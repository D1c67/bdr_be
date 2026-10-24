"""WS5 breaker plan on the WS1 panel schedules (app/services/scope_breakers.py)
and its enforcement (E12). The 7154 test reads the committed WS1 fixture
(real parsed text of the 7154 ELEC set, no model)."""

from __future__ import annotations

import json
from pathlib import Path

from app.services import scope_breakers as sb
from app.services import scope_engine as se
from app.services.scope_policy_seed import SEED_POLICY

FIXTURE = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7154"


def _elec_facts() -> dict:
    data = json.loads((FIXTURE / "page_facts_elec.json").read_text())
    return next(d for d in data["documents"] if d["key"] == "elec")["facts"]


def _plan() -> sb.BreakerPlan:
    facts = _elec_facts()
    sheets = {p["page"]: p["sheet_number"] for p in facts["pages"]}
    return sb.plan_breakers(facts["schedules"], sheet_for_page=sheets)


def test_7154_exactly_one_line_of_four_gfci_breakers():
    plan = _plan()
    assert plan.status == "resolved"
    assert [ln.description for ln in plan.lines] == ["20A/1P CIRCUIT BREAKER, GFCI"]
    line = plan.lines[0]
    assert line.quantity == 4
    assert line.circuits == ["1EELC-1", "1EELC-3", "1EELC-5", "1EELC-7"]
    assert line.sheet == "E0.03" and line.basis == "keynote_new"
    assert plan.reuse_count == 22
    assert "1HD-21" in plan.reuse_circuits and "1LI-16" in plan.reuse_circuits
    d = plan.to_dict()
    assert d["needs_review"] == sb.REUSE_REVIEW
    assert "22 existing panel positions" in d["clarification"]


def _main_with_breakers() -> dict:
    main = json.loads((FIXTURE / "outputs" / "main_none_v1.json").read_text())
    return main


def test_7154_enforce_replaces_provisional_rows_with_the_keyed_line():
    main = _main_with_breakers()
    ctx = {"breaker_plan": _plan().to_dict()}
    out = se.enforce(main, ctx, SEED_POLICY, ())
    rows = [it for h, g, s, it in se.boq_items(out) if sb.is_breaker_row(it["description"])]
    assert [(r["description"], r["quantity"]) for r in rows] == [("20A/1P CIRCUIT BREAKER, GFCI", 4)]
    excluded = [e for e in out["excluded_lines"] if e["rule_id"] == "BRK-REUSE"]
    assert sum(e["quantity"] for e in excluded) == 22
    assert all(e["source"] == "policy" and e["item"] for e in excluded)
    report = out["scope_policy"]
    assert any(n["code"] == sb.REUSE_REVIEW for n in report["needs_review"])
    assert any(c["key"] == "breakers:plan" for c in report["clarifications"])


def _panel(existing: bool, circuits: list[dict]) -> dict:
    return {"kind": "panel", "page": 4, "panel": {"name": "LP1", "existing": existing, "circuits": circuits}}


def _ckt(n: int, desc: str, status: str, *, keys=(), basis=None, gfci=False, wire=None, signals=None) -> dict:
    return {"ckt": n, "desc": desc, "status": status, "keynotes": list(keys), "status_basis": basis, "trip_a": 20,
            "poles": 1, "gfci": gfci, "wire": {"hot": wire}, "signals": signals or {}}


def test_unresolved_key_keeps_the_model_rows_with_a_warning():
    sched = [_panel(True, [
        _ckt(1, "RECEPTS ROOM 101", "unknown", keys=["3"], wire="12"),
        _ckt(3, "RECEPTS ROOM 102", "unknown", wire="12", signals={"wire_filled": True}),
        _ckt(5, "SPARE", "spare"),
    ])]
    plan = sb.plan_breakers(sched)
    assert plan.status == "partial" and plan.unresolved_circuits == ["LP1-1"]
    assert plan.reuse_circuits == ["LP1-3"] and not plan.lines
    assert any(sb.UNRESOLVED_KEY_WARNING in w for w in plan.warnings)
    main = {"boq": {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [{"name": "SWITCHGEAR", "subgroups": [
        {"name": "CIRCUIT BREAKERS", "items": [
            {"description": "20A/1P CIRCUIT BREAKER", "quantity": 1, "unit": "EA", "material_note": "LP1 circuit 1"},
            {"description": "20A/1P CIRCUIT BREAKER", "quantity": 1, "unit": "EA", "material_note": "LP1 circuit 3"},
        ]}]}]}]}]}}
    out = se.enforce(main, {"breaker_plan": plan.to_dict()}, SEED_POLICY, ())
    kept = [it["material_note"] for *_x, it in se.boq_items(out)]
    assert kept == ["LP1 circuit 1"]
    assert any("breaker_key_unresolved" in w["text"] for w in out["scope_policy"]["warnings"])


def test_new_panel_positions_are_breakers_to_buy():
    sched = [_panel(False, [
        _ckt(1, "LIGHTING", "unknown"),
        _ckt(2, "RECEPTS", "unknown", gfci=True),
        _ckt(3, "SPACE", "space"),
        _ckt(4, "SPARE", "spare"),
    ])]
    plan = sb.plan_breakers(sched)
    assert {(ln.description, ln.quantity) for ln in plan.lines} == {
        ("20A/1P CIRCUIT BREAKER", 1), ("20A/1P CIRCUIT BREAKER, GFCI", 1)}
    assert all(ln.basis == "new_panel" for ln in plan.lines)


def test_no_panel_schedule_means_no_plan_and_no_change():
    assert sb.plan_breakers([]).status == "unavailable"
    main = {"boq": {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [{"name": "SWITCHGEAR", "subgroups": [
        {"name": "CIRCUIT BREAKERS", "items": [{"description": "20A/1P CIRCUIT BREAKER", "quantity": 3, "unit": "EA"}]}]}]}]}]}}
    out = se.enforce(main, {"breaker_plan": sb.plan_breakers([]).to_dict()}, SEED_POLICY, ())
    assert [it["quantity"] for *_x, it in se.boq_items(out)] == [3]


def test_breaker_row_detection_and_wording():
    assert sb.is_breaker_row("20A/1P CIRCUIT BREAKER, GFCI")
    assert sb.is_breaker_row("20A/1P GFCI CIRCUIT BREAKER")
    assert not sb.is_breaker_row("30A/3P DISCONNECT SWITCH, NEMA 3R")
    assert not sb.is_breaker_row("PANELBOARD 225A MAIN BREAKER")
    assert sb.breaker_description(30, 2, gfci=False) == "30A/2P CIRCUIT BREAKER"
    assert sb.mentions_circuits("1EELC circuits 9, 11, 13", ["1EELC-11"])
    assert not sb.mentions_circuits("1EELC circuits 9, 11, 13", ["1LH-11"])


# 7172 (dev run 785ae29e): WS1 read general notes on E0.00 as panel "AND" with 4
# unrated positions and parsed no schedule on E0.02; the reader transcribed the two
# new panels as S2 "Branch Panel: A" (21 rows) and S4 "Branch Panel: B" (32 rows).

FIXTURE_7172 = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7172" / "panel_schedules.json"
TRUTH_7172 = {"20A/1P CIRCUIT BREAKER": 58, "20A/2P CIRCUIT BREAKER": 1, "20A/3P CIRCUIT BREAKER": 1,
              "30A/2P CIRCUIT BREAKER": 1, "30A/3P CIRCUIT BREAKER": 5, "100A/3P CIRCUIT BREAKER": 1}
PANEL_A = "PANEL A, 277/480V, 3-PH, 4W, 200A MLO TYPE, 14KAIC, 42 POLES, SURFACE MOUNT, NEMA 1"
PANEL_B = "PANEL B, 120/208V, 3-PH, 4W, 200A MCB TYPE, 10KAIC, 64 POLES, SURFACE MOUNT, NEMA 1"


def _rows_7172() -> list[dict]:
    return json.loads(FIXTURE_7172.read_text())["schedules"]


def _junk_and_panel() -> dict:
    """The WS1 schedule of run 785ae29e (shape and values as stored, raw text
    trimmed): four positions, all circuit 1, no trip or poles."""
    ckt = {"ckt": 1, "ckts": [1], "desc": "", "trip_a": None, "poles": None, "status": "unknown", "keynotes": [],
           "gfci": False, "wire": {"hot": None}, "signals": {"bold": True, "panel_not_marked_existing": True}}
    return {"kind": "panel", "page": 1, "title": "PANEL AND",
            "panel": {"name": "AND", "existing": False, "main": None, "circuits": [dict(ckt) for _ in range(4)]}}


def test_7172_junk_ws1_panel_is_dropped_not_turned_into_four_breakers():
    plan = sb.plan_breakers([_junk_and_panel()], sheet_for_page={1: "E0.00"})
    assert plan.status == "unavailable" and not plan.lines and not plan.panel_lines
    assert any(w.startswith(sb.UNRATED_PANEL_WARNING) and "AND (E0.00)" in w for w in plan.warnings)


def test_7172_panel_headers_parse_from_the_schedule_rows():
    a, b = (sb.panel_from_schedule_row(r) for r in _rows_7172())
    assert a["panel"]["name"] == "A" and b["panel"]["name"] == "B"
    assert not a["panel"]["existing"] and not b["panel"]["existing"]
    assert a["panel"]["header"] == {"volts": "277/480", "phase": 3, "wires": 4, "aic_ka": 14.0, "main_type": "MLO",
                                    "bus_amps": 200, "main_amps": 200, "mounting": "SURFACE", "enclosure": "NEMA 1",
                                    "positions": 42}
    assert b["panel"]["header"]["main_type"] == "MCB" and b["panel"]["header"]["aic_ka"] == 10.0
    assert b["panel"]["header"]["positions"] == 64 and b["panel"]["header"]["volts"] == "120/208"
    assert a["sheet"] == "E0.02" and a["ref"] == "S2" and b["ref"] == "S4"
    # a 3-pole breaker spans three circuit rows and is one circuit
    ckts = {(c["ckt"]): (c["trip_a"], c["poles"]) for c in a["panel"]["circuits"]}
    assert ckts[1] == (100, 3) and 3 not in ckts and 5 not in ckts
    assert ckts[8] == (30, 3) and 10 not in ckts and 12 not in ckts
    # SPACE rows carry no trip on these schedules, so none is a breaker
    assert all(c["status"] != "space" for c in a["panel"]["circuits"] + b["panel"]["circuits"])


def test_7172_breaker_plan_matches_the_estimate_truth():
    plan = sb.plan_breakers([_junk_and_panel()], sheet_for_page={1: "E0.00"}, schedule_rows=_rows_7172())
    assert plan.status == "resolved" and plan.panels == ["A", "B"]
    assert {ln.description: ln.quantity for ln in plan.lines} == TRUTH_7172
    assert all(ln.basis == "new_panel" and ln.sheet == "E0.02" for ln in plan.lines)
    assert plan.new_count == 67 and plan.reuse_count == 0
    one_p = next(ln for ln in plan.lines if ln.description == "20A/1P CIRCUIT BREAKER")
    assert one_p.circuits[:4] == ["A-2", "A-4", "A-6", "A-7"] and "B-15" not in one_p.circuits
    assert [p.description for p in plan.panel_lines] == [PANEL_A, PANEL_B]
    d = plan.to_dict()
    assert [p["ref"] for p in d["panel_lines"]] == ["S2", "S4"]
    assert "New panels carried: " + PANEL_A in d["clarification"]
    back = sb.BreakerPlan.from_dict(d)
    assert [p.description for p in back.panel_lines] == [PANEL_A, PANEL_B]
    assert back.to_dict()["lines"] == d["lines"]


def test_7172_keys_match_the_scorer_truth_keys():
    from app.services import plans_reader_score as score

    plan = sb.plan_breakers([], schedule_rows=_rows_7172())
    keys = {score.classify_line(p.description, "NEW WORK", "SWITCHGEAR", "EA") for p in plan.panel_lines}
    assert keys == {
        score.LineKey("panelboard", frozenset({"14AIC", "200A", "277/480V", "3PH", "42P", "MLO", "NEMA1", "PANEL:A"})),
        score.LineKey("panelboard", frozenset({"10AIC", "120/208V", "200A", "3PH", "64P", "MCB", "NEMA1", "PANEL:B"})),
    }


def test_spare_counts_only_with_a_trip_rating_and_existing_rows_give_no_lines():
    cols = ["CKT", "Circuit Description", "Trip", "Poles", "Poles", "Trip", "Circuit Description", "CKT"]
    row = {"schedule_type": "panel", "name": "Panel LP-2", "notes": "Volts: 120/208; Mains Type: MLO", "columns": cols,
           "rows": [["1", "SPARE", "20 A", "1", "1", "--", "Spare", "2"],
                    ["3", "Space", "--", "1", "2", "30", "AHU-1", "4"],
                    ["5", "LIGHTS", "20", "", "--", "--", "--", "6"]],
           "row_id": "S9", "sheet_number": "E5.01"}
    plan = sb.plan_breakers([], schedule_rows=[row])
    # SPARE 20 A is a breaker; Space and the unrated Spare are not; LIGHTS has a trip but
    # no pole cell and no continuation rows, so it stays an unrated line (enforce warns)
    assert {(ln.amps, ln.poles): ln.quantity for ln in plan.lines} == {(20, 1): 1, (30, 2): 1, (20, None): 1}
    assert not plan.warnings
    assert next(ln for ln in plan.lines if ln.poles == 1).circuits == ["LP-2-1"]
    unrated = next(c for c in sb.panel_from_schedule_row(row)["panel"]["circuits"] if c["ckt"] == 5)
    assert unrated["trip_a"] == 20 and unrated["poles"] is None
    existing = {**row, "name": "(E) Panel LP-2"}
    eplan = sb.plan_breakers([], schedule_rows=[existing])
    assert eplan.panels == ["LP-2"] and not eplan.panel_lines
    assert not [ln for ln in eplan.lines if ln.amps]


def test_ws1_panel_wins_over_the_schedule_row_of_the_same_name():
    facts = _elec_facts()
    sheets = {p["page"]: p["sheet_number"] for p in facts["pages"]}
    cat = json.loads((FIXTURE / "outputs" / "category_electrical_drawings_v1.json").read_text())
    plan = sb.plan_breakers(facts["schedules"], sheet_for_page=sheets, schedule_rows=cat["schedules"])
    assert plan.panels == ["1HD", "1EELC", "1LH", "1LI"] and not plan.panel_lines
    assert [(ln.description, ln.quantity) for ln in plan.lines] == [("20A/1P CIRCUIT BREAKER, GFCI", 4)]
    assert plan.reuse_count == 22


def test_names_panel():
    assert sb.names_panel(PANEL_A, "A")
    assert sb.names_panel('PANELBOARD "B" 225A MCB', "B")
    assert not sb.names_panel(PANEL_A, "B")
    assert not sb.names_panel("20A/1P CIRCUIT BREAKER, PANEL A", "A")
    assert not sb.names_panel("FEEDER TO PANEL A", "A")
