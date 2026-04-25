from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


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
    category: str | None,
    source: str,
    confirmed_by: str | None = None,
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
