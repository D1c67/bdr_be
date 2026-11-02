"""Workbook builder for assistant reports (docs/ASSISTANT_PHASE4_PLAN.md 3.9.2).

House style copied from `bid_invitations_excel.py` (navy `202159` header
fill, thin borders, wrap, landscape, fit to width); every free-text cell
through `cpr_generation._formula_safe` (CWE-1236); times rendered Pacific
with `%-m/%-d/%Y %-I:%M %p`; titles use a colon, never a dash ("Bid
Invitations: week of 10/19/2026"). The `bid_invitations` spec reuses
`build_bid_invitations_workbook` unchanged and `strip_em_dashes` rewrites
its bytes so no string cell of any sheet keeps U+2014 (a colon in the
title, a hyphen elsewhere).

Column kinds of a SheetSpec:
- "text" and "wrap": free text (formula-safe, wrapped).
- "int": a whole number.
- "money": a Decimal, int, float or numeric string; written as a number
  with a dollar format. None or "" stays blank.
- "pct": a percentage in percent units (25.8 means 25.8 percent).
- "date" and "datetime": an aware datetime, a date or an ISO string,
  rendered Pacific as text ("10/23/2026" or "10/23/2026 2:05 PM").
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from app.services.cpr_generation import _formula_safe

PT = ZoneInfo("America/Los_Angeles")
EM_DASH = "\u2014"
NAVY = "202159"
KINDS = ("text", "wrap", "int", "money", "pct", "date", "datetime")
DATETIME_FORMAT = "%-m/%-d/%Y %-I:%M %p"
DATE_FORMAT = "%-m/%-d/%Y"
MONEY_FORMAT = '"$"#,##0.00'
PCT_FORMAT = '0.0"%"'
# Excel's own cell limit is 32,767 characters; stay well under it.
MAX_CELL_CHARS = 5000
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")

# One sheet: (name, columns, rows); a column is (header, width, kind).
SheetSpec = tuple[str, list[tuple[str, int, str]], list[list[Any]]]


def clean_text(value: Any) -> str:
    """Text for a cell: no U+2014, no control characters other than newline
    and tab, clipped to MAX_CELL_CHARS."""
    text = "" if value is None else str(value)
    text = text.replace(EM_DASH, "-")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text[:MAX_CELL_CHARS]


def pacific(value: Any, fmt: str = DATETIME_FORMAT) -> str:
    """A timestamp rendered Pacific; "" for None or an unreadable value. A
    naive datetime is taken as UTC; a bare date renders without a time."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value.strftime(DATE_FORMAT)
    else:
        text = str(value).strip()
        if len(text) == 10:  # a plain calendar date has no zone to convert
            try:
                return date.fromisoformat(text).strftime(DATE_FORMAT)
            except ValueError:
                return clean_text(text)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return clean_text(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PT).strftime(fmt)


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _cell_value(value: Any, kind: str) -> Any:
    if kind == "money":
        return _number(value)
    if kind == "pct":
        return _number(value)
    if kind == "int":
        number = _number(value)
        return int(number) if number is not None else None
    if kind == "date":
        return pacific(value, DATE_FORMAT)
    if kind == "datetime":
        return pacific(value, DATETIME_FORMAT)
    return _formula_safe(clean_text(value))


def sheet_title(name: str, taken: set[str]) -> str:
    """An Excel-legal, unique sheet name (31 characters, none of []:*?/\\)."""
    base = _SHEET_BAD.sub(" ", clean_text(name)).strip() or "Sheet"
    base = base[:31]
    title, n = base, 2
    while title.lower() in taken:
        suffix = f" {n}"
        title = base[: 31 - len(suffix)] + suffix
        n += 1
    taken.add(title.lower())
    return title


def build_workbook(title: str, banner: str, sheets: list[SheetSpec]) -> bytes:
    """The .xlsx bytes of one report: a title row, a banner row (the window,
    the row count, "truncated" when cut) and one formatted sheet per
    SheetSpec (header row, then the rows; panes frozen under the header;
    letter landscape fit to width with the header repeating). Raises
    ValueError for an unknown column kind, a row whose length differs from
    its columns, or no sheet."""
    if not sheets:
        raise ValueError("A workbook needs at least one sheet.")
    header_fill = PatternFill("solid", fgColor=NAVY)
    header_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="B7B7C4")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    top_right = Alignment(vertical="top", horizontal="right")

    wb = Workbook()
    wb.remove(wb.active)
    taken: set[str] = set()
    for name, columns, rows in sheets:
        for _, _, kind in columns:
            if kind not in KINDS:
                raise ValueError(f"Unknown column kind '{kind}'.")
        ws = wb.create_sheet(sheet_title(name, taken))
        ncols = max(1, len(columns))
        for i, (_, width, _) in enumerate(columns, start=1):
            ws.column_dimensions[get_column_letter(i)].width = max(6, min(int(width), 80))

        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
        head = ws.cell(row=1, column=1, value=_formula_safe(clean_text(title)))
        head.font = Font(bold=True, size=13, color=NAVY)
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
        note = ws.cell(row=2, column=1, value=_formula_safe(clean_text(banner)))
        note.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[2].height = 30

        for i, (header, _, _) in enumerate(columns, start=1):
            cell = ws.cell(row=3, column=i, value=_formula_safe(clean_text(header)))
            cell.fill, cell.font, cell.border = header_fill, header_font, border
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")

        for rix, row in enumerate(rows, start=4):
            if len(row) != len(columns):
                raise ValueError(f"Sheet '{name}' row {rix - 3} has {len(row)} values for {len(columns)} columns.")
            for cix, (value, (_, _, kind)) in enumerate(zip(row, columns), start=1):
                cell = ws.cell(row=rix, column=cix, value=_cell_value(value, kind))
                cell.border = border
                cell.alignment = wrap
                if kind == "money":
                    cell.number_format, cell.alignment = MONEY_FORMAT, top_right
                elif kind == "pct":
                    cell.number_format, cell.alignment = PCT_FORMAT, top_right
                elif kind == "int":
                    cell.number_format, cell.alignment = "#,##0", top_right

        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.print_title_rows = "3:3"
        ws.page_margins.left = ws.page_margins.right = 0.4
        ws.page_margins.top = ws.page_margins.bottom = 0.5
        ws.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def strip_em_dashes(content: bytes) -> bytes:
    """Rewrite an existing workbook so no string cell of any sheet keeps
    U+2014: the spaced dash in the first row's title becomes ": " (a colon, the
    house rule for titles) and every other U+2014 becomes a hyphen. Cells are
    re-run through `_formula_safe` because a leading dash turned hyphen would
    otherwise read as a formula lead. Formatting, merges, fills, widths and
    the print set-up survive the openpyxl round trip. Returns the bytes
    unchanged when nothing needed rewriting."""
    wb = load_workbook(io.BytesIO(content))
    changed = False
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if not isinstance(value, str) or EM_DASH not in value:
                    continue
                if cell.row == 1:
                    new = value.replace(f" {EM_DASH} ", ": ").replace(EM_DASH, ":")
                else:
                    new = value.replace(EM_DASH, "-")
                cell.value = _formula_safe(new)
                changed = True
    if not changed:
        return content
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
