"""F-02 (Critical): Arbitrary model-path loading — RED tests.

Tests FAIL until:
- DETECTION_MODEL_ALLOWLIST is added to app/config.py
- /settings/detection-model only accepts allowlisted IDs
- set_detection_model() validates against the allowlist
"""

import pytest

# ── Unit tests: allowlist constants (import from app.config — no DB needed) ───


def test_detection_model_allowlist_exists():
    from app.config import DETECTION_MODEL_ALLOWLIST

    assert isinstance(DETECTION_MODEL_ALLOWLIST, dict)
    assert len(DETECTION_MODEL_ALLOWLIST) > 0


def test_detection_model_allowlist_values_are_filenames():
    from app.config import DETECTION_MODEL_ALLOWLIST

    for logical_id, filename in DETECTION_MODEL_ALLOWLIST.items():
        assert "/" not in filename, f"allowlist filename {filename!r} must not be a path"
        assert filename.endswith(".pt"), f"expected .pt extension, got {filename!r}"


def test_models_dir_exists_in_config():
    from pathlib import Path

    from app.config import MODELS_DIR

    assert isinstance(MODELS_DIR, Path)


def test_max_files_per_request_default_is_20():
    from app.config import MAX_FILES_PER_REQUEST

    assert MAX_FILES_PER_REQUEST == 20


def test_max_file_bytes_default_is_15mib():
    from app.config import MAX_FILE_BYTES

    assert MAX_FILE_BYTES == 15 * 1024 * 1024


def test_max_request_body_bytes_default_is_50mib():
    from app.config import MAX_REQUEST_BODY_BYTES

    assert MAX_REQUEST_BODY_BYTES == 50 * 1024 * 1024


# ── Unit tests: set_detection_model allowlist enforcement (needs app_module) ──
# app_module sets DATABASE_URL + stubs fastembed so app.deps can be imported.


def test_set_detection_model_rejects_arbitrary_path(app_module):
    from app.deps import set_detection_model

    with pytest.raises(ValueError):
        set_detection_model("/tmp/evil.pt")


def test_set_detection_model_rejects_photo_path(app_module):
    from app.deps import set_detection_model

    with pytest.raises(ValueError):
        set_detection_model("/data/photos/uploaded_payload.pt")


def test_set_detection_model_rejects_unknown_id(app_module):
    from app.deps import set_detection_model

    with pytest.raises(ValueError):
        set_detection_model("my_custom_model")


def test_set_detection_model_accepts_allowlisted_id(app_module):
    from app.config import DETECTION_MODEL_ALLOWLIST
    from app.deps import get_detection_model_id, set_detection_model

    first_id = next(iter(DETECTION_MODEL_ALLOWLIST))
    set_detection_model(first_id)
    assert get_detection_model_id() == first_id


def test_get_detection_model_returns_path_under_models_dir(app_module):
    from app.config import MODELS_DIR
    from app.deps import get_detection_model

    path = get_detection_model()
    assert path.startswith(
        str(MODELS_DIR.resolve())
    ), f"get_detection_model() returned {path!r}, expected path under {MODELS_DIR!r}"


# ── Integration tests: API endpoint enforces allowlist ────────────────────────


def test_api_rejects_arbitrary_model_path(client):
    resp = client.post("/settings/detection-model", json={"detection_model": "/tmp/evil.pt"})
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"


def test_api_rejects_unknown_model_id(client):
    resp = client.post("/settings/detection-model", json={"detection_model": "not_in_allowlist"})
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"


def test_api_accepts_allowlisted_model_id(client):
    from app.config import DETECTION_MODEL_ALLOWLIST

    first_id = next(iter(DETECTION_MODEL_ALLOWLIST))
    resp = client.post("/settings/detection-model", json={"detection_model": first_id})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json()["detection_model"] == first_id
