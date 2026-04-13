# SEC_004 — Review PR #13 (FF-03 read-side photo path confinement)

PR: https://github.com/stephenfeather/binbrain/pull/13
Branch: `dev/Developer2`
Addresses: FF-03 from `.swarm/AssessmentReports/SECURITY_FOLLOWUP_AUDIT_120426.md`

## What changed

- `api/app/routes/photos.py`: new `_is_path_under_photo_root` and `_resolve_photo_path_under_root` helpers; applied to `/photos/{id}/file`, `/photos/{id}/detect`, and the vision suggest flow; delete handler refactored to reuse the shared check (400 preserved there).
- `api/scripts/audit_photo_paths.py`: read-only operator audit script; exit 1 with offender list.
- `api/tests/test_security_ff03_photo_path_confinement.py`: 7 tests.

## What to verify

### 1. Helper correctness
- Read `_resolve_photo_path_under_root` carefully. It must:
  - Use `Path.resolve(strict=True)` (or equivalent) so symlinks are dereferenced before the confinement check.
  - Compare against a resolved `photo_root` (not the unresolved config value).
  - Return 404 (not 403 or 500) for escapes, missing files, and non-file targets.
  - Reject both absolute-path escapes (`/etc/passwd`) and relative/symlink escapes.

### 2. Coverage of read-side call sites
- Confirm the helper is called before ANY on-disk dereference of `photos.path` in:
  - `GET /photos/{id}/file`
  - `POST /photos/{id}/detect` (before detector invocation — detector must not see an escape path)
  - The vision suggest flow
  - Any other reader you find via `grep "photos.path\|photo_path" api/app/routes/`. Flag any handler that still opens a stored path without the helper.

### 3. Delete-handler parity
- `DELETE /photos/{id}` previously returned 400 for escapes. Confirm the refactor preserves 400 there (the report says it does — verify).

### 4. Tests actually exercise the attack
- In `test_security_ff03_photo_path_confinement.py`, confirm:
  - Out-of-root test seeds a `photos.path` pointing outside `PHOTO_DIR` (e.g. `/etc/passwd`) and asserts 404.
  - Symlink-escape test creates a symlink whose target is outside `PHOTO_DIR` and asserts 404.
  - Detector-skip test asserts the detector is NOT called when path escapes.
  - Positive control still serves a valid in-root photo.
- Run: `cd api && uv run pytest tests/test_security_ff03_photo_path_confinement.py -v`.

### 5. Regression check
- Run the broader security suite: `uv run pytest tests/ -k "security" -q`. All previously passing F-01/F-05/F-10 tests must still pass.
- Run full API suite: `uv run pytest tests -q`. Note pass/skip counts.

### 6. Scrub script
- Read `api/scripts/audit_photo_paths.py`. Confirm:
  - Read-only (no UPDATE/DELETE against the DB).
  - Exit code 1 when offenders found, 0 when clean.
  - Does NOT auto-quarantine or mutate rows.

### 7. Scope discipline
- Confirm diff only touches `api/app/routes/photos.py`, `api/scripts/audit_photo_paths.py`, the new test file, and any small helper files. No changes to `pyproject.toml`, `uv.lock`, or unrelated routes.

## Out of scope

- FF-02 (Pillow pin), FF-04 (device_metadata disclosure) — separate items.

## Deliverable

- Post verdict as PR comment on #13: APPROVE / REQUEST CHANGES with evidence.
- Write `.swarm/AssessmentReports/SECURITY_PR13_REVIEW_120426.md` summarizing checks 1-7 and result.
- If APPROVED, write Architect handoff at `.swarm/Prompts/ARCH_SEC_004_pr13_approved.md` so merge can proceed.
