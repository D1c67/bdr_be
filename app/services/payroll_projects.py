"""Certified Payroll enrollment — the seam between the projects spine and CP.

A project is "in Certified Payroll" when projects.cp_enrolled_at is set AND a
cp_details row exists (the pm_details pattern from 0057, mirrored in 0063).
Enrollment is explicit and user-selected — prevailing-wage projects only, never
automatic on won — and idempotent under races: the projects update is
optimistically locked on cp_enrolled_at IS NULL and cp_details.project_id is
unique, so a racing double-submit can never double-enroll.

Unenrollment is refused once any CP activity references the project: certified
time entries must not be stranded by an enrollment edit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.supabase_client import get_supabase
from app.services.notifications import audit

# Kept in sync with the router's list select — require_cp_project must return
# the same shape the list presents (cp_details flattened, outcome embedded).
_CP_SELECT = "*, bid_outcomes(result), cp_details(*)"


def _is_duplicate_cp_details(exc: Exception) -> bool:
    msg = str(exc)
    return "cp_details" in msg and ("23505" in msg or "duplicate" in msg.lower())


def require_cp_project(project_id: str) -> dict:
    """Module-route guard: 404 when the project doesn't exist or has no CP
    life. Returns the projects row with the cp_details embed flattened to a
    single dict (unique FK — PostgREST may embed a list or an object)."""
    from fastapi import HTTPException, status  # local: keep the service framework-light

    rows = (
        get_supabase()
        .table("projects")
        .select(_CP_SELECT)
        .eq("id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    project = rows[0]
    if project.get("cp_enrolled_at") is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project is not in Certified Payroll")
    details = project.get("cp_details")
    if isinstance(details, list):
        project["cp_details"] = details[0] if details else None
    return project


def enroll_project(project_id: str, body, actor_id: str) -> dict:
    """Enroll a project into Certified Payroll. Returns the created cp_details
    row. 404 unknown project; 409 when it is already enrolled (or lost a race —
    the enrollment markers are locked exactly like pm.activate_pm_for_win locks
    pm_stage)."""
    from fastapi import HTTPException, status  # local: keep the service framework-light

    sb = get_supabase()
    rows = sb.table("projects").select("id, name").eq("id", project_id).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    updated = (
        sb.table("projects")
        .update(
            {
                "cp_enrolled_at": datetime.now(timezone.utc).isoformat(),
                "cp_enrolled_by": actor_id,
            }
        )
        .eq("id", project_id)
        .is_("cp_enrolled_at", "null")
        .execute()
    ).data
    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Project is already in Certified Payroll"
        )

    details = {"project_id": project_id, **body.model_dump(mode="json")}
    try:
        created = sb.table("cp_details").insert(details).execute().data[0]
    except Exception as exc:  # noqa: BLE001 — compensate, then classify
        if _is_duplicate_cp_details(exc):
            # A retried insert against the unique project_id: the row already
            # exists, so the markers we just set complete the enrollment —
            # clearing them would strand the cp_details row unreachable.
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Project is already in Certified Payroll"
            ) from exc
        # Compensating update (the create_direct_project idiom): clear the
        # enrollment markers so no half-enrolled project haunts the CP list.
        sb.table("projects").update(
            {"cp_enrolled_at": None, "cp_enrolled_by": None}
        ).eq("id", project_id).execute()
        raise

    audit(
        actor_id,
        "cp.enroll",
        "project",
        project_id,
        {"contract_id": body.contract_id, "report_type": body.report_type},
    )
    return created


def create_direct_cp_project(body, actor_id: str) -> dict:
    """Create a brand-new project directly INSIDE Certified Payroll (never a
    bid) — the CP mirror of pm.create_direct_project. The project skips the
    bidding pipeline entirely: current_stage='cp_only' (like pm_only), zero
    stage_events, and it is enrolled in the same shot (cp_enrolled_at set +
    cp_details written), so it lands on the CP dashboard immediately.

    The projects.number uniqueness (0052) still applies — a collision surfaces
    as a 23505 which the router maps to a clean 409, exactly like the bidding
    and PM create paths. Returns the created projects row (caller presents it via
    require_cp_project). Compensating delete on a cp_details failure so no
    half-created project is left invisible to every dashboard."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    project_payload = {
        "name": body.name,
        "number": body.number,
        "address": body.address,
        "current_stage": "cp_only",
        "current_owner_role": None,
        "cp_enrolled_at": now,
        "cp_enrolled_by": actor_id,
        "created_by": actor_id,
    }
    created = sb.table("projects").insert(project_payload).execute().data[0]

    # The CpEnrollBody half (compliance + contractor address) maps 1:1 onto
    # cp_details columns; drop the spine-only fields we just wrote to projects.
    details = {
        "project_id": created["id"],
        **body.model_dump(mode="json", exclude={"name", "number", "address"}),
    }
    try:
        sb.table("cp_details").insert(details).execute()
    except Exception:
        # Compensating delete (cascade cleans children): the projects insert
        # already succeeded, so roll it back rather than strand a stage='cp_only'
        # row with no cp_details on the CP list.
        sb.table("projects").delete().eq("id", created["id"]).execute()
        raise

    audit(
        actor_id,
        "cp.project_create",
        "project",
        created["id"],
        {
            "number": created["number"],
            "contract_id": body.contract_id,
            "report_type": body.report_type,
        },
    )
    return created


def cp_activity_exists(project_id: str) -> bool:
    """True when any certified time entry references the project — the
    unenrollment guard (report rows would be stranded by removing it)."""
    rows = (
        get_supabase()
        .table("cp_time_entries")
        .select("id")
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    ).data or []
    return bool(rows)


def unenroll_project(project_id: str, actor_id: str) -> None:
    """Remove a project from Certified Payroll (enrolled by mistake). Refused
    once CP activity exists. Details are deleted before the markers clear so a
    crash between the two leaves a retryable state, never a marker-less
    cp_details row that would block re-enrollment."""
    from fastapi import HTTPException, status  # local: keep the service framework-light

    require_cp_project(project_id)
    if cp_activity_exists(project_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Certified Payroll activity exists for this project"
        )
    sb = get_supabase()
    sb.table("cp_details").delete().eq("project_id", project_id).execute()
    sb.table("projects").update(
        {"cp_enrolled_at": None, "cp_enrolled_by": None}
    ).eq("id", project_id).execute()
    audit(actor_id, "cp.unenroll", "project", project_id, {})
