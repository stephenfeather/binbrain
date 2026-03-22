import hashlib
import json
import logging
import threading
import urllib.request
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db import repository
from app.deps import (
    SessionLocal, OLLAMA_URL, logger,
    get_active_vision_model, load_settings_from_db,
)
from app.routes import health, items, bins, photos, upc, admin

app = FastAPI(title="BinBrain API")


@app.on_event("startup")
def warmup_vision_model():
    """Load persisted settings, then pre-load the vision model into Ollama."""
    load_settings_from_db()
    model = get_active_vision_model()
    logger.info("event=warmup_start model=%s ollama_url=%s", model, OLLAMA_URL)
    try:
        payload = json.dumps({
            "model": model,
            "prompt": "",
            "keep_alive": -1,
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
        logger.info("event=warmup_done model=%s", model)
    except Exception as e:
        logger.warning("event=warmup_failed model=%s error=%s", model, str(e)[:200])


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


_AUTH_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json"}


@app.middleware("http")
async def api_key_auth_middleware(request: Request, call_next):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    raw_key = request.headers.get("x-api-key")
    request_id = getattr(request.state, "request_id", None)

    if not raw_key:
        return JSONResponse(
            status_code=401,
            content={
                "version": "1",
                "error": {
                    "code": "unauthorized",
                    "message": "Missing X-API-Key header",
                    "request_id": request_id,
                },
            },
            headers={"x-request-id": request_id or ""},
        )

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    db = SessionLocal()
    try:
        key_row = repository.validate_api_key(db, key_hash)
    finally:
        db.close()

    if not key_row:
        return JSONResponse(
            status_code=401,
            content={
                "version": "1",
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or revoked API key",
                    "request_id": request_id,
                },
            },
            headers={"x-request-id": request_id or ""},
        )

    request.state.api_key_id = key_row["id"]

    # Fire-and-forget last_used update
    def _touch():
        s = SessionLocal()
        try:
            repository.touch_api_key_last_used(s, key_row["id"])
            s.commit()
        finally:
            s.close()
    threading.Thread(target=_touch, daemon=True).start()

    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc):
    status_code = exc.status_code
    code_map = {
        400: "bad_request",
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        429: "rate_limited",
        500: "internal_error",
        502: "service_unavailable",
        503: "service_unavailable",
    }
    error_code = code_map.get(status_code, "bad_request" if status_code == 422 else "internal_error")
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    details = exc.detail if status_code == 400 else None
    return JSONResponse(
        status_code=status_code,
        content={
            "version": "1",
            "error": {
                "code": error_code,
                "message": message,
                **({"details": details} if details is not None else {}),
                "request_id": getattr(request.state, "request_id", None),
            }
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={
            "version": "1",
            "error": {
                "code": "bad_request",
                "message": "validation error",
                "details": exc.errors(),
                "request_id": getattr(request.state, "request_id", None),
            }
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("event=unhandled_error request_id=%s", getattr(request.state, "request_id", None))
    return JSONResponse(
        status_code=500,
        content={
            "version": "1",
            "error": {
                "code": "internal_error",
                "message": "internal server error",
                "request_id": getattr(request.state, "request_id", None),
            }
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


# ── Register route modules ──────────────────────────────────────────────────

app.include_router(health.router)
app.include_router(items.router)
app.include_router(bins.router)
app.include_router(photos.router)
app.include_router(upc.router)
app.include_router(admin.router)
