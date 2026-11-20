"""The estimator's due-back date: which one is current, and how it is worded.

Two columns hold a due date. `projects.due_from_estimator_at` is the one the
team edits (project details, the hand-off modals) and the one every internal
screen and the due reminders read. `estimator_assignments.due_at` is a copy
taken when the estimator was assigned: analytics keeps it as the turnaround
benchmark for the first hand-off, and nothing rewrites it afterwards.

So the project's date is the current deadline and the assignment's copy is
only a fallback for a project that has none. Reading the copy first, as the
portal used to, froze the estimator on the date they were assigned with: a
due date moved for a revision round, or simply extended, never reached them.

Pure helpers, no I/O, so the routers, the emails and the tests share one rule.
"""

from datetime import datetime, timezone

from app.services.datetime_format import _parse_ts, format_bid_datetime

Stamp = datetime | str | None


def current_due(project_due: Stamp, assignment_due: Stamp) -> Stamp:
    """The deadline to show an estimator: the project's date when it has one,
    else the date their assignment was made with."""
    return project_due or assignment_due


def _instant(value: Stamp) -> datetime | None:
    """`value` as an aware instant, or None when it is empty or unreadable. A
    naive value is read as UTC (timestamptz columns never produce one)."""
    try:
        dt = _parse_ts(value) if value else None
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def same_instant(a: Stamp, b: Stamp) -> bool:
    """True when two stamps name the same moment (`...Z` from a request body
    and `+00:00` back from Postgres are one date, not a change)."""
    return _instant(a) == _instant(b)


def due_text(value: Stamp) -> str:
    """The due date as an estimator reads it: company (Pacific) wall time with
    the zone spelled out, 'TBD' when there is none. An unreadable value is
    shown as stored rather than dropped."""
    if not value:
        return "TBD"
    try:
        return format_bid_datetime(value)
    except (TypeError, ValueError):
        return str(value)


def upcoming_due_text(project_due: Stamp, now: datetime | None = None) -> str | None:
    """The project's due-back date in words, only while it is still ahead.

    A file update sent after the estimate came back usually has no new
    deadline: the stored date is the one already met, and printing it would
    read as a deadline that passed. So a date is stated only when it is in
    the future, which is exactly the case of a due date set for the changes.
    """
    due = _instant(project_due)
    if due is None:
        return None
    if due <= (now or datetime.now(timezone.utc)):
        return None
    return format_bid_datetime(due)
