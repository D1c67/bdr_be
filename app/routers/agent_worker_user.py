"""Personal worker traffic: private envelopes, live leases, no org trace writes."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.assistant import repo
from app.core import agent_auth as auth
from app.core.supabase_client import get_supabase
from app.services import assistant_browser_runtime as runtime
from app.services import assistant_browser_help as help_flow
from app.services import assistant_browser_records as records
from app.services import assistant_browser_vault as vault
from app.services.browser_agents import protocol

router = APIRouter(prefix="/agent-worker/v1", tags=["personal-browser-worker"])
HEADERS = {"Cache-Control": "no-store"}


@dataclass(frozen=True)
class PersonalLease(auth.AgentLease):
    worker_token: str = field(default="", repr=False, compare=False)


def _run(call):
    try:
        return call()
    except HTTPException:
        raise
    except Exception:
        raise auth.agent_error(protocol.ErrorCode.INVALID_REQUEST, "Personal browser operation is unavailable.") from None


def _parse(model, payload):
    try:
        return model.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        raise auth.agent_error(protocol.ErrorCode.INVALID_REQUEST, "Invalid personal browser request.") from None


def _lease(request: Request, response: Response, job_id: UUID,
           authorization: str = Header(default=""), x_agent_boot: str = Header(default=""),
           x_agent_lease: str = Header(default="")) -> auth.AgentLease:
    response.headers.update(HEADERS)
    worker = auth.authenticate(request, authorization, allow_pending=False)
    if worker.principal_kind != "user":
        raise auth.agent_error(protocol.ErrorCode.IDOR)
    boot = auth.parse_boot(x_agent_boot, required=True)
    auth.check_boot(worker, boot)
    lease = PersonalLease(worker, str(job_id), auth.parse_lease(x_agent_lease, str(job_id)), boot,
                          auth._bearer(authorization))
    _run(lambda: runtime.fenced(lease))
    return lease


def _record(lease, job, kind, payload, tag, seq=0):
    return records.append_record(job, kind, payload, record_id=runtime.record_id(job, tag), seq=seq,
        worker_id=lease.worker.id, boot_id=lease.boot_id, lease_epoch=lease.epoch)


@router.post("/jobs/{job_id}/user/steps")
def steps(request: Request, payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.AppendStepsRequest, payload)
    def save():
        job = runtime.fenced(lease)
        token = request.headers.get("authorization", "").removeprefix("Bearer ").removeprefix("bearer ")
        for step in body.steps:
            clean = runtime.sanitize(job, step.model_dump(mode="json"), worker_token=token)
            _record(lease, job, "step", clean, f"step:{lease.epoch}:{step.idem_key}")
        return {"appended": len(body.steps), "duplicates": 0, "late": 0}
    return _run(save)


@router.put("/jobs/{job_id}/user/checkpoint", status_code=204)
def checkpoint(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.CheckpointRequest, payload)
    _run(lambda: _record(lease, runtime.fenced(lease), "step", {"checkpoint": body.data},
                        f"checkpoint:{lease.epoch}:{body.seq}", body.seq))
    return Response(status_code=204, headers=HEADERS)


@router.post("/jobs/{job_id}/user/artifacts")
def artifact(file: UploadFile = File(...), kind: str = Form(...), sha256: str = Form(...),
             filename: str = Form(...), lease: auth.AgentLease = Depends(_lease)):
    def save():
        job = runtime.fenced(lease)
        credential = vault.job_credential(job)
        # No screenshots during identity work. The runner masks fields on
        # normal pages before upload; the API never stores raw browser files.
        if credential.get("login_attempt_id") or kind != "screenshot":
            raise runtime.BrowserUnavailable()
        blob = file.file.read(records.MAX_IMAGE_BYTES + 1)
        if hashlib.sha256(blob).hexdigest() != sha256:
            raise runtime.BrowserUnavailable()
        mime = "image/png" if blob.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
        payload = {"mime_type": mime, "data": base64.b64encode(blob).decode("ascii")}
        records._image_bytes(payload)
        row = _record(lease, job, "screenshot", payload, f"image:{lease.epoch}:{sha256}")
        return {"artifact_id": row["id"], "existed": False}
    return _run(save)


@router.post("/jobs/{job_id}/user/turns")
def turns(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.UserTurnRequest, payload)
    status, result = _run(lambda: runtime.turn(lease, body))
    return JSONResponse(result, status_code=status, headers=HEADERS)


def _identity(identity):
    if identity != "procore":
        raise auth.agent_error(protocol.ErrorCode.IDOR)


@router.post("/jobs/{job_id}/user-identities/{identity}/session")
def identity_session(identity: str, payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    _identity(identity)
    body = _parse(protocol.UserSessionRequest, payload)
    return _run(lambda: runtime.identity_session(lease, body))


@router.post("/jobs/{job_id}/user-identities/{identity}/login/begin")
def identity_begin(identity: str, payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    _identity(identity)
    body = _parse(protocol.UserLoginBeginRequest, payload)
    return _run(lambda: runtime.identity_begin(lease, body))


@router.post("/jobs/{job_id}/user-identities/{identity}/login/result")
def identity_result(identity: str, payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    _identity(identity)
    body = _parse(protocol.UserLoginResultRequest, payload)
    return _run(lambda: runtime.identity_finish(lease, body))


@router.post("/jobs/{job_id}/user/finish", status_code=204)
def finish(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.FinishRequest, payload)
    _run(lambda: runtime.finish(lease, body.status, body.summary, outcome=body.outcome))
    return Response(status_code=204, headers=HEADERS)


def _release(lease, body=None):
    job = runtime.fenced(lease)
    params = runtime.params(lease)
    if body is not None:
        params.update(p_reason=body.reason, p_retry_at=body.retry_at.isoformat() if body.retry_at else None)
    result = repo.browser_rpc(job["principal_user_id"], "assistant_browser_job_requeue", params)
    if not result.get("ok"):
        raise runtime.BrowserUnavailable()


@router.post("/jobs/{job_id}/user/requeue", status_code=204)
def requeue(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.RequeueRequest, payload)
    _run(lambda: _release(lease, body))
    return Response(status_code=204, headers=HEADERS)


@router.post("/jobs/{job_id}/user/park", status_code=204)
def park(lease: auth.AgentLease = Depends(_lease)):
    _run(lambda: _release(lease))
    return Response(status_code=204, headers=HEADERS)


@router.post("/jobs/{job_id}/user/help")
def open_help(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.UserHelpOpenRequest, payload)
    def save():
        job = runtime.fenced(lease)
        # A cap stop is final for a personal job: answer suppressed, even
        # beside an open help, so the worker finishes failed instead of parking.
        if body.reason_code == "cap_reached" and not body.login_attempt_id:
            return {"help_id": None, "joined_existing": False, "holder": False, "suppressed": True}
        active = repo.browser_help_state(job["principal_user_id"], job["id"])
        if active and active.get("status") == "open":
            return {"help_id": active["id"], "joined_existing": True, "holder": True, "suppressed": False}
        hid = str(uuid4())
        attempt = str(body.login_attempt_id) if body.login_attempt_id else None
        state = help_flow.open_help(job, reason=body.reason_code, title="Your browser task needs help",
            message=body.message or "Please review your browser task.", is_login_scope=bool(attempt),
            help_id=hid, record_id=runtime.record_id(job, f"help:{hid}"), seq=0,
            worker_id=lease.worker.id, boot_id=lease.boot_id, lease_epoch=lease.epoch, login_attempt_id=attempt)
        return {"help_id": state["id"], "joined_existing": False, "holder": True, "suppressed": False}
    return _run(save)


def _help_state(lease, help_id):
    job = runtime.fenced(lease)
    state = help_flow.worker_state(job)
    if state.get("id") != str(help_id):
        raise auth.agent_error(protocol.ErrorCode.IDOR)
    return job, state


@router.get("/jobs/{job_id}/user/help/{help_id}/messages")
def help_messages(help_id: UUID, lease: auth.AgentLease = Depends(_lease)):
    def read():
        _, state = _help_state(lease, help_id)
        messages = [{"id": row["id"], "author_kind": "helper" if row["author_kind"] == "owner" else "agent",
            "action": row.get("action") or "message", "body": row.get("body"), "created_at": row["created_at"],
            "secret_ref": {"message_id": row["id"], "consumed": row["consumed"]} if row.get("is_secret") else None,
            "files": []} for row in state["messages"]]
        return {"status": state["status"], "messages": messages}
    return _run(read)


@router.post("/jobs/{job_id}/user/help/{help_id}/consume", status_code=204)
def help_consume(help_id: UUID, payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.UserHelpConsumeRequest, payload)
    def consume():
        _, state = _help_state(lease, help_id)
        known = {row["id"] for row in state["messages"] if not row.get("is_secret")}
        if any(str(value) not in known for value in body.message_ids):
            raise auth.agent_error(protocol.ErrorCode.IDOR)
        # Acknowledgement is memory-only on the personal runner. A reconnect
        # may reread a nonsecret message; secret delivery has its own SQL CAS.
    _run(consume)
    return Response(status_code=204, headers=HEADERS)


@router.post("/jobs/{job_id}/user/help/{help_id}/messages")
def help_reply(help_id: UUID, payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.HelpReplyRequest, payload)
    def save():
        job, _ = _help_state(lease, help_id)
        row = _record(lease, job, "step", {"help_id": str(help_id), "summary": body.body},
                      f"help-reply:{help_id}:{hashlib.sha256(body.body.encode()).hexdigest()}")
        return {"id": row["id"]}
    return _run(save)


@router.post("/jobs/{job_id}/user/help/{help_id}/secrets/{message_id}/use")
def help_secret(help_id: UUID, message_id: UUID, payload: Any = Body(None),
                lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.UserHelpSecretUseRequest, payload)
    def consume():
        job, _ = _help_state(lease, help_id)
        value = help_flow.consume_secret(job, help_id=str(help_id), record_id=str(message_id),
            login_attempt_id=str(body.login_attempt_id), worker_id=lease.worker.id,
            boot_id=lease.boot_id, lease_epoch=lease.epoch)
        return {"value": value}
    return _run(consume)


@router.post("/user/heartbeat")
def heartbeat(response: Response, payload: Any = Body(None),
              worker: auth.AgentWorker = Depends(auth.require_agent_worker), x_agent_boot: str = Header(default="")):
    response.headers.update(HEADERS)
    body = _parse(protocol.HeartbeatRequest, payload)
    if worker.principal_kind != "user" or str(body.boot_id) != auth.parse_boot(x_agent_boot, required=True):
        raise auth.agent_error(protocol.ErrorCode.IDOR)
    auth.check_boot(worker, str(body.boot_id))
    if len(body.jobs) > 8:
        raise auth.agent_error(protocol.ErrorCode.INVALID_REQUEST)
    def renew():
        sb = get_supabase()
        if not auth.rpc("assistant_browser_worker_ping", {"p_worker": worker.id, "p_boot": str(body.boot_id)}):
            raise auth.agent_error(protocol.ErrorCode.STALE_BOOT)
        settings = (sb.table("browser_agent_settings").select("config_version").limit(1).execute()).data or [{}]
        result = {"config_version": settings[0].get("config_version", 1), "renewed": [], "lost": [],
                  "cancel": [], "orphans": [], "help_updates": [], "drain": worker.draining, "controls": []}
        seen = set()
        for entry in body.jobs:
            jid = str(entry.job_id)
            seen.add(jid)
            lease = auth.AgentLease(worker, jid, entry.epoch, str(body.boot_id))
            try:
                job = runtime.fenced(lease)
                data = repo.browser_rpc(job["principal_user_id"], "assistant_browser_job_heartbeat",
                    {**runtime.params(lease), "p_active_seconds_delta": entry.active_seconds_delta})
                if not data.get("ok"):
                    raise runtime.BrowserUnavailable()
                result["renewed"].append({"job_id": jid, "lease_expires_at": data["job"]["lease_expires_at"]})
                if data.get("help_id"):
                    result["help_updates"].append({"job_id": jid, "help_id": data["help_id"], "status": "open"})
            except Exception:
                result["lost"].append(jid)
        jobs = (sb.table("browser_agent_jobs").select("id,lease_epoch").eq("lease_owner", worker.id)
            .eq("lease_boot_id", str(body.boot_id)).eq("principal_kind", "user")
            .in_("status", ["running", "waiting_help"]).limit(8).execute()).data or []
        result["orphans"] = [{"job_id": row["id"], "epoch": row["lease_epoch"]} for row in jobs if row["id"] not in seen]
        return result
    return _run(renew)
