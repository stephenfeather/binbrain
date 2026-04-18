"""Tests for device_metadata support on /ingest endpoint."""

import json

SAMPLE_METADATA = {
    "device_processing": {
        "version": "1",
        "pipeline_ms": 623,
        "ios_version": "26.0",
        "device_model": "iPhone16,1",
        "quality_scores": {
            "blur_variance": 842.3,
            "exposure_mean": 0.47,
            "saliency_coverage": 0.72,
        },
        "ocr": [
            {"text": "M3x8 DIN 912", "confidence": 0.94},
        ],
        "barcodes": [
            {"payload": "4005176834561", "symbology": "EAN-13"},
        ],
        "classifications": [
            {"label": "screw", "confidence": 0.82},
        ],
        "crop_applied": {
            "original_size": [4032, 3024],
            "crop_rect": [420, 310, 3200, 2400],
        },
    }
}


def test_ingest_without_metadata_still_works(client, valid_jpeg_bytes):
    """Existing behavior: ingest without device_metadata succeeds."""
    resp = client.post(
        "/ingest",
        data={"bin_id": "BIN-META-0001"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["bin_id"] == "BIN-META-0001"
    assert len(body["photos"]) == 1


def test_ingest_with_valid_metadata(client, db, valid_jpeg_bytes):
    """Ingest with valid device_metadata JSON stores it in the DB."""
    from sqlalchemy import text

    resp = client.post(
        "/ingest",
        data={
            "bin_id": "BIN-META-0002",
            "device_metadata": json.dumps(SAMPLE_METADATA),
        },
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert resp.status_code == 200
    body = resp.json()
    photo_id = body["photos"][0]["photo_id"]

    row = db.execute(
        text("SELECT device_metadata FROM photos WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert row is not None
    assert row["device_processing"]["version"] == "1"
    assert row["device_processing"]["ocr"][0]["text"] == "M3x8 DIN 912"


def test_ingest_with_malformed_metadata_returns_400(client):
    """Ingest with invalid JSON in device_metadata returns 400."""
    resp = client.post(
        "/ingest",
        data={
            "bin_id": "BIN-META-0003",
            "device_metadata": "not-valid-json{{{",
        },
        files={"photos": ("photo.jpg", b"fake-jpeg", "image/jpeg")},
    )
    assert resp.status_code == 400
    assert "device_metadata" in resp.json()["error"]["message"].lower()


def test_ingest_metadata_null_treated_as_absent(client, db, valid_jpeg_bytes):
    """Ingest with empty string device_metadata stores NULL."""
    from sqlalchemy import text

    resp = client.post(
        "/ingest",
        data={
            "bin_id": "BIN-META-0004",
            "device_metadata": "",
        },
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert resp.status_code == 200
    photo_id = resp.json()["photos"][0]["photo_id"]

    row = db.execute(
        text("SELECT device_metadata FROM photos WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert row is None


def test_get_bin_persists_device_metadata_without_leaking(client, db, valid_jpeg_bytes):
    """Ingested device_metadata is persisted in the DB but NOT returned by
    the user-plane GET /bins/{bin_id} endpoint (FF-04).

    Supersedes the prior contract which leaked device_metadata in responses.
    See tests/test_security_ff04_device_metadata_disclosure.py for the
    disclosure-focused assertions.
    """
    from sqlalchemy import text

    bin_id = "BIN-META-0005"
    ingest = client.post(
        "/ingest",
        data={
            "bin_id": bin_id,
            "device_metadata": json.dumps(SAMPLE_METADATA),
        },
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert ingest.status_code == 200
    photo_id = ingest.json()["photos"][0]["photo_id"]

    # DB layer still stores the metadata (admin endpoints remain able to read it).
    row = db.execute(
        text("SELECT device_metadata FROM photos WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert row is not None
    assert row["device_processing"]["version"] == "1"

    # User-plane response must NOT include device_metadata.
    resp = client.get(f"/bins/{bin_id}")
    assert resp.status_code == 200
    photos = resp.json()["photos"]
    assert len(photos) == 1
    assert "device_metadata" not in photos[0]
