"""app/services/eval_reference.py on a synthetic backup (made-up project,
made-up prices): estimate reader, BOQ join, labor notes, G3 truth, the
corrected-total formula, adjudications and the data-quality checks."""

from __future__ import annotations

import io
import json
from pathlib import Path

import openpyxl
import pytest
from openpyxl.styles import Alignment, Font

from app.services import eval_reference as R

PROJECT = "11111111-2222-3333-4444-555555555555"
RATE = 100.0


def _boq_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    ws = wb.create_sheet("SHEET")
    for col, head in enumerate(("SR.\nNO.", "DESCRIPTION", "QUANTITY", "UNIT"), start=1):
        ws.cell(4, col, head).font = Font(bold=True)
    rows = [
        ("h0", "DEMOLITION", None, None),
        ("item", "REMOVAL OF LIGHTING FIXTURE AND CONTROLS BACK TO SOURCE", 10, "EA"),
        ("h0", "NEW WORK", None, None),
        ("h1", "WIRING\n(10% wastage is included)", None, None),
        ("h2", "CONDUCTORS (AWG)", None, None),
        ("item", "#12 THHN/THWN Cu", 1100, "FT"),
        ("h2", "CONDUITS", None, None),
        ("item", '3/4"C EMT', 550, "FT"),
        ("acc", '3/4"C EMT Compression Coupling Stl', 55, "EA"),
        ("h1", "OTHER ITEMS", None, None),
        ("h2", "RECEPTACLES (RECEPTACLES INCLUDES BOX AND COVER PLATE)", None, None),
        ("item", "DUPLEX RECEPTACLE, 125V", 20, "EA"),
        ("item", "DUPLEX RECEPTACLE, 125V, GFCI", 4, "EA"),
        ("h2", "MISCELLANEOUS", None, None),
        ("item", "#18/4C CONTROL CABLE", 100, "FT"),
        ("h1", "LIGHTING FIXTURES", None, None),
        ("item", "L1 - 2X4 TROFFER - CATALOG 1", 8, "EA"),
        ("item", "L1-EM - 2X4 TROFFER - CATALOG 1", 2, "EA"),
    ]
    for offset, (kind, text, qty, unit) in enumerate(rows):
        r = 5 + offset
        b = ws.cell(r, 2, text)
        if kind != "acc":
            ws.cell(r, 1, 1)
        if kind in ("h0", "h1"):
            b.font = Font(bold=True)
            b.alignment = Alignment(horizontal="center")
        elif kind == "h2":
            b.font = Font(bold=True)
            b.alignment = Alignment(horizontal="left")
        elif kind == "acc":
            b.alignment = Alignment(horizontal="right")
        if qty is not None:
            ws.cell(r, 3, qty)
        if unit is not None:
            ws.cell(r, 4, unit)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# (description, base qty, wastage, unit, unit hours, unit material)
EST_LINES = [
    ("DEMOLITION", None),
    ("REMOVAL OF LIGHTING FIXTURE AND CONTROLS BACK TO SOURCE", (10, 0, "EA", 1.0, 0)),
    ("NEW WORK", None),
    ("WIRING", None),
    ("CONDUCTORS (AWG)", None),
    ("#12 THHN/THWN Cu", (1000, 0.1, "FT", 0.01, 0.2)),
    ("CONDUITS", None),
    ('3/4"C EMT', (500, 0.1, "FT", 0.02, 1.0)),
    ("OTHER ITEMS", None),
    ("RECEPTACLES (RECEPTACLES INCLUDES BOX AND COVER PLATE)", None),
    ("DUPLEX RECEPTACLE, 125V", (20, 0, "EA", 0.5, 10.0)),
    ("DUPLEX RECEPTACLE, 125V, GFCI", (4, 0, "EA", 0.5, 20.0)),
    ("MISCELLANEOUS", None),
    ("#18/4C CONTROL CABLE", (100, 0.1, "FT", 0.01, 0.5)),  # BOQ carries the base qty here
    ("LIGHTING FIXTURES", None),
    ("L1 - 2X4 TROFFER - CATALOG 1", (8, 0, "EA", 1.0, 100.0)),
    ("L1-EM - 2X4 TROFFER - CATALOG 1", (2, 0, "EA", 1.0, 150.0)),
]


def _estimate_xlsx() -> tuple[bytes, dict]:
    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    ws = wb.create_sheet("Detailed Estimate")
    ws.cell(4, 12, "LABOR RATE")
    ws.cell(4, 14, RATE)
    headers = ["SR.\nNO.", "DRAWING SHEET#", "DESCRIPTION", "QUANTITY", "WASTAGE", "QTY WITH\nWASTAGE", "UNIT", "UNIT LABOR HOURS", "UNIT MATERIAL COST", "UNIT LABOR COST", "TOTAL LABOR HOURS", "TOTAL MATERIAL COST", "TOTAL LABOR COST", "UNIT ITEM COST", "TOTAL ITEM COST"]
    for c, h in enumerate(headers, start=1):
        ws.cell(7, c, h)
    r = 8
    sr = 0
    totals = {"material": 0.0, "labor": 0.0, "hours": 0.0}
    sections: dict[str, dict[str, float]] = {}
    current = ""
    for desc, spec in EST_LINES:
        if spec is None:
            ws.cell(r, 3, desc)
            if desc in ("DEMOLITION", "WIRING", "OTHER ITEMS", "LIGHTING FIXTURES"):
                current = desc
            r += 1
            continue
        base, wastage, unit, uh, um = spec
        sr += 1
        qty = base * (1 + wastage)
        ws.cell(r, 1, sr)
        ws.cell(r, 3, desc)
        ws.cell(r, 4, base)
        ws.cell(r, 5, wastage)
        ws.cell(r, 6, qty)
        ws.cell(r, 7, unit)
        ws.cell(r, 8, uh)
        ws.cell(r, 9, um)
        ws.cell(r, 10, uh * RATE)
        ws.cell(r, 11, qty * uh)
        ws.cell(r, 12, qty * um)
        ws.cell(r, 13, qty * uh * RATE)
        ws.cell(r, 14, um + uh * RATE)
        ws.cell(r, 15, qty * (um + uh * RATE))
        totals["material"] += qty * um
        totals["labor"] += qty * uh * RATE
        totals["hours"] += qty * uh
        s = sections.setdefault(current, {"material": 0.0, "labor": 0.0})
        s["material"] += qty * um
        s["labor"] += qty * uh * RATE
        r += 1
    cost = totals["material"] + totals["labor"]
    ohp_l, ohp_m = totals["labor"] * 0.25, totals["material"] * 0.25
    bid = cost + ohp_l + ohp_m
    r += 1
    for label, value, pct in (("TOTAL MATERIAL COST", totals["material"], None), ("TOTAL LABOR COST", totals["labor"], None), ("TOTAL COST", cost, None), ("TOTAL LABOR HOURS", totals["hours"], None), ("OVERHEAD & PROFIT (LABOR)", ohp_l, 0.25), ("OVERHEAD & PROFIT (MATERIAL)", ohp_m, 0.25), ("TOTAL BID", bid, None)):
        ws.cell(r, 10, label)
        if pct is not None:
            ws.cell(r, 13, pct)
        ws.cell(r, 14, value)
        r += 1
    recap = wb.create_sheet("Bid Recap & Summary")
    recap.cell(2, 2, "BID RECAP")
    for c, h in enumerate(("SR. NO.", "DESCRIPTION", "MATERIAL COST", "LABOR COST", "MATERIAL TAX", "LABOR TAX", "TOTAL COST", "OHP (LABOR)", "OHP (MATERIAL)", "TOTAL PRICE"), start=2):
        recap.cell(3, c, h)
    rr = 4
    for i, (name, s) in enumerate(sections.items(), start=1):
        recap.cell(rr, 2, i)
        recap.cell(rr, 3, name)
        recap.cell(rr, 4, s["material"])
        recap.cell(rr, 5, s["labor"])
        recap.cell(rr, 6, 0)
        recap.cell(rr, 7, 0)
        recap.cell(rr, 8, s["material"] + s["labor"])
        recap.cell(rr, 9, s["labor"] * 0.25)
        recap.cell(rr, 10, s["material"] * 0.25)
        recap.cell(rr, 11, (s["material"] + s["labor"]) * 1.25)
        rr += 1
    recap.cell(rr, 3, "TOTALS")
    recap.cell(rr, 4, totals["material"])
    recap.cell(rr, 5, totals["labor"])
    recap.cell(rr, 6, 0)
    recap.cell(rr, 7, 0)
    recap.cell(rr, 8, cost)
    recap.cell(rr, 9, ohp_l)
    recap.cell(rr, 10, ohp_m)
    recap.cell(rr, 11, bid)
    rr += 2
    recap.cell(rr, 2, "BID SUMMARY")
    rr += 1
    for label, value, rate in (("TOTAL MATERIAL COST", totals["material"], None), ("MATERIAL SALES TAX", 0, 0), ("TOTAL LABOR COST", totals["labor"], None), ("LABOR TAX", 0, 0), ("TOTAL COST", cost, None), ("OVERHEAD & PROFIT (LABOR) @", ohp_l, 0.25), ("OVERHEAD & PROFIT (MATERIAL) @", ohp_m, 0.25), ("TOTAL COST WITH OVERHEADS + PROFIT", bid, None), ("BID SECURITY", 0, 0), ("BASE BID PRICE", bid, None)):
        recap.cell(rr, 3, label)
        if rate is not None:
            recap.cell(rr, 4, rate)
        recap.cell(rr, 5, value)
        rr += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), {"cost": cost, "bid": bid, "hours": totals["hours"], "sections": sections, "material": totals["material"]}


@pytest.fixture
def backup(tmp_path: Path) -> tuple[Path, dict]:
    data = tmp_path / "data"
    data.mkdir()
    storage = tmp_path / "storage" / "project-files" / PROJECT
    (storage / "boq").mkdir(parents=True)
    (storage / "estimate").mkdir(parents=True)
    (storage / "quote").mkdir(parents=True)
    boq_path = storage / "boq" / "aa-BOQ - COUNTS.xlsx"
    boq_path.write_bytes(_boq_xlsx())
    est_bytes, expected = _estimate_xlsx()
    est_path = storage / "estimate" / "bb-ELECTRICAL ESTIMATE.xlsx"
    est_path.write_bytes(est_bytes)
    quote_path = storage / "quote" / "cc-Vendor Lighting Quote.pdf"
    quote_path.write_bytes(b"%PDF-1.4 synthetic")
    wiring_material = expected["sections"]["WIRING"]["material"]
    other_items_material = expected["sections"]["OTHER ITEMS"]["material"]
    labor = 300 * 125.0
    gm_taxed = R.taxed(wiring_material, False, 8.375)
    lighting_taxed = R.taxed(1500.0, False, 8.375)
    materials = round(gm_taxed + lighting_taxed, 2)
    markup = round(materials * 0.10, 2)
    sent_total = round(labor + materials + markup, 2)
    tables = {
        "projects": [{"id": PROJECT, "name": "Synthetic TI", "number": "26.9.9999", "wage_type": "prevailing_wage", "labor_time": "day_work", "project_type": "ti", "owner_type": "public_other", "labor_needed": "union", "bid_method": "hard_bid", "address": "1 Test St"}],
        "project_files": [
            {"id": "f-boq", "project_id": PROJECT, "category": "boq", "storage_path": f"{PROJECT}/boq/aa-BOQ - COUNTS.xlsx", "filename": "BOQ - COUNTS.xlsx", "created_at": "2026-09-01T10:00:00+00:00"},
            {"id": "f-est", "project_id": PROJECT, "category": "estimate", "storage_path": f"{PROJECT}/estimate/bb-ELECTRICAL ESTIMATE.xlsx", "filename": "ELECTRICAL ESTIMATE.xlsx", "created_at": "2026-09-01T10:00:00+00:00"},
            {"id": "f-quote", "project_id": PROJECT, "category": "quote", "storage_path": f"{PROJECT}/quote/cc-Vendor Lighting Quote.pdf", "filename": "Vendor Lighting Quote.pdf", "created_at": "2026-09-02T10:00:00+00:00"},
        ],
        "proposal_sends": [
            {"id": "s1", "project_id": PROJECT, "gc_id": "gc1", "status": "sent", "sent_at": "2026-09-03T18:00:00+00:00", "labor_amount": labor, "material_amount": materials + markup, "gear_amount": None, "underground_amount": None, "low_voltage_amount": 0.0},
            {"id": "s2", "project_id": PROJECT, "gc_id": "gc2", "status": "sent", "sent_at": "2026-09-03T19:00:00+00:00", "labor_amount": labor, "material_amount": materials + markup, "gear_amount": None, "underground_amount": None, "low_voltage_amount": 0.0},
            {"id": "s3", "project_id": PROJECT, "gc_id": "gc3", "status": "failed", "sent_at": None, "labor_amount": 1, "material_amount": 1},
        ],
        "verifications": [{"id": "v1", "project_id": PROJECT, "committed_at": "2026-09-03T17:00:00+00:00", "labor_amount": labor, "materials_amount": materials, "labor_markup_amount": None, "materials_markup_amount": markup, "gear_amount": None, "gear_markup_amount": None, "underground_amount": None, "underground_markup_amount": None, "low_voltage_amount": 0.0, "low_voltage_markup_amount": None}],
        "labor_reviews": [{"id": "l1", "project_id": PROJECT, "labor_notes": "Labor Hours = 300\nLabor Rate = $125", "labor_amount": labor, "labor_breakdown": []}],
        "material_categories": [
            {"id": "mc-gm", "name": "General Material", "kind": "material", "is_general": True, "pricing_section": "materials"},
            {"id": "mc-lt", "name": "Lighting", "kind": "material", "is_general": False, "pricing_section": "materials"},
        ],
        "rfqs": [
            {"id": "rfq-gm", "project_id": PROJECT, "material_category_id": "mc-gm", "status": "draft", "custom_amount": None},
            {"id": "rfq-lt", "project_id": PROJECT, "material_category_id": "mc-lt", "status": "quotes_in", "custom_amount": None},
        ],
        "quotes": [
            {"id": "q-gm", "rfq_id": "rfq-gm", "vendor_id": None, "amount": wiring_material, "quote_file_id": None, "is_selected": True, "tax_included": False, "tax_rate": 8.375, "is_approved": True, "origin": "manual", "received_at": "2026-09-03T10:00:00+00:00"},
            {"id": "q-lt1", "rfq_id": "rfq-lt", "vendor_id": "vend1", "amount": 1500.0, "quote_file_id": "f-quote", "is_selected": True, "tax_included": False, "tax_rate": 8.375, "is_approved": True, "origin": "vendor", "received_at": "2026-09-02T10:00:00+00:00"},
            {"id": "q-lt2", "rfq_id": "rfq-lt", "vendor_id": "vend2", "amount": 1400.0, "quote_file_id": None, "is_selected": False, "tax_included": False, "tax_rate": 8.375, "is_approved": True, "origin": "vendor", "received_at": "2026-09-02T11:00:00+00:00"},
        ],
        "general_material_estimates": [{"project_id": PROJECT, "amount": wiring_material, "tax_included": False, "tax_rate": 8.375, "source": "extracted", "raw_extraction": {"found": True, "wiring_material_cost": wiring_material}}],
        "project_gcs": [{"id": "pg1", "project_id": PROJECT, "gc_id": "gc1", "needs_by": "2026-09-03"}],
        "general_contractors": [{"id": "gc1", "name": "GC One"}, {"id": "gc2", "name": "GC Two"}],
    }
    for name, rows in tables.items():
        (data / f"{name}.json").write_text(json.dumps(rows))
    expected.update({"labor": labor, "sent_total": sent_total, "materials": materials, "markup": markup, "wiring_material": wiring_material, "other_items_material": other_items_material, "gm_taxed": gm_taxed, "lighting_taxed": lighting_taxed})
    return tmp_path, expected


# parse_estimate_lines and the join --------------------------------------------------


def test_parse_estimate_lines_synthetic(backup):
    root, expected = backup
    xlsx = (root / "storage" / "project-files" / PROJECT / "estimate" / "bb-ELECTRICAL ESTIMATE.xlsx").read_bytes()
    book = R.parse_estimate_lines(xlsx)
    assert book.labor_rate == RATE and book.sheets == ["Detailed Estimate"] and not book.warnings
    assert len(book.lines) == 8
    wire = next(ln for ln in book.lines if ln.description == "#12 THHN/THWN Cu")
    assert (wire.section, wire.group, wire.subgroup) == ("NEW WORK", "WIRING", "CONDUCTORS (AWG)")
    assert wire.qty_base == 1000 and wire.wastage == pytest.approx(0.1) and wire.qty == pytest.approx(1100)
    assert wire.unit_hours == 0.01 and wire.unit_material == 0.2 and wire.unit_item_cost == pytest.approx(1.2)
    assert book.totals["hours"] == pytest.approx(expected["hours"]) and book.totals["bid"] == pytest.approx(expected["bid"])
    assert book.totals["ohp_labor_pct"] == 0.25
    recap = R.parse_recap_sections(xlsx)
    assert recap["sheet"] == "Bid Recap & Summary"
    assert recap["sections"]["WIRING"]["material"] == pytest.approx(expected["sections"]["WIRING"]["material"])
    assert recap["totals"]["total"] == pytest.approx(expected["bid"])


def test_join_boq_estimate_wastage(backup):
    root, _expected = backup
    base = root / "storage" / "project-files" / PROJECT
    boq = R.parse_estimator_boq((base / "boq" / "aa-BOQ - COUNTS.xlsx").read_bytes())
    book = R.parse_estimate_lines((base / "estimate" / "bb-ELECTRICAL ESTIMATE.xlsx").read_bytes())
    lines, warnings = R.join_boq_estimate(boq, book.lines, labor_rate=book.labor_rate)
    assert warnings == []
    by = {ln["description"]: ln for ln in lines}
    wire = by["#12 THHN/THWN Cu"]
    assert wire["qty"] == 1100 and wire["qty_base"] == 1000 and wire["unit_item_cost"] == pytest.approx(1.2)
    assert wire["value"] == pytest.approx(1100 * 1.2) and wire["kind"] == "conductor" and wire["est_ref"]
    # control cable: the BOQ carries the base quantity, the join still finds it
    assert by["#18/4C CONTROL CABLE"]["est_ref"] and by["#18/4C CONTROL CABLE"]["qty"] == 100
    acc = by['3/4"C EMT Compression Coupling Stl']
    assert acc["is_accessory"] and acc["est_ref"] is None
    assert by["L1-EM - 2X4 TROFFER - CATALOG 1"]["unit_material"] == 150.0


def test_parse_labor_notes_variants():
    assert R.parse_labor_notes("Labor Hours = 625\nLabor Rate = $125") == (625.0, 125.0)
    assert R.parse_labor_notes("labor Hours = 160\nLabor Rate = 125") == (160.0, 125.0)
    assert R.parse_labor_notes("Labor Hour = 8500 \nLabor Rate = $125/hr.") == (8500.0, 125.0)
    assert R.parse_labor_notes("2 Guys, 2 Weeks, @125 Per Hour") == (None, None)
    assert R.parse_labor_notes("") == (None, None) and R.parse_labor_notes(None) == (None, None)
    hours, rate = R.parse_labor_notes("Labor Rate = 450\nLaboe Rate = $125")
    assert hours is None


def test_taxed_and_verify_sum_rule():
    assert R.taxed(100.0, False, 8.375) == 108.38
    assert R.taxed(100.0, True, 8.375) == 100.0
    assert R.taxed(100.0, False, None) == 108.38
    row = {"labor_amount": 10.0, "materials_amount": 5.555, "materials_markup_amount": None}
    assert R.verify_total(row) == 15.56
    assert R.sent_total({"labor_amount": 1, "material_amount": 2.5, "gear_amount": None}) == 3.5


# build_reference -----------------------------------------------------------------------


def test_build_reference_g3_truth_and_data_quality(backup):
    root, expected = backup
    ref = R.build_reference("T1", PROJECT, root)
    assert ref["schema"] == R.REFERENCE_SCHEMA and ref["project_id"] == PROJECT
    assert ref["sources"]["boq"]["sha256"] and ref["sources"]["estimate"]["filename"] == "ELECTRICAL ESTIMATE.xlsx"
    assert len(ref["quantity_lines"]) == 8 and len(ref["accessory_lines"]) == 1
    assert ref["estimator_recap"]["totals"]["bid"] == pytest.approx(expected["bid"])
    assert ref["estimator_recap"]["totals"]["labor_rate"] == RATE
    assert ref["estimator_recap"]["parse_recap"]["base"] == pytest.approx(expected["bid"], abs=0.01)
    assert ref["g3_sent"]["total"] == pytest.approx(expected["sent_total"]) and ref["g3_sent"]["sends"] == 2
    assert ref["g3_sent"]["distinct_totals"] == [pytest.approx(expected["sent_total"])] and ref["g3_sent"]["spread"] == 0.0
    assert ref["g3_sent"]["last_sent_at"].endswith("-07:00")
    assert ref["g3_verify"]["matches_sent"] is True and ref["g3_verify"]["sum"] == pytest.approx(expected["sent_total"])
    assert ref["g3_labor"] == {"amount": expected["labor"], "hours": 300.0, "rate": 125.0, "parsed": True, "amount_consistent": True,
                               "notes": "Labor Hours = 300\nLabor Rate = $125"}
    lighting = next(m for m in ref["g3_materials"] if m["category"] == "Lighting")
    assert lighting["selected"]["amount"] == 1500.0 and lighting["selected"]["file"]["sha256"]
    assert [a["amount"] for a in lighting["alternatives"]] == [1400.0]
    assert lighting["replaces_section"] == "LIGHTING FIXTURES"
    assert ref["g3_materials_selected_taxed_total"] == pytest.approx(expected["materials"])
    assert ref["g3_markups"]["materials_pct"] == pytest.approx(10.0)
    gm = ref["g3_general_material"]
    assert gm["wiring_only"] is True and gm["other_items_material_missing"] == pytest.approx(expected["other_items_material"])
    assert ref["derived"]["hour_roundup"] == pytest.approx(300 / expected["hours"], abs=1e-4)
    assert ref["derived"]["quote_replaced_sections"] == ["LIGHTING FIXTURES"]
    assert ref["conditions"]["wage_type"] == "prevailing_wage" and ref["project_gcs"][0]["gc_name"] == "GC One"
    statuses = {c["check"]: c["status"] for c in ref["data_quality"]}
    assert statuses == {"verify_sum_matches_sent": "pass", "materials_from_quotes": "pass", "boq_estimate_join_rate": "pass", "recap_cost_matches_lines": "pass",
                        "labor_notes_parsed": "pass", "labor_amount_consistent": "pass"}
    # the corrected total follows the Verify formula with per-category cent rounding
    corrected = ref["derived"]["g3_corrected_detail"]
    gm_corrected = round(expected["wiring_material"] + expected["other_items_material"], 2)
    assert corrected["general_material_pretax"] == pytest.approx(gm_corrected)
    materials = round(R.taxed(gm_corrected, False, 8.375) + expected["lighting_taxed"], 2)
    assert corrected["materials"] == pytest.approx(materials)
    assert corrected["total"] == pytest.approx(round(materials + round(materials * 0.10, 2) + expected["labor"], 2))
    assert ref["derived"]["g3_corrected_total"] == corrected["total"]
    assert ref["adjudications_applied"] == [] and ref["aliases"] == {} and ref["quantity_lines_adjudicated"] == ref["quantity_lines"]
    # round trip and oracle spec
    path = R.write_reference(ref, root / "eval")
    loaded = R.load_reference(root / "eval", "T1")
    assert loaded["g3_sent"]["total"] == ref["g3_sent"]["total"] and path.parent.name == "T1"
    spec = R.reference_to_boq_spec(loaded)
    items = [i for a in spec["areas"] for s in a["sections"] for g in s["groups"] for sg in g["subgroups"] for i in sg["items"]]
    assert len(items) == 8 and all(i["wastage_applies"] is False for i in items)
    assert chr(0x2014) not in json.dumps(ref, default=str)


def test_labor_notes_kept_when_the_amount_disagrees(backup):
    # 8.6: hours and rate come from the regexes alone; a labor amount that
    # hours x rate does not reproduce is reported, never used to null them.
    root, _expected = backup
    path = root / "data" / "labor_reviews.json"
    rows = json.loads(path.read_text())
    rows[0]["labor_notes"] = "Labor Hours = 140\nLabor Rate = $90"
    rows[0]["labor_amount"] = 11700.0  # 140 x 90 = 12,600
    path.write_text(json.dumps(rows))
    data = R.BackupData(root)
    labor = R.g3_truth(data, PROJECT)["g3_labor"]
    assert labor["hours"] == 140.0 and labor["rate"] == 90.0 and labor["parsed"] is True
    assert labor["amount_consistent"] is False
    checks = {c["check"]: c for c in R.data_quality_checks({"g3_labor": labor})}
    assert checks["labor_notes_parsed"]["status"] == "pass"
    assert checks["labor_amount_consistent"]["status"] == "fail"
    assert checks["labor_amount_consistent"]["detail"]["rel_delta"] == pytest.approx(900 / 11700, abs=1e-4)
    assert R.pool_data_quality([PROJECT], root)["labor_amount_inconsistent"] == [PROJECT[:8]]
    rows[0]["labor_amount"] = None
    path.write_text(json.dumps(rows))
    labor = R.g3_truth(R.BackupData(root), PROJECT)["g3_labor"]
    assert labor["hours"] == 140.0 and labor["amount_consistent"] is None
    assert {c["check"]: c["status"] for c in R.data_quality_checks({"g3_labor": labor})}["labor_amount_consistent"] == "skip"


def test_apply_adjudications_add_and_reprice(backup):
    root, expected = backup
    ref = R.build_reference("T1", PROJECT, root)
    adjudications = [
        {"id": "A1", "type": "add_line", "reviewed": False, "line": {"section": "NEW WORK", "group": "SWITCHGEAR", "subgroup": "CIRCUIT BREAKERS", "description": "20A/1P CIRCUIT BREAKER, GFCI", "qty": 4, "unit": "EA", "unit_hours": 0.5, "unit_material": 50.0}},
        {"id": "A2", "type": "set_unit_cost", "reviewed": True, "match": {"description_prefix": "L1-EM", "group": "LIGHTING FIXTURES"}, "value": {"unit_material": 100.0}},
        {"id": "A3", "type": "set_qty", "reviewed": False, "match": {"description": "DUPLEX RECEPTACLE, 125V, GFCI"}, "value": 5},
        {"id": "A4", "type": "price_truth", "reviewed": False, "value": {"g3_corrected_total": 12345.67}},
        {"id": "A5", "type": "note", "reviewed": False, "reason": "L5 measured"},
        {"id": "A6", "type": "remove_line", "reviewed": False, "match": {"description": "#18/4C CONTROL CABLE"}},
        {"id": "A7", "type": "alias", "reviewed": False, "value": {"SHADED": "EM"}},
        {"id": "A8", "type": "bogus", "reviewed": False},
        {"id": "A9", "type": "set_qty", "reviewed": False, "match": {"description": "NOT THERE"}, "value": 1},
    ]
    out = R.apply_adjudications(ref, adjudications)
    adj = {ln["description"]: ln for ln in out["quantity_lines_adjudicated"]}
    assert "20A/1P CIRCUIT BREAKER, GFCI" in adj and adj["20A/1P CIRCUIT BREAKER, GFCI"]["kind"] == "breaker"
    assert adj["20A/1P CIRCUIT BREAKER, GFCI"]["unit_item_cost"] == pytest.approx(50.0 + 0.5 * RATE)
    assert adj["20A/1P CIRCUIT BREAKER, GFCI"]["value"] == pytest.approx(4 * (50.0 + 0.5 * RATE))
    assert adj["L1-EM - 2X4 TROFFER - CATALOG 1"]["unit_material"] == 100.0
    assert adj["L1-EM - 2X4 TROFFER - CATALOG 1"]["value"] == pytest.approx(2 * (100.0 + 1.0 * RATE))
    assert adj["DUPLEX RECEPTACLE, 125V, GFCI"]["qty"] == 5
    assert "#18/4C CONTROL CABLE" not in adj and "#18/4C CONTROL CABLE" in {ln["description"] for ln in out["quantity_lines"]}
    assert out["aliases"] == {"SHADED": "EM"}
    assert out["price_truth_adjudicated"] == {"g3_corrected_total": 12345.67}
    assert out["derived"]["g3_corrected_total"] == 12345.67 and out["derived"]["g3_corrected_total_source"] == "adjudication"
    assert any("differs" in w for w in out["warnings"])
    assert [a["id"] for a in out["adjudications_ignored"]] == ["A8", "A9"]
    assert out["adjudications_reviewed"] is False
    assert [a["status"] for a in out["adjudications_applied"] if a["id"] == "A2"] == ["reviewed"]
    # load_adjudications accepts the file shape with a top-level aliases block
    path = root / "adjudications.json"
    path.write_text(json.dumps({"adjudications": adjudications[:1], "aliases": {"SHADED": "EM"}, "aliases_reviewed": False}))
    loaded = R.load_adjudications(path)
    assert [a["type"] for a in loaded] == ["add_line", "alias"]
    assert R.load_adjudications(root / "missing.json") == []


def test_pick_estimate_file_one_book_rule(tmp_path):
    data = R.BackupData(tmp_path)
    data._tables["project_files"] = [
        {"id": "1", "project_id": "p", "category": "estimate", "filename": "ESTIMATE.xlsx", "storage_path": "p/e1.xlsx", "created_at": "2026-01-01"},
        {"id": "2", "project_id": "p", "category": "estimate", "filename": "ESTIMATE Rev 2.xlsx", "storage_path": "p/e2.xlsx", "created_at": "2026-01-02"},
        {"id": "3", "project_id": "p", "category": "estimate", "filename": "ESTIMATE Rev.1.xlsx", "storage_path": "p/e3.xlsx", "created_at": "2026-01-03"},
        {"id": "4", "project_id": "p", "category": "estimate", "filename": "notes.pdf", "storage_path": "p/n.pdf", "created_at": "2026-01-04"},
        {"id": "5", "project_id": "p", "category": "boq", "filename": "BOQ old.xlsx", "storage_path": "p/b1.xlsx", "created_at": "2026-01-01"},
        {"id": "6", "project_id": "p", "category": "boq", "filename": "BOQ - COUNTS.xlsx", "storage_path": "p/b2.xlsx", "created_at": "2026-01-05"},
    ]
    assert R.pick_estimate_file(data, "p")["id"] == "2"
    assert R.pick_boq_file(data, "p")["id"] == "6"
    assert R.pick_boq_file(data, "none") is None
