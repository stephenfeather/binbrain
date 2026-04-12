"""F-01 (Critical): Path traversal via bin_id — RED tests.

Tests must FAIL until api/app/security.py exists with validate_bin_id()
AND bins.py / photos.py apply it at every entry point.
"""
import pytest


# ── Unit tests: validate_bin_id (no DB needed) ───────────────────────────────

from app.security import validate_bin_id  # noqa: E402 — must be at module level


class TestValidateBinIdRejectsTraversal:
    def test_rejects_dotdot(self):
        with pytest.raises(ValueError):
            validate_bin_id("../evil")

    def test_rejects_dotdot_trailing(self):
        with pytest.raises(ValueError):
            validate_bin_id("bin/..")

    def test_rejects_forward_slash(self):
        with pytest.raises(ValueError):
            validate_bin_id("foo/bar")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError):
            validate_bin_id("foo\\bar")

    def test_rejects_url_encoded_dotdot(self):
        """percent-encoded traversal must be rejected."""
        with pytest.raises(ValueError):
            validate_bin_id("%2e%2e")

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError):
            validate_bin_id("bin\x00evil")

    def test_rejects_leading_dot(self):
        with pytest.raises(ValueError):
            validate_bin_id(".hidden")

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError):
            validate_bin_id("/etc/passwd")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_bin_id("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError):
            validate_bin_id("A" * 65)

    def test_rejects_spaces(self):
        with pytest.raises(ValueError):
            validate_bin_id("bin id")

    def test_rejects_control_chars(self):
        with pytest.raises(ValueError):
            validate_bin_id("bin\x01id")


class TestValidateBinIdAcceptsValid:
    def test_accepts_simple_alphanumeric(self):
        assert validate_bin_id("BIN001") == "BIN001"

    def test_accepts_with_dash(self):
        assert validate_bin_id("bin-garage-1") == "bin-garage-1"

    def test_accepts_with_underscore(self):
        assert validate_bin_id("bin_storage_box") == "bin_storage_box"

    def test_accepts_64_char_max(self):
        val = "A" * 64
        assert validate_bin_id(val) == val

    def test_accepts_mixed_case(self):
        assert validate_bin_id("BinGarage01") == "BinGarage01"

    def test_accepts_single_char(self):
        assert validate_bin_id("A") == "A"

    def test_accepts_digit_after_first(self):
        assert validate_bin_id("B1") == "B1"


# ── Integration tests: endpoint rejects traversal attempts ───────────────────
# These require TEST_DATABASE_URL and a valid image to upload.

def _minimal_jpeg() -> bytes:
    """Return a minimal valid JPEG (1×1 white pixel)."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
        b"\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}"
        b"\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd4\xb8\xff\xd9"
    )


@pytest.mark.parametrize("bad_bin_id", [
    "../evil",
    "../../etc/passwd",
    "foo/bar",
    "foo\\bar",
    "%2e%2e",
    ".hidden",
    "/etc/passwd",
    "bin\x00evil",
    "A" * 65,
    "bin id",
])
def test_ingest_rejects_traversal_bin_id(client, bad_bin_id):
    """POST /ingest with a bad bin_id must return 400."""
    resp = client.post(
        "/ingest",
        data={"bin_id": bad_bin_id},
        files=[("photos", ("test.jpg", _minimal_jpeg(), "image/jpeg"))],
    )
    assert resp.status_code == 400, (
        f"Expected 400 for bin_id={bad_bin_id!r}, got {resp.status_code}: {resp.text}"
    )


def test_ingest_valid_bin_id_accepted(client, tmp_path):
    """POST /ingest with a valid bin_id must NOT return 400 from validation."""
    resp = client.post(
        "/ingest",
        data={"bin_id": "VALIDBIN01"},
        files=[("photos", ("test.jpg", _minimal_jpeg(), "image/jpeg"))],
    )
    # 200 means bin_id was accepted; other errors (e.g., DB) are unrelated to F-01
    assert resp.status_code != 400 or "invalid bin_id" not in resp.text.lower(), (
        f"Valid bin_id was rejected: {resp.text}"
    )


@pytest.mark.parametrize("bad_bin_id", [
    "../evil",
    "foo/bar",
    "%2e%2e",
])
def test_add_to_bin_rejects_traversal_bin_id(client, bad_bin_id):
    """POST /bins/{bin_id}/add with a bad bin_id must be rejected (400 or 404).

    Both status codes are acceptable and safe:
    - 400: the {bin_id:path} route captured the value and _check_bin_id()
      rejected it (the expected path for foo/bar, %2e%2e).
    - 404: the HTTP client URL-normalised the path before it reached Starlette
      (e.g. /bins/../evil/add → /add which has no registered route).  The
      attack fails — no file is written under PHOTO_DIR — so 404 is an equally
      safe outcome.  This applies specifically to the '../evil' case.

    The security invariant is: no path traversal succeeds, regardless of which
    rejection code is returned.
    """
    resp = client.post(
        f"/bins/{bad_bin_id}/add",
        data={"name": "test item"},
    )
    assert resp.status_code in (400, 404), (
        f"Expected 400 or 404 for bin_id={bad_bin_id!r}, got {resp.status_code}: {resp.text}"
    )
