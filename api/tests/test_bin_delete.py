"""ApiDev_005 (FEAT-4-route): DELETE /bins/{bin_id} integration tests.

Calls reattribute_bin_items_to_unassigned (PR #28) inside a single
transaction, then soft-deletes the bin. Sentinel deletion is preempted
with a clean 400 + custom error code so the trigger never fires.
"""

from __future__ import annotations

from sqlalchemy import text


def _seed_bin_with_items(client, db, bin_id: str, item_names: list[str]) -> list[int]:
    """Create the bin via /items POSTs (ensures bin row exists). Returns item_ids."""
    item_ids: list[int] = []
    for name in item_names:
        resp = client.post(
            "/items",
            json={"name": name, "category": "fastener", "bin_id": bin_id},
        )
        assert resp.status_code == 200, resp.text
        item_ids.append(resp.json()["item_id"])
    db.commit()
    return item_ids


# ---------------------------------------------------------------------------
# 1. Happy path — bin with items
# ---------------------------------------------------------------------------


def test_delete_bin_reattributes_items_then_soft_deletes(client, db):
    bin_id = "BIN-DEL-HAPPY-0001"
    item_ids = _seed_bin_with_items(client, db, bin_id, ["alpha", "beta", "gamma"])
    assert len(item_ids) == 3

    resp = client.delete(f"/bins/{bin_id}")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["status"] == "deleted"
    assert body["bin_id"] == bin_id
    assert body["moved_item_count"] == 3
    assert body["deleted_at"] is not None

    deleted_at = db.execute(
        text("SELECT deleted_at FROM bins WHERE bin_id = :b"), {"b": bin_id}
    ).scalar_one()
    assert deleted_at is not None

    src_count = db.execute(
        text("SELECT COUNT(*) FROM bin_items WHERE bin_id = :b"), {"b": bin_id}
    ).scalar_one()
    assert src_count == 0

    moved_in_unassigned = db.execute(
        text(
            "SELECT COUNT(*) FROM bin_items " "WHERE bin_id = 'UNASSIGNED' AND item_id = ANY(:ids)"
        ),
        {"ids": item_ids},
    ).scalar_one()
    assert moved_in_unassigned == 3


# ---------------------------------------------------------------------------
# 2. No-items path
# ---------------------------------------------------------------------------


def test_delete_empty_bin_soft_deletes_with_zero_items(client, db):
    bin_id = "BIN-DEL-EMPTY-0001"
    db.execute(text("INSERT INTO bins (bin_id) VALUES (:b)"), {"b": bin_id})
    db.commit()

    resp = client.delete(f"/bins/{bin_id}")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["status"] == "deleted"
    assert body["moved_item_count"] == 0

    deleted_at = db.execute(
        text("SELECT deleted_at FROM bins WHERE bin_id = :b"), {"b": bin_id}
    ).scalar_one()
    assert deleted_at is not None


# ---------------------------------------------------------------------------
# 3. Sentinel refusal
# ---------------------------------------------------------------------------


def test_delete_sentinel_returns_400_with_custom_error_code(client, db):
    resp = client.delete("/bins/UNASSIGNED")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "cannot_delete_sentinel"
    assert "UNASSIGNED" in body["error"]["message"]

    # Sentinel still present and active.
    row = db.execute(
        text("SELECT bin_id, deleted_at FROM bins WHERE bin_id = 'UNASSIGNED'")
    ).first()
    assert row is not None
    assert row[1] is None


# ---------------------------------------------------------------------------
# 4. Not found
# ---------------------------------------------------------------------------


def test_delete_nonexistent_bin_returns_404(client):
    resp = client.delete("/bins/NONEXISTENT-BIN-9999")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "not_found"


# SEC-31-1: ensure DELETE shares the same bin_id regex validation as POST/PATCH.
# A bin_id containing path separators or characters outside
# ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ must be rejected at the validator,
# not silently treated as a missing bin (or worse, normalized through).
def test_delete_invalid_bin_id_returns_400(client):
    # Dots survive FastAPI's path routing (the {bin_id} param accepts them)
    # but the regex ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ rejects them. Slashes
    # would 404 at the routing layer before reaching the handler.
    resp = client.delete("/bins/bin..with..dots")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "bad_request"
    assert "invalid bin_id" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# 5. Already-deleted idempotency
# ---------------------------------------------------------------------------


def test_delete_already_deleted_bin_returns_404(client, db):
    bin_id = "BIN-DEL-IDEM-0001"
    db.execute(text("INSERT INTO bins (bin_id) VALUES (:b)"), {"b": bin_id})
    db.commit()

    first = client.delete(f"/bins/{bin_id}")
    assert first.status_code == 200, first.text

    second = client.delete(f"/bins/{bin_id}")
    assert second.status_code == 404, second.text
    body = second.json()
    assert body["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# 6. Conflict path — item already in UNASSIGNED gets dropped from source
# ---------------------------------------------------------------------------


def test_delete_bin_drops_conflicting_items_already_in_unassigned(client, db):
    bin_id = "BIN-DEL-CONFLICT-0001"
    [item_id] = _seed_bin_with_items(client, db, bin_id, ["dup widget"])

    # Pre-seed UNASSIGNED with the same item_id (different quantity to
    # confirm the pre-existing row wins, source row is dropped).
    db.execute(
        text("INSERT INTO bin_items (bin_id, item_id, quantity) " "VALUES ('UNASSIGNED', :i, 99)"),
        {"i": item_id},
    )
    db.commit()

    resp = client.delete(f"/bins/{bin_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # CTE consumed 1 source row (it was dropped via ON CONFLICT, not moved).
    assert body["moved_item_count"] == 1

    # UNASSIGNED still has the item exactly once, with the pre-existing quantity.
    rows = db.execute(
        text("SELECT quantity FROM bin_items " "WHERE bin_id = 'UNASSIGNED' AND item_id = :i"),
        {"i": item_id},
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 99


# ---------------------------------------------------------------------------
# 7. Auth — no API key returns 401
# ---------------------------------------------------------------------------


def test_delete_without_api_key_returns_401(app_module):
    """Use a bare TestClient without the X-API-Key header."""
    from fastapi.testclient import TestClient

    bare_client = TestClient(app_module.app)
    resp = bare_client.delete("/bins/SOME-BIN")
    assert resp.status_code == 401
