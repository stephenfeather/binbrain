import ipaddress
import json
import urllib.request

from app.db import repository
from app.deps import (
    DETECTION_MODEL_ALLOWLIST,
    OLLAMA_URL,
    VISION_BASE_URL,
    get_active_vision_model,
    get_db,
    get_detection_model_id,
    get_max_image_px,
    get_suggest_match_threshold,
    is_local_ollama,
    logger,
    set_active_vision_model,
    set_detection_model,
    set_max_image_px,
    set_suggest_match_threshold,
)
from app.middleware import require_admin
from app.services.rate_limiter import require_warmup_rate_limit
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

router = APIRouter()


def _actor_audit_context(request: Request) -> tuple[str, str]:
    """Return ``(actor_ip, actor_key_id)`` for an admin audit row.

    ``request.client.host`` is upstream-controlled: TestClient reports the
    literal ``"testclient"`` and some reverse-proxy middleware can surface
    a hostname instead of an IP. The audit-log helper validates the value
    with ``ipaddress.ip_address(...)`` and would reject anything unparseable,
    so normalize unparseable hosts to ``"0.0.0.0"`` (still a valid INET
    that satisfies the audit contract) rather than 400 on something the
    admin has no control over.

    Raises:
        HTTPException(500): if the auth middleware did not attach an
            ``api_key_id`` to ``request.state`` — the audit-log contract
            requires a non-empty ``actor_key_id``.
    """
    raw_host = request.client.host if request and request.client else ""
    try:
        ipaddress.ip_address(raw_host)
        actor_ip = raw_host
    except (TypeError, ValueError):
        actor_ip = "0.0.0.0"
    actor_key_id = str(getattr(request.state, "api_key_id", "") or "")
    if not actor_key_id:
        raise HTTPException(status_code=500, detail="authenticated key missing")
    return actor_ip, actor_key_id


@router.get("/models")
def list_models(request: Request = None):
    """List available vision models and the active selection.

    When VISION_BASE_URL points to a hosted provider (e.g. Fireworks.ai),
    Ollama's model list is skipped and models returns empty — the active_model
    and vision_provider fields describe the configured backend instead.
    """
    request_id = getattr(request.state, "request_id", None) if request else None
    active = get_active_vision_model()

    if not is_local_ollama():
        from urllib.parse import urlparse

        provider_host = urlparse(VISION_BASE_URL).hostname or VISION_BASE_URL
        logger.info(
            "event=models_list request_id=%s provider=%s active=%s",
            request_id,
            provider_host,
            active,
        )
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
        raise HTTPException(status_code=502, detail="upstream service unavailable") from e

    models = []
    for m in data.get("models", []):
        models.append(
            {
                "name": m.get("name"),
                "size": m.get("size"),
                "modified_at": m.get("modified_at"),
            }
        )

    logger.info(
        "event=models_list request_id=%s count=%s active=%s", request_id, len(models), active
    )
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
        logger.warning(
            "event=models_running_failed request_id=%s error=%s", request_id, str(e)[:200]
        )
        raise HTTPException(status_code=502, detail="upstream service unavailable") from e

    models = []
    for m in data.get("models", []):
        models.append(
            {
                "name": m.get("name"),
                "size": m.get("size"),
                "size_vram": m.get("size_vram"),
                "expires_at": m.get("expires_at"),
            }
        )

    logger.info("event=models_running request_id=%s count=%s", request_id, len(models))
    return {
        "version": "1",
        "active_model": get_active_vision_model(),
        "models": models,
    }


@router.post(
    "/models/select", dependencies=[Depends(require_admin), Depends(require_warmup_rate_limit)]
)
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
        warmup_payload = json.dumps(
            {
                "model": model_name,
                "prompt": "",
                "keep_alive": -1,
            }
        ).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=warmup_payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except Exception as e:
        logger.warning(
            "event=model_select_warmup_failed request_id=%s model=%s error=%s",
            request_id,
            model_name,
            str(e)[:200],
        )
        raise HTTPException(status_code=502, detail="upstream service unavailable") from e

    actor_ip, actor_key_id = _actor_audit_context(request)
    previous = get_active_vision_model()
    try:
        set_active_vision_model(model_name, actor_ip=actor_ip, actor_key_id=actor_key_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    logger.info(
        "event=model_select request_id=%s previous=%s active=%s", request_id, previous, model_name
    )
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
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail="max_image_px must be an integer")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_image_px must be an integer") from None

    actor_ip, actor_key_id = _actor_audit_context(request)
    previous = get_max_image_px()
    try:
        set_max_image_px(value, actor_ip=actor_ip, actor_key_id=actor_key_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    logger.info(
        "event=image_size_set request_id=%s previous=%s new=%s", request_id, previous, value
    )
    return {
        "version": "1",
        "previous_max_image_px": previous,
        "max_image_px": value,
    }


# ── Suggest match threshold (S-01: runtime settings store) ────────────────────


@router.get("/settings/suggest-match-threshold")
def get_suggest_match_threshold_route(request: Request = None):
    """Return the current cosine-similarity floor for auto-suggestions."""
    return {
        "version": "1",
        "suggest_match_threshold": get_suggest_match_threshold(),
    }


@router.post("/settings/suggest-match-threshold", dependencies=[Depends(require_admin)])
def set_suggest_match_threshold_route(
    payload: dict = Body(...),
    request: Request = None,
):
    """Set the auto-suggestion cosine-similarity floor. Requires admin.

    Uses the S-00-AUDIT accessor contract: the setter writes ``settings`` +
    ``app_settings_audit`` in a single transaction, tagged with the caller's
    IP and API-key id.
    """
    request_id = getattr(request.state, "request_id", None) if request else None

    if "value" not in payload:
        raise HTTPException(status_code=400, detail="value is required")
    value = payload["value"]
    # Reject bool before handing to the setter — bool is a subclass of int
    # and float(True) would otherwise slip past the range check.
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail="value must be a number")

    actor_ip, actor_key_id = _actor_audit_context(request)
    previous = get_suggest_match_threshold()
    try:
        set_suggest_match_threshold(value, actor_ip=actor_ip, actor_key_id=actor_key_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    new_value = get_suggest_match_threshold()

    logger.info(
        "event=suggest_match_threshold_set request_id=%s previous=%s new=%s",
        request_id,
        previous,
        new_value,
    )
    return {
        "version": "1",
        "previous_suggest_match_threshold": previous,
        "suggest_match_threshold": new_value,
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

    actor_ip, actor_key_id = _actor_audit_context(request)
    previous = get_detection_model_id()
    try:
        set_detection_model(model_id, actor_ip=actor_ip, actor_key_id=actor_key_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    logger.info(
        "event=detection_model_set request_id=%s previous=%s new=%s", request_id, previous, model_id
    )
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
