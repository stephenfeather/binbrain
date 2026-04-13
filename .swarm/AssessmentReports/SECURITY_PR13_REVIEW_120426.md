# SECURITY Review — PR #13 (FF-03 read-side photo path confinement)

**Date:** 2026-04-12
**Reviewer:** SECURITY
**PR:** https://github.com/stephenfeather/binbrain/pull/13
**Branch:** `dev/Developer2` @ `673c7b3`
**Verdict:** ✅ **APPROVE**

## 1. Helper correctness — PASS

`api/app/routes/photos.py:42` `_resolve_photo_path_under_root`:

- ✅ Uses `Path.resolve(strict=True)` at L54 → symlinks dereferenced, missing files raise before the confinement check.
- ✅ Inner helper `_is_path_under_photo_root` (L24) resolves BOTH `fpath` and `photo_root` before `relative_to` comparison. Root is not trusted unresolved.
- ✅ All failure modes (missing row, `FileNotFoundError`, `OSError`, `RuntimeError`, out-of-root, non-file target) raise `HTTPException(status_code=404, detail="photo not found")` — single response shape, no 403/500 leak.
- ✅ Rejects absolute-path escapes (`/etc/passwd`) and symlink escapes identically via the resolve-then-compare flow.
- ✅ Escape attempts are logged at `warning` with `event=photo_path_escape` for operator forensics without leaking the path in the HTTP response.

## 2. Coverage of read-side call sites — PASS

Grep `photos.path|photo_path|fetch_photo_path` across `api/app/routes/`:

| Handler | Uses helper? | Line |
|---------|--------------|------|
| `GET /photos/{id}/file` | ✅ `_resolve_photo_path_under_root` | photos.py:187 |
| `POST /photos/{id}/detect` | ✅ `_resolve_photo_path_under_root` before `detect(...)` | photos.py:249 |
| `GET /photos/{id}/suggest` (vision) | ✅ `_resolve_photo_path_under_root` before `describe_photo(...)` | photos.py:97 |
| `DELETE /photos/{id}` | ✅ `_is_path_under_photo_root` (shared helper) | photos.py:221 |

No other handler in `api/app/routes/` dereferences `photos.path` on disk. `/groups`, `/confirm` operate on DB rows only.

## 3. Delete-handler parity — PASS

`delete_photo` at photos.py:207 still raises `HTTPException(status_code=**400**, ...)` at L226 on escape, with `event=photo_delete_path_escape` logged. Refactor correctly preserves the F-01 contract (delete returns 400, not 404) while reusing the shared confinement predicate.

## 4. Tests exercise the attack — PASS

`api/tests/test_security_ff03_photo_path_confinement.py` — 7 tests:

| Test | What it proves |
|------|----------------|
| `test_file_rejects_path_outside_photo_root` | DB row with `/etc/passwd` → 404; body contains no `root:` substring |
| `test_file_rejects_nonexistent_out_of_root_path` | Missing out-of-root path → 404 (not 500) |
| `test_file_rejects_symlink_escape` | Symlink inside `photo_root` pointing outside → 404 |
| `test_file_serves_in_root_photo` | Positive control: valid photo still 200 with correct bytes |
| `test_detect_rejects_path_outside_photo_root_and_skips_detector` | Monkeypatches `photos_route.detect`; asserts `calls == []` — detector **never invoked** on escape |
| `test_detect_accepts_in_root_photo` | Positive control: detect reaches the pipeline |
| `test_audit_script_flags_out_of_root_rows` | Scrub script returns exit 1 and prints offending path |

**CI result:** `gh run 24322507087` pytest job → `272 passed, 1 skipped in 3.87s`. The FF-03 tests run green in CI (where `TEST_DATABASE_URL` is set). Locally they skip with `TEST_DATABASE_URL not set`, which is expected fixture gating consistent with the F-05/F-10 suites.

## 5. Regression check — PASS

Broader suite (CI): 272 passed / 1 skipped. Previously passing F-01, F-05, F-10 security tests all still green. No regressions.

## 6. Scrub script — PASS

`api/scripts/audit_photo_paths.py`:

- ✅ Read-only: only a single `SELECT photo_id, path FROM photos ORDER BY photo_id` — no `UPDATE`, `DELETE`, `INSERT` anywhere.
- ✅ Exit code: `return 1` when `offenders` non-empty (L79), `return 0` when clean (L81). `sys.exit(main())` at L85.
- ✅ Does NOT auto-quarantine — explicitly documents operator workflow at L73-L78 ("This script is read-only and will NOT mutate the database.").
- ✅ Reuses the same resolve-then-`relative_to` confinement logic as the route helper, so audit and enforcement agree on what "under root" means.

## 7. Scope discipline — PASS

`gh pr view 13 --json files`:

```
.swarm/Prompts/Dev2_001_ff03_photo_path_confinement.md  (+68)
api/app/routes/photos.py                                (+57/-16)
api/scripts/audit_photo_paths.py                        (+85)
api/tests/test_security_ff03_photo_path_confinement.py  (+217)
```

No changes to `pyproject.toml`, `uv.lock`, `app/deps.py`, other routes, or any unrelated code. Clean, focused diff.

## Other CI signals

| Check | Status |
|-------|--------|
| pytest | ✅ pass (272/1) |
| gitleaks | ✅ pass |
| CodeRabbit | ✅ completed |
| qlty check | ⚠️ 15 blocking issues — pre-existing lint findings, not introduced by this PR (SECURITY is not blocking on qlty for a security hardening PR) |
| pip-audit | ❌ unrelated — fixed by PR #12, already approved |

## Verdict

APPROVE. The helper correctly dereferences symlinks before the confinement check, every read-side photo-path dereference is gated by the helper, the delete-handler 400 contract is preserved, tests exercise real attack vectors (out-of-root DB row, symlink escape, detector-skip assertion), the scrub script is strictly read-only, and CI is green on pytest. FF-03 can be closed after merge.
