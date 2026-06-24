"""Workflow transitions and stage-event history.

The generic `/advance` endpoint moves a project forward one legal step and is
guarded so only a role that OWNS the current stage (or IT Admin) may advance it.
Go/No-Go entry/exit is handled by the dedicated voting router, so this endpoint
refuses to leave the `go_no_go` stage.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import TransitionIn
from app.services import workflow
from app.services.notifications import notify_role

router = APIRouter(prefix="/projects/{project_id}", tags=["workflow"])


@router.get("/stage-events")
async def stage_events(project_id: str, user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return (
        get_supabase()
        .table("stage_events")
        .select("*")
        .eq("project_id", project_id)
        .order("entered_at")
        .execute()
    ).data or []


@router.post("/advance")
async def advance(
    project_id: str,
    body: TransitionIn,
    user: CurrentUser = Depends(get_current_user),
):
    proj = (
        get_supabase().table("projects").select("current_stage").eq("id", project_id).single().execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    current = proj["current_stage"]

    if current == "go_no_go":
        raise HTTPException(status.HTTP_409_CONFLICT, "Use the Go/No-Go vote/override endpoints")

    # To Estimator can't be left until an electrical drawing exists — a hard rule
    # mirrored in the UI (the Continue button is disabled). Specs are optional.
    if current == "to_estimator":
        has_drawing = (
            get_supabase()
            .table("project_files")
            .select("id")
            .eq("project_id", project_id)
            .eq("category", "drawing")
            .limit(1)
            .execute()
        ).data
        if not has_drawing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Upload at least one electrical drawing/plan before advancing to Estimate Received",
            )

    # Receive Quotes can't be left until the PE has confirmed, per category, that
    # the vendor quoted the entire RFQ — a hard rule mirrored in the UI (the
    # advance button blocks and lists the unconfirmed categories). General
    # Material has no vendor quotes and is exempt. Server-side so a direct API
    # call (or the UI's fail-open path on a fetch hiccup) can't bypass it.
    if current == "receive_quotes":
        rows = (
            get_supabase()
            .table("rfqs")
            .select("id, material_categories(name, is_general)")
            .eq("project_id", project_id)
            .eq("quotes_confirmed", False)
            .execute()
        ).data or []
        unconfirmed = [
            r for r in rows if not (r.get("material_categories") or {}).get("is_general")
        ]
        if unconfirmed:
            names = ", ".join(
                (r.get("material_categories") or {}).get("name") or "a category"
                for r in unconfirmed
            )
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Confirm the quotes are complete for every category before advancing: {names}",
            )

    owners = workflow.STAGES[current].owner_roles
    if user.role != Role.IT_ADMIN and user.role not in owners:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Only {[r.value for r in owners]} or it_admin may advance the '{current}' stage",
        )

    updated = workflow.transition_project(project_id, body.to_stage, user.id, body.note)

    # Notify the internal team that now owns the project. We use the internal
    # owner (never the estimator) so a stage like estimate_received — co-owned by
    # the estimator for access — hands off to the PE, not to every estimator's
    # external inbox. Assigned estimators are notified through their own scoped
    # paths (assignment, drawings, notes), not this broadcast.
    new_owner = workflow.internal_owner_role_for(body.to_stage)
    if new_owner:
        notify_role(
            new_owner, project_id, "stage_handoff",
            f"Project advanced to {workflow.STAGES[body.to_stage].label}",
        )
    return updated
