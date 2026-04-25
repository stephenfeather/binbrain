"""S-00-AUDIT: Tests for the ``max_image_px`` runtime settings accessor.

Validates the canonical S-00-AUDIT contract for the setter (settings + audit
row in one transaction, cache only advances after the DB write commits).
Range-validation tests for the HTTP route live in ``test_settings.py``.
"""

import pytest
from sqlalchemy import text

# ── Setter: validation ─────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, 127, 4097, True, False, "abc", 1.5])
def test_setter_rejects_invalid_values(app_module, bad):
    from app import deps

    with pytest.raises(ValueError):
        deps.set_max_image_px(bad, actor_ip="127.0.0.1", actor_key_id="k1")


# ── Setter: persistence + audit ────────────────────────────────────────────


def test_setter_writes_db_and_updates_cache(app_module, db):
    from app import deps

    with _cache_override(deps, 200):
        deps.set_max_image_px(1024, actor_ip="127.0.0.1", actor_key_id="admin-42")

        assert deps.get_max_image_px() == 1024

        row = db.execute(text("SELECT value FROM settings WHERE key = 'max_image_px'")).scalar()
        assert row == "1024"


def test_setter_writes_audit_row(app_module, db):
    from app import deps

    db.execute(text("DELETE FROM settings WHERE key = 'max_image_px'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'max_image_px'"))
    db.commit()

    with _cache_override(deps, 200):
        deps.set_max_image_px(768, actor_ip="10.0.0.9", actor_key_id="admin-7")

    row = (
        db.execute(
            text(
                "SELECT setting_key, old_value, new_value, "
                "host(actor_ip) AS ip, actor_key_id "
                "FROM app_settings_audit "
                "WHERE setting_key = 'max_image_px' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["setting_key"] == "max_image_px"
    assert row["old_value"] is None
    assert row["new_value"] == "768"
    assert row["ip"] == "10.0.0.9"
    assert row["actor_key_id"] == "admin-7"


def test_setter_rolls_back_both_writes_on_audit_failure(app_module, db, monkeypatch):
    from app import deps
    from app.db import repository

    db.execute(text("DELETE FROM settings WHERE key = 'max_image_px'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'max_image_px'"))
    db.commit()

    repository.set_setting(db, "max_image_px", "640")
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(repository, "log_setting_change", boom)

    with _cache_override(deps, 640):
        with pytest.raises(RuntimeError):
            deps.set_max_image_px(2048, actor_ip="127.0.0.1", actor_key_id="a1")

        current = db.execute(text("SELECT value FROM settings WHERE key = 'max_image_px'")).scalar()
        assert current == "640"
        assert deps.get_max_image_px() == 640


# ── Admin HTTP surface ─────────────────────────────────────────────────────


def test_post_settings_image_size_writes_audit_row(client, db, app_module):
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'max_image_px'"))
    db.commit()

    resp = client.post("/settings/image-size", json={"max_image_px": 1024})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "1"
    assert body["max_image_px"] == 1024
    assert "previous_max_image_px" in body

    row = (
        db.execute(
            text(
                "SELECT new_value, actor_key_id, host(actor_ip) AS ip "
                "FROM app_settings_audit "
                "WHERE setting_key = 'max_image_px' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["new_value"] == "1024"
    assert row["actor_key_id"] and row["actor_key_id"] != "0"
    assert row["ip"] == "0.0.0.0"


def test_post_settings_image_size_validation_writes_nothing(client, db, app_module):
    db.execute(text("DELETE FROM settings WHERE key = 'max_image_px'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'max_image_px'"))
    db.commit()

    resp = client.post("/settings/image-size", json={"max_image_px": 50})
    assert resp.status_code == 400

    settings_row = db.execute(
        text("SELECT value FROM settings WHERE key = 'max_image_px'")
    ).scalar()
    assert settings_row is None

    audit_count = db.execute(
        text("SELECT COUNT(*) FROM app_settings_audit WHERE setting_key = 'max_image_px'")
    ).scalar()
    assert audit_count == 0


# ── Helpers ────────────────────────────────────────────────────────────────


class _cache_override:
    """Temporarily clamp ``deps._max_image_px`` for a test."""

    def __init__(self, deps_module, value: int):
        self._deps = deps_module
        self._new = value
        self._prev: int | None = None

    def __enter__(self):
        self._prev = self._deps._max_image_px
        self._deps._max_image_px = self._new
        return self

    def __exit__(self, *_exc):
        self._deps._max_image_px = self._prev
        return False
