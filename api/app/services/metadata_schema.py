"""device_metadata schema validation and size cap (F-11).

Accepted top-level keys:  {"device_processing"}
Size cap:                 16 KiB (METADATA_MAX_BYTES)

The validator is intentionally permissive *inside* allowed keys — nested
structure is not validated so that the iOS client can evolve its payload
without a server-side schema change.  Unknown *top-level* keys are rejected
to prevent unbounded field injection into the jsonb column.
"""
import json

METADATA_MAX_BYTES: int = 16 * 1024  # 16 KiB

_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"device_processing"})


def validate_device_metadata(raw: str) -> dict:
    """Parse and validate *raw* device_metadata JSON.

    Args:
        raw: The raw JSON string received from the client.

    Returns:
        The parsed dict if valid.

    Raises:
        ValueError: if the payload is too large or contains disallowed keys.
        json.JSONDecodeError: if *raw* is not valid JSON (caller should catch).
    """
    encoded = raw.encode("utf-8")
    if len(encoded) > METADATA_MAX_BYTES:
        raise ValueError(
            f"device_metadata exceeds the {METADATA_MAX_BYTES}-byte size limit "
            f"({len(encoded)} bytes received)"
        )

    parsed = json.loads(raw)

    if not isinstance(parsed, dict):
        raise ValueError("device_metadata must be a JSON object")

    unknown = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(
            f"device_metadata contains disallowed top-level fields: {sorted(unknown)}"
        )

    return parsed
