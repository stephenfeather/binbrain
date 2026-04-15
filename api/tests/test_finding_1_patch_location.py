"""Finding #1: PATCH /locations/{id} — rename + description edit.

Ships the server half so iOS can wire up the location edit screen.
Admin-guarded (matches existing POST / DELETE semantics).
"""
from __future__ import annotations

from sqlalchemy import text


def _cleanup(db):
    db.execute(text("UPDATE bins SET location_id = NULL"))
    db.execute(text("DELETE FROM locations"))
    db.commit()


class TestPatchLocationAPI:
    def test_rename_location(self, client, db):
        _cleanup(db)
        loc_id = client.post("/locations", json={"name": "Garage"}).json()["location"]["location_id"]

        resp = client.patch(f"/locations/{loc_id}", json={"name": "Shop"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == "1"
        assert body["location"]["location_id"] == loc_id
        assert body["location"]["name"] == "Shop"

        # Subsequent GET reflects the new name.
        names = [l["name"] for l in client.get("/locations").json()["locations"]]
        assert "Shop" in names and "Garage" not in names

    def test_update_description(self, client, db):
        _cleanup(db)
        loc_id = client.post("/locations", json={"name": "Shed"}).json()["location"]["location_id"]

        resp = client.patch(f"/locations/{loc_id}", json={"description": "Out back"})
        assert resp.status_code == 200
        assert resp.json()["location"]["description"] == "Out back"
        # Name preserved.
        assert resp.json()["location"]["name"] == "Shed"

    def test_clear_description(self, client, db):
        _cleanup(db)
        loc_id = client.post(
            "/locations",
            json={"name": "A", "description": "was set"},
        ).json()["location"]["location_id"]

        resp = client.patch(f"/locations/{loc_id}", json={"description": None})
        assert resp.status_code == 200
        assert resp.json()["location"]["description"] is None

    def test_empty_patch_is_noop_200(self, client, db):
        """An empty body is a no-op success; returns the current record."""
        _cleanup(db)
        loc_id = client.post("/locations", json={"name": "Unchanged"}).json()["location"]["location_id"]

        resp = client.patch(f"/locations/{loc_id}", json={})
        assert resp.status_code == 200
        assert resp.json()["location"]["name"] == "Unchanged"

    def test_rename_conflict_409(self, client, db):
        _cleanup(db)
        client.post("/locations", json={"name": "Kitchen"})
        other_id = client.post("/locations", json={"name": "Pantry"}).json()["location"]["location_id"]

        resp = client.patch(f"/locations/{other_id}", json={"name": "  kitchen  "})
        assert resp.status_code == 409

    def test_empty_name_400(self, client, db):
        _cleanup(db)
        loc_id = client.post("/locations", json={"name": "Loc"}).json()["location"]["location_id"]
        resp = client.patch(f"/locations/{loc_id}", json={"name": "   "})
        assert resp.status_code == 400

    def test_not_found_404(self, client, db):
        _cleanup(db)
        resp = client.patch("/locations/999999", json={"name": "Nope"})
        assert resp.status_code == 404

    def test_non_admin_key_forbidden(self, client, user_client, db):
        """Matches POST/DELETE: user-role keys get 403."""
        _cleanup(db)
        loc_id = client.post("/locations", json={"name": "Admin-only"}).json()["location"]["location_id"]

        resp = user_client.patch(f"/locations/{loc_id}", json={"name": "Hacked"})
        assert resp.status_code == 403
