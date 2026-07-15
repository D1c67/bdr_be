"""Project Management stage machine — the PM mirror of services/workflow.py.

PM stages (precon → active_construction → closeout) live on projects.pm_stage,
a SEPARATE axis from the bidding pipeline's current_stage, and every move is
logged to pm_stage_events (never stage_events — the two lifecycles must not
bleed into each other's analytics). Forward moves are adjacent-only; backward
moves are allowed to any earlier stage but require a note (construction
legitimately bounces — a job pushed to closeout can return to active work — and
the note keeps the reversal explainable). Completion is not a stage: like the
bidding abandon marker, "Mark complete" flips pm_completed_at and preserves
pm_stage='closeout'.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services.notifications import audit, notify_role


@dataclass(frozen=True)
class PMStageDef:
    key: str
    order: int
    label: str


PM_STAGES: dict[str, PMStageDef] = {
    "precon":              PMStageDef("precon",              1, "Preconstruction"),
    "active_construction": PMStageDef("active_construction", 2, "Active Construction"),
    "closeout":            PMStageDef("closeout",            3, "Closeout"),
}

# Legal FORWARD edges (adjacent only). Backward moves are handled separately in
# transition_pm_project — any earlier stage, note required — so bad data can be
# walked back without polluting this map.
PM_TRANSITIONS: dict[str, set[str]] = {
    "precon":              {"active_construction"},
    "active_construction": {"closeout"},
    "closeout":            set(),
}


def _pm_row(project_id: str) -> dict:
    row = (
        get_supabase()
        .table("projects")
        .select("id, name, pm_stage, pm_completed_at")
        .eq("id", project_id)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return row[0]


def append_pm_stage_event(
    project_id: str,
    from_stage: str | None,
    to_stage: str,
    actor_id: str | None,
    note: str | None = None,
) -> None:
    get_supabase().table("pm_stage_events").insert(
        {
            "project_id": project_id,
            "from_stage": from_stage,
            "to_stage": to_stage,
            "actor_id": actor_id,
            "note": note,
        }
    ).execute()


def transition_pm_project(
    project_id: str, to_stage: str, actor_id: str, note: str | None = None
) -> dict:
    """Move a PM project to `to_stage`, validating the move and logging it.

    Returns the updated project row. 404 unknown project; 409 not in PM /
    completed / same stage / non-adjacent forward move / concurrent move;
    400 unknown stage or backward move without a note.
    """
    proj = _pm_row(project_id)
    from_stage = proj.get("pm_stage")
    if from_stage is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is not in Project Management")
    if proj.get("pm_completed_at"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Project is marked complete — stages can no longer change"
        )
    if to_stage not in PM_STAGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown PM stage '{to_stage}'")
    if to_stage == from_stage:
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is already in that stage")

    backward = PM_STAGES[to_stage].order < PM_STAGES[from_stage].order
    if backward:
        if not (note or "").strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Moving a project back a stage requires a note explaining why",
            )
    elif to_stage not in PM_TRANSITIONS[from_stage]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Illegal PM transition {from_stage} → {to_stage}",
        )

    # Optimistic lock: only move if the project is still on the stage we read, so
    # a racing double-submit can't double-apply the move / duplicate the event.
    updated = (
        get_supabase()
        .table("projects")
        .update({"pm_stage": to_stage})
        .eq("id", project_id)
        .eq("pm_stage", from_stage)
        .execute()
    ).data
    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The project's PM stage changed — reload and retry"
        )

    append_pm_stage_event(project_id, from_stage, to_stage, actor_id, note)
    audit(
        actor_id,
        "pm.stage_change",
        "project",
        project_id,
        {"from": from_stage, "to": to_stage, "note": note},
    )
    direction = "moved back to" if backward else "moved to"
    notify_role(
        Role.EXECUTIVE,
        project_id,
        "pm_stage_change",
        f"{proj['name']} {direction} {PM_STAGES[to_stage].label}",
    )
    return updated[0]


def complete_pm_project(project_id: str, actor_id: str) -> dict:
    """Mark a Closeout project complete (the PM end state). Preserves pm_stage —
    the marker pattern the bidding abandon uses — so history keeps its shape."""
    proj = _pm_row(project_id)
    if proj.get("pm_stage") != "closeout":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a project in Closeout can be marked complete"
        )
    if proj.get("pm_completed_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is already marked complete")

    now = datetime.now(timezone.utc).isoformat()
    updated = (
        get_supabase()
        .table("projects")
        .update({"pm_completed_at": now, "pm_completed_by": actor_id})
        .eq("id", project_id)
        .eq("pm_stage", "closeout")
        .is_("pm_completed_at", "null")
        .execute()
    ).data
    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The project changed underneath you — reload and retry"
        )

    audit(actor_id, "pm.complete", "project", project_id, {})
    notify_role(
        Role.EXECUTIVE,
        project_id,
        "pm_stage_change",
        f"{proj['name']} was marked complete",
    )
    return updated[0]
