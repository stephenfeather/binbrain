-- S-00-AUDIT: audit log for the runtime settings store.
--
-- Every change to the ``settings`` table (via ``set_<name>()`` helpers or
-- ``repository.set_setting(...)``) must also insert an ``app_settings_audit``
-- row in the same transaction. If the audit write fails, the setting
-- change is rolled back.
--
-- Schema:
--   id            — surrogate key (identity, monotonic per-insert)
--   setting_key   — the ``settings.key`` being changed
--   old_value     — nullable: NULL on first write (row absent pre-change)
--   new_value     — required, even for no-op "re-writes" (they still audit)
--   actor_ip      — client IP (``Request.client.host``), rejects malformed
--                   strings via Postgres ``inet`` typing
--   actor_key_id  — API-key identifier of the admin actor
--   changed_at    — audit timestamp
--
-- Indexed on (setting_key, changed_at DESC) for per-key history queries.
-- Deploy-step re-apply is a no-op via IF NOT EXISTS guards.

CREATE TABLE IF NOT EXISTS app_settings_audit (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    setting_key    text NOT NULL,
    old_value      text,
    new_value      text NOT NULL,
    actor_ip       inet NOT NULL,
    actor_key_id   text NOT NULL,
    changed_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_settings_audit_key_time
    ON app_settings_audit (setting_key, changed_at DESC);
