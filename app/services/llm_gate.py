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
  the endpoint URL or response bodies).

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
from typing import Iterator

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


def log_call(
    *,
    feature: str,
    provider: str,
    model: str,
    tier_name: str,
    ok: bool,
    error: Exception | None,
    duration_ms: int,
) -> None:
    """Record one LLM call in llm_call_log. Never raises."""
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
        get_supabase().table("llm_call_log").insert(row).execute()
    except Exception:  # noqa: BLE001 - accounting must never break the call
        logger.debug("llm_call_log write failed", exc_info=True)
