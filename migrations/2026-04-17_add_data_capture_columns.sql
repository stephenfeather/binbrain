-- Migration: 2026-04-17_add_data_capture_columns.sql
-- Phase 1 of the Data Capture Foundation (see
-- thoughts/shared/data_capture_synthesis_2026-04-17.md). Closes three
-- unanimous gaps from the synthesis report in one coordinated change:
--
--   * photos.width / photos.height — pixel dimensions of the stored photo,
--     captured at /ingest via PIL after EXIF rotation. Unblocks back-mapping
--     normalized bboxes to high-res training crops without an O(n files)
--     read-time cost.
--
--   * photos.session_id — opaque client-supplied string grouping multiple
--     photos taken in a single cataloging session (gap J, metadata side).
--     iOS will wire this up later; server accepts it now.
--
--   * photo_detections.prompt_version — the vision prompt revision that
--     produced each row (sourced from app.services.vision._PROMPT_VERSION).
--     Without this column, "is prompt v2 better than v1?" is unanswerable
--     from data.
--
-- All three columns are nullable. Pre-existing rows stay NULL — no backfill:
--   * Per Stephen, there are no large permanent installs requiring retro
--     dimension reads.
--   * Pre-instrumentation detections honestly do not know which prompt
--     produced them; NULL > stamping an incorrect "v1".
--
-- Idempotent (ADD COLUMN IF NOT EXISTS). Safe to re-apply.

BEGIN;

ALTER TABLE photos
    ADD COLUMN IF NOT EXISTS width      integer,
    ADD COLUMN IF NOT EXISTS height     integer,
    ADD COLUMN IF NOT EXISTS session_id text;

ALTER TABLE photo_detections
    ADD COLUMN IF NOT EXISTS prompt_version text;

COMMIT;
