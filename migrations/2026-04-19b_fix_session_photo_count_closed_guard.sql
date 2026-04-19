-- Migration: 2026-04-19b_fix_session_photo_count_closed_guard.sql
-- ApiDev_008b (QA F-5 follow-up) — defuse the /ingest validate-then-insert
-- TOCTOU. If a session is closed between /ingest's validate_session_for_ingest
-- check and the photo insert, the AFTER INSERT trigger previously incremented
-- photo_count on an already-closed session (semantically wrong — photo_count
-- must reflect only photos that arrived while the session was open).
--
-- Fix: add `AND ended_at IS NULL` to the UPDATE inside the trigger function so
-- a race-winner-after-close silently skips the count bump. The photo row
-- itself still inserts (append-only data is more valuable than perfect count
-- accuracy, per plan discussion). Delete path already behaves correctly —
-- GREATEST(photo_count - 1, 0) is idempotent on rows that never incremented.
--
-- Idempotent via CREATE OR REPLACE FUNCTION. Re-running over a good DB is a
-- no-op.

BEGIN;

CREATE OR REPLACE FUNCTION sessions_update_photo_count()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    sid uuid;
BEGIN
    IF TG_OP = 'INSERT' AND NEW.session_id IS NOT NULL AND NEW.session_id <> '' THEN
        BEGIN
            sid := NEW.session_id::uuid;
            UPDATE sessions
            SET photo_count = photo_count + 1
            WHERE session_id = sid
              AND ended_at IS NULL;
        EXCEPTION WHEN invalid_text_representation THEN
            NULL;
        END;
    ELSIF TG_OP = 'DELETE' AND OLD.session_id IS NOT NULL AND OLD.session_id <> '' THEN
        BEGIN
            sid := OLD.session_id::uuid;
            UPDATE sessions
            SET photo_count = GREATEST(photo_count - 1, 0)
            WHERE session_id = sid;
        EXCEPTION WHEN invalid_text_representation THEN
            NULL;
        END;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;

COMMIT;
