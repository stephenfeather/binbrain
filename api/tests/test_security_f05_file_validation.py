"""F-05 (High): File-type validation by magic bytes — RED tests.

Tests FAIL until:
- api/app/services/image_validation.py exists with validate_image_file()
- ingest and add-to-bin stream to a temp file, validate, then rename or unlink
- non-image uploads return 415 and leave no residue in PHOTO_DIR
"""

import struct
import tempfile
from pathlib import Path

import pytest

# ── Unit tests: validate_image_file (no DB needed) ───────────────────────────
from app.services.image_validation import ALLOWED_FORMATS, validate_image_file


def _write_tmp(data: bytes, suffix: str = ".tmp") -> Path:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        return Path(f.name)


def _minimal_jpeg_bytes() -> bytes:
    """Minimal JPEG magic header + SOI + EOI so PIL can open it."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00"
        + bytes([8] * 64)
        + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        + b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01"
        + b"\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
        + b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd4\xb8\xff\xd9"
    )


def _minimal_png_bytes() -> bytes:
    """Minimal 1×1 white PNG."""
    import zlib

    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return length + tag + data + crc

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\xff\xff"  # filter byte + RGB pixel
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def _exe_bytes() -> bytes:
    """MZ header — Windows PE executable magic bytes."""
    return b"MZ" + b"\x00" * 100


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4 fake content that is not an image"


def _minimal_heif_bytes() -> bytes:
    """Minimal 1×1 HEIF encoded via pillow-heif.

    Skips the test at collection time if pillow-heif can't encode in this
    environment (libheif without an HEIC encoder plugin).
    """
    pytest.importorskip("pillow_heif")
    import io

    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    img = Image.new("RGB", (1, 1), color=(255, 255, 255))
    buf = io.BytesIO()
    try:
        img.save(buf, format="HEIF")
    except (OSError, ValueError) as exc:
        pytest.skip(f"pillow-heif cannot encode HEIF in this environment: {exc}")
    return buf.getvalue()


def _pt_bytes() -> bytes:
    """Fake PyTorch model bytes (PK header = ZIP)."""
    return b"PK\x03\x04" + b"\x00" * 100


class TestValidateImageFileAcceptsImages:
    def test_accepts_jpeg(self, tmp_path):
        p = tmp_path / "img.jpg"
        p.write_bytes(_minimal_jpeg_bytes())
        fmt = validate_image_file(p)
        assert fmt in ALLOWED_FORMATS

    def test_accepts_png(self, tmp_path):
        p = tmp_path / "img.png"
        p.write_bytes(_minimal_png_bytes())
        fmt = validate_image_file(p)
        assert fmt in ALLOWED_FORMATS

    def test_accepts_heif(self, tmp_path):
        p = tmp_path / "img.heic"
        p.write_bytes(_minimal_heif_bytes())
        fmt = validate_image_file(p)
        assert fmt == "HEIF"
        assert "HEIF" in ALLOWED_FORMATS


class TestValidateImageFileRejectsNonImages:
    def test_rejects_exe(self, tmp_path):
        p = tmp_path / "evil.exe"
        p.write_bytes(_exe_bytes())
        with pytest.raises(ValueError):
            validate_image_file(p)

    def test_rejects_pdf(self, tmp_path):
        p = tmp_path / "document.pdf"
        p.write_bytes(_pdf_bytes())
        with pytest.raises(ValueError):
            validate_image_file(p)

    def test_rejects_pt_model_file(self, tmp_path):
        p = tmp_path / "model.pt"
        p.write_bytes(_pt_bytes())
        with pytest.raises(ValueError):
            validate_image_file(p)

    def test_rejects_random_bytes(self, tmp_path):
        p = tmp_path / "garbage.bin"
        p.write_bytes(b"\x00\x01\x02\x03" * 100)
        with pytest.raises(ValueError):
            validate_image_file(p)

    def test_rejects_exe_with_jpg_extension(self, tmp_path):
        """Renamed executable must still be rejected (magic-byte check, not extension)."""
        p = tmp_path / "photo.jpg"
        p.write_bytes(_exe_bytes())
        with pytest.raises(ValueError):
            validate_image_file(p)

    def test_rejects_pt_with_jpg_extension(self, tmp_path):
        """Renamed .pt model file must be rejected even with .jpg extension."""
        p = tmp_path / "photo.jpg"
        p.write_bytes(_pt_bytes())
        with pytest.raises(ValueError):
            validate_image_file(p)


# ── Integration tests: endpoint returns 415 and leaves no residue ─────────────


def test_ingest_rejects_exe_as_jpg_and_leaves_no_residue(client, app_module):
    """Uploading an EXE with .jpg extension must return 415 and leave no files."""
    from app.deps import photo_root

    bin_id = "F05RESIDUE01"
    before = set(photo_root.rglob("*")) if photo_root.exists() else set()

    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files=[("photos", ("evil.jpg", _exe_bytes(), "image/jpeg"))],
    )
    assert (
        resp.status_code == 415
    ), f"Expected 415 for EXE masquerading as JPEG, got {resp.status_code}: {resp.text}"

    after = set(photo_root.rglob("*"))
    new_files = after - before
    assert not new_files, f"Residue left in PHOTO_DIR after rejection: {new_files}"


def test_ingest_rejects_pt_model_as_jpg(client):
    """Uploading a .pt model file disguised as .jpg must return 415."""
    resp = client.post(
        "/ingest",
        data={"bin_id": "F05PTTEST01"},
        files=[("photos", ("model.jpg", _pt_bytes(), "image/jpeg"))],
    )
    assert (
        resp.status_code == 415
    ), f"Expected 415 for .pt disguised as JPEG, got {resp.status_code}: {resp.text}"


def test_ingest_accepts_valid_jpeg(client):
    """Uploading a genuine JPEG must succeed (200)."""
    resp = client.post(
        "/ingest",
        data={"bin_id": "F05VALID01"},
        files=[("photos", ("real.jpg", _minimal_jpeg_bytes(), "image/jpeg"))],
    )
    assert (
        resp.status_code == 200
    ), f"Expected 200 for valid JPEG, got {resp.status_code}: {resp.text}"


def test_ingest_accepts_valid_heif(client):
    """Uploading a genuine HEIF must succeed (200)."""
    resp = client.post(
        "/ingest",
        data={"bin_id": "F05HEIF01"},
        files=[("photos", ("real.heic", _minimal_heif_bytes(), "image/heic"))],
    )
    assert (
        resp.status_code == 200
    ), f"Expected 200 for valid HEIF, got {resp.status_code}: {resp.text}"
