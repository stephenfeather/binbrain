# Dev2_001 — FF-03: Read-side photo path confinement

From `.swarm/AssessmentReports/SECURITY_FOLLOWUP_AUDIT_120426.md`.

## Background

The path-traversal remediation closed the write side (new uploads land under `PHOTO_DIR` and are validated). Read-side handlers still trust whatever is stored in `photos.path`, so a legacy row written before the fix, or a row from DB tampering, becomes an arbitrary local file-read primitive via `/photos/{id}/file` and an arbitrary local path feed into detection via `/photos/{id}/detect`.

Severity: Medium. Probability: Medium.

## Evidence (from the audit)

- `api/app/routes/photos.py:138-161` — `photo_path` read from DB and opened directly (no `photo_root` check).
- `api/app/routes/photos.py:205-210` — stored `photo_path` passed into detection unchecked.
- `api/app/db/repository.py:246-250` — raw path returned from `photos.path`.
- `api/app/routes/photos.py:176-185` — confinement check runs only on delete.

## Scope

### 1. Enforce `photo_root` confinement on every read-side path use

Factor the existing delete-side confinement check (`routes/photos.py:176-185`) into a small helper (e.g. `_resolve_photo_path_under_root(photo_path, photo_root) -> Path` that raises `HTTPException(404)` on mismatch — a 404, not a 403, to avoid leaking existence). Call it from:

- `GET /photos/{photo_id}/file` (around `photos.py:138-161`)
- `POST /photos/{photo_id}/detect` (around `photos.py:198-228`, before the detection call at 205-210)
- Any other handler that dereferences `photos.path` on disk (audit the file).

Use `Path.resolve(strict=True)` + `is_relative_to(photo_root.resolve())`. Reject symlinks that escape. Reuse whatever the delete handler already does — don't reinvent.

### 2. Stop returning raw paths from the repository

`repository.py:246-250` returns the raw `photos.path` string. Route handlers need the on-disk path to serve files, but responses should not include it. Double-check `fetch_bin_photos` (`repository.py:211-226`) and callers in `routes/bins.py:348-359` — paths should already be stripped there per F-10; if not, strip them.

### 3. Migration / scrub

Add a small operational script (or a one-shot migration) that finds rows where `photos.path` is outside `PHOTO_DIR` and quarantines them (e.g. sets a `quarantined_at` column, or moves them to a separate table — coordinate with Architect if schema change is non-trivial; a simple "log and skip" query may be acceptable if no legacy rows currently exist).

At minimum: a script under `api/scripts/` or `api/app/scripts/` that the operator can run, documenting the count of offending rows. If DB currently has zero such rows, the script is still useful as a repeatable audit.

### 4. Tests

Add tests under `api/tests/` (e.g. `test_security_ff03_photo_path_confinement.py`):

- Seed a photos row whose `path` is outside `PHOTO_DIR` (e.g. `/etc/passwd`).
- Assert `GET /photos/{id}/file` returns 404 (not 200, not 500).
- Assert `POST /photos/{id}/detect` returns 404 and does NOT invoke the detector.
- Positive control: a valid in-root photo still serves normally.
- Symlink escape case: a `photos.path` that resolves outside `PHOTO_DIR` via symlink is rejected.

Follow the existing pattern in `test_security_f05_file_validation.py` and `test_security_f10_path_disclosure.py` for fixtures / client setup.

## Constraints

- Don't touch `pyproject.toml` or `uv.lock`.
- Don't modify the delete-side handler's behavior — just extract its check into a shared helper.
- Return 404 (not 403/500) for out-of-root paths to avoid leaking row existence.
- Keep the migration/scrub conservative — don't auto-delete rows; quarantine or log only.

## Deliverable

- Branch: `dev/Developer2` (your existing worktree).
- Commit the handler/helper changes, the scrub script, and the tests.
- Open PR targeting `main` referencing FF-03 in `.swarm/AssessmentReports/SECURITY_FOLLOWUP_AUDIT_120426.md`.
- SECURITY will scan the PR before Architect merges.

## Coordination note

Developer1 is working on Dev1_007 (CI pip-audit fix, `.github/workflows/ci.yml` only). No file overlap expected — but if you touch `api/app/main.py` for any reason, coordinate via the Architect pane.
