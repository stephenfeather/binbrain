"""FEAT-2-backend (ApiDev2_012): PATCH /bins/{bin_id}/items/{item_id} move semantics.

Spec: docs/openapi.yaml lines 1402-1509. Body field is ``bin_id`` (not
``target_bin_id``), ``minProperties: 1``, ``additionalProperties: false``.
Response includes ``version``, ``bin_id`` (effective), ``item_id``,
``quantity`` (nullable), ``confidence`` (nullable), ``moved`` (bool).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_bin(db, bin_id: str) -> None:
    db.execute(
        text("INSERT INTO bins (bin_id) VALUES (:b) ON CONFLICT DO NOTHING"),
        {"b": bin_id},
    )


def _seed_item(db, name: str = "thing") -> int:
    row = db.execute(
        text(
            "INSERT INTO items (name, category, notes) "
            "VALUES (:n, 'misc', NULL) "
            "ON CONFLICT (fingerprint) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING item_id"
        ),
        {"n": name},
    ).scalar_one()
    return int(row)


def _seed_bin_item(
    db,
    bin_id: str,
    item_id: int,
    *,
    quantity: float | None = 1.0,
    confidence: float | None = 0.5,
) -> None:
    _seed_bin(db, bin_id)
    db.execute(
        text(
            "INSERT INTO bin_items (bin_id, item_id, quantity, confidence) "
            "VALUES (:b, :i, :q, :c)"
        ),
        {"b": bin_id, "i": item_id, "q": quantity, "c": confidence},
    )
    db.commit()


def _bin_item_row(db, bin_id: str, item_id: int) -> dict[str, Any] | None:
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
# 1-3 In-place update
# ---------------------------------------------------------------------------


def test_patch_in_place_quantity_only(client, db):
    item = _seed_item(db, "q-only")
    _seed_bin_item(db, "B-INP-Q", item, quantity=1.0, confidence=0.5)

    resp = client.patch(f"/bins/B-INP-Q/items/{item}", json={"quantity": 7.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "version": "1",
        "bin_id": "B-INP-Q",
        "item_id": item,
        "quantity": 7.0,
        "confidence": 0.5,
        "moved": False,
    }

    row = _bin_item_row(db, "B-INP-Q", item)
    assert row is not None
    assert row["quantity"] == 7.0
    assert row["confidence"] == 0.5


def test_patch_in_place_confidence_only(client, db):
    item = _seed_item(db, "c-only")
    _seed_bin_item(db, "B-INP-C", item, quantity=3.0, confidence=0.2)

    resp = client.patch(f"/bins/B-INP-C/items/{item}", json={"confidence": 0.9})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confidence"] == 0.9
    assert body["quantity"] == 3.0
    assert body["moved"] is False

    row = _bin_item_row(db, "B-INP-C", item)
    assert row["quantity"] == 3.0
    assert row["confidence"] == 0.9


def test_patch_in_place_both_fields(client, db):
    item = _seed_item(db, "both")
    _seed_bin_item(db, "B-INP-BOTH", item, quantity=1.0, confidence=0.1)

    resp = client.patch(
        f"/bins/B-INP-BOTH/items/{item}", json={"quantity": 5.5, "confidence": 0.75}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quantity"] == 5.5
    assert body["confidence"] == 0.75
    assert body["moved"] is False


def test_patch_body_bin_id_equals_path_is_noop_not_moved(client, db):
    """Spec: 'If omitted or equal to the path bin_id, the association stays in place.'"""
    item = _seed_item(db, "same-bin")
    _seed_bin_item(db, "B-SAMEBIN", item, quantity=2.0, confidence=0.3)

    resp = client.patch(f"/bins/B-SAMEBIN/items/{item}", json={"bin_id": "B-SAMEBIN"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bin_id"] == "B-SAMEBIN"
    assert body["moved"] is False
    assert body["quantity"] == 2.0
    assert body["confidence"] == 0.3


# ---------------------------------------------------------------------------
# 5-8 Move paths
# ---------------------------------------------------------------------------


def test_patch_move_to_empty_target_inherits_quantity_and_confidence(client, db):
    item = _seed_item(db, "move-inherit")
    _seed_bin_item(db, "B-SRC-1", item, quantity=4.0, confidence=0.6)
    _seed_bin(db, "B-DST-1")
    db.commit()

    resp = client.patch(f"/bins/B-SRC-1/items/{item}", json={"bin_id": "B-DST-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "version": "1",
        "bin_id": "B-DST-1",
        "item_id": item,
        "quantity": 4.0,
        "confidence": 0.6,
        "moved": True,
    }

    assert _bin_item_row(db, "B-SRC-1", item) is None
    dst = _bin_item_row(db, "B-DST-1", item)
    assert dst is not None
    assert dst["quantity"] == 4.0
    assert dst["confidence"] == 0.6


def test_patch_move_with_quantity_override(client, db):
    item = _seed_item(db, "move-qover")
    _seed_bin_item(db, "B-SRC-2", item, quantity=1.0, confidence=0.4)
    _seed_bin(db, "B-DST-2")
    db.commit()

    resp = client.patch(
        f"/bins/B-SRC-2/items/{item}",
        json={"bin_id": "B-DST-2", "quantity": 99.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bin_id"] == "B-DST-2"
    assert body["quantity"] == 99.0
    assert body["confidence"] == 0.4
    assert body["moved"] is True

    dst = _bin_item_row(db, "B-DST-2", item)
    assert dst["quantity"] == 99.0
    assert dst["confidence"] == 0.4


def test_patch_move_with_confidence_override(client, db):
    item = _seed_item(db, "move-cover")
    _seed_bin_item(db, "B-SRC-3", item, quantity=7.0, confidence=0.2)
    _seed_bin(db, "B-DST-3")
    db.commit()

    resp = client.patch(
        f"/bins/B-SRC-3/items/{item}",
        json={"bin_id": "B-DST-3", "confidence": 0.88},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quantity"] == 7.0
    assert body["confidence"] == 0.88
    assert body["moved"] is True


def test_patch_move_with_both_overrides(client, db):
    item = _seed_item(db, "move-both")
    _seed_bin_item(db, "B-SRC-4", item, quantity=1.0, confidence=0.1)
    _seed_bin(db, "B-DST-4")
    db.commit()

    resp = client.patch(
        f"/bins/B-SRC-4/items/{item}",
        json={"bin_id": "B-DST-4", "quantity": 42.0, "confidence": 0.5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quantity"] == 42.0
    assert body["confidence"] == 0.5
    assert body["moved"] is True


# ---------------------------------------------------------------------------
# 9 Conflict
# ---------------------------------------------------------------------------


def test_patch_move_conflict_returns_409_and_leaves_both_rows_unchanged(client, db):
    item = _seed_item(db, "conflict")
    _seed_bin_item(db, "B-SRC-9", item, quantity=3.0, confidence=0.3)
    _seed_bin_item(db, "B-DST-9", item, quantity=7.0, confidence=0.7)

    resp = client.patch(f"/bins/B-SRC-9/items/{item}", json={"bin_id": "B-DST-9"})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["version"] == "1"
    assert body["error"]["code"] == "target_already_has_item"
    assert "request_id" in body["error"]
    assert resp.headers.get("x-request-id") is not None

    src = _bin_item_row(db, "B-SRC-9", item)
    assert src is not None
    assert src["quantity"] == 3.0
    assert src["confidence"] == 0.3

    dst = _bin_item_row(db, "B-DST-9", item)
    assert dst is not None
    assert dst["quantity"] == 7.0
    assert dst["confidence"] == 0.7


# ---------------------------------------------------------------------------
# 10-11 Sentinel interaction
# ---------------------------------------------------------------------------


def test_patch_move_to_unassigned_succeeds(client, db):
    item = _seed_item(db, "to-unassigned")
    _seed_bin_item(db, "B-SRC-10", item, quantity=1.0, confidence=0.4)

    resp = client.patch(f"/bins/B-SRC-10/items/{item}", json={"bin_id": "UNASSIGNED"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bin_id"] == "UNASSIGNED"
    assert body["moved"] is True

    assert _bin_item_row(db, "B-SRC-10", item) is None
    assert _bin_item_row(db, "UNASSIGNED", item) is not None


def test_patch_move_from_unassigned_succeeds(client, db):
    item = _seed_item(db, "from-unassigned")
    _seed_bin_item(db, "UNASSIGNED", item, quantity=2.0, confidence=0.5)
    _seed_bin(db, "B-DST-11")
    db.commit()

    resp = client.patch(f"/bins/UNASSIGNED/items/{item}", json={"bin_id": "B-DST-11"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bin_id"] == "B-DST-11"
    assert body["moved"] is True

    assert _bin_item_row(db, "UNASSIGNED", item) is None
    assert _bin_item_row(db, "B-DST-11", item) is not None


# ---------------------------------------------------------------------------
# 12-14 404s
# ---------------------------------------------------------------------------


def test_patch_404_when_path_bin_unknown(client, db):
    item = _seed_item(db, "nobin")

    resp = client.patch(f"/bins/B-UNKNOWN-X/items/{item}", json={"quantity": 1.0})
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["version"] == "1"
    assert body["error"]["code"] == "bin_not_found"
    assert resp.headers.get("x-request-id") is not None


def test_patch_404_when_item_not_in_source_bin(client, db):
    item = _seed_item(db, "not-in-src")
    _seed_bin(db, "B-ITEM-MISS")
    db.commit()

    resp = client.patch(f"/bins/B-ITEM-MISS/items/{item}", json={"quantity": 1.0})
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "item_not_found_in_source_bin"


def test_patch_404_when_target_bin_not_found(client, db):
    item = _seed_item(db, "tgt-miss")
    _seed_bin_item(db, "B-SRC-14", item, quantity=1.0, confidence=0.1)

    resp = client.patch(f"/bins/B-SRC-14/items/{item}", json={"bin_id": "B-DOES-NOT-EXIST"})
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "target_bin_not_found"

    # Source row must still be present.
    assert _bin_item_row(db, "B-SRC-14", item) is not None


def test_patch_404_when_target_bin_soft_deleted(client, db):
    item = _seed_item(db, "tgt-softdel")
    _seed_bin_item(db, "B-SRC-14B", item, quantity=1.0, confidence=0.1)
    db.execute(
        text(
            "INSERT INTO bins (bin_id, deleted_at) VALUES (:b, now()) "
            "ON CONFLICT (bin_id) DO UPDATE SET deleted_at = now()"
        ),
        {"b": "B-DELETED-14"},
    )
    db.commit()

    resp = client.patch(f"/bins/B-SRC-14B/items/{item}", json={"bin_id": "B-DELETED-14"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "target_bin_not_found"


# ---------------------------------------------------------------------------
# 15 400s from Pydantic validation
# ---------------------------------------------------------------------------


def test_patch_400_on_empty_body(client, db):
    item = _seed_item(db, "empty-body")
    _seed_bin_item(db, "B-EMPTY", item, quantity=1.0, confidence=0.1)

    resp = client.patch(f"/bins/B-EMPTY/items/{item}", json={})
    assert resp.status_code == 400, resp.text


def test_patch_400_on_confidence_above_one(client, db):
    item = _seed_item(db, "conf-high")
    _seed_bin_item(db, "B-CHI", item, quantity=1.0, confidence=0.5)

    resp = client.patch(f"/bins/B-CHI/items/{item}", json={"confidence": 1.5})
    assert resp.status_code == 400, resp.text


def test_patch_400_on_quantity_below_zero(client, db):
    item = _seed_item(db, "qty-neg")
    _seed_bin_item(db, "B-QNEG", item, quantity=1.0, confidence=0.5)

    resp = client.patch(f"/bins/B-QNEG/items/{item}", json={"quantity": -1.0})
    assert resp.status_code == 400, resp.text


def test_patch_400_on_unknown_field(client, db):
    item = _seed_item(db, "extra-field")
    _seed_bin_item(db, "B-EXTRA", item, quantity=1.0, confidence=0.5)

    resp = client.patch(
        f"/bins/B-EXTRA/items/{item}",
        json={"quantity": 1.0, "color": "red"},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# 16-17 Concurrent race tests
# ---------------------------------------------------------------------------


def test_patch_concurrent_move_from_same_source_one_wins(app_module, client, db):
    """Two threads move the SAME item from B-src to two different empty targets.

    Expect exactly one 200 (the winner) and one 404 ``item_not_found_in_source_bin``
    (the loser, after its FOR UPDATE blocks and the SELECT returns empty
    post-DELETE). Final row count across all three bins = 1.
    """
    item = _seed_item(db, "race-src")
    _seed_bin_item(db, "B-RACESRC", item, quantity=5.0, confidence=0.5)
    _seed_bin(db, "B-RACE-A")
    _seed_bin(db, "B-RACE-B")
    db.commit()

    # 2-party barrier: both threads block at the barrier until both have
    # arrived, then both proceed together. This proves both threads
    # entered the handler concurrently (not serialized by Python-side
    # client construction).
    barrier = threading.Barrier(2)

    def _do_move(target: str) -> tuple[int, dict]:
        c = TestClient(app_module.app)
        c.headers["X-API-Key"] = client.headers["X-API-Key"]
        barrier.wait(timeout=5.0)
        r = c.patch(f"/bins/B-RACESRC/items/{item}", json={"bin_id": target})
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(_do_move, "B-RACE-A")
        fb = pool.submit(_do_move, "B-RACE-B")
        results = [fa.result(timeout=10.0), fb.result(timeout=10.0)]

    status_codes = sorted(r[0] for r in results)
    assert status_codes == [200, 404], results
    loser = next(r for r in results if r[0] == 404)
    assert loser[1]["error"]["code"] == "item_not_found_in_source_bin"

    total = db.execute(
        text(
            "SELECT COUNT(*) FROM bin_items "
            "WHERE item_id = :i AND bin_id IN ('B-RACESRC', 'B-RACE-A', 'B-RACE-B')"
        ),
        {"i": item},
    ).scalar_one()
    assert total == 1, "Exactly one surviving association after the race"


def test_patch_concurrent_moves_into_same_target_yield_one_winner(app_module, client, db):
    """Two threads move the same item from DIFFERENT source bins into the
    SAME target. One wins; the other gets either 404 (source lost its row
    first) or 409 (target unique-violation). Both are valid outcomes of
    the race — assert the disjunction."""
    item = _seed_item(db, "race-tgt")
    _seed_bin_item(db, "B-RACESRC-A", item, quantity=1.0, confidence=0.1)
    _seed_bin_item(db, "B-RACESRC-B", item, quantity=2.0, confidence=0.2)
    _seed_bin(db, "B-RACETGT")
    db.commit()

    barrier = threading.Barrier(2)

    def _do_move(source: str) -> tuple[int, dict]:
        c = TestClient(app_module.app)
        c.headers["X-API-Key"] = client.headers["X-API-Key"]
        barrier.wait(timeout=5.0)
        r = c.patch(f"/bins/{source}/items/{item}", json={"bin_id": "B-RACETGT"})
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(_do_move, "B-RACESRC-A")
        fb = pool.submit(_do_move, "B-RACESRC-B")
        results = [fa.result(timeout=10.0), fb.result(timeout=10.0)]

    winners = [r for r in results if r[0] == 200]
    losers = [r for r in results if r[0] in (404, 409)]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    loser = losers[0]
    if loser[0] == 404:
        assert loser[1]["error"]["code"] == "item_not_found_in_source_bin"
    else:
        assert loser[1]["error"]["code"] == "target_already_has_item"

    # Target holds exactly one row for the item; at most one source row
    # may persist (if the loser's DELETE rolled back).
    tgt = _bin_item_row(db, "B-RACETGT", item)
    assert tgt is not None
    remaining_sources = db.execute(
        text(
            "SELECT COUNT(*) FROM bin_items "
            "WHERE item_id = :i AND bin_id IN ('B-RACESRC-A', 'B-RACESRC-B')"
        ),
        {"i": item},
    ).scalar_one()
    assert remaining_sources <= 1


# ---------------------------------------------------------------------------
# ApiDev2_014 polish
# ---------------------------------------------------------------------------


# SEC-40-2: quantity upper bound + inf/NaN rejection ------------------------


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("raw_value", "label"),
    [
        # inf/NaN/-inf must be sent as raw JSON content because the TestClient's
        # json= kwarg hard-rejects them client-side; real-world misbehaving
        # clients send them anyway, so the server guard is what matters.
        ("Infinity", "positive_infinity"),
        ("-Infinity", "negative_infinity"),
        ("NaN", "nan"),
        ("1e20", "over_upper_bound_1e20"),
        ("1000000001", "over_upper_bound_1e9_plus_1"),
        ("-1.0", "negative"),
    ],
)
def test_patch_400_on_quantity_out_of_bounds(client, db, raw_value, label):
    """SEC-40-2 (ApiDev2_014): quantity must be finite and <= 1e9.
    Prior bound was only ``ge=0`` — inf/NaN/astronomical values slipped
    through to downstream JSON renderers and iOS formatters."""
    item = _seed_item(db, f"qty-bound-{label}")
    bin_id = f"B-QB-{label[:8].upper()}"
    _seed_bin_item(db, bin_id, item, quantity=1.0, confidence=0.5)

    body = f'{{"quantity": {raw_value}}}'
    resp = client.patch(
        f"/bins/{bin_id}/items/{item}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400, (label, resp.text)


@pytest.mark.parametrize(
    ("raw_value", "label"),
    [
        ("Infinity", "positive_infinity"),
        ("NaN", "nan"),
    ],
)
def test_patch_400_on_confidence_inf_or_nan(client, db, raw_value, label):
    """SEC-40-2 (ApiDev2_014): confidence now also rejects inf/NaN
    explicitly via ``allow_inf_nan=False``. Prior ge/le bounds might
    behave inconsistently across Pydantic versions on these specials;
    the explicit guard is the documented contract."""
    item = _seed_item(db, f"conf-finite-{label}")
    bin_id = f"B-CF-{label[:8].upper()}"
    _seed_bin_item(db, bin_id, item, quantity=1.0, confidence=0.5)

    body = f'{{"confidence": {raw_value}}}'
    resp = client.patch(
        f"/bins/{bin_id}/items/{item}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400, (label, resp.text)


# QA-40-O-5: same-bin PATCH WITH override applies in place -----------------


def test_patch_body_bin_id_equals_path_with_override_applies_inplace(client, db):
    """QA-40-O-5 (ApiDev2_014): same-bin PATCH with explicit overrides
    must apply the overrides (moved: false, DB row updated). Previously
    only the no-override case was covered — the override branch was
    untested."""
    item = _seed_item(db, "same-bin-override")
    _seed_bin_item(db, "B-SAMEBIN-OV", item, quantity=2.0, confidence=0.3)

    resp = client.patch(
        f"/bins/B-SAMEBIN-OV/items/{item}",
        json={"bin_id": "B-SAMEBIN-OV", "quantity": 99.0, "confidence": 0.95},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bin_id"] == "B-SAMEBIN-OV"
    assert body["moved"] is False
    assert body["quantity"] == 99.0
    assert body["confidence"] == 0.95

    row = _bin_item_row(db, "B-SAMEBIN-OV", item)
    assert row is not None
    assert row["quantity"] == 99.0
    assert row["confidence"] == 0.95


# QA-40-O-3: missing-row post-update returns 500 envelope ------------------


def test_patch_500_envelope_when_row_disappears_post_update(client, db, monkeypatch):
    """QA-40-O-3 (ApiDev2_014): if a concurrent DELETE removes the row
    between our UPDATE and the re-read, the handler must raise a clean
    500 envelope (not an ``AttributeError`` leak from a stripped assert
    under ``python -O``). Monkeypatch ``get_bin_item`` to return None
    after a successful update_bin_item — simulates the race."""
    from app.db import repository as real_repo

    item = _seed_item(db, "race-disappear")
    _seed_bin_item(db, "B-GONE", item, quantity=1.0, confidence=0.5)

    original_get = real_repo.get_bin_item

    def flaky_get(db_, bin_id, item_id):
        if bin_id == "B-GONE":
            return None
        return original_get(db_, bin_id, item_id)

    monkeypatch.setattr(real_repo, "get_bin_item", flaky_get)

    resp = client.patch(f"/bins/B-GONE/items/{item}", json={"quantity": 5.0})
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["error"]["code"] == "internal_error"
