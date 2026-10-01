"""Lightweight ASGI middleware for baseline HTTP hardening.

Implemented as pure ASGI (not BaseHTTPMiddleware) so they never buffer the body
and stay transparent to streaming responses (the file export) and background
tasks (RFQ send, estimator submit).
"""

from collections.abc import Awaitable, Callable

from starlette.exceptions import HTTPException
from starlette.routing import get_route_path

Scope = dict
Message = dict
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
# (method, route path) -> True when the route is a known multipart upload.
RouteMatcher = Callable[[str, str], bool]

_TOO_LARGE_DETAIL = "request_body_too_large"


def form_routes_matcher(app) -> RouteMatcher:
    """Build a RouteMatcher from the FastAPI app's own routes.

    A route qualifies when one of its body parameters is declared with
    ``Form``/``File`` (``UploadFile`` included): those are the only handlers
    that parse multipart, and Starlette's parser spools file parts to disk and
    caps every other part at 1 MB, so a large body there never sits in RAM.
    Every other route reads the whole body into memory before auth, so it
    must stay under the small cap whatever Content-Type the client declares.

    Routes are read lazily on first use and cached: ``add_middleware`` runs
    before ``include_router`` in main.py, and Starlette only builds the
    middleware stack on the first request, when every router is in place.
    """
    from fastapi import params as fastapi_params
    from fastapi.routing import APIRoute

    cache: list[tuple[frozenset[str], object]] | None = None

    def _load() -> list[tuple[frozenset[str], object]]:
        found = []
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            if any(isinstance(bp.field_info, fastapi_params.Form) for bp in route.dependant.body_params):
                found.append((frozenset(route.methods or ()), route.path_regex))
        return found

    def match(method: str, path: str) -> bool:
        nonlocal cache
        if cache is None:
            cache = _load()
        method = method.upper()
        return any(method in methods and regex.match(path) for methods, regex in cache)

    return match


class BodyTooLarge(HTTPException):
    """Raised from the wrapped ``receive`` once the bytes actually received pass
    the cap. It subclasses Starlette's HTTPException on purpose: FastAPI reads
    the body inside a ``try`` that turns any other exception into a generic
    400, but re-raises HTTPException, so the app's ExceptionMiddleware renders
    this as the same ``{"detail": "request_body_too_large"}`` 413 the
    Content-Length fast path sends (CORS headers included). The middleware also
    catches it itself for an app that has no exception layer.
    """

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=_TOO_LARGE_DETAIL)


class MaxBodySizeMiddleware:
    """Refuse request bodies over a cap, counting the bytes actually received.

    A global backstop so no endpoint can be handed an arbitrarily large body to
    buffer. FastAPI reads the WHOLE body (JSON routes included) before it
    resolves dependencies, so auth and the rate limiters run only after the
    body sits in memory: the cap is the only thing between an anonymous caller
    and a worker holding hundreds of MB. Two caps, chosen by ROUTE, not by a
    header the client controls:

    * a known multipart upload route (``upload_routes`` says which: method +
      path, see ``form_routes_matcher``) called with a ``multipart/form-data``
      Content-Type gets ``max_bytes`` (460 MB). The upload handler streams the
      file through its own per-file cap;
    * everything else gets ``max_json_bytes`` (16 MB), ``max_bytes`` when not
      given. That includes a JSON route sent a spoofed multipart Content-Type
      (FastAPI still buffers the whole body there) and an upload route sent a
      non-multipart body. With no ``upload_routes`` no route gets the large
      cap.

    The declared Content-Length is checked first (cheap, and refuses before a
    single body byte is read). ``receive`` is then wrapped so a chunked request
    with no Content-Length, or one that under-declares it, is refused with the
    same 413 the moment the running total passes the cap, and the app never
    receives the rest.
    """

    def __init__(
        self,
        app,
        max_bytes: int,
        max_json_bytes: int | None = None,
        upload_routes: RouteMatcher | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_json_bytes = max_bytes if max_json_bytes is None else max_json_bytes
        self.upload_routes = upload_routes

    @staticmethod
    def _is_multipart(scope: Scope) -> bool:
        for key, value in scope.get("headers", []):
            if key == b"content-type":
                return value.strip().lower().startswith(b"multipart/form-data")
        return False

    def _cap_for(self, scope: Scope) -> int:
        if self.upload_routes is None or not self._is_multipart(scope):
            return self.max_json_bytes
        if self.upload_routes(scope.get("method", ""), get_route_path(scope)):
            return self.max_bytes
        return self.max_json_bytes

    @staticmethod
    async def _send_413(send: Send) -> None:
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
                "body": b'{"detail":"' + _TOO_LARGE_DETAIL.encode() + b'"}',
            }
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cap = self._cap_for(scope)
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    too_big = int(value) > cap
                except ValueError:
                    too_big = False
                if too_big:
                    await self._send_413(send)
                    return
                break

        received = 0
        response_started = False

        async def receive_wrapper() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    raise BodyTooLarge()
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except BodyTooLarge:
            # Only reached when nothing below rendered it (a bare ASGI app, or
            # a body read outside FastAPI's handler). Answer if we still can.
            if not response_started:
                await self._send_413(send)


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
