from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Idempotency-Key support (ApiDev_idempotency_outcomes / SEC-26-3)
#
# Endpoints that opt in call these helpers to gate a domain write behind a
# (api_key_id, Idempotency-Key) lookup. Replays with the SAME raw body hash
# return the stored response; replays with a DIFFERENT body raise
# IdempotencyKeyMismatch → 409. The key/body binding is SEC-26-3: without
# the body-hash gate, an attacker could replay an old outcome body under a
# new key, or submit two different bodies under the same key and have one
# silently dropped.
# ---------------------------------------------------------------------------


IDEMPOTENCY_TTL_INTERVAL = "24 hours"


class IdempotencyKeyMismatch(Exception):
    """Same (api_key_id, key) seen with a different body_sha256 (SEC-26-3).

    Caller must translate this into an HTTP 409 ``idempotency_key_mismatch``
    and MUST NOT run any domain-side mutation. The tamper/client-bug signal
    is logged at WARN by the caller so ops can alert on it.
    """


def hash_canonical_body(raw: bytes) -> bytes:
    """Return the 32-byte SHA-256 of the request body exactly as received.

    Contract: the argument MUST be the raw bytes from ``await request.body()``
    — pre-JSON-parse, post-decompression. Do NOT re-serialize a parsed
    pydantic model and hash the re-serialized output; JSON canonicalization
    varies across libraries and versions (key order, whitespace, unicode
    escaping) and a re-serialized hash silently diverges between the first
    and second attempts of an otherwise-identical logical request. A test
    (``test_hash_is_raw_bytes_not_json_canonicalization``) enforces that a
    future refactor to semantic hashing breaks visibly.
    """
    return hashlib.sha256(raw).digest()


def fetch_idempotent_record(db: Session, api_key_id: int, key: str) -> dict | None:
    """Look up a non-expired idempotency row for ``(api_key_id, key)``.

    Filters on ``created_at >= now() - 24h`` so a stale row (past TTL) is
    invisible to the caller and will be DELETEd on the next store attempt
    by ``store_idempotent_response``. Returns the row as a dict (keys:
    ``body_sha256``, ``response_status``, ``response_body``) or None.
    """
    row = (
        db.execute(
            text(
                "SELECT body_sha256, response_status, response_body "
                "FROM idempotency_records "
                "WHERE api_key_id = :k AND key = :key "
                f"AND created_at >= now() - interval '{IDEMPOTENCY_TTL_INTERVAL}'"
            ),
            {"k": api_key_id, "key": key},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def store_idempotent_response(
    db: Session,
    api_key_id: int,
    key: str,
    body_sha256: bytes,
    response_status: int,
    response_body: dict,
) -> None:
    """Insert or reuse a dedup row for ``(api_key_id, key)`` with the body hash.

    Called in the same transaction as the domain write so a crash between
    the domain commit and the idempotency write cannot produce a duplicate
    on crash-reclaim: both land atomically or neither does.

    Before inserting, this runs a scoped ``DELETE`` for expired rows of the
    **current** api_key_id only — lazy-on-write cleanup (no cron, no sidecar).
    Write amplification is bounded: one extra index scan over this api_key's
    partition per idempotent POST. Revisit if ``idempotency_records`` grows
    past ~100k rows per key.

    Races: two concurrent txns with the same ``(api_key_id, key)`` could both
    reach this function after both miss the pre-check in
    ``fetch_idempotent_record``. ``ON CONFLICT (api_key_id, key) DO NOTHING``
    plus the ``pg_advisory_xact_lock(hashtext('idempotency'), api_key_id)``
    taken by the route handler serializes the pair so exactly one insert
    lands; the loser re-reads via ``fetch_idempotent_record`` after the
    winner commits. The advisory lock is the authoritative serializer — ON
    CONFLICT is the belt-and-suspenders for the no-advisory-lock case.
    """
    # Lazy cleanup scoped to this api_key only; full-table sweeps belong in
    # an admin job, not on a hot write path.
    db.execute(
        text(
            "DELETE FROM idempotency_records "
            "WHERE api_key_id = :k "
            f"AND created_at < now() - interval '{IDEMPOTENCY_TTL_INTERVAL}'"
        ),
        {"k": api_key_id},
    )
    db.execute(
        text(
            "INSERT INTO idempotency_records "
            "(api_key_id, key, body_sha256, response_status, response_body) "
            "VALUES (:k, :key, :hash, :status, CAST(:body AS jsonb)) "
            "ON CONFLICT (api_key_id, key) DO NOTHING"
        ),
        {
            "k": api_key_id,
            "key": key,
            "hash": body_sha256,
            "status": response_status,
            "body": json.dumps(response_body),
        },
    )


def acquire_idempotency_lock(db: Session, api_key_id: int) -> None:
    """Serialize concurrent idempotent POSTs for a single api_key.

    Transaction-scoped advisory lock released on commit/rollback. Per-owner
    so cross-tenant idempotent traffic is NOT serialized — an admin client
    under heavy load cannot stall a user client's idempotent POSTs.

    See the advisory-lock namespace registry at the top of ``repository``;
    this site takes ``('idempotency', api_key_id)``.
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('idempotency'), :k)"),
        {"k": api_key_id},
    )
