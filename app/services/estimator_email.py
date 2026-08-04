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
    _button,
    _MUTED,
    _NAVY,
    logo_bytes,
    render_branded_html,
)

# (category, doc_type) -> section title, in display order. Initial files first,
# then addenda (which live on both sides of the hand-off), then the post-hand-off
# updates.
#
# The 0077 doc_type axis is what splits one "Changes/Revisions" list into revised
# PLANS and revised SPECS — the estimator prices those from different documents,
# so they must arrive as different sections, not as one pile the reader has to
# sort by filename. Each split category keeps a trailing `None` row for files
# uploaded before 0077 (and for addenda sent from the initial modal, which never
# asks): they group under the original undivided title rather than being dropped.
#
# ORDER IS THE RENDER ORDER. `doc_type` None on a SPLIT category means "no
# document set recorded", never "any".
SECTION_TITLES: list[tuple[str, str | None, str]] = [
    ("drawing", None, "General drawings/plans"),
    ("electrical_drawing", None, "Electrical drawings"),
    ("specification", None, "Specifications"),
    ("addendum", "drawing", "Addenda — plans/drawings"),
    ("addendum", "specification", "Addenda — specifications"),
    ("addendum", None, "Addenda"),
    ("revision", "drawing", "Revised plans/drawings"),
    ("revision", "specification", "Revised specifications"),
    ("revision", None, "Changes/Revisions"),
    ("additional", None, "Additional files"),
]
INITIAL_TAG = "Initial files"
UPDATE_TAG = "Sent after hand-off"
_INITIAL = {"drawing", "electrical_drawing", "specification"}
# Addenda carry no "Initial files"/"Sent after hand-off" pill: they exist in the
# initial package AND in later batches, so either tag would be wrong. Each
# addendum line shows its number + issue date, which is more informative anyway.
_NO_TAG = {"addendum"}
# Categories whose sections are keyed by (category, doc_type); mirrors
# file_categories.DOC_TYPE_CATEGORIES. Imported as a literal rather than from
# that module so this renderer stays a leaf with no app.core import.
_SPLIT_CATEGORIES = {"revision", "addendum"}

PORTAL_LINE_PACKAGE = "Please upload your Estimate, BOQ, and markups via the BDR portal."
PORTAL_LINE_UPDATES = (
    "Please review these against your estimate — the full file package is in the BDR portal."
)


def graph_configured() -> bool:
    """True when Microsoft Graph creds exist (mirrors invite_email's self-gate).
    False locally and in tests, where the caller skips the email instead."""
    return bool(get_settings().ms_client_id)


# Ordered so an addenda-only batch never mislabels as "Changes/Revisions".
# ORDER IS LOAD-BEARING — do not reorder.
_UPDATE_LABELS = [
    ("addendum", "Addenda"),
    ("revision", "Changes/Revisions"),
    ("additional", "Additional files"),
]


def updates_label(files: list[dict]) -> str:
    """Human label for what an updates send contains — drives the subject and
    heading ("whichever were sent")."""
    cats = {f["category"] for f in files}
    return " & ".join(label for c, label in _UPDATE_LABELS if c in cats) or "Changes/Revisions"


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
    if f.get("category") == "addendum" and f.get("addendum_number"):
        meta = f'Addendum {html.escape(str(f["addendum_number"]))}'
        if f.get("addendum_issued_on"):
            meta += f' · issued {html.escape(str(f["addendum_issued_on"]))}'
        item += (
            f'<br><span style="font-size:13px;color:{_NAVY};font-weight:bold;">'
            f"{meta}</span>"
        )
    if f.get("note"):
        item += (
            f'<br><span style="font-size:13px;color:{_MUTED};">'
            f"Note: {html.escape(f['note'])}</span>"
        )
    return item + "</li>"


def section_key(category: str, doc_type: str | None) -> str:
    """The key one section's note is filed under. MUST stay identical to
    `app.core.file_categories.section_key` — that module owns the definition and
    the API validates against it; this copy keeps the renderer a leaf."""
    if category in _SPLIT_CATEGORIES and doc_type in ("drawing", "specification"):
        return f"{category}:{doc_type}"
    return category


def _section_note_block(note: str | None) -> str:
    """The section's "what changed in the plans / in the specs" line, rendered
    directly under its heading and above the file list. Distinct from the
    batch-wide MESSAGE FROM THE G3 TEAM block and from each file's own note."""
    text = (note or "").strip()
    if not text:
        return ""
    return (
        f'<div style="margin:0 0 8px;font-size:13px;color:{_MUTED};">'
        f"{html.escape(text).replace(chr(10), '<br>')}</div>"
    )


def render_sections(files: list[dict], signer, section_notes: dict | None = None) -> str:
    """The grouped file lists: one titled section per (category, doc_type), with
    that section's note under the heading, initial files tagged apart from
    post-hand-off updates. `signer(storage_path) -> url` is injected so rendering
    stays pure and testable.

    A file matches a split-category section only when its doc_type matches
    EXACTLY — including the `None` row, which collects the pre-0077 files that
    never recorded one. So every file lands in exactly one section and none is
    silently dropped."""
    notes = section_notes or {}
    out: list[str] = []
    for category, doc_type, title in SECTION_TITLES:
        if category in _SPLIT_CATEGORIES:
            group = [
                f
                for f in files
                if f["category"] == category and (f.get("doc_type") or None) == doc_type
            ]
        else:
            group = [f for f in files if f["category"] == category]
        if not group:
            continue
        tag_html = "" if category in _NO_TAG else _tag_pill(
            INITIAL_TAG if category in _INITIAL else UPDATE_TAG
        )
        out.append(
            f'<div style="margin:18px 0 8px;font-size:15px;font-weight:bold;'
            f'color:{_NAVY};">{html.escape(title)}{tag_html}</div>'
            # Exact section key first, then the bare category: the Revisions
            # modal keeps addenda in ONE box (whose files are tagged plans/specs
            # per row), so its single note arrives keyed "addendum" and belongs
            # above BOTH addendum sub-sections.
            + _section_note_block(
                notes.get(section_key(category, doc_type)) or notes.get(category)
            )
            + '<ul style="margin:0;padding-left:20px;">'
            + "".join(_file_item(f, signer(f["storage_path"])) for f in group)
            + "</ul>"
        )
    return "".join(out)


def _labeled_block(label: str, text: str | None) -> str:
    """The navy-ruled callout used for free text from the team — the batch-wide
    message on a package/updates send, the reason on a withdrawal notice."""
    if not (text or "").strip():
        return ""
    return (
        f'<div style="margin:0 0 16px;padding:12px 16px;border-left:3px solid {_NAVY};'
        f'background-color:#f5f6f9;border-radius:0 8px 8px 0;">'
        f'<div style="font-size:12px;font-weight:bold;color:{_MUTED};'
        f'letter-spacing:1px;">{html.escape(label)}</div>'
        f'<div style="padding-top:4px;">'
        f"{html.escape(text.strip()).replace(chr(10), '<br>')}</div></div>"
    )


def _message_block(message: str | None) -> str:
    return _labeled_block("MESSAGE FROM THE G3 TEAM", message)


def render_package_email(
    *,
    proj: dict,
    files: list[dict],
    recipient_name: str | None,
    signer,
    message: str | None = None,
) -> str:
    """The full-package email body: greeting, project intro with the due-back
    date, an optional team message, and every file grouped by section."""
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
        + _message_block(message)
        + render_sections(files, signer)
        + f'<p style="margin:18px 0 0;">{html.escape(PORTAL_LINE_PACKAGE)}</p>'
    )
    return render_branded_html(body, subtitle="ESTIMATE FILES")


# The catch-up "Update history" table's Contents column renders each batch's
# summary snapshot, in display order, as e.g. "12 drawings, 3 specs, 1 addendum".
_CONTENTS_LABELS: list[tuple[str, tuple[str, str]]] = [
    ("drawing", ("general drawing", "general drawings")),
    ("electrical_drawing", ("electrical drawing", "electrical drawings")),
    ("specification", ("spec", "specs")),
    ("addendum", ("addendum", "addenda")),
    ("revision", ("revision", "revisions")),
    ("additional", ("additional file", "additional files")),
]

# kind -> the "Sent" column label in the catch-up history table.
_KIND_LABELS = {
    "initial": "Initial package",
    "revision": "Update",
    "reassign": "Catch-up",
}


def _summary_contents(summary: dict | None) -> str:
    """Render a batch's summary-count snapshot as a human phrase in display
    order — "12 drawings, 3 specs, 1 addendum". Empty -> em dash."""
    summary = summary or {}
    parts: list[str] = []
    for cat, (one, many) in _CONTENTS_LABELS:
        n = summary.get(cat) or 0
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    return ", ".join(parts) if parts else "—"


def _history_table(prior: list[dict] | None) -> str:
    """Outlook-safe "Update history" table (Date | Sent | Contents), one row per
    prior batch oldest-first. Same table construction as revision_email._banner.
    Empty/None -> no table."""
    if not prior:
        return ""
    head_cell = (
        f'font-size:12px;font-weight:bold;color:{_MUTED};letter-spacing:1px;'
        f"text-transform:uppercase;padding:6px 12px;border-bottom:1px solid {_BORDER};"
        "text-align:left;"
    )
    body_cell = f"font-size:13px;padding:8px 12px;border-bottom:1px solid {_BORDER};"
    rows = [
        '<tr>'
        f'<td style="{head_cell}">Date</td>'
        f'<td style="{head_cell}">Sent</td>'
        f'<td style="{head_cell}">Contents</td></tr>'
    ]
    for b in prior:
        sent_at = html.escape(str(b.get("sent_at") or ""))
        kind_label = html.escape(_KIND_LABELS.get(b.get("kind"), "Update"))
        contents = html.escape(_summary_contents(b.get("summary")))
        rows.append(
            "<tr>"
            f'<td style="{body_cell}">{sent_at}</td>'
            f'<td style="{body_cell}">{kind_label}</td>'
            f'<td style="{body_cell}">{contents}</td></tr>'
        )
    return (
        '<div style="margin:22px 0 8px;font-size:15px;font-weight:bold;'
        f'color:{_NAVY};">Update history</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;border:1px solid {_BORDER};border-radius:6px;">'
        + "".join(rows)
        + "</table>"
    )


def render_reassign_email(
    *,
    proj: dict,
    files: list[dict],
    recipient_name: str | None,
    signer,
    message: str | None = None,
    prior: list[dict] | None = None,
) -> str:
    """Catch-up package for an estimator added after the project went out.

    `files` is the full current package (initial package + everything already
    sent), grouped by category exactly like the initial package. `prior` is
    `file_sends.prior_batches(project_id)`: [{kind, sent_at, summary}]
    oldest-first, rendered as the compact "Update history" table so the
    chronology is visible without fragmenting the plan set into per-batch lists.

    Everything renders through the branded shell — navy/silver, inline logo,
    (702) 916-3355 signature. No red anywhere (red stays reserved for
    revision_email's high-importance banner)."""
    due = proj.get("due_from_estimator_at") or "TBD"
    intro = (
        f'<p style="margin:0 0 14px;">You’ve been added to project '
        f"<b>{html.escape(proj['name'])}</b> ({html.escape(proj['number'])}). "
        "This package is the complete file set, including every change issued "
        "since the project first went out.</p>"
        f'<p style="margin:0 0 6px;">Due back from estimator: <b>{html.escape(str(due))}</b></p>'
    )
    body = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">{_greeting(recipient_name)}</p>'
        + intro
        + _message_block(message)
        + render_sections(files, signer)
        + _history_table(prior)
        + f'<p style="margin:18px 0 0;">{html.escape(PORTAL_LINE_PACKAGE)}</p>'
    )
    return render_branded_html(body, subtitle="ESTIMATE FILES — CATCH-UP")


def render_updates_email(
    *,
    proj: dict,
    files: list[dict],
    message: str | None,
    signer,
    recipient_name: str | None = None,
    section_notes: dict | None = None,
) -> str:
    """The Changes/Revisions & Additional files email body: optional message
    from the team, then the new files grouped into revised plans / revised specs
    / addenda / additional, each section headed by its own "what changed" note
    (`section_notes`, keyed by `section_key`) and each file by its own note.

    `section_notes` belongs to THIS send only, which is why the full-package and
    catch-up emails (which span every batch) never render it."""
    label = updates_label(files)
    intro = (
        f'<p style="margin:0 0 14px;"><b>{html.escape(label)}</b> for project '
        f"<b>{html.escape(proj['name'])}</b> ({html.escape(proj['number'])}).</p>"
    )
    body = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">{_greeting(recipient_name)}</p>'
        + intro
        + _message_block(message)
        + render_sections(files, signer, section_notes)
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
    message: str | None = None,
    kind: str = "initial",
    prior: list[dict] | None = None,
) -> dict:
    """Email the full file package; returns the email_log row.

    `kind="reassign"` renders the catch-up variant (full package + an Update
    history table built from `prior`) and marks the subject; the default
    `kind="initial"` renders the standard package email."""
    if kind == "reassign":
        body_html = render_reassign_email(
            proj=proj,
            files=files,
            recipient_name=recipient_name,
            signer=_email_signer,
            message=message,
            prior=prior,
        )
        subject_suffix = " (full catch-up)"
    else:
        body_html = render_package_email(
            proj=proj,
            files=files,
            recipient_name=recipient_name,
            signer=_email_signer,
            message=message,
        )
        subject_suffix = ""
    return graph_email.send_mail(
        to=to,
        subject=f"[BDR] Estimate request{subject_suffix} — {proj['name']} ({proj['number']})",
        body_html=body_html,
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        project_id=proj["id"],
        sent_by=sent_by,
    )


# ── Lifecycle notices (withdrawn / reactivated) ─────────────────────────────
# No file links: these say the WORK changed state, not that files did. The
# button lands on the portal home rather than the project — a withdrawn project
# 403s on its detail route (deps.require_project_assignment), so deep-linking
# into it would only dead-end the reader.


def _portal_url() -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/estimator"


def _project_line(proj: dict, sentence: str) -> str:
    return (
        f'<p style="margin:0 0 14px;">Project '
        f"<b>{html.escape(proj['name'])}</b> ({html.escape(proj['number'])}) "
        f"{sentence}</p>"
    )


def render_withdrawn_email(
    *, proj: dict, recipient_name: str | None, note: str | None = None
) -> str:
    """The "stop work" notice: the bid was abandoned while this estimator held
    it. `note` is the internal reason, included only when the team wrote one."""
    body = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">{_greeting(recipient_name)}</p>'
        + _project_line(proj, "has been <b>withdrawn</b> — G3 is no longer bidding it.")
        + _labeled_block("REASON", note)
        + '<p style="margin:0 0 14px;">Please stop work on this estimate. Nothing '
        "further is due from us or from you.</p>"
        + '<p style="margin:0 0 14px;">The project now shows as <b>Withdrawn</b> in '
        "your BDR portal and its files are no longer available there. If that "
        "changes, we'll email you.</p>"
        + _button("Open your portal", _portal_url())
    )
    return render_branded_html(body, subtitle="PROJECT WITHDRAWN")


def render_reactivated_email(*, proj: dict, recipient_name: str | None) -> str:
    """The reverse notice: a withdrawn bid is live again and back on their desk."""
    due = proj.get("due_from_estimator_at") or "TBD"
    body = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">{_greeting(recipient_name)}</p>'
        + _project_line(proj, "is <b>active again</b> — G3 is bidding it after all.")
        + '<p style="margin:0 0 6px;">Due back from estimator: '
        f"<b>{html.escape(str(due))}</b></p>"
        + '<p style="margin:14px 0;">Your assignment was never revoked, so the '
        "project and its full file package are waiting for you in the BDR portal "
        "exactly as you left them.</p>"
        + _button("Open your portal", _portal_url())
    )
    return render_branded_html(body, subtitle="PROJECT REACTIVATED")


def send_withdrawn(
    *,
    proj: dict,
    to: list[str],
    recipient_name: str | None = None,
    note: str | None = None,
    sent_by: str | None = None,
) -> dict:
    """Email one estimator that the bid they hold has been abandoned."""
    return graph_email.send_mail(
        to=to,
        subject=f"[BDR] Project withdrawn — {proj['name']} ({proj['number']})",
        body_html=render_withdrawn_email(proj=proj, recipient_name=recipient_name, note=note),
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        project_id=proj["id"],
        sent_by=sent_by,
    )


def send_reactivated(
    *,
    proj: dict,
    to: list[str],
    recipient_name: str | None = None,
    sent_by: str | None = None,
) -> dict:
    """Email one estimator that a withdrawn bid is back on."""
    return graph_email.send_mail(
        to=to,
        subject=f"[BDR] Project reactivated — {proj['name']} ({proj['number']})",
        body_html=render_reactivated_email(proj=proj, recipient_name=recipient_name),
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
    recipient_name: str | None = None,
    sent_by: str | None = None,
    section_notes: dict | None = None,
) -> dict:
    """Email the pending Changes/Revisions & Additional files; returns the
    email_log row. `recipient_name` personalises the greeting when the caller
    loops one send per active assignee. `section_notes` is this batch's
    per-section "what changed" text (see render_updates_email)."""
    body_html = render_updates_email(
        proj=proj,
        files=files,
        message=message,
        recipient_name=recipient_name,
        signer=_email_signer,
        section_notes=section_notes,
    )
    return graph_email.send_mail(
        to=to,
        subject=f"[BDR] {updates_label(files)} — {proj['name']} ({proj['number']})",
        body_html=body_html,
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        project_id=proj["id"],
        sent_by=sent_by,
    )
