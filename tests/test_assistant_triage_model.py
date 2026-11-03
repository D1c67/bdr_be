"""Phase 8 triage model call (docs/ASSISTANT_PHASE8_PLAN.md 5.4, 16.1, WI-4).

`app/services/assistant_triage_model.classify` prices the batch from the
routed model, fails closed on an unpriced or non-OpenAI route, checks the
live-test cap, reserves under the company spend identity (never a person),
runs the call in the pipeline tier inside the company attribution, settles
the hold in every case and raises only its closed exception set.

`spend.KIND_TRIAGE`, `spend.COMPANY_SPEND_ID` and `repo.triage_company_spend`
(WI-5), the WI-6 settings and the `gpt-6-luna` price row are faked or
monkeypatched here until they land; `app.assistant.triage` (WI-2) is
replaced by a fake module so this file does not depend on its text.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.assistant import llm_messages, repo, spend
from app.core.config import get_settings
from app.services import assistant_triage_model as model
from app.services import llm, llm_gate, llm_health, llm_pricing

COMPANY = "00000000-0000-0000-0000-000000000001"   # 0196 A2a
PERSON = "11111111-2222-4333-8444-555555555555"
RUN = "22222222-3333-4444-8555-666666666666"
HOLD = "33333333-4444-4555-8666-777777777777"

LUNA = llm_pricing.ModelPrice(
    input=Decimal("0.1"),
    cached_input=Decimal("0.01"),
    output=Decimal("0.5"),
    long_context_threshold=272_000,
    long_input_multiplier=Decimal(2),
    long_output_multiplier=Decimal("1.5"),
)


class _Settings:
    """A view of the real settings with overrides, and attributes that can
    be made absent (the WI-6 settings do not exist yet in core/config)."""

    def __init__(self, missing=(), **over):
        object.__setattr__(self, "_base", get_settings())
        object.__setattr__(self, "_over", over)
        object.__setattr__(self, "_missing", set(missing))

    def __getattr__(self, name):
        if name in self._missing:
            raise AttributeError(name)
        if name in self._over:
            return self._over[name]
        return getattr(self._base, name)


def _settings(**over):
    base = {
        "full_self_hosted_llms_enabled": False,
        "assistant_allow_openai_in_self_hosted": False,
        "openai_api_key": "sk-test-not-real",
        "llm_cassette_mode": "off",
        "ai_eval_ledger_enforce": False,
        "llm_background_wait_seconds": 180.0,
        "third_party_llm_max_retries": 1,
    }
    base.update(over)
    wi6 = {"assistant_triage_model", "assistant_triage_max_output_tokens",
           "assistant_triage_timeout_seconds", "assistant_triage_test_spend_cap_usd",
           "assistant_triage_reasoning_effort"}
    return _Settings(missing=wi6 - set(over), **base)


def _pack(n: int = 3) -> dict:
    return {"items": [{"key": f"t{i + 1}", "kind": "bell.rfq_reply"} for i in range(n)]}


class _FakeTriage:
    SYSTEM_PROMPT = "You triage BDR notifications. " * 40
    ANSWER_SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}},
                     "required": ["items"], "additionalProperties": False}

    def __init__(self):
        self.estimate_args = None

    @staticmethod
    def render_pack(pack):
        return json.dumps(pack, sort_keys=True)

    def estimate_input_tokens(self, system, schema, rendered):
        self.estimate_args = (system, schema, rendered)
        return (len(system) + len(json.dumps(schema)) + len(rendered)) // 4


class _World:
    def __init__(self):
        self.reserves: list[dict] = []
        self.settles: list[tuple] = []
        self.spend_reads = 0
        self.calls: list[dict] = []
        self.today = {"spent": "0", "held": "0"}
        self.reserve_error: Exception | None = None
        self.call_result: object = {"items": []}
        # What llm.last_usage() / last_cost_usd() report after the fake call.
        self.usage: object = None
        self.cost: Decimal | None = None
        self.usage_reads: list[tuple] = []


@pytest.fixture
def world(monkeypatch):
    w = _World()
    fake = _FakeTriage()
    w.triage = fake
    monkeypatch.setattr(model, "_triage", lambda: fake)
    monkeypatch.setattr(spend, "COMPANY_SPEND_ID", COMPANY, raising=False)
    monkeypatch.setattr(spend, "KIND_TRIAGE", "triage", raising=False)
    monkeypatch.setitem(llm_pricing.PRICES, "gpt-6-luna", LUNA)

    def reserve(user_id, *, kind, amount, ttl, run_id=None, task_id=None, approval_id=None, settings=None):
        w.reserves.append({"user_id": user_id, "kind": kind, "amount": amount, "ttl": ttl, "run_id": run_id})
        if w.reserve_error is not None:
            raise w.reserve_error
        return HOLD

    def settle(user_id, hold_id):
        w.settles.append((user_id, hold_id))

    def company_spend(user_id):
        assert user_id is None
        w.spend_reads += 1
        return w.today

    def complete_json(feature, *, system, messages, schema=None, schema_name="response",
                      max_tokens=None, timeout=None, settings=None, reasoning_effort=None):
        w.calls.append({
            "feature": feature, "system": system, "messages": messages, "schema": schema,
            "schema_name": schema_name, "max_tokens": max_tokens, "timeout": timeout,
            "reasoning_effort": reasoning_effort,
            "tier": llm_gate.current_tier(), "attribution": llm_messages.current_attribution(),
            "model": llm.resolve(feature, settings).model,
        })
        if isinstance(w.call_result, BaseException):
            raise w.call_result
        return w.call_result

    monkeypatch.setattr(spend, "reserve", reserve)
    monkeypatch.setattr(spend, "settle", settle)
    monkeypatch.setattr(repo, "triage_company_spend", company_spend, raising=False)
    def last_usage():
        w.usage_reads.append(("usage", len(w.calls), len(w.settles)))
        return w.usage

    def last_cost_usd():
        w.usage_reads.append(("cost", len(w.calls), len(w.settles)))
        return w.cost

    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "last_usage", last_usage)
    monkeypatch.setattr(llm, "last_cost_usd", last_cost_usd)
    return w


# ── The feature, its default and its label ──────────────────────────────────


def test_the_feature_is_pinned_to_openai_and_not_an_estimating_feature():
    spec = llm._FEATURES["assistant_triage"]
    assert spec.openai_pinned is True and spec.openai_only is False and spec.switch is None
    assert "assistant_triage" not in llm.PLANS_FEATURES
    assert model.FEATURE == "assistant_triage"


def test_the_feature_resolves_without_the_setting_through_the_getattr_default():
    s = _settings()
    assert not hasattr(s, "assistant_triage_model")
    route = llm.resolve("assistant_triage", s)
    assert (route.provider, route.model) == ("openai", "gpt-6-luna")


def test_the_feature_follows_the_setting_once_it_exists():
    route = llm.resolve("assistant_triage", _settings(assistant_triage_model="gpt-6-luna-2026-10-01"))
    assert route.model == "gpt-6-luna-2026-10-01"


def test_the_feature_has_its_model_status_label():
    assert llm_health.FEATURE_LABELS["assistant_triage"] == "BDR Assistant: notification triage"


# ── Fail closed before any reserve ──────────────────────────────────────────


def test_an_unpriced_model_raises_before_any_reserve(world):
    with pytest.raises(model.TriageUnpriced) as err:
        model.classify(RUN, _pack(), settings=_settings(assistant_triage_model="gpt-unpriced-x",
                                                        assistant_triage_test_spend_cap_usd=1))
    assert err.value.error_kind == "unpriced"
    assert world.reserves == [] and world.calls == [] and world.settles == [] and world.spend_reads == 0


def test_a_self_hosted_route_raises_unpriced_before_any_reserve(world):
    s = _settings(full_self_hosted_llms_enabled=True, assistant_allow_openai_in_self_hosted=False)
    assert llm.resolve("assistant_triage", s).provider == "self_hosted"
    with pytest.raises(model.TriageUnpriced):
        model.classify(RUN, _pack(), settings=s)
    assert world.reserves == [] and world.calls == [] and world.settles == []


def test_the_self_hosted_allow_keeps_the_feature_on_openai(world):
    s = _settings(full_self_hosted_llms_enabled=True, assistant_allow_openai_in_self_hosted=True)
    model.classify(RUN, _pack(), settings=s)
    assert world.calls[0]["model"] == "gpt-6-luna"


def test_an_anthropic_priced_model_is_refused(world):
    # A price row that belongs to another provider is not an OpenAI price.
    with pytest.raises(model.TriageUnpriced):
        model.classify(RUN, _pack(), settings=_settings(assistant_triage_model="claude-opus-5-5"))
    assert world.reserves == []


def test_a_batch_that_cannot_be_rendered_is_a_bad_answer_with_no_reserve(world, monkeypatch):
    def boom(pack):
        raise ValueError("bad pack")

    monkeypatch.setattr(world.triage, "render_pack", boom)
    with pytest.raises(model.TriageBadAnswer):
        model.classify(RUN, _pack(), settings=_settings())
    assert world.reserves == [] and world.calls == []


def test_a_missing_company_spend_identity_fails_closed(world, monkeypatch):
    monkeypatch.delattr(spend, "COMPANY_SPEND_ID")
    with pytest.raises(model.TriageSpendCheck):
        model.classify(RUN, _pack(), settings=_settings())
    assert world.reserves == [] and world.calls == []


# ── The hold ────────────────────────────────────────────────────────────────


def test_the_hold_is_priced_from_the_route_model_over_prompt_schema_and_pack(world):
    pack = _pack(4)
    model.classify(RUN, pack, settings=_settings())
    rendered = _FakeTriage.render_pack(pack)
    assert world.triage.estimate_args == (_FakeTriage.SYSTEM_PROMPT, _FakeTriage.ANSWER_SCHEMA, rendered)
    est_in = world.triage.estimate_input_tokens(*world.triage.estimate_args)
    expected = llm_pricing.estimate_cost(LUNA, input_tokens=est_in, output_tokens=1200 + 200 * 4)
    (r,) = world.reserves
    assert r["amount"] == expected and r["amount"] > 0
    # The system prompt alone is over 1000 characters: the estimate covers it.
    assert est_in > len(_FakeTriage.SYSTEM_PROMPT) // 4


@pytest.mark.parametrize(("items", "setting", "expected"), [
    (1, None, 1400), (5, None, 2200), (10, None, 3200), (10, 1500, 1500), (3, 6000, 1800),
])
def test_max_tokens_is_the_smaller_of_the_setting_and_the_batch_formula(world, items, setting, expected):
    over = {} if setting is None else {"assistant_triage_max_output_tokens": setting}
    model.classify(RUN, _pack(items), settings=_settings(**over))
    assert world.calls[0]["max_tokens"] == expected


def test_p8l3_a_one_item_batch_allows_the_live_reasoning_and_its_answer(world):
    # Live run 864140d6 was cut off at 600 after up to 681 reasoning tokens for one
    # item (step 4 used 778 output tokens for 3 items): the base now covers that.
    model.classify(RUN, _pack(1), settings=_settings())
    assert world.calls[0]["max_tokens"] == 1400 > 778
    assert model.OUTPUT_TOKENS_BASE == 1200 and model.OUTPUT_TOKENS_PER_ITEM == 200
    assert model.DEFAULT_MAX_OUTPUT_TOKENS == 1200 + 200 * 10


@pytest.mark.parametrize(("setting", "expected"), [
    (None, None), ("low", "low"), (" Medium ", "medium"), ("high", "high"), ("", None), ("bogus", None),
])
def test_p8l3_the_call_carries_the_triage_reasoning_effort(world, setting, expected):
    over = {} if setting is None else {"assistant_triage_reasoning_effort": setting}
    model.classify(RUN, _pack(1), settings=_settings(**over))
    assert world.calls[0]["reasoning_effort"] == expected


def test_the_hold_ttl_is_the_lease_formula():
    assert model.triage_hold_ttl(_settings()) == 180 + 45 * 2 + 60 == 330
    s = _settings(llm_background_wait_seconds=100.0, third_party_llm_max_retries=2,
                  assistant_triage_timeout_seconds=30)
    assert model.triage_hold_ttl(s) == 100 + 30 * 3 + 60


def test_the_reserve_uses_the_company_id_kind_triage_and_the_lease_ttl(world):
    model.classify(RUN, _pack(), settings=_settings())
    (r,) = world.reserves
    assert r["user_id"] == COMPANY != PERSON
    assert r["kind"] == "triage" and r["run_id"] == RUN and r["ttl"] == 330


def test_the_call_runs_in_the_pipeline_tier_inside_the_company_attribution(world):
    assert llm_gate.current_tier() == llm_gate.TIER_INTERACTIVE
    result = model.classify(RUN, _pack(2), settings=_settings())
    assert isinstance(result, model.TriageResult) and result.answer == {"items": []}
    (c,) = world.calls
    assert c["tier"] == "pipeline"
    att = c["attribution"]
    assert (att.user_id, att.run_id, att.reservation_id) == (COMPANY, RUN, HOLD)
    assert c["feature"] == "assistant_triage" and c["schema_name"] == "triage_answer"
    assert c["system"] == _FakeTriage.SYSTEM_PROMPT and c["schema"] == _FakeTriage.ANSWER_SCHEMA
    assert c["messages"] == [{"role": "user", "content": _FakeTriage.render_pack(_pack(2))}]
    assert c["timeout"] == 45.0
    # Both context managers are closed afterwards.
    assert llm_gate.current_tier() == llm_gate.TIER_INTERACTIVE
    assert llm_messages.current_attribution() is None
    assert world.settles == [(COMPANY, HOLD)]


def test_a_failed_settle_does_not_hide_the_answer(world, monkeypatch):
    def bad_settle(user_id, hold_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(spend, "settle", bad_settle)
    world.call_result = {"items": [{"key": "t1"}]}
    result = model.classify(RUN, _pack(1), settings=_settings())
    assert result.answer == {"items": [{"key": "t1"}]} and result.usage["reservation_id"] == HOLD


# ── The run's usage (WI7-1) ─────────────────────────────────────────────────


def test_the_result_carries_the_model_hold_tokens_and_cost_read_right_after_the_call(world):
    world.usage = llm.LlmUsage(input_tokens=1200, cached_tokens=300, output_tokens=450, reasoning_tokens=200)
    world.cost = Decimal("0.000345")
    result = model.classify(RUN, _pack(2), settings=_settings())
    assert result.usage == {"model": "gpt-6-luna", "reservation_id": HOLD, "input_tokens": 1200,
                            "cached_tokens": 300, "output_tokens": 450, "cost_usd": Decimal("0.000345")}
    # Read once each, after the one call and before the settle.
    assert world.usage_reads == [("usage", 1, 0), ("cost", 1, 0)]


def test_the_usage_names_the_routed_model_not_the_raw_setting(world):
    result = model.classify(RUN, _pack(1), settings=_settings(assistant_triage_model="gpt-6-luna-2026-10-01"))
    assert result.usage["model"] == "gpt-6-luna-2026-10-01"


def test_no_reported_usage_leaves_only_the_model_and_the_hold(world):
    result = model.classify(RUN, _pack(1), settings=_settings())
    assert result.usage == {"model": "gpt-6-luna", "reservation_id": HOLD}


def test_a_failed_usage_read_never_hides_the_answer(world, monkeypatch):
    def broken():
        raise RuntimeError("contextvar gone")

    monkeypatch.setattr(llm, "last_usage", broken)
    result = model.classify(RUN, _pack(1), settings=_settings())
    assert result.answer == {"items": []} and result.usage == {"model": "gpt-6-luna", "reservation_id": HOLD}
    assert world.settles == [(COMPANY, HOLD)]


def test_a_failure_after_the_call_carries_the_usage_on_the_error(world):
    world.usage = llm.LlmUsage(input_tokens=900, output_tokens=2400)
    world.cost = Decimal("0.00129")
    world.call_result = RuntimeError("provider 500")
    with pytest.raises(model.TriageModelError) as err:
        model.classify(RUN, _pack(1), settings=_settings())
    assert err.value.usage == {"model": "gpt-6-luna", "reservation_id": HOLD, "input_tokens": 900,
                               "cached_tokens": 0, "output_tokens": 2400, "cost_usd": Decimal("0.00129")}


def test_a_failure_before_the_call_carries_no_usage(world):
    world.reserve_error = RuntimeError("rpc down")
    with pytest.raises(model.TriageSpendCheck) as err:
        model.classify(RUN, _pack(), settings=_settings())
    assert err.value.usage is None and world.usage_reads == []


# ── Spend refusals ──────────────────────────────────────────────────────────


def test_a_spend_refusal_is_cap_global_and_makes_no_call(world):
    state = spend.SpendState(*(Decimal("0"),) * 6)
    world.reserve_error = spend.SpendRefused(spend.SCOPE_GLOBAL, state)
    with pytest.raises(model.TriageCapRefused) as err:
        model.classify(RUN, _pack(), settings=_settings())
    assert err.value.error_kind == "cap_global" and err.value.item_class == "cap"
    assert world.calls == [] and world.settles == []


def test_a_reserve_rpc_error_is_spend_check_and_makes_no_call(world):
    world.reserve_error = RuntimeError("rpc failed")
    with pytest.raises(model.TriageSpendCheck) as err:
        model.classify(RUN, _pack(), settings=_settings())
    assert err.value.error_kind == "spend_check"
    assert world.calls == [] and world.settles == []


# ── The live-test cap ───────────────────────────────────────────────────────


def test_no_test_cap_means_no_read_of_company_spend(world):
    model.classify(RUN, _pack(), settings=_settings())
    assert world.spend_reads == 0 and len(world.reserves) == 1


def test_a_zero_test_cap_refuses_before_any_reserve(world):
    with pytest.raises(model.TriageTestCap) as err:
        model.classify(RUN, _pack(), settings=_settings(assistant_triage_test_spend_cap_usd=0))
    assert err.value.error_kind == "cap_test" and err.value.item_class == "cap"
    assert world.spend_reads == 1 and world.reserves == [] and world.calls == []


def test_a_one_dollar_cap_refuses_when_spend_plus_holds_plus_the_hold_pass_it(world):
    world.today = {"spent": "0.9990", "held": "0.0005"}
    with pytest.raises(model.TriageTestCap):
        model.classify(RUN, _pack(10), settings=_settings(assistant_triage_test_spend_cap_usd=1))
    assert world.reserves == []


def test_a_one_dollar_cap_admits_when_there_is_room(world):
    world.today = {"spent": "0.5", "held": "0.1"}
    model.classify(RUN, _pack(10), settings=_settings(assistant_triage_test_spend_cap_usd=Decimal("1")))
    assert world.spend_reads == 1 and len(world.reserves) == 1 and len(world.calls) == 1


def test_a_failed_company_spend_read_fails_closed(world, monkeypatch):
    def broken(user_id):
        raise RuntimeError("rpc missing")

    monkeypatch.setattr(repo, "triage_company_spend", broken, raising=False)
    with pytest.raises(model.TriageSpendCheck):
        model.classify(RUN, _pack(), settings=_settings(assistant_triage_test_spend_cap_usd=1))
    assert world.reserves == []


# ── The closed exception set (F7) ───────────────────────────────────────────


def _real_cut_off_responses() -> list[llm.LlmBadOutput]:
    """The messages services/llm really raises for a cut-off answer: the
    Responses API path (`_raise_if_incomplete`) and the self-hosted chat
    path (`_self_hosted_call`, finish_reason length)."""
    out = []
    with pytest.raises(llm.LlmBadOutput) as a:
        llm._raise_if_incomplete(SimpleNamespace(
            status="incomplete", incomplete_details=SimpleNamespace(reason="max_output_tokens"), usage=None,
        ))
    out.append(a.value)

    class _Chat:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        @staticmethod
        def _create(**kwargs):
            choice = SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"items": ['))
            return SimpleNamespace(choices=[choice], usage=None)

    with pytest.raises(llm.LlmBadOutput) as b:
        llm._self_hosted_call(_Chat(), "m", "sys", [{"role": "user", "content": "x"}], 10, None, "r", True)
    out.append(b.value)
    return out


def test_the_cut_off_prefix_matches_the_real_messages():
    for exc in _real_cut_off_responses():
        assert str(exc).startswith(model.CUT_OFF_PREFIX), str(exc)
        assert isinstance(model._translate(exc), model.TriageTruncated)


def _real_invalid_json() -> llm.LlmBadOutput:
    with pytest.raises(llm.LlmBadOutput) as err:
        llm.parse_json_loose("this is not json")
    return err.value


def _timeout() -> Exception:
    return openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


@pytest.mark.parametrize(("make", "expected", "kind"), [
    (lambda: _real_cut_off_responses()[0], model.TriageTruncated, "truncated"),
    (lambda: _real_cut_off_responses()[1], model.TriageTruncated, "truncated"),
    (_real_invalid_json, model.TriageBadAnswer, "bad_answer"),
    (lambda: llm.LlmBadOutputPermanent("The model refused."), model.TriageBadAnswer, "bad_answer"),
    (lambda: llm_gate.LlmBusy("full"), model.TriageBusy, "busy"),
    (_timeout, model.TriageTimeout, "timeout"),
    (lambda: httpx.ReadTimeout("slow"), model.TriageTimeout, "timeout"),
    (lambda: openai.APIConnectionError(request=httpx.Request("POST", "https://x.invalid")),
     model.TriageModelError, "model_error"),
    (lambda: llm.LlmNotConfigured("OPENAI_API_KEY is not configured on the server."),
     model.TriageModelError, "model_error"),
    (lambda: RuntimeError("provider 500"), model.TriageModelError, "model_error"),
])
def test_every_call_failure_maps_into_the_closed_set_and_the_hold_is_settled(world, make, expected, kind):
    world.call_result = make()
    with pytest.raises(model.TriageError) as err:
        model.classify(RUN, _pack(), settings=_settings())
    assert type(err.value) is expected and err.value.error_kind == kind
    assert type(err.value) in model.ALL
    assert err.value.__cause__ is world.call_result
    assert world.settles == [(COMPANY, HOLD)]


def test_the_closed_set_and_its_error_kinds():
    assert {cls.error_kind: cls.item_class for cls in model.ALL} == {
        "unpriced": "infrastructure",
        "cap_test": "cap",
        "cap_global": "cap",
        "spend_check": "infrastructure",
        "truncated": "answer_quality",
        "bad_answer": "answer_quality",
        "busy": "infrastructure",
        "timeout": "infrastructure",
        "model_error": "infrastructure",
    }
    assert len(model.ALL) == 9
    assert set(model.BREAKER) == {model.TriageModelError, model.TriageTimeout, model.TriageBusy}


# ── Through the real services/llm stack ─────────────────────────────────────


class _FakeOpenAI:
    """Stands in for openai.OpenAI behind llm._client_for."""

    def __init__(self, resp):
        self._resp = resp
        self.requests: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def with_options(self, **kwargs):
        self.options = kwargs
        return self

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._resp


def _resp(text: str, *, status: str = "completed"):
    usage = SimpleNamespace(input_tokens=900, output_tokens=120,
                            input_tokens_details=SimpleNamespace(cached_tokens=0),
                            output_tokens_details=SimpleNamespace(reasoning_tokens=60))
    return SimpleNamespace(status=status, output_text=text, usage=usage,
                           incomplete_details=SimpleNamespace(reason="max_output_tokens"))


@pytest.fixture
def stack(monkeypatch):
    fake = _FakeTriage()
    monkeypatch.setattr(model, "_triage", lambda: fake)
    monkeypatch.setattr(spend, "COMPANY_SPEND_ID", COMPANY, raising=False)
    monkeypatch.setattr(spend, "KIND_TRIAGE", "triage", raising=False)
    monkeypatch.setitem(llm_pricing.PRICES, "gpt-6-luna", LUNA)
    settles: list = []
    monkeypatch.setattr(spend, "reserve", lambda *a, **k: HOLD)
    monkeypatch.setattr(spend, "settle", lambda u, h: settles.append((u, h)))
    logs: list = []

    def log_call(**kwargs):
        att = llm_messages.current_attribution()
        logs.append({**kwargs, "attribution": att})

    monkeypatch.setattr(llm_gate, "log_call", log_call)
    llm_gate.reset_gates()
    return SimpleNamespace(settles=settles, logs=logs, monkeypatch=monkeypatch)


def test_a_real_complete_json_call_logs_the_company_attribution_in_the_pipeline_tier(stack):
    client = _FakeOpenAI(_resp('{"items": [{"key": "t1"}]}'))
    stack.monkeypatch.setattr(llm, "_client_for", lambda route, s: client)
    result = model.classify(RUN, _pack(1), settings=_settings())
    assert result.answer == {"items": [{"key": "t1"}]}
    cost = llm_pricing.estimate_cost(LUNA, input_tokens=900, output_tokens=120)
    assert result.usage == {"model": "gpt-6-luna", "reservation_id": HOLD, "input_tokens": 900,
                            "cached_tokens": 0, "output_tokens": 120, "cost_usd": cost}
    (req,) = client.requests
    assert req["model"] == "gpt-6-luna" and req["max_output_tokens"] == 1400
    assert "reasoning" not in req                               # P8L-3: the model's default effort
    assert req["text"]["format"]["name"] == "triage_answer" and req["text"]["format"]["strict"] is True
    (log,) = stack.logs
    assert log["feature"] == "assistant_triage" and log["model"] == "gpt-6-luna"
    assert log["tier_name"] == "pipeline" and log["ok"] is True
    assert (log["attribution"].user_id, log["attribution"].run_id, log["attribution"].reservation_id) == (
        COMPANY, RUN, HOLD)
    assert log["cost_usd"] == llm_pricing.estimate_cost(LUNA, input_tokens=900, output_tokens=120)
    assert stack.settles == [(COMPANY, HOLD)]


def test_a_real_cut_off_answer_is_truncated_and_settled(stack):
    client = _FakeOpenAI(_resp('{"items": [', status="incomplete"))
    stack.monkeypatch.setattr(llm, "_client_for", lambda route, s: client)
    with pytest.raises(model.TriageTruncated) as err:
        model.classify(RUN, _pack(1), settings=_settings())
    (log,) = stack.logs
    assert log["ok"] is False and log["input_tokens"] == 900   # an incomplete answer still bills
    # The run records the same billed numbers the call log row carries.
    assert (err.value.usage["input_tokens"], err.value.usage["output_tokens"]) == (900, 120)
    assert err.value.usage["cost_usd"] == log["cost_usd"] and err.value.usage["reservation_id"] == HOLD
    assert stack.settles == [(COMPANY, HOLD)]


def test_p8l3_a_set_effort_reaches_the_responses_request(stack):
    client = _FakeOpenAI(_resp('{"items": [{"key": "t1"}]}'))
    stack.monkeypatch.setattr(llm, "_client_for", lambda route, s: client)
    model.classify(RUN, _pack(1), settings=_settings(assistant_triage_reasoning_effort="low"))
    (req,) = client.requests
    assert req["reasoning"] == {"effort": "low"}


def test_p8l4_a_cut_off_sdk_usage_object_is_billed_and_logged(stack):
    """P8L-4: the usage of a real incomplete Responses reply, in the shape the
    openai SDK hands back (types.responses.ResponseUsage, with the extra
    cache_write_tokens the API now sends), copied from a dev probe on
    2026-11-03 (gpt-6-luna, max_output_tokens 120, status incomplete): the
    tokens and the cost reach the llm_call_log row and the run, never zeros."""
    from openai.types.responses import ResponseUsage

    usage = ResponseUsage.model_validate({
        "input_tokens": 1047, "input_tokens_details": {"cached_tokens": 1044, "cache_write_tokens": 0},
        "output_tokens": 120, "output_tokens_details": {"reasoning_tokens": 120}, "total_tokens": 1167})
    resp = SimpleNamespace(status="incomplete", output_text="", usage=usage,
                           incomplete_details=SimpleNamespace(reason="max_output_tokens"))
    client = _FakeOpenAI(resp)
    stack.monkeypatch.setattr(llm, "_client_for", lambda route, s: client)
    with pytest.raises(model.TriageTruncated) as err:
        model.classify(RUN, _pack(1), settings=_settings())
    (log,) = stack.logs
    assert log["ok"] is False
    assert (log["input_tokens"], log["cached_tokens"], log["output_tokens"], log["reasoning_tokens"]) == (
        1047, 1044, 120, 120)
    assert log["cost_usd"] is not None and log["cost_usd"] > 0
    assert err.value.usage["output_tokens"] == 120 and err.value.usage["cost_usd"] == log["cost_usd"]
    assert stack.settles == [(COMPANY, HOLD)]


def test_p8l4r1_a_cut_off_call_that_reports_zero_output_records_the_allowance(stack):
    # The dev row 6bd16e16: a usage object came back with every count 0 on a
    # max_output_tokens cut-off. services/llm floors the llm_call_log row
    # (P8L-4: max_out output tokens and the estimated input, priced) and the
    # run records the same numbers, so the hold settles at the cost the caps sum.
    from openai.types.responses import ResponseUsage

    usage = ResponseUsage.model_validate({
        "input_tokens": 0, "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 0})
    resp = SimpleNamespace(status="incomplete", output_text="", usage=usage,
                           incomplete_details=SimpleNamespace(reason="max_output_tokens"))
    client = _FakeOpenAI(resp)
    stack.monkeypatch.setattr(llm, "_client_for", lambda route, s: client)
    with pytest.raises(model.TriageTruncated) as err:
        model.classify(RUN, _pack(1), settings=_settings())
    (log,) = stack.logs
    (req,) = client.requests
    got = err.value.usage
    assert log["output_tokens"] == got["output_tokens"] == req["max_output_tokens"] == 1400
    assert log["input_tokens"] == got["input_tokens"] and log["cost_usd"] == got["cost_usd"]
    assert got["input_tokens"] > 0 and got["cached_tokens"] == 0
    assert got["cost_usd"] == llm_pricing.estimate_cost(LUNA, input_tokens=got["input_tokens"],
                                                        output_tokens=1400)
    assert got["cost_usd"] > 0 and stack.settles == [(COMPANY, HOLD)]


def test_p8l4r1_only_a_cut_off_with_no_output_is_floored():
    price = LUNA
    real = {"model": "m", "reservation_id": HOLD, "input_tokens": 900, "cached_tokens": 0,
            "output_tokens": 120, "cost_usd": Decimal("0.0001")}
    assert model._floor_cut_off_usage(real, price, 800, 1400) is real
    bare = {"model": "m", "reservation_id": HOLD}
    floored = model._floor_cut_off_usage(bare, price, 800, 1400)
    assert (floored["input_tokens"], floored["cached_tokens"], floored["output_tokens"]) == (800, 0, 1400)
    assert floored["cost_usd"] == llm_pricing.estimate_cost(price, input_tokens=800, output_tokens=1400)
    assert bare == {"model": "m", "reservation_id": HOLD}


def test_p8l4r1_a_model_error_with_zero_output_is_not_floored(world):
    world.usage = llm.LlmUsage(input_tokens=900, output_tokens=0)
    world.cost = Decimal("0")
    world.call_result = RuntimeError("provider 500")
    with pytest.raises(model.TriageModelError) as err:
        model.classify(RUN, _pack(1), settings=_settings())
    assert err.value.usage["output_tokens"] == 0 and err.value.usage["cost_usd"] == Decimal("0")


# ── Source pins ─────────────────────────────────────────────────────────────


_SOURCE = Path(model.__file__).read_text(encoding="utf-8")


def test_only_translate_names_the_llm_exception_classes():
    tree = ast.parse(_SOURCE)
    names = ("LlmBadOutput", "LlmBadOutputPermanent", "LlmBusy")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name != "_translate":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute):
                    assert sub.attr not in names, node.name


def test_the_model_module_names_no_sender_loop_or_prompt_module():
    for word in ("graph_email", "teams_alerts", "send_mail", "app.assistant.loop", "assistant.jobs",
                 "executor", "prompts", "complete_messages"):
        assert word not in _SOURCE, word


def test_the_module_is_plain_def_and_has_no_dashes():
    tree = ast.parse(_SOURCE)
    assert not any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree))
    test_text = Path(__file__).read_text(encoding="utf-8")
    for text in (_SOURCE, test_text):
        assert chr(0x2014) not in text and chr(0x2013) not in text
