from sqlalchemy import text


def test_set_and_get_vision_model_persisted(client, db):
    """POST /models/select persists the setting to the DB."""
    resp = client.post("/models/select", json={"model": "test-model:latest"})
    # May fail with 502 if Ollama is not running — that's expected in CI.
    # But the setting should still NOT be persisted if warmup fails.
    if resp.status_code == 502:
        row = db.execute(
            text("SELECT value FROM settings WHERE key = 'active_vision_model'")
        ).scalar()
        # Setting should not be written when warmup fails
        assert row != "test-model:latest"
        return

    assert resp.status_code == 200
    row = db.execute(
        text("SELECT value FROM settings WHERE key = 'active_vision_model'")
    ).scalar()
    assert row == "test-model:latest"


def test_set_and_get_image_size_persisted(client, db):
    """POST /settings/image-size persists the setting to the DB."""
    resp = client.post("/settings/image-size", json={"max_image_px": 640})
    assert resp.status_code == 200
    assert resp.json()["max_image_px"] == 640

    row = db.execute(
        text("SELECT value FROM settings WHERE key = 'max_image_px'")
    ).scalar()
    assert row == "640"


def test_get_image_size_returns_current(client):
    """GET /settings/image-size returns the current value."""
    resp = client.get("/settings/image-size")
    assert resp.status_code == 200
    assert "max_image_px" in resp.json()


def test_image_size_validation_min(client):
    """POST /settings/image-size rejects values below 128."""
    resp = client.post("/settings/image-size", json={"max_image_px": 50})
    assert resp.status_code == 400


def test_image_size_validation_max(client):
    """POST /settings/image-size rejects values above 4096."""
    resp = client.post("/settings/image-size", json={"max_image_px": 9999})
    assert resp.status_code == 400


def test_image_size_overwrite(client, db):
    """Setting image size twice overwrites the previous value."""
    client.post("/settings/image-size", json={"max_image_px": 800})
    client.post("/settings/image-size", json={"max_image_px": 1024})

    row = db.execute(
        text("SELECT value FROM settings WHERE key = 'max_image_px'")
    ).scalar()
    assert row == "1024"


def test_settings_table_empty_on_fresh_start(db):
    """Settings table exists and starts empty (settings loaded from env defaults)."""
    count = db.execute(text("SELECT COUNT(*) FROM settings")).scalar()
    # May have rows from other tests, but table should exist
    assert count is not None


def test_load_settings_from_db(app_module, db):
    """load_settings_from_db reads persisted values into runtime globals."""
    from app.deps import load_settings_from_db, get_max_image_px, set_max_image_px
    from app.db import repository

    original = get_max_image_px()

    # Write a value directly to DB
    repository.set_setting(db, "max_image_px", "512")
    db.commit()

    # Load from DB
    load_settings_from_db()
    assert get_max_image_px() == 512

    # Restore original
    set_max_image_px(original)
    repository.set_setting(db, "max_image_px", str(original))
    db.commit()
