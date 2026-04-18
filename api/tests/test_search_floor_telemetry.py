"""Dev2_019 — /search relevance floor + zero-result telemetry.

Covers two server-only changes:

1. Env-driven default ``SEARCH_DEFAULT_MIN_SCORE`` (float, default 0.35)
   applied in :func:`app.routes.items.search` when the ``min_score`` query
   param is omitted. Explicit query values still win.
2. Append-only ``search_queries`` telemetry table — one row per ``/search``
   invocation, carrying the effective min_score and result_count so future
   calibration can answer "at floor X, what fraction of queries returned
   zero results?" without trawling log files.

Telemetry is best-effort: a write failure must never surface as a
user-facing error.
"""

from __future__ import annotations

from unittest import mock

from sqlalchemy import text


def _seed_items(client, names: list[str]) -> None:
    for name in names:
        r = client.post("/items", json={"name": name, "category": "test"})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 1. Default floor is applied server-side when min_score is omitted
# ---------------------------------------------------------------------------


def test_search_applies_default_min_score_when_param_omitted(client, db):
    _seed_items(client, ["Alpha Widget"])

    # The stub embedder returns identical vectors so cosine score == 1.0.
    # A default floor of 0.999 would therefore still return the row. We
    # monkeypatch the env to a floor above any possible score (1.01) and
    # confirm the server-applied default filters results to zero.
    with mock.patch.dict("os.environ", {"SEARCH_DEFAULT_MIN_SCORE": "1.01"}):
        resp = client.get("/search", params={"q": "Alpha Widget", "limit": 10})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["min_score"] == 1.01, "server should report the effective default"
    assert body["results"] == []


def test_search_explicit_min_score_overrides_default(client, db):
    _seed_items(client, ["Beta Widget"])

    with mock.patch.dict("os.environ", {"SEARCH_DEFAULT_MIN_SCORE": "1.01"}):
        resp = client.get(
            "/search",
            params={"q": "Beta Widget", "limit": 10, "min_score": 0.0},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["min_score"] == 0.0
    assert len(body["results"]) >= 1


def test_search_default_min_score_falls_back_to_constant(client, db):
    """With no env override, the documented default (0.35) is applied."""
    _seed_items(client, ["Gamma Widget"])

    # Scrub any inherited env value so we exercise the pure default path.
    import os

    resp = None
    saved = os.environ.pop("SEARCH_DEFAULT_MIN_SCORE", None)
    try:
        resp = client.get("/search", params={"q": "Gamma Widget", "limit": 10})
    finally:
        if saved is not None:
            os.environ["SEARCH_DEFAULT_MIN_SCORE"] = saved

    assert resp is not None
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["min_score"] == 0.35
    # Stub embedder yields score 1.0 > 0.35, so results non-empty.
    assert len(body["results"]) >= 1


# ---------------------------------------------------------------------------
# 2. Telemetry row per /search invocation
# ---------------------------------------------------------------------------


def test_search_writes_search_queries_row_on_each_invocation(client, db):
    _seed_items(client, ["Delta Widget"])

    resp = client.get(
        "/search",
        params={"q": "Delta Widget", "limit": 10, "min_score": 0.2},
    )
    assert resp.status_code == 200, resp.text

    row = (
        db.execute(
            text(
                """
            SELECT q, qvec_dims, min_score_effective, result_count
            FROM search_queries
            ORDER BY id DESC
            LIMIT 1
            """
            )
        )
        .mappings()
        .first()
    )
    assert row is not None, "telemetry row missing"
    assert row["q"] == "Delta Widget"
    assert row["qvec_dims"] == 384
    assert row["min_score_effective"] == 0.2
    assert row["result_count"] >= 1


def test_search_flags_zero_result_queries_with_result_count_zero(client, db):
    _seed_items(client, ["Epsilon Widget"])

    # Floor above any achievable stub score forces zero results without
    # erroring the endpoint.
    with mock.patch.dict("os.environ", {"SEARCH_DEFAULT_MIN_SCORE": "1.01"}):
        resp = client.get("/search", params={"q": "Epsilon Widget", "limit": 10})
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []

    row = (
        db.execute(
            text(
                """
            SELECT q, min_score_effective, result_count
            FROM search_queries
            ORDER BY id DESC
            LIMIT 1
            """
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["q"] == "Epsilon Widget"
    assert row["min_score_effective"] == 1.01
    assert row["result_count"] == 0


def test_search_writes_one_telemetry_row_per_call_append_only(client, db):
    _seed_items(client, ["Zeta Widget"])

    before = db.execute(text("SELECT COUNT(*) FROM search_queries")).scalar_one()
    for _ in range(3):
        r = client.get("/search", params={"q": "Zeta Widget", "limit": 10})
        assert r.status_code == 200, r.text
    after = db.execute(text("SELECT COUNT(*) FROM search_queries")).scalar_one()

    assert after - before == 3, "three calls should produce three append-only rows"


# ---------------------------------------------------------------------------
# 3. Telemetry isolation — must never break the /search response contract
# ---------------------------------------------------------------------------


def test_search_telemetry_write_failure_does_not_break_response(client, db):
    _seed_items(client, ["Eta Widget"])

    from app.db import repository

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated telemetry DB outage")

    with mock.patch.object(repository, "insert_search_query", side_effect=_boom):
        resp = client.get("/search", params={"q": "Eta Widget", "limit": 10})

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) >= 1


# ---------------------------------------------------------------------------
# 4. PR22-01 — malformed env value must fall back to 0.35, not 500
# ---------------------------------------------------------------------------


def test_search_falls_back_to_default_on_malformed_env(client, db):
    """A typo in SEARCH_DEFAULT_MIN_SCORE (e.g. 'not-a-float', empty string,
    European decimal like '0,35') must not 500 the endpoint. The documented
    default 0.35 is applied and a warning is logged."""
    _seed_items(client, ["Theta Widget"])

    for bad_value in ("not-a-float", "", "0,35"):
        with mock.patch.dict("os.environ", {"SEARCH_DEFAULT_MIN_SCORE": bad_value}):
            resp = client.get("/search", params={"q": "Theta Widget", "limit": 10})
        assert resp.status_code == 200, (
            f"malformed env {bad_value!r} should not 500; got {resp.status_code}"
        )
        body = resp.json()
        assert body["min_score"] == 0.35, (
            f"malformed env {bad_value!r} should fall back to 0.35; got {body['min_score']}"
        )
