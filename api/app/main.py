import hashlib
import json
import threading
import urllib.request
import uuid
from contextlib import asynccontextmanager

from app.body_size_middleware import BodySizeLimitMiddleware
from app.db import repository
from app.deps import (
    MAX_REQUEST_BODY_BYTES,
    MODELS_DIR,
    OLLAMA_URL,
    VISION_BASE_URL,
    SessionLocal,
    get_active_vision_model,
    load_settings_from_db,
    logger,
    photo_root,
)
from app.routes import (
    admin,
    bins,
    classes,
    health,
    items,
    locations,
    photos,
    sessions,
    upc,
)
from app.services.rate_limiter import _ADMIN_MULTIPLIER, global_limiter
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load persisted settings, assert security invariants, then warm up Ollama."""
    logger.info("event=startup_begin")
    load_settings_from_db()
    logger.info("event=settings_loaded")

    # F-02: assert models directory is outside PHOTO_DIR to prevent path-confusion attacks.
    models_resolved = MODELS_DIR.resolve()
    photo_resolved = photo_root.resolve()
    logger.info(
        "event=validating_security_invariants models_dir=%s photo_dir=%s",
        models_resolved,
        photo_resolved,
    )
    if str(models_resolved).startswith(str(photo_resolved)):
        logger.critical(
            "event=security_violation_detected models_dir=%s photo_dir=%s",
            models_resolved,
            photo_resolved,
        )
        raise RuntimeError(
            f"MODELS_DIR ({MODELS_DIR}) must not be inside PHOTO_DIR ({photo_root}). "
            "Set MODELS_DIR to a directory outside the photo upload area."
        )
    logger.info("event=security_validation_passed")

    # Load confirmed classes; defer YOLO-World init to first detection request
    from app.services import class_registry, detection

    logger.info("event=loading_class_registry")
    db = SessionLocal()
    try:
        class_registry.load_from_db(db)
        class_count = len(class_registry.get_classes())
        logger.info("event=class_registry_loaded count=%d", class_count)
        class_registry.set_reload_callback(detection.reload_classes)
        detection.set_deferred_classes(class_registry.get_classes())
        logger.info("event=deferred_detection_initialized")
    finally:
        db.close()

    model = get_active_vision_model()
    is_local = (
        "localhost" in VISION_BASE_URL
        or "host.docker.internal" in VISION_BASE_URL
        or "127.0.0.1" in VISION_BASE_URL
    )
    logger.info(
        "event=vision_config_loaded model=%s is_local=%s base_url=%s",
        model,
        is_local,
        VISION_BASE_URL,
    )
    if is_local:
        logger.info("event=warmup_start model=%s ollama_url=%s", model, OLLAMA_URL)
        try:
            payload = json.dumps(
                {
                    "model": model,
                    "prompt": "",
                    "keep_alive": -1,
                }
            ).encode()
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
    else:
        logger.info(
            "event=warmup_skipped reason=hosted_provider model=%s base_url=%s",
            model,
            VISION_BASE_URL,
        )

    logger.info("event=startup_complete status=healthy")
    yield
    logger.info("event=shutdown_begin")
    logger.info("event=shutdown_complete")


app = FastAPI(title="BinBrain API", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


# FF-01 (was F-04): reject oversized requests before any buffering occurs.
# Pure ASGI middleware — enforces both Content-Length fast-path AND cumulative
# streaming byte count, so chunked transfers and spoofed Content-Length headers
# cannot bypass the cap. Installed via ``add_middleware`` (see below).


_AUTH_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json"}


def _rate_limit_key_for(request: Request) -> str:
    """Return the rate-limit bucket key for *request*.

    Prefers the authenticated api_key_id (set by api_key_auth_middleware).
    Falls back to ``ip:<client_ip>`` so unauthenticated requests get per-IP
    buckets instead of sharing a single "anon" bucket.
    """
    api_key_id = getattr(request.state, "api_key_id", None)
    if api_key_id is not None:
        return str(api_key_id)
    client = request.client
    ip = client.host if client else "unknown"
    return f"ip:{ip}"


def _assert_auth_runs_before_rate_limit(names: list) -> None:
    """Fail-fast if middleware registration order would put rate-limit outside of auth.

    Starlette's ``app.user_middleware`` stores middleware in LIFO order:
    index 0 is the outermost middleware (runs FIRST in the request pipeline).
    api_key_auth_middleware must appear at a LOWER index than
    global_rate_limit_middleware so that auth executes first and populates
    ``request.state.api_key_id`` before the limiter reads it.

    *names* is a list of middleware function names as returned by iterating
    ``app.user_middleware`` (index 0 = outermost = runs first).

    Raises RuntimeError with a clear message if the invariant is broken.
    """
    try:
        auth_idx = names.index("api_key_auth_middleware")
        rate_idx = names.index("global_rate_limit_middleware")
    except ValueError as exc:
        raise RuntimeError(
            f"Expected both api_key_auth_middleware and global_rate_limit_middleware "
            f"in middleware list; got {names}"
        ) from exc

    # user_middleware stores in LIFO order: index 0 = outermost = runs FIRST.
    # api_key_auth must run BEFORE rate-limit, so auth must be at a LOWER index.
    if auth_idx >= rate_idx:
        raise RuntimeError(
            f"Middleware ordering invariant violated: api_key_auth_middleware "
            f"must appear before global_rate_limit_middleware in user_middleware "
            f"(lower index = outermost = runs first). Current order: {names}"
        )


# F-08: global rate limit — runs after auth middleware sets api_key_id / api_key_role.
# Middleware registration is LIFO: this is defined before api_key_auth in code so it
# executes AFTER auth in the request pipeline (auth is outermost).
@app.middleware("http")
async def global_rate_limit_middleware(request: Request, call_next):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    key = _rate_limit_key_for(request)
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
        # QA F-3 fix: give 410 its own stable code so idempotent clients
        # (e.g. iOS SessionManager closing an already-closed session via the
        # offline queue) don't see misleading ``internal_error`` and alert
        # on what is, by contract, a benign no-op.
        410: "session_closed",
        413: "payload_too_large",
        415: "unsupported_media_type",
        429: "rate_limited",
        500: "internal_error",
        502: "service_unavailable",
        503: "service_unavailable",
    }
    error_code = code_map.get(
        status_code, "bad_request" if status_code == 422 else "internal_error"
    )
    # ApiDev_008: routes may supply a richer detail — a dict with an explicit
    # ``code`` + ``message`` — so the caller sees a domain-specific code
    # (e.g. ``invalid_session``) instead of the generic status-mapped one.
    # Backwards-compatible: string details behave exactly as before.
    if isinstance(exc.detail, dict) and isinstance(exc.detail.get("code"), str):
        error_code = exc.detail["code"]
        message = str(exc.detail.get("message", ""))
        details = None
    else:
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
            },
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


def _sanitize_float_specials(obj):
    """Replace inf/-inf/NaN floats with string sentinels.

    ApiDev2_014: Starlette's JSONResponse uses ``allow_nan=False``, so if
    an error dict's ``input`` field carries a rejected NaN/Infinity value
    the response render itself raises ``ValueError`` and leaks a 500 that
    masks the original 400. Walk the structure once and stringify any
    non-finite float so the 400 renders cleanly.
    """
    import math

    if isinstance(obj, float):
        if math.isnan(obj):
            return "NaN"
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_float_specials(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_float_specials(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Pydantic error dicts can contain non-JSON-serializable values in `input`
    # (e.g. raw `bytes` when a client posts multipart to a JSON endpoint, or
    # float inf/NaN when Pydantic rejected them via allow_inf_nan=False).
    # Route through jsonable_encoder with a bytes fallback + float-special
    # sanitizer so the 400 handler itself never crashes and masks a 400 as
    # a 500.
    details = jsonable_encoder(
        exc.errors(),
        custom_encoder={bytes: lambda b: b[:256].decode("utf-8", "backslashreplace")},
    )
    details = _sanitize_float_specials(details)
    return JSONResponse(
        status_code=400,
        content={
            "version": "1",
            "error": {
                "code": "bad_request",
                "message": "validation error",
                "details": details,
                "request_id": getattr(request.state, "request_id", None),
            },
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "event=unhandled_error request_id=%s", getattr(request.state, "request_id", None)
    )
    return JSONResponse(
        status_code=500,
        content={
            "version": "1",
            "error": {
                "code": "internal_error",
                "message": "internal server error",
                "request_id": getattr(request.state, "request_id", None),
            },
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


# ── Register pure-ASGI middleware (outermost) ───────────────────────────────────
# FF-01: body-size cap must run before auth/rate-limit/request-id so that
# oversize bodies (including chunked or spoofed Content-Length) are rejected
# before any downstream code — including the multipart parser that would
# otherwise spool to disk — observes the request body.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)


# ── Middleware ordering assertion ───────────────────────────────────────────────
# Fail fast at import time if the LIFO ordering invariant is broken.
_assert_auth_runs_before_rate_limit(
    [
        getattr(
            getattr(mw, "kwargs", {}).get("dispatch") or getattr(mw, "cls", None),
            "__name__",
            "",
        )
        for mw in app.user_middleware
    ]
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
app.include_router(sessions.router)
