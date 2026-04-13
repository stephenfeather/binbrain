# SECURITY Review — PR #14 (FF-04 device_metadata disclosure)

**Date:** 2026-04-13
**Reviewer:** SECURITY
**PR:** https://github.com/stephenfeather/binbrain/pull/14
**Branch:** `dev/Developer1`
**Verdict:** ✅ **APPROVE**

## 1. Security: device_metadata genuinely absent — PASS

`api/tests/test_security_ff04_device_metadata_disclosure.py` (2 tests):

| Test | What it proves |
|------|----------------|
| `test_get_bin_photos_do_not_include_device_metadata` | Ingests a photo with non-null `device_metadata` (with OCR text "secret serial"), then asserts `GET /bins/FF04GETBIN01` → photos contain no `device_metadata`, no `path`, but still contain `photo_id` |
| `test_get_bin_photos_omit_device_metadata_even_when_null` | Ingest without metadata still has `device_metadata` key absent in response |

Plus `test_device_metadata.py:test_get_bin_persists_device_metadata_without_leaking` asserts both **DB persistence** (via direct `SELECT device_metadata FROM photos`) and **user-plane absence** — proves the repository layer still stores metadata for any future admin use while the route layer strips it.

CI pytest: **273 passed / 1 skipped** (matches PR body claim).

## 2. Allowlist scope — PASS (SPECIAL ATTENTION check)

**Prompt flagged this as a risk:** "`frozenset({'photo_id'})` may break existing clients (iOS, admin UI). Verify against actual callers before approving."

### Evidence gathered

| Source | Finding |
|--------|---------|
| Repo layout (`eza`) | No `apps/`, no `ios/`, no admin frontend in this repo. Only `api/` + scripts + docs. |
| `binbrain-api-schemas.json:228-237` — **the published `GetBinResponse` JSON schema** | `photos[]` items: `required: ["photo_id"]`, `properties: {photo_id}`, **`additionalProperties: false`**. The contract forbids any field other than `photo_id` on photo objects. |
| `api/app/db/repository.py:211-226` `fetch_bin_photos` | Only `SELECT photo_id, path, device_metadata` — the pre-PR response shape was `{photo_id, device_metadata}` after F-10's `path` strip. No `captured_at`/`mime_type`/`width`/`height` ever existed in this projection. |
| `scripts/live_api_tests.py:40-45` (only in-repo caller) | Only inspects `body["bin_id"]`; does not read any photo field. |
| `docs/openapi.yaml` (`GetBinResponse`) | No `captured_at`/`mime_type`/`width`/`height` declared on bin photo objects. |

### Conclusion

The allowlist `frozenset({"photo_id"})` is **exactly the published schema contract**. Prior to this PR, the response was technically a **schema violation** (returning `device_metadata` against `additionalProperties: false`). The PR brings implementation in line with contract.

No iOS or admin frontend exists in this repo, and the only in-tree caller (`live_api_tests.py`) reads only `bin_id`. There is no functional regression risk for any caller in this repo.

If/when the mobile or admin UI is added and needs additional fields, they must be:
1. Added to `binbrain-api-schemas.json` `GetBinResponse` properties (contract change), then
2. Added to the allowlist (code change).

The FF-04 comment in `bins.py:354-357` documents this workflow explicitly.

## 3. Repository layer unchanged — PASS

`api/app/db/repository.py:211-226` `fetch_bin_photos` is untouched — still selects `photo_id, path, device_metadata`. Admin/internal code can still read `device_metadata` by calling the repository directly or by adding a dedicated admin endpoint that projects it explicitly. The `test_device_metadata.py` update provides a positive DB-level assertion that persistence is unaffected.

## 4. Regression — PASS

CI pytest: **273 passed, 1 skipped in 4.63s** (run 24335392627, job 71050862827). Matches PR body. F-01/F-05/F-07/F-08/F-10/F-11/FF-03 all green.

## 5. Scope discipline — PASS

`gh pr view 14 --json files`:

| File | Change |
|------|--------|
| `api/app/routes/bins.py` | +12/-2 (allowlist constant + projection swap) |
| `api/tests/test_security_ff04_device_metadata_disclosure.py` | +62 (new) |
| `api/tests/test_device_metadata.py` | +22/-20 (updated contract + DB-persistence assertion) |
| `.swarm/Prompts/Dev1_008_ff04_device_metadata.md` | +85 (prompt note) |

No `pyproject.toml`, `uv.lock`, `repository.py`, or unrelated route changes. Clean, minimal diff.

## Other CI signals

| Check | Status |
|-------|--------|
| pytest | ✅ 273/1 |
| gitleaks | ✅ |
| CodeRabbit | ✅ |
| qlty | ⚠️ 12 blocking — pre-existing, not introduced by this PR |
| pip-audit | ❌ unrelated (FF-02 Pillow still pending) — merged PR #12 ensures it at least runs to completion; `continue-on-error: true` in place |

## Verdict

APPROVE. The device_metadata disclosure is closed, the allowlist matches the published JSON schema contract (which is stricter than I expected — the previous response was actually in violation), the repository layer remains neutral so admin uses can still access metadata, and CI is green. FF-04 can be closed after merge.

### Note on the SPECIAL ATTENTION flag

The prompt worried `frozenset({'photo_id'})` might be too narrow and break iOS/admin clients. Evidence shows:
- No such clients exist in this repo.
- The published schema explicitly requires `additionalProperties: false` with only `photo_id` allowed.
- The sole in-tree caller of `GET /bins/{bin_id}` that inspects the body reads only `bin_id`.

The allowlist is correctly scoped. No widening needed.
