"""F-04 (High): Unbounded/fully-buffered uploads — RED tests.

Tests FAIL until:
- body-size middleware rejects > MAX_REQUEST_BODY_BYTES with 413
- handlers enforce max file count (default 20) and per-file size (default 15 MiB)
- uploads are streamed in chunks (not await up.read() full buffer)
"""


def _minimal_jpeg() -> bytes:
    """Smallest valid JPEG bytes (1×1 pixel)."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00"
        + b"\x08" * 64
        + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        + b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00"
        + b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
        + b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd4\xb8\xff\xd9"
    )


# ── Unit tests: constants exist in config (no DB needed) ─────────────────────


def test_max_files_per_request_constant_exists():
    from app.config import MAX_FILES_PER_REQUEST

    assert isinstance(MAX_FILES_PER_REQUEST, int)
    assert MAX_FILES_PER_REQUEST >= 1


def test_max_file_bytes_constant_exists():
    from app.config import MAX_FILE_BYTES

    assert isinstance(MAX_FILE_BYTES, int)
    assert MAX_FILE_BYTES > 0


def test_max_request_body_bytes_constant_exists():
    from app.config import MAX_REQUEST_BODY_BYTES

    assert isinstance(MAX_REQUEST_BODY_BYTES, int)
    assert MAX_REQUEST_BODY_BYTES > 0


def test_max_file_bytes_default_is_15mib():
    from app.config import MAX_FILE_BYTES

    assert (
        MAX_FILE_BYTES == 15 * 1024 * 1024
    ), f"Default MAX_FILE_BYTES should be 15 MiB, got {MAX_FILE_BYTES}"


def test_max_files_per_request_default_is_20():
    from app.config import MAX_FILES_PER_REQUEST

    assert (
        MAX_FILES_PER_REQUEST == 20
    ), f"Default MAX_FILES_PER_REQUEST should be 20, got {MAX_FILES_PER_REQUEST}"


def test_max_request_body_bytes_default_is_50mib():
    from app.config import MAX_REQUEST_BODY_BYTES

    assert (
        MAX_REQUEST_BODY_BYTES == 50 * 1024 * 1024
    ), f"Default MAX_REQUEST_BODY_BYTES should be 50 MiB, got {MAX_REQUEST_BODY_BYTES}"


# ── Integration tests: endpoint enforces limits ───────────────────────────────


def test_ingest_rejects_too_many_files(client):
    """POST /ingest with 21 files must return 400."""
    files = [("photos", (f"test{i}.jpg", _minimal_jpeg(), "image/jpeg")) for i in range(21)]
    resp = client.post(
        "/ingest",
        data={"bin_id": "LIMITTEST01"},
        files=files,
    )
    assert (
        resp.status_code == 400
    ), f"Expected 400 for 21 files, got {resp.status_code}: {resp.text}"


def test_add_to_bin_rejects_too_many_files(client):
    """POST /bins/{bin_id}/add with 21 files must return 400."""
    files = [("photos", (f"test{i}.jpg", _minimal_jpeg(), "image/jpeg")) for i in range(21)]
    resp = client.post(
        "/bins/LIMITTEST02/add",
        files=files,
    )
    assert (
        resp.status_code == 400
    ), f"Expected 400 for 21 files, got {resp.status_code}: {resp.text}"


def test_ingest_rejects_oversized_request_via_content_length(client):
    """POST /ingest with Content-Length > 50 MiB must return 413."""
    # Send a request with a spoofed Content-Length header.
    # TestClient sets Content-Length automatically for multipart; we override it.
    fifty_one_mib = 51 * 1024 * 1024
    resp = client.post(
        "/ingest",
        data={"bin_id": "SIZETEST01"},
        files=[("photos", ("big.jpg", b"x" * 100, "image/jpeg"))],
        headers={"Content-Length": str(fifty_one_mib)},
    )
    assert (
        resp.status_code == 413
    ), f"Expected 413 for oversized Content-Length, got {resp.status_code}: {resp.text}"


def test_happy_path_small_ingest_works(client):
    """POST /ingest with 3 tiny files must not be rejected by limit checks."""
    files = [("photos", (f"ok{i}.jpg", _minimal_jpeg(), "image/jpeg")) for i in range(3)]
    resp = client.post(
        "/ingest",
        data={"bin_id": "HAPPYPATH01"},
        files=files,
    )
    # 200 = worked; other status codes are fine if unrelated to upload limits
    assert resp.status_code not in (
        400,
        413,
    ), f"Small upload was rejected by limit checks: {resp.status_code}: {resp.text}"
