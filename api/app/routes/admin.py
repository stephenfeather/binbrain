import json
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Body, Request
from sqlalchemy.orm import Session

from app.db import repository
from app.deps import (
    get_db, OLLAMA_URL, logger,
    get_active_vision_model, set_active_vision_model,
    get_max_image_px, set_max_image_px,
)

router = APIRouter()


@router.get("/models")
def list_models(request: Request = None):
    """List vision models available on the Ollama server."""
    request_id = getattr(request.state, "request_id", None) if request else None
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.warning("event=models_list_failed request_id=%s error=%s", request_id, str(e)[:200])
        raise HTTPException(status_code=502, detail=f"cannot reach Ollama: {e}")

    models = []
    for m in data.get("models", []):
        models.append({
            "name": m.get("name"),
            "size": m.get("size"),
            "modified_at": m.get("modified_at"),
        })

    active = get_active_vision_model()
    logger.info("event=models_list request_id=%s count=%s active=%s", request_id, len(models), active)
    return {
        "version": "1",
        "active_model": active,
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
        raise HTTPException(status_code=502, detail=f"cannot reach Ollama: {e}")

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


@router.post("/models/select")
def select_model(
    payload: dict = Body(...),
    request: Request = None,
):
    """Select a vision model and warm it up on the Ollama server."""
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
            # Read the streamed response to completion
            resp.read()
    except Exception as e:
        logger.warning("event=model_select_warmup_failed request_id=%s model=%s error=%s", request_id, model_name, str(e)[:200])
        raise HTTPException(status_code=502, detail=f"failed to warm up model: {e}")

    previous = get_active_vision_model()
    set_active_vision_model(model_name)

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


@router.post("/settings/image-size")
def set_image_size(
    payload: dict = Body(...),
    request: Request = None,
):
    """Set the max image size (longest side in pixels) for vision inference."""
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

    logger.info("event=image_size_set request_id=%s previous=%s new=%s", request_id, previous, value)
    return {
        "version": "1",
        "previous_max_image_px": previous,
        "max_image_px": value,
    }


# ── API Key Management ─────────────────────────────────────────────────


@router.post("/admin/api-keys")
def admin_create_api_key(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    key_hash, raw_key = repository.create_api_key(db, name)
    db.commit()

    logger.info(
        "event=api_key_create request_id=%s name=%s",
        db.info.get("request_id"),
        name,
    )
    return {
        "version": "1",
        "key": raw_key,
        "name": name,
        "message": "Save this key — it will not be shown again.",
    }


@router.get("/admin/api-keys")
def admin_list_api_keys(
    db: Session = Depends(get_db),
):
    keys = repository.list_api_keys(db)
    return {"version": "1", "keys": keys}


@router.delete("/admin/api-keys/{key_id}")
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
