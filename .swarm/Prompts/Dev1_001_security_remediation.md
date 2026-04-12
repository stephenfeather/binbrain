# Dev1 Task 001 — Security Remediation Phase 1+2

**Assigned by:** ARCHITECT
**Source report:** `.swarm/AssessmentReports/SECURITY_ASSESSMENT_120426.md` (on branch `dev/SECURITY`; rebase or cherry-pick to pick it up)
**Working branch:** `fix/security-phase-1-2` (cut from `main`)
**PR target:** `main`

## Scope

Remediate the Critical and High findings from the 2026-04-12 security audit. Medium/Low findings (F-07..F-11) are **out of scope** for this task and will be assigned separately — flag with TODO comments if you touch adjacent code, do not fix in-place.

## Findings to Fix

### F-01 (Critical) — Path traversal via `bin_id`
- **Files:** `api/app/routes/bins.py:31-73`, `api/app/routes/bins.py:105-161`, `api/app/deps.py:22-26`, `api/app/routes/photos.py:167-176`
- **Required:**
  1. Add a `validate_bin_id()` helper that rejects any value containing path separators (`/`, `\`), `..`, null/control bytes, or leading `.`; enforce allowlist regex (e.g., `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`).
  2. Apply at every entry point that uses `bin_id` in a filesystem path.
  3. After constructing candidate paths, resolve them and assert they remain under `photo_root.resolve()`; raise `HTTPException(400)` on mismatch.
  4. In `delete_photo()`, re-validate the stored path stays within `photo_root` before `unlink()`.
- **Tests (RED first):** attempts with `../`, absolute paths, `%2e%2e`, null bytes, and backslash must all return 400 and not create any files outside `PHOTO_DIR`.

### F-02 (Critical) — Arbitrary model-path loading (RCE chain)
- **Files:** `api/app/routes/admin.py:184-212`, `api/app/services/detection.py:31-47`
- **Required:**
  1. Replace free-form model-path input with an allowlist identifier (enum or constant dict mapping logical names → server-controlled paths under a fixed models directory).
  2. `/admin/detection/model` (or equivalent) must reject anything not in the allowlist with 400.
  3. `detection.py` must resolve the logical id to the server-controlled path; never load arbitrary user-supplied paths.
  4. Add a startup check asserting the configured models directory is outside `PHOTO_DIR`.
- **Tests:** posting a path outside the allowlist returns 400; posting a valid id loads the mapped model; posting an uploaded photo path returns 400.

### F-03 (Critical) — No admin authorization boundary
- **Files:** `api/app/main.py:73-128`, `api/app/routes/admin.py:76-266`, `api/app/routes/locations.py:14-62`, `api/app/routes/classes.py:17-75`, `api/app/db/repository.py` (api_keys table/ops), `db-init/01-schema.sql` or new migration.
- **Required:**
  1. Add a `role` column (default `'user'`, values `'user'`|`'admin'`) to the `api_keys` table. Create a forward migration under `migrations/`.
  2. Update `scripts/create_api_key.py` to accept `--role` (default `user`).
  3. Extend authentication middleware to attach `request.state.api_key_role`.
  4. Add a `require_admin` FastAPI dependency and apply it to **every** `/admin/*` route, plus mutation routes on `/locations` and `/classes` where current behavior implies admin intent (confirm scope with ARCHITECT if unsure — send `ARCHITECT REQUEST:` if needed).
  5. Data-migration step: the first key ever created becomes admin; additional existing keys remain `user` unless manually upgraded. Document how to upgrade via SQL.
- **Tests:** a `user` key against `/admin/*` returns 403; an `admin` key succeeds; existing user-plane routes still work with either role.

### F-04 (High) — Unbounded / fully-buffered uploads
- **Files:** `api/app/routes/bins.py` (ingest + add), `api/app/main.py` (middleware)
- **Required:**
  1. Enforce per-request max body size (env-driven, default 50 MiB) via middleware that rejects with 413 before buffering.
  2. Enforce per-file size cap (default 15 MiB) and max file count per request (default 20) inside the handlers.
  3. Stream writes in chunks using `aiofiles` or `shutil.copyfileobj` on `up.file`; do not call `await up.read()` on the whole body.
- **Tests:** 60 MiB multipart request rejected with 413; 21-file request rejected with 400; happy-path 3×5 MiB ingest still works and writes stream-correctly.

### F-05 (High) — File-type validation by magic bytes
- **Files:** `api/app/routes/bins.py` upload handlers, possibly a new `api/app/services/image_validation.py`
- **Required:**
  1. After streaming to a temp path (NamedTemporaryFile under `PHOTO_DIR`), validate with `PIL.Image.open().verify()` plus a magic-byte check (e.g., `imghdr` or filetype lib).
  2. Allow only `jpeg`, `png`, `webp`, `heic` (if Pillow-HEIF present). Reject anything else with 415.
  3. Only on success `rename()` the temp file to the final stored path. On rejection, `unlink()` the temp.
- **Tests:** uploading a renamed `.pt` file or `.exe` with `.jpg` extension is rejected and leaves no residue in `PHOTO_DIR`.

### F-06 (High) — Dependency upgrades
- **Files:** `api/pyproject.toml`, `api/uv.lock`
- **Required:**
  1. Bump: `python-multipart >= 0.0.22`, `Pillow >= 12.1.1`, `starlette >= 0.49.1` (or the minimum FastAPI supports without conflict), `requests >= 2.33.0`, `onnx >= 1.21.0`.
  2. Run `uv lock --upgrade-package <name>` for each; commit updated `uv.lock`.
  3. Run the full test suite. If any upgrade causes breakage, investigate — do **not** pin back without `ARCHITECT REQUEST:` clarification.
  4. Re-run `pip-audit` and paste the clean output into the PR description.

**Model-compatibility guardrails (READ BEFORE UPGRADING):**

Several pins are load-bearing for the vision/embedding pipeline:
- `torch==2.6.0+cpu`, `torchvision==0.21.0+cpu`, `ultralytics==8.4.0` are pinned for model compatibility (commit 0f9557e). **Do NOT upgrade these.**
- `fastembed==0.3.6` transitively constrains `onnx` and `onnxruntime`. Bumping `onnx` directly may break fastembed embedding generation.
- `Pillow` is consumed by both `ultralytics` and request handlers. Ultralytics 8.4.0 was released against older Pillow; test detection end-to-end after any Pillow bump.

Required workflow for F-06:
1. After each bump, run the full test suite **including any integration/live tests** that exercise detection and embeddings.
2. If an upgrade causes model load failure, embedding mismatch, or test regression: stop, document the conflict, and output `ARCHITECT REQUEST: <which CVE cannot be resolved without breaking <pipeline>>`. Do not force the upgrade.
3. When a direct upgrade is blocked by a pinned transitive consumer, pick the highest patch version that satisfies both constraints and document residual risk in the PR.
4. Priority for trade-off cases: operational correctness > closing every CVE. Leave deferred CVEs captured as follow-up tasks in the PR body.

## Success Criteria

- All six findings closed with failing-first tests that pass after the fix.
- `pip-audit` reports zero High/Critical advisories on runtime deps.
- Existing test suite passes.
- No changes to Medium/Low findings (F-07..F-11) — those are deferred.
- PR description includes: before/after pip-audit output, migration steps for ops, and a short risk note per finding.

## Workflow Reminders

- TDD red-green-refactor with checkpoint commits per cycle.
- Commit style: conventional commits (`fix(security): ...`).
- Send `ARCHITECT REQUEST:` for any scope ambiguity — do not guess.
- On completion: push branch, open PR, post `ARCHITECT TASK COMPLETED: Pull Request #<N>`.
