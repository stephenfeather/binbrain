import logging
import os
from pathlib import Path
from typing import Optional

from app.config import (
    DEFAULT_DETECTION_MODEL_ID,
    DETECTION_MODEL_ALLOWLIST,
    MAX_FILE_BYTES,
    MAX_FILES_PER_REQUEST,
    MAX_REQUEST_BODY_BYTES,
    MODELS_DIR,
)
from fastapi import Request
from fastembed import TextEmbedding
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]
PHOTO_DIR = os.environ.get("PHOTO_DIR", "/data/photos")
EMBED_MODEL_NAME = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "http://localhost:11434/v1")
VISION_API_KEY = os.environ.get("VISION_API_KEY", "ollama")

# Re-export for callers that import these from app.deps (backwards compat).
MAX_REQUEST_BODY_BYTES = MAX_REQUEST_BODY_BYTES
MAX_FILE_BYTES = MAX_FILE_BYTES
MAX_FILES_PER_REQUEST = MAX_FILES_PER_REQUEST
MODELS_DIR = MODELS_DIR
DETECTION_MODEL_ALLOWLIST = DETECTION_MODEL_ALLOWLIST

# Mutable at runtime via POST /settings/detection-model
_detection_model_id: str = DEFAULT_DETECTION_MODEL_ID

# Mutable at runtime via POST /settings/image-size
_max_image_px = int(os.environ.get("OLLAMA_MAX_IMAGE_PX", "1280"))

# Mutable at runtime via POST /models/select
_active_vision_model = os.environ.get("OLLAMA_VISION_MODEL", "qwen3-vl:4b")

# YOLO-World confidence threshold (lower than YOLO11s due to zero-shot)
_yolo_world_conf = float(os.environ.get("YOLO_WORLD_CONF", "0.15"))

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

photo_root = Path(PHOTO_DIR)
photo_root.mkdir(parents=True, exist_ok=True)

# Local CPU text embeddings (bge-small-en-v1.5 => 384 dims)
embedder = TextEmbedding(model_name=EMBED_MODEL_NAME)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("binbrain")


def get_db(request: Request):
    db = SessionLocal()
    try:
        db.info["request_id"] = getattr(request.state, "request_id", None)
        yield db
    finally:
        db.close()


def canonical_item_text(name: str, category: Optional[str], notes: Optional[str]) -> str:
    parts = [f"name: {name}"]
    if category:
        parts.append(f"category: {category}")
    if notes:
        parts.append(f"notes: {notes}")
    return "\n".join(parts)


def fingerprint_for(name: str, category: Optional[str]) -> str:
    name_part = (name or "").strip().lower()
    cat_part = (category or "").strip().lower()
    return f"{name_part}|{cat_part}"


def embed_text(s: str) -> list[float]:
    s = (s or "").strip()
    if not s:
        raise ValueError("empty text")
    vec = next(embedder.embed([s]))
    return vec.tolist()


def vec_to_pgvector(vec: list[float]) -> str:
    # pgvector accepts a string like: [0.1,0.2,...]
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def get_active_vision_model() -> str:
    return _active_vision_model


def set_active_vision_model(model: str):
    # TODO(audit): conform to new setter signature
    # (value, *, actor_ip: str, actor_key_id: str) and call
    # repository.log_setting_change(...) in-transaction with set_setting.
    # Audit infra landed in S-00-AUDIT (2026-04-20b migration); see
    # ``thoughts/shared/plans/2026-04-20-runtime-settings-store.md`` §Audit Log
    # and conformance tasks A-1..A-3.
    global _active_vision_model
    _active_vision_model = model


def get_max_image_px() -> int:
    return _max_image_px


def set_max_image_px(value: int):
    # TODO(audit): conform to new setter signature
    # (value, *, actor_ip: str, actor_key_id: str) and call
    # repository.log_setting_change(...) in-transaction with set_setting.
    # See S-00-AUDIT / A-1..A-3 in the runtime-settings-store plan.
    global _max_image_px
    _max_image_px = value


# ── Detection model accessors (F-02) ─────────────────────────────────────────


def get_detection_model() -> str:
    """Return the resolved filesystem path of the active detection model.

    Always derived from the allowlist — never a raw user-supplied path.
    """
    filename = DETECTION_MODEL_ALLOWLIST[_detection_model_id]
    return str(MODELS_DIR / filename)


def get_detection_model_id() -> str:
    """Return the current detection model logical ID."""
    return _detection_model_id


def set_detection_model(model_id: str) -> None:
    """Set the active detection model by logical ID.

    Raises:
        ValueError: if model_id is not in DETECTION_MODEL_ALLOWLIST.
    """
    # TODO(audit): conform to new setter signature
    # (value, *, actor_ip: str, actor_key_id: str) and call
    # repository.log_setting_change(...) in-transaction with set_setting.
    # See S-00-AUDIT / A-1..A-3 in the runtime-settings-store plan.
    global _detection_model_id
    if model_id not in DETECTION_MODEL_ALLOWLIST:
        raise ValueError(
            f"model_id {model_id!r} is not in the detection model allowlist; "
            f"allowed: {list(DETECTION_MODEL_ALLOWLIST)}"
        )
    _detection_model_id = model_id


def get_yolo_world_conf() -> float:
    return _yolo_world_conf


def set_yolo_world_conf(value: float):
    """CANONICAL SETTER TEMPLATE (pre-S-00-AUDIT shape — kept minimal pending A-1..A-3).

    The target signature for every S-NN / RL-NN / A-NN setter after
    S-00-AUDIT lands is::

        def set_<name>(value: <Type>, *, actor_ip: str, actor_key_id: str) -> None:
            # 1. Validate value (type + range). Raise ValueError on bad input.
            # 2. Open a session; read current value from repository.get_setting().
            # 3. repository.set_setting(db, "<key>", str(value))
            # 4. repository.log_setting_change(db, key="<key>",
            #        old_value=<prev>, new_value=str(value),
            #        actor_ip=actor_ip, actor_key_id=actor_key_id)
            # 5. db.commit()  # atomic — rollback discards both writes
            # 6. Update the module-level cache.

    Steps 3 and 4 must share a single transaction so a failed audit write
    rolls back the value change too, preserving the invariant that every
    persisted ``settings`` row has an ``app_settings_audit`` row.
    """
    # TODO(audit): conform to new setter signature
    # (value, *, actor_ip: str, actor_key_id: str) and call
    # repository.log_setting_change(...) in-transaction with set_setting.
    # See S-00-AUDIT / A-1..A-3 in the runtime-settings-store plan.
    global _yolo_world_conf
    _yolo_world_conf = value


def load_settings_from_db() -> None:
    """Load persisted settings from DB, falling back to env/defaults."""
    from app.db import repository

    db = SessionLocal()
    try:
        vision_model = repository.get_setting(db, "active_vision_model")
        if vision_model:
            set_active_vision_model(vision_model)
            logger.info("event=settings_loaded key=active_vision_model value=%s", vision_model)

        max_px = repository.get_setting(db, "max_image_px")
        if max_px:
            try:
                set_max_image_px(int(max_px))
                logger.info("event=settings_loaded key=max_image_px value=%s", max_px)
            except (TypeError, ValueError):
                logger.warning("event=settings_load_invalid key=max_image_px value=%s", max_px)

        det_model = repository.get_setting(db, "detection_model")
        if det_model:
            try:
                set_detection_model(det_model)
                logger.info("event=settings_loaded key=detection_model value=%s", det_model)
            except ValueError:
                logger.warning(
                    "event=settings_load_invalid key=detection_model value=%s "
                    "(not in allowlist; keeping default)",
                    det_model,
                )
    except Exception as e:
        logger.warning("event=settings_load_failed error=%s", str(e)[:200])
    finally:
        db.close()
