"""Admission control and accounting for every LLM call.

Two jobs, both provider-wide (they cover the self-hosted pool AND the
3rd-party pools; which limits apply depends on the route's provider):

- slot(): a per-provider concurrency gate. The self-hosted vLLM box has a
  real throughput sweet spot (roughly 8-16 concurrent requests); beyond it
  every request slows down until they all time out. The gate caps in-flight
  calls per provider and reserves a couple of slots for interactive calls
  (a user waiting on an HTTP response) so a pile of background jobs can
  never starve them. Background tiers wait longer for a slot; when the wait
  runs out the call fails with LlmBusy, which classifies as a transient
  "overloaded" error, so queued jobs retry and pipeline callers fall back
  exactly like any other transient failure.

- log_call(): one llm_call_log row per call (success or failure) feeding
  the dev AI monitor page. Best-effort: a logging problem must never break
  the call, so every exception is swallowed. Only the sanitized
  user-facing message is stored, never str(exc) (raw SDK errors can carry
  the endpoint URL or response bodies). Since 0152 the row also carries the
  provider-reported token usage and the computed cost (services/llm_pricing)
  whenever the transport reported usage; the columns stay NULL otherwise.
  A database without 0152 (a backend deployed before the migration) rejects
  those columns, so the row is retried once without them and a WARNING is
  logged once per process: the call log keeps working, only the spend
  numbers are missing until the migration is applied.
- eval_sweep_id (AI Estimating eval ledger, migration MC): stamped when
  the process runs inside an eval sweep (EVAL_SWEEP_ID, or the sweep
  services/eval_ledger admitted the call under) and OMITTED otherwise, so
  a database without the column never sees the key. A database without the
  column rejects a stamped row; it is retried once without the key and a
  WARNING is logged once per process (the ledger then counts only what the
  process committed itself).

Tiers ride a contextvar instead of a parameter threaded through every call
site: the queue worker and the ingest loops wrap their work in
tier("job")/tier("pipeline"); everything else defaults to "interactive".
contextvars survive asyncio.to_thread (it copies the current context), and
setting the var inside a plain worker thread scopes it to that thread.

State is per-process: with 2 uvicorn workers the effective provider cap is
2x the configured limit. Size the env caps accordingly (the defaults
already assume it).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from decimal import Decimal
from typing import Callable, Iterator

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

TIER_JOB = "job"
TIER_INTERACTIVE = "interactive"
TIER_PIPELINE = "pipeline"

_BUSY_MESSAGE = (
    "The AI service is handling too many requests right now. "
    "Please try again in a moment."
)

_ERROR_MAX_CHARS = 500


class LlmBusy(RuntimeError):
    """Admission to the AI concurrency gate timed out: too much in flight."""


_tier_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_tier", default=TIER_INTERACTIVE
)
_job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_job_id", default=None
)


def current_tier() -> str:
    return _tier_var.get()


def current_job_id() -> str | None:
    return _job_id_var.get()


@contextlib.contextmanager
def tier(tier_name: str, job_id: str | None = None) -> Iterator[None]:
    """Mark every LLM call inside the block as this tier (and job)."""
    t1 = _tier_var.set(tier_name)
    t2 = _job_id_var.set(job_id)
    try:
        yield
    finally:
        _tier_var.reset(t1)
        _job_id_var.reset(t2)


# ── Concurrency gate ─────────────────────────────────────────────────────


class _Gate:
    """Counting gate with reserved interactive headroom.

    total permits = the provider cap; background callers additionally pass
    through a smaller semaphore sized (total - reserved), so `reserved`
    slots are only ever reachable by interactive calls.
    """

    def __init__(self, total: int, reserved: int) -> None:
        total = max(1, total)
        reserved = min(max(0, reserved), total - 1)
        self.total = threading.BoundedSemaphore(total)
        self.background = threading.BoundedSemaphore(total - reserved)


_gates: dict[tuple[str, int, int], _Gate] = {}
_gates_lock = threading.Lock()


def _gate_for(provider: str, s: Settings) -> _Gate:
    if provider == "self_hosted":
        total = s.llm_max_concurrent_self_hosted
    else:
        total = s.llm_max_concurrent_third_party
    key = (provider, total, s.llm_interactive_reserved_slots)
    with _gates_lock:
        gate = _gates.get(key)
        if gate is None:
            gate = _gates.setdefault(key, _Gate(total, s.llm_interactive_reserved_slots))
    return gate


def reset_gates() -> None:
    """Test hook: drop all gate state."""
    with _gates_lock:
        _gates.clear()


@contextlib.contextmanager
def slot(
    provider: str, tier_name: str, settings: Settings | None = None
) -> Iterator[None]:
    """Hold one in-flight slot for `provider` for the duration of the call.

    Raises LlmBusy when no slot frees up within the tier's wait budget.
    """
    s = settings or get_settings()
    gate = _gate_for(provider, s)
    if tier_name == TIER_INTERACTIVE:
        if not gate.total.acquire(timeout=s.llm_interactive_wait_seconds):
            raise LlmBusy(_BUSY_MESSAGE)
        try:
            yield
        finally:
            gate.total.release()
        return

    wait = s.llm_background_wait_seconds
    deadline = time.monotonic() + wait
    if not gate.background.acquire(timeout=wait):
        raise LlmBusy(_BUSY_MESSAGE)
    try:
        if not gate.total.acquire(timeout=max(0.1, deadline - time.monotonic())):
            raise LlmBusy(_BUSY_MESSAGE)
        try:
            yield
        finally:
            gate.total.release()
    finally:
        gate.background.release()


# ── Call accounting ──────────────────────────────────────────────────────

# The llm_call_log columns 0152 added. Left out of the retry when the
# database predates the migration.
_USAGE_COLUMNS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "cost_usd")
# PostgREST's "column not found in the schema cache" error code.
_PGRST_UNKNOWN_COLUMN = "PGRST204"
_usage_columns_warned = False
_SWEEP_COLUMN = "eval_sweep_id"
_sweep_column_warned = False
# The llm_call_log columns 0170 added (BDR Assistant attribution). Left out
# of the retry when the database predates the migration.
_ATTRIBUTION_COLUMNS = ("user_id", "assistant_run_id")
_attribution_columns_warned = False
# The llm_call_log column 0174 added (the assistant spend hold a call counts
# against). Its own fallback group: a database with 0170 but not 0174 drops
# only this stamp and keeps user_id and assistant_run_id.
_RESERVATION_COLUMN = "spend_reservation_id"
_reservation_column_warned = False


def _eval_sweep_id() -> str | None:
    """The eval sweep to stamp on this call's row, or None (key omitted)."""
    try:
        from app.services import eval_ledger

        return eval_ledger.sweep_id_for_log(get_settings())
    except Exception:  # noqa: BLE001 - accounting must never break the call
        return None


def _call_attribution():
    """The (user_id, run_id) an assistant turn stamps on its calls (the
    per-user daily cap sums llm_call_log by user_id), or None outside one.
    Lazy import: the gate never depends on the assistant package at load."""
    try:
        from app.assistant.llm_messages import current_attribution

        return current_attribution()
    except Exception:  # noqa: BLE001 - accounting must never break the call
        return None


def _warn_attribution_columns_missing() -> None:
    global _attribution_columns_warned
    if _attribution_columns_warned:
        return
    _attribution_columns_warned = True
    logger.warning(
        "llm_call_log has no user_id/assistant_run_id columns (migration 0170 "
        "not applied); logging assistant calls without them until it is."
    )


def _warn_reservation_column_missing() -> None:
    global _reservation_column_warned
    if _reservation_column_warned:
        return
    _reservation_column_warned = True
    logger.warning(
        "llm_call_log has no spend_reservation_id column (migration 0174 not "
        "applied); logging assistant calls without the hold stamp until it is."
    )


def _warn_sweep_column_missing() -> None:
    global _sweep_column_warned
    if _sweep_column_warned:
        return
    _sweep_column_warned = True
    logger.warning(
        "llm_call_log has no eval_sweep_id column (migration MC not applied); "
        "logging eval calls without the sweep stamp until it is."
    )


def _warn_usage_columns_missing() -> None:
    """One WARNING per process, not one per call."""
    global _usage_columns_warned
    if _usage_columns_warned:
        return
    _usage_columns_warned = True
    logger.warning(
        "llm_call_log has no token/cost columns (migration 0152 not applied); "
        "logging calls without usage until it is."
    )


def _insert_with_fallback(sb, row: dict) -> None:
    """Insert one llm_call_log row. A database that predates a migration
    rejects that migration's optional columns: the row is retried without
    the column group the error names (the MC sweep stamp, then the 0152
    usage columns), at most once per group, with one WARNING per process.
    A bare PGRST204 that names no column drops the sweep stamp first."""
    groups: list[tuple[list[str], Callable[[], None]]] = [
        ([_SWEEP_COLUMN], _warn_sweep_column_missing),
        (list(_USAGE_COLUMNS), _warn_usage_columns_missing),
        ([_RESERVATION_COLUMN], _warn_reservation_column_missing),
        (list(_ATTRIBUTION_COLUMNS), _warn_attribution_columns_missing),
    ]
    while True:
        try:
            sb.table("llm_call_log").insert(row).execute()
            return
        except Exception as exc:  # noqa: BLE001 - retried below or re-raised
            text = f"{getattr(exc, 'code', '')} {getattr(exc, 'message', '')} {exc}"
            present = [(keys, warn) for keys, warn in groups if any(k in row for k in keys)]
            named = [(keys, warn) for keys, warn in present if any(f"'{k}'" in text for k in keys)]
            pick = named[0] if named else (present[0] if present and _PGRST_UNKNOWN_COLUMN in text else None)
            if pick is None:
                raise
            keys, warn = pick
            for k in keys:
                row.pop(k, None)
            groups = [g for g in groups if g[0] is not keys]
            warn()


def log_call(
    *,
    feature: str,
    provider: str,
    model: str,
    tier_name: str,
    ok: bool,
    error: Exception | None,
    duration_ms: int,
    input_tokens: int | None = None,
    cached_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cost_usd: Decimal | float | None = None,
) -> None:
    """Record one LLM call in llm_call_log. Never raises.

    The token and cost arguments are optional: None leaves the column NULL
    (the transport did not report usage, or the model has no price row).
    input_tokens includes cached_tokens and output_tokens includes
    reasoning_tokens, the way OpenAI reports them."""
    try:
        from app.core.supabase_client import get_supabase
        from app.services import llm_errors

        row: dict = {
            "feature": feature,
            "provider": provider,
            "model": model or None,
            "tier": tier_name,
            "job_id": current_job_id(),
            "ok": ok,
            "duration_ms": duration_ms,
        }
        if error is not None:
            label = f"self-hosted:{model}" if provider == "self_hosted" else model
            row["error_kind"] = llm_errors.classify(error)
            row["error"] = llm_errors.user_message(error, label)[:_ERROR_MAX_CHARS]
        for column, value in (
            ("input_tokens", input_tokens),
            ("cached_tokens", cached_tokens),
            ("output_tokens", output_tokens),
            ("reasoning_tokens", reasoning_tokens),
        ):
            if value is not None:
                row[column] = int(value)
        if cost_usd is not None:
            # float, not Decimal: the PostgREST client JSON-encodes the body
            # and Decimal is not JSON serializable. numeric(12, 6) keeps the
            # 6 decimals cost_for rounds to.
            row["cost_usd"] = float(cost_usd)
        attribution = _call_attribution()
        if attribution is not None:
            row["user_id"] = attribution.user_id
            row["assistant_run_id"] = attribution.run_id
            reservation_id = getattr(attribution, "reservation_id", None)
            if reservation_id:
                row[_RESERVATION_COLUMN] = reservation_id
        sweep_id = _eval_sweep_id()
        if sweep_id:
            row[_SWEEP_COLUMN] = sweep_id
        _insert_with_fallback(get_supabase(), row)
    except Exception:  # noqa: BLE001 - accounting must never break the call
        logger.debug("llm_call_log write failed", exc_info=True)
