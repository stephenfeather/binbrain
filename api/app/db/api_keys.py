from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import text
from sqlalchemy.orm import Session


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def create_api_key(db: Session, name: str, role: str = "user") -> tuple[str, str]:
    raw_key = "bb_" + secrets.token_urlsafe(32)
    key_hash = hash_api_key(raw_key)
    db.execute(
        text("INSERT INTO api_keys (key_hash, name, role) VALUES (:key_hash, :name, :role)"),
        {"key_hash": key_hash, "name": name, "role": role},
    )
    return key_hash, raw_key


def validate_api_key(db: Session, key_hash: str) -> dict | None:
    # F-03: include role so the auth middleware can attach it to request.state.
    row = (
        db.execute(
            text("SELECT id, name, role, revoked_at FROM api_keys WHERE key_hash = :key_hash"),
            {"key_hash": key_hash},
        )
        .mappings()
        .first()
    )
    if not row:
        return None
    if row["revoked_at"] is not None:
        return None
    return dict(row)


def list_api_keys(db: Session) -> list[dict]:
    rows = (
        db.execute(
            text(
                "SELECT id, name, role, created_at, revoked_at, last_used FROM api_keys ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def revoke_api_key(db: Session, key_id: int) -> bool:
    res = db.execute(
        text(
            "UPDATE api_keys SET revoked_at = now() WHERE id = :key_id AND revoked_at IS NULL RETURNING id"
        ),
        {"key_id": key_id},
    )
    return res.scalar() is not None


def touch_api_key_last_used(db: Session, key_id: int) -> None:
    db.execute(
        text("UPDATE api_keys SET last_used = now() WHERE id = :key_id"),
        {"key_id": key_id},
    )
