"""plans_reader_review: the post-run review chain on a fake Supabase client
(the chain order rules -> review -> revise -> apply -> re-price -> recheck;
shadow mode writes nothing to the estimate; a human edit marks the verdict
stale; one review per base pricing version; the revise ops reach the BOQ
xlsx with quantities equal to the priced v+1 through the modify path
fake). No model call: the group answers are scripted. Synthetic numbers."""

from __future__ import annotations

import io
from decimal import Decimal
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from app.services import plans_reader_render as render
from app.services import plans_reader_review as chain
from app.services import plans_reader_reviewer as rv
from app.services import plans_reader_rows as rows_mod
from app.services import plans_reader_toolloop as toolloop
from app.services.plans_reader_loop_store import FakeLoopStore, loop_key
from app.services.plans_reader_review_rules import ReviewInputs

from tests.ws7_fake_sb import FakeSB

DOC = "11111111-2222-3333-4444-555555555555"
PAGE_TEXT = "PROVIDE L5 UNDERCABINET LED FIXTURES, CONTINUOUS ROW, LENGTHS AS REQUIRED. DUPLEX RECEPTACLES AS SHOWN."
RUN = {"id": "run-1", "project_id": None, "status": "done", "name": "Test Clinic", "settings_snapshot": {"routes": {}},
       "ledger": {"project": {"name": "Test Clinic"}}}


def _settings(mode="apply"):
    return SimpleNamespace(plans_reader_review_mode=mode, plans_reader_review_budget_usd=4.0,
                           ai_pricing_agent_tools_enabled=False, plans_reader_existing_tie_in_ft=25.0,
                           plans_reader_review_local_rules_fallback=False, openai_plans_model="gpt-6-astra",
                           plans_reader_route_map={}, plans_reader_routes="", plans_reader_reasoning_effort="medium")


def _item(desc, qty, unit="EA", line_id=None):
    return {"line_id": line_id, "description": desc, "quantity": qty, "unit": unit, "sheet_refs": ["E2.01"], "caveat": None,
            "material_note": None, "material_category": "General Material", "wastage_applies": unit == "FT",
            "is_accessory": False}


def _spec():
    return {"areas": [{"area": "BASE BID", "notes": [], "sections": [{"heading": "NEW WORK", "groups": [
        {"name": "OTHER ITEMS", "subgroups": [{"name": "RECEPTACLES (RECEPTACLES INCLUDES 4\"S DEEP COMB BOX AND COVER PLATE)",
                                               "items": [_item("DUPLEX RECEPTACLE", 74, line_id="B1")]}]},
        {"name": "LIGHTING FIXTURES", "subgroups": [{"name": "FIXTURES", "items": [
            _item("L5 UNDERCABINET LED", 7, line_id="B2"), _item("L1 2X4 LED TROFFER", 40, line_id="B3")]}]},
    ]}]}]}


def _artifact(spec, rate=100.0):
    lines = []
    for row in rows_mod.iter_boq_lines(spec):
        lines.append({"line_id": row["line_id"], "description": row["description"], "unit": row["unit"], "qty": row["quantity"],
                      "qty_base": row["quantity"], "unit_hours": 0.5, "unit_material_usd": 10.0, "excluded": False,
                      "priced": True, "kind": "other", "hours_source": "library", "material_source": "library",
                      "quantity_basis": "counted", "section": "NEW WORK", "group": row["group"], "subgroup": row["subgroup"]})
    total = sum(ln["qty"] * (ln["unit_hours"] * rate + ln["unit_material_usd"]) for ln in lines)
    return {"schema": "bdr.priced_estimate/1", "refused": False, "wage_type": "non_prevailing_wage", "labor_rate_usd": rate,
            "lines": lines, "allowances": [], "quotes": [], "sections": [], "exclusions": [], "clarifications": [],
            "totals": {"bid_total_usd": round(total, 2)}}


def _loaded(spec, boq_version=1):
    art = _artifact(spec)
    inputs = ReviewInputs(run_id="run-1", boq=spec, priced=art, gate_findings=[], scope_report=None, local_rules_fallback=False,
                          total_usd=art["totals"]["bid_total_usd"], existing_tie_in_ft=25.0)
    return chain.LoadedInputs(inputs=inputs, boq_version=boq_version, boq_spec=spec, artifact=art,
                              documents=[{"id": DOC, "name": "E-SET.pdf", "category": "electrical_drawings", "pages": 12}],
                              wage_type="non_prevailing_wage", line_values=chain.line_values_from_artifact(art),
                              total_usd=art["totals"]["bid_total_usd"], project_name="Test Clinic")


def _sb(spec):
    sb = FakeSB()
    sb.tables["plans_reader_deliverables"] = [{"id": "d-1", "run_id": "run-1", "kind": "boq", "version": 1, "spec_json": spec,
                                              "instruction": None}]
    sb.tables["plans_reader_pricing"] = [{"id": "pv-1", "run_id": "run-1", "version": 1, "boq_version": 1, "status": "priced",
                                         "instruction": "finish", "stale": False, "context": {"wage_type": "non_prevailing_wage"}}]
    return sb


def _loop_result(parsed, cost="0.4"):
    return toolloop.LoopResult(status="done", parsed=parsed, text="{}", rounds=2, spent_usd=Decimal(cost), tool_calls=1,
                               images=0, stop_reason="answer", resumed=False, model="scripted", provider="openai")


REVIEW_ANSWER = {
    "summary": "L5 is a continuous row in feet.",
    "systems_checked": [{"system": "lighting", "status": "covered", "line_ids": ["B2", "B3"]}],
    "findings": [{
        "ref": "RV-A", "kind": "unit_wrong", "severity": "critical", "title": "L5 undercabinet is measured in feet",
        "detail": "The note says continuous row, lengths as required.", "line_ids": ["B2"],
        "evidence": [{"type": "page_quote", "document_id": DOC, "page": 3,
                      "quote": "L5 undercabinet LED fixtures, continuous row, lengths as required", "ref_id": None},
                     {"type": "tool", "document_id": None, "page": None, "quote": None, "ref_id": "m:meas-9"}],
        "proposed_ops": [{"op": "set_unit", "line_id": "B2", "from_unit": "EA", "to_unit": "FT", "to_qty": 43.5, "basis": "measured"}],
        "qty_delta_hint": None, "confidence": 0.9,
    }],
}


def _page_text(d, p):
    return PAGE_TEXT if (d == DOC and p == 3) else None


def _run_review_group(sb, group):
    store = FakeLoopStore(run_id="run-1")
    store.insert_tool_call(loop_key(group_id=str(group["id"])), 1, {"call_id": "call_1", "tool_name": "measure_polyline", "args": {},
                                                                   "result_data": {"length_ft": 43.5}, "citations": ["m:meas-9"]})
    out = rv.finalize_review_loop(REVIEW_ANSWER, _loop_result(REVIEW_ANSWER), sb=sb, run=RUN, group=group, store=store,
                                  page_text=_page_text)
    sb.table("plans_reader_groups").update({"status": "done", "output": out}).eq("id", group["id"]).execute()
    return out


def _run_revise_group(sb, group, decisions):
    pack = group["loop_state"]["revise"]
    parsed = {"dispositions": [{"finding_id": fid, "decision": dec, "reason": "ok", "evidence": [], "ops": []}
                               for fid, dec in decisions(pack["findings"])]}
    out = rv.finalize_revise_loop(parsed, _loop_result(parsed, "0.2"), sb=sb, run=RUN, group=group, page_text=_page_text)
    sb.table("plans_reader_groups").update({"status": "done", "output": out}).eq("id", group["id"]).execute()
    return out


def _decide(findings):
    for f in findings:
        yield f["finding_id"], ("apply" if f["origin"] == "reviewer" else "decline")


def _groups(sb, kind):
    return [g for g in sb.tables.get("plans_reader_groups", []) if g["kind"] == kind]


def _review(sb):
    return sb.tables["plans_reader_reviews"][0]


def _xlsx_rows(data: bytes) -> dict[str, tuple[float, str]]:
    wb = load_workbook(io.BytesIO(data), data_only=True)
    out = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=1, values_only=True):
            if row and len(row) >= 4 and isinstance(row[1], str) and row[2] is not None and isinstance(row[2], (int, float)):
                out[row[1].strip().upper()] = (float(row[2]), str(row[3] or ""))
    return out


# begin ------------------------------------------------------------------------------------------


def test_begin_runs_the_rules_lane_stores_findings_and_queues_one_review_group():
    spec = _spec()
    sb = _sb(spec)
    queued = []
    review = chain.begin(sb, RUN, pricing_version=1, settings=_settings(), loaded=_loaded(spec),
                         enqueue=lambda _sb, groups, _run: queued.extend(groups))
    assert review["stage"] == "review" and review["mode"] == "apply" and review["base_pricing_version"] == 1
    assert review["result"]["stages"]["rules"]["status"] == "done" and review["result"]["stages"]["review"]["status"] == "queued"
    findings = chain.stored_findings(sb, review["id"])
    assert findings and all(f.finding_id.startswith("RF") for f in findings)
    assert any(f.rule_id == "R1" and f.severity == "critical" for f in findings)  # 74 devices, no branch wire
    group = _groups(sb, "review")[0]
    assert queued == [group] and group["mode"] == "review" and group["route"]["provider"] == "openai"
    pack = group["loop_state"]["review"]
    assert pack["review_id"] == review["id"] and [ln["line_id"] for ln in pack["lines"]] == ["B1", "B2", "B3"]
    assert pack["rule_findings"][0]["finding_id"] == "RF1" and pack["documents"][0]["id"] == DOC
    # one review per base pricing version: a second begin returns the same row, queues nothing
    again = chain.begin(sb, RUN, pricing_version=1, settings=_settings(), loaded=_loaded(spec),
                        enqueue=lambda *_a: queued.append("again"))
    assert again["id"] == review["id"] and "again" not in queued and len(_groups(sb, "review")) == 1


def test_begin_is_inert_when_the_mode_is_off_and_refuses_an_unknown_pricing_version():
    spec = _spec()
    sb = _sb(spec)
    assert chain.begin(sb, RUN, pricing_version=1, settings=_settings("off"), loaded=_loaded(spec)) is None
    assert "plans_reader_reviews" not in sb.tables
    with pytest.raises(chain.ReviewRefused):
        chain.begin(sb, RUN, pricing_version=7, settings=_settings(), loaded=_loaded(spec))


def test_begin_skips_a_boq_without_lines():
    spec = {"areas": []}
    sb = _sb(spec)
    loaded = _loaded(spec)
    review = chain.begin(sb, RUN, pricing_version=1, settings=_settings(), loaded=loaded, enqueue=lambda *_a: None)
    assert review["stage"] == "done" and review["verdict"] == "skipped" and _groups(sb, "review") == []


# The chain ------------------------------------------------------------------------------------


def test_chain_order_apply_mode_revise_ops_reach_the_boq_xlsx_and_the_priced_v2(monkeypatch):
    spec = _spec()
    sb = _sb(spec)
    loaded = _loaded(spec)
    S = _settings("apply")
    queued = []
    enqueue = lambda _sb, groups, _run: queued.extend(g["kind"] for g in groups)  # noqa: E731
    review = chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=loaded, enqueue=enqueue)
    rid = review["id"]
    # advance while the review group is still queued: nothing changes
    assert chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue)["stage"] == "review"
    assert queued == ["review"]
    # the review group answers (scripted) and settles done
    out = _run_review_group(sb, _groups(sb, "review")[0])
    # the model said critical; code recomputes from the $420 impact on a $7,260 estimate: warning
    assert out["findings"][0]["evidence_verified"] and out["findings"][0]["severity"] == "warning"
    assert out["findings"][0]["impact_usd"] == pytest.approx(420.0)
    after_review = _review(sb)
    assert after_review["result"]["stages"]["review"]["status"] == "done" and after_review["cost_usd"] == pytest.approx(0.4)
    reviewer_findings = [f for f in chain.stored_findings(sb, rid) if f.origin == "reviewer"]
    assert len(reviewer_findings) == 1 and reviewer_findings[0].finding_id != "RF1"  # numbered after the rule findings
    # advance -> the revise group is queued with the candidates
    review = chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue)
    assert review["stage"] == "revise" and queued == ["review", "revise"]
    revise_group = _groups(sb, "revise")[0]
    cand = [f["finding_id"] for f in revise_group["loop_state"]["revise"]["findings"]]
    assert reviewer_findings[0].finding_id in cand and "RF1" in cand
    # the builder's one turn (scripted): apply the reviewer's finding, decline the rule finding
    _run_revise_group(sb, revise_group, _decide)
    # the modify path fake: renders the BOQ xlsx exactly as render_deliverable would and stores the deliverable row
    captured = {}

    def modify_path(_sb, run, new_spec, *, version):
        captured["xlsx"] = render.render_boq_xlsx(new_spec)
        row = {"id": f"d-{version}", "run_id": run["id"], "kind": "boq", "version": version, "spec_json": new_spec,
               "instruction": chain.REVISED_INSTRUCTION}
        _sb.table("plans_reader_deliverables").insert(row).execute()
        return row

    priced = {}

    def enqueue_pricing(_sb, run, *, settings=None, reason="manual"):
        priced["reason"] = reason
        return {"id": "job-1"}

    review = chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue, modify_path=modify_path,
                           enqueue_pricing=enqueue_pricing)
    assert review["stage"] == "recheck" and priced["reason"] == chain.REVIEW_REVISION_MARKER
    assert review["result"]["stages"]["apply"] == {"status": "done", "boq_version": 2, "applied": 1, "rejected": 0}
    assert review["result"]["revision"]["applied"][0]["op"]["op"] == "set_unit"
    assert sb.tables["plans_reader_pricing"][0]["stale"] is True  # v1 pricing is stale after the revision
    v2 = next(d for d in sb.tables["plans_reader_deliverables"] if d["version"] == 2)
    assert v2["instruction"] == chain.REVISED_INSTRUCTION
    lines_v2 = {r["line_id"]: r for r in rows_mod.iter_boq_lines(v2["spec_json"])}
    assert lines_v2["B2"]["unit"] == "FT" and lines_v2["B2"]["quantity"] == pytest.approx(43.5)
    dispositions = {f.finding_id: f.disposition for f in chain.stored_findings(sb, rid)}
    assert dispositions[reviewer_findings[0].finding_id] == "applied" and dispositions["RF1"] == "declined"
    # the pricing job prices BOQ v2 (fake) with the marker and calls begin again: the recheck, not a new review
    art_v2 = _artifact(v2["spec_json"])
    sb.tables["plans_reader_pricing"].append({"id": "pv-2", "run_id": "run-1", "version": 2, "boq_version": 2, "status": "priced",
                                              "instruction": chain.REVIEW_REVISION_MARKER, "stale": False,
                                              "context": {"wage_type": "non_prevailing_wage"}})
    loaded_v2 = _loaded(v2["spec_json"], boq_version=2)
    done = chain.begin(sb, RUN, pricing_version=2, settings=S, loaded=loaded_v2, enqueue=lambda *_a: queued.append("extra"))
    assert done["id"] == rid and done["stage"] == "done" and done["revised_pricing_version"] == 2
    assert len(sb.tables["plans_reader_reviews"]) == 1 and "extra" not in queued
    assert done["result"]["stages"]["recheck"]["status"] == "done"
    post = [f for f in chain.stored_findings(sb, rid) if f.post_revision]
    assert post and all(f.finding_id not in dispositions for f in post)  # numbered after everything before
    assert done["verdict"] == "needs_human" and any("RF" in r for r in done["reasons"])  # R1 (no branch wire) declined, so open
    # the revise op reached the xlsx: the L5 row reads 43.5 FT, equal to the priced v2 line
    cells = _xlsx_rows(captured["xlsx"])
    priced_v2 = {ln["description"].upper(): (float(ln["qty"]), ln["unit"]) for ln in art_v2["lines"]}
    assert cells["L5 UNDERCABINET LED"] == (43.5, "FT") == priced_v2["L5 UNDERCABINET LED"]
    assert cells["DUPLEX RECEPTACLE"] == (74.0, "EA") == priced_v2["DUPLEX RECEPTACLE"]
    # the chain's steps ran in order
    assert list(done["result"]["stages"]) == ["rules", "review", "revise", "apply", "recheck"]
    result = chain.result_for(sb, "run-1")
    assert result["verdict"] == "needs_human" and result["revised_pricing_version"] == 2 and result["cost_usd"] == pytest.approx(0.6)
    block = chain.review_brief_block(result, include_money=True)
    assert block.startswith("Independent review: needs_human (pricing v1, revised v2)") and "impact $" in block
    assert "impact $" not in chain.review_brief_block(result, include_money=False)
    redacted = chain.redact(result)
    assert all(f["impact_usd"] is None for f in redacted["findings"]) and redacted["cost_usd"] is None


def test_shadow_mode_computes_the_revision_and_writes_nothing_to_the_estimate():
    spec = _spec()
    sb = _sb(spec)
    loaded = _loaded(spec)
    S = _settings("shadow")
    enqueue = lambda *_a: None  # noqa: E731
    review = chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=loaded, enqueue=enqueue)
    assert review["mode"] == "shadow"
    _run_review_group(sb, _groups(sb, "review")[0])
    chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue)
    _run_revise_group(sb, _groups(sb, "revise")[0], _decide)
    touched = []
    done = chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue,
                         modify_path=lambda *a, **k: touched.append("modify"), enqueue_pricing=lambda *a, **k: touched.append("price"))
    assert touched == [] and done["stage"] == "done"
    assert [d["version"] for d in sb.tables["plans_reader_deliverables"]] == [1]
    assert sb.tables["plans_reader_pricing"][0]["stale"] is False
    rev = done["result"]["revision"]
    assert rev["mode"] == "shadow" and len(rev["applied"]) == 1 and rev["applied"][0]["op"]["op"] == "set_unit"
    assert done["result"]["stages"]["apply"] == {"status": "skipped", "reason": "shadow mode"}
    reviewer = next(f for f in chain.stored_findings(sb, review["id"]) if f.origin == "reviewer")
    assert reviewer.disposition == "open" and "shadow" in reviewer.disposition_reason
    assert done["verdict"] == "needs_human"  # nothing was written, so the critical findings stay open


def test_ops_the_rules_refuse_route_the_finding_to_a_human():
    spec = _spec()
    sb = _sb(spec)
    loaded = _loaded(spec)
    S = _settings("apply")
    enqueue = lambda *_a: None  # noqa: E731
    chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=loaded, enqueue=enqueue)
    group = _groups(sb, "review")[0]
    answer = {**REVIEW_ANSWER, "findings": [{**REVIEW_ANSWER["findings"][0],
                                             "evidence": [REVIEW_ANSWER["findings"][0]["evidence"][0]]}]}  # page quote only
    out = rv.finalize_review_loop(answer, _loop_result(answer), sb=sb, run=RUN, group=group, store=FakeLoopStore(), page_text=_page_text)
    assert out["findings"][0]["evidence_verified"]
    sb.table("plans_reader_groups").update({"status": "done", "output": out}).eq("id", group["id"]).execute()
    chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue)
    _run_revise_group(sb, _groups(sb, "revise")[0], _decide)
    touched = []
    done = chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue, modify_path=lambda *a, **k: touched.append(1),
                         enqueue_pricing=lambda *a, **k: touched.append(2))
    assert touched == [] and done["stage"] == "done" and done["verdict"] == "needs_human"
    rev = done["result"]["revision"]
    assert rev["applied"] == [] and rev["rejected"][0]["code"] == "unit_change_needs_measurement" and rev["rejected"][0]["route"] == "human"
    reviewer = next(f for f in chain.stored_findings(sb, done["id"]) if f.origin == "reviewer")
    assert reviewer.disposition == "rejected_by_rule" and "unit_change_needs_measurement" in reviewer.disposition_reason


def test_a_review_with_no_candidates_ends_clear_or_advisory_without_a_revise_group():
    spec = {"areas": [{"area": "BASE BID", "notes": [], "sections": [{"heading": "NEW WORK", "groups": [
        {"name": "LIGHTING FIXTURES", "subgroups": [{"name": "FIXTURES", "items": [_item("L1 2X4 LED TROFFER", 10, line_id="B1")]}]}]}]}]}
    sb = _sb(spec)
    loaded = _loaded(spec)
    S = _settings("apply")
    review = chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=loaded, enqueue=lambda *_a: None)
    group = _groups(sb, "review")[0]
    answer = {"summary": "Nothing wrong.", "systems_checked": [], "findings": []}
    out = rv.finalize_review_loop(answer, _loop_result(answer), sb=sb, run=RUN, group=group, store=FakeLoopStore(), page_text=_page_text)
    sb.table("plans_reader_groups").update({"status": "done", "output": out}).eq("id", group["id"]).execute()
    done = chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=lambda *_a: None)
    assert done["id"] == review["id"] and done["stage"] == "done" and done["verdict"] in ("clear", "advisory")
    assert _groups(sb, "revise") == [] and done["result"]["stages"]["revise"] == {"status": "skipped", "reason": "no finding to revise"}


def test_a_review_group_stopped_by_a_cap_ends_the_review_partial():
    spec = _spec()
    sb = _sb(spec)
    S = _settings("apply")
    review = chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=_loaded(spec), enqueue=lambda *_a: None)
    group = _groups(sb, "review")[0]
    ended = chain.finish_partial(sb, RUN, group=group, reason="The daily spend cap is reached.")
    assert ended["id"] == review["id"] and ended["stage"] == "partial" and ended["verdict"] == "incomplete"
    assert "spend cap" in ended["error"] and ended["result"]["stages"]["review"]["status"] == "failed"
    assert chain.advance(sb, RUN, settings=S) is None  # nothing open


def test_a_failed_review_group_seen_by_advance_ends_partial():
    spec = _spec()
    sb = _sb(spec)
    S = _settings("apply")
    chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=_loaded(spec), enqueue=lambda *_a: None)
    group = _groups(sb, "review")[0]
    sb.table("plans_reader_groups").update({"status": "failed", "error": "boom"}).eq("id", group["id"]).execute()
    done = chain.advance(sb, RUN, settings=S, loaded=_loaded(spec))
    assert done["stage"] == "partial" and done["verdict"] == "incomplete" and "boom" in done["error"]


def test_advance_recovers_a_done_group_whose_output_was_not_recorded():
    spec = _spec()
    sb = _sb(spec)
    loaded = _loaded(spec)
    S = _settings("apply")
    chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=loaded, enqueue=lambda *_a: None)
    group = _groups(sb, "review")[0]
    out = rv.finalize_review_loop(REVIEW_ANSWER, _loop_result(REVIEW_ANSWER), sb=None, run=RUN, group=group,
                                  store=FakeLoopStore(), page_text=_page_text)  # sb None: nothing recorded
    sb.table("plans_reader_groups").update({"status": "done", "output": out}).eq("id", group["id"]).execute()
    assert _review(sb)["result"]["stages"]["review"]["status"] == "queued"
    review = chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=lambda *_a: None)
    assert review["stage"] == "revise" and review["result"]["stages"]["review"]["status"] == "done"


# Stale and one per base ---------------------------------------------------------------------


def test_a_human_edit_after_the_review_marks_it_stale_but_the_chains_own_revision_does_not():
    spec = _spec()
    sb = _sb(spec)
    S = _settings("apply")
    chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=_loaded(spec), enqueue=lambda *_a: None)
    assert chain.on_boq_changed(sb, "run-1", instruction=chain.REVISED_INSTRUCTION) == 0
    assert _review(sb).get("verdict") is None
    assert chain.on_boq_changed(sb, "run-1", instruction="Change the panel count to 3", user="u1") == 1
    row = _review(sb)
    assert row["verdict"] == "stale" and "human_edit" in row["reasons"] and row["result"]["verdict"] == "stale"
    assert chain.on_boq_changed(sb, "run-1", instruction="another edit") == 0  # already stale


def test_a_new_pricing_version_marks_the_older_review_stale_and_starts_one_review_per_base():
    spec = _spec()
    sb = _sb(spec)
    S = _settings("apply")
    first = chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=_loaded(spec), enqueue=lambda *_a: None)
    sb.tables["plans_reader_pricing"].append({"id": "pv-3", "run_id": "run-1", "version": 3, "boq_version": 2, "status": "priced",
                                              "instruction": "boq_version", "stale": False, "context": {}})
    second = chain.begin(sb, RUN, pricing_version=3, settings=S, loaded=_loaded(spec, boq_version=2), enqueue=lambda *_a: None)
    assert second["id"] != first["id"] and second["base_pricing_version"] == 3
    rows = {r["base_pricing_version"]: r for r in sb.tables["plans_reader_reviews"]}
    assert rows[1]["verdict"] == "stale" and rows[3]["stage"] == "review"
    assert len(rows) == 2 and chain.review_for_version(sb, "run-1", 3)["id"] == second["id"]
    # a revision marker with no review waiting for a recheck starts nothing
    sb.tables["plans_reader_pricing"].append({"id": "pv-4", "run_id": "run-1", "version": 4, "boq_version": 3, "status": "priced",
                                              "instruction": chain.REVIEW_REVISION_MARKER, "stale": False, "context": {}})
    assert chain.begin(sb, RUN, pricing_version=4, settings=S, loaded=_loaded(spec), enqueue=lambda *_a: None) is None
    assert len(sb.tables["plans_reader_reviews"]) == 2


# Verdicts -------------------------------------------------------------------------------------


def test_verdict_rules():
    from app.services.plans_reader_review_rules import ReviewFinding

    crit = ReviewFinding(origin="rule", rule_id="R1", kind="missing_scope", severity="critical", title="x", finding_id="RF1")
    warn = ReviewFinding(origin="band", rule_id="hours_per_device", kind="band_outlier", severity="warning", title="y", finding_id="RF2")
    info = ReviewFinding(origin="rule", rule_id="R14", kind="false_rfi", severity="info", title="z", finding_id="RF3")
    assert chain.verdict_for([crit, warn, info], stage="done") == ("needs_human", ["RF1: x"])
    assert chain.verdict_for([warn, info], stage="done") == ("advisory", ["RF2: y"])
    assert chain.verdict_for([info], stage="done") == ("clear", [])
    assert chain.verdict_for([crit], stage="partial")[0] == "incomplete"
    applied = ReviewFinding(origin="rule", rule_id="R1", kind="missing_scope", severity="critical", title="x", finding_id="RF1",
                            disposition="applied")
    assert chain.verdict_for([applied], stage="done") == ("clear", [])
    declined = ReviewFinding(origin="rule", rule_id="R1", kind="missing_scope", severity="critical", title="x", finding_id="RF1",
                             disposition="declined")
    assert chain.verdict_for([declined], stage="done")[0] == "needs_human"  # a builder's decline never closes a critical finding
    post = ReviewFinding(origin="rule", rule_id="R3", kind="unit_wrong", severity="warning", title="w", finding_id="RF4",
                         post_revision=True)
    # after a revision the rules lane is re-run: the pre-revision copy of a rule finding is superseded by the
    # post-revision findings (RERUN_ORIGINS), so an open pre-revision rule critical no longer counts on its own
    assert chain.verdict_for([crit, post], stage="done", revised=True) == ("advisory", ["RF4: w"])
    assert chain.verdict_for([declined, post], stage="done", revised=True) == ("advisory", ["RF4: w"])
    assert chain.RERUN_ORIGINS == {"rule", "gate", "scope", "band"}


def test_revised_verdict_carries_open_reviewer_findings_the_recheck_does_not_rerun():
    """H1 repro: a declined or rule-refused critical REVIEWER finding is not
    re-run by the recheck, so it stays in the revised verdict's pool."""
    from app.services.plans_reader_review_rules import ReviewFinding

    post_warn = ReviewFinding(origin="rule", rule_id="R3", kind="unit_wrong", severity="warning", title="w", finding_id="RF7",
                              post_revision=True)
    post_info = ReviewFinding(origin="rule", rule_id="R14", kind="false_rfi", severity="info", title="z", finding_id="RF8",
                              post_revision=True)
    declined = ReviewFinding(origin="reviewer", rule_id="missing_scope", kind="missing_scope", severity="critical",
                             title="fire alarm missing", finding_id="RF5", disposition="declined", disposition_by="revise")
    # declined critical reviewer finding + one post-revision warning: needs_human, not advisory
    verdict, reasons = chain.verdict_for([declined, post_warn], stage="done", revised=True)
    assert verdict == "needs_human" and reasons == ["RF5: fire alarm missing"]
    refused = ReviewFinding(origin="reviewer", rule_id="unit_wrong", kind="unit_wrong", severity="critical", title="L5 in feet",
                            finding_id="RF6", disposition="rejected_by_rule", disposition_by="code")
    # rejected_by_rule critical + post-revision info: needs_human, not clear
    assert chain.verdict_for([refused, post_info], stage="done", revised=True) == ("needs_human", ["RF6: L5 in feet"])
    for disp in ("open", "contested"):
        carried = ReviewFinding(origin="reviewer", rule_id="other", kind="other", severity="warning", title="odd",
                                finding_id="RF9", disposition=disp)
        assert chain.verdict_for([carried, post_info], stage="done", revised=True) == ("advisory", ["RF9: odd"])
    # an applied reviewer critical is settled: it does not come back into the pool
    applied = ReviewFinding(origin="reviewer", rule_id="unit_wrong", kind="unit_wrong", severity="critical", title="L5 in feet",
                            finding_id="RF6", disposition="applied", disposition_by="revise")
    assert chain.verdict_for([applied, post_info], stage="done", revised=True) == ("clear", [])
    # nothing post-revision and nothing carried: clear (the rules lane re-ran and found nothing)
    assert chain.verdict_for([applied], stage="done", revised=True) == ("clear", [])
    # the recheck end to end (fake client): the reviewer finding declined by the builder keeps the verdict needs_human
    spec = _spec()
    sb = _sb(spec)
    loaded = _loaded(spec)
    S = _settings("apply")
    enqueue = lambda *_a: None  # noqa: E731
    review = chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=loaded, enqueue=enqueue)
    _run_review_group(sb, _groups(sb, "review")[0])
    chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue)
    # the builder declines the reviewer's finding and applies nothing; a human must look at it
    _run_revise_group(sb, _groups(sb, "revise")[0], lambda fs: ((f["finding_id"], "decline") for f in fs))
    done = chain.advance(sb, RUN, settings=S, loaded=loaded, enqueue=enqueue, modify_path=lambda *a, **k: None,
                         enqueue_pricing=lambda *a, **k: None)
    assert done["stage"] == "done" and done["verdict"] == "needs_human"
    reviewer = next(f for f in chain.stored_findings(sb, review["id"]) if f.origin == "reviewer")
    assert reviewer.disposition == "declined" and reviewer.severity == "warning"  # $420 on $7,260: warning, declined
    assert any(reviewer.finding_id in r for r in done["reasons"]) or any("RF1" in r for r in done["reasons"])


# Redaction (H2) ---------------------------------------------------------------------------------


def _money_keys_left(node, path="result"):
    """Every money value still present after a redaction: band money keys
    of a money metric, impact_usd on a finding, move_share, cost_usd,
    total_usd, anywhere in the document."""
    from app.services import plans_reader_bands as bands_mod

    hits = []
    if isinstance(node, dict):
        money_band = node.get("metric") in bands_mod.MONEY_METRICS
        for k, v in node.items():
            if money_band and k in ("value", "p10", "p25", "p50", "p75", "p90") and v is not None:
                hits.append(f"{path}.{k}")
            if k in ("impact_usd", "move_share", "cost_usd", "total_usd") and v is not None:
                hits.append(f"{path}.{k}")
            hits += _money_keys_left(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits += _money_keys_left(v, f"{path}[{i}]")
    return hits


def test_redact_nulls_money_band_values_under_every_bands_key_and_nested_copies():
    money = {"metric": "bid_per_device", "value": 812.5, "stratum": "building", "n": 29, "p10": 256.0, "p25": 400.0, "p50": 803.0,
             "p75": 1100.0, "p90": 1501.0, "position": "in_band", "percentile": 0.52, "severity": "none", "level": "total",
             "reason": "inside p10 to p90", "band_version": "b1", "line_ids": []}
    plain = {"metric": "hours_per_device", "value": 3.1, "stratum": "building", "n": 29, "p10": 2.33, "p25": 2.8, "p50": 3.46,
             "p75": 4.1, "p90": 6.05, "position": "in_band", "percentile": 0.4, "severity": "none", "level": "component",
             "reason": "inside p10 to p90", "band_version": "b1", "line_ids": []}
    result = {
        "schema": chain.RESULT_SCHEMA, "verdict": "advisory", "reasons": [], "cost_usd": 0.6, "total_usd": 7260.0,
        "findings": [{"finding_id": "RF1", "origin": "rule", "rule_id": "R1", "kind": "missing_scope", "severity": "critical",
                      "title": "x", "detail": "", "line_ids": [], "evidence": [], "proposed_ops": [], "impact_usd": 1234.5,
                      "impact_hours": 3.0, "disposition": "open"}],
        "bands": [dict(money), dict(plain)],
        "bands_after": [dict(money), dict(plain)],
        "stages": {"recheck": {"status": "done", "bands": [dict(money)]}},
        "revision": {"mode": "apply", "applied": [], "rejected": [], "summary": {"applied": 1, "move_share": 0.12}},
        "rules": {"version": "r", "notes": [], "stats": {"lines": 3}},
    }
    redacted = chain.redact(result)
    assert _money_keys_left(redacted) == []
    # the non-money band keeps its values; the money band keeps its shape with nulls; nothing else is lost
    assert redacted["bands"][1]["value"] == 3.1 and redacted["bands_after"][1]["p90"] == 6.05
    assert redacted["bands_after"][0]["value"] is None and redacted["bands_after"][0]["metric"] == "bid_per_device"
    assert redacted["stages"]["recheck"]["bands"][0]["p50"] is None and redacted["stages"]["recheck"]["status"] == "done"
    assert redacted["findings"][0]["impact_usd"] is None and redacted["findings"][0]["impact_hours"] == 3.0
    assert redacted["revision"]["summary"] == {"applied": 1} and redacted["cost_usd"] is None
    assert "total_usd" not in redacted and redacted["verdict"] == "advisory"
    # the input is untouched
    assert result["bands_after"][0]["value"] == 812.5 and result["findings"][0]["impact_usd"] == 1234.5


# advance is cheap and safe without a review (H7); finish_partial without a review row (H8) --------


def test_advance_is_idempotent_cheap_and_never_raises_without_a_pending_review():
    sb = FakeSB()
    S = _settings("apply")
    assert chain.advance(sb, RUN, settings=S) is None
    assert len(sb.calls) == 1 and sb.calls[0][0] == "plans_reader_reviews" and sb.calls[0][1] == "select"
    # mode off: nothing at all, not even the read
    sb_off = FakeSB()
    assert chain.advance(sb_off, RUN, settings=_settings("off")) is None and sb_off.calls == []
    # a client without the reviews table (migration not applied): None, no raise
    broken = FakeSB(fail_tables={"plans_reader_reviews"})
    assert chain.advance(broken, RUN, settings=S) is None
    # a finished review: None, one read, no write, twice in a row
    spec = _spec()
    sb = _sb(spec)
    ended = chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=_loaded(spec), enqueue=lambda *_a: None)
    chain.finish_partial(sb, RUN, group=_groups(sb, "review")[0], reason="cap")
    assert chain._review_rows(sb, "run-1")[0]["stage"] == "partial" and ended["id"]
    before = len(sb.calls)
    assert chain.advance(sb, RUN, settings=S) is None and chain.advance(sb, RUN, settings=S) is None
    new_calls = sb.calls[before:]
    assert len(new_calls) == 2 and all(c[0] == "plans_reader_reviews" and c[1] == "select" for c in new_calls)
    # a stale verdict on a terminal review is not reopened either
    assert all(c[1] == "select" for c in sb.calls[before:])


def test_finish_partial_tolerates_a_missing_review_row():
    sb = FakeSB()
    # the group's pack names a review that does not exist and no review is open: nothing to finish, no raise
    orphan = {"id": "g-9", "run_id": "run-1", "kind": "review", "loop_state": {"review": {"review_id": "nope"}}}
    assert chain.finish_partial(sb, RUN, group=orphan, reason="The daily spend cap is reached.") is None
    # a group without a pack at all
    assert chain.finish_partial(sb, RUN, group={"id": "g-10", "run_id": "run-1", "kind": "revise", "loop_state": {}}, reason="x") is None
    assert chain.finish_partial(sb, RUN, group={"id": "g-11", "kind": "revise"}, reason="x") is None
    assert all(c[1] == "select" for c in sb.calls)  # reads only
    # the pack's review id is gone but another review of the run is open: that one ends partial
    spec = _spec()
    sb = _sb(spec)
    S = _settings("apply")
    review = chain.begin(sb, RUN, pricing_version=1, settings=S, loaded=_loaded(spec), enqueue=lambda *_a: None)
    ended = chain.finish_partial(sb, RUN, group={**orphan, "kind": "revise"}, reason="budget")
    assert ended["id"] == review["id"] and ended["stage"] == "partial" and ended["verdict"] == "incomplete"
    assert ended["result"]["stages"]["revise"]["status"] == "failed" and ended["error"] == "budget"


# The default modify path and the DB loader -----------------------------------------------------


def test_default_modify_path_claims_the_change_slot_and_renders_with_the_review_instruction(monkeypatch):
    from app.services import plans_reader_runs as runs

    sb = FakeSB()
    calls = []

    def claim(_sb, run_id, *, output=None, status="queued"):
        row = {"id": "mg-1", "run_id": run_id, "kind": "modify", "status": status, "output": output}
        _sb.table("plans_reader_groups").insert(row).execute()
        calls.append(("claim", output, status))
        return row

    def render_deliverable(_sb, run, kind, spec, *, version, instruction=None, user=None, documents=None):
        calls.append(("render", kind, version, instruction))
        return {"id": "d-2", "kind": kind, "version": version, "spec_json": spec, "instruction": instruction}

    monkeypatch.setattr(runs, "claim_change_slot", claim)
    monkeypatch.setattr(runs, "render_deliverable", render_deliverable)
    row = chain._default_modify_path(sb, RUN, {"areas": []}, version=2)
    assert row["version"] == 2 and calls[0] == ("claim", {"kind": "boq", "review_revision": True}, "running")
    assert calls[1] == ("render", "boq", 2, chain.REVISED_INSTRUCTION)
    slot = sb.tables["plans_reader_groups"][0]
    assert slot["status"] == "done" and slot["output"]["version"] == 2


def test_default_modify_path_fails_the_slot_group_when_the_render_raises(monkeypatch):
    from app.services import plans_reader_runs as runs

    sb = FakeSB()
    monkeypatch.setattr(runs, "claim_change_slot", lambda _sb, run_id, *, output=None, status="queued":
                        _sb.table("plans_reader_groups").insert({"id": "mg-1", "run_id": run_id, "kind": "modify", "status": status,
                                                                 "output": output}).execute().data[0])

    def boom(*a, **k):
        raise ValueError("renderer refused")

    monkeypatch.setattr(runs, "render_deliverable", boom)
    with pytest.raises(ValueError):
        chain._default_modify_path(sb, RUN, {"areas": []}, version=2)
    assert sb.tables["plans_reader_groups"][0]["status"] == "failed"


def test_load_inputs_tolerates_missing_modules_and_rebuilds_the_artifact_from_the_stored_version():
    sb = FakeSB(fail_tables={"plans_reader_gate_findings", "projects"})
    spec = _spec()
    sb.tables["plans_reader_deliverables"] = [{"id": "d-1", "run_id": "run-1", "kind": "boq", "version": 1, "spec_json": spec}]
    sb.tables["plans_reader_documents"] = [{"id": DOC, "run_id": "run-1", "name": "E-SET.pdf", "category": "electrical_drawings",
                                           "page_count": 12}]
    sb.tables["plans_reader_pricing"] = [{"id": "pv-1", "run_id": "run-1", "version": 1, "boq_version": 1, "status": "blocked",
                                         "context": {"wage_type": "prevailing_wage"}, "categories": {}, "verify_draft": None,
                                         "unpriced": {}, "blockers": [{"code": "linear_in_ea", "detail": "track in EA", "line_ids": []}],
                                         "warnings": []}]
    sb.tables["plans_reader_priced_lines"] = []
    loaded = chain.load_inputs(sb, {**RUN, "project_id": "proj-1"}, sb.tables["plans_reader_pricing"][0], settings=_settings())
    assert loaded.boq_version == 1 and loaded.boq_spec == spec and loaded.documents == [{"id": DOC, "name": "E-SET.pdf",
                                                                                           "category": "electrical_drawings", "pages": 12}]
    assert isinstance(loaded.artifact, dict) and loaded.artifact["lines"] == [] and loaded.wage_type == "prevailing_wage"
    assert loaded.artifact["refused"] is True
    assert loaded.inputs.gate_findings is None and any("gate findings unavailable" in n for n in loaded.notes)
    assert any("project type unavailable" in n for n in loaded.notes) and loaded.project_type is None
    assert loaded.inputs.lines() and loaded.inputs.run_id == "run-1"
    assert chain.band_checks_for(sb, loaded) == []  # no active bands on this client


# K1: redact rebuilds band and R12 findings without any seeded dollar (spec 5.5) ------------------


def test_redact_strips_the_dollars_inside_band_and_r12_findings_and_keeps_the_pricing_payload():
    from tests.test_plans_reader_review_rules import (
        BAND_DOLLARS,
        R12_DOLLARS,
        R12_SHOWN,
        money_key_paths,
        numbers_in,
        seeded_money_band_finding,
        seeded_r12_unit_material_finding,
    )

    r12, band = seeded_r12_unit_material_finding(), seeded_money_band_finding()
    result = {
        "schema": chain.RESULT_SCHEMA, "verdict": "advisory", "reasons": [], "cost_usd": 0.6,
        "base_pricing_version": 1, "revised_pricing_version": None,
        "findings": [r12.to_dict(), band.to_dict()],
        "bands": [band.evidence[0]["facts"]],
        "stages": {"rules": {"status": "done"}},
        "rules": {"version": "r", "notes": [], "stats": {"lines": 1}},
    }
    seeded = R12_DOLLARS | BAND_DOLLARS
    assert (R12_SHOWN | BAND_DOLLARS) <= numbers_in(result)  # positive control
    redacted = chain.redact(result)
    assert _money_keys_left(redacted) == []
    assert not (numbers_in(redacted) & seeded), numbers_in(redacted) & seeded
    # The only money-looking keys left are the nulled impact_usd and cost_usd (the route scrub drops them).
    assert [p for p in money_key_paths(redacted) if not p.endswith(("impact_usd", "cost_usd"))] == []
    assert redacted["cost_usd"] is None and all(f["impact_usd"] is None for f in redacted["findings"])
    f_r12 = next(f for f in redacted["findings"] if f["rule_id"] == "R12")
    assert f_r12["evidence"][0]["facts"] == {"n_projects": 9} and "400" not in f_r12["detail"]
    f_band = next(f for f in redacted["findings"] if f["origin"] == "band")
    assert f_band["evidence"][0]["facts"]["value"] is None and f_band["evidence"][0]["facts"]["n"] == 29
    assert f_band["title"] == band.title and f_band["severity"] == "warning"
    # The pricing viewer's payload is the stored result, untouched by the redaction.
    assert result["findings"][0]["impact_usd"] == pytest.approx(3000.0)
    assert result["findings"][0]["evidence"][0]["facts"]["unit_material_usd"] == 400.0
    assert result["findings"][1]["evidence"][0]["facts"]["value"] == 812.5 and result["bands"][0]["p90"] == 1501.0
    assert result["findings"][0]["detail"] == "unit material 400 vs library [53.3333, 180] over 9 projects"
