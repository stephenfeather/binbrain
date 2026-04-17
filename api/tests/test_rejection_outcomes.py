"""Dev2_017: Rejection Outcomes — Phase 2 of the Data Capture Foundation.

Server-only slice. Covers the new ``photo_suggestion_outcomes`` table and
its fire-and-forget telemetry endpoint ``POST /photos/{id}/outcomes``.

The endpoint is additive: ``/photos/{id}/confirm`` is NOT modified, and
nothing in the acceptance path (``photo_group_items`` -> ``bin_items``)
is touched from here.
"""
from __future__ import annotations

from sqlalchemy import text


def _seed_photo(client, valid_jpeg_bytes, bin_id: str) -> int:
    r = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    return r.json()["photos"][0]["photo_id"]


def _payload(
    decisions: list[dict],
    *,
    vision_model: str = "accounts/fireworks/models/qwen3p6-plus",
    prompt_version: str | None = "v2",
) -> dict:
    body: dict = {
        "vision_model": vision_model,
        "decisions": decisions,
    }
    if prompt_version is not None:
        body["prompt_version"] = prompt_version
    return body


# ---------------------------------------------------------------------------
# 1. Happy path — one row per decision, fields round-trip
# ---------------------------------------------------------------------------


def test_post_outcomes_writes_rows_for_each_decision(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-OUTCOMES-0001")

    payload = _payload(
        [
            {
                "label": "hex bolt",
                "category": "fastener",
                "confidence": 0.91,
                "bbox": [0.12, 0.34, 0.40, 0.68],
                "shown_at": "2026-04-17T19:32:01Z",
                "decision": "accepted",
            },
            {
                "label": "lamp base",
                "confidence": 0.74,
                "shown_at": "2026-04-17T19:32:01Z",
                "decision": "rejected",
            },
            {
                "label": "plastic gear",
                "shown_at": "2026-04-17T19:32:01Z",
                "decision": "edited",
                "edited_to_label": "brass gear",
            },
        ]
    )

    r = client.post(f"/photos/{photo_id}/outcomes", json=payload)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "version": "1",
        "photo_id": photo_id,
        "outcomes_recorded": 3,
    }

    rows = db.execute(
        text(
            "SELECT label, decision, edited_to_label, bbox, confidence "
            "FROM photo_suggestion_outcomes WHERE photo_id = :pid "
            "ORDER BY label"
        ),
        {"pid": photo_id},
    ).mappings().all()
    assert len(rows) == 3
    by_label = {r["label"]: r for r in rows}

    assert by_label["hex bolt"]["decision"] == "accepted"
    assert by_label["hex bolt"]["edited_to_label"] is None
    assert by_label["hex bolt"]["bbox"] == [0.12, 0.34, 0.40, 0.68]
    assert by_label["hex bolt"]["confidence"] == 0.91

    assert by_label["lamp base"]["decision"] == "rejected"
    assert by_label["lamp base"]["edited_to_label"] is None
    assert by_label["lamp base"]["bbox"] is None

    assert by_label["plastic gear"]["decision"] == "edited"
    assert by_label["plastic gear"]["edited_to_label"] == "brass gear"


# ---------------------------------------------------------------------------
# 2. Idempotent-per-model replace
# ---------------------------------------------------------------------------


def test_post_outcomes_is_idempotent_per_vision_model(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-OUTCOMES-0002")

    payload = _payload(
        [
            {"label": "bolt", "shown_at": "2026-04-17T19:00:00Z", "decision": "accepted"},
            {"label": "nut", "shown_at": "2026-04-17T19:00:00Z", "decision": "rejected"},
        ]
    )

    assert client.post(f"/photos/{photo_id}/outcomes", json=payload).status_code == 200
    assert client.post(f"/photos/{photo_id}/outcomes", json=payload).status_code == 200

    count = db.execute(
        text("SELECT COUNT(*) FROM photo_suggestion_outcomes WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert count == 2, "second identical POST must replace, not accumulate"


# ---------------------------------------------------------------------------
# 3. Per-model isolation — different vision_model coexists
# ---------------------------------------------------------------------------


def test_post_outcomes_preserves_other_models(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-OUTCOMES-0003")

    payload_a = _payload(
        [{"label": "bolt", "shown_at": "2026-04-17T19:00:00Z", "decision": "accepted"}],
        vision_model="model-A",
    )
    payload_b = _payload(
        [
            {"label": "washer", "shown_at": "2026-04-17T19:05:00Z", "decision": "rejected"},
            {"label": "screw", "shown_at": "2026-04-17T19:05:00Z", "decision": "ignored"},
        ],
        vision_model="model-B",
    )

    assert client.post(f"/photos/{photo_id}/outcomes", json=payload_a).status_code == 200
    assert client.post(f"/photos/{photo_id}/outcomes", json=payload_b).status_code == 200

    counts = dict(
        db.execute(
            text(
                "SELECT vision_model, COUNT(*) "
                "FROM photo_suggestion_outcomes WHERE photo_id = :pid "
                "GROUP BY vision_model"
            ),
            {"pid": photo_id},
        ).all()
    )
    assert counts == {"model-A": 1, "model-B": 2}


# ---------------------------------------------------------------------------
# 4. 404 on missing photo — no rows written
# ---------------------------------------------------------------------------


def test_post_outcomes_404_on_missing_photo(client, db):
    before = db.execute(text("SELECT COUNT(*) FROM photo_suggestion_outcomes")).scalar()

    payload = _payload(
        [{"label": "x", "shown_at": "2026-04-17T19:00:00Z", "decision": "accepted"}]
    )
    r = client.post("/photos/999999/outcomes", json=payload)
    assert r.status_code == 404, r.text

    after = db.execute(text("SELECT COUNT(*) FROM photo_suggestion_outcomes")).scalar()
    assert before == after


# ---------------------------------------------------------------------------
# 5. Validation: 'edited' without edited_to_label
# ---------------------------------------------------------------------------


def test_post_outcomes_422_on_edited_without_edited_to_label(
    client, db, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-OUTCOMES-0005")

    payload = _payload(
        [
            {
                "label": "plastic gear",
                "shown_at": "2026-04-17T19:00:00Z",
                "decision": "edited",
            }
        ]
    )
    r = client.post(f"/photos/{photo_id}/outcomes", json=payload)
    assert r.status_code in (400, 422), r.text

    count = db.execute(
        text("SELECT COUNT(*) FROM photo_suggestion_outcomes WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# 6. Validation: bbox wrong length
# ---------------------------------------------------------------------------


def test_post_outcomes_422_on_bbox_wrong_length(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-OUTCOMES-0006")

    payload = _payload(
        [
            {
                "label": "bolt",
                "bbox": [0.1, 0.2, 0.3],
                "shown_at": "2026-04-17T19:00:00Z",
                "decision": "accepted",
            }
        ]
    )
    r = client.post(f"/photos/{photo_id}/outcomes", json=payload)
    assert r.status_code in (400, 422), r.text

    count = db.execute(
        text("SELECT COUNT(*) FROM photo_suggestion_outcomes WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# 7. Empty decisions — 200 no-op
# ---------------------------------------------------------------------------


def test_post_outcomes_empty_decisions_list_is_accepted_as_noop(
    client, db, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-OUTCOMES-0007")

    payload = _payload([])
    r = client.post(f"/photos/{photo_id}/outcomes", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["outcomes_recorded"] == 0

    count = db.execute(
        text("SELECT COUNT(*) FROM photo_suggestion_outcomes WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# 8. Decoupling: photo_group_items untouched
# ---------------------------------------------------------------------------


def test_post_outcomes_does_not_touch_photo_group_items(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-OUTCOMES-0008")

    before = db.execute(
        text("SELECT COUNT(*) FROM photo_group_items WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()

    payload = _payload(
        [
            {"label": "a", "shown_at": "2026-04-17T19:00:00Z", "decision": "accepted"},
            {"label": "b", "shown_at": "2026-04-17T19:00:00Z", "decision": "rejected"},
        ]
    )
    r = client.post(f"/photos/{photo_id}/outcomes", json=payload)
    assert r.status_code == 200

    after = db.execute(
        text("SELECT COUNT(*) FROM photo_group_items WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert before == after


# ---------------------------------------------------------------------------
# 9. All 4 decision values accepted in one post
# ---------------------------------------------------------------------------


def test_post_outcomes_survives_all_decision_kinds(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-OUTCOMES-0009")

    payload = _payload(
        [
            {"label": "a1", "shown_at": "2026-04-17T19:00:00Z", "decision": "accepted"},
            {"label": "r1", "shown_at": "2026-04-17T19:00:00Z", "decision": "rejected"},
            {
                "label": "e1",
                "shown_at": "2026-04-17T19:00:00Z",
                "decision": "edited",
                "edited_to_label": "e1-edited",
            },
            {"label": "i1", "shown_at": "2026-04-17T19:00:00Z", "decision": "ignored"},
        ]
    )
    r = client.post(f"/photos/{photo_id}/outcomes", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["outcomes_recorded"] == 4

    decisions = db.execute(
        text(
            "SELECT decision FROM photo_suggestion_outcomes "
            "WHERE photo_id = :pid ORDER BY decision"
        ),
        {"pid": photo_id},
    ).scalars().all()
    assert decisions == ["accepted", "edited", "ignored", "rejected"]
