"""Tests for the ``app_settings_audit`` infrastructure (S-00-AUDIT).

Scope:
- The ``log_setting_change`` helper lands a correct row.
- ``old_value=None`` is persisted as SQL NULL (first-write case).
- The write participates in the caller's transaction — rolling back the
  caller's session discards the audit row.
- Input validation rejects empty strings and malformed IPs.
- The migration's supporting index exists.
"""

import pytest
from sqlalchemy import text


def test_log_setting_change_inserts_row(app_module, db):
    from app.db import repository

    repository.log_setting_change(
        db,
        key="yolo_world_conf",
        old_value="0.15",
        new_value="0.25",
        actor_ip="127.0.0.1",
        actor_key_id="key-123",
    )
    db.commit()

    row = (
        db.execute(
            text(
                "SELECT setting_key, old_value, new_value, "
                "host(actor_ip) AS ip, actor_key_id "
                "FROM app_settings_audit "
                "WHERE setting_key = 'yolo_world_conf' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["setting_key"] == "yolo_world_conf"
    assert row["old_value"] == "0.15"
    assert row["new_value"] == "0.25"
    assert row["ip"] == "127.0.0.1"
    assert row["actor_key_id"] == "key-123"


def test_log_setting_change_null_old_value_on_first_write(app_module, db):
    from app.db import repository

    repository.log_setting_change(
        db,
        key="fresh_key_never_set",
        old_value=None,
        new_value="first",
        actor_ip="10.0.0.1",
        actor_key_id="admin-001",
    )
    db.commit()

    row = db.execute(
        text(
            "SELECT old_value FROM app_settings_audit "
            "WHERE setting_key = 'fresh_key_never_set' "
            "ORDER BY id DESC LIMIT 1"
        )
    ).first()
    assert row is not None
    assert row[0] is None


def test_log_setting_change_respects_transaction_rollback(app_module, db):
    """If the caller rolls back, the audit row must not persist."""
    from app.db import repository

    repository.log_setting_change(
        db,
        key="rollback_probe",
        old_value=None,
        new_value="will-not-persist",
        actor_ip="127.0.0.1",
        actor_key_id="rb-key",
    )
    db.rollback()

    row = db.execute(
        text("SELECT COUNT(*) FROM app_settings_audit " "WHERE setting_key = 'rollback_probe'")
    ).scalar()
    assert row == 0


def test_log_setting_change_rejects_empty_key(app_module, db):
    from app.db import repository

    with pytest.raises(ValueError):
        repository.log_setting_change(
            db,
            key="",
            old_value=None,
            new_value="x",
            actor_ip="127.0.0.1",
            actor_key_id="k",
        )


def test_log_setting_change_rejects_empty_new_value(app_module, db):
    from app.db import repository

    with pytest.raises(ValueError):
        repository.log_setting_change(
            db,
            key="valid_key",
            old_value=None,
            new_value="",
            actor_ip="127.0.0.1",
            actor_key_id="k",
        )


def test_log_setting_change_rejects_empty_actor_key_id(app_module, db):
    from app.db import repository

    with pytest.raises(ValueError):
        repository.log_setting_change(
            db,
            key="valid_key",
            old_value=None,
            new_value="x",
            actor_ip="127.0.0.1",
            actor_key_id="",
        )


def test_log_setting_change_rejects_malformed_actor_ip(app_module, db):
    from app.db import repository

    with pytest.raises(ValueError):
        repository.log_setting_change(
            db,
            key="valid_key",
            old_value=None,
            new_value="x",
            actor_ip="not-an-ip",
            actor_key_id="k",
        )


def test_log_setting_change_accepts_ipv6(app_module, db):
    from app.db import repository

    repository.log_setting_change(
        db,
        key="ipv6_probe",
        old_value=None,
        new_value="ok",
        actor_ip="2001:db8::1",
        actor_key_id="k6",
    )
    db.commit()

    row = db.execute(
        text(
            "SELECT host(actor_ip) FROM app_settings_audit "
            "WHERE setting_key = 'ipv6_probe' "
            "ORDER BY id DESC LIMIT 1"
        )
    ).scalar()
    assert row is not None
    # Postgres normalizes "2001:db8::1" — compare via ipaddress.
    import ipaddress

    assert ipaddress.ip_address(row) == ipaddress.ip_address("2001:db8::1")


def test_audit_index_exists(app_module, db):
    """Migration creates (setting_key, changed_at DESC) index."""
    row = db.execute(
        text(
            "SELECT 1 FROM pg_indexes "
            "WHERE tablename = 'app_settings_audit' "
            "AND indexname = 'idx_app_settings_audit_key_time'"
        )
    ).scalar()
    assert row == 1
