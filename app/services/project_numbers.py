"""App-assigned project numbers (docs/RFP_CREATE.md section 2, migration 0130).

Numbers take the `YY.M.NNNN` form the company already uses, with an optional
trailing `B` marking a budgetary bid: two-digit year, month with no leading
zero, a four-digit counter, dots between (`26.9.7204`, `26.9.7204B`). The
counter is one global row (`project_number_counter`) advanced by the
`next_project_number()` RPC, whose single UPDATE ... RETURNING serializes
concurrent creators, and wraps 9999 -> 0001. The year and month are the
Pacific-time calendar at assignment.

Existing numbers are never rewritten. The format rule applies only to numbers
this module assigns and to the Budgetary toggle (`with_budgetary`), which
refuses a legacy number it cannot parse. Nothing bumps the counter from a
typed number (nothing types one anymore): a PM-created or legacy number that
already holds the next value shows up as a unique violation on the
`projects` insert, and `insert_with_assigned_number` simply asks again.

Pure except where `sb` is passed. No RFP dependency: the New Project form
uses this whether or not RFP ingestion is switched on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NUMBER_RE = re.compile(r"^(\d{2})\.(\d{1,2})\.(\d{4})(B?)$")

# How many consecutive counter values a creator may find already taken before
# giving up. A module constant, not a setting: the value only matters when
# the counter has been leapfrogged by hand-typed PM numbers.
NUMBER_MAX_TRIES = 20

# The office calendar. Mirrors settings.display_timezone (and the frontend's
# COMPANY_TZ); pinned here so the number a project gets never depends on a
# per-deployment setting.
COMPANY_TZ = ZoneInfo("America/Los_Angeles")

COUNTER_MAX = 9999

LEGACY_NUMBER_MESSAGE = (
    "This project's number predates automatic numbering and cannot be changed"
)
NO_FREE_NUMBER_MESSAGE = "No free project number could be assigned; try again"


class LegacyNumberError(ValueError):
    """The Budgetary toggle was asked to rename a number outside the format."""

    def __init__(self, number: str):
        super().__init__(LEGACY_NUMBER_MESSAGE)
        self.number = number


class NoFreeNumberError(RuntimeError):
    """NUMBER_MAX_TRIES consecutive counter values were already in use."""

    def __init__(self, tries: int):
        super().__init__(NO_FREE_NUMBER_MESSAGE)
        self.tries = tries


@dataclass(frozen=True)
class ParsedNumber:
    yy: int
    month: int
    counter: int
    budgetary: bool


def parse(number: str | None) -> ParsedNumber | None:
    """The parts of a conforming number, or None for a legacy / nonconforming
    one. Whitespace is trimmed first (one production row carries a trailing
    space; the 0052 unique index btrims the same way)."""
    if number is None:
        return None
    m = NUMBER_RE.match(str(number).strip())
    if not m:
        return None
    yy, month, counter, flag = m.groups()
    return ParsedNumber(
        yy=int(yy), month=int(month), counter=int(counter), budgetary=flag == "B"
    )


def is_valid(number: str | None) -> bool:
    return parse(number) is not None


def format_number(yy: int, month: int, counter: int, *, budgetary: bool) -> str:
    """`YY.M.NNNN` plus `B` when budgetary: the year two digits, the month
    without a leading zero, the counter zero-padded to four."""
    return f"{yy % 100:02d}.{int(month)}.{int(counter):04d}{'B' if budgetary else ''}"


def prefix_for(now_utc: datetime) -> tuple[int, int]:
    """(year % 100, month) of `now_utc` on the office calendar (Pacific time).
    A naive datetime is read as UTC."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(COMPANY_TZ)
    return local.year % 100, local.month


def next_after(last: int) -> int:
    """The counter value that follows `last`, wrapping 9999 -> 1."""
    return 1 if last >= COUNTER_MAX else last + 1


def with_budgetary(number: str, budgetary: bool) -> str:
    """The same number with the `B` added or stripped. A number outside the
    format cannot be renamed and raises LegacyNumberError."""
    parsed = parse(number)
    if parsed is None:
        raise LegacyNumberError(number)
    return format_number(parsed.yy, parsed.month, parsed.counter, budgetary=budgetary)


# ── Database-backed ─────────────────────────────────────────────────────────


def preview(sb) -> str:
    """The number the next creator would get, for the form's "assigned on
    save" line. Reads the counter row and formats last + 1 (wrap-aware);
    NEVER advances the counter, so two previews in a row agree. An absent
    counter row (a database without 0130) previews 0001."""
    rows = (
        sb.table("project_number_counter").select("last").eq("id", 1).limit(1).execute()
    ).data or []
    last = int(rows[0].get("last") or 0) if rows else 0
    yy, month = prefix_for(datetime.now(timezone.utc))
    return format_number(yy, month, next_after(last), budgetary=False)


def next_counter(sb) -> int:
    """Advance the counter and return the value it landed on. The RPC returns
    a bare integer; PostgREST may wrap it in a one-element list depending on
    the client version, so both shapes are accepted."""
    data = sb.rpc("next_project_number").execute().data
    if isinstance(data, list):
        if not data:
            raise RuntimeError("next_project_number returned no value")
        data = data[0]
    if isinstance(data, dict):
        # A `returns table` shape would come back as {"next_project_number": n}.
        data = next(iter(data.values()))
    return int(data)


def assign(sb, *, budgetary: bool) -> str:
    """Take the next counter value and format it with today's Pacific prefix.
    The counter is spent whether or not the caller's insert succeeds; that is
    by design (it is never rewound), and the caller retries on a collision."""
    counter = next_counter(sb)
    yy, month = prefix_for(datetime.now(timezone.utc))
    return format_number(yy, month, counter, budgetary=budgetary)


def is_duplicate_number(exc: Exception) -> bool:
    """A PostgREST 23505 on the projects.number unique index (migration 0052).
    Anything else, including a 23505 on another table, is not a number clash."""
    msg = str(exc)
    return "projects_number_unique_idx" in msg or ("23505" in msg and "number" in msg)


def insert_with_assigned_number(sb, payload: dict, *, budgetary: bool) -> dict:
    """Insert a `projects` row with a freshly assigned number and return it.

    Assigns, inserts, and on a unique violation on the number index (a
    PM-created or legacy number already holds that value) assigns again, at
    most NUMBER_MAX_TRIES times; then raises NoFreeNumberError. Any other
    insert failure propagates unchanged. `payload` is mutated: its `number`
    is the one the row was inserted with.
    """
    last_exc: Exception | None = None
    for _ in range(NUMBER_MAX_TRIES):
        payload["number"] = assign(sb, budgetary=budgetary)
        try:
            rows = sb.table("projects").insert(payload).execute().data or []
        except Exception as exc:  # noqa: BLE001, a number clash is retried, the rest propagate
            if not is_duplicate_number(exc):
                raise
            last_exc = exc
            continue
        if not rows:
            raise RuntimeError("projects insert returned no row")
        return rows[0]
    raise NoFreeNumberError(NUMBER_MAX_TRIES) from last_exc
