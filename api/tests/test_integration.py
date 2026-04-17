import pytest
from sqlalchemy import text


def test_create_item_twice_idempotent(client, db):
    payload = {"name": "M3 socket head cap screw 12mm", "category": "fastener"}

    r1 = client.post("/items", json=payload)
    assert r1.status_code == 200
    item_id_1 = r1.json()["item_id"]

    r2 = client.post("/items", json=payload)
    assert r2.status_code == 200
    item_id_2 = r2.json()["item_id"]

    assert item_id_1 == item_id_2

    count = db.execute(text("SELECT COUNT(*) FROM items")).scalar_one()
    assert count == 1


def test_bins_endpoints(client):
    bin_id = "BIN-TEST-0001"
    r_item = client.post(
        "/items",
        json={"name": "Test Widget", "category": "widget", "bin_id": bin_id, "quantity": 2},
    )
    assert r_item.status_code == 200

    r_bin = client.get(f"/bins/{bin_id}")
    assert r_bin.status_code == 200
    body = r_bin.json()
    assert body["bin_id"] == bin_id
    assert isinstance(body["items"], list)
    assert isinstance(body["photos"], list)

    r_bins = client.get("/bins")
    assert r_bins.status_code == 200
    bins_body = r_bins.json()
    assert bins_body["version"] == "1"
    assert isinstance(bins_body["bins"], list)


def test_error_shape_on_missing_bin(client):
    r = client.get("/bins/DOES-NOT-EXIST")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"]


def test_photo_suggest_shape(client, db, valid_jpeg_bytes):
    bin_id = "BIN-PHOTO-0001"
    r = client.post(
        "/items",
        json={"name": "Photo Item", "category": "widget", "bin_id": bin_id},
    )
    assert r.status_code == 200

    r_ingest = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r_ingest.status_code == 200
    body = r_ingest.json()
    assert body["bin_id"] == bin_id
    assert isinstance(body["photos"], list)
    assert body["photos"]
    photo_id = body["photos"][0]["photo_id"]

    resp = client.get(f"/photos/{photo_id}/suggest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["photo_id"] == photo_id
    assert isinstance(body["suggestions"], list)
    assert body["suggestions"] == sorted(
        body["suggestions"],
        key=lambda s: (-s.get("confidence", 0.0), s.get("name", "")),
    )


def test_photo_suggest_missing(client):
    resp = client.get("/photos/999999/suggest")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"


def test_suggest_passes_bbox_through_from_vision(client, monkeypatch, valid_jpeg_bytes):
    bin_id = "BIN-BBOX-0001"
    r_ingest = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r_ingest.status_code == 200
    photo_id = r_ingest.json()["photos"][0]["photo_id"]

    monkeypatch.setattr(
        "app.routes.photos.describe_photo",
        lambda *a, **kw: (
            [{"name": "widget", "category": "tool", "confidence": 0.9,
              "bbox": [0.1, 0.2, 0.8, 0.9]}],
            42,
        ),
    )
    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    body = r.json()
    assert body["suggestions"][0]["bbox"] == [0.1, 0.2, 0.8, 0.9]


def test_suggest_bbox_absent_is_null(client, monkeypatch, valid_jpeg_bytes):
    # Back-compat: when vision does not return bbox (older models),
    # the field must be present in the response and set to null.
    bin_id = "BIN-BBOX-0002"
    r_ingest = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r_ingest.status_code == 200
    photo_id = r_ingest.json()["photos"][0]["photo_id"]

    monkeypatch.setattr(
        "app.routes.photos.describe_photo",
        lambda *a, **kw: (
            [{"name": "widget", "category": "tool", "confidence": 0.9}],
            42,
        ),
    )
    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    body = r.json()
    assert "bbox" in body["suggestions"][0]
    assert body["suggestions"][0]["bbox"] is None


# ---------------------------------------------------------------------------
# Dev2_012: /suggest progress heartbeat (Finding #18).
# Design: thoughts/shared/designs/suggest-heartbeat-2026-04-17.md (iOS repo).
# ---------------------------------------------------------------------------


def _reset_suggest_tracker():
    from app.services.suggest_tracker import get_tracker
    t = get_tracker()
    with t._lock:
        t._jobs.clear()


def test_suggest_status_unknown_photo_returns_404(client):
    _reset_suggest_tracker()
    r = client.get("/photos/987654/suggest/status")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_suggest_status_after_success_is_done_final(client, monkeypatch, valid_jpeg_bytes):
    _reset_suggest_tracker()
    r_ingest = client.post(
        "/ingest",
        data={"bin_id": "BIN-STATUS-0001"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = r_ingest.json()["photos"][0]["photo_id"]

    monkeypatch.setattr(
        "app.routes.photos.describe_photo",
        lambda *a, **kw: ([{"name": "widget", "category": "tool", "confidence": 0.9}], 12),
    )
    assert client.get(f"/photos/{photo_id}/suggest").status_code == 200

    r = client.get(f"/photos/{photo_id}/suggest/status")
    assert r.status_code == 200
    body = r.json()
    assert body["photo_id"] == photo_id
    assert body["state"] == "done"
    assert body["stage"] == "final"
    assert body["error_code"] is None
    assert body["elapsed_ms"] >= 0
    assert body["started_at"].endswith("Z")


def test_suggest_status_during_vision_is_running_vision(client, monkeypatch, valid_jpeg_bytes):
    """Status polled from inside describe_photo must show state=running, stage=vision."""
    _reset_suggest_tracker()
    r_ingest = client.post(
        "/ingest",
        data={"bin_id": "BIN-STATUS-0002"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = r_ingest.json()["photos"][0]["photo_id"]

    from app.services.suggest_tracker import get_tracker
    captured: dict = {}

    def fake_describe(*a, **kw):
        entry = get_tracker().get(photo_id)
        captured["state"] = entry.state
        captured["stage"] = entry.stage
        return ([{"name": "widget", "confidence": 0.9}], 0)

    monkeypatch.setattr("app.routes.photos.describe_photo", fake_describe)
    assert client.get(f"/photos/{photo_id}/suggest").status_code == 200

    assert captured == {"state": "running", "stage": "vision"}


def test_suggest_status_after_vision_failure_is_failed(client, monkeypatch, valid_jpeg_bytes):
    _reset_suggest_tracker()
    r_ingest = client.post(
        "/ingest",
        data={"bin_id": "BIN-STATUS-0003"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = r_ingest.json()["photos"][0]["photo_id"]

    def boom(*a, **kw):
        raise RuntimeError("fireworks exploded")

    monkeypatch.setattr("app.routes.photos.describe_photo", boom)
    with pytest.raises(RuntimeError):
        client.get(f"/photos/{photo_id}/suggest")

    r = client.get(f"/photos/{photo_id}/suggest/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert body["error_code"] == "runtimeerror"


def test_suggest_status_elapsed_ms_monotonic_between_polls(client, monkeypatch, valid_jpeg_bytes):
    _reset_suggest_tracker()
    r_ingest = client.post(
        "/ingest",
        data={"bin_id": "BIN-STATUS-0004"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = r_ingest.json()["photos"][0]["photo_id"]

    from app.services.suggest_tracker import get_tracker
    get_tracker().start(photo_id)  # simulate an in-flight job, no terminal yet

    import time as _time
    first = client.get(f"/photos/{photo_id}/suggest/status").json()["elapsed_ms"]
    _time.sleep(0.02)
    second = client.get(f"/photos/{photo_id}/suggest/status").json()["elapsed_ms"]
    assert second >= first


def test_ingest_multiple_photos_returns_ids(client, valid_jpeg_bytes):
    bin_id = "BIN-INGEST-0001"
    files = [
        ("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg")),
        ("photos", ("b.jpg", valid_jpeg_bytes, "image/jpeg")),
        ("photos", ("c.jpg", valid_jpeg_bytes, "image/jpeg")),
    ]
    resp = client.post("/ingest", data={"bin_id": bin_id}, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["bin_id"] == bin_id
    assert isinstance(body["photos"], list)
    assert len(body["photos"]) == 3
    for entry in body["photos"]:
        assert "photo_id" in entry
        assert "path" not in entry  # F-10: filesystem paths must not be disclosed


def test_photo_detect_shape(client, valid_jpeg_bytes):
    bin_id = "BIN-DETECT-0001"
    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert resp.status_code == 200
    photo_id = resp.json()["photos"][0]["photo_id"]

    r_detect = client.post(f"/photos/{photo_id}/detect")
    assert r_detect.status_code == 200
    body = r_detect.json()
    assert body["photo_id"] == photo_id
    assert body["model"] == "stub"
    assert isinstance(body["detections"], list)


def test_photo_detect_missing(client):
    resp = client.post("/photos/999999/detect")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"


def test_photo_groups(client, db, valid_jpeg_bytes):
    bin_id = "BIN-GROUP-0001"
    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert resp.status_code == 200
    photo_id = resp.json()["photos"][0]["photo_id"]

    r_groups = client.get(f"/photos/{photo_id}/groups")
    assert r_groups.status_code == 200
    body = r_groups.json()
    assert body["photo_id"] == photo_id
    assert body["model"] == "stub"
    groups = body["groups"]
    assert isinstance(groups, list)
    assert groups == []

    db.execute(
        text(
            """
            INSERT INTO photo_detections (photo_id, model, label, category, confidence, x1, y1, x2, y2)
            VALUES
              (:photo_id, 'stub', 'M3 screw', 'fastener', 0.9, 0.1, 0.1, 0.2, 0.2),
              (:photo_id, 'stub', 'M3 screw', 'fastener', 0.8, 0.2, 0.2, 0.3, 0.3),
              (:photo_id, 'stub', 'M4 screw', 'fastener', 0.95, 0.3, 0.3, 0.4, 0.4),
              (:photo_id, 'stub', 'washer', 'fastener', 0.5, 0.4, 0.4, 0.5, 0.5)
            """
        ),
        {"photo_id": photo_id},
    )
    db.commit()

    r_groups = client.get(f"/photos/{photo_id}/groups")
    assert r_groups.status_code == 200
    groups = r_groups.json()["groups"]
    assert groups[0]["label"] == "M4 screw"
    assert groups[0]["category"] == "fastener"
    assert groups[0]["count_estimate"] == 1
    assert groups[1]["label"] == "M3 screw"
    assert groups[1]["count_estimate"] == 2
    assert groups == sorted(
        groups,
        key=lambda g: (-g["confidence"], g["label"], g.get("category") or ""),
    )

    cached = db.execute(
        text(
            "SELECT COUNT(*) FROM photo_detection_groups WHERE photo_id = :photo_id AND model = 'stub'"
        ),
        {"photo_id": photo_id},
    ).scalar_one()
    assert cached == 3


def test_photo_groups_missing(client):
    resp = client.get("/photos/999999/groups")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"


def test_detection_cascade_delete(client, db, valid_jpeg_bytes):
    bin_id = "BIN-DEL-DET-0001"
    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert resp.status_code == 200
    photo_id = resp.json()["photos"][0]["photo_id"]

    db.execute(
        text(
            """
            INSERT INTO photo_detections (photo_id, model, label, category, confidence, x1, y1, x2, y2)
            VALUES (:photo_id, 'stub', 'M3 screw', 'fastener', 0.9, 0.1, 0.1, 0.2, 0.2)
            """
        ),
        {"photo_id": photo_id},
    )
    db.execute(
        text(
            """
            INSERT INTO photo_detection_groups (photo_id, model, label, category, confidence_avg, count_estimate)
            VALUES (:photo_id, 'stub', 'M3 screw', 'fastener', 0.9, 1)
            """
        ),
        {"photo_id": photo_id},
    )
    db.commit()

    db.execute(text("DELETE FROM photos WHERE photo_id = :photo_id"), {"photo_id": photo_id})
    db.commit()

    det_count = db.execute(
        text("SELECT COUNT(*) FROM photo_detections WHERE photo_id = :photo_id"),
        {"photo_id": photo_id},
    ).scalar_one()
    grp_count = db.execute(
        text("SELECT COUNT(*) FROM photo_detection_groups WHERE photo_id = :photo_id"),
        {"photo_id": photo_id},
    ).scalar_one()
    assert det_count == 0
    assert grp_count == 0


def test_confirm_groups_idempotent(client, db, valid_jpeg_bytes):
    bin_id = "BIN-CONFIRM-0001"
    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert resp.status_code == 200
    photo_id = resp.json()["photos"][0]["photo_id"]

    payload = {
        "version": "1",
        "bin_id": bin_id,
        "selected_groups": [
            {"group_key": "M3 screw|fastener", "label": "M3 screw", "category": "fastener", "quantity": 34},
            {"group_key": "washer|fastener", "label": "washer", "category": "fastener", "quantity": 12},
        ],
    }
    r1 = client.post(f"/photos/{photo_id}/confirm", json=payload)
    assert r1.status_code == 200
    r2 = client.post(f"/photos/{photo_id}/confirm", json=payload)
    assert r2.status_code == 200

    results = r1.json()["results"]
    assert len(results) == 2
    item_ids = [i["item_id"] for i in results]

    count_items = db.execute(
        text("SELECT COUNT(*) FROM items WHERE name IN ('M3 screw','washer')")
    ).scalar_one()
    assert count_items == 2

    count_bin_items = db.execute(
        text("SELECT COUNT(*) FROM bin_items WHERE bin_id = :bin_id"),
        {"bin_id": bin_id},
    ).scalar_one()
    assert count_bin_items == 2

    count_links = db.execute(
        text("SELECT COUNT(*) FROM photo_group_items WHERE photo_id = :photo_id"),
        {"photo_id": photo_id},
    ).scalar_one()
    assert count_links == 2

    stored_item_ids = db.execute(text("SELECT item_id FROM items ORDER BY item_id")).scalars().all()
    assert all(i in stored_item_ids for i in item_ids)


def test_soft_deleted_bin_hidden(client, db):
    bin_id = "BIN-DEL-0001"
    r_item = client.post(
        "/items",
        json={"name": "Soft Delete Item", "category": "widget", "bin_id": bin_id},
    )
    assert r_item.status_code == 200

    db.execute(text("UPDATE bins SET deleted_at = now() WHERE bin_id = :bin_id"), {"bin_id": bin_id})
    db.commit()

    r_bin = client.get(f"/bins/{bin_id}")
    assert r_bin.status_code == 404

    r_bins = client.get("/bins")
    assert r_bins.status_code == 200
    bins = r_bins.json()["bins"]
    assert all(b["bin_id"] != bin_id for b in bins)


def test_soft_deleted_item_hidden_from_search(client, db):
    r_item = client.post(
        "/items",
        json={"name": "Hidden Item", "category": "widget"},
    )
    assert r_item.status_code == 200
    item_id = r_item.json()["item_id"]

    db.execute(text("UPDATE items SET deleted_at = now() WHERE item_id = :item_id"), {"item_id": item_id})
    db.commit()

    r_search = client.get("/search", params={"q": "Hidden Item"})
    assert r_search.status_code == 200
    results = r_search.json()["results"]
    assert all(r["item_id"] != item_id for r in results)


# ---------------------------------------------------------------------------
# UPC tests
# ---------------------------------------------------------------------------

def test_create_item_with_upc(client, db):
    resp = client.post("/items", json={"name": "UPC Widget", "category": "test", "upc": "111111111111"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["upc"] == "111111111111"
    stored = db.execute(text("SELECT upc FROM items WHERE item_id = :id"), {"id": body["item_id"]}).scalar()
    assert stored == "111111111111"


def test_create_item_without_upc_unchanged(client):
    resp = client.post("/items", json={"name": "No UPC Widget", "category": "test"})
    assert resp.status_code == 200
    assert resp.json()["upc"] is None


def test_create_item_invalid_upc_rejected(client):
    resp = client.post("/items", json={"name": "Bad UPC Item", "category": "test", "upc": "notaupc"})
    assert resp.status_code == 400
    assert "invalid UPC" in resp.json()["error"]["message"]


def test_null_upcs_do_not_conflict(client):
    r1 = client.post("/items", json={"name": "No UPC Alpha", "category": "test"})
    r2 = client.post("/items", json={"name": "No UPC Beta", "category": "test"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["item_id"] != r2.json()["item_id"]


def test_upc_preserved_on_fingerprint_conflict(client, db):
    # Insert item with UPC, then re-insert same name+category without UPC.
    # The existing UPC must not be overwritten with NULL.
    r1 = client.post("/items", json={"name": "Stable UPC Item", "category": "test", "upc": "222222222222"})
    assert r1.status_code == 200
    item_id = r1.json()["item_id"]

    r2 = client.post("/items", json={"name": "Stable UPC Item", "category": "test"})
    assert r2.status_code == 200
    assert r2.json()["item_id"] == item_id

    stored = db.execute(text("SELECT upc FROM items WHERE item_id = :id"), {"id": item_id}).scalar()
    assert stored == "222222222222"


def test_upc_lookup_invalid_format(client):
    assert client.get("/upc/abc123").status_code == 400
    assert client.get("/upc/123").status_code == 400
    assert client.get("/upc/12345678901234").status_code == 400


def test_upc_lookup_local_hit(client):
    client.post("/items", json={"name": "Local UPC Item", "category": "test", "upc": "333333333333"})
    resp = client.get("/upc/333333333333")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "local"
    assert body["name"] == "Local UPC Item"
    assert body["upc"] == "333333333333"
    assert body["item_id"] is not None


def test_upc_lookup_unknown_degrades_gracefully(client, monkeypatch):
    import app.services.upc_lookup as upc_mod
    monkeypatch.setattr(upc_mod, "_lookup_upcitemdb", lambda upc: None)
    monkeypatch.setattr(upc_mod, "_lookup_goupc", lambda upc: None)

    resp = client.get("/upc/999999999991")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "unknown"
    assert body["item_id"] is None
    assert body["name"] is None


def test_upc_lookup_second_call_is_local(client, monkeypatch):
    import app.services.upc_lookup as upc_mod
    from app.services.upc_lookup import UPCResult

    calls = []

    def fake_lookup(upc):
        calls.append(upc)
        return UPCResult(name="Cached Product", category="Tools", brand=None, source="upcitemdb")

    monkeypatch.setattr(upc_mod, "_lookup_upcitemdb", fake_lookup)

    resp1 = client.get("/upc/444444444444")
    assert resp1.json()["source"] == "upcitemdb"
    assert len(calls) == 1

    # Second call: item now cached locally, external should not be hit
    resp2 = client.get("/upc/444444444444")
    assert resp2.json()["source"] == "local"
    assert len(calls) == 1  # still 1 — external not called again


def test_add_to_bin_with_upc(client, db):
    bin_id = "BIN-UPC-ADD-001"
    resp = client.post(f"/bins/{bin_id}/add", data={
        "name": "UPC Bin Item", "category": "test", "upc": "555555555555",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["upc"] == "555555555555"
    stored = db.execute(text("SELECT upc FROM items WHERE item_id = :id"), {"id": body["item_id"]}).scalar()
    assert stored == "555555555555"


def test_add_to_bin_reuses_existing_upc(client, db):
    # Create item with UPC via POST /items
    r = client.post("/items", json={"name": "Original Name", "category": "test", "upc": "666666666666"})
    assert r.status_code == 200
    original_id = r.json()["item_id"]

    # Add to bin with same UPC but different name — should reuse original item
    bin_id = "BIN-UPC-REUSE-001"
    resp = client.post(f"/bins/{bin_id}/add", data={
        "name": "Different Name", "category": "other", "upc": "666666666666",
    })
    assert resp.status_code == 200
    assert resp.json()["item_id"] == original_id

    # Only one item with this UPC in DB
    count = db.execute(
        text("SELECT COUNT(*) FROM items WHERE upc = '666666666666'")
    ).scalar_one()
    assert count == 1


def test_bin_items_include_upc(client):
    bin_id = "BIN-UPC-VIEW-001"
    client.post("/items", json={
        "name": "Bin UPC Item", "category": "test", "upc": "777777777777", "bin_id": bin_id,
    })
    resp = client.get(f"/bins/{bin_id}")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 1
    assert "upc" in items[0]
    assert items[0]["upc"] == "777777777777"


def test_search_results_include_upc(client):
    client.post("/items", json={"name": "Searchable UPC Item", "category": "test", "upc": "888888888888"})
    # Use limit=100 — stub embeddings are all identical so ordering is arbitrary
    resp = client.get("/search", params={"q": "Searchable UPC Item", "limit": 100})
    assert resp.status_code == 200
    results = resp.json()["results"]
    match = next((r for r in results if r["name"] == "Searchable UPC Item"), None)
    assert match is not None
    assert match["upc"] == "888888888888"


def test_search_results_include_upc_null_for_items_without(client):
    client.post("/items", json={"name": "No UPC Search Item", "category": "test"})
    resp = client.get("/search", params={"q": "No UPC Search Item", "limit": 100})
    assert resp.status_code == 200
    results = resp.json()["results"]
    match = next((r for r in results if r["name"] == "No UPC Search Item"), None)
    assert match is not None
    assert "upc" in match
    assert match["upc"] is None
