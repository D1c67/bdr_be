"""Worker API, Procore and RFI half (docs/browser_agents/SPEC.md 3.2, 5.4, 5.5 with O2):

  POST /agent-worker/v1/jobs/{job_id}/identities/{identity}/session
  POST /agent-worker/v1/jobs/{job_id}/identities/{identity}/login/begin
  POST /agent-worker/v1/jobs/{job_id}/identities/{identity}/login/result
  POST /agent-worker/v1/jobs/{job_id}/rfi-capture

The work is in services/browser_agents/identities.py (the shared login) and
pm_apps/procore.py (capture validation); this module is the HTTP edge with
the same rules as routers/agent_worker.py: plain `def`, a bare 404 while
BROWSER_AGENTS_ENABLED is off, the worker's token and lease headers,
Cache-Control: no-store on everything, X-Error-Code on every refusal.

login/begin is the one response that carries the Procore password. It is
serialized with `wire_json()` (pydantic masks SecretStr everywhere else),
sent no-store, and never logged here.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Path
from fastapi.responses import Response

from app.core import supabase_client
from app.core.agent_auth import AgentLease, agent_error, require_agent_lease, rpc
from app.core.features import require_browser_agents
from app.routers.agent_worker import _no_store, _parse_body
from app.services.browser_agents import identities, protocol, queue, registry
from app.services.browser_agents.pm_apps import procore as procore_app
from app.services.browser_agents.protocol import ErrorCode
from app.services.browser_agents.types import rfi_from_email

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix=protocol.API_PREFIX,
    tags=["agent-worker"],
    dependencies=[Depends(require_browser_agents), Depends(_no_store)],
)

IDENTITY_PATH = Path(..., pattern=protocol.AGENT_TYPE_RE.pattern)

# The sweeper steps this phase owns (unreported logins, contributions).
rfi_from_email.register_sweep_steps()


def _json(model: protocol._Resp, *, reveal: bool = False) -> Response:
    content = model.wire_json() if reveal else model.model_dump_json()
    return Response(content=content, media_type="application/json", headers={"Cache-Control": "no-store"})


# ── Identities (O2) ─────────────────────────────────────────────────────────────


@router.post("/jobs/{job_id}/identities/{identity}/session", response_model=None)
def identity_session(
    identity: str = IDENTITY_PATH,
    payload: Any = Body(default=None),
    lease: AgentLease = Depends(require_agent_lease),
) -> Response:
    """The shared stored cookie jar for the job's host, or why not:
    no_jar | locked | help_open | not_configured. No Procore call."""
    body = _parse_body(protocol.SessionRequest, payload)
    return _json(identities.session(lease, identity, body))


@router.post("/jobs/{job_id}/identities/{identity}/login/begin", response_model=None)
def identity_login_begin(
    identity: str = IDENTITY_PATH,
    payload: Any = Body(default=None),
    lease: AgentLease = Depends(require_agent_lease),
) -> Response:
    """Reserve the shared login slot and release the account to the worker's
    login routine (granted: attempt_id, email, password), or refuse it
    (spacing | locked | help_open | not_configured, with retry_at/help_id)."""
    body = _parse_body(protocol.LoginBeginRequest, payload)
    return _json(identities.login_begin(lease, identity, body), reveal=True)


@router.post("/jobs/{job_id}/identities/{identity}/login/result", response_model=None)
def identity_login_result(
    identity: str = IDENTITY_PATH,
    payload: Any = Body(default=None),
    lease: AgentLease = Depends(require_agent_lease),
) -> Response:
    """What the login page did. ok saves the jar for every Procore client;
    password_rejected counts toward the shared lock; the rest record
    last_error. help_reason tells the worker which help to open."""
    body = _parse_body(protocol.LoginResultRequest, payload)
    return _json(identities.login_result(lease, identity, body))


# ── POST /jobs/{job_id}/rfi-capture ─────────────────────────────────────────────


def _sb():
    return supabase_client.get_supabase()


@router.post("/jobs/{job_id}/rfi-capture", response_model=None)
def rfi_capture(payload: Any = Body(default=None), lease: AgentLease = Depends(require_agent_lease)) -> Response:
    """Hand a validated rfi_detail_json capture to the PM RFI sync: one
    pm_rfi_sync_runs row (trigger 'agent'), the job moves to applying and the
    sweeper closes it from the run. A capture that fails validation fails
    the job (capture_invalid, 422). A resend after success returns the same
    run."""
    body = _parse_body(protocol.RfiCaptureRequest, payload)
    sb = _sb()
    rows = sb.table(queue.JOBS).select("*").eq("id", lease.job_id).limit(1).execute().data or []
    if not rows:
        raise agent_error(ErrorCode.LEASE_LOST)
    job = rows[0]
    try:
        spec = registry.get_type(job["agent_type"])
    except registry.UnknownAgentType:
        raise agent_error(ErrorCode.INVALID_REQUEST, "unknown agent type") from None
    if job.get("principal_kind", "org") != "org" or not spec.allows_capture or spec.platform != body.platform:
        raise agent_error(ErrorCode.INVALID_REQUEST, "this job type does not capture RFIs")
    if job.get("status") == "applying" and job.get("sync_run_id") and int(job.get("lease_epoch") or 0) == lease.epoch:
        runs = sb.table("pm_rfi_sync_runs").select("id, agent_job_id, detail_artifact_id").eq(
            "id", str(job["sync_run_id"])).limit(1).execute().data or []
        if runs and str(runs[0].get("agent_job_id")) == lease.job_id and str(
                runs[0].get("detail_artifact_id")) == str(body.detail_artifact_id):
            return _json(protocol.RfiCaptureResponse(run_id=runs[0]["id"], accepted=True))
    # Fence before judging the capture: a stale worker cannot fail the job.
    rpc("browser_agent_fence", {**lease.params(), "p_statuses": ["running", "waiting_help"]})
    try:
        envelope = procore_app.load_and_check(sb, job, body, queue.now_utc())
    except procore_app.CaptureInvalid as exc:
        changed = rpc("browser_agent_finish", {
            **lease.params(), "p_status": "failed", "p_outcome": None, "p_error_kind": "capture_invalid",
            "p_error": f"The capture was refused ({exc.reason}).",
            "p_result": {"summary": "Capture refused", "capture_refused": exc.reason},
        })
        if changed:
            queue.notify_job_failed(job, "capture_invalid", f"the capture was refused ({exc.reason})")
        raise agent_error(ErrorCode.CAPTURE_INVALID, f"capture refused: {exc.reason}") from None
    run_id = rpc("browser_agent_submit_rfi_capture", {
        **lease.params(), "p_artifact": str(body.detail_artifact_id), "p_external_rfi_id": body.external_rfi_id,
    })
    if isinstance(run_id, list):
        run_id = run_id[0] if run_id else None
    if not run_id:
        raise agent_error(ErrorCode.CAPTURE_INVALID, "the capture was not queued")
    # The company the capture was validated against, for the apply shim
    # (the envelope may not carry one).
    # Merged into result: a helper's cap reset (help.cap_reset_patch) keeps
    # the totals spent before it there.
    prior = job.get("result") if isinstance(job.get("result"), dict) else {}
    sb.table(queue.JOBS).update({"result": {**prior, "capture": {
        "company_id": body.company_id, "rfi_id": body.external_rfi_id,
        "artifact_id": str(body.detail_artifact_id), "captured_at": envelope.captured_at.isoformat(),
    }}}).eq("id", lease.job_id).execute()
    return _json(protocol.RfiCaptureResponse(run_id=run_id, accepted=True))
