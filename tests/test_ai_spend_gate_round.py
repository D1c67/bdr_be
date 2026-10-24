"""The tool-loop gate (services/ai_spend.gate_round).

Pinned here:
  order       group budget, then run ceiling, then the daily cap, then the
              eval-ledger hook; the first failure wins and later checks do
              not run.
  group       GroupBudgetReached when spent + estimate passes the group's
              budget (a message safe to show); spent comes from the caller
              or from the group's (or chat message's, on the message_id
              column) plans_reader_calls rows.
  ceiling     factor x max(estimated_cost_usd_max, estimated_cost_usd);
              unknown estimates never block; chat and modify are exempt;
              RunCeilingReached is NOT a SpendCapReached, so the daily-cap
              pause path never catches it.
  ledger      a pluggable hook runs last and may raise its own error;
              None removes it.
Fake sb and monkeypatched sums; no database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import ai_spend

D = Decimal


@pytest.fixture
def no_db(monkeypatch):
    sums: dict[tuple[str, str], Decimal] = {}
    monkeypatch.setattr(ai_spend, "_scope_spent", lambda sb, column, value: sums.get((column, value), D(0)))
    calls: list[Decimal] = []
    monkeypatch.setattr(ai_spend, "check_cap", lambda sb, *, estimated_usd=0, now=None: calls.append(D(str(estimated_usd))) or D("50"))
    ai_spend.set_ledger_hook(None)
    yield sums, calls
    ai_spend.set_ledger_hook(None)


def _run(**over) -> dict:
    base = {"id": "run1", "estimated_cost_usd": 10.0, "estimated_cost_usd_max": 12.0}
    base.update(over)
    return base


def test_group_budget_refuses_and_leaves_the_run_alone(no_db):
    sums, calls = no_db
    sums[("group_id", "g1")] = D("0.70")
    ctx = ai_spend.GateContext(sb=None, kind="recount", estimated_usd=D("0.15"), run=_run(), group_id="g1", max_group_usd=D("0.80"))
    with pytest.raises(ai_spend.GroupBudgetReached) as exc:
        ai_spend.gate_round(ctx)
    assert "$0.80" in str(exc.value) and "$0.70" in str(exc.value)
    assert (exc.value.max_usd, exc.value.spent_usd, exc.value.estimated_usd) == (D("0.80"), D("0.70"), D("0.15"))
    assert not isinstance(exc.value, ai_spend.SpendCapReached)
    assert calls == []  # the daily cap never ran
    sums[("group_id", "g1")] = D("0.60")
    assert ai_spend.gate_round(ctx) == D("50")
    assert calls == [D("0.15")]


def test_group_spent_can_be_passed_or_read_from_the_message(no_db):
    sums, _ = no_db
    sums[("message_id", "m1")] = D("0.95")
    sums[("thread_id", "m1")] = D("0")  # a thread sum is never what a message budget reads
    ctx = ai_spend.GateContext(sb=None, kind="chat", estimated_usd=D("0.10"), message_id="m1", max_group_usd=D("1.00"))
    with pytest.raises(ai_spend.GroupBudgetReached):
        ai_spend.gate_round(ctx)
    ctx.group_spent_usd = D("0.50")
    ai_spend.gate_round(ctx)


def test_message_spent_sums_the_message_id_column():
    class Q:
        def __init__(self):
            self.filters = []

        def table(self, name):
            self.name = name
            return self

        def select(self, cols):
            return self

        def eq(self, col, val):
            self.filters.append((col, val))
            return self

        def order(self, col):
            return self

        def range(self, a, b):
            return self

        def execute(self):
            return type("R", (), {"data": [{"id": 1, "cost_usd": 0.25}, {"id": 2, "cost_usd": "0.05"}]})()

    sb = Q()
    assert ai_spend.message_spent_usd(sb, "m1") == D("0.30")
    assert sb.name == ai_spend.CALLS_TABLE and sb.filters == [("message_id", "m1")]


def test_run_ceiling_uses_factor_times_the_max_estimate(no_db):
    sums, calls = no_db
    assert ai_spend.run_ceiling_usd(_run(), 1.25) == D("15.00")
    assert ai_spend.run_ceiling_usd(_run(estimated_cost_usd_max=None), 1.25) == D("12.50")
    assert ai_spend.run_ceiling_usd(_run(estimated_cost_usd=None, estimated_cost_usd_max=None), 1.25) is None
    assert ai_spend.run_ceiling_usd(None) is None
    sums[("run_id", "run1")] = D("14.90")
    ctx = ai_spend.GateContext(sb=None, kind="main", estimated_usd=D("0.20"), run=_run(), group_id="g1", factor=D("1.25"))
    with pytest.raises(ai_spend.RunCeilingReached) as exc:
        ai_spend.gate_round(ctx)
    assert "$15.00" in str(exc.value) and "PLANS_READER_RUN_CEILING_FACTOR" in str(exc.value)
    assert not isinstance(exc.value, ai_spend.SpendCapReached)
    assert calls == []
    # The default factor comes from settings (1.25 in code).
    ctx.factor = None
    with pytest.raises(ai_spend.RunCeilingReached):
        ai_spend.gate_round(ctx)
    ctx.run_spent_usd = D("1.00")
    assert ai_spend.gate_round(ctx) == D("50")


@pytest.mark.parametrize("kind", ["chat", "modify"])
def test_chat_and_modify_are_exempt_from_the_ceiling(no_db, kind):
    sums, calls = no_db
    sums[("run_id", "run1")] = D("100")
    ctx = ai_spend.GateContext(sb=None, kind=kind, estimated_usd=D("1"), run=_run(), message_id="t1")
    assert ai_spend.gate_round(ctx) == D("50")
    assert calls == [D("1")]
    assert ai_spend.check_run_ceiling(run=_run(), kind=kind, spent_usd=D("100"), estimated_usd=D("1"), factor=1.25) is None


def test_unknown_estimate_never_blocks(no_db):
    sums, calls = no_db
    sums[("run_id", "run1")] = D("100")
    ctx = ai_spend.GateContext(sb=None, kind="main", estimated_usd=D("1"), run=_run(estimated_cost_usd=None, estimated_cost_usd_max=None), group_id="g1")
    assert ai_spend.gate_round(ctx) == D("50")


def test_daily_cap_then_ledger_hook_run_last(no_db, monkeypatch):
    _, calls = no_db
    seen: list[str] = []
    ai_spend.set_ledger_hook(lambda ctx: seen.append(ctx.kind))
    ctx = ai_spend.GateContext(sb=None, kind="review", estimated_usd=D("0.30"), run=_run(), group_id="g2", max_group_usd=D("1.50"))
    assert ai_spend.gate_round(ctx) == D("50")
    assert calls == [D("0.30")] and seen == ["review"]
    assert ai_spend.ledger_hook() is not None

    class Refused(RuntimeError):
        pass

    def refuse(ctx):
        raise Refused("no_open_sweep")

    ai_spend.set_ledger_hook(refuse)
    with pytest.raises(Refused):
        ai_spend.gate_round(ctx)

    def cap(sb, *, estimated_usd=0, now=None):
        raise ai_spend.SpendCapReached("cap", cap_usd=D("75"), spent_usd=D("75"))

    monkeypatch.setattr(ai_spend, "check_cap", cap)
    seen.clear()
    with pytest.raises(ai_spend.SpendCapReached):
        ai_spend.gate_round(ctx)
    assert seen == []  # the hook never ran after the cap refused
