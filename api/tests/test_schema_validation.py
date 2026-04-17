from fastapi.testclient import TestClient
from sqlalchemy import text

from schema_utils import validate_schema


def test_health_schema(app_module):
    client = TestClient(app_module.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    validate_schema("https://binbrain.local/schemas/health.response.json", resp.json())


def test_ingest_schema(client, valid_jpeg_bytes):
    resp = client.post(
        "/ingest",
        data={"bin_id": "BIN-SCHEMA-0001"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert resp.status_code == 200
    validate_schema("https://binbrain.local/schemas/ingest.response.json", resp.json())


def test_items_schema(client):
    resp = client.post(
        "/items",
        json={"name": "Schema Item", "category": "fastener"},
    )
    assert resp.status_code == 200
    validate_schema("https://binbrain.local/schemas/items.post.response.json", resp.json())


def test_bins_schema(client):
    resp = client.get("/bins")
    assert resp.status_code == 200
    validate_schema("https://binbrain.local/schemas/bins.list.response.json", resp.json())


def test_bin_get_schema(client):
    bin_id = "BIN-SCHEMA-GET-0001"
    resp = client.post(
        "/items",
        json={"name": "Schema Bin Item", "category": "fastener", "bin_id": bin_id},
    )
    assert resp.status_code == 200

    r_bin = client.get(f"/bins/{bin_id}")
    assert r_bin.status_code == 200
    validate_schema("https://binbrain.local/schemas/bin.get.response.json", r_bin.json())


def test_photo_suggest_schema(client, valid_jpeg_bytes):
    resp = client.post(
        "/ingest",
        data={"bin_id": "BIN-SCHEMA-SUGGEST-0001"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = resp.json()["photos"][0]["photo_id"]

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200
    validate_schema("https://binbrain.local/schemas/photo.suggest.response.json", r.json())


def test_photo_suggest_status_schema(client, monkeypatch, valid_jpeg_bytes):
    resp = client.post(
        "/ingest",
        data={"bin_id": "BIN-SCHEMA-STATUS-0001"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = resp.json()["photos"][0]["photo_id"]

    monkeypatch.setattr(
        "app.routes.photos.describe_photo",
        lambda *a, **kw: ([{"name": "widget", "confidence": 0.9}], 0),
    )
    assert client.get(f"/photos/{photo_id}/suggest").status_code == 200

    r = client.get(f"/photos/{photo_id}/suggest/status")
    assert r.status_code == 200
    validate_schema("https://binbrain.local/schemas/photo.suggest.status.response.json", r.json())


def test_photo_detect_schema(client, valid_jpeg_bytes):
    resp = client.post(
        "/ingest",
        data={"bin_id": "BIN-SCHEMA-DETECT-0001"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = resp.json()["photos"][0]["photo_id"]

    r = client.post(f"/photos/{photo_id}/detect")
    assert r.status_code == 200
    validate_schema("https://binbrain.local/schemas/photo.detect.response.json", r.json())


def test_photo_groups_schema(client, db, valid_jpeg_bytes):
    resp = client.post(
        "/ingest",
        data={"bin_id": "BIN-SCHEMA-GROUPS-0001"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = resp.json()["photos"][0]["photo_id"]

    # Seed detections so groups are non-empty
    db.execute(
        text(
            """
            INSERT INTO photo_detections (photo_id, model, label, category, confidence, x1, y1, x2, y2)
            VALUES
              (:photo_id, 'stub', 'M3 screw', 'fastener', 0.9, 0.1, 0.1, 0.2, 0.2)
            """
        ),
        {"photo_id": photo_id},
    )
    db.commit()

    r = client.get(f"/photos/{photo_id}/groups")
    assert r.status_code == 200
    validate_schema("https://binbrain.local/schemas/photo.groups.response.json", r.json())


def test_photo_confirm_schema(client, valid_jpeg_bytes):
    resp = client.post(
        "/ingest",
        data={"bin_id": "BIN-SCHEMA-CONFIRM-0001"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    photo_id = resp.json()["photos"][0]["photo_id"]

    payload = {
        "version": "1",
        "bin_id": "BIN-SCHEMA-CONFIRM-0001",
        "selected_groups": [
            {
                "group_key": "M3 screw|fastener",
                "label": "M3 screw",
                "category": "fastener",
                "quantity": 2,
            }
        ],
    }
    r = client.post(f"/photos/{photo_id}/confirm", json=payload)
    assert r.status_code == 200
    validate_schema("https://binbrain.local/schemas/photo.confirm.response.json", r.json())
