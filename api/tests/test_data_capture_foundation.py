"""Dev2_016: Data Capture Foundation — Phase 1.

Covers the three unanimous gaps from the Data Capture Synthesis report:

* B — ``photos.width`` / ``photos.height`` populated at ``/ingest`` time
* J — ``photos.session_id`` (client-supplied, nullable, opaque, length-capped)
* C — ``photo_detections.prompt_version`` stamped by the VLM ``/suggest`` path

All DB-touching tests piggyback on the existing conftest fixtures (``client``,
``db``, ``valid_jpeg_bytes``) and the autouse TRUNCATE between tests.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jpeg_bytes(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), "red").save(buf, format="JPEG")
    return buf.getvalue()


def _png_bytes(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), "blue").save(buf, format="PNG")
    return buf.getvalue()


def _seed_photo(client, body_bytes, bin_id: str, *, mime: str = "image/jpeg", ext: str = "jpg") -> int:
    r = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": (f"photo.{ext}", body_bytes, mime)},
    )
    assert r.status_code == 200, r.text
    return r.json()["photos"][0]["photo_id"]


def _fake_describe(hits, elapsed: int = 42):
    calls = {"n": 0}

    def fn(*a, **kw):
        calls["n"] += 1
        return (hits, elapsed)

    return calls, fn


# ---------------------------------------------------------------------------
# 1-2. Width / height
# ---------------------------------------------------------------------------


def test_ingest_persists_image_dimensions(client, db):
    """/ingest reads the placed file with PIL and stores width/height."""
    body = _png_bytes(200, 300)
    r = client.post(
        "/ingest",
        data={"bin_id": "BIN-DIMS-0001"},
        files={"photos": ("photo.png", body, "image/png")},
    )
    assert r.status_code == 200, r.text
    photo_id = r.json()["photos"][0]["photo_id"]

    row = db.execute(
        text("SELECT width, height FROM photos WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).mappings().one()
    assert row["width"] == 200
    assert row["height"] == 300


def test_ingest_records_null_dimensions_when_pil_fails(client, db, monkeypatch, caplog):
    """If PIL.Image.open raises for the dims read, /ingest still succeeds and
    stores NULL dims.

    The column capture is best-effort — failure must NOT break ingest, and a
    warning must be logged so the regression is observable.

    Patch ``app.routes.bins.Image`` (the module-local reference) with a stub
    so validation — which uses its own ``PIL.Image`` import — stays intact and
    only the dims read in /ingest is affected.
    """
    import app.routes.bins as bins_mod

    class _BoomImage:
        @staticmethod
        def open(path, *args, **kwargs):
            raise OSError("simulated decode failure")

    monkeypatch.setattr(bins_mod, "Image", _BoomImage)

    with caplog.at_level("WARNING", logger="app.deps"):
        r = client.post(
            "/ingest",
            data={"bin_id": "BIN-DIMS-0002"},
            files={"photos": ("photo.jpg", _jpeg_bytes(10, 10), "image/jpeg")},
        )

    assert r.status_code == 200, r.text
    photo_id = r.json()["photos"][0]["photo_id"]

    row = db.execute(
        text("SELECT width, height FROM photos WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).mappings().one()
    assert row["width"] is None
    assert row["height"] is None

    assert any("ingest_dims_failed" in rec.getMessage() for rec in caplog.records), (
        "expected an ingest_dims_failed warning log"
    )


# ---------------------------------------------------------------------------
# 3-5. session_id behaviour
# ---------------------------------------------------------------------------


def test_ingest_persists_session_id_when_provided(client, db, valid_jpeg_bytes):
    r = client.post(
        "/ingest",
        data={"bin_id": "BIN-SESS-0001", "session_id": "sess-abc123"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    photo_id = r.json()["photos"][0]["photo_id"]

    sid = db.execute(
        text("SELECT session_id FROM photos WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert sid == "sess-abc123"


def test_ingest_session_id_is_optional(client, db, valid_jpeg_bytes):
    r = client.post(
        "/ingest",
        data={"bin_id": "BIN-SESS-0002"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    photo_id = r.json()["photos"][0]["photo_id"]

    sid = db.execute(
        text("SELECT session_id FROM photos WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalar()
    assert sid is None


def test_ingest_rejects_oversized_session_id(client, db, valid_jpeg_bytes):
    before = db.execute(text("SELECT COUNT(*) FROM photos")).scalar()
    r = client.post(
        "/ingest",
        data={"bin_id": "BIN-SESS-0003", "session_id": "x" * 200},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 400, r.text
    after = db.execute(text("SELECT COUNT(*) FROM photos")).scalar()
    assert before == after, "no photo row should be inserted when validation fails"


def test_ingest_rejects_nonprintable_session_id(client, db, valid_jpeg_bytes):
    before = db.execute(text("SELECT COUNT(*) FROM photos")).scalar()
    r = client.post(
        "/ingest",
        data={"bin_id": "BIN-SESS-0004", "session_id": "\x00bad"},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 400, r.text
    after = db.execute(text("SELECT COUNT(*) FROM photos")).scalar()
    assert before == after


# ---------------------------------------------------------------------------
# 6-7. prompt_version on the /suggest Fireworks path
# ---------------------------------------------------------------------------


def test_suggest_persists_prompt_version(client, db, monkeypatch, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-PV-0001")
    hits = [{"name": "Widget", "category": "tool", "confidence": 0.9, "bbox": [0.1, 0.2, 0.3, 0.4]}]
    _, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    r = client.get(f"/photos/{photo_id}/suggest")
    assert r.status_code == 200, r.text

    rows = db.execute(
        text("SELECT prompt_version FROM photo_detections WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalars().all()
    assert rows, "expected at least one detection row"
    assert all(v == "v2" for v in rows), rows


def test_suggest_cached_rows_prompt_version_roundtrip(client, db, monkeypatch, valid_jpeg_bytes):
    """Dev2_014 cache-hit path: the second call is served from the DB, but the
    persisted rows written on the first call must still carry ``prompt_version = 'v2'``
    and the cached branch must still function.
    """
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-PV-0002")
    hits = [{"name": "Cached", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    calls, fn = _fake_describe(hits)
    monkeypatch.setattr("app.routes.photos.describe_photo", fn)

    first = client.get(f"/photos/{photo_id}/suggest").json()
    second = client.get(f"/photos/{photo_id}/suggest").json()

    assert calls["n"] == 1
    assert first["cached"] is False
    assert second["cached"] is True

    rows = db.execute(
        text("SELECT prompt_version FROM photo_detections WHERE photo_id = :pid"),
        {"pid": photo_id},
    ).scalars().all()
    assert rows == ["v2"]


# ---------------------------------------------------------------------------
# 8. YOLO /detect does NOT stamp prompt_version
# ---------------------------------------------------------------------------


def test_yolo_detection_writes_null_prompt_version(client, db, monkeypatch, valid_jpeg_bytes):
    """The YOLO writer in app.routes.photos.detect_for_photo calls
    ``insert_photo_detections(db, photo_id, model, detections)`` with no
    ``prompt_version`` kwarg. The new optional default (None) must produce
    NULL rows so the column honestly marks these as "not a VLM prompt run".
    """
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-PV-YOLO-0001")

    # Conftest patches photos_route.detect to return []; swap in a stub with rows.
    import app.routes.photos as photos_route

    stub_rows = [{"label": "bolt", "category": "fastener", "confidence": 0.88,
                  "bbox": [0.10, 0.10, 0.40, 0.40]}]
    monkeypatch.setattr(photos_route, "detect", lambda photo_path: stub_rows)
    monkeypatch.setattr(photos_route, "get_model_name", lambda: "yoloe-stub")

    r = client.post(f"/photos/{photo_id}/detect")
    assert r.status_code == 200, r.text

    rows = db.execute(
        text(
            "SELECT prompt_version FROM photo_detections "
            "WHERE photo_id = :pid AND model = :model"
        ),
        {"pid": photo_id, "model": "yoloe-stub"},
    ).scalars().all()
    assert rows == [None]
