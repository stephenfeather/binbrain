# Dev1_003 — Fix 4 failing tests blocking CI

## Context

PR #6 adds GitHub Actions CI (pytest + pip-audit + gitleaks). The CI won't go green until 4 failing tests on `main` are fixed. All 4 are pre-existing issues not caught by the squash-merge of PR #2; they only surface when the *full* suite runs in order against a real DB.

Your worktree is currently on `standby` at `31a34d9`. Rebase a new branch off `origin/main`:

```bash
git fetch origin
git checkout -b fix/test-stubs-and-schema origin/main
```

## Failing tests

Run locally against the existing test DB to reproduce:

```bash
TEST_DATABASE_URL=postgresql+psycopg://binbrain:claude_dev@localhost:5434/binbrain \
  uv run --project api pytest api/tests -q
```

Expected: 248 pass, 4 fail:

1. `api/tests/test_security_f08_rate_limiting.py::test_detect_endpoint_returns_429_on_rate_limit`
2. `api/tests/test_security_f08_rate_limiting.py::test_suggest_endpoint_returns_429_on_rate_limit`
3. `api/tests/test_security_f08_rate_limiting.py::test_upc_endpoint_returns_429_on_rate_limit`
4. `api/tests/test_schema_validation.py::test_ingest_schema`

## Root causes (pre-investigated)

### Failures 1–3 (rate-limit integration tests)

At `api/app/services/rate_limiter.py:127-143`, the FastAPI dependency factory closes
over the limiter instance at module import time:

```python
def _make_dep(limiter: SlidingWindowRateLimiter):
    def _dep(request: Request) -> None:
        ...
        if not limiter.check(key, role_multiplier=multiplier):   # <-- closure
            raise HTTPException(status_code=429, ...)
    return _dep

require_vision_rate_limit = _make_dep(vision_limiter)
require_upc_rate_limit = _make_dep(upc_limiter)
```

The tests use `_swap_limiter` at `test_security_f08_rate_limiting.py:122-128` to
rebind `rate_limiter.upc_limiter = tighter_limiter` via `setattr`. This does NOT
affect the already-captured closure inside `require_upc_rate_limit`, so the
production limiter (max_calls=30, period=60s) is still doing the checking.
That's why 2 back-to-back requests both return 200.

The 6-second `urlopen timed out` warnings from upcitemdb.com are a separate
smell (tests hit the live internet), but not the cause of the 429 failure.

### Failure 4 (ingest schema)

`api/tests/test_schema_validation.py::test_ingest_schema` validates an ingest
response against a JSON schema. The schema requires `photos[].path`:

```
required: ['photo_id', 'path']
```

but the response under test is `{"photo_id": 1}` — no `path`. This is almost
certainly a consequence of the F-10 path-disclosure remediation (paths removed
from responses) without a matching schema update. Confirm which direction to
go: update the schema (drop `path` from required, or remove from the schema
entirely) if F-10's intent is that paths must never appear in responses.

## Fix strategy (your judgment — these are starting points)

### For the rate-limit tests

You have at least two reasonable options. Pick one with a short rationale in the PR description:

**Option A — make production more testable.** Change `_make_dep` (and the three `require_*_rate_limit` dependencies) so the limiter is resolved by *name* at each request, not captured at import time. For example, have `_make_dep` take a string attribute name and look up `getattr(rate_limiter_module, name)` per call. Tests' existing `setattr` approach then Just Works. Minor runtime cost (one module attribute lookup per request); acceptable.

**Option B — use FastAPI dependency_overrides.** Keep production code as-is; rewrite the integration tests to use `app.dependency_overrides[require_upc_rate_limit] = <tight_dep>` to inject a tighter limiter at the DI boundary. This is the conventional FastAPI-blessed pattern but requires test refactoring.

Either is acceptable. **Do NOT** simply delete the integration tests or mark them skip — that regresses F-08 coverage and will get flagged on review.

Also: while you're here, stub the external `upcitemdb.com` call used by the UPC route so tests don't touch the live internet. The test for `test_upc_endpoint_returns_429_on_rate_limit` should not depend on external HTTP. Use a patch/fixture in `conftest.py` or at the test level. The existing pattern in `conftest.py` already stubs fastembed and YOLO detection — follow the same shape.

### For the schema test

Read the F-10 remediation commits (look at `api/app/routes/*.py` changes in `31a34d9`) to understand what was removed from responses. Then:

- If `path` should never appear in ingest responses (F-10 intent), update the ingest response schema to drop `path` from `required` and from `properties` if no longer emitted.
- If `path` was meant to be preserved but transformed (e.g., relative path only), re-add it to the route response and keep the schema.

Confirm your interpretation in the PR description.

## Success criteria

1. Full suite passes: `TEST_DATABASE_URL=... uv run --project api pytest api/tests -q` exits 0 with 252 passed.
2. No external HTTP in the test run (no `urlopen` warnings for upcitemdb).
3. F-08 rate-limit integration coverage preserved — the three "returns 429" tests still assert the same behavior.
4. No regression in F-10 path-disclosure — paths must not appear in API responses (if you adjust the schema, don't re-expose paths in the route).
5. PR description explains: which option (A or B) you took for the rate-limit fix, and the schema interpretation for F-10.

## Process

- Use TDD discipline: make each failing test pass one at a time, commit per fix.
- One commit per logical fix (rate-limit binding, upcitemdb stub, schema alignment).
- Open PR as `fix: resolve 4 test failures blocking CI` — reference PR #6 as the parent motivation.
- Say `ARCHITECT TASK COMPLETED: Pull Request #N` when done.

If you hit ambiguity (especially on the F-10 schema interpretation), ask via `ARCHITECT REQUEST:` — don't guess.
