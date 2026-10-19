"""POST /agent-worker/v1/jobs/{job_id}/turns: the model proxy (SPEC.md 1, 3.2).

The worker posts what its last tool observed; BDR answers with the next
tool call (200), "another BDR process is thinking on this turn, ask again"
(202) or a stop (200, stop=true). All the work is in
services/browser_agents/turns.py; this module is the HTTP edge only, with the
same rules as routers/agent_worker.py: plain `def`, a bare 404 while
BROWSER_AGENTS_ENABLED is off, the worker's own token and lease headers,
Cache-Control: no-store, and X-Error-Code on every refusal.

The caller's own bearer token is passed down only so the scrub can redact
it if it ever shows up in an observation; it is never stored or logged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from app.core.agent_auth import AgentLease, require_agent_lease
from app.core.features import require_browser_agents
from app.routers.agent_worker import _no_store, _parse_body
from app.services.browser_agents import protocol, turns

router = APIRouter(
    prefix=protocol.API_PREFIX,
    tags=["agent-worker"],
    dependencies=[Depends(require_browser_agents), Depends(_no_store)],
)


def _bearer_token(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        token = value.split(" ", 1)[1].strip()
        return token if protocol.WORKER_TOKEN_RE.match(token) else None
    return None


@router.post("/jobs/{job_id}/turns", response_model=None)
def post_turn(
    request: Request,
    payload: Any = Body(default=None),
    lease: AgentLease = Depends(require_agent_lease),
) -> JSONResponse:
    """One model turn: fence, idempotency (turn_no = prev_turn_no + 1),
    scrub, caps, context, provider call, record, validate. 200 with
    {turn_no, assistant_text, action, caps}, 202 {retry_after}, or 200
    {stop: true, reason, retry_at?, detail?}."""
    body = _parse_body(protocol.TurnRequest, payload)
    reply = turns.handle_turn(lease, body, worker_token=_bearer_token(request), api_host=request.url.hostname)
    return JSONResponse(reply.body, status_code=reply.status_code, headers={"Cache-Control": "no-store"})
