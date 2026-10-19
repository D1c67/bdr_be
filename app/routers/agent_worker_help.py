"""Worker API, help half (docs/browser_agents/SPEC.md 3.2 and 6, O5):

  POST /agent-worker/v1/jobs/{job_id}/help
  GET  /agent-worker/v1/jobs/{job_id}/help/{help_id}/messages?after=
  POST /agent-worker/v1/jobs/{job_id}/help/{help_id}/consume
  POST /agent-worker/v1/jobs/{job_id}/help/{help_id}/messages
  POST /agent-worker/v1/jobs/{job_id}/help/{help_id}/secrets/{message_id}/use
  POST /agent-worker/v1/jobs/{job_id}/contributions

/jobs/{job_id}/live stays unrouted (404): the reserved live-view seam.

The work is in services/browser_agents/help.py; this module is the HTTP edge
with the same rules as routers/agent_worker.py: plain `def`, a bare 404
while BROWSER_AGENTS_ENABLED is off, the worker's token and lease headers,
Cache-Control: no-store on everything, X-Error-Code on every refusal. Every
help_id must be one the job waits on, every message id must belong to that
help request, every artifact to this job or its help (IDOR).

secrets/{message_id}/use is the one response that carries a helper secret
(an MFA code). It is serialized with `wire_json()` (pydantic masks
SecretStr everywhere else), sent no-store, and never logged here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Query, status
from fastapi.responses import Response

from app.core.agent_auth import AgentLease, require_agent_lease
from app.core.features import require_browser_agents
from app.routers.agent_worker import _no_store, _parse_body
from app.services.browser_agents import help as help_flow
from app.services.browser_agents import protocol

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix=protocol.API_PREFIX,
    tags=["agent-worker"],
    dependencies=[Depends(require_browser_agents), Depends(_no_store)],
)

# The help sweeper steps (reminders, claim lapse, resolve, expiry, secret
# purge, answered-while-parked).
help_flow.register_sweep_steps()


def _json(model: protocol._Resp, *, reveal: bool = False) -> Response:
    content = model.wire_json() if reveal else model.model_dump_json()
    return Response(content=content, media_type="application/json", headers={"Cache-Control": "no-store"})


def _help_id(value: uuid.UUID) -> str:
    return str(value)


@router.post("/jobs/{job_id}/help", response_model=None)
def open_help(payload: Any = Body(default=None), lease: AgentLease = Depends(require_agent_lease)) -> Response:
    """Open (or join) the help request for the reason's scope. BDR derives
    the scope and the title; the agent's message is kept as untrusted text.
    suppressed=true: a link scope was given up or skipped in the last 24 h,
    the worker finishes failed access_denied_suppressed."""
    body = _parse_body(protocol.HelpOpenRequest, payload)
    return _json(help_flow.open_help(lease, body))


@router.get("/jobs/{job_id}/help/{help_id}/messages", response_model=None)
def help_messages(
    help_id: uuid.UUID = Path(...),
    after: int = Query(default=0, ge=0),
    lease: AgentLease = Depends(require_agent_lease),
) -> Response:
    """Helper and system messages after `after`. A secret message comes as a
    secret_ref only; its value is fetched once through secrets/{id}/use."""
    return _json(help_flow.worker_messages(lease, _help_id(help_id), after))


@router.post("/jobs/{job_id}/help/{help_id}/consume", status_code=status.HTTP_204_NO_CONTENT)
def help_consume(
    help_id: uuid.UUID = Path(...),
    payload: Any = Body(default=None),
    lease: AgentLease = Depends(require_agent_lease),
) -> None:
    """The job read these messages (help_reads). A job holding the page for
    this request goes back to running on the same lease."""
    body = _parse_body(protocol.HelpConsumeRequest, payload)
    help_flow.consume(lease, _help_id(help_id), body.message_ids)


@router.post("/jobs/{job_id}/help/{help_id}/messages", response_model=None)
def help_reply(
    help_id: uuid.UUID = Path(...),
    payload: Any = Body(default=None),
    lease: AgentLease = Depends(require_agent_lease),
) -> Response:
    """The agent's own reply in the thread (reply_help). Scrubbed of URL
    credentials and known secrets; shown to helpers as untrusted text."""
    body = _parse_body(protocol.HelpReplyRequest, payload)
    return _json(protocol.HelpReplyResponse(id=help_flow.agent_reply(lease, _help_id(help_id), body.body)))


@router.post("/jobs/{job_id}/help/{help_id}/secrets/{message_id}/use", response_model=None)
def help_secret_use(
    help_id: uuid.UUID = Path(...),
    message_id: int = Path(..., ge=1),
    lease: AgentLease = Depends(require_agent_lease),
) -> Response:
    """Release a helper secret once, to the login routine of the job holding
    the login page. The value never reaches the model or a log."""
    return _json(help_flow.use_secret(lease, _help_id(help_id), message_id), reveal=True)


@router.post("/jobs/{job_id}/contributions", response_model=None)
def contributions(payload: Any = Body(default=None), lease: AgentLease = Depends(require_agent_lease)) -> Response:
    """Helper files and notes for an RFI (applied when the RFI row exists)."""
    body = _parse_body(protocol.ContributionsRequest, payload)
    return _json(help_flow.contributions(lease, body))

