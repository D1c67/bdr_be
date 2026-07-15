"""Project Management — core router: the PM list, direct create, the per-project
overview, PM details edits, and the PM stage lifecycle (advance / back / complete).

PM shares the projects spine with bidding but is a separate lifecycle (see
services/pm_workflow.py). Reads are any PM-read role (accountant included, the
external estimator NEVER — require_pm_read rejects it); writes are PM-write
roles. Module routers (financials / field / documents) live in their own files
under the same /pm/projects/{id} prefix.
"""

from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    PMDetailsUpdate,
    PMProjectCreate,
    PMStageTransitionIn,
)
from app.routers.projects import _NUMBER_TAKEN, _is_duplicate_number, redact_for_role
from app.services import email_ingest, pm, pm_workflow
from app.services.notifications import audit
from app.services.project_status import derive_status

router = APIRouter(prefix="/pm", tags=["pm"])

_PM_SELECT = "*, bid_outcomes(result), pm_details(*)"


def _present_pm(project: dict, role: Role) -> dict:
    """Attach derived status + flatten the pm_details embed, then redact.
    The PM mirror of routers/projects._present — PM rows still carry bid facts
    (status 'won', confidential actual_bid_at) that need the same treatment."""
    outcome = project.pop("bid_outcomes", None)
    if isinstance(outcome, list):
        result = outcome[0].get("result") if outcome else None
    elif isinstance(outcome, dict):
        result = outcome.get("result")
    else:
        result = None
    details = project.pop("pm_details", None)
    if isinstance(details, list):
        details = details[0] if details else None
    project["status"] = derive_status(
        project.get("current_stage"), project.get("abandoned_at"), result
    )
    project["pm_details"] = details
    return redact_for_role(project, role)


def _fetch_pm_project(project_id: str) -> dict:
    rows = (
        get_supabase()
        .table("projects")
        .select(_PM_SELECT)
        .eq("id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    project = rows[0]
    if project.get("pm_stage") is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project is not in Project Management")
    return project


@router.get("/projects")
def list_pm_projects(
    pm_stage: str | None = None,
    include_completed: bool = False,
    user: CurrentUser = Depends(require_pm_read),
):
    """The PM dashboard list: every project with a PM life. Completed projects
    are hidden unless asked for (they remain fully retained)."""
    if pm_stage is not None and pm_stage not in pm_workflow.PM_STAGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown PM stage: {pm_stage}")
    query = (
        get_supabase()
        .table("projects")
        .select(_PM_SELECT)
        .not_.is_("pm_stage", "null")
    )
    if pm_stage is not None:
        query = query.eq("pm_stage", pm_stage)
    if not include_completed:
        query = query.is_("pm_completed_at", "null")
    resp = query.order("created_at", desc=True).execute()
    return [_present_pm(p, user.role) for p in resp.data or []]


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_pm_project(
    body: PMProjectCreate,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_pm_write),
):
    """Direct creation in PM — no bid. Live jobs onboard at any stage via
    initial_stage. Project numbers share the bidding uniqueness rule (0052)."""
    try:
        created = pm.create_direct_project(body, user.id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — unique violation → number re-use
        if _is_duplicate_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _NUMBER_TAKEN) from exc
        raise
    # Learn-back: re-scan Unknown emails against the new project (see
    # email_ingest.rescan_unknown_for_project). Best-effort, never raises.
    background.add_task(email_ingest.rescan_unknown_for_project, created["id"])
    return _present_pm(_fetch_pm_project(created["id"]), user.role)


@router.get("/projects/{project_id}")
def get_pm_project(project_id: str, user: CurrentUser = Depends(require_pm_read)):
    """The PM overview: project + pm_details + a contract-value headline."""
    project = _present_pm(_fetch_pm_project(project_id), user.role)
    details = project.get("pm_details") or {}
    original = details.get("original_contract_value")
    approved = pm.approved_change_total(project_id)
    current = None
    if original is not None:
        current = str(Decimal(str(original)) + approved)
    project["financials_headline"] = {
        "original_contract_value": str(original) if original is not None else None,
        "approved_change_total": str(approved),
        "current_contract_value": current,
    }
    return project


@router.patch("/projects/{project_id}")
def update_pm_details(
    project_id: str,
    body: PMDetailsUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    """Edit the PM detail record. exclude_unset: an explicit null clears a field.
    Name/number/address edits go through the shared PATCH /projects/{id}."""
    _fetch_pm_project(project_id)  # 404 unless the project is in PM
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if patch.get("customer_gc_id"):
        gc = (
            get_supabase()
            .table("general_contractors")
            .select("id")
            .eq("id", patch["customer_gc_id"])
            .execute()
        ).data
        if not gc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer GC not found")
    updated = (
        get_supabase()
        .table("pm_details")
        .update(patch)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PM details not found")
    audit(user.id, "pm.details_update", "project", project_id, patch)
    return _present_pm(_fetch_pm_project(project_id), user.role)


@router.post("/projects/{project_id}/stage")
def change_pm_stage(
    project_id: str,
    body: PMStageTransitionIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Advance (adjacent-only) or move back (any earlier stage, note required)."""
    pm_workflow.transition_pm_project(project_id, body.to_stage, user.id, body.note)
    return _present_pm(_fetch_pm_project(project_id), user.role)


@router.post("/projects/{project_id}/complete")
def complete_pm_project(
    project_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    """Mark a Closeout project complete — the PM end state. The project stays
    fully retained; PM lists hide it by default."""
    pm_workflow.complete_pm_project(project_id, user.id)
    return _present_pm(_fetch_pm_project(project_id), user.role)


@router.get("/projects/{project_id}/stage-events")
def list_pm_stage_events(
    project_id: str, user: CurrentUser = Depends(require_pm_read)
):
    _fetch_pm_project(project_id)
    rows = (
        get_supabase()
        .table("pm_stage_events")
        .select("*")
        .eq("project_id", project_id)
        .order("entered_at")
        .execute()
    ).data or []
    return rows
