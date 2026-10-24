"""app/services/estimate_parse.py on synthetic workbooks built with openpyxl
in the test (no real estimator data, no I/O beyond bytes)."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import openpyxl
import pytest

from app.services import estimate_parse as ep

HEADERS = (
    "SR.\nNO.", "DRAWING SHEET#", "DESCRIPTION", "QUANTITY", "WASTAGE", "QTY WITH\nWASTAGE",
    "UNIT", "UNIT LABOR HOURS", "UNIT MATERIAL COST", "UNIT LABOR COST", "TOTAL LABOR HOURS",
    "TOTAL MATERIAL COST", "TOTAL LABOR COST", "UNIT ITEM COST", "TOTAL ITEM COST",
)


def _workbook(*, drop_sheet_col: bool = False, recap: bool = True, rate: bool = True) -> bytes:
    """A two-section estimate in the template layout, with synthetic units."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Detailed Estimate"
    ws["D2"], ws["E2"] = "PROJECT", "TEST PROJECT"
    if rate:
        ws["L4"], ws["N4"] = "LABOR RATE", 77
    ws["A5"], ws["B5"] = "DATE: ", dt.datetime(2026, 1, 15)
    headers = [h for h in HEADERS if not (drop_sheet_col and h == "DRAWING SHEET#")]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=7, column=i, value=h)
    off = 0 if drop_sheet_col else 1  # column shift when DRAWING SHEET# is gone
    c_desc, c_qty, c_w, c_qw, c_unit, c_h, c_i = (2 + off, 3 + off, 4 + off, 5 + off, 6 + off, 7 + off, 8 + off)
    rows = [
        ("DEMOLITION", None, None, None, None, None, None),
        ("REMOVAL OF LIGHT FIXTURE", 10, 0, 10, "EA", 0.5, 0),
        ("NEW WORK", None, None, None, None, None, None),
        ("WIRING\n(10% wastage is included)", None, None, None, None, None, None),
        ("CONDUCTORS (AWG)", None, None, None, None, None, None),
        ("#12 THHN/THWN Cu", 1000, 0.1, 1100, "FT", 0.01, 0.2),
        ("CONDUITS", None, None, None, None, None, None),
        ('3/4"C EMT', 500, 0.1, 550, "FT", 0.03, 1.0),
        ("OTHER ITEMS", None, None, None, None, None, None),
        ("RECEPTACLES (RECEPTACLES INCLUDES BOX)", None, None, None, None, None, None),
        ("DUPLEX RECEPTACLE", 20, 0, 20, "EA", 0.4, 10),
        ("ALLOWANCES", None, None, None, None, None, None),
        ("PERMIT AND UTILITY FEES", 0, None, None, "LS", None, None),
    ]
    r = 8
    for desc, qty, w, qw, unit, h, i in rows:
        ws.cell(row=r, column=c_desc, value=desc)
        if qty is not None:
            ws.cell(row=r, column=c_qty, value=qty)
        if w is not None:
            ws.cell(row=r, column=c_w, value=w)
        if qw is not None:
            ws.cell(row=r, column=c_qw, value=qw)
        if unit is not None:
            ws.cell(row=r, column=c_unit, value=unit)
        if h is not None:
            ws.cell(row=r, column=c_h, value=h)
        if i is not None:
            ws.cell(row=r, column=c_i, value=i)
        if desc == "REMOVAL OF LIGHT FIXTURE" and not drop_sheet_col:
            ws.cell(row=r, column=2, value="ED2.01, ED3.01")
        r += 1
        if desc == "REMOVAL OF LIGHT FIXTURE":
            ws.cell(row=r, column=16, value="SUBTOTAL MATERIAL")
            ws.cell(row=r, column=17, value="SUBTOTAL LABOR")
            ws.cell(row=r, column=18, value="SUBTOTAL")
            ws.cell(row=r + 1, column=16, value=0)
            ws.cell(row=r + 1, column=17, value=385)
            ws.cell(row=r + 1, column=18, value=385)
            r += 2
    ws.cell(row=r + 1, column=10, value="TOTAL MATERIAL COST  ")
    ws.cell(row=r + 1, column=14, value=970)
    ws.cell(row=r + 2, column=10, value="TOTAL LABOR HOURS")
    ws.cell(row=r + 2, column=14, value=40.5)
    if recap:
        rs = wb.create_sheet("Bid Recap & Summary")
        rs["B2"] = "BID RECAP"
        for i, h in enumerate(("SR. NO.", "DESCRIPTION", "MATERIAL COST", "LABOR COST", "MATERIAL TAX",
                               "LABOR TAX", "TOTAL COST", "OHP (LABOR)", "OHP (MATERIAL)", "TOTAL PRICE"), start=2):
            rs.cell(row=3, column=i, value=h)
        data = [("DEMOLITION", 0, 385), ("WIRING", 720, 1925), ("OTHER ITEMS", 200, 616), ("ALLOWANCES", 0, 0)]
        for n, (label, mat, lab) in enumerate(data, start=4):
            rs.cell(row=n, column=2, value=n - 3)
            rs.cell(row=n, column=3, value=label)
            rs.cell(row=n, column=4, value=mat)
            rs.cell(row=n, column=5, value=lab)
            rs.cell(row=n, column=11, value=(mat + lab) * 1.25)
        rs["C9"], rs["D9"], rs["E9"] = "TOTALS", 920, 2926
        rs["B11"] = "BID SUMMARY"
        rs["C12"], rs["E12"] = "TOTAL MATERIAL COST", 920
        rs["C13"], rs["D13"], rs["E13"] = "MATERIAL SALES TAX", 0, 0
        rs["C14"], rs["E14"] = "TOTAL LABOR COST", 2926
        rs["C15"], rs["D15"], rs["E15"] = "OVERHEAD & PROFIT (LABOR) @", 0.25, 731.5
        rs["C16"], rs["D16"], rs["E16"] = "OVERHEAD & PROFIT (MATERIAL) @", 0.2, 184
        rs["C18"], rs["E18"] = "BASE BID PRICE", 4761.5
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_template_workbook():
    parsed = ep.parse_estimate(_workbook())
    assert parsed.sheets == ["Detailed Estimate"]
    assert parsed.labor_rate == Decimal("77")
    assert parsed.workbook_date == dt.date(2026, 1, 15)
    items = parsed.item_lines
    assert [ln.description for ln in items] == [
        "REMOVAL OF LIGHT FIXTURE", "#12 THHN/THWN Cu", '3/4"C EMT', "DUPLEX RECEPTACLE",
    ]
    demo, wire, emt, recep = items
    assert (demo.section, demo.group, demo.subgroup) == ("DEMOLITION", "DEMOLITION", "")
    assert demo.sheet_refs == ("ED2.01", "ED3.01")
    assert (wire.section, wire.group, wire.subgroup) == ("NEW WORK", "WIRING", "CONDUCTORS (AWG)")
    assert wire.qty_base == Decimal("1000") and wire.wastage == Decimal("0.1")
    assert wire.quantity == Decimal("1100") and wire.quantity_eff == Decimal("1100")
    assert wire.unit_labor_hours == Decimal("0.01") and wire.unit_material_cost == Decimal("0.2")
    assert emt.group == "WIRING" and emt.subgroup == "CONDUITS"
    assert recep.group == "OTHER ITEMS" and recep.subgroup.startswith("RECEPTACLES")
    allow = parsed.allowance_lines
    assert len(allow) == 1 and allow[0].description == "PERMIT AND UTILITY FEES" and allow[0].unit == "LS"
    assert parsed.summary["total_material_cost"] == Decimal("970")
    assert parsed.summary["total_labor_hours"] == Decimal("40.5")
    assert parsed.total_labor_hours == Decimal("40.5")
    assert parsed.basis_hours() == Decimal("5") + Decimal("11") + Decimal("16.5") + Decimal("8")
    assert parsed.subtotals["Detailed Estimate:DEMOLITION"] == {
        "material": Decimal("0"), "labor": Decimal("385"), "total": Decimal("385"),
    }
    assert list(parsed.recap_sections) == ["DEMOLITION", "WIRING", "OTHER ITEMS", "ALLOWANCES"]
    assert parsed.recap_sections["WIRING"].material == Decimal("720")
    assert parsed.recap_totals is not None and parsed.recap_totals.labor == Decimal("2926")
    assert parsed.ohp_labor_pct == Decimal("25.00") and parsed.ohp_material_pct == Decimal("20.0")
    assert parsed.material_tax_pct == Decimal("0")
    assert parsed.base_bid == Decimal("4761.5")
    assert parsed.warnings == []
    assert parsed.to_dict()["lines"][1]["quantity"] == "1100"


def test_parse_off_template_column_maps_by_label():
    parsed = ep.parse_estimate(_workbook(drop_sheet_col=True))
    wire = parsed.item_lines[1]
    assert wire.description == "#12 THHN/THWN Cu"
    assert wire.qty_base == Decimal("1000") and wire.unit == "FT"
    assert wire.unit_labor_hours == Decimal("0.01") and wire.unit_material_cost == Decimal("0.2")
    assert wire.sheet_refs == ()


def test_missing_recap_and_rate_are_warnings_not_errors():
    parsed = ep.parse_estimate(_workbook(recap=False, rate=False))
    assert parsed.labor_rate is None
    assert parsed.recap_sections == {}
    assert any("LABOR RATE" in w for w in parsed.warnings)
    assert any("recap" in w for w in parsed.warnings)


def test_no_detail_sheet_raises():
    wb = openpyxl.Workbook()
    wb.active["A1"] = "nothing here"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ep.EstimateParseError, match="UNIT LABOR HOURS"):
        ep.parse_estimate(buf.getvalue())


def test_one_book_rule_prefers_latest_rev_else_most_lines():
    full = ep.parse_estimate(_workbook())
    small = ep.ParsedEstimate(sheets=["S"], labor_rate=None, workbook_date=None, lines=full.lines[:2])
    books = [
        ({"filename": "A ESTIMATE.xlsx", "created_at": "2026-02-01"}, full),
        ({"filename": "A ESTIMATE REV 1.xlsx", "created_at": "2026-02-03"}, small),
        ({"filename": "A ESTIMATE REV 2.xlsx", "created_at": "2026-02-05"}, small),
    ]
    assert ep.choose_book(books) == 2  # latest Rev wins over the bigger original
    assert ep.choose_book(books[:1]) == 0
    assert ep.choose_book([books[0], ({"filename": "B.xlsx", "created_at": "2026-03-01"}, small)]) == 0
    assert ep.choose_book([]) is None
    assert ep.is_rev_book("X - REVISED ESTIMATE.xlsx") and not ep.is_rev_book("X - ESTIMATE.xlsx")


def test_price_rows_and_allowances_from_parsed_book():
    parsed = ep.parse_estimate(_workbook())
    rows = ep.price_rows(parsed)
    assert [r.line_id for r in rows] == ["B1", "B2", "B3", "B4"]
    wire = rows[1]
    assert wire.quantity == Decimal("1100") and wire.qty_base == Decimal("1000")
    assert wire.wastage_applies is True and rows[0].wastage_applies is False
    assert wire.group == "WIRING" and wire.subgroup == "CONDUCTORS (AWG)" and wire.section == "NEW WORK"
    assert wire.quantity_basis == "manual"
    allowances = ep.allowances(parsed)
    assert len(allowances) == 1 and allowances[0].name == "PERMIT AND UTILITY FEES"
    assert allowances[0].amount_usd == Decimal("0")


def test_dry_run_markdown_lists_failures(tmp_path):
    f = ep.EstimateFile(
        project_id="abcdef12-0000", file_id="f", filename="X.xlsx", path=tmp_path / "x.xlsx",
        created_at="2026-01-01", submission_round=1, estimator_deliverable=True,
    )
    report = ep.DryRunReport(entries=[
        ep.DryRunEntry(file=f, ok=True, error=None, sheets=1, lines=4, allowance_rows=1,
                       labor_rate=True, workbook_date=True, recap_sections=3, chosen=True),
        ep.DryRunEntry(file=f, ok=False, error="no detail sheet"),
    ])
    text = ep.dry_run_markdown(report, generated_at="2026-10-21 12:00 PDT")
    assert "parsed 1; failed 1" in text
    assert "- abcdef12 X.xlsx: no detail sheet" in text
    assert "\u2014" not in text and "\u2013" not in text


def test_estimate_files_reads_project_files_json(tmp_path):
    root = tmp_path
    (root / "data").mkdir()
    (root / "storage" / "project-files" / "p1" / "estimate").mkdir(parents=True)
    (root / "storage" / "project-files" / "p1" / "estimate" / "a-EST.xlsx").write_bytes(_workbook())
    rows = [
        {"id": "f1", "project_id": "p1", "category": "estimate", "filename": "EST.xlsx",
         "storage_path": "p1/estimate/a-EST.xlsx", "created_at": "2026-01-01", "submission_round": 1,
         "estimator_deliverable": True},
        {"id": "f2", "project_id": "p1", "category": "estimate", "filename": "EST.pdf",
         "storage_path": "p1/estimate/b-EST.pdf", "created_at": "2026-01-02"},
        {"id": "f3", "project_id": "p1", "category": "boq", "filename": "BOQ.xlsx",
         "storage_path": "p1/boq/c-BOQ.xlsx", "created_at": "2026-01-03"},
    ]
    (root / "data" / "project_files.json").write_text(__import__("json").dumps(rows))
    files = ep.estimate_files(root)
    assert [f.file_id for f in files] == ["f1"]
    report, chosen = ep.parse_backup(root)
    assert report.parsed == 1 and list(chosen) == ["p1"] and report.entries[0].chosen
