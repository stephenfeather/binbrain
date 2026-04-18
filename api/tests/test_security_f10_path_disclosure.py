"""F-10 (Low): API responses disclose internal filesystem paths — RED tests.

Tests FAIL until:
- /ingest response no longer includes 'path' for each photo entry
- /bins/{bin_id}/add response no longer includes 'path' for each photo entry
- GET /bins/{bin_id} photos list no longer includes 'path'
"""


def test_ingest_response_does_not_include_path(client, valid_jpeg_bytes):
    """POST /ingest photo entries must not contain 'path' (filesystem leak)."""
    resp = client.post(
        "/ingest",
        data={"bin_id": "F10INGEST01"},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    for entry in resp.json()["photos"]:
        assert "path" not in entry, f"Filesystem path disclosed in /ingest response: {entry}"
        assert "photo_id" in entry, "photo_id must still be present"


def test_ingest_multiple_does_not_include_path(client, valid_jpeg_bytes):
    """POST /ingest with multiple files must omit path from all entries."""
    files = [("photos", (f"p{i}.jpg", valid_jpeg_bytes, "image/jpeg")) for i in range(3)]
    resp = client.post("/ingest", data={"bin_id": "F10INGEST02"}, files=files)
    assert resp.status_code == 200
    for entry in resp.json()["photos"]:
        assert (
            "path" not in entry
        ), f"Filesystem path disclosed in multi-file /ingest response: {entry}"


def test_get_bin_photos_do_not_include_path(client, valid_jpeg_bytes):
    """GET /bins/{bin_id} photo entries must not contain 'path'."""
    client.post(
        "/ingest",
        data={"bin_id": "F10GETBIN01"},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    resp = client.get("/bins/F10GETBIN01")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    for photo in resp.json()["photos"]:
        assert "path" not in photo, f"Filesystem path disclosed in GET /bins response: {photo}"
        assert "photo_id" in photo, "photo_id must still be present"
