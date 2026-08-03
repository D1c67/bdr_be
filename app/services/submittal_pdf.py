"""Render a project submittal REQUEST to a PDF — the per-category list of items
we're asking a vendor to provide product submittals (cut sheets) for.

One PDF per material category is attached to that category's vendor emails and
archived into the project's Documents hub (category 'submittal'). Built from an
HTML template in the G3 form style and rendered by the Gotenberg **Chromium**
engine — the same sidecar the RFI form uses (see rfi_pdf.py) — with the same
fail-closed contract: a non-PDF response raises ConversionError so the caller
fails that category's sends rather than emailing a broken attachment.

Every field is HTML-escaped here; the item descriptions include hand-typed
ad-hoc lines, so escaping is the injection boundary.
"""

import base64
import html
from datetime import datetime, timezone

import httpx

from app.core.config import get_settings
from app.services import email_branding
from app.services.office_preview import ConversionError

# Design tokens shared with rfi_pdf / globals.css so the sheet reads as one of
# G3's printed forms, not a look-alike.
_NAVY = "#202159"
_INK = "#26292f"
_MUTED = "#5b606b"
_FAINT = "#9aa0a8"
_RULE = "#c9ccd2"
_BAND_BG = "#eef0f4"
_FONT = "Arial, Helvetica, sans-serif"


def _esc(v: object) -> str:
    return html.escape("" if v is None else str(v))


def _fmt_qty(v: object) -> str:
    """Whole numbers without a trailing .0; anything non-numeric passes through
    escaped (quantities are free-form on pm_materials)."""
    if v is None or v == "":
        return ""
    try:
        d = float(v)
    except (TypeError, ValueError):
        return _esc(v)
    return _esc(int(d) if d == int(d) else f"{d:g}")


def _logo_data_uri() -> str:
    return "data:image/jpeg;base64," + base64.b64encode(email_branding.logo_bytes()).decode()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%b %-d, %Y")


def _cell(content: str, *, head: bool = False, extra: str = "") -> str:
    base = f"border:1px solid {_RULE};padding:6px 10px;font-size:12px;vertical-align:top;"
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


def render_html(project: dict, category_name: str, items: list[dict]) -> str:
    """The full HTML document for one category's submittal request."""
    ident_rows = "".join(
        f"<tr>{_cell(label, head=True)}{_cell(value)}</tr>"
        for label, value in (
            ("Date", _esc(_today())),
            ("Project name", _esc(project.get("name"))),
            ("Project #", f'<span style="font-family:monospace;">{_esc(project.get("number"))}</span>'),
            ("Category", _esc(category_name)),
        )
    )

    if items:
        item_rows = "".join(
            "<tr>"
            + _cell(_esc(idx + 1), extra="text-align:center;width:40px;")
            + _cell(_esc(it.get("description")))
            + _cell(_fmt_qty(it.get("quantity")), extra="text-align:right;width:80px;")
            + _cell(_esc(it.get("unit")), extra="width:90px;")
            + _cell(_esc(it.get("notes")))
            + "</tr>"
            for idx, it in enumerate(items)
        )
    else:
        item_rows = (
            f'<tr><td colspan="5" style="border:1px solid {_RULE};padding:14px;'
            f'text-align:center;color:{_FAINT};font-size:12px;">No items listed.</td></tr>'
        )

    header_cells = (
        _cell("Sr.", head=True, extra="text-align:center;width:40px;")
        + _cell("Description", head=True)
        + _cell("Qty", head=True, extra="text-align:right;width:80px;")
        + _cell("Unit", head=True, extra="width:90px;")
        + _cell("Notes", head=True)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ font-family: {_FONT}; color: {_INK}; font-size: 12px; line-height: 1.5; }}
  table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  a {{ color: {_NAVY}; }}
</style>
</head>
<body>
  <div class="sheet">
    {_band("Submittal Request")}

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

    <div style="padding:14px 0 6px;color:{_MUTED};font-size:12px;">
      Please provide product submittals (cut sheets) for the following items.
    </div>

    <table>
      <colgroup>
        <col style="width:40px;"><col><col style="width:80px;"><col style="width:90px;"><col style="width:170px;">
      </colgroup>
      <tr>{header_cells}</tr>
      {item_rows}
    </table>
  </div>
</body>
</html>"""


def render_pdf(project: dict, category_name: str, items: list[dict]) -> bytes:
    """Render the request to PDF via Gotenberg's Chromium HTML route.

    Raises ConversionError on any failure (converter down, non-PDF response) so
    the caller fails that category's sends rather than emailing a broken
    attachment — the same fail-closed contract as rfi_pdf / office_preview.
    """
    return html_to_pdf(render_html(project, category_name, items))


def html_to_pdf(doc: str) -> bytes:
    """Letter-size Chromium render, shared by the vendor request sheet, the GC
    transmittal, the per-category cover pages and pdf_combine's image pages.
    Fail-closed: anything that isn't a PDF raises ConversionError."""
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
        raise ConversionError(f"gotenberg chromium {resp.status_code}: {resp.text[:200]}")
    return resp.content


def pdf_filename(project: dict, category_name: str) -> str:
    """`Submittal Request - <Project#> - <Category>.pdf`, filename-safe/bounded."""
    return f"Submittal Request - {_safe_name(project.get('number'))} - {_safe_name(category_name, 'Materials')}.pdf"


def _safe_name(v: object, fallback: str = "Project") -> str:
    v = " ".join(str(v or "").split())
    v = "".join(c for c in v if c.isalnum() or c in " -_().").strip()
    return v[:80].strip() or fallback


# ── GC-facing: the submittal approval transmittal (migration 0081) ───────────
#
# The cover sheet that fronts a submittal approval package. Unlike the vendor
# request sheet above (which lists materials we WANT submittals for), this lists
# the files we ARE submitting, grouped by category, with an action column the GC
# marks up. It doubles as the archived record of exactly what went out.


def render_package_html(
    project: dict,
    *,
    number: int,
    groups: list[tuple[str, list[dict]]],
    message: str | None,
    recipients: list[dict],
    cc_recipients: list[dict],
    supersedes_number: int | None = None,
) -> str:
    """The full HTML document for one submittal approval package.

    `groups` is [(category_name, [{filename, description}])] in display order.
    Every value is HTML-escaped — filenames and the cover note are user-supplied,
    so escaping is the injection boundary (same contract as render_html).

    `supersedes_number` marks the sheet as a resubmittal of an earlier package,
    both in the banner and as an identity row: this is the sheet the GC files
    against their own review, so it has to say which review it answers."""
    pkg_no = str(number).zfill(3)
    prior_no = str(supersedes_number).zfill(3) if supersedes_number else None

    def _people(rows: list[dict]) -> str:
        return (
            ", ".join(
                f"{r.get('name') or ''} <{r.get('email') or ''}>".strip() for r in rows
            )
            or "—"
        )

    ident_rows = "".join(
        f"<tr>{_cell(label, head=True)}{_cell(value)}</tr>"
        for label, value in (
            ("Submittal #", f'<span style="font-family:monospace;">{_esc(pkg_no)}</span>'),
            ("Date", _esc(_today())),
            ("Project name", _esc(project.get("name"))),
            ("Project #", f'<span style="font-family:monospace;">{_esc(project.get("number"))}</span>'),
            ("To", _esc(_people(recipients))),
        )
        + ((("CC", _esc(_people(cc_recipients))),) if cc_recipients else ())
        + (
            (
                (
                    "Resubmittal of",
                    f'<span style="font-family:monospace;">{_esc(prior_no)}</span>',
                ),
            )
            if prior_no
            else ()
        )
    )

    header_cells = (
        _cell("Sr.", head=True, extra="text-align:center;width:40px;")
        + _cell("Submittal / file", head=True)
        + _cell("Item", head=True)
        + _cell("Approved", head=True, extra="text-align:center;width:70px;")
        + _cell("As noted", head=True, extra="text-align:center;width:70px;")
        + _cell("Rejected", head=True, extra="text-align:center;width:70px;")
    )
    # An empty box the GC ticks — this sheet is meant to come back marked up.
    box = (
        f'<span style="display:inline-block;width:12px;height:12px;'
        f'border:1px solid {_RULE};"></span>'
    )

    body_rows = ""
    n = 0
    for cat_name, files in groups:
        body_rows += (
            f'<tr><td colspan="6" style="border:1px solid {_RULE};background:{_BAND_BG};'
            f"padding:5px 10px;font-size:10.5px;font-weight:700;color:{_INK};"
            f'text-transform:uppercase;letter-spacing:.04em;">{_esc(cat_name)}</td></tr>'
        )
        for f in files:
            n += 1
            body_rows += (
                "<tr>"
                + _cell(_esc(n), extra="text-align:center;width:40px;")
                + _cell(_esc(f.get("filename")))
                + _cell(_esc(f.get("description")))
                + _cell(box, extra="text-align:center;width:70px;")
                + _cell(box, extra="text-align:center;width:70px;")
                + _cell(box, extra="text-align:center;width:70px;")
                + "</tr>"
            )
    if not body_rows:
        body_rows = (
            f'<tr><td colspan="6" style="border:1px solid {_RULE};padding:14px;'
            f'text-align:center;color:{_FAINT};font-size:12px;">No files listed.</td></tr>'
        )

    note = ""
    if (message or "").strip():
        note = (
            f'<div style="margin:12px 0 0;padding:10px 12px;border:1px solid {_RULE};'
            f'border-left:3px solid {_NAVY};font-size:12px;color:{_INK};">'
            f'<div style="font-size:10.5px;font-weight:700;color:{_MUTED};'
            f'text-transform:uppercase;letter-spacing:.04em;">Note</div>'
            f"<div style=\"padding-top:3px;\">{_esc(message.strip()).replace(chr(10), '<br>')}</div></div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ font-family: {_FONT}; color: {_INK}; font-size: 12px; line-height: 1.5; }}
  table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  a {{ color: {_NAVY}; }}
</style>
</head>
<body>
  <div class="sheet">
    {_band("Resubmittal Transmittal" if prior_no else "Submittal Transmittal")}

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

    <div style="padding:14px 0 6px;color:{_MUTED};font-size:12px;">
      {
        f"The following submittals are resubmitted in response to your review of "
        f"Submittal {_esc(prior_no)}."
        if prior_no
        else "The following submittals are provided for your review and approval."
      }
      Please mark each item below and return this sheet with your response.
    </div>

    <table>
      <colgroup>
        <col style="width:40px;"><col><col style="width:180px;">
        <col style="width:70px;"><col style="width:70px;"><col style="width:70px;">
      </colgroup>
      <tr>{header_cells}</tr>
      {body_rows}
    </table>
    {note}
  </div>
</body>
</html>"""


def render_package_pdf(
    project: dict,
    *,
    number: int,
    groups: list[tuple[str, list[dict]]],
    message: str | None,
    recipients: list[dict],
    cc_recipients: list[dict],
    supersedes_number: int | None = None,
) -> bytes:
    """Render the approval transmittal to PDF. Raises ConversionError on any
    failure, so the caller fails the send rather than emailing the GC a package
    with no cover sheet saying what's in it."""
    doc = render_package_html(
        project,
        number=number,
        groups=groups,
        message=message,
        recipients=recipients,
        cc_recipients=cc_recipients,
        supersedes_number=supersedes_number,
    )
    return html_to_pdf(doc)


def package_pdf_filename(project: dict, number: int) -> str:
    """`Submittal Transmittal - <Project#> - 003.pdf`, filename-safe/bounded."""
    return f"Submittal Transmittal - {_safe_name(project.get('number'))} - {str(number).zfill(3)}.pdf"


# ── GC-facing: the per-category cover page ───────────────────────────────────
#
# Each material category in an approval package is delivered as ONE combined PDF
# — this cover page, then every submittal in that category appended behind it
# (pdf_combine). The GC opens one file per category and knows from page 1 which
# category it is and what should be inside.
#
# Deliberately NOT a second markup sheet: the package's single transmittal
# carries the Approved / As-noted / Rejected boxes so the GC has one sheet to
# mark up and return, not N of them.


def render_category_cover_html(
    project: dict,
    *,
    number: int,
    category_name: str,
    files: list[dict],
) -> str:
    """The cover page fronting one category's combined submittal PDF.

    `files` is [{filename, description, merged}] in the order they are appended;
    `merged` False means the file could not be folded in and rides the email as a
    separate attachment — it is still listed here, flagged, because the cover page
    is the GC's index of what the category contains.
    """
    pkg_no = str(number).zfill(3)

    ident_rows = "".join(
        f"<tr>{_cell(label, head=True)}{_cell(value)}</tr>"
        for label, value in (
            ("Submittal #", f'<span style="font-family:monospace;">{_esc(pkg_no)}</span>'),
            ("Date", _esc(_today())),
            ("Project name", _esc(project.get("name"))),
            ("Project #", f'<span style="font-family:monospace;">{_esc(project.get("number"))}</span>'),
            ("Category", _esc(category_name)),
        )
    )

    header_cells = (
        _cell("Sr.", head=True, extra="text-align:center;width:40px;")
        + _cell("Submittal / file", head=True)
        + _cell("Item", head=True)
    )

    separate = [f for f in files if not f.get("merged", True)]
    rows = "".join(
        "<tr>"
        + _cell(_esc(i), extra="text-align:center;width:40px;")
        + _cell(
            _esc(f.get("filename"))
            + (
                ""
                if f.get("merged", True)
                else f'<span style="color:{_MUTED};font-size:10.5px;"> (sent as a separate attachment)</span>'
            )
        )
        + _cell(_esc(f.get("description")))
        + "</tr>"
        for i, f in enumerate(files, start=1)
    ) or (
        f'<tr><td colspan="3" style="border:1px solid {_RULE};padding:14px;'
        f'text-align:center;color:{_FAINT};font-size:12px;">No files listed.</td></tr>'
    )

    footnote = ""
    if separate:
        footnote = (
            f'<div style="margin:12px 0 0;padding:10px 12px;border:1px solid {_RULE};'
            f'border-left:3px solid {_NAVY};font-size:11px;color:{_MUTED};">'
            f"{len(separate)} of the files above could not be combined into this PDF "
            "and are attached to the email separately.</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ font-family: {_FONT}; color: {_INK}; font-size: 12px; line-height: 1.5; }}
  table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  a {{ color: {_NAVY}; }}
</style>
</head>
<body>
  <div class="sheet">
    {_band("Requested Submittals")}

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

    <div style="padding:18px 0 10px;text-align:center;">
      <div style="color:{_MUTED};font-size:11px;text-transform:uppercase;letter-spacing:.08em;">
        Category
      </div>
      <div style="color:{_NAVY};font-size:22px;font-weight:700;letter-spacing:.01em;padding-top:2px;">
        {_esc(category_name)}
      </div>
    </div>

    <div style="padding:0 0 6px;color:{_MUTED};font-size:12px;">
      The submittals listed below follow this page in this document, submitted for
      your review and approval.
    </div>

    <table>
      <colgroup><col style="width:40px;"><col><col style="width:180px;"></colgroup>
      <tr>{header_cells}</tr>
      {rows}
    </table>
    {footnote}
  </div>
</body>
</html>"""


def render_category_cover_pdf(
    project: dict,
    *,
    number: int,
    category_name: str,
    files: list[dict],
) -> bytes:
    """Render one category's cover page. Raises ConversionError on any failure,
    so the caller fails the send rather than shipping a headless merge the GC
    can't tell apart from the next category's."""
    return html_to_pdf(
        render_category_cover_html(
            project, number=number, category_name=category_name, files=files
        )
    )


def category_pdf_filename(project: dict, category_name: str) -> str:
    """`Requested Submittals - <Project#> - <Category>.pdf`, filename-safe/bounded.

    This is the name the GC receives and the name the copy is archived under in
    the project's Documents hub, so the two are the same string by construction.
    """
    return (
        f"Requested Submittals - {_safe_name(project.get('number'))} - "
        f"{_safe_name(category_name, 'Materials')}.pdf"
    )
