"""F-08 (Medium): No rate limiting — tests for tiered budgets.

Tests cover:
- SlidingWindowRateLimiter class (unit, fake-clock injection)
- Four named limiters: global, vision, warmup, upc
- Role multiplier (admin keys get 4× the budget)
- LRU/FIFO eviction (memory bound)
- Integration: 429 returned on the correct endpoints
"""


# ── Unit tests: SlidingWindowRateLimiter (no DB needed) ──────────────────────


def test_rate_limiter_class_exists():
    from app.services.rate_limiter import SlidingWindowRateLimiter

    rl = SlidingWindowRateLimiter(max_calls=5, period=60.0)
    assert rl is not None


def test_rate_limiter_allows_within_limit():
    from app.services.rate_limiter import SlidingWindowRateLimiter

    rl = SlidingWindowRateLimiter(max_calls=3, period=60.0)
    assert rl.check("key1") is True
    assert rl.check("key1") is True
    assert rl.check("key1") is True


def test_rate_limiter_blocks_over_limit():
    from app.services.rate_limiter import SlidingWindowRateLimiter

    rl = SlidingWindowRateLimiter(max_calls=2, period=60.0)
    assert rl.check("key1") is True
    assert rl.check("key1") is True
    assert rl.check("key1") is False  # 3rd call exceeds limit of 2


def test_rate_limiter_keys_are_independent():
    from app.services.rate_limiter import SlidingWindowRateLimiter

    rl = SlidingWindowRateLimiter(max_calls=1, period=60.0)
    assert rl.check("key1") is True
    assert rl.check("key1") is False  # key1 exhausted
    assert rl.check("key2") is True  # key2 unaffected


def test_rate_limiter_reset_clears_state():
    from app.services.rate_limiter import SlidingWindowRateLimiter

    rl = SlidingWindowRateLimiter(max_calls=1, period=60.0)
    assert rl.check("key1") is True
    assert rl.check("key1") is False
    rl.reset("key1")
    assert rl.check("key1") is True  # Reset — allowed again


def test_rate_limiter_reset_all_clears_all_keys():
    from app.services.rate_limiter import SlidingWindowRateLimiter

    rl = SlidingWindowRateLimiter(max_calls=1, period=60.0)
    rl.check("key1")
    rl.check("key2")
    rl.reset()
    assert rl.check("key1") is True
    assert rl.check("key2") is True


def test_rate_limiter_window_expires_with_fake_clock():
    """Fake clock injection avoids real sleeps in test."""
    from app.services.rate_limiter import SlidingWindowRateLimiter

    now = [0.0]

    def fake_clock():
        return now[0]

    rl = SlidingWindowRateLimiter(max_calls=1, period=10.0, time_fn=fake_clock)
    assert rl.check("key1") is True  # allowed at t=0
    assert rl.check("key1") is False  # blocked within window
    now[0] = 11.0  # advance past window
    assert rl.check("key1") is True  # old call expired — allowed again


def test_admin_role_multiplier_increases_budget():
    """Admin keys receive RATE_LIMIT_ADMIN_MULTIPLIER × the base limit."""
    from app.services.rate_limiter import _ADMIN_MULTIPLIER, SlidingWindowRateLimiter

    rl = SlidingWindowRateLimiter(max_calls=2, period=60.0)
    effective_admin_limit = int(2 * _ADMIN_MULTIPLIER)
    # User: 2 calls then blocked
    for _ in range(2):
        rl.check("user1")
    assert rl.check("user1") is False
    # Admin: effective_admin_limit calls allowed
    for _ in range(effective_admin_limit):
        assert rl.check("admin1", role_multiplier=_ADMIN_MULTIPLIER) is True
    assert rl.check("admin1", role_multiplier=_ADMIN_MULTIPLIER) is False


def test_fifo_cap_evicts_oldest_key():
    """When _MAX_TRACKED_KEYS is reached, the oldest key is evicted (FIFO, insertion order)."""
    from app.services import rate_limiter as rl_mod

    original_max = rl_mod._MAX_TRACKED_KEYS
    rl_mod._MAX_TRACKED_KEYS = 3
    try:
        rl = rl_mod.SlidingWindowRateLimiter(max_calls=10, period=60.0)
        rl.check("a")
        rl.check("b")
        rl.check("c")
        # All three in dict; adding "d" should evict "a"
        rl.check("d")
        assert "a" not in rl._calls, "Oldest key 'a' should have been evicted"
        assert "d" in rl._calls
    finally:
        rl_mod._MAX_TRACKED_KEYS = original_max


def test_max_tracked_keys_env_var_respected(monkeypatch):
    """RATE_LIMIT_MAX_TRACKED_KEYS env var controls the module-level constant."""
    import importlib

    import app.services.rate_limiter as rl_mod

    monkeypatch.setenv("RATE_LIMIT_MAX_TRACKED_KEYS", "42")
    importlib.reload(rl_mod)
    try:
        assert (
            rl_mod._MAX_TRACKED_KEYS == 42
        ), f"Expected _MAX_TRACKED_KEYS=42, got {rl_mod._MAX_TRACKED_KEYS}"
    finally:
        # Restore to default so other tests are unaffected
        monkeypatch.delenv("RATE_LIMIT_MAX_TRACKED_KEYS", raising=False)
        importlib.reload(rl_mod)


def test_module_level_limiters_exist():
    from app.services.rate_limiter import (
        SlidingWindowRateLimiter,
        global_limiter,
        upc_limiter,
        vision_limiter,
        warmup_limiter,
    )

    for lim in (global_limiter, vision_limiter, warmup_limiter, upc_limiter):
        assert isinstance(lim, SlidingWindowRateLimiter)


# ── Integration tests: endpoints return 429 when limit hit ───────────────────


def _swap_limiter(rate_limiter_mod, attr: str, max_calls: int):
    """Return (original, temp) — caller must restore original in finally."""
    from app.services.rate_limiter import SlidingWindowRateLimiter

    original = getattr(rate_limiter_mod, attr)
    temp = SlidingWindowRateLimiter(max_calls=max_calls, period=60.0)
    setattr(rate_limiter_mod, attr, temp)
    return original, temp


def test_detect_endpoint_returns_429_on_rate_limit(user_client, app_module):
    """POST /photos/{id}/detect returns 429 when the vision limit is exceeded.

    Uses a role='user' client so the admin 4× multiplier does not inflate
    max_calls=1 to 4, which would allow both requests through.
    """
    from app.services import rate_limiter

    original, _ = _swap_limiter(rate_limiter, "vision_limiter", max_calls=1)
    try:
        resp1 = user_client.post("/photos/999999/detect")
        assert resp1.status_code != 429, f"Already limited: {resp1.text}"
        resp2 = user_client.post("/photos/999999/detect")
        assert (
            resp2.status_code == 429
        ), f"Expected 429 on 2nd request, got {resp2.status_code}: {resp2.text}"
    finally:
        rate_limiter.vision_limiter = original


def test_suggest_endpoint_returns_429_on_rate_limit(user_client, app_module):
    """GET /photos/{id}/suggest returns 429 when the vision limit is exceeded."""
    from app.services import rate_limiter

    original, _ = _swap_limiter(rate_limiter, "vision_limiter", max_calls=1)
    try:
        resp1 = user_client.get("/photos/999999/suggest")
        assert resp1.status_code != 429, f"Already limited: {resp1.text}"
        resp2 = user_client.get("/photos/999999/suggest")
        assert (
            resp2.status_code == 429
        ), f"Expected 429 on 2nd request, got {resp2.status_code}: {resp2.text}"
    finally:
        rate_limiter.vision_limiter = original


def test_upc_endpoint_returns_429_on_rate_limit(user_client, app_module, monkeypatch):
    """GET /upc/{upc} returns 429 when the upc limit is exceeded.

    Uses a role='user' client (no admin 4× multiplier).
    The external upcitemdb.com call is stubbed for fast, deterministic results.
    """
    import app.routes.upc as upc_route
    from app.services.upc_lookup import UPCResult

    def _stub_lookup(upc: str) -> UPCResult:
        return UPCResult(name=None, category=None, brand=None, source="unknown")

    monkeypatch.setattr(upc_route, "lookup_upc", _stub_lookup)

    from app.services import rate_limiter

    original, _ = _swap_limiter(rate_limiter, "upc_limiter", max_calls=1)
    try:
        resp1 = user_client.get("/upc/012345678905")
        assert resp1.status_code != 429, f"Already limited: {resp1.text}"
        resp2 = user_client.get("/upc/012345678905")
        assert (
            resp2.status_code == 429
        ), f"Expected 429 on 2nd request, got {resp2.status_code}: {resp2.text}"
    finally:
        rate_limiter.upc_limiter = original


# ── RATE_LIMIT_DISABLED kill-switch (dev-only bypass) ────────────────────────


def test_disabled_flag_short_circuits_check(monkeypatch):
    """When ``_DISABLED`` is True, ``check`` returns True past the configured
    budget and does NOT record a call (no deque growth)."""
    from app.services import rate_limiter as rl_mod
    from app.services.rate_limiter import SlidingWindowRateLimiter

    monkeypatch.setattr(rl_mod, "_DISABLED", True)

    rl = SlidingWindowRateLimiter(max_calls=1, period=60.0)
    for _ in range(50):
        assert rl.check("any-key") is True

    # Internal deque was never written — proves the bypass short-circuits
    # before the lock/deque path, so re-enabling won't surface stale state.
    assert "any-key" not in rl._calls


def test_disabled_flag_default_false_preserves_blocking():
    """Sanity: with the default (unset) env var, the limiter still blocks."""
    from app.services import rate_limiter as rl_mod
    from app.services.rate_limiter import SlidingWindowRateLimiter

    assert rl_mod._DISABLED is False
    rl = SlidingWindowRateLimiter(max_calls=1, period=60.0)
    assert rl.check("k") is True
    assert rl.check("k") is False


def test_disabled_flag_env_var_truthy_values(monkeypatch):
    """Accept the same truthy spellings the docstring promises."""
    import importlib

    from app.services import rate_limiter as rl_mod

    for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("RATE_LIMIT_DISABLED", value)
        importlib.reload(rl_mod)
        assert rl_mod._DISABLED is True, f"Expected True for {value!r}"

    for value in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("RATE_LIMIT_DISABLED", value)
        importlib.reload(rl_mod)
        assert rl_mod._DISABLED is False, f"Expected False for {value!r}"

    monkeypatch.delenv("RATE_LIMIT_DISABLED", raising=False)
    importlib.reload(rl_mod)
    assert rl_mod._DISABLED is False


def test_disabled_flag_bypasses_global_middleware(user_client, app_module, monkeypatch):
    """End-to-end: with ``_DISABLED`` True, the global middleware lets calls
    through past the configured budget."""
    from app.services import rate_limiter

    monkeypatch.setattr(rate_limiter, "_DISABLED", True)
    original, _ = _swap_limiter(rate_limiter, "global_limiter", max_calls=1)
    try:
        # Three requests against a budget of 1 — all should pass with bypass on.
        for i in range(3):
            resp = user_client.get("/health")
            assert (
                resp.status_code != 429
            ), f"Request {i + 1} unexpectedly limited despite _DISABLED=True: {resp.text}"
    finally:
        rate_limiter.global_limiter = original
