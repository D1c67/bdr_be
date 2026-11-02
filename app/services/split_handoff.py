"""Assistant splitter handoff (docs/ASSISTANT_PHASE2_PLAN.md 3.3).

The service behind story 4: staged PDFs from the assistant panel are split
into a project by the Bid File Splitter (`split_drop`), a finished manual
splitter job is filed into a project (`attach`), and the outcome of a split
task is read back per file (`job_outcome`, `task_progress`).

All functions are sync and called from plain `def` routes or from the
threadpool. They reach assistant tables only through `app.assistant.repo`
(lazy import). Every entry point that a module under `app/assistant/` calls
creates its own client inside the service (`task_progress_for(task)`,
`job_outcome_for(job_id)`, `status_for(user, project_id)`), so no module
under `app/assistant/` ever needs `get_supabase`. The `(sb, ...)` forms are
the internal implementations.

`app.main` never imports this module (the projects routes import it on
first use), and this module imports `app.assistant` only inside functions.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from app.core import provenance, supabase_client
from app.core.config import get_settings
from app.services import file_hashes, llm, llm_pricing, storage
from app.services.notifications import audit

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.deps import CurrentUser

logger = logging.getLogger(__name__)

# 3.3.1 Constants.
PER_CARD_MAX_FILES = 10
PER_CARD_MAX_PAGES = 400
ATTACH_MAX_FILES = 10
ATTACH_MAX_SOURCE_BYTES = 1536 * 1024 * 1024  # attach re-files in the request (source sets are re-uploaded)
SPLIT_MAX_ATTEMPTS = 2                      # queue attempts per assistant file (the default is 6, config.py:397)
MISSING_GRACE_SECONDS = 15 * 60             # a done/failed file with no rows this long after finished_at is "missing"
PENDING_TASK_REPAIR_SECONDS = 10 * 60       # a task still "pending" this long is failed by lazy repair
SPLIT_TASK_TTL_SECONDS = 6 * 3600           # the hold outlives a long run; above STALE_RUN_SECONDS (bid_split.py:102)
RESERVE_MARGIN = Decimal("1.25")            # one in-run retry per call is common (bid_split.py:778-816, :919-966)
TOKENS_PER_PAGE = 2300                      # 1568 px JPEG q70 (config.py:286-287), measured on dev 2026-10-22
TOKENS_FIXED_PER_CALL = 1500
OUTPUT_TOKENS_TRIAGE = 300
OUTPUT_TOKENS_BATCH = 900
NAMING_INPUT_TOKENS, NAMING_OUTPUT_TOKENS = 3000, 800

# The status_for lists (3.10.3).
STATUS_MAX_STAGED = 20
STATUS_MAX_JOBS = 10
STATUS_MAX_TASKS = 5
STATUS_JOB_DAYS = 14

TASK_KIND = "split_into_project"
SPLIT_TOOL = "split_files_into_project"
# Task statuses whose split_drop_out the by-key recovery route reports (3.4):
# the task has a job and is past `pending`.
RECOVERABLE_TASK_STATUSES = ("waiting_external", "running", "done", "failed")

# File outcomes (3.3.7, 5.2) and whole-file reasons.
OUTCOME_PENDING = "pending"
OUTCOME_SPLIT = "split"
OUTCOME_WHOLE = "whole"
OUTCOME_FAILED = "failed"
OUTCOME_MISSING = "missing"
_LANDING = "landing"   # internal: shows as `pending` in `outcome`
REASON_PACKAGE_SENT = "package_sent"
REASON_RUN_FAILED = "run_failed"
REASON_REFILE_FAILED = "refile_failed"
REASON_NO_DOCUMENTS = "no_documents"
REASON_OTHER = "other"

AUDIT_SPLIT_STARTED = "assistant.split_started"
AUDIT_SPLIT_ATTACHED = "assistant.split_attached"

_CENT = Decimal("0.01")
_ZERO = Decimal("0")
_JOBS = "bid_split_jobs"
_FILES = "bid_split_files"
_SEGMENTS = "bid_split_segments"
_PROJECT_FILES = "project_files"
_SETTLED_JOB_STATUSES = ("done", "done_with_errors", "failed")
_ATTACHABLE_JOB_STATUSES = ("done", "done_with_errors")
_SOURCE_MANUAL = "manual"
_SOURCE_PROJECT = "project"

MSG_BAD_FILE_IDS = "Name between 1 and {max} staged files, each once."
MSG_FILE_NOT_AVAILABLE = (
    "One or more files are no longer staged, or were not attached in this conversation."
)
MSG_TOO_MANY_PAGES = "These files have {pages} pages; one split takes at most {max} pages."
MSG_HANDOFF_LOCKED = (
    "The hand-off package for this project was already sent, so files cannot be split into it. "
    "Changes go through Revisions on the To Estimator step."
)
MSG_DUPLICATE = "One or more files are already on this project."
MSG_PRICE_UNKNOWN = "The cost of this split cannot be estimated for the splitter's current model."
MSG_PRICE_CHANGED = "The split would now cost up to ${estimate}, above the approved ${max}."
MSG_SPEND_CAP = "The split does not fit under the {scope} daily AI spend cap (${remaining} remaining)."
MSG_STAGE_FAILED = "The files could not be handed to the Bid File Splitter. Nothing was charged."
MSG_NOT_FOUND = "Not Found"
MSG_PROJECT_NOT_FOUND = "Project not found."
MSG_JOB_NOT_FOUND = "That splitter job was not found."
MSG_SOURCE_NOT_ATTACHABLE = "Only a job started on the Bid Splitter page can be filed into a project."
MSG_JOB_NOT_FINISHED = "That splitter job has not finished yet."
MSG_NOTHING_TO_FILE = "That splitter job has no finished file to file."
MSG_JOB_TOO_LARGE = (
    "That splitter job is too large to file in one step (at most {files} finished files and "
    "{gb} GB of source PDFs)."
)
MSG_JOB_ALREADY_LINKED = "That splitter job is already filed into another project."
MSG_JOB_ATTACH_LOCKED = (
    "The hand-off package for this project was already sent, so a splitter job cannot be filed "
    "into it. Changes go through Revisions on the To Estimator step."
)
MSG_QUEUE_WHY = "the job queue is off"


class SplitRefused(Exception):
    """A business refusal of split-drop or attach. The route wraps it into
    `HTTPException(status, {"code", "message", **detail})` with the
    `X-Error-Code` header (the phase 1 `labor_changed` pattern)."""

    def __init__(self, code: str, message: str, status: int = 409, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


@dataclass(frozen=True)
class SplitEstimate:
    amount: Decimal   # worst case, times RESERVE_MARGIN, rounded up to the cent; 0 on the self-hosted route
    model: str
    provider: str


# ── Small helpers ────────────────────────────────────────────────────────────


def _sb():
    return supabase_client.get_supabase()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def usd(value: Any) -> str:
    """Money as a string with cents (never a float). Remaining amounts are
    floored to the cent, so the card never shows more than is left."""
    amount = _dec(value)
    if amount is None:
        amount = _ZERO
    return str(max(amount, _ZERO).quantize(_CENT, rounding=ROUND_FLOOR))


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def clean_ids(file_ids: Any) -> list[str] | None:
    """The distinct uuids in order, or None when the list is empty, too
    long, repeats an id or holds anything that is not a uuid."""
    if not isinstance(file_ids, (list, tuple)):
        return None
    out: list[str] = []
    for raw in file_ids:
        if not isinstance(raw, str) or not _is_uuid(raw.strip()):
            return None
        value = str(uuid.UUID(raw.strip()))
        if value in out:
            return None
        out.append(value)
    if not 1 <= len(out) <= PER_CARD_MAX_FILES:
        return None
    return out


def _actor_run_id(user: CurrentUser) -> str | None:
    actor = provenance.current()
    if actor is not None and actor.user_id == str(user.id):
        return actor.run_id
    return getattr(user, "via_assistant_run_id", None)


def _project(sb, project_id: str) -> dict | None:
    rows = sb.table("projects").select("id, number, name").eq("id", project_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _project_ref(project: dict | None, project_id: str | None) -> dict | None:
    if not project_id:
        return None
    number = (project or {}).get("number")
    return {"id": str(project_id), "number": str(number) if number else None}


def splitter_available(settings: Settings | None = None) -> tuple[bool, str | None]:
    """`(available, why not)`: the drop zone's splitter state, and the job
    queue on (split-drop enqueues directly and never runs a split in this
    process, 3.3.4 step 4)."""
    from app.services import rfp_file_drop, rfp_split

    s = settings or get_settings()
    available, reason = rfp_file_drop.splitter_state(s)
    if not available:
        return False, reason
    if not getattr(s, "llm_queue_enabled", False):
        return False, rfp_split.MSG_QUEUE_OFF
    return True, None


def _handoff_locked(project_id: str) -> bool:
    from app.routers import files as files_router

    return bool(files_router.handoff_locked(project_id))


# ── 3.3.2 The estimate ───────────────────────────────────────────────────────


def estimate_split_usd(pages_per_file: list[int], settings: Settings | None = None) -> SplitEstimate | None:
    """Worst case (every file a drawing set): per file one triage call over
    `min(pages, bid_split_triage_pages)` pages, `ceil(pages /
    bid_split_pages_per_call)` batch calls and one naming call, each priced
    with `llm_pricing.estimate_cost(model, input_tokens=..., output_tokens=...)`
    where `model = llm.active_model("bid_split", s)`. Sum times
    RESERVE_MARGIN, rounded up to the cent. The self-hosted route gives
    amount 0; any other unpriced model returns None and every caller
    refuses (never estimate 0). One displayed number, "up to $X.XX", is
    also the reservation amount and the approved ceiling (`max_usd`)."""
    s = settings or get_settings()
    try:
        route = llm.resolve("bid_split", s)
        model = llm.active_model("bid_split", s)
    except Exception:  # noqa: BLE001 - an unreadable route has no price
        logger.exception("split handoff: the bid_split route could not be resolved")
        return None
    provider = str(route.provider or "")
    if provider == "self_hosted":
        return SplitEstimate(_ZERO.quantize(_CENT), str(model), provider)
    triage_pages = max(1, _int(getattr(s, "bid_split_triage_pages", 12)))
    per_call = max(1, _int(getattr(s, "bid_split_pages_per_call", 8)))
    calls: list[tuple[int, int]] = []
    for raw in pages_per_file:
        pages = max(1, _int(raw))
        calls.append((min(pages, triage_pages) * TOKENS_PER_PAGE + TOKENS_FIXED_PER_CALL, OUTPUT_TOKENS_TRIAGE))
        for start in range(0, pages, per_call):
            batch = min(per_call, pages - start)
            calls.append((batch * TOKENS_PER_PAGE + TOKENS_FIXED_PER_CALL, OUTPUT_TOKENS_BATCH))
        calls.append((NAMING_INPUT_TOKENS, NAMING_OUTPUT_TOKENS))
    total = _ZERO
    for input_tokens, output_tokens in calls:
        cost = llm_pricing.estimate_cost(str(model), input_tokens=input_tokens, output_tokens=output_tokens)
        if cost is None:
            return None
        total += cost
    amount = (total * RESERVE_MARGIN).quantize(_CENT, rounding=ROUND_CEILING)
    return SplitEstimate(amount, str(model), provider)


# ── 3.3.3 Duplicates ─────────────────────────────────────────────────────────


def duplicates(sb, project_id: str, staged: list[dict]) -> dict[str, dict]:
    """Staged rows whose bytes the project already holds:
    `{assistant_file_id: {project_file_id, filename, category}}`. Selects the
    project's `project_files` rows whose `size_bytes` is among the staged
    sizes, fills missing sha256 with `file_hashes.fill(..., budget_s=10)`,
    and matches on sha256. A row the budget could not hash is not a
    duplicate (fail open)."""
    sizes = sorted({_int(r.get("size_bytes")) for r in staged if r.get("size_bytes") is not None})
    if not sizes:
        return {}
    rows = (
        sb.table(_PROJECT_FILES).select("id, filename, category, size_bytes, sha256, storage_path")
        .eq("project_id", project_id).in_("size_bytes", sizes).order("created_at").execute()
    ).data or []
    unhashed = [r for r in rows if file_hashes.needs_hash(r)]
    if unhashed:
        file_hashes.fill(sb, unhashed, budget_s=10)
    out: dict[str, dict] = {}
    for st in staged:
        sha = st.get("sha256")
        if not sha:
            continue
        for row in rows:
            if row.get("sha256") == sha and _int(row.get("size_bytes")) == _int(st.get("size_bytes")):
                out[str(st["id"])] = {
                    "project_file_id": str(row["id"]), "filename": row.get("filename"),
                    "category": row.get("category"),
                }
                break
    return out


# ── 3.3.5 The split card's read ──────────────────────────────────────────────


def _file_problem(row: dict | None, *, now: datetime, conversation_id: str | None,
                  open_ids: set[str]) -> str | None:
    if row is None:
        return "not_found"
    if row.get("consumed_at") or row.get("consumed_task_id"):
        return "consumed"
    expires = _parse_ts(row.get("expires_at"))
    if row.get("purged_at") or expires is None or expires <= now:
        return "expired"
    if not row.get("message_id"):
        return "not_attached"
    if conversation_id is not None and str(row.get("conversation_id") or "") != conversation_id:
        return "other_conversation"
    if str(row.get("id")) in open_ids:
        return "in_open_card"
    return None


def _spend_block(user_id: str) -> dict:
    from app.assistant import spend

    st = spend.state(str(user_id))
    return {"user_remaining_usd": usd(st.user_remaining), "global_remaining_usd": usd(st.global_remaining)}


def split_preview(sb, project_id: str, user: CurrentUser, file_ids: list[str]) -> dict:
    """The read the split card is built from (3.3.5). Never raises for
    business reasons: it reports. `{project: {id, number, name},
    handoff_locked, splitter: {available, reason}, files: [{file_id,
    filename, pages, size_bytes, sha256, problem, duplicate_of}], totals:
    {files, pages}, limits: {max_files, max_pages}, estimate: {usd, model,
    provider, priced}, spend: {user_remaining_usd, global_remaining_usd}}`.
    `problem` is null or one of not_found, consumed, expired, not_attached,
    other_conversation, in_open_card (not_owner is reported as not_found).

    Raises SplitRefused only for a request it cannot read: bad_file_ids
    (400) and project_not_found (404)."""
    from app.assistant import repo

    ids = clean_ids(file_ids)
    if ids is None:
        raise SplitRefused("bad_file_ids", MSG_BAD_FILE_IDS.format(max=PER_CARD_MAX_FILES), 400)
    project = _project(sb, project_id)
    if project is None:
        raise SplitRefused("project_not_found", MSG_PROJECT_NOT_FOUND, 404)
    s = get_settings()
    rows = {str(r["id"]): r for r in repo.staged_files_by_ids(user.id, ids)}
    open_ids = repo.open_split_card_file_ids(user.id)
    run_id = _actor_run_id(user)
    conversation_id = repo.conversation_for_run(user.id, run_id) if run_id else None
    now = _now()
    problems = {
        fid: _file_problem(rows.get(fid), now=now, conversation_id=conversation_id, open_ids=open_ids)
        for fid in ids
    }
    live = [rows[fid] for fid in ids if fid in rows and problems[fid] in (None, "in_open_card")]
    dupes = duplicates(sb, project_id, live)
    files = []
    for fid in ids:
        row = rows.get(fid)
        files.append({
            "file_id": fid,
            "filename": row.get("filename") if row else None,
            "pages": _int(row.get("page_count")) if row else 0,
            "size_bytes": _int(row.get("size_bytes")) if row else 0,
            "sha256": row.get("sha256") if row else None,
            "problem": problems[fid],
            "duplicate_of": dupes.get(fid),
        })
    pages = [f["pages"] for f in files if f["problem"] != "not_found"]
    estimate = estimate_split_usd(pages, s) if pages else None
    available, reason = splitter_available(s)
    return {
        "project": {"id": str(project["id"]), "number": project.get("number"), "name": project.get("name")},
        "handoff_locked": _handoff_locked(project_id),
        "splitter": {"available": available, "reason": reason},
        "files": files,
        "totals": {"files": len(files), "pages": sum(pages)},
        "limits": {"max_files": PER_CARD_MAX_FILES, "max_pages": PER_CARD_MAX_PAGES},
        "estimate": {
            "usd": str(estimate.amount) if estimate else None,
            "model": estimate.model if estimate else None,
            "provider": estimate.provider if estimate else None,
            "priced": estimate is not None,
        },
        "spend": _spend_block(user.id),
    }


# ── 3.3.4 split-drop ─────────────────────────────────────────────────────────


def split_drop_out(task: dict) -> dict:
    """The one body for the 201, the replay and the by-key recovery route,
    rebuilt from `task.payload` and `task.split_job_id`:
    `{task_id, job_id, project_id, files: [{assistant_file_id, split_file_id,
    pages}], pages, estimate_usd}` (money as a string)."""
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    files = [
        {
            "assistant_file_id": str(f.get("assistant_file_id") or ""),
            "split_file_id": str(f.get("split_file_id") or ""),
            "pages": _int(f.get("pages")),
        }
        for f in (payload.get("files") or []) if isinstance(f, dict)
    ]
    pages = payload.get("pages")
    return {
        "task_id": str(task.get("id")),
        "job_id": str(task["split_job_id"]) if task.get("split_job_id") else None,
        "project_id": str(task["project_id"]) if task.get("project_id") else None,
        "files": files,
        "pages": _int(pages) if pages is not None else sum(f["pages"] for f in files),
        "estimate_usd": str(payload.get("estimate_usd")) if payload.get("estimate_usd") is not None else None,
    }


def recovered_split_drop(user: CurrentUser, project_id: str, key: str) -> dict | None:
    """`GET /projects/{id}/split-drop/by-key/{key}` (3.4): `split_drop_out`
    of the caller's task created with approval `key` on this project, only
    when it has a job and is past `pending`; None otherwise (a task a failed
    compensation left `pending` is not reported as executed)."""
    from app.assistant import repo

    task = repo.task_by_approval(user.id, key)
    if (task is None or str(task.get("project_id") or "") != project_id
            or not _executed_task(task)):
        return None
    return split_drop_out(task)


def _executed_task(task: dict) -> bool:
    """A task whose split-drop got its job and was not compensated. A
    compensation can leave split_job_id set on a task it failed
    `stage_failed`; that split never ran and is never reported as executed."""
    if not task.get("split_job_id") or task.get("status") not in RECOVERABLE_TASK_STATUSES:
        return False
    return not (task.get("status") == "failed" and task.get("failure_reason") == "stage_failed")


def _replay(task: dict) -> tuple[dict, bool]:
    """A task this approval already created. One that never got its job (a
    split-drop whose compensation failed, left `pending` or failed
    `stage_failed`) is not reported as executed."""
    if not _executed_task(task):
        raise SplitRefused("split_stage_failed", MSG_STAGE_FAILED)
    return split_drop_out(task), True


def _attempt(label: str, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - each cleanup runs on its own; the caller re-raises
        logger.exception("split handoff: compensation step failed: %s", label)
        return False
    return True


def _cancel_queued_runs(sb, split_file_ids: list[str]) -> None:
    """Cancel the queued bid_split runs of a compensated split-drop WITHOUT
    the queue's domain mark: llm_queue.cancel marks the file failed, which
    runs bid_split._after_failed and files the PDF whole into the project
    the compensation is undoing. A run a worker already claimed is left to
    the job delete that follows."""
    from app.services import llm_queue

    for fid in split_file_ids:
        (
            sb.table("llm_jobs")
            .update({"status": "canceled", "finished_at": _now().isoformat(), "claimed_by": None,
                     "lease_expires_at": None, "last_error": MSG_STAGE_FAILED})
            .eq("job_type", llm_queue.JOB_BID_SPLIT).eq("target_id", fid).eq("status", "queued")
            .execute()
        )


def _delete_job(sb, job_id: str) -> None:
    sb.table(_FILES).delete().eq("job_id", job_id).execute()
    storage.delete_bid_split_prefix(job_id)
    sb.table(_JOBS).delete().eq("id", job_id).execute()


def _compensate(sb, user_id: str, *, task_id: str, task_inserted: bool, hold_id: str | None,
                job_id: str | None, split_file_ids: list[str]) -> bool:
    """Undo steps 7 to 11 (3.3.4 step 12). Every action is attempted on its
    own; True when all of them succeeded."""
    from app.assistant import repo, spend

    ok = True
    if job_id:
        ok &= _attempt("cancel queued runs", _cancel_queued_runs, sb, split_file_ids)
        ok &= _attempt("delete the job", _delete_job, sb, job_id)
    ok &= _attempt("release the hold", spend.release, user_id, hold_id)
    if task_inserted:
        ok &= _attempt("fail the task", repo.update_task, user_id, task_id, {
            "status": "failed", "failure_reason": "stage_failed", "finished_at": _now().isoformat(),
        }, expect_status=("pending", "waiting_external"))
    ok &= _attempt("unclaim the staged files", repo.unclaim_staged_files, user_id, task_id)
    return ok


def _check_files(repo, user: CurrentUser, ids: list[str], approval: dict) -> list[dict]:
    """Step 2: every id present, owned, unclaimed, unpurged, live and linked
    to a user message of the approval's conversation."""
    rows = {str(r["id"]): r for r in repo.staged_files_by_ids(user.id, ids)}
    now = _now()
    conversation_id = str(approval.get("conversation_id") or "")
    bad: list[str] = []
    message_ids: set[str] = set()
    for fid in ids:
        row = rows.get(fid)
        problem = _file_problem(row, now=now, conversation_id=conversation_id or None, open_ids=set())
        if problem is not None or not conversation_id:
            bad.append(fid)
        else:
            message_ids.add(str(row["message_id"]))
    for mid in sorted(message_ids):
        message = repo.get_message(user.id, mid)
        if not message or message.get("role") != "user":
            bad.extend(fid for fid in ids if str((rows.get(fid) or {}).get("message_id")) == mid)
    if bad:
        raise SplitRefused("file_not_available", MSG_FILE_NOT_AVAILABLE, detail={"file_ids": sorted(set(bad))})
    return [rows[fid] for fid in ids]


def split_drop(
    sb,
    project_id: str,
    user: CurrentUser,
    file_ids: list[str],
    *,
    approval_id: str,
    run_id: str | None,
    max_usd: Decimal,
) -> tuple[dict, bool]:
    """Split the caller's staged files into the project on one new
    `source='project'` splitter job; returns `(split_drop_out(task), replay)`.

    Order (3.3.4): 1 replay by `approval_id`; 2 files (1 to
    PER_CARD_MAX_FILES distinct ids, owned, unclaimed, unpurged, live,
    linked to a user message of the approval's conversation, at most
    PER_CARD_MAX_PAGES pages); 3 hand-off lock; 4 splitter and queue
    available; 5 duplicates; 6 estimate (None gives `price_unknown`, above
    `max_usd` gives `price_changed`). Steps 1 to 6 have no side effects.
    7 claim the staged rows (fenced update); 8 reserve the split hold;
    9 insert the `pending` task; 10 open the job and `stage_copy` each file,
    task to `waiting_external`; 11 enqueue one `bid_split` llm_job per file
    with `max_attempts_override=SPLIT_MAX_ATTEMPTS` (never `bid_split.dispatch`).
    12 one compensation block undoes 7 to 11 and raises
    `split_stage_failed`. 13 best effort: purge the staging objects unless
    the owner is on legal hold; audit `assistant.split_started`.

    Raises SplitRefused with the 5.3 codes: bad_file_ids (400),
    file_not_available, too_many_pages, handoff_locked,
    splitter_unavailable, duplicate_file, price_unknown, price_changed,
    spend_cap_reached, split_stage_failed."""
    from app.assistant import repo, spend
    from app.services import bid_split, llm_queue, rfp_file_drop

    s = get_settings()
    # 1. Replay.
    existing = repo.task_by_approval(user.id, approval_id)
    if existing is not None:
        return _replay(existing)
    # 2. Files.
    ids = clean_ids(file_ids)
    if ids is None:
        raise SplitRefused("bad_file_ids", MSG_BAD_FILE_IDS.format(max=PER_CARD_MAX_FILES), 400)
    approval = repo.get_approval(user.id, approval_id)
    if approval is None:
        raise SplitRefused("not_found", MSG_NOT_FOUND, 404)
    project = _project(sb, project_id)
    if project is None:
        raise SplitRefused("project_not_found", MSG_PROJECT_NOT_FOUND, 404)
    rows = _check_files(repo, user, ids, approval)
    pages = [_int(r.get("page_count")) for r in rows]
    if sum(pages) > PER_CARD_MAX_PAGES:
        raise SplitRefused("too_many_pages", MSG_TOO_MANY_PAGES.format(pages=sum(pages), max=PER_CARD_MAX_PAGES),
                           detail={"pages": sum(pages), "max_pages": PER_CARD_MAX_PAGES})
    # 3. Lock.
    if _handoff_locked(project_id):
        raise SplitRefused("handoff_locked", MSG_HANDOFF_LOCKED)
    # 4. Splitter and queue.
    available, reason = splitter_available(s)
    if not available:
        raise SplitRefused("splitter_unavailable", reason or MSG_QUEUE_WHY, detail={"reason": reason})
    # 5. Duplicates.
    dupes = duplicates(sb, project_id, rows)
    if dupes:
        raise SplitRefused("duplicate_file", MSG_DUPLICATE, detail={
            "duplicates": [{"file_id": fid, **dupes[fid]} for fid in ids if fid in dupes],
        })
    # 6. Estimate against the approved ceiling.
    estimate = estimate_split_usd(pages, s)
    if estimate is None:
        raise SplitRefused("price_unknown", MSG_PRICE_UNKNOWN)
    ceiling = _dec(max_usd)
    if ceiling is None or estimate.amount > ceiling:
        shown = usd(ceiling) if ceiling is not None else "0.00"
        raise SplitRefused("price_changed", MSG_PRICE_CHANGED.format(estimate=estimate.amount, max=shown),
                           detail={"estimate_usd": str(estimate.amount), "max_usd": shown})

    # 7. Claim (one fenced update; a short count unclaims and re-checks for
    #    a concurrent replay of the same approval).
    task_id = str(uuid.uuid4())
    claimed = repo.claim_staged_files(user.id, ids, task_id=task_id, project_id=project_id)
    if claimed < len(ids):
        _attempt("unclaim after a short claim", repo.unclaim_staged_files, user.id, task_id)
        existing = repo.task_by_approval(user.id, approval_id)
        if existing is not None:
            return _replay(existing)
        raise SplitRefused("file_not_available", MSG_FILE_NOT_AVAILABLE, detail={"file_ids": ids})
    # 8. Reserve the split hold.
    try:
        hold_id = spend.reserve(user.id, kind=spend.KIND_SPLIT_TASK, amount=estimate.amount,
                                ttl=SPLIT_TASK_TTL_SECONDS, run_id=run_id, task_id=task_id,
                                approval_id=approval_id)
    except spend.SpendRefused as exc:
        _attempt("unclaim after a refused hold", repo.unclaim_staged_files, user.id, task_id)
        remaining = exc.state.user_remaining if exc.scope == spend.SCOPE_USER else exc.state.global_remaining
        scope_word = "your" if exc.scope == spend.SCOPE_USER else "company"
        raise SplitRefused("spend_cap_reached", MSG_SPEND_CAP.format(scope=scope_word, remaining=usd(remaining)),
                           detail={"scope": exc.scope, "remaining_usd": usd(remaining)}) from None
    except Exception:
        _attempt("unclaim after a failed hold", repo.unclaim_staged_files, user.id, task_id)
        raise
    # 9. Task.
    count = len(ids)
    number = str(project.get("number") or "the project")
    payload = {
        "file_ids": ids, "pages": sum(pages), "estimate_usd": str(estimate.amount), "max_usd": usd(ceiling),
    }
    try:
        repo.insert_task(user.id, {
            "id": task_id, "kind": TASK_KIND, "status": "pending",
            "title": f"Split {count} file{'s' if count != 1 else ''} into {number}",
            "project_id": project_id, "conversation_id": approval.get("conversation_id"),
            "message_id": approval.get("message_id"), "approval_id": approval_id, "run_id": run_id,
            "spend_reservation_id": hold_id, "payload": payload, "created_by": user.id,
        })
    except repo.TaskExists:
        # A concurrent replay of the same card won the unique index.
        _attempt("release after a replay", spend.release, user.id, hold_id)
        _attempt("unclaim after a replay", repo.unclaim_staged_files, user.id, task_id)
        existing = repo.task_by_approval(user.id, approval_id)
        if existing is None:
            raise
        return _replay(existing)
    except Exception:
        _attempt("release after a failed task insert", spend.release, user.id, hold_id)
        _attempt("unclaim after a failed task insert", repo.unclaim_staged_files, user.id, task_id)
        raise

    job_id: str | None = None
    split_file_ids: list[str] = []
    try:
        # 10. Job and files: a new `project` job, one server-side copy per file.
        job, _ = rfp_file_drop.drop_job(sb, project_id, user.id, None, s, extra={
            "assistant_task_id": task_id, "spend_reservation_id": hold_id,
        })
        job_id = str(job["id"])
        files_out: list[dict] = []
        for row, page_count in zip(rows, pages):
            frow = bid_split.stage_copy(
                sb, job, row["filename"], row["storage_path"], _int(row.get("size_bytes")), page_count,
                assistant_run_id=run_id, spend_reservation_id=hold_id,
            )
            split_file_ids.append(str(frow["id"]))
            files_out.append({"assistant_file_id": str(row["id"]), "split_file_id": str(frow["id"]),
                              "pages": page_count})
        payload = {**payload, "files": files_out}
        if not repo.update_task(user.id, task_id, {
            "status": "waiting_external", "split_job_id": job_id, "payload": payload,
        }, expect_status="pending"):
            raise RuntimeError("the split task left pending before its job was attached")
        # 11. Enqueue each file directly (never bid_split.dispatch: its thread
        #     fallback would run a split in this request's process).
        for fid in split_file_ids:
            llm_queue.enqueue(llm_queue.JOB_BID_SPLIT, target_id=fid, payload={"file_id": fid},
                              created_by=user.id, max_attempts_override=SPLIT_MAX_ATTEMPTS)
    except Exception as exc:
        # 12. One compensation block.
        logger.exception("split handoff: staging the split for task %s failed", task_id)
        if _compensate(sb, user.id, task_id=task_id, task_inserted=True, hold_id=hold_id, job_id=job_id,
                       split_file_ids=split_file_ids):
            raise SplitRefused("split_stage_failed", MSG_STAGE_FAILED) from exc
        raise

    # 13. Best effort: the staging objects are copied, so they go (unless the
    #     owner is on legal hold); the audit row.
    try:
        if not repo.legal_hold(user.id):
            for row in rows:
                try:
                    storage.delete_file(row["storage_path"])
                    repo.mark_staged_purged(user.id, str(row["id"]))
                except Exception:  # noqa: BLE001 - the purge sweep retries it
                    logger.exception("split handoff: staged object %s could not be purged", row.get("id"))
    except Exception:  # noqa: BLE001 - the split is under way
        logger.exception("split handoff: the legal hold of %s could not be read; nothing purged", user.id)
    try:
        audit(user.id, AUDIT_SPLIT_STARTED, "project", project_id, {
            "task_id": task_id, "job_id": job_id, "files": count, "pages": sum(pages),
        })
    except Exception:  # noqa: BLE001 - the split is under way
        logger.exception("split handoff: audit of task %s failed", task_id)
    task = repo.get_task(user.id, task_id) or {
        "id": task_id, "split_job_id": job_id, "project_id": project_id, "payload": payload,
    }
    return split_drop_out(task), False


# ── 3.3.7 Outcomes ───────────────────────────────────────────────────────────


def _whole_reason(note: str | None, category: str | None, file_status: str | None, has_segments: bool) -> str:
    """Why a file was filed whole, from structured state first (3.3.7)."""
    from app.services import rfp_split

    text = str(note or "")
    shared_prefix = rfp_split.MSG_WHOLE_NOT_SPLIT_SENT.split("{reason}", 1)[0]
    sent_suffix = rfp_split.MSG_WHOLE_NOT_SPLIT_SENT.split("{reason}", 1)[1]
    if (text.startswith(rfp_split.MSG_WHOLE_SENT)
            or (text.startswith(shared_prefix) and text.endswith(sent_suffix))
            or category == "additional"):
        return REASON_PACKAGE_SENT
    if file_status == "failed":
        return REASON_RUN_FAILED
    if rfp_split._WHY_REFILE_FAILED in text:
        return REASON_REFILE_FAILED
    if (file_status == "done" and not has_segments) or rfp_split._WHY_NO_DOCUMENTS in text:
        return REASON_NO_DOCUMENTS
    return REASON_OTHER


def _classify_file(f: dict, rows: list[dict], has_segments: bool, now: datetime) -> dict:
    status = str(f.get("status") or "")
    out = {
        "split_file_id": str(f["id"]), "filename": f.get("filename"), "status": status,
        "outcome": OUTCOME_PENDING, "documents": 0, "categories": {}, "reason": None,
        "pages": _int(f.get("page_count")),
    }
    if status in ("pending", "running"):
        return out
    segment_rows = [r for r in rows if r.get("bid_split_segment_id")]
    if segment_rows:
        docs = [r for r in segment_rows if not r.get("is_source_set")]
        categories: dict[str, int] = {}
        for r in docs:
            key = str(r.get("category") or "other")
            categories[key] = categories.get(key, 0) + 1
        out.update(outcome=OUTCOME_SPLIT, documents=len(docs), categories=categories)
        return out
    whole = next((r for r in rows if not r.get("bid_split_segment_id") and not r.get("is_source_set")), None)
    if whole is not None:
        out.update(outcome=OUTCOME_WHOLE, documents=1,
                   categories={str(whole.get("category") or "other"): 1},
                   reason=_whole_reason(whole.get("note"), whole.get("category"), status, has_segments))
        return out
    if rows:
        # Only a kept source set (a partial re-file); the rows that were cut
        # are gone. Reported whole, never split.
        out.update(outcome=OUTCOME_WHOLE, reason=REASON_OTHER)
        return out
    ended = _parse_ts(f.get("finished_at")) or _parse_ts(f.get("updated_at"))
    if ended is not None and (now - ended).total_seconds() < MISSING_GRACE_SECONDS:
        out["outcome"] = _LANDING
        return out
    if status == "failed" and ended is not None:
        out["outcome"] = OUTCOME_FAILED
        return out
    out["outcome"] = OUTCOME_MISSING
    return out


def _job_counts(files: list[dict]) -> dict:
    return {
        "file_count": len(files),
        "files_done": sum(1 for f in files if f.get("status") == "done"),
        "files_failed": sum(1 for f in files if f.get("status") == "failed"),
    }


def _outcomes(sb, job: dict, *, only: list[str] | None = None, now: datetime | None = None) -> tuple[list[dict], bool]:
    """Per-file outcomes of the job (internal `landing` kept) and `ready`."""
    files = (
        sb.table(_FILES).select("id, job_id, filename, status, page_count, finished_at, updated_at, created_at, "
                                "error, file_kind, size_bytes")
        .eq("job_id", str(job["id"])).order("created_at").execute()
    ).data or []
    if only is not None:
        wanted = set(only)
        files = [f for f in files if str(f["id"]) in wanted]
    ids = [str(f["id"]) for f in files]
    rows: list[dict] = []
    segmented: set[str] = set()
    if ids:
        if job.get("project_id"):
            rows = (
                sb.table(_PROJECT_FILES).select("id, bid_split_file_id, bid_split_segment_id, is_source_set, note, "
                                                "category")
                .eq("project_id", str(job["project_id"])).in_("bid_split_file_id", ids).execute()
            ).data or []
        segmented = {
            str(r.get("file_id"))
            for r in (sb.table(_SEGMENTS).select("id, file_id").in_("file_id", ids).execute().data or [])
        }
    by_file: dict[str, list[dict]] = {}
    for r in rows:
        by_file.setdefault(str(r.get("bid_split_file_id")), []).append(r)
    current = now or _now()
    out = [_classify_file(f, by_file.get(str(f["id"]), []), str(f["id"]) in segmented, current) for f in files]
    ready = job.get("status") != "processing" and not any(
        o["outcome"] in (OUTCOME_PENDING, _LANDING) for o in out)
    return out, ready


def _public(outcomes: list[dict]) -> list[dict]:
    return [{**o, "outcome": OUTCOME_PENDING if o["outcome"] == _LANDING else o["outcome"]} for o in outcomes]


def _job_ref(job: dict, files: list[dict]) -> dict:
    return {"id": str(job["id"]), "status": str(job.get("status") or ""), **_job_counts(files)}


def job_outcome(sb, job: dict) -> dict:
    """Per file of the job, the outcome from the project's rows (3.3.7):
    `pending`, `split` (with `documents` and `categories`), `whole` (with
    `reason` one of package_sent, run_failed, refile_failed, no_documents,
    other, classified from structured state first), `failed`, `missing`
    (no rows MISSING_GRACE_SECONDS after `finished_at`; within the grace the
    file is `landing`, shown as `pending`).

    `{job_id, project_id, job: {id, status, file_count, files_done,
    files_failed}, files: [...], ready, split, whole, failed, missing,
    pending, documents}`: the counts feed the attach card's result."""
    outcomes, ready = _outcomes(sb, job)
    files = _public(outcomes)
    return {
        "job_id": str(job["id"]),
        "project_id": str(job["project_id"]) if job.get("project_id") else None,
        "job": _job_ref(job, files),
        "files": files,
        "ready": ready,
        "split": sum(1 for f in files if f["outcome"] == OUTCOME_SPLIT),
        "whole": sum(1 for f in files if f["outcome"] == OUTCOME_WHOLE),
        "failed": sum(1 for f in files if f["outcome"] == OUTCOME_FAILED),
        "missing": sum(1 for f in files if f["outcome"] == OUTCOME_MISSING),
        "pending": sum(1 for f in files if f["outcome"] == OUTCOME_PENDING),
        "documents": sum(f["documents"] for f in files if f["outcome"] == OUTCOME_SPLIT),
    }


def _get_job(sb, job_id: str) -> dict | None:
    rows = sb.table(_JOBS).select("*").eq("id", str(job_id)).limit(1).execute().data or []
    return rows[0] if rows else None


def task_progress(sb, task: dict) -> dict:
    """`{task, job: {id, status, file_count, files_done, files_failed},
    files: [{split_file_id, filename, status, outcome, documents, categories,
    reason}], ready}` (3.3.7). `ready` = the job is not `processing` and no
    file is `pending` or `landing` (`landing` shows as `pending` in
    `outcome`). File names here are tainted text.

    Only the files split-drop staged count (task.payload.files); `task` and
    the top-level `project` carry `{id, number}`. A task whose job row is
    gone answers `job: null` and `ready: true` (the continuation fails it
    job_missing); a task with no job yet answers `ready: false`."""
    project_id = task.get("project_id")
    project = _project(sb, str(project_id)) if project_id else None
    ref = _project_ref(project, project_id)
    head = {
        "task": {"id": str(task.get("id")), "status": task.get("status"), "project": ref},
        "project": ref,
    }
    job_id = task.get("split_job_id")
    if not job_id:
        return {**head, "job": None, "files": [], "ready": False}
    job = _get_job(sb, str(job_id))
    if job is None:
        return {**head, "job": None, "files": [], "ready": True}
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    staged = [str(f.get("split_file_id")) for f in payload.get("files") or []
              if isinstance(f, dict) and f.get("split_file_id")]
    outcomes, ready = _outcomes(sb, job, only=staged or None)
    files = [{k: v for k, v in f.items() if k != "pages"} for f in _public(outcomes)]
    return {**head, "job": _job_ref(job, files), "files": files, "ready": ready}


def job_outcome_for(job_id: str) -> dict:
    """Client-free `job_outcome` for callers under `app/assistant/`: creates
    the Supabase client inside the service. Raises LookupError when the job
    is gone."""
    sb = _sb()
    job = _get_job(sb, job_id)
    if job is None:
        raise LookupError(f"split job {job_id} not found")
    return job_outcome(sb, job)


def task_progress_for(task: dict) -> dict:
    """Client-free `task_progress` for callers under `app/assistant/`
    (`continue_job`, `GET /assistant/tasks/{id}`): creates the Supabase
    client inside the service."""
    return task_progress(_sb(), task)


# ── 3.3.6 attach ─────────────────────────────────────────────────────────────


def _job_files(sb, job_id: str) -> list[dict]:
    return (
        sb.table(_FILES).select("id, filename, status, page_count, size_bytes, file_kind, created_at")
        .eq("job_id", job_id).order("created_at").execute()
    ).data or []


def _attach_problem(job: dict | None, user: CurrentUser, project_id: str, files: list[dict]) -> str | None:
    """The first precondition of attach the job fails (3.3.6), or None. A
    job already linked to this project is a replay (None)."""
    if job is None or str(job.get("created_by") or "") != str(user.id):
        return "job_not_found"
    linked = str(job.get("project_id") or "")
    if linked and linked != project_id:
        return "job_already_linked"
    replay = linked == project_id and job.get("source") == _SOURCE_PROJECT and not job.get("assistant_task_id")
    if not replay and job.get("source") != _SOURCE_MANUAL:
        return "source_not_attachable"
    if job.get("status") not in _ATTACHABLE_JOB_STATUSES:
        return "job_not_finished"
    done = [f for f in files if f.get("status") == "done"]
    if not done:
        return "nothing_to_file"
    if len(done) > ATTACH_MAX_FILES or sum(_int(f.get("size_bytes")) for f in done) > ATTACH_MAX_SOURCE_BYTES:
        return "job_too_large"
    return None


_ATTACH_MESSAGES = {
    "job_not_found": MSG_JOB_NOT_FOUND,
    "job_already_linked": MSG_JOB_ALREADY_LINKED,
    "source_not_attachable": MSG_SOURCE_NOT_ATTACHABLE,
    "job_not_finished": MSG_JOB_NOT_FINISHED,
    "nothing_to_file": MSG_NOTHING_TO_FILE,
    "job_too_large": MSG_JOB_TOO_LARGE.format(files=ATTACH_MAX_FILES, gb=ATTACH_MAX_SOURCE_BYTES / 1024 ** 3),
    "handoff_locked": MSG_JOB_ATTACH_LOCKED,
}


def _refuse(code: str) -> SplitRefused:
    return SplitRefused(code, _ATTACH_MESSAGES[code])


def attach(sb, project_id: str, user: CurrentUser, job_id: str, *, idempotency_key: str) -> tuple[dict, bool]:
    """File a finished manual splitter job of the caller into the project
    (3.3.6); returns `(job_outcome(job), replay)`. Preconditions (SplitRefused
    409): job_not_found (missing or not the caller's), source_not_attachable
    (not `manual`), job_not_finished, nothing_to_file, job_too_large (more
    than ATTACH_MAX_FILES done files or ATTACH_MAX_SOURCE_BYTES of sources),
    job_already_linked, handoff_locked. CAS links the job (`project_id`,
    `source='project'`), then `rfp_split.resync_after_run` per done file;
    audit `assistant.split_attached`. A replay (the job already linked to
    this project) re-runs the reconcile, which is idempotent."""
    from app.services import rfp_split

    job = _get_job(sb, job_id)
    files = _job_files(sb, job_id) if job is not None else []
    problem = _attach_problem(job, user, project_id, files)
    if problem is not None:
        raise _refuse(problem)
    if _handoff_locked(project_id):
        raise _refuse("handoff_locked")
    replay = str(job.get("project_id") or "") == project_id
    if not replay:
        try:
            linked = (
                sb.table(_JOBS).update({"project_id": project_id, "source": _SOURCE_PROJECT})
                .eq("id", job_id).is_("project_id", "null").eq("source", _SOURCE_MANUAL).execute()
            ).data or []
        except Exception:
            # The update may have committed with its response lost: when the
            # job now reads linked here, go on to the re-file (otherwise the
            # recovery read would find it linked with nothing filed).
            again = _get_job(sb, job_id)
            if (again is None or str(again.get("project_id") or "") != project_id
                    or again.get("source") != _SOURCE_PROJECT):
                raise
            linked = [again]
        if not linked:
            job = _get_job(sb, job_id)
            if job is None or str(job.get("project_id") or "") != project_id:
                raise _refuse("job_already_linked")
            replay = True
    done = [f for f in files if f.get("status") == "done"]
    for f in done:
        rfp_split.resync_after_run(str(f["id"]))
    actor = provenance.current()
    try:
        audit(user.id, AUDIT_SPLIT_ATTACHED, "project", project_id, {
            "job_id": job_id, "files": len(done),
            "approval_id": actor.approval_id if actor is not None else idempotency_key,
        })
    except Exception:  # noqa: BLE001 - the files are filed
        logger.exception("split handoff: audit of the attach of job %s failed", job_id)
    job = _get_job(sb, job_id) or {**(job or {}), "project_id": project_id, "source": _SOURCE_PROJECT}
    return job_outcome(sb, job), replay


def attached_outcome(sb, project_id: str, user: CurrentUser, job_id: str) -> dict | None:
    """`GET /projects/{id}/attach-split-job/{job_id}` (3.4): `job_outcome`
    of the caller's job when it is linked to this project, else None. Also
    None while a done file has no project rows (`missing` or `pending`): an
    attach that died after its link but before the re-file is not reported
    as executed (the card fails; a re-approved attach replays the
    idempotent re-file)."""
    job = _get_job(sb, job_id)
    if (job is None or str(job.get("created_by") or "") != str(user.id)
            or str(job.get("project_id") or "") != project_id):
        return None
    out = job_outcome(sb, job)
    unfiled = (OUTCOME_MISSING, OUTCOME_PENDING)
    if any(f.get("status") == "done" and f.get("outcome") in unfiled for f in out["files"]):
        return None
    return out


def attach_preview(sb, project_id: str, user: CurrentUser, job_id: str) -> dict:
    """The read the attach card is built from (3.4): `{project,
    handoff_locked, job: {id, created_at, status, file_count, files_done,
    files_failed, source, linked_project_id, own, source_bytes}, files:
    [{filename, status, pages, kind}], problem}`. Another user's job (or a
    missing one) answers `job: null`, `files: []` and `job_not_found`, so
    its id reveals nothing. Raises SplitRefused project_not_found (404)."""
    project = _project(sb, project_id)
    if project is None:
        raise SplitRefused("project_not_found", MSG_PROJECT_NOT_FOUND, 404)
    job = _get_job(sb, job_id)
    own = job is not None and str(job.get("created_by") or "") == str(user.id)
    files = _job_files(sb, job_id) if own else []
    problem = _attach_problem(job if own else None, user, project_id, files)
    done = [f for f in files if f.get("status") == "done"]
    return {
        "project": {"id": str(project["id"]), "number": project.get("number"), "name": project.get("name")},
        "handoff_locked": _handoff_locked(project_id),
        "job": {
            "id": str(job["id"]), "created_at": job.get("created_at"), "status": job.get("status"),
            **_job_counts(files), "source": job.get("source"),
            "linked_project_id": str(job["project_id"]) if job.get("project_id") else None,
            "own": True, "source_bytes": sum(_int(f.get("size_bytes")) for f in done),
        } if own else None,
        "files": [
            {"filename": f.get("filename"), "status": f.get("status"), "pages": _int(f.get("page_count")),
             "kind": f.get("file_kind")}
            for f in files
        ],
        "problem": problem,
    }


# ── The status read (3.10.3) ─────────────────────────────────────────────────


def _status_staged(repo, user: CurrentUser, now: datetime) -> list[dict]:
    """The caller's live, unclaimed staged rows linked to a user message of
    the turn's conversation, newest first. Empty for a caller with no turn
    (a human) or a run with no ledger row."""
    run_id = _actor_run_id(user)
    conversation_id = repo.conversation_for_run(user.id, run_id) if run_id else None
    if not conversation_id:
        return []
    rows = repo.staged_files_in_conversation(user.id, conversation_id, limit=STATUS_MAX_STAGED)
    live = [
        r for r in rows
        if _file_problem(r, now=now, conversation_id=conversation_id, open_ids=set()) is None
    ]
    live.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("id"))), reverse=True)
    return [
        {
            "file_id": str(r["id"]), "staged_filename": r.get("filename"), "pages": _int(r.get("page_count")),
            "size_bytes": _int(r.get("size_bytes")),
            "size_mb": round(_int(r.get("size_bytes")) / (1024 * 1024), 1),
            "message_id": str(r.get("message_id")), "expires_at": r.get("expires_at"),
        }
        for r in live[:STATUS_MAX_STAGED]
    ]


def _status_jobs(sb, user: CurrentUser, now: datetime) -> list[dict]:
    """The caller's own manual, unattached, settled jobs of the last 14 days."""
    since = (now - timedelta(days=STATUS_JOB_DAYS)).isoformat()
    jobs = (
        sb.table(_JOBS).select("id, status, created_at, source, project_id, created_by")
        .eq("created_by", str(user.id)).eq("source", _SOURCE_MANUAL).is_("project_id", "null")
        .in_("status", list(_SETTLED_JOB_STATUSES)).gte("created_at", since)
        .order("created_at", desc=True).limit(STATUS_MAX_JOBS).execute()
    ).data or []
    if not jobs:
        return []
    files = (
        sb.table(_FILES).select("id, job_id, filename, status, created_at")
        .in_("job_id", [str(j["id"]) for j in jobs]).order("created_at").execute()
    ).data or []
    by_job: dict[str, list[dict]] = {}
    for f in files:
        by_job.setdefault(str(f.get("job_id")), []).append(f)
    out = []
    for j in jobs:
        mine = by_job.get(str(j["id"]), [])
        counts = _job_counts(mine)
        out.append({
            "job_id": str(j["id"]), "created_at": j.get("created_at"), "status": j.get("status"),
            "files": counts["file_count"], "files_done": counts["files_done"],
            "files_failed": counts["files_failed"],
            "job_first_filename": mine[0].get("filename") if mine else None,
        })
    return out


def _status_tasks(sb, repo, user: CurrentUser) -> list[dict]:
    tasks = repo.list_tasks(user.id, limit=STATUS_MAX_TASKS)
    project_ids = sorted({str(t["project_id"]) for t in tasks if t.get("project_id")})
    numbers: dict[str, Any] = {}
    if project_ids:
        numbers = {
            str(p["id"]): p.get("number")
            for p in (sb.table("projects").select("id, number").in_("id", project_ids).execute().data or [])
        }
    out = []
    for t in tasks:
        payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
        pid = str(t["project_id"]) if t.get("project_id") else None
        out.append({
            "task_id": str(t["id"]), "status": t.get("status"), "project_id": pid,
            "project_number": numbers.get(pid) if pid else None,
            "files": len(payload.get("file_ids") or payload.get("files") or []),
            "failure_reason": t.get("failure_reason"), "created_at": t.get("created_at"),
        })
    return out


def status_for(user: CurrentUser, project_id: str | None) -> dict[str, Any]:
    """`GET /assistant/split/status` (3.10.3, before trimming, ids full):
    `{splitter: {available, reason, max_pages_per_file}, limits:
    {max_files_per_card, max_pages_per_card}, staged: [...], jobs: [...],
    tasks: [...], project: {id, number, handoff_locked} | null, spend:
    {user_remaining_usd, global_remaining_usd}}`. Creates the Supabase client
    inside the service. Raises SplitRefused project_not_found (404)."""
    from app.assistant import repo

    sb = _sb()
    s = get_settings()
    now = _now()
    project_out = None
    if project_id:
        project = _project(sb, project_id)
        if project is None:
            raise SplitRefused("project_not_found", MSG_PROJECT_NOT_FOUND, 404)
        project_out = {"id": str(project["id"]), "number": project.get("number"),
                       "handoff_locked": _handoff_locked(project_id)}
    available, reason = splitter_available(s)
    return {
        "splitter": {"available": available, "reason": reason,
                     "max_pages_per_file": _int(getattr(s, "bid_split_max_pages_per_file", 600))},
        "limits": {"max_files_per_card": PER_CARD_MAX_FILES, "max_pages_per_card": PER_CARD_MAX_PAGES},
        "staged": _status_staged(repo, user, now),
        "jobs": _status_jobs(sb, user, now),
        "tasks": _status_tasks(sb, repo, user),
        "project": project_out,
        "spend": _spend_block(user.id),
    }

