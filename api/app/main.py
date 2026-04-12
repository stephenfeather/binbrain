import hashlib
import json
import threading
import urllib.request
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db import repository
from app.deps import (
    SessionLocal, OLLAMA_URL, logger,
    get_active_vision_model, load_settings_from_db,
    MAX_REQUEST_BODY_BYTES, MODELS_DIR, photo_root,
)
from app.routes import health, items, bins, photos, upc, admin, classes, locations
from app.services.rate_limiter import global_limiter, _ADMIN_MULTIPLIER


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load persisted settings, assert security invariants, then warm up Ollama."""
    load_settings_from_db()

    # F-02: assert models directory is outside PHOTO_DIR to prevent path-confusion attacks.
    models_resolved = MODELS_DIR.resolve()
    photo_resolved = photo_root.resolve()
    if str(models_resolved).startswith(str(photo_resolved)):
        raise RuntimeError(
            f"MODELS_DIR ({MODELS_DIR}) must not be inside PHOTO_DIR ({photo_root}). "
            "Set MODELS_DIR to a directory outside the photo upload area."
        )

    # Load confirmed classes; defer YOLO-World init to first detection request
    from app.services import class_registry, detection
    db = SessionLocal()
    try:
        class_registry.load_from_db(db)
        class_registry.set_reload_callback(detection.reload_classes)
        detection.set_deferred_classes(class_registry.get_classes())
    finally:
        db.close()

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

    yield


app = FastAPI(title="BinBrain API", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


# F-04: reject oversized requests before any buffering occurs.
@app.middleware("http")
async def limit_body_size_middleware(request: Request, call_next):
    content_length_header = request.headers.get("content-length")
    if content_length_header:
        try:
            cl = int(content_length_header)
        except ValueError:
            cl = 0
        if cl > MAX_REQUEST_BODY_BYTES:
            request_id = getattr(request.state, "request_id", None)
            return JSONResponse(
                status_code=413,
                content={
                    "version": "1",
                    "error": {
                        "code": "payload_too_large",
                        "message": (
                            f"Request body exceeds the {MAX_REQUEST_BODY_BYTES}-byte limit"
                        ),
                        "request_id": request_id,
                    },
                },
            )
    return await call_next(request)


_AUTH_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json"}


# F-08: global rate limit — runs after auth middleware sets api_key_id / api_key_role.
# Middleware registration is LIFO: this is defined before api_key_auth in code so it
# executes AFTER auth in the request pipeline (auth is outermost).
@app.middleware("http")
async def global_rate_limit_middleware(request: Request, call_next):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    key = str(getattr(request.state, "api_key_id", "anon"))
    role = getattr(request.state, "api_key_role", "user")
    multiplier = _ADMIN_MULTIPLIER if role == "admin" else 1.0
    if not global_limiter.check(key, role_multiplier=multiplier):
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=429,
            content={
                "version": "1",
                "error": {
                    "code": "rate_limited",
                    "message": "rate limit exceeded",
                    "request_id": request_id,
                },
            },
        )
    return await call_next(request)


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
    # F-03: attach role so require_admin dependency can enforce authorization.
    request.state.api_key_role = key_row.get("role", "user")

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
        403: "forbidden",
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


# ── Register route modules ──────────────────────────────────────────────────────

app.include_router(health.router)
app.include_router(items.router)
app.include_router(bins.router)
app.include_router(photos.router)
app.include_router(upc.router)
app.include_router(admin.router)
app.include_router(classes.router)
app.include_router(locations.router)
