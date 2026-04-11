"""Tests for location data in bin list/detail responses."""
from __future__ import annotations

import pytest
from sqlalchemy import text


class TestBinsWithLocation:
    """Tests that bin endpoints include location data."""

    def _cleanup(self, db):
        db.execute(text("DELETE FROM bin_items"))
        db.execute(text("DELETE FROM photos"))
        db.execute(text("DELETE FROM bins"))
        db.execute(text("DELETE FROM locations"))
        db.commit()

    def test_list_bins_includes_location(self, client, db):
        self._cleanup(db)
        loc_resp = client.post("/locations", data={"name": "Garage"})
        loc_id = loc_resp.json()["location"]["location_id"]
        db.execute(
            text("INSERT INTO bins (bin_id, location_id) VALUES ('BIN-WITH-LOC', :loc_id)"),
            {"loc_id": loc_id},
        )
        db.commit()

        resp = client.get("/bins")
        assert resp.status_code == 200
        bins = resp.json()["bins"]
        matched = [b for b in bins if b["bin_id"] == "BIN-WITH-LOC"]
        assert len(matched) == 1
        assert matched[0]["location_id"] == loc_id
        assert matched[0]["location_name"] == "Garage"

    def test_list_bins_null_location(self, client, db):
        self._cleanup(db)
        db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-NO-LOC')"))
        db.commit()

        resp = client.get("/bins")
        bins = resp.json()["bins"]
        matched = [b for b in bins if b["bin_id"] == "BIN-NO-LOC"]
        assert len(matched) == 1
        assert matched[0]["location_id"] is None
        assert matched[0]["location_name"] is None

    def test_get_bin_includes_location(self, client, db):
        self._cleanup(db)
        loc_resp = client.post("/locations", data={"name": "Kitchen"})
        loc_id = loc_resp.json()["location"]["location_id"]
        db.execute(
            text("INSERT INTO bins (bin_id, location_id) VALUES ('BIN-DETAIL-LOC', :loc_id)"),
            {"loc_id": loc_id},
        )
        db.commit()

        resp = client.get("/bins/BIN-DETAIL-LOC")
        assert resp.status_code == 200
        body = resp.json()
        assert body["location_id"] == loc_id
        assert body["location_name"] == "Kitchen"

    def test_get_bin_null_location(self, client, db):
        self._cleanup(db)
        db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-DETAIL-NOLOC')"))
        db.commit()

        resp = client.get("/bins/BIN-DETAIL-NOLOC")
        assert resp.status_code == 200
        body = resp.json()
        assert body["location_id"] is None
        assert body["location_name"] is None
