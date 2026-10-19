"""Worker API for the browser agent platform: /agent-worker/v1 (SPEC.md 3).

The Windows worker (bdr_be/agent_worker) is the only caller. Every request
and response body is a model from app/services/browser_agents/protocol.py,
the frozen contract the worker vendors byte for byte; this router never
invents a shape of its own.

Served here (B2): enroll, hello, config, heartbeat, claim, steps,
checkpoint, artifacts, finish, requeue, park, releases and the canary page.
The turns, identities (login), help, contributions and rfi-capture routes
belong to the later phases and are mounted from their own modules.

Rules every handler follows:
  - plain `def` (the Supabase SDK is synchronous; FastAPI runs these in its
    threadpool);
  - a bare 404 while BROWSER_AGENTS_ENABLED is off (router dependency,
    solved before auth, like every other feature gate);
  - Cache-Control: no-store on every response, errors included;
  - every refusal carries X-Error-Code with a protocol.ErrorCode value; the
    fenced RPCs raise those codes and core.agent_auth.rpc maps them 1:1;
  - nothing Procore-facing: this router never writes to Procore.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from app.core import supabase_client
from app.core.agent_auth import (
    AgentLease,
    AgentWorker,
    agent_error,
    bytea_literal,
    client_ip,
    new_worker_token,
    normalize_enroll_code,
    parse_boot,
    require_agent_lease,
    require_agent_worker,
    require_agent_worker_or_pending,
    rpc,
    sha256_bytes,
)
from app.core.config import get_settings
from app.core.features import require_browser_agents
from app.core.redact import redact_text, redact_url
from app.services import notifications
from app.services.browser_agents import artifacts as artifact_store
from app.services.browser_agents import protocol, queue, registry
from app.services.browser_agents.protocol import ErrorCode
from app.services.browser_agents.types import canary

logger = logging.getLogger(__name__)

UTC = timezone.utc
ENROLL_IP_FALLBACK = "0.0.0.0"   # inet column; a request with no readable peer shares one bucket


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix=protocol.API_PREFIX,
    tags=["agent-worker"],
    dependencies=[Depends(require_browser_agents), Depends(_no_store)],
)


def _sb():
    return supabase_client.get_supabase()


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_body(model, payload: Any):
    """Validate a request body against its protocol model. A malformed body
    is invalid_request (422) with the first problem's location only: no
    echo of values, which may be secrets."""
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "body"
        raise agent_error(ErrorCode.INVALID_REQUEST, f"invalid request: {where}") from None


def _header_boot_matches(header_boot: str, body_boot: uuid.UUID) -> None:
    """X-Agent-Boot (when sent) must agree with the body's boot_id."""
    boot = parse_boot(header_boot, required=False)
    if boot is not None and boot != str(body_boot):
        raise agent_error(ErrorCode.INVALID_REQUEST, "X-Agent-Boot disagrees with boot_id")


def _settings_row() -> dict[str, Any]:
    return queue.load_settings()


def _desired_release(settings_row: dict[str, Any]) -> dict[str, Any] | None:
    """The release IT Admin pinned (desired_worker_version), when its row
    exists and its manifest and signature check out. A row that fails the
    check is never advertised (and is logged loudly)."""
    version = settings_row.get("desired_worker_version")
    if not version:
        return None
    rows = (
        _sb().table("browser_agent_worker_releases").select("*").eq("version", version).limit(1).execute()
    ).data or []
    if not rows:
        return None
    row = rows[0]
    if not artifact_store.verify_release(row, get_settings().browser_agent_release_pubkey):
        logger.error("browser agents: release %s failed its signature check; not advertised", version)
        return None
    return row


# ── 1. POST /enroll (no auth) ─────────────────────────────────────────────────


def _count_enroll_attempt(ip: str) -> None:
    """DB counter, BROWSER_AGENT_ENROLL_PER_HOUR per IP per clock hour
    (every attempt counts, good or bad). Compare-and-set increments, so two
    workers racing cannot both slip under the cap."""
    sb = _sb()
    window = _now().replace(minute=0, second=0, microsecond=0)
    window_iso = queue.iso(window)
    limit = get_settings().browser_agent_enroll_per_hour
    for _ in range(5):
        rows = (
            sb.table("browser_agent_enroll_attempts").select("count")
            .eq("ip", ip).eq("window_start", window_iso).limit(1).execute()
        ).data or []
        if not rows:
            try:
                sb.table("browser_agent_enroll_attempts").insert(
                    {"ip": ip, "window_start": window_iso, "count": 1}
                ).execute()
                return
            except Exception:  # noqa: BLE001 - a racing insert won; read it again
                continue
        count = int(rows[0].get("count") or 0)
        if count >= limit:
            retry = int((window + timedelta(hours=1) - _now()).total_seconds()) + 1
            raise agent_error(ErrorCode.RATE_LIMITED, "too many enrollment attempts",
                              headers={"Retry-After": str(max(retry, 1))})
        updated = (
            sb.table("browser_agent_enroll_attempts").update({"count": count + 1})
            .eq("ip", ip).eq("window_start", window_iso).eq("count", count).execute()
        ).data
        if updated:
            return
    raise agent_error(ErrorCode.RATE_LIMITED, "too many enrollment attempts")


def _unique_worker_name(base: str) -> str:
    """The enrollment's worker name, suffixed when a (revoked) worker
    already holds it: names are unique and workers are never deleted."""
    sb = _sb()
    base = base.strip()[:52] or "Agent PC"
    if len(base) < 2:
        base = f"{base} PC"
    for n in range(1, 50):
        name = base if n == 1 else f"{base} ({n})"
        taken = sb.table("browser_agent_workers").select("id").eq("name", name).limit(1).execute().data
        if not taken:
            return name
    return f"{base[:40]} {uuid.uuid4().hex[:8]}"


@router.post("/enroll", response_model=None)
def enroll(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
    """Trade a single-use enrollment code for a worker token. The worker
    starts `pending`: it may /hello and /config, and claims nothing until an
    IT Admin approves it. The token is returned once and stored only as a
    sha256 hash; the code is spent whatever happens next."""
    ip = client_ip(request) or ENROLL_IP_FALLBACK
    _count_enroll_attempt(ip)
    if isinstance(payload, dict) and isinstance(payload.get("code"), str):
        payload = {**payload, "code": normalize_enroll_code(payload["code"])}
    body = _parse_body(protocol.EnrollRequest, payload)
    sb = _sb()
    rows = (
        sb.table("browser_agent_enrollments").select("*")
        .eq("code_hash", bytea_literal(sha256_bytes(body.code))).limit(1).execute()
    ).data or []
    now = _now()
    enrollment = rows[0] if rows else None
    if (
        enrollment is None
        or enrollment.get("used_at") is not None
        or (queue.parse_ts(enrollment.get("expires_at")) or now) <= now
    ):
        raise agent_error(ErrorCode.ENROLL_INVALID, "enrollment code is invalid, used or expired")
    spent = (
        sb.table("browser_agent_enrollments").update({"used_at": queue.iso(now)})
        .eq("id", enrollment["id"]).is_("used_at", "null").execute()
    ).data
    if not spent:
        raise agent_error(ErrorCode.ENROLL_INVALID, "enrollment code is invalid, used or expired")
    token, token_hash, prefix = new_worker_token()
    worker_row = {
        "name": _unique_worker_name(enrollment.get("worker_name") or body.hostname),
        "status": "pending",
        "token_hash": bytea_literal(token_hash),
        "token_prefix": prefix,
        "hostname": body.hostname,
        "os_info": body.os_info,
        "version": body.version,
        "enroll_ip": ip if ip != ENROLL_IP_FALLBACK else None,
        "created_by": enrollment.get("created_by"),
    }
    worker = (sb.table("browser_agent_workers").insert(worker_row).execute()).data[0]
    sb.table("browser_agent_enrollments").update({"worker_id": worker["id"]}).eq("id", enrollment["id"]).execute()
    try:
        notifications.audit(
            enrollment.get("created_by"), "browser_agent.worker_enrolled", "browser_agent_workers",
            str(worker["id"]),
            {"worker_name": worker["name"], "hostname": body.hostname, "version": body.version, "ip": ip},
        )
    except Exception:  # noqa: BLE001 - the audit row is best effort here; the worker row is the record
        logger.exception("browser agents: enroll audit row failed")
    queue.notify_it_admin(
        "browser_agent.worker_pending",
        f"A browser agent worker ({body.hostname}) enrolled and is waiting for approval.",
        {"worker_id": str(worker["id"]), "hostname": body.hostname, "ip": ip},
    )
    return protocol.EnrollResponse(worker_id=worker["id"], token=token, status="pending").model_dump(mode="json")


# ── 2. POST /hello ────────────────────────────────────────────────────────────


@router.post("/hello")
def hello(
    payload: Any = Body(default=None),
    worker: AgentWorker = Depends(require_agent_worker_or_pending),
    x_agent_boot: str = Header(default=""),
) -> dict[str, Any]:
    """Once per worker process start. Registers the new boot (every lease
    an older boot held is released uncharged: running jobs requeue
    'interrupted', waiting jobs park) and counts restarts: more than 6 in an
    hour drains the worker (suspended) and bells IT Admin once."""
    body = _parse_body(protocol.HelloRequest, payload)
    _header_boot_matches(x_agent_boot, body.boot_id)
    info = {
        "version": body.version,
        "chromium_version": body.chromium_version,
        "bad_versions": body.bad_versions,
        "capabilities": body.capabilities.model_dump(mode="json"),
    }
    data = rpc("browser_agent_hello", {
        "p_worker": worker.id, "p_boot": str(body.boot_id), "p_info": info, "p_ip": worker.ip,
    }) or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    if data.get("status") == "suspended" and int(data.get("hello_count_hour") or 0) == 7:
        queue.notify_it_admin(
            "browser_agent.worker_suspicious",
            f"Browser agent worker {worker.name} restarted more than 6 times in an hour and was "
            "set to draining. Check the PC before clearing it.",
            {"worker_id": worker.id},
        )
    settings_row = _settings_row()
    release = _desired_release(settings_row)
    return protocol.HelloResponse(
        status=data.get("status") or worker.status,
        config_version=int(data.get("config_version") or settings_row.get("config_version") or 1),
        desired_version=release["version"] if release else None,
        min_version=release["min_version"] if release else None,
        server_time=data.get("server_time") or _now(),
        requeued=int(data.get("requeued") or 0),
    ).model_dump(mode="json")


# ── 3. GET /config ────────────────────────────────────────────────────────────


@router.get("/config", response_model=None)
def config(
    response: Response,
    worker: AgentWorker = Depends(require_agent_worker_or_pending),
    if_none_match: str = Header(default=""),
) -> Any:
    """Everything the PC needs to run, pulled live: kill switch, its own
    slots/drain/types, per-type enablement, concurrency, step and time caps,
    host narrowing, the hold time and the signed release. Provider, model,
    prices and keys are deliberately absent (BDR proxies every model call
    and snapshots them at claim), so changing them needs nothing on the PC.
    A pending worker gets {pending: true}. ETag is the config_version."""
    if worker.status == "pending":
        return protocol.PendingResponse(pending=True).model_dump(mode="json")
    settings_row = _settings_row()
    version = int(settings_row.get("config_version") or 1)
    etag = str(version)
    if if_none_match.strip().strip('"') == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED,
                        headers={"ETag": etag, "Cache-Control": "no-store"})
    response.headers["ETag"] = etag
    sb = _sb()
    wrow = (
        sb.table("browser_agent_workers").select("status, max_slots, draining, allowed_types")
        .eq("id", worker.id).limit(1).execute()
    ).data or [{}]
    types = sb.table("browser_agent_types").select(
        "key, enabled, identity, max_concurrency, max_steps, max_active_seconds, host_narrowing"
    ).execute().data or []
    release = _desired_release(settings_row)
    return protocol.ConfigResponse(
        config_version=version,
        enabled=bool(settings_row.get("enabled")),
        worker=protocol.WorkerConfig(
            status=wrow[0].get("status") or worker.status,
            max_slots=int(wrow[0].get("max_slots") or worker.max_slots),
            draining=bool(wrow[0].get("draining")),
            allowed_types=list(wrow[0].get("allowed_types") or []),
        ),
        types={
            t["key"]: protocol.TypeConfig(
                enabled=bool(t.get("enabled")) and registry.has_type(t["key"]),
                identity=t.get("identity"),
                max_concurrency=int(t.get("max_concurrency") or 0),
                max_steps=int(t.get("max_steps") or 0),
                max_active_seconds=int(t.get("max_active_seconds") or protocol.MAX_ACTIVE_SECONDS_DEFAULT),
                host_narrowing=t.get("host_narrowing"),
            )
            for t in types
        },
        help_hold_seconds=int(settings_row.get("help_hold_seconds") or 900),
        release=protocol.ReleaseInfo(
            version=release["version"], sha256=release["sha256"], min_version=release["min_version"],
            manifest=release["manifest"], signature=release["signature"],
        ) if release else None,
    ).model_dump(mode="json")


# ── 4. POST /heartbeat ────────────────────────────────────────────────────────


@router.post("/heartbeat")
def heartbeat(
    payload: Any = Body(default=None),
    worker: AgentWorker = Depends(require_agent_worker),
    x_agent_boot: str = Header(default=""),
) -> dict[str, Any]:
    """Every 15 s from the worker's own thread. Renews each listed lease by
    (owner, epoch, boot), even an expired one nobody reclaimed; reports
    lost leases, cancels, orphans (leases this boot holds but did not list),
    help updates, drain and the config_version to refetch on. Never bumps
    config_version itself."""
    body = _parse_body(protocol.HeartbeatRequest, payload)
    _header_boot_matches(x_agent_boot, body.boot_id)
    jobs = [
        {"job_id": str(j.job_id), "epoch": j.epoch, "active_seconds_delta": j.active_seconds_delta}
        for j in body.jobs
    ]
    data = rpc("browser_agent_heartbeat", {
        "p_worker": worker.id, "p_boot": str(body.boot_id), "p_jobs": jobs, "p_ip": worker.ip,
        "p_lease_seconds": get_settings().browser_agent_lease_seconds,
    }) or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    resp = protocol.HeartbeatResponse.model_validate(data)
    resp.controls = []      # live-view seam: always empty in v1
    return resp.model_dump(mode="json")


# ── 5. POST /claim ──────────────────────────────────────────────────────────


@router.post("/claim")
def claim(
    payload: Any = Body(default=None),
    worker: AgentWorker = Depends(require_agent_worker),
    x_agent_boot: str = Header(default=""),
) -> dict[str, Any]:
    """Claim up to `capacity` jobs per type. The RPC enforces the boot,
    the kill switch, drain, allowed and advertised types, enablement and
    each type's global max_concurrency; BDR first trims the capacity to the
    worker's free slots. A job prepare() closes is not returned."""
    body = _parse_body(protocol.ClaimRequest, payload)
    _header_boot_matches(x_agent_boot, body.boot_id)
    jobs = queue.claim(worker.id, str(body.boot_id), dict(body.capacity), max_slots=worker.max_slots)
    return protocol.ClaimResponse(jobs=jobs).model_dump(mode="json")


# ── 6. POST /jobs/{job_id}/steps ──────────────────────────────────────────────


def _scrub(value: Any) -> Any:
    """BDR's second redaction pass over worker step payloads: every URL
    inside a string loses userinfo, query and fragment."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def step_row(step: protocol.StepIn) -> dict[str, Any]:
    """One worker step as the append RPC takes it. payload_sha256 hashes the
    canonical args||result as received (before the second scrub and the
    size cap), so the ledger can prove what the worker sent."""
    args, result = step.args, step.result
    payload_sha = hashlib.sha256(_canonical(args) + b"||" + _canonical(result)).hexdigest()
    args, result = _scrub(args), _scrub(result)
    size = len(_canonical(args)) + len(_canonical(result))
    if size > protocol.STEP_PAYLOAD_MAX_BYTES:
        args = {"truncated": True, "bytes": size} if args is not None else None
        result = {"truncated": True, "bytes": size} if result is not None else None
    return {
        "idem_key": step.idem_key,
        "kind": step.kind,
        "actor": step.actor,
        "summary": redact_text(step.summary)[: protocol.STEP_SUMMARY_MAX],
        "tool_name": step.tool_name,
        "args": args,
        "result": result,
        "payload_sha256": payload_sha,
        "url_before": redact_url(step.url_before)[: protocol.URL_MAX] if step.url_before else None,
        "url_after": redact_url(step.url_after)[: protocol.URL_MAX] if step.url_after else None,
        "screenshot_artifact_id": str(step.screenshot_artifact_id) if step.screenshot_artifact_id else None,
        "turn_no": step.turn_no,
        "ok": step.ok,
        "duration_ms": step.duration_ms,
    }


def _check_job_artifacts(job_id: str, artifact_ids: set[str]) -> None:
    """IDOR: every artifact a worker cites must belong to this job."""
    if not artifact_ids:
        return
    rows = (
        _sb().table("browser_agent_artifacts").select("id")
        .eq("job_id", job_id).in_("id", sorted(artifact_ids)).execute()
    ).data or []
    if {str(r["id"]) for r in rows} != artifact_ids:
        raise agent_error(ErrorCode.IDOR, "artifact does not belong to this job")


def _bump_step_count(job_id: str, appended: int) -> None:
    """jobs.step_count += appended (compare-and-set; a lost race retries)."""
    if appended <= 0:
        return
    sb = _sb()
    for _ in range(3):
        rows = sb.table(queue.JOBS).select("step_count").eq("id", job_id).limit(1).execute().data or []
        if not rows:
            return
        current = int(rows[0].get("step_count") or 0)
        if (sb.table(queue.JOBS).update({"step_count": current + appended})
                .eq("id", job_id).eq("step_count", current).execute()).data:
            return


@router.post("/jobs/{job_id}/steps")
def append_steps(payload: Any = Body(default=None), lease: AgentLease = Depends(require_agent_lease)) -> dict[str, Any]:
    """Append up to 200 worker steps. Idempotent on (job_id, idem_key): a
    resend reports duplicates. Steps for an epoch this worker held earlier
    (proved by its claim step) land as late=true with no state effect."""
    body = _parse_body(protocol.AppendStepsRequest, payload)
    _check_job_artifacts(
        lease.job_id, {str(s.screenshot_artifact_id) for s in body.steps if s.screenshot_artifact_id}
    )
    rows = [step_row(s) for s in body.steps]
    data = rpc("browser_agent_append_steps", {**lease.params(), "p_rows": rows}) or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    resp = protocol.AppendStepsResponse.model_validate(
        {"appended": data.get("appended", 0), "duplicates": data.get("duplicates", 0), "late": data.get("late", 0)}
    )
    _bump_step_count(lease.job_id, resp.appended)
    return resp.model_dump(mode="json")


# ── 7. PUT /jobs/{job_id}/checkpoint ─────────────────────────────────────────


@router.put("/jobs/{job_id}/checkpoint", status_code=status.HTTP_204_NO_CONTENT)
def put_checkpoint(payload: Any = Body(default=None), lease: AgentLease = Depends(require_agent_lease)) -> None:
    """Monotonic: applied only when seq is above the stored one (a stale
    resend is a quiet no-op). The data must fit the type's checkpoint shape."""
    body = _parse_body(protocol.CheckpointRequest, payload)
    rows = _sb().table(queue.JOBS).select("agent_type").eq("id", lease.job_id).limit(1).execute().data or []
    if not rows:
        raise agent_error(ErrorCode.LEASE_LOST)
    try:
        registry.get_type(rows[0]["agent_type"]).validate_checkpoint(body.data)
    except registry.UnknownAgentType:
        raise agent_error(ErrorCode.INVALID_REQUEST, "unknown agent type") from None
    except ValueError:
        raise agent_error(ErrorCode.INVALID_REQUEST, "checkpoint does not fit the type") from None
    rpc("browser_agent_checkpoint", {**lease.params(), "p_seq": body.seq, "p_data": body.data})


# ── 8. POST /jobs/{job_id}/artifacts (multipart) ──────────────────────────────


@router.post("/jobs/{job_id}/artifacts")
def upload_artifact(
    lease: AgentLease = Depends(require_agent_lease),
    kind: str = Form(...),
    sha256: str = Form(...),
    filename: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Store one screenshot, RFI envelope or page snapshot for this job.
    Fenced (running or waiting_help at this epoch), capped per kind,
    sha256 recomputed, content sniffed; idempotent on (job, kind, sha256)."""
    form = _parse_body(protocol.ArtifactUploadForm, {"kind": kind, "sha256": sha256, "filename": filename})
    rpc("browser_agent_fence", {**lease.params(), "p_statuses": ["running", "waiting_help"]})
    try:
        checked = artifact_store.check_upload(form.kind, form.sha256, form.filename, file.file)
    except artifact_store.ArtifactTooLarge as exc:
        raise agent_error(ErrorCode.ARTIFACT_INVALID, str(exc)) from None
    except artifact_store.ArtifactRejected as exc:
        raise agent_error(ErrorCode.ARTIFACT_INVALID, str(exc)) from None
    artifact_id, existed = artifact_store.store_worker_artifact(lease.job_id, lease.worker.id, checked)
    return protocol.ArtifactUploadResponse(artifact_id=artifact_id, existed=existed).model_dump(mode="json")


# ── 14. POST /jobs/{job_id}/finish ────────────────────────────────────────────


@router.post("/jobs/{job_id}/finish", status_code=status.HTTP_204_NO_CONTENT)
def finish(payload: Any = Body(default=None), lease: AgentLease = Depends(require_agent_lease)) -> None:
    """Terminal write from the worker. The outcome must be one this type
    may report (rfi_from_email: nothing_to_do only, a capture goes through
    /rfi-capture; canary: self_test_ok). The same terminal status twice is a
    no-op. A failure bells IT Admin once."""
    body = _parse_body(protocol.FinishRequest, payload)
    rows = _sb().table(queue.JOBS).select("id, agent_type, project_id").eq("id", lease.job_id).limit(1).execute().data or []
    if not rows:
        raise agent_error(ErrorCode.LEASE_LOST)
    job = rows[0]
    try:
        spec = registry.get_type(job["agent_type"])
    except registry.UnknownAgentType:
        raise agent_error(ErrorCode.INVALID_REQUEST, "unknown agent type") from None
    if body.status == "succeeded" and body.outcome not in spec.succeed_outcomes:
        raise agent_error(ErrorCode.INVALID_REQUEST, f"outcome {body.outcome} is not valid for {spec.key}")
    error = redact_text(body.error)[: protocol.ERROR_TEXT_MAX] if body.error else None
    changed = rpc("browser_agent_finish", {
        **lease.params(), "p_status": body.status, "p_outcome": body.outcome,
        "p_error_kind": body.error_kind, "p_error": error,
        "p_result": {"summary": redact_text(body.summary)[: protocol.FINISH_SUMMARY_MAX]},
    })
    if body.status == "failed" and changed:
        queue.notify_job_failed(job, body.error_kind or "other", redact_text(body.summary)[:200] or "see the job")


# ── 15. POST /jobs/{job_id}/requeue, POST /jobs/{job_id}/park ────────────────


@router.post("/jobs/{job_id}/requeue", status_code=status.HTTP_204_NO_CONTENT)
def requeue(payload: Any = Body(default=None), lease: AgentLease = Depends(require_agent_lease)) -> None:
    """Give the job back. shutdown/drain (only with service_stop or a
    draining worker), login_spacing, provider_outage and interrupted are
    uncharged; crash is charged and fails crashed_repeatedly at max_attempts.
    Interruptions count toward max_interruptions. The epoch moves on."""
    body = _parse_body(protocol.RequeueRequest, payload)
    retry_at = body.retry_at
    if retry_at is not None:
        retry_at = retry_at if retry_at.tzinfo else retry_at.replace(tzinfo=UTC)
        retry_at = min(retry_at, _now() + timedelta(hours=24))   # a worker cannot park a job for days
    outcome = rpc("browser_agent_requeue", {
        **lease.params(), "p_reason": body.reason,
        "p_retry_at": queue.iso(retry_at) if retry_at else None,
        "p_service_stop": body.service_stop,
    })
    if outcome == "failed":
        rows = _sb().table(queue.JOBS).select("id, project_id, error_kind").eq("id", lease.job_id).limit(1).execute().data or []
        job = rows[0] if rows else {"id": lease.job_id}
        queue.notify_job_failed(job, job.get("error_kind") or "crashed_repeatedly",
                                f"requeued as {body.reason} past its limit")


@router.post("/jobs/{job_id}/park", status_code=status.HTTP_204_NO_CONTENT)
def park(lease: AgentLease = Depends(require_agent_lease)) -> None:
    """The help hold ran out: release the lease, keep waiting_help. A
    helper's answer requeues the job (help phase); an answer that arrived
    while the page was held but was never read requeues it right away."""
    rpc("browser_agent_park", lease.params())
    from app.services.browser_agents import help as help_flow

    try:
        help_flow.requeue_if_answered(lease.job_id)
    except Exception:  # noqa: BLE001 - the sweeper's answered-parked step catches it
        logger.exception("browser agents: requeue after park failed for job %s", lease.job_id)


# ── 16. GET /releases/{version} ───────────────────────────────────────────────


@router.get("/releases/{version}")
def release(version: str, worker: AgentWorker = Depends(require_agent_worker)) -> dict[str, Any]:
    """A signed release: the manifest, its Ed25519 signature and a 900 s
    signed download URL. The worker verifies the signature with its pinned
    key and the package sha256 before installing anything."""
    if not protocol.SEMVER_RE.match(version):
        raise agent_error(ErrorCode.INVALID_REQUEST, "version must be MAJOR.MINOR.PATCH")
    rows = (
        _sb().table("browser_agent_worker_releases").select("*").eq("version", version).limit(1).execute()
    ).data or []
    if not rows or not artifact_store.verify_release(rows[0], get_settings().browser_agent_release_pubkey):
        raise agent_error(ErrorCode.INVALID_REQUEST, "no such release")
    row = rows[0]
    return protocol.ReleaseResponse(
        manifest=row["manifest"], signature=row["signature"],
        url=artifact_store.signed_url(row["storage_path"]),
    ).model_dump(mode="json")


# ── GET /installer.ps1 (no auth) ──────────────────────────────────────────────

INSTALLER_TEMPLATE_PATH = "installer/bdr-install-template.ps1"
INSTALLER_RELEASE_PLACEHOLDER = b"'__RELEASE_JSON_B64__'"


@router.get("/installer.ps1", response_class=Response)
def installer() -> Response:
    """The one-file Windows installer the Add worker command downloads
    (SPEC.md 8 "Install"). The template was rendered and uploaded on the
    owner's Mac by scripts/publish_agent_worker_release.py --installer-api
    (API base, release public key, launcher, WinSW XML); here only the
    desired release is filled in, with a fresh 900 s download link, so the
    file must be run within 15 minutes. Public on purpose: it carries
    nothing secret, and the owner checks the key fingerprint it prints
    against the password manager before anything changes on the PC."""
    release = _desired_release(_settings_row())
    if release is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No worker release is selected",
                            headers={"Cache-Control": "no-store"})
    try:
        template = artifact_store.download_object(INSTALLER_TEMPLATE_PATH)
    except Exception:  # noqa: BLE001 - no template uploaded yet
        logger.warning("browser agents: installer template missing")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The installer is not published",
                            headers={"Cache-Control": "no-store"}) from None
    if template.count(INSTALLER_RELEASE_PLACEHOLDER) != 1:
        logger.error("browser agents: installer template has no release placeholder")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The installer is not published",
                            headers={"Cache-Control": "no-store"})
    payload = {"manifest": release["manifest"], "signature": release["signature"],
               "url": artifact_store.signed_url(release["storage_path"])}
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode("utf-8"))
    body = template.replace(INSTALLER_RELEASE_PLACEHOLDER, b"'" + encoded + b"'")
    return Response(content=body, media_type="text/plain; charset=utf-8", headers={
        "Cache-Control": "no-store",
        "Content-Disposition": 'attachment; filename="bdr-install.ps1"',
    })


# ── GET /canary/page (static, no auth) ────────────────────────────────────────


@router.get("/canary/page", response_class=HTMLResponse)
def canary_page() -> HTMLResponse:
    """The canary's only allowed page. The worker's browser opens it
    without a token, so it is static, carries no data and runs no script."""
    return HTMLResponse(canary.canary_page_html(), headers=dict(canary.CANARY_PAGE_SECURITY_HEADERS))
