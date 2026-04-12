"""F-11 (Low): device_metadata accepted as arbitrary JSON — RED tests.

Tests FAIL until:
- /ingest rejects device_metadata with unknown top-level keys with 400
- /ingest rejects device_metadata that exceeds the 16 KiB size cap with 400
- /ingest still accepts well-formed device_metadata with allowed keys
"""
import json


SAMPLE_METADATA = {
    "device_processing": {
        "version": "1",
        "pipeline_ms": 500,
    }
}


# ── Unit tests: validation logic (no DB needed) ───────────────────────────────

def test_validate_device_metadata_module_exists():
    from app.services.metadata_schema import validate_device_metadata
    assert callable(validate_device_metadata)


def test_validate_device_metadata_accepts_allowed_key():
    from app.services.metadata_schema import validate_device_metadata
    # Must not raise for valid metadata
    validate_device_metadata(json.dumps(SAMPLE_METADATA))


def test_validate_device_metadata_rejects_unknown_top_level_key():
    from app.services.metadata_schema import validate_device_metadata
    import pytest
    with pytest.raises(ValueError, match="disallowed"):
        validate_device_metadata(json.dumps({"unknown_key": "value"}))


def test_validate_device_metadata_rejects_mixed_keys():
    from app.services.metadata_schema import validate_device_metadata
    import pytest
    bad = {"device_processing": {"version": "1"}, "injected": "extra"}
    with pytest.raises(ValueError, match="disallowed"):
        validate_device_metadata(json.dumps(bad))


def test_validate_device_metadata_rejects_oversized():
    from app.services.metadata_schema import validate_device_metadata, METADATA_MAX_BYTES
    import pytest
    # Build a string that exceeds the limit
    big = json.dumps({"device_processing": {"data": "x" * (METADATA_MAX_BYTES + 1)}})
    with pytest.raises(ValueError, match="size"):
        validate_device_metadata(big)


def test_validate_device_metadata_accepts_empty_device_processing():
    from app.services.metadata_schema import validate_device_metadata
    validate_device_metadata(json.dumps({"device_processing": {}}))


def test_metadata_max_bytes_is_16kib():
    from app.services.metadata_schema import METADATA_MAX_BYTES
    assert METADATA_MAX_BYTES == 16 * 1024


# ── Integration tests: /ingest endpoint enforces schema ──────────────────────

def test_ingest_rejects_unknown_top_level_metadata_key(client, valid_jpeg_bytes):
    """POST /ingest with unknown top-level metadata key must return 400."""
    resp = client.post(
        "/ingest",
        data={
            "bin_id": "F11SCHEMA01",
            "device_metadata": json.dumps({"unknown_field": "value"}),
        },
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 400, (
        f"Expected 400 for unknown metadata key, got {resp.status_code}: {resp.text}"
    )


def test_ingest_rejects_oversized_metadata(client, valid_jpeg_bytes):
    """POST /ingest with device_metadata exceeding 16 KiB must return 400."""
    huge = json.dumps({"device_processing": {"data": "x" * 20000}})
    resp = client.post(
        "/ingest",
        data={"bin_id": "F11SCHEMA02", "device_metadata": huge},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 400, (
        f"Expected 400 for oversized metadata, got {resp.status_code}: {resp.text}"
    )


def test_ingest_accepts_valid_metadata(client, valid_jpeg_bytes):
    """POST /ingest with well-formed device_metadata must succeed."""
    resp = client.post(
        "/ingest",
        data={
            "bin_id": "F11SCHEMA03",
            "device_metadata": json.dumps(SAMPLE_METADATA),
        },
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 200, (
        f"Expected 200 for valid metadata, got {resp.status_code}: {resp.text}"
    )


def test_ingest_accepts_missing_metadata(client, valid_jpeg_bytes):
    """POST /ingest without device_metadata must still succeed."""
    resp = client.post(
        "/ingest",
        data={"bin_id": "F11SCHEMA04"},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 200, (
        f"Expected 200 for missing metadata, got {resp.status_code}: {resp.text}"
    )
