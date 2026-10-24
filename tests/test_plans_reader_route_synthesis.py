"""plans_reader_routes (WS3 B5): the rectilinear MST, the exit clamp, tie-ins,
the home-run allowance, cross-sheet dedupe, conductor sizing per segment,
the lower tiers (equipment, untagged, density), the M rows in the
renderer's conduit form, and the 7154 fixture replay of the whole tagged
path at $0 with precomputed registrations (no PDF, no model).

Named `route_synthesis` because tests/test_plans_reader_routes.py already
covers the AI Estimating HTTP routes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import plans_reader_geometry as G
from app.services import plans_reader_measure as M
from app.services import plans_reader_routes as R
from app.services.plans_reader_render import parse_conduit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/plans_reader/run_7154/page_facts_elec.json"
DOC_ID = "538c95b5-3df0-4d7e-b18b-c01135083d20"
CONV = R.Conventions()


def _frame(key: str = "E2.01", ft: float = 4.0, **kw) -> R.SheetFrame:
    return R.SheetFrame(sheet_key=key, document_id="doc1", page=1, w_in=42.0, h_in=30.0, ft_per_in=ft, **kw)


def _dev(x: float, y: float, key: str = "E2.01") -> R.Device:
    return R.Device(document_id="doc1", page=1, sheet_key=key, x=x, y=y, tag="LP1-1")


def _circuit(devices, **kw) -> R.CircuitSpec:
    base = dict(circuit_id="LP1-1", panel="LP1", ckts=[1], devices=list(devices), wiring="new", trip_a=20, poles=1, schedule_awg="#10", load_va=0.72)
    base.update(kw)
    return R.CircuitSpec(**base)


def _panel(x: float = 0.8, y: float = 0.8, key: str = "E2.01") -> R.PanelInfo:
    return R.PanelInfo(name="LP1", volts="120/208", phase=3, sheet_key=key, document_id="doc1", page=1, x=x, y=y, method="plan_label", confidence=0.7)


# Geometry ----------------------------------------------------------------------------------


def test_rect_mst_known_grid():
    total, edges = R.rect_mst([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    assert total == 3.0 and len(edges) == 3
    assert R.rect_mst([(2.0, 2.0)]) == (0.0, [])
    assert R.rect_mst([]) == (0.0, [])


def test_exit_clamp_preserves_manhattan_total():
    frame = _frame()
    frames = {"E2.01": frame}
    # Two devices 4 in apart on paper (16 ft); the panel sits far outside the work box.
    devices = [_dev(0.2, 0.5), _dev(0.2 + 4 / 42, 0.5)]
    panel = _panel(0.8, 0.8)
    route = R.synthesize_routes([_circuit(devices)], {"LP1": panel}, frames, R.RegistrationGraph(), CONV)[0]
    col = next(s for s in route.segments if s.kind == "collector")
    hr = next(s for s in route.segments if s.kind == "homerun")
    # Direct Manhattan from the device nearest the panel to the panel, in feet.
    dx = (0.8 - (0.2 + 4 / 42)) * 42 * 4
    dy = (0.8 - 0.5) * 30 * 4
    assert col.length_ft + hr.length_ft == pytest.approx(16.0 + dx + dy, abs=0.05)
    assert col.length_ft > 16.0  # the leg to the clamped exit point is collector
    assert route.basis == "assumed_route" and route.caveat == "(ASSUMED)"
    assert hr.polyline and hr.sheet_key == "E2.01" and hr.ft_per_in == 4.0
    assert col.polylines and all(len(pl) >= 2 for pl in col.polylines)


def test_route_factor_scales_measured_terms_only():
    frame = _frame()
    devices = [_dev(0.2, 0.5), _dev(0.3, 0.5)]
    base = R.synthesize_routes([_circuit(devices)], {"LP1": _panel()}, {"E2.01": frame}, None, CONV)[0]
    scaled = R.synthesize_routes([_circuit(devices)], {"LP1": _panel()}, {"E2.01": frame}, None, CONV.replace(route_factor=1.2))[0]
    assert scaled.total_ft == pytest.approx(base.total_ft * 1.2, rel=1e-6)
    with_rise = R.synthesize_routes([_circuit(devices)], {"LP1": _panel()}, {"E2.01": frame}, None, CONV.replace(panel_rise_ft=10, drop_ft_per_device=4))[0]
    assert with_rise.home_run_ft == pytest.approx(base.home_run_ft + 10, abs=0.01)
    assert next(s for s in with_rise.segments if s.kind == "drop").length_ft == 8.0


def test_synthesize_existing_circuit_tie_in():
    circuit = _circuit([_dev(0.2, 0.5)], wiring="tie_in", tie_in_ft=25.0, desc="EXISTING LOAD", keynotes=["E2.01:3"])
    route = R.synthesize_routes([circuit], {"LP1": _panel()}, {"E2.01": _frame()}, None, CONV)[0]
    assert [s.kind for s in route.segments] == ["tie_in"]
    assert route.total_ft == 25.0 and route.basis == "assumed_allowance" and route.tier == "tie_in"
    assert not any(s.kind == "homerun" for s in route.segments)
    none = _circuit([_dev(0.2, 0.5)], wiring="none")
    assert R.synthesize_routes([none], {"LP1": _panel()}, {"E2.01": _frame()}, None, CONV) == []


def test_synthesize_unknown_panel_uses_allowance():
    route = R.synthesize_routes([_circuit([_dev(0.2, 0.5), _dev(0.3, 0.5)])], {}, {"E2.01": _frame()}, None, CONV)[0]
    assert "homerun_allowance" in route.flags and "panel_unknown" in route.flags
    assert route.basis == "assumed_allowance" and route.caveat == "(ASSUMED)"
    allowance = next(s for s in route.segments if s.kind == "allowance")
    assert allowance.length_ft == CONV.homerun_default_ft and allowance.basis == "assumed_allowance"
    # A panel on another sheet without a registration also falls back.
    other = _panel(0.5, 0.5, key="E1.00")
    route2 = R.synthesize_routes([_circuit([_dev(0.2, 0.5)])], {"LP1": other}, {"E2.01": _frame(), "E1.00": _frame("E1.00", 16.0)}, R.RegistrationGraph(), CONV)[0]
    assert "homerun_allowance" in route2.flags and any(f.startswith("registration_missing") for f in route2.flags)
    # No scale on the device sheet: allowance with a scale flag.
    nts = R.synthesize_routes([_circuit([_dev(0.2, 0.5)])], {"LP1": _panel()}, {"E2.01": _frame(ft=None)}, None, CONV)[0]
    assert nts.basis == "assumed_allowance" and any(f.startswith("scale_unknown") for f in nts.flags)


def test_home_run_through_a_registered_overall_sheet():
    # Enlarged plan at 1/4" registers onto the overall plan at 1/16" with k = 0.25.
    enlarged = _frame("E2.01", 4.0)
    overall = R.SheetFrame(sheet_key="E1.00", document_id="doc1", page=6, w_in=42.0, h_in=30.0, ft_per_in=16.0)
    reg = G.Registration(src="E2.01", dst="E1.00", transform=G.Transform(k=0.25, tx=9.0, ty=11.0), method="single_anchor", confidence=0.7)
    graph = R.RegistrationGraph([reg])
    panel = R.PanelInfo(name="LP1", volts="120/208", sheet_key="E1.00", document_id="doc1", page=6, x=0.5, y=0.75, confidence=0.7)
    devices = [_dev(0.2, 0.5), _dev(0.25, 0.5)]
    route = R.synthesize_routes([_circuit(devices)], {"LP1": panel}, {"E2.01": enlarged, "E1.00": overall}, graph, CONV)[0]
    hr = next(s for s in route.segments if s.kind == "homerun")
    assert hr.sheet_key == "E1.00" and hr.ft_per_in == 16.0 and hr.length_ft > 0
    assert route.basis == "assumed_route" and "homerun_allowance" not in route.flags
    # The exit point maps back onto the overall sheet inside the work box image.
    assert hr.polyline[-1] == pytest.approx([0.5, 0.75], abs=1e-4)


def test_dedupe_devices_across_registered_sheets():
    enlarged = _frame("E2.01", 4.0)
    overall = R.SheetFrame(sheet_key="E1.00", document_id="doc1", page=6, w_in=42.0, h_in=30.0, ft_per_in=16.0)
    reg = G.Registration(src="E2.01", dst="E1.00", transform=G.Transform(k=0.25, tx=9.0, ty=11.0), method="single_anchor", confidence=0.7)
    graph = R.RegistrationGraph([reg])
    d1 = _dev(0.2, 0.5)
    # The same device drawn on the overall plan (mapped position), within 1 ft.
    mx, my = reg.transform.apply(0.2 * 42, 0.5 * 30)
    d2 = R.Device(document_id="doc1", page=6, sheet_key="E1.00", x=mx / 42, y=my / 30, tag="LP1-1")
    kept, flags = R._dedupe_devices([d1, d2], {"E2.01": enlarged, "E1.00": overall}, graph)
    assert list(kept) == ["E2.01"] and flags == ["dedupe:E1.00:1"]
    # Without a registration the sheets split.
    kept2, _ = R._dedupe_devices([d1, d2], {"E2.01": enlarged, "E1.00": overall}, R.RegistrationGraph())
    assert sorted(kept2) == ["E1.00", "E2.01"]
    route = R.synthesize_routes([_circuit([d1, d2])], {}, {"E2.01": enlarged, "E1.00": overall}, R.RegistrationGraph(), CONV)[0]
    assert "circuit_split_unregistered" in route.flags


def test_registered_sheets_merge_into_one_tree_with_one_home_run():
    # Two devices on the enlarged plan and one more (a different device) on the
    # overall plan: the sheets register, so one collector and one home run.
    enlarged = _frame("E2.01", 4.0)
    overall = R.SheetFrame(sheet_key="E1.00", document_id="doc1", page=6, w_in=42.0, h_in=30.0, ft_per_in=16.0)
    reg = G.Registration(src="E2.01", dst="E1.00", transform=G.Transform(k=0.25, tx=9.0, ty=11.0), method="single_anchor", confidence=0.7)
    graph = R.RegistrationGraph([reg])
    panel = R.PanelInfo(name="LP1", volts="120/208", sheet_key="E1.00", document_id="doc1", page=6, x=0.5, y=0.75, confidence=0.7)
    d1, d2 = _dev(0.2, 0.5), _dev(0.25, 0.5)
    # 10 ft east of d2 on the overall plan (16 ft per inch): 0.625 in further on E1.00.
    x2, y2 = reg.transform.apply(0.25 * 42, 0.5 * 30)
    d3 = R.Device(document_id="doc1", page=6, sheet_key="E1.00", x=(x2 + 0.625) / 42, y=y2 / 30, tag="LP1-1")
    frames = {"E2.01": enlarged, "E1.00": overall}
    route = R.synthesize_routes([_circuit([d1, d2, d3])], {"LP1": panel}, frames, graph, CONV)[0]
    assert "circuit_split_unregistered" not in route.flags and "merged:E1.00->E2.01" in route.flags
    assert [s.kind for s in route.segments].count("homerun") == 1 and "homerun_allowance" not in route.flags
    collector = next(s for s in route.segments if s.kind == "collector")
    # The merged tree adds exactly the 10 ft from d2 to d3 to the collector;
    # the exit clamp moves the same 10 ft out of the home run.
    two_only = R.synthesize_routes([_circuit([d1, d2])], {"LP1": panel}, frames, graph, CONV)[0]
    col2 = next(s for s in two_only.segments if s.kind == "collector")
    assert collector.length_ft - col2.length_ft == pytest.approx(10.0, abs=0.01)
    assert route.total_ft == pytest.approx(two_only.total_ft, abs=0.01)
    assert collector.sheet_key == "E2.01" and len(collector.polylines) == 3


def test_conductors_lighting_power_and_ground():
    frames = {"E2.01": _frame()}
    devices = [_dev(0.2, 0.5), _dev(0.3, 0.5)]
    power = R.synthesize_routes([_circuit(devices)], {"LP1": _panel()}, frames, None, CONV)[0]
    col = next(s for s in power.segments if s.kind == "collector")
    hr = next(s for s in power.segments if s.kind == "homerun")
    assert col.conductors == {"#12": 3} and col.conductors_text == "3#12" and col.raceway == '3/4"C EMT'
    assert hr.conductors == {"#10": 3} and hr.conductors_text == "3#10"
    assert power.sizing["home_run_awg"] == "#10" and power.sizing["collector_awg"] == "#12" and power.sizing["basis"] == "schedule"
    lighting = R.synthesize_routes([_circuit(devices, desc="LIGHTING PHARMACY", lighting=True, schedule_awg="#10", load_va=1.09)], {"LP1": R.PanelInfo(name="LP1", volts="480/277", sheet_key="E2.01", document_id="doc1", page=1, x=0.8, y=0.8)}, frames, None, CONV)[0]
    assert all(s.conductors == {"#12": 3} for s in lighting.segments if s.kind in ("collector", "homerun"))
    assert lighting.sizing["basis"] == "lighting_default"
    # A 30 A two-pole power circuit: 2 hots + neutral + ground, ground by breaker.
    big = R.synthesize_routes([_circuit(devices, poles=2, trip_a=30, schedule_awg="#8")], {"LP1": _panel()}, frames, None, CONV)[0]
    hr2 = next(s for s in big.segments if s.kind == "homerun")
    assert hr2.conductors == {"#8": 3, "#10": 1} and hr2.conductors_text == "3#8, 1#10G"


def test_mc_wiring_method_emits_cable_rows():
    conv = CONV.replace(wiring_method="mc")
    route = R.synthesize_routes([_circuit([_dev(0.2, 0.5), _dev(0.3, 0.5)])], {"LP1": _panel()}, {"E2.01": _frame()}, None, conv)[0]
    assert all(s.raceway is None and s.cable for s in route.segments)
    rows = R.branch_quantities([route], conv)
    assert [r["description"] for r in rows] == ["#10/2C MC CABLE", "#12/2C MC CABLE"]
    assert all(r["subgroup"] == "CONDUCTORS (AWG)" and r["unit"] == "FT" for r in rows)


def test_branch_quantities_descriptions_match_conduit_regex():
    routes = R.synthesize_routes([_circuit([_dev(0.2, 0.5), _dev(0.3, 0.5)]), _circuit([_dev(0.4, 0.6)], circuit_id="LP1-3", ckts=[3])], {"LP1": _panel()}, {"E2.01": _frame()}, None, CONV)
    rows = R.branch_quantities(routes, CONV)
    assert [r["row_id"] for r in rows] == ["M1", "M2", "M3"]
    assert [r["description"] for r in rows] == ["#12 THHN/THWN Cu", "#10 THHN/THWN Cu", '3/4"C EMT']
    conduit = rows[2]
    assert parse_conduit(conduit["description"]) == ("3/4", "EMT") and conduit["subgroup"] == "CONDUITS"
    assert all(r["unit"] == "FT" and r["wastage_applies"] and r["caveat"] == "(ASSUMED)" and r["basis"] == "assumed_route" for r in rows)
    assert conduit["quantity"] == pytest.approx(sum(r.total_ft for r in routes), abs=0.1)
    # Wire is 3 x conduit (H + N + G, one circuit per conduit).
    assert rows[0]["quantity"] + rows[1]["quantity"] == pytest.approx(3 * conduit["quantity"], abs=0.3)
    assert rows[0]["refs"] == ["r:r1", "r:r2"] and rows[0]["sheet_refs"] == ["E2.01"]
    assert {t["circuit_id"] for t in conduit["derivation"]["terms"]} == {"LP1-1", "LP1-3"}
    totals = R.route_totals(routes)
    assert totals["circuits"] == 2 and totals["raceway"] == {'3/4"C EMT': conduit["quantity"]}


def test_density_fallback_never_empty():
    routes = R.density_routes(40, CONV, project_type="ti")
    assert len(routes) == 1 and routes[0].basis == "assumed_density" and "branch_density_only" in routes[0].flags
    rows = R.branch_quantities(routes, CONV)
    conduit = next(r for r in rows if r["subgroup"] == "CONDUITS")
    assert conduit["quantity"] > 0 and conduit["basis"] == "assumed_density" and conduit["caveat"] == "(ASSUMED)"
    wire = sum(r["quantity"] for r in rows if r["subgroup"] == "CONDUCTORS (AWG)")
    band = routes[0].sizing["band"]
    assert band["band_version"] and band["basis"] == "assumed_band"
    assert conduit["quantity"] == pytest.approx(40 * routes[0].sizing["emt_ft_per_device"], rel=0.02)
    assert wire == pytest.approx(40 * routes[0].sizing["wire_ft_per_device"], rel=0.02)  # the band's branch wire FT per device p50, not 3 x conduit
    assert R.density_routes(0, CONV) == []


def test_equipment_tier_gives_every_connection_an_allowance():
    devs = [R.Device("doc1", 1, "E2.01", 0.3, 0.3, tag="ECONN", source="equipment"), R.Device("doc1", 1, "E2.01", 0.6, 0.6, tag="ECONN", source="equipment")]
    routes = R.equipment_routes(devs, {}, {"E2.01": _frame()}, CONV, route_seq=5)
    assert [r.route_id for r in routes] == ["r6", "r7"] and all(r.tier == "equipment" for r in routes)
    assert all(r.total_ft == CONV.homerun_default_ft and r.basis == "assumed_allowance" for r in routes)
    rows = R.branch_quantities(routes, CONV)
    assert next(r for r in rows if r["subgroup"] == "CONDUITS")["quantity"] == 300.0


def test_untagged_tier_groups_receptacles_into_synthetic_circuits():
    devs = [R.Device("doc1", 1, "E2.01", 0.2 + i * 0.02, 0.5, tag="N", source="census") for i in range(13)]
    routes = R.untagged_routes(devs, {"LP1": _panel()}, {"E2.01": _frame()}, None, CONV)
    assert len(routes) == 3  # 6 + 6 + 1
    assert all(r.basis == "assumed_route_untagged" and r.tier == "untagged" and r.caveat == "(ASSUMED)" for r in routes)
    assert sum(len(r.devices) for r in routes) == 13


def test_weakest_basis_and_levels():
    assert R.weakest_basis(["measured_route", "assumed_route"]) == "assumed_route"
    assert R.weakest_basis(["assumed_density", "assumed_allowance"]) == "assumed_density"
    assert R.weakest_basis([]) == "assumed_route"
    assert R.level_of("LEVEL 2 POWER PLAN") == 2 and R.level_of("3RD FLOOR LIGHTING") == 3 and R.level_of("OVERALL PLAN") is None


def test_conventions_from_settings_and_replace():
    conv = R.Conventions.from_settings()
    assert conv.min_branch_awg == "#12" and conv.branch_conduit == '3/4"C EMT' and conv.route_factor == 1.0
    assert conv.replace(route_factor=1.1).route_factor == 1.1 and conv.to_dict()["version"]
    assert R.Conventions.from_settings(route_factor=0.9).route_factor == 0.9


# 7154 fixture replay ($0, no PDF: scales from the facts, registrations precomputed) --------


def _fixture_measure() -> dict:
    data = json.loads(FIXTURE.read_text())
    elec = next(d["facts"] for d in data["documents"] if d["key"] == "elec")
    doc = M.MeasureDocument.from_facts(DOC_ID, 1, elec, label="electrical_drawings")
    regs = [
        G.Registration(src="E2.01", dst="ED2.01", transform=G.Transform(), method="identity_frame", confidence=1.0),
        G.Registration(src="E2.01", dst="E3.01", transform=G.Transform(), method="identity_frame", confidence=1.0),
        G.Registration(src="ED2.01", dst="E1.00", transform=G.Transform(k=0.25, tx=9.3605, ty=11.6837), method="single_anchor", confidence=0.7, anchor="ADMIIN. ASSISTANT"),
    ]
    inputs = M.MeasureInputs(run_id="run7154", documents=[doc], registrations=regs, project_type="ti")
    return M.build_measure(inputs, CONV)


@pytest.fixture(scope="module")
def fixture_measure() -> dict:
    return _fixture_measure()


def test_7154_replay_branch_totals(fixture_measure):
    m = fixture_measure
    assert m["stats"]["tags"] == 119 and m["stats"]["routes"] == 26 and m["stats"]["registrations"] == 3
    by = {q["description"]: q for q in m["quantities"]}
    emt, w12, w10 = by['3/4"C EMT'], by["#12 THHN/THWN Cu"], by["#10 THHN/THWN Cu"]
    print(f"WS3 measured (fixture replay): EMT {emt['quantity']} FT, #12 {w12['quantity']} FT, #10 {w10['quantity']} FT")
    # Spec 6 WS3 acceptance, pre-wastage: EMT within 10% of 6,337.71 FT; #12 and #10 within 20% (provisional).
    assert abs(emt["quantity"] - 6337.71) / 6337.71 <= 0.10
    assert abs(w12["quantity"] - 6718.32) / 6718.32 <= 0.20
    assert abs(w10["quantity"] - 12294.80) / 12294.80 <= 0.20
    assert w12["quantity"] + w10["quantity"] == pytest.approx(3 * emt["quantity"], abs=1.0)
    assert all(q["basis"] == "assumed_route" and q["caveat"] == "(ASSUMED)" for q in (emt, w12, w10))
    assert emt["sheet_refs"] == ["E1.00", "E2.01", "E3.01"]
    assert parse_conduit(emt["description"]) == ("3/4", "EMT")
    routes = {r["circuit_id"]: r for r in m["routes"]}
    assert len(routes) == 26 and all(r["basis"] == "assumed_route" for r in routes.values())
    assert routes["1HD-21"]["devices"] == 35 and routes["1LH-11"]["devices"] == 10
    assert all(s["conductors"] == "3#12" for s in routes["1HD-21"]["segments"])
    assert any(s["conductors"] == "3#10" and s["kind"] == "homerun" for s in routes["1LI-16"]["segments"])
    assert all("homerun_allowance" not in r["flags"] for r in routes.values())
    # Every home run is drawn on E1.00 at 16 ft per inch with a polyline for the evidence layer.
    hrs = [s for r in routes.values() for s in r["segments"] if s["kind"] == "homerun"]
    assert len(hrs) == 26 and all(s["sheet_key"] == "E1.00" and s["ft_per_in"] == 16.0 and len(s["polyline"]) >= 2 for s in hrs)
    assert all(0 <= v <= 1 for s in hrs for p in s["polyline"] for v in p)


def test_7154_replay_scales_panels_and_findings(fixture_measure):
    m = fixture_measure
    scales = {s["sheet_key"]: s for s in m["scales"]}
    assert {k: s["ft_per_in"] for k, s in scales.items()} == {"E1.00": 16.0, "ED2.01": 4.0, "ED3.01": 4.0, "E2.01": 4.0, "E3.01": 4.0}
    assert all(s["status"] == "selected" for s in scales.values())
    panels = {p["name"]: p for p in m["panels"]}
    assert panels["1LI"]["location"]["sheet_key"] == "E1.00" and panels["1HD"]["volts"] == "480/277"
    assert not [f for f in m["findings"] if f["severity"] == "block"]
    assert m["bands"]["wire_to_conduit"] == pytest.approx(3.0, abs=0.01) and m["bands"]["status"] == "in_band"
    assert m["conventions"]["version"] == CONV.version and m["wiring_method"] == "emt"
    assert m["stats"]["elapsed_ms"] < 60_000
