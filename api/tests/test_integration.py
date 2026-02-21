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
    assert isinstance(r_bins.json(), list)


def test_error_shape_on_missing_bin(client):
    r = client.get("/bins/DOES-NOT-EXIST")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == "http_error"
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


def test_photo_suggest_missing(client):
    resp = client.get("/photos/999999/suggest")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "http_error"


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
    assert all(b["bin_id"] != bin_id for b in r_bins.json())


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
