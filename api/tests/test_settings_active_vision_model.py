"""S-00-AUDIT: Tests for the ``active_vision_model`` runtime settings accessor.

Mirrors ``test_settings_suggest_match_threshold.py``: validates the canonical
S-00-AUDIT contract for the setter (settings + audit row in one transaction,
cache only advances after the DB write commits).
"""

from sqlalchemy import text

# ── Setter: persistence + audit ─────────────────────────────────────────────


def test_setter_writes_db_and_updates_cache(app_module, db):
    from app import deps

    with _cache_override(deps, "prev-model:1"):
        deps.set_active_vision_model(
            "test-vision:7b", actor_ip="127.0.0.1", actor_key_id="admin-42"
        )

        assert deps.get_active_vision_model() == "test-vision:7b"

        row = db.execute(
            text("SELECT value FROM settings WHERE key = 'active_vision_model'")
        ).scalar()
        assert row == "test-vision:7b"


def test_setter_writes_audit_row(app_module, db):
    from app import deps

    db.execute(text("DELETE FROM settings WHERE key = 'active_vision_model'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'active_vision_model'"))
    db.commit()

    with _cache_override(deps, "prev-model:1"):
        deps.set_active_vision_model("vision-x:9b", actor_ip="10.0.0.9", actor_key_id="admin-7")

    row = (
        db.execute(
            text(
                "SELECT setting_key, old_value, new_value, "
                "host(actor_ip) AS ip, actor_key_id "
                "FROM app_settings_audit "
                "WHERE setting_key = 'active_vision_model' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["setting_key"] == "active_vision_model"
    assert row["old_value"] is None  # First write — no prior DB row.
    assert row["new_value"] == "vision-x:9b"
    assert row["ip"] == "10.0.0.9"
    assert row["actor_key_id"] == "admin-7"


def test_setter_rolls_back_both_writes_on_audit_failure(app_module, db, monkeypatch):
    """If ``log_setting_change`` raises, neither the settings row nor the cache advances."""
    from app import deps
    from app.db import repository

    db.execute(text("DELETE FROM settings WHERE key = 'active_vision_model'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'active_vision_model'"))
    db.commit()

    repository.set_setting(db, "active_vision_model", "old-model:1")
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(repository, "log_setting_change", boom)

    import pytest as _pytest

    with _cache_override(deps, "old-model:1"):
        with _pytest.raises(RuntimeError):
            deps.set_active_vision_model("new-model:2", actor_ip="127.0.0.1", actor_key_id="a1")

        # Settings row did not advance.
        current = db.execute(
            text("SELECT value FROM settings WHERE key = 'active_vision_model'")
        ).scalar()
        assert current == "old-model:1"

        # Cache did not advance.
        assert deps.get_active_vision_model() == "old-model:1"


# ── Admin HTTP surface ──────────────────────────────────────────────────────


def test_post_models_select_writes_audit_row(client, db, app_module):
    """End-to-end: POST /models/select writes both settings and audit rows."""
    from app import deps

    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'active_vision_model'"))
    db.commit()

    with _cache_override(deps, "prev-model:1"):
        resp = client.post("/models/select", json={"model": "test-model:latest"})

    if resp.status_code == 502:
        # Ollama unreachable — warmup failed before the setter was called.
        # In that case neither table should have advanced for this test value.
        row = db.execute(
            text("SELECT value FROM settings WHERE key = 'active_vision_model'")
        ).scalar()
        assert row != "test-model:latest"
        return

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "1"
    assert body["active_model"] == "test-model:latest"
    assert body["previous_model"] == "prev-model:1"

    row = (
        db.execute(
            text(
                "SELECT new_value, actor_key_id, host(actor_ip) AS ip "
                "FROM app_settings_audit "
                "WHERE setting_key = 'active_vision_model' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["new_value"] == "test-model:latest"
    assert row["actor_key_id"] and row["actor_key_id"] != "0"
    # TestClient sends client.host="testclient" → normalized to 0.0.0.0.
    assert row["ip"] == "0.0.0.0"


# ── Helpers ─────────────────────────────────────────────────────────────────


class _cache_override:
    """Temporarily clamp ``deps._active_vision_model`` for a test."""

    def __init__(self, deps_module, value: str):
        self._deps = deps_module
        self._new = value
        self._prev: str | None = None

    def __enter__(self):
        self._prev = self._deps._active_vision_model
        self._deps._active_vision_model = self._new
        return self

    def __exit__(self, *_exc):
        self._deps._active_vision_model = self._prev
        return False
