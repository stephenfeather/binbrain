"""ApiDev2_013: POST /associate duplicate-insert feedback (``inserted`` flag).

The route previously swallowed the ``insert_bin_item`` return value and
always responded ``{ok: true, ...}`` regardless of whether a new row was
created or the unique-constraint on ``(bin_id, item_id)`` fired silently.
iOS could not distinguish "new association" from "already there — nothing
changed", so a user re-confirming the same suggestion produced no
feedback.

The fix is signal-only: the route now surfaces the bool return as an
additive ``inserted`` field. No schema change, no quantity merging —
``ON CONFLICT DO NOTHING`` behavior is unchanged.
"""

from __future__ import annotations

from sqlalchemy import text


def _seed_item(db, name: str = "associate-thing") -> int:
    """Create a reusable items row and return its item_id.

    Commits so the route's separate Session (per-request ``get_db``) can
    see the row when it runs the ``bin_items`` INSERT's FK check.
    """
    row = db.execute(
        text(
            "INSERT INTO items (name, category, notes) "
            "VALUES (:n, 'misc', NULL) "
            "ON CONFLICT (fingerprint) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING item_id"
        ),
        {"n": name},
    ).scalar_one()
    db.commit()
    return int(row)


def _row_count(db, bin_id: str, item_id: int) -> int:
    return int(
        db.execute(
            text("SELECT COUNT(*) FROM bin_items " "WHERE bin_id = :b AND item_id = :i"),
            {"b": bin_id, "i": item_id},
        ).scalar_one()
    )


def _bin_item_row(db, bin_id: str, item_id: int) -> dict | None:
    row = (
        db.execute(
            text(
                "SELECT bin_id, item_id, quantity, confidence "
                "FROM bin_items WHERE bin_id = :b AND item_id = :i"
            ),
            {"b": bin_id, "i": item_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 1. Happy path — fresh insert returns inserted=true
# ---------------------------------------------------------------------------


def test_associate_first_call_returns_inserted_true(client, db):
    item = _seed_item(db, "assoc-fresh")

    resp = client.post(
        "/associate",
        json={"bin_id": "B-ASSOC-1", "item_id": item, "quantity": 4, "confidence": 0.6},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "ok": True,
        "bin_id": "B-ASSOC-1",
        "item_id": item,
        "inserted": True,
    }
    assert _row_count(db, "B-ASSOC-1", item) == 1


# ---------------------------------------------------------------------------
# 2. Duplicate — second call with identical pair returns inserted=false
# ---------------------------------------------------------------------------


def test_associate_duplicate_returns_inserted_false_and_preserves_row(client, db):
    item = _seed_item(db, "assoc-dup")

    first = client.post(
        "/associate",
        json={"bin_id": "B-ASSOC-2", "item_id": item, "quantity": 7, "confidence": 0.9},
    )
    assert first.status_code == 200
    assert first.json()["inserted"] is True

    # Second call with DIFFERENT quantity/confidence — the contract is
    # "no change on conflict", so the original row must be preserved.
    second = client.post(
        "/associate",
        json={"bin_id": "B-ASSOC-2", "item_id": item, "quantity": 99, "confidence": 0.1},
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body == {
        "ok": True,
        "bin_id": "B-ASSOC-2",
        "item_id": item,
        "inserted": False,
    }
    assert _row_count(db, "B-ASSOC-2", item) == 1

    row = _bin_item_row(db, "B-ASSOC-2", item)
    assert row is not None
    assert row["quantity"] == 7.0, "quantity must NOT be updated on conflict"
    assert row["confidence"] == 0.9, "confidence must NOT be updated on conflict"


# ---------------------------------------------------------------------------
# 3. Distinct item_ids into the same bin — both inserted=true
# ---------------------------------------------------------------------------


def test_associate_distinct_items_into_same_bin_both_inserted_true(client, db):
    item_a = _seed_item(db, "assoc-distinct-a")
    item_b = _seed_item(db, "assoc-distinct-b")
    assert item_a != item_b

    r_a = client.post("/associate", json={"bin_id": "B-ASSOC-3", "item_id": item_a})
    r_b = client.post("/associate", json={"bin_id": "B-ASSOC-3", "item_id": item_b})

    assert r_a.status_code == 200, r_a.text
    assert r_b.status_code == 200, r_b.text
    assert r_a.json()["inserted"] is True
    assert r_b.json()["inserted"] is True

    total = int(
        db.execute(
            text("SELECT COUNT(*) FROM bin_items WHERE bin_id = :b"),
            {"b": "B-ASSOC-3"},
        ).scalar_one()
    )
    assert total == 2


# ---------------------------------------------------------------------------
# 4. Same item into two different bins — both inserted=true
# ---------------------------------------------------------------------------


def test_associate_same_item_into_two_bins_both_inserted_true(client, db):
    """The unique constraint is on (bin_id, item_id), so the same item_id
    CAN appear in two different bins. Each should land as a fresh insert."""
    item = _seed_item(db, "assoc-multi-bin")

    r_a = client.post("/associate", json={"bin_id": "B-ASSOC-4A", "item_id": item})
    r_b = client.post("/associate", json={"bin_id": "B-ASSOC-4B", "item_id": item})

    assert r_a.status_code == 200
    assert r_b.status_code == 200
    assert r_a.json()["inserted"] is True
    assert r_b.json()["inserted"] is True

    assert _row_count(db, "B-ASSOC-4A", item) == 1
    assert _row_count(db, "B-ASSOC-4B", item) == 1
