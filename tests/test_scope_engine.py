"""WS5 scope engine (app/services/scope_engine.py): enforce idempotence and
conservation, inclusions replace-or-merge, compound RFI rewrite, overrides,
allowances, DB seams with a fake client, the tools, the replay stages, and the
golden 7154 replay on real parsed WS1 text plus the FALSE-DROP metric."""

from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from app.core.config import get_settings
from app.services import bid_conditions as bidc
from app.services import plans_reader_tools as tools
from app.services import scope_attribution as sa
from app.services import scope_breakers as brk
from app.services import scope_engine as se
from app.services import scope_policy
from app.services import scope_standards as ss
from app.services import scope_tools
from app.services.eval_bundle import DEFAULT_BACKUP_DIR
from app.services.scope_attribution import Statement
from app.services.scope_policy_seed import SEED_POLICY, THERMOSTAT_CLARIFICATION

FIXTURE = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7154"
EVAL_ROOT = Path(get_settings().eval_root)
BUNDLE = EVAL_ROOT / "bundles" / "7154" / "ff8fad25"
REF_DIR = EVAL_ROOT / "references" / "7154"
HONEYWELL = "Access controls and all Fire detection systems for the building are provided by Honeywell."
TELEMETRY = ("Telemetry antennas, Centrak devices, and wireless access point exact locations must be noted on a "
             "marked-up floor plan delivered to the engineering representative prior to any demolition. Removal, "
             "storage, and re-installation of these devices shall be the responsibility of the SUCCESSFUL BIDDER.")
FURNITURE = ("The general contractor shall provide electrical, data, and plumbing connections from infrastructure to "
             "pre-manufactured furniture systems.")
DOORS = "Cylindrical locksets: Schlage or equal ND series prepped for large format interchangeable core"
PAINT = "Door and Frame Paint Spec / Sherwin-Williams - Substitutes for paint brand and sheen are not acceptable."
EXA = "exa"
EXA_NAME = "Exhibit A - Scope of Work"


def _main() -> dict:
    return json.loads((FIXTURE / "outputs" / "main_none_v1.json").read_text())


def _category() -> dict:
    return json.loads((FIXTURE / "outputs" / "category_electrical_drawings_v1.json").read_text())


def _elec_facts() -> dict:
    data = json.loads((FIXTURE / "page_facts_elec.json").read_text())
    return next(d for d in data["documents"] if d["key"] == "elec")["facts"]


def _settings(**over: Any) -> Any:
    """Settings at the configured dev value of plans_reader_demo_two_line_style
    (false, the code default; WS4 measured the style share at 11.1%) unless a
    test overrides it."""
    return get_settings().model_copy(update={"plans_reader_demo_two_line_style": False, **over})


TWO_LINE = pytest.mark.parametrize("two_line", [False, True], ids=["style_off", "style_on"])


def _exhibit_statements() -> list[Statement]:
    pages = {HONEYWELL: 2, TELEMETRY: 2, FURNITURE: 1, DOORS: 5, PAINT: 6}
    return [Statement(text=t, document_id=EXA, page=p, role="owner_exhibit", level="owner_exhibit") for t, p in pages.items()]


def _ctx(**over: Any) -> dict[str, Any]:
    """A ScopeContext built by the engine's own pieces from committed
    fixtures (WS1 panel facts, the 7154 category output) and a few Exhibit A
    sentences, without any PDF."""
    statements = _exhibit_statements() + [
        Statement(text="EMT WITH STEEL INSULATED THROAT SET SCREW FITTINGS MAY BE USED IN DRY, PROTECTED INTERIOR LOCATIONS.",
                  document_id="elec", page=2, role="general_note", level="general_note", sheet="E0.01", ref="GN5"),
        Statement(text="ALL WIRING ABOVE CEILING SHALL BE IN RIGID CONDUIT.", document_id="elec", page=2,
                  role="general_note", level="general_note", sheet="E0.01", ref="GN6"),
        Statement(text="EXISTING LIGHT FIXTURE TO BE REMOVED. REMOVAL SHALL INCLUDE BUT NOT BE LIMITED TO REMOVAL OF "
                       "LIGHT FIXTURE, CONDUIT, CONDUCTORS, ETC FROM LIGHT FIXTURE TO POINT OF SOURCE.",
                  document_id="elec", page=8, role="keynote", level="keynote", sheet="ED3.01", ref="KN1"),
        Statement(text="MAINTAIN AND CAP OFF EXISTING CONDUIT AND CONDUCTORS FOR USE IN NEW CONSTRUCTION.",
                  document_id="elec", page=8, role="sheet_note", level="sheet_note", sheet="ED3.01", ref="SN1"),
    ]
    names = {EXA: EXA_NAME, "elec": "ELEC"}
    obligations = sa.attribute(statements, document_names=names)
    page_text = {(s.document_id, s.page): "" for s in statements}
    for s in statements:
        page_text[(s.document_id, s.page)] += s.text + "\n"
    fact_obs = [o for o in obligations if se._fact_eligible(o)]
    facts = sa.facts_from_obligations(fact_obs, lambda d, p: page_text.get((d, p)))
    inputs = se.ScopeInputs([], category_outputs={"electrical_drawings": _category()})
    elec = _elec_facts()
    plan = brk.plan_breakers(elec["schedules"], sheet_for_page={p["page"]: p["sheet_number"] for p in elec["pages"]})
    bid = bidc.classify(None, ["3.1.1 Nevada Revised Statutes (NRS), Chapter 338 - Public Works"], SEED_POLICY)
    ctx = {
        "schema": se.SCOPE_CONTEXT_SCHEMA,
        "policy_id": None,
        "policy_sha": scope_policy.policy_sha(SEED_POLICY),
        "bid_conditions": bid.to_dict(),
        "documents": [{"document_id": EXA, "name": EXA_NAME, "category": "rfp"}],
        "obligations": [o.to_dict() for o in obligations],
        "facts": [f.to_dict() for f in facts],
        "fact_splits": se._fact_splits(fact_obs, facts),
        "standards": ss.decisions_dict(ss.resolve(statements, SEED_POLICY, document_names=names)),
        "breaker_plan": plan.to_dict(),
        "inclusions": se.compute_inclusions(inputs, SEED_POLICY, settings=_settings()),
        "device_facts": {"tr_count": 3, "tr_method": "legend_symbol", "tr_cite": "REC-TAMP legend symbol",
                         "grade_notes": ["Hospital grade devices in patient care areas per E2.01 SN10."],
                         "spec_facts": [{"key": "device_grade", "value": "hospital", "source_ref": "E2.01 SN10"}]},
        "document_amounts": [],
    }
    ctx.update(over)
    return ctx


def _totals(out: dict) -> dict[str, float]:
    tot: dict[str, float] = defaultdict(float)
    for *_x, it in se.boq_items(out):
        if not it.get("is_accessory"):
            tot[str(it.get("unit") or "EA")] += float(it.get("quantity") or 0)
    return tot


def _rows(out: dict, pattern: str) -> list[dict]:
    import re

    rx = re.compile(pattern, re.I)
    return [it for *_x, it in se.boq_items(out) if rx.search(str(it.get("description") or ""))]


# Enforce ----------------------------------------------------------------------------------------


@TWO_LINE
def test_enforce_is_idempotent(two_line):
    s = _settings(plans_reader_demo_two_line_style=two_line)
    once = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=s)
    twice = se.enforce(once, _ctx(), SEED_POLICY, (), settings=s)
    assert json.dumps(once, sort_keys=True, default=str) == json.dumps(twice, sort_keys=True, default=str)


def _assert_conserved(before_out: dict, out: dict) -> None:
    """BOQ before = BOQ after + excluded_lines - quantity_added, per unit."""
    before = _totals(before_out)
    after = _totals(out)
    excluded: dict[str, float] = defaultdict(float)
    for e in out["excluded_lines"]:
        excluded[e["unit"]] += float(e["quantity"])
    added: dict[str, float] = defaultdict(float)
    for a in out["scope_policy"]["quantity_added"]:
        added[a["unit"]] += float(a["quantity"])
    for unit in set(before) | set(after) | set(excluded) | set(added):
        assert abs(before.get(unit, 0) - (after.get(unit, 0) + excluded.get(unit, 0) - added.get(unit, 0))) < 1e-6, unit
    for e in out["excluded_lines"]:
        assert e["line_id"].startswith("B") and e["reason"] and e["decision_key"] and e["item"]


@TWO_LINE
def test_enforce_conserves_quantities_across_boq_and_excluded_lines(two_line):
    main = _main()
    out = se.enforce(main, _ctx(), SEED_POLICY, (), settings=_settings(plans_reader_demo_two_line_style=two_line))
    _assert_conserved(main, out)
    rules = {a["rule_id"] for a in out["scope_policy"]["quantity_added"]}
    assert "IN-CONTROL-CABLE" in rules


def _breaker_main(qty: float, plain: float = 22) -> dict:
    items = [{"description": "20A/1P GFCI CIRCUIT BREAKER", "quantity": qty, "unit": "EA", "sheet_refs": ["E0.04"]}]
    if plain:
        items.append({"description": "20A/1P CIRCUIT BREAKER", "quantity": plain, "unit": "EA", "sheet_refs": ["E0.04"]})
    return {"boq": {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [
        {"name": "SWITCHGEAR", "subgroups": [{"name": "CIRCUIT BREAKERS", "items": items}]}]}]}]},
        "estimate": {"extra_allowances": []}, "brief": {"rfis": []}}


def test_breakers_record_every_quantity_change_and_conserve():
    plan = _ctx()["breaker_plan"]
    assert len(plan["reuse_circuits"]) == 22
    # a model row larger than the plan: the difference is an excluded_lines entry, never silently overwritten
    main = _breaker_main(10, plain=0)
    out = se.enforce(main, {"breaker_plan": plan}, SEED_POLICY, ())
    rows = [it for *_x, it in se.boq_items(out) if brk.is_breaker_row(it["description"])]
    assert [(r["description"], r["quantity"]) for r in rows] == [("20A/1P CIRCUIT BREAKER, GFCI", 4)]
    delta = [e for e in out["excluded_lines"] if e.get("delta_of")]
    assert len(delta) == 1 and delta[0]["quantity"] == 6 and delta[0]["rule_id"] == "BRK-REUSE"
    assert delta[0]["decision_key"].endswith(":delta") and delta[0]["source"] == "policy"
    _assert_conserved(main, out)
    again = se.enforce(out, {"breaker_plan": plan}, SEED_POLICY, ())
    assert json.dumps(again, sort_keys=True, default=str) == json.dumps(out, sort_keys=True, default=str)
    # an include override on the delta puts the 6 EA back on the same row
    restored = se.enforce(out, {"breaker_plan": plan}, SEED_POLICY,
                          [{"decision_key": delta[0]["decision_key"], "action": "include", "note": "all ten are new"}])
    rows = [it for *_x, it in se.boq_items(restored) if brk.is_breaker_row(it["description"])]
    assert [r["quantity"] for r in rows] == [10] and not [e for e in restored["excluded_lines"] if e.get("delta_of")]
    # the 22 provisional rows use the whole reuse evidence: the 6 extra GFCI stay, for review
    both = se.enforce(_breaker_main(10), {"breaker_plan": plan}, SEED_POLICY, ())
    assert [r["quantity"] for r in _rows(both, r"BREAKER")] == [10]
    assert [e["quantity"] for e in both["excluded_lines"]] == [22]
    assert any(n["code"] == "breaker_unplanned" for n in both["scope_policy"]["needs_review"])
    _assert_conserved(_breaker_main(10), both)
    # no reuse evidence: nothing the plan does not explain is lowered or dropped
    no_reuse = {**plan, "reuse_circuits": []}
    kept = se.enforce(_breaker_main(10), {"breaker_plan": no_reuse}, SEED_POLICY, ())
    assert [r["quantity"] for r in _rows(kept, r"BREAKER")] == [10, 22] and kept["excluded_lines"] == []
    # a plan line without a rating never matches or replaces a rated row
    unrated = {**no_reuse, "lines": [{**plan["lines"][0], "amps": None, "poles": None, "description": "CIRCUIT BREAKER"}]}
    fused = _breaker_main(0, plain=0)
    fused["boq"]["areas"][0]["sections"][0]["groups"][0]["subgroups"][0]["items"] = [
        {"description": "60AT/60AF/3P FUSED CIRCUIT BREAKER", "quantity": 1, "unit": "EA"}]
    out_u = se.enforce(fused, {"breaker_plan": unrated}, SEED_POLICY, ())
    assert [(r["description"], r["quantity"]) for r in _rows(out_u, r"BREAKER")] == [
        ("60AT/60AF/3P FUSED CIRCUIT BREAKER", 1)]
    assert any(w["key"].startswith("breakers:unrated") for w in out_u["scope_policy"]["warnings"])
    # a model row smaller than the plan: the increase is recorded as added quantity
    small = _breaker_main(1)
    out2 = se.enforce(small, {"breaker_plan": plan}, SEED_POLICY, ())
    added = out2["scope_policy"]["quantity_added"]
    assert [(a["kind"], a["quantity"]) for a in added] == [("increase", 3)]
    _assert_conserved(small, out2)
    # no model breaker row at all: the plan row is inserted and counted as added
    none = _breaker_main(0)
    none["boq"]["areas"][0]["sections"][0]["groups"][0]["subgroups"][0]["items"] = []
    out3 = se.enforce(none, {"breaker_plan": plan}, SEED_POLICY, ())
    assert [(a["kind"], a["rule_id"], a["quantity"]) for a in out3["scope_policy"]["quantity_added"]] == [
        ("insert", "BRK-PLAN", 4)]
    _assert_conserved(none, out3)


def test_connections_stay_7_and_are_never_double_inserted():
    out = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=_settings())
    rows = _rows(out, r"CONNECTION")
    rows = [r for r in rows if "REMOVAL" not in r["description"]]
    assert [(r["description"], r["quantity"]) for r in rows] == [("ELECTRICAL CONNECTION", 7)]
    again = se.enforce(out, _ctx(), SEED_POLICY, (), settings=_settings())
    assert len([r for r in _rows(again, r"^ELECTRICAL CONNECTION$")]) == 1


def test_connections_inserted_once_when_the_boq_has_none():
    main = _main()
    for *_x, it in se.boq_items(main):
        if "CONNECTION" in it["description"] and "MAKE SAFE" not in it["description"]:
            it["description"] = "MISC ITEM"
    out = se.enforce(main, _ctx(), SEED_POLICY, (), settings=_settings())
    assert [r["quantity"] for r in _rows(out, r"^ELECTRICAL CONNECTION$")] == [7]


def test_thermostats_5_not_10_across_demolition_and_new_work_sheets():
    cat = {"counts": [
        {"description": "Existing thermostat sensor relocation demolition phase: remove wiring and conduit back to VV",
         "quantity": 5, "unit": "EA", "sheet_number": "MD2.01"},
        {"description": "Relocate existing thermostat sensors and provide new conduit and wiring to VV",
         "quantity": 5, "unit": "EA", "sheet_number": "M2.01"},
        {"description": "New thermostat sensor location with wiring and conduit to new VV",
         "quantity": 2, "unit": "EA", "sheet_number": "M2.01"},
    ]}
    incs = se.compute_inclusions(se.ScopeInputs([], category_outputs={"x": cat}), SEED_POLICY, settings=_settings())
    by = {i["rule_id"]: i for i in incs}
    assert by["IN-THERMOSTAT-RELOC"]["quantity"] == 5
    assert by["IN-CONTROL-CABLE"]["quantity"] == 125 and by["IN-CONTROL-CABLE"]["caveat"] == "(ASSUMED)"
    main = {"boq": {"areas": [{"area": "A", "sections": [
        {"heading": "DEMOLITION", "groups": [{"name": "DEMOLITION", "subgroups": [{"name": "", "items": [
            {"description": "RELOCATE THERMOSTAT (REMOVE)", "quantity": 5, "unit": "EA", "sheet_refs": ["MD2.01"]}]}]}]},
        {"heading": "NEW WORK", "groups": [{"name": "OTHER ITEMS", "subgroups": [{"name": "MISCELLANEOUS", "items": [
            {"description": "THERMOSTAT RELOCATION", "quantity": 5, "unit": "EA", "sheet_refs": ["M2.01"]}]}]}]},
    ]}]}}
    out = se.enforce(main, {"inclusions": incs}, SEED_POLICY, ())
    rows = _rows(out, r"THERMOSTAT")
    assert [(r["description"], r["quantity"]) for r in rows] == [("RELOCATION OF THERMOSTAT TO NEW LOCATION", 5)]
    assert any("new-work sheet wins" in e["reason"] for e in out["excluded_lines"])
    assert _rows(out, r"#18/4C CONTROL CABLE")[0]["quantity"] == 125
    assert any(c["text"] == THERMOSTAT_CLARIFICATION for c in out["scope_policy"]["clarifications"])


@TWO_LINE
def test_7154_fixture_enforce_breakers_devices_inclusions_and_demolition(two_line):
    from app.services import plans_reader_acceptance as acc
    from app.services import plans_reader_score as score

    out = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=_settings(plans_reader_demo_two_line_style=two_line))
    breakers = [it for *_x, it in se.boq_items(out) if brk.is_breaker_row(it["description"])]
    assert [(b["description"], b["quantity"]) for b in breakers] == [("20A/1P CIRCUIT BREAKER, GFCI", 4)]
    assert _rows(out, r"^DUPLEX RECEPTACLE, TR$")[0]["quantity"] == 3
    assert sum(r["quantity"] for r in _rows(out, r"RECEPTACLE") if "REMOV" not in r["description"]) == 74
    assert _rows(out, r"RELOCATION OF THERMOSTAT")[0]["quantity"] == 5
    demo = [it for h, _g, _s, it in se.boq_items(out) if h.upper().startswith("DEMO")]
    assert all(it["demo_disposition"]["value"] == "REMOVE_TO_SOURCE" and it["demo_disposition"]["cite"] for it in demo)
    power = [it for h, g, sg, it in se.boq_items(out)
             if score.classify_line(it["description"], h, g, it.get("unit") or "", subgroup=sg).kind == "demo_power"]
    assert sum(r["quantity"] for r in power) == 35
    if two_line:
        assert [r["description"] for r in power] == [se.DEMO_POWER_LINE]
        assert len(_rows(out, r"^REMOVAL OF LIGHTING FIXTURE")) == 1
    else:
        assert [(r["description"], r["quantity"]) for r in power] == [(se.DEMO_POWER_LINE, 28), (se.DEMO_FLOOR_LINE, 7)]
        assert _rows(out, r"^REMOVE RECESSED DOWNLIGHT$"), "lighting rows stay itemized without the two-line style"
    assert not _rows(out, r"MAKE\s*SAFE")
    art = {"main": out, "boq_spec": out["boq"]}
    e22 = acc.check_e22(acc.AcceptanceInputs(artifacts=art, case_id="7154", options={"demo_two_line_style": two_line}))
    assert e22.status == "pass", e22.measured


def test_extra_allowances_need_a_document_amount_and_name_the_removed_allowance():
    out = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=_settings())
    assert out["estimate"]["extra_allowances"] == []
    removed = out["scope_policy"]["excluded_allowances"]
    assert len(removed) == 10
    texts = [e["text"] for e in out["scope_policy"]["exclusions"] + out["scope_policy"]["clarifications"]]
    for name in _main()["estimate"]["extra_allowances"]:
        assert any(name[:24] in t for t in texts), name
    main = _main()
    main["estimate"]["extra_allowances"] = ["OWNER ALLOWANCE FOR NETWORK SWITCH REPLACEMENT"]
    ctx = _ctx(document_amounts=[{"text": "Include an owner allowance of $5,000 for network switch replacement.",
                                  "document_id": "d", "page": 1, "match": "owner allowance"}])
    kept = se.enforce(main, ctx, SEED_POLICY, (), settings=_settings())
    assert kept["estimate"]["extra_allowances"] == ["OWNER ALLOWANCE FOR NETWORK SWITCH REPLACEMENT"]


def test_exclusions_cite_exhibit_a_for_every_owner_obligation():
    from app.services import plans_reader_acceptance as acc

    out = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=_settings())
    report = out["scope_policy"]
    art = {"main": out, "scope_report": report, "priced_estimate": {"lines": [], "allowances": []}}
    inp = acc.AcceptanceInputs(artifacts=art, case_id="7154")
    e7 = acc.check_e7(inp)
    e8 = acc.check_e8(inp)
    assert e7.status == "pass", e7.measured
    assert e8.status == "pass", e8.measured
    assert any("Honeywell" in e["text"] and "Exhibit A" in e["text"] and "Fire alarm" in e["text"]
               for e in report["exclusions"])


def test_compound_rfi_rewritten_by_removing_the_decided_clause():
    main = _main()
    main["brief"]["rfis"] = [
        {"source": "E0.01", "question": "Resolve above-ceiling rigid versus EMT permission and MC prohibition versus "
                                        "hospital-grade MC language. Delineate patient-care rooms requiring NEC 517.13 "
                                        "and hospital-grade receptacles."},
        {"source": "ED3.01", "question": "Confirm whether lighting conduit/conductors are retained/capped or removed to source."},
        {"source": "D6", "question": "Provide telemetry, Centrak and wireless access device inventory, relocation "
                                     "locations and storage conditions. Confirm the specific device-storage obligation "
                                     "governs over the general contents-storage exclusion."},
        {"source": "E0.04", "question": "Provide full L4 control information."},
    ]
    out = se.enforce(main, _ctx(), SEED_POLICY, (), settings=_settings())
    qs = [r["question"] for r in out["brief"]["rfis"]]
    assert qs == ["Delineate patient-care rooms requiring NEC 517.13 and hospital-grade receptacles.",
                  "Provide full L4 control information."]
    report = out["scope_policy"]
    assert len(report["rfis_rewritten"]) == 1 and len(report["rfis_removed"]) == 2


def test_rewrite_question_unit_rules():
    decided = se.decided_topic_patterns({"temporary_power", "thermostats", "firestopping"})
    q, removed = se.rewrite_question(
        "Confirm normal work hours, approved outage windows and available temporary power.", decided)
    assert q == "Confirm normal work hours and approved outage windows." and removed == ["temporary_power"]
    q, _ = se.rewrite_question(
        "Resolve electrical versus mechanical/controls scope for 120V ATC sources, breakers, five relocations, "
        "two new sensors and programming.", decided)
    assert q == ("Resolve electrical versus mechanical/controls scope for 120V ATC sources, breakers, two new "
                 "sensors and programming.")
    assert se.rewrite_question("Confirm temporary office loads and layouts.", decided)[0] is None
    assert se.rewrite_question("Provide full L4 control information.", decided) == ("Provide full L4 control information.", [])


def test_overrides_keep_chat_edits_and_apply_only_their_own_delta():
    main = _main()
    sub = main["boq"]["areas"][0]["sections"][1]["groups"][1]["subgroups"][1]["items"]
    sub.append({"description": "TEMPORARY POWER DISTRIBUTION", "quantity": 1, "unit": "LS", "origin": "chat"})
    sub.append({"description": "TEMPORARY LIGHTING STRINGERS", "quantity": 2, "unit": "EA"})
    out = se.enforce(main, _ctx(), SEED_POLICY, (), settings=_settings())
    assert _rows(out, r"TEMPORARY POWER DISTRIBUTION"), "a chat row is exempt from automatic rules"
    assert not _rows(out, r"TEMPORARY LIGHTING STRINGERS")
    excl = next(e for e in out["excluded_lines"] if e["description"] == "TEMPORARY LIGHTING STRINGERS")
    restored = se.enforce(out, _ctx(), SEED_POLICY, [{"decision_key": excl["decision_key"], "action": "include",
                                                     "note": "owner asked for stringers"}], settings=_settings())
    assert _rows(restored, r"TEMPORARY LIGHTING STRINGERS")
    assert all(e["line_id"] != excl["line_id"] for e in restored["excluded_lines"])
    assert any(o["key"] == excl["decision_key"] for o in restored["scope_policy"]["overrides_applied"])
    dimmer = _rows(restored, r"^WALL DIMMER$")[0]
    forced = se.enforce(restored, _ctx(), SEED_POLICY, [{"decision_key": f"line:{dimmer['line_id']}", "action": "exclude",
                                                        "note": "dimmers by the lighting vendor"}], settings=_settings())
    assert not _rows(forced, r"^WALL DIMMER$")
    assert any(e["source"] == "human" and e["line_id"] == dimmer["line_id"] for e in forced["excluded_lines"])
    assert _rows(forced, r"TEMPORARY POWER DISTRIBUTION")
    accepted = se.enforce(_main(), _ctx(), SEED_POLICY, [{"decision_key": "standard:raceway", "action": "accept",
                                                         "note": "EMT confirmed with the engineer"}], settings=_settings())
    assert not [n for n in accepted["scope_policy"]["needs_review"] if n["key"] == "standard:raceway"]


def test_report_shape_and_proposal_lines_are_suggestions_only():
    out = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=_settings())
    rep = out["scope_policy"]
    assert rep["schema"] == se.SCOPE_REPORT_SCHEMA
    for key in ("decisions", "clarifications", "exclusions", "proposal_lines", "warnings", "needs_review",
                "overrides_applied", "counts"):
        assert key in rep
    assert all(p["status"] == "suggested" for p in rep["proposal_lines"])
    assert rep["proposal_lines"][0]["kind"] == "template"
    wage = next(c for c in rep["clarifications"] if c["key"] == "bid_conditions:wage_type")
    assert "Prevailing wage" in wage["text"]
    race = next(c for c in rep["clarifications"] if c["key"] == "standard:raceway")
    assert "EMT" in race["text"]
    assert any(n["code"] == "raceway_conflict" for n in rep["needs_review"])


def test_digest_is_binding_and_capped():
    ctx = _ctx()
    digest = se.render_digest(ctx)
    assert digest.startswith("G3 SCOPE DECISIONS (binding")
    assert "prevailing_wage" in digest and "Honeywell" in digest
    short = se.render_digest(ctx, max_tokens=100)
    assert len(short) <= 300


def test_hooks_evaluate_rules_text_classify_obligation():
    hits = se.evaluate([{"line_id": "B9", "description": "TEMPORARY POWER FOR TRAILERS"},
                        {"line_id": "B10", "description": "DUPLEX RECEPTACLE"}], policy=SEED_POLICY)
    assert [(h.row_id, h.rule_id) for h in hits] == [("B9", "EX-TEMP-POWER")]
    text = se.rules_text(policy=SEED_POLICY)
    assert "p:EX-TEMP-POWER" in text and "p:EX-CUT-PATCH-PAINT" in text
    res = se.classify_obligation({"description": "TELEMETRY / CENTRAK / WAP SURVEY", "quantity": 1, "unit": "LS"}, _ctx())
    assert res["carry"] == "exclude"


def test_tr_census_with_no_hit_is_unknown_and_never_moves_quantity():
    from app.services.pagefacts.contracts import DocFacts

    facts = DocFacts.from_dict(_elec_facts())
    inputs = se.ScopeInputs([{"id": "elec", "category": "electrical_drawings", "name": "ELEC"}],
                            doc_facts=lambda _d: facts, pdf_bytes=lambda _d: b"%PDF-1.4")
    for res in ({"total": 0, "pages": {}}, {"total": 0, "pages": {"9": 0, "10": 0}}):
        df = se.device_facts(inputs, [], symbol_counter=lambda *_a, _r=res, **_k: _r)
        assert df.tr_count is None and "tr_symbol_census_zero" in df.warnings
    hit = se.device_facts(inputs, [], symbol_counter=lambda *_a, **_k: {"total": 3, "pages": {"9": 2, "10": 1}})
    assert hit.tr_count == 3 and hit.tr_method == "legend_symbol"
    main = {"boq": {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [
        {"name": "WIRING DEVICES", "subgroups": [{"name": "RECEPTACLES", "items": [
            {"description": "DUPLEX RECEPTACLE", "quantity": 20, "unit": "EA"},
            {"description": "DUPLEX RECEPTACLE, TR", "quantity": 6, "unit": "EA"}]}]}]}]}]}}
    for stored in (None, 0):
        out = se.enforce(main, {"device_facts": {"tr_count": stored}}, SEED_POLICY, ())
        assert [(r["description"], r["quantity"]) for r in _rows(out, r"DUPLEX")] == [
            ("DUPLEX RECEPTACLE", 20), ("DUPLEX RECEPTACLE, TR", 6)], stored
    moved = se.enforce(main, {"device_facts": {"tr_count": 3}}, SEED_POLICY, ())
    assert [r["quantity"] for r in _rows(moved, r"DUPLEX")] == [23, 3]


def test_control_cable_per_device_length_comes_only_from_settings():
    cat = {"counts": [{"description": "Relocate existing thermostat sensors and provide new conduit and wiring to VV",
                       "quantity": 5, "unit": "EA", "sheet_number": "M2.01"}]}
    inputs = se.ScopeInputs([], category_outputs={"x": cat})

    def cable(settings: Any) -> list[dict]:
        return [i for i in se.compute_inclusions(inputs, SEED_POLICY, settings=settings) if i["rule_id"] == "IN-CONTROL-CABLE"]

    assert cable(_settings(plans_reader_control_cable_ft_per_device=40))[0]["quantity"] == 200
    assert cable(_settings(plans_reader_control_cable_ft_per_device=0)) == [], "0 turns the assumed cable off"
    assert cable(None)[0]["quantity"] == 5 * get_settings().plans_reader_control_cable_ft_per_device

    class NoKey:
        pass

    assert cable(NoKey()) == [], "no settings attribute: no hard-coded length"


def test_drawn_fire_alarm_and_access_lines_are_excluded_by_the_verified_honeywell_fact():
    main = _main()
    items = main["boq"]["areas"][0]["sections"][1]["groups"][1]["subgroups"][1]["items"]
    for desc in ("FIRE ALARM HORN/STROBE", "SMOKE DETECTOR", "CARD READER", "FIRE ALARM PULL STATION"):
        items.append({"description": desc, "quantity": 3, "unit": "EA", "sheet_refs": ["E2.01"]})
    items.append({"description": "FIRE ALARM CONDUIT AND BOXES", "quantity": 3, "unit": "EA", "sheet_refs": ["E2.01"]})
    out = se.enforce(main, _ctx(), SEED_POLICY, (), settings=_settings())
    for desc in ("FIRE ALARM HORN/STROBE", "SMOKE DETECTOR", "CARD READER", "FIRE ALARM PULL STATION"):
        assert not _rows(out, rf"^{desc}$"), desc
        e = next(e for e in out["excluded_lines"] if e["description"] == desc)
        assert e["source"] == "fact" and e["party"] == "Honeywell" and e["fact_id"]
    assert _rows(out, r"^FIRE ALARM CONDUIT AND BOXES$"), "rough-in stays (for review) when the whole system is by others"
    codes = {(n["key"].split(":")[0], n["code"]) for n in out["scope_policy"]["needs_review"]}
    assert ("fact_review", "fact_conflict") in codes
    from app.services import plans_reader_acceptance as acc

    art = {"main": out, "scope_report": out["scope_policy"], "priced_estimate": {"lines": [], "allowances": []}}
    assert acc.check_e7(acc.AcceptanceInputs(artifacts=art, case_id="7154")).status == "pass"


# DB seams (fake client) -------------------------------------------------------------------------


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db: FakeSB, table: str):
        self.db, self.table = db, table
        self.op = "select"
        self.payload: Any = None
        self.filters: list[tuple[str, Any]] = []
        self.order_by: tuple[str, bool] | None = None
        self.lim: int | None = None

    def select(self, *_a, **_k):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self.op, self.payload = "upsert", (payload, on_conflict)
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, col, desc=False):
        self.order_by = (col, desc)
        return self

    def limit(self, n):
        self.lim = n
        return self

    def _match(self, row):
        return all(row.get(c) == v for c, v in self.filters)

    def execute(self):
        if self.table in self.db.missing:
            raise RuntimeError(f'relation "{self.table}" does not exist')
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            for it in items:
                if self.table in self.db.columns:
                    bad = set(it) - self.db.columns[self.table]
                    if bad:
                        raise RuntimeError(f"column {sorted(bad)} does not exist")
                rows.append(dict(it))
            return _Res([dict(i) for i in items])
        if self.op == "update":
            if self.table in self.db.columns and set(self.payload) - self.db.columns[self.table]:
                raise RuntimeError("column does not exist")
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self.payload)
            return _Res(hit)
        if self.op == "upsert":
            payload, conflict = self.payload
            keys = [k.strip() for k in (conflict or "").split(",") if k.strip()]
            for r in rows:
                if keys and all(r.get(k) == payload.get(k) for k in keys):
                    r.update(payload)
                    return _Res([dict(r)])
            rows.append(dict(payload))
            return _Res([dict(payload)])
        out = [dict(r) for r in rows if self._match(r)]
        if self.order_by:
            col, desc = self.order_by
            out.sort(key=lambda r: r.get(col) or 0, reverse=desc)
        if self.lim is not None:
            out = out[: self.lim]
        return _Res(out)


class FakeSB:
    def __init__(self, tables=None, *, missing=(), columns=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.missing = set(missing)
        self.columns = dict(columns or {})

    def table(self, name):
        return _Q(self, name)


RUN = {"id": "11111111-1111-4111-8111-111111111111", "project_id": None}


def test_prepare_writes_the_policy_output_and_tolerates_missing_md_columns():
    sb = FakeSB({"plans_reader_documents": [], "plans_reader_runs": [dict(RUN)],
                 "plans_reader_outputs": [{"run_id": RUN["id"], "scope": "category", "category": "electrical_drawings",
                                           "version": 1, "output": _category()}]},
                columns={"plans_reader_runs": {"id", "status"}})
    ctx = se.prepare(sb, RUN, None, allow_llm=False, settings=_settings())
    stored = [r for r in sb.tables["plans_reader_outputs"] if r["scope"] == "policy"]
    assert len(stored) == 1 and stored[0]["version"] == 1
    assert "category" not in stored[0], "None values are omitted from inserts"
    assert stored[0]["output"]["schema"] == se.SCOPE_CONTEXT_SCHEMA
    assert ctx["inclusions"] and ctx["policy_sha"] == scope_policy.active_policy().sha256
    se.prepare(sb, RUN, None, settings=_settings())
    assert sorted(r["version"] for r in sb.tables["plans_reader_outputs"] if r["scope"] == "policy") == [1, 2]


def test_facts_for_run_standards_for_and_overrides_for_run():
    ctx = _ctx()
    sb = FakeSB({"plans_reader_outputs": [{"run_id": RUN["id"], "scope": "policy", "version": 1, "output": ctx}]},
                missing={"plans_reader_scope_overrides"})
    facts = se.facts_for_run(sb, RUN["id"])
    assert facts and all(f.id.startswith("F") for f in facts)
    assert any(f.party == "Honeywell" and f.verified for f in facts)
    assert se.standards_for(sb, RUN["id"])["raceway"]["value"] == "EMT"
    assert se.overrides_for_run(sb, RUN["id"]) == []
    assert se.facts_for_run(None, RUN["id"]) == []
    assert se.facts_for_run(FakeSB(missing={"plans_reader_outputs"}), RUN["id"]) == []


def test_enforce_main_output_flag_off_unchanged_on_enforced_and_failure_safe(monkeypatch):
    main = _main()
    sb = FakeSB({"plans_reader_outputs": [{"run_id": RUN["id"], "scope": "policy", "version": 1, "output": _ctx()}],
                 "plans_reader_scope_overrides": []})
    off = se.enforce_main_output(sb, RUN, main, settings=_settings(ai_scope_policy_enforce=False))
    assert off == main
    on = se.enforce_main_output(sb, RUN, main, settings=_settings(ai_scope_policy_enforce=True))
    assert on["scope_policy"]["schema"] == se.SCOPE_REPORT_SCHEMA
    monkeypatch.setattr(se, "enforce", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert se.enforce_main_output(sb, RUN, main, settings=_settings(ai_scope_policy_enforce=True)) == main


def test_enforce_for_run_writes_the_next_main_version():
    sb = FakeSB({"plans_reader_outputs": [
        {"run_id": RUN["id"], "scope": "main", "version": 1, "output": _main(), "source_group_ids": ["g1"]},
        {"run_id": RUN["id"], "scope": "policy", "version": 1, "output": _ctx()},
    ], "plans_reader_scope_overrides": []})
    out = se.enforce_for_run(sb, RUN["id"], settings=_settings())
    assert out is not None and out["scope_policy"]
    mains = sorted(r["version"] for r in sb.tables["plans_reader_outputs"] if r["scope"] == "main")
    assert mains == [1, 2]
    assert se.enforce_for_run(FakeSB(), RUN["id"]) is None


def _deliverables(main: dict, versions: dict[str, int] | None = None) -> list[dict]:
    rows = []
    for kind in ("boq", "estimate", "brief"):
        spec = se._main_spec(kind, main)
        rows.append({"run_id": RUN["id"], "kind": kind, "version": (versions or {}).get(kind, 1),
                     "spec_json": copy.deepcopy(spec)})
    return rows


def _db_for_run(main: dict, deliverables: list[dict], overrides: list[dict] | None = None) -> FakeSB:
    return FakeSB({"plans_reader_outputs": [
        {"run_id": RUN["id"], "scope": "main", "version": 1, "output": main, "source_group_ids": ["g1"]},
        {"run_id": RUN["id"], "scope": "policy", "version": 1, "output": _ctx()},
    ], "plans_reader_deliverables": deliverables, "plans_reader_scope_overrides": list(overrides or []),
        "plans_reader_runs": [dict(RUN)]})


def test_enforce_for_run_enforces_the_latest_specs_and_keeps_chat_and_modify_edits():
    enforced = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=_settings())
    dels = _deliverables(enforced)
    boq = dels[0]["spec_json"]
    # a modify call added a row (strict schema: no line id, no origin) and changed a quantity
    sub = boq["areas"][0]["sections"][1]["groups"][1]["subgroups"][1]["items"]
    sub.append({"description": "TEMPORARY LIGHTING STRINGERS", "quantity": 2, "unit": "EA"})
    dimmer = next(it for *_x, it in se.boq_items({"boq": boq}) if it["description"] == "WALL DIMMER")
    dimmer["quantity"] = dimmer["quantity"] + 5
    for *_x, it in se.boq_items({"boq": boq}):
        it.pop("line_id", None)
    dels[0]["version"] = 2
    sb = _db_for_run(enforced, dels)
    renders: list[tuple[str, int]] = []

    def fake_render(_sb, run, kind, spec, *, version, instruction=None, **_k):
        renders.append((kind, version))
        return {"kind": kind, "version": version}

    out = se.enforce_for_run(sb, RUN["id"], settings=_settings(), render=fake_render)
    meta = out["scope_enforced_from"]
    assert meta["mode"] == "deliverable_specs" and meta["main_version"] == 1
    assert meta["deliverable_versions"] == {"boq": 2, "brief": 1, "estimate": 1} and meta["edited_kinds"] == ["boq"]
    added = _rows(out, r"^TEMPORARY LIGHTING STRINGERS$")
    assert added and added[0]["origin"] == "modify", "a modify row is exempt from the temporary-power rule"
    assert _rows(out, r"^WALL DIMMER$")[0]["quantity"] == dimmer["quantity"]
    assert dict(renders) == {"boq": 3, "estimate": 2}, renders
    mains = sorted(r["version"] for r in sb.tables["plans_reader_outputs"] if r["scope"] == "main")
    assert mains == [1, 2]
    # unchanged rows got their line ids back, so line overrides keep their target
    assert all(it.get("line_id") for *_x, it in se.boq_items(out))


def test_enforce_for_run_without_edits_enforces_main_and_records_it():
    enforced = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=_settings())
    sb = _db_for_run(enforced, _deliverables(enforced))
    out = se.enforce_for_run(sb, RUN["id"], settings=_settings(), render=lambda *a, **k: pytest.fail("no render"))
    assert out["scope_enforced_from"]["mode"] == "main" and out["scope_enforced_from"]["edited_kinds"] == []


def test_apply_override_applies_only_its_delta_to_the_latest_specs():
    enforced = se.enforce(_main(), _ctx(), SEED_POLICY, (), settings=_settings())
    dels = _deliverables(enforced)
    sub = dels[0]["spec_json"]["areas"][0]["sections"][1]["groups"][1]["subgroups"][1]["items"]
    sub.append({"description": "CHAT ADDED SURGE DEVICE", "quantity": 1, "unit": "EA"})
    dimmer = next(it for *_x, it in se.boq_items(enforced) if it["description"] == "WALL DIMMER")
    override = {"run_id": RUN["id"], "decision_key": f"line:{dimmer['line_id']}", "action": "exclude",
                "note": "dimmers by the lighting vendor"}
    sb = _db_for_run(enforced, dels, [override])
    rendered: dict[str, dict] = {}

    def fake_render(_sb, run, kind, spec, *, version, instruction=None, **_k):
        rendered[kind] = spec
        return {"version": version}

    off = se.apply_override(sb, RUN["id"], override, settings=_settings(ai_scope_policy_enforce=False), render=fake_render)
    assert off == {"applied": False, "reason": "enforcement_off"} and not rendered
    res = se.apply_override(sb, RUN["id"], override, settings=_settings(ai_scope_policy_enforce=True), render=fake_render)
    assert res["applied"] is True and res["enforced_from"]["rendered"] == {"boq": 2, "estimate": 2}
    boq_items = [it for *_x, it in se.boq_items({"boq": rendered["boq"]})]
    assert not [it for it in boq_items if it["description"] == "WALL DIMMER"]
    assert [it for it in boq_items if it["description"] == "CHAT ADDED SURGE DEVICE"], "the chat row is kept"
    before = {it["description"]: it["quantity"] for *_x, it in se.boq_items(enforced)}
    after = {it["description"]: it["quantity"] for it in boq_items}
    changed = {d for d in before if after.get(d) != before[d]}
    assert changed == {"WALL DIMMER"}, changed


def test_table_policy_loader_reads_the_active_row_or_falls_back():
    doc = copy.deepcopy(SEED_POLICY)
    doc["version"] = 7
    sb = FakeSB({"ai_scope_policies": [{"id": "p1", "version": 7, "status": "active", "doc": doc, "sha256": "x"}]})
    loader = se.table_policy_loader(lambda: sb, ttl_s=0)
    active = loader()
    assert active is not None and active.version == 7 and active.policy_id == "p1" and active.source == "table"
    assert se.table_policy_loader(lambda: FakeSB(missing={"ai_scope_policies"}), ttl_s=0)() is None
    scope_policy.reset_active_policy()
    try:
        scope_policy.set_active_policy_loader(loader)
        assert scope_policy.active_policy().version == 7
    finally:
        scope_policy.reset_active_policy()


def test_table_policy_loader_cache_is_short_and_invalidated_on_activation():
    doc7 = copy.deepcopy(SEED_POLICY)
    doc7["version"] = 7
    doc8 = copy.deepcopy(SEED_POLICY)
    doc8["version"] = 8
    sb = FakeSB({"ai_scope_policies": [{"id": "p1", "version": 7, "status": "active", "doc": doc7, "sha256": "x"},
                                       {"id": "p2", "version": 8, "status": "draft", "doc": doc8, "sha256": "y"}]})
    loader = se.table_policy_loader(lambda: sb, ttl_s=3600)
    assert loader().version == 7
    sb.tables["ai_scope_policies"][0]["status"] = "archived"
    sb.tables["ai_scope_policies"][1]["status"] = "active"
    assert loader().version == 7, "cached within the TTL"
    se.invalidate_policy_cache()
    assert loader().version == 8
    assert se.POLICY_TTL_S <= 60


# Tools ----------------------------------------------------------------------------------------


def test_scope_tools_are_registered_with_strict_args():
    for name in ("get_scope_policy", "get_facts"):
        spec = tools.get_spec(name) or (scope_tools.register() or tools.get_spec(name))
        assert spec is not None and spec.mode == "read"
        assert spec.args_schema["additionalProperties"] is False
        assert set(spec.args_schema["required"]) == set(spec.args_schema["properties"])
    assert tools.get_spec("get_scope_policy").kinds == frozenset({"main", "review", "revise", "chat"})
    assert tools.get_spec("get_facts").kinds == frozenset({"main", "review", "chat"})


def test_scope_tools_dispatch_through_the_service_slot_and_the_stored_context():
    scope_tools.register()
    spec = tools.get_spec("get_scope_policy")
    fake = tools.fake_services(policy=tools.FakeScopePolicyService(rules=[{"rule_id": "EX-BIM", "topic": "bim"}]))
    ctx = tools.ToolContext(run_id=RUN["id"], kind="main", services=fake)
    res = tools.dispatch(spec, ctx, {"topic": "bim", "item_description": None})
    assert not res.refused and res.citations == ["p:EX-BIM"]
    bad = tools.dispatch(spec, ctx, {"topic": "bim"})
    assert bad.refused and bad.code == "invalid_args"
    facts_spec = tools.get_spec("get_facts")
    assert tools.dispatch(facts_spec, ctx, {"system": None}).code == "unavailable"
    sb = FakeSB({"plans_reader_outputs": [{"run_id": RUN["id"], "scope": "policy", "version": 1, "output": _ctx()}]})
    db_ctx = tools.ToolContext(run_id=RUN["id"], kind="review", sb=sb, services=tools.ToolServices())
    res = tools.dispatch(facts_spec, db_ctx, {"system": "fire_alarm"})
    assert not res.refused and res.data["facts"] and all(f["system"] == "fire_alarm" for f in res.data["facts"])
    assert set(res.citations) == {f["id"] for f in res.data["facts"]}
    pol = tools.dispatch(spec, db_ctx, {"topic": "temporary_power", "item_description": None})
    assert not pol.refused and pol.data["bid_conditions"]["wage_type"] == "prevailing_wage"
    assert "p:EX-TEMP-POWER" in pol.citations and pol.data["standards"]["raceway"]["value"] == "EMT"


# Replay stages --------------------------------------------------------------------------------


def test_register_replay_stages_owns_derive_and_enforce():
    from app.services import plans_reader_replay as replay

    saved = dict(replay._STAGES)
    try:
        se.register_replay_stages()
        stages = replay.registered_stages()
        assert stages["derive"].owner == "WS5" and not stages["derive"].legacy
        assert stages["enforce"].owner == "WS5" and "scope_report" in stages["enforce"].writes
    finally:
        replay._STAGES.clear()
        replay._STAGES.update(saved)


# Golden: the 7154 replay on real parsed WS1 text --------------------------------------------------


GOLDEN_7154 = pytest.mark.skipif(
    not (BUNDLE / "bundle.json").exists() or not (REF_DIR / "expected.json").exists()
    or not (DEFAULT_BACKUP_DIR / "data" / "project_files.json").exists(),
    reason="EVAL_ROOT 7154 bundle, expected.json or the production backup is not on disk",
)


@pytest.fixture(scope="module", params=[False, True], ids=["style_off", "style_on"])
def replay_7154(request, tmp_path_factory):
    """The 7154 replay at both values of plans_reader_demo_two_line_style
    (false is the configured dev value)."""
    from app.services import eval_bundle
    from app.services import plans_reader_replay as replay

    saved = dict(replay._STAGES)
    try:
        se.register_replay_stages()
        bundle = eval_bundle.load_bundle(BUNDLE)
        result = replay.replay(
            bundle, from_stage="derive", to_stage="enforce",
            stages={"recount": "stored", "consolidate": "rerun"},
            out_dir=tmp_path_factory.mktemp("replay7154"),
            settings_overrides={"plans_reader_demo_two_line_style": request.param},
        )
    finally:
        replay._STAGES.clear()
        replay._STAGES.update(saved)
    result.two_line_style = request.param
    return result


@pytest.mark.golden
@GOLDEN_7154
def test_7154_replay_meets_the_ws5_acceptance_list(replay_7154):
    result = replay_7154
    assert result.ok, [s.error for s in result.stages if s.error]
    out = result.artifacts["main"]
    ctx = result.artifacts["scope_context"]
    report = result.artifacts["scope_report"]
    from app.services import eval_bundle

    eval_main = eval_bundle.load_bundle(BUNDLE).main_output
    original = eval_main["estimate"]["extra_allowances"]
    # extra allowances 10 -> 0
    assert len(original) == 10 and out["estimate"]["extra_allowances"] == []
    # FA, access, telemetry, Centrak, WAP, furniture, door hardware, paint: no rows, each excluded with an Exhibit A cite
    import re

    forbidden = re.compile(r"TELEMETRY|CENTRAK|\bWAP\b|WIRELESS ACCESS|FURNITURE (?!CONNECTION)|DOOR HARDWARE|\bPAINT"
                           r"|FIRE\s*ALARM|\bFA\b|ACCESS\s+(?:CONTROL|INTERFACE)", re.I)
    assert not [it["description"] for *_x, it in se.boq_items(out) if forbidden.search(it["description"])]
    texts = [e["text"] for e in report["exclusions"] + report["clarifications"]]
    for topic in ("fire alarm", "access control", "telemetry", "centrak", "wap", "furniture", "door hardware", "paint"):
        hits = [t for t in texts if topic in t.lower()]
        assert hits, topic
        assert any("Exhibit A" in t for t in hits), topic
    # breakers exactly 4 GFCI
    breakers = [it for *_x, it in se.boq_items(out) if brk.is_breaker_row(it["description"])]
    assert [(b["description"], b["quantity"]) for b in breakers] == [("20A/1P CIRCUIT BREAKER, GFCI", 4)]
    # thermostats 5 EA and control cable present
    assert [r["quantity"] for r in _rows(out, r"RELOCATION OF THERMOSTAT")] == [5]
    assert _rows(out, r"#18/4C CONTROL CABLE")[0]["quantity"] > 0
    # raceway EMT with clarification and no raceway RFI
    assert ctx["standards"]["raceway"]["value"] == "EMT"
    assert any(c["key"] == "standard:raceway" and "EMT" in c["text"] for c in report["clarifications"])
    rfis = [r["question"] for r in out["brief"]["rfis"]]
    assert not [q for q in rfis if re.search(r"\braceway\b|\bEMT\b|\brigid\b|\bMC\b", q, re.I)]
    # demolition remove-to-source with no retain/remove RFI
    assert ctx["standards"]["demo_disposition"]["value"] == "REMOVE_TO_SOURCE"
    assert not [q for q in rfis if re.search(r"maintain(?:ed)?\s+and\s+cap|retained/capped|\bremov\w*\b.{0,80}\bto\s+(?:the\s+)?source", q, re.I)]
    demo = [it for h, _g, _s, it in se.boq_items(out) if h.upper().startswith("DEMO")]
    assert demo and all(it["demo_disposition"]["value"] == "REMOVE_TO_SOURCE" for it in demo)
    # receptacles 74 with no style splits, TR 3 from the legend symbol count
    recs = [it for *_x, it in se.boq_items(out) if re.search(r"RECEPTACLE", it["description"]) and "REMOV" not in it["description"]]
    assert sum(r["quantity"] for r in recs) == 74
    assert not [r for r in recs if re.search(r"EMERGENCY|ESSENTIAL|NORMAL|LIFE SAFETY|DEDICATED", r["description"])]
    assert _rows(out, r"^DUPLEX RECEPTACLE, TR$")[0]["quantity"] == 3
    assert ctx["device_facts"]["tr_method"] == "legend_symbol" and ctx["device_facts"]["tr_count"] == 3
    # no RFI whose topics are all decided
    decided = se.decided_topic_patterns(report["decided_topics"])
    assert all(se.rewrite_question(q, decided)[0] == q for q in rfis)
    # wage PW
    assert ctx["bid_conditions"]["wage_type"] == "prevailing_wage"
    assert ctx["bid_conditions"]["wage_basis"] == "document_cue"
    print("ws5 7154 replay:", json.dumps({
        "statements": ctx["stats"]["statements"], "facts": ctx["stats"]["facts"],
        "verified_facts": ctx["stats"]["verified_facts"], "excluded_lines": len(out["excluded_lines"]),
        "rfis_before": len(eval_main["brief"]["rfis"]), "rfis_after": len(rfis),
        "rfis_removed": len(report["rfis_removed"]), "rfis_rewritten": len(report["rfis_rewritten"]),
        "needs_review": [n["code"] for n in report["needs_review"]],
    }))


@pytest.mark.golden
@GOLDEN_7154
def test_7154_replay_passes_the_ws5_acceptance_detectors(replay_7154):
    from app.services import plans_reader_acceptance as acc
    from app.services import plans_reader_score as score

    expected = json.loads((REF_DIR / "expected.json").read_text())
    reference = json.loads((REF_DIR / "reference.json").read_text())
    art = {k: v for k, v in replay_7154.artifacts.items() if not k.startswith("category:")}
    rows = score.rows_from_spec(art["boq_spec"])
    scored = score.score_case(rows, reference, adjudicated=True, case_id="7154")
    inp = acc.AcceptanceInputs(artifacts=art, case_id="7154", reference=reference, expected=expected, score=scored,
                               options={"demo_two_line_style": replay_7154.two_line_style})
    for cid in ("E5", "E7", "E8", "E12", "E14", "E20", "E21", "E22"):
        res = getattr(acc, f"check_{cid.lower()}")(inp)
        assert res.status == "pass", (cid, res.measured)
    e4 = acc.check_e4(inp)
    assert e4.measured["no_wage_rfi"]["status"] == "pass"  # the wage decision part needs the WS6 priced estimate
    e6 = acc.check_e6(inp)
    assert e6.measured["clarification_names_emt"]["status"] == "pass"
    assert e6.measured["no_raceway_rfi"]["status"] == "pass"  # the conduit part needs the WS3 derive stage
    priced = {"lines": [], "allowances": []}
    inp2 = acc.AcceptanceInputs(artifacts={**art, "priced_estimate": priced}, case_id="7154", expected=expected)
    assert acc.check_e7(inp2).status == "pass"
    assert acc.check_e8(inp2).status == "pass"


# Golden: FALSE-DROP over the golden estimator BOQs ----------------------------------------------------


BACKUP_CATEGORY_TO_RUN: dict[str, str] = {
    "electrical_drawing": "electrical_drawings", "drawing": "drawings", "specification": "specifications",
    "addendum": "addenda", "other": "other",
}
SCOPE_SOURCES = ("scope_engine", "scope_attribution", "scope_standards", "scope_breakers", "scope_devices",
                 "bid_conditions", "scope_policy", "scope_policy_seed")


def _golden_main(rows: list) -> dict:
    """An estimator BOQ as a main output; every row cites an electrical sheet
    (drawn), the conservative case for the party rule."""
    areas: dict[str, dict[str, dict[str, dict[str, list]]]] = {}
    for n, r in enumerate(rows, 1):
        sec = areas.setdefault(r.area or "AREA 1", {}).setdefault(r.section or "NEW WORK", {})
        sec.setdefault(r.group or "OTHER ITEMS", {}).setdefault(r.subgroup or "", []).append({
            "line_id": f"B{n}", "description": r.description, "quantity": r.quantity, "unit": r.unit,
            "is_accessory": bool(r.is_accessory), "sheet_refs": ["E1.01"]})
    spec = {"areas": [{"area": a, "sections": [{"heading": s, "groups": [{"name": g, "subgroups": [
        {"name": sg, "items": items} for sg, items in gv.items()]} for g, gv in sv.items()]} for s, sv in av.items()]}
        for a, av in areas.items()]}
    return {"boq": spec, "estimate": {"extra_allowances": []}, "brief": {"rfis": []}}


def _golden_inputs(data: Any, pid: str, main: dict) -> tuple[se.ScopeInputs, str] | None:
    """The ScopeInputs of one golden project from the backup, read only
    through the EVAL_ROOT caches: WS1 page facts of its electrical drawing
    PDFs (cache/pagefacts/<sha256>.json) and the text layer of its
    specifications, addenda and other PDFs (cache/ws5_pdf_text). No PDF is
    extracted here and no symbol census runs (TR stays unknown). None when
    neither cache holds anything for the project. The second value keys
    the cached context."""
    import hashlib

    from app.services.pagefacts.contracts import DocFacts

    cache = EVAL_ROOT / "cache"
    docs: list[dict] = []
    facts: dict[str, Any] = {}
    texts: dict[str, list[str]] = {}
    seen: set[str] = set()
    for f in data.files(pid):
        cat = BACKUP_CATEGORY_TO_RUN.get(str(f.get("category") or ""))
        if cat is None or not str(f.get("filename", "")).lower().endswith(".pdf"):
            continue
        fid = str(f["id"])
        if cat in ("electrical_drawings",):
            path = data.file_path(f)
            if not path.exists():
                continue
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if sha in seen:
                continue  # the same set uploaded twice counts once
            seen.add(sha)
            pf = cache / "pagefacts" / f"{sha}.json"
            if pf.exists():
                facts[fid] = DocFacts.from_dict(json.loads(pf.read_text()))
                docs.append({"id": fid, "name": f.get("filename"), "category": cat,
                             "page_count": facts[fid].page_count})
            continue
        if cat == "drawings":
            continue  # no WS1 facts are cached for non-electrical drawing sets
        for n in (400, 4):
            tp = cache / "ws5_pdf_text" / f"{fid}_{n}.json"
            if tp.exists():
                texts[fid] = list(json.loads(tp.read_text()).get("pages") or [])
                docs.append({"id": fid, "name": f.get("filename"), "category": cat, "page_count": len(texts[fid])})
                break
    if not docs:
        return None

    def _lines(doc_id: str, page: int) -> list[str]:
        pages = texts.get(doc_id) or []
        if 1 <= page <= len(pages):
            return [ln.strip() for ln in str(pages[page - 1]).splitlines() if ln.strip()]
        return []

    project = data.project(pid) if hasattr(data, "project") else None
    inputs = se.ScopeInputs(docs, project=project, main_output=main, doc_facts=lambda d: facts.get(d),
                            page_lines=_lines, pdf_bytes=lambda _d: None)
    src = "".join((Path(se.__file__).parent / f"{m}.py").read_text() for m in SCOPE_SOURCES)
    key = hashlib.sha256((src + scope_policy.policy_sha(SEED_POLICY) + json.dumps(sorted(d["id"] for d in docs))
                          + json.dumps([r["description"] for *_x, r in se.boq_items(main)])).encode()).hexdigest()[:20]
    return inputs, key


def _golden_context(data: Any, pid: str, main: dict) -> dict | None:
    """prepare_context over the project's cached WS1 facts and text, cached
    under EVAL_ROOT/cache/ws5_scope_context (keyed by the scope code, the
    seed policy, the documents and the BOQ)."""
    got = _golden_inputs(data, pid, main)
    if got is None:
        return None
    inputs, key = got
    path = EVAL_ROOT / "cache" / "ws5_scope_context" / f"{pid}_{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    ctx = se.prepare_context(inputs, policy=scope_policy.seed_policy(), settings=_settings(), allow_llm=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ctx, default=str))
    except OSError:
        pass
    return ctx


def _false_drops(main: dict, out: dict) -> tuple[int, int, list[str]]:
    after = {it["line_id"]: (it["description"], float(it.get("quantity") or 0)) for *_x, it in se.boq_items(out)}
    total = changed = 0
    examples: list[str] = []
    for *_x, it in se.boq_items(main):
        if it.get("is_accessory"):
            continue
        total += 1
        got = after.get(it["line_id"])
        if got is None or got[0] != it["description"] or got[1] != float(it["quantity"] or 0):
            changed += 1
            if len(examples) < 400:
                examples.append(f"{it['description']} {it['quantity']} {it['unit']} -> {got}")
    return changed, total, examples


@pytest.mark.golden
@pytest.mark.skipif(not (DEFAULT_BACKUP_DIR / "data" / "project_files.json").exists(),
                    reason="the production backup is not on disk")
def test_false_drop_rate_on_golden_estimator_boqs_is_at_most_one_percent():
    """FALSE-DROP (spec WS5 acceptance): estimator rows dropped or replaced by
    enforce, over the non-holdout golden BOQs (excluded_projects
    ('scope_policy') removes the holdout). Measured twice: with an empty
    ScopeContext (policy rules and receptacle merges only) and with the real
    ScopeContext from prepare_context over each project's cached WS1 facts
    and document text (facts, obligations, breaker plan, standards and
    demolition canonicalization, inclusions). The gate reads the real one."""
    from app.services import eval_manifest as em
    from app.services.plans_reader_score import parse_estimator_boq

    data = em.BackupData(DEFAULT_BACKUP_DIR)
    excluded = em.excluded_projects("scope_policy")
    empty = {"changed": 0, "total": 0, "projects": 0}
    real = {"changed": 0, "total": 0, "projects": 0, "facts": 0, "verified_facts": 0}
    examples: list[str] = []
    for pid in [g for g in em.GOLDEN_IDS if g not in excluded]:
        boq = [r for r in data.files(pid, "boq") if str(r.get("filename", "")).lower().endswith((".xlsx", ".xlsm"))]
        if not boq:
            continue
        try:
            rows = parse_estimator_boq(data.file_path(boq[-1]).read_bytes())
        except Exception:  # noqa: BLE001 - an unparseable book is skipped (the parser has its own tests)
            continue
        main = _golden_main(rows)
        c, t, _ex = _false_drops(main, se.enforce(main, {}, SEED_POLICY, ()))
        empty["changed"] += c
        empty["total"] += t
        empty["projects"] += 1
        ctx = _golden_context(data, pid, main)
        if ctx is None:
            continue
        out = se.enforce(main, ctx, SEED_POLICY, (), settings=_settings())
        c, t, ex = _false_drops(main, out)
        real["changed"] += c
        real["total"] += t
        real["projects"] += 1
        real["facts"] += int((ctx.get("stats") or {}).get("facts") or 0)
        real["verified_facts"] += int((ctx.get("stats") or {}).get("verified_facts") or 0)
        examples.extend(f"{pid[:8]}: {e}" for e in ex)
    empty_rate = empty["changed"] / max(1, empty["total"])
    real_rate = real["changed"] / max(1, real["total"])
    print(f"false-drop empty context: {empty['changed']} of {empty['total']} estimator rows over "
          f"{empty['projects']} golden BOQs ({empty_rate:.4f})")
    print(f"false-drop real context: {real['changed']} of {real['total']} estimator rows over {real['projects']} "
          f"golden BOQs with cached WS1 facts or text ({real_rate:.4f}); facts {real['facts']}, verified "
          f"{real['verified_facts']}")
    for e in examples[:60]:
        print("  ", e)
    assert empty["projects"] >= 50 and empty["total"] >= 3000
    assert real["projects"] >= 45 and real["total"] >= 2500
    assert empty_rate <= 0.01
    assert real_rate <= 0.01


# Wave 3 fix Y3 (run 51ec302b): thermostats from the mechanical page facts, category questions resolved ------


def _mech_facts(*, markers_new: int = 5, markers_demo: int = 5) -> Any:
    """A DocFacts-shaped object with the 7154 mechanical keynotes (MD2.01 KN10
    and M2.01 KN10 and KN11) and their keynote markers."""
    from types import SimpleNamespace

    def page(n: int, sheet: str, title: str, notes: list[dict], markers: dict[str, int]) -> Any:
        return SimpleNamespace(page=n, sheet_number=sheet, sheet_title=title, roles=["plan"], page_role="plan",
                               primary_ft_per_in=4.0, notes=notes,
                               keynote_markers=[{"number": k, "x": 0.1 * i, "y": 0.5, "confidence": 0.6}
                                                for k, c in markers.items() for i in range(c)])

    kn_demo = {"kind": "keynotes", "items": [
        {"number": "10", "text": "EXISTING THERMOSTAT SENSORS TO BE RELOCATED PER NEW WORK PLAN. REMOVE ALL WIRING AND "
                                 "CONDUITS UP TO VV AS INDICATED."}]}
    kn_new = {"kind": "keynotes", "items": [
        {"number": "10", "text": "RELOCATE EXISTING THERMOSTAT SENSOR AS INDICATED. COORDINATE FINAL LOCATION WITH "
                                 "ARCHITECT. PROVIDE NEW CONDUIT WIRING UP TO VV AS INDICATED."},
        {"number": "11", "text": "PROVIDE NEW THERMOSTAT SENSOR WIRING AND CONDUIT UP TO VV AS INDICATED."}]}
    return SimpleNamespace(pages=[
        page(5, "MD2.01", "OVERALL MECHANICAL PLAN - DEMOLITION WORK", [kn_demo], {"10": markers_demo, "1": 18}),
        page(7, "M2.01", "OVERALL MECHANICAL PLAN - NEW WORK", [kn_new], {"10": markers_new, "11": 2, "14": 19}),
    ], legend={"entries": []}, schedules=[])


def _no_thermostat_category() -> dict:
    cat = _category()
    cat["counts"] = [r for r in cat["counts"] if not se.THERMOSTAT_FAMILY_RE.search(str(r.get("description") or ""))]
    return cat


def test_thermostat_inclusion_falls_back_to_the_mechanical_keynote_facts_when_no_row_carries_it():
    mech = {"id": "mech", "name": "MECH", "category": "mechanical_drawings", "page_count": 8}
    inputs = se.ScopeInputs([mech], category_outputs={"electrical_drawings": _no_thermostat_category()},
                            doc_facts=lambda _d: _mech_facts())
    incs = {i["rule_id"]: i for i in se.compute_inclusions(inputs, SEED_POLICY, settings=_settings())}
    reloc = incs["IN-THERMOSTAT-RELOC"]
    assert reloc["quantity"] == 5 and reloc["basis"] == "counted" and reloc["trigger_source"] == "page_facts"
    # the new-work sheet wins over the demolition sheet (5, not 10); KN11 (new wiring) is not a relocation
    assert [(t["sheet"], t["ref"], t["quantity"], t["used"]) for t in reloc["triggers"]] == \
        [("MD2.01", "KN10", 5.0, False), ("M2.01", "KN10", 5.0, True)]
    cable = incs["IN-CONTROL-CABLE"]
    assert cable["quantity"] == 125 and cable["basis"] == "assumed_per_trigger" and cable["trigger_source"] == "page_facts"
    # enforce inserts both lines citing the sheet and keynote
    out = se.enforce(_main(), _ctx(inclusions=list(incs.values())), SEED_POLICY, settings=_settings())
    therm = _rows(out, r"RELOCATION OF THERMOSTAT")
    assert [(t["quantity"], t["unit"], t["sheet_refs"], t["basis"]) for t in therm] == [(5, "EA", ["M2.01"], "policy")]
    assert "M2.01 KN10" in therm[0]["material_note"]
    assert [(c["quantity"], c["caveat"]) for c in _rows(out, r"#18/4C CONTROL CABLE")] == [(125, "(ASSUMED)")]
    # the reader rows still win when they exist (the imported runs' C49/C53)
    inputs2 = se.ScopeInputs([mech], category_outputs={"electrical_drawings": _category()},
                             doc_facts=lambda _d: _mech_facts())
    incs2 = {i["rule_id"]: i for i in se.compute_inclusions(inputs2, SEED_POLICY, settings=_settings())}
    assert incs2["IN-THERMOSTAT-RELOC"]["trigger_source"] == "reader_rows"
    assert all(t["source"] != "page_facts" for t in incs2["IN-THERMOSTAT-RELOC"]["triggers"] if "source" in t)
    assert incs2["IN-THERMOSTAT-RELOC"]["quantity"] == 5


def test_thermostat_keynote_without_markers_carries_no_quantity_and_the_demolition_sheet_backs_it_up():
    mech = {"id": "mech", "name": "MECH", "category": "mechanical_drawings", "page_count": 8}
    cats = {"electrical_drawings": _no_thermostat_category()}
    inputs = se.ScopeInputs([mech], category_outputs=cats, doc_facts=lambda _d: _mech_facts(markers_new=0))
    incs = {i["rule_id"]: i for i in se.compute_inclusions(inputs, SEED_POLICY, settings=_settings())}
    assert incs["IN-THERMOSTAT-RELOC"]["quantity"] == 5  # MD2.01 counts when M2.01 has no marker
    assert [t["used"] for t in incs["IN-THERMOSTAT-RELOC"]["triggers"]] == [True, False]
    inputs = se.ScopeInputs([mech], category_outputs=cats,
                            doc_facts=lambda _d: _mech_facts(markers_new=0, markers_demo=0))
    assert "IN-THERMOSTAT-RELOC" not in {i["rule_id"] for i in se.compute_inclusions(inputs, SEED_POLICY, settings=_settings())}


Q17 = "Are lighting conduits and conductors to be retained and capped for reuse, or removed back to source?"
Q2 = "Is EMT acceptable above ceilings, or is RGS/IMC required for all above-ceiling wiring?"
Q18 = "Confirm track lengths and individual heads per cluster, and verify the two small entry fixtures are exit signs."


def _questions() -> list[dict]:
    return [{"row_id": "Q17", "category": "electrical_drawings", "sheet": "ED3.01", "question": Q17},
            {"row_id": "Q2", "category": "electrical_drawings", "sheet": "E0.01", "question": Q2},
            {"row_id": "Q18", "category": "electrical_drawings", "sheet": "ED3.01", "question": Q18},
            {"row_id": "Q2", "category": "specifications", "sheet": None,
             "question": "Confirm the lockset hardware sets and substitution precedence for the pharmacy doors."}]


def test_category_questions_decided_by_the_standards_are_resolved_with_the_deciding_clarification():
    ctx = _ctx(questions=_questions())
    out = se.enforce(_main(), ctx, SEED_POLICY, settings=_settings())
    rep = out["scope_policy"]
    resolved = {(q["category"], q["row_id"]): q for q in rep["questions_resolved"]}
    e20 = resolved[("electrical_drawings", "Q17")]
    assert e20["topics"] == ["demo_disposition"] and "demo_disposition" in e20["decision_keys"]
    assert "back to source" in e20["clarification"] and "ED3.01 KN1" in e20["clarification"]
    assert resolved[("electrical_drawings", "Q2")]["topics"] == ["raceway"]
    assert "EMT" in resolved[("electrical_drawings", "Q2")]["clarification"]
    assert resolved[("specifications", "Q2")]["topics"] == ["door_hardware"]  # row ids repeat across categories
    assert ("electrical_drawings", "Q18") not in resolved and not rep["questions_rewritten"]
    assert rep["counts"]["questions_resolved"] == 3
    # the demolition disposition still lands on every demolition line with its citation (E20 second part)
    demo = [it for h, _g, _s, it in se.boq_items(out) if h.upper().startswith("DEMO")]
    assert demo and all(it["demo_disposition"]["cite"] for it in demo)
    # idempotent and carried through a second enforce
    again = se.enforce(out, ctx, SEED_POLICY, settings=_settings())
    assert again["scope_policy"]["questions_resolved"] == rep["questions_resolved"]
    # a human override keeps a question open
    ov = [{"decision_key": "question:electrical_drawings:Q17", "action": "include", "note": "ask anyway"}]
    kept = se.enforce(_main(), ctx, SEED_POLICY, ov, settings=_settings())["scope_policy"]
    assert ("electrical_drawings", "Q17") not in {(q["category"], q["row_id"]) for q in kept["questions_resolved"]}
    assert any(o["key"] == "question:electrical_drawings:Q17" for o in kept["overrides_applied"])


def test_prepare_context_collects_the_reader_questions_and_the_report_lists_them_resolved():
    cat = _category()
    cat["questions"] = [{"row_id": "Q17", "question": Q17, "sheet_number": "ED3.01", "status": "active"},
                        {"row_id": "Q9", "question": "old", "sheet_number": "E0.03", "status": "superseded"}]
    inputs = se.ScopeInputs([], category_outputs={"electrical_drawings": cat})
    assert se.category_questions(inputs) == [{"row_id": "Q17", "category": "electrical_drawings", "sheet": "ED3.01",
                                              "question": Q17}]
    ctx = se.prepare_context(inputs, policy=scope_policy.seed_policy(), settings=_settings())
    assert ctx["questions"] == se.category_questions(inputs)


# Dev run 51ec302b (integration run 1 of 7154): the reader's Q11 and the S9
# luminaire schedule it read on E0.04 (L4 CONTROL METHOD readable: LOCAL).
Q11 = ("Confirm L5 linear lengths versus fixture-unit quantities, connection accessories and power supply arrangement, "
       "and confirm L4 local control.")
S9_COLUMNS = ["TAG", "DESCRIPTION", "MTG", "LAMP TYPE", "DELIVERED LUMENS", "WATTS", "VOLTAGE", "CONTROL METHOD",
              "MANUFACTURER - SERIES"]
S9_ROWS = [["L1", "2' X 4' RECESSED TROFFER", "RE", "LED", "3,071 lm", "40 / FIX", "277", "0-10V", "METALUX - #24GR"],
           ["L4", "TASK LED LIGHTING", "WL", "LED", "N/A", "9 / FIX", "120", "LOCAL", "WAC LIGHTING #BL-29903"],
           ["L5", "UNDERCABINET LED LIGHTING", "UC", "LED", "345 lm", "5 / FIX", "120", "LOCAL", "WAC LIGHTING #LS-LED14P"]]


def _run_51ec_category() -> dict:
    sheet = {"page": 5, "sheet_key": "D2P5", "document_id": "elec", "sheet_number": "E0.04"}
    out = {"category": "electrical_drawings", "rows_version": 2, "summary": "", "pages": [], "floors": [], "merge_log": {},
           "counts": [], "runs": [], "scope": [], "specs": [], "details": []}
    out["schedules"] = [{"row_id": "S9", "name": "LUMINAIRE SCHEDULE", "schedule_type": "fixture", "columns": S9_COLUMNS,
                         "rows": S9_ROWS, "status": "active", "page": 5, "document_id": "elec", "sheet_number": "E0.04",
                         "sheet": sheet}]
    out["questions"] = [{"row_id": "Q11", "question": Q11, "sheet_number": "E0.04", "status": "active", "sheet": sheet},
                        {"row_id": "Q12", "question": "Confirm the L4 control method.", "sheet_number": "E0.04",
                         "status": "active", "sheet": sheet}]
    return out


def test_a_question_the_luminaire_schedule_answers_is_resolved_or_rewritten():
    """E19: the schedule's readable CONTROL METHOD cell for L4 (LOCAL)
    answers "confirm L4 local control": the compound Q11 keeps its L5 clause
    and records the clarification, the single-clause question is resolved."""
    inputs = se.ScopeInputs([], category_outputs={"electrical_drawings": _run_51ec_category()})
    sched = {e["tag"]: e for e in se.category_luminaire_schedule(inputs)}
    assert sched["L4"] == {"tag": "L4", "description": "TASK LED LIGHTING", "control": "LOCAL", "ref": "S9#r2", "sheet": "E0.04"}
    ctx = se.prepare_context(inputs, policy=scope_policy.seed_policy(), settings=_settings())
    assert ctx["luminaire_schedule"] == se.category_luminaire_schedule(inputs)
    out = se.enforce(_main(), _ctx(questions=ctx["questions"], luminaire_schedule=ctx["luminaire_schedule"]), SEED_POLICY,
                     settings=_settings())
    rep = out["scope_policy"]
    q11 = {q["row_id"]: q for q in rep["questions_rewritten"]}["Q11"]
    assert q11["key"] == "question:electrical_drawings:Q11" and q11["topics"] == ["luminaire_control:L4"]
    assert q11["after"] == "Confirm L5 linear lengths versus fixture-unit quantities, connection accessories and power supply arrangement."
    assert q11["clarification"] == "L4 control LOCAL per the luminaire schedule (E0.04)"
    assert q11["decision_keys"] == ["luminaire_schedule:S9#r2"]
    q12 = {q["row_id"]: q for q in rep["questions_resolved"]}["Q12"]
    assert q12["clarification"] == "L4 control LOCAL per the luminaire schedule (E0.04)"
    # a human override keeps it open
    ov = [{"decision_key": "question:electrical_drawings:Q12", "action": "include", "note": "ask anyway"}]
    kept = se.enforce(_main(), _ctx(questions=ctx["questions"], luminaire_schedule=ctx["luminaire_schedule"]), SEED_POLICY, ov,
                      settings=_settings())["scope_policy"]
    assert "Q12" not in {q["row_id"] for q in kept["questions_resolved"]}


def test_an_unreadable_control_cell_leaves_the_luminaire_question_standing():
    """The weekend fixture reads the L4 CONTROL cell as "[unreadable]": the
    schedule decides nothing for L4, so Q11 stands (L5 LOCAL is readable but
    Q11 asks about L5 lengths, not its control)."""
    inputs = se.ScopeInputs([], category_outputs={"electrical_drawings": _category()})
    sched = {e["tag"]: e for e in se.category_luminaire_schedule(inputs)}
    assert sched["L4"]["control"] is None and sched["L5"]["control"] == "LOCAL"
    questions = [{"row_id": "Q11", "category": "electrical_drawings", "sheet": "E0.04", "question": Q11},
                 {"row_id": "Q12", "category": "electrical_drawings", "sheet": "E0.04", "question": "Confirm the L4 control method."}]
    rep = se.enforce(_main(), _ctx(questions=questions, luminaire_schedule=list(sched.values())), SEED_POLICY,
                     settings=_settings())["scope_policy"]
    touched = {q["row_id"] for q in rep["questions_resolved"] + rep["questions_rewritten"]}
    assert "Q11" not in touched and "Q12" not in touched
    assert se.category_luminaire_schedule(se.ScopeInputs([], category_outputs={})) == []


def test_rigid_without_conduit_steel_or_metal_is_not_a_raceway_mention():
    statements = [
        Statement(text="EMT WITH STEEL INSULATED THROAT SET SCREW FITTINGS MAY BE USED IN DRY, PROTECTED INTERIOR LOCATIONS.",
                  document_id="elec", page=2, role="general_note", level="general_note", sheet="E0.01", ref="GN5"),
        Statement(text="Extruded rigid plastic over aluminum retainer.", document_id="spec", page=151, role="spec",
                  level="spec"),
        Statement(text="Fabricate hollow metal work to be rigid and free of defects.", document_id="spec", page=85,
                  role="spec", level="spec"),
        Statement(text="Provide weather stripping with integral barrier fin or fins of semi-rigid material.",
                  document_id="spec", page=103, role="spec", level="spec"),
    ]
    d = ss.resolve_raceway(statements, SEED_POLICY)
    assert d.value == "EMT" and d.source == "documents" and d.needs_review is None
    # a real rigid conduit requirement on an electrical note still conflicts (7154 E0.01)
    rigid = Statement(text="ALL WIRING ABOVE CEILING SHALL BE IN RIGID CONDUIT.", document_id="elec", page=2,
                      role="general_note", level="general_note", sheet="E0.01", ref="GN6")
    d2 = ss.resolve_raceway(statements + [rigid], SEED_POLICY)
    assert d2.value == "EMT" and d2.needs_review == ss.RACEWAY_REVIEW
    assert [m.cite for m in d2.conflicts] == ["E0.01 GN5", "E0.01 GN6"]
    # an unlabeled spec sentence that names conduit still counts
    spec = Statement(text="All branch circuit raceways shall be rigid galvanized steel conduit.", document_id="spec",
                     page=3, role="spec", level="spec")
    assert ss.resolve_raceway([spec], SEED_POLICY).value == "RGS"


RUN_51EC = EVAL_ROOT / "bundles" / "7154" / "51ec302b"


@pytest.mark.golden
@pytest.mark.skipif(not (RUN_51EC / "bundle.json").is_file() or not (BUNDLE / "bundle.json").is_file(),
                    reason="the 7154 run bundles are not on disk")
def test_run_51ec302b_stored_output_enforced_gives_thermostats_tr_and_no_demo_question():
    """$0 measurement on the first full live run of 7154: the stored main
    output and policy context, the thermostat inclusion from the relabeled
    mechanical set's page facts (extracted from the same PDF bytes in the
    weekend bundle), the reader questions from the stored category outputs."""
    import re

    from app.services import eval_bundle

    run = eval_bundle.load_bundle(RUN_51EC)
    week = eval_bundle.load_bundle(BUNDLE)
    main = json.loads((RUN_51EC / "outputs" / "main_none_v1.json").read_text())
    ctx = json.loads((RUN_51EC / "outputs" / "policy_none_v1.json").read_text())
    cats: dict[str, tuple[int, dict]] = {}
    for p in (RUN_51EC / "outputs").glob("category_*_v*.json"):
        o = json.loads(p.read_text())
        v = int(p.stem.rsplit("_v", 1)[1])
        if o["category"] not in cats or v > cats[o["category"]][0]:
            cats[o["category"]] = (v, o)
    outputs = {c: o for c, (_v, o) in cats.items()}
    mech = next(d for d in run.documents if d["category"] == "mechanical_drawings")
    assert mech["category_label"] == "electrical_drawings" and mech["facts_status"] == "done"
    mech_week = next(d for d in week.documents if "MECH" in str(d.get("name")))
    pdf = week.doc_bytes(mech_week["id"])
    inputs = se.ScopeInputs([mech], category_outputs=outputs, pdf_bytes=lambda _d: pdf)
    incs = {i["rule_id"]: i for i in se.compute_inclusions(inputs, SEED_POLICY, settings=_settings())}
    assert incs["IN-THERMOSTAT-RELOC"]["quantity"] == 5 and incs["IN-THERMOSTAT-RELOC"]["trigger_source"] == "page_facts"
    assert [(t["sheet"], t["ref"], t["quantity"]) for t in incs["IN-THERMOSTAT-RELOC"]["triggers"] if t["used"]] == \
        [("M2.01", "KN10", 5.0)]
    assert incs["IN-CONTROL-CABLE"]["quantity"] == 125
    assert incs["IN-EQUIP-CONNECTION"]["quantity"] == 7
    ctx = {**ctx, "inclusions": list(incs.values()), "questions": se.category_questions(inputs)}
    out = se.enforce(main, ctx, SEED_POLICY, settings=_settings())
    rep = out["scope_policy"]
    assert [r["quantity"] for r in _rows(out, r"RELOCATION OF THERMOSTAT")] == [5]
    assert [r["quantity"] for r in _rows(out, r"#18/4C CONTROL CABLE")] == [125]
    assert _rows(out, r"^DUPLEX RECEPTACLE$")[0]["quantity"] == 54
    assert _rows(out, r"^DUPLEX RECEPTACLE, TR$")[0]["quantity"] == 3
    recs = [it for *_x, it in se.boq_items(out) if re.search(r"RECEPTACLE", it["description"]) and "REMOV" not in it["description"]]
    assert sum(r["quantity"] for r in recs) == 74
    assert not [r for r in recs if re.search(r"EMERGENCY|ESSENTIAL|NORMAL|LIFE SAFETY|DEDICATED|\bBRANCH\b", r["description"])]
    tr = next(d for d in rep["decisions"] if d["key"] == "devices:tr")["after"]
    assert tr["applied"] and tr["reduced"] and tr["from_line"] == "B5"
    resolved = {(q["category"], q["row_id"]): q for q in rep["questions_resolved"]}
    assert resolved[("electrical_drawings", "Q17")]["topics"] == ["demo_disposition"]
    assert "ED3.01 KN1" in resolved[("electrical_drawings", "Q17")]["clarification"]
    assert resolved[("electrical_drawings", "Q2")]["topics"] == ["raceway"]
    e20 = re.compile(r"maintain(?:ed)?\s+and\s+cap|\bcapp?(?:ed)?\b.{0,80}\bremov|\bremov\w*\b.{0,80}\bto\s+(?:the\s+)?source\b"
                     r"|retained/capped", re.I | re.S)
    open_questions = [r["question"] for r in out["brief"]["rfis"]]
    open_questions += [q["question"] for q in ctx["questions"] if (q["category"], q["row_id"]) not in resolved]
    assert not [q for q in open_questions if e20.search(q)]
    assert not [q for q in open_questions if re.search(r"\braceway\b|\bEMT\b|\brigid\b", q, re.I)]
    demo = [it for h, _g, _s, it in se.boq_items(out) if h.upper().startswith("DEMO")]
    assert demo and all(it["demo_disposition"]["value"] == "REMOVE_TO_SOURCE" for it in demo)
    # the raceway review stays: E0.01 really conflicts (EMT permitted vs rigid above ceiling)
    assert "raceway_conflict" in {n["code"] for n in rep["needs_review"]}
    assert json.dumps(se.enforce(out, ctx, SEED_POLICY, settings=_settings()), sort_keys=True) == json.dumps(out, sort_keys=True)


# Provenance of engine lines and brief RFI resolution (integration run 2, 2492ba7a) -------------


def _mech_lines() -> dict[int, list[str]]:
    """Text-layer lines of the two mechanical plan pages as the WS1 text
    layer reads a keynote column (numbered items wrapped over lines)."""
    return {
        5: ["KEYNOTES", "1. EXISTING DUCTWORK TO REMAIN.", "10. EXISTING THERMOSTAT SENSORS TO BE",
            "RELOCATED PER NEW WORK PLAN. REMOVE ALL", "WIRING AND CONDUITS UP TO VV AS INDICATED."],
        7: ["KEYNOTES", "10. RELOCATE EXISTING THERMOSTAT SENSOR", "AS INDICATED. COORDINATE FINAL LOCATION WITH",
            "11. PROVIDE NEW THERMOSTAT SENSOR WIRING AND", "CONDUIT UP TO VV AS INDICATED."],
    }


def test_page_facts_inclusion_cites_the_keynote_text_line_and_passes_the_provenance_gates():
    """Run 2492ba7a B42: the thermostat line inserted from the mechanical
    set's page facts cited nothing (unsourced_line). It now cites the keynote
    text line (t:<doc8>:<page>:<line>, the tool layer's ref) with no combine
    (a text fact has no quantity for the arithmetic recheck), basis policy;
    the control cable companion cites the same keynote."""
    from app.services import plans_reader_gates as gates

    mech = {"id": "9b4e6ca9-mech", "name": "MECH", "category": "mechanical_drawings", "page_count": 8}
    lines = _mech_lines()
    inputs = se.ScopeInputs([mech], category_outputs={"electrical_drawings": _no_thermostat_category()},
                            doc_facts=lambda _d: _mech_facts(), page_lines=lambda _d, p: lines.get(p))
    incs = {i["rule_id"]: i for i in se.compute_inclusions(inputs, SEED_POLICY, settings=_settings())}
    reloc = incs["IN-THERMOSTAT-RELOC"]
    assert [(t["sheet"], t["text_ref"]) for t in reloc["triggers"]] == [("MD2.01", "t:9b4e6ca9:5:2"), ("M2.01", "t:9b4e6ca9:7:1")]
    ref = {"ref": "t:9b4e6ca9:7:1", "multiplier": 1.0}
    assert se._trigger_sources(reloc) == {"source_refs": [ref]}
    assert se._trigger_sources(incs["IN-CONTROL-CABLE"]) == {"source_refs": [ref]}
    ctx = _ctx(inclusions=list(incs.values()))
    out = se.enforce(_main(), ctx, SEED_POLICY, settings=_settings())
    therm = _rows(out, r"RELOCATION OF THERMOSTAT")[0]
    cable = _rows(out, r"#18/4C CONTROL CABLE")[0]
    assert therm["quantity"] == 5 and therm["source_refs"] == [ref] and therm["basis"] == "policy"
    assert "combine" not in therm and cable["source_refs"] == [ref] and "combine" not in cable
    report = gates.run_final(boq=[dict(therm, line_id="B1"), dict(cable, line_id="B2")], mode="tools", settings=_settings())
    assert not [f.gate_id for f in report.findings if f.gate_id in ("unsourced_line", "unverifiable_citation", "arithmetic")]
    # the line an earlier enforce inserted without refs is sourced on re-enforce (run 2492ba7a's stored main)
    stale = copy.deepcopy(out)
    for it in _rows(stale, r"RELOCATION OF THERMOSTAT"):
        it.pop("source_refs")
    again = se.enforce(stale, ctx, SEED_POLICY, settings=_settings())
    assert _rows(again, r"RELOCATION OF THERMOSTAT")[0]["source_refs"] == [ref]
    assert json.dumps(se.enforce(again, ctx, SEED_POLICY, settings=_settings()), sort_keys=True) == json.dumps(again, sort_keys=True)
    # a page with no text layer gives no ref: the line cites nothing and the gate routes it to a human, as before
    inputs2 = se.ScopeInputs([mech], category_outputs={"electrical_drawings": _no_thermostat_category()},
                             doc_facts=lambda _d: _mech_facts())
    incs2 = {i["rule_id"]: i for i in se.compute_inclusions(inputs2, SEED_POLICY, settings=_settings())}
    assert all(t["text_ref"] is None for t in incs2["IN-THERMOSTAT-RELOC"]["triggers"])
    assert se._trigger_sources(incs2["IN-THERMOSTAT-RELOC"]) == {}


def test_keynote_text_ref_picks_the_numbered_line_and_falls_back_to_a_short_head():
    lines = {3: ["NOTES", "7. PROVIDE NEW", "THERMOSTAT WIRING.", "10. RELOCATE EXISTING THERMOSTAT SENSOR AS INDICATED"]}
    inputs = se.ScopeInputs([{"id": "abcdef0123", "name": "M", "category": "mechanical_drawings"}],
                            page_lines=lambda _d, p: lines.get(p))
    assert se.keynote_text_ref(inputs, "abcdef0123", 3, "10", "RELOCATE EXISTING THERMOSTAT SENSOR AS INDICATED.") == "t:abcdef01:3:3"
    assert se.keynote_text_ref(inputs, "abcdef0123", 3, "7", "PROVIDE NEW THERMOSTAT WIRING.") == "t:abcdef01:3:1"
    assert se.keynote_text_ref(inputs, "abcdef0123", 3, "9", "SOMETHING NOT ON THE PAGE AT ALL") is None
    assert se.keynote_text_ref(inputs, "abcdef0123", 4, "10", "RELOCATE EXISTING THERMOSTAT") is None


def test_rewrite_question_keeps_an_undecided_clause_with_its_own_subject():
    l4 = (r"(?s)\bL4\b.{0,60}\bCONTROL|\bCONTROL.{0,60}\bL4\b",)
    l5 = (r"(?s)\bL5\b.{0,60}\bCONTROL|\bCONTROL.{0,60}\bL5\b",)
    decided = {**se.decided_topic_patterns({"temporary_power"}), "luminaire_control:L4": l4, "luminaire_control:L5": l5}
    q, removed = se.rewrite_question("Confirm L4 control method and L5 module lengths, quantity unit and wattage basis.", decided)
    assert q == "Confirm L5 module lengths, quantity unit and wattage basis." and removed == ["luminaire_control:L4"]
    assert se.rewrite_question(q, decided) == (q, [])  # stable
    assert se.rewrite_question("Confirm temporary office loads and layouts.", decided) == (None, ["temporary_power"])
    # hyphenated system names are the same decided topic (the run 2492ba7a fragment)
    systems = se.decided_topic_patterns({"access_control", "fire_alarm"})
    assert se.rewrite_question("Access-control system furnishing, wiring and programming remain by Honeywell.", systems)[0] is None
    assert se.rewrite_question("Confirm the fire-alarm interface and the card-reader rough-in.", systems)[0] is None


def test_brief_rfis_decided_by_the_policy_and_the_luminaire_schedule_are_resolved_like_questions():
    """The three run 2492ba7a questions the policy had decided (raceway with
    the conflict under review, demolition disposition from a sheet note,
    schedule-decided L4 control) and the Honeywell RFI resolve in the brief
    with the deciding keys and clarification; an undecided RFI is untouched."""
    main = _main()
    main["brief"]["rfis"] = [
        {"source": "Q2", "question": "Confirm whether 'ALL WIRING ABOVE CEILING SHALL BE IN RIGID CONDUIT' requires "
                                     "RGS/IMC throughout or permits EMT in protected interior ceiling spaces."},
        {"source": "Q15", "question": "Does ED3.01 require existing lighting conduit and conductors to remain capped "
                                      "for reuse or to be removed back to source?"},
        {"source": "Q10", "question": "Confirm L4 control method and L5 module lengths, quantity unit and wattage basis."},
        {"source": "Q22", "question": "Identify any electrical raceway, box and power requirements for the CR annotation "
                                      "and Honeywell interface. Access-control system furnishing, wiring and programming "
                                      "remain by Honeywell."},
        {"source": "Q8", "question": "Confirm existing panel manufacturer, breaker series and interrupting rating for "
                                     "new keyed GFCI breakers."},
    ]
    schedule = [{"tag": "L4", "control": "LOCAL", "ref": "S9#r4", "sheet": "E0.04", "description": "TASK LED LIGHTING"},
                {"tag": "L5", "control": "LOCAL", "ref": "S9#r5", "sheet": "E0.04", "description": "UNDERCABINET LED LIGHTING"}]
    ctx = _ctx(luminaire_schedule=schedule)
    out = se.enforce(main, ctx, SEED_POLICY, settings=_settings())
    rep = out["scope_policy"]
    assert [r["question"] for r in out["brief"]["rfis"]] == [
        "Confirm L5 module lengths, quantity unit and wattage basis.",
        "Confirm existing panel manufacturer, breaker series and interrupting rating for new keyed GFCI breakers."]
    removed = {r["source"]: r for r in rep["rfis_removed"]}
    assert removed["Q2"]["topics"] == ["raceway"] and "standard:raceway" in removed["Q2"]["decision_keys"]
    assert "EMT" in removed["Q2"]["clarification"]
    assert "raceway_conflict" in {n["code"] for n in rep["needs_review"]}  # the conflict stays on the chip
    assert removed["Q15"]["topics"] == ["demo_disposition"] and "demo_disposition" in removed["Q15"]["decision_keys"]
    assert "ED3.01 KN1" in removed["Q15"]["clarification"]
    assert {"access_control", "fire_alarm"} <= set(removed["Q22"]["topics"])
    assert any(k.startswith("obligation:") and "access_control" in k for k in removed["Q22"]["decision_keys"])
    assert removed["Q22"]["clarification"]  # the first decided topic's clarification (raceway before the obligation)
    (rewritten,) = rep["rfis_rewritten"]
    assert rewritten["source"] == "Q10" and rewritten["topics"] == ["luminaire_control:L4"]
    assert rewritten["clarification"] == "L4 control LOCAL per the luminaire schedule (E0.04)"
    assert rewritten["decision_keys"] == ["luminaire_schedule:S9#r4"]
    assert rep["counts"]["rfis_removed"] == 3 and rep["counts"]["rfis_rewritten"] == 1
    assert json.dumps(se.enforce(out, ctx, SEED_POLICY, settings=_settings()), sort_keys=True) == json.dumps(out, sort_keys=True)


def test_breaker_lines_the_plan_inserts_cite_the_consolidator_policy_row():
    def _without_breakers(main: dict) -> dict:
        for area in main["boq"]["areas"]:
            for sec in area["sections"]:
                for grp in sec["groups"]:
                    for sub in grp["subgroups"]:
                        sub["items"] = [it for it in sub["items"]
                                        if not brk.is_breaker_row(it["description"], group=grp["name"], subgroup=sub["name"])]
        return main

    main = _without_breakers(_main())
    main["consolidator"] = {"mode": "tools", "policy_rows": [
        {"row_id": "M7", "kind": "M", "description": "20A/1P CIRCUIT BREAKER, GFCI", "quantity": 4.0, "unit": "EA",
         "basis": "policy", "origin": {"group_kind": "derive", "source": "scope_breakers"}}]}
    out = se.enforce(main, _ctx(), SEED_POLICY, settings=_settings())
    rows = _rows(out, r"CIRCUIT BREAKER")
    assert [(r["description"], r["quantity"], r["scope_rule"], r["source_refs"], r["combine"], r["basis"]) for r in rows] == \
        [("20A/1P CIRCUIT BREAKER, GFCI", 4, "BRK-PLAN", [{"ref": "M7", "multiplier": 1.0}], "single", "policy")]
    legacy = se.enforce(_without_breakers(_main()), _ctx(), SEED_POLICY, settings=_settings())
    (row,) = _rows(legacy, r"CIRCUIT BREAKER")
    assert row["scope_rule"] == "BRK-PLAN" and "source_refs" not in row


RUN_2492 = EVAL_ROOT / "bundles" / "7154" / "2492ba7a"
_E20_RE = re.compile(r"maintain(?:ed)?\s+and\s+cap|\bcapp?(?:ed)?\b.{0,80}\bremov|\bremov\w*\b.{0,80}\bto\s+(?:the\s+)?source\b"
                     r"|retained/capped", re.I | re.S)
_RACEWAY_RFI_RE = re.compile(r"\braceway\b|\bEMT\b|\brigid\b", re.I)
_L4_CONTROL_RE = re.compile(r"\bL4\b.{0,60}\bcontrol|\bcontrol.{0,60}\bL4\b", re.I | re.S)


@pytest.mark.skipif(not (RUN_2492 / "outputs" / "main_none_v1.json").is_file() or not BUNDLE.is_dir(),
                    reason="the 2492ba7a bundle or the weekend bundle is not on disk")
def test_run_2492ba7a_engine_lines_are_sourced_and_the_decided_brief_rfis_are_resolved():
    """$0 measurement on integration run 2 of 7154 (final gates: unsourced
    B41 and B42, arithmetic B8; pricing refused). The stored main output and
    policy context, re-enforced with the thermostat triggers rebuilt from the
    mechanical set's page facts and text layer (the same PDF bytes, from the
    weekend bundle): every engine line is sourced, the TR split pair
    reproduces, the provenance gates are clean; the three category questions
    the policy decided, carried into the brief, resolve or lose their decided
    clause while the undecided RFIs stay as written."""
    from app.services import eval_bundle
    from app.services import plans_reader_gates as gates

    run = eval_bundle.load_bundle(RUN_2492)
    week = eval_bundle.load_bundle(BUNDLE)
    main = json.loads((RUN_2492 / "outputs" / "main_none_v1.json").read_text())
    ctx = json.loads((RUN_2492 / "outputs" / "policy_none_v1.json").read_text())
    measure = json.loads((RUN_2492 / "outputs" / "measure_none_v1.json").read_text())
    cats: dict[str, tuple[int, dict]] = {}
    for p in (RUN_2492 / "outputs").glob("category_*_v*.json"):
        o = json.loads(p.read_text())
        v = int(p.stem.rsplit("_v", 1)[1])
        if o["category"] not in cats or v > cats[o["category"]][0]:
            cats[o["category"]] = (v, o)
    outputs = {c: o for c, (_v, o) in cats.items()}
    mech = next(d for d in run.documents if d["category"] == "mechanical_drawings")
    mech_week = next(d for d in week.documents if "MECH" in str(d.get("name")))
    pdf = week.doc_bytes(mech_week["id"])
    inputs = se.ScopeInputs([mech], category_outputs=outputs, pdf_bytes=lambda _d: pdf)
    incs = {i["rule_id"]: i for i in se.compute_inclusions(inputs, SEED_POLICY, settings=_settings())}
    reloc = incs["IN-THERMOSTAT-RELOC"]
    assert reloc["quantity"] == 5 and reloc["trigger_source"] == "page_facts"
    (used,) = [t for t in reloc["triggers"] if t["used"]]
    assert (used["sheet"], used["ref"], used["text_ref"]) == ("M2.01", "KN10", "t:9b4e6ca9:7:166")
    assert inputs.lines(mech["id"], 7)[166].startswith("10. RELOCATE EXISTING THERMOSTAT SENSOR")
    stored_rfis = list(main["brief"]["rfis"])
    assert len(stored_rfis) == 8
    quoted = [
        {"source": "Q2", "question": "Confirm whether 'ALL WIRING ABOVE CEILING SHALL BE IN RIGID CONDUIT' requires "
                                     "RGS/IMC throughout or permits EMT in protected interior ceiling spaces."},
        {"source": "Q15", "question": "Does ED3.01 require existing lighting conduit and conductors to remain capped "
                                      "for reuse or to be removed back to source?"},
        {"source": "Q10", "question": "Confirm L4 control method and L5 module lengths, quantity unit and wattage basis."},
    ]
    assert {q["question"] for q in quoted} <= {q["question"] for q in ctx["questions"]}  # the reader wrote them
    main2 = copy.deepcopy(main)
    main2["brief"]["rfis"] = stored_rfis + quoted
    ctx2 = {**ctx, "inclusions": list(incs.values())}
    out = se.enforce(main2, ctx2, SEED_POLICY, settings=_settings())
    rep = out["scope_policy"]
    lines = {it["line_id"]: it for *_x, it in se.boq_items(out)}
    assert lines["B41"]["description"] == "DUPLEX RECEPTACLE, TR" and lines["B41"]["quantity"] == 3
    assert lines["B41"]["source_refs"] == [{"ref": "C34", "multiplier": 1.0}] and lines["B41"]["combine"] == "split"
    assert lines["B41"]["split_of"] == "B8" and lines["B8"]["split_to"] == [{"line_id": "B41", "quantity": 3}]
    assert lines["B8"]["quantity"] == 28 and lines["B8"]["source_refs"] == [{"ref": "C34", "multiplier": 1.0}]
    assert lines["B42"]["description"] == "RELOCATION OF THERMOSTAT TO NEW LOCATION" and lines["B42"]["quantity"] == 5
    assert lines["B42"]["source_refs"] == [{"ref": "t:9b4e6ca9:7:166", "multiplier": 1.0}] and "combine" not in lines["B42"]
    assert lines["B42"]["basis"] == "policy" and lines["B32"]["source_refs"] == [{"ref": "M6", "multiplier": 1.0}]
    assert not [it for it in lines.values() if it.get("scope_rule") and not it.get("source_refs")]
    report = gates.run_final(boq=out, main=out, category_outputs=list(outputs.values()), measure=measure,
                             dispositions=out.get("dispositions") or [], excluded_lines=out.get("excluded_lines") or [],
                             mode="tools", route="human", settings=_settings())
    provenance = [(f.gate_id, dict(f.target)) for f in report.findings
                  if f.gate_id in ("unsourced_line", "unverifiable_citation", "arithmetic", "double_consumption")]
    assert provenance == []
    # the brief: the three decided questions resolve or lose their decided clause
    after = [r["question"] for r in out["brief"]["rfis"]]
    removed = {r["source"]: r for r in rep["rfis_removed"] if r.get("source")}
    assert removed["Q2"]["topics"] == ["raceway"] and "standard:raceway" in removed["Q2"]["decision_keys"]
    assert "raceway_conflict" in {n["code"] for n in rep["needs_review"]}
    assert removed["Q15"]["topics"] == ["demo_disposition"] and "ED3.01 KN1" in removed["Q15"]["clarification"]
    rewritten = {r["source"]: r for r in rep["rfis_rewritten"] if r.get("source")}
    assert rewritten["Q10"]["after"] == "Confirm L5 module lengths, quantity unit and wattage basis."
    assert rewritten["Q10"]["topics"] == ["luminaire_control:L4"]
    assert "Confirm L5 module lengths, quantity unit and wattage basis." in after
    assert not [q for q in after if _E20_RE.search(q) or _RACEWAY_RFI_RE.search(q) or _L4_CONTROL_RE.search(q)]
    # of the eight stored RFIs the fragment on the excluded access control system goes, the other seven stay as written
    fragment = "Access-control system furnishing and wiring."
    assert [r for r in stored_rfis if r["question"] != fragment] == [r for r in out["brief"]["rfis"] if r["source"] != "Q10"]
    assert removed["Q22"]["question"] == fragment and "access_control" in removed["Q22"]["topics"]
    assert json.dumps(se.enforce(out, ctx2, SEED_POLICY, settings=_settings()), sort_keys=True) == json.dumps(out, sort_keys=True)


def test_device_merge_keeps_every_merged_line_sourced():
    """DEV-MERGE through _merge_line_sources: the surviving receptacle line
    cites the union of its members' rows (combine sum), so the arithmetic
    recheck reproduces the merged quantity and no merged row is an orphan.
    Idempotent, and the decision's members record their own refs."""
    from app.services import plans_reader_gates as gates

    main = {"boq": {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [
        {"name": "WIRING DEVICES", "subgroups": [{"name": "RECEPTACLES", "items": [
            {"line_id": "B35", "description": "DUPLEX RECEPTACLE, GFCI", "quantity": 26, "unit": "EA",
             "source_refs": [{"ref": "C32", "multiplier": 1.0}], "combine": "single", "basis": "counted",
             "sheet_refs": ["E2.01"]},
            {"line_id": "B36", "description": "DUPLEX RECEPTACLE, GFCI, EMERGENCY", "quantity": 9, "unit": "EA",
             "source_refs": [{"ref": "C33", "multiplier": 1.0}], "combine": "single", "basis": "counted",
             "sheet_refs": ["E2.01"]}]}]}]}]}]}}
    cat = {"category": "electrical_drawings", "counts": [
        {"row_id": "C32", "description": "DUPLEX RECEPTACLE, GFCI", "quantity": 26, "unit": "EA", "sheet_number": "E2.01",
         "status": "active"},
        {"row_id": "C33", "description": "DUPLEX RECEPTACLE, GFCI, EMERGENCY", "quantity": 9, "unit": "EA",
         "sheet_number": "E2.01", "status": "active"}]}
    out = se.enforce(main, {}, SEED_POLICY, (), settings=_settings())
    (row,) = _rows(out, r"GFCI")
    assert row["line_id"] == "B35" and row["quantity"] == 35 and row["combine"] == "sum"
    assert row["source_refs"] == [{"ref": "C32", "multiplier": 1.0}, {"ref": "C33", "multiplier": 1.0}]
    assert row["sheet_refs"] == ["E2.01"] and row["basis"] == "counted"
    dec = out["scope_policy"]["decisions"]
    merge = next(d for d in (dec.values() if isinstance(dec, dict) else dec) if d["rule_id"] == "DEV-MERGE")
    assert [(m["line_id"], m["source_refs"]) for m in merge["before"]] == [
        ("B35", [{"ref": "C32", "multiplier": 1.0}]), ("B36", [{"ref": "C33", "multiplier": 1.0}])]
    report = gates.run_final(boq=out, main=out, category_outputs=[cat], mode="tools", route="human", settings=_settings())
    assert [f.gate_id for f in report.findings if f.gate_id in ("arithmetic", "orphan_rows", "unsourced_line")] == []
    again = se.enforce(out, {}, SEED_POLICY, (), settings=_settings())
    assert json.dumps(again, sort_keys=True) == json.dumps(out, sort_keys=True)


RUN_AD69 = EVAL_ROOT / "bundles" / "7154" / "ad69e5a4"


@pytest.mark.skipif(not (RUN_AD69 / "groups" / "main_none_0_p1.json").is_file(),
                    reason="the ad69e5a4 bundle is not on disk")
def test_run_ad69e5a4_device_merge_passes_the_provenance_gates_and_the_l4_question_is_rewritten():
    """$0 measurement on the 7154 re-run from consolidating (final gates on
    the stored main: arithmetic B35 "stored 35 EA, computed 26 EA" and
    orphan C33 after DEV-MERGE folded B36 into B35; pricing refused). The
    stored pre-enforce main group output, re-enforced with the run's stored
    policy context: B35 cites C32 and C33 (combine sum), the provenance gates
    report no arithmetic, orphan_rows or unsourced_line finding, the enforce
    is idempotent, and the reader question Q10 on the L4 control method (a
    category question, not a brief RFI on this run) is rewritten to its L5
    clause from the luminaire schedule (E0.04, L4 control LOCAL)."""
    from app.services import plans_reader_gates as gates

    group = json.loads((RUN_AD69 / "groups" / "main_none_0_p1.json").read_text())
    ctx = json.loads((RUN_AD69 / "outputs" / "policy_none_v1.json").read_text())
    measure = json.loads((RUN_AD69 / "outputs" / "measure_none_v1.json").read_text())
    cats: dict[str, tuple[int, dict]] = {}
    for p in (RUN_AD69 / "outputs").glob("category_*_v*.json"):
        o = json.loads(p.read_text())
        v = int(p.stem.rsplit("_v", 1)[1])
        if o["category"] not in cats or v > cats[o["category"]][0]:
            cats[o["category"]] = (v, o)
    outputs = [o for _v, o in cats.values()]
    assert [(e["tag"], e["control"]) for e in ctx["luminaire_schedule"] if e["tag"] == "L4"] == [("L4", "LOCAL")]
    l4 = "Confirm L4 control method and L5 module lengths, quantity unit and wattage basis."
    assert [q["row_id"] for q in ctx["questions"] if q["question"] == l4] == ["Q10"]
    assert not [r for r in group["brief"]["rfis"] if _L4_CONTROL_RE.search(r["question"])]
    pre = {it["line_id"]: it for *_x, it in se.boq_items(group)}
    assert (pre["B35"]["quantity"], pre["B35"]["source_refs"]) == (26, [{"multiplier": 1.0, "ref": "C32"}])
    assert (pre["B36"]["quantity"], pre["B36"]["source_refs"]) == (9, [{"multiplier": 1.0, "ref": "C33"}])
    out = se.enforce(group, ctx, SEED_POLICY, settings=_settings())
    lines = {it["line_id"]: it for *_x, it in se.boq_items(out)}
    assert "B36" not in lines and len(lines) == 37
    assert lines["B35"]["quantity"] == 35 and lines["B35"]["combine"] == "sum"
    assert lines["B35"]["source_refs"] == [{"ref": "C32", "multiplier": 1.0}, {"ref": "C33", "multiplier": 1.0}]
    report = gates.run_final(boq=out, main=out, category_outputs=outputs, measure=measure,
                             dispositions=out.get("dispositions") or [], excluded_lines=out.get("excluded_lines") or [],
                             mode="tools", route="human", settings=_settings())
    provenance = [(f.gate_id, dict(f.target)) for f in report.findings
                  if f.gate_id in ("arithmetic", "orphan_rows", "unsourced_line", "double_consumption")]
    # The final gates now index rows through the consolidator's alias-aware
    # index, so count rows that an id collision across categories used to hide
    # on this stored run (merged from id 1 per category) are visible and can
    # read as orphans; the line provenance itself must be clean.
    assert [p for p in provenance if p[0] != "orphan_rows"] == []
    rep = out["scope_policy"]
    rewritten = {r["row_id"]: r for r in rep["questions_rewritten"]}
    assert rewritten["Q10"]["after"] == "Confirm L5 module lengths, quantity unit and wattage basis."
    assert rewritten["Q10"]["topics"] == ["luminaire_control:L4"]
    assert rewritten["Q10"]["clarification"] == "L4 control LOCAL per the luminaire schedule (E0.04)"
    assert not [r for r in out["brief"]["rfis"] if _L4_CONTROL_RE.search(r["question"])]
    assert json.dumps(se.enforce(out, ctx, SEED_POLICY, settings=_settings()), sort_keys=True) == json.dumps(out, sort_keys=True)


# 7154 run b87f0ffd: the model carried FIRE/SMOKE DAMPER ELECTRICAL RECONNECTION 6 EA (B18 from
# C40) and no ELECTRICAL CONNECTION line; the reader rows C40 (6) and C41 (1) name both connections.
# B18 is the run's stored line; the inclusion record carries those rows as its triggers in the
# shape compute_inclusions stores (run 2492ba7a's record, the triggers from b87f0ffd's rows).
B18_RUN_B87 = {"basis": "counted", "caveat": None, "combine": "single", "description": "FIRE/SMOKE DAMPER ELECTRICAL RECONNECTION",
               "is_accessory": False, "line_id": "B18", "material_category": "General Material", "material_note": None,
               "needs_recount": False, "quantity": 6.0, "sheet_refs": ["E2.01"], "source_refs": [{"multiplier": 1.0, "ref": "C40"}],
               "unit": "EA", "wastage_applies": False}
EQUIP_INC_RUN_B87 = {
    "also_pattern": r"\bconnect(?:ion|ions)?\b", "basis": "counted", "caveat": None, "clarification": None, "companion_of": None,
    "dedupe_scope": "sheet", "description": "ELECTRICAL CONNECTION", "group": "OTHER ITEMS", "material_category": "General Material",
    "measure_row_id": None,
    "pattern": r"\b(?:vav|fcu|ahu|rtu|exhaust\s+fans?|fans?|pumps?|unit\s+heaters?|(?:fire[\s/]+)?smoke\s+dampers?|dampers?)\b",
    "quantity": 7, "rule_id": "IN-EQUIP-CONNECTION", "section": "NEW WORK", "subgroup": "MISCELLANEOUS",
    "trigger_family": "equipment_connection", "trigger_source": "reader_rows",
    "triggers": [
        {"demolition": False, "description": "Fire/smoke damper electrical reconnection", "quantity": 6.0, "row_id": "C40", "sheet": "E2.01", "used": True},
        {"demolition": False, "description": "VAV electrical reconnection", "quantity": 1.0, "row_id": "C41", "sheet": "E2.01", "used": True},
    ],
    "unit": "EA", "wastage_applies": False,
}


def _equip_main(*items: dict) -> dict:
    return {"boq": {"areas": [{"area": "EVAL 7154", "sections": [
        {"heading": "NEW WORK", "groups": [{"name": "OTHER ITEMS", "subgroups": [{"name": "MISCELLANEOUS", "items": [copy.deepcopy(i) for i in items]}]}]},
    ]}]}}


def _equip_decision(out: dict) -> dict:
    return next(d for d in out["scope_policy"]["decisions"] if d["key"] == "inclusion:IN-EQUIP-CONNECTION")


def test_equipment_connection_inclusion_wins_over_the_reconnection_line_of_run_b87f0ffd():
    out = se.enforce(_equip_main(B18_RUN_B87), {"inclusions": [EQUIP_INC_RUN_B87]}, SEED_POLICY, ())
    rows = _rows(out, r"CONNECT")
    assert len(rows) == 1  # merged into the inclusion line, never a second line beside it
    line = rows[0]
    assert (line["description"], line["quantity"], line["unit"]) == ("ELECTRICAL CONNECTION", 7, "EA")
    assert [r["ref"] for r in line["source_refs"]] == ["C40", "C41"] and line["combine"] == "sum"
    assert line["sheet_refs"] == ["E2.01"] and line["line_id"] == "B18"
    dec = _equip_decision(out)
    assert dec["action"] == "canonicalize" and dec["after"] == 7
    assert dec["before"] == [{"line_id": "B18", "description": "FIRE/SMOKE DAMPER ELECTRICAL RECONNECTION", "quantity": 6.0}]
    ev = dec["evidence"][0]
    assert ev["quantity_from"] == "triggers" and ev["model_quantity"] == 6 and ev["trigger_quantity"] == 7
    assert ev["model_refs"] == ["C40"] and ev["trigger_refs"] == ["C40", "C41"]
    # Idempotent: a second enforce changes nothing.
    again = se.enforce(out, {"inclusions": [EQUIP_INC_RUN_B87]}, SEED_POLICY, ())
    assert [(r["description"], r["quantity"], [x["ref"] for x in r["source_refs"]]) for r in _rows(again, r"CONNECT")] == [("ELECTRICAL CONNECTION", 7, ["C40", "C41"])]


def test_equipment_connection_model_count_stands_when_not_lower_or_citing_other_rows():
    # The model's count is not lower: the inclusion only canonicalizes the description.
    eq = {**B18_RUN_B87, "quantity": 7.0, "source_refs": [{"multiplier": 1.0, "ref": "C40"}, {"multiplier": 1.0, "ref": "C41"}], "combine": "sum"}
    out = se.enforce(_equip_main(eq), {"inclusions": [EQUIP_INC_RUN_B87]}, SEED_POLICY, ())
    line = _rows(out, r"CONNECT")[0]
    assert (line["description"], line["quantity"]) == ("ELECTRICAL CONNECTION", 7)
    assert not _equip_decision(out)["evidence"]
    # The model line cites a row the triggers do not: the trigger sum does not cover it, the model count stands.
    other = {**B18_RUN_B87, "source_refs": [{"multiplier": 1.0, "ref": "C99"}]}
    out = se.enforce(_equip_main(other), {"inclusions": [EQUIP_INC_RUN_B87]}, SEED_POLICY, ())
    line = _rows(out, r"CONNECT")[0]
    assert (line["description"], line["quantity"], [r["ref"] for r in line["source_refs"]]) == ("ELECTRICAL CONNECTION", 6, ["C99"])
    # Triggers without reader rows (a context stored before row ids): the model count stands.
    bare = {**EQUIP_INC_RUN_B87, "triggers": [{**t, "row_id": None} for t in EQUIP_INC_RUN_B87["triggers"]]}
    out = se.enforce(_equip_main(B18_RUN_B87), {"inclusions": [bare]}, SEED_POLICY, ())
    assert [(r["description"], r["quantity"]) for r in _rows(out, r"CONNECT")] == [("ELECTRICAL CONNECTION", 6)]


def test_connection_text_reads_a_reconnection_as_a_connection():
    from app.services import scope_devices as devices

    also = scope_policy.compile_pattern(EQUIP_INC_RUN_B87["also_pattern"])
    for text in ("FIRE/SMOKE DAMPER ELECTRICAL RECONNECTION", "VAV electrical reconnection"):
        assert not also.search(text) and also.search(devices.connection_text(text)), text
    assert devices.connection_text("VAV RE-CONNECTION") == "VAV CONNECTION"
    assert devices.connection_text("PRECONNECTED WHIP") == "PRECONNECTED WHIP"


# 7172 breaker plan wiring (dev run 785ae29e): the reader's panel schedule S rows feed the
# plan when WS1 parsed no panel; new panels give breaker lines and their panel lines.

FIXTURE_7172_PANELS = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7172" / "panel_schedules.json"
PANEL_A_7172 = "PANEL A, 277/480V, 3-PH, 4W, 200A MLO TYPE, 14KAIC, 42 POLES, SURFACE MOUNT, NEMA 1"
PANEL_B_7172 = "PANEL B, 120/208V, 3-PH, 4W, 200A MCB TYPE, 10KAIC, 64 POLES, SURFACE MOUNT, NEMA 1"


def _inputs_7172(extra_schedules: list[dict] | None = None) -> se.ScopeInputs:
    rows = json.loads(FIXTURE_7172_PANELS.read_text())["schedules"] + list(extra_schedules or [])
    docs = [{"id": "d-elec", "name": "Elec", "category": "electrical_drawings", "page_count": 7}]
    return se.ScopeInputs(docs, category_outputs={"electrical_drawings": {"schedules": rows}},
                          doc_facts=lambda _doc_id: None, pdf_bytes=lambda _doc_id: None)


def test_7172_breaker_plan_reads_the_reader_panel_schedules():
    mech_panel = {**json.loads(FIXTURE_7172_PANELS.read_text())["schedules"][0], "name": "Panel MP", "row_id": "S99",
                  "sheet_number": "M2.01", "sheet": {"sheet_number": "M2.01"}}
    plan = se.breaker_plan(_inputs_7172([mech_panel]))
    assert plan.status == "resolved" and plan.panels == ["A", "B"]  # the M-sheet copy is not read
    assert {ln.description: ln.quantity for ln in plan.lines} == {
        "20A/1P CIRCUIT BREAKER": 58, "20A/2P CIRCUIT BREAKER": 1, "20A/3P CIRCUIT BREAKER": 1,
        "30A/2P CIRCUIT BREAKER": 1, "30A/3P CIRCUIT BREAKER": 5, "100A/3P CIRCUIT BREAKER": 1}
    assert [p.description for p in plan.panel_lines] == [PANEL_A_7172, PANEL_B_7172]


def _empty_gear_main() -> dict:
    return {"boq": {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [
        {"name": "SWITCHGEAR", "subgroups": [{"name": "TRANSFORMERS", "items": [
            {"description": "TRANSFORMER T-B", "quantity": 1, "unit": "EA", "line_id": "L1"}]}]}]}]}]},
        "estimate": {"extra_allowances": []}, "brief": {"rfis": []}}


def test_7172_enforce_inserts_breakers_and_both_panel_lines_once():
    plan = se.breaker_plan(_inputs_7172()).to_dict()
    main = _empty_gear_main()
    main["consolidator"] = {"policy_rows": [
        {"row_id": "M40", "description": PANEL_A_7172, "quantity": 1.0, "line_hint": {"kind": "panel"}},
        {"row_id": "M41", "description": "20A/1P CIRCUIT BREAKER", "quantity": 58.0,
         "line_hint": {"kind": "breaker"}}]}
    out = se.enforce(main, {"breaker_plan": plan}, SEED_POLICY, ())
    gear = {(s, it["description"]): it for _h, g, s, it in se.boq_items(out) if g == "SWITCHGEAR"}
    assert {d: it["quantity"] for (s, d), it in gear.items() if s == "CIRCUIT BREAKERS"} == {
        "20A/1P CIRCUIT BREAKER": 58, "20A/2P CIRCUIT BREAKER": 1, "20A/3P CIRCUIT BREAKER": 1,
        "30A/2P CIRCUIT BREAKER": 1, "30A/3P CIRCUIT BREAKER": 5, "100A/3P CIRCUIT BREAKER": 1}
    panels = {d: it for (s, d), it in gear.items() if s == "PANELS"}
    assert set(panels) == {PANEL_A_7172, PANEL_B_7172}
    assert panels[PANEL_A_7172]["source_refs"] == [{"ref": "M40", "multiplier": 1.0}]
    assert panels[PANEL_A_7172]["basis"] == "policy" and "source_refs" not in panels[PANEL_B_7172]
    assert gear[("CIRCUIT BREAKERS", "20A/1P CIRCUIT BREAKER")]["source_refs"] == [{"ref": "M41", "multiplier": 1.0}]
    assert gear[("TRANSFORMERS", "TRANSFORMER T-B")]["quantity"] == 1
    decisions = out["scope_policy"]["decisions"]
    assert {d["key"] for d in decisions} >= {"breakers:panel:A", "breakers:panel:B", "breakers:plan"}
    _assert_conserved(main, out)
    again = se.enforce(out, {"breaker_plan": plan}, SEED_POLICY, ())
    assert json.dumps(again, sort_keys=True, default=str) == json.dumps(out, sort_keys=True, default=str)


def test_7172_existing_panel_row_is_not_duplicated_and_exclude_override_skips_insert():
    plan = se.breaker_plan(_inputs_7172()).to_dict()
    main = _empty_gear_main()
    main["boq"]["areas"][0]["sections"][0]["groups"][0]["subgroups"].append({"name": "PANELS", "items": [
        {"description": "PANELBOARD 'A' 200A MLO 277/480V", "quantity": 1, "unit": "EA", "line_id": "L2"}]})
    out = se.enforce(main, {"breaker_plan": plan}, SEED_POLICY,
                     [{"decision_key": "breakers:panel:B", "action": "exclude", "note": "owner furnished"}])
    panels = [it["description"] for _h, _g, s, it in se.boq_items(out) if s == "PANELS"]
    assert panels == ["PANELBOARD 'A' 200A MLO 277/480V"]
    assert "breakers:panel:B" in {o["key"] for o in out["scope_policy"]["overrides_applied"]}


def test_7172_stale_policy_breaker_row_from_the_junk_panel_is_excluded():
    plan = se.breaker_plan(_inputs_7172()).to_dict()
    main = _empty_gear_main()
    main["boq"]["areas"][0]["sections"][0]["groups"][0]["subgroups"].append({"name": "CIRCUIT BREAKERS", "items": [
        {"description": "CIRCUIT BREAKER", "quantity": 4.0, "unit": "EA", "line_id": "L3",
         "source_refs": [{"ref": "M4", "multiplier": 1.0}], "combine": "single", "basis": "policy"}]})
    main["consolidator"] = {"policy_rows": [
        {"row_id": "M4", "description": "CIRCUIT BREAKER", "quantity": 4.0, "circuits": ["AND-1"] * 4,
         "origin": {"group_kind": "derive", "source": "scope_breakers"}, "line_hint": {"kind": "breaker"}}]}
    out = se.enforce(main, {"breaker_plan": plan}, SEED_POLICY, ())
    assert not [it for *_x, it in se.boq_items(out) if it["description"] == "CIRCUIT BREAKER"]
    stale = [e for e in out["excluded_lines"] if e["decision_key"].startswith("breakers:stale:")]
    assert len(stale) == 1 and stale[0]["quantity"] == 4 and stale[0]["rule_id"] == "BRK-PLAN"
    assert not any(n["code"] == "breaker_unplanned" for n in out["scope_policy"]["needs_review"])
    _assert_conserved(main, out)
    again = se.enforce(out, {"breaker_plan": plan}, SEED_POLICY, ())
    assert json.dumps(again, sort_keys=True, default=str) == json.dumps(out, sort_keys=True, default=str)
