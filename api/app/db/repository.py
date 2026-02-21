from __future__ import annotations

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


def insert_item(db: Session, name: str, category: Optional[str], notes: Optional[str]) -> int:
    res = db.execute(
        text(
            """
            INSERT INTO items (name, category, notes)
            VALUES (:name, :category, :notes)
            ON CONFLICT (fingerprint) DO UPDATE
            SET name = EXCLUDED.name,
                category = EXCLUDED.category,
                notes = EXCLUDED.notes,
                deleted_at = NULL
            RETURNING item_id
            """
        ),
        {"name": name, "category": category, "notes": notes},
    )
    return int(res.scalar_one())


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
) -> None:
    db.execute(
        text(
            """
            INSERT INTO bin_items (bin_id, item_id, confidence, quantity)
            VALUES (:bin_id, :item_id, :confidence, :quantity)
            ON CONFLICT DO NOTHING
            """
        ),
        {"bin_id": bin_id, "item_id": item_id, "confidence": confidence, "quantity": quantity},
    )


def insert_photo(db: Session, bin_id: str, path: str) -> None:
    db.execute(
        text("INSERT INTO photos (bin_id, path) VALUES (:bin_id, :path)"),
        {"bin_id": bin_id, "path": path},
    )


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
              COALESCE(ia.item_count, 0) AS item_count,
              COALESCE(pa.photo_count, 0) AS photo_count,
              GREATEST(
                b.created_at,
                COALESCE(ia.last_item_at, b.created_at),
                COALESCE(pa.last_photo_at, b.created_at)
              ) AS last_updated
            FROM bins b
            LEFT JOIN item_agg ia ON ia.bin_id = b.bin_id
            LEFT JOIN photo_agg pa ON pa.bin_id = b.bin_id
            WHERE b.deleted_at IS NULL
            ORDER BY last_updated DESC
            """
        )
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
                  (e.embedding <=> CAST(:qvec AS vector)) AS distance,
                  array_remove(array_agg(bi.bin_id), NULL) AS bins
                FROM item_embeddings e
                JOIN items i ON i.item_id = e.item_id
                LEFT JOIN bin_items bi ON bi.item_id = i.item_id
                WHERE i.deleted_at IS NULL
                GROUP BY i.item_id, i.name, i.category, e.embedding
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
                  (e.embedding <=> CAST(:qvec AS vector)) AS distance,
                  array_remove(array_agg(bi.bin_id), NULL) AS bins
                FROM item_embeddings e
                JOIN items i ON i.item_id = e.item_id
                LEFT JOIN bin_items bi ON bi.item_id = i.item_id
                WHERE i.deleted_at IS NULL
                  AND (e.embedding <=> CAST(:qvec AS vector)) <= :max_distance
                GROUP BY i.item_id, i.name, i.category, e.embedding
                ORDER BY e.embedding <=> CAST(:qvec AS vector)
                LIMIT :limit
                OFFSET :offset
                """
            ),
            {"qvec": qvec_str, "limit": limit, "offset": offset, "max_distance": max_distance},
        ).mappings().all()
    return [dict(row) for row in rows]
