"""FEAT-3: UNASSIGNED sentinel bin + reattribution helper.

Server schema prep — no API surface yet. Covers:

* the sentinel exists on a fresh DB (autouse fixture re-seeds after truncate),
* the protective trigger blocks both hard DELETE and soft-delete UPDATE on
  the sentinel,
* ``reattribute_bin_items_to_unassigned`` moves rows, drops conflicts
  cleanly (no UNIQUE violation), and refuses to operate on the sentinel.
"""

from __future__ import annotations

import pytest
from app.db import repository
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def test_unassigned_sentinel_exists(db):
    row = db.execute(
        text("SELECT bin_id, deleted_at FROM bins WHERE bin_id = 'UNASSIGNED'")
    ).first()
    assert row is not None
    assert row[1] is None


def test_cannot_hard_delete_unassigned_sentinel(db):
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("DELETE FROM bins WHERE bin_id = 'UNASSIGNED'"))
        db.flush()
    assert "cannot delete sentinel UNASSIGNED bin" in str(exc.value)
    db.rollback()


def test_cannot_soft_delete_unassigned_sentinel(db):
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("UPDATE bins SET deleted_at = now() WHERE bin_id = 'UNASSIGNED'"))
        db.flush()
    assert "cannot soft-delete sentinel UNASSIGNED bin" in str(exc.value)
    db.rollback()


def test_other_bins_can_still_be_soft_deleted(db):
    db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-DELETABLE-0001')"))
    db.execute(text("UPDATE bins SET deleted_at = now() WHERE bin_id = 'BIN-DELETABLE-0001'"))
    db.commit()

    deleted_at = db.execute(
        text("SELECT deleted_at FROM bins WHERE bin_id = 'BIN-DELETABLE-0001'")
    ).scalar_one()
    assert deleted_at is not None


def _seed_bin_with_item(db, bin_id: str, item_name: str) -> int:
    db.execute(text("INSERT INTO bins (bin_id) VALUES (:b)"), {"b": bin_id})
    item_id = db.execute(
        text("INSERT INTO items (name, category) VALUES (:n, 'misc') " "RETURNING item_id"),
        {"n": item_name},
    ).scalar_one()
    db.execute(
        text("INSERT INTO bin_items (bin_id, item_id, quantity) " "VALUES (:b, :i, 5)"),
        {"b": bin_id, "i": item_id},
    )
    db.commit()
    return int(item_id)


def test_reattribute_moves_rows_to_unassigned(db):
    item_id = _seed_bin_with_item(db, "BIN-REATTR-0001", "lone widget")

    moved = repository.reattribute_bin_items_to_unassigned(db, "BIN-REATTR-0001")
    db.commit()

    assert moved == 1

    src = db.execute(
        text("SELECT COUNT(*) FROM bin_items WHERE bin_id = 'BIN-REATTR-0001'")
    ).scalar_one()
    assert src == 0

    dst = db.execute(
        text("SELECT COUNT(*) FROM bin_items " "WHERE bin_id = 'UNASSIGNED' AND item_id = :i"),
        {"i": item_id},
    ).scalar_one()
    assert dst == 1


def test_reattribute_drops_conflicts_without_violating_unique(db):
    # Same item ends up in both the source bin and UNASSIGNED already; the
    # source row must be dropped (not merged) so the UNIQUE(bin_id, item_id)
    # constraint isn't violated.
    item_id = _seed_bin_with_item(db, "BIN-REATTR-DUP-0001", "dup widget")
    db.execute(
        text("INSERT INTO bin_items (bin_id, item_id, quantity) " "VALUES ('UNASSIGNED', :i, 99)"),
        {"i": item_id},
    )
    db.commit()

    result = repository.reattribute_bin_items_to_unassigned(db, "BIN-REATTR-DUP-0001")
    db.commit()

    # Source row was dropped, UNASSIGNED row stayed put.
    assert result == 1
    src = db.execute(
        text("SELECT COUNT(*) FROM bin_items WHERE bin_id = 'BIN-REATTR-DUP-0001'")
    ).scalar_one()
    assert src == 0

    quantity = db.execute(
        text("SELECT quantity FROM bin_items " "WHERE bin_id = 'UNASSIGNED' AND item_id = :i"),
        {"i": item_id},
    ).scalar_one()
    # Pre-existing UNASSIGNED row preserved (no quantity merging).
    assert quantity == 99


def test_reattribute_idempotent_no_rows(db):
    # No source rows → 0 moved, no error.
    db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-EMPTY-0001')"))
    db.commit()
    moved = repository.reattribute_bin_items_to_unassigned(db, "BIN-EMPTY-0001")
    assert moved == 0


def test_reattribute_refuses_sentinel_self_target(db):
    with pytest.raises(ValueError):
        repository.reattribute_bin_items_to_unassigned(db, "UNASSIGNED")
