"""P8L-4: a Responses call cut off at max_output_tokens that reports no
output tokens is logged at its allowance (services/llm._floor_cut_off).

Invariants pinned here:

  floor       status incomplete + reason max_output_tokens + 0 output tokens
              (or no usage at all) logs output_tokens = the request's
              max_output_tokens (else the one the response echoes), keeps a
              reported input, and estimates the input from the request text
              (len/4, rounded up; image and file payloads skipped) when none
              was reported. The cost is priced from those numbers.
  unchanged   a cut-off that reports output, a completed call, another
              incomplete reason, and a cut-off with no known allowance keep
              the provider's numbers exactly.
  transports  complete_json / complete_text (_openai_call), complete_parts
              and complete_turn all floor the llm_call_log row and
              last_usage() / last_cost_usd().
  triage      the run's usage equals the floored llm_call_log row, so
              assistant_triage_model._floor_cut_off_usage does not fire a
              second time and the hold settles at the same cost the company
              and test caps sum (no double count).

No test reaches the network or Supabase: clients are fakes and
llm_gate.log_call is captured (tests/conftest.py _no_real_supabase stays on).
"""

import math
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import llm, llm_gate, llm_parts, llm_pricing


def _s(**over) -> Settings:
    base = dict(_env_file=None, openai_api_key="o-key", anthropic_api_key="a-key")
    base.update(over)
    return Settings(**base)


def _usage(inp=0, cached=0, out=0, reasoning=0):
    return SimpleNamespace(
        input_tokens=inp,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens=out,
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def _resp(text="", *, usage=None, status="incomplete", reason="max_output_tokens", **extra):
    return SimpleNamespace(
        id="resp_1",
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output_text=text,
        output=[],
        usage=usage,
        **extra,
    )


class FakeOpenAI:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)

    def with_options(self, **kw):
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


MODEL = "gpt-6-astra-2026-09-03"


def _json_call(monkeypatch, resp, *, max_tokens=500, system="s" * 40, content="u" * 80):
    client = _install(monkeypatch, FakeOpenAI(resp))
    with pytest.raises(llm.LlmBadOutput, match="cut off"):
        llm.complete_json("proposal", system=system, messages=[{"role": "user", "content": content}],
                          max_tokens=max_tokens, settings=_s(openai_proposal_model=MODEL))
    return client.calls[0]


# ── The floor itself ────────────────────────────────────────────────────────


def test_zero_usage_cut_off_logs_the_allowance_and_an_estimated_input(monkeypatch, logged):
    req = _json_call(monkeypatch, _resp(usage=_usage()))
    (row,) = logged
    expected_in = math.ceil(llm._request_chars(
        {k: req.get(k) for k in ("instructions", "input", "tools", "text")}) / 4)
    assert expected_in >= (40 + 80) // 4
    assert row["ok"] is False
    assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"], row["reasoning_tokens"]) == (
        expected_in, 0, 500, 0)
    assert row["cost_usd"] == llm_pricing.estimate_cost(MODEL, input_tokens=expected_in, output_tokens=500)
    assert row["cost_usd"] > 0
    assert llm.last_usage() == llm.LlmUsage(expected_in, 0, 500, 0)
    assert llm.last_cost_usd() == row["cost_usd"]


def test_a_reported_input_is_kept_and_only_the_output_is_floored(monkeypatch, logged):
    _json_call(monkeypatch, _resp(usage=_usage(inp=1047, cached=1044)))
    (row,) = logged
    assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"]) == (1047, 1044, 500)
    assert row["cost_usd"] == llm_pricing.estimate_cost(
        MODEL, input_tokens=1047, cached_tokens=1044, output_tokens=500)


def test_no_usage_object_at_all_is_floored_too(monkeypatch, logged):
    _json_call(monkeypatch, _resp(usage=None))
    (row,) = logged
    assert row["output_tokens"] == 500 and row["input_tokens"] > 0 and row["cost_usd"] > 0


def test_a_cut_off_that_reports_output_is_left_exactly_as_reported(monkeypatch, logged):
    _json_call(monkeypatch, _resp(usage=_usage(inp=1022, out=16, reasoning=16)))
    (row,) = logged
    assert (row["input_tokens"], row["output_tokens"], row["reasoning_tokens"]) == (1022, 16, 16)
    assert row["cost_usd"] == llm_pricing.estimate_cost(MODEL, input_tokens=1022, output_tokens=16)


@pytest.mark.parametrize("status,reason", [
    ("completed", None),
    ("incomplete", "content_filter"),
    ("incomplete", "other"),
])
def test_other_outcomes_with_zero_usage_are_not_floored(status, reason):
    resp = _resp(usage=_usage(), status=status, reason=reason)
    assert llm._openai_usage(resp, {"max_output_tokens": 500, "instructions": "x" * 400}) == llm.LlmUsage()
    assert llm._openai_usage(_resp(usage=None, status=status, reason=reason),
                             {"max_output_tokens": 500}) is None


def test_without_a_known_allowance_nothing_is_floored():
    assert llm._openai_usage(_resp(usage=_usage())) == llm.LlmUsage()
    assert llm._openai_usage(_resp(usage=None)) is None
    assert llm._openai_usage(_resp(usage=_usage()), {"max_output_tokens": None}) == llm.LlmUsage()


def test_the_echoed_allowance_is_used_when_no_request_is_given():
    # The openai SDK Response echoes max_output_tokens; app/assistant/llm_messages
    # calls _raise_if_unusable_parts(resp) without the request.
    got = llm._openai_usage(_resp(usage=_usage(), max_output_tokens=900))
    assert got == llm.LlmUsage(0, 0, 900, 0)          # no request text, so no input estimate
    got = llm._openai_usage(_resp(usage=_usage(), max_output_tokens=900), {"max_output_tokens": 700})
    assert got.output_tokens == 700                   # what was sent wins over the echo


def test_the_input_estimate_skips_image_file_and_encrypted_payloads():
    blob = "A" * 1_000_000
    req = {
        "max_output_tokens": 100,
        "instructions": "abcd" * 10,
        "input": [{"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64," + blob},
            {"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64," + blob},
            {"type": "reasoning", "encrypted_content": blob},
        ]}],
    }
    got = llm._openai_usage(_resp(usage=None), req)
    assert got.output_tokens == 100
    assert 10 <= got.input_tokens < 100


def test_a_broken_usage_never_raises():
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    assert llm._openai_usage(_resp(usage=Boom()), {"max_output_tokens": 5}) is None


def test_a_failing_floor_keeps_the_provider_numbers(monkeypatch):
    def boom(*a, **k):
        raise RecursionError("deep")

    monkeypatch.setattr(llm, "_request_chars", boom)
    got = llm._openai_usage(_resp(usage=_usage(cached=3)), {"max_output_tokens": 5, "input": "x"})
    assert got == llm.LlmUsage(0, 3, 0, 0)           # the reported usage survives, unfloored


def test_reasoning_never_exceeds_the_floored_output():
    got = llm._openai_usage(_resp(usage=_usage(inp=10, reasoning=900)), {"max_output_tokens": 500})
    assert got.output_tokens == 500 and got.reasoning_tokens == 500


def test_the_cut_off_message_is_unchanged_without_a_request():
    # tests/test_assistant_triage_model.py pins the prefix with a one-argument call.
    with pytest.raises(llm.LlmBadOutput, match="^Model response was cut off"):
        llm._raise_if_incomplete(_resp(usage=None))


# ── Every Responses transport ───────────────────────────────────────────────


def test_complete_text_floors(monkeypatch, logged):
    _install(monkeypatch, FakeOpenAI(_resp(usage=_usage())))
    with pytest.raises(llm.LlmBadOutput):
        llm.complete_text("proposal", system="s", messages=[{"role": "user", "content": "x"}],
                          max_tokens=300, settings=_s(openai_proposal_model=MODEL))
    assert logged[0]["output_tokens"] == 300 and logged[0]["cost_usd"] > 0


def test_complete_parts_floors(monkeypatch, logged):
    _install(monkeypatch, FakeOpenAI(_resp(usage=_usage())))
    with pytest.raises(llm.LlmBadOutputPermanent, match="max_output_tokens"):
        llm.complete_parts("plans_sub", system="s", parts=[{"type": "input_text", "text": "Ledger"}],
                           max_tokens=800, settings=_s(plans_reader_enabled=True))
    (row,) = logged
    assert row["output_tokens"] == 800 and row["input_tokens"] > 0 and row["cost_usd"] > 0


def test_complete_turn_floors_the_max_tokens_stop(monkeypatch, logged):
    from app.services import llm_queue

    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    _install(monkeypatch, FakeOpenAI(_resp("{", usage=_usage())))
    req = llm_parts.ModelRequest(
        feature="plans_recount", system="sys",
        messages=(llm_parts.UserTurn((llm_parts.TextPart("hello"),)),), max_tokens=1200,
    )
    turn = llm.complete_turn("plans_recount", req, settings=_s(plans_reader_enabled=True))
    assert turn.stop_reason == "max_tokens"
    assert turn.usage.output_tokens == 1200 and turn.usage.input_tokens > 0
    assert turn.cost_usd is not None and turn.cost_usd > 0
    assert logged[0]["output_tokens"] == 1200 and logged[0]["cost_usd"] == turn.cost_usd


# ── Triage: one floor, no double count ──────────────────────────────────────


def test_the_triage_run_carries_the_floored_log_row_and_its_own_floor_stays_quiet(monkeypatch, logged, caplog):
    from app.services import assistant_triage_model as model

    price = llm_pricing.price_for(MODEL)
    _json_call(monkeypatch, _resp(usage=_usage()), max_tokens=1400)
    (row,) = logged
    usage = model._call_usage(MODEL, "hold-1")
    assert usage["output_tokens"] == row["output_tokens"] == 1400
    assert usage["input_tokens"] == row["input_tokens"] and usage["cost_usd"] == row["cost_usd"]
    caplog.clear()
    floored = model._floor_cut_off_usage(usage, price, est_in=99_999, max_out=1400)
    assert floored == usage                                   # nothing added on top
    assert "P8L-4" not in caplog.text
    assert isinstance(row["cost_usd"], Decimal)
