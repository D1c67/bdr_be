"""AI Estimating daily spend cap (services/ai_spend + routers/ai_spend).

Invariants pinned here:

  window      starts at the later of Pacific midnight and the last reset,
              across PDT/PST and right around midnight.
  spend       sums plans_reader_calls.cost_usd inside the window only,
              paging past 1,000 rows, NULL costs counted as zero.
  cap         check_cap refuses at spent >= cap and when the estimate would
              reach it; the message is app-authored and user-safe.
  notice      after_call notifies IT Admins and Executives (bell + email,
              type ai_spend.cap_hit) once per window, writes one cap_hit
              event, and re-arms after a reset or the next Pacific day.
  writes      set_cap validates 1..10000 and logs cap_changed + audit;
              reset_window moves the window and logs cap_reset + audit.
  routes      GET is any internal role; PATCH /cap and POST /reset are IT
              Admin and Executive only; the estimator gets 403 everywhere;
              everything 404s while PLANS_READER_ENABLED is off.
  resilience  a failing notice, event or audit never turns a refusal into
              a generic error or a saved Settings change into a 500.

Supabase is faked in memory per house convention (own copy, with the
conditional UPDATE's or_() filter the notice dedupe relies on).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_internal
from app.core.features import require_plans_reader
from app.core.roles import Role
from app.routers import ai_spend as router_mod
from app.services import ai_spend
from app.services import notification_email

UTC = timezone.utc
PT = ZoneInfo("America/Los_Angeles")


def pt(y, mo, d, h=0, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=PT).astimezone(UTC)


# ── Fake Supabase ────────────────────────────────────────────────────────


def _ts(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


_TS_COLS = {"created_at", "cap_hit_notified_at", "cap_window_started_at"}
# What a PostgREST filter literal for a timestamp must look like: UTC with a
# Z suffix, no '+' (URL-decodes to a space) and no commas (or=() separator).
_PG_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")


def _split_top(expr):
    """Split an or=() body on top-level commas (not inside and(...))."""
    parts, depth, cur = [], 0, ""
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _parse_cond(part):
    """One PostgREST condition, or ('and', [conds]) for and(...)."""
    if part.startswith("and(") and part.endswith(")"):
        return ("and", [_parse_cond(p) for p in _split_top(part[4:-1])])
    col, op, val = part.split(".", 2)
    if col in _TS_COLS and val != "null":
        assert _PG_TS.match(val), f"bad timestamp literal in filter: {val!r}"
    return (op, col, val)


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._ors = []
        self._orders = []
        self._limit = None
        self._range = None
        self._ignore_dup = False

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload, ignore_duplicates=False, **_k):
        self._op, self._payload, self._ignore_dup = "upsert", payload, ignore_duplicates
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def or_(self, expr):
        self.db.or_exprs.append((self.table, self._op, expr))
        self._ors.append([_parse_cond(p) for p in _split_top(expr)])
        return self

    def order(self, col, desc=False, **_k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, **_k):
        self._limit = n
        return self

    def range(self, start, end, **_k):
        self._range = (start, end)
        return self

    @staticmethod
    def _cond(row, op, col, val=None):
        if op == "and":
            return all(_Query._cond(row, *c) for c in col)
        have = row.get(col)
        if op == "is":
            assert val == "null"
            return have is None
        if have is None:
            return False
        if col in _TS_COLS:
            have, val = _ts(have), _ts(val)
        else:
            have, val = (have, val) if op == "eq" and not isinstance(val, str) else (str(have), str(val))
        if op == "eq":
            return have == val
        if op == "gte":
            return have >= val
        if op == "gt":
            return have > val
        if op == "lt":
            return have < val
        raise AssertionError(f"unsupported op {op}")

    def _matches(self, row):
        if not all(self._cond(row, op, col, val) for op, col, val in self._filters):
            return False
        return all(any(self._cond(row, *c) for c in group) for group in self._ors)

    def execute(self):
        self.db.calls.append((self.table, self._op))
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            for col, desc in reversed(self._orders):
                def key(r, col=col):
                    v = r.get(col)
                    return (v is None, _ts(v) if col in _TS_COLS and v is not None else str(v))
                hits.sort(key=key, reverse=desc)
            if self._range is not None:
                hits = hits[self._range[0] : self._range[1] + 1]
            if self._limit is not None:
                hits = hits[: self._limit]
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = {"id": len(rows) + 1, "created_at": self.db.now.isoformat(), **p}
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "upsert":
            existing = [r for r in rows if r.get("id") == self._payload.get("id")]
            if existing:
                if not self._ignore_dup:
                    existing[0].update(self._payload)
                return SimpleNamespace(data=[])
            row = {
                ai_spend.CAP_COLUMN: 75,
                "cap_window_started_at": None,
                "cap_hit_notified_at": None,
                "updated_by": None,
                "updated_at": self.db.now.isoformat(),
                **self._payload,
            }
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        raise AssertionError(f"unsupported op {self._op}")


class FakeDB:
    def __init__(self, tables=None, now=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.now = now or datetime.now(UTC)
        self.calls = []
        self.or_exprs = []

    def table(self, name):
        return _Query(self, name)


def _settings(cap=75, window=None, notified=None):
    return {
        "id": 1,
        ai_spend.CAP_COLUMN: cap,
        "cap_window_started_at": window.isoformat() if window else None,
        "cap_hit_notified_at": notified.isoformat() if notified else None,
        "updated_by": None,
        "updated_at": "2026-10-01T00:00:00+00:00",
    }


def _call(at: datetime, cost) -> dict:
    return {"id": f"c-{at.timestamp()}-{cost}", "created_at": at.isoformat(), "cost_usd": cost}


def _db(calls=(), **settings_kw):
    return FakeDB({"ai_spend_settings": [_settings(**settings_kw)], "plans_reader_calls": list(calls)})


@pytest.fixture
def sent(monkeypatch):
    """Capture notify_role and audit calls made through the service."""
    out = {"notices": [], "audits": []}

    def _notify(role, project_id, type_, message, rfq_id=None, mirror_email=True, metadata=None):
        out["notices"].append(
            {"role": role, "project_id": project_id, "type": type_, "message": message,
             "mirror_email": mirror_email, "metadata": metadata}
        )

    monkeypatch.setattr(ai_spend.notifications, "notify_role", _notify)
    monkeypatch.setattr(
        ai_spend.notifications, "audit", lambda *a, **k: out["audits"].append(a)
    )
    return out


# ── Window math ──────────────────────────────────────────────────────────


def test_window_starts_at_pacific_midnight_pdt():
    now = pt(2026, 10, 17, 14, 30)
    assert ai_spend.window_start(now, _settings()) == datetime(2026, 10, 17, 7, tzinfo=UTC)


def test_window_starts_at_pacific_midnight_pst():
    now = pt(2026, 12, 3, 9)
    assert ai_spend.window_start(now, _settings()) == datetime(2026, 12, 3, 8, tzinfo=UTC)


def test_window_just_before_and_after_midnight():
    before = pt(2026, 10, 17, 23, 59, 59)
    after = pt(2026, 10, 18, 0, 0, 1)
    assert ai_spend.window_start(before, _settings()) == pt(2026, 10, 17)
    assert ai_spend.window_start(after, _settings()) == pt(2026, 10, 18)
    # 23:59 Pacific is already the next UTC day; the window still follows PT.
    assert before.date().isoformat() == "2026-10-18"


def test_window_after_reset_uses_reset_time():
    reset = pt(2026, 10, 17, 11)
    now = pt(2026, 10, 17, 15)
    assert ai_spend.window_start(now, _settings(window=reset)) == reset


def test_reset_from_yesterday_is_ignored_at_new_day():
    reset = pt(2026, 10, 16, 22)
    now = pt(2026, 10, 17, 1)
    assert ai_spend.window_start(now, _settings(window=reset)) == pt(2026, 10, 17)


def test_window_accepts_z_suffixed_reset():
    row = _settings()
    row["cap_window_started_at"] = "2026-10-17T18:00:00Z"
    now = pt(2026, 10, 17, 15)
    assert ai_spend.window_start(now, row) == datetime(2026, 10, 17, 18, tzinfo=UTC)


# ── Spend ────────────────────────────────────────────────────────────────


def test_spent_counts_only_the_window():
    now = pt(2026, 10, 17, 15)
    db = _db([
        _call(pt(2026, 10, 16, 23, 59), "50.00"),  # yesterday
        _call(pt(2026, 10, 17, 0, 0), "1.25"),  # exactly midnight counts
        _call(pt(2026, 10, 17, 9), 2.5),
        _call(pt(2026, 10, 17, 10), None),  # failed call, no cost
    ])
    assert ai_spend.spent_usd(db, now) == Decimal("3.75")


def test_spent_after_reset_starts_from_reset():
    reset = pt(2026, 10, 17, 11)
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 10), "70"), _call(pt(2026, 10, 17, 12), "4.5")], window=reset)
    assert ai_spend.spent_usd(db, now) == Decimal("4.5")


def test_spent_pages_past_one_thousand_rows():
    now = pt(2026, 10, 17, 15)
    base = pt(2026, 10, 17, 1)
    calls = [
        {"id": f"c{i:05d}", "created_at": (base + timedelta(seconds=i)).isoformat(), "cost_usd": "0.01"}
        for i in range(2345)
    ]
    db = _db(calls)
    assert ai_spend.spent_usd(db, now) == Decimal("23.45")


def test_settings_row_created_when_missing():
    db = FakeDB({"ai_spend_settings": [], "plans_reader_calls": []})
    row = ai_spend.get_settings_row(db)
    assert row["id"] == 1
    assert ai_spend.cap_of(row) == Decimal("75")
    assert len(db.tables["ai_spend_settings"]) == 1


def test_status_payload():
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "20.1234")])
    out = ai_spend.status(db, now)
    assert out["cap_usd"] == 75.0
    assert out["spent_usd"] == 20.1234
    assert out["remaining_usd"] == 54.8766
    assert out["cap_hit"] is False
    assert out["blocked"] is False
    assert out["window_started_at"] == pt(2026, 10, 17).isoformat()
    assert set(out) == {
        "cap_usd", "spent_usd", "remaining_usd", "window_started_at", "cap_hit",
        "blocked", "cap_hit_notified_at", "updated_at", "updated_by",
    }


def test_spent_keyset_paging_across_a_shared_timestamp():
    """1,500 rows inserted in one transaction share created_at: the keyset
    cursor falls back to id inside the tie, so nothing is lost or repeated."""
    now = pt(2026, 10, 17, 15)
    at = pt(2026, 10, 17, 9).isoformat()
    calls = [{"id": f"{i:032x}", "created_at": at, "cost_usd": "0.01"} for i in range(1500)]
    calls.append(_call(pt(2026, 10, 17, 10), "2"))
    db = _db(calls)
    assert ai_spend.spent_usd(db, now) == Decimal("17.00")
    keyset = [e for t, _op, e in db.or_exprs if t == "plans_reader_calls"]
    assert len(keyset) == 1
    assert re.match(
        r"^created_at\.gt\.(\S+Z),and\(created_at\.eq\.\1,id\.gt\.[0-9a-f]{32}\)$", keyset[0]
    ), keyset[0]


def test_spent_counts_rows_inserted_mid_sum_once():
    """A call recorded while the sum pages lands after the keyset cursor
    and is counted exactly once."""
    now = pt(2026, 10, 17, 15)
    base = pt(2026, 10, 17, 1)
    calls = [
        {"id": f"c{i:05d}", "created_at": (base + timedelta(seconds=i)).isoformat(), "cost_usd": "0.01"}
        for i in range(1200)
    ]
    db = _db(calls)
    real_table = db.table
    state = {"selects": 0}

    def table(name):
        q = real_table(name)
        if name == "plans_reader_calls":
            orig = q.execute

            def execute():
                out = orig()
                state["selects"] += 1
                if state["selects"] == 1:
                    # Inserted between page 1 and page 2, older than "now".
                    db.tables["plans_reader_calls"].insert(
                        0, {"id": "a-late", "created_at": pt(2026, 10, 17, 14).isoformat(), "cost_usd": "5"}
                    )
                return out

            q.execute = execute
        return q

    db.table = table
    assert ai_spend.spent_usd(db, now) == Decimal("17.00")


# ── check_cap ────────────────────────────────────────────────────────────


def test_check_cap_allows_under_cap(sent):
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "10")])
    assert ai_spend.check_cap(db, now=now) == Decimal("65")
    assert ai_spend.check_cap(db, estimated_usd=Decimal("64.99"), now=now) == Decimal("65")
    assert sent["notices"] == []


def test_check_cap_refuses_at_cap_and_notifies_once(sent):
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "75")])
    with pytest.raises(ai_spend.SpendCapReached) as exc:
        ai_spend.check_cap(db, now=now)
    assert str(exc.value) == (
        "AI Estimating reached its daily spend cap of $75.00. "
        "Runs are paused until the cap is raised or reset in Settings."
    )
    assert exc.value.cap_usd == Decimal("75")
    assert exc.value.spent_usd == Decimal("75")
    assert len(sent["notices"]) == 2
    with pytest.raises(ai_spend.SpendCapReached):
        ai_spend.check_cap(db, now=now)
    assert len(sent["notices"]) == 2


def test_check_cap_refuses_when_estimate_reaches_cap(sent):
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "72")])
    with pytest.raises(ai_spend.SpendCapReached) as exc:
        ai_spend.check_cap(db, estimated_usd=5, now=now)
    assert "$3.00 left today" in str(exc.value)
    assert "Raise or reset" in str(exc.value)
    assert exc.value.estimated_usd == Decimal("5")


def test_estimate_refusal_notifies_once_and_reports_blocked(sent):
    """$3 left and every step costs $5: spend never reaches the cap, yet AI
    Estimating is paused. Both roles hear once and Settings shows blocked."""
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "72")])
    for minutes in (0, 5, 10):
        with pytest.raises(ai_spend.SpendCapReached):
            ai_spend.check_cap(db, estimated_usd=5, now=now + timedelta(minutes=minutes))
    assert [n["role"] for n in sent["notices"]] == [Role.IT_ADMIN, Role.EXECUTIVE]
    note = sent["notices"][0]
    assert note["type"] == "ai_spend.cap_hit"
    assert note["mirror_email"] is True
    assert note["message"] == (
        "AI Estimating paused a run: its next step needs about $5.00 but only $3.00 "
        "is left of the daily spend cap of $75.00. Raise or reset the cap in Settings "
        "to continue."
    )
    assert note["metadata"] == {"spent_usd": 72.0, "cap_usd": 75.0, "estimated_usd": 5.0}
    events = db.tables["ai_spend_events"]
    assert [e["kind"] for e in events] == ["cap_hit"]
    assert events[0]["details"]["reason"] == "estimate"
    assert events[0]["details"]["estimated_usd"] == "5.00"

    st = ai_spend.status(db, now + timedelta(minutes=11))
    assert st["cap_hit"] is False
    assert st["blocked"] is True

    # Raising the cap above the spend clears the blocked state and re-arms.
    out = ai_spend.set_cap(db, cap_usd=100, actor_id="u1", now=now + timedelta(minutes=12))
    assert out["blocked"] is False


def test_estimate_larger_than_the_whole_cap_says_raise(sent):
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "1")])
    with pytest.raises(ai_spend.SpendCapReached) as exc:
        ai_spend.check_cap(db, estimated_usd=80, now=now)
    msg = str(exc.value)
    assert msg == (
        "This step needs about $80.00, more than AI Estimating's whole daily spend "
        "cap of $75.00. Raise the cap above that in Settings to continue."
    )
    assert "reset" not in msg
    assert msg.isascii()
    assert "Raise the cap above that" in sent["notices"][0]["message"]


class _FailingDB(FakeDB):
    """FakeDB whose chosen (table, op) pairs raise, like a PostgREST hiccup."""

    def __init__(self, *a, fail=(), **k):
        super().__init__(*a, **k)
        self.fail = set(fail)

    def table(self, name):
        q = super().table(name)
        orig = q.execute

        def execute():
            if (name, q._op) in self.fail:
                raise RuntimeError(f"{name} {q._op} failed")
            return orig()

        q.execute = execute
        return q


def _failing_db(calls=(), fail=(), **settings_kw):
    return _FailingDB(
        {"ai_spend_settings": [_settings(**settings_kw)], "plans_reader_calls": list(calls)},
        fail=fail,
    )


def test_check_cap_raises_spend_cap_reached_when_the_notice_fails(sent):
    """The claim UPDATE blowing up must not turn the refusal into a generic
    error: the run has to land in over_cap, not failed."""
    db = _failing_db([_call(pt(2026, 10, 17, 9), "80")], fail={("ai_spend_settings", "update")})
    with pytest.raises(ai_spend.SpendCapReached) as exc:
        ai_spend.check_cap(db, now=pt(2026, 10, 17, 15))
    assert "reached its daily spend cap of $75.00" in str(exc.value)

    db = _failing_db([_call(pt(2026, 10, 17, 9), "72")], fail={("ai_spend_settings", "update")})
    with pytest.raises(ai_spend.SpendCapReached):
        ai_spend.check_cap(db, estimated_usd=5, now=pt(2026, 10, 17, 15))


def test_failed_event_insert_after_claim_still_notifies_both_roles(sent):
    db = _failing_db([_call(pt(2026, 10, 17, 9), "80")], fail={("ai_spend_events", "insert")})
    ai_spend.after_call(db, now=pt(2026, 10, 17, 15))
    assert [n["role"] for n in sent["notices"]] == [Role.IT_ADMIN, Role.EXECUTIVE]
    assert db.tables["ai_spend_settings"][0]["cap_hit_notified_at"] is not None


def test_notice_claim_filter_literal(sent):
    """The conditional claim sends a PostgREST-safe literal: Z suffix, no '+'."""
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "80")])
    ai_spend.after_call(db, now=now)
    claims = [e for t, op, e in db.or_exprs if t == "ai_spend_settings" and op == "update"]
    assert claims == [
        "cap_hit_notified_at.is.null,cap_hit_notified_at.lt.2026-10-17T07:00:00.000000Z"
    ]


def test_spend_cap_reached_is_runtime_error_and_ascii():
    exc = ai_spend.SpendCapReached("x", cap_usd=Decimal("1"), spent_usd=Decimal("1"))
    assert isinstance(exc, RuntimeError)
    db = _db([_call(pt(2026, 10, 17, 9), "1000")])
    with pytest.raises(ai_spend.SpendCapReached) as caught:
        ai_spend.check_cap(db, now=pt(2026, 10, 17, 15))
    msg = str(caught.value)
    assert msg.isascii()
    assert "\u2014" not in msg


# ── after_call notice ────────────────────────────────────────────────────


def test_after_call_under_cap_does_nothing(sent):
    db = _db([_call(pt(2026, 10, 17, 9), "74.99")])
    ai_spend.after_call(db, now=pt(2026, 10, 17, 15))
    assert sent["notices"] == []
    assert db.tables.get("ai_spend_events", []) == []


def test_after_call_notifies_both_roles_once_per_window(sent):
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "60"), _call(pt(2026, 10, 17, 10), "15.5")])
    ai_spend.after_call(db, now=now)
    ai_spend.after_call(db, now=now + timedelta(minutes=5))
    ai_spend.after_call(db, now=now + timedelta(minutes=10))

    assert [n["role"] for n in sent["notices"]] == [Role.IT_ADMIN, Role.EXECUTIVE]
    for n in sent["notices"]:
        assert n["type"] == "ai_spend.cap_hit"
        assert n["project_id"] is None
        assert n["mirror_email"] is True
        assert n["metadata"] == {"spent_usd": 75.5, "cap_usd": 75.0}
        assert n["message"] == (
            "AI Estimating reached its daily spend cap of $75.00. "
            "Runs are paused until the cap is raised or reset in Settings."
        )
    events = db.tables["ai_spend_events"]
    assert [e["kind"] for e in events] == ["cap_hit"]
    assert events[0]["amount_usd"] == "75.5000"
    assert events[0]["actor_id"] is None
    assert db.tables["ai_spend_settings"][0]["cap_hit_notified_at"] == now.isoformat()


def test_record_is_after_call():
    assert ai_spend.record is ai_spend.after_call


def test_notice_rearms_on_next_pacific_day(sent):
    yesterday_hit = pt(2026, 10, 16, 15)
    db = _db(
        [_call(pt(2026, 10, 17, 9), "80")],
        notified=yesterday_hit,
    )
    ai_spend.after_call(db, now=pt(2026, 10, 17, 15))
    assert len(sent["notices"]) == 2


def test_notice_claim_lost_to_another_worker(sent):
    """The read saw no stamp, but another worker claimed the window first:
    the conditional update matches nothing and no second notice goes out."""
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "80")])
    row = ai_spend.get_settings_row(db)
    db.tables["ai_spend_settings"][0]["cap_hit_notified_at"] = (now - timedelta(minutes=1)).isoformat()
    sent_now = ai_spend._notify_once(
        db, row=row, start=ai_spend.window_start(now, row), cap=Decimal("75"),
        spent=Decimal("80"), now=now,
    )
    assert sent_now is False
    assert sent["notices"] == []
    assert db.tables.get("ai_spend_events", []) == []


def test_after_call_never_raises(monkeypatch, sent):
    class Broken:
        def table(self, _name):
            raise RuntimeError("db down")

    ai_spend.after_call(Broken(), now=pt(2026, 10, 17, 15))  # no exception


def test_one_role_failing_does_not_lose_the_other(monkeypatch):
    got = []

    def _notify(role, *a, **k):
        if role == Role.IT_ADMIN:
            raise RuntimeError("boom")
        got.append(role)

    monkeypatch.setattr(ai_spend.notifications, "notify_role", _notify)
    db = _db([_call(pt(2026, 10, 17, 9), "80")])
    ai_spend.after_call(db, now=pt(2026, 10, 17, 15))
    assert got == [Role.EXECUTIVE]


# ── set_cap / reset_window ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad", [0, "0.99", -5, 10000.01, "abc", "NaN", "Infinity", "1e30", "-1e30", "1E-30", None, True]
)
def test_set_cap_validation(bad, sent):
    db = _db()
    with pytest.raises(ValueError):
        ai_spend.set_cap(db, cap_usd=bad, actor_id="u1")
    assert db.tables.get("ai_spend_events", []) == []
    assert sent["audits"] == []


@pytest.mark.parametrize("good", [1, "10000", Decimal("150.505")])
def test_set_cap_accepts_bounds(good, sent):
    db = _db()
    ai_spend.set_cap(db, cap_usd=good, actor_id="u1", now=pt(2026, 10, 17, 15))
    stored = Decimal(str(db.tables["ai_spend_settings"][0][ai_spend.CAP_COLUMN]))
    assert stored == Decimal(str(good)).quantize(Decimal("0.01"))


def test_set_cap_writes_event_audit_and_rearms(sent):
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "80")], notified=pt(2026, 10, 17, 12))
    out = ai_spend.set_cap(db, cap_usd=Decimal("150"), actor_id="u1", now=now)
    row = db.tables["ai_spend_settings"][0]
    assert row[ai_spend.CAP_COLUMN] == "150.00"
    assert row["updated_by"] == "u1"
    assert row["cap_hit_notified_at"] is None  # re-armed for the new cap
    ev = db.tables["ai_spend_events"][-1]
    assert ev["kind"] == "cap_changed"
    assert ev["actor_id"] == "u1"
    assert ev["details"]["from_usd"] == "75.00"
    assert ev["details"]["to_usd"] == "150.00"
    assert sent["audits"][0][:4] == ("u1", "ai_spend.cap_changed", "ai_spend_settings", "1")
    assert out["cap_usd"] == 150.0
    assert out["cap_hit"] is False
    assert sent["notices"] == []


def test_set_cap_below_spend_notifies(sent):
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "40")])
    out = ai_spend.set_cap(db, cap_usd=30, actor_id="u1", now=now)
    assert out["cap_hit"] is True
    assert len(sent["notices"]) == 2
    kinds = [e["kind"] for e in db.tables["ai_spend_events"]]
    assert kinds == ["cap_changed", "cap_hit"]


@pytest.mark.parametrize(
    "fail",
    [
        {("ai_spend_events", "insert")},
        {("ai_spend_events", "insert"), ("audit",)},
    ],
)
def test_set_cap_saved_change_survives_bookkeeping_failures(monkeypatch, sent, fail):
    if ("audit",) in fail:
        monkeypatch.setattr(
            ai_spend.notifications, "audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        )
    db = _failing_db([_call(pt(2026, 10, 17, 9), "40")], fail=fail)
    out = ai_spend.set_cap(db, cap_usd=30, actor_id="u1", now=pt(2026, 10, 17, 15))
    assert out["cap_usd"] == 30.0
    assert out["cap_hit"] is True
    assert db.tables["ai_spend_settings"][0][ai_spend.CAP_COLUMN] == "30.00"
    assert len(sent["notices"]) == 2  # the notice still went out


def test_set_cap_survives_a_failing_claim(sent):
    """Lowering the cap below spend claims the notice with a second UPDATE;
    that UPDATE failing must not 500 a cap that is already saved."""
    db = _db([_call(pt(2026, 10, 17, 9), "40")])
    real_table = db.table
    updates = {"n": 0}

    def table(name):
        q = real_table(name)
        orig = q.execute

        def execute():
            if name == "ai_spend_settings" and q._op == "update":
                updates["n"] += 1
                if updates["n"] == 2:
                    raise RuntimeError("claim failed")
            return orig()

        q.execute = execute
        return q

    db.table = table
    out = ai_spend.set_cap(db, cap_usd=30, actor_id="u1", now=pt(2026, 10, 17, 15))
    assert out["cap_usd"] == 30.0
    assert sent["notices"] == []


def test_reset_window_survives_bookkeeping_failures(monkeypatch, sent):
    monkeypatch.setattr(
        ai_spend.notifications, "audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    now = pt(2026, 10, 17, 15)
    db = _failing_db([_call(pt(2026, 10, 17, 9), "80")], fail={("ai_spend_events", "insert")})
    out = ai_spend.reset_window(db, actor_id="u2", now=now)
    assert out["spent_usd"] == 0.0
    assert db.tables["ai_spend_settings"][0]["cap_window_started_at"] == now.isoformat()


def test_reset_window_moves_window_and_rearms(sent):
    now = pt(2026, 10, 17, 15)
    db = _db([_call(pt(2026, 10, 17, 9), "80")], notified=pt(2026, 10, 17, 12))
    out = ai_spend.reset_window(db, actor_id="u2", now=now)
    row = db.tables["ai_spend_settings"][0]
    assert row["cap_window_started_at"] == now.isoformat()
    assert row["cap_hit_notified_at"] is None
    assert out["spent_usd"] == 0.0
    assert out["cap_hit"] is False
    assert out["window_started_at"] == now.isoformat()
    ev = db.tables["ai_spend_events"][-1]
    assert ev["kind"] == "cap_reset"
    assert ev["amount_usd"] == "80.0000"
    assert ev["actor_id"] == "u2"
    assert sent["audits"][0][:2] == ("u2", "ai_spend.cap_reset")

    # A new hit after the reset notifies again.
    db.tables["plans_reader_calls"].append(_call(now + timedelta(minutes=30), "76"))
    ai_spend.after_call(db, now=now + timedelta(hours=1))
    assert len(sent["notices"]) == 2


# ── Notification email registration ──────────────────────────────────────


def test_cap_hit_heading_and_link(monkeypatch):
    assert notification_email.heading_for("ai_spend.cap_hit") == "AI Estimating spend cap reached"
    monkeypatch.setattr(
        notification_email, "get_settings",
        lambda: SimpleNamespace(frontend_url="https://bdr.example.com/"),
    )
    link = notification_email._deep_link(None, Role.EXECUTIVE.value, "ai_spend.cap_hit")
    assert link == "https://bdr.example.com/settings"


# ── Routes ───────────────────────────────────────────────────────────────


def test_route_dependencies():
    by_key = {(next(iter(r.methods)), r.path): r for r in router_mod.router.routes}
    assert set(by_key) == {
        ("GET", "/ai-spend"), ("PATCH", "/ai-spend/cap"), ("POST", "/ai-spend/reset"),
    }
    get_deps = [d.call for d in by_key[("GET", "/ai-spend")].dependant.dependencies]
    assert require_internal in get_deps
    assert require_plans_reader in get_deps  # router-level flag gate
    for key in (("PATCH", "/ai-spend/cap"), ("POST", "/ai-spend/reset")):
        deps = [d.call for d in by_key[key].dependant.dependencies]
        assert router_mod.require_spend_admin in deps


USERS = {
    "exec": Role.EXECUTIVE,
    "it": Role.IT_ADMIN,
    "admin": Role.ESTIMATING_ADMIN,
    "labor": Role.ESTIMATING_ENGINEER_LABOR,
    "acct": Role.ACCOUNTANT,
    "estimator": Role.ESTIMATOR,
}


@pytest.fixture
def flag_env(monkeypatch):
    """Set PLANS_READER_ENABLED for the block (the local .env may set it)."""

    def _set(on: bool):
        monkeypatch.setenv("PLANS_READER_ENABLED", "true" if on else "false")
        get_settings.cache_clear()

    yield _set
    monkeypatch.delenv("PLANS_READER_ENABLED", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def api(monkeypatch, sent, flag_env):
    import app.main

    flag_env(True)

    db = _db([_call(datetime.now(UTC) - timedelta(seconds=5), "10")])
    monkeypatch.setattr(router_mod, "get_supabase", lambda: db)
    who = {"key": "exec"}

    def _user():
        return CurrentUser(
            id=f"user-{who['key']}", email=f"{who['key']}@g3.com",
            role=USERS[who["key"]], is_active=True,
        )

    app.main.app.dependency_overrides[get_current_user] = _user
    client = TestClient(app.main.app, raise_server_exceptions=True)
    yield SimpleNamespace(client=client, who=who, db=db)
    app.main.app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("key", ["exec", "it", "admin", "labor", "acct"])
def test_get_status_any_internal_role(api, key):
    api.who["key"] = key
    res = api.client.get("/ai-spend")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["cap_usd"] == 75.0
    assert body["cap_hit"] is False


def test_estimator_is_refused_everywhere(api):
    api.who["key"] = "estimator"
    assert api.client.get("/ai-spend").status_code == 403
    assert api.client.patch("/ai-spend/cap", json={"cap_usd": 100}).status_code == 403
    assert api.client.post("/ai-spend/reset").status_code == 403


@pytest.mark.parametrize("key", ["admin", "labor", "acct"])
def test_writes_refused_for_other_internal_roles(api, key):
    api.who["key"] = key
    assert api.client.patch("/ai-spend/cap", json={"cap_usd": 100}).status_code == 403
    assert api.client.post("/ai-spend/reset").status_code == 403
    assert api.db.tables.get("ai_spend_events", []) == []


@pytest.mark.parametrize("key", ["exec", "it"])
def test_patch_cap_and_reset_for_admins(api, key):
    api.who["key"] = key
    res = api.client.patch("/ai-spend/cap", json={"cap_usd": 120.5})
    assert res.status_code == 200, res.text
    assert res.json()["cap_usd"] == 120.5
    assert api.db.tables["ai_spend_settings"][0]["updated_by"] == f"user-{key}"

    res = api.client.post("/ai-spend/reset")
    assert res.status_code == 200, res.text
    assert res.json()["spent_usd"] == 0.0
    assert [e["kind"] for e in api.db.tables["ai_spend_events"]] == ["cap_changed", "cap_reset"]


@pytest.mark.parametrize("bad", [0, 0.5, 10001, "abc"])
def test_patch_cap_rejects_out_of_range(api, bad):
    api.who["key"] = "exec"
    res = api.client.patch("/ai-spend/cap", json={"cap_usd": bad})
    assert res.status_code == 422
    assert api.db.tables.get("ai_spend_events", []) == []


def test_patch_cap_service_value_error_is_422(api, monkeypatch):
    """The router maps a service ValueError (not just Pydantic) to 422."""

    def _boom(*_a, **_k):
        raise ValueError("The daily cap must be a dollar amount.")

    monkeypatch.setattr(router_mod.ai_spend, "set_cap", _boom)
    api.who["key"] = "exec"
    res = api.client.patch("/ai-spend/cap", json={"cap_usd": 100})
    assert res.status_code == 422
    assert res.json()["detail"] == "The daily cap must be a dollar amount."


def test_routes_404_while_feature_is_off(api, flag_env):
    flag_env(False)
    api.who["key"] = "exec"
    for res in (
        api.client.get("/ai-spend"),
        api.client.patch("/ai-spend/cap", json={"cap_usd": 100}),
        api.client.post("/ai-spend/reset"),
    ):
        assert res.status_code == 404
        assert res.json() == {"detail": "Not Found"}
    assert api.db.tables.get("ai_spend_events", []) == []
    assert api.db.tables["ai_spend_settings"][0]["updated_by"] is None


def test_get_status_reports_blocked(api):
    api.who["key"] = "acct"
    body = api.client.get("/ai-spend").json()
    assert body["blocked"] is False
