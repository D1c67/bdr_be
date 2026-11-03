"""Spend reservations (docs/ASSISTANT.md, Permissions item 12;
docs/ASSISTANT_PHASE2_PLAN.md 3.8).

One source for the assistant's daily caps: the database function
assistant_spend_state (migration 0174) sums today's attributed llm_call_log
cost (Pacific day) and the live holds, per user and company-wide, and
assistant_spend_reserve admits a hold under one advisory lock, so both
uvicorn workers see the same numbers. A hold is live while it is active and
not expired; a call that runs under a hold stamps its id on its log row, so
the admission counts max(hold, logged) for it, never both.

  turns        each model call reserves its estimate (kind turn_call,
               10 minutes), runs inside attributed(..., reservation_id=hold)
               and settles in a finally (app/assistant/loop.py);
  split tasks  split-drop reserves the card's estimate once (kind
               split_task, 6 hours); the continuation settles it, the
               split-drop failure paths release it;
  AE work      a page read (plans_pages) or an AI Estimating run
               (ai_estimate) holds its card's maximum; settle_external
               closes it with the AE ledger's actual cost (phase 4).

Plain `def` throughout (the callers are sync: the loop, the jobs, plain
`def` routes and the threadpool). Every row goes through
app/assistant/repo.py; money travels as Decimal here and as a string over
the wire (the repo wrappers convert).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.assistant import repo
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_USER_CAP = Decimal("5")
DEFAULT_GLOBAL_CAP = Decimal("50")

KIND_TURN_CALL = "turn_call"
KIND_SPLIT_TASK = "split_task"
# Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.7, D10): paid page reads and
# assistant-started AI Estimating runs. Their model calls are not stamped
# with the hold, so they close through settle_external at the AE ledger's
# cost, and the 0180 state counts their closed actuals, settled or released.
KIND_PLANS_PAGES = "plans_pages"
KIND_AI_ESTIMATE = "ai_estimate"
KIND_BROWSER_TASK = "browser_task"
# Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md 4.1 A2a, 5.4; owner questions 11 (b)
# and 12 (b)): model triage spends company money. A triage hold is held only
# by the company spend identity (0196 assistant_company_spend_id(), mirrored
# as COMPANY_SPEND_ID), which holds no other kind; reserve refuses either
# pairing before the RPC (0196 B2 refuses it too) and the RPC checks only the
# company cap for it. Triage is not an external kind: its call carries the
# hold, so settle closes it at the logged cost.
KIND_TRIAGE = "triage"
COMPANY_SPEND_ID = repo.COMPANY_SPEND_ID
KINDS = (KIND_TURN_CALL, KIND_SPLIT_TASK, KIND_PLANS_PAGES, KIND_AI_ESTIMATE, KIND_BROWSER_TASK, KIND_TRIAGE)
EXTERNAL_KINDS = (KIND_PLANS_PAGES, KIND_AI_ESTIMATE, KIND_BROWSER_TASK)
TURN_CALL_TTL_SECONDS = 600
MIN_TTL_SECONDS, MAX_TTL_SECONDS = 30, 43200   # the function's argument check

SCOPE_USER = "user"
SCOPE_GLOBAL = "global"

STATUS_SETTLED = "settled"
STATUS_RELEASED = "released"

_ZERO = Decimal("0")


def _dec(value: Any, default: Decimal = _ZERO) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


@dataclass(frozen=True)
class SpendState:
    """Today's caps, logged spend and live holds (Pacific day)."""

    user_cap: Decimal
    user_spent: Decimal
    user_held: Decimal
    global_cap: Decimal
    global_spent: Decimal
    global_held: Decimal

    @property
    def user_remaining(self) -> Decimal:
        return max(self.user_cap - self.user_spent - self.user_held, _ZERO)

    @property
    def global_remaining(self) -> Decimal:
        return max(self.global_cap - self.global_spent - self.global_held, _ZERO)

    @property
    def over_cap(self) -> bool:
        """The phase 0 guard semantics with holds counted: at or over a cap."""
        return (self.user_spent + self.user_held >= self.user_cap
                or self.global_spent + self.global_held >= self.global_cap)

    @classmethod
    def from_rpc(cls, data: Any, *, user_default: Decimal, global_default: Decimal) -> SpendState:
        d = data if isinstance(data, dict) else {}
        return cls(
            user_cap=_dec(d.get("user_cap"), user_default),
            user_spent=_dec(d.get("user_spent")),
            user_held=_dec(d.get("user_held")),
            global_cap=_dec(d.get("global_cap"), global_default),
            global_spent=_dec(d.get("global_spent")),
            global_held=_dec(d.get("global_held")),
        )


class SpendRefused(Exception):
    """The admission refused a hold: `scope` is "user" or "global", `state`
    the SpendState the function saw."""

    def __init__(self, scope: str, state: SpendState):
        super().__init__(f"assistant spend cap reached ({scope})")
        self.scope = scope
        self.state = state


def cap_defaults(settings: Settings | None = None) -> tuple[Decimal, Decimal]:
    """(user default, global default) for the function arguments: the env
    caps, used only when the user has no assistant_profiles row or the
    assistant_settings row is missing (the same rule as loop.caps_for)."""
    s = settings or get_settings()
    return (
        _dec(getattr(s, "assistant_daily_cap_usd", None), DEFAULT_USER_CAP),
        _dec(getattr(s, "assistant_global_daily_cap_usd", None), DEFAULT_GLOBAL_CAP),
    )


def state(user_id: str, *, settings: Settings | None = None) -> SpendState:
    """rpc assistant_spend_state: today's caps, spend and live holds."""
    user_default, global_default = cap_defaults(settings)
    data = repo.rpc_spend_state(user_id, user_default, global_default)
    return SpendState.from_rpc(data, user_default=user_default, global_default=global_default)


def reserve(
    user_id: str,
    *,
    kind: str,
    amount: Decimal,
    ttl: int,
    run_id: str | None = None,
    task_id: str | None = None,
    approval_id: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Admit one hold of `amount` for `ttl` seconds; returns the hold id.
    Raises SpendRefused when the hold does not fit under the user's or the
    company's cap (spent + held + amount >= cap; a zero amount passes while
    spent + held < cap). Any other failure propagates: callers fail closed."""
    if kind not in KINDS:
        raise ValueError(f"unknown reservation kind {kind!r}")
    if (kind == KIND_TRIAGE) != repo.is_company_spend_id(user_id):
        # A triage hold is the company's; the company id holds nothing else.
        raise ValueError("a triage hold is held only by the company spend identity, which holds no other kind")
    amount = _dec(amount)
    if amount < _ZERO:
        raise ValueError("a reservation amount is never negative")
    ttl = int(ttl)
    if not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        raise ValueError(f"a reservation lives {MIN_TTL_SECONDS} to {MAX_TTL_SECONDS} seconds")
    user_default, global_default = cap_defaults(settings)
    data = repo.rpc_spend_reserve(
        user_id, kind=kind, amount=amount, ttl_seconds=ttl,
        user_cap_default=user_default, global_cap_default=global_default,
        run_id=run_id, task_id=task_id, approval_id=approval_id,
    )
    data = data if isinstance(data, dict) else {}
    seen = SpendState.from_rpc(data.get("state"), user_default=user_default, global_default=global_default)
    if not data.get("ok"):
        if "ok" not in data:
            raise RuntimeError("assistant_spend_reserve returned no result")
        scope = SCOPE_USER if data.get("reason") == "user_cap" else SCOPE_GLOBAL
        raise SpendRefused(scope, seen)
    hold_id = str(data.get("id") or "").strip()
    if not hold_id:
        raise RuntimeError("assistant_spend_reserve admitted a hold without an id")
    return hold_id


def settle(user_id: str, hold_id: str | None) -> None:
    """active -> settled with actual = the logged cost on the hold (0 when
    none). Fenced on status active, so a second settle is a no-op."""
    if not hold_id:
        return
    repo.close_reservation(user_id, str(hold_id), STATUS_SETTLED)


def release(user_id: str, hold_id: str | None) -> None:
    """active -> released with actual = the logged cost on the hold. Used by
    the paths where the work never ran (split-drop failures, lazy repair)."""
    if not hold_id:
        return
    repo.close_reservation(user_id, str(hold_id), STATUS_RELEASED)


def settle_external(user_id: str, hold_id: str | None, actual_usd: Decimal, *, release: bool = False) -> bool:
    """Close a hold with an actual cost measured outside llm_call_log (the
    AE chat message cost or plans_reader_runs.cost_usd), not the logged
    cost on the hold (docs/ASSISTANT_PHASE4_PLAN.md 3.7). settled, or
    released with `release` (a canceled, failed or timed-out page read or
    run still spent its actual, which the 0180 state counts either way).
    Fenced on status active: False when the hold is already closed or not
    this person's, so a second close is a no-op. A negative or unreadable
    actual raises ValueError before anything is written."""
    if not hold_id:
        return False
    actual = _dec(actual_usd, Decimal("-1"))
    if not actual.is_finite() or actual < _ZERO:
        raise ValueError("an external actual cost is a non-negative amount")
    status = STATUS_RELEASED if release else STATUS_SETTLED
    return bool(repo.close_reservation(user_id, str(hold_id), status, actual_usd=actual))
