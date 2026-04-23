"""device_metadata schema validation, size cap, and sensitive-field hashing (F-11).

Constraints
-----------
- Top-level key allowlist:      {"device_processing"}
- Inner key allowlist:          ALLOWED_DEVICE_PROCESSING_KEYS
- Size cap:                     4 KiB (METADATA_MAX_BYTES)
- Max nesting depth:            5 levels (accommodates ocr[i].bounding_box.x)
- Max per-value string length:  1 KiB (STRING_MAX_BYTES)
- Sensitive fields:             fields whose name matches /(device_id|imei|mac|serial)$/i
                                are HMAC-SHA256 hashed with server-side pepper
                                (env ``METADATA_HASH_PEPPER``) before persistence.
                                When the pepper is unset the HMAC key is empty —
                                still deterministic but not cross-install-correlatable
                                once a pepper is configured.

The validator is strict on structure to prevent unbounded field injection into
the jsonb column, while permissive on value types within allowed keys so that
the iOS client can evolve its payload without server-side schema changes.
"""

import hashlib
import hmac
import json
import os
import re

# ── Constants ────────────────────────────────────────────────────────────────

METADATA_MAX_BYTES: int = 4 * 1024  # 4 KiB
STRING_MAX_BYTES: int = 1 * 1024  # 1 KiB per string value
NESTING_MAX_DEPTH: int = 5

_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"device_processing"})

# Explicit allowlist for fields inside device_processing.
# Covers all fields present in the iOS client's canonical payload.
ALLOWED_DEVICE_PROCESSING_KEYS: frozenset[str] = frozenset(
    {
        # Operational / quality fields
        "version",
        "pipeline_ms",
        "ios_version",
        "device_model",
        "quality_scores",
        "ocr",
        "barcodes",
        "classifications",
        "crop_applied",
        # Optional pipeline context objects sent by the iOS client.
        "saliency_context",
        "capture_metadata",
        "quality_override_context",
        "canny_metrics",
        "optimized_upload",
        # Device identity fields — values are HMAC-SHA256 hashed with server-side pepper
        # (env METADATA_HASH_PEPPER) before persistence (see below).
        "device_id",
        "device_serial",
        "device_imei",
        "wifi_mac",
        "bluetooth_mac",
    }
)

# Regex matching sensitive PII field names that must be hashed before persistence.
_SENSITIVE_KEY_RE = re.compile(r"(device_id|imei|mac|serial)$", re.IGNORECASE)


# ── Internal helpers ─────────────────────────────────────────────────────────


def _get_pepper() -> bytes:
    """Read METADATA_HASH_PEPPER from env each call (test-friendly)."""
    return os.environ.get("METADATA_HASH_PEPPER", "").encode("utf-8")


def _hash_value(value: str) -> str:
    """Return HMAC-SHA256 hex digest of *value* keyed with the server-side pepper."""
    return hmac.new(_get_pepper(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _check_depth(obj: object, current: int) -> None:
    """Raise ValueError if *obj* contains nesting beyond NESTING_MAX_DEPTH."""
    if current > NESTING_MAX_DEPTH:
        raise ValueError(f"device_metadata nesting depth exceeds maximum of {NESTING_MAX_DEPTH}")
    if isinstance(obj, dict):
        for v in obj.values():
            _check_depth(v, current + 1)
    elif isinstance(obj, list):
        for item in obj:
            _check_depth(item, current + 1)


def _check_string_lengths(obj: object) -> None:
    """Raise ValueError if any string value in *obj* exceeds STRING_MAX_BYTES."""
    if isinstance(obj, str):
        if len(obj.encode("utf-8")) > STRING_MAX_BYTES:
            raise ValueError(
                f"device_metadata string value exceeds the {STRING_MAX_BYTES}-byte limit"
            )
    elif isinstance(obj, dict):
        for v in obj.values():
            _check_string_lengths(v)
    elif isinstance(obj, list):
        for item in obj:
            _check_string_lengths(item)


def _hash_sensitive_fields(obj: object) -> object:
    """Recursively replace values of sensitive-named fields with HMAC-SHA256 hashes.

    Any field whose name matches /(device_id|imei|mac|serial)$/i and whose
    value is a string is replaced with ``_hash_value(value)`` (HMAC-SHA256 keyed
    with the server-side pepper from env ``METADATA_HASH_PEPPER``).
    Non-string values for sensitive fields are left unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: (
                _hash_value(v)
                if _SENSITIVE_KEY_RE.search(k) and isinstance(v, str)
                else _hash_sensitive_fields(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_hash_sensitive_fields(item) for item in obj]
    return obj


# ── Public API ───────────────────────────────────────────────────────────────


def validate_device_metadata(raw: str) -> dict:
    """Parse, validate, and sanitise *raw* device_metadata JSON.

    Applies all constraints (size, schema, depth, string length) then hashes
    sensitive fields before returning the processed dict for persistence.

    Args:
        raw: The raw JSON string received from the client.

    Returns:
        Validated and sanitised dict, ready for storage.

    Raises:
        ValueError: size, schema, depth, or string-length constraint violated.
        json.JSONDecodeError: *raw* is not valid JSON (caller should catch separately).
    """
    # 1. Size cap (checked on raw bytes before any parsing).
    encoded = raw.encode("utf-8")
    if len(encoded) > METADATA_MAX_BYTES:
        raise ValueError(
            f"device_metadata size ({len(encoded)} bytes) exceeds the "
            f"{METADATA_MAX_BYTES}-byte limit"
        )

    parsed = json.loads(raw)

    if not isinstance(parsed, dict):
        raise ValueError("device_metadata must be a JSON object")

    # 2. Top-level key allowlist.
    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise ValueError(
            f"device_metadata contains disallowed top-level fields: {sorted(unknown_top)}"
        )

    # 3. Inner key allowlist for device_processing.
    dp = parsed.get("device_processing")
    if dp is not None and isinstance(dp, dict):
        unknown_inner = set(dp.keys()) - ALLOWED_DEVICE_PROCESSING_KEYS
        if unknown_inner:
            raise ValueError(
                f"device_processing contains disallowed fields: {sorted(unknown_inner)}"
            )

    # 4. Nesting depth check.
    _check_depth(parsed, current=0)

    # 5. Per-value string length check.
    _check_string_lengths(parsed)

    # 6. Hash sensitive field values before persistence.
    return _hash_sensitive_fields(parsed)
