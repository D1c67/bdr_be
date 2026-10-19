"""Browser agent model plumbing: adapters, model listing, probe, redaction.

  adapters    each of the five providers builds its native request (tools,
              parallel calls off, stateless, images, tool results) and parses
              text, the tool call and usage; google keeps the key in the
              header and replays thought signatures; error kinds classify.
  models      list_models filters, prefills known prices and caches 1 h per
              key; probe forces one tool call with a 1x1 PNG and reports a
              fixed error text; record/recent_probe audit and find passes.
  redaction   exact values in plain, URL-encoded and base64 forms, the helper
              HMAC, credential URL queries, invisible characters, and the
              global httpx log filter.
  prompts     versioned, hashed, no em dashes, O1 rules present.
"""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from app.core import redact
from app.services import llm_errors, llm_gate
from app.services.browser_agents import llm as providers
from app.services.browser_agents import prompts, protocol, redaction
from app.services.browser_agents.llm import models as llm_models
from app.services.browser_agents.llm.anthropic_ import AnthropicAdapter
from app.services.browser_agents.llm.base import (
    CompleteArgs,
    ImagePart,
    Message,
    ToolCall,
    classify,
    safe_call_id,
)
from app.services.browser_agents.llm.compat import CompatAdapter
from app.services.browser_agents.llm.google_ import GoogleAdapter
from app.services.browser_agents.llm.openai_ import OpenAIAdapter
from tests.browser_agents_fake import FakeDB
from tests.browser_agents_harness import KEY_B, Harness
from tests.browser_agents_llm_fakes import (
    FakeAnthropic,
    FakeCompat,
    FakeGenai,
    FakeOpenAI,
    auth_error,
    connection_error,
    fake_client,
    not_found_error,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
TOOLS = protocol.tools_for("canary")


def transcript() -> list[Message]:
    call = ToolCall(call_id="call.1/x", name="navigate", args={"url": "https://h/p"},
                    replay={"provider": "google", "thought_signature": base64.b64encode(b"sig").decode()})
    return [
        Message(role="user", text="seed facts"),
        Message(role="user", text="blank page", images=(ImagePart("image/png", PNG),)),
        Message(role="assistant", text="going", tool_call=call),
        Message(role="tool", call_id="call.1/x", tool_name="navigate", text="<untrusted>ok</untrusted>", ok=True),
        Message(role="user", text="Current page", images=(ImagePart("image/jpeg", b"\xff\xd8jpg"),)),
    ]


def args(**over) -> CompleteArgs:
    return CompleteArgs(**{"model": "m-1", "system": "SYSTEM", "messages": transcript(), "tools": TOOLS,
                           "max_output_tokens": 512, **over})


# ── adapters ──────────────────────────────────────────────────────────────────


def test_openai_responses_request_and_parse():
    client = FakeOpenAI("find_text", {"query": "BDR"})
    res = OpenAIAdapter(client).complete(args())
    kw = client.responses.kwargs[0]
    assert kw["store"] is False and kw["parallel_tool_calls"] is False and kw["instructions"] == "SYSTEM"
    assert kw["max_output_tokens"] == 512 and kw["tool_choice"] == "auto"
    assert [t["name"] for t in kw["tools"]] == [t["name"] for t in TOOLS]
    assert all(t["type"] == "function" for t in kw["tools"])
    types_ = [i.get("type") or i.get("role") for i in kw["input"]]
    assert types_ == ["user", "user", "assistant", "function_call", "function_call_output", "user"]
    assert kw["input"][1]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert json.loads(kw["input"][3]["arguments"]) == {"url": "https://h/p"}
    assert client.options[-1] == {"timeout": 90.0, "max_retries": 0}
    assert res.text == "Opening the page."
    assert [(c.name, c.args) for c in res.tool_calls] == [("find_text", {"query": "BDR"})]
    assert res.usage.input_tokens == 1200 and res.usage.cached_tokens == 200 and res.usage.output_tokens == 80
    assert res.provider_request_id == "req_openai_1"


def test_openai_forced_tool_and_bad_arguments():
    client = FakeOpenAI(reply=SimpleNamespace(id="r", output=[SimpleNamespace(
        type="function_call", call_id="c", name="navigate", arguments="{not json")], usage=None))
    res = OpenAIAdapter(client).complete(args(force_tool="navigate"))
    assert client.responses.kwargs[0]["tool_choice"] == {"type": "function", "name": "navigate"}
    assert res.tool_calls[0].parse_error == "arguments were not valid JSON"
    assert res.usage.input_tokens == 0


def test_anthropic_messages_request_and_parse():
    client = FakeAnthropic("wait_for", {"seconds": 2})
    res = AnthropicAdapter(client).complete(args())
    kw = client.messages.kwargs[0]
    assert kw["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert kw["system"] == "SYSTEM" and kw["max_tokens"] == 512
    assert [t["name"] for t in kw["tools"]] == [t["name"] for t in TOOLS] and "input_schema" in kw["tools"][0]
    roles = [m["role"] for m in kw["messages"]]
    assert roles == ["user", "assistant", "user"]                  # merged, alternating
    assert [b["type"] for b in kw["messages"][0]["content"]] == ["text", "text", "image"]
    tool_use = kw["messages"][1]["content"][1]
    assert tool_use["type"] == "tool_use" and tool_use["id"] == "call_1_x"     # sanitized id
    result_block = kw["messages"][2]["content"][0]
    assert result_block == {"type": "tool_result", "tool_use_id": "call_1_x",
                            "content": "<untrusted>ok</untrusted>", "is_error": False}
    assert kw["messages"][2]["content"][2]["source"]["media_type"] == "image/jpeg"
    assert res.tool_calls[0].name == "wait_for" and res.tool_calls[0].args == {"seconds": 2}
    assert res.usage.input_tokens == 1300 and res.usage.cached_tokens == 300 and res.usage.output_tokens == 90
    forced = AnthropicAdapter(FakeAnthropic())
    forced.complete(args(force_tool="navigate"))
    assert forced.client.messages.kwargs[0]["tool_choice"] == {
        "type": "tool", "name": "navigate", "disable_parallel_tool_use": True}


def test_google_genai_request_and_parse():
    client = FakeGenai("find_text", {"query": "ok"})
    res = GoogleAdapter(client).complete(args())
    kw = client.models.kwargs[0]
    cfg = kw["config"]
    assert cfg.system_instruction == "SYSTEM" and cfg.max_output_tokens == 512
    assert cfg.automatic_function_calling.disable is True
    assert cfg.tool_config.function_calling_config.mode.value == "AUTO"
    names = [d.name for d in cfg.tools[0].function_declarations]
    assert names == [t["name"] for t in TOOLS]
    assert cfg.tools[0].function_declarations[0].parameters_json_schema == TOOLS[0]["parameters"]
    roles = [c.role for c in kw["contents"]]
    assert roles == ["user", "model", "user"]
    fc_part = kw["contents"][1].parts[-1]
    assert fc_part.function_call.name == "navigate" and fc_part.thought_signature == b"sig"
    fr = kw["contents"][2].parts[0].function_response
    assert fr.name == "navigate" and fr.response == {"output": "<untrusted>ok</untrusted>"}
    assert kw["contents"][0].parts[2].inline_data.mime_type == "image/png"
    assert res.text == ""                                        # the thought part is not text
    assert res.tool_calls[0].name == "find_text" and res.tool_calls[0].args == {"query": "ok"}
    assert base64.b64decode(res.tool_calls[0].replay["thought_signature"]) == b"gemini-sig-1"
    assert res.usage.output_tokens == 100 and res.usage.input_tokens == 900
    forced = GoogleAdapter(FakeGenai())
    forced.complete(args(force_tool="navigate"))
    fcc = forced.client.models.kwargs[0]["config"].tool_config.function_calling_config
    assert fcc.mode.value == "ANY" and fcc.allowed_function_names == ["navigate"]


def test_google_signature_is_not_replayed_to_another_provider_or_without_one():
    client = FakeGenai()
    msgs = transcript()
    msgs[2] = Message(role="assistant", tool_call=ToolCall("c", "navigate", {"url": "https://h/p"},
                                                           replay={"provider": "openai", "thought_signature": "eA=="}))
    GoogleAdapter(client).complete(args(messages=msgs))
    assert client.models.kwargs[0]["contents"][1].parts[-1].thought_signature is None


def test_google_client_sends_the_key_only_as_a_header():
    client = providers._build_client("google", "AIza-test-key")
    api = client._api_client
    assert api._http_options.headers.get("x-goog-api-key") == "AIza-test-key"
    assert "key=" not in (api._http_options.base_url or "")


@pytest.mark.parametrize("provider,base", [("xai", "https://api.x.ai/v1"), ("meta", "https://api.meta.ai/v1")])
def test_compat_chat_completions_request_and_parse(provider, base):
    client = FakeCompat("navigate", {"url": "https://h/p"})
    res = CompatAdapter(client, provider).complete(args())
    kw = client.chat.completions.kwargs[0]
    assert kw["parallel_tool_calls"] is False and kw["tool_choice"] == "auto" and kw["max_tokens"] == 512
    roles = [m["role"] for m in kw["messages"]]
    assert roles == ["system", "user", "user", "assistant", "tool", "user"]
    assert kw["messages"][3]["tool_calls"][0]["function"]["name"] == "navigate"
    assert kw["messages"][4] == {"role": "tool", "tool_call_id": "call.1/x", "content": "<untrusted>ok</untrusted>"}
    assert kw["messages"][2]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert res.tool_calls[0].args == {"url": "https://h/p"} and res.usage.input_tokens == 800
    real = providers._build_client(provider, "k")
    assert str(real.base_url).rstrip("/") == base


def test_get_adapter_needs_a_key_and_a_known_provider(monkeypatch):
    s = Harness(monkeypatch).settings.model_copy(update={"anthropic_api_key": "", "xai_api_key": "x"})
    with pytest.raises(providers.ProviderNotConfigured):
        providers.get_adapter("anthropic", s)
    with pytest.raises(providers.UnknownProvider):
        providers.get_adapter("mistral", s)
    providers.reset_clients()
    adapter = providers.get_adapter("xai", s)
    assert isinstance(adapter, CompatAdapter) and adapter.provider == "xai"
    assert providers.get_adapter("xai", s).client is adapter.client            # cached per key
    info = {p["provider"]: p for p in providers.providers_info(s)}
    assert info["xai"]["key_configured"] and not info["anthropic"]["key_configured"]
    assert info["meta"]["experimental"] and not info["openai"]["experimental"]


def test_error_classification():
    assert classify(connection_error()) == llm_errors.KIND_UNREACHABLE
    assert classify(auth_error()) == llm_errors.KIND_UNAUTHORIZED
    assert classify(not_found_error()) == "model_not_found"
    from google.genai import errors as genai_errors

    assert classify(genai_errors.ClientError(404, {"error": {"message": "x"}})) == "model_not_found"
    assert classify(genai_errors.ClientError(403, {"error": {"message": "x"}})) == llm_errors.KIND_UNAUTHORIZED
    assert classify(genai_errors.ClientError(429, {"error": {"message": "slow"}})) == llm_errors.KIND_RATE_LIMITED
    assert classify(genai_errors.ServerError(503, {"error": {"message": "x"}})) == llm_errors.KIND_OVERLOADED
    assert classify(providers.ProviderNotConfigured("no key")) == llm_errors.KIND_NOT_CONFIGURED
    assert safe_call_id("a b/c") == "a_b_c" and safe_call_id("") == "call"


# ── model listing and probe ───────────────────────────────────────────────────


@pytest.fixture
def h(monkeypatch):
    harness = Harness(monkeypatch, openai_api_key="sk-o", anthropic_api_key="sk-a", google_api_key="g",
                      xai_api_key="x", meta_api_key="")
    monkeypatch.setattr(providers, "get_settings", lambda: harness.settings)
    monkeypatch.setattr(llm_models, "get_settings", lambda: harness.settings)
    llm_models.reset_cache()
    providers.reset_clients()
    llm_gate.reset_gates()
    return harness


def test_list_models_filters_prefills_prices_and_caches(monkeypatch, h):
    client = fake_client("openai", models=["gpt-6-astra", "gpt-5.4-mini", "text-embedding-3", "whisper-1",
                                           "o4-mini", "dall-e-3"])
    monkeypatch.setattr(providers, "_client", lambda p, k: client)
    out = llm_models.list_models("openai")
    ids = [m["id"] for m in out["models"]]
    assert ids == ["gpt-5.4-mini", "gpt-6-astra", "o4-mini"]
    astra = next(m for m in out["models"] if m["id"] == "gpt-6-astra")
    assert astra["price_in_per_mtok"] == 10.0 and astra["price_out_per_mtok"] == 50.0
    assert next(m for m in out["models"] if m["id"] == "o4-mini")["price_in_per_mtok"] is None
    again = llm_models.list_models("openai")
    assert again is out and client.models.list_calls == 1
    llm_models.list_models("openai", force=True)
    assert client.models.list_calls == 2


@pytest.mark.parametrize("provider,raw,want", [
    ("anthropic", ["claude-opus-5", "claude-sonnet-5"], ["claude-opus-5", "claude-sonnet-5"]),
    ("xai", ["grok-5", "grok-5-image", "grok-embed"], ["grok-5"]),
])
def test_list_models_other_providers(monkeypatch, h, provider, raw, want):
    client = fake_client(provider, models=raw)
    monkeypatch.setattr(providers, "_client", lambda p, k: client)
    assert [m["id"] for m in llm_models.list_models(provider)["models"]] == want


def test_list_models_google_uses_reported_capabilities(monkeypatch, h):
    items = [SimpleNamespace(name="models/gemini-3-pro", display_name="Gemini 3 Pro",
                             supported_actions=["generateContent", "countTokens"]),
             SimpleNamespace(name="models/gemini-embedding-2", display_name="E", supported_actions=["embedContent"]),
             SimpleNamespace(name="models/gemini-x-tts", display_name="T", supported_actions=["generateContent"])]
    client = FakeGenai(models=items)
    monkeypatch.setattr(providers, "_client", lambda p, k: client)
    out = llm_models.list_models("google")
    assert out["models"] == [{"id": "gemini-3-pro", "label": "Gemini 3 Pro",
                              "price_in_per_mtok": None, "price_out_per_mtok": None}]


def test_list_models_and_probe_need_the_key(h):
    with pytest.raises(llm_models.ProviderKeyMissing):
        llm_models.list_models("meta")
    with pytest.raises(llm_models.ProviderKeyMissing):
        llm_models.probe("meta", "llama-5")


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google", "xai"])
def test_probe_forces_one_tool_call_with_an_image(monkeypatch, h, provider):
    client = fake_client(provider, "report_probe", {"ok": True, "color": "red"})
    monkeypatch.setattr(providers, "_client", lambda p, k: client)
    res = llm_models.probe(provider, "model-x")
    assert res["ok"] is True and res["error"] is None and res["latency_ms"] >= 0 and res["probed_at"]
    if provider == "openai":
        kw = client.responses.kwargs[0]
        assert kw["tool_choice"] == {"type": "function", "name": "report_probe"}
        assert kw["input"][0]["content"][1]["type"] == "input_image"
    elif provider == "anthropic":
        assert client.messages.kwargs[0]["tool_choice"]["name"] == "report_probe"
    elif provider == "google":
        assert client.models.kwargs[0]["config"].tool_config.function_calling_config.mode.value == "ANY"
    else:
        assert client.chat.completions.kwargs[0]["tool_choice"]["function"]["name"] == "report_probe"
    log = h.db.rows("llm_call_log")
    assert log and log[-1]["feature"] == "browser_agent_probe" and log[-1]["ok"] is True


def test_probe_reports_failures_with_fixed_text(monkeypatch, h):
    wrong = fake_client("openai", "navigate", {"url": "x"})
    monkeypatch.setattr(providers, "_client", lambda p, k: wrong)
    assert llm_models.probe("openai", "m")["error"] == "The model did not answer with exactly one tool call."
    broken = FakeOpenAI(reply=auth_error())
    monkeypatch.setattr(providers, "_client", lambda p, k: broken)
    res = llm_models.probe("openai", "m")
    assert res["ok"] is False and res["error"] == "The AI provider rejected the API key."
    assert "sk-live-123" not in json.dumps(res)
    with pytest.raises(ValueError):
        llm_models.probe("openai", "bad model id!")


class ProbeDB(FakeDB):
    def rpc_browser_agent_update_settings(self, p_scope, p_target, p_patch, p_actor, p_actor_label, p_probe=None):
        self.one("browser_agent_types", key=p_target).update(p_patch)
        return {"changed": list(p_patch)}


def test_record_and_recent_probe(monkeypatch):
    harness = Harness(monkeypatch, db=ProbeDB())
    monkeypatch.setattr(llm_models, "_now", lambda: harness.db.now)
    harness.db.one("browser_agent_types", key="canary").update(provider="openai", model="gpt-x",
                                                               model_probe_ok_at=None)
    ok = {"ok": True, "latency_ms": 10, "error": None, "probed_at": "2026-10-19T17:00:00.000000Z"}
    llm_models.record_probe("canary", "openai", "gpt-x", ok, actor_id=None, actor_label="Pat (IT Admin)")
    llm_models.record_probe("canary", "openai", "gpt-y", {**ok, "ok": False, "error": "x"}, actor_id=None,
                            actor_label="Pat (IT Admin)")
    changes = harness.db.rows("browser_agent_settings_changes", field="model_probe")
    assert len(changes) == 2 and changes[0]["changed_by_label"] == "Pat (IT Admin)"
    assert harness.db.one("browser_agent_types", key="canary")["model_probe_ok_at"] == ok["probed_at"]
    for row in changes:
        row["changed_at"] = harness.db.stamp()
    assert llm_models.recent_probe("canary", "openai", "gpt-x")["ok"] is True
    assert llm_models.recent_probe("canary", "openai", "gpt-y") is None
    harness.db.advance(601)
    assert llm_models.recent_probe("canary", "openai", "gpt-x") is None


# ── redaction ─────────────────────────────────────────────────────────────────


def test_exact_values_in_every_form_and_labels_only():
    s = redaction.SecretSet()
    s.add("procore_password", "p@ss word/+1", severe=True)
    s.add("cookie", "short", severe=False, min_len=12)          # too short: ignored
    for form in ("p@ss word/+1", "p%40ss%20word%2F%2B1", "p%40ss+word%2F%2B1",
                 base64.b64encode(b"p@ss word/+1").decode(), base64.urlsafe_b64encode(b"p@ss word/+1").decode()):
        report = redaction.ScrubReport()
        out = redaction.scrub({"k": [f"x {form} y"]}, s, report)
        assert form not in json.dumps(out) and report.severe and report.hits == ["procore_password"]
    report = redaction.ScrubReport()
    assert redaction.scrub("short and fine", s, report) == "short and fine" and not report.hits


def test_helper_code_hmac_and_invisible_characters(monkeypatch):
    harness = Harness(monkeypatch)
    monkeypatch.setattr(redaction, "get_settings", lambda: harness.settings)
    s = redaction.SecretSet(hmacs={redaction.secret_hmac("9X4K2Q")}, hmac_key=base64.b64decode(KEY_B))
    report = redaction.ScrubReport()
    out = redaction.scrub("code​ is 9X4K2Q now", s, report)
    assert out == "code is [redacted] now" and report.severe and report.hits == ["helper_secret"]


def test_credential_queries_are_redacted_and_the_rest_kept():
    text = ("see https://user:pw@app.procore.com/1/project/rfi?tab=open&X-Amz-Signature=abc&sig=zz#frag "
            "and https://example.com/a?b=c")
    out = redaction.redact_token_queries(text)
    assert "abc" not in out and "zz" not in out and "user:pw" not in out
    assert "tab=open" in out and "https://example.com/a?b=c" in out


def test_load_secrets_collects_every_known_source(monkeypatch):
    harness = Harness(monkeypatch, procore_login_password="EnvPassw0rd")
    monkeypatch.setattr(redaction, "get_settings", lambda: harness.settings)
    redaction.reset_for_tests()
    redaction.register_secret_source(lambda: ["DbPassw0rd!"])
    redaction.register_secret_source(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    harness.db.table("rfp_harvest_sessions").insert({"provider": "procore", "cookies": [
        {"name": "a", "value": "cookie-value-123456789"}]}).execute()
    s = redaction.load_secrets("job-1", worker_token="bdrw_" + "A" * 43)
    labels = {label for label, _, _ in s.exact}
    assert labels == {"procore_password", "worker_token", "cookie"}
    values = {v for _, v, _ in s.exact}
    assert {"EnvPassw0rd", "DbPassw0rd!", "cookie-value-123456789"} <= values
    redaction.reset_for_tests()


def test_httpx_log_filter_redacts_urls(caplog):
    flt = redact.install_httpx_filter()
    assert redact.install_httpx_filter() is flt
    assert logging.getLogger("httpx").filters.count(flt) == 1
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "GET",
                                        httpx.URL("https://storage.test/o?token=SECRET&x=1"), "HTTP/1.1 200 OK")
    assert "SECRET" not in caplog.text and "https://storage.test/o" in caplog.text


# ── prompts ───────────────────────────────────────────────────────────────────


def test_prompts_are_versioned_hashed_and_carry_the_o1_rules():
    rfi = prompts.get_prompt("rfi_from_email.v1", "rfi_from_email")
    canary = prompts.get_prompt(None, "canary")
    assert canary.version == "canary.v1" and len(rfi.sha256) == 64 and rfi.sha256 != canary.sha256
    for needle in ("structured site tools first", "ONLY RFI data BDR imports", "untrusted_page_content",
                   "request_help", "never write, summarize or correct RFI content"):
        assert needle.lower() in rfi.text.lower(), needle
    for p in prompts.PROMPTS.values():
        assert "\u2014" not in p.text and "\u2013" not in p.text
    with pytest.raises(prompts.UnknownPrompt):
        prompts.get_prompt("rfi_from_email.v9", "rfi_from_email")
