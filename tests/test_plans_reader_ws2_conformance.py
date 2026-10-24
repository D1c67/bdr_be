"""WS2 conformance of the spine's run_group dispatch
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS2 step 12, 4.2, 4.3; read
against services/plans_reader_agents.py as the wave 3 spine left it).

What WS2 needs from the dispatch, pinned with the spine suite's fakes:
- each loop kind gets the tools of its mode: review read plus review (never
  a write tool), revise and main everything of their kind, sub and
  escalation none; offer order sorted by name;
- the LoopContext carries the kind's route step, the snapshotted route,
  the response schema (main in tools mode answers through `finish`, no
  schema), the loop budget lowered by the settings and the route, the
  queue lease heartbeat, the call timeout and a durable loop store keyed by
  the group;
- pricing tools reach agent loops only with AI_PRICING_AGENT_TOOLS_ENABLED;
- review kinds do not run on a run that is not done (they stay queued) and
  the daily-cap pre-check uses the loop budget, not a single-call estimate.
No network, no database, no model.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import ai_spend
from app.services import plans_reader_agents as agents
from app.services import plans_reader_runs as runs
from app.services import plans_reader_tool_handlers  # noqa: F401  (registers zoom_region before the fixture swaps the registry)
from app.services import plans_reader_tools as tools
from app.services.plans_reader_review_schemas import REVIEW_SCHEMA, REVISE_SCHEMA
from tests.test_plans_reader_runs import make_settings, seed_document, set_status
from tests.test_plans_reader_spine import (  # noqa: F401
    SPINE_ON,
    _done_run,
    _reading_run,
    _review_group,
    _run,
    _sub,
    env,
    review_seam,
)


def _schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": [], "properties": {}}


def _h(ctx, args):
    return tools.ToolResult.ok({"ok": True})


@pytest.fixture
def mode_tools():
    saved = dict(tools.REGISTRY)
    tools.REGISTRY.clear()
    every = tools.kinds_from_codes("RC, M, RV, RS")
    tools.register(tools.ToolSpec("z_read", "Read.", _schema(), {}, "read", every, _h))
    tools.register(tools.ToolSpec("a_read", "Read too.", _schema(), {}, "read", every, _h))
    tools.register(tools.ToolSpec("note_finding", "Review.", _schema(), {}, "review", frozenset({"review"}), _h))
    tools.register(tools.ToolSpec("write_line", "Write.", _schema(), {}, "write", tools.kinds_from_codes("M, RS"), _h))
    tools.register(tools.ToolSpec("price_it", "Price.", _schema(), {}, "read", every, _h, internal_pricing=True))
    yield
    tools.REGISTRY.clear()
    tools.REGISTRY.update(saved)


def _capture(monkeypatch) -> dict:
    from app.services import plans_reader_toolloop as toolloop

    seen: dict = {}

    def run_loop(ctx):
        seen["ctx"] = ctx
        if ctx.kind == "main":
            # F7: a LoopFailed on main now falls back to code, so a main
            # capture stops with a plain error that propagates.
            raise RuntimeError("stop after capture")
        raise toolloop.LoopFailed("stop after capture", reason="failed")

    monkeypatch.setattr(toolloop, "run_loop", run_loop)
    return seen


def _main_group(env, **settings):  # noqa: F811
    env.settings = make_settings(**SPINE_ON, plans_reader_consolidator_mode="tools", **settings)
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    run = set_status(env, run, "consolidating")
    snapshot = dict(run.get("settings_snapshot") or {})
    snapshot["routes"] = {"main": {"provider": "openai", "model": "gpt-6-astra", "effort": "medium",
                                   "budget_usd": 4.5, "tools": True}}
    run = set_status(env, run, "consolidating", settings_snapshot=snapshot)
    return run, env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status="queued", pages=[],
                           est_input_tokens=5000)


@pytest.mark.parametrize(
    "kind,expected",
    [("review", ["a_read", "note_finding", "z_read"]), ("revise", ["a_read", "write_line", "z_read"])],
)
def test_review_kinds_get_the_tools_of_their_mode(env, monkeypatch, review_seam, mode_tools, kind, expected):  # noqa: F811
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: None)
    seen = _capture(monkeypatch)
    run, _doc = _done_run(env)
    agents.run_group(env.db, _review_group(env, run, kind)["id"])
    ctx = seen["ctx"]
    assert [t.name for t in ctx.tools] == expected  # sorted, mode-filtered, pricing hidden
    assert ctx.response_schema is (REVIEW_SCHEMA if kind == "review" else REVISE_SCHEMA)
    assert ctx.step == kind and ctx.kind == kind and ctx.feature == agents.FEATURE_BY_KIND[kind]


def test_main_in_tools_mode_carries_route_budget_heartbeat_timeout_and_store(env, monkeypatch, review_seam, mode_tools):  # noqa: F811
    from app.services import plans_reader_tool_handlers as tool_handlers
    from app.services.plans_reader_loop_store import SupabaseLoopStore

    run, main = _main_group(env, plans_reader_call_timeout_seconds=240)
    seen = _capture(monkeypatch)
    with pytest.raises(Exception, match="stop after capture"):
        agents.run_group(env.db, main["id"])
    ctx = seen["ctx"]
    assert [t.name for t in ctx.tools] == ["a_read", "write_line", "z_read"]
    assert ctx.response_schema is None and ctx.schema_name == "plans_main_tools"
    assert ctx.route["budget_usd"] == 4.5 and ctx.step == "main"
    assert ctx.budget.max_usd == Decimal("4.5")  # the route lowers the 6.00 kind cap
    assert ctx.tool_ctx.heartbeat is tool_handlers.heartbeat and ctx.tool_ctx.group_id == main["id"]
    assert ctx.timeout == 240 and isinstance(ctx.store, SupabaseLoopStore) and ctx.group_id == main["id"]


def test_pricing_tools_reach_agent_loops_only_with_the_agent_tools_flag(env, monkeypatch, review_seam, mode_tools):  # noqa: F811
    run, main = _main_group(env, ai_pricing_agent_tools_enabled=True)
    seen = _capture(monkeypatch)
    with pytest.raises(Exception, match="stop after capture"):
        agents.run_group(env.db, main["id"])
    assert "price_it" in [t.name for t in seen["ctx"].tools]


def test_sub_and_escalation_get_no_tools():
    assert tools.offered_tools("sub") == [] and tools.offered_tools("escalation") == []


def test_a_review_kind_on_a_run_still_reading_stays_queued(env, monkeypatch, review_seam):  # noqa: F811
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    seen = _capture(monkeypatch)
    run = _reading_run(env)
    group = _review_group(env, run, "review")
    agents.run_group(env.db, group["id"])
    assert "ctx" not in seen and env.db.rows("plans_reader_groups", id=group["id"])[0]["status"] == "queued"
    assert _run(env, run)["status"] == "reading"


def test_the_cap_precheck_uses_the_loop_budget(env, monkeypatch, review_seam):  # noqa: F811
    run, main = _main_group(env)
    estimates: list = []

    def check_cap(sb, *, estimated_usd=0, now=None):
        estimates.append(Decimal(str(estimated_usd)))
        raise ai_spend.SpendCapReached("cap", cap_usd=Decimal("75"), spent_usd=Decimal("75"))

    monkeypatch.setattr(ai_spend, "check_cap", check_cap)
    agents.run_group(env.db, main["id"])
    assert estimates == [Decimal("4.5")]
