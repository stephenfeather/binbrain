from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


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
    import app.deps as deps

    client = TestClient(app_module.app)
    original_engine = deps.engine
    try:
        deps.engine.dispose()
        deps.engine = create_engine("postgresql+psycopg://bad:bad@127.0.0.1:1/bad")
        deps.SessionLocal.configure(bind=deps.engine)

        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "service_unavailable"
    finally:
        deps.engine.dispose()
        deps.engine = original_engine
        deps.SessionLocal.configure(bind=deps.engine)
