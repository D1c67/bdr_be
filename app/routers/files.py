"""Project file upload / list / signed-download.

Storage objects live in the private `project-files` bucket; downloads are served
as short-TTL signed URLs. The estimator is restricted: they may only read
`drawing` files and only write `estimate`/`boq`/`markup`, and only for projects
they are actively assigned to.
"""

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
from fastapi.responses import JSONResponse

from app.core.deps import (
    CurrentUser,
    require_project_assignment,
)
from app.core.ratelimit import estimator_rate_limit
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import office_preview, storage
from app.services.notifications import audit, notify_role, notify_user

# Rate limit estimator file traffic (no-op for internal roles).
router = APIRouter(
    prefix="/projects/{project_id}/files",
    tags=["files"],
    dependencies=[Depends(estimator_rate_limit)],
)

# What the estimator is allowed to touch. 'specification' is intentionally NOT
# in ESTIMATOR_READ — specs stay internal; only drawings reach the estimator.
ESTIMATOR_READ = {"drawing"}
ESTIMATOR_WRITE = {"estimate", "boq", "markup"}
VALID_CATEGORIES = {
    "drawing",
    "specification",
    "estimate",
    "boq",
    "markup",
    "rfq_split",
    "quote",
    "other",
}


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
    if user.role == Role.ESTIMATOR and rec["category"] not in (ESTIMATOR_READ | ESTIMATOR_WRITE):
        audit(user.id, "access.denied", "project_file", file_id, {"category": rec["category"]})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return rec


@router.get("")
async def list_files(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    q = get_supabase().table("project_files").select("*").eq("project_id", project_id)
    if user.role == Role.ESTIMATOR:
        q = q.in_("category", list(ESTIMATOR_READ | ESTIMATOR_WRITE))
    return q.order("created_at", desc=True).execute().data or []


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_file(
    project_id: str,
    background: BackgroundTasks,
    category: str = Form(...),
    material_category_id: str | None = Form(None),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_project_assignment),
):
    if category not in VALID_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")
    if user.role == Role.ESTIMATOR and category not in ESTIMATOR_WRITE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators may only upload estimate/boq/markup")
    if user.role not in INTERNAL_ROLES and user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")

    content = await file.read()
    path = storage.build_object_path(project_id, category, file.filename or "upload")
    storage.upload_file(path, content, file.content_type or "application/octet-stream")

    convertible = office_preview.is_convertible(file.filename, category)
    row = (
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
                "mime_type": file.content_type,
                "size_bytes": len(content),
                "preview_status": "pending" if convertible else "none",
            }
        )
        .execute()
    ).data[0]
    audit(user.id, "file.upload", "project_file", row["id"], {"category": category})
    if convertible:
        # Sync task → runs in the threadpool after the response; never blocks.
        background.add_task(office_preview.generate_preview, row["id"])

    # Adding a drawing after intake means whoever prices off the drawings should
    # re-check their work. (Multiple drawings per project are legitimate.)
    if category == "drawing":
        _notify_drawing_changed(project_id, user, "added")

    return row


def _notify_drawing_changed(project_id: str, user: CurrentUser, verb: str) -> None:
    """Alert PE/PM/assigned-estimator that a project's drawings changed, post-intake.

    `verb` is "added" or "removed". During intake nothing is sent — the PA is still
    assembling the package and no one is pricing off it yet.
    """
    proj = (
        get_supabase()
        .table("projects")
        .select("current_stage, name, number")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj or proj["current_stage"] == "intake":
        return

    label = f"{proj.get('number') or ''} {proj.get('name') or ''}".strip() or "a project"
    msg = f"Electrical drawing {verb} for {label} — re-check anything priced off it."
    notify_role(Role.PM, project_id, "drawing_changed", msg)
    notify_role(Role.PE, project_id, "drawing_changed", msg)

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


@router.get("/{file_id}/download")
async def download_url(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    rec = _get_file_checked(project_id, file_id, user)
    url = storage.signed_url(rec["storage_path"])
    audit(user.id, "file.download", "project_file", file_id, None)
    return {"url": url, "filename": rec["filename"]}


@router.get("/{file_id}/preview-url")
async def preview_url(
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
async def preview_file(
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
async def delete_file(
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
    elif user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")

    # Sent proposals are evidence of what we bid — immutable; and only the
    # send-out owners may delete even an unsent generated proposal.
    if rec["category"] == "proposal":
        if user.role not in (Role.PA, Role.PM, Role.IT_ADMIN):
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

    storage.delete_file(rec["storage_path"])
    # The derivative path is deterministic — delete it unconditionally (not just
    # when preview_path is set) so a conversion racing this delete can't leave
    # an orphan behind. Best-effort: an orphan must never block the delete.
    derivative_paths = {
        rec.get("preview_path"),
        office_preview.preview_object_path(project_id, file_id),
    }
    for path in filter(None, derivative_paths):
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001
            pass
    get_supabase().table("project_files").delete().eq("id", file_id).execute()
    audit(user.id, "file.delete", "project_file", file_id, {"category": rec["category"]})

    # Removing a drawing after intake is a change downstream pricers must know about.
    if rec["category"] == "drawing":
        _notify_drawing_changed(project_id, user, "removed")
