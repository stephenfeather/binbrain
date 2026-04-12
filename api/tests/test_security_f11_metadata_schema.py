"""F-11 (Low): device_metadata tightened schema validation — tests.

Constraints enforced:
- 4 KiB size cap
- Top-level allowlist: {"device_processing"}
- Inner allowlist for device_processing keys
- Max nesting depth 4
- Per-value string max 1 KiB
- HMAC-SHA256 hashing of sensitive fields (device_id, imei, mac, serial)
  with server-side pepper from env METADATA_HASH_PEPPER
"""
import hashlib
import hmac
import json
import os


SAMPLE_METADATA = {
    "device_processing": {
        "version": "1",
        "pipeline_ms": 500,
        "quality_scores": {"blur_variance": 0.8},
    }
}


# ── Unit tests: validation logic (no DB needed) ───────────────────────────────

def test_validate_device_metadata_module_exists():
    from app.services.metadata_schema import validate_device_metadata
    assert callable(validate_device_metadata)


def test_validate_device_metadata_accepts_allowed_key():
    from app.services.metadata_schema import validate_device_metadata
    validate_device_metadata(json.dumps(SAMPLE_METADATA))


def test_validate_device_metadata_rejects_unknown_top_level_key():
    from app.services.metadata_schema import validate_device_metadata
    import pytest
    with pytest.raises(ValueError, match="disallowed"):
        validate_device_metadata(json.dumps({"unknown_key": "value"}))


def test_validate_device_metadata_rejects_unknown_inner_key():
    from app.services.metadata_schema import validate_device_metadata
    import pytest
    bad = {"device_processing": {"version": "1", "injected": "extra"}}
    with pytest.raises(ValueError, match="disallowed"):
        validate_device_metadata(json.dumps(bad))


def test_validate_device_metadata_rejects_oversized():
    """Payload exceeding 4 KiB is rejected with 'size' error."""
    from app.services.metadata_schema import validate_device_metadata, METADATA_MAX_BYTES
    import pytest
    # Many small ocr entries — each text < 1 KiB but total > 4 KiB.
    many_ocr = [{"text": f"item-{i:05d}", "confidence": 0.9} for i in range(200)]
    big = json.dumps({"device_processing": {"ocr": many_ocr}})
    assert len(big.encode()) > METADATA_MAX_BYTES, "Test payload must exceed size cap"
    with pytest.raises(ValueError, match="size"):
        validate_device_metadata(big)


def test_validate_device_metadata_rejects_deep_nesting():
    """Nesting depth > 4 is rejected."""
    from app.services.metadata_schema import validate_device_metadata
    import pytest
    # root(0) → device_processing(1) → quality_scores(2) → level3(3) → level4(4) → level5(5)
    deep = {"device_processing": {"quality_scores": {"a": {"b": {"c": "too deep"}}}}}
    with pytest.raises(ValueError, match="depth"):
        validate_device_metadata(json.dumps(deep))


def test_validate_device_metadata_rejects_oversized_string_value():
    """A single string value > 1 KiB is rejected."""
    from app.services.metadata_schema import validate_device_metadata, STRING_MAX_BYTES
    import pytest
    bad = {"device_processing": {"version": "x" * (STRING_MAX_BYTES + 1)}}
    with pytest.raises(ValueError, match="string"):
        validate_device_metadata(json.dumps(bad))


def test_validate_device_metadata_accepts_empty_device_processing():
    from app.services.metadata_schema import validate_device_metadata
    validate_device_metadata(json.dumps({"device_processing": {}}))


def test_metadata_max_bytes_is_4kib():
    from app.services.metadata_schema import METADATA_MAX_BYTES
    assert METADATA_MAX_BYTES == 4 * 1024


def test_metadata_string_max_bytes_is_1kib():
    from app.services.metadata_schema import STRING_MAX_BYTES
    assert STRING_MAX_BYTES == 1 * 1024


def test_sensitive_field_device_id_is_hashed():
    """device_id values are HMAC-SHA256 hashed, not stored raw."""
    from app.services.metadata_schema import validate_device_metadata
    raw_value = "IMEI-123456789012345"
    metadata = {"device_processing": {"device_id": raw_value}}
    result = validate_device_metadata(json.dumps(metadata))
    expected = hmac.new(
        os.environ.get("METADATA_HASH_PEPPER", "").encode("utf-8"),
        raw_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert result["device_processing"]["device_id"] == expected, (
        f"device_id should be HMAC-SHA256 hash, got {result['device_processing']['device_id']!r}"
    )
    assert result["device_processing"]["device_id"] != raw_value


def test_sensitive_field_imei_is_hashed():
    from app.services.metadata_schema import validate_device_metadata
    raw = "990000862471854"
    result = validate_device_metadata(json.dumps({"device_processing": {"device_imei": raw}}))
    expected = hmac.new(
        os.environ.get("METADATA_HASH_PEPPER", "").encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert result["device_processing"]["device_imei"] == expected


def test_sensitive_field_mac_is_hashed():
    from app.services.metadata_schema import validate_device_metadata
    raw = "AA:BB:CC:DD:EE:FF"
    result = validate_device_metadata(json.dumps({"device_processing": {"wifi_mac": raw}}))
    expected = hmac.new(
        os.environ.get("METADATA_HASH_PEPPER", "").encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert result["device_processing"]["wifi_mac"] == expected


def test_non_string_sensitive_field_unchanged():
    """Non-string values for sensitive-named fields are left as-is."""
    from app.services.metadata_schema import validate_device_metadata
    metadata = {"device_processing": {"device_id": 12345}}
    result = validate_device_metadata(json.dumps(metadata))
    assert result["device_processing"]["device_id"] == 12345


def test_allowed_fields_pass_through_unmodified():
    """Non-sensitive fields are not altered."""
    from app.services.metadata_schema import validate_device_metadata
    metadata = {"device_processing": {"version": "1", "pipeline_ms": 999}}
    result = validate_device_metadata(json.dumps(metadata))
    assert result["device_processing"]["version"] == "1"
    assert result["device_processing"]["pipeline_ms"] == 999


# ── Integration tests: /ingest endpoint enforces schema ──────────────────────

def test_ingest_rejects_unknown_top_level_metadata_key(client, valid_jpeg_bytes):
    resp = client.post(
        "/ingest",
        data={"bin_id": "F11SCHEMA01", "device_metadata": json.dumps({"unknown": "x"})},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"


def test_ingest_rejects_unknown_nested_metadata_key(client, valid_jpeg_bytes):
    bad = {"device_processing": {"version": "1", "evil_key": "x"}}
    resp = client.post(
        "/ingest",
        data={"bin_id": "F11SCHEMA05", "device_metadata": json.dumps(bad)},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 400, f"Expected 400 for unknown nested key, got {resp.status_code}: {resp.text}"


def test_ingest_rejects_oversized_metadata(client, valid_jpeg_bytes):
    many_ocr = [{"text": f"item-{i:05d}", "confidence": 0.9} for i in range(200)]
    huge = json.dumps({"device_processing": {"ocr": many_ocr}})
    resp = client.post(
        "/ingest",
        data={"bin_id": "F11SCHEMA02", "device_metadata": huge},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 400, f"Expected 400 for oversized metadata, got {resp.status_code}: {resp.text}"


def test_ingest_accepts_valid_metadata(client, valid_jpeg_bytes):
    resp = client.post(
        "/ingest",
        data={"bin_id": "F11SCHEMA03", "device_metadata": json.dumps(SAMPLE_METADATA)},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_ingest_device_id_stored_hashed(client, db, valid_jpeg_bytes):
    """device_id is SHA-256 hashed before persistence."""
    from sqlalchemy import text
    raw_id = "IMEI:device-test-12345"
    metadata = {"device_processing": {"device_id": raw_id}}
    resp = client.post(
        "/ingest",
        data={"bin_id": "F11SCHEMA04", "device_metadata": json.dumps(metadata)},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    photo_id = resp.json()["photos"][0]["photo_id"]
    row = db.execute(
        text("SELECT device_metadata FROM photos WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert row is not None
    stored = row["device_processing"]["device_id"]
    expected_hash = hmac.new(
        os.environ.get("METADATA_HASH_PEPPER", "").encode("utf-8"),
        raw_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert stored == expected_hash, (
        f"Expected HMAC-SHA256 hash of device_id, got {stored!r}"
    )
    assert stored != raw_id, "Raw device_id must not be stored"


def test_pepper_changes_hash_output(monkeypatch):
    """Different pepper values produce different hashes; neither equals plain SHA-256."""
    from app.services.metadata_schema import _hash_value
    raw = "test-device-id-42"

    monkeypatch.setenv("METADATA_HASH_PEPPER", "pepper-a")
    hash_a = _hash_value(raw)

    monkeypatch.setenv("METADATA_HASH_PEPPER", "pepper-b")
    hash_b = _hash_value(raw)

    plain_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    assert hash_a != hash_b, "Different peppers must produce different hashes"
    assert hash_a != plain_sha256, "Peppered hash must differ from plain SHA-256"
    assert hash_b != plain_sha256, "Peppered hash must differ from plain SHA-256"


def test_no_pepper_is_deterministic(monkeypatch):
    """Without a pepper, hashing the same value twice yields the same non-empty result."""
    from app.services.metadata_schema import _hash_value
    monkeypatch.delenv("METADATA_HASH_PEPPER", raising=False)
    raw = "test-device-id-42"
    assert _hash_value(raw) == _hash_value(raw)
    assert _hash_value(raw) != ""
