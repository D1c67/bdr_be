"""Project Management documents — upload / list / signed-download / delete.

Deliberately a PARALLEL structure to project_files: none of the bidding
machinery (estimator visibility, hand-off locking, submission rounds) applies
here, and the external estimator can never reach these routes (the PM guards
exclude it). Objects share the private `project-files` bucket under
`{project_id}/pm/{category}/…`. Notes are set at upload time only — there is
no note-edit endpoint in v1.
"""

from urllib.parse import quote

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.ratelimit import export_rate_limit, upload_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import PmDocsExportIn
from app.routers.files import (
    FILE_NOTE_MAX_CHARS,
    _export_lock,
    _read_capped,
    _safe_content_type,
)
from app.services import file_export, pm_folders, storage
from app.services.notifications import audit
from app.services.pm import require_pm_project

router = APIRouter(prefix="/pm/projects/{project_id}/documents", tags=["pm-documents"])

# Mirrors the pm_doc_category enum (0058 + 0065 + 0067) — validated here so an
# unknown value is a clean 400 instead of a Postgres enum error. The 0065 additions
# (specification/quote/estimate/billing) let uploads land in the bidding-side
# business folders the unified hub exposes (see app.services.pm_folders); 0067 adds
# 'rfi', which is how the RFI editor's upload-and-attach files reach the hub.
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
    "specification",
    "quote",
    "estimate",
    "billing",
    "rfi",
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


# ── Unified documents hub (PM + bidding + certified payroll) ──────────────────
# The Documents tab is the one place a PM sees *every* document. Reads union the
# three stores via app.services.pm_folders; PM uploads stay the only writable
# store (bidding/CP files are read-only mirrors, folded in with `writable=False`).

# Keys returned to the client omit `storage_path` — the internal layout stays on
# the server; downloads/exports resolve paths through the same visibility filter.
_PUBLIC_ITEM_FIELDS = (
    "key",
    "source",
    "id",
    "folder",
    "category",
    "filename",
    "size_bytes",
    "note",
    "created_at",
    "writable",
    "cp_meta",
)


def _public_item(item: dict) -> dict:
    return {k: item[k] for k in _PUBLIC_ITEM_FIELDS if k in item}


@router.get("/all")
def list_all_documents(
    project_id: str,
    user: CurrentUser = Depends(require_pm_read),
):
    """Every document for the project — PM uploads, bidding files, and
    certified-payroll files — each tagged with its business `folder` and a stable
    `key` the download/export routes accept."""
    require_pm_project(project_id)
    items = pm_folders.list_project_documents(project_id)
    return [_public_item(i) for i in items]


@router.get("/file")
def document_signed_url(
    project_id: str,
    key: str,
    user: CurrentUser = Depends(require_pm_read),
):
    """Attachment signed URL for any hub document by `key` ("source:id").

    Resolution goes through `list_project_documents` (not a direct per-source
    lookup) so the hub's visibility rules — unsent estimator drafts excluded, CP
    files only where tagged to this project — also gate downloads. An unknown or
    out-of-scope key is a clean 404, never a cross-project/draft leak.
    """
    require_pm_project(project_id)
    item = next(
        (i for i in pm_folders.list_project_documents(project_id) if i["key"] == key),
        None,
    )
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    # Attachment disposition: a top-level open downloads rather than renders it
    # inline (defuses a stored HTML/SVG masquerade).
    url = storage.signed_url(item["storage_path"], download=item["filename"] or True)
    audit(user.id, "pm_doc.download", "project", project_id, {"key": key})
    return {"url": url, "filename": item["filename"]}


@router.post("/export", dependencies=[Depends(export_rate_limit)])
async def export_documents(
    project_id: str,
    body: PmDocsExportIn | None = Body(default=None),
    user: CurrentUser = Depends(require_pm_read),
):
    """Bundle the project's documents into a single `.zip`, one folder per
    business folder (Plans/, Quotes/, Certified Payroll/, …).

    No body (or `{}`) exports every document the caller may read; pass
    `{"keys": [...]}` for a subset. A foreign/out-of-scope key simply doesn't
    match the allowed set — no leak. The whole body is guarded so any failure
    surfaces as an HTTPException (keeping CORS headers) rather than a raw 500.
    """
    try:
        await run_in_threadpool(require_pm_project, project_id)
        items = await run_in_threadpool(pm_folders.list_project_documents, project_id)
        if body and body.keys is not None:
            wanted = set(body.keys)
            items = [i for i in items if i["key"] in wanted]
        if not items:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No documents to export")

        # size_bytes is nullable — coalesce so a legacy NULL can't defeat the guard.
        total = sum((i.get("size_bytes") or 0) for i in items)
        max_bytes = get_settings().export_max_total_bytes
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"Too large to export at once (~{total // (1024 * 1024)} MB); "
                "select fewer documents.",
            )

        # Shape rows for the folder-aware ZIP builder and shed storage-internal
        # fields the builder doesn't need.
        rows = [
            {
                "folder": pm_folders.FOLDER_LABELS.get(i["folder"], "Other"),
                "folder_rank": pm_folders.folder_rank(i["folder"]),
                "filename": i.get("filename"),
                "storage_path": i["storage_path"],
            }
            for i in items
        ]

        # One archive builds per process at a time (shared with the bidding
        # export) — concurrent builds are the OOM vector. Retriable 429 if busy.
        if not _export_lock.acquire(blocking=False):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=ErrorCode.RATE_LIMITED,
                headers={"Retry-After": "10", "X-RateLimit-Scope": RateLimitScope.FILE_EXPORT},
            )
        try:
            spool, manifest, size = await run_in_threadpool(
                file_export.build_folder_export_spooled, rows
            )
        finally:
            _export_lock.release()

        ok_count = sum(1 for m in manifest if m["status"] == "ok")
        if ok_count == 0:
            spool.close()
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No documents to export")

        proj_q = (
            get_supabase().table("projects").select("number, name").eq("id", project_id).single()
        )
        proj = (await run_in_threadpool(proj_q.execute)).data or {}
        await run_in_threadpool(
            audit,
            user.id,
            "pm_doc.export",
            "project",
            project_id,
            {"count": ok_count, "subset": bool(body and body.keys is not None)},
        )

        filename = file_export.export_filename(proj, "documents")

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
