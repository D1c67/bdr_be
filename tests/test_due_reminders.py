"""Due-date reminder poller — window math, completion, recipients, poll_once.

Pure-logic tests plus poll_once against a fake Supabase client; nothing here
touches the network or real settings beyond the display timezone.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.roles import Role
from app.services import due_reminders as dr
from app.services.due_reminder_prefs import (
    ActualBidPref,
    NotificationPrefsDoc,
    TaskKindPref,
    effective_prefs,
)

DUE = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
LOOKBACK = timedelta(days=7)


def _fire(now, palette=dr.TASK_PALETTE, include_expired=True):
    return dr.fire_key(DUE, now, palette, include_expired, LOOKBACK)


# ── fire_key window math ──────────────────────────────────────────────────


def test_fire_key_task_palette_windows():
    assert _fire(DUE - timedelta(weeks=2)) == "2w"            # start inclusive
    assert _fire(DUE - timedelta(weeks=2, seconds=1)) is None  # before largest
    assert _fire(DUE - timedelta(weeks=1, seconds=1)) == "2w"  # end exclusive
    assert _fire(DUE - timedelta(weeks=1)) == "1w"
    assert _fire(DUE - timedelta(days=2)) == "2d"
    assert _fire(DUE - timedelta(days=1)) == "1d"
    assert _fire(DUE - timedelta(hours=1)) == "1h"
    assert _fire(DUE - timedelta(seconds=1)) == "1h"           # runs up to due


def test_fire_key_expired_only_within_lookback():
    assert _fire(DUE) == "expired"
    assert _fire(DUE + LOOKBACK - timedelta(seconds=1)) == "expired"
    assert _fire(DUE + LOOKBACK) is None
    assert _fire(DUE, include_expired=False) is None


def test_fire_key_actual_bid_palette():
    p = dr.ACTUAL_BID_PALETTE
    assert dr.fire_key(DUE, DUE - timedelta(hours=24), p, False, LOOKBACK) == "24h"
    assert dr.fire_key(DUE, DUE - timedelta(hours=8), p, False, LOOKBACK) == "8h"
    assert dr.fire_key(DUE, DUE - timedelta(minutes=30), p, False, LOOKBACK) == "1h"
    assert dr.fire_key(DUE, DUE - timedelta(hours=25), p, False, LOOKBACK) is None
    assert dr.fire_key(DUE, DUE, p, False, LOOKBACK) is None  # no expired


# ── is_complete ───────────────────────────────────────────────────────────


def test_is_complete_bid_kinds_only_at_terminal_stages():
    for kind in ("internal_bid", "actual_bid"):
        assert dr.is_complete(kind, "submitted", False, [])
        assert dr.is_complete(kind, "declined", False, [])
        assert not dr.is_complete(kind, "send_out", False, [])
        assert not dr.is_complete(kind, "intake", False, [])


def test_is_complete_estimator_kind():
    assert not dr.is_complete("due_from_estimator", "to_estimator", False, [])
    assert dr.is_complete("due_from_estimator", "to_estimator", True, [])
    assert dr.is_complete("due_from_estimator", "estimate_received", False, [])
    assert not dr.is_complete("due_from_estimator", "intake", False, [])


def test_is_complete_vendors_kind():
    assert not dr.is_complete("due_from_vendors", "receive_quotes", False, [])
    assert not dr.is_complete("due_from_vendors", "receive_quotes", False, ["sent"])
    assert not dr.is_complete("due_from_vendors", "rfqs", False, ["draft", "quotes_in"])
    assert dr.is_complete("due_from_vendors", "receive_quotes", False, ["quotes_in", "closed"])
    assert dr.is_complete("due_from_vendors", "labor_numbers", False, [])


def test_is_complete_unknown_stage_fails_toward_reminding():
    assert not dr.is_complete("internal_bid", "mystery_stage", False, [])


def test_is_complete_unknown_kind_raises():
    with pytest.raises(ValueError):
        dr.is_complete("nonsense", "intake", False, [])


# ── Messages ──────────────────────────────────────────────────────────────


def test_build_message_wording():
    project = {"name": "Acme Tower", "number": "1234"}
    msg = dr.build_message(dr.KINDS["internal_bid"], project, "2d", DUE.isoformat())
    assert "Internal bid for Acme Tower (#1234) is due within 2 days" in msg
    msg = dr.build_message(dr.KINDS["due_from_vendors"], project, "expired", DUE.isoformat())
    assert "Vendor quotes for Acme Tower (#1234) are past due" in msg
    msg = dr.build_message(dr.KINDS["actual_bid"], project, "8h", DUE.isoformat())
    assert "due to the GC within 8 hours" in msg


def test_build_message_without_number():
    msg = dr.build_message(dr.KINDS["internal_bid"], {"name": "Acme"}, "1h", DUE.isoformat())
    assert "Internal bid for Acme is due within 1 hour" in msg


# ── Recipient resolution ──────────────────────────────────────────────────


def _eff(role: Role, stored=None) -> NotificationPrefsDoc:
    return effective_prefs(role, stored)


def test_recipients_role_defaults():
    profiles = [{"id": "pe1", "role": "pe"}, {"id": "acct1", "role": "accountant"}]
    eff = {p["id"]: _eff(Role(p["role"])) for p in profiles}
    got = dr._internal_recipients(dr.KINDS["due_from_vendors"], "1h", profiles, eff)
    assert got == {"pe1"}


def test_recipients_stored_offsets_respected():
    profiles = [{"id": "pm1", "role": "pm"}, {"id": "pa1", "role": "pa"}]
    eff = {
        "pm1": _eff(Role.PM, {"internal_bid": {"enabled": True, "offsets": ["1h"]}}),
        "pa1": _eff(Role.PA),
    }
    got = dr._internal_recipients(dr.KINDS["internal_bid"], "2d", profiles, eff)
    assert got == {"pa1"}


def test_recipients_opt_in_beyond_role_default():
    profiles = [{"id": "ex1", "role": "executive"}]
    eff = {
        "ex1": _eff(
            Role.EXECUTIVE,
            {"due_from_vendors": {"enabled": True, "offsets": ["2w", "1h"]}},
        )
    }
    got = dr._internal_recipients(dr.KINDS["due_from_vendors"], "1h", profiles, eff)
    assert got == {"ex1"}


def test_recipients_actual_bid_hard_role_filter():
    # Even a hand-built doc holding actual_bid must not reach a non-PA.
    doc = NotificationPrefsDoc(
        internal_bid=TaskKindPref(enabled=False, offsets=[]),
        due_from_estimator=TaskKindPref(enabled=False, offsets=[]),
        due_from_vendors=TaskKindPref(enabled=False, offsets=[]),
        actual_bid=ActualBidPref(offsets=["8h"]),
    )
    profiles = [{"id": "ex1", "role": "executive"}, {"id": "pa1", "role": "pa"}]
    eff = {"ex1": doc, "pa1": _eff(Role.PA)}
    got = dr._internal_recipients(dr.KINDS["actual_bid"], "8h", profiles, eff)
    assert got == {"pa1"}


# ── _page_all (PostgREST max-rows cap) ────────────────────────────────────


def test_page_all_drains_past_the_response_cap():
    rows = [{"id": i} for i in range(2500)]

    class _Page:
        def __init__(self, lo, hi):
            self.lo, self.hi = lo, hi

        def execute(self):
            return SimpleNamespace(data=rows[self.lo : self.hi + 1])

    assert dr._page_all(_Page) == rows  # 1000 + 1000 + 500


def test_page_all_single_short_page():
    calls = []

    class _Page:
        def __init__(self, lo, hi):
            calls.append((lo, hi))

        def execute(self):
            return SimpleNamespace(data=[{"id": 1}])

    assert dr._page_all(_Page) == [{"id": 1}]
    assert calls == [(0, 999)]


# ── poll_once against a fake Supabase ─────────────────────────────────────


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, sb, table):
        self._sb = sb
        self._table = table
        self._op = "select"
        self._payload = None
        self._in_args = []

    def select(self, *a, **k):
        return self

    @property
    def not_(self):
        return self

    def in_(self, col, values):
        self._in_args.append((col, list(values)))
        return self

    def or_(self, *a):
        return self

    def eq(self, *a):
        return self

    def is_(self, *a):
        return self

    def order(self, *a):
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload, **kwargs):
        self._op = "upsert"
        self._payload = payload
        self._kwargs = kwargs
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        self._sb.calls.append(
            SimpleNamespace(
                table=self._table, op=self._op, payload=self._payload,
                in_args=self._in_args,
            )
        )
        responder = self._sb.responses.get((self._table, self._op), [])
        data = responder(self._payload) if callable(responder) else responder
        return _FakeResult(data)


class _FakeSupabase:
    def __init__(self, responses):
        self.calls = []
        self.responses = responses

    def table(self, name):
        return _FakeQuery(self, name)


def _echo_ledger(payload):
    return [{"id": f"L{i}", "user_id": row["user_id"]} for i, row in enumerate(payload)]


def _setup(monkeypatch, responses, now):
    fake = _FakeSupabase(responses)
    monkeypatch.setattr(dr, "get_supabase", lambda: fake)
    monkeypatch.setattr(dr, "_now", lambda: now)
    monkeypatch.setattr(
        dr, "get_settings",
        lambda: SimpleNamespace(
            due_reminder_expired_horizon_days=7,
            due_reminder_poll_interval_seconds=300,
        ),
    )
    return fake


def _calls(fake, table, op):
    return [c for c in fake.calls if c.table == table and c.op == op]


NOW = datetime(2026, 6, 12, 15, 0, tzinfo=timezone.utc)


def test_poll_once_vendor_event_end_to_end(monkeypatch):
    due_raw = (NOW + timedelta(minutes=30)).isoformat()
    project = {
        "id": "p1", "name": "Acme", "number": "42", "current_stage": "receive_quotes",
        "internal_bid_at": None, "actual_bid_at": None,
        "due_from_estimator_at": None, "due_from_vendors_at": due_raw,
    }
    fake = _setup(monkeypatch, {
        ("projects", "select"): [project],
        ("rfqs", "select"): [{"project_id": "p1", "status": "sent"}],
        ("profiles", "select"): [{"id": "pe1", "role": "pe"}, {"id": "pm1", "role": "pm"}],
        ("notification_prefs", "select"): [],
        ("due_reminder_log", "upsert"): _echo_ledger,
        ("notifications", "insert"): [],
    }, NOW)

    dr.poll_once()

    [up] = _calls(fake, "due_reminder_log", "upsert")
    assert up.payload == [{
        "project_id": "p1", "user_id": "pe1", "kind": "due_from_vendors",
        "offset_key": "1h", "due_at_snapshot": due_raw,
    }]
    [ins] = _calls(fake, "notifications", "insert")
    [note] = ins.payload
    assert note["user_id"] == "pe1" and note["project_id"] == "p1"
    assert note["type"] == "due.due_from_vendors.1h"
    assert "Vendor quotes for Acme (#42) are due within 1 hour" in note["message"]


def test_poll_once_duplicate_tick_inserts_nothing(monkeypatch):
    due_raw = (NOW + timedelta(minutes=30)).isoformat()
    project = {
        "id": "p1", "name": "Acme", "number": None, "current_stage": "receive_quotes",
        "internal_bid_at": None, "actual_bid_at": None,
        "due_from_estimator_at": None, "due_from_vendors_at": due_raw,
    }
    fake = _setup(monkeypatch, {
        ("projects", "select"): [project],
        ("profiles", "select"): [{"id": "pe1", "role": "pe"}],
        ("due_reminder_log", "upsert"): [],  # all duplicates
    }, NOW)

    dr.poll_once()

    assert _calls(fake, "due_reminder_log", "upsert")
    assert not _calls(fake, "notifications", "insert")


def test_poll_once_notification_failure_rolls_ledger_back(monkeypatch):
    due_raw = (NOW + timedelta(minutes=30)).isoformat()
    project = {
        "id": "p1", "name": "Acme", "number": None, "current_stage": "receive_quotes",
        "internal_bid_at": None, "actual_bid_at": None,
        "due_from_estimator_at": None, "due_from_vendors_at": due_raw,
    }

    def _boom(_payload):
        raise RuntimeError("insert failed")

    fake = _setup(monkeypatch, {
        ("projects", "select"): [project],
        ("profiles", "select"): [{"id": "pe1", "role": "pe"}],
        ("due_reminder_log", "upsert"): _echo_ledger,
        ("notifications", "insert"): _boom,
    }, NOW)

    dr.poll_once()  # must not raise — event failures are isolated

    [deleted] = _calls(fake, "due_reminder_log", "delete")
    assert deleted.in_args == [("id", ["L0"])]


def test_poll_once_complete_task_is_silent(monkeypatch):
    # due_from_estimator in window, but the project is already past to_estimator.
    due_raw = (NOW + timedelta(hours=36)).isoformat()
    project = {
        "id": "p1", "name": "Acme", "number": None, "current_stage": "rfqs",
        "internal_bid_at": None, "actual_bid_at": None,
        "due_from_estimator_at": due_raw, "due_from_vendors_at": None,
    }
    fake = _setup(monkeypatch, {
        ("projects", "select"): [project],
        ("project_files", "select"): [],
    }, NOW)

    dr.poll_once()

    assert not _calls(fake, "due_reminder_log", "upsert")
    assert not _calls(fake, "notifications", "insert")


def test_poll_once_estimator_added_via_assignment(monkeypatch):
    due_raw = (NOW + timedelta(hours=36)).isoformat()  # 2d window
    project = {
        "id": "p1", "name": "Acme", "number": None, "current_stage": "to_estimator",
        "internal_bid_at": None, "actual_bid_at": None,
        "due_from_estimator_at": due_raw, "due_from_vendors_at": None,
    }
    fake = _setup(monkeypatch, {
        ("projects", "select"): [project],
        ("project_files", "select"): [],
        ("profiles", "select"): [
            {"id": "pa1", "role": "pa"},
            {"id": "acct1", "role": "accountant"},
            {"id": "est1", "role": "estimator"},
        ],
        ("notification_prefs", "select"): [],
        ("estimator_assignments", "select"): [
            {"project_id": "p1", "estimator_id": "est1"},
            {"project_id": "p1", "estimator_id": "ghost"},  # not an active profile
        ],
        ("due_reminder_log", "upsert"): _echo_ledger,
        ("notifications", "insert"): [],
    }, NOW)

    dr.poll_once()

    [up] = _calls(fake, "due_reminder_log", "upsert")
    assert sorted(r["user_id"] for r in up.payload) == ["est1", "pa1"]
    assert {r["offset_key"] for r in up.payload} == {"2d"}


def test_poll_once_actual_bid_goes_only_to_pa(monkeypatch):
    due_raw = (NOW + timedelta(hours=5)).isoformat()  # 8h window
    project = {
        "id": "p1", "name": "Acme", "number": "7", "current_stage": "verify",
        "internal_bid_at": None, "actual_bid_at": due_raw,
        "due_from_estimator_at": None, "due_from_vendors_at": None,
    }
    fake = _setup(monkeypatch, {
        ("projects", "select"): [project],
        ("profiles", "select"): [
            {"id": "pa1", "role": "pa"}, {"id": "ex1", "role": "executive"},
        ],
        # The executive somehow stored actual_bid prefs — must still be excluded.
        ("notification_prefs", "select"): [
            {"user_id": "ex1", "prefs": {"actual_bid": {"offsets": ["8h"]}}},
        ],
        ("due_reminder_log", "upsert"): _echo_ledger,
        ("notifications", "insert"): [],
    }, NOW)

    dr.poll_once()

    [up] = _calls(fake, "due_reminder_log", "upsert")
    assert [r["user_id"] for r in up.payload] == ["pa1"]
    [ins] = _calls(fake, "notifications", "insert")
    assert ins.payload[0]["type"] == "due.actual_bid.8h"
    assert "due to the GC within 8 hours" in ins.payload[0]["message"]
