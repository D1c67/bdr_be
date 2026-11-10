"""Assistant offboarding (app/services/assistant_offboarding.py;
docs/ASSISTANT_PHASE3_PLAN.md 3.10), over the FakeDB with a fake queue.

Invariants pinned here:

  table      every mode runs exactly the steps of the 3.10.2 table:
             mfa_reset revokes, expires every open card owner_offboarded,
             cancels queued turns and execute jobs (never a continuation)
             and pauses active routines mfa_reset; role_changed expires only
             the cards the new role cannot use (role_changed) and stops
             work only when the new role is not a writer; deactivated,
             demoted_external and sweep do everything and pause every
             routine that could restart on its own as owner_offboarded;
             deleted does everything but routines, and purges staging;
  order      expire, stop tasks, cancel split runs, cancel jobs twice,
             release holds;
  never      a failing step is recorded and audited, never raised; ok is
             False only for a failure among steps 1 to 5;
  history    an MFA reset, a deactivation, a reactivation and a new grant
             leave the routine paused owner_offboarded through a resume
             pass; a row paused no_delegation in the revoke-before-write
             window ends up owner_offboarded;
  phase 4    (3.24) a page read with a queued chat job: job canceled, hold
             closed with its actual, task failed owner_changed, no message
             and no bell; an AI Estimating run in progress: run canceled,
             hold released with the actual (user_spent includes it); a run
             that already ended is finalized; a demoted Executive's evidence
             report is purged in role_changed and a legal hold person's
             reports are kept; _release_holds leaves the AE kinds to the hook.
  phase 7    (6.8) deactivated, demoted_external and sweep dismiss the
             undelivered inbox rows owner_offboarded, skip the pending ledger
             sends offboarded (claimed rows keep their own fence) and delete
             the person's own subscription overrides and project mutes;
             deleted (the cascade), mfa_reset and role_changed do nothing;
             a failing step is counted phase7_unfinished and the rest run.
  browser v2 (WI-18, plan 17.5) deactivated, demoted_external and sweep run the
             0192 revoke RPC a second time (after the jobs are canceled), the
             purge_v2 RPC, delete the pinned job credentials and the staged
             download objects (rows marked purged only after the object is
             gone; kept on legal hold), then read back that no grant,
             admission, request, hold, live session or safe rule is left;
             deleted purges only the staged objects (the cascade does the
             rest); mfa_reset and role_changed do nothing; every query is
             the person's own; the hook is idempotent, counts only, and a
             failing step is counted browser_v2_unfinished while the rest
             run; the 0192 revoke RPC text names every table it must clear.
  phase 8 (WI-13, plan 11) deactivated, demoted_external and sweep skip the
             person's pending and claimed triage items `offboarded` and fail
             their claimed runs; deleted relies on the cascade; mfa_reset and
             role_changed do nothing; the hook is registered last, is
             idempotent, repeats while a full page of 500 comes back, and a
             failing step is counted phase8_unfinished; offboard() raises
             for the company spend id before any step or hook runs (MC-1, F3).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from decimal import Decimal

from app.assistant import access, plans_tasks, spend
from app.core import features, supabase_client
from app.services import assistant_ai_estimate
from app.services import assistant_offboarding as off
from app.services import assistant_scheduler, assistant_staging, llm_queue, notifications
from tests.assistant_fakes import (
    APPROVAL_ID,
    HOLD,
    NOW,
    SCHEDULE,
    TASK,
    USER,
    FakeDB,
    INBOX,
    INBOX2,
    P7_OTHER,
    PROJECT,
    SEND,
    FakeStorage,
    approval,
    conversation,
    delegation,
    profile,
    report,
    reservation,
    schedule,
    send_row,
    settings_row,
    staged_file,
    subscription_row,
    task,
    inbox_row,
    triage_item_row,
    triage_run_row,
)

EM_DASH = chr(0x2014)
A2 = "99999999-9999-4999-8999-999999999992"
S_FEATURE, S_MFA, S_NODEL, S_USER, S_FAIL = (f"ffffffff-ffff-4fff-8fff-00000000000{i}" for i in range(1, 6))
SPLIT_JOB_A = "dddddddd-dddd-4ddd-8ddd-ddddddddddd9"
F_TASK = "efefefef-0000-4000-8000-000000000001"
F_LOOSE = "efefefef-0000-4000-8000-000000000002"
F_HUMAN = "efefefef-0000-4000-8000-000000000003"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    db = FakeDB(
        now=NOW, profiles=[profile()], assistant_delegations=[delegation()], assistant_settings=[settings_row()],
        assistant_profiles=[], assistant_conversations=[], assistant_messages=[], assistant_approvals=[],
        assistant_tasks=[], assistant_spend_reservations=[], llm_call_log=[], assistant_files=[],
        assistant_schedules=[], assistant_schedule_runs=[], bid_split_jobs=[], bid_split_files=[], llm_jobs=[],
        audit_log=[], assistant_reports=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    FakeStorage().install(monkeypatch)
    monkeypatch.setattr(features, "enabled_map", lambda role=None: {"bidding": True, "pm": True,
                                                                   "certified_payroll": True})
    bells: list[tuple] = []
    monkeypatch.setattr(notifications, "notify_user", lambda *a, **k: bells.append((a, k)))
    audits: list[dict] = []
    monkeypatch.setattr(notifications, "audit", lambda actor_id, action, entity=None, entity_id=None, payload=None:
                        audits.append({"actor_id": actor_id, "action": action, "entity_id": entity_id,
                                       "payload": payload}))
    canceled: list[str] = []

    def active_job(job_type, target_id):
        return next((j for j in db.tables["llm_jobs"] if j["job_type"] == job_type
                     and j.get("target_id") == target_id and j["status"] in ("queued", "running")), None)

    def cancel(job_id):
        job = next((j for j in db.tables["llm_jobs"] if j["id"] == job_id), None)
        if job is None or job["status"] != "queued":
            return None
        job["status"] = "canceled"
        canceled.append(job_id)
        return dict(job)

    monkeypatch.setattr(llm_queue, "active_job", active_job)
    monkeypatch.setattr(llm_queue, "cancel", cancel)
    # The 0192 purge is a system RPC the FakeDB does not model (browser v2 hook).
    db.bv2_rpc_results["assistant_browser_purge_v2"] = {"sessions": 0, "live_idle": 0, "frames": 0, "input": 0}
    return SimpleNamespace(db=db, audits=audits, canceled=canceled, bells=bells)


def _seed(world) -> None:
    """One of everything: a pending and an approved card, an open split task
    with its hold and a queued splitter run, a loose assistant splitter run,
    a human splitter run, queued and running assistant jobs, a staged file
    and routines in every state step 7 looks at."""
    db = world.db.tables
    db["assistant_approvals"].extend([
        approval(action_kind="set_labor_numbers", message_id=None),
        approval(id=A2, status="approved", action_kind="todo_create", message_id=None,
                 decided_at=NOW.isoformat()),
    ])
    db["assistant_tasks"].append(task(payload={"files": [{"split_file_id": F_TASK}]}))
    db["assistant_spend_reservations"].append(reservation())
    db["bid_split_jobs"].extend([
        {"id": SPLIT_JOB_A, "created_by": USER, "actor_kind": "assistant"},
        {"id": "human-job", "created_by": USER, "actor_kind": "human"},
    ])
    db["bid_split_files"].extend([
        {"id": F_TASK, "job_id": SPLIT_JOB_A, "assistant_run_id": "r1", "status": "pending"},
        {"id": F_LOOSE, "job_id": SPLIT_JOB_A, "assistant_run_id": "r1", "status": "pending"},
        {"id": F_HUMAN, "job_id": "human-job", "assistant_run_id": None, "status": "pending"},
    ])
    db["llm_jobs"].extend([
        {"id": "split-task", "job_type": "bid_split", "target_id": F_TASK, "status": "queued", "payload": {}},
        {"id": "split-loose", "job_type": "bid_split", "target_id": F_LOOSE, "status": "queued", "payload": {}},
        {"id": "split-human", "job_type": "bid_split", "target_id": F_HUMAN, "status": "queued", "payload": {}},
        {"id": "turn", "job_type": "assistant_turn", "status": "queued", "payload": {"user_id": USER}},
        {"id": "exec", "job_type": "assistant_approval_execute", "status": "queued", "payload": {"user_id": USER}},
        {"id": "cont", "job_type": "assistant_continue", "status": "queued", "payload": {"user_id": USER}},
        {"id": "other", "job_type": "assistant_turn", "status": "queued", "payload": {"user_id": "someone"}},
        {"id": "busy", "job_type": "assistant_turn", "status": "running", "payload": {"user_id": USER}},
    ])
    db["assistant_files"].append(staged_file())
    paused = {"paused_at": NOW.isoformat()}
    db["assistant_schedules"].extend([
        schedule(),
        schedule(id=S_FEATURE, status="paused_feature_off", **paused),
        schedule(id=S_MFA, status="paused", paused_reason="mfa_reset", **paused),
        schedule(id=S_NODEL, status="paused", paused_reason="no_delegation", **paused),
        schedule(id=S_USER, status="paused", paused_reason="user", **paused),
        schedule(id=S_FAIL, status="paused", paused_reason="failures", **paused),
    ])


def _card(world, approval_id=APPROVAL_ID):
    row = next(r for r in world.db.tables["assistant_approvals"] if r["id"] == approval_id)
    return row["status"], row.get("expired_reason")


def _schedules(world) -> dict:
    return {r["id"]: (r["status"], r["paused_reason"]) for r in world.db.tables["assistant_schedules"]}


def _job(world, job_id) -> str:
    return next(j for j in world.db.tables["llm_jobs"] if j["id"] == job_id)["status"]


def _revoked(world) -> bool:
    return world.db.tables["assistant_delegations"][0]["revoked_at"] is not None


# ── The mode table, row by row ───────────────────────────────────────────────


def test_mfa_reset(world):
    _seed(world)
    result = off.offboard(USER, "mfa_reset", actor_id="admin")
    assert result.ok and result.failures == []
    assert _revoked(world) and world.db.tables["assistant_delegations"][0]["revoked_reason"] == "mfa_reset"
    assert _card(world) == ("expired", "owner_offboarded") and _card(world, A2) == ("expired", "owner_offboarded")
    # Turns and execute jobs canceled; the continuation stops at its own owner check (D16).
    assert _job(world, "turn") == "canceled" and _job(world, "exec") == "canceled"
    assert _job(world, "cont") == "queued" and _job(world, "other") == "queued"
    # No task, splitter, hold or staging step.
    assert world.db.tables["assistant_tasks"][0]["status"] == "waiting_external"
    assert _job(world, "split-loose") == "queued"
    assert world.db.tables["assistant_spend_reservations"][0]["status"] == "active"
    assert world.db.tables["assistant_files"][0]["expires_at"] == staged_file()["expires_at"]
    sched = _schedules(world)
    assert sched[SCHEDULE] == ("paused", "mfa_reset")
    assert sched[S_FEATURE] == ("paused_feature_off", None) and sched[S_NODEL] == ("paused", "no_delegation")
    assert result.counts == {"delegations": 1, "cards": 2, "jobs": 2, "schedules": 1, "browser_credentials": 0}


@pytest.mark.parametrize("mode", ["deactivated", "demoted_external", "sweep"])
def test_leaving_modes(world, mode):
    _seed(world)
    result = off.offboard(USER, mode, actor_id=None if mode == "sweep" else "admin")
    assert result.ok, result.failures
    assert _revoked(world)
    assert _card(world) == ("expired", "owner_offboarded") and _card(world, A2) == ("expired", "owner_offboarded")
    t = world.db.tables["assistant_tasks"][0]
    assert (t["status"], t["failure_reason"]) == ("failed", "owner_changed")
    for job in ("split-task", "split-loose", "turn", "exec", "cont"):
        assert _job(world, job) == "canceled", job
    assert _job(world, "split-human") == "queued" and _job(world, "other") == "queued"
    assert world.db.tables["assistant_spend_reservations"][0]["status"] == "released"
    assert world.db.tables["assistant_files"][0]["expires_at"] != staged_file()["expires_at"]
    sched = _schedules(world)
    for sid in (SCHEDULE, S_FEATURE, S_MFA, S_NODEL):
        assert sched[sid] == ("paused", "owner_offboarded"), sid
    assert sched[S_USER] == ("paused", "user") and sched[S_FAIL] == ("paused", "failures")
    assert result.counts["schedules"] == 4 and result.counts["split_runs"] == 1
    # The running turn stops at its next principal check.
    assert result.still_running == 1


def test_deleted_does_everything_but_routines_and_purges_staging(world, monkeypatch):
    _seed(world)
    purges: list[dict] = []
    real = assistant_staging.purge_expired

    def purge(user_id, limit):
        purges.append({"user_id": user_id, "limit": limit})
        return real(user_id, limit)

    monkeypatch.setattr(assistant_staging, "purge_expired", purge)
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.ok and _revoked(world)
    assert _card(world) == ("expired", "owner_offboarded")
    assert world.db.tables["assistant_tasks"][0]["status"] == "failed"
    assert world.db.tables["assistant_spend_reservations"][0]["status"] == "released"
    assert all(s[0] != "paused" or s[1] != "owner_offboarded" for s in _schedules(world).values())
    assert _schedules(world)[SCHEDULE] == ("active", None)
    assert purges and all(p == {"user_id": USER, "limit": 200} for p in purges)
    staged = world.db.tables["assistant_files"][0]
    assert staged["purged_at"] is not None


def test_role_changed_to_a_writer_expires_only_the_cards_the_new_role_cannot_use(world):
    _seed(world)
    result = off.offboard(USER, "role_changed", actor_id="admin", old_role="executive", new_role="estimating_admin")
    assert result.ok
    assert not _revoked(world)
    assert _card(world) == ("pending", None) and _card(world, A2) == ("approved", None)
    assert world.db.tables["assistant_tasks"][0]["status"] == "waiting_external"
    assert _job(world, "turn") == "queued"
    assert world.db.tables["assistant_spend_reservations"][0]["status"] == "active"
    assert _schedules(world)[SCHEDULE] == ("active", None)
    assert "jobs" not in result.counts and "schedules" not in result.counts


def test_role_changed_to_a_reader_stops_work_and_expires_the_write_cards(world):
    _seed(world)
    result = off.offboard(USER, "role_changed", actor_id="admin", old_role="executive", new_role="accountant")
    assert result.ok and not _revoked(world)
    assert _card(world) == ("expired", "role_changed") and _card(world, A2) == ("expired", "role_changed")
    assert world.db.tables["assistant_tasks"][0]["status"] == "failed"
    assert _job(world, "split-loose") == "canceled"
    assert world.db.tables["assistant_spend_reservations"][0]["status"] == "released"
    # No queued-job step and no routine step for an internal role change.
    assert _job(world, "turn") == "queued" and _schedules(world)[SCHEDULE] == ("active", None)


# ── Order, failures, audit ───────────────────────────────────────────────────


def test_steps_run_in_order_and_jobs_are_canceled_twice(world, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(off, "revoke_delegation", lambda u, r: calls.append("revoke") or 1)
    monkeypatch.setattr(off, "_expire_cards", lambda u, m, r: calls.append("expire") or 0)
    monkeypatch.setattr(off, "_stop_tasks", lambda u: calls.append("tasks") or (0, set(), 0))
    monkeypatch.setattr(off, "_cancel_split_runs", lambda u, c: calls.append("split_runs") or (0, 0))
    monkeypatch.setattr(off, "_assistant_jobs", lambda u, t, s: calls.append(f"jobs:{s}") or [])
    monkeypatch.setattr(off, "_release_holds", lambda u: calls.append("holds") or 0)
    monkeypatch.setattr(off, "_pause_schedules", lambda u, r, widen: calls.append("schedules") or 0)
    monkeypatch.setattr(off, "_retire_staging", lambda u, purge: calls.append("staging") or 0)
    off.offboard(USER, "deactivated", actor_id="admin")
    assert calls == ["revoke", "expire", "tasks", "split_runs", "jobs:queued", "jobs:queued", "jobs:running",
                     "holds", "schedules", "staging"]


def test_a_failing_step_is_audited_and_never_raised(world, monkeypatch):
    _seed(world)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(off, "_expire_cards", boom)
    monkeypatch.setattr(off, "_retire_staging", boom)
    result = off.offboard(USER, "deactivated", actor_id="admin", old_role="executive")
    assert result.ok is False and result.failures == ["cards", "staging"]
    # Later steps still ran.
    assert _schedules(world)[SCHEDULE] == ("paused", "owner_offboarded")
    audit = world.audits[-1]
    assert audit["action"] == "assistant.offboarded" and audit["actor_id"] == "admin" and audit["entity_id"] == USER
    payload = audit["payload"]
    assert payload["mode"] == "deactivated" and payload["failures"] == ["cards", "staging"]
    assert payload["old_role"] == "executive" and "still_running" in payload
    assert "filename" not in str(payload) and "A-101.pdf" not in str(payload)


def test_a_failure_outside_the_critical_steps_keeps_ok(world, monkeypatch):
    monkeypatch.setattr(off, "_retire_staging", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.ok and result.failures == ["staging"]


def test_a_failed_cancel_in_step_3_fails_the_tasks_step(world, monkeypatch):
    """OFB-2: a transient error canceling a task's queued run is not
    swallowed: the task still stops, the step fails, a hard delete refuses."""
    _seed(world)
    real = llm_queue.active_job
    hits = {"n": 0}

    def flaky(job_type, target_id):
        if target_id == F_TASK and hits["n"] < 2:
            hits["n"] += 1
            raise RuntimeError("supabase blip")
        return real(job_type, target_id)

    monkeypatch.setattr(llm_queue, "active_job", flaky)
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.ok is False and "tasks" in result.failures
    assert world.db.tables["assistant_tasks"][0]["status"] == "failed"
    # Step 4 no longer skips the task's file and cancels it on its retry.
    assert _job(world, "split-task") == "canceled" and _job(world, "split-loose") == "canceled"


def test_a_covered_run_still_queued_fails_the_split_runs_step(world, monkeypatch):
    """OFB-2 backstop: step 4 re-checks the files step 3 covered."""
    from app.assistant import continue_job

    _seed(world)
    monkeypatch.setattr(continue_job, "cancel_queued_runs", lambda ids: 0)
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.ok is False and "split_runs" in result.failures and "tasks" not in result.failures


def test_a_hard_delete_audits_staged_objects_it_could_not_purge(world, monkeypatch):
    """OFB-6: a storage failure during the purge is in the audit; the step
    is not critical, so the delete still proceeds."""
    _seed(world)
    monkeypatch.setattr(assistant_staging, "_delete_object", lambda path: False)
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.ok and result.failures == ["staging"]
    assert result.counts["staging_unpurged"] == 1 and result.counts["files"] == 1
    payload = world.audits[-1]["payload"]
    assert payload["failures"] == ["staging"] and payload["staging_unpurged"] == 1


def test_a_revoke_before_the_profile_write_is_counted(world):
    """OFB-7: update_user revokes first, so step 1 finds nothing; the
    prior count reaches the audit."""
    assert off.revoke_delegation(USER, "deactivated") == 1
    result = off.offboard(USER, "deactivated", actor_id="admin", prior_revoked=1)
    assert result.counts["delegations"] == 1
    assert world.audits[-1]["payload"]["delegations"] == 1


def test_an_unknown_mode_does_nothing(world):
    _seed(world)
    result = off.offboard(USER, "vacation", actor_id="admin")
    assert result.ok is False and result.failures == ["mode"] and not _revoked(world)


def test_hooks_run_for_every_mode_and_add_their_counts(world, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(off, "HOOKS", [lambda u, m: seen.append((u, m)) or {"ms_tokens": 2}])
    for mode in off.MODES:
        result = off.offboard(USER, mode, actor_id="admin", new_role="executive")
        assert result.counts.get("ms_tokens") == 2
    assert [m for _, m in seen] == list(off.MODES)


# ── Phase 4: AI Estimating tasks and reports (3.24) ──────────────────────────

CHAT_MSG = "98989898-9898-4898-8898-989898989891"
AE_RUN = "14141414-1414-4414-8414-141414141411"
R_EVIDENCE = "12121212-1212-4212-8212-121212121211"
R_BIDS = "12121212-1212-4212-8212-121212121212"
R_LOOSE = "12121212-1212-4212-8212-121212121213"


@pytest.fixture
def ae(world, monkeypatch):
    """Fake AE state, a fake run cancel, the storage double for reports and
    a listing over it."""
    world.db.tables["assistant_conversations"].append(conversation())
    state = {
        "chat": {"status": "running", "content": "", "cost_usd": "0.70", "error": None},
        "run": {"status": "reading", "cost_usd": "1.10", "finished_at": None, "error": None},
        "canceled": [], "cancel_error": None,
    }
    monkeypatch.setattr(assistant_ai_estimate, "chat_message_state",
                        lambda sb, message_id, *, viewer_role: dict(state["chat"]))
    monkeypatch.setattr(assistant_ai_estimate, "run_state", lambda sb, run_id: dict(state["run"]))

    def cancel(run_id, user_id=None):
        if state["cancel_error"]:
            raise state["cancel_error"]
        state["canceled"].append(run_id)
        state["run"] = {**state["run"], "status": "canceled"}
        return True

    monkeypatch.setattr(off, "cancel_ae_run", cancel)
    store = FakeStorage().install(monkeypatch)

    def report_objects(user_id):
        prefix = f"assistant-reports/{user_id}/"
        return {p[len(prefix):]: None for p in store.objects if p.startswith(prefix)}

    monkeypatch.setattr(off, "report_objects", report_objects)
    state["store"] = store
    return state


def _page_task(world, **over):
    row = task(kind="plans_read_pages", status="running", split_job_id=None, run_id=None, approval_id=None,
               payload={"plans_message_id": CHAT_MSG, "pages": [3, 4]}, **over)
    world.db.tables["assistant_tasks"].append(row)
    world.db.tables["assistant_spend_reservations"].append(
        reservation(kind="plans_pages", task_id=row["id"], approval_id=None, amount_usd=2.13))
    return row


def _run_task(world, **over):
    row = task(kind="ai_estimating_run", status="waiting_external", split_job_id=None, approval_id=None,
               run_id=AE_RUN, title="AI Estimating run for 26.10.7212",
               payload={"phase": "started", "run_id": AE_RUN}, **over)
    world.db.tables["assistant_tasks"].append(row)
    world.db.tables["assistant_spend_reservations"].append(
        reservation(kind="ai_estimate", task_id=row["id"], approval_id=None, amount_usd=14.0))
    return row


def _t(world):
    return next(r for r in world.db.tables["assistant_tasks"] if r["id"] == TASK)


def _h(world):
    return next(r for r in world.db.tables["assistant_spend_reservations"] if r["id"] == HOLD)


def _put_report(world, ae, rid, spec, owner=USER, **over):
    row = report(id=rid, user_id=owner, spec=spec, **over)
    world.db.tables["assistant_reports"].append(row)
    ae["store"].objects[row["storage_path"]] = b"xlsx"
    return row


def _report(world, rid):
    return next(r for r in world.db.tables["assistant_reports"] if r["id"] == rid)


@pytest.mark.parametrize("mode", ["deactivated", "demoted_external", "sweep", "deleted"])
def test_a_page_read_with_a_queued_chat_job_is_stopped_and_its_hold_closed_with_its_actual(world, ae, mode):
    _page_task(world)
    world.db.tables["llm_jobs"].append({"id": "chat-job", "job_type": llm_queue.JOB_PLANS_READER_CHAT,
                                        "target_id": CHAT_MSG, "status": "queued", "payload": {}})
    before = spend.state(USER)
    result = off.offboard(USER, mode, actor_id="admin")
    assert result.ok and result.failures == []
    assert _job(world, "chat-job") == "canceled"
    row = _t(world)
    assert (row["status"], row["failure_reason"]) == ("failed", plans_tasks.FAILED_OWNER)
    assert _h(world)["status"] == "released" and Decimal(str(_h(world)["actual_usd"])) == Decimal("0.70")
    assert spend.state(USER).user_spent - before.user_spent == Decimal("0.70")
    assert result.counts["plans_tasks"] == 1 and result.counts["ae_holds"] == 1
    assert result.counts["ae_runs_canceled"] == 0
    # No message and no bell reach a person who left.
    assert world.db.tables["assistant_messages"] == [] and world.bells == []


def test_a_running_chat_job_is_never_canceled(world, ae):
    _page_task(world)
    world.db.tables["llm_jobs"].append({"id": "chat-job", "job_type": llm_queue.JOB_PLANS_READER_CHAT,
                                        "target_id": CHAT_MSG, "status": "running", "payload": {}})
    off.offboard(USER, "deactivated", actor_id="admin")
    assert _job(world, "chat-job") == "running" and _t(world)["status"] == "failed"


def test_an_ai_estimating_run_in_progress_is_canceled_and_its_hold_released_with_the_cost_so_far(world, ae):
    _run_task(world)
    before = spend.state(USER)
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert ae["canceled"] == [AE_RUN]
    row = _t(world)
    assert (row["status"], row["failure_reason"]) == ("failed", plans_tasks.FAILED_OWNER)
    assert _h(world)["status"] == "released" and Decimal(str(_h(world)["actual_usd"])) == Decimal("1.10")
    assert spend.state(USER).user_spent - before.user_spent == Decimal("1.10")
    assert result.counts["ae_runs_canceled"] == 1 and result.counts["ae_holds"] == 1
    assert result.counts["plans_tasks"] == 1
    assert world.db.tables["assistant_messages"] == [] and world.bells == []


def test_a_run_that_already_ended_is_finalized_as_usual(world, ae):
    _run_task(world)
    ae["run"] = {"status": "done", "cost_usd": "9.50", "finished_at": NOW.isoformat(), "error": None}
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert ae["canceled"] == []
    assert _t(world)["status"] == "done"
    assert _h(world)["status"] == "settled" and Decimal(str(_h(world)["actual_usd"])) == Decimal("9.50")
    assert result.counts["ae_runs_canceled"] == 0


def test_offboarding_the_ae_tasks_twice_is_idempotent(world, ae):
    _run_task(world)
    _page_task(world, id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2")
    first = off.offboard(USER, "sweep", actor_id=None)
    second = off.offboard(USER, "sweep", actor_id=None)
    assert first.counts["plans_tasks"] == 2
    assert second.counts["plans_tasks"] == 0 and second.counts["ae_runs_canceled"] == 0
    assert second.counts["ae_holds"] == 0 and ae["canceled"] == [AE_RUN]


def test_release_holds_leaves_the_ae_kinds_to_the_hook(world):
    world.db.tables["assistant_spend_reservations"].extend([
        reservation(),
        reservation(id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2", kind="plans_pages", task_id=None, approval_id=None),
        reservation(id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee3", kind="ai_estimate", task_id=None, approval_id=None),
    ])
    assert off._release_holds(USER) == 1
    status = {r["id"]: r["status"] for r in world.db.tables["assistant_spend_reservations"]}
    assert status[HOLD] == "released"
    assert status["eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2"] == "active"
    assert status["eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee3"] == "active"


def test_an_ae_hold_with_no_task_behind_it_is_released_at_zero_in_a_leaving_mode(world, ae):
    world.db.tables["assistant_spend_reservations"].append(
        reservation(kind="ai_estimate", task_id=None, approval_id=None))
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert _h(world)["status"] == "released" and Decimal(str(_h(world)["actual_usd"])) == Decimal("0")
    assert result.counts["ae_holds"] == 1


def test_a_demoted_executives_evidence_report_is_purged_in_role_changed_and_the_rest_kept(world, ae):
    _put_report(world, ae, R_EVIDENCE, "project_timeline")
    _put_report(world, ae, R_BIDS, "bid_invitations")
    world.db.tables["profiles"][0]["role"] = "estimating_admin"          # the write has happened
    result = off.offboard(USER, "role_changed", actor_id="admin", old_role="executive", new_role="estimating_admin")
    assert result.ok and result.counts["reports_purged"] == 1
    purged = _report(world, R_EVIDENCE)
    assert purged["status"] == "purged" and purged["purged_at"]
    assert f"assistant-reports/{USER}/{R_EVIDENCE}.xlsx" not in ae["store"].objects
    assert _report(world, R_BIDS)["status"] == "ready"
    assert f"assistant-reports/{USER}/{R_BIDS}.xlsx" in ae["store"].objects
    # Still a writer: nothing of the AE work stops.
    assert result.counts["plans_tasks"] == 0 and result.counts["ae_holds"] == 0


def test_a_workbook_with_actual_bid_dates_is_purged_when_its_owner_is_demoted_to_a_non_viewer(world, ae):
    # SEC-3: quote_readiness is INTERNAL_ROLES, but this one holds bid dates.
    _put_report(world, ae, R_EVIDENCE, "quote_readiness", params={"week": "2026-10-19", "bid_dates": True})
    _put_report(world, ae, R_BIDS, "bid_invitations", params={"range": "wtd"})
    world.db.tables["profiles"][0]["role"] = "estimating_engineer_labor"
    result = off.offboard(USER, "role_changed", actor_id="admin", old_role="estimating_admin",
                          new_role="estimating_engineer_labor")
    assert result.ok and result.counts["reports_purged"] == 1
    assert _report(world, R_EVIDENCE)["status"] == "purged"
    assert _report(world, R_BIDS)["status"] == "ready"


def test_role_changed_to_a_reader_stops_ae_work_too(world, ae):
    _run_task(world)
    world.db.tables["profiles"][0]["role"] = "accountant"
    result = off.offboard(USER, "role_changed", actor_id="admin", old_role="executive", new_role="accountant")
    assert ae["canceled"] == [AE_RUN] and _t(world)["status"] == "failed"
    assert result.counts["ae_runs_canceled"] == 1


def test_role_changed_to_a_writer_keeps_the_ae_work(world, ae):
    _run_task(world)
    world.db.tables["profiles"][0]["role"] = "estimating_admin"
    result = off.offboard(USER, "role_changed", actor_id="admin", old_role="executive", new_role="estimating_admin")
    assert ae["canceled"] == [] and _t(world)["status"] == "waiting_external" and _h(world)["status"] == "active"
    assert result.counts["ae_runs_canceled"] == 0


@pytest.mark.parametrize("mode", ["deactivated", "deleted"])
def test_leaving_modes_delete_every_report_object(world, ae, mode):
    _put_report(world, ae, R_EVIDENCE, "project_timeline")
    _put_report(world, ae, R_BIDS, "bid_invitations")
    result = off.offboard(USER, mode, actor_id="admin")
    assert result.counts["reports_purged"] == 2
    assert not [p for p in ae["store"].objects if p.startswith(f"assistant-reports/{USER}/")]
    assert {_report(world, R_EVIDENCE)["status"], _report(world, R_BIDS)["status"]} == {"purged"}


def test_a_legal_hold_persons_reports_are_kept(world, ae):
    world.db.tables["assistant_profiles"].append({"user_id": USER, "legal_hold": True})
    _put_report(world, ae, R_EVIDENCE, "project_timeline")
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert result.counts["reports_purged"] == 0
    assert _report(world, R_EVIDENCE)["status"] == "ready"
    assert f"assistant-reports/{USER}/{R_EVIDENCE}.xlsx" in ae["store"].objects


def test_an_object_that_survives_its_delete_keeps_the_row_and_is_counted_unfinished(world, ae, monkeypatch):
    _put_report(world, ae, R_EVIDENCE, "project_timeline")
    monkeypatch.setattr("app.services.storage.delete_file", lambda path: None)     # a silent failure
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert _report(world, R_EVIDENCE)["status"] == "ready"
    assert result.counts["reports_purged"] == 0 and result.counts["phase4_unfinished"] == 1
    assert result.ok and world.audits[-1]["payload"]["phase4_unfinished"] == 1


def test_a_run_that_cannot_be_canceled_keeps_its_hold_and_is_counted_unfinished(world, ae):
    _run_task(world)
    ae["cancel_error"] = RuntimeError("supabase 503")
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert _t(world)["status"] == "waiting_external" and _h(world)["status"] == "active"
    assert result.counts["phase4_unfinished"] == 1 and result.counts["ae_runs_canceled"] == 0
    # The sweep mode retries it once the blip is over.
    ae["cancel_error"] = None
    again = off.offboard(USER, "sweep", actor_id=None)
    assert _t(world)["status"] == "failed" and again.counts["ae_runs_canceled"] == 1


def test_an_mfa_reset_leaves_the_ae_work_and_the_reports_alone(world, ae):
    _run_task(world)
    _put_report(world, ae, R_EVIDENCE, "project_timeline")
    result = off.offboard(USER, "mfa_reset", actor_id="admin")
    assert _t(world)["status"] == "waiting_external" and _h(world)["status"] == "active"
    assert _report(world, R_EVIDENCE)["status"] == "ready"
    assert "plans_tasks" not in result.counts and "reports_purged" not in result.counts


def test_cancel_ae_run_treats_a_finished_or_missing_run_as_not_canceled(monkeypatch):
    from app.services import plans_reader_runs

    def refuse(status):
        def cancel(sb, run, *, user):
            assert user.role == "it_admin"
            raise plans_reader_runs.RunRefused("nope", status)
        return cancel

    monkeypatch.setattr(supabase_client, "get_supabase", lambda: object())
    for status in (404, 409):
        monkeypatch.setattr(plans_reader_runs, "cancel", refuse(status))
        assert off.cancel_ae_run(AE_RUN, USER) is False
    monkeypatch.setattr(plans_reader_runs, "cancel", refuse(500))
    with pytest.raises(plans_reader_runs.RunRefused):
        off.cancel_ae_run(AE_RUN, USER)
    monkeypatch.setattr(plans_reader_runs, "cancel", lambda sb, run, *, user: {"id": run["id"]})
    assert off.cancel_ae_run(AE_RUN, USER) is True


def test_the_hook_is_registered_and_names_its_counts(world, ae):
    assert off.phase4_offboard in off.HOOKS
    assert off.phase4_offboard(USER, "mfa_reset") == {}
    assert off.phase4_offboard(USER, "sweep") == {"plans_tasks": 0, "ae_runs_canceled": 0, "ae_holds": 0,
                                                  "reports_purged": 0}


# ── History across modes ─────────────────────────────────────────────────────


def _resume_settings(monkeypatch):
    s = SimpleNamespace(assistant_schedule_max_failures=3, assistant_schedule_max_unread=10)
    monkeypatch.setattr(assistant_scheduler, "get_settings", lambda: s)


def test_mfa_reset_then_deactivation_then_reactivation_and_a_new_grant_stays_paused(world, monkeypatch):
    _resume_settings(monkeypatch)
    world.db.tables["assistant_schedules"].append(schedule())
    off.offboard(USER, "mfa_reset", actor_id="admin")
    assert _schedules(world)[SCHEDULE] == ("paused", "mfa_reset")
    off.offboard(USER, "deactivated", actor_id="admin")
    assert _schedules(world)[SCHEDULE] == ("paused", "owner_offboarded")
    # Reactivation, then the next chat creates a new grant.
    world.db.tables["profiles"][0]["is_active"] = True
    world.db.tables["assistant_delegations"].append(delegation(
        id="del-2", created_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()))
    assert assistant_scheduler.resume_pass(NOW) == 0
    assert _schedules(world)[SCHEDULE] == ("paused", "owner_offboarded")


def test_a_row_paused_no_delegation_before_the_profile_write_ends_owner_offboarded(world, monkeypatch):
    world.db.tables["assistant_schedules"].append(schedule())
    off.revoke_delegation(USER, "deactivated")
    # A scheduler run in the window between the revoke and the profile write.
    world.db.tables["assistant_schedules"][0].update(status="paused", paused_reason="no_delegation",
                                                     paused_at=NOW.isoformat())
    world.db.tables["profiles"][0]["is_active"] = False
    off.offboard(USER, "deactivated", actor_id="admin")
    assert _schedules(world)[SCHEDULE] == ("paused", "owner_offboarded")


def test_the_module_carries_no_em_dash():
    with open(off.__file__, encoding="utf-8") as fh:
        assert EM_DASH not in fh.read()


@pytest.mark.parametrize('mode', off.MODES)
def test_every_offboarding_mode_destroys_personal_browser_material(world, mode):
    world.db.tables['assistant_credentials'] = [{'id':'cred', 'user_id':USER, 'status':'active', 'version':1,
                                                'ciphertext':'encrypted', 'wrapped_dek':'wrapped'}]
    world.db.tables['assistant_browser_sessions'] = [{'user_id':USER, 'ciphertext':'cookie'}]
    result = off.offboard(USER, mode, actor_id='admin')
    assert result.ok
    row = world.db.tables['assistant_credentials'][0]
    assert row['status'] == 'revoked' and row['ciphertext'] is None and row['wrapped_dek'] is None
    assert not world.db.tables['assistant_browser_sessions']


def test_personal_browser_revoke_failure_is_critical(world):
    world.db.rpc_errors['assistant_browser_revoke_owner'] = RuntimeError('database unavailable')
    result = off.offboard(USER, 'deleted', actor_id='admin')
    assert not result.ok and 'browser_credentials' in result.failures


# ── Phase 7 hook (6.8) ───────────────────────────────────────────────────────

CLAIMED_SEND = "5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e52"
OTHER_SEND = "5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e53"
DELIVERED = "bc000001-bc00-4c00-8c00-bc0000000001"
OTHER_INBOX = "bc000002-bc00-4c00-8c00-bc0000000002"
P7_COUNTS = {"inbox_dismissed": 2, "sends_skipped": 1, "subscriptions_deleted": 2, "project_mutes_deleted": 1}


def _seed_p7(world) -> None:
    """Router state of the person and of someone else: two undelivered rows
    (immediate and digest), a delivered one, a pending and a claimed send,
    two overrides and a role default, and a project mute."""
    t = world.db.tables
    t["assistant_inbox"] = [
        inbox_row(),
        inbox_row(id=INBOX2, dedupe_key="p7:d", delivery="digest"),
        inbox_row(id=DELIVERED, dedupe_key="p7:rung", delivered_at=NOW.isoformat()),
        inbox_row(id=OTHER_INBOX, dedupe_key="p7:o", user_id=P7_OTHER),
    ]
    t["assistant_external_sends"] = [
        send_row(),
        send_row(id=CLAIMED_SEND, target_id=INBOX2, status="claimed", claimed_by="w",
                 claimed_at=NOW.isoformat(), attempts=1),
        send_row(id=OTHER_SEND, user_id=P7_OTHER),
    ]
    t["assistant_subscriptions"] = [
        subscription_row(), subscription_row(event_type="bid.due_not_ready"),
        subscription_row(user_id=None, role="executive", source="admin"), subscription_row(user_id=P7_OTHER),
    ]
    t["assistant_project_mutes"] = [
        {"user_id": USER, "project_id": PROJECT, "created_at": NOW.isoformat()},
        {"user_id": P7_OTHER, "project_id": PROJECT, "created_at": NOW.isoformat()},
    ]


def _p7_state(world) -> dict:
    t = world.db.tables
    return {
        "inbox": {r["id"]: r["dismissed_reason"] for r in t["assistant_inbox"]},
        "sends": {r["id"]: (r["status"], r["error_kind"]) for r in t["assistant_external_sends"]},
        "subscriptions": sorted((str(s["role"]), str(s["user_id"])) for s in t["assistant_subscriptions"]),
        "mutes": sorted(m["user_id"] for m in t["assistant_project_mutes"]),
    }


UNTOUCHED = {
    "inbox": {INBOX: None, INBOX2: None, DELIVERED: None, OTHER_INBOX: None},
    "sends": {SEND: ("pending", None), CLAIMED_SEND: ("claimed", None), OTHER_SEND: ("pending", None)},
    "subscriptions": sorted([("None", USER), ("None", USER), ("executive", "None"), ("None", P7_OTHER)]),
    "mutes": sorted([USER, P7_OTHER]),
}


@pytest.mark.parametrize("mode", ["deactivated", "demoted_external", "sweep"])
def test_phase7_leaving_modes_clear_the_router_state_of_the_person(world, mode):
    _seed_p7(world)
    result = off.offboard(USER, mode, actor_id=None if mode == "sweep" else "admin")
    assert result.ok and result.failures == []
    assert _p7_state(world) == {
        "inbox": {INBOX: "owner_offboarded", INBOX2: "owner_offboarded", DELIVERED: None, OTHER_INBOX: None},
        "sends": {SEND: ("skipped", "offboarded"), CLAIMED_SEND: ("claimed", None), OTHER_SEND: ("pending", None)},
        "subscriptions": sorted([("executive", "None"), ("None", P7_OTHER)]),
        "mutes": [P7_OTHER],
    }
    for key, value in P7_COUNTS.items():
        assert result.counts[key] == value, key
    assert "phase7_unfinished" not in result.counts
    # Counts only in the audit.
    payload = world.audits[-1]["payload"]
    assert payload["inbox_dismissed"] == 2 and payload["project_mutes_deleted"] == 1
    assert "p7:" not in str(payload) and "A to-do" not in str(payload)


def test_phase7_deleted_relies_on_the_cascade(world):
    _seed_p7(world)
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.ok
    assert _p7_state(world) == UNTOUCHED
    assert not set(P7_COUNTS) & set(result.counts)


@pytest.mark.parametrize("mode,new_role", [("mfa_reset", None), ("role_changed", "accountant"),
                                           ("role_changed", "estimating_admin")])
def test_phase7_mfa_reset_and_role_changed_leave_the_router_alone(world, mode, new_role):
    _seed_p7(world)
    result = off.offboard(USER, mode, actor_id="admin", old_role="executive", new_role=new_role)
    assert result.ok
    assert _p7_state(world) == UNTOUCHED
    assert not set(P7_COUNTS) & set(result.counts) and "phase7_unfinished" not in result.counts
    assert off.phase7_offboard(USER, mode) == {}


def test_phase7_hook_is_registered_after_phase_4_and_is_idempotent(world):
    assert off.HOOKS.index(off.phase7_offboard) == off.HOOKS.index(off.browser_v2_offboard) - 1
    assert off.HOOKS.index(off.phase4_offboard) < off.HOOKS.index(off.phase7_offboard)
    _seed_p7(world)
    assert off.phase7_offboard(USER, "deactivated") == P7_COUNTS
    assert off.phase7_offboard(USER, "sweep") == {key: 0 for key in P7_COUNTS}


def test_phase7_a_failing_step_is_counted_and_the_rest_still_run(world, monkeypatch):
    from app.assistant import repo

    _seed_p7(world)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "offboard_skip_pending_sends", boom)
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert result.ok and result.failures == []          # never a critical step, never a failed step
    assert result.counts["phase7_unfinished"] == 1
    assert result.counts["inbox_dismissed"] == 2 and result.counts["subscriptions_deleted"] == 2
    assert _p7_state(world)["sends"][SEND] == ("pending", None)


# ── Browser v2 hook (plan 17.5, WI-18) ───────────────────────────────────────

BV2_OTHER = "12121212-1212-4121-8121-121212121212"
D_OLD, D_LIVE, D_STORED, D_PURGED, D_OTHER = (f"dd000000-0000-4000-8000-00000000000{i}" for i in range(1, 6))
MIGRATION_0192 = Path(__file__).resolve().parents[1] / "supabase/migrations/0192_assistant_browser_v2.sql"
V2_PREFIX = f"assistant-files/{USER}/browser/"


def _download(row_id, user=USER, **over) -> dict:
    base = {"id": row_id, "user_id": user, "job_id": "job-1", "status": "staged", "purged_at": None,
            "storage_path": f"assistant-files/{user}/browser/{row_id}-a.pdf",
            "expires_at": (NOW + timedelta(days=3)).isoformat(), "created_at": NOW.isoformat()}
    base.update(over)
    return base


def _simulate_revoke(world, calls: list | None = None):
    """The tables the 0192 RPC assistant_browser_revoke_owner clears (its text
    is pinned below); the FakeDB models only the credentials and sessions."""
    from app.assistant import repo

    real = repo.browser_revoke_owner

    def revoke(user_id):
        if calls is not None:
            calls.append(user_id)
        t = world.db.tables
        for table, status in (("assistant_browser_site_grants", "revoked"), ("assistant_browser_admissions", "declined"),
                              ("assistant_browser_requests", "cancelled"), ("assistant_browser_holds", "aborted"),
                              ("assistant_browser_live_sessions", "ended")):
            for row in t.get(table, []):
                if row.get("user_id") == user_id and row.get("status") in (
                        "active", "waiting_grant", "ready", "requested", "open", "approved"):
                    row["status"] = status
        t["assistant_browser_user_safe_rules"] = [
            r for r in t.get("assistant_browser_user_safe_rules", []) if r.get("user_id") != user_id]
        return real(user_id)

    return revoke


@pytest.fixture
def v2(world, monkeypatch):
    from app.assistant import repo

    store = FakeStorage()
    store.install(monkeypatch)
    t = world.db.tables
    t["assistant_browser_site_grants"] = [
        {"id": "g1", "user_id": USER, "status": "active"}, {"id": "g2", "user_id": BV2_OTHER, "status": "active"}]
    t["assistant_browser_admissions"] = [
        {"id": "a1", "user_id": USER, "status": "waiting_grant"}, {"id": "a2", "user_id": USER, "status": "enqueued"},
        {"id": "a3", "user_id": BV2_OTHER, "status": "ready"}]
    t["assistant_browser_requests"] = [{"id": "r1", "user_id": USER, "status": "requested"}]
    t["assistant_browser_holds"] = [{"id": "h1", "user_id": USER, "status": "open"},
                                    {"id": "h2", "user_id": BV2_OTHER, "status": "open"}]
    t["assistant_browser_live_sessions"] = [{"id": "l1", "user_id": USER, "status": "open"}]
    t["assistant_browser_user_safe_rules"] = [{"id": "s1", "user_id": USER}, {"id": "s2", "user_id": BV2_OTHER}]
    t["assistant_browser_job_credentials"] = [
        {"id": "p1", "user_id": USER}, {"id": "p2", "user_id": USER}, {"id": "p3", "user_id": BV2_OTHER}]
    t["assistant_browser_downloads"] = [
        _download(D_OLD, expires_at=(NOW - timedelta(days=1)).isoformat()), _download(D_LIVE),
        _download(D_STORED, status="stored", storage_path=None, expires_at=None),
        _download(D_PURGED, purged_at=NOW.isoformat()),
        _download(D_OTHER, user=BV2_OTHER)]
    for row in t["assistant_browser_downloads"]:
        if row["storage_path"]:
            store.objects[row["storage_path"]] = b"%PDF"
    revokes: list = []
    monkeypatch.setattr(repo, "browser_revoke_owner", _simulate_revoke(world, revokes))
    return SimpleNamespace(world=world, db=world.db, store=store, revokes=revokes)


def _dl(v2, row_id) -> dict:
    return next(r for r in v2.db.tables["assistant_browser_downloads"] if r["id"] == row_id)


def _owned(v2, table, user) -> list:
    return [r["id"] for r in v2.db.tables[table] if r["user_id"] == user]


def test_browser_v2_hook_is_registered_right_after_phase7_and_before_phase8(world):
    # Phase 8 (WI-13) appends its hook after browser v2's.
    assert off.HOOKS[-2] is off.browser_v2_offboard
    assert off.HOOKS.index(off.phase7_offboard) < off.HOOKS.index(off.browser_v2_offboard)


@pytest.mark.parametrize("mode", ["deactivated", "demoted_external", "sweep"])
def test_browser_v2_leaving_modes_clear_the_state_of_the_person_only(v2, mode):
    result = off.offboard(USER, mode, actor_id=None if mode == "sweep" else "admin")
    assert result.ok and result.failures == []
    t = v2.db.tables
    # The early step and the hook each ran the revoke RPC (the second after the jobs stopped).
    assert v2.revokes == [USER, USER]
    assert [r["status"] for r in t["assistant_browser_site_grants"]] == ["revoked", "active"]
    assert {r["id"]: r["status"] for r in t["assistant_browser_admissions"]} == {
        "a1": "declined", "a2": "enqueued", "a3": "ready"}
    assert {r["id"]: r["status"] for r in t["assistant_browser_holds"]} == {"h1": "aborted", "h2": "open"}
    assert t["assistant_browser_live_sessions"][0]["status"] == "ended"
    assert _owned(v2, "assistant_browser_user_safe_rules", USER) == []
    assert _owned(v2, "assistant_browser_user_safe_rules", BV2_OTHER) == ["s2"]
    # Pinned job credentials: the person's two go, the other person's stays.
    assert _owned(v2, "assistant_browser_job_credentials", USER) == []
    assert _owned(v2, "assistant_browser_job_credentials", BV2_OTHER) == ["p3"]
    # Staged downloads: both live and expired objects go, rows marked purged; the stored and
    # already purged rows and the other person's object are untouched.
    assert _dl(v2, D_OLD)["purged_at"] and _dl(v2, D_LIVE)["purged_at"]
    assert _dl(v2, D_STORED)["purged_at"] is None and _dl(v2, D_PURGED)["purged_at"] == NOW.isoformat()
    assert _dl(v2, D_OTHER)["purged_at"] is None
    assert f"{V2_PREFIX}{D_OLD}-a.pdf" not in v2.store.objects and f"{V2_PREFIX}{D_LIVE}-a.pdf" not in v2.store.objects
    assert f"assistant-files/{BV2_OTHER}/browser/{D_OTHER}-a.pdf" in v2.store.objects
    assert result.counts["browser_v2_pins_deleted"] == 2 and result.counts["browser_v2_downloads_purged"] == 2
    assert "browser_v2_unfinished" not in result.counts and "browser_v2_downloads_unpurged" not in result.counts
    # Counts only in the audit: no path, file name or id.
    payload = v2.world.audits[-1]["payload"]
    assert payload["browser_v2_pins_deleted"] == 2 and "browser/" not in str(payload) and D_OLD not in str(payload)


def test_browser_v2_purge_rpc_runs_with_no_user_scope(v2):
    off.offboard(USER, "deactivated", actor_id="admin")
    purge = [p for name, p in v2.db.rpc_calls if name == "assistant_browser_purge_v2"]
    assert len(purge) == 1 and "p_user_id" not in purge[0]


def test_browser_v2_deleted_purges_only_the_staged_objects(v2):
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.ok
    assert v2.revokes == [USER]                      # the early step only; the cascade does the rest
    t = v2.db.tables
    assert [r["status"] for r in t["assistant_browser_site_grants"]] == ["revoked", "active"]
    assert _owned(v2, "assistant_browser_job_credentials", USER) == ["p1", "p2"]
    assert _dl(v2, D_OLD)["purged_at"] and _dl(v2, D_LIVE)["purged_at"]
    assert not [rpc for rpc, _ in v2.db.rpc_calls if rpc == "assistant_browser_purge_v2"]
    assert result.counts["browser_v2_downloads_purged"] == 2 and "browser_v2_pins_deleted" not in result.counts


@pytest.mark.parametrize("mode,new_role", [("mfa_reset", None), ("role_changed", "accountant"),
                                           ("role_changed", "estimating_admin")])
def test_browser_v2_mfa_reset_and_role_changed_leave_the_hook_alone(v2, mode, new_role):
    result = off.offboard(USER, mode, actor_id="admin", old_role="executive", new_role=new_role)
    assert result.ok
    assert off.browser_v2_offboard(USER, mode) == {}
    assert not [k for k in result.counts if k.startswith("browser_v2_")]
    assert _owned(v2, "assistant_browser_job_credentials", USER) == ["p1", "p2"]
    assert _dl(v2, D_LIVE)["purged_at"] is None and f"{V2_PREFIX}{D_LIVE}-a.pdf" in v2.store.objects


def test_browser_v2_legal_hold_keeps_the_staged_objects_but_not_the_access(v2):
    v2.db.tables["assistant_profiles"].append({"user_id": USER, "legal_hold": True})
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert result.ok and result.counts["browser_v2_downloads_purged"] == 0
    assert "browser_v2_unfinished" not in result.counts
    assert _dl(v2, D_LIVE)["purged_at"] is None and f"{V2_PREFIX}{D_LIVE}-a.pdf" in v2.store.objects
    # Access and authentication material still go.
    assert v2.db.tables["assistant_browser_site_grants"][0]["status"] == "revoked"
    assert _owned(v2, "assistant_browser_job_credentials", USER) == []


def test_browser_v2_an_object_that_cannot_be_deleted_keeps_its_row_and_is_counted(v2):
    v2.store.fail_delete = True
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert result.ok                                    # never a critical step
    assert result.counts["browser_v2_downloads_purged"] == 0
    assert result.counts["browser_v2_downloads_unpurged"] == 2 and result.counts["browser_v2_unfinished"] == 1
    assert _dl(v2, D_LIVE)["purged_at"] is None
    v2.store.fail_delete = False
    again = off.browser_v2_offboard(USER, "sweep")       # the sweep retries
    assert again["browser_v2_downloads_purged"] == 2 and "browser_v2_unfinished" not in again


def test_browser_v2_a_hard_delete_counts_objects_it_could_not_purge(v2):
    v2.store.fail_delete = True
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.counts["browser_v2_downloads_unpurged"] == 2 and result.counts["browser_v2_unfinished"] == 1


def test_browser_v2_state_left_after_the_revoke_is_counted_unfinished(v2, monkeypatch):
    from app.assistant import repo

    monkeypatch.setattr(repo, "browser_revoke_owner", lambda user_id: 0)       # the RPC cleared nothing
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert result.counts["browser_v2_unfinished"] == 1
    # The Python-side steps still ran.
    assert _owned(v2, "assistant_browser_job_credentials", USER) == []
    assert result.counts["browser_v2_downloads_purged"] == 2


def test_browser_v2_a_failing_step_is_counted_and_the_rest_still_run(v2, monkeypatch):
    from app.assistant import repo

    def boom(*a, **k):
        raise RuntimeError("db down")

    with monkeypatch.context() as m:
        m.setattr(repo, "browser_offboard_pins_delete", boom)
        counts = off.browser_v2_offboard(USER, "deactivated")
    assert counts["browser_v2_unfinished"] == 1
    assert counts["browser_v2_downloads_purged"] == 2 and "browser_v2_pins_deleted" not in counts
    v2.db.rpc_errors["assistant_browser_purge_v2"] = RuntimeError("rpc down")
    counts = off.browser_v2_offboard(USER, "sweep")          # the sweep retries what was left
    assert counts["browser_v2_unfinished"] == 1 and counts["browser_v2_pins_deleted"] == 2


def test_browser_v2_hook_is_idempotent(v2):
    first = off.browser_v2_offboard(USER, "deactivated")
    assert first["browser_v2_pins_deleted"] == 2 and first["browser_v2_downloads_purged"] == 2
    second = off.browser_v2_offboard(USER, "sweep")
    assert second["browser_v2_pins_deleted"] == 0 and second["browser_v2_downloads_purged"] == 0
    assert "browser_v2_unfinished" not in second
    assert _dl(v2, D_OTHER)["purged_at"] is None


def test_browser_v2_hook_module_carries_no_em_dash():
    from app.assistant import repo
    from app.routers import assistant_inspection
    from app.services import assistant_browser_records

    for module in (off, repo, assistant_inspection, assistant_browser_records):
        with open(module.__file__, encoding="utf-8") as fh:
            assert EM_DASH not in fh.read(), module.__name__


REVOKE_TOUCHES = ("assistant_credentials", "assistant_browser_site_grants", "assistant_browser_admissions",
                  "assistant_browser_requests", "assistant_browser_holds", "assistant_browser_live_sessions",
                  "assistant_browser_user_safe_rules", "assistant_browser_sessions")


def test_the_0192_revoke_rpc_names_every_table_the_hook_relies_on():
    sql = MIGRATION_0192.read_text(encoding="utf-8")
    body = sql[sql.index("create or replace function public.assistant_browser_revoke_owner"):]
    body = body[: body.index("$$;", body.index("end $$") if "end $$" in body else 0) + 3]
    code = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
    for table in REVOKE_TOUCHES:
        assert re.search(rf"(update|delete from) public\.{table}\b", code), table
    assert "assistant_browser_cancel_credentials" in code
    # What the RPC leaves is exactly what the Python steps delete.
    for left in ("assistant_browser_job_credentials", "assistant_browser_downloads"):
        assert f"public.{left}" not in code, left


# ── Phase 8 hook (plan 11, WI-13) ────────────────────────────────────────────

T_PEND, T_CLAIMED, T_DONE, T_OTHER, T_OTHER2 = (f"7a000000-0000-4000-8000-00000000000{i}" for i in range(1, 6))
T_RUN, T_OTHER_RUN = "7b000000-0000-4000-8000-000000000001", "7b000000-0000-4000-8000-000000000002"
P8_OTHER = P7_OTHER


def _seed_p8(world) -> None:
    """The person's pending, claimed and finished triage items and one
    claimed run, and the same for someone else."""
    t = world.db.tables
    t["assistant_triage_items"] = [
        triage_item_row(id=T_PEND),
        triage_item_row(id=T_CLAIMED, status="claimed", run_id=T_RUN),
        triage_item_row(id=T_DONE, status="decided", decision="none"),
        triage_item_row(id=T_OTHER, user_id=P8_OTHER),
        triage_item_row(id=T_OTHER2, user_id=P8_OTHER, status="claimed", run_id=T_OTHER_RUN),
    ]
    t["assistant_triage_runs"] = [
        triage_run_row(id=T_RUN),
        triage_run_row(id=T_OTHER_RUN, user_id=P8_OTHER),
    ]


def _p8_state(world) -> dict:
    t = world.db.tables
    return {
        "items": {r["id"]: (r["status"], r["skip_reason"]) for r in t["assistant_triage_items"]},
        "runs": {r["id"]: (r["status"], r["error_kind"]) for r in t["assistant_triage_runs"]},
    }


P8_UNTOUCHED = {
    "items": {T_PEND: ("pending", None), T_CLAIMED: ("claimed", None), T_DONE: ("decided", None),
              T_OTHER: ("pending", None), T_OTHER2: ("claimed", None)},
    "runs": {T_RUN: ("claimed", None), T_OTHER_RUN: ("claimed", None)},
}


@pytest.mark.parametrize("mode", ["deactivated", "demoted_external", "sweep"])
def test_phase8_leaving_modes_skip_the_triage_work_of_the_person_only(world, mode):
    _seed_p8(world)
    result = off.offboard(USER, mode, actor_id=None if mode == "sweep" else "admin")
    assert result.ok and result.failures == []
    assert _p8_state(world) == {
        "items": {T_PEND: ("skipped", "offboarded"), T_CLAIMED: ("skipped", "offboarded"),
                  T_DONE: ("decided", None), T_OTHER: ("pending", None), T_OTHER2: ("claimed", None)},
        # The claimed run fails first, so a late finish emits nothing; the other person's run is untouched.
        "runs": {T_RUN: ("failed", "switched_off"), T_OTHER_RUN: ("claimed", None)},
    }
    assert result.counts["triage_items_skipped"] == 2
    assert "phase8_unfinished" not in result.counts
    assert world.audits[-1]["payload"]["triage_items_skipped"] == 2


def test_phase8_deleted_relies_on_the_cascade(world):
    _seed_p8(world)
    result = off.offboard(USER, "deleted", actor_id="admin")
    assert result.ok
    assert _p8_state(world) == P8_UNTOUCHED
    assert "triage_items_skipped" not in result.counts and off.phase8_offboard(USER, "deleted") == {}


@pytest.mark.parametrize("mode,new_role", [("mfa_reset", None), ("role_changed", "accountant"),
                                           ("role_changed", "estimating_admin")])
def test_phase8_mfa_reset_and_role_changed_leave_triage_alone(world, mode, new_role):
    _seed_p8(world)
    result = off.offboard(USER, mode, actor_id="admin", old_role="executive", new_role=new_role)
    assert result.ok
    assert _p8_state(world) == P8_UNTOUCHED
    assert "triage_items_skipped" not in result.counts and "phase8_unfinished" not in result.counts
    assert off.phase8_offboard(USER, mode) == {}
    assert not [c for c in world.db.rpc_calls if c[0] == "assistant_triage_skip_user"]


def test_phase8_hook_is_registered_last_and_is_idempotent(world):
    assert off.HOOKS[-1] is off.phase8_offboard
    assert off.HOOKS.index(off.browser_v2_offboard) < off.HOOKS.index(off.phase8_offboard)
    _seed_p8(world)
    assert off.phase8_offboard(USER, "deactivated") == {"triage_items_skipped": 2}
    assert off.phase8_offboard(USER, "sweep") == {"triage_items_skipped": 0}
    assert off.phase8_offboard(USER, "deactivated") == {"triage_items_skipped": 0}


def test_phase8_the_rpc_is_called_for_the_person_with_the_offboarded_reason(world):
    _seed_p8(world)
    off.phase8_offboard(USER, "deactivated")
    calls = [p for name, p in world.db.rpc_calls if name == "assistant_triage_skip_user"]
    assert len(calls) == 1
    assert calls[0]["p_user_id"] == USER and calls[0]["p_reason"] == "offboarded"


def test_phase8_page_size_matches_the_sql_limit():
    import re
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "0196_assistant_phase8.sql").read_text()
    start = sql.index("create or replace function public.assistant_triage_skip_user(")
    nxt = sql.find("create or replace function", start + 10)
    body = sql[start:nxt if nxt != -1 else len(sql)]
    limits = {int(n) for n in re.findall(r"\blimit\s+(\d+)", body)}
    assert limits == {off.TRIAGE_SKIP_PAGE}


def test_phase8_a_full_page_repeats_until_the_backlog_is_gone(world, monkeypatch):
    from app.assistant import repo

    pages = iter([500, 500, 120])
    seen: list[str] = []
    monkeypatch.setattr(repo, "triage_skip_user", lambda user_id, reason, now=None: seen.append(reason) or next(pages))
    assert off.phase8_offboard(USER, "sweep") == {"triage_items_skipped": 1120}
    assert seen == ["offboarded"] * 3


def test_phase8_a_backlog_beyond_the_rounds_is_left_for_the_next_sweep(world, monkeypatch):
    from app.assistant import repo

    calls: list[int] = []
    monkeypatch.setattr(repo, "triage_skip_user", lambda *a, **k: calls.append(1) or 500)
    result = off.phase8_offboard(USER, "sweep")
    assert len(calls) == off.TRIAGE_SKIP_ROUNDS
    assert result == {"triage_items_skipped": 500 * off.TRIAGE_SKIP_ROUNDS}


def test_phase8_a_failing_step_is_counted_and_never_raised(world, monkeypatch):
    from app.assistant import repo

    _seed_p8(world)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "triage_skip_user", boom)
    result = off.offboard(USER, "deactivated", actor_id="admin")
    assert result.ok and result.failures == []          # never a critical step, never a failed step
    assert result.counts["phase8_unfinished"] == 1
    assert _p8_state(world) == P8_UNTOUCHED
    # The Phase 7 hook still ran.
    assert "inbox_dismissed" in result.counts


def test_offboard_refuses_the_company_spend_id_before_any_step_or_hook(world, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(off, "HOOKS", [lambda u, m: seen.append((u, m)) or {}])
    _seed(world)
    _seed_p8(world)
    before = [(c[0], c[1]) for c in world.db.calls]
    for mode in off.MODES:
        for spelling in (spend.COMPANY_SPEND_ID, spend.COMPANY_SPEND_ID.upper()):
            with pytest.raises(ValueError):
                off.offboard(spelling, mode, actor_id="admin")
    assert seen == []
    assert world.audits == [] and world.bells == []
    assert [(c[0], c[1]) for c in world.db.calls] == before
    assert world.db.rpc_calls == []
    assert _p8_state(world) == P8_UNTOUCHED


def test_the_sweep_of_a_live_triage_hold_never_offboards_the_company_id(world, monkeypatch):
    """A fake active triage hold under the company id: the owners read never
    lists it, and offboard refuses it even if a caller passes it anyway."""
    from app.assistant import repo

    world.db.tables["assistant_spend_reservations"] = [
        reservation(user_id=spend.COMPANY_SPEND_ID, id="5a000000-0000-4000-8000-0000000000aa", kind="triage"),
    ]
    assert spend.COMPANY_SPEND_ID not in repo.owners_with_open_state(None)
    with pytest.raises(ValueError):
        off.offboard(spend.COMPANY_SPEND_ID, "sweep", actor_id=None)
    assert [r["status"] for r in world.db.tables["assistant_spend_reservations"]] == ["active"]


def test_the_phase8_hook_carries_no_em_dash():
    text = Path(off.__file__).read_text(encoding="utf-8")
    start = text.index("# ── Phase 8 hook")
    end = text.index("# ── The entry point")
    assert EM_DASH not in text[start:end] and chr(0x2013) not in text[start:end]
