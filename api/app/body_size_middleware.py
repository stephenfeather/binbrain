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
from collections.abc import Awaitable, Callable

_Send = Callable[[dict], Awaitable[None]]
_Receive = Callable[[], Awaitable[dict]]


class BodySizeLimitMiddleware:
    """Pure ASGI middleware that enforces a cumulative body-size cap.

    Installed as the outermost HTTP middleware so that oversize bodies are
    rejected before any downstream middleware (auth, rate limit) or handler
    starts consuming them.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope: dict, receive: _Receive, send: _Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Fast-reject when Content-Length is present and already over cap.
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > self.max_bytes:
                    await self._send_413(send)
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
            if total > self.max_bytes:
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
            await self._send_413(send)
            return

        # Under cap: replay the buffered body frames to the downstream app,
        # then delegate any further receive() calls to the original channel.
        queue = list(buffered)

        async def replay_receive() -> dict:
            if queue:
                return queue.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _send_413(self, send: _Send) -> None:
        body = json.dumps(
            {
                "version": "1",
                "error": {
                    "code": "payload_too_large",
                    "message": (f"Request body exceeds the {self.max_bytes}-byte limit"),
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
