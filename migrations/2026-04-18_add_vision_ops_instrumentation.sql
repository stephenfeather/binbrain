-- Migration: 2026-04-18_add_vision_ops_instrumentation.sql
-- Phase 3 of the Data Capture Foundation (see
-- thoughts/shared/data_capture_synthesis_2026-04-17.md and Dev2_018 prompt).
--
-- Adds ops-instrumentation rows for /suggest invocations (`vision_calls`)
-- and per-match threshold capture for embedding matches
-- (`photo_suggestion_matches`). Both are append-only — unlike the Dev2_017
-- outcomes table there is no DELETE-then-INSERT replace pattern; history
-- matters here for latency/error-rate/threshold-tuning queries.
--
-- `vision_calls` subsumes the Dev2_015 iter-2 bbox-anomaly log lines (gap #5)
-- and the SuggestTracker state transitions (gap #8) via its `flags` jsonb
-- bag. No separate anomaly or tracker-history tables are introduced.
-- `flags` is ADD-ONLY — known keys are never renamed or removed. Today's
-- keys: `whole_image_bbox_warn` (bool), `bbox_normalized` (bool),
-- `stages` (text[] of completed pipeline stages).
--
-- `vision_calls.photo_id` uses ON DELETE SET NULL so telemetry survives the
-- deletion of the photo it describes (rate-of-deletion is itself a metric
-- we want to retain on the row).
--
-- Pre-vision 404 terminal states (photo missing, path escape) are NOT
-- recorded here because the vision_model is not yet resolved and
-- `vision_calls.model` is NOT NULL. Those failures are already logged
-- separately (`event=photo_path_escape`). If a future task needs
-- pre-resolve telemetry it is a separate access-log concern.
--
-- Idempotent: IF NOT EXISTS guards on every DDL statement. Safe to re-apply.

BEGIN;

CREATE TABLE IF NOT EXISTS vision_calls (
    id             bigserial PRIMARY KEY,
    photo_id       bigint REFERENCES photos(photo_id) ON DELETE SET NULL,
    model          text NOT NULL,
    prompt_version text,
    base_url       text,
    started_at     timestamptz NOT NULL,
    elapsed_ms     integer,
    hits_count     integer,
    cached         boolean NOT NULL,
    outcome        text NOT NULL CHECK (outcome IN ('ok','error')),
    error_code     text,
    flags          jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS vision_calls_started_at_idx ON vision_calls (started_at);
CREATE INDEX IF NOT EXISTS vision_calls_model_idx      ON vision_calls (model);
CREATE INDEX IF NOT EXISTS vision_calls_outcome_idx    ON vision_calls (outcome);
CREATE INDEX IF NOT EXISTS vision_calls_photo_idx      ON vision_calls (photo_id);

CREATE TABLE IF NOT EXISTS photo_suggestion_matches (
    id                    bigserial PRIMARY KEY,
    photo_detection_id    bigint NOT NULL
                          REFERENCES photo_detections(id) ON DELETE CASCADE,
    matched_item_id       bigint REFERENCES items(item_id) ON DELETE SET NULL,
    score                 double precision NOT NULL,
    threshold_at_compute  double precision NOT NULL,
    computed_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS photo_suggestion_matches_detection_idx
    ON photo_suggestion_matches (photo_detection_id);
CREATE INDEX IF NOT EXISTS photo_suggestion_matches_item_idx
    ON photo_suggestion_matches (matched_item_id);

COMMIT;
