"""Pure configuration constants — no I/O, no DB, safe to import in unit tests."""

import os
from pathlib import Path

# ── Upload limits (F-04) ──────────────────────────────────────────────────────
MAX_REQUEST_BODY_BYTES: int = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(50 * 1024 * 1024)))
# Admin-role keys get a higher cumulative body-size cap for bulk ingest.
# Falls back to the user cap when unset, so behaviour is unchanged unless
# overridden.
MAX_REQUEST_BODY_BYTES_ADMIN: int = int(
    os.environ.get("MAX_REQUEST_BODY_BYTES_ADMIN", str(500 * 1024 * 1024))
)
MAX_FILE_BYTES: int = int(os.environ.get("MAX_FILE_BYTES", str(15 * 1024 * 1024)))
MAX_FILES_PER_REQUEST: int = int(os.environ.get("MAX_FILES_PER_REQUEST", "20"))

# ── Detection model allowlist (F-02) ─────────────────────────────────────────
# Maps logical ID -> filename within MODELS_DIR.
# Only IDs listed here may be selected via the API.
# Default to a directory writable by the app user inside the container image.
# Finding #3 (2026-04-15 walkthrough): the previous default "/models" is owned
# by root; the app user (uid=100) cannot create it, so ultralytics' auto-download
# path crashed in a background thread with PermissionError while HTTP 200 had
# already returned. Override with $MODELS_DIR (e.g. a bind-mounted host volume
# preloaded with the allowlisted weights) for production.
MODELS_DIR: Path = Path(os.environ.get("MODELS_DIR", "/app/models"))

DETECTION_MODEL_ALLOWLIST: dict[str, str] = {
    "yoloe-v8s-seg": "yoloe-v8s-seg.pt",
    "yolo11s": "yolo11s.pt",
}

# Default model ID: prefer YOLO_MODEL_ID env var; fall back to first allowlist entry.
DEFAULT_DETECTION_MODEL_ID: str = os.environ.get("YOLO_MODEL_ID", "yoloe-v8s-seg")
if DEFAULT_DETECTION_MODEL_ID not in DETECTION_MODEL_ALLOWLIST:
    DEFAULT_DETECTION_MODEL_ID = next(iter(DETECTION_MODEL_ALLOWLIST))
