"""plans_reader_review_rules (the $0 review lane) and the record_finding tool
(build spec WS7 wave 2): WS7-unique rules R1, R3, R7, R8, R12, R13, R14, the
measured-quantity floor, the Verify-basis invariants, the coverage rule, the
consumption of WS4 gate findings and WS5 decisions, and the rules on the
7154 replay. Synthetic prices only; the golden R3 population test reads the
backup and is marked golden."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import plans_reader_review_rules as rr
from app.services import plans_reader_review_tools as rtools
from app.services.estimating_contracts import GateFinding
from app.services.plans_reader_bands import BandCheck
from app.services.plans_reader_tools import (
    REGISTRY,
    FakeMeasureService,
    FakePriceService,
    ToolContext,
    dispatch,
    offered_tools,
)

from tests.plans_reader_fixtures import ELEC_DOC_7154, FIXTURE_7154, load_7154
from tests.ws7_fake_sb import FakeSB

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")


# Helpers ---------------------------------------------------------------------------------


def _boq(items_by_group: dict[tuple[str, str, str], list[dict]], area: str = "BASE BID") -> dict:
    """A BOQ spec: {(section, group, subgroup): [items]}."""
    sections: dict[str, dict] = {}
    for (section, group, subgroup), items in items_by_group.items():
        sec = sections.setdefault(section, {"heading": section, "groups": []})
        grp = next((g for g in sec["groups"] if g["name"] == group), None)
        if grp is None:
            grp = {"name": group, "subgroups": []}
            sec["groups"].append(grp)
        grp["subgroups"].append({"name": subgroup, "items": items})
    return {"areas": [{"area": area, "sections": list(sections.values())}]}


def _item(desc, qty, unit="EA", **extra):
    return {"description": desc, "quantity": qty, "unit": unit, "sheet_refs": extra.pop("sheet_refs", ["E2.01"]),
            **extra}


NEW = "NEW WORK"


def _devices(n=24):
    return {(NEW, "OTHER ITEMS", "RECEPTACLES"): [_item("DUPLEX RECEPTACLE", n)]}


@pytest.fixture(scope="module")
def run7154() -> dict:
    return load_7154()


@pytest.fixture(scope="module")
def facts7154() -> list[dict]:
    data = json.loads((FIXTURE_7154 / "page_facts_elec.json").read_text())
    out = []
    for doc in data["documents"]:
        f = dict(doc["facts"])
        f["document_id"] = ELEC_DOC_7154 if doc["key"] == "elec" else "mech-doc"
        out.append(f)
    return out


def _inputs7154(run7154, facts7154, **kw) -> rr.ReviewInputs:
    return rr.ReviewInputs(
        run_id="run-7154", boq=run7154["deliverables"]["boq"],
        category_outputs=[run7154["electrical"], run7154["rfp"], run7154["specifications"]],
        page_facts=facts7154, **kw,
    )


# 7154 replay -----------------------------------------------------------------------------


def test_rules_on_the_7154_replay_with_local_copies(run7154, facts7154):
    res = rr.run_rules(_inputs7154(run7154, facts7154, local_rules_fallback=True))
    got = {(f.rule_id, tuple(f.line_ids), f.severity) for f in res.findings}
    assert ("R1", (), "critical") in got  # new devices, no branch wire (E2)
    assert ("R3", ("B8",), "critical") in got  # lighting track rows in EA; local linear_in_ea fired
    assert ("R3", ("B35",), "warning") in got  # L5 runs in EA, schedule per foot, WS4 not fired
    assert not any(f.rule_id == "R3" and "B9" in f.line_ids for f in res.findings)  # heads excluded
    assert ("partial_basis", ("B6",), "critical") in got and ("partial_basis", ("B7",), "critical") in got
    assert ("MQ1", (), "warning") in got  # 4 new circuits on 1EELC x 75 FT floor vs 0 FT
    r14 = [f for f in res.findings if f.rule_id == "R14"]
    assert any("L4 control = LOCAL" in f.title for f in r14)  # E19: the RFI the text layer answers
    assert all(f.severity == "info" and f.disposition == "info_only" for f in r14)
    assert not any(f.rule_id in ("CV1", "CV2") for f in res.findings)
    assert [f.finding_id for f in res.findings] == [f"RF{i}" for i in range(1, len(res.findings) + 1)]
    assert res.findings[0].severity == "critical"
    assert res.stats["by_severity"] == {"critical": 4, "warning": 2, "info": 2}


def test_7154_ws4_typed_gate_input_makes_r3_critical_and_is_not_repeated(run7154, facts7154):
    gate = GateFinding(run_id="run-7154", output_version=1, stage="final", gate_id="linear_in_ea",
                       severity="block", route="human", target={"line_id": "B8"}, message="track in EA")
    res = rr.run_rules(_inputs7154(run7154, facts7154, gate_findings=[gate], local_rules_fallback=False))
    r3 = [f for f in res.findings if f.rule_id == "R3" and f.line_ids == ["B8"]]
    assert len(r3) == 1 and r3[0].severity == "critical"
    assert any(e["rule_id"] == "gate:linear_in_ea" for e in r3[0].evidence)
    assert not any(f.origin == "gate" and f.rule_id == "linear_in_ea" for f in res.findings)
    # with no WS4 input and the fallback off, R3 stays a warning
    bare = rr.run_rules(_inputs7154(run7154, facts7154, local_rules_fallback=False))
    assert [f.severity for f in bare.findings if f.rule_id == "R3" and f.line_ids == ["B8"]] == ["warning"]
    assert not any(f.origin == "gate" for f in bare.findings)
    assert "WS4 gates absent and the local fallback is off" in bare.notes


def test_r6_breakers_are_left_to_ws5(run7154, facts7154):
    res = rr.run_rules(_inputs7154(run7154, facts7154, local_rules_fallback=True))
    assert not any(f.rule_id == "R6" for f in res.findings)
    assert not any(set(f.line_ids) & {"B12", "B13", "B14", "B15", "B16"} for f in res.findings)


# Single rules ----------------------------------------------------------------------------


def test_r1_needs_twenty_devices_and_zero_branch_wire():
    assert rr.run_rules(rr.ReviewInputs(boq=_boq(_devices(19)))).by_rule("R1") == []
    assert len(rr.run_rules(rr.ReviewInputs(boq=_boq(_devices(24)))).by_rule("R1")) == 1
    wired = _devices(24)
    wired[(NEW, "CONDUCTORS", "CONDUCTORS")] = [_item('#12 THHN CU', 900, "FT")]
    assert rr.run_rules(rr.ReviewInputs(boq=_boq(wired))).by_rule("R1") == []


def test_r1_subsumes_the_ws4_new_devices_gate():
    gate = GateFinding(run_id=None, output_version=1, stage="final", gate_id="new_devices_no_branch",
                       severity="block", target={}, message="no branch")
    res = rr.run_rules(rr.ReviewInputs(boq=_boq(_devices()), gate_findings=[gate]))
    assert len(res.by_rule("R1")) == 1
    assert any(e["rule_id"] == "gate:new_devices_no_branch" for e in res.by_rule("R1")[0].evidence)
    assert res.by_rule("new_devices_no_branch") == []


@pytest.mark.parametrize(("desc", "fires"), [
    ("LED TAPE LIGHT 24V", True),
    ("CONTINUOUS ROW LED STRIP FIXTURE", True),
    ("UNDERCABINET LIGHT RUN", True),
    ("LIGHTING TRACK", True),
    ("TRACK HEAD, 12W", False),
    ("4FT LED STRIP", False),
    ("LINEAR PENDANT 8' - 0\"", False),
    ("END CAP FOR LINEAR FIXTURE", False),
    ("REFLECTIVE TAPE TO POST", False),
    ("1\" GRC TAPE WRAPPED", False),
    ("DUPLEX RECEPTACLE", False),
])
def test_r3_trigger_vocabulary(desc, fires):
    line = {"line_id": "B1", "description": desc, "unit": "EA", "quantity": 4}
    assert (rr.r3_trigger(line, {}) is not None) is fires


def test_r3_per_foot_schedule_and_quote_evidence():
    boq = _boq({(NEW, "LIGHTING FIXTURES", "FIXTURES"): [_item("L9, LED COVE FIXTURE", 6), _item("L2, TROFFER", 6)]})
    facts = [{"fixture_types": [{"tag": "L9", "linear_hint": True}, {"tag": "L2", "linear_hint": False}]}]
    res = rr.run_rules(rr.ReviewInputs(boq=boq, page_facts=facts))
    assert [f.line_ids for f in res.by_rule("R3")] == [["B1"]]
    res = rr.run_rules(rr.ReviewInputs(boq=boq, per_foot_quote_line_ids=["B2"]))
    assert [f.line_ids for f in res.by_rule("R3")] == [["B2"]]
    assert res.by_rule("R3")[0].severity == "warning"


def test_r7_wage_from_ws5_bid_conditions():
    priced = {"wage_type": "non_prevailing_wage", "totals": {}}
    bc = {"wage_type": "prevailing_wage", "wage_basis": "document_cue", "needs_review": [],
          "cues": [{"cue": "nrs338", "weight": 3, "document_id": "d1", "page": 4, "quote": "per NRS 338"}]}
    res = rr.run_rules(rr.ReviewInputs(priced=priced, bid_conditions=bc))
    r7 = res.by_rule("R7")
    assert len(r7) == 1 and r7[0].severity == "critical" and r7[0].kind == "bid_condition"
    assert r7[0].evidence[0]["type"] == "page_quote" and "NRS 338" in r7[0].evidence[0]["quote"]
    same = rr.run_rules(rr.ReviewInputs(priced={"wage_type": "prevailing_wage"}, bid_conditions=bc))
    assert same.by_rule("R7") == []
    unknown = dict(bc, cues=[], needs_review=["wage_unknown"])
    got = rr.run_rules(rr.ReviewInputs(priced={"wage_type": "prevailing_wage"}, bid_conditions=unknown))
    assert [f.severity for f in got.by_rule("R7")] == ["warning"]


def test_r7_local_text_search_only_behind_the_flag():
    facts = [{"document_id": "d1", "pages": [{"page": 3}]}]
    text = {("d1", 3): "This is a public works project subject to NRS 338 prevailing wages."}
    inp = dict(priced={"wage_type": "non_prevailing_wage"}, page_facts=facts,
               page_text=lambda d, p: text.get((d, p)))
    assert rr.run_rules(rr.ReviewInputs(**inp, local_rules_fallback=False)).by_rule("R7") == []
    assert len(rr.run_rules(rr.ReviewInputs(**inp, local_rules_fallback=True)).by_rule("R7")) == 1


def test_r8_only_positive_or_non_template_allowances():
    priced = {"allowances": [
        {"name": "ARC FLASH STUDY", "amount_usd": 0},
        {"name": "TESTING AND COMMISSIONING", "amount_usd": 1500},
        {"name": "TEMPORARY POWER", "amount_usd": 0},
        {"name": "OWNER ALLOWANCE", "amount_usd": 0},
    ]}
    res = rr.run_rules(rr.ReviewInputs(priced=priced))
    by_name = {f.title.split(": ", 1)[1]: f for f in res.by_rule("R8")}
    assert "ARC FLASH STUDY" not in by_name
    assert by_name["TEMPORARY POWER"].severity == "warning"
    assert by_name["TEMPORARY POWER"].kind == "excluded_scope_carried"
    # A positive COMMISSIONING allowance is an E5 house-excluded topic.
    assert by_name["TESTING AND COMMISSIONING"].severity == "warning"
    assert by_name["TESTING AND COMMISSIONING"].kind == "excluded_scope_carried"
    assert by_name["OWNER ALLOWANCE"].severity == "info"


def test_r8_grades_an_allowance_ws5_removed_as_excluded():
    priced = {"allowances": [{"name": "SPECIAL INSPECTION SUPPORT", "amount_usd": 900}]}
    report = {"decisions": [{"key": "allowance:SPECIAL INSPECTION SUPPORT", "rule_id": None,
                             "action": "remove_allowance", "subject": "SPECIAL INSPECTION SUPPORT"}]}
    got = rr.run_rules(rr.ReviewInputs(priced=priced, scope_report=report)).by_rule("R8")
    assert [(f.severity, f.kind) for f in got] == [("warning", "excluded_scope_carried")]
    plain = rr.run_rules(rr.ReviewInputs(priced=priced)).by_rule("R8")
    assert [f.severity for f in plain] == ["info"]


E5_TOPIC_NAMES = (
    "TEMPORARY POWER", "TEMP POWER", "TEMPORARY DISTRIBUTION AND LOCALLY SWITCHED LIGHTING", "TEMPORARY LIGHTING",
    "TEMPORARY HEPA POWER AND EMERGENCY SHUTOFF", "HEPA", "COORDINATION STUDY", "COMMISSIONING",
    "TESTING AND COMMISSIONING", "PATCH", "ELECTRICAL CUTTING, PATCHING AND FINISH RESTORATION", "FIRESTOP",
    "FIRESTOPPING", "RATED BOX PADS AND FIRESTOP SYSTEM LABELING", "BIM AND INTERTRADE COORDINATION",
    "FACTORY-AUTHORIZED OWNER TRAINING AND RECORDING", "OCCUPIED-HOSPITAL PHASING AND OUTAGE SUPPORT",
    "PHASING", "ANCILLARY", "LIGHTING AND RECEPTACLE CONTROL ANCILLARY EQUIPMENT",
)


def test_house_excluded_regex_covers_the_e5_topics_and_the_ws5_seed_rules():
    from app.services import scope_policy
    from app.services.scope_policy_seed import SEED_POLICY

    assert [n for n in E5_TOPIC_NAMES if not rr.HOUSE_EXCLUDED_RE.search(n)] == []
    # Every name a WS5 house-topic seed rule matches is matched locally too.
    house_rules = {"EX-TEMP-POWER", "EX-CUT-PATCH-PAINT", "EX-FIRESTOP-LABELS", "EX-FIRESTOPPING", "EX-BIM",
                   "EX-TRAINING", "EX-COMMISSIONING", "EX-PHASING-PREMIUM", "EX-ANCILLARY"}
    rules = {r["rule_id"]: r for r in SEED_POLICY["rules"] if r["rule_id"] in house_rules}
    assert set(rules) == house_rules
    for name in E5_TOPIC_NAMES:
        if any(scope_policy.match_rule(r, name) for r in rules.values()):
            assert rr.HOUSE_EXCLUDED_RE.search(name), name
    for rid, r in rules.items():
        assert any(scope_policy.match_rule(r, n) for n in E5_TOPIC_NAMES), rid
    assert not rr.HOUSE_EXCLUDED_RE.search("ARC FLASH STUDY")
    assert not rr.HOUSE_EXCLUDED_RE.search("TEMPERATURE SENSOR")


def test_r12_only_quote_or_manual_lines():
    price = FakePriceService({
        "led troffer": {"uom": "EA"}, "duplex receptacle": {"uom": "EA"},
    })

    class Lib(FakePriceService):
        def lookup(self, query):
            out = super().lookup(query)
            if out["status"] == "ok":
                out["labor_hours"] = {"median": 1.0, "p25": 0.8, "p75": 1.2, "n_projects": 9}
                out["material"] = {"median": 100.0, "p25": 80.0, "p75": 120.0, "n_projects": 9}
            return out

    lib = Lib(price._table)
    lines = [
        {"line_id": "B1", "description": "LED TROFFER", "unit": "EA", "qty": 10, "priced": True,
         "unit_hours": 1.0, "unit_material_usd": 400.0, "material_source": "vendor_quote", "hours_source": "library"},
        {"line_id": "B2", "description": "DUPLEX RECEPTACLE", "unit": "EA", "qty": 10, "priced": True,
         "unit_hours": 5.0, "unit_material_usd": 100.0, "material_source": "library", "hours_source": "library"},
        {"line_id": "B3", "description": "DUPLEX RECEPTACLE", "unit": "EA", "qty": 10, "priced": True,
         "unit_hours": 5.0, "unit_material_usd": 100.0, "material_source": "library", "hours_source": "manual"},
    ]
    res = rr.run_rules(rr.ReviewInputs(priced={"lines": lines}, price=lib))
    got = {(f.line_ids[0], f.title.split(" outside")[0]) for f in res.by_rule("R12")}
    assert got == {("B1", "unit material"), ("B3", "unit hours")}
    assert all(f.severity == "warning" for f in res.by_rule("R12"))
    b1 = next(f for f in res.by_rule("R12") if f.line_ids == ["B1"])
    assert b1.impact_usd == pytest.approx(3000.0)
    assert b1.to_dict(include_money=False)["impact_usd"] is None


def test_r13_duplicates_across_areas_with_shared_sheets():
    spec = _boq({(NEW, "OTHER ITEMS", "MISCELLANEOUS"): [_item("VAV POWER CONNECTION", 2, sheet_refs=["E2.01"])]})
    spec["areas"].append(_boq({(NEW, "OTHER ITEMS", "MISCELLANEOUS"): [
        _item("VAV power connection", 2, sheet_refs=["E2.01", "M2.01"])]}, area="ALT 1")["areas"][0])
    res = rr.run_rules(rr.ReviewInputs(boq=spec))
    assert [f.line_ids for f in res.by_rule("R13")] == [["B1", "B2"]]
    spec["areas"][1]["sections"][0]["groups"][0]["subgroups"][0]["items"][0]["sheet_refs"] = ["E3.01"]
    assert rr.run_rules(rr.ReviewInputs(boq=spec)).by_rule("R13") == []


def test_r14_uses_page_text_when_facts_lack_the_tag():
    q = [{"question": "Confirm the control method for L7.", "document_id": "d1", "page": 5}]
    text = {("d1", 5): "TYPE DESCRIPTION CONTROL\nL7 TASK LIGHT LOCAL\n"}
    res = rr.run_rules(rr.ReviewInputs(questions=q, page_text=lambda d, p: text.get((d, p))))
    assert len(res.by_rule("R14")) == 1
    assert rr.run_rules(rr.ReviewInputs(questions=q)).by_rule("R14") == []


def test_measured_floor_and_route_rerun():
    spec = _devices()
    spec[(NEW, "CONDUCTORS", "CONDUCTORS")] = [_item("#12 THHN CU", 100, "FT")]

    class Routes(FakeMeasureService):
        def synthesize_routes(self, document_id=None, page=None, circuits=None):
            return {"routes": [], "totals": {"#12": 900.0}, "caveats": []}

    res = rr.run_rules(rr.ReviewInputs(boq=_boq(spec), circuits=6, measure_service=Routes(), existing_tie_in_ft=25))
    assert [f.rule_id for f in res.findings if f.rule_id.startswith("MQ")] == ["MQ1", "MQ2"]
    ok = rr.run_rules(rr.ReviewInputs(boq=_boq(spec), circuits=1, existing_tie_in_ft=25))
    assert ok.by_rule("MQ1") == []  # 100 FT >= 75% of 75 FT


def _priced(**over):
    doc = {
        "wage_type": "prevailing_wage", "labor_rate_usd": 100.0,
        "lines": [{"line_id": "B1", "description": "DUPLEX RECEPTACLE", "unit": "EA", "qty": 10, "priced": True,
                   "unit_hours": 0.5, "unit_material_usd": 10.0, "material_source": "library"}],
        "quotes": [], "allowances": [],
        "totals": {"hours_raw": 100.0, "hours": 125.0, "tax_rate_pct": 10.0, "material_usd": 110.0,
                   "verify": {"labor_amount": 12500.0, "materials_amount": 110.0, "gear_amount": None,
                              "underground_amount": None, "low_voltage_amount": None}},
    }
    doc.update(over)
    return doc


def test_verify_invariants_clean_draft_passes():
    res = rr.run_rules(rr.ReviewInputs(priced=_priced(), bid_conditions={"wage_type": "prevailing_wage"},
                                       rate_table={"prevailing_wage": 100.0}))
    assert [f.rule_id for f in res.findings if f.rule_id.startswith("V")] == []


def test_verify_invariants_catch_each_break():
    rate = rr.run_rules(rr.ReviewInputs(priced=_priced(), rate_table={"prevailing_wage": 120.0}))
    assert [f.rule_id for f in rate.findings] == ["V1"]
    wage = rr.run_rules(rr.ReviewInputs(priced=_priced(), bid_conditions={"wage_type": "non_prevailing_wage"}))
    assert "V1" in {f.rule_id for f in wage.findings}
    p = _priced()
    p["totals"] = dict(p["totals"], hours=90.0)
    assert {"V2"} <= {f.rule_id for f in rr.run_rules(rr.ReviewInputs(priced=p)).findings}
    p = _priced()
    p["totals"] = dict(p["totals"], material_usd=100.0)
    assert [f.rule_id for f in rr.run_rules(rr.ReviewInputs(priced=p)).findings] == ["V3"]
    cats = {"General Material": {"section": "materials", "pre_tax": 100.0, "total": 100.0, "taxable": True,
                                 "tax_included": False, "source": "library"},
            "Switchgear": {"section": "gear", "pre_tax": 500.0, "total": 550.0, "taxable": True,
                           "tax_included": False, "source": "library"}}
    got = {f.rule_id for f in rr.run_rules(rr.ReviewInputs(priced=_priced(), pricing_categories=cats)).findings}
    assert got == {"V3", "V4"}  # general material untaxed; gear section never reached Verify
    q = _priced(quotes=[{"category": "Lighting", "amount_usd": 5000.0, "tax_included": True,
                         "replaces_sections": ["materials"]}])
    assert "V5" in {f.rule_id for f in rr.run_rules(rr.ReviewInputs(priced=q)).findings}


def test_coverage_rule_fixture_types_panels_equipment_and_zero_yield_sheets():
    boq = _boq({(NEW, "LIGHTING FIXTURES", "FIXTURES"): [_item("L1, TROFFER", 4, sheet_refs=["E3.01"])]})
    facts = [{"fixture_types": [{"tag": "L1"}, {"tag": "L2", "description": "DOWNLIGHT"}],
              "panels": [{"name": "LA", "new_circuits": [1, 3]}, {"name": "HB", "existing": True, "new_circuits": []}],
              "pages": [{"sheet_number": "E3.01", "page_role": "plan"}, {"sheet_number": "E4.01", "page_role": "plan"},
                        {"sheet_number": "M2.01", "page_role": "plan"}]}]
    res = rr.run_rules(rr.ReviewInputs(boq=boq, page_facts=facts, equipment_marks=["EF-1"]))
    titles = sorted(f.title for f in res.findings if f.rule_id in ("CV1", "CV2"))
    assert titles == ["Electrical plan sheet E4.01 yielded nothing", "Equipment EF-1 has no BOQ line",
                      "Fixture type L2 has no BOQ line", "Panel LA has new work and no BOQ line"]


def test_scope_decisions_are_consumed_as_typed_input():
    boq = _boq({(NEW, "OTHER ITEMS", "MISCELLANEOUS"): [_item("FIRE ALARM DEVICE", 12)]})
    report = {"decisions": [{"key": "fa:1", "rule_id": "EX-FIRE-ALARM", "action": "exclude", "subject": "B1",
                             "clarification": "Fire alarm by others"}], "needs_review": ["wage_unknown"]}
    res = rr.run_rules(rr.ReviewInputs(boq=boq, scope_report=report))
    scope = [f for f in res.findings if f.origin == "scope"]
    assert {(f.severity, tuple(f.line_ids)) for f in scope} == {("critical", ("B1",)), ("info", ())}
    report["decisions"][0]["action"] = "include"
    assert [f.severity for f in rr.run_rules(rr.ReviewInputs(boq=boq, scope_report=report)).findings
            if f.origin == "scope"] == ["info"]


def test_scope_decisions_from_a_real_ws5_enforce_report():
    """A real scope_engine.enforce ScopeReport: the decision names its line
    only in `key` (rule:<rule_id>:<line_id>), `subject` is the description.
    The same line still carried in the final BOQ is critical."""
    import copy as _copy

    from app.services import scope_engine as se
    from app.services.scope_policy_seed import SEED_POLICY
    from tests.test_scope_engine import _ctx, _main, _settings

    main = _main()
    sub = next(g["subgroups"][0] for a in main["boq"]["areas"] for sec in a["sections"] for g in sec["groups"]
               if g["name"] == "OTHER ITEMS")
    extra = _copy.deepcopy(sub["items"][0])
    extra.update(line_id="B90", description="TEMPORARY POWER AND LIGHTING", quantity=1, unit="LS")
    sub["items"].append(extra)
    out = se.enforce(main, _ctx(), SEED_POLICY, (), settings=_settings())
    report = out["scope_policy"]
    decision = next(d for d in report["decisions"] if d["key"] == "rule:EX-TEMP-POWER:B90")
    assert decision["subject"] == "TEMPORARY POWER AND LIGHTING" and "line_ids" not in decision
    assert rr.decision_line_ids(decision) == ["B90"]
    carried = [{"line_id": "B90", "description": "TEMPORARY POWER AND LIGHTING", "unit": "LS", "quantity": 1}]
    got = rr.scope_review_findings(report, carried)
    crit = [f for f in got if f.severity == "critical"]
    assert [(f.kind, f.line_ids, f.rule_id) for f in crit] == [("excluded_scope_carried", ["B90"], "EX-TEMP-POWER")]
    # The enforced BOQ no longer carries it: nothing critical.
    assert not [f for f in rr.scope_review_findings(report, [{"line_id": "B1"}]) if f.severity == "critical"]


def test_local_scope_copies_only_behind_the_flag():
    boq = _boq({(NEW, "OTHER ITEMS", "MISCELLANEOUS"): [_item("TEMPORARY POWER FOR TRADES", 1, unit="LS")]})
    assert not [f for f in rr.run_rules(rr.ReviewInputs(boq=boq)).findings if f.origin == "scope"]
    got = rr.run_rules(rr.ReviewInputs(boq=boq, local_rules_fallback=True))
    assert [f.rule_id for f in got.findings if f.origin == "scope"] == ["L-HOUSE"]


def test_band_checks_become_band_findings():
    check = BandCheck(metric="branch_wire_ft_per_device", value=130.0, stratum="building", n=29, p10=22.0,
                      p25=30.0, p50=46.0, p75=60.0, p90=99.0, position="above", percentile=96.0,
                      severity="warning", level="component", line_ids=("B4",))
    res = rr.run_rules(rr.ReviewInputs(band_checks=[check]))
    assert [(f.origin, f.kind, f.severity, f.line_ids) for f in res.findings] == [
        ("band", "band_outlier", "warning", ["B4"])]


def test_finding_rows_omit_nulls_and_round_trip():
    f = rr.ReviewFinding(origin="rule", rule_id="R13", kind="double_count", severity="warning",
                         title="dup \u2014 check", line_ids=["B2", "B10", "X1"])
    assert f.title == "dup - check" and f.line_ids == ["B2", "B10"]
    rr.number_findings([f])
    row = f.to_row(review_id="rev-1", run_id="run-1")
    assert row["finding_id"] == "RF1" and "impact_usd" not in row and "disposition_reason" not in row
    back = rr.ReviewFinding.from_dict(f.to_dict())
    assert back.fingerprint == f.fingerprint and back.line_ids == f.line_ids
    sb = FakeSB()
    sb.table("plans_reader_review_findings").insert([row]).execute()
    assert sb.tables["plans_reader_review_findings"][0]["severity"] == "warning"


# Replay stage ----------------------------------------------------------------------------


def test_replay_stage_review_reruns_the_rules_lane(run7154, facts7154):
    from app.services import plans_reader_replay as replay

    rr.register_replay_stage()
    spec = replay.stage("review")
    assert spec.owner == "WS7" and not spec.legacy and not spec.llm
    notes: list[str] = []
    ctx = SimpleNamespace(note=notes.append)
    art = {"boq_spec": run7154["deliverables"]["boq"], "category:electrical_drawings": run7154["electrical"],
           "gate_report": {"schema": "bdr.gate_report/1", "blocking": True,
                           "checks": [{"code": "linear_in_ea", "severity": "block", "detail": "track", "rows": ["B8"]}]},
           "page_facts": facts7154,
           "review": {"verdict": "advisory", "findings": [{"origin": "reviewer", "title": "stored"}]}}
    with replay.no_provider_calls():
        out = spec.fn(ctx, art)["review_rules"]
    assert out["schema"] == rr.REVIEW_RULES_SCHEMA
    r3 = [f for f in out["findings"] if f["rule_id"] == "R3" and f["line_ids"] == ["B8"]]
    assert r3 and r3[0]["severity"] == "critical"
    assert out["reviewer_findings_stored"] == [{"origin": "reviewer", "title": "stored"}]


# record_finding --------------------------------------------------------------------------


def test_record_finding_is_registered_for_review_groups_only():
    spec = rtools.ensure_registered()
    assert REGISTRY["record_finding"] is spec
    assert spec.mode == "review" and spec.kinds == frozenset({"review"}) and spec.cost_class == "free"
    assert "record_finding" in {s.name for s in offered_tools("review", mode="review")}
    assert "record_finding" not in {s.name for s in offered_tools("review", mode="read")}
    assert "record_finding" not in {s.name for s in offered_tools("recount")}
    assert rtools.ensure_registered() is spec  # idempotent


def test_record_finding_keeps_severity_only_with_ledger_evidence():
    rtools.ensure_registered()
    ctx = ToolContext(run_id="run-1", kind="review", group_id="grp-1", ledger_refs={"t:abcd1234:5:12"})
    ok = dispatch(REGISTRY["record_finding"], ctx, {
        "severity": "critical", "line_ids": ["B34", "B99x"], "claim": "L4 control is LOCAL per the schedule",
        "evidence": ["t:abcd1234:5:12", "t:ffff0000:1:1"], "suggested_fix": None})
    assert not ok.refused and ok.data["severity"] == "critical" and ok.data["ref"] == "f:RV1"
    assert ok.data["dropped"] == {"line_ids": ["B99x"], "evidence": ["t:ffff0000:1:1"]}
    assert "f:RV1" in ctx.ledger_refs
    weak = dispatch(REGISTRY["record_finding"], ctx, {
        "severity": "critical", "line_ids": ["B2"], "claim": "Quantity looks low for the area",
        "evidence": [], "suggested_fix": "recount"})
    assert weak.data["severity"] == "info" and weak.data["finding_id"] == "RV2"
    bad = dispatch(REGISTRY["record_finding"], ctx, {
        "severity": "warning", "line_ids": ["X7"], "claim": "something is off here", "evidence": [],
        "suggested_fix": None})
    assert bad.refused and bad.code == "unknown_lines"
    invalid = dispatch(REGISTRY["record_finding"], ctx, {"severity": "fatal", "line_ids": [], "claim": "x",
                                                         "evidence": [], "suggested_fix": None})
    assert invalid.refused and invalid.code == "invalid_args"
    drained = rtools.pop_recorded("run-1", "grp-1")
    assert [d["finding_id"] for d in drained] == ["RV1", "RV2"]
    assert rtools.pop_recorded("run-1", "grp-1") == []


# Golden: the R3 population of 8.7 -----------------------------------------------------------


def _r3_population_with_gates() -> tuple[list[dict], list[GateFinding]]:
    """The R3 population rows (section 8.7) and the real WS4 linear_in_ea
    findings: plans_reader_gates.run_final over each project's rows."""
    from app.services import eval_manifest as em
    from app.services import plans_reader_gates as pg
    from app.services.eval_reference import BackupData
    from app.services.plans_reader_score import parse_estimator_boq

    data = BackupData(BACKUP)
    pop = em.population("r3", data)
    assert not set(pop["ids"]) & em.HOLDOUT_IDS
    lines: list[dict] = []
    gates: list[GateFinding] = []
    for pid in pop["ids"]:
        boq = [r for r in data.files(pid, "boq") if str(r.get("filename", "")).lower().endswith((".xlsx", ".xlsm"))]
        project: list[dict] = []
        for r in parse_estimator_boq(data.file_path(boq[-1]).read_bytes()):
            d = r.description.upper()
            if r.unit == "EA" and not r.is_accessory and em.R3_MATCH_RE.search(d) and not em.R3_EXCLUDE_RE.search(d):
                project.append({"line_id": f"B{len(lines) + len(project) + 1}", "description": r.description,
                                "unit": "EA", "quantity": r.quantity, "section": r.section, "group": r.group,
                                "subgroup": r.subgroup, "area": r.area})
        lines += project
        if project:
            report = pg.run_final(boq=project, run=str(pid), mode="legacy")
            gates += [g for g in report.findings if g.gate_id == "linear_in_ea"]
    assert len(lines) == pop["n_rows"]
    # Recomputed 2026-10-21 (holdout excluded): 119 rows from 25 projects
    # (147 from 28 when designed). Recorded in the build log.
    assert pop["n_rows"] == 119 and len(pop["ids"]) == 25
    return lines, gates


# The two legitimate EA rows the WS4 gate blocks today (electrical_items
# false positives: "CABLE EGRESS COVERS" reads as cable, "TRACK WITH NORMAL
# FIXTURES" reads as lighting track). Measured 2026-10-22: 68 linear_in_ea
# findings over the 119 rows (5 block, 63 warn); R3 fires on 5 rows, 2 of
# them critical.
R3_KNOWN_FALSE_CRITICAL = ("CABLE EGRESS", "TRACK WITH NORMAL FIXTURES")


@pytest.mark.golden
@pytest.mark.skipif(not BACKUP.is_dir(), reason="needs the BDR_Prod backup on disk")
def test_r3_population_critical_rows_are_only_the_known_gate_false_positives():
    lines, gates = _r3_population_with_gates()
    assert any(g.severity == "block" for g in gates)  # the critical branch is reachable
    findings = rr.rule_r3(lines, rr.ReviewInputs(), gates)
    by_id = {ln["line_id"]: ln for ln in lines}
    critical = [by_id[f.line_ids[0]]["description"].upper() for f in findings if f.severity == "critical"]
    assert len(critical) == len(R3_KNOWN_FALSE_CRITICAL)
    assert all(any(k in d for d in critical) for k in R3_KNOWN_FALSE_CRITICAL), critical
    assert len(findings) < len(lines)  # most legitimate rows do not even warn


@pytest.mark.golden
@pytest.mark.skipif(not BACKUP.is_dir(), reason="needs the BDR_Prod backup on disk")
@pytest.mark.xfail(strict=True, reason="WS4 linear_in_ea blocks 2 legitimate EA rows (CABLE EGRESS, TRACK WITH "
                   "NORMAL FIXTURES) through electrical_items false positives; R3 turns them critical")
def test_r3_population_legitimate_ea_rows_never_fire_r3_critical():
    lines, gates = _r3_population_with_gates()
    findings = rr.rule_r3(lines, rr.ReviewInputs(), gates)
    assert [f for f in findings if f.severity == "critical"] == []


# record_finding durability through the tool loop store ----------------------------------------


def test_record_finding_survives_kill_and_resume_without_renumbering(monkeypatch):
    """A real plans_reader_toolloop.run_loop over a FakeLoopStore (fake
    OpenAI transport): the reviewer records three findings over three
    rounds; the loop is killed at every write point, the process bucket is
    lost with it, and the resumed loop plus pop_recorded(store=...) return
    exactly RV1, RV2, RV3 with their claims, never a renumbered or lost one."""
    from decimal import Decimal

    from app.services import ai_spend, llm, llm_gate, llm_queue
    from app.services import llm_parts as lp
    from app.services import plans_reader_toolloop as loop
    from app.services.plans_reader_budgets import LoopBudget
    from app.services.plans_reader_loop_store import FakeLoopStore, Killed
    from app.services.plans_reader_tools import ToolResult, ToolSpec
    from tests.test_plans_reader_toolloop import FINAL, SCHEMA, FakeOpenAI, _schema, _settings

    llm_gate.reset_gates()
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: None)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    monkeypatch.setattr(ai_spend, "check_cap", lambda sb, *, estimated_usd=0, now=None: Decimal("50"))
    monkeypatch.setattr(ai_spend, "_scope_spent", lambda sb, column, value: Decimal("0"))
    ai_spend.set_ledger_hook(None)
    group = "88888888-8888-4888-8888-888888888888"

    def probe(ctx: ToolContext, args: dict) -> ToolResult:
        return ToolResult.ok({"ok": True}, citations=["t:1a2b3c4d:1:1"])

    probe_spec = ToolSpec("probe", "Probe the page.", _schema({"q": {"type": "string"}}), {}, "read",
                          frozenset({"review"}), probe)
    specs = [probe_spec, rtools.SPEC]
    saved = dict(REGISTRY)
    REGISTRY["probe"] = probe_spec

    def finding(n: int) -> tuple[str, dict]:
        return ("record_finding", {"severity": "warning", "line_ids": [f"B{n}"], "claim": f"Claim number {n} about B{n}",
                                   "evidence": ["t:1a2b3c4d:1:1"], "suggested_fix": None})

    policy = {1: [("probe", {"q": "a"}), finding(1)], 2: [finding(2)], 3: [finding(3)], 4: "final"}

    def ctx(store: FakeLoopStore) -> loop.LoopContext:
        return loop.LoopContext(
            run={}, kind="review", feature="plans_review", system="Review the estimate.",
            user_parts=[lp.TextPart("Ledger", cache_break=True), lp.TextPart("Review B1 to B3.")],
            tools=specs, tool_ctx=ToolContext(run_id="run-rv", kind="review", group_id=group),
            store=store, response_schema=SCHEMA, schema_name="review",
            budget=LoopBudget(kind="review", max_rounds=6, max_tool_calls=16, max_calls_per_round=6, max_images=4,
                              max_usd=Decimal("1.00")),
            group_id=group, settings=_settings(),
        )

    expected = [("RV1", ["B1"], "Claim number 1 about B1"), ("RV2", ["B2"], "Claim number 2 about B2"),
                ("RV3", ["B3"], "Claim number 3 about B3")]
    try:
        baseline = FakeLoopStore(run_id="run-rv")
        monkeypatch.setattr(llm, "_client_for", lambda route, settings: FakeOpenAI(policy))
        assert loop.run_loop(ctx(baseline)).parsed == FINAL
        got = rtools.pop_recorded("run-rv", group, store=baseline)
        assert [(f["finding_id"], f["line_ids"], f["claim"]) for f in got] == expected
        assert all(f["severity"] == "warning" and f["evidence"] == ["t:1a2b3c4d:1:1"] for f in got)
        for kill_at in range(1, baseline.write_count + 1):
            store = FakeLoopStore(run_id="run-rv", kill_at=kill_at)
            client = FakeOpenAI(policy)
            monkeypatch.setattr(llm, "_client_for", lambda route, settings, c=client: c)
            with pytest.raises(Killed):
                loop.run_loop(ctx(store))
            rtools._RECORDED.clear()  # the process died with the loop
            assert loop.run_loop(ctx(store)).parsed == FINAL, kill_at
            got = rtools.pop_recorded("run-rv", group, store=store)
            assert rtools.pop_recorded("run-rv", group) == []
            rows = [r for r in store.tool_calls(f"group:{group}") if r["tool_name"] == "record_finding"]
            interrupted = [r for r in rows if r["refused"] and (r["result_data"] or {}).get("code") == "interrupted"]
            ids = [f["finding_id"] for f in got]
            assert len(ids) == len(set(ids)), kill_at  # never two findings with one id
            if not interrupted:
                assert [(f["finding_id"], f["line_ids"], f["claim"]) for f in got] == expected, kill_at
                continue
            # A kill inside the handler window: the loop answers that call
            # "interrupted" (a review tool never re-runs) and the model never
            # saw its id, so the ids handed out stay contiguous from RV1 and
            # every other finding survives with its claim.
            lost = {r["args"]["claim"] for r in interrupted}
            assert [f["claim"] for f in got] == [c for _i, _l, c in expected if c not in lost], kill_at
            assert ids == [f"RV{i}" for i in range(1, len(ids) + 1)], kill_at
            seen_by_model = {(r["result_data"] or {}).get("finding_id") for r in rows if not r["refused"]}
            assert set(ids) == seen_by_model, kill_at
    finally:
        REGISTRY.clear()
        REGISTRY.update(saved)
        rtools._RECORDED.clear()
        llm_gate.reset_gates()


# K1: no dollar reaches a non-pricing reader through a band or R12 finding ---------------------


_NUMBER_TOKEN_RE = __import__("re").compile(r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])")


def numbers_in(node) -> set[float]:
    """Every number in a payload, as a numeric leaf or as a standalone number
    token inside a string, rounded to cents (so a formatted 53.3333 matches
    the 53.333... it was printed from)."""
    out: set[float] = set()
    if isinstance(node, bool) or node is None:
        return out
    if isinstance(node, (int, float)):
        out.add(round(float(node), 2))
    elif isinstance(node, str):
        out |= {round(float(m), 2) for m in _NUMBER_TOKEN_RE.findall(node)}
    elif isinstance(node, dict):
        for k, v in node.items():
            out |= numbers_in(k) | numbers_in(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            out |= numbers_in(v)
    return out


def money_key_paths(node, path="") -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if rr._MONEY_KEY_RE.search(str(k)):
                out.append(p)
            out += money_key_paths(v, p)
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            out += money_key_paths(v, f"{path}[{i}]")
    return out


# Synthetic numbers only (never real project dollars).
R12_SEED = {"unit_material_usd": 400.0, "p25": 80.0, "p75": 120.0, "median": 100.0, "qty": 10}
R12_DOLLARS = {400.0, 80.0, 120.0, 100.0, 3000.0, round(80.0 / rr.R12_SPREAD, 2), round(120.0 * rr.R12_SPREAD, 2)}
# The dollars a pricing viewer sees in the finding (the library median only feeds the impact).
R12_SHOWN = R12_DOLLARS - {100.0}
BAND_SEED = {"value": 812.5, "p10": 256.0, "p25": 400.0, "p50": 803.0, "p75": 1100.0, "p90": 1501.0}
BAND_DOLLARS = {round(v, 2) for v in BAND_SEED.values()}


def seeded_r12_unit_material_finding() -> rr.ReviewFinding:
    class Lib(FakePriceService):
        def lookup(self, query):
            out = super().lookup(query)
            if out["status"] == "ok":
                out["labor_hours"] = {"median": 1.0, "p25": 0.8, "p75": 1.2, "n_projects": 9}
                out["material"] = {"median": R12_SEED["median"], "p25": R12_SEED["p25"], "p75": R12_SEED["p75"],
                                   "n_projects": 9}
            return out

    lines = [{"line_id": "B1", "description": "LED TROFFER", "unit": "EA", "qty": R12_SEED["qty"], "priced": True,
              "unit_hours": 1.0, "unit_material_usd": R12_SEED["unit_material_usd"], "material_source": "vendor_quote",
              "hours_source": "library"}]
    found = rr.rule_r12(rr.ReviewInputs(priced={"lines": lines}, price=Lib({"led troffer": {"uom": "EA"}})))
    assert len(found) == 1 and found[0].impact_usd == pytest.approx(3000.0)
    found[0].finding_id = "RF7"
    return found[0]


def seeded_money_band_finding() -> rr.ReviewFinding:
    check = BandCheck(metric="bid_per_device", stratum="building", n=29, position="above", percentile=0.93,
                      severity="warning", level="total", reason="outside p10 to p90 (advisory)", band_version="b1",
                      line_ids=("B1",), **BAND_SEED)
    found = rr.band_review_findings([check])
    assert len(found) == 1
    found[0].finding_id = "RF8"
    return found[0]


def test_r12_unit_material_facts_are_keyed_as_money_and_the_pricing_payload_keeps_them():
    f = seeded_r12_unit_material_finding()
    full = f.to_dict()
    assert full["evidence"][0]["facts"] == {"unit_material_usd": 400.0, "p25_usd": 80.0, "p75_usd": 120.0, "n_projects": 9}
    assert full["detail"] == "unit material 400 vs library [53.3333, 180] over 9 projects"
    assert full["impact_usd"] == pytest.approx(3000.0)
    assert R12_SHOWN <= numbers_in(full)  # positive control: the scanner sees the seeded dollars
    # The unit hours variant keeps plain keys and its hour numbers (not money).
    hours = rr.rule_r12(rr.ReviewInputs(priced={"lines": [
        {"line_id": "B3", "description": "DUPLEX RECEPTACLE", "unit": "EA", "qty": 10, "priced": True,
         "unit_hours": 5.0, "unit_material_usd": 100.0, "material_source": "library", "hours_source": "manual"}]},
        price=type("Lib", (FakePriceService,), {"lookup": lambda self, q: {
            "status": "ok", "matched": {}, "candidates": [],
            "labor_hours": {"median": 1.0, "p25": 0.8, "p75": 1.2, "n_projects": 9},
            "material": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n_projects": 9}}})()))
    assert [h.title.split(" outside")[0] for h in hours] == ["unit hours"]
    hd = hours[0].to_dict(include_money=False)
    assert hd["evidence"][0]["facts"] == {"value": 5.0, "p25": 0.8, "p75": 1.2, "n_projects": 9}
    assert hd["detail"].startswith("unit hours 5 vs library") and hd["impact_hours"] == pytest.approx(40.0)


def test_r12_and_money_band_findings_carry_no_seeded_dollar_for_a_non_pricing_reader():
    r12 = seeded_r12_unit_material_finding().to_dict(include_money=False)
    assert money_key_paths(r12) == ["impact_usd"] and r12["impact_usd"] is None
    assert r12["evidence"][0]["facts"] == {"n_projects": 9}
    assert r12["detail"] == ("unit material outside the library band over 9 projects; "
                             "the dollar values are visible to pricing roles only")
    assert not (numbers_in(r12) & R12_DOLLARS), numbers_in(r12) & R12_DOLLARS
    assert r12["title"] == "unit material outside the library band: LED TROFFER" and r12["line_ids"] == ["B1"]

    band = seeded_money_band_finding()
    full = band.to_dict()
    assert full["evidence"][0]["facts"]["value"] == 812.5 and full["evidence"][0]["facts"]["p90"] == 1501.0
    assert BAND_DOLLARS <= numbers_in(full)
    red = band.to_dict(include_money=False)
    facts = red["evidence"][0]["facts"]
    assert all(facts[k] is None for k in ("value", "p10", "p25", "p50", "p75", "p90"))
    assert facts["metric"] == "bid_per_device" and facts["n"] == 29 and facts["position"] == "above"
    assert not (numbers_in(red) & BAND_DOLLARS), numbers_in(red) & BAND_DOLLARS
    # A band of a count metric keeps its values for everyone.
    count = BandCheck(metric="hours_per_device", value=3.1, stratum="building", n=29, p10=1.02, p25=2.0, p50=2.94,
                      p75=4.0, p90=5.12, position="in_band", percentile=0.5, severity="warning", level="component",
                      reason="x", band_version="b1", line_ids=())
    plain = rr.band_review_findings([count])[0].to_dict(include_money=False)
    assert plain["evidence"][0]["facts"]["value"] == 3.1 and plain["evidence"][0]["facts"]["p90"] == 5.12
