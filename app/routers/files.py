"""Project file upload / list / signed-download.

Storage objects live in the private `project-files` bucket; downloads are served
as short-TTL signed URLs. The estimator is restricted: they may only read
`drawing`/`specification` files plus the `revision`/`additional`/`addendum`
updates that were actually sent to them (an uploaded-but-unsent update is still
a draft), only write their own `estimate`/`boq`/`markup` deliverables, and only
for projects they are actively assigned to.

Once the initial package has actually been SENT to an estimator (see
`handoff_locked` — a package sent, not merely an assignment created), the
initial `drawing`/`specification` blocks lock — no uploads or deletes. New
material goes in as `revision` ("Changes/Revisions") or `additional`
("Additional files"), each requiring a per-file note; once emailed to the
estimators those files become undeletable too (their notes stay editable).
Addenda are the one category uploadable on BOTH sides of that lock; they carry
an addendum number + issue date instead of a note, and estimators never upload
them.
"""

import os
import threading
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import (
    CurrentUser,
    require_project_assignment,
)
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.file_categories import (  # re-exported: tests import these from files.py
    DOC_TYPE_CATEGORIES,
    DOC_TYPE_REQUIRED_CATEGORIES,
    DOC_TYPES,
    ESTIMATOR_READ,
    ESTIMATOR_WRITE,
    FILE_NOTE_MAX_CHARS,
    INITIAL_CATEGORIES,
    UPDATE_CATEGORIES,
    VALID_CATEGORIES,
)
from app.core.file_categories import (
    # `X as X` marks these as deliberate re-exports (pyflakes/ruff F401), not
    # dead imports: the handler bodies that consume them land in the next wave.
    ADDENDUM_CATEGORY as ADDENDUM_CATEGORY,
    ADDENDUM_NUMBER_MAX_CHARS as ADDENDUM_NUMBER_MAX_CHARS,
    ESTIMATOR_QUERY_CATEGORIES as ESTIMATOR_QUERY_CATEGORIES,
    PACKAGE_CATEGORIES as PACKAGE_CATEGORIES,
    SENT_GATED_CATEGORIES as SENT_GATED_CATEGORIES,
)
from app.core.ratelimit import estimator_rate_limit, export_rate_limit, upload_rate_limit
from app.core.roles import WRITER_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import FilesExportIn
from app.services import estimator_rounds, file_export, office_preview, storage
from app.services.notifications import audit, notify_role, notify_user

# Rate limit estimator file traffic (no-op for internal roles).
router = APIRouter(
    prefix="/projects/{project_id}/files",
    tags=["files"],
    dependencies=[Depends(estimator_rate_limit)],
)

# The category sets now live in ONE place, app/core/file_categories.py — see
# that module's docstring for what each set means and why `addendum` belongs to
# neither INITIAL_CATEGORIES nor UPDATE_CATEGORIES. They are re-exported here
# verbatim because tests/test_file_updates.py and tests/test_estimator_rounds.py
# import them from this module. Do not re-declare them here.

# Only one export archive builds per process at a time. Building downloads and
# compresses every file; letting several run concurrently is the export OOM
# vector. Non-blocking acquire → 429 if one is already in flight.
_export_lock = threading.BoundedSemaphore(1)

# Canonical, safe content-types keyed by extension. An uploaded object's stored
# mime_type is derived from its extension via this map (never trusted from the
# client), and anything unknown is stored as octet-stream, so a file whose bytes
# are really HTML/SVG/JS can't be persisted with an active, inline-renderable
# content-type. This normalizes storage/serving; it does NOT reject uploads.
_SAFE_MIME_BY_EXT: dict[str, str] = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".zip": "application/zip",
}


def _safe_content_type(filename: str | None) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return _SAFE_MIME_BY_EXT.get(ext, "application/octet-stream")


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read the upload into memory, aborting past `max_bytes` instead of
    buffering an unbounded body (single-request OOM defence)."""
    limit_mb = max_bytes // (1024 * 1024)
    # Fast path: reject up front when the multipart part advertises its size.
    if upload.size is not None and upload.size > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File is too large (limit {limit_mb} MB).",
        )
    buf = bytearray()
    while chunk := await upload.read(1024 * 1024):
        buf += chunk
        if len(buf) > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"File is too large (limit {limit_mb} MB).",
            )
    return bytes(buf)

LOCKED_MESSAGE = (
    "Drawings & specifications are locked once an estimator is assigned — "
    "add the file under Changes/Revisions or Additional files instead"
)
NOTE_REQUIRED_MESSAGE = "A note describing this file is required"
NOT_LOCKED_MESSAGE = (
    "No estimator hand-off yet — upload to Drawings & plans or Specifications instead"
)
SENT_IMMUTABLE_MESSAGE = "Files already sent to the estimators cannot be deleted"
# The estimator→team mirror of the two rules above (estimator_rounds.py):
ROUND_SEALED_MESSAGE = (
    "Files already sent to the team cannot be removed — upload a revision instead"
)
ADDITIONAL_TOO_EARLY_MESSAGE = (
    "Additional files are available after your first submission"
)
# Addendum metadata (number + issue date). Mirrors the DB CHECK in 0076.
ADDENDUM_NUMBER_REQUIRED_MESSAGE = "An addendum number is required (e.g. 1, 02, 3A)"
ADDENDUM_DATE_REQUIRED_MESSAGE = "The addendum's issue date is required"
ADDENDUM_META_ONLY_MESSAGE = "Addendum number and issue date only apply to addenda"
DOC_TYPE_REQUIRED_MESSAGE = (
    "Say whether this revision is to the plans/drawings or to the specifications"
)
DOC_TYPE_ONLY_MESSAGE = (
    "Plans/specifications only apply to revisions and addenda — "
    "the initial package says which it is by its own category"
)


def is_handoff_locked(assignments: list[dict]) -> bool:
    """Legacy signal: a package was actually EMAILED to this assignee.

    An UNSENT active assignment no longer locks. It used to, which produced the
    unrecoverable state: assign while Graph was unconfigured -> locked with
    nothing sent -> drawings frozen, no way to send. That state is now
    unreachable (assign 503s without Graph) and the historical ones are unfrozen
    by this change so they can finally be completed."""
    return any(a.get("sent_to_estimator_at") for a in assignments)


def handoff_locked(project_id: str) -> bool:
    sb = get_supabase()
    # The authoritative signal: any send batch means the package left the
    # building. Claimed BEFORE the email in `file_sends.claim_batch`, so this is
    # true the instant a send is committed.
    if (
        sb.table("file_send_batches")
        .select("id")
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    ).data:
        return True
    rows = (
        sb.table("estimator_assignments")
        .select("revoked_at, sent_to_estimator_at")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if is_handoff_locked(rows):
        return True
    # Belt-and-braces: emailed updates (revisions/additional/addenda) also prove
    # the hand-off happened, even if no batch or assignment row carries a stamp.
    updates = (
        sb.table("project_files")
        .select("sent_to_estimators_at")
        .eq("project_id", project_id)
        .in_("category", list(SENT_GATED_CATEGORIES))
        .execute()
    ).data or []
    return any(u.get("sent_to_estimators_at") for u in updates)


def _estimator_visible(rec: dict, user_id: str) -> bool:
    """Category-level read gate for the external estimator."""
    if rec["category"] in ESTIMATOR_READ:
        return True
    if rec["category"] in ESTIMATOR_WRITE:
        # A deliverable belongs to the estimator who uploaded it. With more than
        # one active assignee (which "Re-assign" makes routine) an estimator
        # must never read a competitor's estimate workbook. Delete was already
        # uploader-scoped; read was not.
        return rec.get("uploaded_by") == user_id
    # Updates AND addenda only become visible once actually emailed — an
    # uploaded-but-unsent revision or addendum is still a draft. `.get`, never
    # `[]`: some callers pass a dict with no such key.
    return rec["category"] in SENT_GATED_CATEGORIES and bool(
        rec.get("sent_to_estimators_at")
    )


def _get_file_checked(project_id: str, file_id: str, user: CurrentUser) -> dict:
    """Load a file row, 404 if missing, and enforce the estimator category guard
    (auditing denials — an important signal for the external estimator)."""
    rec = (
        get_supabase()
        .table("project_files")
        .select("*")
        .eq("id", file_id)
        .eq("project_id", project_id)
        .single()
        .execute()
    ).data
    if not rec:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if user.role == Role.ESTIMATOR and not _estimator_visible(rec, user.id):
        audit(user.id, "access.denied", "project_file", file_id, {"category": rec["category"]})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return rec


@router.get("")
def list_files(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    q = get_supabase().table("project_files").select("*").eq("project_id", project_id)
    if user.role == Role.ESTIMATOR:
        q = q.in_("category", list(ESTIMATOR_QUERY_CATEGORIES))
    rows = q.order("created_at", desc=True).execute().data or []
    if user.role == Role.ESTIMATOR:
        rows = [r for r in rows if _estimator_visible(r, user.id)]
        # The log is the estimator's only view of send history, and it is scoped
        # to batches addressed to THEM. Leaving the raw stamp here hands a
        # re-assigned estimator send timestamps that predate their assignment —
        # i.e. proof that other sends, and therefore other recipients, exist.
        # The column stays in the query so _estimator_visible still gates on it;
        # only the serialized value is blanked.
        for r in rows:
            r["sent_to_estimators_at"] = None
    return rows


@router.get("/lock")
def lock_state(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    """Whether the initial drawing/spec blocks are locked (package actually
    sent) — lets the UI collapse the blocks and reroute uploads to
    Changes/Revisions. Role-branched: the estimator gets only facts about
    themselves; project-wide counts would leak that earlier sends (and therefore
    other recipients) exist."""
    from app.services import file_sends

    locked = handoff_locked(project_id)
    if user.role == Role.ESTIMATOR:
        stats = file_sends.batch_stats(project_id, estimator_id=user.id)
        return {"locked": locked, "sent": stats["batch_count"] > 0}
    stats = file_sends.batch_stats(project_id)
    return {
        "locked": locked,
        "sent": stats["package_sent_at"] is not None,  # kind='initial' EXISTS
        "batch_count": stats["batch_count"],
        "first_sent_at": stats["first_sent_at"],
    }


@router.get("/send-batches")
def send_batches(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    """The Plans & Specs Log — every send batch this caller is entitled to see.

    Two role-shaped projections built entirely inside `file_sends.build_log`
    (never one payload post-filtered): the internal viewer sees recipients and
    the sender; the estimator sees only batches addressed to them, with the
    recipient/sender keys ABSENT and 'reassign' collapsed to 'initial'. Returned
    as the raw role-shaped dict — no response_model — so the estimator's absent
    keys stay absent rather than serializing as null.
    """
    from app.services import file_sends

    payload = file_sends.build_log(project_id, user)
    if user.role == Role.ESTIMATOR:
        # External-user reads are a security signal (same rationale as
        # file.download). No audit for internal reads.
        audit(
            user.id,
            "estimator.log_view",
            "project",
            project_id,
            {"batch_count": len(payload.get("batches", []))},
        )
    return payload


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_file(
    project_id: str,
    background: BackgroundTasks,
    category: str = Form(...),
    material_category_id: str | None = Form(None),
    note: str | None = Form(None),
    doc_type: str | None = Form(None),  # 'drawing' | 'specification' (0077)
    addendum_number: str | None = Form(None),
    addendum_issued_on: str | None = Form(None),  # ISO calendar date "YYYY-MM-DD"
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_project_assignment),
):
    if category not in VALID_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")
    if user.role == Role.ESTIMATOR and category not in ESTIMATOR_WRITE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators may only upload deliverable files")
    if user.role != Role.ESTIMATOR and category == "estimator_additional":
        # The estimator's own box; team-side extras go in 'other' or the
        # update categories.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Estimator-only category")
    # Writers (internal minus the read-only accountant) or the estimator (gated
    # above) may upload; the accountant is read-only.
    if user.role not in WRITER_ROLES and user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    if user.role == Role.ESTIMATOR and category == "estimator_additional":
        # Additional files only make sense as part of a revision round — the
        # original hand-off is estimate/boq/markup.
        if await run_in_threadpool(estimator_rounds.latest_submission, project_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ADDITIONAL_TOO_EARLY_MESSAGE)

    note = (note or "").strip() or None
    if note and len(note) > FILE_NOTE_MAX_CHARS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Note is too long")

    if category == ADDENDUM_CATEGORY:
        # Estimators never reach here: 'addendum' is not in ESTIMATOR_WRITE, so
        # the ESTIMATOR_WRITE gate above already 403s them. This is the only
        # thing enforcing "estimators view addenda but never upload them" — an
        # addendum carries a number + issue date instead of a note, and is
        # uploadable on BOTH sides of the hand-off lock (neither UPDATE_CATEGORIES
        # nor INITIAL_CATEGORIES contains it, so the lock branches below skip it).
        addendum_number = (addendum_number or "").strip()
        if not addendum_number:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ADDENDUM_NUMBER_REQUIRED_MESSAGE)
        if len(addendum_number) > ADDENDUM_NUMBER_MAX_CHARS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Addendum number is too long")
        try:
            issued_on = date.fromisoformat((addendum_issued_on or "").strip())
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ADDENDUM_DATE_REQUIRED_MESSAGE)
        # +1 day of clock skew; a far-future date is a typo on a document that,
        # by definition, has already been issued.
        if issued_on > (datetime.now(timezone.utc).date() + timedelta(days=1)):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "The addendum issue date cannot be in the future"
            )
    elif addendum_number or addendum_issued_on:
        # Addendum metadata on a non-addendum — mirrors the DB CHECK.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, ADDENDUM_META_ONLY_MESSAGE)

    # doc_type — WHICH DOCUMENT SET a post-hand-off file belongs to (0077).
    # Orthogonal to `category`: it splits the one "Changes/Revisions" bucket into
    # revised plans vs revised specs so the modal, the email and the log can keep
    # them apart. Required for revisions (the Revisions modal always knows which
    # section the file came from); optional for addenda, which the initial
    # "Upload plans and specs" modal also uploads without asking. Legacy rows
    # predating 0077 stay NULL and render in the untitled group.
    doc_type = (doc_type or "").strip() or None
    if doc_type is not None:
        if doc_type not in DOC_TYPES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid document type")
        if category not in DOC_TYPE_CATEGORIES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, DOC_TYPE_ONLY_MESSAGE)
    elif category in DOC_TYPE_REQUIRED_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, DOC_TYPE_REQUIRED_MESSAGE)

    if category in UPDATE_CATEGORIES:
        # Updates only exist relative to a hand-off, and each must say what it
        # is — the note travels with the file to the estimators.
        if not note:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, NOTE_REQUIRED_MESSAGE)
        if not await run_in_threadpool(handoff_locked, project_id):
            raise HTTPException(status.HTTP_409_CONFLICT, NOT_LOCKED_MESSAGE)
    elif category in INITIAL_CATEGORIES and await run_in_threadpool(handoff_locked, project_id):
        raise HTTPException(status.HTTP_409_CONFLICT, LOCKED_MESSAGE)

    content = await _read_capped(file, get_settings().upload_max_bytes)
    # Never trust the client-supplied content-type: derive a safe, canonical one
    # from the extension so a stored file can't later be served as active HTML.
    stored_mime = _safe_content_type(file.filename)
    path = storage.build_object_path(project_id, category, file.filename or "upload")
    await run_in_threadpool(storage.upload_file, path, content, stored_mime)

    convertible = office_preview.is_convertible(file.filename, category)
    insert = (
        get_supabase()
        .table("project_files")
        .insert(
            {
                "project_id": project_id,
                "category": category,
                "storage_path": path,
                "filename": file.filename,
                "material_category_id": material_category_id,
                "uploaded_by": user.id,
                "mime_type": stored_mime,
                "size_bytes": len(content),
                "preview_status": "pending" if convertible else "none",
                "note": note,
                # NULL for everything except revisions/addenda (0077 CHECK).
                "doc_type": doc_type,
                # Both NULL for non-addenda (required by the 0076 DB CHECK);
                # `issued_on` is only bound in the addendum branch above, so the
                # conditional never evaluates it for other categories.
                "addendum_number": addendum_number or None,
                "addendum_issued_on": (
                    issued_on.isoformat() if category == ADDENDUM_CATEGORY else None
                ),
                # Estimator deliverables start as drafts of the open round —
                # sealed (submission_round stamped) only when they press Send.
                "estimator_deliverable": user.role == Role.ESTIMATOR
                and category in ESTIMATOR_WRITE,
            }
        )
    )
    try:
        row = (await run_in_threadpool(insert.execute)).data[0]
    except Exception:
        # The object was PUT before this insert; without its row nothing would
        # ever reclaim it (e.g. the project was discarded mid-upload, making the
        # insert fail its FK). Best-effort — the original error is the answer.
        try:
            await run_in_threadpool(storage.delete_file, path)
        except Exception:  # noqa: BLE001
            pass
        raise
    audit_payload = {"category": category}
    if doc_type:
        audit_payload["doc_type"] = doc_type
    if category == ADDENDUM_CATEGORY:
        audit_payload["addendum_number"] = addendum_number
    await run_in_threadpool(
        audit, user.id, "file.upload", "project_file", row["id"], audit_payload
    )
    if convertible:
        # Sync task → runs in the threadpool after the response; never blocks.
        background.add_task(office_preview.generate_preview, row["id"])

    # Adding a drawing after intake means whoever prices off the drawings should
    # re-check their work. (Multiple drawings per project are legitimate.)
    if category in ("drawing", "electrical_drawing"):
        await run_in_threadpool(
            _notify_drawing_changed, project_id, user, "added", category
        )

    return row


def _notify_drawing_changed(
    project_id: str, user: CurrentUser, verb: str, category: str = "drawing"
) -> None:
    """Alert the Estimating Engineer + assigned estimator that a project's drawings
    changed, post-intake.

    `verb` is "added" or "removed"; `category` is 'drawing' (General) or
    'electrical_drawing'. During intake nothing is sent — the Estimating
    Admin is still assembling the package and no one is pricing off it yet.
    """
    from app.services import workflow

    proj = (
        get_supabase()
        .table("projects")
        .select("current_stage, name, number")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    # During intake nothing is sent — the Estimating Admin is still assembling the
    # package and no one is pricing off it yet. Suppress until intake completes.
    if not proj or not workflow.is_category_complete(
        workflow.load_category_state(project_id), "intake"
    ):
        return

    label = f"{proj.get('number') or ''} {proj.get('name') or ''}".strip() or "a project"
    noun = "Electrical drawing" if category == "electrical_drawing" else "General drawing"
    msg = f"{noun} {verb} for {label} - re-check anything priced off it."
    # A drawing change can invalidate material pricing AND labor counts — both
    # engineer focuses need the re-check ping.
    notify_role(Role.ESTIMATING_ENGINEER_MATERIALS, project_id, "drawing_changed", msg)
    notify_role(Role.ESTIMATING_ENGINEER_LABOR, project_id, "drawing_changed", msg)

    # Plus any currently-assigned (active) estimator on this project.
    assignments = (
        get_supabase()
        .table("estimator_assignments")
        .select("estimator_id")
        .eq("project_id", project_id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .execute()
    ).data or []
    for est_id in {a["estimator_id"] for a in assignments}:
        notify_user(est_id, project_id, "drawing_changed", msg)


class FileNoteIn(BaseModel):
    note: str

    @field_validator("note")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("note must not be empty")
        if len(v) > FILE_NOTE_MAX_CHARS:
            raise ValueError("note is too long")
        return v


@router.patch("/{file_id}/note")
def update_note(
    project_id: str,
    file_id: str,
    body: FileNoteIn,
    user: CurrentUser = Depends(require_project_assignment),
):
    """Edit an update file's note. Notes stay editable even after the file is
    sent (the file is immutable, its description isn't) — the estimators keep
    the emailed wording; the app always shows the latest."""
    if user.role not in WRITER_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    # .limit(1), not .single(): a foreign/unknown id must be a clean 404.
    rows = (
        get_supabase()
        .table("project_files")
        .select("id, category")
        .eq("id", file_id)
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    ).data or []
    rec = rows[0] if rows else None
    if not rec:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if rec["category"] not in UPDATE_CATEGORIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Notes only apply to Changes/Revisions and Additional files",
        )
    row = (
        get_supabase()
        .table("project_files")
        .update({"note": body.note})
        .eq("id", file_id)
        .execute()
    ).data[0]
    audit(user.id, "file.note_updated", "project_file", file_id, None)
    return row


@router.get("/{file_id}/download")
def download_url(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    rec = _get_file_checked(project_id, file_id, user)
    # Serve as an attachment so a top-level open of the URL downloads the file
    # rather than rendering it inline (defuses a stored HTML/SVG masquerade).
    url = storage.signed_url(rec["storage_path"], download=rec["filename"] or True)
    audit(user.id, "file.download", "project_file", file_id, None)
    return {"url": url, "filename": rec["filename"]}


@router.post("/export", dependencies=[Depends(export_rate_limit)])
async def export_files(
    project_id: str,
    body: FilesExportIn | None = Body(default=None),
    user: CurrentUser = Depends(require_project_assignment),
):
    """Bundle the project's files into a single `.zip` download.

    No body (or `{}`) exports every file the caller may read; pass
    `{"file_ids": [...]}` for a subset. Estimators are restricted to the same
    categories they may read elsewhere (drawings + their estimate/boq/markup),
    so a foreign or forbidden id simply doesn't match — no leak, no IDOR.

    The whole body is guarded so any failure surfaces as an HTTPException (which
    keeps CORS headers) rather than a raw 500 that the browser reports as the
    opaque "Failed to fetch".
    """
    try:
        sb = get_supabase()
        q = (
            sb.table("project_files")
            # `uploaded_by` is MANDATORY here: _estimator_visible now scopes
            # ESTIMATOR_WRITE reads to the uploader, and without this column
            # every row evaluates `None == user.id` and 100% of an estimator's
            # own deliverables silently drop from their ZIP (a 404 below).
            .select(
                "id, category, storage_path, filename, size_bytes, "
                "sent_to_estimators_at, uploaded_by"
            )
            .eq("project_id", project_id)
        )
        if user.role == Role.ESTIMATOR:
            q = q.in_("category", list(ESTIMATOR_QUERY_CATEGORIES))
        else:
            # Internal exports must never bundle an unsent estimator draft —
            # the team only receives files the estimator actually sent.
            q = estimator_rounds.exclude_unsent(q)
        if body and body.file_ids is not None:
            q = q.in_("id", body.file_ids)
        rows = (await run_in_threadpool(q.execute)).data or []
        if user.role == Role.ESTIMATOR:
            rows = [r for r in rows if _estimator_visible(r, user.id)]
        if not rows:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No files to export")

        # size_bytes is nullable (added in 0021 with no default) — coalesce so a
        # legacy NULL can't crash or silently defeat the OOM guard.
        total = sum((r.get("size_bytes") or 0) for r in rows)
        max_bytes = get_settings().export_max_total_bytes
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"Too large to export at once (~{total // (1024 * 1024)} MB); "
                "select fewer files.",
            )

        # Only one archive builds per process at a time — concurrent builds are
        # the export OOM vector. Fail fast with a retriable, code-tagged 429
        # rather than let builds pile up and exhaust RAM.
        if not _export_lock.acquire(blocking=False):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=ErrorCode.RATE_LIMITED,
                headers={"Retry-After": "10", "X-RateLimit-Scope": RateLimitScope.FILE_EXPORT},
            )
        try:
            # Build into a spooled temp file (spills to disk past 8MB) so the whole
            # archive is never resident in RAM; stream it out afterwards.
            spool, manifest, size = await run_in_threadpool(
                file_export.build_export_spooled, rows
            )
        finally:
            _export_lock.release()

        ok_count = sum(1 for m in manifest if m["status"] == "ok")
        if ok_count == 0:
            # Every object was missing from storage — nothing to hand back.
            spool.close()
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No files to export")

        proj_q = sb.table("projects").select("number, name").eq("id", project_id).single()
        proj = (await run_in_threadpool(proj_q.execute)).data or {}
        # files_exported_at drives the internal "export your files" banner —
        # only an internal export may clear it, never the external estimator
        # downloading their own package.
        if user.role != Role.ESTIMATOR:
            stamp = sb.table("projects").update(
                {"files_exported_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", project_id)
            await run_in_threadpool(stamp.execute)
        await run_in_threadpool(
            audit,
            user.id,
            "file.export",
            "project",
            project_id,
            {"count": ok_count, "subset": bool(body and body.file_ids is not None)},
        )

        filename = file_export.export_filename(proj)

        def _stream_zip():
            try:
                while chunk := spool.read(262144):
                    yield chunk
            finally:
                spool.close()

        return StreamingResponse(
            _stream_zip(),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"; '
                    f"filename*=UTF-8''{quote(filename)}"
                ),
                "Content-Length": str(size),
                "Cache-Control": "no-store",
                "X-Export-File-Count": str(ok_count),
            },
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — keep CORS headers; never a raw 500
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Export failed"
        ) from exc


@router.get("/{file_id}/preview-url")
def preview_url(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    """Signed URL for a file's PDF preview derivative.

    200 with the URL when the derivative is ready; 202 with just the status
    otherwise (pending/failed/none) so the frontend can poll or fall back.
    """
    rec = _get_file_checked(project_id, file_id, user)
    if rec.get("preview_status") == "ready" and rec.get("preview_path"):
        url = storage.signed_url(rec["preview_path"])
        audit(user.id, "file.preview", "project_file", file_id, None)
        return {
            "preview_status": "ready",
            "url": url,
            "filename": rec["filename"],
            "kind": "pdf",
        }
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"preview_status": rec.get("preview_status") or "none"},
    )


@router.get("/{file_id}/preview")
def preview_file(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    """Server-side render of a stored .xlsx into rows (last-resort fallback when
    the PDF derivative isn't available, plus compact inline tables)."""
    rec = _get_file_checked(project_id, file_id, user)
    if not (rec["filename"] or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Preview only supports .xlsx files")

    from app.services.rfq_excel import rows_for_preview

    rows = rows_for_preview(storage.download_file(rec["storage_path"]))
    return {"filename": rec["filename"], "rows": rows}


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    rec = (
        get_supabase()
        .table("project_files")
        .select("*")
        .eq("id", file_id)
        .eq("project_id", project_id)
        .single()
        .execute()
    ).data
    if not rec:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")

    # The estimator may only remove their own deliverable uploads (never drawings).
    if user.role == Role.ESTIMATOR:
        if rec["category"] not in ESTIMATOR_WRITE or rec.get("uploaded_by") != user.id:
            audit(user.id, "access.denied", "project_file", file_id, {"category": rec["category"]})
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
        # Deliverables already sent to the team are what the team is pricing
        # from — immutable to the estimator (mirror of SENT_IMMUTABLE_MESSAGE).
        if rec.get("submission_round") is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, ROUND_SEALED_MESSAGE)
    elif user.role not in WRITER_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")

    # Once the hand-off has begun, the initial package is what the estimators
    # are pricing off — it can't quietly shrink. Post-hand-off corrections go
    # in as Changes/Revisions instead.
    if rec["category"] in INITIAL_CATEGORIES and handoff_locked(project_id):
        raise HTTPException(status.HTTP_409_CONFLICT, LOCKED_MESSAGE)
    # Same once an update OR addendum was emailed: sent files are evidence of
    # what the estimators received (mirrors the sent-proposal rule below).
    if rec["category"] in SENT_GATED_CATEGORIES and rec.get("sent_to_estimators_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, SENT_IMMUTABLE_MESSAGE)

    # Sent proposals are evidence of what we bid — immutable; and only a writer
    # role may delete even an unsent generated proposal.
    if rec["category"] == "proposal":
        if user.role not in WRITER_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
        sent_ref = (
            get_supabase()
            .table("proposal_sends")
            .select("id")
            .eq("file_id", file_id)
            .in_("status", ["sent", "sending"])
            .limit(1)
            .execute()
        ).data
        if sent_ref:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Sent proposals are immutable and cannot be deleted"
            )

    if user.role == Role.ESTIMATOR:
        # Row first, and conditionally: a Send racing this delete seals the row,
        # the condition then matches nothing, and the file stays part of its
        # announced round. (Better a possible orphaned storage object than a
        # sealed round row whose object is gone — so storage cleanup below is
        # best-effort.)
        deleted = (
            get_supabase()
            .table("project_files")
            .delete()
            .eq("id", file_id)
            .is_("submission_round", "null")
            .execute()
        ).data or []
        if not deleted:
            raise HTTPException(status.HTTP_409_CONFLICT, ROUND_SEALED_MESSAGE)
        cleanup_paths = {rec["storage_path"]}
    else:
        storage.delete_file(rec["storage_path"])
        cleanup_paths = set()
    # The derivative path is deterministic — delete it unconditionally (not just
    # when preview_path is set) so a conversion racing this delete can't leave
    # an orphan behind. Best-effort: an orphan must never block the delete.
    cleanup_paths |= {
        rec.get("preview_path"),
        office_preview.preview_object_path(project_id, file_id),
    }
    for path in filter(None, cleanup_paths):
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001
            pass
    if user.role != Role.ESTIMATOR:
        get_supabase().table("project_files").delete().eq("id", file_id).execute()
    audit(user.id, "file.delete", "project_file", file_id, {"category": rec["category"]})

    # Removing a drawing after intake is a change downstream pricers must know about.
    if rec["category"] in ("drawing", "electrical_drawing"):
        _notify_drawing_changed(project_id, user, "removed", rec["category"])
