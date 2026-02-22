from fastapi.testclient import TestClient
from sqlalchemy import text


def test_health_db_ok(app_module):
    client = TestClient(app_module.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "1"
    assert body["ok"] is True
    assert body["db_ok"] is True
    assert body["expected_dims"] == 384


def test_health_db_down(app_module):
    client = TestClient(app_module.app)
    original_engine = app_module.engine
    try:
        app_module.engine.dispose()
        app_module.engine = app_module.create_engine("postgresql+psycopg://bad:bad@127.0.0.1:1/bad")
        app_module.SessionLocal.configure(bind=app_module.engine)

        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "service_unavailable"
    finally:
        app_module.engine.dispose()
        app_module.engine = original_engine
        app_module.SessionLocal.configure(bind=app_module.engine)
