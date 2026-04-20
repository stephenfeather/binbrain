-- Migration: 2026-04-19e_idempotency_response_size_cap.sql
-- SEC-42-1 (ApiDev2_014) — bound idempotency_records.response_body storage.
--
-- The table's jsonb column has no size constraint today. Current callers
-- store ~60-byte payloads, but the repository helper is general-purpose;
-- a future opt-in endpoint that echoes user-controlled data could fill
-- the table with multi-MiB rows per (api_key_id, key), bounded only by
-- the 50 MiB request cap and 24h TTL. 64 KiB is ~1000x today's payload
-- and well below any realistic response the route stores.
--
-- Idempotent: ADD CONSTRAINT IF NOT EXISTS is not supported in pg17,
-- so we wrap in a DO block that checks pg_constraint first.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'idempotency_response_size_cap'
          AND conrelid = 'idempotency_records'::regclass
    ) THEN
        ALTER TABLE idempotency_records
            ADD CONSTRAINT idempotency_response_size_cap
            CHECK (octet_length(response_body::text) <= 65536);
    END IF;
END$$;

COMMIT;
