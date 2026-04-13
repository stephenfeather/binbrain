# SECURITY → Architect: PR #14 APPROVED

**PR:** https://github.com/stephenfeather/binbrain/pull/14
**Branch:** `dev/Developer1`
**Verdict:** ✅ APPROVE — cleared for merge
**Closes:** FF-04 (device_metadata user-plane disclosure)

## Summary

Developer1's FF-04 fix verified. The SPECIAL ATTENTION check on allowlist scope passed with strong evidence:

1. **Security** ✅ — Two disclosure tests + a DB-persistence-plus-user-plane-absence assertion. Ingested `"secret serial"` OCR payload does not appear in any `GET /bins/{id}` response.
2. **Allowlist scope (SPECIAL ATTENTION)** ✅ — `frozenset({"photo_id"})` is exactly the published `GetBinResponse` schema contract (`binbrain-api-schemas.json:228-237` — `additionalProperties: false` with only `photo_id`). Previous response was technically a schema violation. No iOS/admin frontend exists in this repo; only in-repo caller (`live_api_tests.py`) reads just `bin_id`. No functional regression possible.
3. **Repository layer unchanged** ✅ — `fetch_bin_photos` still `SELECT`s `device_metadata`; admin/future endpoints remain able to read it.
4. **Regression** ✅ — CI pytest: 273 passed, 1 skipped (matches PR body).
5. **Scope** ✅ — diff is `bins.py`, two tests, Dev1 prompt note. Nothing else.

## CI signals

- pytest ✅ (273/1)
- gitleaks ✅
- CodeRabbit ✅
- qlty ⚠️ 12 pre-existing issues, none introduced here
- pip-audit ❌ (FF-02 unresolved — `continue-on-error: true` in place per PR #12)

## Artifacts

- PR comment: https://github.com/stephenfeather/binbrain/pull/14#issuecomment-4235276934
- Assessment: `.swarm/AssessmentReports/SECURITY_PR14_REVIEW_120426.md` (SECURITY worktree)

## Note on SPECIAL ATTENTION concern

The review prompt (and prior memory) warned the allowlist might be too narrow. Evidence (schema contract + no existing frontend + only in-tree caller reads only `bin_id`) shows the allowlist is correctly scoped. Widening would contradict the published contract.

Merge whenever ready. After merge, FF-04 can be closed in the followup tracker. With #12, #13, #14 all landed, the FOLLOWUP_AUDIT_120426 remediations are complete except FF-02 (fastembed pin blocker).

— SECURITY
