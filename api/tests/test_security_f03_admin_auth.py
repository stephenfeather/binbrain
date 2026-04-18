"""F-03 (Critical): No admin authorization boundary — RED tests.

Tests FAIL until:
- api_keys table has a `role` column ('user'|'admin')
- auth middleware attaches request.state.api_key_role
- require_admin dependency exists and is applied to /admin/* routes
"""

import hashlib
import secrets

import pytest
from sqlalchemy import text

# ── Fixtures: user key and admin key ─────────────────────────────────────────


@pytest.fixture(scope="module")
def user_api_key(app_module):
    """A 'user'-role key that must NOT access /admin/* routes."""
    from app.deps import SessionLocal

    raw = "bb_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    db = SessionLocal()
    try:
        db.execute(
            text("INSERT INTO api_keys (key_hash, name, role) VALUES (:h, :n, 'user')"),
            {"h": key_hash, "n": "test-user-role"},
        )
        db.commit()
    finally:
        db.close()
    return raw


@pytest.fixture(scope="module")
def admin_api_key(app_module):
    """An 'admin'-role key that CAN access /admin/* routes."""
    from app.deps import SessionLocal

    raw = "bb_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    db = SessionLocal()
    try:
        db.execute(
            text("INSERT INTO api_keys (key_hash, name, role) VALUES (:h, :n, 'admin')"),
            {"h": key_hash, "n": "test-admin-role"},
        )
        db.commit()
    finally:
        db.close()
    return raw


@pytest.fixture()
def user_client(app_module, user_api_key):
    from fastapi.testclient import TestClient

    c = TestClient(app_module.app)
    c.headers["X-API-Key"] = user_api_key
    return c


@pytest.fixture()
def admin_client(app_module, admin_api_key):
    from fastapi.testclient import TestClient

    c = TestClient(app_module.app)
    c.headers["X-API-Key"] = admin_api_key
    return c


# ── Tests: user key blocked from /admin/* ─────────────────────────────────────


def test_user_key_blocked_from_admin_list_keys(user_client):
    """GET /admin/api-keys must return 403 for a user-role key."""
    resp = user_client.get("/admin/api-keys")
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"


def test_user_key_blocked_from_admin_create_key(user_client):
    """POST /admin/api-keys must return 403 for a user-role key."""
    resp = user_client.post("/admin/api-keys", json={"name": "should-fail"})
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"


def test_user_key_blocked_from_detection_model_set(user_client):
    """POST /settings/detection-model must return 403 for a user-role key."""
    from app.deps import DETECTION_MODEL_ALLOWLIST

    first_id = next(iter(DETECTION_MODEL_ALLOWLIST))
    resp = user_client.post(
        "/settings/detection-model",
        json={"detection_model": first_id},
    )
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"


# ── Tests: admin key succeeds on /admin/* ────────────────────────────────────


def test_admin_key_can_list_keys(admin_client):
    """GET /admin/api-keys must succeed for an admin-role key."""
    resp = admin_client.get("/admin/api-keys")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


# ── Tests: user-plane routes still work with user-role key ───────────────────


def test_user_key_can_list_bins(user_client):
    """GET /bins must work for a user-role key."""
    resp = user_client.get("/bins")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_user_key_can_read_health(app_module):
    """GET /health must be public (no key required)."""
    from fastapi.testclient import TestClient

    c = TestClient(app_module.app)
    resp = c.get("/health")
    assert resp.status_code == 200


# ── Tests: schema has role column ────────────────────────────────────────────


def test_api_keys_table_has_role_column(db):
    """api_keys table must have a role column."""
    row = db.execute(text("SELECT role FROM api_keys LIMIT 1")).first()
    # Just checking the column exists (no rows is fine)
    assert row is None or row[0] in ("user", "admin")


def test_role_column_default_is_user(db):
    """Inserting a key without specifying role must default to 'user'."""
    raw = "bb_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    db.execute(
        text("INSERT INTO api_keys (key_hash, name) VALUES (:h, :n)"),
        {"h": key_hash, "n": "default-role-test"},
    )
    row = db.execute(
        text("SELECT role FROM api_keys WHERE key_hash = :h"),
        {"h": key_hash},
    ).first()
    assert row[0] == "user"
    db.rollback()
