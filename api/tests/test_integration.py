import pytest
from sqlalchemy import text


def test_create_item_twice_idempotent(client, db):
    payload = {"name": "M3 socket head cap screw 12mm", "category": "fastener"}

    r1 = client.post("/items", data=payload)
    assert r1.status_code == 200
    item_id_1 = r1.json()["item_id"]

    r2 = client.post("/items", data=payload)
    assert r2.status_code == 200
    item_id_2 = r2.json()["item_id"]

    assert item_id_1 == item_id_2

    count = db.execute(text("SELECT COUNT(*) FROM items")).scalar_one()
    assert count == 1


def test_bins_endpoints(client):
    bin_id = "BIN-TEST-0001"
    r_item = client.post(
        "/items",
        data={"name": "Test Widget", "category": "widget", "bin_id": bin_id, "quantity": 2},
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


def test_photo_suggest_shape(client, db):
    bin_id = "BIN-PHOTO-0001"
    r = client.post(
        "/items",
        data={"name": "Photo Item", "category": "widget", "bin_id": bin_id},
    )
    assert r.status_code == 200

    r_ingest = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", b"fake", "image/jpeg")},
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


def test_ingest_multiple_photos_returns_ids(client):
    bin_id = "BIN-INGEST-0001"
    files = [
        ("photos", ("a.jpg", b"aaa", "image/jpeg")),
        ("photos", ("b.jpg", b"bbb", "image/jpeg")),
        ("photos", ("c.jpg", b"ccc", "image/jpeg")),
    ]
    resp = client.post("/ingest", data={"bin_id": bin_id}, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["bin_id"] == bin_id
    assert isinstance(body["photos"], list)
    assert len(body["photos"]) == 3
    for entry in body["photos"]:
        assert "photo_id" in entry
        assert "path" in entry


def test_photo_detect_shape(client):
    bin_id = "BIN-DETECT-0001"
    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", b"fake", "image/jpeg")},
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


def test_photo_groups(client, db):
    bin_id = "BIN-GROUP-0001"
    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", b"fake", "image/jpeg")},
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


def test_detection_cascade_delete(client, db):
    bin_id = "BIN-DEL-DET-0001"
    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", b"fake", "image/jpeg")},
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


def test_confirm_groups_idempotent(client, db):
    bin_id = "BIN-CONFIRM-0001"
    resp = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", b"fake", "image/jpeg")},
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
        data={"name": "Soft Delete Item", "category": "widget", "bin_id": bin_id},
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
        data={"name": "Hidden Item", "category": "widget"},
    )
    assert r_item.status_code == 200
    item_id = r_item.json()["item_id"]

    db.execute(text("UPDATE items SET deleted_at = now() WHERE item_id = :item_id"), {"item_id": item_id})
    db.commit()

    r_search = client.get("/search", params={"q": "Hidden Item"})
    assert r_search.status_code == 200
    results = r_search.json()["results"]
    assert all(r["item_id"] != item_id for r in results)
