"""User-facing datetime formatting in the configured display timezone.

Lives in its own module (rather than rfq_sending, its original home) so the
due-reminder poller and pure-logic tests can format dates without dragging in
the OpenAI/Graph import stack.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def _parse_ts(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_bid_datetime(value: datetime | str | None) -> str:
    """'Wednesday, June 24th 11:00 AM' in the configured display timezone."""
    dt = _parse_ts(value)
    if dt is None:
        return "TBD"
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo(get_settings().display_timezone))
    day = dt.day
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt:%A}, {dt:%B} {day}{suffix} {hour}:{dt:%M} {ampm}"
