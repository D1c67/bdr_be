"""Go/No-Go status, the manual decision (step 2), and undoing a decision.

The score gate itself runs when a project enters the stage (routers/workflow.py
→ services/gono.apply_entry_action). This router serves the current status, lets
any writer decide a project that is parked in review, and lets any writer take a
recorded decision back (`/undo`) while it has changed nothing downstream.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES
from app.core.supabase_client import get_supabase
from app.models.schemas import GonoDecisionIn
from app.services import gono, workflow
from app.services.gono import THRESHOLDS, compute_score, finalize, outcome_for_score

router = APIRouter(prefix="/projects/{project_id}/gono", tags=["go-no-go"])


@router.get("")
def gono_status(project_id: str, user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    sb = get_supabase()
    project = (
        sb.table("projects").select("*").eq("id", project_id).single().execute()
    ).data
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    decision = (
        sb.table("go_no_go_decisions")
        .select("*, profiles(full_name)")
        .eq("project_id", project_id)
        .execute()
    ).data
    score = compute_score(project)
    decided = decision[0] if decision else None
    return {
        "score": score,
        "outcome": outcome_for_score(score),
        "thresholds": THRESHOLDS,
        "decision": decided,
        # Whether the recorded decision can still be taken back, and why not.
        **gono.undo_status(project, workflow.load_category_state(project_id), decided),
    }


@router.post("/decide")
def decide(
    project_id: str,
    body: GonoDecisionIn,
    user: CurrentUser = Depends(require_role(*WRITER_ROLES)),
):
    """Any writer pushes a project in review to Go or No-Go, whatever its score."""
    project = (
        get_supabase()
        .table("projects")
        .select("*")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    state = workflow.load_category_state(project_id)
    if state.get("intake", {}).get("current_task") != "go_no_go" or state["intake"]["status"] != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is not in the Go/No-Go step")
    finalize(project_id, body.outcome, "manual", user.id, score=compute_score(project))
    return {"decided": body.outcome, "method": "manual"}


@router.post("/undo")
def undo_decision(
    project_id: str,
    user: CurrentUser = Depends(require_role(*WRITER_ROLES)),
):
    """Take back the recorded decision and put the project back in review here.

    A No-Go always reverses (a declined project is frozen). A Go only reverses
    while it changed nothing outside the gate — still at To Estimator, no
    estimator assigned, no package sent — otherwise this is a 409 saying so.
    """
    gono.undo(project_id, user.id)
    return {"undone": True}
