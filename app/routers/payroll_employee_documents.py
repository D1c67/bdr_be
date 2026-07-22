"""Certified Payroll — employee document vault (W-4 / I-9 / certs / licenses).

A faithful adaptation of routers/pm_documents.py: metadata rows in
employee_documents, bytes in the private `project-files` bucket under
payroll/employees/{employee_id}/…, short-TTL signed-URL downloads. This
replaces the legacy CPR app's BYTEA-in-DB storage.
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
from app.core.deps import CurrentUser, require_cp_read, require_cp_write
from app.core.ratelimit import upload_rate_limit
from app.core.supabase_client import get_supabase

# Shared upload hygiene — capped body reads and extension-derived content
# types (never trusted from the client); see routers/files.py for rationale.
from app.routers.files import _read_capped, _safe_content_type
from app.services import storage
from app.services.notifications import audit

router = APIRouter(prefix="/payroll/employees/{employee_id}/documents", tags=["payroll"])

# Mirrors the cp_doc_category enum (0063) — validated here so an unknown value
# is a clean 400 instead of a Postgres enum error.
CP_DOC_CATEGORIES = ("w4", "i9", "certification", "license", "other")


def _require_employee(employee_id: str) -> None:
    rows = (
        get_supabase()
        .table("employees")
        .select("id")
        .eq("id", employee_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")


def _get_doc(employee_id: str, doc_id: str) -> dict:
    # .limit(1), not .single(): a foreign/unknown id must be a clean 404.
    rows = (
        get_supabase()
        .table("employee_documents")
        .select("*")
        .eq("id", doc_id)
        .eq("employee_id", employee_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return rows[0]


@router.get("")
def list_documents(
    employee_id: str,
    user: CurrentUser = Depends(require_cp_read),
):
    _require_employee(employee_id)
    return (
        get_supabase()
        .table("employee_documents")
        .select("*")
        .eq("employee_id", employee_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_document(
    employee_id: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_cp_write),
):
    await run_in_threadpool(_require_employee, employee_id)
    if category not in CP_DOC_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")

    content = await _read_capped(file, get_settings().upload_max_bytes)
    stored_mime = _safe_content_type(file.filename)
    # Unlike PM documents, opaque/unknown types are rejected outright — HR
    # documents are a small set of well-known formats.
    if stored_mime == "application/octet-stream":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported file type")
    path = storage.build_object_path(
        "payroll", f"employees/{employee_id}", file.filename or "upload"
    )
    await run_in_threadpool(storage.upload_file, path, content, stored_mime)

    insert = (
        get_supabase()
        .table("employee_documents")
        .insert(
            {
                "employee_id": employee_id,
                "category": category,
                "storage_path": path,
                "filename": file.filename or "upload",
                "mime_type": stored_mime,
                "size_bytes": len(content),
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
        "cp.doc_upload",
        "employee",
        employee_id,
        {"category": category, "filename": file.filename},
    )
    return row


@router.get("/{doc_id}/download")
def download_document(
    employee_id: str,
    doc_id: str,
    user: CurrentUser = Depends(require_cp_read),
):
    _require_employee(employee_id)
    rec = _get_doc(employee_id, doc_id)
    # Attachment disposition: a top-level open downloads the file rather than
    # rendering it inline (defuses a stored HTML/SVG masquerade).
    url = storage.signed_url(rec["storage_path"], download=rec["filename"] or True)
    audit(user.id, "cp.doc_download", "employee", employee_id, {"doc_id": doc_id})
    return {"url": url, "filename": rec["filename"]}


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    employee_id: str,
    doc_id: str,
    user: CurrentUser = Depends(require_cp_write),
):
    _require_employee(employee_id)
    rec = _get_doc(employee_id, doc_id)
    # Storage first, best-effort: an already-missing object must never block
    # removing the row.
    try:
        storage.delete_file(rec["storage_path"])
    except Exception:  # noqa: BLE001
        pass
    get_supabase().table("employee_documents").delete().eq("id", doc_id).execute()
    audit(
        user.id,
        "cp.doc_delete",
        "employee",
        employee_id,
        {"doc_id": doc_id, "category": rec["category"]},
    )
