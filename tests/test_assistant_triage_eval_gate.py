"""Phase 8 WI-14b: the triage evals wired into scripts/check_eval_report.py.

Offline only: the live triage report is built here by hand, the offline tiers
run against the real eval files (no network, no database)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def check():
    return _load("check_eval_report_wi14b", "scripts/check_eval_report.py")


@pytest.fixture(scope="module")
def triage_evals():
    from app.assistant.evals import run_triage_evals

    return run_triage_evals


def _pass(**over) -> dict:
    base = {"batch_size_setting": 10, "cases": 44, "graded": 44, "decision_agreement": 0.93,
            "severity_agreement": 0.9, "covered_escalations": 0, "urgent_on_not_urgent": 0,
            "invalid_answers": 0, "infrastructure_errors": {}, "gate": "pass"}
    base.update(over)
    return base


def _live(**over) -> dict:
    base = {"gate": "pass", "stopped": None, "cap_usd": 1.0, "spent_usd": 0.02, "subset": False,
            "partial": False, "passes": [_pass()], "started_at": "2099-01-01T00:00:00+00:00",
            "sources_sha256": HASH, "model": MODEL, "batch_max": 10}
    base.update(over)
    return base


HASH = "ab" * 32
MODEL = "gpt-6-luna"


def _problems(check, report, **kw):
    kw.setdefault("report_mtime", 2000.0)
    kw.setdefault("newest_source", 1000.0)
    kw.setdefault("expected_sources_hash", HASH)
    kw.setdefault("expected_model", MODEL)
    kw.setdefault("expected_batch_max", 10)
    return check.triage_report_problems(report, **kw)


def test_constants_stay_in_step_with_the_runner(check, triage_evals):
    assert check.TRIAGE_AGREEMENT_MIN == triage_evals.GATE_AGREEMENT == 0.85
    assert check.TRIAGE_TEST_CAP_USD == float(triage_evals.MAX_TEST_CAP) == 1.0
    for source in check.TRIAGE_SOURCES:
        assert source.is_file(), source


def test_a_complete_live_report_passes(check):
    assert _problems(check, _live(), expected_cases=44) == []
    assert _problems(check, _live(passes=[_pass(), _pass(batch_size_setting=5)])) == []
    # Exactly 85 percent and exactly the 1 USD cap are the floors.
    assert _problems(check, _live(spent_usd=1.0, passes=[_pass(decision_agreement=0.85)])) == []


@pytest.mark.parametrize("over, word", [
    ({"gate": "fail"}, "gate is"),
    ({"gate": "incomplete"}, "gate is"),
    ({"stopped": "cap"}, "stopped early"),
    ({"subset": True}, "subset"),
    ({"subset_ids": ["c1"]}, "subset"),
    ({"partial": True}, "partial"),
    ({"cap_usd": 2.0}, "cap_usd"),
    ({"cap_usd": 0}, "cap_usd"),
    ({"cap_usd": None}, "cap_usd"),
    ({"spent_usd": 1.5, "cap_usd": 1.0}, "spent_usd"),
    ({"spent_usd": None}, "spent_usd"),
    ({"passes": []}, "no passes"),
    ({"passes": [_pass(gate="fail")]}, "gate is"),
    ({"passes": [_pass(graded=40)]}, "graded 40 of 44"),
    ({"passes": [_pass(decision_agreement=0.84)]}, "decision agreement"),
    ({"passes": [_pass(decision_agreement=None)]}, "decision agreement"),
    ({"passes": [_pass(covered_escalations=1)]}, "covered negatives"),
    ({"passes": [_pass(urgent_on_not_urgent=2)]}, "not-urgent"),
    ({"passes": [_pass(invalid_answers=1)]}, "invalid answers"),
    ({"passes": [_pass(invalid_answers=None)]}, "invalid answers"),
    ({"passes": [_pass(infrastructure_errors={"TriageBusy": 1})]}, "infrastructure"),
    # Batch size 1 alone does not vouch for the batches of 10 production sends.
    ({"passes": [_pass(batch_size_setting=1)]}, "configured batch size 10"),
    ({"passes": [_pass(batch_size_setting=True)]}, "configured batch size 10"),
    # Model and batch maximum must be recorded and current.
    ({"model": "gpt-6-old"}, "measured model"),
    ({"model": None}, "measured model"),
    ({"batch_max": 5}, "batch_max"),
    ({"batch_max": None}, "batch_max"),
    # Freshness comes from the report itself.
    ({"started_at": None}, "no started_at"),
    ({"started_at": "not a date"}, "no started_at"),
    ({"sources_sha256": None}, "no sources_sha256"),
    ({"sources_sha256": "cd" * 32}, "does not match"),
])
def test_each_live_gate_failure_is_reported(check, over, word):
    found = _problems(check, _live(**over))
    assert found and any(word in f for f in found), (over, found)


def test_coverage_of_the_golden_set_and_freshness(check):
    assert any("golden set has 50" in f for f in _problems(check, _live(), expected_cases=50))
    assert any("written before" in f for f in _problems(check, _live(), report_mtime=500.0))
    assert any("no file time" in f for f in _problems(check, _live(), report_mtime=None))
    assert _problems(check, ["not", "an", "object"]) == ["triage report: the top level is not an object"]


def test_a_run_that_began_before_a_source_edit_fails_even_with_a_new_file_time(check):
    # The edit landed at 3000 after the run started at 2000 (ISO for 2000 is 1970); the file time is
    # the end of the run, long after the edit, so only started_at catches it.
    early = _live(started_at="2026-01-01T00:00:00+00:00")
    edit = datetime_ts("2026-01-01T00:10:00+00:00")
    found = _problems(check, early, newest_source=edit, report_mtime=edit + 3600)
    assert any("run started before" in f for f in found), found
    assert _problems(check, _live(started_at="2026-01-01T00:20:00+00:00"), newest_source=edit,
                     report_mtime=edit + 3600) == []


def datetime_ts(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


def test_a_copied_stale_report_fails(check, tmp_path, capsys):
    base, source = _base_report(tmp_path, check)
    live = tmp_path / "stale.json"
    sources_hash = check.triage_sources_hash()
    n = check.triage_golden_count()
    live.write_text(json.dumps(_live(started_at="2020-01-01T00:00:00+00:00", sources_sha256=sources_hash,
                                     passes=[_pass(cases=n, graded=n)])), encoding="utf-8")
    os.utime(live, (4_000_000_000, 4_000_000_000))   # a plain cp / touch gives a brand new file time
    assert check.main([str(base), "--source", str(source), "--triage-report", str(live)]) == 1
    assert "the run started before" in capsys.readouterr().out


def test_sources_hash_tracks_the_files(check, tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("one", encoding="utf-8")
    b.write_text("two", encoding="utf-8")
    base = check.triage_sources_hash((a, b))
    assert base == check.triage_sources_hash((a, b)) and len(base) == 64
    a.write_text("one!", encoding="utf-8")
    assert check.triage_sources_hash((a, b)) != base
    a.write_text("one", encoding="utf-8")
    b.unlink()
    assert check.triage_sources_hash((a, b)) != base
    assert len(check.triage_sources_hash()) == 64


def test_expected_settings_come_from_the_settings_object(check):
    fake = SimpleNamespace(assistant_triage_model="gpt-6-luna", assistant_triage_batch_max=10)
    assert check.triage_expected_settings(fake) == ("gpt-6-luna", 10)
    assert check.triage_expected_settings(SimpleNamespace()) == (None, None)
    assert check.triage_expected_settings(SimpleNamespace(assistant_triage_model="", assistant_triage_batch_max=True)) == (None, None)


def test_the_offline_tiers_pass_on_the_real_files(check):
    assert check.triage_offline_problems() == []
    assert check.triage_golden_count() == 63


GOOD_SUMMARY = {"cases": 44, "injection_cases": 28, "covered_negatives": 19, "time_of_day": 13, "not_urgent": 10,
                "triage_module": "compared 10 enum lists; built 44 packs"}


class _FakeModule:
    INJECTION = "injection"

    def __init__(self, *, summary=None, report=None, builder_error=None, check_error=None):
        self._summary = summary if summary is not None else dict(GOOD_SUMMARY)
        self._report = report if report is not None else {"gate": "pass", "failed": 0, "pass_rate": 1.0, "results": []}
        self._builder_error, self._check_error = builder_error, check_error

    def check(self):
        if self._check_error:
            raise self._check_error
        return self._summary

    def load_json(self, path):
        return {}

    def validate_injection(self, data):
        return []

    def load_pack_builder(self):
        if self._builder_error:
            raise self._builder_error
        return lambda *a, **k: {}

    def run_injection(self, cases, builder):
        return self._report


def test_offline_tier_failures_never_pass(check):
    assert check.triage_offline_problems(_FakeModule()) == []
    bad = {"gate": "fail", "failed": 1, "pass_rate": 0.96, "results": [{"id": "inj1", "pass": False}]}
    found = check.triage_offline_problems(_FakeModule(report=bad))
    assert found and "inj1" in found[0]
    # A missing pack builder is a failure, never a skipped tier.
    found = check.triage_offline_problems(_FakeModule(builder_error=RuntimeError("stub")))
    assert found and "did not run" in found[0] and "RuntimeError" in found[0]
    assert check.triage_offline_problems(_FakeModule(check_error=ValueError("planted text")))[0].endswith("(ValueError)")
    assert any("empty" in f for f in check.triage_offline_problems(
        _FakeModule(summary={**GOOD_SUMMARY, "cases": 0, "injection_cases": 0})))
    # A pass rate under 100 percent fails even when the gate field says pass.
    soft = {"gate": "pass", "failed": 0, "pass_rate": 0.99, "results": []}
    assert check.triage_offline_problems(_FakeModule(report=soft))


@pytest.mark.parametrize("over, word", [
    ({"covered_negatives": 14}, "covered_negatives"),
    ({"covered_negatives": 0}, "covered_negatives"),
    ({"covered_negatives": None}, "covered_negatives"),
    ({"time_of_day": 5}, "time_of_day"),
    ({"time_of_day": 0}, "time_of_day"),
    ({"not_urgent": 0}, "not_urgent"),
    ({"triage_module": "unavailable"}, "did not build packs"),
    ({"triage_module": "compared 10 enum lists; pack builder unavailable"}, "did not build packs"),
    ({"triage_module": None}, "did not build packs"),
])
def test_offline_minimums_are_enforced(check, over, word):
    found = check.triage_offline_problems(_FakeModule(summary={**GOOD_SUMMARY, **over}))
    assert any(word in f for f in found), (over, found)


def test_offline_minimums_are_exactly_the_floor(check):
    floor = {**GOOD_SUMMARY, "covered_negatives": 15, "time_of_day": 6, "not_urgent": 1}
    assert check.triage_offline_problems(_FakeModule(summary=floor)) == []


def _base_report(tmp_path, check):
    good = {"gate": "pass", "skipped": [], "started_at": "2099-01-01T00:00:00+00:00",
            "results": [{"id": f"g{i}", "kind": "golden", "grade": {"pass": True}} for i in range(10)],
            "summary": {"attack_success_rate": 0.0},
            "tool_selection": {"started_at": "2099-01-01T00:00:00+00:00", "cases": check.SELECTION_CASES, "accuracy": 0.95,
                               "skipped": 0, "skipped_cases": [], "missed": []}}
    path = tmp_path / "base.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    return path, source


def test_main_leaves_the_existing_gate_alone_without_the_flags(check, tmp_path, capsys):
    base, source = _base_report(tmp_path, check)
    assert check.main([str(base), "--source", str(source)]) == 0
    assert "triage" not in capsys.readouterr().out


def test_main_require_triage(check, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(check, "triage_expected_settings", lambda *a, **k: (MODEL, 10))
    base, source = _base_report(tmp_path, check)
    args = [str(base), "--source", str(source)]
    # Required and missing.
    assert check.main([*args, "--require-triage"]) == 1
    assert "triage report: missing" in capsys.readouterr().out
    # A good live report; sources older than the file.
    live = tmp_path / "live.json"
    n = check.triage_golden_count()
    good = _live(sources_sha256=check.triage_sources_hash(), passes=[_pass(cases=n, graded=n)])
    live.write_text(json.dumps(good), encoding="utf-8")
    os.utime(live, (4_000_000_000, 4_000_000_000))
    assert check.main([*args, "--require-triage", "--triage-report", str(live)]) == 0
    assert "PASS" in capsys.readouterr().out
    # A failing live report is reported and exits 1; supplying only the path also triggers the gate.
    live.write_text(json.dumps(_live(gate="fail", passes=[_pass(covered_escalations=2)])), encoding="utf-8")
    os.utime(live, (4_000_000_000, 4_000_000_000))
    assert check.main([*args, "--triage-report", str(live)]) == 1
    out = capsys.readouterr().out
    assert "FAIL triage report: gate is 'fail'" in out and "covered negatives" in out
    # A stale file fails on freshness.
    os.utime(live, (1_000_000_000, 1_000_000_000))
    assert check.main([*args, "--triage-report", str(live)]) == 1
    assert "written before" in capsys.readouterr().out
    # A report measured on other sources, another model or another batch maximum fails.
    live.write_text(json.dumps({**good, "sources_sha256": "cd" * 32}), encoding="utf-8")
    os.utime(live, (4_000_000_000, 4_000_000_000))
    assert check.main([*args, "--triage-report", str(live)]) == 1
    assert "sources_sha256 does not match" in capsys.readouterr().out
    monkeypatch.setattr(check, "triage_expected_settings", lambda *a, **k: ("gpt-6-other", 10))
    live.write_text(json.dumps(good), encoding="utf-8")
    os.utime(live, (4_000_000_000, 4_000_000_000))
    assert check.main([*args, "--triage-report", str(live)]) == 1
    assert "measured model" in capsys.readouterr().out
    monkeypatch.setattr(check, "triage_expected_settings", lambda *a, **k: (None, None))
    assert check.main([*args, "--triage-report", str(live)]) == 1
    assert "could not be read" in capsys.readouterr().out
    monkeypatch.setattr(check, "triage_expected_settings", lambda *a, **k: (MODEL, 10))
    # Unreadable files exit 2.
    (tmp_path / "junk.json").write_text("nope", encoding="utf-8")
    assert check.main([*args, "--triage-report", str(tmp_path / "junk.json")]) == 2
    assert check.main([*args, "--triage-report", str(tmp_path / "missing.json")]) == 2


def test_main_reports_an_offline_tier_failure(check, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(check, "triage_expected_settings", lambda *a, **k: (MODEL, 10))
    monkeypatch.setattr(check, "triage_offline_problems",
                        lambda *a, **k: ["triage offline: injection tier is not 100 percent: ['inj1']"])
    base, source = _base_report(tmp_path, check)
    n = check.triage_golden_count()
    live = tmp_path / "live_ok.json"
    live.write_text(json.dumps(_live(sources_sha256=check.triage_sources_hash(), passes=[_pass(cases=n, graded=n)])),
                    encoding="utf-8")
    os.utime(live, (4_000_000_000, 4_000_000_000))
    assert check.main([str(base), "--source", str(source), "--require-triage", "--triage-report", str(live)]) == 1
    out = capsys.readouterr().out
    assert "FAIL triage offline: injection tier is not 100 percent" in out and "PASS" not in out


def test_no_em_or_en_dash_in_the_checker(check):
    text = (ROOT / "scripts" / "check_eval_report.py").read_text(encoding="utf-8")
    assert "\u2014" not in text and "\u2013" not in text
