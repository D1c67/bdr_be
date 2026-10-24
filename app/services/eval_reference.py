"""Ground-truth references for the AI Estimating eval (WS0, spec 8.6).

`build_reference(case_id, project_id, backup_dir)` reads the production
backup on disk (data/<table>.json plus storage/project-files) and writes the
truth of one case as a plain dict (`reference.json`):

- the estimator BOQ (category `boq`, latest by created_at) parsed with
  `plans_reader_score.parse_estimator_boq`;
- the estimator estimate workbook (category `estimate`, one-book rule of
  8.8: latest Rev, else the most lines) read by the minimal reader below and
  joined to the BOQ lines (exact normalized description, then similarity
  with equal unit and quantity within 0.5%); the line value v of 3.6 is
  qty x (unit material + unit hours x the workbook labor rate);
- the Bid Recap sections (own reader) cross-checked with
  `auto_send_estimate.parse_recap`;
- what G3 sent (`proposal_sends` with status sent, latest sent_at, the
  spread across GCs reported), the committed Verify (the ten
  VERIFY_KEYS), the labor review notes ("Labor Hours = 625 / Labor Rate =
  $125"), the vendor quotes joined through rfqs and material_categories,
  the General Material row, the project conditions and GCs;
- adjudications from `references/<case>/adjudications.json` applied with
  their reviewed flag (an unreviewed adjudication is applied and reported
  as unreviewed; nothing in this module marks one reviewed);
- data-quality checks (Verify sum equals the sent total, materials equal
  the taxed selected quotes, the BOQ-to-estimate join rate, recap cost
  equals the line totals, labor notes parsed).

Hook for the integrator: WS6 builds `estimate_parse` in parallel; when it
lands, `parse_estimate_lines` here becomes a thin adapter over it (the
column names in EstimateLine match the WS6 design: base qty, wastage, qty
with wastage, unit, unit hours, unit material, unit item cost, total).

Everything with dollars stays under EVAL_ROOT. This module never touches
a database or a model, and committed tests feed it synthetic workbooks.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import openpyxl

from app.services.auto_send_estimate import EstimateError, parse_recap
from app.services.electrical_items import (
    BOQ_GROUPS,
    BOQ_LEVEL0,
    BoqRow,
    _canonical,
    _clean_text,
    _heading_name,
    _KNOWN_SUBGROUPS,
    normalize_description,
    normalize_unit,
    similarity,
)
from app.services.estimating_contracts import VERIFY_KEYS
from app.services.plans_reader_score import (
    DEFAULT_BACKUP_DIR,
    classify_line,
    merge_aliases,
    parse_estimator_boq,
)

REFERENCE_SCHEMA = "bdr.eval.reference/1"
REFERENCE_BUILDER_VERSION = 2  # 2: labor hours kept whenever both regexes parse; amount_consistent reported apart
ADJUDICATION_TYPES: tuple[str, ...] = (
    "add_line",
    "remove_line",
    "set_qty",
    "set_unit",
    "set_unit_cost",
    "price_truth",
    "alias",
    "note",
)
COMPANY_TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_TAX_RATE_PCT = 8.375
SENT_AMOUNT_KEYS: tuple[str, ...] = (
    "labor_amount",
    "material_amount",
    "gear_amount",
    "underground_amount",
    "low_voltage_amount",
)
VERIFY_MATERIAL_KEYS: tuple[str, ...] = (
    "materials_amount",
    "gear_amount",
    "underground_amount",
    "low_voltage_amount",
)
# Material category name -> estimate recap section whose material it replaces.
QUOTE_SECTION_MAP: dict[str, str] = {
    "LIGHTING": "LIGHTING FIXTURES",
    "SWITCHGEAR": "SWITCHGEAR",
    "GENERATOR & EQUIPMENT": "SWITCHGEAR",
    "TRENCHING": "TRENCH AND UNDERGROUND CONDUITS",
    "DEMOLITION": "DEMOLITION",
    "LOW VOLTAGE": "LOW VOLTAGE WORK",
    "EV CHARGERS": "EV CHARGERS",
}
JOIN_QTY_TOLERANCE = 0.005
JOIN_SIMILARITY = 0.9
_CENT = Decimal("0.01")
_ESTIMATE_HEADER_KEY = "UNIT LABOR HOURS"
_ESTIMATE_COLUMNS: dict[str, str] = {
    "SR. NO.": "sr",
    "SR. NO": "sr",
    "SR NO": "sr",
    "DRAWING SHEET#": "sheet",
    "DESCRIPTION": "description",
    "QUANTITY": "qty_base",
    "WASTAGE": "wastage",
    "QTY WITH WASTAGE": "qty",
    "UNIT": "unit",
    "UNIT LABOR HOURS": "unit_hours",
    "UNIT MATERIAL COST": "unit_material",
    "UNIT LABOR COST": "unit_labor_cost",
    "TOTAL LABOR HOURS": "total_hours",
    "TOTAL MATERIAL COST": "total_material",
    "TOTAL LABOR COST": "total_labor",
    "UNIT ITEM COST": "unit_item_cost",
    "TOTAL ITEM COST": "total_item_cost",
}
_ESTIMATE_TOTAL_LABELS: dict[str, str] = {
    "TOTAL MATERIAL COST": "material",
    "TOTAL LABOR COST": "labor",
    "ALLOWANCES": "allowances",
    "TOTAL COST": "cost",
    "TOTAL LABOR HOURS": "hours",
    "OVERHEAD & PROFIT (LABOR)": "ohp_labor",
    "OVERHEAD & PROFIT (MATERIAL)": "ohp_material",
    "TOTAL BID": "bid",
}
_RECAP_COLUMNS: dict[str, str] = {
    "DESCRIPTION": "description",
    "MATERIAL COST": "material",
    "LABOR COST": "labor",
    "MATERIAL TAX": "material_tax",
    "LABOR TAX": "labor_tax",
    "TOTAL COST": "cost",
    "OHP (LABOR)": "ohp_labor",
    "OHP (MATERIAL)": "ohp_material",
    "TOTAL PRICE": "total",
}
_HOURS_RE = re.compile(r"labou?r?e?\s*hours?\s*[=:]\s*\$?\s*([\d,]+(?:\.\d+)?)", re.I)
_HOURS_STRICT_RE = re.compile(r"Labor Hours = (\d+(\.\d+)?)")
_RATE_RE = re.compile(r"labou?r?e?\s*rate\s*[=:]\s*\$?\s*([\d,]+(?:\.\d+)?)", re.I)
_RATE_STRICT_RE = re.compile(r"Labor Rate = \$(\d+)")
_REV_RE = re.compile(r"\bREV\.?\s*(\d+)\b", re.I)


def _cents(value: Any) -> float:
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _norm_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\n", " ")).strip().upper().rstrip(":").strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_pacific_iso() -> str:
    return datetime.now(UTC).astimezone(COMPANY_TZ).isoformat(timespec="seconds")


def to_pacific_iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(COMPANY_TZ).isoformat(timespec="seconds")


# Backup tables ---------------------------------------------------------------------


class BackupData:
    """Lazy, cached readers over `backup_dir/data/<table>.json` and the
    storage tree. Read-only."""

    def __init__(self, backup_dir: Path | str = DEFAULT_BACKUP_DIR) -> None:
        self.backup_dir = Path(backup_dir)
        self.data_dir = self.backup_dir / "data"
        self.storage_dir = self.backup_dir / "storage" / "project-files"
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._by_project: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def table(self, name: str) -> list[dict[str, Any]]:
        if name not in self._tables:
            path = self.data_dir / f"{name}.json"
            rows = json.loads(path.read_text()) if path.exists() else []
            if isinstance(rows, dict):
                rows = rows.get("rows") or []
            self._tables[name] = rows
        return self._tables[name]

    def by_project(self, name: str, project_id: str) -> list[dict[str, Any]]:
        if name not in self._by_project:
            index: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in self.table(name):
                index[str(row.get("project_id"))].append(row)
            self._by_project[name] = index
        return list(self._by_project[name].get(project_id, ()))

    def by_id(self, name: str) -> dict[str, dict[str, Any]]:
        return {str(r.get("id")): r for r in self.table(name)}

    def project(self, project_id: str) -> dict[str, Any] | None:
        return self.by_id("projects").get(project_id)

    def resolve_project_id(self, prefix_or_id: str) -> str:
        """Full project id from an id or a unique prefix."""
        ids = [p["id"] for p in self.table("projects") if str(p["id"]).startswith(prefix_or_id)]
        if not ids:
            raise KeyError(f"no project id starts with {prefix_or_id!r}")
        if len(ids) > 1:
            raise ValueError(f"project prefix {prefix_or_id!r} is ambiguous")
        return ids[0]

    def file_path(self, file_row: Mapping[str, Any]) -> Path:
        return self.storage_dir / str(file_row["storage_path"])

    def files(self, project_id: str, category: str | None = None) -> list[dict[str, Any]]:
        rows = self.by_project("project_files", project_id)
        if category is not None:
            rows = [r for r in rows if r.get("category") == category]
        return sorted(rows, key=lambda r: str(r.get("created_at") or ""))


def _is_workbook(row: Mapping[str, Any]) -> bool:
    return str(row.get("filename", "")).lower().endswith((".xlsx", ".xlsm"))


def pick_boq_file(data: BackupData, project_id: str) -> dict[str, Any] | None:
    """Category boq, latest by created_at (8.6)."""
    rows = [r for r in data.files(project_id, "boq") if _is_workbook(r)]
    return rows[-1] if rows else None


def pick_estimate_file(data: BackupData, project_id: str) -> dict[str, Any] | None:
    """One-book rule (8.8): the latest Rev workbook, else the one with the
    most lines (ties go to the latest upload)."""
    rows = [r for r in data.files(project_id, "estimate") if _is_workbook(r)]
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    with_rev = [(int(_REV_RE.search(str(r["filename"])).group(1)), r) for r in rows if _REV_RE.search(str(r["filename"]))]
    if with_rev:
        best = max(with_rev, key=lambda t: (t[0], str(t[1].get("created_at") or "")))
        return best[1]
    counted: list[tuple[int, str, dict[str, Any]]] = []
    for r in rows:
        path = data.file_path(r)
        try:
            n = len(parse_estimate_lines(path.read_bytes()).lines)
        except Exception:  # noqa: BLE001
            n = -1
        counted.append((n, str(r.get("created_at") or ""), r))
    return max(counted, key=lambda t: (t[0], t[1]))[2]


# Estimate workbook (minimal reader; WS6 estimate_parse replaces it) -------------


@dataclass
class EstimateLine:
    area: str
    section: str
    group: str
    subgroup: str
    sr: float | None
    sheet_refs: str
    description: str
    qty_base: float | None
    wastage: float | None
    qty: float | None
    unit: str
    unit_hours: float | None
    unit_material: float | None
    unit_labor_cost: float | None
    total_hours: float | None
    total_material: float | None
    total_labor: float | None
    unit_item_cost: float | None
    total_item_cost: float | None
    ref: str


@dataclass
class EstimateBook:
    lines: list[EstimateLine]
    labor_rate: float | None
    totals: dict[str, float | None]
    sheets: list[str]
    warnings: list[str] = field(default_factory=list)


def _header_map(ws: Any) -> tuple[int, dict[str, int]] | None:
    for r in range(1, min(ws.max_row, 20) + 1):
        labels = {}
        for c in range(1, min(ws.max_column, 30) + 1):
            key = _ESTIMATE_COLUMNS.get(_norm_label(ws.cell(r, c).value))
            if key and key not in labels:
                labels[key] = c
        if "unit_hours" in labels and "description" in labels:
            return r, labels
    return None


def parse_estimate_lines(xlsx_bytes: bytes) -> EstimateBook:
    """Detailed Estimate (or per-area sheets) by the `UNIT LABOR HOURS`
    header; labor rate from the LABOR RATE label; the summary labels
    (TOTAL LABOR HOURS, TOTAL COST, TOTAL BID, ...) from the last sheet
    that carries them."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    lines: list[EstimateLine] = []
    labor_rate: float | None = None
    totals: dict[str, float | None] = {}
    sheets: list[str] = []
    warnings: list[str] = []
    level0 = set(BOQ_LEVEL0)
    groups = set(BOQ_GROUPS) | {"ALLOWANCES"}
    for ws in wb.worksheets:
        found = _header_map(ws)
        if found is None:
            continue
        header, cols = found
        sheets.append(ws.title)
        for r in range(1, header):
            for c in range(1, min(ws.max_column, 30) + 1):
                if _norm_label(ws.cell(r, c).value) == "LABOR RATE":
                    for cc in range(c + 1, min(ws.max_column, 30) + 1):
                        v = _f(ws.cell(r, cc).value)
                        if v is not None:
                            labor_rate = labor_rate or v
                            break
        dcol = cols["description"]
        section, group, subgroup = "NEW WORK", "", ""
        for r in range(header + 1, ws.max_row + 1):
            desc_raw = ws.cell(r, dcol).value
            desc = _clean_text(desc_raw)
            row_labels = {
                _norm_label(ws.cell(r, c).value): c
                for c in range(1, min(ws.max_column, 30) + 1)
                if isinstance(ws.cell(r, c).value, str)
            }
            for label, key in _ESTIMATE_TOTAL_LABELS.items():
                c = row_labels.get(label)
                if c is None or (c == dcol and key not in ("allowances",)):
                    continue
                nums = [_f(ws.cell(r, cc).value) for cc in range(c + 1, min(ws.max_column, 30) + 1)]
                nums = [n for n in nums if n is not None]
                if nums:
                    totals[key] = nums[-1]
                    if key in ("ohp_labor", "ohp_material") and len(nums) >= 2:
                        totals[f"{key}_pct"] = nums[-2]
            if not desc:
                continue
            qty_base = _f(ws.cell(r, cols["qty_base"]).value) if "qty_base" in cols else None
            qty = _f(ws.cell(r, cols["qty"]).value) if "qty" in cols else None
            unit = normalize_unit(ws.cell(r, cols["unit"]).value) if "unit" in cols else ""
            name, _note = _heading_name(desc_raw)
            canon = _canonical(name)
            if qty_base is None and qty is None and not unit:
                if canon in level0:
                    section, group, subgroup = canon, "", ""
                elif canon in groups:
                    group, subgroup = canon, ""
                elif canon in _KNOWN_SUBGROUPS or (desc.isupper() and len(desc) <= 80):
                    subgroup = name
                continue
            if qty_base is None and qty is None:
                continue

            def cell(key: str) -> float | None:
                return _f(ws.cell(r, cols[key]).value) if key in cols else None

            unit_hours = cell("unit_hours")
            unit_material = cell("unit_material")
            unit_item_cost = cell("unit_item_cost")
            if unit_item_cost is None and labor_rate is not None and (unit_hours is not None or unit_material is not None):
                unit_item_cost = (unit_material or 0.0) + (unit_hours or 0.0) * labor_rate
            lines.append(
                EstimateLine(
                    area=_clean_text(ws.title),
                    section=section,
                    group=group,
                    subgroup=subgroup,
                    sr=cell("sr"),
                    sheet_refs=_clean_text(ws.cell(r, cols["sheet"]).value) if "sheet" in cols else "",
                    description=desc,
                    qty_base=qty_base,
                    wastage=cell("wastage"),
                    qty=qty if qty is not None else qty_base,
                    unit=unit,
                    unit_hours=unit_hours,
                    unit_material=unit_material,
                    unit_labor_cost=cell("unit_labor_cost"),
                    total_hours=cell("total_hours"),
                    total_material=cell("total_material"),
                    total_labor=cell("total_labor"),
                    unit_item_cost=unit_item_cost,
                    total_item_cost=cell("total_item_cost"),
                    ref=f"{ws.title}!{openpyxl.utils.get_column_letter(dcol)}{r}",
                )
            )
    if not sheets:
        warnings.append("no sheet with a UNIT LABOR HOURS header")
    if labor_rate is None:
        warnings.append("labor rate label not found")
    return EstimateBook(lines=lines, labor_rate=labor_rate, totals=totals, sheets=sheets, warnings=warnings)


def parse_recap_sections(xlsx_bytes: bytes) -> dict[str, Any]:
    """Per-section rows of the Bid Recap (DESCRIPTION, MATERIAL COST, LABOR
    COST, OHP (LABOR), OHP (MATERIAL), TOTAL PRICE) plus the TOTALS row."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    out: dict[str, Any] = {"sheet": None, "sections": {}, "totals": {}}
    for ws in wb.worksheets:
        header: tuple[int, dict[str, int]] | None = None
        for r in range(1, min(ws.max_row, 15) + 1):
            cols: dict[str, int] = {}
            for c in range(1, min(ws.max_column, 20) + 1):
                key = _RECAP_COLUMNS.get(_norm_label(ws.cell(r, c).value))
                if key and key not in cols:
                    cols[key] = c
            if "description" in cols and "material" in cols and "total" in cols:
                header = (r, cols)
                break
        if header is None:
            continue
        hr, cols = header
        out["sheet"] = ws.title
        for r in range(hr + 1, ws.max_row + 1):
            name = _norm_label(ws.cell(r, cols["description"]).value)
            if not name:
                continue
            row = {k: _f(ws.cell(r, c).value) for k, c in cols.items() if k != "description"}
            if name == "TOTALS":
                out["totals"] = row
                break
            if any(v is not None for v in row.values()):
                out["sections"][name] = row
        break
    return out


# BOQ to estimate join ---------------------------------------------------------------


def join_boq_estimate(
    boq_rows: Sequence[BoqRow],
    est_lines: Sequence[EstimateLine],
    *,
    labor_rate: float | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Each BOQ row joined to at most one estimate line: exact normalized
    description with equal unit and quantity within 0.5% first, then v1
    similarity >= 0.9 with the same unit and quantity rule. Returns the
    reference quantity lines and warnings for unjoined rows."""
    boq_areas = {r.area for r in boq_rows}
    est_areas = {ln.area for ln in est_lines}
    compare_areas = len(boq_areas) > 1 and len(est_areas) > 1
    used: set[int] = set()
    warnings: list[str] = []
    out: list[dict[str, Any]] = []

    def qty_close(a: float | None, b: float) -> bool:
        if a is None:
            return False
        if b == 0:
            return a == 0
        return abs(a - b) <= max(JOIN_QTY_TOLERANCE * abs(b), 0.011)

    def qty_ok(ln: EstimateLine, b: float) -> bool:
        # BOQ quantities include wastage (column F); a few BOQs carry the base
        # quantity (column D) on lines outside the WIRING group, so both match.
        return qty_close(ln.qty, b) or qty_close(ln.qty_base, b)

    def candidates(row: BoqRow) -> Iterable[int]:
        for j, ln in enumerate(est_lines):
            if j in used:
                continue
            if compare_areas and normalize_description(ln.area) != normalize_description(row.area):
                continue
            if ln.unit != normalize_unit(row.unit):
                continue
            yield j

    for i, row in enumerate(boq_rows):
        nd = normalize_description(row.description)
        match: int | None = None
        for j in candidates(row):
            if normalize_description(est_lines[j].description) == nd and qty_ok(est_lines[j], row.quantity):
                match = j
                break
        if match is None:
            best: tuple[float, int] | None = None
            for j in candidates(row):
                ln = est_lines[j]
                if not qty_ok(ln, row.quantity):
                    continue
                s = similarity(row, BoqRow(ln.area, ln.section, ln.group, ln.subgroup, ln.description, ln.qty or 0.0, ln.unit))
                if s >= JOIN_SIMILARITY and (best is None or s > best[0]):
                    best = (s, j)
            if best is not None:
                match = best[1]
        est = est_lines[match] if match is not None else None
        if est is not None:
            used.add(match)
        elif not row.is_accessory:
            warnings.append(f"BOQ row not joined to an estimate line: {row.ref} {row.description[:60]}")
        unit_hours = est.unit_hours if est else None
        unit_material = est.unit_material if est else None
        rate = labor_rate
        unit_item_cost = None
        if est is not None and rate is not None and (unit_hours is not None or unit_material is not None):
            unit_item_cost = (unit_material or 0.0) + (unit_hours or 0.0) * rate
        elif est is not None:
            unit_item_cost = est.unit_item_cost
        key = classify_line(row.description, row.section, row.group, row.unit, subgroup=row.subgroup)
        out.append(
            {
                "line_id": f"Q{i + 1:03d}",
                "area": row.area,
                "section": row.section,
                "group": row.group,
                "subgroup": row.subgroup,
                "description": row.description,
                "kind": key.kind,
                "attrs": sorted(key.attrs),
                "unit": normalize_unit(row.unit),
                "qty": float(row.quantity),
                "qty_base": est.qty_base if est else None,
                "wastage": est.wastage if est else None,
                "unit_hours": unit_hours,
                "unit_material": unit_material,
                "unit_item_cost": unit_item_cost,
                "labor_rate": rate,
                "value": None if unit_item_cost is None else float(row.quantity) * unit_item_cost,
                "is_accessory": row.is_accessory,
                "boq_ref": row.ref,
                "est_ref": est.ref if est else None,
                "sheet_refs": est.sheet_refs if est else "",
            }
        )
    return out, warnings


# G3 truth ---------------------------------------------------------------------------


def parse_labor_notes(text: str | None) -> tuple[float | None, float | None]:
    """(hours, rate) from a labor review note such as
    "Labor Hours = 625 / Labor Rate = $125". The strict spec form is tried
    first, then a lenient spelling-tolerant form; None when absent."""
    if not text:
        return None, None
    hours: float | None = None
    rate: float | None = None
    m = _HOURS_STRICT_RE.search(text) or _HOURS_RE.search(text)
    if m:
        hours = _f(m.group(1))
    m = _RATE_STRICT_RE.search(text) or _RATE_RE.search(text)
    if m:
        rate = _f(m.group(1))
    return hours, rate


def sent_total(row: Mapping[str, Any]) -> float:
    return _cents(sum(float(row.get(k) or 0) for k in SENT_AMOUNT_KEYS))


def verify_total(row: Mapping[str, Any]) -> float:
    return _cents(sum(float(row.get(k) or 0) for k in VERIFY_KEYS))


def taxed(amount: float, tax_included: bool | None, tax_rate_pct: float | None) -> float:
    """A quote amount with tax applied the way Verify does (cents per category)."""
    if tax_included:
        return _cents(amount)
    rate = DEFAULT_TAX_RATE_PCT if tax_rate_pct is None else float(tax_rate_pct)
    return _cents(Decimal(str(amount)) * (Decimal(1) + Decimal(str(rate)) / Decimal(100)))


def g3_truth(data: BackupData, project_id: str) -> dict[str, Any]:
    """Sent, Verify, labor review, quotes, General Material, conditions and GCs."""
    project = data.project(project_id) or {}
    files_by_id = data.by_id("project_files")

    sends = [r for r in data.by_project("proposal_sends", project_id) if r.get("status") == "sent"]
    sends.sort(key=lambda r: str(r.get("sent_at") or ""))
    totals = [sent_total(r) for r in sends]
    latest = sends[-1] if sends else None
    g3_sent: dict[str, Any] = {
        "total": sent_total(latest) if latest else None,
        "components": {k: latest.get(k) for k in SENT_AMOUNT_KEYS} if latest else None,
        "sends": len(sends),
        "distinct_totals": sorted(set(totals)),
        "spread": _cents(max(totals) - min(totals)) if totals else None,
        "last_sent_at": to_pacific_iso(latest.get("sent_at")) if latest else None,
        "gc_ids": sorted({str(r.get("gc_id")) for r in sends if r.get("gc_id")}),
    }

    verifies = [r for r in data.by_project("verifications", project_id) if r.get("committed_at")]
    verifies.sort(key=lambda r: str(r.get("committed_at") or ""))
    verify = verifies[-1] if verifies else None
    g3_verify: dict[str, Any] = {k: (verify.get(k) if verify else None) for k in VERIFY_KEYS}
    g3_verify["committed_at"] = to_pacific_iso(verify.get("committed_at")) if verify else None
    g3_verify["sum"] = verify_total(verify) if verify else None
    g3_verify["matches_sent"] = (
        verify is not None and g3_sent["total"] is not None and abs(g3_verify["sum"] - g3_sent["total"]) <= 1.0
    )
    g3_verify["materials_total"] = (
        _cents(sum(float(verify.get(k) or 0) for k in VERIFY_MATERIAL_KEYS)) if verify else None
    )

    reviews = data.by_project("labor_reviews", project_id)
    review = reviews[-1] if reviews else None
    hours, rate = parse_labor_notes(review.get("labor_notes") if review else None)
    amount = _f(review.get("labor_amount")) if review else None
    # 8.6: hours and rate come from the two regexes alone; parsed means
    # both were read. Whether hours x rate reproduces the review's labor
    # amount (within a dollar) is reported separately (`amount_consistent`,
    # None when either side is unknown) and never nulls the truth.
    found = hours is not None and rate is not None
    amount_consistent = None if not found or amount is None else abs(hours * rate - amount) <= 1.0
    g3_labor = {
        "amount": amount,
        "hours": hours if found else None,
        "rate": rate if found else None,
        "parsed": found,
        "amount_consistent": amount_consistent,
        "notes": review.get("labor_notes") if review else None,
    }

    categories = data.by_id("material_categories")
    rfqs = data.by_project("rfqs", project_id)
    quotes_by_rfq: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rfq_ids = {str(r["id"]) for r in rfqs}
    for q in data.table("quotes"):
        if str(q.get("rfq_id")) in rfq_ids:
            quotes_by_rfq[str(q["rfq_id"])].append(q)
    materials: list[dict[str, Any]] = []
    selected_taxed_total = 0.0
    for rfq in rfqs:
        cat = categories.get(str(rfq.get("material_category_id")), {})
        entries = []
        selected = None
        for q in sorted(quotes_by_rfq.get(str(rfq["id"]), []), key=lambda q: str(q.get("received_at") or "")):
            amount_q = _f(q.get("amount"))
            file_row = files_by_id.get(str(q.get("quote_file_id"))) if q.get("quote_file_id") else None
            path = data.file_path(file_row) if file_row else None
            entry = {
                "quote_id": q.get("id"),
                "amount": amount_q,
                "tax_included": q.get("tax_included"),
                "tax_rate": _f(q.get("tax_rate")),
                "taxed": taxed(amount_q, q.get("tax_included"), _f(q.get("tax_rate"))) if amount_q is not None else None,
                "origin": q.get("origin") or q.get("source"),
                "vendor_id": q.get("vendor_id"),
                "is_selected": bool(q.get("is_selected")),
                "is_approved": bool(q.get("is_approved")),
                "quote_file_id": q.get("quote_file_id"),
                "file": (
                    {
                        "filename": file_row.get("filename"),
                        "storage_path": file_row.get("storage_path"),
                        "sha256": sha256_file(path) if path and path.exists() else None,
                    }
                    if file_row
                    else None
                ),
            }
            entries.append(entry)
            if entry["is_selected"] and selected is None:
                selected = entry
        if selected is not None and selected["taxed"] is not None:
            selected_taxed_total += selected["taxed"]
        materials.append(
            {
                "rfq_id": rfq.get("id"),
                "category": cat.get("name"),
                "kind": cat.get("kind"),
                "pricing_section": cat.get("pricing_section"),
                "is_general": bool(cat.get("is_general")),
                "status": rfq.get("status"),
                "custom_amount": _f(rfq.get("custom_amount")),
                "selected": selected,
                "alternatives": [e for e in entries if e is not selected],
                "replaces_section": QUOTE_SECTION_MAP.get(str(cat.get("name") or "").upper()),
            }
        )
    selected_taxed_total = _cents(selected_taxed_total)

    gme_rows = data.by_project("general_material_estimates", project_id)
    gme = gme_rows[-1] if gme_rows else None
    g3_general_material: dict[str, Any] = {
        "amount": _f(gme.get("amount")) if gme else None,
        "tax_included": gme.get("tax_included") if gme else None,
        "tax_rate": _f(gme.get("tax_rate")) if gme else None,
        "source": gme.get("source") if gme else None,
        "raw_extraction": gme.get("raw_extraction") if gme else None,
    }

    markup_pct = None
    if verify and verify.get("materials_amount") and verify.get("materials_markup_amount") is not None:
        markup_pct = round(float(verify["materials_markup_amount"]) / float(verify["materials_amount"]) * 100, 2)
    labor_markup_pct = None
    if verify and verify.get("labor_amount") and verify.get("labor_markup_amount") is not None:
        labor_markup_pct = round(float(verify["labor_markup_amount"]) / float(verify["labor_amount"]) * 100, 2)

    gcs = data.by_id("general_contractors")
    project_gcs = [
        {"gc_id": r.get("gc_id"), "gc_name": (gcs.get(str(r.get("gc_id"))) or {}).get("name"), "needs_by": r.get("needs_by")}
        for r in data.by_project("project_gcs", project_id)
    ]
    conditions = {
        "wage_type": project.get("wage_type"),
        "labor_time": project.get("labor_time"),
        "project_type": project.get("project_type"),
        "owner_type": project.get("owner_type"),
        "labor_needed": project.get("labor_needed"),
        "bid_method": project.get("bid_method"),
        "address": project.get("address"),
    }
    return {
        "project": {"id": project_id, "number": project.get("number"), "name": project.get("name")},
        "conditions": conditions,
        "project_gcs": project_gcs,
        "g3_sent": g3_sent,
        "g3_verify": g3_verify,
        "g3_labor": g3_labor,
        "g3_materials": materials,
        "g3_materials_selected_taxed_total": selected_taxed_total,
        "g3_general_material": g3_general_material,
        "g3_markups": {"materials_pct": markup_pct, "labor_pct": labor_markup_pct},
    }


# Corrected total (the Verify leak) ---------------------------------------------------


def corrected_g3_total(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ADJ4 formula, generically: when the General Material extraction
    took only the WIRING material (`wiring_only`), add the recap OTHER ITEMS
    material to the selected General Material quote, tax every category to
    the cent, apply the Verify materials markup, add the labor and the other
    Verify sections. None when the leak is not present."""
    gm = ref.get("g3_general_material") or {}
    if not gm.get("wiring_only"):
        return None
    missing = gm.get("other_items_material_missing") or 0.0
    verify = ref.get("g3_verify") or {}
    tax = (ref.get("derived") or {}).get("tax_rate_pct") or DEFAULT_TAX_RATE_PCT
    markup_pct = (ref.get("g3_markups") or {}).get("materials_pct")
    gm_pretax = None
    categories: dict[str, float] = {}
    for m in ref.get("g3_materials") or []:
        sel = m.get("selected")
        if not sel or sel.get("amount") is None:
            continue
        name = str(m.get("category") or "")
        if m.get("is_general") or name.upper() == "GENERAL MATERIAL":
            gm_pretax = float(sel["amount"])
            continue
        if m.get("pricing_section") in (None, "materials"):
            categories[name] = taxed(float(sel["amount"]), sel.get("tax_included"), sel.get("tax_rate"))
    if gm_pretax is None:
        gm_pretax = float(gm.get("amount") or 0.0)
    gm_corrected = _cents(gm_pretax + float(missing))
    gm_taxed = taxed(gm_corrected, gm.get("tax_included"), gm.get("tax_rate") or tax)
    materials = _cents(gm_taxed + sum(categories.values()))
    markup = _cents(Decimal(str(materials)) * Decimal(str(markup_pct or 0)) / Decimal(100))
    other = sum(float(verify.get(k) or 0) for k in VERIFY_KEYS if k not in ("materials_amount", "materials_markup_amount"))
    total = _cents(materials + markup + other)
    return {
        "general_material_pretax": gm_corrected,
        "general_material_taxed": gm_taxed,
        "categories_taxed": categories,
        "materials": materials,
        "materials_markup": markup,
        "other_verify": _cents(other),
        "total": total,
        "formula": "sum over categories of round(pretax x (1 + tax), 2); markup = round(materials x pct, 2); plus labor and the other Verify sections",
    }


# Adjudications ---------------------------------------------------------------------


def load_adjudications(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    entries = doc.get("adjudications") if isinstance(doc, dict) else doc
    out = list(entries or [])
    if isinstance(doc, dict) and doc.get("aliases"):
        out.append({"id": "aliases", "type": "alias", "value": dict(doc["aliases"]), "reviewed": bool(doc.get("aliases_reviewed", False))})
    return out


def _line_matches(line: Mapping[str, Any], match: Mapping[str, Any]) -> bool:
    if match.get("line_id") and line.get("line_id") == match["line_id"]:
        return True
    prefix = match.get("description_prefix")
    if prefix and normalize_description(line.get("description")).startswith(normalize_description(prefix)):
        if match.get("group") and _canonical(match["group"]) != _canonical(line.get("group") or ""):
            return False
        return True
    exact = match.get("description")
    if exact and normalize_description(line.get("description")) == normalize_description(exact):
        return True
    return False


def apply_adjudications(ref: dict[str, Any], adjudications: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return `ref` with `quantity_lines_adjudicated`, `price_truth_adjudicated`
    and `aliases` applied from the entries. Every entry is applied whatever
    its `reviewed` flag; `adjudications_applied` records each with its
    status (reviewed or unreviewed) and `adjudications_reviewed` is true
    only when all of them are reviewed. Unknown types are listed under
    `adjudications_ignored`."""
    lines = [dict(ln) for ln in ref.get("quantity_lines") or []]
    applied: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    price_truth: dict[str, Any] = {}
    aliases: dict[str, str] = dict(ref.get("aliases") or {})
    labor_rate = ((ref.get("estimator_recap") or {}).get("totals") or {}).get("labor_rate")

    def recompute(line: dict[str, Any]) -> None:
        rate = line.get("labor_rate") or labor_rate
        um, uh = line.get("unit_material"), line.get("unit_hours")
        if um is not None or uh is not None:
            if rate is not None:
                line["unit_item_cost"] = (um or 0.0) + (uh or 0.0) * float(rate)
            else:
                line["unit_item_cost"] = line.get("unit_item_cost")
        uic = line.get("unit_item_cost")
        line["value"] = None if uic is None else float(line["qty"]) * float(uic)

    for adj in adjudications:
        kind = adj.get("type")
        status = "reviewed" if adj.get("reviewed") else "unreviewed"
        record = {"id": adj.get("id"), "type": kind, "status": status}
        if kind not in ADJUDICATION_TYPES:
            ignored.append({**record, "reason": "unknown type"})
            continue
        if kind == "note":
            applied.append(record)
            continue
        if kind == "alias":
            for word, variant in (adj.get("value") or {}).items():
                aliases[str(word).upper()] = str(variant).upper()
            applied.append(record)
            continue
        if kind == "price_truth":
            price_truth.update(dict(adj.get("value") or {}))
            applied.append(record)
            continue
        if kind == "add_line":
            new = dict(adj.get("line") or {})
            new.setdefault("line_id", f"A{len(applied) + 1:03d}")
            new.setdefault("area", lines[0]["area"] if lines else "SHEET")
            new.setdefault("section", "NEW WORK")
            new.setdefault("group", "")
            new.setdefault("subgroup", "")
            new.setdefault("is_accessory", False)
            new["unit"] = normalize_unit(new.get("unit"))
            new["qty"] = float(new.get("qty") or 0.0)
            key = classify_line(new.get("description"), new["section"], new["group"], new["unit"], subgroup=new["subgroup"], aliases=merge_aliases(aliases))
            new["kind"], new["attrs"] = key.kind, sorted(key.attrs)
            new.setdefault("labor_rate", labor_rate)
            new["adjudication"] = adj.get("id")
            recompute(new)
            lines.append(new)
            applied.append({**record, "line_id": new["line_id"]})
            continue
        match = adj.get("match") or {}
        hits = [ln for ln in lines if _line_matches(ln, match)]
        if not hits:
            ignored.append({**record, "reason": "no line matched"})
            continue
        for ln in hits:
            ln.setdefault("adjudications", []).append(adj.get("id"))
            if kind == "remove_line":
                ln["removed"] = True
            elif kind == "set_qty":
                ln["qty"] = float(adj["value"])
                recompute(ln)
            elif kind == "set_unit":
                ln["unit"] = normalize_unit(adj["value"])
                if adj.get("qty") is not None:
                    ln["qty"] = float(adj["qty"])
                recompute(ln)
            elif kind == "set_unit_cost":
                value = adj.get("value") or {}
                if "unit_material" in value:
                    ln["unit_material"] = value["unit_material"]
                if "unit_hours" in value:
                    ln["unit_hours"] = value["unit_hours"]
                if "unit_item_cost" in value:
                    ln["unit_item_cost"] = value["unit_item_cost"]
                    ln["unit_material"] = ln.get("unit_material")
                recompute(ln)
        applied.append({**record, "lines": [ln["line_id"] for ln in hits]})
    ref["quantity_lines_adjudicated"] = [ln for ln in lines if not ln.get("removed")]
    ref["price_truth_adjudicated"] = price_truth
    ref["aliases"] = aliases
    ref["adjudications_applied"] = applied
    ref["adjudications_ignored"] = ignored
    ref["adjudications_reviewed"] = bool(applied) and all(a["status"] == "reviewed" for a in applied)
    derived = ref.setdefault("derived", {})
    if "g3_corrected_total" in price_truth:
        computed = derived.get("g3_corrected_total")
        derived["g3_corrected_total"] = float(price_truth["g3_corrected_total"])
        derived["g3_corrected_total_source"] = "adjudication"
        if computed is not None and abs(float(computed) - float(price_truth["g3_corrected_total"])) > 0.01:
            ref.setdefault("warnings", []).append(
                f"adjudicated corrected total differs from the computed one by {float(price_truth['g3_corrected_total']) - float(computed):.2f}"
            )
    return ref


# Data quality -------------------------------------------------------------------------


def data_quality_checks(ref: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    sent = (ref.get("g3_sent") or {}).get("total")
    verify = ref.get("g3_verify") or {}
    checks.append(
        {
            "check": "verify_sum_matches_sent",
            "status": "pass" if verify.get("matches_sent") else ("skip" if sent is None or verify.get("sum") is None else "fail"),
            "detail": {"delta": None if sent is None or verify.get("sum") is None else _cents(verify["sum"] - sent)},
        }
    )
    mat = verify.get("materials_total")
    quotes_taxed = ref.get("g3_materials_selected_taxed_total")
    checks.append(
        {
            "check": "materials_from_quotes",
            "status": "skip" if mat is None or quotes_taxed is None else ("pass" if abs(mat - quotes_taxed) <= 1.0 else "fail"),
            "detail": {"delta": None if mat is None or quotes_taxed is None else _cents(mat - quotes_taxed)},
        }
    )
    lines = [ln for ln in ref.get("quantity_lines") or [] if not ln.get("is_accessory")]
    joined = [ln for ln in lines if ln.get("est_ref")]
    rate = len(joined) / len(lines) if lines else None
    checks.append(
        {
            "check": "boq_estimate_join_rate",
            "status": "skip" if rate is None else ("pass" if rate >= 0.95 else "fail"),
            "detail": {"rate": None if rate is None else round(rate, 4), "joined": len(joined), "lines": len(lines)},
        }
    )
    recap_cost = ((ref.get("estimator_recap") or {}).get("totals") or {}).get("cost")
    line_total = (ref.get("estimator_lines") or {}).get("total_item_cost_sum")
    checks.append(
        {
            "check": "recap_cost_matches_lines",
            "status": "skip" if recap_cost is None or line_total is None else ("pass" if abs(recap_cost - line_total) <= 1.0 else "fail"),
            "detail": {"delta": None if recap_cost is None or line_total is None else _cents(recap_cost - line_total)},
        }
    )
    labor = ref.get("g3_labor") or {}
    checks.append(
        {
            "check": "labor_notes_parsed",
            "status": "pass" if labor.get("parsed") else ("skip" if not labor.get("notes") else "fail"),
            "detail": {"hours_known": labor.get("hours") is not None},
        }
    )
    consistent = labor.get("amount_consistent")
    checks.append(
        {
            "check": "labor_amount_consistent",
            "status": "skip" if consistent is None else ("pass" if consistent else "fail"),
            "detail": {"rel_delta": None if consistent is None or not labor["amount"]
                       else round((labor["hours"] * labor["rate"] - labor["amount"]) / labor["amount"], 4)},
        }
    )
    return checks


# Build ----------------------------------------------------------------------------------


def build_reference(
    case_id: str,
    project_id: str,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    *,
    adjudications: Sequence[Mapping[str, Any]] | None = None,
    data: BackupData | None = None,
) -> dict[str, Any]:
    """The reference dict of one case (see the module docstring)."""
    data = data or BackupData(backup_dir)
    project_id = data.resolve_project_id(project_id)
    warnings: list[str] = []
    ref: dict[str, Any] = {
        "schema": REFERENCE_SCHEMA,
        "builder_version": REFERENCE_BUILDER_VERSION,
        "case_id": case_id,
        "project_id": project_id,
        "built_at": now_pacific_iso(),
        "backup_dir": str(data.backup_dir),
        "sources": {},
        "warnings": warnings,
    }

    boq_file = pick_boq_file(data, project_id)
    boq_rows: list[BoqRow] = []
    if boq_file is None:
        warnings.append("no BOQ workbook in the backup")
    else:
        path = data.file_path(boq_file)
        ref["sources"]["boq"] = {"project_file_id": boq_file["id"], "filename": boq_file["filename"], "storage_path": boq_file["storage_path"], "sha256": sha256_file(path), "created_at": to_pacific_iso(boq_file.get("created_at"))}
        boq_rows = parse_estimator_boq(path.read_bytes())

    est_file = pick_estimate_file(data, project_id)
    book = EstimateBook(lines=[], labor_rate=None, totals={}, sheets=[])
    recap_sections: dict[str, Any] = {"sections": {}, "totals": {}}
    recap_parsed: dict[str, Any] | None = None
    if est_file is None:
        warnings.append("no estimate workbook in the backup")
    else:
        path = data.file_path(est_file)
        xlsx = path.read_bytes()
        ref["sources"]["estimate"] = {"project_file_id": est_file["id"], "filename": est_file["filename"], "storage_path": est_file["storage_path"], "sha256": sha256_file(path), "created_at": to_pacific_iso(est_file.get("created_at"))}
        book = parse_estimate_lines(xlsx)
        warnings.extend(f"estimate: {w}" for w in book.warnings)
        recap_sections = parse_recap_sections(xlsx)
        try:
            parsed = parse_recap(xlsx)
            recap_parsed = {
                "materials": float(parsed["materials"]),
                "labor": float(parsed["labor"]),
                "base": float(parsed["base"]),
                "warnings": list(parsed.get("warnings") or []),
            }
        except EstimateError as exc:
            warnings.append(f"parse_recap: {exc.code}")
    marked = data.files(project_id, "marked_plans")
    if marked:
        mp = marked[-1]
        ref["sources"]["marked_plans"] = {"project_file_id": mp["id"], "filename": mp["filename"], "storage_path": mp["storage_path"]}

    lines, join_warnings = join_boq_estimate(boq_rows, book.lines, labor_rate=book.labor_rate)
    warnings.extend(join_warnings)
    ref["quantity_lines"] = [ln for ln in lines if not ln["is_accessory"]]
    ref["accessory_lines"] = [ln for ln in lines if ln["is_accessory"]]
    ref["estimator_lines"] = {
        "count": len(book.lines),
        "sheets": book.sheets,
        "total_item_cost_sum": _cents(sum(ln.total_item_cost or 0.0 for ln in book.lines)) if book.lines else None,
        "total_hours_sum": round(sum(ln.total_hours or 0.0 for ln in book.lines), 4) if book.lines else None,
        "lines": [asdict(ln) for ln in book.lines],
    }
    sections = {}
    for name, row in (recap_sections.get("sections") or {}).items():
        sections[name] = {k: row.get(k) for k in ("material", "labor", "ohp_labor", "ohp_material", "total", "cost")}
    rt = recap_sections.get("totals") or {}
    ref["estimator_recap"] = {
        "sheet": recap_sections.get("sheet"),
        "sections": sections,
        "totals": {
            "material": rt.get("material") if rt else book.totals.get("material"),
            "labor": rt.get("labor") if rt else book.totals.get("labor"),
            "cost": rt.get("cost") if rt else book.totals.get("cost"),
            "ohp_labor": rt.get("ohp_labor") if rt else book.totals.get("ohp_labor"),
            "ohp_material": rt.get("ohp_material") if rt else book.totals.get("ohp_material"),
            "bid": rt.get("total") if rt else book.totals.get("bid"),
            "hours": book.totals.get("hours"),
            "labor_rate": book.labor_rate,
            "ohp_labor_pct": book.totals.get("ohp_labor_pct"),
            "ohp_material_pct": book.totals.get("ohp_material_pct"),
            "allowances": book.totals.get("allowances"),
        },
        "parse_recap": recap_parsed,
    }

    ref.update(g3_truth(data, project_id))

    gm = ref["g3_general_material"]
    wiring_material = (sections.get("WIRING") or {}).get("material")
    other_items_material = (sections.get("OTHER ITEMS") or {}).get("material")
    # The Verify leak (spec 3.3): the General Material amount equals the
    # WIRING material alone while OTHER ITEMS carries material too.
    wiring_only = bool(
        gm.get("amount") is not None
        and wiring_material is not None
        and other_items_material
        and abs(float(gm["amount"]) - float(wiring_material)) <= 1.0
    )
    gm["wiring_only"] = wiring_only
    gm["wiring_material"] = wiring_material
    gm["other_items_material_missing"] = other_items_material if wiring_only else 0.0
    gm["non_quoted_sections_material"] = {
        name: row.get("material")
        for name, row in sections.items()
        if name not in {m.get("replaces_section") for m in ref["g3_materials"] if m.get("selected") and (m["selected"].get("amount") or 0) > 0}
    }

    est_hours = ref["estimator_recap"]["totals"].get("hours")
    g3_hours = ref["g3_labor"].get("hours")
    tax_rates = [m["selected"]["tax_rate"] for m in ref["g3_materials"] if m.get("selected") and m["selected"].get("tax_rate") is not None]
    tax_rate = tax_rates[0] if tax_rates else (gm.get("tax_rate") or DEFAULT_TAX_RATE_PCT)
    ref["derived"] = {
        "est_hours": est_hours,
        "g3_hours": g3_hours,
        "hour_roundup": round(g3_hours / est_hours, 4) if est_hours and g3_hours else None,
        "g3_rate": ref["g3_labor"].get("rate"),
        "estimator_rate": book.labor_rate,
        "tax_rate_pct": tax_rate,
        "quote_replaced_sections": sorted(
            {m["replaces_section"] for m in ref["g3_materials"] if m.get("replaces_section") and m.get("selected") and (m["selected"].get("amount") or 0) > 0 and m["selected"].get("origin") == "vendor"}
        ),
        "g3_corrected_total": None,
        "g3_corrected_total_source": None,
    }
    corrected = corrected_g3_total(ref)
    if corrected is not None:
        ref["derived"]["g3_corrected_total"] = corrected["total"]
        ref["derived"]["g3_corrected_total_source"] = "computed"
        ref["derived"]["g3_corrected_detail"] = corrected

    ref["aliases"] = {}
    apply_adjudications(ref, adjudications or [])
    ref["data_quality"] = data_quality_checks(ref)
    return ref


def reference_dir(eval_root: Path | str, case_id: str) -> Path:
    return Path(eval_root) / "references" / case_id


def write_reference(ref: Mapping[str, Any], eval_root: Path | str) -> Path:
    path = reference_dir(eval_root, str(ref["case_id"])) / "reference.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ref, indent=2, sort_keys=False, default=str) + "\n")
    return path


def load_reference(eval_root: Path | str, case_id: str) -> dict[str, Any]:
    path = reference_dir(eval_root, case_id) / "reference.json"
    return json.loads(path.read_text())


def reference_to_boq_spec(ref: Mapping[str, Any], *, adjudicated: bool = True) -> dict[str, Any]:
    """An oracle BOQ_SPEC built from the truth quantities (for the $0 LOPO
    pricing backtest: the engine prices the estimator's own quantities).
    FT lines carry the wastage-inclusive quantity with wastage_applies false
    so a renderer never applies it twice."""
    lines = ref.get("quantity_lines_adjudicated") if adjudicated else None
    if lines is None:
        lines = ref.get("quantity_lines") or []
    areas: dict[str, dict[str, dict[str, dict[str, list[dict[str, Any]]]]]] = {}
    for ln in lines:
        area = areas.setdefault(ln.get("area") or "SHEET", {})
        section = area.setdefault(ln.get("section") or "NEW WORK", {})
        group = section.setdefault(ln.get("group") or "", {})
        items = group.setdefault(ln.get("subgroup") or "", [])
        items.append(
            {
                "description": ln["description"],
                "quantity": ln["qty"],
                "unit": ln["unit"],
                "sheet_refs": [s.strip() for s in str(ln.get("sheet_refs") or "").split(",") if s.strip()],
                "caveat": None,
                "material_note": None,
                "material_category": "General Material",
                "wastage_applies": False,
                "is_accessory": False,
                "line_id": ln.get("line_id"),
            }
        )
    return {
        "title": f"ORACLE BOQ {ref.get('case_id')}",
        "subtitle": "estimator quantities from the eval reference",
        "closing_notes": [],
        "areas": [
            {
                "area": area,
                "notes": [],
                "sections": [
                    {
                        "heading": section,
                        "note": None,
                        "groups": [
                            {
                                "name": group,
                                "note": None,
                                "subgroups": [{"name": sub, "note": None, "items": items} for sub, items in subs.items()],
                            }
                            for group, subs in groups.items()
                        ],
                    }
                    for section, groups in sections.items()
                ],
            }
            for area, sections in areas.items()
        ],
    }


# Pool checks ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _data_cached(backup_dir: str) -> BackupData:
    return BackupData(backup_dir)


def pool_data_quality(
    project_ids: Iterable[str],
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
) -> dict[str, Any]:
    """Counts (no dollars) of the two G3 truth checks over projects with a
    sent proposal and a committed Verify: Verify sum equals the latest sent
    total within $1 and Verify materials equal the taxed selected quotes."""
    data = _data_cached(str(backup_dir))
    counted = 0
    verify_ok = 0
    materials_ok = 0
    labor_parsed = 0
    labor_rows = 0
    labor_inconsistent: list[str] = []
    failing_verify: list[str] = []
    failing_materials: list[str] = []
    for pid in project_ids:
        truth = g3_truth(data, pid)
        if truth["g3_sent"]["total"] is None or truth["g3_verify"]["sum"] is None:
            continue
        counted += 1
        if truth["g3_verify"]["matches_sent"]:
            verify_ok += 1
        else:
            failing_verify.append(pid[:8])
        mat = truth["g3_verify"]["materials_total"]
        if mat is not None and abs(mat - truth["g3_materials_selected_taxed_total"]) <= 1.0:
            materials_ok += 1
        else:
            failing_materials.append(pid[:8])
        if truth["g3_labor"].get("notes"):
            labor_rows += 1
            if truth["g3_labor"].get("parsed"):
                labor_parsed += 1
            if truth["g3_labor"].get("amount_consistent") is False:
                labor_inconsistent.append(pid[:8])
    return {
        "cases_sent_and_verified": counted,
        "verify_sum_matches_sent": verify_ok,
        "materials_match_quotes": materials_ok,
        "failing_verify": failing_verify,
        "failing_materials": failing_materials,
        "labor_notes_rows": labor_rows,
        "labor_notes_parsed": labor_parsed,
        "labor_amount_inconsistent": labor_inconsistent,
    }


__all__ = [
    "REFERENCE_SCHEMA", "REFERENCE_BUILDER_VERSION", "ADJUDICATION_TYPES", "BackupData",
    "EstimateLine", "EstimateBook", "parse_estimate_lines", "parse_recap_sections", "join_boq_estimate",
    "parse_labor_notes", "sent_total", "verify_total", "taxed", "g3_truth", "corrected_g3_total",
    "load_adjudications", "apply_adjudications", "data_quality_checks", "build_reference",
    "write_reference", "load_reference", "reference_dir", "reference_to_boq_spec", "pool_data_quality",
    "pick_boq_file", "pick_estimate_file", "sha256_file", "now_pacific_iso", "to_pacific_iso",
]
