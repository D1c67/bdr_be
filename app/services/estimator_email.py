"""Branded G3 emails for the estimator hand-off.

Two emails live here:
- the **file package** — every file the estimator works from (initial drawings
  and specifications, plus any Changes/Revisions and Additional files already
  sent). Sent automatically to a newly-assigned estimator, or re-sent manually
  to all active assignees.
- the **file updates** email — the not-yet-sent Changes/Revisions and
  Additional files (each with its required note) plus an optional message,
  sent to every active assignee.

Files are linked as short-TTL signed URLs, never attached bytes (same rationale
as the RFQ/proposal senders: estimator plan sets routinely exceed attachment
limits). Sections keep initial files visually distinct from post-hand-off
updates so a later-assigned estimator can tell which is which.
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

# Category -> section title, in display order. Initial files first, then the
# post-hand-off updates.
SECTION_TITLES: list[tuple[str, str]] = [
    ("drawing", "Electrical drawings"),
    ("specification", "Specifications"),
    ("revision", "Changes/Revisions"),
    ("additional", "Additional files"),
]
INITIAL_TAG = "Initial files"
UPDATE_TAG = "Sent after hand-off"
_INITIAL = {"drawing", "specification"}

PORTAL_LINE_PACKAGE = "Please upload your Estimate, BOQ, and markups via the BDR portal."
PORTAL_LINE_UPDATES = (
    "Please review these against your estimate — the full file package is in the BDR portal."
)


def graph_configured() -> bool:
    """True when Microsoft Graph creds exist (mirrors invite_email's self-gate).
    False locally and in tests, where the caller skips the email instead."""
    return bool(get_settings().ms_client_id)


def updates_label(files: list[dict]) -> str:
    """Human label for what an updates send contains — drives the subject and
    heading ("whichever were sent")."""
    cats = {f["category"] for f in files}
    has_rev = "revision" in cats
    has_add = "additional" in cats
    if has_rev and has_add:
        return "Changes/Revisions & Additional files"
    if has_add:
        return "Additional files"
    return "Changes/Revisions"


def _greeting(recipient_name: str | None) -> str:
    first = (recipient_name or "").strip().split(" ")[0]
    return f"Hi {html.escape(first)}," if first else "Hi there,"


def _tag_pill(tag: str) -> str:
    return (
        f'<span style="display:inline-block;margin-left:8px;padding:2px 9px;'
        f"border:1px solid {_BORDER};border-radius:999px;font-size:11px;"
        f'font-weight:bold;color:{_MUTED};background-color:#f5f6f9;">'
        f"{html.escape(tag)}</span>"
    )


def _file_item(f: dict, url: str) -> str:
    safe_url = html.escape(url, quote=True)
    item = (
        f'<li style="margin:0 0 10px;"><a href="{safe_url}" '
        f'style="color:{_NAVY};font-weight:bold;">{html.escape(f["filename"])}</a>'
    )
    if f.get("note"):
        item += (
            f'<br><span style="font-size:13px;color:{_MUTED};">'
            f"Note: {html.escape(f['note'])}</span>"
        )
    return item + "</li>"


def render_sections(files: list[dict], signer) -> str:
    """The grouped file lists: one titled section per category, initial files
    tagged apart from post-hand-off updates. `signer(storage_path) -> url` is
    injected so rendering stays pure and testable."""
    out: list[str] = []
    for category, title in SECTION_TITLES:
        group = [f for f in files if f["category"] == category]
        if not group:
            continue
        tag = INITIAL_TAG if category in _INITIAL else UPDATE_TAG
        out.append(
            f'<div style="margin:18px 0 8px;font-size:15px;font-weight:bold;'
            f'color:{_NAVY};">{html.escape(title)}{_tag_pill(tag)}</div>'
            f'<ul style="margin:0;padding-left:20px;">'
            + "".join(_file_item(f, signer(f["storage_path"])) for f in group)
            + "</ul>"
        )
    return "".join(out)


def _message_block(message: str | None) -> str:
    if not (message or "").strip():
        return ""
    return (
        f'<div style="margin:0 0 16px;padding:12px 16px;border-left:3px solid {_NAVY};'
        f'background-color:#f5f6f9;border-radius:0 8px 8px 0;">'
        f'<div style="font-size:12px;font-weight:bold;color:{_MUTED};'
        f'letter-spacing:1px;">MESSAGE FROM THE G3 TEAM</div>'
        f'<div style="padding-top:4px;">'
        f"{html.escape(message.strip()).replace(chr(10), '<br>')}</div></div>"
    )


def render_package_email(
    *,
    proj: dict,
    files: list[dict],
    recipient_name: str | None,
    signer,
) -> str:
    """The full-package email body: greeting, project intro with the due-back
    date, and every file grouped by section."""
    due = proj.get("due_from_estimator_at") or "TBD"
    intro = (
        f'<p style="margin:0 0 14px;">Project '
        f"<b>{html.escape(proj['name'])}</b> ({html.escape(proj['number'])}) "
        f"is ready for estimating.</p>"
        f'<p style="margin:0 0 6px;">Due back from estimator: <b>{html.escape(str(due))}</b></p>'
    )
    body = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">{_greeting(recipient_name)}</p>'
        + intro
        + render_sections(files, signer)
        + f'<p style="margin:18px 0 0;">{html.escape(PORTAL_LINE_PACKAGE)}</p>'
    )
    return render_branded_html(body, subtitle="ESTIMATE FILES")


def render_updates_email(
    *,
    proj: dict,
    files: list[dict],
    message: str | None,
    signer,
) -> str:
    """The Changes/Revisions & Additional files email body: optional message
    from the team, then the new files with their notes."""
    label = updates_label(files)
    intro = (
        f'<p style="margin:0 0 14px;"><b>{html.escape(label)}</b> for project '
        f"<b>{html.escape(proj['name'])}</b> ({html.escape(proj['number'])}).</p>"
    )
    body = (
        '<p style="margin:0 0 14px;color:' + _MUTED + ';">Hi there,</p>'
        + intro
        + _message_block(message)
        + render_sections(files, signer)
        + f'<p style="margin:18px 0 0;">{html.escape(PORTAL_LINE_UPDATES)}</p>'
    )
    return render_branded_html(body, subtitle="FILE UPDATES")


# Emailed links live much longer than the app's default 15-minute signed URLs:
# an external estimator opens the package email hours or days after the send.
# 72h balances that against these being bearer URLs; the portal remains the
# durable access path once a link lapses.
EMAIL_LINK_TTL_SECONDS = 72 * 3600


def _email_signer(storage_path: str) -> str:
    # use_cache=False: emailed links must carry the full TTL, never a
    # partially-spent memoized URL.
    return storage.signed_url(storage_path, EMAIL_LINK_TTL_SECONDS, use_cache=False)


def send_package(
    *,
    proj: dict,
    to: list[str],
    files: list[dict],
    recipient_name: str | None = None,
    sent_by: str | None = None,
) -> dict:
    """Email the full file package; returns the email_log row."""
    body_html = render_package_email(
        proj=proj, files=files, recipient_name=recipient_name, signer=_email_signer
    )
    return graph_email.send_mail(
        to=to,
        subject=f"[BDR] Estimate request — {proj['name']} ({proj['number']})",
        body_html=body_html,
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        project_id=proj["id"],
        sent_by=sent_by,
    )


def send_updates(
    *,
    proj: dict,
    to: list[str],
    files: list[dict],
    message: str | None = None,
    sent_by: str | None = None,
) -> dict:
    """Email the pending Changes/Revisions & Additional files; returns the
    email_log row."""
    body_html = render_updates_email(proj=proj, files=files, message=message, signer=_email_signer)
    return graph_email.send_mail(
        to=to,
        subject=f"[BDR] {updates_label(files)} — {proj['name']} ({proj['number']})",
        body_html=body_html,
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        project_id=proj["id"],
        sent_by=sent_by,
    )
