"""Estimator hand-off (steps 3-4): assignments, send-to-estimator email, and the
minimal estimator-facing endpoints.

The estimator is an external/untrusted user. Their access is gated everywhere by
`require_project_assignment` and they see only assigned projects + drawing files.
"""

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import (
    CurrentUser,
    get_current_user,
    require_project_assignment,
    require_role,
)
from app.core.ratelimit import estimator_rate_limit
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import general_material, graph_email, storage
from app.services.notifications import audit, dismiss_notifications, notify_role, notify_user

router = APIRouter(tags=["estimator"])

# A project must have at least one electrical drawing before it can be handed to
# the estimator — enforced here (not just in the UI) because it's a hard rule:
# you can't assign or email an estimator a package with no drawings.
NO_DRAWING_MESSAGE = "Upload at least one electrical drawing/plan first"


def project_has_drawing(project_id: str) -> bool:
    rows = (
        get_supabase()
        .table("project_files")
        .select("id")
        .eq("project_id", project_id)
        .eq("category", "drawing")
        .limit(1)
        .execute()
    ).data or []
    return bool(rows)


class AssignIn(BaseModel):
    estimator_id: str
    due_at: datetime | None = None
    expires_at: datetime | None = None


# ── Picking an estimator (PA/PM/IT Admin) ─────────────────────────────────


@router.get("/estimators")
async def list_estimators(_: CurrentUser = Depends(require_role(Role.PA, Role.PM, Role.IT_ADMIN))):
    # Estimator-role profiles plus dev accounts (is_dev). Dev accounts can switch
    # their own role and bypass the estimator assignment gates (see deps.py), so
    # they're selectable here to test/run the estimator flow themselves.
    return (
        get_supabase()
        .table("profiles")
        .select("id, full_name, email, role, is_dev")
        .or_("role.eq.estimator,is_dev.eq.true")
        .eq("is_active", True)
        .order("full_name")
        .execute()
    ).data or []


# ── Assignment management ─────────────────────────────────────────────────


@router.post("/projects/{project_id}/assign-estimator", status_code=status.HTTP_201_CREATED)
async def assign_estimator(
    project_id: str,
    body: AssignIn,
    user: CurrentUser = Depends(require_role(Role.PA, Role.PM, Role.IT_ADMIN)),
):
    if not project_has_drawing(project_id):
        raise HTTPException(status.HTTP_409_CONFLICT, NO_DRAWING_MESSAGE)
    row = (
        get_supabase()
        .table("estimator_assignments")
        .insert(
            {
                "project_id": project_id,
                "estimator_id": body.estimator_id,
                "assigned_by": user.id,
                "due_at": body.due_at.isoformat() if body.due_at else None,
                "expires_at": body.expires_at.isoformat() if body.expires_at else None,
            }
        )
        .execute()
    ).data[0]
    audit(user.id, "estimator.assign", "project", project_id, {"estimator_id": body.estimator_id})
    notify_user(body.estimator_id, project_id, "assigned", "You were assigned to a project")
    return row


@router.get("/projects/{project_id}/assignments")
async def list_assignments(
    project_id: str, _: CurrentUser = Depends(require_role(Role.PA, Role.PM, Role.IT_ADMIN))
):
    return (
        get_supabase()
        .table("estimator_assignments")
        .select("*, profiles!estimator_assignments_estimator_id_fkey(full_name, email)")
        .eq("project_id", project_id)
        .execute()
    ).data or []


@router.post("/projects/{project_id}/assignments/{assignment_id}/revoke")
async def revoke_assignment(
    project_id: str,
    assignment_id: str,
    user: CurrentUser = Depends(require_role(Role.PA, Role.PM, Role.IT_ADMIN)),
):
    rows = (
        get_supabase()
        .table("estimator_assignments")
        .update({"revoked_at": "now()"})
        .eq("id", assignment_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    audit(user.id, "estimator.revoke", "project", project_id, {"assignment_id": assignment_id})
    # The estimator no longer has the project, so their "assigned" ping is stale.
    estimator_id = rows[0]["estimator_id"] if rows else None
    if estimator_id:
        dismiss_notifications(project_id=project_id, types=["assigned"], user_id=estimator_id)
    return {"revoked": True}


# ── Send to estimator (step 3): email drawings + due date ─────────────────


@router.post("/projects/{project_id}/send-to-estimator")
async def send_to_estimator(
    project_id: str,
    user: CurrentUser = Depends(require_role(Role.PA, Role.PM)),
):
    sb = get_supabase()
    proj = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    # Active assignees → recipients.
    assigns = (
        sb.table("estimator_assignments")
        .select("estimator_id, profiles!estimator_assignments_estimator_id_fkey(email, full_name)")
        .eq("project_id", project_id)
        .is_("revoked_at", "null")
        .execute()
    ).data or []
    if not assigns:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assign an estimator first")
    recipients = [a["profiles"]["email"] for a in assigns if a.get("profiles")]

    # Short-TTL signed links to the electrical drawings. Hand-off requires at
    # least one — never email an estimator a package with no drawings.
    drawings = (
        sb.table("project_files")
        .select("filename, storage_path")
        .eq("project_id", project_id)
        .eq("category", "drawing")
        .execute()
    ).data or []
    if not drawings:
        raise HTTPException(status.HTTP_409_CONFLICT, NO_DRAWING_MESSAGE)
    links = "".join(
        # use_cache=False: emailed links must carry the full TTL, never a
        # partially-spent memoized URL.
        f'<li><a href="{storage.signed_url(d["storage_path"], use_cache=False)}">{d["filename"]}</a></li>'
        for d in drawings
    )
    due = proj.get("due_from_estimator_at") or "TBD"
    body_html = (
        f"<p>Project <b>{proj['name']}</b> ({proj['number']}) is ready for estimating.</p>"
        f"<p>Due back from estimator: <b>{due}</b></p>"
        f"<p>Electrical drawings:</p><ul>{links or '<li>(none uploaded)</li>'}</ul>"
        f"<p>Please upload your Estimate, BOQ, and markups via the BDR portal.</p>"
    )

    log = graph_email.send_mail(
        to=recipients,
        subject=f"[BDR] Estimate request — {proj['name']} ({proj['number']})",
        body_html=body_html,
        project_id=project_id,
        sent_by=user.id,
    )
    # Start the turnaround clock once, on the first send. Re-sends (e.g. to add a
    # recipient) leave the original timestamp so the measured time stays honest.
    sb.table("estimator_assignments").update({"sent_to_estimator_at": "now()"}).eq(
        "project_id", project_id
    ).is_("revoked_at", "null").is_("sent_to_estimator_at", "null").execute()
    audit(user.id, "estimator.email_sent", "project", project_id, {"to": recipients})
    return {"sent_to": recipients, "email_log_id": log["id"]}


# ── Estimator-facing minimal endpoints ────────────────────────────────────


@router.get("/estimator/projects", dependencies=[Depends(estimator_rate_limit)])
async def my_assigned_projects(user: CurrentUser = Depends(get_current_user)):
    """An estimator's assigned projects — minimal fields only."""
    if user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators only")
    sb = get_supabase()
    assigns = (
        sb.table("estimator_assignments")
        .select("project_id, due_at, expires_at")
        .eq("estimator_id", user.id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .execute()
    ).data or []
    if not assigns:
        return []
    ids = [a["project_id"] for a in assigns]
    projs = (
        sb.table("projects").select("id, name, number, current_stage").in_("id", ids).execute()
    ).data or []
    due_by = {a["project_id"]: a["due_at"] for a in assigns}
    return [{**p, "due_at": due_by.get(p["id"])} for p in projs]


@router.post("/estimator/projects/{project_id}/submit", dependencies=[Depends(estimator_rate_limit)])
async def submit_deliverables(
    project_id: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_project_assignment),
):
    """The estimator hands their deliverables back to the team.

    Files are already uploaded; this signals completion so the PA/PM know the
    estimate is ready. Requires at least one deliverable file to exist.
    """
    if user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators only")
    sb = get_supabase()
    proj = sb.table("projects").select("name, number").eq("id", project_id).single().execute().data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    files = (
        sb.table("project_files")
        .select("category")
        .eq("project_id", project_id)
        .in_("category", ["estimate", "boq", "markup"])
        .execute()
    ).data or []
    if not files:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Upload at least one estimate, BOQ, or markup file first"
        )

    counts: dict[str, int] = {}
    for f in files:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    summary = ", ".join(f"{n} {c}" for c, n in counts.items())

    # Stamp the return so analytics can measure received → returned turnaround.
    # Last submit wins (a re-submit reflects the latest hand-off).
    sb.table("estimator_assignments").update({"returned_at": "now()"}).eq(
        "project_id", project_id
    ).eq("estimator_id", user.id).is_("revoked_at", "null").execute()

    audit(user.id, "estimator.submit", "project", project_id, {"counts": counts})
    msg = f"Estimator submitted deliverables for {proj['name']} ({proj['number']}): {summary}"
    notify_role(Role.PA, project_id, "estimate_submitted", msg)
    notify_role(Role.PM, project_id, "estimate_submitted", msg)

    # Pull the general-material (wiring) price from the estimate in the background.
    if counts.get("estimate"):
        background.add_task(general_material.run_extraction, project_id)
    return {"submitted": True, "counts": counts}


@router.get("/estimator/projects/{project_id}")
async def estimator_project_detail(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    if user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators only")
    # Minimal projection — never pricing/markup/quotes.
    return (
        get_supabase()
        .table("projects")
        .select("id, name, number, current_stage, due_from_estimator_at, notes")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
