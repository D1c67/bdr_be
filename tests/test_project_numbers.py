"""App-assigned project numbers (services/project_numbers, docs/RFP_CREATE.md
section 2 and section 12's first bullet).

Pinned:

- format_number / parse round trip in both spellings; the month is written
  without a leading zero and the counter zero-padded to four; parse trims
  whitespace (the one production row with a trailing space) and answers None
  for a legacy or nonconforming string.
- prefix_for is the office (Pacific) calendar: Dec 31 23:30 PT, which is
  Jan 1 07:30 UTC, is still December of the old year; Jan 1 05:00 UTC likewise;
  Jan 1 08:00 UTC is the new year. A naive datetime is read as UTC.
- preview reads the counter row, formats last + 1, wraps 9999 -> 0001, and
  NEVER calls the rpc; an absent row previews 0001.
- next_counter accepts the scalar, one-element list and dict shapes the rpc
  can come back in; assign formats what the rpc returned (wrap included) and
  adds the B on request.
- with_budgetary adds and strips the marker, is idempotent, and raises
  LegacyNumberError (a ValueError) with the user-facing sentence on a legacy
  number.
- insert_with_assigned_number retries past a unique violation on the number
  index (a PM-created or legacy number holding the value), spends one counter
  value per try, gives up at NUMBER_MAX_TRIES with NoFreeNumberError and its
  sentence, and lets any other insert failure propagate on the first try.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import project_numbers as pn

# ── Fake Supabase: a counter row, an rpc, and a projects table with the 0052
# unique index simulated ─────────────────────────────────────────────────────


class _Result:
    def __init__(self, data):
        self.data = data


class _Rpc:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.rpc_calls.append((self.name, self.params))
        if self.db.rpc_raises is not None:
            raise self.db.rpc_raises
        self.db.last = pn.next_after(self.db.last)
        shape = self.db.rpc_shape
        if shape == "list":
            return _Result([self.db.last])
        if shape == "dict":
            return _Result({"next_project_number": self.db.last})
        if shape == "empty":
            return _Result([])
        return _Result(self.db.last)


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.preds = "select", None, []
        self._limit = None

    def select(self, *a, **k):
        self.db.selects.append((self.table, a[0] if a else "*"))
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def eq(self, col, val):
        self.preds.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self.table == "project_number_counter":
            rows = [] if self.db.counter_row_missing else [{"id": 1, "last": self.db.last}]
            return _Result([r for r in rows if all(r.get(c) == v for c, v in self.preds)])
        if self.table == "projects" and self.op == "insert":
            self.db.insert_attempts.append(dict(self.payload))
            if self.db.insert_raises is not None:
                raise self.db.insert_raises
            key = (self.payload.get("number") or "").strip().lower()
            if key in {(r.get("number") or "").strip().lower() for r in self.db.projects}:
                raise Exception(
                    "{'code': '23505', 'message': 'duplicate key value violates unique "
                    'constraint "projects_number_unique_idx"\'}'
                )
            row = {"id": f"p{len(self.db.projects) + 1}", **self.payload}
            self.db.projects.append(row)
            return _Result([dict(row)])
        return _Result([])


class FakeDB:
    def __init__(self, *, last=7203, projects=(), rpc_shape="scalar"):
        self.last = last
        self.projects = [dict(p) for p in projects]
        self.rpc_shape = rpc_shape
        self.rpc_raises = None
        self.rpc_calls: list = []
        self.selects: list = []
        self.insert_attempts: list = []
        self.insert_raises = None
        self.counter_row_missing = False

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params=None):
        return _Rpc(self, name, params)


@pytest.fixture()
def september(monkeypatch):
    """Pin "now" to 2026-09-16 17:00 UTC (10:00 PDT): prefix 26.9."""

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(pn, "datetime", _Now)


# ── format / parse ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "parts, text",
    [
        ((26, 9, 7204, False), "26.9.7204"),
        ((26, 9, 7204, True), "26.9.7204B"),
        ((26, 12, 1, False), "26.12.0001"),
        ((27, 1, 9999, True), "27.1.9999B"),
        ((0, 1, 1, False), "00.1.0001"),
    ],
)
def test_format_and_parse_round_trip(parts, text):
    yy, month, counter, budgetary = parts
    assert pn.format_number(yy, month, counter, budgetary=budgetary) == text
    parsed = pn.parse(text)
    assert parsed == pn.ParsedNumber(yy=yy, month=month, counter=counter, budgetary=budgetary)
    assert pn.format_number(parsed.yy, parsed.month, parsed.counter, budgetary=parsed.budgetary) == text


def test_format_takes_a_four_digit_year_down_to_two():
    assert pn.format_number(2026, 9, 7204, budgetary=False) == "26.9.7204"


def test_parse_trims_whitespace_and_accepts_a_zero_padded_month():
    assert pn.parse("26.9.7204 ") == pn.ParsedNumber(26, 9, 7204, False)
    assert pn.parse("  26.9.7204B\t") == pn.ParsedNumber(26, 9, 7204, True)
    # \d{1,2} admits "09"; the value is the month, and format writes it bare.
    assert pn.parse("26.09.7204") == pn.ParsedNumber(26, 9, 7204, False)
    assert pn.format_number(26, 9, 7204, budgetary=False) == "26.9.7204"


@pytest.mark.parametrize(
    "legacy",
    [
        "G3-2026-001",
        "",
        "   ",
        None,
        "2026.9.7204",
        "26.9.720",
        "26.9.72045",
        "26.9.7204b",
        "26.9.7204BB",
        "26-9-7204",
        "26.9.7204 B",
        "26.123.7204",
        "PM-101",
    ],
)
def test_parse_answers_none_for_legacy_and_nonconforming_strings(legacy):
    assert pn.parse(legacy) is None
    assert pn.is_valid(legacy) is False


def test_is_valid_agrees_with_parse():
    assert pn.is_valid("26.9.7204") is True
    assert pn.is_valid("26.9.7204B") is True
    assert pn.is_valid("26.9.7204 ") is True


def test_number_re_is_the_documented_pattern():
    assert pn.NUMBER_RE.pattern == r"^(\d{2})\.(\d{1,2})\.(\d{4})(B?)$"
    assert pn.NUMBER_MAX_TRIES == 20


# ── prefix_for (Pacific time) ───────────────────────────────────────────────


def test_prefix_is_still_december_late_on_new_years_eve_pacific():
    # Dec 31 23:30 PST = Jan 1 07:30 UTC.
    assert pn.prefix_for(datetime(2027, 1, 1, 7, 30, tzinfo=timezone.utc)) == (26, 12)


def test_prefix_is_still_december_at_five_utc_on_january_first():
    # Jan 1 05:00 UTC = Dec 31 21:00 PST.
    assert pn.prefix_for(datetime(2027, 1, 1, 5, 0, tzinfo=timezone.utc)) == (26, 12)


def test_prefix_rolls_at_midnight_pacific():
    # Jan 1 08:00 UTC = Jan 1 00:00 PST.
    assert pn.prefix_for(datetime(2027, 1, 1, 8, 0, tzinfo=timezone.utc)) == (27, 1)


def test_prefix_honours_daylight_time():
    # Jul 1 06:59 UTC = Jun 30 23:59 PDT (UTC-7).
    assert pn.prefix_for(datetime(2026, 7, 1, 6, 59, tzinfo=timezone.utc)) == (26, 6)
    assert pn.prefix_for(datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)) == (26, 7)


def test_prefix_reads_a_naive_datetime_as_utc():
    assert pn.prefix_for(datetime(2027, 1, 1, 7, 30)) == (26, 12)


def test_prefix_from_an_offset_aware_non_utc_datetime():
    # 2026-09-16 00:30 in New York is 21:30 the day before in Los Angeles;
    # same month, so only the conversion path is exercised here.
    from zoneinfo import ZoneInfo

    ny = datetime(2026, 10, 1, 0, 30, tzinfo=ZoneInfo("America/New_York"))
    assert pn.prefix_for(ny) == (26, 9)


# ── next_after / preview ───────────────────────────────────────────────────


def test_next_after_wraps_at_9999():
    assert pn.next_after(0) == 1
    assert pn.next_after(7203) == 7204
    assert pn.next_after(9998) == 9999
    assert pn.next_after(9999) == 1


def test_preview_formats_last_plus_one_and_never_calls_the_rpc(september):
    db = FakeDB(last=7203)
    db.rpc_raises = AssertionError("preview must not advance the counter")
    assert pn.preview(db) == "26.9.7204"
    assert pn.preview(db) == "26.9.7204"
    assert db.rpc_calls == []
    assert db.last == 7203
    assert ("project_number_counter", "last") in db.selects


def test_preview_wraps_9999_to_0001(september):
    assert pn.preview(FakeDB(last=9999)) == "26.9.0001"


def test_preview_on_an_empty_database_is_0001(september):
    assert pn.preview(FakeDB(last=0)) == "26.9.0001"
    db = FakeDB()
    db.counter_row_missing = True
    assert pn.preview(db) == "26.9.0001"


# ── next_counter / assign ──────────────────────────────────────────────────


@pytest.mark.parametrize("shape", ["scalar", "list", "dict"])
def test_next_counter_accepts_every_rpc_result_shape(shape):
    db = FakeDB(last=7203, rpc_shape=shape)
    assert pn.next_counter(db) == 7204
    assert db.rpc_calls == [("next_project_number", None)]


def test_next_counter_refuses_an_empty_result():
    db = FakeDB(last=7203, rpc_shape="empty")
    with pytest.raises(RuntimeError):
        pn.next_counter(db)


def test_assign_formats_the_rpc_value_with_todays_prefix(september):
    db = FakeDB(last=7203)
    assert pn.assign(db, budgetary=False) == "26.9.7204"
    assert pn.assign(db, budgetary=True) == "26.9.7205B"
    assert db.rpc_calls == [("next_project_number", None)] * 2


def test_assign_wraps_9999_to_0001(september):
    db = FakeDB(last=9999)
    assert pn.assign(db, budgetary=False) == "26.9.0001"
    assert db.last == 1


# ── with_budgetary ─────────────────────────────────────────────────────────


def test_with_budgetary_adds_and_strips_the_marker():
    assert pn.with_budgetary("26.9.7204", True) == "26.9.7204B"
    assert pn.with_budgetary("26.9.7204B", False) == "26.9.7204"
    # Idempotent, and normalised (trimmed) on the way through.
    assert pn.with_budgetary("26.9.7204B", True) == "26.9.7204B"
    assert pn.with_budgetary("26.9.7204", False) == "26.9.7204"
    assert pn.with_budgetary("26.9.7204 ", True) == "26.9.7204B"


@pytest.mark.parametrize("legacy", ["G3-2026-001", "PM-101", "", "26.9.720"])
def test_with_budgetary_refuses_a_legacy_number_with_the_sentence(legacy):
    with pytest.raises(pn.LegacyNumberError) as exc:
        pn.with_budgetary(legacy, True)
    assert str(exc.value) == (
        "This project's number predates automatic numbering and cannot be changed"
    )
    assert exc.value.number == legacy
    assert isinstance(exc.value, ValueError)


# ── insert_with_assigned_number ────────────────────────────────────────────


def test_insert_assigns_and_returns_the_row(september):
    db = FakeDB(last=7203)
    payload = {"name": "Acme Tower", "number": "typed-by-an-old-client"}
    row = pn.insert_with_assigned_number(db, payload, budgetary=False)
    assert row["number"] == "26.9.7204"
    assert row["id"] == "p1"
    assert payload["number"] == "26.9.7204"
    assert db.insert_attempts == [{"name": "Acme Tower", "number": "26.9.7204"}]


def test_insert_adds_the_budgetary_marker(september):
    db = FakeDB(last=7203)
    row = pn.insert_with_assigned_number(db, {"name": "Budget job"}, budgetary=True)
    assert row["number"] == "26.9.7204B"


def test_insert_retries_past_numbers_already_taken(september):
    # A PM-created row typed 26.9.7204 and a legacy row 26.9.7205 (with a
    # trailing space, as the unique index folds it): the loop skips both.
    db = FakeDB(
        last=7203,
        projects=[{"id": "pm", "number": "26.9.7204"}, {"id": "old", "number": "26.9.7205 "}],
    )
    row = pn.insert_with_assigned_number(db, {"name": "Third try"}, budgetary=False)
    assert row["number"] == "26.9.7206"
    assert [a["number"] for a in db.insert_attempts] == ["26.9.7204", "26.9.7205", "26.9.7206"]
    # One counter value per try, never rewound.
    assert len(db.rpc_calls) == 3
    assert db.last == 7206


def test_insert_gives_up_at_the_cap_with_the_sentence(september):
    taken = [{"id": f"t{i}", "number": f"26.9.{7204 + i:04d}"} for i in range(pn.NUMBER_MAX_TRIES)]
    db = FakeDB(last=7203, projects=taken)
    with pytest.raises(pn.NoFreeNumberError) as exc:
        pn.insert_with_assigned_number(db, {"name": "Unlucky"}, budgetary=False)
    assert str(exc.value) == "No free project number could be assigned; try again"
    assert exc.value.tries == pn.NUMBER_MAX_TRIES
    assert len(db.insert_attempts) == pn.NUMBER_MAX_TRIES
    assert len(db.rpc_calls) == pn.NUMBER_MAX_TRIES
    # The chained cause is the last unique violation, for the logs.
    assert exc.value.__cause__ is not None and "23505" in str(exc.value.__cause__)


def test_insert_succeeds_on_the_last_permitted_try(september):
    taken = [
        {"id": f"t{i}", "number": f"26.9.{7204 + i:04d}"} for i in range(pn.NUMBER_MAX_TRIES - 1)
    ]
    db = FakeDB(last=7203, projects=taken)
    row = pn.insert_with_assigned_number(db, {"name": "Just in time"}, budgetary=False)
    assert row["number"] == f"26.9.{7203 + pn.NUMBER_MAX_TRIES:04d}"


def test_insert_lets_other_failures_propagate_at_once(september):
    db = FakeDB(last=7203)
    db.insert_raises = RuntimeError("connection reset")
    with pytest.raises(RuntimeError, match="connection reset"):
        pn.insert_with_assigned_number(db, {"name": "x"}, budgetary=False)
    assert len(db.insert_attempts) == 1
    assert len(db.rpc_calls) == 1


def test_insert_does_not_retry_a_unique_violation_on_another_index(september):
    db = FakeDB(last=7203)
    db.insert_raises = Exception(
        'duplicate key value violates unique constraint "project_gcs_pkey"'
    )
    with pytest.raises(Exception, match="project_gcs_pkey"):
        pn.insert_with_assigned_number(db, {"name": "x"}, budgetary=False)
    assert len(db.insert_attempts) == 1


def test_insert_refuses_an_empty_insert_result(september):
    class _Silent(FakeDB):
        def table(self, name):
            q = super().table(name)
            if name == "projects":
                q.execute = lambda: SimpleNamespace(data=[])
            return q

    with pytest.raises(RuntimeError, match="no row"):
        pn.insert_with_assigned_number(_Silent(last=7203), {"name": "x"}, budgetary=False)


# ── is_duplicate_number ────────────────────────────────────────────────────


def test_is_duplicate_number_recognises_the_index_and_a_23505_on_number():
    assert pn.is_duplicate_number(
        Exception('duplicate key value violates unique constraint "projects_number_unique_idx"')
    )
    assert pn.is_duplicate_number(Exception("{'code': '23505', 'message': 'number exists'}"))
    assert not pn.is_duplicate_number(
        Exception('duplicate key value violates unique constraint "project_gcs_pkey"')
    )
    assert not pn.is_duplicate_number(Exception("Project not found"))
