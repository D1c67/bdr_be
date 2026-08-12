"""Saved New Bid drafts. The intake form parked before the project exists:
saving a draft has NO side effects (no project row, no number reservation, no
emails), and only name + number are required. Everything else the form holds
rides in an opaque `data` blob the frontend re-loads verbatim.

Drafts are a shared team resource, same trust model as projects: any writer may
list, read, update or delete any draft. A draft reserves nothing - the number
is deliberately not unique here, POST /projects still 409s a duplicate at
create time.

The one field the backend looks inside the blob for is the confidential actual
(to-GC) bid date, `data.fields.actual_bid_at`: responses strip it for roles
outside ACTUAL_BID_VIEWER_ROLES, and writes by roles outside
ACTUAL_BID_EDITOR_ROLES can neither set, change nor clear it - a PUT carries
the stored value forward untouched.

Files (0109): a draft also holds the files attached in the New Bid modal,
limited to the intake package categories (drawing, electrical_drawing,
specification, addendum). Objects are stored under `drafts/{draft_id}/` in the
same bucket as project files, with the same `{category}/{uuid}-{name}` key
scheme. Addendum metadata (number, issue date, doc_type) is OPTIONAL at draft
stage - a draft saves incomplete work - but validated whenever present, and a
PATCH may set or explicitly clear it. POST /bid-drafts/{id}/transfer then MOVES
every object onto a project (prefix swap, bytes never re-uploaded), inserts
project_files rows shaped exactly like a fresh intake upload, and retires the
draft. The transfer refuses (400) before moving anything if any addendum still
lacks its number or a non-future issue date, and is retryable: each moved file
loses its bid_draft_files row immediately, so a retry after a mid-way failure
only processes the remainder.
"""

import os
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_writer
from app.core.file_categories import (
    ADDENDUM_CATEGORY,
    ADDENDUM_NUMBER_MAX_CHARS,
    DOC_TYPES,
    INITIAL_CATEGORIES,
)
from app.core.ratelimit import upload_rate_limit
from app.core.roles import ACTUAL_BID_EDITOR_ROLES, ACTUAL_BID_VIEWER_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import office_preview, storage
from app.services.notifications import audit

router = APIRouter(prefix="/bid-drafts", tags=["bid-drafts"])

# Mirrors PMProjectCreate's caps (schemas.py); ProjectCreate itself has none.
DRAFT_NAME_MAX = 300
DRAFT_NUMBER_MAX = 100

# The list view never carries the blob - it exists so the New Bid page can show
# a compact picker, and omitting `data` also keeps the confidential
# fields.actual_bid_at out of responses that were never redacted.
_LIST_SELECT = "id, name, number, created_by, created_at, updated_at"

# ── Draft files (0109) ───────────────────────────────────────────────────────

# What the New Bid modal attaches: the intake package blocks plus addenda.
DRAFT_FILE_CATEGORIES = INITIAL_CATEGORIES | {ADDENDUM_CATEGORY}

ADDENDUM_META_ONLY_MESSAGE = "Addendum number and issue date only apply to addenda"
ADDENDUM_DATE_INVALID_MESSAGE = "The addendum's issue date must be a valid date (YYYY-MM-DD)"
ADDENDUM_DATE_FUTURE_MESSAGE = "The addendum issue date cannot be in the future"
DOC_TYPE_ONLY_MESSAGE = "A document type only applies to addenda"
TRANSFER_INCOMPLETE_MESSAGE = (
    "Every addendum needs its number and issue date before the files can move "
    "to the project. Missing on: "
)

# What a draft-file response carries - never storage_path (server detail; the
# frontend renames/deletes by id and downloads nothing until transfer).
_FILE_OUT_FIELDS = (
    "id",
    "category",
    "filename",
    "size_bytes",
    "addendum_number",
    "addendum_issued_on",
    "doc_type",
    "created_at",
)

# Deliberate duplicates of app/routers/files.py's private helpers (that module
# is single-owner for the project-files surface; drafts mirror its behavior
# rather than importing its internals). Keep the two in sync.
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


def _file_out(row: dict) -> dict:
    return {k: row.get(k) for k in _FILE_OUT_FIELDS}


def _validate_addendum_meta(
    category: str,
    addendum_number: str | None,
    addendum_issued_on: str | None,
    doc_type: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Normalize + validate the OPTIONAL addendum metadata trio.

    Unlike files.py's upload, nothing is required here - a draft saves
    incomplete work, and the transfer endpoint enforces completeness. What IS
    enforced is well-formedness whenever a value is present, and the same
    category pairing files.py applies: metadata on a non-addendum is rejected.
    Returns (number, issued_on ISO string, doc_type), each None when absent.
    """
    number = (addendum_number or "").strip() or None
    issued_raw = (addendum_issued_on or "").strip() or None
    doc_type = (doc_type or "").strip() or None

    if category != ADDENDUM_CATEGORY:
        if number or issued_raw:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ADDENDUM_META_ONLY_MESSAGE)
        if doc_type:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, DOC_TYPE_ONLY_MESSAGE)
        return None, None, None

    if number and len(number) > ADDENDUM_NUMBER_MAX_CHARS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Addendum number is too long")
    issued_on = None
    if issued_raw:
        try:
            issued_on = date.fromisoformat(issued_raw)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ADDENDUM_DATE_INVALID_MESSAGE)
        # +1 day of clock skew, same as files.py: a far-future date is a typo on
        # a document that, by definition, has already been issued.
        if issued_on > (datetime.now(timezone.utc).date() + timedelta(days=1)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ADDENDUM_DATE_FUTURE_MESSAGE)
    if doc_type and doc_type not in DOC_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid document type")
    return number, issued_on.isoformat() if issued_on else None, doc_type


def _get_draft_file(draft_id: UUID, file_id: UUID) -> dict:
    """The full stored bid_draft_files row, or a 404."""
    rows = (
        get_supabase()
        .table("bid_draft_files")
        .select("*")
        .eq("id", str(file_id))
        .eq("draft_id", str(draft_id))
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return rows[0]


def _list_draft_files(draft_id: UUID) -> list[dict]:
    rows = (
        get_supabase()
        .table("bid_draft_files")
        .select("*")
        .eq("draft_id", str(draft_id))
        .order("created_at")
        .execute()
    ).data or []
    return [_file_out(r) for r in rows]


class BidDraftIn(BaseModel):
    name: str = Field(min_length=1, max_length=DRAFT_NAME_MAX)
    number: str = Field(min_length=1, max_length=DRAFT_NUMBER_MAX)
    # The rest of the intake form, verbatim. Opaque to the backend except for
    # the actual_bid_at rules below; any JSON object is accepted.
    data: dict = Field(default_factory=dict)

    @field_validator("name", "number")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Cannot be empty")
        return v


def _strip_actual_bid_at(data: dict) -> dict:
    """A copy of the blob without `fields.actual_bid_at`.

    The blob is client-supplied, so every level is shape-checked rather than
    trusted. Copies instead of mutating - the caller may hold the stored row.
    """
    fields = data.get("fields")
    if isinstance(fields, dict) and "actual_bid_at" in fields:
        data = dict(data)
        fields = dict(fields)
        fields.pop("actual_bid_at")
        data["fields"] = fields
    return data


def _redact_row(row: dict, role: Role) -> dict:
    """Strip the confidential actual bid date from a full row for non-viewers."""
    if role not in ACTUAL_BID_VIEWER_ROLES and isinstance(row.get("data"), dict):
        row = dict(row)
        row["data"] = _strip_actual_bid_at(row["data"])
    return row


def _sanitize_incoming(data: dict, role: Role, stored: dict | None = None) -> dict:
    """The blob a write is allowed to store.

    Editors (ACTUAL_BID_EDITOR_ROLES) store what they sent. Everyone else has
    the incoming `fields.actual_bid_at` dropped, and - on an update - the
    stored value carried forward instead, so a non-editor can neither see,
    clear, nor smuggle a value into the confidential field an executive saved.
    """
    if role in ACTUAL_BID_EDITOR_ROLES:
        return data
    data = _strip_actual_bid_at(data)
    stored_fields = stored.get("fields") if isinstance(stored, dict) else None
    if isinstance(stored_fields, dict) and "actual_bid_at" in stored_fields:
        data = dict(data)
        fields = data.get("fields")
        fields = dict(fields) if isinstance(fields, dict) else {}
        fields["actual_bid_at"] = stored_fields["actual_bid_at"]
        data["fields"] = fields
    return data


def _get_draft(draft_id: UUID) -> dict:
    """The full stored row, or a 404."""
    rows = (
        get_supabase()
        .table("bid_drafts")
        .select("*")
        .eq("id", str(draft_id))
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    return rows[0]


@router.get("")
def list_drafts(user: CurrentUser = Depends(require_writer)):
    """Every draft, most recently touched first, without the form blob."""
    return (
        get_supabase()
        .table("bid_drafts")
        .select(_LIST_SELECT)
        .order("updated_at", desc=True)
        .limit(500)
        .execute()
    ).data or []


@router.post("", status_code=status.HTTP_201_CREATED)
def create_draft(payload: BidDraftIn, user: CurrentUser = Depends(require_writer)):
    row = (
        get_supabase()
        .table("bid_drafts")
        .insert(
            {
                "name": payload.name,
                "number": payload.number,
                "data": _sanitize_incoming(payload.data, user.role),
                "created_by": user.id,
            }
        )
        .execute()
    ).data[0]
    return _redact_row(row, user.role)


@router.get("/{draft_id}")
def get_draft(draft_id: UUID, user: CurrentUser = Depends(require_writer)):
    row = dict(_redact_row(_get_draft(draft_id), user.role))
    row["files"] = _list_draft_files(draft_id)
    return row


@router.put("/{draft_id}")
def update_draft(
    draft_id: UUID,
    payload: BidDraftIn,
    user: CurrentUser = Depends(require_writer),
):
    existing = _get_draft(draft_id)
    row = (
        get_supabase()
        .table("bid_drafts")
        .update(
            {
                "name": payload.name,
                "number": payload.number,
                "data": _sanitize_incoming(
                    payload.data, user.role, stored=existing.get("data")
                ),
            }
        )
        .eq("id", str(draft_id))
        .execute()
    ).data[0]
    return _redact_row(row, user.role)


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_draft(draft_id: UUID, user: CurrentUser = Depends(require_writer)):
    _get_draft(draft_id)
    # The row delete cascades away the bid_draft_files rows (0109). The storage
    # sweep is best-effort and walks the prefix rather than trusting the rows,
    # so an upload racing this delete can't leave an unreclaimed orphan.
    get_supabase().table("bid_drafts").delete().eq("id", str(draft_id)).execute()
    try:
        storage.delete_draft_prefix(str(draft_id))
    except Exception:  # noqa: BLE001
        pass


# ── Draft files (0109) ───────────────────────────────────────────────────────


@router.post(
    "/{draft_id}/files",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_draft_file(
    draft_id: UUID,
    category: str = Form(...),
    doc_type: str | None = Form(None),  # 'drawing' | 'specification'
    addendum_number: str | None = Form(None),
    addendum_issued_on: str | None = Form(None),  # ISO calendar date "YYYY-MM-DD"
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_writer),
):
    """Attach a file to a draft. Same multipart field names, size cap and rate
    limit as the project upload (files.py); addendum metadata optional here."""
    if category not in DRAFT_FILE_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")
    await run_in_threadpool(_get_draft, draft_id)
    number, issued_on, doc_type = _validate_addendum_meta(
        category, addendum_number, addendum_issued_on, doc_type
    )

    content = await _read_capped(file, get_settings().upload_max_bytes)
    # Never trust the client-supplied content-type (same rule as files.py).
    stored_mime = _safe_content_type(file.filename)
    path = storage.build_draft_object_path(str(draft_id), category, file.filename or "upload")
    await run_in_threadpool(storage.upload_file, path, content, stored_mime)

    insert = (
        get_supabase()
        .table("bid_draft_files")
        .insert(
            {
                "draft_id": str(draft_id),
                "category": category,
                "filename": file.filename,
                "storage_path": path,
                "size_bytes": len(content),
                "content_type": stored_mime,
                "addendum_number": number,
                "addendum_issued_on": issued_on,
                "doc_type": doc_type,
            }
        )
    )
    try:
        row = (await run_in_threadpool(insert.execute)).data[0]
    except Exception:
        # The object was PUT before this insert; without its row nothing would
        # ever reclaim it. Best-effort - the original error is the answer.
        try:
            await run_in_threadpool(storage.delete_file, path)
        except Exception:  # noqa: BLE001
            pass
        raise
    return _file_out(row)


class BidDraftFileMetaIn(BaseModel):
    """PATCH body for a draft addendum's metadata. Absent fields are untouched;
    explicit nulls clear (allowed at draft stage - transfer enforces
    completeness)."""

    addendum_number: str | None = None
    addendum_issued_on: str | None = None
    doc_type: str | None = None


@router.patch("/{draft_id}/files/{file_id}")
def update_draft_file(
    draft_id: UUID,
    file_id: UUID,
    body: BidDraftFileMetaIn,
    user: CurrentUser = Depends(require_writer),
):
    rec = _get_draft_file(draft_id, file_id)
    if rec["category"] != ADDENDUM_CATEGORY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, ADDENDUM_META_ONLY_MESSAGE)
    # Route every sent field through the same validator as the upload; a null
    # (or blank) value clears. Only sent fields land in the update.
    number, issued_on, doc_type = _validate_addendum_meta(
        rec["category"], body.addendum_number, body.addendum_issued_on, body.doc_type
    )
    updates: dict = {}
    if "addendum_number" in body.model_fields_set:
        updates["addendum_number"] = number
    if "addendum_issued_on" in body.model_fields_set:
        updates["addendum_issued_on"] = issued_on
    if "doc_type" in body.model_fields_set:
        updates["doc_type"] = doc_type
    if not updates:
        return _file_out(rec)
    rows = (
        get_supabase()
        .table("bid_draft_files")
        .update(updates)
        .eq("id", str(file_id))
        .eq("draft_id", str(draft_id))
        .execute()
    ).data
    if not rows:
        # A transfer or delete raced this edit and removed the row.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return _file_out(rows[0])


@router.delete("/{draft_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_draft_file(
    draft_id: UUID,
    file_id: UUID,
    user: CurrentUser = Depends(require_writer),
):
    rec = _get_draft_file(draft_id, file_id)
    # Row first, conditionally (files.py's race pattern): a transfer racing this
    # delete removes the row, this delete then matches nothing, and the file
    # stays part of its project. Better a possible orphaned storage object than
    # a moved file whose object is gone - so storage cleanup is best-effort.
    deleted = (
        get_supabase()
        .table("bid_draft_files")
        .delete()
        .eq("id", str(file_id))
        .eq("draft_id", str(draft_id))
        .execute()
    ).data or []
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    try:
        storage.delete_file(rec["storage_path"])
    except Exception:  # noqa: BLE001
        pass


class BidDraftTransferIn(BaseModel):
    project_id: UUID


@router.post("/{draft_id}/transfer")
def transfer_draft(
    draft_id: UUID,
    body: BidDraftTransferIn,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_writer),
):
    """Move every draft file onto a project and retire the draft.

    Each object is MOVED in storage (prefix swap: `drafts/{draft_id}/` becomes
    `{project_id}/`, rest of the key preserved) and a project_files row is
    inserted shaped exactly like a fresh intake upload through files.py. The
    whole set is prechecked first: any addendum missing its number or a
    non-future issue date is a 400 with nothing moved. A mid-way failure is a
    502 and the endpoint is retryable - every moved file loses its
    bid_draft_files row immediately, so a retry only processes the remainder
    (a file whose previous attempt died between move and insert is recognized
    by its destination object existing and is not moved or inserted twice).
    """
    _get_draft(draft_id)
    project_id = str(body.project_id)
    projects = (
        get_supabase()
        .table("projects")
        .select("id, abandoned_at")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data
    if not projects:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    if projects[0].get("abandoned_at"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This project is abandoned - reactivate it before transferring draft files",
        )

    files = (
        get_supabase()
        .table("bid_draft_files")
        .select("*")
        .eq("draft_id", str(draft_id))
        .order("created_at")
        .execute()
    ).data or []

    # PRECHECK the whole set before moving anything: at draft stage addendum
    # metadata may be incomplete, at project stage it may not (0076 CHECK).
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    incomplete = []
    for f in files:
        if f["category"] != ADDENDUM_CATEGORY:
            continue
        try:
            issued_on = date.fromisoformat(f.get("addendum_issued_on") or "")
        except ValueError:
            issued_on = None
        if not (f.get("addendum_number") or "").strip() or not issued_on or issued_on > tomorrow:
            incomplete.append(f["filename"] or "(unnamed file)")
    if incomplete:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, TRANSFER_INCOMPLETE_MESSAGE + ", ".join(incomplete)
        )

    prefix = f"drafts/{draft_id}/"
    moved = 0
    for f in files:
        src = f["storage_path"]
        if src.startswith(prefix):
            # The normal case: prefix swap, key preserved - identical to what a
            # fresh upload of this file would have used.
            dest = f"{project_id}/{src[len(prefix):]}"
        else:  # a legacy/foreign path; mint a fresh project key
            dest = storage.build_object_path(project_id, f["category"], f["filename"] or "upload")

        already_moved = False
        try:
            storage.move_object(src, dest)
        except Exception:
            # A retry can find this file already moved (the previous attempt
            # failed after the move): destination present + source gone is that
            # fingerprint. Anything else is a real failure. Keys carry a uuid
            # minted at draft-upload time, so a separately uploaded file - even
            # one with the same name - can never occupy this destination.
            if storage.object_exists(dest) and not storage.object_exists(src):
                already_moved = True
            else:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f'Moving "{f["filename"]}" to the project failed; nothing was '
                    f"lost - try the transfer again ({moved} file(s) already moved).",
                )

        existing = []
        if already_moved:
            # The previous attempt may also have inserted the row before dying.
            existing = (
                get_supabase()
                .table("project_files")
                .select("*")
                .eq("project_id", project_id)
                .eq("storage_path", dest)
                .limit(1)
                .execute()
            ).data or []
        if existing:
            row = existing[0]
        else:
            convertible = office_preview.is_convertible(f["filename"], f["category"])
            insert = (
                get_supabase()
                .table("project_files")
                .insert(
                    {
                        # The exact shape of a fresh intake upload (files.py).
                        "project_id": project_id,
                        "category": f["category"],
                        "storage_path": dest,
                        "filename": f["filename"],
                        "material_category_id": None,
                        "uploaded_by": user.id,
                        "mime_type": f.get("content_type")
                        or _safe_content_type(f["filename"]),
                        "size_bytes": f.get("size_bytes"),
                        "preview_status": "pending" if convertible else "none",
                        "note": None,
                        "doc_type": f.get("doc_type"),
                        "addendum_number": f.get("addendum_number") or None,
                        "addendum_issued_on": (
                            f.get("addendum_issued_on")
                            if f["category"] == ADDENDUM_CATEGORY
                            else None
                        ),
                        "estimator_deliverable": False,
                    }
                )
            )
            try:
                row = insert.execute().data[0]
            except Exception:
                # The object has already moved; the retry recognizes it via the
                # already_moved branch above and only re-inserts the row.
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f'Attaching "{f["filename"]}" to the project failed; nothing '
                    f"was lost - try the transfer again ({moved} file(s) already moved).",
                )
            # The side effects an intake-stage upload performs (files.py): the
            # audit entry and the preview derivative. The drawing-changed
            # notification is deliberately absent - files.py itself suppresses
            # it until intake completes, and a transfer lands on a project at
            # intake.
            audit_payload: dict = {"category": f["category"]}
            if f.get("doc_type"):
                audit_payload["doc_type"] = f["doc_type"]
            if f["category"] == ADDENDUM_CATEGORY:
                audit_payload["addendum_number"] = f.get("addendum_number")
            audit(user.id, "file.upload", "project_file", row["id"], audit_payload)
            if convertible:
                background.add_task(office_preview.generate_preview, row["id"])

        # Retire the draft row the moment its file is safely on the project -
        # this is what makes a retry only process the remainder.
        get_supabase().table("bid_draft_files").delete().eq("id", f["id"]).execute()
        moved += 1

    get_supabase().table("bid_drafts").delete().eq("id", str(draft_id)).execute()
    try:
        storage.delete_draft_prefix(str(draft_id))
    except Exception:  # noqa: BLE001
        pass
    return {"moved": moved}
