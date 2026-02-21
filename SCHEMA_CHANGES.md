# Schema Changes

## 2026-02-21
- Task 1 (GET /bins/{bin_id}): no database schema changes.
- Task 2 (GET /bins): no database schema changes.
- Task 3: add unique index on bin_items (bin_id, item_id):
  - Migration: `migrations/2026-02-21_add_bin_items_unique.sql`
  - SQL: `CREATE UNIQUE INDEX IF NOT EXISTS bin_items_unique ON bin_items (bin_id, item_id);`
