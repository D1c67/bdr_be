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

The RFP Ingestion sandbox (job type 'rfp_ingest', one row per run) rides the
same table and the same lease/retry machinery but is NOT an LLM job: it
spawns a subprocess per file for up to hours. Three things keep it from
starving or blocking the AI work:

- The worker claims in TWO passes per tick, each with its own capacity: the
  LLM job types against llm_queue_worker_concurrency, then ['rfp_ingest']
  against rfp_ingest_sandbox_concurrency minus the sandbox runs already in
  flight here. The claim RPC's job_types filter is what keeps the passes
  disjoint, so a sandbox run never occupies an LLM slot and vice versa.
- A running job is exposed to its runner through the `current_job`
  contextvar so the sandbox monitor loop can renew_lease() every 30 s (a
  lost renewal means the sweep already handed the job to another worker:
  the runner kills its child and stops) and requeue_self() on shutdown so
  the next worker resumes the run instead of waiting out the lease.
- Failures inside the sandbox runner arrive as app-authored exceptions that
  declare their own kind (llm_errors.declared_kind); _handle_failure uses
  the spec's model_label ("sandbox") instead of resolving an LLM route, and
  its whole body is guarded so an internal error there still reaches the
  terminal CAS. The spec also supplies its own error_message, so an exception
  the sandbox never declared is recorded with a sandbox-authored sentence
  rather than one about "the AI provider" and "the model".
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from app.core.config import Settings, get_settings
from app.core.supabase_client import get_supabase
from app.services import llm_errors, llm_gate

logger = logging.getLogger(__name__)

# Per-process worker identity: leases are fenced on this token.
_WORKER_TOKEN = uuid.uuid4().hex

JOB_BOQ = "boq_extraction"
JOB_GENERAL_MATERIAL = "general_material"
JOB_PROPOSAL = "proposal_lines"
JOB_BID_SPLIT = "bid_split"
JOB_RFP_INGEST = "rfp_ingest"
JOB_RFP_HARVEST = "rfp_harvest"
JOB_PORTAL_SCAN = "rfp_portal_scan"
JOB_PORTAL_HARVEST = "rfp_portal_harvest"
JOB_RFP_CREATE_FILES = "rfp_create_files"

# The job types that spend an LLM slot. The sandbox, harvest, portal and
# promotion types are deliberately not here: each family is claimed in its
# own pass with its own capacity (see worker_loop).
LLM_JOB_TYPES = (JOB_BOQ, JOB_GENERAL_MATERIAL, JOB_PROPOSAL, JOB_BID_SPLIT)
# The portal request stream (docs/RFP_NGEM_PORTAL.md 2.2): the scan and the
# portal harvest share the harvest capacity with the Procore harvest, one
# human-paced session per worker at a time.
PORTAL_JOB_TYPES = (JOB_PORTAL_SCAN, JOB_PORTAL_HARVEST)
HARVEST_JOB_TYPES = (JOB_RFP_HARVEST,) + PORTAL_JOB_TYPES
# The RFP creation slice's document promotion (docs/RFP_CREATE.md 5): a
# storage-streaming job that rides the third pass beside the harvests and
# counts against the same capacity (minutes of streaming per project).
CREATE_JOB_TYPES = (JOB_RFP_CREATE_FILES,)
THIRD_PASS_JOB_TYPES = HARVEST_JOB_TYPES + CREATE_JOB_TYPES
NON_LLM_JOB_TYPES = (JOB_RFP_INGEST,) + THIRD_PASS_JOB_TYPES

# Model label recorded for sandbox jobs in place of an LLM model name.
SANDBOX_MODEL_LABEL = "sandbox"

_FEATURE_BY_TYPE = {
    JOB_BOQ: "boq",
    JOB_GENERAL_MATERIAL: "estimate",
    JOB_PROPOSAL: "proposal",
    JOB_BID_SPLIT: "bid_split",
    JOB_RFP_INGEST: "rfp_ingest",
    JOB_RFP_HARVEST: "rfp_harvest",
    JOB_PORTAL_SCAN: "rfp_portal",
    JOB_PORTAL_HARVEST: "rfp_portal",
    JOB_RFP_CREATE_FILES: "rfp_create",
}

# The job whose spec.run is executing on this thread (None outside _execute,
# including the BackgroundTasks fallback, where renew_lease/requeue_self are
# no-ops). Set per _execute call; asyncio.to_thread copies the context, so
# concurrent jobs never see each other's row.
current_job: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "llm_queue_current_job", default=None
)

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
    # Label stored with failure messages. Defaults to the feature's active LLM
    # model (llm.active_model); non-LLM jobs supply their own so no route is
    # resolved for a feature llm.py has never heard of.
    model_label: Callable[[Settings], str] | None = None
    # (exception, model_label) -> the sentence stored in last_error and in the
    # domain row's error column. Defaults to llm_errors.user_message, which
    # talks about the AI provider and the model; a non-LLM job supplies its own
    # so an exception it never declared cannot describe a sandbox run as an
    # LLM call (or leak developer-facing text from a raw ValueError).
    error_message: Callable[[Exception, str], str] | None = None

    def __post_init__(self) -> None:
        if self.error_message is None:
            object.__setattr__(self, "error_message", llm_errors.user_message)
        if self.model_label is None:
            feature = self.feature

            def _active_model(s: Settings) -> str:
                from app.services import llm

                return llm.active_model(feature, s)

            object.__setattr__(self, "model_label", _active_model)


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
    if job_type == JOB_BID_SPLIT:
        from app.services import bid_split as m

        return _JobSpec(
            "bid_split",
            # forced_kind rides in the payload (a reprocess pins the user's
            # verdict), so lease requeues keep forcing the same way.
            lambda p: m.execute(p["file_id"], forced_kind=p.get("forced_kind")),
            lambda target, fields: m._mark(target, **fields),
            lambda target: _row_status("bid_split_files", "id", target),
        )
    if job_type == JOB_RFP_INGEST:
        from app.sandbox import protocol
        from app.services import rfp_ingest as m

        def _rfp_message(exc: Exception, _label: str) -> str:
            """Sandbox-authored text for every sandbox failure.

            The runner's two exception classes carry sentences the service
            wrote for users, so those pass through. Anything else escaping
            execute (a postgrest APIError from a mark, an httpx timeout, a
            raw ValueError) would otherwise be described by llm_errors as a
            problem with "the AI provider" and "the model" for a run that
            never touched an LLM, or stored verbatim as developer-facing
            text. Same mapping as rfp_ingest.run_in_background, so the queue
            path and the BackgroundTasks fallback record the same thing.
            """
            if isinstance(exc, (m.RfpIngestTransient, m.RfpIngestPermanent)):
                return str(exc)
            return protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED]

        return _JobSpec(
            "rfp_ingest",
            # One job per RUN; the payload carries only the run id.
            lambda p: m.execute(p["run_id"]),
            # The queue only ever marks {"status": pending|failed, "error"}; the
            # service's mark is a CAS that ignores marks a canceled or finished
            # run must not take.
            lambda target, fields: m.mark_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            # Maps every terminal run status to 'done' so the AI monitor's
            # retry refuses; /rfp-ingest/runs/{id}/retry is the only retry path.
            lambda target: m.current_status(target),
            model_label=lambda s: SANDBOX_MODEL_LABEL,
            error_message=_rfp_message,
        )
    if job_type == JOB_RFP_HARVEST:
        from app.services import rfp_harvest as m

        return _JobSpec(
            "rfp_harvest",
            # One job per EMAIL row; `force` refreshes a complete harvest.
            lambda p: m.execute(p["email_id"], force=bool(p.get("force"))),
            lambda target, fields: m.mark_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            lambda target: m.current_status(target),
            model_label=lambda s: m.MODEL_LABEL,
            error_message=m.error_message,
        )
    if job_type == JOB_PORTAL_SCAN:
        from app.services import rfp_portal_ingest as m

        return _JobSpec(
            "rfp_portal",
            # One job per RUN row (docs/RFP_NGEM_PORTAL.md 2.2).
            lambda p: m.execute_scan(p["run_id"]),
            lambda target, fields: m.mark_scan_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            # Every terminal run status maps to 'done' so the AI monitor's
            # retry refuses; Run now is the retry path.
            lambda target: m.scan_status(target),
            model_label=lambda s: m.MODEL_LABEL,
            error_message=m.error_message,
        )
    if job_type == JOB_PORTAL_HARVEST:
        from app.services import rfp_portal_ingest as m

        return _JobSpec(
            "rfp_portal",
            # One job per INVITATION row; `force` refreshes a complete harvest.
            lambda p: m.execute_harvest(p["invitation_id"], force=bool(p.get("force"))),
            lambda target, fields: m.mark_harvest_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            lambda target: m.current_status(target),
            model_label=lambda s: m.MODEL_LABEL,
            error_message=m.error_message,
        )
    if job_type == JOB_RFP_CREATE_FILES:
        from app.services import rfp_create_files as m

        return _JobSpec(
            "rfp_create",
            # One job per PROJECT (docs/RFP_CREATE.md 5): the harvest's
            # verified documents into the project's files. `harvest_id` names
            # the harvest to promote when the payload carries one (4.6's
            # recent-project link promotes ITS invitation's harvest into a
            # project whose record belongs to another's); without it the job
            # falls back to the record's harvest, as it always has.
            lambda p: m.execute(p["project_id"], harvest_id=p.get("harvest_id")),
            lambda target, fields: m.mark_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            # complete and failed both map to 'done' so the AI monitor's
            # retry refuses; "Retry documents" on the page is the retry path.
            lambda target: m.current_status(target),
            model_label=lambda s: m.MODEL_LABEL,
            error_message=m.error_message,
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


def _claim(s: Settings, max_jobs: int, job_types: list[str] | None = None) -> list[dict]:
    """Claim up to max_jobs due jobs through the claim_llm_jobs RPC. job_types
    narrows the claim to those types (the RPC's 4th parameter, migration
    0119); None claims any type, which only the tests and ad-hoc tooling use:
    the worker loop always names a pass."""
    params: dict[str, Any] = {
        "worker_id": _WORKER_TOKEN,
        "lease_seconds": s.llm_queue_lease_seconds,
        "max_jobs": max_jobs,
    }
    if job_types is not None:
        params["job_types"] = list(job_types)
    resp = get_supabase().rpc("claim_llm_jobs", params).execute()
    return resp.data or []


def _claim_tick(s: Settings, running_types: Iterable[str]) -> list[dict]:
    """One tick's claims, in two passes with independent capacities: the LLM
    job types against llm_queue_worker_concurrency, then the sandbox type
    against rfp_ingest_sandbox_concurrency. `running_types` is the job type of
    every job still executing in this process. The sandbox pass is skipped
    while the feature flag is off (a disabled feature must never spawn a
    child; its queued jobs wait for the flag or an operator cancel)."""
    types = list(running_types)
    claimed: list[dict] = []
    llm_capacity = s.llm_queue_worker_concurrency - sum(
        1 for jt in types if jt not in NON_LLM_JOB_TYPES
    )
    if llm_capacity > 0:
        claimed.extend(_claim(s, llm_capacity, list(LLM_JOB_TYPES)))
    if s.rfp_ingest_enabled:
        rfp_capacity = s.rfp_ingest_sandbox_concurrency - sum(
            1 for jt in types if jt == JOB_RFP_INGEST
        )
        if rfp_capacity > 0:
            claimed.extend(_claim(s, rfp_capacity, [JOB_RFP_INGEST]))
    # Third pass: the harvests (docs/RFP_HARVEST.md 2.2 and
    # docs/RFP_NGEM_PORTAL.md 2.2), one job per worker by default so each
    # platform sees one human-paced session at a time. The Procore harvest,
    # the two portal jobs and the document promotion (docs/RFP_CREATE.md 5)
    # share the capacity; each family is claimed only while its slice is on
    # (queued jobs wait for the flag or a cancel). The promotion needs only
    # the master switch: it streams from storage, never from a platform.
    harvest_types: list[str] = []
    if s.rfp_ingest_enabled and s.rfp_harvest_enabled:
        harvest_types.append(JOB_RFP_HARVEST)
    if s.rfp_portal_any_enabled:  # NGEM or BuildingConnected (one shared scan job type)
        harvest_types.extend(PORTAL_JOB_TYPES)
    if s.rfp_ingest_enabled:
        harvest_types.extend(CREATE_JOB_TYPES)
    if harvest_types:
        harvest_capacity = s.rfp_harvest_concurrency - sum(
            1 for jt in types if jt in THIRD_PASS_JOB_TYPES
        )
        if harvest_capacity > 0:
            claimed.extend(_claim(s, harvest_capacity, harvest_types))
    return claimed


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


def renew_lease(job: dict | None = None) -> bool:
    """Extend the lease of the job executing on this thread (or `job`) by
    another llm_queue_lease_seconds. Long runners (the sandbox monitor loop)
    call this every 30 s so the sweep never mistakes live work for a crash.

    Returns False when the CAS loses: the attempt is stale (the lease expired
    and the job was requeued and reclaimed, possibly by this very process
    with a higher attempts count), the job is no longer running, or it is not
    ours. The runner must treat False as "stop now": its writes would be
    fenced out anyway and a second worker may already own the run. Returns
    True with no write when no job is current (the BackgroundTasks fallback).

    Never raises. A transport failure is not a lost lease: the lease still
    has most of its window left, the next renewal retries, and the sweep is
    the backstop if the database stays unreachable for the whole lease.
    """
    job = job if job is not None else current_job.get()
    if job is None:
        return True
    try:
        lease = (_now() + timedelta(seconds=get_settings().llm_queue_lease_seconds)).isoformat()
        return _cas_job(get_supabase(), job, {"lease_expires_at": lease})
    except Exception:  # noqa: BLE001 - a DB hiccup must not abort hours of work
        logger.exception("llm queue: lease renewal for %s errored; keeping the lease", job["id"])
        return True


def requeue_self(job: dict | None = None) -> bool:
    """Hand the job executing on this thread (or `job`) back to the queue as
    interrupted, due immediately, so the next worker (or this one after a
    restart) resumes it. Used by the sandbox runner on shutdown: it kills its
    child, leaves the domain rows as they are (the run's own CAS marks carry
    the resume state) and returns normally; _execute then sees the job is no
    longer running and skips its success mark.

    The requeue is fenced like every other write (status running, our token,
    our attempt), so a job the sweep already requeued is left alone. Returns
    the CAS outcome; True with no write when no job is current.
    """
    job = job if job is not None else current_job.get()
    if job is None:
        return True
    ok = _cas_job(
        get_supabase(),
        job,
        {
            "status": "queued",
            "next_attempt_at": _now().isoformat(),
            "claimed_by": None,
            "lease_expires_at": None,
            "error_kind": "interrupted",
            "last_error": _INTERRUPTED_MESSAGE,
        },
    )
    if ok:
        # Mirror the row locally so _execute's success path knows the job
        # was handed on and does not log a spurious lost-lease warning.
        job["status"] = "queued"
        logger.warning("llm queue: job %s requeued itself (shutdown)", job["id"])
    return ok


def _mark_domain(spec: _JobSpec, job: dict, fields: dict) -> None:
    try:
        spec.mark(job["target_id"], fields)
    except Exception:  # noqa: BLE001 - a vanished domain row must not wedge the queue
        logger.exception(
            "llm queue: domain mark failed for %s %s", job["job_type"], job["target_id"]
        )


def _execute(job: dict) -> None:
    """Run one claimed job to a terminal or requeued state. Never raises:
    the failure handler is guarded end to end and the success mark's own
    database error is logged (the sweep requeues the job when its lease
    expires, and the rerun is idempotent)."""
    s = get_settings()
    spec = _spec(job["job_type"])
    sb = get_supabase()
    token = current_job.set(job)
    try:
        with llm_gate.tier(llm_gate.TIER_JOB, job_id=job["id"]):
            spec.run(job.get("payload") or {})
    except Exception as exc:  # noqa: BLE001 - classified below
        _handle_failure(sb, job, spec, exc, s)
        return
    finally:
        current_job.reset(token)
    if job.get("status") == "queued":
        # requeue_self() handed the job on (shutdown); nothing left to mark.
        return
    try:
        succeeded = _cas_job(
            sb,
            job,
            {
                "status": "succeeded",
                "finished_at": _now().isoformat(),
                "lease_expires_at": None,
                "claimed_by": None,
            },
        )
    except Exception:  # noqa: BLE001 - _execute never raises
        logger.exception("llm queue: success mark for %s errored", job["id"])
        return
    if not succeeded:
        # The lease expired mid-run and the sweep requeued the job. The work
        # itself completed (the domain row says done); the requeued run will
        # re-do it idempotently. Rare: lease >> real runtimes.
        logger.warning("llm queue: lost lease on %s before completion", job["id"])


_HANDLER_FAILED_MESSAGE = (
    "The run failed and its error could not be recorded. Run it again; if "
    "this keeps happening, contact your IT Director."
)


def _is_lease_lost(exc: BaseException) -> bool:
    """True for rfp_sandbox_runner.LeaseLost without importing the runner
    (it must stay optional here): match the class by name and module, or an
    explicit `llm_queue_lease_lost` attribute, and fall back to isinstance
    only when the runner module imports cleanly."""
    if getattr(exc, "llm_queue_lease_lost", False) is True:
        return True
    cls = type(exc)
    if cls.__name__ == "LeaseLost" and cls.__module__.endswith("rfp_sandbox_runner"):
        return True
    try:
        from app.services.rfp_sandbox_runner import LeaseLost
    except Exception:  # noqa: BLE001 - the runner is optional to this module
        return False
    return isinstance(exc, LeaseLost)


def _handle_failure(
    sb: Any, job: dict, spec: _JobSpec, exc: Exception, s: Settings
) -> None:
    """Requeue or terminally fail a job whose run raised. Never raises: an
    error inside the classification or the domain mark still reaches a
    terminal CAS, so a job can never be left running with a live lease and
    no worker (the sweep would eventually catch it, but with a misleading
    'interrupted' verdict instead of the real failure)."""
    if _is_lease_lost(exc):
        # The sweep already requeued the job and another claim owns it; our
        # fence would refuse every write anyway, and the domain row belongs
        # to the new owner. Leave everything alone.
        logger.warning(
            "llm queue: job %s (%s) lost its lease mid-run; no writes",
            job["id"],
            job["job_type"],
        )
        return
    try:
        _handle_failure_inner(sb, job, spec, exc, s)
    except Exception:  # noqa: BLE001 - the terminal CAS below is the last resort
        logger.exception(
            "llm queue: failure handling for %s (%s) errored; forcing terminal state",
            job["id"],
            job["job_type"],
        )
        try:
            # Fenced on status=running: a job the inner handler already
            # requeued or failed before erroring is left as it is, domain
            # row included (its new owner, or the earlier mark, governs it).
            forced = _cas_job(
                sb,
                job,
                {
                    "status": "failed",
                    "finished_at": _now().isoformat(),
                    "lease_expires_at": None,
                    "claimed_by": None,
                    "error_kind": llm_errors.KIND_UNKNOWN,
                    "last_error": _HANDLER_FAILED_MESSAGE,
                },
            )
            if forced:
                _mark_domain(
                    spec, job, {"status": "failed", "error": _HANDLER_FAILED_MESSAGE}
                )
        except Exception:  # noqa: BLE001 - nothing left to try; the sweep is the backstop
            logger.exception("llm queue: terminal CAS for %s errored", job["id"])


def _handle_failure_inner(
    sb: Any, job: dict, spec: _JobSpec, exc: Exception, s: Settings
) -> None:
    kind = llm_errors.classify(exc)
    model = spec.model_label(s)
    message = spec.error_message(exc, model)[:_ERROR_MAX_CHARS]
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
        try:
            cutoff = (_now() - timedelta(days=s.llm_call_log_retention_days)).isoformat()
            sb.table("llm_call_log").delete().lt("created_at", cutoff).execute()
            sb.table("llm_jobs").delete().lt("created_at", cutoff).in_(
                "status", ["succeeded", "failed", "canceled"]
            ).execute()
        except Exception:  # noqa: BLE001 - one prune must not skip the other
            logger.exception("llm queue: ledger prune failed")
        if s.rfp_ingest_enabled:
            # Sandbox retention: derived outputs + quarantine copies of runs
            # past rfp_ingest_retention_days (rows and manifests are kept).
            try:
                from app.services import rfp_ingest

                rfp_ingest.prune_expired()
            except Exception:  # noqa: BLE001 - retention must never wedge the sweep
                logger.exception("llm queue: rfp ingest retention prune failed")


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


_RESTORED_MESSAGE = "The project was deleted while this AI job was in flight. Run it again."


def release_restored_project_jobs(project_id: str) -> int:
    """After a project restore (docs/PROJECT_DELETE.md 4.3): the snapshot
    brings back jobs that were queued or running at delete time, but their
    worker and lease are long gone. Fail them (and their pending/running
    domain rows) so nothing reruns on its own and users can start again.
    Returns how many jobs were released."""
    sb = get_supabase()
    stranded = (
        sb.table("llm_jobs")
        .select("*")
        .eq("project_id", project_id)
        .in_("status", list(_ACTIVE_STATUSES))
        .execute()
    ).data or []
    released = 0
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
                    "last_error": _RESTORED_MESSAGE,
                }
            )
            .eq("id", job["id"])
            .in_("status", list(_ACTIVE_STATUSES))
            .execute()
        )
        if resp.data:
            released += 1
            try:
                spec = _spec(job["job_type"])
            except KeyError:
                logger.warning("llm queue: restored job %s has unknown type %s",
                               job["id"], job["job_type"])
                continue
            _mark_domain(spec, job, {"status": "failed", "error": _RESTORED_MESSAGE})
    return released


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
    # task -> job_type, so each claim pass can count only its own kind.
    running: dict[asyncio.Task, str] = {}
    while True:
        s = get_settings()
        try:
            await asyncio.to_thread(_sweep, s)
            for job in await asyncio.to_thread(_claim_tick, s, list(running.values())):
                task = asyncio.create_task(asyncio.to_thread(_execute, job))
                running[task] = job["job_type"]
                task.add_done_callback(lambda t: running.pop(t, None))
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("llm queue tick failed")
        await asyncio.sleep(max(0.5, float(s.llm_queue_poll_interval_seconds)))
