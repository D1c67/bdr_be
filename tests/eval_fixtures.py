"""Synthetic eval fixtures shared by the WS0 bundle, replay, acceptance and
eval_run tests. Everything here is made up: synthetic project ids, blank
PDFs, unit costs of 1.0, a synthetic sent total. Quantities mirror the count
thresholds that spec 3.5 fixes as data (4 GFCI breakers, 5 thermostats, 74
receptacles, 35 demolition power devices ...), which are counts, not prices.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

from pypdf import PdfWriter

from app.services import plans_reader_score as score_mod
from app.services.estimating_contracts import excluded_project_ids_sha256
from app.services.plans_reader_acceptance import AcceptanceInputs

PROJECT_ID = "7154abcd-0000-4000-8000-000000000001"
EXCLUDED_IDS = [PROJECT_ID, "0bad0bad-0000-4000-8000-000000000002"]
RUN_ID = "ab12cd34-0000-4000-8000-000000000099"
ELEC_DOC = "d0c00001-0000-4000-8000-000000000001"
SPEC_DOC = "d0c00002-0000-4000-8000-000000000002"
ELEC_FILE = "f1le0001-0000-4000-8000-000000000001"
SPEC_FILE = "f1le0002-0000-4000-8000-000000000002"
ELEC_PF = "pf000001-0000-4000-8000-000000000001"
SPEC_PF = "pf000002-0000-4000-8000-000000000002"
RECEPT = 'RECEPTACLES (RECEPTACLES INCLUDES 4"S DEEP COMB BOX AND COVER PLATE)'


def blank_pdf(pages: int, *, width: float = 72 * 42, height: float = 72 * 30) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=width, height=height)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def make_backup(root: Path, *, elec_pages: int = 3, spec_pages: int = 4) -> Path:
    """A fake production backup: data/project_files.json rows for the two
    run files (folder names deliberately disagree with the category) and
    blank PDFs at their storage paths."""
    rows = [
        {"id": ELEC_PF, "project_id": PROJECT_ID, "category": "electrical_drawing",
         "storage_path": f"{PROJECT_ID}/misc/{'a' * 32}-Elec.pdf", "filename": "Elec.pdf", "size_bytes": 10},
        {"id": SPEC_PF, "project_id": PROJECT_ID, "category": "specification",
         "storage_path": f"{PROJECT_ID}/drawing/{'b' * 32}-Specs.pdf", "filename": "Specs.pdf", "size_bytes": 10},
    ]
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "project_files.json").write_text(json.dumps(rows))
    for row, pages in ((rows[0], elec_pages), (rows[1], spec_pages)):
        path = root / "storage" / "project-files" / row["storage_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blank_pdf(pages))
    return root


# Truth ---------------------------------------------------------------------------


def _line(i: int, section: str, group: str, subgroup: str, desc: str, qty: float, unit: str, *, accessory: bool = False) -> dict[str, Any]:
    return {
        "line_id": f"Q{i:03d}", "area": "SHEET", "section": section, "group": group, "subgroup": subgroup,
        "description": desc, "unit": unit, "qty": qty, "qty_base": qty, "wastage": 0.0,
        "unit_hours": 0.5, "unit_material": 0.5, "unit_item_cost": 1.0, "labor_rate": 1.0, "value": qty,
        "is_accessory": accessory, "boq_ref": f"SHEET!B{i}", "est_ref": f"Detailed Estimate!C{i}", "sheet_refs": [],
    }


TRUTH_LINES: list[tuple[str, str, str, str, float, str]] = [
    ("DEMOLITION", "DEMOLITION", "", "REMOVAL OF LIGHTING FIXTURE AND CONTROLS W/ ASSOCIATE WIRE & CONDUIT BACK TO SOURCE", 49, "EA"),
    ("DEMOLITION", "DEMOLITION", "", "REMOVAL OF POWER DEVICES W/ BACKBOX & ASSOCIATE WIRE, CONDUIT BACK TO SOURCE", 35, "EA"),
    ("DEMOLITION", "DEMOLITION", "", "REMOVAL OF LIGHTING TRACK", 50.07, "FT"),
    ("NEW WORK", "WIRING", "CONDUCTORS (AWG)", "#12 THHN/THWN Cu", 7390.15, "FT"),
    ("NEW WORK", "WIRING", "CONDUCTORS (AWG)", "#10 THHN/THWN Cu", 13524.28, "FT"),
    ("NEW WORK", "WIRING", "CONDUITS", '3/4"C EMT', 6971.48, "FT"),
    ("NEW WORK", "OTHER ITEMS", RECEPT, "DUPLEX RECEPTACLE, 125V", 54, "EA"),
    ("NEW WORK", "OTHER ITEMS", RECEPT, "DUPLEX RECEPTACLE, 125V, GFCI", 11, "EA"),
    ("NEW WORK", "OTHER ITEMS", RECEPT, "DUPLEX RECEPTACLE, 125V, TR", 3, "EA"),
    ("NEW WORK", "OTHER ITEMS", RECEPT, "DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY", 4, "EA"),
    ("NEW WORK", "OTHER ITEMS", RECEPT, "QUADPLEX RECEPTACLE", 1, "EA"),
    ("NEW WORK", "OTHER ITEMS", RECEPT, "QUADPLEX RECEPTACLE CONTROLLED BY OCCUPANCY", 1, "EA"),
    ("NEW WORK", "OTHER ITEMS", "MISCELLANEOUS", "ELECTRICAL CONECTION", 7, "EA"),
    ("NEW WORK", "OTHER ITEMS", "MISCELLANEOUS", "#18/4C CONTROL CABLE", 141, "FT"),
    ("NEW WORK", "OTHER ITEMS", "MISCELLANEOUS", "RELOCATION OF THERMOSTAT TO NEW LOCATION", 5, "EA"),
    ("NEW WORK", "LIGHTING FIXTURES", "FIXTURES", "L1 - 2' X 4' RECESSED TROFFER", 16, "EA"),
    ("NEW WORK", "LIGHTING FIXTURES", "FIXTURES", "L1-EM - 2' X 4' RECESSED TROFFER", 5, "EA"),
    ("NEW WORK", "LIGHTING FIXTURES", "FIXTURES", "L2 - 2' X 2' RECESSED TROFFER", 1, "EA"),
    ("NEW WORK", "LIGHTING FIXTURES", "FIXTURES", 'L3 - 6" RECESSED DOWNLIGHT', 13, "EA"),
    ("NEW WORK", "LIGHTING FIXTURES", "FIXTURES", 'L3-EM - 6" RECESSED DOWNLIGHT', 3, "EA"),
    ("NEW WORK", "LIGHTING FIXTURES", "FIXTURES", "L4 - TASK LED LIGHTING", 3, "EA"),
    ("NEW WORK", "LIGHTING FIXTURES", "FIXTURES", "L5 - UNDERCABINET LED LIGHTING", 43.46, "FT"),
    ("NEW WORK", "LIGHTING FIXTURES", "FIXTURES", "X1 - LED EXIT SIGN", 2, "EA"),
    ("NEW WORK", "LIGHTING CONTROLS", "CONTROLS", "DIMMER SWITCH", 10, "EA"),
    ("NEW WORK", "LIGHTING CONTROLS", "CONTROLS", "DIMMER SWITCH W/ LOCKABLE COVER", 1, "EA"),
    ("NEW WORK", "LIGHTING CONTROLS", "CONTROLS", "DIMMER & OCCUPANCY SENSOR SWITCH", 2, "EA"),
    ("NEW WORK", "LIGHTING CONTROLS", "CONTROLS", "OCCUPANCY SENSOR", 3, "EA"),
    ("NEW WORK", "LIGHTING CONTROLS", "CONTROLS", "OCCUPANCY SENSOR SWITCH", 1, "EA"),
    ("NEW WORK", "SWITCHGEAR", "CIRCUIT BREAKERS", "20A/1P CIRCUIT BREAKER, GFCI", 4, "EA"),
]


def synthetic_reference(*, project_id: str = PROJECT_ID, wage_type: str | None = "prevailing_wage") -> dict[str, Any]:
    lines = [_line(i + 1, *row) for i, row in enumerate(TRUTH_LINES)]
    accessories = [
        _line(101, "NEW WORK", "WIRING", "OTHER CONDUIT ACCESSORIES", '3/4" EMT CONNECTORS', 140, "EA", accessory=True),
        _line(102, "NEW WORK", "WIRING", "OTHER CONDUIT ACCESSORIES", '3/4" EMT COUPLINGS', 698, "EA", accessory=True),
        _line(103, "NEW WORK", "WIRING", "OTHER CONDUIT ACCESSORIES", '3/4" EMT STRAPS', 698, "EA", accessory=True),
    ]
    return {
        "schema": "bdr.eval.reference/1",
        "case_id": "7154",
        "project_id": project_id,
        "quantity_lines": [ln for ln in lines if not ln["description"].startswith("20A/1P")],
        "quantity_lines_adjudicated": lines,
        "accessory_lines": accessories,
        "aliases": {"SHADED": "EM"},
        "conditions": {"wage_type": wage_type, "labor_time": "day_work", "project_type": "other", "owner_type": "public_other"},
        "estimator_recap": {
            "sections": {
                "DEMOLITION": {"total": 300.0, "material": 0.0},
                "WIRING": {"total": 500.0, "material": 100.0},
                "OTHER ITEMS": {"total": 200.0, "material": 50.0},
                "LIGHTING FIXTURES": {"total": 400.0, "material": 250.0},
                "LIGHTING CONTROLS": {"total": 100.0, "material": 40.0},
            },
            "totals": {"bid": 1500.0, "hours": 100.0, "labor_rate": 1.0, "cost": 1200.0},
        },
        "g3_sent": {"total": 1600.0, "sends": 1},
        "g3_labor": {"hours": 110.0, "rate": 2.0, "parsed": True},
        "derived": {"g3_corrected_total": 1650.0},
    }


def synthetic_expected() -> dict[str, Any]:
    """The expected.json keys the checks resolve, with made-up numbers."""
    return {
        "schema": "bdr.eval.expected/1",
        "case_id": "7154",
        "e9": {"lowest_eligible_quote_pretax_usd": 1000.0, "alternate_quote_pretax_usd": 1100.0},
        "e13": {"duplex_unit_material_min_usd": 10.0, "gfci_unit_material_min_usd": 20.0},
    }


# BOQ specs ---------------------------------------------------------------------------


def _item(desc: str, qty: float, unit: str, **extra: Any) -> dict[str, Any]:
    it = {"description": desc, "quantity": qty, "unit": unit, "is_accessory": False, "wastage_applies": False,
          "sheet_refs": [], "caveat": None, "material_note": None, "material_category": "General Material"}
    it.update(extra)
    return it


def _spec(title: str, demo: list[dict], groups: list[tuple[str, str, list[dict]]]) -> dict[str, Any]:
    by_group: dict[str, list[tuple[str, list[dict]]]] = {}
    for g, sg, items in groups:
        by_group.setdefault(g, []).append((sg, items))
    return {
        "title": title,
        "subtitle": "1 Synthetic Way",
        "areas": [{
            "area": title,
            "notes": [],
            "sections": [
                {"heading": "DEMOLITION", "groups": [{"name": "DEMOLITION", "subgroups": [{"name": "", "items": demo}]}]},
                {"heading": "NEW WORK", "groups": [
                    {"name": g, "subgroups": [{"name": sg, "items": items} for sg, items in subs]} for g, subs in by_group.items()
                ]},
            ],
        }],
        "closing_notes": [],
    }


def bad_boq_spec() -> dict[str, Any]:
    """The weekend failure shape: no lengths, split receptacles, 5 breaker
    rows, L5 in EA, track in EA, a make-safe damper row."""
    demo = [
        _item("REMOVE DUPLEX RECEPTACLE", 10, "EA", caveat="(NEED TO CONFIRM DETAILS)", sheet_refs=["ED2.01"]),
        _item("REMOVE HATCHED RECEPTACLE ASSEMBLY", 9, "EA", sheet_refs=["ED2.01"]),
        _item("REMOVE MULTIPLE-RECEPTACLE ASSEMBLY", 8, "EA", sheet_refs=["ED2.01"]),
        _item("REMOVE FLOOR OUTLET ASSEMBLY", 7, "EA", sheet_refs=["ED2.01"]),
        _item("REMOVE RECESSED DOWNLIGHT", 13, "EA", sheet_refs=["ED3.01"]),
        _item("REMOVE RECTANGULAR LIGHT FIXTURE", 5, "EA", sheet_refs=["ED3.01"], coverage="tile_subtotal"),
        _item("REMOVE LIGHTING TRACK ROW", 3, "EA", sheet_refs=["ED3.01"]),
        _item("REMOVE TRACK-LIGHT HEAD ASSEMBLY", 12, "EA", sheet_refs=["ED3.01"]),
        _item("REMOVE LIGHT SWITCH", 2, "EA", sheet_refs=["ED3.01"]),
        _item("MAKE SAFE DAMPER ELECTRICAL CONNECTION", 1, "EA"),
    ]
    groups = [
        ("SWITCHGEAR", "CIRCUIT BREAKERS", [
            _item("20A/1P GFCI CIRCUIT BREAKER", 4, "EA"),
            _item("20A/1P CIRCUIT BREAKER", 7, "EA", caveat="(NEED TO CONFIRM SCOPE OF WORK)"),
            _item("20A/1P CIRCUIT BREAKER", 6, "EA", caveat="(NEED TO CONFIRM SCOPE OF WORK)"),
            _item("20A/1P CIRCUIT BREAKER", 8, "EA", caveat="(NEED TO CONFIRM SCOPE OF WORK)"),
            _item("20A/1P CIRCUIT BREAKER", 1, "EA", caveat="(NEED TO CONFIRM SCOPE OF WORK)"),
        ]),
        ("OTHER ITEMS", RECEPT, [
            _item("DUPLEX RECEPTACLE", 26, "EA"),
            _item("DUPLEX RECEPTACLE, EMERGENCY", 28, "EA"),
            _item("DUPLEX RECEPTACLE, EMERGENCY, DEDICATED", 3, "EA"),
            _item("DUPLEX RECEPTACLE, GFCI", 10, "EA"),
            _item("DUPLEX RECEPTACLE, GFCI, DEDICATED", 1, "EA"),
            _item("DUPLEX RECEPTACLE, OCCUPANCY CONTROLLED", 4, "EA"),
            _item("QUADPLEX RECEPTACLE", 1, "EA"),
            _item("QUADPLEX RECEPTACLE, OCCUPANCY CONTROLLED", 1, "EA"),
        ]),
        ("OTHER ITEMS", "MISCELLANEOUS", [
            _item("FIRE/SMOKE DAMPER POWER CONNECTION", 6, "EA"),
            _item("VAV POWER CONNECTION", 1, "EA"),
            _item("TELEMETRY / CENTRAK / WAP SURVEY AND REINSTALLATION", 1, "LS"),
        ]),
        ("LIGHTING FIXTURES", "FIXTURES", [
            _item("L1, 2X4 RECESSED LED TROFFER", 16, "EA"),
            _item("L1, 2X4 RECESSED LED TROFFER, LIFE SAFETY", 5, "EA", caveat="(NEED TO CONFIRM DETAILS)"),
            _item("L2, 2X2 RECESSED LED TROFFER", 1, "EA"),
            _item("L3, 6-INCH RECESSED LED DOWNLIGHT", 13, "EA"),
            _item("L3, 6-INCH RECESSED LED DOWNLIGHT, SHADED", 3, "EA", caveat="(NEED TO CONFIRM DETAILS)"),
            _item("L4, WALL-MOUNTED LED TASK LIGHT", 3, "EA"),
            _item("L5, UNDERCABINET LED LIGHTING RUN", 7, "EA", caveat="(NEED TO CONFIRM DETAILS)"),
            _item("X1, LED EDGE-LIT EXIT SIGN", 2, "EA"),
        ]),
        ("LIGHTING CONTROLS", "CONTROLS", [
            _item("CEILING OCCUPANCY SENSOR", 3, "EA"),
            _item("WALL DIMMER", 10, "EA"),
            _item("WALL DIMMER WITH LOCKABLE COVER", 1, "EA"),
            _item("WALL OCCUPANCY-SENSOR DIMMER", 2, "EA"),
            _item("WALL OCCUPANCY-SENSOR SWITCH", 1, "EA"),
        ]),
    ]
    return _spec("Synthetic Pharmacy", demo, groups)


def good_boq_spec() -> dict[str, Any]:
    """A BOQ that satisfies every 3.5 row on the synthetic truth."""
    demo = [
        _item("REMOVAL OF LIGHTING FIXTURE AND CONTROLS BACK TO SOURCE", 32, "EA", sheet_refs=["ED3.01"], basis="counted",
              demo_disposition={"decision": "remove_to_source", "cite": "ED3.01 keynote 2"}),
        _item("REMOVAL OF 2X4 TROFFER LIGHTING FIXTURE BACK TO SOURCE", 17, "EA", sheet_refs=["ED3.01"], basis="counted",
              demo_disposition={"decision": "remove_to_source", "cite": "ED3.01 keynote 2"}),
        _item("REMOVAL OF POWER DEVICES W/ BACKBOX BACK TO SOURCE", 35, "EA", sheet_refs=["ED2.01"], basis="counted",
              demo_disposition={"decision": "remove_to_source", "cite": "ED2.01 keynote 1"}),
        _item("REMOVAL OF LIGHTING TRACK", 50, "FT", sheet_refs=["ED3.01"], basis="measured",
              demo_disposition={"decision": "remove_to_source", "cite": "ED3.01 keynote 2"}),
    ]
    groups = [
        ("WIRING", "CONDUCTORS (AWG)", [
            _item("#12 THHN/THWN Cu", 6718.32, "FT", wastage_applies=True, basis="assumed_route"),
            _item("#10 THHN/THWN Cu", 12294.80, "FT", wastage_applies=True, basis="assumed_route"),
        ]),
        ("WIRING", "CONDUITS", [_item('3/4"C EMT', 6337.71, "FT", wastage_applies=True, basis="assumed_route")]),
        ("SWITCHGEAR", "CIRCUIT BREAKERS", [_item("20A/1P CIRCUIT BREAKER, GFCI", 4, "EA", basis="schedule")]),
        ("OTHER ITEMS", RECEPT, [
            _item("DUPLEX RECEPTACLE, 125V", 54, "EA", basis="counted"),
            _item("DUPLEX RECEPTACLE, 125V, GFCI", 11, "EA", basis="counted"),
            _item("DUPLEX RECEPTACLE, 125V, TR", 3, "EA", basis="counted"),
            _item("DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY", 4, "EA", basis="counted"),
            _item("QUADPLEX RECEPTACLE", 1, "EA", basis="counted"),
            _item("QUADPLEX RECEPTACLE CONTROLLED BY OCCUPANCY", 1, "EA", basis="counted"),
        ]),
        ("OTHER ITEMS", "MISCELLANEOUS", [
            _item("ELECTRICAL CONNECTION", 7, "EA", basis="counted"),
            _item("#18/4C CONTROL CABLE", 128.18, "FT", wastage_applies=True, basis="policy"),
            _item("RELOCATION OF THERMOSTAT TO NEW LOCATION", 5, "EA", basis="counted"),
        ]),
        ("LIGHTING FIXTURES", "FIXTURES", [
            _item("L1 - 2' X 4' RECESSED TROFFER", 16, "EA", basis="counted"),
            _item("L1-EM - 2' X 4' RECESSED TROFFER", 5, "EA", basis="counted"),
            _item("L2 - 2' X 2' RECESSED TROFFER", 1, "EA", basis="counted"),
            _item('L3 - 6" RECESSED DOWNLIGHT', 13, "EA", basis="counted"),
            _item('L3-EM - 6" RECESSED DOWNLIGHT', 3, "EA", basis="counted"),
            _item("L4 - TASK LED LIGHTING", 3, "EA", basis="counted"),
            _item("L5 - UNDERCABINET LED LIGHTING", 44, "FT", basis="measured"),
            _item("X1 - LED EXIT SIGN", 2, "EA", basis="counted"),
        ]),
        ("LIGHTING CONTROLS", "CONTROLS", [
            _item("DIMMER SWITCH", 10, "EA", basis="counted"),
            _item("DIMMER SWITCH W/ LOCKABLE COVER", 1, "EA", basis="counted"),
            _item("DIMMER & OCCUPANCY SENSOR SWITCH", 2, "EA", basis="counted"),
            _item("OCCUPANCY SENSOR", 3, "EA", basis="counted"),
            _item("OCCUPANCY SENSOR SWITCH", 1, "EA", basis="counted"),
        ]),
    ]
    return _spec("Synthetic Pharmacy", demo, groups)


# Priced estimate and friends -----------------------------------------------------------------


VERIFY_KEYS = ("labor_amount", "materials_amount", "gear_amount", "underground_amount", "low_voltage_amount",
               "labor_markup_amount", "materials_markup_amount", "gear_markup_amount", "underground_markup_amount",
               "low_voltage_markup_amount")


def good_priced_estimate(expected: dict[str, Any] | None = None) -> dict[str, Any]:
    exp = expected or synthetic_expected()
    lines = []
    for i, row in enumerate(TRUTH_LINES):
        section, group, subgroup, desc, qty, unit = row
        if section == "DEMOLITION":
            continue
        mat = 1.0
        extra: dict[str, Any] = {}
        if "DUPLEX" in desc and "GFCI" in desc:
            mat = exp["e13"]["gfci_unit_material_min_usd"] + 1
            extra["spec_fact_ref"] = "F3"
        elif "DUPLEX" in desc:
            mat = exp["e13"]["duplex_unit_material_min_usd"] + 1
            extra["spec_fact_ref"] = "F3"
        lines.append({
            "line_id": f"B{i + 1}", "section": section, "group": group, "subgroup": subgroup, "description": desc,
            "kind": "count" if unit == "EA" else "length", "unit": unit, "qty_base": qty, "wastage": 0.0, "qty": qty,
            "quantity_basis": "counted", "unit_hours": 0.5, "unit_material_usd": mat, "hours_source": "library",
            "material_source": "vendor_quote" if group == "LIGHTING FIXTURES" else "library", "n": 5, "p25": 0.4, "p75": 0.6,
            "quote_ref": "Q1" if group == "LIGHTING FIXTURES" else None, "priced": True, "excluded": False,
            "exclusion_reason": None, **extra,
        })
    verify = {k: None for k in VERIFY_KEYS}
    verify.update({"labor_amount": 1250.0, "materials_amount": 300.0, "materials_markup_amount": 30.0, "low_voltage_amount": 0.0})
    return {
        "schema": "bdr.priced_estimate/1",
        "refused": False,
        "refusal_reasons": [],
        "wage_type": "prevailing_wage",
        "labor_rate_usd": 125.0,
        "labor_rate_basis": "rate table",
        "lines": lines,
        "allowances": [{"name": "PERMIT AND UTILITY FEES", "amount_usd": 0.0, "included": True}],
        "quotes": [{"category": "Lighting", "vendor": "Vendor A", "amount_usd": exp["e9"]["lowest_eligible_quote_pretax_usd"],
                    "tax_included": False, "replaces_sections": ["LIGHTING FIXTURES"], "file_sha256": "c" * 64,
                    "reason": "lowest eligible", "reconciled": {"L1": 21, "L3": 16, "L5_ft": 50, "collapse_warning": True}}],
        "sections": {
            "LIGHTING FIXTURES": {"hours": 20.0, "labor_usd": 500.0, "material_usd": 200.0, "material_source": "vendor_quote"},
            "OTHER ITEMS": {"hours": 10.0, "labor_usd": 250.0, "material_usd": 40.0, "material_source": "library"},
            "WIRING": {"hours": 15.0, "labor_usd": 400.0, "material_usd": 50.0, "material_source": "library"},
            "LIGHTING CONTROLS": {"hours": 4.0, "labor_usd": 100.0, "material_usd": 10.0, "material_source": "library"},
        },
        "totals": {"hours_raw": 95.0, "hours": 100.0, "hour_roundup": 1.05, "labor_usd": 1250.0, "material_pretax_usd": 277.0,
                   "tax_rate_pct": 8.375, "material_usd": 300.0, "verify": verify, "bid_total_usd": 1580.0},
        "exclusions": [
            "Fire alarm and access control by others (Honeywell) per Exhibit A page 3",
            "Telemetry, Centrak and WAP survey and reinstallation by owner per Exhibit A p. 4",
            "Furniture connections by furniture vendor per Exhibit A p. 4",
            "Door hardware by others per Exhibit A p. 5",
            "Paint and patching by GC per Exhibit A p. 5",
            "Temporary power and lighting by GC per Exhibit A p. 2",
            "Commissioning and coordination study by others per E0.00 general notes",
        ],
        "clarifications": ["Branch raceway 3/4 in EMT with set-screw fittings per E2.01 note 8"],
        "rfis": [],
        "metadata": {"excluded_project_ids_sha256": excluded_project_ids_sha256(EXCLUDED_IDS),
                     "excluded_project_ids": list(EXCLUDED_IDS), "library_fingerprint": "d" * 64},
    }


def good_gate_report() -> dict[str, Any]:
    return {"schema": "bdr.gate_report/1", "blocking": False, "checks": [{"code": "branch_ratio", "severity": "warn", "detail": "ratio 2.9", "rows": []}]}


def good_documents() -> list[dict[str, Any]]:
    return [
        {"id": ELEC_DOC, "name": "Elec.pdf", "category": "electrical_drawings", "file_id": ELEC_FILE, "page_count": 3,
         "page_start": 1, "page_end": 3, "segment_id": None, "facts_status": "done",
         "page_meta": [
             {"page": 1, "pdf_page": 1, "w_in": 42.0, "h_in": 30.0, "sheet_number": "E0.00", "title": "COVERSHEET", "page_role": "legend_symbols"},
             {"page": 2, "pdf_page": 2, "w_in": 42.0, "h_in": 30.0, "sheet_number": "E2.01", "title": "POWER PLAN", "page_role": "plan"},
             {"page": 3, "pdf_page": 3, "w_in": 42.0, "h_in": 30.0, "sheet_number": "E3.01", "title": "LIGHTING PLAN", "page_role": "plan"},
         ]},
        {"id": SPEC_DOC, "name": "Specs.pdf", "category": "specifications", "file_id": SPEC_FILE, "page_count": 170,
         "page_start": 1, "page_end": 170, "segment_id": None, "facts_status": "done",
         "page_meta": [{"page": p, "pdf_page": p, "page_role": "spec_text"} for p in range(1, 171)]},
    ]


def bad_documents() -> list[dict[str, Any]]:
    docs = good_documents()
    for m in docs[0]["page_meta"]:
        m["sheet_number"] = None
        m["title"] = None
        m["page_role"] = "other"
    docs[0].pop("facts_status")
    docs.insert(1, {"id": "d0c00003-0000-4000-8000-000000000003", "name": "MECH_RESPONSE D1.pdf", "category": "electrical_drawings",
                    "file_id": "f1le0003-0000-4000-8000-000000000003", "page_count": 2, "page_start": 1, "page_end": 2,
                    "page_meta": [{"page": 1, "sheet_number": "M0.00", "title": "MECH", "page_role": "other"},
                                  {"page": 2, "sheet_number": "M2.01", "title": "MECH PLAN", "page_role": "other"}]})
    return docs


def good_ledger() -> dict[str, Any]:
    return {
        "project": {"name": "Synthetic Pharmacy", "number": "26.8.0001", "address": "1 Synthetic Way"},
        "legend_pages": [{"document_id": ELEC_DOC, "page": 1, "sheet_number": "E0.00"}],
        "legend": [{"tag": f"T{i}", "description": f"entry {i}"} for i in range(60)],
        "sheet_index": [{"document_id": ELEC_DOC, "page": p, "sheet_number": s, "page_role": r}
                        for p, s, r in ((1, "E0.00", "legend_symbols"), (2, "E2.01", "plan"), (3, "E3.01", "plan"))],
        "spec_toc": [],
    }


def bad_ledger() -> dict[str, Any]:
    led = good_ledger()
    led["legend_pages"] = []
    led["legend"] = []
    for s in led["sheet_index"]:
        s["page_role"] = "other"
        s["sheet_number"] = None
    return led


def good_category_output() -> dict[str, Any]:
    return {
        "category": "electrical_drawings",
        "counts": [{"page": 2, "description": "DUPLEX RECEPTACLE", "quantity": 54, "unit": "EA", "document_id": ELEC_DOC}],
        "runs": [], "schedules": [{"name": "LUMINAIRE SCHEDULE", "page": 1, "rows": [["L4", "TASK LED", "LOCAL", "120"]]}],
        "specs": [], "scope": [], "details": [], "questions": [], "pages": [{"page": 1, "tags": ["legend"]}], "summary": "ok",
    }


def bad_category_output() -> dict[str, Any]:
    cat = good_category_output()
    cat["schedules"][0]["rows"][0][2] = "[unreadable]"
    cat["questions"] = [{"question": "Confirm exact L4 control method.", "page": 1, "why": "unreadable cell"}]
    cat["summary"] = "L4 control remains unreadable."
    return cat


def good_main(boq: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "boq": boq or good_boq_spec(),
        "brief": {"rfis": [], "risks": [], "systems": [], "exclusions": ["Temporary power by GC"], "assumptions": [],
                  "project_summary": "Synthetic remodel.", "sheet_summaries": [], "spec_highlights": [], "missing_documents": [],
                  "electrical_scope_summary": "Lighting and power.", "other_trades_affecting_electrical": []},
        "markups": {"sheets": []},
        "estimate": {"address": "1 Synthetic Way", "project_name": "Synthetic Pharmacy", "general_notes": [], "area_notes": [],
                     "extra_allowances": [], "include_standard_allowances": True},
        "marked_plans": {"sheets": [{"area": "Synthetic Pharmacy", "page": 3, "document_id": ELEC_DOC, "sheet_number": "E3.01",
                                     "doc_index": 1,
                                     "legend": [{"prefix": "L1", "description": "L1 troffer", "quantity": 2, "unit": "EA"},
                                                {"prefix": "L5", "description": "L5 undercabinet run", "quantity": 44, "unit": "FT"}],
                                     "marks": [{"kind": "dot", "points": [[0.2, 0.2]], "legend_index": 0},
                                               {"kind": "dot", "points": [[0.4, 0.4]], "legend_index": 0},
                                               {"kind": "polyline", "points": [[0.6, 0.6], [0.7, 0.6]], "legend_index": 1}]}]},
        "material_notes": [],
        "confidence_notes": [],
        "scope_policy": {"decisions": [{"key": "demo_disposition", "rule_id": "demo-1", "action": "remove_to_source",
                                        "evidence": ["ED2.01 keynote 1"]}],
                         "clarifications": ["Branch raceway 3/4 in EMT"], "exclusions": [], "proposal_lines": []},
    }


def bad_main() -> dict[str, Any]:
    main = good_main(bad_boq_spec())
    main.pop("scope_policy")
    main["brief"]["rfis"] = [
        {"question": "Confirm whether owner requirements such as prevailing wage apply.", "source": "Exhibit A"},
        {"question": "Resolve above-ceiling rigid versus EMT permission.", "source": "E0.01"},
        {"question": "Confirm whether lighting conduit is maintained and capped or removed to source.", "source": "ED3.01"},
        {"question": "Confirm exact L4 control method.", "source": "E0.04"},
    ]
    main["brief"]["assumptions"] = ["Branch wiring cannot be defensibly measured; no physical page calibration was supplied."]
    main["estimate"]["general_notes"] = ["Do not use per-device averages. No invented assumed routes have been output."]
    main["estimate"]["extra_allowances"] = [
        "TEMPORARY DISTRIBUTION AND LOCALLY SWITCHED LIGHTING", "TEMPORARY HEPA POWER", "HONEYWELL DESIGN-BUILD FA AND ACCESS INTERFACE SCOPE",
        "TELEMETRY / CENTRAK / WAP SURVEY AND REINSTALLATION", "LIGHTING CONTROL ANCILLARY EQUIPMENT", "ELECTRICAL CUTTING AND PATCHING",
        "FIRESTOP SYSTEM LABELING", "BIM COORDINATION", "OWNER TRAINING", "OCCUPIED-HOSPITAL PHASING",
    ]
    main["marked_plans"]["sheets"][0]["marks"] = [
        {"kind": "dot", "points": [[0.6, 0.6]], "legend_index": 0},
        {"kind": "dot", "points": [[0.6, 0.6]], "legend_index": 0},
        {"kind": "dot", "points": [[0.7, 0.7]], "legend_index": 0},
        {"kind": "dot", "points": [[0.7, 0.7]], "legend_index": 0},
    ]
    return main


def good_rendered_rows() -> list[dict[str, Any]]:
    base = [{"area": "SHEET", "section": "NEW WORK", "group": "WIRING", "subgroup": "CONDUITS", "description": '3/4"C EMT',
             "quantity": 6971.48, "unit": "FT", "is_accessory": False}]
    for name, qty in (("CONNECTORS", 140), ("COUPLINGS", 698), ("STRAPS", 698)):
        base.append({"area": "SHEET", "section": "NEW WORK", "group": "WIRING", "subgroup": "OTHER CONDUIT ACCESSORIES",
                     "description": f'3/4" EMT {name}', "quantity": qty, "unit": "EA", "is_accessory": True})
    return base


def good_groups() -> list[dict[str, Any]]:
    return [
        {"id": "g1", "kind": "sub", "category": "electrical_drawings", "group_index": 0, "pass": 1, "status": "done",
         "pages": [{"document_id": ELEC_DOC, "page": p, "kind": "image"} for p in (1, 2, 3)], "est_input_tokens": 200000},
        {"id": "g2", "kind": "sub", "category": "specifications", "group_index": 0, "pass": 1, "status": "done",
         "pages": [{"document_id": SPEC_DOC, "page": p, "kind": "file"} for p in range(1, 61)], "est_input_tokens": 60000},
        {"id": "g3", "kind": "main", "category": None, "group_index": 0, "pass": 1, "status": "done", "pages": [], "est_input_tokens": 80000},
    ]


def bad_groups() -> list[dict[str, Any]]:
    groups = good_groups()
    groups[1]["pages"] = [{"document_id": SPEC_DOC, "page": p, "kind": "file"} for p in range(1, 171)]
    return groups


def good_planned_requests() -> list[dict[str, Any]]:
    return [
        {"kind": "sub", "category": "electrical_drawings", "group_index": 0, "has_legend_text": True, "pages_without_facts_block": 0, "est_input_tokens": 200000},
        {"kind": "sub", "category": "specifications", "group_index": 0, "has_legend_text": False, "pages_without_facts_block": 0, "est_input_tokens": 60000},
    ]


def good_artifacts(expected: dict[str, Any] | None = None) -> dict[str, Any]:
    main = good_main()
    return {
        "documents": good_documents(),
        "files": [],
        "ledger": good_ledger(),
        "groups": good_groups(),
        "calls": [],
        "chat": [],
        "category:electrical_drawings": good_category_output(),
        "main": main,
        "boq_spec": main["boq"],
        "estimate_spec": {"boq": main["boq"], "estimate": main["estimate"]},
        "brief_spec": main["brief"],
        "marked_plans_spec": main["marked_plans"],
        "scope_report": main["scope_policy"],
        "priced_estimate": good_priced_estimate(expected),
        "gate_report": good_gate_report(),
        "census": {"max_mismatch": 0.04},
        "rendered": {"files": {}, "boq_rows": good_rendered_rows()},
        "planned_requests": good_planned_requests(),
        "chat_probe": {"question": "How many labor hours?", "answer": "About 104 labor hours in total."},
    }


def bad_artifacts() -> dict[str, Any]:
    main = bad_main()
    return {
        "documents": bad_documents(),
        "files": [],
        "ledger": bad_ledger(),
        "groups": bad_groups(),
        "calls": [],
        "chat": [],
        "category:electrical_drawings": bad_category_output(),
        "main": main,
        "boq_spec": main["boq"],
        "estimate_spec": {"boq": main["boq"], "estimate": main["estimate"]},
        "brief_spec": main["brief"],
        "marked_plans_spec": main["marked_plans"],
        "rendered": {"files": {}, "boq_rows": [{"area": "SHEET", "section": "NEW WORK", "group": "OTHER ITEMS", "subgroup": RECEPT,
                                                "description": "DUPLEX RECEPTACLE", "quantity": 26, "unit": "EA", "is_accessory": False}]},
        "chat_probe": {"question": "How many labor hours?", "answer": "A defensible labor-hour estimate is not available from this run."},
    }


def build_inputs(artifacts: dict[str, Any], *, reference: dict[str, Any] | None = None, expected: dict[str, Any] | None = None,
                 case_id: str = "7154", docs: Any = None, options: dict[str, Any] | None = None,
                 sent_proposal_text: str | None = None) -> AcceptanceInputs:
    ref = reference if reference is not None else synthetic_reference()
    exp = expected if expected is not None else synthetic_expected()
    spec = artifacts.get("boq_spec")
    score = score_raw = None
    if isinstance(spec, dict):
        rows = score_mod.rows_from_spec(spec)
        priced = artifacts.get("priced_estimate") if isinstance(artifacts.get("priced_estimate"), dict) else None
        score = score_mod.score_case(rows, ref, adjudicated=True, priced=priced, case_id=case_id)
        score_raw = score_mod.score_case(rows, ref, adjudicated=False, priced=priced, case_id=case_id)
    return AcceptanceInputs(artifacts=copy.deepcopy(artifacts), case_id=case_id, reference=ref, expected=exp, score=score,
                            score_raw=score_raw, docs=docs, options=dict(options or {"demo_two_line_style": False}),
                            sent_proposal_text=sent_proposal_text)


# A bundle on disk --------------------------------------------------------------------------------


def make_bundle(root: Path, *, good: bool = True, with_chat: bool = True, artifacts: bool = False) -> Path:
    """Write a synthetic bundle in the weekend layout under `root`."""
    main = good_main() if good else bad_main()
    run = {
        "id": RUN_ID, "name": "SYNTH 0001", "status": "done", "model": "fake-model", "project_id": PROJECT_ID,
        "source": "project", "ledger": good_ledger() if good else bad_ledger(),
        "settings_snapshot": {"openai_plans_model": "fake-model", "code_version": "synthetic", "prompt_hash": "p" * 64},
        "input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.0, "estimated_input_tokens": 1200, "groups_total": 3,
        "created_at": "2026-10-21T10:00:00+00:00",
    }
    files = [
        {"id": ELEC_FILE, "run_id": RUN_ID, "filename": "Elec.pdf", "label": "electrical_drawings", "page_count": 3,
         "status": "labeled", "source_project_file_id": ELEC_PF, "split_file_id": None, "size_bytes": 10,
         "storage_path": f"plans-reader/{RUN_ID}/source/elec.pdf", "mime_type": "application/pdf", "created_at": "2026-10-21T10:00:01+00:00"},
        {"id": SPEC_FILE, "run_id": RUN_ID, "filename": "Specs.pdf", "label": "specifications", "page_count": 4,
         "status": "labeled", "source_project_file_id": SPEC_PF, "split_file_id": None, "size_bytes": 10,
         "storage_path": f"plans-reader/{RUN_ID}/source/specs.pdf", "mime_type": "application/pdf", "created_at": "2026-10-21T10:00:02+00:00"},
    ]
    documents = good_documents() if good else bad_documents()
    documents = [d for d in documents if d["file_id"] in (ELEC_FILE, SPEC_FILE)]
    documents[1]["page_count"] = 4
    documents[1]["page_end"] = 4
    documents[1]["page_meta"] = documents[1]["page_meta"][:4]
    for d in documents:
        d["run_id"] = RUN_ID
        d["created_at"] = "2026-10-21T10:01:00+00:00"
    groups = good_groups() if good else bad_groups()
    for g in groups:
        g["run_id"] = RUN_ID
    cat = good_category_output() if good else bad_category_output()
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("groups", "outputs", "deliverables"):
        (root / sub).mkdir(exist_ok=True)
    (root / "run.json").write_text(json.dumps(run, indent=1))
    (root / "files.json").write_text(json.dumps(files, indent=1))
    (root / "documents.json").write_text(json.dumps(documents, indent=1))
    (root / "groups.json").write_text(json.dumps(groups, indent=1))
    (root / "groups" / "sub_electrical_drawings_0_p1.json").write_text(json.dumps(cat))
    (root / "groups" / "sub_specifications_0_p1.json").write_text(json.dumps({"category": "specifications", "specs": [], "questions": []}))
    (root / "outputs" / "category_electrical_drawings_v1.json").write_text(json.dumps(cat))
    (root / "outputs" / "category_specifications_v1.json").write_text(json.dumps({"category": "specifications", "specs": [], "questions": []}))
    (root / "outputs" / "main_none_v1.json").write_text(json.dumps(main))
    deliverables = []
    for i, (kind, spec) in enumerate((("boq", main["boq"]), ("brief", main["brief"]),
                                      ("estimate", {"boq": main["boq"], "estimate": main["estimate"]}),
                                      ("marked_plans", main["marked_plans"]))):
        deliverables.append({"id": f"d{i}", "run_id": RUN_ID, "kind": kind, "version": 1, "filename": f"{kind}.bin",
                             "spec_len": len(json.dumps(spec)), "storage_path": f"plans-reader/{RUN_ID}/deliverables/v1/{kind}.bin"})
        (root / "deliverables" / f"{kind}_v1.json").write_text(json.dumps(spec))
    (root / "deliverables" / "estimate_v2.json").write_text(json.dumps({"boq": main["boq"], "estimate": main["estimate"]}))
    deliverables.append({**deliverables[2], "id": "d9", "version": 2})
    (root / "deliverables.json").write_text(json.dumps(deliverables, indent=1))
    (root / "calls.json").write_text(json.dumps([{"id": "c1", "run_id": RUN_ID, "kind": "sub", "model": "fake-model", "cost_usd": 0.0,
                                                   "input_tokens": 10, "output_tokens": 5, "ok": True,
                                                   "request_snapshot": {"parts": [{"type": "input_text", "head": "RUN LEDGER"}]}}]))
    chat = []
    if with_chat:
        chat = [{"id": "t1", "run_id": RUN_ID, "title": "How many electrical labor hours should be estimated for this project?",
                 "messages": [{"role": "user", "content": "How many electrical labor hours should be estimated for this project?"},
                              {"role": "assistant", "content": "About 104 labor hours in total." if good else "A defensible labor-hour estimate is not available."}]}]
    (root / "chat.json").write_text(json.dumps(chat))
    if artifacts:
        (root / "artifacts").mkdir(exist_ok=True)
        (root / "artifacts" / "priced_estimate.json").write_text(json.dumps(good_priced_estimate()))
        (root / "artifacts" / "gate_report.json").write_text(json.dumps(good_gate_report()))
    return root
