"""F-08 middleware-ordering tests — no HTTP needed, DB required for app import.

Tests cover:
- _rate_limit_key_for: api_key_id happy path, ip fallback, missing client
- _assert_auth_runs_before_rate_limit: correct order passes, reversed raises,
  missing middleware raises

app_module fixture is required because importing app.main triggers the full
SQLAlchemy / FastAPI import chain that conftest initialises via app_module.
"""
import types

import pytest


def _make_request(api_key_id=None, client_host=None, has_client=True):
    """Build a minimal Request-like stub for _rate_limit_key_for tests."""
    state = types.SimpleNamespace()
    if api_key_id is not None:
        state.api_key_id = api_key_id

    if has_client and client_host is not None:
        client = types.SimpleNamespace(host=client_host)
    elif not has_client:
        client = None
    else:
        client = types.SimpleNamespace(host="127.0.0.1")

    return types.SimpleNamespace(state=state, client=client)


# ── _rate_limit_key_for tests ─────────────────────────────────────────────────

def test_rate_limit_key_for_returns_api_key_id_when_set(app_module):
    from app.main import _rate_limit_key_for
    req = _make_request(api_key_id="key-xyz")
    assert _rate_limit_key_for(req) == "key-xyz"


def test_rate_limit_key_for_falls_back_to_ip_when_missing(app_module):
    from app.main import _rate_limit_key_for
    req = _make_request(client_host="1.2.3.4")
    assert _rate_limit_key_for(req) == "ip:1.2.3.4"


def test_rate_limit_key_for_handles_missing_client(app_module):
    from app.main import _rate_limit_key_for
    req = _make_request(has_client=False)
    assert _rate_limit_key_for(req) == "ip:unknown"


# ── _assert_auth_runs_before_rate_limit tests ─────────────────────────────────

def test_assert_middleware_order_passes_on_correct_order(app_module):
    """user_middleware index 0 = outermost = runs first; auth at index 0 means auth runs first."""
    from app.main import _assert_auth_runs_before_rate_limit
    # auth at lower index (outermost) → runs before rate_limit → correct
    names = ["api_key_auth_middleware", "global_rate_limit_middleware"]
    _assert_auth_runs_before_rate_limit(names)  # must not raise


def test_assert_middleware_order_raises_on_reversed(app_module):
    """rate_limit at lower index → runs before auth → invariant violated."""
    from app.main import _assert_auth_runs_before_rate_limit
    names = ["global_rate_limit_middleware", "api_key_auth_middleware"]
    with pytest.raises(RuntimeError, match="ordering invariant"):
        _assert_auth_runs_before_rate_limit(names)


def test_assert_middleware_order_raises_on_missing_middleware(app_module):
    """Only one of the two middlewares present → RuntimeError."""
    from app.main import _assert_auth_runs_before_rate_limit
    with pytest.raises(RuntimeError):
        _assert_auth_runs_before_rate_limit(["global_rate_limit_middleware"])
    with pytest.raises(RuntimeError):
        _assert_auth_runs_before_rate_limit(["api_key_auth_middleware"])
