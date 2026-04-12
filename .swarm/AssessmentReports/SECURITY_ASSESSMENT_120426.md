# BinBrain Security Assessment

Assessment date: 2026-04-12

Scope reviewed: full repository at the worktree root, with targeted source review of `api/app/`, `api/Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`, `db-init/01-schema.sql`, `migrations/*.sql`, `scripts/create_api_key.py`, `install.sh`, `README.md`, `.gitignore`, `api/pyproject.toml`, and `api/uv.lock`.

Threat model:
- The primary exposed surface is a FastAPI service that accepts authenticated multipart uploads, serves stored photo content, performs outbound HTTP calls, and mutates global runtime settings.
- The service appears intended for trusted local or small-team deployment, but the default compose files publish both the API and PostgreSQL to the host.
- The system stores inventory data, image uploads, model settings, and API keys. It does not appear to handle regulated data, but the photo and inventory corpus should still be treated as sensitive operational data.

Method:
- Manual source review of all request boundaries, state-changing routes, database access, file handling, and Docker/runtime configuration.
- Targeted grep only to locate candidates before reading source.
- Dependency audit using `uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements.txt` and `uvx --python 3.12 --from pip-audit pip-audit -r <exported file> --no-deps --disable-pip`.
- Repository scan for hardcoded secrets and CI/CD workflow directories.

## Executive Summary

BinBrain has a compact codebase and a few solid baseline controls, but its current trust model is weak: any valid API key grants full control, uploads are insufficiently constrained, and a configurable detection-model path creates a credible code-execution chain when combined with arbitrary file upload. The highest-risk issues are:

1. `bin_id` is used directly in filesystem paths, enabling writes outside the intended photo root.
2. Any valid API key can change the detection model path, and the selected model is loaded through Ultralytics' `torch.load()` path.
3. All API keys are effectively admin keys; there are no scopes, roles, or route-level authorization checks.
4. Upload handling is unbounded and fully buffered into memory before being written to disk.

Positive controls are present:
- API keys are generated with high entropy and stored as SHA-256 hashes, not plaintext (`api/app/db/repository.py:550-559`).
- SQL access is parameterized throughout the repository layer; no user input is interpolated directly into SQL text beyond a fixed-column update helper (`api/app/db/repository.py:13-620`).
- `.env`, uploaded data, and downloaded model weights are gitignored (`.gitignore:1-15`).
- The installer generates a random PostgreSQL password rather than embedding a static secret (`install.sh:249-275`).
- The runtime container drops to a non-root `app` user (`api/Dockerfile:31-35`).

## Findings Summary

| ID | Title | Severity | Probability | Evidence |
| --- | --- | --- | --- | --- |
| F-01 | Path traversal in upload directory construction | Critical | High | `api/app/routes/bins.py:31-73`, `api/app/routes/bins.py:105-161`, `api/app/deps.py:22-26`, `api/app/routes/photos.py:167-176` |
| F-02 | Arbitrary model-path loading creates a plausible authenticated RCE chain | Critical | Medium | `api/app/routes/admin.py:184-212`, `api/app/services/detection.py:31-47`, `api/app/routes/bins.py:61-73`, `api/.venv/lib/python3.12/site-packages/ultralytics/nn/tasks.py:1395-1490` |
| F-03 | No authorization boundary between regular and admin operations | Critical | High | `api/app/main.py:73-128`, `api/app/routes/admin.py:76-266`, `api/app/routes/locations.py:14-62`, `api/app/routes/classes.py:17-75` |
| F-04 | Multipart uploads are unbounded and buffered into memory | High | High | `api/app/routes/bins.py:24-29`, `api/app/routes/bins.py:61-71`, `api/app/routes/bins.py:93-103`, `api/app/routes/bins.py:146-166`, `api/app/main.py:61-128` |
| F-05 | Upload validation allows arbitrary file types into the processing pipeline | High | High | `api/app/routes/bins.py:61-68`, `api/app/routes/bins.py:151-158`, `api/app/services/vision.py:25-31`, `api/app/routes/photos.py:144-159` |
| F-06 | Locked dependencies contain known vulnerabilities in active code paths | High | Medium | `api/pyproject.toml:9-19`, `api/uv.lock:1229-1245`, `api/uv.lock:1550-1555`, `api/uv.lock:1739-1750`, `api/uv.lock:1961-1969`, `api/uv.lock:1139-1158` |
| F-07 | Raw exception text is reflected to clients | Medium | High | `api/app/main.py:131-160`, `api/app/routes/admin.py:22-28`, `api/app/routes/admin.py:51-57`, `api/app/routes/admin.py:89-105`, `api/app/routes/items.py:79-81`, `api/app/routes/items.py:120-123`, `api/app/routes/bins.py:195-197` |
| F-08 | Expensive endpoints have no rate limiting or quotas | Medium | High | `api/app/main.py:61-128`, `api/app/routes/photos.py:32-124`, `api/app/routes/photos.py:187-217`, `api/app/routes/admin.py:76-123`, `api/app/routes/upc.py:39-72` |
| F-09 | Default compose configuration exposes API and PostgreSQL to the host | Medium | Medium | `docker-compose.yml:12-13`, `docker-compose.yml:45-46`, `docker-compose.dev.yml:1-10`, `README.md:41-50` |
| F-10 | API responses disclose internal filesystem paths and opaque device metadata | Low | High | `api/app/routes/bins.py:70-73`, `api/app/routes/bins.py:160-161`, `api/app/routes/bins.py:227-233`, `api/app/db/repository.py:211-226` |
| F-11 | `device_metadata` is accepted as arbitrary JSON with no schema or size bounds | Low | Medium | `api/app/routes/bins.py:28-41`, `api/app/db/repository.py:163-176` |

## Detailed Findings

### F-01: Path traversal in upload directory construction

Severity: Critical

Impact:
- An authenticated caller can cause files to be written outside `PHOTO_DIR`.
- This breaks the intended storage boundary and can place attacker-controlled files in other application-writable directories.
- The stored path is later trusted for delete operations, increasing the blast radius of the original write.

Probability: High

Evidence:
- `photo_root` is created directly from `PHOTO_DIR` with no later confinement checks (`api/app/deps.py:22-26`).
- `/ingest` strips whitespace from `bin_id` but otherwise accepts it as-is, then uses `photo_root / bin_id` and `mkdir(parents=True, exist_ok=True)` before writing bytes (`api/app/routes/bins.py:31-73`).
- `/bins/{bin_id}/add` repeats the same pattern for optional photo uploads (`api/app/routes/bins.py:105-161`).
- `delete_photo()` trusts the stored path and unlinks it without verifying it still resides under the photo root (`api/app/routes/photos.py:167-176`).

Recommended remediation:
- Reject `bin_id` values containing path separators, `..`, control characters, or absolute paths.
- Replace user-derived storage paths with a server-generated opaque directory or object key.
- Resolve candidate paths before use and enforce that they remain under `photo_root.resolve()`.
- Store relative paths or opaque file IDs in the database rather than absolute filesystem paths.

### F-02: Arbitrary model-path loading creates a plausible authenticated RCE chain

Severity: Critical

Impact:
- Any authenticated caller can upload arbitrary bytes, obtain the stored on-disk path, set that path as the active detection model, and trigger model loading.
- The installed Ultralytics code path loads model artifacts through `torch.load()` and will even attempt module installation on certain load failures.
- In practice this creates a strong authenticated code-execution chain if a crafted `.pt` payload is accepted as a model artifact.

Probability: Medium

Evidence:
- Upload endpoints preserve unknown file extensions instead of rejecting them, then return the stored path in the response (`api/app/routes/bins.py:61-73`, `api/app/routes/bins.py:151-161`).
- The detection-model settings endpoint accepts any non-empty string and persists it as the global model path (`api/app/routes/admin.py:184-212`).
- Detection loads the configured path with `YOLOE(desired_path)` whenever the path changes (`api/app/services/detection.py:31-47`).
- The installed Ultralytics package uses `torch_load(file, map_location="cpu")` in `torch_safe_load()` and `load_checkpoint()` for `.pt` artifacts (`api/.venv/lib/python3.12/site-packages/ultralytics/nn/tasks.py:1395-1490`).

Recommended remediation:
- Treat detection models as an allowlisted identifier set, not a free-form path.
- Remove filesystem path selection from the public API entirely.
- If dynamic model loading must remain, require a signed artifact pipeline and load only from a controlled internal directory.
- Prefer a safe format such as `safetensors`; do not use `torch.load()` on user-influenced files.
- Separate admin-only runtime configuration from general API-key access.

### F-03: No authorization boundary between regular and admin operations

Severity: Critical

Impact:
- Any leaked or low-trust API key becomes a full-control credential.
- A caller with any valid key can mint more keys, revoke keys, change global model settings, mutate data, and manage locations/classes.
- There is no mechanism to issue constrained client keys or isolate high-risk operations.

Probability: High

Evidence:
- The only authentication check is a global middleware that validates key existence and revocation status, then sets `request.state.api_key_id`; it does not attach or verify roles/scopes (`api/app/main.py:73-128`).
- Admin key management and runtime settings are available on the same trust plane as normal data routes (`api/app/routes/admin.py:76-266`).
- Other mutating routes such as `/locations` and `/classes` have no additional authorization dependency (`api/app/routes/locations.py:14-62`, `api/app/routes/classes.py:17-75`).

Recommended remediation:
- Introduce scoped or role-based API keys, with distinct admin-only credentials for `/admin/*` and other control-plane routes.
- Enforce authorization per router or per endpoint, not only in a single global authentication middleware.
- Record actor identity for all privileged mutations and retain an audit trail.
- Rotate all existing keys after introducing a privilege boundary.

### F-04: Multipart uploads are unbounded and buffered into memory

Severity: High

Impact:
- A single authenticated request can consume large amounts of memory and disk.
- Multiple concurrent requests can deny service even without sophisticated attack tooling.
- The current code path does not reject oversize bodies early and does not stream writes incrementally.

Probability: High

Evidence:
- `/ingest` accepts `photos: list[UploadFile] = File(...)` with no count or size controls (`api/app/routes/bins.py:24-29`).
- Each file is read fully via `await up.read()` before being written to disk (`api/app/routes/bins.py:61-71`).
- `/bins/{bin_id}/add` repeats the same fully buffered upload pattern (`api/app/routes/bins.py:93-103`, `api/app/routes/bins.py:146-166`).
- The application middleware stack contains only request IDs and API-key validation; no body-size enforcement or quotas are present (`api/app/main.py:61-128`).

Recommended remediation:
- Enforce request-size, file-count, and per-file limits at the reverse proxy and application layers.
- Stream uploaded content in chunks instead of calling `await up.read()`.
- Add per-key quotas and backpressure for write-heavy routes.
- Reject overlarge images based on decoded dimensions before downstream processing.

### F-05: Upload validation allows arbitrary file types into the processing pipeline

Severity: High

Impact:
- Attackers can store non-image content in the photo store.
- Downstream image-processing code then attempts to open attacker-controlled files with Pillow.
- This increases exposure to parser vulnerabilities and operational instability.

Probability: High

Evidence:
- Unknown extensions are preserved rather than rejected; the fallback behavior is effectively “accept and store” (`api/app/routes/bins.py:61-68`, `api/app/routes/bins.py:151-158`).
- `describe_photo()` later opens the stored path with Pillow (`api/app/services/vision.py:25-31`).
- `/photos/{photo_id}/file?w=...` also opens the stored file with Pillow for resizing (`api/app/routes/photos.py:144-159`).

Recommended remediation:
- Validate content type by magic bytes and successful image decode at ingest time, not by filename suffix.
- Reject unsupported formats instead of storing them.
- Normalize accepted uploads into a safe internal format before later processing.
- Consider virus/malware scanning if these uploads can originate from semi-trusted clients.

### F-06: Locked dependencies contain known vulnerabilities in active code paths

Severity: High

Impact:
- Vulnerable framework and parser/image-processing dependencies increase exposure in exactly the areas this application exercises: HTTP handling, multipart parsing, image parsing, outbound HTTP, and embedding/model support.
- Some findings are transitive rather than directly invoked by application code, but the overall dependency posture needs attention.

Probability: Medium

Evidence:
- Runtime dependency declarations include `python-multipart`, `fastembed`, `Pillow`, and `ultralytics` (`api/pyproject.toml:9-19`).
- The lockfile pins vulnerable versions including `pillow==10.4.0`, `python-multipart==0.0.12`, `requests==2.32.5`, `starlette==0.45.3`, and `onnx==1.20.1` for Python `<3.13` (`api/uv.lock:1229-1245`, `api/uv.lock:1550-1555`, `api/uv.lock:1739-1750`, `api/uv.lock:1961-1969`, `api/uv.lock:1139-1158`).
- `pip-audit` against the exported locked requirements under Python 3.12 reported:
  - `python-multipart 0.0.12`: `CVE-2024-53981`, `CVE-2026-24486`
  - `pillow 10.4.0`: `CVE-2026-25990`
  - `starlette 0.45.3`: `CVE-2025-54121`, `CVE-2025-62727`
  - `requests 2.32.5`: `CVE-2026-25645`
  - `onnx 1.20.1`: `CVE-2026-34447`, `CVE-2026-34446`, `GHSA-q56x-g2fj-4rj6`, `CVE-2026-28500`, `CVE-2026-27489`, `CVE-2026-34445`

Recommended remediation:
- Upgrade `python-multipart` to at least `0.0.22`.
- Upgrade `Pillow` to `12.1.1` or newer.
- Upgrade `Starlette`, `Requests`, and `ONNX` to versions that clear the reported advisories.
- Re-lock dependencies and re-run the audit after upgrades.
- Remove or isolate dependencies that are not required in production paths.

### F-07: Raw exception text is reflected to clients

Severity: Medium

Impact:
- Clients can learn operational details such as upstream connectivity failures, embedding failures, and internal exception strings.
- This helps reconnaissance and makes future attacks easier to tune.

Probability: High

Evidence:
- The global HTTP exception handler reflects string `detail` values back to clients (`api/app/main.py:131-160`).
- Multiple admin endpoints expose raw Ollama failure messages (`api/app/routes/admin.py:22-28`, `api/app/routes/admin.py:51-57`, `api/app/routes/admin.py:89-105`).
- Item creation and search embed failures are also reflected directly (`api/app/routes/items.py:79-81`, `api/app/routes/items.py:120-123`).
- `/bins/{bin_id}/add` returns raw exception text on unexpected failures (`api/app/routes/bins.py:195-197`).

Recommended remediation:
- Replace raw exception text with stable, generic client messages.
- Keep detailed exception strings only in structured server logs.
- Normalize third-party service failures to a small public error vocabulary.

### F-08: Expensive endpoints have no rate limiting or quotas

Severity: Medium

Impact:
- An authenticated caller can monopolize CPU, memory, GPU, or outbound HTTP capacity by repeatedly invoking model-heavy endpoints.
- The lack of quotas makes abuse and accidental overload difficult to distinguish.

Probability: High

Evidence:
- There is no rate-limiting middleware or quota enforcement in the main app (`api/app/main.py:61-128`).
- `/photos/{photo_id}/suggest` invokes the vision model and embedding search (`api/app/routes/photos.py:32-124`).
- `/photos/{photo_id}/detect` invokes object detection (`api/app/routes/photos.py:187-217`).
- `/models/select` forces model warm-up against the configured Ollama server (`api/app/routes/admin.py:76-123`).
- `/upc/{upc}` can trigger outbound HTTP lookups to a third-party service (`api/app/routes/upc.py:39-72`, `api/app/services/upc_lookup.py:51-101`).

Recommended remediation:
- Add per-key rate limits and concurrency caps.
- Use stricter budgets for model-warmup, detection, and vision inference routes.
- Queue expensive operations behind worker pools where practical.
- Emit per-key usage metrics and alerts.

### F-09: Default compose configuration exposes API and PostgreSQL to the host

Severity: Medium

Impact:
- If deployed as-is on a networked host, both the API and the database are reachable outside the container network.
- There is no TLS termination, reverse-proxy filtering, or host binding restriction in the compose defaults.

Probability: Medium

Evidence:
- PostgreSQL is published on the host with `"${DB_PORT:-5434}:5432"` (`docker-compose.yml:12-13`).
- The API is published on the host with `"${API_PORT:-8000}:8000"` (`docker-compose.yml:45-46`).
- The README directs operators to run `docker compose up -d` as the default setup path (`README.md:41-50`).
- Development mode overlays hot reload but does not harden exposure (`docker-compose.dev.yml:1-10`).

Recommended remediation:
- Bind services to `127.0.0.1` when local access is sufficient.
- Do not publish PostgreSQL in production.
- Place the API behind a reverse proxy enforcing TLS, request-size limits, and rate limits.
- Maintain clearly separated production and development deployment manifests.

### F-10: API responses disclose internal filesystem paths and opaque device metadata

Severity: Low

Impact:
- Clients learn the on-disk layout of uploaded content and receive raw metadata blobs that may contain more than the API contract intends.
- This aids reconnaissance and increases coupling between clients and server internals.

Probability: High

Evidence:
- `/ingest` returns `path` for each uploaded photo (`api/app/routes/bins.py:70-73`).
- `/bins/{bin_id}/add` also returns stored `path` values (`api/app/routes/bins.py:160-161`).
- `/bins/{bin_id}` returns `photos`, which are populated from `SELECT photo_id, path, device_metadata FROM photos` (`api/app/routes/bins.py:227-233`, `api/app/db/repository.py:211-226`).

Recommended remediation:
- Return opaque photo identifiers or signed file URLs instead of filesystem paths.
- Expose only a documented subset of photo metadata.
- Keep storage layout and server-side paths private.

### F-11: `device_metadata` is accepted as arbitrary JSON with no schema or size bounds

Severity: Low

Impact:
- Clients can persist arbitrarily shaped JSON into the database.
- This creates future compatibility and storage-abuse risk, and it can accidentally capture more sensitive device data than intended.

Probability: Medium

Evidence:
- `/ingest` accepts `device_metadata` as a free-form form field and only checks that it parses as JSON (`api/app/routes/bins.py:28-41`).
- The parsed object is stored directly into `jsonb` with no schema validation, field allowlist, or size controls (`api/app/db/repository.py:163-176`).

Recommended remediation:
- Define a schema for allowed metadata fields and reject anything outside it.
- Cap serialized size.
- Consider dropping or hashing sensitive device identifiers before persistence.

## Secrets and Credentials Review

No hardcoded production credentials were found in tracked repository files during the audit. The current posture is mixed but generally reasonable:

- `.env` is gitignored (`.gitignore:1-15`).
- The installer generates a random PostgreSQL password instead of embedding a default value (`install.sh:249-275`).
- API keys are generated with `secrets.token_urlsafe(32)` and only the SHA-256 hash is stored (`api/app/db/repository.py:550-559`).
- The README still instructs operators to hand-create `.env` and exposes local examples that place sensitive connection strings into shell history unless handled carefully (`README.md:36-39`, `README.md:128-129`).

Recommended follow-up:
- Add a `.env.example` with placeholders and move operator guidance away from inline secrets.
- Consider prefixing or labeling admin keys separately once scoped authorization exists.
- Add automated secret scanning to pre-commit and CI once a CI pipeline exists.

## CI/CD Review

No in-repository workflow directories were present under `.github/`, `.gitlab/`, `.circleci/`, or `.azure-pipelines/` at the time of review, so no CI secret exposure or workflow-level privilege escalation was identified inside this repository. The tradeoff is that there is also no evidence of automated:

- dependency auditing
- secret scanning
- container image scanning
- SAST/DAST checks

Recommended follow-up:
- Add at least dependency audit, secret scan, and image scan steps to the project’s eventual CI pipeline.
- Fail builds on new critical or high-severity dependency advisories.

## Controls Verified

- SQL queries are parameterized through SQLAlchemy text bindings rather than raw string interpolation for user input (`api/app/db/repository.py:13-620`).
- The container runtime switches to a non-root user (`api/Dockerfile:31-35`).
- The main unhandled exception handler returns a generic 500 response rather than leaking a traceback (`api/app/main.py:180-194`).
- Request IDs are attached to responses, which helps incident correlation (`api/app/main.py:61-67`).

## Verification Notes

Commands run during the assessment:

```bash
rg --files .
rg -n "password|secret|token|api[_-]?key|DATABASE_URL|POSTGRES|OLLAMA|Authorization|x-api-key" . --hidden -g '!api/.venv/**'
uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements.txt
uvx --python 3.12 --from pip-audit pip-audit -r <exported requirements> --no-deps --disable-pip
find . -maxdepth 3 -type d \( -name '.github' -o -name '.gitlab' -o -name '.circleci' -o -name '.azure-pipelines' \)
```

Dependency-audit result summary:

```text
Found 12 known vulnerabilities in 5 packages
onnx 1.20.1: 6 advisories, fixed in 1.21.0
pillow 10.4.0: CVE-2026-25990, fixed in 12.1.1
python-multipart 0.0.12: CVE-2024-53981, CVE-2026-24486, fixed in 0.0.18/0.0.22
requests 2.32.5: CVE-2026-25645, fixed in 2.33.0
starlette 0.45.3: CVE-2025-54121, CVE-2025-62727, fixed in 0.47.2/0.49.1
```

Overall conclusion:

The codebase is readable and relatively small, but it is not ready for hostile-network exposure in its current form. Fix the two critical trust-boundary issues first:

1. Constrain upload paths and remove user influence over storage layout.
2. Remove free-form detection model path selection and introduce real admin-only authorization.

Immediately after that, harden uploads, upgrade vulnerable dependencies, and add request-level abuse controls.
