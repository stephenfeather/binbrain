from fastapi.testclient import TestClient


def test_health_without_key_omits_auth_fields(app_module):
    c = TestClient(app_module.app)
    resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "auth_ok" not in body
    assert "role" not in body


def test_health_with_invalid_key_reports_auth_false(app_module):
    c = TestClient(app_module.app)
    resp = c.get("/health", headers={"X-API-Key": "bb_not_a_real_key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["auth_ok"] is False
    assert "role" not in body


def test_health_with_admin_key_reports_role(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_ok"] is True
    assert body["role"] == "admin"


def test_health_with_user_key_reports_role(user_client):
    resp = user_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_ok"] is True
    assert body["role"] == "user"
