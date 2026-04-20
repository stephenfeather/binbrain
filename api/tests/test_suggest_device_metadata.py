"""ApiDev_S-DM-01: surface device-processing OCR + barcodes in /suggest response.

Tests cover:
- Pure extraction logic (extract_ocr_and_barcodes)
- /suggest integration with device_metadata present
- /suggest integration with device_metadata absent (NULL)
- FF-04: PII fields never leak into the response
- Backward compat: existing suggest fields unchanged
"""

import json

from app.services.device_metadata import extract_ocr_and_barcodes

# ── Pure unit tests for extract_ocr_and_barcodes ────────────────────────────

_FULL_METADATA = {
    "device_processing": {
        "version": "1",
        "pipeline_ms": 320,
        "ios_version": "Version 26.4.1 (Build 23E254)",
        "device_model": "iPhone16,2",
        "ocr": [
            {"text": "X00130ND1Z", "confidence": 0.5},
            {"text": "Elegoo 120pcs Multicolored Ribbon Cables Kit", "confidence": 1.0},
            {"text": "New", "confidence": 1.0},
            {"text": "too quiet", "confidence": 0.1},
        ],
        "barcodes": [
            {"payload": "X00130ND1Z", "symbology": "VNBarcodeSymbologyCode128"},
        ],
        "quality_scores": {"blur_variance": 0, "exposure_mean": 0},
        "classifications": [{"label": "document", "confidence": 0.12}],
        "crop_applied": {"crop_rect": [0, 0, 100, 100], "original_size": [200, 400]},
    }
}


def test_extract_ocr_surfaced_above_threshold():
    ocr, barcodes = extract_ocr_and_barcodes(_FULL_METADATA)
    texts = [e["text"] for e in ocr]
    assert "Elegoo 120pcs Multicolored Ribbon Cables Kit" in texts
    assert "New" in texts


def test_extract_ocr_filters_below_threshold():
    ocr, _ = extract_ocr_and_barcodes(_FULL_METADATA)
    texts = [e["text"] for e in ocr]
    assert "too quiet" not in texts


def test_extract_ocr_deduplicates_barcode_payload():
    # "X00130ND1Z" is in barcodes[] — must not appear in ocr_results too.
    ocr, _ = extract_ocr_and_barcodes(_FULL_METADATA)
    texts = [e["text"] for e in ocr]
    assert "X00130ND1Z" not in texts


def test_extract_barcodes_present():
    _, barcodes = extract_ocr_and_barcodes(_FULL_METADATA)
    assert len(barcodes) == 1
    assert barcodes[0]["payload"] == "X00130ND1Z"
    assert barcodes[0]["symbology"] == "VNBarcodeSymbologyCode128"


def test_extract_none_metadata_returns_empty_lists():
    ocr, barcodes = extract_ocr_and_barcodes(None)
    assert ocr == []
    assert barcodes == []


def test_extract_missing_device_processing_returns_empty():
    ocr, barcodes = extract_ocr_and_barcodes({"something_else": {}})
    assert ocr == []
    assert barcodes == []


def test_extract_ocr_cap_at_ten():
    many_ocr = [{"text": f"item {i}", "confidence": 1.0} for i in range(20)]
    meta = {"device_processing": {"ocr": many_ocr}}
    ocr, _ = extract_ocr_and_barcodes(meta)
    assert len(ocr) == 10


def test_extract_pii_fields_not_in_output():
    meta = {
        "device_processing": {
            "ocr": [{"text": "Hello", "confidence": 0.9}],
            "barcodes": [],
            "device_id": "abc123",
            "device_serial": "SN999",
            "wifi_mac": "AA:BB:CC:DD:EE:FF",
            "bluetooth_mac": "11:22:33:44:55:66",
            "device_imei": "123456789012345",
        }
    }
    ocr, barcodes = extract_ocr_and_barcodes(meta)
    # Only text/confidence keys in OCR entries
    for entry in ocr:
        assert set(entry.keys()) == {"text", "confidence"}
    # Only payload/symbology keys in barcode entries
    for b in barcodes:
        assert set(b.keys()) == {"payload", "symbology"}


def test_extract_confidence_coerced_to_float():
    meta = {"device_processing": {"ocr": [{"text": "Hi", "confidence": 1}]}}
    ocr, _ = extract_ocr_and_barcodes(meta)
    assert len(ocr) == 1
    assert isinstance(ocr[0]["confidence"], float)


# ── Integration tests via /suggest endpoint ───────────────────────────────────


def _seed_photo_with_metadata(client, valid_jpeg_bytes, bin_id: str, metadata: dict) -> int:
    r = client.post(
        "/ingest",
        data={"bin_id": bin_id, "device_metadata": json.dumps(metadata)},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200, f"ingest failed: {r.text}"
    return r.json()["photos"][0]["photo_id"]


def _seed_photo_no_metadata(client, valid_jpeg_bytes, bin_id: str) -> int:
    r = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200
    return r.json()["photos"][0]["photo_id"]


def _stub_describe(hits=None, elapsed=10):
    def fn(*a, **kw):
        return (hits or [], elapsed)
    return fn


def test_suggest_ocr_results_surfaced(client, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo_with_metadata(
        client, valid_jpeg_bytes, "DM01-BIN-001", _FULL_METADATA
    )
    monkeypatch.setattr("app.routes.photos.describe_photo", _stub_describe())

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    body = r.json()

    assert "ocr_results" in body
    texts = [e["text"] for e in body["ocr_results"]]
    assert "Elegoo 120pcs Multicolored Ribbon Cables Kit" in texts
    assert "too quiet" not in texts  # below confidence threshold
    assert "X00130ND1Z" not in texts  # deduped against barcode


def test_suggest_barcodes_surfaced(client, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo_with_metadata(
        client, valid_jpeg_bytes, "DM01-BIN-002", _FULL_METADATA
    )
    monkeypatch.setattr("app.routes.photos.describe_photo", _stub_describe())

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    body = r.json()

    assert "barcodes" in body
    assert len(body["barcodes"]) == 1
    assert body["barcodes"][0]["payload"] == "X00130ND1Z"
    assert body["barcodes"][0]["symbology"] == "VNBarcodeSymbologyCode128"


def test_suggest_empty_arrays_when_no_device_metadata(client, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo_no_metadata(client, valid_jpeg_bytes, "DM01-BIN-003")
    monkeypatch.setattr("app.routes.photos.describe_photo", _stub_describe())

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    body = r.json()

    assert body["ocr_results"] == []
    assert body["barcodes"] == []


def test_suggest_pii_fields_not_in_response(client, monkeypatch, valid_jpeg_bytes):
    meta = {
        "device_processing": {
            "version": "1",
            "ocr": [{"text": "Hello", "confidence": 0.9}],
            "barcodes": [],
        }
    }
    photo_id = _seed_photo_with_metadata(client, valid_jpeg_bytes, "DM01-BIN-004", meta)
    monkeypatch.setattr("app.routes.photos.describe_photo", _stub_describe())

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    response_text = r.text

    for pii_key in ("device_id", "device_model", "ios_version", "crop_applied",
                    "quality_scores", "classifications", "device_serial",
                    "wifi_mac", "bluetooth_mac", "device_imei"):
        assert pii_key not in response_text, f"PII key '{pii_key}' leaked into /suggest response"


def test_suggest_existing_fields_unchanged(client, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo_with_metadata(
        client, valid_jpeg_bytes, "DM01-BIN-005", _FULL_METADATA
    )
    hits = [{"name": "Widget", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.2, 0.3, 0.4]}]
    monkeypatch.setattr("app.routes.photos.describe_photo", _stub_describe(hits))

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    body = r.json()

    # All pre-existing fields still present
    for key in ("version", "photo_id", "model", "vision_elapsed_ms", "cached",
                "prompt_version", "suggestions"):
        assert key in body, f"Pre-existing field '{key}' missing from response"

    assert body["version"] == "1"
    assert body["photo_id"] == photo_id
    assert len(body["suggestions"]) == 1
    assert body["suggestions"][0]["name"] == "Widget"
