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
