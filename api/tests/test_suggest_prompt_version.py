"""Dev2_016b: /suggest echoes prompt_version at response root.

Closes the lineage gap from Dev2_016: iOS receives vision_model on /suggest
but previously had no prompt_version to round-trip on /outcomes. This suite
covers the fresh-call, cache-hit, NULL-in-DB, and historical-version paths.
"""

from app.services.vision import PROMPT_VERSION
from sqlalchemy import text


def _seed_photo(client, valid_jpeg_bytes, bin_id: str) -> int:
    r = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200
    return r.json()["photos"][0]["photo_id"]


def _fake_describe(hits, elapsed=42):
    def fn(*a, **kw):
        return (hits, elapsed)

    return fn


def test_suggest_response_includes_prompt_version_on_fresh_call(
    client, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-PV-0001")
    hits = [{"name": "Thing", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    monkeypatch.setattr("app.routes.photos.describe_photo", _fake_describe(hits))

    body = client.get(f"/photos/{photo_id}/suggest").json()
    assert body["cached"] is False
    assert "prompt_version" in body
    assert body["prompt_version"] == PROMPT_VERSION


def test_suggest_response_includes_prompt_version_on_cache_hit(
    client, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-PV-0002")
    hits = [{"name": "Thing", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    monkeypatch.setattr("app.routes.photos.describe_photo", _fake_describe(hits))

    first = client.get(f"/photos/{photo_id}/suggest").json()
    second = client.get(f"/photos/{photo_id}/suggest").json()

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["prompt_version"] == PROMPT_VERSION
    assert first["prompt_version"] == second["prompt_version"]


def test_suggest_response_prompt_version_is_null_when_cached_rows_have_null(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-PV-0003")
    hits = [{"name": "Thing", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    monkeypatch.setattr("app.routes.photos.describe_photo", _fake_describe(hits))

    # Prime the cache, then simulate a pre-instrumentation row by nulling the stamp.
    first = client.get(f"/photos/{photo_id}/suggest").json()
    assert first["cached"] is False
    db.execute(
        text("UPDATE photo_detections SET prompt_version = NULL WHERE photo_id = :pid"),
        {"pid": photo_id},
    )
    db.commit()

    cached = client.get(f"/photos/{photo_id}/suggest").json()
    assert cached["cached"] is True
    assert "prompt_version" in cached
    assert cached["prompt_version"] is None


def test_suggest_response_prompt_version_reflects_db_not_constant_on_cache_hit(
    client, db, monkeypatch, valid_jpeg_bytes
):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-PV-0004")
    hits = [{"name": "Thing", "category": "tool", "confidence": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2]}]
    monkeypatch.setattr("app.routes.photos.describe_photo", _fake_describe(hits))

    # Prime the cache, then overwrite the stamp with a historical prompt version.
    client.get(f"/photos/{photo_id}/suggest")
    db.execute(
        text("UPDATE photo_detections SET prompt_version = 'v1' WHERE photo_id = :pid"),
        {"pid": photo_id},
    )
    db.commit()
    assert PROMPT_VERSION != "v1", "test presumes current constant is not 'v1'"

    cached = client.get(f"/photos/{photo_id}/suggest").json()
    assert cached["cached"] is True
    assert cached["prompt_version"] == "v1"
