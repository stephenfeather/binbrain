"""FF-01: ASGI middleware enforcing a cumulative request-body byte cap.

The previous F-04 middleware inspected only ``Content-Length``. Chunked
transfers and clients that omit or spoof ``Content-Length`` bypassed the cap
and reached the handler, where oversize bodies consumed memory/disk/CPU.

This middleware enforces ``MAX_REQUEST_BODY_BYTES`` (default 50 MiB, override
via env var) by:

1. Fast-rejecting when a truthful ``Content-Length`` header already exceeds
   the cap — no body is ever read.
2. Wrapping the ASGI ``receive`` channel so that cumulative ``http.request``
   bytes are counted as they arrive. As soon as the running total exceeds
   the cap the middleware drains the remaining stream, emits a ``413``
   JSON response, and short-circuits the request before the downstream app
   handler runs.

Because the downstream handler is never invoked on oversize requests, no
multipart temp files are created under ``PHOTO_DIR`` for rejected uploads —
the F-05 temp-file flow is not reached.

The env var ``MAX_REQUEST_BODY_BYTES`` is documented in ``app.config``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

_Send = Callable[[dict], Awaitable[None]]
_Receive = Callable[[], Awaitable[dict]]
_RoleResolver = Callable[[str], str | None]

logger = logging.getLogger("binbrain")


def _client_ip(scope: dict) -> str:
    client = scope.get("client")
    return client[0] if isinstance(client, list | tuple) and client else "unknown"


class BodySizeLimitMiddleware:
    """Pure ASGI middleware that enforces a cumulative body-size cap.

    Installed as the outermost HTTP middleware so that oversize bodies are
    rejected before any downstream middleware (auth, rate limit) or handler
    starts consuming them.

    Admin-role API keys are granted a higher cap via ``admin_max_bytes``.
    A ``role_resolver`` callable maps a raw ``X-API-Key`` header value to the
    key's role ("admin" / "user" / None for unknown). The resolver is the
    single injection point so the middleware stays decoupled from the DB —
    callers wire a real resolver in production; unit tests pass a stub.
    """

    def __init__(
        self,
        app,
        max_bytes: int,
        admin_max_bytes: int | None = None,
        role_resolver: _RoleResolver | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)
        self.admin_max_bytes = (
            int(admin_max_bytes) if admin_max_bytes is not None else self.max_bytes
        )
        self.role_resolver = role_resolver

    def _effective_cap(self, scope: dict) -> int:
        """Resolve the body-size cap for this request.

        Returns ``admin_max_bytes`` when the X-API-Key header maps to an
        admin-role key; ``max_bytes`` otherwise. Failures in the resolver
        fall back to the user cap — a misconfigured resolver must never
        *raise* the cap for unauthenticated traffic.
        """
        if self.role_resolver is None or self.admin_max_bytes == self.max_bytes:
            return self.max_bytes
        raw_key: str | None = None
        for name, value in scope.get("headers", ()):
            if name == b"x-api-key":
                raw_key = value.decode("latin-1", errors="ignore")
                break
        if not raw_key:
            return self.max_bytes
        try:
            role = self.role_resolver(raw_key)
        except Exception:
            logger.exception("event=body_size_role_resolver_failed")
            return self.max_bytes
        return self.admin_max_bytes if role == "admin" else self.max_bytes

    async def __call__(self, scope: dict, receive: _Receive, send: _Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        effective_cap = self._effective_cap(scope)

        # Fast-reject when Content-Length is present and already over cap.
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > effective_cap:
                    logger.warning(
                        "event=request_too_large mode=content_length "
                        "declared=%d cap=%d path=%s ip=%s",
                        declared,
                        effective_cap,
                        scope.get("path", ""),
                        _client_ip(scope),
                    )
                    await self._send_413(send, effective_cap)
                    await _drain(receive)
                    return
                break

        # Streaming enforcement: count bytes as they arrive on the ASGI
        # receive channel. If cumulative bytes exceed the cap, drain the
        # remaining stream and short-circuit with 413.
        total = 0
        cap_exceeded = False
        buffered: list[dict] = []
        more_body = True
        while more_body:
            message = await receive()
            mtype = message.get("type")
            if mtype == "http.disconnect":
                # Client went away before we finished reading the body —
                # forward the disconnect and stop.
                buffered.append(message)
                break
            if mtype != "http.request":
                # Unknown message type: forward it and stop accumulating.
                buffered.append(message)
                break
            body = message.get("body", b"")
            total += len(body)
            if total > effective_cap:
                cap_exceeded = True
                # Drain any remaining body frames before responding so the
                # client's stream is fully consumed (prevents broken-pipe
                # races on the server side).
                if message.get("more_body", False):
                    await _drain(receive)
                break
            buffered.append(message)
            more_body = message.get("more_body", False)

        if cap_exceeded:
            logger.warning(
                "event=request_too_large mode=streaming "
                "observed=%d cap=%d path=%s ip=%s",
                total,
                effective_cap,
                scope.get("path", ""),
                _client_ip(scope),
            )
            await self._send_413(send, effective_cap)
            return

        # Under cap: replay the buffered body frames to the downstream app,
        # then delegate any further receive() calls to the original channel.
        queue = list(buffered)

        async def replay_receive() -> dict:
            if queue:
                return queue.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _send_413(self, send: _Send, effective_cap: int | None = None) -> None:
        cap = self.max_bytes if effective_cap is None else effective_cap
        body = json.dumps(
            {
                "version": "1",
                "error": {
                    "code": "payload_too_large",
                    "message": (f"Request body exceeds the {cap}-byte limit"),
                },
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )


async def _drain(receive: _Receive) -> None:
    """Pull remaining ``http.request`` frames until ``more_body`` is False."""
    while True:
        msg = await receive()
        mtype = msg.get("type")
        if mtype != "http.request":
            return
        if not msg.get("more_body", False):
            return
