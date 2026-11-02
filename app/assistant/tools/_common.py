"""Helpers shared by the tool modules: text caps, Pacific time rendering,
id checks and the output shapes the trimmers build, plus (phase 1) the value
normalization, argument parsing and card formatting of the write tools.
Pure functions only. Phase 4 adds the bid-amount strip helper and the
Pacific date window helper."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")

# Per-field text caps (characters) for human-authored text in concise and
# detailed outputs. The executor applies the whole-result cap on top.
CONCISE_TEXT = 240
DETAILED_TEXT = 1200

_WS = re.compile(r"\s+")


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def clip(text: Any, limit: int) -> str | None:
    """Whitespace-collapsed text cut at `limit` with a marker; None stays
    None so an empty field does not look like a value."""
    if text is None:
        return None
    s = _WS.sub(" ", str(text)).strip()
    if not s:
        return None
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 6)].rstrip() + " [cut]"


def text_cap(response_format: str) -> int:
    return DETAILED_TEXT if response_format == "detailed" else CONCISE_TEXT


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=PT)


def pt(value: Any) -> str | None:
    """An ISO timestamp rendered in Pacific time as "2026-10-21 02:00 PM PT";
    None when missing or unparseable."""
    dt = parse_ts(value)
    if dt is None:
        return None
    local = dt.astimezone(PT)
    return local.strftime("%Y-%m-%d %I:%M %p").replace(" 0", " ") + " PT"


def pt_date(value: Any) -> str | None:
    dt = parse_ts(value)
    return dt.astimezone(PT).strftime("%Y-%m-%d") if dt else None


def rows_of(payload: Any) -> list[dict]:
    """A list payload, or [] for anything else (a 404 body, None)."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


def dict_of(payload: Any) -> dict:
    return payload if isinstance(payload, dict) else {}


def compact(mapping: Mapping[str, Any]) -> dict:
    """Drop None values and empty lists so concise output stays short."""
    out: dict = {}
    for k, v in mapping.items():
        if v is None:
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        out[k] = v
    return out


def narrowing_hint(total: int, shown: int, how: str) -> str | None:
    if total > shown:
        return f"{shown} of {total} shown. {how}"
    return None


class ToolArgumentError(ValueError):
    """A tool call whose arguments cannot build a request (a missing or
    malformed id). The executor answers the model with the message and
    records a failed step; nothing is sent."""


def require_uuid(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not is_uuid(value):
        raise ToolArgumentError(
            f"{key} must be a project id from app_search or whats_due (the short id or the full id)."
        )
    return str(value)


# ── Phase 1: values for the write tools (docs/ASSISTANT_PHASE1_PLAN.md 3.7) ──
#
# Targets, gates and payloads are hashed over canonical JSON, which refuses
# floats and Decimals (app/assistant/approvals.py canonical_json). norm_value
# turns every number into a string so a row PostgREST returns with numeric
# columns as floats and ints hashes the same way every time it is read.

UTC = timezone.utc

MONEY_FIELDS = frozenset({"labor_amount", "amount"})
DATETIME_FIELDS = frozenset({
    "internal_bid_at", "actual_bid_at", "invitation_at", "job_walk_at", "due_from_estimator_at",
    "due_from_vendors_at", "updated_at", "completed_at", "created_at", "due_at",
})
DATE_FIELDS = frozenset({"est_start_date", "est_finish_date", "due_date", "needs_by"})

_MONEY_RE = re.compile(r"^\d{1,12}(\.\d{1,2})?$")
_LOCAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
# The norm_value form of a datetime: what a revert sends back unchanged.
_EXACT_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?\+00:00$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CENT = Decimal("0.01")


def _number_text(value: Any) -> str:
    """str(int) for an integral number, else the fixed-point string of
    Decimal(str(x)).normalize(). Never a float or a Decimal."""
    d = Decimal(str(value))
    if not d.is_finite():
        return str(value)
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


def money_text(value: Any) -> str | None:
    """A two-decimal string ("12500.00"), or None when `value` is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return str(d.quantize(_CENT, rounding=ROUND_HALF_UP))


def _utc_text(dt: datetime) -> str:
    """YYYY-MM-DDTHH:MM:SS+00:00, with microseconds only when non-zero."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="auto")


def _parse_instant(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def norm_value(field: str, v: Any) -> Any:
    """One value in the form targets, gates and payloads carry: datetimes to
    UTC ISO, money to two-decimal strings, every other number to a string,
    dicts and lists element by element (a dict's keys name its values'
    fields). Never returns a Decimal or a float; everything else unchanged."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, Mapping):
        return {str(k): norm_value(str(k), x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [norm_value(field, x) for x in v]
    if field in MONEY_FIELDS:
        money = money_text(v)
        if money is not None:
            return money
    if isinstance(v, datetime):
        return _utc_text(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        if field in DATETIME_FIELDS:
            dt = _parse_instant(v)
            return _utc_text(dt) if dt is not None else v
        return v
    if isinstance(v, (int, float, Decimal)):
        return _number_text(v)
    return v


def parse_money(value: Any, field: str) -> str:
    """A money argument: digits with up to two decimals (a leading $, commas
    and spaces are dropped first), normalized to two decimals."""
    text = re.sub(r"[\s,$]", "", str(value))
    if not _MONEY_RE.match(text):
        raise ToolArgumentError(
            f"{field} must be dollars as digits with up to two decimals, for example 12500 or 12500.50."
        )
    return money_text(text) or "0.00"


def parse_local_datetime(value: Any, field: str, *, allow_exact: bool = False) -> str:
    """A Pacific local wall time "YYYY-MM-DDTHH:MM" as UTC ISO. A local time
    that does not exist (the spring DST gap) moves forward one hour; an
    ambiguous one (the fall repeat) takes the first, daylight, instant
    (fold=0). With `allow_exact` (reverts only) the norm_value UTC form is
    accepted and returned unchanged, so a revert restores the stored instant
    exactly."""
    text = str(value or "").strip()
    if allow_exact and _EXACT_UTC_RE.match(text):
        return text
    if not _LOCAL_RE.match(text):
        raise ToolArgumentError(f"{field} must be Pacific local time as YYYY-MM-DDTHH:MM, for example 2026-10-22T14:00.")
    try:
        naive = datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ToolArgumentError(f"{field} is not a real date and time: {text}.") from None
    # fold=0 on a nonexistent time uses the offset before the transition
    # (PST), which lands one hour later on the wall clock (PDT).
    return _utc_text(naive.replace(tzinfo=PT, fold=0))


def parse_date(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _DATE_RE.match(text):
        raise ToolArgumentError(f"{field} must be a date as YYYY-MM-DD.")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ToolArgumentError(f"{field} is not a real date: {text}.") from None


def display(field: str, value: Any) -> str:
    """A value as an approval card shows it: money "$12,500.00", datetimes
    "2026-10-22 2:00 PM PT", dates as is, booleans Yes or No, null Empty."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "Empty"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if field in MONEY_FIELDS:
        money = money_text(value)
        if money is not None:
            return f"${Decimal(money):,.2f}"
    if field in DATETIME_FIELDS:
        return pt(value) or str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(display(field, v) for v in value) or "Empty"
    return str(value)


# ── Phase 4: bid amounts and Pacific date windows ────────────────────────────
# (docs/ASSISTANT_PHASE4_PLAN.md 3.18, 3.8)

# Keys that carry our bid amount, its parts or the date it went out. The
# model sees them only when the user's role is in ACTUAL_BID_VIEWER_ROLES.
# `value_trend` is /analytics/overview's per-bucket sum of bid_price, the
# *_markup_amount keys and `markups` carry markup dollars. A vendor quote's
# `amount` is not here (owner question 17).
BID_AMOUNT_KEYS = frozenset({
    "bid_price", "total_bid_amount", "total_amount", "our_amount", "our_min", "winning_amount",
    "material_amount", "gear_amount", "underground_amount", "low_voltage_amount", "labor_amount",
    "markup_amount", "materials_amount", "labor_markup_amount", "materials_markup_amount",
    "gear_markup_amount", "underground_markup_amount", "low_voltage_markup_amount", "markups",
    "value_trend", "original_contract_value", "approved_change_total", "current_contract_value",
    "sov_total", "balance_to_finish", "difference", "difference_pct", "actual_bid_at",
})


def _amount_viewer(role: Any) -> bool:
    from app.core.roles import ACTUAL_BID_VIEWER_ROLES, Role

    try:
        return Role(getattr(role, "value", role)) in ACTUAL_BID_VIEWER_ROLES
    except ValueError:
        return False


def _strip(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _strip(v) for k, v in value.items() if k not in BID_AMOUNT_KEYS}
    if isinstance(value, (list, tuple)):
        return [_strip(v) for v in value]
    return value


def strip_bid_amounts(obj: Any, role: Any) -> Any:
    """`obj` without any BID_AMOUNT_KEYS key at any depth (a deep copy of
    dicts and lists) when `role` is not in ACTUAL_BID_VIEWER_ROLES; a role
    that is unknown or None counts as a non-viewer. A viewer gets `obj`
    back unchanged. Every phase 4 trimmer that can see such keys calls it
    after its own whitelist."""
    if _amount_viewer(role):
        return obj
    return _strip(obj)


def _pt_day(value: Any, field: str) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.astimezone(PT).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(parse_date(value, field))


def pt_date_range(
    since: Any = None,
    until: Any = None,
    *,
    default_days: int = 90,
    max_days: int = 400,
    today: date | None = None,
) -> tuple[str, str]:
    """A Pacific date window ("YYYY-MM-DD", "YYYY-MM-DD"), `until` inclusive.

    `until` defaults to today in Pacific time, `since` to `default_days`
    before `until`. Raises ToolArgumentError on a malformed date, on `since`
    after `until`, or on a window longer than `max_days` days. The evidence
    routes apply the same defaults and caps (ASSISTANT_EVIDENCE_DEFAULT_DAYS
    90, ASSISTANT_EVIDENCE_MAX_DAYS 400); tools use this to fill a card's
    "Window" row and to send explicit dates."""
    end = _pt_day(until, "until") or today or datetime.now(PT).date()
    start = _pt_day(since, "since") or (end - timedelta(days=max(0, int(default_days))))
    if start > end:
        raise ToolArgumentError("since must be on or before until.")
    if (end - start).days > int(max_days):
        raise ToolArgumentError(f"The window is at most {int(max_days)} days; narrow since or until.")
    return start.isoformat(), end.isoformat()


def pt_range_label(since: str | None, until: str | None) -> str:
    """"2026-07-23 to 2026-10-21 (Pacific)" for a card row; a missing end
    reads "start" or "today"."""
    return f"{since or 'start'} to {until or 'today'} (Pacific)"
