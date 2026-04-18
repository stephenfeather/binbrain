"""Security validation helpers.

Pure functions with no module-level I/O — safe to import in unit tests.
"""

import re

# Allowlist: alphanumeric start, then alphanumeric/dash/underscore, max 64 chars total.
# Rejects: path separators, .., null/control bytes, percent-encoding, spaces, leading dot.
_BIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_bin_id(bin_id: str) -> str:
    """Validate bin_id against the allowlist regex.

    Accepts: alphanumeric, dash, underscore; 1-64 characters; must start
    with an alphanumeric character.

    Rejects: slashes, backslashes, dots, percent signs, null bytes, spaces,
    control characters, empty strings, and values longer than 64 characters.

    Returns:
        The original bin_id string if valid.

    Raises:
        ValueError: with message "invalid bin_id" if the value fails validation.
    """
    if not isinstance(bin_id, str) or not _BIN_ID_RE.match(bin_id):
        raise ValueError("invalid bin_id")
    return bin_id
