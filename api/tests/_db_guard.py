"""Test-DB isolation validator (Dev2_013 Problem B).

Kept separate from conftest.py so the validator is importable from test modules
without tangling with pytest's conftest resolution. The underscore prefix keeps
pytest from collecting this file.
"""

from typing import Optional
from urllib.parse import urlparse


def test_db_isolation_error(test_db_url: str, prod_db_url: Optional[str]) -> Optional[str]:
    """Return a human-readable error if ``TEST_DATABASE_URL`` is unsafe, else None.

    Hard-fail conditions:
    1. Unset — refuse (caller decides whether to skip or fail).
    2. Cross-terminal coordination DB (``continuous_claude`` or user ``claude``).
    3. Equal to ``DATABASE_URL`` (production).
    4. DB name component does not contain ``"test"`` (case-insensitive).
    """
    if not test_db_url:
        return "TEST_DATABASE_URL not set"

    parsed = urlparse(test_db_url.replace("postgresql+psycopg://", "postgresql://"))
    db_name = (parsed.path or "").lstrip("/")

    if db_name == "continuous_claude" or parsed.username == "claude":
        return (
            f"TEST_DATABASE_URL points at the cross-terminal coordination DB "
            f"(user={parsed.username!r}, db={db_name!r}). Refusing to run — this "
            f"would drop/recreate binbrain tables in continuous_claude. "
            f"Use the binbrain_db container (host port 5434)."
        )

    if prod_db_url and test_db_url == prod_db_url:
        return (
            f"TEST_DATABASE_URL must differ from DATABASE_URL (both are "
            f"{test_db_url!r}). Refusing to run — this would execute tests "
            f"against the production database and wipe live data."
        )

    if "test" not in db_name.lower():
        return (
            f"TEST_DATABASE_URL database name {db_name!r} has no 'test' marker. "
            f"Refusing to run — the DB name must contain 'test' (e.g. "
            f"'binbrain_test') so accidental prod pointers fail loudly before "
            f"any schema drop. Source .env.test to get the correct URL."
        )

    return None
