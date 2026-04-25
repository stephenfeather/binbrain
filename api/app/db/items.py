from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("binbrain")


def find_item_by_upc(db: Session, upc: str) -> dict | None:
    row = (
        db.execute(
            text(
                """
            SELECT item_id, name, category, upc
            FROM items
            WHERE upc = :upc AND deleted_at IS NULL
            """
            ),
            {"upc": upc},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def insert_upc_lookup(
    db: Session,
    *,
    upc: str,
    item_id: int | None,
    source: str,
    raw_response: dict | None,
    elapsed_ms: int | None,
) -> int:
    """Append one ``item_upc_lookups`` provenance row and return its id.

    ApiDev2_002 (Gap #7). Every ``/upc/{upc}`` invocation writes exactly one
    row covering one of four outcomes — ``local`` cache hit, ``upcitemdb``
    external hit, ``go-upc`` external hit, or ``unknown`` fallback. The table
    is append-only: callers never UPDATE or DELETE rows so the full audit
    trail is preserved across re-lookups.

    ``raw_response`` is serialized with ``json.dumps`` and CAST to ``jsonb``
    to match the existing patterns in ``insert_photo`` and
    ``insert_vision_call``. If serialization fails (non-JSON-serializable
    values reaching this layer from a misbehaving upstream), the helper
    drops the raw body to NULL rather than raising — provenance is
    best-effort telemetry and must never break the route. DB errors are
    NOT swallowed: a failing INSERT surfaces so the caller's try/except
    can decide whether to log-and-continue.

    Does NOT commit. Caller controls transaction boundaries so this write
    can participate in the same transaction as the item insert or, more
    commonly, run in its own isolated transaction after the item commit
    has succeeded.
    """
    try:
        raw_json: str | None = json.dumps(raw_response) if raw_response is not None else None
    except (TypeError, ValueError) as exc:
        logger.warning(
            "event=upc_lookup_raw_response_unserializable upc=%s source=%s err=%s",
            upc,
            source,
            exc,
        )
        raw_json = None
    res = db.execute(
        text(
            """
            INSERT INTO item_upc_lookups
              (upc, item_id, source, raw_response, elapsed_ms)
            VALUES
              (:upc, :item_id, :source, CAST(:raw_response AS jsonb), :elapsed_ms)
            RETURNING id
            """
        ),
        {
            "upc": upc,
            "item_id": item_id,
            "source": source,
            "raw_response": raw_json,
            "elapsed_ms": elapsed_ms,
        },
    )
    return int(res.scalar_one())


def _normalize_category(category: str | None) -> str | None:
    if category is None:
        return None
    normalized = category.strip().lower()
    return normalized or None


def insert_item(
    db: Session,
    name: str,
    category: str | None,
    notes: str | None,
    upc: str | None = None,
) -> int:
    category = _normalize_category(category)
    res = db.execute(
        text(
            """
            INSERT INTO items (name, category, notes, upc)
            VALUES (:name, :category, :notes, :upc)
            ON CONFLICT (fingerprint) DO UPDATE
            SET name = EXCLUDED.name,
                category = EXCLUDED.category,
                notes = EXCLUDED.notes,
                upc = COALESCE(EXCLUDED.upc, items.upc),
                deleted_at = NULL
            RETURNING item_id
            """
        ),
        {"name": name, "category": category, "notes": notes, "upc": upc},
    )
    return int(res.scalar_one())


def insert_item_with_status(
    db: Session,
    name: str,
    category: str | None,
    notes: str | None,
    upc: str | None = None,
) -> tuple[int, bool]:
    category = _normalize_category(category)
    res = (
        db.execute(
            text(
                """
            INSERT INTO items (name, category, notes, upc)
            VALUES (:name, :category, :notes, :upc)
            ON CONFLICT (fingerprint) DO UPDATE
            SET name = EXCLUDED.name,
                category = EXCLUDED.category,
                notes = EXCLUDED.notes,
                upc = COALESCE(EXCLUDED.upc, items.upc),
                deleted_at = NULL
            RETURNING item_id, (xmax = 0) AS inserted
            """
            ),
            {"name": name, "category": category, "notes": notes, "upc": upc},
        )
        .mappings()
        .one()
    )
    return int(res["item_id"]), bool(res["inserted"])


def upsert_item_embedding(db: Session, item_id: int, model: str, dims: int, embedding: str) -> None:
    db.execute(
        text(
            """
            INSERT INTO item_embeddings (item_id, model, dims, embedding)
            VALUES (:item_id, :model, :dims, CAST(:embedding AS vector))
            ON CONFLICT (item_id) DO UPDATE
            SET model = EXCLUDED.model,
                dims = EXCLUDED.dims,
                embedding = EXCLUDED.embedding,
                updated_at = now()
            """
        ),
        {"item_id": item_id, "model": model, "dims": dims, "embedding": embedding},
    )


def insert_bin_item(
    db: Session,
    bin_id: str,
    item_id: int,
    confidence: float | None,
    quantity: float | None,
) -> bool:
    res = db.execute(
        text(
            """
            INSERT INTO bin_items (bin_id, item_id, confidence, quantity)
            VALUES (:bin_id, :item_id, :confidence, :quantity)
            ON CONFLICT DO NOTHING
            RETURNING id
            """
        ),
        {"bin_id": bin_id, "item_id": item_id, "confidence": confidence, "quantity": quantity},
    )
    return res.scalar() is not None


def delete_bin_item(db: Session, bin_id: str, item_id: int) -> bool:
    res = db.execute(
        text("DELETE FROM bin_items WHERE bin_id = :bin_id AND item_id = :item_id RETURNING id"),
        {"bin_id": bin_id, "item_id": item_id},
    )
    return res.scalar() is not None


def update_bin_item(
    db: Session,
    bin_id: str,
    item_id: int,
    quantity: float | None = None,
    confidence: float | None = None,
) -> bool:
    sets: list[str] = []
    params: dict = {"bin_id": bin_id, "item_id": item_id}
    if quantity is not None:
        sets.append("quantity = :quantity")
        params["quantity"] = quantity
    if confidence is not None:
        sets.append("confidence = :confidence")
        params["confidence"] = confidence
    if not sets:
        return False
    sql = f"UPDATE bin_items SET {', '.join(sets)} WHERE bin_id = :bin_id AND item_id = :item_id RETURNING id"
    res = db.execute(text(sql), params)
    return res.scalar() is not None


def get_bin_item(db: Session, bin_id: str, item_id: int) -> dict | None:
    """Return the ``bin_items`` row for ``(bin_id, item_id)`` or ``None``.

    FEAT-2-backend: the PATCH route's response needs the effective row
    values (post-update for in-place, post-insert for move) — the spec's
    ``quantity`` / ``confidence`` are nullable and must reflect what the
    row actually holds, not just the supplied overrides.
    """
    row = (
        db.execute(
            text(
                "SELECT bin_id, item_id, quantity, confidence "
                "FROM bin_items WHERE bin_id = :b AND item_id = :i"
            ),
            {"b": bin_id, "i": item_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def fetch_bin_items(db: Session, bin_id: str) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT
              i.item_id,
              i.name,
              i.category,
              i.upc,
              bi.quantity,
              bi.confidence,
              pso.photo_id AS source_photo_id,
              pso.bbox     AS source_bbox
            FROM bin_items bi
            JOIN items i ON i.item_id = bi.item_id
            LEFT JOIN LATERAL (
                SELECT photo_id, bbox
                FROM photo_suggestion_outcomes
                WHERE item_id = i.item_id
                  AND decision IN ('accepted', 'edited')
                ORDER BY decided_at DESC
                LIMIT 1
            ) pso ON TRUE
            WHERE bi.bin_id = :bin_id
              AND i.deleted_at IS NULL
            ORDER BY i.item_id
            """
            ),
            {"bin_id": bin_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def backfill_item_upc_if_missing(db: Session, item_id: int, upc: str) -> bool:
    res = db.execute(
        text(
            """
            UPDATE items SET upc = :upc
            WHERE item_id = :item_id AND upc IS NULL
            """
        ),
        {"item_id": item_id, "upc": upc},
    )
    return bool(res.rowcount)


def search_items_by_embedding(db: Session, qvec_str: str, limit: int) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT
              i.item_id,
              i.name,
              i.category,
              i.upc,
              1.0 - (e.embedding <=> CAST(:qvec AS vector)) AS score,
              array_remove(array_agg(bi.bin_id), NULL) AS bins
            FROM item_embeddings e
            JOIN items i ON i.item_id = e.item_id
            LEFT JOIN bin_items bi ON bi.item_id = i.item_id
            WHERE i.deleted_at IS NULL
            GROUP BY i.item_id, i.name, i.category, i.upc, e.embedding
            ORDER BY e.embedding <=> CAST(:qvec AS vector)
            LIMIT :limit
            """
            ),
            {"qvec": qvec_str, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def search_items(
    db: Session,
    qvec_str: str,
    limit: int,
    offset: int,
    min_score: float | None,
) -> list[dict]:
    if min_score is None:
        rows = (
            db.execute(
                text(
                    """
                SELECT
                  i.item_id,
                  i.name,
                  i.category,
                  i.upc,
                  1.0 - (e.embedding <=> CAST(:qvec AS vector)) AS score,
                  array_remove(array_agg(bi.bin_id), NULL) AS bins
                FROM item_embeddings e
                JOIN items i ON i.item_id = e.item_id
                LEFT JOIN bin_items bi ON bi.item_id = i.item_id
                WHERE i.deleted_at IS NULL
                GROUP BY i.item_id, i.name, i.category, i.upc, e.embedding
                ORDER BY e.embedding <=> CAST(:qvec AS vector)
                LIMIT :limit
                OFFSET :offset
                """
                ),
                {"qvec": qvec_str, "limit": limit, "offset": offset},
            )
            .mappings()
            .all()
        )
    else:
        max_distance = 1.0 - min_score
        rows = (
            db.execute(
                text(
                    """
                SELECT
                  i.item_id,
                  i.name,
                  i.category,
                  i.upc,
                  1.0 - (e.embedding <=> CAST(:qvec AS vector)) AS score,
                  array_remove(array_agg(bi.bin_id), NULL) AS bins
                FROM item_embeddings e
                JOIN items i ON i.item_id = e.item_id
                LEFT JOIN bin_items bi ON bi.item_id = i.item_id
                WHERE i.deleted_at IS NULL
                  AND (e.embedding <=> CAST(:qvec AS vector)) <= :max_distance
                GROUP BY i.item_id, i.name, i.category, i.upc, e.embedding
                ORDER BY e.embedding <=> CAST(:qvec AS vector)
                LIMIT :limit
                OFFSET :offset
                """
                ),
                {"qvec": qvec_str, "limit": limit, "offset": offset, "max_distance": max_distance},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]
