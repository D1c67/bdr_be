"""Telling the assigned external estimator when a bid dies — or comes back.

Abandon and reactivate are internal lifecycle flips, but an external estimator
is usually mid-work when one lands: abandon shuts the portal on them
(`deps.require_project_assignment` 403s every project route) and sweeps their
bells, so without this the work would simply vanish with no word. Both
directions send the same pair — a portal bell row plus a branded email.

Two rules hold for everything in here:

- **Best-effort.** The status change is already committed by the time these run.
  A dead mailbox or a PostgREST blip must never turn a successful abandon into a
  500, so every recipient is isolated and the whole sweep is wrapped.
- **Active assignees only.** Same definition the access gate uses (not revoked,
  not expired) — an estimator whose window already lapsed lost the project long
  before this and gets no mail about it.
"""

import logging

from app.core.supabase_client import get_supabase
from app.services import estimator_email
from app.services.notifications import dismiss_notifications, notify_user

logger = logging.getLogger(__name__)

# Deliberately NOT in notifications.ESTIMATOR_NOTIFICATION_TYPES: that list is
# what abandon sweeps off the estimator's bell, and this is the one row that has
# to survive the sweep — it's the notice explaining why everything else went.
WITHDRAWN_TYPE = "project_withdrawn"
REACTIVATED_TYPE = "project_reactivated"

# The bell shows one line; a 2,000-character reason belongs in the email, not in
# a dropdown. Truncated for the bell only — the email carries the full text.
_BELL_NOTE_CAP = 240


def _active_assignees(project_id: str) -> list[dict]:
    """Active assignees with their profile email/name.

    Mirrors `estimator._active_assignments` (kept as its own copy so a service
    never imports a router); if the "active" definition changes there, change it
    here too.
    """
    return (
        get_supabase()
        .table("estimator_assignments")
        .select("estimator_id, profiles!estimator_assignments_estimator_id_fkey(email, full_name)")
        .eq("project_id", project_id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .execute()
    ).data or []


def _project_tag(project: dict) -> str:
    number, name = project.get("number"), project.get("name")
    return f"#{number} · {name}" if number and name else (name or f"#{number}")


def _bell_message(project: dict, note: str | None) -> str:
    msg = f"Project withdrawn: {_project_tag(project)} — G3 is no longer bidding it."
    text = (note or "").strip()
    if text:
        clipped = text[:_BELL_NOTE_CAP].rstrip() + "…" if len(text) > _BELL_NOTE_CAP else text
        msg += f" Reason: {clipped}"
    return msg


def notify_withdrawn(project: dict, *, note: str | None = None, actor_id: str | None = None) -> None:
    """Tell every active assignee their project was abandoned: bell + email.

    Call AFTER the abandon is persisted and after the bell sweep — the sweep
    clears the estimator-facing types, and this row must land on the cleared
    bell, not be cleared by it. Any stale "reactivated" row is dismissed first
    so the two notices can never contradict each other.
    """
    _run(project, kind="withdrawn", note=note, actor_id=actor_id)


def notify_reactivated(project: dict, *, actor_id: str | None = None) -> None:
    """Tell every active assignee the withdrawn project is live again, and clear
    the withdrawn notice that is no longer true."""
    _run(project, kind="reactivated", note=None, actor_id=actor_id)


def _run(project: dict, *, kind: str, note: str | None, actor_id: str | None) -> None:
    withdrawn = kind == "withdrawn"
    type_ = WITHDRAWN_TYPE if withdrawn else REACTIVATED_TYPE
    try:
        # The opposite notice is now a lie — drop it. Project-scoped without a
        # user filter is exactly right: only estimators ever receive these types.
        dismiss_notifications(
            project_id=project["id"],
            types=[REACTIVATED_TYPE if withdrawn else WITHDRAWN_TYPE],
        )
        assignees = _active_assignees(project["id"])
    except Exception:  # noqa: BLE001
        logger.exception("Estimator %s notice: lookup failed for %s", kind, project.get("id"))
        return

    message = (
        _bell_message(project, note)
        if withdrawn
        else f"Project reactivated: {_project_tag(project)} — it's back in your portal."
    )
    graph_ready = estimator_email.graph_configured()
    for a in assignees:
        est_id = a.get("estimator_id")
        prof = a.get("profiles") or {}
        email = (prof.get("email") or "").strip()
        try:
            if est_id:
                # mirror_email=False: the generic mirror would deep-link into the
                # project, which is precisely the page they can no longer open.
                # The branded notice below is this event's email.
                notify_user(est_id, project["id"], type_, message, mirror_email=False)
            if email and graph_ready:
                send = estimator_email.send_withdrawn if withdrawn else estimator_email.send_reactivated
                kwargs = {"note": note} if withdrawn else {}
                send(
                    proj=project,
                    to=[email],
                    recipient_name=prof.get("full_name"),
                    sent_by=actor_id,
                    **kwargs,
                )
        except Exception:  # noqa: BLE001 — one bad recipient never stops the rest
            logger.exception(
                "Estimator %s notice failed (project=%s estimator=%s)",
                kind, project.get("id"), est_id,
            )
