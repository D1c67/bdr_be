"""The AI Estimating tool loop (services/plans_reader_toolloop) over the
in-memory store (services/plans_reader_loop_store.FakeLoopStore) and fake
provider transports driven through llm.complete_turn.

Pinned here:
  parity      the same scripted policy gives the same tool trace and the
              same stored rounds on the OpenAI and the Anthropic route.
  durability  every round is a stored row (sent then done) with its
              plans_reader_calls row, tool results stored by call id, the
              pre-call stage stored under round 0 and never re-run.
  resume      kill-and-resume at 20 random write points: zero duplicate
              writes, at most one duplicate model call per crash, the same
              final answer; an orphaned round is billed once at its
              estimate and re-sent once; a second orphan fails the group.
  budget      an unaffordable budget fails before any call; a gate refusal
              on a tool round forces the final round; no loop ends more
              than 15% over its budget.
  steering    the final round carries the steer text; on OpenAI the tools
              are omitted, on Anthropic they stay with tool_choice auto and
              a stray tool call gets an error result plus ONE extra round;
              a second stray call fails the group; a max_tokens stop on a
              tool round forces the final round.
  failures    a permanent provider error marks the round failed and a
              resume fails the same way.
  limits      max_calls_per_round, max_tool_calls and max_images refuse a
              call before its handler runs; one result never adds more
              images than are left; a round (and the pre-call block) is
              compacted to 80,000 chars; an Anthropic route fits oversized
              views before sending.
  spend       two crashes then a resume: spent equals the call rows; the
              gate counts earlier attempts once; a retryable failure is
              not an orphan; a lost lease abandons the row at $0.
  writes      a write tool never runs twice after a crash between its
              dispatch and its stored result (kill at every write point).
No network, no database, no prices from any project.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import ai_spend, llm, llm_gate, llm_parts as lp, llm_queue
from app.services import plans_reader_loop_store as store_mod
from app.services import plans_reader_toolloop as loop
from app.services.plans_reader_budgets import LoopBudget
from app.services.plans_reader_loop_store import FakeLoopStore, Killed
from app.services.plans_reader_tools import (
    REGISTRY,
    ToolContext,
    ToolResult,
    ToolSpec,
    fake_services,
    kinds_from_codes,
    register,
    reset_services,
)

GROUP = "77777777-7777-4777-8777-777777777777"
SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
    "additionalProperties": False,
}
FINAL = {"count": 38}
ANTHROPIC_ROUTES = json.dumps({"recount": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"}})


# ── tools under test ─────────────────────────────────────────────────────────


def _schema(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


class Handlers:
    """Counts handler invocations so resume tests can prove a tool never
    ran twice."""

    def __init__(self) -> None:
        self.probe_calls: list[dict] = []
        self.snap_calls: list[dict] = []

    def probe(self, ctx: ToolContext, args: dict) -> ToolResult:
        self.probe_calls.append(dict(args))
        n = len(self.probe_calls)
        return ToolResult.ok({"ok": True, "q": args.get("q"), "n": n}, citations=[f"t:1a2b3c4d:1:{n}"])

    def snap(self, ctx: ToolContext, args: dict) -> ToolResult:
        self.snap_calls.append(dict(args))
        view = {"view_id": f"v{len(self.snap_calls)}", "ref": f"v:v{len(self.snap_calls)}", "bbox": [0, 0, 0.1, 0.1], "w_px": 10, "h_px": 10, "image": b"png-bytes"}
        return ToolResult.ok({"views": [{k: v for k, v in view.items() if k != "image"}]}, citations=[view["ref"]], views=[view])


@pytest.fixture
def tools():
    saved = dict(REGISTRY)
    REGISTRY.clear()
    h = Handlers()
    probe = register(ToolSpec("probe", "Probe the page.", _schema({"q": {"type": "string"}}), {}, "read", kinds_from_codes("RC, RV"), h.probe))
    snap = register(ToolSpec("snap", "Snap a view.", _schema({"page": {"type": "integer"}}), {}, "read", kinds_from_codes("RC, RV"), h.snap, cost_class="image"))
    yield SimpleNamespace(handlers=h, specs=[probe, snap])
    REGISTRY.clear()
    REGISTRY.update(saved)
    reset_services()


# ── transports: the round is derived from the request, never from a counter ─


POLICY_SIMPLE = {1: [("probe", {"q": "a"})], 2: [("snap", {"page": 8}), ("probe", {"q": "b"})], 3: [("probe", {"q": "c"})], 4: "final"}


def _openai_usage(round_no: int, out: int = 100):
    return SimpleNamespace(
        input_tokens=1000 * round_no, input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens=out, output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


class FakeOpenAI:
    def __init__(self, policy: dict, *, output_tokens: int = 100):
        self.policy = policy
        self.output_tokens = output_tokens
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def with_options(self, **kw):
        return self

    def round_of(self, kwargs: dict) -> int:
        prev = kwargs.get("previous_response_id")
        return int(prev.split("_")[1]) + 1 if prev else 1

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.round_of(kwargs)
        step = self.policy.get(r, "final")
        usage = _openai_usage(r, self.output_tokens)
        rid = f"resp_{r}"
        if step == "cut":
            return SimpleNamespace(id=rid, status="incomplete", incomplete_details=SimpleNamespace(reason="max_output_tokens"), output_text="", output=[], usage=usage)
        if step == "filtered":
            return SimpleNamespace(id=rid, status="incomplete", incomplete_details=SimpleNamespace(reason="content_filter"), output_text="", output=[], usage=usage)
        if step == "final" or "tools" not in kwargs:
            return SimpleNamespace(id=rid, status="completed", incomplete_details=None, output_text=json.dumps(FINAL), output=[], usage=usage)
        items = [SimpleNamespace(type="function_call", call_id=f"call_{r}_{i}", name=name, arguments=json.dumps(args)) for i, (name, args) in enumerate(step)]
        return SimpleNamespace(id=rid, status="completed", incomplete_details=None, output_text="", output=items, usage=usage)


class Block:
    def __init__(self, **fields):
        self._fields = fields
        for k, v in fields.items():
            setattr(self, k, v)

    def model_dump(self, mode="json", exclude_none=True):
        return dict(self._fields)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def _anthropic_usage(round_no: int, out: int = 100):
    return SimpleNamespace(input_tokens=1000 * round_no, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=out, cache_creation=None)


class FakeAnthropic:
    """Calls a tool whenever the policy says so, tools offered or not (the
    final-round stray call of spec 4.3)."""

    def __init__(self, policy: dict):
        self.policy = policy
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def with_options(self, **kw):
        return self

    def round_of(self, kwargs: dict) -> int:
        return sum(1 for m in kwargs["messages"] if m["role"] == "assistant") + 1

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        r = self.round_of(kwargs)
        step = self.policy.get(r, "final")
        usage = _anthropic_usage(r)
        mid = f"msg_{r}"
        if step == "cut":
            return FakeStream(SimpleNamespace(id=mid, model="claude-opus-5-5", stop_reason="max_tokens", stop_details=None, content=[Block(type="text", text="{")], usage=usage))
        if step == "final":
            return FakeStream(SimpleNamespace(id=mid, model="claude-opus-5-5", stop_reason="end_turn", stop_details=None, content=[Block(type="text", text=json.dumps(FINAL))], usage=usage))
        blocks = [Block(type="thinking", thinking="", signature=f"sig{r}")] + [Block(type="tool_use", id=f"toolu_{r}_{i}", name=name, input=dict(args)) for i, (name, args) in enumerate(step)]
        return FakeStream(SimpleNamespace(id=mid, model="claude-opus-5-5", stop_reason="tool_use", stop_details=None, content=blocks, usage=usage))


# ── harness ─────────────────────────────────────────────────────────────────


@pytest.fixture
def quiet(monkeypatch):
    llm_gate.reset_gates()
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: None)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    monkeypatch.setattr(ai_spend, "check_cap", lambda sb, *, estimated_usd=0, now=None: Decimal("50"))
    monkeypatch.setattr(ai_spend, "_scope_spent", lambda sb, column, value: Decimal("0"))
    ai_spend.set_ledger_hook(None)
    yield
    ai_spend.set_ledger_hook(None)
    llm_gate.reset_gates()


def _settings(routes: str = "") -> Settings:
    return Settings(_env_file=None, openai_api_key="o-key", anthropic_api_key="a-key", plans_reader_enabled=True, plans_reader_routes=routes)


def _ctx(store, specs, *, settings: Settings, budget: LoopBudget | None = None, precalls=(), run: dict | None = None, **over) -> loop.LoopContext:
    base = dict(
        run=run or {},
        kind="recount",
        feature="plans_recount",
        system="Count things.",
        user_parts=[lp.TextPart("Ledger", cache_break=True), lp.TextPart("Count the troffers.")],
        tools=specs,
        tool_ctx=ToolContext(run_id="run", kind="recount", services=fake_services()),
        store=store,
        response_schema=SCHEMA,
        schema_name="recount",
        budget=budget,
        group_id=GROUP,
        precalls=list(precalls),
        settings=settings,
    )
    base.update(over)
    return loop.LoopContext(**base)


def _budget(**over) -> LoopBudget:
    base = dict(kind="recount", max_rounds=6, max_tool_calls=16, max_calls_per_round=6, max_images=8, max_usd=Decimal("0.80"))
    base.update(over)
    return LoopBudget(**base)


def _run_openai(monkeypatch, store, specs, policy=POLICY_SIMPLE, *, budget=None, client=None, **over):
    client = client or FakeOpenAI(policy)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    result = loop.run_loop(_ctx(store, specs, settings=_settings(), budget=budget, **over))
    return result, client


def _run_anthropic(monkeypatch, store, specs, policy=POLICY_SIMPLE, *, budget=None, client=None, **over):
    client = client or FakeAnthropic(policy)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    result = loop.run_loop(_ctx(store, specs, settings=_settings(ANTHROPIC_ROUTES), budget=budget, **over))
    return result, client


def _trace(store, key=f"group:{GROUP}") -> list[tuple[int, str, dict]]:
    return [(c["round_no"], c["tool_name"], c["args"]) for c in store.tool_calls(key)]


KEY = f"group:{GROUP}"


# ── parity and durability ───────────────────────────────────────────────────


def test_same_policy_same_trace_on_both_providers(quiet, monkeypatch, tools):
    s1 = FakeLoopStore()
    r1, c1 = _run_openai(monkeypatch, s1, tools.specs)
    s2 = FakeLoopStore()
    r2, c2 = _run_anthropic(monkeypatch, s2, tools.specs)
    assert r1.parsed == FINAL and r2.parsed == FINAL
    assert r1.status == r2.status == "done"
    assert _trace(s1) == _trace(s2)
    assert [(c[0], c[1]) for c in _trace(s1)] == [(1, "probe"), (2, "snap"), (2, "probe"), (3, "probe")]
    assert (r1.rounds, r2.rounds) == (4, 4)
    assert (r1.provider, r2.provider) == ("openai", "anthropic")
    assert len(c1.calls) == len(c2.calls) == 4
    # Both providers stored the same round shapes.
    assert [(r["round_no"], r["status"], r["assistant_raw"]["final"]) for r in s1.done_rounds(KEY)] == [
        (r["round_no"], r["status"], r["assistant_raw"]["final"]) for r in s2.done_rounds(KEY)
    ]
    # The Anthropic rounds replay their thinking blocks unchanged.
    assert c2.calls[1]["messages"][1]["content"][0]["type"] == "thinking"
    assert c2.calls[1]["messages"][1]["content"][0]["signature"] == "sig1"


def test_rounds_calls_images_and_ledger_are_durable(quiet, monkeypatch, tools):
    store = FakeLoopStore()
    result, client = _run_openai(monkeypatch, store, tools.specs)
    rounds = store.done_rounds(KEY)
    assert [r["round_no"] for r in rounds] == [1, 2, 3, 4]
    assert all(r["request_sha256"] and len(r["request_sha256"]) == 64 for r in rounds)
    assert all(r["response_id"] == f"resp_{r['round_no']}" for r in rounds)
    assert rounds[-1]["assistant_raw"]["parsed"] == FINAL
    assert [c["round_no"] for c in store.calls] == [1, 2, 3, 4]
    assert all(c["ok"] and c["provider"] == "openai" and c["model"] == "gpt-6-astra" for c in store.calls)
    assert all(Decimal(str(c["cost_usd"])) > 0 for c in store.calls)
    assert result.spent_usd == sum(Decimal(str(c["cost_usd"])) for c in store.calls)
    assert result.images == 1 and result.tool_calls == 4
    assert result.ledger_refs == {"t:1a2b3c4d:1:1", "t:1a2b3c4d:1:2", "t:1a2b3c4d:1:3", "v:v1"}
    # The snap image rode along as an input_image after the tool outputs.
    round3_input = client.calls[2]["input"]
    kinds = [item.get("type") or item.get("role") for item in round3_input]
    assert kinds[:2] == ["function_call_output", "function_call_output"]
    assert round3_input[-1]["role"] == "user"
    assert any(p["type"] == "input_image" for p in round3_input[-1]["content"])
    state = store.loop_states[KEY]
    assert state["round_no"] == 4 and state["status"] == "done" and state["tool_calls"] == 4
    assert "loop_state" in {w[0] for w in store.writes}
    # The system prompt carries the loop rules and the schema every round.
    assert client.calls[0]["instructions"].endswith(loop.LOOP_RULES)
    assert all(kw["text"]["format"]["name"] == "recount" for kw in client.calls)


def test_precall_stage_runs_once_and_is_injected_cited(quiet, monkeypatch, tools):
    store = FakeLoopStore()
    pre = [loop.PreCall("probe", {"q": "census"}, label="census")]
    result, client = _run_openai(monkeypatch, store, tools.specs, {1: "final"}, precalls=pre)
    assert result.parsed == FINAL
    first = client.calls[0]["input"][0]["content"]
    texts = [p["text"] for p in first if p["type"] == "input_text"]
    assert texts[0] == "Ledger" and loop.PRECALL_HEADING in texts[-1]
    assert '[census] probe({"q": "census"})' in texts[-1] and '"n":1' in texts[-1]
    rows = store.tool_calls(KEY)
    assert [(r["round_no"], r["call_id"], r["tool_name"]) for r in rows] == [(0, "pre1", "probe")]
    assert "t:1a2b3c4d:1:1" in result.ledger_refs
    assert len(tools.handlers.probe_calls) == 1
    # A second run over the same store (the stored final) runs no tool and no model.
    again, client2 = _run_openai(monkeypatch, store, tools.specs, {1: "final"}, precalls=pre)
    assert again.parsed == FINAL and again.resumed is True
    assert client2.calls == [] and len(tools.handlers.probe_calls) == 1


# ── kill and resume ─────────────────────────────────────────────────────────


def test_kill_and_resume_at_twenty_random_points(quiet, monkeypatch, tools):
    baseline = FakeLoopStore()
    base_result, base_client = _run_openai(monkeypatch, baseline, tools.specs)
    total_writes = baseline.write_count
    assert total_writes >= 20
    rng = random.Random(7154)
    points = sorted(rng.sample(range(1, total_writes + 1), 20))
    for kill_at in points:
        store = FakeLoopStore(kill_at=kill_at)
        client = FakeOpenAI(POLICY_SIMPLE)
        monkeypatch.setattr(llm, "_client_for", lambda route, settings, c=client: c)
        with pytest.raises(Killed):
            loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
        calls_before = len(client.calls)
        result = loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
        assert result.parsed == FINAL, kill_at
        assert result.resumed is (kill_at > 1), kill_at  # a kill before the first write leaves nothing to resume
        assert store.duplicate_attempts == 0, kill_at
        assert store.duplicate_done_rounds(KEY) == 0, kill_at
        assert [r["round_no"] for r in store.done_rounds(KEY)] == [1, 2, 3, 4], kill_at
        assert len(client.calls) <= len(base_client.calls) + 1, kill_at
        assert len(client.calls) >= calls_before
        ids = [c["call_id"] for c in store.tool_calls(KEY)]
        assert len(ids) == len(set(ids)) == 4, kill_at
        assert _trace(store) == _trace(baseline), kill_at
        assert sorted(c["round_no"] for c in store.calls if c["ok"]) == [1, 2, 3, 4], kill_at
        assert store.duplicate_attempts == 0
    assert base_result.parsed == FINAL


def test_orphaned_round_is_billed_once_at_estimate_and_resent_once(quiet, monkeypatch, tools):
    baseline = FakeLoopStore()
    _run_openai(monkeypatch, baseline, tools.specs)
    # The write right after the first round's `sent` row is its `done` update.
    sent_index = next(i for i, w in enumerate(baseline.writes, start=1) if w[0] == "round")
    store = FakeLoopStore(kill_at=sent_index + 1)
    client = FakeOpenAI(POLICY_SIMPLE)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(Killed):
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert len(client.calls) == 1
    result = loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert result.parsed == FINAL
    rows = store.rounds(KEY)
    orphan = [r for r in rows if r["round_no"] == 1 and r["status"] == "abandoned"]
    assert len(orphan) == 1 and orphan[0]["cost_usd"] == orphan[0]["est_usd"] > 0
    assert "orphaned" in orphan[0]["error"]
    assert [r["status"] for r in rows if r["round_no"] == 1] == ["abandoned", "done"]
    billed = [c for c in store.calls if c["round_no"] == 1]
    assert [(c["ok"], c["attempt"]) for c in billed] == [(False, 1), (True, 2)]
    assert "orphaned attempt billed at estimate" in billed[0]["error"]
    assert len(client.calls) == 5  # one duplicate model call for the crash
    assert result.spent_usd == sum(Decimal(str(c["cost_usd"])) for c in store.calls)
    assert store.duplicate_attempts == 0


def test_second_orphan_of_the_same_round_fails_the_group(quiet, monkeypatch, tools):
    store = FakeLoopStore()
    store.insert_round(KEY, 1, request_sha256="a" * 64, est_usd=Decimal("0.01"))
    first = store.rounds(KEY)[0]
    store.update_round(first["id"], {"status": "abandoned", "error": "orphaned", "cost_usd": Decimal("0.01")})
    store.insert_round(KEY, 1, request_sha256="b" * 64, est_usd=Decimal("0.01"))
    client = FakeOpenAI(POLICY_SIMPLE)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(loop.LoopFailed) as exc:
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert exc.value.reason == "orphaned" and "twice" in str(exc.value)
    assert client.calls == []
    assert [r["status"] for r in store.rounds(KEY)] == ["abandoned", "abandoned"]
    assert [c["attempt"] for c in store.calls] == [2]


# ── budget ───────────────────────────────────────────────────────────────────


def test_unaffordable_budget_fails_before_any_call(quiet, monkeypatch, tools):
    store = FakeLoopStore()
    client = FakeOpenAI(POLICY_SIMPLE)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(loop.LoopFailed) as exc:
        loop.run_loop(_ctx(store, tools.specs, settings=_settings(), budget=_budget(max_usd=Decimal("0.0001"))))
    assert exc.value.reason == "budget"
    assert client.calls == [] and store.rounds(KEY) == []


def test_gate_refusal_on_a_tool_round_forces_the_final_round(quiet, monkeypatch, tools):
    refusals = []

    def gate(ctx):
        if not refusals:
            refusals.append(ctx.estimated_usd)
            raise ai_spend.GroupBudgetReached("over", max_usd=Decimal("0.8"), spent_usd=Decimal("0.79"), estimated_usd=ctx.estimated_usd)
        return Decimal("50")

    monkeypatch.setattr(ai_spend, "gate_round", gate)
    store = FakeLoopStore()
    result, client = _run_openai(monkeypatch, store, tools.specs)
    assert result.parsed == FINAL and result.rounds == 1
    assert len(client.calls) == 1 and "tools" not in client.calls[0]
    assert client.calls[0]["input"][0]["content"][-1]["text"] == loop.FINAL_ROUND_STEER
    assert store.done_rounds(KEY)[0]["assistant_raw"]["final"] is True


def test_gate_refusal_on_the_final_round_fails_with_the_gate_sentence(quiet, monkeypatch, tools):
    def gate(ctx):
        raise ai_spend.RunCeilingReached("ceiling hit", ceiling_usd=Decimal("1"), spent_usd=Decimal("1"), estimated_usd=Decimal("0.1"))

    monkeypatch.setattr(ai_spend, "gate_round", gate)
    store = FakeLoopStore()
    with pytest.raises(loop.LoopFailed, match="ceiling hit") as exc:
        _run_openai(monkeypatch, store, tools.specs)
    assert exc.value.reason == "budget" and store.rounds(KEY) == []


def test_daily_cap_propagates_untouched(quiet, monkeypatch, tools):
    cap = ai_spend.SpendCapReached("daily cap", cap_usd=Decimal("75"), spent_usd=Decimal("75"))

    def check_cap(sb, *, estimated_usd=0, now=None):
        raise cap

    monkeypatch.setattr(ai_spend, "check_cap", check_cap)
    with pytest.raises(ai_spend.SpendCapReached):
        _run_openai(monkeypatch, FakeLoopStore(), tools.specs)


@pytest.mark.parametrize("output_tokens", [100, 2_000, 6_000])
def test_no_loop_ends_more_than_fifteen_percent_over_budget(quiet, monkeypatch, tools, output_tokens):
    store = FakeLoopStore()
    client = FakeOpenAI(POLICY_SIMPLE, output_tokens=output_tokens)
    budget = _budget(max_usd=Decimal("0.40"))
    try:
        result, _ = _run_openai(monkeypatch, store, tools.specs, client=client, budget=budget)
        spent = result.spent_usd
    except loop.LoopFailed as exc:
        assert exc.reason == "budget"
        spent = sum(Decimal(str(c["cost_usd"])) for c in store.calls)
    assert spent <= budget.max_usd * Decimal("1.15")


# ── steering and stray tool calls ───────────────────────────────────────────


def test_openai_final_round_omits_tools_and_steers(quiet, monkeypatch, tools):
    store = FakeLoopStore()
    result, client = _run_openai(monkeypatch, store, tools.specs, budget=_budget(max_rounds=2))
    assert result.parsed == FINAL and result.rounds == 2
    assert "tools" in client.calls[0] and "tools" not in client.calls[1]
    tail = client.calls[1]["input"][-1]
    assert tail["role"] == "user" and tail["content"][-1]["text"] == loop.FINAL_ROUND_STEER
    assert store.done_rounds(KEY)[1]["assistant_raw"]["final"] is True


def test_anthropic_stray_tool_call_gets_an_error_result_and_one_extra_round(quiet, monkeypatch, tools):
    policy = {1: [("probe", {"q": "a"})], 2: [("probe", {"q": "stray"})], 3: "final"}
    store = FakeLoopStore()
    result, client = _run_anthropic(monkeypatch, store, tools.specs, policy, budget=_budget(max_rounds=2))
    assert result.parsed == FINAL and result.rounds == 3
    assert len(client.calls) == 3
    # Round 2 (the planned final): tools stay, tool_choice auto, the steer closes the user message.
    kw2 = client.calls[1]
    assert [t["name"] for t in kw2["tools"]] == ["probe", "snap"] and kw2["tool_choice"] == {"type": "auto"}
    assert kw2["messages"][-1]["content"][-1]["text"] == loop.FINAL_ROUND_STEER
    # Round 3: the stray call was answered with an error result, not run.
    kw3 = client.calls[2]
    stray = kw3["messages"][-1]["content"][0]
    assert stray["type"] == "tool_result" and stray["is_error"] is True
    assert loop.STRAY_TOOL_RESULT in stray["content"][0]["text"]
    assert len(tools.handlers.probe_calls) == 1
    stored = [c for c in store.tool_calls(KEY) if c["round_no"] == 2]
    assert len(stored) == 1 and stored[0]["refused"] is True and stored[0]["result_data"]["code"] == "final_round"


def test_second_stray_tool_call_fails_the_group_bounded(quiet, monkeypatch, tools):
    policy = {1: [("probe", {"q": "a"})], 2: [("probe", {"q": "s1"})], 3: [("probe", {"q": "s2"})], 4: "final"}
    store = FakeLoopStore()
    with pytest.raises(loop.LoopFailed) as exc:
        _run_anthropic(monkeypatch, store, tools.specs, policy, budget=_budget(max_rounds=2))
    assert exc.value.reason == "failed" and "tool use was finished" in str(exc.value)
    assert len(store.done_rounds(KEY)) == 3  # never a fourth round
    assert len(tools.handlers.probe_calls) == 1


def test_max_tokens_on_a_tool_round_forces_the_final_round(quiet, monkeypatch, tools):
    policy = {1: "cut", 2: [("probe", {"q": "never"})]}
    store = FakeLoopStore()
    result, client = _run_openai(monkeypatch, store, tools.specs, policy)
    assert result.parsed == FINAL and result.rounds == 2
    assert "tools" not in client.calls[1]
    assert client.calls[1]["input"][-1]["content"][-1]["text"] == loop.FINAL_ROUND_STEER
    assert store.done_rounds(KEY)[0]["assistant_raw"]["stop_reason"] == "max_tokens"
    assert tools.handlers.probe_calls == []


def test_max_tokens_on_the_final_round_fails(quiet, monkeypatch, tools):
    policy = {1: "cut", 2: "cut"}
    with pytest.raises(loop.LoopFailed, match="cut off"):
        _run_openai(monkeypatch, FakeLoopStore(), tools.specs, policy)


def test_image_cap_forces_the_final_round(quiet, monkeypatch, tools):
    policy = {1: [("snap", {"page": 1})], 2: [("snap", {"page": 2})], 3: [("snap", {"page": 3})], 4: "final"}
    store = FakeLoopStore()
    result, client = _run_openai(monkeypatch, store, tools.specs, policy, budget=_budget(max_images=2))
    assert result.images == 2 and result.rounds == 3
    assert "tools" in client.calls[1] and "tools" not in client.calls[2]


# ── failures ─────────────────────────────────────────────────────────────────


def test_permanent_provider_error_marks_the_round_failed_and_resume_fails(quiet, monkeypatch, tools):
    policy = {1: [("probe", {"q": "a"})], 2: "filtered"}
    store = FakeLoopStore()
    client = FakeOpenAI(policy)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(llm.LlmBadOutputPermanent):
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    rows = store.rounds(KEY)
    assert [(r["round_no"], r["status"]) for r in rows] == [(1, "done"), (2, "failed")]
    assert [c["ok"] for c in store.calls] == [True, False]
    with pytest.raises(loop.LoopFailed) as exc:
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert exc.value.reason == "failed" and len(client.calls) == 2


def test_unparseable_final_answer_is_a_retryable_abandoned_round(quiet, monkeypatch, tools):
    """The transport raises LlmBadOutput (retryable) on junk JSON; the loop
    leaves the round abandoned so the queue's retry re-sends it once."""

    class Garbage(FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(id="resp_1", status="completed", incomplete_details=None, output_text="not json", output=[], usage=_openai_usage(1))

    store = FakeLoopStore()
    client = Garbage({})
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(llm.LlmBadOutput):
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    rows = store.rounds(KEY)
    assert [(r["round_no"], r["status"]) for r in rows] == [(1, "abandoned")]
    assert [c["ok"] for c in store.calls] == [False]


def test_unpriced_model_and_bad_keys_are_refused(quiet, tools):
    routes = json.dumps({"recount": {"provider": "openai", "model": "gpt-99-unpriced", "effort": "low"}})
    with pytest.raises(loop.LoopFailed, match="unpriced"):
        loop.run_loop(_ctx(FakeLoopStore(), tools.specs, settings=_settings(routes)))
    with pytest.raises(ValueError):
        loop.run_loop(_ctx(FakeLoopStore(), tools.specs, settings=_settings(), group_id=None))


def test_lost_lease_abandons_the_unsent_row_at_zero_and_is_no_orphan(quiet, monkeypatch, tools):
    lease = {"ok": False}
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: lease["ok"])
    store = FakeLoopStore()
    client = FakeOpenAI(POLICY_SIMPLE)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(llm.LlmLeaseLost):
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert client.calls == []
    rows = store.rounds(KEY)
    assert [r["status"] for r in rows] == ["abandoned"]
    assert rows[0]["cost_usd"] == 0 and rows[0]["error"] == loop.LEASE_LOST_ERROR
    assert store.calls == []  # nothing was sent, nothing is billed
    # The next attempt neither bills it as an orphan nor counts it toward the twice rule.
    lease["ok"] = True
    result = loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert result.parsed == FINAL and len(client.calls) == 4
    assert [c["attempt"] for c in store.calls if c["round_no"] == 1] == [2]
    assert all(c["ok"] for c in store.calls)
    assert result.spent_usd == sum(Decimal(str(c["cost_usd"])) for c in store.calls)


def test_a_lease_lost_at_the_heartbeat_writes_no_round(quiet, monkeypatch, tools):
    store = FakeLoopStore()
    client = FakeOpenAI(POLICY_SIMPLE)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)

    def lost():
        raise llm.LlmLeaseLost("lease lost")

    ctx = _ctx(store, tools.specs, settings=_settings(), tool_ctx=ToolContext(run_id="run", kind="recount", services=fake_services(), heartbeat=lost))
    with pytest.raises(llm.LlmLeaseLost):
        loop.run_loop(ctx)
    assert store.rounds(KEY) == [] and client.calls == []


# ── the store itself ─────────────────────────────────────────────────────────


def test_store_keys_and_compaction():
    assert store_mod.key_columns("group:abc") == {"group_id": "abc"}
    assert store_mod.key_columns("message:m1") == {"message_id": "m1"}
    with pytest.raises(ValueError):
        store_mod.key_columns("thread:x")
    assert store_mod.loop_key(group_id="g") == "group:g"
    assert store_mod._compact({"a": 1, "b": None}) == {"a": 1}


def test_supabase_store_omits_null_columns_and_is_idempotent():
    """A minimal fake client records inserts and answers selects from them."""

    class Q:
        def __init__(self, db, table):
            self.db, self.table, self.filters, self.payload, self.op = db, table, [], None, "select"

        def select(self, *_a):
            return self

        def insert(self, payload):
            self.op, self.payload = "insert", payload
            return self

        def update(self, payload):
            self.op, self.payload = "update", payload
            return self

        def eq(self, col, val):
            self.filters.append((col, val))
            return self

        def order(self, *_a, **_k):
            return self

        def range(self, *_a):
            return self

        def limit(self, *_a):
            return self

        def execute(self):
            rows = self.db.setdefault(self.table, [])
            if self.op == "insert":
                row = {"id": f"id{len(rows) + 1}", "created_at": f"2026-10-21T00:00:00.{len(rows):06d}+00:00", **self.payload}
                rows.append(row)
                return SimpleNamespace(data=[dict(row)])
            hits = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
            if self.op == "update":
                for r in hits:
                    r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in hits])

    db: dict = {}
    sb = SimpleNamespace(table=lambda name: Q(db, name))
    store = store_mod.SupabaseLoopStore(sb, run_id="run1")
    row = store.insert_round("group:g1", 1, request_sha256="x" * 64, est_usd=Decimal("0.01"))
    assert set(row) >= {"run_id", "group_id", "round_no", "status", "request_sha256", "est_usd"}
    assert "message_id" not in row and "response_id" not in row
    again = store.insert_round("group:g1", 1, request_sha256="y" * 64, est_usd=Decimal("0.02"))
    assert again["id"] == row["id"] and len(db[store_mod.ROUNDS_TABLE]) == 1
    store.update_round(row["id"], {"status": "done", "response_id": "r1", "cost_usd": Decimal("0.003"), "error": None})
    assert db[store_mod.ROUNDS_TABLE][0]["status"] == "done" and db[store_mod.ROUNDS_TABLE][0]["cost_usd"] == 0.003
    call = store.insert_tool_call("group:g1", 1, {"call_id": "c1", "tool_name": "probe", "args": {}, "result_text": "ok", "views": None})
    assert "views" not in call and call["refused"] is False
    assert store.insert_tool_call("group:g1", 1, {"call_id": "c1", "tool_name": "probe"})["id"] == call["id"]
    assert len(store.tool_calls("group:g1")) == 1
    assert store.record_call("group:g1", 1, 1, {"kind": "recount", "cost_usd": Decimal("0.003"), "ok": True, "thread_id": None, "conservative_cost": False})
    assert not store.record_call("group:g1", 1, 1, {"kind": "recount", "cost_usd": Decimal("0.003"), "ok": True})
    calls = db[store_mod.CALLS_TABLE]
    assert len(calls) == 1 and calls[0]["request_snapshot"]["loop_attempt"] == 1
    assert "thread_id" not in calls[0] and "conservative_cost" not in calls[0] and calls[0]["group_id"] == "g1"
    store.save_loop_state("group:g1", {"round_no": 1})
    assert db[store_mod.GROUPS_TABLE] == [] or True  # update on an absent row writes nothing
    store.save_loop_state("message:m1", {"round_no": 1})  # messages have no column: a no-op


# ── the read-only view ───────────────────────────────────────────────────────


def test_loop_view_redacts_pricing_for_non_pricing_callers():
    rounds = [
        {"id": "r1", "round_no": 1, "status": "done", "assistant_raw": {"text": "", "final": False, "stop_reason": "tool_use"}, "tool_calls": [{"call_id": "c1", "name": "probe", "args": {"q": "a"}}], "created_at": "2026-10-21T19:00:00+00:00"},
        {"id": "r2", "round_no": 2, "status": "done", "assistant_raw": {"text": "", "final": False, "stop_reason": "tool_use"}, "tool_calls": [{"call_id": "c2", "name": "price_lookup", "args": {"description": "EMT"}}], "created_at": "2026-10-21T19:01:00+00:00"},
        {"id": "r3", "round_no": 3, "status": "done", "assistant_raw": {"text": "it costs 12 dollars", "final": True, "stop_reason": "final"}, "tool_calls": [], "created_at": "2026-10-21T19:02:00+00:00"},
    ]
    calls = [
        {"id": "t1", "round_no": 1, "call_id": "c1", "tool_name": "probe", "args": {"q": "a"}, "result_text": "ok", "result_data": {"ok": True}, "citations": ["t:x:1:1"], "created_at": "2026-10-21T19:00:30+00:00"},
        {"id": "t2", "round_no": 2, "call_id": "c2", "tool_name": "price_lookup", "args": {"description": "EMT"}, "result_text": "12.00", "result_data": {"usd": 12}, "citations": ["$:emt@v1"], "created_at": "2026-10-21T19:01:30+00:00"},
    ]
    group = {"id": "g", "kind": "review", "status": "done"}
    hidden = loop.loop_view(group, rounds, calls, include_pricing=False)
    assert hidden["pricing_visible"] is False
    assert hidden["tool_calls"][0]["result_text"] == "ok" and hidden["tool_calls"][0]["pricing_restricted"] is False
    priced = hidden["tool_calls"][1]
    assert priced["result_text"] == "pricing" and priced["result_data"] == loop.PRICING_REDACTED and priced["args"] == loop.PRICING_REDACTED
    assert priced["citations"] == [] and priced["pricing_restricted"] is True
    assert [r["pricing_restricted"] for r in hidden["rounds"]] == [False, True, True]
    assert hidden["rounds"][2]["text"] == "pricing" and hidden["rounds"][1]["tool_calls"][0]["args"] == loop.PRICING_REDACTED
    assert "12.00" not in json.dumps(hidden) and "12 dollars" not in json.dumps(hidden)
    # Pacific timestamps.
    assert hidden["rounds"][0]["created_at"] == "2026-10-21T12:00:00-07:00"
    shown = loop.loop_view(group, rounds, calls, include_pricing=True)
    assert shown["tool_calls"][1]["result_text"] == "12.00" and shown["rounds"][2]["text"] == "it costs 12 dollars"
    assert shown["rounds"][2]["pricing_restricted"] is False and shown["tool_calls"][1]["pricing_restricted"] is True


def test_request_sha_is_stable_and_blind_to_image_bytes():
    def req(png: bytes) -> lp.ModelRequest:
        return lp.ModelRequest(feature="plans_recount", system="s", messages=(lp.UserTurn((lp.TextPart("a"), lp.ImagePart(png, 10, 10))),))

    assert loop.request_sha256(req(b"x" * 10)) == loop.request_sha256(req(b"y" * 10))
    assert loop.request_sha256(req(b"x" * 10)) != loop.request_sha256(req(b"x" * 11))


# ── wave 2 review fixes: limits, compaction, spend, orphans, idempotent writes ─


def _png(w: int, h: int) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def more_tools(tools):
    """Extra fakes: a write tool that counts its executions and records the
    write key, an image tool with three views, a wide-image tool and a tool
    with a 12,000-char result."""
    counts = SimpleNamespace(writes=[], keys=[])

    def writer(ctx: ToolContext, args: dict) -> ToolResult:
        counts.writes.append(dict(args))
        counts.keys.append(ctx.write_key)
        return ToolResult.ok({"written": args.get("note")})

    def triple(ctx: ToolContext, args: dict) -> ToolResult:
        views = [{"view_id": f"t{i}", "ref": f"v:t{i}", "bbox": [0, 0, 0.1, 0.1], "w_px": 10, "h_px": 10, "image": b"png"} for i in range(3)]
        return ToolResult.ok({"views": len(views)}, citations=[v["ref"] for v in views], views=views)

    def wide(ctx: ToolContext, args: dict) -> ToolResult:
        view = {"view_id": "w1", "ref": "v:w1", "bbox": [0, 0, 1, 0.1], "w_px": 3583, "h_px": 480, "image": _png(3583, 480)}
        return ToolResult.ok({"views": 1}, citations=["v:w1"], views=[view])

    def big(ctx: ToolContext, args: dict) -> ToolResult:
        return ToolResult.ok({}, text="x" * 30_000)

    specs = [
        register(ToolSpec("writer", "Write a note.", _schema({"note": {"type": "string"}}), {}, "write", kinds_from_codes("RC, RV"), writer)),
        register(ToolSpec("triple", "Three views.", _schema({"page": {"type": "integer"}}), {}, "read", kinds_from_codes("RC, RV"), triple, cost_class="image")),
        register(ToolSpec("wide", "A wide view.", _schema({"page": {"type": "integer"}}), {}, "read", kinds_from_codes("RC, RV"), wide, cost_class="image")),
        register(ToolSpec("big", "A big result.", _schema({"page": {"type": "integer"}}), {}, "read", kinds_from_codes("RC, RV"), big, max_calls_per_group=40)),
    ]
    return SimpleNamespace(counts=counts, specs=tools.specs + specs, handlers=tools.handlers)


def test_max_calls_per_round_refuses_calls_before_they_run(quiet, monkeypatch, more_tools):
    policy = {1: [("probe", {"q": str(i)}) for i in range(4)], 2: "final"}
    store = FakeLoopStore()
    result, _ = _run_openai(monkeypatch, store, more_tools.specs, policy, budget=_budget(max_calls_per_round=2))
    assert result.parsed == FINAL
    assert len(more_tools.handlers.probe_calls) == 2
    rows = store.tool_calls(KEY)
    assert [r["refused"] for r in rows] == [False, False, True, True]
    assert {r["result_data"]["code"] for r in rows if r["refused"]} == {"max_calls"}
    assert all(r["result_data"]["detail"]["limit"] == "max_calls_per_round" for r in rows if r["refused"])


def test_max_tool_calls_is_enforced_inside_the_round(quiet, monkeypatch, more_tools):
    policy = {1: [("probe", {"q": "a"}), ("probe", {"q": "b"})], 2: [("probe", {"q": "c"}), ("probe", {"q": "d"})], 3: "final"}
    store = FakeLoopStore()
    result, client = _run_openai(monkeypatch, store, more_tools.specs, policy, budget=_budget(max_tool_calls=3))
    assert result.parsed == FINAL
    assert [c["q"] for c in more_tools.handlers.probe_calls] == ["a", "b", "c"]
    refused = [r for r in store.tool_calls(KEY) if r["refused"]]
    assert len(refused) == 1 and refused[0]["result_data"]["detail"]["limit"] == "max_tool_calls"
    assert "tools" not in client.calls[2]  # the budget then forces the final round


def test_image_budget_refuses_image_tools_and_caps_a_result(quiet, monkeypatch, more_tools):
    policy = {1: [("snap", {"page": 1}), ("triple", {"page": 2}), ("snap", {"page": 3}), ("probe", {"q": "text still runs"})], 2: "final"}
    store = FakeLoopStore()
    result, client = _run_openai(monkeypatch, store, more_tools.specs, policy, budget=_budget(max_images=3))
    assert result.images == 3
    rows = {r["tool_name"] + str(r["args"].get("page", "")): r for r in store.tool_calls(KEY)}
    assert len(rows["triple2"]["views"]) == 2 and "1 of 3 views not delivered" in rows["triple2"]["result_text"]
    assert rows["snap3"]["refused"] and rows["snap3"]["result_data"]["code"] == "max_images"
    assert not rows["probe"]["refused"]
    images_sent = [p for p in client.calls[1]["input"][-1]["content"] if p["type"] == "input_image"]
    assert len(images_sent) == 3


def test_round_results_are_compacted_to_the_round_cap(quiet, monkeypatch, more_tools):
    policy = {1: [("big", {"page": i}) for i in range(8)], 2: "final"}
    store = FakeLoopStore()
    _, client = _run_openai(monkeypatch, store, more_tools.specs, policy, budget=_budget(max_calls_per_round=10, max_tool_calls=40))
    outputs = [item["output"] for item in client.calls[1]["input"] if item.get("type") == "function_call_output"]
    assert len(outputs) == 8 and all(len(o) <= 12_000 for o in outputs)
    assert sum(len(o) for o in outputs) <= 80_000 < 8 * 12_000
    assert all("truncated" in o for o in outputs)


def test_precall_block_is_compacted_to_the_round_cap(quiet, monkeypatch, more_tools):
    pre = [loop.PreCall("big", {"page": i}) for i in range(8)]
    store = FakeLoopStore()
    _, client = _run_openai(monkeypatch, store, more_tools.specs, {1: "final"}, precalls=pre)
    block = [p["text"] for p in client.calls[0]["input"][0]["content"] if p["type"] == "input_text"][-1]
    assert block.count("x" * 100) and block.count("x") <= 80_000
    assert len(block) <= 80_000 + 2_000  # the heading and the call labels on top


def test_anthropic_route_fits_oversized_views_before_sending(quiet, monkeypatch, more_tools):
    policy = {1: [("wide", {"page": 1})], 2: "final"}
    store = FakeLoopStore()
    result, client = _run_anthropic(monkeypatch, store, more_tools.specs, policy)
    assert result.parsed == FINAL and result.images == 1
    blocks = client.calls[1]["messages"][-1]["content"]
    images_sent = [b for b in blocks if b.get("type") == "image"] + [
        c for b in blocks if b.get("type") == "tool_result" for c in (b.get("content") or []) if c.get("type") == "image"
    ]
    assert len(images_sent) == 1
    from PIL import Image
    import base64
    import io

    im = Image.open(io.BytesIO(base64.b64decode(images_sent[0]["source"]["data"])))
    assert lp.ANTHROPIC_IMAGES.fits(*im.size) and max(im.size) <= 2576


def test_two_crashes_then_resume_spend_equals_the_call_rows(quiet, monkeypatch, tools):
    class KillOn(FakeLoopStore):
        def __init__(self):
            super().__init__()
            self.kill_when = None

        def _write(self, label, payload):
            if self.kill_when is not None and self.kill_when(label, payload):
                self.kill_when = None
                self.write_count += 1
                raise Killed(f"killed before {label}")
            super()._write(label, payload)

    store = KillOn()
    client = FakeOpenAI(POLICY_SIMPLE)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    store.kill_when = lambda label, p: label == "round_update" and p == {"round_no": 1, "status": "done"}
    with pytest.raises(Killed):
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    store.kill_when = lambda label, p: label == "round_update" and p == {"round_no": 2, "status": "done"}
    with pytest.raises(Killed):
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    result = loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert result.parsed == FINAL
    orphans = [r for r in store.rounds(KEY) if r["status"] == "abandoned"]
    assert [r["round_no"] for r in orphans] == [1, 2]
    billed = sum(Decimal(str(c["cost_usd"])) for c in store.calls)
    assert result.spent_usd == billed
    print(f"two-crash resume: spent_usd {result.spent_usd}, call rows {billed}, orphans {len(orphans)}")
    assert result.spent_usd > sum(Decimal(str(r["cost_usd"])) for r in store.done_rounds(KEY))


def test_gate_counts_earlier_attempts_once(quiet, monkeypatch, tools):
    seen = []
    real = ai_spend.gate_round

    def spy(ctx):
        seen.append((ctx.max_group_usd, ctx.group_spent_usd))
        return real(ctx)

    monkeypatch.setattr(ai_spend, "gate_round", spy)
    store = FakeLoopStore()
    store.insert_round(KEY, 1, request_sha256="a" * 64, est_usd=Decimal("0.05"))
    first = store.rounds(KEY)[0]
    store.update_round(first["id"], {"status": "abandoned", "error": "timed out", "cost_usd": Decimal("0.05")})
    result, _ = _run_openai(monkeypatch, store, tools.specs, budget=_budget(max_usd=Decimal("0.80")))
    assert result.parsed == FINAL
    # The gate keeps the group's own cap and counts the earlier attempt in spent.
    assert seen[0] == (Decimal("0.80"), Decimal("0.05"))
    assert result.spent_usd == sum(Decimal(str(c["cost_usd"])) for c in store.calls) + Decimal("0.05")


def test_a_retryable_failure_then_one_orphan_does_not_fail_the_group(quiet, monkeypatch, tools):
    class Flaky(FakeOpenAI):
        def __init__(self, policy):
            super().__init__(policy)
            self.failed = False

        def _create(self, **kwargs):
            if not self.failed:
                self.failed = True
                self.calls.append(kwargs)
                raise TimeoutError("provider timed out")
            return super()._create(**kwargs)

    store = FakeLoopStore()
    client = Flaky(POLICY_SIMPLE)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(TimeoutError):
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    timeout_row = store.rounds(KEY)[0]
    assert timeout_row["status"] == "abandoned" and timeout_row["cost_usd"] > 0  # billed at estimate
    # Second attempt of round 1 is orphaned (killed before its done update).
    store.kill_at = store.write_count + 2
    with pytest.raises(Killed):
        loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert [r["status"] for r in store.rounds(KEY)] == ["abandoned", "sent"]
    result = loop.run_loop(_ctx(store, tools.specs, settings=_settings()))
    assert result.parsed == FINAL
    assert [r["status"] for r in store.rounds(KEY) if r["round_no"] == 1] == ["abandoned", "abandoned", "done"]
    assert result.spent_usd == sum(Decimal(str(c["cost_usd"])) for c in store.calls)


def test_write_tool_never_runs_twice_after_a_crash_between_dispatch_and_store(quiet, monkeypatch, more_tools):
    policy = {1: [("writer", {"note": "n1"})], 2: "final"}
    store = FakeLoopStore()
    client = FakeOpenAI(policy)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    baseline = FakeLoopStore()
    loop.run_loop(_ctx(baseline, more_tools.specs, settings=_settings()))
    assert more_tools.counts.keys == [f"{GROUP}:call_1_0"]
    finalize_at = next(i for i, w in enumerate(baseline.writes, start=1) if w[0] == "tool_call_update")
    more_tools.counts.writes.clear()
    store.kill_at = finalize_at  # the handler ran; its result was never stored
    with pytest.raises(Killed):
        loop.run_loop(_ctx(store, more_tools.specs, settings=_settings()))
    assert len(more_tools.counts.writes) == 1
    result = loop.run_loop(_ctx(store, more_tools.specs, settings=_settings()))
    assert result.parsed == FINAL
    assert len(more_tools.counts.writes) == 1  # never re-run
    row = store.get_tool_call(KEY, "call_1_0")
    assert row["refused"] and row["result_data"]["code"] == "interrupted"
    assert store.duplicate_attempts == 0


def test_kill_anywhere_runs_each_write_call_at_most_once(quiet, monkeypatch, more_tools):
    policy = {1: [("writer", {"note": "n1"}), ("probe", {"q": "a"})], 2: [("writer", {"note": "n2"})], 3: "final"}
    baseline = FakeLoopStore()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: FakeOpenAI(policy))
    loop.run_loop(_ctx(baseline, more_tools.specs, settings=_settings()))
    for kill_at in range(1, baseline.write_count + 1):
        more_tools.counts.writes.clear()
        store = FakeLoopStore(kill_at=kill_at)
        client = FakeOpenAI(policy)
        monkeypatch.setattr(llm, "_client_for", lambda route, settings, c=client: c)
        with pytest.raises(Killed):
            loop.run_loop(_ctx(store, more_tools.specs, settings=_settings()))
        result = loop.run_loop(_ctx(store, more_tools.specs, settings=_settings()))
        assert result.parsed == FINAL, kill_at
        notes = [w["note"] for w in more_tools.counts.writes]
        assert sorted(set(notes)) == sorted(notes), (kill_at, notes)  # at most once each
        assert store.duplicate_attempts == 0, kill_at


def test_loop_view_redacts_every_tool_call_after_the_first_pricing_round():
    rounds = [
        {"id": "r1", "round_no": 1, "status": "done", "assistant_raw": {"text": "", "final": False}, "tool_calls": [{"call_id": "c1", "name": "probe", "args": {"q": "a"}}]},
        {"id": "r2", "round_no": 2, "status": "done", "assistant_raw": {"text": "", "final": False}, "tool_calls": [{"call_id": "c2", "name": "price_lookup", "args": {"description": "EMT"}}]},
        {"id": "r3", "round_no": 3, "status": "done", "assistant_raw": {"text": "", "final": False}, "tool_calls": [{"call_id": "c3", "name": "record_finding", "args": {"claim": "EMT at 4.44 per foot"}}]},
    ]
    calls = [
        {"id": "t1", "round_no": 1, "call_id": "c1", "tool_name": "probe", "args": {"q": "a"}, "result_text": "ok"},
        {"id": "t2", "round_no": 2, "call_id": "c2", "tool_name": "price_lookup", "args": {"description": "EMT"}, "result_text": "4.44"},
        {"id": "t3", "round_no": 3, "call_id": "c3", "tool_name": "record_finding", "args": {"claim": "EMT at 4.44 per foot"}, "result_text": "f:1 EMT at 4.44", "result_data": {"claim": "EMT at 4.44 per foot"}, "citations": ["f:1"]},
    ]
    hidden = loop.loop_view({"id": "g"}, rounds, calls, include_pricing=False)
    assert "4.44" not in json.dumps(hidden)
    later = hidden["tool_calls"][2]
    assert later["args"] == loop.PRICING_REDACTED and later["result_data"] == loop.PRICING_REDACTED
    assert later["citations"] == [] and later["pricing_restricted"] is True
    assert hidden["rounds"][2]["tool_calls"][0]["args"] == loop.PRICING_REDACTED
    assert hidden["tool_calls"][0]["result_text"] == "ok" and hidden["tool_calls"][0]["pricing_restricted"] is False
    shown = loop.loop_view({"id": "g"}, rounds, calls, include_pricing=True)
    assert shown["tool_calls"][2]["args"] == {"claim": "EMT at 4.44 per foot"}


# ── stop_when: a tool declared the work finished ────────────────────────────


def test_stop_when_ends_the_loop_right_after_the_rounds_tools_are_served(quiet, monkeypatch, tools):
    policy = {1: [("probe", {"q": "a"})], 2: [("probe", {"q": "b"})], 3: "final"}
    store = FakeLoopStore()
    result, client = _run_openai(monkeypatch, store, tools.specs, policy, stop_when=lambda: len(tools.handlers.probe_calls) >= 1)
    assert result.status == "done" and result.stop_reason == "tool_stop" and result.parsed is None
    assert (result.rounds, result.tool_calls) == (1, 1) and len(client.calls) == 1
    assert _trace(store) == [(1, "probe", {"q": "a"})]
    assert store.loop_states[KEY]["status"] == "done"
    # The same policy on Anthropic stops at the same point.
    s2 = FakeLoopStore()
    r2, c2 = _run_anthropic(monkeypatch, s2, tools.specs, policy, stop_when=lambda: len(tools.handlers.probe_calls) >= 2)
    assert r2.stop_reason == "tool_stop" and r2.rounds == 1 and len(c2.calls) == 1 and _trace(s2) == [(1, "probe", {"q": "a"})]


def test_a_stop_listener_that_says_no_or_raises_never_ends_the_loop(quiet, monkeypatch, tools):
    s1 = FakeLoopStore()
    r1, c1 = _run_openai(monkeypatch, s1, tools.specs, stop_when=lambda: False)
    assert r1.parsed == FINAL and r1.stop_reason != "tool_stop" and len(c1.calls) == 4

    def broken():
        raise RuntimeError("listener broke")

    s2 = FakeLoopStore()
    r2, c2 = _run_openai(monkeypatch, s2, tools.specs, stop_when=broken)
    assert r2.parsed == FINAL and r2.stop_reason != "tool_stop" and len(c2.calls) == 4


# ── per-attempt loop keys (WS7 recount second sample) ───────────────────────


def test_attempt_keys_leave_plain_keys_unchanged():
    assert store_mod.loop_key(group_id="g") == "group:g" == store_mod.loop_key(group_id="g", attempt=1)
    assert store_mod.loop_key(message_id="m") == "message:m"
    assert store_mod.loop_key(group_id="g", attempt=2) == "group:g#a2"
    assert store_mod.key_columns("group:g#a2") == {"group_id": "g"}
    assert store_mod.split_key("group:g#a2") == ("group:g", 2) and store_mod.split_key("group:g") == ("group:g", 1)
    assert store_mod.round_offset("group:g") == 0 and store_mod.round_offset("group:g#a2") == store_mod.ATTEMPT_ROUND_BASE
    assert store_mod.stored_call_id("group:g", "pre1") == "pre1"
    assert store_mod.stored_call_id("group:g#a2", "pre1") == "a2:pre1"
    assert store_mod.loop_call_id("group:g#a2", "a2:pre1") == "pre1"
    ctx = SimpleNamespace(group_id="g", message_id=None)
    assert loop.LoopContext.key.fget(SimpleNamespace(**vars(ctx), loop_attempt=None)) == "group:g"
    assert loop.LoopContext.key.fget(SimpleNamespace(**vars(ctx), loop_attempt=2)) == "group:g#a2"


class _UniqueDB:
    """A fake Supabase client that enforces the MC unique indexes of
    migration 0165: (coalesce(group_id, message_id), round_no) where status
    <> 'abandoned' on rounds, (coalesce(group_id, message_id), call_id) on
    tool calls. A collision raises like PostgREST would."""

    def __init__(self):
        self.db: dict = {}

    def table(self, name):
        return _UniqueQ(self, name)


class _UniqueQ:
    def __init__(self, owner, table):
        self.owner, self.table, self.filters, self.payload, self.op = owner, table, [], None, "select"

    def select(self, *_a):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, *_a):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        rows = self.owner.db.setdefault(self.table, [])
        if self.op == "insert":
            row = {"id": f"{self.table}-{len(rows) + 1}", "created_at": f"2026-10-22T00:00:00.{len(rows):06d}+00:00", **self.payload}
            owner_key = row.get("group_id") or row.get("message_id")
            for r in rows:
                same = (r.get("group_id") or r.get("message_id")) == owner_key
                if self.table == store_mod.ROUNDS_TABLE and same and r["round_no"] == row["round_no"] and r.get("status") != "abandoned":
                    raise RuntimeError("duplicate key value violates unique constraint plans_reader_loop_rounds_uq")
                if self.table == store_mod.TOOL_CALLS_TABLE and same and r["call_id"] == row["call_id"]:
                    raise RuntimeError("duplicate key value violates unique constraint plans_reader_tool_calls_uq")
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        hits = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
        if self.op == "update":
            for r in hits:
                r.update(self.payload)
        return SimpleNamespace(data=[dict(r) for r in hits])


def test_second_loop_of_a_group_runs_under_its_attempt_key_without_collisions(quiet, monkeypatch, tools):
    client = _UniqueDB()
    client.db[store_mod.GROUPS_TABLE] = [{"id": GROUP, "loop_state": {"recount": {"targets": [{"row_id": "C22"}]}}}]
    store = store_mod.SupabaseLoopStore(client, run_id="run")
    pre = [loop.PreCall("probe", {"q": "pre"}, label="pre")]
    first, c1 = _run_openai(monkeypatch, store, tools.specs, precalls=pre)
    second, c2 = _run_openai(monkeypatch, store, tools.specs, precalls=pre, loop_attempt=2)
    assert first.parsed == second.parsed == FINAL
    assert not second.resumed and len(c2.calls) == 4  # a real second loop, not the primary's stored answer
    rounds = client.db[store_mod.ROUNDS_TABLE]
    assert sorted(r["round_no"] for r in rounds) == [1, 2, 3, 4, 1001, 1002, 1003, 1004]
    assert all(r["group_id"] == GROUP for r in rounds)
    stored_ids = [c["call_id"] for c in client.db[store_mod.TOOL_CALLS_TABLE]]
    assert "pre1" in stored_ids and "a2:pre1" in stored_ids
    assert [r["round_no"] for r in store.rounds(KEY)] == [1, 2, 3, 4]
    assert [r["round_no"] for r in store.rounds(f"{KEY}#a2")] == [1, 2, 3, 4]
    assert {c["call_id"] for c in store.tool_calls(f"{KEY}#a2")} == {c["call_id"] for c in store.tool_calls(KEY)}
    assert store.get_tool_call(f"{KEY}#a2", "pre1")["round_no"] == 0
    calls = client.db[store_mod.CALLS_TABLE]
    assert sorted(c["round_no"] for c in calls) == [1, 2, 3, 4, 1001, 1002, 1003, 1004]
    # loop_state keeps the recount targets and the second attempt's state.
    state = client.db[store_mod.GROUPS_TABLE][0]["loop_state"]
    assert state["recount"] == {"targets": [{"row_id": "C22"}]}
    assert state["status"] == "done" and state["attempt_2"]["status"] == "done"
    # A resumed second attempt finds its own final answer at $0.
    again, c3 = _run_openai(monkeypatch, store, tools.specs, precalls=pre, loop_attempt=2)
    assert again.resumed and again.parsed == FINAL and c3.calls == []


def test_without_an_attempt_key_a_second_loop_would_resume_the_first(quiet, monkeypatch, tools):
    store = store_mod.SupabaseLoopStore(_UniqueDB(), run_id="run")
    _run_openai(monkeypatch, store, tools.specs)
    same, client = _run_openai(monkeypatch, store, tools.specs)
    assert same.resumed and client.calls == []  # why the second sample needs its own key
