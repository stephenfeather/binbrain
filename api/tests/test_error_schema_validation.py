from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from schema_utils import validate_schema


def test_not_found_error_schema(client):
    resp = client.get("/bins/DOES-NOT-EXIST")
    assert resp.status_code == 404
    validate_schema("https://binbrain.local/schemas/error.response.json", resp.json())


def test_bad_request_error_schema(client):
    resp = client.post("/items", json={"name": ""})
    assert resp.status_code == 400
    validate_schema("https://binbrain.local/schemas/error.response.json", resp.json())


def test_service_unavailable_error_schema(app_module):
    import app.deps as deps

    client = TestClient(app_module.app)
    original_engine = deps.engine
    try:
        deps.engine.dispose()
        deps.engine = create_engine("postgresql+psycopg://bad:bad@127.0.0.1:1/bad")
        deps.SessionLocal.configure(bind=deps.engine)

        resp = client.get("/health")
        assert resp.status_code == 503
        validate_schema("https://binbrain.local/schemas/error.response.json", resp.json())
    finally:
        deps.engine.dispose()
        deps.engine = original_engine
        deps.SessionLocal.configure(bind=deps.engine)
