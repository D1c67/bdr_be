"""Estimator workbook parser (AI_ESTIMATING_BUILD_SPEC WS6, `estimate_parse`).

Reads the "<PROJECT> - ELECTRICAL ESTIMATE.xlsx" workbooks the external
estimators send (the layout `plans_reader_render.render_estimate_xlsx`
reproduces): one or more detail sheets whose header row carries UNIT LABOR
HOURS, a LABOR RATE cell above it, and a "Bid Recap & Summary" sheet with one
row per section. Pure openpyxl over bytes: no I/O beyond the bytes, no
database, no model.

Cells are found by their labels, never by fixed addresses: the header row is
the first row (within the first 40) with a UNIT LABOR HOURS label, and the
columns are mapped from the header labels, with the template letters (D base
quantity, E wastage, F quantity with wastage, G unit, H unit labor hours, I
unit material cost, N unit item cost, O total item cost) as the fallback for a
label that is absent. One sent workbook drops the DRAWING SHEET# column, which
shifts every letter by one; mapping by label handles it.

The right-hand summary block of the detail sheet (TOTAL MATERIAL COST, TOTAL
LABOR COST, ALLOWANCES, TOTAL COST, TOTAL LABOR HOURS) and the recap sheet
(per-section MATERIAL COST .. TOTAL PRICE, TOTALS, BID SUMMARY rates and BASE
BID PRICE) are read when present and reported as warnings when absent.

`choose_book` applies the one-book rule of spec 8.8: per project the latest
Rev workbook, else the one with the most lines. `price_rows` turns a parsed
book into the engine's `PriceRow`s so the eval harness can price the
estimator's own quantities (LOPO, counterfactuals).

`dry_run` parses every estimate workbook of a production backup on disk and
explains each failure; `scripts/pricing_eval.py --mode parse-dryrun` writes
its report under EVAL_ROOT.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import openpyxl

from app.services.electrical_items import (
    BOQ_GROUPS,
    BOQ_LEVEL0,
    _canonical,
    _heading_name,
    _KNOWN_SUBGROUPS,
    _SUBGROUP_PARENT,
    normalize_unit,
)
from app.services.pricing_types import Allowance, PriceRow

PARSER_VERSION = 1

HEADER_SCAN_ROWS = 40
HEADER_KEY = "UNIT LABOR HOURS"
# Header label (normalized) -> column key. Prefix match, longest label first.
HEADER_LABELS: tuple[tuple[str, str], ...] = (
    ("DRAWING SHEET", "sheet_refs"),
    ("DESCRIPTION", "description"),
    ("QTY WITH WASTAGE", "quantity"),
    ("QUANTITY", "qty_base"),
    ("WASTAGE", "wastage"),
    ("UNIT LABOR HOURS", "unit_labor_hours"),
    ("UNIT MATERIAL COST", "unit_material_cost"),
    ("UNIT LABOR COST", "unit_labor_cost"),
    ("TOTAL LABOR HOURS", "total_labor_hours"),
    ("TOTAL MATERIAL COST", "total_material_cost"),
    ("TOTAL LABOR COST", "total_labor_cost"),
    ("UNIT ITEM COST", "unit_cost"),
    ("TOTAL ITEM COST", "total_cost"),
    ("UNIT", "unit"),
)
# Template letters (1-based column numbers) used when a label is missing.
TEMPLATE_COLUMNS: dict[str, int] = {
    "sheet_refs": 2,
    "description": 3,
    "qty_base": 4,
    "wastage": 5,
    "quantity": 6,
    "unit": 7,
    "unit_labor_hours": 8,
    "unit_material_cost": 9,
    "unit_labor_cost": 10,
    "total_labor_hours": 11,
    "total_material_cost": 12,
    "total_labor_cost": 13,
    "unit_cost": 14,
    "total_cost": 15,
}
# Right-hand summary labels of the detail sheet (normalized) -> key.
SUMMARY_LABELS: dict[str, str] = {
    "TOTAL MATERIAL COST": "total_material_cost",
    "TOTAL LABOR COST": "total_labor_cost",
    "ALLOWANCES": "allowances",
    "TOTAL COST": "total_cost",
    "TOTAL LABOR HOURS": "total_labor_hours",
}
LABOR_RATE_LABEL = "LABOR RATE"
SUBTOTAL_LABELS = ("SUBTOTAL MATERIAL", "SUBTOTAL LABOR", "SUBTOTAL")
ALLOWANCES_HEADING = "ALLOWANCES"

# Recap sheet: cost column labels (normalized) -> RecapRow field.
RECAP_COLUMNS: dict[str, str] = {
    "MATERIAL COST": "material",
    "LABOR COST": "labor",
    "MATERIAL TAX": "material_tax",
    "LABOR TAX": "labor_tax",
    "TOTAL COST": "total_cost",
    "OHP (LABOR)": "ohp_labor",
    "OHP (MATERIAL)": "ohp_material",
    "TOTAL PRICE": "price",
}
RECAP_TOTALS_LABEL = "TOTALS"
# BID SUMMARY labels (normalized prefix) -> key; the rate sits in the cell
# before the value when the row carries two numbers.
RECAP_SUMMARY_LABELS: tuple[tuple[str, str], ...] = (
    ("TOTAL MATERIAL COST", "sum_material"),
    ("MATERIAL SALES TAX", "material_tax"),
    ("TOTAL LABOR COST", "sum_labor"),
    ("LABOR TAX", "labor_tax"),
    ("TOTAL COST WITH OVERHEADS", "total_with_ohp"),
    ("TOTAL COST", "total_cost"),
    ("OVERHEAD & PROFIT (LABOR)", "ohp_labor"),
    ("OVERHEAD & PROFIT (MATERIAL)", "ohp_material"),
    ("BID SECURITY", "bid_security"),
    ("BASE BID PRICE", "base_bid"),
)
_REV_RE = re.compile(r"\bREV(?:ISION|ISED)?\b", re.IGNORECASE)
_WORKBOOK_SUFFIXES = (".xlsx", ".xlsm")


class EstimateParseError(ValueError):
    """The workbook does not carry a parseable estimate; the message says why."""


# Shapes -------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedLine:
    """One item row of a detail sheet. Quantities are Decimals as read; the
    unit is the estimator's spelling normalized through `normalize_unit`."""

    sheet: str
    row: int
    section: str
    group: str
    subgroup: str
    description: str
    qty_base: Decimal | None
    wastage: Decimal
    quantity: Decimal | None
    unit: str
    unit_labor_hours: Decimal | None
    unit_material_cost: Decimal | None
    unit_cost: Decimal | None = None
    total_cost: Decimal | None = None
    is_allowance: bool = False
    sheet_refs: tuple[str, ...] = ()

    @property
    def quantity_eff(self) -> Decimal | None:
        """The wastage-inclusive quantity: column F when present, else D x (1 + E)."""
        if self.quantity is not None:
            return self.quantity
        if self.qty_base is None:
            return None
        return self.qty_base * (Decimal(1) + self.wastage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "row": self.row,
            "section": self.section,
            "group": self.group,
            "subgroup": self.subgroup,
            "description": self.description,
            "qty_base": _s(self.qty_base),
            "wastage": _s(self.wastage),
            "quantity": _s(self.quantity),
            "unit": self.unit,
            "unit_labor_hours": _s(self.unit_labor_hours),
            "unit_material_cost": _s(self.unit_material_cost),
            "unit_cost": _s(self.unit_cost),
            "total_cost": _s(self.total_cost),
            "is_allowance": self.is_allowance,
            "sheet_refs": list(self.sheet_refs),
        }


@dataclass
class RecapRow:
    """One labeled row of the Bid Recap (a section or the TOTALS row)."""

    label: str
    material: Decimal = Decimal(0)
    labor: Decimal = Decimal(0)
    material_tax: Decimal = Decimal(0)
    labor_tax: Decimal = Decimal(0)
    total_cost: Decimal = Decimal(0)
    ohp_labor: Decimal = Decimal(0)
    ohp_material: Decimal = Decimal(0)
    price: Decimal = Decimal(0)

    def add(self, other: RecapRow) -> None:
        for key in RECAP_COLUMNS.values():
            setattr(self, key, getattr(self, key) + getattr(other, key))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label}
        out.update({key: _s(getattr(self, key)) for key in RECAP_COLUMNS.values()})
        return out


@dataclass
class ParsedEstimate:
    """A parsed estimator workbook."""

    sheets: list[str]
    labor_rate: Decimal | None
    workbook_date: dt.date | None
    lines: list[ParsedLine]
    recap_sections: dict[str, RecapRow] = field(default_factory=dict)
    recap_totals: RecapRow | None = None
    recap_summary: dict[str, Decimal] = field(default_factory=dict)
    ohp_labor_pct: Decimal | None = None
    ohp_material_pct: Decimal | None = None
    material_tax_pct: Decimal | None = None
    labor_tax_pct: Decimal | None = None
    base_bid: Decimal | None = None
    summary: dict[str, Decimal] = field(default_factory=dict)
    subtotals: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    parser_version: int = PARSER_VERSION

    @property
    def item_lines(self) -> list[ParsedLine]:
        return [ln for ln in self.lines if not ln.is_allowance]

    @property
    def allowance_lines(self) -> list[ParsedLine]:
        return [ln for ln in self.lines if ln.is_allowance]

    @property
    def total_labor_hours(self) -> Decimal | None:
        """The workbook's TOTAL LABOR HOURS when it carries one, else the sum
        of unit hours x wastage-inclusive quantity over the item lines."""
        got = self.summary.get("total_labor_hours")
        if got is not None:
            return got
        return self.basis_hours()

    def basis_hours(self) -> Decimal:
        """Sum of unit labor hours x quantity (with wastage) over item lines."""
        total = Decimal(0)
        for ln in self.item_lines:
            q = ln.quantity_eff
            if q is not None and ln.unit_labor_hours is not None:
                total += ln.unit_labor_hours * q
        return total

    def material_total(self) -> Decimal:
        """Sum of unit material cost x quantity (with wastage) over item lines."""
        total = Decimal(0)
        for ln in self.item_lines:
            q = ln.quantity_eff
            if q is not None and ln.unit_material_cost is not None:
                total += ln.unit_material_cost * q
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "parser_version": self.parser_version,
            "sheets": list(self.sheets),
            "labor_rate": _s(self.labor_rate),
            "workbook_date": self.workbook_date.isoformat() if self.workbook_date else None,
            "lines": [ln.to_dict() for ln in self.lines],
            "recap_sections": {k: v.to_dict() for k, v in self.recap_sections.items()},
            "recap_totals": self.recap_totals.to_dict() if self.recap_totals else None,
            "recap_summary": {k: _s(v) for k, v in self.recap_summary.items()},
            "ohp_labor_pct": _s(self.ohp_labor_pct),
            "ohp_material_pct": _s(self.ohp_material_pct),
            "material_tax_pct": _s(self.material_tax_pct),
            "labor_tax_pct": _s(self.labor_tax_pct),
            "base_bid": _s(self.base_bid),
            "summary": {k: _s(v) for k, v in self.summary.items()},
            "subtotals": {k: {kk: _s(vv) for kk, vv in v.items()} for k, v in self.subtotals.items()},
            "warnings": list(self.warnings),
        }


# Helpers ------------------------------------------------------------------------


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def _label(value: Any) -> str:
    """Normalized label text for comparisons: upper, collapsed spaces, no
    trailing punctuation or colon."""
    return _text(value).upper().rstrip(" :.")


def _num(value: Any) -> Decimal | None:
    """A Decimal from a numeric cell (str through Decimal(str)); text, blanks,
    booleans and non-finite values give None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            out = Decimal(str(value))
        except InvalidOperation:
            return None
        return out if out.is_finite() else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, str):
        txt = value.strip().replace(",", "").replace("$", "")
        if not txt or txt.startswith("="):
            return None
        try:
            out = Decimal(txt)
        except InvalidOperation:
            return None
        return out if out.is_finite() else None
    return None


def _date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %B %Y", "%B %d, %Y"):
            try:
                return dt.datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def _open(xlsx_bytes: bytes):
    try:
        return openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    except Exception as exc:  # openpyxl raises many types for a bad file
        raise EstimateParseError(f"workbook could not be opened: {exc}") from exc


def _header_row(ws) -> tuple[int, dict[str, int]] | None:
    """(row, {column key: 1-based column}) of the first header row carrying
    UNIT LABOR HOURS, columns mapped by label with the template fallback."""
    last = min(ws.max_row or 0, HEADER_SCAN_ROWS)
    for r in range(1, last + 1):
        labels = {c: _label(ws.cell(r, c).value) for c in range(1, (ws.max_column or 0) + 1)}
        if not any(HEADER_KEY in v for v in labels.values()):
            continue
        cols: dict[str, int] = {}
        for c, lab in labels.items():
            if not lab:
                continue
            for prefix, key in HEADER_LABELS:
                if lab.startswith(prefix) and key not in cols:
                    cols[key] = c
                    break
        taken = set(cols.values())
        for key, col in TEMPLATE_COLUMNS.items():
            if key in cols:
                continue
            if key == "sheet_refs" and col in taken:
                continue  # a sheet without DRAWING SHEET# has no refs column
            cols[key] = col
        return r, cols
    return None


def _find_label_value(ws, label: str, *, max_row: int) -> Decimal | None:
    """The first number to the right of the first cell equal to `label`
    within rows 1..max_row."""
    for r in range(1, max_row + 1):
        for c in range(1, (ws.max_column or 0) + 1):
            if _label(ws.cell(r, c).value) == label:
                for cc in range(c + 1, (ws.max_column or 0) + 1):
                    got = _num(ws.cell(r, cc).value)
                    if got is not None:
                        return got
                return None
    return None


def _find_date(ws, *, max_row: int) -> dt.date | None:
    for r in range(1, max_row + 1):
        for c in range(1, (ws.max_column or 0) + 1):
            if _label(ws.cell(r, c).value).startswith("DATE"):
                for cc in range(c, (ws.max_column or 0) + 1):
                    got = _date(ws.cell(r, cc).value)
                    if got is not None:
                        return got
    return None


def _sheet_refs(value: Any) -> tuple[str, ...]:
    txt = _text(value)
    if not txt:
        return ()
    return tuple(p.strip() for p in re.split(r"[,;]", txt) if p.strip())


# Detail sheets --------------------------------------------------------------------


def _parse_detail_sheet(ws, header: int, cols: dict[str, int], parsed: ParsedEstimate) -> int:
    """Walk one detail sheet below its header; append lines, subtotals and the
    right-hand summary to `parsed`. Returns the number of lines added."""
    level0 = set(BOQ_LEVEL0)
    groups = set(BOQ_GROUPS)
    section, group, subgroup = "NEW WORK", "", ""
    c_desc, c_unit = cols["description"], cols["unit"]
    max_col = ws.max_column or 0
    added = 0
    seen_summary: set[str] = set()
    pending_subtotal: tuple[str, dict[str, int]] | None = None
    for r in range(header + 1, (ws.max_row or 0) + 1):
        raw_desc = ws.cell(r, c_desc).value
        desc = _text(raw_desc)
        # Subtotal value row: numbers under the SUBTOTAL labels of the row above.
        if pending_subtotal is not None:
            label_group, label_cols = pending_subtotal
            values = {k: _num(ws.cell(r, c).value) for k, c in label_cols.items()}
            parsed.subtotals[label_group] = {k: v for k, v in values.items() if v is not None}
            pending_subtotal = None
        # Right-hand summary labels (to the right of the UNIT column); a
        # multi-area workbook carries one block per sheet and they add up.
        for c in range(c_unit + 1, max_col + 1):
            lab = _label(ws.cell(r, c).value)
            key = SUMMARY_LABELS.get(lab)
            if key is None or key in seen_summary:
                continue
            for cc in range(c + 1, max_col + 1):
                got = _num(ws.cell(r, cc).value)
                if got is not None:
                    seen_summary.add(key)
                    parsed.summary[key] = parsed.summary.get(key, Decimal(0)) + got
                    break
        # SUBTOTAL label row (labels in P/Q/R or wherever they sit).
        label_cols = {}
        for c in range(c_unit + 1, max_col + 1):
            lab = _label(ws.cell(r, c).value)
            if lab in SUBTOTAL_LABELS:
                label_cols[{"SUBTOTAL MATERIAL": "material", "SUBTOTAL LABOR": "labor",
                            "SUBTOTAL": "total"}[lab]] = c
        if label_cols:
            pending_subtotal = (f"{ws.title}:{group or section}", label_cols)
        if not desc:
            continue
        qty_base = _num(ws.cell(r, cols["qty_base"]).value)
        unit = normalize_unit(ws.cell(r, c_unit).value)
        hours = _num(ws.cell(r, cols["unit_labor_hours"]).value)
        name, _note = _heading_name(raw_desc)
        canon = _canonical(name)
        in_vocab = canon in level0 or canon in groups or canon in _KNOWN_SUBGROUPS
        is_heading = qty_base is None and not unit and hours is None
        if is_heading or (in_vocab and qty_base is None and not unit):
            if canon in level0:
                section, group, subgroup = canon, "", ""
            elif canon in groups or canon == ALLOWANCES_HEADING:
                group, subgroup = canon, ""
            elif canon in _KNOWN_SUBGROUPS:
                subgroup = name
            elif group:
                subgroup = name
            else:
                group, subgroup = name, ""
            continue
        if qty_base is None and not unit:
            continue  # a note row
        this_group = group
        if not this_group and subgroup:
            csub = _canonical(subgroup)
            this_group = next((g for pre, g in _SUBGROUP_PARENT if csub.startswith(pre)), "")
        if not this_group and section == "DEMOLITION":
            this_group = "DEMOLITION"
        is_allowance = _canonical(group) == ALLOWANCES_HEADING or unit == "LS"
        wastage = _num(ws.cell(r, cols["wastage"]).value) or Decimal(0)
        if wastage > 1:  # a percent written as 10 instead of 0.1
            wastage = wastage / Decimal(100)
        parsed.lines.append(
            ParsedLine(
                sheet=ws.title,
                row=r,
                section=section,
                group=this_group,
                subgroup=subgroup,
                description=desc,
                qty_base=qty_base,
                wastage=wastage,
                quantity=_num(ws.cell(r, cols["quantity"]).value),
                unit=unit,
                unit_labor_hours=hours,
                unit_material_cost=_num(ws.cell(r, cols["unit_material_cost"]).value),
                unit_cost=_num(ws.cell(r, cols["unit_cost"]).value),
                total_cost=_num(ws.cell(r, cols["total_cost"]).value),
                is_allowance=is_allowance,
                sheet_refs=(
                    _sheet_refs(ws.cell(r, cols["sheet_refs"]).value) if "sheet_refs" in cols else ()
                ),
            )
        )
        added += 1
    return added


# Recap sheet ----------------------------------------------------------------------


def _recap_sheet(wb):
    for ws in wb.worksheets:
        if "RECAP" in ws.title.upper():
            return ws
    return None


def _parse_recap(ws, parsed: ParsedEstimate) -> None:
    max_row, max_col = ws.max_row or 0, ws.max_column or 0
    header = None
    cols: dict[str, int] = {}
    for r in range(1, min(max_row, HEADER_SCAN_ROWS) + 1):
        labels = {c: _label(ws.cell(r, c).value) for c in range(1, max_col + 1)}
        if "MATERIAL COST" in labels.values() and "LABOR COST" in labels.values():
            header = r
            for c, lab in labels.items():
                key = RECAP_COLUMNS.get(lab)
                if key and key not in cols:
                    cols[key] = c
                if lab == "DESCRIPTION":
                    cols["description"] = c
            break
    if header is None:
        parsed.warnings.append("recap: no header row with MATERIAL COST and LABOR COST")
        return
    c_desc = cols.get("description", 3)
    in_summary = False
    for r in range(header + 1, max_row + 1):
        label = _label(ws.cell(r, c_desc).value)
        if not label:
            # BID SUMMARY title sits one column left of the description column.
            if any(_label(ws.cell(r, c).value) == "BID SUMMARY" for c in range(1, max_col + 1)):
                in_summary = True
            continue
        if label == "BID SUMMARY":
            in_summary = True
            continue
        if in_summary:
            key = next((k for pre, k in RECAP_SUMMARY_LABELS if label.startswith(pre)), None)
            if key is None:
                continue
            nums = [
                _num(ws.cell(r, c).value) for c in range(c_desc + 1, max_col + 1)
            ]
            nums = [n for n in nums if n is not None]
            if not nums:
                continue
            parsed.recap_summary[key] = nums[-1]
            if len(nums) >= 2 and key in ("material_tax", "labor_tax", "ohp_labor", "ohp_material"):
                rate = nums[-2]
                pct = rate * 100 if rate <= 1 else rate
                setattr(parsed, {"material_tax": "material_tax_pct", "labor_tax": "labor_tax_pct",
                                 "ohp_labor": "ohp_labor_pct", "ohp_material": "ohp_material_pct"}[key], pct)
            if key == "base_bid":
                parsed.base_bid = nums[-1]
            continue
        row = RecapRow(label=label)
        any_number = False
        for key, c in cols.items():
            if key == "description":
                continue
            got = _num(ws.cell(r, c).value)
            if got is not None:
                any_number = True
                setattr(row, key, got)
        if not any_number:
            continue
        if label == RECAP_TOTALS_LABEL:
            parsed.recap_totals = row
            continue
        if label in parsed.recap_sections:
            parsed.recap_sections[label].add(row)  # multi-area recaps repeat labels
        else:
            parsed.recap_sections[label] = row


# Public API -----------------------------------------------------------------------


def parse_estimate(xlsx_bytes: bytes) -> ParsedEstimate:
    """Parse one estimator workbook. Raises EstimateParseError when no sheet
    carries a UNIT LABOR HOURS header (nothing to price); every other gap is
    a warning on the result."""
    wb = _open(xlsx_bytes)
    try:
        parsed = ParsedEstimate(sheets=[], labor_rate=None, workbook_date=None, lines=[])
        for ws in wb.worksheets:
            found = _header_row(ws)
            if found is None:
                continue
            header, cols = found
            parsed.sheets.append(ws.title)
            if parsed.labor_rate is None:
                parsed.labor_rate = _find_label_value(ws, LABOR_RATE_LABEL, max_row=header - 1)
            if parsed.workbook_date is None:
                parsed.workbook_date = _find_date(ws, max_row=header - 1)
            n = _parse_detail_sheet(ws, header, cols, parsed)
            if n == 0:
                parsed.warnings.append(f"sheet {ws.title!r}: header found but no item rows")
        if not parsed.sheets:
            raise EstimateParseError(
                f"no detail sheet with a {HEADER_KEY} header (sheets: {', '.join(wb.sheetnames)})"
            )
        if parsed.labor_rate is None:
            parsed.warnings.append("no LABOR RATE cell above the header")
        if parsed.workbook_date is None:
            parsed.warnings.append("no DATE cell above the header")
        recap = _recap_sheet(wb)
        if recap is None:
            parsed.warnings.append("no recap sheet (title containing RECAP)")
        else:
            _parse_recap(recap, parsed)
            if not parsed.recap_sections:
                parsed.warnings.append("recap: no section rows")
        if not parsed.item_lines:
            parsed.warnings.append("no item lines (only headings or allowances)")
        return parsed
    finally:
        wb.close()


def is_rev_book(filename: str) -> bool:
    return bool(_REV_RE.search(filename or ""))


def choose_book(books: Sequence[tuple[dict[str, Any], ParsedEstimate]]) -> int | None:
    """One-book rule (spec 8.8): the index of the latest Rev workbook (by
    `created_at`, then `submission_round`), else the book with the most item
    lines (ties: the latest). `books` pairs a project_files row (filename,
    created_at, submission_round) with its parse. None for an empty list."""
    if not books:
        return None

    def when(meta: dict[str, Any]) -> tuple[str, int]:
        return (str(meta.get("created_at") or ""), int(meta.get("submission_round") or 0))

    revs = [i for i, (meta, _p) in enumerate(books) if is_rev_book(str(meta.get("filename") or ""))]
    if revs:
        return max(revs, key=lambda i: when(books[i][0]))
    return max(range(len(books)), key=lambda i: (len(books[i][1].item_lines), when(books[i][0])))


def price_rows(parsed: ParsedEstimate, *, prefix: str = "B") -> list[PriceRow]:
    """The engine's rows for the estimator's own quantities: one PriceRow per
    item line with a positive quantity, ids B1..Bn in workbook order. The
    quantity is the wastage-inclusive one; qty_base is column D."""
    rows: list[PriceRow] = []
    n = 0
    for ln in parsed.item_lines:
        qty = ln.quantity_eff
        if qty is None or qty <= 0:
            continue
        n += 1
        rows.append(
            PriceRow(
                line_id=f"{prefix}{n}",
                description=ln.description,
                group=ln.group,
                subgroup=ln.subgroup or None,
                section=ln.section,
                unit=ln.unit or "EA",
                quantity=qty,
                qty_base=ln.qty_base if ln.qty_base is not None else qty,
                wastage_applies=ln.wastage > 0,
                sheet_refs=ln.sheet_refs,
                quantity_basis="manual",
            )
        )
    return rows


def allowances(parsed: ParsedEstimate) -> tuple[Allowance, ...]:
    """The workbook's allowance rows as engine allowances (amount from the
    quantity column when it carries a number, else 0)."""
    out: list[Allowance] = []
    for ln in parsed.allowance_lines:
        amount = ln.qty_base if ln.qty_base is not None else Decimal(0)
        out.append(Allowance(name=ln.description, amount_usd=amount))
    return tuple(out)


# Backup population and dry run -------------------------------------------------------


@dataclass(frozen=True)
class EstimateFile:
    """One estimate workbook of a production backup on disk."""

    project_id: str
    file_id: str
    filename: str
    path: Path
    created_at: str
    submission_round: int | None
    estimator_deliverable: bool

    @property
    def meta(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "created_at": self.created_at,
            "submission_round": self.submission_round,
        }


def estimate_files(backup_root: str | Path) -> list[EstimateFile]:
    """Every `estimate` workbook (.xlsx/.xlsm) of the backup that exists on
    disk, mapped through data/project_files.json (never by folder)."""
    root = Path(backup_root)
    with open(root / "data" / "project_files.json", encoding="utf-8") as fh:
        rows = json.load(fh)
    out: list[EstimateFile] = []
    for row in rows:
        if row.get("category") != "estimate":
            continue
        name = str(row.get("filename") or "")
        if not name.lower().endswith(_WORKBOOK_SUFFIXES):
            continue
        path = root / "storage" / "project-files" / str(row.get("storage_path") or "")
        if not path.is_file():
            continue
        out.append(
            EstimateFile(
                project_id=str(row["project_id"]),
                file_id=str(row["id"]),
                filename=name,
                path=path,
                created_at=str(row.get("created_at") or ""),
                submission_round=row.get("submission_round"),
                estimator_deliverable=bool(row.get("estimator_deliverable")),
            )
        )
    out.sort(key=lambda f: (f.project_id, f.created_at, f.filename))
    return out


@dataclass
class DryRunEntry:
    file: EstimateFile
    ok: bool
    error: str | None
    sheets: int = 0
    lines: int = 0
    allowance_rows: int = 0
    labor_rate: bool = False
    workbook_date: bool = False
    recap_sections: int = 0
    warnings: list[str] = field(default_factory=list)
    chosen: bool = False


@dataclass
class DryRunReport:
    entries: list[DryRunEntry]

    @property
    def parsed(self) -> int:
        return sum(1 for e in self.entries if e.ok)

    @property
    def failed(self) -> list[DryRunEntry]:
        return [e for e in self.entries if not e.ok]

    @property
    def projects(self) -> int:
        return len({e.file.project_id for e in self.entries})


def parse_backup(backup_root: str | Path) -> tuple[DryRunReport, dict[str, ParsedEstimate]]:
    """Parse every estimate workbook of the backup. Returns the dry-run report
    and the chosen book per project (one-book rule)."""
    entries: list[DryRunEntry] = []
    by_project: dict[str, list[tuple[DryRunEntry, ParsedEstimate]]] = {}
    for f in estimate_files(backup_root):
        try:
            parsed = parse_estimate(f.path.read_bytes())
        except EstimateParseError as exc:
            entries.append(DryRunEntry(file=f, ok=False, error=str(exc)))
            continue
        except Exception as exc:  # a corrupt file is explained, never fatal
            entries.append(DryRunEntry(file=f, ok=False, error=f"{type(exc).__name__}: {exc}"))
            continue
        entry = DryRunEntry(
            file=f,
            ok=True,
            error=None,
            sheets=len(parsed.sheets),
            lines=len(parsed.item_lines),
            allowance_rows=len(parsed.allowance_lines),
            labor_rate=parsed.labor_rate is not None,
            workbook_date=parsed.workbook_date is not None,
            recap_sections=len(parsed.recap_sections),
            warnings=list(parsed.warnings),
        )
        entries.append(entry)
        by_project.setdefault(f.project_id, []).append((entry, parsed))
    chosen: dict[str, ParsedEstimate] = {}
    for pid, items in by_project.items():
        idx = choose_book([(e.file.meta, p) for e, p in items])
        if idx is None:
            continue
        items[idx][0].chosen = True
        chosen[pid] = items[idx][1]
    return DryRunReport(entries=entries), chosen


def dry_run_markdown(report: DryRunReport, *, generated_at: str) -> str:
    """The dry-run report (no unit prices or totals; counts only)."""
    lines = [
        "# Estimate workbook parse dry run",
        "",
        f"Generated {generated_at} (America/Los_Angeles). Parser version {PARSER_VERSION}.",
        "",
        f"Workbooks: {len(report.entries)} over {report.projects} projects; "
        f"parsed {report.parsed}; failed {len(report.failed)}.",
        "",
        "| Project | File | Sheets | Lines | Allow. | Rate | Date | Recap rows | Book | Warnings |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for e in report.entries:
        if not e.ok:
            continue
        lines.append(
            f"| {e.file.project_id[:8]} | {e.file.filename} | {e.sheets} | {e.lines} | "
            f"{e.allowance_rows} | {'yes' if e.labor_rate else 'no'} | "
            f"{'yes' if e.workbook_date else 'no'} | {e.recap_sections} | "
            f"{'chosen' if e.chosen else ''} | {'; '.join(e.warnings)} |"
        )
    lines.append("")
    lines.append("## Failures")
    lines.append("")
    if not report.failed:
        lines.append("None.")
    for e in report.failed:
        lines.append(f"- {e.file.project_id[:8]} {e.file.filename}: {e.error}")
    return "\n".join(lines) + "\n"


def iter_lines(parsed: Iterable[ParsedEstimate]) -> Iterable[ParsedLine]:
    for p in parsed:
        yield from p.item_lines
