"""Tool-loop budget rules (services/plans_reader_budgets).

Pinned here:
  kinds       recount 6 rounds / 16 calls / 8 images / $1.50, main 20
              rounds / $6.00, review 10 rounds / 10 calls / $1.50, revise
              ONE round / no tools / $1.00 (spec WS7); settings
              and route budgets only lower them; budgets key on exactly one
              of group_id / message_id.
  max_tokens  clamp((max_usd - spent - reserved_final - input_cost) /
              output_price) between the floor and the ceiling, 0 when the
              floor is unaffordable; the long-context output multiplier
              applies above the row's threshold.
  cache gate  round 1 with 0 cached tokens still allows round 2 (round 2
              is predicted from the prefix); from round 3 the gate reads
              round 2's observed ratio.
  stops       a max_tokens stop mid-loop forces the next round final with
              the remaining budget; on the final round it fails. A stray
              tool call on a final round gets one repair round; the second
              fails, and no round is planned past max_rounds + repairs.
  tolerance   a simulated loop that always spends its cap never ends more
              than 15% over budget, for every kind and both providers.
Unpriced models are refused. Synthetic prices only.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import llm_pricing
from app.services import plans_reader_budgets as b

ASTRA = "gpt-6-astra"
OPUS = "claude-opus-5-5"


def _usage(inp, cached, out):
    return SimpleNamespace(input_tokens=inp, cached_tokens=cached, output_tokens=out)


def test_kind_budgets_match_the_spec():
    rc = b.budget_for("recount")
    assert (rc.max_rounds, rc.max_tool_calls, rc.max_images, rc.max_usd) == (6, 16, 8, Decimal("1.50"))
    mn = b.budget_for("main")
    assert (mn.max_rounds, mn.max_usd, mn.min_cache_ratio) == (20, Decimal("6.00"), 0.5)
    rv = b.budget_for("review")
    assert (rv.max_rounds, rv.max_tool_calls, rv.max_usd) == (10, 10, Decimal("1.50"))
    rs = b.budget_for("revise")
    assert (rs.max_rounds, rs.max_tool_calls, rs.max_images, rs.max_usd) == (1, 0, 0, Decimal("1.00"))
    with pytest.raises(ValueError):
        b.budget_for("sub")


def test_settings_and_route_budgets_only_lower():
    s = SimpleNamespace(
        plans_reader_consolidator_max_rounds=8, plans_reader_consolidator_max_tool_calls=50,
        plans_reader_consolidator_max_usd=9.0, plans_reader_recount_budget_usd=0.5,
        plans_reader_review_budget_usd=4.0,
    )
    mn = b.budget_for("main", settings=s, route={"budget_usd": 3.0})
    assert (mn.max_rounds, mn.max_tool_calls, mn.max_usd) == (8, 50, Decimal("3.0"))
    assert b.budget_for("recount", settings=s).max_usd == Decimal("0.5")
    assert b.budget_for("review", settings=s).max_usd == Decimal("1.50")
    assert b.budget_for("revise", route=SimpleNamespace(budget_usd=None)).max_usd == Decimal("1.00")


def test_budget_key_needs_exactly_one_id():
    assert b.budget_key(group_id="g1") == "group:g1"
    assert b.budget_key(message_id="m1") == "message:m1"
    with pytest.raises(ValueError):
        b.budget_key()
    with pytest.raises(ValueError):
        b.budget_key(group_id="g", message_id="m")


def test_round_max_tokens_clamps_and_refuses_below_floor():
    row = llm_pricing.ModelPrice(input=Decimal(10), cached_input=Decimal(1), output=Decimal(50))
    # $1 budget, $0.10 spent, $0.20 reserved, 10,000 uncached input ($0.10):
    # $0.60 left / $0.00005 per token = 12,000 tokens, capped at 8,000.
    assert b.round_max_tokens(
        price=row, max_usd=Decimal("1"), spent=Decimal("0.10"), reserved_final=Decimal("0.20"),
        input_tokens=10_000, floor=400, ceiling=8_000,
    ) == 8_000
    assert b.round_max_tokens(
        price=row, max_usd=Decimal("1"), spent=Decimal("0.10"), reserved_final=Decimal("0.20"),
        input_tokens=10_000, floor=400, ceiling=20_000,
    ) == 12_000
    assert b.round_max_tokens(
        price=row, max_usd=Decimal("0.31"), spent=Decimal("0.10"), reserved_final=Decimal("0.20"),
        input_tokens=10_000, floor=400, ceiling=20_000,
    ) == 0
    long_row = llm_pricing.ModelPrice(
        input=Decimal(10), cached_input=Decimal(1), output=Decimal(50),
        long_context_threshold=1_000, long_input_multiplier=Decimal(2), long_output_multiplier=Decimal("1.5"),
    )
    # 2,000 input over the line: input $0.04, output at $75 per 1M.
    assert b.round_max_tokens(
        price=long_row, max_usd=Decimal("1"), spent=Decimal(0), reserved_final=Decimal(0),
        input_tokens=2_000, floor=400, ceiling=100_000,
    ) == int(Decimal("0.96") / (Decimal(75) / Decimal(1_000_000)))
    with pytest.raises(ValueError, match="unpriced"):
        b.round_max_tokens(price="gpt-5.3-mini", max_usd=Decimal(1), spent=Decimal(0), reserved_final=Decimal(0), input_tokens=10)


def test_round_one_with_zero_cached_tokens_still_allows_round_two():
    state = b.LoopState(budget=b.budget_for("main"), price=ASTRA, prefix_tokens=20_000)
    p1 = b.plan_round(state, new_tokens=0)
    assert (p1.round_no, p1.final, p1.cached_tokens) == (1, False, 0)
    assert p1.max_tokens >= 400 and p1.est_usd > 0
    decision = b.record_round(state, p1, usage=_usage(20_000, 0, 800), cost_usd=Decimal("0.24"), stop_reason="tool_use", tool_calls=3)
    assert decision == "continue"
    p2 = b.plan_round(state, new_tokens=3_000)
    assert p2.final is False and p2.reason == "continue"
    # Round 2 is predicted from the prefix: 20,800 / 23,800 of the input cached.
    assert p2.cached_tokens == int(p2.input_tokens * (20_800 / 23_800))


def test_cache_gate_reads_round_two_from_round_three():
    state = b.LoopState(budget=b.budget_for("main"), price=ASTRA, prefix_tokens=20_000)
    p1 = b.plan_round(state, new_tokens=0)
    b.record_round(state, p1, usage=_usage(20_000, 0, 500), cost_usd=Decimal("0.225"), stop_reason="tool_use")
    p2 = b.plan_round(state, new_tokens=2_000)
    assert p2.final is False
    # Round 2 observed: nothing cached (below the main gate of 0.5).
    b.record_round(state, p2, usage=_usage(22_500, 0, 500), cost_usd=Decimal("0.25"), stop_reason="tool_use")
    p3 = b.plan_round(state, new_tokens=2_000)
    assert p3.final is True and p3.reason == "cache_gate"
    # With a good observed ratio the loop continues.
    good = b.LoopState(budget=b.budget_for("main"), price=ASTRA, prefix_tokens=20_000)
    b.record_round(good, b.plan_round(good, new_tokens=0), usage=_usage(20_000, 0, 500), cost_usd=Decimal("0.225"), stop_reason="tool_use")
    b.record_round(good, b.plan_round(good, new_tokens=2_000), usage=_usage(22_500, 20_000, 500), cost_usd=Decimal("0.07"), stop_reason="tool_use")
    p3g = b.plan_round(good, new_tokens=2_000)
    assert p3g.final is False and p3g.cached_tokens == int(p3g.input_tokens * (20_000 / 22_500))
    assert good.latest_cache_ratio == good.observed_cache_ratio == 20_000 / 22_500
    # Kinds without a gate never finalize on the ratio.
    rc = b.LoopState(budget=b.budget_for("recount"), price=ASTRA, prefix_tokens=5_000)
    for _ in range(2):
        b.record_round(rc, b.plan_round(rc, new_tokens=500), usage=_usage(5_500, 0, 300), cost_usd=Decimal("0.07"), stop_reason="tool_use")
    assert b.plan_round(rc, new_tokens=500).reason == "continue"


def test_max_tokens_mid_loop_forces_final_and_on_final_fails():
    state = b.LoopState(budget=b.budget_for("recount"), price=ASTRA, prefix_tokens=5_000)
    p1 = b.plan_round(state, new_tokens=0)
    assert b.record_round(state, p1, usage=_usage(5_000, 0, p1.max_tokens), cost_usd=Decimal("0.08"), stop_reason="max_tokens") == "forced_final"
    p2 = b.plan_round(state, new_tokens=1_000)
    assert p2.final is True and p2.reason == "forced"
    assert p2.max_tokens >= b.budget_for("recount").final_output_tokens
    assert p2.reserved_final_usd == 0
    assert b.record_round(state, p2, usage=_usage(8_000, 4_000, p2.max_tokens), cost_usd=Decimal("0.3"), stop_reason="max_tokens") == "failed"


def test_max_rounds_and_tool_call_caps_force_final():
    budget = b.budget_for("recount")
    state = b.LoopState(budget=budget, price=ASTRA, prefix_tokens=2_000)
    for r in range(1, budget.max_rounds):
        plan = b.plan_round(state, new_tokens=200)
        assert plan.final is False, r
        b.record_round(state, plan, usage=_usage(plan.input_tokens, plan.cached_tokens, 200), cost_usd=Decimal("0.01"), stop_reason="tool_use", tool_calls=1)
    last = b.plan_round(state, new_tokens=200)
    assert last.final is True and last.reason == "max_rounds"
    calls = b.LoopState(budget=budget, price=ASTRA, prefix_tokens=2_000)
    plan = b.plan_round(calls, new_tokens=0)
    assert b.record_round(calls, plan, usage=_usage(2_000, 0, 200), cost_usd=Decimal("0.01"), stop_reason="tool_use", tool_calls=16) == "forced_final"
    # A tool call on the planned final round gets one bounded extra round.
    fin = b.LoopState(budget=budget, price=ASTRA, prefix_tokens=2_000, force_final=True)
    plan = b.plan_round(fin, new_tokens=0)
    assert plan.final is True
    assert b.record_round(fin, plan, usage=_usage(2_000, 0, 50), cost_usd=Decimal("0.01"), stop_reason="tool_use") == "forced_final"
    assert fin.repairs == 1


def test_a_second_stray_tool_call_on_a_final_round_fails():
    budget = b.budget_for("recount")
    state = b.LoopState(budget=budget, price=ASTRA, prefix_tokens=2_000, force_final=True)
    decisions = []
    for _ in range(12):
        plan = b.plan_round(state, new_tokens=0)
        if not plan.affordable:
            break
        decision = b.record_round(state, plan, usage=_usage(2_000, 0, 50), cost_usd=Decimal("0.01"), stop_reason="tool_use")
        decisions.append(decision)
        if decision == "failed":
            break
    assert decisions == ["forced_final", "failed"]
    assert state.repairs == 2 and len(state.rounds) == 2


def test_no_round_is_planned_past_max_rounds_plus_the_repair_round():
    budget = b.budget_for("recount")
    state = b.LoopState(budget=budget, price=ASTRA, prefix_tokens=2_000)
    for _ in range(budget.max_rounds + budget.max_repair_rounds):
        plan = b.plan_round(state, new_tokens=100)
        assert plan.affordable
        state.rounds.append(b.RoundRecord(plan.round_no, plan.input_tokens, plan.cached_tokens, 100, Decimal("0.01"), "tool_use", plan.final))
    over = b.plan_round(state, new_tokens=100)
    assert (over.affordable, over.final, over.reason, over.est_usd) == (False, True, "max_rounds", Decimal("0"))


def _simulate(kind: str, price: str, provider: str, *, new_tokens: int, prefix: int, cached_actual: float | None = None) -> b.LoopState:
    """A loop whose model always spends every output token it is allowed
    and whose inputs match the plan; `cached_actual` overrides the cache
    share the provider really delivers (None = as predicted)."""
    state = b.LoopState(budget=b.budget_for(kind), price=price, provider=provider, prefix_tokens=prefix)
    for _ in range(40):
        plan = b.plan_round(state, new_tokens=new_tokens)
        if not plan.affordable:
            break
        cached = plan.cached_tokens if cached_actual is None else int(plan.input_tokens * cached_actual)
        usage = _usage(plan.input_tokens, cached, plan.max_tokens)
        cost = b.estimate_round(price, input_tokens=plan.input_tokens, cached_tokens=cached, output_tokens=plan.max_tokens, cache_write_tokens=plan.cache_write_tokens)
        decision = b.record_round(state, plan, usage=usage, cost_usd=cost, stop_reason="final" if plan.final else "tool_use", tool_calls=2)
        if decision in ("done", "failed"):
            break
    return state


@pytest.mark.parametrize("kind", ["recount", "main", "review", "revise"])
@pytest.mark.parametrize("price,provider", [(ASTRA, "openai"), (OPUS, "anthropic")])
@pytest.mark.parametrize("cached_actual", [None, 0.0])
def test_no_group_over_budget_by_more_than_15_percent(kind, price, provider, cached_actual):
    prefix = {"recount": 8_000, "main": 60_000, "review": 30_000, "revise": 20_000}[kind]
    state = _simulate(kind, price, provider, new_tokens=4_000, prefix=prefix, cached_actual=cached_actual)
    assert state.rounds, "the loop never ran"
    assert b.within_tolerance(state.spent, state.budget.max_usd), (kind, price, cached_actual, state.spent)
    # The loop ends with a final answer, at max_rounds, or on a budget stop
    # (the next plan is unaffordable); it never keeps spending past that.
    ended = state.rounds[-1].final or len(state.rounds) == state.budget.max_rounds
    assert ended or not b.plan_round(state, new_tokens=4_000).affordable


def test_big_prefix_ends_at_once_when_a_final_cannot_follow():
    # A 230k-token recount context cannot afford a tool round plus a final
    # inside $0.80: the first round is already the final one.
    state = b.LoopState(budget=b.budget_for("recount"), price=ASTRA, prefix_tokens=230_000)
    plan = b.plan_round(state, new_tokens=0)
    assert plan.final is True and plan.reason == "budget"
    assert plan.max_tokens == 0 or plan.est_usd <= state.budget.max_usd


def test_input_limit_forces_final_on_openai_only():
    state = b.LoopState(budget=b.budget_for("main"), price=ASTRA, prefix_tokens=250_000)
    assert b.plan_round(state, new_tokens=0).reason == "input_limit"
    ant = b.LoopState(budget=b.budget_for("main"), price=OPUS, provider="anthropic", prefix_tokens=250_000)
    assert ant.budget.max_usd == Decimal("6.00")
    assert b.plan_round(ant, new_tokens=0).reason in ("continue", "budget")


def test_tolerance_helpers():
    assert b.over_budget_ratio(Decimal("1.10"), Decimal("1")) == pytest.approx(0.10)
    assert b.within_tolerance(Decimal("1.15"), Decimal("1")) is True
    assert b.within_tolerance(Decimal("1.16"), Decimal("1")) is False
    assert b.tokens_for_chars(10) == 4


def test_review_and_revise_budgets_follow_ws7_and_the_reviewer_agrees():
    from app.services import plans_reader_reviewer as reviewer

    rv = b.budget_for("review")
    rs = b.budget_for("revise")
    assert reviewer.review_budget() == rv and reviewer.revise_budget() == rs
    # One round, no tools: the first plan is already the final one.
    state = b.LoopState(budget=rs, price=ASTRA, prefix_tokens=20_000)
    plan = b.plan_round(state, new_tokens=0)
    assert plan.final is True and plan.reason == "max_rounds" and plan.affordable
