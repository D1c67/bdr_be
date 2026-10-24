"""app/services/plans_reader_acceptance.py: every 3.5 row's detector passes on
the synthetic good artifacts and fails on the synthetic bad ones (one
mutation per check), registration against the contract registry, the
text-layer anchors for E17 (synthetic anchors), and, golden, the self-test
on the weekend bundle asserting the Baseline column."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.services import eval_text_anchors as T
from app.services import plans_reader_acceptance as A
from app.services.estimating_contracts import CHECK_SPECS, CHECKS, FIXED_CHECK_IDS, CheckResult, ContractError, register_check

from tests import eval_fixtures as F

WEEKEND = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/ff8fad25")
EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
golden = pytest.mark.skipif(
    not (WEEKEND / "run.json").exists() or not (EVAL_ROOT / "references" / "7154" / "expected.json").exists()
    or not (BACKUP / "data" / "project_files.json").exists(),
    reason="the weekend bundle, the 7154 reference or the production backup is not on disk",
)
SENT_PROPOSAL = "Exclusions: temporary power and lighting, fire alarm by others, commissioning. Painting by GC."


# Registry ---------------------------------------------------------------------------------


def test_every_fixed_check_is_registered_with_the_table_flags():
    for cid in FIXED_CHECK_IDS:
        check = CHECKS[cid]
        spec = CHECK_SPECS[cid]
        assert check.fn is A.DETECTORS[cid]
        assert check.gates_7154_smoke == spec.gates_7154_smoke and check.replayable == spec.replayable
        assert check.owner == "WS0" and check.title
    assert not {cid for cid in FIXED_CHECK_IDS if cid not in CHECKS}
    assert CHECKS["GC2"].gates_7154_smoke is False and CHECKS["E3"].replayable is False
    with pytest.raises(ContractError):
        register_check("E2", A.check_e2, gates_7154_smoke=False, replayable=True)


def test_run_checks_order_and_error_capture(monkeypatch):
    inp = F.build_inputs(F.good_artifacts())
    results = A.run_checks(inp)
    assert [r.id for r in results] == list(FIXED_CHECK_IDS)
    monkeypatch.setitem(CHECKS, "E2", type(CHECKS["E2"])(id="E2", fn=lambda i: 1 / 0, gates_7154_smoke=True, replayable=True,
                                                         threshold=CHECKS["E2"].threshold))
    bad = A.run_checks(inp, ["E2"])[0]
    assert bad.status == "error" and "ZeroDivisionError" in bad.measured["exception"]
    assert A.run_checks(inp, ["nope"])[0].status == "error"


# Good and bad synthetic artifacts -------------------------------------------------------------


@pytest.fixture(scope="module")
def good_results() -> dict[str, CheckResult]:
    inp = F.build_inputs(F.good_artifacts(), sent_proposal_text=SENT_PROPOSAL)
    return {r.id: r for r in A.run_checks(inp)}


@pytest.fixture(scope="module")
def bad_results() -> dict[str, CheckResult]:
    inp = F.build_inputs(F.bad_artifacts())
    return {r.id: r for r in A.run_checks(inp)}


@pytest.mark.parametrize("cid", [c for c in FIXED_CHECK_IDS if c != "E17"])
def test_each_check_passes_on_the_good_artifacts(good_results, cid):
    r = good_results[cid]
    assert r.status == "pass", (cid, r.measured, r.evidence)
    assert r.threshold and r.owner == "WS0"
    assert '{"ref"' not in json.dumps(r.threshold)  # expected.json references resolved


@pytest.mark.parametrize("cid", [c for c in FIXED_CHECK_IDS if c not in ("E17", "E25", "GC2", "GC3")])
def test_each_check_fails_on_the_bad_artifacts(bad_results, cid):
    r = bad_results[cid]
    assert r.status == "fail", (cid, r.measured, r.evidence)


def test_bad_artifacts_special_rows(bad_results):
    assert bad_results["E25"].status == "pass"  # scorer v2 scores any run
    assert bad_results["GC2"].status == "skip" and "no scope report" in bad_results["GC2"].evidence[0]
    assert bad_results["GC3"].status == "skip"
    assert A.smoke_gate(list(bad_results.values()))["pass"] is False
    assert "E2" in A.smoke_gate(list(bad_results.values()))["failing"]


def test_smoke_gate_on_good_results(good_results):
    # E17 skips on the synthetic artifacts (no marked plans) and 3.5 gives it
    # no skip allowance, so the smoke gate fails on it and says why.
    gate = A.smoke_gate(list(good_results.values()))
    assert gate["pass"] is False and gate["skipped"] == ["E17"] and gate["failing"] == ["E17"]
    assert "not allowed" in gate["reasons"]["E17"]
    rest = [r for cid, r in good_results.items() if cid != "E17"]
    assert A.smoke_gate(rest) == {"failing": [], "reasons": {}, "skipped": [], "pass": True}


def _gated(cid: str, status: str, parts: dict[str, str]) -> CheckResult:
    return CheckResult(id=cid, status=status, measured={"parts": parts})


def test_smoke_gate_skip_rules_of_3_5():
    # E1: only the chat part may skip.
    assert A.smoke_gate([_gated("E1", "pass", {"priced_estimate": "pass", "chat_probe": "skip"})])["pass"] is True
    assert A.smoke_gate([_gated("E1", "skip", {"priced_estimate": "skip", "chat_probe": "skip"})])["pass"] is False
    # E15: no skip.
    gate = A.smoke_gate([_gated("E15", "skip", {"demo_track": "skip"})])
    assert gate["pass"] is False and gate["failing"] == ["E15"]
    # E16: the caveat part may skip on replay only.
    e16 = _gated("E16", "pass", {"ledger_legend": "pass", "legend_in_drawing_requests": "pass", "fixture_caveats": "skip"})
    assert A.smoke_gate([e16])["pass"] is True
    assert A.smoke_gate([e16], live=True)["pass"] is False
    e16_bad = _gated("E16", "pass", {"ledger_legend": "skip", "legend_in_drawing_requests": "pass", "fixture_caveats": "pass"})
    assert "ledger_legend" in A.smoke_gate([e16_bad])["reasons"]["E16"]
    # E11: the live count may skip on replay; the code-level gates may not.
    e11 = _gated("E11", "pass", {"demo_lighting": "skip", "no_tile_subtotal_rows": "pass", "census_vs_boq": "pass"})
    assert A.smoke_gate([e11])["pass"] is True
    e11_bad = _gated("E11", "pass", {"demo_lighting": "pass", "no_tile_subtotal_rows": "pass", "census_vs_boq": "skip"})
    assert A.smoke_gate([e11_bad])["pass"] is False
    # E19: live row, may skip whole on a replay, never on a live run.
    e19 = _gated("E19", "skip", {"outputs": "skip"})
    assert A.smoke_gate([e19])["pass"] is True and A.smoke_gate([e19], live=True)["pass"] is False
    # A skip on a row that does not gate the smoke is ignored by the gate.
    assert A.smoke_gate([_gated("GC2", "skip", {"generic_scope": "skip"})]) == {"failing": [], "reasons": {}, "skipped": [], "pass": True}
    # safe_refusal on a gated row is a fail.
    assert A.smoke_gate([_gated("E2", "safe_refusal", {"truth_bands": "safe_refusal"})])["failing"] == ["E2"]


def test_smoke_gate_dollar_coverage_reads_the_in_band_field():
    class _Score:
        dollar_coverage = 0.80
        dollar_coverage_partial = 0.95
    gate = A.smoke_gate([], score=_Score())
    assert gate["pass"] is False and gate["failing"] == ["dollar_coverage"]
    assert gate["dollar_coverage"]["value"] == 0.80 and gate["dollar_coverage"]["partial_credit"] == 0.95
    _Score.dollar_coverage = 0.85
    assert A.smoke_gate([], score=_Score())["pass"] is True


def test_results_table_shape(good_results):
    rows = A.results_table([good_results["E2"]])
    assert rows[0]["id"] == "E2" and rows[0]["status"] == "pass" and rows[0]["gates_7154_smoke"] is True
    assert rows[0]["parts"] == {"new_devices_need_branch": "pass", "truth_bands": "pass"}


# Targeted mutations: one failing part at a time ----------------------------------------------


def _mutated(mutate):
    art = F.good_artifacts()
    mutate(art)
    return F.build_inputs(art, sent_proposal_text=SENT_PROPOSAL)


def test_e1_bid_total_must_equal_verify_sum_to_the_cent():
    def mutate(art):
        art["priced_estimate"]["totals"]["bid_total_usd"] += 0.01
    r = A.check_e1(_mutated(mutate))
    assert r.status == "fail" and r.measured["priced_estimate"]["bid_total_equals_verify_sum"] is False
    assert r.measured["chat_probe"]["status"] == "pass"


def test_e1_chat_part_skips_without_a_probe_and_fails_on_decline():
    def no_probe(art):
        art.pop("chat_probe")
    r = A.check_e1(_mutated(no_probe))
    assert r.status == "pass" and r.measured["chat_probe"]["status"] == "skip"

    def declined(art):
        art["chat_probe"]["answer"] = "I cannot give labor hours."
    r = A.check_e1(_mutated(declined))
    assert r.status == "fail" and r.measured["chat_probe"]["declined"] is True


def test_e2_generic_rule_new_devices_need_branch_ft():
    def mutate(art):
        for sec in art["boq_spec"]["areas"][0]["sections"]:
            for g in sec["groups"]:
                if g["name"] == "WIRING":
                    g["subgroups"] = []
        art["main"]["boq"] = art["boq_spec"]
    r = A.check_e2(_mutated(mutate))
    assert r.status == "fail" and r.measured["new_devices_need_branch"]["branch_conductor_ft"] == 0


def test_e2_emt_must_be_good_not_only_acceptable():
    def mutate(art):
        for sec in art["boq_spec"]["areas"][0]["sections"]:
            for g in sec["groups"]:
                for sg in g["subgroups"]:
                    for it in sg["items"]:
                        if it["description"] == '3/4"C EMT':
                            it["quantity"] = 5200.0  # about 18% low: acceptable, not good
    r = A.check_e2(_mutated(mutate))
    assert r.status == "fail" and r.measured["truth_bands"]['3/4" EMT']["band"] == "acceptable"


def test_e4_wage_rfi_fails_even_with_a_decision():
    def mutate(art):
        art["brief_spec"]["rfis"] = [{"question": "Does NRS 338 prevailing wage apply?"}]
        art["main"]["brief"] = art["brief_spec"]
    r = A.check_e4(_mutated(mutate))
    assert r.status == "fail" and r.measured["no_wage_rfi"]["wage_rfis"] == 1


def test_e5_priced_house_exclusion_and_extra_allowance():
    def mutate(art):
        art["priced_estimate"]["allowances"].append({"name": "TEMPORARY POWER", "amount_usd": 5.0, "included": True})
    r = A.check_e5(_mutated(mutate))
    assert r.status == "fail" and r.measured["priced_house_exclusions"]["hits"] == ["TEMPORARY POWER"]
    assert r.measured["extra_allowances"]["extra_allowances"] == ["TEMPORARY POWER"]


def test_e7_needs_party_and_exhibit_cite():
    def mutate(art):
        art["priced_estimate"]["exclusions"][0] = "Fire alarm and access control by others"
    r = A.check_e7(_mutated(mutate))
    assert r.status == "fail" and r.measured["exclusion_names_party_with_cite"]["party_named"] is False


def test_e8_topic_without_citation_fails():
    def mutate(art):
        art["priced_estimate"]["exclusions"][3] = "Door hardware by others"
    r = A.check_e8(_mutated(mutate))
    assert r.status == "fail" and r.measured["topics_excluded_with_citation"]["missing"] == ["door hardware"]


def test_e9_alternate_selection_needs_a_reason_and_reconcile_skips_without_counts():
    def alt_no_reason(art):
        q = art["priced_estimate"]["quotes"][0]
        q["amount_usd"] = F.synthetic_expected()["e9"]["alternate_quote_pretax_usd"]
        q.pop("reason")
    r = A.check_e9(_mutated(alt_no_reason))
    assert r.status == "fail" and r.measured["pretax_within_tolerance"]["matched"] is None

    def alt_with_reason(art):
        q = art["priced_estimate"]["quotes"][0]
        q["amount_usd"] = F.synthetic_expected()["e9"]["alternate_quote_pretax_usd"]
        q.pop("reconciled")
    r = A.check_e9(_mutated(alt_with_reason))
    assert r.status == "pass" and r.measured["pretax_within_tolerance"]["matched"] == "alternate_selection"
    assert r.measured["quote_reconciles"]["status"] == "skip"


def test_e10_ea_line_needs_the_uom_gate():
    def mutate(art):
        for sec in art["boq_spec"]["areas"][0]["sections"]:
            for g in sec["groups"]:
                for sg in g["subgroups"]:
                    for it in sg["items"]:
                        if it["description"].startswith("L5"):
                            it["unit"], it["quantity"] = "EA", 7
        art["gate_report"]["checks"].append({"code": "uom_conflict_quote", "severity": "block", "detail": "L5 EA vs quote FT", "rows": ["B22"]})
    r = A.check_e10(_mutated(mutate))
    assert r.status == "fail" and r.measured["l5_unit_and_band"]["units"] == ["EA"]
    assert r.measured["uom_conflict_quote_raised"]["status"] == "pass"


def test_e11_tile_subtotal_row_fails():
    def mutate(art):
        art["boq_spec"]["areas"][0]["sections"][0]["groups"][0]["subgroups"][0]["items"][1]["coverage"] = "tile_subtotal"
    r = A.check_e11(_mutated(mutate))
    assert r.status == "fail" and len(r.measured["no_tile_subtotal_rows"]["rows"]) == 1


def test_e12_provisional_breakers_fail():
    def mutate(art):
        for sec in art["boq_spec"]["areas"][0]["sections"]:
            for g in sec["groups"]:
                if g["name"] == "SWITCHGEAR":
                    g["subgroups"][0]["items"].append(F._item("20A/1P CIRCUIT BREAKER", 22, "EA", caveat="(NEED TO CONFIRM SCOPE OF WORK)"))
    r = A.check_e12(_mutated(mutate))
    assert r.status == "fail" and r.measured["breaker_lines"]["provisional_ea"] == 22


def test_e13_library_median_without_spec_fact_fails():
    def mutate(art):
        for ln in art["priced_estimate"]["lines"]:
            if "DUPLEX" in ln["description"]:
                ln.pop("spec_fact_ref", None)
    r = A.check_e13(_mutated(mutate))
    assert r.status == "fail"


def test_e15_safe_refusal_elsewhere_fail_on_7154():
    def mutate(art):
        for sec in art["boq_spec"]["areas"][0]["sections"]:
            for g in sec["groups"]:
                for sg in g["subgroups"]:
                    for it in sg["items"]:
                        if "TRACK" in it["description"]:
                            it["unit"], it["quantity"], it["basis"] = "EA", 3, "counted"
        art["gate_report"] = {"blocking": True, "checks": [{"code": "linear_in_ea", "severity": "block", "detail": "track in EA", "rows": ["B4"]}]}
    inp = _mutated(mutate)
    assert A.check_e15(inp).status == "fail"
    other = copy.copy(inp)
    other.case_id = "7177"
    assert A.check_e15(other).status == "safe_refusal"


def test_e16_caveat_rows_fail_and_planned_requests_part_skips():
    def mutate(art):
        art.pop("planned_requests")
        art["boq_spec"]["areas"][0]["sections"][1]["groups"][-2]["subgroups"][0]["items"][0]["caveat"] = "(NEED TO CONFIRM DETAILS)"
    r = A.check_e16(_mutated(mutate))
    assert r.status == "fail" and r.measured["fixture_caveats"]["rows"] == 1
    assert r.measured["legend_in_drawing_requests"]["status"] == "skip"


def test_e18_planned_tokens_cap_on_7154():
    def mutate(art):
        art["planned_requests"][0]["est_input_tokens"] = 950_000
    r = A.check_e18(_mutated(mutate))
    assert r.status == "fail" and r.measured["planned_input_tokens"]["est_input_tokens"] == 1_010_000


def test_e20_demo_disposition_without_citation_fails():
    def mutate(art):
        art.pop("scope_report")
        art["main"].pop("scope_policy")
        for it in art["boq_spec"]["areas"][0]["sections"][0]["groups"][0]["subgroups"][0]["items"]:
            it["demo_disposition"] = {"decision": "remove_to_source"}
    r = A.check_e20(_mutated(mutate))
    assert r.status == "fail" and r.measured["demo_disposition"]["with_citation"] == 0


def test_e22_two_line_style_allows_one_row():
    def two_rows(art):
        items = art["boq_spec"]["areas"][0]["sections"][0]["groups"][0]["subgroups"][0]["items"]
        power = next(it for it in items if "POWER DEVICES" in it["description"])
        power["quantity"] = 20
        items.append(F._item("REMOVAL OF FLOOR OUTLET BACK TO SOURCE", 15, "EA", basis="counted"))
    inp = _mutated(two_rows)
    assert A.check_e22(inp).status == "pass"
    inp.options["demo_two_line_style"] = True
    r = A.check_e22(inp)
    assert r.status == "fail" and r.measured["demo_power_rows"]["max_rows"] == 1


def test_e23_couplings_outside_tolerance_fails():
    def mutate(art):
        for row in art["rendered"]["boq_rows"]:
            if "COUPLINGS" in row["description"]:
                row["quantity"] = 500
    r = A.check_e23(_mutated(mutate))
    assert r.status == "fail" and r.measured["emt_accessories"]["couplings_rel"] > 0.1


def test_e24_mechanical_document_detected_from_page_meta():
    def mutate(art):
        art["documents"].append({"id": "m", "name": "Mech.pdf", "category": "electrical_drawings", "page_count": 2, "facts_status": "done",
                                 "page_meta": [{"page": 1, "sheet_number": "M0.01", "title": "x", "page_role": "other"},
                                               {"page": 2, "sheet_number": "MD2.01", "title": "y", "page_role": "plan"}]})
    r = A.check_e24(_mutated(mutate))
    assert r.status == "fail" and r.measured["no_mechanical_set_read_as_electrical"]["offenders"] == ["Mech.pdf"]


def test_gc2_recall_and_violation():
    inp = F.build_inputs(F.good_artifacts(), sent_proposal_text="Excludes temporary power, bond, permits and fees, after-hours work.")
    r = A.check_gc2(inp)
    assert r.status == "fail" and r.measured["generic_scope"]["recall"] < 0.7

    def violation(art):
        art["priced_estimate"]["lines"][0]["description"] = "TEMPORARY POWER DISTRIBUTION"
    r = A.check_gc2(_mutated(violation))
    assert r.status == "fail" and r.measured["generic_scope"]["violations"] == ["TEMPORARY POWER DISTRIBUTION"]
    assert A.check_gc2(F.build_inputs(F.good_artifacts(), sent_proposal_text="No lexicon topic here.")).status == "skip"


def test_gc3_refusal_mapping():
    def refused(art):
        art["priced_estimate"]["refused"] = True
        art["gate_report"] = {"blocking": True, "checks": [{"code": "linear_in_ea", "severity": "block", "detail": "L5", "rows": ["B22"]}]}
        for sec in art["boq_spec"]["areas"][0]["sections"]:
            for g in sec["groups"]:
                for sg in g["subgroups"]:
                    for it in sg["items"]:
                        if it["description"].startswith("L5"):
                            it["unit"], it["quantity"] = "EA", 7
    inp = _mutated(refused)
    assert A.check_gc3(inp).status == "fail"  # 7154 must not refuse
    other = copy.copy(inp)
    other.case_id = "7177"
    r = A.check_gc3(other)
    assert r.status == "safe_refusal" and r.measured["refusals"]["confirmed"] == ["linear_in_ea"]

    def false_refusal(art):
        art["priced_estimate"]["refused"] = True
        art["gate_report"] = {"blocking": True, "checks": [{"code": "wage_type_unknown", "severity": "block", "detail": "", "rows": []}]}
    inp = _mutated(false_refusal)
    inp.case_id = "7177"
    r = A.check_gc3(inp)
    assert r.status == "fail" and r.measured["refusals"]["false_refusals"] == ["wage_type_unknown"]


def test_gc4_digest_must_include_own_project():
    def mutate(art):
        meta = art["priced_estimate"]["metadata"]
        meta["excluded_project_ids"] = [F.EXCLUDED_IDS[1]]
        from app.services.estimating_contracts import excluded_project_ids_sha256

        meta["excluded_project_ids_sha256"] = excluded_project_ids_sha256(meta["excluded_project_ids"])
    r = A.check_gc4(_mutated(mutate))
    assert r.status == "fail" and r.measured["excluded_projects"]["includes_own_project"] is False


def test_absent_artifacts_skip_with_a_reason():
    inp = F.build_inputs({"documents": [], "groups": [], "ledger": {}})
    statuses = {r.id: (r.status, r.evidence) for r in A.run_checks(inp)}
    for cid in ("E2", "E3", "E10", "E12", "E14", "E15", "E16", "E17", "E18", "E19", "E21", "E22", "E23", "E24", "E25", "GC2", "GC3"):
        assert statuses[cid][0] == "skip", cid
        assert statuses[cid][1], cid
    # rows where the absent artifact is the failure (3.5 baseline column)
    for cid in ("E1", "E4", "E9", "E13", "GC1", "GC4"):
        assert statuses[cid][0] == "fail", cid


# Text anchors (E17) -------------------------------------------------------------------------------


def test_classify_token_families():
    assert T.classify_token("L1") == ("fixture", "L1")
    assert T.classify_token("L1L1") == ("fixture", "L1")
    assert T.classify_token("L3-EM") == ("fixture", "L3")
    assert T.classify_token("1EELC-1,3") == ("circuit", "1EELC")
    assert T.classify_token("G/A/1LI-4") == ("circuit", "1LI")
    assert T.classify_token("L5/1LH") == ("fixture", "L5")
    assert T.classify_token("FSD-2") == ("equipment", "FSD")
    assert T.classify_token("VV-1-N") == ("equipment", "VV")
    assert T.classify_token("(X)(X)") == ("demo_x", "(X)")
    assert T.classify_token("OC") == ("switch", "OC")
    assert T.classify_token("CONDUIT") is None and T.classify_token("") is None


def test_wanted_for_legend_and_reference_entries():
    assert T.wanted_for_legend({"prefix": "L1E", "description": "L1 shaded/life-safety"}) == ({"fixture"}, {"L1"})
    assert T.wanted_for_legend({"prefix": "DR", "description": "Remove open-symbol duplex"}) == ({"demo_x"}, None)
    assert T.wanted_for_legend({"prefix": "GFI", "description": "GFCI duplex"}) == ({"circuit", "equipment"}, None)
    assert T.wanted_for_legend({"prefix": "D", "description": "Wall dimmer"}) == ({"switch"}, None)
    assert T.wanted_for_legend({"prefix": "Z", "description": "something"}) == (None, None)
    assert T.is_reference_legend({"description": "Existing XF-18 remain; reference only"})
    assert not T.is_reference_legend({"description": "L1 troffer"})


def test_mark_distances_and_stacked_duplicates():
    sheet = T.SheetAnchors("doc", 3, 42.0, 30.0, 4.0, "E3.01", T.METHOD_TEXT_LAYER, "full", anchors=[
        T.TextAnchor("fixture", "L1", "L1", 0.2, 0.2), T.TextAnchor("circuit", "1LI", "1LI-2", 0.5, 0.5),
    ])
    legend = [{"prefix": "L1", "description": "L1 troffer"}, {"prefix": "R", "description": "Duplex receptacle"},
              {"prefix": "EX", "description": "Existing panel remain; reference only"}]
    marks = [{"kind": "dot", "points": [[0.2, 0.2]], "legend_index": 0}, {"kind": "dot", "points": [[0.5, 0.52]], "legend_index": 1},
             {"kind": "dot", "points": [[0.9, 0.9]], "legend_index": 2}]
    rows = T.mark_distances(sheet, marks, legend)
    assert len(rows) == 2 and rows[0]["distance_ft"] == 0.0 and rows[0]["anchor"] == "L1"
    assert rows[1]["anchor"] == "1LI-2" and rows[1]["distance_ft"] == pytest.approx(0.02 * 30 * 4, abs=0.01)
    assert rows[1]["distance_any_ft"] == rows[1]["distance_ft"] and not rows[1]["fallback"]
    assert len(T.mark_distances(sheet, marks, legend, skip_reference=False)) == 3
    assert T.stacked_duplicates([[0.1, 0.1], [0.1, 0.1], [0.1, 0.1], [0.5, 0.5]], 42.0, 30.0, 4.0) == 2
    assert T.stacked_duplicates([[0.1, 0.1], [0.3, 0.3]], 42.0, 30.0, 4.0) == 0
    assert T.stacked_duplicates([[0.1, 0.1], [0.1, 0.1]], 42.0, 30.0, None) == 1


def test_e17_with_synthetic_anchors(monkeypatch):
    good_sheet = T.SheetAnchors(F.ELEC_DOC, 3, 42.0, 30.0, 4.0, "E3.01", T.METHOD_TEXT_LAYER, "full",
                                anchors=[T.TextAnchor("fixture", "L1", "L1", 0.2, 0.2), T.TextAnchor("fixture", "L1", "L1", 0.4, 0.4)])

    class FakeExtractor:
        def __init__(self, *a, **k):
            pass

        def sheet(self, document_id, pdf, page, *, ft_per_in_fallback=None):
            return good_sheet

    monkeypatch.setattr(T, "AnchorExtractor", FakeExtractor)

    class Docs:
        def get(self, doc_id):
            return b"%PDF"

    good = F.build_inputs(F.good_artifacts(), docs=Docs())
    r = A.check_e17(good)
    assert r.status == "pass", r.measured
    assert r.measured["median_anchor_distance"]["median_ft"] == 0.0
    bad = F.build_inputs(F.bad_artifacts(), docs=Docs())
    r = A.check_e17(bad)
    assert r.status == "fail" and r.measured["stacked_duplicates"]["stacked"] == 2 and r.measured["over_5ft_share"]["over_5ft"] == 4
    nodocs = F.build_inputs(F.good_artifacts())
    assert A.check_e17(nodocs).status == "skip"


# Golden: the self-test on the weekend bundle ---------------------------------------------------------


@golden
@pytest.mark.golden
def test_acceptance_self_test_on_the_weekend_bundle(tmp_path):
    """Every 3.5 row measured on the stored weekend bundle: FAIL with the
    Baseline column values where the artifact exists in the bundle, skip
    elsewhere, listing exactly which rows skipped and why."""
    report = A.self_test(WEEKEND, eval_root=EVAL_ROOT, backup_dir=BACKUP, out_dir=tmp_path / "replay")
    statuses = {r.id: r.status for r in report.rows}
    expected = {cid: A.BASELINE_7154[cid]["status"] for cid in FIXED_CHECK_IDS}
    assert statuses == expected
    failures = [(r.id, r.measured) for r in report.failures()]
    assert not failures, failures
    assert report.skipped() == {
        "GC2": ["scope_report: no scope report"],
        "GC3": ["refusals: no gate report and no priced estimate (nothing refused)"],
    }
    by = {r.id: r.measured for r in report.rows}
    assert by["E3"]["denial_phrases"]["hits"] == 4
    assert by["E5"]["extra_allowances"]["house_excluded"] == 8 and by["E5"]["extra_allowances"]["extra_allowances"] == 10
    assert by["E11"]["demo_lighting"]["ai"] == 34
    assert by["E12"]["breaker_lines"]["breaker_total_ea"] == 26 and by["E12"]["breaker_lines"]["provisional_ea"] == 22
    assert by["E16"]["ledger_legend"]["legend_pages"] == []
    assert by["E21"]["unpriced_modifier_rows"]["rows"] == 4 and by["E21"]["receptacle_total"]["total"] == 74
    assert by["E22"]["demo_power_rows"]["rows"] == 4 and by["E22"]["sum_within_1"]["sum"] == 34
    assert by["E24"]["spec_pages_sent"]["sent"] == 170
    e17 = by["E17"]
    assert 205 <= e17["over_5ft_share"]["marks"] <= 208 and 100 <= e17["over_5ft_share"]["over_5ft_any"] <= 130
    assert e17["stacked_duplicates"]["stacked"] >= 20 and e17["ft_lines_have_polylines"]["polylines"] == 0
    # parts that skipped inside failing rows (artifacts no legacy stage produces yet)
    parts = {r.id: r.measured["parts"] for r in report.rows}
    assert parts["E10"]["uom_conflict_quote_raised"] == "skip"
    assert parts["E11"]["census_vs_boq"] == "skip"
    assert parts["E16"]["legend_in_drawing_requests"] == "skip"
    assert parts["E18"]["facts_block_every_page"] == "skip"
    assert parts["E1"]["chat_probe"] == "fail"
    assert "truth_cost" not in json.dumps(by["E25"])


# Live-run bundle members (bid conditions, pricing metadata) -----------------------------------


def test_e1_priced_version_must_price_the_latest_boq_version():
    def stale_boq(art):
        art["priced_estimate"]["metadata"]["boq_version"] = 1
        art["deliverable_versions"] = {"boq": 2}
    r = A.check_e1(_mutated(stale_boq))
    assert r.status == "fail" and r.measured["priced_estimate"]["boq_version"] == 1
    assert r.measured["priced_estimate"]["latest_boq_version"] == 2

    def same_boq(art):
        art["priced_estimate"]["metadata"]["boq_version"] = 2
        art["priced_estimate"]["metadata"]["pricing_version"] = 3
        art["deliverable_versions"] = {"boq": 2}
    r = A.check_e1(_mutated(same_boq))
    assert r.measured["priced_estimate"]["status"] == "pass" and r.measured["priced_estimate"]["pricing_version"] == 3


def test_e4_reads_the_wage_from_bid_conditions_without_a_priced_estimate():
    def run_column(art):
        art.pop("priced_estimate")
        art["bid_conditions"] = {"wage_type": "prevailing_wage"}
    r = A.check_e4(_mutated(run_column))
    m = r.measured["wage_decision"]
    assert r.status == "fail" and m["present"] is True and m["wage_type"] == "prevailing_wage"
    assert m["wage_source"] == "run.bid_conditions" and m["labor_rate_usd"] is None and m["priced"] is False

    def policy_output(art):
        art.pop("priced_estimate")
        art["policy"] = {"bid_conditions": {"wage_type": "prevailing_wage"}}
    m = A.check_e4(_mutated(policy_output)).measured["wage_decision"]
    assert m["present"] is True and m["wage_source"] == "policy.bid_conditions"

    def nothing(art):
        art.pop("priced_estimate")
    assert A.check_e4(_mutated(nothing)).measured["wage_decision"]["present"] is False


def test_e4_priced_wage_wins_and_bid_conditions_fill_a_missing_one():
    def priced_without_wage(art):
        art["priced_estimate"]["wage_type"] = None
        art["bid_conditions"] = {"wage_type": "prevailing_wage"}
    r = A.check_e4(_mutated(priced_without_wage))
    assert r.status == "pass" and r.measured["wage_decision"]["wage_source"] == "run.bid_conditions"
    r = A.check_e4(_mutated(lambda art: art.update(bid_conditions={"wage_type": "non_prevailing"})))
    assert r.status == "pass" and r.measured["wage_decision"]["wage_source"] == "priced_estimate"


def test_a_rewritten_category_question_is_reported_as_its_rewrite():
    from app.services import plans_reader_acceptance as A

    inputs = A.AcceptanceInputs(artifacts={
        "category:electrical_drawings": {"questions": [
            {"row_id": "Q10", "question": "Confirm L4 control method and L5 module lengths."},
            {"row_id": "Q11", "question": "Which panel feeds the pharmacy?"},
        ]},
        "scope_report": {"questions_rewritten": [{"category": "electrical_drawings", "row_id": "Q10",
                                                  "question": "Confirm L4 control method and L5 module lengths.",
                                                  "after": "Confirm L5 module lengths."}]},
    })
    assert inputs.questions() == ["Confirm L5 module lengths.", "Which panel feeds the pharmacy?"]
