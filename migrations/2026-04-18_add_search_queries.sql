-- Migration: 2026-04-18_add_search_queries.sql
-- Dev2_019 — /search relevance floor + zero-result telemetry.
--
-- Adds the ``search_queries`` append-only telemetry table. One row per
-- ``/search`` invocation (every call — not just zero-result), carrying the
-- effective min_score and result_count. Zero-result queries are identified
-- by ``result_count = 0`` rather than living in a separate table; writing
-- every call lets us calibrate the floor after the fact ("what fraction of
-- queries at threshold X returned zero?").
--
-- Companion to Gap #11 in the data-capture assessment. Paired with the
-- server-side default ``SEARCH_DEFAULT_MIN_SCORE`` env var (default 0.35)
-- applied by ``api/app/routes/items.py::search`` when the client omits the
-- ``min_score`` query param. ``min_score_effective`` records the value
-- actually used for the call, regardless of its source (client param vs.
-- env default) so history stays interpretable if the default changes.
--
-- Append-only: no DELETE + INSERT replace pattern; history matters for
-- calibration. Same discipline as ``vision_calls`` (Dev2_018).
--
-- Indexes cover the two queries we expect to run:
--   1. "show recent zero-result queries" — filtered by result_count=0
--      ordered by created_at desc.
--   2. "count queries per day at each floor" — grouped by created_at,
--      min_score_effective.
--
-- Idempotent: IF NOT EXISTS guards on every DDL statement. Safe to re-apply.

BEGIN;

CREATE TABLE IF NOT EXISTS search_queries (
    id                    bigserial PRIMARY KEY,
    request_id            text,
    q                     text NOT NULL,
    qvec_dims             integer NOT NULL,
    min_score_effective   double precision NOT NULL,
    result_count          integer NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS search_queries_created_at_idx
    ON search_queries (created_at);
CREATE INDEX IF NOT EXISTS search_queries_result_count_idx
    ON search_queries (result_count);

COMMIT;
