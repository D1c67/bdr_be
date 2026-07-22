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
    doc = render_html(project, category_name, items)
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
    def _safe(v: str) -> str:
        v = " ".join((v or "").split())
        v = "".join(c for c in v if c.isalnum() or c in " -_().").strip()
        return v[:80].strip()

    number = _safe(str(project.get("number") or "")) or "Project"
    cat = _safe(category_name) or "Materials"
    return f"Submittal Request - {number} - {cat}.pdf"
