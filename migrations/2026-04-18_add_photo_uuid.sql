-- Migration: 2026-04-18_add_photo_uuid.sql
-- FEAT-6 — additive photo_uuid column on photos.
--
-- Rationale: stable cross-environment identifier for export paths.
-- ``photo_id`` (bigint) stays as the internal PK for FK performance
-- across the many child tables (photo_labels, photo_detections,
-- photo_detection_groups, photo_group_items, photo_suggestion_outcomes,
-- vision_calls). Externally-facing export flows that need a
-- non-enumerable, non-reallocatable handle use ``photo_uuid`` instead.
--
-- Idempotent: ``ADD COLUMN IF NOT EXISTS``, ``CREATE UNIQUE INDEX IF NOT EXISTS``,
-- and a guarded backfill. Safe to re-apply.

BEGIN;

ALTER TABLE photos
    ADD COLUMN IF NOT EXISTS photo_uuid uuid;

-- Backfill existing rows before tightening constraints. gen_random_uuid()
-- is built-in since PostgreSQL 13. No-op on fresh DBs / already-populated.
UPDATE photos
    SET photo_uuid = gen_random_uuid()
    WHERE photo_uuid IS NULL;

ALTER TABLE photos
    ALTER COLUMN photo_uuid SET NOT NULL,
    ALTER COLUMN photo_uuid SET DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX IF NOT EXISTS photos_photo_uuid_uq
    ON photos (photo_uuid);

COMMIT;
