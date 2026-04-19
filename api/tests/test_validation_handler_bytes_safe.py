"""Regression: validation_exception_handler must not crash on non-JSON-serializable
ctx values (e.g. raw `bytes` from a misrouted multipart body to a JSON endpoint).

Device-observed bug: POST /associate with multipart/form-data returned
500 internal_error instead of 400 bad_request because JSONResponse default
encoder cannot serialize `bytes`, which caused the handler itself to raise.
"""

from fastapi.testclient import TestClient


def _multipart_bytes() -> tuple[bytes, str]:
    boundary = "Boundary-4A8764D8-DEAB-4C8C-9DC6-3D697FDE4BFC"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="item_id"\r\n\r\n'
        "15\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="bin_id"\r\n\r\n'
        "BIN-0003\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="confidence"\r\n\r\n'
        "0.95\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    return body, boundary


def test_associate_with_multipart_returns_400_not_500(client: TestClient, test_api_key: str):
    """iOS shipped multipart/form-data to /associate (a JSON endpoint). The
    handler must return a structured 400 validation error, NOT a 500 that
    masks the real cause."""
    body, boundary = _multipart_bytes()
    resp = client.post(
        "/associate",
        content=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-API-Key": test_api_key,
        },
    )
    assert resp.status_code == 400, (
        f"Expected 400 bad_request (validation error), got {resp.status_code}. "
        f"Body: {resp.text}"
    )
    payload = resp.json()
    assert payload["error"]["code"] == "bad_request"
    # Details should be present and JSON-serializable (no raw bytes leaked).
    assert "details" in payload["error"]
    # Sanity: round-trip through json to confirm no bytes escaped.
    import json as _json

    _json.dumps(payload)


def test_validation_handler_keeps_error_payload_serializable(client: TestClient, test_api_key: str):
    """Direct guard: any RequestValidationError whose `input` contains bytes
    must still produce a JSON-serializable response body."""
    # Send a bytes body to a JSON endpoint with the wrong content-type.
    resp = client.post(
        "/associate",
        content=b"\x00\x01\x02\xff not json",
        headers={
            "Content-Type": "application/octet-stream",
            "X-API-Key": test_api_key,
        },
    )
    # Must be 400 or 415, never 500. Key invariant: no handler crash.
    assert resp.status_code != 500, f"Handler crashed on bytes input: {resp.text}"
    payload = resp.json()
    assert payload["error"]["code"] != "internal_error"
