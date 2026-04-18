"""Dev2_013 Problem A: state cleanup between test runs.

Two tests that both create items and both expect the items table to be empty
at the start. Without the autouse cleanup fixture, the second test would see
the first test's row and fail.
"""

from sqlalchemy import text


def test_cleanup_items_empty_at_start_pass_a(client, db):
    count = db.execute(text("SELECT COUNT(*) FROM items")).scalar_one()
    assert count == 0, f"Expected clean items table at test start, got {count}"
    r = client.post("/items", json={"name": "Cleanup Item A", "category": "test"})
    assert r.status_code == 200


def test_cleanup_items_empty_at_start_pass_b(client, db):
    count = db.execute(text("SELECT COUNT(*) FROM items")).scalar_one()
    assert count == 0, (
        f"Expected clean items table at test start, got {count}. "
        f"Previous test's row leaked across the autouse truncate fixture."
    )


# ---------------------------------------------------------------------------
# Dev2_013 Problem B: guard against TEST_DATABASE_URL pointing at production.
# Unit tests against the pure validator in _db_guard.py. Exercising the
# validator without spawning pytest subprocesses keeps these tests fast and
# avoids any risk of a misconfigured subprocess actually touching prod.
# ---------------------------------------------------------------------------


def test_guard_rejects_unset_test_url():
    from _db_guard import test_db_isolation_error as _test_db_isolation_error

    msg = _test_db_isolation_error("", "postgresql://x/y")
    assert msg is not None
    assert "not set" in msg.lower()


def test_guard_rejects_continuous_claude_db():
    from _db_guard import test_db_isolation_error as _test_db_isolation_error

    msg = _test_db_isolation_error(
        "postgresql+psycopg://claude:pw@localhost:5432/continuous_claude",
        None,
    )
    assert msg is not None
    assert "continuous_claude" in msg or "coordination" in msg.lower()


def test_guard_rejects_test_url_equal_to_prod_url():
    from _db_guard import test_db_isolation_error as _test_db_isolation_error

    url = "postgresql+psycopg://binbrain:pw@localhost:5432/binbrain"
    msg = _test_db_isolation_error(url, url)
    assert msg is not None
    assert "differ" in msg.lower() or "production" in msg.lower()


def test_guard_rejects_db_name_without_test_marker():
    from _db_guard import test_db_isolation_error as _test_db_isolation_error

    msg = _test_db_isolation_error(
        "postgresql+psycopg://binbrain:pw@localhost:5432/binbrain",
        None,
    )
    assert msg is not None
    assert "test" in msg.lower()


def test_guard_accepts_well_formed_test_url():
    from _db_guard import test_db_isolation_error as _test_db_isolation_error

    msg = _test_db_isolation_error(
        "postgresql+psycopg://binbrain:pw@localhost:5432/binbrain_test",
        "postgresql+psycopg://binbrain:pw@localhost:5432/binbrain",
    )
    assert msg is None


def test_guard_accepts_any_test_marker_case():
    from _db_guard import test_db_isolation_error as _test_db_isolation_error

    assert _test_db_isolation_error("postgresql+psycopg://u:p@h:1/myTESTdb", None) is None
