"""scripts/scope_attrib_probe.py: the WS5 attribution fallback probe
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS5; 0.4 ledger rules 2, 3, 6).

Pinned here:
  selection  smoke and core in spec order, then the pool; holdout and
             projects without a specification PDF passed over; answer-named
             files left out; --tiers holdout refused;
  dry run    the ScopeContext is built from the backup's PDFs at $0, the
             fallback request is captured (never sent), uncertain and sent
             counts and the estimate are printed, nothing is armed;
  refusals   no --sweep, a sweep of another purpose or workstream, a prod
             database, PLANS_READER_ENABLED off, and a plan larger than the
             money left; nothing is armed and no call is made;
  live path  scripted complete_turn, in-memory ledger: the process is armed
             (record_missing), one call per project, the accepted
             attribution changes the excluded obligations against the
             code-only context, results.json and table.md are written.
No network, no database, no model.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.services import eval_ledger, llm
from tests import probe_fixtures as pf

probe = pf.load_script("scope_attrib_probe")

UNCERTAIN = "Owner furnished fire alarm panel."
SPEC_LINES = ["SECTION 01 10 00 SUMMARY OF WORK", UNCERTAIN, "Fire alarm system by others where noted."]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def boom(*_a, **_k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("a model call was attempted")

    monkeypatch.setattr(llm, "complete_turn", boom)
    monkeypatch.setattr(llm, "complete_parts", boom)
    saved = {k: os.environ.get(k) for k in pf.ARMED}
    yield
    pf.restore_env(saved)
    get_settings.cache_clear()
    eval_ledger.use_store(None)


def _backup(tmp_path):
    fb = pf.FakeBackup(tmp_path / "backup")
    for pid in (pf.P7154, pf.P7177, pf.P7188, pf.POOL1):
        fb.project(pid)
    fb.file(pf.P7154, "specification", "Project Manual.pdf", pf.make_pdf(SPEC_LINES))
    fb.file(pf.P7154, "specification", "Bid Proposal Form.pdf", pf.make_pdf(["PROPOSAL FORM"]))
    fb.file(pf.P7154, "estimate", "Estimate.pdf", pf.make_pdf(["answer"]))
    fb.file(pf.P7177, "drawing", "E1.pdf", pf.make_pdf(["E1 POWER PLAN"]))
    fb.file(pf.P7188, "specification", "Holdout Spec.pdf", pf.make_pdf(SPEC_LINES))
    fb.file(pf.POOL1, "specification", "Pool Spec.pdf", pf.make_pdf(["The owner will review the fire alarm submittals."]))
    return fb.data()


def test_selection_order_holdout_and_left_out_files(tmp_path):
    data = _backup(tmp_path)
    picks, passed = probe.pick_projects(data, n=8)
    assert [p.case for p in picks] == ["7154", pf.POOL1[:8]]
    assert all(p.project_id != pf.P7188 for p in picks)
    first = picks[0]
    assert [d["name"] for d in first.documents] == ["Project Manual.pdf"]
    assert first.documents[0]["category"] == "specifications"
    assert any("names an answer" in s for s in first.left_out)
    why = {p["case"]: p["why"] for p in passed}
    assert why["7177"] == "no specification PDF in the backup"
    assert why["7181"].startswith("not in the backup")
    picks1, _ = probe.pick_projects(data, n=1)
    assert [p.case for p in picks1] == ["7154"]


def test_dry_run_plans_at_zero_dollars_and_arms_nothing(tmp_path, capsys):
    data = _backup(tmp_path)
    before = {k: os.environ.get(k) for k in pf.ARMED}
    code = probe.main(["--dry-run", "--eval-root", str(tmp_path / "eval"), "--out", str(tmp_path / "plan")],
                      settings=pf.settings(), data=data)
    out = capsys.readouterr().out
    assert code == 0
    assert "model calls made: 0" in out
    plan = json.loads((tmp_path / "plan" / "plan.json").read_text())
    rows = {r["case"]: r for r in plan["projects"]}
    assert rows["7154"]["uncertain"] == 1 and rows["7154"]["sent"] == 1
    assert rows["7154"]["input_tokens"] > 0 and rows["7154"]["expected_usd"] > 0
    assert rows["7154"]["max_tokens"] == 32_000  # the production request, unchanged
    assert rows[pf.POOL1[:8]]["sent"] == 0 and rows[pf.POOL1[:8]]["expected_usd"] == 0
    assert plan["route"]["feature"] == "plans_scope_attrib" and plan["route"]["step"] == "scope_attrib"
    assert plan["route"]["effort"] == "low"
    assert {k: os.environ.get(k) for k in pf.ARMED} == before
    assert llm.complete_turn.__name__ == "boom"  # the capture was undone


def test_require_uncertain_passes_over_projects_without_a_call(tmp_path, capsys):
    data = _backup(tmp_path)
    code = probe.main(["--dry-run", "--require-uncertain", "--eval-root", str(tmp_path / "eval")],
                      settings=pf.settings(), data=data)
    out = capsys.readouterr().out
    assert code == 0
    assert "026b2dc5 (pool): no uncertain statement" in out
    assert "1 projects, 1 calls" in out


@pytest.mark.parametrize("case", ["no_sweep", "purpose", "workstream", "prod", "plans_off", "budget", "holdout"])
def test_live_refusals_arm_nothing(tmp_path, capsys, case):
    data = _backup(tmp_path)
    s = pf.settings()
    argv = ["--eval-root", str(tmp_path / "eval"), "--sweep", "open"]
    if case == "no_sweep":
        argv = ["--eval-root", str(tmp_path / "eval")]
        pf.open_sweep(workstream="WS5")
    elif case == "purpose":
        pf.open_sweep(workstream="WS5", purpose="ab")
    elif case == "workstream":
        pf.open_sweep(workstream="WS6")
    elif case == "prod":
        pf.open_sweep(workstream="WS5")
        s = pf.settings(supabase_url=pf.PROD_URL)
    elif case == "plans_off":
        pf.open_sweep(workstream="WS5")
        s = pf.settings(plans_reader_enabled=False)
    elif case == "budget":
        pf.open_sweep(workstream="WS5")
        argv += ["--max-usd", "0.0001"]
    else:
        argv += ["--tiers", "holdout"]
    before = {k: os.environ.get(k) for k in pf.ARMED}
    code = probe.main(argv, settings=s, data=data)
    err = capsys.readouterr().err
    assert code == 2 and "refused:" in err
    if case in ("no_sweep", "purpose", "workstream", "prod", "plans_off", "holdout"):
        assert {k: os.environ.get(k) for k in pf.ARMED} == before
    if case == "purpose":
        assert "dev_probe" in err
    if case == "workstream":
        assert "WS5" in err


def test_live_run_records_accepted_attributions_and_the_excluded_change(tmp_path, capsys, monkeypatch):
    data = _backup(tmp_path)
    sweep = pf.open_sweep(workstream="WS5")
    calls: list = []

    def answer(req):
        assert req.feature == "plans_scope_attrib" and req.step == "scope_attrib" and not req.tools
        return {"items": [{"index": 1, "party": "owner", "carry": "exclude", "systems": ["fire_alarm"],
                           "quote": "Owner furnished fire alarm panel"}]}

    monkeypatch.setattr(llm, "complete_turn", pf.scripted_turn(answer, cost="0.07", calls=calls))
    out_dir = tmp_path / "out"
    code = probe.main(["--sweep", "open", "--eval-root", str(tmp_path / "eval"), "--out", str(out_dir)],
                      settings=pf.settings(), data=data)
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert len(calls) == 1  # one capped call, only the project with an uncertain statement
    assert os.environ["EVAL_SWEEP_ID"] == str(sweep["id"])
    assert os.environ["AI_EVAL_LEDGER_ENFORCE"] == "true"
    assert os.environ["LLM_CASSETTE_MODE"] == "record_missing"
    assert os.environ["LLM_CASSETTE_DIR"] == str(tmp_path / "eval" / "cassettes")
    res = json.loads((out_dir / "results.json").read_text())
    rows = {r["case"]: r for r in res["results"]}
    r = rows["7154"]
    assert r["sent"] == 1 and r["accepted"] == 1 and r["rejected"] == 0
    assert r["cost_usd"] == pytest.approx(0.07)
    assert r["excluded_llm"] == r["excluded_code"] + 1 and r["delta"] == 1 and len(r["newly_excluded"]) == 1
    assert rows[pf.POOL1[:8]]["called"] is False
    assert res["spent_usd"] == pytest.approx(0.07)
    assert (out_dir / "table.md").read_text().startswith("| case |")


def test_a_rejected_quote_keeps_the_code_answer(tmp_path, capsys, monkeypatch):
    data = _backup(tmp_path)
    pf.open_sweep(workstream="WS5")

    def answer(_req):
        return {"items": [{"index": 1, "party": "owner", "carry": "exclude", "systems": [], "quote": "words not in it"}]}

    monkeypatch.setattr(llm, "complete_turn", pf.scripted_turn(answer))
    out_dir = tmp_path / "out"
    assert probe.main(["--sweep", "open", "--only", "7154", "--eval-root", str(tmp_path / "eval"), "--out", str(out_dir)],
                      settings=pf.settings(), data=data) == 0
    r = json.loads((out_dir / "results.json").read_text())["results"][0]
    assert r["accepted"] == 0 and r["rejected"] == 1 and r["rejected_why"] == {"quote_not_verified": 1}
    assert r["delta"] == 0


def test_excluded_change_counts_moves_both_ways():
    code = {"obligations": [{"obligation_id": "ob1", "carry": "exclude"}, {"obligation_id": "ob2", "carry": "neutral"}],
            "stats": {"verified_facts": 1}}
    after = {"obligations": [{"obligation_id": "ob1", "carry": "include"}, {"obligation_id": "ob2", "carry": "exclude"},
                             {"obligation_id": "ob3", "carry": "exclude"}], "stats": {"verified_facts": 2}}
    ch = probe.excluded_change(code, after)
    assert ch["excluded_code"] == 1 and ch["excluded_llm"] == 2 and ch["delta"] == 1
    assert ch["newly_excluded"] == ["ob2", "ob3"] and ch["no_longer_excluded"] == ["ob1"]
    assert Decimal(str(ch["verified_facts_llm"])) == 2
