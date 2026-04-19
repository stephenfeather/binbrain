-- Migration: 2026-04-19_add_sessions_table.sql
-- ApiDev_008 (Q-session-id closeout) — server-assigned session lifecycle.
--
-- Replaces the implicit, client-supplied `photos.session_id text` column with
-- a real lifecycle table. Clients now POST /sessions to open, ingest photos
-- under that session_id, and DELETE /sessions/{id} to close.
--
-- The `photos.session_id` column itself stays `text` for backward-compat with
-- Phase 1 rows (nullable, no validation). New writes go through /ingest
-- validation against this table. Type migration to `uuid` is deferred
-- (non-goal per plan — would require a backfill audit on legacy rows).
--
-- The AFTER INSERT/DELETE trigger on photos maintains a denormalized
-- photo_count for fast Settings display. Trigger footgun: a bare RETURN NEW
-- in a DELETE trigger silently cancels the DELETE; use RETURN COALESCE(NEW,
-- OLD) so OLD is returned on DELETE and NEW on INSERT (see prior FEAT-3
-- review for the same footgun).
--
-- Idempotent: IF NOT EXISTS on table + indexes, CREATE OR REPLACE on the
-- trigger function, DROP TRIGGER IF EXISTS before re-creating.

BEGIN;

CREATE TABLE IF NOT EXISTS sessions (
    session_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    api_key_id  bigint NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    started_at  timestamptz NOT NULL DEFAULT now(),
    ended_at    timestamptz,
    label       text,
    photo_count int NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS sessions_api_key_idx
    ON sessions (api_key_id, started_at DESC);

CREATE INDEX IF NOT EXISTS sessions_open_idx
    ON sessions (api_key_id) WHERE ended_at IS NULL;

-- Trigger: maintain sessions.photo_count as photos are inserted/deleted.
-- photos.session_id is text; sessions.session_id is uuid — cast at join time.
-- Silently ignore rows whose session_id fails UUID coercion (legacy Phase 1
-- rows may carry non-UUID strings); they were never joined with a session
-- and shouldn't count.
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
            WHERE session_id = sid;
        EXCEPTION WHEN invalid_text_representation THEN
            -- Legacy non-UUID value: ignore.
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
    -- Footgun guard: AFTER DELETE sees NEW as NULL; bare RETURN NEW would
    -- cancel the delete. COALESCE yields OLD on DELETE and NEW on INSERT.
    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS photos_update_session_photo_count ON photos;
CREATE TRIGGER photos_update_session_photo_count
    AFTER INSERT OR DELETE ON photos
    FOR EACH ROW
    EXECUTE FUNCTION sessions_update_photo_count();

COMMIT;
