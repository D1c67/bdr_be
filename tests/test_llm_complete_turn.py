"""llm.complete_turn, route resolution and the complete_parts contract.

Pinned here:
  features    the six new AI Estimating feature keys are registered through
              _plans_feature (switch PLANS_READER_ENABLED, never
              self-hosted) and resolve to OpenAI on OPENAI_PLANS_MODEL when
              no route is set, exactly like the four original ones.
  routes      plans_reader_model_routes: a step the env does not name keeps
              OPENAI_PLANS_MODEL; a named step uses its provider, model and
              effort; the snapshot wins over the live settings; the 8.3
              defaults are all OpenAI; routes_snapshot is JSON-ready;
              scope_attrib takes provider and model from main (low
              effort) unless named; modify has no route step;
              complete_parts applies the route effort when the caller
              passes none.
  snapshot    complete_parts sends byte-identical kwargs to the fixture
              captured before the WS2 change (six representative calls).
  turn        the OpenAI side of complete_turn builds a Responses request
              from the neutral transcript (full replay without a previous
              id), reports max_tokens instead of raising, chains with
              previous_response_id, and refuses self-hosted routes.
  pricing     an unpriced model is surfaced: ModelTurn.cost_usd is None and
              last_cost_usd() is None while usage is still recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import PLANS_READER_DEFAULT_ROUTES, Settings
from app.services import llm, llm_gate, llm_parts as lp, llm_queue
from app.services import plans_reader_model_routes as routes

FIXTURE = Path(__file__).parent / "fixtures" / "ai_estimating" / "complete_parts_kwargs_before.json"

NEW_FEATURES = ("plans_recount", "plans_review", "plans_revise", "plans_derive", "plans_scope_attrib", "pricing_quote")
ALL_PLANS = ("plans_sub", "plans_main", "plans_modify", "plans_chat") + NEW_FEATURES


def _s(**over) -> Settings:
    base = dict(_env_file=None, openai_api_key="o-key", anthropic_api_key="a-key", plans_reader_enabled=True)
    base.update(over)
    return Settings(**base)


class FakeOpenAI:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def with_options(self, **kw):
        self.calls.append({"__with_options__": kw})
        return self


def _usage(inp=1000, cached=200, out=300, reasoning=100):
    return SimpleNamespace(
        input_tokens=inp, input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens=out, output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def _resp(text="", *, output=(), usage=None, status="completed", rid="resp_1", reason=None):
    return SimpleNamespace(
        id=rid, status=status, incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output_text=text, output=list(output), usage=usage if usage is not None else _usage(),
    )


@pytest.fixture
def quiet(monkeypatch):
    llm_gate.reset_gates()
    rows: list[dict] = []
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: rows.append(kw))
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    yield rows
    llm_gate.reset_gates()


# ── features ────────────────────────────────────────────────────────────────


def test_new_features_registered_like_the_original_plans_features():
    s = _s()
    assert llm.PLANS_FEATURES == ALL_PLANS
    for feature in NEW_FEATURES:
        spec = llm._FEATURES[feature]
        assert spec.openai_only is True and spec.switch is not None
        route = llm.resolve(feature, s)
        assert (route.provider, route.model) == ("openai", "gpt-6-astra")
        assert llm.is_configured(feature, s) is True
        assert llm.unavailable_reason(feature, _s(plans_reader_enabled=False)) == llm.PLANS_READER_OFF_MESSAGE
        # Never self-hosted, whatever the master switch says.
        sh = _s(full_self_hosted_llms_enabled=True, self_hosted_llm_local_base_url="https://box", self_hosted_llm_local_model="qwen")
        assert llm.resolve(feature, sh).provider == "openai"
    assert llm._FEATURES["plans_modify"].route_step is None
    assert llm._FEATURES["plans_recount"].route_step == "recount"
    assert llm._FEATURES["pricing_quote"].route_step == "quote"


def test_openai_plans_model_still_moves_unrouted_steps():
    s = _s(openai_plans_model="x-1")
    for feature in ALL_PLANS:
        assert llm.resolve(feature, s).model == "x-1"


# ── routes ──────────────────────────────────────────────────────────────────


def test_default_routes_are_all_openai_and_unrouted_steps_follow_the_plans_model():
    s = _s(openai_plans_model="x-2")
    for step in routes.STEPS:
        r = routes.resolve_step(step, s)
        assert (r.provider, r.model, r.explicit) == ("openai", "x-2", False)
        assert PLANS_READER_DEFAULT_ROUTES[step]["provider"] == "openai"
    assert routes.resolve_step("scope_attrib", s).effort == "low"
    assert routes.resolve_step("sub", s).effort == "medium" == s.plans_reader_reasoning_effort
    assert routes.resolve_step("main", s).budget_usd == 6


def test_named_steps_use_their_route_and_snapshot_wins():
    s = _s(plans_reader_routes=json.dumps({"review": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"}}))
    r = routes.resolve_step("review", s)
    assert (r.provider, r.model, r.effort, r.explicit) == ("anthropic", "claude-opus-5-5", "high", True)
    assert routes.resolve_step("main", s).model == "gpt-6-astra"
    assert routes.explicit_steps(s) == frozenset({"review"})
    assert routes.providers_in_use(s) == frozenset({"openai", "anthropic"})
    route = llm.resolve("plans_review", s)
    assert (route.provider, route.model, route.api_key, route.step, route.effort) == ("anthropic", "claude-opus-5-5", "a-key", "review", "high")
    snap = routes.routes_snapshot(s)
    assert snap["review"]["provider"] == "anthropic" and snap["review"]["budget_usd"] == 4.0
    json.dumps(snap)
    later = _s()  # the env changed back; the run keeps its snapshot
    assert routes.resolve_step("review", later, snapshot=snap).provider == "anthropic"
    assert routes.resolve_step("sub", later, snapshot=snap).model == "gpt-6-astra"
    assert routes.resolve_feature("plans_modify", later) is None
    assert routes.resolve_feature("plans_sub", later, step="escalation").step == "escalation"
    assert routes.step_for_kind("escalation") == "escalation" and routes.step_for_feature("plans_chat") == "chat"
    with pytest.raises(ValueError):
        routes.resolve_step("nope", later)


def test_scope_attrib_follows_the_main_route_at_low_effort():
    s = _s(plans_reader_routes=json.dumps({"main": {"model": "gpt-5.4"}}))
    r = routes.resolve_step("scope_attrib", s)
    assert (r.provider, r.model, r.effort, r.explicit) == ("openai", "gpt-5.4", "low", False)
    tools = _s(plans_reader_consolidator_mode="tools", plans_reader_routes=json.dumps({"main": {"provider": "anthropic", "model": "claude-sonnet-5-5"}}))
    r = routes.resolve_step("scope_attrib", tools)
    assert (r.provider, r.model, r.effort) == ("anthropic", "claude-sonnet-5-5", "low")
    assert llm.resolve("plans_scope_attrib", tools).provider == "anthropic"
    # A snapshot without a scope_attrib row derives from the snapshot's main.
    snap = {"main": {"provider": "openai", "model": "gpt-5.4-mini", "effort": "high"}}
    assert routes.resolve_step("scope_attrib", _s(), snapshot=snap).model == "gpt-5.4-mini"
    # Naming scope_attrib in the env stops the derivation.
    named = _s(plans_reader_routes=json.dumps({"main": {"model": "gpt-5.4"}, "scope_attrib": {"model": "gpt-5.4-nano"}}))
    r = routes.resolve_step("scope_attrib", named)
    assert (r.model, r.effort, r.explicit) == ("gpt-5.4-nano", "low", True)


def test_kind_steps_agree_with_feature_steps():
    from app.services.plans_reader_agents import FEATURE_BY_KIND

    assert routes.step_for_kind("modify") is None
    for kind, feature in FEATURE_BY_KIND.items():
        if kind == "escalation":
            continue  # plans_sub feature, but its own route row (KIND_STEP comment)
        assert routes.step_for_kind(kind) == routes.step_for_feature(feature), kind


def test_complete_parts_applies_the_route_effort(monkeypatch, quiet):
    def effort_of(feature, s, **kw):
        client = FakeOpenAI(_resp('{"ok": true}'))
        monkeypatch.setattr(llm, "_client_for", lambda route, settings, c=client: c)
        llm.complete_parts(feature, system="x", parts=PARTS[:1], settings=s, **kw)
        return client.calls[1].get("reasoning")

    routed = _s(plans_reader_routes=json.dumps({"sub": {"model": "gpt-5.4", "effort": "high"}, "chat": {"effort": "low"}}))
    assert effort_of("plans_sub", routed) == {"effort": "high"}
    assert effort_of("plans_chat", routed) == {"effort": "low"}
    assert effort_of("plans_sub", routed, reasoning_effort="minimal") == {"effort": "minimal"}  # the caller wins
    assert effort_of("plans_main", routed) == {"effort": "medium"}  # the 8.3 row
    # plans_modify has no route step: the run-wide default.
    assert effort_of("plans_modify", _s(plans_reader_reasoning_effort="high")) == {"effort": "high"}
    # A null route effort (chat's default) inherits the run-wide default.
    assert effort_of("plans_chat", _s(plans_reader_reasoning_effort="high")) == {"effort": "high"}


# ── complete_parts snapshot ─────────────────────────────────────────────────


PARTS = [
    {"type": "input_text", "text": "Ledger"},
    {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "original"},
    {"type": "input_file", "filename": "spec.pdf", "file_data": "data:application/pdf;base64,BB"},
]
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
TOOLS = [{"type": "function", "name": "get_brief", "description": "d", "parameters": SCHEMA, "strict": True}]
SNAPSHOT_CASES = [
    ("json_sub", dict(feature="plans_sub", system="You read plans.", parts=PARTS, schema=SCHEMA, schema_name="sub_output")),
    ("text_no_schema_effort", dict(feature="plans_main", system="sys", parts=PARTS[:1], reasoning_effort="high", max_tokens=1234, timeout=33.0)),
    ("tools_first_round", dict(feature="plans_chat", system="chat", parts=PARTS[:1], tools=TOOLS, schema=SCHEMA, schema_name="chat")),
    ("tool_outputs_with_parts", dict(feature="plans_chat", system="chat", parts=PARTS[:2], tools=TOOLS, previous_response_id="resp_0", tool_outputs=[{"call_id": "c1", "output": {"a": 1}}, {"call_id": "c2", "output": "plain"}])),
    ("tool_outputs_only", dict(feature="plans_chat", system="chat", parts=[], previous_response_id="resp_0", tool_outputs=[{"call_id": "c1", "output": "x"}], max_tokens=50)),
    ("modify_continue", dict(feature="plans_modify", system="m", parts=PARTS[:1], previous_response_id="resp_9", schema=SCHEMA)),
]


def test_complete_parts_kwargs_are_byte_identical_to_the_pre_change_snapshot(monkeypatch, quiet):
    before = json.loads(FIXTURE.read_text())
    after: dict[str, list] = {}
    s = _s()
    for name, kw in SNAPSHOT_CASES:
        client = FakeOpenAI(_resp('{"ok": true}', usage=None))
        monkeypatch.setattr(llm, "_client_for", lambda route, settings, c=client: c)
        kw = dict(kw)
        feature = kw.pop("feature")
        llm.complete_parts(feature, settings=s, **kw)
        after[name] = client.calls
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)


# ── complete_turn on OpenAI ─────────────────────────────────────────────────


def _req(**over) -> lp.ModelRequest:
    base = dict(
        feature="plans_recount", system="Count.",
        messages=(lp.UserTurn((lp.TextPart("Ledger"),)),),
        tools=(lp.ToolDef("zoom_region", "z", SCHEMA), lp.ToolDef("census", "c", SCHEMA)),
        response_schema=SCHEMA, schema_name="recount", max_tokens=2_000, cache_key="plans:run1:recount",
    )
    base.update(over)
    return lp.ModelRequest(**base)


def test_first_round_request_shape_and_tool_round(monkeypatch, quiet):
    call = SimpleNamespace(type="function_call", call_id="call_1", name="census", arguments='{"tag": "(X)"}')
    client = FakeOpenAI(_resp("", output=[call]))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    turn = llm.complete_turn("plans_recount", _req(), settings=_s())
    assert turn.stop_reason == "tool_use" and turn.parsed is None
    assert turn.tool_calls == [{"call_id": "call_1", "name": "census", "args": {"tag": "(X)"}}]
    assert (turn.provider, turn.model, turn.response_id) == ("openai", "gpt-6-astra", "resp_1")
    assert client.calls[0] == {"__with_options__": {"timeout": 600.0, "max_retries": 0}}
    kw = client.calls[1]
    assert kw["model"] == "gpt-6-astra" and kw["instructions"] == "Count."
    assert kw["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Ledger"}]}]
    assert [t["name"] for t in kw["tools"]] == ["census", "zoom_region"]
    assert kw["reasoning"] == {"effort": "medium"} and kw["max_output_tokens"] == 2_000
    assert kw["text"]["format"]["name"] == "recount" and kw["prompt_cache_key"] == "plans:run1:recount"
    assert quiet[-1]["feature"] == "plans_recount" and quiet[-1]["cost_usd"] is not None


def test_chained_round_sends_only_new_items_and_final_round_omits_tools(monkeypatch, quiet):
    client = FakeOpenAI(_resp('{"ok": true}', rid="resp_2"))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    messages = (
        lp.UserTurn((lp.TextPart("Ledger"),)),
        lp.AssistantTurn("openai", "gpt-6-astra", "resp_1", tool_calls=(lp.ToolCall("call_1", "census", {"tag": "(X)"}),)),
        lp.ToolResultsTurn(
            (lp.ToolResultMsg("call_1", "38 found", images=(lp.ImagePart(b"png", 10, 10),)),),
            extra_parts=(lp.TextPart("Tool use is finished."),),
        ),
    )
    turn = llm.complete_turn("plans_recount", _req(messages=messages, previous_response_id="resp_1", tools_enabled=False), settings=_s())
    assert turn.stop_reason == "final" and turn.parsed == {"ok": True}
    kw = client.calls[1]
    assert kw["previous_response_id"] == "resp_1" and "tools" not in kw
    assert kw["input"][0] == {"type": "function_call_output", "call_id": "call_1", "output": "38 found"}
    user = kw["input"][1]
    assert user["role"] == "user"
    assert [p["type"] for p in user["content"]] == ["input_text", "input_image", "input_text"]
    assert user["content"][0]["text"] == "Images for call call_1:"


def test_full_replay_without_a_previous_id(monkeypatch, quiet):
    client = FakeOpenAI(_resp('{"ok": true}'))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    messages = (
        lp.UserTurn((lp.TextPart("Ledger"),)),
        lp.AssistantTurn("openai", "gpt-6-astra", "resp_1", tool_calls=(lp.ToolCall("call_1", "census", {"tag": "(X)"}),)),
        lp.ToolResultsTurn((lp.ToolResultMsg("call_1", "38 found"),)),
    )
    llm.complete_turn("plans_recount", _req(messages=messages), settings=_s())
    items = client.calls[1]["input"]
    assert [i.get("type") or i.get("role") for i in items] == ["user", "function_call", "function_call_output"]
    assert items[1] == {"type": "function_call", "call_id": "call_1", "name": "census", "arguments": '{"tag": "(X)"}'}
    assert "previous_response_id" not in client.calls[1]


def test_max_output_tokens_is_reported_not_raised_but_filters_and_refusals_are(monkeypatch, quiet):
    client = FakeOpenAI(_resp("{", status="incomplete", reason="max_output_tokens"))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    turn = llm.complete_turn("plans_recount", _req(), settings=_s())
    assert turn.stop_reason == "max_tokens" and turn.parsed is None and turn.usage.input_tokens == 1000
    client = FakeOpenAI(_resp("", status="incomplete", reason="content_filter"))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(llm.LlmBadOutputPermanent):
        llm.complete_turn("plans_recount", _req(), settings=_s())
    refusal = SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal="no")])
    client = FakeOpenAI(_resp("", output=[refusal]))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(llm.LlmBadOutputPermanent):
        llm.complete_turn("plans_recount", _req(), settings=_s())
    client = FakeOpenAI(_resp("", status="incomplete", reason="other"))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(llm.LlmBadOutput):
        llm.complete_turn("plans_recount", _req(), settings=_s())


def test_effort_precedence_and_step_override(monkeypatch, quiet):
    s = _s(plans_reader_routes=json.dumps({"escalation": {"model": "gpt-5.4", "effort": "low"}}))
    client = FakeOpenAI(_resp('{"ok": true}'), _resp('{"ok": true}'), _resp('{"ok": true}'))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    llm.complete_turn("plans_sub", _req(step="escalation"), settings=s)
    assert client.calls[1]["model"] == "gpt-5.4" and client.calls[1]["reasoning"] == {"effort": "low"}
    llm.complete_turn("plans_sub", _req(step="escalation", effort="xhigh"), settings=s)
    assert client.calls[3]["reasoning"] == {"effort": "xhigh"}
    # A snapshotted route wins over the live settings.
    snap = {"provider": "openai", "model": "gpt-5.4-mini", "effort": "high", "budget_usd": None, "tools": True}
    llm.complete_turn("plans_sub", _req(step="escalation", route=snap), settings=_s())
    assert client.calls[5]["model"] == "gpt-5.4-mini" and client.calls[5]["reasoning"] == {"effort": "high"}
    with pytest.raises(ValueError, match="step"):
        llm.complete_turn("plans_modify", _req(route=snap), settings=_s())


def test_unpriced_model_is_surfaced_as_no_cost(monkeypatch, quiet):
    s = _s(openai_plans_model="gpt-5.3-mini")
    client = FakeOpenAI(_resp('{"ok": true}'))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    turn = llm.complete_turn("plans_recount", _req(), settings=s)
    assert turn.model == "gpt-5.3-mini" and turn.cost_usd is None
    assert turn.usage.input_tokens == 1000 and llm.last_usage().input_tokens == 1000
    assert llm.last_cost_usd() is None and quiet[-1]["cost_usd"] is None


def test_missing_key_and_self_hosted_routes_refuse(monkeypatch, quiet):
    with pytest.raises(llm.LlmNotConfigured, match="OPENAI_API_KEY"):
        llm.complete_turn("plans_recount", _req(), settings=_s(openai_api_key=""))
    with pytest.raises(llm.LlmNotConfigured, match="ANTHROPIC_API_KEY"):
        llm.complete_turn(
            "plans_recount", _req(),
            settings=_s(anthropic_api_key="", plans_reader_routes=json.dumps({"recount": {"provider": "anthropic", "model": "claude-opus-5-5"}})),
        )
    sh = _s(full_self_hosted_llms_enabled=True, self_hosted_llm_local_base_url="https://box", self_hosted_llm_local_model="qwen")
    with pytest.raises(llm.LlmNotConfigured, match="self-hosted"):
        llm.complete_turn("boq", _req(feature="boq"), settings=sh)


def test_openai_image_limits_checked_before_sending(monkeypatch, quiet):
    client = FakeOpenAI(_resp('{"ok": true}'))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    huge = _req(messages=(lp.UserTurn((lp.ImagePart(b"png", 20_000, 20_000),)),))
    with pytest.raises(llm.LlmBadOutputPermanent, match="too large"):
        llm.complete_turn("plans_recount", huge, settings=_s())
    assert client.calls == []
