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
import threading
from typing import Any

import pytest
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
    assert resp.json()["error"]["code"] == "too_many_open_sessions"


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


# ---------------------------------------------------------------------------
# ApiDev_008b — SEC-35-1 / QA F-1..F-10 follow-ups
# ---------------------------------------------------------------------------


# --- F-1 / SEC-35-1: TOCTOU cap enforcement via single guarded INSERT -----


def test_open_session_cap_holds_under_concurrent_post(app_module, db):
    """Fire N > cap concurrent POST /sessions from the SAME api_key. The
    guarded INSERT...SELECT WHERE should keep the open-session total at or
    below the cap (20). Contract: count_open_sessions(api_key) <= 20 always.

    Uses a dedicated api_key so the assertion isn't polluted by sessions
    created elsewhere in the test run.
    """
    raw, api_key_id = _make_extra_api_key(db)

    cap = 20
    attempts = 30

    def _fire(results: list[int], idx: int) -> None:
        c = TestClient(app_module.app)
        c.headers["X-API-Key"] = raw
        resp = c.post("/sessions", json={})
        results[idx] = resp.status_code

    results = [0] * attempts
    threads = [threading.Thread(target=_fire, args=(results, i)) for i in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every request either succeeded with 201 or was refused with 429 — no
    # 5xx surprises.
    assert set(results) <= {201, 429}, results

    open_count = db.execute(
        text("SELECT COUNT(*) FROM sessions " "WHERE api_key_id = :k AND ended_at IS NULL"),
        {"k": api_key_id},
    ).scalar_one()
    assert open_count <= cap, (
        f"open-session cap breached: {open_count} open for api_key {api_key_id} "
        f"across {attempts} concurrent POSTs (results: {results})"
    )
    # And the cap was actually exercised — at least one 429 means the guard
    # fired, so this isn't a no-op test if the race were to somehow still
    # let everything through.
    assert 429 in results, results


def test_post_session_at_cap_returns_429_with_too_many_open_sessions_code(client):
    """Reinforces the existing 429 test with a body assertion — the
    error.code must be 'too_many_open_sessions' (an endpoint-specific code
    distinct from the global rate-limit middleware's 'rate_limited'), not
    'internal_error'. Regression guard for future code_map / detail changes."""
    for _ in range(20):
        assert client.post("/sessions", json={}).status_code == 201
    resp = client.post("/sessions", json={})
    assert resp.status_code == 429
    body = resp.json()
    assert body["version"] == "1"
    assert body["error"]["code"] == "too_many_open_sessions"


# --- F-4: 404 body equivalence between "not found" and "not yours" --------


def _strip_request_id(body: dict) -> dict:
    """request_id is per-request; masks the enumeration-leak assertion."""
    err = dict(body.get("error", {}))
    err.pop("request_id", None)
    return {**body, "error": err}


def test_delete_sessions_404_body_identical_whether_not_found_or_not_yours(client, db):
    """F-4: status-equality alone doesn't guard the plan's enumeration-leak
    contract. Assert response BODIES match so a future refactor can't add
    e.g. error.message='not yours' without breaking this test."""
    other_raw, _ = _make_extra_api_key(db)
    other = TestClient(client.app)
    other.headers["X-API-Key"] = other_raw
    s = _post_session(other)

    not_yours = client.delete(f"/sessions/{s['session_id']}")
    not_found = client.delete("/sessions/00000000-0000-0000-0000-000000000000")

    assert not_yours.status_code == not_found.status_code == 404
    assert _strip_request_id(not_yours.json()) == _strip_request_id(not_found.json())


def test_get_sessions_404_body_identical_whether_not_found_or_not_yours(client, db):
    other_raw, _ = _make_extra_api_key(db)
    other = TestClient(client.app)
    other.headers["X-API-Key"] = other_raw
    s = _post_session(other)

    not_yours = client.get(f"/sessions/{s['session_id']}")
    not_found = client.get("/sessions/00000000-0000-0000-0000-000000000000")

    assert not_yours.status_code == not_found.status_code == 404
    assert _strip_request_id(not_yours.json()) == _strip_request_id(not_found.json())


# --- F-5: /ingest close-mid-ingest trigger no-op on closed session --------


def test_trigger_skips_photo_count_bump_when_session_closed_between_insert(client, db):
    """QA F-5 / ApiDev_008b: simulate the rare race where /ingest validates
    an open session, then the client (or a sibling tab) closes it, then
    the photo row actually inserts. The AFTER INSERT trigger must NOT
    increment photo_count on a closed session — the photo still lands."""
    s = _post_session(client)
    sid = s["session_id"]

    # Close the session directly (simulating the race winner).
    db.execute(
        text("UPDATE sessions SET ended_at = now() WHERE session_id = CAST(:s AS uuid)"),
        {"s": sid},
    )
    db.commit()

    # Insert a photo that claims the now-closed session. In production this
    # would only happen via a race; here we bypass /ingest validation to
    # exercise the trigger directly.
    db.execute(text("INSERT INTO bins (bin_id) VALUES ('BIN-F5-RACE')"))
    db.execute(
        text(
            "INSERT INTO photos (bin_id, path, session_id) "
            "VALUES ('BIN-F5-RACE', '/tmp/race.jpg', :s)"
        ),
        {"s": sid},
    )
    db.commit()

    # Photo row should exist…
    photo_present = db.execute(
        text("SELECT COUNT(*) FROM photos WHERE bin_id = 'BIN-F5-RACE'")
    ).scalar_one()
    assert photo_present == 1

    # …but photo_count on the closed session should still be 0.
    count = db.execute(
        text("SELECT photo_count FROM sessions WHERE session_id = CAST(:s AS uuid)"),
        {"s": sid},
    ).scalar_one()
    assert count == 0


# --- SEC-35-2: Bidi codepoints blocked in label ----------------------------


@pytest.mark.parametrize(
    "bidi_char",
    [
        "\u202a",  # LEFT-TO-RIGHT EMBEDDING
        "\u202b",  # RIGHT-TO-LEFT EMBEDDING
        "\u202c",  # POP DIRECTIONAL FORMATTING
        "\u202d",  # LEFT-TO-RIGHT OVERRIDE
        "\u202e",  # RIGHT-TO-LEFT OVERRIDE (the big spoofing one)
        "\u2066",  # LEFT-TO-RIGHT ISOLATE
        "\u2067",  # RIGHT-TO-LEFT ISOLATE
        "\u2068",  # FIRST STRONG ISOLATE
        "\u2069",  # POP DIRECTIONAL ISOLATE
    ],
)
def test_post_sessions_rejects_bidi_codepoints_in_label(client, bidi_char):
    """SEC-35-2: each Unicode Bidi override / isolate codepoint is rejected
    with 400 so a malicious owner can't plant a label whose rendered glyphs
    differ from the stored bytes."""
    resp = client.post("/sessions", json={"label": f"harmless{bidi_char}text"})
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize(
    "label",
    [
        "Garage bins 🔧",  # emoji (astral plane)
        "Atelier café à Montréal",  # Latin + combining accents
        "仓库 — 2026年4月",  # CJK + em-dash + ideographic digits
        "Инвентарь: болты и гайки",  # Cyrillic
        "مخزن القطع",  # Arabic (RTL script, no override controls)
        "עברית לְלֹא בִּידי",  # Hebrew (RTL, no control codepoints)
        "🔧⚙️🔩 toolbox",  # consecutive emoji + ASCII
        "100% ✓",  # ASCII + checkmark
    ],
)
def test_post_sessions_accepts_legitimate_unicode_labels(client, label):
    """G-4 (post-review): SEC-35-2's Bidi block must NOT regress legitimate
    multilingual labels. Emoji, accented Latin, CJK, Cyrillic, and bare
    RTL scripts (Arabic / Hebrew) that don't rely on override/isolate
    codepoints must all round-trip cleanly."""
    resp = client.post("/sessions", json={"label": label})
    assert resp.status_code == 201, resp.text
    assert resp.json()["session"]["label"] == label


# --- F-6: GET /sessions list excludes other owners' rows -------------------


def test_get_sessions_list_excludes_other_owners_sessions(client, db):
    mine = [_post_session(client)["session_id"] for _ in range(2)]

    other_raw, _ = _make_extra_api_key(db)
    other = TestClient(client.app)
    other.headers["X-API-Key"] = other_raw
    theirs = [_post_session(other)["session_id"] for _ in range(2)]

    resp = client.get("/sessions", params={"limit": 100})
    assert resp.status_code == 200
    ids = {s["session_id"] for s in resp.json()["sessions"]}
    assert set(mine) <= ids
    assert ids.isdisjoint(set(theirs)), "list_sessions leaked another owner's rows"


# --- F-7: GET /sessions/{id} with malformed UUID -> 404, no txn poisoning --


def test_get_session_malformed_uuid_returns_404_and_does_not_poison_txn(client):
    resp = client.get("/sessions/definitely-not-a-uuid")
    assert resp.status_code == 404
    # Follow-up request must still work — proves rollback happened.
    resp2 = client.get("/sessions", params={"limit": 5})
    assert resp2.status_code == 200


def test_delete_session_malformed_uuid_returns_404_and_does_not_poison_txn(client):
    resp = client.delete("/sessions/not-even-close-to-a-uuid")
    assert resp.status_code == 404
    resp2 = client.get("/sessions", params={"limit": 5})
    assert resp2.status_code == 200


# --- F-8: Trigger spec regression guard ------------------------------------


def test_trigger_definition_documents_update_limitation(db):
    """Regression guard: the AFTER INSERT OR DELETE trigger does NOT cover
    UPDATE. If UPDATE is ever added to photos.session_id (e.g. a future
    text->uuid migration or reassignment flow), this guard must fail so
    the author remembers to extend sessions_update_photo_count() to
    rebalance photo_count across old/new session_id values."""
    spec = (
        db.execute(
            text(
                "SELECT event_manipulation FROM information_schema.triggers "
                "WHERE trigger_name = 'photos_update_session_photo_count' "
                "ORDER BY event_manipulation"
            )
        )
        .scalars()
        .all()
    )
    assert spec == ["DELETE", "INSERT"], (
        "Trigger spec changed — if UPDATE was added, also add a photo_count "
        "rebalance branch to sessions_update_photo_count(). If UPDATE was "
        "removed, this guard is obsolete."
    )


# --- F-9: _validate_label parametrized edges -------------------------------


@pytest.mark.parametrize(
    "raw,expected_status,expected_stored",
    [
        ("", 201, None),
        ("   ", 201, None),
        ("  trimmed  ", 201, "trimmed"),
        ("hello\tworld", 400, None),
        ("hello\nworld", 400, None),
        ("hello\rworld", 400, None),
        ("x" * 120, 201, "x" * 120),
    ],
)
def test_validate_label_edges(client, raw, expected_status, expected_stored):
    resp = client.post("/sessions", json={"label": raw})
    assert resp.status_code == expected_status, resp.text
    if expected_status == 201:
        assert resp.json()["session"]["label"] == expected_stored


# --- F-10: 401 response-body shape pinned ----------------------------------


def test_sessions_routes_401_body_has_unauthorized_code(app_module):
    """F-10: middleware handles 401 directly and MUST emit error.code =
    'unauthorized'. 401 is not in the handler's code_map — if someone
    moves this path through the default HTTPException flow, the body
    would regress to 'internal_error' (see F-3 pattern on 410)."""
    bare = TestClient(app_module.app)
    for method, url in [
        ("POST", "/sessions"),
        ("GET", "/sessions"),
        ("GET", "/sessions/00000000-0000-0000-0000-000000000000"),
        ("DELETE", "/sessions/00000000-0000-0000-0000-000000000000"),
    ]:
        resp = bare.request(method, url)
        assert resp.status_code == 401, f"{method} {url} -> {resp.status_code}"
        body = resp.json()
        assert body.get("version") == "1", body
        assert body["error"]["code"] == "unauthorized", body


# ---------------------------------------------------------------------------
# ApiDev_008c — PR #35 + PR #36 review follow-ups
#   O-4 / G-2 — /ingest four-mode invalid_session body equivalence
#   G-1       — pg_locks-inspecting advisory-lock observability
#   G-3       — endpoint-level F-5 close-mid-ingest race
#   G-5       — Bidi off-by-one adjacent-codepoint accept cases
# ---------------------------------------------------------------------------


# --- O-4 / G-2: /ingest four-mode body equivalence -------------------------


@pytest.mark.parametrize(
    "bad_session_kind",
    ["closed", "not_yours", "unknown_uuid", "malformed_uuid"],
)
def test_ingest_invalid_session_bodies_are_indistinguishable(client, db, bad_session_kind):
    """QA F-4 (PR #35) / re-opened as O-4/G-2 (PR #36): all four /ingest
    invalid_session failure modes MUST produce byte-identical response
    bodies modulo request_id. Any divergence (error.code, error.message,
    or key set) is an enumeration-leak regression — an attacker could
    distinguish "session exists but closed" from "session doesn't exist"
    from "session owned by another key"."""
    if bad_session_kind == "closed":
        s = _post_session(client)
        client.delete(f"/sessions/{s['session_id']}")
        sid = s["session_id"]
    elif bad_session_kind == "not_yours":
        other_raw, _ = _make_extra_api_key(db)
        other = TestClient(client.app)
        other.headers["X-API-Key"] = other_raw
        sid = _post_session(other)["session_id"]
    elif bad_session_kind == "unknown_uuid":
        sid = "00000000-0000-0000-0000-000000000000"
    else:
        sid = "definitely-not-a-uuid"

    resp = _ingest(client, f"BIN-INV-{bad_session_kind[:6]}", sid)
    assert resp.status_code == 400, (bad_session_kind, resp.text)
    body = _strip_request_id(resp.json())
    assert body["error"]["code"] == "invalid_session", body
    assert body["error"]["message"] == ("Session not found, not yours, or already closed"), body
    # After stripping request_id, the error subtree must be exactly
    # {code, message} — no extra leak fields (e.g. "reason").
    assert set(body["error"].keys()) == {"code", "message"}, body


# --- G-1: pg_locks inspector proves the advisory lock is load-bearing ------


def test_open_session_cap_observes_advisory_lock_contention(app_module, db):
    """G-1 (QA PR #36): the 30-thread cap test proves the OUTCOME (cap
    holds, 429 fires) but not the MECHANISM. This test fires concurrent
    POSTs and polls ``pg_locks`` for waiting advisory locks mid-race. If
    a future refactor removes ``pg_advisory_xact_lock`` but keeps the
    guarded INSERT, this test starts failing (the READ-COMMITTED race
    reappears, with no advisory wait visible)."""
    import time

    raw, _api_key_id = _make_extra_api_key(db)

    observed_waits: list[int] = []
    stop = threading.Event()

    def _fire() -> None:
        c = TestClient(app_module.app)
        c.headers["X-API-Key"] = raw
        for _ in range(3):
            c.post("/sessions", json={})

    def _inspect() -> None:
        # ApiDev_008d G-2/SEC-37-1: 1ms poll + 3s deadline. The 5ms/2s
        # combo from PR #37 had symmetric risks — fast dev laptops could
        # complete the 18 POSTs between polls and miss the race, while
        # slow CI runners could miss the 2s deadline and flake.
        # ApiDev_008d G-1/SEC-37-3: classid filter pins this query to the
        # ('session_create', api_key_id) namespace so a future unrelated
        # advisory lock elsewhere in the app cannot false-positive here.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not stop.is_set():
            rows = db.execute(
                text(
                    "SELECT COUNT(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND granted = false "
                    "AND classid::int = hashtext('session_create')"
                )
            ).scalar_one()
            if rows:
                observed_waits.append(int(rows))
                return
            time.sleep(0.001)

    inspector = threading.Thread(target=_inspect)
    threads = [threading.Thread(target=_fire) for _ in range(6)]
    inspector.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    inspector.join()

    assert observed_waits, (
        "No advisory-lock wait observed across 6 concurrent POST /sessions × 3. "
        "Either the lock is too fast to race on this machine, or — more "
        "likely — the lock is no longer being taken. Check "
        "repository.create_session_if_under_cap for the "
        "pg_advisory_xact_lock call."
    )


# --- G-3: endpoint-level F-5 close-mid-ingest race via monkeypatch ---------


def test_ingest_race_close_between_validate_and_insert_lands_photo_and_skips_count(
    client, db, monkeypatch
):
    """G-3 (QA PR #36): end-to-end exercise of the /ingest close-mid-ingest
    race. Monkeypatches ``validate_session_for_ingest`` to stall after it
    returns True, races a ``DELETE /sessions/{id}`` to commit during the
    stall, then lets the insert proceed. Contract (per back-ported plan
    refinement in migration 2026-04-19c): late writes LAND (200), photo
    row persists, sessions.photo_count stays at its pre-close value.

    If the architect ever flips to "reject late writes" (close-wins
    returns 400 invalid_session), flip the status_code assertion below —
    this test becomes the contract-reversal assertion.

    SEC-37-4 (PR #37 review): this test's race geometry depends on the
    current /ingest transaction shape — validate_session_for_ingest
    returns BEFORE the photo INSERT + commit at bins.py:~296, with no
    wrapping ``with db.begin():`` around validate+insert. A future refactor
    that fuses validate + insert into one explicit transaction would block
    the closer's DELETE on a row lock instead of racing cleanly; the
    timing-based assertion here must be revisited if that happens."""
    from app.db import repository as real_repo

    s = _post_session(client)
    sid = s["session_id"]

    validate_returned = threading.Event()
    closer_committed = threading.Event()
    original_validate = real_repo.validate_session_for_ingest

    def slowed_validate(db_, s_id, k_id):
        result = original_validate(db_, s_id, k_id)
        # Only stall on OUR session — other /ingest calls in the test run
        # (e.g. the closer itself hitting /sessions) must be fast.
        if s_id == sid and result:
            validate_returned.set()
            closer_committed.wait(timeout=3.0)
        return result

    monkeypatch.setattr(real_repo, "validate_session_for_ingest", slowed_validate)

    def closer() -> None:
        validate_returned.wait(timeout=3.0)
        # Fresh TestClient in case monkeypatched module state differs.
        closer_client = TestClient(client.app)
        closer_client.headers["X-API-Key"] = client.headers["X-API-Key"]
        r = closer_client.delete(f"/sessions/{sid}")
        assert r.status_code == 200, r.text
        closer_committed.set()

    threading.Thread(target=closer, daemon=True).start()
    resp = _ingest(client, "BIN-RACE-E2E", sid)

    # Contract: late writes land.
    assert resp.status_code == 200, (
        "Late write rejected — if policy changed to reject, flip this "
        f"assertion and the plan doc. Got {resp.status_code}: {resp.text}"
    )

    photos = db.execute(
        text("SELECT COUNT(*) FROM photos WHERE session_id = :s"),
        {"s": sid},
    ).scalar_one()
    assert photos == 1, "Photo row must still land on a closed session"

    count = db.execute(
        text("SELECT photo_count FROM sessions WHERE session_id = CAST(:s AS uuid)"),
        {"s": sid},
    ).scalar_one()
    assert count == 0, "photo_count must NOT be bumped on a closed session"

    ended_at = db.execute(
        text("SELECT ended_at FROM sessions WHERE session_id = CAST(:s AS uuid)"),
        {"s": sid},
    ).scalar_one()
    assert ended_at is not None, "Session must be closed after the race"


# --- G-5: Bidi off-by-one boundary tests -----------------------------------


@pytest.mark.parametrize(
    "adjacent_char",
    [
        "\u202f",  # NARROW NO-BREAK SPACE — just above U+202E
        "\u2065",  # unassigned — just below U+2066
        "\u206a",  # INHIBIT SYMMETRIC SWAPPING — just above U+2069
    ],
)
def test_post_sessions_accepts_codepoints_adjacent_to_bidi_blocklist(client, adjacent_char):
    """G-5 (QA PR #36) / G-3 comment fix (PR #37 QA): guards against a
    WIDENING refactor of the Bidi block (e.g. block expanded to include
    U+202F or another adjacent codepoint). Per-codepoint tests already
    cover narrowing drift (``<=`` → ``<`` would let U+202E / U+2069 escape
    those assertions); this test is the symmetric neighbor-accept
    assertion. U+2029 was removed from this list in ApiDev_008d because
    SEC-37-2 category-Zl/Zp block now rejects it — see
    ``test_post_sessions_rejects_line_and_paragraph_separators``."""
    resp = client.post("/sessions", json={"label": f"label{adjacent_char}tail"})
    assert resp.status_code == 201, (adjacent_char.encode("unicode_escape"), resp.text)


# --- SEC-37-2: Zl/Zp category block (PR #37 aegis) ------------------------


@pytest.mark.parametrize(
    "zlzp_char",
    [
        "\u2028",  # LINE SEPARATOR (category Zl)
        "\u2029",  # PARAGRAPH SEPARATOR (category Zp)
    ],
)
def test_post_sessions_rejects_line_and_paragraph_separators(client, zlzp_char):
    """SEC-37-2 (PR #37 aegis): Unicode category Zl (LINE SEPARATOR,
    U+2028) and Zp (PARAGRAPH SEPARATOR, U+2029) can cause display-layer
    line breaks in renderers that honor them, producing labels that wrap
    oddly or hide trailing content. Category-level block is forward-
    compatible with any future Zl/Zp additions."""
    resp = client.post("/sessions", json={"label": f"harmless{zlzp_char}text"})
    assert resp.status_code == 400, resp.text


def test_post_sessions_accepts_codepoint_adjacent_to_zl_block(client):
    """SEC-37-2 neighbor-accept: U+2027 HYPHENATION POINT (category Po)
    sits immediately below U+2028 Zl. Positive assertion pins the
    boundary so a future widening (e.g. category-Po also blocked, or a
    hex-range expansion catching U+2027) breaks CI. Above-boundary
    neighbor (U+202A) is already rejected by the Bidi block, so only the
    below-boundary neighbor is meaningful here."""
    resp = client.post("/sessions", json={"label": "label\u2027tail"})
    assert resp.status_code == 201, resp.text


# --- SEC-37-3: advisory-lock namespace registry CI guard -------------------


def test_advisory_lock_namespaces_match_repository_registry():
    """SEC-37-3 (PR #37 aegis): the registry comment block at the top of
    repository.py declares every reserved advisory-lock namespace. Until
    this test landed, that was honor-system — a future contributor could
    add a ``pg_advisory_*(hashtext('new_ns'), ...)`` call without
    declaring it, silently risking namespace collision with another
    caller. This test walks ``api/app/**/*.py``, extracts every
    ``pg_advisory_*(hashtext('<ns>'), ...)`` namespace string, and asserts
    each one is declared in the registry comment. Fails loudly on
    undeclared additions."""
    import re
    from pathlib import Path

    app_root = Path(__file__).resolve().parent.parent / "app"
    call_pattern = re.compile(r"pg_advisory_\w+\(\s*hashtext\(\s*'([^']+)'\s*\)")
    found: set[str] = set()
    for py in app_root.rglob("*.py"):
        for match in call_pattern.finditer(py.read_text(encoding="utf-8")):
            found.add(match.group(1))

    registry = (app_root / "db" / "repository.py").read_text(encoding="utf-8")
    # Registry comment block declares reserved namespaces as, e.g.,
    #   ('session_create', api_key_id)
    registry_pattern = re.compile(r"#\s+\('([^']+)',")
    declared: set[str] = set(registry_pattern.findall(registry))

    undeclared = found - declared
    assert not undeclared, (
        f"Advisory-lock namespace(s) {sorted(undeclared)} used in app code "
        f"but NOT declared in the registry comment block in "
        f"api/app/db/repository.py. Declared: {sorted(declared)}. "
        f"Every pg_advisory_* call site must add its namespace to the "
        f"registry before landing (SEC-36-1 / SEC-37-3)."
    )
