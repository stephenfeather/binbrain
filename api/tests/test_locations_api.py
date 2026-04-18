"""Integration tests for the /locations API endpoints."""

from __future__ import annotations

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
        resp = client.post("/locations", json={"name": "Garage"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1"
        assert body["location"]["name"] == "Garage"
        assert body["location"]["location_id"] > 0
        assert body["location"]["description"] is None

    def test_create_location_with_description(self, client, db):
        self._cleanup(db)
        resp = client.post(
            "/locations",
            json={
                "name": "Workshop",
                "description": "Detached building",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["location"]["description"] == "Detached building"

    def test_create_location_duplicate_409(self, client, db):
        self._cleanup(db)
        client.post("/locations", json={"name": "Kitchen"})
        resp = client.post("/locations", json={"name": "  kitchen  "})
        assert resp.status_code == 409

    def test_create_location_empty_name_400(self, client, db):
        resp = client.post("/locations", json={"name": "   "})
        assert resp.status_code == 400

    def test_list_locations_after_create(self, client, db):
        self._cleanup(db)
        client.post("/locations", json={"name": "Attic"})
        client.post("/locations", json={"name": "Basement"})
        resp = client.get("/locations")
        body = resp.json()
        names = [loc["name"] for loc in body["locations"]]
        assert "Attic" in names
        assert "Basement" in names

    def test_delete_location(self, client, db):
        self._cleanup(db)
        create_resp = client.post("/locations", json={"name": "To Delete"})
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
        create_resp = client.post("/locations", json={"name": "Old Room"})
        loc_id = create_resp.json()["location"]["location_id"]
        db.execute(
            text("INSERT INTO bins (bin_id, location_id) VALUES ('BIN-API-DEL', :loc_id)"),
            {"loc_id": loc_id},
        )
        db.commit()
        client.delete(f"/locations/{loc_id}")
        row = db.execute(text("SELECT location_id FROM bins WHERE bin_id = 'BIN-API-DEL'")).scalar()
        assert row is None


class TestBinLocationAPI:
    """Tests for PATCH /bins/{bin_id}/location endpoint."""

    def _cleanup(self, db):
        db.execute(text("UPDATE bins SET location_id = NULL"))
        db.execute(text("DELETE FROM locations"))
        db.execute(text("DELETE FROM bin_items"))
        # FEAT-3: skip the UNASSIGNED sentinel — the protect_unassigned_bin
        # trigger would reject an unscoped DELETE otherwise.
        db.execute(text("DELETE FROM bins WHERE bin_id <> 'UNASSIGNED'"))
        db.commit()

    def test_assign_location_to_bin(self, client, db):
        self._cleanup(db)
        db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-ASSIGN')"))
        db.commit()
        loc_resp = client.post("/locations", json={"name": "Garage"})
        loc_id = loc_resp.json()["location"]["location_id"]

        resp = client.patch("/bins/BIN-ASSIGN/location", json={"location_id": loc_id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1"
        assert body["bin_id"] == "BIN-ASSIGN"
        assert body["location_id"] == loc_id
        assert body["location_name"] == "Garage"

    def test_clear_location_from_bin(self, client, db):
        self._cleanup(db)
        loc_resp = client.post("/locations", json={"name": "Temp"})
        loc_id = loc_resp.json()["location"]["location_id"]
        db.execute(
            text("INSERT INTO bins (bin_id, location_id) VALUES ('BIN-CLEAR', :loc_id)"),
            {"loc_id": loc_id},
        )
        db.commit()

        resp = client.patch("/bins/BIN-CLEAR/location", json={"location_id": None})
        assert resp.status_code == 200
        body = resp.json()
        assert body["location_id"] is None
        assert body["location_name"] is None

    def test_assign_location_bin_not_found(self, client, db):
        resp = client.patch("/bins/NONEXISTENT/location", json={"location_id": 1})
        assert resp.status_code == 404

    def test_assign_location_not_found(self, client, db):
        self._cleanup(db)
        db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-BADLOC')"))
        db.commit()
        resp = client.patch("/bins/BIN-BADLOC/location", json={"location_id": 99999})
        assert resp.status_code == 404
