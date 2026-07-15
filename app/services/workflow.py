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
# `owner_roles` is the "whose task" hint for each stage — it drives stage-handoff
# notifications, the dashboard "yours" markers and the TaskOwnerBanner. It NO
# LONGER hard-gates who may advance: any writer role can act on any stage (the
# advance gate enforces that), with Verify the sole exception (Executive / IT
# Admin only — see VERIFY_ROLES). The estimator co-owns `estimate_received` for
# access only.
STAGES: dict[str, StageDef] = {
    "intake":            StageDef("intake",            1, (Role.ESTIMATING_ADMIN,), "Intake"),
    "go_no_go":          StageDef("go_no_go",          2, (Role.ESTIMATING_ENGINEER, Role.ESTIMATING_ADMIN, Role.EXECUTIVE), "Go / No-Go"),
    "to_estimator":      StageDef("to_estimator",      3, (Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER), "To Estimator"),
    "estimate_received": StageDef("estimate_received", 4, (Role.ESTIMATOR, Role.ESTIMATING_ENGINEER), "Estimate Received"),
    "rfqs":              StageDef("rfqs",              5, (Role.ESTIMATING_ENGINEER,), "RFQs"),
    "receive_quotes":    StageDef("receive_quotes",    6, (Role.ESTIMATING_ENGINEER,), "Receive Quotes"),
    "labor_numbers":     StageDef("labor_numbers",     7, (Role.ESTIMATING_ENGINEER,), "Labor Numbers"),
    "markup":            StageDef("markup",            8, (Role.ESTIMATING_ENGINEER,), "Markup"),
    # GC Pricing: set the bid number per general contractor (one figure each). The
    # Estimating Engineer decides the per-GC numbers; the Executive then reviews
    # them (read-only) at Verify.
    "gc_pricing":        StageDef("gc_pricing",        9, (Role.ESTIMATING_ENGINEER, Role.EXECUTIVE), "GC Pricing"),
    "verify":            StageDef("verify",            10, (Role.EXECUTIVE,), "Verify"),
    "send_out":          StageDef("send_out",          11, (Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER), "Send Out"),
    # Submitted is a resting state (bid is out, awaiting the award decision); the
    # outstanding task is now recording the Win/Loss outcome, owned by the
    # Estimating Admin.
    "submitted":         StageDef("submitted",         12, (Role.ESTIMATING_ADMIN,), "Submitted"),
    "bid_outcome":       StageDef("bid_outcome",       13, (Role.ESTIMATING_ADMIN,), "Win / Loss"),
    "declined":          StageDef("declined",          99, (), "Declined"),
    # Placeholder for projects created directly in Project Management (never bid).
    # Terminal by construction: no TRANSITIONS edge leads into or out of it, so
    # transition_project can never move a project onto/off this stage — it exists
    # only so STAGES[...] lookups on such rows never KeyError. PM's own lifecycle
    # lives in pm_stage / services/pm_workflow.py, not here.
    "pm_only":           StageDef("pm_only",           98, (), "PM Only"),
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
    "markup":            {"gc_pricing"},
    "gc_pricing":        {"verify"},
    "verify":            {"send_out"},
    "send_out":          {"submitted"},
    "submitted":         {"bid_outcome"},
    "bid_outcome":       set(),
    "declined":          set(),
    "pm_only":           set(),
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
    # A post-verify pricing edit bounced the project back to verify; the ping is
    # done once the Executive re-commits and the project advances past verify again.
    "reverify_required":    "verify",
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


# ── Re-verification on a post-verify pricing edit ─────────────────────────────
# A pricing-affecting edit on a project that has ALREADY passed Verify must send
# it back to `verify` so the Executive re-commits the numbers they signed off on,
# then resume at the stage it was on (stored in projects.reverify_return_stage).
# These moves deliberately bypass the forward-only TRANSITIONS map (the abandon
# precedent, routers/projects.py): a re-verify is an intentional, audited backward
# move, not a pipeline edge, so adding `send_out → verify` etc. to TRANSITIONS
# would break the forward-only invariant the rest of the app and tests rely on.

_VERIFY_ORDER = STAGES["verify"].order  # 10


def reopen_verify(project_id: str, actor_id: str | None, reason: str) -> tuple[dict, bool]:
    """Bounce a project that has passed Verify back to the `verify` stage.

    No-op (returns moved=False) when the project is already at `verify` or has not
    passed it. On a real bounce it appends a `stage_events` row, flips
    `current_stage` to `verify`, records the pre-edit stage in
    `reverify_return_stage` (only on the FIRST bounce, so a second edit during
    re-verify can't lose the true return stage), and clears the committed
    verification snapshot (committed_at / verified_by) so the figures become
    editable and committable again — the override numbers are kept so the form
    re-opens pre-filled. Returns (project_row, moved).
    """
    sb = get_supabase()
    proj = (
        sb.table("projects")
        .select("id, current_stage, reverify_return_stage")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    from_stage = proj["current_stage"]
    if from_stage == "verify" or STAGES[from_stage].order <= _VERIFY_ORDER:
        return proj, False  # not past verify (or already bounced) — nothing to do

    update: dict = {
        "current_stage": "verify",
        "current_owner_role": owner_role_for("verify"),
    }
    # Set the return stage only on the first bounce; never overwrite it.
    if proj.get("reverify_return_stage") is None:
        update["reverify_return_stage"] = from_stage
    # Optimistic lock: only flip if the project is still on the stage we read, so a
    # racing concurrent edit can't double-apply the bounce (insert a duplicate
    # event / re-clear the snapshot).
    updated = (
        sb.table("projects")
        .update(update)
        .eq("id", project_id)
        .eq("current_stage", from_stage)
        .execute()
    ).data
    if not updated:
        return proj, False  # a concurrent edit already bounced it

    sb.table("stage_events").insert(
        {
            "project_id": project_id,
            "from_stage": from_stage,
            "to_stage": "verify",
            "actor_id": actor_id,
            "note": reason,
        }
    ).execute()
    sb.table("verifications").update(
        {"committed_at": None, "verified_by": None, "updated_at": "now()"}
    ).eq("project_id", project_id).execute()
    return updated[0], True


def return_from_reverify(project_id: str, return_stage: str, actor_id: str | None) -> dict:
    """After a re-verify commit, send the project back to the stage it was on
    before the bounce (`return_stage`) and clear the marker. Bypasses TRANSITIONS
    for the same reason `reopen_verify` does — `verify → submitted` / `verify →
    bid_outcome` are not legal forward edges, but the bid was already submitted /
    awarded and must not be forced back through send_out."""
    if return_stage not in STAGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown stage '{return_stage}'")
    sb = get_supabase()
    sb.table("stage_events").insert(
        {
            "project_id": project_id,
            "from_stage": "verify",
            "to_stage": return_stage,
            "actor_id": actor_id,
            "note": "Re-verify committed",
        }
    ).execute()
    updated = (
        sb.table("projects")
        .update(
            {
                "current_stage": return_stage,
                "current_owner_role": owner_role_for(return_stage),
                "reverify_return_stage": None,
            }
        )
        .eq("id", project_id)
        .execute()
    ).data
    _dismiss_stale_notifications(project_id, return_stage)
    return updated[0] if updated else {}


def maybe_reopen_verify_after_edit(
    project_id: str, actor_id: str | None, reason: str
) -> bool:
    """If a pricing-affecting edit just landed on a project that has ALREADY passed
    Verify, bounce it back to `verify` for re-commit and notify the Executive + PM.

    No-op otherwise: not yet verified → edits are fine; already at verify → nothing
    to do; declined or abandoned → skip. Best-effort — the pricing write already
    succeeded and must not be rolled back, so any failure here is logged and
    swallowed (returns False)."""
    try:
        proj = (
            get_supabase()
            .table("projects")
            .select("current_stage, abandoned_at")
            .eq("id", project_id)
            .single()
            .execute()
        ).data
        if not proj or proj.get("abandoned_at"):
            return False
        stage = proj["current_stage"]
        # pm_only guarded explicitly: its order (98) is past verify but it was
        # never in the pipeline, so there is nothing to re-verify.
        if stage in ("declined", "pm_only") or STAGES[stage].order <= _VERIFY_ORDER:
            return False  # not past verify (includes being at verify)
        _, moved = reopen_verify(project_id, actor_id, reason)
        if moved:
            message = f"A pricing-affecting edit was made after Verify — re-commit required ({reason})."
            # Past send_out means the bid is already out the door; make clear the
            # re-commit only fixes the internal record, not the sent proposal.
            if STAGES[stage].order > STAGES["send_out"].order:
                message += (
                    " The bid already sent to GCs is unchanged; this updates the "
                    "internal pricing record only."
                )
            for role in (Role.EXECUTIVE, Role.ESTIMATING_ENGINEER):
                notifications.notify_role(role, project_id, "reverify_required", message)
        return moved
    except Exception:  # noqa: BLE001 — the edit succeeded; bouncing is best-effort
        logger.exception("Re-verify bounce failed for project %s", project_id)
        return False
