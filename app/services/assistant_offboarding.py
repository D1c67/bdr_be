"""Assistant offboarding (docs/ASSISTANT_PHASE3_PLAN.md 3.10).

Called from every user-admin path in app/routers/users.py (MFA reset,
deactivation, demotion to the external estimator, an internal role change,
hard delete) and from the nightly orphan sweep. Per mode it revokes the
delegation, expires open cards, stops split tasks and their queued
splitter runs, cancels queued assistant jobs, releases spend holds, pauses
routines (never ends them, deviation D10), retires staged files and runs
the HOOKS later phases add.

Never raises: each step runs in its own try and is logged, and the caller
decides what a failure means (a hard delete refuses on a failure among
steps 1 to 5). One audit row at the end, with counts only (no file names,
tainted text). Every assistant_* row is read and written through
app/assistant/repo.py; llm_jobs, bid_split_jobs and bid_split_files are
BDR tables, read with the service-role client (this module is outside
app/assistant/).

Running jobs are never canceled (llm_queue.cancel refuses them): they stop
at the per-call principal check or the split hold guard, and are counted
in `still_running`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.core import supabase_client

logger = logging.getLogger(__name__)

MODES = ("mfa_reset", "deactivated", "demoted_external", "role_changed", "deleted", "sweep")
# Steps 1 to 5: a hard delete refuses when any of them failed (3.10.4).
CRITICAL_STEPS = ("delegation", "cards", "tasks", "split_runs", "jobs", "browser_credentials")
# The modes that leave the person with no assistant at all.
LEAVING_MODES = ("deactivated", "demoted_external", "sweep", "deleted")
APPROVAL_REASON_OFFBOARDED = "owner_offboarded"
APPROVAL_REASON_ROLE = "role_changed"
# The task failure reason bid_split._assistant_owner_stopped recognizes.
TASK_REASON = "owner_changed"
SPLIT_TOOL = "split_into_project"
PURGE_LIMIT = 200
PURGE_ROUNDS = 20
JOB_LIMIT = 200


@dataclass
class OffboardResult:
    ok: bool                                              # every critical step succeeded
    counts: dict[str, int] = field(default_factory=dict)  # delegations, cards, tasks, split_runs, jobs, holds, schedules, files
    failures: list[str] = field(default_factory=list)     # step names
    still_running: int = 0                                # running jobs that stop at their next owner or hold check


# Phases 4 to 6 add Microsoft, credential and cookie-jar cleanup here. Each
# hook takes (user_id, mode) and returns its counts. Phase 4 appends
# phase4_offboard below.
HOOKS: list[Callable[[str, str], dict]] = []


def revoke_delegation(user_id: str, reason: str) -> int:
    """Revoke the live delegation (repo.revoke_delegation) and return the
    count. update_user calls it before the profile write so unattended work
    stops first (3.10.4). Raises on a transport failure (offboard records it
    as a failed step; users.py logs it and goes on)."""
    from app.assistant import repo

    return repo.revoke_delegation(str(user_id), str(reason))


# ── Steps ────────────────────────────────────────────────────────────────────


def _is_writer(role: str | None) -> bool:
    from app.core.roles import WRITER_ROLES, Role

    try:
        return Role(role) in WRITER_ROLES
    except ValueError:
        return False


def _expire_cards(user_id: str, mode: str, new_role: str | None) -> int:
    """Step 2: pending and approved cards expire (no bell), then each card's
    message settles. role_changed expires only the cards whose tool the new
    role may not use."""
    from app.assistant import registry, repo
    from app.core.roles import Role

    rows = repo.open_approvals(user_id)
    if mode == "role_changed":
        try:
            role = Role(new_role)
        except ValueError:
            role = None
        keep = []
        for row in rows:
            spec = registry.get(str(row.get("action_kind") or ""))
            if spec is not None and role is not None and role in spec.roles:
                continue
            keep.append(row)
        rows, reason = keep, APPROVAL_REASON_ROLE
    else:
        reason = APPROVAL_REASON_OFFBOARDED
    if not rows:
        return 0
    by_id = {str(r["id"]): r for r in rows}
    moved = repo.expire_approvals(user_id, list(by_id), reason)
    for row in moved:
        message_id = row.get("message_id") or by_id.get(str(row.get("id")), {}).get("message_id")
        try:
            repo.settle_message(user_id, message_id)
        except Exception:  # noqa: BLE001 - the message settles on its next read
            logger.exception("assistant offboarding: message of card %s did not settle", row.get("id"))
    return len(moved)


class StagingLeftover(RuntimeError):
    """Step 8 on a hard delete left storage objects unpurged; the step
    fails (it is not critical) and the audit carries both counts."""

    def __init__(self, retired: int, unpurged: int):
        super().__init__(f"{unpurged} staged object(s) not purged")
        self.retired = retired
        self.unpurged = unpurged


def _stop_tasks(user_id: str) -> tuple[int, set[str], int]:
    """Step 3: every open split task stops silently (queued runs canceled,
    the task failed owner_changed, its hold released). Returns (tasks
    stopped, the bid_split_files ids they cover, runs still running).

    The queued runs are canceled here, before stop_for_owner, so a failed
    cancel is seen: every task is still stopped, then the step raises, so
    a hard delete refuses (a run left queued would execute unscoped once
    created_by goes NULL) and step 4 retries every assistant file."""
    from app.assistant import continue_job, repo

    stopped, covered, running, failed = 0, set(), 0, 0
    for task in repo.open_tasks(user_id):
        if task.get("kind") != SPLIT_TOOL:
            continue
        ids = continue_job._file_ids(task, None)
        try:
            continue_job.cancel_queued_runs(ids)
            canceled = True
        except Exception:  # noqa: BLE001 - counted, the step fails below
            logger.exception("assistant offboarding: queued runs of task %s could not be canceled", task.get("id"))
            canceled, failed = False, failed + 1
        if canceled:
            covered.update(ids)
        # A failed cancel is retried once inside stop_for_owner.
        continue_job.stop_for_owner(user_id, task, TASK_REASON, bell=False, cancel=not canceled)
        stopped += 1
        running += _running_runs(ids)
    if failed:
        raise RuntimeError(f"queued split runs of {failed} task(s) could not be canceled")
    return stopped, covered, running


def _queued_runs(file_ids: list[str]) -> int:
    """How many of these files still have a queued bid_split job. Raises
    on a read failure (step 4 then fails closed)."""
    from app.services import llm_queue

    queued = 0
    for file_id in file_ids:
        job = llm_queue.active_job(llm_queue.JOB_BID_SPLIT, file_id)
        if job and job.get("status") == "queued":
            queued += 1
    return queued


def _running_runs(file_ids: list[str]) -> int:
    """How many of these files' bid_split jobs are still running (best
    effort, for still_running)."""
    from app.services import llm_queue

    running = 0
    for file_id in file_ids:
        try:
            job = llm_queue.active_job(llm_queue.JOB_BID_SPLIT, file_id)
        except Exception:  # noqa: BLE001
            continue
        if job and job.get("status") == "running":
            running += 1
    return running


def _assistant_split_files(user_id: str) -> list[str]:
    """bid_split_files of the person's assistant split jobs (created_by the
    person, actor_kind 'assistant') that still carry assistant_run_id and
    are not finished (0174 C2; a human Retry clears the run id)."""
    sb = supabase_client.get_supabase()
    jobs = (
        sb.table("bid_split_jobs").select("id")
        .eq("created_by", user_id).eq("actor_kind", "assistant").limit(JOB_LIMIT).execute()
    ).data or []
    job_ids = [str(j["id"]) for j in jobs if j.get("id")]
    if not job_ids:
        return []
    files = (
        sb.table("bid_split_files").select("id, assistant_run_id, status")
        .in_("job_id", job_ids).in_("status", ["pending", "running"]).limit(JOB_LIMIT * 5).execute()
    ).data or []
    return [str(f["id"]) for f in files if f.get("id") and f.get("assistant_run_id")]


def _cancel_split_runs(user_id: str, covered: set[str]) -> tuple[int, int]:
    """Step 4: queued bid_split runs of assistant files step 3 did not
    cover. Returns (files checked, runs still running). Then re-checks the
    files step 3 covered and raises when one still has a queued run, so
    the step fails rather than let it run unscoped after a hard delete."""
    from app.assistant import continue_job

    ids = [f for f in _assistant_split_files(user_id) if f not in covered]
    running = continue_job.cancel_queued_runs(ids) if ids else 0
    leftover = _queued_runs(sorted(covered))
    if leftover:
        raise RuntimeError(f"{leftover} queued split run(s) of stopped tasks are still queued")
    return len(ids), running


def _assistant_jobs(user_id: str, job_types: tuple[str, ...], status: str) -> list[dict]:
    return (
        supabase_client.get_supabase().table("llm_jobs").select("id, job_type, status, payload")
        .in_("job_type", list(job_types)).eq("status", status)
        .contains("payload", {"user_id": str(user_id)}).limit(JOB_LIMIT).execute()
    ).data or []


def _cancel_jobs(user_id: str, job_types: tuple[str, ...], passes: int) -> int:
    """Step 5: cancel the person's queued assistant jobs of these types.
    Two passes for the leaving modes: the cancellations of steps 3 and 4
    wake assistant_continue (bid_split.py)."""
    from app.services import llm_queue

    canceled = 0
    for _ in range(passes):
        for job in _assistant_jobs(user_id, job_types, "queued"):
            if llm_queue.cancel(str(job["id"])) is not None:
                canceled += 1
    return canceled


def _release_holds(user_id: str) -> int:
    """Step 6: every active spend hold released (the work never finishes),
    except the AI Estimating holds the phase 4 hook closes with their
    actual cost."""
    from app.assistant import repo, spend

    released = 0
    for hold in repo.active_holds(user_id):
        # The AI Estimating holds close at the AE actual cost, through
        # settle_external in the phase 4 hook (3.24); a plain release here
        # would fence that close out and the cost would never be counted.
        if hold.get("kind") in spend.EXTERNAL_KINDS:
            continue
        spend.release(user_id, str(hold["id"]))
        released += 1
    return released


def _pause_schedules(user_id: str, reason: str, *, widen: bool) -> int:
    """Step 7. MFA reset: active rows pause as mfa_reset (the resume pass
    restarts them after the next grant). Leaving: every row that could
    restart on its own (active, paused_feature_off, paused mfa_reset or
    no_delegation) pauses as owner_offboarded; rows the person or a
    ceiling paused keep their reason."""
    from app.assistant import repo

    return repo.pause_schedules(user_id, reason, include_resumable_paused=widen)


def _retire_staging(user_id: str, *, purge: bool) -> int:
    """Step 8: unconsumed staged rows retired; on a hard delete their
    objects are purged right away, so none outlives its row (offboarding
    map G10)."""
    from app.assistant import repo
    from app.services import assistant_staging

    retired = 0
    for row in repo.unconsumed_staged_files(user_id):
        if repo.retire_staged_file(user_id, str(row["id"]), allow_linked=True):
            retired += 1
    if purge:
        for _ in range(PURGE_ROUNDS):
            if assistant_staging.purge_expired(user_id=user_id, limit=PURGE_LIMIT) <= 0:
                break
        # purge_expired never raises and returns 0 on a failure too, so the
        # rows still holding an object are counted: the cascade would orphan
        # them, and the audit must say so (3.10.4: step 8 proceeds, audited).
        left = repo.unpurged_staged_count(user_id)
        if left:
            raise StagingLeftover(retired, left)
    return retired


def _audit(actor_id: str | None, user_id: str, mode: str, old_role: str | None, new_role: str | None,
           result: OffboardResult) -> None:
    try:
        from app.services import notifications

        notifications.audit(actor_id, "assistant.offboarded", "profile", user_id, {
            "mode": mode, "old_role": old_role, "new_role": new_role, **result.counts,
            "failures": list(result.failures), "still_running": result.still_running,
        })
    except Exception:  # noqa: BLE001 - the audit never fails the caller
        logger.exception("assistant offboarding: audit failed for %s", user_id)


# ── Phase 4 hook (docs/ASSISTANT_PHASE4_PLAN.md 3.24) ────────────────────────

REPORT_PREFIX = "assistant-reports"
REPORT_LIST_PAGE = 1000
AE_TASK_LIMIT = 200


def cancel_ae_run(run_id: str, user_id: str | None = None) -> bool:
    """Cancel one AI Estimating run (plans_reader_runs.cancel) as a system
    actor. True when this call canceled it; False when it had already
    finished or no longer exists (409 and 404 refusals). Any other error
    propagates, so the caller keeps the hold and retries. The system actor
    carries the it_admin role only to pass the service's estimator check;
    no HTTP route or audit row is involved."""
    from app.services import plans_reader_runs

    actor = SimpleNamespace(id=user_id, role="it_admin", is_dev=False)
    try:
        plans_reader_runs.cancel(supabase_client.get_supabase(), {"id": str(run_id)}, user=actor)
    except plans_reader_runs.RunRefused as exc:
        if getattr(exc, "status_code", 0) in (404, 409):
            return False
        raise
    return True


def report_objects(user_id: str) -> dict[str, str | None]:
    """The objects under `assistant-reports/{user_id}/` as {file name:
    created_at}. Raises on a storage error (callers treat that as "not
    verified": a row is never marked purged on a failed listing)."""
    from app.services import storage

    store = supabase_client.get_supabase().storage.from_(storage.BUCKET)
    out: dict[str, str | None] = {}
    offset = 0
    while True:
        page = store.list(f"{REPORT_PREFIX}/{user_id}", {"limit": REPORT_LIST_PAGE, "offset": offset}) or []
        for entry in page:
            name = entry.get("name")
            if name:
                out[str(name)] = entry.get("created_at")
        if len(page) < REPORT_LIST_PAGE:
            return out
        offset += REPORT_LIST_PAGE


def purge_reports(rows: list[dict]) -> tuple[int, int]:
    """Delete the storage objects of these `assistant_reports` rows and mark
    each row purged, only after a listing of the owner's prefix shows the
    object is gone (storage.delete_file may answer without raising when the
    delete failed). Returns (rows purged, rows left for a retry). Never
    raises: an owner whose delete or listing fails keeps every row."""
    from app.assistant import repo
    from app.services import storage

    by_owner: dict[str, list[dict]] = {}
    for row in rows:
        by_owner.setdefault(str(row["user_id"]), []).append(row)
    purged = left = 0
    for owner, items in by_owner.items():
        for row in items:
            try:
                storage.delete_file(str(row["storage_path"]))
            except Exception:  # noqa: BLE001 - verified below; a retry follows
                logger.exception("assistant offboarding: report object %s could not be deleted", row.get("id"))
        try:
            present = report_objects(owner)
        except Exception:  # noqa: BLE001
            logger.exception("assistant offboarding: reports of %s could not be listed", owner)
            left += len(items)
            continue
        for row in items:
            name = str(row.get("storage_path") or "").rsplit("/", 1)[-1]
            if name in present:
                left += 1
                logger.warning("assistant offboarding: report object %s is still present", row.get("id"))
                continue
            try:
                if repo.mark_report_purged(owner, str(row["id"])):
                    purged += 1
            except Exception:  # noqa: BLE001
                logger.exception("assistant offboarding: report %s could not be marked purged", row.get("id"))
                left += 1
    return purged, left


def _cancel_plans_chat_job(message_id: str) -> None:
    """Cancel the QUEUED plans_reader_chat job of a page read's answer
    (cancel only, never a claim; a running job finishes and is billed).
    The shared helper of the timeout path (plans_tasks)."""
    from app.assistant import plans_tasks

    plans_tasks.cancel_queued_chat_job(message_id)


def _offboard_page_read(user_id: str, task: dict, counts: dict[str, int]) -> None:
    from app.assistant import plans_tasks
    from app.services import assistant_ai_estimate

    message_id = str(plans_tasks._dict(task.get("payload")).get("plans_message_id") or "")
    cost = "0"
    if message_id:
        _cancel_plans_chat_job(message_id)
        state = assistant_ai_estimate.chat_message_state(None, message_id, viewer_role="")
        cost = state.get("cost_usd") or "0"
    changed, closed = plans_tasks.stop_task(
        user_id, task, plans_tasks.FAILED_OWNER, actual_usd=cost, bell=False)
    counts["plans_tasks"] += int(changed)
    counts["ae_holds"] += int(closed)


def _offboard_ae_run(user_id: str, task: dict, counts: dict[str, int]) -> None:
    from app.assistant import plans_tasks
    from app.services import assistant_ai_estimate

    run_id = plans_tasks.run_id_of(task)
    cost = "0"
    if run_id:
        state = assistant_ai_estimate.run_state(None, run_id)
        if state.get("status") in plans_tasks.TERMINAL_RUN_STATUSES:
            # Finished already: finalize as in 3.6.4 (message, settle, bell).
            plans_tasks.advance(user_id, task)
            return
        if state.get("status") != "missing":
            counts["ae_runs_canceled"] += int(cancel_ae_run(run_id, user_id))
            cost = assistant_ai_estimate.run_state(None, run_id).get("cost_usd") or "0"
    changed, closed = plans_tasks.stop_task(
        user_id, task, plans_tasks.FAILED_OWNER, actual_usd=cost, bell=False)
    counts["plans_tasks"] += int(changed)
    counts["ae_holds"] += int(closed)


def _offboard_loose_ae_holds(user_id: str, counts: dict[str, int]) -> None:
    """An AI Estimating hold with no open task behind it (a crash between
    the reserve and the task insert) is released with a zero actual."""
    from app.assistant import repo, spend

    for hold in repo.active_holds(user_id):
        if hold.get("kind") not in spend.EXTERNAL_KINDS:
            continue
        task_id = hold.get("task_id")
        if task_id and (repo.get_task(user_id, str(task_id)) or {}).get("status") in ("pending", "running",
                                                                                      "waiting_external"):
            continue
        counts["ae_holds"] += int(spend.settle_external(user_id, str(hold["id"]), Decimal("0"), release=True))


def _offboard_reports(user_id: str, mode: str, role: Any, counts: dict[str, int]) -> None:
    """Leaving modes: every report object of the person goes. role_changed:
    those the live role may not read (assistant_reports.live_role_may_read:
    the spec's SPEC_ROLES, and ACTUAL_BID_VIEWER_ROLES for a workbook built
    with actual bid dates). Skipped while the person is on legal hold (3.9.3)."""
    from app.assistant import repo
    from app.services import assistant_reports

    if repo.legal_hold(user_id):
        return
    rows = repo.owner_reports(user_id, statuses=("ready",))
    if mode == "role_changed":
        rows = [
            r for r in rows
            if not assistant_reports.live_role_may_read(r, role)
        ]
    if not rows:
        return
    purged, left = purge_reports(rows)
    counts["reports_purged"] += purged
    if left:
        raise RuntimeError(f"{left} report object(s) could not be purged")


def phase4_offboard(user_id: str, mode: str) -> dict:
    """The phase 4 offboarding hook (3.24). In the modes that stop work (the
    leaving modes, and role_changed to a role that is not a writer): each
    open page read cancels its queued chat job and closes its hold with the
    AE message cost; each open AI Estimating run is canceled (or finalized
    when it already ended) and its hold released with the run's cost so far;
    the task fails `owner_changed` with no bell. Reports: leaving modes
    delete every object, role_changed those the new role may not read; legal
    hold skips them. Returns {plans_tasks, ae_runs_canceled, ae_holds,
    reports_purged}, plus phase4_unfinished when a step failed. Every step is
    idempotent (fenced on task status and the active hold), so the sweep mode
    re-runs it safely. A step that fails is logged and the rest still run."""
    from app.assistant import plans_tasks, repo
    from app.core.roles import Role

    if mode == "mfa_reset":
        return {}          # nothing of phase 4 stops on an MFA reset
    counts = {"plans_tasks": 0, "ae_runs_canceled": 0, "ae_holds": 0, "reports_purged": 0}
    problems: list[str] = []
    role: Any = None
    try:
        role = Role((repo.human_profile(user_id) or {}).get("role"))
    except ValueError:
        role = None
    stops = mode in LEAVING_MODES or (mode == "role_changed" and (role is None or not _is_writer(role.value)))

    if stops:
        for task in repo.open_ae_tasks(user_id, limit=AE_TASK_LIMIT):
            try:
                if task.get("kind") == plans_tasks.KIND_PAGES:
                    _offboard_page_read(user_id, task, counts)
                elif task.get("kind") == plans_tasks.KIND_RUN:
                    _offboard_ae_run(user_id, task, counts)
            except Exception:  # noqa: BLE001 - one task never stops the rest
                logger.exception("assistant offboarding: AE task %s did not stop", task.get("id"))
                problems.append("task")
        try:
            _offboard_loose_ae_holds(user_id, counts)
        except Exception:  # noqa: BLE001
            logger.exception("assistant offboarding: AE holds of %s did not close", user_id)
            problems.append("holds")
    if mode in LEAVING_MODES or mode == "role_changed":
        try:
            _offboard_reports(user_id, mode, role, counts)
        except Exception:  # noqa: BLE001
            logger.exception("assistant offboarding: reports of %s were not all purged", user_id)
            problems.append("reports")
    if problems:
        # Not raised: the counts of the work that did finish still reach the
        # audit, with the number of steps left undone next to them (the
        # sweep mode re-runs the hook, which is idempotent).
        counts["phase4_unfinished"] = len(problems)
    return counts


HOOKS.append(phase4_offboard)


# ── Phase 7 hook (docs/ASSISTANT_PHASE7_PLAN.md 6.8) ─────────────────────────


def phase7_offboard(user_id: str, mode: str) -> dict:
    """The phase 7 offboarding hook (6.8). Acts only in the leaving modes
    that keep the profile row (deactivated, demoted_external, sweep): a hard
    delete relies on the cascade, and mfa_reset and role_changed leave the
    router alone (the person still gets alerts; the live role gates what
    each row may show). The person's undelivered inbox rows are dismissed
    owner_offboarded, their pending ledger sends are skipped offboarded
    (claimed rows settle through their own fence), and their own
    subscription overrides and project mutes are deleted, so a returning
    person starts from the role defaults. Returns {inbox_dismissed,
    sends_skipped, subscriptions_deleted, project_mutes_deleted}, plus
    phase7_unfinished when a step failed (counts only). Every step is
    idempotent and bounded (500 rows), so the sweep mode re-runs the hook
    safely and a larger backlog finishes on a later sweep; a step that
    fails is logged and the rest still run."""
    from app.assistant import repo

    if mode not in LEAVING_MODES or mode == "deleted":
        return {}
    steps = (
        ("inbox_dismissed", repo.offboard_dismiss_undelivered),
        ("sends_skipped", repo.offboard_skip_pending_sends),
        ("subscriptions_deleted", repo.offboard_delete_subscriptions),
        ("project_mutes_deleted", repo.offboard_delete_project_mutes),
    )
    counts: dict[str, int] = {}
    problems = 0
    for key, fn in steps:
        try:
            counts[key] = int(fn(user_id))
        except Exception:  # noqa: BLE001 - one step never stops the rest
            logger.exception("assistant offboarding: phase 7 step %s failed for %s", key, user_id)
            problems += 1
    if problems:
        counts["phase7_unfinished"] = problems
    return counts


HOOKS.append(phase7_offboard)


# ── The entry point ──────────────────────────────────────────────────────────


def offboard(user_id: str, mode: str, *, actor_id: str | None,
             old_role: str | None = None, new_role: str | None = None,
             prior_revoked: int = 0) -> OffboardResult:
    """Run the steps of 3.10.2 for `mode` (one of MODES), in order. Never
    raises; failures are listed in the result and the audit row.
    `prior_revoked` is the delegations update_user already revoked before
    the profile write, so the audit counts them (step 1 then finds none)."""
    from app.services import llm_queue

    user_id = str(user_id)
    result = OffboardResult(ok=True)
    if mode not in MODES:
        result.ok = False
        result.failures.append("mode")
        logger.error("assistant offboarding: unknown mode %r for %s", mode, user_id)
        return result

    leaving = mode in LEAVING_MODES
    stops_work = leaving or (mode == "role_changed" and not _is_writer(new_role))
    covered: set[str] = set()

    def step(name: str, body: Callable[[], Any], count: str | None = None) -> None:
        """Run one step; `count` names the counts key its int result goes to."""
        try:
            value = body()
        except Exception:  # noqa: BLE001 - each step is independent
            logger.exception("assistant offboarding: step %s failed for %s (%s)", name, user_id, mode)
            result.failures.append(name)
            return
        if count is not None:
            result.counts[count] = int(value or 0)

    def tasks() -> int:
        stopped, ids, running = _stop_tasks(user_id)
        covered.update(ids)
        result.still_running += running
        return stopped

    def split_runs() -> int:
        checked, running = _cancel_split_runs(user_id, covered)
        result.still_running += running
        return checked

    def staging() -> int:
        try:
            return _retire_staging(user_id, purge=mode == "deleted")
        except StagingLeftover as exc:
            result.counts["files"] = exc.retired
            result.counts["staging_unpurged"] = exc.unpurged
            raise

    def jobs(types: tuple[str, ...], passes: int) -> int:
        canceled = _cancel_jobs(user_id, types, passes)
        result.still_running += len(_assistant_jobs(user_id, types, "running"))
        return canceled

    # 1. The delegation (an internal role change keeps it).
    if mode != "role_changed":
        step("delegation", lambda: revoke_delegation(user_id, mode), "delegations")
        if prior_revoked:
            result.counts["delegations"] = result.counts.get("delegations", 0) + int(prior_revoked)
    # 2. Cards.
    step("cards", lambda: _expire_cards(user_id, mode, new_role), "cards")
    # Personal credentials are revoked even when feature flags are off.
    # The SQL function also fences jobs and clears sessions atomically.
    from app.assistant import repo
    step("browser_credentials", lambda: repo.browser_revoke_owner(user_id), "browser_credentials")
    # 3 and 4. Split tasks, then the queued splitter runs of assistant files.
    if stops_work:
        step("tasks", tasks, "tasks")
        step("split_runs", split_runs, "split_runs")
    # 5. Queued assistant jobs (D16: an MFA reset leaves assistant_continue
    #    to its own owner check).
    if mode == "mfa_reset":
        step("jobs", lambda: jobs((llm_queue.JOB_ASSISTANT_TURN, llm_queue.JOB_ASSISTANT_APPROVAL_EXECUTE), 1),
             "jobs")
    elif leaving:
        step("jobs", lambda: jobs(tuple(llm_queue.ASSISTANT_JOB_TYPES), 2), "jobs")
    # 6. Spend holds.
    if stops_work:
        step("holds", lambda: _release_holds(user_id), "holds")
    # 7. Routines: paused, never ended (D10); a hard delete cascades them.
    if mode == "mfa_reset":
        step("schedules", lambda: _pause_schedules(user_id, "mfa_reset", widen=False), "schedules")
    elif mode in ("deactivated", "demoted_external", "sweep"):
        step("schedules", lambda: _pause_schedules(user_id, "owner_offboarded", widen=True), "schedules")
    # 8. Staging.
    if leaving:
        step("staging", staging, "files")
    # 9. Hooks (phases 4 to 6).
    for hook in list(HOOKS):
        def run_hook(hook: Callable[[str, str], dict] = hook) -> None:
            for key, value in (hook(user_id, mode) or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    result.counts[str(key)] = result.counts.get(str(key), 0) + value

        step("hooks", run_hook)

    result.ok = not any(name in CRITICAL_STEPS for name in result.failures)
    _audit(actor_id, user_id, mode, old_role, new_role, result)
    return result
