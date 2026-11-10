"""The 150-prompt tool-selection set (app/assistant/evals/tool_selection.json;
docs/ASSISTANT_PHASE4_PLAN.md 3.20), offline: the file validates (shape,
ids, registered and offered names, coverage), the find_tools resolver puts
an expected deferred tool in its top 6 for at least 85 percent of the
deferred prompts, the validator refuses each kind of bad prompt, and the
selection grader, summary and live mode behave (the model is a fake; no
network, no database)."""

from __future__ import annotations

import copy
import json
from collections import Counter
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.assistant import llm_messages, registry
from app.assistant.evals import run_evals as ev
from app.core.roles import INTERNAL_ROLES
from app.services import llm

DATA = ev.load_json(ev.TOOL_SELECTION)
CASES = ev.validate_tool_selection(DATA)


def _bad(mutate) -> dict:
    data = copy.deepcopy(DATA)
    mutate(data["prompts"])
    return data


# ── the file ────────────────────────────────────────────────────────────────


def test_exactly_150_prompts_with_unique_ids():
    # 169 after WI-12; browser v2 WI-19 added four (plan 18.2 evals 9, 10, 11, 15); Phase 8 WI-10 added five inbox prompts.
    assert len(CASES) == ev.SELECTION_CASES == 178
    assert len({c["id"] for c in CASES}) == 178


def test_coverage_counts():
    tools = registry.all_tools()
    expected = Counter(n for c in CASES for n in c["expected_tools"])
    for name in ev.PHASE4_TOOLS:
        assert expected[name] >= 3, name
    assert set(ev.PHASE4_TOOLS) <= set(tools)
    offered = {n for n, s in tools.items() if s.offered}
    for name in offered - set(ev.PHASE4_TOOLS):
        assert expected[name] >= 1, name
    assert sum(1 for c in CASES if not c["expected_tools"]) >= 15
    assert {c["role"] for c in CASES} == {r.value for r in INTERNAL_ROLES}
    # Every role has a fair share, not one token prompt.
    assert min(Counter(c["role"] for c in CASES).values()) >= 10


def test_the_flags_off_prompts_expect_no_tool_of_the_switched_off_module():
    tools = registry.all_tools()
    off = [c for c in CASES if c.get("flags")]
    assert sum(1 for c in off if c["flags"].get("ai_estimating") is False) >= 2
    for case in off:
        assert not case["expected_tools"], case["id"]
        offered = {s.name for s in ev.selection_toolset(case)}
        for name in case["not_tools"]:
            spec = tools[name]
            if spec.sub_app in case["flags"] and case["flags"][spec.sub_app] is False:
                assert name not in offered, (case["id"], name)


def test_every_expected_name_is_offered_to_its_role():
    for case in CASES:
        offered = {s.name for s in ev.selection_toolset(case)}
        assert set(case["expected_tools"]) | set(case["acceptable_alternates"]) <= offered, case["id"]


def test_the_resolver_finds_the_deferred_tool_for_at_least_85_percent():
    cover = ev.resolver_coverage(CASES)
    assert cover["eligible"] >= 75
    assert cover["rate"] >= ev.RESOLVER_MIN_RATE, cover["misses"]


def test_the_plan_refusals_are_in_the_set():
    by_id = {c["id"]: c for c in CASES}
    pay = by_id["neg-alex-pay"]
    assert pay["expected_tools"] == [] and "person_evidence" in pay["not_tools"]
    assert by_id["off-ae-spec"]["flags"] == {"ai_estimating": False}
    assert by_id["neg-gono-decide"]["acceptable_alternates"] == ["advance_lane", "get_project_context"]


def test_no_em_dash_and_check_reports_the_section():
    text = ev.TOOL_SELECTION.read_text(encoding="utf-8")
    assert "\u2014" not in text and "\\u2014" not in text
    summary = ev.check()["tool_selection"]
    assert summary["cases"] == 178 and summary["negative"] >= 15 and summary["flags_off"] >= 2
    assert summary["resolver"]["rate"] >= ev.RESOLVER_MIN_RATE


def test_main_check_exits_zero(capsys):
    assert ev.main(["--check"]) == 0
    assert '"tool_selection"' in capsys.readouterr().out


# ── the validator refuses ───────────────────────────────────────────────────


@pytest.mark.parametrize("mutate, word", [
    (lambda p: p.pop(), "exactly 178"),
    (lambda p: p.append(dict(p[0], id="extra-prompt")), "exactly 178"),
    (lambda p: p[1].update(id=p[0]["id"]), "unique"),
    (lambda p: p[0].update(role="estimator"), "internal role"),
    (lambda p: p[0].update(expected_tools=["no_such_tool"]), "unregistered"),
    (lambda p: p[0].update(not_tools=list(p[0]["expected_tools"])), "also expected"),
    (lambda p: p[0].update(extra="x"), "keys"),
    (lambda p: p[0].update(flags={"ai_estimating": "no"}), "flags"),
    (lambda p: p[0].update(context={"path": "/x", "other": 1}), "context"),
    (lambda p: p[0].update(message="Read this \u2014 now"), "em dash"),
])
def test_the_validator_refuses(mutate, word):
    with pytest.raises(ev.EvalFileError) as exc:
        ev.validate_tool_selection(_bad(mutate))
    assert word in str(exc.value)


def test_a_tool_the_role_does_not_get_is_refused():
    def mutate(prompts):
        case = next(c for c in prompts if c["role"] == "accountant")
        case.update(expected_tools=["person_evidence"], acceptable_alternates=[], not_tools=[])
    with pytest.raises(ev.EvalFileError, match="not offered to accountant"):
        ev.validate_tool_selection(_bad(mutate))


def test_a_plans_tool_with_ai_estimating_off_is_refused():
    def mutate(prompts):
        case = next(c for c in prompts if c["id"] == "plans-search-fire-alarm")
        case["flags"] = {"ai_estimating": False}
    with pytest.raises(ev.EvalFileError, match="not offered"):
        ev.validate_tool_selection(_bad(mutate))


def test_coverage_shortfalls_are_refused():
    def thin(prompts):
        for c in prompts:
            if c["expected_tools"] == ["ai_usage"]:
                c["expected_tools"] = ["app_search"]
                c["acceptable_alternates"] = []
    with pytest.raises(ev.EvalFileError, match="fewer than 3"):
        ev.validate_tool_selection(_bad(thin))

    def few_negatives(prompts):
        for c in prompts:
            if not c["expected_tools"] and not c.get("flags"):
                c["expected_tools"], c["acceptable_alternates"], c["not_tools"] = ["whats_due"], [], []
    with pytest.raises(ev.EvalFileError, match="negative"):
        ev.validate_tool_selection(_bad(few_negatives))

    def no_ae_off(prompts):
        for c in prompts:
            if (c.get("flags") or {}).get("ai_estimating") is False:
                c["flags"] = {"pm": False}
    with pytest.raises(ev.EvalFileError, match="AE-off"):
        ev.validate_tool_selection(_bad(no_ae_off))


# ── grading ─────────────────────────────────────────────────────────────────


def _case(expected=(), alt=(), not_tools=()):
    return {"id": "p", "expected_tools": list(expected), "acceptable_alternates": list(alt),
            "not_tools": list(not_tools)}


def test_grade_positive_prompts():
    case = _case(["plans_search_notes"], ["plans_list_runs"], ["run_ai_estimating"])
    assert ev.grade_selection(case, [["find_tools"], ["plans_search_notes"]])["hit"]
    assert ev.grade_selection(case, [["load_skill"], ["plans_list_runs"]])["hit"]
    # The right tool beside find_tools in round 1 still chose it.
    assert ev.grade_selection(case, [["plans_search_notes", "find_tools"], []])["hit"]
    assert not ev.grade_selection(case, [["find_tools"], ["app_search"]])["hit"]
    assert not ev.grade_selection(case, [[]])["hit"]
    # ENGINE-4: the decisive (last non-surfacing) round is graded, so settling
    # on the wrong tool after a right first call is a miss.
    assert not ev.grade_selection(case, [["plans_search_notes", "find_tools"], ["app_search"]])["hit"]
    assert ev.grade_selection(case, [["app_search", "find_tools"], ["plans_search_notes"]])["hit"]
    missed = ev.grade_selection(case, [["find_tools"], ["plans_search_notes", "run_ai_estimating"]])
    assert not missed["hit"] and missed["not_tools_called"] == ["run_ai_estimating"]


def test_grade_prompts_that_ask_for_a_surfacing_tool():
    """Live gate 2026-10-24: "Do you have a tool for certified payroll? Look
    it up." called find_tools and then answered; the decisive-round rule
    found no non-surfacing round and graded it a miss."""
    find_case = _case(["find_tools"], ["load_skill", "cp_week_summary"])
    assert ev.grade_selection(find_case, [["find_tools"], []])["hit"]
    assert ev.grade_selection(find_case, [["find_tools"], ["cp_week_summary"]])["hit"]
    assert not ev.grade_selection(find_case, [["find_tools"], ["app_search"]])["hit"]
    assert not ev.grade_selection(find_case, [[]])["hit"]
    skill_case = _case(["load_skill"], ["splitter_status"])
    assert ev.grade_selection(skill_case, [["load_skill"], []])["hit"]
    # A prompt that wants a real tool still misses on surfacing calls alone.
    plans = _case(["plans_get_output"], ["plans_list_runs"])
    assert not ev.grade_selection(plans, [["find_tools"], ["load_skill"]])["hit"]
    assert not ev.grade_selection(plans, [["find_tools"], []])["hit"]


def test_grade_negative_prompts():
    case = _case([], ["get_project_context"], ["person_evidence"])
    assert ev.grade_selection(case, [[]])["hit"]
    assert ev.grade_selection(case, [["find_tools"], []])["hit"]
    assert ev.grade_selection(case, [["load_skill"], ["get_project_context"]])["hit"]
    assert not ev.grade_selection(case, [["app_search"]])["hit"]
    assert not ev.grade_selection(case, [["find_tools"], ["person_evidence"]])["hit"]


def test_summary_gate():
    hit = {"id": "a", "grade": {"hit": True}}
    miss = {"id": "b", "grade": {"hit": False}}
    s = ev.selection_summary([hit] * 161 + [miss] * 17, [])
    assert (s["cases"], s["hits"], s["accuracy"], s["gate"]) == (178, 161, round(161 / 178, 4), "pass")
    assert ev.selection_summary([hit] * 160 + [miss] * 18, [])["gate"] == "fail"
    s = ev.selection_summary([hit] * 177, [{"id": "z", "reason": "budget"}])
    assert (s["skipped"], s["gate"], s["accuracy"]) == (1, "incomplete", round(177 / 178, 4))
    assert ev.selection_summary([hit] * 20, [])["gate"] == "fail"  # an --only run never passes
    assert s["missed"] == []


# ── the live mode, over a fake model ────────────────────────────────────────


class FakeModel:
    """Answers each call from a script: a list of rounds, each a list of
    (tool name, args). Records the tool names offered on every call."""

    def __init__(self, script, cost="0.01"):
        self.script = list(script)
        self.offered: list[list[str]] = []
        self.messages: list[list[dict]] = []
        self.systems: list[str] = []
        self.cost = Decimal(cost)

    def __call__(self, *, system, messages, tools, model=None, **kw):
        self.offered.append(sorted(t["name"] for t in tools or ()))
        self.messages.append(list(messages))
        self.systems.append(system)
        calls = self.script.pop(0) if self.script else []
        tool_calls = [{"call_id": f"c{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(calls)]
        items = [{"type": "function_call", "call_id": c["call_id"], "name": c["name"],
                  "arguments": json.dumps(c["arguments"])} for c in tool_calls]
        usage = llm.LlmUsage(input_tokens=9000, cached_tokens=0, output_tokens=40, reasoning_tokens=0)
        return llm_messages.MessagesResult(text="" if calls else "ok", tool_calls=tool_calls, output_items=items,
                                           usage=usage, response_id="r", model=model or "gpt-test",
                                           cost_usd=self.cost)


def _by_id(cid):
    return next(c for c in CASES if c["id"] == cid)


def test_a_find_tools_round_surfaces_the_found_definitions_for_one_more_call():
    case = _by_id("plans-search-fire-alarm")
    fake = FakeModel([[("find_tools", {"query": "plan notes fire alarm spec", "limit": 6})],
                      [("plans_search_notes", {"q": "fire alarm"})]])
    res = ev.run_selection_prompt(case, call=fake, model="gpt-test")
    assert res["grade"]["hit"] and res["grade"]["rounds"] == [["find_tools"], ["plans_search_notes"]]
    assert len(fake.offered) == 2
    deferred = {n for n, s in registry.all_tools().items() if s.deferred}
    assert not set(fake.offered[0]) & deferred
    assert "plans_search_notes" in fake.offered[1] and "find_tools" in fake.offered[0]
    # The second call carries the function call and its resolved output.
    outputs = [m for m in fake.messages[1] if m.get("type") == "function_call_output"]
    assert outputs and "plans_search_notes" in outputs[0]["output"]
    # The page context reaches the model as the data block, then the message.
    first = fake.messages[0]
    assert case["context"]["project_id"] in json.dumps(first[0]) and case["message"] in json.dumps(first[-1])
    assert res["cost_usd"] == pytest.approx(0.02)


def test_a_load_skill_round_surfaces_the_skill_tools():
    case = _by_id("timeline-last-month")
    fake = FakeModel([[("load_skill", {"name": "explaining-what-happened"})], [("project_timeline", {})]])
    res = ev.run_selection_prompt(case, call=fake, model=None)
    assert res["grade"]["hit"]
    assert {"project_timeline", "person_evidence", "estimator_timeline"} <= set(fake.offered[1])


def test_a_round_without_surfacing_is_the_only_call_and_nothing_executes(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("selection mode never runs a tool or reads the database")

    from app.assistant import executor, repo

    monkeypatch.setattr(executor.Executor, "call", boom)
    monkeypatch.setattr(repo, "_sb", boom)
    case = _by_id("due-today")
    fake = FakeModel([[("whats_due", {"scope": "today"})]])
    res = ev.run_selection_prompt(case, call=fake, model="gpt-test")
    assert res["grade"]["hit"] and len(fake.offered) == 1


def test_the_system_prompt_is_the_roles_own():
    accountant = _by_id("pm-stage-this-job")
    fake = FakeModel([[]])
    ev.run_selection_prompt(accountant, call=fake, model=None)
    assert "accountant" in fake.systems[0]
    assert not {"plans_read_pages", "advance_lane", "person_evidence"} & set(fake.offered[0])


def test_the_budget_and_errors_skip_prompts_never_grade_them():
    cases = [_by_id("due-today"), _by_id("due-overdue"), _by_id("todos-mine")]
    fake = FakeModel([[("whats_due", {})]] * 3, cost="5.00")
    section = ev.run_selection(cases, call=fake, model="m", budget_usd=8.0)
    assert section["graded"] == 2 and section["skipped_cases"] == [{"id": "todos-mine", "reason": "budget"}]
    assert section["gate"] == "incomplete" and section["cost_usd"] == pytest.approx(10.0)

    def broken(**kw):
        raise llm.LlmBadOutput("no text")
    section = ev.run_selection(cases[:1], call=broken, model="m", budget_usd=8.0)
    assert section["graded"] == 0 and section["skipped_cases"][0]["reason"] == "error: LlmBadOutput"


def test_run_select_merges_its_section_into_the_report(tmp_path, monkeypatch):
    from app.assistant import llm_messages as lm
    from app.core import config

    report = tmp_path / "report.json"
    report.write_text(json.dumps({"gate": "pass", "results": [], "started_at": "2026-10-24T00:00:00+00:00"}))
    monkeypatch.setattr(config, "get_settings",
                        lambda: SimpleNamespace(supabase_url="https://bpidntbyvoooqvaispup.supabase.co"))
    fake = FakeModel([[("whats_due", {"scope": "today"})]])
    monkeypatch.setattr(lm, "complete_messages", fake)
    code = ev.main(["--select", "--only", "due-today", "--report", str(report), "--model", "gpt-test"])
    data = json.loads(report.read_text())
    assert code == 1  # one prompt is not the 150-prompt gate
    assert data["gate"] == "pass" and data["results"] == []
    sel = data["tool_selection"]
    assert (sel["cases"], sel["hits"], sel["gate"], sel["model"]) == (1, 1, "fail", "gpt-test")


def test_run_select_refuses_prod_and_staging(tmp_path, monkeypatch):
    from app.core import config

    for ref in ev.FORBIDDEN_REFS:
        monkeypatch.setattr(config, "get_settings", lambda ref=ref: SimpleNamespace(supabase_url=f"https://{ref}.supabase.co"))
        with pytest.raises(SystemExit):
            ev.main(["--select", "--report", str(tmp_path / "r.json")])


def test_read_report_tolerates_missing_and_bad_files(tmp_path):
    assert ev.read_report(tmp_path / "none.json") == {}
    (tmp_path / "bad.json").write_text("not json")
    assert ev.read_report(tmp_path / "bad.json") == {}
    (tmp_path / "list.json").write_text("[]")
    assert ev.read_report(tmp_path / "list.json") == {}
