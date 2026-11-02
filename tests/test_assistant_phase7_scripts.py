"""Phase 7 WI-9 scripts (docs/ASSISTANT_PHASE7_PLAN.md 11.3, 11.5 steps 9, 9b and 10):
scripts/assistant_proactive_tick.py and scripts/check_proactive_claims.py.

No database, Graph or Teams call and no process is started. The guards are
pure functions over a settings object; the claim check's seeding, assertion
and cleanup logic runs against the FakeDB emulation of 0190 with two simulated
workers (threads sharing one FakeDB) in place of the two spawned processes.

Pinned here:

  tick guards    wrong ref, the prod ref, a local or lookalike URL, a missing
                 allowlist, a --now outside [real now - 24 h, real now] with
                 the tails on, a --now outside [real now - 30 days, real now
                 + 8 days] either way, a bad test email override, the router
                 env off; a run that can send needs the owner's override, a
                 tick at the real now, the RFP bench off and the two e2e
                 accounts; naive --now is Pacific;
  tick seed      source 'emit', an explicit occurred_at, allowlisted users
                 only, ids printed; --no-tails passes tails=False and never
                 reads the cursors; a refusal calls nothing;
  claims guards  dev ref only, both global channel switches off, user id
                 parsing, the worker env (external on, both switches off,
                 allowlist, no test override);
  claims check   two workers over one FakeDB pass every check, then every row
                 is gone; each check fails when its invariant is broken; a
                 refusal writes nothing (a --now outside the travel window, a
                 todo.due subscription row off the code default, an existing
                 digest slot row, a pending or lease-expired event); a failing
                 runner still cleans up; cleanup deletes only events the run
                 created, including one a retry handed back.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.core import supabase_client
from app.core.config import get_settings
from app.services import assistant_delivery
from app.services import assistant_proactive as svc
from scripts import assistant_proactive_tick as tick
from scripts import check_proactive_claims as claims
from tests import assistant_fakes as fk
from tests.assistant_fakes import USER, FakeDB

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
EM_DASH = chr(0x2014)
DEV_URL = "https://bpidntbyvoooqvaispup.supabase.co"
PROD_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
USER2 = "a0000000-0000-4000-8000-000000000003"
FRI_705 = datetime(2026, 10, 30, 7, 5, tzinfo=PT).astimezone(UTC)
REAL_NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
SCRIPTS = Path(tick.__file__).parent


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    """Both scripts read the wall clock through _real_now; pin it so the
    fixed dates below stay inside the travel window."""
    monkeypatch.setattr(tick, "_real_now", lambda: REAL_NOW)
    monkeypatch.setattr(claims, "_real_now", lambda: REAL_NOW)


def settings(**over) -> SimpleNamespace:
    base = dict(
        supabase_url=DEV_URL, assistant_enabled=True, assistant_proactive_enabled=True,
        assistant_proactive_external_enabled=True, assistant_proactive_user_allowlist=USER,
        assistant_proactive_test_email_to="", notification_emails_enabled=False, teams_alerts_enabled=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── The tick script ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://abcdefghijklmnopqrst.supabase.co", "http://127.0.0.1:54321", "", None,
    "https://bpidntbyvoooqvaispup.supabase.co.evil.test", "https://evil.test/bpidntbyvoooqvaispup",
    "https://bpidntbyvoooqvaispup.evil.test", "https://xbpidntbyvoooqvaispup.supabase.co",
])
def test_the_ref_guard_refuses_anything_but_the_dev_project(url):
    with pytest.raises(tick.Refused, match="not the dev project"):
        tick.require_dev_ref(settings(supabase_url=url))


def test_the_ref_guard_names_the_prod_project_and_passes_dev():
    with pytest.raises(tick.Refused, match="production"):
        tick.require_dev_ref(settings(supabase_url=PROD_URL))
    with pytest.raises(tick.Refused, match="production"):
        tick.require_dev_ref(settings(supabase_url=DEV_URL + "/?x=zxqkqotlcgbalwoqeeca"))
    tick.require_dev_ref(settings())
    tick.require_dev_ref(settings(supabase_url=DEV_URL.upper()))


@pytest.mark.parametrize("raw", ["", " ", ",", " , ,", None])
def test_a_missing_allowlist_is_refused(raw):
    with pytest.raises(tick.Refused, match="ALLOWLIST"):
        tick.require_allowlist(settings(assistant_proactive_user_allowlist=raw))


def test_the_allowlist_is_lowercased_ids():
    assert tick.require_allowlist(settings(assistant_proactive_user_allowlist=" AA-1 , bb-2 ,")) == ["aa-1", "bb-2"]


@pytest.mark.parametrize("override", ["someone@example.com", "baseballtom33@gmail.com.evil.test", "x@gmail.com",
                                      "baseballtom33@gmail.com,other@example.com"])
def test_a_test_email_that_is_not_the_owners_is_refused(override):
    with pytest.raises(tick.Refused, match="TEST_EMAIL_TO"):
        tick.require_test_email(settings(assistant_proactive_test_email_to=override))


@pytest.mark.parametrize("override", ["", None, "baseballtom33@gmail.com", " BaseballTom33@Gmail.com "])
def test_the_owners_address_or_nothing_passes(override):
    tick.require_test_email(settings(assistant_proactive_test_email_to=override))


def test_the_router_env_must_be_on():
    for over in ({"assistant_enabled": False}, {"assistant_proactive_enabled": False}):
        with pytest.raises(tick.Refused, match="ASSISTANT_PROACTIVE_ENABLED"):
            tick.require_router_env(settings(**over))
    tick.require_router_env(settings())


def test_the_tail_window_is_the_last_24_hours():
    real = REAL_NOW
    for ok in (real, real - timedelta(hours=24), real - timedelta(hours=1)):
        tick.require_tail_window(ok, real)
    for bad in (real + timedelta(seconds=1), real - timedelta(hours=24, seconds=1), real + timedelta(days=3)):
        with pytest.raises(tick.Refused, match="24 hours"):
            tick.require_tail_window(bad, real)


def test_check_guards_applies_the_window_only_with_the_tails_on():
    far = REAL_NOW + timedelta(days=5)
    with pytest.raises(tick.Refused, match="24 hours"):
        tick.check_guards(settings(), now=far, tails=True, real_now=REAL_NOW)
    assert tick.check_guards(settings(), now=far, tails=False, real_now=REAL_NOW) == [USER]
    assert tick.check_guards(settings(), now=REAL_NOW - timedelta(hours=2), tails=True, real_now=REAL_NOW) == [USER]


def test_check_guards_runs_every_refusal_first():
    far = REAL_NOW + timedelta(days=5)
    for over, match in (({"supabase_url": PROD_URL}, "production"),
                        ({"assistant_proactive_user_allowlist": ""}, "ALLOWLIST"),
                        ({"assistant_proactive_test_email_to": "a@b.co"}, "TEST_EMAIL_TO"),
                        ({"assistant_enabled": False}, "ASSISTANT_PROACTIVE_ENABLED")):
        with pytest.raises(tick.Refused, match=match):
            tick.check_guards(settings(**over), now=far, tails=False, real_now=REAL_NOW)


def test_a_naive_time_is_pacific_and_an_aware_time_is_kept():
    assert tick.parse_when("2026-10-30T19:30") == datetime(2026, 10, 30, 19, 30, tzinfo=PT)
    assert tick.parse_when("2026-10-30T19:30").utcoffset() == timedelta(0)
    assert tick.parse_when("2026-10-30T19:30:00Z") == datetime(2026, 10, 30, 19, 30, tzinfo=UTC)
    assert tick.parse_when("2026-10-30T19:30:00-07:00") == datetime(2026, 10, 31, 2, 30, tzinfo=UTC)
    # Across the November change: 01:30 on 11-01 is still PDT, 07:00 on 11-02 is PST.
    assert tick.parse_when("2026-11-02T07:00") == datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    for bad in ("", "  ", "tomorrow", "2026-13-01"):
        with pytest.raises(ValueError):
            tick.parse_when(bad)


def test_the_arguments():
    parser = tick.build_parser()
    args = parser.parse_args(["--now", "2026-10-30T07:00", "--no-tails", "--seed", "x.json", "--worker", "w1"])
    assert args.now == datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
    assert args.no_tails is True and args.seed == "x.json" and args.worker == "w1"
    args = parser.parse_args(["--now", "2026-10-30T07:00Z"])
    assert args.no_tails is False and args.seed is None and args.worker is None
    for bad in ([], ["--now", "soon"], ["--no-tails"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)
    assert tick.default_worker().startswith("tick-script:") and tick.default_worker() != tick.default_worker()


def write_seed(tmp_path, events) -> str:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    return str(path)


def test_a_seed_file_becomes_emit_events_for_allowlisted_users(tmp_path):
    path = write_seed(tmp_path, [{
        "event_type": "todo.due", "subject_user_id": USER.upper(), "occurred_at": "2026-10-30T19:30",
        "payload": {"todo_id": "101", "due": "2026-10-30", "overdue": False, "title": "free text is dropped"},
    }])
    (row,) = tick.load_seed(path, [USER])
    assert row["source"] == "emit" and row["status"] == "pending" and row["subject_user_id"] == USER
    assert row["dedupe_key"] == "p7:todo.due:101:2026-10-30"
    assert row["payload"] == {"todo_id": "101", "due": "2026-10-30", "overdue": False}
    assert row["occurred_at"] == datetime(2026, 10, 31, 2, 30, tzinfo=UTC).isoformat()
    explicit = write_seed(tmp_path, {"events": [{
        "event_type": "todo.due", "subject_user_id": USER, "occurred_at": "2026-10-30T19:30Z",
        "dedupe_key": "p7:todo.due:custom", "payload": {"todo_id": "101", "due": "2026-10-30"}}]})
    assert tick.load_seed(explicit, [USER])[0]["dedupe_key"] == "p7:todo.due:custom"


@pytest.mark.parametrize("event, match", [
    ({"event_type": "todo.due", "subject_user_id": "someone-else", "occurred_at": "2026-10-30T19:30"}, "allowlist"),
    ({"event_type": "todo.due", "occurred_at": "2026-10-30T19:30"}, "allowlist"),
    ({"event_type": "todo.due", "subject_user_id": USER}, "occurred_at"),
    ({"event_type": "todo.due", "subject_user_id": USER, "occurred_at": "soon"}, "ISO"),
    ({"event_type": "not.a.kind", "subject_user_id": USER, "occurred_at": "2026-10-30T19:30"}, "event_type"),
    ({"event_type": "todo.due", "subject_user_id": USER, "occurred_at": "2026-10-30T19:30", "status": "processed"},
     "unknown fields"),
    ({"event_type": "todo.due", "subject_user_id": USER, "occurred_at": "2026-10-30T19:30", "payload": {}},
     "missing"),
])
def test_a_bad_seed_event_is_refused(tmp_path, event, match):
    with pytest.raises(tick.Refused, match=match):
        tick.load_seed(write_seed(tmp_path, [event]), [USER])


def test_an_empty_seed_file_is_refused(tmp_path):
    for body in ([], {"events": []}, {"events": "x"}):
        with pytest.raises(tick.Refused, match="non-empty"):
            tick.load_seed(write_seed(tmp_path, body), [USER])


class TickWorld:
    """main() with the settings, the DB and poll_once replaced."""

    def __init__(self, monkeypatch, **over):
        self.db = FakeDB(now=FRI_705, assistant_settings=[fk.settings_row(proactive_enabled=True)],
                         assistant_router_cursors=[fk.cursor_row(name="notifications"), fk.cursor_row(name="audit_log")])
        self.s = settings(**over)
        self.polls: list[tuple] = []
        monkeypatch.setattr("app.core.config.get_settings", lambda: self.s)
        monkeypatch.setattr(supabase_client, "get_supabase", lambda: self.db)
        monkeypatch.setattr(svc, "poll_once", self._poll)

    def _poll(self, now, *, worker, tails):
        self.polls.append((now, worker, tails))
        return {"switch_on": True, "route": {"processed": 1}, "sends": {"claimed": 0, "sent": 0}}


def test_main_runs_one_tick_with_the_tails_off(monkeypatch, capsys, tmp_path):
    w = TickWorld(monkeypatch)
    seed = write_seed(tmp_path, [{"event_type": "todo.due", "subject_user_id": USER,
                                  "occurred_at": "2026-10-30T19:30", "payload": {"todo_id": "101", "due": "2026-10-30"}}])
    code = tick.main(["--now", "2026-11-02T07:00", "--no-tails", "--seed", seed, "--worker", "w-test"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert w.polls == [(datetime(2026, 11, 2, 15, 0, tzinfo=UTC), "w-test", False)]
    assert out["tails"] is False and out["worker"] == "w-test" and out["external"] is True
    assert out["poll"] == {"switch_on": True, "route": {"processed": 1}} and out["sends"] == {"claimed": 0, "sent": 0}
    (event,) = w.db.tables["assistant_events"]
    assert out["seeded_event_ids"] == [event["id"]]
    assert event["source"] == "emit" and event["status"] == "pending" and event["subject_user_id"] == USER
    # The cursors are never read or written with the tails off.
    assert [r["cursor_at"] for r in w.db.tables["assistant_router_cursors"]] == [fk.P7_T0.isoformat()] * 2


def test_main_seeds_nothing_twice(monkeypatch, capsys, tmp_path):
    w = TickWorld(monkeypatch)
    seed = write_seed(tmp_path, [{"event_type": "todo.due", "subject_user_id": USER,
                                  "occurred_at": "2026-10-30T19:30", "payload": {"todo_id": "101", "due": "2026-10-30"}}])
    tick.main(["--now", "2026-10-30T19:30", "--no-tails", "--seed", seed])
    first = json.loads(capsys.readouterr().out)
    tick.main(["--now", "2026-10-30T19:30", "--no-tails", "--seed", seed])
    second = json.loads(capsys.readouterr().out)
    assert len(first["seeded_event_ids"]) == 1 and second["seeded_event_ids"] == []
    assert second["seed_existing_keys"] == ["p7:todo.due:101:2026-10-30"]
    assert len(w.db.tables["assistant_events"]) == 1


def test_main_runs_the_tails_for_a_recent_time(monkeypatch, capsys):
    w = TickWorld(monkeypatch)
    recent = (REAL_NOW - timedelta(hours=2)).isoformat()
    assert tick.main(["--now", recent]) == 0
    assert w.polls[0][2] is True
    assert json.loads(capsys.readouterr().out)["tails"] is True


@pytest.mark.parametrize("over, argv, message", [
    ({"supabase_url": PROD_URL}, ["--now", "2026-10-30T19:30", "--no-tails"], "production"),
    ({"supabase_url": "http://127.0.0.1:54321"}, ["--now", "2026-10-30T19:30", "--no-tails"], "dev project"),
    ({"assistant_proactive_user_allowlist": ""}, ["--now", "2026-10-30T19:30", "--no-tails"], "ALLOWLIST"),
    ({"assistant_proactive_test_email_to": "x@example.com"}, ["--now", "2026-10-30T19:30", "--no-tails"],
     "TEST_EMAIL_TO"),
    ({}, ["--now", "2020-01-01T07:00"], "24 hours"),
    ({}, ["--now", "2099-01-01T07:00"], "24 hours"),
    ({"assistant_proactive_enabled": False}, ["--now", "2026-10-30T19:30", "--no-tails"], "ASSISTANT_PROACTIVE_ENABLED"),
])
def test_main_refuses_before_touching_anything(monkeypatch, capsys, tmp_path, over, argv, message):
    w = TickWorld(monkeypatch, **over)
    seed = write_seed(tmp_path, [{"event_type": "todo.due", "subject_user_id": USER,
                                  "occurred_at": "2026-10-30T19:30", "payload": {"todo_id": "101", "due": "2026-10-30"}}])
    assert tick.main([*argv, "--seed", seed]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and message in captured.err
    assert w.polls == [] and w.db.tables.get("assistant_events", []) == []


def test_main_refuses_a_seed_for_someone_off_the_allowlist(monkeypatch, capsys, tmp_path):
    w = TickWorld(monkeypatch)
    seed = write_seed(tmp_path, [{"event_type": "todo.due", "subject_user_id": USER2,
                                  "occurred_at": "2026-10-30T19:30", "payload": {"todo_id": "101", "due": "2026-10-30"}}])
    assert tick.main(["--now", "2026-10-30T19:30", "--no-tails", "--seed", seed]) == 2
    assert "allowlist" in capsys.readouterr().err and w.polls == []


# ── The tick script: the travel window and a run that can send ───────────────


def test_the_travel_window_bounds_every_tick():
    real = REAL_NOW
    for ok in (real - timedelta(days=30), real + timedelta(days=8), real + timedelta(days=7, hours=21)):
        tick.require_travel_window(ok, real)
    for bad in (real - timedelta(days=30, seconds=1), real + timedelta(days=8, seconds=1),
                real + timedelta(days=368), real - timedelta(days=400)):
        with pytest.raises(tick.Refused, match="30 days before and 8 days after"):
            tick.require_travel_window(bad, real)
    # --no-tails no longer frees --now.
    with pytest.raises(tick.Refused, match="purge"):
        tick.check_guards(settings(), now=REAL_NOW + timedelta(days=9), tails=False, real_now=REAL_NOW)
    with pytest.raises(tick.Refused, match="purge"):
        tick.check_guards(settings(), now=REAL_NOW - timedelta(days=31), tails=False, real_now=REAL_NOW)


@pytest.mark.parametrize("when", ["2027-11-03T07:00", "2026-11-09T07:00", "2026-09-30T07:00"])
def test_main_refuses_a_far_no_tails_time_before_the_purge_can_run(monkeypatch, capsys, when):
    w = TickWorld(monkeypatch)
    assert tick.main(["--now", when, "--no-tails"]) == 2
    assert "8 days after" in capsys.readouterr().err and w.polls == []


def test_the_live_channels_come_from_the_send_step_inputs():
    assert tick.live_channels(settings()) == []
    assert tick.live_channels(settings(notification_emails_enabled=True)) == []          # no Graph creds
    assert tick.live_channels(settings(notification_emails_enabled=True, ms_client_id="x")) == ["email"]
    assert tick.live_channels(settings(teams_alerts_enabled=True)) == ["teams"]
    assert tick.live_channels(settings(notification_emails_enabled=True, ms_client_id="x",
                                       teams_alerts_enabled=True)) == ["email", "teams"]
    # The external switch off settles everything channel_off.
    assert tick.live_channels(settings(assistant_proactive_external_enabled=False, notification_emails_enabled=True,
                                       ms_client_id="x", teams_alerts_enabled=True)) == []


LIVE_EMAIL = {"notification_emails_enabled": True, "ms_client_id": "x"}
LIVE_TEAMS = {"teams_alerts_enabled": True}
OWNER = {"assistant_proactive_test_email_to": "baseballtom33@gmail.com"}


@pytest.mark.parametrize("live", [LIVE_EMAIL, LIVE_TEAMS])
def test_a_run_that_can_send_needs_the_owners_override(live):
    # The review scenario: an empty override and a travelled clock.
    for override in ("", " ", "BaseballTom33@gmail.com"):
        with pytest.raises(tick.Refused, match="TEST_EMAIL_TO must be baseballtom33@gmail.com"):
            tick.check_guards(settings(**live, assistant_proactive_test_email_to=override),
                              now=REAL_NOW + timedelta(days=3), tails=False, real_now=REAL_NOW)


@pytest.mark.parametrize("live", [LIVE_EMAIL, LIVE_TEAMS])
@pytest.mark.parametrize("tails", [False, True])
def test_a_run_that_can_send_ticks_at_the_real_now(live, tails):
    s = settings(**live, **OWNER)
    for off in (timedelta(minutes=-6), timedelta(hours=-3)) + ((timedelta(minutes=6), timedelta(days=3)) if not tails
                                                               else ()):
        with pytest.raises(tick.Refused, match="within 5 minutes of the real now"):
            tick.check_guards(s, now=REAL_NOW + off, tails=tails, real_now=REAL_NOW)
    for off in (timedelta(0), timedelta(minutes=-5)):
        assert tick.check_guards(s, now=REAL_NOW + off, tails=tails, real_now=REAL_NOW) == [USER]


@pytest.mark.parametrize("live", [LIVE_EMAIL, LIVE_TEAMS])
def test_a_run_that_can_send_needs_the_rfp_bench_off(live):
    with pytest.raises(tick.Refused, match="RFP_TESTING_ENABLED"):
        tick.check_guards(settings(**live, **OWNER, rfp_testing_enabled=True), now=REAL_NOW, tails=False,
                          real_now=REAL_NOW)


FRI_1305 = datetime(2026, 10, 30, 13, 5, tzinfo=PT).astimezone(UTC)
SAT_1220 = datetime(2026, 10, 31, 12, 20, tzinfo=PT).astimezone(UTC)


@pytest.mark.parametrize("live", [LIVE_EMAIL, LIVE_TEAMS])
def test_live_past_slot_accepts_friday_1305_from_a_saturday_clock(live):
    s = settings(**live, **OWNER)
    assert tick.check_guards(s, now=FRI_1305, tails=False, real_now=SAT_1220, live_past_slot=True) == [USER]
    # 23h15m back is inside the 24 hour tail window too.
    assert tick.check_guards(s, now=FRI_1305, tails=True, real_now=SAT_1220, live_past_slot=True) == [USER]
    # Without the flag the same clock is refused.
    with pytest.raises(tick.Refused, match="within 5 minutes of the real now"):
        tick.check_guards(s, now=FRI_1305, tails=False, real_now=SAT_1220)


def test_live_past_slot_stops_at_72_hours():
    s = settings(**LIVE_EMAIL, **OWNER)
    assert tick.check_guards(s, now=REAL_NOW - timedelta(hours=72), tails=False, real_now=REAL_NOW,
                             live_past_slot=True) == [USER]
    for back in (timedelta(hours=73), timedelta(hours=72, minutes=1)):
        with pytest.raises(tick.Refused, match="at most 72 hours in the past"):
            tick.check_guards(s, now=REAL_NOW - back, tails=False, real_now=REAL_NOW, live_past_slot=True)


def test_live_past_slot_never_reaches_into_the_future():
    s = settings(**LIVE_TEAMS, **OWNER)
    assert tick.check_guards(s, now=REAL_NOW + timedelta(minutes=5), tails=False, real_now=REAL_NOW,
                             live_past_slot=True) == [USER]
    for ahead in (timedelta(minutes=6), timedelta(hours=1), timedelta(days=3)):
        with pytest.raises(tick.Refused, match="not more than 5 minutes ahead"):
            tick.check_guards(s, now=REAL_NOW + ahead, tails=False, real_now=REAL_NOW, live_past_slot=True)


def test_live_past_slot_keeps_every_other_live_guard():
    past = REAL_NOW - timedelta(hours=20)
    for override in ("", "x@example.com"):
        with pytest.raises(tick.Refused, match="TEST_EMAIL_TO"):
            tick.check_guards(settings(**LIVE_EMAIL, assistant_proactive_test_email_to=override), now=past,
                              tails=False, real_now=REAL_NOW, live_past_slot=True)
    with pytest.raises(tick.Refused, match="RFP_TESTING_ENABLED"):
        tick.check_guards(settings(**LIVE_EMAIL, **OWNER, rfp_testing_enabled=True), now=past, tails=False,
                          real_now=REAL_NOW, live_past_slot=True)
    with pytest.raises(tick.Refused, match="dev project"):
        tick.check_guards(settings(**LIVE_EMAIL, **OWNER, supabase_url="http://127.0.0.1:54321"), now=past,
                          tails=False, real_now=REAL_NOW, live_past_slot=True)
    with pytest.raises(tick.Refused, match="ALLOWLIST"):
        tick.check_guards(settings(**LIVE_EMAIL, **OWNER, assistant_proactive_user_allowlist=""), now=past,
                          tails=False, real_now=REAL_NOW, live_past_slot=True)
    # The tail window is separate from the live guard and still applies.
    with pytest.raises(tick.Refused, match="24 hours"):
        tick.check_guards(settings(**LIVE_EMAIL, **OWNER), now=REAL_NOW - timedelta(hours=30), tails=True,
                          real_now=REAL_NOW, live_past_slot=True)


def test_live_past_slot_changes_nothing_for_a_channel_off_run():
    s = settings(rfp_testing_enabled=True)
    for flag in (False, True):
        assert tick.check_guards(s, now=REAL_NOW + timedelta(days=3), tails=False, real_now=REAL_NOW,
                                 live_past_slot=flag) == [USER]


def test_main_live_past_slot_runs_and_reports_it(monkeypatch, capsys):
    w = TickWorld(monkeypatch, **LIVE_TEAMS, **OWNER, rfp_testing_enabled=False)
    w.db.tables["profiles"] = [fk.profile(id=USER, role="executive", email="bdr.e2e.admin@g3test.dev")]
    past = (REAL_NOW - timedelta(hours=20)).isoformat()
    assert tick.main(["--now", past, "--no-tails"]) == 2
    assert "within 5 minutes" in capsys.readouterr().err and w.polls == []
    assert tick.main(["--now", past, "--no-tails", "--live-past-slot"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["live_past_slot"] is True and out["live_channels"] == ["teams"] and len(w.polls) == 1
    assert tick.main(["--now", REAL_NOW.isoformat(), "--no-tails"]) == 0
    assert json.loads(capsys.readouterr().out)["live_past_slot"] is False
    # Not an e2e account: still refused with the flag.
    w.db.tables["profiles"] = [fk.profile(id=USER, role="executive", email="someone@g3electrical.com")]
    assert tick.main(["--now", past, "--no-tails", "--live-past-slot"]) == 2
    assert "e2e accounts" in capsys.readouterr().err


def test_a_channel_off_run_still_travels_freely():
    s = settings(notification_emails_enabled=False, teams_alerts_enabled=False, rfp_testing_enabled=True)
    assert tick.check_guards(s, now=REAL_NOW + timedelta(days=3), tails=False, real_now=REAL_NOW) == [USER]


def test_the_e2e_accounts_match_the_delivery_guard():
    assert tick.E2E_EMAILS == assistant_delivery.TEST_TEAMS_ALLOWED


def test_main_refuses_a_live_run_for_anyone_but_the_e2e_accounts(monkeypatch, capsys):
    w = TickWorld(monkeypatch, **LIVE_TEAMS, **OWNER, rfp_testing_enabled=False)
    w.db.tables["profiles"] = [fk.profile(id=USER, role="executive", email="someone@g3electrical.com")]
    assert tick.main(["--now", REAL_NOW.isoformat(), "--no-tails"]) == 2
    assert "e2e accounts" in capsys.readouterr().err and w.polls == []
    w.db.tables["profiles"] = []
    assert tick.main(["--now", REAL_NOW.isoformat(), "--no-tails"]) == 2
    assert "e2e accounts" in capsys.readouterr().err and w.polls == []
    w.db.tables["profiles"] = [fk.profile(id=USER, role="executive", email="BDR.e2e.admin@g3test.dev")]
    assert tick.main(["--now", REAL_NOW.isoformat(), "--no-tails"]) == 0
    assert json.loads(capsys.readouterr().out)["live_channels"] == ["teams"] and len(w.polls) == 1


def test_main_refuses_the_review_scenario(monkeypatch, capsys):
    # Email live (MS_CLIENT_ID set, NOTIFICATION_EMAILS_ENABLED left on), no
    # override, a travelled clock: refused before anything runs.
    w = TickWorld(monkeypatch, **LIVE_EMAIL)
    assert tick.main(["--now", "2026-11-03T07:00", "--no-tails"]) == 2
    assert "can send email for real" in capsys.readouterr().err and w.polls == []


# ── The claim check ──────────────────────────────────────────────────────────


def test_the_claims_guards():
    claims.check_guards(settings())
    with pytest.raises(tick.Refused, match="production"):
        claims.check_guards(settings(supabase_url=PROD_URL))
    with pytest.raises(tick.Refused, match="dev project"):
        claims.check_guards(settings(supabase_url="https://abcdefghijklmnopqrst.supabase.co"))
    with pytest.raises(tick.Refused, match="TEST_EMAIL_TO"):
        claims.check_guards(settings(assistant_proactive_test_email_to="x@example.com"))
    for over, name in (({"notification_emails_enabled": True}, "NOTIFICATION_EMAILS_ENABLED"),
                       ({"teams_alerts_enabled": True}, "TEAMS_ALERTS_ENABLED")):
        with pytest.raises(tick.Refused, match=name):
            claims.check_guards(settings(**over))
    with pytest.raises(tick.Refused, match="NOTIFICATION_EMAILS_ENABLED and TEAMS_ALERTS_ENABLED"):
        claims.require_channels_off(settings(notification_emails_enabled=True, teams_alerts_enabled=True))


def test_user_id_parsing():
    assert claims.parse_user_ids(f" {USER.upper()} , {USER2} ") == [USER, USER2]
    assert claims.parse_user_ids(USER) == [USER]
    for bad in ("", ",", "not-a-uuid", f"{USER},{USER}", f"{USER},{USER2},{USER2[:-1]}1"):
        with pytest.raises(ValueError):
            claims.parse_user_ids(bad)
    args = claims.build_parser().parse_args(["--user-ids", USER, "--now", "2026-10-30T07:05", "--iterations", "3"])
    assert args.user_ids == [USER] and args.iterations == 3 and args.now == FRI_705
    assert claims.build_parser().parse_args(["--user-ids", USER]).iterations == 20
    for bad in ([], ["--user-ids", "nope"]):
        with pytest.raises(SystemExit):
            claims.build_parser().parse_args(bad)


def test_the_worker_env_sends_nothing_and_names_the_allowlist():
    env = claims.child_env([USER, USER2], environ={})
    assert env["ASSISTANT_PROACTIVE_EXTERNAL_ENABLED"] == "true" and env["ASSISTANT_PROACTIVE_ENABLED"] == "true"
    assert env["NOTIFICATION_EMAILS_ENABLED"] == "false" and env["TEAMS_ALERTS_ENABLED"] == "false"
    assert env["RFP_TESTING_ENABLED"] == "false" and env["ASSISTANT_PROACTIVE_TEST_EMAIL_TO"] == ""
    assert env["ASSISTANT_PROACTIVE_USER_ALLOWLIST"] == f"{USER},{USER2}"
    assert len(env["ASSISTANT_TOKEN_SECRET"]) >= 32
    assert claims.child_env([USER], environ={"ASSISTANT_TOKEN_SECRET": "s" * 40})["ASSISTANT_TOKEN_SECRET"] == "s" * 40
    assert claims.child_env([USER], environ={"ASSISTANT_TOKEN_SECRET": "short"})["ASSISTANT_TOKEN_SECRET"] != "short"


def test_the_worker_env_loads_as_valid_settings(monkeypatch):
    for key, value in claims.child_env([USER], environ={}).items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.assistant_proactive_external_enabled and not s.notification_emails_enabled
        assert not s.teams_alerts_enabled and not s.rfp_testing_enabled and s.assistant_proactive_test_email_to == ""
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


class ClaimWorld:
    """Two allowlisted people, one FakeDB, the real router and send step with
    the fake helper modules, and two threads standing in for two processes."""

    def __init__(self, monkeypatch, users=(USER,), now=FRI_705):
        self.mp = monkeypatch
        self.now = now
        self.users = list(users)
        self.db = FakeDB(now=now)
        t = self.db.tables
        t["assistant_settings"] = [fk.settings_row(proactive_enabled=True)]
        t["assistant_router_cursors"] = [fk.cursor_row(name="notifications"), fk.cursor_row(name="audit_log")]
        t["profiles"] = [fk.profile(id=USER, role="executive", email="exe@g3.test"),
                         fk.profile(id=USER2, role="estimating_admin", email="ea@g3.test")]
        monkeypatch.setattr(supabase_client, "get_supabase", lambda: self.db)
        self.s = get_settings().model_copy(update={
            "assistant_enabled": True, "assistant_proactive_enabled": True,
            "assistant_proactive_external_enabled": True, "assistant_proactive_user_allowlist": ",".join(users),
            "assistant_proactive_digest_times": "07:00,13:00", "assistant_proactive_digest_catchup_hours": 3,
            "assistant_proactive_quiet_start": "18:00", "assistant_proactive_quiet_end": "06:00",
            "assistant_proactive_claim_batch": 50, "notification_emails_enabled": False,
            "teams_alerts_enabled": False, "rfp_testing_enabled": False,
        })
        monkeypatch.setattr(svc, "get_settings", lambda: self.s)
        monkeypatch.setattr(assistant_delivery, "get_settings", lambda: self.s)
        monkeypatch.setattr(svc, "_card_sub_app", lambda kind: None)
        self.helpers = fk.install_delivery_fakes(monkeypatch)
        self.helpers.teams.configured = False   # TEAMS_ALERTS_ENABLED=false
        self.threads_polled: list[str] = []

    def runner(self, workers, now, iterations, users):
        barrier = threading.Barrier(len(workers))
        results: list[dict] = []

        def go(worker: str) -> None:
            out = {"worker": worker, "polls": [], "error": None}
            try:
                for _ in range(iterations):
                    try:
                        barrier.wait(30)
                    except threading.BrokenBarrierError:
                        pass
                    out["polls"].append(svc.poll_once(now, worker=worker, tails=False))
            except Exception as exc:  # noqa: BLE001
                out["error"] = exc.__class__.__name__
            results.append(out)

        threads = [threading.Thread(target=go, args=(w,)) for w in workers]
        for th in threads:
            th.start()
        for th in threads:
            th.join(120)
        return results

    def check(self, **kw):
        return claims.run_check(self.db, self.s, self.users, runner=self.runner, now=kw.pop("now", self.now),
                                iterations=kw.pop("iterations", 4), log=lambda line: None, **kw)

    def tables(self) -> dict[str, list[dict]]:
        return {k: [dict(r) for r in v] for k, v in self.db.tables.items() if v}


def passed(checks: dict[str, list[str]]) -> bool:
    return all(not v for v in checks.values())


@pytest.mark.parametrize("users", [(USER,), (USER, USER2)])
def test_two_workers_pass_every_check_and_every_row_is_removed(monkeypatch, users):
    w = ClaimWorld(monkeypatch, users)
    before = w.tables()
    checks, summary = w.check(iterations=claims.ITERATIONS)
    assert passed(checks), checks
    assert len(checks) >= 8
    assert summary["polls"] == 2 * claims.ITERATIONS and summary["step_errors"] == []
    assert summary["cleanup"]["failed"] == [] and summary["leftover"] == {}
    assert w.tables() == before
    # The external step ran for real against the fakes and sent nothing.
    assert w.helpers.graph.calls == [] and w.helpers.teams.calls == []


def test_the_run_was_a_real_two_worker_race(monkeypatch):
    w = ClaimWorld(monkeypatch, (USER, USER2))
    run, results = seeded_run(w)
    polled = {r["worker"] for r in results}
    assert polled == set(run.workers) and len(run.workers) == 2 and len(set(run.workers)) == 2
    made = claims.collect_created(w.db, run)
    assert {d["claimed_by"] for d in made["digests"]} <= set(run.workers)
    assert len(made["digests"]) == 2 and len(made["inbox"]) == 2 * (1 + claims.TODOS_PER_USER)
    # One Teams row per immediate row, one email row per digest, all channel_off.
    channels = sorted((s["channel"], s["status"], s["error_kind"]) for s in made["sends"])
    assert channels == [("email", "skipped", "channel_off")] * 2 + [("teams", "skipped", "channel_off")] * 2
    assert {e["status"] for e in w.db.tables["assistant_events"] if e["id"] in run.event_ids} == {"processed"}
    assert all(e["attempts"] == 1 for e in w.db.tables["assistant_events"] if e["id"] in run.event_ids)
    assert [r["cursor_at"] for r in w.db.tables["assistant_router_cursors"]] == [fk.P7_T0.isoformat()] * 2


def seeded_run(w: ClaimWorld, iterations: int = 4):
    """Seed and run the two workers without cleaning up, for the break tests."""
    day, slot_at = FRI_705.astimezone(PT).date(), datetime(2026, 10, 30, 7, 0, tzinfo=PT).astimezone(UTC)
    run = claims.Run(run_id="t1", users=w.users, now=w.now, day=day, slot="am", slot_at=slot_at,
                     workers=["claims-t1-a", "claims-t1-b"])
    run.before = claims.snapshot(w.db, w.users, day)
    claims.seed(w.db, run, lambda line: None)
    return run, w.runner(run.workers, w.now, iterations, w.users)


def failing(w: ClaimWorld, run, results) -> dict[str, list[str]]:
    return {k: v for k, v in claims.evaluate(w.db, run, results).items() if v}


def test_the_unbroken_state_passes(monkeypatch):
    w = ClaimWorld(monkeypatch, (USER,))
    run, results = seeded_run(w)
    assert failing(w, run, results) == {}


def rows(w, table):
    return w.db.tables.setdefault(table, [])


def test_a_second_digest_for_a_slot_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    extra = dict(rows(w, "assistant_digests")[0], id="d0000000-0000-4000-8000-0000000000aa")
    rows(w, "assistant_digests").append(extra)
    bad = failing(w, run, results)
    assert any("digest" in m for m in bad["one digest ledger row per (user, date, slot)"])


def test_a_missing_digest_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    rows(w, "assistant_digests").clear()
    assert "one digest ledger row per (user, date, slot)" in failing(w, run, results)


def test_a_wrong_item_count_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    rows(w, "assistant_digests")[0]["item_count"] = 3
    bad = failing(w, run, results)
    assert "lists 3 item" in bad["one digest ledger row per (user, date, slot)"][0]


def test_a_second_digest_bell_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    bell = next(b for b in rows(w, "notifications") if b["type"] == "assistant.digest")
    rows(w, "notifications").append(dict(bell, id="b0000000-0000-4000-8000-0000000000aa"))
    assert "one bell per (user, date, slot) and per immediate row" in failing(w, run, results)


def test_a_missing_inbox_bell_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    rows(w, "notifications")[:] = [b for b in rows(w, "notifications") if b["type"] != "assistant.inbox"]
    assert "one bell per (user, date, slot) and per immediate row" in failing(w, run, results)


def test_an_extra_or_missing_inbox_row_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    row = next(r for r in rows(w, "assistant_inbox") if r["delivery"] == "digest")
    rows(w, "assistant_inbox").append(dict(row, id="c0000000-0000-4000-8000-0000000000aa", dedupe_key="p7:x:extra"))
    assert any("expected 3 new inbox rows" in m for m in failing(w, run, results)["one inbox row per (user, key)"])
    rows(w, "assistant_inbox")[:] = [r for r in rows(w, "assistant_inbox") if r["delivery"] != "immediate"]
    assert "one inbox row per (user, key)" in failing(w, run, results)


def test_a_repeated_inbox_key_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    row = next(r for r in rows(w, "assistant_inbox") if r["delivery"] == "digest")
    rows(w, "assistant_inbox").append(dict(row, id="c0000000-0000-4000-8000-0000000000bb"))
    assert any("repeat" in m for m in failing(w, run, results)["one inbox row per (user, key)"])


def test_a_duplicate_send_row_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    rows(w, "assistant_external_sends").append(
        dict(rows(w, "assistant_external_sends")[0], id="e0000000-0000-4000-8000-0000000000aa"))
    bad = failing(w, run, results)["one send ledger row per (target, channel), none sent"]
    assert any("repeats" in m for m in bad)


def test_a_missing_send_row_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    rows(w, "assistant_external_sends").pop()
    assert any("differ" in m for m in failing(w, run, results)["one send ledger row per (target, channel), none sent"])


def test_a_send_that_was_not_channel_off_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    send = rows(w, "assistant_external_sends")[0]
    send.update(status="sent", sent_at=FRI_705.isoformat(), error_kind=None)
    assert any("not skipped/channel_off" in m
               for m in failing(w, run, results)["one send ledger row per (target, channel), none sent"])


def test_a_teams_switch_that_is_off_expects_no_teams_row(monkeypatch):
    w = ClaimWorld(monkeypatch)
    w.db.tables["teams_alert_users"] = [{"user_id": USER, "enabled": False}]
    run, results = seeded_run(w)
    sends = rows(w, "assistant_external_sends")
    assert [s["channel"] for s in sends] == ["email"]
    assert failing(w, run, results) == {}


def test_an_event_finished_twice_or_not_at_all_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    event = next(e for e in rows(w, "assistant_events") if e["id"] in run.event_ids)
    event["attempts"] = 2
    assert any("2 attempt" in m for m in failing(w, run, results)["every seeded event finished once"])
    event.update(attempts=1, status="pending", processed_at=None)
    assert "every seeded event finished once" in failing(w, run, results)
    rows(w, "assistant_events")[:] = [e for e in rows(w, "assistant_events") if e["id"] not in run.event_ids]
    assert any("vanished" in m for m in failing(w, run, results)["every seeded event finished once"])


def test_an_unfinished_slot_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    slot = next(s for s in rows(w, "assistant_proactive_slots") if s["step"] == "digest_am")
    slot["finished_at"] = None
    assert "the digest slot finished once" in failing(w, run, results)


def test_a_digest_claimed_twice_across_polls_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    results[0]["polls"][0].setdefault("digests", {})["digests"] = 5
    assert "each digest claimed once across all polls" in failing(w, run, results)


def test_a_dead_or_skipping_worker_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    results[0]["error"] = "RuntimeError"
    assert "both workers polled" in failing(w, run, results)
    results[0]["error"] = None
    results[1]["polls"].append({"skipped": "env_off"})
    assert any("env_off" in m for m in failing(w, run, results)["both workers polled"])
    assert "both workers polled" in failing(w, run, results[:1])


def test_a_moved_cursor_fails(monkeypatch):
    w = ClaimWorld(monkeypatch)
    run, results = seeded_run(w)
    rows(w, "assistant_router_cursors")[0]["cursor_at"] = FRI_705.isoformat()
    assert "tails=False left the cursors alone" in failing(w, run, results)


def test_cleanup_removes_what_the_router_created_and_nothing_else(monkeypatch):
    w = ClaimWorld(monkeypatch, (USER,))
    other = fk.inbox_row(id="c0000000-0000-4000-8000-0000000000f1", user_id=USER2, dedupe_key="p7:other:keep",
                         event_id=None, delivered_at=FRI_705.isoformat(), digest_id=None)
    rows(w, "assistant_inbox").append(other)
    keep_todo = {"id": "d0000000-0000-4000-8000-0000000000f1", "user_id": USER2, "title": "keep", "due_date": None,
                 "is_done": True}
    w.db.tables.setdefault("todos", []).append(keep_todo)
    before = w.tables()
    run, results = seeded_run(w)
    out = claims.cleanup(w.db, run)
    assert out["failed"] == []
    assert out["deleted"]["assistant_inbox"] == 1 + claims.TODOS_PER_USER
    assert out["deleted"]["todos"] == claims.TODOS_PER_USER + 1
    assert w.tables() == before
    assert claims.collect_created(w.db, run) == {k: [] for k in ("inbox", "digests", "sends", "bells", "slots", "events")}


def test_a_failing_runner_still_cleans_up(monkeypatch):
    w = ClaimWorld(monkeypatch)
    before = w.tables()

    def boom(workers, now, iterations, users):
        raise RuntimeError("worker pool died")

    with pytest.raises(RuntimeError, match="pool died"):
        claims.run_check(w.db, w.s, w.users, runner=boom, now=w.now, iterations=1, log=lambda line: None)
    assert w.tables() == before


def test_a_cleanup_failure_is_reported_as_a_failed_check(monkeypatch):
    w = ClaimWorld(monkeypatch)
    real = claims._delete_ids

    def flaky(client, table, ids, column="id"):
        if table == "todos":
            raise RuntimeError("delete refused")
        return real(client, table, ids, column)

    monkeypatch.setattr(claims, "_delete_ids", flaky)
    checks, summary = w.check()
    assert summary["cleanup"]["failed"] == ["todos:RuntimeError"]
    assert checks["cleanup removed every row"]
    # The todos stay behind: the seeded ids are in the SEEDED line for a manual delete.
    assert len(rows(w, "todos")) == claims.TODOS_PER_USER + 1


@pytest.mark.parametrize("mutate, message", [
    (lambda w: w.db.tables["assistant_settings"][0].update(proactive_enabled=False), "company switch is off"),
    (lambda w: w.db.tables["assistant_settings"][0].update(enabled=False), "company switch is off"),
    (lambda w: w.db.tables["profiles"][0].update(is_active=False), "not an active internal profile"),
    (lambda w: w.db.tables["profiles"][0].update(role="estimator"), "not an active internal profile"),
    (lambda w: w.db.tables["profiles"].clear(), "not an active internal profile"),
    (lambda w: w.db.tables.setdefault("assistant_digests", []).append(
        fk.digest_row(user_id=USER, digest_date="2026-10-30", slot="am")), "already has the 2026-10-30 am digest"),
    (lambda w: w.db.tables.setdefault("assistant_inbox", []).append(
        fk.inbox_row(id="c0000000-0000-4000-8000-0000000000f2", user_id=USER, dedupe_key="p7:old:1",
                     delivery="digest", delivered_at=None, event_id=None)), "pending router inbox row"),
    (lambda w: w.db.tables.setdefault("todos", []).append(
        {"id": "d0000000-0000-4000-8000-0000000000f2", "user_id": USER, "title": "open", "due_date": "2026-10-29",
         "is_done": False}), "open to-do due 2026-10-29"),
])
def test_a_refusal_writes_nothing(monkeypatch, mutate, message):
    w = ClaimWorld(monkeypatch)
    mutate(w)
    before = w.tables()
    with pytest.raises(tick.Refused, match=message):
        w.check()
    assert w.tables() == before


def test_a_time_outside_an_open_slot_is_refused(monkeypatch):
    for when in (datetime(2026, 10, 30, 6, 59, tzinfo=PT), datetime(2026, 10, 30, 10, 0, tzinfo=PT),
                 datetime(2026, 10, 30, 16, 0, tzinfo=PT), datetime(2026, 10, 31, 7, 5, tzinfo=PT)):
        w = ClaimWorld(monkeypatch, now=when.astimezone(UTC))
        before = w.tables()
        with pytest.raises(tick.Refused, match="open digest slot"):
            w.check()
        assert w.tables() == before


def test_the_afternoon_slot_and_a_holiday(monkeypatch):
    pm = datetime(2026, 10, 30, 13, 10, tzinfo=PT).astimezone(UTC)
    w = ClaimWorld(monkeypatch, now=pm)
    checks, summary = w.check()
    assert passed(checks) and summary["slot"] == "pm"
    w = ClaimWorld(monkeypatch)
    w.db.tables["assistant_settings"][0]["holidays"] = ["2026-10-30"]
    with pytest.raises(tick.Refused, match="open digest slot"):
        w.check()


def test_the_default_time_is_the_latest_opened_morning_slot(monkeypatch):
    w = ClaimWorld(monkeypatch)
    setup = claims.slot_setup(w.s, [])
    sat = datetime(2026, 10, 31, 9, 0, tzinfo=PT).astimezone(UTC)
    assert claims.default_now(sat, setup) == FRI_705
    fri_early = datetime(2026, 10, 30, 7, 4, tzinfo=PT).astimezone(UTC)
    assert claims.default_now(fri_early, setup) == datetime(2026, 10, 29, 7, 5, tzinfo=PT).astimezone(UTC)
    held = claims.slot_setup(w.s, ["2026-10-30"])
    assert claims.default_now(sat, held) == datetime(2026, 10, 29, 7, 5, tzinfo=PT).astimezone(UTC)
    checks, summary = w.check(now=None, real_now=sat)
    assert passed(checks) and summary["now"] == FRI_705.isoformat()


def test_the_open_slot_edges():
    setup = claims.slot_setup(get_settings().model_copy(update={"assistant_proactive_digest_times": "07:00,13:00"}), [])
    at = lambda h, m: datetime(2026, 10, 30, h, m, tzinfo=PT).astimezone(UTC)  # noqa: E731
    assert claims.open_slot(at(7, 0), setup)[0] == "am"
    assert claims.open_slot(at(9, 59), setup)[0] == "am"
    assert claims.open_slot(at(10, 0), setup) is None
    assert claims.open_slot(at(13, 0), setup)[0] == "pm"
    assert claims.open_slot(at(15, 59), setup)[0] == "pm"
    assert claims.open_slot(at(16, 0), setup) is None


def test_main_refuses_before_it_connects(monkeypatch, capsys):
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings(supabase_url=PROD_URL))
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: pytest.fail("connected"))
    assert claims.main(["--user-ids", USER]) == 2
    assert "production" in capsys.readouterr().err
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings(teams_alerts_enabled=True))
    assert claims.main(["--user-ids", USER]) == 2
    assert "TEAMS_ALERTS_ENABLED" in capsys.readouterr().err


def test_main_exits_one_on_a_failed_check_and_zero_on_a_pass(monkeypatch, capsys):
    w = ClaimWorld(monkeypatch)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings())
    monkeypatch.setattr(claims, "spawn_runner", w.runner)
    assert claims.main(["--user-ids", USER, "--now", "2026-10-30T07:05", "--iterations", "3"]) == 0
    out = capsys.readouterr().out
    assert "PASS: one inbox row per (user, key)" in out and "FAIL" not in out
    assert json.loads(out[out.index("{\n"):])["leftover"] == {}

    def break_it(workers, now, iterations, users):
        results = w.runner(workers, now, iterations, users)
        rows(w, "assistant_events")[0]["attempts"] = 2
        return results

    monkeypatch.setattr(claims, "spawn_runner", break_it)
    assert claims.main(["--user-ids", USER, "--now", "2026-10-30T07:05", "--iterations", "3"]) == 1
    assert "FAIL: every seeded event finished once" in capsys.readouterr().out


def test_a_refusal_in_main_exits_two_and_writes_nothing(monkeypatch, capsys):
    w = ClaimWorld(monkeypatch)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings())
    before = w.tables()
    assert claims.main(["--user-ids", USER, "--now", "2026-10-31T07:05"]) == 2
    assert "open digest slot" in capsys.readouterr().err
    assert w.tables() == before


# ── The claim check: preflight against what is already there ─────────────────


@pytest.mark.parametrize("when", [REAL_NOW + timedelta(days=8, minutes=5), REAL_NOW - timedelta(days=31),
                                  datetime(2027, 11, 3, 7, 5, tzinfo=PT).astimezone(UTC)])
def test_a_time_outside_the_travel_window_is_refused(monkeypatch, when):
    w = ClaimWorld(monkeypatch, now=when)
    before = w.tables()
    with pytest.raises(tick.Refused, match="8 days after"):
        w.check()
    assert w.tables() == before


def test_main_refuses_a_far_claims_time(monkeypatch, capsys):
    w = ClaimWorld(monkeypatch)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings())
    before = w.tables()
    assert claims.main(["--user-ids", USER, "--now", "2027-11-03T07:05"]) == 2
    assert "8 days after" in capsys.readouterr().err
    assert w.tables() == before


@pytest.mark.parametrize("row, mode", [
    (fk.subscription_row(user_id=USER, severity="high"), "alert"),                       # the user picked Alert
    (fk.subscription_row(user_id=USER, enabled=False), "off"),                            # muted
    (fk.subscription_row(user_id=USER, severity="low", source="feedback_auto"), "inbox"),  # a feedback downgrade
    (fk.subscription_row(role="executive", user_id=None, severity="urgent", source="admin"), "urgent"),
    (fk.subscription_row(role="executive", user_id=None, enabled=False, source="admin"), "off"),
])
def test_a_todo_due_subscription_off_the_default_is_refused(monkeypatch, row, mode):
    w = ClaimWorld(monkeypatch)
    w.db.tables["assistant_subscriptions"] = [row]
    before = w.tables()
    with pytest.raises(tick.Refused, match=f"todo.due {mode}, not the code default") as info:
        w.check()
    assert str(row["id"]) in str(info.value)
    assert w.tables() == before


def test_a_todo_due_row_that_keeps_the_default_passes(monkeypatch):
    w = ClaimWorld(monkeypatch)
    w.db.tables["assistant_subscriptions"] = [
        fk.subscription_row(user_id=USER, severity="normal"),                                     # Digest, the default
        fk.subscription_row(role="estimating_admin", user_id=None, severity="urgent", source="admin"),  # another role
        fk.subscription_row(user_id=USER, event_type="assistant.approval_expiring", enabled=False),  # another kind
    ]
    checks, _summary = w.check()
    assert passed(checks), checks


def test_a_user_row_over_an_admin_row_counts_as_the_user_row(monkeypatch):
    # The step 9 state: the role row says urgent, the user row says Alert.
    w = ClaimWorld(monkeypatch)
    user_row = fk.subscription_row(user_id=USER, severity="high")
    role_row = fk.subscription_row(role="executive", user_id=None, severity="urgent", source="admin")
    w.db.tables["assistant_subscriptions"] = [user_row, role_row]
    with pytest.raises(tick.Refused, match="todo.due alert") as info:
        w.check()
    assert user_row["id"] in str(info.value) and role_row["id"] in str(info.value)


@pytest.mark.parametrize("finished", [FRI_705.isoformat(), None])
def test_an_existing_digest_slot_row_is_refused(monkeypatch, finished):
    w = ClaimWorld(monkeypatch)
    w.db.tables["assistant_proactive_slots"] = [fk.slot_row(step="digest_am", slot_date="2026-10-30",
                                                            claimed_by="real-loop", finished_at=finished)]
    before = w.tables()
    with pytest.raises(tick.Refused, match=r"already has \(digest_am, 2026-10-30\)"):
        w.check()
    assert w.tables() == before


def test_a_slot_row_for_another_slot_or_day_passes(monkeypatch):
    w = ClaimWorld(monkeypatch)
    w.db.tables["assistant_proactive_slots"] = [
        fk.slot_row(step="digest_pm", slot_date="2026-10-30", finished_at=FRI_705.isoformat()),
        fk.slot_row(step="digest_am", slot_date="2026-10-29", finished_at=FRI_705.isoformat()),
    ]
    before = list(w.db.tables["assistant_proactive_slots"])
    checks, _summary = w.check()
    assert passed(checks), checks
    assert w.db.tables["assistant_proactive_slots"] == before


OTHER = "a0000000-0000-4000-8000-0000000000e9"


@pytest.mark.parametrize("event", [
    fk.event_row(id="e1000000-0000-4000-8000-000000000001", subject_user_id=OTHER, source="emit"),
    fk.event_row(id="e1000000-0000-4000-8000-000000000002", subject_user_id=OTHER, attempts=1, last_error="x",
                 next_attempt_at=(FRI_705 + timedelta(hours=1)).isoformat()),
    fk.event_row(id="e1000000-0000-4000-8000-000000000003", subject_user_id=OTHER, status="claimed",
                 claimed_by="tick-script:old", attempts=1, claimed_at=(FRI_705 - timedelta(minutes=11)).isoformat()),
])
def test_a_pending_or_lease_expired_event_is_refused(monkeypatch, event):
    w = ClaimWorld(monkeypatch)
    w.db.tables["assistant_events"] = [event]
    before = w.tables()
    with pytest.raises(tick.Refused, match="pending or claimed under an expired lease") as info:
        w.check()
    assert event["id"] in str(info.value)
    assert w.tables() == before


def test_a_processed_or_freshly_claimed_event_is_left_alone(monkeypatch):
    w = ClaimWorld(monkeypatch)
    done = fk.event_row(id="e1000000-0000-4000-8000-000000000004", subject_user_id=OTHER, status="processed",
                        processed_at=FRI_705.isoformat(), attempts=1, claimed_by="real-loop")
    fresh = fk.event_row(id="e1000000-0000-4000-8000-000000000005", subject_user_id=OTHER, status="claimed",
                         claimed_by="real-loop", attempts=1, claimed_at=FRI_705.isoformat(),
                         dedupe_key="p7:todo.due:fresh:2026-10-30")
    w.db.tables["assistant_events"] = [done, fresh]
    before = [dict(done), dict(fresh)]
    checks, summary = w.check()
    assert passed(checks), checks
    assert summary["leftover"] == {}
    assert w.db.tables["assistant_events"] == before


def test_cleanup_skips_an_event_that_existed_before_the_run(monkeypatch):
    # A row that slipped past the preflight (claimed after it ran) and was
    # then claimed by a worker is routed, but never deleted by the cleanup.
    w = ClaimWorld(monkeypatch)
    old = fk.event_row(id="e1000000-0000-4000-8000-000000000006", subject_user_id=OTHER, status="claimed",
                       claimed_by="real-loop", attempts=1, claimed_at=FRI_705.isoformat(),
                       occurred_at=datetime(2026, 10, 30, 7, 0, tzinfo=PT).astimezone(UTC).isoformat())
    foreign = fk.event_row(id="e1000000-0000-4000-8000-000000000007", subject_user_id=OTHER, source="emit",
                           status="processed", processed_at=FRI_705.isoformat(), attempts=1,
                           dedupe_key="p7:todo.due:foreign:2026-10-30")
    w.db.tables["assistant_events"] = [old]
    run, _results = seeded_run(w)
    assert old["id"] in run.before["events"]
    old.update(claimed_by=run.workers[0])                 # the worker took it over
    foreign.update(claimed_by=run.workers[1])             # an app emit made during the run
    w.db.tables["assistant_events"].append(foreign)
    made = claims.collect_created(w.db, run)
    assert {e["id"] for e in made["events"]} == set(run.event_ids)
    claims.cleanup(w.db, run)
    assert {e["id"] for e in w.db.tables["assistant_events"]} == {old["id"], foreign["id"]}


@pytest.mark.parametrize("occurred", ["slot", "now"])
def test_cleanup_finds_a_run_event_a_retry_handed_back(monkeypatch, occurred):
    # A sweeper (slot instant) or approval-scan (the run's now) event that went
    # to retry: pending, claimed_by null, last_error set, never reclaimed at
    # the fixed now. It is this run's, so it is collected and deleted.
    w = ClaimWorld(monkeypatch)
    run, _results = seeded_run(w)
    at = run.slot_at if occurred == "slot" else run.now
    retried = fk.event_row(id="e1000000-0000-4000-8000-000000000008", subject_user_id=OTHER, source="sweeper",
                           status="pending", claimed_by=None, attempts=1, last_error="RuntimeError",
                           occurred_at=at.isoformat(), next_attempt_at=(run.now + timedelta(seconds=60)).isoformat(),
                           dedupe_key="p7:todo.due:retried:2026-10-30")
    w.db.tables["assistant_events"].append(retried)
    assert retried["id"] in {e["id"] for e in claims.collect_created(w.db, run)["events"]}
    claims.cleanup(w.db, run)
    assert retried["id"] not in {e["id"] for e in w.db.tables["assistant_events"]}
    assert claims.collect_created(w.db, run)["events"] == []


def test_a_pending_event_from_an_earlier_tick_is_not_claimed_as_the_runs(monkeypatch):
    # Same shape as a retried run event but a different clock: not this run's.
    w = ClaimWorld(monkeypatch)
    run, _results = seeded_run(w)
    earlier = fk.event_row(id="e1000000-0000-4000-8000-000000000009", subject_user_id=OTHER, source="sweeper",
                           status="pending", claimed_by=None, attempts=1, last_error="RuntimeError",
                           occurred_at=(run.now - timedelta(days=1)).isoformat(),
                           dedupe_key="p7:todo.due:earlier:2026-10-29")
    w.db.tables["assistant_events"].append(earlier)
    claims.cleanup(w.db, run)
    assert earlier["id"] in {e["id"] for e in w.db.tables["assistant_events"]}


# ── Source scans ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["assistant_proactive_tick.py", "check_proactive_claims.py"])
def test_the_scripts_are_clean_text_that_never_sends(name):
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert EM_DASH not in text
    for banned in ("notify_role", "notify_user", "notifications.deliver", "import notification_email", "teams_alerts.",
                   "graph_email", "send_mail", "openai", "anthropic", "get_supabase_admin", "quotes"):
        assert banned not in text, banned
    assert "bpidntbyvoooqvaispup" in text or name == "check_proactive_claims.py"
    # The prod ref appears only in the guard, never as a target.
    assert text.count("zxqkqotlcgbalwoqeeca") <= 1


def test_this_file_has_no_em_dash():
    assert EM_DASH not in Path(__file__).read_text(encoding="utf-8")
