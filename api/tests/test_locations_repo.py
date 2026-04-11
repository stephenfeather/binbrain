"""Unit tests for location repository functions."""
from __future__ import annotations

import pytest
from sqlalchemy import text


class TestLocationRepository:
    """Tests for location repository functions (require TEST_DATABASE_URL)."""

    def _cleanup(self, db):
        db.execute(text("UPDATE bins SET location_id = NULL"))
        db.execute(text("DELETE FROM locations"))
        db.commit()

    def test_list_locations_empty(self, db):
        from app.db import repository
        self._cleanup(db)
        result = repository.list_locations(db)
        assert result == []

    def test_create_location_returns_dict(self, db):
        from app.db import repository
        self._cleanup(db)
        loc = repository.create_location(db, "Garage", None)
        db.commit()
        assert loc is not None
        assert loc["name"] == "Garage"
        assert loc["location_id"] > 0
        assert loc["description"] is None

    def test_create_location_with_description(self, db):
        from app.db import repository
        self._cleanup(db)
        loc = repository.create_location(db, "Workshop", "Detached building out back")
        db.commit()
        assert loc["description"] == "Detached building out back"

    def test_create_location_duplicate_returns_none(self, db):
        from app.db import repository
        self._cleanup(db)
        repository.create_location(db, "Kitchen", None)
        db.commit()
        dup = repository.create_location(db, "  kitchen  ", None)
        db.commit()
        assert dup is None

    def test_list_locations_returns_active_only(self, db):
        from app.db import repository
        self._cleanup(db)
        repository.create_location(db, "Attic", None)
        loc2 = repository.create_location(db, "Basement", "Below ground")
        db.commit()
        repository.soft_delete_location(db, loc2["location_id"])
        db.commit()
        result = repository.list_locations(db)
        names = [r["name"] for r in result]
        assert "Attic" in names
        assert "Basement" not in names

    def test_list_locations_ordered_by_name(self, db):
        from app.db import repository
        self._cleanup(db)
        repository.create_location(db, "Zulu Room", None)
        repository.create_location(db, "Alpha Room", None)
        db.commit()
        result = repository.list_locations(db)
        names = [r["name"] for r in result]
        assert names == sorted(names, key=str.lower)

    def test_soft_delete_location(self, db):
        from app.db import repository
        self._cleanup(db)
        loc = repository.create_location(db, "Shed", None)
        db.commit()
        deleted = repository.soft_delete_location(db, loc["location_id"])
        db.commit()
        assert deleted is True

    def test_soft_delete_location_not_found(self, db):
        from app.db import repository
        self._cleanup(db)
        deleted = repository.soft_delete_location(db, 99999)
        assert deleted is False

    def test_soft_delete_location_nulls_bin_references(self, db):
        from app.db import repository
        self._cleanup(db)
        loc = repository.create_location(db, "Old Room", None)
        db.commit()
        db.execute(text("INSERT INTO bins (bin_id, location_id) VALUES ('BIN-DEL-TEST', :loc_id)"),
                   {"loc_id": loc["location_id"]})
        db.commit()
        repository.soft_delete_location(db, loc["location_id"])
        db.commit()
        row = db.execute(text("SELECT location_id FROM bins WHERE bin_id = 'BIN-DEL-TEST'")).scalar()
        assert row is None

    def test_update_bin_location(self, db):
        from app.db import repository
        self._cleanup(db)
        loc = repository.create_location(db, "New Room", None)
        db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-LOC-TEST') ON CONFLICT DO NOTHING"), {})
        db.commit()
        updated = repository.update_bin_location(db, "BIN-LOC-TEST", loc["location_id"])
        db.commit()
        assert updated is True
        row = db.execute(text("SELECT location_id FROM bins WHERE bin_id = 'BIN-LOC-TEST'")).scalar()
        assert row == loc["location_id"]

    def test_update_bin_location_clear(self, db):
        from app.db import repository
        self._cleanup(db)
        loc = repository.create_location(db, "Temp Room", None)
        db.execute(text("INSERT INTO bins (bin_id, location_id) VALUES ('BIN-CLR-TEST', :loc_id)"),
                   {"loc_id": loc["location_id"]})
        db.commit()
        updated = repository.update_bin_location(db, "BIN-CLR-TEST", None)
        db.commit()
        assert updated is True
        row = db.execute(text("SELECT location_id FROM bins WHERE bin_id = 'BIN-CLR-TEST'")).scalar()
        assert row is None

    def test_update_bin_location_bin_not_found(self, db):
        from app.db import repository
        self._cleanup(db)
        updated = repository.update_bin_location(db, "NONEXISTENT-BIN", None)
        assert updated is False

    def test_get_location_by_id(self, db):
        from app.db import repository
        self._cleanup(db)
        loc = repository.create_location(db, "Closet", "Hall closet")
        db.commit()
        found = repository.get_location(db, loc["location_id"])
        assert found is not None
        assert found["name"] == "Closet"
        assert found["description"] == "Hall closet"

    def test_get_location_by_id_not_found(self, db):
        from app.db import repository
        self._cleanup(db)
        found = repository.get_location(db, 99999)
        assert found is None

    def test_get_location_deleted_returns_none(self, db):
        from app.db import repository
        self._cleanup(db)
        loc = repository.create_location(db, "Gone Room", None)
        db.commit()
        repository.soft_delete_location(db, loc["location_id"])
        db.commit()
        found = repository.get_location(db, loc["location_id"])
        assert found is None
