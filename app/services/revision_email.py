"""High-importance branded alert for estimator revision rounds.

When the estimator sends a round of changed/additional deliverables (round ≥ 2),
every user in CHANGE_REVIEW_ROLES gets one personalized G3-branded email —
flagged high importance at the Graph level (red "!" in Outlook) and carrying a
red banner in the body — listing exactly which files changed, per type, with a
deep-link into the project.

Same fire-and-forget contract as notification_email: `queue_revision_alert`
hands off to a daemon thread, self-gates on `notification_emails_enabled` +
Graph creds (tests force the flag off), and every recipient send is isolated so
one bad address never stops the rest — and an email outage never fails the
estimator's submit.
"""

import html
import logging
import threading

from app.core.config import get_settings
from app.core.roles import CHANGE_REVIEW_ROLES
from app.core.supabase_client import get_supabase
from app.services import graph_email
from app.services.email_branding import (
    LOGO_CONTENT_ID,
    LOGO_FILENAME,
    _button,
    _FONT,
    _MUTED,
    _NAVY,
    _RED,
    logo_bytes,
    render_branded_html,
)

logger = logging.getLogger(__name__)

# Category -> section label, in display order (mirrors estimator_email's
# SECTION_TITLES for the opposite direction).
SECTION_LABELS: list[tuple[str, str]] = [
    ("estimate", "Revised estimate"),
    ("boq", "Revised BOQ"),
    ("markup", "Revised markups"),
    ("estimator_additional", "Additional files"),
]

URGENCY_LINE = (
    "Please review promptly — pricing steps that already used the earlier "
    "files may need to be reprocessed."
)


def _greeting(recipient_name: str | None) -> str:
    first = (recipient_name or "").strip().split(" ")[0]
    return f"Hi {html.escape(first)}," if first else "Hi there,"


def _banner() -> str:
    """The visible high-importance marker — a solid red band above the heading.
    Table-based so it renders in Outlook; red is deliberate here (the one
    place the brand's minimal-red rule steps aside for urgency)."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 16px;"><tr>'
        f'<td style="background-color:{_RED};border-radius:6px;padding:10px 16px;'
        f'{_FONT}font-size:13px;font-weight:bold;letter-spacing:1px;color:#ffffff;">'
        "HIGH IMPORTANCE — ESTIMATOR SENT CHANGES</td></tr></table>"
    )


def _sections(files: list[dict]) -> str:
    out: list[str] = []
    for category, label in SECTION_LABELS:
        group = [f for f in files if f["category"] == category]
        if not group:
            continue
        out.append(
            f'<div style="margin:14px 0 6px;font-size:15px;font-weight:bold;'
            f'color:{_NAVY};">{html.escape(label)}</div>'
            '<ul style="margin:0;padding-left:20px;">'
            + "".join(
                f'<li style="margin:0 0 6px;">{html.escape(f["filename"] or "file")}</li>'
                for f in group
            )
            + "</ul>"
        )
    return "".join(out)


def render_revision_alert(
    *,
    recipient_name: str | None,
    proj: dict,
    round_no: int,
    files: list[dict],
    cta_url: str,
) -> str:
    intro = (
        f'<p style="margin:0 0 14px;">The estimator sent '
        f"<b>changes/revisions (round {round_no})</b> for project "
        f"<b>{html.escape(proj.get('name') or '')}</b> "
        f"({html.escape(proj.get('number') or '')}).</p>"
    )
    body = (
        _banner()
        + f'<p style="margin:0 0 14px;color:{_MUTED};">{_greeting(recipient_name)}</p>'
        + intro
        + _sections(files)
        + f'<p style="margin:18px 0 14px;">{html.escape(URGENCY_LINE)}</p>'
        + _button("Review changes", cta_url)
    )
    return render_branded_html(body, subtitle="ESTIMATOR FILE CHANGES")


def queue_revision_alert(project_id: str, round_no: int, files: list[dict]) -> None:
    """Schedule the alert for every CHANGE_REVIEW_ROLES recipient. No-op (and
    never raises) when notification emails are disabled or Graph is missing."""
    settings = get_settings()
    if not settings.notification_emails_enabled or not settings.ms_client_id:
        return
    threading.Thread(
        target=_run, args=(project_id, round_no, files), daemon=True
    ).start()


def _run(project_id: str, round_no: int, files: list[dict]) -> None:
    try:
        sb = get_supabase()
        proj = (
            sb.table("projects")
            .select("id, name, number")
            .eq("id", project_id)
            .single()
            .execute()
        ).data or {}
        recipients = (
            sb.table("profiles")
            .select("id, full_name, email")
            .in_("role", [r.value for r in CHANGE_REVIEW_ROLES])
            .eq("is_active", True)
            .execute()
        ).data or []
        cta_url = f"{get_settings().frontend_url.rstrip('/')}/projects/{project_id}"
        number, name = proj.get("number") or "", proj.get("name") or ""
        subject = (
            f"G3 BDR · [HIGH IMPORTANCE] Estimator changes (round {round_no}) — "
            f"#{number} {name}".rstrip()
        )
    except Exception:  # noqa: BLE001 — background work must never crash the worker
        logger.exception("Revision alert setup failed (project=%s)", project_id)
        return

    for r in recipients:
        if not r.get("email"):
            continue
        try:
            html_body = render_revision_alert(
                recipient_name=r.get("full_name"),
                proj=proj,
                round_no=round_no,
                files=files,
                cta_url=cta_url,
            )
            graph_email.send_mail(
                to=[r["email"]],
                subject=subject,
                body_html=html_body,
                inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
                project_id=project_id,
                importance="high",
            )
        except Exception:  # noqa: BLE001 — one bad recipient never stops the rest
            logger.exception(
                "Revision alert failed (project=%s user=%s)", project_id, r.get("id")
            )
