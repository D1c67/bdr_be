"""WS5 wave 3: the scope policy replay eval script (scripts/scope_policy_replay_eval.py).

Unit tests use synthetic main outputs; the golden test replays the stored 7154
bundle at $0 (skipped when EVAL_ROOT or the backup is not on disk).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services.eval_bundle import DEFAULT_BACKUP_DIR

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scope_policy_replay_eval.py"
EVAL_ROOT = Path(get_settings().eval_root)
BUNDLE = EVAL_ROOT / "bundles" / "7154" / "ff8fad25"


def _load():
    spec = importlib.util.spec_from_file_location("scope_policy_replay_eval", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ev = _load()


def _main(n_lines: int, allowances: int, rfis: int) -> dict:
    items = [{"description": f"ITEM {i}", "quantity": i + 1, "unit": "EA"} for i in range(n_lines)]
    return {
        "boq": {"areas": [{"name": "AREA 1", "sections": [{"name": "NEW WORK", "groups": [
            {"name": "DEVICES", "subgroups": [{"name": "WIRING DEVICES", "items": items}]}]}]}]},
        "estimate": {"extra_allowances": [{"name": f"ALLOWANCE {i}"} for i in range(allowances)]},
        "brief": {"rfis": [{"question": f"Q{i}?"} for i in range(rfis)], "exclusions": []},
    }


def test_summarize_and_aggregate_count_every_measure():
    before = _main(5, 3, 4)
    after = _main(4, 0, 2)
    after["excluded_lines"] = [{"line_id": "B5", "source": "policy", "reason": "policy_exclusion: synthetic"}]
    after["scope_policy"] = {
        "counts": {"exclusions": 2, "clarifications": 1, "needs_review": 1},
        "exclusions": [{"key": "a", "text": "Painting by others, allowance $1,234.50.", "cite": "Exhibit A p.1"},
                       {"key": "b", "text": "Temporary power excluded.", "cite": None}],
        "clarifications": [{"key": "c", "text": "Minimum branch raceway 3/4\" EMT.", "cite": "E0.01"}],
        "needs_review": [{"key": "n", "code": "raceway_conflict"}],
        "proposal_lines": [{"kind": "template", "status": "suggested"}, {"kind": "exclusion", "status": "suggested"}],
        "decided_topics": ["paint", "temporary_power"],
    }
    ctx = {"bid_conditions": {"wage_type": "prevailing_wage", "wage_basis": "document_cue",
                              "cues": [{"weight": 3}, {"weight": -3}]},
           "stats": {"statements": 10, "facts": 2, "verified_facts": 1}}
    row = ev.summarize_case(case_id="T1", run_id8="abcd1234", before=before, after=after, ctx=ctx,
                            truth_wage="prevailing_wage", idempotent=True)
    assert row["ok"] and row["before"] == {"boq_lines": 5, "extra_allowances": 3, "rfis": 4, "brief_exclusions": 0,
                                           "excluded_lines": 0}
    assert row["after"]["boq_lines"] == 4 and row["after"]["extra_allowances"] == 0 and row["after"]["excluded_lines"] == 1
    assert row["excluded_by_reason"] == {"policy_exclusion": 1} and row["excluded_by_source"] == {"policy": 1}
    assert row["needs_review"] == {"raceway_conflict": 1} and row["proposal_lines_all_suggested"]
    assert row["cited_entries"] == 2 and row["cited_share"] == pytest.approx(2 / 3, abs=1e-4)
    assert "$[amount]" in row["exclusion_texts"][0] and "1,234" not in row["exclusion_texts"][0]
    assert row["wage"]["agrees"] is True and row["wage"]["positive_cues"] == 1
    miss = ev.summarize_case(case_id="T2", run_id8="ffff0000", before=before, after=None, ctx=None, truth_wage=None,
                             idempotent=None, stage_errors=["derive: boom"])
    totals = ev.aggregate([row, miss])
    assert totals["bundles"] == 2 and totals["replayed"] == 1
    assert (totals["extra_allowances_before"], totals["extra_allowances_after"]) == (3, 0)
    assert totals["wage_agree"] == 1 and totals["wage_agreement"] == 1.0 and totals["idempotent"] == 1
    md = ev.render_markdown([row, miss], totals, {"generated_at": "2026-10-22 00:00", "policy_source": "seed",
                                                  "policy_version": 1, "policy_sha": "0" * 64, "stages": "WS5",
                                                  "settings_overrides": {}, "demo_two_line_style": False,
                                                  "enforce_flag": True})
    assert "10 -> 0" not in md and "3 -> 0" in md and chr(0x2013) not in md and chr(0x2014) not in md


def test_record_wage_and_discovery(tmp_path):
    projects = {"p1": {"wage_type": "non_prevailing_wage"}, "p2": {"wage_type": "NRS 338 above threshold"}}
    assert ev.record_wage(projects, [None, "p1"]) == "non_prevailing_wage"
    assert ev.record_wage(projects, ["p2"]) is None and ev.record_wage(projects, ["nope"]) is None
    (tmp_path / "7000" / "abcd1234").mkdir(parents=True)
    (tmp_path / "7000" / "abcd1234" / "bundle.json").write_text("{}")
    (tmp_path / "7001" / "nometa").mkdir(parents=True)
    assert ev.discover_bundles(tmp_path) == [tmp_path / "7000" / "abcd1234"]
    assert ev.discover_bundles(tmp_path / "missing") == []


@pytest.mark.golden
@pytest.mark.skipif(not (BUNDLE / "bundle.json").exists() or not (DEFAULT_BACKUP_DIR / "data" / "projects.json").exists(),
                    reason="EVAL_ROOT 7154 bundle or the production backup is not on disk")
def test_7154_replay_eval_values(tmp_path):
    from app.services import plans_reader_replay as replay

    saved = dict(replay._STAGES)
    try:
        ev.register_stages(real=False)
        row = ev.run_case(BUNDLE, backup_dir=DEFAULT_BACKUP_DIR, projects=ev.load_projects(DEFAULT_BACKUP_DIR),
                          out_root=tmp_path, expected_project=None,
                          settings_overrides={"plans_reader_demo_two_line_style": False})
    finally:
        replay._STAGES.clear()
        replay._STAGES.update(saved)
    assert row["ok"], row["stage_errors"]
    assert (row["before"]["extra_allowances"], row["after"]["extra_allowances"]) == (10, 0)
    assert row["after"]["rfis"] < row["before"]["rfis"]
    assert row["wage"]["predicted"] == "prevailing_wage" and row["wage"]["agrees"] is True
    assert row["idempotent"] is True and row["proposal_lines_all_suggested"]
    assert row["report_counts"]["clarifications"] > 0 and row["report_counts"]["exclusions"] > 0
