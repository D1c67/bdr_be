"""BOQ → RFQ extraction (replaces manual RFQ creation).

The Estimating Engineer kicks off an analysis of the estimator's BOQ; Claude
separates the materials by category (returning JSON) as a background job. The
engineer polls for the result, then reviews / refines / edits it, and on confirm
we create one RFQ per material category (merging sites), persist the line items,
and generate a per-category RFQ Excel that becomes the RFQ's split file.

Open to any writer role, like the rest of the RFQ flow.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_writer
from app.core.ratelimit import ai_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import BoqAnalysisStart, BoqConfirmIn, BoqRefineIn
from app.services import (
    boq_extraction,
    estimator_rounds,
    office_preview,
    rfq_excel,
    storage,
    workflow,
)
from app.services.notifications import audit

router = APIRouter(prefix="/projects/{project_id}/boq-analysis", tags=["boq-analysis"])
_PE = require_writer

_XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# A pending/running analysis older than this is treated as abandoned (a crashed
# job) so it can't block new runs forever.
_JOB_STALE_MINUTES = 15


def _active_job_exists(sb, project_id: str) -> bool:
    rows = (
        sb.table("boq_analyses")
        .select("id, status, created_at")
        .eq("project_id", project_id)
        .in_("status", ["pending", "running"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return False
    stamp_raw = rows[0].get("created_at") or ""
    try:
        stamp = datetime.fromisoformat(str(stamp_raw).replace("Z", "+00:00"))
    except ValueError:
        return True  # unparseable stamp on an in-flight row → treat as active
    return datetime.now(timezone.utc) - stamp < timedelta(minutes=_JOB_STALE_MINUTES)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(ai_rate_limit)])
async def start_analysis(
    project_id: str,
    body: BoqAnalysisStart,
    background: BackgroundTasks,
    user: CurrentUser = Depends(_PE),
):
    sb = get_supabase()
    # One paid extraction at a time per project — a second concurrent kickoff is
    # almost always a double-click or a script, and each fires a paid Opus call.
    if _active_job_exists(sb, project_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A BOQ analysis is already in progress for this project.",
        )
    # Explicit or latest, the chosen BOQ must be one the estimator actually
    # sent (or an internal upload) — never an unsent draft of the open round.
    boq_file_id = estimator_rounds.resolve_boq_file_id(project_id, body.boq_file_id)

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


@router.post("/{analysis_id}/refine", dependencies=[Depends(ai_rate_limit)])
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
    # Re-confirming reshapes the RFQ categories/line items that feed pricing, so
    # re-verify if the project already passed Verify.
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "BOQ re-confirmed — RFQ categories changed")
    return {"created": created}
