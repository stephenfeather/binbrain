# SECURITY Review — PR #12 (pip-audit CI fix)

**Date:** 2026-04-12
**Reviewer:** SECURITY
**PR:** https://github.com/stephenfeather/binbrain/pull/12
**Branch:** `dev/Developer1` @ `f27ad61`
**Verdict:** ✅ **APPROVE**

## Scope

One-line change to `.github/workflows/ci.yml`:

```diff
- run: uv export --frozen --no-dev --format requirements-txt -o /tmp/requirements.txt
+ run: uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt
```

## Verification Checklist

| # | Check | Result |
|---|-------|--------|
| 1 | Diff is minimal (ci.yml only, no pyproject/uv.lock edits) | ✅ `gh pr view 12 --json files` shows only `.github/workflows/ci.yml` (+1/-1) |
| 2 | Exported requirements no longer contain editable project | ✅ `grep "^-e \|file:///.*binbrain"` on fresh export returned 0 matches |
| 3 | pip-audit runs to completion | ✅ `uvx pip-audit --requirement /tmp/requirements_pr12.txt --strict` → "No known vulnerabilities found" (no install error) |
| 4 | No regression in dependency coverage | ✅ fastembed 0.3.6, pillow 10.4.0, onnx 1.21.0, starlette 1.0.0, python-multipart 0.0.26, requests 2.33.1, fastapi 0.135.3, pydantic 2.12.5, uvicorn 0.30.6 all present. Export is 1179 lines. |
| 5 | `continue-on-error: true` retained on pip-audit job | ✅ Present at ci.yml:63 with accompanying FF-02 comment |

## Notes

- Local pip-audit returned clean (no Pillow CVE-2026-25990 finding). This is stricter than expected but not a regression — the advisory may have been withdrawn from PyPI/OSV, or the runner environment sees it differently. Either way, the job now reaches the audit step instead of failing at `uv export` / `pip install`.
- `continue-on-error` correctly retained per FF-02 guidance (fastembed pin is load-bearing).
- Out-of-scope items (FF-02 Pillow upgrade, FF-03, FF-04) untouched.

## Recommendation

APPROVE and merge. No follow-up security actions required for this PR. FF-02 remains tracked separately.
