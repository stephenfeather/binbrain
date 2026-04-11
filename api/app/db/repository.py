from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


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


def find_item_by_upc(db: Session, upc: str) -> dict | None:
    row = db.execute(
        text(
            """
            SELECT item_id, name, category, upc
            FROM items
            WHERE upc = :upc AND deleted_at IS NULL
            """
        ),
        {"upc": upc},
    ).mappings().first()
    return dict(row) if row else None


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
            RETURNING item_id, (xmax = 0) AS inserted
            """
        ),
        {"name": name, "category": category, "notes": notes, "upc": upc},
    ).mappings().one()
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
        text(
            "DELETE FROM bin_items WHERE bin_id = :bin_id AND item_id = :item_id RETURNING id"
        ),
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


def insert_photo(db: Session, bin_id: str, path: str) -> int:
    res = db.execute(
        text("INSERT INTO photos (bin_id, path) VALUES (:bin_id, :path) RETURNING photo_id"),
        {"bin_id": bin_id, "path": path},
    )
    return int(res.scalar_one())


def bin_exists(db: Session, bin_id: str) -> bool:
    return bool(
        db.execute(
            text("SELECT 1 FROM bins WHERE bin_id = :bin_id AND deleted_at IS NULL"),
            {"bin_id": bin_id},
        ).scalar()
    )


def fetch_bin_items(db: Session, bin_id: str) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT
              i.item_id,
              i.name,
              i.category,
              i.upc,
              bi.quantity,
              bi.confidence
            FROM bin_items bi
            JOIN items i ON i.item_id = bi.item_id
            WHERE bi.bin_id = :bin_id
              AND i.deleted_at IS NULL
            ORDER BY i.item_id
            """
        ),
        {"bin_id": bin_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def fetch_bin_photos(db: Session, bin_id: str) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT
              photo_id,
              path
            FROM photos
            WHERE bin_id = :bin_id
            ORDER BY photo_id
            """
        ),
        {"bin_id": bin_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def photo_exists(db: Session, photo_id: int) -> bool:
    return bool(
        db.execute(
            text("SELECT 1 FROM photos WHERE photo_id = :photo_id"),
            {"photo_id": photo_id},
        ).scalar()
    )


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
    rows = db.execute(
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
    ).mappings().all()
    return [dict(row) for row in rows]


def insert_photo_detections(
    db: Session,
    photo_id: int,
    model: str,
    detections: list[dict],
) -> None:
    if not detections:
        return
    db.execute(
        text(
            """
            INSERT INTO photo_detections
              (photo_id, model, label, category, confidence, x1, y1, x2, y2)
            VALUES
              (:photo_id, :model, :label, :category, :confidence, :x1, :y1, :x2, :y2)
            """
        ),
        [
            {
                "photo_id": photo_id,
                "model": model,
                "label": d["label"],
                "category": d.get("category"),
                "confidence": d["confidence"],
                "x1": d["bbox"][0],
                "y1": d["bbox"][1],
                "x2": d["bbox"][2],
                "y2": d["bbox"][3],
            }
            for d in detections
        ],
    )


def clear_detection_groups(db: Session, photo_id: int, model: str) -> None:
    db.execute(
        text(
            "DELETE FROM photo_detection_groups WHERE photo_id = :photo_id AND model = :model"
        ),
        {"photo_id": photo_id, "model": model},
    )


def fetch_cached_groups(db: Session, photo_id: int, model: str) -> list[dict]:
    rows = db.execute(
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
    ).mappings().all()
    return [dict(row) for row in rows]


def compute_groups_from_detections(db: Session, photo_id: int, model: str) -> list[dict]:
    rows = db.execute(
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
    ).mappings().all()
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
    rows = db.execute(
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
    ).mappings().all()
    return [dict(row) for row in rows]


def search_items_by_embedding(db: Session, qvec_str: str, limit: int) -> list[dict]:
    rows = db.execute(
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
    ).mappings().all()
    return [dict(row) for row in rows]


def search_items(
    db: Session,
    qvec_str: str,
    limit: int,
    offset: int,
    min_score: Optional[float],
) -> list[dict]:
    if min_score is None:
        rows = db.execute(
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
        ).mappings().all()
    else:
        max_distance = 1.0 - min_score
        rows = db.execute(
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
        ).mappings().all()
    return [dict(row) for row in rows]


# ── API Key Management ─────────────────────────────────────────────────────────


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def create_api_key(db: Session, name: str) -> tuple[str, str]:
    raw_key = "bb_" + secrets.token_urlsafe(32)
    key_hash = hash_api_key(raw_key)
    db.execute(
        text(
            "INSERT INTO api_keys (key_hash, name) VALUES (:key_hash, :name)"
        ),
        {"key_hash": key_hash, "name": name},
    )
    return key_hash, raw_key


def validate_api_key(db: Session, key_hash: str) -> dict | None:
    row = db.execute(
        text(
            "SELECT id, name, revoked_at FROM api_keys WHERE key_hash = :key_hash"
        ),
        {"key_hash": key_hash},
    ).mappings().first()
    if not row:
        return None
    if row["revoked_at"] is not None:
        return None
    return dict(row)


def list_api_keys(db: Session) -> list[dict]:
    rows = db.execute(
        text(
            "SELECT id, name, created_at, revoked_at, last_used FROM api_keys ORDER BY id"
        )
    ).mappings().all()
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
    rows = db.execute(
        text(
            """
            SELECT class_name, category, source, confirmed_at
            FROM confirmed_classes
            WHERE removed_at IS NULL
            ORDER BY class_name
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def insert_confirmed_class(
    db: Session,
    class_name: str,
    category: Optional[str],
    source: str,
    confirmed_by: Optional[str] = None,
) -> dict | None:
    row = db.execute(
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
    ).mappings().first()
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


def list_locations(db: Session) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT location_id, name, description, created_at
            FROM locations
            WHERE deleted_at IS NULL
            ORDER BY name
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def get_location(db: Session, location_id: int) -> dict | None:
    row = db.execute(
        text(
            """
            SELECT location_id, name, description, created_at
            FROM locations
            WHERE location_id = :location_id AND deleted_at IS NULL
            """
        ),
        {"location_id": location_id},
    ).mappings().first()
    return dict(row) if row else None


def create_location(
    db: Session,
    name: str,
    description: Optional[str],
) -> dict | None:
    row = db.execute(
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
    ).mappings().first()
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
            text(
                "UPDATE bins SET location_id = NULL WHERE location_id = :location_id"
            ),
            {"location_id": location_id},
        )
    return deleted


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
