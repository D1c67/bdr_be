"""Daily spend cap for AI Estimating (docs/PLANS_READER.md section 6, 0152).

AI Estimating (the plans reader) sends whole plan sets to a paid OpenAI model,
so one bad loop or an oversized upload could burn real money before anyone
notices. The owner set a daily cap ($75 by default, in `ai_spend_settings`)
that IT Admins and Executives can raise or reset from Settings, and both roles
hear about it (bell + email) the first time a window hits it.

What counts as spend: the sum of `plans_reader_calls.cost_usd` since the
window start. The window start is the LATER of today's midnight in
America/Los_Angeles (the company day, see datetime_format / COMPANY_TZ in the
frontend) and `cap_window_started_at`, which a reset moves to now. So a reset
gives a fresh allowance for the rest of the day and the next Pacific midnight
starts a new window on its own.

Contract with the plans reader (services/plans_reader_agents.py and
services/plans_reader_chat.py):

- call `check_cap(sb, estimated_usd=...)` BEFORE every model call. It raises
  `SpendCapReached` (a RuntimeError with a message safe to show a user) when
  the window's spend plus the estimate reaches the cap. The caller must not
  make the call, and should stop the run or answer the chat with the message.
- insert the `plans_reader_calls` row (with `cost_usd`) itself, then call
  `after_call(sb)`. That re-evaluates the cap and sends the once-per-window
  cap-hit notification when the call just tipped it over.
- write that row with the billed cost for EVERY call that reached OpenAI,
  failed ones included (a timeout after the request was accepted, a schema
  error on the response, a non-2xx with usage). Spend is only what
  `plans_reader_calls.cost_usd` says: a missing row or a NULL cost counts as
  zero, so a call that billed but left no row is invisible to the cap. Use
  the response usage when there is one, else the pre-call estimate.

The cap is soft by whatever calls are already in flight: parallel group jobs
can each pass `check_cap` before any of them records its cost. Passing a
realistic `estimated_usd` keeps the overshoot small.

The cap-hit notice is deduped by `cap_hit_notified_at` through a conditional
UPDATE (only a row whose stamp is NULL or older than the window start is
claimed), so two workers that tip the cap at the same moment send one notice.
The same notice goes out when a step is refused because its estimate would
pass the cap (spend can sit just under the cap forever while every run is
blocked), and `status()["blocked"]` reports that state to Settings. One
notice per window covers both cases; raising the cap above the window's
spend or resetting the window re-arms it.

Notices, event rows and audit rows are bookkeeping: a failure in any of them
is logged and never turns a refusal into a generic error or a saved Settings
change into a 500.

Spend is summed by keyset paging over (created_at, id), which neither skips
nor double counts rows inserted while the sum runs. At the cap's scale (a
few hundred calls a day under a $10,000 ceiling) a window is a handful of
pages; a SQL sum function would make it one round trip if volume ever grows.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.core.roles import Role
from app.services import notifications

logger = logging.getLogger(__name__)

COMPANY_TZ = ZoneInfo("America/Los_Angeles")

SETTINGS_TABLE = "ai_spend_settings"
EVENTS_TABLE = "ai_spend_events"
CALLS_TABLE = "plans_reader_calls"
CAP_COLUMN = "plans_reader_daily_cap_usd"

NOTIFY_TYPE = "ai_spend.cap_hit"
NOTIFY_ROLES = (Role.IT_ADMIN, Role.EXECUTIVE)

DEFAULT_CAP_USD = Decimal("75")
MIN_CAP_USD = Decimal("1")
MAX_CAP_USD = Decimal("10000")

_CENT = Decimal("0.01")
_SPEND_PLACES = Decimal("0.0001")
_PAGE = 1000
_MAX_PAGES = 100

_SETTINGS_COLS = (
    f"id, {CAP_COLUMN}, cap_window_started_at, cap_hit_notified_at, updated_by, updated_at"
)


class SpendCapReached(RuntimeError):
    """The AI Estimating daily spend cap is reached; the model call must not
    happen. `str(exc)` is app-authored and safe to show a user."""

    def __init__(
        self,
        message: str,
        *,
        cap_usd: Decimal,
        spent_usd: Decimal,
        estimated_usd: Decimal = Decimal("0"),
    ) -> None:
        super().__init__(message)
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd
        self.estimated_usd = estimated_usd


# ── Small helpers ───────────────────────────────────────────────────────────


def _now() -> datetime:
    """The current instant in UTC (one seam for tests)."""
    return datetime.now(UTC)


def _parse_dt(value: Any) -> datetime | None:
    """A timestamptz as PostgREST returns it ('Z' or offset), or a datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _iso(dt: datetime | None) -> str | None:
    """ISO 8601 in UTC for JSON and row writes, None passes through."""
    return dt.astimezone(UTC).isoformat() if dt else None


def _pg_ts(dt: datetime) -> str:
    """Z-suffixed UTC literal for a PostgREST filter: no '+' (which URL-decodes
    to a space) and no commas (which delimit or=() conditions)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dec(value: Any) -> Decimal:
    """A money value from a row or a caller as Decimal; junk, NULL, NaN and
    infinity count as zero so a bad row never breaks the sum."""
    if value is None or value == "":
        return Decimal("0")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return out if out.is_finite() else Decimal("0")


def _money(value: Decimal) -> str:
    """$1,234.50 style, ASCII only."""
    return f"${value.quantize(_CENT):,.2f}"


def cap_of(row: dict) -> Decimal:
    """The daily cap in dollars from a settings row ($75 when unset)."""
    cap = row.get(CAP_COLUMN)
    return _dec(cap) if cap is not None else DEFAULT_CAP_USD


# ── Window and spend ────────────────────────────────────────────────────────


def pacific_day_start(now: datetime) -> datetime:
    """Midnight of `now`'s calendar day in America/Los_Angeles, as UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    day = now.astimezone(COMPANY_TZ).date()
    return datetime.combine(day, time.min, tzinfo=COMPANY_TZ).astimezone(UTC)


def window_start(now: datetime, settings_row: dict | None) -> datetime:
    """The later of today's Pacific midnight and the last reset."""
    day_start = pacific_day_start(now)
    reset_at = _parse_dt((settings_row or {}).get("cap_window_started_at"))
    if reset_at and reset_at > day_start:
        return reset_at.astimezone(UTC)
    return day_start


def get_settings_row(sb) -> dict:
    """The single settings row, created with the column defaults if missing
    (0152 seeds it, but a wiped table must not break every run)."""
    rows = (
        sb.table(SETTINGS_TABLE).select(_SETTINGS_COLS).eq("id", 1).limit(1).execute()
    ).data or []
    if rows:
        return rows[0]
    sb.table(SETTINGS_TABLE).upsert({"id": 1}, ignore_duplicates=True).execute()
    rows = (
        sb.table(SETTINGS_TABLE).select(_SETTINGS_COLS).eq("id", 1).limit(1).execute()
    ).data or []
    if rows:
        return rows[0]
    # The write was swallowed somewhere: carry on with the defaults rather
    # than fail every AI Estimating call on a settings read.
    logger.warning("ai_spend_settings row missing after insert; using defaults")
    return {
        "id": 1,
        CAP_COLUMN: str(DEFAULT_CAP_USD),
        "cap_window_started_at": None,
        "cap_hit_notified_at": None,
        "updated_by": None,
        "updated_at": None,
    }


def _spent_since(sb, start: datetime) -> Decimal:
    """Sum cost_usd of calls created at or after `start`.

    Keyset paging on (created_at, id): each page asks for rows strictly
    after the last one seen, so rows inserted mid-sum (always at the end of
    the created_at order) are neither skipped nor counted twice, which
    offset paging cannot promise."""
    total = Decimal("0")
    since = _pg_ts(start)
    cursor: tuple[str, str] | None = None
    for _ in range(_MAX_PAGES):
        query = (
            sb.table(CALLS_TABLE)
            .select("id, created_at, cost_usd")
            .gte("created_at", since)
        )
        if cursor is not None:
            at, last_id = cursor
            query = query.or_(f"created_at.gt.{at},and(created_at.eq.{at},id.gt.{last_id})")
        chunk = (query.order("created_at").order("id").limit(_PAGE).execute()).data or []
        for row in chunk:
            total += _dec(row.get("cost_usd"))
        if len(chunk) < _PAGE:
            return total
        last = chunk[-1]
        last_at = _parse_dt(last.get("created_at"))
        if last_at is None or last.get("id") is None:
            logger.error("ai_spend: call row without created_at or id; sum stops here")
            return total
        cursor = (_pg_ts(last_at), str(last["id"]))
    logger.error(
        "ai_spend: more than %d calls in one window; sum truncated, the cap under-counts",
        _PAGE * _MAX_PAGES,
    )
    return total


def spent_usd(sb, now: datetime | None = None, *, settings_row: dict | None = None) -> Decimal:
    """Sum of plans_reader_calls.cost_usd since the current window start."""
    now = now or _now()
    row = settings_row if settings_row is not None else get_settings_row(sb)
    return _spent_since(sb, window_start(now, row))


# ── Status ──────────────────────────────────────────────────────────────────


def _evaluate(sb, now: datetime | None = None) -> tuple[dict, datetime, Decimal, Decimal]:
    now = now or _now()
    row = get_settings_row(sb)
    start = window_start(now, row)
    return row, start, cap_of(row), _spent_since(sb, start)


def status(sb, now: datetime | None = None) -> dict:
    """The JSON-ready payload GET /ai-spend returns (money as floats, times
    as ISO strings). `cap_hit_notified_at` is the raw stamp, which may belong
    to an earlier window; `cap_hit` is the live verdict (spent >= cap).
    `blocked` is also true when a step was refused this window because its
    estimate would pass the cap, until the cap is raised above the window's
    spend or the window is reset."""
    row, start, cap, spent = _evaluate(sb, now)
    remaining = max(cap - spent, Decimal("0"))
    notified = _parse_dt(row.get("cap_hit_notified_at"))
    cap_hit = spent >= cap
    return {
        "cap_usd": float(cap.quantize(_CENT)),
        "spent_usd": float(spent.quantize(_SPEND_PLACES)),
        "remaining_usd": float(remaining.quantize(_SPEND_PLACES)),
        "window_started_at": _iso(start),
        "cap_hit": cap_hit,
        "blocked": cap_hit or bool(notified and notified >= start),
        "cap_hit_notified_at": _iso(notified),
        "updated_at": _iso(_parse_dt(row.get("updated_at"))),
        "updated_by": row.get("updated_by"),
    }


# ── The gate before every model call ────────────────────────────────────────


def check_cap(sb, *, estimated_usd: Decimal | float | int = 0, now: datetime | None = None) -> Decimal:
    """Raise SpendCapReached when spend plus `estimated_usd` reaches the cap.

    Called by the plans reader before EVERY model call (see the module
    docstring). Returns the dollars left in the window when the call may go
    ahead. When the cap itself is already reached, this also makes sure the
    once-per-window notice went out (a cap lowered below today's spend is
    reached without any call tipping it).
    """
    row, start, cap, spent = _evaluate(sb, now)
    estimate = max(_dec(estimated_usd), Decimal("0"))
    if spent >= cap:
        exc = SpendCapReached(
            _reached_message(cap), cap_usd=cap, spent_usd=spent, estimated_usd=estimate
        )
    elif spent + estimate >= cap:
        if estimate >= cap:
            message = (
                f"This step needs about {_money(estimate)}, more than AI Estimating's "
                f"whole daily spend cap of {_money(cap)}. Raise the cap above that in "
                "Settings to continue."
            )
        else:
            message = (
                f"This step would take AI Estimating past its daily spend cap of "
                f"{_money(cap)} ({_money(cap - spent)} left today). Raise or reset "
                "the cap in Settings to continue."
            )
        exc = SpendCapReached(message, cap_usd=cap, spent_usd=spent, estimated_usd=estimate)
    else:
        return cap - spent
    # Every refusal makes sure IT Admins and Executives heard, once per
    # window: a cap that refuses each step's estimate while spend sits just
    # under it pauses AI Estimating as surely as a reached one.
    try:
        _notify_once(
            sb, row=row, start=start, cap=cap, spent=spent, now=now,
            estimated_usd=estimate if spent < cap else None,
        )
    except Exception:  # noqa: BLE001 - the refusal must reach the caller as SpendCapReached
        logger.exception("ai_spend cap notice failed during check_cap")
    raise exc


def after_call(sb, now: datetime | None = None) -> None:
    """Re-evaluate the cap after a call's cost is recorded; notify IT Admins
    and Executives once per window when it is reached. Never raises: the
    call already happened and its result must still be stored."""
    try:
        row, start, cap, spent = _evaluate(sb, now)
        if spent >= cap:
            _notify_once(sb, row=row, start=start, cap=cap, spent=spent, now=now)
    except Exception:  # noqa: BLE001 - bookkeeping must never fail the caller
        logger.exception("ai_spend.after_call failed")


# docs/PLANS_READER.md section 6 names this step `record()`; the call row
# itself is written by plans_reader_agents, so the name is an alias.
record = after_call


def _reached_message(cap: Decimal) -> str:
    """The refusal and notice text once the window's spend reached the cap."""
    return (
        f"AI Estimating reached its daily spend cap of {_money(cap)}. "
        "Runs are paused until the cap is raised or reset in Settings."
    )


def _notify_once(
    sb,
    *,
    row: dict,
    start: datetime,
    cap: Decimal,
    spent: Decimal,
    now: datetime | None = None,
    estimated_usd: Decimal | None = None,
) -> bool:
    """Claim this window's notice and send it. Returns True when this caller
    claimed it, False when it was already sent for the window.

    `estimated_usd` set means spend is still under the cap and a step was
    refused because its estimate would pass it; the notice says so. The
    claim UPDATE may raise (callers guard it). After a successful claim the
    notices go out first, each role on its own, and the `cap_hit` event row
    last, so a failed audit insert can never lose the emails."""
    notified = _parse_dt(row.get("cap_hit_notified_at"))
    if notified and notified >= start:
        return False
    stamp = now or _now()
    claimed = (
        sb.table(SETTINGS_TABLE)
        .update({"cap_hit_notified_at": _iso(stamp)})
        .eq("id", 1)
        .or_(f"cap_hit_notified_at.is.null,cap_hit_notified_at.lt.{_pg_ts(start)}")
        .execute()
    ).data or []
    if not claimed:
        return False  # another worker claimed it between the read and here
    cap_q, spent_q = cap.quantize(_CENT), spent.quantize(_SPEND_PLACES)
    metadata: dict[str, Any] = {"spent_usd": float(spent_q), "cap_usd": float(cap_q)}
    details: dict[str, Any] = {
        "cap_usd": str(cap_q),
        "spent_usd": str(spent_q),
        "window_started_at": _iso(start),
        "reason": "reached",
    }
    if estimated_usd is None:
        message = _reached_message(cap)
    else:
        est_q = estimated_usd.quantize(_CENT)
        metadata["estimated_usd"] = float(est_q)
        details.update(reason="estimate", estimated_usd=str(est_q))
        if estimated_usd >= cap:
            fix = "Raise the cap above that in Settings to continue."
        else:
            fix = "Raise or reset the cap in Settings to continue."
        message = (
            f"AI Estimating paused a run: its next step needs about {_money(estimated_usd)} "
            f"but only {_money(max(cap - spent, Decimal('0')))} is left of the daily spend "
            f"cap of {_money(cap)}. {fix}"
        )
    for role in NOTIFY_ROLES:
        try:
            notifications.notify_role(
                role, None, NOTIFY_TYPE, message, mirror_email=True, metadata=metadata
            )
        except Exception:  # noqa: BLE001 - one role's failure must not lose the other
            logger.exception("ai_spend cap-hit notice to %s failed", role.value)
    try:
        sb.table(EVENTS_TABLE).insert(
            {"kind": "cap_hit", "amount_usd": str(spent_q), "details": details, "actor_id": None}
        ).execute()
    except Exception:  # noqa: BLE001 - the notices already went out
        logger.exception("ai_spend cap_hit event insert failed")
    logger.warning(
        "AI Estimating spend cap notice (%s): %s of %s", details["reason"], spent_q, cap_q
    )
    return True


def _bookkeep(label: str, fn, *args, **kwargs) -> None:
    """Run one post-write step (event row, audit row, notice); log a failure
    instead of raising, because the Settings change it describes is already
    saved and the admin must see it as saved."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("ai_spend %s failed after the settings write", label)


# ── Settings writes (IT Admin / Executive) ──────────────────────────────────


def _validate_cap(cap_usd: Any) -> Decimal:
    """Parse and range-check a cap, rounded to cents. Every bad input raises
    ValueError (the range is checked before rounding, so a huge exponent
    such as 1e30 cannot reach quantize and raise InvalidOperation)."""
    try:
        cap = Decimal(str(cap_usd))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("The daily cap must be a dollar amount.") from exc
    if not cap.is_finite():
        raise ValueError("The daily cap must be a dollar amount.")
    out_of_range = ValueError(
        f"The daily cap must be between {_money(MIN_CAP_USD)} and {_money(MAX_CAP_USD)}."
    )
    if cap < MIN_CAP_USD - _CENT or cap > MAX_CAP_USD + _CENT:
        raise out_of_range
    try:
        cap = cap.quantize(_CENT)
    except InvalidOperation as exc:
        raise out_of_range from exc
    if cap < MIN_CAP_USD or cap > MAX_CAP_USD:
        raise out_of_range
    return cap


def set_cap(sb, *, cap_usd: Decimal | float | int | str, actor_id: str | None, now: datetime | None = None) -> dict:
    """Change the daily cap. Raises ValueError outside 1..10000 dollars.

    Raising the cap above what this window already spent re-arms the
    notice (a later hit of the new cap notifies again). Lowering it to or
    below today's spend reaches the cap at once and sends the notice."""
    new_cap = _validate_cap(cap_usd)
    now = now or _now()
    row = get_settings_row(sb)
    old_cap = cap_of(row)
    start = window_start(now, row)
    spent = _spent_since(sb, start)
    update: dict[str, Any] = {
        CAP_COLUMN: str(new_cap),
        "updated_by": actor_id,
        "updated_at": _iso(now),
    }
    if spent < new_cap:
        update["cap_hit_notified_at"] = None
    sb.table(SETTINGS_TABLE).update(update).eq("id", 1).execute()
    details = {
        "from_usd": str(old_cap.quantize(_CENT)),
        "to_usd": str(new_cap),
        "spent_usd": str(spent.quantize(_SPEND_PLACES)),
    }
    _bookkeep(
        "cap_changed event",
        lambda: sb.table(EVENTS_TABLE).insert(
            {"kind": "cap_changed", "amount_usd": str(new_cap), "details": details, "actor_id": actor_id}
        ).execute(),
    )
    _bookkeep(
        "cap_changed audit",
        notifications.audit, actor_id, "ai_spend.cap_changed", "ai_spend_settings", "1", details,
    )
    if spent >= new_cap:
        fresh = {**row, **update}
        _bookkeep(
            "cap_hit notice",
            _notify_once, sb, row=fresh, start=start, cap=new_cap, spent=spent, now=now,
        )
    return status(sb, now)


def reset_window(sb, *, actor_id: str | None, now: datetime | None = None) -> dict:
    """Start a fresh window now: spend counts from zero for the rest of the
    Pacific day and the cap-hit notice is re-armed."""
    now = now or _now()
    row = get_settings_row(sb)
    start = window_start(now, row)
    spent_before = _spent_since(sb, start)
    sb.table(SETTINGS_TABLE).update(
        {
            "cap_window_started_at": _iso(now),
            "cap_hit_notified_at": None,
            "updated_by": actor_id,
            "updated_at": _iso(now),
        }
    ).eq("id", 1).execute()
    details = {
        "spent_before_usd": str(spent_before.quantize(_SPEND_PLACES)),
        "cap_usd": str(cap_of(row).quantize(_CENT)),
        "previous_window_started_at": _iso(start),
    }
    _bookkeep(
        "cap_reset event",
        lambda: sb.table(EVENTS_TABLE).insert(
            {
                "kind": "cap_reset",
                "amount_usd": str(spent_before.quantize(_SPEND_PLACES)),
                "details": details,
                "actor_id": actor_id,
            }
        ).execute(),
    )
    _bookkeep(
        "cap_reset audit",
        notifications.audit, actor_id, "ai_spend.cap_reset", "ai_spend_settings", "1", details,
    )
    return status(sb, now)
