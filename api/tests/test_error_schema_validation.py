from fastapi.testclient import TestClient

from schema_utils import validate_schema


def test_not_found_error_schema(client):
    resp = client.get("/bins/DOES-NOT-EXIST")
    assert resp.status_code == 404
    validate_schema("https://binbrain.local/schemas/error.response.json", resp.json())


def test_bad_request_error_schema(client):
    resp = client.post("/items", data={"name": ""})
    assert resp.status_code == 400
    validate_schema("https://binbrain.local/schemas/error.response.json", resp.json())


def test_service_unavailable_error_schema(app_module):
    client = TestClient(app_module.app)
    original_engine = app_module.engine
    try:
        app_module.engine.dispose()
        app_module.engine = app_module.create_engine("postgresql+psycopg://bad:bad@127.0.0.1:1/bad")
        app_module.SessionLocal.configure(bind=app_module.engine)

        resp = client.get("/health")
        assert resp.status_code == 503
        validate_schema("https://binbrain.local/schemas/error.response.json", resp.json())
    finally:
        app_module.engine.dispose()
        app_module.engine = original_engine
        app_module.SessionLocal.configure(bind=app_module.engine)
