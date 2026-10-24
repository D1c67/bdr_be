"""plans_reader_consolidator: the main group in mode tools (build spec 4.1,
4.3, 5.8; WS4 wave 3). Scripted-model replay on the committed 7154 fixture
(the merged v2 electrical output with its page facts, a synthetic measure
output, synthetic facts and a synthetic breaker plan), every 5.8 refusal
code, idempotent writes on resume, the code assembly, the replay stage and
the tool registrations. No model is called: `llm.complete_turn` is replaced
by a scripted policy. Synthetic quantities everywhere outside the fixture;
no prices anywhere.

Replay numbers recorded by `test_scripted_replay_on_7154` (printed with -s):
BOQ lines, candidates, accepted, first-turn tokens, loop rounds, orphans.
"""

from __future__ import annotations

import copy
import json
import re
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest

from app.core.config import Settings
from app.services import ai_spend, llm, llm_gate, llm_parts, llm_queue
from app.services import plans_reader_consolidator as C
from app.services import plans_reader_gates as G
from app.services import plans_reader_lines as L
from app.services import plans_reader_merge as MG
from app.services import plans_reader_rows as R
from app.services import plans_reader_toolloop as loop
from app.services.estimating_contracts import Fact, FactCite, validate_gate_report
from app.services.plans_reader_loop_store import FakeLoopStore, loop_key
from app.services.plans_reader_replay import registered_stages
from app.services.plans_reader_review_schemas import CONSOLIDATOR_WRITE_TOOL_ARGS, REFUSAL_CODES
from app.services.plans_reader_schemas import CATEGORIES, MAIN_AGENT_SCHEMA
from app.services.plans_reader_tools import (
    REGISTRY,
    FakeScopePolicyService,
    ToolContext,
    dispatch,
    fake_services,
    offered_tools,
)
from tests.plans_reader_fixtures import ELEC_DOC_7154, FIXTURE_7154, load_7154

GROUP = "77777777-7777-4777-8777-777777777777"
RFP_DOC = "059e95bd-55eb-4d3b-87d4-f2bea2a261d0"
HONEYWELL_QUOTE = "Access controls and all Fire detection systems for the building are provided by Honeywell."
HONEYWELL_ALLOWANCE = "HONEYWELL DESIGN-BUILD FA AND ACCESS INTERFACE SCOPE"
SETTINGS = {"plans_reader_gates_blocking": True, "ai_scope_policy_enforce": True,
            "plans_reader_census_tol_strong": 0.05, "plans_reader_census_tol_weak": 0.10}
RECEPT_SUBGROUP = "RECEPTACLES (RECEPTACLES INCLUDES 4\"S DEEP COMB BOX AND COVER PLATE)"


def honeywell_fact() -> Fact:
    return Fact(id="F1", system="fire alarm and access control", statement=HONEYWELL_QUOTE, party="Honeywell", verified=True,
                cite=FactCite(document_id=RFP_DOC, page=2, quote=HONEYWELL_QUOTE), kind="provided_by_others")


def unverified_fact() -> Fact:
    return Fact(id="F2", system="telecom", statement="Data cabling by the owner's vendor.", party="owner vendor", verified=False,
                cite=FactCite(document_id=RFP_DOC, page=3, quote="Data cabling by the owner's vendor."), kind="provided_by_others")


def measure_output() -> dict:
    q = lambda rid, group, sub, desc, qty, basis, kind, sheet: {  # noqa: E731
        "row_id": rid, "group": group, "subgroup": sub, "description": desc, "quantity": qty, "unit": "FT",
        "wastage_applies": True, "basis": basis, "caveat": None, "sheet_refs": [sheet], "line_hint": {"kind": kind},
    }
    return {
        "version": 1,
        "conventions": {"version": "g3-branch-2026-10-a", "branch_conduit": "3/4\"C EMT", "min_branch_awg": 12, "route_factor": 1.0},
        "scales": [], "routes": [], "findings": [],
        "quantities": [
            q("M1", "WIRING", "CONDUCTORS (AWG)", "#12 THHN/THWN Cu", 900, "measured_route", "branch", "E2.01"),
            q("M2", "WIRING", "CONDUITS", "3/4\"C EMT", 300, "measured_route", "branch", "E2.01"),
            q("M3", "OTHER ITEMS", "MISCELLANEOUS", "#18/4C CONTROL CABLE", 125, "assumed_allowance", "control_cable", "M2.01"),
        ],
    }


def scope_context(facts: list[Fact]) -> dict:
    return {
        "digest": "G3 SCOPE DECISIONS (binding; computed by code from the documents and the G3 policy):\n- Wage: prevailing_wage (document_cue). Labor time: day.",
        "facts": [f.to_dict() for f in facts],
        "breaker_plan": {"status": "resolved", "lines": [
            {"amps": 20, "poles": 1, "gfci": True, "afci": False, "quantity": 4, "circuits": ["1HD-21", "1HD-23"], "sheet": "E0.03",
             "basis": "keynote_new", "description": "20A 1P GFCI BREAKER"},
        ]},
    }


@pytest.fixture(scope="module")
def run7154() -> dict:
    return load_7154()


@pytest.fixture(scope="module")
def merged(run7154) -> dict:
    facts = json.loads((FIXTURE_7154 / "page_facts_elec.json").read_text())
    pages = {(ELEC_DOC_7154, p["page"]): p for p in facts["documents"][0]["facts"]["pages"]}
    return MG.consolidate_category("electrical_drawings", run7154["groups"], words_by_page=pages, ledger=run7154["run"]["ledger"],
                                   documents=run7154["documents"], llm_rollup=False)


def make_inputs(run7154, merged, *, facts: list[Fact] | None = None, measure: dict | None = None, settings: Any = None) -> C.ConsolidatorInputs:
    facts = [honeywell_fact(), unverified_fact()] if facts is None else facts
    return C.ConsolidatorInputs(
        run=run7154["run"], documents=run7154["documents"],
        category_outputs={"electrical_drawings": merged, "rfp": run7154["rfp"], "specifications": run7154["specifications"]},
        measure=measure_output() if measure is None else measure, scope_context=scope_context(facts), facts=facts,
        ledger=run7154["run"]["ledger"], groups=run7154["groups"], settings=settings or dict(SETTINGS),
    )


@pytest.fixture
def session(run7154, merged) -> C.Session:
    C.reset_sessions()
    s = C.Session(make_inputs(run7154, merged), group_id=GROUP)
    C.register_session(s, group_id=GROUP)
    yield s
    C.reset_sessions()


def _ctx(**over) -> ToolContext:
    base = dict(run_id=str(over.pop("run_id", "run-7154")), kind="main", group_id=GROUP, services=fake_services())
    base.update(over)
    return ToolContext(**base)


def _line(**kw) -> dict:
    line = {"area": "UMC Outpatient Pharmacy Remodel", "section": "NEW WORK", "group": "OTHER ITEMS", "subgroup": RECEPT_SUBGROUP,
            "description": "DUPLEX RECEPTACLE", "unit": "EA", "source_refs": [{"ref": "C13", "multiplier": 1}], "combine": "single",
            "caveat": None, "material_note": None, "material_category": "General Material"}
    line.update(kw)
    return line


_V2_ITEM_KEYS = ("line_id", "source_refs", "combine", "basis", "needs_recount")


def legacy_shape(output: dict) -> dict:
    """The main output with the v2 provenance keys stripped from every BOQ
    item, as the strict MAIN_AGENT_SCHEMA would see it (the renderer itself
    ignores the extra keys)."""
    out = json.loads(json.dumps({k: output[k] for k in MAIN_AGENT_SCHEMA["properties"]}))
    for area in out["boq"]["areas"]:
        for section in area["sections"]:
            for group in section["groups"]:
                for sub in group["subgroups"]:
                    sub["items"] = [{k: v for k, v in it.items() if k not in _V2_ITEM_KEYS} for it in sub["items"]]
    return out


def _refused(data: dict, code: str) -> dict:
    assert data.get("refused") is True, data
    assert data["code"] == code, (data["code"], data.get("message"))
    return data


def _first(session: C.Session, pred) -> str:
    return next(rid for rid in sorted(session.rows, key=C._row_order) if rid[0] == "C" and pred(session.rows[rid]))


# ---------------------------------------------------------------------------
# Session, candidates, read tools
# ---------------------------------------------------------------------------


def test_session_rows_floors_candidates_and_policy_rows(session):
    c_rows = [r for r in session.rows if r[0] == "C"]
    assert len(c_rows) == 56 and all(session.rows[r]["status"] == "active" for r in c_rows)
    assert len(session.floors) == 11
    assert {r for r in session.rows if r[0] == "M"} == {"M1", "M2", "M3", "M4"}  # 3 measure rows + the breaker policy row
    policy = session.rows["M4"]
    assert policy["basis"] == "policy" and policy["origin"]["source"] == "scope_breakers" and policy["quantity"] == 4
    kinds = {c["kind"] for c in session.candidates.values()}
    assert kinds == {"count", "measure", "policy"}
    assert list(session.candidates) == [f"K{i}" for i in range(1, len(session.candidates) + 1)]
    # a candidate sums the same device family across sheets and never a tile subtotal without floor
    for c in session.candidates.values():
        if c["kind"] == "count" and any(session.rows[r].get("coverage") in R.INCOMPLETE_COVERAGES for r in c["row_ids"]):
            assert c["combine"] == "floor"
    # rows that read as existing are not proposed and say so
    existing = [u for u in session.unproposed if "existing" in u["reason"]]
    assert existing and all(re.search(r"(?i)existing|\(E\)", u["description"]) for u in existing)
    # the measure candidates keep the measure's group and subgroup
    m = next(c for c in session.candidates.values() if c["row_ids"] == ["M1"])
    assert (m["group"], m["subgroup"], m["unit"], m["quantity"], m["basis"]) == ("WIRING", "CONDUCTORS (AWG)", "FT", 900, "measured")


PANEL_A = "PANEL A, 277/480V, 3-PH, 4W, 200A MLO TYPE, 14KAIC, 42 POLES, SURFACE MOUNT, NEMA 1"


def test_policy_rows_carry_the_plan_panel_lines(run7154, merged):
    """The WS5 plan's new panels (scope_breakers PanelLine) are policy rows
    beside the breaker rows, so the PANEL A line enforce inserts cites a
    row of the run: M<n> after the breaker row, 1 EA, SWITCHGEAR / PANELS,
    line_hint kind panel, proposed as a policy candidate. A panel line
    without a description and junk entries are skipped."""
    inputs = make_inputs(run7154, merged)
    inputs.scope_context["breaker_plan"]["panel_lines"] = [
        {"description": PANEL_A, "name": "A", "header": {"volts": "277/480V"}, "quantity": 1, "unit": "EA", "sheet": "E0.02",
         "source": "schedule_row", "ref": "S2", "basis": "new_panel"},
        {"name": "B", "header": {}, "sheet": "E0.02"},
        "junk",
    ]
    s = C.Session(inputs, group_id="g-panels")
    assert [(r["row_id"], r["description"], r["quantity"]) for r in s.policy_rows] == [("M4", "20A 1P GFCI BREAKER", 4.0), ("M5", PANEL_A, 1.0)]
    panel = s.rows["M5"]
    assert panel["line_hint"] == {"area": "NEW WORK", "group": "SWITCHGEAR", "subgroup": "PANELS", "kind": "panel"}
    assert (panel["unit"], panel["basis"], panel["coverage"], panel["status"], panel["circuits"]) == ("EA", "policy", "derived", "active", [])
    assert panel["origin"] == {"group_kind": "derive", "source": "scope_breakers"} and panel["sheet"]["sheet_number"] == "E0.02"
    assert panel["kind"] == "M" and (panel["group"], panel["subgroup"]) == ("SWITCHGEAR", "PANELS")
    cand = next(c for c in s.candidates.values() if c["row_ids"] == ["M5"])
    assert cand["kind"] == "policy" and (cand["group"], cand["subgroup"], cand["unit"], cand["quantity"]) == ("SWITCHGEAR", "PANELS", "EA", 1)
    assert "refusal" not in cand and cand["description"] == PANEL_A
    accepted = C.apply_write(s, "accept_candidates", {"ids": [cand["id"]], "overrides": []})
    assert accepted["results"][0].get("refused") is None
    line = next(ln for ln in s.lines() if ln["description"] == PANEL_A)
    assert (line["group"], line["subgroup"], line["quantity"]) == ("SWITCHGEAR", "PANELS", 1)
    assert [sr["ref"] for sr in line["source_refs"]] == ["M5"]
    # a plan without panel lines (today's 7154) is unchanged
    plain = C.Session(make_inputs(run7154, merged), group_id="g-plain")
    assert [r["row_id"] for r in plain.policy_rows] == ["M4"]


def test_consolidator_prompt_names_schedule_listed_variants_and_gear_by_rating():
    """The luminaire sentence keeps a schedule-listed variant tag (AE beside
    A) with no suffix; gear is named by rating and a panel by its schedule
    header; a panel or transformer row is never cited under an equipment
    connection line. The prompt stays dash-free and hashed."""
    text = C.CONSOLIDATOR_SYSTEM
    assert "L1 - <schedule description>, L1-EM for the life-safety variant, X1 for exit signs; a schedule that lists the variant as its own tag, AE beside A, keeps that tag with no suffix" in text
    assert "Gear is named by rating (20A/1P CIRCUIT BREAKER, 30A/3P DISCONNECT SWITCH, NEMA 3R, 75KVA TRANSFORMER) and a panel by its schedule header" in text
    assert "a panel or transformer row is never cited under an equipment connection line (refused unit_mismatch)" in text
    assert chr(0x2013) not in text and chr(0x2014) not in text
    from app.services import plans_reader_prompt_hash as ph

    C.register_prompt_parts()
    assert ph.registered_parts()["ws4.consolidator_system"] == text


def test_read_tools_inside_a_session(session):
    ctx = _ctx()
    rows = dispatch(REGISTRY["get_rows"], ctx, {"kind": "C", "sheet_key": None, "query": "receptacle", "status": None, "offset": None})
    assert not rows.refused and rows.data["total"] >= 5 and all(r["id"].startswith("C") for r in rows.data["rows"])
    assert set(rows.citations) == {r["id"] for r in rows.data["rows"]}
    paged = dispatch(REGISTRY["get_rows"], ctx, {"kind": None, "sheet_key": None, "query": None, "status": None, "offset": 300})
    assert paged.data["total"] == len(session.rows) and paged.data["next_offset"] is None
    row = dispatch(REGISTRY["get_row"], ctx, {"id": "C13"})
    assert row.data["row_id"] == "C13" and row.data["consumed_by"] is None and row.citations == ["C13"]
    assert dispatch(REGISTRY["get_row"], ctx, {"id": "C999"}).code == "unknown_ref"
    census = dispatch(REGISTRY["get_census"], ctx, {"sheet_key": "D1P8"})
    assert census.data["total"] >= 1 and all(f["sheet_key"] == "D1P8" for f in census.data["floors"])
    demo = next(f for f in census.data["floors"] if f["family"] == "demo_x")
    assert demo["floor"] == 39 and demo["status"] in ("pass", "short", "unknown") and demo["accounted"] is not None
    proposed = dispatch(REGISTRY["propose_lines"], ctx, {"section": "DEMOLITION"})
    assert proposed.data["total"] >= 5 and all(c["section"] == "DEMOLITION" for c in proposed.data["candidates"])
    assert all(c["refs"] and c["id"].startswith("K") for c in proposed.data["candidates"])
    assert dispatch(REGISTRY["get_rows"], ctx, {"kind": "Z"}).code == "invalid_args"


def test_read_tools_without_a_session_use_the_row_store():
    C.reset_sessions()
    ctx = _ctx(run_id="other")
    # the registry's RowStore answers (the WS2 fake here); no session is needed for reads
    got = dispatch(REGISTRY["get_rows"], ctx, {"kind": None, "sheet_key": None, "query": None, "status": None, "offset": None})
    assert not got.refused and got.data["total"] == 0
    bare = ToolContext(run_id="other", kind="review", services=fake_services(rows=None))
    assert dispatch(REGISTRY["get_rows"], bare, {"kind": None, "sheet_key": None, "query": None, "status": None, "offset": None}).code == "unavailable"
    assert dispatch(REGISTRY["propose_lines"], ctx, {"section": None}).code == C.NO_SESSION
    assert dispatch(REGISTRY["add_boq_line"], ctx, _line()).code == C.NO_SESSION


# ---------------------------------------------------------------------------
# Every refusal code of 5.8
# ---------------------------------------------------------------------------


def test_add_boq_line_refusals(session):
    w = lambda **kw: C.apply_write(session, "add_boq_line", _line(**kw))  # noqa: E731
    _refused(w(source_refs=[{"ref": "C999", "multiplier": 1}]), "unknown_ref")
    session.rows["C13"]["status"] = "unverifiable"
    _refused(w(), "unverifiable_ref")
    session.rows["C13"]["status"] = "superseded"
    _refused(w(), "superseded_ref")
    session.rows["C13"]["status"] = "active"
    _refused(w(source_refs=[{"ref": "M1", "multiplier": 1}]), "unit_mismatch")
    tile = _first(session, lambda r: r.get("coverage") == "tile_subtotal")
    _refused(w(section="DEMOLITION", group="DEMOLITION", subgroup="", description="REMOVE EXISTING ROUND DOWNLIGHT",
               source_refs=[{"ref": tile, "multiplier": 1}], material_category="Demolition"), "partial_requires_floor")
    _refused(w(group="NOT A GROUP"), "vocab")
    _refused(w(group="WIRING", subgroup="CONDUITS", description="3/4\" EMT CONDUIT", unit="FT", source_refs=[{"ref": "M2", "multiplier": 1}]), "conduit_form")
    _refused(w(description="FIRE ALARM SMOKE DETECTOR"), "excluded_by_fact")["detail"]["fact_id"] == "F1"
    # rough-in for a system by others stays with the EC (never excluded_by_fact)
    ok = w(description="FIRE ALARM ROUGH-IN BOX AND CONDUIT STUB", subgroup="MISCELLANEOUS")
    assert ok.get("line_id") == "B1" and ok["quantity"] == session.rows["C13"]["quantity"]
    _refused(w(), "ref_consumed")["detail"]
    _refused(C.apply_write(session, "update_boq_line", {"line_id": "B99", **{k: None for k in _line()}}), "unknown_line")
    _refused(C.apply_write(session, "remove_boq_line", {"line_id": "B99", "reason": "x"}), "unknown_line")
    got = C.apply_write(session, "update_boq_line", {"line_id": "B1", **{k: None for k in _line()}, "description": "FIRE ALARM BACK BOX ROUGH-IN"})
    assert got["line_id"] == "B1"
    assert C.apply_write(session, "remove_boq_line", {"line_id": "B1", "reason": "test"})["freed_refs"] == ["C13"]
    _refused(C.apply_write(session, "accept_candidates", {"ids": ["K999"], "overrides": []})["results"][0], "unknown_candidate")


def test_account_rows_refusals_and_dispositions(session):
    w = lambda **kw: C.apply_write(session, "account_rows", {"row_ids": ["C1"], "disposition": "existing_to_remain", "reason": "existing switchgear",  # noqa: E731
                                                              "fact_id": None, "duplicate_of": None, "note_ref": None, **kw}, ctx=_ctx())
    _refused(w(row_ids=["C1", "C999"]), "unknown_ref")
    _refused(w(disposition="duplicate_view"), "duplicate_view_requires_duplicate_of")
    _refused(w(disposition="duplicate_view", duplicate_of="C1"), "duplicate_view_requires_duplicate_of")
    _refused(w(disposition="by_others"), "by_others_unverified")
    _refused(w(disposition="by_others", fact_id="F2"), "by_others_unverified")  # unverified never excludes
    assert session.human_findings and session.human_findings[-1]["kind"] == "by_others_unverified"
    _refused(w(disposition="existing_circuit"), "existing_circuit_uncited")
    _refused(w(disposition="existing_circuit", note_ref="t:deadbeef:9:1"), "existing_circuit_uncited")
    ctx = _ctx()
    ctx.ledger_refs.add("t:deadbeef:9:1")
    ok = C.apply_write(session, "account_rows", {"row_ids": ["C1"], "disposition": "existing_circuit", "reason": "reuse", "fact_id": None,
                                                 "duplicate_of": None, "note_ref": "t:deadbeef:9:1"}, ctx=ctx)
    assert ok["accounted"] == ["C1"] and session.consumed_by("C1") == "existing_circuit"
    q_row = next(r for r in session.rows if r[0] == "Q")
    assert C.apply_write(session, "account_rows", {"row_ids": ["C2"], "disposition": "existing_circuit", "reason": "reuse", "fact_id": None,
                                                   "duplicate_of": None, "note_ref": q_row})["accounted"] == ["C2"]
    assert C.apply_write(session, "account_rows", {"row_ids": ["C3"], "disposition": "by_others", "reason": "honeywell", "fact_id": "F1",
                                                   "duplicate_of": None, "note_ref": None})["accounted"] == ["C3"]
    # duplicate_view: the same item on another sheet
    dup_a = _first(session, lambda r: "duplex" in r["description"].lower() and "remove" not in r["description"].lower())
    others = [rid for rid in session.rows if rid[0] == "C" and rid != dup_a and session.rows[rid].get("item_key") == session.rows[dup_a].get("item_key")
              and session.rows[rid]["sheet"]["sheet_key"] != session.rows[dup_a]["sheet"]["sheet_key"]]
    if others:
        assert C.apply_write(session, "account_rows", {"row_ids": [others[0]], "disposition": "duplicate_view", "reason": "enlarged plan",
                                                       "fact_id": None, "duplicate_of": dup_a, "note_ref": None})["accounted"] == [others[0]]
    assert len(session.dispositions) >= 3


def test_allowance_and_cited_text_refusals(session):
    allowance = lambda name, **cite: C.apply_write(session, "add_allowance", {"name": name, "cite": {"row_id": None, "fact_id": None, **cite}}, ctx=_ctx())  # noqa: E731
    _refused(allowance("TEMPORARY LIGHTING"), "unresolved_cite")
    _refused(allowance("TEMPORARY LIGHTING", row_id="C999"), "unresolved_cite")
    _refused(allowance(HONEYWELL_ALLOWANCE, fact_id="F1"), "excluded_by_fact")["detail"]["fact_id"] == "F1"
    _refused(allowance("FIRE ALARM INTERFACE ALLOWANCE", row_id="C13"), "excluded_by_fact")
    ok = allowance("TEMPORARY LIGHTING", row_id="C13")
    assert ok["allowances"] == ["TEMPORARY LIGHTING"]
    assert allowance("temporary lighting", row_id="C13")["allowances"] == ["TEMPORARY LIGHTING"]  # deduped
    policy = fake_services(policy=FakeScopePolicyService([{"rule_id": "EX-TEMP-POWER", "topic": "temporary_power", "kind": "hard",
                                                           "decision": "exclude", "default_text": "Temporary power is excluded."}]))
    ctx = _ctx(services=policy)
    _refused(C.apply_write(session, "add_allowance", {"name": "TEMPORARY POWER", "cite": {"row_id": "C13", "fact_id": None}}, ctx=ctx), "excluded_by_policy")
    for name in ("add_exclusion", "add_assumption", "add_rfi", "add_risk"):
        _refused(C.apply_write(session, name, {"text": "something", "cite": "nowhere"}), "unresolved_cite")
        assert C.apply_write(session, name, {"text": f"{name} text", "cite": "F1"})["count"] == 1
        assert C.apply_write(session, name, {"text": f"{name} by sheet", "cite": "E2.01"})["count"] == 2
        assert C.apply_write(session, name, {"text": f"{name} by spec", "cite": session.inputs.ledger["spec_toc"][1]["section"]})["count"] == 3
    _refused(C.apply_write(session, "add_exclusion", {"text": "Allow 40 hours for this", "cite": "F1"}), "pricing_terms")


def test_prose_refusals_and_brief_shape(session):
    C.apply_write(session, "accept_candidates", {"ids": list(session.candidates), "overrides": []})
    recept = G.receptacle_total(session.lines())
    assert recept > 0
    w = lambda field, value: C.apply_write(session, "write_brief", {"field": field, "value": value})  # noqa: E731
    _refused(w("project_summary", ["not", "a string"]), "brief_value_shape")
    _refused(w("project_summary", "Budget about $12,000 for this."), "pricing_terms")
    _refused(w("project_summary", "Plan 80 MH of labor."), "pricing_terms")
    _refused(w("project_summary", f"The plans show {int(recept) + 5} receptacles on E2.01."), "brief_numbers")
    _refused(w("electrical_scope_summary", "Branch lengths cannot be defensibly measured from these plans."), "capability_denial")
    assert w("project_summary", f"A pharmacy remodel with {int(recept)} new receptacles.")["written"] == ["project_summary"]
    assert w("systems", [{"system": "Lighting", "in_scope": "yes", "notes": None}])["written"] == ["project_summary", "systems"]
    _refused(C.apply_write(session, "set_estimate_notes", {"general_notes": ["Include 12 hrs of testing"], "area_notes": []}), "pricing_terms")
    assert C.apply_write(session, "set_estimate_notes", {"general_notes": ["Coordinate shutdowns with the owner."], "area_notes": [{"area": "A", "notes": ["n"]}]}) == {"general_notes": 1, "area_notes": 1}
    note = {"material_category": "Lighting", "notes": ["Fixtures per schedule"], "specified_manufacturers": [], "lead_time_items": []}
    _refused(C.apply_write(session, "set_material_notes", {"notes": [note, note]}), "vocab")
    _refused(C.apply_write(session, "set_material_notes", {"notes": [{**note, "material_category": "Paint"}]}), "vocab")
    assert C.apply_write(session, "set_material_notes", {"notes": [note]})["categories"] == ["Lighting"]
    # capability denial is only a refusal when the measure output has data
    bare = C.Session(C.ConsolidatorInputs(**{**session.inputs.__dict__, "measure": None}), group_id="g-bare")
    assert C.apply_write(bare, "write_brief", {"field": "electrical_scope_summary", "value": "Branch lengths cannot be defensibly measured here."})["written"]


def test_finish_limit_finished_and_invalid_args(session):
    for n in (1, 2, 3):
        got = C.apply_write(session, "finish", {"confidence_notes": [f"call {n}"]})
        assert got["passed"] is False and got["calls_left"] == 3 - n
        assert {f["gate"] for f in got["findings"]} <= set(G.FINAL_GATES) and all(f["route"] == "model_fix" for f in got["findings"])
        assert all(f["route"] == "human" for f in got["blocking_routed"])
    assert session.finished is True and got["note"] == C.FINISHED_NOTE
    _refused(C.apply_write(session, "finish", {"confidence_notes": []}), "finish_limit")
    _refused(C.apply_write(session, "add_boq_line", _line()), C.FINISHED)
    ctx = _ctx()
    assert dispatch(REGISTRY["add_boq_line"], ctx, {"area": "x"}).code == "invalid_args"
    assert dispatch(REGISTRY["account_rows"], ctx, {"row_ids": ["C1"], "disposition": "made_up", "reason": "r", "fact_id": None, "duplicate_of": None, "note_ref": None}).code == "invalid_args"


def test_every_refusal_code_of_the_contract_is_produced_by_this_module():
    """Each 5.8 refusal code the contract lists for the consolidator tools is
    a code this module can return (the tests above exercise each one)."""
    src = (C.__file__ and open(C.__file__, encoding="utf-8").read()) + open(L.__file__, encoding="utf-8").read()
    for name, codes in REFUSAL_CODES.items():
        if name not in CONSOLIDATOR_WRITE_TOOL_ARGS:
            continue
        for code in codes:
            assert f'"{code}"' in src or f"{code.upper()} = " in src, (name, code)


# ---------------------------------------------------------------------------
# Idempotent writes and resume
# ---------------------------------------------------------------------------


def test_writes_are_idempotent_by_write_key_and_replay_from_the_store(session):
    first = C.apply_write(session, "add_boq_line", _line(), write_key=f"{GROUP}:call_1")
    again = C.apply_write(session, "add_boq_line", _line(), write_key=f"{GROUP}:call_1")
    assert first == again and len(session.book.lines) == 1 and first["line_id"] == "B1"
    assert C.apply_write(session, "add_boq_line", _line(), write_key=f"{GROUP}:call_2")["code"] == "ref_consumed"
    # a crashed attempt left its calls in the store: a rebuilt session replays them once
    store = FakeLoopStore(run_id="run-7154")
    key = loop_key(group_id=GROUP)
    store.insert_tool_call(key, 1, {"call_id": "call_1", "tool_name": "add_boq_line", "args": _line(), "result_data": first, "refused": False})
    store.insert_tool_call(key, 1, {"call_id": "call_2", "tool_name": "add_boq_line", "args": _line(), "result_data": {"refused": True, "code": "ref_consumed"}, "refused": True})
    store.insert_tool_call(key, 2, {"call_id": "call_3", "tool_name": "account_rows", "args": {"row_ids": ["C1"], "disposition": "existing_to_remain", "reason": "r", "fact_id": None, "duplicate_of": None, "note_ref": None},
                                    "result_data": {"accounted": ["C1"]}, "refused": False})
    store.insert_tool_call(key, 2, {"call_id": "call_4", "tool_name": "get_rows", "args": {}, "result_data": {"rows": []}, "refused": False})
    store.insert_tool_call(key, 3, {"call_id": "call_5", "tool_name": "add_rfi", "args": {"text": "q", "cite": "F1"}, "result_data": {"pending": True}, "refused": False})
    rebuilt = C.Session(session.inputs, group_id=GROUP)
    assert C.replay_stored_writes(rebuilt, store, key, owner=GROUP) == 2
    assert list(rebuilt.book.lines) == ["B1"] and rebuilt.consumed_by("C1") == "existing_to_remain" and rebuilt.rfis == []
    assert rebuilt.writes[f"{GROUP}:call_1"] == first and f"{GROUP}:call_2" not in rebuilt.writes
    # replaying twice changes nothing
    assert C.replay_stored_writes(rebuilt, store, key, owner=GROUP) == 0 and len(rebuilt.book.lines) == 1
    # build_main_loop with a store does the same and registers the session
    C.reset_sessions()
    parts, precalls = C.build_main_loop(None, session.inputs.run, {"id": GROUP}, SETTINGS, inputs=session.inputs, store=store)
    live = C.session_for(_ctx())
    assert live is not None and list(live.book.lines) == ["B1"] and live.replayed_writes == 2
    assert [p.name for p in precalls] == ["propose_lines", "get_census"]
    assert isinstance(parts[0], llm_parts.TextPart) and parts[0].text.startswith("G3 SCOPE DECISIONS")
    assert any(getattr(p, "cache_break", False) for p in parts)
    C.reset_sessions()


# ---------------------------------------------------------------------------
# The scripted-model replay through the WS2 loop
# ---------------------------------------------------------------------------


@pytest.fixture
def quiet(monkeypatch):
    llm_gate.reset_gates()
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: None)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    monkeypatch.setattr(ai_spend, "check_cap", lambda sb, *, estimated_usd=0, now=None: Decimal("50"))
    monkeypatch.setattr(ai_spend, "_scope_spent", lambda sb, column, value: Decimal("0"))
    ai_spend.set_ledger_hook(None)
    yield
    ai_spend.set_ledger_hook(None)
    llm_gate.reset_gates()


def _settings() -> Settings:
    return Settings(_env_file=None, openai_api_key="o-key", plans_reader_enabled=True, plans_reader_gates_blocking=True,
                    ai_scope_policy_enforce=True, plans_reader_consolidator_mode="tools")


class ScriptedModel:
    """A scripted consolidator policy: reads the candidates from the pre-call
    block, accepts them, accounts the leftovers finish reports, writes the
    prose, then answers. Every request is recorded; no provider is touched."""

    def __init__(self) -> None:
        self.requests: list[llm_parts.ModelRequest] = []
        self.round = 0

    def usage(self, output: int) -> llm.LlmUsage:
        """A cached prefix from round 2 on (the main budget's cache gate
        forces the final round when the observed cache ratio stays low)."""
        return llm.LlmUsage(input_tokens=20000, cached_tokens=18000 if self.round > 1 else 0, output_tokens=output)

    @staticmethod
    def _text(turn: Any) -> str:
        parts = getattr(turn, "parts", None) or getattr(turn, "results", ())
        out = []
        for p in parts:
            out.append(getattr(p, "text", ""))
        return "\n".join(out)

    def __call__(self, feature: str, req: llm_parts.ModelRequest, *, settings: Any = None) -> llm_parts.ModelTurn:
        self.requests.append(req)
        self.round += 1
        first = self._text(req.messages[0])
        last = self._text(req.messages[-1])
        calls: list[dict] = []
        n = 0

        def call(name: str, args: dict) -> None:
            nonlocal n
            n += 1
            calls.append({"call_id": f"r{self.round}c{n}", "name": name, "args": args})

        if self.round == 1:
            ids = sorted(set(re.findall(r'"id":"(K\d+)"', first)), key=lambda k: int(k[1:]))
            existing = sorted(set(re.findall(r'"id":"(C\d+)","description":"[^"]*","reason":"reads as existing', first)), key=lambda k: int(k[1:]))
            call("accept_candidates", {"ids": ids, "overrides": [{"id": ids[0], "description": None, "group": None, "subgroup": None, "caveat": "(NEED TO CONFIRM DETAILS)", "material_note": None}]})
            call("account_rows", {"row_ids": existing, "disposition": "existing_to_remain", "reason": "existing gear to remain", "fact_id": None, "duplicate_of": None, "note_ref": None})
            call("add_allowance", {"name": HONEYWELL_ALLOWANCE, "cite": {"row_id": None, "fact_id": "F1"}})
            call("add_exclusion", {"text": "Fire alarm and access control devices and programming by Honeywell; rough-in by G3.", "cite": "F1"})
            call("write_brief", {"field": "project_summary", "value": "Outpatient pharmacy remodel: demolition, new receptacles, fixtures and controls."})
            call("write_brief", {"field": "electrical_scope_summary", "value": "Branch circuits per the measure output; existing service to remain."})
            call("write_brief", {"field": "systems", "value": [{"system": "Fire alarm", "in_scope": "partial", "notes": "devices by Honeywell"}]})
            call("set_estimate_notes", {"general_notes": ["Coordinate shutdowns with the owner."], "area_notes": []})
            call("set_material_notes", {"notes": [{"material_category": "Lighting", "notes": ["Per schedule"], "specified_manufacturers": [], "lead_time_items": []}]})
            call("finish", {"confidence_notes": ["first pass"]})
        elif '"passed":false' in last:
            left = re.search(r'"unaccounted_rows":\[([^\]]*)\]', last)
            ids = re.findall(r'"(C\d+)"', left.group(1)) if left else []
            call("add_rfi", {"text": "Confirm the lighting track row length for removal.", "cite": "ED3.01"})
            if ids:
                call("account_rows", {"row_ids": ids, "disposition": "info_only", "reason": "linear item in EA or reference row; carried as an RFI", "fact_id": None, "duplicate_of": None, "note_ref": None})
            call("finish", {"confidence_notes": ["second pass"]})
        if calls and req.tools_enabled:
            return llm_parts.ModelTurn(text="", parsed=None, tool_calls=calls, usage=self.usage(400),
                                       cost_usd=None, response_id=f"resp_{self.round}", stop_reason="tool_use", provider="openai", model="gpt-6-astra")
        return llm_parts.ModelTurn(text="Consolidation complete.", parsed=None, tool_calls=[], usage=self.usage(40),
                                   cost_usd=None, response_id=f"resp_{self.round}", stop_reason="final", provider="openai", model="gpt-6-astra")


def _loop_ctx(store: FakeLoopStore, inputs: C.ConsolidatorInputs, settings: Settings) -> tuple[loop.LoopContext, C.Session]:
    C.reset_sessions()
    parts, precalls = C.build_main_loop(None, inputs.run, {"id": GROUP}, settings, inputs=inputs, store=store)
    session = C.session_for(_ctx())
    assert session is not None
    ctx = loop.LoopContext(
        run=dict(inputs.run), kind="main", feature="plans_main", system=C.CONSOLIDATOR_SYSTEM, user_parts=parts,
        tools=offered_tools("main", mode="write"), tool_ctx=_ctx(), store=store, response_schema=None, schema_name="plans_main_tools",
        group_id=GROUP, precalls=precalls, settings=settings,
    )
    return ctx, session


def test_scripted_replay_on_7154(run7154, merged, quiet, monkeypatch, capsys):
    """The acceptance of WS4 on the imported 7154, scripted: every BOQ line
    reproduces from its sources, no orphan rows, no unsourced lines, the
    Honeywell allowance absent, branch lines sourced to M rows, finish ends
    the loop on its pass."""
    model = ScriptedModel()
    monkeypatch.setattr(llm, "complete_turn", model)
    settings = _settings()
    inputs = make_inputs(run7154, merged, settings=settings)
    store = FakeLoopStore(run_id=inputs.run_id)
    ctx, session = _loop_ctx(store, inputs, settings)
    result = loop.run_loop(ctx)
    assert result.status == "done" and result.stop_reason == "final" and result.text == "Consolidation complete."
    assert session.finished and session.passed is True and session.finish_calls == 2
    # the loop ended right after the finish that passed: rounds = 2 tool rounds + the answer
    assert result.rounds == 3 and model.round == 3
    assert not model.requests[-1].tools_enabled or not model.requests[-1].messages[-1].results[-1].is_error
    output = C.finalize_main_loop(result.parsed, result, sb=None, run=inputs.run, group={"id": GROUP})
    jsonschema.validate(legacy_shape(output), MAIN_AGENT_SCHEMA)
    lines = G.boq_lines_of(output)
    rows = {**session.rows}
    # 100% of lines reproduce from their sources; 0 unsourced; 0 orphans
    assert lines and L.recheck(lines, rows) == []
    assert all(ln.get("source_refs") and ln.get("combine") for ln in lines)
    assert output["consolidator"]["unaccounted_rows"] == []
    report = output["consolidator"]["gate_report"]
    validate_gate_report(report)
    blocking = {c["code"] for c in report["checks"] if c["severity"] == "block"}
    assert blocking <= C.HUMAN_GATES, blocking
    assert "orphan_rows" not in blocking and "unsourced_line" not in blocking and "arithmetic" not in blocking
    # Honeywell: excluded, not an allowance
    assert HONEYWELL_ALLOWANCE not in output["estimate"]["extra_allowances"]
    assert any("Honeywell" in e for e in output["brief"]["exclusions"])
    # no line is in a system the Honeywell fact excludes (dampers and rough-in are EC work and stay)
    excluded_systems = G.fact_systems(honeywell_fact())
    assert all(not (G.systems_of(ln["description"]) & excluded_systems) or G.is_raceway_text(ln["description"], ln["unit"]) for ln in lines)
    # branch lines are sourced to M rows only
    wiring = [ln for ln in lines if ln["group"] == "WIRING"]
    assert wiring and all(all(r["ref"].startswith("M") for r in ln["source_refs"]) for ln in wiring)
    assert {ln["description"] for ln in wiring} >= {"#12 THHN/THWN CU", "3/4\"C EMT"}
    assert all(ln["basis"] == "measured" and ln["wastage_applies"] for ln in wiring)
    # the breaker policy row became a SWITCHGEAR line with basis policy
    breakers = [ln for ln in lines if ln["subgroup"] == "CIRCUIT BREAKERS" and ln["source_refs"][0]["ref"] == "M4"]
    assert breakers and breakers[0]["basis"] == "policy" and breakers[0]["quantity"] == 4
    # the candidate override landed
    assert any(ln["caveat"] == "(NEED TO CONFIRM DETAILS)" for ln in lines)
    # durability: every tool call stored once, writes memoized by group_id:call_id
    stored = store.tool_calls(loop_key(group_id=GROUP))
    refused = [(c["tool_name"], (c.get("result_data") or {}).get("code")) for c in stored if c.get("refused")]
    assert refused == [("add_allowance", "excluded_by_fact")], refused
    assert len({c["call_id"] for c in stored}) == len(stored) and len(stored) == 2 + 10 + 3
    assert len(session.writes) == 12  # 9 round-1 writes (the allowance was refused) + 3 in round 2
    # the consolidator block and the measured numbers
    prov = output["consolidator"]
    assert prov["mode"] == "tools" and prov["finished"] and prov["passed"] and prov["loop"]["rounds"] == 3
    assert prov["rows_version"] == 2 and prov["policy_rows"][0]["row_id"] == "M4"
    assert 15_000 <= prov["first_parts_tokens"] <= 30_000, prov["first_parts_tokens"]
    print(json.dumps({
        "replay_7154": {
            "boq_lines": len(lines), "candidates": prov["candidates"], "candidates_accepted": prov["candidates_accepted"],
            "lines_reproduced": f"{len(lines)}/{len(lines)}", "orphan_rows": 0, "unsourced_lines": 0, "first_parts_tokens": prov["first_parts_tokens"],
            "loop_rounds": result.rounds, "tool_calls": result.tool_calls, "finish_calls": prov["finish_calls"], "dispositions": len(output["dispositions"]),
            "blocking_human_routed": sorted(blocking), "wiring_lines": len(wiring),
        }
    }))
    # the final-gate pass at render time reads the dispositions and policy rows from the output itself
    rep = G.run_final(boq=output, main=output, category_outputs=list(inputs.category_outputs.values()), facts=inputs.facts, measure=inputs.measure,
                      settings=SETTINGS, mode="tools")
    assert {f.gate_id for f in rep.findings if f.blocking} <= C.HUMAN_GATES
    assert C.session_for(_ctx()) is None  # released by the finalizer


def test_finish_third_call_ends_the_loop_even_when_failing(run7154, merged, quiet, monkeypatch):
    class StubbornModel(ScriptedModel):
        def __call__(self, feature, req, *, settings=None):
            self.requests.append(req)
            self.round += 1
            if self.round <= 4 and req.tools_enabled:
                return llm_parts.ModelTurn(text="", parsed=None, tool_calls=[{"call_id": f"f{self.round}", "name": "finish", "args": {"confidence_notes": []}}],
                                           usage=self.usage(10), cost_usd=None, response_id=f"r{self.round}",
                                           stop_reason="tool_use", provider="openai", model="gpt-6-astra")
            return llm_parts.ModelTurn(text="done", parsed=None, tool_calls=[], usage=self.usage(5), cost_usd=None,
                                       response_id=f"r{self.round}", stop_reason="final", provider="openai", model="gpt-6-astra")

    model = StubbornModel()
    monkeypatch.setattr(llm, "complete_turn", model)
    settings = _settings()
    inputs = make_inputs(run7154, merged, settings=settings)
    store = FakeLoopStore(run_id=inputs.run_id)
    ctx, session = _loop_ctx(store, inputs, settings)
    result = loop.run_loop(ctx)
    assert session.finish_calls == 3 and session.finished and session.passed is False
    results = [c for c in store.tool_calls(loop_key(group_id=GROUP)) if c["tool_name"] == "finish"]
    assert [c["refused"] for c in results] == [False, False, False, True]
    assert results[2]["result_data"]["note"] == C.FINISHED_NOTE and results[3]["result_data"]["code"] == "finish_limit"
    output = C.finalize_main_loop(result.parsed, result, sb=None, run=inputs.run, group={"id": GROUP})
    assert output["consolidator"]["passed"] is False and output["consolidator"]["gate_report"]["blocking"] is True


def test_fallback_assembles_in_code_and_a_fresh_session_finalizes(run7154, merged):
    inputs = make_inputs(run7154, merged)
    out = C.fallback_main_output(None, inputs.run, {"id": GROUP}, SETTINGS, reason="loop_failed", inputs=inputs)
    jsonschema.validate(legacy_shape(out), MAIN_AGENT_SCHEMA)
    prov = out["consolidator"]
    assert prov["mode"] == "fallback" and prov["reason"] == "loop_failed" and prov["lines"] >= 30 and prov["gate_report"]
    assert any("Honeywell" in e for e in out["brief"]["exclusions"]) and HONEYWELL_ALLOWANCE not in out["estimate"]["extra_allowances"]
    assert all(d["disposition"] == "existing_to_remain" for d in out["dispositions"])
    assert L.recheck(G.boq_lines_of(out), C.Session(inputs).rows) == []
    # finalize on a live session that never called finish runs the gates once (route human)
    C.reset_sessions()
    live = C.register_session(C.Session(inputs, group_id=GROUP), group_id=GROUP)
    C.apply_write(live, "accept_candidates", {"ids": list(live.candidates)[:3], "overrides": []})
    got = C.finalize_main_loop(None, SimpleNamespace(as_dict=lambda: {"rounds": 1}, text="bye"), sb=None, run=inputs.run, group={"id": GROUP})
    assert got["consolidator"]["lines"] == 3 and got["consolidator"]["gate_report"]["blocking"] is True
    assert got["consolidator"]["closing_message"] == "bye" and got["consolidator"]["finish_calls"] == 0
    assert all(f.route in ("human", "none") for f in live.gate_findings)
    assert C.session_for(_ctx()) is None


# ---------------------------------------------------------------------------
# Replay stage and registrations
# ---------------------------------------------------------------------------


def _replay_ctx(run: dict, overrides: dict | None = None):
    return SimpleNamespace(bundle=SimpleNamespace(run=run), settings_overrides=overrides or {}, notes=[], note=lambda t: None)


def test_replay_stage_modes(run7154, merged):
    from app.services import plans_reader_replay as RP

    RP.reset_stages(real=True)
    try:
        C.register_replay_stage()
        _check_replay_stage_modes(run7154, merged)
    finally:
        # Restore every real stage for the rest of the process (a bare
        # reset_stages() keeps only the legacy entries and breaks later
        # replay tests such as test_plans_reader_measure).
        RP.reset_stages(real=True)
        C.register_replay_stage()


def _check_replay_stage_modes(run7154, merged):
    spec = registered_stages()["consolidate"]
    assert spec.version == C.CONSOLIDATOR_VERSION and spec.owner == "WS4" and not spec.legacy and not spec.llm
    inputs = make_inputs(run7154, merged)
    art = {
        "category:electrical_drawings": merged, "category:rfp": run7154["rfp"], "category:specifications": run7154["specifications"],
        "measure": inputs.measure, "scope_context": inputs.scope_context, "documents": run7154["documents"], "ledger": inputs.ledger,
        "groups": run7154["groups"], "main": run7154["main"],
    }
    ctx = _replay_ctx(run7154["run"])
    ctx.notes = []
    ctx.note = ctx.notes.append
    stored = C.replay_consolidate(ctx, dict(art))
    assert stored["main"] is run7154["main"] or stored["main"] == run7154["main"]
    assert {"boq_spec", "estimate_spec", "brief_spec"} <= set(stored)
    assembly = C.replay_consolidate(_replay_ctx(run7154["run"], {"consolidate_mode": "assembly", "plans_reader_gates_blocking": True}), dict(art))
    assert assembly["consolidator"]["mode"] == "fallback" and assembly["boq_spec"]["areas"] and assembly["main"]["consolidator"]["lines"] >= 30
    script = [{"tool": "accept_candidates", "args": {"ids": ["K1", "K2"], "overrides": []}},
              {"tool": "add_exclusion", "args": {"text": "FA by Honeywell", "cite": "F1"}},
              {"tool": "add_allowance", "args": {"name": HONEYWELL_ALLOWANCE, "cite": {"row_id": None, "fact_id": "F1"}}},
              {"tool": "finish", "args": {"confidence_notes": ["cassette"]}}]
    cassette = C.replay_consolidate(_replay_ctx(run7154["run"], {"consolidate_mode": "cassette"}), {**art, "consolidator_script": script})
    assert cassette["consolidator"]["mode"] == "cassette" and cassette["consolidator"]["lines"] == 2
    assert cassette["main"]["estimate"]["extra_allowances"] == [] and cassette["main"]["brief"]["exclusions"] == ["FA by Honeywell [F1]"]
    with pytest.raises(ValueError):
        C.replay_consolidate(_replay_ctx(run7154["run"], {"consolidate_mode": "nope"}), dict(art))


def test_replay_stage_test_leaves_the_real_stages_registered(run7154, merged, monkeypatch):
    """test_replay_stage_modes restores the real stages even when its body
    fails, so a later replay test (test_plans_reader_measure) still sees
    the real derive and merge stages and this module's consolidate."""
    import sys

    from app.services import plans_reader_measure  # noqa: F401 - registers the real derive stage
    from app.services import plans_reader_merge  # noqa: F401 - registers the real merge stage

    def boom(*_a):
        raise AssertionError("body failed")

    monkeypatch.setattr(sys.modules[__name__], "_check_replay_stage_modes", boom)
    with pytest.raises(AssertionError, match="body failed"):
        test_replay_stage_modes(run7154, merged)
    stages = registered_stages()
    for name in ("consolidate", "derive", "merge"):
        assert name in stages and not stages[name].legacy, name


def test_tools_registered_with_the_contract_schemas():
    for name, schema in CONSOLIDATOR_WRITE_TOOL_ARGS.items():
        spec = REGISTRY[name]
        assert spec.args_schema is schema and spec.mode == "write" and spec.kinds == frozenset({"main"})
    for name in ("get_rows", "get_row", "get_census", "propose_lines"):
        assert REGISTRY[name].mode == "read" and "main" in REGISTRY[name].kinds
    assert "propose_lines" not in {s.name for s in offered_tools("review")}
    assert "get_rows" in {s.name for s in offered_tools("review")} and "get_census" in {s.name for s in offered_tools("review")}
    assert "add_boq_line" not in {s.name for s in offered_tools("chat")}
    assert REGISTRY["finish"].max_calls_per_group == 4
    for spec in C.WRITE_SPECS:
        assert chr(0x2013) not in spec.description and chr(0x2014) not in spec.description
    assert chr(0x2014) not in C.CONSOLIDATOR_SYSTEM and chr(0x2013) not in C.CONSOLIDATOR_SYSTEM


# ---------------------------------------------------------------------------
# stop_when support (tool_stop) and the session lookup
# ---------------------------------------------------------------------------


def test_session_for_is_safe_without_a_session(run7154, merged):
    C.reset_sessions()
    assert C.session_for(None) is None
    assert C.session_for(SimpleNamespace(group_id=None, run_id=None)) is None
    assert C.session_for(SimpleNamespace()) is None
    assert C.session_for(_ctx()) is None
    assert C.session_finished(None) is False and C.session_finished(_ctx()) is False
    live = C.register_session(C.Session(make_inputs(run7154, merged), group_id=GROUP), group_id=GROUP)
    assert C.session_for(_ctx()) is live and C.session_finished(_ctx()) is False
    live.finished = True
    assert C.session_finished(_ctx()) is True
    # by run id when the group id is unknown
    assert C.session_for(SimpleNamespace(group_id="other", run_id=live.inputs.run_id)) is live
    C.reset_sessions()


def _drive_to_a_pass(session: C.Session) -> None:
    """The writes the ScriptedModel makes, applied in code, until finish passes."""
    ids = list(session.candidates)
    existing = [u["id"] for u in session.unproposed if "existing" in u["reason"]]
    script = [
        ("accept_candidates", {"ids": ids, "overrides": []}),
        ("account_rows", {"row_ids": existing, "disposition": "existing_to_remain", "reason": "existing gear to remain",
                          "fact_id": None, "duplicate_of": None, "note_ref": None}),
        ("add_exclusion", {"text": "Fire alarm and access control devices by Honeywell; rough-in by G3.", "cite": "F1"}),
        ("write_brief", {"field": "project_summary", "value": "Outpatient pharmacy remodel."}),
        ("finish", {"confidence_notes": ["first pass"]}),
    ]
    C.run_script(session, script, key_prefix="drive")
    if not session.passed:
        left = session.unaccounted_rows()
        C.run_script(session, [
            ("account_rows", {"row_ids": left, "disposition": "info_only", "reason": "reference row", "fact_id": None,
                              "duplicate_of": None, "note_ref": None}),
            ("finish", {"confidence_notes": ["second pass"]}),
        ], key_prefix="drive2")
    assert session.finished and session.passed is True


def test_finalize_on_tool_stop_assembles_from_the_line_book(run7154, merged):
    """stop_when fired right after the finish that passed: no closing
    answer, parsed None, stop reason tool_stop; the output still comes
    from the session's line book."""
    C.reset_sessions()
    inputs = make_inputs(run7154, merged)
    session = C.register_session(C.Session(inputs, group_id=GROUP), group_id=GROUP)
    _drive_to_a_pass(session)
    lines_before = len(session.book.lines)
    result = SimpleNamespace(parsed=None, text="", stop_reason="tool_stop",
                             as_dict=lambda: {"status": "done", "rounds": 2, "stop_reason": "tool_stop"})
    out = C.finalize_main_loop(None, result, sb=None, run=inputs.run, group={"id": GROUP})
    jsonschema.validate(legacy_shape(out), MAIN_AGENT_SCHEMA)
    prov = out["consolidator"]
    assert prov["lines"] == lines_before > 0 and prov["passed"] is True and prov["finished"] is True
    assert prov["stop_reason"] == "tool_stop" and "closing_message" not in prov
    assert prov["loop"]["stop_reason"] == "tool_stop" and prov["unaccounted_rows"] == []
    assert L.recheck(G.boq_lines_of(out), session.rows) == []
    assert C.session_for(_ctx()) is None


def test_stop_when_ends_the_main_loop_right_after_the_pass(run7154, merged, quiet, monkeypatch):
    """With the WS2 loop's stop_when (group A1), the scripted loop ends on
    tool_stop after round 2 (no closing round) and the finalizer
    assembles the full output."""
    if "stop_when" not in getattr(loop.LoopContext, "__dataclass_fields__", {}):
        pytest.skip("plans_reader_toolloop.LoopContext has no stop_when yet (group A1)")
    model = ScriptedModel()
    monkeypatch.setattr(llm, "complete_turn", model)
    settings = _settings()
    inputs = make_inputs(run7154, merged, settings=settings)
    store = FakeLoopStore(run_id=inputs.run_id)
    ctx, session = _loop_ctx(store, inputs, settings)
    ctx.stop_when = lambda: C.session_finished(ctx.tool_ctx)
    result = loop.run_loop(ctx)
    assert result.stop_reason == "tool_stop" and model.round == 2 and result.parsed is None
    out = C.finalize_main_loop(result.parsed, result, sb=None, run=inputs.run, group={"id": GROUP})
    assert out["consolidator"]["passed"] is True and out["consolidator"]["stop_reason"] == "tool_stop"
    assert out["consolidator"]["lines"] == len(session.book.lines) > 0


# ---------------------------------------------------------------------------
# The main A/B seam (scripts/plans_reader_ab.py resolves ab_runner by name)
# ---------------------------------------------------------------------------


AB_ROUTE = {"provider": "openai", "model": "gpt-6-astra", "effort": "medium"}


def _ab_task(**over) -> SimpleNamespace:
    base = dict(id="ab-main-1", kind="main", case_id=None, bundle=None, run_id=None, truth={"metric": "passed", "target": True})
    base.update(over)
    return SimpleNamespace(**base)


class RecordingModel(ScriptedModel):
    """The scripted consolidator that also keeps every turn it returned."""

    def __init__(self) -> None:
        super().__init__()
        self.turns: list[llm_parts.ModelTurn] = []

    def __call__(self, feature, req, *, settings=None):
        turn = super().__call__(feature, req, settings=settings)
        self.turns.append(turn)
        return turn


def _expected_cost(turns) -> float:
    from app.services import llm_pricing

    total = Decimal("0")
    for t in turns:
        cost, _conservative = llm_pricing.cost_or_conservative(t.model, t.usage, "openai")
        total += Decimal(str(cost))
    return float(total)


def test_ab_runner_returns_the_shape_with_the_recorded_cost(run7154, merged, quiet, monkeypatch):
    model = RecordingModel()
    monkeypatch.setattr(llm, "complete_turn", model)
    settings = _settings()
    C.reset_sessions()
    out = C.ab_runner(_ab_task(), AB_ROUTE, settings, inputs=make_inputs(run7154, merged, settings=settings), sb=None,
                      services=fake_services())
    assert set(out) >= {"answer", "cost_usd", "truncated", "stop_reason"}
    answer = out["answer"]
    assert set(answer) == set(C.AB_METRICS)
    assert answer["passed"] is True and answer["finished"] is True and answer["finish_calls"] == 2
    assert answer["lines"] > 0 and answer["unaccounted_rows"] == 0
    # the Honeywell allowance was refused (excluded_by_fact) and never retried
    assert answer["refused_calls"] == 1 and answer["refused_then_abandoned"] == 1
    assert answer["matched_lines"] is None and answer["count_lines_exact"] is None  # no reference in the task truth
    assert out["truncated"] is False and out["stop_reason"] in ("final", "tool_stop") and out["error"] is None
    assert model.turns and out["cost_usd"] > 0
    assert out["cost_usd"] == pytest.approx(_expected_cost(model.turns), abs=1e-5)
    assert isinstance(out["cost_usd"], float)
    # every request went out on the A/B route through the main loop; nothing left registered
    assert all(r.feature == "plans_main" for r in model.requests)
    assert C._SESSIONS == {}


def test_ab_runner_marks_a_cache_gated_loop_truncated(run7154, merged, quiet, monkeypatch):
    """A route whose prefix never caches: the main budget's cache gate
    forces the final round at round 3, so the task is reported truncated
    (kept out of the 8.3 comparison) and never passed."""

    class Uncached(RecordingModel):
        def __call__(self, feature, req, *, settings=None):
            self.requests.append(req)
            self.round += 1
            usage = llm.LlmUsage(input_tokens=20000, cached_tokens=0, output_tokens=200)
            if req.tools_enabled:
                turn = llm_parts.ModelTurn(text="", parsed=None, tool_calls=[{"call_id": f"u{self.round}", "name": "get_census", "args": {"sheet_key": None}}],
                                           usage=usage, cost_usd=None, response_id=f"u{self.round}", stop_reason="tool_use",
                                           provider="openai", model="gpt-6-astra")
            else:
                turn = llm_parts.ModelTurn(text="Out of rounds.", parsed=None, tool_calls=[], usage=usage, cost_usd=None,
                                           response_id=f"u{self.round}", stop_reason="final", provider="openai", model="gpt-6-astra")
            self.turns.append(turn)
            return turn

    model = Uncached()
    monkeypatch.setattr(llm, "complete_turn", model)
    settings = _settings()
    C.reset_sessions()
    out = C.ab_runner(_ab_task(), AB_ROUTE, settings, inputs=make_inputs(run7154, merged, settings=settings), sb=None,
                      services=fake_services())
    assert out["truncated"] is True and out["plan_reason"] == "cache_gate"
    assert out["answer"]["passed"] is False and out["answer"]["finish_calls"] == 0
    assert model.round == 3 and not model.requests[-1].tools_enabled
    assert out["cost_usd"] == pytest.approx(_expected_cost(model.turns), abs=1e-5)
    assert C._SESSIONS == {}


def test_ab_runner_accepts_a_script_route_and_a_task_without_inputs_refuses_cleanly():
    route = SimpleNamespace(as_dict=lambda: dict(AB_ROUTE))
    assert C._route_dict(route) == AB_ROUTE and C._route_dict(AB_ROUTE) == AB_ROUTE
    with pytest.raises(ValueError, match="bundle or a run_id"):
        C.ab_runner(_ab_task(), route, _settings(), sb=None)
    with pytest.raises(ValueError, match="dev client"):
        C.ab_runner(_ab_task(run_id="r1"), route, _settings(), sb=None)
    import inspect

    assert list(inspect.signature(C.ab_runner).parameters)[:3] == ["task", "route", "settings"]


def test_ab_runner_resolves_the_task_bundle_and_reference_under_the_given_eval_root(tmp_path, monkeypatch):
    """The A/B script's --eval-root reaches the seam as `eval_root`: a
    relative bundle resolves under it, not under settings.eval_root;
    without it the settings' root is the default."""
    from pathlib import Path

    from app.services import eval_bundle

    seen: list[Path] = []

    class Stop(Exception):
        pass

    def fake_load(path):
        seen.append(Path(path))
        raise Stop

    monkeypatch.setattr(eval_bundle, "load_bundle", fake_load)
    settings = _settings()
    with pytest.raises(Stop):
        C.ab_runner(_ab_task(bundle="bundles/case1/run1"), AB_ROUTE, settings, sb=None, eval_root=tmp_path)
    with pytest.raises(Stop):
        C.ab_runner(_ab_task(bundle="bundles/case1/run1"), AB_ROUTE, settings, sb=None)
    assert seen == [tmp_path / "bundles/case1/run1", Path(settings.eval_root) / "bundles/case1/run1"]
    assert C._eval_path("ref.json", settings, tmp_path) == tmp_path / "ref.json"
    assert C._eval_path("/abs/ref.json", settings, tmp_path) == Path("/abs/ref.json")


def test_ab_runner_passes_the_backup_dir_to_the_services(run7154, merged, quiet, monkeypatch, tmp_path):
    seen: list[dict] = []

    class Stop(Exception):
        pass

    def recording_services(inputs, **kwargs):
        seen.append(kwargs)
        raise Stop

    monkeypatch.setattr(C, "ab_services", recording_services)
    settings = _settings()
    C.reset_sessions()
    with pytest.raises(Stop):
        C.ab_runner(_ab_task(), AB_ROUTE, settings, inputs=make_inputs(run7154, merged, settings=settings), sb=None,
                    eval_root=tmp_path, backup_dir=tmp_path / "backup")
    assert seen and seen[0]["backup_dir"] == tmp_path / "backup"
    assert C._SESSIONS == {}


# ---------------------------------------------------------------------------
# First live WS4 iteration (run 19344776): orphan rows and the Q14 refusal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Luminaire names, measured items and the unconsumed M rows (7154 run 51ec302b)
# ---------------------------------------------------------------------------


L1_CANON = "L1 - 2' X 4' RECESSED TROFFER (CONTROL 0-10V)"
L1_EM_CANON = "L1-EM - 2' X 4' RECESSED TROFFER (CONTROL 0-10V)"
L3_CANON = "L3 - 6\" RECESSED DOWNLIGHT (CONTROL 0-10V)"


def _fixture_line(**kw) -> dict:
    return _line(group="LIGHTING FIXTURES", subgroup="FIXTURES", material_category="Lighting", **kw)


def test_luminaire_schedule_and_candidates_named_by_tag(session):
    """The S9 luminaire schedule of the fixture gives the tags; every
    fixture candidate is named "<tag> - <schedule description>" with the
    schedule's readable control method, the EM variant from the row's own
    words (shaded, life safety), the tag from the row's text or from the
    schedule row it resembles (the COMcheck rows carry no tag)."""
    sched = {e["tag"]: e for e in session.luminaires}
    assert list(sched) == ["L1", "L2", "L3", "L4", "L5", "X1"]
    assert (sched["L1"]["description"], sched["L1"]["control"], sched["L1"]["ref"], sched["L1"]["sheet"]) == ("2' X 4' RECESSED TROFFER", "0-10V", "S9#r1", "E0.04")
    assert sched["L4"]["control"] is None  # "[unreadable]" in the stored read is not a control value
    assert sched["L5"]["control"] == "LOCAL"
    names = {}
    for c in session.candidates.values():
        if c["kind"] == "count" and c["group"] == "LIGHTING FIXTURES":
            names[session.rows[c["row_ids"][0]]["description"]] = c["description"]
    assert names["New luminaire type L1, unshaded"] == L1_CANON
    assert names["New luminaire type L1, shaded/life-safety arrangement"] == L1_EM_CANON
    assert names["New round luminaire type L3, unshaded"] == L3_CANON
    assert names["New round luminaire type L3, shaded symbol"] == "L3-EM - 6\" RECESSED DOWNLIGHT (CONTROL 0-10V)"
    assert names["New task luminaire type L4"] == "L4 - TASK LED LIGHTING"
    assert names["New luminaire type X1"] == "X1 - LED EDGE LIT EXIT SIGN. NUMBER OF FACES AND CHEVRONS PER PLANS"
    assert names["New linear luminaire run type L5, variable length"] == "L5 - UNDERCABINET LED LIGHTING (CONTROL LOCAL)"
    assert names["2x4 recessed LED troffer, compliance-calculation quantity only"] == L1_CANON
    assert names["6-inch recessed LED downlight, compliance-calculation quantity only"] == L3_CANON
    # grouping stays on the row's own identity: the COMcheck row and the plan count are separate candidates
    assert len([c for c in session.candidates.values() if c["description"] == L1_CANON]) == 2
    # demolition rows keep their wording
    demo = [c["description"] for c in session.candidates.values() if c["section"] == "DEMOLITION"]
    assert demo and not any(C.leading_tag(d, sched) for d in demo)
    # the resolver's own answers
    rows = session.rows
    c34 = _first(session, lambda r: r["description"] == "New luminaire type L1, unshaded")
    got = C.resolve_luminaire(rows[c34], session.luminaires)
    assert (got["tag"], got["base"], got["variant"], got["how"]) == ("L1", "L1", None, "text")
    c8 = _first(session, lambda r: r["description"].startswith("2x4 recessed LED troffer, compliance"))
    assert C.resolve_luminaire(rows[c8], session.luminaires)["how"] == "similarity"
    assert C.resolve_luminaire({"description": "Duplex receptacle", "unit": "EA"}, session.luminaires) is None
    assert C.resolve_luminaire({"description": "Pendant fixture", "unit": "EA"}, session.luminaires) is None  # nothing resembles it
    assert C.resolve_luminaire({"description": "New luminaire type L1", "unit": "EA"}, []) is None
    assert C.luminaire_match_score("2X4 RECESSED LED TROFFER, NORMAL BRANCH", sched["L1"]) == 3
    assert C.luminaire_match_score("2X4 RECESSED LED TROFFER, NORMAL BRANCH", sched["L2"]) == 0  # the form differs
    assert C.leading_tag("TYPE 'A' - PENDANT", ["A"]) == "A" and C.leading_tag("LED EDGE-LIT EXIT SIGN", sched) is None
    assert C.leading_tag("L1-EM - TROFFER", sched) == "L1-EM" and C.leading_tag("L9 - TROFFER", sched) is None


def test_luminaire_lines_are_named_by_the_tag_of_their_cited_rows(session):
    """Write-time rule: a fixture line whose description lacks the tag of
    its cited rows is renamed (warning); the right tag in the model's own
    words is kept; the base tag without the row's variant is renamed; a
    different tag is refused (luminaire_tag); accept_candidates overrides
    and update_boq_line go through the same rule."""
    c34 = _first(session, lambda r: r["description"] == "New luminaire type L1, unshaded")
    c35 = _first(session, lambda r: r["description"] == "New luminaire type L1, shaded/life-safety arrangement")
    c39 = _first(session, lambda r: r["description"] == "New task luminaire type L4")
    got = C.apply_write(session, "add_boq_line", _fixture_line(description="2X4 RECESSED LED TROFFER, NORMAL BRANCH", source_refs=[{"ref": c34, "multiplier": 1}]))
    assert got["line_id"] and session.book.lines[got["line_id"]]["description"] == L1_CANON
    assert any("renamed by the luminaire tag" in w for w in got["warnings"])
    assert session.book.lines[got["line_id"]]["quantity"] == session.rows[c34]["quantity"]
    kept = C.apply_write(session, "add_boq_line", _fixture_line(description="L1-EM - 2X4 TROFFER, LIFE SAFETY BRANCH", source_refs=[{"ref": c35, "multiplier": 1}]))
    assert session.book.lines[kept["line_id"]]["description"] == "L1-EM - 2X4 TROFFER, LIFE SAFETY BRANCH" and not kept["warnings"]
    up = C.apply_write(session, "update_boq_line", {"line_id": kept["line_id"], **{k: None for k in _line()}, "description": "L1 - 2X4 TROFFER"})
    assert session.book.lines[kept["line_id"]]["description"] == L1_EM_CANON and any("renamed" in w for w in up["warnings"])
    bad = _refused(C.apply_write(session, "add_boq_line", _fixture_line(description="L3 - TASK LIGHT", source_refs=[{"ref": c39, "multiplier": 1}])), C.LUMINAIRE_TAG)
    assert (bad["detail"]["found"], bad["detail"]["expected"], bad["detail"]["schedule_ref"], bad["detail"]["description"]) == ("L3", "L4", "S9#r4", "L4 - TASK LED LIGHTING")
    assert session.book.consumed_by(c39) is None  # a refusal changes nothing
    cand = next(c for c in session.candidates.values() if c["row_ids"] == [c39])
    res = C.apply_write(session, "accept_candidates", {"ids": [cand["id"]], "overrides": [
        {"id": cand["id"], "description": "TASK LED LIGHT, WALL MOUNTED", "group": None, "subgroup": None, "caveat": None, "material_note": None}]})["results"][0]
    assert session.book.lines[res["line_id"]]["description"] == "L4 - TASK LED LIGHTING" and res["warnings"]
    # a refusal inside accept_candidates is reported per id, nothing written
    c37 = _first(session, lambda r: r["description"] == "New round luminaire type L3, unshaded")
    cand3 = next(c for c in session.candidates.values() if c["row_ids"] == [c37])
    out = C.apply_write(session, "accept_candidates", {"ids": [cand3["id"]], "overrides": [
        {"id": cand3["id"], "description": "L2 - DOWNLIGHT", "group": None, "subgroup": None, "caveat": None, "material_note": None}]})
    assert out["accepted"] == 0 and out["results"][0]["code"] == C.LUMINAIRE_TAG and session.book.consumed_by(c37) is None
    # receptacle lines never go through the rule
    plain = C.apply_write(session, "add_boq_line", _line())
    assert plain["line_id"] and not plain.get("warnings") and session.book.lines[plain["line_id"]]["description"] == "DUPLEX RECEPTACLE"
    # the scorer matches the canonical names by tag (the run's wording scored as extras)
    from app.services import plans_reader_score as S

    aliases = S.merge_aliases({"SHADED": "EM"})
    for desc, tag in ((L1_CANON, "L1"), (L1_EM_CANON, "L1-EM"), ("L4 - TASK LED LIGHTING", "L4")):
        key = S.classify_line(desc, "NEW WORK", "LIGHTING FIXTURES", "EA", subgroup="FIXTURES", aliases=aliases)
        assert (key.kind, set(key.attrs)) == ("fixture", {tag}), desc


def _track_measure() -> dict:
    measure = measure_output()
    measure["quantities"].append({
        "row_id": "M4", "group": "DEMOLITION", "subgroup": "", "description": "Remove existing track-lighting assemblies",
        "quantity": 50.01, "unit": "FT", "wastage_applies": False, "basis": "measured_route", "caveat": None, "sheet_refs": ["ED3.01"],
        "line_hint": {"kind": "linear_demolition", "area": "DEMOLITION", "group": "DEMOLITION", "subgroup": "", "replaces_unit": "EA", "tag": "(X), keynote 1"},
    })
    return measure


def test_measured_items_keep_their_name_and_unconsumed_m_rows_block(run7154, merged):
    """A line citing an M row keeps the measured item: the run's wording
    "... INCLUDING HEADS" (a lighting demolition in FT under the scorer and
    parse_item) is replaced by "REMOVAL OF LIGHTING TRACK"; wording that
    keeps the identity is kept. finish lists every unconsumed M row under
    orphan_rows (kind measure) with the call that resolves it, and the
    finding clears once the rows are cited."""
    C.reset_sessions()
    session = C.Session(make_inputs(run7154, merged, measure=_track_measure()), group_id=GROUP)
    assert {r for r in session.rows if r[0] == "M"} == {"M1", "M2", "M3", "M4", "M5"}  # M5 = the policy breaker row
    cand = next(c for c in session.candidates.values() if c["row_ids"] == ["M4"])
    assert (cand["description"], cand["section"], cand["unit"], cand["quantity"], cand["basis"]) == (C.TRACK_REMOVAL_DESCRIPTION, "DEMOLITION", "FT", 50.01, "measured")
    assert C.measure_line_description(session, session.rows["M1"]) == "#12 THHN/THWN CU"
    track = _line(section="DEMOLITION", group="DEMOLITION", subgroup="", unit="FT", material_category="Demolition",
                  description="REMOVE EXISTING TRACK-LIGHTING ASSEMBLIES INCLUDING HEADS", caveat="Includes associated track heads.",
                  source_refs=[{"ref": "M4", "multiplier": 1}], combine="single")
    got = C.apply_write(session, "add_boq_line", track)
    line = session.book.lines[got["line_id"]]
    assert (line["description"], line["quantity"], line["unit"], line["basis"], line["caveat"]) == ("REMOVAL OF LIGHTING TRACK", 50.01, "FT", "measured", "Includes associated track heads.")
    assert any("replaced by the measured item" in w for w in got["warnings"])
    assert C.apply_write(session, "remove_boq_line", {"line_id": got["line_id"], "reason": "test"})["freed_refs"] == ["M4"]
    same = C.apply_write(session, "add_boq_line", {**track, "description": "REMOVE EXISTING LIGHTING TRACK"})
    assert session.book.lines[same["line_id"]]["description"] == "REMOVE EXISTING LIGHTING TRACK" and not same["warnings"]
    assert "M4" not in session.unaccounted_rows() and session.unaccounted_rows()[-4:] == ["M1", "M2", "M3", "M5"]
    fin = C.apply_write(session, "finish", {"confidence_notes": []})
    assert fin["passed"] is False
    f = next(f for f in fin["findings"] if f["gate"] == "orphan_rows" and f["target"].get("kind") == "measure")
    assert f["target"]["row_ids"] == ["M1", "M2", "M3", "M5"] and f["route"] == "model_fix"
    assert [r for r in fin["unaccounted_rows"] if r[0] == "M"] == ["M1", "M2", "M3", "M5"]
    by_row = {i["row_id"]: i["instruction"] for i in fin["orphan_instructions"]}
    assert by_row["M1"].startswith("M1 (900 FT #12 THHN/THWN Cu") and "accept_candidates(['K" in by_row["M1"] and "add_boq_line citing M1 in FT" in by_row["M1"]
    assert "the app names it '20A 1P GFCI BREAKER'" in by_row["M5"] and "account_rows(['M5']" in by_row["M5"]
    assert f["instructions"] == [by_row[r] for r in ["M1", "M2", "M3", "M5"]]
    ids = [c["id"] for c in session.candidates.values() if c["kind"] in ("measure", "policy") and c["row_ids"] != ["M4"]]
    assert C.apply_write(session, "accept_candidates", {"ids": ids, "overrides": []})["accepted"] == 4
    fin2 = C.apply_write(session, "finish", {"confidence_notes": []})
    assert not any(x["gate"] == "orphan_rows" and x["target"].get("kind") == "measure" for x in fin2["findings"] + fin2["blocking_routed"])
    assert not any(r[0] == "M" for r in fin2["unaccounted_rows"])
    assert "M4" not in session.unaccounted_rows()
    C.reset_sessions()


def test_rfi_the_luminaire_schedule_answers_is_refused(session):
    bad = _refused(C.apply_write(session, "add_rfi", {"text": "Confirm L5 linear lengths and confirm L5 local control.", "cite": "E0.04"}), C.DECIDED_BY_SCHEDULE)
    assert bad["detail"] == {"tag": "L5", "control": "LOCAL", "schedule_ref": "S9#r5"} and "LOCAL" in bad["message"]
    # L4's control cell is unreadable in this read, so the question stands; other RFIs are untouched
    assert C.apply_write(session, "add_rfi", {"text": "Confirm the L4 control method.", "cite": "E0.04"})["added"]["text"] == "Confirm the L4 control method."
    assert C.apply_write(session, "add_rfi", {"text": "Confirm the number of exit sign faces.", "cite": "E0.04"})["count"] == 2
    assert C.apply_write(session, "add_risk", {"text": "L5 control wiring may be local.", "cite": "E0.04"})["count"] == 1
    assert session.schedule_decides("") is None


def _controls_fact() -> Fact:
    return Fact(id="F4", system="hvac_controls", statement="ATC wiring by the temperature controls contractor.", party="other trade",
                verified=True, cite=FactCite(document_id="m", page=7, quote="ALL ATC WIRING BY TEMPERATURE CONTROLS CONTRACTOR."),
                kind="provided_by_others")


def test_refused_candidates_and_orphans_carry_a_resolving_instruction(session):
    """The first live iteration offered C19 (an EA count of the lighting
    track rows) as a candidate refused unit_mismatch, and both finish calls
    listed the orphan ids with no instruction, so the row stayed an orphan.
    A refused candidate and every orphan now name the call that resolves
    them; the gate itself is unchanged."""
    view = C.propose_view(session)
    refused = [c for c in view["candidates"] if c.get("refusal")]
    track = next(c for c in refused if "TRACK" in c["description"])
    assert track["refusal"]["code"] == "unit_mismatch" and track["qty"] is None
    text = track["refusal"]["instruction"]
    assert track["refs"][0] in text and "account_rows" in text and "info_only" in text and "add_risk" in text
    assert "follow its instruction" in view["note"]
    result = C.apply_write(session, "finish", {"confidence_notes": []})
    assert result["passed"] is False
    ids = result["unaccounted_rows"]
    assert ids and [i["row_id"] for i in result["orphan_instructions"]] == ids
    orphan = next(f for f in result["findings"] if f["gate"] == "orphan_rows")
    assert len(orphan["instructions"]) == min(40, len(orphan["target"]["row_ids"])) and all(orphan["instructions"])
    by_row = {i["row_id"]: i["instruction"] for i in result["orphan_instructions"]}
    existing = next(i for i in result["orphan_instructions"] if "to remain" in i["instruction"])
    assert "existing_to_remain" in existing["instruction"] and existing["row_id"] in {u["id"] for u in session.unproposed}
    assert "account_rows" in by_row[track["refs"][0]] and "measure" in by_row[track["refs"][0]]
    accept = next(i for i in result["orphan_instructions"] if "accept_candidates" in i["instruction"])
    cid = re.search(r"accept_candidates\(\['(K\d+)'\]\)", accept["instruction"]).group(1)
    assert accept["row_id"] in session.candidates[cid]["row_ids"]
    # a linear row with a measured M row of its class is told to cite that row
    session.rows["M9"] = {"row_id": "M9", "kind": "M", "description": "LIGHTING TRACK", "quantity": 40.0, "unit": "FT",
                          "status": "active", "basis": "measured_route", "coverage": "derived", "sheet": {"sheet_number": "ED3.01"}}
    assert "M9" in C.row_instruction(session, [track["refs"][0]])
    # a row of a system a verified fact excludes is told to account it by_others with the fact
    session.facts.append(_controls_fact())
    session.facts_by_id["F4"] = session.facts[-1]
    session.rows["C999"] = {"row_id": "C999", "kind": "C", "description": "DDC controller", "quantity": 2, "unit": "EA",
                            "status": "active", "coverage": "sheet_total", "sheet": {"sheet_number": "M2.01", "sheet_key": "D2P7"}}
    assert "account_rows(['C999'], 'by_others', fact_id='F4')" in C.row_instruction(session, ["C999"])


def test_a_policy_inclusion_beats_an_excluding_fact_at_write_time(run7154, merged):
    """Q14 at add_boq_line: the first live iteration refused the model's
    thermostat relocation line with excluded_by_fact (F4 hvac_controls)
    although the binding inclusion carries it, and the row stayed an
    orphan. The line is accepted, finish records fact_conflict (warn) and
    no excluded_system; a system no inclusion carries is still refused."""
    C.reset_sessions()
    session = C.Session(make_inputs(run7154, merged, facts=[honeywell_fact(), _controls_fact()]), group_id=GROUP)
    C.register_session(session, group_id=GROUP)
    try:
        rid = _first(session, lambda r: "thermostat" in C._row_description(r).lower() and "relocat" in C._row_description(r).lower())
        line = _line(description="RELOCATION OF THERMOSTAT TO NEW LOCATION", subgroup="MISCELLANEOUS",
                     source_refs=[{"ref": rid, "multiplier": 1}])
        res = C.apply_write(session, "add_boq_line", line)
        assert res.get("refused") is not True and res["quantity"] == session.rows[rid]["quantity"], res
        assert "carried by G3 policy inclusion" in C.row_instruction(session, [rid]) or session.consumed_by(rid) == res["line_id"]
        refused = _refused(C.apply_write(session, "add_boq_line", _line(description="DDC CONTROLLER", source_refs=[{"ref": "C13", "multiplier": 1}])), "excluded_by_fact")
        assert "account_rows" in refused["message"] and "by_others" in refused["message"]
        result = C.apply_write(session, "finish", {"confidence_notes": []})
        assert any(w["gate"] == "fact_conflict" and w["target"].get("line_id") == res["line_id"] for w in result["warnings"])
        assert not any(f["gate"] == "excluded_system" for f in result["findings"] + result["blocking_routed"])
    finally:
        C.reset_sessions()


# ---------------------------------------------------------------------------
# The A/B harness reproduces the live spine (sweep a064650c, dev run b6e898c7)
# ---------------------------------------------------------------------------
#
# Measured on 2026-10-22: the in-process A/B offered main 18 tools where the
# backend offered 31 (the live round 1 request hash matches only the 31-tool
# list: the startup imports of app/main.py plus price_lookup, dev runs with
# pricing on), read the bundle's v1 rows with no derive outputs (no scope
# digest, 0 facts, no measure, 0 census floors, 35 candidates; live: digest,
# 60 facts, 7 M rows, 11 floors, 42 candidates) and bound none of the WS1,
# measure or policy services. Route A then hit the 2,500-token round cap in
# the middle of a tool call (LlmBadOutput, invalid arguments), which raised
# out of ab_runner and was recorded at $0 although the ledger billed $0.73.


class FixtureBundle:
    """The committed 7154 fixture as a RunBundle (the members
    plans_reader_replay.stored_artifacts and LocalDocBytes read); no source
    PDF is committed, so doc_bytes raises like a missing backup file."""

    def __init__(self, d: dict) -> None:
        from tests.plans_reader_fixtures import FIXTURE_7154 as root

        self.root, self.run, self.documents, self.files = root, d["run"], d["documents"], d["files"]
        self.groups, self.calls, self.chat, self.artifacts = d["groups"], [], [], {}
        self.main_output, self.case_id, self.run_id = d["main"], "7154", d["run"]["id"]
        self._cats = {"electrical_drawings": d["electrical"], "rfp": d["rfp"], "specifications": d["specifications"]}

    @property
    def categories(self) -> list[str]:
        return list(self._cats)

    def category_output(self, category: str) -> dict:
        import copy

        return copy.deepcopy(self._cats[category])

    # live-run members of plans_reader_replay.live_run_artifacts: the
    # committed fixture has none of them
    output_versions: dict = {}
    run_columns: dict = {}
    notes: list = []
    gate_finding_rows: list = []
    quote_rows: list = []
    pricing_rows: list = []
    recount_rows: list = []
    signoff_rows: list = []

    def latest_deliverable(self, kind: str) -> None:
        return None

    def chat_probe(self) -> None:
        return None

    def scope_output(self, scope: str) -> None:
        return None

    def has_member(self, name: str) -> bool:
        return False

    def latest_pricing(self) -> None:
        return None

    def latest_review(self) -> None:
        return None

    def doc_bytes(self, document_id: str, *, backup_dir: Any = None) -> bytes:
        raise FileNotFoundError(f"no source PDF for {document_id} in the committed fixture")


def _fixture_facts() -> dict:
    from app.services.pagefacts.contracts import DocFacts
    from tests.plans_reader_fixtures import MECH_DOC_7154

    pf = json.loads((FIXTURE_7154 / "page_facts_elec.json").read_text())
    return {ELEC_DOC_7154: DocFacts.from_dict(pf["documents"][0]["facts"]), MECH_DOC_7154: DocFacts.from_dict(pf["documents"][1]["facts"])}


@pytest.fixture
def prepared7154(run7154, monkeypatch):
    """ab_prepare on the fixture: the WS1 page facts of the two drawing
    documents (the committed facts; the PDFs are not committed), the real
    merge stage and the derive chain of the replay registry (WS5 scope part
    chained before the WS3 measure part, the art handed along). Both derive
    parts read the source PDFs, so here they answer the synthetic Honeywell
    context and measure output of this file; the chain itself is real and
    test_ab_prepare_runs_the_real_derive_chain_on_the_fixture runs it
    unstubbed."""
    from app.services import plans_reader_measure, scope_engine

    seen: list[str] = []

    def scope_part(rctx, art):
        seen.append("scope")
        return {"scope_context": scope_context([honeywell_fact(), unverified_fact()])}

    def measure_part(rctx, art):
        seen.append("measure" if isinstance(art.get("scope_context"), dict) else "measure without scope")
        return {"measure": measure_output()}

    monkeypatch.setattr(scope_engine, "replay_prepare_stage", scope_part)
    monkeypatch.setattr(plans_reader_measure, "_replay_derive", measure_part)
    prepared = C.ab_prepare(FixtureBundle(run7154), _settings(), page_facts=_fixture_facts())
    assert seen == ["scope", "measure"]
    return prepared


def test_ab_prepare_runs_the_real_derive_chain_on_the_fixture(run7154):
    """Unstubbed: the real merge and the real WS5 + WS3 derive chain run on
    the fixture (no PDFs: the documents without bytes are noted, never
    fabricated) and write the v2 rows, the scope context with its digest
    and a measure output."""
    prepared = C.ab_prepare(FixtureBundle(run7154), _settings(), page_facts=_fixture_facts())
    art = prepared.art
    assert {"measure", "scope_context"} <= set(art) and art["scope_context"]["digest"].startswith("G3 SCOPE DECISIONS")
    assert art["category:electrical_drawings"].get("floors") and art["category:rfp"].get("floors") in (None, [])
    assert any("has no local bytes" in n for n in prepared.notes)
    assert prepared.inputs.scope_context is art["scope_context"] and prepared.inputs.measure is art["measure"]


class LiveDb:
    """The rows a live run holds after its derive group (documents, the
    category, policy and measure outputs, the groups), served with the
    query shapes load_inputs issues."""

    def __init__(self, prepared: C.AbPrepared) -> None:
        art, run_id = prepared.art, prepared.inputs.run_id
        outputs = [{"run_id": run_id, "scope": "category", "category": k.split(":", 1)[1], "version": 1, "output": v}
                   for k, v in art.items() if k.startswith("category:")]
        outputs.append({"run_id": run_id, "scope": "policy", "category": None, "version": 1, "output": art["scope_context"]})
        outputs.append({"run_id": run_id, "scope": "measure", "category": None, "version": 1, "output": art["measure"]})
        self.tables = {
            "plans_reader_documents": [{**d, "run_id": run_id} for d in art["documents"]],
            "plans_reader_outputs": outputs,
            "plans_reader_groups": [{**g, "run_id": run_id} for g in art["groups"]],
        }

    def table(self, name: str) -> Any:
        return C._AbQuery(self.tables.get(name, []))


def _live_first_request(prepared: C.AbPrepared, settings: Settings, monkeypatch) -> llm_parts.ModelRequest:
    """The first request of the live spine for a run whose derive outputs
    are stored: build_main_loop over the database rows and the LoopContext
    of plans_reader_agents._run_loop_group (its offered tools and pricing
    rule written out here, not through the A/B helpers)."""
    model = RecordingModel()
    monkeypatch.setattr(llm, "complete_turn", model)
    db = LiveDb(prepared)
    run = {**prepared.inputs.run, "settings_snapshot": {"routes": {"main": dict(AB_ROUTE, tools=True)}}}
    group = {"id": "live-" + GROUP[5:], "run_id": run["id"], "kind": "main"}
    store = FakeLoopStore(run_id=run["id"])
    C.reset_sessions()
    parts, precalls = C.build_main_loop(db, run, group, settings, store=store)
    pricing_allowed = bool(getattr(settings, "ai_pricing_enabled", False) and settings.ai_pricing_agent_tools_enabled)
    tool_ctx = ToolContext(run_id=run["id"], kind="main", group_id=group["id"], services=fake_services())
    ctx = loop.LoopContext(
        run=run, kind="main", feature="plans_main", system=C.CONSOLIDATOR_SYSTEM, user_parts=parts,
        tools=offered_tools("main", mode="write", pricing_allowed=pricing_allowed), tool_ctx=tool_ctx, store=store,
        response_schema=None, schema_name="plans_main_tools", route=dict(AB_ROUTE, tools=True), step="main",
        group_id=group["id"], precalls=precalls, settings=settings, run_spent_usd=Decimal("0"),
        stop_when=lambda: C.session_finished(tool_ctx),
    )
    loop.run_loop(ctx)
    C.reset_sessions()
    return model.requests[0]


BACKEND_ONLY_TOOLS = {"census", "explain_quantity", "get_facts", "get_schedule", "get_scope_policy", "keynote_lookup",
                      "legend_lookup", "measure_polyline", "sheet_index", "spec_lookup", "synthesize_routes", "text_search"}


def test_ab_tool_modules_are_the_backend_startup_imports_and_register_the_live_tools():
    """Every module of AB_TOOL_MODULES is imported by the backend at startup
    (app/main.py), and importing them offers main the tools the in-process
    A/B was missing; with pricing on (dev) price_lookup too."""
    from pathlib import Path

    main_py = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    for name in C.AB_TOOL_MODULES:
        package, _, leaf = name.rpartition(".")
        assert re.search(rf"from {re.escape(package)} import [^\n]*\b{leaf}\b", main_py), name
    names = set(C.ab_register_tools())
    assert BACKEND_ONLY_TOOLS <= names and len(names) == 30, sorted(names)
    priced = Settings(_env_file=None, openai_api_key="o-key", ai_pricing_enabled=True, ai_pricing_agent_tools_enabled=True)
    assert C.ab_pricing_allowed(priced) and not C.ab_pricing_allowed(_settings())
    assert {s.name for s in offered_tools("main", mode="write", pricing_allowed=True)} - names == {"price_lookup"}


def test_ab_prepare_derives_the_inputs_the_live_main_group_reads(prepared7154, run7154):
    """The bundle's v1 outputs become the v2 rows the import stores (C 56,
    O 50, P 37, Q 38, S 14 as on dev run b6e898c7), the census floors come
    from the page facts, the derive chain writes the measure (M rows) and
    the scope context (digest, facts, breaker plan) and the session sees
    all of it."""
    inputs = prepared7154.inputs
    session = C.Session(inputs, group_id="prep")
    kinds = {k: sum(1 for r in session.rows if r[0] == k) for k in "CDOPQS"}
    # The replay merge numbers every category on one run sequence, so the
    # rfp and specifications rows keep their own ids and the session holds
    # all of them with no collision alias (dev run b6e898c7, merged from 1
    # per category, dropped them: C 56, D 4, O 50, P 37, Q 38, S 14 survived).
    assert kinds == {"C": 56, "D": 4, "O": 111, "P": 73, "Q": 61, "S": 19}
    assert session.row_aliases == {} and not any("original_row_id" in r for r in session.rows.values())
    assert [r["row_id"] for r in inputs.category_outputs["electrical_drawings"]["counts"]] == [f"C{i}" for i in range(1, 57)]
    assert session.floors and any(r.startswith("M") for r in session.rows)
    assert inputs.measure and inputs.measure.get("quantities") and inputs.scope_context["digest"].startswith("G3 SCOPE DECISIONS")
    assert [f.id for f in inputs.facts] == ["F1", "F2"] and session.policy_rows
    parts = C.user_parts_for(session, _settings())
    texts = [p.text for p in parts]
    assert texts[0].startswith("G3 SCOPE DECISIONS") and any(t.startswith("MEASURE OUTPUT (app-computed") for t in texts)
    assert not any("none recorded for this run" in t for t in texts)
    # the WS1 facts are in memory: documents with facts done, the drawing documents only (no PDFs committed)
    assert {d["id"] for d in prepared7154.facts_store.documents} == set(_fixture_facts())
    assert all(d["facts_status"] == "done" and d["run_id"] == inputs.run_id for d in prepared7154.facts_store.documents)
    assert any("has no facts" in n for n in prepared7154.notes)
    # the derive stages ran through the replay registry and left it as it was
    from app.services.plans_reader_replay import registered_stages as stages_now

    assert stages_now()["derive"].owner != "WS5"


def test_ab_services_bind_ws1_measure_and_policy_like_run_services(prepared7154):
    """The loop's read tools answer from the bundle's facts, the derived
    measure and the derived scope context (they refused or were missing
    in the A/B before)."""
    C.ab_register_tools()
    inputs = prepared7154.inputs
    services = C.ab_services(inputs, prepared=prepared7154)
    assert {"text", "census", "schedules", "legend", "measure", "policy"} <= set(services.available())
    ctx = ToolContext(run_id=inputs.run_id, kind="main", group_id="svc", services=services)
    with prepared7154.facts_store.serving():
        sheets = dispatch(REGISTRY["sheet_index"], ctx, {})
        legend = dispatch(REGISTRY["legend_lookup"], ctx, {"query": None, "effect": None})
        census = dispatch(REGISTRY["census"], ctx, {"document_id": ELEC_DOC_7154, "page": 7, "pattern": {"kind": "tag", "value": "X"}, "region": None})
        text = dispatch(REGISTRY["text_search"], ctx, {"query": "PANEL", "document_id": None, "pages": None, "sheets": None,
                                                        "mode": "token", "limit": 3, "offset": None})
    assert not sheets.refused and sheets.data["total"] >= 10 and {s["document_id"] for s in sheets.data["sheets"]} >= {ELEC_DOC_7154}
    assert not legend.refused and legend.data["entries"]
    assert not census.refused and census.data["method"] == "text_layer"
    # words come from the source PDF, which the fixture does not commit: unknown, never fabricated
    assert text.data.get("error") == "facts_unavailable" or text.data.get("hits") == []
    m_id = next(q["row_id"] for q in inputs.measure["quantities"])
    explained = dispatch(REGISTRY["explain_quantity"], ctx, {"row_id": m_id})
    assert not explained.refused and explained.data["row"]["row_id"] == m_id
    facts = dispatch(REGISTRY["get_facts"], ctx, {"system": None})
    assert not facts.refused and [f["id"] for f in facts.data["facts"]] == ["F1", "F2"]
    rules = dispatch(REGISTRY["get_scope_policy"], ctx, {"topic": None, "item_description": None})
    assert not rules.refused and rules.data["rules"] and rules.data["bid_conditions"]["wage_type"] is None


def test_ab_runner_sends_the_live_first_turn_and_tools_and_passes_finish(prepared7154, quiet, monkeypatch):
    """The A/B route's first request equals the live spine's for a run whose
    derive outputs are stored (same system, tools, user parts with the
    pre-call block and output cap: request_sha256 over all of them, the
    hash the live loop stores per round), and the scripted consolidator
    passes finish with lines on it."""
    settings = _settings()
    live_req = _live_first_request(prepared7154, settings, monkeypatch)
    model = RecordingModel()
    monkeypatch.setattr(llm, "complete_turn", model)
    out = C.ab_runner(_ab_task(bundle="bundles/7154/fixture", case_id="7154"), AB_ROUTE, settings, prepared=prepared7154, sb=None)
    ab_req = model.requests[0]
    assert [t.name for t in ab_req.tools_offered] == [t.name for t in live_req.tools_offered]
    assert BACKEND_ONLY_TOOLS <= {t.name for t in ab_req.tools_offered}
    assert [p.text for p in ab_req.messages[0].parts] == [p.text for p in live_req.messages[0].parts]
    assert loop.request_sha256(ab_req) == loop.request_sha256(live_req)
    first = ab_req.messages[0].parts
    assert first[0].text.startswith("G3 SCOPE DECISIONS") and "FACTS (F rows; a verified fact" in first[2].text
    assert '"floors":[{' in first[-1].text and '"M' in first[-1].text  # census floors and M row candidates in the pre-call block
    answer = out["answer"]
    assert out["error"] is None and out["attempts"] == 1 and out["retried"] == []
    assert answer["passed"] is True and answer["finished"] is True and answer["lines"] > 0 and answer["unaccounted_rows"] == 0
    assert out["cost_usd"] == pytest.approx(_expected_cost(model.turns), abs=1e-5)
    assert C._SESSIONS == {}


def test_ab_runner_prepares_a_bundle_task_with_the_script_roots(run7154, quiet, monkeypatch, tmp_path):
    """A bundle task goes through ab_prepare with the loaded bundle and the
    script's --backup."""
    from app.services import eval_bundle

    seen: dict = {}

    class Stop(Exception):
        pass

    monkeypatch.setattr(eval_bundle, "load_bundle", lambda path: FixtureBundle(run7154))

    def fake_prepare(bundle, settings, *, backup_dir=None, **_kw):
        seen.update(bundle=bundle, backup_dir=backup_dir)
        raise Stop

    monkeypatch.setattr(C, "ab_prepare", fake_prepare)
    with pytest.raises(Stop):
        C.ab_runner(_ab_task(bundle="bundles/7154/x"), AB_ROUTE, _settings(), sb=None, eval_root=tmp_path, backup_dir=tmp_path / "b")
    assert isinstance(seen["bundle"], FixtureBundle) and seen["backup_dir"] == tmp_path / "b"


class CutOffModel(RecordingModel):
    """Round `fail_on` raises the cut-off tool call of route A (LlmBadOutput,
    invalid arguments) `times` times, after publishing the billed cost of
    that call as llm._log does (llm.last_cost_usd())."""

    def __init__(self, *, fail_on: int, times: int, cost: str = "0.167791") -> None:
        super().__init__()
        self.fail_on, self.times, self.cost, self.failures = fail_on, times, Decimal(cost), 0

    def __call__(self, feature, req, *, settings=None):
        llm._reset_last()  # as llm.complete_turn does before anything can raise
        if self.round + 1 == self.fail_on and self.failures < self.times:
            self.failures += 1
            self.requests.append(req)
            llm._last_usage.set(llm.LlmUsage(input_tokens=31568, cached_tokens=30321, output_tokens=2500))
            llm._last_cost.set(self.cost)
            raise llm.LlmBadOutput("Model returned a tool call with invalid arguments. Retry the generation.")
        return super().__call__(feature, req, settings=settings)


def _no_retry_settings() -> Settings:
    return Settings(_env_file=None, openai_api_key="o-key", plans_reader_enabled=True, plans_reader_gates_blocking=True,
                    ai_scope_policy_enforce=True, plans_reader_consolidator_mode="tools", llm_queue_retry_delays="")


def test_ab_runner_failure_reports_the_billed_cost_and_the_error(run7154, merged, quiet, monkeypatch):
    """Route A on 2026-10-22: a provider error the queue would not retry
    again is reported, not raised: error set, and cost_usd is what was
    billed (the done rounds plus the failed call's llm.last_cost_usd()),
    never 0."""
    model = CutOffModel(fail_on=2, times=9)
    monkeypatch.setattr(llm, "complete_turn", model)
    settings = _no_retry_settings()
    C.reset_sessions()
    out = C.ab_runner(_ab_task(), AB_ROUTE, settings, inputs=make_inputs(run7154, merged, settings=settings), sb=None,
                      services=fake_services())
    assert out["error"] == "LlmBadOutput: Model returned a tool call with invalid arguments. Retry the generation."
    assert out["stop_reason"] == "error" and out["attempts"] == 1 and out["rounds"] == 1
    billed = _expected_cost(model.turns) + float(model.cost)
    assert model.turns and out["cost_usd"] == pytest.approx(billed, abs=1e-6) and out["cost_usd"] > float(model.cost)
    assert out["answer"] is not None and out["answer"]["passed"] is False  # what the session held after round 1
    assert C._SESSIONS == {}


def test_ab_runner_retries_a_transient_error_like_the_queue_and_resumes(run7154, merged, quiet, monkeypatch):
    """The live queue re-runs the main group job on a transient error and
    run_loop resumes from the stored rounds; the A/B does the same, so the
    cut-off round is re-sent and the loop finishes. The failed attempt's
    cost stays in the route's cost."""
    model = CutOffModel(fail_on=2, times=1)
    monkeypatch.setattr(llm, "complete_turn", model)
    settings = _settings()
    C.reset_sessions()
    out = C.ab_runner(_ab_task(), AB_ROUTE, settings, inputs=make_inputs(run7154, merged, settings=settings), sb=None,
                      services=fake_services())
    assert out["error"] is None and out["attempts"] == 2 and len(out["retried"]) == 1 and "LlmBadOutput" in out["retried"][0]
    assert out["answer"]["passed"] is True and out["answer"]["lines"] > 0
    assert out["cost_usd"] == pytest.approx(_expected_cost(model.turns) + float(model.cost), abs=1e-6)
    # the resumed loop never repeated a write: one stored call per call id
    assert C._SESSIONS == {}


def test_ab_runner_attaches_the_spend_to_a_spend_cap_refusal(run7154, merged, quiet, monkeypatch):
    model = RecordingModel()

    def capped(feature, req, *, settings=None):
        llm._reset_last()
        if model.round >= 1:
            raise ai_spend.SpendCapReached("daily cap reached", cap_usd=Decimal("50"), spent_usd=Decimal("50"))
        return model(feature, req, settings=settings)

    monkeypatch.setattr(llm, "complete_turn", capped)
    settings = _settings()
    C.reset_sessions()
    with pytest.raises(ai_spend.SpendCapReached) as caught:
        C.ab_runner(_ab_task(), AB_ROUTE, settings, inputs=make_inputs(run7154, merged, settings=settings), sb=None,
                    services=fake_services())
    assert caught.value.cost_usd == pytest.approx(_expected_cost(model.turns), abs=1e-6) and caught.value.cost_usd > 0
    assert C._SESSIONS == {}


# ---------------------------------------------------------------------------
# A count row of a separate item is never folded into a measured FT line
# ---------------------------------------------------------------------------

_DEV_DOC = "ef16fe2a-aa6a-48c0-b9a7-a87f28d5d6a4"
# Dev run 51ec302b (integration run 1 of 7154): the ED3.01 demolition rows of
# the electrical category output v2 (after the WS4 recount fix: one C27, no
# C54) and the measured track row M5. Em dashes of the stored wording are
# commas here.
_DEV_DEMO_ROWS = [
    ("C21", "Remove existing rectangular recessed light fixtures", "(X)", 17, "sheet_total"),
    ("C22", "Remove existing track-light heads, visible subset", "(X), keynote 1", 8, "tile_subtotal"),
    ("C23", "Remove existing round downlights", "(X)", 13, "sheet_total"),
    ("C24", "Remove existing linear light fixture assemblies", "(X)", 2, "sheet_total"),
    ("C25", "Remove existing track-lighting assemblies", "(X), keynote 1", 3, "sheet_total"),
    ("C26", "Remove existing entry light/exit-sign fixtures", "(X), keynote 1", 2, "sheet_total"),
    ("C27", "Remove existing light switches and control wiring", "S (X), keynote 2", 2, "sheet_total"),
    ("C28", "Remove existing entry light fixtures, type unconfirmed", "(X), keynote 1", 2, "sheet_total"),
]


def _dev_demo_output() -> dict:
    sheet = {"page": 8, "sheet_key": "D2P8", "document_id": _DEV_DOC, "sheet_number": "ED3.01"}
    counts = [
        {"row_id": rid, "description": desc, "symbol_or_tag": tag, "quantity": qty, "unit": "EA", "basis": "counted_symbols",
         "coverage": cov, "status": "active", "document_id": _DEV_DOC, "page": 8, "sheet_number": "ED3.01", "sheet": dict(sheet),
         "origin": {"group_id": "g", "group_kind": "sub", "pass": 1, "local_id": rid.lower()}, "flags": [], "alternates": [], "superseded": []}
        for rid, desc, tag, qty, cov in _DEV_DEMO_ROWS
    ]
    out = {"category": "electrical_drawings", "rows_version": 2, "summary": "", "pages": [], "counts": counts, "floors": [], "merge_log": {}}
    for key in ("runs", "scope", "specs", "schedules", "details", "questions"):
        out[key] = []
    return out


def _dev_demo_measure() -> dict:
    measure = measure_output()
    measure["quantities"] = [
        {"row_id": "M4", "group": "LIGHTING FIXTURES", "subgroup": "FIXTURES", "description": "L5 UNDERCABINET LED LIGHTING", "quantity": 43.34,
         "unit": "FT", "wastage_applies": False, "basis": "measured_route", "caveat": None, "sheet_refs": ["E3.01"], "line_hint": {"kind": "luminaire_run"}},
        {"row_id": "M5", "group": "DEMOLITION", "subgroup": "", "description": "Remove existing track-lighting assemblies", "quantity": 50.01,
         "unit": "FT", "wastage_applies": False, "basis": "measured_route", "caveat": None, "sheet_refs": ["ED3.01"],
         "line_hint": {"tag": "(X), keynote 1", "area": "DEMOLITION", "kind": "linear_demolition", "group": "DEMOLITION", "subgroup": "", "replaces_unit": "EA"}},
    ]
    return measure


def _dev_demo_inputs(run7154, settings: Any = None) -> C.ConsolidatorInputs:
    facts = [honeywell_fact()]
    return C.ConsolidatorInputs(
        run=run7154["run"], documents=run7154["documents"], category_outputs={"electrical_drawings": _dev_demo_output()},
        measure=_dev_demo_measure(), scope_context=scope_context(facts), facts=facts, ledger=run7154["run"]["ledger"],
        groups=[], settings=settings or dict(SETTINGS),
    )


def _cand_of(session: C.Session, rid: str) -> dict:
    return next(c for c in session.candidates.values() if rid in c["row_ids"])


def test_demolition_candidates_keep_separate_items_and_measured_lines_refuse_count_rows(run7154):
    """Run 51ec302b folded the track HEADS count (C22, EA) into the measured
    track line (M5, FT) and dropped the entry light row: the proposal put
    C21, C22, C26 and C28 in one candidate named after the rectangular
    fixtures. A demolished item of its own wording is now its own candidate,
    a line citing an M row refuses a count row (unit_mismatch) and so does
    an account_rows that says an M row of another item carries it."""
    C.reset_sessions()
    session = C.Session(_dev_demo_inputs(run7154), group_id=GROUP)
    heads = _cand_of(session, "C22")
    assert heads["row_ids"] == ["C22"] and heads["section"] == "DEMOLITION" and heads["unit"] == "EA"
    assert heads["combine"] == "floor" and heads["quantity"] == 8 and not heads.get("refusal")
    assert _cand_of(session, "C21")["row_ids"] == ["C21"]
    entry = _cand_of(session, "C26")
    assert "C21" not in entry["row_ids"] and "C22" not in entry["row_ids"] and entry["section"] == "DEMOLITION"
    track = _cand_of(session, "M5")
    assert (track["row_ids"], track["description"], track["unit"], track["quantity"]) == (["M5"], C.TRACK_REMOVAL_DESCRIPTION, "FT", 50.01)
    # a line citing an M row and a count row is refused with the call that carries the count row
    folded = _line(section="DEMOLITION", group="DEMOLITION", subgroup="", unit="FT", material_category="Demolition", combine="sum",
                   description="REMOVE EXISTING TRACK-LIGHTING ASSEMBLIES INCLUDING HEADS",
                   source_refs=[{"ref": "M5", "multiplier": 1}, {"ref": "C22", "multiplier": 1}])
    bad = _refused(C.apply_write(session, "add_boq_line", folded), "unit_mismatch")
    assert bad["detail"]["row_ids"] == ["C22"] and f"accept_candidates(['{heads['id']}'])" in bad["message"]
    # M rows of two items in one line
    mixed = {**folded, "description": "REMOVAL OF LIGHTING TRACK", "source_refs": [{"ref": "M5", "multiplier": 1}, {"ref": "M4", "multiplier": 1}]}
    assert _refused(C.apply_write(session, "add_boq_line", mixed), "unit_mismatch")["detail"]["row_ids"] == ["M5", "M4"]
    # accounting the heads as carried by M5 is the same fold; the EA track row of the measured item itself may be accounted
    fold = {"row_ids": ["C22"], "disposition": "info_only", "reason": "Track assemblies measured by M5 and carried in FT including heads.",
            "fact_id": None, "duplicate_of": None, "note_ref": None}
    assert _refused(C.apply_write(session, "account_rows", fold), "unit_mismatch")["detail"] == {"row_id": "C22", "measured": ["M5"]}
    assert "C22" not in session.accounted
    ok = C.apply_write(session, "account_rows", {**fold, "row_ids": ["C25"], "disposition": "superseded_by_schedule", "reason": "measured by M5"})
    assert ok["accounted"] == ["C25"]
    assert C.apply_write(session, "account_rows", {**fold, "reason": "visible subset only; see the census"})["accounted"] == ["C22"]
    # the measured line on its own keeps the app's name
    got = C.apply_write(session, "add_boq_line", {**folded, "combine": "single", "source_refs": [{"ref": "M5", "multiplier": 1}]})
    assert session.book.lines[got["line_id"]]["description"] == C.TRACK_REMOVAL_DESCRIPTION
    assert "only M rows of the same item" in C.CONSOLIDATOR_SYSTEM and "track heads beside the measured track" in C.CONSOLIDATOR_SYSTEM
    C.reset_sessions()


def test_fixture_track_heads_are_their_own_demolition_candidate(session):
    heads = _first(session, lambda r: "track-light head" in r["description"])
    cand = _cand_of(session, heads)
    assert cand["row_ids"] == [heads] and cand["section"] == "DEMOLITION" and cand["quantity"] == 12
    # rows of one item across sheets stay one candidate (the duplex demolition of two sheets)
    assert any(len(c["row_ids"]) > 1 for c in session.candidates.values() if c["section"] == "DEMOLITION")


def test_scripted_loop_on_the_run_rows_carries_heads_entry_lights_and_track_separately(run7154, quiet, monkeypatch):
    """The scripted-model harness (accept every candidate, account the
    leftovers) over the run's stored ED3.01 rows: the result carries a track
    heads EA line, the entry light row and the M5 FT line, each its own."""
    model = ScriptedModel()
    monkeypatch.setattr(llm, "complete_turn", model)
    settings = _settings()
    inputs = _dev_demo_inputs(run7154, settings)
    store = FakeLoopStore(run_id=inputs.run_id)
    ctx, session = _loop_ctx(store, inputs, settings)
    result = loop.run_loop(ctx)
    assert result.status == "done"
    output = C.finalize_main_loop(result.parsed, result, sb=None, run=inputs.run, group={"id": GROUP})
    lines = G.boq_lines_of(output)
    by_ref = {r["ref"]: ln for ln in lines for r in ln["source_refs"]}
    heads, entry, track = by_ref["C22"], by_ref["C26"], by_ref["M5"]
    assert heads["unit"] == "EA" and heads["quantity"] == 8 and heads["section"] == "DEMOLITION"
    assert [r["ref"] for r in heads["source_refs"]] == ["C22"]
    assert entry["unit"] == "EA" and "C21" not in [r["ref"] for r in entry["source_refs"]]
    assert track["unit"] == "FT" and track["quantity"] == 50.01 and [r["ref"] for r in track["source_refs"]] == ["M5"]
    assert track["description"] == C.TRACK_REMOVAL_DESCRIPTION and len({id(heads), id(entry), id(track)}) == 3
    demo_ea = sum(ln["quantity"] for ln in lines if ln["section"] == "DEMOLITION" and ln["unit"] == "EA")
    assert demo_ea == 17 + 8 + 13 + 2 + 2 + 2 + 2  # every demolished item of ED3.01 except the EA track row
    assert L.recheck(lines, {**session.rows}) == []


def test_device_lines_are_named_with_the_estimator_vocabulary(session):
    """Write-time rule (spec 3.4): a count line whose cited rows read as a
    standard device is renamed to the estimator's name; the model's wording
    goes to material_note (a caveat would be folded back into the
    description by the renderer and the scorer). 7154 run 2: "WALL DIMMER,
    INCLUDING OCCUPANCY-SENSOR OVERRIDE DIMMERS" -> DIMMER SWITCH and
    "WALL OCCUPANCY-SENSING DIMMER" -> DIMMER & OCCUPANCY SENSOR SWITCH, so
    the scorer pairs them with DIMMER SWITCH 10 and DIMMER & OCCUPANCY
    SENSOR SWITCH 2 instead of reading 13 and 0."""
    from app.services import plans_reader_score as S

    dimmer = _first(session, lambda r: r["description"].startswith("Wall dimmer, excluding"))
    lock = _first(session, lambda r: r["description"] == "Wall dimmer with lockable cover")
    occ_dim = _first(session, lambda r: r["description"] == "Wall occupancy-sensor dimmer")
    occ_sw = _first(session, lambda r: r["description"] == "Wall occupancy-sensor switch")
    ceiling = _first(session, lambda r: r["description"].startswith("Ceiling-mounted dual-technology occupancy sensor"))
    o_prefix = _first(session, lambda r: r["description"] == "New duplex receptacle with O prefix")
    plain = _first(session, lambda r: r["description"].startswith("New duplex receptacle, open symbol"))
    damper = _first(session, lambda r: r["description"] == "Electrical connection to new fire/smoke damper")

    def ctl(desc, ref, **kw):
        return _line(description=desc, group="LIGHTING CONTROLS", subgroup="CONTROLS", source_refs=[{"ref": ref, "multiplier": 1}], **kw)

    got = C.apply_write(session, "add_boq_line", ctl("WALL DIMMER, INCLUDING OCCUPANCY-SENSOR OVERRIDE DIMMERS", dimmer))
    line = session.book.lines[got["line_id"]]
    assert line["description"] == "DIMMER SWITCH" and line["quantity"] == session.rows[dimmer]["quantity"]
    assert line["material_note"] == "Reader wording: WALL DIMMER, INCLUDING OCCUPANCY-SENSOR OVERRIDE DIMMERS." and line["caveat"] is None
    assert any("renamed to the estimator vocabulary" in w for w in got["warnings"])
    got = C.apply_write(session, "add_boq_line", ctl("WALL OCCUPANCY-SENSING DIMMER", occ_dim, material_note="Per E3.01 legend."))
    line = session.book.lines[got["line_id"]]
    assert (line["description"], line["material_note"]) == ("DIMMER & OCCUPANCY SENSOR SWITCH", "Per E3.01 legend. Reader wording: WALL OCCUPANCY-SENSING DIMMER.")
    lock_line = C.apply_write(session, "add_boq_line", ctl("WALL DIMMER WITH LOCKABLE COVER", lock))["line_id"]
    assert session.book.lines[lock_line]["description"] == "DIMMER SWITCH W/ LOCKABLE COVER"
    assert session.book.lines[C.apply_write(session, "add_boq_line", ctl("WALL OCCUPANCY SENSOR SWITCH", occ_sw))["line_id"]]["description"] == "OCCUPANCY SENSOR SWITCH"
    # placement comes from the rows: a ceiling sensor named as a switch by the model is a sensor
    assert session.book.lines[C.apply_write(session, "add_boq_line", ctl("OCCUPANCY SENSOR SWITCH", ceiling))["line_id"]]["description"] == "OCCUPANCY SENSOR"
    # a conforming name is kept without a warning (125V aside); the model's priced modifier survives a row that abbreviates it
    kept = C.apply_write(session, "add_boq_line", _line(description="DUPLEX RECEPTACLE, 125V", source_refs=[{"ref": plain, "multiplier": 1}]))
    assert session.book.lines[kept["line_id"]]["description"] == "DUPLEX RECEPTACLE, 125V" and not kept.get("warnings")
    ctrl = C.apply_write(session, "add_boq_line", _line(description="DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY", source_refs=[{"ref": o_prefix, "multiplier": 1}]))
    assert session.book.lines[ctrl["line_id"]]["description"] == "DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY" and not ctrl.get("warnings")
    conn = C.apply_write(session, "add_boq_line", _line(description="EQUIPMENT CONNECTION TO FIRE/SMOKE DAMPER", subgroup="MISCELLANEOUS", source_refs=[{"ref": damper, "multiplier": 1}]))
    assert session.book.lines[conn["line_id"]]["description"] == "ELECTRICAL CONNECTION"
    # update_boq_line goes through the same rule
    up = C.apply_write(session, "update_boq_line", {"line_id": conn["line_id"], **{k: None for k in _line()}, "description": "ELECTRICAL HOOK-UP, DAMPER"})
    assert session.book.lines[conn["line_id"]]["description"] == "ELECTRICAL CONNECTION" and up["warnings"]
    # rows that read as different devices leave the line alone with a warning
    for lid in (got["line_id"], lock_line):
        C.apply_write(session, "remove_boq_line", {"line_id": lid, "reason": "test"})
    mixed = C.apply_write(session, "add_boq_line", _line(description="LIGHTING CONTROL DEVICES", group="LIGHTING CONTROLS", subgroup="CONTROLS",
                                                         combine="sum", source_refs=[{"ref": occ_dim, "multiplier": 1}, {"ref": lock, "multiplier": 1}]))
    assert session.book.lines[mixed["line_id"]]["description"] == "LIGHTING CONTROL DEVICES"
    assert any("different devices" in w for w in mixed["warnings"])
    # the renamed lines classify to the 7154 truth keys of spec 3.4
    expect = {
        "DIMMER SWITCH": ("dimmer", set()),
        "DIMMER & OCCUPANCY SENSOR SWITCH": ("dimmer", {"OCC"}),
        "DIMMER SWITCH W/ LOCKABLE COVER": ("dimmer", {"LOCK"}),
        "OCCUPANCY SENSOR SWITCH": ("occ_sensor", {"WALL"}),
        "OCCUPANCY SENSOR": ("occ_sensor", {"CEILING"}),
    }
    for desc, (kind, attrs) in expect.items():
        key = S.classify_line(desc, "NEW WORK", "LIGHTING CONTROLS", "EA", subgroup="CONTROLS")
        assert (key.kind, set(key.attrs)) == (kind, attrs), desc


# ---------------------------------------------------------------------------
# 7172 (Five Star smoke run 785ae29e): the run's stored rows and schedule
# ---------------------------------------------------------------------------
#
# tests/fixtures/plans_reader/run_7172_main holds the stored category
# outputs (counts and schedules, positions stripped), the measure and policy
# outputs, the documents and the run ledger of the dev run, plus the 40
# count lines of the case truth as quantities (no dollars anywhere).

FIXTURE_7172 = FIXTURE_7154.parent / "run_7172_main"


def _load_7172(name: str) -> Any:
    return json.loads((FIXTURE_7172 / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def inputs7172() -> C.ConsolidatorInputs:
    run = _load_7172("run")
    art = {
        "category:electrical_drawings": _load_7172("category_electrical_drawings"),
        "category:general_drawings": _load_7172("category_general_drawings"),
        "category:mechanical_drawings": _load_7172("category_mechanical_drawings"),
        "measure": _load_7172("measure"), "policy": _load_7172("policy"), "documents": _load_7172("documents"),
        "ledger": run["ledger"], "groups": [],
    }
    return C.inputs_from_artifacts(art, run, settings=dict(SETTINGS))


@pytest.fixture
def session7172(inputs7172) -> C.Session:
    C.reset_sessions()
    s = C.Session(C._fresh_inputs(inputs7172), group_id="g7172")
    C.register_session(s, group_id="g7172")
    yield s
    C.reset_sessions()


def _cand_rows(session: C.Session, *row_ids: str) -> dict:
    return next(c for c in session.candidates.values() if c["row_ids"] == list(row_ids))


def _cite(*row_ids: str) -> list[dict]:
    return [{"ref": r, "multiplier": 1} for r in row_ids]


def test_7172_row_id_collisions_are_recovered_not_dropped(session7172):
    """Every category of the run was merged from C1, so the general (C1-C5)
    and mechanical (C1-C20) rows collided with the electrical ids and the
    live session dropped the electrical schedule counts, panels A and B, the
    transformer and all seven breaker rows (first id wins). The session now
    keeps every row: the later category's rows are re-numbered on the run
    sequence in a fixed order, carry original_row_id, and the gates see
    the same ids through gate_outputs. The policy scope output of a live
    bundle is the WS5 scope context (the breaker plan row M4)."""
    s = session7172
    c_rows = sorted((r for r in s.rows if r[0] == "C"), key=C._row_order)
    assert len(c_rows) == 62 + 5 + 20 and c_rows[-1] == "C87"
    assert s.rows["C14"]["description"].startswith("New panel A") and s.rows["C23"]["quantity"] == 54
    assert s.row_aliases["C63"] == "C1" and s.rows["C63"]["original_row_id"] == "C1" and s.rows["C63"]["category"] == "general_drawings"
    assert s.row_aliases["C68"] == "C1" and s.rows["C68"]["category"] == "mechanical_drawings"
    assert len(s.row_aliases) == 25 and all(k[0] == "C" for k in s.row_aliases)  # the fixture keeps only the counts of the other categories
    stored = {str(r["row_id"]) for out in s.inputs.category_outputs.values() for r in out.get("counts") or []}
    assert "C63" not in stored  # the stored outputs are untouched
    gated = {str(r["row_id"]) for out in s.gate_outputs for r in out.get("counts") or []}
    assert {"C63", "C68", "C87"} <= gated and "C14" in gated
    assert [(r["row_id"], r["description"], r["quantity"]) for r in s.policy_rows] == [("M4", "CIRCUIT BREAKER", 4.0)]
    # without a collision the index is exactly index_rows (7154 electrical rows alone)
    rows, aliases = C.index_sources([s.inputs.category_outputs["electrical_drawings"]], None)
    assert aliases == {} and len([r for r in rows if r[0] == "C"]) == 62


def test_7172_fixture_remerged_on_one_run_sequence_holds_every_row_without_aliases(inputs7172):
    """V1 hook 1 measured on the 7172 fixture. As stored, the three outputs
    were each merged from 1 (general C1-C5, mechanical C1-C20, electrical
    C1-C62): index_rows holds 62 of the 87 count rows. Re-merged on one run
    sequence (each category seeded from the outputs already produced, the
    way plans_reader_runs seeds consolidate_category after the hook edit)
    every one of the 99 rows has its own id and the session holds all of
    them with no collision alias: panels A and B, the transformer and the
    seven breaker rows are plain rows again."""
    stored = inputs7172.category_outputs
    assert len([r for r in R.index_rows(list(stored.values())) if r[0] == "C"]) == 62  # 25 rows behind the first of each id
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    remerged: dict[str, dict] = {}
    for cat in sorted(stored, key=lambda c: rank[c]):
        v1 = MG.legacy_view(stored[cat])  # the v1 shape (the fixture holds no groups)
        remerged[cat] = MG.consolidate_output(cat, v1, [], documents=inputs7172.documents, run_outputs=list(remerged.values()))
    ids = [r["row_id"] for o in remerged.values() for k, _p in R.ROW_KINDS for r in o[k]]
    assert len(ids) == len(set(ids)) == 87 + 3 + 9
    assert [r["row_id"] for r in remerged["general_drawings"]["counts"]] == [f"C{i}" for i in range(1, 6)]
    assert [r["row_id"] for r in remerged["mechanical_drawings"]["counts"]] == [f"C{i}" for i in range(6, 26)]
    assert remerged["electrical_drawings"]["counts"][0]["row_id"] == "C26" and remerged["electrical_drawings"]["row_seq"]["C"] == 87
    assert len(R.index_rows(list(remerged.values()))) == 99

    inputs = C.ConsolidatorInputs(
        run=dict(inputs7172.run), documents=list(inputs7172.documents), category_outputs=remerged,
        measure=copy.deepcopy(inputs7172.measure), scope_context=copy.deepcopy(inputs7172.scope_context),
        facts=list(inputs7172.facts), ledger=dict(inputs7172.ledger), groups=[], settings=dict(SETTINGS),
    )
    s = C.Session(inputs, group_id="g7172-remerged")
    assert s.row_aliases == {} and not any("original_row_id" in r for r in s.rows.values())
    kinds = {k: sum(1 for r in s.rows if r[0] == k) for k in "CRS"}
    assert kinds == {"C": 87, "R": 3, "S": 9}
    # the same rows as the stored outputs, each under exactly one id
    stored_rows = sorted((c, r["description"], float(r["quantity"] or 0)) for c, o in stored.items() for r in o["counts"])
    session_rows = sorted((r["category"], r["description"], float(r["quantity"] or 0)) for r in s.rows.values() if r.get("kind") == "C")
    assert session_rows == stored_rows
    # the electrical rows the live session lost are plain rows now
    descs = [r["description"] for r in s.rows.values() if r.get("kind") == "C" and r["category"] == "electrical_drawings"]
    assert any(d.startswith("New panel A") for d in descs) and any(d.startswith("New panel B") for d in descs)
    assert sum(1 for d in descs if "breaker" in d.lower()) >= 7
    assert s.gate_outputs == list(remerged.values())  # no re-numbering for the gates either
    assert [(r["row_id"], r["description"], r["quantity"]) for r in s.policy_rows] == [("M4", "CIRCUIT BREAKER", 4.0)]


def test_7172_luminaire_schedule_accepts_letter_tags_and_schedule_listed_variants(session7172):
    """The S1 Lighting Fixture Schedule (MARK column) gives A, AE, B, BE, C,
    CE, D, DE, E, EX1, EX2, F, FE. AE is the schedule's own emergency
    variant of A (same description, battery pack in the comments), so a row
    that says type AE is AE, never AE-EM, and a type A row with emergency
    words becomes AE; an exit sign is emergency in itself and never gets a
    suffix. Every fixture candidate is "<TAG> - <schedule description>";
    the linear fixtures of different tags are separate candidates (they
    were one 57 EA candidate), a schedule quantity and the plan count of a
    tag are separate candidates with a note, and the whole-sheet recount
    row of type F supersedes its first pass rows."""
    s = session7172
    sched = {e["tag"]: e for e in s.luminaires}
    assert list(sched) == ["A", "AE", "B", "BE", "C", "CE", "D", "DE", "E", "EX1", "EX2", "F", "FE"]
    assert sched["AE"]["variant_of"] == "A" and sched["AE"]["emergency"] and sched["A"]["variant_of"] is None
    assert sched["EX1"]["emergency"] and sched["DE"]["variant_of"] == "D" and sched["AE"]["notes"] == "PROVIDE 10W EMERGENCY BATTERY PACK"
    rows = s.rows
    assert (C.resolve_luminaire(rows["C36"], s.luminaires)["tag"], C.resolve_luminaire(rows["C36"], s.luminaires)["how"]) == ("AE", "text")
    assert C.resolve_luminaire(rows["C2"], s.luminaires)["tag"] == "AE"  # schedule count row with "emergency battery pack"
    assert C.resolve_luminaire(rows["C47"], s.luminaires)["tag"] == "EX1"  # "Exit/emergency fixture type EX1"
    assert C.resolve_luminaire(rows["C41"], s.luminaires)["tag"] == "D"
    assert C.resolve_luminaire({"description": "Light fixture type A, emergency battery pack", "unit": "EA"}, s.luminaires)["tag"] == "AE"
    assert C.resolve_luminaire(rows["C46"], s.luminaires) is None  # four linear tags resemble it equally
    names = {c["row_ids"][0]: (c["description"], c["quantity"]) for c in s.candidates.values() if c["group"] == "LIGHTING FIXTURES"}
    assert names["C35"] == ("A - 2' X 4' CEILING LIGHT", 26) and names["C36"] == ("AE - 2' X 4' CEILING LIGHT", 29)
    assert names["C37"] == ("B - SQUARE RECESSED CAN", 14) and names["C38"] == ("BE - SQUARE RECESSED CAN", 14)
    assert names["C39"] == ("C - PENDANT LIGHT", 3) and names["C40"] == ("CE - PENDANT LIGHT", 4)
    assert names["C41"] == ("D - LINEAR LIGHT FIXTURE", 9) and names["C42"] == ("DE - LINEAR LIGHT FIXTURE", 10)
    assert names["C43"] == ("E - CEILING LIGHT FIXTURE", 4) and names["C47"] == ("EX1 - EXIT SIGN", 6) and names["C48"] == ("EX2 - EXIT SIGN", 1)
    assert names["C45"] == ("FE - LINEAR LIGHT FIXTURE", 5)
    assert _cand_rows(s, "C62")["description"] == "F - LINEAR LIGHT FIXTURE" and _cand_rows(s, "C62")["quantity"] == 8
    assert {u["id"] for u in s.unproposed if "recount row C62" in u["reason"]} == {"C44"}
    assert C.row_instruction(s, ["C44"]).endswith("account_rows info_only naming C62")
    assert _cand_rows(s, "C46")["description"].startswith("SHORT LINEAR LIGHT FIXTURE")  # unresolved: reader wording, its own line
    # the schedule quantities are separate candidates that name their plan twin
    twin = s.compact_candidate(_cand_rows(s, "C1"))
    assert twin["description"] == "A - 2' X 4' CEILING LIGHT" and _cand_rows(s, "C35")["id"] in twin["note"]
    assert "note" not in s.compact_candidate(_cand_rows(s, "C35"))
    # write-time rule: the run's wording is renamed by the tag; the right tag in the model's words is kept
    got = C.apply_write(s, "add_boq_line", _fixture_line(area="EVAL 7172", description="LIGHT FIXTURE TYPE AE", source_refs=_cite("C36")))
    assert s.book.lines[got["line_id"]]["description"] == "AE - 2' X 4' CEILING LIGHT" and got["warnings"]
    kept = C.apply_write(s, "add_boq_line", _fixture_line(area="EVAL 7172", description="D - Long linear luminaire", source_refs=_cite("C41")))
    assert s.book.lines[kept["line_id"]]["description"] == "D - Long linear luminaire" and not any("renamed" in w for w in kept["warnings"])
    bad = _refused(C.apply_write(s, "add_boq_line", _fixture_line(area="EVAL 7172", description="B - SQUARE RECESSED CAN", source_refs=_cite("C38"))), C.LUMINAIRE_TAG)
    assert (bad["detail"]["found"], bad["detail"]["expected"]) == ("B", "BE")
    # duplicate_view of a schedule quantity against its plan count resolves by tag
    dup = C.apply_write(s, "account_rows", {"row_ids": ["C1"], "disposition": "duplicate_view", "duplicate_of": "C35", "reason": "schedule count", "fact_id": None, "note_ref": None})
    assert dup["accounted"] == ["C1"]
    # the scorer keys the names on the leading tag (the variant fallback lands AE on the AE-EM truth)
    from app.services import plans_reader_score as S

    for desc, tag in (("A - 2' X 4' CEILING LIGHT", "A"), ("AE - 2' X 4' CEILING LIGHT", "AE"), ("EX1 - EXIT SIGN", "EX1"), ("D - LINEAR LIGHT FIXTURE", "D")):
        key = S.classify_line(desc, "NEW WORK", "LIGHTING FIXTURES", "EA", subgroup="FIXTURES")
        assert (key.kind, set(key.attrs)) == ("fixture", {tag}), desc


def test_7172_gear_candidates_are_their_own_lines_with_canonical_names(session7172):
    """Panels A and B (refused as conductors because of "4-wire"), the
    transformer and the disconnects are gear candidates in their SWITCHGEAR
    subgroups with the estimator's names: the panel from its schedule
    header, the transformer by kVA with its plan and one-line views as one
    unit (combine max), the disconnects by the rating the panel schedule
    gives the equipment they name (RTU circuits 30A/3P five of six, ODU
    30A/2P, water heater 20A/2P; NEMA 3R on the roof, NEMA 1 indoors), the
    breakers by rating across both panels. A gear row is never folded into
    an equipment connection line."""
    s = session7172
    assert not [c for c in s.candidates.values() if c.get("refusal")]
    a = _cand_rows(s, "C14", "C60")
    assert (a["description"], a["quantity"], a["combine"], a["subgroup"]) == ("PANEL A, 277/480V, 3-PH, 4W, 200A MLO TYPE, 14KAIC, 42 POLES, SURFACE MOUNT, NEMA 1", 1, "max", "PANELS")
    assert _cand_rows(s, "C15", "C61")["description"] == "PANEL B, 120/208V, 3-PH, 4W, 200A MCB TYPE, 10KAIC, 64 POLES, SURFACE MOUNT, NEMA 1"
    t = _cand_rows(s, "C16", "C59")
    assert (t["description"], t["quantity"], t["combine"], t["subgroup"]) == ("75KVA TRANSFORMER", 1, "max", "TRANSFORMERS")
    rtu = _cand_rows(s, "C27")
    assert (rtu["description"], rtu["quantity"], rtu["subgroup"]) == ("30A/3P DISCONNECT SWITCH, NEMA 3R", 6, "DISCONNECT SWITCHES")
    assert "5 of 6 matching circuit(s) at 30A/3P; other circuits 20A/3P x1" in rtu["material_note"] and "NEMA 3R" in rtu["material_note"]
    assert _cand_rows(s, "C29")["description"] == "30A/2P DISCONNECT SWITCH, NEMA 3R"
    assert _cand_rows(s, "C32")["description"] == "20A/2P DISCONNECT SWITCH, NEMA 1"
    assert s.schedule_rating_for("Rooftop-unit disconnect symbols") == {"amps": 30, "poles": 3, "circuits": 6, "agree": 5, "others": {"20A/3P": 1}}
    assert s.schedule_rating_for("Garbage disposal disconnect") is None and s.panel_header("Z") is None
    assert s.panel_header("B")["main_type"] == "MCB" and s.panel_header("B")["positions"] == 64
    b20 = _cand_rows(s, "C19", "C23")
    assert (b20["description"], b20["quantity"], b20["combine"], b20["group"], b20["subgroup"]) == ("20A/1P CIRCUIT BREAKER", 58, "sum", "SWITCHGEAR", "CIRCUIT BREAKERS")
    breakers = {c["description"]: c["quantity"] for c in s.candidates.values() if c["subgroup"] == "CIRCUIT BREAKERS" and c["kind"] == "count"}
    assert breakers == {"20A/1P CIRCUIT BREAKER": 58, "100A/3P CIRCUIT BREAKER": 1, "20A/3P CIRCUIT BREAKER": 1, "30A/3P CIRCUIT BREAKER": 5,
                        "20A/2P CIRCUIT BREAKER": 1, "30A/2P CIRCUIT BREAKER": 1, "ROOFTOP HEAT-PUMP UNIT POWER CONNECTION, 460V 3-PHASE, MCA 19A, MAX FUSE 25A": 1,
                        "ROOFTOP HEAT-PUMP UNIT POWER CONNECTION, 460V 3-PHASE, MCA 10A, MAX FUSE 15A": 1,
                        "ROOFTOP HEAT-PUMP UNIT POWER CONNECTIONS, 460V 3-PHASE, MCA 15A, MAX FUSE 20A EACH": 4}
    assert next(c for c in s.candidates.values() if c["kind"] == "policy")["description"] == "CIRCUIT BREAKER"
    assert {u["id"] for u in s.unproposed if "existing" in u["reason"]} >= {"C17", "C18"}
    assert C.gear_unit(C.parse_item("75 kVA transformer T-B mounted above panel B", "EA")) == "T:T-B"
    assert C.gear_unit(C.parse_item("Panel A location, existing status indicated by run ledger", "EA")) == "P:A"
    # the gear fold guard (c): a connection line never carries a panel or a transformer row
    conn = _line(area="EVAL 7172", subgroup="MISCELLANEOUS", description="ELECTRICAL CONNECTION", combine="sum", source_refs=_cite("C26", "C15"))
    bad = _refused(C.apply_write(s, "add_boq_line", conn), "unit_mismatch")
    assert bad["detail"]["row_ids"] == ["C15"] and "panel (gear)" in bad["message"] and s.book.consumed_by("C26") is None
    bad = _refused(C.apply_write(s, "add_boq_line", {**conn, "description": "RTU AND TRANSFORMER HOOK-UPS", "source_refs": _cite("C26", "C16")}), "unit_mismatch")
    assert bad["detail"]["row_ids"] == ["C16"] and "transformer (gear)" in bad["message"]
    ok = C.apply_write(s, "add_boq_line", {**conn, "combine": "single", "source_refs": _cite("C26")})
    assert s.book.lines[ok["line_id"]]["description"] == "ELECTRICAL CONNECTION" and s.book.lines[ok["line_id"]]["quantity"] == 6
    # write-time gear naming: the reader's wording is renamed, a name with the right ratings is kept
    gear = _line(area="EVAL 7172", group="SWITCHGEAR", subgroup="DISCONNECT SWITCHES", material_category="Switchgear")
    got = C.apply_write(s, "add_boq_line", {**gear, "description": "ROOFTOP-UNIT DISCONNECT SYMBOLS; RATINGS NOT SHOWN", "source_refs": _cite("C27")})
    line = s.book.lines[got["line_id"]]
    assert line["description"] == "30A/3P DISCONNECT SWITCH, NEMA 3R" and line["material_note"].startswith("Reader wording: ROOFTOP-UNIT DISCONNECT SYMBOLS")
    assert "Rating from the panel schedule" in line["material_note"] and any("renamed to the estimator vocabulary" in w for w in got["warnings"])
    kept = C.apply_write(s, "add_boq_line", {**gear, "description": "30A/2P NEMA 3R DISCONNECT", "source_refs": _cite("C29")})
    assert s.book.lines[kept["line_id"]]["description"] == "30A/2P NEMA 3R DISCONNECT" and not kept.get("warnings")
    panel = C.apply_write(s, "add_boq_line", {**gear, "subgroup": "PANELS", "description": "PANEL B", "combine": "max", "source_refs": _cite("C15", "C61")})
    assert s.book.lines[panel["line_id"]]["description"].startswith("PANEL B, 120/208V, 3-PH, 4W, 200A MCB TYPE")
    brk = C.apply_write(s, "add_boq_line", {**gear, "subgroup": "CIRCUIT BREAKERS", "description": "BRANCH BREAKERS 20A 1P", "combine": "sum", "source_refs": _cite("C19", "C23")})
    assert s.book.lines[brk["line_id"]]["description"] == "20A/1P CIRCUIT BREAKER" and s.book.lines[brk["line_id"]]["quantity"] == 58
    # a transformer seen on the plan and the one-line is one unit for duplicate_view
    assert s.same_item(s.rows["C16"], s.rows["C59"]) and not s.same_item(s.rows["C14"], s.rows["C15"])


def test_7172_device_and_outlet_candidates_use_the_vocabulary(session7172):
    """The E2.00 symbol rows were one 201 EA receptacle candidate; they are
    now the estimator's devices in their groups: duplex and quadplex
    receptacles, the floor box, tele/data and TV outlets under LOW VOLTAGE
    WORK, the S/OS and M lighting-control marks as OCCUPANCY SENSOR SWITCH
    and OCCUPANCY SENSOR under LIGHTING CONTROLS; a symbol outside the
    vocabulary stays its own line in the reader's words."""
    s = session7172
    dup = _cand_rows(s, "C52", "C53")
    assert (dup["description"], dup["quantity"], dup["group"], dup["subgroup"]) == ("DUPLEX RECEPTACLE", 81, "OTHER ITEMS", RECEPT_SUBGROUP)
    assert (_cand_rows(s, "C54")["description"], _cand_rows(s, "C54")["quantity"]) == ("QUADPLEX RECEPTACLE", 35)
    other = _cand_rows(s, "C55")
    assert other["description"] == "CIRCULAR POWER-OUTLET SYMBOLS WITH INTERNAL RECTANGLE" and other["subgroup"] == RECEPT_SUBGROUP
    floor = _cand_rows(s, "C56")
    assert (floor["description"], floor["quantity"], floor["subgroup"]) == ("FLOOR BOX RECEPTACLE", 2, RECEPT_SUBGROUP)
    data = _cand_rows(s, "C57")
    assert (data["description"], data["quantity"], data["group"], data["subgroup"]) == ("TELE/DATA OUTLET", 77, "LOW VOLTAGE WORK", "OUTLETS/DEVICES")
    tv = _cand_rows(s, "C58")
    assert (tv["description"], tv["quantity"], tv["group"]) == ("TV OUTLET", 4, "LOW VOLTAGE WORK")
    sw = _cand_rows(s, "C49")
    assert (sw["description"], sw["quantity"], sw["group"]) == ("OCCUPANCY SENSOR SWITCH", 28, "LIGHTING CONTROLS")
    assert (_cand_rows(s, "C50")["description"], _cand_rows(s, "C50")["quantity"]) == ("OCCUPANCY SENSOR", 10)
    assert _cand_rows(s, "C51")["description"].startswith("UNIDENTIFIED CIRCULAR LIGHTING-CONTROL") and _cand_rows(s, "C51")["group"] == "LIGHTING CONTROLS"
    assert (_cand_rows(s, "C34")["description"], _cand_rows(s, "C34")["subgroup"]) == ("ROOF RECEPTACLE, WEATHERPROOF AND GFCI RATED", RECEPT_SUBGROUP)
    # write-time: the run's names are renamed to the vocabulary, the wording kept in the note
    lv = _line(area="EVAL 7172", group="LOW VOLTAGE WORK", subgroup="OUTLETS/DEVICES", material_category="Low Voltage")
    got = C.apply_write(s, "add_boq_line", {**lv, "description": "LOW-VOLTAGE OUTLET LOCATION, GREEN TRIANGLE", "source_refs": _cite("C57")})
    line = s.book.lines[got["line_id"]]
    assert (line["description"], line["quantity"], line["material_note"]) == ("TELE/DATA OUTLET", 77, "Reader wording: LOW-VOLTAGE OUTLET LOCATION, GREEN TRIANGLE.")
    assert s.book.lines[C.apply_write(s, "add_boq_line", {**lv, "description": "TV OUTLET LOCATION", "source_refs": _cite("C58")})["line_id"]]["description"] == "TV OUTLET"
    ctl = _line(area="EVAL 7172", group="LIGHTING CONTROLS", subgroup="CONTROLS", material_category="Lighting")
    assert s.book.lines[C.apply_write(s, "add_boq_line", {**ctl, "description": "WALL-MOUNTED LIGHTING-CONTROL SYMBOLS MARKED S/OS", "source_refs": _cite("C49")})["line_id"]]["description"] == "OCCUPANCY SENSOR SWITCH"
    assert s.book.lines[C.apply_write(s, "add_boq_line", {**ctl, "description": "LIGHTING-CONTROL SYMBOLS MARKED M", "source_refs": _cite("C50")})["line_id"]]["description"] == "OCCUPANCY SENSOR"
    fb = C.apply_write(s, "add_boq_line", _line(area="EVAL 7172", subgroup="MISCELLANEOUS", description="CONFERENCE-TABLE FLOOR-BOX RECEPTACLE SYMBOLS", source_refs=_cite("C56")))
    assert s.book.lines[fb["line_id"]]["description"] == "FLOOR BOX RECEPTACLE"
    # a mechanical row and an electrical row of one device are the same item for duplicate_view
    assert s.same_item(s.rows["C29"], s.rows["C76"])


def _scripted_7172(session: C.Session) -> list[dict]:
    """The scripted model of the fix-pass harness: accept every candidate; a
    schedule quantity whose plan twin exists, another category's view of a
    disconnect already carried, the rows a whole-sheet recount displaced
    and the rows that read as existing are accounted; then finish."""
    cands = session.candidates
    accept, script = [], [("propose_lines", {"section": None})]
    seen_names: dict[str, dict] = {}
    for c in cands.values():
        if c.get("refusal"):
            continue
        twin = session.schedule_twin(c)
        if twin is not None:
            script.append(("account_rows", {"row_ids": c["row_ids"], "disposition": "duplicate_view", "duplicate_of": twin["row_ids"][0],
                                            "reason": "schedule quantity; the plan count carries the line", "fact_id": None, "note_ref": None}))
            continue
        first = seen_names.get(c["description"])
        if first is not None and c["description"].endswith("DISCONNECT SWITCH, NEMA 3R") and c["row_ids"][0] in session.row_aliases:
            script.append(("account_rows", {"row_ids": c["row_ids"], "disposition": "duplicate_view", "duplicate_of": first["row_ids"][0],
                                            "reason": "another category's view of the same disconnect", "fact_id": None, "note_ref": None}))
            continue
        seen_names.setdefault(c["description"], c)
        accept.append(c["id"])
    script.insert(1, ("accept_candidates", {"ids": accept, "overrides": []}))
    for marker, disposition in (("recount row", "info_only"), ("existing", "existing_to_remain")):
        ids = [u["id"] for u in session.unproposed if marker in u["reason"]]
        if ids:
            script.append(("account_rows", {"row_ids": ids, "disposition": disposition, "reason": marker, "fact_id": None, "duplicate_of": None, "note_ref": None}))
    script.append(("finish", {"confidence_notes": ["scripted harness"]}))
    return C.run_script(session, script)


def test_7172_scripted_harness_scores_the_count_lines(session7172):
    """The fix-pass measurement: the run's stored rows through the scripted
    model, scored against the 40 count lines of the case truth with
    plans_reader_score. The stored run scored 5 of 40 count lines exact;
    the same rows through this code score 30 (fixtures 13 of 13, breakers
    6 of 6, panels 2, transformer, disconnects 3, sensors, outlets); the
    misses are the switched receptacle splits the symbols do not carry, the
    thermostat and the boxes. finish passes with the re-numbered rows."""
    from app.services import plans_reader_score as S

    s = session7172
    trace = _scripted_7172(s)
    refused = [t for t in trace if isinstance(t.get("result"), dict) and t["result"].get("refused")]
    assert refused == [] and s.unaccounted_rows() == [] and s.passed is True and s.finished is True
    main = C.assemble_main_output(s, mode="harness")
    by_desc = {}
    for area in main["boq"]["areas"]:
        for sec in area["sections"]:
            for g in sec["groups"]:
                for sg in g["subgroups"]:
                    for it in sg["items"]:
                        by_desc[it["description"]] = (g["name"], it["quantity"])
    assert by_desc["A - 2' X 4' CEILING LIGHT"] == ("LIGHTING FIXTURES", 26) and by_desc["F - LINEAR LIGHT FIXTURE"] == ("LIGHTING FIXTURES", 8)
    assert by_desc["20A/1P CIRCUIT BREAKER"] == ("SWITCHGEAR", 58) and by_desc["75KVA TRANSFORMER"] == ("SWITCHGEAR", 1)
    assert by_desc["30A/3P DISCONNECT SWITCH, NEMA 3R"] == ("SWITCHGEAR", 6) and by_desc["30A/2P DISCONNECT SWITCH, NEMA 3R"] == ("SWITCHGEAR", 1)
    assert by_desc["TELE/DATA OUTLET"] == ("LOW VOLTAGE WORK", 77) and by_desc["OCCUPANCY SENSOR SWITCH"] == ("LIGHTING CONTROLS", 28)
    assert "ELECTRICAL CONNECTION" in by_desc and by_desc["CIRCUIT BREAKER"] == ("SWITCHGEAR", 4)  # the policy row stays a line of its own
    truth = [S.TruthLine(line_id=t["line_id"], area=t["area"], section=t["section"], group=t["group"], subgroup=t["subgroup"],
                         description=t["description"], unit=t["unit"], qty=float(t["qty"])) for t in _load_7172("truth_count_lines")]
    rows = S.rows_from_spec(C._specs_from_main(main)["boq_spec"])
    score = S.score_case(rows, truth=truth, case_id="7172")
    assert score.count_lines_total == 40 and score.count_lines_exact >= 30
    exact = {ln.description for ln in score.lines if ln.unit == "EA" and ln.status == "exact"}
    assert {"20A/1P CIRCUIT BREAKER", "75KVA TRANSFORMER", "30A/3P DISCONNECT SWITCH, NEMA 3R", "TELE/DATA OUTLET", "TV OUTLET",
            "OCCUPANCY SENSOR SWITCH"} <= exact
    assert sum(1 for d in exact if d.startswith(("A, ", "AE, ", "B, ", "BE, ", "C, ", "CE, ", "D, ", "DE, ", "E, ", "EX1, ", "EX2, ", "F, ", "FE, "))) == 13
