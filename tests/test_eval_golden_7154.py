"""Golden tests on the 7154 case (WS0; spec 3.4, 3.5 E25, 1.2 item 2).

These need EVAL_ROOT (the stored weekend bundle, the 7154 reference and
expected.json) and the production backup on disk, so they skip in CI.
Every real dollar figure is read from EVAL_ROOT/references/7154/expected.json;
the test file holds counts, ratios and tolerances only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services import eval_reference as R
from app.services import plans_reader_score as S
from app.services.estimating_contracts import CHECK_SPECS, REFERENCE_KEYS, resolve_reference_thresholds

EVAL_ROOT = Path(get_settings().eval_root)
BUNDLE = EVAL_ROOT / "bundles" / "7154" / "ff8fad25"
REF_DIR = EVAL_ROOT / "references" / "7154"
BACKUP = S.DEFAULT_BACKUP_DIR

pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(
        not (BUNDLE / "deliverables" / "boq_v1.json").exists()
        or not (REF_DIR / "expected.json").exists()
        or not (BACKUP / "data" / "project_files.json").exists(),
        reason="EVAL_ROOT bundle, 7154 expected.json or the production backup is not on disk",
    ),
]


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads((REF_DIR / "expected.json").read_text())


@pytest.fixture(scope="module")
def reference() -> dict:
    """Rebuilt from the backup every time (not the stored file), so the
    builder itself is under test."""
    adjudications = R.load_adjudications(REF_DIR / "adjudications.json")
    return R.build_reference("7154", "7dfc64e6", BACKUP, adjudications=adjudications)


@pytest.fixture(scope="module")
def ai_rows() -> list[S.BoqRow]:
    return S.rows_from_spec(json.loads((BUNDLE / "deliverables" / "boq_v1.json").read_text()))


def test_expected_json_carries_every_documented_key(expected):
    for key in REFERENCE_KEYS:
        node = expected
        for part in key.split("."):
            assert part in node, key
            node = node[part]
        assert isinstance(node, (int, float))
    for cid in ("E9", "E13"):
        resolved = resolve_reference_thresholds(CHECK_SPECS[cid], expected)
        assert "ref" not in json.dumps(resolved)
    for section in ("totals", "general_material", "recap", "scorer", "quotes", "seeds", "adjudications"):
        assert section in expected


def test_reference_7154_numbers(reference, expected):
    t = expected["totals"]
    recap = reference["estimator_recap"]["totals"]
    assert recap["cost"] == pytest.approx(t["estimator_cost_usd"], abs=0.01)
    assert recap["bid"] == pytest.approx(t["estimator_bid_usd"], abs=0.01)
    assert recap["hours"] == pytest.approx(t["estimator_hours"], abs=0.001)
    assert recap["labor_rate"] == t["estimator_rate_usd"]
    assert recap["ohp_labor_pct"] == t["estimator_ohp_labor_pct"] and recap["ohp_material_pct"] == t["estimator_ohp_material_pct"]
    assert recap["material"] == pytest.approx(t["estimator_material_cost_usd"], abs=0.01)
    assert recap["labor"] == pytest.approx(t["estimator_labor_cost_usd"], abs=0.01)
    for name, row in expected["recap"].items():
        assert reference["estimator_recap"]["sections"][name]["total"] == pytest.approx(row["total"], abs=0.01), name
        assert reference["estimator_recap"]["sections"][name]["material"] == pytest.approx(row["material"], abs=0.01), name
    sent = reference["g3_sent"]
    assert sent["total"] == pytest.approx(t["g3_sent_usd"]) and sent["sends"] == t["g3_sends"]
    assert sent["distinct_totals"] == [pytest.approx(t["g3_sent_usd"])] and sent["spread"] == 0.0
    assert sent["components"]["material_amount"] == pytest.approx(t["g3_sent_material_usd"])
    assert sent["components"]["labor_amount"] == pytest.approx(t["g3_sent_labor_usd"])
    verify = reference["g3_verify"]
    assert verify["labor_amount"] == pytest.approx(t["verify_labor_usd"])
    assert verify["materials_amount"] == pytest.approx(t["verify_materials_usd"])
    assert verify["materials_markup_amount"] == pytest.approx(t["verify_materials_markup_usd"])
    assert verify["low_voltage_amount"] == t["verify_low_voltage_usd"] and verify["matches_sent"] is True
    assert verify["sum"] == pytest.approx(t["g3_sent_usd"])
    labor = reference["g3_labor"]
    assert labor["hours"] == t["g3_hours"] and labor["rate"] == t["g3_rate_usd"] and labor["parsed"] is True
    assert labor["amount_consistent"] is True
    assert labor["amount"] == pytest.approx(t["verify_labor_usd"])
    assert reference["derived"]["hour_roundup"] == pytest.approx(t["hour_roundup"], abs=0.001)
    assert reference["g3_markups"]["materials_pct"] == pytest.approx(t["materials_markup_pct"])
    assert reference["derived"]["tax_rate_pct"] == expected["quotes"]["tax_rate_pct"]
    # quotes: Graybar selected, Codale the alternative, General Material manual
    lighting = next(m for m in reference["g3_materials"] if m["category"] == "Lighting")
    assert lighting["selected"]["amount"] == expected["quotes"]["graybar_pretax_usd"]
    assert lighting["selected"]["origin"] == "vendor" and lighting["selected"]["file"]["sha256"]
    assert [a["amount"] for a in lighting["alternatives"]] == [expected["quotes"]["codale_pretax_usd"]]
    gm = next(m for m in reference["g3_materials"] if m["category"] == "General Material")
    assert gm["selected"]["amount"] == expected["quotes"]["general_material_manual_usd"]
    assert reference["derived"]["quote_replaced_sections"] == ["LIGHTING FIXTURES"]
    assert reference["conditions"]["wage_type"] == "prevailing_wage" and reference["conditions"]["labor_needed"] == "union"
    assert reference["conditions"]["owner_type"] == "public_other" and reference["conditions"]["labor_time"] == "day_work"


def test_reference_7154_verify_leak_and_corrected_total(reference, expected):
    g = expected["general_material"]
    gm = reference["g3_general_material"]
    assert gm["wiring_only"] is True
    assert gm["amount"] == pytest.approx(g["wiring_material_usd"], abs=0.01)
    assert gm["other_items_material_missing"] == pytest.approx(g["other_items_material_usd"], abs=0.01)
    detail = reference["derived"]["g3_corrected_detail"]
    assert detail["general_material_pretax"] == pytest.approx(g["corrected_pretax_usd"])
    assert detail["general_material_taxed"] == pytest.approx(g["corrected_taxed_usd"])
    assert detail["categories_taxed"]["Lighting"] == pytest.approx(g["lighting_taxed_usd"])
    assert detail["materials"] == pytest.approx(g["materials_corrected_usd"])
    assert detail["materials_markup"] == pytest.approx(g["markup_corrected_usd"])
    assert detail["total"] == pytest.approx(expected["totals"]["g3_corrected_usd"])
    # ADJ4 agrees with the computed figure, so no discrepancy warning
    assert reference["derived"]["g3_corrected_total"] == pytest.approx(expected["totals"]["g3_corrected_usd"])
    assert reference["derived"]["g3_corrected_total_source"] == "adjudication"
    assert not any("differs" in w for w in reference["warnings"])
    assert {c["check"]: c["status"] for c in reference["data_quality"]} == {
        "verify_sum_matches_sent": "pass",
        "materials_from_quotes": "pass",
        "boq_estimate_join_rate": "pass",
        "recap_cost_matches_lines": "pass",
        "labor_notes_parsed": "pass",
        "labor_amount_consistent": "pass",
    }


def test_reference_7154_lines_and_adjudications(reference, expected):
    sc = expected["scorer"]
    q = sc["quantities"]
    lines = reference["quantity_lines"]
    assert len(lines) == sc["count_lines_total"] + sc["length_lines_total"] == 28
    assert len(reference["accessory_lines"]) == 6
    assert all(ln["est_ref"] for ln in lines)
    by = {ln["description"]: ln for ln in lines}
    assert by["#12 THHN/THWN Cu"]["qty"] == pytest.approx(q["wire_12_ft"], abs=0.01)
    assert by["#12 THHN/THWN Cu"]["qty_base"] == pytest.approx(q["wire_12_base_ft"], abs=0.01)
    assert by["#12 THHN/THWN Cu"]["unit_item_cost"] == pytest.approx(expected["unit_costs"]["wire_12_unit_item_cost_usd"], abs=0.001)
    assert by['3/4"C EMT']["unit_item_cost"] == pytest.approx(expected["unit_costs"]["emt_34_unit_item_cost_usd"], abs=0.001)
    assert by["#18/4C CONTROL CABLE"]["qty"] == q["control_cable_ft"]
    assert by["DUPLEX RECEPTACLE, 125V"]["unit_material"] == expected["unit_costs"]["duplex_material_usd"]
    assert by["DUPLEX RECEPTACLE, 125V, GFCI"]["unit_material"] == expected["unit_costs"]["gfci_material_usd"]
    # the BOQ rounds the control cable to a whole foot, so the sum sits about a dollar under the recap total
    assert sum(ln["value"] for ln in lines) == pytest.approx(sc["truth_cost_usd"], abs=2.0)
    assert sum(1 for ln in lines if ln["unit"] == "FT") == sc["length_lines_total"]
    # adjudications: ADJ1 adds the GFCI breakers, ADJ2 and ADJ3 reprice the EM fixtures, SHADED is EM
    adj = {ln["description"]: ln for ln in reference["quantity_lines_adjudicated"]}
    assert len(adj) == 29
    assert adj["20A/1P CIRCUIT BREAKER, GFCI"]["qty"] == expected["adjudications"]["adj1_gfci_breakers_ea"]
    l1em = next(v for k, v in adj.items() if k.startswith("L1-EM"))
    l3em = next(v for k, v in adj.items() if k.startswith("L3-EM"))
    raw_l1em = next(v for k, v in by.items() if k.startswith("L1-EM"))
    assert raw_l1em["unit_material"] == expected["adjudications"]["adj2_l1_em_unit_material_usd"][0]
    assert l1em["unit_material"] == expected["adjudications"]["adj2_l1_em_unit_material_usd"][1]
    assert l3em["unit_material"] == expected["adjudications"]["adj3_l3_em_unit_material_usd"][1]
    assert reference["aliases"] == {"SHADED": "EM"}
    assert reference["adjudications_reviewed"] is False
    assert [a["id"] for a in reference["adjudications_applied"]] == ["ADJ1", "ADJ2", "ADJ3", "ADJ4", "ADJ5", "aliases"]
    assert all(a["status"] == "unreviewed" for a in reference["adjudications_applied"])


def test_scorer_v2_on_weekend_bundle(reference, ai_rows, expected):
    """E25: 17 of 22 count lines exact raw, partial-credit dollar coverage 0.319 +/- 0.005,
    L1 <-> L1 and L1-EM <-> AI LIFE SAFETY (5), TR never paired with a
    demolition row, two unit mismatches, zero unresolved collisions."""
    sc = expected["scorer"]
    e25 = CHECK_SPECS["E25"].threshold
    score = S.score_case(ai_rows, reference, adjudicated=False)
    assert score.matcher_version == e25["matcher_version"] == 2
    assert score.count_lines_exact == e25["count_lines_exact"] == sc["count_lines_exact"]
    assert score.count_lines_total == e25["count_lines_total"] == sc["count_lines_total"]
    # E25's 0.319 is the partial-credit (prototype) formula; the 3.6 in-band
    # reading is lower on the same bundle.
    assert score.dollar_coverage_partial == pytest.approx(e25["dollar_coverage"], abs=e25["dollar_coverage_tol"])
    assert score.dollar_coverage is not None and score.dollar_coverage < score.dollar_coverage_partial
    assert score.covered_cost == pytest.approx(sc["covered_usd"], abs=1.0)
    assert score.truth_cost == pytest.approx(sc["truth_cost_usd"], abs=2.0)
    assert score.collisions.unresolved == e25["unresolved_truth_key_collisions"] == 0
    assert score.unit_mismatches == sc["unit_mismatches"] == 2
    assert score.length_lines_total == 6 and score.length_lines_produced == 0
    by = {ln.description.split(" - ")[0].split(",")[0]: ln for ln in score.lines}
    l1 = by["L1"]
    assert l1.status == "exact" and l1.ai_qty == expected["scorer"]["quantities"]["l1_ea"]
    assert len(l1.ai_refs) == 1 and "LIFE SAFETY" not in l1.ai_descriptions[0]
    l1em = by["L1-EM"]
    assert l1em.status == "exact" and l1em.ai_qty == 5 and "LIFE SAFETY" in l1em.ai_descriptions[0]
    assert by["L3-EM"].status == "exact" and "SHADED" in by["L3-EM"].ai_descriptions[0]
    tr = next(ln for ln in score.lines if ln.description.endswith(", TR"))
    assert tr.ai_qty == 0 and tr.ai_refs == [] and tr.status == "miss"
    demo_rows = [e for e in score.extras if e["section"] == "DEMOLITION"]
    assert all(e["kind"] == "demo_other" for e in demo_rows)
    duplex = next(ln for ln in score.lines if ln.description == "DUPLEX RECEPTACLE, 125V")
    assert duplex.ai_qty == 57 and duplex.status == "within_10pct"
    demo_lighting = next(ln for ln in score.lines if ln.kind == "demo_lighting")
    demo_power = next(ln for ln in score.lines if ln.kind == "demo_power")
    assert demo_lighting.ai_qty == 34 and demo_power.ai_qty == 34
    track = next(ln for ln in score.lines if ln.kind == "demo_track")
    assert track.status == "unit_mismatch" and track.unit_mismatch_rows == 1
    l5 = next(ln for ln in score.lines if ln.description.startswith("L5"))
    assert l5.status == "unit_mismatch"
    assert score.extras_count == 6 and sum(e["qty"] for e in score.extras if e["kind"] == "breaker") == 26
    # adjudicated truth: ADJ1 makes the 4 GFCI breakers an exact line
    adjudicated = S.score_case(ai_rows, reference, adjudicated=True)
    assert adjudicated.count_lines_exact == sc["count_lines_exact_adjudicated"]
    assert adjudicated.count_lines_total == sc["count_lines_total_adjudicated"]
    assert adjudicated.extras_count == 5
    md = S.score_markdown(score)
    assert "17 of 22" in md and chr(0x2014) not in md and chr(0x2013) not in md
    data = S.score_json(score)
    assert "truth_cost" not in data and data["count_lines_exact"] == 17


def test_v1_scorer_baseline_is_the_e25_failure(ai_rows):
    """The v1 one-to-one matcher mis-pairs (recall 60.7%, MAPE 36%, L1 mispaired)."""
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "plans_reader_golden.py"
    if "plans_reader_golden" in sys.modules:
        golden = sys.modules["plans_reader_golden"]
    else:
        spec = importlib.util.spec_from_file_location("plans_reader_golden", script)
        golden = importlib.util.module_from_spec(spec)
        sys.modules["plans_reader_golden"] = golden
        spec.loader.exec_module(golden)
    est = S.parse_estimator_boq(S.find_backup_boq("7dfc64e6", BACKUP).read_bytes())
    report = golden.match_rows(ai_rows, est)
    assert report.recall == pytest.approx(0.6071, abs=0.001)
    assert report.exact_matches == 9
    mispaired = [p for p in report.pairs if p.est.description.startswith("L1 ") and "LIFE SAFETY" in p.ai.description]
    assert mispaired, "v1 pairs the estimator L1 with the AI LIFE SAFETY row"
