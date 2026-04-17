"""Dev2_014: persist /suggest vision hits and serve from cache on re-call.

Covers: persistence, cache hit, ?refresh=true, cached flag, full-round-trip
bbox fidelity.
"""
import pytest
from sqlalchemy import text


def _seed_photo(client, valid_jpeg_bytes, bin_id: str) -> int:
    r = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200
    return r.json()["photos"][0]["photo_id"]


def _fake_describe(hits, elapsed=42):
    """Return a counter + stub describe_photo that captures invocation count."""
    calls = {"n": 0}

    def fn(*a, **kw):
        calls["n"] += 1
        return (hits, elapsed)

    return calls, fn


def test_suggest_persists_vision_hits_to_photo_detections(client, db, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-CACHE-0001")
    hits = [
        {"name": "Widget", "category": "tool", "confidence": 0.9, "bbox": [0.1, 0.2, 0.3, 0.4]},
        {"name": "Gadget", "category": "electronics", "confidence": 0.7, "bbox": [0.5, 0.6, 0.7, 0.8]},
    ]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    rows = db.execute(
        text(
            "SELECT label, category, confidence, x1, y1, x2, y2 FROM photo_detections "
            "WHERE photo_id = :pid ORDER BY label"
        ),
        {"pid": photo_id},
    ).mappings().all()
    assert len(rows) == 2
    # Stored rows carry the exact bbox coords that came back from Fireworks.
    by_label = {row["label"]: row for row in rows}
    assert by_label["Gadget"]["x1"] == 0.5
    assert by_label["Gadget"]["y2"] == 0.8
    assert by_label["Widget"]["confidence"] == pytest.approx(0.9)


def test_suggest_returns_cached_on_second_call(client, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-CACHE-0002")
    hits = [{"name": "Cached", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    calls, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    first = client.get(f"/photos/{photo_id}/suggest").json()
    second = client.get(f"/photos/{photo_id}/suggest").json()

    assert calls["n"] == 1  # Fireworks called exactly once
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["vision_elapsed_ms"] == 0
    assert [s["name"] for s in second["suggestions"]] == [s["name"] for s in first["suggestions"]]


def test_suggest_refresh_true_forces_fresh_call(client, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-CACHE-0003")
    hits_v1 = [{"name": "Old", "category": "tool", "confidence": 0.8, "bbox": [0.0, 0.0, 0.1, 0.1]}]
    hits_v2 = [{"name": "New", "category": "tool", "confidence": 0.9, "bbox": [0.5, 0.5, 0.6, 0.6]}]

    calls = {"n": 0}
    stack = [hits_v1, hits_v2]

    def fn(*a, **kw):
        calls["n"] += 1
        return (stack.pop(0), 42)

    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    first = client.get(f"/photos/{photo_id}/suggest").json()
    refreshed = client.get(f"/photos/{photo_id}/suggest?refresh=true").json()

    assert calls["n"] == 2
    assert first["cached"] is False
    assert refreshed["cached"] is False
    assert [s["name"] for s in refreshed["suggestions"]] == ["New"]


def test_suggest_cached_flag_reflects_source(client, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-CACHE-0004")
    hits = [{"name": "X", "confidence": 0.5, "category": "tool", "bbox": [0.1, 0.1, 0.2, 0.2]}]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    a = client.get(f"/photos/{photo_id}/suggest").json()
    b = client.get(f"/photos/{photo_id}/suggest").json()
    c = client.get(f"/photos/{photo_id}/suggest?refresh=true").json()

    assert a["cached"] is False
    assert b["cached"] is True
    assert c["cached"] is False


def test_suggest_response_bbox_preserved_across_cache_hop(client, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-CACHE-0005")
    hits = [{"name": "Exact", "category": "tool", "confidence": 0.75, "bbox": [0.12, 0.34, 0.56, 0.78]}]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    _ = client.get(f"/photos/{photo_id}/suggest").json()
    cached = client.get(f"/photos/{photo_id}/suggest").json()
    assert cached["cached"] is True
    assert cached["suggestions"][0]["bbox"] == [0.12, 0.34, 0.56, 0.78]
