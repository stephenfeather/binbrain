# ApiDev2_S00 — app_settings_audit table + log_setting_change helper

**Plan:** `thoughts/shared/plans/2026-04-20-runtime-settings-store.md` (row S-00-AUDIT)
**Branch:** `feature/apidev2-s00-audit-log`
**Worktree:** `.worktrees/apidev2-s00-audit-log/`
**Base:** `main @ 19d7ebe`

## Problem

Stephen's updated contract for the runtime settings store requires every change to be auditable: who (API key id), from where (IP), when (timestamp), and the old/new values. This PR ships the infrastructure. Every subsequent setter consumes it. Gates every other S-NN / RL-NN.

## Deliverables

1. **Migration** `migrations/2026-04-20b_app_settings_audit.sql`:
   - `CREATE TABLE IF NOT EXISTS app_settings_audit (...)` — see dispatch for columns.
   - `CREATE INDEX IF NOT EXISTS idx_app_settings_audit_key_time ON app_settings_audit (setting_key, changed_at DESC);`
   - `IF NOT EXISTS` guards so deploy-step re-apply is a no-op.

2. **Helper** `repository.log_setting_change(db, key, old_value, new_value, actor_ip, actor_key_id) -> None`:
   - Takes a `Session` (same transaction as the `set_setting` write).
   - Validates inputs: non-empty `key` / `new_value` / `actor_key_id`; parse `actor_ip` as `ipaddress.ip_address(...)` to reject malformed strings.
   - Issues INSERT; does NOT commit. Caller owns the transaction.
   - Rolls back implicitly when the caller's transaction rolls back (transactional safety).

3. **Template doc update** in `api/app/deps.py`:
   - Docstring (or inline comment) on `get_yolo_world_conf` / `set_yolo_world_conf` block showing the new canonical signature `set_yolo_world_conf(value: float, *, actor_ip: str, actor_key_id: str) -> None` and that it must call `log_setting_change()` in-transaction.
   - **Do NOT actually rewrite** `set_yolo_world_conf` or the other 3 existing setters. Instead add `# TODO(audit): conform to new setter signature + log_setting_change` comments on each of:
     - `set_active_vision_model`
     - `set_max_image_px`
     - `set_detection_model` / `set_detection_model_id`
     - `set_yolo_world_conf`

4. **No new admin routes.** No consumer call-site changes. Infrastructure only.

5. **Tests** `api/tests/test_settings_audit.py`:
   - Happy path: insert via helper, row observable with correct columns.
   - `old_value=None` case → NULL in column.
   - Transaction rollback: after `log_setting_change()`, caller rolls back → no row present.
   - Index exists (query `pg_indexes` for `idx_app_settings_audit_key_time`).
   - Input validation: empty `key`, empty `new_value`, empty `actor_key_id`, malformed `actor_ip` all raise `ValueError`.

6. **README**: add "Settings audit log" subsection — audit table name, what's logged, how to query history for a key.

## Scope guards

- No existing setter behavior changes.
- No admin endpoints in this PR.
- No prod DB writes. Migration validated against the local test/dev DB only.

## PR

- Title: `feat(settings): add app_settings_audit table + log_setting_change helper (S-00-AUDIT)`
- Body: problem, table schema, helper signature, transactional-safety note, test coverage, migration note.
- Ping team-lead via SendMessage with PR URL. Do NOT merge.
