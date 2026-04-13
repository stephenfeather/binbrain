-- Migration: 2026-04-13_add_photo_device_metadata.sql
-- On-device image pipeline: store iOS-generated metadata (OCR, barcodes,
-- classifications, quality scores) alongside each photo.
--
-- The column is nullable because:
--   - Existing photos have no metadata
--   - Non-iOS clients don't send metadata
--   - Older iOS client versions don't send metadata
--
-- No index initially: this column is write-heavy, read-rarely. Add a GIN
-- index later if queries against metadata become common.

BEGIN;

ALTER TABLE photos
    ADD COLUMN IF NOT EXISTS device_metadata jsonb;

COMMIT;
