"""The assistant routine loop (app/services/assistant_scheduler.py;
docs/ASSISTANT_PHASE3_PLAN.md 3.4), over the FakeDB with a fake recipe
read, a recording bell and a recording mailer.

Invariants pinned here:

  claim      two workers polling the same claim run each due occurrence
             once; a Run now row waits while a worker holds a scheduled run
             of the same schedule, and a scheduled slot due while a Run now
             row is open is skipped overlap;
  refill     fills the second slot and never writes next_run_at; an outage
             leaves one missed row per stale slot and then one catch-up or
             on-time run; a rule with no future run pauses no_future_run;
  owner      an inactive, demoted or switched-off owner pauses the routine
             as owner_changed, a missing delegation as no_delegation (auto
             resumable), bidding off as paused_feature_off; never a failure;
  skips      holiday (weekday rules only), the digest stub (scheduled
             due_today only, never for an Accountant), only_if_changed
             no_change, the watch auto-end;
  delivery   chat, then bell, then email, each through its claim ledger;
             a channel already claimed is never sent again; the bell and
             the email run inside the run's provenance scope; an empty
             result is chat only; the email cap and an email failure mark an
             otherwise delivered run; two runs racing to create the
             conversation leave one conversation_id and both messages in it;
  failures   3 failures pause with a bell, 10 unread pause with a bell;
             the reaper fails abandoned and not-picked-up runs;
  resume     feature on again, or a fresh grant after an MFA or delegation
             pause, restarts the routine with both slots recomputed;
  read       the recipe read goes through the real Executor over
             RECIPE_READ_SPEC with a bdra_ token for (owner, run id), and
             forwards the phase 4 period and week params;
  roles      an estimator_vs_bid routine whose owner lost the actual-bid
             viewer role pauses owner_changed (a skip, never a failure);
  rider      maintenance.tick runs only with ASSISTANT_MAINTENANCE_ENABLED.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.assistant import access, principal, recipes, repo, rrules
from app.assistant.executor import Executor, RequestFailed
from app.core import features, provenance, supabase_client
from app.core.roles import Role
from app.services import assistant_maintenance, assistant_scheduler as sched, graph_email, notifications
from tests.assistant_fakes import (
    NOW,
    SCHEDULE,
    SCHEDULE2,
    SECRET,
    USER,
    WEEKDAYS_7AM,
    FakeDB,
    delegation,
    profile,
    schedule,
    schedule_run,
    settings_row,
)

EM_DASH = chr(0x2014)
# The real read, kept before the fixture patches it.
REAL_READ_RECIPE = sched.read_recipe
PT = rrules.PT
DAILY_7AM = "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"
RUN2 = "abababab-abab-4bab-8bab-abababababa2"
P1 = "12121212-1212-4212-8212-121212121211"
P2 = "12121212-1212-4212-8212-121212121212"


def _payload(n: int = 2, recipe: str = "due_today", **extra) -> dict:
    rows = [{
        "project": {"id": pid, "number": f"26.10.72{i:02d}", "name": f"Probe job {i}",
                    "internal_bid_at": (NOW + timedelta(hours=3)).isoformat()},
        "checks": {"has_quotes": i % 2 == 1, "has_labor": True, "has_markups": True},
        "severity": "warn", "missing_categories": None,
    } for i, pid in enumerate((P1, P2)[:n])]
    return {"recipe": recipe, "window": {"start": NOW.isoformat(), "end": NOW.isoformat(), "label": "Today"},
            "rows": rows, "watch": None, "digest_sent_today": False, **extra}


@pytest.fixture
def world(monkeypatch):
    s = SimpleNamespace(
        assistant_token_secret=SECRET, assistant_enabled=True, assistant_scheduler_poll_seconds=60,
        assistant_schedule_catchup_hours=3, assistant_schedule_claim_batch=20, assistant_schedule_max_failures=3,
        assistant_schedule_max_unread=10, assistant_schedule_stale_run_minutes=15,
        assistant_schedule_max_emails_per_day=20, assistant_maintenance_enabled=False,
        ms_client_id="graph-app", frontend_url="https://bdr.example.com/",
    )
    monkeypatch.setattr(sched, "get_settings", lambda: s)
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    monkeypatch.setattr(access, "get_settings", lambda: s)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    monkeypatch.setattr(sched, "_last_resume", None)
    # The fake's recipe check still lists the four phase 3 recipes (0180 B1
    # widens the real constraint); the two phase 4 recipes are valid here.
    monkeypatch.setattr(FakeDB, "RECIPES", tuple(FakeDB.RECIPES) + tuple(
        r for r in recipes.PHASE4_RECIPES if r not in FakeDB.RECIPES))
    db = FakeDB(
        now=NOW, profiles=[profile()], assistant_delegations=[delegation()], assistant_settings=[settings_row()],
        assistant_profiles=[], assistant_conversations=[], assistant_messages=[], assistant_schedules=[],
        assistant_schedule_runs=[], email_log=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    flags = {"bidding": True, "pm": True, "certified_payroll": True}
    monkeypatch.setattr(features, "enabled_map", lambda role=None: dict(flags))

    order: list[str] = []
    bells: list[dict] = []

    def notify_user(user_id, project_id, type_, message, **kw):
        order.append(f"bell:{type_}")
        bells.append({"user_id": user_id, "project_id": project_id, "type": type_, "message": message,
                      "actor": provenance.current(), **kw})

    monkeypatch.setattr(notifications, "notify_user", notify_user)
    emails: list[dict] = []
    mail = {"fail": False}

    def send_mail(**kw):
        order.append("email")
        if mail["fail"]:
            raise RuntimeError("graph is down")
        actor = provenance.current()
        row = {"id": f"log-{len(emails) + 1}", "sent_by": kw.get("sent_by"), "created_at": NOW.isoformat(),
               "actor_kind": "assistant" if actor else "human"}
        db.tables["email_log"].append(row)
        emails.append({**kw, "actor": actor})
        return row

    monkeypatch.setattr(graph_email, "send_mail", send_mail)

    state = {"payload": _payload(), "reads": []}

    def read_recipe(run, schedule_row, role):
        state["reads"].append({"run": dict(run), "schedule": dict(schedule_row), "role": role})
        value = state["payload"]
        if isinstance(value, Exception):
            raise value
        return value(run) if callable(value) else value

    monkeypatch.setattr(sched, "read_recipe", read_recipe)

    real_post = sched._post_chat

    def post_chat(run, schedule_row, content, blocks):
        order.append("chat")
        return real_post(run, schedule_row, content, blocks)

    monkeypatch.setattr(sched, "_post_chat", post_chat)
    return SimpleNamespace(db=db, s=s, flags=flags, bells=bells, emails=emails, mail=mail, state=state,
                           order=order)


def _add_schedule(world, **over) -> dict:
    row = schedule(**over)
    world.db.tables["assistant_schedules"].append(row)
    return row


def _due(world, **over) -> dict:
    base = {"next_run_at": (NOW - timedelta(minutes=1)).isoformat(),
            "after_next_run_at": (NOW + timedelta(days=1)).isoformat()}
    base.update(over)
    return _add_schedule(world, **base)


def _runs(world, schedule_id=SCHEDULE) -> list[dict]:
    return [r for r in world.db.tables["assistant_schedule_runs"] if r["schedule_id"] == schedule_id]


def _sched(world, schedule_id=SCHEDULE) -> dict:
    return next(r for r in world.db.tables["assistant_schedules"] if r["id"] == schedule_id)


def _messages(world) -> list[dict]:
    return world.db.tables["assistant_messages"]


def _claimed_run(world, **over) -> dict:
    """A run as the claim hands it back (status claimed, in the table)."""
    row = schedule_run(**over)
    world.db.tables["assistant_schedule_runs"].append(row)
    return dict(row)


def _types(world) -> list[str]:
    return [b["type"] for b in world.bells]


# ── The claim ────────────────────────────────────────────────────────────────


def test_two_workers_polling_the_same_claim_run_each_occurrence_once(world, monkeypatch):
    _due(world)
    _due(world, id=SCHEDULE2, title="Bids due this week, weekdays at 7:00 AM", recipe="due_this_week")
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def worker():
        barrier.wait()
        results.append(sched.poll_once(NOW))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["claimed"] for r in results) == 2
    for sid in (SCHEDULE, SCHEDULE2):
        runs = _runs(world, sid)
        assert len(runs) == 1 and runs[0]["status"] == "delivered"
    assert len(_messages(world)) == 2
    # A third poll finds nothing due.
    assert sched.poll_once(NOW)["claimed"] == 0
    assert len(world.db.tables["assistant_schedule_runs"]) == 2


def test_a_run_now_row_waits_while_a_worker_holds_a_scheduled_run(world):
    _add_schedule(world)
    held = _claimed_run(world, status="running", claimed_by="worker-a", created_at=NOW.isoformat())
    manual = _claimed_run(world, id=RUN2, kind="run_now", claimed_by=None, started_at=None,
                          scheduled_for=(NOW.replace(microsecond=NOW.microsecond or 1)).isoformat(),
                          created_at=NOW.isoformat())
    sched.poll_once(NOW)
    row = next(r for r in world.db.tables["assistant_schedule_runs"] if r["id"] == manual["id"])
    assert row["claimed_by"] is None and row["status"] == "claimed"
    next(r for r in world.db.tables["assistant_schedule_runs"] if r["id"] == held["id"])["status"] = "delivered"
    sched.poll_once(NOW)
    assert row["status"] == "delivered" and row["claimed_by"] == sched.WORKER_ID


def test_a_scheduled_slot_due_while_a_run_now_row_is_open_is_skipped_overlap(world):
    _due(world)
    _claimed_run(world, id=RUN2, kind="run_now", claimed_by=None, started_at=None,
                 scheduled_for=(NOW - timedelta(minutes=2)).replace(microsecond=7).isoformat(),
                 created_at=NOW.isoformat())
    counts = sched.poll_once(NOW)
    scheduled = [r for r in _runs(world) if r["kind"] == "scheduled"]
    assert len(scheduled) == 1 and scheduled[0]["status"] == "skipped" and scheduled[0]["error_kind"] == "overlap"
    assert counts["skipped"] == 1
    manual = next(r for r in _runs(world) if r["kind"] == "run_now")
    assert manual["status"] == "delivered"


def test_overlap_skips_a_slot_while_the_previous_run_is_still_open(world):
    _due(world)
    _claimed_run(world, status="running", claimed_by="worker-a",
                 scheduled_for=(NOW - timedelta(days=1)).isoformat(), created_at=NOW.isoformat())
    sched.poll_once(NOW)
    new = [r for r in _runs(world) if r["id"] != schedule_run()["id"]]
    assert [r["error_kind"] for r in new] == ["overlap"]
    assert _sched(world)["consecutive_failures"] == 0


# ── Refill and outages ───────────────────────────────────────────────────────


def test_refill_fills_the_second_slot_and_never_writes_next_run_at(world):
    nxt = (NOW + timedelta(hours=1)).isoformat()
    row = _add_schedule(world, next_run_at=nxt, after_next_run_at=None)
    assert sched.refill(NOW) == 1
    expected = rrules.occurrences(WEEKDAYS_7AM, sched._ts(row["dtstart"]), sched._ts(nxt), 1, frozenset())[0]
    assert _sched(world)["next_run_at"] == nxt
    assert sched._ts(_sched(world)["after_next_run_at"]) == expected
    # A filled slot is never refilled.
    assert sched.refill(NOW) == 0


# A Wednesday, 8:00 AM Pacific: one hour after that day's 7:00 AM slot.
WED_8AM = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def _pt_7am(day: int) -> datetime:
    """7:00 AM Pacific on 2026-10-<day> (PDT), as UTC."""
    return datetime(2026, 10, day, 14, 0, tzinfo=timezone.utc)


def test_a_two_day_outage_with_an_empty_second_slot_leaves_one_missed_row_then_a_catch_up(world):
    world.db.now = WED_8AM
    _add_schedule(world, next_run_at=_pt_7am(19).isoformat(), after_next_run_at=None,
                  dtstart=_pt_7am(12).isoformat())
    for _ in range(3):
        sched.poll_once(WED_8AM)
    runs = sorted(_runs(world), key=lambda r: r["scheduled_for"])
    assert [(r["scheduled_for"], r["status"], r["error_kind"]) for r in runs] == [
        (_pt_7am(19).isoformat(), "skipped", "missed"),
        (_pt_7am(21).isoformat(), "delivered", None),
    ]
    assert _sched(world)["next_run_at"] == _pt_7am(22).isoformat()


def test_an_outage_with_both_slots_filled_records_the_two_stale_slots_once_each(world):
    world.db.now = WED_8AM
    _add_schedule(world, next_run_at=_pt_7am(12).isoformat(), after_next_run_at=_pt_7am(13).isoformat(),
                  dtstart=_pt_7am(5).isoformat())
    for _ in range(4):
        sched.poll_once(WED_8AM)
    runs = sorted(_runs(world), key=lambda r: r["scheduled_for"])
    assert [(r["scheduled_for"], r["status"], r["error_kind"]) for r in runs] == [
        (_pt_7am(12).isoformat(), "skipped", "missed"),
        (_pt_7am(13).isoformat(), "skipped", "missed"),
        (_pt_7am(21).isoformat(), "delivered", None),
    ]


def test_an_hourly_outage_delivers_once_not_once_per_stale_slot(world):
    """SCHED-4: an occurrence whose next slot is already due is superseded
    (skipped missed), so the backlog collapses to the newest due run."""
    hourly = "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;BYHOUR=7,8,9,10,11,12,13,14,15,16,17;BYMINUTE=0"

    def at(h: int) -> datetime:
        return datetime(2026, 10, 21, h + 7, 0, tzinfo=timezone.utc)   # h:00 PDT

    _add_schedule(world, rrule=hourly, next_run_at=at(8).isoformat(), after_next_run_at=at(9).isoformat(),
                  dtstart=_pt_7am(5).isoformat(), delivery="chat_bell_email")
    for minute in range(5, 12):
        now = at(12) + timedelta(minutes=minute)
        world.db.now = now
        sched.poll_once(now)
    runs = sorted(_runs(world), key=lambda r: r["scheduled_for"])
    delivered = [r for r in runs if r["status"] == "delivered"]
    assert [r["scheduled_for"] for r in delivered] == [at(12).isoformat()]
    assert all((r["status"], r["error_kind"]) == ("skipped", "missed") for r in runs if r not in delivered)
    assert len(_messages(world)) == 1 and len(world.emails) == 1


def test_two_pending_run_now_rows_are_claimed_one_at_a_time(world):
    """SCHED-1 defense in depth: the manual CTE takes the oldest unclaimed
    Run now row of a schedule; the newer one waits."""
    _add_schedule(world)
    first = _claimed_run(world, kind="run_now", claimed_by=None, started_at=None,
                         scheduled_for=NOW.replace(microsecond=11).isoformat(),
                         created_at=(NOW - timedelta(seconds=2)).isoformat())
    world.db.tables["assistant_schedule_runs"].append(schedule_run(
        id=RUN2, kind="run_now", claimed_by=None, started_at=None,
        scheduled_for=NOW.replace(microsecond=12).isoformat(), created_at=(NOW - timedelta(seconds=1)).isoformat()))
    claimed = repo.claim_due_schedules(None, worker="w", limit=5, catchup_seconds=10800)
    assert [r["id"] for r in claimed] == [first["id"]]


def test_a_rule_with_no_future_run_pauses_with_a_bell(world, monkeypatch):
    _add_schedule(world, after_next_run_at=None)
    monkeypatch.setattr(rrules, "occurrences", lambda *a, **k: [])
    assert sched.refill(NOW) == 0
    row = _sched(world)
    assert row["status"] == "paused" and row["paused_reason"] == "no_future_run" and row["paused_at"]
    assert _types(world) == [sched.BELL_PAUSED]


# ── The owner ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("setup, status, reason", [
    (lambda w: w.db.tables["profiles"][0].update(is_active=False), "paused", "owner_changed"),
    (lambda w: w.db.tables["profiles"][0].update(role="estimator"), "paused", "owner_changed"),
    (lambda w: w.db.tables["assistant_settings"][0].update(enabled=False), "paused", "owner_changed"),
    (lambda w: w.db.tables["assistant_delegations"].clear(), "paused", "no_delegation"),
    (lambda w: w.flags.update(bidding=False), "paused_feature_off", None),
])
def test_owner_problems_pause_with_the_right_reason_and_never_count_a_failure(world, setup, status, reason):
    _due(world)
    setup(world)
    sched.poll_once(NOW)
    row = _sched(world)
    assert row["status"] == status and row["paused_reason"] == reason and row["paused_at"]
    assert row["consecutive_failures"] == 0
    run = _runs(world)[0]
    assert run["status"] == "skipped"
    assert run["error_kind"] == ("feature_off" if status == "paused_feature_off" else "owner_changed")
    assert world.state["reads"] == [] and _messages(world) == []
    assert _types(world) == ([sched.BELL_PAUSED] if status == "paused_feature_off" else [])


def test_a_run_of_a_schedule_no_longer_active_is_skipped(world):
    _add_schedule(world, status="paused", paused_reason="failures", paused_at=NOW.isoformat())
    run = _claimed_run(world)
    assert sched.run_claimed(run, NOW) == "not_active"
    assert _runs(world)[0]["error_kind"] == "not_active"


# ── Skips ────────────────────────────────────────────────────────────────────


def test_a_weekday_rule_skips_a_holiday_and_a_daily_rule_runs_on_it(world):
    when = NOW - timedelta(minutes=1)
    world.db.tables["assistant_settings"][0]["holidays"] = [when.astimezone(PT).date().isoformat()]
    _add_schedule(world)
    _add_schedule(world, id=SCHEDULE2, rrule=DAILY_7AM, title="Bids due today, daily at 7:00 AM")
    run = _claimed_run(world, scheduled_for=when.isoformat())
    run2 = _claimed_run(world, id=RUN2, schedule_id=SCHEDULE2, scheduled_for=when.isoformat())
    assert sched.run_claimed(run, NOW) == "holiday"
    assert sched.run_claimed(run2, NOW) == "delivered"
    assert _sched(world)["consecutive_failures"] == 0


def test_the_digest_stub_skips_a_scheduled_due_today_run_once_the_digest_went_out(world):
    world.state["payload"] = _payload(digest_sent_today=True)
    _add_schedule(world)
    assert sched.run_claimed(_claimed_run(world), NOW) == "digest_sent"
    assert _runs(world)[0]["status"] == "skipped" and _messages(world) == []
    # A Run now click still delivers.
    manual = _claimed_run(world, id=RUN2, kind="run_now", scheduled_for=NOW.replace(microsecond=3).isoformat())
    assert sched.run_claimed(manual, NOW) == "delivered"


def test_the_digest_stub_never_applies_to_an_accountant(world):
    world.db.tables["profiles"][0]["role"] = "accountant"
    world.state["payload"] = _payload(digest_sent_today=True)
    _add_schedule(world)
    assert sched.run_claimed(_claimed_run(world), NOW) == "delivered"


def test_only_if_changed_records_no_change_and_run_now_ignores_it(world):
    payload = world.state["payload"]
    digest = recipes.result_hash("due_today", payload["rows"])
    _add_schedule(world, only_if_changed=True, last_result_hash=digest, consecutive_failures=1)
    assert sched.run_claimed(_claimed_run(world), NOW) == "no_change"
    run = _runs(world)[0]
    assert run["status"] == "no_change" and run["stats"] == {"rows": 2} and run["finished_at"]
    assert _sched(world)["consecutive_failures"] == 0 and _messages(world) == []
    manual = _claimed_run(world, id=RUN2, kind="run_now", scheduled_for=NOW.replace(microsecond=5).isoformat())
    assert sched.run_claimed(manual, NOW) == "delivered"
    assert len(_messages(world)) == 1
    assert _sched(world)["last_result_hash"] == digest      # Run now never writes it


def test_a_watch_whose_bid_closed_ends_the_routine(world):
    world.state["payload"] = _payload(0, "labor_not_entered", watch={"project_id": P1, "state": "closed"})
    _add_schedule(world, recipe="labor_not_entered", params={"project_id": P1}, project_id=P1,
                  title="Labor not entered on 26.10.7200, weekdays at 8:00 AM")
    assert sched.run_claimed(_claimed_run(world), NOW) == "watch_finished"
    row = _sched(world)
    assert row["status"] == "ended" and row["ended_reason"] == "watch_finished" and row["ended_at"]
    run = _runs(world)[0]
    assert run["status"] == "skipped" and run["error_kind"] == "watch_finished"
    assert [m["content"] for m in _messages(world)] == [sched.TEXT_WATCH_ENDED]
    assert _types(world) == [sched.BELL_ENDED]
    assert world.bells[0]["project_id"] == P1


def test_a_watch_whose_project_was_deleted_bells_without_the_project_fk(world):
    """SCHED-3: notifications.project_id references projects(id), so a gone
    project's id goes in the metadata only and the ended bell still lands."""
    world.state["payload"] = _payload(0, "labor_not_entered", watch={"project_id": P1, "state": "missing"})
    _add_schedule(world, recipe="labor_not_entered", params={"project_id": P1}, project_id=P1,
                  title="Labor not entered on 26.10.7200, weekdays at 8:00 AM")
    assert sched.run_claimed(_claimed_run(world), NOW) == "watch_finished"
    assert _types(world) == [sched.BELL_ENDED]
    assert world.bells[0]["project_id"] is None and world.bells[0]["metadata"]["project_id"] == P1


def _reap_during(world, monkeypatch, name):
    """Fail the run (as the reaper would) just before `name` runs."""
    real = getattr(sched, name)

    def wrapped(run, *a, **k):
        row = next(r for r in world.db.tables["assistant_schedule_runs"] if r["id"] == run["id"])
        row["status"] = "failed"
        sched_row = _sched(world)
        sched_row["consecutive_failures"] = int(sched_row.get("consecutive_failures") or 0) + 1
        return real(run, *a, **k)

    monkeypatch.setattr(sched, name, wrapped)


def test_a_run_reaped_before_its_chat_post_is_lost_and_keeps_the_failure(world, monkeypatch):
    """SCHED-2: a refused chat claim is not a delivery: no bell, no email, and
    neither last_result_hash nor the failure count is touched."""
    _add_schedule(world, delivery="chat_bell_email", only_if_changed=True)
    _reap_during(world, monkeypatch, "_post_chat")
    assert sched.run_claimed(_claimed_run(world), NOW) == "lost"
    row = _sched(world)
    assert row["consecutive_failures"] == 1 and row.get("last_result_hash") is None
    assert _messages(world) == [] and world.bells == [] and world.emails == []


def test_a_run_reaped_after_its_chat_post_keeps_the_failure(world, monkeypatch):
    """SCHED-2: the delivered update lost its fence (reaped mid-delivery),
    so the reaper's failure count and the old result hash stay."""
    from app.assistant import repo

    _add_schedule(world, delivery="chat", only_if_changed=True)

    real_update = repo.update_run

    def update_run(user_id, run_id, patch, **kw):
        if patch.get("status") == "delivered":
            row = next(r for r in world.db.tables["assistant_schedule_runs"] if r["id"] == run_id)
            row["status"] = "failed"
            _sched(world)["consecutive_failures"] = 1
        return real_update(user_id, run_id, patch, **kw)

    monkeypatch.setattr(repo, "update_run", update_run)
    assert sched.run_claimed(_claimed_run(world), NOW) == "lost"
    row = _sched(world)
    assert row["consecutive_failures"] == 1 and row.get("last_result_hash") is None


# ── Delivery ─────────────────────────────────────────────────────────────────


def test_delivery_goes_chat_then_bell_then_email_inside_the_run_scope(world):
    _add_schedule(world, delivery="chat_bell_email")
    run = _claimed_run(world)
    assert sched.run_claimed(run, NOW) == "delivered"
    assert world.order == ["chat", f"bell:{sched.BELL_DELIVERED}", "email"]
    row = _runs(world)[0]
    assert row["status"] == "delivered" and row["error_kind"] is None
    assert row["delivered"] == {"chat": True, "bell": True, "email": "log-1"}
    assert row["message_id"] == sched.message_id_for(run["id"]) and row["stats"] == {"rows": 2}
    # Both side effects carry the run's provenance (actor_kind 'assistant').
    assert world.bells[0]["actor"] == provenance.AssistantActor(USER, run["id"])
    assert world.emails[0]["actor"] == provenance.AssistantActor(USER, run["id"])
    assert world.bells[0]["mirror_email"] is False
    assert world.bells[0]["metadata"]["schedule_run_id"] == run["id"]
    assert world.emails[0]["to"] == ["tom@g3.test"] and world.emails[0]["sent_by"] == USER
    assert world.emails[0]["subject"] == "G3 BDR · Your routine: Bids due today, weekdays at 7:00 AM (2)"
    assert EM_DASH not in world.emails[0]["subject"]
    # The message: counts only, names in the table, in a tainted schedule conversation.
    msg = _messages(world)[0]
    assert "Probe job" not in msg["content"] and msg["role"] == "assistant" and msg["status"] == "done"
    assert any(b["type"] == "table" for b in msg["blocks"])
    conv = world.db.tables["assistant_conversations"][0]
    assert conv["surface"] == "schedule" and conv["tainted_at"] and conv["title"] == _sched(world)["title"]
    assert _sched(world)["conversation_id"] == conv["id"]
    s = _sched(world)
    assert s["unread_deliveries"] == 1 and s["last_delivered_at"] and s["last_result_hash"] == row["result_hash"]


def test_a_channel_already_claimed_is_never_sent_again(world):
    row = _add_schedule(world, delivery="chat_bell_email")
    run = _claimed_run(world, status="running", delivered={"chat": "claimed", "bell": "claimed", "email": True})
    # A refused claim with no message row means nothing was posted (SCHED-2).
    assert sched._post_chat(run, dict(row), "x", []) is None
    assert sched._ring(run, dict(row), _payload()) is False
    assert sched._email(run, dict(row), _payload(), profile(), NOW) == (False, None)
    assert _messages(world) == [] and world.bells == [] and world.emails == []


def test_an_empty_result_is_delivered_to_chat_only(world):
    world.state["payload"] = _payload(0)
    _add_schedule(world, delivery="chat_bell_email")
    assert sched.run_claimed(_claimed_run(world), NOW) == "delivered"
    assert world.order == ["chat"]
    assert _messages(world)[0]["content"] == "No bids are due today."
    assert _runs(world)[0]["delivered"] == {"chat": True}
    assert _sched(world)["unread_deliveries"] == 0


def test_the_email_cap_records_email_cap_and_chat_and_bell_still_deliver(world):
    world.db.tables["email_log"].extend(
        {"id": f"old-{i}", "sent_by": USER, "actor_kind": "assistant", "created_at": NOW.isoformat()}
        for i in range(20))
    # A human email and someone else's assistant email do not count.
    world.db.tables["email_log"].append({"id": "h", "sent_by": USER, "actor_kind": "human",
                                         "created_at": NOW.isoformat()})
    _add_schedule(world, delivery="chat_bell_email")
    assert sched.run_claimed(_claimed_run(world), NOW) == "delivered"
    row = _runs(world)[0]
    assert row["status"] == "delivered" and row["error_kind"] == "email_cap"
    assert row["delivered"] == {"chat": True, "bell": True, "email": False}
    assert world.emails == [] and _types(world) == [sched.BELL_DELIVERED]
    assert _sched(world)["consecutive_failures"] == 0


def test_the_twentieth_email_still_goes_out(world):
    world.db.tables["email_log"].extend(
        {"id": f"old-{i}", "sent_by": USER, "actor_kind": "assistant", "created_at": NOW.isoformat()}
        for i in range(19))
    _add_schedule(world, delivery="chat_bell_email")
    sched.run_claimed(_claimed_run(world), NOW)
    assert len(world.emails) == 1 and _runs(world)[0]["error_kind"] is None


def test_an_email_failure_marks_email_failed_on_a_delivered_run(world):
    world.mail["fail"] = True
    _add_schedule(world, delivery="chat_bell_email")
    assert sched.run_claimed(_claimed_run(world), NOW) == "delivered"
    row = _runs(world)[0]
    assert row["status"] == "delivered" and row["error_kind"] == "email_failed"
    assert row["delivered"]["email"] is False and _sched(world)["consecutive_failures"] == 0


def test_no_graph_setup_marks_email_unavailable(world):
    world.s.ms_client_id = ""
    _add_schedule(world, delivery="chat_bell_email")
    sched.run_claimed(_claimed_run(world), NOW)
    assert _runs(world)[0]["error_kind"] == "email_unavailable" and world.emails == []


def test_two_runs_racing_to_create_the_conversation_share_the_one_that_won(world):
    row = _add_schedule(world)
    stale_a, stale_b = dict(row), dict(row)
    run_a = _claimed_run(world, status="running")
    run_b = _claimed_run(world, id=RUN2, status="running",
                         scheduled_for=(NOW - timedelta(minutes=2)).isoformat())
    sched._post_chat(run_a, stale_a, "first", [])
    sched._post_chat(run_b, stale_b, "second", [])
    linked = _sched(world)["conversation_id"]
    assert linked and len(world.db.tables["assistant_conversations"]) == 2
    assert [m["conversation_id"] for m in _messages(world)] == [linked, linked]


def test_a_chat_post_that_fails_fails_the_run(world, monkeypatch):
    _add_schedule(world)

    def boom(user_id, row):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "insert_message", boom)
    assert sched.run_claimed(_claimed_run(world), NOW) == "failed"
    run = _runs(world)[0]
    assert run["status"] == "failed" and run["error_kind"] == "chat_failed"
    assert run["delivered"] == {"chat": False}
    assert _sched(world)["consecutive_failures"] == 1


# ── Failures, unread, the reaper ─────────────────────────────────────────────


def test_three_failures_pause_the_routine_with_a_bell(world):
    world.state["payload"] = RequestFailed("http_500", "HTTP 500")
    _add_schedule(world, consecutive_failures=1)
    assert sched.run_claimed(_claimed_run(world), NOW) == "failed"
    assert _sched(world)["consecutive_failures"] == 2 and _sched(world)["status"] == "active"
    assert _runs(world)[0]["error_kind"] == "http_500"
    run2 = _claimed_run(world, id=RUN2, scheduled_for=(NOW - timedelta(minutes=3)).isoformat())
    assert sched.run_claimed(run2, NOW) == "failed"
    row = _sched(world)
    assert row["status"] == "paused" and row["paused_reason"] == "failures" and row["consecutive_failures"] == 3
    assert _types(world) == [sched.BELL_PAUSED] and "3 failed runs" in world.bells[0]["message"]


def test_a_firewall_refusal_and_a_crash_are_failures_with_their_kind(world):
    world.state["payload"] = RequestFailed("firewall", "refused")
    _add_schedule(world)
    sched.run_claimed(_claimed_run(world), NOW)
    assert _runs(world)[0]["error_kind"] == "firewall"
    world.state["payload"] = ValueError("bad")
    sched.run_claimed(_claimed_run(world, id=RUN2, scheduled_for=NOW.replace(microsecond=9).isoformat()), NOW)
    assert next(r for r in _runs(world) if r["id"] == RUN2)["error_kind"] == "recipe_failed"


def test_a_delivered_run_resets_the_failure_count(world):
    _add_schedule(world, consecutive_failures=2)
    sched.run_claimed(_claimed_run(world), NOW)
    assert _sched(world)["consecutive_failures"] == 0


def test_ten_unread_deliveries_pause_the_routine_with_a_bell(world):
    _add_schedule(world, unread_deliveries=9)
    assert sched.run_claimed(_claimed_run(world), NOW) == "delivered"
    row = _sched(world)
    assert row["status"] == "paused" and row["paused_reason"] == "unread" and row["unread_deliveries"] == 10
    assert _types(world) == [sched.BELL_DELIVERED, sched.BELL_PAUSED]


def test_the_reaper_fails_abandoned_and_unpicked_runs_and_counts_them(world):
    _add_schedule(world)
    old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    _claimed_run(world, status="running", claimed_by="worker-a", started_at=old, created_at=old)
    _claimed_run(world, id=RUN2, kind="run_now", claimed_by=None, started_at=None, created_at=old,
                 scheduled_for=(NOW - timedelta(minutes=20)).replace(microsecond=1).isoformat())
    assert sched.reap(NOW) == 2
    kinds = {r["id"]: (r["status"], r["error_kind"]) for r in _runs(world)}
    assert kinds[schedule_run()["id"]] == ("failed", "abandoned")
    assert kinds[RUN2] == ("failed", "not_picked_up")
    assert _sched(world)["consecutive_failures"] == 2


# ── The resume pass ──────────────────────────────────────────────────────────


def test_resume_pass_restarts_a_feature_off_routine_once_bidding_is_back(world):
    _add_schedule(world, status="paused_feature_off", paused_at=NOW.isoformat(),
                  next_run_at=(NOW - timedelta(days=3)).isoformat(), after_next_run_at=None)
    world.flags["bidding"] = False
    assert sched.resume_pass(NOW) == 0
    world.flags["bidding"] = True
    assert sched.resume_pass(NOW) == 1
    row = _sched(world)
    assert row["status"] == "active" and row["paused_at"] is None
    assert sched._ts(row["next_run_at"]) > NOW and sched._ts(row["after_next_run_at"]) > sched._ts(row["next_run_at"])
    assert _types(world) == [sched.BELL_RESUMED]


@pytest.mark.parametrize("reason", ["mfa_reset", "no_delegation"])
def test_resume_pass_needs_a_grant_made_after_the_pause(world, reason):
    paused_at = NOW - timedelta(hours=1)
    _add_schedule(world, status="paused", paused_reason=reason, paused_at=paused_at.isoformat())
    world.db.tables["assistant_delegations"][0]["created_at"] = (paused_at - timedelta(minutes=5)).isoformat()
    assert sched.resume_pass(NOW) == 0 and _sched(world)["status"] == "paused"
    world.db.tables["assistant_delegations"][0]["created_at"] = (paused_at + timedelta(minutes=5)).isoformat()
    assert sched.resume_pass(NOW) == 1 and _sched(world)["status"] == "active"
    assert _sched(world)["paused_reason"] is None


def test_resume_pass_leaves_a_routine_the_person_paused(world):
    _add_schedule(world, status="paused", paused_reason="user", paused_at=NOW.isoformat())
    assert sched.resume_pass(NOW) == 0 and _sched(world)["status"] == "paused"


def test_resume_pass_leaves_the_row_paused_over_the_cap(world):
    for i in range(10):
        _add_schedule(world, id=f"ffffffff-ffff-4fff-8fff-{i:012d}")
    _add_schedule(world, status="paused_feature_off", paused_at=NOW.isoformat())
    assert sched.resume_pass(NOW) == 0
    assert _sched(world)["status"] == "paused_feature_off"


# ── Run now ──────────────────────────────────────────────────────────────────


def test_a_run_now_row_is_picked_up_by_the_claim_and_never_moves_next_run_at(world):
    row = _add_schedule(world, status="paused", paused_reason="user", paused_at=NOW.isoformat(),
                        only_if_changed=True,
                        last_result_hash=recipes.result_hash("due_today", world.state["payload"]["rows"]))
    _claimed_run(world, kind="run_now", claimed_by=None, started_at=None, created_at=NOW.isoformat(),
                 scheduled_for=NOW.replace(microsecond=NOW.microsecond or 1).isoformat())
    sched.poll_once(NOW)
    run = _runs(world)[0]
    assert run["status"] == "delivered" and run["claimed_by"] == sched.WORKER_ID
    assert _sched(world)["next_run_at"] == row["next_run_at"] and _sched(world)["status"] == "paused"


# ── The read through the shim ────────────────────────────────────────────────


def test_read_recipe_goes_through_the_executor_with_a_token_for_the_run(world, monkeypatch):
    sent: list[tuple] = []

    def send(self, req, token):
        sent.append((req, principal.verify_token(token, settings=world.s)))
        return 200, {"recipe": "missing_quotes", "rows": []}

    monkeypatch.setattr(Executor, "_send_via_shim", send)
    row = _add_schedule(world, recipe="missing_quotes", params={"horizon_days": 5})
    run = _claimed_run(world)
    payload = REAL_READ_RECIPE(run, row, Role.EXECUTIVE)
    assert payload == {"recipe": "missing_quotes", "rows": []}
    req, claims = sent[0]
    assert req.route == recipes.RECIPE_ROWS_ROUTE and req.path == "/assistant/recipes/missing_quotes/rows"
    assert req.params["horizon_days"] == 5 and sched._ts(req.params["at"]) == sched._ts(run["scheduled_for"])
    assert (claims.user_id, claims.run_id, claims.approval_id) == (USER, run["id"], None)


@pytest.mark.parametrize("recipe, key, value", [
    ("estimator_vs_bid", "period", "last_month"),
    ("quote_readiness", "week", "next_week"),
])
def test_read_recipe_forwards_the_period_and_the_week(world, monkeypatch, recipe, key, value):
    sent: list = []

    def send(self, req, token):
        sent.append(req)
        return 200, {"recipe": recipe, "rows": []}

    monkeypatch.setattr(Executor, "_send_via_shim", send)
    row = _add_schedule(world, recipe=recipe, params={key: value})
    REAL_READ_RECIPE(_claimed_run(world), row, Role.EXECUTIVE)
    assert sent[0].params[key] == value
    assert sent[0].path == f"/assistant/recipes/{recipe}/rows"
    # A routine without the param sends none (the route applies its default).
    sent.clear()
    row["params"] = {}
    REAL_READ_RECIPE(_claimed_run(world, id=RUN2), row, Role.EXECUTIVE)
    assert key not in sent[0].params


def test_an_estimator_vs_bid_routine_of_a_demoted_owner_pauses_as_owner_changed(world):
    _due(world, recipe="estimator_vs_bid", params={"period": "last_month"}, title="Estimator vs our price, monthly")
    world.db.tables["profiles"][0]["role"] = "estimating_engineer_labor"     # still internal, lost the viewer role
    sched.poll_once(NOW)
    row = _sched(world)
    assert row["status"] == "paused" and row["paused_reason"] == "owner_changed" and row["paused_at"]
    assert row["consecutive_failures"] == 0
    run = _runs(world)[0]
    assert run["status"] == "skipped" and run["error_kind"] == "owner_changed"
    assert world.state["reads"] == [] and _messages(world) == [] and _types(world) == []


def test_an_estimator_vs_bid_routine_of_a_viewer_role_still_runs(world):
    world.state["payload"] = _payload(recipe="estimator_vs_bid")
    _due(world, recipe="estimator_vs_bid", params={"period": "last_month"}, title="Estimator vs our price, monthly")
    sched.poll_once(NOW)
    assert _sched(world)["status"] == "active"
    assert _runs(world)[0]["status"] == "delivered"
    assert world.state["reads"][0]["schedule"]["params"] == {"period": "last_month"}


def test_a_routine_open_to_every_internal_role_is_not_paused_for_a_role_change(world):
    _due(world, recipe="quote_readiness", params={"week": "next_week"}, title="Key quotes, weekly")
    world.db.tables["profiles"][0]["role"] = "estimating_engineer_labor"
    world.state["payload"] = _payload(recipe="quote_readiness")
    sched.poll_once(NOW)
    assert _sched(world)["status"] == "active" and _runs(world)[0]["status"] == "delivered"


# ── The maintenance rider ────────────────────────────────────────────────────


def test_tick_runs_only_with_maintenance_enabled(world, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(assistant_maintenance, "tick", lambda now, *, worker: calls.append({"worker": worker}))
    sched.poll_once(NOW)
    assert calls == []
    world.s.assistant_maintenance_enabled = True
    sched.poll_once(NOW)
    assert calls == [{"worker": sched.WORKER_ID}]


def test_one_crashing_run_does_not_stop_the_others(world, monkeypatch):
    _due(world)
    _due(world, id=SCHEDULE2, recipe="due_this_week", title="Bids due this week, weekdays at 7:00 AM")
    real = sched.run_claimed

    def flaky(run, now=None):
        if run["schedule_id"] == SCHEDULE:
            raise RuntimeError("boom")
        return real(run, now)

    monkeypatch.setattr(sched, "run_claimed", flaky)
    counts = sched.poll_once(NOW)
    assert counts["failed"] == 1 and counts["delivered"] == 1
    assert _runs(world)[0]["error_kind"] == "crashed"
    assert _runs(world, SCHEDULE2)[0]["status"] == "delivered"


def test_fixed_texts_carry_no_em_dash():
    texts = [sched.TEXT_WATCH_ENDED, sched.BELL_TEXT_FAILURES, sched.BELL_TEXT_UNREAD, sched.BELL_TEXT_FEATURE_OFF,
             sched.BELL_TEXT_NO_FUTURE_RUN, sched.BELL_TEXT_RESUMED, sched.BELL_TEXT_WATCH_ENDED,
             *sched.TEXT_FAILED.values()]
    assert all(EM_DASH not in t for t in texts)
    with open(sched.__file__, encoding="utf-8") as fh:
        assert EM_DASH not in fh.read()
