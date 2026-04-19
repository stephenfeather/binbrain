"""ApiDev_008 — /sessions routes + /ingest session validation + trigger.

Plan: thoughts/shared/plans/2026-04-19-session-id-explicit-boundary.md

Server-assigned session lifecycle:
- POST /sessions            -> 201 {session_id, started_at, label}, 429 if >20 open
- DELETE /sessions/{id}     -> 200 {..., ended_at, photo_count}, 404 / 410
- GET  /sessions/{id}       -> 200 / 404 (404 also on not-yours)
- GET  /sessions            -> paginated, ?state=open|closed|all
- POST /ingest session_id   -> 400 invalid_session on bad/closed/not-yours
- Trigger:                  -> AFTER INSERT OR DELETE on photos maintains
                               sessions.photo_count (floors at 0).
"""

from __future__ import annotations

import hashlib
import io
import secrets
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(buf, format="JPEG")
    return buf.getvalue()


def _make_extra_api_key(db, *, role: str = "admin") -> tuple[str, int]:
    """Create a second API key (distinct from test_api_key) so we can test
    cross-owner 404 enumeration-leak paths. Returns (raw_key, api_key_id)."""
    raw = "bb_other_" + secrets.token_urlsafe(24)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    row = db.execute(
        text(
            "INSERT INTO api_keys (key_hash, name, role) "
            "VALUES (:h, 'other-owner', :r) RETURNING id"
        ),
        {"h": key_hash, "r": role},
    ).scalar_one()
    db.commit()
    return raw, int(row)


def _post_session(client: TestClient, label: str | None = None) -> dict[str, Any]:
    body = {"label": label} if label is not None else {}
    resp = client.post("/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]


# ---------------------------------------------------------------------------
# POST /sessions
# ---------------------------------------------------------------------------


def test_post_sessions_returns_201_with_session_id_and_started_at(client):
    resp = client.post("/sessions", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body.get("version") == "1"
    s = body["session"]
    assert "session_id" in s and len(s["session_id"]) == 36
    assert "started_at" in s
    assert s["label"] is None


def test_post_sessions_accepts_and_echoes_label(client):
    s = _post_session(client, label="Garage bins 2026-04-19")
    assert s["label"] == "Garage bins 2026-04-19"


def test_post_sessions_rejects_label_over_120_chars(client):
    resp = client.post("/sessions", json={"label": "x" * 121})
    assert resp.status_code == 400, resp.text


def test_post_sessions_rejects_label_with_control_chars(client):
    resp = client.post("/sessions", json={"label": "bad\x00null"})
    assert resp.status_code == 400, resp.text


def test_post_sessions_429_when_caller_has_over_20_open_sessions(client):
    for _ in range(20):
        assert client.post("/sessions", json={}).status_code == 201
    # 21st open session must be refused.
    resp = client.post("/sessions", json={})
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "rate_limited"


# ---------------------------------------------------------------------------
# DELETE /sessions/{id}
# ---------------------------------------------------------------------------


def test_delete_sessions_happy_path_sets_ended_at(client):
    s = _post_session(client)
    resp = client.delete(f"/sessions/{s['session_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()["session"]
    assert body["session_id"] == s["session_id"]
    assert body["ended_at"] is not None
    assert body["photo_count"] == 0


def test_delete_sessions_404_when_not_found(client):
    # Well-formed UUID, but no row.
    resp = client.delete("/sessions/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404, resp.text


def test_delete_sessions_404_when_owned_by_another_api_key(client, db):
    other_raw, other_id = _make_extra_api_key(db)
    other_client = TestClient(client.app)
    other_client.headers["X-API-Key"] = other_raw
    s = _post_session(other_client)
    # Original caller must see 404 (don't leak existence).
    resp = client.delete(f"/sessions/{s['session_id']}")
    assert resp.status_code == 404, resp.text


def test_delete_sessions_410_when_already_closed(client):
    s = _post_session(client)
    first = client.delete(f"/sessions/{s['session_id']}")
    assert first.status_code == 200
    second = client.delete(f"/sessions/{s['session_id']}")
    assert second.status_code == 410, second.text


# ---------------------------------------------------------------------------
# GET /sessions/{id}
# ---------------------------------------------------------------------------


def test_get_session_by_id_returns_caller_own_row(client):
    s = _post_session(client, label="inspection-run")
    resp = client.get(f"/sessions/{s['session_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()["session"]
    assert body["session_id"] == s["session_id"]
    assert body["label"] == "inspection-run"
    assert body["ended_at"] is None
    assert body["photo_count"] == 0


def test_get_session_by_id_404_when_not_yours(client, db):
    other_raw, _ = _make_extra_api_key(db)
    other_client = TestClient(client.app)
    other_client.headers["X-API-Key"] = other_raw
    s = _post_session(other_client)
    resp = client.get(f"/sessions/{s['session_id']}")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# GET /sessions (list + filters + pagination)
# ---------------------------------------------------------------------------


def test_get_sessions_default_returns_all_of_callers(client):
    created = [_post_session(client)["session_id"] for _ in range(3)]
    resp = client.get("/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [s["session_id"] for s in body["sessions"]]
    for sid in created:
        assert sid in ids


def test_get_sessions_orders_started_at_desc(client):
    first = _post_session(client)
    second = _post_session(client)
    resp = client.get("/sessions")
    assert resp.status_code == 200
    rows = resp.json()["sessions"]
    positions = {r["session_id"]: i for i, r in enumerate(rows)}
    assert positions[second["session_id"]] < positions[first["session_id"]]


def test_get_sessions_state_open_filters_out_closed(client):
    open_s = _post_session(client)
    closed_s = _post_session(client)
    assert client.delete(f"/sessions/{closed_s['session_id']}").status_code == 200

    resp = client.get("/sessions", params={"state": "open"})
    assert resp.status_code == 200
    ids = [s["session_id"] for s in resp.json()["sessions"]]
    assert open_s["session_id"] in ids
    assert closed_s["session_id"] not in ids


def test_get_sessions_state_closed_only_returns_closed(client):
    open_s = _post_session(client)
    closed_s = _post_session(client)
    assert client.delete(f"/sessions/{closed_s['session_id']}").status_code == 200

    resp = client.get("/sessions", params={"state": "closed"})
    assert resp.status_code == 200
    ids = [s["session_id"] for s in resp.json()["sessions"]]
    assert closed_s["session_id"] in ids
    assert open_s["session_id"] not in ids


def test_get_sessions_pagination_limit_and_offset(client):
    created = [_post_session(client)["session_id"] for _ in range(5)]

    page1 = client.get("/sessions", params={"limit": 2, "offset": 0})
    page2 = client.get("/sessions", params={"limit": 2, "offset": 2})
    assert page1.status_code == 200
    assert page2.status_code == 200

    ids1 = {s["session_id"] for s in page1.json()["sessions"]}
    ids2 = {s["session_id"] for s in page2.json()["sessions"]}
    assert len(ids1) == 2 and len(ids2) == 2
    assert ids1.isdisjoint(ids2)
    # Every id on either page should come from what we created in this test.
    assert ids1 | ids2 <= set(created)


def test_get_sessions_clamps_limit_to_100(client):
    resp = client.get("/sessions", params={"limit": 500})
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# /ingest session validation
# ---------------------------------------------------------------------------


def _ingest(client: TestClient, bin_id: str, session_id: str | None) -> Any:
    data: dict[str, str] = {"bin_id": bin_id}
    if session_id is not None:
        data["session_id"] = session_id
    files = [("photos", ("p.jpg", _jpeg_bytes(), "image/jpeg"))]
    return client.post("/ingest", data=data, files=files)


def test_ingest_with_valid_open_session_writes_photo_and_increments_count(client, db):
    s = _post_session(client)
    resp = _ingest(client, "BIN-SESS-A", s["session_id"])
    assert resp.status_code == 200, resp.text

    photo_ids = [p["photo_id"] for p in resp.json()["photos"]]
    assert len(photo_ids) == 1

    stored = db.execute(
        text("SELECT session_id FROM photos WHERE photo_id = :p"),
        {"p": photo_ids[0]},
    ).scalar_one()
    assert stored == s["session_id"]

    count = db.execute(
        text("SELECT photo_count FROM sessions WHERE session_id = :s"),
        {"s": s["session_id"]},
    ).scalar_one()
    assert count == 1


def test_ingest_with_closed_session_returns_400_invalid_session(client):
    s = _post_session(client)
    assert client.delete(f"/sessions/{s['session_id']}").status_code == 200

    resp = _ingest(client, "BIN-SESS-B", s["session_id"])
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_session"


def test_ingest_with_someone_elses_session_returns_same_400_invalid_session(client, db):
    other_raw, _ = _make_extra_api_key(db)
    other_client = TestClient(client.app)
    other_client.headers["X-API-Key"] = other_raw
    s = _post_session(other_client)

    resp = _ingest(client, "BIN-SESS-C", s["session_id"])
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_session"


def test_ingest_without_session_id_still_works(client):
    resp = _ingest(client, "BIN-SESS-D", session_id=None)
    assert resp.status_code == 200, resp.text


def test_ingest_with_unknown_uuid_returns_400_invalid_session(client):
    resp = _ingest(client, "BIN-SESS-E", session_id="00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_session"


# ---------------------------------------------------------------------------
# Trigger — direct DB insert/delete on photos
# ---------------------------------------------------------------------------


def test_trigger_increments_photo_count_on_insert(client, db):
    s = _post_session(client)
    db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-TRIG-I')"))
    db.execute(
        text(
            "INSERT INTO photos (bin_id, path, session_id) "
            "VALUES ('BIN-TRIG-I', '/tmp/p.jpg', :s)"
        ),
        {"s": s["session_id"]},
    )
    db.commit()

    count = db.execute(
        text("SELECT photo_count FROM sessions WHERE session_id = :s"),
        {"s": s["session_id"]},
    ).scalar_one()
    assert count == 1


def test_trigger_decrements_photo_count_on_delete_and_floors_at_zero(client, db):
    s = _post_session(client)
    db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-TRIG-D')"))
    db.execute(
        text(
            "INSERT INTO photos (bin_id, path, session_id) "
            "VALUES ('BIN-TRIG-D', '/tmp/p1.jpg', :s)"
        ),
        {"s": s["session_id"]},
    )
    db.commit()

    assert (
        db.execute(
            text("SELECT photo_count FROM sessions WHERE session_id = :s"),
            {"s": s["session_id"]},
        ).scalar_one()
        == 1
    )

    db.execute(
        text("DELETE FROM photos WHERE bin_id = 'BIN-TRIG-D' AND session_id = :s"),
        {"s": s["session_id"]},
    )
    db.commit()

    count = db.execute(
        text("SELECT photo_count FROM sessions WHERE session_id = :s"),
        {"s": s["session_id"]},
    ).scalar_one()
    assert count == 0

    # A second delete against an already-zero session must floor at 0, not go
    # negative. Insert a row outside the session, then delete it — the delete
    # should be a no-op for this session's count.
    db.execute(
        text(
            "INSERT INTO photos (bin_id, path, session_id) "
            "VALUES ('BIN-TRIG-D', '/tmp/p2.jpg', NULL)"
        )
    )
    db.execute(text("DELETE FROM photos WHERE bin_id = 'BIN-TRIG-D' AND session_id IS NULL"))
    db.commit()
    assert (
        db.execute(
            text("SELECT photo_count FROM sessions WHERE session_id = :s"),
            {"s": s["session_id"]},
        ).scalar_one()
        == 0
    )


def test_trigger_ignores_non_uuid_legacy_session_ids(client, db):
    # A Phase 1 row with a legacy non-UUID session_id must not blow up the
    # trigger — it should be a silent no-op.
    db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-LEGACY')"))
    db.execute(
        text(
            "INSERT INTO photos (bin_id, path, session_id) "
            "VALUES ('BIN-LEGACY', '/tmp/legacy.jpg', 'legacy-nonuuid-xyz')"
        )
    )
    db.commit()

    # No exception implies the trigger handled invalid_text_representation.
    photo_id = db.execute(
        text("SELECT photo_id FROM photos WHERE session_id = 'legacy-nonuuid-xyz'")
    ).scalar_one()
    assert photo_id is not None


# ---------------------------------------------------------------------------
# Auth requirement
# ---------------------------------------------------------------------------


def test_sessions_routes_require_api_key(app_module):
    bare = TestClient(app_module.app)
    for method, url in [
        ("POST", "/sessions"),
        ("GET", "/sessions"),
        ("GET", "/sessions/00000000-0000-0000-0000-000000000000"),
        ("DELETE", "/sessions/00000000-0000-0000-0000-000000000000"),
    ]:
        resp = bare.request(method, url)
        assert resp.status_code == 401, f"{method} {url} -> {resp.status_code}"


# ---------------------------------------------------------------------------
# QA PR #35 follow-ups
#   F-2 — Trigger AFTER DELETE on legacy non-UUID / empty session_id.
#   F-3 — 410 already-closed response body uses stable "session_closed" code.
# ---------------------------------------------------------------------------


def test_trigger_allows_delete_of_legacy_non_uuid_photo_row(client, db):
    """Phase-1 photos.session_id can be any text. The AFTER DELETE trigger
    must ignore non-UUID values silently, not block the DELETE."""
    db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-LEGACY-DEL')"))
    db.execute(
        text(
            "INSERT INTO photos (bin_id, path, session_id) "
            "VALUES ('BIN-LEGACY-DEL', '/tmp/legacy-del.jpg', 'pre-uuid-client-token')"
        )
    )
    db.commit()

    photo_id = db.execute(
        text("SELECT photo_id FROM photos WHERE session_id = 'pre-uuid-client-token'")
    ).scalar_one()

    # Must NOT raise — a regression in the DELETE-branch EXCEPTION handler
    # would propagate invalid_text_representation and abort.
    db.execute(text("DELETE FROM photos WHERE photo_id = :p"), {"p": photo_id})
    db.commit()

    assert (
        db.execute(
            text("SELECT COUNT(*) FROM photos WHERE photo_id = :p"), {"p": photo_id}
        ).scalar_one()
        == 0
    )


def test_trigger_allows_delete_of_empty_string_session_id(client, db):
    """Defensive: the trigger's <> '' guard should also cover deletes.
    Empty-string session_id is not expected in production but has been seen
    in test fixtures historically."""
    db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-EMPTY-DEL')"))
    db.execute(
        text(
            "INSERT INTO photos (bin_id, path, session_id) "
            "VALUES ('BIN-EMPTY-DEL', '/tmp/empty.jpg', '')"
        )
    )
    db.commit()
    # Must not raise.
    db.execute(text("DELETE FROM photos WHERE bin_id = 'BIN-EMPTY-DEL'"))
    db.commit()


def test_delete_sessions_410_body_uses_session_closed_code(client):
    """QA F-3: 410 response body must carry a stable, meaningful error code
    so idempotent clients don't alert on what is by contract a benign
    no-op (double-close from offline queue retry)."""
    s = _post_session(client)
    assert client.delete(f"/sessions/{s['session_id']}").status_code == 200
    resp = client.delete(f"/sessions/{s['session_id']}")
    assert resp.status_code == 410
    body = resp.json()
    assert body["version"] == "1"
    assert body["error"]["code"] == "session_closed", body
    assert "already" in body["error"]["message"].lower()
