"""Workflow transitions and stage-event history.

The generic `/advance` endpoint moves a project forward one legal step. Any
writer role (every internal role except the read-only accountant) may advance any
stage — the per-stage owner is only a "whose task" hint. The sole exception is
`verify`, which is restricted to the Executive (with IT Admin as override).

Advancing into `go_no_go` runs the score gate (services/gono): score >= 30 goes
straight through to To Estimator, below 20 is declined, 20-29 parks in review —
unless the sender pushes review/go/no_go explicitly (TransitionIn.gono_action).
Leaving `go_no_go` is refused here: a project leaves it only through a decision
(the gate or the gono decide endpoint).
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import INTERNAL_ROLES, VERIFY_ROLES, WRITER_ROLES
from app.core.supabase_client import get_supabase
from app.models.schemas import TransitionIn
from app.routers.projects import redact_for_role
from app.services import gono, workflow
from app.services.notifications import notify_role

router = APIRouter(prefix="/projects/{project_id}", tags=["workflow"])


@router.get("/stage-events")
def stage_events(project_id: str, user: CurrentUser = Depends(get_current_user)):
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
def advance(
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
        raise HTTPException(status.HTTP_409_CONFLICT, "Use the Go/No-Go decide endpoint")

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

    # Verify is restricted to the Executive (IT Admin override); every other stage
    # may be advanced by any writer role. The accountant (read-only) and estimator
    # are never writers and are rejected here.
    if current == "verify":
        if user.role not in VERIFY_ROLES:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only the executive (or it_admin) may advance the 'verify' stage",
            )
    elif user.role not in WRITER_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Read-only or insufficient role to advance this stage",
        )

    updated = workflow.transition_project(project_id, body.to_stage, user.id, body.note)

    # Entering Go/No-Go runs the score gate: the project may pass straight
    # through (>= 30), be declined (< 20), or park in review — or the sender may
    # have pushed an outcome explicitly. When the gate moves the project on, its
    # own notifications/audit cover the handoff, so skip the generic one below.
    if body.to_stage == "go_no_go":
        outcome, moved = gono.apply_entry_action(project_id, user.id, body.gono_action)
        if outcome is not None:
            # Redact like every other project-row response — the raw update row
            # carries the confidential actual_bid_at, which non-viewer writers
            # (e.g. estimating_engineer) must not receive.
            return {**redact_for_role(moved or updated, user.role), "gono_outcome": outcome}

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
    return redact_for_role(updated, user.role)
