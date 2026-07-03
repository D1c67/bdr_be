"""Lightweight ASGI middleware for baseline HTTP hardening.

Implemented as pure ASGI (not BaseHTTPMiddleware) so they never buffer the body
and stay transparent to streaming responses (the file export) and background
tasks (RFQ send, estimator submit).
"""

from collections.abc import Awaitable, Callable

Scope = dict
Message = dict
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class MaxBodySizeMiddleware:
    """Reject any request whose declared Content-Length exceeds ``max_bytes``.

    A cheap, global backstop so no endpoint can be handed an arbitrarily large
    body to buffer. Honest clients (and virtually all HTTP tooling) send
    Content-Length; the streaming cap in the upload handler covers the case of a
    client that lies about or omits it on the one route that buffers a body.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for key, value in scope.get("headers", []):
                if key == b"content-length":
                    try:
                        too_big = int(value) > self.max_bytes
                    except ValueError:
                        too_big = False
                    if too_big:
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 413,
                                "headers": [(b"content-type", b"application/json")],
                            }
                        )
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b'{"detail":"request_body_too_large"}',
                            }
                        )
                        return
                    break
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Attach baseline security response headers to every HTTP response.

    Only sets a header if the app did not already set it, so per-response
    overrides (e.g. an export's Content-Disposition) are never clobbered.
    """

    def __init__(self, app, headers: dict[str, str]) -> None:
        self.app = app
        self._headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k for k, _ in headers}
                for k, v in self._headers:
                    if k not in present:
                        headers.append((k, v))
            await send(message)

        await self.app(scope, receive, send_wrapper)
