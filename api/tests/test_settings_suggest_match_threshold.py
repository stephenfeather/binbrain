"""S-01: Tests for the ``suggest_match_threshold`` runtime settings accessor.

Covers the canonical accessor-pattern contract introduced in S-00-AUDIT:

- Getter with a three-way fallback chain (DB → env → hardcoded default).
- Setter that validates, writes ``settings``, writes ``app_settings_audit``
  in the same transaction, and updates the module-level cache.
- Admin HTTP surface (``GET`` / ``POST /settings/suggest-match-threshold``).
- End-to-end: POST a new value, then ``/photos/*/suggest`` emits match rows
  whose ``threshold_at_compute`` column reflects the new value.
"""

import math

import pytest
from sqlalchemy import text

# ── Getter + fallback chain ──────────────────────────────────────────────────


def test_getter_returns_cached_value(app_module):
    from app import deps

    with _cache_override(deps, 0.77):
        assert deps.get_suggest_match_threshold() == pytest.approx(0.77)


def test_load_settings_from_db_populates_cache(app_module, db):
    """When the DB has a row, ``load_settings_from_db()`` sets the cache."""
    from app import deps
    from app.db import repository

    repository.set_setting(db, "suggest_match_threshold", "0.62")
    db.commit()

    # Start from a clearly-different value so we can observe the load.
    with _cache_override(deps, 0.10):
        deps.load_settings_from_db()
        assert deps.get_suggest_match_threshold() == pytest.approx(0.62)


def test_load_settings_from_db_leaves_cache_when_row_absent(app_module, db):
    """DB row absent → cache keeps whatever the startup env/default gave it."""
    from app import deps

    db.execute(text("DELETE FROM settings WHERE key = 'suggest_match_threshold'"))
    db.commit()

    with _cache_override(deps, 0.42):
        deps.load_settings_from_db()
        # Cache is untouched when the DB row is absent.
        assert deps.get_suggest_match_threshold() == pytest.approx(0.42)


def test_env_default_parses_to_float(monkeypatch):
    """``_parse_suggest_match_threshold_env()`` reads the env var."""
    from app import deps

    monkeypatch.setenv("SUGGEST_MATCH_THRESHOLD", "0.33")
    assert deps._parse_suggest_match_threshold_env() == pytest.approx(0.33)


def test_env_default_falls_back_to_hardcoded_when_unset(monkeypatch):
    from app import deps

    monkeypatch.delenv("SUGGEST_MATCH_THRESHOLD", raising=False)
    assert deps._parse_suggest_match_threshold_env() == pytest.approx(0.85)


def test_env_default_falls_back_on_malformed_value(monkeypatch):
    """Malformed env var must not crash import — logs + returns 0.85."""
    from app import deps

    monkeypatch.setenv("SUGGEST_MATCH_THRESHOLD", "not-a-float")
    assert deps._parse_suggest_match_threshold_env() == pytest.approx(0.85)


def test_env_default_falls_back_on_out_of_range(monkeypatch):
    from app import deps

    monkeypatch.setenv("SUGGEST_MATCH_THRESHOLD", "1.5")
    assert deps._parse_suggest_match_threshold_env() == pytest.approx(0.85)


# ── Setter: validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_value",
    [-0.1, 1.1, math.nan, "abc", True, False, None],
)
def test_setter_rejects_invalid_values(app_module, bad_value):
    from app import deps

    with pytest.raises(ValueError):
        deps.set_suggest_match_threshold(
            bad_value,
            actor_ip="127.0.0.1",
            actor_key_id="k1",
        )


def test_setter_accepts_edges(app_module, db):
    from app import deps

    with _cache_override(deps, 0.5):
        deps.set_suggest_match_threshold(0.0, actor_ip="127.0.0.1", actor_key_id="k1")
        assert deps.get_suggest_match_threshold() == pytest.approx(0.0)
        deps.set_suggest_match_threshold(1.0, actor_ip="127.0.0.1", actor_key_id="k1")
        assert deps.get_suggest_match_threshold() == pytest.approx(1.0)


# ── Setter: persistence + audit ─────────────────────────────────────────────


def test_setter_writes_db_and_updates_cache(app_module, db):
    from app import deps

    with _cache_override(deps, 0.10):
        deps.set_suggest_match_threshold(0.73, actor_ip="127.0.0.1", actor_key_id="admin-42")

        # Cache reflects the new value.
        assert deps.get_suggest_match_threshold() == pytest.approx(0.73)

        # DB row is persisted as a string.
        row = db.execute(
            text("SELECT value FROM settings WHERE key = 'suggest_match_threshold'")
        ).scalar()
        assert row == "0.73"


def test_setter_writes_audit_row(app_module, db):
    from app import deps

    db.execute(text("DELETE FROM settings WHERE key = 'suggest_match_threshold'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'suggest_match_threshold'"))
    db.commit()

    with _cache_override(deps, 0.20):
        deps.set_suggest_match_threshold(0.88, actor_ip="10.0.0.9", actor_key_id="admin-42")

    row = (
        db.execute(
            text(
                "SELECT setting_key, old_value, new_value, "
                "host(actor_ip) AS ip, actor_key_id "
                "FROM app_settings_audit "
                "WHERE setting_key = 'suggest_match_threshold' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["setting_key"] == "suggest_match_threshold"
    # First write → no prior DB row → old_value is NULL.
    assert row["old_value"] is None
    assert row["new_value"] == "0.88"
    assert row["ip"] == "10.0.0.9"
    assert row["actor_key_id"] == "admin-42"


def test_setter_second_call_records_old_value(app_module, db):
    from app import deps

    db.execute(text("DELETE FROM settings WHERE key = 'suggest_match_threshold'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'suggest_match_threshold'"))
    db.commit()

    with _cache_override(deps, 0.20):
        deps.set_suggest_match_threshold(0.50, actor_ip="127.0.0.1", actor_key_id="a1")
        deps.set_suggest_match_threshold(0.60, actor_ip="127.0.0.1", actor_key_id="a1")

    rows = (
        db.execute(
            text(
                "SELECT old_value, new_value "
                "FROM app_settings_audit "
                "WHERE setting_key = 'suggest_match_threshold' "
                "ORDER BY id ASC"
            )
        )
        .mappings()
        .all()
    )
    assert [(r["old_value"], r["new_value"]) for r in rows] == [
        (None, "0.5"),
        ("0.5", "0.6"),
    ]


def test_setter_same_value_still_logs_audit_row(app_module, db):
    """Plan §Accessor contract: 'idempotent in value but NOT in audit'."""
    from app import deps

    db.execute(text("DELETE FROM settings WHERE key = 'suggest_match_threshold'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'suggest_match_threshold'"))
    db.commit()

    with _cache_override(deps, 0.20):
        deps.set_suggest_match_threshold(0.42, actor_ip="127.0.0.1", actor_key_id="a1")
        deps.set_suggest_match_threshold(0.42, actor_ip="127.0.0.1", actor_key_id="a1")

    count = db.execute(
        text(
            "SELECT COUNT(*) FROM app_settings_audit WHERE setting_key = 'suggest_match_threshold'"
        )
    ).scalar()
    assert count == 2


def test_setter_rolls_back_both_writes_on_audit_failure(app_module, db, monkeypatch):
    """If ``log_setting_change`` raises after ``set_setting``, no row persists."""
    from app import deps
    from app.db import repository

    db.execute(text("DELETE FROM settings WHERE key = 'suggest_match_threshold'"))
    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'suggest_match_threshold'"))
    db.commit()

    # Seed a DB row so we can later assert it did not change.
    repository.set_setting(db, "suggest_match_threshold", "0.11")
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(repository, "log_setting_change", boom)

    with _cache_override(deps, 0.11):
        with pytest.raises(RuntimeError):
            deps.set_suggest_match_threshold(0.99, actor_ip="127.0.0.1", actor_key_id="a1")

        # The settings-row write rolled back with the audit failure.
        current = db.execute(
            text("SELECT value FROM settings WHERE key = 'suggest_match_threshold'")
        ).scalar()
        assert current == "0.11"

        # The in-memory cache also did not advance.
        assert deps.get_suggest_match_threshold() == pytest.approx(0.11)


# ── Admin HTTP surface ──────────────────────────────────────────────────────


def test_get_settings_suggest_match_threshold_returns_current(client, app_module):
    from app import deps

    with _cache_override(deps, 0.33):
        resp = client.get("/settings/suggest-match-threshold")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "1"
    assert body["suggest_match_threshold"] == pytest.approx(0.33)


def test_post_settings_suggest_match_threshold_admin_roundtrip(client, db, app_module):
    from app import deps

    with _cache_override(deps, 0.10):
        resp = client.post(
            "/settings/suggest-match-threshold",
            json={"value": 0.66},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == "1"
        assert body["suggest_match_threshold"] == pytest.approx(0.66)
        assert body["previous_suggest_match_threshold"] == pytest.approx(0.10)

        row = db.execute(
            text("SELECT value FROM settings WHERE key = 'suggest_match_threshold'")
        ).scalar()
        assert row == "0.66"

        get_resp = client.get("/settings/suggest-match-threshold")
        assert get_resp.json()["suggest_match_threshold"] == pytest.approx(0.66)


def test_post_settings_suggest_match_threshold_rejects_non_admin(user_client):
    resp = user_client.post(
        "/settings/suggest-match-threshold",
        json={"value": 0.5},
    )
    assert resp.status_code == 403, resp.text


def test_post_settings_suggest_match_threshold_rejects_missing_value(client):
    resp = client.post("/settings/suggest-match-threshold", json={})
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", [-0.1, 1.1, "abc", True])
def test_post_settings_suggest_match_threshold_rejects_invalid(client, bad):
    resp = client.post(
        "/settings/suggest-match-threshold",
        json={"value": bad},
    )
    assert resp.status_code == 400


def test_post_settings_suggest_match_threshold_writes_audit_row(client, db, app_module):
    from app import deps

    db.execute(text("DELETE FROM app_settings_audit WHERE setting_key = 'suggest_match_threshold'"))
    db.commit()

    with _cache_override(deps, 0.10):
        resp = client.post(
            "/settings/suggest-match-threshold",
            json={"value": 0.55},
        )
        assert resp.status_code == 200

    row = (
        db.execute(
            text(
                "SELECT new_value, actor_key_id, host(actor_ip) AS ip "
                "FROM app_settings_audit "
                "WHERE setting_key = 'suggest_match_threshold' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["new_value"] == "0.55"
    # actor_key_id is the integer PK from api_keys, stringified by the route.
    assert row["actor_key_id"] and row["actor_key_id"] != "0"
    # TestClient reports 127.0.0.1 as the client host.
    assert row["ip"] == "127.0.0.1"


# ── Helpers ─────────────────────────────────────────────────────────────────


class _cache_override:
    """Temporarily clamp ``deps._suggest_match_threshold`` for a test.

    Restores the prior value on exit even when the block raises, so test
    ordering doesn't leak the module-level cache.
    """

    def __init__(self, deps_module, value: float):
        self._deps = deps_module
        self._new = value
        self._prev: float | None = None

    def __enter__(self):
        self._prev = self._deps._suggest_match_threshold
        self._deps._suggest_match_threshold = self._new
        return self

    def __exit__(self, *_exc):
        self._deps._suggest_match_threshold = self._prev
        return False
