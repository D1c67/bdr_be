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
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES
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
    """Advance one CATEGORY's head task by one step (the category model). The server
    computes the next task; `body.category` says which lane. Panel-owned heads
    (go_no_go, verify, send_out, submitted) are completed from their own endpoints."""
    category = body.category
    if not category or category not in workflow.CATEGORY_TASKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A valid `category` is required")

    proj = (
        get_supabase()
        .table("projects")
        .select("id, current_stage, abandoned_at")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    if proj["current_stage"] == "declined":
        raise HTTPException(status.HTTP_409_CONFLICT, "Project was declined at Go/No-Go")
    # An abandoned bid is frozen where it died — it can't be advanced (which would
    # also be the door into Go/No-Go). Reactivate first.
    if proj.get("abandoned_at"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This project is abandoned — reactivate it before advancing",
        )

    state = workflow.load_category_state(project_id)
    cs = state.get(category, {})
    if cs.get("status") != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"The {workflow.CATEGORY_LABELS[category]} category is not active yet",
        )
    head = cs["current_task"]
    if head in workflow.PANEL_OWNED_HEADS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{workflow.STAGES[head].label}' is completed from its own panel, not the generic advance",
        )

    # To Estimator (last intake task) can't be left until an electrical drawing exists —
    # a hard rule mirrored in the UI. This gates the intake → material/labor unlock.
    if category == "intake" and head == "to_estimator":
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
                "Upload at least one electrical drawing/plan before completing Intake",
            )

    # Receive Quotes (last material task) can't be left until every non-general RFQ
    # category is confirmed quoted-in-full — server-side so a direct call can't bypass it.
    if category == "material_numbers" and head == "receive_quotes":
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

    # Any writer role may advance any (non-panel-owned) category head; verify is the
    # only role-gated head and it is panel-owned (refused above).
    if user.role not in WRITER_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Read-only or insufficient role to advance this category",
        )

    updated = workflow.advance_category(project_id, category, user.id, body.note)

    # Advancing intake's first task moves it INTO go_no_go, which runs the score gate:
    # the project may pass straight through (>= 30), be declined (< 20), or park in
    # review — or the sender may push an outcome. When the gate moves it on, its own
    # notifications/audit cover the handoff, so return early.
    if head == "intake":
        outcome, moved = gono.apply_entry_action(project_id, user.id, body.gono_action)
        if outcome is not None:
            return {**redact_for_role(moved or updated, user.role), "gono_outcome": outcome}

    # Hand off to the internal team that now owns the affected lane(s): the advanced
    # category's new head, plus any category the fan-out just unlocked (intake complete
    # unlocks material + labor). Internal owner only (never broadcast to estimators).
    new_state = workflow.load_category_state(project_id)
    handoff_heads: list[str] = []
    adv = new_state.get(category, {})
    if adv.get("status") == "active":
        handoff_heads.append(adv["current_task"])
    for cat in workflow.CATEGORY_ORDER:
        if state.get(cat, {}).get("status") == "locked" and new_state.get(cat, {}).get("status") == "active":
            handoff_heads.append(new_state[cat]["current_task"])
    for h in dict.fromkeys(handoff_heads):  # de-dupe, preserve order
        new_owner = workflow.internal_owner_role_for(h)
        if new_owner:
            notify_role(
                new_owner, project_id, "stage_handoff",
                f"Project advanced to {workflow.STAGES[h].label}",
            )
    return redact_for_role(updated, user.role)
