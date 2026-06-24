"""Go/No-Go voting and Executive override (step 2)."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import OverrideIn, VoteIn
from app.services import workflow
from app.services.gono import VOTING_ROLES, tally_decision
from app.services.notifications import audit, notify_role

router = APIRouter(prefix="/projects/{project_id}/gono", tags=["go-no-go"])


def _ensure_in_gono(project_id: str) -> None:
    proj = (
        get_supabase().table("projects").select("current_stage").eq("id", project_id).single().execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    if proj["current_stage"] != "go_no_go":
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is not in the Go/No-Go stage")


def _current_role_votes(project_id: str) -> dict[Role, str]:
    """Latest vote per voting-role for a project."""
    rows = (
        get_supabase()
        .table("go_no_go_votes")
        .select("vote, created_at, profiles!inner(role)")
        .eq("project_id", project_id)
        .order("created_at")
        .execute()
    ).data or []
    role_votes: dict[Role, str] = {}
    for r in rows:
        role = Role(r["profiles"]["role"])
        if role in VOTING_ROLES:
            role_votes[role] = r["vote"]  # later rows overwrite → latest wins
    return role_votes


def _finalize(project_id: str, outcome: str, method: str, decided_by: str | None) -> None:
    sb = get_supabase()
    sb.table("go_no_go_decisions").upsert(
        {"project_id": project_id, "outcome": outcome, "method": method, "decided_by": decided_by},
        on_conflict="project_id",
    ).execute()
    to_stage = "to_estimator" if outcome == "go" else "declined"
    workflow.transition_project(project_id, to_stage, decided_by, f"Go/No-Go: {outcome} ({method})")
    if outcome == "go":
        notify_role(Role.PA, project_id, "gono_go", "Project accepted — send to estimator")
    audit(decided_by, f"gono.{outcome}", "project", project_id, {"method": method})


@router.get("")
async def gono_status(project_id: str, user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    sb = get_supabase()
    votes = (
        sb.table("go_no_go_votes")
        .select("vote, comment, created_at, profiles!inner(full_name, role)")
        .eq("project_id", project_id)
        .order("created_at")
        .execute()
    ).data or []
    decision = (
        sb.table("go_no_go_decisions").select("*").eq("project_id", project_id).execute()
    ).data
    return {"votes": votes, "decision": decision[0] if decision else None}


@router.post("/vote")
async def cast_vote(
    project_id: str,
    body: VoteIn,
    user: CurrentUser = Depends(require_role(Role.PM, Role.PA, Role.EXECUTIVE)),
):
    _ensure_in_gono(project_id)
    sb = get_supabase()
    sb.table("go_no_go_votes").upsert(
        {"project_id": project_id, "voter_id": user.id, "vote": body.vote, "comment": body.comment},
        on_conflict="project_id,voter_id",
    ).execute()
    audit(user.id, "gono.vote", "project", project_id, {"vote": body.vote})

    outcome = tally_decision(_current_role_votes(project_id))
    if outcome:
        _finalize(project_id, outcome, "majority", user.id)
    return {"recorded": body.vote, "decided": outcome}


@router.post("/override")
async def executive_override(
    project_id: str,
    body: OverrideIn,
    user: CurrentUser = Depends(require_role(Role.EXECUTIVE)),
):
    """Executive forces the outcome, ending the vote immediately."""
    _ensure_in_gono(project_id)
    _finalize(project_id, body.outcome, "override", user.id)
    return {"decided": body.outcome, "method": "override"}
