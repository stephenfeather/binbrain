"""F-08 (Medium): No rate limiting — RED tests.

Tests FAIL until:
- app/services/rate_limiter.py exists with SlidingWindowRateLimiter
- Expensive endpoints (/photos/{id}/detect, /photos/{id}/suggest, /upc/{upc})
  apply require_expensive_rate_limit dependency
- Rate limit exceeded returns 429
"""


# ── Unit tests: RateLimiter class (no DB needed) ─────────────────────────────

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
    assert rl.check("key1") is False   # key1 exhausted
    assert rl.check("key2") is True    # key2 unaffected


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


def test_rate_limiter_window_expires():
    """Calls outside the time window are not counted."""
    import time
    from app.services.rate_limiter import SlidingWindowRateLimiter
    rl = SlidingWindowRateLimiter(max_calls=1, period=0.05)  # 50ms window
    assert rl.check("key1") is True
    assert rl.check("key1") is False   # within window — blocked
    time.sleep(0.1)                    # wait for window to expire
    assert rl.check("key1") is True    # old call expired — allowed again


def test_module_level_expensive_limiter_exists():
    from app.services.rate_limiter import expensive_limiter, SlidingWindowRateLimiter
    assert isinstance(expensive_limiter, SlidingWindowRateLimiter)


# ── Integration tests: endpoints return 429 when rate limit hit ───────────────

def test_detect_endpoint_returns_429_on_rate_limit(client, app_module):
    """POST /photos/{id}/detect must return 429 when the rate limit is exceeded."""
    from app.services import rate_limiter
    original = rate_limiter.expensive_limiter
    rate_limiter.expensive_limiter = rate_limiter.SlidingWindowRateLimiter(max_calls=1, period=60.0)
    try:
        # First request goes through (may be 404 — that's fine, not 429)
        resp1 = client.post("/photos/999999/detect")
        assert resp1.status_code != 429, (
            f"Rate limit already exceeded before test started: {resp1.text}"
        )
        # Second request must be rate-limited
        resp2 = client.post("/photos/999999/detect")
        assert resp2.status_code == 429, (
            f"Expected 429 on 2nd request, got {resp2.status_code}: {resp2.text}"
        )
    finally:
        rate_limiter.expensive_limiter = original


def test_suggest_endpoint_returns_429_on_rate_limit(client, app_module):
    """GET /photos/{id}/suggest must return 429 when the rate limit is exceeded."""
    from app.services import rate_limiter
    original = rate_limiter.expensive_limiter
    rate_limiter.expensive_limiter = rate_limiter.SlidingWindowRateLimiter(max_calls=1, period=60.0)
    try:
        resp1 = client.get("/photos/999999/suggest")
        assert resp1.status_code != 429, f"Already limited: {resp1.text}"
        resp2 = client.get("/photos/999999/suggest")
        assert resp2.status_code == 429, (
            f"Expected 429 on 2nd request, got {resp2.status_code}: {resp2.text}"
        )
    finally:
        rate_limiter.expensive_limiter = original


def test_upc_endpoint_returns_429_on_rate_limit(client, app_module):
    """GET /upc/{upc} must return 429 when the rate limit is exceeded."""
    from app.services import rate_limiter
    original = rate_limiter.expensive_limiter
    rate_limiter.expensive_limiter = rate_limiter.SlidingWindowRateLimiter(max_calls=1, period=60.0)
    try:
        resp1 = client.get("/upc/012345678905")  # valid UPC-12 check digit
        assert resp1.status_code != 429, f"Already limited: {resp1.text}"
        resp2 = client.get("/upc/012345678905")
        assert resp2.status_code == 429, (
            f"Expected 429 on 2nd request, got {resp2.status_code}: {resp2.text}"
        )
    finally:
        rate_limiter.expensive_limiter = original
