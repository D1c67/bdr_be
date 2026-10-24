"""Verified price rows and cache-write billing (services/llm_pricing).

Pinned here (prices checked 2026-10-21 on the provider pricing pages):
  OpenAI      gpt-6-astra 10 / 1 / 50 (2x, 1.5x above 272k); gpt-5.4
              2.5 / 0.25 / 15 with the same long-context tier;
              gpt-5.4-mini 0.75 / 0.075 / 4.5; gpt-5.4-nano 0.2 / 0.02 /
              1.25 (no long-context tier).
  Anthropic   claude-opus-5-5 4 / 0.20 / 20; claude-sonnet-5-5 2 / 0.10 /
              10; claude-opus-5 5 / 0.50 / 25; cache writes 1.25x (5m) and
              2x (1h) the input price; no long-context tier.
  matching    dated snapshots (YYYYMMDD or YYYY-MM-DD) match; siblings do
              not (claude-opus-5-6 is not opus-5); the app's three gpt-5.4
              models now price (they logged NULL before).
  cost        cache writes bill at the multiplier and never also as
              uncached input; LlmUsage carries the write counts; the
              conservative fallback prices an unpriced model at the
              provider's most expensive row and says so.
Synthetic token counts only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.services import llm_pricing
from app.services.llm import LlmUsage

D = Decimal


@pytest.mark.parametrize(
    "model,prices,threshold,provider",
    [
        ("gpt-6-astra", ("10", "1", "50"), 272_000, "openai"),
        ("gpt-5.4", ("2.5", "0.25", "15"), 272_000, "openai"),
        ("gpt-5.4-mini", ("0.75", "0.075", "4.5"), None, "openai"),
        ("gpt-5.4-nano", ("0.2", "0.02", "1.25"), None, "openai"),
        ("claude-opus-5-5", ("4", "0.2", "20"), None, "anthropic"),
        ("claude-sonnet-5-5", ("2", "0.1", "10"), None, "anthropic"),
        ("claude-opus-5", ("5", "0.5", "25"), None, "anthropic"),
    ],
)
def test_rows_match_the_pricing_pages(model, prices, threshold, provider):
    row = llm_pricing.price_for(model)
    assert row is not None
    assert (row.input, row.cached_input, row.output) == tuple(D(p) for p in prices)
    assert row.long_context_threshold == threshold
    assert row.provider == provider
    if provider == "anthropic":
        assert (row.cache_write_5m_multiplier, row.cache_write_1h_multiplier) == (D("1.25"), D(2))
    else:
        assert (row.cache_write_5m_multiplier, row.cache_write_1h_multiplier) == (D(1), D(1))
    if threshold:
        assert (row.long_input_multiplier, row.long_output_multiplier) == (D(2), D("1.5"))


def test_every_model_the_app_configures_on_openai_has_a_row():
    s = Settings(_env_file=None)
    for model in (
        s.openai_plans_model, s.openai_bid_split_model, s.openai_email_model, s.openai_quote_model,
        s.openai_alias_model, s.openai_rfp_classify_model, s.openai_proposal_model, s.openai_email_match_model,
    ):
        assert llm_pricing.price_for(model) is not None, model
    assert llm_pricing.price_for(s.claude_bid_split_model) is not None  # the splitter model


@pytest.mark.parametrize(
    "model,expect",
    [
        ("claude-opus-5-5-20260401", "claude-opus-5-5"),
        ("claude-opus-5-20260101", "claude-opus-5"),
        ("gpt-5.4-2026-03-05", "gpt-5.4"),
        ("GPT-5.4-Mini", "gpt-5.4-mini"),
        ("claude-opus-5-6", None),
        ("claude-opus-5-5-mini", None),
        ("gpt-5.4-nano-x", None),
        ("gpt-5.4-20", None),
        ("claude-haiku-5-5", None),
    ],
)
def test_snapshots_match_and_siblings_do_not(model, expect):
    row = llm_pricing.price_for(model)
    if expect is None:
        assert row is None
    else:
        assert row is llm_pricing.PRICES[expect]


def test_anthropic_cache_writes_bill_at_the_multiplier():
    usage = LlmUsage(input_tokens=2000, cached_tokens=500, output_tokens=100, cache_write_5m_tokens=400, cache_write_1h_tokens=100)
    # uncached 1000 x 4 + 500 x 0.2 + 400 x 4 x 1.25 + 100 x 4 x 2 + 100 x 20
    # = 4000 + 100 + 2000 + 800 + 2000 = 8900 / 1e6
    assert llm_pricing.cost_for("claude-opus-5-5", usage) == D("0.008900")
    assert llm_pricing.cost_for("claude-sonnet-5-5", usage) == D("0.004450")
    # OpenAI rows ignore the multipliers (neutral 1) but never bill writes twice.
    plain = LlmUsage(input_tokens=2000, cached_tokens=500, output_tokens=100)
    assert llm_pricing.cost_for("gpt-6-astra", usage) == llm_pricing.cost_for("gpt-6-astra", plain)


def test_write_counts_are_clamped_inside_input():
    assert llm_pricing.estimate_cost(
        "claude-opus-5-5", input_tokens=1000, cached_tokens=800, cache_write_5m_tokens=500, cache_write_1h_tokens=500
    ) == D("0.001160")  # 800 x 0.2 + 200 x 4 x 1.25 = 160 + 1000


def test_llm_usage_carries_cache_write_fields_with_defaults():
    u = LlmUsage(10, 2, 3, 1)
    assert (u.cache_write_5m_tokens, u.cache_write_1h_tokens, u.cache_write_tokens) == (0, 0, 0)
    assert u.cache_ratio == 0.2
    assert LlmUsage().cache_ratio == 0.0


def test_output_price_per_token_applies_the_long_context_multiplier():
    assert llm_pricing.output_price_per_token("gpt-6-astra") == D(50) / D(1_000_000)
    assert llm_pricing.output_price_per_token("gpt-6-astra", input_tokens=300_000) == D(75) / D(1_000_000)
    assert llm_pricing.output_price_per_token("gpt-5.3-mini") is None


def test_conservative_fallback_prices_at_the_most_expensive_row():
    usage = LlmUsage(input_tokens=1000, output_tokens=100)
    cost, conservative = llm_pricing.cost_or_conservative("claude-haiku-5-5", usage, "anthropic")
    assert conservative is True
    assert cost == llm_pricing.cost_for("claude-opus-5", usage)
    cost, conservative = llm_pricing.cost_or_conservative("gpt-5.3-mini", usage, "openai")
    assert conservative is True and cost == llm_pricing.cost_for("gpt-6-astra", usage)
    cost, conservative = llm_pricing.cost_or_conservative("gpt-5.4-nano", usage, "openai")
    assert conservative is False and cost == llm_pricing.cost_for("gpt-5.4-nano", usage)
    assert llm_pricing.cost_or_conservative("x", usage, "self_hosted") == (None, False)
    assert llm_pricing.cost_or_conservative("x", None, "openai") == (None, False)
    assert llm_pricing.most_expensive("openai") is llm_pricing.PRICES["gpt-6-astra"]
