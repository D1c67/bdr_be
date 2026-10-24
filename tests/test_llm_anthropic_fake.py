"""Anthropic Messages transport with a fake client (services/llm_anthropic)
and provider parity of the tool loop.

Pinned here:
  kwargs      streaming call with adaptive thinking, explicit effort in
              output_config, strict tools sorted by name, tool_choice auto
              (never any/tool, never budget_tokens), json_schema format
              only with a schema, the system block cached, top-level
              cache_control, at most 4 breakpoints in total, tools kept on
              the final round, refusal fallback through extra_body and the
              beta header when enabled.
  messages    assistant raw blocks (thinking included) replayed unchanged;
              every tool result of a round in ONE user message followed by
              the extra parts; OpenAI-born assistant turns synthesized.
  response    end_turn is final (parsed with a schema), tool_use yields
              {call_id, name, args} dicts from the SDK input (never from
              serialized JSON), max_tokens is reported not raised, refusal
              is LlmBadOutputPermanent with the category, pause_turn is
              retryable; usage includes cache reads and TTL-split writes
              and the fallback model prices the turn.
  parity      the same scripted tool policy produces the same tool trace
              through llm.complete_turn on the OpenAI and Anthropic routes.
No network: the clients are fakes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import llm, llm_anthropic, llm_gate, llm_parts as lp, llm_queue

SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
    "additionalProperties": False,
}
TOOLS = (
    lp.ToolDef("zoom_region", "Zoom", {"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"], "additionalProperties": False}),
    lp.ToolDef("census", "Census", {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"], "additionalProperties": False}),
)


def _req(**over) -> lp.ModelRequest:
    base = dict(
        feature="plans_recount",
        system="Count things.",
        messages=(lp.UserTurn((lp.TextPart("Ledger", cache_break=True), lp.TextPart("Count the troffers."))),),
        tools=TOOLS,
        response_schema=SCHEMA,
        schema_name="recount",
        max_tokens=4000,
    )
    base.update(over)
    return lp.ModelRequest(**base)


# ── kwargs ──────────────────────────────────────────────────────────────────


def test_build_kwargs_shape():
    kw = llm_anthropic.build_kwargs(_req(), model="claude-opus-5-5", effort="medium")
    assert kw["model"] == "claude-opus-5-5"
    assert kw["max_tokens"] == 4000
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert kw["system"] == [{"type": "text", "text": "Count things.", "cache_control": {"type": "ephemeral"}}]
    assert [t["name"] for t in kw["tools"]] == ["census", "zoom_region"]
    assert all(t["strict"] is True and "input_schema" in t for t in kw["tools"])
    assert kw["tool_choice"] == {"type": "auto"}
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert "budget_tokens" not in json.dumps(kw)
    assert "extra_body" not in kw
    # The explicit part break survives: system + top-level + 1 part = 3 <= 4.
    assert kw["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in kw["messages"][0]["content"][1]


def test_build_kwargs_without_schema_and_with_fallback():
    kw = llm_anthropic.build_kwargs(_req(response_schema=None), model="m", effort="high", refusal_fallback=True)
    assert kw["output_config"] == {"effort": "high"}
    assert kw["extra_body"] == {"fallbacks": "default"}
    assert kw["extra_headers"] == {"anthropic-beta": llm_anthropic.FALLBACK_BETA}


def test_tools_stay_on_the_final_round_with_auto_choice():
    kw = llm_anthropic.build_kwargs(_req(tools_enabled=False), model="m", effort="low")
    assert [t["name"] for t in kw["tools"]] == ["census", "zoom_region"]
    assert kw["tool_choice"] == {"type": "auto"}


def test_at_most_four_breakpoints():
    parts = tuple(lp.TextPart(f"p{i}", cache_break=True) for i in range(6))
    kw = llm_anthropic.build_kwargs(_req(messages=(lp.UserTurn(parts),)), model="m", effort="low")
    marks = sum(1 for b in kw["messages"][0]["content"] if "cache_control" in b)
    assert marks == 2  # 4 - system - top-level
    kw1 = llm_anthropic.build_kwargs(_req(messages=(lp.UserTurn(parts),), cache_breakpoints=1), model="m", effort="low")
    assert "cache_control" in kw1["system"][0]
    assert "cache_control" not in kw1
    assert sum(1 for b in kw1["messages"][0]["content"] if "cache_control" in b) == 0
    kw0 = llm_anthropic.build_kwargs(_req(cache_breakpoints=0), model="m", effort="low")
    assert "cache_control" not in kw0["system"][0] and "cache_control" not in kw0


def test_messages_replay_raw_blocks_and_batch_tool_results():
    raw = (
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "t1", "name": "census", "input": {"tag": "(X)"}},
        {"type": "tool_use", "id": "t2", "name": "zoom_region", "input": {"page": 8}},
    )
    turns = (
        lp.UserTurn((lp.TextPart("Q"),)),
        lp.AssistantTurn("anthropic", "m", "msg_1", tool_calls=(lp.ToolCall("t1", "census", {"tag": "(X)"}), lp.ToolCall("t2", "zoom_region", {"page": 8})), raw=raw),
        lp.ToolResultsTurn(
            (
                lp.ToolResultMsg("t1", '{"count": 38}'),
                lp.ToolResultMsg("t2", "zoomed", images=(lp.ImagePart(b"png", 10, 10),), is_error=False),
            ),
            extra_parts=(lp.TextPart("Tool use is finished. Answer now in the required format with what you have."),),
        ),
    )
    msgs = llm_anthropic.anthropic_messages(turns)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == [dict(b) for b in raw]
    results = msgs[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text"]
    assert results[0] == {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": '{"count": 38}'}]}
    assert results[1]["content"][1]["type"] == "image"
    assert results[2]["text"].startswith("Tool use is finished.")


def test_openai_born_assistant_turn_is_synthesized():
    turns = (
        lp.UserTurn((lp.TextPart("Q"),)),
        lp.AssistantTurn("openai", "gpt", "resp_1", tool_calls=(lp.ToolCall("call_1", "census", {"tag": "A"}),), text="thinking aloud"),
        lp.ToolResultsTurn((lp.ToolResultMsg("call_1", "err", is_error=True),)),
    )
    msgs = llm_anthropic.anthropic_messages(turns)
    assert msgs[1]["content"] == [
        {"type": "text", "text": "thinking aloud"},
        {"type": "tool_use", "id": "call_1", "name": "census", "input": {"tag": "A"}},
    ]
    assert msgs[2]["content"][0]["is_error"] is True


# ── response reading ────────────────────────────────────────────────────────


def _usage(inp=1000, read=600, create=300, out=50, split=None):
    creation = SimpleNamespace(**split) if split else None
    return SimpleNamespace(
        input_tokens=inp, cache_read_input_tokens=read, cache_creation_input_tokens=create,
        output_tokens=out, cache_creation=creation,
    )


def _msg(stop="end_turn", content=(), usage=None, model="claude-opus-5-5", mid="msg_1", details=None):
    return SimpleNamespace(
        id=mid, model=model, stop_reason=stop, stop_details=details,
        content=list(content), usage=usage if usage is not None else _usage(),
    )


class Block:
    """A pydantic-like block with model_dump (what the SDK hands back)."""

    def __init__(self, **fields):
        self._fields = fields
        for k, v in fields.items():
            setattr(self, k, v)

    def model_dump(self, mode="json", exclude_none=True):
        return dict(self._fields)


def test_usage_splits_cache_writes_by_ttl_and_falls_back_to_5m():
    u = llm_anthropic.anthropic_usage(_msg(usage=_usage(split={"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 100})))
    assert (u.input_tokens, u.cached_tokens, u.output_tokens) == (1900, 600, 50)
    assert (u.cache_write_5m_tokens, u.cache_write_1h_tokens) == (200, 100)
    assert u.reasoning_tokens == 0
    u2 = llm_anthropic.anthropic_usage(_msg(usage=_usage()))
    assert (u2.cache_write_5m_tokens, u2.cache_write_1h_tokens) == (300, 0)


def test_read_final_with_schema_parses_and_prices_by_answering_model():
    resp = _msg(content=[Block(type="thinking", thinking="", signature="s"), Block(type="text", text='{"count": 17}')], model="claude-sonnet-5-5")
    turn = llm_anthropic.read_message(resp, req=_req(), model="claude-opus-5-5")
    assert turn.stop_reason == "final" and turn.parsed == {"count": 17}
    assert turn.model == "claude-sonnet-5-5" and turn.provider == "anthropic"
    assert turn.raw_assistant[0] == {"type": "thinking", "thinking": "", "signature": "s"}
    # input 1000 is the UNCACHED count on the wire (reads and writes are
    # added back): 1000 x 2 + 600 x 0.1 + 300 x 2 x 1.25 + 50 x 10
    # = 2000 + 60 + 750 + 500 = 3310 / 1e6
    assert str(turn.cost_usd) == "0.003310"
    assert turn.usage.input_tokens == 1900


def test_read_tool_use_takes_inputs_as_dicts():
    resp = _msg(stop="tool_use", content=[Block(type="tool_use", id="toolu_1", name="census", input={"tag": "(X)"})])
    turn = llm_anthropic.read_message(resp, req=_req(), model="m")
    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls == [{"call_id": "toolu_1", "name": "census", "args": {"tag": "(X)"}}]
    assert turn.parsed is None
    bad = _msg(stop="tool_use", content=[Block(type="tool_use", id="", name="census", input={})])
    with pytest.raises(llm.LlmBadOutput):
        llm_anthropic.read_message(bad, req=_req(), model="m")


def test_read_max_tokens_reported_refusal_permanent_pause_retryable():
    cut = llm_anthropic.read_message(_msg(stop="max_tokens", content=[Block(type="text", text="{")]), req=_req(), model="m")
    assert cut.stop_reason == "max_tokens" and cut.parsed is None
    with pytest.raises(llm.LlmBadOutputPermanent, match="cyber"):
        llm_anthropic.read_message(_msg(stop="refusal", details=SimpleNamespace(category="cyber")), req=_req(), model="m")
    with pytest.raises(llm.LlmBadOutput):
        llm_anthropic.read_message(_msg(stop="pause_turn"), req=_req(), model="m")


# ── the call through a fake client ───────────────────────────────────────────


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeAnthropic:
    def __init__(self, *messages):
        self.queue = list(messages)
        self.calls: list[dict] = []
        self.options: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.queue.pop(0))

    def with_options(self, **kw):
        self.options.append(kw)
        return self


@pytest.fixture
def logged(monkeypatch):
    llm_gate.reset_gates()
    rows: list[dict] = []
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: rows.append(kw))
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    yield rows
    llm_gate.reset_gates()


def _settings(routes: str = "") -> Settings:
    return Settings(
        _env_file=None, openai_api_key="o-key", anthropic_api_key="a-key",
        plans_reader_enabled=True, plans_reader_routes=routes,
    )


ANTHROPIC_ROUTES = json.dumps({"recount": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"}})


def test_anthropic_turn_renews_lease_and_logs(monkeypatch, logged):
    client = FakeAnthropic(_msg(content=[Block(type="text", text='{"count": 3}')]))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    renewed = []
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: renewed.append(1) or True)
    turn = llm.complete_turn("plans_recount", _req(), settings=_settings(ANTHROPIC_ROUTES))
    assert turn.parsed == {"count": 3} and turn.provider == "anthropic"
    assert renewed == [1]
    assert client.options == [{"timeout": 600.0, "max_retries": 0}]
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["effort"] == "high"
    assert kw["extra_body"] == {"fallbacks": "default"}  # the refusal fallback defaults on
    assert logged[-1]["provider"] == "anthropic" and logged[-1]["ok"] is True
    assert logged[-1]["input_tokens"] == 1900 and logged[-1]["cost_usd"] is not None
    assert llm.last_usage().cached_tokens == 600


def test_lost_lease_refuses_to_send(monkeypatch, logged):
    client = FakeAnthropic(_msg())
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: False)
    with pytest.raises(llm.LlmLeaseLost):
        llm.complete_turn("plans_recount", _req(), settings=_settings(ANTHROPIC_ROUTES))
    assert client.calls == []


def test_request_over_the_anthropic_limits_is_refused_before_sending(monkeypatch, logged):
    client = FakeAnthropic(_msg())
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    big = _req(messages=(lp.UserTurn((lp.ImagePart(b"x", 4000, 4000),)),))
    with pytest.raises(llm.LlmBadOutputPermanent, match="too large"):
        llm.complete_turn("plans_recount", big, settings=_settings(ANTHROPIC_ROUTES))
    assert client.calls == []


# ── provider parity: one scripted policy, two transports, same trace ────────


POLICY = [
    [("census", {"tag": "(X)"})],
    [("zoom_region", {"page": 8}), ("zoom_region", {"page": 7})],
    None,  # final answer
]
FINAL = {"count": 38}


class FakeOpenAIPolicy:
    def __init__(self):
        self.round = 0
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def with_options(self, **kw):
        return self

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        step = POLICY[self.round]
        self.round += 1
        usage = SimpleNamespace(
            input_tokens=1000 * self.round, input_tokens_details=SimpleNamespace(cached_tokens=0),
            output_tokens=100, output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        )
        if step is None or "tools" not in kwargs:
            return SimpleNamespace(id=f"resp_{self.round}", status="completed", incomplete_details=None,
                                   output_text=json.dumps(FINAL), output=[], usage=usage)
        items = [
            SimpleNamespace(type="function_call", call_id=f"call_{self.round}_{i}", name=name, arguments=json.dumps(args))
            for i, (name, args) in enumerate(step)
        ]
        return SimpleNamespace(id=f"resp_{self.round}", status="completed", incomplete_details=None,
                               output_text="", output=items, usage=usage)


class FakeAnthropicPolicy:
    def __init__(self):
        self.round = 0
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def with_options(self, **kw):
        return self

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        step = POLICY[self.round]
        self.round += 1
        usage = _usage(inp=1000 * self.round, read=0, create=0, out=100)
        if step is None:
            return FakeStream(_msg(content=[Block(type="text", text=json.dumps(FINAL))], usage=usage, mid=f"msg_{self.round}"))
        blocks = [Block(type="thinking", thinking="", signature=f"sig{self.round}")] + [
            Block(type="tool_use", id=f"toolu_{self.round}_{i}", name=name, input=dict(args))
            for i, (name, args) in enumerate(step)
        ]
        return FakeStream(_msg(stop="tool_use", content=blocks, usage=usage, mid=f"msg_{self.round}"))


def _drive(feature: str, settings: Settings, max_rounds: int = 5) -> list[list[tuple[str, dict]]]:
    """A minimal loop over complete_turn: serve every tool with a canned
    text, append the assistant and result turns, stop on final."""
    trace: list[list[tuple[str, dict]]] = []
    messages: tuple[lp.Turn, ...] = (lp.UserTurn((lp.TextPart("Ledger", cache_break=True), lp.TextPart("Count."))),)
    previous = None
    steer_at = len(POLICY)  # the round the scripted policy answers on
    for r in range(1, max_rounds + 1):
        final = r >= steer_at
        req = _req(messages=messages, tools_enabled=not final, previous_response_id=previous)
        turn = llm.complete_turn(feature, req, settings=settings)
        if turn.stop_reason == "final":
            assert turn.parsed == FINAL
            return trace
        assert turn.stop_reason == "tool_use"
        trace.append([(c["name"], c["args"]) for c in turn.tool_calls])
        results = tuple(lp.ToolResultMsg(c["call_id"], f"served {c['name']}") for c in turn.tool_calls)
        extra = (lp.TextPart("Tool use is finished. Answer now in the required format with what you have."),) if r + 1 == steer_at else ()
        messages = messages + (turn.assistant_turn(), lp.ToolResultsTurn(results, extra_parts=extra))
        previous = turn.response_id
    raise AssertionError("no final answer")


def test_same_policy_gives_the_same_tool_trace_on_both_providers(monkeypatch, logged):
    openai_client = FakeOpenAIPolicy()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: openai_client)
    openai_trace = _drive("plans_recount", _settings())
    anthropic_client = FakeAnthropicPolicy()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: anthropic_client)
    anthropic_trace = _drive("plans_recount", _settings(ANTHROPIC_ROUTES))
    expected = [[(n, a) for n, a in step] for step in POLICY if step is not None]
    assert openai_trace == expected == anthropic_trace
    # OpenAI chained on response ids and sent only the new items after round 1.
    assert openai_client.calls[1]["previous_response_id"] == "resp_1"
    assert [i["type"] for i in openai_client.calls[1]["input"]] == ["function_call_output"]
    assert [i["type"] for i in openai_client.calls[2]["input"][:2]] == ["function_call_output", "function_call_output"]
    assert openai_client.calls[2]["input"][2]["role"] == "user"  # the finish steer
    # Anthropic replayed the whole transcript with the thinking blocks untouched.
    last = anthropic_client.calls[2]["messages"]
    assert [m["role"] for m in last] == ["user", "assistant", "user", "assistant", "user"]
    assert last[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig1"}
    assert len(last[4]["content"]) == 3  # two tool results + the finish steer, one message
    assert all("tools" in c for c in anthropic_client.calls)
    assert [log["provider"] for log in logged] == ["openai"] * 3 + ["anthropic"] * 3
