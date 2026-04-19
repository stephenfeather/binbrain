-- Migration: 2026-04-19_add_client_retry_count.sql
-- ApiDev2_005 (Swift2b-γ offline outcomes). Adds telemetry for
-- client-side offline-queue pressure on the fire-and-forget
-- /photos/{id}/outcomes endpoint. Populated from the
-- X-Client-Retry-Count request header.
--
-- No index: analytics groups by retry bucket, never filters.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Safe to re-apply.

BEGIN;

ALTER TABLE photo_suggestion_outcomes
    ADD COLUMN IF NOT EXISTS client_retry_count int;

COMMENT ON COLUMN photo_suggestion_outcomes.client_retry_count IS
    'Client-reported retry attempt count when this outcome was finally delivered. '
    'NULL for historical rows. 0 for first-attempt success. Higher values indicate '
    'offline-queue pressure on the client. Populated from X-Client-Retry-Count '
    'request header.';

COMMIT;
