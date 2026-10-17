"""Missing files on an RFP-created project: /projects/{id}/rfp-files
(docs/RFP_MISSING_FILES.md section 7).

The callout on the project page reads the issue (every file or link the
harvest could not bring in, why, the link, the email) and acts on it: mark
an item handled or reopen it, dismiss them all (undoable), and drop the
files a person fetched by hand (PDFs through the Bid File Splitter on a job
linked to the project, Word and Excel whole).

Access:

- the env switch: every route 404s with the bare body while RFP Ingestion
  is off (RFP_INGESTION_ENABLED, read through `getattr` so an absent flag
  fails closed), the same switch the project page's `rfp_created` block
  reads; mounted under the bidding sub-app;
- every route depends on `require_project_assignment` (the project's own
  gate) and on `require_internal` for the read (the external estimator
  never sees the callout); the writes take `require_writer` (the project
  writers, the upload route's gate; the read-only accountant 403s);
- rate limits: the catch-all budget per account, the upload budget on the
  drop.

Event-loop rule: every handler is a plain `def` except the drop, which is
async only for the streamed multipart read and runs every sync call through
`run_in_threadpool` (the splitter upload's pattern).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_internal, require_project_assignment, require_writer
from app.core.error_codes import RateLimitScope
from app.core.ratelimit import large_upload_slot, rate_limit, upload_rate_limit
from app.core.supabase_client import get_supabase
from app.routers.files import _read_capped
from app.services import rfp_file_drop, rfp_file_issues

logger = logging.getLogger(__name__)


def rfp_files_enabled() -> bool:
    """Served while RFP Ingestion is (the only way a project carries items)."""
    return bool(getattr(get_settings(), "rfp_ingest_enabled", False))


def require_rfp_files() -> None:
    """Router-level dependency: 404 with the bare body before auth runs."""
    if not rfp_files_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


rfp_files_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

router = APIRouter(
    prefix="/projects/{project_id}/rfp-files",
    tags=["rfp-files"],
    dependencies=[Depends(require_rfp_files)],
)

_MSG_NOT_RFP = "This project was not created from an RFP invitation."
_MSG_ITEM_NOT_FOUND = "This item is not open on this project."
_MSG_ITEM_NOT_RESOLVED = "This item is not marked handled on this project."
_MSG_PROJECT_NOT_FOUND = "Project not found"


def _uuid_or_404(value: str | None, message: str) -> str:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, message) from None
    return str(value)


def _issue(sb, project_id: str) -> dict | None:
    return rfp_file_issues.issue_for_project(sb, project_id)


def _item_answer(sb, project_id: str, row: dict) -> dict:
    names = {}
    if row.get("resolved_by"):
        rows = sb.table("profiles").select("id, full_name").eq("id", row["resolved_by"]).limit(1).execute().data or []
        names = {str(r["id"]): r.get("full_name") for r in rows}
    return {"item": rfp_file_issues.item_wire(row, names), "issue": _issue(sb, project_id)}


# ── Routes (literal paths before parameters) ─────────────────────────────


@router.get("/issue", dependencies=[Depends(rfp_files_rate_limit), Depends(require_internal)])
def get_rfp_files_issue(project_id: str, _: CurrentUser = Depends(require_project_assignment)) -> dict | None:
    """The issue (section 7), or null when the project has no items."""
    _uuid_or_404(project_id, _MSG_PROJECT_NOT_FOUND)
    return _issue(get_supabase(), project_id)


@router.post("/dismiss", dependencies=[Depends(rfp_files_rate_limit), Depends(require_writer)])
def dismiss_rfp_files(project_id: str, user: CurrentUser = Depends(require_project_assignment)) -> dict:
    """"Dismiss all": every open item resolved (`dismiss_all`) with who and
    when on the record. 409 only when nothing is open: a second dismissal
    (items a later sync added) re-stamps and resolves the new ones. Audited
    `rfp_files.dismiss`."""
    _uuid_or_404(project_id, _MSG_NOT_RFP)
    sb = get_supabase()
    try:
        record = rfp_file_issues.dismiss_all(sb, project_id, user.id)
    except rfp_file_issues.IssueConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _MSG_NOT_RFP)
    return {"issue": _issue(sb, project_id)}


@router.delete("/dismiss", dependencies=[Depends(rfp_files_rate_limit), Depends(require_writer)])
def undismiss_rfp_files(project_id: str, user: CurrentUser = Depends(require_project_assignment)) -> dict:
    """Undo "Dismiss all": exactly the items it resolved reopen. 409 when
    not dismissed. Audited `rfp_files.undismiss`."""
    _uuid_or_404(project_id, _MSG_NOT_RFP)
    sb = get_supabase()
    try:
        record = rfp_file_issues.undismiss(sb, project_id, user.id)
    except rfp_file_issues.IssueConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _MSG_NOT_RFP)
    return {"issue": _issue(sb, project_id)}


def _project_exists(sb, project_id: str) -> bool:
    return bool(sb.table("projects").select("id").eq("id", project_id).limit(1).execute().data)


@router.post(
    "/drop",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit), Depends(require_writer)],
)
async def drop_rfp_file(
    project_id: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    job_id: str | None = Form(None),
    user: CurrentUser = Depends(require_project_assignment),
) -> dict:
    """One file a person fetched by hand (section 5): a PDF to the Bid File
    Splitter on the project's drop job (`job_id` from the batch's first
    answer joins it), or whole into the project documents. `{route, job_id,
    split_file_id, project_file_id, category, resolved, issue}`. 415 for
    anything but PDF, Word and Excel (or a PDF that will not open), 413 over
    the file cap, 409 from the upload policy's lock. Audited
    `rfp_files.drop`."""
    _uuid_or_404(project_id, _MSG_PROJECT_NOT_FOUND)
    s = get_settings()
    sb = get_supabase()
    if not await run_in_threadpool(_project_exists, sb, project_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _MSG_PROJECT_NOT_FOUND)
    # A large body holds an in-flight slot for exactly as long as it is
    # resident (the upload route's rule).
    with large_upload_slot(user.id, file.size):
        content = await _read_capped(file, s.upload_max_bytes)
        try:
            result = await run_in_threadpool(
                rfp_file_drop.drop, sb, project_id, user, file.filename, content,
                job_id=job_id if isinstance(job_id, str) and job_id.strip() else None,
                background=background, settings=s,
            )
        except rfp_file_drop.DropRefused as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        finally:
            del content
    issue = await run_in_threadpool(_issue, sb, project_id)
    return {**result, "issue": issue}


@router.post(
    "/items/{item_id}/resolve", dependencies=[Depends(rfp_files_rate_limit), Depends(require_writer)],
)
def resolve_rfp_file_item(
    project_id: str, item_id: str, user: CurrentUser = Depends(require_project_assignment),
) -> dict:
    """Mark one item handled. 404 when it is not this project's or is
    already resolved. Audited `rfp_files.resolve`."""
    _uuid_or_404(project_id, _MSG_ITEM_NOT_FOUND)
    _uuid_or_404(item_id, _MSG_ITEM_NOT_FOUND)
    sb = get_supabase()
    row = rfp_file_issues.resolve(sb, project_id, item_id, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _MSG_ITEM_NOT_FOUND)
    return _item_answer(sb, project_id, row)


@router.delete(
    "/items/{item_id}/resolve", dependencies=[Depends(rfp_files_rate_limit), Depends(require_writer)],
)
def reopen_rfp_file_item(
    project_id: str, item_id: str, user: CurrentUser = Depends(require_project_assignment),
) -> dict:
    """Reopen a handled item (any resolution). 404 when it is not this
    project's or is not resolved. Audited `rfp_files.reopen`."""
    _uuid_or_404(project_id, _MSG_ITEM_NOT_RESOLVED)
    _uuid_or_404(item_id, _MSG_ITEM_NOT_RESOLVED)
    sb = get_supabase()
    row = rfp_file_issues.reopen(sb, project_id, item_id, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _MSG_ITEM_NOT_RESOLVED)
    return _item_answer(sb, project_id, row)
