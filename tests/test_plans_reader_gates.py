"""plans_reader_gates: integrity gates, findings persistence and the pricing
gate (build spec 4.4, 5.4, 5.6, WS4). `test_gates_legacy_7154` runs the
final gates over the stored weekend main output of run ff8fad25 (the
committed fixture) and asserts the exact finding set the spec names.
Synthetic numbers everywhere else; no dollars."""

from __future__ import annotations

import copy
import json
import random
from collections import Counter

import pytest

from app.services import plans_reader_gates as G
from app.services import plans_reader_merge as MG
from app.services import plans_reader_rows as R
from app.services.estimating_contracts import (
    Fact,
    FactCite,
    GateFinding,
    InMemoryFindingsStore,
    RecordFindings,
    validate_gate_report,
)
from app.services.plans_reader_replay import registered_stages
from tests.plans_reader_fixtures import ELEC_DOC_7154, FIXTURE_7154, load_7154

SETTINGS = {
    "plans_reader_census_tol_strong": 0.05, "plans_reader_census_tol_weak": 0.10,
    "plans_reader_gates_blocking": True, "plans_reader_demo_two_line_style": False,
}
RFP_DOC = "059e95bd-55eb-4d3b-87d4-f2bea2a261d0"
HONEYWELL_QUOTE = "Access controls and all Fire detection systems for the building are provided by Honeywell."


@pytest.fixture(scope="module")
def run7154() -> dict:
    return load_7154()


@pytest.fixture(scope="module")
def elec_pages() -> dict[tuple[str, int], dict]:
    facts = json.loads((FIXTURE_7154 / "page_facts_elec.json").read_text())
    return {(ELEC_DOC_7154, p["page"]): p for p in facts["documents"][0]["facts"]["pages"]}


@pytest.fixture(scope="module")
def merged(run7154, elec_pages) -> dict:
    return MG.consolidate_category(
        "electrical_drawings", run7154["groups"], words_by_page=elec_pages, ledger=run7154["run"]["ledger"],
        documents=run7154["documents"], llm_rollup=False,
    )


def honeywell_fact() -> Fact:
    return Fact(
        id="F1", system="fire alarm and access control", statement=HONEYWELL_QUOTE, party="Honeywell", verified=True,
        cite=FactCite(document_id=RFP_DOC, page=2, quote=HONEYWELL_QUOTE), kind="provided_by_others",
    )


def damper_fact() -> Fact:
    return Fact(
        id="F2", system="fire/smoke dampers", statement="Damper assemblies are furnished and installed by mechanical.",
        party="mechanical", verified=True, cite=FactCite(document_id="m", page=2, quote="DAMPERS BY MECHANICAL"), kind="provided_by_others",
    )


@pytest.fixture(scope="module")
def legacy_report(run7154, merged) -> G.GateReport:
    return G.run_final(
        boq=run7154["main"], run={"id": run7154["run"]["id"]}, main=run7154["main"], category_outputs=[merged],
        facts=[honeywell_fact(), damper_fact()], linear_evidence={"L5"}, settings=SETTINGS,
    )


# The 7154 legacy evaluation -----------------------------------------------------------------


def test_gates_legacy_7154(run7154, merged, legacy_report):
    """The exact finding set on the stored 7154 main output.

    new_devices_no_branch counts 148 devices, not the spec's 146: the
    definition (gates.new_device_count) is every EA line of the OTHER ITEMS,
    LIGHTING FIXTURES and LIGHTING CONTROLS groups whose item class is a
    device class, and that includes the 2 X1 exit signs (EA luminaires on
    E3.01) the spec's 146 left out. Kept at 148 on purpose: an exit sign is
    a new device that needs a branch circuit like any other luminaire, so
    the zero-branch evidence is the same either way.

    census_floor: the re-measured golden check (2026-10-22, no-line pairs
    count as estimator 0) admits no family as blocking (demo_x 16 of 17 =
    94.1%), so every family warns: demo_x on ED3.01 (34 accounted vs 39)
    and the two E3.01 type-mark floors (L1 21 vs 39, L3 16 vs 32). The spec
    lists census_floor ED3.01 among the blocks; with demo_x admitted as
    blocking (the owner override below) the blocking set is the spec's."""
    rep = legacy_report
    assert rep.mode == "legacy" and rep.enforced and rep.blocking
    assert set(rep.not_applicable) == set(G.PROVENANCE_GATES) | {"branch_ratio"}
    counts = Counter(f.gate_id for f in rep.findings)
    assert counts == {
        "new_devices_no_branch": 1, "linear_in_ea": 2, "partial_basis": 1, "census_floor": 3,
        "capability_denial": 1, "excluded_system": 1,
    }
    lines = {ln["line_id"]: ln for ln in R.iter_rows(run7154["main"], kinds="B")}
    by = rep.by_gate()

    nd = by["new_devices_no_branch"][0]
    assert nd.severity == "block" and nd.route == "human"
    assert nd.evidence["conductor_ft"] == 0 and nd.evidence["raceway_ft"] == 0 and nd.evidence["cable_ft"] == 0
    assert nd.evidence["devices"] == 148  # the docstring records the definition (146 + the 2 X1 exit signs)
    assert nd.evidence["satisfied_by"] is None

    linear = {lines[f.target["line_id"]]["description"]: f for f in by["linear_in_ea"]}
    assert set(linear) == {"L5, UNDERCABINET LED LIGHTING RUN", "REMOVE LIGHTING TRACK ROW"}
    assert all(f.severity == "block" for f in linear.values())
    assert linear["REMOVE LIGHTING TRACK ROW"].target["item_class"] == "demolition of lighting_track"
    assert linear["L5, UNDERCABINET LED LIGHTING RUN"].evidence["evidence"] is True

    pb = by["partial_basis"][0]
    assert pb.severity == "block" and pb.target["sheet_key"] == "D1P8" and pb.target["sheet_number"] == "ED3.01"
    rect = [lid for lid in pb.target["row_ids"] if lines[lid]["description"] == "REMOVE RECTANGULAR LIGHT FIXTURE"]
    assert rect and pb.target["row_ids"][0] == rect[0]
    assert all(lines[lid]["sheet_refs"] == ["ED3.01"] for lid in pb.target["row_ids"])

    census = {(f.target["sheet_key"], f.target["family"]): f for f in by["census_floor"]}
    assert set(census) == {("D1P8", "demo_x"), ("D1P10", "fixture_mark:L1"), ("D1P10", "fixture_mark:L3")}
    assert all(f.severity == "warn" for f in census.values())  # no family passed the golden check
    cf = census[("D1P8", "demo_x")]
    assert (cf.evidence["floor"], cf.evidence["accounted"], cf.evidence["tolerance"]) == (39, 34, 2)
    assert (census[("D1P10", "fixture_mark:L1")].evidence["floor"], census[("D1P10", "fixture_mark:L1")].evidence["accounted"]) == (39, 21)
    assert "ED2.01" not in {f.evidence.get("sheet_number") for f in by["census_floor"]}  # 34 of 34 passes

    cd = by["capability_denial"][0]
    assert cd.severity == "block" and len(cd.evidence["hits"]) == 4
    assert {h["pattern"] for h in cd.evidence["hits"]} == {
        r"physical page calibration", r"do not use per-device averages", r"(no|without) .*invented assumed routes",
        r"cannot be defensibly measured",
    }

    ex = by["excluded_system"][0]
    assert ex.severity == "warn" and ex.evidence["moved"] is True
    assert ex.target["allowance"] == "HONEYWELL DESIGN-BUILD FA AND ACCESS INTERFACE SCOPE" and ex.target["fact_id"] == "F1"
    assert rep.moved_allowances[0]["cite"] == {"document_id": RFP_DOC, "page": 2, "quote": HONEYWELL_QUOTE}
    assert rep.excluded == []  # the damper and VAV connection lines stay: EC work

    blocking = sorted({f.gate_id for f in rep.findings if f.blocking})
    assert blocking == ["capability_denial", "linear_in_ea", "new_devices_no_branch", "partial_basis"]
    report = validate_gate_report(rep.report())
    assert report["blocking"] is True
    assert {c["code"] for c in report["checks"]} == set(counts)
    summary = rep.summary()
    assert summary["codes"] == blocking and summary["mode"] == "legacy" and summary["manual_lines_unapproved"] == 0
    assert summary["evaluated_at"].endswith(("-07:00", "-08:00"))
    assert G.pricing_allowed({"id": "r", "gate_summary": summary}) == (False, blocking)
    # owner override: demo_x admitted as blocking gives the spec's blocking set
    gs = G.GateSettings.load(SETTINGS, blocking_families=frozenset({"demo_x"}))
    rep2 = G.run_final(boq=run7154["main"], run={"id": run7154["run"]["id"]}, main=run7154["main"], category_outputs=[merged],
                       facts=[honeywell_fact(), damper_fact()], linear_evidence={"L5"}, settings=gs)
    assert sorted({f.gate_id for f in rep2.findings if f.blocking}) == [
        "capability_denial", "census_floor", "linear_in_ea", "new_devices_no_branch", "partial_basis",
    ]


def test_legacy_7154_without_facts_or_evidence(run7154, merged):
    rep = G.run_final(boq=run7154["main"], main=run7154["main"], category_outputs=[merged], settings=SETTINGS)
    counts = Counter(f.gate_id for f in rep.findings)
    assert "excluded_system" not in counts
    l5 = [f for f in rep.findings if f.gate_id == "linear_in_ea" and f.target["item_class"].startswith("luminaire_linear")]
    assert l5 and l5[0].severity == "warn"  # without per-foot evidence the class share decides: warn
    assert rep.moved_allowances == []


def test_facts_damper_is_not_fire_alarm(run7154, merged):
    assert G.systems_of("FIRE/SMOKE DAMPER POWER CONNECTION") == {"equipment_connection"}
    assert G.systems_of("fire/smoke dampers by mechanical") == {"damper"}
    assert G.systems_of("FIRE ALARM HORN STROBE") == {"fire_alarm"}
    assert G.systems_of("FIRE ALARM ROUGH-IN (CONDUIT AND BOXES)") == {"rough_in"}
    assert G.systems_of("HONEYWELL DESIGN-BUILD FA AND ACCESS INTERFACE SCOPE") == {"fire_alarm"}
    assert G.fact_systems(honeywell_fact()) == {"fire_alarm", "access_control"}
    assert G.fact_systems(damper_fact()) == {"damper"}
    # a damper fact excludes nothing on the 7154 BOQ; a fire alarm device line would be excluded,
    # and a fire alarm rough-in or a connection never is
    rep = G.run_final(boq=run7154["main"], main=run7154["main"], category_outputs=[merged], facts=[damper_fact()], settings=SETTINGS)
    assert not [f for f in rep.findings if f.gate_id == "excluded_system"]
    main = copy.deepcopy(run7154["main"])
    items = main["boq"]["areas"][0]["sections"][1]["groups"][1]["subgroups"][1]["items"]
    base = dict(items[0])
    items.append(dict(base, description="FIRE ALARM HORN STROBE", quantity=4))
    items.append(dict(base, description="FIRE ALARM ROUGH-IN, CONDUIT AND BOXES", quantity=4))
    rep = G.run_final(boq=main, main=main, category_outputs=[merged], facts=[honeywell_fact()], settings=SETTINGS)
    ex = [f for f in rep.findings if f.gate_id == "excluded_system" and "line_id" in f.target]
    assert len(ex) == 1 and ex[0].severity == "block" and ex[0].target["system"] == "fire_alarm"
    assert len(rep.excluded) == 1 and rep.excluded[0].description == "FIRE ALARM HORN STROBE" and rep.excluded[0].fact_id == "F1"
    # an unverified fact never excludes
    unverified = Fact(id="F3", system="fire alarm", statement="x", party="others", verified=False, cite=FactCite(document_id="d", page=1, quote="x"))
    rep = G.run_final(boq=main, main=main, category_outputs=[merged], facts=[unverified], settings=SETTINGS)
    assert not [f for f in rep.findings if f.gate_id == "excluded_system"]


def test_fa_devices_by_others_keeps_raceway_lines(run7154, merged):
    """Spec WS4: refusal excluded_by_fact never for raceway-by-EC lines. "FA
    devices by others; EC provides conduit and boxes" keeps the FA conduit
    (the reviewer's probe) and the devices are still excluded."""
    statement = "FA devices by others; EC provides conduit and boxes"
    fact = Fact(id="F5", system="fire alarm", statement=statement, party="others", verified=True,
                cite=FactCite(document_id="d", page=3, quote=statement), kind="provided_by_others")
    main = copy.deepcopy(run7154["main"])
    items = main["boq"]["areas"][0]["sections"][1]["groups"][1]["subgroups"][1]["items"]
    base = dict(items[0])
    items.append(dict(base, description="FIRE ALARM CONDUIT", quantity=200, unit="FT"))
    items.append(dict(base, description="3/4\" EMT FOR FIRE ALARM", quantity=200, unit="FT"))
    items.append(dict(base, description="FIRE ALARM JUNCTION BOX", quantity=6, unit="EA"))
    items.append(dict(base, description="FIRE ALARM HORN STROBE", quantity=4, unit="EA"))
    rep = G.run_final(boq=main, main=main, category_outputs=[merged], facts=[fact], settings=SETTINGS)
    ex = [f for f in rep.findings if f.gate_id == "excluded_system" and "line_id" in f.target]
    assert [e.description for e in rep.excluded] == ["FIRE ALARM HORN STROBE"]
    assert len(ex) == 1 and ex[0].severity == "block"
    assert G.is_raceway_text("FIRE ALARM CONDUIT", "FT") and G.is_raceway_text("3/4\" EMT FOR FIRE ALARM", "FT")
    assert not G.is_raceway_text("FIRE ALARM HORN STROBE") and not G.is_raceway_text("FIRE ALARM CABLE", "FT")
    assert G.fact_excludes_raceway(fact) is False
    # a fact naming no raceway (devices by others) keeps the raceway too
    assert G.fact_excludes_raceway(honeywell_fact()) is False
    # a fact that itself puts the conduit on others excludes the conduit lines
    full = Fact(id="F6", system="fire alarm", statement="Fire alarm system complete, including conduit and boxes, by others.",
                party="others", verified=True, cite=FactCite(document_id="d", page=3, quote="x"), kind="provided_by_others")
    assert G.fact_excludes_raceway(full) is True
    rep = G.run_final(boq=main, main=main, category_outputs=[merged], facts=[full], settings=SETTINGS)
    assert {e.description for e in rep.excluded} == {"FIRE ALARM CONDUIT", "3/4\" EMT FOR FIRE ALARM", "FIRE ALARM JUNCTION BOX", "FIRE ALARM HORN STROBE"}
    # a WS5 split decides first
    assert G.fact_excludes_raceway({"verified": True, "statement": "conduit and boxes by others", "split": "devices_by_others"}) is False


def test_partial_note_re_is_subtotal_re_plus_the_enlarged_tile_phrase():
    assert G.PARTIAL_NOTE_RE.pattern.startswith(R.SUBTOTAL_RE.pattern)
    for text in ("New tile subtotal only.", "inside the tiles only", "Five complete symbols in enlarged tile only", "region subtotal"):
        assert G.PARTIAL_NOTE_RE.search(text), text
    for text in ("panel schedule not complete", "clipped at match line", "existing circuit not complete", "tiles only shown for clarity"):
        assert not G.PARTIAL_NOTE_RE.search(text), text


def test_new_devices_no_branch_satisfied_by_cable_fact_or_disposition(run7154, merged):
    main = copy.deepcopy(run7154["main"])
    wiring = {"name": "WIRING", "note": None, "subgroups": [{"name": "CONDUCTORS (AWG)", "note": None, "items": [
        {"description": "#12 MC CABLE", "quantity": 500, "unit": "FT", "sheet_refs": ["E2.01"], "caveat": None, "material_note": None,
         "material_category": "General Material", "wastage_applies": True, "is_accessory": False}]}]}
    main["boq"]["areas"][0]["sections"][1]["groups"].insert(0, wiring)
    rep = G.run_final(boq=main, main=main, category_outputs=[merged], settings=SETTINGS)
    assert not [f for f in rep.findings if f.gate_id == "new_devices_no_branch"]  # cable counts as conductor and raceway
    existing = Fact(id="F4", system="branch circuits", statement="New receptacles extend the existing circuits in place.", party="EC", verified=True,
                    cite=FactCite(document_id="e", page=7, quote="EXTEND EXISTING CIRCUITS"), kind="existing_to_remain")
    rep = G.run_final(boq=run7154["main"], main=run7154["main"], category_outputs=[merged], facts=[existing], settings=SETTINGS)
    nd = [f for f in rep.findings if f.gate_id == "new_devices_no_branch"][0]
    assert nd.severity == "warn" and nd.evidence["satisfied_by"] == "fact F4"
    rep = G.run_final(boq=run7154["main"], main=run7154["main"], category_outputs=[merged], dispositions=[{"row_ids": ["C1"], "disposition": "existing_circuit", "note_ref": "t:1"}], settings=SETTINGS)
    nd = [f for f in rep.findings if f.gate_id == "new_devices_no_branch"][0]
    assert nd.severity == "warn" and nd.evidence["satisfied_by"] == "disposition existing_circuit"
    assert G.pricing_allowed({"gate_summary": rep.summary()})[0] is False  # the other blocks remain


def _device_and_cable_main(cable_description: str, cable_ft: float = 125) -> dict:
    """A tools-mode main output with 20 new duplex receptacles and one FT
    cable line (synthetic quantities)."""
    item = lambda desc, qty, unit, cat: {  # noqa: E731
        "description": desc, "quantity": qty, "unit": unit, "sheet_refs": ["E2.01"], "caveat": None,
        "material_note": None, "material_category": cat, "wastage_applies": unit == "FT", "is_accessory": False,
    }
    return {
        "brief": {}, "estimate": {}, "material_notes": [], "confidence_notes": [],
        "boq": {"title": "T", "subtitle": None, "closing_notes": [], "areas": [{"area": "A", "notes": [], "sections": [
            {"heading": "NEW WORK", "note": None, "groups": [
                {"name": "OTHER ITEMS", "note": None, "subgroups": [
                    {"name": "RECEPTACLES (RECEPTACLES INCLUDES 4\"S DEEP COMB BOX AND COVER PLATE)", "note": None,
                     "items": [item("DUPLEX RECEPTACLE", 20, "EA", "General Material")]},
                    {"name": "MISCELLANEOUS", "note": None, "items": [item(cable_description, cable_ft, "FT", "General Material")]},
                ]},
            ]},
        ]}]},
    }


def test_control_and_low_voltage_cable_never_satisfy_new_devices_no_branch():
    """Only power cable (MC, AC, NM) is branch wiring: #18/4C control cable
    and CAT6 leave 20 new receptacles with no branch circuit (block)."""
    for desc in ("#18/4C CONTROL CABLE", "CAT6 CABLE", "#22/2C LOW VOLTAGE CABLE"):
        main = _device_and_cable_main(desc)
        feet = G.branch_feet(G.boq_lines_of(main))
        assert feet["cable_ft"] == 0 and feet["conductor_ft"] == 0 and feet["raceway_ft"] == 0, desc
        assert feet["other_cable_ft"] == 125, desc
        rep = G.run_final(boq=main, main=main, settings=SETTINGS)
        nd = [f for f in rep.findings if f.gate_id == "new_devices_no_branch"]
        assert len(nd) == 1 and nd[0].severity == "block" and nd[0].evidence["devices"] == 20, desc
        assert nd[0].evidence["cable_ft"] == 0 and nd[0].evidence["other_cable_ft"] == 125
    # a control cable M row in the measure output does not satisfy it either
    measure = {"version": 1, "quantities": [{"row_id": "M1", "description": "#18/4C CONTROL CABLE", "quantity": 125, "unit": "FT", "basis": "assumed_allowance"}]}
    main = _device_and_cable_main("#18/4C CONTROL CABLE", 0)
    rep = G.run_final(boq=main, main=main, measure=measure, settings=SETTINGS)
    assert [f.severity for f in rep.findings if f.gate_id == "new_devices_no_branch"] == ["block"]
    # power cable still counts as both conductor and raceway
    for desc in ("#12 MC CABLE", "12/2 NM-B CABLE", "AC 12/2 CABLE", "12/2 ROMEX"):
        main = _device_and_cable_main(desc)
        assert G.branch_feet(G.boq_lines_of(main))["cable_ft"] == 125, desc
        assert not [f for f in G.run_final(boq=main, main=main, settings=SETTINGS).findings if f.gate_id == "new_devices_no_branch"], desc
    assert G.is_power_cable("#12 MC CABLE") and not G.is_power_cable("#18/4C CONTROL CABLE") and not G.is_power_cable("#12 THHN")


def test_measure_rows_count_as_branch(run7154, merged):
    measure = {"version": 1, "quantities": [
        {"row_id": "M1", "description": "#12 THHN/THWN Cu", "quantity": 900, "unit": "FT", "basis": "measured_route"},
        {"row_id": "M2", "description": "3/4\"C EMT", "quantity": 300, "unit": "FT", "basis": "measured_route"},
    ]}
    rep = G.run_final(boq=run7154["main"], main=run7154["main"], category_outputs=[merged], measure=measure, settings=SETTINGS)
    assert not [f for f in rep.findings if f.gate_id == "new_devices_no_branch"]
    assert G.branch_feet([], measure) == {"conductor_ft": 900.0, "raceway_ft": 300.0, "cable_ft": 0.0}


# Never lower (property) -------------------------------------------------------------------------


def _spec_lines(spec: dict) -> dict[str, float]:
    return {ln["line_id"]: ln["quantity"] for ln in R.iter_rows(spec, kinds="B")}


def test_never_lower_property(run7154, merged):
    """Whatever the gates find, no quantity of the BOQ, the rows or the
    floors changes; findings only ever route the problem."""
    rng = random.Random(7154)
    base_main = run7154["main"]
    before_rows = {r["row_id"]: r["quantity"] for r in merged["counts"]}
    before_floors = json.dumps(merged["floors"], sort_keys=True)
    for _ in range(12):
        main = copy.deepcopy(base_main)
        lines = [it for a in main["boq"]["areas"] for s in a["sections"] for g in s["groups"] for sg in g["subgroups"] for it in sg["items"]]
        for it in rng.sample(lines, 6):
            it["quantity"] = rng.choice([0, 1, it["quantity"], it["quantity"] * 3, 999])
            if rng.random() < 0.3:
                it["unit"] = rng.choice(["EA", "FT"])
        before = _spec_lines(main)
        rep = G.run_final(boq=main, main=main, category_outputs=[merged], facts=[honeywell_fact()], settings=SETTINGS, route=rng.choice(["human", "model_fix"]))
        assert _spec_lines(main) == before
        assert {r["row_id"]: r["quantity"] for r in merged["counts"]} == before_rows
        assert json.dumps(merged["floors"], sort_keys=True) == before_floors
        validate_gate_report(rep.report())
        for f in rep.findings:
            assert f.stage == "final" and f.route in ("human", "model_fix", "none")
            if f.severity == "block":
                assert f.route in G.ROUTES_FINAL
            assert len({x.fingerprint for x in rep.findings}) == len(rep.findings)


# Tools mode -----------------------------------------------------------------------------------


def _v2_line(lid: str, desc: str, qty: float, unit: str, refs: list[str], *, section="NEW WORK", group="OTHER ITEMS", combine="sum", sheet_refs=("E2.01",), **kw) -> dict:
    line = {
        "line_id": lid, "description": desc, "quantity": qty, "unit": unit, "sheet_refs": list(sheet_refs), "caveat": None,
        "material_note": None, "material_category": "General Material", "wastage_applies": False, "is_accessory": False,
        "source_refs": [{"ref": r, "multiplier": 1} for r in refs], "combine": combine, "basis": "counted", "needs_recount": False,
        "area": "A", "section": section, "group": group, "subgroup": "", "kind": "B", "row_id": lid,
    }
    line.update(kw)
    return line


def _row(rid: str, desc: str, qty: float, *, status="active", coverage="sheet_total", sheet_key="D1P1", number="E2.01", unit="EA") -> dict:
    return {"row_id": rid, "description": desc, "quantity": qty, "unit": unit, "status": status, "coverage": coverage,
            "basis": "counted_symbols", "sheet": {"document_id": "d1", "page": 1, "sheet_number": number, "sheet_key": sheet_key},
            "document_id": "d1", "page": 1, "flags": [], "superseded": [], "alternates": [], "family_tags": [], "positions": [],
            "origin": {"group_id": "g", "group_kind": "sub", "pass": 1, "local_id": "c1"}, "fingerprint": rid, "item_key": None}


def _cat(rows: list[dict]) -> dict:
    return {"rows_version": 2, "category": "electrical_drawings", "counts": rows, "runs": [], "schedules": [], "specs": [],
            "scope": [], "details": [], "questions": [], "floors": [], "row_seq": {"C": len(rows)}, "merge_log": {}}


def test_tools_mode_provenance_gates():
    rows = [_row("C1", "Duplex receptacle", 12), _row("C2", "Quad receptacle", 2), _row("C3", "GFCI receptacle", 3),
            _row("C4", "Old read", 5, status="superseded"), _row("C5", "Switch", 4, sheet_key="D1P2", number="E3.01")]
    lines = [
        _v2_line("B1", "DUPLEX RECEPTACLE", 12, "EA", ["C1"]),
        _v2_line("B2", "QUADPLEX RECEPTACLE", 2, "EA", ["C2"]),
        _v2_line("B3", "GFCI RECEPTACLE", 9, "EA", ["C3"]),  # arithmetic: 3 cited, 9 stored
        _v2_line("B4", "DUPLEX RECEPTACLE, TR", 12, "EA", ["C1"]),  # double consumption of C1
        _v2_line("B5", "OLD DEVICE", 5, "EA", ["C4"]),  # superseded citation
        _v2_line("B6", "GHOST DEVICE", 1, "EA", ["C77"]),  # unknown citation
        _v2_line("B7", "UNSOURCED DEVICE", 1, "EA", []),
        _v2_line("B8", "#12 THHN/THWN Cu", 400, "FT", ["M1"], group="WIRING", combine="sum"),
        _v2_line("B9", "3/4\"C EMT", 150, "FT", ["M2"], group="WIRING", combine="sum"),
    ]
    measure = {"version": 1, "quantities": [{"row_id": "M1", "description": "#12 THHN/THWN Cu", "quantity": 400, "unit": "FT", "basis": "measured_route"},
                                            {"row_id": "M2", "description": "3/4\"C EMT", "quantity": 150, "unit": "FT", "basis": "measured_route"}]}
    main = {"brief": {"project_summary": "17 receptacles and 3 L1 fixtures", "systems": []}, "estimate": {"extra_allowances": []}}
    rep = G.run_final(boq=lines, main=main, category_outputs=[_cat(rows)], measure=measure, settings=SETTINGS, route="model_fix")
    assert rep.mode == "tools" and set(rep.not_applicable) == {"census_floor", "branch_ratio"}  # no floors given
    by = rep.by_gate()
    assert {f.target["line_id"] for f in by["unverifiable_citation"]} == {"B5", "B6"}
    assert by["unsourced_line"][0].target["line_id"] == "B7"
    assert by["double_consumption"][0].target["ref"] == "C1" and by["double_consumption"][0].target["row_ids"] == ["B1", "B4"]
    assert by["arithmetic"][0].target["line_id"] == "B3" and by["arithmetic"][0].evidence["computed"] == 3
    assert by["orphan_rows"][0].target["row_ids"] == ["C5"]
    assert by["lost_sheets"][0].target["sheet_keys"] == ["D1P2"]
    assert by["brief_numbers"][0].evidence["claims"][0]["claim"] == "17 receptacles"
    assert "new_devices_no_branch" not in by
    assert all(f.route == "model_fix" for f in rep.findings if f.severity == "block")
    # the claims the brief gets right are not findings
    ok_main = {"brief": {"project_summary": "23 receptacles"}, "estimate": {"extra_allowances": []}}
    # (lines[7:9] consume the two M rows: an unconsumed M row is an orphan too)
    rep2 = G.run_final(boq=lines[:3] + lines[7:9], main=ok_main, category_outputs=[_cat(rows[:3])], measure=measure, settings=SETTINGS)
    assert "brief_numbers" not in rep2.by_gate() and "orphan_rows" not in rep2.by_gate()
    assert G.brief_number_claims(ok_main, lines[:3]) == []
    # A row consumed by a line that enforce moved to excluded_lines (the
    # policy's "thermostat relocation counted once" on 7154 iteration 2) is
    # accounted for, not an orphan; its sheet is used.
    excluded = [{"line_id": "B10", "description": "REMOVE EXISTING THERMOSTAT", "quantity": 4, "unit": "EA", "system": None,
                 "reason": "counted once", "source": "policy", "rule_id": "IN-THERMOSTAT-RELOC",
                 "item": _v2_line("B10", "REMOVE EXISTING THERMOSTAT", 4, "EA", ["C5"])}]
    rep3 = G.run_final(boq=lines[:3] + lines[7:9], main=ok_main, category_outputs=[_cat(rows[:3] + [rows[4]])], measure=measure,
                       excluded_lines=excluded, settings=SETTINGS)
    assert "orphan_rows" not in rep3.by_gate() and "lost_sheets" not in rep3.by_gate()
    rep4 = G.run_final(boq=lines[:3] + lines[7:9], main=ok_main, category_outputs=[_cat(rows[:3] + [rows[4]])], measure=measure, settings=SETTINGS)
    assert rep4.by_gate()["orphan_rows"][0].target["row_ids"] == ["C5"]
    # a partial line in tools mode: needs_recount or basis partial
    partial = [_v2_line("B1", "DUPLEX RECEPTACLE", 12, "EA", ["C1"], needs_recount=True, basis="partial")]
    rep3 = G.run_final(boq=partial, main=ok_main, category_outputs=[_cat(rows[:1])], measure=None, settings=SETTINGS)
    assert rep3.by_gate()["partial_basis"][0].target["row_ids"] == ["B1"]
    manual = [_v2_line("B1", "DUPLEX RECEPTACLE", 12, "EA", [], combine="manual")]
    rep4 = G.run_final(boq=manual, main=ok_main, category_outputs=[_cat(rows[:1])], measure=None, settings=SETTINGS)
    assert rep4.manual_lines_unapproved == 1 and G.pricing_allowed({"gate_summary": rep4.summary()})[1][0] == "manual_lines_unapproved"


def test_unconsumed_measure_rows_block_in_tools_mode():
    """Every measure quantity row (M) is consumed by a line or carries a
    disposition, else orphan_rows blocks with kind measure and the row ids
    (7154 run 51ec302b: the measured track demolition M5 must reach a line);
    a disposition, an excluded line or a citing line clears it; a zero
    quantity never blocks."""
    rows = [_row("C1", "Duplex receptacle", 12)]
    lines = [
        _v2_line("B1", "DUPLEX RECEPTACLE", 12, "EA", ["C1"]),
        _v2_line("B2", "#12 THHN/THWN Cu", 400, "FT", ["M1"], group="WIRING"),
    ]
    measure = {"version": 1, "quantities": [
        {"row_id": "M1", "description": "#12 THHN/THWN Cu", "quantity": 400, "unit": "FT", "basis": "measured_route"},
        {"row_id": "M2", "description": "Remove existing track-lighting assemblies", "quantity": 50.01, "unit": "FT", "basis": "measured_route"},
        {"row_id": "M3", "description": "3/4\"C EMT", "quantity": 0, "unit": "FT", "basis": "measured_route"},
    ]}
    main = {"brief": {"project_summary": "A remodel."}, "estimate": {"extra_allowances": []}}
    rep = G.run_final(boq=lines, main=main, category_outputs=[_cat(rows)], measure=measure, settings=SETTINGS, route="model_fix")
    orphans = rep.by_gate()["orphan_rows"]
    assert len(orphans) == 1 and orphans[0].target == {"row_ids": ["M2"], "kind": "measure"}
    assert orphans[0].severity == "block" and orphans[0].route == "model_fix" and "M2" in orphans[0].message
    assert rep.blocking and not G.pricing_allowed({"gate_summary": rep.summary()})[0]
    # a disposition covering the row clears it
    rep2 = G.run_final(boq=lines, main=main, category_outputs=[_cat(rows)], measure=measure, settings=SETTINGS,
                       dispositions=[{"row_ids": ["M2"], "disposition": "info_only", "reason": "track measured by hand"}])
    assert "orphan_rows" not in rep2.by_gate()
    # a line citing the row clears it; the zero-quantity M3 never blocks
    track = _v2_line("B3", "REMOVAL OF LIGHTING TRACK", 50.01, "FT", ["M2"], section="DEMOLITION", group="DEMOLITION", basis="measured")
    rep3 = G.run_final(boq=lines + [track], main=main, category_outputs=[_cat(rows)], measure=measure, settings=SETTINGS)
    assert "orphan_rows" not in rep3.by_gate()
    # an excluded line that consumed the row accounts for it
    rep4 = G.run_final(boq=lines, main=main, category_outputs=[_cat(rows)], measure=measure, settings=SETTINGS,
                       excluded_lines=[{"line_id": "B3", "reason": "policy", "item": track}])
    assert "orphan_rows" not in rep4.by_gate()
    # the pure helper: order, status and quantity rules
    index = {"M2": {"quantity": 1, "status": "active"}, "M10": {"quantity": 2}, "M1": {"quantity": 3, "status": "superseded"}, "M4": {"quantity": 0}}
    assert G.unconsumed_measure_rows(index) == ["M2", "M10"]
    assert G.unconsumed_measure_rows(index, consumed=["M2"], disposed=["M10"]) == []
    # legacy mode (v1 lines) never evaluates it
    legacy = [{"description": "DUPLEX RECEPTACLE", "quantity": 12, "unit": "EA", "section": "NEW WORK", "group": "OTHER ITEMS", "subgroup": ""}]
    assert "orphan_rows" in G.run_final(boq=legacy, main=main, category_outputs=[_cat(rows)], measure=measure, settings=SETTINGS, mode="legacy").not_applicable


def test_unit_class_and_linear_shares():
    lines = [
        _v2_line("B1", "DUPLEX RECEPTACLE", 12, "FT", ["C1"]),
        _v2_line("B2", "#12 THHN/THWN Cu", 100, "EA", ["C2"], group="WIRING"),
        _v2_line("B3", "L7, LINEAR LED PENDANT", 4, "EA", ["C3"], group="LIGHTING FIXTURES"),
        _v2_line("B4", "L8, COVE LED", 4, "EA", ["C4"], group="LIGHTING FIXTURES"),
    ]
    rows = [_row("C1", "Duplex receptacle", 12), _row("C2", "x", 100), _row("C3", "y", 4), _row("C4", "z", 4)]
    settings = dict(SETTINGS)
    rep = G.run_final(boq=lines, category_outputs=[_cat(rows)], settings=settings, measure={"version": 1, "quantities": [
        {"row_id": "M1", "description": "#10 THHN/THWN Cu", "quantity": 10, "unit": "FT"}, {"row_id": "M2", "description": "3/4\"C EMT", "quantity": 10, "unit": "FT"}]})
    by = rep.by_gate()
    assert by["unit_class"][0].target["line_id"] == "B1" and by["unit_class"][0].severity == "block"
    kinds = {f.target["line_id"]: (f.severity, f.target["item_class"]) for f in by["linear_in_ea"]}
    assert kinds["B2"] == ("block", "conductor")
    assert kinds["B3"][1] == "luminaire_linear:linear" and kinds["B4"][1] == "luminaire_linear:cove"
    # the golden share decides: a class at or above 80% FT blocks, below it warns; run evidence always blocks
    gs = G.GateSettings.load(settings, ft_share={"linear": 0.9, "cove": 0.1})
    rep = G.run_final(boq=lines, category_outputs=[_cat(rows)], settings=gs, measure=None)
    kinds = {f.target["line_id"]: f.severity for f in rep.by_gate()["linear_in_ea"]}
    assert kinds["B3"] == "block" and kinds["B4"] == "warn"
    rep = G.run_final(boq=lines, category_outputs=[_cat(rows)], settings=gs, linear_evidence={"L8"})
    assert {f.target["line_id"]: f.severity for f in rep.by_gate()["linear_in_ea"]}["B4"] == "block"
    assert all(v < G.LINEAR_BLOCK_SHARE for v in G.LINEAR_LUMINAIRE_FT_SHARE.values())
    assert G._effective_ft_share({"cove": (1, 0), "linear": (7, 41)}) == {"cove": 0.0, "linear": 0.1458}
    assert G.BLOCKING_FAMILIES == frozenset()
    assert G.WARN_FAMILIES == set(G.floors_mod.FAMILIES) - G.BLOCKING_FAMILIES
    assert G.RECOUNT_FAMILIES == {"demo_x", "circuit"}


def test_census_families_warn_unless_blocking(run7154, merged):
    # every known family that is not blocking warns (a warn-only family still warns)
    rep = G.run_final(boq=run7154["main"], main=run7154["main"], category_outputs=[merged], settings=SETTINGS)
    fams = {f.target["family"]: f.severity for f in rep.by_gate()["census_floor"]}
    assert fams == {"demo_x": "warn", "fixture_mark:L1": "warn", "fixture_mark:L3": "warn"}
    gs = G.GateSettings.load(SETTINGS, blocking_families=frozenset({"demo_x"}), warn_families=frozenset({"fixture_mark"}))
    rep = G.run_final(boq=run7154["main"], main=run7154["main"], category_outputs=[merged], settings=gs)
    fams = {f.target["family"]: f.severity for f in rep.by_gate()["census_floor"]}
    assert fams == {"demo_x": "block", "fixture_mark:L1": "warn", "fixture_mark:L3": "warn"}
    # a family in neither set raises nothing
    gs = G.GateSettings.load(SETTINGS, blocking_families=frozenset(), warn_families=frozenset({"circuit"}))
    rep = G.run_final(boq=run7154["main"], main=run7154["main"], category_outputs=[merged], settings=gs)
    assert "census_floor" not in rep.by_gate()
    checks = G.census_checks(G.GateContext(run_id=None, output_version=1, mode="legacy", route="human", settings=gs,
                                           lines=list(R.iter_rows(run7154["main"], kinds="B")), floors=G._coerce_floors(None, [merged])))
    assert {c["floor_obj"].key: c["status"] for c in checks if c["floor_obj"].sheet_number == "ED2.01"} == {"demo_x": "pass"}


# Pre-consolidation targets ------------------------------------------------------------------------


def test_pre_consolidation_returns_recount_targets_only(merged, elec_pages):
    targets = G.pre_consolidation([merged], settings=SETTINGS, page_facts=elec_pages)
    assert targets and all(set(t) >= {"target_id", "gate_id", "sheet_key", "family", "row_ids", "regions", "priority"} for t in targets)
    assert [t["target_id"] for t in targets] == [f"T{i}" for i in range(1, len(targets) + 1)]
    # v2 accounting puts 37 instances (the 3 track rows count) against the floor of 39,
    # inside the tolerance of 2: the recount on ED3.01 comes from its tile-subtotal rows
    assert not [t for t in targets if t["gate_id"] == "census_floor"]
    partial = [t for t in targets if t["gate_id"] == "partial_basis"]
    assert len(partial) == 1 and partial[0]["sheet_key"] == "D1P8" and partial[0]["family"] == "demo_x" and partial[0]["floor"] == 39
    rect = [r for r in merged["counts"] if "rectangular" in r["description"]][0]["row_id"]
    assert rect in partial[0]["row_ids"] and sum(r["contains"] for r in partial[0]["regions"]) == 39
    assert not [t for t in targets if t["sheet_key"] == "D1P7"]  # ED2.01 is whole
    # a tighter tolerance makes the census short and the target strong-first
    tight = dict(SETTINGS, plans_reader_census_tol_strong=0.0, plans_reader_census_tol_weak=0.0)
    targets = G.pre_consolidation([merged], settings=tight, page_facts=elec_pages)
    census = [t for t in targets if t["gate_id"] == "census_floor"]
    assert census[0]["sheet_key"] == "D1P8" and census[0]["family"] == "demo_x" and census[0]["floor"] == 39 and census[0]["accounted"] == 37
    assert census[0]["target_id"] == "T1" and sum(r["contains"] for r in census[0]["regions"]) == 39
    assert not [t for t in targets if t["family"].startswith("fixture_mark")]  # weak families not admitted by default
    assert all(t["gate_id"] in ("census_floor", "partial_basis") for t in targets)
    assert G.pre_consolidation([merged], settings=tight, page_facts=elec_pages, max_targets=1) == targets[:1]


# Persistence --------------------------------------------------------------------------------------


class _Query:
    def __init__(self, table: "_Table", op: str, payload=None):
        self.table, self.op, self.payload = table, op, payload
        self.filters: list[tuple[str, object]] = []

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def _match(self, row):
        return all(str(row.get(k)) == str(v) for k, v in self.filters)

    def execute(self):
        if self.op == "select":
            return type("R", (), {"data": [dict(r) for r in self.table.rows if self._match(r)]})()
        if self.op == "insert":
            assert all(v is not None for v in self.payload.values()), "inserts omit null values"
            row = dict(self.payload)
            row["id"] = f"id{len(self.table.rows) + 1}"
            row.setdefault("status", "open")
            row.setdefault("route", "none")
            self.table.rows.append(row)
            self.table.log.append(("insert", row["id"]))
            return type("R", (), {"data": [dict(row)]})()
        if self.op == "update":
            n = 0
            for r in self.table.rows:
                if self._match(r):
                    r.update(self.payload)
                    n += 1
            self.table.log.append(("update", dict(self.payload), n))
            return type("R", (), {"data": []})()
        raise AssertionError(self.op)


class _Table:
    def __init__(self):
        self.rows: list[dict] = []
        self.log: list = []

    def select(self, *_a):
        return _Query(self, "select")

    def insert(self, payload):
        return _Query(self, "insert", payload)

    def update(self, payload):
        return _Query(self, "update", payload)


class _FakeSb:
    def __init__(self):
        self.tables: dict[str, _Table] = {}

    def table(self, name):
        assert name == G.FINDINGS_TABLE
        return self.tables.setdefault(name, _Table())


def test_record_findings_upsert_keeps_human_status(legacy_report):
    sb = _FakeSb()
    run = {"id": legacy_report.run_id}
    assert isinstance(G.record_findings, RecordFindings)
    with pytest.raises(Exception):
        G.record_findings(sb, {"id": "11111111-1111-1111-1111-111111111111"}, legacy_report.findings, "final")
    stored = G.record_findings(sb, run, legacy_report.findings, "final")
    table = sb.tables[G.FINDINGS_TABLE]
    assert len(stored) == len(legacy_report.findings) == len(table.rows)
    assert all(r["run_id"] == run["id"] and r["stage"] == "final" and r["output_version"] == 1 for r in table.rows)
    assert all("resolved_by" not in r and "routed_group_id" not in r for r in table.rows)  # null columns omitted
    # a human resolves one and overrides another
    fps = {r["gate_id"]: r for r in table.rows if not (r["gate_id"] == "census_floor" and "demo_x" not in json.dumps(r["target"]))}
    fps["census_floor"].update({"status": "resolved", "resolved_by": "u1", "note": "recounted by hand"})
    fps["capability_denial"]["status"] = "overridden"
    # the second pass raises the same findings but one (new_devices_no_branch gone) plus a new one
    second = [f for f in legacy_report.findings if f.gate_id != "new_devices_no_branch"]
    extra = GateFinding(run_id=None, output_version=1, stage="pre", gate_id="orphan_rows", severity="block", route="model_fix", target={"row_ids": ["C9"]}, message="m")
    stored = G.record_findings(sb, run, second + [extra], "final")
    rows = {r["gate_id"]: r for r in table.rows if not (r["gate_id"] == "census_floor" and "demo_x" not in json.dumps(r["target"]))}
    assert rows["census_floor"]["status"] == "resolved" and rows["census_floor"]["note"] == "recounted by hand"
    assert rows["capability_denial"]["status"] == "overridden"
    assert rows["new_devices_no_branch"]["status"] == "superseded"
    assert rows["orphan_rows"]["stage"] == "final" and rows["orphan_rows"]["status"] == "open"
    assert len(table.rows) == len(legacy_report.findings) + 1  # upsert: no duplicate rows
    assert {f.gate_id for f in stored} == {f.gate_id for f in second} | {"orphan_rows"}
    assert [f for f in stored if f.gate_id == "census_floor" and f.target["family"] == "demo_x"][0].status == "resolved"
    # a superseded finding raised again reopens
    stored = G.record_findings(sb, run, legacy_report.findings, "final")
    assert rows["new_devices_no_branch"]["status"] == "open"
    # the table read and the pricing gate agree with the in-memory store
    mem = InMemoryFindingsStore()
    mem.record_findings(None, run, legacy_report.findings, "final")
    assert {f.fingerprint for f in G.findings_for_run(sb, run["id"])} == {f.fingerprint for f in mem.for_run(run["id"])} | {extra.fingerprint}
    allowed, reasons = G.pricing_allowed(run, sb=sb)
    assert allowed is False and "new_devices_no_branch" in reasons and "census_floor" not in reasons
    with pytest.raises(Exception):
        G.record_findings(sb, run, legacy_report.findings, "nope")


def test_pricing_allowed_rules(legacy_report):
    assert G.pricing_allowed({"id": "r"}) == (False, ["pricing_gates_unavailable"])
    assert G.pricing_allowed("r") == (False, ["pricing_gates_unavailable"])
    # never allowed by absence: a source that shows no final gate pass ever ran
    assert G.pricing_allowed({"id": "r1"}, sb=_FakeSb()) == (False, [G.GATES_NOT_RUN])
    assert G.pricing_allowed({"id": "r1"}, findings=[]) == (False, ["gates_not_run"])
    pricing_only = [GateFinding(run_id=None, output_version=1, stage="pricing", gate_id="uom_conflict_quote", severity="warn", route="human", target={"line_id": "B1"}, message="m")]
    assert G.pricing_allowed({"id": "r1"}, findings=pricing_only) == (False, ["gates_not_run"])
    # the gate_summary is the marker: a clean summary with an empty table allows; a blocking one refuses
    clean_run = {"id": "r1", "gate_summary": G.gate_summary_from_findings([], enforced=True)}
    assert G.pricing_allowed(clean_run, sb=_FakeSb()) == (True, [])
    blocked_run = {"id": "r1", "gate_summary": legacy_report.summary()}
    assert G.pricing_allowed(blocked_run, sb=_FakeSb())[0] is False
    clean = G.gate_summary_from_findings([], enforced=True)
    assert G.pricing_allowed({"gate_summary": clean}) == (True, [])
    warn_only = G.gate_summary_from_findings([f for f in legacy_report.findings if f.severity == "warn"], enforced=True)
    assert G.pricing_allowed({"gate_summary": warn_only}) == (True, [])
    not_enforced = G.gate_summary_from_findings(legacy_report.findings, enforced=False)
    allowed, reasons = G.pricing_allowed({"gate_summary": not_enforced})
    assert allowed is True and reasons and all(r.startswith("not_enforced:") for r in reasons)
    allowed, reasons = G.pricing_allowed({"id": "r"}, findings=legacy_report.findings)
    assert allowed is False and reasons == sorted({f.gate_id for f in legacy_report.findings if f.severity == "block"})
    resolved = [GateFinding.from_row(dict(f.to_row(), status="resolved")) for f in legacy_report.findings]
    assert G.pricing_allowed({"id": "r"}, findings=resolved) == (True, [])


# Golden checks verdict rule ------------------------------------------------------------------------


def _golden_checks_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "plans_reader_golden_checks.py"
    spec = importlib.util.spec_from_file_location("plans_reader_golden_checks_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_golden_check_counts_no_line_pairs_as_failures():
    gc = _golden_checks_module()

    def pair(status, no_line=False):
        return {"status": status, "no_estimator_line": no_line}

    # 16 passing pairs with a line and one no-line pair (floor 4, estimator 0): 16 of 17 = 94.1%, warn-only
    v = gc.family_verdict("demo_x", [pair("pass")] * 16 + [pair("fail", True)])
    assert (v["passed"], v["pairs"], v["pass_share"], v["verdict"]) == (16, 17, 0.9412, "warn_only")
    assert (v["passed_with_line"], v["pairs_with_line"], v["pass_share_with_line"]) == (16, 16, 1.0)
    assert v["no_estimator_line"] == 1 and v["no_estimator_line_failed"] == 1
    # a failing family is warn-only at any count (relocate_r 1 of 3 is not "unmeasured")
    assert gc.family_verdict("relocate_r", [pair("pass"), pair("fail"), pair("fail")])["verdict"] == "warn_only"
    # a passing family needs MIN_JUDGED_PAIRS to block; a weak family that passes warns
    assert gc.family_verdict("demo_x", [pair("pass")] * 3)["verdict"] == "unmeasured"
    assert gc.family_verdict("demo_x", [pair("pass")] * gc.MIN_JUDGED_PAIRS)["verdict"] == "blocking"
    assert gc.family_verdict("circuit", [pair("pass")] * gc.MIN_JUDGED_PAIRS)["verdict"] == "warn"
    assert gc.family_verdict("demo_x", [])["verdict"] == "unmeasured"


# Replay stage -------------------------------------------------------------------------------------


def test_replay_gate_stage(run7154, merged):
    G.register_replay_stage()  # another test may have reset the registry to the legacy entries
    spec = registered_stages()["gate"]
    assert spec.owner == "WS4" and not spec.legacy

    class _Ctx:
        bundle = type("B", (), {"run": {"id": run7154["run"]["id"]}})()
        settings_overrides = {"plans_reader_gates_blocking": True}

        def __init__(self):
            self.notes = []

        def note(self, text):
            self.notes.append(text)

    art = {"main": run7154["main"], "boq_spec": run7154["main"]["boq"], "category:electrical_drawings": merged,
           "facts": [honeywell_fact().to_dict()]}
    ctx = _Ctx()
    out = G.replay_gate(ctx, art)
    validate_gate_report(out["gate_report"])
    assert out["gate_report"]["blocking"] is True and out["gate_summary"]["mode"] == "legacy"
    assert out["moved_allowances"][0]["fact_id"] == "F1"
    assert any(n.startswith("gate: mode legacy") for n in ctx.notes)
    assert G.replay_gate(ctx, {}) == {}


def _controls_fact() -> Fact:
    return Fact(
        id="F4", system="hvac_controls", statement="ATC wiring by the temperature controls contractor.", party="other trade",
        verified=True, cite=FactCite(document_id="m", page=7, quote="ALL ATC WIRING BY TEMPERATURE CONTROLS CONTRACTOR."),
        kind="provided_by_others",
    )


def test_policy_inclusion_beats_an_excluding_fact_in_tools_mode():
    """Owner decision Q14 (the same precedence as scope_engine.fact_exclusions):
    a line a G3 policy inclusion carries is a fact_conflict warning for a
    human, never an excluded_system block. The first live WS4 iteration
    blocked the engine's own thermostat relocation line (B31) with
    excluded_system by F4. A line no inclusion carries still blocks."""
    rows = [_row("C53", "Relocate existing thermostat sensors", 5, number="M2.01"), _row("C60", "DDC controller", 2, number="M2.01")]
    lines = [
        _v2_line("B1", "RELOCATION OF THERMOSTAT TO NEW LOCATION", 5, "EA", ["C53"], sheet_refs=("M2.01",)),
        _v2_line("B2", "DDC CONTROLLER", 2, "EA", ["C60"], sheet_refs=("M2.01",)),
    ]
    main = {"brief": {"systems": []}, "estimate": {"extra_allowances": []}}
    plain = G.run_final(boq=lines, main=main, category_outputs=[_cat(rows)], facts=[_controls_fact()], settings=SETTINGS, mode="tools")
    assert {f.target["line_id"] for f in plain.by_gate()["excluded_system"]} == {"B1", "B2"}
    ctx = {"inclusions": [{"rule_id": "IN-THERMOSTAT-RELOC", "trigger_family": "thermostat", "pattern": r"\b(?:thermostats?|t-?stats?)\b",
                           "description": "RELOCATION OF THERMOSTAT TO NEW LOCATION", "unit": "EA"}]}
    matcher = G.policy_include_matcher(ctx, policy={"rules": []})
    assert matcher(lines[0]) == "IN-THERMOSTAT-RELOC" and matcher(lines[1]) is None
    assert matcher({"description": "ANY LINE", "scope_rule": "IN-X"}) == "IN-X"
    rep = G.run_final(boq=lines, main=main, category_outputs=[_cat(rows)], facts=[_controls_fact()], settings=SETTINGS, mode="tools",
                      policy_includes=matcher)
    by = rep.by_gate()
    assert [f.target["line_id"] for f in by["excluded_system"]] == ["B2"]  # not loosened for a line no inclusion carries
    conflict = by["fact_conflict"][0]
    assert conflict.severity == "warn" and conflict.target == {"line_id": "B1", "fact_id": "F4", "system": "building_automation", "rule_id": "IN-THERMOSTAT-RELOC"}
    assert [e.line_id for e in rep.excluded] == ["B2"]
    # an engine insert names its rule itself; the seed policy's include rule carries the line with no context at all
    inserted = [dict(lines[0], scope_rule="IN-THERMOSTAT-RELOC")]
    rep2 = G.run_final(boq=inserted, main=main, category_outputs=[_cat(rows)], facts=[_controls_fact()], settings=SETTINGS, mode="tools")
    assert "excluded_system" not in rep2.by_gate() and rep2.by_gate()["fact_conflict"][0].target["rule_id"] == "IN-THERMOSTAT-RELOC"
    assert G.policy_include_matcher(None)(lines[0]) == "IN-THERMOSTATS"
    assert G.policy_include_matcher(None)({"description": "DUPLEX RECEPTACLE"}) is None


def test_split_pair_is_one_consumer_in_tools_mode():
    """DEV-TR on 7154 run 2: B8 (28 EA) and its split B41 (3 EA, TR) both
    cite C34 (31 EA). Linked from both sides they consume the row once (no
    double_consumption, no unsourced_line) and the arithmetic gate checks
    28 + 3 = 31; a pair that does not add up blocks; a one-sided or missing
    marker is still a double consumption."""
    rows = [_row("C34", "Duplex receptacle, 125V, emergency circuit", 31)]
    main = {"brief": {"project_summary": "A pharmacy remodel", "systems": []}, "estimate": {"extra_allowances": []}}

    def pair(parent_qty=28, child_qty=3, **child_over):
        parent = _v2_line("B8", "DUPLEX RECEPTACLE", parent_qty, "EA", ["C34"], combine="single",
                          split_to=[{"line_id": "B41", "quantity": child_qty}])
        child = _v2_line("B41", "DUPLEX RECEPTACLE, TR", child_qty, "EA", ["C34"], combine="split", split_of="B8", scope_rule="DEV-TR")
        child.update(child_over)
        return [parent, child]

    rep = G.run_final(boq=pair(), main=main, category_outputs=[_cat(rows)], settings=SETTINGS, route="model_fix")
    by = rep.by_gate()
    assert rep.mode == "tools"
    assert not ({"double_consumption", "unsourced_line", "arithmetic", "unverifiable_citation", "orphan_rows"} & set(by))
    # run 2 before the fix: the parent kept 31 while the split line held 3
    rep = G.run_final(boq=pair(parent_qty=31), main=main, category_outputs=[_cat(rows)], settings=SETTINGS, route="model_fix")
    by = rep.by_gate()
    assert "double_consumption" not in by
    assert [f.target["line_id"] for f in by["arithmetic"]] == ["B8"] and by["arithmetic"][0].evidence["computed"] == 28
    # no markers at all: two lines on one row (the B4 case above)
    plain = pair()
    plain[0].pop("split_to")
    plain[1].update({"combine": "single", "split_of": None})
    by = G.run_final(boq=plain, main=main, category_outputs=[_cat(rows)], settings=SETTINGS, route="model_fix").by_gate()
    assert by["double_consumption"][0].target == {"ref": "C34", "row_ids": ["B41", "B8"]}
    # a marker on the split line only: not a pair; the recheck names the missing link
    half = pair()
    half[0].pop("split_to")
    by = G.run_final(boq=half, main=main, category_outputs=[_cat(rows)], settings=SETTINGS, route="model_fix").by_gate()
    assert by["double_consumption"][0].target["ref"] == "C34"
    assert {f.target["line_id"] for f in by["arithmetic"]} == {"B8", "B41"}
    assert any("not listed in the split_to" in f.message for f in by["arithmetic"])
