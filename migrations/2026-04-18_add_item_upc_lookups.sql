-- Migration: 2026-04-18_add_item_upc_lookups.sql
-- ApiDev2_002 — append-only provenance table for /upc/{upc} lookups.
--
-- Closes Gap #7 per thoughts/shared/data_capture_synthesis_2026-04-17.md.
-- One row per lookup attempt (local hit, external success, external failure,
-- unknown). Never UPDATEd — provides the full audit trail of what each source
-- returned at what time. Rationale for a separate table over columns on
-- ``items``: provenance is a *lineage* question (when, from which source,
-- with what raw payload) — columns on ``items`` would get overwritten on
-- re-lookup and lose that history.
--
-- Matches the append-only discipline of ``vision_calls`` (Dev2_018) and
-- ``search_queries`` (Dev2_019).
--
-- Indexes cover the queries we expect to run:
--   1. "recent lookups for this UPC"         — (upc, created_at DESC)
--   2. "all lookups that produced item X"    — (item_id) WHERE item_id IS NOT NULL
--   3. "call volume / quality per source"    — (source, created_at DESC)
--
-- Idempotent: IF NOT EXISTS guards on every DDL statement. Safe to re-apply.

BEGIN;

CREATE TABLE IF NOT EXISTS item_upc_lookups (
    id            bigserial PRIMARY KEY,
    upc           text NOT NULL,
    item_id       bigint REFERENCES items(item_id) ON DELETE SET NULL,
    source        text NOT NULL,
    raw_response  jsonb,
    elapsed_ms    integer,
    created_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT item_upc_lookups_source_check
        CHECK (source IN ('local', 'upcitemdb', 'go-upc', 'unknown'))
);

CREATE INDEX IF NOT EXISTS item_upc_lookups_upc_idx
    ON item_upc_lookups (upc, created_at DESC);

CREATE INDEX IF NOT EXISTS item_upc_lookups_item_id_idx
    ON item_upc_lookups (item_id)
    WHERE item_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS item_upc_lookups_source_created_idx
    ON item_upc_lookups (source, created_at DESC);

COMMIT;
