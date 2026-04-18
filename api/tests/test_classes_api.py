"""Integration tests for the /classes API endpoints."""

from __future__ import annotations

from unittest.mock import patch


class TestClassesAPI:
    """Tests for /classes endpoints (require TEST_DATABASE_URL)."""

    def test_list_classes_empty(self, client, db):
        resp = client.get("/classes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1"
        assert isinstance(body["classes"], list)
        assert body["count"] == len(body["classes"])

    def test_confirm_class(self, client, db):
        resp = client.post(
            "/classes/confirm",
            json={
                "class_name": "Phillips screwdriver",
                "category": "tools",
                "source": "vision_llm",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1"
        assert body["class_name"] == "phillips screwdriver"
        assert body["added"] is True
        assert body["reload_triggered"] is True
        assert body["active_class_count"] >= 1

    def test_confirm_class_duplicate_is_idempotent(self, client, db):
        client.post(
            "/classes/confirm",
            json={
                "class_name": "bolt",
                "category": "fastener",
                "source": "manual",
            },
        )
        resp = client.post(
            "/classes/confirm",
            json={
                "class_name": "Bolt",
                "category": "fastener",
                "source": "manual",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["added"] is False

    def test_confirm_class_empty_name(self, client):
        resp = client.post(
            "/classes/confirm",
            json={
                "class_name": "   ",
                "source": "manual",
            },
        )
        assert resp.status_code == 400

    def test_list_classes_after_confirm(self, client, db):
        client.post(
            "/classes/confirm",
            json={
                "class_name": "capacitor",
                "category": "electronics",
                "source": "vision_llm",
            },
        )
        resp = client.get("/classes")
        body = resp.json()
        names = [c["class_name"] for c in body["classes"]]
        assert "capacitor" in names

    def test_delete_class(self, client, db):
        client.post(
            "/classes/confirm",
            json={
                "class_name": "to_delete",
                "source": "manual",
            },
        )
        resp = client.delete("/classes/to_delete")
        assert resp.status_code == 200
        body = resp.json()
        assert body["removed"] is True

        # Verify it's gone from the list
        resp = client.get("/classes")
        names = [c["class_name"] for c in resp.json()["classes"]]
        assert "to_delete" not in names

    def test_delete_class_not_found(self, client):
        resp = client.delete("/classes/nonexistent_xyz")
        assert resp.status_code == 404

    @patch("app.services.detection.reload_classes")
    def test_reload_endpoint(self, mock_reload, client):
        resp = client.post("/classes/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["reloaded"] is True
        assert "active_class_count" in body
