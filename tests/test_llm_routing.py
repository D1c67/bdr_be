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
    for feature in llm._FEATURES:
        assert llm.resolve(feature, s).provider == "self_hosted"
    assert llm.resolve("boq", s).model == "m1"
    assert llm.resolve("boq", s).base_url == "http://localhost:11434/v1"


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
