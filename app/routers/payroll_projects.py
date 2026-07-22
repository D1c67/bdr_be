"""Certified Payroll — project enrollment router: the enrolled list, the
enrollment picker, enroll/unenroll, and CP detail edits.

CP shares the projects spine with bidding and PM but is a separate membership
(cp_enrolled_at + cp_details, see services/payroll_projects.py). Reads are any
CP-read role (accountant included, the external estimator never); writes are
CP-write roles. The weekly payroll pipeline lives in its own routers under the
same /payroll prefix.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_cp_read, require_cp_write
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.models.schemas import CpDetailsPatch, CpEnrollBody, CpProjectCreate
from app.routers.projects import _NUMBER_TAKEN, _is_duplicate_number, redact_for_role
from app.services import payroll_projects
from app.services.notifications import audit
from app.services.project_status import derive_status

router = APIRouter(prefix="/payroll/projects", tags=["payroll"])

_CP_SELECT = "*, bid_outcomes(result), cp_details(*)"


def _outcome_result(project: dict) -> str | None:
    """Pop the bid_outcomes embed (unique FK — object or list by PostgREST
    version) down to the bare result string."""
    outcome = project.pop("bid_outcomes", None)
    if isinstance(outcome, list):
        return outcome[0].get("result") if outcome else None
    if isinstance(outcome, dict):
        return outcome.get("result")
    return None


def _present_cp(project: dict, role: Role) -> dict:
    """Attach derived status + flatten the cp_details embed, then redact.
    The CP mirror of pm._present_pm — enrolled rows still carry bid facts
    (status 'won', confidential actual_bid_at) that need the same treatment."""
    result = _outcome_result(project)
    details = project.get("cp_details")
    if isinstance(details, list):
        details = details[0] if details else None
    project["status"] = derive_status(
        project.get("current_stage"), project.get("abandoned_at"), result
    )
    project["cp_details"] = details
    return redact_for_role(project, role)


@router.get("")
def list_cp_projects(user: CurrentUser = Depends(require_cp_read)):
    """The CP dashboard list: every project enrolled in Certified Payroll."""
    resp = (
        get_supabase()
        .table("projects")
        .select(_CP_SELECT)
        .not_.is_("cp_enrolled_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    return [_present_cp(p, user.role) for p in resp.data or []]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_cp_project(
    body: CpProjectCreate,
    user: CurrentUser = Depends(require_cp_write),
):
    """Direct creation INSIDE Certified Payroll — a brand-new prevailing-wage
    project that never existed as a bid. Mirrors PM's POST /pm/projects: the
    project is created at current_stage='cp_only' and enrolled in one shot (the
    same hard-gated compliance set as /enroll). Project numbers share the bidding
    uniqueness rule (0052) — a re-used number is a clean 409, not a raw 500."""
    try:
        created = payroll_projects.create_direct_cp_project(body, user.id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — unique violation → number re-use
        if _is_duplicate_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _NUMBER_TAKEN) from exc
        raise
    return _present_cp(payroll_projects.require_cp_project(created["id"]), user.role)


@router.get("/eligible")
def list_eligible_projects(user: CurrentUser = Depends(require_cp_read)):
    """The enrollment picker: EVERY BDR project at any lifecycle point
    (bidding, PM-only, cp_only imports), minimally shaped — the FE disables
    rows already enrolled rather than hiding them."""
    rows = (
        get_supabase()
        .table("projects")
        .select(
            "id, name, number, current_stage, cp_enrolled_at, abandoned_at,"
            " bid_outcomes(result)"
        )
        .order("created_at", desc=True)
        .execute()
    ).data or []
    return [
        {
            "id": p["id"],
            "name": p.get("name"),
            "number": p.get("number"),
            "status": derive_status(
                p.get("current_stage"), p.get("abandoned_at"), _outcome_result(p)
            ),
            "cp_enrolled": bool(p.get("cp_enrolled_at")),
        }
        for p in rows
    ]


@router.post("/{project_id}/enroll", status_code=status.HTTP_201_CREATED)
def enroll_project(
    project_id: str,
    body: CpEnrollBody,
    user: CurrentUser = Depends(require_cp_write),
):
    """Enroll a project into Certified Payroll (explicit, prevailing-wage only)."""
    payroll_projects.enroll_project(project_id, body, user.id)
    return _present_cp(payroll_projects.require_cp_project(project_id), user.role)


@router.get("/{project_id}")
def get_cp_project(project_id: str, user: CurrentUser = Depends(require_cp_read)):
    """The CP overview: project + flattened cp_details, same shape as the list."""
    return _present_cp(payroll_projects.require_cp_project(project_id), user.role)


@router.patch("/{project_id}")
def update_cp_details(
    project_id: str,
    body: CpDetailsPatch,
    user: CurrentUser = Depends(require_cp_write),
):
    """Edit the CP detail record. exclude_unset: an explicit null clears a field.
    Name/number edits go through the shared PATCH /projects/{id}."""
    payroll_projects.require_cp_project(project_id)  # 404 unless enrolled
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    updated = (
        get_supabase()
        .table("cp_details")
        .update(patch)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certified Payroll details not found")
    audit(user.id, "cp.details_update", "project", project_id, patch)
    return _present_cp(payroll_projects.require_cp_project(project_id), user.role)


@router.delete("/{project_id}/enrollment", status_code=status.HTTP_204_NO_CONTENT)
def unenroll_project(project_id: str, user: CurrentUser = Depends(require_cp_write)):
    """Remove a project from Certified Payroll — refused (409) once any
    certified time entry references it."""
    payroll_projects.unenroll_project(project_id, user.id)
