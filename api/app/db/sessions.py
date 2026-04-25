from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Sessions (ApiDev_008 — Q-session-id explicit boundary)
#
# Server-assigned session lifecycle. All rows are scoped to a single
# api_key_id; cross-owner lookups must return None so the route layer can
# map to 404 without leaking existence (enumeration-safe).
# ---------------------------------------------------------------------------


def _session_row_to_dict(row) -> dict:
    return {
        "session_id": str(row.session_id),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "label": row.label,
        "photo_count": int(row.photo_count),
    }


def count_open_sessions(db: Session, api_key_id: int) -> int:
    """Return the open-session count for ``api_key_id``.

    Kept as a generic read helper (GET /sessions, Settings UI). Do NOT use
    as the pre-check on the ``POST /sessions`` create path — that invites
    the TOCTOU race closed by ``create_session_if_under_cap`` (SEC-35-1 /
    ApiDev_008b). The count+insert MUST be serialized via the advisory lock
    inside ``create_session_if_under_cap``.
    """
    return int(
        db.execute(
            text("SELECT COUNT(*) FROM sessions " "WHERE api_key_id = :k AND ended_at IS NULL"),
            {"k": api_key_id},
        ).scalar_one()
    )


def create_session(db: Session, api_key_id: int, label: str | None) -> dict:
    """Unconditionally insert a session row.

    **Prefer ``create_session_if_under_cap`` for any caller subject to the
    20-open-session cap.** This bare helper does NOT check the cap and does
    NOT hold the advisory lock — a caller pairing it with a separate
    ``count_open_sessions`` read re-introduces the SEC-35-1 TOCTOU that
    ApiDev_008b closed. Retained here for tests and seed fixtures that
    want deterministic inserts without the lock dance (no current
    production callers).
    """
    row = db.execute(
        text(
            "INSERT INTO sessions (api_key_id, label) "
            "VALUES (:k, :label) "
            "RETURNING session_id, started_at, ended_at, label, photo_count"
        ),
        {"k": api_key_id, "label": label},
    ).one()
    return _session_row_to_dict(row)


def create_session_if_under_cap(
    db: Session, api_key_id: int, label: str | None, cap: int
) -> dict | None:
    """Atomic open-session cap enforcement (ApiDev_008b F-1 / SEC-35-1).

    Collapses the prior ``count_open_sessions`` + ``create_session`` pair into
    a single transactional block so two concurrent ``POST /sessions`` from
    the same api_key cannot both pass the pre-check and both land rows.

    Returns None if the cap is already at or above ``cap``; the caller
    raises 429.

    Serialization: under READ COMMITTED, a plain guarded
    ``INSERT ... SELECT WHERE (SELECT COUNT(*)) < cap`` still leaks under
    concurrent bursts because both statements see the same pre-commit
    snapshot (empirically, 30 concurrent POSTs landed 22 rows). To make
    the cap strict we take a transaction-scoped advisory lock keyed on
    a stable hash of ``('session_create', api_key_id)``. Other txns
    holding the same lock block until this txn commits, which serializes
    the count+insert pair per api_key. The lock is released automatically
    at commit/rollback. Cross-api-key POSTs are NOT serialized (different
    lock keys) so throughput is preserved.
    """
    # A single bigint key is required; combine a namespace constant with
    # the api_key_id to avoid collisions with any other advisory locks
    # future code might take.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('session_create'), :k)"),
        {"k": api_key_id},
    )
    row = db.execute(
        text(
            "INSERT INTO sessions (api_key_id, label) "
            "SELECT :k, :label "
            "WHERE ( "
            "    SELECT COUNT(*) FROM sessions "
            "    WHERE api_key_id = :k AND ended_at IS NULL "
            ") < :cap "
            "RETURNING session_id, started_at, ended_at, label, photo_count"
        ),
        {"k": api_key_id, "label": label, "cap": cap},
    ).one_or_none()
    return _session_row_to_dict(row) if row is not None else None


def find_session(db: Session, session_id: str, api_key_id: int) -> dict | None:
    """Ownership-scoped lookup. Returns None if not found OR not caller's."""
    row = db.execute(
        text(
            "SELECT session_id, started_at, ended_at, label, photo_count "
            "FROM sessions "
            "WHERE session_id = CAST(:s AS uuid) AND api_key_id = :k"
        ),
        {"s": session_id, "k": api_key_id},
    ).one_or_none()
    return _session_row_to_dict(row) if row is not None else None


def end_session(db: Session, session_id: str, api_key_id: int) -> dict | str | None:
    """Close a session.

    Returns:
        dict                 on success (row with ended_at populated)
        "already_closed"     if the row exists but ended_at is non-null
        None                 if the row does not exist OR is not caller's
    """
    existing = db.execute(
        text(
            "SELECT ended_at FROM sessions "
            "WHERE session_id = CAST(:s AS uuid) AND api_key_id = :k"
        ),
        {"s": session_id, "k": api_key_id},
    ).one_or_none()
    if existing is None:
        return None
    if existing.ended_at is not None:
        return "already_closed"

    row = db.execute(
        text(
            "UPDATE sessions SET ended_at = now() "
            "WHERE session_id = CAST(:s AS uuid) AND api_key_id = :k "
            "RETURNING session_id, started_at, ended_at, label, photo_count"
        ),
        {"s": session_id, "k": api_key_id},
    ).one()
    return _session_row_to_dict(row)


def list_sessions(
    db: Session,
    api_key_id: int,
    state: str,
    limit: int,
    offset: int,
) -> list[dict]:
    filter_sql = ""
    if state == "open":
        filter_sql = " AND ended_at IS NULL"
    elif state == "closed":
        filter_sql = " AND ended_at IS NOT NULL"
    # state == "all" falls through with no extra filter.

    rows = db.execute(
        text(
            "SELECT session_id, started_at, ended_at, label, photo_count "
            "FROM sessions "
            "WHERE api_key_id = :k" + filter_sql + " ORDER BY started_at DESC "
            "LIMIT :limit OFFSET :offset"
        ),
        {"k": api_key_id, "limit": limit, "offset": offset},
    ).all()
    return [_session_row_to_dict(r) for r in rows]


def validate_session_for_ingest(db: Session, session_id: str, api_key_id: int) -> bool:
    """True iff the session exists, belongs to caller, and is open.

    Returns False (rather than raising) on malformed UUID strings so the route
    layer can map every failure mode to the same `invalid_session` error code
    without leaking a distinction between "not yours", "not found", "closed",
    or "not a UUID".
    """
    try:
        row = db.execute(
            text(
                "SELECT 1 FROM sessions "
                "WHERE session_id = CAST(:s AS uuid) "
                "AND api_key_id = :k "
                "AND ended_at IS NULL"
            ),
            {"s": session_id, "k": api_key_id},
        ).scalar()
    except Exception:
        # Malformed UUID trips DataError from Postgres; collapse to False.
        db.rollback()
        return False
    return bool(row)
