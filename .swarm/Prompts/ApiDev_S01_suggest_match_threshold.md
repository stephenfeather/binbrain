# ApiDev_v2 — S-01: Promote SUGGEST_MATCH_THRESHOLD to runtime store

Plan: `thoughts/shared/plans/2026-04-20-runtime-settings-store.md`
Prereq: S-00-AUDIT (merged 4a326d8) — `app_settings_audit` table and
`repository.log_setting_change(...)` helper already available.

## Scope

- One constant only: `SUGGEST_MATCH_THRESHOLD` (default `0.85`, range `[0.0, 1.0]`).
- Key in `settings`/`app_settings_audit`: `suggest_match_threshold`.
- Replace the per-request `os.environ.get(...)` read in `photos.py:250`.
- Delete the dead module-level `_SUGGEST_MATCH_THRESHOLD` at `photos.py:36`.
- Remove `SUGGEST_MATCH_THRESHOLD` from `docker-compose.yml` api env.

## New accessor (per S-00-AUDIT contract)

```python
def get_suggest_match_threshold() -> float: ...

def set_suggest_match_threshold(
    value: float, *, actor_ip: str, actor_key_id: str
) -> None:
    # 1. Validate type + range ([0.0, 1.0], NaN reject, bool reject).
    # 2. Open session; read old value via repository.get_setting().
    # 3. repository.set_setting(db, "suggest_match_threshold", str(value))
    # 4. repository.log_setting_change(db, key=..., old_value=..., new_value=...,
    #        actor_ip=..., actor_key_id=...)
    # 5. db.commit()  (atomic — audit failure rolls back value write too)
    # 6. Update module-level cache.
```

Fallback chain: DB row → env `SUGGEST_MATCH_THRESHOLD` → hardcoded `0.85`.

## Admin endpoint

- `GET  /settings/suggest-match-threshold` — returns current value, no auth
  beyond the API-key middleware (matches other getters in `admin.py`).
- `POST /settings/suggest-match-threshold` — admin-only (`require_admin`).
  Body: `{"value": <float>}`. Passes
  `actor_ip=request.client.host` and `actor_key_id=str(request.state.api_key_id)`
  into the setter.

## Tests (`api/tests/test_settings_suggest_match_threshold.py`)

- Getter: DB present → DB value; DB absent + env → env; both absent → `0.85`.
- Setter: writes DB + updates cache; out-of-range (`-0.1`, `1.1`, `NaN`,
  `"abc"`, `True`) → `ValueError`; audit row created; audit txn rollback
  (simulate post-audit failure → no persisted rows).
- Admin endpoint: 403 for user-role key; admin round-trip (POST → GET);
  integration — POST new value, verify `get_suggest_match_threshold()`
  reflects it.

## Out of scope

- Other knobs, unrelated cleanups (`TODO(audit)` on the four existing
  setters is A-1..A-3 territory — leave them).
- Prod DB writes.
- CLI tool (CLI-01).
