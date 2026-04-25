"""S-00-AUDIT: Tests for the ``detection_model`` runtime settings accessor.

Validates the canonical S-00-AUDIT contract for the setter (settings + audit
row in one transaction, cache only advances after the DB write commits).
Allowlist-rejection tests for the HTTP route live in
``test_security_f02_model_allowlist.py``.
"""

import pytest
from sqlalchemy import text

# ── Setter: persistence + audit ─────────────────────────────────────────────


def test_setter_writes_db_and_updates_cache(app_module, db):
    from app import deps
    from app.config import DETECTION_MODEL_ALLOWLIST

    first_id = next(iter(DETECTION_MODEL_ALLOWLIST))

    with _cache_override(deps, first_id):
        deps.set_detection_model(first_id, actor_ip="127.0.0.1", actor_key_id="admin-42")

        assert deps.get_detection_model_id() == first_id

        row = db.execute(text("SELECT value FROM settings WHERE key = 'detection_model'")).scalar()
        assert row == first_id


def test_setter_writes_audit_row(app_module, db):
    from app import deps
    from app.config import DETECTION_MODEL_ALLOWLIST

    first_id = next(iter(DETECTION_MODEL_ALLOWLIST))

    db.execute(text("DELETE FROM settings WHERE key = 'detection_model'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'detection_model'"))
    db.commit()

    with _cache_override(deps, first_id):
        deps.set_detection_model(first_id, actor_ip="10.0.0.9", actor_key_id="admin-7")

    row = (
        db.execute(
            text(
                "SELECT setting_key, old_value, new_value, "
                "host(actor_ip) AS ip, actor_key_id "
                "FROM app_settings_audit "
                "WHERE setting_key = 'detection_model' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["setting_key"] == "detection_model"
    assert row["old_value"] is None
    assert row["new_value"] == first_id
    assert row["ip"] == "10.0.0.9"
    assert row["actor_key_id"] == "admin-7"


def test_setter_rejects_unknown_id_with_no_writes(app_module, db):
    from app import deps

    db.execute(text("DELETE FROM settings WHERE key = 'detection_model'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'detection_model'"))
    db.commit()

    with pytest.raises(ValueError):
        deps.set_detection_model("not_in_allowlist", actor_ip="127.0.0.1", actor_key_id="k1")

    settings_row = db.execute(
        text("SELECT value FROM settings WHERE key = 'detection_model'")
    ).scalar()
    assert settings_row is None

    audit_count = db.execute(
        text("SELECT COUNT(*) FROM app_settings_audit WHERE setting_key = 'detection_model'")
    ).scalar()
    assert audit_count == 0


def test_setter_rolls_back_both_writes_on_audit_failure(app_module, db, monkeypatch):
    from app import deps
    from app.config import DETECTION_MODEL_ALLOWLIST
    from app.db import repository

    first_id = next(iter(DETECTION_MODEL_ALLOWLIST))

    db.execute(text("DELETE FROM settings WHERE key = 'detection_model'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'detection_model'"))
    db.commit()

    repository.set_setting(db, "detection_model", first_id)
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(repository, "log_setting_change", boom)

    with _cache_override(deps, first_id):
        with pytest.raises(RuntimeError):
            deps.set_detection_model(first_id, actor_ip="127.0.0.1", actor_key_id="a1")

        current = db.execute(
            text("SELECT value FROM settings WHERE key = 'detection_model'")
        ).scalar()
        assert current == first_id
        assert deps.get_detection_model_id() == first_id


# ── Admin HTTP surface ──────────────────────────────────────────────────────


def test_post_settings_detection_model_writes_audit_row(client, db, app_module):
    from app.config import DETECTION_MODEL_ALLOWLIST

    first_id = next(iter(DETECTION_MODEL_ALLOWLIST))

    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'detection_model'"))
    db.commit()

    resp = client.post("/settings/detection-model", json={"detection_model": first_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "1"
    assert body["detection_model"] == first_id
    assert "previous_detection_model" in body

    row = (
        db.execute(
            text(
                "SELECT new_value, actor_key_id, host(actor_ip) AS ip "
                "FROM app_settings_audit "
                "WHERE setting_key = 'detection_model' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["new_value"] == first_id
    assert row["actor_key_id"] and row["actor_key_id"] != "0"
    assert row["ip"] == "0.0.0.0"


def test_post_settings_detection_model_invalid_writes_nothing(client, db, app_module):
    db.execute(text("DELETE FROM settings WHERE key = 'detection_model'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'detection_model'"))
    db.commit()

    resp = client.post("/settings/detection-model", json={"detection_model": "not_in_allowlist"})
    assert resp.status_code == 400

    settings_row = db.execute(
        text("SELECT value FROM settings WHERE key = 'detection_model'")
    ).scalar()
    assert settings_row is None

    audit_count = db.execute(
        text("SELECT COUNT(*) FROM app_settings_audit WHERE setting_key = 'detection_model'")
    ).scalar()
    assert audit_count == 0


# ── Helpers ─────────────────────────────────────────────────────────────────


class _cache_override:
    """Temporarily clamp ``deps._detection_model_id`` for a test."""

    def __init__(self, deps_module, value: str):
        self._deps = deps_module
        self._new = value
        self._prev: str | None = None

    def __enter__(self):
        self._prev = self._deps._detection_model_id
        self._deps._detection_model_id = self._new
        return self

    def __exit__(self, *_exc):
        self._deps._detection_model_id = self._prev
        return False
