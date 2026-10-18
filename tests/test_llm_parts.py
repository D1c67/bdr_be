"""OpenAI Responses transport with mixed content parts (llm.complete_parts)
and the token/cost accounting every LLM call now carries (0152).

Invariants pinned here:

  request     instructions = system; one user message carrying the parts in
              order; reasoning, tools, max_output_tokens and strict
              json_schema only when given (reasoning defaults to
              PLANS_READER_REASONING_EFFORT for plans_*); tool_outputs become
              function_call_output items (dict outputs JSON-encoded) with
              previous_response_id, optionally followed by a user message;
              the client gets the plans timeout and no SDK retries.
  response    parsed JSON + text + usage + response id + tool calls
              ({call_id, name, arguments: dict}); parsed is None when the
              model answered with tool calls; status incomplete, bad tool
              arguments and an empty call id raise LlmBadOutput; output
              limit, content filter and refusals are LlmBadOutputPermanent
              (bad_input, not retried); missing usage reads as zeros.
  routing     anthropic and self-hosted routes raise LlmNotConfigured.
  accounting  _guarded logs tokens and cost for OpenAI, Anthropic and
              self-hosted usage shapes (success and failure), exposes them
              through last_usage()/last_cost_usd(), and log_call writes the
              new columns (and leaves them out when usage is unknown, or
              retries without them on a database that predates 0152).
              last_usage()/last_cost_usd() are cleared at the start of every
              complete_* call, so an early failure never shows stale spend.
  lease       inside a queue job the lease is renewed before the request;
              a lost lease refuses to send.
"""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import llm, llm_gate


def _s(**over) -> Settings:
    base = dict(_env_file=None, openai_api_key="o-key", anthropic_api_key="a-key")
    base.update(over)
    return Settings(**base)


def _usage(inp=1000, cached=200, out=300, reasoning=100):
    return SimpleNamespace(
        input_tokens=inp,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens=out,
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def _resp(text="", *, output=(), usage=None, status="completed", rid="resp_1", reason=None):
    return SimpleNamespace(
        id=rid,
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output_text=text,
        output=list(output),
        usage=usage,
    )


def _fn_call(call_id="call_1", name="get_brief", arguments='{"section": "risks"}'):
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=arguments)


class FakeOpenAI:
    """Exposes only responses.create (the Responses wire) plus with_options,
    recording every request and the options the caller set."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []
        self.options: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def with_options(self, **kw):
        self.options.append(kw)
        return self


@pytest.fixture
def logged(monkeypatch):
    llm_gate.reset_gates()
    rows: list[dict] = []
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: rows.append(kw))
    yield rows
    llm_gate.reset_gates()


def _install(monkeypatch, client):
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    return client


PARTS = [
    {"type": "input_text", "text": "Ledger"},
    {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "original"},
    {"type": "input_file", "filename": "spec.pdf", "file_data": "data:application/pdf;base64,BB"},
]
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


# ── Request shape ───────────────────────────────────────────────────────────


def test_json_call_builds_the_responses_request_and_parses(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI(_resp('{"ok": true}', usage=_usage())))
    out = llm.complete_parts(
        "plans_sub",
        system="You read plans.",
        parts=PARTS,
        schema=SCHEMA,
        schema_name="sub_output",
        reasoning_effort="medium",
        max_tokens=64_000,
        settings=_s(),
    )
    (req,) = client.calls
    assert req["model"] == "gpt-6-astra"
    assert req["instructions"] == "You read plans."
    assert req["input"] == [{"role": "user", "content": PARTS}]
    assert req["reasoning"] == {"effort": "medium"}
    assert req["max_output_tokens"] == 64_000
    assert req["text"] == {
        "format": {"type": "json_schema", "name": "sub_output", "schema": SCHEMA, "strict": True}
    }
    assert "tools" not in req and "previous_response_id" not in req
    # Long plans calls: the configured timeout, and the queue owns retries.
    assert client.options == [{"timeout": 600.0, "max_retries": 0}]

    assert out.parsed == {"ok": True}
    assert out.text == '{"ok": true}'
    assert out.response_id == "resp_1"
    assert out.tool_calls == []
    assert out.usage == llm.LlmUsage(1000, 200, 300, 100)
    assert out.model == "gpt-6-astra"
    # 800 x $10 + 200 x $1 + 300 x $50 per 1M
    assert out.cost_usd == Decimal("0.023200")


def test_optional_fields_stay_out_of_a_plain_text_call(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI(_resp("Plain answer.")))
    out = llm.complete_parts(
        "plans_chat",
        system="s",
        parts=[{"type": "input_text", "text": "hi"}],
        timeout=42,
        settings=_s(),
    )
    (req,) = client.calls
    for key in ("tools", "text", "max_output_tokens", "previous_response_id"):
        assert key not in req
    # Plans features always carry the configured effort.
    assert req["reasoning"] == {"effort": "medium"}
    assert client.options == [{"timeout": 42.0, "max_retries": 0}]
    assert out.parsed is None and out.text == "Plain answer."
    # No usage reported -> zeros on the result, NULL columns in the log.
    assert out.usage == llm.LlmUsage()
    assert out.cost_usd is None
    assert logged[0]["input_tokens"] is None and logged[0]["cost_usd"] is None


def test_timeout_setting_drives_the_default(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI(_resp("x")))
    llm.complete_parts(
        "plans_main",
        system="s",
        parts=[{"type": "input_text", "text": "x"}],
        settings=_s(plans_reader_call_timeout_seconds=300),
    )
    assert client.options == [{"timeout": 300.0, "max_retries": 0}]


# ── Tools ───────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "name": "get_brief",
        "description": "Return the brief.",
        "parameters": {
            "type": "object",
            "properties": {"section": {"type": "string"}},
            "required": ["section"],
            "additionalProperties": False,
        },
        "strict": True,
    }
]


def test_tool_calls_are_returned_and_parsed_is_none(monkeypatch, logged):
    resp = _resp(
        "",
        output=[
            SimpleNamespace(type="reasoning", id="rs_1"),
            _fn_call("call_1", "get_brief", '{"section": "risks"}'),
            _fn_call("call_2", "list_documents", ""),
        ],
        usage=_usage(),
        rid="resp_tools",
    )
    client = _install(monkeypatch, FakeOpenAI(resp))
    out = llm.complete_parts(
        "plans_chat", system="s", parts=PARTS[:1], schema=SCHEMA, tools=TOOLS, settings=_s()
    )
    assert client.calls[0]["tools"] == TOOLS
    assert out.response_id == "resp_tools"
    assert out.tool_calls == [
        {"call_id": "call_1", "name": "get_brief", "arguments": {"section": "risks"}},
        {"call_id": "call_2", "name": "list_documents", "arguments": {}},
    ]
    # The model asked for tools instead of answering: nothing to parse yet.
    assert out.parsed is None


def test_tool_outputs_continue_the_previous_response(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI(_resp('{"ok": false}', usage=_usage())))
    out = llm.complete_parts(
        "plans_chat",
        system="s",
        parts=[],
        schema=SCHEMA,
        tools=TOOLS,
        previous_response_id="resp_tools",
        tool_outputs=[
            {"call_id": "call_1", "output": "The brief text."},
            {"call_id": "call_2", "output": [{"id": "d1", "name": "E-101"}]},
        ],
        settings=_s(),
    )
    (req,) = client.calls
    assert req["previous_response_id"] == "resp_tools"
    assert req["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "The brief text."},
        {
            "type": "function_call_output",
            "call_id": "call_2",
            "output": json.dumps([{"id": "d1", "name": "E-101"}]),
        },
    ]
    assert out.parsed == {"ok": False}


def test_tool_outputs_can_carry_page_images_after_them(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI(_resp("Read them.")))
    llm.complete_parts(
        "plans_chat",
        system="s",
        parts=PARTS[1:2],
        previous_response_id="resp_x",
        tool_outputs=[{"call_id": "call_9", "output": {"pages": [3], "status": "attached"}}],
        settings=_s(),
    )
    req = client.calls[0]
    assert req["input"][0]["type"] == "function_call_output"
    assert req["input"][1] == {"role": "user", "content": PARTS[1:2]}


def test_follow_up_turn_with_previous_response_id_only(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI(_resp("Next.")))
    llm.complete_parts(
        "plans_chat",
        system="s",
        parts=[{"type": "input_text", "text": "and the panels?"}],
        previous_response_id="resp_prev",
        settings=_s(),
    )
    req = client.calls[0]
    assert req["previous_response_id"] == "resp_prev"
    assert req["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "and the panels?"}]}
    ]


def test_tool_outputs_without_a_previous_response_are_refused(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI())
    with pytest.raises(ValueError, match="previous_response_id"):
        llm.complete_parts(
            "plans_chat",
            system="s",
            parts=[],
            tool_outputs=[{"call_id": "c", "output": "x"}],
            settings=_s(),
        )
    with pytest.raises(ValueError, match="call_id"):
        llm.complete_parts(
            "plans_chat",
            system="s",
            parts=[],
            previous_response_id="r",
            tool_outputs=[{"output": "x"}],
            settings=_s(),
        )
    with pytest.raises(ValueError, match="at least one content part"):
        llm.complete_parts("plans_chat", system="s", parts=[], settings=_s())
    assert client.calls == []


def test_malformed_tool_arguments_are_bad_output(monkeypatch, logged):
    _install(monkeypatch, FakeOpenAI(_resp("", output=[_fn_call(arguments="{not json")])))
    with pytest.raises(llm.LlmBadOutput, match="tool call"):
        llm.complete_parts("plans_chat", system="s", parts=PARTS[:1], settings=_s())
    _install(monkeypatch, FakeOpenAI(_resp("", output=[_fn_call(arguments="[1, 2]")])))
    with pytest.raises(llm.LlmBadOutput, match="tool call"):
        llm.complete_parts("plans_chat", system="s", parts=PARTS[:1], settings=_s())


# ── Failures ────────────────────────────────────────────────────────────────


def test_incomplete_status_is_bad_output_and_still_logs_usage(monkeypatch, logged):
    resp = _resp('{"ok": tr', status="incomplete", reason="max_output_tokens",
                 usage=_usage(inp=5000, cached=0, out=2000, reasoning=1500))
    _install(monkeypatch, FakeOpenAI(resp))
    with pytest.raises(llm.LlmBadOutputPermanent, match="cut off \\(max_output_tokens\\)"):
        llm.complete_parts("plans_sub", system="s", parts=PARTS, schema=SCHEMA, settings=_s())
    (row,) = logged
    assert row["ok"] is False and isinstance(row["error"], llm.LlmBadOutput)
    # An incomplete response still bills; the spend is recorded.
    assert row["input_tokens"] == 5000 and row["output_tokens"] == 2000
    assert row["reasoning_tokens"] == 1500
    assert row["cost_usd"] == Decimal("0.150000")
    assert llm.last_usage() == llm.LlmUsage(5000, 0, 2000, 1500)


def test_invalid_json_with_a_schema_is_bad_output(monkeypatch, logged):
    _install(monkeypatch, FakeOpenAI(_resp("not json")))
    with pytest.raises(llm.LlmBadOutput, match="not valid JSON"):
        llm.complete_parts("plans_sub", system="s", parts=PARTS, schema=SCHEMA, settings=_s())


def test_non_openai_routes_are_refused(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI())
    with pytest.raises(llm.LlmNotConfigured, match="needs the OpenAI provider"):
        llm.complete_parts("boq", system="s", parts=PARTS[:1], settings=_s())  # anthropic
    sh = _s(
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url="http://localhost:11434/v1",
        self_hosted_llm_local_model="qwen",
    )
    with pytest.raises(llm.LlmNotConfigured, match="needs the OpenAI provider"):
        llm.complete_parts("proposal", system="s", parts=PARTS[:1], settings=sh)
    assert client.calls == []
    # The plans features are pinned to OpenAI, so the self-hosted switch
    # does not refuse them (a missing key does).
    with pytest.raises(llm.LlmNotConfigured):
        llm.complete_parts("plans_sub", system="s", parts=PARTS[:1],
                           settings=_s(full_self_hosted_llms_enabled=True,
                                       self_hosted_llm_local_base_url="http://localhost:11434/v1",
                                       openai_api_key=""))
    assert client.calls == []


def test_missing_openai_key_is_not_configured(monkeypatch, logged):
    _install(monkeypatch, FakeOpenAI())
    with pytest.raises(llm.LlmNotConfigured, match="OPENAI_API_KEY"):
        llm.complete_parts(
            "plans_sub", system="s", parts=PARTS[:1], settings=_s(openai_api_key="")
        )


# ── Accounting on every provider path ───────────────────────────────────────


def test_complete_json_openai_logs_usage_and_cost(monkeypatch, logged):
    client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kw: _resp('{"a": 1}', usage=_usage(1000, 0, 100, 0))
        )
    )
    client.with_options = lambda **kw: client
    _install(monkeypatch, client)
    s = _s(openai_proposal_model="gpt-6-astra-2026-09-03")
    out = llm.complete_json(
        "proposal", system="s", messages=[{"role": "user", "content": "x"}], settings=s
    )
    assert out == {"a": 1}  # return type unchanged
    row = logged[0]
    assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"]) == (1000, 0, 100)
    assert row["reasoning_tokens"] == 0
    assert row["cost_usd"] == Decimal("0.015000")  # dated snapshot priced as astra
    assert llm.last_usage() == llm.LlmUsage(1000, 0, 100, 0)
    assert llm.last_cost_usd() == Decimal("0.015000")


def test_anthropic_usage_is_normalized_to_include_cache_reads(monkeypatch, logged):
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello")],
        usage=SimpleNamespace(
            input_tokens=500,
            cache_read_input_tokens=1000,
            cache_creation_input_tokens=200,
            output_tokens=80,
        ),
    )
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp))
    client.with_options = lambda **kw: client
    _install(monkeypatch, client)
    out = llm.complete_text(
        "boq", system="s", messages=[{"role": "user", "content": "x"}], settings=_s()
    )
    assert out == "hello"
    row = logged[0]
    assert row["provider"] == "anthropic"
    assert row["input_tokens"] == 1700 and row["cached_tokens"] == 1000
    assert row["output_tokens"] == 80 and row["reasoning_tokens"] == 0
    assert row["cost_usd"] is None  # no price row for Claude models


def test_self_hosted_chat_usage_is_logged_without_cost(monkeypatch, logged):
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'),
                                 finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=321,
            completion_tokens=45,
            prompt_tokens_details=None,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
        ),
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: resp)))
    client.with_options = lambda **kw: client
    _install(monkeypatch, client)
    sh = _s(
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url="http://localhost:11434/v1",
        self_hosted_llm_local_model="qwen",
    )
    llm.complete_json(
        "email_match", system="s", messages=[{"role": "user", "content": "x"}], settings=sh
    )
    row = logged[0]
    assert row["provider"] == "self_hosted"
    assert (row["input_tokens"], row["cached_tokens"]) == (321, 0)
    assert (row["output_tokens"], row["reasoning_tokens"]) == (45, 12)
    assert row["cost_usd"] is None


def test_transport_without_usage_logs_nulls_and_clears_last_usage(monkeypatch, logged):
    # First a call with usage, then one without: last_usage must not go stale.
    first = FakeOpenAI(_resp("a", usage=_usage()))
    _install(monkeypatch, first)
    llm.complete_parts("plans_chat", system="s", parts=PARTS[:1], settings=_s())
    assert llm.last_usage() is not None
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="x")])  # no usage attr
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp))
    client.with_options = lambda **kw: client
    _install(monkeypatch, client)
    llm.complete_text("boq", system="s", messages=[{"role": "user", "content": "x"}],
                      settings=_s())
    assert logged[-1]["input_tokens"] is None and logged[-1]["cost_usd"] is None
    assert llm.last_usage() is None and llm.last_cost_usd() is None


def test_garbage_usage_never_breaks_the_call(monkeypatch, logged):
    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    resp = _resp('{"ok": true}', usage=Exploding())
    _install(monkeypatch, FakeOpenAI(resp))
    out = llm.complete_parts("plans_sub", system="s", parts=PARTS, schema=SCHEMA, settings=_s())
    assert out.parsed == {"ok": True}
    assert out.usage == llm.LlmUsage()
    assert logged[0]["input_tokens"] is None


# ── log_call writes the new columns ─────────────────────────────────────────


class _Insert:
    def __init__(self, db, name):
        self.db, self.name, self.payload = db, name, None

    def insert(self, payload):
        self.payload = payload
        return self

    def execute(self):
        self.db.tables.setdefault(self.name, []).append(dict(self.payload))
        return SimpleNamespace(data=[self.payload])


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def table(self, name):
        return _Insert(self, name)


def test_log_call_writes_token_and_cost_columns(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    llm_gate.log_call(
        feature="plans_sub",
        provider="openai",
        model="gpt-6-astra",
        tier_name="job",
        ok=True,
        error=None,
        duration_ms=61_000,
        input_tokens=230_000,
        cached_tokens=40_000,
        output_tokens=12_000,
        reasoning_tokens=4_000,
        cost_usd=Decimal("2.540000"),
    )
    row = db.tables["llm_call_log"][0]
    assert row["input_tokens"] == 230_000 and row["cached_tokens"] == 40_000
    assert row["output_tokens"] == 12_000 and row["reasoning_tokens"] == 4_000
    # JSON-safe for the PostgREST client (Decimal is not serializable).
    assert row["cost_usd"] == 2.54 and isinstance(row["cost_usd"], float)
    json.dumps(row)


def test_log_call_leaves_unknown_usage_out_of_the_row(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    llm_gate.log_call(
        feature="boq", provider="self_hosted", model="qwen", tier_name="interactive",
        ok=True, error=None, duration_ms=5,
    )
    row = db.tables["llm_call_log"][0]
    for column in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
                   "cost_usd"):
        assert column not in row


def test_guarded_end_to_end_into_the_call_log(monkeypatch):
    llm_gate.reset_gates()
    db = FakeDB()
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    _install(monkeypatch, FakeOpenAI(_resp('{"ok": true}', usage=_usage(100_000, 0, 1_000, 0))))
    with llm_gate.tier(llm_gate.TIER_JOB, job_id="job-9"):
        llm.complete_parts("plans_sub", system="s", parts=PARTS, schema=SCHEMA, settings=_s())
    row = db.tables["llm_call_log"][0]
    assert row["feature"] == "plans_sub" and row["job_id"] == "job-9" and row["ok"] is True
    assert row["input_tokens"] == 100_000 and row["output_tokens"] == 1_000
    assert row["cost_usd"] == 1.05
    llm_gate.reset_gates()


# ── Review fixes: stale usage, permanent failures, defaults, lease ─────────


def test_early_failure_clears_the_previous_calls_usage(monkeypatch, logged):
    # A $1.05 call succeeds, then a call that fails before reaching the
    # provider must not leave that $1.05 readable for the caller's except path.
    _install(monkeypatch, FakeOpenAI(_resp('{"ok": true}', usage=_usage(100_000, 0, 1_000, 0))))
    llm.complete_parts("plans_sub", system="s", parts=PARTS, schema=SCHEMA, settings=_s())
    assert llm.last_cost_usd() == Decimal("1.050000")
    with pytest.raises(ValueError, match="call_id"):
        llm.complete_parts(
            "plans_chat", system="s", parts=[], previous_response_id="r",
            tool_outputs=[{"output": "x"}], settings=_s(),
        )
    assert llm.last_usage() is None and llm.last_cost_usd() is None
    # Same for a refusal to route (no OpenAI key) and for the other
    # complete_* entry points.
    _install(monkeypatch, FakeOpenAI(_resp('{"ok": true}', usage=_usage(100_000, 0, 1_000, 0))))
    llm.complete_parts("plans_sub", system="s", parts=PARTS, schema=SCHEMA, settings=_s())
    assert llm.last_usage() is not None
    sh = _s(full_self_hosted_llms_enabled=True,
            self_hosted_llm_local_base_url="http://localhost:11434/v1")
    with pytest.raises(llm.LlmNotConfigured):
        llm.complete_parts("plans_sub", system="s", parts=PARTS, settings=_s(openai_api_key=""))
    assert llm.last_usage() is None and llm.last_cost_usd() is None
    _install(monkeypatch, FakeOpenAI(_resp('{"ok": true}', usage=_usage(100_000, 0, 1_000, 0))))
    llm.complete_parts("plans_sub", system="s", parts=PARTS, schema=SCHEMA, settings=_s())
    with pytest.raises(llm.LlmNotConfigured):
        llm.complete_text(
            "boq", system="s", messages=[{"role": "user", "content": "x"}],
            settings=sh,  # no model configured: _require_route raises first
        )
    assert llm.last_cost_usd() is None


def test_tool_call_without_a_call_id_is_bad_output(monkeypatch, logged):
    _install(monkeypatch, FakeOpenAI(_resp("", output=[_fn_call(call_id="")])))
    with pytest.raises(llm.LlmBadOutput, match="call id"):
        llm.complete_parts("plans_chat", system="s", parts=PARTS[:1], settings=_s())
    _install(monkeypatch, FakeOpenAI(_resp("", output=[_fn_call(call_id="  ")])))
    with pytest.raises(llm.LlmBadOutput, match="call id"):
        llm.complete_parts("plans_chat", system="s", parts=PARTS[:1], settings=_s())


def test_unfixable_answers_are_permanent_and_others_stay_transient(monkeypatch, logged):
    from app.services import llm_errors

    def run(resp):
        _install(monkeypatch, FakeOpenAI(resp))
        with pytest.raises(llm.LlmBadOutput) as info:
            llm.complete_parts("plans_sub", system="s", parts=PARTS, schema=SCHEMA, settings=_s())
        return info.value

    cut = run(_resp("{", status="incomplete", reason="max_output_tokens", usage=_usage()))
    assert isinstance(cut, llm.LlmBadOutputPermanent)
    assert llm_errors.classify(cut) == llm_errors.KIND_BAD_INPUT
    assert not llm_errors.is_transient(cut)
    assert "output token limit" in llm_errors.user_message(cut, "gpt-6-astra")

    filtered = run(_resp("", status="incomplete", reason="content_filter", usage=_usage()))
    assert isinstance(filtered, llm.LlmBadOutputPermanent)
    assert "content_filter" in str(filtered)

    refusal = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="refusal", refusal="I can't help with that.")],
    )
    refused = run(_resp("", output=[refusal], usage=_usage()))
    assert isinstance(refused, llm.LlmBadOutputPermanent)
    assert "declined" in str(refused)
    # The refusal still billed and is logged with its usage.
    assert logged[-1]["input_tokens"] == 1000 and logged[-1]["ok"] is False

    other = run(_resp("{", status="incomplete", reason="something_new", usage=_usage()))
    assert type(other) is llm.LlmBadOutput
    assert llm_errors.is_transient(other)
    bad_json = run(_resp("not json", usage=_usage()))
    assert type(bad_json) is llm.LlmBadOutput


def test_reasoning_effort_defaults_to_the_setting_and_can_be_overridden(monkeypatch, logged):
    client = _install(monkeypatch, FakeOpenAI(_resp("a"), _resp("b"), _resp("c")))
    llm.complete_parts("plans_chat", system="s", parts=PARTS[:1],
                       settings=_s(plans_reader_reasoning_effort="HIGH"))
    llm.complete_parts("plans_modify", system="s", parts=PARTS[:1],
                       reasoning_effort="low", settings=_s())
    llm.complete_parts("plans_main", system="s", parts=PARTS[:1], settings=_s())
    assert [c["reasoning"] for c in client.calls] == [
        {"effort": "high"}, {"effort": "low"}, {"effort": "medium"},
    ]


def test_usage_short_name_aliases_match_the_contract():
    u = llm.LlmUsage(1000, 200, 300, 100)
    assert (u.input, u.cached, u.output, u.reasoning) == (1000, 200, 300, 100)
    with pytest.raises(AttributeError):
        u.input = 5  # read-only


def test_lease_is_renewed_before_the_request_and_a_lost_lease_refuses(monkeypatch, logged):
    from app.services import llm_queue

    order: list[str] = []
    client = FakeOpenAI(_resp("a"))
    original = client._create

    def create(**kw):
        order.append("request")
        return original(**kw)

    client.responses = SimpleNamespace(create=create)
    _install(monkeypatch, client)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: order.append("renew") or True)
    llm.complete_parts("plans_sub", system="s", parts=PARTS[:1], settings=_s())
    assert order == ["renew", "request"]

    lost = _install(monkeypatch, FakeOpenAI(_resp("never sent")))
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: False)
    with pytest.raises(llm.LlmLeaseLost, match="lease"):
        llm.complete_parts("plans_sub", system="s", parts=PARTS[:1], settings=_s())
    assert lost.calls == []


def test_lease_renewal_is_a_no_op_outside_a_queue_job():
    from app.services import llm_queue

    assert llm_queue.current_job.get() is None
    assert llm_queue.renew_lease() is True


class _PrePlans152DB(FakeDB):
    """A database without 0152: any usage column in the insert is rejected
    the way PostgREST rejects an unknown column."""

    def __init__(self, error=None):
        super().__init__()
        self.attempts: list[dict] = []
        self.error = error

    def table(self, name):
        db = self

        class _Ins(_Insert):
            def execute(self):
                db.attempts.append(dict(self.payload))
                if db.error is not None:
                    raise db.error
                if "input_tokens" in self.payload or "cost_usd" in self.payload:
                    raise RuntimeError(
                        "{'code': 'PGRST204', 'message': \"Could not find the "
                        "'cost_usd' column of 'llm_call_log' in the schema cache\"}"
                    )
                return super().execute()

        return _Ins(self, name)


def test_log_call_falls_back_without_usage_columns_before_0152(monkeypatch, caplog):
    db = _PrePlans152DB()
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    monkeypatch.setattr(llm_gate, "_usage_columns_warned", False)
    for _ in range(2):
        llm_gate.log_call(
            feature="boq", provider="anthropic", model="claude", tier_name="job",
            ok=True, error=None, duration_ms=5, input_tokens=10, cached_tokens=0,
            output_tokens=5, reasoning_tokens=0, cost_usd=Decimal("0.1"),
        )
    rows = db.tables["llm_call_log"]
    assert len(rows) == 2
    assert rows[0]["feature"] == "boq" and rows[0]["ok"] is True
    for column in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
                   "cost_usd"):
        assert column not in rows[0]
    warnings = [r for r in caplog.records if "0152" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelname == "WARNING"


def test_log_call_does_not_retry_unrelated_failures(monkeypatch):
    db = _PrePlans152DB(error=RuntimeError("connection reset"))
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    llm_gate.log_call(
        feature="boq", provider="anthropic", model="claude", tier_name="job",
        ok=True, error=None, duration_ms=5, input_tokens=10, cost_usd=Decimal("0.1"),
    )
    assert len(db.attempts) == 1 and "llm_call_log" not in db.tables
