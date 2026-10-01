"""RFP Ingestion Sandbox: the dev-only API under /rfp-ingest.

The HTTP surface of the ingestion sandbox (docs/RFP_INGESTION_SANDBOX.md,
section 3.8). Every piece of behavior lives in app/services/rfp_ingest: this
module only validates the request, maps the service's app-authored refusals
onto HTTP statuses, writes the audit rows and shapes the responses the
/ingestion-sandbox page is written against. Nothing here touches a table,
a bucket or a child process directly.

Access, three locks deep:

- The env flag. `require_rfp_ingest` is a ROUTER-level dependency, so while
  RFP_INGEST_ENABLED is false every route 404s with the bare "Not Found"
  body before a token is ever read (the tool is indistinguishable from one
  that was never implemented; same contract as the Bid File Splitter).
- Dev accounts only. `Depends(require_dev)` sits in EVERY endpoint signature
  (profiles.is_dev, 403 otherwise), the AI monitor precedent. There is no
  role split: the sandbox is a developer's bench, not a workflow surface.
- Rate limits, in the decorators: `rfp_ingest_rate_limit` (the tool's own
  120/min budget) on every read and on cancel/delete, `ai_rate_limit` on
  the two routes that put a sandbox run on the queue (start, retry), and
  `upload_rate_limit` on the multipart upload.

Request hygiene: every path id must be a CANONICAL uuid (`uuid.UUID` parses
it and the spelling matches the canonical lowercase 8-4-4-4-12 form) before
it can reach PostgREST. Parsing alone is not enough: Python accepts the
`urn:uuid:`, braced and hyphen-less spellings that Postgres' uuid input
refuses with 22P02, which is the unhandled 500 (and the lost CORS headers
behind a "Failed to fetch") this guard exists to prevent. A refusal is the
same 404 a missing row gets, so ids stay non-enumerable. The `/emails`
search term goes through the emails router's `_sanitize_query` (PostgREST
filter syntax and ilike wildcards stripped, capped at 200 chars) before the
service sees it.

Cost control on `/status`: the service can end its report with a REAL
self-test (a sandbox child, seconds of wall clock) and every handler here
runs in the shared anyio threadpool, so an unguarded read could park the
whole pool on child spawns under nothing but the 120/min budget. The route
therefore asks for the self-test to be run fresh ONLY for `?self_test=1`
(`status_report(self_test_fresh=...)`): a plain read builds at most a cheap
report carrying this worker's last verdict, and serves a report younger than
`_STATUS_CACHE_SECONDS` without building anything at all. On top of that,
exactly one report is built per worker at a time (`_status_lock`). Nothing
else in this module can start a child.

Error mapping: the service raises `RfpIngestPermanent` with an `http_status`
(400 unknown filter, 404 missing row, 409 state conflict, 413 over a cap)
and an app-authored sentence, which becomes the HTTPException detail
verbatim; `RfpIngestDuplicate` (the partial unique index on (run_id,
sha256) refused an upload) is a 409 whose detail names the winning file id;
`RfpIngestTransient` (an infrastructure fault the queue would retry) is a
503. Nothing from an exception, a filename or a child ever lands in a
response through this module: the sentences are the service's.

Event-loop rule: the Supabase SDK is synchronous, so every handler is a plain
`def` (FastAPI runs it in its threadpool) except the multipart upload, which
is `async def` only for the streamed body read and hands the service call
to `run_in_threadpool`.

Dispatch: the service's `dispatch()` puts one llm_jobs row per run on the
durable queue (job_type rfp_ingest, priority 200) and degrades to FastAPI
BackgroundTasks when the queue is off or the enqueue fails, exactly like the
splitter; the handlers therefore take a `BackgroundTasks` parameter and
pass it through. The response of every run-shaped write is the run detail
(`GET /runs/{id}`: the row, its files without manifests, and the queue's
poll info) so the page can render the result without a second request.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, model_validator
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_dev
from app.core.features import require_rfp_ingest
from app.core.ratelimit import ai_rate_limit, rfp_ingest_rate_limit, upload_rate_limit
from app.routers.emails import _sanitize_query
from app.routers.files import _read_capped
from app.sandbox import protocol
from app.services import rfp_ingest as ri
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/rfp-ingest",
    tags=["rfp-ingest"],
    dependencies=[Depends(require_rfp_ingest)],
)

_RUN_NOT_FOUND = "Run not found"
_FILE_NOT_FOUND = "File not found"
_PAGE_NOT_FOUND = "Page not found"
_EMAIL_NOT_FOUND = "Email not found"
_DEFAULT_UPLOAD_NAME = "upload.pdf"

# Listing bounds. The service clamps to its own ceiling (500); these are the
# request-side defaults and the documented maxima.
_RUNS_DEFAULT_LIMIT = 50
_RUNS_MAX_LIMIT = 500
_PAGES_DEFAULT_LIMIT = 100
_PAGES_MAX_LIMIT = 500
_EMAILS_DEFAULT_LIMIT = 25
_EMAILS_MAX_LIMIT = 100

_AUDIT_ENTITY = "rfp_ingest_run"

# GET /status: how long a built report stays servable, and how long a request
# that found the builder busy waits for that report before giving up. The
# self-test measures a fraction of a second on a healthy host, so a caller
# that waits this long is queued behind a sandbox that is already in trouble.
_STATUS_CACHE_SECONDS = 30.0
_STATUS_WAIT_SECONDS = 5.0
_MSG_STATUS_BUSY = "A sandbox status check is already running; try again in a moment."

_status_lock = threading.Lock()
_status_cache: tuple[float, dict] | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _uuid_or_404(value: str | None, message: str) -> str:
    """Refuse an id PostgREST could not cast BEFORE any query runs, with the
    same 404 a missing row gets. Only the canonical lowercase 8-4-4-4-12
    spelling passes: `uuid.UUID` also parses `urn:uuid:...`, `{...}` and
    hyphen-less forms that Postgres' uuid input rejects with 22P02, so
    parsing alone would let through exactly the 500 this guard prevents.
    The original spelling is returned (never a re-serialized one) so the
    fake databases in tests and PostgREST agree."""
    text = str(value)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, message) from None
    if text.lower() != str(parsed):
        raise HTTPException(status.HTTP_404_NOT_FOUND, message)
    return text


def _service(fn, /, *args, **kwargs):
    """Call a synchronous service function and map its refusals to HTTP.

    The service's sentences are app-authored, so they are safe to pass to
    the client verbatim; the status comes with the exception."""
    try:
        return fn(*args, **kwargs)
    except ri.RfpIngestDuplicate as exc:
        detail = str(exc)
        if exc.existing_file_id:
            detail = f"{detail} (existing file {exc.existing_file_id})"
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    except ri.RfpIngestPermanent as exc:
        raise HTTPException(exc.http_status, str(exc)) from exc
    except ri.RfpIngestTransient as exc:
        logger.warning("rfp ingest: transient fault answered 503: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


def _run_detail_or_404(run_id: str) -> dict:
    """The run detail (row + files without manifests + queue poll info)."""
    run = ri.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _RUN_NOT_FOUND)
    return run


# ── Status / email picker ────────────────────────────────────────────────


def _status_report(fresh: bool) -> dict:
    """The service report under the single-flight gate described in the
    module docstring. `fresh` decides whether the report's self-test is a real
    child spawn or this worker's last verdict, so only `?self_test=1` can cost
    a child here. On top of that at most one report is built per worker at a
    time, a report younger than `_STATUS_CACHE_SECONDS` answers a plain read
    without building anything, and a caller that cannot get the builder within
    `_STATUS_WAIT_SECONDS` takes the last report rather than building one of
    its own. `cached` says which of the two the caller got."""
    global _status_cache

    def _recent() -> dict | None:
        snapshot = _status_cache
        if snapshot is None or time.monotonic() - snapshot[0] >= _STATUS_CACHE_SECONDS:
            return None
        return snapshot[1]

    if not fresh:
        recent = _recent()
        if recent is not None:
            return {**recent, "cached": True}
    if not _status_lock.acquire(timeout=_STATUS_WAIT_SECONDS):
        snapshot = _status_cache
        if snapshot is not None:
            logger.warning("rfp ingest: status builder busy; answered the last report")
            return {**snapshot[1], "cached": True}
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _MSG_STATUS_BUSY)
    try:
        # Published while this request waited for the lock: a plain read
        # takes it instead of building a second report for the same answer.
        if not fresh:
            recent = _recent()
            if recent is not None:
                return {**recent, "cached": True}
        report = ri.status_report(self_test_fresh=fresh)
        _status_cache = (time.monotonic(), report)
        return {**report, "cached": False}
    finally:
        _status_lock.release()


@router.get("/status", dependencies=[Depends(rfp_ingest_rate_limit)])
def sandbox_status(
    self_test: bool = Query(default=False),
    _: CurrentUser = Depends(require_dev),
) -> dict:
    """Limits, versions, platform notes, free scratch disk, slot occupancy
    and the sandbox self-test, plus `cached` and `self_test_fresh`.

    A plain read spawns NOTHING: it answers a report built within the last
    30 seconds, or builds one (a disk_usage call and a slot scan) carrying
    this worker's last self-test verdict, which is `{ok: null}` until one has
    run. `?self_test=1` demands a FRESH self-test (a real child on the
    embedded page, a few seconds): call it on demand, never from a poll.
    Either way only one report is built per worker at a time, so concurrent
    readers cannot fill the threadpool with children; a reader that waits out
    that gate gets the last report (`cached` true), or a 503 when this worker
    has never built one."""
    return _status_report(fresh=self_test)


@router.get("/emails", dependencies=[Depends(rfp_ingest_rate_limit)])
def list_emails(
    q: str | None = None,
    limit: int = Query(default=_EMAILS_DEFAULT_LIMIT, ge=1, le=_EMAILS_MAX_LIMIT),
    _: CurrentUser = Depends(require_dev),
) -> list[dict]:
    """Recent inbound ingested emails for the picker, newest first, each with
    `attachment_count` and `stored_count`. `q` is a plain substring search
    over subject / sender address / sender name."""
    term = _sanitize_query(q) if q else ""
    return ri.recent_emails(q=term or None, limit=limit)


# ── Runs: create, upload, start ──────────────────────────────────────────


class RunCreateIn(BaseModel):
    """`{source: "upload"}` opens a staging run that collects files one POST
    at a time until /start; `{source: "email", email_id}` creates a pending
    run over the email's attachment rows and dispatches it at once."""

    source: Literal["upload", "email"]
    email_id: str | None = None

    @model_validator(mode="after")
    def _email_needs_an_id(self) -> RunCreateIn:
        if self.source == "email" and not self.email_id:
            raise ValueError("email_id is required for an email run.")
        return self


@router.post(
    "/runs",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rfp_ingest_rate_limit)],
)
def create_run(
    body: RunCreateIn,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_dev),
) -> dict:
    """Open a run. An upload run answers `staging` with an empty file list;
    an email run answers the run detail already `pending` (409 when the
    email has no attachment rows, 413 over the per-run file cap)."""
    if body.source == "upload":
        run = _service(ri.create_upload_run, created_by=user.id)
        audit(user.id, "rfp_ingest.create", _AUDIT_ENTITY, run["id"], {"source": "upload"})
        run["files"] = []
        run["queue"] = None
        return run
    email_id = _uuid_or_404(body.email_id, _EMAIL_NOT_FOUND)
    run, files = _service(ri.create_email_run, email_id=email_id, created_by=user.id)
    job = _service(ri.dispatch, run["id"], created_by=user.id, background=background)
    audit(
        user.id,
        "rfp_ingest.create",
        _AUDIT_ENTITY,
        run["id"],
        {
            "source": "email",
            "email_id": email_id,
            "file_count": len(files),
            "job_id": job.get("id") if job else None,
        },
    )
    return _run_detail_or_404(run["id"])


@router.post(
    "/runs/{run_id}/files",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_run_file(
    run_id: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_dev),
) -> dict:
    """Add one file to a staging run (one file per request: the global
    request-body cap is per request while the upload cap is per file). A
    PDF, or with office files enabled a Word or Excel document (.docx,
    .xlsx, .doc, .xls; recognised by magic and declared extension, converted
    to PDF by the runner, never opened here).

    The bytes are read into memory under `upload_max_bytes` (413 past it),
    then the service sniffs and hashes them in the worker thread and inserts
    the row BEFORE the quarantine upload so the partial unique index on
    (run_id, sha256) is the duplicate check: a duplicate is a 409 and writes
    nothing. A file rejected at sniff (not a PDF, Word or Excel file, a
    polyglot, empty, over the per-file byte cap) is still RECORDED as
    `rejected` with no object and answered 201, so the run's history shows
    it. 404 unknown run, 409 unless the run is staging, 413 when it already
    holds the per-run maximum. The handler is async only for the streamed
    multipart read."""
    _uuid_or_404(run_id, _RUN_NOT_FOUND)
    s = get_settings()
    content = await _read_capped(file, s.upload_max_bytes)
    return await run_in_threadpool(
        _service,
        ri.add_upload_file,
        run_id,
        filename=file.filename or _DEFAULT_UPLOAD_NAME,
        declared_mime=file.content_type,
        data=content,
    )


@router.post("/runs/{run_id}/start", dependencies=[Depends(ai_rate_limit)])
def start_run(
    run_id: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_dev),
) -> dict:
    """CAS the run staging -> pending and dispatch it (queue, BackgroundTasks
    fallback). 409 when the run is not staging, has no files, or every file
    was rejected at upload."""
    _uuid_or_404(run_id, _RUN_NOT_FOUND)
    run = _service(ri.start_run, run_id)
    _service(ri.dispatch, run["id"], created_by=user.id, background=background)
    detail = _run_detail_or_404(run_id)
    rejected = sum(
        1 for f in detail.get("files") or [] if f.get("status") == protocol.STATUS_REJECTED
    )
    audit(
        user.id,
        "rfp_ingest.start",
        _AUDIT_ENTITY,
        run_id,
        {"file_count": int(run.get("file_count") or 0), "rejected": rejected},
    )
    return detail


# ── Runs: reads ──────────────────────────────────────────────────────────


@router.get("/runs", dependencies=[Depends(rfp_ingest_rate_limit)])
def list_runs(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=_RUNS_DEFAULT_LIMIT, ge=1, le=_RUNS_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(require_dev),
) -> dict:
    """`{rows, total, offset, limit}`, newest first. `status` narrows to one
    run status (400 on an unknown one)."""
    return _service(ri.list_runs, status=status_filter, limit=limit, offset=offset)


@router.get("/runs/{run_id}", dependencies=[Depends(rfp_ingest_rate_limit)])
def get_run(run_id: str, _: CurrentUser = Depends(require_dev)) -> dict:
    """The run, its files (manifest excluded) and `queue`, the llm_jobs poll
    info for the run's job (None when it was never queued)."""
    _uuid_or_404(run_id, _RUN_NOT_FOUND)
    return _run_detail_or_404(run_id)


# ── Files and pages ──────────────────────────────────────────────────────
# The literal sub-paths (/pages, /urls) are registered before the bare
# /files/{file_id} route, the codebase's literal-before-param ordering rule.


@router.get("/files/{file_id}/pages", dependencies=[Depends(rfp_ingest_rate_limit)])
def list_pages(
    file_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=_PAGES_DEFAULT_LIMIT, ge=1, le=_PAGES_MAX_LIMIT),
    _: CurrentUser = Depends(require_dev),
) -> dict:
    """`{rows, total, offset, limit}` in page order; every ok page carries a
    server-minted `thumb_url` (None for failed pages). A file with no pages
    (or no such file) answers an empty page rather than a 404: the file
    detail is the existence check."""
    _uuid_or_404(file_id, _FILE_NOT_FOUND)
    return ri.list_pages(file_id, offset=offset, limit=limit)


@router.get("/files/{file_id}/urls", dependencies=[Depends(rfp_ingest_rate_limit)])
def file_urls(file_id: str, _: CurrentUser = Depends(require_dev)) -> dict:
    """`{images_pdf: [url, ...], text, manifest}`: download URLs for the
    derived objects (the raw file in quarantine never gets one)."""
    _uuid_or_404(file_id, _FILE_NOT_FOUND)
    urls = ri.file_urls(file_id)
    if urls is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _FILE_NOT_FOUND)
    return urls


@router.get("/files/{file_id}", dependencies=[Depends(rfp_ingest_rate_limit)])
def get_file(file_id: str, _: CurrentUser = Depends(require_dev)) -> dict:
    """The file row with its manifest and hazards (the one read that carries
    the manifest jsonb). `source_format` and the manifest's `conversion`
    block say what an office file was taken for and how it became the PDF
    the sandbox verified; the quarantine object itself is never signed."""
    _uuid_or_404(file_id, _FILE_NOT_FOUND)
    row = ri.get_file(file_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _FILE_NOT_FOUND)
    return row


@router.get("/pages/{page_id}/urls", dependencies=[Depends(rfp_ingest_rate_limit)])
def page_urls(page_id: str, _: CurrentUser = Depends(require_dev)) -> dict:
    """`{full}`: the reading-tier signed URL (None for a failed page)."""
    _uuid_or_404(page_id, _PAGE_NOT_FOUND)
    urls = ri.page_urls(page_id)
    if urls is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _PAGE_NOT_FOUND)
    return urls


# ── Runs: cancel, retry, delete ──────────────────────────────────────────


@router.post("/runs/{run_id}/cancel", dependencies=[Depends(rfp_ingest_rate_limit)])
def cancel_run(run_id: str, user: CurrentUser = Depends(require_dev)) -> dict:
    """Cancel a staging, pending or running run (409 otherwise). The run is
    marked canceled first (sticky), then its queued job is canceled; a
    runner mid-child sees the mark, kills the child and hands the in-flight
    file back."""
    _uuid_or_404(run_id, _RUN_NOT_FOUND)
    _service(ri.cancel_run, run_id)
    audit(user.id, "rfp_ingest.cancel", _AUDIT_ENTITY, run_id, {})
    return _run_detail_or_404(run_id)


@router.post("/runs/{run_id}/retry", dependencies=[Depends(ai_rate_limit)])
def retry_run(
    run_id: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_dev),
) -> dict:
    """Retry a failed, partially failed or canceled run: its failed, pending
    and running files go back to pending (outputs removed), rejected files
    are never revisited, and the run is dispatched again. 409 on any other
    status or while a job is still queued or running for the run."""
    _uuid_or_404(run_id, _RUN_NOT_FOUND)
    _service(ri.retry_run, run_id, created_by=user.id, background=background)
    audit(user.id, "rfp_ingest.retry", _AUDIT_ENTITY, run_id, {})
    return _run_detail_or_404(run_id)


@router.delete("/runs/{run_id}", dependencies=[Depends(rfp_ingest_rate_limit)])
def delete_run(run_id: str, user: CurrentUser = Depends(require_dev)) -> dict:
    """Remove a run, its rows (cascade) and both storage prefixes. Only a
    staging or settled run (done, done_with_errors, failed, canceled,
    expired) can go; cancel a live one first and wait for it to stop."""
    _uuid_or_404(run_id, _RUN_NOT_FOUND)
    _service(ri.delete_run, run_id)
    audit(user.id, "rfp_ingest.delete", _AUDIT_ENTITY, run_id, {})
    return {"id": run_id, "deleted": True}
