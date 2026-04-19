-- Migration: 2026-04-19c_document_photo_count_trigger.sql
-- ApiDev_008c SEC-36-4 — documentation-only re-application of
-- ``sessions_update_photo_count`` with an in-body comment block describing
-- the two valid close-mid-ingest interleavings, and a ``COMMENT ON FUNCTION``
-- so DBAs inspecting pg_proc / psql ``\df+`` see the design rationale.
--
-- Behavior is UNCHANGED from 2026-04-19b_fix_session_photo_count_closed_guard.
-- This migration only adds commentary — do NOT drop or edit the prior
-- migration. Idempotent (CREATE OR REPLACE + COMMENT ON FUNCTION).

BEGIN;

CREATE OR REPLACE FUNCTION sessions_update_photo_count()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    sid uuid;
    -- ---------------------------------------------------------------------
    -- ApiDev_008b/008c — close-mid-ingest race design.
    --
    -- /ingest validates the session is open then, separately, inserts the
    -- photo. A sibling DELETE /sessions/{id} can land between those two
    -- steps. Two valid interleavings:
    --
    --   (A) ingest-wins: DELETE lands AFTER the photo INSERT committed.
    --       The INSERT already incremented photo_count (session was still
    --       open at trigger time). On DELETE the session's ended_at is
    --       set, but photo_count remains the historical accurate snapshot
    --       of "photos that were on this session while it was open".
    --
    --   (B) close-wins: DELETE lands BEFORE the photo INSERT fires the
    --       trigger. The ``AND ended_at IS NULL`` guard below skips the
    --       photo_count bump. The photo row still persists on the closed
    --       session (append-only — never lose user data). photo_count
    --       stays at its pre-close value.
    --
    -- Design choice (per architect, plan back-ported): late writes LAND,
    -- but never gain photo_count. Under this contract, photo_count is
    -- the count of photos that arrived while the session was open —
    -- a metric, not a referential-integrity invariant. Routes that need
    -- strict in-session photo accounting should FOR UPDATE the session
    -- row at insert time; none do today.
    --
    -- DELETE branch: GREATEST(... , 0) floors photo_count to prevent
    -- negative values when a photo row pre-dates its session (e.g. a
    -- photo whose session was never open at insert time is later
    -- deleted). Legacy non-UUID session_id values short-circuit via the
    -- ``EXCEPTION WHEN invalid_text_representation`` guard so a
    -- Phase-1-era DELETE never aborts the underlying operation.
    -- ---------------------------------------------------------------------
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
    -- Footgun guard (FEAT-3 lesson): AFTER DELETE sees NEW as NULL; a
    -- bare RETURN NEW silently cancels the delete. COALESCE yields OLD
    -- on DELETE and NEW on INSERT.
    RETURN COALESCE(NEW, OLD);
END;
$$;

COMMENT ON FUNCTION sessions_update_photo_count() IS
    'AFTER INSERT OR DELETE trigger on photos. Maintains denormalized '
    'sessions.photo_count. INSERT branch skips closed sessions '
    '(AND ended_at IS NULL) so late writes from the /ingest validate-then-'
    'insert race still land but do not gain photo_count. See '
    'migrations/2026-04-19c_document_photo_count_trigger.sql for the '
    'full design rationale and the two valid race interleavings.';

COMMIT;
