"""Cadence and RFC 5545 rule expansion for assistant routines
(docs/ASSISTANT_PHASE3_PLAN.md 3.1).

The model never writes a rule. `schedule_create` passes a structured
`Cadence`; the server builds the rule with `to_rrule` and the title from
enums (deviation D3). Every rule is expanded in America/Los_Angeles wall
time and every stored or returned instant is UTC:

  * a wall time inside the spring-forward gap runs one hour later on the
    wall clock (02:30 on 2027-03-14 is 10:30Z), and a repeated wall time
    in the fall-back hour runs once, at its first (PDT) instant (01:30 on
    2026-11-01 is 08:30Z);
  * weekday rules (`weekday_only`) skip the G3 holiday list; other rules
    run on holidays;
  * `to_rrule` writes exactly one BYMINUTE and never BYSECOND (dtstart is
    floored to the minute), so the 0170 caps trigger's 1 hour reasoning
    holds; `from_rrule` refuses every part `to_rrule` never writes,
    BYSECOND included.

Pure: python-dateutil and the standard library only, nothing from `app`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

PT = ZoneInfo("America/Los_Angeles")
FREQUENCIES = ("hourly", "daily", "weekdays", "weekly", "monthly")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR")
ALL_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
# A rule with no occurrence inside this many days (a holiday list covering
# every slot) cannot be scheduled.
SEARCH_HORIZON_DAYS = 400
# docs/ASSISTANT.md:131; settings.assistant_schedule_catchup_hours overrides.
CATCHUP_SECONDS = 3 * 3600

# The longest hourly window, end hour minus start hour.
MAX_HOURLY_WINDOW = 18
MAX_WEEKLY_INTERVAL = 4
MAX_DAY_OF_MONTH = 28

CADENCE_KEYS = frozenset({
    "frequency", "hour", "minute", "days", "day_of_month", "interval",
    "window_start_hour", "window_end_hour",
})

DAY_NAMES = {
    "MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday",
    "FR": "Friday", "SA": "Saturday", "SU": "Sunday",
}

# The rule parts to_rrule writes, per frequency (FREQ always). Anything
# else in a stored rule is refused by from_rrule.
_RULE_PARTS = {
    "DAILY": ({"BYHOUR", "BYMINUTE"}, set()),
    "MONTHLY": ({"BYMONTHDAY", "BYHOUR", "BYMINUTE"}, set()),
    "WEEKLY": ({"BYDAY", "BYHOUR", "BYMINUTE"}, {"INTERVAL"}),
    "HOURLY": ({"BYDAY", "BYHOUR", "BYMINUTE"}, set()),
}
_PART_RE = re.compile(r"^[A-Z]+=[A-Z0-9,]+$")
_NUMBER_RE = re.compile(r"^[0-9]{1,2}$")

NEVER_RUNS_TEXT = "This routine would never run."


@dataclass(frozen=True)
class Cadence:
    frequency: str                  # one of FREQUENCIES
    hour: int | None                # 0..23; required except hourly
    minute: int                     # 0..59
    days: tuple[str, ...]           # weekly: 1..7 of ALL_DAYS; hourly: WEEKDAYS or ALL_DAYS; else ()
    day_of_month: int | None        # monthly: 1..28
    interval: int                   # weekly 1..4; every other frequency 1
    window_start_hour: int | None   # hourly only, 0..23
    window_end_hour: int | None     # hourly only, start..23

    def as_dict(self) -> dict:
        """The cadence object the API and the tools pass (5.2 Cadence)."""
        return {
            "frequency": self.frequency,
            "hour": self.hour,
            "minute": self.minute,
            "days": list(self.days),
            "day_of_month": self.day_of_month,
            "interval": self.interval,
            "window_start_hour": self.window_start_hour,
            "window_end_hour": self.window_end_hour,
        }


class CadenceError(ValueError):
    """A cadence or rule the server refuses. `code` is "bad_cadence"; the
    message is one sentence, shown to the model and the user."""

    code = "bad_cadence"


# ── parse ────────────────────────────────────────────────────────────────────

_FIELD_TEXT = {
    "hour": "The hour",
    "minute": "The minute",
    "day_of_month": "The day of the month",
    "interval": "The number of weeks between runs",
    "window_start_hour": "The first hour of the window",
    "window_end_hour": "The last hour of the window",
}


def _int(obj: Mapping, key: str, lo: int, hi: int) -> int | None:
    value = obj.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and _NUMBER_RE.match(value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        raise CadenceError(f"{_FIELD_TEXT[key]} must be a whole number from {lo} to {hi}.")
    if not lo <= value <= hi:
        raise CadenceError(f"{_FIELD_TEXT[key]} must be from {lo} to {hi}.")
    return value


def _days(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [v for v in value.split(",") if v.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise CadenceError("Days must be a list of two-letter codes: MO, TU, WE, TH, FR, SA or SU.")
    out: list[str] = []
    for item in items:
        code = str(item).strip().upper() if isinstance(item, str) else None
        if code not in ALL_DAYS:
            raise CadenceError("Days must be two-letter codes: MO, TU, WE, TH, FR, SA or SU.")
        if code in out:
            raise CadenceError("Each day can be listed once.")
        out.append(code)
    return tuple(sorted(out, key=ALL_DAYS.index))


def parse_cadence(obj: Mapping) -> Cadence:
    """Strict parse of the cadence object the tool and the routes pass.
    Raises CadenceError for an unknown frequency, a missing hour, a minute
    out of range, days outside the set, day_of_month above 28, an interval
    out of range, or an hourly window with start after end or longer than
    18 hours. A field the frequency does not use (days on a daily rule, an
    hour on an hourly rule) is refused rather than ignored, so a routine
    never runs on a schedule the user did not ask for. A missing minute is
    0 and a missing interval is 1."""
    if isinstance(obj, Cadence):
        obj = obj.as_dict()
    if not isinstance(obj, Mapping):
        raise CadenceError("The cadence must be an object.")
    if any(k not in CADENCE_KEYS for k in obj):
        raise CadenceError("The cadence has a field BDR does not use.")
    frequency = obj.get("frequency")
    frequency = frequency.strip().lower() if isinstance(frequency, str) else ""
    if frequency not in FREQUENCIES:
        raise CadenceError("The frequency must be hourly, daily, weekdays, weekly or monthly.")

    hour = _int(obj, "hour", 0, 23)
    minute = _int(obj, "minute", 0, 59)
    minute = 0 if minute is None else minute
    days = _days(obj.get("days"))
    day_of_month = _int(obj, "day_of_month", 1, MAX_DAY_OF_MONTH)
    interval = _int(obj, "interval", 1, MAX_WEEKLY_INTERVAL if frequency == "weekly" else 1)
    interval = 1 if interval is None else interval
    window_start = _int(obj, "window_start_hour", 0, 23)
    window_end = _int(obj, "window_end_hour", 0, 23)

    if frequency != "monthly" and day_of_month is not None:
        raise CadenceError("Only a monthly routine takes a day of the month.")
    if frequency not in ("weekly", "hourly") and days:
        raise CadenceError("Only weekly and hourly routines take days.")

    if frequency == "hourly":
        if hour is not None:
            raise CadenceError("An hourly routine takes a window (its first and last hour), not an hour.")
        if window_start is None or window_end is None:
            raise CadenceError("An hourly routine needs the first and last hour of its window.")
        if window_start > window_end:
            raise CadenceError("The window must start at or before its last hour.")
        if window_end - window_start > MAX_HOURLY_WINDOW:
            raise CadenceError(f"An hourly window can be at most {MAX_HOURLY_WINDOW} hours long.")
        if set(days) == set(WEEKDAYS):
            days = WEEKDAYS
        elif set(days) == set(ALL_DAYS):
            days = ALL_DAYS
        else:
            raise CadenceError("An hourly routine runs on weekdays (MO, TU, WE, TH, FR) or on every day.")
    else:
        if window_start is not None or window_end is not None:
            raise CadenceError("Only an hourly routine takes a window.")
        if hour is None:
            raise CadenceError("The hour is required (0 to 23, Pacific time).")
        if frequency == "weekly" and not days:
            raise CadenceError("A weekly routine needs at least one day.")
        if frequency == "monthly" and day_of_month is None:
            raise CadenceError(f"A monthly routine needs a day of the month from 1 to {MAX_DAY_OF_MONTH}.")

    return Cadence(
        frequency=frequency,
        hour=hour,
        minute=minute,
        days=days,
        day_of_month=day_of_month,
        interval=interval,
        window_start_hour=window_start,
        window_end_hour=window_end,
    )


# ── rule text ────────────────────────────────────────────────────────────────


def to_rrule(c: Cadence) -> str:
    """weekdays: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=h;BYMINUTE=m.
    daily: FREQ=DAILY;BYHOUR=h;BYMINUTE=m. weekly: FREQ=WEEKLY;INTERVAL=n;
    BYDAY=...;BYHOUR=h;BYMINUTE=m. monthly: FREQ=MONTHLY;BYMONTHDAY=d;
    BYHOUR=h;BYMINUTE=m. hourly: FREQ=HOURLY;BYDAY=...;BYHOUR=s,...,e;
    BYMINUTE=m. Always one BYMINUTE value, never BYSECOND, never DTSTART.
    The cadence is re-validated first, so a hand-built Cadence cannot
    produce a rule parse_cadence would refuse."""
    c = parse_cadence(c)
    tail = f"BYHOUR={c.hour};BYMINUTE={c.minute}"
    if c.frequency == "weekdays":
        return f"FREQ=WEEKLY;BYDAY={','.join(WEEKDAYS)};{tail}"
    if c.frequency == "daily":
        return f"FREQ=DAILY;{tail}"
    if c.frequency == "weekly":
        return f"FREQ=WEEKLY;INTERVAL={c.interval};BYDAY={','.join(c.days)};{tail}"
    if c.frequency == "monthly":
        return f"FREQ=MONTHLY;BYMONTHDAY={c.day_of_month};{tail}"
    hours = ",".join(str(h) for h in range(c.window_start_hour, c.window_end_hour + 1))
    return f"FREQ=HOURLY;BYDAY={','.join(c.days)};BYHOUR={hours};BYMINUTE={c.minute}"


def _numbers(value: str) -> list[int]:
    pieces = value.split(",")
    if not all(_NUMBER_RE.match(p) for p in pieces):
        raise CadenceError("This rule is not one BDR writes.")
    return [int(p) for p in pieces]


def _one(value: str) -> int:
    numbers = _numbers(value)
    if len(numbers) != 1:
        raise CadenceError("This rule is not one BDR writes.")
    return numbers[0]


def from_rrule(text: str) -> Cadence:
    """The inverse of to_rrule. Refuses (CadenceError) any part to_rrule
    never writes: COUNT, UNTIL, DTSTART, BYSECOND, RDATE, EXDATE and the
    rest, a repeated part, and a part that does not belong to the rule's
    frequency. The message never echoes the rule text."""
    if not isinstance(text, str) or not text.strip():
        raise CadenceError("This rule is not one BDR writes.")
    parts: dict[str, str] = {}
    for chunk in text.strip().upper().split(";"):
        if not _PART_RE.match(chunk):
            raise CadenceError("This rule is not one BDR writes.")
        key, value = chunk.split("=", 1)
        if key in parts:
            raise CadenceError("This rule repeats a part.")
        parts[key] = value
    freq = parts.pop("FREQ", None)
    if freq not in _RULE_PARTS:
        raise CadenceError("This rule is not one BDR writes.")
    required, optional = _RULE_PARTS[freq]
    if not required <= set(parts) or not set(parts) <= required | optional:
        raise CadenceError("This rule uses a part BDR never writes.")

    minute = _one(parts["BYMINUTE"])
    days = parts["BYDAY"].split(",") if "BYDAY" in parts else []
    if freq == "DAILY":
        obj: dict[str, Any] = {"frequency": "daily", "hour": _one(parts["BYHOUR"]), "minute": minute}
    elif freq == "MONTHLY":
        obj = {"frequency": "monthly", "hour": _one(parts["BYHOUR"]), "minute": minute,
               "day_of_month": _one(parts["BYMONTHDAY"])}
    elif freq == "WEEKLY":
        hour = _one(parts["BYHOUR"])
        if "INTERVAL" not in parts and tuple(days) == WEEKDAYS:
            obj = {"frequency": "weekdays", "hour": hour, "minute": minute}
        else:
            obj = {"frequency": "weekly", "hour": hour, "minute": minute, "days": days,
                   "interval": _one(parts.get("INTERVAL", "1"))}
    else:
        hours = _numbers(parts["BYHOUR"])
        if hours != list(range(hours[0], hours[-1] + 1)):
            raise CadenceError("An hourly rule must run every hour of one window.")
        obj = {"frequency": "hourly", "minute": minute, "days": days,
               "window_start_hour": hours[0], "window_end_hour": hours[-1]}
    return parse_cadence(obj)


def weekday_only(c: Cadence) -> bool:
    """True for weekdays, and for weekly or hourly rules whose days are a
    subset of WEEKDAYS. Those rules skip holidays."""
    if c.frequency == "weekdays":
        return True
    if c.frequency in ("weekly", "hourly"):
        return bool(c.days) and set(c.days) <= set(WEEKDAYS)
    return False


# ── words ────────────────────────────────────────────────────────────────────


def clock_text(hour: int, minute: int) -> str:
    """7:00 AM, 12:30 PM, 12:00 AM."""
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def days_text(days: Iterable[str]) -> str:
    """Monday; Monday and Thursday; Monday, Wednesday and Friday."""
    names = [DAY_NAMES[d] for d in days]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _hourly_span(c: Cadence) -> str:
    start = clock_text(c.window_start_hour, c.minute)
    if c.window_start_hour == c.window_end_hour:
        return f"at {start}"
    return f"every hour from {start} to {clock_text(c.window_end_hour, c.minute)}"


def cadence_text(c: Cadence) -> str:
    """Server-built English, no dash characters, for example "Weekdays at
    7:00 AM Pacific, holidays skipped", "Every Monday and Thursday at 6:30
    AM Pacific, holidays skipped", "Every 2 weeks on Monday at 7:00 AM
    Pacific, holidays skipped", "On day 1 of each month at 8:00 AM
    Pacific", "Every hour from 7:00 AM to 5:00 PM Pacific on weekdays,
    holidays skipped". Every weekday_only rule says that holidays are
    skipped, since it is true of each of them."""
    c = parse_cadence(c)
    skipped = ", holidays skipped" if weekday_only(c) else ""
    if c.frequency == "weekdays":
        return f"Weekdays at {clock_text(c.hour, c.minute)} Pacific{skipped}"
    if c.frequency == "daily":
        return f"Every day at {clock_text(c.hour, c.minute)} Pacific"
    if c.frequency == "weekly":
        when = f"at {clock_text(c.hour, c.minute)} Pacific"
        if c.interval == 1:
            return f"Every {days_text(c.days)} {when}{skipped}"
        return f"Every {c.interval} weeks on {days_text(c.days)} {when}{skipped}"
    if c.frequency == "monthly":
        return f"On day {c.day_of_month} of each month at {clock_text(c.hour, c.minute)} Pacific"
    on = "on weekdays" if c.days == WEEKDAYS else "every day"
    span = _hourly_span(c)
    return f"{span[0].upper()}{span[1:]} Pacific {on}{skipped}"


def short_text(c: Cadence) -> str:
    """The lower-case phrase routine titles use, without the zone: "weekdays
    at 7:00 AM", "every Monday and Thursday at 6:30 AM", "every 2 weeks on
    Monday at 7:00 AM", "day 1 of each month at 8:00 AM", "every hour from
    7:00 AM to 5:00 PM on weekdays"."""
    c = parse_cadence(c)
    if c.frequency == "weekdays":
        return f"weekdays at {clock_text(c.hour, c.minute)}"
    if c.frequency == "daily":
        return f"every day at {clock_text(c.hour, c.minute)}"
    if c.frequency == "weekly":
        if c.interval == 1:
            return f"every {days_text(c.days)} at {clock_text(c.hour, c.minute)}"
        return f"every {c.interval} weeks on {days_text(c.days)} at {clock_text(c.hour, c.minute)}"
    if c.frequency == "monthly":
        return f"day {c.day_of_month} of each month at {clock_text(c.hour, c.minute)}"
    return f"{_hourly_span(c)} {'on weekdays' if c.days == WEEKDAYS else 'every day'}"


# ── expansion ────────────────────────────────────────────────────────────────


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _holiday_set(holidays: Iterable[Any] | None) -> frozenset[date]:
    out: set[date] = set()
    for h in holidays or ():
        if isinstance(h, datetime):
            out.add(h.date())
        elif isinstance(h, date):
            out.add(h)
        else:
            try:
                out.add(date.fromisoformat(str(h)[:10]))
            except ValueError:
                continue
    return frozenset(out)


def occurrences(rrule: str, dtstart_utc: datetime, after_utc: datetime, n: int,
                holidays: frozenset[date]) -> list[datetime]:
    """The next n aware UTC instants strictly after after_utc. Expands naive
    Pacific wall times from dtstart (floored to the minute), converts each
    with fold 0, drops instants at or before after_utc and duplicates (the
    spring-forward hour), and for weekday_only rules drops instants whose
    Pacific date is in holidays. Stops after SEARCH_HORIZON_DAYS.

    `holidays` may also hold ISO date strings (the stored list). Raises
    CadenceError for a rule from_rrule refuses."""
    if n <= 0:
        return []
    skip = _holiday_set(holidays) if weekday_only(from_rrule(rrule)) else frozenset()
    after = _utc(after_utc)
    local_start = _utc(dtstart_utc).astimezone(PT).replace(tzinfo=None, second=0, microsecond=0)
    after_local = after.astimezone(PT).replace(tzinfo=None)
    horizon = after_local + timedelta(days=SEARCH_HORIZON_DAYS)
    rule = rrulestr(rrule, dtstart=local_start)

    out: list[datetime] = []
    seen: set[datetime] = set()
    # One hour of slack: a naive wall time inside the repeated fall-back
    # hour can sort before after_local while its fold 0 instant is later.
    for wall in rule.xafter(after_local - timedelta(hours=1), inc=False):
        if wall > horizon:
            break
        instant = wall.replace(tzinfo=PT).astimezone(timezone.utc)
        if instant <= after or instant in seen:
            continue
        seen.add(instant)
        if skip and instant.astimezone(PT).date() in skip:
            continue
        out.append(instant)
        if len(out) >= n:
            break
    return out


def next_runs(rrule: str, dtstart_utc: datetime, now_utc: datetime, holidays: frozenset[date],
              n: int = 3) -> list[datetime]:
    """occurrences(..., after_utc=now_utc - 1 second, n). An empty result
    raises CadenceError("This routine would never run.")."""
    runs = occurrences(rrule, dtstart_utc, _utc(now_utc) - timedelta(seconds=1), n, holidays)
    if not runs:
        raise CadenceError(NEVER_RUNS_TEXT)
    return runs
