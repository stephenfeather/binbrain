"""ApiDev_idempotency_outcomes — server-side Idempotency-Key honoring on
``POST /photos/{photo_id}/outcomes``.

Matches iOS PR #26 (Swift2_018b F-6): the client already ships an
``Idempotency-Key: <UUID>`` header; until this feature lands the server
ignores it. Behavior codified here:

- First sighting of ``(api_key_id, key)`` → process normally, store the
  response keyed by ``(api_key_id, key, SHA-256(raw body))``.
- Replay with matching body → return stored response + ``X-Idempotent-Replay: true``.
- Replay with a DIFFERENT body → 409 ``idempotency_key_mismatch`` (SEC-26-3).
- Different keys with identical body → both land.
- Malformed key → 400 ``invalid_idempotency_key``.
- Missing header → no storage, behavior unchanged.
- TTL 24h, lazy-cleanup on write.
- Composite PK ``(api_key_id, key)`` so cross-tenant keys don't collide.
- Thread race: two simultaneous POSTs with the same K+B → exactly one
  replace_photo_suggestion_outcomes run, the other gets the stored body.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


def _seed_photo(client, valid_jpeg_bytes, bin_id: str) -> int:
    r = client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    return r.json()["photos"][0]["photo_id"]


def _payload(decisions: list[dict] | None = None) -> dict:
    if decisions is None:
        decisions = [
            {
                "label": "widget",
                "confidence": 0.91,
                "shown_at": "2026-04-19T19:32:01Z",
                "decision": "accepted",
            }
        ]
    return {
        "vision_model": "accounts/fireworks/models/qwen3p6-plus",
        "prompt_version": "v2",
        "decisions": decisions,
    }


def _new_key() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 1. Happy path — idempotent replay returns stored body + replay header
# ---------------------------------------------------------------------------


def test_first_post_stores_response_and_replay_returns_it(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-IDEMP-0001")
    payload = _payload()
    key = _new_key()

    r1 = client.post(
        f"/photos/{photo_id}/outcomes",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 200, r1.text
    # First hit MUST NOT carry the replay header — downstream observability
    # relies on the header being present only on stored-response returns.
    assert r1.headers.get("X-Idempotent-Replay") != "true"

    r2 = client.post(
        f"/photos/{photo_id}/outcomes",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert r2.headers.get("X-Idempotent-Replay") == "true"

    # Exactly one idempotency_records row, exactly one DB write observable via
    # photo_suggestion_outcomes row count (the second POST must not re-DELETE +
    # re-INSERT, otherwise concurrent analytics scans would see a gap).
    stored = db.execute(
        text("SELECT COUNT(*) FROM idempotency_records"),
    ).scalar()
    assert stored == 1, f"expected 1 idempotency row, got {stored}"

    outcome_rows = db.execute(
        text(
            "SELECT COUNT(*) FROM photo_suggestion_outcomes "
            "WHERE photo_id = :pid"
        ),
        {"pid": photo_id},
    ).scalar()
    assert outcome_rows == len(payload["decisions"])


# ---------------------------------------------------------------------------
# 2. Body mismatch — same key, different body → 409, state unchanged
# ---------------------------------------------------------------------------


def test_same_key_different_body_returns_409(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-IDEMP-0002")
    payload_a = _payload(
        [
            {
                "label": "widget",
                "confidence": 0.80,
                "shown_at": "2026-04-19T19:32:01Z",
                "decision": "accepted",
            }
        ]
    )
    payload_b = _payload(
        [
            {
                "label": "widget",
                "confidence": 0.80,
                "shown_at": "2026-04-19T19:32:01Z",
                # Mutation that must surface as 409, not silent overwrite.
                "decision": "rejected",
            }
        ]
    )
    key = _new_key()

    r1 = client.post(
        f"/photos/{photo_id}/outcomes",
        json=payload_a,
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 200

    r2 = client.post(
        f"/photos/{photo_id}/outcomes",
        json=payload_b,
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["error"]["code"] == "idempotency_key_mismatch"
    assert "request_id" in body["error"]

    # State-unchanged guard: still exactly one accepted row, no rejected row.
    decisions = db.execute(
        text(
            "SELECT decision FROM photo_suggestion_outcomes "
            "WHERE photo_id = :pid"
        ),
        {"pid": photo_id},
    ).scalars().all()
    assert decisions == ["accepted"]


# ---------------------------------------------------------------------------
# 3. Different keys, same body → both proceed
# ---------------------------------------------------------------------------


def test_different_keys_same_body_both_land(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-IDEMP-0003")
    payload = _payload()
    key1 = _new_key()
    key2 = _new_key()
    assert key1 != key2

    assert client.post(
        f"/photos/{photo_id}/outcomes",
        json=payload,
        headers={"Idempotency-Key": key1},
    ).status_code == 200
    assert client.post(
        f"/photos/{photo_id}/outcomes",
        json=payload,
        headers={"Idempotency-Key": key2},
    ).status_code == 200

    stored = db.execute(
        text("SELECT COUNT(*) FROM idempotency_records"),
    ).scalar()
    assert stored == 2


# ---------------------------------------------------------------------------
# 4. TTL — 25h-old row is lazily cleaned up; new POST proceeds fresh
# ---------------------------------------------------------------------------


def test_expired_record_is_cleaned_lazily(client, db, valid_jpeg_bytes, test_api_key):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-IDEMP-0004")
    payload = _payload()
    key = _new_key()

    # Resolve api_key_id the same way the auth middleware does so the
    # partition we're backdating matches the one the endpoint will target.
    key_hash = hashlib.sha256(test_api_key.encode()).hexdigest()
    api_key_id = db.execute(
        text("SELECT id FROM api_keys WHERE key_hash = :h"),
        {"h": key_hash},
    ).scalar_one()

    # Plant an expired row (25h ago) directly in the table. The body_sha256
    # here is deliberately garbage — a correctly implemented lazy cleanup
    # wipes it before any mismatch check fires, so the next POST treats it
    # as a new request and stores a fresh response under the same key.
    db.execute(
        text(
            "INSERT INTO idempotency_records "
            "(api_key_id, key, body_sha256, response_status, response_body, created_at) "
            "VALUES (:k_id, :key, :hash, :status, :body, now() - interval '25 hours')"
        ),
        {
            "k_id": api_key_id,
            "key": key,
            "hash": b"\x00" * 32,
            "status": 200,
            "body": json.dumps({"stale": True}),
        },
    )
    db.commit()

    r = client.post(
        f"/photos/{photo_id}/outcomes",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r.status_code == 200
    # Response is fresh, not the stale body.
    assert r.json() != {"stale": True}

    # Exactly one row survives — the fresh one, not the expired one.
    rows = db.execute(
        text(
            "SELECT created_at < now() - interval '1 hour' AS is_stale "
            "FROM idempotency_records WHERE api_key_id = :k",
        ),
        {"k": api_key_id},
    ).scalars().all()
    assert rows == [False]


# ---------------------------------------------------------------------------
# 5. Missing header — regression guard: header is optional
# ---------------------------------------------------------------------------


def test_missing_idempotency_key_header_writes_no_record(client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-IDEMP-0005")
    payload = _payload()

    r = client.post(f"/photos/{photo_id}/outcomes", json=payload)
    assert r.status_code == 200
    # No header, no dedup row, no replay header — endpoint behaves exactly
    # like the pre-ApiDev_idempotency_outcomes shape.
    assert r.headers.get("X-Idempotent-Replay") != "true"
    stored = db.execute(
        text("SELECT COUNT(*) FROM idempotency_records"),
    ).scalar()
    assert stored == 0


# ---------------------------------------------------------------------------
# 6. Api-key isolation — two tenants reusing the same key string don't dedup
# ---------------------------------------------------------------------------


def test_same_key_different_api_keys_do_not_cross_dedup(
    app_module, db, valid_jpeg_bytes, test_api_key, user_api_key
):
    # Both keys post under the same Idempotency-Key string but the composite
    # PK (api_key_id, key) means neither can see the other's record.
    c_admin = TestClient(app_module.app)
    c_admin.headers["X-API-Key"] = test_api_key
    c_user = TestClient(app_module.app)
    c_user.headers["X-API-Key"] = user_api_key

    photo_id = _seed_photo(c_admin, valid_jpeg_bytes, "BIN-IDEMP-0006")
    payload = _payload()
    shared_key = _new_key()

    r1 = c_admin.post(
        f"/photos/{photo_id}/outcomes",
        json=payload,
        headers={"Idempotency-Key": shared_key},
    )
    assert r1.status_code == 200
    # Not a replay — this api_key has never seen this key before.
    assert r1.headers.get("X-Idempotent-Replay") != "true"

    r2 = c_user.post(
        f"/photos/{photo_id}/outcomes",
        json=payload,
        headers={"Idempotency-Key": shared_key},
    )
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") != "true"

    stored = db.execute(
        text("SELECT COUNT(*) FROM idempotency_records"),
    ).scalar()
    assert stored == 2


# ---------------------------------------------------------------------------
# 7. Malformed key — 400 invalid_idempotency_key, parametrized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    [
        "not-a-uuid",
        "",
        "12345",
        "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",  # non-hex
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8 ",  # trailing whitespace
    ],
)
def test_malformed_idempotency_key_returns_400(bad_key, client, db, valid_jpeg_bytes):
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-IDEMP-0007")

    r = client.post(
        f"/photos/{photo_id}/outcomes",
        json=_payload(),
        headers={"Idempotency-Key": bad_key},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_idempotency_key"
    # No domain write, no idempotency row.
    assert (
        db.execute(text("SELECT COUNT(*) FROM idempotency_records")).scalar()
        == 0
    )
    assert (
        db.execute(
            text("SELECT COUNT(*) FROM photo_suggestion_outcomes WHERE photo_id = :pid"),
            {"pid": photo_id},
        ).scalar()
        == 0
    )


# ---------------------------------------------------------------------------
# 8. Concurrency — true thread race against a live test DB (PR #24 lesson)
# ---------------------------------------------------------------------------


def test_concurrent_posts_with_same_key_race_one_winner(
    app_module, valid_jpeg_bytes, test_api_key
):
    """Two threads POST K+B at the same moment. Exactly one must run the
    domain write (one row in photo_suggestion_outcomes for this photo_id),
    and both clients must observe the same successful response body.

    Uses a real ThreadPoolExecutor against the live test DB, not sequential
    retries. If the advisory lock / ON CONFLICT guard fails, the test
    observes either (a) two domain writes, (b) a 409 raised inside the
    would-be winner, or (c) a deadlock hang — all of which fail cleanly.
    """
    from app.deps import SessionLocal, engine

    # Seed photo via a dedicated client (not part of the race).
    seed_client = TestClient(app_module.app)
    seed_client.headers["X-API-Key"] = test_api_key
    photo_id = _seed_photo(seed_client, valid_jpeg_bytes, "BIN-IDEMP-0008")

    payload = _payload()
    key = _new_key()
    barrier = threading.Barrier(2)

    def fire() -> tuple[int, dict, str | None]:
        # One TestClient per thread; TestClient is a thin wrapper over
        # requests.Session and is not safe to share across threads here.
        c = TestClient(app_module.app)
        c.headers["X-API-Key"] = test_api_key
        barrier.wait(timeout=5)
        r = c.post(
            f"/photos/{photo_id}/outcomes",
            json=payload,
            headers={"Idempotency-Key": key},
        )
        return r.status_code, r.json(), r.headers.get("X-Idempotent-Replay")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(fire)
        f2 = pool.submit(fire)
        results = [f1.result(timeout=15), f2.result(timeout=15)]

    statuses = [s for s, _, _ in results]
    bodies = [b for _, b, _ in results]
    assert statuses == [200, 200], f"expected both 200, got {statuses}: {bodies}"
    assert bodies[0] == bodies[1], f"response bodies diverged: {bodies}"

    replay_flags = [flag for _, _, flag in results]
    # Exactly one thread should see the replay header — the loser. The winner
    # ran the domain write; the loser read back the stored response.
    # (A tie at the SQL level is impossible: one txn acquires the advisory
    # lock first, the other blocks then sees the inserted row.)
    assert replay_flags.count("true") == 1, (
        f"expected exactly one X-Idempotent-Replay=true, got {replay_flags}"
    )

    # Domain write ran exactly once.
    db = SessionLocal()
    try:
        outcome_rows = db.execute(
            text(
                "SELECT COUNT(*) FROM photo_suggestion_outcomes WHERE photo_id = :pid"
            ),
            {"pid": photo_id},
        ).scalar()
        idem_rows = db.execute(
            text("SELECT COUNT(*) FROM idempotency_records"),
        ).scalar()
    finally:
        db.close()

    assert outcome_rows == len(payload["decisions"])
    assert idem_rows == 1


# ---------------------------------------------------------------------------
# 9. Canonical body is RAW BYTES, not re-serialized JSON. Enforces §4 of the
#    prompt: a future dev who switches to ``json.dumps(payload.dict())``
#    breaks this test.
# ---------------------------------------------------------------------------


def test_hash_is_raw_bytes_not_json_canonicalization(client, valid_jpeg_bytes):
    """Two requests with semantically equal JSON but different byte layouts
    (whitespace / key order) produce different SHA-256 hashes → second hit
    under the same key returns 409.
    """
    photo_id = _seed_photo(client, valid_jpeg_bytes, "BIN-IDEMP-0009")
    key = _new_key()

    # Two representations of the same logical payload, byte-different.
    body_a = (
        '{"vision_model":"accounts/fireworks/models/qwen3p6-plus",'
        '"prompt_version":"v2",'
        '"decisions":[{"label":"widget","confidence":0.5,'
        '"shown_at":"2026-04-19T19:32:01Z","decision":"accepted"}]}'
    )
    body_b = (
        '{ "vision_model": "accounts/fireworks/models/qwen3p6-plus", '
        '"prompt_version": "v2", '
        '"decisions": [ {"label": "widget", "confidence": 0.5, '
        '"shown_at": "2026-04-19T19:32:01Z", "decision": "accepted"} ] }'
    )

    r1 = client.post(
        f"/photos/{photo_id}/outcomes",
        content=body_a,
        headers={
            "Idempotency-Key": key,
            "Content-Type": "application/json",
        },
    )
    assert r1.status_code == 200, r1.text

    r2 = client.post(
        f"/photos/{photo_id}/outcomes",
        content=body_b,
        headers={
            "Idempotency-Key": key,
            "Content-Type": "application/json",
        },
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_key_mismatch"
