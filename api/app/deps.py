import logging
import logging.handlers
import os
from datetime import datetime, timezone
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


def _parse_suggest_match_threshold_env() -> float:
    """Parse ``SUGGEST_MATCH_THRESHOLD`` from the environment (S-01).

    Applied at import time as the pre-DB fallback for
    ``_suggest_match_threshold``. Swallows malformed or out-of-range values
    (logs a warning and returns the hardcoded 0.85 default) so a bad
    operator env cannot crash the FastAPI import.
    """
    raw = os.environ.get("SUGGEST_MATCH_THRESHOLD", "0.85")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logging.getLogger("binbrain").warning(
            "event=settings_env_invalid key=SUGGEST_MATCH_THRESHOLD value=%r "
            "(not a float; using default 0.85)",
            raw,
        )
        return 0.85
    if not (0.0 <= value <= 1.0):
        logging.getLogger("binbrain").warning(
            "event=settings_env_invalid key=SUGGEST_MATCH_THRESHOLD value=%r "
            "(outside [0.0, 1.0]; using default 0.85)",
            raw,
        )
        return 0.85
    return value


# Mutable at runtime via POST /settings/suggest-match-threshold (S-01)
_suggest_match_threshold = _parse_suggest_match_threshold_env()

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

photo_root = Path(PHOTO_DIR)
photo_root.mkdir(parents=True, exist_ok=True)

# Local CPU text embeddings (bge-small-en-v1.5 => 384 dims)
embedder = TextEmbedding(model_name=EMBED_MODEL_NAME)

_LOG_DIR = Path(os.environ.get("LOG_DIR", "/data/logs"))


class _ISO8601Formatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return (
            datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )


_log_fmt = _ISO8601Formatter("%(asctime)s %(message)s")

_root = logging.getLogger()
_root.setLevel(logging.INFO)

_stream_h = logging.StreamHandler()
_stream_h.setFormatter(_log_fmt)
_root.addHandler(_stream_h)

try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _file_h = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "api.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _file_h.setFormatter(_log_fmt)
    _root.addHandler(_file_h)
except OSError as _log_exc:
    logging.getLogger("binbrain").warning(
        "event=log_file_init_failed path=%s err=%s", _LOG_DIR / "api.log", _log_exc
    )

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


def get_suggest_match_threshold() -> float:
    """Return the current cosine-similarity floor for auto-suggestions (S-01).

    Source-of-truth order (populated at startup by
    :func:`load_settings_from_db`):

    1. ``settings`` DB row (``suggest_match_threshold``).
    2. ``SUGGEST_MATCH_THRESHOLD`` environment variable.
    3. Hardcoded ``0.85``.
    """
    return _suggest_match_threshold


def set_suggest_match_threshold(
    value: float,
    *,
    actor_ip: str,
    actor_key_id: str,
) -> None:
    """Set the auto-suggestion match threshold (S-01).

    Follows the S-00-AUDIT canonical setter template: validates the value,
    writes ``settings`` and ``app_settings_audit`` in the same transaction
    (so an audit failure discards the settings write too), and only then
    updates the module-level cache.

    Raises:
        ValueError: if ``value`` is not a finite number in ``[0.0, 1.0]``.
    """
    from app.db import repository

    # Reject bool before float — in Python, ``True``/``False`` are ``int``
    # instances and float(True) == 1.0, which would sneak past the range check.
    if isinstance(value, bool):
        raise ValueError("suggest_match_threshold must not be a bool")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"suggest_match_threshold must be a float, got {value!r}") from None
    if parsed != parsed:  # NaN
        raise ValueError("suggest_match_threshold must not be NaN")
    if not (0.0 <= parsed <= 1.0):
        raise ValueError(f"suggest_match_threshold must be in [0.0, 1.0], got {parsed}")

    global _suggest_match_threshold

    db = SessionLocal()
    try:
        old_value = repository.get_setting(db, "suggest_match_threshold")
        new_value = str(parsed)
        repository.set_setting(db, "suggest_match_threshold", new_value)
        repository.log_setting_change(
            db,
            key="suggest_match_threshold",
            old_value=old_value,
            new_value=new_value,
            actor_ip=actor_ip,
            actor_key_id=actor_key_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    _suggest_match_threshold = parsed


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

        suggest_threshold = repository.get_setting(db, "suggest_match_threshold")
        if suggest_threshold:
            global _suggest_match_threshold
            try:
                parsed = float(suggest_threshold)
                if 0.0 <= parsed <= 1.0 and parsed == parsed:
                    _suggest_match_threshold = parsed
                    logger.info(
                        "event=settings_loaded key=suggest_match_threshold value=%s",
                        suggest_threshold,
                    )
                else:
                    logger.warning(
                        "event=settings_load_invalid key=suggest_match_threshold "
                        "value=%s (out of range; keeping prior cache)",
                        suggest_threshold,
                    )
            except (TypeError, ValueError):
                logger.warning(
                    "event=settings_load_invalid key=suggest_match_threshold value=%s",
                    suggest_threshold,
                )
    except Exception as e:
        logger.warning("event=settings_load_failed error=%s", str(e)[:200])
    finally:
        db.close()
