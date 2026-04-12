"""Pure configuration constants — no I/O, no DB, safe to import in unit tests."""
import os
from pathlib import Path

# ── Upload limits (F-04) ──────────────────────────────────────────────────────
MAX_REQUEST_BODY_BYTES: int = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(50 * 1024 * 1024)))
MAX_FILE_BYTES: int = int(os.environ.get("MAX_FILE_BYTES", str(15 * 1024 * 1024)))
MAX_FILES_PER_REQUEST: int = int(os.environ.get("MAX_FILES_PER_REQUEST", "20"))

# ── Detection model allowlist (F-02) ─────────────────────────────────────────
# Maps logical ID -> filename within MODELS_DIR.
# Only IDs listed here may be selected via the API.
MODELS_DIR: Path = Path(os.environ.get("MODELS_DIR", "/models"))

DETECTION_MODEL_ALLOWLIST: dict[str, str] = {
    "yoloe-v8s-seg": "yoloe-v8s-seg.pt",
    "yolo11s": "yolo11s.pt",
}

# Default model ID: prefer YOLO_MODEL_ID env var; fall back to first allowlist entry.
DEFAULT_DETECTION_MODEL_ID: str = os.environ.get("YOLO_MODEL_ID", "yoloe-v8s-seg")
if DEFAULT_DETECTION_MODEL_ID not in DETECTION_MODEL_ALLOWLIST:
    DEFAULT_DETECTION_MODEL_ID = next(iter(DETECTION_MODEL_ALLOWLIST))
