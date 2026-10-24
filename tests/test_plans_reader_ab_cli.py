"""scripts/plans_reader_ab.py: the route A/B harness of spec 8.3
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS2 step 13, live step L2;
0.4 ledger rules 2, 3 and 6).

Pinned here:
  dry run     prints the plan (sheets resolved from the bundle, tiles, the
              expected dollars and the loop caps per route, pending tasks)
              and writes plan.json, with no sweep, no runner call and no
              model call;
  refusals    a live run refuses without --sweep (rule 2), with a sweep that
              is closed or not of purpose ab, with an unpriced model (rule
              3), against a database that is not dev, and when the summed
              loop caps exceed what the sweep has left; nothing is armed
              and nothing runs;
  live path   with a scripted runner and the in-memory ledger: the process
              is armed (EVAL_SWEEP_ID, the ledger guard, cassette record),
              routes run interleaved per task, the disagreeing task is
              repeated, results.json and table.md are written;
  comparison  the 8.3 decision (more exact tasks wins; within one task the
              cheaper per task wins), truncated tasks kept out and listed;
  parsing     routes, task validation, read-answer normalization.
No network, no database, no model, synthetic tasks and bundles only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import Settings, get_settings
from app.services import eval_ledger, llm

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plans_reader_ab.py"
DEV_URL = "https://bpidntbyvoooqvaispup.supabase.co"
PROD_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
DOC = "11111111-1111-4111-8111-111111111111"
ARMED = ("EVAL_SWEEP_ID", "AI_EVAL_LEDGER_ENFORCE", "LLM_CASSETTE_MODE", "LLM_CASSETTE_DIR")


def _load():
    if "plans_reader_ab" in sys.modules:
        return sys.modules["plans_reader_ab"]
    spec = importlib.util.spec_from_file_location("plans_reader_ab", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plans_reader_ab"] = mod
    spec.loader.exec_module(mod)
    return mod


ab = _load()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No model call may happen; the armed env and the ledger store are put
    back (and the settings cache cleared) after every test."""

    def boom(*_a, **_k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("a model call was attempted")

    monkeypatch.setattr(llm, "complete_turn", boom)
    monkeypatch.setattr(llm, "complete_parts", boom)
    saved = {k: os.environ.get(k) for k in ARMED}
    ab._BUNDLES.clear()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    eval_ledger.use_store(None)


def _settings(**over) -> Settings:
    return Settings(_env_file=None, openai_api_key="test-key", supabase_url=DEV_URL, **over)


def _bundle(root: Path) -> Path:
    b = root / "bundles" / "case1" / "run1"
    (b / "outputs").mkdir(parents=True)
    (b / "run.json").write_text(json.dumps({"id": "run-1"}))
    (b / "files.json").write_text("[]")
    (b / "groups.json").write_text("[]")
    (b / "documents.json").write_text(json.dumps([{
        "id": DOC, "category": "electrical_drawings", "name": "E set", "page_count": 3,
        "sheet_width_in": 36.0, "sheet_height_in": 24.0,
        "page_meta": [{"page": 1}, {"page": 2, "w_in": 42.0, "h_in": 30.0}, {"page": 3}],
    }]))
    (b / "outputs" / "category_electrical_drawings_v1.json").write_text(json.dumps({
        "category": "electrical_drawings",
        "pages": [{"document_id": DOC, "page": 2, "sheet_number": "E2.01"}, {"document_id": DOC, "page": 3, "sheet_number": "E0.04"}],
    }))
    return b


def _tasks(root: Path) -> Path:
    _bundle(root)
    doc = {
        "schema": ab.TASKS_SCHEMA,
        "name": "synthetic l2",
        "tasks": [
            {"id": "t-recount", "kind": "recount", "case_id": "case1", "bundle": "bundles/case1/run1", "sheet": "E2.01",
             "symbol": "duplex receptacle", "truth": {"qty": 12}, "reviewed": True},
            {"id": "t-read", "kind": "read", "case_id": "case1", "bundle": "bundles/case1/run1", "document_id": DOC,
             "page": 3, "question": "Control method of luminaire type Z9?", "truth": {"text": "LOCAL"}},
            {"id": "t-main", "kind": "main", "case_id": "case1", "run_id": "run-1", "truth": {"metric": "matched", "target": 9}},
            {"id": "t-later", "kind": "recount", "status": "pending_bundle", "note": "needs the dev bundle"},
        ],
    }
    path = root / "references" / "ab" / "recount_l2.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(doc))
    return path


ROUTES = ["--route-a", "openai:gpt-6-astra:medium", "--route-b", "openai:gpt-5.4-mini:medium"]


def _never(*_a):  # pragma: no cover - reaching it fails the test
    raise AssertionError("the runner was called")


# -- dry run ---------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_the_estimate_with_no_sweep_and_no_call(tmp_path, capsys):
    _tasks(tmp_path)
    out = tmp_path / "plan"
    code = ab.main(["--dry-run", "--eval-root", str(tmp_path), "--out", str(out), *ROUTES,
                    "--repeats-on-disagreement", "1"], runner=_never, settings=_settings())
    assert code == 0
    printed = capsys.readouterr().out
    assert "3 ready tasks, 1 pending" in printed and "pending: t-later (pending_bundle): needs the dev bundle" in printed
    assert "model calls made: 0" in printed and "NOT PRICED" not in printed
    plan = json.loads((out / "plan.json").read_text())
    rows = {r["task"]: r for r in plan["tasks"]}
    assert rows["t-recount"]["document_id"] == DOC and rows["t-recount"]["page"] == 2  # sheet resolved
    rec = rows["t-recount"]["estimates"]["A"]
    assert rec["tiles"] >= 2 and rec["input_tokens"] > 3000 and rec["cap_usd"] == 1.5
    assert 0 < rec["expected_usd"] <= 1.5
    assert rows["t-read"]["estimates"]["B"]["tiles"] == 1
    assert rows["t-main"]["estimates"]["A"] == {**rows["t-main"]["estimates"]["A"], "cap_usd": 6.0, "expected_usd": 3.0}
    assert plan["cap_usd"] == pytest.approx(1.5 * 4 + 6.0 * 2)
    assert plan["repeat_cap_usd"] == pytest.approx(plan["cap_usd"])
    assert plan["model_calls_made"] == 0 and plan["problems"] == []


def test_the_route_budget_and_the_settings_lower_the_cap(tmp_path, capsys):
    _tasks(tmp_path)
    s = _settings(plans_reader_recount_budget_usd=0.5)
    code = ab.main(["--dry-run", "--eval-root", str(tmp_path), "--only", "t-recount",
                    "--route-a", "openai:gpt-6-astra:medium:0.3", "--route-b", "openai:gpt-5.4-mini"], runner=_never, settings=s)
    assert code == 0
    printed = capsys.readouterr().out
    line = next(ln for ln in printed.splitlines() if ln.startswith("t-recount"))
    assert "$0.30" in line and "$0.50" in line


def test_an_unresolvable_sheet_is_a_plan_problem(tmp_path, capsys):
    path = _tasks(tmp_path)
    doc = json.loads(path.read_text())
    doc["tasks"][0]["sheet"] = "E9.99"
    path.write_text(json.dumps(doc))
    assert ab.main(["--dry-run", "--eval-root", str(tmp_path), *ROUTES], runner=_never, settings=_settings()) == 0
    assert "problem: t-recount" in capsys.readouterr().out


# -- refusals --------------------------------------------------------------------------


def test_a_live_run_refuses_without_a_sweep(tmp_path, capsys):
    _tasks(tmp_path)
    code = ab.main(["--eval-root", str(tmp_path), *ROUTES], runner=_never, settings=_settings())
    assert code == 2
    err = capsys.readouterr().err
    assert "needs --sweep" in err and "spec 0.4 rule 2" in err
    assert all(os.environ.get(k) in (None, "") or k != "EVAL_SWEEP_ID" for k in ARMED)


def _ledger(purpose: str = "ab", budget: str = "29", close: bool = False) -> str:
    store = eval_ledger.MemoryLedgerStore()
    eval_ledger.use_store(store)
    row = eval_ledger.open_sweep(label="l2 test", workstream="WS2", purpose=purpose, budget_usd=Decimal(budget))
    if close:
        eval_ledger.close_sweep(sweep_id=row["id"])
    return str(row["id"])


@pytest.mark.parametrize("purpose,close,why", [("dev_probe", False, "purpose dev_probe"), ("ab", True, "not open")])
def test_a_live_run_refuses_a_sweep_that_is_closed_or_not_ab(tmp_path, capsys, purpose, close, why):
    _tasks(tmp_path)
    before = os.environ.get("EVAL_SWEEP_ID")
    sweep = _ledger(purpose, close=close)
    assert ab.main(["--eval-root", str(tmp_path), "--sweep", sweep, *ROUTES], runner=_never, settings=_settings()) == 2
    assert why in capsys.readouterr().err
    assert os.environ.get("EVAL_SWEEP_ID") == before  # nothing armed


def test_a_live_run_refuses_no_open_sweep_an_unpriced_model_prod_and_an_oversized_plan(tmp_path, capsys):
    _tasks(tmp_path)
    eval_ledger.use_store(eval_ledger.MemoryLedgerStore())
    assert ab.main(["--eval-root", str(tmp_path), "--sweep", "open", *ROUTES], runner=_never, settings=_settings()) == 2
    assert "no sweep is open" in capsys.readouterr().err
    sweep = _ledger()
    unpriced = ["--route-a", "openai:gpt-6-astra", "--route-b", "openai:not-a-priced-model"]
    assert ab.main(["--eval-root", str(tmp_path), "--sweep", sweep, *unpriced], runner=_never, settings=_settings()) == 2
    assert "rule 3" in capsys.readouterr().err
    prod = Settings(_env_file=None, openai_api_key="k", supabase_url=PROD_URL)
    assert ab.main(["--eval-root", str(tmp_path), "--sweep", sweep, *ROUTES], runner=_never, settings=prod) == 2
    assert "dev project" in capsys.readouterr().err
    eval_ledger.use_store(None)
    small = _ledger(budget="5")
    assert ab.main(["--eval-root", str(tmp_path), "--sweep", small, *ROUTES], runner=_never, settings=_settings()) == 2
    assert "exceed what sweep" in capsys.readouterr().err


# -- the live path with a scripted runner ------------------------------------------------


def test_a_scripted_live_run_arms_the_sweep_repeats_disagreements_and_writes_results(tmp_path, capsys):
    _tasks(tmp_path)
    sweep = _ledger()
    calls: list[tuple[str, str, int]] = []
    answers = {("t-recount", "A"): 12, ("t-recount", "B"): 11, ("t-read", "A"): "local", ("t-read", "B"): "LOCAL (per schedule)"}

    def runner(task, route, rep):
        calls.append((task.id, route.label, rep))
        assert os.environ["EVAL_SWEEP_ID"] == sweep and os.environ["AI_EVAL_LEDGER_ENFORCE"] == "true"
        assert os.environ["LLM_CASSETTE_MODE"] == "record"
        if task.kind == "main":
            return ab.TaskResult(task=task.id, route=route.label, rep=rep, answer={"matched": 9}, exact=True,
                                 cost_usd=1.0, truncated=route.label == "B")
        answer = answers[(task.id, route.label)]
        exact, err = ab.score(task, answer)
        return ab.TaskResult(task=task.id, route=route.label, rep=rep, answer=answer, exact=exact, abs_error=err,
                             cost_usd=0.2 if route.label == "A" else 0.05)

    out = tmp_path / "out"
    code = ab.main(["--eval-root", str(tmp_path), "--sweep", sweep, "--out", str(out),
                    "--repeats-on-disagreement", "1", *ROUTES], runner=runner, settings=_settings())
    assert code == 0
    # interleaved per task, then the one disagreeing task (t-recount) repeated
    assert calls == [("t-recount", "A", 0), ("t-recount", "B", 0), ("t-read", "A", 0), ("t-read", "B", 0),
                     ("t-main", "A", 0), ("t-main", "B", 0), ("t-recount", "A", 1), ("t-recount", "B", 1)]
    payload = json.loads((out / "results.json").read_text())
    assert payload["schema"] == ab.RESULTS_SCHEMA and payload["sweep_id"] == sweep
    sm = payload["comparison"]["summary"]
    assert (sm["compared"], sm["both"], sm["only_A"], sm["only_B"]) == (2, 1, 1, 0)
    assert sm["truncated_or_failed"] == ["t-main"]
    assert payload["comparison"]["decision"]["winner"] == "B"  # within one task, B is cheaper per task
    assert "decision: B" in (out / "table.md").read_text()
    assert "t-recount" in capsys.readouterr().out


# -- comparison and parsing ---------------------------------------------------------------


def _t(i, qty=1):
    return ab.Task(id=f"t{i}", kind="recount", bundle="b", symbol="s", truth={"qty": qty})


def _r(task, label, answer, cost=0.1, truncated=False, rep=0):
    exact, err = ab.score(task, answer)
    return ab.TaskResult(task=task.id, route=label, rep=rep, answer=answer, exact=exact, abs_error=err,
                         cost_usd=cost, truncated=truncated)


def test_more_exact_tasks_win_outside_one_task_and_truncated_tasks_stay_out():
    tasks = [_t(i) for i in range(5)]
    results = []
    for t in tasks[:4]:
        results += [_r(t, "A", 1, cost=0.5), _r(t, "B", 2 if t.id != "t0" else 1, cost=0.1)]
    results += [_r(tasks[4], "A", 1, truncated=True), _r(tasks[4], "B", 1)]
    cmp = ab.compare(tasks, results)
    assert cmp["summary"]["exact_A"] == 4 and cmp["summary"]["exact_B"] == 1
    assert cmp["decision"]["winner"] == "A" and "more exact" in cmp["decision"]["reason"]
    assert cmp["summary"]["truncated_or_failed"] == ["t4"]


def test_repeats_take_the_majority_and_errors_count_as_failed():
    t = _t(1, qty=3)
    results = [_r(t, "A", 3), _r(t, "A", 4, rep=1), _r(t, "A", 3, rep=2), _r(t, "B", 3)]
    cmp = ab.compare([t], results)
    assert cmp["rows"][0]["A"]["exact"] is True and cmp["rows"][0]["A"]["exact_share"] == round(2 / 3, 4)
    failed = ab.compare([t], [_r(t, "A", 3), ab.TaskResult(task="t1", route="B", rep=0, error="boom")])
    assert failed["summary"]["truncated_or_failed"] == ["t1"] and failed["decision"]["winner"] is None


def test_run_ab_stops_a_pair_the_budget_cannot_cover():
    tasks = [_t(1), _t(2)]
    routes = [ab.Route("A", "openai", "gpt-6-astra"), ab.Route("B", "openai", "gpt-5.4-mini")]
    caps = {(t.id, r.label): Decimal("0.8") for t in tasks for r in routes}
    seen = []

    def runner(task, route, rep):
        seen.append(task.id)
        return ab.TaskResult(task=task.id, route=route.label, rep=rep, answer=1, exact=True, cost_usd=0.5)

    results, skipped = ab.run_ab(tasks, routes, runner, caps=caps, remaining_usd=Decimal("2.0"), log=lambda m: None)
    assert seen == ["t1", "t1"] and skipped == ["t2"] and len(results) == 2


def test_route_and_task_parsing():
    r = ab.parse_route("openai:gpt-6-astra:high:1.5", "A")
    assert r.as_dict() == {"provider": "openai", "model": "gpt-6-astra", "tools": True, "effort": "high", "budget_usd": 1.5}
    assert ab.parse_route('{"provider": "anthropic", "model": "claude-opus-5-5"}', "B").priced
    for bad in ("", "openai", "x:gpt", "openai:gpt-6-astra:medium:-1", "{bad json"):
        with pytest.raises(ab.AbError):
            ab.parse_route(bad, "A")
    assert ab.normalize_text("  Local control ") == "LOCAL CONTROL"
    read = ab.Task(id="r", kind="read", bundle="b", question="q", truth={"text": "LOCAL"})
    assert ab.score(read, "LOCAL (per schedule)") == (True, None) and ab.score(read, "LOCALE")[0] is False
    assert ab.score(_t(1, qty=17), "16") == (False, 1.0) and ab.score(_t(1), None) == (False, None)


def test_task_file_validation(tmp_path):
    bad = tmp_path / "t.json"
    for tasks, msg in (
        ([{"id": "a", "kind": "recount", "bundle": "b", "symbol": "s", "truth": {"qty": 1.5}}], "whole number"),
        ([{"id": "a", "kind": "read", "bundle": "b", "truth": {"text": "X"}}], "question"),
        ([{"id": "a", "kind": "nope"}], "kind"),
        ([{"id": "a", "kind": "recount", "colour": 1}], "unknown fields"),
        ([{"id": "a", "kind": "main", "status": "x"}, {"id": "a", "kind": "main", "status": "x"}], "unique"),
        ([{"id": "a", "kind": "recount", "bundle": "b", "symbol": "s", "truth": {"qty": 1}, "region": [0, 0, 2, 1]}], "region"),
    ):
        bad.write_text(json.dumps({"schema": ab.TASKS_SCHEMA, "tasks": tasks}))
        with pytest.raises(ab.AbError, match=msg):
            ab.load_tasks(bad)
    bad.write_text(json.dumps({"schema": "other", "tasks": []}))
    with pytest.raises(ab.AbError, match="not bdr.eval.ab_tasks/1"):
        ab.load_tasks(bad)
    with pytest.raises(ab.AbError, match="not found"):
        ab.load_tasks(tmp_path / "missing.json")


# -- the WS4 main seam ---------------------------------------------------------------------


def test_main_tasks_resolve_the_consolidator_ab_runner_by_name(tmp_path, monkeypatch):
    """make_runner calls plans_reader_consolidator.ab_runner(task, route,
    settings) for a main task and scores its answer by the truth metric;
    the WS4 module exports the seam with that signature and its metric
    names."""
    import importlib
    import inspect

    module = importlib.import_module(ab.MAIN_SEAM[0])
    real = getattr(module, ab.MAIN_SEAM[1])
    assert callable(real) and list(inspect.signature(real).parameters)[:3] == ["task", "route", "settings"]
    assert {"passed", "matched_lines", "refused_then_abandoned"} <= set(module.AB_METRICS)
    seen = []

    def fake(task, route, settings):
        seen.append((task.id, route.label, settings))
        return {"answer": {"passed": True, "refused_then_abandoned": 1}, "cost_usd": 0.42, "truncated": False,
                "stop_reason": "tool_stop"}

    monkeypatch.setattr(module, ab.MAIN_SEAM[1], fake)
    s = _settings()
    runner = ab.make_runner(sb=None, settings=s, eval_root=tmp_path)
    task = ab.Task(id="t-main", kind="main", run_id="run-1", truth={"metric": "passed", "target": True})
    route = ab.parse_route("openai:gpt-6-astra:medium", "A")
    res = runner(task, route, 0)
    assert seen == [("t-main", "A", s)]
    assert res.exact is True and res.cost_usd == 0.42 and res.truncated is False and res.stop_reason == "tool_stop"
    other = ab.Task(id="t-main2", kind="main", run_id="run-1", truth={"metric": "refused_then_abandoned", "target": 0})
    assert runner(other, route, 0).exact is False


def test_the_dry_run_never_reaches_the_main_seam(tmp_path, monkeypatch, capsys):
    import importlib

    module = importlib.import_module(ab.MAIN_SEAM[0])

    def boom(*_a, **_k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("ab_runner was called in a dry run")

    monkeypatch.setattr(module, ab.MAIN_SEAM[1], boom)
    _tasks(tmp_path)
    assert ab.main(["--dry-run", "--eval-root", str(tmp_path), *ROUTES], runner=_never, settings=_settings()) == 0
    assert "t-main" in capsys.readouterr().out


def _fake_seam(monkeypatch, outcome):
    """Patch the WS4 main seam with a fake taking the script's roots;
    `outcome(route_label)` gives its return value. Returns the call log."""
    import importlib

    module = importlib.import_module(ab.MAIN_SEAM[0])
    seen: list[dict] = []

    def fake(task, route, settings, *, eval_root=None, backup_dir=None):
        seen.append({"task": task.id, "route": route.label, "eval_root": eval_root, "backup_dir": backup_dir})
        return outcome(route.label)

    monkeypatch.setattr(module, ab.MAIN_SEAM[1], fake)
    return seen


def test_a_loop_failed_main_route_is_excluded_not_scored_as_a_loss(tmp_path, monkeypatch, capsys):
    """Spec 8.3: the seam reports a LoopFailed as stop_reason loop_failed
    with an error (truncated False); the runner records the error, so the
    task lands in truncated_or_failed and is never compared."""
    _tasks(tmp_path)
    sweep = _ledger()

    def outcome(label):
        if label == "A":
            return {"answer": {"matched": 2}, "cost_usd": 0.3, "truncated": False, "stop_reason": "loop_failed",
                    "error": "LoopFailed: provider 500 three times"}
        return {"answer": {"matched": 9}, "cost_usd": 0.2, "truncated": False, "stop_reason": "tool_stop", "error": None}

    _fake_seam(monkeypatch, outcome)
    out = tmp_path / "out"
    code = ab.main(["--eval-root", str(tmp_path), "--sweep", sweep, "--out", str(out), "--only", "t-main", *ROUTES],
                   settings=_settings(), sb=object())
    assert code == 0
    payload = json.loads((out / "results.json").read_text())
    by_route = {r["route"]: r for r in payload["results"]}
    assert by_route["A"]["error"] == "LoopFailed: provider 500 three times" and by_route["A"]["stop_reason"] == "loop_failed"
    assert by_route["B"]["error"] is None and by_route["B"]["exact"] is True
    sm = payload["comparison"]["summary"]
    assert sm["truncated_or_failed"] == ["t-main"] and sm["compared"] == 0
    assert payload["comparison"]["decision"]["winner"] is None
    assert "ERROR LoopFailed" in capsys.readouterr().out


def test_a_loop_failed_stop_reason_without_an_error_still_counts_as_failed(tmp_path, monkeypatch):
    _fake_seam(monkeypatch, lambda label: {"answer": {"matched": 9}, "cost_usd": 0.1, "truncated": False,
                                           "stop_reason": "loop_failed"})
    runner = ab.make_runner(sb=None, settings=_settings(), eval_root=tmp_path)
    task = ab.Task(id="t-main", kind="main", run_id="run-1", truth={"metric": "matched", "target": 9})
    res = runner(task, ab.parse_route("openai:gpt-6-astra:medium", "A"), 0)
    assert res.error == "loop_failed"
    clean = ab.TaskResult(task="t-main", route="B", rep=0, answer={"matched": 9}, exact=True)
    assert ab.compare([task], [res, clean])["summary"]["truncated_or_failed"] == ["t-main"]
    # an errored pair is never a disagreement to repeat
    assert ab._disagrees([res, ab.TaskResult(task="t-main", route="B", rep=0, answer=1, exact=False)], "t-main",
                         ["A", "B"]) is False


def test_the_script_roots_reach_the_main_seam(tmp_path, monkeypatch):
    """--eval-root and --backup reach ab_runner as eval_root and
    backup_dir; a seam without those parameters is called with three
    positionals only (test_main_tasks_resolve_the_consolidator_ab_runner_by_name)."""
    import importlib
    import inspect

    params = inspect.signature(getattr(importlib.import_module(ab.MAIN_SEAM[0]), ab.MAIN_SEAM[1])).parameters
    assert {"eval_root", "backup_dir"} <= set(params)
    _tasks(tmp_path)
    sweep = _ledger()
    seen = _fake_seam(monkeypatch, lambda label: {"answer": {"matched": 9}, "cost_usd": 0.1, "truncated": False,
                                                  "stop_reason": "tool_stop"})
    backup = tmp_path / "backup"
    code = ab.main(["--eval-root", str(tmp_path), "--sweep", sweep, "--out", str(tmp_path / "out"), "--only", "t-main",
                    "--backup", str(backup), *ROUTES], settings=_settings(), sb=object())
    assert code == 0
    assert [(c["route"], c["eval_root"], c["backup_dir"]) for c in seen] == [("A", tmp_path, backup), ("B", tmp_path, backup)]
    # without --backup only the eval root is passed (the seam keeps its default backup dir)
    seen.clear()
    runner = ab.make_runner(sb=None, settings=_settings(), eval_root=tmp_path)
    runner(ab.Task(id="t-main", kind="main", run_id="run-1", truth={"metric": "matched", "target": 9}),
           ab.parse_route("openai:gpt-6-astra:medium", "A"), 0)
    assert seen == [{"task": "t-main", "route": "A", "eval_root": tmp_path, "backup_dir": None}]


# -- a failed main route keeps the dollars it billed (sweep a064650c, 2026-10-22) --------


def test_a_failed_main_route_keeps_its_billed_cost_and_error(tmp_path, monkeypatch, capsys):
    """Route A of the first live main A/B: a cut-off tool call
    (LlmBadOutput) after five billed rounds was recorded at $0 although the
    ledger billed $0.73. The seam now reports the failure with the cost its
    loop store recorded; the result keeps both, the rounds and tool calls
    reach results.json, the spend counts in spent_usd and the task is kept
    out of the comparison."""
    _tasks(tmp_path)
    sweep = _ledger()
    error = "LlmBadOutput: Model returned a tool call with invalid arguments. Retry the generation."

    def outcome(label):
        if label == "A":
            return {"answer": {"matched": 0}, "cost_usd": 0.731661, "truncated": False, "stop_reason": "error",
                    "rounds": 5, "tool_calls": 31, "error": error, "attempts": 6, "retried": [error] * 5}
        return {"answer": {"matched": 9}, "cost_usd": 0.32302, "truncated": False, "stop_reason": "tool_stop",
                "rounds": 14, "tool_calls": 100, "error": None}

    _fake_seam(monkeypatch, outcome)
    out = tmp_path / "out"
    code = ab.main(["--eval-root", str(tmp_path), "--sweep", sweep, "--out", str(out), "--only", "t-main", *ROUTES],
                   settings=_settings(), sb=object())
    assert code == 0
    payload = json.loads((out / "results.json").read_text())
    by_route = {r["route"]: r for r in payload["results"]}
    assert by_route["A"]["cost_usd"] == 0.731661 and by_route["A"]["error"] == error
    assert by_route["A"]["rounds"] == 5 and by_route["A"]["tool_calls"] == 31 and by_route["A"]["stop_reason"] == "error"
    assert by_route["B"]["rounds"] == 14 and by_route["B"]["tool_calls"] == 100
    assert payload["spent_usd"] == pytest.approx(0.731661 + 0.32302, abs=1e-6)
    assert payload["comparison"]["summary"]["truncated_or_failed"] == ["t-main"]
    assert "cost 0.7317" in capsys.readouterr().out


def test_a_main_route_that_raises_keeps_the_cost_on_the_exception(tmp_path, monkeypatch):
    """The seam raises the spend cap and the ledger refusal with the spend
    so far on the exception (`cost_usd`); run_ab records it on the result
    (with the route's model) and takes it off the budget left, instead of
    $0."""
    import importlib

    from app.services import ai_spend

    module = importlib.import_module(ab.MAIN_SEAM[0])

    def raising(task, route, settings, *, eval_root=None, backup_dir=None):
        exc = ai_spend.SpendCapReached("The AI Estimating daily spend cap is reached.", cap_usd=Decimal("50"), spent_usd=Decimal("50"))
        exc.cost_usd = 0.4125
        raise exc

    monkeypatch.setattr(module, ab.MAIN_SEAM[1], raising)
    runner = ab.make_runner(sb=None, settings=_settings(), eval_root=tmp_path)
    task = ab.Task(id="t-main", kind="main", run_id="run-1", truth={"metric": "matched", "target": 9})
    routes = [ab.parse_route("openai:gpt-6-astra:medium", "A"), ab.parse_route("openai:gpt-5.4-mini:medium", "B")]
    logs: list[str] = []
    results, skipped = ab.run_ab([task], routes, runner, caps={("t-main", "A"): Decimal("1"), ("t-main", "B"): Decimal("1")},
                                 remaining_usd=Decimal("2.5"), repeats=0, log=logs.append)
    assert skipped == [] and [r.cost_usd for r in results] == [0.4125, 0.4125]
    assert all(r.error.startswith("SpendCapReached: ") and r.model and r.provider == "openai" for r in results)
    assert ab.exception_cost(ValueError("no spend")) == 0.0
    assert "cost 0.4125" in logs[0] and "ERROR SpendCapReached" in logs[0]
