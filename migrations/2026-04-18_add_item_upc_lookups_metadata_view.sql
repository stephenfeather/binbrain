-- Migration: 2026-04-18_add_item_upc_lookups_metadata_view.sql
-- ApiDev2_002b (SEC-24-3) — analytics boundary for item_upc_lookups.
--
-- Excludes raw_response (attacker-influenced, unbounded upstream payload).
-- Analytics / BI readers should query this view, not the base table.
-- Idempotent: CREATE OR REPLACE is safe to re-apply.

BEGIN;

CREATE OR REPLACE VIEW item_upc_lookups_metadata AS
SELECT
    id,
    item_id,
    upc,
    source,
    elapsed_ms,
    created_at
FROM item_upc_lookups;

COMMENT ON VIEW item_upc_lookups_metadata IS
  'Analytics projection of item_upc_lookups excluding raw_response (SEC-24-3). '
  'Use this view for dashboards / reports; base table is engineering-only.';

COMMIT;
