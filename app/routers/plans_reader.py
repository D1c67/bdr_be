"""AI Estimating (the plans reader): the HTTP surface of docs/PLANS_READER.md.

A run takes a bid set (uploads, or a read-only copy of an existing project's
files), splits the unlabeled files with the Bid File Splitter, has GPT-6 Astra
read every category in token-budgeted groups, consolidates the outputs into a
brief plus the BOQ, estimate and markup specs, and renders those into files.
Users then chat with the stored outputs and ask for modified deliverables.

This router is thin on purpose. The lifecycle lives in
services/plans_reader_runs.py, the model calls in plans_reader_agents.py and
plans_reader_chat.py, and the work itself runs as llm_jobs (one job per model
call) so nothing here waits on a model. What the router owns:

- the gates: `require_plans_reader` (PLANS_READER_ENABLED; every route 404s
  before auth while it is off) and `require_internal` on the router itself,
  because the external estimator must never reach it, and
  `require_ai_estimating` (AI_ESTIMATING_STATE: live = every internal role,
  test = Executives and IT Admins, dev = nobody); every write (create, upload, project files,
  start, cancel, resume, delete, re-render, modify, chat thread create, chat
  send) also takes `require_writer`, so the read-only accountant can view
  every run, output and file but can never spend money or change a run
  (section 3.10);
  `ai_rate_limit` on the routes that spend or queue AI work
  (run creation, start, resume, re-render, modify, chat send),
  `upload_rate_limit` on uploads and `report_rate_limit` on the cost
  preview (it reads every file);
- the state checks that give a clear 409 before a service is asked to do
  something its run cannot do, with the stable code in `X-Error-Code`
  (docs/ERROR_CODES.md, AI Estimating);
- the "inert" rule: nothing that would spend is queued while the OpenAI route
  is unavailable (self-hosted mode, no key) or the job queue is off (503);
- signed download links and the read-only lookups that have no service
  function (stored outputs, documents, deliverables, the project picker).

Service errors map by type: SpendCapReached is a 409 with its own sentence,
LlmNotConfigured a 503, and an exception that carries an int `status_code`
keeps it (plans_reader_runs.RunRefused, a ValueError subclass, and
bid_split.UploadRefused style): any 4xx, and a 5xx only from an app-authored
ValueError, so a provider exception's text never reaches the page. After
that, LookupError is a 404 and any other app-authored ValueError a 400 on
input routes or a 409 on state-changing actions.

Every sync Supabase and storage call runs in the threadpool: the handlers are
plain `def` except the multipart upload, which awaits the capped read and
hands everything else to run_in_threadpool (event-loop rule, core/deps.py).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_ai_estimating, require_internal, require_writer
from app.core.error_codes import ErrorCode
from app.core.features import require_plans_reader
from app.core.ratelimit import ai_rate_limit, report_rate_limit, upload_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    AiEstimatingMessageIn,
    AiEstimatingModifyIn,
    AiEstimatingProjectFilesIn,
    AiEstimatingRunIn,
    AiEstimatingStatusOut,
    AiEstimatingThreadIn,
)
from app.routers.files import _read_capped
from app.services import ai_spend, llm, llm_queue, storage
from app.services.notifications import audit
from app.services.plans_reader_schemas import CATEGORIES, DELIVERABLE_KINDS

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai-estimating",
    tags=["ai-estimating"],
    dependencies=[Depends(require_plans_reader), Depends(require_internal), Depends(require_ai_estimating)],
)

# The feature every spending action is gated on. The four plans_* features
# share one route (OpenAI only), so the sub agent's stands in for all.
_GATE_FEATURE = "plans_sub"

RENDER_MODES: tuple[str, ...] = ("adaptive", "tiles_260")

# plans_reader_runs.status (CHECK in migration 0152).
RUN_STATUSES: tuple[str, ...] = (
    "created",
    "uploading",
    "splitting",
    "reading",
    "consolidating",
    "rendering",
    "done",
    "done_with_errors",
    "failed",
    "canceled",
    "over_cap",
)
_OPEN_FOR_FILES = ("created", "uploading")
_STARTABLE = ("created", "uploading")
# A run in these statuses has jobs in flight; it must be canceled before it
# is deleted (the jobs would otherwise write into rows that are gone). Delete
# also refuses any run with a running group or an unanswered chat message
# (_busy_detail): an over_cap run's in-flight groups and a finished run's
# modify or chat answer are still writing.
_WORKING = ("splitting", "reading", "consolidating", "rendering")
_FINISHED = ("done", "done_with_errors", "failed", "canceled")
_HAS_DELIVERABLES = ("done", "done_with_errors")
# plans_reader_files statuses start has not taken yet (it lists files before
# its created -> splitting compare-and-set).
_FILE_NOT_TAKEN = ("pending",)

# Extensions a run reads (plans_reader_runs.add_project_files refuses others).
_READABLE_EXTENSIONS = (".pdf", ".docx", ".xlsx")
_PROJECT_SEARCH_LIMIT = 20
# The picker reads project files a page at a time (PostgREST caps one
# response at 1,000 rows) and stops after this many pages.
_PICKER_PAGE = 1000
_PICKER_MAX_PAGES = 10
_Q_MAX = 200
# The Calls tab lists at most this many calls of a run, newest first.
_CALLS_MAX = 2000
_CALL_COLUMNS = (
    "id, run_id, group_id, thread_id, kind, category, model, reasoning_effort, response_id, "
    "input_tokens, cached_tokens, output_tokens, reasoning_tokens, cost_usd, duration_ms, ok, "
    "error_kind, error, created_at"
)

MSG_RUN_NOT_FOUND = "Run not found"
MSG_NOT_OPEN_FOR_FILES = "Files can only be added before the run starts."
MSG_NOT_STARTABLE = "This run has already started."
MSG_NOT_CANCELABLE = "This run has already finished."
MSG_NOT_RESUMABLE = "Only a run paused at the daily spend cap, or one that stalled while working, can be resumed."
MSG_DELETE_WORKING = "This run is still working. Cancel it first, then delete it."
MSG_DELETE_BUSY = (
    "A step of this run is still talking to the AI model. Wait for it to finish, "
    "then delete the run."
)
MSG_LATE_FILE = (
    "The run started before this file finished uploading, so it was not added. "
    "Start a new run to read it."
)
MSG_NO_DELIVERABLES = "Deliverables can be changed once the run has finished."
MSG_RENDERING = "The run is still rendering its files. Try again when it finishes."
MSG_NO_PDF_KIND = "This deliverable is a PDF already; it has no separate PDF copy."
MSG_NO_BRIEF_PDF = (
    "This version has no PDF copy yet (the PDF converter was not available "
    "when it was made). Open the preview to build one, or download the file."
)
MSG_QUEUE_OFF = (
    "AI Estimating needs the AI job queue, which is off on this server "
    "(LLM_QUEUE_ENABLED). Nothing was queued."
)
MSG_NOT_CONFIGURED = (
    "AI Estimating is not configured on this server: it needs OPENAI_API_KEY "
    "and OPENAI_PLANS_MODEL. Nothing was queued."
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _runs():
    """services/plans_reader_runs, imported on use (like the llm_queue job
    specs) so the app still boots if a plans reader service fails to import:
    only these routes would fail, never the shared spine."""
    from app.services import plans_reader_runs

    return plans_reader_runs


def _chat():
    """services/plans_reader_chat, imported on use (see _runs)."""
    from app.services import plans_reader_chat

    return plans_reader_chat


def _http(code: int, detail: str, error_code: ErrorCode | None = None) -> HTTPException:
    """An HTTPException carrying the stable code in X-Error-Code."""
    headers = {"X-Error-Code": error_code.value} if error_code else None
    return HTTPException(code, detail, headers=headers)


def _conflict(detail: str) -> HTTPException:
    return _http(status.HTTP_409_CONFLICT, detail, ErrorCode.AI_ESTIMATING_NOT_ACTIONABLE)


def _uuid_or_404(value: str, what: str = "Run") -> str:
    """404 for anything that is not a uuid, before it reaches PostgREST
    (which would 500 on a malformed uuid literal)."""
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found") from None
    return str(value)


def _first(sb, table: str, **eq: Any) -> dict | None:
    query = sb.table(table).select("*")
    for col, val in eq.items():
        query = query.eq(col, val)
    rows = query.limit(1).execute().data or []
    return rows[0] if rows else None


def _run_or_404(sb, run_id: str) -> dict:
    _uuid_or_404(run_id)
    run = _first(sb, "plans_reader_runs", id=run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_RUN_NOT_FOUND)
    return run


def _declared_status(exc: Exception) -> int | None:
    """The HTTP status an exception declares, when the router may honor it:
    any 4xx, and a 5xx only from an app-authored ValueError (RunRefused's
    503 for a storage sweep or the converter), never from a provider or
    transport exception whose text was not written for the page."""
    code = getattr(exc, "status_code", None)
    if not isinstance(code, int) or isinstance(code, bool):
        return None
    if 400 <= code < 500:
        return code
    if 500 <= code < 600 and isinstance(exc, ValueError):
        return code
    return None


def _service(fn: Callable[..., Any], *args: Any, conflict: bool, **kwargs: Any) -> Any:
    """Call a plans_reader service and map its errors to HTTP (module
    docstring). A declared status wins (RunRefused carries the answer the
    service chose; a 409 also gets the not-actionable code). `conflict`
    decides what an undeclared app-authored ValueError means: True on
    state-changing actions (409 with the not-actionable code), False on
    input routes (400)."""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except ai_spend.SpendCapReached as exc:
        raise _http(status.HTTP_409_CONFLICT, str(exc), ErrorCode.AI_ESTIMATING_SPEND_CAP) from exc
    except llm.LlmNotConfigured as exc:
        raise _http(
            status.HTTP_503_SERVICE_UNAVAILABLE, str(exc), ErrorCode.AI_ESTIMATING_UNAVAILABLE
        ) from exc
    except (KeyError, IndexError):
        raise  # a bug in the service, not a missing row: let it surface as a 500
    except Exception as exc:
        code = _declared_status(exc)
        if code == status.HTTP_409_CONFLICT:
            raise _conflict(str(exc)) from exc
        if code is not None:
            raise HTTPException(code, str(exc)) from exc
        if isinstance(exc, LookupError):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from exc
        if isinstance(exc, ValueError):
            if conflict:
                raise _conflict(str(exc)) from exc
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        raise


def _require_available(*, queue: bool = True) -> None:
    """503 unless a model call could be made: the OpenAI route answers for
    the plans features and (with `queue`) the job queue that runs them is
    on. Checked on every route that would queue spending work, so a
    keyless deployment stays inert with a clear sentence instead of failing
    jobs (the self-hosted switch does not move this feature off OpenAI). Chat passes queue=False: plans_reader_chat
    answers on a thread when the queue is off (bid_split.dispatch rule),
    while a run's groups and a modify need the queue's leases and retries."""
    s = get_settings()
    reason = llm.unavailable_reason(_GATE_FEATURE, s)
    if reason:
        raise _http(status.HTTP_503_SERVICE_UNAVAILABLE, reason, ErrorCode.AI_ESTIMATING_UNAVAILABLE)
    if not llm.is_configured(_GATE_FEATURE, s):
        raise _http(
            status.HTTP_503_SERVICE_UNAVAILABLE, MSG_NOT_CONFIGURED, ErrorCode.AI_ESTIMATING_UNAVAILABLE
        )
    if queue and not s.llm_queue_enabled:
        raise _http(status.HTTP_503_SERVICE_UNAVAILABLE, MSG_QUEUE_OFF, ErrorCode.AI_ESTIMATING_UNAVAILABLE)


_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f]")


def _display_name(name: str | None, default: str) -> str:
    """A client-supplied file name reduced to its last path segment with
    control characters dropped, capped at 200 characters. It is only ever a
    label; the services build storage paths from their own safe form."""
    text = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = _UNSAFE_NAME.sub("", text).strip()
    return (text or default)[:200]


def _pdf_name(name: str | None) -> str:
    base = _display_name(name, "document")
    return base if base.lower().endswith(".pdf") else f"{base}.pdf"


def _refuse_late_files(sb, run_id: str, rows: list[dict]) -> None:
    """Close the add-file / start race. The service checks the run is
    `created`, then converts and stores (minutes for a large office file)
    and inserts the file `pending`; a start in between listed the run's
    files before this insert, so the file would sit pending forever and
    hold the run in splitting (pending counts as unsettled). When the run
    has left `created` by now, every row still `pending` is marked failed
    with a sentence (settled, visible, swept with the run) and the request
    answers 409. A file start did list is already past pending, or start's
    own mark overwrites this one, so it is still read."""
    if not rows:
        return
    run = _first(sb, "plans_reader_runs", id=run_id)
    if run and run.get("status") in _OPEN_FOR_FILES:
        return
    ids = [str(r["id"]) for r in rows if r.get("id")]
    if ids:
        (
            sb.table("plans_reader_files")
            .update({"status": "failed", "error": MSG_LATE_FILE})
            .in_("id", ids)
            .in_("status", list(_FILE_NOT_TAKEN))
            .execute()
        )
    raise _conflict(MSG_LATE_FILE)


def _busy_detail(sb, run_id: str) -> str | None:
    """Why a run cannot be deleted right now beyond its status: a group
    still running (llm_queue.cancel stops queued jobs only, and an over_cap
    run or a finished run's modify can have one in flight), or a chat answer
    still pending or running. Those jobs would write into cascaded rows and
    upload objects after the storage sweep."""
    running = (
        sb.table("plans_reader_groups")
        .select("id")
        .eq("run_id", run_id)
        .eq("status", "running")
        .limit(1)
        .execute()
    ).data or []
    if running:
        return MSG_DELETE_BUSY
    threads = (
        sb.table("plans_reader_chat_threads").select("id").eq("run_id", run_id).execute()
    ).data or []
    thread_ids = [t["id"] for t in threads if t.get("id")]
    if not thread_ids:
        return None
    answering = (
        sb.table("plans_reader_chat_messages")
        .select("id")
        .in_("thread_id", thread_ids)
        .eq("role", "assistant")
        .in_("status", ["pending", "running"])
        .limit(1)
        .execute()
    ).data or []
    return MSG_DELETE_BUSY if answering else None


def _audit(user: CurrentUser, action: str, run_id: str, payload: dict | None = None) -> None:
    """Audit write that never fails the request: the action already happened."""
    try:
        audit(user.id, action, "plans_reader_run", run_id, payload)
    except Exception:  # noqa: BLE001 - the audit trail must not undo a done action
        logger.exception("ai estimating: audit %s for run %s failed", action, run_id)


# ── Status ───────────────────────────────────────────────────────────────


class _StatusOut(AiEstimatingStatusOut):
    """GET /status as the page reads it. `promax_max_pages_per_message` is
    the name the setting carries (PLANS_READER_PROMAX_MAX_PAGES_PER_MESSAGE);
    `promax_max_pages` stays for older callers. `spend` is ai_spend.status
    (the GET /ai-spend payload, `blocked` included)."""

    promax_max_pages_per_message: int


@router.get("/status", response_model=_StatusOut)
def ai_estimating_status(_: CurrentUser = Depends(require_internal)) -> dict:
    """The page header: whether a run could reach the model, which model and
    effort answer, the render modes, the Pro Max page cap, the upload cap
    and today's spend against the daily cap."""
    s = get_settings()
    reason = llm.unavailable_reason(_GATE_FEATURE, s)
    configured = llm.is_configured(_GATE_FEATURE, s)
    try:
        model: str | None = llm.active_model(_GATE_FEATURE, s)
    except Exception:  # noqa: BLE001 - a label only; the page still loads
        model = s.openai_plans_model or None
    return {
        "enabled": bool(configured and not reason and s.llm_queue_enabled),
        "unavailable_reason": reason
        or (None if configured else MSG_NOT_CONFIGURED)
        or (None if s.llm_queue_enabled else MSG_QUEUE_OFF),
        "model": model,
        "reasoning_effort": s.plans_reader_reasoning_effort,
        "render_modes": list(RENDER_MODES),
        "promax_max_pages": s.plans_reader_promax_max_pages_per_message,
        "promax_max_pages_per_message": s.plans_reader_promax_max_pages_per_message,
        "upload_max_mb": s.upload_max_bytes // (1024 * 1024),
        "spend": ai_spend.status(get_supabase()),
    }


# ── Runs ─────────────────────────────────────────────────────────────────


@router.post("/runs", status_code=status.HTTP_201_CREATED, dependencies=[Depends(ai_rate_limit)])
def create_run(body: AiEstimatingRunIn, user: CurrentUser = Depends(require_writer)) -> dict:
    """Open a run. Files arrive afterwards, one request each (uploads) or in
    one copy request (project files), and nothing spends until start."""
    project_id = _uuid_or_404(body.project_id, "Project") if body.project_id else None
    sb = get_supabase()
    run = _service(
        _runs().create_run,
        sb,
        name=body.name,
        source=body.source,
        project_id=project_id,
        render_mode=body.render_mode or RENDER_MODES[0],
        user=user,
        conflict=False,
    )
    _audit(
        user,
        "ai_estimating.run_created",
        run["id"],
        {"source": body.source, "project_id": project_id, "render_mode": run.get("render_mode")},
    )
    return run


@router.post(
    "/runs/{run_id}/files",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_run_file(
    run_id: str,
    file: UploadFile = File(...),
    label: str | None = Form(default=None),
    user: CurrentUser = Depends(require_writer),
) -> dict:
    """Add one file (PDF, xlsx or docx) to a run that has not started. The
    optional `label` is a splitter category; without it (or with
    general_drawings) the file goes through the Bid File Splitter."""
    s = get_settings()
    sb = get_supabase()
    run = await run_in_threadpool(_run_or_404, sb, run_id)
    if run.get("status") not in _OPEN_FOR_FILES:
        raise _conflict(MSG_NOT_OPEN_FOR_FILES)
    clean_label = (label or "").strip() or None
    if clean_label is not None and clean_label not in CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown file label.")
    filename = _display_name(file.filename, "upload.pdf")
    content = await _read_capped(file, s.upload_max_bytes)
    row = await run_in_threadpool(
        _service,
        _runs().add_upload,
        sb,
        run,
        filename=filename,
        content=content,
        label=clean_label,
        user=user,
        conflict=False,
    )
    await run_in_threadpool(_refuse_late_files, sb, run_id, [row] if isinstance(row, dict) else [])
    return row


@router.post("/runs/{run_id}/project-files", status_code=status.HTTP_201_CREATED)
def add_project_files(
    run_id: str, body: AiEstimatingProjectFilesIn, user: CurrentUser = Depends(require_writer)
) -> list[dict]:
    """Copy files of an existing project into a run (server side, read only)."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") not in _OPEN_FOR_FILES:
        raise _conflict(MSG_NOT_OPEN_FOR_FILES)
    ids: list[str] = []
    for raw in body.project_file_ids:
        try:
            fid = str(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A project file id is not valid.") from None
        if fid not in ids:
            ids.append(fid)
    rows = _service(
        _runs().add_project_files, sb, run, project_file_ids=ids, user=user, conflict=False
    )
    _refuse_late_files(sb, run_id, [r for r in (rows or []) if isinstance(r, dict)])
    return rows


@router.get("/runs/{run_id}/estimate", dependencies=[Depends(report_rate_limit)])
def estimate_run(run_id: str, _: CurrentUser = Depends(require_internal)) -> dict:
    """Cost preview (tokens, dollars, and whether it fits under today's
    remaining cap). Before start it plans every file (it reads each one, so
    it is rate-limited); after start the service returns the estimate
    stored at start with the live cap."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    return _service(_runs().estimate, sb, run, conflict=False)


@router.post("/runs/{run_id}/start", dependencies=[Depends(ai_rate_limit)])
def start_run(run_id: str, user: CurrentUser = Depends(require_writer)) -> dict:
    """Start reading: labeled files become documents, unlabeled ones go to
    the splitter, and the group jobs follow. Refused (409) on a run that
    already started, has no files, is over the per-run token limit, or
    would pass the daily spend cap."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") not in _STARTABLE:
        raise _conflict(MSG_NOT_STARTABLE)
    _require_available()
    result = _service(_runs().start, sb, run, user=user, conflict=True)
    _audit(user, "ai_estimating.run_started", run_id, {"name": run.get("name")})
    return result


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: CurrentUser = Depends(require_writer)) -> dict:
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") in _FINISHED:
        raise _conflict(MSG_NOT_CANCELABLE)
    return _service(_runs().cancel, sb, run, user=user, conflict=True)


@router.post("/runs/{run_id}/resume", dependencies=[Depends(ai_rate_limit)])
def resume_run(run_id: str, user: CurrentUser = Depends(require_writer)) -> dict:
    """Continue a run: re-queue the groups a spend-cap pause left waiting
    (over_cap), or nudge one that stalled in reading, consolidating or
    rendering (plans_reader_runs.RESUMABLE_STATUSES). The service decides
    the rest: a rendering run is taken over only once it has not changed
    for RECOVERY_AFTER_SECONDS (409 before that), and only the statuses that
    would queue model calls check the cap. A rendering run makes no model
    call, so it is not refused while the OpenAI route is unavailable.
    Returns the run."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    current = run.get("status")
    if current not in _runs().RESUMABLE_STATUSES:
        raise _conflict(MSG_NOT_RESUMABLE)
    if current != "rendering":
        _require_available()
    return _service(_runs().resume, sb, run, user=user, conflict=True)


@router.get("/runs")
def list_runs(
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=_Q_MAX),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    _: CurrentUser = Depends(require_internal),
) -> Any:
    if status_filter is not None and status_filter not in RUN_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown status: {status_filter}")
    term = (q or "").strip() or None
    return _service(
        _runs().list_runs,
        get_supabase(),
        status=status_filter,
        q=term,
        limit=limit,
        offset=offset,
        conflict=False,
    )


def _run_calls(sb, run_id: str) -> list[dict]:
    """The run's plans_reader_calls rows for the Calls tab, newest first, at
    most _CALLS_MAX (`totals.calls` in the payload counts them all). The
    request snapshot stays out: it is large and the row already carries
    every figure the tab shows. Read a page at a time, because PostgREST
    caps one response at 1,000 rows."""
    out: list[dict] = []
    while len(out) < _CALLS_MAX:
        start = len(out)
        end = min(start + _PICKER_PAGE, _CALLS_MAX) - 1
        rows = (
            sb.table("plans_reader_calls")
            .select(_CALL_COLUMNS)
            .eq("run_id", run_id)
            .order("created_at", desc=True)
            .order("id", desc=True)
            .range(start, end)
            .execute()
        ).data or []
        out.extend(rows)
        if len(rows) < end - start + 1:
            break
    return out[:_CALLS_MAX]


def _group_document_pages(sb, run_id: str) -> dict[str, dict[str, int]]:
    """Per group, how many of its pages come from each document
    ({group_id: {document_id: pages}}). The run payload carries only the
    page count of each group (the page entries are large), and the document
    tree splits a group's tokens over its documents by this share."""
    rows = (
        sb.table("plans_reader_groups").select("id, pages").eq("run_id", run_id).execute()
    ).data or []
    out: dict[str, dict[str, int]] = {}
    for g in rows:
        counts: dict[str, int] = {}
        for page in g.get("pages") or []:
            if isinstance(page, dict) and page.get("document_id"):
                doc_id = str(page["document_id"])
                counts[doc_id] = counts.get(doc_id, 0) + 1
        out[str(g.get("id"))] = counts
    return out


@router.get("/runs/{run_id}")
def get_run(run_id: str, _: CurrentUser = Depends(require_internal)) -> dict:
    """Everything the run view polls: run, files, documents, groups,
    outputs, deliverables, totals and queue detail (get_run_payload), plus
    `calls` (the newest _CALLS_MAX plans_reader_calls rows, newest first)
    and, on each group, `document_pages` ({document_id: pages}).

    get_run_payload already reads every group's pages and every call; when
    it returns `calls` or per-group `document_pages` itself, the router
    keeps them and skips its own read (the second scan below is only the
    fallback until the service builds both from the rows it has)."""
    sb = get_supabase()
    _run_or_404(sb, run_id)
    payload = _service(_runs().get_run_payload, sb, run_id, conflict=False)
    if not payload:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_RUN_NOT_FOUND)
    if not isinstance(payload.get("calls"), list):
        payload["calls"] = _run_calls(sb, run_id)
    groups = payload.get("groups") or []
    missing = [g for g in groups if not isinstance(g.get("document_pages"), dict)]
    if missing:
        doc_pages = _group_document_pages(sb, run_id)
        for g in missing:
            g["document_pages"] = doc_pages.get(str(g.get("id")), {})
    return payload


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(run_id: str, user: CurrentUser = Depends(require_writer)) -> Response:
    """Delete a run with its stored objects and its splitter job. A run that
    is still working must be canceled first."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") in _WORKING:
        raise _conflict(MSG_DELETE_WORKING)
    busy = _busy_detail(sb, run_id)
    if busy:
        raise _conflict(busy)
    _service(_runs().delete, sb, run, user=user, conflict=True)
    _audit(user, "ai_estimating.run_deleted", run_id, {"name": run.get("name"), "status": run.get("status")})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Outputs, documents, deliverables ─────────────────────────────────────


@router.get("/runs/{run_id}/outputs/{category}")
def get_output(run_id: str, category: str, _: CurrentUser = Depends(require_internal)) -> dict:
    """The current version of one category's consolidated output, or of the
    main output (`main`)."""
    if category != "main" and category not in CATEGORIES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Output not found")
    sb = get_supabase()
    _run_or_404(sb, run_id)
    query = sb.table("plans_reader_outputs").select("*").eq("run_id", run_id)
    if category == "main":
        query = query.eq("scope", "main")
    else:
        query = query.eq("scope", "category").eq("category", category)
    rows = query.order("version", desc=True).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Output not found")
    return rows[0]


@router.get("/runs/{run_id}/documents/{doc_id}/download")
def download_document(run_id: str, doc_id: str, _: CurrentUser = Depends(require_internal)) -> dict:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    _uuid_or_404(doc_id, "Document")
    doc = _first(sb, "plans_reader_documents", id=doc_id, run_id=run_id)
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return {"url": storage.signed_url(doc["storage_path"], download=_pdf_name(doc.get("name")))}


def _deliverable_kind_or_404(kind: str) -> str:
    if kind not in DELIVERABLE_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deliverable not found")
    return kind


def _latest_deliverable(sb, run_id: str, kind: str) -> dict | None:
    rows = (
        sb.table("plans_reader_deliverables")
        .select("*")
        .eq("run_id", run_id)
        .eq("kind", kind)
        .order("version", desc=True)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


@router.get("/runs/{run_id}/deliverables/{kind}/{version}/download")
def download_deliverable(
    run_id: str,
    kind: str,
    version: int,
    file_format: str | None = Query(default=None, alias="format", pattern="^pdf$"),
    _: CurrentUser = Depends(require_internal),
) -> dict:
    """A signed link to one deliverable version. `?format=pdf` on an office
    kind (the BOQ and estimate xlsx, the brief docx: plans_reader_runs
    .PDF_COPY_KINDS) gives its PDF copy (stored beside the file under the
    same key with .pdf, plans_reader_runs.pdf_copy_path; no row of its own),
    404 when none exists yet (the preview route builds one). The PDF kinds
    have no separate copy."""
    _deliverable_kind_or_404(kind)
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    row = _first(sb, "plans_reader_deliverables", run_id=run_id, kind=kind, version=version)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deliverable not found")
    if file_format == "pdf":
        runs = _runs()
        if kind not in runs.PDF_COPY_KINDS:
            raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NO_PDF_KIND)
        path = runs.pdf_copy_path(str(row["storage_path"]))
        if not storage.object_exists(path):
            raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NO_BRIEF_PDF)
        name = runs.pdf_copy_filename(kind, runs._project_name(run), version)
        return {"url": storage.signed_url(path, download=name)}
    name = _display_name(row.get("filename"), f"{kind}-v{version}")
    return {"url": storage.signed_url(row["storage_path"], download=name)}


@router.get("/runs/{run_id}/deliverables/{kind}/{version}/preview-url")
def deliverable_preview_url(
    run_id: str,
    kind: str,
    version: int,
    _: CurrentUser = Depends(require_internal),
) -> dict:
    """What the in-app viewer can show for one deliverable version, in the
    shape the frontend's preview strategies poll: `preview_status` ready
    (with `url` and `filename`), pending, failed or unavailable. A PDF kind
    is ready as it is. An office kind is ready once its PDF copy exists; a
    version without one (rendered while the converter was down, or before
    copies existed) is converted on a background thread and reads pending
    until a later poll finds the copy, failed when the conversion did not
    produce a PDF, or unavailable when no converter is configured (the
    frontend then downloads the file instead)."""
    _deliverable_kind_or_404(kind)
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    row = _first(sb, "plans_reader_deliverables", run_id=run_id, kind=kind, version=version)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deliverable not found")
    return _runs().pdf_copy_status(run, row)


@router.post(
    "/runs/{run_id}/deliverables/{kind}/rerender",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ai_rate_limit)],
)
def rerender_deliverable(run_id: str, kind: str, user: CurrentUser = Depends(require_writer)) -> dict:
    """Queue one deliverable to be rendered again as a new version (no model
    call: after a renderer fix, or to get a file a renderer failed on the
    first time, which has no deliverable row yet; the service then renders
    from the main output and answers 404 only when there is nothing to
    render). Nothing is drawn in the request: the service claims the run's
    one change slot (a `modify` group, so a re-render and a modify never run
    together, 409 otherwise) and queues a plans_reader_modify job with
    `rerender` true. Returns {group, queue, kind, version}, version being
    the one the job will write; poll GET /runs/{id} until the group settles
    and the new version shows. It queues AI-queue work, so it carries the
    AI rate limit."""
    _deliverable_kind_or_404(kind)
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") == "rendering":
        raise _conflict(MSG_RENDERING)
    result = _service(_runs().rerender_deliverable, sb, run, kind, user=user, conflict=True)
    info = result if isinstance(result, dict) else {}
    group = info.get("group") if isinstance(info.get("group"), dict) else {}
    _audit(
        user,
        "ai_estimating.deliverable_rerendered",
        run_id,
        {"kind": kind, "version": info.get("version"), "group_id": group.get("id")},
    )
    return result


@router.post(
    "/runs/{run_id}/deliverables/{kind}/modify",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ai_rate_limit)],
)
def modify_deliverable(
    run_id: str, kind: str, body: AiEstimatingModifyIn, user: CurrentUser = Depends(require_writer)
) -> dict:
    """Ask the model to rewrite one deliverable's spec per an instruction;
    the app re-renders it as a new version. Queued as a plans_reader_modify
    job on a `modify` group row; poll GET /runs/{id} (the group and, once
    done, the new deliverable version show there).

    One change per run at a time (a modify or a re-render), through
    plans_reader_runs.claim_change_slot: migration 0153's partial unique
    index allows one queued or running modify group per run (its 23505 is a
    409), and on a database without it every racer but the oldest withdraws
    its own row. The payload names the version the user saw
    (base_version); the job refuses when another version landed first. The
    daily cap is checked before anything is queued."""
    _deliverable_kind_or_404(kind)
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") not in _HAS_DELIVERABLES:
        raise _conflict(MSG_NO_DELIVERABLES)
    latest = _latest_deliverable(sb, run_id, kind)
    if not latest:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deliverable not found")
    _require_available()
    _service(ai_spend.check_cap, sb, conflict=True)
    group = _service(_runs().claim_change_slot, sb, run_id, conflict=True)
    try:
        job = llm_queue.enqueue(
            llm_queue.JOB_PLANS_MODIFY,
            target_id=group["id"],
            payload={
                "group_id": group["id"],
                "run_id": run_id,
                "kind": kind,
                "instruction": body.instruction,
                "user_id": user.id,
                "base_version": int(latest.get("version") or 0),
            },
            created_by=user.id,
        )
    except Exception:
        # No job means nothing will ever run the group; drop it so the run
        # does not show a change that is not coming.
        sb.table("plans_reader_groups").delete().eq("id", group["id"]).execute()
        raise
    sb.table("plans_reader_groups").update({"llm_job_id": job["id"]}).eq("id", group["id"]).execute()
    group["llm_job_id"] = job["id"]
    _audit(
        user,
        "ai_estimating.deliverable_modified",
        run_id,
        {"kind": kind, "group_id": group["id"], "instruction": body.instruction[:500]},
    )
    return {"group": group, "queue": llm_queue.poll_info(llm_queue.JOB_PLANS_MODIFY, group["id"])}


# ── Chat ─────────────────────────────────────────────────────────────────


def _thread_or_404(sb, run_id: str, thread_id: str) -> dict:
    _uuid_or_404(thread_id, "Thread")
    thread = _first(sb, "plans_reader_chat_threads", id=thread_id, run_id=run_id)
    if not thread:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    return thread


@router.get("/runs/{run_id}/chat/threads")
def list_threads(run_id: str, _: CurrentUser = Depends(require_internal)) -> Any:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    return _service(_chat().list_threads, sb, run_id, conflict=False)


@router.post("/runs/{run_id}/chat/threads", status_code=status.HTTP_201_CREATED)
def create_thread(
    run_id: str, body: AiEstimatingThreadIn, user: CurrentUser = Depends(require_writer)
) -> dict:
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    return _service(_chat().create_thread, sb, run, title=body.title, user=user, conflict=False)


@router.get("/runs/{run_id}/chat/threads/{thread_id}")
def get_thread(run_id: str, thread_id: str, _: CurrentUser = Depends(require_internal)) -> dict:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    _thread_or_404(sb, run_id, thread_id)
    payload = _service(_chat().get_thread, sb, thread_id, conflict=False)
    if not payload:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    return payload


@router.post(
    "/runs/{run_id}/chat/threads/{thread_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ai_rate_limit)],
)
def send_message(
    run_id: str,
    thread_id: str,
    body: AiEstimatingMessageIn,
    user: CurrentUser = Depends(require_writer),
) -> dict:
    """Post a user message; the answer is a queued job (plans_reader_chat)
    on the pending assistant message this returns as
    `{user_message, assistant_message}`. Poll the thread.

    A previous answer still pending or running blocks the thread (409 from
    the service, BUSY_MESSAGE). The router keeps no busy or staleness check
    of its own: the service's (plans_reader_chat._live_answers) first fails
    an answer that can never finish (its job ended, or a thread-fallback
    answer with no sign of life for a call timeout plus a lease), so a dead
    answer never blocks the thread and a live one is never cut off
    (docs/PLANS_READER.md 8.4)."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    thread = _thread_or_404(sb, run_id, thread_id)
    _require_available(queue=False)
    return _service(
        _chat().send_message,
        sb,
        run,
        thread,
        content=body.content,
        pro_max=body.pro_max,
        user=user,
        conflict=True,
    )


# ── Project picker ───────────────────────────────────────────────────────


def _readable_name(filename: str | None) -> bool:
    name = str(filename or "").lower()
    return name.endswith(_READABLE_EXTENSIONS)


def _pickable_files(sb, project_ids: list[str]) -> list[dict]:
    """The project files a run would accept: the allow list of categories
    plans_reader_runs maps to a label (filtered in SQL, so commercial files
    never count against a page) and a readable extension. Read a page at a
    time so a busy project never pushes another project's files past the
    response cap."""
    allowed = sorted(_runs().PROJECT_CATEGORY_LABELS)
    out: list[dict] = []
    for page in range(_PICKER_MAX_PAGES):
        start = page * _PICKER_PAGE
        rows = (
            sb.table("project_files")
            .select("id, project_id, category, filename, size_bytes")
            .in_("project_id", project_ids)
            .in_("category", allowed)
            .order("created_at")
            .order("id")
            .range(start, start + _PICKER_PAGE - 1)
            .execute()
        ).data or []
        out.extend(f for f in rows if _readable_name(f.get("filename")))
        if len(rows) < _PICKER_PAGE:
            break
    return out


def _search_term(q: str | None) -> str | None:
    """The search box as a plain substring: characters that would break the
    PostgREST or_() syntax or act as LIKE wildcards are dropped (the
    routers/emails.py rule)."""
    term = re.sub(r"[,()%_*\\\x00-\x1f\x7f]", " ", (q or "")[:_Q_MAX]).strip()
    return term or None


@router.get("/project-search")
def project_search(
    q: str | None = Query(default=None, max_length=_Q_MAX), _: CurrentUser = Depends(require_internal)
) -> list[dict]:
    """Up to 20 projects for the New run picker, newest first, matched on
    name or number, each with the files a run can copy. Same visibility as
    the dashboard list (GET /projects): every internal role sees every bid;
    Project Management and Certified Payroll only projects stay out."""
    sb = get_supabase()
    query = (
        sb.table("projects")
        .select("id, number, name, current_stage")
        .neq("current_stage", "pm_only")
        .neq("current_stage", "cp_only")
    )
    term = _search_term(q)
    if term:
        query = query.or_(f"name.ilike.%{term}%,number.ilike.%{term}%")
    projects = query.order("created_at", desc=True).limit(_PROJECT_SEARCH_LIMIT).execute().data or []
    ids = [p["id"] for p in projects]
    files_by_project: dict[str, list[dict]] = {pid: [] for pid in ids}
    if ids:
        for f in _pickable_files(sb, ids):
            files_by_project.setdefault(f["project_id"], []).append(
                {
                    "id": f["id"],
                    "category": f.get("category"),
                    "filename": f.get("filename"),
                    "size_bytes": f.get("size_bytes"),
                }
            )
    return [
        {
            "id": p["id"],
            "number": p.get("number"),
            "name": p.get("name"),
            "stage": p.get("current_stage"),
            "files": files_by_project.get(p["id"], []),
        }
        for p in projects
    ]
