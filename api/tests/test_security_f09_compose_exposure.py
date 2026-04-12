"""F-09 (Medium): Default compose exposes API and PostgreSQL to the host — RED tests.

Tests FAIL until docker-compose.yml binds both the DB and API ports to 127.0.0.1,
preventing exposure on networked interfaces.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent  # tests/ → api/ → repo root
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


def _compose_text() -> str:
    return COMPOSE_FILE.read_text()


def test_compose_file_exists():
    assert COMPOSE_FILE.exists(), f"docker-compose.yml not found at {COMPOSE_FILE}"


def test_db_port_bound_to_localhost():
    """PostgreSQL must be bound to 127.0.0.1, not all interfaces."""
    text = _compose_text()
    # A raw "${DB_PORT:-5434}:5432" binding exposes on 0.0.0.0; require the
    # explicit host prefix.
    assert '127.0.0.1:${DB_PORT' in text or "127.0.0.1:${DB_PORT" in text, (
        "DB port must be bound to 127.0.0.1 (e.g. '127.0.0.1:${DB_PORT:-5434}:5432'). "
        f"Found: {[ln for ln in text.splitlines() if '5432' in ln]}"
    )


def test_api_port_bound_to_localhost():
    """API must be bound to 127.0.0.1, not all interfaces."""
    text = _compose_text()
    assert '127.0.0.1:${API_PORT' in text or "127.0.0.1:${API_PORT" in text, (
        "API port must be bound to 127.0.0.1 (e.g. '127.0.0.1:${API_PORT:-8000}:8000'). "
        f"Found: {[ln for ln in text.splitlines() if '8000' in ln]}"
    )


def test_db_port_not_exposed_bare():
    """Bare port mapping '${DB_PORT:-5434}:5432' (without 127.0.0.1 prefix) must not exist."""
    text = _compose_text()
    for line in text.splitlines():
        stripped = line.strip()
        # A bare mapping starts with - and the port without a host prefix
        if stripped.startswith('- "${DB_PORT') or stripped.startswith("- '${DB_PORT"):
            assert False, f"DB port exposed without host binding: {line!r}"
        if stripped.startswith('- ${DB_PORT'):
            assert False, f"DB port exposed without host binding: {line!r}"


def test_api_port_not_exposed_bare():
    """Bare port mapping '${API_PORT:-8000}:8000' (without 127.0.0.1 prefix) must not exist."""
    text = _compose_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('- "${API_PORT') or stripped.startswith("- '${API_PORT"):
            assert False, f"API port exposed without host binding: {line!r}"
        if stripped.startswith('- ${API_PORT'):
            assert False, f"API port exposed without host binding: {line!r}"
