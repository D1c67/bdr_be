"""BOQ → RFQ extraction (replaces manual RFQ creation).

The PE kicks off an analysis of the estimator's BOQ; Claude separates the
materials by category (returning JSON) as a background job. The PE polls for the
result, then reviews / refines / edits it, and on confirm we create one RFQ per
material category (merging sites), persist the line items, and generate a
per-category RFQ Excel that becomes the RFQ's split file.

Gated to PE / IT Admin, like the rest of the RFQ flow.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_role
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.models.schemas import BoqAnalysisStart, BoqConfirmIn, BoqRefineIn, BoqResultIn
from app.services import boq_extraction, office_preview, rfq_excel, storage
from app.services.notifications import audit

router = APIRouter(prefix="/projects/{project_id}/boq-analysis", tags=["boq-analysis"])
_PE = require_role(Role.PE, Role.IT_ADMIN)

_XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("", status_code=status.HTTP_201_CREATED)
async def start_analysis(
    project_id: str,
    body: BoqAnalysisStart,
    background: BackgroundTasks,
    user: CurrentUser = Depends(_PE),
):
    sb = get_supabase()
    boq_file_id = body.boq_file_id
    if not boq_file_id:
        latest = (
            sb.table("project_files")
            .select("id")
            .eq("project_id", project_id)
            .eq("category", "boq")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        ).data
        if not latest:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "No BOQ file uploaded for this project"
            )
        boq_file_id = latest[0]["id"]

    row = (
        sb.table("boq_analyses")
        .insert(
            {
                "project_id": project_id,
                "boq_file_id": boq_file_id,
                "status": "pending",
                "created_by": user.id,
            }
        )
        .execute()
    ).data[0]
    background.add_task(boq_extraction.run_extraction, row["id"])
    audit(user.id, "boq.analyze", "boq_analysis", row["id"], {"boq_file_id": boq_file_id})
    return row


@router.get("/latest")
async def latest_analysis(project_id: str, user: CurrentUser = Depends(_PE)):
    rows = (
        get_supabase()
        .table("boq_analyses")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data
    return rows[0] if rows else None


@router.post("/{analysis_id}/refine")
async def refine_analysis(
    project_id: str,
    analysis_id: str,
    body: BoqRefineIn,
    background: BackgroundTasks,
    user: CurrentUser = Depends(_PE),
):
    sb = get_supabase()
    sb.table("boq_analyses").update({"status": "running", "error": None}).eq(
        "id", analysis_id
    ).eq("project_id", project_id).execute()
    background.add_task(boq_extraction.refine_extraction, analysis_id, body.message)
    audit(user.id, "boq.refine", "boq_analysis", analysis_id, {"message": body.message})
    return {"status": "running"}


@router.put("/{analysis_id}/result")
async def save_result(
    project_id: str,
    analysis_id: str,
    body: BoqResultIn,
    user: CurrentUser = Depends(_PE),
):
    """Persist the PE's directly-edited extraction JSON."""
    updated = (
        get_supabase()
        .table("boq_analyses")
        .update({"result_json": body.result_json, "status": "done"})
        .eq("id", analysis_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")
    audit(user.id, "boq.edit", "boq_analysis", analysis_id, None)
    return updated[0]


@router.post("/{analysis_id}/confirm")
async def confirm_analysis(
    project_id: str,
    analysis_id: str,
    body: BoqConfirmIn,
    background: BackgroundTasks,
    user: CurrentUser = Depends(_PE),
):
    """Turn the confirmed groups into RFQs + line items + generated RFQ files."""
    if not body.groups:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No category groups to confirm")
    sb = get_supabase()

    # Resolve category names up front for the generated workbook titles.
    cat_ids = [g.material_category_id for g in body.groups]
    cats = (
        sb.table("material_categories").select("id, name").in_("id", cat_ids).execute()
    ).data or []
    names = {c["id"]: c["name"] for c in cats}

    created = []
    for group in body.groups:
        category_id = group.material_category_id
        if category_id not in names:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Unknown material category: {category_id}"
            )

        # One RFQ per (project, category) — reuse the existing row if present.
        existing = (
            sb.table("rfqs")
            .select("id")
            .eq("project_id", project_id)
            .eq("material_category_id", category_id)
            .execute()
        ).data
        if existing:
            rfq_id = existing[0]["id"]
        else:
            rfq_id = (
                sb.table("rfqs")
                .insert(
                    {
                        "project_id": project_id,
                        "material_category_id": category_id,
                        "created_by": user.id,
                    }
                )
                .execute()
            ).data[0]["id"]

        # Replace the line items behind this RFQ with the confirmed set.
        sb.table("rfq_line_items").delete().eq("rfq_id", rfq_id).execute()
        items = [it.model_dump(mode="json") for it in group.items]
        if items:
            sb.table("rfq_line_items").insert(
                [{**it, "rfq_id": rfq_id, "sort_order": i} for i, it in enumerate(items)]
            ).execute()

        # Generate the per-category RFQ Excel and attach it as the split file.
        name = names[category_id]
        xlsx = rfq_excel.build_rfq_workbook(name, items)
        filename = f"{name.replace('/', '_')}_RFQ.xlsx"
        path = storage.build_object_path(project_id, "rfq_split", filename)
        storage.upload_file(path, xlsx, _XLSX_TYPE)
        convertible = office_preview.is_convertible(filename, "rfq_split")
        file_row = (
            sb.table("project_files")
            .insert(
                {
                    "project_id": project_id,
                    "category": "rfq_split",
                    "storage_path": path,
                    "filename": filename,
                    "material_category_id": category_id,
                    "uploaded_by": user.id,
                    "mime_type": _XLSX_TYPE,
                    "size_bytes": len(xlsx),
                    "preview_status": "pending" if convertible else "none",
                }
            )
            .execute()
        ).data[0]
        if convertible:
            background.add_task(office_preview.generate_preview, file_row["id"])
        sb.table("rfqs").update({"split_file_id": file_row["id"]}).eq("id", rfq_id).execute()
        created.append({"rfq_id": rfq_id, "material_category_id": category_id, "file_id": file_row["id"]})

    audit(user.id, "boq.confirm", "boq_analysis", analysis_id, {"rfqs": len(created)})
    return {"created": created}
