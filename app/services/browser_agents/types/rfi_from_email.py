"""The rfi_from_email agent type, BDR side (docs/browser_agents/SPEC.md 5,
overrides O1 and O2).

A filed PM email that names a Procore RFI (Pass A: an RFI show link; Pass C:
only a numbered subject) becomes one job per RFI. The worker's model logs in
(ensure_login, O2), locates the RFI when only a number is known, captures
its REST detail (capture_rfi_detail) and submits it to /rfi-capture; BDR
applies the capture through pm_rfi_sync (trigger 'agent') and the sweeper
closes the job from the run. The model never authors RFI data (O1).

This module holds:
  SPEC                 the registry entry (seed and checkpoint models, tools,
                       identity, prompt, the claim-time prepare);
  prepare(job)         SPEC.md 5.2: the link was removed, disabled or now
                       points elsewhere -> failed/no_link (no notice); the
                       local row is closed and complete, or was synced since
                       the email arrived -> succeeded/already_current;
                       otherwise the job runs;
  agent_route_active() whether pm_rfi_sync routes emails here (30 s cache);
  enqueue_for_email()  the seeding call pm_rfi_sync._queue_email_rfi makes;
  retry_apply()        SPEC.md 5.4.7: re-run a failed apply of the same
                       capture (new apply_attempt, new run);
  register_sweep_steps the sweeper's unreported-login and contribution steps.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.core import supabase_client
from app.services.browser_agents import protocol
from app.services.browser_agents.registry import AgentTypeSpec, PrepareResult

logger = logging.getLogger(__name__)

KEY = "rfi_from_email"
ROUTE_CACHE_SECONDS = 30.0
RETRY_PRIORITY = 100

_route_lock = threading.Lock()
_route_cache: tuple[float, bool] | None = None


def _sb():
    return supabase_client.get_supabase()


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Claim-time short-circuits ─────────────────────────────────────────────────


def prepare(job: dict[str, Any]) -> PrepareResult:
    from app.services import pm_rfi_sync

    try:
        seed = protocol.RfiFromEmailInput.model_validate(job.get("input") or {})
    except ValidationError:
        return PrepareResult.fail("unsupported", "The job seed is not a valid RFI seed")
    project_id = job.get("project_id")
    if not project_id:
        return PrepareResult.fail("no_link", "The job names no BDR project", notify=False)
    sb = _sb()
    link = pm_rfi_sync.get_link(sb, project_id)
    if not (
        link
        and link.get("platform") == "procore"
        and link.get("sync_enabled")
        and link.get("host") == seed.host
        and str(link.get("external_project_id") or "") == seed.external_project_id
    ):
        return PrepareResult.fail(
            "no_link", "The project's Procore link was removed, turned off or changed", notify=False
        )
    if seed.rfi_id:
        rows = (
            sb.table("rfis").select("id, status, external_updated_at, external_synced_at")
            .eq("project_id", project_id).eq("external_id", seed.rfi_id).limit(1).execute()
        ).data or []
        local = rows[0] if rows else None
        if pm_rfi_sync._finished(local):
            return PrepareResult.succeed("already_current", "The RFI is already closed and complete in BDR")
        synced = _ts((local or {}).get("external_synced_at"))
        if synced is not None and synced >= _ts(seed.email_message_at):
            return PrepareResult.succeed("already_current", "The RFI was synced after this email arrived")
    return PrepareResult.run()


SPEC = AgentTypeSpec(
    key=KEY,
    label="RFI from email",
    input_model=protocol.RfiFromEmailInput,
    checkpoint_model=protocol.RfiCheckpoint,
    identity="procore",
    prompt_version="rfi_from_email.v1",
    succeed_outcomes=("nothing_to_do",),
    prepare=prepare,
    allows_capture=True,
    help_reasons=tuple(protocol.HelpReasonCode.__args__),  # type: ignore[attr-defined]
    platform="procore",
)


# ── Routing and seeding ────────────────────────────────────────────────────────


def reset_route_cache() -> None:
    global _route_cache
    with _route_lock:
        _route_cache = None


def agent_route_active() -> bool:
    """The kill switch is on and the rfi_from_email type is enabled. Read
    from the database at most every 30 s per process; an unreadable row
    counts as off (the legacy email run takes over)."""
    global _route_cache
    now = time.monotonic()
    with _route_lock:
        if _route_cache is not None and now - _route_cache[0] < ROUTE_CACHE_SECONDS:
            return _route_cache[1]
    active = False
    try:
        sb = _sb()
        settings_rows = sb.table("browser_agent_settings").select("enabled").eq("id", 1).limit(1).execute().data or []
        type_rows = sb.table("browser_agent_types").select("enabled").eq("key", KEY).limit(1).execute().data or []
        active = bool(settings_rows and settings_rows[0].get("enabled") and type_rows and type_rows[0].get("enabled"))
    except Exception:  # noqa: BLE001 - unknown means off: the legacy run is the safe default
        logger.warning("browser agents: rfi_from_email routing state unreadable; using the legacy email run")
    with _route_lock:
        _route_cache = (now, active)
    return active


def enqueue_for_email(email: dict, link: dict, rfi_ids: list[str]) -> int:
    """Queue the jobs for one email: one per distinct RFI id (at most
    pm_apps.procore.MAX_JOBS_PER_EMAIL), or one locate job from the subject
    hint when `rfi_ids` is empty. Returns jobs newly created (a re-read
    email finds its jobs by dedupe key and creates nothing). Raises on a
    database failure."""
    from app.services import procore_client as pc
    from app.services.browser_agents import queue
    from app.services.browser_agents.pm_apps import procore as procore_app

    email_id = str(email.get("id"))
    project_id = link["project_id"]
    company = link.get("external_company_id") or pc.extract_company_id(
        email.get("body_text"), str(link.get("external_project_id"))
    )
    source_email = email_id if procore_app._is_uuid(email_id) else None
    created = 0
    ids = sorted({str(r) for r in rfi_ids if protocol.PROCORE_ID_RE.match(str(r))})
    if len(ids) > procore_app.MAX_JOBS_PER_EMAIL:
        logger.warning("browser agents: email %s names %d RFIs; queueing the first %d",
                       email_id, len(ids), procore_app.MAX_JOBS_PER_EMAIL)
        ids = ids[: procore_app.MAX_JOBS_PER_EMAIL]
    if ids:
        for rfi_id in ids:
            seed = procore_app.seed_input(email, link, rfi_id=rfi_id, company_id=company)
            result = queue.enqueue(KEY, f"email:{email_id}:rfi:{rfi_id}", seed, project_id=project_id,
                                   source_email_id=source_email, platform="procore")
            created += int(result.created)
        return created
    hint, _ = procore_app.subject_hints(email.get("subject"))
    if not hint:
        return 0
    seed = procore_app.seed_input(email, link, rfi_id=None, company_id=company)
    result = queue.enqueue(KEY, f"email:{email_id}:locate", seed, project_id=project_id,
                           source_email_id=source_email, platform="procore")
    return int(result.created)


# ── Apply retry (SPEC.md 5.4.7) ────────────────────────────────────────────────


class RetryRefused(ValueError):
    """The job is not a failed apply of a capture."""


def retry_apply(job_id: str) -> dict:
    """Apply the same capture again after apply_failed: apply_attempt + 1,
    a new pm_rfi_sync_runs row (its own unique key), the job back to
    applying. Compare-and-set on (status, apply_attempt): two clicks make
    one retry. Returns the new run. The caller audits."""
    from app.services.browser_agents import queue

    sb = _sb()
    rows = sb.table("browser_agent_jobs").select("*").eq("id", job_id).limit(1).execute().data or []
    if not rows:
        raise RetryRefused("job not found")
    job = rows[0]
    if job.get("agent_type") != KEY or job.get("status") != "failed" or job.get("outcome") != "apply_failed":
        raise RetryRefused("only a failed apply can be retried")
    previous = None
    if job.get("sync_run_id"):
        prev_rows = sb.table("pm_rfi_sync_runs").select("*").eq("id", str(job["sync_run_id"])).limit(1).execute().data or []
        previous = prev_rows[0] if prev_rows else None
    if not previous or not previous.get("detail_artifact_id"):
        raise RetryRefused("the job has no capture to apply again")
    attempt = int(job.get("apply_attempt") or 0)
    # The run first, then the job in ONE compare-and-set that also points it
    # at the new run: the sweeper never sees an 'applying' job still naming
    # the old failed run (it would fail the job again while the new run
    # applies). Two clicks: the (agent_job_id, apply_attempt) unique index
    # refuses the second run.
    run = {
        "project_id": job["project_id"],
        "trigger": "agent",
        "status": "queued",
        "agent_job_id": job_id,
        "apply_attempt": attempt + 1,
        "detail_artifact_id": previous["detail_artifact_id"],
        "target_external_rfi_id": previous.get("target_external_rfi_id"),
        "source_email_id": previous.get("source_email_id"),
    }
    try:
        inserted = sb.table("pm_rfi_sync_runs").insert(run).execute().data or []
    except Exception as exc:  # noqa: BLE001 - the unique index: another retry won
        if "23505" in str(exc) or "duplicate key" in str(exc):
            raise RetryRefused("the job changed; reload it") from None
        raise
    if not inserted or not inserted[0].get("id"):
        raise RuntimeError("the apply run was not created")
    new_run = inserted[0]
    moved = (
        sb.table("browser_agent_jobs").update({
            "status": "applying", "outcome": "pending", "apply_attempt": attempt + 1, "error_kind": None,
            "error": None, "finished_at": None, "sync_run_id": new_run["id"],
        }).eq("id", job_id).eq("status", "failed").eq("apply_attempt", attempt).execute()
    ).data
    if not moved:
        sb.table("pm_rfi_sync_runs").update({"status": "failed", "error": "the job changed before the retry"}).eq(
            "id", new_run["id"]).eq("status", "queued").execute()
        raise RetryRefused("the job changed; reload it")
    queue.log_step(job_id, int(job.get("lease_epoch") or 0), "capture.retry",
                   f"Apply retried (attempt {attempt + 1})", actor="admin",
                   result={"run_id": new_run.get("id"), "apply_attempt": attempt + 1})
    return new_run


# ── Sweeper steps ──────────────────────────────────────────────────────────────


def register_sweep_steps() -> None:
    """Plug this type's periodic work into the browser agent sweeper:
    unreported browser logins count as failures after 5 min (identities),
    and helper contributions apply once their RFI exists (contributions)."""
    from app.services.browser_agents import contributions, identities, queue

    queue.register_sweep_step("unreported_logins", identities.sweep_unreported_logins)
    queue.register_sweep_step("contributions", contributions.sweep_pending)
