"""FF-04 (Low): device_metadata must not leak to user-plane responses.

Companion to F-10 path-disclosure tests. `device_metadata` (operational
telemetry, OCR output, classification payloads) is stored per photo on
ingest (F-11 sanitized), but must NOT be returned in default user-plane
bin responses. Admin endpoints would need to project it explicitly.
"""

import json

SAMPLE_METADATA = {
    "device_processing": {
        "version": "1",
        "ocr": [{"text": "secret serial", "confidence": 0.91}],
    }
}


def test_get_bin_photos_do_not_include_device_metadata(client, valid_jpeg_bytes):
    """GET /bins/{bin_id} photo entries must not contain 'device_metadata'."""
    ingest = client.post(
        "/ingest",
        data={
            "bin_id": "FF04GETBIN01",
            "device_metadata": json.dumps(SAMPLE_METADATA),
        },
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert ingest.status_code == 200, f"ingest failed: {ingest.text}"

    resp = client.get("/bins/FF04GETBIN01")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    photos = resp.json()["photos"]
    assert len(photos) == 1, "expected one photo in bin"
    for photo in photos:
        assert (
            "device_metadata" not in photo
        ), f"device_metadata disclosed in GET /bins response: {photo}"
        assert (
            "path" not in photo
        ), f"path disclosed in GET /bins response (F-10 regression): {photo}"
        # Positive control: photo_id must still be present.
        assert "photo_id" in photo, "photo_id must still be present"


def test_get_bin_photos_omit_device_metadata_even_when_null(client, valid_jpeg_bytes):
    """Allowlist must drop device_metadata regardless of whether it's null."""
    ingest = client.post(
        "/ingest",
        data={"bin_id": "FF04GETBIN02"},  # no device_metadata on ingest
        files=[("photos", ("b.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert ingest.status_code == 200

    resp = client.get("/bins/FF04GETBIN02")
    assert resp.status_code == 200
    for photo in resp.json()["photos"]:
        assert (
            "device_metadata" not in photo
        ), f"device_metadata key present (even if null) in response: {photo}"
        assert "photo_id" in photo
