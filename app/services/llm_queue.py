"""Durable queue for the long-running AI jobs.

Replaces fire-and-forget FastAPI BackgroundTasks for BOQ extraction, the
general material estimate, and proposal scope lines. Routers insert an
llm_jobs row; a worker loop (started from the lifespan in every uvicorn
worker) claims due jobs atomically and runs them. What that buys over
BackgroundTasks:

- Nothing is lost on a deploy or crash: a running job's lease expires and
  the sweep requeues it instead of stranding the domain row at "running"
  forever (the old 15-minute lazy stale-fail is now only a backstop for
  queue-disabled mode).
- Transient failures (model server down / timeout / overloaded / provider
  5xx / unusable output) retry automatically on the schedule in
  LLM_QUEUE_RETRY_DELAYS. Permanent failures (scanned PDF, missing config,
  out of API tokens) fail immediately with a specific user-facing message.
- Load is queued instead of dropped: the worker runs a bounded number of
  jobs at once and everything else waits its turn, visible to the user as
  "queued (position N)" via poll_info() joined into the poll endpoints.

Multi-worker safety: claims go through the claim_llm_jobs RPC (FOR UPDATE
SKIP LOCKED), so the two prod uvicorn workers never claim the same job.
Attempts are counted at claim time, so a worker that dies mid-run has
still spent that attempt; the lease-expiry sweep requeues or terminally
fails the job without guessing what happened.

The domain status rows keep their existing vocabularies: 'pending' covers
queued and waiting-for-retry, 'running' an attempt in flight. The FE keeps
polling exactly as before and gets the queue detail alongside.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.core.config import Settings, get_settings
from app.core.supabase_client import get_supabase
from app.services import llm_errors, llm_gate

logger = logging.getLogger(__name__)

# Per-process worker identity: leases are fenced on this token.
_WORKER_TOKEN = uuid.uuid4().hex

JOB_BOQ = "boq_extraction"
JOB_GENERAL_MATERIAL = "general_material"
JOB_PROPOSAL = "proposal_lines"

_FEATURE_BY_TYPE = {
    JOB_BOQ: "boq",
    JOB_GENERAL_MATERIAL: "estimate",
    JOB_PROPOSAL: "proposal",
}

_ACTIVE_STATUSES = ("queued", "running")
_ERROR_MAX_CHARS = 500

_INTERRUPTED_MESSAGE = (
    "The run was interrupted (the server restarted or was redeployed). "
    "It has been queued to run again automatically."
)
_INTERRUPTED_FINAL_MESSAGE = (
    "The run was interrupted repeatedly (server restarts). Run it again; "
    "if this keeps happening, contact your IT Director."
)
_CANCELED_MESSAGE = "Canceled from the AI monitor page."


class JobAlreadyActive(RuntimeError):
    """An active (queued/running) job already exists for this target."""

    def __init__(self, job: dict):
        super().__init__("A job is already queued or running for this item.")
        self.job = job


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _JobSpec:
    feature: str
    run: Callable[[dict], None]  # raises on failure; owns running/done marks
    mark: Callable[[str, dict], None]  # patch the domain status row
    current_status: Callable[[str], str | None]  # domain row's status, None if gone


def _row_status(table: str, key_column: str, target_id: str) -> str | None:
    rows = (
        get_supabase()
        .table(table)
        .select("status")
        .eq(key_column, target_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0].get("status") if rows else None


def _spec(job_type: str) -> _JobSpec:
    """Runner + domain-row adapter per job type. Imports are lazy so this
    module stays importable from the routers without dragging every service
    (and their SDK imports) in at boot."""
    if job_type == JOB_BOQ:
        from app.services import boq_extraction as m

        return _JobSpec(
            "boq",
            lambda p: m.execute(p["analysis_id"]),
            lambda target, fields: m._mark(target, **fields),
            lambda target: _row_status("boq_analyses", "id", target),
        )
    if job_type == JOB_GENERAL_MATERIAL:
        from app.services import general_material as m

        return _JobSpec(
            "estimate",
            lambda p: m.execute(p["project_id"]),
            lambda target, fields: m._save(target, **fields),
            lambda target: _row_status("general_material_estimates", "project_id", target),
        )
    if job_type == JOB_PROPOSAL:
        from app.services import proposal_scope as m

        return _JobSpec(
            "proposal",
            lambda p: m.execute(p["draft_id"]),
            lambda target, fields: m._mark(target, **fields),
            lambda target: _row_status("proposal_drafts", "id", target),
        )
    raise KeyError(f"Unknown llm job type: {job_type}")


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) == "23505" or "23505" in text or "duplicate key" in text


# ── Enqueue / lookup ─────────────────────────────────────────────────────


def enqueue(
    job_type: str,
    *,
    target_id: str,
    project_id: str | None = None,
    payload: dict | None = None,
    created_by: str | None = None,
    priority: int = 100,
    settings: Settings | None = None,
    raise_on_active: bool = False,
) -> dict:
    """Queue a job. If an active job already exists for the same target the
    existing job is returned instead (double-clicks and racing tabs collapse
    onto one run; the partial unique index is the authority). Callers that
    need to KNOW they collapsed (to warn the user) pass raise_on_active=True
    and catch JobAlreadyActive, which carries the existing job."""
    s = settings or get_settings()
    sb = get_supabase()
    row = {
        "job_type": job_type,
        "feature": _FEATURE_BY_TYPE[job_type],
        "target_id": str(target_id),
        "project_id": project_id,
        "payload": payload or {},
        "created_by": created_by,
        "priority": priority,
        "max_attempts": 1 + len(s.llm_retry_delay_list),
    }
    try:
        resp = sb.table("llm_jobs").insert(row).execute()
        return resp.data[0]
    except Exception as exc:  # noqa: BLE001 - only the dup-key race is expected
        if not _is_unique_violation(exc):
            raise
        existing = active_job(job_type, target_id)
        if existing:
            if raise_on_active:
                raise JobAlreadyActive(existing) from exc
            return existing
        # The conflicting job went terminal between our insert and the lookup;
        # the partial-index slot is free again, so try once more.
        try:
            resp = sb.table("llm_jobs").insert(row).execute()
            return resp.data[0]
        except Exception as exc2:  # noqa: BLE001
            if _is_unique_violation(exc2):
                existing = active_job(job_type, target_id)
                if existing:
                    if raise_on_active:
                        raise JobAlreadyActive(existing) from exc2
                    return existing
            raise


def active_job(job_type: str, target_id: str) -> dict | None:
    """The queued/running job for this target, if any."""
    resp = (
        get_supabase()
        .table("llm_jobs")
        .select("*")
        .eq("job_type", job_type)
        .eq("target_id", str(target_id))
        .in_("status", list(_ACTIVE_STATUSES))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def latest_job(job_type: str, target_id: str) -> dict | None:
    resp = (
        get_supabase()
        .table("llm_jobs")
        .select("*")
        .eq("job_type", job_type)
        .eq("target_id", str(target_id))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def queue_position(job: dict) -> int | None:
    """1-based position among queued jobs; None once running/terminal."""
    if job.get("status") != "queued":
        return None
    resp = (
        get_supabase()
        .table("llm_jobs")
        .select("id, priority, created_at")
        .eq("status", "queued")
        .order("priority")
        .order("created_at")
        .limit(200)
        .execute()
    )
    for idx, row in enumerate(resp.data or []):
        if row["id"] == job["id"]:
            return idx + 1
    return None


def poll_info(job_type: str, target_id: str) -> dict | None:
    """Queue detail joined into the FE poll responses. None when this target
    has never been queued (legacy rows, queue-disabled mode)."""
    job = latest_job(job_type, target_id)
    if not job:
        return None
    queued = job["status"] == "queued"
    return {
        "state": job["status"],
        "attempts": job.get("attempts", 0),
        "max_attempts": job.get("max_attempts", 1),
        "position": queue_position(job) if queued else None,
        "next_attempt_at": job.get("next_attempt_at") if queued else None,
        "retrying": queued and (job.get("attempts") or 0) > 0,
        "error_kind": job.get("error_kind"),
        "last_error": job.get("last_error"),
    }


# ── Worker ───────────────────────────────────────────────────────────────


def _claim(s: Settings, max_jobs: int) -> list[dict]:
    resp = get_supabase().rpc(
        "claim_llm_jobs",
        {
            "worker_id": _WORKER_TOKEN,
            "lease_seconds": s.llm_queue_lease_seconds,
            "max_jobs": max_jobs,
        },
    ).execute()
    return resp.data or []


def _cas_job(sb: Any, job: dict, fields: dict) -> bool:
    """Update the job only if it is still in the state we think it is in.

    The fence is (id, status=running, claimed_by, attempts). attempts matters:
    claimed_by alone is per-PROCESS, so a stale attempt whose lease expired
    could otherwise clobber the SAME process's fresh reclaim of the job. The
    claim RPC increments attempts on every claim, making (claimed_by,
    attempts) unique per claim generation. Lost the race -> False."""
    resp = (
        sb.table("llm_jobs")
        .update(fields)
        .eq("id", job["id"])
        .eq("status", "running")
        .eq("claimed_by", job.get("claimed_by") or _WORKER_TOKEN)
        .eq("attempts", job.get("attempts") or 0)
        .execute()
    )
    return bool(resp.data)


def _mark_domain(spec: _JobSpec, job: dict, fields: dict) -> None:
    try:
        spec.mark(job["target_id"], fields)
    except Exception:  # noqa: BLE001 - a vanished domain row must not wedge the queue
        logger.exception(
            "llm queue: domain mark failed for %s %s", job["job_type"], job["target_id"]
        )


def _execute(job: dict) -> None:
    """Run one claimed job to a terminal or requeued state. Never raises."""
    s = get_settings()
    spec = _spec(job["job_type"])
    sb = get_supabase()
    try:
        with llm_gate.tier(llm_gate.TIER_JOB, job_id=job["id"]):
            spec.run(job.get("payload") or {})
    except Exception as exc:  # noqa: BLE001 - classified below
        _handle_failure(sb, job, spec, exc, s)
        return
    if not _cas_job(
        sb,
        job,
        {
            "status": "succeeded",
            "finished_at": _now().isoformat(),
            "lease_expires_at": None,
            "claimed_by": None,
        },
    ):
        # The lease expired mid-run and the sweep requeued the job. The work
        # itself completed (the domain row says done); the requeued run will
        # re-do it idempotently. Rare: lease >> real runtimes.
        logger.warning("llm queue: lost lease on %s before completion", job["id"])


def _handle_failure(
    sb: Any, job: dict, spec: _JobSpec, exc: Exception, s: Settings
) -> None:
    from app.services import llm

    kind = llm_errors.classify(exc)
    model = llm.active_model(spec.feature, s)
    message = llm_errors.user_message(exc, model)[:_ERROR_MAX_CHARS]
    attempt = job.get("attempts") or 1
    delays = s.llm_retry_delay_list

    if llm_errors.is_transient_kind(kind) and attempt <= len(delays):
        delay = delays[attempt - 1]
        requeued = _cas_job(
            sb,
            job,
            {
                "status": "queued",
                "next_attempt_at": (_now() + timedelta(seconds=delay)).isoformat(),
                "claimed_by": None,
                "lease_expires_at": None,
                "error_kind": kind,
                "last_error": message,
            },
        )
        if requeued:
            # Domain row back to pending: the FE keeps polling through the
            # retry window instead of showing a terminal failure.
            _mark_domain(spec, job, {"status": "pending", "error": None})
            logger.warning(
                "llm job %s (%s) attempt %s failed (%s), retrying in %ss",
                job["id"],
                job["job_type"],
                attempt,
                kind,
                delay,
                exc_info=exc,
            )
        return

    total = f" (failed after {attempt} attempts)" if attempt > 1 else ""
    failed = _cas_job(
        sb,
        job,
        {
            "status": "failed",
            "finished_at": _now().isoformat(),
            "lease_expires_at": None,
            "claimed_by": None,
            "error_kind": kind,
            "last_error": message,
        },
    )
    if failed:
        _mark_domain(
            spec, job, {"status": "failed", "error": (message + total)[:_ERROR_MAX_CHARS]}
        )
        logger.error(
            "llm job %s (%s) failed terminally after %s attempt(s): %s",
            job["id"],
            job["job_type"],
            attempt,
            kind,
            exc_info=exc,
        )
    else:
        # A superseding claim owns the job (our lease expired mid-run and the
        # sweep requeued it). Its outcome governs the domain row, not ours.
        logger.warning(
            "llm queue: job %s lost its lease before terminal fail; leaving domain row alone",
            job["id"],
        )


# ── Sweep: lease expiry + retention ──────────────────────────────────────

_last_prune: float = 0.0
_PRUNE_EVERY_SECONDS = 3600.0


def _sweep(s: Settings) -> None:
    """Requeue (or terminally fail) running jobs whose lease expired, then
    prune old ledger rows once an hour. Runs on every worker; all writes are
    conditional so double-execution is harmless."""
    sb = get_supabase()
    now_iso = _now().isoformat()
    expired = (
        sb.table("llm_jobs")
        .select("*")
        .eq("status", "running")
        .lt("lease_expires_at", now_iso)
        .limit(50)
        .execute()
    ).data or []
    for job in expired:
        spec = _spec(job["job_type"])
        attempt = job.get("attempts") or 1
        if attempt < (job.get("max_attempts") or 1):
            if _cas_job(
                sb,
                job,
                {
                    "status": "queued",
                    "next_attempt_at": now_iso,
                    "claimed_by": None,
                    "lease_expires_at": None,
                    "error_kind": "interrupted",
                    "last_error": _INTERRUPTED_MESSAGE,
                },
            ):
                _mark_domain(spec, job, {"status": "pending", "error": None})
                logger.warning("llm queue: requeued interrupted job %s", job["id"])
        else:
            if _cas_job(
                sb,
                job,
                {
                    "status": "failed",
                    "finished_at": now_iso,
                    "claimed_by": None,
                    "lease_expires_at": None,
                    "error_kind": "interrupted",
                    "last_error": _INTERRUPTED_FINAL_MESSAGE,
                },
            ):
                _mark_domain(
                    spec, job, {"status": "failed", "error": _INTERRUPTED_FINAL_MESSAGE}
                )
                logger.error("llm queue: job %s interrupted too many times", job["id"])

    global _last_prune
    if time.monotonic() - _last_prune >= _PRUNE_EVERY_SECONDS:
        _last_prune = time.monotonic()
        cutoff = (_now() - timedelta(days=s.llm_call_log_retention_days)).isoformat()
        sb.table("llm_call_log").delete().lt("created_at", cutoff).execute()
        sb.table("llm_jobs").delete().lt("created_at", cutoff).in_(
            "status", ["succeeded", "failed", "canceled"]
        ).execute()


# ── Monitor actions ──────────────────────────────────────────────────────


def requeue_terminal(job: dict, created_by: str | None) -> dict:
    """Fresh job for a failed/canceled one (AI monitor "Retry"). The old row
    stays as history; the domain row goes back to pending so the FE polls.

    Guarded against stale retries: if the target has since completed (a later
    run or a manual override produced a result), or a newer job supersedes
    this one, retrying would clobber current data and is refused."""
    if job.get("status") not in ("failed", "canceled"):
        raise ValueError("Only failed or canceled jobs can be retried.")
    spec = _spec(job["job_type"])
    newest = latest_job(job["job_type"], job["target_id"])
    if newest and newest.get("id") != job.get("id"):
        raise ValueError(
            "A newer run exists for this item; retry that one instead."
        )
    domain_status = spec.current_status(job["target_id"])
    if domain_status in ("done", "not_found"):
        raise ValueError(
            "This item has since completed. Re-run it from the project page instead."
        )
    fresh = enqueue(
        job["job_type"],
        target_id=job["target_id"],
        project_id=job.get("project_id"),
        payload=job.get("payload") or {},
        created_by=created_by,
        priority=job.get("priority") or 100,
    )
    if fresh["id"] != job["id"]:
        _mark_domain(spec, job, {"status": "pending", "error": None})
    return fresh


def cancel(job_id: str) -> dict | None:
    """Cancel a QUEUED job (AI monitor). Running jobs cannot be canceled
    safely (the model call is already in flight), EXCEPT zombies whose lease
    already expired: their worker is gone, so the dev can clear them instead
    of waiting for the sweep. Returns the updated row, or None when the job
    was not cancelable."""
    sb = get_supabase()
    fields = {
        "status": "canceled",
        "finished_at": _now().isoformat(),
        "claimed_by": None,
        "lease_expires_at": None,
        "last_error": _CANCELED_MESSAGE,
    }
    resp = (
        sb.table("llm_jobs").update(fields).eq("id", job_id).eq("status", "queued").execute()
    )
    rows = resp.data or []
    if not rows:
        resp = (
            sb.table("llm_jobs")
            .update(fields)
            .eq("id", job_id)
            .eq("status", "running")
            .lt("lease_expires_at", _now().isoformat())
            .execute()
        )
        rows = resp.data or []
    if not rows:
        return None
    job = rows[0]
    _mark_domain(
        _spec(job["job_type"]), job, {"status": "failed", "error": _CANCELED_MESSAGE}
    )
    return job


_DISABLED_MESSAGE = "The AI queue was disabled. Run it again."


def release_stranded_for_disabled_mode() -> None:
    """One-shot cleanup when LLM_QUEUE_ENABLED is turned off: with no worker
    loop, queued/running jobs would sit forever and their pending/running
    domain rows would block new starts. Fail them all so users can re-run
    inline. Safe to run from every worker (each update is conditional)."""
    sb = get_supabase()
    stranded = (
        sb.table("llm_jobs")
        .select("*")
        .in_("status", list(_ACTIVE_STATUSES))
        .limit(500)
        .execute()
    ).data or []
    for job in stranded:
        resp = (
            sb.table("llm_jobs")
            .update(
                {
                    "status": "failed",
                    "finished_at": _now().isoformat(),
                    "claimed_by": None,
                    "lease_expires_at": None,
                    "error_kind": "interrupted",
                    "last_error": _DISABLED_MESSAGE,
                }
            )
            .eq("id", job["id"])
            .in_("status", list(_ACTIVE_STATUSES))
            .execute()
        )
        if resp.data:
            _mark_domain(
                _spec(job["job_type"]), job, {"status": "failed", "error": _DISABLED_MESSAGE}
            )
            logger.warning("llm queue disabled: released stranded job %s", job["id"])


# ── Loop ─────────────────────────────────────────────────────────────────


async def worker_loop() -> None:
    """Claim and run due jobs forever. Runs in every uvicorn worker; the
    claim RPC keeps the workers disjoint, so more workers = more throughput.
    Mirrors the polling_loop convention (sync work via asyncio.to_thread,
    a tick can never kill the loop)."""
    logger.info("llm queue worker started (token %s)", _WORKER_TOKEN[:8])
    running: set[asyncio.Task] = set()
    while True:
        s = get_settings()
        try:
            await asyncio.to_thread(_sweep, s)
            capacity = s.llm_queue_worker_concurrency - len(running)
            if capacity > 0:
                for job in await asyncio.to_thread(_claim, s, capacity):
                    task = asyncio.create_task(asyncio.to_thread(_execute, job))
                    running.add(task)
                    task.add_done_callback(running.discard)
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("llm queue tick failed")
        await asyncio.sleep(max(0.5, float(s.llm_queue_poll_interval_seconds)))
