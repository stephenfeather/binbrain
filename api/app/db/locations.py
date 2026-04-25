from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


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
    description: str | None,
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
    name: str | None = None,
    description: str | None = None,
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
