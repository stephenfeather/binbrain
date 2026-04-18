-- Migration: 2026-04-17_add_photo_suggestion_outcomes.sql
-- Phase 2 of the Data Capture Foundation (see
-- thoughts/shared/data_capture_synthesis_2026-04-17.md). Closes the
-- highest-ROI gap surfaced by BOTH independent data-capture surveys:
-- the rejection/hard-negative training signal.
--
-- Today /photos/{id}/confirm persists only the items the user accepted
-- (via photo_group_items -> bin_items -> items). Suggestions the user
-- saw-and-rejected, saw-and-ignored, or edited-to-a-different-label
-- evaporate. This table captures the full presented-list per photo with
-- the user's decision for each. iOS will POST the batch to a new
-- /photos/{id}/outcomes endpoint after a successful /confirm
-- (fire-and-forget), so /confirm is not modified.
--
-- Schema choices:
--   * bbox stored as double precision[] (4-element) rather than four
--     scalar columns to match the embedding-friendly shape called out in
--     the gaps report and avoid column proliferation.
--   * shown_at is client-supplied (the moment iOS presented the
--     suggestion); decided_at defaults to server now() so the server is
--     authoritative for the persisted-at timestamp.
--   * decision is CHECK-constrained to the four legal values. edited_to_label
--     is non-null in application logic when decision = 'edited' (validated
--     at the API layer so the DB CHECK stays simple).
--   * prompt_version is nullable so legacy or non-prompt-tagged clients
--     can still post outcomes.
--
-- Idempotent: IF NOT EXISTS on table + indexes. Safe to re-apply.

BEGIN;

CREATE TABLE IF NOT EXISTS photo_suggestion_outcomes (
    id               bigserial PRIMARY KEY,
    photo_id         bigint NOT NULL REFERENCES photos(photo_id) ON DELETE CASCADE,
    vision_model     text   NOT NULL,
    prompt_version   text,
    label            text   NOT NULL,
    category         text,
    confidence       double precision,
    bbox             double precision[],
    shown_at         timestamptz NOT NULL,
    decision         text   NOT NULL
                     CHECK (decision IN ('accepted', 'rejected', 'edited', 'ignored')),
    edited_to_label  text,
    decided_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS photo_suggestion_outcomes_photo_idx
    ON photo_suggestion_outcomes (photo_id);

CREATE INDEX IF NOT EXISTS photo_suggestion_outcomes_decision_idx
    ON photo_suggestion_outcomes (decision);

-- Composite index supports the scoped DELETE in
-- replace_photo_suggestion_outcomes, which filters by
-- (photo_id, vision_model) on every idempotent replay.
CREATE INDEX IF NOT EXISTS photo_suggestion_outcomes_photo_model_idx
    ON photo_suggestion_outcomes (photo_id, vision_model);

COMMIT;
