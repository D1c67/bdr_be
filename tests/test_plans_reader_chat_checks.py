"""scripts/plans_reader_chat_checks.py: the WS8 scripted chat checks
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS8 acceptance, $3 on gpt-6-astra).

Pinned here:
  scoring     canned answers per question: counts with a sheet cite, the
              refusal with no invented number, explain_row as a pricing
              actor (pricing_restricted true) and as an engineer (false, no
              dollar figure), tools used, a failed status, an em dash, the
              never-used modify_deliverable;
  questions   the 12 defaults, params, a questions file that adds truth,
              unknown ids and bad regexes refused;
  roles       sessions fit by /users/me role and /features ai_pricing;
              --role-token reads the token from the environment;
  live path   a fake backend: preflight refusals (ledger off, no sweep,
              wrong sweep, too little left), threads created and polled,
              a role-gated question skipped without a session of its role,
              the budget stop, results.json and table.md written.
No network, no database, no model.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plans_reader_chat_checks.py"


def _load():
    if "plans_reader_chat_checks" in sys.modules:
        return sys.modules["plans_reader_chat_checks"]
    spec = importlib.util.spec_from_file_location("plans_reader_chat_checks", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plans_reader_chat_checks"] = mod
    spec.loader.exec_module(mod)
    return mod


cc = _load()
Q = {q.id: q for q in cc.default_questions()}


def _msg(content: str, tools: tuple[str, ...] = (), *, status: str = "done", restricted: bool = False, **extra):
    return {"id": "m1", "role": "assistant", "status": status, "content": content,
            "tool_calls": [{"name": t, "arguments": {}, "result_summary": "ok"} for t in tools],
            "pricing_restricted": restricted, **extra}


# -- scoring ----------------------------------------------------------------------------


def test_there_are_twelve_questions_with_the_two_role_gated_explain_rows():
    qs = cc.default_questions()
    assert len(qs) == 12 and len({q.id for q in qs}) == 12
    assert {q.id: q.role for q in qs if q.role != "any"} == {"explain_row_pricing": "pricing",
                                                              "explain_row_engineer": "engineer"}


def test_a_count_with_its_sheet_and_a_tool_passes():
    v = cc.score(Q["count_sheet"], _msg("Sheet E2.01 shows 74 duplex receptacles (D1 P12).", ("get_output",)))
    assert v["passed"], v["failures"]


@pytest.mark.parametrize("content,tools,why", [
    ("There are several duplex receptacles on the sheet.", ("get_output",), "answer lacks"),
    ("Sheet E2.01 shows 74 duplex receptacles.", (), "used none of the tools"),
    ("Sheet E2.01 shows 74 duplex receptacles " + chr(0x2014) + " see notes.", ("get_output",), "em or en dash"),
    ("Sheet E2.01 shows 74 duplex receptacles.", ("get_output", "modify_deliverable"), "used modify_deliverable"),
])
def test_a_count_answer_fails_for_each_unmet_part(content, tools, why):
    v = cc.score(Q["count_sheet"], _msg(content, tools))
    assert not v["passed"] and any(why in f for f in v["failures"])


def test_a_failed_message_fails_with_its_error():
    v = cc.score(Q["deliverable"], _msg("", status="failed", error="spend cap"))
    assert not v["passed"] and v["failures"][0] == "status failed (spend cap)"


def test_the_unanswerable_question_needs_a_refusal_and_no_invented_hours():
    good = "The run's outputs do not record site labor or crew hours, so I cannot answer that from this run."
    assert cc.score(Q["unanswerable"], _msg(good, ("get_brief",)))["passed"]
    invented = cc.score(Q["unanswerable"], _msg("The crew worked about 120 hours and installed conduit.", ()))
    assert not invented["passed"]
    assert any("does not say" in f for f in invented["failures"]) and any("hours figure" in f for f in invented["failures"])
    hedged = cc.score(Q["unanswerable"], _msg("That is not in the outputs, but probably 40 hrs.", ()))
    assert not hedged["passed"] and any("hours figure" in f for f in hedged["failures"])


def test_explain_row_as_a_pricing_actor_needs_the_restricted_tag():
    ok = cc.score(Q["explain_row_pricing"], _msg("B1: 74 EA from C3, unit $12.40 (unverified AI draft).",
                                                 ("explain_row",), restricted=True))
    assert ok["passed"] and ok["pricing_restricted"] is True
    untagged = cc.score(Q["explain_row_pricing"], _msg("B1: 74 EA from C3.", ("explain_row",)))
    assert not untagged["passed"] and "pricing_restricted false, expected true" in untagged["failures"]


def test_explain_row_as_an_engineer_has_no_price_block():
    ok = cc.score(Q["explain_row_engineer"], _msg("B1: 74 EA from census C3 on E2.01. Pricing is done by G3's "
                                                  "estimators outside this tool.", ("explain_row",)))
    assert ok["passed"], ok["failures"]
    priced = cc.score(Q["explain_row_engineer"], _msg("B1 costs $ 918.", ("explain_row",), restricted=True))
    assert not priced["passed"]
    assert any("\\$" in f for f in priced["failures"]) and any("pricing_restricted true" in f for f in priced["failures"])
    no_tool = cc.score(Q["explain_row_engineer"], _msg("B1: 74 EA.", ("get_output",)))
    assert "did not use explain_row" in no_tool["failures"]


@pytest.mark.parametrize("qid,content,tools", [
    ("legend_lookup", "The GFCI receptacle symbol is a duplex with GFI text; legend on E0.01.", ("legend_lookup",)),
    ("schedule_cell", "Type A1 is a 2x4 LED troffer, 120/277V.", ("get_schedule",)),
    ("scope_exclusion", "Fire alarm is excluded (by others), per the brief.", ("get_brief",)),
    ("measured_length", "The run measured 1,240 ft of branch circuit conduit on E2.01 and E2.02.", ("explain_row",)),
    ("linear_fixture", "About 312 LF of linear LED strip fixtures.", ("get_output",)),
    ("rfi", "RFI 1: confirm the panel location.", ("get_output",)),
    ("deliverable", "The run produced the BOQ (version 2), the estimate and the brief.", ("get_deliverable",)),
    ("spec_requirement", "Section 26 27 26 requires specification grade devices.", ("spec_lookup",)),
])
def test_good_canned_answers_pass(qid, content, tools):
    v = cc.score(Q[qid], _msg(content, tools))
    assert v["passed"], (qid, v["failures"])


def test_no_message_is_a_failure():
    assert cc.score(Q["rfi"], None) == {"passed": False, "failures": ["no assistant message"], "tools": [],
                                        "pricing_restricted": None}


# -- questions file ---------------------------------------------------------------------


def test_params_and_a_questions_file_add_the_runs_truth(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({
        "schema": cc.QUESTIONS_SCHEMA, "name": "7154 chat", "params": {"count_sheet": "E3.01", "line": "B7"},
        "questions": [{"id": "count_sheet", "expect": {"answer_all": [r"\b21\b"]}},
                      {"id": "rfi", "text": "List the RFIs.", "expect": {"replace": True, "answer_all": ["RFI"]}}],
    }))
    name, qs = cc.load_questions(path, {"line": "B9"})
    by = {q.id: q for q in qs}
    assert name == "7154 chat" and "E3.01" in by["count_sheet"].text and "B9" in by["explain_row_pricing"].text
    assert r"\b21\b" in by["count_sheet"].expect.answer_all and "E3\\.01" in by["count_sheet"].expect.answer_all
    assert by["rfi"].text == "List the RFIs." and by["rfi"].expect.answer_all == ["RFI"] and not by["rfi"].expect.tools_any
    assert not cc.score(by["count_sheet"], _msg("E3.01 shows 20 troffers.", ("get_output",)))["passed"]


@pytest.mark.parametrize("doc,why", [
    ({"schema": "nope"}, "is not"),
    ({"schema": "bdr.eval.chat_checks/1", "questions": [{"id": "zzz"}]}, "unknown question"),
    ({"schema": "bdr.eval.chat_checks/1", "params": {"bogus": "1"}}, "unknown params"),
    ({"schema": "bdr.eval.chat_checks/1", "questions": [{"id": "rfi", "expect": {"answer_all": ["("]}}]}, "bad regex"),
])
def test_a_bad_questions_file_is_refused(tmp_path, doc, why):
    path = tmp_path / "q.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(cc.ChatCheckError, match=why):
        cc.load_questions(path)


# -- roles -------------------------------------------------------------------------------


def test_sessions_fit_by_role_and_ai_pricing():
    exec_s = cc.Session("e2e", dict, role="executive", ai_pricing=True)
    eng = cc.Session("eng", dict, role="estimating_engineer_labor", ai_pricing=False)
    eng_live = cc.Session("eng live", dict, role="estimating_engineer_labor", ai_pricing=True)
    assert exec_s.fits("any") and exec_s.fits("pricing") and not exec_s.fits("engineer")
    assert eng.fits("engineer") and not eng.fits("pricing") and not eng_live.fits("engineer")
    assert cc.pick_session([exec_s, eng], "engineer") is eng and cc.pick_session([exec_s], "engineer") is None


def test_role_tokens_come_from_the_environment():
    sessions = cc.parse_role_tokens(["engineer=ENG_TOKEN"], {"ENG_TOKEN": "abc"})
    assert sessions[0].headers() == {"Authorization": "Bearer abc"}
    with pytest.raises(cc.ChatCheckError, match="is empty"):
        cc.parse_role_tokens(["engineer=MISSING"], {})
    with pytest.raises(cc.ChatCheckError, match="pricing or engineer"):
        cc.parse_role_tokens(["accountant=X"], {"X": "1"})


# -- dry run and the live path (fake backend) --------------------------------------------


def test_dry_run_prints_the_questions_and_expectations(tmp_path, capsys):
    code = cc.main(["--dry-run", "--eval-root", str(tmp_path), "--out", str(tmp_path / "o")])
    assert code == 0
    out = capsys.readouterr().out
    assert "12 questions, feature plans_chat on gpt-6-astra, budget $3.00" in out
    assert "[explain_row_engineer] role engineer" in out and "model calls made by this script: 0" in out
    assert len(json.loads((tmp_path / "o" / "questions.json").read_text())) == 12


def test_a_live_check_needs_a_run_id(tmp_path, capsys):
    assert cc.main(["--eval-root", str(tmp_path)], api=object(), sessions=[]) == 2
    assert "--run-id" in capsys.readouterr().err


class FakeBackend:
    """The chat routes in memory. `answers` maps question text to the
    assistant message the backend finishes with; it turns done on the
    second poll."""

    def __init__(self, answers: dict, *, ledger: dict | None = None, roles: dict[str, tuple] | None = None):
        self.answers, self.threads, self.polls, self.calls = answers, {}, {}, []
        self.ledger = ledger if ledger is not None else {"enforced": True, "open_sweep_id": "s-1",
                                                         "sweep_remaining": 5.0}
        self.roles = roles or {"e2e": ("executive", True)}

    def call(self, session, method, path, body=None):
        self.calls.append((session.name, method, path))
        if path == "/users/me":
            return 200, {"role": self.roles[session.name][0]}
        if path == "/features":
            return 200, {"ai_estimating": True, "ai_pricing": self.roles[session.name][1]}
        if path == "/ai-estimating/status":
            return 200, {"enabled": True, "model": "gpt-6-astra", "eval_ledger": self.ledger}
        if method == "POST" and path.endswith("/chat/threads"):
            tid = f"t{len(self.threads) + 1}"
            self.threads[tid] = None
            return 201, {"id": tid, "title": body["title"]}
        if method == "POST" and path.endswith("/messages"):
            tid = path.split("/")[-2]
            key = (session.name, body["content"])
            self.threads[tid] = key if key in self.answers else body["content"]
            return 202, {"user_message": {"id": "u"}, "assistant_message": {"id": f"a-{tid}", "status": "pending"}}
        if method == "GET" and "/chat/threads/" in path:
            tid = path.rsplit("/", 1)[-1]
            self.polls[tid] = self.polls.get(tid, 0) + 1
            msg = {"id": f"a-{tid}", "role": "assistant", "status": "running"}
            if self.polls[tid] >= 2:
                msg = {**self.answers[self.threads[tid]], "id": f"a-{tid}"}
            return 200, {"thread": {"id": tid}, "messages": [{"id": "u", "role": "user"}, msg]}
        return 404, "not found"


def _answers(cost: float = 0.05) -> dict:
    good = {
        "count_sheet": ("Sheet E2.01 shows 74 duplex receptacles.", ("get_output",), False),
        "legend_lookup": ("Legend on E0.01: GFCI receptacle.", ("legend_lookup",), False),
        "schedule_cell": ("Type A1: 2x4 troffer, 277V.", ("get_schedule",), False),
        "scope_exclusion": ("Fire alarm is by others (excluded).", ("get_brief",), False),
        "measured_length": ("Measured 400 ft on E2.01.", ("explain_row",), False),
        "linear_fixture": ("120 LF.", ("get_output",), False),
        "rfi": ("RFI 1: panel location.", ("get_output",), False),
        "deliverable": ("BOQ version 1.", ("get_deliverable",), False),
        "spec_requirement": ("Section 26 27 26.", ("spec_lookup",), False),
        "unanswerable": ("The outputs do not record crew hours, so I cannot say.", (), False),
        "explain_row_pricing": ("B1: 74 EA, unit $12.40 (AI draft).", ("explain_row",), True),
        "explain_row_engineer": ("B1: 74 EA from C3.", ("explain_row",), False),
    }
    out: dict = {}
    for q in cc.default_questions():
        content, tools, restricted = good[q.id]
        # explain_row is asked twice with the same text: the engineer's
        # answer is keyed by its session
        key = ("eng", q.text) if q.id == "explain_row_engineer" else q.text
        out[key] = {**_msg(content, tools, restricted=restricted), "cost_usd": cost, "input_tokens": 9000,
                    "output_tokens": 300}
    return out


def test_live_path_asks_scores_and_skips_the_engineer_question_without_an_engineer(tmp_path, capsys):
    answers = _answers()
    backend = FakeBackend(answers)
    sessions = [cc.Session("e2e", dict)]
    out = tmp_path / "out"
    code = cc.main(["--run-id", "r-1", "--eval-root", str(tmp_path), "--out", str(out), "--poll-seconds", "0",
                    "--only", "count_sheet,unanswerable,explain_row_pricing,explain_row_engineer"],
                   api=backend, sessions=sessions, sleep=lambda _s: None)
    assert code == 1  # the engineer question is skipped, so not every question passed
    payload = json.loads((out / "results.json").read_text())
    status = {r["id"]: r["status"] for r in payload["results"]}
    assert status == {"count_sheet": "passed", "unanswerable": "passed", "explain_row_pricing": "passed",
                      "explain_row_engineer": "skipped"}
    skipped = next(r for r in payload["results"] if r["id"] == "explain_row_engineer")
    assert "no session fits role engineer" in skipped["failures"][0]
    assert payload["backend"]["open_sweep_id"] == "s-1" and payload["spent_usd"] == pytest.approx(0.15)
    assert sum(1 for c in backend.calls if c[1] == "POST" and c[2].endswith("/messages")) == 3
    table = (out / "table.md").read_text()
    assert "passed 3, failed 0, skipped 1, error 0 of 4" in table


def test_an_engineer_session_runs_the_engineer_question(tmp_path):
    q = Q["explain_row_engineer"]
    backend = FakeBackend(_answers(), roles={"e2e": ("executive", True), "eng": ("estimating_engineer_materials", False)})
    sessions = [cc.Session("e2e", dict), cc.Session("eng", dict)]
    for s in sessions:
        cc.describe_session(backend, s)
    res = cc.run_checks([q], sessions, backend, "r-1", poll_s=0, sleep=lambda _s: None, log=lambda _l: None)
    assert res[0].status == "passed" and res[0].session == "eng" and res[0].session_role == "estimating_engineer_materials"


def test_the_budget_stops_posting_questions():
    backend = FakeBackend(_answers(cost=0.20))
    sessions = [cc.Session("e2e", dict, role="executive", ai_pricing=True)]
    qs = [Q["count_sheet"], Q["rfi"], Q["deliverable"]]
    res = cc.run_checks(qs, sessions, backend, "r-1", budget=Decimal("0.50"), per_question_cap=Decimal("0.25"),
                        poll_s=0, sleep=lambda _s: None, log=lambda _l: None)
    assert [r.status for r in res] == ["passed", "passed", "skipped"] and "budget" in res[2].failures[0]


def test_an_answer_that_never_finishes_is_an_error():
    backend = FakeBackend(_answers())
    backend.polls = {}
    ticks = iter(range(0, 10_000, 100))
    sessions = [cc.Session("e2e", dict, role="executive", ai_pricing=True)]

    def never(session, method, path, body=None):
        if method == "GET" and "/chat/threads/" in path:
            return 200, {"messages": [{"id": "a-t1", "status": "running"}]}
        return FakeBackend.call(backend, session, method, path, body)

    backend.call = never  # type: ignore[method-assign]
    res = cc.ask(backend, sessions[0], "r-1", Q["rfi"], poll_s=0, max_wait_s=250, sleep=lambda _s: None,
                 clock=lambda: next(ticks))
    assert res.status == "error" and "no answer within 250 s" in res.failures[0]


@pytest.mark.parametrize("ledger,args,why", [
    ({"enforced": False}, [], "not enforced"),
    ({"enforced": True, "open_sweep_id": None}, [], "no sweep is open"),
    ({"enforced": True, "open_sweep_id": "s-1", "sweep_remaining": 9}, ["--sweep", "s-2"], "not s-2"),
    ({"enforced": True, "open_sweep_id": "s-1", "sweep_remaining": 1.5}, [], "under the $3.00 budget"),
])
def test_preflight_refuses_a_backend_that_is_not_ready(tmp_path, capsys, ledger, args, why):
    backend = FakeBackend(_answers(), ledger=ledger)
    code = cc.main(["--run-id", "r-1", "--eval-root", str(tmp_path), *args], api=backend,
                   sessions=[cc.Session("e2e", dict)], sleep=lambda _s: None)
    assert code == 2 and why in capsys.readouterr().err
    assert not any(c[2].endswith("/messages") for c in backend.calls)
