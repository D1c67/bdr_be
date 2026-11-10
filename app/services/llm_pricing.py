"""Per-model token prices and the cost of one LLM call.

Every call through services/llm.py writes its provider-reported token usage
to llm_call_log (0152), and this module turns that usage into dollars so the
AI Monitor and the AI Estimating spend cap (services/ai_spend) can show and
enforce real spend instead of guesses.

Only prices verified against the provider's own pricing page live here. A
model without a row prices as None (the cost columns stay NULL) rather than
a made-up number: a wrong price would silently skew the daily cap, a NULL is
visibly "unknown". Rows and their check dates:

- OpenAI (developers.openai.com/api/docs/pricing, checked 2026-10-21):
  gpt-6-astra, gpt-5.4, gpt-5.4-mini, gpt-5.4-nano. The three gpt-5.4 rows
  were added by the AI Estimating build (docs/AI_ESTIMATING_BUILD_SPEC.md
  1.3 forbids a live model without a verified row; before 2026-10-21 their
  calls logged a NULL cost).
- OpenAI (developers.openai.com/api/docs/pricing, checked 2026-11-02):
  gpt-6-luna (0.10 input, 0.01 cached input, 0.50 output per 1M tokens),
  added by Phase 8 triage (docs/ASSISTANT_PHASE8_PLAN.md 5.4). Re-check
  the page on the day the model is first used live.
- Anthropic (platform.claude.com/docs/en/about-claude/pricing, checked
  2026-10-21): claude-opus-5-5, claude-sonnet-5-5, claude-opus-5 (the
  splitter model). Cache writes bill at 1.25x the input price for the
  5-minute TTL and 2x for the 1-hour TTL; cache reads at the row's
  cached_input (0.05x the input price on Opus 5.5 and Sonnet 5.5, 0.1x on
  Opus 5). No long-context tier: the full 1M window bills at the standard
  rate. Haiku rows are deferred.
  Sonnet 5.5 cache read, re-verified for the spec's "re-verify on build
  day" rule: https://platform.claude.com/docs/en/about-claude/pricing read
  on 2026-10-21 lists "Claude Sonnet 5.5 | $2 / MTok | $2.50 / MTok |
  $4 / MTok | $0.10 / MTok | $10 / MTok" (cache hits at 0.05x the base
  input price). The spec's draft $0.20 (and the bundled claude-api
  reference) is the older figure; the row uses the page's $0.10.

- OpenAI hosted web search (developers.openai.com/api/docs/pricing, read
  2026-11-02: "$10.00 / 1k calls + Search content tokens billed at model
  rates" for reasoning models): WEB_SEARCH_CALL_PRICE, USD 0.01 per
  web_search_call item. The search content tokens are already inside the
  call's reported input tokens. Every web_search_call item counts, whether
  its action is a search, an open_page or a find_in_page (the conservative
  reading). It is not a per-model row: cost_for adds it when the usage
  carries a `web_search_calls` count (llm_messages.WebSearchUsage).

Every self-hosted model has no row: self-hosted calls have no per-token
price at all.

Billing rules encoded per row:

- `input` is charged on the uncached part of the prompt, `cached_input` on
  the cached part. OpenAI reports usage.input_tokens INCLUDING the cached
  tokens, so uncached = input_tokens - cached_tokens.
- `output` is charged on usage.output_tokens, which already includes the
  reasoning tokens (reasoning is billed as output, never twice).
- Anthropic cache writes: `cache_write_5m_tokens` and `cache_write_1h_tokens`
  are part of input_tokens (the LlmUsage convention keeps input inclusive)
  and bill at input x the row's write multiplier; they are never also
  billed as uncached input.
- Long context: when a request's input_tokens exceed the row's threshold
  (272,000 for gpt-6-astra, gpt-6-luna and gpt-5.4) the WHOLE request bills at the
  long-context multipliers (2x input and cached input, 1.5x output), not
  just the tokens over the line. The plans reader packs groups under
  240,000 tokens for exactly this reason.

Model matching: exact id, or a dated snapshot of it (`gpt-6-astra-2026-09-03`
or `claude-opus-5-5-20260401` match their row). The longest matching prefix
wins. A suffix that is not a date (`gpt-6-astra-mini`, `claude-opus-5-6`)
does NOT match, because a sibling model is a different price, and pricing
it as its parent would be silently wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

_PER_MILLION = Decimal(1_000_000)
# llm_call_log.cost_usd and plans_reader_calls.cost_usd are numeric(12, 6).
_QUANT = Decimal("0.000001")
# A dated snapshot suffix: YYYYMMDD or YYYY-MM-DD (nothing else).
_DATE_SUFFIX_RE = re.compile(r"^(\d{8}|\d{4}-\d{2}-\d{2})$")

PROVIDERS = ("openai", "anthropic")

# OpenAI hosted web search: USD 10 per 1,000 tool calls (see the module
# docstring for the check date). Billed per web_search_call item.
WEB_SEARCH_CALL_PRICE = Decimal("0.01")


def web_search_cost(calls: int) -> Decimal:
    """The tool-call fee of `calls` hosted web_search_call items."""
    try:
        count = max(0, int(calls or 0))
    except (TypeError, ValueError):
        count = 0
    return (WEB_SEARCH_CALL_PRICE * count).quantize(_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens, plus the optional long-context rule."""

    input: Decimal
    cached_input: Decimal
    output: Decimal
    # input_tokens strictly above this bill the whole request at the
    # multipliers below. None = no long-context tier.
    long_context_threshold: int | None = None
    long_input_multiplier: Decimal = Decimal(1)
    long_output_multiplier: Decimal = Decimal(1)
    # Prompt-cache WRITE tokens bill at input x these (Anthropic: 1.25x for
    # the 5-minute TTL, 2x for 1 hour). OpenAI reports no write tokens, so
    # its rows keep the neutral 1.
    cache_write_5m_multiplier: Decimal = Decimal(1)
    cache_write_1h_multiplier: Decimal = Decimal(1)
    provider: str = "openai"


# Checked 2026-10-21 against developers.openai.com/api/docs/pricing (OpenAI)
# and platform.claude.com/docs/en/about-claude/pricing (Anthropic). USD per
# 1M tokens. Re-check on the day a model is first used live (spec 0.4 rule 3).
PRICES: dict[str, ModelPrice] = {
    "gpt-6-astra": ModelPrice(
        input=Decimal("10"),
        cached_input=Decimal("1"),
        output=Decimal("50"),
        long_context_threshold=272_000,
        long_input_multiplier=Decimal(2),
        long_output_multiplier=Decimal("1.5"),
    ),
    # Checked 2026-11-02 against developers.openai.com/api/docs/pricing; the
    # Phase 8 triage model. Prompts above 272K input tokens bill the whole
    # request at 2x input and cached input and 1.5x output (a triage call
    # stays under 6,000 input tokens, so the tier never applies in practice).
    "gpt-6-luna": ModelPrice(
        input=Decimal("0.1"),
        cached_input=Decimal("0.01"),
        output=Decimal("0.5"),
        long_context_threshold=272_000,
        long_input_multiplier=Decimal(2),
        long_output_multiplier=Decimal("1.5"),
    ),
    "gpt-5.4": ModelPrice(
        input=Decimal("2.5"),
        cached_input=Decimal("0.25"),
        output=Decimal("15"),
        long_context_threshold=272_000,
        long_input_multiplier=Decimal(2),
        long_output_multiplier=Decimal("1.5"),
    ),
    "gpt-5.4-mini": ModelPrice(
        input=Decimal("0.75"),
        cached_input=Decimal("0.075"),
        output=Decimal("4.5"),
    ),
    "gpt-5.4-nano": ModelPrice(
        input=Decimal("0.2"),
        cached_input=Decimal("0.02"),
        output=Decimal("1.25"),
    ),
    "claude-opus-5-5": ModelPrice(
        input=Decimal("4"),
        cached_input=Decimal("0.2"),
        output=Decimal("20"),
        cache_write_5m_multiplier=Decimal("1.25"),
        cache_write_1h_multiplier=Decimal(2),
        provider="anthropic",
    ),
    "claude-sonnet-5-5": ModelPrice(
        input=Decimal("2"),
        # $0.10 per MTok: platform.claude.com/docs/en/about-claude/pricing,
        # read 2026-10-21 (0.05x the $2 input price).
        cached_input=Decimal("0.1"),
        output=Decimal("10"),
        cache_write_5m_multiplier=Decimal("1.25"),
        cache_write_1h_multiplier=Decimal(2),
        provider="anthropic",
    ),
    "claude-opus-5": ModelPrice(
        input=Decimal("5"),
        cached_input=Decimal("0.5"),
        output=Decimal("25"),
        cache_write_5m_multiplier=Decimal("1.25"),
        cache_write_1h_multiplier=Decimal(2),
        provider="anthropic",
    ),
}


def price_for(model: str | None) -> ModelPrice | None:
    """The price row for `model` (exact id or a dated snapshot), or None."""
    m = (model or "").strip().lower()
    if not m:
        return None
    best: str | None = None
    for prefix in PRICES:
        if m == prefix or (
            m.startswith(prefix + "-") and _DATE_SUFFIX_RE.match(m[len(prefix) + 1 :])
        ):
            if best is None or len(prefix) > len(best):
                best = prefix
    return PRICES[best] if best is not None else None


def most_expensive(provider: str) -> ModelPrice | None:
    """The priciest row of a provider (by output price): the conservative
    stand-in for a billed call on an unpriced model of that provider. Used
    only where plans_reader_calls rows are written, never by cost_for."""
    rows = [row for row in PRICES.values() if row.provider == provider]
    if not rows:
        return None
    return max(rows, key=lambda row: (row.output, row.input))


def cost_or_conservative(model: str | None, usage: Any, provider: str) -> tuple[Decimal | None, bool]:
    """(cost, conservative): the real cost when the model has a row, else
    the cost at the provider's most expensive row with conservative=True,
    else (None, False) when the provider has no rows or there is no usage.
    A billed call must never be recorded as $0 (spec WS2 6.3)."""
    if usage is None:
        return None, False
    cost = cost_for(model, usage)
    if cost is not None:
        return cost, False
    row = most_expensive(provider)
    if row is None:
        return None, False
    return (
        estimate_cost(
            row,
            input_tokens=_field(usage, "input_tokens"),
            cached_tokens=_field(usage, "cached_tokens"),
            output_tokens=_field(usage, "output_tokens"),
            cache_write_5m_tokens=_field(usage, "cache_write_5m_tokens"),
            cache_write_1h_tokens=_field(usage, "cache_write_1h_tokens"),
        ),
        True,
    )


def _field(usage: Any, name: str) -> int:
    """Read one token count from an LlmUsage-like object or a mapping."""
    if isinstance(usage, Mapping):
        value = usage.get(name)
    else:
        value = getattr(usage, name, None)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def cost_for(model: str | None, usage: Any) -> Decimal | None:
    """Dollar cost of one call, rounded to 6 decimals, or None when the model
    has no price row or no usage was reported.

    `usage` is a services.llm.LlmUsage, or anything with the same attributes
    or keys: input_tokens (including cached and cache writes), cached_tokens,
    output_tokens (including reasoning), and the optional Anthropic
    cache_write_5m_tokens / cache_write_1h_tokens. reasoning_tokens is
    accepted and ignored for pricing because it is already inside
    output_tokens. A usage with `web_search_calls` (hosted web search) adds
    WEB_SEARCH_CALL_PRICE per call on top of the token cost.
    """
    if usage is None:
        return None
    price = price_for(model)
    if price is None:
        return None
    cost = estimate_cost(
        price,
        input_tokens=_field(usage, "input_tokens"),
        cached_tokens=_field(usage, "cached_tokens"),
        output_tokens=_field(usage, "output_tokens"),
        cache_write_5m_tokens=_field(usage, "cache_write_5m_tokens"),
        cache_write_1h_tokens=_field(usage, "cache_write_1h_tokens"),
    )
    searches = _field(usage, "web_search_calls")
    if cost is not None and searches:
        cost = (cost + web_search_cost(searches)).quantize(_QUANT, rounding=ROUND_HALF_UP)
    return cost


def estimate_cost(
    price: ModelPrice | str,
    *,
    input_tokens: int,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> Decimal | None:
    """Cost of a planned or finished request from raw counts (used by the
    run cost preview as well as cost_for). `price` is a row or a model id;
    an unknown model id returns None. The cached and cache-write counts are
    parts of input_tokens and are clamped so they never exceed it."""
    row = price_for(price) if isinstance(price, str) else price
    if row is None:
        return None
    input_tokens = max(0, int(input_tokens))
    cached_tokens = min(max(0, int(cached_tokens)), input_tokens)
    write_5m = min(max(0, int(cache_write_5m_tokens)), input_tokens - cached_tokens)
    write_1h = min(max(0, int(cache_write_1h_tokens)), input_tokens - cached_tokens - write_5m)
    output_tokens = max(0, int(output_tokens))
    in_mult = out_mult = Decimal(1)
    if row.long_context_threshold is not None and input_tokens > row.long_context_threshold:
        in_mult = row.long_input_multiplier
        out_mult = row.long_output_multiplier
    uncached = input_tokens - cached_tokens - write_5m - write_1h
    total = (
        Decimal(uncached) * row.input * in_mult
        + Decimal(cached_tokens) * row.cached_input * in_mult
        + Decimal(write_5m) * row.input * row.cache_write_5m_multiplier * in_mult
        + Decimal(write_1h) * row.input * row.cache_write_1h_multiplier * in_mult
        + Decimal(output_tokens) * row.output * out_mult
    ) / _PER_MILLION
    return total.quantize(_QUANT, rounding=ROUND_HALF_UP)


def output_price_per_token(price: ModelPrice | str, *, input_tokens: int = 0) -> Decimal | None:
    """Dollars per OUTPUT token for a request of `input_tokens` (the
    long-context multiplier applies above the row's threshold). The budget
    rules (services/plans_reader_budgets) size a round's max_tokens from it.
    None for an unpriced model."""
    row = price_for(price) if isinstance(price, str) else price
    if row is None:
        return None
    mult = Decimal(1)
    if row.long_context_threshold is not None and int(input_tokens) > row.long_context_threshold:
        mult = row.long_output_multiplier
    return row.output * mult / _PER_MILLION
