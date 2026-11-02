"""Recipe data over a fake Supabase client (docs/ASSISTANT_PHASE3_PLAN.md
3.2.2): windows, watch state, rows per recipe, the digest stub, the email,
and the shared due_reminders.missing_quote_categories. No network."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.recipes import RecipeError
from app.services import assistant_recipes as ar
from app.services import due_reminders

PT = ar.PT
UTC = timezone.utc
EM = "\u2014"

P1 = "00000000-0000-4000-8000-000000000001"
P2 = "00000000-0000-4000-8000-000000000002"
P3 = "00000000-0000-4000-8000-000000000003"
P4 = "00000000-0000-4000-8000-000000000004"


def _pt(*a):
    return datetime(*a, tzinfo=PT).astimezone(UTC)


def _ts(value) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class _Query:
    """Enough of the PostgREST builder for the recipe reads: filters apply
    to in-memory rows (embedded selects are stored on the rows already)."""

    def __init__(self, sb, table):
        self._sb = sb
        self._table = table
        self._filters = []
        self._negate = False
        self._limit = None
        self._range = None

    def select(self, *a, **k):
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def _add(self, fn):
        negate, self._negate = self._negate, False
        self._filters.append((lambda r: not fn(r)) if negate else fn)
        return self

    def eq(self, col, value):
        return self._add(lambda r: str(r.get(col)) == str(value))

    def in_(self, col, values):
        values = {str(v) for v in values}
        return self._add(lambda r: str(r.get(col)) in values)

    def is_(self, col, value):
        assert value == "null"
        return self._add(lambda r: r.get(col) is None)

    def gte(self, col, value):
        return self._add(lambda r: r.get(col) is not None and _ts(r[col]) >= _ts(value))

    def lt(self, col, value):
        return self._add(lambda r: r.get(col) is not None and _ts(r[col]) < _ts(value))

    def order(self, *a, **k):
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        self._sb.calls.append(self._table)
        rows = [dict(r) for r in self._sb.tables.get(self._table, []) if all(f(r) for f in self._filters)]
        if self._range:
            rows = rows[self._range[0]: self._range[1] + 1]
        if self._limit is not None:
            rows = rows[: self._limit]
        return SimpleNamespace(data=rows)


class _FakeSb:
    def __init__(self, **tables):
        self.tables = tables
        self.calls = []

    def table(self, name):
        return _Query(self, name)


def _project(pid, name, due, stage="rfqs", **extra):
    return {"id": pid, "name": name, "number": f"26.10.{pid[-1]}", "internal_bid_at": due,
            "current_stage": stage, "abandoned_at": None, "failed_to_bid_at": None, **extra}


def _rfq(rid, pid, name, quoted):
    return {"id": rid, "project_id": pid,
            "material_categories": {"name": name, "pricing_section": "materials"},
            "quotes": [{"id": f"q-{rid}"}] if quoted else []}


_FULL_MARKUP = {"labor_markup_amount": 1, "materials_markup_amount": 1, "gear_markup_amount": 1,
                "underground_markup_amount": 1, "low_voltage_markup_amount": 1}


def _world():
    """Friday 2026-10-23: P1 due today (missing a Gear quote, labor in),
    P2 due Sunday (all quotes in, no labor), P3 due next Wednesday (no RFQ,
    no labor), P4 due today but submitted (never listed)."""
    rfqs = [_rfq("r1", P1, "Wiring", True), _rfq("r2", P1, "Gear", False), _rfq("r3", P2, "Wiring", True)]
    return _FakeSb(
        projects=[
            _project(P1, "Tower <One>", _pt(2026, 10, 23, 14, 0).isoformat()),
            _project(P2, "Plaza", _pt(2026, 10, 25, 9, 0).isoformat()),
            _project(P3, "Depot", _pt(2026, 10, 28, 9, 0).isoformat()),
            _project(P4, "Done", _pt(2026, 10, 23, 10, 0).isoformat(), stage="submitted"),
        ],
        rfqs=rfqs,
        quotes=[{"rfq_id": r["id"]} for r in rfqs if r["quotes"]],
        labor_reviews=[{"project_id": P1, "labor_amount": 100}],
        markups=[{"project_id": pid, **_FULL_MARKUP} for pid in (P1, P2, P3)],
        due_digest_log=[{"id": "d1", "digest_date": "2026-10-23", "user_id": "u1"}],
    )


FRIDAY = _pt(2026, 10, 23, 7, 0)
MONDAY = _pt(2026, 10, 26, 7, 0)


# ── windows ──────────────────────────────────────────────────────────────────


def test_window_due_today_is_the_pacific_day():
    start, end, label = ar.window_for("due_today", FRIDAY, None)
    assert (start, end) == (_pt(2026, 10, 23, 0, 0), _pt(2026, 10, 24, 0, 0))
    assert label == "Friday, October 23"
    # Late evening Pacific is already the next UTC day; the window is still Friday.
    late = ar.window_for("due_today", _pt(2026, 10, 23, 23, 30), None)
    assert late[:2] == (start, end)


def test_window_due_this_week_runs_to_the_end_of_sunday():
    start, end, label = ar.window_for("due_this_week", FRIDAY, None)
    assert (start, end) == (_pt(2026, 10, 23, 0, 0), _pt(2026, 10, 26, 0, 0))
    assert label == "Friday, October 23 to Sunday, October 25"
    start, end, label = ar.window_for("due_this_week", MONDAY, None)
    assert (start, end) == (_pt(2026, 10, 26, 0, 0), _pt(2026, 11, 2, 0, 0))
    assert label == "Monday, October 26 to Sunday, November 1"
    sunday = ar.window_for("due_this_week", _pt(2026, 10, 25, 7, 0), None)
    assert sunday == (_pt(2026, 10, 25, 0, 0), _pt(2026, 10, 26, 0, 0), "Sunday, October 25")


def test_window_horizon_days():
    start, end, label = ar.window_for("missing_quotes", FRIDAY, None)
    assert (start, end) == (_pt(2026, 10, 23, 0, 0), _pt(2026, 10, 30, 0, 0))
    assert label == "Friday, October 23 to Thursday, October 29"
    assert ar.window_for("labor_not_entered", FRIDAY, 1)[:2] == (_pt(2026, 10, 23, 0, 0), _pt(2026, 10, 24, 0, 0))
    # Across the fall-back change the end is still a Pacific midnight.
    _, end, _ = ar.window_for("labor_not_entered", _pt(2026, 10, 30, 7, 0), 7)
    assert end == _pt(2026, 11, 6, 0, 0) and end.astimezone(PT).hour == 0


def test_window_refuses_an_unknown_recipe():
    with pytest.raises(RecipeError):
        ar.window_for("nope", FRIDAY, None)


# ── watch state ──────────────────────────────────────────────────────────────


def test_watch_state():
    sb = _FakeSb(projects=[
        _project(P1, "Open", None),
        _project(P2, "Sent", None, stage="submitted"),
        _project(P3, "Gone quiet", None, abandoned_at="2026-10-20T00:00:00Z"),
        _project(P4, "Failed", None, failed_to_bid_at="2026-10-20T00:00:00Z"),
    ])
    assert ar.watch_state(sb, P1) == "open"
    assert ar.watch_state(sb, P2) == "closed"
    assert ar.watch_state(sb, P3) == "closed"
    assert ar.watch_state(sb, P4) == "closed"
    assert ar.watch_state(sb, "00000000-0000-4000-8000-00000000dead") == "missing"


# ── rows ─────────────────────────────────────────────────────────────────────


def _ids(payload):
    return [r["project"]["id"] for r in payload["rows"]]


def test_due_today_rows_use_the_digest_membership_and_checks():
    payload = ar.rows_for(_world(), "due_today", at_utc=FRIDAY)
    assert payload["recipe"] == "due_today" and payload["watch"] is None
    assert _ids(payload) == [P1]   # P4 is submitted
    [row] = payload["rows"]
    assert row["checks"] == {"has_quotes": False, "has_labor": True, "has_markups": True}
    assert row["severity"] == "red" and row["missing_categories"] is None
    assert payload["window"]["label"] == "Friday, October 23"
    assert _ts(payload["window"]["start"]) == _pt(2026, 10, 23, 0, 0)


def test_due_this_week_rows():
    assert _ids(ar.rows_for(_world(), "due_this_week", at_utc=FRIDAY)) == [P1, P2]


def test_missing_quotes_rows_carry_the_missing_categories():
    payload = ar.rows_for(_world(), "missing_quotes", at_utc=FRIDAY)
    assert _ids(payload) == [P1, P3]
    by_id = {r["project"]["id"]: r for r in payload["rows"]}
    assert by_id[P1]["missing_categories"] == ["Gear"]
    assert by_id[P3]["missing_categories"] is None   # no RFQ yet
    assert _ids(ar.rows_for(_world(), "missing_quotes", at_utc=FRIDAY, horizon_days=1)) == [P1]


def test_labor_not_entered_rows():
    assert _ids(ar.rows_for(_world(), "labor_not_entered", at_utc=FRIDAY)) == [P2, P3]


def test_a_watch_reads_the_one_open_project_whatever_its_date():
    sb = _world()
    far = ar.rows_for(sb, "labor_not_entered", at_utc=FRIDAY, project_id=P3, horizon_days=1)
    assert far["watch"] == {"project_id": P3, "state": "open"}
    assert _ids(far) == [P3]
    assert set(far["rows"][0]["project"]) == {"id", "name", "number", "internal_bid_at", "current_stage"}
    done = ar.rows_for(sb, "labor_not_entered", at_utc=FRIDAY, project_id=P1)
    assert done["watch"]["state"] == "open" and done["rows"] == []
    closed = ar.rows_for(sb, "missing_quotes", at_utc=FRIDAY, project_id=P4)
    assert closed["watch"] == {"project_id": P4, "state": "closed"} and closed["rows"] == []
    gone = ar.rows_for(sb, "missing_quotes", at_utc=FRIDAY, project_id="00000000-0000-4000-8000-00000000dead")
    assert gone["watch"]["state"] == "missing" and gone["rows"] == []


def test_rows_for_refuses_bad_params():
    with pytest.raises(RecipeError):
        ar.rows_for(_world(), "due_today", at_utc=FRIDAY, project_id=P1)
    with pytest.raises(RecipeError):
        ar.rows_for(_world(), "missing_quotes", at_utc=FRIDAY, horizon_days=31)


# ── digest stub ──────────────────────────────────────────────────────────────


def test_digest_sent():
    sb = _world()
    assert ar.digest_sent(sb, "u1", date(2026, 10, 23)) is True
    assert ar.digest_sent(sb, "u1", date(2026, 10, 26)) is False
    assert ar.digest_sent(sb, "u2", date(2026, 10, 23)) is False


# ── email ────────────────────────────────────────────────────────────────────


def test_render_email_escapes_names_and_links_to_routines():
    payload = ar.rows_for(_world(), "missing_quotes", at_utc=FRIDAY)
    body = ar.render_email("Bids missing vendor quotes, weekdays at 7:00 AM", payload,
                           recipient_name="Dana <Smith>", base_url="https://bdr.example.com/")
    assert "Tower &lt;One&gt; (#26.10.1)" in body and "Tower <One>" not in body
    assert "Hi Dana," in body
    assert "https://bdr.example.com/assistant?tab=routines" in body
    assert f"https://bdr.example.com/projects/{P1}" in body
    assert "Bids missing vendor quotes: 2." in body
    assert "Gear" in body and "No RFQ yet" in body
    assert EM not in body


def test_render_email_for_no_rows():
    payload = ar.rows_for(_FakeSb(), "due_today", at_utc=FRIDAY)
    body = ar.render_email("Bids due today, weekdays at 7:00 AM", payload, recipient_name="",
                           base_url="https://bdr.example.com")
    assert "No bids are due today." in body and "Hi there," in body
    assert "Due (Pacific)" not in body


# ── the shared reminders helper ──────────────────────────────────────────────


def test_missing_quote_categories():
    sb = _world()
    out = due_reminders.missing_quote_categories(sb, [P1, P2, P3])
    assert out == {P1: ["Gear"], P2: [], P3: None}
    assert due_reminders.missing_quote_categories(sb, []) == {}
    assert sb.calls.count("quotes") == 1


def test_modules_have_no_em_dash():
    from pathlib import Path

    assert EM not in Path(ar.__file__).read_text(encoding="utf-8")
