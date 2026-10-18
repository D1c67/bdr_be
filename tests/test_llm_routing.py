"""LLM routing layer (services/llm): provider selection, the strict
self-hosted guarantee, config validation, and transport fallbacks."""

import io
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.core.config import Settings
from app.services import llm, openai_text


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


def _sh(**over) -> Settings:
    base = dict(
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url="http://localhost:11434/v1",
        anthropic_api_key="3p-key-must-never-be-used",
        openai_api_key="3p-key-must-never-be-used",
    )
    base.update(over)
    return _s(**base)


def _fake_sh_client(content='{"ok": true}', fail_first=None, finish_reason="stop"):
    """OpenAI-compatible stub exposing ONLY chat.completions — any attempt to
    use the 3rd-party Responses/Messages APIs would AttributeError."""
    calls: list[dict] = []
    state = {"fail": fail_first}

    def create(**kwargs):
        calls.append(kwargs)
        if state["fail"] is not None and "response_format" in kwargs:
            exc = state["fail"]
            state["fail"] = None
            raise exc
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content), finish_reason=finish_reason
                )
            ]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        with_options=lambda **kw: client,
    )
    return client, calls


# ── Route resolution ────────────────────────────────────────────────────────


def test_flag_off_routes_to_third_party_vendors():
    s = _s(anthropic_api_key="a", openai_api_key="o")
    assert llm.resolve("boq", s).provider == "anthropic"
    assert llm.resolve("boq", s).model == s.claude_boq_model
    assert llm.resolve("estimate", s).provider == "anthropic"
    assert llm.resolve("email_match", s).provider == "openai"
    assert llm.resolve("email_match", s).model == s.openai_email_match_model
    assert llm.resolve("proposal", s).provider == "openai"
    assert llm.resolve("aliases", s).model == s.openai_alias_model


def test_flag_on_routes_everything_self_hosted_even_with_3p_keys():
    s = _sh(self_hosted_boq_model="m1", self_hosted_email_match_model="m2")
    for feature, spec in llm._FEATURES.items():
        if spec.openai_only:  # pinned to OpenAI in both modes
            continue
        assert llm.resolve(feature, s).provider == "self_hosted"
    assert llm.resolve("boq", s).model == "m1"
    assert llm.resolve("boq", s).base_url == "http://localhost:11434/v1"


PLANS_FEATURES = ("plans_sub", "plans_main", "plans_modify", "plans_chat")


def test_plans_features_are_openai_only_on_the_astra_model():
    s = _s(openai_api_key="o")
    for feature in PLANS_FEATURES:
        assert llm._FEATURES[feature].openai_only is True
        route = llm.resolve(feature, s)
        assert route.provider == "openai"
        assert route.model == "gpt-6-astra" == s.openai_plans_model
        assert llm.is_configured(feature, s) is True
        assert llm.unavailable_reason(feature, s) is None
    assert llm.resolve("plans_sub", _s(openai_api_key="o", openai_plans_model="x-1")).model == "x-1"
    # Every pre-existing feature stays routable in self-hosted mode.
    assert [f for f, spec in llm._FEATURES.items() if spec.openai_only] == list(PLANS_FEATURES)


def test_self_hosted_flag_keeps_openai_only_features_on_openai():
    # Owner decision 2026-10-18: the strict switch moves every feature to the
    # box EXCEPT AI Estimating, which keeps running on the OpenAI key.
    s = _sh(self_hosted_llm_local_model="qwen", openai_api_key="o")
    for feature in PLANS_FEATURES:
        route = llm.resolve(feature, s)
        assert route.provider == "openai" and route.model == "gpt-6-astra" and route.api_key == "o"
        assert llm.is_configured(feature, s) is True
        assert llm.unavailable_reason(feature, s) is None
        assert llm.active_model(feature, s) == "gpt-6-astra"
    # Without an OpenAI key the feature is unconfigured, not re-routed.
    keyless = _sh(self_hosted_llm_local_model="qwen", openai_api_key="")
    assert llm.resolve("plans_sub", keyless).provider == "openai"
    assert llm.is_configured("plans_sub", keyless) is False
    with pytest.raises(llm.LlmNotConfigured):
        llm.complete_parts("plans_sub", system="s", parts=[{"type": "input_text", "text": "x"}], settings=keyless)
    # The rest of the app still goes to the box.
    assert llm.resolve("boq", s).provider == "self_hosted"
    assert llm.unavailable_reason("boq", s) is None


def test_plans_reader_flag_off_is_the_only_by_design_exclusion():
    # The queue labels a failed job with active_model; a raise there would
    # replace the by-design reason (not_configured) with the generic
    # handler-failed text (unknown).
    from app.services import llm_errors

    for s in (_s(openai_api_key="o", plans_reader_enabled=False),
              _sh(self_hosted_llm_local_model="qwen", plans_reader_enabled=False)):
        for feature in PLANS_FEATURES:
            assert "PLANS_READER_ENABLED" in llm.unavailable_reason(feature, s)
            assert llm.is_configured(feature, s) is False
            assert llm.active_model(feature, s) == "gpt-6-astra"
        assert llm.unavailable_reason("boq", s) is None
    exc = llm.LlmNotConfigured(llm.PLANS_READER_OFF_MESSAGE)
    assert llm_errors.classify(exc) == llm_errors.KIND_NOT_CONFIGURED
    assert llm_errors.user_message(exc, "gpt-6-astra") == llm.PLANS_READER_OFF_MESSAGE


def test_plans_lease_check_keeps_headroom():
    from app.core.config import PLANS_READER_LEASE_HEADROOM_SECONDS

    # Defaults: 600 + 180 + 120 fits a 900 s lease exactly.
    assert _s(plans_reader_enabled=True).plans_reader_call_timeout_seconds == 600
    assert PLANS_READER_LEASE_HEADROOM_SECONDS == 120
    with pytest.raises(ValueError, match="headroom"):
        _s(plans_reader_enabled=True, plans_reader_call_timeout_seconds=601)
    # Off, the check does not run.
    off = _s(plans_reader_enabled=False, plans_reader_call_timeout_seconds=601)
    assert off.plans_reader_call_timeout_seconds == 601


def test_live_box_override_does_not_choke_on_openai_only_features(monkeypatch):
    from app.services import llm_target

    s = _sh(
        self_hosted_llm_local_model="qwen",
        self_hosted_llm_ec2_base_url="https://llm.example.com/v1",
        self_hosted_llm_ec2_model="qwen-ec2",
    )
    monkeypatch.setattr(llm_target, "_override", "ec2")
    try:
        switched = llm_target.apply(s)
        assert switched.self_hosted_llm_target == "ec2"
        assert llm.resolve("plans_main", s).provider == "openai"  # the box switch never touches it
        assert llm.resolve("boq", s).model == "qwen-ec2"
    finally:
        llm_target.reset()


def test_target_selects_ec2_pair():
    s = _sh(
        self_hosted_llm_target="ec2",
        self_hosted_llm_ec2_base_url="https://llm.internal.example.com/v1/",
        self_hosted_llm_ec2_api_key="ec2-key",
        self_hosted_llm_local_api_key="local-key",
    )
    route = llm.resolve("boq", s)
    assert route.base_url == "https://llm.internal.example.com/v1"  # trailing / stripped
    assert route.api_key == "ec2-key"


def test_target_selects_ec2b_pair():
    s = _sh(
        self_hosted_llm_target="ec2b",
        self_hosted_llm_ec2_base_url="https://a.example.com/v1",
        self_hosted_llm_ec2_api_key="a-key",
        self_hosted_llm_ec2b_base_url="https://b.example.com/v1/",
        self_hosted_llm_ec2b_api_key="b-key",
    )
    route = llm.resolve("boq", s)
    assert route.base_url == "https://b.example.com/v1"
    assert route.api_key == "b-key"


def test_target_model_is_the_default_for_every_feature():
    # Flipping the target moves every feature to that box's model in one line.
    a = _sh(
        self_hosted_llm_target="ec2",
        self_hosted_llm_ec2_base_url="https://a.example.com/v1",
        self_hosted_llm_ec2_model="qwen-3.5-4b",
        self_hosted_llm_ec2b_base_url="https://b.example.com/v1",
        self_hosted_llm_ec2b_model="qwen-3.8-27b",
    )
    b = _s(**{**a.model_dump(), "self_hosted_llm_target": "ec2b"})
    for feature in ("boq", "quote_pdf", "bid_split", "rfp_extract"):
        assert llm.resolve(feature, a).model == "qwen-3.5-4b"
        assert llm.resolve(feature, b).model == "qwen-3.8-27b"
    assert llm.active_model("boq", b) == "self-hosted:qwen-3.8-27b"


def test_feature_model_overrides_target_model_and_off_disables():
    s = _sh(
        self_hosted_llm_local_model="target-model",
        self_hosted_boq_model="feature-model",
        self_hosted_quote_pdf_model="OFF",
    )
    assert llm.resolve("boq", s).model == "feature-model"
    assert llm.resolve("estimate", s).model == "target-model"
    assert llm.resolve("quote_pdf", s).model == ""
    assert not llm.is_configured("quote_pdf", s)
    assert llm.is_configured("estimate", s)
    # "off" on the classify model also switches off the features that fall
    # back to it, instead of silently landing on the target model.
    s2 = _sh(self_hosted_llm_local_model="target-model", self_hosted_rfp_classify_model="off")
    assert llm.resolve("rfp_extract", s2).model == ""


# ── is_configured / active_model ────────────────────────────────────────────


def test_is_configured_third_party_requires_key():
    assert not llm.is_configured("boq", _s(anthropic_api_key=""))
    assert llm.is_configured("boq", _s(anthropic_api_key="a"))
    assert not llm.is_configured("email_vary", _s(openai_api_key=""))
    assert llm.is_configured("email_vary", _s(openai_api_key="o"))


def test_is_configured_self_hosted_requires_model():
    assert not llm.is_configured("boq", _sh())  # no model set
    assert llm.is_configured("boq", _sh(self_hosted_boq_model="m"))
    # 3rd-party keys are irrelevant while the flag is on
    assert not llm.is_configured("email_match", _sh(openai_api_key="o"))


def test_active_model_tags_self_hosted():
    assert llm.active_model("boq", _s()) == "claude-opus-4-8"
    assert llm.active_model("boq", _sh(self_hosted_boq_model="qwen3")) == "self-hosted:qwen3"


def test_unconfigured_errors_name_the_env_var():
    with pytest.raises(llm.LlmNotConfigured, match="SELF_HOSTED_BOQ_MODEL"):
        llm.complete_text("boq", system="s", messages=[], settings=_sh())
    with pytest.raises(llm.LlmNotConfigured, match="ANTHROPIC_API_KEY"):
        llm.complete_text("boq", system="s", messages=[], settings=_s(anthropic_api_key=""))
    with pytest.raises(llm.LlmNotConfigured, match="OPENAI_API_KEY"):
        llm.complete_text("proposal", system="s", messages=[], settings=_s(openai_api_key=""))


# ── Strict no-fallback guarantee ────────────────────────────────────────────


def test_self_hosted_calls_never_touch_third_party(monkeypatch):
    s = _sh(self_hosted_email_match_model="local-m")
    client, calls = _fake_sh_client(content='{"candidate_index": 1, "confidence": 0.9}')
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    monkeypatch.setattr(
        llm, "_build_client", lambda *a: pytest.fail("must not build a real client")
    )
    out = llm.complete_json(
        "email_match", system="sys", messages=[{"role": "user", "content": "x"}], settings=s
    )
    assert out["candidate_index"] == 1
    assert len(calls) == 1
    assert calls[0]["model"] == "local-m"
    # the request went to chat.completions with the system turn first
    assert calls[0]["messages"][0]["role"] == "system"


def test_self_hosted_failure_raises_instead_of_falling_back(monkeypatch):
    s = _sh(self_hosted_email_vary_model="local-m")
    req = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")

    def _down(**kwargs):
        raise openai.APIConnectionError(request=req)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_down))
    )
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(llm.SelfHostedUnreachable):
        llm.complete_text(
            "email_vary", system="s", messages=[{"role": "user", "content": "b"}], settings=s
        )
    # and the graceful feature-level fallback still applies:
    monkeypatch.setattr(openai_text, "get_settings", lambda: s)
    assert openai_text.vary_email_body("base body", []) == "base body"


def test_feature_fallbacks_when_self_hosted_unconfigured(monkeypatch):
    s = _sh()  # flag on, no per-feature models
    monkeypatch.setattr(openai_text, "get_settings", lambda: s)
    assert openai_text.vary_email_body("base body", []) == "base body"
    assert openai_text.alt_material_names("EMT", "Conduit") == []
    assert openai_text.extract_quote_from_pdf(b"%PDF-1.4", "q.pdf", {}) is None


# ── Self-hosted transport details ───────────────────────────────────────────


def test_self_hosted_retries_without_response_format_on_400(monkeypatch):
    req = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")
    err = openai.BadRequestError(
        "response_format is not supported",
        response=httpx.Response(400, request=req),
        body=None,
    )
    client, calls = _fake_sh_client(content='```json\n{"names": ["a"]}\n```', fail_first=err)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    out = llm.complete_json(
        "aliases",
        system="sys",
        messages=[{"role": "user", "content": "x"}],
        schema={"type": "object"},
        settings=_sh(self_hosted_aliases_model="m"),
    )
    assert out == {"names": ["a"]}  # fences tolerated
    assert "response_format" in calls[0] and "response_format" not in calls[1]
    # schema is embedded in the system prompt so the retry still knows the shape
    assert "JSON Schema" in calls[0]["messages"][0]["content"]


def test_self_hosted_truncation_raises(monkeypatch):
    client, _ = _fake_sh_client(content='{"partial', finish_reason="length")
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(ValueError, match="cut off"):
        llm.complete_json(
            "proposal",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            settings=_sh(self_hosted_proposal_model="m"),
        )


def test_self_hosted_pdf_without_text_degrades_to_manual_entry(monkeypatch):
    from pypdf import PdfWriter

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)

    s = _sh(self_hosted_quote_pdf_model="m")
    client, calls = _fake_sh_client()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(ValueError, match="no extractable text"):
        llm.complete_pdf_json(
            "quote_pdf",
            prompt="p",
            pdf_bytes=buf.getvalue(),
            filename="q.pdf",
            schema={"type": "object"},
            schema_name="quote_extraction",
            settings=s,
        )
    assert not calls  # nothing was sent anywhere
    monkeypatch.setattr(openai_text, "get_settings", lambda: s)
    assert openai_text.extract_quote_from_pdf(buf.getvalue(), "q.pdf", {}) is None


def test_parse_json_loose_handles_fence_shapes():
    assert llm.parse_json_loose('{"a": 1}') == {"a": 1}
    assert llm.parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json_loose('```\n{"a": 1}\n```') == {"a": 1}
    # whole reply on one line — small local models do this
    assert llm.parse_json_loose('```json {"a": 1}```') == {"a": 1}
    assert llm.parse_json_loose('```json {"a": 1}') == {"a": 1}
    with pytest.raises(ValueError, match="not valid JSON"):
        llm.parse_json_loose("no json here")


def test_self_hosted_merges_consecutive_same_role_turns(monkeypatch):
    # A caller may legitimately pass consecutive same-role turns ([user, user]);
    # strict-alternation chat templates (Mistral on vLLM/TGI) 400 unless merged.
    client, calls = _fake_sh_client(content='{"sites": []}')
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    llm.complete_json(
        "boq",
        system="sys",
        messages=[
            {"role": "user", "content": "doc"},
            {"role": "user", "content": "feedback"},
        ],
        settings=_sh(self_hosted_boq_model="m"),
    )
    roles = [m["role"] for m in calls[0]["messages"]]
    assert roles == ["system", "user"]
    assert calls[0]["messages"][1]["content"] == "doc\n\nfeedback"


# ── Boot-time config validation ─────────────────────────────────────────────


def test_flag_on_without_base_url_refuses_to_boot():
    with pytest.raises(ValueError, match="no base URL"):
        _s(full_self_hosted_llms_enabled=True)


def test_flag_on_with_bad_target_refuses_to_boot():
    with pytest.raises(ValueError, match="SELF_HOSTED_LLM_TARGET"):
        _sh(self_hosted_llm_target="cloud")


def test_flag_on_requires_url_scheme():
    with pytest.raises(ValueError, match="scheme"):
        _sh(self_hosted_llm_local_base_url="localhost:11434/v1")


def test_missing_ca_bundle_refuses_to_boot(tmp_path):
    with pytest.raises(ValueError, match="CA_BUNDLE"):
        _sh(self_hosted_llm_ca_bundle=str(tmp_path / "missing.pem"))


def test_production_refuses_plain_http_unless_acknowledged():
    prod = dict(
        environment="production",
        supabase_service_role_key="k",
        # Incidental: production also refuses to boot without a Gotenberg URL.
        gotenberg_url="http://bdr-gotenberg:3000",
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url="http://10.0.0.5:8000/v1",
    )
    with pytest.raises(ValueError, match="http"):
        _s(**prod)
    # explicit acknowledgement (private-network traffic) boots
    _s(**prod, self_hosted_llm_allow_http=True)
    # https needs no acknowledgement
    _s(
        **{**prod, "self_hosted_llm_local_base_url": "https://10.0.0.5:8000/v1"},
    )


def test_flag_off_ignores_self_hosted_config_entirely():
    # Nothing self-hosted is validated (or used) while the switch is off.
    s = _s(self_hosted_llm_target="cloud", self_hosted_llm_local_base_url="")
    assert llm.resolve("boq", s).provider == "anthropic"


def test_health_snapshot_grades_openai_only_features_on_openai_in_self_hosted_mode(monkeypatch):
    from app.services import llm_health

    client = SimpleNamespace(models=SimpleNamespace(list=lambda: [SimpleNamespace(id="qwen")]))
    client.with_options = lambda **kw: client
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    llm_health.reset_cache()
    snap = llm_health.check(_sh(self_hosted_llm_local_model="qwen", openai_api_key="o"))
    by_key = {f.key: f for f in snap.features}
    assert set(PLANS_FEATURES) <= set(by_key)
    assert {by_key[k].provider for k in PLANS_FEATURES} == {"OpenAI"}
    assert {p.provider for p in snap.providers} == {"self_hosted", "openai"}
    assert snap.status == "ok"
    # With the page's own flag off the plans features are left out, so a
    # self-hosted deployment without the page never shows amber for it.
    llm_health.reset_cache()
    snap = llm_health.check(_sh(self_hosted_llm_local_model="qwen", openai_api_key="", plans_reader_enabled=False))
    assert not {f.key for f in snap.features} & set(PLANS_FEATURES)
    assert snap.status == "ok"

    third_party = llm_health.check(_s(anthropic_api_key="a", openai_api_key="o"))
    by_key = {f.key: f for f in third_party.features}
    assert by_key["plans_sub"].label == "AI Estimating: sub agents"
    assert by_key["plans_chat"].model == "gpt-6-astra"
    llm_health.reset_cache()


# ── Anthropic JSON completions carry the schema ─────────────────────────────


def _fake_anthropic_client(text: str):
    """Messages-API stub that records the system prompt it was given."""
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

    client = SimpleNamespace(
        messages=SimpleNamespace(create=create), with_options=lambda **kw: client
    )
    return client, calls


def test_complete_json_on_anthropic_puts_the_schema_in_the_system_prompt(monkeypatch):
    """Claude has no response_format. Without the schema in the prompt the
    model answers in prose and every call fails to parse (the bid splitter's
    segment naming did exactly that on claude-opus-5)."""
    client, calls = _fake_anthropic_client('{"segments": [{"name": "Electrical Drawings"}]}')
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    schema = {"type": "object", "properties": {"segments": {"type": "array"}}}
    out = llm.complete_json(
        "bid_split",
        system="Name each segment.",
        messages=[{"role": "user", "content": "1. Electrical, pages 1-7"}],
        schema=schema,
        settings=_s(anthropic_api_key="a", openai_api_key="o", bid_split_llm_provider="anthropic"),
    )
    assert out == {"segments": [{"name": "Electrical Drawings"}]}
    system_text = "".join(part["text"] for part in calls[0]["system"])
    assert system_text.startswith("Name each segment.")
    assert '"segments"' in system_text and "JSON" in system_text


def test_complete_json_on_anthropic_without_a_schema_leaves_the_prompt_alone(monkeypatch):
    client, calls = _fake_anthropic_client('{"ok": true}')
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    llm.complete_json(
        "bid_split",
        system="Prompt that defines its own shape.",
        messages=[{"role": "user", "content": "x"}],
        settings=_s(anthropic_api_key="a", openai_api_key="o", bid_split_llm_provider="anthropic"),
    )
    assert calls[0]["system"][0]["text"] == "Prompt that defines its own shape."
