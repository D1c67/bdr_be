"""Projects + intake (step 1) and the dashboard list."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_internal, require_role
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    AbandonIn,
    ProjectCreate,
    ProjectGCIn,
    ProjectOut,
    ProjectUpdate,
)
from app.services import proposal_send, workflow
from app.services.notifications import audit, notify_role
from app.services.project_status import derive_status

# Roles allowed to abandon / reactivate a project (same set that may see the
# confidential actual bid date).
STATUS_CHANGE_ROLES = (Role.EXECUTIVE, Role.PA, Role.IT_ADMIN)

router = APIRouter(prefix="/projects", tags=["projects"])


def redact_for_role(project: dict, role: Role) -> dict:
    """Null the actual (to-GC) bid date for roles that may not see it.

    Redaction is server-side so the date never reaches the client; every
    handler that returns a project row must pass it through here.
    """
    if role in ACTUAL_BID_VIEWER_ROLES:
        return project
    return {**project, "actual_bid_at": None}


def _present(project: dict, role: Role) -> dict:
    """Attach the derived lifecycle `status` (from the embedded bid outcome, if
    any) and redact. Pass every returned project row through here so the API
    `status` field stays consistent with the dashboard/analytics derivation."""
    outcome = project.pop("bid_outcomes", None)
    # The projects↔bid_outcomes FK is unique, so PostgREST may embed it as a
    # single object (to-one) or a list depending on version — handle both.
    if isinstance(outcome, list):
        result = outcome[0].get("result") if outcome else None
    elif isinstance(outcome, dict):
        result = outcome.get("result")
    else:
        result = None
    project["status"] = derive_status(
        project.get("current_stage"), project.get("abandoned_at"), result
    )
    return redact_for_role(project, role)


def _fetch_project_with_outcome(project_id: str) -> dict:
    """Load a project plus its (0-or-1) bid outcome so `status` is fully derivable."""
    resp = (
        get_supabase()
        .table("projects")
        .select("*, bid_outcomes(result)")
        .eq("id", project_id)
        .single()
        .execute()
    )
    if not resp.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return resp.data


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    stage: str | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """Dashboard list. Estimators never see the full list (assigned-only)."""
    if user.role == Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators use /estimator/projects")
    if stage is not None and stage not in workflow.STAGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown stage: {stage}")
    query = get_supabase().table("projects").select("*, bid_outcomes(result)")
    if stage is not None:
        query = query.eq("current_stage", stage)
    resp = query.order("created_at", desc=True).execute()
    return [_present(p, user.role) for p in resp.data or []]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    user: CurrentUser = Depends(require_role(Role.PA, Role.PM, Role.IT_ADMIN)),
):
    """Create a project (typically the PA). Starts in the `intake` stage."""
    sb = get_supabase()
    payload = body.model_dump(exclude={"gcs"}, mode="json")
    payload["created_by"] = user.id
    payload["current_stage"] = "intake"
    payload["current_owner_role"] = Role.PA.value
    created = sb.table("projects").insert(payload).execute().data[0]

    if body.gcs:
        sb.table("project_gcs").insert(
            [{"project_id": created["id"], "gc_id": g.gc_id} for g in body.gcs]
        ).execute()

    # Record the initial stage event so analytics has a start timestamp.
    sb.table("stage_events").insert(
        {"project_id": created["id"], "from_stage": None, "to_stage": "intake", "actor_id": user.id}
    ).execute()
    audit(user.id, "project.create", "project", created["id"], {"number": created["number"]})
    return _present(created, user.role)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return _present(_fetch_project_with_outcome(project_id), user.role)


# Who may edit each intake field via PATCH. Bid dates are the PA's alone;
# deadlines, schedule, and labor fields are open to every internal role; the
# identity fields keep their original PA/PM ownership. Pricing is never
# patchable here — it lives in the quote/labor/markup steps.
_FIELD_EDITORS: dict[str, frozenset[Role]] = {
    "internal_bid_at": frozenset({Role.PA}),
    "actual_bid_at": frozenset({Role.PA}),
    "due_from_estimator_at": INTERNAL_ROLES,
    "due_from_vendors_at": INTERNAL_ROLES,
    "est_start_date": INTERNAL_ROLES,
    "est_finish_date": INTERNAL_ROLES,
    "labor_time": INTERNAL_ROLES,
    "wage_type": INTERNAL_ROLES,
    "labor_note": INTERNAL_ROLES,
    "address": INTERNAL_ROLES,
    "name": frozenset({Role.PA, Role.PM, Role.IT_ADMIN}),
    "number": frozenset({Role.PA, Role.PM, Role.IT_ADMIN}),
    "invitation_at": frozenset({Role.PA, Role.PM, Role.IT_ADMIN}),
    "notes": frozenset({Role.PA, Role.PM, Role.IT_ADMIN}),
    # Go/No-Go scoring answers (reference only) — open like the labor fields.
    "project_type": INTERNAL_ROLES,
    "owner_type": INTERNAL_ROLES,
    "labor_needed": INTERNAL_ROLES,
    "bid_method": INTERNAL_ROLES,
    "competitor_known": INTERNAL_ROLES,
    "gc_known": INTERNAL_ROLES,
    "subs_needed": INTERNAL_ROLES,
    "est_value_band": INTERNAL_ROLES,
    "scope_fit": INTERNAL_ROLES,
}


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: CurrentUser = Depends(require_role(*INTERNAL_ROLES)),
):
    # exclude_unset (not exclude_none) so an explicit null clears a field.
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if patch.get("name", "") is None or patch.get("number", "") is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name and number cannot be cleared")
    denied = sorted(f for f in patch if user.role not in _FIELD_EDITORS.get(f, frozenset()))
    if denied:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Your role may not edit: {', '.join(denied)}",
        )
    updated = (
        get_supabase().table("projects").update(patch).eq("id", project_id).execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    audit(user.id, "project.update", "project", project_id, patch)
    return _present(updated[0], user.role)


# ── Abandon / reactivate ────────────────────────────────────────────────────
# A status change that is NOT a stage transition: abandon leaves current_stage
# untouched (so we know where the bid died) and only flips the abandon marker,
# so it does a direct projects.update rather than going through
# workflow.transition_project (which would validate against TRANSITIONS and
# overwrite the stage). Reversible via /reactivate. Both are restricted to the
# Executive / PA / IT admin set.


def _project_status_row(project_id: str) -> dict:
    row = (
        get_supabase()
        .table("projects")
        .select("id, name, current_stage, abandoned_at")
        .eq("id", project_id)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return row[0]


@router.post("/{project_id}/abandon", response_model=ProjectOut)
async def abandon_project(
    project_id: str,
    body: AbandonIn | None = None,
    user: CurrentUser = Depends(require_role(*STATUS_CHANGE_ROLES)),
):
    """Abandon a bid at its current stage. `current_stage` is preserved; the
    derived status becomes `abandoned`. Reversible via /reactivate."""
    existing = _project_status_row(project_id)
    if existing.get("abandoned_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is already abandoned")
    now = datetime.now(timezone.utc).isoformat()
    updated = (
        get_supabase()
        .table("projects")
        .update({"abandoned_at": now, "abandoned_by": user.id})
        .eq("id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    audit(user.id, "project.abandon", "project", project_id,
          {"stage": existing["current_stage"], "note": body.note if body else None})
    notify_role(Role.EXECUTIVE, project_id, "project_abandoned",
                f"Project abandoned: {existing['name']}")
    return _present(updated[0], user.role)


@router.post("/{project_id}/reactivate", response_model=ProjectOut)
async def reactivate_project(
    project_id: str,
    user: CurrentUser = Depends(require_role(*STATUS_CHANGE_ROLES)),
):
    """Reactivate an abandoned project, returning it to its stage-derived status."""
    existing = _project_status_row(project_id)
    if not existing.get("abandoned_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is not abandoned")
    updated = (
        get_supabase()
        .table("projects")
        .update({"abandoned_at": None, "abandoned_by": None})
        .eq("id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    audit(user.id, "project.reactivate", "project", project_id,
          {"stage": existing["current_stage"]})
    # Re-fetch with the outcome embedded so a reactivated win/loss bid reports its
    # true status (won/lost), not just the abandon-free fallback.
    return _present(_fetch_project_with_outcome(project_id), user.role)


# ── project ↔ GC membership ────────────────────────────────────────────────
# Editable at ANY stage by any internal role (the estimator is rejected): GCs
# join and drop out of bids mid-pipeline, so membership can't be frozen at
# intake. Membership is the whole story — any GC on the project is a bid
# candidate; who we actually bid to is recorded by which proposals were sent.
# The send path is hardened against the set changing under it
# (assert_send_isolation re-verifies every row against the live GC).


def _project_or_404(project_id: str) -> dict:
    row = (
        get_supabase()
        .table("projects")
        .select("id, name, current_stage")
        .eq("id", project_id)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return row[0]


def _project_gc_rows(project_id: str) -> list[dict]:
    """Wire shape shared by the GET and returned from every membership write
    (the panel swaps its whole list for the response)."""
    rows = (
        get_supabase()
        .table("project_gcs")
        .select("general_contractors(id, name, gc_contacts(id, name, email, phone))")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    out = []
    for r in rows:
        gc = r.get("general_contractors")
        if not gc:
            continue
        contacts = sorted(gc.get("gc_contacts") or [], key=lambda c: (c.get("name") or "").lower())
        out.append({"id": gc["id"], "name": gc["name"], "contacts": contacts})
    return sorted(out, key=lambda g: g["name"].lower())


def _block_if_sending(project_id: str, gc_id: str) -> None:
    """Dropping a GC mid-send would trip the isolation assertions and mark the
    send failed — make the user resolve the in-flight send first."""
    sending = (
        get_supabase()
        .table("proposal_sends")
        .select("id")
        .eq("project_id", project_id)
        .eq("gc_id", gc_id)
        .eq("status", "sending")
        .limit(1)
        .execute()
    ).data
    if sending:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A proposal send to this GC is in progress or unresolved — wait or retry it first.",
        )


@router.get("/{project_id}/gcs")
async def list_project_gcs(project_id: str, _: CurrentUser = Depends(require_internal)):
    _project_or_404(project_id)
    return _project_gc_rows(project_id)


@router.post("/{project_id}/gcs", status_code=status.HTTP_201_CREATED)
async def add_project_gc(
    project_id: str,
    body: ProjectGCIn,
    user: CurrentUser = Depends(require_internal),
):
    sb = get_supabase()
    _project_or_404(project_id)
    gc = (
        sb.table("general_contractors").select("id, name").eq("id", body.gc_id).execute()
    ).data
    if not gc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GC not found")
    existing = (
        sb.table("project_gcs")
        .select("id")
        .eq("project_id", project_id)
        .eq("gc_id", body.gc_id)
        .limit(1)
        .execute()
    ).data
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{gc[0]['name']} is already on this project"
        )
    sb.table("project_gcs").insert(
        {"project_id": project_id, "gc_id": body.gc_id}
    ).execute()
    audit(user.id, "project.gc_add", "project", project_id,
          {"gc_id": body.gc_id, "gc_name": gc[0]["name"]})
    return _project_gc_rows(project_id)


@router.delete("/{project_id}/gcs/{gc_id}")
async def remove_project_gc(
    project_id: str,
    gc_id: str,
    user: CurrentUser = Depends(require_internal),
):
    sb = get_supabase()
    _project_or_404(project_id)
    rows = (
        sb.table("project_gcs")
        .select("id")
        .eq("project_id", project_id)
        .eq("gc_id", gc_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GC is not on this project")
    _block_if_sending(project_id, gc_id)
    sb.table("project_gcs").delete().eq("project_id", project_id).eq("gc_id", gc_id).execute()
    # Sent history stays in proposal_sends; never-sent rows are retired so the
    # Send Out panel stops offering them.
    proposal_send.retire_unsent_proposals(project_id, gc_id)
    audit(user.id, "project.gc_remove", "project", project_id, {"gc_id": gc_id})
    return _project_gc_rows(project_id)
