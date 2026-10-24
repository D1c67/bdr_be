"""scripts/plans_reader_l1_conformance.py: the WS2 L1 conformance probe
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS2 acceptance; 0.4 ledger rules).

Pinned here:
  dry run     prints the plan (both all-OpenAI routes, the static prefix
              over 1,024 tokens, the ledger reserve and expected dollars
              under the $3 budget) with no sweep and no model call;
  refusals    no --sweep, a sweep of the wrong purpose or workstream, a
              closed sweep, an unpriced model, a non-dev database, a plan
              over the budget or over what the sweep has left;
  live path   with a scripted model and the in-memory ledger: the process
              is armed (EVAL_SWEEP_ID, ledger guard, cassette record_missing),
              the one tool call is served by the fake text index, every
              check passes and results.json and table.md are written;
  checks      two tool calls, no cached tokens in round 2, a final that
              breaks the schema, a billed cost that is not llm_pricing's,
              and the hand calculation (incl. the long-context tier).
No network, no database, no model.
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
from app.services import eval_ledger, llm, llm_parts, llm_pricing

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plans_reader_l1_conformance.py"
DEV_URL = "https://bpidntbyvoooqvaispup.supabase.co"
PROD_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
ARMED = ("EVAL_SWEEP_ID", "AI_EVAL_LEDGER_ENFORCE", "LLM_CASSETTE_MODE", "LLM_CASSETTE_DIR")


def _load():
    if "plans_reader_l1_conformance" in sys.modules:
        return sys.modules["plans_reader_l1_conformance"]
    spec = importlib.util.spec_from_file_location("plans_reader_l1_conformance", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plans_reader_l1_conformance"] = mod
    spec.loader.exec_module(mod)
    return mod


l1 = _load()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def boom(*_a, **_k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("a real model call was attempted")

    monkeypatch.setattr(llm, "complete_turn", boom)
    monkeypatch.setattr(llm, "complete_parts", boom)
    saved = {k: os.environ.get(k) for k in ARMED}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    eval_ledger.use_store(None)


def _settings(**over) -> Settings:
    over.setdefault("supabase_url", DEV_URL)
    return Settings(_env_file=None, openai_api_key="test-key", **over)


def _ledger(purpose: str = "dev_probe", workstream: str = "WS2", budget: str = "3", close: bool = False) -> str:
    store = eval_ledger.MemoryLedgerStore()
    eval_ledger.use_store(store)
    row = eval_ledger.open_sweep(label="l1 test", workstream=workstream, purpose=purpose, budget_usd=Decimal(budget))
    if close:
        eval_ledger.close_sweep(sweep_id=row["id"])
    return str(row["id"])


class Scripted:
    """A fake complete_turn: round 1 makes the scripted tool calls, round 2
    answers the schema. Records every request it saw."""

    def __init__(self, *, calls: int = 1, cached: int = 1536, final: dict | None = None, bad_cost: bool = False,
                 no_tool: bool = False):
        self.calls, self.cached, self.final, self.bad_cost, self.no_tool = calls, cached, final, bad_cost, no_tool
        self.requests: list[llm_parts.ModelRequest] = []

    def __call__(self, feature, req, *, settings=None):
        assert feature == l1.FEATURE and req.step == l1.STEP and req.cache_key
        self.requests.append(req)
        model = req.route["model"]
        if req.previous_response_id is None:
            usage = llm.LlmUsage(input_tokens=1900, cached_tokens=0, output_tokens=210, reasoning_tokens=120)
            calls = [] if self.no_tool else [
                {"call_id": f"call_{i}", "name": l1.TOOL_NAME, "args": l1.expected_args()} for i in range(self.calls)
            ]
            return self._turn(model, usage, "resp_1", "tool_use" if calls else "final", calls,
                              None if calls else {"answer": "x", "citations": [], "confidence": 0.1})
        usage = llm.LlmUsage(input_tokens=2050, cached_tokens=self.cached, output_tokens=330, reasoning_tokens=200)
        final = self.final if self.final is not None else {
            "answer": l1.TARGET_LINE, "citations": ["t:a1b2c3d4:1:7"], "confidence": 0.95}
        return self._turn(model, usage, "resp_2", "final", [], final)

    def _turn(self, model, usage, rid, stop, calls, parsed):
        cost = llm_pricing.cost_for(model, usage)
        if self.bad_cost:
            cost = cost + Decimal("0.01")
        return llm_parts.ModelTurn(text=json.dumps(parsed) if parsed else "", parsed=parsed, tool_calls=calls,
                                   usage=usage, cost_usd=cost, response_id=rid, stop_reason=stop,
                                   provider="openai", model=model)


def _never(*_a, **_k):  # pragma: no cover - reaching it fails the test
    raise AssertionError("the model was called")


# -- dry run ---------------------------------------------------------------------------


def test_dry_run_prints_the_plan_with_no_sweep_and_no_call(tmp_path, capsys):
    before = {k: os.environ.get(k) for k in ARMED}
    out = tmp_path / "plan"
    code = l1.main(["--dry-run", "--eval-root", str(tmp_path), "--out", str(out)], turn_fn=_never, settings=_settings())
    assert code == 0
    printed = capsys.readouterr().out
    assert "openai:gpt-6-astra:medium | yes" in printed and "openai:gpt-5.4-mini:medium | yes" in printed
    assert "model calls made: 0" in printed and "problem:" not in printed
    plan = json.loads((out / "plan.json").read_text())
    assert plan["feature"] == "plans_recount" and plan["purpose"] == "dev_probe" and plan["workstream"] == "WS2"
    assert plan["cassette_mode"] == "record_missing" and plan["model_calls_made"] == 0
    for r in plan["routes"]:
        assert r["prefix_tokens_est"] >= l1.MIN_CACHE_PREFIX_TOKENS
        assert r["tool_hits_planned"] == 1
        assert r["rounds"][1]["input_tokens_est"] > r["rounds"][0]["input_tokens_est"]
    assert 0 < plan["expected_usd"] < plan["reserve_usd"] < 3
    assert {k: os.environ.get(k) for k in ARMED} == before  # nothing armed


def test_round_two_keeps_the_prefix_and_chains_on_round_one():
    route = l1.parse_route("openai:gpt-6-astra:medium")
    first = l1.round1_request(route)
    assert [t.name for t in first.tools] == ["text_search"] and first.response_schema == l1.FINAL_SCHEMA
    assert first.route == {"provider": "openai", "model": "gpt-6-astra", "tools": True, "effort": "medium"}
    second, res = l1._simulated_round2(route, first)
    assert not res.refused and l1.TARGET_LINE in res.text
    assert second.system == first.system and second.tools == first.tools and second.cache_key == first.cache_key
    assert second.previous_response_id == "resp_plan" and second.messages[0] == first.messages[0]
    assert isinstance(second.messages[-1], llm_parts.ToolResultsTurn)


# -- refusals --------------------------------------------------------------------------


def test_a_live_run_refuses_without_a_sweep(tmp_path, capsys):
    assert l1.main(["--eval-root", str(tmp_path)], turn_fn=_never, settings=_settings()) == 2
    assert "needs --sweep" in capsys.readouterr().err
    assert os.environ.get("AI_EVAL_LEDGER_ENFORCE") in (None, "", "false")


@pytest.mark.parametrize("purpose,workstream,close,why", [
    ("ab", "WS2", False, "purpose ab"),
    ("dev_probe", "WS8", False, "belongs to WS8"),
    ("dev_probe", "WS2", True, "not open"),
])
def test_a_live_run_refuses_the_wrong_sweep(tmp_path, capsys, purpose, workstream, close, why):
    sweep = _ledger(purpose, workstream, close=close)
    assert l1.main(["--eval-root", str(tmp_path), "--sweep", sweep], turn_fn=_never, settings=_settings()) == 2
    assert why in capsys.readouterr().err
    assert os.environ.get("EVAL_SWEEP_ID") != sweep


def test_a_live_run_refuses_an_unpriced_model_and_a_non_openai_route(tmp_path, capsys):
    sweep = _ledger()
    assert l1.main(["--eval-root", str(tmp_path), "--sweep", sweep, "--route", "openai:gpt-9-unpriced:medium"],
                   turn_fn=_never, settings=_settings()) == 2
    assert "no verified llm_pricing row" in capsys.readouterr().err
    assert l1.main(["--dry-run", "--route", "anthropic:claude-opus-5-5"], turn_fn=_never, settings=_settings()) == 2
    assert "OpenAI only" in capsys.readouterr().err


def test_a_live_run_refuses_a_database_other_than_dev(tmp_path, capsys):
    sweep = _ledger()
    assert l1.main(["--eval-root", str(tmp_path), "--sweep", sweep], turn_fn=_never,
                   settings=_settings(supabase_url=PROD_URL)) == 2
    assert "dev project" in capsys.readouterr().err


def test_a_live_run_refuses_a_plan_over_the_budget_or_the_sweep(tmp_path, capsys):
    sweep = _ledger()
    assert l1.main(["--eval-root", str(tmp_path), "--sweep", sweep, "--budget", "0.05"], turn_fn=_never,
                   settings=_settings()) == 2
    assert "exceeds the probe budget" in capsys.readouterr().err
    sweep = _ledger(budget="0.10")
    assert l1.main(["--eval-root", str(tmp_path), "--sweep", sweep], turn_fn=_never, settings=_settings()) == 2
    assert "has left" in capsys.readouterr().err


# -- live path (scripted model) ----------------------------------------------------------


def test_live_path_arms_the_sweep_serves_the_tool_and_passes(tmp_path, capsys):
    sweep = _ledger()
    model = Scripted()
    out = tmp_path / "out"
    code = l1.main(["--eval-root", str(tmp_path), "--sweep", "open", "--out", str(out), "--no-log-check"],
                   turn_fn=model, settings=_settings())
    assert code == 0, capsys.readouterr()
    assert os.environ["EVAL_SWEEP_ID"] == sweep and os.environ["AI_EVAL_LEDGER_ENFORCE"] == "true"
    assert os.environ["LLM_CASSETTE_MODE"] == "record_missing"
    assert os.environ["LLM_CASSETTE_DIR"] == str(tmp_path / "cassettes")
    assert len(model.requests) == 4  # two rounds on each of the two routes
    round2 = model.requests[1]
    results_turn = round2.messages[-1]
    assert isinstance(results_turn, llm_parts.ToolResultsTurn) and l1.TARGET_LINE in results_turn.results[0].text
    payload = json.loads((out / "results.json").read_text())
    assert payload["schema"] == l1.RESULTS_SCHEMA and payload["passed"] is True and payload["sweep_id"] == sweep
    for r in payload["results"]:
        assert all(r["checks"].values()) and r["info"]["tool_hit"] and r["info"]["answer_quotes_line"]
        assert r["info"]["answer_cites_ref"]
    table = (out / "table.md").read_text()
    assert "L1 conformance: 2 of 2 routes pass" in table and "FAIL" not in table


def _route():
    return l1.parse_route("openai:gpt-5.4-mini:medium")


def test_two_tool_calls_fail_the_one_tool_call_check_but_round_two_still_runs():
    res = l1.run_route(_route(), turn_fn=Scripted(calls=2))
    assert res.checks["one_tool_call"] is False and res.checks["cached_round2"] and res.checks["final_schema"]
    assert not res.passed and len(res.calls) == 2


def test_no_cached_tokens_in_round_two_fails():
    res = l1.run_route(_route(), turn_fn=Scripted(cached=0))
    assert res.checks["cached_round2"] is False and not res.passed


def test_a_final_that_breaks_the_schema_fails():
    res = l1.run_route(_route(), turn_fn=Scripted(final={"answer": "x", "citations": "t:1"}))
    assert res.checks["final_schema"] is False and "schema_problem" in res.info


def test_a_billed_cost_that_is_not_llm_pricing_fails_the_cost_check():
    res = l1.run_route(_route(), turn_fn=Scripted(bad_cost=True))
    assert res.checks["cost_matches"] is False and res.info["cost"]["billed_matches"] is False


def test_no_tool_call_stops_the_route_with_an_error():
    res = l1.run_route(_route(), turn_fn=Scripted(no_tool=True))
    assert res.error and "no tool call" in res.error and len(res.calls) == 1 and not res.passed


def test_a_model_error_is_recorded_not_raised():
    def broken(*_a, **_k):
        raise eval_ledger.EvalBudgetRefused("over", code="sweep_budget")

    res = l1.run_route(_route(), turn_fn=broken)
    assert res.error and "EvalBudgetRefused" in res.error and not res.passed and not res.calls


def test_a_replayed_call_is_flagged_and_its_zero_billing_is_accepted():
    class Replay(Scripted):
        def _turn(self, model, usage, rid, stop, calls, parsed):
            u = llm.CassetteUsage(**{k: getattr(usage, k) for k in ("input_tokens", "cached_tokens", "output_tokens",
                                                                     "reasoning_tokens")})
            t = super()._turn(model, u, rid, stop, calls, parsed)
            return llm_parts.ModelTurn(text=t.text, parsed=t.parsed, tool_calls=t.tool_calls, usage=u,
                                       cost_usd=Decimal("0"), response_id=rid, stop_reason=stop,
                                       provider="openai", model=model)

    res = l1.run_route(_route(), turn_fn=Replay())
    assert res.passed and all(c.replayed for c in res.calls) and res.billed_usd == 0


# -- the hand calculation ----------------------------------------------------------------


def test_hand_cost_matches_the_written_out_prices():
    usage = llm.LlmUsage(input_tokens=2000, cached_tokens=1000, output_tokens=500)
    # gpt-6-astra: 1000 x $10 + 1000 x $1 + 500 x $50, per million
    assert l1.hand_cost("gpt-6-astra", usage) == Decimal("0.036")
    assert l1.hand_cost("gpt-5.4-mini", usage) == (Decimal(1000) * Decimal("0.75") + Decimal(1000) * Decimal("0.075")
                                                    + Decimal(500) * Decimal("4.5")) / Decimal(1_000_000)
    assert l1.hand_cost("gpt-9-unpriced", usage) is None


def test_hand_cost_bills_the_whole_request_at_the_long_context_tier():
    usage = llm.LlmUsage(input_tokens=300_000, cached_tokens=0, output_tokens=1000)
    expected = (Decimal(300_000) * Decimal(10) * 2 + Decimal(1000) * Decimal(50) * Decimal("1.5")) / Decimal(1_000_000)
    assert l1.hand_cost("gpt-6-astra", usage) == expected
    assert l1.cents(expected) == l1.cents(llm_pricing.cost_for("gpt-6-astra", usage))


def test_log_check_compares_logged_rows_with_the_live_calls():
    res = l1.run_route(_route(), turn_fn=Scripted())
    rows = [{"input_tokens": c.input_tokens, "cached_tokens": c.cached_tokens, "output_tokens": c.output_tokens,
             "cost_usd": c.billed_usd} for c in res.calls]
    assert l1.log_check(rows, [res])["matches"] is True
    assert l1.log_check(rows[:1], [res])["matches"] is False
