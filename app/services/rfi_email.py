"""Branded G3 email that delivers an RFI to the GC.

The email itself is a short cover note — the RFI content travels as an attached
PDF (rfi_pdf.render_pdf), which is byte-identical to the in-app "View RFI" form
and is also archived in the project's Documents. Any files the RFI references
(its request attachments) ride along as short-TTL signed download links rather
than attached bytes, matching the estimator/RFQ senders (plan sets routinely
exceed attachment limits). Sends are recorded in email_log by graph_email.
"""

import html

from app.core.config import get_settings
from app.services import graph_email, storage
from app.services.email_branding import (
    LOGO_CONTENT_ID,
    LOGO_FILENAME,
    _BORDER,
    _MUTED,
    _NAVY,
    logo_bytes,
    render_branded_html,
)

# Linked files outlive the app's 15-minute default: a GC opens the RFI email
# hours or days later. Mirrors estimator_email's rationale; the BDR portal stays
# the durable path once a link lapses.
LINK_TTL_SECONDS = 72 * 3600


def graph_configured() -> bool:
    """True when Microsoft Graph creds exist (mirrors the other senders' self-gate);
    False locally and in tests, where the caller returns a clear error instead."""
    return bool(get_settings().ms_client_id)


def signed_link(storage_path: str) -> str:
    # use_cache=False so the emailed link carries the full TTL, never a
    # partially-spent memoized URL.
    return storage.signed_url(storage_path, LINK_TTL_SECONDS, use_cache=False)


def _message_block(message: str | None) -> str:
    if not (message or "").strip():
        return ""
    return (
        f'<div style="margin:0 0 16px;padding:12px 16px;border-left:3px solid {_NAVY};'
        f'background-color:#f5f6f9;border-radius:0 8px 8px 0;">'
        f'<div style="font-size:12px;font-weight:bold;color:{_MUTED};letter-spacing:1px;">'
        f"MESSAGE FROM THE G3 TEAM</div>"
        f'<div style="padding-top:4px;">'
        f"{html.escape(message.strip()).replace(chr(10), '<br>')}</div></div>"
    )


def _links_block(attachment_links: list[tuple[str, str]]) -> str:
    if not attachment_links:
        return ""
    items = "".join(
        f'<li style="margin:0 0 8px;">'
        f'<a href="{html.escape(url, quote=True)}" style="color:{_NAVY};font-weight:bold;">'
        f"{html.escape(name)}</a></li>"
        for name, url in attachment_links
    )
    return (
        f'<div style="margin:18px 0 4px;font-size:14px;font-weight:bold;color:{_NAVY};">'
        f"Referenced files</div>"
        f'<ul style="margin:0;padding-left:20px;">{items}</ul>'
        f'<p style="margin:6px 0 0;font-size:12px;color:{_MUTED};">'
        f"These links expire in 72 hours.</p>"
    )


def render_email(
    *,
    project: dict,
    rfi: dict,
    message: str | None,
    attachment_links: list[tuple[str, str]],
) -> str:
    rfi_no = str(rfi.get("rfi_number") or "").zfill(3)
    due = rfi.get("due_at")
    urgent = rfi.get("priority") == "urgent"
    intro = (
        f'<p style="margin:0 0 14px;">Please find attached <b>RFI {rfi_no}</b>'
        f" — {html.escape(rfi.get('subject') or '')} — for project "
        f"<b>{html.escape(project.get('name') or '')}</b> "
        f"({html.escape(str(project.get('number') or ''))}).</p>"
    )
    meta_bits = []
    if urgent:
        meta_bits.append('<b style="color:#951e2d;">Priority: Urgent</b>')
    if due:
        from app.services.rfi_pdf import _fmt_day

        meta_bits.append(f"Response needed by <b>{html.escape(_fmt_day(due))}</b>")
    meta = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">{" &middot; ".join(meta_bits)}</p>'
        if meta_bits
        else ""
    )
    body = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">Hi there,</p>'
        + intro
        + meta
        + _message_block(message)
        + '<p style="margin:0 0 8px;">The full request is in the attached RFI form. '
        "Please reply with your response at your earliest convenience.</p>"
        + _links_block(attachment_links)
    )
    return render_branded_html(body, subtitle="REQUEST FOR INFORMATION")


def send_rfi_email(
    *,
    to: list[str],
    project: dict,
    rfi: dict,
    message: str | None,
    pdf_name: str,
    pdf_bytes: bytes,
    attachment_links: list[tuple[str, str]],
    sent_by: str | None = None,
) -> dict:
    """Email the RFI (attached as a PDF) to `to`; returns the email_log row.

    Raises on send failure (graph_email records the failure in email_log first),
    so the caller records nothing and the RFI is not marked sent.
    """
    rfi_no = str(rfi.get("rfi_number") or "").zfill(3)
    subject = (
        f"[G3] RFI {rfi_no} — {rfi.get('subject') or ''} — "
        f"{project.get('name') or ''} ({project.get('number') or ''})"
    )
    body_html = render_email(
        project=project, rfi=rfi, message=message, attachment_links=attachment_links
    )
    return graph_email.send_mail(
        to=to,
        subject=subject,
        body_html=body_html,
        attachments=[(pdf_name, pdf_bytes)],
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        project_id=project.get("id"),
        sent_by=sent_by,
        importance="high" if rfi.get("priority") == "urgent" else None,
    )
