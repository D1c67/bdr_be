"""Golden pool tests (WS0 step 2 classifier gate, step 3 data-quality
counts, step 4 manifest). They read the production backup and EVAL_ROOT, so
they skip in CI. Counts only: no dollar figure is asserted or printed.

Classifier gate (spec WS0 step 2, 3.6): over the 62 golden estimator BOQs
that are not holdout (pool and core plus the smoke cases; the 4 holdout
cases are never read here), `classify_line` must leave 0 unresolved
within-case truth-key collisions and put at most 15% of non-accessory
lines, and at most 10% of their value, in kind `other`. The per-case
report is written to EVAL_ROOT/reports/classifier_gate.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services import eval_manifest as M
from app.services import eval_reference as R
from app.services import plans_reader_score as S

EVAL_ROOT = Path(get_settings().eval_root)
BACKUP = S.DEFAULT_BACKUP_DIR
OTHER_LINE_SHARE_MAX = 0.15
OTHER_VALUE_SHARE_MAX = 0.10

pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(
        not (BACKUP / "data" / "project_files.json").exists() or not EVAL_ROOT.exists(),
        reason="the production backup or EVAL_ROOT is not on disk",
    ),
]


@pytest.fixture(scope="module")
def data() -> R.BackupData:
    return R.BackupData(BACKUP)


@pytest.fixture(scope="module")
def gate_report(data) -> dict:
    """Per-case classifier gate over the 62 non-holdout golden BOQs."""
    cases = []
    value_totals: list[tuple[float, float]] = []
    for pid in M.GOLDEN_IDS:
        if pid in M.HOLDOUT_IDS:
            continue
        boq_file = R.pick_boq_file(data, pid)
        est_file = R.pick_estimate_file(data, pid)
        if boq_file is None:
            cases.append({"project": pid[:8], "status": "no_boq"})
            continue
        rows = S.parse_estimator_boq(data.file_path(boq_file).read_bytes())
        book = R.parse_estimate_lines(data.file_path(est_file).read_bytes()) if est_file else R.EstimateBook([], None, {}, [])
        lines, _w = R.join_boq_estimate(rows, book.lines, labor_rate=book.labor_rate)
        truth = S.truth_lines_from_reference({"quantity_lines": [ln for ln in lines if not ln["is_accessory"]]})
        classified, report = S.classify_truth(truth, S.GLOBAL_ALIASES)
        non_acc = [t for t in classified if not t.is_accessory]
        value_total = sum(t.value or 0.0 for t in non_acc)
        other = [t for t in non_acc if t.key is not None and t.key.kind == "other"]
        other_value = sum(t.value or 0.0 for t in other)
        value_totals.append((value_total, other_value))
        kinds: dict[str, int] = {}
        for t in non_acc:
            kinds[t.key.kind] = kinds.get(t.key.kind, 0) + 1
        cases.append(
            {
                "project": pid[:8],
                "tier": M.tier_of(pid),
                "status": "ok",
                "lines": len(non_acc),
                "lines_raw": sum(1 for r in rows if not r.is_accessory),
                "other_lines": len(other),
                "other_value_share": round(other_value / value_total, 4) if value_total else None,
                "collisions_merged": report.merged,
                "collisions_unit_split": report.unit_split,
                "collisions_unresolved": report.unresolved,
                "unit_split_detail": [d for d in report.detail if d["status"] == "unit_split"],
                "unresolved_detail": [d for d in report.detail if d["status"] == "unresolved"],
                "kinds": dict(sorted(kinds.items())),
                "joined": sum(1 for ln in lines if not ln["is_accessory"] and ln["est_ref"]),
            }
        )
    ok = [c for c in cases if c["status"] == "ok"]
    total_lines = sum(c["lines"] for c in ok)
    total_raw = sum(c["lines_raw"] for c in ok)
    other_lines = sum(c["other_lines"] for c in ok)
    value_cases = [c for c in ok if c["other_value_share"] is not None]
    summary = {
        "matcher_version": S.MATCHER_VERSION,
        "cases": len(cases),
        "cases_ok": len(ok),
        "lines_raw": total_raw,
        "lines_after_merge": total_lines,
        "other_lines": other_lines,
        "other_line_share": round(other_lines / total_lines, 4) if total_lines else None,
        "other_value_share": round(sum(o for _t, o in value_totals) / sum(t for t, _o in value_totals), 4) if value_totals else None,
        "other_value_share_case_max": max(c["other_value_share"] for c in value_cases) if value_cases else None,
        "other_value_share_case_mean": round(sum(c["other_value_share"] for c in value_cases) / len(value_cases), 4) if value_cases else None,
        "cases_over_value_share": [c["project"] for c in value_cases if c["other_value_share"] > OTHER_VALUE_SHARE_MAX],
        "collisions_merged": sum(c["collisions_merged"] for c in ok),
        "collisions_unit_split": sum(c["collisions_unit_split"] for c in ok),
        "cases_with_unit_split": sum(1 for c in ok if c["collisions_unit_split"]),
        "collisions_unresolved": sum(c["collisions_unresolved"] for c in ok),
        "join_rate": round(sum(c["joined"] for c in ok) / total_raw, 4) if total_raw else None,
    }
    report = {"summary": summary, "cases": cases, "written_at": R.now_pacific_iso()}
    out = EVAL_ROOT / "reports" / "classifier_gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    return report


def test_classifier_gate_counts(gate_report):
    summary = gate_report["summary"]
    assert summary["cases"] == 62 and summary["cases_ok"] == 62
    assert summary["collisions_unresolved"] == 0, [c["unresolved_detail"] for c in gate_report["cases"] if c.get("collisions_unresolved")]
    # The gate of 3.6 is the share over all 62 BOQs. Single unclassifiable
    # lines in tiny BOQs (a room remote, a bespoke assembly) push a few pool
    # cases above 10% of their own value; they are listed, not failed.
    assert summary["other_line_share"] <= OTHER_LINE_SHARE_MAX
    assert summary["other_value_share"] <= OTHER_VALUE_SHARE_MAX
    assert len(summary["cases_over_value_share"]) <= 5
    # measured on 2026-10-21 (MATCHER_VERSION 2): a change here means the
    # classifier moved; re-measure and update the numbers with the report.
    assert summary["lines_raw"] == 3545
    assert summary["other_lines"] <= 30
    assert summary["collisions_merged"] <= 200
    # different-unit collisions (same LineKey, FT vs EA vs SQ FT) are split
    # by the UNIT attr, never left invisible: 19 in 14 cases on 2026-10-21
    assert summary["collisions_unit_split"] == 19 and summary["cases_with_unit_split"] == 14
    assert summary["join_rate"] >= 0.95


def test_pool_data_quality_counts():
    counts = R.pool_data_quality(M.GOLDEN_IDS, BACKUP)
    assert counts["cases_sent_and_verified"] == 39
    assert counts["verify_sum_matches_sent"] == 37
    assert counts["materials_match_quotes"] == 39
    assert sorted(counts["failing_verify"]) == ["e70b9bdc", "f33871cc"]
    non_holdout = R.pool_data_quality([g for g in M.GOLDEN_IDS if g not in M.HOLDOUT_IDS], BACKUP)
    assert non_holdout["cases_sent_and_verified"] == 35
    assert non_holdout["verify_sum_matches_sent"] == 33


def test_manifest_on_disk_matches_spec_8_1():
    path = EVAL_ROOT / "manifest" / "manifest.json"
    if not path.exists():
        pytest.skip("manifest.json not built yet (scripts/eval_manifest_build.py)")
    manifest = M.load_manifest(EVAL_ROOT)
    assert [c.case_id for c in manifest.cases] == ["7154", "7177", "7172", "7181", "7158", "7196", "7179", "7167", "7188", "7173", "7178", "7148"]
    expected_big = {"7154": 18, "7177": 7, "7172": 25, "7181": 12, "7158": 14, "7196": 9, "7179": 19, "7167": 13, "7188": 1, "7173": 12, "7178": 14, "7148": 20}
    expected_spec = {"7154": 170, "7177": 38, "7172": 0, "7181": 182, "7158": 0, "7196": 293, "7179": 0, "7167": 0, "7188": 152, "7173": 674, "7178": 75, "7148": 0}
    for c in manifest.cases:
        assert c.big_sheets == expected_big[c.case_id], c.case_id
        assert c.spec_pages == expected_spec[c.case_id], c.case_id
        assert c.sent_truth and c.committed_verify
        for inp in c.quantity_inputs:
            assert inp.category not in M.ANSWER_CATEGORIES and inp.category != "quote"
        M.blind_guard(c, list(c.quantity_inputs), stage="quantity", answer_hashes=manifest.answer_sha256)
    ymca = manifest.case("7177")
    assert ymca.misfiled_inputs == ("2026-08-28_ITB_100028_Addendum_No._02_LVCC_Central_Hall_Airwalls.pdf",)
    assert manifest.case("7188").distinct_sent_totals == 2
    assert manifest.populations["lopo"]["n"] == 33 and sorted(manifest.populations["lopo"]["failed_reconcile"]) == ["e70b9bdc", "f33871cc"]
    assert manifest.populations["golden"]["n"] == 66
    assert len(manifest.holdout_ids) == 4


def test_references_on_disk_for_all_12_cases():
    missing = [c for c in M.CASE_PREFIX if not (EVAL_ROOT / "references" / c / "reference.json").exists()]
    if missing:
        pytest.skip(f"references not built yet for {missing} (scripts/eval_reference_build.py --all)")
    for case_id in M.CASE_PREFIX:
        ref = R.load_reference(EVAL_ROOT, case_id)
        assert ref["schema"] == R.REFERENCE_SCHEMA and ref["case_id"] == case_id
        assert ref["g3_sent"]["total"] is not None and ref["g3_verify"]["sum"] is not None
        assert ref["quantity_lines"], case_id
        statuses = {c["check"]: c["status"] for c in ref["data_quality"]}
        assert statuses["verify_sum_matches_sent"] == "pass", case_id
        assert statuses["materials_from_quotes"] == "pass", case_id
        assert statuses["boq_estimate_join_rate"] == "pass", case_id
