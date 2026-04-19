-- ApiDev_idempotency_outcomes: server-side Idempotency-Key dedup for
-- POST /photos/{photo_id}/outcomes. Closes the loop on binbrain-ios PR #26
-- F-6 (SEC-26-3): the iOS client already sends Idempotency-Key; this table
-- lets the server honor it with SHA-256(raw body) binding so a replay with
-- a mutated body cannot silently overwrite the stored response.
--
-- Composite PK (api_key_id, key) gives per-tenant isolation: two distinct
-- api_keys can reuse the same key string without colliding (test 6).
-- response_body is jsonb because the outcomes endpoint returns JSON only;
-- if a future endpoint opts in with non-JSON responses, this column needs
-- to widen to bytea.
--
-- Cleanup strategy is lazy-on-write (per-partition DELETE on the hot write
-- path, scoped to the current api_key_id) — see repository docstrings.
BEGIN;

CREATE TABLE IF NOT EXISTS idempotency_records (
    api_key_id      bigint      NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    key             text        NOT NULL,
    body_sha256     bytea       NOT NULL,
    response_status int         NOT NULL,
    response_body   jsonb       NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (api_key_id, key)
);

CREATE INDEX IF NOT EXISTS idempotency_records_created_at_idx
    ON idempotency_records (created_at);

COMMENT ON TABLE idempotency_records IS
    'Per-api_key replay cache for endpoints opting into Idempotency-Key header. '
    'Key+body-hash binding (SEC-26-3): replays with the same key but a different '
    'body_sha256 return 409; matching replays return the stored response. TTL 24h, '
    'cleanup lazy-on-write scoped to the current api_key_id. '
    'See migrations/2026-04-19d_idempotency_records.sql for design rationale.';

COMMIT;
