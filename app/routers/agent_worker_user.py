"""Personal worker traffic: private envelopes, live leases, no org trace writes.

Both personal types share these routes. The v2 type `user_browser`
(docs/ASSISTANT_BROWSER_V2_PLAN.md 5.4, 6.5, 7.4, 10.2, 11) adds the routes
in protocol.USER_V2_ENDPOINTS: policy, holds and their release long poll,
site holds, the session poll, login begin and result by credential id,
downloads (multipart) and the v2 help open. Every one is lease-fenced; a
v2 route refuses a retired `user_browser_read` job and the Procore
identity routes refuse a v2 job.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from app.assistant import repo
from app.core import agent_auth as auth
from app.core.supabase_client import get_supabase
from app.services import assistant_browser_runtime as runtime
from app.services import assistant_browser_help as help_flow
from app.services import assistant_browser_holds as holds
from app.services import assistant_browser_records as records
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
        # No screenshots during identity work (any credential of the job,
        # 7.4, 10.7) or in operator mode (9). The runner masks fields on
        # normal pages before upload; the API never stores raw browser files.
        if runtime.login_open(job) or kind != "screenshot" or job.get("control_mode") == "operator":
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
    job = _run(lambda: runtime.fenced(lease))
    if runtime.is_v2(job):
        return _open_help_v2(lease, _parse(protocol.UserHelpOpenV2Request, payload))
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


def _open_help_v2(lease, body: protocol.UserHelpOpenV2Request) -> dict:
    """v2 help open (UserHelpOpenV2Request, 8.1). The reason vocabulary is
    UserHelpReasonCode; `hold` rows are opened by the hold routes only."""
    def save():
        job = runtime.require_v2(runtime.fenced(lease))
        if body.reason_code == "cap_reached" and not body.login_attempt_id:
            return {"help_id": None, "joined_existing": False, "holder": False, "suppressed": True}
        if body.reason_code == "hold":
            raise runtime.BrowserUnavailable()
        hid = str(uuid4())
        state = help_flow.open_help_v2(job, reason=body.reason_code, message=body.message, help_id=hid,
            record_id=runtime.record_id(job, f"help:{hid}"), worker_id=lease.worker.id, boot_id=lease.boot_id,
            lease_epoch=lease.epoch, credential_id=str(body.credential_id) if body.credential_id else None,
            login_attempt_id=str(body.login_attempt_id) if body.login_attempt_id else None)
        return protocol.HelpOpenResponse(help_id=state["id"], joined_existing=not state["created"],
                                         holder=True).model_dump(mode="json")
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
    job = _run(lambda: runtime.fenced(lease))
    # v2: an mfa_step_up code belongs to the job's open help, not to a login
    # attempt, so the attempt id may be null (8.1).
    body = _parse(_UserHelpSecretUseV2 if runtime.is_v2(job) else protocol.UserHelpSecretUseRequest, payload)
    def consume():
        job, _ = _help_state(lease, help_id)
        value = help_flow.consume_secret(job, help_id=str(help_id), record_id=str(message_id),
            login_attempt_id=str(body.login_attempt_id) if body.login_attempt_id else None,
            worker_id=lease.worker.id, boot_id=lease.boot_id, lease_epoch=lease.epoch)
        return {"value": value}
    return _run(consume)


class _UserHelpSecretUseV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_attempt_id: UUID | None = None


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
                if runtime.is_v2(job):
                    result["help_updates"].extend(_hold_updates(job, data.get("help_id")))
            except Exception:
                result["lost"].append(jid)
        jobs = (sb.table("browser_agent_jobs").select("id,lease_epoch").eq("lease_owner", worker.id)
            .eq("lease_boot_id", str(body.boot_id)).eq("principal_kind", "user")
            .in_("status", ["running", "waiting_help"]).limit(8).execute()).data or []
        result["orphans"] = [{"job_id": row["id"], "epoch": row["lease_epoch"]} for row in jobs if row["id"] not in seen]
        return result
    return _run(renew)


# ── v2 routes (protocol.USER_V2_ENDPOINTS) ───────────────────────────────────

# A decided hold is reported on heartbeats for this long after it was opened,
# so a worker that missed the release poll answer learns of the change (6.5).
HOLD_UPDATE_WINDOW_SECONDS = 15 * 60


def _hold_updates(job: dict, open_help_id) -> list[dict]:
    """HelpUpdate rows for the job's latest hold once it is no longer open:
    its help row is resolved, so the worker polls the release route."""
    from datetime import datetime, timedelta, timezone

    try:
        rows = repo.browser_holds(job["principal_user_id"], str(job["id"]), limit=1)
    except Exception:  # noqa: BLE001 - the release long poll is the main channel
        return []
    out = []
    since = datetime.now(timezone.utc) - timedelta(seconds=HOLD_UPDATE_WINDOW_SECONDS)
    for row in rows:
        try:
            created = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if (row.get("status") != "open" and row.get("help_id") and str(row["help_id"]) != str(open_help_id or "")
                and created.tzinfo is not None and created >= since):
            out.append({"job_id": str(job["id"]), "help_id": str(row["help_id"]), "status": "resolved"})
    return out


def _v2(lease) -> dict:
    return runtime.require_v2(runtime.fenced(lease))


@router.get("/jobs/{job_id}/user/policy")
def user_policy(lease: auth.AgentLease = Depends(_lease)):
    """UserJobPolicy (10.2): fetched after claim and after every site hold
    release, safe-rule change or scope fence code."""
    return _run(lambda: runtime.job_policy(lease))


@router.post("/jobs/{job_id}/user/holds")
def user_hold(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    """A held request or click (6.2, 6.5 step 1). Answers UserHoldRelease
    (pending); the owner's card and Teams alert come from the outbox."""
    body = _parse(protocol.UserHoldRequest, payload)
    return _run(lambda: holds.open_hold(_v2(lease), lease.worker.id, lease.boot_id, lease.epoch,
                                        body.model_dump(mode="json")))


# Release long polls in flight, per (worker, hold) and per worker. The wait
# is an asyncio sleep, never a threadpool thread; each read is one short
# threadpool call (the sync Supabase SDK never runs on the event loop).
_RELEASE_POLLS: set[tuple[str, str]] = set()
_RELEASE_POLLS_PER_WORKER: dict[str, int] = {}
_RELEASE_POLLS_LOCK = threading.Lock()
RELEASE_POLLS_PER_WORKER_MAX = 8
RELEASE_POLL_STEP_SECONDS = holds.LONG_POLL_STEP_SECONDS


def _release_poll_begin(worker_id: str, hold_id: str) -> bool:
    key = (worker_id, hold_id)
    with _RELEASE_POLLS_LOCK:
        if key in _RELEASE_POLLS or _RELEASE_POLLS_PER_WORKER.get(worker_id, 0) >= RELEASE_POLLS_PER_WORKER_MAX:
            return False
        _RELEASE_POLLS.add(key)
        _RELEASE_POLLS_PER_WORKER[worker_id] = _RELEASE_POLLS_PER_WORKER.get(worker_id, 0) + 1
        return True


def _release_poll_end(worker_id: str, hold_id: str) -> None:
    with _RELEASE_POLLS_LOCK:
        _RELEASE_POLLS.discard((worker_id, hold_id))
        left = _RELEASE_POLLS_PER_WORKER.get(worker_id, 0) - 1
        if left > 0:
            _RELEASE_POLLS_PER_WORKER[worker_id] = left
        else:
            _RELEASE_POLLS_PER_WORKER.pop(worker_id, None)


async def _arun(call):
    """_run for an async route: the call runs in the threadpool."""
    try:
        return await run_in_threadpool(call)
    except HTTPException:
        raise
    except Exception:
        raise auth.agent_error(protocol.ErrorCode.INVALID_REQUEST, "Personal browser operation is unavailable.") from None


def _release_pending(lease, hold_id: str) -> dict:
    """A pending answer with no release read (a second poll for the same
    hold, or past the per-worker cap): nothing is consumed."""
    job = _v2(lease)
    hold = repo.browser_hold(str(job["principal_user_id"]), hold_id) or {}
    if str(hold.get("job_id") or "") != str(job["id"]) or hold.get("kind") not in ("final_action", "site"):
        raise ValueError("hold")
    return protocol.UserHoldRelease(hold_id=hold_id, kind=hold["kind"], status="pending",
                                    retry_after=1).model_dump(mode="json")


@router.get("/jobs/{job_id}/user/holds/{hold_id}/release")
async def user_hold_release(hold_id: UUID, lease: auth.AgentLease = Depends(_lease)):
    """Long poll (up to 25 s) for the owner's decision: a signed token for a
    final action (answered once), a site release, a decline or pending.
    One poll per hold and at most 8 per worker wait at a time; another one
    answers pending at once."""
    hid, worker_id = str(hold_id), str(lease.worker.id)
    if not _release_poll_begin(worker_id, hid):
        return await _arun(lambda: _release_pending(lease, hid))
    try:
        job = await _arun(lambda: _v2(lease))

        def read():
            return holds.release_status(job["id"], lease.worker.id, lease.boot_id, lease.epoch, hid,
                                        wait_seconds=0, user_id=job["principal_user_id"])

        deadline = time.monotonic() + protocol.USER_HOLD_RELEASE_POLL_SECONDS
        while True:
            answer = await _arun(read)
            left = deadline - time.monotonic()
            if not isinstance(answer, dict) or answer.get("status") != "pending" or left <= 0:
                return answer
            await asyncio.sleep(min(RELEASE_POLL_STEP_SECONDS, left))
    finally:
        _release_poll_end(worker_id, hid)


@router.post("/jobs/{job_id}/user/site-hold")
def user_site_hold(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    """A top-level navigation outside the task scope, held before DNS (5.4)."""
    body = _parse(protocol.UserSiteHoldRequest, payload)
    return _run(lambda: holds.open_site_hold(_v2(lease), lease.worker.id, lease.boot_id, lease.epoch,
                                             body.model_dump(mode="json")))


@router.get("/jobs/{job_id}/user/session")
def user_session(request: Request, lease: auth.AgentLease = Depends(_lease)):
    """UserSessionPoll (7.4): a session another task of this account saved
    after the job's claim, never another account's jar."""
    body = _parse(protocol.UserSessionPoll, dict(request.query_params))
    return _run(lambda: runtime.session_poll(lease, body))


@router.post("/jobs/{job_id}/user/login/begin")
def user_login_begin(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    """fill_credential (7.4): the username and password for one attempt, for
    a credential in the job's set, on one of its login origins."""
    body = _parse(protocol.UserCredentialFillRequest, payload)
    return _run(lambda: runtime.login_begin_v2(lease, body))


@router.post("/jobs/{job_id}/user/login/result")
def user_login_result(payload: Any = Body(None), lease: auth.AgentLease = Depends(_lease)):
    body = _parse(protocol.UserCredentialResultRequest, payload)
    return _run(lambda: runtime.login_finish_v2(lease, body))


@router.post("/jobs/{job_id}/user/downloads")
def user_download(file: UploadFile = File(...), sha256: str = Form(...), filename: str = Form(...),
                  size_bytes: int = Form(...), sniffed_kind: str = Form(...), source_origin: str = Form(...),
                  scan: str = Form(...), lease: auth.AgentLease = Depends(_lease)):
    """A scanned download (11.1, 11.2). Multipart, so the upload cap applies;
    the server re-sniffs and re-hashes before it writes the project file or
    the staging copy (assistant_browser_downloads, WI-15)."""
    meta = _parse(protocol.UserDownloadMeta, {"sha256": sha256, "filename": filename, "size_bytes": size_bytes,
                                              "sniffed_kind": sniffed_kind, "source_origin": source_origin,
                                              "scan": scan})
    def save():
        from app.services import assistant_browser_downloads as downloads

        job = _v2(lease)
        result = downloads.accept(job, lease.worker.id, lease.boot_id, lease.epoch, stream=file.file,
                                  meta=meta.model_dump(mode="json"))
        return protocol.UserDownloadResponse.model_validate(result).model_dump(mode="json")
    return _run(save)
