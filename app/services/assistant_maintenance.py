"""The assistant's maintenance riders (docs/ASSISTANT_PHASE3_PLAN.md 3.5).

`tick` runs on every scheduler poll (assistant_scheduler.poll_once, while
ASSISTANT_MAINTENANCE_ENABLED is on) and starts each rider once its
per-process interval has passed:

  approvals  every 5 minutes: pending cards past expires_at, of an owner
             who left, of a tool whose sub-app is off, or on a closed
             project expire; approved cards stranded without an execute
             job are recovered (the sweep phase 1 deferred);
  tasks      every 10 minutes: open split tasks whose owner has bidding off
             fail feature_off;
  staging    every 10 minutes: the bounded system sweep of staged objects
             (phase 2's deferred move);
  plans_notes_sweep  every 10 minutes (phase 4, 3.3): up to 10 recently
             finished AI Estimating runs whose notes lag their outputs are
             synced into plans_reader_notes;
  ae_task_sweep  every minute (phase 4, 3.5.4 and 3.6.3): open page reads and
             AI Estimating runs are finalized, and a composite run route that
             died short of `started` is cleaned up;
  nightly    once per Pacific day between 02:00 and 05:00: the 30-day tool
             result purge, the memory archive and purge, and the project
             note moves on merge;
  orphans    once per Pacific day: offboarding for owners whose profile is
             missing, inactive or no longer internal (deactivations and
             deletes done by SQL or in the dashboard);
  report_purge  once per Pacific day (phase 4, 3.9.3): expired report
             workbooks lose their storage object, then their row is marked
             purged once the object is verified gone; stray objects whose
             row is gone are removed.

The daily steps are claimed by inserting assistant_sweeps (step,
sweep_date), so they run once across both workers. The interval riders claim
nothing: every step of them is idempotent under two workers. Every system sweep goes
through a repo function with user_id None (pinned by
tests/test_assistant_repo_isolation). The only direct reads are BDR tables
(projects, deleted_projects, bid_outcomes, llm_jobs, profiles) with the
service-role client; this module is outside app/assistant/. No model runs
here (deviation D4).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core import supabase_client

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")

APPROVALS_EVERY_SECONDS = 5 * 60
TASKS_EVERY_SECONDS = 10 * 60
STAGING_EVERY_SECONDS = 10 * 60
NIGHTLY_WINDOW_HOURS = (2, 5)        # Pacific, [start, end)
SWEEP_LIMIT = 100
STAGING_LIMIT = 50
NIGHTLY_LIMIT = 500
ORPHAN_LIMIT = 200
NOTE_REVIEW_PAGES = 10               # live project notes read per night: up to 10 pages of NIGHTLY_LIMIT
STRANDED_SECONDS = 10 * 60
PLANS_NOTES_EVERY_SECONDS = 10 * 60
AE_TASKS_EVERY_SECONDS = 60
PLANS_NOTES_RECENT_DAYS = 14         # runs finished this recently are kept in sync
PLANS_NOTES_SCAN_LIMIT = 100         # recent runs checked per sweep
PLANS_NOTES_SYNC_LIMIT = 10          # runs synced per sweep
PLANS_NOTES_IDLE_SECONDS = 6 * 3600  # a run that wrote nothing is not retried this soon
AE_TASK_SCAN_LIMIT = 100             # open AE tasks read per sweep
AE_TASK_ADVANCE_LIMIT = 20           # tasks of eligible owners handled per sweep
REPORT_PURGE_LIMIT = 200
ORPHAN_REPORT_FOLDERS = 50           # owner prefixes checked per day (rotating)
ORPHAN_REPORT_MIN_AGE_HOURS = 24     # a younger object may be mid-upload (its row is not inserted yet)
UNUSED_DAYS = 180                    # an active unpinned note unused this long is archived
PENDING_DAYS = 30                    # a pending note untouched this long is archived (owner question 21)
ARCHIVED_DAYS = 365                  # an archived note this old is deleted

# Per-process interval state: rider name -> monotonic time of the last run,
# and step -> the Pacific date this process last tried to claim.
_last_run: dict[str, float] = {}
_claimed_dates: dict[str, str] = {}
# plans_notes_sweep: run id -> monotonic time its last sync wrote nothing.
_notes_idle: dict[str, float] = {}

_REPORT_OBJECT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.xlsx$")


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _due(name: str, every: float) -> bool:
    tick = time.monotonic()
    last = _last_run.get(name)
    if last is not None and tick - last < every:
        return False
    _last_run[name] = tick
    return True


def _role(human: dict | None) -> Any:
    from app.core.roles import INTERNAL_ROLES, Role

    if not human or not human.get("is_active"):
        return None
    try:
        role = Role(human.get("role"))
    except ValueError:
        return None
    return role if role in INTERNAL_ROLES else None


def tick(now: datetime | None = None, *, worker: str) -> dict:
    """Run every rider whose interval has passed; each is bounded, best
    effort and logged. Returns {rider: its count} for the riders that ran."""
    now = _now(now)
    out: dict[str, Any] = {}
    riders: tuple[tuple[str, float | None, Callable[[], Any]], ...] = (
        ("approvals", APPROVALS_EVERY_SECONDS, lambda: run_approvals_sweep(now)),
        ("tasks", TASKS_EVERY_SECONDS, lambda: run_tasks_sweep(now)),
        ("staging", STAGING_EVERY_SECONDS, lambda: run_staging_sweep(now)),
        ("plans_notes_sweep", PLANS_NOTES_EVERY_SECONDS, lambda: run_plans_notes_sweep(now)),
        ("ae_task_sweep", AE_TASKS_EVERY_SECONDS, lambda: run_ae_task_sweep(now)),
        ("mail_audit", 3600, lambda: run_mail_audit(now)),
        ("browser_tasks", 60, lambda: run_browser_tasks()),
        ("nightly", None, lambda: run_nightly(now, worker=worker)),
        ("orphans", None, lambda: run_orphans(now, worker=worker)),
        ("report_purge", None, lambda: run_report_purge(now, worker=worker)),
    )
    for name, every, rider in riders:
        if every is not None and not _due(name, every):
            continue
        try:
            result = rider()
        except Exception:  # noqa: BLE001 - one rider never stops the others
            logger.exception("assistant maintenance: %s failed", name)
            continue
        if result is not None:
            out[name] = result
    return out


# ── 1. Approvals ─────────────────────────────────────────────────────────────


def _closed_projects(sb: Any, project_ids: list[str]) -> set[str]:
    """The ids among these whose card can no longer run: the row is gone,
    or abandoned_at or failed_to_bid_at is set, or the stage is declined
    (the `closed` half of tools/writes.project_gate)."""
    ids = sorted({str(p) for p in project_ids if p})
    if not ids:
        return set()
    rows = (
        sb.table("projects").select("id, abandoned_at, failed_to_bid_at, current_stage")
        .in_("id", ids).execute()
    ).data or []
    found = {str(r["id"]): r for r in rows if r.get("id")}
    closed = set()
    for pid in ids:
        row = found.get(pid)
        if (row is None or row.get("abandoned_at") or row.get("failed_to_bid_at")
                or row.get("current_stage") == "declined"):
            closed.add(pid)
    return closed


def run_approvals_sweep(now: datetime | None = None) -> dict:
    """Rider 1. Pending cards past expires_at expire timed_out (no bell, the
    lazy read's own rule); pending cards whose owner is inactive or not
    internal expire owner_offboarded; pending cards whose tool's sub-app is
    off expire feature_off with the phase 1 bell; approved cards stranded
    10 minutes without a live execute job are recovered; pending cards on a
    closed or deleted project expire project_closed with the bell. A card on
    a project that moved to Verify or was submitted is left pending: that
    phase comes from GET /projects/{id} only and stays caught at approve
    and execute time."""
    from app.assistant import approvals, approvals_job, registry, repo
    from app.core import features

    now = _now(now)
    counts = {"timed_out": 0, "owner_offboarded": 0, "feature_off": 0, "stranded": 0, "project_closed": 0}
    owners: dict[str, Any] = {}

    def role_of(user_id: str) -> Any:
        if user_id not in owners:
            owners[user_id] = _role(repo.human_profile(user_id))
        return owners[user_id]

    for row in repo.pending_approvals_due(None, limit=SWEEP_LIMIT):
        user_id, approval_id = str(row["user_id"]), str(row["id"])
        try:
            expires = _ts(row.get("expires_at"))
            if expires is not None and expires <= now:
                after = repo.expire_if_due(user_id, approval_id)
                if after and after.get("status") == "expired":
                    repo.settle_message(user_id, row.get("message_id"))
                    counts["timed_out"] += 1
                continue
            role = role_of(user_id)
            if role is None:
                for moved in repo.expire_approvals(user_id, [approval_id], "owner_offboarded"):
                    repo.settle_message(user_id, moved.get("message_id") or row.get("message_id"))
                    counts["owner_offboarded"] += 1
                continue
            spec = registry.get(str(row.get("action_kind") or ""))
            if spec is not None and spec.sub_app and not features.enabled_map(role).get(spec.sub_app, False):
                if approvals_job._expire(user_id, row, "feature_off", expect="pending"):
                    counts["feature_off"] += 1
        except Exception:  # noqa: BLE001 - one bad row never stops the sweep
            logger.exception("assistant maintenance: approval %s sweep failed", approval_id)

    for row in repo.approved_stranded(None, older_than_seconds=STRANDED_SECONDS, limit=SWEEP_LIMIT):
        try:
            after = approvals.recover_stranded(str(row["user_id"]), row, now)
            if after and after.get("status") != "approved":
                counts["stranded"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("assistant maintenance: stranded approval %s failed", row.get("id"))

    with_project = repo.pending_approvals_with_project(None, limit=SWEEP_LIMIT)
    if with_project:
        closed = _closed_projects(supabase_client.get_supabase(), [r.get("project_id") for r in with_project])
        for row in with_project:
            if str(row.get("project_id")) not in closed:
                continue
            try:
                if approvals_job._expire(str(row["user_id"]), row, "project_closed", expect="pending"):
                    counts["project_closed"] += 1
            except Exception:  # noqa: BLE001
                logger.exception("assistant maintenance: closed-project card %s failed", row.get("id"))
    return counts


# ── 2. Tasks ─────────────────────────────────────────────────────────────────


def run_tasks_sweep(now: datetime | None = None) -> int:
    """Rider 2: open split tasks whose owner has bidding off fail
    feature_off through continue_job.stop_for_owner (queued runs canceled,
    hold released, the bell). Nothing to do while bidding is on for every
    role, which is the deployment flag. The cross-user task read is the
    repo system sweep `open_split_tasks(None, limit=...)`."""
    from app.assistant import access, continue_job, repo
    from app.core import features

    if features.enabled_map(None).get("bidding", False):
        return 0
    stopped = 0
    for task in repo.open_split_tasks(None, limit=SWEEP_LIMIT):
        try:
            user_id = str(task["user_id"])
            role = _role(repo.human_profile(user_id))
            if role is not None and features.enabled_map(role).get("bidding", False):
                continue
            continue_job.stop_for_owner(user_id, task, access.FEATURE_OFF)
            stopped += 1
        except Exception:  # noqa: BLE001
            logger.exception("assistant maintenance: task %s sweep failed", task.get("id"))
    return stopped


# ── 3. Staging ───────────────────────────────────────────────────────────────


def run_staging_sweep(now: datetime | None = None) -> int:
    """Rider 3: the bounded system sweep of staged objects (consumed or
    expired rows lose their storage object; owners on legal hold skipped)."""
    from app.services import assistant_staging

    return assistant_staging.purge_expired(user_id=None, limit=STAGING_LIMIT)


# ── 4. Nightly ───────────────────────────────────────────────────────────────


def _first_try_today(step: str, now: datetime) -> str | None:
    """The Pacific date when this process has not settled its claim of
    `step` today yet, else None. The date is recorded by _claim_today only
    once claim_sweep answered, so a transient error retries next poll."""
    day = now.astimezone(PT).date().isoformat()
    if _claimed_dates.get(step) == day:
        return None
    return day


def _claim_today(step: str, now: datetime, *, worker: str) -> str | None:
    """The Pacific date when this call claimed `step` for today, else None.
    One settled claim attempt per step per day per process: True or False
    from claim_sweep records the date; an exception leaves it unset, and a
    retry is safe (an insert that did commit hits the key and is False)."""
    from app.assistant import repo

    day = _first_try_today(step, now)
    if day is None:
        return None
    claimed = repo.claim_sweep(None, step, day, worker=worker)
    _claimed_dates[step] = day
    return day if claimed else None


def _review_project_notes(sb: Any) -> tuple[list[dict], list[dict]]:
    """(moves, archives) over the live project notes, read page by page up
    to NOTE_REVIEW_PAGES pages, each list capped at NIGHTLY_LIMIT."""
    from app.assistant import repo

    moves: list[dict] = []
    archives: list[dict] = []
    for page in range(NOTE_REVIEW_PAGES):
        notes = repo.project_notes_for_review(None, limit=NIGHTLY_LIMIT, offset=page * NIGHTLY_LIMIT)
        page_moves, page_archives = _project_note_actions(sb, notes)
        moves += page_moves
        archives += page_archives
        if len(notes) < NIGHTLY_LIMIT or (len(moves) >= NIGHTLY_LIMIT and len(archives) >= NIGHTLY_LIMIT):
            break
    return moves[:NIGHTLY_LIMIT], archives[:NIGHTLY_LIMIT]


def _delete_tool_results(rows: list[dict]) -> int:
    from app.assistant import repo

    by_owner: dict[str, list[str]] = {}
    for row in rows:
        by_owner.setdefault(str(row["user_id"]), []).append(str(row["id"]))
    deleted = 0
    for user_id, ids in by_owner.items():
        try:
            deleted += repo.delete_tool_results(user_id, ids)
        except Exception:  # noqa: BLE001
            logger.exception("assistant maintenance: tool result purge failed for %s", user_id)
    return deleted


def _apply_each(rows: list[dict], op: str, fields_for: Callable[[dict], dict] | None = None) -> int:
    """memory_apply(op, actor 'system') per row; a refusal (legal hold, a
    row changed since the read) skips that row."""
    from app.assistant import repo

    done = 0
    for row in rows:
        try:
            repo.memory_apply(
                str(row["user_id"]), op, actor="system", memory_id=str(row["id"]),
                expected_updated_at=row.get("updated_at") if op == "archive" else None,
                fields=fields_for(row) if fields_for else None,
            )
            done += 1
        except repo.MemoryRefused as exc:
            logger.info("assistant maintenance: %s of memory %s refused (%s)", op, row.get("id"), exc.code)
        except Exception:  # noqa: BLE001
            logger.exception("assistant maintenance: %s of memory %s failed", op, row.get("id"))
    return done


def _project_note_actions(sb: Any, notes: list[dict]) -> tuple[list[dict], list[dict]]:
    """(moves, archives) for live project notes: a gone project merged into
    one that still exists moves the note there; any other gone project, and
    a project abandoned, failed to bid or lost (bid_outcomes), archives it."""
    ids = sorted({str(n["project_id"]) for n in notes if n.get("project_id")})
    if not ids:
        return [], []
    projects = {
        str(r["id"]): r for r in (
            sb.table("projects").select("id, abandoned_at, failed_to_bid_at").in_("id", ids).execute()
        ).data or [] if r.get("id")
    }
    lost = {
        str(r["project_id"]) for r in (
            sb.table("bid_outcomes").select("project_id, result").in_("project_id", ids).execute()
        ).data or [] if r.get("result") == "lost"
    }
    gone = [pid for pid in ids if pid not in projects]
    merged: dict[str, str] = {}
    if gone:
        for r in (
            sb.table("deleted_projects").select("project_id, reason, merged_into_project_id")
            .in_("project_id", gone).execute()
        ).data or []:
            target = r.get("merged_into_project_id")
            if r.get("reason") == "merged" and target:
                merged[str(r["project_id"])] = str(target)
        targets = sorted(set(merged.values()))
        alive = {
            str(r["id"]) for r in (
                sb.table("projects").select("id").in_("id", targets).execute()
            ).data or []
        } if targets else set()
        merged = {pid: target for pid, target in merged.items() if target in alive}
    moves, archives = [], []
    for note in notes:
        pid = str(note.get("project_id") or "")
        if not pid:
            continue
        if pid in merged:
            moves.append({**note, "to": merged[pid]})
        elif pid not in projects:
            archives.append(note)
        else:
            row = projects[pid]
            if row.get("abandoned_at") or row.get("failed_to_bid_at") or pid in lost:
                archives.append(note)
    return moves, archives


def run_nightly(now: datetime | None = None, *, worker: str) -> dict | None:
    """Rider 4, once per Pacific day between 02:00 and 05:00, claimed in
    assistant_sweeps (a duplicate key means the other worker has it): (a)
    tool results past expires_at deleted, owners on legal hold excluded;
    (b) memories: active unpinned rows unused for 180 days and pending rows
    untouched for 30 days archived, archived rows older than a year deleted
    (legal hold excluded, and the delete op refuses it too); (c) project
    notes moved on a merge or archived when the project is gone, abandoned,
    failed to bid or lost. Each sub-step is bounded at 500 rows; the counts
    go into assistant_sweeps.result. None when it did not run."""
    from app.assistant import repo

    now = _now(now)
    start, end = NIGHTLY_WINDOW_HOURS
    if not start <= now.astimezone(PT).hour < end:
        return None
    day = _claim_today("nightly", now, worker=worker)
    if day is None:
        return None
    result: dict[str, Any] = {}
    steps: tuple[tuple[str, Callable[[], int]], ...] = (
        ("tool_results_deleted", lambda: _delete_tool_results(
            repo.tool_results_to_purge(None, limit=NIGHTLY_LIMIT))),
        ("memories_archived", lambda: _apply_each(repo.memories_to_auto_archive(
            None, unused_days=UNUSED_DAYS, pending_days=PENDING_DAYS, limit=NIGHTLY_LIMIT), "archive")),
        ("memories_deleted", lambda: _apply_each(
            repo.memories_to_purge(None, archived_days=ARCHIVED_DAYS, limit=NIGHTLY_LIMIT), "delete")),
    )
    for name, step in steps:
        try:
            result[name] = step()
        except Exception:  # noqa: BLE001 - one sub-step never stops the others
            logger.exception("assistant maintenance: nightly %s failed", name)
            result[name] = None
    try:
        moves, archives = _review_project_notes(supabase_client.get_supabase())
        result["notes_moved"] = _apply_each(moves, "move", lambda n: {"project_id": n["to"]})
        result["notes_archived"] = _apply_each(archives, "archive")
    except Exception:  # noqa: BLE001
        logger.exception("assistant maintenance: nightly project notes failed")
        result["notes_moved"] = result["notes_archived"] = None
    try:
        repo.finish_sweep(None, "nightly", day, result)
    except Exception:  # noqa: BLE001
        logger.exception("assistant maintenance: nightly result could not be recorded")
    return result


# ── 5. Orphans ───────────────────────────────────────────────────────────────


def _orphan_job_owners(sb: Any) -> set[str]:
    """Owners named by queued assistant jobs (llm_jobs payload user_id), so a
    job whose owner row is gone is canceled too."""
    from app.services import llm_queue

    rows = (
        sb.table("llm_jobs").select("id, payload").in_("job_type", list(llm_queue.ASSISTANT_JOB_TYPES))
        .eq("status", "queued").limit(ORPHAN_LIMIT).execute()
    ).data or []
    return {str((r.get("payload") or {}).get("user_id")) for r in rows
            if isinstance(r.get("payload"), dict) and (r.get("payload") or {}).get("user_id")}


def run_orphans(now: datetime | None = None, *, worker: str) -> dict | None:
    """Rider 5, once per Pacific day, claimed in assistant_sweeps: every
    owner with open assistant state (a live delegation, a pending or
    approved card, an active schedule, an open task, an active hold, or a
    queued assistant job) or with phase 7 state (an own subscription
    override, a project mute, an undelivered inbox row, a pending external
    send) whose profile is missing, inactive or not internal is offboarded
    in mode 'sweep'. Covers deactivations and deletes done outside
    app/routers/users.py within a day (offboarding map G2). None when it
    did not run."""
    from app.assistant import repo
    from app.services import assistant_offboarding

    now = _now(now)
    day = _claim_today("orphans", now, worker=worker)
    if day is None:
        return None
    owners = set(repo.owners_with_open_state(None, limit=ORPHAN_LIMIT))
    try:
        owners |= _orphan_job_owners(supabase_client.get_supabase())
    except Exception:  # noqa: BLE001 - the state rows still cover most cases
        logger.exception("assistant maintenance: queued job owners could not be read")
    try:
        owners |= set(repo.phase7_owners_with_open_state(None, limit=ORPHAN_LIMIT))
    except Exception:  # noqa: BLE001 - the other owners are still checked
        logger.exception("assistant maintenance: phase 7 owners could not be read")
    result = {"checked": 0, "offboarded": 0, "failed": 0}
    for user_id in sorted(owners):
        result["checked"] += 1
        try:
            if _role(repo.human_profile(user_id)) is not None:
                continue
            outcome = assistant_offboarding.offboard(user_id, "sweep", actor_id=None)
            result["offboarded"] += 1
            if not outcome.ok:
                result["failed"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("assistant maintenance: orphan check of %s failed", user_id)
            result["failed"] += 1
    try:
        repo.finish_sweep(None, "orphans", day, result)
    except Exception:  # noqa: BLE001
        logger.exception("assistant maintenance: orphan result could not be recorded")
    return result


# ── 6. Plans notes (phase 4, 3.3) ────────────────────────────────────────────


def run_plans_notes_sweep(now: datetime | None = None) -> dict:
    """Rider 6, every 10 minutes per process, no database claim: among the
    runs finished in the last 14 days (newest first, at most 100), the ones
    whose plans_reader_notes lag their outputs (`notes_behind`) are synced,
    up to 10 per sweep. A run whose sync wrote nothing (an output that
    flattens to no rows) stays "behind" by definition, so it is skipped for
    6 hours instead of taking a slot every sweep. Idempotent under two
    workers: the upsert ignores duplicates and the version removal only
    drops lower versions. Counts only."""
    from app.services import plans_reader_notes

    now = _now(now)
    mono = time.monotonic()
    for run_id in [r for r, at in _notes_idle.items() if mono - at >= PLANS_NOTES_IDLE_SECONDS]:
        _notes_idle.pop(run_id, None)
    sb = supabase_client.get_supabase()
    since = (now - timedelta(days=PLANS_NOTES_RECENT_DAYS)).isoformat()
    rows = (
        sb.table("plans_reader_runs").select("id, finished_at")
        .not_.is_("finished_at", "null").gte("finished_at", since)
        .order("finished_at", desc=True).limit(PLANS_NOTES_SCAN_LIMIT).execute()
    ).data or []
    ids = [str(r["id"]) for r in rows if r.get("id") and str(r["id"]) not in _notes_idle]
    behind = plans_reader_notes.notes_behind(sb, ids)
    counts = {"checked": len(ids), "behind": len(behind), "synced": 0, "written": 0, "removed": 0, "failed": 0}
    for run_id in behind[:PLANS_NOTES_SYNC_LIMIT]:
        try:
            result = plans_reader_notes.sync_run(sb, run_id, max_rows=None) or {}
        except Exception:  # noqa: BLE001 - one run never stops the sweep
            logger.exception("assistant maintenance: plans notes sync of %s failed", run_id)
            counts["failed"] += 1
            continue
        written, removed = int(result.get("written") or 0), int(result.get("removed") or 0)
        counts["synced"] += 1
        counts["written"] += written
        counts["removed"] += removed
        if written == 0 and removed == 0:
            _notes_idle[run_id] = mono
    return counts


# ── 7. AI Estimating tasks (phase 4, 3.5.4 and 3.6.3) ────────────────────────


def _stranded_route(task: dict, now: datetime) -> bool:
    """An assistant-started run whose composite route died: its phase is
    short of `started` and the task has not moved for 10 minutes (the route
    itself is bounded at 60 seconds)."""
    if task.get("kind") != "ai_estimating_run":
        return False
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    if payload.get("phase") in (None, "", "started"):
        return False
    moved = _ts(task.get("updated_at")) or _ts(task.get("created_at"))
    return moved is not None and (now - moved).total_seconds() >= STRANDED_SECONDS


def _clean_stranded_route(user_id: str, task: dict) -> bool:
    """Cancel the run the dead route created (when one exists and is still
    live), release the hold with the cost so far (0 before start) and fail
    the task route_interrupted with a message and a bell. A run that already
    ended is finalized as usual instead. A cancel that fails for another
    reason leaves everything for the next sweep."""
    from app.assistant import plans_tasks
    from app.services import assistant_ai_estimate, assistant_offboarding

    run_id = plans_tasks.run_id_of(task)
    cost: Any = "0"
    if run_id:
        state = assistant_ai_estimate.run_state(None, run_id)
        if state.get("status") in plans_tasks.TERMINAL_RUN_STATUSES:
            return plans_tasks.advance(user_id, task)
        if state.get("status") != "missing":
            assistant_offboarding.cancel_ae_run(run_id, user_id)
            cost = assistant_ai_estimate.run_state(None, run_id).get("cost_usd") or "0"
    changed, _closed = plans_tasks.stop_task(
        user_id, task, plans_tasks.FAILED_ROUTE, actual_usd=cost,
        text=plans_tasks.TEXT_ROUTE_INTERRUPTED.format(number=plans_tasks.project_number(task)),
    )
    return changed


def run_ae_task_sweep(now: datetime | None = None) -> dict:
    """Rider 7, every minute per process, no database claim: the open page
    reads and AI Estimating runs (`repo.open_ae_tasks(None)`, oldest first,
    at most 100 read) are finalized through plans_tasks.advance, and a
    stranded composite route is cleaned up. Owners with no live delegation
    (or a dead account or switch) are skipped without a message or bell;
    offboarding already stopped their tasks, and their holds expire by
    themselves. At most 20 tasks of eligible owners are handled per sweep.
    Every step is fenced on the task status and the active hold, so two
    workers may run it at once."""
    from app.assistant import access, plans_tasks, repo

    now = _now(now)
    counts = {"checked": 0, "advanced": 0, "stranded": 0, "skipped_owner": 0, "failed": 0}
    eligible: dict[str, bool] = {}
    for task in repo.open_ae_tasks(None, limit=AE_TASK_SCAN_LIMIT):
        if counts["checked"] >= AE_TASK_ADVANCE_LIMIT:
            break
        user_id = str(task["user_id"])
        try:
            if user_id not in eligible:
                eligible[user_id] = access.owner_check(user_id, sub_app=None).problem is None
            if not eligible[user_id]:
                counts["skipped_owner"] += 1
                continue
            counts["checked"] += 1
            if _stranded_route(task, now):
                counts["stranded"] += int(_clean_stranded_route(user_id, task))
            elif plans_tasks.advance(user_id, task):
                counts["advanced"] += 1
        except Exception:  # noqa: BLE001 - one bad task never stops the sweep
            logger.exception("assistant maintenance: AE task %s sweep failed", task.get("id"))
            counts["failed"] += 1
    return counts


# ── 8. Report purge (phase 4, 3.9.3) ─────────────────────────────────────────


def _remove_orphan_reports(now: datetime) -> int:
    """Objects under assistant-reports/ whose row is gone: for ORPHAN_REPORT_
    FOLDERS owner prefixes a day (rotating through all of them), every object
    that is a report file name, at least 24 hours old (a younger one may be
    mid-upload, row not inserted yet) and not the id of one of the owner's
    ready rows is deleted. Owners on legal hold are skipped; an owner with
    1000 or more ready rows is skipped (the comparison would be partial)."""
    from app.assistant import repo
    from app.services import assistant_offboarding as off
    from app.services import storage

    store = supabase_client.get_supabase().storage.from_(storage.BUCKET)
    folders = sorted(
        str(e["name"]) for e in (store.list(off.REPORT_PREFIX, {"limit": 1000, "offset": 0}) or [])
        if e.get("name")
    )
    if not folders:
        return 0
    start = (now.astimezone(PT).date().toordinal() * ORPHAN_REPORT_FOLDERS) % len(folders)
    window = [folders[(start + i) % len(folders)] for i in range(min(ORPHAN_REPORT_FOLDERS, len(folders)))]
    cutoff = now - timedelta(hours=ORPHAN_REPORT_MIN_AGE_HOURS)
    removed = 0
    for owner in window:
        if removed >= REPORT_PURGE_LIMIT:
            break
        try:
            if repo.legal_hold(owner):
                continue
            ready = repo.owner_reports(owner, statuses=("ready",), limit=1000)
            if len(ready) >= 1000:
                continue
            known = {f"{r['id']}.xlsx" for r in ready}
            for name, created in off.report_objects(owner).items():
                made = _ts(created)
                if name in known or not _REPORT_OBJECT.match(name) or made is None or made > cutoff:
                    continue
                storage.delete_file(f"{off.REPORT_PREFIX}/{owner}/{name}")
                removed += 1
        except Exception:  # noqa: BLE001 - one owner prefix never stops the pass
            logger.exception("assistant maintenance: orphan report pass failed for %s", owner)
    return removed


def run_report_purge(now: datetime | None = None, *, worker: str) -> dict | None:
    """Rider 8, once per Pacific day, claimed in assistant_sweeps (0180 B5):
    up to 200 expired ready report rows (never an owner on legal hold) lose
    their storage object and are marked purged only once a listing of the
    owner's prefix shows it gone; a still-present object is retried the next
    day and logged. Then the orphan pass. The counts are returned and logged,
    not written to assistant_sweeps.result: the isolation test pins the
    callers of repo.finish_sweep to run_nightly and run_orphans. None when it
    did not run."""
    from app.assistant import repo
    from app.services import assistant_offboarding

    now = _now(now)
    day = _claim_today("report_purge", now, worker=worker)
    if day is None:
        return None
    result: dict[str, Any] = {}
    try:
        rows = repo.reports_to_purge(None, limit=REPORT_PURGE_LIMIT)
        result["purged"], result["left"] = assistant_offboarding.purge_reports(rows)
    except Exception:  # noqa: BLE001
        logger.exception("assistant maintenance: report purge failed")
        result["purged"] = result["left"] = None
    try:
        result["orphans_removed"] = _remove_orphan_reports(now)
    except Exception:  # noqa: BLE001
        logger.exception("assistant maintenance: orphan report pass failed")
        result["orphans_removed"] = None
    logger.info("assistant maintenance: report purge %s: %s", day, result)
    return result


def run_mail_audit(now: datetime | None = None) -> dict:
    """Count-only weekly review digest and effective exclusion audit."""
    from app.services import assistant_mail_audit
    return assistant_mail_audit.tick(now=_now(now))


def run_browser_tasks() -> dict:
    """Expire private work and purge expired envelopes, even with execution off."""
    from app.assistant import repo
    from app.services import assistant_browser_records, notifications

    counts = repo.browser_rpc(None, "assistant_browser_sweep", {"p_limit": 100})
    notices = repo.browser_rpc(None, "assistant_browser_notice_claim", {"p_limit": 100})
    rows = notices.get("rows") or []
    if rows:
        notifications.deliver(rows, email=True)
    counts["notices"] = len(rows)
    counts["retention"] = assistant_browser_records.purge_expired_records(limit=100)
    return counts
