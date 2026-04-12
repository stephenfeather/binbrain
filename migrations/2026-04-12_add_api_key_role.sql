-- Migration: 2026-04-12_add_api_key_role.sql
-- F-03: Introduce role-based authorization for API keys.
--
-- Adds a `role` column to api_keys with:
--   - default 'user' for all existing and new keys
--   - CHECK constraint enforcing only 'user' | 'admin'
--
-- Data migration: the first key ever created becomes admin;
-- all others remain 'user' unless manually upgraded.
--
-- To manually promote a key to admin:
--   UPDATE api_keys SET role = 'admin' WHERE id = <key_id>;
--
-- To list current keys and roles:
--   SELECT id, name, role, created_at FROM api_keys ORDER BY id;

BEGIN;

ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'user'
        CHECK (role IN ('user', 'admin'));

-- Promote the oldest key to admin (bootstrap policy).
-- If you prefer not to auto-promote, comment out the next UPDATE and run
-- the manual SQL above for whichever key(s) should be admin.
UPDATE api_keys
SET role = 'admin'
WHERE id = (SELECT MIN(id) FROM api_keys);

COMMIT;
