"""Workflow state machine — the spine of the bidding pipeline.

Defines the ordered stages, which role owns each stage, the legal transitions,
and a helper that performs a transition while appending a `stage_events` row
(the source of truth for time-in-stage analytics).
"""

import logging
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import notifications

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageDef:
    key: str
    order: int
    owner_roles: tuple[Role, ...]
    label: str


# The 10-step pipeline plus terminal states. `order` drives the stepper UI;
# terminal stages share/extend the ordering but are not "advanced past".
STAGES: dict[str, StageDef] = {
    "intake":            StageDef("intake",            1, (Role.PA,), "Intake"),
    "go_no_go":          StageDef("go_no_go",          2, (Role.PM, Role.PA, Role.EXECUTIVE), "Go / No-Go"),
    "to_estimator":      StageDef("to_estimator",      3, (Role.PA, Role.PM), "To Estimator"),
    "estimate_received": StageDef("estimate_received", 4, (Role.ESTIMATOR, Role.PE), "Estimate Received"),
    "rfqs":              StageDef("rfqs",              5, (Role.PE,), "RFQs"),
    "receive_quotes":    StageDef("receive_quotes",    6, (Role.PE,), "Receive Quotes"),
    "labor_numbers":     StageDef("labor_numbers",     7, (Role.PM,), "Labor Numbers"),
    "markup":            StageDef("markup",            8, (Role.PM,), "Markup"),
    "verify":            StageDef("verify",            9, (Role.EXECUTIVE, Role.PM), "Verify"),
    "send_out":          StageDef("send_out",          10, (Role.PA, Role.PM), "Send Out"),
    # Submitted is a resting state (bid is out, awaiting the award decision); the
    # outstanding task is now the PA recording the Win/Loss outcome, so the PA owns it.
    "submitted":         StageDef("submitted",         11, (Role.PA,), "Submitted"),
    "bid_outcome":       StageDef("bid_outcome",       12, (Role.PA,), "Win / Loss"),
    "declined":          StageDef("declined",          99, (), "Declined"),
}

# Allowed forward transitions. Linear pipeline; go_no_go can also decline.
TRANSITIONS: dict[str, set[str]] = {
    "intake":            {"go_no_go"},
    "go_no_go":          {"to_estimator", "declined"},
    "to_estimator":      {"estimate_received"},
    "estimate_received": {"rfqs"},
    "rfqs":              {"receive_quotes"},
    "receive_quotes":    {"labor_numbers"},
    "labor_numbers":     {"markup"},
    "markup":            {"verify"},
    "verify":            {"send_out"},
    "send_out":          {"submitted"},
    "submitted":         {"bid_outcome"},
    "bid_outcome":       set(),
    "declined":          set(),
}


# Auto-dismissal of stage-gated notifications. Each entry maps a notification
# type (exact) or `due.<kind>.` prefix to the stage during which it is still a
# pending task; once a project advances PAST that stage (new order > the listed
# stage's order) the notification is stale and is dismissed. Types a router
# creates *after* transition_project() returns (verified, submitted, gono_go,
# bid_outcome, …) survive their own transition because the sweep runs before
# they exist, and die at the next one.
#
# Deliberately NOT stage-gated (dismissed elsewhere, or never):
#   stage_handoff  — special-cased in the sweep (only the latest one is current)
#   quote.received / rfq.reply_received — per-RFQ when priced (routers/rfqs.py);
#       late quotes must still notify after the stage advances
#   estimator_note — per-user when read (routers/notes.py)
#   assigned       — per-estimator on revoke (routers/estimator.py); the entry
#       below is only a stage backstop so a stale ping can't linger forever
#   proposal_send_failed — cleared on a successful resend (services/proposal_send);
#       the entry below is a backstop once the bid is submitted
#   bid_outcome / security_alert — terminal / not project-scoped; kept until read
_STAGE_DISMISS_TYPES: dict[str, str] = {
    # notification type   : pending-through stage key
    "gono_go":              "to_estimator",
    "assigned":             "estimate_received",
    "estimate_submitted":   "estimate_received",
    "drawing_changed":      "verify",
    "verified":             "send_out",
    "submitted":            "submitted",
    "proposal_send_failed": "send_out",
}
_STAGE_DISMISS_PREFIXES: dict[str, str] = {
    # due.<kind>. prefix       : pending-through stage key
    "due.due_from_estimator.":  "to_estimator",
    "due.due_from_vendors.":    "receive_quotes",
    "due.internal_bid.":        "send_out",
    "due.actual_bid.":          "send_out",
}


def _dismiss_stale_notifications(project_id: str, new_stage: str) -> None:
    """Dismiss notifications whose task is finished now the project reached
    `new_stage`. Best-effort cleanup — must never roll back a committed
    transition, so failures are logged and swallowed."""
    new_order = STAGES[new_stage].order
    # stage_handoff is always dismissed: only the newest handoff is relevant, and
    # the advance router creates it *after* this returns, so it isn't touched.
    types = ["stage_handoff"]
    types += [t for t, s in _STAGE_DISMISS_TYPES.items() if new_order > STAGES[s].order]
    prefixes = [p for p, s in _STAGE_DISMISS_PREFIXES.items() if new_order > STAGES[s].order]
    try:
        notifications.dismiss_notifications(
            project_id=project_id, types=types, type_prefixes=prefixes or None
        )
    except Exception:  # noqa: BLE001 — cleanup must not break the transition
        logger.exception("Notification dismissal failed for project %s", project_id)


def can_transition(from_stage: str, to_stage: str) -> bool:
    return to_stage in TRANSITIONS.get(from_stage, set())


def owner_role_for(stage: str) -> Role | None:
    defn = STAGES.get(stage)
    return defn.owner_roles[0] if defn and defn.owner_roles else None


def internal_owner_role_for(stage: str) -> Role | None:
    """The first INTERNAL role that owns `stage`.

    Used to address a stage-handoff notification to a real team inbox. It skips
    the estimator, who co-owns `estimate_received` only for access — broadcasting
    a handoff to every estimator would leak the project to unassigned external
    accounts. For every other stage the first owner is already internal, so this
    returns the same role as `owner_role_for`.
    """
    defn = STAGES.get(stage)
    if not defn:
        return None
    for role in defn.owner_roles:
        if role in INTERNAL_ROLES:
            return role
    return None


def transition_project(
    project_id: str, to_stage: str, actor_id: str | None, note: str | None = None
) -> dict:
    """Advance a project to `to_stage`, validating the transition and logging it.

    Returns the updated project row. Raises 409 if the transition is illegal.
    """
    sb = get_supabase()
    proj = (
        sb.table("projects")
        .select("id, current_stage")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    from_stage = proj["current_stage"]
    if to_stage not in STAGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown stage '{to_stage}'")
    if not can_transition(from_stage, to_stage):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Illegal transition {from_stage} → {to_stage}",
        )

    # Append the event first (analytics source of truth), then update the project.
    sb.table("stage_events").insert(
        {
            "project_id": project_id,
            "from_stage": from_stage,
            "to_stage": to_stage,
            "actor_id": actor_id,
            "note": note,
        }
    ).execute()

    updated = (
        sb.table("projects")
        .update(
            {
                "current_stage": to_stage,
                "current_owner_role": owner_role_for(to_stage),
            }
        )
        .eq("id", project_id)
        .execute()
    ).data

    # The task that the old stage's notifications were nagging about is now done.
    _dismiss_stale_notifications(project_id, to_stage)

    return updated[0] if updated else proj
