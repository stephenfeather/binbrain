import json
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Body, Request
from sqlalchemy.orm import Session

from app.db import repository
from app.deps import (
    get_db, SessionLocal, OLLAMA_URL, VISION_BASE_URL, logger,
    get_active_vision_model, set_active_vision_model,
    get_max_image_px, set_max_image_px,
    get_detection_model_id, set_detection_model,
    DETECTION_MODEL_ALLOWLIST,
)
from app.middleware import require_admin
from app.services.rate_limiter import require_warmup_rate_limit

router = APIRouter()


def _is_local_ollama() -> bool:
    """True when VISION_BASE_URL points to a local Ollama instance."""
    return "localhost" in VISION_BASE_URL or "host.docker.internal" in VISION_BASE_URL or "127.0.0.1" in VISION_BASE_URL


@router.get("/models")
def list_models(request: Request = None):
    """List available vision models and the active selection.

    When VISION_BASE_URL points to a hosted provider (e.g. Fireworks.ai),
    Ollama's model list is skipped and models returns empty — the active_model
    and vision_provider fields describe the configured backend instead.
    """
    request_id = getattr(request.state, "request_id", None) if request else None
    active = get_active_vision_model()

    if not _is_local_ollama():
        from urllib.parse import urlparse
        provider_host = urlparse(VISION_BASE_URL).hostname or VISION_BASE_URL
        logger.info("event=models_list request_id=%s provider=%s active=%s", request_id, provider_host, active)
        return {
            "version": "1",
            "active_model": active,
            "vision_provider": provider_host,
            "models": [],
        }

    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.warning("event=models_list_failed request_id=%s error=%s", request_id, str(e)[:200])
        raise HTTPException(status_code=502, detail="upstream service unavailable")

    models = []
    for m in data.get("models", []):
        models.append({
            "name": m.get("name"),
            "size": m.get("size"),
            "modified_at": m.get("modified_at"),
        })

    logger.info("event=models_list request_id=%s count=%s active=%s", request_id, len(models), active)
    return {
        "version": "1",
        "active_model": active,
        "vision_provider": "localhost",
        "models": models,
    }


@router.get("/models/running")
def running_models(request: Request = None):
    """List models currently loaded in Ollama VRAM/memory."""
    request_id = getattr(request.state, "request_id", None) if request else None
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/ps")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.warning("event=models_running_failed request_id=%s error=%s", request_id, str(e)[:200])
        raise HTTPException(status_code=502, detail="upstream service unavailable")

    models = []
    for m in data.get("models", []):
        models.append({
            "name": m.get("name"),
            "size": m.get("size"),
            "size_vram": m.get("size_vram"),
            "expires_at": m.get("expires_at"),
        })

    logger.info("event=models_running request_id=%s count=%s", request_id, len(models))
    return {
        "version": "1",
        "active_model": get_active_vision_model(),
        "models": models,
    }


@router.post("/models/select", dependencies=[Depends(require_admin), Depends(require_warmup_rate_limit)])
def select_model(
    payload: dict = Body(...),
    request: Request = None,
):
    """Select a vision model and warm it up on the Ollama server. Requires admin."""
    request_id = getattr(request.state, "request_id", None) if request else None

    model_name = (payload.get("model") or "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="model is required")

    # Warm up the model by sending a lightweight generate request with keep_alive=-1
    try:
        warmup_payload = json.dumps({
            "model": model_name,
            "prompt": "",
            "keep_alive": -1,
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=warmup_payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except Exception as e:
        logger.warning("event=model_select_warmup_failed request_id=%s model=%s error=%s", request_id, model_name, str(e)[:200])
        raise HTTPException(status_code=502, detail="upstream service unavailable")

    previous = get_active_vision_model()
    set_active_vision_model(model_name)

    try:
        settings_db = SessionLocal()
        repository.set_setting(settings_db, "active_vision_model", model_name)
        settings_db.commit()
        settings_db.close()
    except Exception as e:
        logger.warning("event=setting_persist_failed key=active_vision_model error=%s", str(e)[:200])

    logger.info("event=model_select request_id=%s previous=%s active=%s", request_id, previous, model_name)
    return {
        "version": "1",
        "previous_model": previous,
        "active_model": model_name,
    }


@router.get("/settings/image-size")
def get_image_size(request: Request = None):
    """Return the current max image size used for vision inference."""
    return {
        "version": "1",
        "max_image_px": get_max_image_px(),
    }


@router.post("/settings/image-size", dependencies=[Depends(require_admin)])
def set_image_size(
    payload: dict = Body(...),
    request: Request = None,
):
    """Set the max image size (longest side in pixels) for vision inference. Requires admin."""
    request_id = getattr(request.state, "request_id", None) if request else None

    value = payload.get("max_image_px")
    if value is None:
        raise HTTPException(status_code=400, detail="max_image_px is required")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_image_px must be an integer")
    if value < 128 or value > 4096:
        raise HTTPException(status_code=400, detail="max_image_px must be between 128 and 4096")

    previous = get_max_image_px()
    set_max_image_px(value)

    try:
        settings_db = SessionLocal()
        repository.set_setting(settings_db, "max_image_px", str(value))
        settings_db.commit()
        settings_db.close()
    except Exception as e:
        logger.warning("event=setting_persist_failed key=max_image_px error=%s", str(e)[:200])

    logger.info("event=image_size_set request_id=%s previous=%s new=%s", request_id, previous, value)
    return {
        "version": "1",
        "previous_max_image_px": previous,
        "max_image_px": value,
    }


# ── Detection Model Settings (F-02: allowlist enforced) ───────────────────────


@router.get("/settings/detection-model")
def get_detection_model_setting(request: Request = None):
    """Return the current detection model logical ID and available options."""
    return {
        "version": "1",
        "detection_model": get_detection_model_id(),
        "available_models": list(DETECTION_MODEL_ALLOWLIST.keys()),
    }


@router.post("/settings/detection-model", dependencies=[Depends(require_admin)])
def set_detection_model_setting(
    payload: dict = Body(...),
    request: Request = None,
):
    """Set the detection model by logical ID (allowlisted). Requires admin.

    Accepts only IDs from DETECTION_MODEL_ALLOWLIST; arbitrary paths are rejected
    with 400 to prevent the authenticated RCE chain described in F-02.
    """
    request_id = getattr(request.state, "request_id", None) if request else None

    model_id = (payload.get("detection_model") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="detection_model is required")

    if model_id not in DETECTION_MODEL_ALLOWLIST:
        raise HTTPException(
            status_code=400,
            detail=(
                f"detection_model {model_id!r} is not in the allowlist; "
                f"allowed: {list(DETECTION_MODEL_ALLOWLIST)}"
            ),
        )

    previous = get_detection_model_id()
    set_detection_model(model_id)

    try:
        settings_db = SessionLocal()
        repository.set_setting(settings_db, "detection_model", model_id)
        settings_db.commit()
        settings_db.close()
    except Exception as e:
        logger.warning("event=setting_persist_failed key=detection_model error=%s", str(e)[:200])

    logger.info("event=detection_model_set request_id=%s previous=%s new=%s", request_id, previous, model_id)
    return {
        "version": "1",
        "previous_detection_model": previous,
        "detection_model": model_id,
    }


# ── API Key Management (F-03: admin required) ─────────────────────────────────


@router.post("/admin/api-keys", dependencies=[Depends(require_admin)])
def admin_create_api_key(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    role = (payload.get("role") or "user").strip()
    if role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")

    key_hash, raw_key = repository.create_api_key(db, name, role=role)
    db.commit()

    logger.info(
        "event=api_key_create request_id=%s name=%s role=%s",
        db.info.get("request_id"),
        name,
        role,
    )
    return {
        "version": "1",
        "key": raw_key,
        "name": name,
        "role": role,
        "message": "Save this key — it will not be shown again.",
    }


@router.get("/admin/api-keys", dependencies=[Depends(require_admin)])
def admin_list_api_keys(
    db: Session = Depends(get_db),
):
    keys = repository.list_api_keys(db)
    return {"version": "1", "keys": keys}


@router.delete("/admin/api-keys/{key_id}", dependencies=[Depends(require_admin)])
def admin_revoke_api_key(
    key_id: int,
    db: Session = Depends(get_db),
):
    revoked = repository.revoke_api_key(db, key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="key not found or already revoked")
    db.commit()

    logger.info(
        "event=api_key_revoke request_id=%s key_id=%s",
        db.info.get("request_id"),
        key_id,
    )
    return {"version": "1", "key_id": key_id, "revoked": True}
