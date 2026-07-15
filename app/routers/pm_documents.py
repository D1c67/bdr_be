"""Project Management documents — upload / list / signed-download / delete.

Deliberately a PARALLEL structure to project_files: none of the bidding
machinery (estimator visibility, hand-off locking, submission rounds) applies
here, and the external estimator can never reach these routes (the PM guards
exclude it). Objects share the private `project-files` bucket under
`{project_id}/pm/{category}/…`. Notes are set at upload time only — there is
no note-edit endpoint in v1.
"""

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.ratelimit import upload_rate_limit
from app.core.supabase_client import get_supabase
from app.routers.files import FILE_NOTE_MAX_CHARS, _read_capped, _safe_content_type
from app.services import storage
from app.services.notifications import audit
from app.services.pm import require_pm_project

router = APIRouter(prefix="/pm/projects/{project_id}/documents", tags=["pm-documents"])

# Mirrors the pm_doc_category enum (0058) — validated here so an unknown value
# is a clean 400 instead of a Postgres enum error.
PM_DOC_CATEGORIES = (
    "contract",
    "change_order",
    "submittal",
    "permit",
    "as_built",
    "drawing",
    "schedule",
    "correspondence",
    "photo",
    "closeout",
    "other",
)


def _get_doc(project_id: str, doc_id: str) -> dict:
    # .limit(1), not .single(): a foreign/unknown id must be a clean 404.
    rows = (
        get_supabase()
        .table("pm_documents")
        .select("*")
        .eq("id", doc_id)
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return rows[0]


@router.get("")
def list_documents(
    project_id: str,
    category: str | None = None,
    user: CurrentUser = Depends(require_pm_read),
):
    require_pm_project(project_id)
    if category is not None and category not in PM_DOC_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")
    q = get_supabase().table("pm_documents").select("*").eq("project_id", project_id)
    if category is not None:
        q = q.eq("category", category)
    return q.order("created_at", desc=True).execute().data or []


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_document(
    project_id: str,
    category: str = Form(...),
    note: str | None = Form(None),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_pm_write),
):
    await run_in_threadpool(require_pm_project, project_id)
    if category not in PM_DOC_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")
    note = (note or "").strip() or None
    if note and len(note) > FILE_NOTE_MAX_CHARS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Note is too long")

    content = await _read_capped(file, get_settings().upload_max_bytes)
    stored_mime = _safe_content_type(file.filename)
    path = storage.build_object_path(project_id, f"pm/{category}", file.filename or "upload")
    await run_in_threadpool(storage.upload_file, path, content, stored_mime)

    insert = (
        get_supabase()
        .table("pm_documents")
        .insert(
            {
                "project_id": project_id,
                "category": category,
                "storage_path": path,
                "filename": file.filename or "upload",
                "mime_type": stored_mime,
                "size_bytes": len(content),
                "note": note,
                "uploaded_by": user.id,
            }
        )
    )
    try:
        row = (await run_in_threadpool(insert.execute)).data[0]
    except Exception:
        # The object already landed in storage — best-effort removal so a
        # failed insert doesn't strand an orphan; the original error surfaces.
        try:
            await run_in_threadpool(storage.delete_file, path)
        except Exception:  # noqa: BLE001
            pass
        raise
    await run_in_threadpool(
        audit,
        user.id,
        "pm_doc.upload",
        "project",
        project_id,
        {"category": category, "filename": file.filename},
    )
    return row


@router.get("/{doc_id}/download")
def download_document(
    project_id: str,
    doc_id: str,
    user: CurrentUser = Depends(require_pm_read),
):
    require_pm_project(project_id)
    rec = _get_doc(project_id, doc_id)
    # Attachment disposition: a top-level open downloads the file rather than
    # rendering it inline (defuses a stored HTML/SVG masquerade).
    url = storage.signed_url(rec["storage_path"], download=rec["filename"] or True)
    audit(user.id, "pm_doc.download", "project", project_id, {"doc_id": doc_id})
    return {"url": url, "filename": rec["filename"]}


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    project_id: str,
    doc_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    """Always allowed for writers — PM documents carry none of the bidding
    immutability rules by design."""
    require_pm_project(project_id)
    rec = _get_doc(project_id, doc_id)
    # Storage first, best-effort: an already-missing object must never block
    # removing the row.
    try:
        storage.delete_file(rec["storage_path"])
    except Exception:  # noqa: BLE001
        pass
    get_supabase().table("pm_documents").delete().eq("id", doc_id).execute()
    audit(
        user.id,
        "pm_doc.delete",
        "project",
        project_id,
        {"doc_id": doc_id, "category": rec["category"]},
    )
