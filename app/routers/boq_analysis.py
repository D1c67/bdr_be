"""BOQ → RFQ extraction (replaces manual RFQ creation).

The Estimating Engineer kicks off an analysis of the estimator's BOQ; Claude
separates the materials by category (returning JSON) as a background job. The
engineer polls for the result, then reviews and corrects it directly (inline
qty/unit edits, category moves, removals — autosaved as a server-side draft),
and on confirm we create one RFQ per material category (merging sites), persist
the line items, and generate a per-category RFQ Excel that becomes the RFQ's
split file. The confirm also captures a training example (model output vs the
user's corrected output) for the dev Training page.

Open to any writer role, like the rest of the RFQ flow.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_writer
from app.core.ratelimit import ai_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import BoqAnalysisStart, BoqConfirmIn, BoqDraftIn
from app.services import (
    boq_extraction,
    boq_training,
    estimator_rounds,
    office_preview,
    rfq_excel,
    storage,
    workflow,
)
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/boq-analysis", tags=["boq-analysis"])
_PE = require_writer

_XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# A pending/running analysis older than this is treated as abandoned (a crashed
# job) so it can't block new runs forever.
_JOB_STALE_MINUTES = 15


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


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
def start_analysis(
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
def latest_analysis(project_id: str, user: CurrentUser = Depends(_PE)):
    # Everything except input_snapshot: the panel polls this every 3s during a
    # run, and the snapshot (up to ~400KB of rendered BOQ text) is only read by
    # the training detail route.
    rows = (
        get_supabase()
        .table("boq_analyses")
        .select(
            "id, project_id, boq_file_id, status, model, result_json, error, "
            "draft_json, draft_updated_by, draft_updated_at, "
            "created_by, created_at, updated_at"
        )
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data
    return rows[0] if rows else None


@router.patch("/{analysis_id}/draft")
def save_draft(
    project_id: str,
    analysis_id: str,
    body: BoqDraftIn,
    user: CurrentUser = Depends(_PE),
):
    """Autosave the reviewer's correction draft (inline edits / moves / removals).

    Last-write-wins, no locking — the panel debounces saves and one reviewer
    realistically works an analysis at a time. Deliberately not audited: a
    keystroke-debounced autosave would flood the activity log.
    """
    sb = get_supabase()
    rows = (
        sb.table("boq_analyses")
        .select("id, status")
        .eq("id", analysis_id)
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")
    if rows[0].get("status") != "done":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a completed analysis can hold a draft."
        )
    draft_json = body.draft.model_dump(mode="json") if body.draft else None
    now = datetime.now(timezone.utc).isoformat()
    sb.table("boq_analyses").update(
        {"draft_json": draft_json, "draft_updated_by": user.id, "draft_updated_at": now}
    ).eq("id", analysis_id).execute()
    return {"draft_json": draft_json, "draft_updated_at": now}


@router.post("/{analysis_id}/confirm")
def confirm_analysis(
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

    # Resolve category names up front for the generated workbook titles — and
    # for the training diff, which also needs names for mapped-but-empty groups.
    # Mapping ids only feed the diff, so a malformed one (a stale client passing
    # the panel's "" Hold sentinel through) must not abort the confirm: filter
    # instead of letting PostgREST reject the uuid cast.
    cat_ids = {g.material_category_id for g in body.groups}
    cat_ids.update(m.material_category_id for m in body.group_mappings if _is_uuid(m.material_category_id))
    cats = (
        sb.table("material_categories").select("id, name").in_("id", sorted(cat_ids)).execute()
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

        # Replace the line items behind this RFQ with the confirmed set. `src`
        # is diff bookkeeping, not an rfq_line_items column — keep it out.
        sb.table("rfq_line_items").delete().eq("rfq_id", rfq_id).execute()
        items = [it.model_dump(mode="json", exclude={"src"}) for it in group.items]
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

    # Training capture — record (model output vs user-corrected output) for the
    # dev Training page. Best-effort: a capture bug must never fail the confirm.
    try:
        analysis = (
            sb.table("boq_analyses")
            .select("*")
            .eq("id", analysis_id)
            .eq("project_id", project_id)
            .limit(1)
            .execute()
        ).data or []
        if analysis:
            boq_training.capture_example(analysis[0], project_id, body, user.id, names)
    except Exception:
        logger.exception("BOQ training capture failed (analysis %s)", analysis_id)
    return {"created": created}
