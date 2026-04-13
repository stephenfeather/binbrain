# Dev1_006 — Close GH Issue #4: rate-limit middleware ordering assertion

Minor defense-in-depth follow-up from PR #2 aegis re-review. One file: `api/app/main.py` (plus tests).

## Background

`global_rate_limit_middleware` (main.py:113–133) keys by `request.state.api_key_id`, falling back to the literal string `"anon"` when the attribute is missing. Starlette middleware is registered LIFO — the current file order (global-limit defined above api-key-auth) depends on that. A future reorder (or a request path that bypasses auth) would make the limiter key by `"anon"`, collapsing all unauthenticated traffic into one shared bucket and potentially bypassing auth-derived identity.

## Scope

### 1. Per-IP fallback instead of "anon"

In `api/app/main.py:117`, replace:

```python
key = str(getattr(request.state, "api_key_id", "anon"))
```

with a helper call that returns `ip:<client_ip>` when `api_key_id` is missing:

```python
key = _rate_limit_key_for(request)
```

Add the helper near the middleware (or in a small module-level function):

```python
def _rate_limit_key_for(request: Request) -> str:
    """Return the rate-limit bucket key for *request*.

    Prefers the authenticated api_key_id (set by api_key_auth_middleware).
    Falls back to `ip:<client_ip>` so unauthenticated requests get per-IP
    buckets instead of sharing a single "anon" bucket.
    """
    api_key_id = getattr(request.state, "api_key_id", None)
    if api_key_id is not None:
        return str(api_key_id)
    client = request.client
    ip = client.host if client else "unknown"
    return f"ip:{ip}"
```

### 2. Startup assertion on middleware ordering

Add a module-level pure helper:

```python
def _assert_auth_runs_before_rate_limit(user_middleware) -> None:
    """Fail-fast if middleware registration order would put rate-limit outside of auth.

    Starlette applies middleware LIFO — the LAST registered runs FIRST.
    api_key_auth_middleware must therefore be registered AFTER
    global_rate_limit_middleware so auth executes first and populates
    request.state.api_key_id before the limiter reads it.

    Raises RuntimeError with a clear message if the invariant is broken.
    """
    names = []
    for mw in user_middleware:
        # Each entry exposes a `cls` attribute (Starlette Middleware);
        # for @app.middleware("http") decorators the callable is wrapped.
        # Inspect repr / attributes to find the endpoint function name.
        fn = getattr(mw, "kwargs", {}).get("dispatch") or getattr(mw, "cls", None)
        names.append(getattr(fn, "__name__", repr(fn)))

    try:
        auth_idx = names.index("api_key_auth_middleware")
        rate_idx = names.index("global_rate_limit_middleware")
    except ValueError as exc:
        raise RuntimeError(
            f"Expected both api_key_auth_middleware and global_rate_limit_middleware "
            f"in user_middleware; got {names}"
        ) from exc

    # LIFO: higher index = registered later = runs earlier (outer).
    # api_key_auth must run BEFORE rate-limit, so it must be OUTER,
    # which means it must have a HIGHER index than rate-limit.
    if auth_idx <= rate_idx:
        raise RuntimeError(
            f"Middleware ordering invariant violated: api_key_auth_middleware "
            f"must be registered AFTER global_rate_limit_middleware so it runs "
            f"first (LIFO). Current order: {names}"
        )
```

Note: the exact attribute walk may need tweaking based on how `@app.middleware("http")` exposes the endpoint name. First write a small probe test (`print([repr(mw) for mw in app.user_middleware])`) against the real app to confirm how the decorated functions surface, then adjust.

Call it at startup — either at module load after all middleware are registered, or inside an `@app.on_event("startup")` / lifespan handler. Module load is fine and fail-fast'iest:

```python
# At the end of main.py, after all @app.middleware decorators:
_assert_auth_runs_before_rate_limit(app.user_middleware)
```

### 3. Tests

Create `api/tests/test_security_f08_middleware_ordering.py` with:

1. **`test_rate_limit_key_for_returns_api_key_id_when_set`** — build a `Request`-like stub with `state.api_key_id = "key-xyz"`, assert `_rate_limit_key_for(stub) == "key-xyz"`.
2. **`test_rate_limit_key_for_falls_back_to_ip_when_missing`** — stub with no `api_key_id`, client host `"1.2.3.4"`, assert `"ip:1.2.3.4"`.
3. **`test_rate_limit_key_for_handles_missing_client`** — stub with no `api_key_id` and `request.client = None`, assert `"ip:unknown"`.
4. **`test_assert_middleware_order_passes_on_correct_order`** — build a fake middleware list with `global_rate_limit_middleware` before `api_key_auth_middleware` (registration order), assert it does NOT raise.
5. **`test_assert_middleware_order_raises_on_reversed`** — same two but reversed, assert `RuntimeError` with a message containing "ordering invariant".
6. **`test_assert_middleware_order_raises_on_missing_middleware`** — only one of the two present, assert `RuntimeError`.

You'll need to import the helpers. If Starlette's middleware-list shape makes constructing fakes painful, refactor `_assert_auth_runs_before_rate_limit` to accept a simpler `names: list[str]` instead and do the attribute-walk inline at the call site. Cleaner for testing.

## Success criteria

1. `_rate_limit_key_for` helper exists, used by `global_rate_limit_middleware`, and defaults to `ip:<client.host>` (or `ip:unknown`).
2. `_assert_auth_runs_before_rate_limit` runs at app startup (module load or lifespan) and raises `RuntimeError` if invariant is broken.
3. 6 new unit tests pass, isolated (no DB, no HTTP).
4. Full suite green: `TEST_DATABASE_URL=postgresql+psycopg://binbrain:claude_dev@localhost:5434/binbrain uv run --project api --python 3.12 pytest api/tests -q` → 261 passed (255 existing + 6 new).
5. Manually verify app still starts: `uv run --project api --python 3.12 uvicorn api.app.main:app` boots without RuntimeError.

## Process

- Branch: `fix/issue-4-middleware-ordering` off `origin/main`
- TDD: write the tests first (start with the `_rate_limit_key_for` ones — easiest), then implement.
- One commit is fine.
- PR body must include `Closes #4`.
- Say `ARCHITECT TASK COMPLETED: Pull Request #N` when done.

## Non-goals

- Do NOT refactor the middlewares themselves — keep behavior identical except for the fallback-key change.
- Do NOT add a second rate limiter or global-config changes. Focus on the assertion + fallback.
- If Starlette's `user_middleware` API is too opaque to introspect reliably, ARCHITECT REQUEST — don't fabricate attribute access.
