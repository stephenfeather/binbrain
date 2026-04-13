# Dev1_004 — Close GH Issue #5: rate limiter polish

Small Info-severity follow-up from the PR #2 re-review. Two items in `api/app/services/rate_limiter.py`:

## Scope

### 1. Env-tunable tracked-keys cap

`_MAX_TRACKED_KEYS = 10_000` is hardcoded. Make it env-tunable:

```python
_MAX_TRACKED_KEYS = int(os.environ.get("RATE_LIMIT_MAX_TRACKED_KEYS", "10000"))
```

Match the pattern already used for `GLOBAL_RATE_LIMIT`, `VISION_RATE_LIMIT`, etc.

### 2. FIFO vs LRU alignment

Current eviction in `SlidingWindowRateLimiter.check` uses `next(iter(self._calls))` which evicts by **insertion order** (FIFO), not by **recency of access** (LRU). The docstring and comment reference "LRU/FIFO" — pick one:

**Recommended: rename to FIFO.** The semantic cost of true LRU here is minimal (keys stay alive while any recent call is in the window anyway, and the whole dict is pruned as windows expire). Update the docstring, the comment at the eviction site, and the test name (`test_lru_cap_evicts_oldest_key` → `test_fifo_cap_evicts_oldest_key`).

If you strongly prefer true LRU, use `OrderedDict` with `move_to_end(key)` on each `check()` call — but justify in the PR that it's worth the per-call reorder cost.

## Success criteria

1. `RATE_LIMIT_MAX_TRACKED_KEYS` env var overrides the default when set.
2. All docstring/comment references to "LRU" are corrected to match the actual behavior (FIFO), OR a true-LRU implementation is added with justification.
3. Existing test for eviction behavior still passes (rename it to match).
4. Add one unit test that verifies the env var is respected (e.g., set `RATE_LIMIT_MAX_TRACKED_KEYS=3`, reimport module, assert the module-level constant reflects it, or inject via construction).
5. Full test suite green locally: `TEST_DATABASE_URL=postgresql+psycopg://binbrain:claude_dev@localhost:5434/binbrain uv run --project api pytest api/tests -q` → 253+ passed.

## Process

- Branch: `fix/issue-5-rate-limiter-polish` off `origin/main`
- TDD: write/rename the tests first, then adjust production.
- One commit is fine — this is small.
- Close GH #5 in the PR body with `Closes #5`.
- Say `ARCHITECT TASK COMPLETED: Pull Request #N` when done.

If you hit ambiguity on the LRU-vs-FIFO choice, pick FIFO (simpler) — no need to ARCHITECT REQUEST.
