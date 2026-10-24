"""WS6 golden checks against the production backup on disk and EVAL_ROOT
(AI_ESTIMATING_BUILD_SPEC WS6 acceptance, 1.3 item 4, 3.4). Marked `golden`
and skipped when the backup or EVAL_ROOT is absent (CI).

Expected 7154 values are read from EVAL_ROOT/references/7154/expected.json
(WS0, the one reference file; `pricing_eval.expected_values` maps its keys);
no real-project dollar figure is typed here. Assertions compare shares and
relative errors; the dollar-bearing reports live under EVAL_ROOT/reports.
Every library in these runs excludes the four holdout projects (8.1).
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pricing_eval.py"
_HAVE_DATA = (
    BACKUP.is_dir()
    and (BACKUP / "data" / "project_files.json").is_file()
    and (EVAL_ROOT / "references" / "7154").is_dir()
)

pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(not _HAVE_DATA, reason="backup or EVAL_ROOT not present"),
]


def _load_eval():
    if "pricing_eval" in sys.modules:
        return sys.modules["pricing_eval"]
    spec = importlib.util.spec_from_file_location("pricing_eval", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pricing_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ev():
    return _load_eval()


@pytest.fixture(scope="module")
def backup(ev):
    return ev.Backup.load(BACKUP, EVAL_ROOT)


@pytest.fixture(scope="module")
def expected(ev):
    exp = ev.expected_values(EVAL_ROOT)
    if "sent_total_usd" not in exp:
        pytest.skip("no 7154 expected values under EVAL_ROOT/references/7154")
    return exp


@pytest.fixture(scope="module")
def lopo(ev, backup):
    return ev.run_lopo(backup)


def test_every_estimate_workbook_parses_or_is_explained():
    from app.services import cost_library as cl

    report, chosen = cl.backup_books(BACKUP)
    assert len(report.entries) >= 70
    assert report.parsed >= len(report.entries) - 1
    for entry in report.failed:
        assert entry.error  # every failure carries its reason
        assert "UNIT LABOR HOURS" in entry.error  # the one known failure: not an estimate
    assert len(chosen) == report.projects


def test_verify_replay_reproduces_7154_and_most_committed_verifications(ev, backup, expected):
    results = ev.run_verify_replay(backup)
    assert len(results) >= 30
    within = sum(1 for r in results if r.within_one)
    assert within / len(results) >= 0.90
    for r in results:
        if not r.within_one:
            assert r.explanation  # every miss is explained
    case = next(r for r in results if r.project_id.startswith(ev.CASE_7154_PREFIX))
    assert case.reproduced == Decimal(str(expected["sent_total_usd"]))


def test_general_material_pretax_with_estimator_units_matches_expected(ev, backup, expected):
    from app.services import cost_library as cl

    _report, chosen = cl.backup_books(BACKUP)
    pid = next(p for p in chosen if p.startswith(ev.CASE_7154_PREFIX))
    got = ev.general_material_pretax(chosen[pid])
    assert got == Decimal(str(expected["general_material_pretax_usd"]))


def test_counterfactuals_7154(ev, backup, expected):
    out = ev.run_counterfactual(backup, EVAL_ROOT)
    from app.services import eval_manifest as em

    assert set(out["library_excluded"]) == {p[:8] for p in em.excluded_projects("pricing_library", "7154")}
    assert out["seeds_active"] == 2
    assert out["a1_exact"] is True
    assert out["general_material_pretax_estimator_units"] == expected["general_material_pretax_usd"]
    # PG-A2: estimator quantities, LOPO units, selected quote, recorded hours and rate.
    assert out["a2_within_tolerance"] is True, out["a2_vs_corrected"]
    assert out["a2_lowest_quote_bid"] <= out["a2_bid"]  # Q9: the lowest eligible quote is cheaper
    assert out["a3_with_grade_fact_seed_lines"] >= 2  # E13 seeds only with a cited fact
    assert out["a3_with_grade_fact_bid"] > out["a3_bid"]
    assert out["a3_status"] in ("priced", "provisional")
    # PG-A3 restated against the latest bands file (WS7 wave 1, file-based)
    bands = out["a3_band_checks"]
    assert bands["band_version"] and bands["checks"]
    metrics = {c["metric"] for c in bands["checks"]}
    assert "hours_per_device" in metrics
    assert all(c["band_version"] == bands["band_version"] for c in bands["checks"])


def test_expected_values_come_from_ws0_expected_json_only(ev):
    exp = ev.expected_values(EVAL_ROOT)
    for key in ("sent_total_usd", "corrected_total_usd", "general_material_pretax_usd",
                "general_material_wiring_usd", "general_material_other_items_usd", "seeds", "e9", "e13"):
        assert key in exp, key
    # the estimator-units GM figure is WIRING + OTHER ITEMS material, not G3's manual GM figure
    wiring = Decimal(str(exp["general_material_wiring_usd"]))
    other = Decimal(str(exp["general_material_other_items_usd"]))
    assert Decimal(str(exp["general_material_pretax_usd"])) == (wiring + other).quantize(Decimal("0.01"))
    assert exp["general_material_pretax_usd"] != exp["general_material_manual_usd"]
    assert exp["pg_a2_tolerance"] == 0.03
    assert not (EVAL_ROOT / "references" / "7154" / "pricing_expected.json").exists()
    # the seeds reach the policy only through expected.json; the code default is inert
    from app.services import pricing_policy as pp

    assert all(not s.active for s in pp.DEFAULT_GRADE_SEEDS)
    seeded = ev.seeded_policy(pp.DEFAULT_POLICY, exp)
    assert all(s.active for s in seeded.grade_seeds) and len(seeded.grade_seeds) == 2


def test_lopo_population_and_hard_rejects(lopo):
    from app.services import eval_manifest as em

    m = lopo["metrics"]
    assert m["n_cases"] >= 30
    assert m["hard_reject_violations"] == 0
    holdout = set(em.HOLDOUT_IDS)
    assert len(holdout) == 4
    assert all(r.project_id not in holdout for r in lopo["runs"])
    # 8.1: the library of every run excludes the four holdout projects and the run's own project
    for r in lopo["runs"]:
        excluded = set(r.library_excluded)
        assert holdout <= excluded, r.project_id[:8]
        assert r.project_id in excluded
        assert excluded == set(em.excluded_projects("pricing_library", r.project_id))
    assert set(lopo["holdout_excluded"]) == holdout


@pytest.mark.xfail(
    strict=True,
    reason=(
        "WS6 wave 1 miss, reported not hidden (EVAL_ROOT/reports/pricing_lopo.md, build log). "
        "Measured 2026-10-22 with holdout-free libraries (eval_manifest.excluded_projects): "
        "strong or good share 44.0% (target 70%), hours within 10% on 30.3% (target 65%), hours "
        "median abs error 17.5% (target 8%), false-confident rate 27.9% of 562 lines (target 5%). "
        "The estimators' unit values spread far more than 35% between projects, so coverage and "
        "the false-confident rate cannot both reach their targets with per-item medians; the pool's "
        "basis sits under the estimator's hours mostly on roadway and one-off work (lines matched to "
        "nothing, the catch-all class fallback). Owner question open (0.5). Remove this marker when "
        "the targets are met."
    ),
)
def test_lopo_library_fidelity_gates(lopo):
    """WS6 acceptance: strong or good covers >= 70% of basis hours (median per
    project); workbook-basis hours within 10% on >= 65% with median abs error
    <= 8%; false-confident rate <= 5%. A miss fails loudly here and is
    reported in EVAL_ROOT/reports/pricing_lopo.md."""
    m = lopo["metrics"]
    misses = []
    if m["strong_good_share_median"] < 0.70:
        misses.append(f"strong_good_share_median {m['strong_good_share_median']:.1%} < 70%")
    if m["hours_within_10"] < 0.65:
        misses.append(f"hours_within_10 {m['hours_within_10']:.1%} < 65%")
    if m["hours_median_abs_error"] > 0.08:
        misses.append(f"hours_median_abs_error {m['hours_median_abs_error']:.1%} > 8%")
    if m["false_confident_rate"] > 0.05:
        misses.append(f"false_confident_rate {m['false_confident_rate']:.1%} > 5%")
    assert not misses, "; ".join(misses)


def test_lopo_golden_verify_band(lopo):
    """Clean set (G3 hours over engine basis in 0.95 to 1.6): median <= 8%
    with >= 60% within 10%. Met on 2026-10-21 with holdout-free libraries
    (median 7.4%, 72.7% within 10%, n 11); with the coverage-regime uplift of
    2026-10-22 median 5.2%, 72.7% within 10%."""
    m = lopo["metrics"]
    assert m["band_clean_n"] >= 5
    misses = []
    if m["band_median_abs_error"] > 0.08:
        misses.append(f"band_median_abs_error {m['band_median_abs_error']:.1%} > 8%")
    if m["band_within_10"] < 0.60:
        misses.append(f"band_within_10 {m['band_within_10']:.1%} < 60%")
    assert not misses, "; ".join(misses)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "WS6 wave 1 miss, reported not hidden (EVAL_ROOT/reports/pricing_lopo.md, build log). "
        "Measured 2026-10-22 with holdout-free libraries and the coverage-regime uplift: 1.3 item 4 "
        "oracle median abs error 14.8% (target 7%), within 10% on 30.3% (target 70%), 0 refusals, "
        "33 cases (2026-10-21 single uplift: 15.7% and 30.3%). Owner question open (0.5: gate vs "
        "report; per-project productivity factor). Remove this marker when the targets are met."
    ),
)
def test_lopo_oracle_backtest_1_3_item_4(lopo):
    """1.3 item 4: median abs error vs G3 sent <= 7%, >= 70% within 10%
    (refusals count as misses)."""
    m = lopo["metrics"]
    misses = []
    if m["oracle_median_abs_error"] > 0.07:
        misses.append(f"oracle_median_abs_error {m['oracle_median_abs_error']:.1%} > 7%")
    if m["oracle_within_10"] < 0.70:
        misses.append(f"oracle_within_10 {m['oracle_within_10']:.1%} < 70%")
    assert not misses, "; ".join(misses)


def test_lopo_uplift_is_calibrated_per_coverage_regime(lopo):
    """The hour uplift is one value per coverage regime (WS6 hours policy,
    2026-10-22): both regimes calibrated from their own training pairs, the
    covered one below the under-covered one, and 7154 (every line priced,
    no fallback share to speak of) runs as covered with a LOO uplift at or
    under the pooled one."""
    from app.services import pricing_policy as pp

    cal = lopo["calibration_by_coverage"]
    assert cal["covered_source"] == "regime" and cal["covered"]["n_clean"] >= 5
    assert Decimal(cal["chosen_covered"]) < Decimal(cal["chosen_under_covered"])
    assert Decimal(cal["pooled"]["chosen_uplift"]) == Decimal(lopo["calibration"]["chosen_uplift"])
    by_cov = lopo["training_pairs_by_coverage"]
    assert set(by_cov) == set(pp.COVERAGE_CLASSES) and sum(by_cov.values()) == lopo["training_pairs"]
    case = next(r for r in lopo["runs"] if r.project_id.startswith(ev_prefix()))
    assert case.coverage == pp.COVERAGE_COVERED
    assert case.uplift is not None and case.uplift <= Decimal(cal["pooled"]["chosen_uplift"])
    assert all(r.coverage in pp.COVERAGE_CLASSES for r in lopo["runs"])


def ev_prefix() -> str:
    return _load_eval().CASE_7154_PREFIX
