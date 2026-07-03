"""Go/No-Go status and the manual decision (step 2).

The score gate itself runs when a project enters the stage (routers/workflow.py
→ services/gono.apply_entry_action). This router serves the current status and
lets any writer decide a project that is parked in review.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES
from app.core.supabase_client import get_supabase
from app.models.schemas import GonoDecisionIn
from app.services.gono import THRESHOLDS, compute_score, finalize, outcome_for_score

router = APIRouter(prefix="/projects/{project_id}/gono", tags=["go-no-go"])


@router.get("")
async def gono_status(project_id: str, user: CurrentUser = Depends(get_current_user)):
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
    return {
        "score": score,
        "outcome": outcome_for_score(score),
        "thresholds": THRESHOLDS,
        "decision": decision[0] if decision else None,
    }


@router.post("/decide")
async def decide(
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
    if project["current_stage"] != "go_no_go":
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is not in the Go/No-Go stage")
    finalize(project_id, body.outcome, "manual", user.id, score=compute_score(project))
    return {"decided": body.outcome, "method": "manual"}
