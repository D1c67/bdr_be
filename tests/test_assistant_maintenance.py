"""The assistant's maintenance riders (app/services/assistant_maintenance.py;
docs/ASSISTANT_PHASE3_PLAN.md 3.5), over the FakeDB.

Invariants pinned here:

  tick       each rider runs once its per-process interval passed; one
             failing rider never stops the others;
  approvals  a pending card past expires_at expires timed_out; an owner
             who left expires owner_offboarded; a tool whose sub-app is off
             expires feature_off with the bell; a card on an abandoned,
             failed-to-bid, declined or deleted project expires
             project_closed with the bell, and a card on a submitted
             project stays pending; a stranded approved card is recovered;
  staging    the system sweep is purge_expired(user_id=None, limit=50);
  nightly    only between 02:00 and 05:00 Pacific, claimed once per day
             across two workers; expired tool results deleted, archived
             memories older than a year deleted, unused and stale pending
             memories archived, owners on legal hold excluded throughout;
             project notes moved on a merge, archived when the project is
             gone, abandoned, failed to bid or lost; counts recorded;
  orphans    once per day; owners with open state and a missing, inactive
             or non-internal profile are offboarded in mode 'sweep';
  plans_notes_sweep  (phase 4) at most 10 lagging runs of the last 14 days
             are synced per sweep, a run that wrote nothing sits out;
  ae_task_sweep  (phase 4) open page reads and runs advance, owners with no
             live delegation are skipped, at most 20 are handled, a
             stranded composite route is cleaned up (run canceled, hold
             released, task route_interrupted), concurrent sweeps are no-ops;
  report_purge  (phase 4) claimed once per Pacific day across two workers;
             the object is deleted and verified gone before the row is
             marked purged, legal hold owners are skipped, orphan objects
             older than a day with no row are removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.assistant import access, plans_tasks, repo
from app.core import features, supabase_client
from app.services import assistant_ai_estimate, assistant_offboarding, assistant_staging, llm_queue, notifications
from app.services import assistant_maintenance as maint
from app.services import plans_reader_notes, storage
from tests.assistant_fakes import (
    APPROVAL_ID,
    MEMORY,
    MEMORY2,
    NOW,
    OTHER_USER,
    PROJECT,
    PROJECT2,
    USER,
    CheckViolation,
    FakeDB,
    UniqueViolation,
    approval,
    delegation,
    memory_row,
    profile,
    inbox_row,
    report,
    reservation,
    schedule,
    send_row,
    settings_row,
    subscription_row,
    task,
)

EM_DASH = chr(0x2014)
# 03:00 AM Pacific (PDT) on 2026-10-23, inside the nightly window.
NIGHT = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
A2 = "99999999-9999-4999-8999-999999999992"
A3 = "99999999-9999-4999-8999-999999999993"
A4 = "99999999-9999-4999-8999-999999999994"
A5 = "99999999-9999-4999-8999-999999999995"
P_ABANDONED = "70707070-7070-4070-8070-707070707071"
P_FTB = "70707070-7070-4070-8070-707070707072"
P_DECLINED = "70707070-7070-4070-8070-707070707073"
P_GONE = "70707070-7070-4070-8070-707070707074"
P_MERGED = "70707070-7070-4070-8070-707070707075"
P_LOST = "70707070-7070-4070-8070-707070707076"
MEMORY3 = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc3"
MEMORY4 = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc4"
MEMORY5 = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc5"
MEMORY6 = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc6"


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _insert_sweep_0180(self, row: dict) -> None:
    """The fake's sweep insert with the 0180 B5 step check (report_purge);
    tests/assistant_fakes.py (WI-5) still lists only nightly and orphans."""
    row.pop("id", None)
    row.pop("created_at", None)
    row.setdefault("started_at", self.now_iso())
    row.setdefault("finished_at", None)
    row.setdefault("result", None)
    if row.get("step") not in ("nightly", "orphans", "report_purge"):
        raise CheckViolation("assistant_sweeps_step_check (check_violation)")
    if any(r.get("step") == row["step"] and str(r.get("sweep_date")) == str(row.get("sweep_date"))
           for r in self.tables.get("assistant_sweeps", [])):
        raise UniqueViolation("assistant_sweeps_pkey")


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    db = FakeDB(
        now=NOW, profiles=[profile(), profile(id=OTHER_USER, full_name="Ann Other", email="ann@g3.test")],
        assistant_delegations=[delegation()], assistant_settings=[settings_row()],
        assistant_profiles=[{"user_id": USER, "legal_hold": False}], assistant_conversations=[],
        assistant_messages=[], assistant_approvals=[], assistant_tool_results=[], assistant_tool_calls=[],
        assistant_memories=[], assistant_memory_events=[], assistant_memory_revisions=[],
        assistant_schedules=[], assistant_schedule_runs=[], assistant_sweeps=[], assistant_tasks=[],
        assistant_spend_reservations=[], projects=[], deleted_projects=[], bid_outcomes=[], llm_jobs=[],
        assistant_reports=[], plans_reader_runs=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(FakeDB, "_insert_sweep", _insert_sweep_0180)
    monkeypatch.setattr(maint, "_notes_idle", {})
    flags = {"bidding": True, "pm": True, "certified_payroll": True}
    monkeypatch.setattr(features, "enabled_map", lambda role=None: dict(flags))
    bells: list[dict] = []
    monkeypatch.setattr(notifications, "notify_user", lambda user_id, project_id, type_, message, **kw:
                        bells.append({"user_id": user_id, "type": type_, "message": message, **kw}))
    # notifications binds get_supabase by name, so the real offboarding
    # (run_orphans) would write its audit row to the real database.
    audits: list[tuple] = []
    monkeypatch.setattr(notifications, "audit", lambda *a, **kw: audits.append(a))
    monkeypatch.setattr(llm_queue, "latest_job", lambda job_type, target: None)
    monkeypatch.setattr(maint, "_last_run", {})
    monkeypatch.setattr(maint, "_claimed_dates", {})
    return SimpleNamespace(db=db, flags=flags, bells=bells, audits=audits)


def _card(world, **over) -> dict:
    row = approval(**over)
    world.db.tables["assistant_approvals"].append(row)
    return row


def _status(world, approval_id) -> tuple[str, str | None]:
    row = next(r for r in world.db.tables["assistant_approvals"] if r["id"] == approval_id)
    return row["status"], row.get("expired_reason")


# ── tick ─────────────────────────────────────────────────────────────────────


def test_tick_runs_each_rider_once_per_interval_and_survives_a_failure(world, monkeypatch):
    calls: list[str] = []

    def boom(now=None):
        calls.append("approvals")
        raise RuntimeError("boom")

    monkeypatch.setattr(maint, "run_approvals_sweep", boom)
    monkeypatch.setattr(maint, "run_tasks_sweep", lambda now=None: calls.append("tasks") or 0)
    monkeypatch.setattr(maint, "run_staging_sweep", lambda now=None: calls.append("staging") or 0)
    monkeypatch.setattr(maint, "run_plans_notes_sweep", lambda now=None: calls.append("plans_notes") or {})
    monkeypatch.setattr(maint, "run_ae_task_sweep", lambda now=None: calls.append("ae_tasks") or {})
    monkeypatch.setattr(maint, "run_nightly", lambda now=None, *, worker: calls.append("nightly"))
    monkeypatch.setattr(maint, "run_orphans", lambda now=None, *, worker: calls.append("orphans"))
    monkeypatch.setattr(maint, "run_report_purge", lambda now=None, *, worker: calls.append("report_purge"))
    out = maint.tick(DAY, worker="w1")
    assert calls == ["approvals", "tasks", "staging", "plans_notes", "ae_tasks", "nightly", "orphans",
                     "report_purge"]
    assert "approvals" not in out and out["tasks"] == 0
    assert out["plans_notes_sweep"] == {} and out["ae_task_sweep"] == {}
    calls.clear()
    maint.tick(DAY, worker="w1")
    # The interval riders wait; the daily ones decide for themselves.
    assert calls == ["nightly", "orphans", "report_purge"]


# ── approvals ────────────────────────────────────────────────────────────────


def test_a_card_on_a_closed_or_deleted_project_expires_and_a_submitted_one_stays(world):
    world.db.tables["projects"].extend([
        {"id": P_ABANDONED, "abandoned_at": NOW.isoformat(), "failed_to_bid_at": None, "current_stage": "bidding"},
        {"id": P_FTB, "abandoned_at": None, "failed_to_bid_at": NOW.isoformat(), "current_stage": "bidding"},
        {"id": P_DECLINED, "abandoned_at": None, "failed_to_bid_at": None, "current_stage": "declined"},
        {"id": PROJECT, "abandoned_at": None, "failed_to_bid_at": None, "current_stage": "submitted"},
    ])
    for aid, pid in ((APPROVAL_ID, P_ABANDONED), (A2, P_FTB), (A3, P_DECLINED), (A4, P_GONE), (A5, PROJECT)):
        _card(world, id=aid, project_id=pid, message_id=None)
    counts = maint.run_approvals_sweep(NOW)
    assert counts["project_closed"] == 4
    for aid in (APPROVAL_ID, A2, A3, A4):
        assert _status(world, aid) == ("expired", "project_closed")
    assert _status(world, A5) == ("pending", None)
    assert [b["type"] for b in world.bells] == ["assistant.approval_expired"] * 4


def test_a_timed_out_card_expires_without_a_bell(world):
    world.db.tables["projects"].append({"id": PROJECT, "current_stage": "bidding"})
    _card(world, expires_at=(NOW - timedelta(minutes=1)).isoformat(), message_id=None)
    counts = maint.run_approvals_sweep(datetime.now(timezone.utc))
    assert counts["timed_out"] == 1 and _status(world, APPROVAL_ID) == ("expired", "timed_out")
    assert world.bells == []


def test_a_card_of_an_owner_who_left_expires_owner_offboarded(world):
    world.db.tables["projects"].append({"id": PROJECT, "current_stage": "bidding"})
    world.db.tables["profiles"][0]["is_active"] = False
    _card(world, message_id=None)
    assert maint.run_approvals_sweep(NOW)["owner_offboarded"] == 1
    assert _status(world, APPROVAL_ID) == ("expired", "owner_offboarded")


def test_a_card_whose_sub_app_is_off_expires_feature_off_with_the_bell(world):
    world.db.tables["projects"].append({"id": PROJECT, "current_stage": "bidding"})
    world.flags["bidding"] = False
    _card(world, action_kind="set_labor_numbers", message_id=None)
    _card(world, id=A2, action_kind="todo_create", message_id=None)
    assert maint.run_approvals_sweep(NOW)["feature_off"] == 1
    assert _status(world, APPROVAL_ID) == ("expired", "feature_off")
    assert _status(world, A2) == ("pending", None)
    assert [b["type"] for b in world.bells] == ["assistant.approval_expired"]


def test_a_stranded_approved_card_is_recovered(world):
    _card(world, status="approved", decided_at=_ago(minutes=20), message_id=None, project_id=None)
    assert maint.run_approvals_sweep(NOW)["stranded"] == 1
    assert _status(world, APPROVAL_ID)[0] == "failed"


# ── tasks and staging ────────────────────────────────────────────────────────


def test_the_tasks_rider_does_nothing_while_bidding_is_on(world, monkeypatch):
    called: list = []
    monkeypatch.setattr(repo, "open_tasks", lambda *a, **k: called.append(a) or [], raising=False)
    assert maint.run_tasks_sweep(NOW) == 0 and called == []


def test_the_tasks_rider_stops_open_split_tasks_while_bidding_is_off(world, monkeypatch):
    from app.assistant import continue_job

    world.flags["bidding"] = False
    world.db.tables["assistant_tasks"].extend([
        task(), task(id="abababab-abab-4bab-8bab-ababababab02", status="done"),
        task(id="abababab-abab-4bab-8bab-ababababab03", kind="instruction"),
    ])
    stopped: list = []
    monkeypatch.setattr(continue_job, "stop_for_owner",
                        lambda user_id, t, problem, **kw: stopped.append((user_id, t["id"], problem)))
    assert maint.run_tasks_sweep(NOW) == 1
    assert stopped == [(USER, task()["id"], access.FEATURE_OFF)]


def test_the_staging_rider_is_the_bounded_system_sweep(world, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(assistant_staging, "purge_expired", lambda user_id, limit: calls.append(
        {"user_id": user_id, "limit": limit}) or 3)
    assert maint.run_staging_sweep(NOW) == 3
    assert calls == [{"user_id": None, "limit": 50}]


# ── nightly ──────────────────────────────────────────────────────────────────


def test_nightly_runs_only_inside_its_window(world):
    assert maint.run_nightly(DAY, worker="w1") is None
    assert world.db.tables["assistant_sweeps"] == []


def test_nightly_is_claimed_once_per_day_across_two_workers(world, monkeypatch):
    first = maint.run_nightly(NIGHT, worker="w1")
    assert first is not None
    # The second worker is another process: its own interval state.
    monkeypatch.setattr(maint, "_claimed_dates", {})
    assert maint.run_nightly(NIGHT, worker="w2") is None
    sweeps = world.db.tables["assistant_sweeps"]
    assert len(sweeps) == 1 and sweeps[0]["claimed_by"] == "w1" and sweeps[0]["sweep_date"] == "2026-10-23"
    assert sweeps[0]["finished_at"] and sweeps[0]["result"] == first
    # Same process, same day: no second claim attempt.
    calls = len(world.db.calls)
    assert maint.run_nightly(NIGHT, worker="w1") is None and len(world.db.calls) == calls


def test_nightly_purges_and_archives_with_legal_hold_excluded(world):
    db = world.db
    db.tables["assistant_profiles"].append({"user_id": OTHER_USER, "legal_hold": True})
    db.tables["assistant_tool_results"].extend([
        {"id": "r-old", "user_id": USER, "expires_at": _ago(days=1)},
        {"id": "r-new", "user_id": USER, "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()},
        {"id": "r-held", "user_id": OTHER_USER, "expires_at": _ago(days=1)},
    ])
    db.tables["assistant_memories"].extend([
        memory_row(id=MEMORY, last_used_at=_ago(days=200), updated_at=_ago(days=300)),
        memory_row(id=MEMORY2, title="Pinned", pinned=True, last_used_at=_ago(days=400)),
        memory_row(id=MEMORY3, title="Waiting", status="pending_review", updated_at=_ago(days=31)),
        memory_row(id=MEMORY4, title="Old archive", status="archived", archived_at=_ago(days=400)),
        memory_row(id=MEMORY5, user_id=OTHER_USER, title="Held archive", status="archived",
                   archived_at=_ago(days=400)),
        memory_row(id=MEMORY6, title="Fresh", last_used_at=_ago(days=3)),
    ])
    result = maint.run_nightly(NIGHT, worker="w1")
    assert result["tool_results_deleted"] == 1
    assert {r["id"] for r in db.tables["assistant_tool_results"]} == {"r-new", "r-held"}
    status = {m["id"]: m["status"] for m in db.tables["assistant_memories"]}
    assert status[MEMORY] == "archived" and status[MEMORY3] == "archived"
    assert status[MEMORY2] == "active" and status[MEMORY6] == "active"
    assert MEMORY4 not in status and status[MEMORY5] == "archived"
    assert result["memories_archived"] == 2 and result["memories_deleted"] == 1
    events = [(e["memory_id"], e["event"], e["actor"]) for e in db.tables["assistant_memory_events"]]
    assert (MEMORY, "archived", "system") in events


def test_nightly_moves_project_notes_on_a_merge_and_archives_closed_ones(world):
    db = world.db
    db.tables["projects"].extend([
        {"id": PROJECT, "abandoned_at": None, "failed_to_bid_at": None},
        {"id": PROJECT2, "abandoned_at": None, "failed_to_bid_at": None},
        {"id": P_ABANDONED, "abandoned_at": NOW.isoformat(), "failed_to_bid_at": None},
        {"id": P_LOST, "abandoned_at": None, "failed_to_bid_at": None},
    ])
    db.tables["bid_outcomes"].append({"project_id": P_LOST, "result": "lost"})
    db.tables["deleted_projects"].extend([
        {"project_id": P_MERGED, "reason": "merged", "merged_into_project_id": PROJECT2},
        {"project_id": P_GONE, "reason": "duplicate", "merged_into_project_id": None},
    ])
    note = dict(kind="project_note")
    db.tables["assistant_memories"].extend([
        memory_row(id=MEMORY, project_id=PROJECT, title="Open", **note),
        memory_row(id=MEMORY2, project_id=P_MERGED, title="Merged", **note),
        memory_row(id=MEMORY3, project_id=P_GONE, title="Gone", **note),
        memory_row(id=MEMORY4, project_id=P_ABANDONED, title="Abandoned", **note),
        memory_row(id=MEMORY5, project_id=P_LOST, title="Lost", **note),
    ])
    result = maint.run_nightly(NIGHT, worker="w1")
    rows = {m["id"]: m for m in db.tables["assistant_memories"]}
    assert rows[MEMORY]["status"] == "active" and rows[MEMORY]["project_id"] == PROJECT
    assert rows[MEMORY2]["status"] == "active" and rows[MEMORY2]["project_id"] == PROJECT2
    assert [rows[m]["status"] for m in (MEMORY3, MEMORY4, MEMORY5)] == ["archived"] * 3
    assert result["notes_moved"] == 1 and result["notes_archived"] == 3
    assert (MEMORY2, "moved") in [(e["memory_id"], e["event"]) for e in db.tables["assistant_memory_events"]]


def test_nightly_reads_past_a_full_page_of_healthy_project_notes(world, monkeypatch):
    """OFB-5: notes on healthy projects never leave the live set, so the
    review pages past them instead of rereading the same oldest page."""
    monkeypatch.setattr(maint, "NIGHTLY_LIMIT", 3)
    db = world.db
    db.tables["projects"].append({"id": PROJECT, "abandoned_at": None, "failed_to_bid_at": None})
    note = dict(kind="project_note", project_id=PROJECT)
    for i in range(6):
        db.tables["assistant_memories"].append(memory_row(
            id=f"cdcdcdcd-cdcd-4dcd-8dcd-00000000000{i}", title=f"Open {i}",
            created_at=f"2026-01-0{i + 1}T00:00:00+00:00", **note))
    db.tables["assistant_memories"].append(memory_row(
        id=MEMORY6, project_id=P_GONE, kind="project_note", title="Gone late",
        created_at="2026-09-01T00:00:00+00:00"))
    result = maint.run_nightly(NIGHT, worker="w1")
    rows = {m["id"]: m for m in db.tables["assistant_memories"]}
    assert rows[MEMORY6]["status"] == "archived" and result["notes_archived"] == 1


def test_a_failed_sweep_claim_is_retried_on_the_next_poll(world, monkeypatch):
    """OFB-8: an error from claim_sweep does not burn the day's attempt."""
    real = repo.claim_sweep
    state = {"fail": True}

    def flaky(user_id, step, day, *, worker):
        if state["fail"]:
            raise RuntimeError("supabase 503")
        return real(user_id, step, day, worker=worker)

    monkeypatch.setattr(repo, "claim_sweep", flaky)
    with pytest.raises(RuntimeError):
        maint.run_nightly(NIGHT, worker="w1")
    with pytest.raises(RuntimeError):
        maint.run_orphans(NIGHT, worker="w1")
    state["fail"] = False
    assert maint.run_nightly(NIGHT, worker="w1") is not None
    assert maint.run_orphans(NIGHT, worker="w1") is not None
    # A settled claim (True or False) still means one attempt per day.
    calls = len(world.db.calls)
    assert maint.run_nightly(NIGHT, worker="w1") is None and len(world.db.calls) == calls


# ── orphans ──────────────────────────────────────────────────────────────────


def test_orphans_offboard_owners_who_left_once_per_day(world, monkeypatch):
    gone = "45454545-4545-4545-8545-454545454545"
    world.db.tables["profiles"][1]["is_active"] = False
    world.db.tables["assistant_schedules"].extend([
        schedule(), schedule(id="ffffffff-ffff-4fff-8fff-fffffffffff9", user_id=OTHER_USER)])
    world.db.tables["llm_jobs"].append({"id": "j1", "job_type": "assistant_turn", "status": "queued",
                                        "payload": {"user_id": gone}})
    calls: list[tuple] = []
    monkeypatch.setattr(assistant_offboarding, "offboard", lambda user_id, mode, *, actor_id: calls.append(
        (user_id, mode, actor_id)) or assistant_offboarding.OffboardResult(ok=True))
    result = maint.run_orphans(DAY, worker="w1")
    assert sorted(calls) == sorted([(OTHER_USER, "sweep", None), (gone, "sweep", None)])
    assert result == {"checked": 3, "offboarded": 2, "failed": 0}
    monkeypatch.setattr(maint, "_claimed_dates", {})
    assert maint.run_orphans(DAY, worker="w2") is None
    assert len([s for s in world.db.tables["assistant_sweeps"] if s["step"] == "orphans"]) == 1


def _phase7_state(db: FakeDB, user_id: str) -> None:
    """One row of each phase 7 table the orphan read covers."""
    db.tables["assistant_subscriptions"] = [subscription_row(user_id=user_id)]
    db.tables["assistant_project_mutes"] = [{"user_id": user_id, "project_id": PROJECT, "created_at": NOW.isoformat()}]
    db.tables["assistant_inbox"] = [inbox_row(user_id=user_id)]
    db.tables["assistant_external_sends"] = [send_row(user_id=user_id)]


def test_phase7_owners_with_open_state_reads_each_table_and_respects_the_scope(world):
    db = world.db
    assert repo.phase7_owners_with_open_state(None) == []
    db.tables["assistant_subscriptions"] = [
        subscription_row(user_id=OTHER_USER),                                  # a person override
        subscription_row(user_id=None, role="executive", source="admin")]      # a role default: not owned
    assert repo.phase7_owners_with_open_state(None) == [OTHER_USER]
    db.tables["assistant_subscriptions"] = []
    db.tables["assistant_project_mutes"] = [{"user_id": USER, "project_id": PROJECT, "created_at": NOW.isoformat()}]
    assert repo.phase7_owners_with_open_state(None) == [USER]
    db.tables["assistant_project_mutes"] = []
    db.tables["assistant_inbox"] = [
        inbox_row(user_id=OTHER_USER, delivery="digest"),
        inbox_row(id="b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3", dedupe_key="p7:a", user_id=USER,
                  delivered_at=NOW.isoformat()),
        inbox_row(id="b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4", dedupe_key="p7:b", user_id=USER,
                  dismissed_at=NOW.isoformat()),
        inbox_row(id="b5b5b5b5-b5b5-4b5b-8b5b-b5b5b5b5b5b5", dedupe_key="p7:c", user_id=USER, delivery="bell")]
    assert repo.phase7_owners_with_open_state(None) == [OTHER_USER]
    db.tables["assistant_inbox"] = []
    db.tables["assistant_external_sends"] = [send_row(user_id=USER), send_row(
        id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e52", user_id=OTHER_USER, status="sent")]
    assert repo.phase7_owners_with_open_state(None) == [USER]
    _phase7_state(db, USER)
    db.tables["assistant_subscriptions"].append(subscription_row(user_id=OTHER_USER))
    assert repo.phase7_owners_with_open_state(None) == sorted([USER, OTHER_USER])
    assert repo.phase7_owners_with_open_state(OTHER_USER) == [OTHER_USER]
    assert repo.phase7_owners_with_open_state("44444444-4444-4444-8444-444444444444") == []
    assert repo.phase7_owners_with_open_state(None, limit=1) == [min(USER, OTHER_USER)]


def test_orphans_offboard_an_inactive_person_whose_only_state_is_phase7(world):
    db = world.db
    db.tables["profiles"][1]["is_active"] = False
    db.tables["assistant_subscriptions"] = [subscription_row(user_id=OTHER_USER)]
    db.tables["assistant_project_mutes"] = [{"user_id": OTHER_USER, "project_id": PROJECT,
                                             "created_at": NOW.isoformat()}]
    assert repo.owners_with_open_state(None) == [USER]  # the active person's delegation, not OTHER_USER
    result = maint.run_orphans(DAY, worker="w1")
    assert result == {"checked": 2, "offboarded": 1, "failed": 0}
    assert db.tables["assistant_subscriptions"] == [] and db.tables["assistant_project_mutes"] == []
    [(actor, action, entity, entity_id, details)] = world.audits
    assert (actor, action, entity, entity_id) == (None, "assistant.offboarded", "profile", OTHER_USER)
    assert details["mode"] == "sweep" and details["subscriptions_deleted"] == 1


def test_orphans_offboard_a_person_with_only_an_undelivered_inbox_row_or_pending_send(world):
    db = world.db
    db.tables["profiles"][1]["is_active"] = False
    db.tables["assistant_inbox"] = [inbox_row(user_id=OTHER_USER)]
    db.tables["assistant_external_sends"] = [send_row(user_id=OTHER_USER)]
    result = maint.run_orphans(DAY, worker="w1")
    assert result == {"checked": 2, "offboarded": 1, "failed": 0}
    assert db.tables["assistant_inbox"][0]["dismissed_reason"] == "owner_offboarded"
    assert db.tables["assistant_external_sends"][0]["status"] == "skipped"
    assert [a[1] for a in world.audits] == ["assistant.offboarded"]


def test_orphans_offboard_a_person_whose_only_state_is_browser_v2(world, monkeypatch):
    """A saved login (or any browser v2 state) of someone deactivated outside
    app/routers/users.py is found by the daily sweep."""
    db = world.db
    db.tables["profiles"][1]["is_active"] = False
    db.tables["assistant_credentials"] = [{"id": "c1", "user_id": OTHER_USER, "status": "active"}]
    calls: list[tuple] = []
    monkeypatch.setattr(assistant_offboarding, "offboard", lambda user_id, mode, *, actor_id: calls.append(
        (user_id, mode)) or assistant_offboarding.OffboardResult(ok=True))
    result = maint.run_orphans(DAY, worker="w1")
    assert calls == [(OTHER_USER, "sweep")]
    assert result == {"checked": 2, "offboarded": 1, "failed": 0}


def test_orphans_go_on_when_the_browser_v2_owner_read_fails(world, monkeypatch):
    world.db.tables["profiles"][1]["is_active"] = False
    world.db.tables["assistant_schedules"].append(schedule(user_id=OTHER_USER))
    monkeypatch.setattr(repo, "browser_v2_owners_with_open_state",
                        lambda user_id, *, limit: (_ for _ in ()).throw(RuntimeError("supabase 503")))
    calls: list[tuple] = []
    monkeypatch.setattr(assistant_offboarding, "offboard", lambda user_id, mode, *, actor_id: calls.append(
        (user_id, mode)) or assistant_offboarding.OffboardResult(ok=True))
    assert maint.run_orphans(DAY, worker="w1") == {"checked": 2, "offboarded": 1, "failed": 0}
    assert calls == [(OTHER_USER, "sweep")]


def test_orphans_leave_an_active_person_with_phase7_state_alone(world, monkeypatch):
    _phase7_state(world.db, OTHER_USER)
    calls: list[tuple] = []
    monkeypatch.setattr(assistant_offboarding, "offboard", lambda *a, **kw: calls.append(a))
    result = maint.run_orphans(DAY, worker="w1")
    assert result == {"checked": 2, "offboarded": 0, "failed": 0} and calls == []
    assert len(world.db.tables["assistant_subscriptions"]) == 1 and len(world.db.tables["assistant_project_mutes"]) == 1


def test_orphans_go_on_when_the_phase7_owner_read_fails(world, monkeypatch):
    world.db.tables["profiles"][1]["is_active"] = False
    world.db.tables["assistant_schedules"].append(schedule(user_id=OTHER_USER))

    def broken(user_id, *, limit):
        raise RuntimeError("supabase 503")

    monkeypatch.setattr(repo, "phase7_owners_with_open_state", broken)
    calls: list[tuple] = []
    monkeypatch.setattr(assistant_offboarding, "offboard", lambda user_id, mode, *, actor_id: calls.append(
        (user_id, mode)) or assistant_offboarding.OffboardResult(ok=True))
    result = maint.run_orphans(DAY, worker="w1")
    assert calls == [(OTHER_USER, "sweep")]
    assert result == {"checked": 2, "offboarded": 1, "failed": 0}
    assert len([s for s in world.db.tables["assistant_sweeps"] if s["step"] == "orphans"]) == 1


def test_every_system_sweep_is_bounded(world, monkeypatch):
    seen: dict[str, dict] = {}

    def recorder(name):
        def fn(user_id, *a, **kw):
            assert user_id is None
            seen[name] = kw
            return [] if name != "claim_sweep" else True
        return fn

    for name in ("pending_approvals_due", "pending_approvals_with_project", "approved_stranded",
                 "tool_results_to_purge", "memories_to_auto_archive", "memories_to_purge",
                 "project_notes_for_review", "owners_with_open_state", "phase7_owners_with_open_state",
                 "browser_v2_owners_with_open_state", "claim_sweep"):
        monkeypatch.setattr(repo, name, recorder(name))
    maint.run_approvals_sweep(NOW)
    maint.run_nightly(NIGHT, worker="w1")
    maint.run_orphans(NIGHT, worker="w1")
    for name, kw in seen.items():
        if name != "claim_sweep":
            assert 0 < kw["limit"] <= 500, name
    assert seen["claim_sweep"] == {"worker": "w1"}
    # The approvals sweep expires timed-out cards, so it reads with no expires_at bound.
    assert "expires_after" not in seen["pending_approvals_due"]


# ── plans_notes_sweep (phase 4, 3.3) ─────────────────────────────────────────


def _run_row(i: int, **over) -> dict:
    row = {"id": f"00000000-0000-4000-8000-{i:012d}", "finished_at": _ago(hours=i + 1), "status": "done"}
    row.update(over)
    return row


@pytest.fixture
def notes(world, monkeypatch):
    """Fake notes service: every run handed over is "behind"; sync_run writes
    `written` rows unless the run is in `quiet`."""
    state = {"seen": [], "synced": [], "quiet": set(), "fail": set(), "written": 3}

    def notes_behind(sb, run_ids):
        state["seen"].append(list(run_ids))
        return list(run_ids)

    def sync_run(sb, run_id, *, max_rows=5000):
        assert max_rows is None, "the sweep finishes what the lazy repair left"
        state["synced"].append(run_id)
        if run_id in state["fail"]:
            raise RuntimeError("storage down")
        written = 0 if run_id in state["quiet"] else state["written"]
        return {"run_id": run_id, "written": written, "removed": 0, "versions": {}, "partial": False}

    monkeypatch.setattr(plans_reader_notes, "notes_behind", notes_behind)
    monkeypatch.setattr(plans_reader_notes, "sync_run", sync_run)
    return state


def test_the_notes_sweep_reads_recent_finished_runs_and_syncs_at_most_ten(world, notes):
    rows = [_run_row(i) for i in range(120)]
    rows.append(_run_row(500, finished_at=_ago(days=20)))       # too old
    rows.append(_run_row(501, finished_at=None, status="reading"))  # not finished
    world.db.tables["plans_reader_runs"].extend(rows)
    result = maint.run_plans_notes_sweep()
    [seen] = notes["seen"]
    assert len(seen) == maint.PLANS_NOTES_SCAN_LIMIT == 100
    assert seen == [r["id"] for r in rows[:100]]                  # newest first
    assert _run_row(500)["id"] not in seen and _run_row(501)["id"] not in seen
    assert notes["synced"] == seen[:10]
    assert result == {"checked": 100, "behind": 100, "synced": 10, "written": 30, "removed": 0, "failed": 0}


def test_the_notes_sweep_claims_nothing_and_a_second_worker_is_idempotent(world, notes):
    world.db.tables["plans_reader_runs"].extend(_run_row(i) for i in range(3))
    first = maint.run_plans_notes_sweep()
    second = maint.run_plans_notes_sweep()
    assert first["synced"] == second["synced"] == 3
    assert world.db.tables["assistant_sweeps"] == []


def test_a_run_that_wrote_nothing_sits_out_six_hours(world, notes, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(maint.time, "monotonic", lambda: clock["t"])
    world.db.tables["plans_reader_runs"].extend(_run_row(i) for i in range(3))
    notes["quiet"] = {_run_row(0)["id"]}
    maint.run_plans_notes_sweep()
    notes["seen"].clear(), notes["synced"].clear()
    clock["t"] += 600
    maint.run_plans_notes_sweep()
    assert _run_row(0)["id"] not in notes["seen"][0] and len(notes["seen"][0]) == 2
    clock["t"] += maint.PLANS_NOTES_IDLE_SECONDS
    notes["seen"].clear()
    maint.run_plans_notes_sweep()
    assert _run_row(0)["id"] in notes["seen"][0]


def test_one_failing_sync_never_stops_the_others(world, notes):
    world.db.tables["plans_reader_runs"].extend(_run_row(i) for i in range(3))
    notes["fail"] = {_run_row(1)["id"]}
    result = maint.run_plans_notes_sweep()
    assert result["failed"] == 1 and result["synced"] == 2 and len(notes["synced"]) == 3


# ── ae_task_sweep (phase 4, 3.5.4 and 3.6.3) ─────────────────────────────────

CHAT_MSG = "98989898-9898-4898-8898-989898989891"
AE_RUN = "14141414-1414-4414-8414-141414141411"
CONV_ID = "44444444-4444-4444-8444-444444444444"


def _ae_task(world, n: int = 0, *, kind="plans_read_pages", user=USER, hold_kind="plans_pages", **over):
    tid = f"bbbbbbbb-0000-4000-8000-{n:012d}"
    hid = f"eeeeeeee-0000-4000-8000-{n:012d}"
    payload = ({"plans_message_id": CHAT_MSG, "pages": [1, 2]} if kind == "plans_read_pages"
               else {"phase": "started", "run_id": AE_RUN})
    row = task(id=tid, user_id=user, kind=kind, status="running", split_job_id=None, payload=payload,
               title="AI Estimating run for 26.10.7212", spend_reservation_id=hid, approval_id=None,
               run_id=AE_RUN if kind == "ai_estimating_run" else None, conversation_id=CONV_ID,
               created_at=_ago(minutes=60 - n % 50), updated_at=_ago(minutes=60 - n % 50))
    row.update(over)
    world.db.tables["assistant_tasks"].append(row)
    world.db.tables["assistant_spend_reservations"].append(
        reservation(id=hid, user_id=user, kind=hold_kind, task_id=tid, approval_id=None, amount_usd=2.0))
    return row


@pytest.fixture
def ae(world, monkeypatch):
    world.db.tables["assistant_conversations"].append(
        {"id": CONV_ID, "user_id": USER, "title": None, "surface": "panel", "mode": "normal", "project_id": None,
         "tainted_at": None, "mailbox_tainted_at": None, "last_message_at": None, "archived_at": None,
         "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()})
    state = {
        "chat": {"status": "running", "content": "", "cost_usd": "0.00", "error": None},
        "run": {"status": "reading", "cost_usd": "0.00", "finished_at": None, "error": None},
        "canceled": [], "cancel_error": None,
    }
    monkeypatch.setattr(assistant_ai_estimate, "chat_message_state",
                        lambda sb, message_id, *, viewer_role: dict(state["chat"]))
    monkeypatch.setattr(assistant_ai_estimate, "run_state", lambda sb, run_id: dict(state["run"]))

    def cancel(run_id, user_id=None):
        if state["cancel_error"]:
            raise state["cancel_error"]
        state["canceled"].append(run_id)
        state["run"] = {**state["run"], "status": "canceled", "cost_usd": "0.45"}
        return True

    monkeypatch.setattr(assistant_offboarding, "cancel_ae_run", cancel)
    return state


def _row(world, tid):
    return next(r for r in world.db.tables["assistant_tasks"] if r["id"] == tid)


def _hold_of(world, tid):
    return next(r for r in world.db.tables["assistant_spend_reservations"] if r["task_id"] == tid)


def test_the_task_sweep_advances_open_ae_tasks_through_the_real_finalize(world, ae):
    page = _ae_task(world, 1)
    ae["chat"] = {"status": "done", "content": "It is a 400A panel.", "cost_usd": "0.60", "error": None}
    result = maint.run_ae_task_sweep()
    assert result == {"checked": 1, "advanced": 1, "stranded": 0, "skipped_owner": 0, "failed": 0}
    assert _row(world, page["id"])["status"] == "done"
    assert _hold_of(world, page["id"])["status"] == "settled"
    [msg] = world.db.tables["assistant_messages"]
    assert msg["server_data"] is True and "It is a 400A panel." in msg["content"]
    # A second worker's sweep finds nothing open: no second message, no second bell.
    assert maint.run_ae_task_sweep()["checked"] == 0
    assert len(world.db.tables["assistant_messages"]) == 1 and len(world.bells) == 1


def test_the_task_sweep_is_bounded_at_twenty_and_skips_owners_with_no_live_delegation(world, ae, monkeypatch):
    for n in range(25):
        _ae_task(world, n)
    ghost = _ae_task(world, 90, user=OTHER_USER)      # active profile, no delegation
    handled: list[str] = []
    monkeypatch.setattr(plans_tasks, "advance", lambda user_id, t: handled.append(t["id"]) or False)
    result = maint.run_ae_task_sweep()
    assert result["checked"] == maint.AE_TASK_ADVANCE_LIMIT == 20 and len(handled) == 20
    assert ghost["id"] not in handled
    assert result["skipped_owner"] <= 1
    # Alone, the ghost's task is skipped with no message and no bell.
    world.db.tables["assistant_tasks"][:] = [ghost]
    handled.clear()
    assert maint.run_ae_task_sweep() == {"checked": 0, "advanced": 0, "stranded": 0, "skipped_owner": 1, "failed": 0}
    assert handled == [] and world.db.tables["assistant_messages"] == [] and world.bells == []


def test_one_failing_task_never_stops_the_sweep(world, ae, monkeypatch):
    a, b = _ae_task(world, 1), _ae_task(world, 2)

    def advance(user_id, t):
        if t["id"] == a["id"]:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(plans_tasks, "advance", advance)
    result = maint.run_ae_task_sweep()
    assert result["failed"] == 1 and result["advanced"] == 1 and result["checked"] == 2
    assert b["id"]


def _strand(world, phase="created", minutes=15, **over):
    fields = {"payload": {"phase": phase, "run_id": AE_RUN}, "created_at": _ago(minutes=minutes + 5),
              "updated_at": _ago(minutes=minutes), **over}
    return _ae_task(world, 7, kind="ai_estimating_run", hold_kind="ai_estimate", **fields)


def test_a_composite_route_stranded_short_of_started_is_cleaned_up(world, ae):
    t = _strand(world)
    result = maint.run_ae_task_sweep()
    assert result["stranded"] == 1 and result["advanced"] == 0
    assert ae["canceled"] == [AE_RUN]
    row = _row(world, t["id"])
    assert (row["status"], row["failure_reason"]) == ("failed", plans_tasks.FAILED_ROUTE)
    hold = _hold_of(world, t["id"])
    assert hold["status"] == "released" and Decimal(str(hold["actual_usd"])) == Decimal("0.45")
    [msg] = world.db.tables["assistant_messages"]
    assert msg["content"] == "The AI Estimating run for 26.10.7212 did not finish starting and was stopped."
    assert [b["type"] for b in world.bells] == [plans_tasks.BELL_FAILED]
    # Idempotent: the next sweep (or the other worker) finds the task closed.
    assert maint.run_ae_task_sweep()["checked"] == 0 and ae["canceled"] == [AE_RUN]


def test_a_stranded_route_with_no_run_yet_releases_the_hold_at_zero(world, ae):
    t = _strand(world, phase="reserved", run_id=None, payload={"phase": "reserved"})
    maint.run_ae_task_sweep()
    assert ae["canceled"] == []
    assert _row(world, t["id"])["failure_reason"] == plans_tasks.FAILED_ROUTE
    assert Decimal(str(_hold_of(world, t["id"])["actual_usd"])) == Decimal("0")


def test_a_route_under_ten_minutes_old_and_a_started_run_are_not_stranded(world, ae):
    young = _strand(world, minutes=5)
    started = _ae_task(world, 8, kind="ai_estimating_run", hold_kind="ai_estimate",
                       created_at=_ago(hours=2), updated_at=_ago(hours=2))
    result = maint.run_ae_task_sweep()
    assert result["stranded"] == 0 and ae["canceled"] == []
    assert _row(world, young["id"])["status"] == "running" and _row(world, started["id"])["status"] == "running"


def test_a_stranded_task_whose_run_already_ended_is_finalized_not_failed(world, ae):
    t = _strand(world)
    ae["run"] = {"status": "done", "cost_usd": "5.00", "finished_at": NOW.isoformat(), "error": None}
    result = maint.run_ae_task_sweep()
    assert result["stranded"] == 1 and ae["canceled"] == []
    assert _row(world, t["id"])["status"] == "done" and _hold_of(world, t["id"])["status"] == "settled"


def test_a_cancel_that_fails_leaves_the_task_and_hold_for_the_next_sweep(world, ae):
    t = _strand(world)
    ae["cancel_error"] = RuntimeError("supabase 503")
    result = maint.run_ae_task_sweep()
    assert result["failed"] == 1 and result["stranded"] == 0
    assert _row(world, t["id"])["status"] == "running" and _hold_of(world, t["id"])["status"] == "active"
    ae["cancel_error"] = None
    assert maint.run_ae_task_sweep()["stranded"] == 1
    assert _row(world, t["id"])["status"] == "failed"


# ── report_purge (phase 4, 3.9.3) ────────────────────────────────────────────

THIRD = "56565656-5656-4656-8656-565656565656"
R1, R2, R3, R4, R5 = (f"12121212-1212-4212-8212-12121212121{i}" for i in range(1, 6))


class Bucket:
    """The project-files bucket slice the purge rider touches: delete_file
    (which can fail silently, like storage's remove) and list."""

    def __init__(self):
        self.objects: dict[str, str] = {}
        self.silent_failure = False
        self.fail_list = False
        self.deleted: list[str] = []

    def put(self, user_id: str, name: str, created: str | None = None) -> str:
        path = f"assistant-reports/{user_id}/{name}"
        self.objects[path] = created or (DAY - timedelta(days=3)).isoformat()
        return path

    def delete_file(self, path):
        self.deleted.append(path)
        if not self.silent_failure:
            self.objects.pop(path, None)

    def list(self, prefix, opts=None):
        if self.fail_list:
            raise RuntimeError("storage list failed")
        if prefix == "assistant-reports":
            return [{"name": n} for n in sorted({p.split("/")[1] for p in self.objects})]
        return [{"name": p.rsplit("/", 1)[1], "created_at": c} for p, c in sorted(self.objects.items())
                if p.rsplit("/", 1)[0] == prefix]


@pytest.fixture
def bucket(world, monkeypatch):
    b = Bucket()
    world.db.storage = SimpleNamespace(from_=lambda name: b)
    monkeypatch.setattr(storage, "delete_file", b.delete_file)
    world.db.tables["assistant_profiles"].append({"user_id": THIRD, "legal_hold": True})
    world.db.tables["profiles"].append(profile(id=THIRD, full_name="Held Person", email="held@g3.test"))
    return b


def _expired_report(world, bucket, rid, owner=USER, **over):
    row = report(id=rid, user_id=owner, expires_at=_ago(days=1), **over)
    world.db.tables["assistant_reports"].append(row)
    bucket.put(owner, f"{rid}.xlsx")
    return row


def _report_row(world, rid):
    return next(r for r in world.db.tables["assistant_reports"] if r["id"] == rid)


def test_expired_reports_lose_their_object_and_are_marked_purged_once_it_is_gone(world, bucket):
    _expired_report(world, bucket, R1)
    _expired_report(world, bucket, R2, owner=OTHER_USER)
    # Report expiry queries use the wall clock, as does _expired_report's _ago.
    fresh = report(id=R3, user_id=USER, expires_at=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat())
    world.db.tables["assistant_reports"].append(fresh)
    bucket.put(USER, f"{R3}.xlsx")
    held = _expired_report(world, bucket, R4, owner=THIRD)
    result = maint.run_report_purge(DAY, worker="w1")
    assert result["purged"] == 2 and result["left"] == 0 and result["orphans_removed"] == 0
    for rid in (R1, R2):
        row = _report_row(world, rid)
        assert row["status"] == "purged" and row["purged_at"]
    assert f"assistant-reports/{USER}/{R1}.xlsx" not in bucket.objects
    assert f"assistant-reports/{OTHER_USER}/{R2}.xlsx" not in bucket.objects
    # An unexpired report and a legal-hold owner's expired one are untouched.
    assert _report_row(world, R3)["status"] == "ready" and f"assistant-reports/{USER}/{R3}.xlsx" in bucket.objects
    assert _report_row(world, held["id"])["status"] == "ready"
    assert f"assistant-reports/{THIRD}/{R4}.xlsx" in bucket.objects


def test_report_purge_is_claimed_once_per_day_across_two_workers(world, bucket, monkeypatch):
    first = maint.run_report_purge(DAY, worker="w1")
    assert first is not None
    monkeypatch.setattr(maint, "_claimed_dates", {})        # the other worker is another process
    assert maint.run_report_purge(DAY, worker="w2") is None
    sweeps = [s for s in world.db.tables["assistant_sweeps"] if s["step"] == "report_purge"]
    assert len(sweeps) == 1 and sweeps[0]["claimed_by"] == "w1" and sweeps[0]["sweep_date"] == "2026-10-23"
    calls = len(world.db.calls)
    assert maint.run_report_purge(DAY, worker="w1") is None and len(world.db.calls) == calls
    # Tomorrow it runs again.
    assert maint.run_report_purge(DAY + timedelta(days=1), worker="w1") is not None


def test_an_object_whose_delete_silently_failed_keeps_its_row_ready_and_is_retried_tomorrow(world, bucket):
    _expired_report(world, bucket, R1)
    bucket.silent_failure = True
    result = maint.run_report_purge(DAY, worker="w1")
    assert result["purged"] == 0 and result["left"] == 1
    assert _report_row(world, R1)["status"] == "ready" and _report_row(world, R1)["purged_at"] is None
    bucket.silent_failure = False
    result = maint.run_report_purge(DAY + timedelta(days=1), worker="w1")
    assert result["purged"] == 1 and _report_row(world, R1)["status"] == "purged"


def test_a_failed_listing_never_marks_a_row_purged(world, bucket):
    _expired_report(world, bucket, R1)
    bucket.fail_list = True
    result = maint.run_report_purge(DAY, worker="w1")
    assert result["purged"] == 0 and result["left"] == 1 and result["orphans_removed"] is None
    assert _report_row(world, R1)["status"] == "ready"


def test_the_orphan_pass_removes_old_unclaimed_report_objects_only(world, bucket):
    keep = report(id=R1, user_id=USER, expires_at=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat())
    world.db.tables["assistant_reports"].append(keep)
    bucket.put(USER, f"{R1}.xlsx")                                                    # row-backed
    old_orphan = bucket.put(USER, f"{R2}.xlsx")                                       # row gone, 3 days old
    bucket.put(USER, f"{R3}.xlsx", created=(DAY - timedelta(hours=2)).isoformat())    # maybe mid-upload
    bucket.put(USER, "notes.txt")                                                     # not a report name
    bucket.put(THIRD, f"{R5}.xlsx")                                                   # legal hold owner
    result = maint.run_report_purge(DAY, worker="w1")
    assert result["orphans_removed"] == 1
    assert old_orphan not in bucket.objects
    for name in (R1, R3):
        assert f"assistant-reports/{USER}/{name}.xlsx" in bucket.objects
    assert f"assistant-reports/{USER}/notes.txt" in bucket.objects
    assert f"assistant-reports/{THIRD}/{R5}.xlsx" in bucket.objects


def test_the_orphan_pass_rotates_through_at_most_fifty_owner_prefixes(world, bucket):
    for i in range(60):
        bucket.put(f"00000000-0000-4000-8000-{i:012d}", f"{R2}.xlsx")
    first = maint.run_report_purge(DAY, worker="w1")
    assert first["orphans_removed"] == maint.ORPHAN_REPORT_FOLDERS == 50
    assert len(bucket.objects) == 10


def test_the_module_carries_no_em_dash():
    with open(maint.__file__, encoding="utf-8") as fh:
        assert EM_DASH not in fh.read()
