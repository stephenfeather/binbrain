from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# FEAT-3 sentinel bin. Items whose parent bin is soft-deleted are
# reattributed here so they remain reachable through
# ``GET /bins/UNASSIGNED`` instead of vanishing into the hidden bin.
# Created in ``migrations/2026-04-18_add_unassigned_bin_sentinel.sql``;
# mirrored in ``api/tests/conftest.py`` ``_init_schema`` and re-seeded
# after every truncate. A DB trigger refuses DELETE, soft-delete via
# ``UPDATE … SET deleted_at``, and rename via ``UPDATE … SET bin_id`` on
# this row.
UNASSIGNED_BIN_ID: str = "UNASSIGNED"


# ---------------------------------------------------------------------------
# Reserved bin-name registry (ApiDev_011).
#
# Users may not create or rename bins into these names. Values are stored in
# their *normalized* form (see ``_normalize_bin_name``); the predicate
# ``is_reserved_bin_name`` normalizes the caller's raw input before the
# membership check so the comparison is case-insensitive and whitespace-
# trimmed. Adding a new reserved name is a one-line change to this frozenset
# — no validator-logic edit required (see ``test_registry_is_authoritative``).
#
# The iOS client mirrors this registry for pre-flight UX
# (Swift2_023_reserved_bin_names.md); the server is the authoritative reject.
# When adding or removing an entry here, coordinate the iOS side in lockstep.
# ---------------------------------------------------------------------------
_RESERVED_BIN_NAME_STEMS: frozenset[str] = frozenset(
    {
        "unassigned",  # collides with the UNASSIGNED sentinel bin_id
        "binless",  # iOS-facing display string for the unassigned state
    }
)


def _normalize_bin_name(raw: str) -> str:
    """Normalize a user-supplied bin name for reserved-registry comparison.

    Uses ``str.casefold()`` rather than ``str.lower()`` — casefold is the
    Python-correct transform for case-insensitive comparison across Unicode
    (handles German ß → ``"ss"``, Greek final sigma, etc.). Also strips
    leading and trailing whitespace so ``"Binless  "`` and
    ``"\\tunassigned\\n"`` collapse to their reserved form.
    """
    return raw.strip().casefold()


def is_reserved_bin_name(raw: str) -> bool:
    """Return True iff ``raw`` (in any case / padding) is in the reserved set.

    Takes the raw user string — callers MUST NOT normalize first. The
    predicate owns normalization so the registry and the normalizer stay
    in lock-step. Lives at the HTTP boundary in ``_check_bin_id``; do NOT
    sprinkle this check into repository helpers, since the ``UNASSIGNED``
    sentinel row is written by internal seed + soft-delete reattribution
    paths that are legitimately exempt.
    """
    if not isinstance(raw, str):
        return False
    return _normalize_bin_name(raw) in _RESERVED_BIN_NAME_STEMS


def ensure_bin_active_or_create(db: Session, bin_id: str) -> None:
    row = db.execute(
        text("SELECT deleted_at FROM bins WHERE bin_id = :bin_id"),
        {"bin_id": bin_id},
    ).first()
    if row is None:
        db.execute(
            text("INSERT INTO bins (bin_id) VALUES (:bin_id)"),
            {"bin_id": bin_id},
        )
        return
    if row[0] is not None:
        raise ValueError("bin is deleted")


def reattribute_bin_items_to_unassigned(db: Session, source_bin_id: str) -> int:
    """Move every ``bin_items`` row from ``source_bin_id`` to ``UNASSIGNED``.

    FEAT-3 — used by the bin soft-delete handler so items don't vanish when
    their parent bin is hidden. Atomic single-statement CTE that respects the
    existing ``bin_items_unique (bin_id, item_id)`` constraint without a
    DELETE-then-UPDATE race: another transaction inserting a conflicting
    ``(UNASSIGNED, item_id)`` between the two steps would have caused the
    UPDATE to fail with a unique-violation. The CTE pattern (reviewed by
    Gemini on PR #28) is race-free in a single statement:

    1. ``DELETE FROM bin_items WHERE bin_id = source RETURNING …`` removes
       every source-bin row in one shot.
    2. ``INSERT … SELECT FROM deleted ON CONFLICT (bin_id, item_id) DO
       NOTHING`` re-inserts each into ``UNASSIGNED``. Conflicts (item already
       in UNASSIGNED) silently no-op — we don't merge quantities.

    Note: rows are physically replaced (new ``id``, ``created_at`` is
    preserved by carrying it through). If preserving the row identity ever
    matters, a SERIALIZABLE transaction or table lock would be the next step.

    Idempotent. Returns the number of source rows the CTE consumed (moved +
    dropped via conflict). Raises ``ValueError`` if the caller tries to
    reattribute the sentinel itself.
    """
    if source_bin_id == UNASSIGNED_BIN_ID:
        raise ValueError("cannot reattribute the UNASSIGNED sentinel bin to itself")

    res = db.execute(
        text(
            """
            WITH deleted AS (
                DELETE FROM bin_items
                WHERE bin_id = :source
                RETURNING item_id, quantity, confidence, created_at
            ),
            inserted AS (
                INSERT INTO bin_items (bin_id, item_id, quantity, confidence, created_at)
                SELECT :unassigned, item_id, quantity, confidence, created_at
                FROM deleted
                ON CONFLICT (bin_id, item_id) DO NOTHING
                RETURNING 1
            )
            SELECT COUNT(*) FROM deleted
            """
        ),
        {"source": source_bin_id, "unassigned": UNASSIGNED_BIN_ID},
    )
    return int(res.scalar() or 0)


def bin_exists(db: Session, bin_id: str) -> bool:
    return bool(
        db.execute(
            text("SELECT 1 FROM bins WHERE bin_id = :bin_id AND deleted_at IS NULL"),
            {"bin_id": bin_id},
        ).scalar()
    )


def soft_delete_bin(db: Session, bin_id: str) -> datetime | None:
    """Soft-delete an active bin by stamping ``deleted_at = now()``.

    FEAT-4-route helper. Returns the new ``deleted_at`` timestamp on
    success, or ``None`` when the bin doesn't exist or is already
    soft-deleted (the WHERE clause is the row-level guard, so concurrent
    deletes resolve to one winner and one None). The
    ``protect_unassigned_bin`` trigger blocks soft-delete on the sentinel
    even if a caller bypasses route-level validation, so that case
    surfaces as ``IntegrityError`` from the underlying execute.

    Does NOT commit — caller controls transaction boundaries so this can
    participate in the same transaction as ``reattribute_bin_items_to_unassigned``.
    """
    row = db.execute(
        text(
            """
            UPDATE bins
            SET deleted_at = now()
            WHERE bin_id = :bin_id
              AND deleted_at IS NULL
            RETURNING deleted_at
            """
        ),
        {"bin_id": bin_id},
    ).first()
    return row[0] if row else None


def list_bins(db: Session) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            WITH item_agg AS (
              SELECT
                bi.bin_id,
                COUNT(*)::int AS item_count,
                MAX(bi.created_at) AS last_item_at
              FROM bin_items bi
              JOIN items i ON i.item_id = bi.item_id
              WHERE i.deleted_at IS NULL
              GROUP BY bi.bin_id
            ),
            photo_agg AS (
              SELECT
                bin_id,
                COUNT(*)::int AS photo_count,
                MAX(created_at) AS last_photo_at
              FROM photos
              GROUP BY bin_id
            )
            SELECT
              b.bin_id,
              b.location_id,
              l.name AS location_name,
              COALESCE(ia.item_count, 0) AS item_count,
              COALESCE(pa.photo_count, 0) AS photo_count,
              GREATEST(
                b.created_at,
                COALESCE(ia.last_item_at, b.created_at),
                COALESCE(pa.last_photo_at, b.created_at)
              ) AS last_updated
            FROM bins b
            LEFT JOIN locations l ON l.location_id = b.location_id AND l.deleted_at IS NULL
            LEFT JOIN item_agg ia ON ia.bin_id = b.bin_id
            LEFT JOIN photo_agg pa ON pa.bin_id = b.bin_id
            WHERE b.deleted_at IS NULL
            ORDER BY last_updated DESC
            """
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def update_bin_location(db: Session, bin_id: str, location_id: int | None) -> bool:
    res = db.execute(
        text(
            """
            UPDATE bins
            SET location_id = :location_id
            WHERE bin_id = :bin_id AND deleted_at IS NULL
            RETURNING bin_id
            """
        ),
        {"bin_id": bin_id, "location_id": location_id},
    )
    return res.scalar() is not None


# Sentinel error codes returned by ``move_bin_item`` — short, stable
# strings that the route maps 1-to-1 to HTTP envelope codes.
MOVE_ERR_ITEM_NOT_FOUND_IN_SOURCE = "item_not_found_in_source_bin"
MOVE_ERR_TARGET_BIN_NOT_FOUND = "target_bin_not_found"
MOVE_ERR_TARGET_ALREADY_HAS_ITEM = "target_already_has_item"


def move_bin_item(
    db: Session,
    *,
    source_bin_id: str,
    target_bin_id: str,
    item_id: int,
    quantity_override: float | None = None,
    confidence_override: float | None = None,
) -> dict | str:
    """Atomically move one ``bin_items`` row from source to target.

    FEAT-2-backend (ApiDev2_012). Returns the effective target row on
    success; returns one of the ``MOVE_ERR_*`` sentinel strings on a
    validation failure (caller renders the 404/409 envelope).

    Transaction shape (all inside the caller's open transaction so the
    route commits once on success):

    1. ``SELECT ... FROM bin_items WHERE bin_id=:source AND item_id=:i
       FOR UPDATE`` — row-lock the source row; missing -> NOT_FOUND.
    2. ``SELECT 1 FROM bins WHERE bin_id=:target AND deleted_at IS NULL``
       — verify target is active; missing -> TARGET_NOT_FOUND.
    3. Advisory pre-check on ``bin_items`` for the target; already
       present -> ALREADY_HAS_ITEM.
    4. ``DELETE FROM bin_items WHERE bin_id=:source AND item_id=:i``.
    5. ``INSERT INTO bin_items ...`` with overrides applied. An
       ``IntegrityError`` on ``bin_items_unique`` (racing concurrent
       move that committed between step 3 and step 5) is caught and
       translated to ALREADY_HAS_ITEM. The caller must ``db.rollback()``
       on any sentinel return.
    """
    from sqlalchemy.exc import IntegrityError

    source_row = (
        db.execute(
            text(
                "SELECT quantity, confidence FROM bin_items "
                "WHERE bin_id = :src AND item_id = :i FOR UPDATE"
            ),
            {"src": source_bin_id, "i": item_id},
        )
        .mappings()
        .first()
    )
    if source_row is None:
        return MOVE_ERR_ITEM_NOT_FOUND_IN_SOURCE

    target_active = db.execute(
        text("SELECT 1 FROM bins WHERE bin_id = :tgt AND deleted_at IS NULL"),
        {"tgt": target_bin_id},
    ).first()
    if target_active is None:
        return MOVE_ERR_TARGET_BIN_NOT_FOUND

    existing_target = db.execute(
        text("SELECT 1 FROM bin_items WHERE bin_id = :tgt AND item_id = :i"),
        {"tgt": target_bin_id, "i": item_id},
    ).first()
    if existing_target is not None:
        return MOVE_ERR_TARGET_ALREADY_HAS_ITEM

    new_quantity = quantity_override if quantity_override is not None else source_row["quantity"]
    new_confidence = (
        confidence_override if confidence_override is not None else source_row["confidence"]
    )

    db.execute(
        text("DELETE FROM bin_items WHERE bin_id = :src AND item_id = :i"),
        {"src": source_bin_id, "i": item_id},
    )
    try:
        db.execute(
            text(
                "INSERT INTO bin_items (bin_id, item_id, quantity, confidence) "
                "VALUES (:tgt, :i, :q, :c)"
            ),
            {
                "tgt": target_bin_id,
                "i": item_id,
                "q": new_quantity,
                "c": new_confidence,
            },
        )
    except IntegrityError:
        return MOVE_ERR_TARGET_ALREADY_HAS_ITEM

    return {
        "bin_id": target_bin_id,
        "item_id": item_id,
        "quantity": new_quantity,
        "confidence": new_confidence,
    }
