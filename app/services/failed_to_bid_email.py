"""Team-wide email when a bid is marked Failed to bid.

A missed bid is news for everyone in the building, so POST
/projects/{id}/failed-to-bid emails every active internal user (INTERNAL_ROLES,
the read-only accountant included; never the external estimator) one short
G3-branded message naming the project, who recorded the miss, the stage it
was at, the reason and the explanation they typed.

Same fire-and-forget contract as bid_date_change_email: the queue call hands
off to a daemon thread, self-gates on `notification_emails_enabled` + Graph
creds (tests force the flag off), and each recipient send is isolated so one
bad address never stops the rest and an email outage never fails the action.
Each send is project-scoped, so it lands in the per-project notification log
through email_log.
"""

import logging
import threading

from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES
from app.core.supabase_client import get_supabase
from app.services import graph_email
from app.services.bid_date_change_email import _actor_label, _project_label, _project_tag
from app.services.email_branding import (
    LOGO_CONTENT_ID,
    LOGO_FILENAME,
    logo_bytes,
    render_notification_email,
)

logger = logging.getLogger(__name__)

RECIPIENT_ROLES = INTERNAL_ROLES

# Mirrors schemas.FailedToBidReason and the frontend reason picker.
REASON_LABELS: dict[str, str] = {
    "missed_deadline": "Missed the bid deadline",
    "late_plans": "Plans or documents arrived too late",
    "missing_quotes": "Vendor quotes not received in time",
    "estimate_not_finished": "Estimate or takeoff not finished",
    "capacity": "Not enough team capacity",
    "submission_problem": "Problem submitting the bid",
    "other": "Other",
}


def subject_for(project: dict | None) -> str:
    tag = _project_tag(project)
    return f"Failed to bid: {tag}" if tag else "Failed to bid"


def render_failed_to_bid_email(
    *,
    recipient_name: str | None,
    project: dict | None,
    stage_label: str | None,
    reason: str,
    note: str,
    marked_by: str | None,
    cta_url: str,
) -> str:
    """One sentence saying what happened and who recorded it, then the stage,
    reason and explanation. Plain text goes through the shared notification
    renderer, which escapes it."""
    tag = _project_tag(project) or "this project"
    who = f" by {marked_by}" if marked_by else ""
    lines = [f"{tag} was marked as Failed to bid{who}.", ""]
    if stage_label:
        lines.append(f"Stage: {stage_label}")
    lines.append(f"Reason: {REASON_LABELS.get(reason, reason)}")
    lines += ["", "Why we failed to bid:", note]
    return render_notification_email(
        recipient_name=recipient_name,
        heading="Failed to bid",
        message="\n".join(lines),
        cta_label="Open project",
        cta_url=cta_url,
        project_label=_project_label(project),
    )


def queue_failed_to_bid(
    project_id: str, stage_label: str | None, reason: str, note: str, actor_id: str | None
) -> None:
    """Schedule the email for every RECIPIENT_ROLES user. No-op (and never
    raises) when notification emails are disabled or Graph is missing."""
    settings = get_settings()
    if not settings.notification_emails_enabled or not settings.ms_client_id:
        return
    threading.Thread(
        target=_run, args=(project_id, stage_label, reason, note, actor_id), daemon=True
    ).start()


def _run(
    project_id: str, stage_label: str | None, reason: str, note: str, actor_id: str | None
) -> None:
    try:
        sb = get_supabase()
        project = (
            sb.table("projects")
            .select("id, name, number")
            .eq("id", project_id)
            .single()
            .execute()
        ).data or {}
        recipients = (
            sb.table("profiles")
            .select("id, full_name, email")
            .in_("role", [r.value for r in RECIPIENT_ROLES])
            .eq("is_active", True)
            .execute()
        ).data or []
        marked_by = _actor_label(sb, actor_id)
        cta_url = f"{get_settings().frontend_url.rstrip('/')}/projects/{project_id}"
        subject = subject_for(project)
    except Exception:  # noqa: BLE001 - background work must never crash the worker
        logger.exception("Failed-to-bid email setup failed (project=%s)", project_id)
        return

    for r in recipients:
        if not r.get("email"):
            continue
        try:
            html_body = render_failed_to_bid_email(
                recipient_name=r.get("full_name"),
                project=project,
                stage_label=stage_label,
                reason=reason,
                note=note,
                marked_by=marked_by,
                cta_url=cta_url,
            )
            graph_email.send_mail(
                to=[r["email"]],
                subject=subject,
                body_html=html_body,
                inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
                project_id=project_id,
                sent_by=actor_id,
            )
        except Exception:  # noqa: BLE001 - one bad recipient never stops the rest
            logger.exception(
                "Failed-to-bid email failed (project=%s user=%s)", project_id, r.get("id")
            )
