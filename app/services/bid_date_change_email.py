"""Team-wide email when a project's INTERNAL bid date changes.

The internal bid date is the deadline the whole team works against (the
dashboard, Bids Today, the due reminders and the daily digest all key off it),
so moving it is news for everyone who works the bid. When PATCH /projects
changes the value, every active internal user except the read-only accountant
gets one short, personalized G3-branded email whose subject states the new
date outright, so the change is legible from the inbox list alone:

    Internal bid date changed to Wednesday, June 24th 11:00 AM PDT for #26-014 Van Ness Tower

Same fire-and-forget contract as notification_email / revision_email:
`queue_internal_bid_date_change` hands off to a daemon thread, self-gates on
`notification_emails_enabled` + Graph creds (tests force the flag off), and
each recipient send is isolated so one bad address never stops the rest, and
an email outage never fails the edit that triggered it. No bell row is
created: the date itself is visible on every project surface, and the email
lands in the per-project notification log through email_log like any other
project-scoped send.
"""

import logging
import threading
from datetime import datetime

from app.core.config import get_settings
from app.core.roles import WRITER_ROLES
from app.core.supabase_client import get_supabase
from app.services import graph_email
from app.services.datetime_format import _parse_ts, format_bid_datetime
from app.services.email_branding import (
    LOGO_CONTENT_ID,
    LOGO_FILENAME,
    logo_bytes,
    render_notification_email,
)

logger = logging.getLogger(__name__)

# Every internal user except the accountant. WRITER_ROLES is exactly that set
# (INTERNAL_ROLES minus the read-only accountant); the external estimator is
# never internal and is never told about internal dates.
RECIPIENT_ROLES = WRITER_ROLES

Stamp = datetime | str | None


def bid_date_changed(previous: Stamp, current: Stamp) -> bool:
    """True when the stored internal bid date actually moved.

    Compared as instants, so the same moment spelled two ways (`...Z` from the
    request body, `+00:00` back from Postgres) is not a change and sends
    nothing. Clearing the date, or setting one where there was none, counts."""
    return _parse_ts(previous) != _parse_ts(current)


def _project_tag(project: dict | None) -> str:
    number = (project or {}).get("number") or ""
    name = (project or {}).get("name") or ""
    if number and name:
        return f"#{number} {name}"
    return name or (f"#{number}" if number else "")


def _project_label(project: dict | None) -> str | None:
    """The chip above the heading, in the same '#number · name' shape the
    notification mirror emails use."""
    number = (project or {}).get("number") or ""
    name = (project or {}).get("name") or ""
    if number and name:
        return f"#{number} · {name}"
    return name or (f"#{number}" if number else None)


def _fmt(value: Stamp) -> str:
    return format_bid_datetime(value) if value else "none"


def subject_for(project: dict | None, current: Stamp) -> str:
    """'Internal bid date changed to <date> for #<number> <name>'. A cleared
    date reads 'removed' instead, since there is no date to state."""
    if current is None:
        head = "Internal bid date removed"
    else:
        head = f"Internal bid date changed to {format_bid_datetime(current)}"
    tag = _project_tag(project)
    return f"{head} for {tag}" if tag else head


def render_internal_bid_date_email(
    *,
    recipient_name: str | None,
    project: dict | None,
    previous: Stamp,
    current: Stamp,
    changed_by: str | None,
    cta_url: str,
) -> str:
    """The body: one sentence saying what moved and who moved it, the old and
    new dates, and a button into the project. Plain text goes through the
    shared notification renderer, which escapes it."""
    tag = _project_tag(project) or "this project"
    who = f" by {changed_by}" if changed_by else ""
    if current is None:
        heading = "Internal bid date removed"
        intro = f"The internal bid date for {tag} was removed{who}."
    else:
        heading = "Internal bid date changed"
        intro = f"The internal bid date for {tag} was changed{who}."
    message = f"{intro}\n\nPrevious: {_fmt(previous)}\nNew: {_fmt(current)}"
    return render_notification_email(
        recipient_name=recipient_name,
        heading=heading,
        message=message,
        cta_label="Open project",
        cta_url=cta_url,
        project_label=_project_label(project),
    )


def queue_internal_bid_date_change(
    project_id: str, previous: Stamp, current: Stamp, actor_id: str | None
) -> None:
    """Schedule the email for every RECIPIENT_ROLES user. No-op (and never
    raises) when notification emails are disabled or Graph is missing."""
    settings = get_settings()
    if not settings.notification_emails_enabled or not settings.ms_client_id:
        return
    threading.Thread(
        target=_run, args=(project_id, previous, current, actor_id), daemon=True
    ).start()


def _actor_label(sb, actor_id: str | None) -> str | None:
    """Who made the change, by name (email as a fallback). Best-effort: a
    failed lookup only costs the sentence its 'by ...' clause."""
    if not actor_id:
        return None
    try:
        rows = (
            sb.table("profiles")
            .select("full_name, email")
            .eq("id", actor_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve actor %s for bid-date email", actor_id, exc_info=True)
        return None
    if not rows:
        return None
    return rows[0].get("full_name") or rows[0].get("email")


def _run(project_id: str, previous: Stamp, current: Stamp, actor_id: str | None) -> None:
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
        changed_by = _actor_label(sb, actor_id)
        cta_url = f"{get_settings().frontend_url.rstrip('/')}/projects/{project_id}"
        subject = subject_for(project, current)
    except Exception:  # noqa: BLE001 - background work must never crash the worker
        logger.exception("Internal bid date email setup failed (project=%s)", project_id)
        return

    for r in recipients:
        if not r.get("email"):
            continue
        try:
            html_body = render_internal_bid_date_email(
                recipient_name=r.get("full_name"),
                project=project,
                previous=previous,
                current=current,
                changed_by=changed_by,
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
                "Internal bid date email failed (project=%s user=%s)", project_id, r.get("id")
            )
