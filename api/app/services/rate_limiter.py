"""Sliding-window per-key rate limiter and FastAPI dependency (F-08).

Thread-safe, in-memory only.  State is lost on restart, which is acceptable
for a local/small-team deployment where DoS mitigation is best-effort.

Usage
-----
from app.services.rate_limiter import expensive_limiter

# In a FastAPI dependency:
def require_expensive_rate_limit(request: Request):
    key = str(getattr(request.state, "api_key_id", "anon"))
    if not expensive_limiter.check(key):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
"""
import os
from collections import deque
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request


class SlidingWindowRateLimiter:
    """Count requests per key inside a sliding time window.

    Args:
        max_calls: Maximum number of calls allowed within *period*.
        period:    Window length in seconds.
    """

    def __init__(self, max_calls: int, period: float = 60.0) -> None:
        self._max_calls = max_calls
        self._period = period
        self._calls: dict[str, deque] = {}
        self._lock = Lock()

    def check(self, key: str) -> bool:
        """Return True and record the call if the key is within its limit.

        Returns False (without recording) when the limit is already reached.
        """
        now = monotonic()
        cutoff = now - self._period
        with self._lock:
            if key not in self._calls:
                self._calls[key] = deque()
            dq = self._calls[key]
            # Evict timestamps that have fallen outside the window.
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self._max_calls:
                return False
            dq.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        """Clear rate-limit state for *key*, or for all keys if key is None."""
        with self._lock:
            if key is None:
                self._calls.clear()
            else:
                self._calls.pop(key, None)


# ── Module-level instances ────────────────────────────────────────────────────

# Expensive endpoints: model inference, detection, vision, external UPC lookup.
# Default: 60 calls per minute per API key.  Override via EXPENSIVE_RATE_LIMIT.
expensive_limiter = SlidingWindowRateLimiter(
    max_calls=int(os.environ.get("EXPENSIVE_RATE_LIMIT", "60")),
    period=60.0,
)


def require_expensive_rate_limit(request: Request) -> None:
    """FastAPI dependency: enforce per-key rate limit on expensive endpoints.

    Raises HTTPException(429) when the calling API key has exceeded its quota.
    Apply via ``dependencies=[Depends(require_expensive_rate_limit)]``.
    """
    key = str(getattr(request.state, "api_key_id", "anon"))
    if not expensive_limiter.check(key):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
