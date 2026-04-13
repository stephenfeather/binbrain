# SEC_005 — Review PR #14 (FF-04 device_metadata disclosure)

PR: https://github.com/stephenfeather/binbrain/pull/14
Branch: `dev/Developer1` (rebased on main after #12/#13 merged)
Addresses: FF-04 from `.swarm/AssessmentReports/SECURITY_FOLLOWUP_AUDIT_120426.md`

## What changed

- `api/app/routes/bins.py`: photo projection now goes through `_PUBLIC_PHOTO_FIELDS = frozenset({"photo_id"})` allowlist, stripping `path` (F-10) and `device_metadata` (FF-04).
- `api/tests/test_security_ff04_device_metadata_disclosure.py`: new (2 tests).
- `api/tests/test_device_metadata.py`: updated to assert DB persistence without user-plane exposure.
- Repository layer unchanged.

## What to verify

### 1. Security: device_metadata genuinely absent

- Read the new `test_security_ff04_device_metadata_disclosure.py` — confirm it:
  - Inserts a photo with non-null `device_metadata`.
  - Asserts `GET /bins/{bin_id}` returns photo objects that do NOT contain a `device_metadata` key.
  - Asserts `path` is also absent (F-10 regression).
- Run: `cd api && uv run pytest tests/test_security_ff04_device_metadata_disclosure.py -v`.

### 2. Scope of allowlist — SPECIAL ATTENTION

The PR uses `_PUBLIC_PHOTO_FIELDS = frozenset({"photo_id"})`. That is a much tighter projection than the previous response shape. **This is a behavior change that may break existing clients** (iOS app, admin UI, etc.).

- Grep for all callers of `GET /bins/{bin_id}` in `apps/ios/`, any admin frontend, and `api/app/routes/` itself.
- Identify fields that callers currently consume (e.g. `captured_at`, `mime_type`, `width`, `height`, `detection_status`, `bin_id`, etc.).
- If the allowlist is genuinely too narrow (functional regression, not just cosmetic), **REQUEST CHANGES** asking Developer1 to widen it to the minimum needed public fields. Do NOT approve a security fix that silently breaks product functionality.
- If `photo_id` really is all clients need (verify by inspecting iOS `BinResponse` / decoding models), note it explicitly in your assessment.

### 3. Repository layer unchanged

Confirm `api/app/db/repository.py` still returns `device_metadata` — admin endpoints and any legitimate internal use must still be able to read it.

### 4. Regression

Run:
```bash
cd api
uv run pytest tests/ -k "security" -q
uv run pytest tests -q
```
All F-series and FF-series tests must still pass. PR body claims 273 passed / 1 skipped full; 146 passed security. Verify.

### 5. Scope discipline

Confirm diff only touches:
- `api/app/routes/bins.py`
- `api/tests/test_security_ff04_device_metadata_disclosure.py`
- `api/tests/test_device_metadata.py`

No `pyproject.toml`, `uv.lock`, or repository changes.

## Deliverable

- Post verdict as PR comment on #14: APPROVE / REQUEST CHANGES with evidence.
- If REQUEST CHANGES because of the allowlist breadth, be specific about which fields clients need (cite evidence from iOS models or existing API consumers).
- Write `.swarm/AssessmentReports/SECURITY_PR14_REVIEW_120426.md`.
- If APPROVED, write Architect handoff at `.swarm/Prompts/ARCH_SEC_005_pr14_approved.md`.
