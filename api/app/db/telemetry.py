from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


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
    client_retry_count: int | None = None,
) -> None:
    """Replace the outcome rows for ``(photo_id, vision_model)`` atomically.

    Dev2_017 (Phase 2 data capture). The endpoint is idempotent per retry:
    a DELETE on the scoping key runs unconditionally, followed by a
    batched INSERT of the current decisions. Outcomes for other
    ``vision_model`` values on the same photo are left alone.

    ``client_retry_count`` (ApiDev2_005, Swift2b-gamma): per-request telemetry
    from the ``X-Client-Retry-Count`` header. Applied uniformly to every
    row in the batch, since the batch originates from a single HTTP
    request. ``None`` (default) leaves the column NULL — preserves
    backward compat for pre-Swift2_018 callers and historical rows.
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
               confidence, bbox, shown_at, decision, edited_to_label,
               client_retry_count, item_id)
            VALUES
              (:photo_id, :vision_model, :prompt_version, :label, :category,
               :confidence, :bbox, :shown_at, :decision, :edited_to_label,
               :client_retry_count, :item_id)
            """
        ),
        [
            {
                "photo_id": photo_id,
                "vision_model": vision_model,
                "prompt_version": prompt_version,
                "client_retry_count": client_retry_count,
                "item_id": None,
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
