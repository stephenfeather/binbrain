from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session


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


def fetch_photo_device_metadata(db: Session, photo_id: int) -> dict | None:
    row = db.execute(
        text("SELECT device_metadata FROM photos WHERE photo_id = :photo_id"),
        {"photo_id": photo_id},
    ).first()
    if row is None:
        return None
    metadata = row[0]
    return metadata if isinstance(metadata, dict) else None


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
