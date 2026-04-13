# Dev1_008 — FF-04: device_metadata disclosure in bin responses

From `.swarm/AssessmentReports/SECURITY_FOLLOWUP_AUDIT_120426.md` (FF-04, Low). Severity low but the original F-10 is only partially remediated until this is closed.

## First: sync with main

Your worktree is behind main (PRs #12 pip-audit and #13 FF-03 both merged). Before starting:

```bash
git fetch origin main
git rebase origin/main
```

Resolve any conflicts (unlikely — this work touches different files than #12/#13).

## Background

`F-10` stripped filesystem paths from bin responses. But `device_metadata` — operational telemetry, OCR output, classification payloads — is still returned in normal `/bins/{bin_id}` responses. `F-11` added schema and hashing so what IS returned is sanitized, but the default user-plane response still includes it. That's the remaining gap.

## Evidence (from the audit)

- `api/app/db/repository.py:211-226` — `fetch_bin_photos` still selects `device_metadata`.
- `api/app/routes/bins.py:348-359` — response strips only `path`, returns remaining photo fields unchanged (so `device_metadata` flows through).
- `api/tests/test_security_f10_path_disclosure.py:1-52` — only asserts path removal; does not assert metadata suppression.

## Scope

### 1. Remove `device_metadata` from default user-plane bin responses

In `api/app/routes/bins.py` (around line 348-359 — the photo-projection step inside `GET /bins/{bin_id}` and any sibling endpoints that return photos), drop `device_metadata` from the returned dict along with `path`.

Prefer an allowlist over a denylist. Define the set of public photo fields explicitly and project to those. Example shape (adjust to match actual field names in the codebase):

```python
_PUBLIC_PHOTO_FIELDS = {
    "id", "bin_id", "captured_at", "uploaded_at",
    "mime_type", "width", "height", "detection_status",
    # ... whatever else clients need
}
```

Strip any field outside the allowlist before returning. Document why this matters in a short comment referencing FF-04.

You do NOT need to change `repository.py:211-226` — fetching metadata from the DB is fine; what matters is it doesn't leak into user responses. Keep the DB layer neutral so admin endpoints can still access it.

### 2. Consider whether any existing endpoint legitimately needs `device_metadata`

`grep "device_metadata" api/app/routes/` — if any handler (e.g. an admin route) already uses it intentionally, leave that alone. The scope is default user-plane responses only.

If no handler needs it yet and the product plan requires it be surfaced somewhere, DO NOT invent a new endpoint in this PR. Flag it as a TODO in the PR description and coordinate with the Architect separately.

### 3. Extend F-10 test coverage

Update `api/tests/test_security_f10_path_disclosure.py` (or add a companion test file `test_security_ff04_device_metadata_disclosure.py` — your call) to assert:

- After a photo with non-null `device_metadata` is inserted, `GET /bins/{bin_id}` returns photo objects that do NOT contain a `device_metadata` key.
- Same for any other endpoint you audited that returns photo projections.
- Positive control: other photo fields (id, captured_at, mime_type, etc.) are still present.

Follow the existing fixture pattern in `test_security_f10_path_disclosure.py`.

### 4. Run the full security suite

```bash
cd api
uv run pytest tests/ -k "security" -q
uv run pytest tests -q
```

F-01/F-05/F-07/F-08/F-10/F-11/FF-03 tests must still pass. Record pass/skip counts in the PR body.

## Constraints

- Don't touch `pyproject.toml` or `uv.lock`.
- Don't touch repository layer — strip at the route/projection layer.
- Don't add new admin endpoints in this PR (flag as TODO if needed).
- Allowlist projection is required; denylist is acceptable only if the photo row has unstable field shape (verify by reading the code).

## Deliverable

- Branch: `dev/Developer1` (rebased onto main first).
- Commit the route change, the tests, and any small helper.
- Open PR targeting `main`, titled "FF-04: Remove device_metadata from user-plane bin responses". Body should reference FF-04 in `.swarm/AssessmentReports/SECURITY_FOLLOWUP_AUDIT_120426.md` and list affected endpoints + test counts.
- Print the PR URL when done.
- SECURITY will scan before Architect merges.
