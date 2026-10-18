"""Per-model token pricing (services/llm_pricing).

Invariants pinned here:

  price_for   exact ids and dated snapshots match their row (longest prefix
              wins); sibling models (a non-date suffix), unknown models,
              self-hosted labels and empty ids have no row.
  cost_for    uncached input at the input rate, cached input at the cached
              rate, output (reasoning included) at the output rate; above
              272,000 input tokens the WHOLE request bills 2x input/cached
              and 1.5x output; results round to 6 decimals; None without a
              price row or usage; accepts LlmUsage, mappings and partial
              objects; cached is clamped to input.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import llm_pricing
from app.services.llm import LlmUsage


def test_astra_row_matches_the_published_prices():
    row = llm_pricing.price_for("gpt-6-astra")
    assert row is not None
    assert (row.input, row.cached_input, row.output) == (Decimal(10), Decimal(1), Decimal(50))
    assert row.long_context_threshold == 272_000


@pytest.mark.parametrize(
    "model",
    ["gpt-6-astra", "GPT-6-Astra", "gpt-6-astra-2026-09-03", " gpt-6-astra "],
)
def test_exact_ids_and_dated_snapshots_match(model):
    assert llm_pricing.price_for(model) is llm_pricing.PRICES["gpt-6-astra"]


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6-astra-mini",       # a sibling model, not a snapshot
        "gpt-6",                  # shorter than the row
        "gpt-5.4-mini",           # no official price recorded in the repo
        "claude-sonnet-5",
        "self-hosted:qwen-3.5-4b",
        "",
        None,
    ],
)
def test_models_without_a_row_price_as_none(model):
    assert llm_pricing.price_for(model) is None
    assert llm_pricing.cost_for(model, LlmUsage(1000, 0, 1000, 0)) is None


def test_longest_prefix_wins(monkeypatch):
    short = llm_pricing.ModelPrice(Decimal(1), Decimal(1), Decimal(1))
    long_ = llm_pricing.ModelPrice(Decimal(2), Decimal(2), Decimal(2))
    monkeypatch.setitem(llm_pricing.PRICES, "acme", short)
    monkeypatch.setitem(llm_pricing.PRICES, "acme-2", long_)
    assert llm_pricing.price_for("acme-2-20260101") is long_
    assert llm_pricing.price_for("acme-20260101") is short


def test_basic_cost_split_between_uncached_cached_and_output():
    # 100k input of which 40k cached, 10k output (2k of it reasoning).
    usage = LlmUsage(input_tokens=100_000, cached_tokens=40_000, output_tokens=10_000,
                     reasoning_tokens=2_000)
    # 60k x $10 + 40k x $1 + 10k x $50 per 1M = 0.60 + 0.04 + 0.50
    assert llm_pricing.cost_for("gpt-6-astra", usage) == Decimal("1.140000")


def test_reasoning_tokens_are_not_billed_twice():
    a = LlmUsage(input_tokens=1000, cached_tokens=0, output_tokens=500, reasoning_tokens=0)
    b = LlmUsage(input_tokens=1000, cached_tokens=0, output_tokens=500, reasoning_tokens=400)
    assert llm_pricing.cost_for("gpt-6-astra", a) == llm_pricing.cost_for("gpt-6-astra", b)


def test_exactly_at_the_long_context_line_bills_normally():
    usage = LlmUsage(input_tokens=272_000, cached_tokens=0, output_tokens=0)
    assert llm_pricing.cost_for("gpt-6-astra", usage) == Decimal("2.720000")


def test_over_the_line_bills_the_whole_request_at_the_long_rate():
    usage = LlmUsage(input_tokens=272_001, cached_tokens=72_001, output_tokens=10_000)
    # (200,000 x 10 x 2 + 72,001 x 1 x 2 + 10,000 x 50 x 1.5) / 1M
    expected = (
        Decimal(200_000) * 20 + Decimal(72_001) * 2 + Decimal(10_000) * 75
    ) / Decimal(1_000_000)
    assert llm_pricing.cost_for("gpt-6-astra", usage) == expected.quantize(Decimal("0.000001"))
    assert llm_pricing.cost_for("gpt-6-astra", usage) == Decimal("4.894002")


def test_rounds_to_six_decimals():
    usage = LlmUsage(input_tokens=1, cached_tokens=0, output_tokens=0)
    assert llm_pricing.cost_for("gpt-6-astra", usage) == Decimal("0.000010")
    usage = LlmUsage(input_tokens=0, cached_tokens=0, output_tokens=0)
    assert llm_pricing.cost_for("gpt-6-astra", usage) == Decimal("0.000000")


def test_accepts_mappings_partial_objects_and_clamps_cached():
    assert llm_pricing.cost_for(
        "gpt-6-astra", {"input_tokens": 100_000, "output_tokens": 0}
    ) == Decimal("1.000000")
    assert llm_pricing.cost_for(
        "gpt-6-astra", SimpleNamespace(input_tokens=100_000)
    ) == Decimal("1.000000")
    # A cached count above the input count cannot produce a negative uncached part.
    assert llm_pricing.cost_for(
        "gpt-6-astra", {"input_tokens": 100_000, "cached_tokens": 5_000_000}
    ) == Decimal("0.100000")
    # Garbage counts read as zero rather than raising.
    assert llm_pricing.cost_for(
        "gpt-6-astra", {"input_tokens": "lots", "output_tokens": None}
    ) == Decimal("0.000000")


def test_no_usage_is_none():
    assert llm_pricing.cost_for("gpt-6-astra", None) is None


def test_estimate_cost_takes_a_model_id_or_a_row():
    row = llm_pricing.PRICES["gpt-6-astra"]
    by_id = llm_pricing.estimate_cost("gpt-6-astra", input_tokens=240_000, output_tokens=8_000)
    by_row = llm_pricing.estimate_cost(row, input_tokens=240_000, output_tokens=8_000)
    assert by_id == by_row == Decimal("2.800000")
    assert llm_pricing.estimate_cost("unknown-model", input_tokens=10) is None
