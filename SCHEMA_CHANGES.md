# Schema Changes

## 2026-02-21
- Task 1 (GET /bins/{bin_id}): no database schema changes.
- Task 2 (GET /bins): no database schema changes.
- Task 3: add unique index on bin_items (bin_id, item_id):
  - Migration: `migrations/2026-02-21_add_bin_items_unique.sql`
  - SQL: `CREATE UNIQUE INDEX IF NOT EXISTS bin_items_unique ON bin_items (bin_id, item_id);`

## 2026-02-21
- Task 5 (POST /bins/{bin_id}/add): no database schema changes.

## 2026-02-21
- Task 7 (GET /search pagination): no database schema changes.

## 2026-02-21
- Task 8 (GET /search min_score): no database schema changes.

## 2026-02-21
- Refactor 1 (repository layer): no database schema changes.

## 2026-02-21
- Refactor 2 (structured logging): no database schema changes.

## 2026-02-21
- Testing strategy: no database schema changes.

## 2026-02-21
- /items upsert: no database schema changes.

## 2026-02-21
- Task 4 (soft delete): add deleted_at columns to bins and items.
  - Migration: `migrations/2026-02-21_add_soft_delete.sql`

## 2026-02-21
- Task 6 (photo_labels table):
  - Migration: `migrations/2026-02-21_add_photo_labels.sql`

## 2026-02-21
- DB health check + structured error responses: no database schema changes.

## 2026-02-21
- QR label generator (PDF): no database schema changes.

## 2026-02-21
- Photo suggestions endpoint: no database schema changes.

## 2026-02-21
- /ingest response shape: no database schema changes.

## 2026-02-21
- Photo detect endpoint: no database schema changes.

## 2026-02-21
- Photo groups endpoint: no database schema changes.

## 2026-02-21
- Persisted detections and groups: added photo_detections and photo_detection_groups.
  - Migration: `migrations/2026-02-21_add_photo_detections.sql`
