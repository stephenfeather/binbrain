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
