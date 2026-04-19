# Schema Changes

## 2026-04-19
- ApiDev_idempotency_outcomes (SEC-26-3): new `idempotency_records` table —
  composite PK `(api_key_id, key)`, `body_sha256 bytea`, `response_status int`,
  `response_body jsonb`, `created_at timestamptz`. Powers server-side
  Idempotency-Key dedup on `POST /photos/{id}/outcomes` with 24h TTL and
  lazy-on-write cleanup scoped to the current api_key.
  - Migration: `migrations/2026-04-19d_idempotency_records.sql`

- ApiDev2_005 (Swift2b-γ offline outcomes): add `client_retry_count int` to
  `photo_suggestion_outcomes`. Populated from `X-Client-Retry-Count` request
  header on `POST /photos/{id}/outcomes`. NULL for historical rows;
  `0` for first-attempt success.
  - Migration: `migrations/2026-04-19_add_client_retry_count.sql`

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

## 2026-02-21
- Photo confirm endpoint + audit table: added photo_group_items.
  - Migration: `migrations/2026-02-21_add_photo_group_items.sql`

## 2026-04-19
- ApiDev_008 Q-session-id explicit boundary: new `sessions` table (uuid PK `gen_random_uuid()`, `api_key_id bigint` FK → `api_keys(id)` ON DELETE CASCADE, `started_at`, `ended_at`, `label`, denormalized `photo_count`) + two indexes (`(api_key_id, started_at DESC)`, partial `(api_key_id) WHERE ended_at IS NULL`). Adds `AFTER INSERT OR DELETE` trigger on `photos` — `sessions_update_photo_count()` — maintaining `photo_count` with `RETURN COALESCE(NEW, OLD)` so DELETE firings don't short-circuit the underlying row operation. Trigger silently ignores legacy non-UUID `photos.session_id` values via `EXCEPTION WHEN invalid_text_representation`. `photos.session_id` stays `text` (Phase 1 compat); type migration to `uuid` is deferred. Idempotent (IF NOT EXISTS, CREATE OR REPLACE, DROP TRIGGER IF EXISTS).
  - Migration: `migrations/2026-04-19_add_sessions_table.sql`
