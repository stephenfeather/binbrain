"""Dev2_018: Vision Ops Instrumentation — Phase 3 of the Data Capture Foundation.

Server-only slice. Covers two new append-only tables:

* ``vision_calls`` — one row per ``/suggest`` invocation (success, cache hit, or
  error) carrying latency, model, prompt_version, and an anomaly ``flags`` bag.
  Subsumes the Dev2_015 iter-2 bbox-anomaly log lines (gap #5) and the
  SuggestTracker state transitions (gap #8).
* ``photo_suggestion_matches`` — one row per embedding-match candidate, with
  ``score`` and ``threshold_at_compute`` captured at invocation time so the
  ``SUGGEST_MATCH_THRESHOLD`` knob becomes tunable from data (gap #2).

Telemetry writes MUST be best-effort: a failure in either writer MUST NOT break
the ``/suggest`` response contract. Telemetry is append-only — unlike the
Dev2_017 outcomes table there is no DELETE-then-INSERT replace pattern; history
matters here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_photo(client, valid_jpeg_bytes, bin_id: str) -> int:
    r = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    return r.json()["photos"][0]["photo_id"]


def _fake_describe(hits, elapsed=42, anomaly_flags: dict | None = None):
    """Stub describe_photo that returns canned hits and optionally sets
    anomaly markers on the route-supplied ``flags_out`` bag.
    """
    anomaly_flags = anomaly_flags or {}
    calls = {"n": 0}

    def fn(*a, **kw):
        calls["n"] += 1
        flags_out = kw.get("flags_out")
        if flags_out is not None:
            flags_out.update(anomaly_flags)
        return (list(hits), elapsed)

    return calls, fn


def _vision_call_rows(db, photo_id: int) -> list[dict]:
    rows = (
        db.execute(
            text(
                "SELECT model, prompt_version, base_url, started_at, elapsed_ms, "
                "hits_count, cached, outcome, error_code, flags "
                "FROM vision_calls WHERE photo_id = :pid ORDER BY id"
            ),
            {"pid": photo_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _match_rows(db, photo_id: int) -> list[dict]:
    rows = (
        db.execute(
            text(
                "SELECT m.photo_detection_id, m.matched_item_id, m.score, "
                "       m.threshold_at_compute "
                "FROM photo_suggestion_matches m "
                "JOIN photo_detections d ON d.id = m.photo_detection_id "
                "WHERE d.photo_id = :pid ORDER BY m.id"
            ),
            {"pid": photo_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _seed_item(db, name: str, category: str = "tool") -> int:
    """Insert an item plus a stub 384-dim embedding. Returns item_id."""
    row = (
        db.execute(
            text(
                "INSERT INTO items (name, category) VALUES (:name, :category) "
                "RETURNING item_id"
            ),
            {"name": name, "category": category},
        )
        .mappings()
        .one()
    )
    item_id = row["item_id"]
    # Matches conftest fastembed stub (384 dims, all 0.1).
    vec_literal = "[" + ",".join(["0.1"] * 384) + "]"
    db.execute(
        text(
            "INSERT INTO item_embeddings (item_id, model, dims, embedding) "
            "VALUES (:item_id, 'stub', 384, CAST(:vec AS vector))"
        ),
        {"item_id": item_id, "vec": vec_literal},
    )
    db.commit()
    return item_id


# ---------------------------------------------------------------------------
# vision_calls: terminal-state coverage
# ---------------------------------------------------------------------------


def test_suggest_writes_vision_call_on_fresh_success(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-VC-0001")
    hits = [
        {"name": "Widget", "category": "tool", "confidence": 0.9, "bbox": [0.1, 0.2, 0.3, 0.4]},
        {"name": "Gadget", "category": "tool", "confidence": 0.7, "bbox": [0.5, 0.6, 0.7, 0.8]},
    ]
    _, fn = _fake_describe(hits, elapsed=77)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text

    rows = _vision_call_rows(db, photo_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "ok"
    assert row["cached"] is False
    assert row["elapsed_ms"] is not None and row["elapsed_ms"] >= 0
    assert row["hits_count"] == 2
    assert row["prompt_version"] == "v2"
    assert row["error_code"] is None
    assert row["model"]  # non-empty


def test_suggest_writes_vision_call_on_cache_hit(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-VC-0002")
    hits = [{"name": "X", "category": "tool", "confidence": 0.5, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    # Prime the cache.
    client.get(f"/photos/{photo_id}/suggest")
    # Second call is a cache hit.
    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text

    rows = _vision_call_rows(db, photo_id)
    assert len(rows) == 2  # append-only: one per invocation
    fresh, cached = rows[0], rows[1]
    assert fresh["cached"] is False
    assert cached["cached"] is True
    assert cached["outcome"] == "ok"
    assert cached["hits_count"] == 1
    assert cached["elapsed_ms"] is not None and cached["elapsed_ms"] >= 0


def test_suggest_writes_vision_call_on_vlm_error(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-VC-0003")

    class BoomError(RuntimeError):
        pass

    def fn(*a, **kw):
        raise BoomError("fireworks down")

    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code >= 500  # existing behavior: exception propagates

    rows = _vision_call_rows(db, photo_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "error"
    assert row["error_code"] is not None and row["error_code"] != ""
    assert row["cached"] is False


def test_suggest_vision_call_captures_whole_image_bbox_anomaly(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-VC-0004")
    hits = [
        {
            "name": "OversizedBox",
            "category": "tool",
            "confidence": 0.6,
            "bbox": [0.0, 0.0, 0.99, 0.99],
        }
    ]
    _, fn = _fake_describe(hits, anomaly_flags={"whole_image_bbox_warn": True})
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text

    rows = _vision_call_rows(db, photo_id)
    assert len(rows) == 1
    assert rows[0]["flags"].get("whole_image_bbox_warn") is True


def test_suggest_vision_call_captures_bbox_normalized_anomaly(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-VC-0005")
    hits = [
        {
            "name": "PixelBox",
            "category": "tool",
            "confidence": 0.6,
            "bbox": [0.1, 0.1, 0.3, 0.3],
        }
    ]
    _, fn = _fake_describe(hits, anomaly_flags={"bbox_normalized": True})
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text

    rows = _vision_call_rows(db, photo_id)
    assert len(rows) == 1
    assert rows[0]["flags"].get("bbox_normalized") is True
    # Suggestions still come back — anomaly is telemetry-only, not a rejection.
    body = r.json()
    assert len(body["suggestions"]) == 1


def test_suggest_telemetry_write_failure_does_not_break_response(
    client, monkeypatch, valid_jpeg_bytes, caplog
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-VC-0006")
    hits = [{"name": "Safe", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    def boom(*a, **kw):
        raise RuntimeError("telemetry DB offline")

    monkeypatch.setattr("app.routes.photos.repository.insert_vision_call", boom)

    with caplog.at_level("WARNING"):
        r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["photo_id"] == photo_id
    assert len(body["suggestions"]) == 1
    # Failure was logged, not raised.
    assert any(
        "vision_call_telemetry_write_failed" in rec.getMessage() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# photo_suggestion_matches: threshold capture
# ---------------------------------------------------------------------------


def test_suggest_writes_match_rows_for_above_threshold_hits(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-M-0001")
    item_id = _seed_item(db, name="Widget", category="tool")
    hits = [
        {"name": "Widget", "category": "tool", "confidence": 0.9, "bbox": [0.1, 0.1, 0.2, 0.2]}
    ]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    # Force score above default threshold (0.85).
    def fake_search(db_, qvec, limit):
        return [
            {
                "item_id": item_id,
                "name": "Widget",
                "category": "tool",
                "upc": None,
                "score": 0.97,
                "bins": [],
            }
        ]

    monkeypatch.setattr(
        "app.routes.photos.repository.search_items_by_embedding", fake_search
    )

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text

    rows = _match_rows(db, photo_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["matched_item_id"] == item_id
    assert row["score"] == pytest.approx(0.97)
    assert row["threshold_at_compute"] == pytest.approx(0.85)


def test_suggest_writes_match_rows_with_null_item_for_below_threshold(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-M-0002")
    item_id = _seed_item(db, name="Sprocket", category="tool")
    hits = [
        {"name": "Sprocket", "category": "tool", "confidence": 0.9, "bbox": [0.1, 0.1, 0.2, 0.2]}
    ]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    # Force score below default threshold (0.85).
    def fake_search(db_, qvec, limit):
        return [
            {
                "item_id": item_id,
                "name": "Sprocket",
                "category": "tool",
                "upc": None,
                "score": 0.42,
                "bins": [],
            }
        ]

    monkeypatch.setattr(
        "app.routes.photos.repository.search_items_by_embedding", fake_search
    )

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text

    rows = _match_rows(db, photo_id)
    assert len(rows) == 1
    row = rows[0]
    # The below-threshold rejection signal is the whole point of this row.
    assert row["matched_item_id"] is None
    assert row["score"] == pytest.approx(0.42)
    assert row["threshold_at_compute"] == pytest.approx(0.85)


def test_suggest_match_rows_carry_threshold_at_compute_time(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-M-0003")
    item_id = _seed_item(db, name="Ratchet", category="tool")
    hits = [
        {"name": "Ratchet", "category": "tool", "confidence": 0.9, "bbox": [0.1, 0.1, 0.2, 0.2]}
    ]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    # score=0.8 is below default 0.85 but above the monkeypatched 0.7 — proves
    # the row carries the live env value, not a stale module-level constant.
    def fake_search(db_, qvec, limit):
        return [
            {
                "item_id": item_id,
                "name": "Ratchet",
                "category": "tool",
                "upc": None,
                "score": 0.8,
                "bins": [],
            }
        ]

    monkeypatch.setattr(
        "app.routes.photos.repository.search_items_by_embedding", fake_search
    )
    monkeypatch.setenv("SUGGEST_MATCH_THRESHOLD", "0.7")

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text

    rows = _match_rows(db, photo_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["threshold_at_compute"] == pytest.approx(0.7)
    # 0.8 >= 0.7 so the match is accepted (matched_item_id populated).
    assert row["matched_item_id"] == item_id


# ---------------------------------------------------------------------------
# Append-only invariant
# ---------------------------------------------------------------------------


def test_vision_calls_are_not_deduped_across_repeated_suggest_calls(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-VC-0007")
    hits = [{"name": "Repeat", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    for _ in range(3):
        r = client.get(f"/photos/{photo_id}/suggest")
        assert r.status_code == 200

    rows = _vision_call_rows(db, photo_id)
    assert len(rows) == 3
    # First row is fresh; subsequent two are cache hits.
    assert [r["cached"] for r in rows] == [False, True, True]
