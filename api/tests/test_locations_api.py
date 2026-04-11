"""Integration tests for the /locations API endpoints."""
from __future__ import annotations

import pytest
from sqlalchemy import text


class TestLocationsAPI:
    """Tests for /locations endpoints (require TEST_DATABASE_URL)."""

    def _cleanup(self, db):
        db.execute(text("UPDATE bins SET location_id = NULL"))
        db.execute(text("DELETE FROM locations"))
        db.commit()

    def test_list_locations_empty(self, client, db):
        self._cleanup(db)
        resp = client.get("/locations")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1"
        assert body["locations"] == []

    def test_create_location(self, client, db):
        self._cleanup(db)
        resp = client.post("/locations", data={"name": "Garage"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1"
        assert body["location"]["name"] == "Garage"
        assert body["location"]["location_id"] > 0
        assert body["location"]["description"] is None

    def test_create_location_with_description(self, client, db):
        self._cleanup(db)
        resp = client.post("/locations", data={
            "name": "Workshop",
            "description": "Detached building",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["location"]["description"] == "Detached building"

    def test_create_location_duplicate_409(self, client, db):
        self._cleanup(db)
        client.post("/locations", data={"name": "Kitchen"})
        resp = client.post("/locations", data={"name": "  kitchen  "})
        assert resp.status_code == 409

    def test_create_location_empty_name_400(self, client, db):
        resp = client.post("/locations", data={"name": "   "})
        assert resp.status_code == 400

    def test_list_locations_after_create(self, client, db):
        self._cleanup(db)
        client.post("/locations", data={"name": "Attic"})
        client.post("/locations", data={"name": "Basement"})
        resp = client.get("/locations")
        body = resp.json()
        names = [loc["name"] for loc in body["locations"]]
        assert "Attic" in names
        assert "Basement" in names

    def test_delete_location(self, client, db):
        self._cleanup(db)
        create_resp = client.post("/locations", data={"name": "To Delete"})
        loc_id = create_resp.json()["location"]["location_id"]
        resp = client.delete(f"/locations/{loc_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1"
        assert body["deleted"] is True

        # Verify gone from list
        list_resp = client.get("/locations")
        names = [loc["name"] for loc in list_resp.json()["locations"]]
        assert "To Delete" not in names

    def test_delete_location_not_found(self, client, db):
        resp = client.delete("/locations/99999")
        assert resp.status_code == 404

    def test_delete_location_nulls_bin_reference(self, client, db):
        self._cleanup(db)
        create_resp = client.post("/locations", data={"name": "Old Room"})
        loc_id = create_resp.json()["location"]["location_id"]
        db.execute(text("INSERT INTO bins (bin_id, location_id) VALUES ('BIN-API-DEL', :loc_id)"),
                   {"loc_id": loc_id})
        db.commit()
        client.delete(f"/locations/{loc_id}")
        row = db.execute(text("SELECT location_id FROM bins WHERE bin_id = 'BIN-API-DEL'")).scalar()
        assert row is None
