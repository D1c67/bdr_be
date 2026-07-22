"""Render an RFI to a PDF that mirrors the in-app "View RFI" form.

The PDF is both the artifact emailed to the GC and the copy archived in the
project's Documents (category 'rfi') — the durable record of exactly what was
sent. It is built from an HTML template that reproduces
`bdr_fe/components/pm/field/RfiDetailModal.tsx` (G3's printed RFI form: ruled
cells, header bands, the logo beside the identifying fields, then the request and
a blank-able response section) and rendered by the Gotenberg **Chromium** engine —
the same sidecar already used for Office → PDF previews (see office_preview.py) —
so the emailed sheet and the on-screen form stay one and the same.

The question is server-sanitized HTML (nh3, see services/sanitize.py) and is
embedded as-is; every other field is HTML-escaped here.
"""

import base64
import html
from datetime import date, datetime

import httpx

from app.core.config import get_settings
from app.services import email_branding
from app.services.office_preview import ConversionError

# Pulled from the design tokens the modal renders with (globals.css), so the
# printed sheet reads as the same document, not a look-alike.
_NAVY = "#202159"
_INK = "#26292f"
_MUTED = "#5b606b"
_FAINT = "#9aa0a8"
_RULE = "#c9ccd2"
_BAND_BG = "#eef0f4"
_FONT = "Arial, Helvetica, sans-serif"


def _fmt_day(v: str | None) -> str:
    """ISO date/timestamp → "Jul 3, 2026", matching the frontend's fmtDay. Empty
    string (not an em dash) for blanks: this is a form, and a blank cell reads as
    a field left to fill rather than "no value"."""
    if not v:
        return ""
    try:
        text = str(v)
        parsed = date.fromisoformat(text[:10]) if len(text) >= 10 else None
        if parsed is None:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return ""
    # %-d (no leading zero) is POSIX; the backend runs on Linux/macOS.
    return parsed.strftime("%b %-d, %Y")


def _esc(v: object) -> str:
    return html.escape("" if v is None else str(v))


def _logo_data_uri() -> str:
    return "data:image/jpeg;base64," + base64.b64encode(email_branding.logo_bytes()).decode()


def _cell(content: str, *, head: bool = False, extra: str = "") -> str:
    base = (
        f"border:1px solid {_RULE};padding:6px 10px;font-size:12px;vertical-align:top;"
    )
    if head:
        base += (
            f"color:{_MUTED};text-transform:uppercase;letter-spacing:.04em;"
            "font-weight:700;font-size:10.5px;white-space:nowrap;"
        )
    else:
        base += f"color:{_INK};"
    return f'<td style="{base}{extra}">{content}</td>'


def _band(label: str) -> str:
    return (
        f'<div style="border:1px solid {_RULE};background:{_BAND_BG};padding:6px 10px;'
        f"text-align:center;text-transform:uppercase;letter-spacing:.06em;"
        f'font-size:11px;font-weight:700;color:{_INK};margin:0;">{_esc(label)}</div>'
    )


def _attachments_list(items: list[dict]) -> str:
    names = [_esc(a.get("filename") or a.get("key")) for a in (items or [])]
    if not names:
        return "&mdash;"
    return "<br>".join(f"&bull;&nbsp;{n}" for n in names)


def render_html(project: dict, rfi: dict) -> str:
    """The full HTML document for one RFI, ready for Chromium → PDF."""
    rfi_no = str(rfi.get("rfi_number") or "").zfill(3)
    company = rfi.get("assigned_gc_name") or rfi.get("asked_of") or ""
    contact = rfi.get("assigned_contact_name") or ""
    submitted_by = rfi.get("created_by_name")
    submitted = f"{submitted_by} — G3 Electrical" if submitted_by else "G3 Electrical"
    answer = (rfi.get("answer") or "").strip()
    answer_html = _esc(answer).replace("\n", "<br>") if answer else (
        f'<span style="color:{_FAINT};">No response recorded yet.</span>'
    )
    answer_docs = rfi.get("answer_attachments") or []

    # Identifying block: logo cell beside a 2-col label/value grid (as on the form).
    ident_rows = "".join(
        f"<tr>{_cell(label, head=True)}{_cell(value)}</tr>"
        for label, value in (
            ("RFI #", f'<span style="font-family:monospace;">{rfi_no}</span>'),
            ("Date", _esc(_fmt_day(rfi.get("created_at")))),
            ("Date needed by", _esc(_fmt_day(rfi.get("due_at")))),
            ("Project name", _esc(project.get("name"))),
            ("Project #", f'<span style="font-family:monospace;">{_esc(project.get("number"))}</span>'),
            ("Ref.: Drawing no.", _esc(", ".join(rfi.get("drawing_numbers") or []))),
        )
    )

    response_docs_row = ""
    if answer_docs:
        response_docs_row = (
            f"<table style='width:100%;border-collapse:collapse;table-layout:fixed;'>"
            f"<colgroup><col style='width:170px;'><col></colgroup>"
            f"<tr>{_cell('Response documents', head=True)}"
            f"{_cell(_attachments_list(answer_docs))}</tr></table>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ font-family: {_FONT}; color: {_INK}; font-size: 12px; line-height: 1.5; }}
  .sheet {{ width: 100%; }}
  table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  a {{ color: {_NAVY}; }}
  .req p {{ margin: 0 0 8px; }}
  .req ul, .req ol {{ margin: 0 0 8px 18px; padding: 0; }}
</style>
</head>
<body>
  <div class="sheet">
    {_band("Request for Information Form")}

    <table style="border-left:1px solid {_RULE};border-right:1px solid {_RULE};border-bottom:1px solid {_RULE};">
      <colgroup><col style="width:190px;"><col></colgroup>
      <tr>
        <td style="border-right:1px solid {_RULE};padding:14px;text-align:center;vertical-align:middle;">
          <img src="{_logo_data_uri()}" alt="G3 Electrical" style="width:110px;height:auto;">
        </td>
        <td style="padding:0;">
          <table style="table-layout:fixed;">
            <colgroup><col style="width:150px;"><col></colgroup>
            {ident_rows}
          </table>
        </td>
      </tr>
    </table>

    <table><colgroup><col style="width:170px;"><col></colgroup>
      <tr>{_cell("To", head=True)}{_cell(_esc(contact))}</tr>
      <tr>{_cell("Company", head=True)}{_cell(_esc(company))}</tr>
    </table>

    {_band("RFI Description")}
    <div style="border-left:1px solid {_RULE};border-right:1px solid {_RULE};border-bottom:1px solid {_RULE};padding:14px;">
      <p style="margin:0 0 12px;font-weight:700;color:{_INK};">RFI {rfi_no} — {_esc(rfi.get("subject"))}</p>
      <p style="margin:0 0 4px;font-weight:700;">Reference:</p>
      <p style="margin:0 0 12px;color:{_MUTED};">{_esc(", ".join(rfi.get("applicable_references") or [])) or "&mdash;"}</p>
      <p style="margin:0 0 4px;font-weight:700;">Request:</p>
      <div class="req">{rfi.get("question") or ""}</div>
    </div>

    <table><colgroup><col style="width:170px;"><col></colgroup>
      <tr>{_cell("Attachments", head=True)}{_cell(_attachments_list(rfi.get("attachments") or []))}</tr>
      <tr>{_cell("Submitted by", head=True)}{_cell(_esc(submitted))}</tr>
    </table>

    {_band("Response to RFI")}
    <div style="border-left:1px solid {_RULE};border-right:1px solid {_RULE};border-bottom:1px solid {_RULE};padding:14px;min-height:90px;white-space:pre-wrap;">{answer_html}</div>
    {response_docs_row}
    <table><colgroup><col style="width:170px;"><col style="width:170px;"><col></colgroup>
      <tr>{_cell("Response by", head=True)}{_cell(_esc(rfi.get("answered_by")))}
          {_cell(_esc(_fmt_day(rfi.get("answered_at"))))}</tr>
    </table>
  </div>
</body>
</html>"""


def render_pdf(project: dict, rfi: dict) -> bytes:
    """Render the RFI form to PDF via Gotenberg's Chromium HTML route.

    Raises ConversionError on any failure (converter down, non-PDF response) so
    the caller fails the send rather than emailing a broken/empty attachment —
    the same fail-closed contract as office_preview.convert_for_send.
    """
    doc = render_html(project, rfi)
    s = get_settings()
    try:
        resp = httpx.post(
            f"{s.gotenberg_base_url}/forms/chromium/convert/html",
            files={"index.html": ("index.html", doc.encode("utf-8"), "text/html")},
            data={
                "paperWidth": "8.5",
                "paperHeight": "11",
                "marginTop": "0.5",
                "marginBottom": "0.5",
                "marginLeft": "0.5",
                "marginRight": "0.5",
                "printBackground": "true",
            },
            timeout=s.preview_convert_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise ConversionError(f"gotenberg unreachable: {exc}") from exc
    if resp.status_code != 200 or not resp.content or not resp.content.startswith(b"%PDF"):
        raise ConversionError(
            f"gotenberg chromium {resp.status_code}: {resp.text[:200]}"
        )
    return resp.content


def pdf_filename(rfi: dict) -> str:
    """`RFI-003 - <subject>.pdf`, with the subject reduced to filename-safe text
    and bounded so a 300-char subject can't produce an unwieldy object name."""
    rfi_no = str(rfi.get("rfi_number") or "").zfill(3)
    subject = " ".join((rfi.get("subject") or "").split())
    subject = "".join(c for c in subject if c.isalnum() or c in " -_().").strip()
    subject = subject[:80].strip()
    return f"RFI-{rfi_no}{f' - {subject}' if subject else ''}.pdf"
