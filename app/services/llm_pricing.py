"""Per-model token prices and the cost of one LLM call.

Every call through services/llm.py writes its provider-reported token usage
to llm_call_log (0152), and this module turns that usage into dollars so the
AI Monitor and the AI Estimating spend cap (services/ai_spend) can show and
enforce real spend instead of guesses.

Only prices verified against the provider's own pricing page live here. A
model without a row prices as None (the cost columns stay NULL) rather than
a made-up number: a wrong price would silently skew the daily cap, a NULL is
visibly "unknown". Today that means only gpt-6-astra (docs/PLANS_READER.md
section 1, checked 2026-10-17). The gpt-5.4 mini/nano models the older
features use, the Claude models and every self-hosted model have no row:
no official price is recorded in the repo, and self-hosted calls have no
per-token price at all.

Billing rules encoded per row:

- `input` is charged on the uncached part of the prompt, `cached_input` on
  the cached part. OpenAI reports usage.input_tokens INCLUDING the cached
  tokens, so uncached = input_tokens - cached_tokens.
- `output` is charged on usage.output_tokens, which already includes the
  reasoning tokens (reasoning is billed as output, never twice).
- Long context: when a request's input_tokens exceed the row's threshold
  (272,000 for gpt-6-astra) the WHOLE request bills at the long-context
  multipliers (2x input and cached input, 1.5x output), not just the tokens
  over the line. The plans reader packs groups under 240,000 tokens for
  exactly this reason.

Model matching: exact id, or a dated snapshot of it (`gpt-6-astra-2026-09-03`
matches `gpt-6-astra`). The longest matching prefix wins. A suffix that is
not a date (`gpt-6-astra-mini`) does NOT match, because a sibling model is a
different price, and pricing it as its parent would be silently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

_PER_MILLION = Decimal(1_000_000)
# llm_call_log.cost_usd and plans_reader_calls.cost_usd are numeric(12, 6).
_QUANT = Decimal("0.000001")


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


PRICES: dict[str, ModelPrice] = {
    "gpt-6-astra": ModelPrice(
        input=Decimal("10"),
        cached_input=Decimal("1"),
        output=Decimal("50"),
        long_context_threshold=272_000,
        long_input_multiplier=Decimal(2),
        long_output_multiplier=Decimal("1.5"),
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
            m.startswith(prefix + "-") and m[len(prefix) + 1 : len(prefix) + 2].isdigit()
        ):
            if best is None or len(prefix) > len(best):
                best = prefix
    return PRICES[best] if best is not None else None


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
    or keys: input_tokens (including cached), cached_tokens, output_tokens
    (including reasoning). reasoning_tokens is accepted and ignored for
    pricing because it is already inside output_tokens.
    """
    if usage is None:
        return None
    price = price_for(model)
    if price is None:
        return None
    input_tokens = _field(usage, "input_tokens")
    cached = min(_field(usage, "cached_tokens"), input_tokens)
    output_tokens = _field(usage, "output_tokens")
    return estimate_cost(
        price, input_tokens=input_tokens, cached_tokens=cached, output_tokens=output_tokens
    )


def estimate_cost(
    price: ModelPrice | str,
    *,
    input_tokens: int,
    output_tokens: int = 0,
    cached_tokens: int = 0,
) -> Decimal | None:
    """Cost of a planned or finished request from raw counts (used by the
    run cost preview as well as cost_for). `price` is a row or a model id;
    an unknown model id returns None."""
    row = price_for(price) if isinstance(price, str) else price
    if row is None:
        return None
    input_tokens = max(0, int(input_tokens))
    cached_tokens = min(max(0, int(cached_tokens)), input_tokens)
    output_tokens = max(0, int(output_tokens))
    in_mult = out_mult = Decimal(1)
    if row.long_context_threshold is not None and input_tokens > row.long_context_threshold:
        in_mult = row.long_input_multiplier
        out_mult = row.long_output_multiplier
    uncached = input_tokens - cached_tokens
    total = (
        Decimal(uncached) * row.input * in_mult
        + Decimal(cached_tokens) * row.cached_input * in_mult
        + Decimal(output_tokens) * row.output * out_mult
    ) / _PER_MILLION
    return total.quantize(_QUANT, rounding=ROUND_HALF_UP)
