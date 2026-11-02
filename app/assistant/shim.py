"""The ASGI shim: the only thing an assistant run talks to (docs/ASSISTANT.md,
"The shim is never the served app").

`make_shim(...)` returns an ASGI callable that closes over `app.main.app`
(imported lazily inside the call, so `app.main` never imports this package
and the import direction is one way), marks the scope `bdr_internal`, runs
the firewall, enters the provenance context and awaits the served app. It is
passed only to `httpx.ASGITransport(app=shim)`. It is never assigned to
`app.main.app`, never registered with add_middleware, and never served:
Dockerfile keeps `app.main:app`.

The firewall, in order: method DELETE is refused outright; the request is
matched against app.main.app.router (Route.matches) and the matched route's
(method, path_format) is refused when it is on firewall.DENY_LIST, whatever
the role; anything not in the registry allowlist is refused. Refusals are a
JSON 403 with code `assistant_firewall` and never reach the app, so no
dependency, no rate limiter and no handler runs for a refused call.

Identity is per run, not per process: a run acts for one user and one
run_id, so the callable is built per run by `make_shim` rather than being a
single module-level object shared by every executor.
"""

from __future__ import annotations

import json
from collections.abc import Collection, MutableMapping
from dataclasses import dataclass
from typing import Any

from app.assistant import firewall
from app.assistant.firewall import RoutePair
from app.assistant.principal import SCOPE_MARKER
from app.core import provenance
from app.core.deps import IMPERSONATE_HEADER

FIREWALL_CODE = "assistant_firewall"
# Request headers the shim strips before the app sees them: the principal
# never reads them, and stripping keeps a replayed browser header inert.
STRIPPED_HEADERS: frozenset[bytes] = frozenset({IMPERSONATE_HEADER.encode("ascii")})


def served_app() -> Any:
    """`app.main.app`, imported on first use. The boot assertion of the
    design record lives here: the served object must be the FastAPI app
    itself, never this shim or anything wrapped around it."""
    from fastapi import FastAPI

    from app import main as served

    application = served.app
    if not isinstance(application, FastAPI):
        raise RuntimeError("app.main.app must be the FastAPI instance, never a wrapper")
    return application


def _refusal_body(verdict: firewall.Verdict) -> bytes:
    body = {
        "detail": FIREWALL_CODE,
        "code": FIREWALL_CODE,
        "reason": verdict.reason,
        "method": verdict.method,
        "path_format": verdict.path_format,
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


async def _refuse(send: Any, verdict: firewall.Verdict) -> None:
    body = _refusal_body(verdict)
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-error-code", FIREWALL_CODE.encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


@dataclass(frozen=True)
class AssistantShim:
    """One run's ASGI entry point. Frozen: the identity cannot drift."""

    allowlist: frozenset[RoutePair]
    user_id: str
    run_id: str
    approval_id: str | None = None

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            raise RuntimeError("the assistant shim serves http requests only")
        request_scope = dict(scope)
        request_scope[SCOPE_MARKER] = True
        request_scope["headers"] = [
            (name, value) for name, value in request_scope.get("headers", [])
            if name.lower() not in STRIPPED_HEADERS
        ]
        application = served_app()
        verdict = firewall.check_scope(application, request_scope, self.allowlist)
        if not verdict.allowed:
            await _refuse(send, verdict)
            return
        with provenance.acting_as_assistant(self.user_id, self.run_id, self.approval_id):
            await application(request_scope, receive, send)


def make_shim(
    allowlist: Collection[RoutePair],
    *,
    user_id: str,
    run_id: str,
    approval_id: str | None = None,
) -> AssistantShim:
    """The ASGI callable for one run. `allowlist` is the registry's set of
    (method, path_format) pairs for the tools offered to this run."""
    if not user_id or not run_id:
        raise ValueError("make_shim needs a user_id and a run_id")
    return AssistantShim(
        allowlist=firewall.normalize_pairs(allowlist),
        user_id=str(user_id),
        run_id=str(run_id),
        approval_id=str(approval_id) if approval_id else None,
    )
