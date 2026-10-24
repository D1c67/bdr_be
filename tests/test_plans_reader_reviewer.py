"""plans_reader_reviewer: the review and revise seams the spine resolves,
scripted model answers (no provider call), code validation of evidence
(page quotes by difflib, tool refs against the group's trace, bands, rule
findings, recounts), severity recomputed from code impact, the cap on tool
calls, and the revise dispositions. Synthetic numbers only."""

from __future__ import annotations

from decimal import Decimal

import jsonschema
import pytest

from app.services import plans_reader_ops as ops
from app.services import plans_reader_reviewer as rv
from app.services import plans_reader_toolloop as toolloop
from app.services.plans_reader_loop_store import FakeLoopStore, loop_key
from app.services.plans_reader_review_schemas import REVIEW_SCHEMA, REVISE_SCHEMA
from app.services.plans_reader_schemas import validate_strict
from app.services.plans_reader_tools import REGISTRY, ToolContext, dispatch

DOC = "11111111-2222-3333-4444-555555555555"
PAGE_TEXT = ("ELECTRICAL GENERAL NOTES. ALL WORK SHALL COMPLY WITH NRS 338 PREVAILING WAGE REQUIREMENTS. "
             "PROVIDE (5) L5 UNDERCABINET LED FIXTURES, CONTINUOUS ROW, LENGTHS AS REQUIRED. "
             "THERMOSTAT RELOCATION BY ELECTRICAL CONTRACTOR, CONTROL CABLE #18/4C.")


def _pack(**over):
    pack = {
        "review_id": "rev-1", "run_id": "run-1", "base_pricing_version": 1, "boq_version": 1,
        "project_name": "Test Clinic", "project_type": "ti", "wage_type": "non_prevailing_wage",
        "documents": [{"id": DOC, "name": "E-SET.pdf", "category": "electrical_drawings", "pages": 12}],
        "lines": [
            {"line_id": "B1", "area": "BASE BID", "section": "NEW WORK", "group": "OTHER ITEMS",
             "subgroup": "RECEPTACLES", "description": "DUPLEX RECEPTACLE", "quantity": 74, "unit": "EA",
             "basis": "counted", "sheet_refs": ["E2.01"], "flags": [], "ext_usd": 7400.0},
            {"line_id": "B5", "area": "BASE BID", "section": "NEW WORK", "group": "LIGHTING FIXTURES",
             "subgroup": "FIXTURES", "description": "L5 UNDERCABINET LED", "quantity": 7, "unit": "EA",
             "basis": "counted", "sheet_refs": ["E3.01"], "flags": [], "ext_usd": 4200.0},
            {"line_id": "B6", "area": "BASE BID", "section": "NEW WORK", "group": "LIGHTING FIXTURES",
             "subgroup": "FIXTURES", "description": "L1 2X4 LED TROFFER", "quantity": 40, "unit": "EA",
             "basis": "counted", "sheet_refs": ["E3.01"], "flags": [], "ext_usd": 20000.0},
        ],
        "rule_findings": [
            {"finding_id": "RF1", "origin": "rule", "rule_id": "R1", "kind": "missing_scope", "severity": "critical",
             "title": "New devices without branch wire", "detail": "", "line_ids": [], "evidence": [], "proposed_ops": []},
            {"finding_id": "RF2", "origin": "scope", "rule_id": "scope:fire_alarm", "kind": "excluded_scope_carried",
             "severity": "warning", "title": "Fire alarm carried", "detail": "", "line_ids": ["B6"], "evidence": [],
             "proposed_ops": []},
        ],
        "bands": [
            {"metric": "branch_wire_ft_per_device", "value": 0.0, "p10": 22.3, "p50": 46.0, "p90": 99.1,
             "position": "below_extreme", "severity": "critical", "n": 29},
            {"metric": "hours_per_device", "value": 3.1, "p10": 2.33, "p50": 3.46, "p90": 6.05, "position": "in_band",
             "severity": "none", "n": 29},
        ],
        "recounts": [{"target_id": "rc-1", "row_id": "C22", "outcome": "raise", "builder_qty": 5, "recount_qty": 17}],
        "systems": ["power", "lighting", "fire alarm"],
        "scope_digest": "DIGEST",
        "total_usd": 100_000.0,
        "allowance_names": [],
    }
    pack.update(over)
    return pack


def _ctx(pack=None, **over):
    kw = dict(trace={"t:1111:3:12": "text_search", "m:meas-1": "measure_polyline", "call_7": "zoom_region",
                     "$:L5@lib1": "price_lookup", "c:cen-1": "census", "p:temporary_power": "get_scope_policy"},
              page_text=lambda d, p: PAGE_TEXT if (d == DOC and p == 3) else None)
    kw.update(over)
    return rv.context_from_pack(pack or _pack(), **kw)


def _finding(**over):
    f = {"ref": "RV-A", "kind": "unit_wrong", "severity": "critical", "title": "L5 undercabinet is a continuous row in feet",
         "detail": "The note says continuous row, lengths as required.", "line_ids": ["B5"],
         "evidence": [{"type": "page_quote", "document_id": DOC, "page": 3,
                       "quote": "L5 undercabinet LED fixtures, continuous row, lengths as required", "ref_id": None}],
         "proposed_ops": [{"op": "set_unit", "line_id": "B5", "from_unit": "EA", "to_unit": "FT", "to_qty": 43.5,
                           "basis": "measured"}],
         "qty_delta_hint": None, "confidence": 0.8}
    f.update(over)
    return f


# Schemas, budget, prompts ----------------------------------------------------------------


def test_schemas_are_strict_and_the_tool_call_cap_is_ten():
    validate_strict(REVIEW_SCHEMA)
    validate_strict(REVISE_SCHEMA)
    assert rv.REVIEW_MAX_TOOL_CALLS == 10
    budget = rv.review_budget()
    assert budget.kind == "review" and budget.max_tool_calls == 10
    assert rv.review_budget(route={"budget_usd": 0.5}).max_usd == Decimal("0.5")
    revise = rv.revise_budget()
    assert revise.max_tool_calls == 0 and revise.max_rounds == 1
    assert "10 tool calls" in rv.REVIEW_SYSTEM and "ONE turn and no tools" in rv.REVISE_SYSTEM
    for prompt in (rv.REVIEW_SYSTEM, rv.REVISE_SYSTEM):
        assert "\u2013" not in prompt and "\u2014" not in prompt
    # the kind budgets agree with the reviewer's caps (H6): review 10 tool calls, revise one round and no tools
    from app.services import plans_reader_budgets as budgets

    assert budget.max_tool_calls == min(budgets.budget_for("review").max_tool_calls, rv.REVIEW_MAX_TOOL_CALLS) == 10
    assert revise.kind == "revise" and revise.max_rounds == 1 and revise.max_tool_calls == 0


def test_plans_reader_revise_module_re_exports_the_revise_seam():
    from app.services import plans_reader_revise as revise_mod

    assert revise_mod.REVISE_SYSTEM is rv.REVISE_SYSTEM and revise_mod.REVISE_SCHEMA is REVISE_SCHEMA
    assert revise_mod.build_revise_loop is rv.build_revise_loop and revise_mod.finalize_revise_loop is rv.finalize_revise_loop
    assert revise_mod.validate_dispositions is rv.validate_dispositions and revise_mod.revise_budget is rv.revise_budget
    assert revise_mod.revise_budget().max_tool_calls == 0
    # the spine's seam shape: a system prompt string, a callable builder and a callable finalizer
    assert isinstance(revise_mod.REVISE_SYSTEM, str) and callable(revise_mod.build_revise_loop) and callable(revise_mod.finalize_revise_loop)


# Page quotes -----------------------------------------------------------------------------


def test_page_quote_verification_by_difflib():
    exact = rv.verify_page_quote("Thermostat relocation by electrical contractor", PAGE_TEXT)
    assert exact.verified and exact.ratio == 1.0
    fuzzy = rv.verify_page_quote("THERMOSTAT RELOCATION BY ELECTRICAL CONTRACTER, CONTROL CABLE", PAGE_TEXT)
    assert fuzzy.verified and 0.85 <= fuzzy.ratio < 1.0
    short = rv.verify_page_quote("NRS 338 wage", PAGE_TEXT)
    assert not short.verified and "shorter" in short.reason
    invented = rv.verify_page_quote("Provide forty two emergency generator transfer switches", PAGE_TEXT)
    assert not invented.verified
    no_term = rv.verify_page_quote("Thermostat relocation by electrical contractor", PAGE_TEXT, terms=["generator"])
    assert not no_term.verified and "key term" in no_term.reason
    assert not rv.verify_page_quote("Thermostat relocation by electrical contractor", None).verified
    assert not rv.verify_page_quote("Thermostat relocation by electrical contractor", "").verified


# Evidence validation ---------------------------------------------------------------------


def test_evidence_types_verified_against_pages_trace_bands_rules_and_recounts():
    ctx = _ctx()
    terms = rv.key_terms("L5 UNDERCABINET LED")
    checks = {
        "quote_ok": {"type": "page_quote", "document_id": DOC, "page": 3,
                     "quote": "L5 undercabinet LED fixtures, continuous row, lengths as required"},
        "quote_wrong_page": {"type": "page_quote", "document_id": DOC, "page": 4,
                             "quote": "L5 undercabinet LED fixtures, continuous row, lengths as required"},
        "quote_bad_doc": {"type": "page_quote", "document_id": "nope", "page": 3, "quote": "L5 undercabinet LED fixtures, continuous row"},
        "quote_page_beyond": {"type": "page_quote", "document_id": DOC, "page": 13, "quote": "L5 undercabinet LED fixtures, continuous row"},
        "tool_ok": {"type": "tool", "ref_id": "m:meas-1"},
        "tool_call_id": {"type": "tool", "ref_id": "call_7"},
        "tool_foreign": {"type": "tool", "ref_id": "m:someone-else"},
        "band_out": {"type": "band", "ref_id": "branch_wire_ft_per_device"},
        "band_in": {"type": "band", "ref_id": "hours_per_device"},
        "band_unknown": {"type": "band", "ref_id": "bid_per_device"},
        "rule_ok": {"type": "rule", "ref_id": "RF2"},
        "rule_unknown": {"type": "rule", "ref_id": "RF9"},
        "recount_ok": {"type": "recount", "ref_id": "C22"},
        "recount_unknown": {"type": "recount", "ref_id": "C99"},
        "bad_type": {"type": "vibes"},
    }
    got = {k: rv._evidence_entry(v, ctx, terms) for k, v in checks.items()}
    assert {k for k, e in got.items() if e["verified"]} == {"quote_ok", "tool_ok", "tool_call_id", "band_out", "rule_ok", "recount_ok"}
    assert got["tool_ok"]["tool"] == "measure_polyline" and got["rule_ok"]["policy"] is True
    assert got["recount_ok"]["outcome"] == "raise" and got["band_in"]["reason"].startswith("band inside")
    signals = rv.evidence_signals(got.values())
    assert signals.policy_hit
    assert signals.signals >= {ops.SIGNAL_PAGE_QUOTE, ops.SIGNAL_MEASUREMENT, ops.SIGNAL_BAND, ops.SIGNAL_RULE,
                               ops.SIGNAL_POLICY, ops.SIGNAL_RECOUNT, ops.SIGNAL_RECOUNT_RAISE}
    assert ops.SIGNAL_STATUTE_QUOTE not in signals.signals
    statute = rv.evidence_signals([{"type": "page_quote", "verified": True,
                                    "quote": "all work shall comply with NRS 338 prevailing wage requirements"}])
    assert ops.SIGNAL_STATUTE_QUOTE in statute.signals
    tool_signals = rv.evidence_signals([{"type": "tool", "verified": True, "tool": "price_lookup", "ref_id": "$:L5@lib1"},
                                        {"type": "tool", "verified": True, "tool": "census", "ref_id": "c:cen-1"},
                                        {"type": "tool", "verified": True, "tool": "get_scope_policy", "ref_id": "p:temporary_power"}])
    assert tool_signals.signals == {ops.SIGNAL_QUOTE, ops.SIGNAL_CENSUS_FLOOR, ops.SIGNAL_POLICY} and tool_signals.policy_hit


def test_result_quantities_reads_the_measurement_tool_result_shapes():
    assert rv.result_quantities({"measure_id": "m1", "ref": "m:m1", "length_ft": 43.5, "scale": {"ft_per_in": 8.0}}) == (43.5,)
    linear = {"instances": [{"length_ft": 10.2, "method": "vector_rect"}, {"length_ft": 33.3}], "total_ft": 43.5, "unmatched": [],
              "candidates": 2}
    assert rv.result_quantities(linear) == (10.2, 33.3, 43.5)
    routes = {"routes": [{"circuit": "A-1", "total_ft": 120.0, "segments": [{"length_ft": 80.0}, {"length_ft": 40.0}]}],
              "totals": {}, "caveats": []}
    assert rv.result_quantities(routes) == (120.0, 80.0, 40.0)
    assert rv.result_quantities({"quantity": 17, "qty": "4", "quoted_qty": 0, "length_ft": None, "other": 9}) == (17.0, 4.0)
    assert rv.result_quantities({"length_ft": -3, "total_ft": float("nan")}) == ()
    assert rv.result_quantities(None) == () and rv.result_quantities("text") == ()
    deep = {"a": {"b": {"c": {"d": {"e": {"length_ft": 1.0}}}}}}
    assert rv.result_quantities(deep) == ()  # beyond QUANTITY_SCAN_DEPTH
    # a price lookup carries no quantity of its own (the extended values are dollars and hours)
    price = {"status": "ok", "matched": {"item_key": "L5"}, "extended": {"hours": 3.5, "material_usd": 210.0}, "citation": "$:L5@lib1"}
    assert rv.result_quantities(price) == ()


def test_quantities_from_store_cover_measurement_and_quote_tool_calls_only():
    store = FakeLoopStore(run_id="run-1")
    key = loop_key(group_id="grp-1")
    store.insert_tool_call(key, 1, {"call_id": "call_1", "tool_name": "measure_polyline", "args": {},
                                    "result_data": {"length_ft": 43.5}, "citations": ["m:meas-9"]})
    store.insert_tool_call(key, 1, {"call_id": "call_2", "tool_name": "text_search", "args": {},
                                    "result_data": {"hits": [{"quantity": 99, "text": "(99) RECEPTACLES"}]}, "citations": ["t:1111:3:12"]})
    store.insert_tool_call(key, 2, {"call_id": "call_3", "tool_name": "find_linear_items", "args": {}, "refused": True,
                                    "result_data": {"total_ft": 500.0}, "citations": ["m:refused"]})
    store.insert_tool_call(key, 2, {"call_id": "call_4", "tool_name": "find_linear_items", "args": {},
                                    "result_data": {"instances": [{"length_ft": 20.0}], "total_ft": 20.0}, "citations": ["m:lin-1"]})
    store.insert_tool_call(key, 3, {"call_id": "call_5", "tool_name": "price_lookup", "args": {"quantity": 7},
                                    "result_data": {"status": "ok", "extended": {"hours": 3.5}}, "citations": ["$:L5@lib1"]})
    got = rv.quantities_from_store(store, "grp-1")
    assert got == {"call_1": (43.5,), "m:meas-9": (43.5,), "call_4": (20.0,), "m:lin-1": (20.0,)}
    trace = rv.trace_from_store(store, "grp-1")
    assert "t:1111:3:12" in trace and "m:refused" not in trace and "$:L5@lib1" in trace
    # the evidence entry of a verified measurement carries the quantities; the signals collect them
    ctx = rv.context_from_pack(_pack(), trace=trace, page_text=lambda d, p: None, trace_quantities=got)
    entry = rv._evidence_entry({"type": "tool", "ref_id": "m:meas-9"}, ctx, [])
    assert entry["verified"] and entry["quantities"] == [43.5]
    quote = rv._evidence_entry({"type": "tool", "ref_id": "$:L5@lib1"}, ctx, [])
    assert quote["verified"] and quote["quantities"] == []
    text = rv._evidence_entry({"type": "tool", "ref_id": "t:1111:3:12"}, ctx, [])
    assert text["verified"] and text["quantities"] == []
    fe = rv.evidence_signals([entry, quote, text])
    assert fe.signals == {ops.SIGNAL_MEASUREMENT, ops.SIGNAL_QUOTE, ops.SIGNAL_PAGE_QUOTE} and fe.quantities == (43.5,)
    assert fe.quantity_matches(43.5) and not fe.quantity_matches(60.0)
    # a quantity on an unverified entry never counts
    assert rv.evidence_signals([{**entry, "verified": False}]).quantities == ()
    # finalize_review_loop reads the quantities from the store and persists them on the finding's evidence
    group = _group(pack=_pack())
    answer = {"summary": "", "systems_checked": [],
              "findings": [_finding(evidence=[{"type": "tool", "document_id": None, "page": None, "quote": None, "ref_id": "m:meas-9"}])]}
    result = toolloop.LoopResult(status="done", parsed=answer, text="{}", rounds=1, spent_usd=Decimal("0"), tool_calls=1,
                                 images=0, stop_reason="answer", resumed=False)
    out = rv.finalize_review_loop(answer, result, sb=None, run={"id": "run-1"}, group=group, store=store, page_text=lambda d, p: None)
    assert out["findings"][0]["evidence"][0]["quantities"] == [43.5]
    assert rv.evidence_signals(out["findings"][0]["evidence"]).quantities == (43.5,)


def test_unknown_line_ids_dropped_and_unverified_findings_capped_at_info_with_ops_dropped():
    ctx = _ctx()
    parsed = {"summary": "s", "systems_checked": [{"system": "power", "status": "covered", "line_ids": ["B1", "B77"]}],
              "findings": [
                  _finding(),
                  _finding(ref="RV-B", line_ids=["B1", "B42"], severity="critical", title="Receptacles undercounted",
                           evidence=[{"type": "page_quote", "document_id": DOC, "page": 3,
                                      "quote": "Provide forty two duplex receptacles on the north wall", "ref_id": None}],
                           proposed_ops=[{"op": "set_quantity", "line_id": "B1", "from_qty": 74, "to_qty": 90, "basis": "counted", "note": None}]),
                  _finding(ref="RV-C", line_ids=["B6"], severity="warning", kind="double_count", title="Troffer double count",
                           evidence=[{"type": "tool", "document_id": None, "page": None, "quote": None, "ref_id": "m:someone-else"}],
                           proposed_ops=[{"op": "set_quantity", "line_id": "B6", "from_qty": 40, "to_qty": 38, "basis": "counted", "note": None}]),
              ]}
    findings, stats = rv.validate_review(parsed, ctx)
    assert stats["schema_ok"] and stats["findings_in"] == 3 and stats["dropped_line_ids"] == 1
    by_ref = {f.title: f for f in findings}
    a = by_ref["L5 undercabinet is a continuous row in feet"]
    assert a.evidence_verified and a.proposed_ops and a.line_ids == ["B5"] and a.origin == "reviewer"
    b = by_ref["Receptacles undercounted"]
    assert b.line_ids == ["B1"] and not b.evidence_verified and b.severity == "info" and b.proposed_ops == []
    assert b.disposition == "info_only" and "no verified evidence" in b.detail
    c = by_ref["Troffer double count"]
    assert not c.evidence_verified and c.severity == "info" and c.proposed_ops == []
    assert stats["unverified"] == 2 and stats["dropped_ops"] == 2


def test_ops_are_validated_against_the_op_schema_and_known_lines():
    assert rv.validate_op({"op": "set_quantity", "line_id": "B1", "from_qty": 1, "to_qty": 2, "basis": "counted", "note": None}, ["B1"])
    assert rv.validate_op({"op": "set_quantity", "line_id": "B9", "from_qty": 1, "to_qty": 2, "basis": "counted", "note": None}, ["B1"]) is None
    assert rv.validate_op({"op": "set_quantity", "line_id": "B1", "from_qty": 1}, ["B1"]) is None  # missing fields
    assert rv.validate_op({"op": "set_price", "line_id": "B1"}, ["B1"]) is None
    assert rv.validate_op("not an op", ["B1"]) is None


# Severity recomputation ------------------------------------------------------------------


def test_severity_recomputed_from_code_impact():
    total = 100_000.0  # threshold max($1,000, 1%) = $1,000; warning at $250
    assert rv.severity_for(None, total, "critical", verified=False) == "info"
    assert rv.severity_for(None, total, "critical", verified=True) == "critical"  # unknown impact never demotes
    assert rv.severity_for(None, total, "warning", verified=True) == "warning"
    assert rv.severity_for(5_000.0, total, "info", verified=True) == "critical"
    assert rv.severity_for(999.0, total, "critical", verified=True) == "warning"
    assert rv.severity_for(100.0, total, "critical", verified=True) == "info"
    assert rv.severity_for(1_500.0, 1_000_000.0, "critical", verified=True) == "info"  # 1% of 1M = 10k; warn at 2.5k


def test_impact_from_ops_and_from_the_delta_hint():
    values = {"B1": 7_400.0, "B5": 4_200.0}
    quantities = {"B1": 74.0, "B5": 7.0}
    qty_op = [{"op": "set_quantity", "line_id": "B1", "from_qty": 74, "to_qty": 84, "basis": "counted", "note": None}]
    assert rv.impact_for(["B1"], qty_op, None, line_values=values, line_quantities=quantities) == pytest.approx(1_000.0)
    remove = [{"op": "remove_row", "line_id": "B5", "from_qty": 7, "reason_code": "duplicate"}]
    assert rv.impact_for(["B5"], remove, None, line_values=values, line_quantities=quantities) == pytest.approx(4_200.0)
    unit = [{"op": "set_unit", "line_id": "B5", "from_unit": "EA", "to_unit": "FT", "to_qty": 43.5, "basis": "measured"}]
    assert rv.impact_for(["B5"], unit, None, line_values=values, line_quantities=quantities) == pytest.approx(4_200.0)
    assert rv.impact_for(["B1"], [], -4, line_values=values, line_quantities=quantities) == pytest.approx(400.0)
    assert rv.impact_for(["B1", "B5"], [], -4, line_values=values, line_quantities=quantities) is None
    assert rv.impact_for(["B1"], qty_op, None, line_values=None, line_quantities=quantities) is None


def test_validate_review_recomputes_severity_from_impact():
    ctx = _ctx()
    big = _finding(ref="RV-A", severity="info", title="L5 undercabinet is a continuous row in feet")  # set_unit on a $4,200 line
    small = _finding(ref="RV-B", severity="critical", line_ids=["B1"], kind="quantity_suspect", title="Two receptacles missed",
                     evidence=[{"type": "tool", "document_id": None, "page": None, "quote": None, "ref_id": "c:cen-1"}],
                     proposed_ops=[{"op": "set_quantity", "line_id": "B1", "from_qty": 74, "to_qty": 76, "basis": "counted", "note": None}])
    findings, _stats = rv.validate_review({"summary": "", "systems_checked": [], "findings": [big, small]}, ctx)
    sev = {f.title: (f.severity, f.impact_usd) for f in findings}
    assert sev["L5 undercabinet is a continuous row in feet"] == ("critical", 4200.0)  # the model said info
    assert sev["Two receptacles missed"] == ("info", 200.0)  # the model said critical; $200 is below a quarter of $1,000


def test_recorded_findings_from_the_tool_join_the_answer():
    ctx = _ctx(recorded=[{"finding_id": "RV1", "severity_claimed": "warning", "severity": "warning", "line_ids": ["B6", "B99"],
                          "claim": "Troffers on E3.01 look double counted with the enlarged plan.",
                          "evidence": ["t:1111:3:12"], "suggested_fix": "Check the enlarged plan", "call_id": "call_1"},
                         {"finding_id": "RV2", "severity_claimed": "critical", "severity": "info", "line_ids": ["B1"],
                          "claim": "An unsupported claim about receptacles.", "evidence": ["t:not-in-trace"],
                          "suggested_fix": None, "call_id": "call_2"}])
    findings, stats = rv.validate_review({"summary": "", "systems_checked": [], "findings": []}, ctx)
    assert stats["recorded"] == 2 and len(findings) == 2
    rv1 = next(f for f in findings if f.title.startswith("RV1"))
    assert rv1.rule_id == rv.RECORDED_RULE_ID and rv1.line_ids == ["B6"] and rv1.evidence_verified and rv1.severity == "warning"
    rv2 = next(f for f in findings if f.title.startswith("RV2"))
    assert not rv2.evidence_verified and rv2.severity == "info"


def test_a_non_object_answer_is_an_incomplete_review():
    findings, stats = rv.validate_review({}, _ctx())
    assert findings == [] and stats["schema_ok"] is False


# The seams (scripted loop) ---------------------------------------------------------------


class _Settings:
    ai_pricing_agent_tools_enabled = False
    plans_reader_review_budget_usd = 4.0


def _group(kind="review", pack=None):
    key = "review" if kind == "review" else "revise"
    return {"id": "grp-1", "run_id": "run-1", "kind": kind, "loop_state": {key: pack or _pack()}, "status": "running"}


def test_build_review_loop_returns_the_pack_text_and_registered_precalls():
    from app.services import scope_tools  # noqa: F401 - registers get_scope_policy and get_facts (app.main does the same)

    parts, precalls = rv.build_review_loop(None, {"id": "run-1"}, _group(), _Settings())
    assert len(parts) == 1
    text = parts[0].text
    assert "B5 | BASE BID | NEW WORK | LIGHTING FIXTURES | FIXTURES | L5 UNDERCABINET LED | 7 | EA" in text
    assert "ext_usd" not in text and "7400" not in text  # pricing hidden unless the agent pricing tools are on
    assert "RF1 | critical | rule:R1" in text and "branch_wire_ft_per_device | 0.0 | 22.3" in text
    assert "rc-1 | C22 | raise | 5 | 17" in text and "SCOPE DIGEST" in text
    money = rv.build_review_loop(None, {"id": "run-1"}, _group(), type("S", (), {"ai_pricing_agent_tools_enabled": True})())[0][0].text
    assert "| 7400" in money and "impact_usd" not in money
    for pre in precalls:
        spec = REGISTRY.get(pre.name)
        assert spec is not None and "review" in spec.kinds and spec.mode == "read", pre.name
        jsonschema.Draft202012Validator(spec.args_schema).validate(pre.args)
    assert [p.name for p in precalls][:2] == ["get_scope_policy", "get_facts"]
    with pytest.raises(rv.ReviewerUnavailable):
        rv.build_review_loop(None, {"id": "run-1"}, {"id": "g", "kind": "review", "loop_state": {}}, _Settings())


def test_scripted_review_loop_through_the_spine_shape(monkeypatch):
    """The spine builds a LoopContext from the seam's parts and runs
    toolloop.run_loop; here the loop is scripted and the finalizer reads
    the trace from the loop store (a stored zoom result makes m: refs
    citable, a foreign ref is not)."""
    pack = _pack()
    group = _group(pack=pack)
    parts, precalls = rv.build_review_loop(None, {"id": "run-1"}, group, _Settings())
    store = FakeLoopStore(run_id="run-1")
    key = loop_key(group_id="grp-1")
    store.insert_tool_call(key, 1, {"call_id": "call_1", "tool_name": "measure_polyline", "args": {},
                                    "result_data": {"length_ft": 43.5}, "citations": ["m:meas-9"]})
    answer = {"summary": "One unit error on L5.", "systems_checked": [{"system": "lighting", "status": "covered", "line_ids": ["B5", "B6"]}],
              "findings": [_finding(evidence=[{"type": "tool", "document_id": None, "page": None, "quote": None, "ref_id": "m:meas-9"},
                                              {"type": "tool", "document_id": None, "page": None, "quote": None, "ref_id": "m:other-group"}])]}
    seen = {}

    def scripted(ctx):
        seen["ctx"] = ctx
        ctx.tool_ctx.call_counts["measure_polyline"] = 1
        return toolloop.LoopResult(status="done", parsed=answer, text="{}", rounds=2, spent_usd=Decimal("0.4"),
                                   tool_calls=1, images=0, stop_reason="answer", resumed=False, model="scripted", provider="openai")

    monkeypatch.setattr(toolloop, "run_loop", scripted)
    tool_ctx = ToolContext(run_id="run-1", kind="review", group_id="grp-1")
    ctx = toolloop.LoopContext(run={"id": "run-1"}, kind="review", feature=rv.REVIEW_FEATURE, system=rv.REVIEW_SYSTEM,
                               user_parts=parts, tools=[], tool_ctx=tool_ctx, store=store, response_schema=REVIEW_SCHEMA,
                               schema_name="review", step=rv.REVIEW_STEP, group_id="grp-1", precalls=precalls,
                               budget=rv.review_budget(_Settings()))
    result = toolloop.run_loop(ctx)
    assert seen["ctx"].budget.max_tool_calls == 10
    output = rv.finalize_review_loop(result.parsed, result, sb=None, run={"id": "run-1"}, group=group, store=store,
                                     page_text=lambda d, p: None)
    assert output["review_id"] == "rev-1" and output["cost_usd"] == pytest.approx(0.4) and output["model"] == "scripted"
    assert output["tool_calls"] == 1 and output["incomplete"] is False
    f = output["findings"][0]
    verified = [e["verified"] for e in f["evidence"]]
    assert verified == [True, False] and f["evidence_verified"] and f["severity"] == "critical"
    assert f["proposed_ops"][0]["op"] == "set_unit"
    assert output["systems_checked"] == [{"system": "lighting", "status": "covered", "line_ids": ["B5", "B6"]}]


def test_finalize_review_with_a_non_object_answer_is_incomplete():
    result = toolloop.LoopResult(status="done", parsed=None, text="", rounds=1, spent_usd=Decimal("0"), tool_calls=0,
                                 images=0, stop_reason="cut", resumed=False)
    out = rv.finalize_review_loop(None, result, sb=None, run={"id": "run-1"}, group=_group(), store=FakeLoopStore(),
                                  page_text=lambda d, p: None)
    assert out["incomplete"] is True and out["findings"] == []


def test_record_finding_tool_feeds_the_finalizer_through_pop_recorded():
    from app.services import plans_reader_review_tools as rtools

    ctx = ToolContext(run_id="run-1", kind="review", group_id="grp-1", ledger_refs={"t:1111:3:12"}, call_id="call_9")
    res = dispatch(rtools.SPEC, ctx, {"severity": "warning", "line_ids": ["B6"], "claim": "Troffers counted twice on the enlarged plan.",
                                      "evidence": ["t:1111:3:12", "t:bogus"], "suggested_fix": None})
    assert not res.refused and res.data["verified_evidence"] == ["t:1111:3:12"]
    result = toolloop.LoopResult(status="done", parsed={"summary": "", "systems_checked": [], "findings": []}, text="{}",
                                 rounds=1, spent_usd=Decimal("0"), tool_calls=1, images=0, stop_reason="answer", resumed=False)
    out = rv.finalize_review_loop(result.parsed, result, sb=None, run={"id": "run-1"}, group=_group(), store=None,
                                  page_text=lambda d, p: None)
    assert len(out["findings"]) == 1 and out["findings"][0]["rule_id"] == rv.RECORDED_RULE_ID
    # the in-process bucket was drained: a second finalize finds nothing
    assert rtools.recorded("run-1", "grp-1") == []


# Revise ----------------------------------------------------------------------------------


def _revise_pack():
    pack = _pack()
    pack["findings"] = [
        {"finding_id": "RF3", "origin": "reviewer", "rule_id": "unit_wrong", "kind": "unit_wrong", "severity": "critical",
         "title": "L5 continuous row", "detail": "", "line_ids": ["B5"], "evidence_verified": True,
         "evidence": [{"type": "tool", "verified": True, "tool": "measure_polyline", "ref_id": "m:meas-9"}],
         "proposed_ops": [{"op": "set_unit", "line_id": "B5", "from_unit": "EA", "to_unit": "FT", "to_qty": 43.5, "basis": "measured"}],
         "disposition": "open"},
        {"finding_id": "RF1", "origin": "rule", "rule_id": "R1", "kind": "missing_scope", "severity": "critical",
         "title": "No branch wire", "detail": "", "line_ids": [], "evidence": [], "proposed_ops": [], "disposition": "open"},
    ]
    return pack


def test_build_revise_loop_lists_findings_with_verified_evidence_and_no_precalls():
    parts, precalls = rv.build_revise_loop(None, {"id": "run-1"}, _group("revise", _revise_pack()), _Settings())
    assert precalls == [] and len(parts) == 1
    text = parts[0].text
    assert "RF3 | critical | reviewer:unit_wrong" in text and "RF3 verified evidence" in text and "proposed_ops" in text
    assert "REVISION TURN" in text


def test_validate_dispositions_apply_modify_decline_and_unknowns():
    pack = _revise_pack()
    findings = {f["finding_id"]: f for f in pack["findings"]}
    parsed = {"dispositions": [
        {"finding_id": "RF3", "decision": "apply", "reason": "measured", "evidence": [], "ops": []},
        {"finding_id": "RF1", "decision": "modify", "reason": "add the wire", "evidence": [],
         "ops": [{"op": "add_row", "area": "BASE BID", "section": "NEW WORK", "group": "WIRING", "subgroup": "CONDUCTORS (AWG)",
                  "description": "#12 THHN", "qty": 6718, "unit": "FT", "basis": "measured", "sheet_refs": ["E2.01"], "item_key": None},
                 {"op": "set_quantity", "line_id": "B77", "from_qty": 1, "to_qty": 2, "basis": "counted", "note": None}]},
        {"finding_id": "RF3", "decision": "decline", "reason": "dup", "evidence": [], "ops": []},
        {"finding_id": "RF9", "decision": "apply", "reason": "", "evidence": [], "ops": []},
        {"finding_id": "RF1", "decision": "whatever", "reason": "", "evidence": [], "ops": []},
    ]}
    out, stats = rv.validate_dispositions(parsed, findings, ["B1", "B5", "B6"])
    assert stats["schema_ok"] is False  # "whatever" is outside the enum; the rest still validates
    assert [d["finding_id"] for d in out] == ["RF3", "RF1"]
    assert out[0]["decision"] == "apply" and out[0]["ops"] == findings["RF3"]["proposed_ops"]  # apply with empty ops takes the finding's
    assert out[1]["decision"] == "modify" and [o["op"] for o in out[1]["ops"]] == ["add_row"]
    assert stats["unknown_findings"] == 3 and stats["dropped_ops"] == 1
    declined, _ = rv.validate_dispositions({"dispositions": [{"finding_id": "RF3", "decision": "decline", "reason": "x", "evidence": [],
                                                              "ops": findings["RF3"]["proposed_ops"]}]}, findings, ["B5"])
    assert declined[0]["ops"] == []


def test_finalize_revise_loop_verifies_the_cited_page_quotes():
    pack = _revise_pack()
    group = _group("revise", pack)
    parsed = {"dispositions": [{"finding_id": "RF3", "decision": "decline", "reason": "The note is about L4, not L5.",
                                "evidence": [{"type": "page_quote", "document_id": DOC, "page": 3,
                                              "quote": "Provide (5) L5 undercabinet LED fixtures, continuous row", "ref_id": None},
                                             {"type": "page_quote", "document_id": DOC, "page": 3,
                                              "quote": "the L4 fixtures are owner furnished and installed", "ref_id": None}],
                                "ops": []}]}
    result = toolloop.LoopResult(status="done", parsed=parsed, text="{}", rounds=1, spent_usd=Decimal("0.2"), tool_calls=0,
                                 images=0, stop_reason="answer", resumed=False, model="scripted", provider="openai")
    out = rv.finalize_revise_loop(parsed, result, sb=None, run={"id": "run-1"}, group=group,
                                  page_text=lambda d, p: PAGE_TEXT if p == 3 else None)
    d = out["dispositions"][0]
    assert d["decision"] == "decline" and [e["verified"] for e in d["evidence"]] == [True, False]
    assert out["cost_usd"] == pytest.approx(0.2) and out["model"] == "scripted"
