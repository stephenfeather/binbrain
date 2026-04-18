"""Image validation service.

Validates uploaded files by magic bytes and PIL decode.
No extension-based trust — content is the authority.
"""

from pathlib import Path

# PIL format names that we accept.
ALLOWED_FORMATS: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP"})

# Try to enable HEIC/HEIF support if pillow-heif is installed.
try:
    import pillow_heif  # type: ignore[import-untyped]

    pillow_heif.register_heif_opener()
    ALLOWED_FORMATS = ALLOWED_FORMATS | {"HEIF"}
except ImportError:
    pass

# Magic-byte signatures for allowed formats.
# Each entry: (offset, magic_bytes, format_name)
_MAGIC: list[tuple[int, bytes, str]] = [
    (0, b"\xff\xd8\xff", "JPEG"),
    (0, b"\x89PNG\r\n\x1a\n", "PNG"),
    # WebP: "RIFF" at 0, "WEBP" at offset 8
    (0, b"RIFF", "WEBP_RIFF"),
]


def _magic_byte_format(path: Path) -> str | None:
    """Return a format name from magic bytes, or None if unrecognised."""
    with path.open("rb") as f:
        header = f.read(16)

    if header[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "WEBP"
    # HEIC/HEIF: ftyp box at offset 4
    if header[4:8] == b"ftyp" and header[8:12] in (
        b"heic",
        b"HEIC",
        b"heis",
        b"heix",
        b"hevc",
        b"hevx",
        b"mif1",
        b"msf1",
    ):
        return "HEIF"
    return None


def validate_image_file(path: Path) -> str:
    """Validate that *path* contains an allowed image type.

    Performs both a magic-byte check and a PIL verify() to confirm the file
    is a well-formed image of an accepted type.

    Args:
        path: Filesystem path to the file to validate.

    Returns:
        The detected format string (e.g., "JPEG", "PNG").

    Raises:
        ValueError: If the file is not a recognised/allowed image type,
                    or if PIL verification fails.
    """
    magic_fmt = _magic_byte_format(path)
    if magic_fmt is None or magic_fmt not in ALLOWED_FORMATS:
        raise ValueError(
            f"unsupported file type (magic bytes indicate {magic_fmt!r}); "
            f"allowed formats: {sorted(ALLOWED_FORMATS)}"
        )

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as img:
            pil_fmt = img.format
            img.verify()
    except (UnidentifiedImageError, Exception) as exc:
        raise ValueError(f"image verification failed: {exc}") from exc

    if pil_fmt not in ALLOWED_FORMATS:
        raise ValueError(
            f"PIL detected {pil_fmt!r}, which is not in allowed formats: "
            f"{sorted(ALLOWED_FORMATS)}"
        )

    return pil_fmt
