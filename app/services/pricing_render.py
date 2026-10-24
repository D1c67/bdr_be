"""The AI priced estimate workbook (AI_ESTIMATING_BUILD_SPEC WS6
`pricing_render`): the estimate template of plans_reader_render with the
engine's prices filled in, a Bid Recap whose BASE BID equals the Verify
draft's bid total to the cent, a Verify Summary sheet, and the Basis and
Checks sheets WS8 fills (`basis_rows`, `checks_rows`; empty is fine).

Layout of a detailed estimate sheet (columns of EST_HEADERS):

- N4 is the labor rate of the Verify draft (the policy rate for the wage
  type, or the recorded or overridden rate);
- H (unit labor hours) and I (unit material cost) are VALUES from the
  priced lines; a quote-covered line carries 0 material here because the
  vendor quote is its own recap row; a blocked or unpriced line stays
  blank and says why in column S (AI PRICE BASIS);
- J to O are formulas: J = H x $N$4, K = F x H, L = F x I, M = F x J,
  N = I + J, O = L + M; the SUBTOTAL row under every group holds P = SUM of
  L, Q = SUM of M and R = P + Q over the group's rows.

The Bid Recap (plans_reader_render._write_recap draws it and returns the
layout) holds one VALUE row per group label plus one per vendor quote,
the allowances, and three adjustment rows that make the arithmetic close
on the Verify draft exactly: the labor hours round-up (reviewer hours
minus basis hours, at the rate), the material tax rounding adjustment
(Verify taxes each category row, the recap taxes each group row) and the
markup adjustment (Verify marks up each tax-inclusive section amount,
the recap marks up each row). TOTALS and the BID SUMMARY are formulas over
those rows; BASE BID PRICE is written as the value `verify.bid_total` and
the Checks sheet carries the formula that must evaluate to 0 when the
workbook is recalculated (the bdr-gotenberg recalc check is wave 3).

Every sheet starts with the banner "INTERNAL - AI PRICED DRAFT - NOT
VERIFIED" until a sign-off is given. Pure: bytes in, bytes out.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from app.services import plans_reader_render as prr
from app.services.pricing_engine import FLAG_BLOCKED, FLAG_QUOTE_COVERED
from app.services.pricing_formula import apply_tax, round_cents
from app.services.pricing_policy import DEFAULT_POLICY, PricingPolicy
from app.services.pricing_types import PricedEstimate, PricedLine
from app.services.plans_reader_rows import iter_boq_lines

RENDER_VERSION = 1
DRAFT_BANNER = "INTERNAL - AI PRICED DRAFT - NOT VERIFIED"
SIGNED_BANNER = "INTERNAL - AI PRICED - VERIFIED {by}"
VERIFY_SHEET = "Verify Summary"
BASIS_SHEET = "Basis"
CHECKS_SHEET = "Checks"
BASIS_COL = 19  # S: AI PRICE BASIS
MONEY_FMT = "$#,##0.00"
HOURS_FMT = "0.00##"
BANNER_FILL = "FFFFC7CE"
LABEL_ROUNDUP = "LABOR HOURS ROUND-UP"
LABEL_TAX_ADJ = "MATERIAL TAX ROUNDING ADJUSTMENT"
LABEL_MARKUP_ADJ = "MARKUP ADJUSTMENT (VERIFY)"
LABEL_QUOTE = "VENDOR QUOTE - {category}"
_ZERO = Decimal(0)
DEFAULT_BASIS_HEADER = ("LINE", "DESCRIPTION", "QTY", "UNIT", "QUANTITY BASIS", "SOURCES", "PRICE BASIS")
DEFAULT_CHECKS_HEADER = ("CHECK", "STATUS", "DETAIL", "LINES")


def priced_estimate_filename(project_name: str, version: int) -> str:
    name = prr._clean(project_name) or "Project"
    return f"{name} - AI PRICED ESTIMATE v{int(version)}.xlsx"


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _dec(value: Any) -> Decimal:
    if value is None:
        return _ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _banner(ws: Worksheet, text: str, *, last_col: int) -> None:
    c = ws.cell(row=1, column=1, value=text)
    prr._style(c, bold=True, size=12, h="center", v="center", fill=prr._solid(BANNER_FILL), border=False)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    ws.row_dimensions[1].height = 21


def _basis_text(line: PricedLine | None) -> str:
    if line is None:
        return "not priced: line not in the pricing version"
    if FLAG_BLOCKED in line.flags:
        gates = [f for f in line.flags if f not in (FLAG_BLOCKED,)]
        return "BLOCKED: " + ", ".join(gates) if gates else "BLOCKED"
    parts: list[str] = []
    if line.labor.value is not None:
        parts.append(f"hours {line.labor.method} {line.labor.quality} n={line.labor.n_projects}")
    else:
        parts.append("hours unpriced")
    if FLAG_QUOTE_COVERED in line.flags:
        parts.append(f"material by quote {line.quote_id or ''}".strip())
    elif line.material.value is not None:
        parts.append(f"material {line.material.method} {line.material.quality}")
    else:
        parts.append("material unpriced")
    extra = [f for f in line.flags if f not in (FLAG_QUOTE_COVERED,)]
    if extra:
        parts.append("flags: " + ", ".join(extra))
    return "; ".join(parts)


class _PricedSheet(prr._EstimateSheet):
    """The estimate sheet writer with the pricing columns filled."""

    def priced_item(self, line: PricedLine | None, description: str, quantity: float, wastage: float, unit: str) -> int:
        r = self.item(description, quantity, wastage, unit)
        ws = self.ws
        if line is not None and FLAG_BLOCKED not in line.flags:
            if line.labor.value is not None:
                h = ws.cell(row=r, column=8, value=_f(line.labor.value))
                prr._style(h, h="center", v="center", fmt=HOURS_FMT)
            material = _ZERO if FLAG_QUOTE_COVERED in line.flags else line.material.value
            if material is not None:
                i = ws.cell(row=r, column=9, value=_f(material))
                prr._style(i, h="center", v="center", fmt=MONEY_FMT)
        formulas = {
            10: f"=IF(H{r}=\"\",\"\",H{r}*$N$4)",
            11: f"=IF(H{r}=\"\",\"\",F{r}*H{r})",
            12: f"=IF(I{r}=\"\",\"\",F{r}*I{r})",
            13: f"=IF(H{r}=\"\",\"\",F{r}*J{r})",
            14: f"=IF(AND(H{r}=\"\",I{r}=\"\"),\"\",N(I{r})+N(J{r}))",
            15: f"=IF(AND(H{r}=\"\",I{r}=\"\"),\"\",N(L{r})+N(M{r}))",
        }
        for col, formula in formulas.items():
            c = ws.cell(row=r, column=col, value=formula)
            prr._style(c, h="center", v="center", fmt=HOURS_FMT if col == 11 else MONEY_FMT)
        s = prr._text_cell(ws, r, BASIS_COL, _basis_text(line))
        prr._style(s, v="center", wrap=True, border=False, size=9)
        return r

    def priced_subtotal(self, first: int, last: int) -> None:
        r = self.row
        self.subtotal()
        if first and last:
            values = (f"=SUM(L{first}:L{last})", f"=SUM(M{first}:M{last})", f"=P{r + 1}+Q{r + 1}")
            for i, formula in enumerate(values):
                c = self.ws.cell(row=r + 1, column=16 + i, value=formula)
                prr._style(c, bold=True, h="center", v="center", fill=prr._solid(prr.SUBTOTAL_FILL), fmt=MONEY_FMT)


def _line_ids_by_path(boq_spec: Mapping[str, Any]) -> dict[str, str]:
    return {row["path"]: row["line_id"] for row in iter_boq_lines(boq_spec)}


def _write_priced_area(
    ws: Worksheet,
    area: dict[str, Any],
    area_index: int,
    estimate_spec: dict[str, Any],
    priced: PricedEstimate,
    ids_by_path: Mapping[str, str],
    date: datetime.date,
    area_notes: list[str],
    *,
    labor_rate: Decimal,
    banner: str,
    label_lines: dict[str, list[PricedLine]],
) -> list[str]:
    """One detailed estimate sheet with prices; mirrors
    plans_reader_render._write_estimate_area (same rows, same labels)."""
    _banner(ws, banner, last_col=BASIS_COL)
    prr._write_estimate_header(ws, estimate_spec, date)
    rate_cell = ws.cell(row=4, column=14, value=float(labor_rate))
    prr._style(rate_cell, bold=True, size=14, h="center", v="center", fill=prr._solid(prr.HEADER_FILL), fmt=MONEY_FMT)
    head = ws.cell(row=prr.EST_HEADER_ROW, column=BASIS_COL, value="AI PRICE BASIS")
    prr._style(head, bold=True, h="center", v="center", wrap=True, fill=prr._solid(prr.HEADER_FILL))
    ws.column_dimensions["S"].width = 44
    sheet = _PricedSheet(ws)
    labels: list[str] = []
    for si, section in enumerate(area.get("sections") or []):
        if not isinstance(section, dict) or not prr._section_has_items(section, estimate=True):
            continue
        heading = prr._clean(section.get("heading")).upper()
        sheet.heading(prr._subheading(heading, section.get("note")), fill=prr._solid(prr.LEVEL0_FILL), center=True)
        demolition = heading == "DEMOLITION"
        demo_first = demo_last = 0
        for gi, group in enumerate(section.get("groups") or []):
            if not isinstance(group, dict) or not prr._group_has_items(group, estimate=True):
                continue
            gname = prr._clean(group.get("name"))
            if gname and gname.upper() != heading:
                sheet.heading(prr._group_heading(gname, group.get("note")), fill=prr._group_fill(gname), center=True)
            label = "DEMOLITION" if demolition else (gname or heading)
            g_first = g_last = 0
            for ui, sg in enumerate(group.get("subgroups") or []):
                if not isinstance(sg, dict):
                    continue
                sname = prr._clean(sg.get("name"))
                raw_items = sg.get("items") or []
                items = [(ii, it) for ii, it in enumerate(raw_items) if isinstance(it, dict) and not it.get("is_accessory")]
                if not items:
                    continue
                if sname:
                    sheet.heading(prr._subheading(sname, sg.get("note")), fill=prr._theme(*prr.SUBGROUP_FILL), center=False)
                first = last = 0
                refs: list[str] = []
                for ii, it in items:
                    path = f"areas[{area_index}].sections[{si}].groups[{gi}].subgroups[{ui}].items[{ii}]"
                    line = priced.line(ids_by_path.get(path, "")) if ids_by_path.get(path) else None
                    q = round(prr._num(it.get("quantity"), f"Estimate item '{prr._clean(it.get('description'))[:60]}'"), 2)
                    r = sheet.priced_item(line, prr._item_description(it), q, prr.estimate_wastage(it), prr._clean(it.get("unit")) or "EA")
                    if line is not None:
                        label_lines.setdefault(label, []).append(line)
                    first = first or r
                    last = r
                    for ref in it.get("sheet_refs") or []:
                        ref_s = prr._clean(ref)
                        if ref_s and ref_s not in refs:
                            refs.append(ref_s)
                sheet.sheet_refs(first, last, refs)
                sheet.blank()
                g_first = g_first or first
                g_last = last
            if not demolition:
                sheet.priced_subtotal(g_first, g_last)
                labels.append(label)
            else:
                demo_first = demo_first or g_first
                demo_last = g_last
        if demolition:
            sheet.priced_subtotal(demo_first, demo_last)
            labels.append("DEMOLITION")

    allowances = []
    if estimate_spec.get("include_standard_allowances", True):
        allowances.extend(prr.ESTIMATE_ALLOWANCES)
    allowances.extend(prr._clean(a) for a in estimate_spec.get("extra_allowances") or [] if prr._clean(a))
    if allowances:
        sheet.heading("ALLOWANCES", fill=prr._theme(*prr.SUBGROUP_FILL), center=False)
        for a in allowances:
            sheet.allowance(a)
        labels.append("ALLOWANCES")

    sheet.row += 1
    notes = [n for n in list(estimate_spec.get("general_notes") or []) + area_notes if prr._clean(n)]
    if not notes:
        sheet.note("", first=True)
    for i, n in enumerate(notes):
        sheet.note(n, first=i == 0)
    ws.print_area = f"A1:R{sheet.row - 1}"
    return labels


# Recap --------------------------------------------------------------------------------------


def _group_material_cents(lines: Iterable[PricedLine]) -> Decimal:
    """Library material of a recap label, rounded per (category, BOQ group)
    exactly like the engine's category roll-up, so the recap material
    column sums to the Verify category rows."""
    by_key: dict[tuple[str, str], Decimal] = {}
    for ln in lines:
        if ln.material_cost is None or FLAG_QUOTE_COVERED in ln.flags or FLAG_BLOCKED in ln.flags:
            continue
        key = (ln.pricing_category or "", (ln.row.group or "").upper())
        by_key[key] = by_key.get(key, _ZERO) + ln.material_cost
    return sum((round_cents(v) for v in by_key.values()), _ZERO)


def _group_hours(lines: Iterable[PricedLine]) -> Decimal:
    return sum((ln.labor_hours for ln in lines if ln.labor_hours is not None and FLAG_BLOCKED not in ln.flags), _ZERO)


def recap_rows(
    priced: PricedEstimate,
    label_lines: Mapping[str, Sequence[PricedLine]],
    labels: Sequence[str],
    policy: PricingPolicy,
    *,
    quote_labels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The recap value rows in order: group labels, vendor quotes,
    allowances, then the three adjustment rows. Each row: label and the
    cent values of D material, E labor, F material tax, G labor tax, I
    OHP labor, J OHP material (H and K are formulas)."""
    verify = priced.verify
    if verify is None:
        raise ValueError("A priced estimate without a Verify draft cannot be rendered.")
    rate = verify.labor_rate
    tax_pct = policy.tax_rate_pct
    labor_pct = policy.labor_markup_pct
    rows: list[dict[str, Any]] = []

    def row(label: str, *, d: Decimal = _ZERO, e: Decimal = _ZERO, f: Decimal = _ZERO, g: Decimal = _ZERO,
            i: Decimal = _ZERO, j: Decimal = _ZERO, section: str = "materials") -> dict[str, Any]:
        return {"label": label, "d": d, "e": e, "f": f, "g": g, "i": i, "j": j, "section": section}

    quoted_lines = 0
    for label in labels:
        if label.upper() == "ALLOWANCES":
            continue
        lines = list(label_lines.get(label, ()))
        d = _group_material_cents(lines)
        e = round_cents(_group_hours(lines) * rate)
        f = round_cents(d * tax_pct / Decimal(100))
        section = next((ln.pricing_section for ln in lines if ln.pricing_section), "materials")
        pct = _dec(policy.material_markup_pct.get(section, _ZERO))
        rows.append(row(label, d=d, e=e, f=f, i=round_cents(e * labor_pct / Decimal(100)),
                        j=round_cents((d + f) * pct / Decimal(100)), section=section))
        quoted_lines += sum(1 for ln in lines if FLAG_QUOTE_COVERED in ln.flags)
    for cat in sorted(priced.categories):
        entry = priced.categories[cat]
        if not entry.get("quote_id"):
            continue
        amount = round_cents(_dec(entry.get("quote_amount")))
        total, tax = apply_tax(amount, bool(entry.get("quote_tax_included")), tax_pct)
        section = str(entry.get("section") or "materials")
        pct = _dec(policy.material_markup_pct.get(section, _ZERO))
        vendor = (quote_labels or {}).get(str(entry["quote_id"]), "")
        label = LABEL_QUOTE.format(category=cat.upper()) + (f" ({vendor})" if vendor else "")
        rows.append(row(label, d=amount, f=tax, j=round_cents((total or amount) * pct / Decimal(100)), section=section))
    allowance_total = sum(
        (round_cents(_dec(entry.get("allowances"))) for entry in priced.categories.values() if entry.get("allowances") is not None),
        _ZERO,
    )
    if "ALLOWANCES" in [lb.upper() for lb in labels] or allowance_total:
        pct = _dec(policy.material_markup_pct.get(policy.allowance_section, _ZERO))
        rows.append(row("ALLOWANCES", d=allowance_total, j=round_cents(allowance_total * pct / Decimal(100)),
                        section=policy.allowance_section))

    # Adjustment rows: close on the Verify draft to the cent.
    labor_sum = sum((r["e"] for r in rows), _ZERO)
    labor_markup_sum = sum((r["i"] for r in rows), _ZERO)
    rows.append(row(
        f"{LABEL_ROUNDUP} ({verify.labor_hours_basis.normalize()} H TO {verify.labor_hours_reviewer.normalize()} H)",
        e=verify.labor_amount - labor_sum, i=verify.labor_markup_amount - labor_markup_sum,
    ))
    verify_tax = sum((round_cents(_dec(entry.get("tax"))) for entry in priced.categories.values()), _ZERO)
    tax_sum = sum((r["f"] for r in rows), _ZERO)
    rows.append(row(LABEL_TAX_ADJ, f=verify_tax - tax_sum))
    numbers = verify.numbers()
    verify_markup = sum(
        (numbers[k] or _ZERO for k in ("materials_markup_amount", "gear_markup_amount",
                                       "underground_markup_amount", "low_voltage_markup_amount")),
        _ZERO,
    )
    markup_sum = sum((r["j"] for r in rows), _ZERO)
    rows.append(row(LABEL_MARKUP_ADJ, j=verify_markup - markup_sum))
    return rows


def write_recap(ws: Worksheet, priced: PricedEstimate, rows: Sequence[dict[str, Any]], *, banner: str) -> dict[str, Any]:
    """Draw the recap through plans_reader_render._write_recap with every
    value row as a label, fill the values, the TOTALS and BID SUMMARY
    formulas and the BASE BID value. Returns the layout plus the cell
    addresses of each row."""
    verify = priced.verify
    assert verify is not None
    layout = prr._write_recap(ws, [r["label"] for r in rows])
    _banner(ws, banner, last_col=11)
    addresses: dict[str, dict[str, str]] = {}
    label_rows = list(layout["label_rows"].items())
    for (key, r), spec in zip(label_rows, rows, strict=True):
        values = {4: spec["d"], 5: spec["e"], 6: spec["f"], 7: spec["g"], 9: spec["i"], 10: spec["j"]}
        for col, value in values.items():
            c = ws.cell(row=r, column=col, value=float(value))
            prr._style(c, h="center", v="center", wrap=True, fmt=MONEY_FMT)
        h = ws.cell(row=r, column=8, value=f"=D{r}+E{r}+F{r}+G{r}")
        prr._style(h, h="center", v="center", fmt=MONEY_FMT)
        k = ws.cell(row=r, column=11, value=f"=H{r}+I{r}+J{r}")
        prr._style(k, h="center", v="center", fmt=MONEY_FMT)
        addresses[key] = {
            "row": str(r), "material": f"D{r}", "labor": f"E{r}", "material_tax": f"F{r}", "labor_tax": f"G{r}",
            "total_cost": f"H{r}", "ohp_labor": f"I{r}", "ohp_material": f"J{r}", "total_price": f"K{r}",
        }
    first, last, totals = layout["first_row"], layout["last_row"], layout["totals_row"]
    for col in range(4, 12):
        letter = chr(ord("A") + col - 1)
        c = ws.cell(row=totals, column=col, value=f"=SUM({letter}{first}:{letter}{last})")
        prr._style(c, bold=True, size=14, h="center", v="center", wrap=True, fmt=MONEY_FMT)
    summary = layout["summary_rows"]
    refs = {
        "TOTAL MATERIAL COST": f"=D{totals}",
        "MATERIAL SALES TAX": f"=F{totals}",
        "TOTAL LABOR COST": f"=E{totals}",
        "LABOR TAX": f"=G{totals}",
        "TOTAL COST": f"=H{totals}",
        "OVERHEAD & PROFIT (LABOR) @": f"=I{totals}",
        "OVERHEAD & PROFIT (MATERIAL) @": f"=J{totals}",
        "TOTAL COST WITH OVERHEADS + PROFIT": f"=K{totals}",
        "BID SECURITY": 0.0,
    }
    for label, value in refs.items():
        r = summary.get(label)
        if r is None:
            continue
        c = ws.cell(row=r, column=5, value=value)
        prr._style(c, bold=label[0].isupper() and label.startswith("TOTAL"), v="center", fmt=MONEY_FMT)
    base = layout["base_bid_row"]
    b = ws.cell(row=base, column=5, value=float(verify.bid_total))
    prr._style(b, bold=True, size=14, v="center", fill=prr._solid(prr.SUBTOTAL_FILL), fmt=MONEY_FMT)
    layout["addresses"] = addresses
    layout["base_bid_cell"] = f"E{base}"
    layout["check_rows"] = {
        "total_with_ohp": summary["TOTAL COST WITH OVERHEADS + PROFIT"],
        "bid_security": summary["BID SECURITY"],
        "base_bid": base,
    }
    layout["check_formula"] = (
        f"=ROUND(E{summary['TOTAL COST WITH OVERHEADS + PROFIT']}+E{summary['BID SECURITY']}-E{base},2)"
    )
    return layout


# Summary sheets ---------------------------------------------------------------------------------


def _kv_sheet(ws: Worksheet, title: str, pairs: Sequence[tuple[str, Any]], *, banner: str) -> None:
    _banner(ws, banner, last_col=4)
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 60
    t = ws.cell(row=2, column=1, value=title)
    prr._style(t, bold=True, size=14, border=False)
    r = 4
    for key, value in pairs:
        k = ws.cell(row=r, column=1, value=key)
        prr._style(k, bold=True, border=False)
        if isinstance(value, Decimal):
            v = ws.cell(row=r, column=2, value=float(value))
            prr._style(v, border=False, fmt=MONEY_FMT if "hours" not in key.lower() else HOURS_FMT)
        else:
            v = prr._text_cell(ws, r, 2, "" if value is None else str(value))
            prr._style(v, border=False, wrap=True)
        r += 1


def verify_summary_pairs(priced: PricedEstimate, *, version: int | None = None) -> list[tuple[str, Any]]:
    verify = priced.verify
    pairs: list[tuple[str, Any]] = [("Status", priced.status)]
    if version is not None:
        pairs.append(("Pricing version", version))
    if verify is not None:
        for key, value in verify.numbers().items():
            pairs.append((key.replace("_", " ").capitalize(), value if value is not None else "n/a"))
        pairs.extend([
            ("Bid total", verify.bid_total),
            ("Labor hours basis", verify.labor_hours_basis),
            ("Labor hours reviewer", verify.labor_hours_reviewer),
            ("Labor rate", verify.labor_rate),
            ("Labor rate source", verify.labor_rate_source),
        ])
    pairs.extend([
        ("Range low", priced.range_low if priced.range_low is not None else "n/a"),
        ("Range high", priced.range_high if priced.range_high is not None else "n/a"),
    ])
    qs = priced.quality_summary or {}
    shares = qs.get("hours_share_by_quality") or {}
    pairs.append(("Hours share by quality", ", ".join(f"{k}: {float(v):.1%}" for k, v in sorted(shares.items())) or "n/a"))
    shares_m = qs.get("material_share_by_quality") or {}
    pairs.append(("Material share by quality", ", ".join(f"{k}: {float(v):.1%}" for k, v in sorted(shares_m.items())) or "n/a"))
    pairs.append(("Fallback share (hours)", f"{float(qs.get('fallback_share_hours') or 0):.1%}"))
    pairs.append(("Provisional reasons", ", ".join(qs.get("provisional_reasons") or ()) or "none"))
    pairs.append(("Unpriced lines", ", ".join(priced.unpriced.lines) or "none"))
    pairs.append(("Blockers", "; ".join(f"{b.code}: {b.detail}" for b in priced.blockers) or "none"))
    pairs.append(("Warnings", "; ".join(f"{w.code}: {w.detail}" for w in priced.warnings) or "none"))
    pairs.append(("Policy version", priced.policy_version))
    pairs.append(("Library fingerprint", priced.library_fingerprint))
    pairs.append(("Excluded project ids sha256", priced.excluded_project_ids_sha256))
    return pairs


def _table_sheet(ws: Worksheet, title: str, header: Sequence[str], rows: Iterable[Any], *, banner: str) -> int:
    _banner(ws, banner, last_col=max(4, len(header)))
    t = ws.cell(row=2, column=1, value=title)
    prr._style(t, bold=True, size=14, border=False)
    for i, h in enumerate(header, start=1):
        c = ws.cell(row=4, column=i, value=h)
        prr._style(c, bold=True, h="center", v="center", wrap=True, fill=prr._solid(prr.HEADER_FILL))
        ws.column_dimensions[chr(ord("A") + i - 1)].width = 28 if i != 2 else 60
    r = 5
    for row in rows:
        values = [row.get(h) for h in header] if isinstance(row, Mapping) else list(row)
        for i, value in enumerate(values, start=1):
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                c = ws.cell(row=r, column=i, value=float(value))
                prr._style(c, v="center")
            else:
                c = prr._text_cell(ws, r, i, "" if value is None else str(value))
                prr._style(c, v="center", wrap=True)
        r += 1
    return r


def _header_of(rows: Sequence[Any], default: Sequence[str]) -> list[str]:
    if rows and isinstance(rows[0], Mapping):
        header: list[str] = []
        for row in rows:
            for key in row:
                if key not in header:
                    header.append(str(key))
        return header
    return list(default)


# Entry point -----------------------------------------------------------------------------------


def render_priced_estimate_xlsx(
    boq_spec: Mapping[str, Any],
    estimate_spec: Mapping[str, Any],
    priced: PricedEstimate,
    *,
    date: datetime.date,
    policy: PricingPolicy = DEFAULT_POLICY,
    version: int | None = None,
    basis_rows: Sequence[Any] = (),
    checks_rows: Sequence[Any] = (),
    quote_labels: Mapping[str, str] | None = None,
    signed_off: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """The priced workbook bytes and its recap layout (addresses, the
    base bid cell, the check formula). `basis_rows` and `checks_rows` are
    WS8's; dicts (header = their keys) or sequences (default headers)."""
    if priced.verify is None:
        raise ValueError("A priced estimate without a Verify draft cannot be rendered.")
    if not isinstance(estimate_spec, Mapping):
        raise ValueError("The estimate spec must be an object.")
    boq = dict(boq_spec)
    est = dict(estimate_spec)
    areas = prr._areas(boq)
    banner = SIGNED_BANNER.format(by=signed_off) if signed_off else DRAFT_BANNER
    ids_by_path = _line_ids_by_path(boq)
    wb = Workbook()
    wb.active.title = "Sheet1"
    titles = prr._area_titles(areas, "Detailed Estimate")
    notes_per_sheet = prr._route_area_notes(est, areas, titles)
    area_labels: list[list[str]] = []
    label_lines: dict[str, list[PricedLine]] = {}
    for ai, (area, title, extra) in enumerate(zip(areas, titles, notes_per_sheet, strict=True)):
        ws = wb.create_sheet(title)
        area_labels.append(_write_priced_area(
            ws, area, ai, est, priced, ids_by_path, date, extra,
            labor_rate=priced.verify.labor_rate, banner=banner, label_lines=label_lines,
        ))
    recap_labels = area_labels[0] if len(areas) == 1 else prr._multi_area_recap(area_labels)
    rows = recap_rows(priced, label_lines, recap_labels, policy, quote_labels=quote_labels)
    layout = write_recap(wb.create_sheet(prr.RECAP_SHEET), priced, rows, banner=banner)
    _kv_sheet(wb.create_sheet(VERIFY_SHEET), "VERIFY SUMMARY (AI DRAFT)", verify_summary_pairs(priced, version=version), banner=banner)
    _table_sheet(wb.create_sheet(BASIS_SHEET), "BASIS", _header_of(basis_rows, DEFAULT_BASIS_HEADER), basis_rows, banner=banner)
    checks_ws = wb.create_sheet(CHECKS_SHEET)
    header = _header_of(checks_rows, DEFAULT_CHECKS_HEADER)
    next_row = _table_sheet(checks_ws, "CHECKS", header, checks_rows, banner=banner)
    c = prr._text_cell(checks_ws, next_row, 1, "recap_base_bid_equals_verify")
    prr._style(c, v="center")
    ref = f"'{prr.RECAP_SHEET}'!E"
    cr = layout["check_rows"]
    f = checks_ws.cell(
        row=next_row, column=2,
        value=f"=ROUND({ref}{cr['total_with_ohp']}+{ref}{cr['bid_security']}-{ref}{cr['base_bid']},2)",
    )
    prr._style(f, v="center", fmt="0.00")
    d = prr._text_cell(checks_ws, next_row, 3, "0.00 when the recap summary equals the Verify bid total (recalc check)")
    prr._style(d, v="center", wrap=True)
    wb.active = 1
    layout["render_version"] = RENDER_VERSION
    layout["labels"] = list(recap_labels)
    layout["sheetnames"] = list(wb.sheetnames)
    return prr._workbook_bytes(wb), layout


__all__ = [
    "RENDER_VERSION", "DRAFT_BANNER", "SIGNED_BANNER", "VERIFY_SHEET", "BASIS_SHEET", "CHECKS_SHEET",
    "LABEL_ROUNDUP", "LABEL_TAX_ADJ", "LABEL_MARKUP_ADJ", "priced_estimate_filename", "recap_rows",
    "write_recap", "verify_summary_pairs", "render_priced_estimate_xlsx",
]
