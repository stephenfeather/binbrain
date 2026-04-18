"""Tiered per-key sliding-window rate limiter and FastAPI dependencies (F-08).

Architecture
------------
- Four named limiters at different budgets (see module-level instances below).
- Each limiter is per API-key-id, in-memory, thread-safe.
- Admin-role keys receive a configurable multiplier on the base limit.
- Memory bounded by a FIFO key-eviction cap (_MAX_TRACKED_KEYS).

v1 Note: in-process state is lost on restart.  This is acceptable for the
current single-process local deployment.  A Redis-backed implementation is the
planned follow-up for multi-process / multi-instance deployments.

Environment variables
---------------------
GLOBAL_RATE_LIMIT          int  per-key req/min for all authenticated routes  (default 120)
VISION_RATE_LIMIT          int  per-key req/min for /photos/{id}/suggest|detect (default 10)
WARMUP_RATE_LIMIT          int  per-key req/min for model warmup / admin model routes (default 3)
UPC_RATE_LIMIT             int  per-key req/min for /upc/{upc}                (default 30)
RATE_LIMIT_ADMIN_MULTIPLIER  float multiplier applied to each limit for admin keys (default 4)
RATE_LIMIT_MAX_TRACKED_KEYS  int  maximum number of API-key slots held in memory (default 10000)
RATE_LIMIT_DISABLED        bool  dev-only kill-switch ("1"/"true"/"yes" => bypass every limiter)
                                 (default unset). DO NOT enable in production — this short-circuits
                                 all four limiters (global + vision + warmup + UPC) regardless of
                                 their configured budgets, removing the only in-process backpressure
                                 against runaway clients and outbound-vision/UPC cost amplification.
"""

import os
from collections import deque
from collections.abc import Callable
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request

_MAX_TRACKED_KEYS: int = int(os.environ.get("RATE_LIMIT_MAX_TRACKED_KEYS", "10000"))
_ADMIN_MULTIPLIER: float = float(os.environ.get("RATE_LIMIT_ADMIN_MULTIPLIER", "4"))
_DISABLED: bool = os.environ.get("RATE_LIMIT_DISABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
"""Read once at import time so the kill-switch state is stable for the process lifetime
(matches the deliberate immutability of the other module-level config). Tests that need
to toggle this should monkeypatch ``rate_limiter._DISABLED`` directly — module-global
function lookups (see the IMPORTANT comment below the limiters) make the swap visible
without re-importing."""


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window rate limiter.

    Args:
        max_calls:  Maximum calls allowed within *period* for a non-admin key.
        period:     Window length in seconds (default 60).
        time_fn:    Callable returning current time as a float (default monotonic).
                    Inject a fake clock in tests to avoid real sleeps.
    """

    def __init__(
        self,
        max_calls: int,
        period: float = 60.0,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._max_calls = max_calls
        self._period = period
        self._time_fn = time_fn or monotonic
        self._calls: dict[str, deque] = {}
        self._lock = Lock()

    def check(self, key: str, role_multiplier: float = 1.0) -> bool:
        """Record a call for *key* and return True if within the limit.

        Returns False (without recording) when the caller is over their quota.
        The effective limit is ``int(max_calls * role_multiplier)``.

        When the module-level ``RATE_LIMIT_DISABLED`` env var is truthy this
        short-circuits to True without touching the deque, so rate-limit state
        does not accumulate during dev/test runs that bypass the limiter. The
        lookup is via the module global (not a closed-over copy) so tests can
        monkeypatch ``rate_limiter._DISABLED`` without re-importing.
        """
        # Read through the module global — this is the IMPORTANT note's pattern.
        from app.services import rate_limiter as _rl

        if _rl._DISABLED:
            return True

        now = self._time_fn()
        cutoff = now - self._period
        effective_limit = max(1, int(self._max_calls * role_multiplier))

        with self._lock:
            if key not in self._calls:
                # FIFO cap: evict the oldest-inserted key when at the limit.
                if len(self._calls) >= _MAX_TRACKED_KEYS:
                    oldest = next(iter(self._calls))
                    del self._calls[oldest]
                self._calls[key] = deque()

            dq = self._calls[key]
            # Evict expired timestamps (sliding window).
            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= effective_limit:
                return False

            dq.append(now)

            # Evict the deque itself if all calls expired (memory efficiency).
            if not dq:  # only after popleft loop; won't happen here but defensive
                del self._calls[key]

            return True

    def reset(self, key: str | None = None) -> None:
        """Clear rate-limit state for *key*, or for all keys if key is None.

        Intended for testing; not used in production paths.
        """
        with self._lock:
            if key is None:
                self._calls.clear()
            else:
                self._calls.pop(key, None)


# ── Module-level limiter instances ────────────────────────────────────────────

global_limiter = SlidingWindowRateLimiter(
    max_calls=int(os.environ.get("GLOBAL_RATE_LIMIT", "120")),
)
"""120 req/min per key; applied as middleware to all authenticated endpoints."""

vision_limiter = SlidingWindowRateLimiter(
    max_calls=int(os.environ.get("VISION_RATE_LIMIT", "10")),
)
"""10 req/min per key; applies to /photos/{id}/suggest and /photos/{id}/detect."""

warmup_limiter = SlidingWindowRateLimiter(
    max_calls=int(os.environ.get("WARMUP_RATE_LIMIT", "3")),
)
"""3 req/min per key; applies to model-warmup and admin model-selection routes."""

upc_limiter = SlidingWindowRateLimiter(
    max_calls=int(os.environ.get("UPC_RATE_LIMIT", "30")),
)
"""30 req/min per key; applies to /upc/{upc} (outbound HTTP lookup)."""


# ── FastAPI dependency functions ──────────────────────────────────────────────
#
# IMPORTANT: these must be plain module-level functions (not closures from a
# factory).  A factory like _make_dep(vision_limiter) captures the *object* at
# import time; replacing `rate_limiter.vision_limiter` in tests then has no
# effect on the closed-over reference.  Module-global functions look up the
# name in this module's __dict__ at *call time*, so attribute-swaps in tests
# (e.g. rate_limiter.vision_limiter = stub) are always visible.


def _limiter_key(request: Request) -> tuple[str, float]:
    """Return (key, role_multiplier) for the current request."""
    key = str(getattr(request.state, "api_key_id", "anon"))
    role = getattr(request.state, "api_key_role", "user")
    multiplier = _ADMIN_MULTIPLIER if role == "admin" else 1.0
    return key, multiplier


def require_vision_rate_limit(request: Request) -> None:
    """Rate-limit dependency for /photos/{id}/suggest and /photos/{id}/detect."""
    key, mult = _limiter_key(request)
    if not vision_limiter.check(key, role_multiplier=mult):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


def require_warmup_rate_limit(request: Request) -> None:
    """Rate-limit dependency for model-warmup and admin model routes."""
    key, mult = _limiter_key(request)
    if not warmup_limiter.check(key, role_multiplier=mult):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


def require_upc_rate_limit(request: Request) -> None:
    """Rate-limit dependency for /upc/{upc} (outbound HTTP lookup)."""
    key, mult = _limiter_key(request)
    if not upc_limiter.check(key, role_multiplier=mult):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
