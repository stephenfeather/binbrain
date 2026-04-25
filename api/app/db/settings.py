from __future__ import annotations

import ipaddress

from sqlalchemy import text
from sqlalchemy.orm import Session


def get_setting(db: Session, key: str) -> str | None:
    row = db.execute(
        text("SELECT value FROM settings WHERE key = :key"),
        {"key": key},
    ).scalar()
    return row


def set_setting(db: Session, key: str, value: str) -> None:
    db.execute(
        text(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (:key, :value, now())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = now()
            """
        ),
        {"key": key, "value": value},
    )


def log_setting_change(
    db: Session,
    *,
    key: str,
    old_value: str | None,
    new_value: str,
    actor_ip: str,
    actor_key_id: str,
) -> None:
    """Record a settings-store change in ``app_settings_audit``.

    Runs inside the caller's transaction — the caller is responsible for
    commit / rollback. Pairs with :func:`set_setting`: if the enclosing
    transaction rolls back, the audit row is discarded too, preserving the
    invariant that every persisted ``settings`` write has an audit row.

    The ``actor_ip`` is parsed via :mod:`ipaddress` before hitting Postgres
    so malformed input produces a clear :class:`ValueError` instead of an
    opaque ``InvalidTextRepresentation`` from the driver.

    Raises:
        ValueError: if ``key``, ``new_value``, or ``actor_key_id`` is empty,
            or if ``actor_ip`` is not a valid IPv4/IPv6 address.
    """
    if not key:
        raise ValueError("key must be a non-empty string")
    if not new_value:
        raise ValueError("new_value must be a non-empty string")
    if not actor_key_id:
        raise ValueError("actor_key_id must be a non-empty string")
    try:
        ipaddress.ip_address(actor_ip)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"actor_ip is not a valid IP address: {actor_ip!r}") from exc

    db.execute(
        text(
            """
            INSERT INTO app_settings_audit
                (setting_key, old_value, new_value, actor_ip, actor_key_id)
            VALUES
                (:key, :old_value, :new_value, :actor_ip, :actor_key_id)
            """
        ),
        {
            "key": key,
            "old_value": old_value,
            "new_value": new_value,
            "actor_ip": actor_ip,
            "actor_key_id": actor_key_id,
        },
    )
