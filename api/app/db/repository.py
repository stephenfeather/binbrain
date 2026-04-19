from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("binbrain")


# FEAT-3 sentinel bin. Items whose parent bin is soft-deleted are
# reattributed here so they remain reachable through
# ``GET /bins/UNASSIGNED`` instead of vanishing into the hidden bin.
# Created in ``migrations/2026-04-18_add_unassigned_bin_sentinel.sql``;
# mirrored in ``api/tests/conftest.py`` ``_init_schema`` and re-seeded
# after every truncate. A DB trigger refuses DELETE, soft-delete via
# ``UPDATE … SET deleted_at``, and rename via ``UPDATE … SET bin_id`` on
# this row.
UNASSIGNED_BIN_ID: str = "UNASSIGNED"


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
    item_id: Optional[int],
    source: str,
    raw_response: Optional[dict],
    elapsed_ms: Optional[int],
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


def insert_item(
    db: Session,
    name: str,
    category: Optional[str],
    notes: Optional[str],
    upc: Optional[str] = None,
) -> int:
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
    category: Optional[str],
    notes: Optional[str],
    upc: Optional[str] = None,
) -> tuple[int, bool]:
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
    confidence: Optional[float],
    quantity: Optional[float],
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
    quantity: Optional[float] = None,
    confidence: Optional[float] = None,
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


def insert_photo(
    db: Session,
    bin_id: str,
    path: str,
    device_metadata: dict | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
    session_id: str | None = None,
) -> int:
    res = db.execute(
        text(
            "INSERT INTO photos (bin_id, path, device_metadata, width, height, session_id) "
            "VALUES (:bin_id, :path, CAST(:device_metadata AS jsonb), :width, :height, :session_id) "
            "RETURNING photo_id"
        ),
        {
            "bin_id": bin_id,
            "path": path,
            "device_metadata": json.dumps(device_metadata) if device_metadata else None,
            "width": width,
            "height": height,
            "session_id": session_id,
        },
    )
    return int(res.scalar_one())


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
                  AND decision = 'accepted'
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


def fetch_bin_photos(db: Session, bin_id: str) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT
              photo_id,
              path,
              device_metadata
            FROM photos
            WHERE bin_id = :bin_id
            ORDER BY photo_id
            """
            ),
            {"bin_id": bin_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def photo_exists(db: Session, photo_id: int) -> bool:
    return bool(
        db.execute(
            text("SELECT 1 FROM photos WHERE photo_id = :photo_id"),
            {"photo_id": photo_id},
        ).scalar()
    )


def find_photo_by_uuid(db: Session, photo_uuid: str) -> int | None:
    """Resolve a ``photo_uuid`` to its internal ``photo_id``.

    FEAT-6: export paths accept the stable cross-environment uuid handle;
    internal joins continue to use the bigint PK. Returns None if no match.
    """
    return db.execute(
        text("SELECT photo_id FROM photos WHERE photo_uuid = :photo_uuid"),
        {"photo_uuid": photo_uuid},
    ).scalar()


def delete_photo(db: Session, photo_id: int) -> str | None:
    """Delete a photo row and return its path, or None if not found."""
    return db.execute(
        text("DELETE FROM photos WHERE photo_id = :photo_id RETURNING path"),
        {"photo_id": photo_id},
    ).scalar()


def fetch_photo_path(db: Session, photo_id: int) -> str | None:
    return db.execute(
        text("SELECT path FROM photos WHERE photo_id = :photo_id"),
        {"photo_id": photo_id},
    ).scalar()


def fetch_photo_groups(db: Session, photo_id: int) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT
              (label || '|' || COALESCE(category, '')) AS group_key,
              label,
              category,
              AVG(confidence)::float AS confidence,
              COUNT(*)::int AS count_estimate
            FROM photo_labels
            WHERE photo_id = :photo_id
            GROUP BY label, category
            ORDER BY confidence DESC, label ASC, category ASC
            """
            ),
            {"photo_id": photo_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def insert_photo_detections(
    db: Session,
    photo_id: int,
    model: str,
    detections: list[dict],
    *,
    prompt_version: str | None = None,
) -> list[int]:
    """Insert detection rows for ``(photo_id, model)`` and return their ids.

    Returns the list of newly inserted ``photo_detections.id`` values in the
    same order as ``detections``. Callers that need to wire a downstream FK
    (e.g. ``photo_suggestion_matches.photo_detection_id``) can zip this list
    against the inputs. Callers that don't care about ids (YOLO /detect) can
    ignore the return value.
    """
    if not detections:
        return []
    # PR#21 review follow-up (Gemini #4): one roundtrip, not N. Build a
    # single multi-row INSERT with per-row placeholders so Postgres returns
    # every inserted id in VALUES order in one network trip. The previous
    # per-row RETURNING loop was correct but regressed YOLO /detect
    # throughput (which ignores the ids) for no benefit.
    placeholders: list[str] = []
    params: dict = {}
    for i, d in enumerate(detections):
        placeholders.append(
            f"(:photo_id_{i}, :model_{i}, :label_{i}, :category_{i}, "
            f":confidence_{i}, :x1_{i}, :y1_{i}, :x2_{i}, :y2_{i}, "
            f":prompt_version_{i})"
        )
        params[f"photo_id_{i}"] = photo_id
        params[f"model_{i}"] = model
        params[f"label_{i}"] = d["label"]
        params[f"category_{i}"] = d.get("category")
        params[f"confidence_{i}"] = d["confidence"]
        params[f"x1_{i}"] = d["bbox"][0]
        params[f"y1_{i}"] = d["bbox"][1]
        params[f"x2_{i}"] = d["bbox"][2]
        params[f"y2_{i}"] = d["bbox"][3]
        params[f"prompt_version_{i}"] = prompt_version
    rows = (
        db.execute(
            text(
                f"""
            INSERT INTO photo_detections
              (photo_id, model, label, category, confidence,
               x1, y1, x2, y2, prompt_version)
            VALUES
              {", ".join(placeholders)}
            RETURNING id
            """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [int(r["id"]) for r in rows]


def get_photo_detections(db: Session, photo_id: int, model: str) -> list[dict]:
    """Return rows for (photo_id, model) as a list of dicts.

    Keys match the ``insert_photo_detections`` write shape so callers can
    round-trip a detection set through the DB without format translation:
    ``{"id", "label", "category", "confidence", "bbox", "prompt_version"}``
    where ``bbox`` is a 4-element ``[x1, y1, x2, y2]`` list and
    ``prompt_version`` is the VLM prompt revision stamped at write time
    (``None`` for rows written before Dev2_016 prompt-version
    instrumentation). ``id`` is the primary key and is load-bearing for
    downstream match-telemetry writes (``photo_suggestion_matches``).
    """
    rows = (
        db.execute(
            text(
                """
            SELECT id, label, category, confidence, x1, y1, x2, y2, prompt_version
            FROM photo_detections
            WHERE photo_id = :photo_id AND model = :model
            ORDER BY id
            """
            ),
            {"photo_id": photo_id, "model": model},
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": int(row["id"]),
            "label": row["label"],
            "category": row["category"],
            "confidence": float(row["confidence"]),
            "bbox": [float(row["x1"]), float(row["y1"]), float(row["x2"]), float(row["y2"])],
            "prompt_version": row["prompt_version"],
        }
        for row in rows
    ]


def clear_photo_detections(db: Session, photo_id: int, model: str) -> None:
    """Delete all ``photo_detections`` rows for (photo_id, model).

    Used by /suggest to realise the "latest vision answer wins" invariant
    before writing new detections from a fresh Fireworks call.
    """
    db.execute(
        text("DELETE FROM photo_detections WHERE photo_id = :photo_id AND model = :model"),
        {"photo_id": photo_id, "model": model},
    )


def clear_detection_groups(db: Session, photo_id: int, model: str) -> None:
    db.execute(
        text("DELETE FROM photo_detection_groups WHERE photo_id = :photo_id AND model = :model"),
        {"photo_id": photo_id, "model": model},
    )


def fetch_cached_groups(db: Session, photo_id: int, model: str) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT
              (label || '|' || COALESCE(category, '')) AS group_key,
              label,
              category,
              confidence_avg AS confidence,
              count_estimate
            FROM photo_detection_groups
            WHERE photo_id = :photo_id AND model = :model
            ORDER BY confidence_avg DESC, label ASC, category ASC
            """
            ),
            {"photo_id": photo_id, "model": model},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def compute_groups_from_detections(db: Session, photo_id: int, model: str) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT
              (label || '|' || COALESCE(category, '')) AS group_key,
              label,
              category,
              AVG(confidence)::float AS confidence,
              COUNT(*)::int AS count_estimate
            FROM photo_detections
            WHERE photo_id = :photo_id AND model = :model
            GROUP BY label, category
            ORDER BY confidence DESC, label ASC, category ASC
            """
            ),
            {"photo_id": photo_id, "model": model},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def insert_detection_groups(
    db: Session,
    photo_id: int,
    model: str,
    groups: list[dict],
) -> None:
    if not groups:
        return
    db.execute(
        text(
            """
            INSERT INTO photo_detection_groups
              (photo_id, model, label, category, confidence_avg, count_estimate)
            VALUES
              (:photo_id, :model, :label, :category, :confidence_avg, :count_estimate)
            """
        ),
        [
            {
                "photo_id": photo_id,
                "model": model,
                "label": g["label"],
                "category": g.get("category"),
                "confidence_avg": g["confidence"],
                "count_estimate": g["count_estimate"],
            }
            for g in groups
        ],
    )


def insert_photo_group_item(
    db: Session,
    photo_id: int,
    model: str,
    label: str,
    category: str | None,
    item_id: int,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO photo_group_items (photo_id, model, label, category, item_id)
            VALUES (:photo_id, :model, :label, :category, :item_id)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "photo_id": photo_id,
            "model": model,
            "label": label,
            "category": category,
            "item_id": item_id,
        },
    )


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
    min_score: Optional[float],
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


# ── API Key Management ─────────────────────────────────────────────────────────


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


# ── Settings ──────────────────────────────────────────────────────────


def get_setting(db: Session, key: str) -> Optional[str]:
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


# ── Confirmed Classes ────────────────────────────────────────────────


def fetch_active_classes(db: Session) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT class_name, category, source, confirmed_at
            FROM confirmed_classes
            WHERE removed_at IS NULL
            ORDER BY class_name
            """
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def insert_confirmed_class(
    db: Session,
    class_name: str,
    category: Optional[str],
    source: str,
    confirmed_by: Optional[str] = None,
) -> dict | None:
    row = (
        db.execute(
            text(
                """
            INSERT INTO confirmed_classes (class_name, category, source, confirmed_by)
            VALUES (lower(trim(:class_name)), :category, :source, :confirmed_by)
            ON CONFLICT (lower(trim(class_name))) WHERE removed_at IS NULL
            DO NOTHING
            RETURNING id, class_name
            """
            ),
            {
                "class_name": class_name,
                "category": category,
                "source": source,
                "confirmed_by": confirmed_by,
            },
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def soft_delete_class(db: Session, class_name: str) -> bool:
    res = db.execute(
        text(
            """
            UPDATE confirmed_classes
            SET removed_at = now()
            WHERE lower(trim(class_name)) = lower(trim(:class_name))
              AND removed_at IS NULL
            RETURNING id
            """
        ),
        {"class_name": class_name},
    )
    return res.scalar() is not None


# ── Locations ──────────────────────────────────────────────────────────


def fetch_bin_location(db: Session, bin_id: str) -> dict:
    row = (
        db.execute(
            text(
                """
            SELECT b.location_id, l.name AS location_name
            FROM bins b
            LEFT JOIN locations l ON l.location_id = b.location_id AND l.deleted_at IS NULL
            WHERE b.bin_id = :bin_id
            """
            ),
            {"bin_id": bin_id},
        )
        .mappings()
        .first()
    )
    return {
        "location_id": row["location_id"] if row else None,
        "location_name": row["location_name"] if row else None,
    }


def list_locations(db: Session) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT location_id, name, description, created_at
            FROM locations
            WHERE deleted_at IS NULL
            ORDER BY name
            """
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def get_location(db: Session, location_id: int) -> dict | None:
    row = (
        db.execute(
            text(
                """
            SELECT location_id, name, description, created_at
            FROM locations
            WHERE location_id = :location_id AND deleted_at IS NULL
            """
            ),
            {"location_id": location_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def create_location(
    db: Session,
    name: str,
    description: Optional[str],
) -> dict | None:
    row = (
        db.execute(
            text(
                """
            INSERT INTO locations (name, description)
            VALUES (trim(:name), :description)
            ON CONFLICT (lower(trim(name))) WHERE deleted_at IS NULL
            DO NOTHING
            RETURNING location_id, name, description, created_at
            """
            ),
            {"name": name, "description": description},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def update_location(
    db: Session,
    location_id: int,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    update_description: bool = False,
) -> dict | None | str:
    """Update a location's name and/or description.

    Returns:
        * the updated row (dict) on success
        * None if the location does not exist (or is soft-deleted)
        * the string "conflict" if the new name collides with another active
          location's case-insensitive trimmed name

    `update_description=True` distinguishes "set description to NULL" from
    "leave description unchanged"; when False, description is ignored.
    """
    # Conflict check before the UPDATE so PATCH {name: existing} yields 409.
    if name is not None:
        conflict = db.execute(
            text(
                """
                SELECT location_id FROM locations
                WHERE lower(trim(name)) = lower(trim(:name))
                  AND deleted_at IS NULL
                  AND location_id <> :location_id
                LIMIT 1
                """
            ),
            {"name": name, "location_id": location_id},
        ).scalar()
        if conflict is not None:
            return "conflict"

    sets = []
    params: dict = {"location_id": location_id}
    if name is not None:
        sets.append("name = trim(:name)")
        params["name"] = name
    if update_description:
        sets.append("description = :description")
        params["description"] = description

    if not sets:
        return get_location(db, location_id)

    row = (
        db.execute(
            text(
                f"""
            UPDATE locations
            SET {", ".join(sets)}
            WHERE location_id = :location_id AND deleted_at IS NULL
            RETURNING location_id, name, description, created_at
            """
            ),
            params,
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def soft_delete_location(db: Session, location_id: int) -> bool:
    res = db.execute(
        text(
            """
            UPDATE locations
            SET deleted_at = now()
            WHERE location_id = :location_id
              AND deleted_at IS NULL
            RETURNING location_id
            """
        ),
        {"location_id": location_id},
    )
    deleted = res.scalar() is not None
    if deleted:
        db.execute(
            text("UPDATE bins SET location_id = NULL WHERE location_id = :location_id"),
            {"location_id": location_id},
        )
    return deleted


def link_suggestion_outcomes_to_item(
    db: Session,
    *,
    photo_id: int,
    label: str,
    category: str | None,
    item_id: int,
) -> int:
    """Attach ``item_id`` to any outcome rows on ``photo_id`` that produced it.

    FEAT-5 provenance wiring. Called from ``/photos/{id}/confirm`` right after
    an item is materialized from a suggestion. Matches any still-unlinked
    outcome on the same photo whose decision produced this label:

      * ``decision='accepted'`` and the original ``label`` matches.
      * ``decision='edited'``   and the user-chosen ``edited_to_label``
        matches (the bbox / source photo are still the right provenance —
        only the label was rewritten).

    Returns the number of outcome rows linked. Safe to call repeatedly:
    ``item_id IS NULL`` on the WHERE clause makes it idempotent.
    """
    result = db.execute(
        text(
            """
            UPDATE photo_suggestion_outcomes
            SET item_id = :item_id
            WHERE photo_id = :photo_id
              AND item_id IS NULL
              AND category IS NOT DISTINCT FROM :category
              AND (
                  (decision = 'accepted' AND label = :label)
                  OR (decision = 'edited' AND edited_to_label = :label)
              )
            """
        ),
        {
            "item_id": item_id,
            "photo_id": photo_id,
            "label": label,
            "category": category,
        },
    )
    return int(result.rowcount or 0)


def replace_photo_suggestion_outcomes(
    db: Session,
    photo_id: int,
    vision_model: str,
    prompt_version: str | None,
    decisions: list[dict],
) -> None:
    """Replace the outcome rows for ``(photo_id, vision_model)`` atomically.

    Dev2_017 (Phase 2 data capture). The endpoint is idempotent per retry:
    a DELETE on the scoping key runs unconditionally, followed by a
    batched INSERT of the current decisions. Outcomes for other
    ``vision_model`` values on the same photo are left alone.
    """
    db.execute(
        text(
            "DELETE FROM photo_suggestion_outcomes "
            "WHERE photo_id = :photo_id AND vision_model = :vision_model"
        ),
        {"photo_id": photo_id, "vision_model": vision_model},
    )
    if not decisions:
        return
    db.execute(
        text(
            """
            INSERT INTO photo_suggestion_outcomes
              (photo_id, vision_model, prompt_version, label, category,
               confidence, bbox, shown_at, decision, edited_to_label)
            VALUES
              (:photo_id, :vision_model, :prompt_version, :label, :category,
               :confidence, :bbox, :shown_at, :decision, :edited_to_label)
            """
        ),
        [
            {
                "photo_id": photo_id,
                "vision_model": vision_model,
                "prompt_version": prompt_version,
                **d,
            }
            for d in decisions
        ],
    )


def insert_vision_call(
    db: Session,
    *,
    photo_id: int | None,
    model: str,
    prompt_version: str | None,
    base_url: str | None,
    started_at: datetime,
    elapsed_ms: int | None,
    hits_count: int | None,
    cached: bool,
    outcome: str,
    error_code: str | None,
    flags: dict | None,
) -> int:
    """Append one ``vision_calls`` row and return its id.

    Dev2_018 Phase 3. One row per ``/suggest`` invocation (success, cache
    hit, or error). Append-only: callers never replace or delete existing
    rows. ``flags`` is a bag — known keys today: ``whole_image_bbox_warn``,
    ``bbox_normalized``, ``stages``. Additive-only over time; never rename.

    ``model`` is NOT NULL per schema. Pre-vision-resolution failures (404 on
    photo lookup) are therefore NOT written here — they have no model to
    attribute the call to.
    """
    flags_json = json.dumps(flags or {})
    row = (
        db.execute(
            text(
                """
            INSERT INTO vision_calls
              (photo_id, model, prompt_version, base_url, started_at,
               elapsed_ms, hits_count, cached, outcome, error_code, flags)
            VALUES
              (:photo_id, :model, :prompt_version, :base_url, :started_at,
               :elapsed_ms, :hits_count, :cached, :outcome, :error_code,
               CAST(:flags AS jsonb))
            RETURNING id
            """
            ),
            {
                "photo_id": photo_id,
                "model": model,
                "prompt_version": prompt_version,
                "base_url": base_url,
                "started_at": started_at,
                "elapsed_ms": elapsed_ms,
                "hits_count": hits_count,
                "cached": cached,
                "outcome": outcome,
                "error_code": error_code,
                "flags": flags_json,
            },
        )
        .mappings()
        .one()
    )
    return int(row["id"])


def insert_photo_suggestion_matches(
    db: Session,
    *,
    rows: list[dict],
) -> None:
    """Batched append of ``photo_suggestion_matches`` rows.

    Each row shape: ``{photo_detection_id, matched_item_id, score,
    threshold_at_compute}``. ``matched_item_id`` may be ``None`` — a row
    with a NULL item is the "we saw this hit but rejected it because score
    < threshold" signal and is the whole point of the table. No-op if
    ``rows`` is empty.

    Append-only: callers never delete or replace. A fresh /suggest of the
    same photo writes new rows; history is retained.
    """
    if not rows:
        return
    db.execute(
        text(
            """
            INSERT INTO photo_suggestion_matches
              (photo_detection_id, matched_item_id, score, threshold_at_compute)
            VALUES
              (:photo_detection_id, :matched_item_id, :score, :threshold_at_compute)
            """
        ),
        [
            {
                "photo_detection_id": r["photo_detection_id"],
                "matched_item_id": r.get("matched_item_id"),
                "score": r["score"],
                "threshold_at_compute": r["threshold_at_compute"],
            }
            for r in rows
        ],
    )


def insert_search_query(
    db: Session,
    *,
    request_id: str | None,
    q: str,
    qvec_dims: int,
    min_score_effective: float,
    result_count: int,
) -> int:
    """Append one ``search_queries`` row and return its id.

    Dev2_019. One row per ``/search`` invocation, written on every call
    (not only zero-result) so the relevance floor can be calibrated after
    the fact. ``min_score_effective`` is whatever value actually filtered
    the query, regardless of source (explicit param vs. env default).
    Append-only — callers never delete or replace.
    """
    row = (
        db.execute(
            text(
                """
            INSERT INTO search_queries
              (request_id, q, qvec_dims, min_score_effective, result_count)
            VALUES
              (:request_id, :q, :qvec_dims, :min_score_effective, :result_count)
            RETURNING id
            """
            ),
            {
                "request_id": request_id,
                "q": q,
                "qvec_dims": qvec_dims,
                "min_score_effective": min_score_effective,
                "result_count": result_count,
            },
        )
        .mappings()
        .one()
    )
    return int(row["id"])


def update_bin_location(db: Session, bin_id: str, location_id: Optional[int]) -> bool:
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
