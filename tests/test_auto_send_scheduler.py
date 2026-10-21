"""Auto send poller: window math, claims, missed detection and the digest gate.

poll_once runs against an in-memory fake Supabase with the auto_send service
functions, Graph send_mail and notify_role all monkeypatched; nothing here
touches a database, Graph or the network.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.core import roles
from app.core.config import Settings
from app.core.roles import Role
from app.services import auto_send_scheduler as sched

TZ = ZoneInfo("America/Los_Angeles")

SETTINGS = SimpleNamespace(
    auto_send_poller_enabled=True,
    auto_send_poll_interval_seconds=30,
    auto_send_preflight_time="15:00",
    auto_send_send_time="17:05",
    auto_send_digest_time="17:10",
    auto_send_catchup_minutes=120,
    auto_send_missed_lookback_days=7,
    auto_send_gc_redirect="",
    frontend_url="https://bdr.example.com/",
)

NOTIFY_ROLES = frozenset(
    {Role.ESTIMATING_ADMIN, Role.ACCOUNTANT, Role.EXECUTIVE, Role.IT_ADMIN}
)

TUE = date(2026, 10, 20)  # a Tuesday, PDT


def _at(day: date, h: int, mi: int) -> datetime:
    """UTC instant of a Pacific wall-clock time."""
    return datetime(day.year, day.month, day.day, h, mi, tzinfo=TZ).astimezone(timezone.utc)


# ── In-memory fake Supabase ───────────────────────────────────────────────


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._op = "select"
        self._filters = []
        self._payload = None
        self._conflict = None
        self._range = None

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._filters.append(lambda r: r.get(col) in vals)
        return self

    def gte(self, col, val):
        self._filters.append(lambda r: r.get(col) is not None and str(r.get(col)) >= val)
        return self

    def lte(self, col, val):
        self._filters.append(lambda r: r.get(col) is not None and str(r.get(col)) <= val)
        return self

    def order(self, *a, **k):
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        assert ignore_duplicates, "claims must never overwrite"
        self._op = "upsert"
        self._payload = rows
        self._conflict = on_conflict.split(",")
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        self._db.calls.append((self._table, self._op))
        if (self._table, self._op) in self._db.fail:
            raise RuntimeError(f"{self._table} {self._op} down")
        rows = self._db.tables.setdefault(self._table, [])
        match = [r for r in rows if all(f(r) for f in self._filters)]
        if self._op == "select":
            out = [dict(r) for r in match]
            if self._range:
                out = out[self._range[0] : self._range[1] + 1]
            return _Result(out)
        if self._op == "delete":
            self._db.tables[self._table] = [r for r in rows if r not in match]
            return _Result(match)
        inserted = []
        for row in self._payload:
            key = tuple(row[c] for c in self._conflict)
            if any(tuple(r.get(c) for c in self._conflict) == key for r in rows):
                continue
            self._db.seq += 1
            new = {"id": f"{self._table}-{self._db.seq}", **row}
            rows.append(new)
            inserted.append(dict(new))
        return _Result(inserted)


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: set[tuple[str, str]] = set()
        self.seq = 0

    def table(self, name):
        return _Query(self, name)


# ── Fake auto_send service ────────────────────────────────────────────────


def _ready(pid):
    return {
        "project_id": pid,
        "outcome": "ready",
        "reasons": [],
        "markup_pct": "10.00",
        "amounts": {
            "material": "1100.00",
            "labor": "2200.00",
            "total": "3300.00",
            "base_material": "1000.00",
            "base_labor": "2000.00",
        },
        "gcs": [
            {"gc_id": "gc1", "gc_name": "Acme GC", "recipients": ["bids@acme.test"],
             "source": "bid_contacts", "reason": None},
        ],
    }


class FakeService:
    def __init__(self, db):
        self.db = db
        self.row = {"enabled": True, "enabled_changed_at": "2026-01-01T00:00:00Z"}
        self.row_error = False
        self.due: dict[date, list[dict]] = {}
        self.evals: dict[str, dict] = {}
        self.run_status = "sent"
        self.sends: list[tuple[str, date, str]] = []
        self.missed: list[tuple[str, date]] = []
        self.reclaims = 0

    def get_settings_row(self, sb=None):
        if self.row_error:
            raise RuntimeError("settings read failed")
        return self.row

    def reclaim_stuck(self, sb=None):
        self.reclaims += 1
        return 0

    def due_projects(self, day, sb=None):
        return [dict(p) for p in self.due.get(day, [])]

    def evaluate(self, project_id, *, sb=None):
        return self.evals.get(project_id) or _ready(project_id)

    def _claim(self, project_id, bid_date, status, reasons=None):
        runs = self.db.tables.setdefault("auto_send_runs", [])
        if any(r["project_id"] == project_id and r["bid_date"] == bid_date.isoformat()
               for r in runs):
            return None
        self.db.seq += 1
        run = {
            "id": f"run-{self.db.seq}",
            "project_id": project_id,
            "bid_date": bid_date.isoformat(),
            "status": status,
            "reasons": reasons or [],
        }
        runs.append(run)
        return run

    def run_send(self, project_id, bid_date, *, trigger="scheduled", actor_id=None,
                 force_unknown=False, sb=None):
        self.sends.append((project_id, bid_date, trigger))
        return self._claim(project_id, bid_date, self.run_status)

    def record_missed(self, project_id, bid_date, reasons, sb=None):
        run = self._claim(project_id, bid_date, "missed", reasons)
        if run:
            self.missed.append((project_id, bid_date))
        return run


PROFILES = [
    {"id": "u-ea", "full_name": "Pat Admin", "email": "pat@g3.test",
     "role": "estimating_admin", "is_active": True},
    {"id": "u-acct", "full_name": "Ash Books", "email": "ash@g3.test",
     "role": "accountant", "is_active": True},
    {"id": "u-exec", "full_name": "Alex Exec", "email": "alex@g3.test",
     "role": "executive", "is_active": True},
    {"id": "u-it", "full_name": "Ivy IT", "email": "ivy@g3.test",
     "role": "it_admin", "is_active": True},
    {"id": "u-eng", "full_name": "Eli Engineer", "email": "eli@g3.test",
     "role": "estimating_engineer_labor", "is_active": True},
    {"id": "u-est", "full_name": "Ext Estimator", "email": "est@x.test",
     "role": "estimator", "is_active": True},
    {"id": "u-gone", "full_name": "Old Exec", "email": "old@g3.test",
     "role": "executive", "is_active": False},
    {"id": "u-noemail", "full_name": "No Email", "email": None,
     "role": "executive", "is_active": True},
]
RECIPIENTS = {"pat@g3.test", "ash@g3.test", "alex@g3.test", "ivy@g3.test"}


def _project(pid="p1", name="Anita Gelato TI", number="7001",
             created_at="2026-09-01T00:00:00Z"):
    return {"id": pid, "name": name, "number": number,
            "internal_bid_at": "2026-10-20T21:00:00Z", "created_at": created_at,
            "current_stage": "send_out"}


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    db.tables["profiles"] = [dict(p) for p in PROFILES]
    db.tables["projects"] = [_project()]
    svc = FakeService(db)
    clock = {"now": _at(TUE, 12, 0)}
    mails: list[dict] = []
    notes: list[tuple] = []
    monkeypatch.setattr(roles, "AUTO_SEND_NOTIFY_ROLES", NOTIFY_ROLES)
    monkeypatch.setattr(sched, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(sched, "get_supabase", lambda: db)
    monkeypatch.setattr(sched, "_now", lambda: clock["now"])
    monkeypatch.setattr(sched, "_settled_days", {})

    env_state = {"mail_error": False}

    def _send_mail(**kw):
        if env_state["mail_error"]:
            raise RuntimeError("graph down")
        mails.append(kw)
        return {"id": "log"}

    monkeypatch.setattr(sched.graph_email, "send_mail", _send_mail)
    monkeypatch.setattr(sched, "notify_role", lambda *a, **k: notes.append((a, k)))
    for name in ("get_settings_row", "due_projects", "evaluate", "run_send",
                 "record_missed", "reclaim_stuck"):
        monkeypatch.setattr(sched.auto_send, name, getattr(svc, name))

    def tick(day, h, mi):
        clock["now"] = _at(day, h, mi)
        sched.poll_once()

    return SimpleNamespace(db=db, svc=svc, mails=mails, notes=notes, tick=tick,
                           state=env_state)


def _kinds(env, kind):
    return [r for r in env.db.tables.get("auto_send_email_log", []) if r["kind"] == kind]


# ── Preflight window ──────────────────────────────────────────────────────


def test_preflight_not_before_window(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 14, 59)
    assert not env.mails


def test_preflight_at_window_start_goes_to_notify_roles(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 15, 0)
    assert {m["to"][0] for m in env.mails} == RECIPIENTS
    for m in env.mails:
        assert m["importance"] == "high"
        assert m["subject"] == (
            "G3 BDR · [HIGH IMPORTANCE] Auto send at 5:05 PM today: Tuesday, October 20"
        )
        assert "cc" not in m and "project_id" not in m
        assert m["inline_images"][0][0] == "g3-logo"
        assert "HIGH IMPORTANCE: AUTO SEND AT 5:05 PM" in m["body_html"]
        assert "https://bdr.example.com/projects/p1?box=auto_send" in m["body_html"]
    assert len(_kinds(env, "preflight")) == 4
    assert not env.svc.sends  # the preflight never sends to GCs


def test_preflight_duplicate_tick_sends_once(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 15, 0)
    env.tick(TUE, 15, 1)
    env.tick(TUE, 16, 30)
    assert len(env.mails) == 4


def test_preflight_skipped_when_every_project_is_silent(env):
    env.svc.due[TUE] = [_project()]
    env.svc.evals["p1"] = {"outcome": "silent", "reasons": []}
    env.tick(TUE, 15, 0)
    assert not env.mails
    assert not _kinds(env, "preflight")  # nothing claimed either


def test_preflight_skipped_with_no_due_projects(env):
    env.tick(TUE, 15, 0)
    assert not env.mails


def test_preflight_send_failure_releases_claim_then_retries(env):
    env.svc.due[TUE] = [_project()]
    env.state["mail_error"] = True
    env.tick(TUE, 15, 0)
    assert not env.mails
    assert not _kinds(env, "preflight")  # every claim rolled back
    env.state["mail_error"] = False
    env.tick(TUE, 15, 1)
    assert {m["to"][0] for m in env.mails} == RECIPIENTS


def test_preflight_stops_at_send_time(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 17, 5)
    assert not [m for m in env.mails if "Auto send at" in m["subject"]]


# ── Send window ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("h", "mi", "sends"),
    [(17, 4, False), (17, 5, True), (19, 4, True), (19, 5, False)],
)
def test_send_window_bounds(env, h, mi, sends):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, h, mi)
    assert bool(env.svc.sends) is sends
    if sends:
        assert env.svc.sends == [("p1", TUE, "scheduled")]


def test_send_closed_window_records_missed_and_notifies_ea_once(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 19, 5)
    assert not env.svc.sends
    assert env.svc.missed == [("p1", TUE)]
    [(args, _kw)] = env.notes
    assert args[0] == Role.ESTIMATING_ADMIN
    assert args[1] == "p1" and args[2] == "auto_send.missed"
    assert "Anita Gelato TI (7001)" in args[3] and "5:05 PM" in args[3]
    [run] = env.db.tables["auto_send_runs"]
    assert run["status"] == "missed" and run["reasons"][0]["code"] == "missed"
    # later ticks (and the other worker) never notify again
    env.tick(TUE, 19, 6)
    env.tick(TUE, 23, 59)
    assert len(env.notes) == 1


def test_missed_lost_race_does_not_notify(env, monkeypatch):
    env.svc.due[TUE] = [_project()]
    # the other worker won the claim
    monkeypatch.setattr(sched.auto_send, "record_missed", lambda *a, **k: None)
    env.tick(TUE, 19, 30)
    assert not env.notes


def test_send_never_runs_next_day(env):
    env.svc.due[TUE] = [_project()]
    env.tick(date(2026, 10, 21), 17, 5)  # Wednesday: Tuesday's project
    assert not env.svc.sends
    assert env.svc.missed == [("p1", TUE)]


def test_send_skips_project_with_a_run_row(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 17, 5)
    env.tick(TUE, 17, 6)
    assert len(env.svc.sends) == 1


def test_send_skips_silent_project(env):
    env.svc.due[TUE] = [_project()]
    env.svc.evals["p1"] = {"outcome": "silent", "reasons": []}
    env.tick(TUE, 17, 5)
    env.tick(TUE, 19, 30)
    assert not env.svc.sends and not env.svc.missed and not env.notes


def test_send_one_failing_project_never_stops_the_rest(env, monkeypatch):
    env.svc.due[TUE] = [_project("p1"), _project("p2", name="Second")]
    real = env.svc.run_send

    def flaky(pid, *a, **k):
        if pid == "p1":
            raise RuntimeError("boom")
        return real(pid, *a, **k)

    monkeypatch.setattr(sched.auto_send, "run_send", flaky)
    env.tick(TUE, 17, 5)
    assert env.svc.sends == [("p2", TUE, "scheduled")]


def test_weekend_fires(env):
    sat = date(2026, 10, 24)
    env.svc.due[sat] = [_project()]
    env.tick(sat, 15, 0)
    assert len(env.mails) == 4
    env.tick(sat, 17, 5)
    assert env.svc.sends == [("p1", sat, "scheduled")]


@pytest.mark.parametrize(
    ("day", "send_utc"),
    [
        # Spring forward (2026-03-08): 17:05 PDT is 00:05 UTC on the 9th.
        (date(2026, 3, 8), datetime(2026, 3, 9, 0, 5, tzinfo=timezone.utc)),
        # Fall back (2026-11-01): 17:05 PST is 01:05 UTC on the 2nd.
        (date(2026, 11, 1), datetime(2026, 11, 2, 1, 5, tzinfo=timezone.utc)),
    ],
)
def test_dst_days_use_wall_clock(env, monkeypatch, day, send_utc):
    env.svc.due[day] = [_project()]
    assert sched._instant(day, (17, 5)) == send_utc
    monkeypatch.setattr(sched, "_now", lambda: send_utc - timedelta(minutes=1))
    sched.poll_once()
    assert not env.svc.sends
    monkeypatch.setattr(sched, "_now", lambda: send_utc)
    sched.poll_once()
    assert env.svc.sends == [("p1", day, "scheduled")]


# ── Switch / settings ─────────────────────────────────────────────────────


def test_disabled_does_nothing(env):
    env.svc.row = {"enabled": False, "enabled_changed_at": None}
    env.svc.due[TUE] = [_project()]
    for h, mi in ((15, 0), (17, 5), (17, 45), (19, 30)):
        env.tick(TUE, h, mi)
    assert not env.mails and not env.svc.sends and not env.svc.missed and not env.notes
    assert env.svc.reclaims == 4  # stuck runs are still reclaimed
    assert not env.db.calls


def test_settings_read_failure_does_nothing(env):
    env.svc.row_error = True
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 17, 5)
    assert not env.svc.sends and env.svc.reclaims == 0 and not env.db.calls


def test_reclaim_failure_does_not_stop_the_tick(env, monkeypatch):
    def boom(sb=None):
        raise RuntimeError("reclaim failed")

    monkeypatch.setattr(sched.auto_send, "reclaim_stuck", boom)
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 17, 5)
    assert env.svc.sends


# ── Missed lookback ───────────────────────────────────────────────────────


def test_missed_respects_enabled_changed_at(env):
    mon = date(2026, 10, 19)
    env.svc.due[mon] = [_project("p-mon")]
    # switched on Tuesday morning: Monday's 5:05 PM was before the switch
    env.svc.row = {"enabled": True, "enabled_changed_at": "2026-10-20T16:00:00Z"}
    env.tick(TUE, 9, 0)
    assert not env.svc.missed and not env.notes


def test_missed_lookback_catches_earlier_day(env):
    mon = date(2026, 10, 19)
    env.svc.due[mon] = [_project("p-mon")]
    env.tick(TUE, 9, 0)
    assert env.svc.missed == [("p-mon", mon)]
    assert len(env.notes) == 1


def test_missed_lookback_is_bounded(env):
    old = date(2026, 10, 12)  # 8 days before Tuesday, outside the 7 day lookback
    env.svc.due[old] = [_project("p-old")]
    env.tick(TUE, 9, 0)
    assert not env.svc.missed


def test_missed_ignores_project_created_after_send_instant(env):
    mon = date(2026, 10, 19)
    late = _project("p-late", created_at="2026-10-20T01:00:00Z")  # Mon 6:00 PM PDT
    early = _project("p-early", created_at="2026-10-19T23:00:00Z")  # Mon 4:00 PM PDT
    env.svc.due[mon] = [late, early]
    env.tick(TUE, 9, 0)
    assert env.svc.missed == [("p-early", mon)]


def test_missed_fetches_created_at_when_due_rows_lack_it(env):
    mon = date(2026, 10, 19)
    row = _project("p-x")
    row.pop("created_at")
    env.svc.due[mon] = [row]
    env.db.tables["projects"] = [{"id": "p-x", "created_at": "2026-09-01T00:00:00Z"}]
    env.tick(TUE, 9, 0)
    assert env.svc.missed == [("p-x", mon)]


def test_missed_skips_project_with_a_run(env):
    mon = date(2026, 10, 19)
    env.svc.due[mon] = [_project("p-mon")]
    env.db.tables["auto_send_runs"] = [
        {"id": "r0", "project_id": "p-mon", "bid_date": "2026-10-19", "status": "failed"}
    ]
    env.tick(TUE, 9, 0)
    assert not env.svc.missed


def test_missed_past_day_is_not_requeried_every_tick(env, monkeypatch):
    mon = date(2026, 10, 19)
    calls = []
    real = env.svc.due_projects

    def counting(day, sb=None):
        calls.append(day)
        return real(day, sb=sb)

    monkeypatch.setattr(sched.auto_send, "due_projects", counting)
    env.svc.due[mon] = [_project("p-mon")]
    env.tick(TUE, 9, 0)
    env.tick(TUE, 9, 1)
    assert calls.count(mon) == 1


# ── Digest gate ───────────────────────────────────────────────────────────


def _digests(env):
    return [m for m in env.mails if "Auto send results" in m["subject"]]


def test_digest_after_send(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 17, 5)
    env.tick(TUE, 17, 9)
    assert not _digests(env)
    env.tick(TUE, 17, 10)
    digests = _digests(env)
    assert {m["to"][0] for m in digests} == RECIPIENTS
    for m in digests:
        assert m["importance"] == "high"
        assert m["subject"] == "G3 BDR · [HIGH IMPORTANCE] Auto send results: Tuesday, October 20"
        assert "HIGH IMPORTANCE: AUTO SEND RESULTS" in m["body_html"]
        assert "Anita Gelato TI" in m["body_html"]
    env.tick(TUE, 17, 11)
    assert len(_digests(env)) == 4  # claimed: one per person per day


def test_digest_waits_for_sending_until_hard_deadline(env):
    env.svc.due[TUE] = [_project()]
    env.svc.run_status = "sending"
    env.tick(TUE, 17, 5)
    env.tick(TUE, 17, 10)
    env.tick(TUE, 17, 39)
    assert not _digests(env)
    env.tick(TUE, 17, 40)
    assert len(_digests(env)) == 4
    assert "Still sending" in _digests(env)[0]["body_html"]


def test_digest_fires_once_sending_settles(env):
    env.svc.due[TUE] = [_project()]
    env.svc.run_status = "sending"
    env.tick(TUE, 17, 5)
    env.tick(TUE, 17, 12)
    assert not _digests(env)
    env.db.tables["auto_send_runs"][0]["status"] = "sent"
    env.tick(TUE, 17, 13)
    assert len(_digests(env)) == 4


def test_no_digest_without_runs(env):
    env.tick(TUE, 17, 30)
    env.tick(TUE, 21, 0)
    assert not env.mails


def test_digest_includes_missed_after_late_restart(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 20, 0)  # server came back after the catch-up window
    assert env.svc.missed == [("p1", TUE)]
    digests = _digests(env)
    assert len(digests) == 4
    assert "Missed" in digests[0]["body_html"]


def test_digest_send_failure_releases_claim(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 17, 5)
    env.state["mail_error"] = True
    env.tick(TUE, 17, 10)
    assert not _kinds(env, "digest")
    env.state["mail_error"] = False
    env.tick(TUE, 17, 11)
    assert len(_digests(env)) == 4


def test_digest_batches_gc_rows(env):
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 17, 5)
    run_id = env.db.tables["auto_send_runs"][0]["id"]
    env.db.tables["auto_send_gcs"] = [
        {"id": "g1", "run_id": run_id, "gc_id": "gc1", "gc_name": "Acme GC",
         "status": "sent", "recipients": "bids@acme.test", "error": None},
    ]
    env.tick(TUE, 17, 10)
    body = _digests(env)[0]["body_html"]
    assert "Acme GC" in body and "bids@acme.test" in body
    assert env.db.calls.count(("auto_send_gcs", "select")) == 1


# ── Boot validation (config.py) ───────────────────────────────────────────


def test_config_defaults():
    # Field defaults (the suite's env pins override the live values).
    defaults = {k: f.default for k, f in Settings.model_fields.items() if k.startswith("auto_send_")}
    assert defaults == {
        "auto_send_poller_enabled": True,
        "auto_send_poll_interval_seconds": 30,
        "auto_send_preflight_time": "15:00",
        "auto_send_send_time": "17:05",
        "auto_send_digest_time": "17:10",
        "auto_send_catchup_minutes": 120,
        "auto_send_missed_lookback_days": 7,
        "auto_send_gc_redirect": "",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"auto_send_send_time": "5:05 PM"},
        {"auto_send_preflight_time": "25:00"},
        {"auto_send_digest_time": "17:10,17:20"},
        {"auto_send_preflight_time": "17:05"},  # not before the send
        {"auto_send_digest_time": "17:00"},  # before the send
        {"auto_send_catchup_minutes": 0},
        {"auto_send_send_time": "23:00", "auto_send_digest_time": "23:30",
         "auto_send_catchup_minutes": 61},  # would run past midnight
        {"auto_send_poll_interval_seconds": 1},
        {"auto_send_missed_lookback_days": -1},
        {"auto_send_gc_redirect": "not-an-address"},
    ],
)
def test_config_refuses_bad_schedule(overrides):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)


def test_config_accepts_redirect_address():
    s = Settings(_env_file=None, auto_send_gc_redirect="qa@g3electrical.com")
    assert s.auto_send_gc_redirect == "qa@g3electrical.com"


def test_conftest_pins_poller_off():
    import os

    assert os.environ["AUTO_SEND_POLLER_ENABLED"] == "false"
    assert os.environ["AUTO_SEND_GC_REDIRECT"] == ""


# ── review fixes (2026-10-19) ─────────────────────────────────────────────


def test_switch_off_mid_tick_stops_the_remaining_projects(env, monkeypatch):
    """Findings 7 / 14: the switch is re-read before every project."""
    env.svc.due[TUE] = [_project("p1"), _project("p2", name="Second")]
    real = env.svc.run_send

    def _send_then_switch_off(pid, *a, **k):
        out = real(pid, *a, **k)
        env.svc.row = {"enabled": False, "enabled_changed_at": "2026-10-21T00:06:00Z"}
        return out

    monkeypatch.setattr(sched.auto_send, "run_send", _send_then_switch_off)
    env.tick(TUE, 17, 5)
    assert env.svc.sends == [("p1", TUE, "scheduled")]
    # Nothing claimed for p2, so it is neither sent later nor reported missed
    # (the switch was off at its send time).
    assert [r["project_id"] for r in env.db.tables["auto_send_runs"]] == ["p1"]


def test_switch_read_failure_mid_tick_counts_as_off(env, monkeypatch):
    env.svc.due[TUE] = [_project("p1"), _project("p2", name="Second")]
    real = env.svc.run_send

    def _send_then_break(pid, *a, **k):
        out = real(pid, *a, **k)
        env.svc.row_error = True
        return out

    monkeypatch.setattr(sched.auto_send, "run_send", _send_then_break)
    env.tick(TUE, 17, 5)
    assert env.svc.sends == [("p1", TUE, "scheduled")]


@pytest.mark.parametrize(
    ("changed_at", "sends"),
    [
        ("2026-10-21T00:45:00Z", False),  # turned on at 17:45 PT, after 17:05
        ("2026-10-21T00:05:00Z", False),  # turned on exactly at the send instant
        ("2026-10-20T16:00:00Z", True),   # on since 09:00 PT: a real catch-up
        (None, True),                     # never flipped
    ],
)
def test_switching_on_after_the_send_time_does_not_send_today(env, changed_at, sends):
    """Finding 8: catch-up is for a server that was down at 5:05 PM, never for a
    switch that was off then."""
    env.svc.row = {"enabled": True, "enabled_changed_at": changed_at}
    env.svc.due[TUE] = [_project()]
    env.tick(TUE, 17, 46)
    assert bool(env.svc.sends) is sends
    # Not owed, so not missed either once the window closes.
    env.tick(TUE, 19, 30)
    assert bool(env.svc.missed) is False


def test_window_closing_mid_loop_stops_the_send(env, monkeypatch):
    """Finding 16: never claim a scheduled send after the catch-up window."""
    env.svc.due[TUE] = [_project("p1"), _project("p2", name="Second")]
    real = env.svc.run_send

    def _slow_send(pid, *a, **k):
        out = real(pid, *a, **k)
        monkeypatch.setattr(sched, "_now", lambda: _at(TUE, 19, 5))
        return out

    monkeypatch.setattr(sched.auto_send, "run_send", _slow_send)
    monkeypatch.setattr(sched, "_now", lambda: _at(TUE, 19, 4))
    sched.poll_once()
    assert env.svc.sends == [("p1", TUE, "scheduled")]


def test_send_waits_while_another_date_run_of_the_project_is_sending(env):
    """Finding 4: a Send now still going on an older run must finish first."""
    env.svc.due[TUE] = [_project()]
    env.db.tables["auto_send_runs"] = [
        {"id": "run-old", "project_id": "p1", "bid_date": "2026-10-13", "status": "sending"}
    ]
    env.tick(TUE, 17, 5)
    assert not env.svc.sends
    env.db.tables["auto_send_runs"][0]["status"] = "partial"
    env.tick(TUE, 17, 6)
    assert env.svc.sends == [("p1", TUE, "scheduled")]


def test_one_failing_evaluation_never_drops_the_preflight(env, monkeypatch):
    """Finding 11: the project that could not be checked is listed as will
    not auto send; the others are still reported."""
    env.svc.due[TUE] = [_project("p1"), _project("p2", name="Second Job", number="7002")]
    real = env.svc.evaluate

    def flaky(pid, *, sb=None):
        if pid == "p1":
            raise RuntimeError("read timeout")
        return real(pid, sb=sb)

    monkeypatch.setattr(sched.auto_send, "evaluate", flaky)
    env.tick(TUE, 15, 0)
    assert {m["to"][0] for m in env.mails} == RECIPIENTS
    body = env.mails[0]["body_html"]
    assert "Second Job" in body
    assert sched.CHECK_FAILED_MESSAGE in body
