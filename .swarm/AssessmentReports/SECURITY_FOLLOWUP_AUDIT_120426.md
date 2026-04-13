# BinBrain Security Follow-Up Audit

Assessment date: 2026-04-12

Requested prompt path: `.swarm/Prompts/SEC_002_followup_audit.md`

Prompt resolution note:
- The requested prompt file does not exist on `origin/main`, the current worktree, or any branch history reachable from this repository.
- The nearest unambiguous interpretation of the request is a follow-up audit of the security remediation branches already present in the repo.

Audited branch states:
- `origin/fix/security-phase-1-2` @ `5bef244`
- `origin/fix/security-phase-3` @ `304ec0e`

Scope:
- Verification of the original 2026-04-12 findings `F-01` through `F-11` from `.swarm/AssessmentReports/SECURITY_ASSESSMENT_120426.md`
- Manual source review of the remediated code paths
- Targeted dependency audit
- Targeted security test execution plus one full API-suite run on the latest remediation branch

Method:
- Source review in detached audit worktrees for the two remediation branches.
- Dependency audit with `uv export ... | pip-audit`.
- Security test execution:
  - `origin/fix/security-phase-1-2`: 39 passed, 37 skipped
  - `origin/fix/security-phase-3`: 30 passed, 17 skipped
  - `origin/fix/security-phase-3` full API suite: 100 passed, 152 skipped

## Executive Summary

The remediation work is substantial and mostly effective. The original critical findings around arbitrary model-path loading and missing admin authorization are closed, the path-traversal write primitive is blocked for new uploads, upload type validation is materially stronger, error handling is sanitized, rate limiting exists, and the Docker exposure defaults are tightened.

The remediation is not complete. Three original findings remain partially open, and one new follow-up gap is visible once the original write-path issue is considered against legacy data. The most important remaining problems are:

1. ~~The body-size middleware still trusts `Content-Length`, so oversized chunked uploads can bypass the request cap.~~ **CLOSED** via PR #11 (2026-04-12).
2. `Pillow 10.4.0` remains in the runtime lock despite the required dependency remediation.
3. Read-side photo handlers still trust stored `photos.path` values, which leaves legacy malicious rows or DB tampering as an arbitrary local file-read primitive.
4. `device_metadata` is still returned in normal bin responses, so the original low-severity disclosure issue is only partially remediated.

## Original Findings Status

| Original ID | Status | Notes |
| --- | --- | --- |
| F-01 | Partial | New writes are constrained, but read-side handlers still trust stored `photos.path` rows. |
| F-02 | Closed | Detection model selection is allowlisted and admin-gated. |
| F-03 | Closed | Role-based API-key authorization now exists and is enforced on admin/control-plane routes. |
| F-04 | Closed | Streaming ASGI body-size middleware added (PR #11, `af99a5d`); FF-01 closed. |
| F-05 | Closed | Uploads are streamed to temp files, magic-byte checked, PIL-verified, and renamed only on success. |
| F-06 | Partial | `python-multipart`, `onnx`, `requests`, and `starlette` were upgraded; `Pillow` remains vulnerable. |
| F-07 | Closed | Raw exception text is no longer reflected in normal 5xx/502/400 control paths reviewed here. |
| F-08 | Closed | Global and endpoint-specific rate limiting are in place and tested. |
| F-09 | Closed | Compose ports are bound to `127.0.0.1`. |
| F-10 | Partial | Filesystem paths are removed from responses, but `device_metadata` is still disclosed. |
| F-11 | Closed | `device_metadata` now has schema, size, depth, and string-length bounds, with sensitive-field hashing. |

## Follow-Up Findings

### FF-01: Request-body cap remains bypassable when `Content-Length` is absent or untrusted

**Status:** CLOSED — 2026-04-12 via PR #11 (squash commit `af99a5d`) on branch `fix/security-followup-ff01-ff02`. Replaced with pure ASGI `BodySizeLimitMiddleware` that counts cumulative `http.request` bytes and short-circuits with 413 before the handler runs. SECURITY sign-off: `.swarm/AssessmentReports/SECURITY_FF01_REVIEW_120426.md`.

Severity: High (original)

Impact:
- Attackers can still stream oversized multipart bodies without a trustworthy `Content-Length` header.
- The app will only enforce size after request parsing reaches the route handlers, which leaves the upstream body ingestion path exposed to memory, disk, and CPU pressure.
- This keeps the original upload DoS class partially open.

Probability: Medium

Evidence:
- `origin/fix/security-phase-3 (304ec0e): api/app/main.py:82-104` only checks `request.headers["content-length"]` and otherwise forwards the request unchanged.
- `origin/fix/security-phase-3 (304ec0e): api/app/routes/bins.py:64-89` enforces per-file size only after `UploadFile` objects already exist inside the handler.
- `origin/fix/security-phase-3 (304ec0e): api/tests/test_security_f04_upload_limits.py:94-107` validates only a spoofed `Content-Length` case; it does not cover chunked or headerless multipart bodies.

Recommended remediation:
- Enforce body size on actual bytes read from the ASGI receive channel, not on `Content-Length` alone.
- If the framework/server stack cannot enforce this in-app, add a hard ingress limit at the reverse proxy and reject multipart requests without a bounded body.
- Add a negative test that sends a multipart request without `Content-Length` (or via chunked transfer) and verify it is rejected with `413`.

### FF-02: Known vulnerable `Pillow` version remains in the runtime dependency set

Severity: High

Impact:
- The application still parses attacker-controlled image content through a runtime version flagged by `pip-audit`.
- That leaves a known image-processing vulnerability in active code paths used by upload validation and image resizing.
- The original dependency-remediation objective is therefore not complete.

Probability: Medium

Evidence:
- `origin/fix/security-phase-3 (304ec0e): api/pyproject.toml:17-19` explicitly keeps `Pillow>=10.4.0,<11.0.0`.
- `origin/fix/security-phase-3 (304ec0e): api/uv.lock:1066-1086` resolves `pillow 10.4.0`.
- `origin/fix/security-phase-3 (304ec0e): api/app/services/image_validation.py:72-79` invokes Pillow during upload validation.
- `origin/fix/security-phase-3 (304ec0e): api/app/routes/photos.py:153-161` invokes Pillow during on-demand image resize.
- Follow-up `pip-audit` output on the remediation branch reported: `pillow 10.4.0  CVE-2026-25990  fix 12.1.1`.

Recommended remediation:
- Remove the `Pillow<11` constraint by upgrading or replacing the transitive blocker (`fastembed` or its image-related dependency chain), then lock to `Pillow>=12.1.1`.
- If that is not immediately feasible, isolate image parsing into a separately versioned service or hardened worker and treat the residual CVE as a blocking release risk until resolved.
- Document the blocker as an explicit architectural exception rather than leaving only an inline TODO.

### FF-03: Read-side photo handlers still trust stored filesystem paths, leaving legacy-row arbitrary file read exposure

Severity: Medium

Impact:
- A malicious `photos.path` row created before the write-side fixes, or injected through database compromise, is still treated as a trusted local filesystem path.
- `/photos/{photo_id}/file` can therefore expose arbitrary local files, and `/photos/{photo_id}/detect` can feed arbitrary local paths into downstream processing.
- This is a migration and trust-boundary gap left open after the original path-traversal remediation.

Probability: Medium

Evidence:
- `origin/fix/security-phase-3 (304ec0e): api/app/routes/photos.py:138-161` reads `photo_path` from the database and opens it directly without verifying it remains under `photo_root`.
- `origin/fix/security-phase-3 (304ec0e): api/app/routes/photos.py:205-210` passes the stored `photo_path` directly into detection.
- `origin/fix/security-phase-3 (304ec0e): api/app/db/repository.py:246-250` returns the raw path from `photos.path`.
- `origin/fix/security-phase-3 (304ec0e): api/app/routes/photos.py:176-185` re-validates path confinement only on delete, not on read or detect.

Recommended remediation:
- Apply the same `photo_root` confinement check to every read-side path use (`/file`, `/detect`, and any future vision/OCR routes).
- Add a migration or operational scrub that finds `photos.path` rows outside `PHOTO_DIR`, quarantines them, and prevents continued use.
- Add tests that seed a legacy out-of-root `photos.path` row and verify both `/photos/{id}/file` and `/photos/{id}/detect` fail safely.

### FF-04: `device_metadata` is still disclosed in normal bin responses

Severity: Low

Impact:
- Authenticated callers can still retrieve stored device-processing metadata for every photo in a bin.
- The new schema and hashing reduce sensitivity, but the app still discloses operational telemetry and OCR/classification payloads that were part of the original low-severity disclosure finding.
- This means `F-10` is only partially remediated.

Probability: High

Evidence:
- `origin/fix/security-phase-3 (304ec0e): api/app/db/repository.py:211-226` still selects `device_metadata` in `fetch_bin_photos`.
- `origin/fix/security-phase-3 (304ec0e): api/app/routes/bins.py:348-359` strips only `path` and returns the remaining photo fields unchanged.
- `origin/fix/security-phase-3 (304ec0e): api/tests/test_security_f10_path_disclosure.py:1-52` verifies only filesystem-path removal and does not assert metadata suppression.

Recommended remediation:
- Remove `device_metadata` from default `/bins/{bin_id}` responses, or return only a minimal approved subset needed by clients.
- If raw metadata is operationally necessary, move it to an admin-only or debug-only endpoint.
- Extend the F-10 test coverage to assert that sensitive metadata fields are absent from normal user-plane responses.

## Verified Closures

These remediations appear effective in the audited branch state:

- `F-02` closed:
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/admin.py:187-223`
  - `origin/fix/security-phase-3 (304ec0e): api/app/services/detection.py:31-47`
  - `origin/fix/security-phase-3 (304ec0e): api/app/main.py:27-34`

- `F-03` closed:
  - `origin/fix/security-phase-3 (304ec0e): api/app/main.py:136-193`
  - `origin/fix/security-phase-3 (304ec0e): api/app/middleware.py:8-22`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/admin.py:79-259`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/locations.py:22-63`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/classes.py:31-89`
  - `origin/fix/security-phase-1-2 (5bef244): migrations/2026-04-12_add_api_key_role.sql:19-28`

- `F-05` closed:
  - `origin/fix/security-phase-3 (304ec0e): api/app/services/image_validation.py:49-87`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/bins.py:92-110`
  - `origin/fix/security-phase-3 (304ec0e): api/tests/test_security_f05_file_validation.py:125-166`

- `F-07` closed:
  - `origin/fix/security-phase-3 (304ec0e): api/app/main.py:196-260`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/admin.py:25-31`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/items.py:79-81`

- `F-08` closed:
  - `origin/fix/security-phase-3 (304ec0e): api/app/main.py:110-133`
  - `origin/fix/security-phase-3 (304ec0e): api/app/services/rate_limiter.py:34-146`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/photos.py:198-228`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/upc.py:15-16`

- `F-09` closed:
  - `origin/fix/security-phase-3 (304ec0e): docker-compose.yml:12-13`
  - `origin/fix/security-phase-3 (304ec0e): docker-compose.yml:45-46`

- `F-11` closed:
  - `origin/fix/security-phase-3 (304ec0e): api/app/services/metadata_schema.py:24-161`
  - `origin/fix/security-phase-3 (304ec0e): api/app/routes/bins.py:129-137`
  - `origin/fix/security-phase-3 (304ec0e): api/tests/test_security_f11_metadata_schema.py:31-204`

## Verification Notes

- Dependency audit:
  - `pip-audit` on the remediation branch still reports one known vulnerability: `pillow 10.4.0 -> CVE-2026-25990`, fixed in `12.1.1`.

- Tests:
  - `origin/fix/security-phase-1-2`: targeted security tests passed (`39 passed, 37 skipped`).
  - `origin/fix/security-phase-3`: targeted security tests passed (`30 passed, 17 skipped`).
  - `origin/fix/security-phase-3`: full API test suite passed (`100 passed, 152 skipped`).

## Conclusion

The remediation branches materially improve the security posture and close the original highest-risk code-execution and authorization gaps. They are not ready to be treated as fully complete security remediations until the body-size enforcement is made transport-safe, the remaining Pillow CVE is removed from the runtime lock, read-side path confinement is added for legacy rows, and `device_metadata` disclosure is intentionally scoped or removed.
