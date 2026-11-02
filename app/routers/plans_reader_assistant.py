"""AI Estimating routes for the assistant's plans tools
(docs/ASSISTANT_PHASE4_PLAN.md 3.4, 3.5.1, 3.5.3, 3.6.1, 3.6.3).

A separate router so routers/plans_reader.py stays untouched (its route set
is pinned by tests/test_plans_reader_routes.py). It carries the same three
gates on the router itself: `require_plans_reader` (PLANS_READER_ENABLED;
every route 404s before auth while it is off), `require_internal` and
`require_ai_estimating` (AI_ESTIMATING_STATE decides which roles get in).

Reads (plain routes; a human may call them too, the tools reach them through
the shim). Every one carries `report_rate_limit`:

  GET  /ai-estimating/projects/{project_id}/runs             runs, staleness
  GET  /ai-estimating/runs/{run_id}/brief                    brief (deliverable first, D9)
  GET  /ai-estimating/runs/{run_id}/documents                documents and sheets
  GET  /ai-estimating/runs/{run_id}/outputs/{category}/digest one output section
  GET  /ai-estimating/notes/search                           notes search (RPC A2)
  GET  /ai-estimating/runs/{run_id}/read-pages/estimate      page read cost (3.5.1)
  GET  /ai-estimating/projects/{project_id}/estimate-preview run cost (3.6.1)

Approval-bound writes (only the assistant principal executing an approved
card reaches them: `require_approval_bound`, 404 to a human and to a chat
turn principal; `require_writer`; `ai_rate_limit`):

  POST /ai-estimating/runs/{run_id}/assistant-page-reads     3.5.3
  POST /ai-estimating/projects/{project_id}/assistant-runs   3.6.3

Both are idempotent by approval: a task row with the approval id answers the
replay, and an active AI Estimating hold with the approval id and no task (a
crash between the hold and the task insert) is reused, never reserved again
(0180 A8 allows one such hold per approval). Every failure after the hold
closes it through `spend.settle_external(..., release=True)` with the AE cost
spent so far (0 before anything ran) and fails the task. The composite run
route records its progress in `assistant_tasks.payload.phase` (`reserved`,
`created`, `filled`, `estimated`, `started`), so a replay after a crash or a
timeout resumes where the earlier request stopped.

AE services are called by name and never edited (plan 3.23):
`plans_reader_runs.create_run`, `add_project_files`, `estimate`, `start`,
`cancel`, `TERMINAL_STATUSES`; `plans_reader_chat.create_thread`,
`send_message`, `asks_for_change`, `BUSY_MESSAGE`;
`routers.plans_reader._require_available`. They are imported on use, like
routers/plans_reader.py does, so the app still boots when an AE module fails
to import.

Errors are `{"detail": {"code", "message", ...}}` with the code in
X-Error-Code. Every handler is plain `def` (the Supabase SDK is synchronous).
Assistant rows go through app/assistant/repo.py only.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.assistant import repo
from app.core import supabase_client
from app.core.approval_bound import require_approval_bound
from app.core.config import get_settings
from app.core.deps import CurrentUser, require_ai_estimating, require_internal, require_writer
from app.core.features import require_plans_reader
from app.core.ratelimit import ai_rate_limit, report_rate_limit
from app.services import file_access
from app.services import plans_reader_notes as notes
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai-estimating",
    tags=["ai-estimating-assistant"],
    dependencies=[Depends(require_plans_reader), Depends(require_internal), Depends(require_ai_estimating)],
)

# Route literals the WI-13a tool specs repeat.
RUNS_ROUTE = "/ai-estimating/projects/{project_id}/runs"
BRIEF_ROUTE = "/ai-estimating/runs/{run_id}/brief"
DOCUMENTS_ROUTE = "/ai-estimating/runs/{run_id}/documents"
DIGEST_ROUTE = "/ai-estimating/runs/{run_id}/outputs/{category}/digest"
SEARCH_ROUTE = "/ai-estimating/notes/search"
PAGE_ESTIMATE_ROUTE = "/ai-estimating/runs/{run_id}/read-pages/estimate"
PAGE_READ_ROUTE = "/ai-estimating/runs/{run_id}/assistant-page-reads"
RUN_PREVIEW_ROUTE = "/ai-estimating/projects/{project_id}/estimate-preview"
RUN_CREATE_ROUTE = "/ai-estimating/projects/{project_id}/assistant-runs"

# The tools (action_kind of their approvals) the two approval-bound routes serve.
PAGE_READ_TOOL = "plans_read_pages"
AE_RUN_TOOL = "run_ai_estimating"
TASK_KIND_PAGE_READ = "plans_read_pages"
TASK_KIND_AE_RUN = "ai_estimating_run"

# Error codes (plan 5.3 and 5.4). The last four are this router's own.
APPROVAL_MISMATCH = "approval_mismatch"
ESTIMATE_CHANGED = "estimate_changed"
ASSISTANT_SPEND_CAP = "assistant_spend_cap"
AE_SPEND_CAP = "ai_estimating_spend_cap"
PAGE_READ_BUSY = "page_read_busy"
QUESTION_ASKS_FOR_CHANGE = "question_asks_for_change"
PAGES_INVALID = "pages_invalid"
LONG_CONTEXT = "long_context"
RUN_IN_PROGRESS = "run_in_progress"
AE_UNAVAILABLE = "ae_unavailable"
NO_BRIEF = "no_brief"
NO_OUTPUT = "no_output"
Q_INVALID = "q_invalid"
SCOPE_REQUIRED = "scope_required"
ID_INVALID = "id_invalid"
NOT_FOUND = "not_found"
KIND_INVALID = "kind_invalid"
SECTION_INVALID = "section_invalid"
HOLD_CLOSED = "hold_closed"
ROUTE_FAILED = "route_failed"
AE_REFUSED = "ae_refused"
NO_FILES = "no_files"
TOO_MANY_FILES = "too_many_files"
TOO_MANY_PAGES = "too_many_pages"

# The two AI Estimating hold kinds (app.assistant.spend KIND_PLANS_PAGES and
# KIND_AI_ESTIMATE; a test pins them). spend itself is imported on use, so
# app.main never loads it (tests/test_assistant_integration).
HOLD_PLANS_PAGES = "plans_pages"
HOLD_AI_ESTIMATE = "ai_estimate"

THREAD_TITLE = "Assistant page reads"
PAGE_READ_HOLD_TTL_SECONDS = 4 * 3600
# A re-estimate may exceed the approved maximum by this much (3.5.3, 3.6.3).
ESTIMATE_SLACK = Decimal("1.10")
CEILING_FACTOR_DEFAULT = Decimal("1.25")
PT = ZoneInfo("America/Los_Angeles")

RUNS_LIMIT = 10
NEW_FILES_SHOWN = 20
ERROR_MAX = 300
SHEET_FALLBACK_MAX = 200
BRIEF_LIST_MAX = 40
BRIEF_STR_MAX = 600
SHEETS_MAX = 400
DIGEST_STR_MAX = 1000
DIGEST_LIST_MAX = 200
DIGEST_LIMIT_DEFAULT, DIGEST_LIMIT_MAX = 30, 100
SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX = 30, 50
SEARCH_RUNS_MAX = 3
SEARCH_CANDIDATE_RUNS = 10
LAZY_SYNC_MAX = 2
Q_MIN, Q_MAX = 2, 200
CATEGORY_MAX = 60
FILE_IDS_MAX = 200

BRIEF_KEYS = (
    "project_summary", "electrical_scope_summary", "systems", "assumptions", "exclusions", "risks", "rfis",
    "missing_documents", "other_trades_affecting_electrical", "sheet_summaries", "spec_highlights",
)
CATEGORY_SECTIONS = ("summary", "pages", "scope", "specs", "schedules", "details", "questions", "counts", "runs")
MAIN_SECTIONS = ("brief", "boq", "markups", "material_notes", "confidence_notes")
# Geometry and anything that prices: never in a digest row (3.4).
_DROPPED_KEYS = frozenset({"positions", "points", "route", "lines_points"})
_PRICING_RE = re.compile(r"price|pricing|cost|usd|dollar|amount|margin|markup_pct|unit_rate", re.I)

_RUN_COLUMNS = (
    "id, name, status, source, project_id, created_at, started_at, finished_at, documents_total, cost_usd, error"
)

# One page read at a time per (person, run) in this process: the thread is
# found or created and the message sent under it, so two cards approved at
# once never open two "Assistant page reads" threads. Across workers the 0153
# one-answer-per-thread index is the real serializer (page_read_busy).
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


# ── Helpers ──────────────────────────────────────────────────────────────


def _runs():
    """services/plans_reader_runs, imported on use (routers/plans_reader.py rule)."""
    from app.services import plans_reader_runs

    return plans_reader_runs


def _chat():
    """services/plans_reader_chat, imported on use."""
    from app.services import plans_reader_chat

    return plans_reader_chat


def _estimates():
    """services/assistant_ai_estimate, imported on use (it imports AE modules)."""
    from app.services import assistant_ai_estimate

    return assistant_ai_estimate


def _spend():
    """app.assistant.spend, imported on use (app.main must not load it)."""
    from app.assistant import spend

    return spend


def _sb():
    return supabase_client.get_supabase()


def _error(status_code: int, code: str, message: str, **detail: Any) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message, **detail}, headers={"X-Error-Code": code})


def _uuid_or_400(value: Any, what: str) -> str:
    """A path or body id as a canonical uuid; 400 id_invalid otherwise (never
    a 22P02 from PostgREST)."""
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        raise _error(status.HTTP_400_BAD_REQUEST, ID_INVALID, f"{what} is not a valid id.") from None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _usd(value: Any) -> str:
    d = _dec(value) or Decimal("0")
    return str(max(d, Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def _clip(value: Any, n: int) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    return text[:n] if text else None


def _first(sb, table: str, **eq: Any) -> dict | None:
    query = sb.table(table).select("*")
    for col, val in eq.items():
        query = query.eq(col, val)
    rows = query.limit(1).execute().data or []
    return rows[0] if rows else None


def _project_or_404(sb, project_id: str) -> dict:
    rows = sb.table("projects").select("id, number, name").eq("id", project_id).limit(1).execute().data or []
    if not rows:
        raise _error(status.HTTP_404_NOT_FOUND, NOT_FOUND, "That project was not found.")
    r = rows[0]
    return {"id": str(r["id"]), "number": r.get("number"), "name": r.get("name")}


def _run_or_404(sb, run_id: str) -> dict:
    run = _first(sb, "plans_reader_runs", id=run_id)
    if not run:
        raise _error(status.HTTP_404_NOT_FOUND, NOT_FOUND, "That AI Estimating run was not found.")
    return run


def _run_out(run: dict) -> dict:
    return {"id": str(run["id"]), "name": run.get("name"), "status": run.get("status"),
            "finished_at": run.get("finished_at")}


def _lazy_sync(sb, run_ids: list[str]) -> bool:
    """The lazy repair of 3.3: sync at most LAZY_SYNC_MAX of the runs whose
    notes are behind, each under the default row cap. True (notes_partial)
    when a run is left to the sweep. Never fails the request."""
    ids = [str(r) for r in run_ids if r]
    if not ids:
        return False
    try:
        behind = notes.notes_behind(sb, ids)
    except Exception:  # noqa: BLE001 - search still answers from what is there
        logger.exception("plans assistant: notes_behind failed for %d runs", len(ids))
        return False
    partial = len(behind) > LAZY_SYNC_MAX
    for run_id in behind[:LAZY_SYNC_MAX]:
        try:
            result = notes.sync_run(sb, run_id)
            partial = partial or bool((result or {}).get("partial"))
        except Exception:  # noqa: BLE001 - the sweep finishes it
            logger.exception("plans assistant: lazy notes sync of run %s failed", run_id)
            partial = True
    return partial


def _http_from_available(exc: HTTPException) -> HTTPException:
    detail = exc.detail if isinstance(exc.detail, str) else "AI Estimating is not available on this server."
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, AE_UNAVAILABLE, detail)


def _require_available(*, queue: bool) -> None:
    """routers/plans_reader.py `_require_available` (imported by name on use;
    a test pins the name), its 503 re-coded as ae_unavailable."""
    from app.routers import plans_reader

    try:
        plans_reader._require_available(queue=queue)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
            raise _http_from_available(exc) from None
        raise


def _check_approval(user: CurrentUser, approval_id: str, tool: str, ids: dict[str, str]) -> dict:
    """The approval this request executes: the person's own, executing, for
    this tool, and naming the same ids as the path and body. 409
    approval_mismatch otherwise."""
    row = repo.get_approval(user.id, approval_id)
    payload = (row or {}).get("payload") if isinstance((row or {}).get("payload"), dict) else {}
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    ok = row is not None and row.get("status") == "executing" and row.get("action_kind") == tool
    for key, value in ids.items():
        if ok and args.get(key) is not None and str(args.get(key)).strip().lower() != value:
            ok = False
    if not ok:
        raise _error(status.HTTP_409_CONFLICT, APPROVAL_MISMATCH, "This request does not match its approved card.")
    return row


def _reserve(user: CurrentUser, *, kind: str, amount: Decimal, ttl: int, task_id: str,
             approval_id: str) -> tuple[str, str]:
    """(hold id, task id). An active hold of this kind for the approval is
    reused with its own task id; a closed one is refused (409 hold_closed:
    the earlier attempt released it and 0180 A8 allows one per approval)."""
    def existing() -> dict | None:
        return repo.ae_hold_by_approval(user.id, approval_id, kind)

    spend = _spend()
    hold = existing()
    if hold is None:
        try:
            hold_id = spend.reserve(user.id, kind=kind, amount=amount, ttl=ttl, run_id=user.via_assistant_run_id,
                                    task_id=task_id, approval_id=approval_id)
            return hold_id, task_id
        except spend.SpendRefused as exc:
            remaining = exc.state.user_remaining if exc.scope == spend.SCOPE_USER else exc.state.global_remaining
            who = "your" if exc.scope == spend.SCOPE_USER else "the company's"
            raise _error(
                status.HTTP_409_CONFLICT, ASSISTANT_SPEND_CAP,
                f"This hold (${_usd(amount)}) is over {who} remaining assistant budget today (${_usd(remaining)}).",
                scope=exc.scope, remaining_usd=_usd(remaining),
            ) from None
        except Exception:
            hold = existing()  # a concurrent replay of the same card won the index
            if hold is None:
                raise
    if hold.get("status") != "active":
        raise _error(status.HTTP_409_CONFLICT, HOLD_CLOSED,
                     "The earlier attempt of this card was closed. Ask again for a new card.")
    return str(hold["id"]), str(hold.get("task_id") or task_id)


def _release(user: CurrentUser, hold_id: str | None, actual: Any = "0") -> None:
    """Close an AE hold released with the actual AE cost (never raises)."""
    if not hold_id:
        return
    try:
        _spend().settle_external(user.id, hold_id, _dec(actual) or Decimal("0"), release=True)
    except Exception:  # noqa: BLE001 - an active hold expires on its own
        logger.exception("plans assistant: could not release hold %s", hold_id)


def _fail_task(user: CurrentUser, task: dict, code: str, message: str) -> None:
    """Fail an open task with `code` (never raises)."""
    payload = dict(task.get("payload") or {})
    payload["error"] = {"code": code, "message": message[:ERROR_MAX]}
    try:
        repo.update_task(user.id, str(task["id"]), {
            "status": "failed", "failure_reason": code, "finished_at": _now_iso(), "payload": payload,
        }, expect_status=repo.OPEN_TASK_STATUSES)
    except Exception:  # noqa: BLE001 - the sweep fails a stranded task
        logger.exception("plans assistant: could not fail task %s", task.get("id"))


def _replay_refusal(task: dict) -> HTTPException:
    """A replay of a card whose task already failed answers its refusal again."""
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = str(task.get("failure_reason") or err.get("code") or ROUTE_FAILED)
    message = str(err.get("message") or "This card already ran and did not finish.")
    return _error(status.HTTP_409_CONFLICT, code, message, task_id=str(task["id"]))


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            if len(_THREAD_LOCKS) > 2000:
                _THREAD_LOCKS.clear()
            lock = _THREAD_LOCKS[key] = threading.Lock()
        return lock


# ── 3.4 Reads ────────────────────────────────────────────────────────────


def _sheet_index_fallback(sb, project_id: str) -> list[dict]:
    """Up to 200 {page, title, page_role} from the page_meta of the project's
    Bid File Splitter segments, for a project with no run (3.4)."""
    out: list[dict] = []
    try:
        jobs = sb.table("bid_split_jobs").select("id").eq("project_id", project_id).limit(50).execute().data or []
        job_ids = [str(j["id"]) for j in jobs if j.get("id")]
        if not job_ids:
            return out
        files = sb.table("bid_split_files").select("id").in_("job_id", job_ids).limit(500).execute().data or []
        file_ids = [str(f["id"]) for f in files if f.get("id")]
        if not file_ids:
            return out
        segments = (
            sb.table("bid_split_segments").select("id, file_id, category, page_meta")
            .in_("file_id", file_ids).limit(1000).execute().data or []
        )
    except Exception:  # noqa: BLE001 - the fallback is a hint only
        logger.exception("plans assistant: sheet index fallback for %s failed", project_id)
        return out
    for seg in segments:
        meta = seg.get("page_meta") if isinstance(seg.get("page_meta"), list) else []
        for entry in meta:
            if not isinstance(entry, dict):
                continue
            out.append({
                "page": entry.get("page") or entry.get("pdf_page"),
                "title": _clip(entry.get("title"), 200),
                "page_role": entry.get("page_role"),
            })
            if len(out) >= SHEET_FALLBACK_MAX:
                return out
    return out


@router.get("/projects/{project_id}/runs", dependencies=[Depends(report_rate_limit)])
def project_runs(project_id: str, _: CurrentUser = Depends(require_internal)) -> dict:
    """The project's AI Estimating runs, newest first (at most 10), each with
    its staleness (3.4.1: project files added after the run). With no run,
    `sheet_index_fallback` lists sheet titles the Bid File Splitter found."""
    pid = _uuid_or_400(project_id, "project_id")
    sb = _sb()
    project = _project_or_404(sb, pid)
    rows = (
        sb.table("plans_reader_runs").select(_RUN_COLUMNS).eq("project_id", pid)
        .order("created_at", desc=True).limit(RUNS_LIMIT).execute().data or []
    )
    runs: list[dict] = []
    for run in rows:
        try:
            newer = notes.stale_files(sb, pid, run)
        except Exception:  # noqa: BLE001 - staleness is a hint; the run still lists
            logger.exception("plans assistant: stale files of run %s failed", run.get("id"))
            newer = []
        runs.append({
            "id": str(run["id"]), "name": run.get("name"), "status": run.get("status"),
            "source": run.get("source"), "created_at": run.get("created_at"),
            "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
            "documents_total": int(run.get("documents_total") or 0), "cost_usd": _usd(run.get("cost_usd")),
            "error": _clip(run.get("error"), ERROR_MAX), "stale": bool(newer),
            "new_files_since": newer[:NEW_FILES_SHOWN], "new_files_count": len(newer),
        })
    return {
        "project": project,
        "runs": runs,
        "sheet_index_fallback": None if rows else _sheet_index_fallback(sb, pid),
    }


def _clip_tree(value: Any, *, str_max: int, list_max: int, depth: int = 0) -> Any:
    """Strings clipped, lists capped, recursively (bounded depth)."""
    if depth > 8:
        return None
    if isinstance(value, str):
        return value[:str_max]
    if isinstance(value, list):
        return [_clip_tree(v, str_max=str_max, list_max=list_max, depth=depth + 1) for v in value[:list_max]]
    if isinstance(value, dict):
        return {str(k): _clip_tree(v, str_max=str_max, list_max=list_max, depth=depth + 1) for k, v in value.items()}
    return value


def _latest_output(sb, run_id: str, category: str) -> dict | None:
    query = sb.table("plans_reader_outputs").select("id, scope, category, version, output").eq("run_id", run_id)
    if category == "main":
        query = query.eq("scope", "main")
    else:
        query = query.eq("scope", "category").eq("category", category)
    rows = query.order("version", desc=True).limit(1).execute().data or []
    return rows[0] if rows else None


def _brief_of(spec: Any) -> dict | None:
    if not isinstance(spec, dict):
        return None
    if "project_summary" not in spec and isinstance(spec.get("brief"), dict):
        return spec["brief"]
    return spec


@router.get("/runs/{run_id}/brief", dependencies=[Depends(report_rate_limit)])
def run_brief(run_id: str, _: CurrentUser = Depends(require_internal)) -> dict:
    """The run's brief: the highest brief deliverable (an edit wins, D9),
    else the main output's brief; plus main's material and confidence notes.
    Lists capped at 40 items, strings at 600. 404 no_brief when neither
    exists. Syncs the run's notes when they are behind (3.3 lazy repair)."""
    rid = _uuid_or_400(run_id, "run_id")
    sb = _sb()
    run = _run_or_404(sb, rid)
    partial = _lazy_sync(sb, [rid])
    deliverable = (
        sb.table("plans_reader_deliverables").select("id, version, spec_json").eq("run_id", rid).eq("kind", "brief")
        .order("version", desc=True).limit(1).execute().data or []
    )
    main = _latest_output(sb, rid, "main")
    main_out = main.get("output") if main and isinstance(main.get("output"), dict) else {}
    brief = None
    source = version = None
    if deliverable:
        brief = _brief_of(deliverable[0].get("spec_json"))
        source, version = "brief_deliverable", deliverable[0].get("version")
    if brief is None and isinstance(main_out.get("brief"), dict):
        brief = main_out["brief"]
        source, version = "main", (main or {}).get("version")
    if brief is None:
        raise _error(status.HTTP_404_NOT_FOUND, NO_BRIEF, "This run has no brief yet.")
    clip = {"str_max": BRIEF_STR_MAX, "list_max": BRIEF_LIST_MAX}
    return {
        "run": _run_out(run),
        "source": source,
        "version": version,
        "brief": {key: _clip_tree(brief.get(key), **clip) for key in BRIEF_KEYS},
        "material_notes": _clip_tree(main_out.get("material_notes") or [], **clip),
        "confidence_notes": _clip_tree(main_out.get("confidence_notes") or [], **clip),
        "notes_partial": partial,
    }


@router.get("/runs/{run_id}/documents", dependencies=[Depends(report_rate_limit)])
def run_documents(run_id: str, _: CurrentUser = Depends(require_internal)) -> dict:
    """The run's documents with their sheets from page_meta (at most 400
    sheets across the run; `truncated` past that) and page role counts."""
    rid = _uuid_or_400(run_id, "run_id")
    sb = _sb()
    run = _run_or_404(sb, rid)
    docs = (
        sb.table("plans_reader_documents")
        .select("id, name, category, other_type, page_count, status, page_meta, created_at")
        .eq("run_id", rid).order("created_at").execute().data or []
    )
    shown = 0
    truncated = False
    out: list[dict] = []
    for doc in docs:
        meta = doc.get("page_meta") if isinstance(doc.get("page_meta"), list) else []
        roles: dict[str, int] = {}
        sheets: list[dict] = []
        for entry in meta:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("page_role") or "other")
            roles[role] = roles.get(role, 0) + 1
            if shown >= SHEETS_MAX:
                truncated = True
                continue
            shown += 1
            sheets.append({
                "page": entry.get("page"), "pdf_page": entry.get("pdf_page"),
                "prepended": bool(entry.get("prepended")), "title": _clip(entry.get("title"), 200),
                "page_role": entry.get("page_role"), "confidence": entry.get("confidence"),
                "w_in": entry.get("w_in"), "h_in": entry.get("h_in"),
            })
        out.append({
            "id": str(doc["id"]), "name": doc.get("name"), "category": doc.get("category"),
            "other_type": doc.get("other_type"), "page_count": doc.get("page_count"), "status": doc.get("status"),
            "page_roles": roles, "sheets": sheets,
        })
    return {"run": _run_out(run), "documents": out, "truncated": truncated}


def _scrub(value: Any, depth: int = 0) -> Any:
    """A digest row without geometry or any pricing key, strings clipped."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {
            str(k): _scrub(v, depth + 1) for k, v in value.items()
            if str(k) not in _DROPPED_KEYS and not _PRICING_RE.search(str(k))
        }
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value[:DIGEST_LIST_MAX]]
    if isinstance(value, str):
        return value[:DIGEST_STR_MAX]
    return value


def _boq_rows(boq: Any) -> list[dict]:
    rows: list[dict] = []
    if not isinstance(boq, dict):
        return rows
    for area in boq.get("areas") or []:
        if not isinstance(area, dict):
            continue
        for section in area.get("sections") or []:
            if not isinstance(section, dict):
                continue
            for group in section.get("groups") or []:
                if not isinstance(group, dict):
                    continue
                for sub in group.get("subgroups") or []:
                    if not isinstance(sub, dict):
                        continue
                    for item in sub.get("items") or []:
                        if isinstance(item, dict):
                            rows.append({"area": area.get("area"), "section": section.get("heading"),
                                         "group": group.get("name"), "subgroup": sub.get("name"), **item})
    return rows


def _section_rows(output: dict, category: str, section: str) -> list[Any]:
    value = output.get(section)
    if category == "main" and section == "boq":
        return _boq_rows(value)
    if category == "main" and section == "markups":
        sheets = value.get("sheets") if isinstance(value, dict) else None
        return [s for s in (sheets or []) if isinstance(s, dict)]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        return [{section: value}]
    return []


@router.get("/runs/{run_id}/outputs/{category}/digest", dependencies=[Depends(report_rate_limit)])
def output_digest(
    run_id: str,
    category: str,
    section: str = Query(min_length=1, max_length=40),
    q: str | None = Query(default=None, max_length=Q_MAX),
    limit: int = Query(default=DIGEST_LIMIT_DEFAULT, ge=1, le=DIGEST_LIMIT_MAX),
    offset: int = Query(default=0, ge=0, le=100_000),
    include_superseded: bool = False,
    _: CurrentUser = Depends(require_internal),
) -> dict:
    """One section of the latest version of a category output (or `main`),
    filtered by `q` (case-insensitive) and paged. Geometry (`positions`,
    points, routes) and every pricing key are dropped; superseded v2 rows
    are left out unless asked for. 404 no_output."""
    rid = _uuid_or_400(run_id, "run_id")
    from app.services.plans_reader_schemas import CATEGORIES

    if category != "main" and category not in CATEGORIES:
        raise _error(status.HTTP_404_NOT_FOUND, NO_OUTPUT, "There is no output of that category.")
    allowed = MAIN_SECTIONS if category == "main" else CATEGORY_SECTIONS
    if section not in allowed:
        raise _error(status.HTTP_400_BAD_REQUEST, SECTION_INVALID,
                     f"section must be one of {', '.join(allowed)}.")
    sb = _sb()
    _run_or_404(sb, rid)
    row = _latest_output(sb, rid, category)
    output = row.get("output") if row and isinstance(row.get("output"), dict) else None
    if output is None:
        raise _error(status.HTTP_404_NOT_FOUND, NO_OUTPUT, "This run has no output for that category yet.")
    items = _section_rows(output, category, section)
    if not include_superseded:
        items = [r for r in items if not (isinstance(r, dict) and r.get("status") == "superseded")]
    items = [_scrub(r) for r in items]
    needle = (q or "").strip().lower()
    if needle:
        items = [r for r in items if needle in json.dumps(r, default=str).lower()]
    total = len(items)
    return {
        "version": row.get("version"),
        "rows_version": output.get("rows_version") or 1,
        "section": section,
        "total": total,
        "rows": items[offset:offset + limit],
        "truncated": offset + limit < total,
    }


def _note_out(n: dict, runs: dict[str, dict], newest: str | None) -> dict:
    run = runs.get(str(n.get("run_id"))) or {}
    return {
        "run_id": str(n.get("run_id")), "run_name": run.get("name"), "run_status": run.get("status"),
        "run_finished_at": run.get("finished_at"), "older_run": str(n.get("run_id")) != newest,
        "kind": n.get("kind"), "category": n.get("category") or None, "document_id": n.get("document_id"),
        "page": n.get("page"), "sheet_number": n.get("sheet_number"), "sheet_title": n.get("sheet_title"),
        "title": _clip(n.get("title"), 200), "body": _clip(n.get("body"), 600), "quote": _clip(n.get("quote"), 300),
        "section": n.get("section"), "severity": n.get("severity"),
    }


@router.get("/notes/search", dependencies=[Depends(report_rate_limit)])
def search_notes(
    q: str = Query(default="", max_length=400),
    run_id: str | None = None,
    project_id: str | None = None,
    kind: str | None = Query(default=None, max_length=400),
    category: str | None = Query(default=None, max_length=CATEGORY_MAX),
    limit: int = Query(default=SEARCH_LIMIT_DEFAULT, ge=1, le=SEARCH_LIMIT_MAX),
    _: CurrentUser = Depends(require_internal),
) -> dict:
    """Hybrid search over the plans notes (RPC search_plans_notes, 0180 A2)
    of one run, or of a project's newest runs that have notes (at most 3).
    Notes behind their outputs are synced first (at most 2 runs;
    `notes_partial` when some are left to the sweep). `stale` is the newest
    searched run's staleness."""
    text = (q or "").strip()
    if not Q_MIN <= len(text) <= Q_MAX:
        raise _error(status.HTTP_400_BAD_REQUEST, Q_INVALID, f"Search with {Q_MIN} to {Q_MAX} characters.")
    if bool(run_id) == bool(project_id):
        raise _error(status.HTTP_400_BAD_REQUEST, SCOPE_REQUIRED, "Give exactly one of run_id or project_id.")
    kinds: list[str] | None = None
    if kind:
        kinds = [k.strip() for k in kind.split(",") if k.strip()]
        bad = [k for k in kinds if k not in notes.KINDS]
        if bad or not kinds:
            raise _error(status.HTTP_400_BAD_REQUEST, KIND_INVALID,
                         f"kind must be a comma list of {', '.join(sorted(notes.KINDS))}.")
    sb = _sb()
    if run_id:
        rid = _uuid_or_400(run_id, "run_id")
        candidates = [_run_or_404(sb, rid)]
        project = None
    else:
        pid = _uuid_or_400(project_id, "project_id")
        project = _project_or_404(sb, pid)
        candidates = (
            sb.table("plans_reader_runs").select(_RUN_COLUMNS).eq("project_id", pid)
            .order("created_at", desc=True).limit(SEARCH_CANDIDATE_RUNS).execute().data or []
        )
    ids = [str(r["id"]) for r in candidates]
    partial = _lazy_sync(sb, ids)
    searched: list[dict] = []
    for run in candidates:
        has = sb.table(notes.TABLE).select("id").eq("run_id", str(run["id"])).limit(1).execute().data or []
        if has:
            searched.append(run)
        if len(searched) >= SEARCH_RUNS_MAX:
            break
    by_id = {str(r["id"]): r for r in searched}
    newest = str(searched[0]["id"]) if searched else None
    rows: list[dict] = []
    if searched:
        rows = sb.rpc("search_plans_notes", {
            "p_q": text, "p_run_ids": list(by_id), "p_kinds": kinds, "p_category": category or None,
            "p_limit": limit,
        }).execute().data or []
    stale = False
    if searched:
        newest_run = searched[0]
        pid_for_stale = (project or {}).get("id") or newest_run.get("project_id")
        if pid_for_stale:
            try:
                stale = bool(notes.stale_files(sb, str(pid_for_stale), newest_run))
            except Exception:  # noqa: BLE001 - a hint only
                logger.exception("plans assistant: stale files of run %s failed", newest)
    return {
        "results": [_note_out(n, by_id, newest) for n in rows[:limit]],
        "runs": [_run_out(r) for r in searched],
        "notes_partial": partial,
        "stale": stale,
    }


def _pages_param(raw: str) -> list[int]:
    out: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise _error(status.HTTP_400_BAD_REQUEST, PAGES_INVALID, "pages must be a comma list of page numbers.") from None
    if not out or len(out) > 50:
        raise _error(status.HTTP_400_BAD_REQUEST, PAGES_INVALID, "pages must name 1 to 12 page numbers.")
    return out


@router.get("/runs/{run_id}/read-pages/estimate", dependencies=[Depends(report_rate_limit)])
def read_pages_estimate(
    run_id: str,
    document_id: str,
    pages: str = Query(min_length=1, max_length=400),
    question_chars: int | None = Query(default=None, ge=0, le=10_000),
    user: CurrentUser = Depends(require_internal),
) -> dict:
    """The worst-case cost of a page-read card (3.5.1;
    assistant_ai_estimate.page_read_estimate). Refusals are `problems`, never
    an error: the card refuses on any. `question_chars` is accepted for the
    tool's convenience; the context allowance already covers a question of
    up to 600 characters."""
    rid = _uuid_or_400(run_id, "run_id")
    did = _uuid_or_400(document_id, "document_id")
    wanted = _pages_param(pages)
    sb = _sb()
    run = _run_or_404(sb, rid)
    return _estimates().page_read_estimate(sb, run, did, wanted, str(user.id))


@router.get("/projects/{project_id}/estimate-preview", dependencies=[Depends(report_rate_limit)])
def estimate_preview(
    project_id: str,
    file_ids: str | None = Query(default=None, max_length=FILE_IDS_MAX * 40),
    user: CurrentUser = Depends(require_internal),
) -> dict:
    """The cost of an assistant-started run without creating it (3.6.1;
    assistant_ai_estimate.run_preview). `file_ids` is a comma list (at most
    200) or `all` (or absent): every readable file of the AE categories."""
    pid = _uuid_or_400(project_id, "project_id")
    raw = (file_ids or "").strip()
    ids = None if raw in ("", "all") else [p.strip() for p in raw.split(",") if p.strip()]
    est = _estimates()
    try:
        return est.run_preview(_sb(), pid, ids, str(user.id))
    except est.PreviewRefused as exc:
        raise _error(exc.status, exc.code, str(exc)) from None


# ── 3.5.3 Page read (approval-bound) ─────────────────────────────────────

_MONEY_PATTERN = r"^\d{1,6}(\.\d{1,2})?$"


class PageReadIn(BaseModel):
    """The page-read card's request body (built by the tool, part of the
    hashed payload)."""

    document_id: str = Field(min_length=1, max_length=64)
    pages: list[int] = Field(min_length=1, max_length=12)
    question: str = Field(min_length=10, max_length=600)
    max_usd: str = Field(pattern=_MONEY_PATTERN)


def _page_read_out(task: dict) -> dict:
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    return {
        "task_id": str(task["id"]),
        "run_id": payload.get("plans_run_id"),
        "thread_id": payload.get("thread_id"),
        "message_id": payload.get("plans_message_id"),
    }


def _page_read_content(document_id: str, pages: list[int], question: str) -> str:
    return _estimates().page_read_content(document_id, pages, question)


_PROBLEM_STATUS = {
    "pages_invalid": (status.HTTP_400_BAD_REQUEST, PAGES_INVALID),
    "too_many_pages": (status.HTTP_400_BAD_REQUEST, PAGES_INVALID),
    "long_context": (status.HTTP_400_BAD_REQUEST, LONG_CONTEXT),
    "document_not_in_run": (status.HTTP_400_BAD_REQUEST, "document_not_in_run"),
}


def _find_thread(sb, run_id: str, user_id: str) -> dict | None:
    return _estimates().page_read_thread(sb, run_id, user_id)


def _sent_since(sb, thread_id: str, user_id: str, since: str | None, content: str) -> dict | None:
    """The assistant message an earlier attempt of THIS card already sent on
    the thread (assistant_ai_estimate.sent_page_read_answer)."""
    return _estimates().sent_page_read_answer(sb, thread_id, user_id, since, content)


@router.post(
    "/runs/{run_id}/assistant-page-reads",
    dependencies=[Depends(ai_rate_limit)],
)
def assistant_page_read(
    run_id: str,
    body: PageReadIn,
    response: Response,
    user: CurrentUser = Depends(require_writer),
    approval_id: str = Depends(require_approval_bound),
) -> dict:
    """Execute an approved page-read card (3.5.3): hold `max(approved,
    re-estimated)` as a `plans_pages` reservation, open a
    `plans_read_pages` task and send a server-templated question into the
    person's "Assistant page reads" thread with Pro Max on, so the existing
    `plans_reader_chat` job answers it. Returns `{task_id, run_id,
    thread_id, message_id}`; a replay answers the same (X-Idempotent-Replay).
    WI-11's `plans_tasks.advance` finalizes the task."""
    rid = _uuid_or_400(run_id, "run_id")
    did = _uuid_or_400(body.document_id, "document_id")
    pages = list(body.pages)
    question = body.question.strip()
    if len(question) < 10:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "question_invalid", "The question is too short.")
    approval = _check_approval(user, approval_id, PAGE_READ_TOOL, {"run_id": rid, "document_id": did})
    sb = _sb()
    task = repo.task_by_approval(user.id, approval_id)
    if task is not None:
        if task.get("status") in ("failed", "canceled"):
            raise _replay_refusal(task)
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        if payload.get("plans_message_id"):
            response.headers["X-Idempotent-Replay"] = "true"
            return _page_read_out(task)
    resume = task is not None
    run = _run_or_404(sb, rid)
    content = _page_read_content(did, pages, question)
    orphan: str | None = None
    if task is None:
        held = repo.ae_hold_by_approval(user.id, approval_id, HOLD_PLANS_PAGES)
        orphan = str(held["id"]) if held and held.get("status") == "active" else None
    try:
        if _chat().asks_for_change(content):
            raise _error(status.HTTP_400_BAD_REQUEST, QUESTION_ASKS_FOR_CHANGE,
                         "This tool only reads pages; changes to deliverables are made on the AI Estimating page.")
        _require_available(queue=False)
        if task is None:
            task = _page_read_begin(sb, user, approval, approval_id, run, did, pages, body.max_usd, question)
    except Exception as exc:
        if task is None:
            _release(user, orphan)
        else:
            _release(user, task.get("spend_reservation_id"))
            detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else {}
            _fail_task(user, task, str(detail.get("code") or ROUTE_FAILED),
                       str(detail.get("message") or "The page read could not be sent."))
        raise
    hold_id = task.get("spend_reservation_id")
    try:
        with _thread_lock(f"{user.id}:{rid}"):
            out = _page_read_send(sb, user, run, task, content, did, pages, resume=resume)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if _page_read_abort(user, task, hold_id, str(detail.get("code") or ROUTE_FAILED),
                            str(detail.get("message") or exc.detail)):
            raise
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, ROUTE_FAILED, TEXT_PAGE_READ_SENT_UNRECORDED) from exc
    except Exception as exc:
        logger.exception("plans assistant: page read of task %s failed", task.get("id"))
        sent = bool(_sent_message_id(task))
        if not _page_read_abort(user, task, hold_id, ROUTE_FAILED, "The page read could not be sent."):
            raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, ROUTE_FAILED,
                         TEXT_PAGE_READ_SENT_UNRECORDED) from exc
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, ROUTE_FAILED,
                     "The page read could not be finished." if sent
                     else "The page read could not be sent. Nothing was charged.") from exc
    return out


TEXT_PAGE_READ_SENT_UNRECORDED = (
    "The page read was sent but could not be recorded. Its answer will arrive in this conversation when it finishes."
)


def _sent_message_id(task: dict) -> str:
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    return str(payload.get("plans_message_id") or "")


def _page_read_abort(user: CurrentUser, task: dict, hold_id: str | None, code: str, message: str) -> bool:
    """Close a page read whose send step failed. Before the AE chat message
    was sent: release the hold at $0 and fail the task. After it was sent
    (`_page_read_send` puts the message id on `task` first): cancel its
    still-queued answer job, then release at the message's cost and fail
    the task. When the job cannot be canceled (already claimed, finished,
    or the cancel failed) the answer is billed, so nothing is closed: the
    task stays open with its hold and plans_tasks.advance settles it at
    the real AE cost. Returns True when the task was closed."""
    message_id = _sent_message_id(task)
    if message_id:
        from app.assistant import plans_tasks

        try:
            canceled = plans_tasks.cancel_queued_chat_job(message_id)
        except Exception:  # noqa: BLE001 - left open for the finalize sweep
            logger.exception("plans assistant: could not cancel the answer job of message %s", message_id)
            canceled = False
        if not canceled:
            _store_sent_message(user, task)
            return False
    _release(user, hold_id, _message_cost(task))
    _fail_task(user, task, code, message)
    return True


def _store_sent_message(user: CurrentUser, task: dict) -> None:
    """One more attempt to record the sent message on the open task, so
    finalize reads it (plans_tasks adopts it from the thread otherwise)."""
    try:
        repo.update_task(user.id, str(task["id"]), {"payload": dict(task.get("payload") or {})},
                         expect_status=repo.OPEN_TASK_STATUSES)
    except Exception:  # noqa: BLE001 - plans_tasks._advance_pages adopts the message
        logger.exception("plans assistant: could not record the sent message of task %s", task.get("id"))


def _message_cost(task: dict) -> str:
    """The AE cost of a message this task already sent (0 when none)."""
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    message_id = payload.get("plans_message_id")
    if not message_id:
        return "0"
    try:
        return str(_estimates().chat_message_state(None, str(message_id), viewer_role="executive")["cost_usd"])
    except Exception:  # noqa: BLE001 - nothing billed is the safe default for a message that never ran
        return "0"


def _page_read_begin(sb, user: CurrentUser, approval: dict, approval_id: str, run: dict, document_id: str,
                     pages: list[int], approved: str, question: str) -> dict:
    """Steps 2 to 4: re-estimate, hold, task. Raises HTTPException refusals
    before anything is written, and releases the hold when the task insert
    fails."""
    est = _estimates().page_read_estimate(sb, run, document_id, pages, str(user.id))
    problems = est.get("problems") or []
    if problems:
        first = problems[0]
        code = str(first.get("code") or PAGES_INVALID)
        http, mapped = _PROBLEM_STATUS.get(code, (status.HTTP_409_CONFLICT, code))
        raise _error(http, mapped, str(first.get("message") or "These pages cannot be read."),
                     problems=[p.get("code") for p in problems])
    new_max = _dec(est.get("max_usd")) or Decimal("0")
    approved_max = _dec(approved) or Decimal("0")
    if new_max > approved_max * ESTIMATE_SLACK:
        raise _error(status.HTTP_409_CONFLICT, ESTIMATE_CHANGED,
                     f"Reading these pages now costs up to ${_usd(new_max)}, more than the ${_usd(approved_max)} "
                     "approved. Ask again for a new card.",
                     max_usd=_usd(new_max), approved_usd=_usd(approved_max))
    amount = max(approved_max, new_max)
    ae = est.get("ai_estimating") or {}
    remaining = _dec(ae.get("remaining_usd")) or Decimal("0")
    if ae.get("blocked") or amount >= remaining:
        raise _error(status.HTTP_409_CONFLICT, AE_SPEND_CAP,
                     f"AI Estimating has ${_usd(remaining)} left today, not enough for this read (${_usd(amount)}).",
                     remaining_usd=_usd(remaining))
    hold_id, task_id = _reserve(user, kind=HOLD_PLANS_PAGES, amount=amount, ttl=PAGE_READ_HOLD_TTL_SECONDS,
                                task_id=str(uuid.uuid4()), approval_id=approval_id)
    payload = {
        "plans_run_id": str(run["id"]), "document_id": document_id, "pages": pages, "thread_id": None,
        "plans_message_id": None, "max_usd": _usd(amount), "estimate_usd": est.get("estimate_usd"),
        "document_name": (est.get("document") or {}).get("name"),
        # plans_tasks adopts a sent answer by this card's exact text after a crash.
        "question": question,
    }
    row = {
        "id": task_id, "kind": TASK_KIND_PAGE_READ, "status": "running",
        "title": f"Page read on {str(run.get('name') or 'an AI Estimating run')[:120]}",
        "project_id": run.get("project_id"), "conversation_id": approval.get("conversation_id"),
        "message_id": approval.get("message_id"), "approval_id": approval_id, "run_id": str(run["id"]),
        "spend_reservation_id": hold_id, "payload": payload, "created_by": str(user.id),
    }
    try:
        return repo.insert_task(user.id, row)
    except repo.TaskExists:
        existing = repo.task_by_approval(user.id, approval_id)
        if existing is None:
            raise
        return existing
    except Exception:
        _release(user, hold_id)
        raise


def _page_read_send(sb, user: CurrentUser, run: dict, task: dict, content: str, document_id: str,
                    pages: list[int], *, resume: bool) -> dict:
    """Steps 5 and 7: the person's thread on the run, the message, the task
    update and the file access row. On a resumed card the message an earlier
    attempt sent is adopted instead of sending a second one."""
    chat = _chat()
    thread = _find_thread(sb, str(run["id"]), str(user.id))
    if thread is None:
        thread = chat.create_thread(sb, run, title=THREAD_TITLE, user=user)
    sent = _sent_since(sb, str(thread["id"]), str(user.id), task.get("created_at"), content) if resume else None
    if sent is not None:
        message_id = str(sent["id"])
    else:
        try:
            result = chat.send_message(sb, run, thread, content=content, pro_max=True, user=user)
        except ValueError as exc:
            if str(exc) == getattr(chat, "BUSY_MESSAGE", None):
                raise _error(status.HTTP_409_CONFLICT, PAGE_READ_BUSY,
                             "The previous page read on this run is still being answered. Try again when it finishes.") from None
            raise _error(status.HTTP_409_CONFLICT, AE_REFUSED, str(exc)[:ERROR_MAX]) from None
        message_id = str(((result or {}).get("assistant_message") or {}).get("id") or "")
        if not message_id:
            raise RuntimeError("the AI Estimating chat returned no answer row")
    payload = dict(task.get("payload") or {})
    payload.update({"thread_id": str(thread["id"]), "plans_message_id": message_id})
    # The caller's task carries the sent message before anything else can
    # raise, so a failure path never closes the hold as if nothing was sent.
    task["payload"] = payload
    repo.update_task(user.id, str(task["id"]), {"payload": payload}, expect_status=repo.OPEN_TASK_STATUSES)
    if sent is None:
        file_access.record(
            user, action="view", source="ai_estimating_document", file_id=document_id,
            project_id=run.get("project_id"), filename=payload.get("document_name"),
            detail={"assistant": True, "pages": pages, "run_id": str(run["id"])},
        )
    return {"task_id": str(task["id"]), "run_id": str(run["id"]), "thread_id": str(thread["id"]),
            "message_id": message_id}


# ── 3.6.3 Assistant-started AI Estimating run (approval-bound) ───────────


class AssistantRunIn(BaseModel):
    """The run card's request body. `file_ids` null means every readable
    file of the AE categories (the preview's rule)."""

    file_ids: list[str] | None = Field(default=None, max_length=FILE_IDS_MAX)
    name: str | None = Field(default=None, max_length=200)
    hold_usd: str = Field(pattern=_MONEY_PATTERN)


class _Refusal(Exception):
    """A refusal after the hold: the run is canceled, the hold released and
    the task failed before the HTTP error is raised."""

    def __init__(self, status_code: int, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


def _run_ids(sb, project_id: str, user: CurrentUser, file_ids: list[str] | None) -> list[str]:
    if file_ids is None:
        est = _estimates()
        try:
            preview = est.run_preview(sb, project_id, None, str(user.id))
        except est.PreviewRefused as exc:
            raise _error(exc.status, exc.code, str(exc)) from None
        return [str(f["id"]) for f in preview.get("files") or [] if not f.get("problem")]
    ids: list[str] = []
    for raw in file_ids:
        fid = _uuid_or_400(raw, "A file id")
        if fid not in ids:
            ids.append(fid)
    return ids


def _default_name(project: dict) -> str:
    day = datetime.now(timezone.utc).astimezone(PT).date().isoformat()
    return f"{project.get('number') or 'Project'} assistant run {day}"


def _run_task_out(task: dict) -> dict:
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    return {"task_id": str(task["id"]), "run_id": payload.get("plans_run_id"), "status": task.get("status"),
            "phase": payload.get("phase"), "run_status": payload.get("run_status")}


@router.post(
    "/projects/{project_id}/assistant-runs",
    dependencies=[Depends(ai_rate_limit)],
)
def assistant_run(
    project_id: str,
    body: AssistantRunIn,
    response: Response,
    user: CurrentUser = Depends(require_writer),
    approval_id: str = Depends(require_approval_bound),
) -> dict:
    """Execute an approved run card (3.6.3): hold `hold_usd` as an
    `ai_estimate` reservation, then create the run, copy the files,
    re-estimate and start it, recording each step in the task's
    `payload.phase` so a replay resumes. Refuses 409 estimate_changed (the
    real ceiling passes the hold by more than 10 percent), run_in_progress,
    ai_estimating_spend_cap, assistant_spend_cap, too_many_pages; 503
    ae_unavailable. Returns `{task_id, run_id, status, phase, run_status}`."""
    began = time.monotonic()
    pid = _uuid_or_400(project_id, "project_id")
    approval = _check_approval(user, approval_id, AE_RUN_TOOL, {"project_id": pid})
    sb = _sb()
    task = repo.task_by_approval(user.id, approval_id)
    if task is not None:
        if task.get("status") in ("failed", "canceled"):
            raise _replay_refusal(task)
        response.headers["X-Idempotent-Replay"] = "true"
    else:
        task = _ae_run_begin(sb, user, approval, approval_id, pid, body)
    out = _ae_run_advance(sb, user, task, pid)
    logger.info("plans assistant: run card %s reached %s in %d ms", approval_id, out.get("phase"),
                int((time.monotonic() - began) * 1000))
    return out


def _ae_run_begin(sb, user: CurrentUser, approval: dict, approval_id: str, project_id: str,
                  body: AssistantRunIn) -> dict:
    """Steps 1 and 2: the checks that need no hold, then the hold and the
    task (phase `reserved`)."""
    s = get_settings()
    hold = _dec(body.hold_usd) or Decimal("0")
    if hold <= 0:
        raise _error(status.HTTP_400_BAD_REQUEST, "hold_invalid", "The run's hold must be above $0.00.")
    project = _project_or_404(sb, project_id)
    orphan = repo.ae_hold_by_approval(user.id, approval_id, HOLD_AI_ESTIMATE)
    orphan_id = str(orphan["id"]) if orphan and orphan.get("status") == "active" else None
    try:
        ids = _run_ids(sb, project_id, user, body.file_ids)
        if not ids:
            raise _error(status.HTTP_400_BAD_REQUEST, NO_FILES, "This project has no files AI Estimating can read.")
        max_files = int(getattr(s, "assistant_ae_run_max_files", 15))
        if len(ids) > max_files:
            raise _error(status.HTTP_400_BAD_REQUEST, TOO_MANY_FILES,
                         f"{len(ids)} files is over the assistant's {max_files}-file limit. "
                         "Start a run this large on the AI Estimating page.")
        terminal = list(_runs().TERMINAL_STATUSES)
        busy = (
            sb.table("plans_reader_runs").select("id, status").eq("project_id", project_id)
            .not_.in_("status", terminal).limit(1).execute().data or []
        )
        if busy:
            raise _error(status.HTTP_409_CONFLICT, RUN_IN_PROGRESS, "A run is already in progress for this project.",
                         run_id=str(busy[0]["id"]))
        _require_available(queue=True)
        hold_id, task_id = _reserve(user, kind=HOLD_AI_ESTIMATE, amount=hold,
                                    ttl=int(getattr(s, "assistant_ae_run_hold_ttl_seconds", 43200)),
                                    task_id=str(uuid.uuid4()), approval_id=approval_id)
    except HTTPException:
        _release(user, orphan_id)
        raise
    name = (body.name or "").strip() or _default_name(project)
    row = {
        "id": task_id, "kind": TASK_KIND_AE_RUN, "status": "running",
        "title": f"AI Estimating run for {project.get('number') or 'the project'}",
        "project_id": project_id, "conversation_id": approval.get("conversation_id"),
        "message_id": approval.get("message_id"), "approval_id": approval_id, "run_id": None,
        "spend_reservation_id": hold_id, "created_by": str(user.id),
        "payload": {"phase": "reserved", "file_ids": ids, "name": name, "hold_usd": _usd(hold),
                    "plans_run_id": None, "run_status": None},
    }
    try:
        return repo.insert_task(user.id, row)
    except repo.TaskExists:
        existing = repo.task_by_approval(user.id, approval_id)
        if existing is None:
            raise
        return existing
    except Exception:
        _release(user, hold_id)
        raise


def _set_phase(user: CurrentUser, task: dict, phase: str, **fields: Any) -> dict:
    payload = dict(task.get("payload") or {})
    payload["phase"] = phase
    columns: dict[str, Any] = {}
    for key, value in fields.items():
        if key in ("status", "run_id"):
            columns[key] = value
        else:
            payload[key] = value
    if not repo.update_task(user.id, str(task["id"]), {"payload": payload, **columns},
                            expect_status=repo.OPEN_TASK_STATUSES):
        raise RuntimeError("the run task left its open status while the route was running")
    return {**task, **columns, "payload": payload}


def _orphan_run(sb, user: CurrentUser, project_id: str, since: str | None) -> dict | None:
    """A run an earlier attempt created before it recorded the phase."""
    query = (
        sb.table("plans_reader_runs").select("*").eq("project_id", project_id).eq("created_by", str(user.id))
        .eq("status", "created")
    )
    if since:
        query = query.gte("created_at", since)
    rows = query.order("created_at", desc=True).limit(1).execute().data or []
    return rows[0] if rows else None


def _split_usd(sb, run_id: str) -> Decimal | None:
    """The splitter allowance for the run's unlabeled files (their page
    counts are filled by estimate)."""
    files = sb.table("plans_reader_files").select("id, label, page_count, status").eq("run_id", run_id).execute().data or []
    pages = [int(f.get("page_count") or 1) for f in files
             if f.get("status") != "failed" and (not f.get("label") or f.get("label") == "general_drawings")]
    return _estimates().unlabeled_split_usd(pages)


def _run_pages(sb, run_id: str) -> int:
    files = sb.table("plans_reader_files").select("page_count, status").eq("run_id", run_id).execute().data or []
    return sum(int(f.get("page_count") or 0) for f in files if f.get("status") != "failed")


def _ae_run_advance(sb, user: CurrentUser, task: dict, project_id: str) -> dict:
    """Run the phases from the task's recorded one (3.6.3 steps 3 to 7)."""
    runs = _runs()
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    try:
        if payload.get("phase") == "reserved":
            run = _orphan_run(sb, user, project_id, task.get("created_at"))
            recovered = run is not None
            if run is None:
                run = _ae_service(runs.create_run, sb, name=str(payload.get("name") or "Assistant run"),
                                  source="project", project_id=project_id, render_mode="adaptive", user=user)
            # An adopted run (the earlier attempt stopped before this line) is
            # audited now; a crash between the audit and the phase update can
            # give a second row, never none.
            _audit(user, "ai_estimating.run_created", str(run["id"]),
                   {"via": "assistant", "source": "project", "project_id": project_id,
                    "render_mode": run.get("render_mode"), "task_id": str(task["id"]), "recovered": recovered})
            task = _set_phase(user, task, "created", plans_run_id=str(run["id"]), run_id=str(run["id"]),
                              run_status=run.get("status"))
            payload = task["payload"]
        run_id = str(payload.get("plans_run_id") or "")
        if payload.get("phase") == "created":
            run = _run_for(sb, run_id)
            _ae_service(runs.add_project_files, sb, run, project_file_ids=list(payload.get("file_ids") or []),
                        user=user)
            task = _set_phase(user, task, "filled")
            payload = task["payload"]
        if payload.get("phase") == "filled":
            task = _ae_run_estimate(sb, user, task, run_id)
            payload = task["payload"]
        if payload.get("phase") == "estimated":
            task = _ae_run_start(sb, user, task, run_id)
            payload = task["payload"]
    except _Refusal as exc:
        _ae_run_fail(sb, user, task, exc.code, exc.message)
        raise _error(exc.status_code, exc.code, exc.message, task_id=str(task["id"]), **exc.detail) from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("plans assistant: run task %s failed at %s", task.get("id"), payload.get("phase"))
        released = _ae_run_fail(sb, user, task, ROUTE_FAILED, "The AI Estimating run could not be started.")
        tail = "The hold was released." if released else "It will be cleaned up shortly."
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, ROUTE_FAILED,
                     f"The AI Estimating run could not be started. {tail}",
                     task_id=str(task["id"])) from exc
    return _run_task_out(task)


def _run_for(sb, run_id: str) -> dict:
    run = _first(sb, "plans_reader_runs", id=run_id) if run_id else None
    if not run:
        raise _Refusal(status.HTTP_409_CONFLICT, ROUTE_FAILED, "The run this card created no longer exists.")
    return run


def _ae_service(fn, *args: Any, **kwargs: Any) -> Any:
    """An AE service call whose app-authored refusals (RunRefused, any
    ValueError with a 4xx status) become a _Refusal."""
    from app.services import ai_spend, llm

    try:
        return fn(*args, **kwargs)
    except ai_spend.SpendCapReached as exc:
        raise _Refusal(status.HTTP_409_CONFLICT, AE_SPEND_CAP, str(exc)) from None
    except llm.LlmNotConfigured as exc:
        raise _Refusal(status.HTTP_503_SERVICE_UNAVAILABLE, AE_UNAVAILABLE, str(exc)) from None
    except ValueError as exc:
        code = getattr(exc, "status_code", None)
        http = code if isinstance(code, int) and 400 <= code < 500 else status.HTTP_409_CONFLICT
        raise _Refusal(http, AE_REFUSED, str(exc)[:ERROR_MAX], ae_status=code) from None


def _ae_run_estimate(sb, user: CurrentUser, task: dict, run_id: str) -> dict:
    """Step 5: re-estimate the created run against the approved hold."""
    s = get_settings()
    runs = _runs()
    run = _run_for(sb, run_id)
    est = _ae_service(runs.estimate, sb, run) or {}
    payload = task.get("payload") or {}
    hold = _dec(payload.get("hold_usd")) or Decimal("0")
    max_pages = int(getattr(s, "assistant_ae_run_max_pages", 400))
    pages = _run_pages(sb, run_id)
    if pages > max_pages:
        raise _Refusal(status.HTTP_409_CONFLICT, TOO_MANY_PAGES,
                       f"{pages} pages is over the assistant's {max_pages}-page limit. "
                       "Start a run this large on the AI Estimating page.", pages=pages)
    if est.get("over_cap"):
        raise _Refusal(status.HTTP_409_CONFLICT, AE_SPEND_CAP,
                       "AI Estimating does not have enough left today for this run.")
    split = _split_usd(sb, run_id)
    if split is None:
        raise _Refusal(status.HTTP_409_CONFLICT, ESTIMATE_CHANGED,
                       "The Bid File Splitter has no price, so this run cannot be held.")
    cost_max = _dec(est.get("cost_usd_max")) or _dec(est.get("cost_usd")) or Decimal("0")
    if runs.spine_enabled(s):
        cost_max += _dec(runs.loop_budget_usd_max(s)) or Decimal("0")
    factor = _dec(getattr(s, "plans_reader_run_ceiling_factor", None)) or CEILING_FACTOR_DEFAULT
    ceiling = cost_max * factor + split
    if est.get("over_run_limit") or ceiling > hold * ESTIMATE_SLACK:
        raise _Refusal(status.HTTP_409_CONFLICT, ESTIMATE_CHANGED,
                       f"This run now needs up to ${_usd(ceiling)}, more than the ${_usd(hold)} approved. "
                       "Ask again for a new card.", ceiling_usd=_usd(ceiling), hold_usd=_usd(hold))
    return _set_phase(user, task, "estimated", ceiling_usd=_usd(ceiling), split_usd=_usd(split), pages=pages)


def _ae_run_start(sb, user: CurrentUser, task: dict, run_id: str) -> dict:
    """Step 6: availability, then start. A start that answers 409 because
    the run already left `created` (an earlier attempt started it) counts
    as started."""
    runs = _runs()
    run = _run_for(sb, run_id)
    if run.get("status") == "created":
        try:
            _require_available(queue=True)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            raise _Refusal(status.HTTP_503_SERVICE_UNAVAILABLE, AE_UNAVAILABLE,
                           str(detail.get("message") or "AI Estimating is not available on this server.")) from None
        try:
            _ae_service(runs.start, sb, run, user=user)
        except _Refusal as exc:
            current = _first(sb, "plans_reader_runs", id=run_id) or {}
            if not (exc.code == AE_REFUSED and exc.detail.get("ae_status") == 409
                    and current.get("status") not in (None, "created")):
                raise
        else:
            _audit(user, "ai_estimating.run_started", run_id, {"name": run.get("name"), "via": "assistant"})
    current = _first(sb, "plans_reader_runs", id=run_id) or run
    return _set_phase(user, task, "started", status="waiting_external", run_status=current.get("status"))


def _ae_run_fail(sb, user: CurrentUser, task: dict, code: str, message: str) -> bool:
    """Cancel the run this card created (when it has not finished), release
    the hold with the cost spent so far, fail the task. Never raises.

    The hold is released and the task failed only once the run is known to
    be stopped (canceled now, already terminal, or never created). When the
    cancel fails, nothing is closed and False is returned: the task stays
    open in its phase short of `started`, so ae_task_sweep's stranded-route
    cleanup cancels the run and settles at its real cost later."""
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    run_id = payload.get("plans_run_id")
    actual = "0"
    if run_id:
        try:
            run = _first(sb, "plans_reader_runs", id=str(run_id))
            if run and run.get("status") not in _runs().TERMINAL_STATUSES:
                try:
                    _runs().cancel(sb, run, user=user)
                except _runs().RunRefused:
                    pass  # the run already finished: terminal, safe to settle
        except Exception:  # noqa: BLE001 - left for the stranded-route sweep
            logger.exception("plans assistant: could not cancel run %s; task %s is left for the "
                             "stranded-route sweep", run_id, task.get("id"))
            return False
        try:
            actual = str(_estimates().run_state(sb, str(run_id)).get("cost_usd") or "0")
        except Exception:  # noqa: BLE001 - nothing ran before start
            actual = "0"
    _release(user, task.get("spend_reservation_id"), actual)
    _fail_task(user, task, code, message)
    return True


def _audit(user: CurrentUser, action: str, run_id: str, payload: dict) -> None:
    try:
        audit(user.id, action, "plans_reader_run", run_id, payload)
    except Exception:  # noqa: BLE001 - the action already happened
        logger.exception("plans assistant: audit %s for run %s failed", action, run_id)
