"""FF-01 (High): Request-body cap bypassable when Content-Length is absent
or untrusted — RED tests.

The existing F-04 middleware inspects only ``request.headers['content-length']``.
Chunked transfers or spoofed small Content-Length headers bypass the cap and
reach the handler, where oversize bodies consume memory/disk/CPU.

Required behaviour under test:

1. Fast path: honest ``Content-Length`` over cap → 413 before streaming.
2. Streaming path: cumulative ASGI ``http.request`` bytes > cap → 413 and
   short-circuit the request before the app handler runs.
3. Spoofed small ``Content-Length`` + oversize actual body → 413 (streaming).
4. Under-cap bodies pass through untouched (byte-for-byte).

These tests exercise the middleware directly as an ASGI callable via
``asyncio.run`` so no async pytest plugin is required. They will FAIL until
``app.body_size_middleware.BodySizeLimitMiddleware`` is implemented.
"""

from __future__ import annotations

import asyncio
import json

# ── Tiny ASGI helpers ────────────────────────────────────────────────────────


async def _noop_app(scope, receive, send):
    """Consume the full body, then return 200 with total byte count."""
    total = 0
    more = True
    while more:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        total += len(msg.get("body", b""))
        more = msg.get("more_body", False)
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": f"ok:{total}".encode(),
            "more_body": False,
        }
    )


def _http_scope(headers):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/ingest",
        "raw_path": b"/ingest",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }


def _receive_from(chunks):
    """Build an ASGI ``receive`` callable that yields each chunk as an
    ``http.request`` message, with ``more_body`` set correctly."""
    queue = list(chunks)

    async def receive():
        if not queue:
            return {"type": "http.disconnect"}
        body = queue.pop(0)
        more_body = len(queue) > 0
        return {"type": "http.request", "body": body, "more_body": more_body}

    return receive


class _SendCollector:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body_bytes(self):
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )


def _run(coro):
    return asyncio.run(coro)


# ── Fast-path: honest Content-Length over cap ────────────────────────────────


def test_honest_content_length_over_cap_returns_413():
    from app.body_size_middleware import BodySizeLimitMiddleware

    cap = 100
    mw = BodySizeLimitMiddleware(_noop_app, max_bytes=cap)
    headers = [(b"content-length", str(cap + 1).encode())]
    scope = _http_scope(headers)
    send = _SendCollector()

    _run(mw(scope, _receive_from([b""]), send))

    assert send.status == 413, f"expected 413, got {send.status}"
    payload = json.loads(send.body_bytes)
    assert payload["error"]["code"] == "payload_too_large"


# ── Streaming path: no Content-Length, oversize body ─────────────────────────


def test_streaming_body_over_cap_no_content_length_returns_413():
    from app.body_size_middleware import BodySizeLimitMiddleware

    cap = 100
    mw = BodySizeLimitMiddleware(_noop_app, max_bytes=cap)
    headers = [(b"transfer-encoding", b"chunked")]
    scope = _http_scope(headers)

    chunks = [b"x" * 40 for _ in range(4)]  # 160 bytes total
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 413, f"expected 413, got {send.status}"
    payload = json.loads(send.body_bytes)
    assert payload["error"]["code"] == "payload_too_large"


# ── Streaming path: spoofed small Content-Length ─────────────────────────────


def test_spoofed_small_content_length_over_cap_returns_413():
    from app.body_size_middleware import BodySizeLimitMiddleware

    cap = 100
    mw = BodySizeLimitMiddleware(_noop_app, max_bytes=cap)
    headers = [(b"content-length", b"10")]
    scope = _http_scope(headers)
    chunks = [b"y" * 200]
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 413, f"expected 413, got {send.status}"


# ── Happy path: under-cap body is replayed byte-for-byte ─────────────────────


def test_under_cap_body_is_passed_through():
    from app.body_size_middleware import BodySizeLimitMiddleware

    cap = 1000
    mw = BodySizeLimitMiddleware(_noop_app, max_bytes=cap)
    headers = [(b"content-length", b"300")]
    scope = _http_scope(headers)
    chunks = [b"a" * 100, b"b" * 100, b"c" * 100]
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 200, f"expected 200, got {send.status}"
    assert send.body_bytes == b"ok:300", f"body not replayed correctly: {send.body_bytes!r}"


# ── Non-http scope is passed straight through ────────────────────────────────


def test_non_http_scope_is_passed_through():
    from app.body_size_middleware import BodySizeLimitMiddleware

    called = {"n": 0}

    async def lifespan_app(scope, receive, send):
        called["n"] += 1

    mw = BodySizeLimitMiddleware(lifespan_app, max_bytes=100)

    async def recv():
        return {"type": "lifespan.startup"}

    async def send(msg):
        pass

    _run(mw({"type": "lifespan"}, recv, send))
    assert called["n"] == 1


# ── Admin role bypass: higher cap for admin X-API-Key ────────────────────────


def test_admin_key_gets_admin_cap_for_body_between_user_and_admin_caps():
    """Body between user cap and admin cap must pass for admin keys."""
    from app.body_size_middleware import BodySizeLimitMiddleware

    user_cap = 100
    admin_cap = 1000

    def resolver(raw_key: str) -> str | None:
        return "admin" if raw_key == "bb_admin_key" else "user"

    mw = BodySizeLimitMiddleware(
        _noop_app,
        max_bytes=user_cap,
        admin_max_bytes=admin_cap,
        role_resolver=resolver,
    )
    headers = [
        (b"content-length", b"500"),
        (b"x-api-key", b"bb_admin_key"),
    ]
    scope = _http_scope(headers)
    chunks = [b"z" * 500]
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 200, f"expected 200, got {send.status}"
    assert send.body_bytes == b"ok:500"


def test_user_key_rejected_when_body_exceeds_user_cap():
    """Non-admin keys still get the tight user cap."""
    from app.body_size_middleware import BodySizeLimitMiddleware

    user_cap = 100
    admin_cap = 1000

    def resolver(raw_key: str) -> str | None:
        return "admin" if raw_key == "bb_admin_key" else "user"

    mw = BodySizeLimitMiddleware(
        _noop_app,
        max_bytes=user_cap,
        admin_max_bytes=admin_cap,
        role_resolver=resolver,
    )
    headers = [
        (b"content-length", b"500"),
        (b"x-api-key", b"bb_user_key"),
    ]
    scope = _http_scope(headers)
    chunks = [b"z" * 500]
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 413, f"expected 413, got {send.status}"


def test_unknown_key_falls_back_to_user_cap():
    """Resolver returning None (unknown key) must NOT raise the cap."""
    from app.body_size_middleware import BodySizeLimitMiddleware

    user_cap = 100
    admin_cap = 1000

    def resolver(raw_key: str) -> str | None:
        return None

    mw = BodySizeLimitMiddleware(
        _noop_app,
        max_bytes=user_cap,
        admin_max_bytes=admin_cap,
        role_resolver=resolver,
    )
    headers = [
        (b"content-length", b"500"),
        (b"x-api-key", b"bb_whoever"),
    ]
    scope = _http_scope(headers)
    chunks = [b"z" * 500]
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 413, f"expected 413, got {send.status}"


def test_resolver_exception_falls_back_to_user_cap():
    """A raising resolver must not leak capacity — fall back to user cap."""
    from app.body_size_middleware import BodySizeLimitMiddleware

    user_cap = 100
    admin_cap = 1000

    def resolver(raw_key: str) -> str | None:
        raise RuntimeError("db down")

    mw = BodySizeLimitMiddleware(
        _noop_app,
        max_bytes=user_cap,
        admin_max_bytes=admin_cap,
        role_resolver=resolver,
    )
    headers = [
        (b"content-length", b"500"),
        (b"x-api-key", b"bb_admin_key"),
    ]
    scope = _http_scope(headers)
    chunks = [b"z" * 500]
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 413, f"expected 413, got {send.status}"


def test_no_api_key_header_uses_user_cap():
    """Without X-API-Key, resolver is not consulted and user cap applies."""
    from app.body_size_middleware import BodySizeLimitMiddleware

    user_cap = 100
    admin_cap = 1000
    resolver_calls = {"n": 0}

    def resolver(raw_key: str) -> str | None:
        resolver_calls["n"] += 1
        return "admin"

    mw = BodySizeLimitMiddleware(
        _noop_app,
        max_bytes=user_cap,
        admin_max_bytes=admin_cap,
        role_resolver=resolver,
    )
    headers = [(b"content-length", b"500")]
    scope = _http_scope(headers)
    chunks = [b"z" * 500]
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 413, f"expected 413, got {send.status}"
    assert resolver_calls["n"] == 0, "resolver called without X-API-Key header"


def test_admin_streaming_body_over_user_cap_under_admin_cap_passes():
    """Streaming oversize-vs-user / under-admin body passes for admin."""
    from app.body_size_middleware import BodySizeLimitMiddleware

    user_cap = 100
    admin_cap = 1000

    def resolver(raw_key: str) -> str | None:
        return "admin"

    mw = BodySizeLimitMiddleware(
        _noop_app,
        max_bytes=user_cap,
        admin_max_bytes=admin_cap,
        role_resolver=resolver,
    )
    headers = [
        (b"transfer-encoding", b"chunked"),
        (b"x-api-key", b"bb_admin_key"),
    ]
    scope = _http_scope(headers)
    chunks = [b"q" * 200, b"q" * 200, b"q" * 200]
    send = _SendCollector()

    _run(mw(scope, _receive_from(chunks), send))

    assert send.status == 200, f"expected 200, got {send.status}"
    assert send.body_bytes == b"ok:600"
