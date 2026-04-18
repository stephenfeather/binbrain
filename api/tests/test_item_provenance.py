"""FEAT-5: item provenance — source_photo_id + source_bbox on BinItemRecord.

Covers the wire-up between ``/photos/{id}/outcomes`` (capture),
``/photos/{id}/confirm`` (item materialization + outcome.item_id stamp),
and ``GET /bins/{bin_id}`` (LEFT JOIN LATERAL to surface the most recent
accepted outcome per item).
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


def _post_outcome(client, photo_id: int, decision_row: dict) -> None:
    payload = {
        "vision_model": "test-vision-model",
        "prompt_version": "v1",
        "decisions": [decision_row],
    }
    r = client.post(f"/photos/{photo_id}/outcomes", json=payload)
    assert r.status_code == 200, r.text


def _confirm(client, photo_id: int, bin_id: str, label: str, category: str) -> int:
    r = client.post(
        f"/photos/{photo_id}/confirm",
        json={
            "version": "1",
            "bin_id": bin_id,
            "selected_groups": [
                {
                    "group_key": f"{label}|{category}",
                    "label": label,
                    "category": category,
                    "quantity": 1,
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["results"][0]["item_id"]


# ---------------------------------------------------------------------------
# Happy path — accepted outcome links, GET /bins surfaces photo_id + bbox
# ---------------------------------------------------------------------------


def test_accepted_outcome_populates_source_photo_and_bbox(client, valid_jpeg_bytes):
    bin_id = "BIN-PROV-0001"
    photo_id = _seed_photo(client, valid_jpeg_bytes, bin_id)
    bbox = [0.10, 0.20, 0.50, 0.60]

    _post_outcome(
        client,
        photo_id,
        {
            "label": "hex bolt",
            "category": "fastener",
            "confidence": 0.91,
            "bbox": bbox,
            "shown_at": "2026-04-17T19:32:01Z",
            "decision": "accepted",
        },
    )
    _confirm(client, photo_id, bin_id, "hex bolt", "fastener")

    resp = client.get(f"/bins/{bin_id}")
    assert resp.status_code == 200
    items = resp.json()["items"]
    match = next((it for it in items if it["name"] == "hex bolt"), None)
    assert match is not None
    assert match["source_photo_id"] == photo_id
    assert match["source_bbox"] == bbox


# ---------------------------------------------------------------------------
# Null for legacy items — item exists but no accepted outcome
# ---------------------------------------------------------------------------


def test_legacy_item_without_outcome_has_null_provenance(client):
    bin_id = "BIN-PROV-LEGACY-0001"
    r = client.post(
        "/items",
        json={"name": "Legacy Thing", "category": "misc", "bin_id": bin_id},
    )
    assert r.status_code == 200

    resp = client.get(f"/bins/{bin_id}")
    assert resp.status_code == 200
    items = resp.json()["items"]
    match = next((it for it in items if it["name"] == "Legacy Thing"), None)
    assert match is not None
    assert match["source_photo_id"] is None
    assert match["source_bbox"] is None


# ---------------------------------------------------------------------------
# Most-recent-accepted wins when the same item accumulates multiple outcomes
# ---------------------------------------------------------------------------


def test_most_recent_accepted_outcome_wins(client, db, valid_jpeg_bytes):
    bin_id = "BIN-PROV-MULTI-0001"

    first_photo = _seed_photo(client, valid_jpeg_bytes, bin_id)
    first_bbox = [0.01, 0.02, 0.03, 0.04]
    _post_outcome(
        client,
        first_photo,
        {
            "label": "widget",
            "category": "gizmo",
            "bbox": first_bbox,
            "shown_at": "2026-04-17T10:00:00Z",
            "decision": "accepted",
        },
    )
    item_id = _confirm(client, first_photo, bin_id, "widget", "gizmo")

    second_photo = _seed_photo(client, valid_jpeg_bytes, bin_id)
    second_bbox = [0.30, 0.40, 0.80, 0.90]
    _post_outcome(
        client,
        second_photo,
        {
            "label": "widget",
            "category": "gizmo",
            "bbox": second_bbox,
            "shown_at": "2026-04-18T10:00:00Z",
            "decision": "accepted",
        },
    )
    # Second /confirm hits the same (label,category) so /insert_item_with_status
    # returns the same item_id; link_suggestion_outcomes_to_item stamps the
    # newly-arrived outcome row as well.
    item_id_again = _confirm(client, second_photo, bin_id, "widget", "gizmo")
    assert item_id_again == item_id

    linked = db.execute(
        text("SELECT COUNT(*) FROM photo_suggestion_outcomes " "WHERE item_id = :item_id"),
        {"item_id": item_id},
    ).scalar_one()
    assert linked == 2

    resp = client.get(f"/bins/{bin_id}")
    assert resp.status_code == 200
    match = next((it for it in resp.json()["items"] if it["name"] == "widget"), None)
    assert match is not None
    assert match["source_photo_id"] == second_photo
    assert match["source_bbox"] == second_bbox


# ---------------------------------------------------------------------------
# 'edited' outcomes link too — bbox + source photo are still the right
# provenance; only the label was rewritten.
# ---------------------------------------------------------------------------


def test_edited_outcome_links_via_edited_to_label(client, db, valid_jpeg_bytes):
    bin_id = "BIN-PROV-EDITED-0001"
    photo_id = _seed_photo(client, valid_jpeg_bytes, bin_id)
    bbox = [0.20, 0.20, 0.40, 0.40]

    _post_outcome(
        client,
        photo_id,
        {
            "label": "plastic gear",
            "category": "part",
            "bbox": bbox,
            "shown_at": "2026-04-17T19:32:01Z",
            "decision": "edited",
            "edited_to_label": "brass gear",
        },
    )
    # User confirms with the EDITED label, not the original.
    item_id = _confirm(client, photo_id, bin_id, "brass gear", "part")

    linked = db.execute(
        text(
            "SELECT item_id FROM photo_suggestion_outcomes "
            "WHERE photo_id = :photo_id AND edited_to_label = :label"
        ),
        {"photo_id": photo_id, "label": "brass gear"},
    ).scalar_one()
    assert linked == item_id

    # But 'edited' outcomes do NOT surface through GET /bins — the projection
    # filters on decision='accepted' so the item_id link alone isn't enough.
    # This is intentional: we only show provenance for suggestions the user
    # actually accepted.
    resp = client.get(f"/bins/{bin_id}")
    match = next(
        (it for it in resp.json()["items"] if it["name"] == "brass gear"),
        None,
    )
    assert match is not None
    assert match["source_photo_id"] is None
    assert match["source_bbox"] is None


# ---------------------------------------------------------------------------
# Rejected / ignored outcomes never link (decision filter in UPDATE)
# ---------------------------------------------------------------------------


def test_rejected_outcome_does_not_link(client, db, valid_jpeg_bytes):
    bin_id = "BIN-PROV-REJECT-0001"
    photo_id = _seed_photo(client, valid_jpeg_bytes, bin_id)

    # User saw the suggestion, rejected it, but then manually adds an item
    # with the same label through a different path.
    _post_outcome(
        client,
        photo_id,
        {
            "label": "random thing",
            "category": "misc",
            "bbox": [0.5, 0.5, 0.6, 0.6],
            "shown_at": "2026-04-17T19:32:01Z",
            "decision": "rejected",
        },
    )
    item_id = _confirm(client, photo_id, bin_id, "random thing", "misc")

    linked = db.execute(
        text("SELECT COUNT(*) FROM photo_suggestion_outcomes " "WHERE item_id = :item_id"),
        {"item_id": item_id},
    ).scalar_one()
    assert linked == 0
