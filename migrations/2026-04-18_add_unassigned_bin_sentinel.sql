-- Migration: 2026-04-18_add_unassigned_bin_sentinel.sql
-- FEAT-3 — sentinel "UNASSIGNED" bin so items don't orphan when their parent
-- bin is soft-deleted (blocks FEAT-4: default-bin views on iOS).
--
-- Today, bin_items.bin_id and photos.bin_id reference bins(bin_id) ON DELETE
-- CASCADE, but bin deletion is soft (bins.deleted_at) so cascades never fire
-- — junction rows persist pointing at a hidden bin and become invisible to
-- the user. The sentinel approach keeps the FK semantics simple: the
-- soft-delete handler (added separately) rewrites bin_items.bin_id to
-- 'UNASSIGNED' before flipping deleted_at, so the items stay reachable
-- through GET /bins/UNASSIGNED.
--
-- This migration only sets up the sentinel + protects it from accidental
-- deletion. The reattribution helper and the eventual DELETE /bins/{bin_id}
-- endpoint live in app code.
--
-- Idempotent: ON CONFLICT DO NOTHING on the INSERT, CREATE OR REPLACE on
-- the trigger function, DROP TRIGGER IF EXISTS before re-creating.
-- Safe to re-apply.

BEGIN;

INSERT INTO bins (bin_id, location_id, deleted_at)
VALUES ('UNASSIGNED', NULL, NULL)
ON CONFLICT (bin_id) DO UPDATE
    SET deleted_at = NULL,
        location_id = NULL;

-- Block any future attempt to remove or rename the sentinel: hard DELETE,
-- soft-delete (UPDATE setting deleted_at), or rename via UPDATE on bin_id
-- (a rename would silently break every code path that hard-codes the
-- 'UNASSIGNED' constant). Other UPDATE shapes — notably location
-- reassignment via UPDATE on location_id — are intentionally allowed so the
-- sentinel can sit under a "Misc" or "Garage" location like any other bin.
-- Raises a recognisable SQLSTATE so route-layer error handlers can map
-- cleanly to a 409/422 response without string-matching.
CREATE OR REPLACE FUNCTION protect_unassigned_bin()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.bin_id = 'UNASSIGNED' THEN
        RAISE EXCEPTION 'cannot delete sentinel UNASSIGNED bin'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.bin_id = 'UNASSIGNED' THEN
        IF NEW.deleted_at IS NOT NULL THEN
            RAISE EXCEPTION 'cannot soft-delete sentinel UNASSIGNED bin'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.bin_id <> OLD.bin_id THEN
            RAISE EXCEPTION 'cannot rename sentinel UNASSIGNED bin'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS bins_protect_unassigned ON bins;
CREATE TRIGGER bins_protect_unassigned
    BEFORE UPDATE OR DELETE ON bins
    FOR EACH ROW
    EXECUTE FUNCTION protect_unassigned_bin();

COMMIT;
