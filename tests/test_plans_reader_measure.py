"""plans_reader_measure (WS3 B5 to B10): build_measure on synthetic facts
(tiers, linear items, control cable, findings), apply_measure_to_boq
(replaces only covered rows, one finding per deleted model row), the
renderer accessory chain for 6,337.71 FT of EMT, the MeasureService and its
six registered tools (fake services, scripted stores), the versioned
writer with overrides where a human beats a model, the DB store with a
fake client, the replay stage registration, and the golden 7154 acceptance
on the backup PDF with zero model calls.

No network, no database, no model.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import openpyxl
from collections import Counter

import pytest

from app.services import plans_reader_geometry as G
from app.services import plans_reader_measure as M
from app.services import plans_reader_replay as replay
from app.services import plans_reader_routes as R
from app.services import plans_reader_tools as T
from app.services.electrical_items import parse_item
from app.services.pagefacts.contracts import DocFacts
from app.services.plans_reader_render import boq_quantity, conduit_fittings, other_conduit_accessories, render_boq_xlsx
from app.services.plans_reader_schemas import validate_strict

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/plans_reader/run_7154/page_facts_elec.json"
BOQ_V1 = ROOT / "tests/fixtures/plans_reader/run_7154/deliverables/boq_v1.json"
CATEGORY_V1 = ROOT / "tests/fixtures/plans_reader/run_7154/outputs/category_electrical_drawings_v1.json"
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
ELEC_PDF = (
    BACKUP
    / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011"
    / "drawing/c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
DOC_ID = "538c95b5-3df0-4d7e-b18b-c01135083d20"
CONV = R.Conventions()
golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(not ELEC_PDF.exists(), reason="7154 backup PDF not on this machine")


# Synthetic facts ------------------------------------------------------------------------------


def _page(page: int, number: str, title: str, ft: float | None, *, tags=None, markers=None, notes=None, roles=("plan",)) -> dict:
    return {
        "page": page,
        "w_in": 42.0,
        "h_in": 30.0,
        "rotation": 0,
        "text_class": "cad_text",
        "sheet_number": number,
        "sheet_title": title,
        "roles": list(roles),
        "page_role": roles[0],
        "views": [{"view_id": "v1", "title": title, "number": "1", "scale_text": ('1/4" = 1\'-0"' if ft == 4.0 else '1/16" = 1\'-0"' if ft == 16.0 else "N.T.S."), "ft_per_in": ft, "kind": "plan", "anchor": [0.3, 0.95, 0.5, 0.97], "region": [0.0, 0.0, 0.9, 1.0], "example": False, "nts": ft is None}],
        "primary_ft_per_in": ft,
        "regions": [{"kind": "title_block", "bbox": [0.9, 0.0, 1.0, 1.0], "label": None}, {"kind": "plan_area", "bbox": [0.0, 0.0, 0.9, 1.0], "label": None}],
        "notes": notes or [],
        "keynote_markers": markers or [],
        "tags": tags or {"circuit": [], "status": {}, "type_marks": {}, "panel_labels": []},
    }


def _tag(panel: str, ckt: int, x: float, y: float, fixture: str | None = None) -> dict:
    return {"panel": panel, "circuits": [ckt], "mods": [fixture] if fixture else [], "fixture_type": fixture, "raw": f"{fixture + '/' if fixture else ''}{panel}-{ckt}", "x": x, "y": y, "rotation": 0}


def _schedule(name: str, volts: str, circuits: list[dict]) -> dict:
    return {"kind": "panel", "schedule_id": "s1", "page": 2, "panel": {"name": name, "existing": True, "volts": volts, "phase": 3, "circuits": circuits}}


def _circuit(ckt: int, desc: str, *, status="unknown", wire="10", load=0.72) -> dict:
    return {"ckt": ckt, "ckts": [ckt], "poles": 1, "trip_a": 20, "desc": desc, "status": status, "wire": {"hot": wire, "neutral": wire, "ground": wire}, "load_va": {"A": load}, "keynotes": []}


def _doc(pages: list[dict], schedules: list[dict], *, panel_locations=None, fixture_types=None, legend=None) -> DocFacts:
    return DocFacts.from_dict(
        {
            "sha256": "a" * 64,
            "kind": "drawing",
            "page_count": len(pages),
            "name": "synthetic.pdf",
            "label": "electrical_drawings",
            "schedules": schedules,
            "panels": [{"name": s["panel"]["name"], "schedule_id": s["schedule_id"], "existing": True, "volts": s["panel"]["volts"], "phase": 3} for s in schedules],
            "panel_locations": panel_locations or [],
            "fixture_types": fixture_types or [],
            "legend": legend or {"entries": [], "modifiers": [], "abbreviations": []},
            "pages": pages,
        }
    )


def _single_sheet_inputs(**kw) -> M.MeasureInputs:
    tags = {"circuit": [_tag("LP1", 1, 0.20, 0.50), _tag("LP1", 1, 0.25, 0.50), _tag("LP1", 3, 0.30, 0.60)], "status": {}, "type_marks": {}, "panel_labels": []}
    page = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0, tags=tags)
    doc = _doc([page], [_schedule("LP1", "120/208", [_circuit(1, "RECEPTS"), _circuit(3, "RECEPTS")])], panel_locations=[{"panel": "LP1", "page": 1, "x": 0.8, "y": 0.8, "existing": True, "source": "plan_label"}])
    base = dict(run_id="run-synth", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)])
    base.update(kw)
    return M.MeasureInputs(**base)


# build_measure ---------------------------------------------------------------------------------


def test_build_measure_single_sheet_routes_and_rows():
    m = M.build_measure(_single_sheet_inputs(), CONV)
    assert m["version"] == 1 and m["stats"]["routes"] == 2 and m["stats"]["tags"] == 3
    rows = {q["description"]: q for q in m["quantities"]}
    assert set(rows) == {"#12 THHN/THWN Cu", "#10 THHN/THWN Cu", '3/4"C EMT'}
    assert rows['3/4"C EMT']["quantity"] > 0 and rows['3/4"C EMT']["basis"] == "assumed_route"
    assert m["routes"][0]["segments"][1]["kind"] == "homerun" and m["routes"][0]["segments"][1]["sheet_key"] == "E2.01"
    assert m["panels"][0]["location"]["sheet_key"] == "E2.01"
    assert m["registrations"] == [] and m["findings"] == [] and m["overrides"] == []
    assert all(0 <= v <= 1 for s in m["routes"][0]["segments"] for pl in s["polylines"] for p in pl for v in p)
    text = M.measure_summary_text(m)
    assert "M1:" in text and "never write them yourself" in text and "$" not in text


def test_build_measure_control_cable_only_when_included():
    off = M.build_measure(_single_sheet_inputs(scope=M.ScopeInputs(thermostats_included=False, control_cable_devices=5)), CONV)
    assert not [q for q in off["quantities"] if "CONTROL CABLE" in q["description"]]
    on = M.build_measure(_single_sheet_inputs(scope=M.ScopeInputs(thermostats_included=True, control_cable_devices=5)), CONV)
    cc = next(q for q in on["quantities"] if q["description"] == "#18/4C CONTROL CABLE")
    assert cc["quantity"] == 125.0 and cc["basis"] == "assumed_allowance" and cc["caveat"] == "(ASSUMED)" and cc["wastage_applies"]
    assert cc["line_hint"]["kind"] == "control_cable" and cc["group"] == "OTHER ITEMS"
    scope = M.ScopeInputs.from_scope_context({"standards": {"raceway": "3/4 EMT"}, "inclusions": [{"trigger_family": "thermostat", "count": 5, "line_template": "RELOCATION OF THERMOSTAT"}]})
    assert scope.thermostats_included and scope.control_cable_devices == 5 and scope.wiring_method == "emt"
    assert M.ScopeInputs.from_scope_context({"standards": {"wiring_method": "mc"}}).wiring_method == "mc"


def test_build_measure_density_tier_when_nothing_is_positioned():
    page = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0)
    doc = _doc([page], [])
    counts = {"electrical_drawings": {"counts": [{"description": "DUPLEX RECEPTACLE, 125V", "quantity": 30, "unit": "EA", "sheet_number": "E2.01"}]}}
    m = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs=counts, project_type="ti"), CONV)
    assert m["stats"]["new_devices"] == 30 and m["routes"][0]["tier"] == "density"
    assert [f["code"] for f in m["findings"] if f["code"] == "branch_density_only"] == ["branch_density_only"]
    conduit = next(q for q in m["quantities"] if q["subgroup"] == "CONDUITS")
    assert conduit["quantity"] > 0 and conduit["basis"] == "assumed_density"
    gate = M.branch_coverage_findings({"areas": []}, m)
    assert any(g["gate_id"] == "branch_ratio" and g["code"] == "branch_density_only" for g in gate)


def test_build_measure_untagged_sheet_uses_count_positions_and_equipment_allowances():
    page = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0)
    doc = _doc([page], [_schedule("LP1", "120/208", [_circuit(1, "RECEPTS")])], panel_locations=[{"panel": "LP1", "page": 1, "x": 0.8, "y": 0.8, "source": "plan_label"}])
    counts = {
        "electrical_drawings": {
            "counts": [
                {"description": "DUPLEX RECEPTACLE, 125V", "quantity": 3, "unit": "EA", "sheet_number": "E2.01", "positions": [[0.2, 0.5], [0.25, 0.5], [0.3, 0.5]]},
                {"description": "ELECTRICAL CONNECTION TO NEW VAV", "quantity": 2, "unit": "EA", "sheet_number": "E2.01"},
            ]
        }
    }
    m = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs=counts), CONV)
    tiers = sorted(r["tier"] for r in m["routes"])
    assert tiers == ["equipment", "equipment", "untagged"]
    assert next(r for r in m["routes"] if r["tier"] == "untagged")["basis"] == "assumed_route_untagged"
    assert m["totals"]["by_tier"] == {"equipment": 2, "untagged": 1}


def test_build_measure_tie_in_and_nts_sheet():
    tags = {"circuit": [_tag("LP1", 1, 0.2, 0.5)], "status": {}, "type_marks": {}, "panel_labels": []}
    markers = [{"number": "3", "x": 0.205, "y": 0.505, "confidence": 0.6}]
    notes = [{"kind": "keynotes", "items": [{"number": "3", "text": "INTERCEPT AND EXTEND EXISTING CIRCUIT", "bbox": [0.9, 0.1, 0.95, 0.12]}]}]
    page = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0, tags=tags, markers=markers, notes=notes)
    nts = _page(2, "E0.02", "SINGLE LINE DIAGRAM", None, roles=("diagram",))
    doc = _doc([page, nts], [_schedule("LP1", "120/208", [_circuit(1, "EXISTING LOAD", status="existing")])])
    m = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)]), CONV)
    assert m["routes"][0]["tier"] == "tie_in" and m["routes"][0]["total_ft"] == 25.0
    assert [s["sheet_key"] for s in m["scales"]] == ["E2.01"]  # the diagram is not a plan sheet


def test_build_measure_untagged_sheets_keep_nts_pages_out():
    nts_plan = _page(1, "E2.01", "POWER PLAN - NEW WORK", None, tags={"circuit": [_tag("LP1", 1, 0.2, 0.5)], "status": {}, "type_marks": {}, "panel_labels": []})
    doc = _doc([nts_plan], [_schedule("LP1", "120/208", [_circuit(1, "RECEPTS")])])
    m = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)]), CONV)
    assert m["scales"][0]["status"] == "nts"
    assert m["routes"][0]["basis"] == "assumed_allowance" and any(f.startswith("scale_unknown") for f in m["routes"][0]["flags"])


# Linear items ----------------------------------------------------------------------------------


def _rect(x0: float, y0: float, x1: float, y1: float) -> list[G.Segment]:
    return [G.Segment(x0, y0, x1, y0), G.Segment(x0, y1, x1, y1), G.Segment(x0, y0, x0, y1), G.Segment(x1, y0, x1, y1)]


def test_rect_candidates_and_linear_tag_measurement():
    W, H = 42.0, 30.0
    # A 3 in x 0.1 in undercabinet outline, a tag text 0.3 in above it, and the tag's own 0.5 x 0.12 in frame.
    fixture = _rect(10 / W, 10 / H, 13 / W, 10.1 / H)
    tag_frame = _rect(10.5 / W, 9.55 / H, 11.0 / W, 9.67 / H)
    tag = (10.75 / W, 9.61 / H)
    cands = M.rect_candidates(fixture + tag_frame, w_in=W, h_in=H)
    assert len(cands) == 2 and {round(c.long_in, 2) for c in cands} == {3.0, 0.5}
    res = M.measure_linear_tag([tag], fixture + tag_frame, w_in=W, h_in=H, ft_per_in=4.0)
    assert len(res["instances"]) == 1 and res["instances"][0]["length_ft"] == 12.0 and res["total_ft"] == 12.0
    assert res["unmatched"] == [] and res["method"] == "vector_rect"
    # A second tag claiming the same fixture counts once; a far tag is unmatched.
    res2 = M.measure_linear_tag([tag, (12.5 / W, 9.61 / H), (30 / W, 20 / H)], fixture + tag_frame, w_in=W, h_in=H, ft_per_in=4.0)
    assert res2["total_ft"] == 12.0 and len(res2["unmatched"]) == 1 and "unmatched_tags:1" in res2["flags"]


def test_measure_track_prefers_heavy_runs_and_plausible_lengths():
    W, H = 42.0, 30.0
    heavy = []
    # A heavy dash-dot track 4 in long at x = 10 in (dashes 0.06 in, gaps 0.1 in), stroke 10 pt.
    y = 12.0
    while y < 16.0:
        heavy.append(M.WeightedSegment(10 / W, y / H, 10 / W, min(16.0, y + 0.06) / H, 10.0))
        y += 0.16
    # A thin continuous ceiling grid line next to it, stroke 2 pt, 20 in long.
    heavy.append(M.WeightedSegment(10.2 / W, 5 / H, 10.2 / W, 25 / H, 2.0))
    # A heavy but implausibly long horizontal line through the anchor.
    heavy.append(M.WeightedSegment(2 / W, 11.8 / H, 30 / W, 11.8 / H, 10.0))
    runs = M.merge_runs(heavy, w_in=W, h_in=H)
    assert any(abs(r.length_in(W, H) - 4.0) < 0.1 and w == 10.0 for r, w in runs)
    res = M.measure_track([(10.3 / W, 11.9 / H)], heavy, w_in=W, h_in=H, ft_per_in=4.0)
    assert len(res["instances"]) == 1 and res["instances"][0]["length_ft"] == pytest.approx(16.0, abs=0.5)
    assert res["instances"][0]["stroke_pt"] == 10.0 and res["unmatched"] == []
    assert M.measure_track([(30 / W, 29 / H)], heavy, w_in=W, h_in=H, ft_per_in=4.0)["instances"] == []


def test_linear_demolition_without_anchor_blocks_and_routes_to_human():
    demo = _page(1, "ED3.01", "LIGHTING PLAN - DEMOLITION WORK", 4.0)
    doc = _doc([demo], [])
    counts = {"electrical_drawings": {"counts": [{"row_id": "C7", "description": "Remove existing lighting track row", "quantity": 3, "unit": "EA", "sheet_number": "ED3.01", "symbol_or_tag": "(X), keynote 1", "location": "at (0.47,0.47)"}]}}
    m = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs=counts), CONV)
    item = m["linear_items"][0]
    assert item["kind"] == "linear_demolition" and item["total_ft"] is None and "no_anchor" in item["flags"]
    block = [f for f in m["findings"] if f["code"] == "linear_in_ea"]
    assert len(block) == 1 and block[0]["severity"] == "block" and block[0]["route"] == "human" and block[0]["row_ids"] == ["C7"]
    gate = M.branch_coverage_findings({"areas": []}, m)
    assert any(g["gate_id"] == "linear_in_ea" and g["severity"] == "block" and g["route"] == "human" for g in gate)
    # A thermostat relocation row is not a linear demolition item.
    counts2 = {"electrical_drawings": {"counts": [{"description": "Existing thermostat sensor relocation demolition phase: remove wiring and conduit back to VV", "quantity": 5, "unit": "EA", "sheet_number": "ED3.01"}]}}
    m2 = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs=counts2), CONV)
    assert m2["linear_items"] == [] and not [f for f in m2["findings"] if f["severity"] == "block"]


def test_polyline_length_refuses_nts_and_crossing_viewports():
    frame = R.SheetFrame(sheet_key="E2.01", document_id="d", page=1, w_in=42.0, h_in=30.0, ft_per_in=4.0)
    res = M.polyline_length(frame, [[0.1, 0.1], [0.1 + 10 / 42, 0.1]], 10.0)
    assert res["length_ft"] == pytest.approx(50.0) and res["horizontal_ft"] == pytest.approx(40.0) and res["flags"] == []
    nts = R.SheetFrame(sheet_key="E0.02", document_id="d", page=2, w_in=42.0, h_in=30.0, ft_per_in=None)
    assert M.polyline_length(nts, [[0.1, 0.1], [0.5, 0.5]], None)["flags"] == ["scale_unknown"]
    two = R.SheetFrame(sheet_key="E4.01", document_id="d", page=3, w_in=42.0, h_in=30.0, ft_per_in=4.0, scale_at=lambda x, y: 8.0 if x > 0.5 else 4.0)
    assert M.polyline_length(two, [[0.1, 0.1], [0.9, 0.1]], None)["flags"] == ["crosses_viewports"]
    assert M.polyline_length(frame, [[0.0, 0.0], [1.0, 1.0], [0.0, 1.0], [1.0, 0.0]], None)["flags"] == ["implausible_length"]
    assert M.polyline_length(frame, [[0.1, 0.1]], None)["flags"] == ["too_few_points"]


# apply_measure_to_boq --------------------------------------------------------------------------


def _boq_spec() -> dict:
    return {
        "title": "t",
        "areas": [
            {
                "area": "PHARMACY",
                "notes": [],
                "sections": [
                    {"heading": "DEMOLITION", "groups": [{"name": "DEMOLITION", "subgroups": [{"name": "", "items": [{"description": "REMOVE LIGHTING TRACK ROW", "quantity": 3, "unit": "EA", "sheet_refs": ["ED3.01"], "wastage_applies": False, "is_accessory": False}]}]}]},
                    {
                        "heading": "NEW WORK",
                        "groups": [
                            {"name": "WIRING", "subgroups": [
                                {"name": "CONDUCTORS (AWG)", "items": [
                                    {"description": "#12 THHN/THWN Cu", "quantity": 1000, "unit": "FT", "sheet_refs": ["E2.01"], "wastage_applies": True, "is_accessory": False},
                                    {"description": "#8 THHN/THWN Cu", "quantity": 200, "unit": "FT", "sheet_refs": ["E0.02"], "wastage_applies": True, "is_accessory": False},
                                ]},
                                {"name": "CONDUITS", "items": [
                                    {"description": '3/4"C EMT', "quantity": 500, "unit": "FT", "sheet_refs": ["E2.01"], "wastage_applies": True, "is_accessory": False},
                                    {"description": '1"C EMT', "quantity": 80, "unit": "FT", "sheet_refs": ["E0.02"], "wastage_applies": True, "is_accessory": False},
                                ]},
                            ]},
                            {"name": "LIGHTING FIXTURES", "subgroups": [{"name": "FIXTURES", "items": [
                                {"description": "L5, UNDERCABINET LED LIGHTING RUN", "quantity": 7, "unit": "EA", "sheet_refs": ["E3.01"], "wastage_applies": False, "is_accessory": False, "caveat": "(NEED TO CONFIRM DETAILS)"},
                                {"description": "L1 2'x4' RECESSED TROFFER", "quantity": 16, "unit": "EA", "sheet_refs": ["E3.01"], "wastage_applies": False, "is_accessory": False},
                            ]}]},
                            {"name": "OTHER ITEMS", "subgroups": [{"name": "MISCELLANEOUS", "items": [{"description": "RELOCATION OF THERMOSTAT TO NEW LOCATION", "quantity": 5, "unit": "EA", "sheet_refs": ["M2.01"], "wastage_applies": False, "is_accessory": False}]}]},
                        ],
                    },
                ],
            }
        ],
    }


def _measure_output() -> dict:
    return {
        "version": 1,
        "quantities": [
            {"row_id": "M1", "group": "WIRING", "subgroup": "CONDUCTORS (AWG)", "description": "#12 THHN/THWN Cu", "quantity": 2100.5, "unit": "FT", "wastage_applies": True, "basis": "assumed_route", "caveat": "(ASSUMED)", "sheet_refs": ["E2.01"], "refs": ["r:r1"], "derivation": {"terms": []}, "line_hint": {"area": "NEW WORK", "kind": "branch"}, "material_category": "General Material"},
            {"row_id": "M2", "group": "WIRING", "subgroup": "CONDUITS", "description": '3/4"C EMT', "quantity": 700.2, "unit": "FT", "wastage_applies": True, "basis": "assumed_route", "caveat": "(ASSUMED)", "sheet_refs": ["E2.01"], "refs": ["r:r1"], "derivation": {"terms": []}, "line_hint": {"area": "NEW WORK", "kind": "branch"}, "material_category": "General Material"},
            {"row_id": "M3", "group": "LIGHTING FIXTURES", "subgroup": "FIXTURES", "description": "L5 UNDERCABINET LED LIGHTING", "quantity": 43.3, "unit": "FT", "wastage_applies": False, "basis": "measured_route", "caveat": None, "sheet_refs": ["E3.01"], "refs": ["m:lin1"], "derivation": {"terms": []}, "line_hint": {"area": "NEW WORK", "tag": "L5", "replaces_unit": "EA", "kind": "linear_fixture"}, "material_category": "Lighting"},
            {"row_id": "M4", "group": "DEMOLITION", "subgroup": "", "description": "Remove existing lighting track row", "quantity": 50.0, "unit": "FT", "wastage_applies": False, "basis": "measured_route", "caveat": None, "sheet_refs": ["ED3.01"], "refs": ["m:trk1"], "derivation": {"terms": []}, "line_hint": {"area": "DEMOLITION", "replaces_unit": "EA", "kind": "linear_demolition"}, "material_category": "Demolition"},
            {"row_id": "M5", "group": "OTHER ITEMS", "subgroup": "MISCELLANEOUS", "description": "#18/4C CONTROL CABLE", "quantity": 125.0, "unit": "FT", "wastage_applies": True, "basis": "assumed_allowance", "caveat": "(ASSUMED)", "sheet_refs": [], "refs": [], "derivation": {"terms": []}, "line_hint": {"area": "NEW WORK", "kind": "control_cable"}, "material_category": "Low Voltage"},
        ],
        "findings": [],
        "routes": [],
        "linear_items": [],
    }


def _items(spec: dict) -> list[tuple[str, str, str, float, str]]:
    out = []
    for area in spec["areas"]:
        for section in area["sections"]:
            for group in section["groups"]:
                for sg in group["subgroups"]:
                    for it in sg["items"]:
                        out.append((group["name"], sg["name"], it["description"], it["quantity"], it["unit"]))
    return out


def test_apply_measure_to_boq_replaces_only_covered_rows():
    spec, findings = M.apply_measure_to_boq(_boq_spec(), _measure_output())
    items = _items(spec)
    descs = [i[2] for i in items]
    # Model branch rows are gone, feeders stay, M rows are in.
    assert descs.count("#12 THHN/THWN Cu") == 1 and descs.count('3/4"C EMT') == 1
    assert ("WIRING", "CONDUCTORS (AWG)", "#12 THHN/THWN Cu", 2100.5, "FT") in items
    assert ("WIRING", "CONDUITS", '3/4"C EMT', 700.2, "FT") in items
    assert ("WIRING", "CONDUCTORS (AWG)", "#8 THHN/THWN Cu", 200, "FT") in items
    assert ("WIRING", "CONDUITS", '1"C EMT', 80, "FT") in items
    # Linear EA rows the measure covers became FT; the troffer and the thermostat rows are untouched.
    assert ("LIGHTING FIXTURES", "FIXTURES", "L5, UNDERCABINET LED LIGHTING RUN", 43.3, "FT") in items
    assert ("DEMOLITION", "", "REMOVE LIGHTING TRACK ROW", 50.0, "FT") in items
    assert ("LIGHTING FIXTURES", "FIXTURES", "L1 2'x4' RECESSED TROFFER", 16, "EA") in items
    assert ("OTHER ITEMS", "MISCELLANEOUS", "RELOCATION OF THERMOSTAT TO NEW LOCATION", 5, "EA") in items
    assert ("OTHER ITEMS", "MISCELLANEOUS", "#18/4C CONTROL CABLE", 125.0, "FT") in items
    deleted = [f for f in findings if f["code"] == "model_branch_row_replaced"]
    assert len(deleted) == 2 and {f["before"]["description"] for f in deleted} == {"#12 THHN/THWN Cu", '3/4"C EMT'}
    assert sum(1 for f in findings if f["code"] == "linear_unit_enforced") == 2
    inserted = [f for f in findings if f["code"] == "measure_row_inserted"]
    assert len(inserted) == 3
    l5 = next(it for a in spec["areas"] for s in a["sections"] for g in s["groups"] for sg in g["subgroups"] for it in sg["items"] if it["description"].startswith("L5"))
    assert l5["basis"] == "measured" and l5["caveat"] is None and l5["measure_ref"] == "M3" and l5["wastage_applies"] is False
    emt = next(it for a in spec["areas"] for s in a["sections"] for g in s["groups"] for sg in g["subgroups"] for it in sg["items"] if it["description"] == '3/4"C EMT')
    assert emt["basis"] == "assumed_route" and emt["caveat"] == "(ASSUMED)" and emt["wastage_applies"] and emt["measure_ref"] == "M2"
    # Idempotent: applying again changes nothing more than the first pass did.
    again, findings2 = M.apply_measure_to_boq(spec, _measure_output())
    assert _items(again) == items
    # An empty measure output leaves the spec alone.
    same, none = M.apply_measure_to_boq(_boq_spec(), {"quantities": []})
    assert _items(same) == _items(_boq_spec()) and none == []


def test_apply_measure_to_boq_on_the_stored_7154_spec():
    spec_json = json.loads(BOQ_V1.read_text())
    spec = spec_json.get("spec") or spec_json
    out, findings = M.apply_measure_to_boq(spec, _measure_output())
    items = _items(out)
    assert ("LIGHTING FIXTURES", "FIXTURES", "L5, UNDERCABINET LED LIGHTING RUN", 43.3, "FT") in items
    assert ("DEMOLITION", "", "REMOVE LIGHTING TRACK ROW", 50.0, "FT") in items
    assert ("DEMOLITION", "", "REMOVE TRACK-LIGHT HEAD ASSEMBLY", 12, "EA") in items  # heads are not track
    assert ("WIRING", "CONDUITS", '3/4"C EMT', 700.2, "FT") in items
    assert not [f for f in findings if f["code"] == "model_branch_row_replaced"]  # the stored v1 had no branch rows


def test_branch_coverage_findings_block_on_empty_and_linear_ea():
    spec = _boq_spec()
    for area in spec["areas"]:
        for section in area["sections"]:
            for group in section["groups"]:
                if group["name"] == "WIRING":
                    group["subgroups"] = []
    gate = M.branch_coverage_findings(spec, {"findings": []})
    codes = {(g["gate_id"], g["severity"]) for g in gate}
    assert ("new_devices_no_branch", "block") in codes and ("linear_in_ea", "block") in codes
    assert all("$" not in json.dumps(g) for g in gate)
    track = next(g for g in gate if g["gate_id"] == "linear_in_ea")
    assert track["route"] == "human" and "TRACK" in track["message"]
    # With the measure applied: no block.
    fixed, _ = M.apply_measure_to_boq(_boq_spec(), _measure_output())
    assert not [g for g in M.branch_coverage_findings(fixed, _measure_output()) if g["severity"] == "block"]


# Renderer chain (E23) ----------------------------------------------------------------------------


def test_renderer_derives_accessories_from_the_measure_emt_row():
    item = {"description": '3/4"C EMT', "quantity": 6337.71, "unit": "FT", "wastage_applies": True, "is_accessory": False}
    qty = boq_quantity(item)
    assert qty == pytest.approx(6971.48, abs=0.01)
    fittings, straps = conduit_fittings(item["description"], qty)
    assert [(f[1]) for f in fittings] == [140, 698, 698] and straps == 698
    assert [a[1] for a in other_conduit_accessories(straps)] == [349, 349, 70]
    spec = {"title": "t", "areas": [{"area": "A", "notes": [], "sections": [{"heading": "NEW WORK", "groups": [{"name": "WIRING", "subgroups": [{"name": "CONDUITS", "items": [item]}]}]}]}]}
    wb = openpyxl.load_workbook(__import__("io").BytesIO(render_boq_xlsx(spec)), data_only=True)
    rows = [tuple(c for c in r) for ws in wb.worksheets for r in ws.iter_rows(values_only=True)]
    text = "\n".join(" | ".join(str(c) for c in r if c is not None) for r in rows)
    for needle in ("Compression Conn", "Compression Coupling", "1-H Strap", "Plas Anchor", "Self-Tap Screw", "SQ Box"):
        assert needle in text
    for n in (140, 698, 349, 70):
        assert any(n in r for r in rows), n


# MeasureService, tools and the versioned writer ----------------------------------------------------


@pytest.fixture
def svc_and_measure():
    m = M.build_measure(_single_sheet_inputs(), CONV)
    m["linear_items"] = [
        {"measure_id": "lin1", "ref": "m:lin1", "kind": "linear_fixture", "tag": "L5", "description": "L5 UNDERCABINET", "document_id": "doc1", "page": 1, "sheet_key": "E2.01", "tag_points": [[0.3, 0.3]], "area": "NEW WORK", "group": "LIGHTING FIXTURES", "subgroup": "FIXTURES", "material_category": "Lighting", "count": 2, "flags": [], "basis": "measured_route", "overridden_by": None, "total_ft": 10.0, "instances": [{"tag_point": [0.3, 0.3], "candidate_id": "c1", "bbox": [0.3, 0.31, 0.35, 0.315], "length_ft": 6.0, "method": "vector_rect"}, {"tag_point": [0.3, 0.3], "candidate_id": "c2", "bbox": [0.4, 0.31, 0.43, 0.315], "length_ft": 4.0, "method": "vector_rect"}]},
    ]
    svc = M.MeasureService(M.MemoryMeasureStore(), conv=CONV)
    assert svc.register_run("run-synth", m) == 1
    return svc, m


def test_measure_service_protocol_methods(svc_and_measure):
    svc, m = svc_and_measure
    res = svc.measure_polyline("doc1", 1, [[0.1, 0.1], [0.1 + 10 / 42, 0.1]], 5.0)
    assert res["length_ft"] == pytest.approx(45.0) and res["ref"].startswith("m:") and res["scale"]["ft_per_in"] == 4.0
    assert svc.measure_polyline("doc1", 9, [[0.1, 0.1], [0.2, 0.2]], None)["error"] == "unknown_page"
    assert svc.measure_polyline("nope", 1, [[0.1, 0.1], [0.2, 0.2]], None)["error"] == "unavailable"
    routes = svc.synthesize_routes(None, None, None, run_id="run-synth")
    assert routes["version"] == 1 and len(routes["routes"]) == 2 and routes["totals"]["circuits"] == 2
    only = svc.synthesize_routes(None, None, ["LP1-3"], run_id="run-synth")
    assert [r["circuit"] for r in only["routes"]] == ["LP1-3"]
    exp = svc.explain_quantity("run-synth", "M3")
    assert exp["row"]["description"] == '3/4"C EMT' and exp["scales"][0]["ft_per_in"] == 4.0 and len(exp["routes"]) == 2
    assert svc.explain_quantity("run-synth", "M99")["error"] == "unknown_row"
    found = svc.find_linear_items("run-synth", "doc1", 1, "l5")
    assert found["chosen"] == ["c1", "c2"] and found["total_ft"] == 10.0 and found["tag"] == "L5"


def test_tools_registered_strict_and_dispatch_through_registry(svc_and_measure):
    svc, m = svc_and_measure
    names = {"measure_polyline", "synthesize_routes", "find_linear_items", "explain_quantity", "set_linear_item", "set_panel_location"}
    assert names <= set(T.REGISTRY)
    for name in names:
        spec = T.REGISTRY[name]
        validate_strict(spec.args_schema)
        assert spec.cost_class in ("free", "image")
    assert T.REGISTRY["set_linear_item"].mode == "write" and T.REGISTRY["set_linear_item"].kinds == frozenset({"derive", "chat"})
    assert T.REGISTRY["find_linear_items"].needs_pro_max and T.REGISTRY["find_linear_items"].cost_class == "image"
    assert T.REGISTRY["measure_polyline"].kinds == frozenset({"recount", "main", "review", "revise", "derive", "chat"})
    assert {s.name for s in T.offered_tools("derive")} >= {"measure_polyline", "find_linear_items", "set_linear_item", "set_panel_location"}
    assert "set_linear_item" not in {s.name for s in T.offered_tools("main")}
    ctx = T.ToolContext(run_id="run-synth", kind="main", services=T.fake_services(measure=svc))
    res = T.dispatch(T.REGISTRY["measure_polyline"], ctx, {"document_id": "doc1", "page": 1, "points": [[0.1, 0.1], [0.3, 0.1]], "vertical_ft": None})
    assert not res.refused and res.data["length_ft"] > 0 and res.citations == [res.data["ref"]] and res.data["ref"] in ctx.ledger_refs
    bad = T.dispatch(T.REGISTRY["measure_polyline"], ctx, {"document_id": "doc1", "page": 1})
    assert bad.refused and bad.code == "invalid_args"
    routes = T.dispatch(T.REGISTRY["synthesize_routes"], ctx, {"document_id": None, "page": None, "circuits": None})
    assert not routes.refused and "r:r1" in routes.citations and "M3" in routes.citations
    exp = T.dispatch(T.REGISTRY["explain_quantity"], ctx, {"row_id": "M3"})
    assert not exp.refused and exp.data["row"]["row_id"] == "M3"
    assert "$" not in routes.text and "$" not in exp.text


def test_write_tools_create_versions_with_overrides_and_human_beats_model(svc_and_measure):
    svc, m = svc_and_measure
    base_emt = next(q for q in m["quantities"] if q["description"] == '3/4"C EMT')["quantity"]
    derive = T.ToolContext(run_id="run-synth", kind="derive", services=T.fake_services(measure=svc))
    res = T.dispatch(T.REGISTRY["set_panel_location"], derive, {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.2, 0.2]})
    assert not res.refused and res.data["version"] == 2 and res.data["routes_changed"] == 2
    v2 = svc.latest("run-synth")
    assert v2[0] == 2 and v2[1]["overrides"][-1]["tool"] == "set_panel_location" and v2[1]["overrides"][-1]["by"] == "model"
    assert v2[1]["panels"][0]["location"]["x"] == 0.2 and v2[1]["panels"][0]["location"]["method"] == "model"
    assert next(q for q in v2[1]["quantities"] if q["description"] == '3/4"C EMT')["quantity"] != base_emt
    assert res.data["delta_ft"] == pytest.approx(next(q for q in v2[1]["quantities"] if q["description"] == '3/4"C EMT')["quantity"] - base_emt, abs=0.2)
    # Chat: a reader is refused, a writer's override is human and a later model override never replaces it.
    reader = T.ToolContext(run_id="run-synth", kind="chat", role="accountant", services=T.fake_services(measure=svc))
    assert T.dispatch(T.REGISTRY["set_panel_location"], reader, {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.3, 0.3]}).code == "writer_role_required"
    writer = T.ToolContext(run_id="run-synth", kind="chat", role="executive", services=T.fake_services(measure=svc))
    # ToolContext carries no user id or message text yet: a chat write is refused (spec 5.2, 5.8).
    assert T.dispatch(T.REGISTRY["set_panel_location"], writer, {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.3, 0.3]}).code == "chat_write_unavailable"
    writer.user_id = "u-writer"  # type: ignore[attr-defined]
    writer.message_text = "what is this panel?"  # type: ignore[attr-defined]
    assert T.dispatch(T.REGISTRY["set_panel_location"], writer, {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.3, 0.3]}).code == "change_words_required"
    writer.message_text = "move panel LP1 to the electrical room"  # type: ignore[attr-defined]
    human = T.dispatch(T.REGISTRY["set_panel_location"], writer, {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.3, 0.3]})
    assert not human.refused and human.data["version"] == 3 and svc.latest("run-synth")[1]["overrides"][-1]["by"] == "human"
    assert svc.latest("run-synth")[1]["overrides"][-1]["user_id"] == "u-writer"
    again = T.dispatch(T.REGISTRY["set_panel_location"], derive, {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.9, 0.9]})
    assert again.refused and again.code == "human_override_exists" and svc.latest("run-synth")[0] == 3
    assert svc.latest("run-synth")[1]["panels"][0]["location"]["x"] == 0.3
    unknown = T.dispatch(T.REGISTRY["set_panel_location"], derive, {"panel": "ZZ9", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.9, 0.9]})
    assert unknown.code == "unknown_panel"
    # set_linear_item: keep one candidate, then explicit endpoints.
    lin = T.dispatch(T.REGISTRY["set_linear_item"], derive, {"document_id": "doc1", "page": 1, "tag": "L5", "candidate_ids": ["c1"], "endpoints": None, "view_id": None})
    assert not lin.refused and lin.data["version"] == 4 and lin.data["total_ft"] == 6.0 and [i["id"] for i in lin.data["instances"]] == ["c1"]
    ends = T.dispatch(T.REGISTRY["set_linear_item"], writer, {"document_id": "doc1", "page": 1, "tag": "L5", "candidate_ids": [], "endpoints": [[[0.1, 0.5], [0.1 + 2 / 42, 0.5]]], "view_id": None})
    assert not ends.refused and ends.data["total_ft"] == pytest.approx(8.0) and svc.latest("run-synth")[0] == 5
    m5 = svc.latest("run-synth")[1]
    assert m5["linear_items"][0]["overridden_by"] == "human" and any(q["description"] == "L5 UNDERCABINET" and q["quantity"] == 8.0 for q in m5["quantities"])
    bad = T.dispatch(T.REGISTRY["set_linear_item"], derive, {"document_id": "doc1", "page": 1, "tag": "L5", "candidate_ids": ["zz"], "endpoints": None, "view_id": None})
    assert bad.code in ("unknown_candidate", "human_override_exists")
    assert T.dispatch(T.REGISTRY["set_linear_item"], derive, {"document_id": "doc1", "page": 7, "tag": "L5", "candidate_ids": [], "endpoints": None, "view_id": None}).code == "unknown_page"


def test_write_measure_version_and_db_store_with_a_fake_client():
    class _Query:
        def __init__(self, rows):
            self.rows = rows
            self.filters = {}
            self._desc = False

        def select(self, *_a):
            return self

        def eq(self, k, v):
            self.filters[k] = v
            return self

        def order(self, *_a, desc=False):
            self._desc = desc
            return self

        def limit(self, _n):
            return self

        def insert(self, row):
            self.rows.append(row)
            return self

        def execute(self):
            got = [r for r in self.rows if all(r.get(k) == v for k, v in self.filters.items())]
            got.sort(key=lambda r: r["version"], reverse=self._desc)
            return type("R", (), {"data": got})()

    class _SB:
        def __init__(self):
            self.rows = []

        def table(self, name):
            assert name == "plans_reader_outputs"
            return _Query(self.rows)

    sb = _SB()
    store = M.DbMeasureStore(sb)
    assert store.latest("r1") is None
    v1 = M.write_measure_version(store, "r1", {"version": 1, "quantities": [], "overrides": []})
    v2 = M.write_measure_version(store, "r1", {"version": 1, "quantities": [], "overrides": []}, override={"tool": "set_panel_location", "args": {"panel": "LP1"}, "by": "human", "user_id": "u1"})
    assert (v1, v2) == (1, 2) and store.latest("r1")[0] == 2
    assert store.latest("r1")[1]["overrides"][0]["by"] == "human" and store.latest("r1")[1]["overrides"][0]["at"]
    assert set(sb.rows[0]) == {"run_id", "scope", "category", "version", "output", "source_group_ids"} and sb.rows[0]["scope"] == "measure"


def test_replay_stage_registered():
    spec = replay.stage("derive")
    assert spec.version == M.STAGE_VERSION and not spec.legacy and spec.owner == "WS3+WS5" and "measure" in spec.writes


def test_install_service_registers_with_the_tool_registry():
    before = T.services().measure
    try:
        svc = M.install_service(M.MeasureService(M.MemoryMeasureStore(), conv=CONV))
        assert T.services().measure is svc and M.service() is svc
    finally:
        T.register_services(measure=before)


def test_recompute_keeps_lower_tier_routes_in_quantities():
    # A tagged sheet plus an untagged equipment connection: the override
    # recompute moves only the tagged routes and keeps the equipment allowance.
    inputs = _single_sheet_inputs(category_outputs={"electrical_drawings": {"counts": [{"description": "ELECTRICAL CONNECTION TO NEW VAV", "quantity": 1, "unit": "EA", "sheet_number": "E1.01"}]}})
    page2 = _page(2, "E1.01", "POWER PLAN - NEW WORK", 4.0)
    inputs.documents[0].facts.pages.append(DocFacts.from_dict({"sha256": "b" * 64, "kind": "drawing", "page_count": 1, "pages": [page2]}).pages[0])
    m = M.build_measure(inputs, CONV)
    assert sorted(r["tier"] for r in m["routes"]) == ["equipment", "tagged", "tagged"]
    eq_ft = CONV.homerun_default_ft
    override = {"tool": "set_panel_location", "args": {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.2, 0.2]}, "by": "model"}
    new = M.recompute_with_overrides(m, CONV, override)
    assert sorted(r["tier"] for r in new["routes"]) == ["equipment", "tagged", "tagged"]
    assert [r["route_id"] for r in new["routes"]] == ["r1", "r2", "r3"]
    assert new["totals"]["by_tier"] == {"equipment": 1, "tagged": 2}
    emt_new = next(q for q in new["quantities"] if q["description"] == '3/4"C EMT')
    tagged_emt = sum(s["length_ft"] for r in new["routes"] if r["tier"] == "tagged" for s in r["segments"])
    assert emt_new["quantity"] == pytest.approx(tagged_emt + eq_ft, abs=0.2)
    assert any(t["route_id"] == "r3" for t in emt_new["derivation"]["terms"])
    assert new["stats"]["routes"] == 3


def test_other_trades_plans_keep_the_ws1_scale_only():
    calls: list[tuple[str, int]] = []

    def segs(doc_id: str, page: int):
        calls.append((doc_id, page))
        return None

    elec = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0)
    mech = _page(2, "M2.01", "MECHANICAL PLAN", 4.0)
    mech["discipline_category"] = "mechanical_drawings"
    tagged_mech = _page(3, "M2.02", "MECHANICAL PLAN", 4.0, tags={"circuit": [_tag("LP1", 1, 0.2, 0.5)], "status": {}, "type_marks": {}, "panel_labels": []})
    tagged_mech["discipline_category"] = "mechanical_drawings"
    doc = _doc([elec, mech, tagged_mech], [])
    frames, scales, _ps = M.build_frames(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], segments=segs))
    assert sorted(frames) == ["E2.01", "M2.01", "M2.02"] and frames["M2.01"].ft_per_in == 4.0
    assert {s["sheet_key"]: s["vectors"] for s in scales} == {"E2.01": True, "M2.01": False, "M2.02": True}
    assert sorted(p for _d, p in calls) == [1, 3]
    assert not M.needs_vectors(doc.pages[1]) and M.needs_vectors(doc.pages[0]) and M.needs_vectors(doc.pages[1], panel_pages=[2])


def test_segment_grid_returns_every_segment_inside_a_box():
    W, H = 42.0, 30.0
    segs = [G.Segment(i / W, j / H, (i + 0.5) / W, j / H) for i in range(0, 40, 2) for j in range(0, 28, 2)]
    grid = M._SegmentGrid(segs, w_in=W, h_in=H)
    box = (9.9 / W, 3.9 / H, 20.6 / W, 12.1 / H)
    inside = M._segments_in_box(segs, box)
    assert inside and set(map(id, inside)) <= set(map(id, grid.in_box(box)))
    assert len(grid.in_box(box)) < len(segs)


def _load_script(name: str):
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_calibration_grid_lopo_and_acceptance_on_synthetic_rows():
    E = _load_script("plans_reader_measure_eval")

    def row(pid: str, measured: float, allowance: float, truth: float, s25: float) -> dict:
        comp = {"measured_ft": measured, "allowance_ft": allowance, "devices": 10, "wire12_ft": 3 * (measured + allowance) * 0.7, "wire10_ft": 3 * (measured + allowance) * 0.3, "s_per_25_sheets": s25, "tags": 10, "routes": 2, "tagged_routes": 2, "findings": [], "elapsed_s": 1.0, "collector_ft": measured * 0.6, "homerun_ft": measured * 0.4}
        return {"project_id": pid, "name": f"synthetic {pid}", "comp": comp, "truth": {"conduit_ft": truth, "wire_ft": 3 * truth}}

    # Every project wants route_factor 1.10 on its measured part.
    rows = [row(f"p{i}", 1000.0 * (i + 1), 100.0, 1100.0 * (i + 1) + 100.0, 10.0 + i) for i in range(5)]
    grid = E.grid_errors(rows)
    best = E.best_point(grid)
    assert best["route_factor"] == pytest.approx(1.10) and best["drop_ft_per_device"] == 0 and best["median_abs_err"] == pytest.approx(0.0)
    lopo = E.lopo(rows)
    assert lopo["n"] == 5 and lopo["median_abs_err"] == pytest.approx(0.0) and {p[1] for p in lopo["picks"]} == {1.1}
    acc = E.golden_acceptance(rows)
    assert acc["n_conduit"] == 5 and acc["conduit_within_15"] == 1.0 and acc["conduit_met"]
    assert acc["s25_p90"] == pytest.approx(13.6) and acc["time_met"]
    slow = E.golden_acceptance([row("q", 1000.0, 0.0, 3000.0, 90.0)])
    assert not slow["conduit_met"] and not slow["time_met"] and slow["wire_within_20"] == 0.0
    # Provisional only while n < 15; at n >= 15 a miss is FAILED.
    assert acc["provisional"] and not acc["failed"] and slow["conduit_status"] == "NOT MET (provisional)"
    many_bad = E.golden_acceptance([row(f"b{i}", 1000.0, 0.0, 3000.0, 5.0) for i in range(16)])
    assert not many_bad["provisional"] and many_bad["failed"] and many_bad["conduit_status"] == "FAILED"
    many_good = E.golden_acceptance([row(f"g{i}", 1000.0 * (i + 1), 100.0, 1000.0 * (i + 1) + 100.0, 5.0) for i in range(16)])
    assert not many_good["provisional"] and not many_good["failed"] and many_good["conduit_status"] == "MET"
    text = E.render_calibration(rows, grid, best, lopo, None, {"n_tagged": 5}, [])
    assert "provisional: n 5 < 15" in text and "Collector and home-run FT" in text and "not measurable" in text
    bad_text = E.render_calibration([row(f"b{i}", 1000.0, 0.0, 3000.0, 5.0) for i in range(16)], grid, best, lopo, None, {"n_tagged": 16}, [])
    assert "binding" in bad_text and "FAILED at n 16" in bad_text and "provisional:" not in bad_text


def test_markup_truth_helpers():
    T7 = _load_script("plans_reader_markup_truth")

    class _Obj(dict):
        def get_object(self):
            return self

    page = {"/Annots": [_Obj({"/Subtype": "/Line", "/L": [0, 0, 72, 0]}), _Obj({"/Subtype": "/PolyLine", "/Vertices": [0, 0, 0, 144, 72, 144]}), _Obj({"/Subtype": "/Text"})]}
    inches, n, kinds = T7.annotation_lengths_in(page)
    assert inches == pytest.approx(4.0) and n == 2 and kinds == {"/Line": 1, "/PolyLine": 1, "/Text": 1}
    m = T7.DIMS_RE.search("TRENCH 2' DEEP X 1' WIDE")
    assert m and (float(m.group(1)), float(m.group(2))) == (2.0, 1.0)
    pages = [{"ft": 101.0, "ambiguous": False, "lines": 3}, {"ft": None, "ambiguous": True, "lines": 0}]
    ok = T7.score_project(pages, {"trench_ft": 80.0, "sawcut_ft": 20.0})
    assert ok["rel_err"] == pytest.approx(0.01) and ok["within_5"] is True
    amb = T7.score_project([{"ft": 50.0, "ambiguous": False, "lines": 2}, {"ft": None, "ambiguous": True, "lines": 4}], {"trench_ft": 50.0})
    assert amb["rel_err"] is None and amb["within_5"] is None
    # Page share: only a project whose files hold one annotated page has per-page truth.
    rows = [
        {"project_id": "a", "file": "a.pdf", "pages": [{"page": 1, "ft": 101.0, "ambiguous": False, "lines": 3}, {"page": 2, "ft": None, "ambiguous": True, "lines": 0}]},
        {"project_id": "b", "file": "b.pdf", "pages": [{"page": 1, "ft": 40.0, "ambiguous": False, "lines": 2}, {"page": 2, "ft": 60.0, "ambiguous": False, "lines": 2}]},
    ]
    scores = [{"project_id": "a", "score": ok}, {"project_id": "b", "score": T7.score_project(rows[1]["pages"], {"sawcut_ft": 100.0})}]
    pages = T7.page_scores(scores, rows)
    assert [(p["project_id"], p["page"], p["within_5"]) for p in pages] == [("a", 1, True)]


# Golden 7154 ($0, the backup PDF through WS1 and the WS3 geometry) ------------------------------


@golden
@needs_backup
def test_7154_acceptance_zero_model_calls():
    data = json.loads(FIXTURE.read_text())
    elec = next(d["facts"] for d in data["documents"] if d["key"] == "elec")
    doc = M.MeasureDocument.from_facts(DOC_ID, 1, elec, label="electrical_drawings")
    cat = json.loads(CATEGORY_V1.read_text())
    cat = cat.get("output") or cat
    started = time.monotonic()
    inputs = M.MeasureInputs.from_pdfs("run7154", [doc], {DOC_ID: ELEC_PDF.read_bytes()}, category_outputs={"electrical_drawings": cat}, scope=M.ScopeInputs(thermostats_included=True, control_cable_devices=5), project_type="ti")
    with replay.no_provider_calls():
        m = M.build_measure(inputs, CONV)
    elapsed = time.monotonic() - started
    by = {q["description"]: q for q in m["quantities"]}
    emt, w12, w10 = by['3/4"C EMT']["quantity"], by["#12 THHN/THWN Cu"]["quantity"], by["#10 THHN/THWN Cu"]["quantity"]
    l5 = next(it for it in m["linear_items"] if it.get("tag") == "L5")
    track = next(it for it in m["linear_items"] if it["kind"] == "linear_demolition")
    cc = by["#18/4C CONTROL CABLE"]["quantity"]
    print(
        f"WS3 measured (7154 golden): EMT {emt} FT ({(emt - 6337.71) / 6337.71:+.1%}), #12 {w12} FT ({(w12 - 6718.32) / 6718.32:+.1%}), "
        f"#10 {w10} FT ({(w10 - 12294.8) / 12294.8:+.1%}), L5 {l5['total_ft']} FT ({(l5['total_ft'] - 43.46) / 43.46:+.1%}), "
        f"track {track['total_ft']} FT ({(track['total_ft'] - 50.07) / 50.07:+.1%}), control cable {cc} FT, elapsed {elapsed:.1f} s, "
        f"registrations {[(r['src'], r['dst'], r['method']) for r in m['registrations']]}"
    )
    assert abs(emt - 6337.71) / 6337.71 <= 0.10
    assert abs(w12 - 6718.32) / 6718.32 <= 0.20
    assert abs(w10 - 12294.80) / 12294.80 <= 0.20
    assert abs(l5["total_ft"] - 43.46) / 43.46 <= 0.05 and by[next(k for k in by if k.startswith("L5"))]["unit"] == "FT"
    assert len(l5["instances"]) == 7 and l5["basis"] == "measured_route"
    assert track["anchor_method"] == "keynote_marker" and len(track["instances"]) == 3
    assert abs(track["total_ft"] - 50.07) / 50.07 <= 0.10 and track["basis"] == "measured_route"
    assert not [f for f in m["findings"] if f["code"] == "linear_in_ea"]
    assert cc == 125.0
    assert elapsed < 60.0 and m["stats"]["elapsed_ms"] < 60_000
    assert elapsed * 25.0 / len(elec.pages if hasattr(elec, "pages") else elec["pages"]) < 60.0  # derive time per 25-sheet set
    scales = {s["sheet_key"]: s["ft_per_in"] for s in m["scales"]}
    assert scales == {"E1.00": 16.0, "ED2.01": 4.0, "ED3.01": 4.0, "E2.01": 4.0, "E3.01": 4.0}
    regs = {(r["src"], r["dst"]): r for r in m["registrations"]}
    assert regs[("ED2.01", "E1.00")]["method"] == "single_anchor" and regs[("ED2.01", "E1.00")]["transform"]["k"] == 0.25
    assert m["stats"]["tags"] == 119 and m["stats"]["routes"] == 26
    # Legacy application on the stored v1 BOQ: EMT in, L5 and track in FT, accessories follow.
    spec_json = json.loads(BOQ_V1.read_text())
    out, findings = M.apply_measure_to_boq(spec_json.get("spec") or spec_json, m)
    items = _items(out)
    assert any(d == '3/4"C EMT' and u == "FT" and q == emt for _g, _s, d, q, u in items)
    assert any(d.startswith("L5") and u == "FT" for _g, _s, d, q, u in items)
    assert any("TRACK ROW" in d and u == "FT" for _g, _s, d, q, u in items)
    assert not [g for g in M.branch_coverage_findings(out, m) if g["severity"] == "block"]


WEEKEND_BUNDLE = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/ff8fad25")


@golden
@needs_backup
@pytest.mark.skipif(not (WEEKEND_BUNDLE / "bundle.json").exists(), reason="weekend bundle not on this machine")
def test_derive_replay_stage_on_the_weekend_bundle(tmp_path):
    from app.services import eval_bundle

    bundle = eval_bundle.load_bundle(WEEKEND_BUNDLE)
    res = replay.replay(bundle, from_stage="derive", to_stage="derive", out_dir=tmp_path)
    run = res.stage("derive")
    assert run.error is None and run.mode == "rerun" and run.version == M.STAGE_VERSION and run.written == ["measure"]
    m = res.artifacts["measure"]
    by = {q["description"]: q["quantity"] for q in m["quantities"]}
    assert abs(by['3/4"C EMT'] - 6337.71) / 6337.71 <= 0.10
    # The MECH set (labeled electrical) is framed with its WS1 scale only and routes nothing.
    assert {s["sheet_key"] for s in m["scales"] if not s["vectors"]} == {"MD2.01", "MD3.02", "M2.01", "M3.02"}
    assert m["stats"]["device_sheets"] == ["E2.01", "E3.01"]


def test_failed_derive_falls_back_to_the_density_tier(monkeypatch):
    counts = {"electrical_drawings": {"counts": [{"description": "DUPLEX RECEPTACLE, 125V", "quantity": 20, "unit": "EA", "sheet_number": "E2.01"}, {"description": "REMOVE EXISTING RECEPTACLE", "quantity": 9, "unit": "EA"}]}}
    inputs = _single_sheet_inputs(category_outputs=counts, project_type="ti")

    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(M, "build_frames", boom)
    m = M.build_measure_safe(inputs, CONV)
    codes = [f["code"] for f in m["findings"]]
    assert codes == ["derive_failed", "branch_density_only"] and m["stats"]["failed"] and m["stats"]["new_devices"] == 20
    conduit = next(q for q in m["quantities"] if q["subgroup"] == "CONDUITS")
    assert conduit["basis"] == "assumed_density" and conduit["quantity"] > 0 and conduit["description"] == '3/4"C EMT'
    assert not [g for g in M.branch_coverage_findings({"areas": []}, m) if g["severity"] == "block"]
    monkeypatch.undo()
    ok = M.build_measure_safe(_single_sheet_inputs(), CONV)
    assert "derive_failed" not in [f["code"] for f in ok["findings"]] and ok["stats"]["routes"] == 2


def test_db_store_retries_a_version_taken_by_a_concurrent_writer():
    class _Q:
        def __init__(self, sb):
            self.sb = sb
            self.filters = {}
            self._insert = None

        def select(self, *_a):
            return self

        def eq(self, k, v):
            self.filters[k] = v
            return self

        def order(self, *_a, desc=False):
            return self

        def limit(self, _n):
            return self

        def insert(self, row):
            self._insert = row
            return self

        def execute(self):
            if self._insert is not None:
                if self.sb.race:
                    # Another writer lands version 1 between our read and insert.
                    self.sb.race = False
                    self.sb.rows.append({"run_id": "r1", "scope": "measure", "version": 1, "output": {}})
                if any(r["version"] == self._insert["version"] for r in self.sb.rows):
                    raise RuntimeError('duplicate key value violates unique constraint "plans_reader_outputs_uq" (23505)')
                self.sb.rows.append(self._insert)
                return type("R", (), {"data": [self._insert]})()
            got = sorted((r for r in self.sb.rows if all(r.get(k) == v for k, v in self.filters.items())), key=lambda r: -r["version"])
            return type("R", (), {"data": got[:1]})()

    class _SB:
        def __init__(self):
            self.rows = []
            self.race = True

        def table(self, _name):
            return _Q(self)

    sb = _SB()
    assert M.DbMeasureStore(sb).write("r1", {"quantities": []}) == 2
    assert [r["version"] for r in sb.rows] == [1, 2]

    class _Broken(_SB):
        def table(self, _name):
            raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        M.DbMeasureStore(_Broken()).write("r1", {})


def test_db_store_accepts_a_client_getter():
    seen = []

    class _SB:
        def table(self, name):
            seen.append(name)
            raise RuntimeError("stop")

    store = M.DbMeasureStore(lambda: _SB())
    with pytest.raises(RuntimeError, match="stop"):
        store.latest("r1")
    assert seen == ["plans_reader_outputs"]


# Wave 2 review fixes -------------------------------------------------------------------------------


def test_thermostat_inclusions_from_a_real_ws5_context_count_devices_once():
    from app.services import scope_engine as SE
    from app.services import scope_policy_seed as SEED

    cats = {"electrical_drawings": {"counts": [{"description": "RELOCATE EXISTING THERMOSTAT", "quantity": 5, "unit": "EA", "sheet_number": "E2.01"}]}}
    inclusions = SE.compute_inclusions(SE.ScopeInputs([], category_outputs=cats), SEED.SEED_POLICY)
    # WS5 emits the device record and its 125 FT companion; only the device record counts.
    assert {(i["rule_id"], i["unit"]) for i in inclusions} >= {("IN-THERMOSTAT-RELOC", "EA"), ("IN-CONTROL-CABLE", "FT")}
    scope = M.ScopeInputs.from_scope_context({"standards": {}, "inclusions": inclusions})
    assert scope.thermostats_included and scope.control_cable_devices == 5
    m = M.build_measure(_single_sheet_inputs(scope=scope), CONV)
    cc = next(q for q in m["quantities"] if q["description"] == "#18/4C CONTROL CABLE")
    assert cc["quantity"] == 125.0
    # Two device records of the same family are one thermostat set (deduped, never summed).
    twice = M.ScopeInputs.from_scope_context({"inclusions": [{"trigger_family": "thermostat", "unit": "EA", "quantity": 5}, {"trigger_family": "thermostats", "unit": "EA", "quantity": 4}]})
    assert twice.control_cable_devices == 5
    only_companion = M.ScopeInputs.from_scope_context({"inclusions": [{"trigger_family": "thermostat", "unit": "FT", "quantity": 125, "companion_of": "IN-THERMOSTAT-RELOC"}]})
    assert not only_companion.thermostats_included and only_companion.control_cable_devices == 0


def _wiring_spec(rows: list[tuple[str, str, float, str]]) -> dict:
    groups: dict[str, list[dict]] = {}
    for group, desc, qty, unit in rows:
        groups.setdefault(group, []).append({"description": desc, "quantity": qty, "unit": unit})
    return {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [{"name": g, "subgroups": [{"name": "X", "items": items}]} for g, items in groups.items()]}]}]}


def test_branch_coverage_needs_conductors_and_conduit():
    conduit_only = _wiring_spec([("OTHER ITEMS", "DUPLEX RECEPTACLE", 20, "EA"), ("WIRING", '3/4"C EMT', 500, "FT")])
    gate = M.branch_coverage_findings(conduit_only, {"findings": [], "wiring_method": "emt"})
    block = [g for g in gate if g["gate_id"] == "new_devices_no_branch"]
    assert len(block) == 1 and block[0]["severity"] == "block" and block[0]["target"]["missing"] == ["branch_conductor"]
    wire_only = _wiring_spec([("OTHER ITEMS", "DUPLEX RECEPTACLE", 20, "EA"), ("WIRING", "#12 THHN/THWN Cu", 1500, "FT")])
    assert [g["target"]["missing"] for g in M.branch_coverage_findings(wire_only, {"findings": []}) if g["gate_id"] == "new_devices_no_branch"] == [["branch_conduit"]]
    both = _wiring_spec([("OTHER ITEMS", "DUPLEX RECEPTACLE", 20, "EA"), ("WIRING", '3/4"C EMT', 500, "FT"), ("WIRING", "#12 THHN/THWN Cu", 1500, "FT")])
    assert not [g for g in M.branch_coverage_findings(both, {"findings": []}) if g["gate_id"] == "new_devices_no_branch"]
    cable = _wiring_spec([("OTHER ITEMS", "DUPLEX RECEPTACLE", 20, "EA"), ("WIRING", "#12/2C MC CABLE", 900, "FT")])
    assert not [g for g in M.branch_coverage_findings(cable, {"findings": [], "wiring_method": "mc"}) if g["gate_id"] == "new_devices_no_branch"]
    assert [g for g in M.branch_coverage_findings(cable, {"findings": [], "wiring_method": "emt"}) if g["gate_id"] == "new_devices_no_branch"]


def test_ids_persist_across_measure_versions_and_retired_ids_are_never_reissued():
    def q(row_id, desc, routes):
        return {"row_id": row_id, "group": "WIRING", "subgroup": "CONDUITS" if "EMT" in desc else "CONDUCTORS (AWG)", "description": desc, "refs": [f"r:{r}" for r in routes], "derivation": {"terms": [{"route_id": r} for r in routes]}, "line_hint": {}}

    def route(rid, circuit, tier="tagged"):
        return {"route_id": rid, "ref": f"r:{rid}", "circuit_id": circuit, "panel": "LP1", "tier": tier}

    v1 = M.stabilize_ids({"routes": [route("r1", "LP1-1"), route("r2", "LP1-3")], "quantities": [q("M1", "#12 THHN/THWN Cu", ["r1"]), q("M2", "#10 THHN/THWN Cu", ["r2"]), q("M3", '3/4"C EMT', ["r1", "r2"])], "findings": [{"code": "homerun_allowance", "refs": ["r2"]}]}, None)
    assert v1["row_seq"] == 3 and v1["route_seq"] == 2 and v1["retired_ids"] == []
    # A rebuild where the #10 row disappeared, LP1-1 is gone and a new circuit appeared (fresh numbering).
    v2 = M.stabilize_ids({"routes": [route("r1", "LP1-3"), route("r2", "LP1-5")], "quantities": [q("M1", "#12 THHN/THWN Cu", ["r1", "r2"]), q("M2", '3/4"C EMT', ["r1", "r2"])], "findings": [{"code": "homerun_allowance", "refs": ["r1"]}]}, v1)
    assert [r["route_id"] for r in v2["routes"]] == ["r2", "r3"] and v2["routes"][0]["ref"] == "r:r2"
    rows = {x["description"]: x for x in v2["quantities"]}
    assert rows['3/4"C EMT']["row_id"] == "M3" and rows["#12 THHN/THWN Cu"]["row_id"] == "M1"
    assert rows['3/4"C EMT']["refs"] == ["r:r2", "r:r3"] and [t["route_id"] for t in rows['3/4"C EMT']["derivation"]["terms"]] == ["r2", "r3"]
    assert v2["findings"][0]["refs"] == ["r2"]
    assert v2["retired_ids"] == ["M2", "r1"] and v2["row_seq"] == 3 and v2["route_seq"] == 3
    # The #10 row coming back gets a new id: M2 is retired for good.
    v3 = M.stabilize_ids({"routes": [route("r1", "LP1-3")], "quantities": [q("M1", "#12 THHN/THWN Cu", ["r1"]), q("M2", "#10 THHN/THWN Cu", ["r1"]), q("M3", '3/4"C EMT', ["r1"])]}, v2)
    rows3 = {x["description"]: x["row_id"] for x in v3["quantities"]}
    assert rows3 == {"#12 THHN/THWN Cu": "M1", "#10 THHN/THWN Cu": "M4", '3/4"C EMT': "M3"} and "M2" in v3["retired_ids"]


def test_override_versions_keep_row_and_route_ids(svc_and_measure):
    svc, m = svc_and_measure
    ids1 = {q["description"]: q["row_id"] for q in m["quantities"]}
    routes1 = {r["circuit_id"]: r["route_id"] for r in m["routes"]}
    derive = T.ToolContext(run_id="run-synth", kind="derive", services=T.fake_services(measure=svc))
    T.dispatch(T.REGISTRY["set_panel_location"], derive, {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.2, 0.2]})
    v2 = svc.latest("run-synth")[1]
    assert {q["description"]: q["row_id"] for q in v2["quantities"] if q["description"] in ids1} == {d: i for d, i in ids1.items() if d in {q["description"] for q in v2["quantities"]}}
    assert {r["circuit_id"]: r["route_id"] for r in v2["routes"]} == routes1
    # A full rebuild registered on the same run keeps them too.
    rebuilt = M.build_measure(_single_sheet_inputs(), CONV)
    svc.register_run("run-synth", rebuilt)
    v3 = svc.latest("run-synth")[1]
    assert {r["circuit_id"]: r["route_id"] for r in v3["routes"]} == routes1
    assert {q["description"]: q["row_id"] for q in v3["quantities"]} == {d: i for d, i in ids1.items() if d in {q["description"] for q in v3["quantities"]}}


def test_measure_polyline_ids_are_deterministic_stored_and_resolvable(svc_and_measure):
    svc, _m = svc_and_measure
    pts = [[0.1, 0.1], [0.1 + 10 / 42, 0.1]]
    a = svc.measure_polyline("doc1", 1, pts, 5.0, run_id="run-synth")
    b = svc.measure_polyline("doc1", 1, pts, 5.0, run_id="run-synth")
    c = svc.measure_polyline("doc1", 1, [[0.1, 0.1], [0.1 + 9 / 42, 0.1]], 5.0, run_id="run-synth")
    assert a["measure_id"] == b["measure_id"] != c["measure_id"] and a["measure_id"].startswith("pl") and a["stored"]
    stored = svc.latest("run-synth")[1]["polylines"]
    assert [p["measure_id"] for p in stored] == [a["measure_id"], c["measure_id"]] and svc.latest("run-synth")[0] == 1
    # Another worker (a fresh service over the same store) resolves the ref after a resume.
    other = M.MeasureService(svc.store, conv=CONV)
    got = other.resolve_measurement("run-synth", a["ref"])
    assert got is not None and got["source"] == "polylines" and got["length_ft"] == pytest.approx(45.0)
    assert other.resolve_measurement("run-synth", "m:lin1")["source"] == "linear_items"
    assert other.resolve_measurement("run-synth", "m:nope") is None
    # Polylines survive the next version.
    T.dispatch(T.REGISTRY["set_panel_location"], T.ToolContext(run_id="run-synth", kind="derive", services=T.fake_services(measure=svc)), {"panel": "LP1", "document_id": "doc1", "page": 1, "view_id": None, "point": [0.2, 0.2]})
    assert other.resolve_measurement("run-synth", a["ref"]) is not None


def test_addendum_reissue_supersedes_the_earlier_sheet():
    tags1 = {"circuit": [_tag("LP1", 1, 0.20, 0.50), _tag("LP1", 1, 0.25, 0.50)], "status": {}, "type_marks": {}, "panel_labels": []}
    tags2 = {"circuit": [_tag("LP1", 3, 0.60, 0.40)], "status": {}, "type_marks": {}, "panel_labels": []}
    sched = [_schedule("LP1", "120/208", [_circuit(1, "RECEPTS"), _circuit(3, "RECEPTS")])]
    loc = [{"panel": "LP1", "page": 1, "x": 0.8, "y": 0.8, "source": "plan_label"}]
    d1 = _doc([_page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0, tags=tags1)], sched, panel_locations=loc)
    d2 = _doc([_page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0, tags=tags2)], sched, panel_locations=loc)
    beats: list[int] = []
    inputs = M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, d1), M.MeasureDocument.from_facts("doc2", 2, d2)], heartbeat=lambda: beats.append(1))
    m = M.build_measure(inputs, CONV)
    status = {(s["document_id"], s["status"]) for s in m["scales"]}
    assert ("doc1", "superseded") in status and ("doc2", "selected") in status
    sup = next(s for s in m["scales"] if s["status"] == "superseded")
    assert sup["superseded_by"]["document_id"] == "doc2" and sup["sheet_key"] == "D1P1"
    # Only the later document's tags are routed; nothing is merged.
    assert m["stats"]["tags"] == 1 and all(d["document_id"] == "doc2" for c in m["circuits"] for d in c["devices"])
    assert M.frames_from_output(m)["E2.01"].document_id == "doc2"
    # A count row from the superseded document never lands on the winner's frame.
    frames = M.frames_from_output(m)
    assert M._row_sheet_key({"sheet_number": "E2.01", "document_id": "doc1"}, frames) is None
    assert M._row_sheet_key({"sheet_number": "E2.01", "document_id": "doc2"}, frames) == "E2.01"
    assert len(beats) >= 2  # one heartbeat per plan page in build_frames, plus the stage beats


def test_callout_mismatch_registration_never_drives_routes():
    bad = G.Registration(src="E2.01", dst="E1.00", transform=G.Transform(k=0.25, tx=9.0, ty=11.0), method="single_anchor", confidence=0.3, flags=["callout_mismatch"])
    good = G.Registration(src="E2.01", dst="ED2.01", transform=G.Transform(), method="identity_frame", confidence=1.0)
    findings: list[dict] = []
    inputs = _single_sheet_inputs(registrations=[bad, good])
    graph = M.build_registrations(inputs, {}, [], [], findings=findings)
    assert [r.method for r in graph.registrations] == ["identity_frame"]
    assert findings[0]["code"] == "registration_callout_mismatch" and findings[0]["severity"] == "warn"
    m = M.build_measure(_single_sheet_inputs(registrations=[bad]), CONV)
    assert m["registrations"] == [] and any(f["code"] == "registration_callout_mismatch" for f in m["findings"])


def test_track_anchors_never_use_model_positions():
    W, H = 42.0, 30.0
    heavy = [M.WeightedSegment(10 / W, 12 / H, 10 / W, 16 / H, 10.0)]
    legend = {"entries": [{"tag": "T", "description": "LIGHTING TRACK"}], "modifiers": [], "abbreviations": []}
    row = {"row_id": "C9", "description": "Remove existing lighting track", "quantity": 1, "unit": "EA", "sheet_number": "ED3.01", "positions": [[10.1 / W, 12.1 / H]]}

    def run(markers=None, notes=None, r=row):
        page = _page(1, "ED3.01", "LIGHTING PLAN - DEMOLITION WORK", 4.0, markers=markers, notes=notes)
        doc = _doc([page], [], legend=legend)
        inputs = M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs={"e": {"counts": [r]}}, heavy=lambda d, p: heavy)
        return M.build_measure(inputs, CONV)

    # No keynote marker: a legend TRACK entry plus model positions is NOT an anchor; human block.
    m = run()
    item = m["linear_items"][0]
    assert item["anchors"] == [] and item["total_ft"] is None and any(f["code"] == "linear_in_ea" for f in m["findings"])
    # An uncited marker far from the row positions does not anchor it.
    far = run(markers=[{"number": "4", "x": 30 / W, "y": 25 / H}])
    assert far["linear_items"][0]["anchors"] == []
    # A marker within reach of the positions anchors it (the marker is the anchor, not the position).
    near = run(markers=[{"number": "4", "x": 10.2 / W, "y": 12.2 / H}])
    it = near["linear_items"][0]
    assert it["anchors"] == [[round(10.2 / W, 4), round(12.2 / H, 4)]] and it["anchor_rule"] == "keynote_near_row" and it["total_ft"] == pytest.approx(16.0)
    # No cite and no positions: only a keynote whose text names the track anchors.
    bare = {k: v for k, v in row.items() if k != "positions"}
    unrelated = run(markers=[{"number": "2", "x": 10.2 / W, "y": 12.2 / H}], notes=[{"kind": "keynotes", "items": [{"number": "2", "text": "REMOVE EXISTING RECEPTACLE"}]}], r=bare)
    assert unrelated["linear_items"][0]["anchors"] == []
    named = run(markers=[{"number": "2", "x": 10.2 / W, "y": 12.2 / H}], notes=[{"kind": "keynotes", "items": [{"number": "2", "text": "REMOVE EXISTING LIGHTING TRACK AND HEADS"}]}], r=bare)
    assert named["linear_items"][0]["anchor_rule"] == "keynote_text" and named["linear_items"][0]["total_ft"] == pytest.approx(16.0)
    # Linear derivation terms carry the measure id (WS8 evidence key).
    q = next(x for x in named["quantities"] if x["line_hint"].get("kind") == "linear_demolition")
    assert q["derivation"]["terms"][0]["measure_id"] == q["derivation"]["terms"][0]["linear_item_id"] == named["linear_items"][0]["measure_id"]
    assert q["refs"] == [f"m:{named['linear_items'][0]['measure_id']}"]


def test_bands_come_from_the_active_file_with_lopo_and_fall_back_to_constants(tmp_path):
    from app.services import plans_reader_bands as PB
    from app.services import plans_reader_measure_bands as MB

    def mb(metric, vals):
        return PB.MetricBand(metric=metric, stratum="all", values=tuple((f"p{i}", float(v)) for i, v in enumerate(vals)))

    hist = PB.HistoryBands(band_version="hb1-test", built_at="2026-10-22", bands={
        ("emt_ft_per_device", "all"): mb("emt_ft_per_device", [10, 12, 14, 16, 18, 20, 22, 24, 26, 100]),
        ("branch_wire_ft_per_device", "all"): mb("branch_wire_ft_per_device", [30, 32, 34, 36, 38, 40, 42, 44, 46, 48]),
        ("wire_per_emt34", "all"): mb("wire_per_emt34", [3.0] * 10),
    })
    full = MB.bands_from_history(hist)
    lopo = MB.bands_from_history(hist, exclude=["p9"], case_id="p9")
    assert full.from_file and full.bands["emt_ft_per_device"]["all"]["n"] == 10 and lopo.bands["emt_ft_per_device"]["all"]["n"] == 9
    assert lopo.bands["emt_ft_per_device"]["all"]["p90"] < full.bands["emt_ft_per_device"]["all"]["p90"]
    assert lopo.excluded_project_ids_sha256 != full.excluded_project_ids_sha256
    assert MB.density_per_device("all", lopo)["emt_ft_per_device"] == lopo.bands["emt_ft_per_device"]["all"]["p50"]
    # Saved under a root, it is the active file; an empty root falls back to the constants with a warning.
    PB.save_bands(hist, tmp_path)
    active = MB.load_branch_bands(root=tmp_path)
    assert active.source == "hb1-test" and active.warning is None
    empty = MB.load_branch_bands(root=tmp_path / "none")
    assert not empty.from_file and empty.source.startswith("constants:") and empty.warning
    # The density tier and the band block of a build read the current set.
    page = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0)
    counts = {"electrical_drawings": {"counts": [{"description": "DUPLEX RECEPTACLE, 125V", "quantity": 10, "unit": "EA", "sheet_number": "E2.01"}]}}
    with MB.use(lopo):
        m = M._build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, _doc([page], []))], category_outputs=counts), CONV, lopo)
    conduit = next(q for q in m["quantities"] if q["subgroup"] == "CONDUITS")
    assert conduit["quantity"] == pytest.approx(10 * lopo.bands["emt_ft_per_device"]["all"]["p50"], abs=0.2)
    assert m["bands"]["bands_source"] == "hb1-test" and m["bands"]["bands_case_id"] == "p9" and m["bands"]["excluded_project_ids_sha256"] == lopo.excluded_project_ids_sha256


def test_build_measure_records_the_lopo_exclusion_for_an_eval_case():
    m = M.build_measure(_single_sheet_inputs(eval_case_id="7154"), CONV)
    assert m["bands"]["bands_case_id"] in ("7154", None)
    if m["bands"]["from_file"]:
        assert m["bands"]["bands_case_id"] == "7154" and m["bands"]["excluded_project_ids_sha256"]


def test_register_real_stages_includes_the_measure_derive_stage():
    replay.reset_stages()
    try:
        assert replay.stage("derive").legacy
        replay.register_real_stages()
        spec = replay.stage("derive")
        assert not spec.legacy and spec.version == M.STAGE_VERSION and "scope_context" in spec.reads
    finally:
        replay.reset_stages()
        M.register_replay_stage()


CALIBRATION_JSON = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/reports/measure_calibration.json")
NEVER_EMPTY_JSON = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/reports/measure_never_empty.json")


@golden
@pytest.mark.skipif(not CALIBRATION_JSON.exists(), reason="WS3 calibration report not on this machine")
@pytest.mark.xfail(strict=True, reason="WS3 golden acceptance FAILED at n >= 15 (branch conduit and wire shares below target); owner question per spec 0.5")
def test_ws3_golden_branch_shares_from_the_calibration_report():
    acc = json.loads(CALIBRATION_JSON.read_text())["acceptance"]
    assert acc["n_conduit"] >= 15
    assert acc["conduit_within_15"] >= 0.60 and acc["conduit_within_30"] >= 0.85
    assert acc["wire_within_20"] >= 0.60


@golden
@pytest.mark.skipif(not NEVER_EMPTY_JSON.exists(), reason="WS3 never-empty report not on this machine")
def test_ws3_untagged_projects_never_empty_given_the_readers_device_count():
    """Untagged and vector_text pool projects: with a device count (the
    reader's count rows in a real run) every one yields branch rows. The
    facts-only share is reported, not asserted (facts carry no count)."""
    report = json.loads(NEVER_EMPTY_JSON.read_text())
    print(f"WS3 measured: never-empty facts only {report['non_empty']} of {report['n']}; with the device count {report['non_empty_with_devices']} of {report['n_with_devices']}")
    assert report["n"] >= 30 and report["n_with_devices"] >= 25
    assert report["non_empty_with_devices"] == report["n_with_devices"]


# 7154 ED3.01 track demolition (integration runs 51ec302b and 2492ba7a) ---------------------------------
# Keynote markers and keynote text as WS1 read them on ED3.01 (page 8), and the stroke-weighted
# runs near the three tracks as heavy_segments + merge_runs read them from the sheet: the six
# 10 pt lines are the three tracks (each drawn as a doubled stroke, 16.67 FT at 1/4"), the rest
# are the heavy fixture outlines the other keynote 1 markers point at, the keynote marker frames
# (3 pt) and the 2 pt ceiling grid.
ED301_MARKERS = [
    {"number": "1", "x": 0.3008, "y": 0.5017}, {"number": "1", "x": 0.4505, "y": 0.51}, {"number": "1", "x": 0.4973, "y": 0.51},
    {"number": "1", "x": 0.5449, "y": 0.51}, {"number": "1", "x": 0.2804, "y": 0.5285}, {"number": "1", "x": 0.2804, "y": 0.5384},
    {"number": "1", "x": 0.2804, "y": 0.5654}, {"number": "1", "x": 0.241, "y": 0.593}, {"number": "2", "x": 0.1726, "y": 0.6341},
    {"number": "1", "x": 0.1528, "y": 0.6541}, {"number": "2", "x": 0.5554, "y": 0.6708}, {"number": "3", "x": 0.5451, "y": 0.7255},
    {"number": "3", "x": 0.2889, "y": 0.726}, {"number": "3", "x": 0.4104, "y": 0.7618}, {"number": "3", "x": 0.5141, "y": 0.7853},
    {"number": "3", "x": 0.2316, "y": 0.806},
]
ED301_KEYNOTES = [{"kind": "keynotes", "items": [
    {"number": "1", "text": "EXISTING LIGHT FIXTURE TO BE REMOVED. REMOVAL SHALL INCLUDE BUT NOT BE LIMITED TO REMOVAL OF LIGHT FIXTURE, CONDUIT, CONDUCTORS, ETC FROM LIGHT FIXTURE TO POINT OF SOURCE."},
    {"number": "2", "text": "EXISTING LIGHT SWITCH TO BE REMOVED. REMOVAL SHALL INCLUDE BUT NOT BE LIMITED TO REMOVAL OF LIGHT SWITCH, CONTROL WIRING, ETC FROM SWITCH TO POINT OF SOURCE."},
    {"number": "3", "text": "ALL EXISTING LIGHTING IN THIS AREA TO REMAIN."},
]}]
ED301_RUNS = [
    # (x0, y0, x1, y1, stroke_pt)
    (0.438929, 0.506056, 0.438929, 0.644944, 10.0), (0.439603, 0.506056, 0.439603, 0.644944, 10.0),
    (0.486548, 0.506056, 0.486548, 0.644944, 10.0), (0.487222, 0.506056, 0.487222, 0.644944, 10.0),
    (0.534167, 0.506056, 0.534167, 0.644944, 10.0), (0.534841, 0.506056, 0.534841, 0.644944, 10.0),
    (0.5781, 0.4808, 0.5781, 0.5408, 10.0), (0.4866, 0.5908, 0.9805, 0.5908, 10.0),
    (0.1647, 0.4961, 0.3107, 0.4961, 10.0), (0.2781, 0.5356, 0.379, 0.5356, 10.0), (0.1647, 0.5629, 0.2835, 0.5629, 10.0),
    (0.4462, 0.5074, 0.4549, 0.5074, 3.0), (0.4931, 0.5074, 0.5017, 0.5074, 3.0), (0.5407, 0.5074, 0.5493, 0.5074, 3.0),
    (0.4462, 0.5127, 0.4549, 0.5127, 3.0), (0.4931, 0.5127, 0.5017, 0.5127, 3.0), (0.5407, 0.5127, 0.5493, 0.5127, 3.0),
    (0.3908, 0.4975, 0.583, 0.4975, 2.0), (0.438, 0.5278, 0.5333, 0.5278, 2.0), (0.4378, 0.5061, 0.4378, 0.6449, 2.0),
    (0.4512, 0.4821, 0.4512, 0.6796, 2.0), (0.4854, 0.5061, 0.4854, 0.6449, 2.0), (0.5447, 0.464, 0.5447, 0.6796, 2.0),
]
# The track assembly row as each run's category output stored it (document id mapped onto the
# synthetic document). Run 1 cited keynote 1 with positions above the tracks; run 2 dropped the
# keynote cite and put its positions on the track bodies, where the middle marker sat 2.13 in
# from the nearest position (beyond ANCHOR_REACH_IN), so the old reach test kept 2 of 3 anchors.
TRACK_ROW_RUN1 = {"row_id": "C25", "description": "Remove existing track-lighting assemblies", "quantity": 3, "unit": "EA", "document_id": "doc1", "page": 1, "sheet": {"document_id": "doc1", "page": 1, "sheet_key": "D2P8", "sheet_number": "ED3.01"}, "sheet_number": "ED3.01", "symbol_or_tag": "(X), keynote 1", "material_category_hint": "Demolition", "location": "East public area; at (0.47,0.47) (0.53,0.47) (0.58,0.47)", "positions": [[0.47, 0.47], [0.53, 0.47], [0.58, 0.47]], "notes": "Each track has four visible head clusters. Individual heads and track footage require confirmation; no separate head quantity is included."}
TRACK_ROW_RUN2 = {"row_id": "C26", "description": "Remove existing lighting track assemblies", "quantity": 3, "unit": "EA", "document_id": "doc1", "page": 1, "sheet": {"document_id": "doc1", "page": 1, "sheet_key": "D2P8", "sheet_number": "ED3.01"}, "sheet_number": "ED3.01", "symbol_or_tag": "(X)", "material_category_hint": "Demolition", "location": "East open area; at (0.47,0.57) (0.53,0.57) (0.58,0.57)", "positions": [[0.47, 0.57], [0.53, 0.57], [0.58, 0.57]], "notes": "Three parallel tracks, each with four visible heads. Track footage is not quantified."}
HEADS_ROW_RUN2 = {"row_id": "C27", "description": "Remove existing track-light heads, components of counted tracks", "quantity": 12, "unit": "EA", "document_id": "doc1", "page": 1, "sheet_number": "ED3.01", "material_category_hint": "Demolition", "positions": [[0.47, 0.53], [0.47, 0.56], [0.47, 0.59], [0.47, 0.63], [0.53, 0.53], [0.53, 0.56], [0.53, 0.59], [0.53, 0.63], [0.58, 0.53], [0.58, 0.56], [0.58, 0.59], [0.58, 0.63]]}


def _ed301_measure(rows: list[dict]) -> dict:
    page = _page(1, "ED3.01", "OVERALL LIGHTING PLAN - DEMOLITION WORK", 4.0, markers=ED301_MARKERS, notes=ED301_KEYNOTES)
    doc = _doc([page], [])
    heavy = [M.WeightedSegment(*r) for r in ED301_RUNS]
    inputs = M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs={"electrical_drawings": {"counts": rows}}, heavy=lambda d, p: heavy)
    return M.build_measure(inputs, CONV)


@pytest.mark.parametrize(
    "rows, rule",
    [
        ([TRACK_ROW_RUN1], "keynote_cited"),  # run 51ec302b and the weekend bundle replays
        ([TRACK_ROW_RUN2, HEADS_ROW_RUN2], "keynote_near_row"),  # run 2492ba7a (33.34 FT before the fix)
        ([{**TRACK_ROW_RUN2, "positions": [[0.47, 0.57]], "location": "East open area"}], "keynote_near_row"),  # one rough position
        ([{**TRACK_ROW_RUN2, "positions": [[0.58, 0.57]], "location": "East open area", "quantity": 5}], "keynote_near_row"),  # an overcount
    ],
)
def test_track_demolition_follows_keynote_markers_and_runs_not_the_row_shape(rows, rule):
    m = _ed301_measure(rows)
    tracks = [it for it in m["linear_items"] if it["kind"] == "linear_demolition"]
    assert len(tracks) == 1  # the heads row is never a track
    item = tracks[0]
    assert item["anchor_method"] == "keynote_marker" and item["anchor_rule"] == rule
    assert sorted(item["anchors"]) == [[0.4505, 0.51], [0.4973, 0.51], [0.5449, 0.51]]
    xs = sorted(inst["polyline"][0][0] for inst in item["instances"])
    assert xs == [0.4396, 0.4872, 0.5348] and all(inst["length_ft"] == pytest.approx(16.67) for inst in item["instances"])
    assert item["total_ft"] == pytest.approx(50.01) and abs(item["total_ft"] - 50.07) / 50.07 <= 0.10
    assert not [f for f in m["findings"] if f["code"] == "linear_in_ea"]
    q = next(x for x in m["quantities"] if x["line_hint"].get("kind") == "linear_demolition")
    assert q["quantity"] == pytest.approx(50.01) and q["unit"] == "FT"


def test_track_anchors_reach_the_track_body_and_complete_congruent_siblings():
    page = M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, _doc([_page(1, "ED3.01", "DEMO", 4.0, markers=ED301_MARKERS, notes=ED301_KEYNOTES)], []))]).documents[0].facts.pages[0]
    frame = R.SheetFrame(sheet_key="ED3.01", document_id="doc1", page=1, w_in=42.0, h_in=30.0, ft_per_in=4.0)
    runs = M.merge_runs([M.WeightedSegment(*r) for r in ED301_RUNS], w_in=42.0, h_in=30.0)
    # The measured cause: without the sheet runs the run 2 positions reach only 2 of the 3 markers.
    anchors, rule, sib = M.track_anchors(TRACK_ROW_RUN2, page, frame)
    assert rule == "keynote_near_row" and sorted(anchors) == [[0.4505, 0.51], [0.5449, 0.51]] and sib == 0
    # With the sheet runs the middle track joins as the congruent sibling of the two reached.
    anchors, rule, sib = M.track_anchors(TRACK_ROW_RUN2, page, frame, runs)
    assert sorted(anchors) == [[0.4505, 0.51], [0.4973, 0.51], [0.5449, 0.51]] and sib == 1
    # One position by the west track: one marker in reach, two congruent siblings.
    one = {**TRACK_ROW_RUN2, "positions": [[0.47, 0.57]], "location": ""}
    anchors, rule, sib = M.track_anchors(one, page, frame, runs)
    assert len(anchors) == 3 and sib == 2
    # The quantity caps the siblings.
    anchors, _, sib = M.track_anchors({**one, "quantity": 2}, page, frame, runs)
    assert len(anchors) == 2 and sib == 1
    # No marker in reach: the run body reaches, the heaviest stroke class only (the keynote 2
    # switch marker over a 2 pt grid line stays out), then the siblings.
    low = {**TRACK_ROW_RUN2, "positions": [[0.47, 0.60]], "location": ""}
    anchors, rule, sib = M.track_anchors(low, page, frame, runs)
    assert rule == "keynote_near_row" and sorted(anchors) == [[0.4505, 0.51], [0.4973, 0.51], [0.5449, 0.51]]
    far = {**TRACK_ROW_RUN2, "positions": [[0.58, 0.57]], "location": ""}
    anchors, _, _ = M.track_anchors(far, page, frame, runs)
    assert [0.5554, 0.6708] not in anchors and len(anchors) == 3
    # Congruence: parallel, same length, same span; a doubled stroke is the same track.
    a = G.Segment(0.4396, 0.5061, 0.4396, 0.6449)
    assert M._congruent_runs(a, G.Segment(0.4872, 0.5061, 0.4872, 0.6449), 42.0, 30.0) == pytest.approx(2.0, abs=0.01)
    assert M._congruent_runs(a, G.Segment(0.4389, 0.5061, 0.4389, 0.6449), 42.0, 30.0) < M.TRACK_SIBLING_GAP_IN[0]
    assert M._congruent_runs(a, G.Segment(0.2781, 0.5356, 0.379, 0.5356), 42.0, 30.0) is None
    assert M._congruent_runs(a, G.Segment(0.5781, 0.4808, 0.5781, 0.5408), 42.0, 30.0) is None


# Run b87f0ffd wrote the track assembly row with its heads in a trailing clause and an
# item_key of demolition:luminaire, and its heads row cites keynote 1; run 51ec302b carried a
# heads row beside the assembly row. Literals from the three runs' category outputs (document id
# mapped onto the synthetic document). Each must give one track M row of 50.01 FT.
TRACK_ROW_RUN3 = {"row_id": "C27", "description": "Remove existing track-lighting assemblies including heads", "quantity": 3, "unit": "EA", "document_id": "doc1", "page": 1, "sheet": {"document_id": "doc1", "page": 1, "sheet_key": "D2P8", "sheet_number": "ED3.01"}, "sheet_number": "ED3.01", "symbol_or_tag": "(X)", "item_key": "demolition:luminaire|linear=0|subject=luminaire", "material_category_hint": "Demolition", "location": "Eastern pharmacy at (0.49,0.52) (0.54,0.52) (0.59,0.52)", "positions": [[0.49, 0.52], [0.54, 0.52], [0.59, 0.52]], "notes": "Three complete tracks. Individual head count and track footage require enlarged review; not represented as three individual luminaires."}
HEADS_ROW_RUN3 = {"row_id": "C29", "description": "Visible existing track-lighting heads to remove", "quantity": 12.0, "unit": "EA", "document_id": "doc1", "page": 1, "sheet": {"document_id": "doc1", "page": 1, "sheet_key": "D2P8", "sheet_number": "ED3.01"}, "sheet_number": "ED3.01", "symbol_or_tag": "(X), keynote 1", "item_key": "demolition:luminaire|linear=0|subject=luminaire", "material_category_hint": "Demolition", "positions": [[0.44, 0.5256], [0.44, 0.5589], [0.44, 0.592], [0.44, 0.6254], [0.4876, 0.5256], [0.4876, 0.5589], [0.4876, 0.592], [0.4876, 0.6254], [0.5352, 0.5256], [0.5352, 0.5589], [0.5352, 0.592], [0.5352, 0.6254]], "notes": "Blind recount raise: raise 6 to 12 (recount+second_sample)."}
HEADS_ROW_RUN1 = {"row_id": "C22", "description": "Remove existing track-light heads \u2014 visible subset", "quantity": 8, "unit": "EA", "document_id": "doc1", "page": 1, "sheet": {"document_id": "doc1", "page": 1, "sheet_key": "D2P8", "sheet_number": "ED3.01"}, "sheet_number": "ED3.01", "item_key": "demolition:luminaire|linear=0|subject=luminaire", "material_category_hint": "Demolition", "positions": [[0.4393, 0.5257], [0.4393, 0.5591], [0.4869, 0.5257], [0.4869, 0.5591], [0.4869, 0.592], [0.5345, 0.5257], [0.5345, 0.5591], [0.5345, 0.592]]}


@pytest.mark.parametrize(
    "rows",
    [
        [{**TRACK_ROW_RUN1, "item_key": "demolition:lighting_track|linear=1|subject=lighting_track"}, HEADS_ROW_RUN1],  # 51ec302b
        [{**TRACK_ROW_RUN2, "item_key": "demolition:lighting_track|linear=1|subject=lighting_track"}, HEADS_ROW_RUN2],  # 2492ba7a
        [TRACK_ROW_RUN3, HEADS_ROW_RUN3],  # b87f0ffd (no track M row before the fix)
        [{**TRACK_ROW_RUN3, "symbol_or_tag": "(X), keynote 1"}, HEADS_ROW_RUN3],  # the tag wording never decides
        [{**TRACK_ROW_RUN3, "symbol_or_tag": None}, HEADS_ROW_RUN3],
    ],
)
def test_track_selection_follows_the_item_class_on_all_three_live_runs(rows):
    m = _ed301_measure(rows)
    tracks = [it for it in m["linear_items"] if it["kind"] == "linear_demolition"]
    assert len(tracks) == 1 and tracks[0]["description"] == rows[0]["description"]  # a heads row is never a track
    assert tracks[0]["anchor_method"] == "keynote_marker"
    assert sorted(tracks[0]["anchors"]) == [[0.4505, 0.51], [0.4973, 0.51], [0.5449, 0.51]]
    q = next(x for x in m["quantities"] if x["line_hint"].get("kind") == "linear_demolition")
    assert q["quantity"] == pytest.approx(50.01) and q["unit"] == "FT"
    assert abs(q["quantity"] - 50.07) / 50.07 <= 0.10
    assert not [f for f in m["findings"] if f["code"] == "linear_in_ea"]


def test_track_identity_is_the_item_class_with_the_head_guard_on_the_primary_noun():
    yes = ["Remove existing track-lighting assemblies including heads", "Remove existing lighting track assemblies", "REMOVE LIGHTING TRACK ROW", "REMOVE EXISTING LIGHTING TRACK AND HEADS", "LIGHTING TRACK"]
    no = ["Visible existing track-lighting heads to remove", "Remove existing track-light heads, components of counted tracks", "Remove existing track-light heads \u2014 visible subset", "REMOVE TRACK-LIGHT HEAD ASSEMBLY", "REMOVE CABLE TRAY", "REMOVE UNISTRUT TRACK"]
    assert all(M._is_track_identity(parse_item(d, "EA"), d) for d in yes)
    assert not any(M._is_track_identity(parse_item(d, "EA"), d) for d in no)


# Untagged projects never empty (Five Star fix, wave 3) -----------------------------------------


def _row(row_id: str, desc: str, qty: int, sheet: str, positions=None, **kw) -> dict:
    return {"row_id": row_id, "description": desc, "quantity": qty, "unit": "EA", "sheet_number": sheet, "positions": positions, **kw}


def _untagged_inputs(*, with_schedule: bool = True, tag: bool = False, located: bool = True, **kw) -> M.MeasureInputs:
    tags = {"circuit": [_tag("LP1", 1, 0.22, 0.52)] if tag else [], "status": {}, "type_marks": {}, "panel_labels": []}
    page = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0, tags=tags)
    circuits = [
        _circuit(1, "RECEPTS - OFFICE 201", load=0.54),
        _circuit(3, "RECEPTS - BREAK ROOM", load=0.54),
        _circuit(5, "GENERAL LIGHTING (1)", load=0.6),
        {**_circuit(7, "RTU - 1", load=None), "poles": 3, "trip_a": 30},
        {**_circuit(9, "TRANSFORMER 'T-B'", load=None), "poles": 3, "trip_a": 100},
        _circuit(11, "COPIER", load=1.2),
    ]
    schedules = [_schedule("LP1", "120/208", circuits)] if with_schedule else []
    doc = _doc([page], schedules, panel_locations=[{"panel": "LP1", "page": 1, "x": 0.8, "y": 0.8, "source": "plan_label"}] if (with_schedule and located) else [])
    counts = {
        "electrical_drawings": {
            "counts": [
                _row("C1", "DUPLEX RECEPTACLE, 125V", 4, "E2.01", [[0.2, 0.5], [0.25, 0.5], [0.6, 0.5], [0.65, 0.5]]),
                _row("C2", "LIGHT FIXTURE TYPE A", 6, "E2.01", [[0.2, 0.2], [0.3, 0.2], [0.4, 0.2], [0.5, 0.2], [0.6, 0.2], [0.7, 0.2]]),
                _row("C3", "ROOFTOP-UNIT ELECTRICAL CONNECTIONS, RTU-1", 1, "E2.01", [[0.5, 0.8]]),
                _row("C4", "QUADPLEX RECEPTACLE", 20, "E2.01"),
            ]
        },
        "mechanical_drawings": {"counts": [_row("C1", "ROOFTOP UNIT POWER CONNECTIONS", 1, "E2.01", [[0.5, 0.8]])]},
    }
    base = dict(run_id="run-untagged", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs=counts, project_type="ti")
    base.update(kw)
    return M.MeasureInputs(**base)


def test_untagged_project_distributes_devices_over_schedule_circuits_by_load_kind():
    m = M.build_measure(_untagged_inputs(), CONV)
    st = m["stats"]
    assert st["untagged_project"] is True and st["tags"] == 0 and st["panel_schedules"] == ["LP1"] and st["distributed_circuits"] == 4
    by = {c["id"]: c for c in m["circuits"]}
    assert by["LP1-1"]["load_kind"] == "receptacle" and by["LP1-5"]["load_kind"] == "lighting" and by["LP1-7"]["load_kind"] == "equipment" and by["LP1-9"]["load_kind"] == "feeder"
    # Receptacles: 4 devices over 2 receptacle circuits; luminaires: 6 on the lighting circuit; the RTU connection on the RTU circuit.
    assert [len(by[k]["devices"]) for k in ("LP1-1", "LP1-3", "LP1-5", "LP1-7", "LP1-11")] == [2, 2, 6, 1, 0]
    assert all(by[k]["distributed"] for k in ("LP1-1", "LP1-3", "LP1-5", "LP1-7")) and not by["LP1-11"]["distributed"]
    # The feeder never routes; the copier keeps its home-run allowance (an unpositioned load).
    assert by["LP1-9"]["wiring"] == "none" and "LP1-9" not in {r["circuit_id"] for r in m["routes"]}
    routes = {r["circuit_id"]: r for r in m["routes"]}
    assert set(routes) == {"LP1-1", "LP1-3", "LP1-5", "LP1-7", "LP1-11"}
    for cid in ("LP1-1", "LP1-3", "LP1-5", "LP1-7"):
        r = routes[cid]
        assert r["tier"] == "untagged" and r["basis"] == "assumed_route_untagged" and r["caveat"] == "(ASSUMED)"
        assert "schedule_distribution" in r["flags"] and "untagged" in r["flags"]
        assert all(s["basis"] != "assumed_route" for s in r["segments"])
    assert routes["LP1-11"]["tier"] == "tagged" and "no_devices" in routes["LP1-11"]["flags"]
    assert routes["LP1-5"]["sizing"]["home_run_awg"] == "#12" and routes["LP1-5"]["sizing"]["inputs"]["kind"] == "lighting"
    # No synthetic chain, no equipment allowance: the schedule circuits took every positioned device.
    assert m["totals"]["by_tier"] == {"tagged": 1, "untagged": 4} and st["routed_devices"] == 10  # the RTU connection is not a per-device count
    codes = [f["code"] for f in m["findings"]]
    assert "untagged_schedule_distribution" in codes and "schedule_circuits_open" in codes and "feeder_circuits_excluded" in codes
    assert "equipment_rows_other_trades_skipped" in codes and "schedule_circuits_without_tags" not in codes and "branch_density_only" not in codes
    dist = next(f for f in m["findings"] if f["code"] == "untagged_schedule_distribution")
    assert "6 lighting devices over 1 circuits" in dist["message"] and "4 receptacle devices over 2 circuits" in dist["message"]
    rows = {q["description"]: q for q in m["quantities"]}
    assert rows['3/4"C EMT']["quantity"] > 0 and rows['3/4"C EMT']["basis"] in ("assumed_route_untagged", "assumed_allowance")
    # A tagged sheet keeps the tagged tiers: no distribution.
    tagged = M.build_measure(_untagged_inputs(tag=True), CONV)
    assert tagged["stats"]["untagged_project"] is False and tagged["stats"]["distributed_circuits"] == 0
    assert not any("schedule_distribution" in r["flags"] for r in tagged["routes"])


def test_untagged_project_without_a_schedule_takes_the_band_medians_for_unpositioned_devices():
    m = M.build_measure(_untagged_inputs(with_schedule=False), CONV)
    st = m["stats"]
    # new_devices counts every new-device count row: 4 duplex + 6 luminaires + 20 quadplex + the two RTU
    # connection rows (electrical C3, mechanical C1), which electrical_items classes as equipment_connection
    # since the plural CONNECTIONS vocabulary (V1). The band below still takes only the 30 non-connection
    # devices: connections are the equipment tier's, never per-device band devices.
    assert st["untagged_project"] and st["panel_schedules"] == [] and st["new_devices"] == 32 and st["routed_devices"] == 10
    tiers = Counter(r["tier"] for r in m["routes"])
    assert tiers == {"untagged": 2, "equipment": 1, "density": 1}  # receptacle chain, lighting chain, the RTU allowance, the band remainder
    band = next(r for r in m["routes"] if r["tier"] == "density")
    assert band["circuit_id"] == "BAND" and "untagged_device_band" in band["flags"] and "devices:20" in band["flags"]
    assert band["basis"] == R.BAND_BASIS and band["sizing"]["band"]["band_version"] == m["bands"]["bands_source"] and band["sizing"]["band"]["basis"] == "assumed_band"
    emt_per_device = band["sizing"]["emt_ft_per_device"]
    wire_per_device = band["sizing"]["wire_ft_per_device"]
    raceway = sum(s["length_ft"] for s in band["segments"] if s.get("raceway"))
    wire = sum(s["length_ft"] * sum(s["conductors_parsed"].values()) for s in band["segments"] if s.get("conductors_parsed"))
    assert raceway == pytest.approx(20 * emt_per_device, abs=0.1) and wire == pytest.approx(20 * wire_per_device, abs=0.1)
    finding = next(f for f in m["findings"] if f["code"] == "untagged_device_band")
    assert finding["severity"] == "warn" and "20 of 30" in finding["message"] and m["bands"]["bands_source"] in finding["message"]
    rows = {q["description"]: q for q in m["quantities"]}
    assert rows['3/4"C EMT']["quantity"] >= 20 * emt_per_device
    assert "branch_density_only" not in {f["code"] for f in m["findings"]}
    # Nothing positioned at all: the whole count goes through the density tier as before.
    nothing = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, _doc([_page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0)], []))], category_outputs={"electrical_drawings": {"counts": [_row("C1", "DUPLEX RECEPTACLE", 30, "E2.01")]}}, project_type="ti"), CONV)
    dens = nothing["routes"][0]
    assert dens["tier"] == "density" and dens["circuit_id"] == "DENSITY" and "branch_density_only" in dens["flags"] and dens["sizing"]["band"]["band_version"]
    assert sum(s["length_ft"] for s in dens["segments"] if s.get("raceway")) == pytest.approx(30 * dens["sizing"]["emt_ft_per_device"], abs=0.1)


def test_panel_schedule_from_model_rows_replaces_the_title_false_positive():
    page = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0)
    phantom = {"kind": "panel", "schedule_id": "s1", "page": 1, "panel": {"name": "AND", "existing": False, "volts": None, "phase": None, "circuits": [{"ckt": 1, "ckts": [1], "poles": None, "trip_a": None, "desc": "", "status": "unknown", "load_va": {}, "wire": {}, "keynotes": []} for _ in range(4)]}}
    doc = _doc([page], [phantom])
    model = {"row_id": "S2", "schedule_id": "A", "name": "Branch Panel: A", "schedule_type": "panel", "status": "active", "page": 1, "sheet_number": "E0.02", "columns": ["CKT", "Circuit Description", "Trip", "Poles", "A", "A", "Poles", "Trip", "Circuit Description", "CKT"], "rows": [["1", "RECEPTS - OFFICE", "20 A", "1", "540 VA", "720 VA", "1", "20 A", "RECEPTS - HALL", "2"], ["3", "Space", "--", "1", "", "", "1", "--", "Space", "4"]], "notes": "Volts: 120/208; Phases: 3"}
    counts = {"electrical_drawings": {"counts": [_row("C1", "DUPLEX RECEPTACLE", 2, "E2.01", [[0.2, 0.5], [0.6, 0.5]]), _row("C2", "Panel A location", 1, "E2.01", [[0.8, 0.8]])], "schedules": [model]}}
    m = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs=counts), CONV)
    assert [p["name"] for p in m["panels"]] == ["A"] and m["panels"][0]["volts"] == "120/208"
    assert m["stats"]["schedule_sources"] == {"dropped_ws1": ["AND"], "from_model_rows": ["A"], "ws1": [], "replaced_ws1": []}
    codes = {f["code"] for f in m["findings"]}
    assert {"panel_schedule_unusable", "panel_schedule_from_model_rows", "panel_location_from_count_row", "untagged_schedule_distribution"} <= codes
    assert {c["id"] for c in m["circuits"]} == {"A-1", "A-2"} and all(c["wiring"] == "new" for c in m["circuits"])
    loc = m["panels"][0]["location"]
    assert loc["method"] == "count_row" and loc["confidence"] == 0.5 and loc["sheet_key"] == "E2.01" and (loc["x"], loc["y"]) == (0.8, 0.8)
    # Measured home runs to the placed panel, one receptacle per circuit.
    assert m["stats"]["panel_sheets"] == ["E2.01"] and all(r["home_run_ft"] > 0 and "homerun_allowance" not in r["flags"] for r in m["routes"])
    assert sorted(len(c["devices"]) for c in m["circuits"]) == [1, 1]
    # A model set_panel_location override beats the count-row placement.
    override = {"tool": "set_panel_location", "by": "model", "args": {"panel": "A", "document_id": "doc1", "page": 1, "point": [0.1, 0.1]}}
    again = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs=counts, overrides=[override]), CONV)
    assert again["panels"][0]["location"]["method"] == "model" and again["panels"][0]["location"]["x"] == 0.1


def test_recompute_with_overrides_recomputes_distributed_circuits_once():
    m = M.build_measure(_untagged_inputs(), CONV)
    override = {"tool": "set_panel_location", "by": "human", "args": {"panel": "LP1", "document_id": "doc1", "page": 1, "point": [0.1, 0.9]}}
    new = M.recompute_with_overrides(m, CONV, override)
    ids = [r["circuit_id"] for r in new["routes"]]
    assert sorted(ids) == sorted(r["circuit_id"] for r in m["routes"]) and len(ids) == len(set(ids))
    dist = [r for r in new["routes"] if "schedule_distribution" in r["flags"]]
    assert len(dist) == 4 and all(r["tier"] == "untagged" and r["basis"] == "assumed_route_untagged" for r in dist)
    assert new["panels"][0]["location"]["method"] == "human"
    assert {q["description"] for q in new["quantities"]} == {q["description"] for q in m["quantities"]}


def test_plural_connections_rows_feed_the_equipment_tier_and_other_trades_are_skipped():
    page = _page(1, "E2.01", "POWER PLAN - NEW WORK", 4.0)
    doc = _doc([page], [])
    counts = {
        "electrical_drawings": {"counts": [_row("C1", "Rooftop-unit electrical connections, RTU-1 through RTU-6", 6, "E2.01", [[0.1 * i, 0.3] for i in range(1, 7)])]},
        "mechanical_drawings": {"counts": [_row("C1", "New packaged rooftop HVAC units requiring power connections", 6, "E2.01", [[0.1 * i, 0.3] for i in range(1, 7)])]},
    }
    m = M.build_measure(M.MeasureInputs(run_id="r", documents=[M.MeasureDocument.from_facts("doc1", 1, doc)], category_outputs=counts), CONV)
    assert m["totals"]["by_tier"] == {"equipment": 6}
    skipped = next(f for f in m["findings"] if f["code"] == "equipment_rows_other_trades_skipped")
    assert skipped["refs"] == ["C1"] and "1 connection row(s) (6 connections)" in skipped["message"]


FIVE_STAR_BUNDLE = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7172/785ae29e")
FIVE_STAR_FACTS = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/cache/pagefacts/ea81b290ae581c1791f776dd227d287856f6505725324b18df485cfcfca58d9a.json")
FIVE_STAR_DOC = "7280c6fe-6971-4466-ba6e-7a61c680f772"


@golden
@pytest.mark.skipif(not (FIVE_STAR_BUNDLE / "outputs/category_electrical_drawings_v2.json").exists() or not FIVE_STAR_FACTS.exists(), reason="Five Star bundle or cached facts not on this machine")
def test_7172_five_star_untagged_project_is_never_empty():
    """Dev run 785ae29e read the plans but measured EMT 829 FT: the WS1
    title regex missed "Branch Panel: A/B" and took "PANEL AND" from a spec
    sentence, so the untagged tier chained a handful of count-row points
    into one circuit. Truth: 58 20A/1P, 1 20A/2P, 1 20A/3P, 1 30A/2P,
    5 30A/3P, 1 100A/3P breakers; #12 14,268 FT, #10 12,064 FT, EMT 8,284 FT
    (wastage-inclusive, 398 new devices on the run). No model call."""
    facts = json.loads(FIVE_STAR_FACTS.read_text())
    cat = json.loads((FIVE_STAR_BUNDLE / "outputs/category_electrical_drawings_v2.json").read_text())
    doc = M.MeasureDocument.from_facts(FIVE_STAR_DOC, 1, facts, label="electrical_drawings")
    pdf = FIVE_STAR_BUNDLE / "inputs/43d65d45-550b-43b2-aeb2-be1f62b541c6.pdf"
    if pdf.exists():
        inputs = M.MeasureInputs.from_pdfs("run7172", [doc], {FIVE_STAR_DOC: pdf.read_bytes()}, category_outputs={"electrical_drawings": cat}, project_type="ti", eval_case_id="7172")
    else:
        inputs = M.MeasureInputs(run_id="run7172", documents=[doc], category_outputs={"electrical_drawings": cat}, project_type="ti", eval_case_id="7172")
    with replay.no_provider_calls():
        m = M.build_measure(inputs, CONV)
    st = m["stats"]
    assert st["tags"] == 0 and st["untagged_project"] is True and st["panel_schedules"] == ["A", "B"] and st["panel_sheets"] == ["E2.00"]
    assert st["schedule_sources"]["dropped_ws1"] == ["AND"] and st["schedule_sources"]["from_model_rows"] == ["A", "B"]
    circuits = m["circuits"]
    assert len(circuits) == 67 and sum(1 for c in circuits if c["wiring"] == "new") == 66 and sum(1 for c in circuits if c["distributed"]) == 50
    breakers = Counter((c["poles"], c["amps"]) for c in circuits)
    assert breakers == Counter({(1, 20): 58, (2, 20): 1, (3, 20): 1, (2, 30): 1, (3, 30): 5, (3, 100): 1})
    assert {p["name"]: p["volts"] for p in m["panels"]} == {"A": "277/480", "B": "120/208"}
    by = {q["description"]: q["quantity"] for q in m["quantities"]}
    emt, w12, w10 = by['3/4"C EMT'], by["#12 THHN/THWN Cu"], by["#10 THHN/THWN Cu"]
    print(f"WS3 measured (7172 Five Star): EMT {emt} FT ({(emt - 8284) / 8284:+.1%}), #12 {w12} FT, #10 {w10} FT, wire {w12 + w10} FT ({(w12 + w10 - 26332) / 26332:+.1%}), devices {st['new_devices']}, routed {st['routed_devices']}")
    assert abs(emt - 8284.0) / 8284.0 <= 0.25
    assert abs((w12 + w10) - 26332.0) / 26332.0 <= 0.25
    assert m["bands"]["emt_ft_per_device"]["position"] == "in_band" and m["bands"]["branch_wire_ft_per_device"]["position"] == "in_band"
    codes = {f["code"] for f in m["findings"]}
    assert {"panel_schedule_unusable", "panel_schedule_from_model_rows", "untagged_schedule_distribution", "feeder_circuits_excluded"} <= codes
