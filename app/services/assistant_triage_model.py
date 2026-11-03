"""The one model call of Phase 8 triage (docs/ASSISTANT_PHASE8_PLAN.md 5.4).

`classify(run_id, pack, settings=...)` sends one facts-only batch to the
`assistant_triage` feature (gpt-6-luna, pinned to OpenAI) and returns a
`TriageResult`: the parsed answer and the run's usage (the routed model,
the company hold and the input, cached and output tokens and cost_usd read
from llm.last_usage() / last_cost_usd() right after the call, in the same
thread). A failure after the call reached the model carries the same usage
on the raised error (`TriageError.usage`), so a billed truncated or bad
answer still records its tokens on the run. It is the only Phase 8 module that names services.llm, the
llm_gate exception classes or the triage feature; the triage service
(app/services/assistant_triage.py) imports only `classify`, the closed
exception set below and `ALL`, so it never names the model service
(tests/test_assistant_no_model_unattended.py:42).

Money (owner answers 11 (b), 12 (b) and 22 (a)):
- The call is paid by the company spend identity, never by the person whose
  rows are triaged: the hold is reserved under `spend.COMPANY_SPEND_ID` with
  kind `spend.KIND_TRIAGE`, the call runs inside
  `llm_messages.attributed(COMPANY_SPEND_ID, run_id, reservation_id=hold)`
  so llm_gate.log_call stamps the company id, the triage run id and the hold
  on the llm_call_log row, and the hold is settled with the company id in
  every case. The company cap (assistant_settings.global_daily_cap_usd)
  still stops triage: a refusal is `TriageCapRefused` (`cap_global`).
- The live-test cap `assistant_triage_test_spend_cap_usd` (unset by default;
  the boot validator allows it only on the dev project with an allowlist)
  is compared with today's company triage spend plus live triage holds plus
  the new hold before the reserve; overflow is `TriageTestCap` (`cap_test`).
- Before any reserve, a route that is not OpenAI or a model with no price
  row fails closed (`TriageUnpriced`): a self-hosted fallthrough would log a
  NULL cost and slip past every cap.

The hold covers the system prompt, the answer schema and the rendered pack
(len/4 of all three, `triage.estimate_input_tokens`) plus
`max_out = min(setting, 1200 + 200 x items)` output tokens, priced from
`route.model`, and lives `triage_hold_ttl(settings)` seconds, the same
formula as the claim lease, so it outlives the slowest call the lease
allows. gpt-6-luna's reasoning tokens count against max_output_tokens,
and the live test cut off a 1-item batch at the old 400 + 200 x items
allowance (P8L-3), hence the 1200 base. `assistant_triage_reasoning_effort`
(empty by default: the model's own effort, the one the 16.4 golden set
passed at) is sent as the Responses reasoning effort when set; "low" gave
invalid answers at batch size 10 in the 2026-11-03 eval.

The call runs in llm_gate's `pipeline` tier: the polling thread would
otherwise inherit the interactive tier and take chat's reserved slots.

Settings that arrive with WI-6 (`assistant_triage_model`,
`assistant_triage_max_output_tokens`, `assistant_triage_timeout_seconds`,
`assistant_triage_test_spend_cap_usd`) are read with `getattr` defaults
equal to their planned defaults, and `spend.KIND_TRIAGE`,
`spend.COMPANY_SPEND_ID` and `repo.triage_company_spend` (WI-5) are looked
up when `classify` runs, so this module imports and its tests run before
those land. A missing one fails closed as `TriageSpendCheck`.

Plain `def` throughout: the caller is the triage polling thread
(asyncio.to_thread), and the Supabase SDK under spend and repo is sync.
Never log, print or return the resolved route (it carries the API key).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.assistant import llm_messages, repo, spend
from app.services import llm, llm_errors, llm_gate, llm_pricing

logger = logging.getLogger(__name__)

FEATURE = "assistant_triage"
SCHEMA_NAME = "triage_answer"

# Planned defaults of the WI-6 settings (docs/ASSISTANT_PHASE8_PLAN.md 13).
DEFAULT_MAX_OUTPUT_TOKENS = 3200
DEFAULT_TIMEOUT_SECONDS = 45
REASONING_EFFORTS = ("low", "medium", "high")
# P8L-3: a 1-item batch used up to 681 reasoning tokens live, so the base
# covers the reasoning and each item adds its answer. config._check_triage
# repeats these two numbers (pinned by tests/test_config_assistant_triage.py).
OUTPUT_TOKENS_BASE = 1200
OUTPUT_TOKENS_PER_ITEM = 200

# The exact prefix of every cut-off LlmBadOutput services/llm raises
# (`_raise_if_incomplete` and `_self_hosted_call`); pinned by
# tests/test_assistant_triage_model.py against the real messages.
CUT_OFF_PREFIX = "Model response was cut off"


# ── The closed exception set (5.4, F7) ──────────────────────────────────────


class TriageError(Exception):
    """Base of the closed set. `error_kind` is the run's error kind in
    assistant_triage_runs; `item_class` is how the A4 retry helper treats
    the batch's items (infrastructure, answer_quality or cap)."""

    error_kind = "model_error"
    item_class = "infrastructure"
    # The run's usage when the call reached the model (see `_call_usage`);
    # None when it failed before the call.
    usage: dict | None = None


class TriageUnpriced(TriageError):
    """The route is not OpenAI or its model has no price row."""

    error_kind = "unpriced"
    item_class = "infrastructure"


class TriageTestCap(TriageError):
    """The live-test spend cap would be passed by this hold."""

    error_kind = "cap_test"
    item_class = "cap"


class TriageCapRefused(TriageError):
    """The spend admission refused the hold (the company cap; a triage hold
    has no user scope)."""

    error_kind = "cap_global"
    item_class = "cap"


class TriageSpendCheck(TriageError):
    """The spend check itself failed (the reserve RPC, the test-cap read or
    a missing spend identity); fail closed."""

    error_kind = "spend_check"
    item_class = "infrastructure"


class TriageTruncated(TriageError):
    """The model's answer was cut off (billed; the items retry one at a time)."""

    error_kind = "truncated"
    item_class = "answer_quality"


class TriageBadAnswer(TriageError):
    """The model answered unusably (invalid JSON, wrong shape), or the batch
    could not be rendered into a request."""

    error_kind = "bad_answer"
    item_class = "answer_quality"


class TriageBusy(TriageError):
    """The llm_gate admission wait ran out (LlmBusy)."""

    error_kind = "busy"
    item_class = "infrastructure"


class TriageTimeout(TriageError):
    """The provider call timed out (after the SDK's own retries)."""

    error_kind = "timeout"
    item_class = "infrastructure"


class TriageModelError(TriageError):
    """Any other failure of the call (provider error, refusal, config)."""

    error_kind = "model_error"
    item_class = "infrastructure"


ALL: tuple[type[TriageError], ...] = (
    TriageUnpriced,
    TriageTestCap,
    TriageCapRefused,
    TriageSpendCheck,
    TriageTruncated,
    TriageBadAnswer,
    TriageBusy,
    TriageTimeout,
    TriageModelError,
)
# The failures that open the service's breaker (5.7).
BREAKER: tuple[type[TriageError], ...] = (TriageModelError, TriageTimeout, TriageBusy)


@dataclass(frozen=True)
class TriageResult:
    """What `classify` returns: the parsed answer and the run's usage for
    the fenced finish (p_usage of assistant_triage_finish: model,
    reservation_id, input_tokens, cached_tokens, output_tokens, cost_usd)."""

    answer: Any
    usage: dict = field(default_factory=dict)


def _translate(exc: BaseException) -> TriageError:
    """Map an exception from the model call to the closed set. The only
    place that names the services/llm and llm_gate exception classes."""
    if isinstance(exc, TriageError):
        return exc
    if isinstance(exc, llm.LlmBadOutput):  # LlmBadOutputPermanent included
        if str(exc).startswith(CUT_OFF_PREFIX):
            return TriageTruncated("The triage answer was cut off.")
        return TriageBadAnswer("The triage answer was not usable.")
    if isinstance(exc, llm_gate.LlmBusy):
        return TriageBusy("The AI concurrency gate was full.")
    # The SDK timeout complete_json lets through (openai.APITimeoutError
    # after the SDK's own retries, or an httpx or builtin timeout), as
    # services/llm_errors classifies it: this module may not import a
    # provider SDK (tests/test_llm_static_sdk.py).
    try:
        timed_out = llm_errors.classify(exc) == llm_errors.KIND_TIMEOUT
    except Exception:  # noqa: BLE001 - classification must never mask the failure
        timed_out = False
    if timed_out:
        return TriageTimeout("The triage call timed out.")
    return TriageModelError(f"The triage call failed ({type(exc).__name__}).")


# ── Sizing ──────────────────────────────────────────────────────────────────


def _int_setting(settings: Any, name: str, default: int) -> int:
    value = getattr(settings, name, None)
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def timeout_seconds(settings: Any) -> float:
    return float(_int_setting(settings, "assistant_triage_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))


def triage_hold_ttl(settings: Any) -> int:
    """llm_background_wait_seconds + timeout x (third_party_llm_max_retries + 1)
    + 60: the claim lease formula (5.2), 330 s with today's defaults, so the
    hold covers the slowest call the lease allows. Never a fixed 300."""
    wait = float(getattr(settings, "llm_background_wait_seconds", 180.0))
    retries = _int_setting(settings, "third_party_llm_max_retries", 1)
    return int(round(wait + timeout_seconds(settings) * (retries + 1) + 60))


def reasoning_effort(settings: Any) -> str | None:
    """The effort sent on the triage call: the setting when it is low,
    medium or high, else None, so nothing is sent and the model's own
    default applies (the boot validator refuses other values)."""
    raw = getattr(settings, "assistant_triage_reasoning_effort", "")
    value = raw.strip().lower() if isinstance(raw, str) else ""
    return value if value in REASONING_EFFORTS else None


def max_output_tokens(settings: Any, item_count: int) -> int:
    """min(setting, 1200 + 200 x items): reasoning tokens bill as output and
    count against max_output_tokens, so a full batch is never cut off by
    the formula and a small batch holds less."""
    ceiling = _int_setting(settings, "assistant_triage_max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    return min(ceiling, OUTPUT_TOKENS_BASE + OUTPUT_TOKENS_PER_ITEM * max(int(item_count), 0))


def _test_cap(settings: Any) -> Decimal | None:
    raw = getattr(settings, "assistant_triage_test_spend_cap_usd", None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise TriageSpendCheck("The triage test spend cap is not a number.") from exc


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _triage() -> Any:
    """app.assistant.triage (WI-2: SYSTEM_PROMPT, ANSWER_SCHEMA, render_pack,
    estimate_input_tokens), imported when a batch is classified so this
    module imports while that one is still being written."""
    from app.assistant import triage

    return triage


# ── The call ────────────────────────────────────────────────────────────────


def classify(run_id: str, pack: dict, *, settings: Any) -> TriageResult:
    """Classify one batch. Returns a `TriageResult` (the parsed answer;
    format validation is triage.validate's job; and the run's usage).
    Raises only the closed set in `ALL`, with `usage` set on the error when
    the call reached the model; any hold reserved here is settled under the
    company id before this returns or raises."""
    route = llm.resolve(FEATURE, settings)  # never logged, printed or returned
    model = route.model
    if route.provider != "openai":
        raise TriageUnpriced("The triage feature is not routed to OpenAI.")
    price = llm_pricing.price_for(model)
    if price is None or price.provider != "openai":
        raise TriageUnpriced("The triage model has no OpenAI price row.")

    try:
        triage = _triage()
        system = triage.SYSTEM_PROMPT
        schema = triage.ANSWER_SCHEMA
        rendered = triage.render_pack(pack)
        est_in = int(triage.estimate_input_tokens(system, schema, rendered))
        max_out = max_output_tokens(settings, len(pack["items"]))
    except Exception as exc:  # noqa: BLE001 - a batch that cannot be rendered is a bad pack
        logger.warning("assistant triage: the batch could not be rendered (%s)", type(exc).__name__)
        raise TriageBadAnswer("The triage batch could not be rendered.") from exc

    amount = llm_pricing.estimate_cost(price, input_tokens=est_in, output_tokens=max_out)
    if amount is None:
        raise TriageUnpriced("The triage call could not be priced.")

    try:
        company = spend.COMPANY_SPEND_ID  # A2a; never a person (owner answer 12 (b))
        kind = spend.KIND_TRIAGE
    except AttributeError as exc:
        raise TriageSpendCheck("The company spend identity is not available.") from exc

    test_cap = _test_cap(settings)
    if test_cap is not None:
        try:
            today = repo.triage_company_spend(None)  # A6.7: {spent, held} under the company id
            used = _dec(today["spent"]) + _dec(today["held"])
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise TriageSpendCheck("The company triage spend could not be read.") from exc
        if used + amount > test_cap:
            raise TriageTestCap("The live-test triage spend cap is reached.")

    try:
        hold = spend.reserve(
            company, kind=kind, amount=amount, ttl=triage_hold_ttl(settings),
            run_id=run_id, settings=settings,
        )
    except spend.SpendRefused as exc:
        raise TriageCapRefused("The company assistant spend cap is reached.") from exc
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise TriageSpendCheck("The spend reservation failed.") from exc

    try:
        with llm_gate.tier(llm_gate.TIER_PIPELINE), llm_messages.attributed(
            company, run_id, reservation_id=hold,
        ):
            answer = llm.complete_json(
                FEATURE,
                system=system,
                messages=[{"role": "user", "content": rendered}],
                schema=schema,
                schema_name=SCHEMA_NAME,
                max_tokens=max_out,
                timeout=timeout_seconds(settings),
                settings=settings,
                reasoning_effort=reasoning_effort(settings),
            )
        usage = _call_usage(model, hold)  # right after the call, same thread
    except Exception as exc:  # noqa: BLE001 - every failure maps into the closed set
        usage = _call_usage(model, hold)
        error = _translate(exc)
        if isinstance(error, TriageTruncated):
            usage = _floor_cut_off_usage(usage, price, est_in, max_out)
        error.usage = usage
        raise error from exc
    finally:
        _settle(company, hold)
    return TriageResult(answer=answer, usage=usage)


def _call_usage(model: str, hold: str) -> dict:
    """The run's usage: the routed model name, the company hold and the
    numbers services/llm published for the call just made (input_tokens
    includes cached_tokens, output_tokens includes reasoning). Read right
    after the call in the same thread (the values ride a contextvar that
    every complete_* clears first, so a call that never reached the
    provider shows no numbers, never an earlier call's). Never raises."""
    out: dict = {"model": model, "reservation_id": hold}
    try:
        numbers = llm.last_usage()
        cost = llm.last_cost_usd()
    except Exception as exc:  # noqa: BLE001 - usage is bookkeeping; never mask the answer or the error
        logger.warning("assistant triage: reading the call usage failed (%s)", type(exc).__name__)
        return out
    if numbers is not None:
        for key in ("input_tokens", "cached_tokens", "output_tokens"):
            value = getattr(numbers, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                out[key] = max(0, value)
    if cost is not None:
        out["cost_usd"] = cost
    return out


def _floor_cut_off_usage(usage: dict, price: Any, est_in: int, max_out: int) -> dict:
    """P8L-4-R1: a call cut off at max_output_tokens necessarily produced
    about max_out output tokens, so a usage that reads 0 (or none) output
    tokens for it is wrong (llm_call_log 6bd16e16, 2026-11-03). The run's
    own usage then records max_out output tokens, the estimated input when
    none was read, and the cost priced from those, so the run row and the
    admin cost sum never show a truncated call as free. services/llm
    (_floor_cut_off) now floors the same case on the llm_call_log row, so
    last_usage() already reads the allowance and this returns it unchanged;
    it still covers a cut-off whose allowance services/llm could not read.
    Never raises."""
    try:
        if int(usage.get("output_tokens") or 0) > 0:
            return usage
        floored = dict(usage)
        floored["output_tokens"] = int(max_out)
        if int(floored.get("input_tokens") or 0) <= 0:
            floored["input_tokens"] = max(0, int(est_in))
        floored.setdefault("cached_tokens", 0)
        cost = llm_pricing.estimate_cost(price, input_tokens=floored["input_tokens"],
                                         output_tokens=floored["output_tokens"])
        if cost is not None:
            floored["cost_usd"] = cost
        logger.warning("assistant triage: a cut-off call reported no output tokens; "
                       "the run records the allowance instead (P8L-4)")
        return floored
    except Exception as exc:  # noqa: BLE001 - usage is bookkeeping; never mask the error
        logger.warning("assistant triage: flooring a cut-off usage failed (%s)", type(exc).__name__)
        return usage


def _settle(company: str, hold: str) -> None:
    """Settle the hold under the company id. Best effort: a failed settle
    must not hide the answer or the call's own error; the hold expires at
    its TTL and the service's daily purge step releases it (5.1 step 6)."""
    try:
        spend.settle(company, hold)
    except Exception as exc:  # noqa: BLE001
        logger.warning("assistant triage: settling the company hold failed (%s)", type(exc).__name__)
