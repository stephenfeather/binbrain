# Dev1 Task 002 — Security Remediation Phase 3 (Medium + Low)

**Assigned by:** ARCHITECT
**Source report:** `.swarm/AssessmentReports/SECURITY_ASSESSMENT_120426.md`
**Prior work:** PR #1 (squash-merged as `d8cfbf0`) closed F-01..F-06. This task covers the remaining findings.
**Working branch:** `fix/security-phase-3` (cut from current `main`)
**PR target:** `main`

## Scope

Remediate the three Medium and two Low findings from the 2026-04-12 security audit:

- F-07 (Medium) — raw exception text reflected to clients
- F-08 (Medium) — no rate limiting on expensive endpoints
- F-09 (Medium) — default compose binds API + Postgres to all interfaces
- F-10 (Low) — responses disclose internal filesystem paths
- F-11 (Low) — `device_metadata` accepted as unbounded arbitrary JSON

Out of scope:
- Fresh audit work. If you spot new issues, file them as `ARCHITECT REQUEST:` rather than fixing in-place.
- CI workflow creation. Separate follow-up task.
- `fastembed` upgrade (to unblock residual Pillow CVE). Separate follow-up task.

Line numbers below are from the audit report and may have shifted slightly after Phase 1+2 merged. Re-grep before editing.

## Findings to Fix

### F-07 (Medium) — Raw exception text reflected to clients
- **Files:** `api/app/main.py` (global handlers at lines ~169, 202, 219 post-merge), `api/app/routes/admin.py`, `api/app/routes/items.py`, `api/app/routes/bins.py`, `api/app/routes/photos.py`.
- **Required:**
  1. Define a small public error vocabulary (e.g., `"upstream_unavailable"`, `"model_failure"`, `"embedding_failure"`, `"internal_error"`). Map internal exceptions to these at the handler boundary.
  2. Replace handler responses that echo `str(exc)` or raw third-party error strings with stable generic messages. Preserve the original traceback/exception in structured server logs only.
  3. Normalize `HTTPException(detail=...)` sites that currently concatenate upstream failure strings (Ollama, embedder, UPC lookup) — keep status codes, scrub the `detail`.
  4. Keep 4xx validation detail that reflects the client's own input (e.g., "bin_id contains invalid characters") — those are safe. Only scrub server-side and third-party internals.
- **Tests (RED first):** invoke endpoints with forced upstream failure (monkeypatched Ollama/embedder to raise) and assert response body does not contain the exception class name, traceback fragments, module paths, or upstream error strings. Server logs should still contain the full exception.

### F-08 (Medium) — Rate limiting on expensive endpoints
- **Files:** `api/app/main.py` (middleware registration), new `api/app/middleware.py` additions or new `api/app/services/rate_limit.py`, `api/app/routes/photos.py`, `api/app/routes/admin.py`, `api/app/routes/upc.py`.
- **Required:**
  1. Add per-API-key rate limiting. In-process token bucket or sliding window is acceptable for v1 (document that a multi-worker deployment will need Redis-backed replacement; flag as follow-up).
  2. Tiered budgets (env-driven defaults, document in `api/app/config.py`):
     - Global default: 120 req/min per key.
     - Vision/detection (`/photos/{id}/suggest`, `/photos/{id}/detect`): 10 req/min per key.
     - Model warmup (`/models/select`, any `/admin/*` model-affecting route): 3 req/min per key.
     - UPC outbound lookups (`/upc/{upc}`): 30 req/min per key.
  3. Exceeded limits return `429 Too Many Requests` with `Retry-After` header. Do not leak internal state.
  4. Admin-role keys should still be limited but with a higher multiplier (e.g., 4x) — configurable.
- **Tests:** monkeypatch the clock; hammer a limited endpoint past its budget and assert 429 + `Retry-After`; confirm the global budget applies to un-annotated endpoints; confirm admin multiplier works.

### F-09 (Medium) — Compose binds API + Postgres to all interfaces
- **Files:** `docker-compose.yml`, `docker-compose.dev.yml`, `README.md`, possibly a new `docker-compose.prod.yml.example`.
- **Required:**
  1. In `docker-compose.yml` (the production-default file), change port publishing so Postgres is **not** published on the host by default. The API should bind to `127.0.0.1` unless an env var explicitly opts into wider exposure.
     - Example: `ports: ["127.0.0.1:${API_PORT:-8000}:8000"]` for api; remove `ports:` from postgres.
  2. Preserve developer ergonomics in `docker-compose.dev.yml` — dev overlay can re-publish Postgres on `127.0.0.1:${DB_PORT:-5434}:5432` for local tooling. Do not bind `0.0.0.0` in any committed compose file.
  3. Update README: split "local dev" from "production-ish" startup, make clear the default compose is for local/single-host use and that production requires a reverse proxy.
  4. Add a `docker-compose.prod.yml.example` (not a working default) showing the reverse-proxy + TLS pattern as guidance. Don't ship a working prod compose in this PR — just the documentation skeleton.
- **Tests:** this one is primarily config; add a tiny smoke check (pytest or shell) that parses `docker-compose.yml` and asserts no port mapping matches `0.0.0.0` or lacks an interface prefix. Run existing test suite to catch any unrelated regression.

### F-10 (Low) — Filesystem path disclosure
- **Files:** `api/app/routes/bins.py` (`/ingest`, `/bins/{bin_id}/add`, `/bins/{bin_id}`), `api/app/db/repository.py` (`SELECT photo_id, path, device_metadata ...`), and any response models under `api/app/schemas/`.
- **Required:**
  1. Replace `path` in response bodies with an opaque `photo_id` reference plus a URL pointing at a (to-be-added or existing) photo-retrieval route. If no retrieval route exists yet, return just `photo_id` and flag a follow-up `ARCHITECT REQUEST:` for the download route — do **not** invent one in this PR without confirmation.
  2. Stop serializing raw `device_metadata` blobs on list endpoints. Either omit entirely or return a whitelisted subset (see F-11).
  3. Repository queries that previously returned `path` for API consumption should still be callable internally (detection/suggest paths need it) but must not leak into response models.
- **Tests:** `GET /bins/{bin_id}` response JSON must not contain absolute paths, `/app`, `/photos`, or keys named `path`. Existing internal uses of path remain unchanged.

### F-11 (Low) — `device_metadata` schema + size bounds
- **Files:** `api/app/routes/bins.py` (`/ingest` form field parsing), `api/app/db/repository.py` (insert path), new `api/app/schemas/device_metadata.py` (or equivalent).
- **Required:**
  1. Define a Pydantic model with an explicit allowlist of fields. Reasonable starter set: `make: str`, `model: str`, `os: str`, `os_version: str`, `app_version: str`, `captured_at: datetime | None`, plus a bounded `extras: dict[str, str | int | float | bool] | None` with at most N entries (default 8). Strings capped at 128 chars.
  2. Reject the entire request with 400 if the serialized JSON exceeds a cap (default 4 KiB) or fails schema validation. Generic error message — do not echo the raw payload.
  3. Drop or hash any field obviously an identifier (e.g., if a field named `device_id`, `imei`, `mac`, `serial` is present in `extras`, hash with SHA-256 before persistence). Document the hashing.
  4. Store the validated-and-normalized object, not the raw JSON.
- **Tests:** oversized payload → 400; unknown top-level key → 400; `extras` with 20 keys → 400; valid payload round-trips; `device_id` in extras is stored hashed, not raw.

## Model-Compatibility Guardrails

Same pins as Phase 1+2 remain load-bearing — do **not** upgrade:
- `torch==2.6.0+cpu`, `torchvision==0.21.0+cpu`, `ultralytics==8.4.0`
- `fastembed==0.3.6` (and by extension, `Pillow<11`)

If any Phase 3 change appears to require a version bump of the above, stop and send `ARCHITECT REQUEST:` — do not force the upgrade.

## Success Criteria

- Five findings (F-07..F-11) closed with failing-first tests that pass after the fix.
- `pip-audit` output unchanged (no new CVEs introduced, residual fastembed/Pillow documented as-is).
- Full test suite passes with the test command from the prior handoff:
  `TEST_DATABASE_URL=postgresql+psycopg://binbrain:claude_dev@localhost:5434/binbrain uv run --project .worktrees/Developer1/api pytest .worktrees/Developer1/api/tests -q`
- No behavior changes to F-01..F-06 fixes (regression check).
- PR description includes:
  - Per-finding risk note
  - Rate-limit defaults table
  - Compose binding change summary
  - Note that a Redis-backed rate limiter + prod compose file are deliberately deferred

## Workflow Reminders

- TDD red-green-refactor with one commit per finding (5 commits total, plus any tests-only or docs-only commits).
- Commit style: conventional commits (`fix(security): ...`).
- Send `ARCHITECT REQUEST:` for any scope ambiguity — do not guess.
- If a test becomes flaky under rate limiting, fix the test (inject a clock), not the rate limiter.
- On completion: push branch, open PR, post `ARCHITECT TASK COMPLETED: Pull Request #<N>`.
