"""Dollar and round budgets of the AI Estimating tool loop (WS2 step 6).

Pure arithmetic over the price rows of services/llm_pricing: no settings
reads beyond the optional caps passed in, no database, no network. The loop
(services/plans_reader_toolloop, wave 2) keeps one LoopState per group or
chat message and asks this module, before every round, how big the round
may be and whether it must be the final one; after every round it records
the turn and learns the decision (continue, forced final, failed).

Rules (spec section 6 WS2, fixed per critique):

- Per-round output cap: `max_tokens = clamp((max_usd - spent -
  reserved_final - input_cost) / output_price)`, and the round's estimate
  uses that cap, so a tool round can never spend what the final answer
  needs.
- A `max_tokens` stop on a tool round becomes a forced final round with
  the remaining budget, never a permanent failure. On the final round it
  is a failure (the answer was cut off).
- The cache gate applies from round 3 using round 2's OBSERVED cache ratio
  (round 2 itself is predicted as prefix / (prefix + new) and allowed, so a
  round 1 with 0 cached tokens never ends a loop on its own).
- Kind budgets: recount 6 rounds, 16 tool calls, 8 images, $1.50 (raised from
  $0.80 after the wave 4 L2 A/B: a six-tile sheet on gpt-6-astra left no room
  for one zoom round under $0.80); main
  20 rounds, $6.00 (tool calls from PLANS_READER_CONSOLIDATOR_MAX_TOOL_CALLS);
  review 10 rounds, 10 tool calls (spec WS7: read-only tools, at most 10
  calls), $1.50; revise ONE round, no tools, $1.00 (spec WS7: one bounded
  turn). A route budget or a settings cap only ever lowers a budget.
- A stray tool call on a planned final round is answered with an error
  result and gets max_repair_rounds (1) extra final round; a second one
  fails the group, and plan_round refuses any round past max_rounds plus
  max_repair_rounds.
- Budgets are keyed on group_id (pipeline groups) or message_id (chat).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services import llm_pricing

_QUANT = Decimal("0.000001")
_ZERO = Decimal("0")

KINDS = ("recount", "main", "review", "revise")
PROVIDERS = ("openai", "anthropic")

# Output ceilings per round (tokens). The final answer of a consolidator is
# a whole BOQ; a tool round is a handful of calls.
TOOL_ROUND_OUTPUT_TOKENS = 2_500
FINAL_OUTPUT_TOKENS = {"recount": 6_000, "main": 24_000, "review": 8_000, "revise": 6_000}
MAX_TOKENS_CEILING = 32_000
# Below this many output tokens a round is not worth sending.
MIN_ROUND_OUTPUT_TOKENS = 400
# OpenAI keeps a round under the 272k long-context line; Anthropic's 1M
# window bills flat, so the loop only guards its own planner limits.
MAX_ROUND_INPUT_TOKENS = {"openai": 240_000, "anthropic": 600_000}
# Anthropic: explicit breakpoints make the prefix a cache read from round 2.
ANTHROPIC_PREFIX_CACHE_SHARE = 0.9
# OpenAI: before any observation half of the prefix is assumed cached.
OPENAI_DEFAULT_CACHE_RATIO = 0.5
# Spec: "no group over budget by more than 15%" (the loop's own tolerance;
# the ledger stops a sweep at the same figure).
OVER_BUDGET_TOLERANCE = 0.15


@dataclass(frozen=True)
class LoopBudget:
    kind: str
    max_rounds: int
    max_tool_calls: int
    max_calls_per_round: int
    max_images: int
    max_usd: Decimal
    min_cache_ratio: float | None = None
    final_output_tokens: int = 6_000
    tool_round_output_tokens: int = TOOL_ROUND_OUTPUT_TOKENS
    max_tokens_ceiling: int = MAX_TOKENS_CEILING
    max_repair_rounds: int = 1


KIND_BUDGETS: dict[str, LoopBudget] = {
    "recount": LoopBudget(
        kind="recount", max_rounds=6, max_tool_calls=16, max_calls_per_round=6, max_images=8,
        max_usd=Decimal("1.50"), min_cache_ratio=None, final_output_tokens=FINAL_OUTPUT_TOKENS["recount"],
    ),
    "main": LoopBudget(
        kind="main", max_rounds=20, max_tool_calls=400, max_calls_per_round=10, max_images=0,
        max_usd=Decimal("6.00"), min_cache_ratio=0.5, final_output_tokens=FINAL_OUTPUT_TOKENS["main"],
    ),
    # Spec WS7: the reviewer gets read-only tools, at most 10 tool calls
    # and $1.50; the revise turn is ONE bounded round with no tools, $1.00.
    "review": LoopBudget(
        kind="review", max_rounds=10, max_tool_calls=10, max_calls_per_round=8, max_images=12,
        max_usd=Decimal("1.50"), min_cache_ratio=None, final_output_tokens=FINAL_OUTPUT_TOKENS["review"],
    ),
    "revise": LoopBudget(
        kind="revise", max_rounds=1, max_tool_calls=0, max_calls_per_round=6, max_images=0,
        max_usd=Decimal("1.00"), min_cache_ratio=None, final_output_tokens=FINAL_OUTPUT_TOKENS["revise"],
    ),
}


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except Exception:  # noqa: BLE001 - a junk cap never lowers a budget to garbage
        return None
    return out if out.is_finite() and out > 0 else None


def budget_for(
    kind: str,
    *,
    settings: Any = None,
    route: Any = None,
) -> LoopBudget:
    """The LoopBudget of a kind, lowered (never raised) by the step route's
    budget_usd and by the settings caps: main reads
    plans_reader_consolidator_max_rounds / _max_tool_calls / _max_usd,
    recount reads plans_reader_recount_budget_usd, review reads
    plans_reader_review_budget_usd. `route` is a StepRoute or its dict."""
    if kind not in KIND_BUDGETS:
        raise ValueError(f"No loop budget for kind {kind!r} (kinds: {', '.join(KINDS)}).")
    base = KIND_BUDGETS[kind]
    max_rounds, max_calls, max_usd = base.max_rounds, base.max_tool_calls, base.max_usd
    if settings is not None:
        if kind == "main":
            max_rounds = min(max_rounds, int(getattr(settings, "plans_reader_consolidator_max_rounds", max_rounds)))
            max_calls = min(max_calls, int(getattr(settings, "plans_reader_consolidator_max_tool_calls", max_calls)))
            cap = _dec(getattr(settings, "plans_reader_consolidator_max_usd", None))
        elif kind == "recount":
            cap = _dec(getattr(settings, "plans_reader_recount_budget_usd", None))
        elif kind == "review":
            cap = _dec(getattr(settings, "plans_reader_review_budget_usd", None))
        else:
            cap = None
        if cap is not None:
            max_usd = min(max_usd, cap)
    if route is not None:
        raw = route.get("budget_usd") if isinstance(route, dict) else getattr(route, "budget_usd", None)
        cap = _dec(raw)
        if cap is not None:
            max_usd = min(max_usd, cap)
    return LoopBudget(
        kind=base.kind,
        max_rounds=max(1, max_rounds),
        max_tool_calls=max(0, max_calls),
        max_calls_per_round=base.max_calls_per_round,
        max_images=base.max_images,
        max_usd=max_usd,
        min_cache_ratio=base.min_cache_ratio,
        final_output_tokens=base.final_output_tokens,
        tool_round_output_tokens=base.tool_round_output_tokens,
        max_tokens_ceiling=base.max_tokens_ceiling,
        max_repair_rounds=base.max_repair_rounds,
    )


def budget_key(*, group_id: str | None = None, message_id: str | None = None) -> str:
    """The key a budget is tracked under: a pipeline group or a chat
    message, never both and never neither."""
    if bool(group_id) == bool(message_id):
        raise ValueError("A loop budget is keyed on exactly one of group_id or message_id.")
    return f"group:{group_id}" if group_id else f"message:{message_id}"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundRecord:
    round_no: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: Decimal
    stop_reason: str  # final | tool_use | max_tokens
    final: bool  # the round was planned as the final one

    @property
    def cache_ratio(self) -> float:
        return (self.cached_tokens / self.input_tokens) if self.input_tokens > 0 else 0.0


@dataclass
class LoopState:
    """Per-budget-key memory of the loop. `prefix_tokens` is the planned
    input of the first round (system, ledger, first user turn);
    `price` the model's row or id."""

    budget: LoopBudget
    price: llm_pricing.ModelPrice | str
    provider: str = "openai"
    prefix_tokens: int = 0
    rounds: list[RoundRecord] = field(default_factory=list)
    force_final: bool = False
    tool_calls: int = 0
    images: int = 0
    repairs: int = 0
    key: str = ""

    @property
    def spent(self) -> Decimal:
        return sum((r.cost_usd for r in self.rounds), _ZERO)

    @property
    def next_round_no(self) -> int:
        return len(self.rounds) + 1

    @property
    def last(self) -> RoundRecord | None:
        return self.rounds[-1] if self.rounds else None

    @property
    def observed_cache_ratio(self) -> float | None:
        """Round 2's observed ratio, the figure the cache gate reads."""
        if len(self.rounds) >= 2:
            return self.rounds[1].cache_ratio
        return None

    @property
    def latest_cache_ratio(self) -> float | None:
        """The last done round's observed ratio (from round 2 on), the
        figure cost predictions use so a provider that delivers less cache
        than expected is learned after one round, never compounded."""
        if len(self.rounds) >= 2:
            return self.rounds[-1].cache_ratio
        return None

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.budget.max_usd - self.spent, _ZERO)


@dataclass(frozen=True)
class RoundPlan:
    round_no: int
    final: bool
    max_tokens: int  # 0 = the round cannot be afforded
    est_usd: Decimal
    input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    reserved_final_usd: Decimal
    reason: str  # continue | max_rounds | forced | budget | cache_gate | input_limit

    @property
    def affordable(self) -> bool:
        return self.max_tokens > 0


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def _row(price: llm_pricing.ModelPrice | str) -> llm_pricing.ModelPrice:
    row = llm_pricing.price_for(price) if isinstance(price, str) else price
    if row is None:
        raise ValueError(f"No verified price row for model {price!r}; the loop refuses to run unpriced.")
    return row


def estimate_round(
    price: llm_pricing.ModelPrice | str,
    *,
    input_tokens: int,
    cached_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Dollar estimate of one round (cache writes priced at the 5-minute
    multiplier)."""
    cost = llm_pricing.estimate_cost(
        _row(price),
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        cache_write_5m_tokens=cache_write_tokens,
    )
    return cost if cost is not None else _ZERO


def round_max_tokens(
    *,
    price: llm_pricing.ModelPrice | str,
    max_usd: Decimal,
    spent: Decimal,
    reserved_final: Decimal,
    input_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    floor: int = MIN_ROUND_OUTPUT_TOKENS,
    ceiling: int = MAX_TOKENS_CEILING,
) -> int:
    """clamp((max_usd - spent - reserved_final - input_cost) / output_price)
    to [floor, ceiling]; 0 when even `floor` output tokens are not
    affordable (the round must not be sent)."""
    row = _row(price)
    input_cost = estimate_round(
        row, input_tokens=input_tokens, cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens
    )
    per_token = llm_pricing.output_price_per_token(row, input_tokens=input_tokens) or _ZERO
    available = Decimal(max_usd) - Decimal(spent) - Decimal(reserved_final) - input_cost
    if per_token <= 0:
        return int(ceiling)
    affordable = int((available / per_token).to_integral_value(rounding="ROUND_FLOOR"))
    if affordable < floor:
        return 0
    return min(int(ceiling), affordable)


def predicted_cache_ratio(state: LoopState, *, new_tokens: int) -> float:
    """The share of the next round's input expected to be a cache read.
    Round 1: nothing. Round 2: prefix / (prefix + new) (the whole first
    request is a cache candidate; 90% of it on Anthropic, where the
    explicit breakpoints decide). From round 3: the last round's observed
    ratio on either provider."""
    r = state.next_round_no
    if r == 1:
        return 0.0
    last = state.last
    prefix = (last.input_tokens + last.output_tokens) if last else state.prefix_tokens
    if r == 2:
        share = ANTHROPIC_PREFIX_CACHE_SHARE if state.provider == "anthropic" else 1.0
        return share * prefix / max(1, prefix + new_tokens)
    observed = state.latest_cache_ratio
    if observed is not None:
        return observed
    return ANTHROPIC_PREFIX_CACHE_SHARE if state.provider == "anthropic" else OPENAI_DEFAULT_CACHE_RATIO


def plan_round(state: LoopState, *, new_tokens: int) -> RoundPlan:
    """Size the next round. `new_tokens` is the planning estimate of what
    this round adds (tool results, images, extra parts; the first user
    turn on round 1 is `state.prefix_tokens`)."""
    budget = state.budget
    r = state.next_round_no
    last = state.last
    if r > budget.max_rounds + budget.max_repair_rounds:
        # Past the kind's rounds plus its bounded repair rounds: nothing more
        # may be sent, whatever the dollars say.
        return RoundPlan(
            round_no=r, final=True, max_tokens=0, est_usd=_ZERO.quantize(_QUANT), input_tokens=0,
            cached_tokens=0, cache_write_tokens=0, reserved_final_usd=_ZERO.quantize(_QUANT), reason="max_rounds",
        )
    context = (last.input_tokens + last.output_tokens) if last else 0
    input_tokens = context + int(new_tokens) + (state.prefix_tokens if last is None else 0)
    ratio = predicted_cache_ratio(state, new_tokens=new_tokens)
    cached = int(input_tokens * ratio)
    write = input_tokens if (state.provider == "anthropic" and r == 1) else 0

    final = r >= budget.max_rounds or state.force_final
    reason = "continue"
    if r >= budget.max_rounds:
        reason = "max_rounds"
    elif state.force_final:
        reason = "forced"
    if not final and input_tokens > MAX_ROUND_INPUT_TOKENS.get(state.provider, MAX_ROUND_INPUT_TOKENS["openai"]):
        final, reason = True, "input_limit"
    if (
        not final
        and budget.min_cache_ratio is not None
        and r >= 3
        and (state.observed_cache_ratio or 0.0) < budget.min_cache_ratio
    ):
        final, reason = True, "cache_gate"

    def _reserved_final(after_output: int) -> Decimal:
        # The final round after this one: this round's input and output
        # become context, the final answer is the kind's planned output.
        ctx = input_tokens + after_output
        final_ratio = state.latest_cache_ratio if state.latest_cache_ratio is not None else (
            ANTHROPIC_PREFIX_CACHE_SHARE if state.provider == "anthropic" else OPENAI_DEFAULT_CACHE_RATIO
        )
        return estimate_round(
            state.price,
            input_tokens=ctx,
            cached_tokens=int(ctx * final_ratio),
            output_tokens=budget.final_output_tokens,
        )

    def _size(is_final: bool) -> tuple[int, Decimal]:
        floor = budget.final_output_tokens if is_final else MIN_ROUND_OUTPUT_TOKENS
        ceiling = budget.max_tokens_ceiling if is_final else budget.tool_round_output_tokens
        reserved = _ZERO if is_final else _reserved_final(ceiling)
        cap = round_max_tokens(
            price=state.price,
            max_usd=budget.max_usd,
            spent=state.spent,
            reserved_final=reserved,
            input_tokens=input_tokens,
            cached_tokens=cached,
            cache_write_tokens=write,
            floor=min(floor, ceiling),
            ceiling=ceiling,
        )
        return cap, reserved

    cap, reserved = _size(final)
    if not final and cap == 0:
        # The tool round plus a final answer no longer fit: finish now.
        final, reason = True, "budget"
        cap, reserved = _size(True)
    est = estimate_round(
        state.price,
        input_tokens=input_tokens,
        cached_tokens=cached,
        output_tokens=cap,
        cache_write_tokens=write,
    )
    if not final and state.spent + est + reserved > budget.max_usd:
        final, reason = True, "budget"
        cap, reserved = _size(True)
        est = estimate_round(
            state.price, input_tokens=input_tokens, cached_tokens=cached, output_tokens=cap, cache_write_tokens=write
        )
    if final and cap == 0:
        reason = "budget"
        est = _ZERO
    return RoundPlan(
        round_no=r,
        final=final,
        max_tokens=cap,
        est_usd=est.quantize(_QUANT, rounding=ROUND_HALF_UP),
        input_tokens=input_tokens,
        cached_tokens=cached,
        cache_write_tokens=write,
        reserved_final_usd=reserved.quantize(_QUANT, rounding=ROUND_HALF_UP),
        reason=reason,
    )


def record_round(
    state: LoopState,
    plan: RoundPlan,
    *,
    usage: Any,
    cost_usd: Decimal | None,
    stop_reason: str,
    tool_calls: int = 0,
    images: int = 0,
) -> str:
    """Store a finished round and return the decision: "done" (final
    answer in), "continue" (tool round), "forced_final" (a max_tokens stop
    on a tool round, or the first stray tool call on a final round: the
    next round is the final one with what is left) or "failed" (a
    max_tokens stop on the final round, or a stray tool call on a final
    round once the max_repair_rounds extra rounds are used up)."""
    in_t = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else plan.input_tokens
    cached = int(getattr(usage, "cached_tokens", 0) or 0) if usage is not None else plan.cached_tokens
    out_t = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else plan.max_tokens
    cost = Decimal(cost_usd) if cost_usd is not None else plan.est_usd
    state.rounds.append(
        RoundRecord(
            round_no=plan.round_no,
            input_tokens=in_t,
            cached_tokens=cached,
            output_tokens=out_t,
            cost_usd=cost.quantize(_QUANT, rounding=ROUND_HALF_UP),
            stop_reason=stop_reason,
            final=plan.final,
        )
    )
    state.tool_calls += int(tool_calls)
    state.images += int(images)
    if stop_reason == "max_tokens":
        if plan.final:
            return "failed"
        state.force_final = True
        return "forced_final"
    if stop_reason == "final":
        return "done"
    if plan.final:
        # A tool call on the planned final round: the loop answers it with
        # an error result and gets ONE bounded extra round (spec 4.3); a
        # second stray call past max_repair_rounds fails the group.
        state.repairs += 1
        if state.repairs > state.budget.max_repair_rounds:
            return "failed"
        state.force_final = True
        return "forced_final"
    if state.tool_calls >= state.budget.max_tool_calls or state.images >= state.budget.max_images > 0:
        state.force_final = True
        return "forced_final"
    return "continue"


def over_budget_ratio(spent: Decimal, max_usd: Decimal) -> float:
    """spent / max_usd - 1 (0 when within budget)."""
    if max_usd <= 0:
        return 0.0
    return max(0.0, float((Decimal(spent) - Decimal(max_usd)) / Decimal(max_usd)))


def within_tolerance(spent: Decimal, max_usd: Decimal, tolerance: float = OVER_BUDGET_TOLERANCE) -> bool:
    return over_budget_ratio(spent, max_usd) <= tolerance


def tokens_for_chars(chars: int, chars_per_token: int = 3) -> int:
    return math.ceil(max(0, int(chars)) / max(1, chars_per_token))


__all__ = [
    "KIND_BUDGETS",
    "KINDS",
    "LoopBudget",
    "LoopState",
    "MAX_ROUND_INPUT_TOKENS",
    "OVER_BUDGET_TOLERANCE",
    "RoundPlan",
    "RoundRecord",
    "budget_for",
    "budget_key",
    "estimate_round",
    "over_budget_ratio",
    "plan_round",
    "predicted_cache_ratio",
    "record_round",
    "round_max_tokens",
    "tokens_for_chars",
    "within_tolerance",
]
