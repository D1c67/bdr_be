"""Branded email mirror of every in-app notification.

Whenever a notification row is created (role broadcast, single user, due-date
reminder, or estimator note), the same recipient also gets a G3-branded HTML
email with a button that deep-links straight to the page the notification is
about. The bell and the inbox stay in sync.

Design notes:
- Fire-and-forget: `queue()` hands off to a daemon thread so a notification
  never adds Graph latency to the request that created it, and a send failure
  can never break the originating flow (e.g. the due-reminder ledger rollback).
- Best-effort: every send is wrapped; one bad recipient never stops the rest.
- Self-gating: sends only when `notification_emails_enabled` is set AND Graph
  credentials exist. Tests force the flag off (tests/conftest.py), so no test
  ever spawns a thread or touches the network.

The deep link resolves by recipient role: estimators go to the estimator
portal (`/estimator/projects/{id}`), everyone else to the internal project hub
(`/projects/{id}`); a notification with no project falls back to the role's
home page.
"""

import logging
import threading

from app.core.config import get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import graph_email
from app.services.email_branding import (
    LOGO_CONTENT_ID,
    LOGO_FILENAME,
    logo_bytes,
    render_notification_email,
)

logger = logging.getLogger(__name__)

# type (or its first dotted segment) -> (email heading, button label). The
# heading is a short human title; the message body is the same text the bell
# shows. due.* matches on the "due" segment; the per-offset/expired wording
# lives in the message itself.
_TYPE_META: dict[str, tuple[str, str]] = {
    "verified": ("Pricing committed — ready to send out", "Open Send Out"),
    "stage_handoff": ("A project moved to your stage", "Open project"),
    "drawing_changed": ("Project drawings changed", "Review drawings"),
    "gono_go": ("Project accepted — send to estimator", "Open project"),
    "assigned": ("You were assigned to a project", "Open your assignment"),
    "estimate_submitted": ("Estimate submitted", "Review the estimate"),
    "rfq.reply_received": ("A vendor replied to an RFQ", "View quotes"),
    "quote.received": ("A vendor quote came in", "View quotes"),
    "bid_outcome": ("Bid outcome recorded", "Open project"),
    "submitted": ("Bid submitted", "Open project"),
    "proposal_send_failed": ("A proposal send failed", "Open Send Out"),
    "security_alert": ("Security alert", "Review activity"),
    "estimator_note": ("New note on a project", "Open the conversation"),
    "nudge": ("You've been nudged about a to-do", "Open your to-dos"),
    "due": ("Deadline approaching", "Open project"),
}

_DEFAULT_META = ("BDR notification", "Open BDR")


def _meta(type_: str) -> tuple[str, str]:
    base = _TYPE_META.get(type_) or _TYPE_META.get(type_.split(".", 1)[0], _DEFAULT_META)
    heading, cta = base
    # The due poller emits a single past-due "expired" notice — reflect that.
    if type_.startswith("due.") and type_.endswith(".expired"):
        heading = "Deadline passed"
    return heading, cta


def _deep_link(project_id: str | None, role: str | None, type_: str | None = None) -> str:
    base = get_settings().frontend_url.rstrip("/")
    is_estimator = role == Role.ESTIMATOR.value
    if project_id:
        prefix = "/estimator/projects" if is_estimator else "/projects"
        return f"{base}{prefix}/{project_id}"
    # A nudge isn't tied to a project — send the recipient straight to their list.
    if type_ == "nudge":
        return f"{base}/todos"
    return f"{base}/estimator" if is_estimator else f"{base}/dashboard"


def _project_label(project: dict | None) -> str | None:
    if not project:
        return None
    number, name = project.get("number"), project.get("name")
    if number and name:
        return f"#{number} · {name}"
    return name or (f"#{number}" if number else None)


def _subject(heading: str, project: dict | None) -> str:
    if project:
        number, name = project.get("number"), project.get("name")
        tag = f"#{number} {name}" if number and name else (name or (f"#{number}" if number else ""))
        if tag:
            return f"G3 BDR · {heading} — {tag}"
    return f"G3 BDR · {heading}"


def queue(rows: list[dict]) -> None:
    """Schedule branded emails for freshly-created notification rows.

    Each row is a dict with `user_id`, `project_id` (nullable), `type`,
    `message` — the same shape inserted into the notifications table. No-op
    (and never raises) when disabled or when Graph isn't configured.
    """
    rows = [r for r in (rows or []) if r.get("user_id")]
    if not rows:
        return
    settings = get_settings()
    if not settings.notification_emails_enabled or not settings.ms_client_id:
        return
    threading.Thread(target=_run, args=(rows,), daemon=True).start()


def _run(rows: list[dict]) -> None:
    """Resolve recipients/projects once, then send one personalized email per
    notification. Each step is isolated so a single failure can't lose the rest."""
    try:
        sb = get_supabase()
        user_ids = sorted({r["user_id"] for r in rows})
        profiles = (
            sb.table("profiles")
            .select("id, full_name, email, role, is_active")
            .in_("id", user_ids)
            .execute()
        ).data or []
        profile_by_id = {p["id"]: p for p in profiles}

        project_ids = sorted({r["project_id"] for r in rows if r.get("project_id")})
        project_by_id: dict[str, dict] = {}
        if project_ids:
            projects = (
                sb.table("projects")
                .select("id, name, number")
                .in_("id", project_ids)
                .execute()
            ).data or []
            project_by_id = {p["id"]: p for p in projects}
    except Exception:  # noqa: BLE001 — background work must never crash the worker
        logger.exception("Notification-email batch setup failed")
        return

    for row in rows:
        try:
            _send_one(row, profile_by_id.get(row["user_id"]), project_by_id.get(row.get("project_id")))
        except Exception:  # noqa: BLE001
            logger.exception(
                "Notification email failed (type=%s user=%s project=%s)",
                row.get("type"), row.get("user_id"), row.get("project_id"),
            )


def _send_one(row: dict, profile: dict | None, project: dict | None) -> None:
    if not profile or not profile.get("is_active") or not profile.get("email"):
        return  # no active recipient / no address — nothing to send
    type_ = row.get("type") or ""
    heading, cta_label = _meta(type_)
    html = render_notification_email(
        recipient_name=profile.get("full_name"),
        heading=heading,
        message=row.get("message") or "",
        cta_label=cta_label,
        cta_url=_deep_link(row.get("project_id"), profile.get("role"), type_),
        project_label=_project_label(project),
    )
    graph_email.send_mail(
        to=[profile["email"]],
        subject=_subject(heading, project),
        body_html=html,
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        project_id=row.get("project_id"),
    )
