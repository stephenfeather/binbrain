-- Migration: 2026-04-18_add_item_id_to_photo_suggestion_outcomes.sql
-- FEAT-5 — item provenance (source photo + bbox) on BinItemRecord.
--
-- photo_suggestion_outcomes captured the presented-list decisions keyed
-- by (photo_id, label, category), but did not retain the item_id that
-- an 'accepted' decision produced via the /confirm path
-- (outcomes -> photo_group_items -> bin_items -> items). Without that
-- link, BinItemRecord cannot cheaply surface "which photo + bbox did
-- this item originate from?" for the iOS item-detail view.
--
-- This migration adds a nullable item_id FK to outcomes, indexes it for
-- the LEFT JOIN on item_id, and backfills historical rows via an
-- unambiguous (photo_id, label, category) match against
-- photo_group_items. Going forward, /photos/{id}/confirm sets item_id
-- directly as items are materialized.
--
-- item_id is nullable because:
--   * legacy rows (pre-Phase-2) may have no matching photo_group_items
--   * 'rejected' / 'ignored' / 'edited' outcomes never produced an item
--   * ambiguous matches (multiple items share a label on the same photo)
--     are intentionally left null — iOS handles the optional case.
--
-- ON DELETE SET NULL: preserves the historical outcome (training
-- signal) even if the item is later deleted from the catalogue.
--
-- Idempotent: IF NOT EXISTS guards + WHERE item_id IS NULL on backfill.
-- Safe to re-apply on partially-migrated DBs.

BEGIN;

ALTER TABLE photo_suggestion_outcomes
    ADD COLUMN IF NOT EXISTS item_id bigint
        REFERENCES items(item_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS photo_suggestion_outcomes_item_idx
    ON photo_suggestion_outcomes (item_id)
    WHERE item_id IS NOT NULL;

-- Backfill: for every 'accepted' outcome row with no item_id, find the
-- unique item_id that photo_group_items recorded for the same
-- (photo_id, label, category). Skip ambiguous triples (same photo,
-- same label+category mapped to more than one item_id across models)
-- and rows that already have item_id set.
UPDATE photo_suggestion_outcomes o
SET item_id = pgi.item_id
FROM photo_group_items pgi
WHERE o.photo_id = pgi.photo_id
  AND o.label    = pgi.label
  AND o.category IS NOT DISTINCT FROM pgi.category
  AND o.decision = 'accepted'
  AND o.item_id  IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM photo_group_items pgi2
      WHERE pgi2.photo_id = pgi.photo_id
        AND pgi2.label    = pgi.label
        AND pgi2.category IS NOT DISTINCT FROM pgi.category
        AND pgi2.item_id <> pgi.item_id
  );

COMMIT;
