"""Bids Today (GET /projects/today) — membership, sent-today handling, and
redaction.

The page's contract: every live bid whose internal due date has arrived (office
calendar) stays listed until it goes out, however overdue; a bid that went out
earlier today stays for the rest of the day flagged sent_today. Membership is
driven by internal_bid_at ONLY — the confidential actual_bid_at must never
decide when a row appears, or non-privileged users could read the actual date
off the calendar.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.roles import Role
from app.routers import projects as pr

UTC = timezone.utc


# ── day bounds (office calendar) ───────────────────────────────────────────


def test_day_bounds_are_the_office_calendar_day():
    # Friday 2026-07-31 11:00 PDT.
    now = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
    start, end = pr._bids_today_day_bounds(now)
    assert start == datetime(2026, 7, 31, tzinfo=pr.REPORT_TZ).astimezone(UTC)
    assert end == start + timedelta(days=1)


def test_day_bounds_late_utc_evening_is_still_the_office_day():
    # 2026-08-01 02:00 UTC is still Friday 7/31 in the office.
    now = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
    start, _ = pr._bids_today_day_bounds(now)
    assert start.astimezone(pr.REPORT_TZ).date() == datetime(2026, 7, 31).date()


# ── endpoint over a filtering fake Supabase ────────────────────────────────


class _NotQ:
    def __init__(self, q):
        self._q = q

    def is_(self, col, val):
        assert val == "null"
        self._q._rows = [r for r in self._q._rows if r.get(col) is not None]
        return self._q


class _Q:
    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def neq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) != val]
        return self

    def is_(self, col, val):
        assert val == "null"
        self._rows = [r for r in self._rows if r.get(col) is None]
        return self

    @property
    def not_(self):
        return _NotQ(self)

    def lt(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) is not None and r[col] < val]
        return self

    def gte(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) is not None and r[col] >= val]
        return self

    def in_(self, col, vals):
        allowed = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def order(self, col, desc=False):
        self._rows.sort(key=lambda r: r.get(col) or "", reverse=desc)
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


class _FakeSB:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Q(self._tables.get(name, []))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _project(pid, internal, stage, **extra):
    return {
        "id": pid,
        "name": f"Project {pid}",
        "internal_bid_at": _iso(internal) if internal else None,
        "actual_bid_at": None,
        "current_stage": stage,
        "abandoned_at": None,
        **extra,
    }


@pytest.fixture()
def tables():
    # Fixtures are built relative to the real clock, offset in whole days so
    # they land on the intended side of the office-day bounds regardless of
    # when the suite runs. `now` itself is always inside today's bounds.
    now = datetime.now(UTC)
    projects = [
        _project("due-today", now, "rfqs"),
        _project("overdue", now - timedelta(days=3), "verify"),
        _project("future", now + timedelta(days=3), "rfqs"),
        _project("sent-today", now - timedelta(days=1), "submitted"),
        _project(
            "sent-today-outcome", now - timedelta(hours=1), "bid_outcome",
            bid_outcomes=[{"result": "won"}],
        ),
        _project("sent-earlier", now - timedelta(days=2), "submitted"),
        _project("abandoned", now, "rfqs", abandoned_at=_iso(now - timedelta(days=1))),
        _project("declined", now, "declined"),
        _project("pm-only", now, "pm_only"),
        # Only an actual date: must NOT appear — membership never keys on it.
        _project("actual-only", None, "rfqs", actual_bid_at=_iso(now)),
    ]
    events = [
        {"project_id": "sent-today", "to_stage": "submitted", "entered_at": _iso(now)},
        {"project_id": "sent-today-outcome", "to_stage": "submitted", "entered_at": _iso(now)},
        {"project_id": "sent-earlier", "to_stage": "submitted",
         "entered_at": _iso(now - timedelta(days=2))},
    ]
    return {"projects": projects, "stage_events": events}


@pytest.fixture()
def call(tables, monkeypatch):
    monkeypatch.setattr(pr, "get_supabase", lambda: _FakeSB(tables))
    monkeypatch.setattr(pr.workflow, "load_category_states", lambda ids: {})

    def _call(role=Role.ESTIMATING_ADMIN):
        return pr.bids_today(user=SimpleNamespace(id="u1", role=role))

    return _call


def test_membership(call):
    ids = [p["id"] for p in call()]
    assert set(ids) == {"due-today", "overdue", "sent-today", "sent-today-outcome"}


def test_most_overdue_first(call):
    ids = [p["id"] for p in call()]
    assert ids[0] == "overdue"  # ascending internal_bid_at


def test_sent_today_flag(call):
    rows = {p["id"]: p for p in call()}
    assert rows["sent-today"]["sent_today"] is True
    assert rows["sent-today"]["status"] == "sent"
    # A same-day win/loss record must not hide the row or drop the flag.
    assert rows["sent-today-outcome"]["sent_today"] is True
    assert rows["sent-today-outcome"]["status"] == "won"
    assert rows["due-today"]["sent_today"] is False
    assert rows["overdue"]["sent_today"] is False


def test_actual_bid_at_redacted_for_engineers(call, tables):
    # Give an included row a confidential actual date and check both sides.
    tables["projects"][1]["actual_bid_at"] = _iso(datetime.now(UTC))
    for p in call(Role.ESTIMATING_ENGINEER_MATERIALS):
        assert p["actual_bid_at"] is None
    rows = {p["id"]: p for p in call(Role.ESTIMATING_ADMIN)}
    assert rows["overdue"]["actual_bid_at"] is not None


def test_estimators_are_rejected(call):
    with pytest.raises(HTTPException) as exc:
        call(Role.ESTIMATOR)
    assert exc.value.status_code == 403
