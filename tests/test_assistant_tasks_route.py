"""GET /assistant/tasks/{id} with its lazy repair, the task repo functions,
and the spend reservation repo functions over the fake 0174 rpcs
(docs/ASSISTANT_PHASE2_PLAN.md 3.6, 3.8 and 5.2, WI-2).

Invariants pinned here:

  owner     another user's task, or a non-uuid id, is 404 task_not_found;
  shape     TaskOut is built from the task row and
            split_handoff.task_progress_for (monkeypatched: WI-3 fills it);
  repair    a ready waiting_external task, one older than 6 hours, and a
            running task idle for 10 minutes (fenced back first) enqueue
            assistant_continue with 3 attempts; a fresh one does not; a
            pending task older than 10 minutes fails stage_failed with its
            hold released and its staged rows unclaimed;
  tasks     one task per approval (TaskExists), fenced updates;
  spend     reserve refuses at the user cap and at the global cap, a hold
            counts until settled or expired, a split hold is reduced by the
            log rows carrying its id, close is fenced and records the
            logged cost, money travels as strings.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, repo
from app.core import supabase_client
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant as router_mod
from app.services import llm_queue, split_handoff

from tests.assistant_fakes import (
    APPROVAL_ID, CONV, HOLD, NOW, OTHER_USER, PROJECT, RUN, SPLIT_JOB, STAGED, STAGED2, TASK, USER, FakeDB,
    approval, profile, reservation, settings_row, staged_file, task,
)

# The real service, kept before the fixture patches it (the integration
# test below feeds TaskOut from it over the fake tables).
REAL_TASK_PROGRESS_FOR = split_handoff.task_progress_for

PROGRESS = {
    "job": {"id": SPLIT_JOB, "status": "processing", "file_count": 2, "files_done": 1, "files_failed": 0},
    "files": [
        {"split_file_id": "f1", "filename": "A-101.pdf", "status": "done", "outcome": "split", "documents": 6,
         "categories": {"drawing": 6}, "reason": None},
        {"split_file_id": "f2", "filename": "Spec.pdf", "status": "running", "outcome": "pending",
         "documents": 0, "categories": {}, "reason": None},
    ],
    "ready": False,
}


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_tasks=[task()], assistant_files=[], assistant_spend_reservations=[reservation()],
        llm_call_log=[], projects=[{"id": PROJECT, "number": "26.9.7130"}],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    progress = {"value": dict(PROGRESS)}
    monkeypatch.setattr(split_handoff, "task_progress_for", lambda t: progress["value"])
    jobs: list[dict] = []
    monkeypatch.setattr(llm_queue, "enqueue", lambda job_type, **kw: jobs.append({"job_type": job_type, **kw})
                        or {"id": f"job-{len(jobs)}"})
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True, aal="aal2", mfa_enrolled=True)
    return SimpleNamespace(client=TestClient(app), db=db, jobs=jobs, progress=progress)


def _age(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


# ── shape and ownership ──────────────────────────────────────────────────────


def test_task_out_shape(api):
    res = api.client.get(f"/assistant/tasks/{TASK}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {
        "id": TASK, "kind": "split_into_project", "status": "waiting_external",
        "title": "Split 2 files into 26.9.7130", "project": {"id": PROJECT, "number": "26.9.7130"},
        "conversation_id": CONV, "approval_id": APPROVAL_ID, "failure_reason": None,
        "created_at": api.db.tables["assistant_tasks"][0]["created_at"], "finished_at": None,
        "job": PROGRESS["job"], "files": PROGRESS["files"], "ready": False,
    }
    assert api.jobs == []


def test_another_users_task_and_bad_ids_are_404(api):
    api.db.tables["assistant_tasks"][0]["user_id"] = OTHER_USER
    for path in (f"/assistant/tasks/{TASK}", "/assistant/tasks/nope"):
        res = api.client.get(path)
        assert res.status_code == 404 and res.headers["X-Error-Code"] == "task_not_found"
    assert api.jobs == []


def test_a_failed_progress_read_still_answers_and_repairs_nothing(api, monkeypatch):
    def boom(t):
        raise NotImplementedError

    monkeypatch.setattr(split_handoff, "task_progress_for", boom)
    api.db.tables["assistant_tasks"][0]["created_at"] = _age(minutes=5)
    body = api.client.get(f"/assistant/tasks/{TASK}").json()
    assert body["job"] is None and body["files"] == [] and body["ready"] is False
    assert body["project"] == {"id": PROJECT, "number": "26.9.7130"}
    assert api.jobs == []


def test_a_finished_task_shows_the_outcomes_recorded_when_it_finished(api):
    """Removing a filed document later (the Files panel) must not turn a
    finished split's file into Waiting or Missing: the result recorded at
    finish wins; the file name stays live and an unrecorded file stays live."""
    row = api.db.tables["assistant_tasks"][0]
    row["status"] = "done"
    row["finished_at"] = NOW.isoformat()
    row["result"] = {"outcome": "split", "files": [
        {"split_file_id": "f1", "status": "done", "outcome": "split", "documents": 6,
         "categories": {"drawing": 6}, "reason": None},
    ]}
    api.progress["value"] = {
        **PROGRESS, "ready": True,
        "job": {**PROGRESS["job"], "status": "done", "files_done": 2},
        "files": [
            {"split_file_id": "f1", "filename": "A-101.pdf", "status": "done", "outcome": "pending",
             "documents": 0, "categories": {}, "reason": None},
            {"split_file_id": "f2", "filename": "Spec.pdf", "status": "done", "outcome": "missing",
             "documents": 0, "categories": {}, "reason": None},
        ],
    }
    body = api.client.get(f"/assistant/tasks/{TASK}").json()
    f1, f2 = body["files"]
    assert f1 == {"split_file_id": "f1", "filename": "A-101.pdf", "status": "done", "outcome": "split",
                  "documents": 6, "categories": {"drawing": 6}, "reason": None}
    assert f2["outcome"] == "missing" and f2["filename"] == "Spec.pdf"
    assert api.jobs == []


def test_a_running_task_keeps_the_live_outcomes(api):
    row = api.db.tables["assistant_tasks"][0]
    row["result"] = {"files": [{"split_file_id": "f2", "outcome": "split", "documents": 9}]}
    body = api.client.get(f"/assistant/tasks/{TASK}").json()
    assert body["files"] == PROGRESS["files"]


def test_task_out_from_the_real_split_service(api, monkeypatch):
    """The route's TaskOut over split_handoff.task_progress_for itself: file
    names, outcomes, the job counts and the project number reach the card,
    and only the files the task staged are counted."""
    monkeypatch.setattr(split_handoff, "task_progress_for", REAL_TASK_PROGRESS_FOR)
    row = api.db.tables["assistant_tasks"][0]
    row["payload"] = {**row["payload"], "files": [
        {"assistant_file_id": STAGED, "split_file_id": "sf-1", "pages": 3},
        {"assistant_file_id": STAGED2, "split_file_id": "sf-2", "pages": 2},
    ]}
    api.db.tables.update(
        bid_split_jobs=[{"id": SPLIT_JOB, "status": "processing", "project_id": PROJECT, "source": "project",
                         "assistant_task_id": TASK}],
        bid_split_files=[
            {"id": "sf-1", "job_id": SPLIT_JOB, "filename": "A-101.pdf", "status": "done", "page_count": 3,
             "created_at": _age(minutes=3), "finished_at": _age(minutes=1)},
            {"id": "sf-2", "job_id": SPLIT_JOB, "filename": "Spec.pdf", "status": "running", "page_count": 2,
             "created_at": _age(minutes=2)},
            {"id": "sf-human", "job_id": SPLIT_JOB, "filename": "Other.pdf", "status": "done", "page_count": 1,
             "created_at": _age(minutes=1)},
        ],
        bid_split_segments=[{"id": "seg-1", "file_id": "sf-1"}, {"id": "seg-2", "file_id": "sf-1"}],
        project_files=[
            {"id": "pf-1", "project_id": PROJECT, "bid_split_file_id": "sf-1", "bid_split_segment_id": "seg-1",
             "is_source_set": False, "note": None, "category": "drawing"},
            {"id": "pf-2", "project_id": PROJECT, "bid_split_file_id": "sf-1", "bid_split_segment_id": "seg-2",
             "is_source_set": False, "note": None, "category": "spec"},
        ],
    )
    body = api.client.get(f"/assistant/tasks/{TASK}").json()
    assert body["project"] == {"id": PROJECT, "number": "26.9.7130"}
    assert body["job"] == {"id": SPLIT_JOB, "status": "processing", "file_count": 2, "files_done": 1,
                           "files_failed": 0}
    assert body["files"] == [
        {"split_file_id": "sf-1", "filename": "A-101.pdf", "status": "done", "outcome": "split", "documents": 2,
         "categories": {"drawing": 1, "spec": 1}, "reason": None},
        {"split_file_id": "sf-2", "filename": "Spec.pdf", "status": "running", "outcome": "pending",
         "documents": 0, "categories": {}, "reason": None},
    ]
    assert body["ready"] is False
    assert api.jobs == []


# ── lazy repair ──────────────────────────────────────────────────────────────


def _continue_job(api):
    assert len(api.jobs) == 1
    job = api.jobs[0]
    assert job["job_type"] == "assistant_continue" and job["target_id"] == TASK
    assert job["payload"] == {"task_id": TASK, "user_id": USER}
    assert job["max_attempts_override"] == 3 and job["priority"] == 45 and job["created_by"] == USER
    return job


def test_ready_waiting_task_enqueues_the_continuation(api):
    api.progress["value"] = {**PROGRESS, "ready": True}
    assert api.client.get(f"/assistant/tasks/{TASK}").json()["ready"] is True
    _continue_job(api)
    assert api.db.tables["assistant_tasks"][0]["status"] == "waiting_external"


def test_waiting_task_older_than_six_hours_enqueues(api):
    api.db.tables["assistant_tasks"][0]["created_at"] = _age(hours=6, minutes=1)
    api.client.get(f"/assistant/tasks/{TASK}")
    _continue_job(api)


def test_fresh_waiting_task_is_left_alone(api):
    api.client.get(f"/assistant/tasks/{TASK}")
    assert api.jobs == []


def test_stale_running_task_is_fenced_back_then_enqueued(api):
    row = api.db.tables["assistant_tasks"][0]
    row.update(status="running", updated_at=_age(minutes=11))
    body = api.client.get(f"/assistant/tasks/{TASK}").json()
    assert body["status"] == "waiting_external" and row["status"] == "waiting_external"
    _continue_job(api)


def test_fresh_running_task_is_left_alone(api):
    row = api.db.tables["assistant_tasks"][0]
    row.update(status="running", updated_at=_age(minutes=3))
    assert api.client.get(f"/assistant/tasks/{TASK}").json()["status"] == "running"
    assert api.jobs == []


def test_stale_pending_task_fails_releases_and_unclaims(api):
    row = api.db.tables["assistant_tasks"][0]
    row.update(status="pending", split_job_id=None, created_at=_age(minutes=11))
    api.db.tables["assistant_files"] = [
        staged_file(consumed_at=NOW.isoformat(), consumed_task_id=TASK, consumed_project_id=PROJECT),
        staged_file(id=STAGED2, storage_path="p2", consumed_at=NOW.isoformat(), consumed_task_id="other"),
    ]
    api.progress["value"] = {"job": None, "files": [], "ready": False}
    body = api.client.get(f"/assistant/tasks/{TASK}").json()
    assert body["status"] == "failed" and body["failure_reason"] == "stage_failed" and body["finished_at"]
    hold = api.db.tables["assistant_spend_reservations"][0]
    assert hold["status"] == "released" and hold["actual_usd"] == "0" and hold["settled_at"]
    first, second = api.db.tables["assistant_files"]
    assert first["consumed_task_id"] is None and first["consumed_at"] is None
    assert second["consumed_task_id"] == "other"
    assert api.jobs == []
    # A second read changes nothing more.
    assert api.client.get(f"/assistant/tasks/{TASK}").json()["status"] == "failed"


def test_fresh_pending_task_is_left_alone(api):
    api.db.tables["assistant_tasks"][0].update(status="pending", created_at=_age(minutes=3))
    assert api.client.get(f"/assistant/tasks/{TASK}").json()["status"] == "pending"
    assert api.db.tables["assistant_spend_reservations"][0]["status"] == "active"


def test_finished_tasks_are_never_repaired(api):
    for state in ("done", "failed", "canceled"):
        api.db.tables["assistant_tasks"][0].update(status=state, created_at=_age(hours=7), updated_at=_age(hours=7))
        api.progress["value"] = {**PROGRESS, "ready": True}
        assert api.client.get(f"/assistant/tasks/{TASK}").json()["status"] == state
    assert api.jobs == []


# ── task repo ────────────────────────────────────────────────────────────────


def test_one_task_per_approval_and_fenced_updates(api):
    api.db.tables["assistant_tasks"] = []
    row = repo.insert_task(USER, {"id": TASK, "kind": "split_into_project", "approval_id": APPROVAL_ID})
    assert row["id"] == TASK and row["status"] == "pending" and row["user_id"] == USER
    with pytest.raises(repo.TaskExists) as exc:
        repo.insert_task(USER, {"kind": "split_into_project", "approval_id": APPROVAL_ID})
    assert "23505" in str(exc.value) and "assistant_tasks_approval_uq" in str(exc.value)
    assert repo.task_by_approval(USER, APPROVAL_ID)["id"] == TASK
    assert repo.task_by_approval(OTHER_USER, APPROVAL_ID) is None
    assert repo.update_task(USER, TASK, {"status": "running"}, expect_status="waiting_external") is False
    assert repo.update_task(USER, TASK, {"status": "waiting_external"}, expect_status="pending") is True
    assert repo.update_task(USER, TASK, {"status": "running"}, expect_status=("waiting_external", "running"))
    assert repo.update_task(OTHER_USER, TASK, {"status": "done"}) is False
    assert repo.get_task(USER, TASK)["status"] == "running"
    repo.update_task(USER, TASK, {"split_job_id": SPLIT_JOB})
    assert [t["id"] for t in repo.active_task_jobs(USER)] == [TASK]
    assert [t["id"] for t in repo.list_tasks(USER, limit=5)] == [TASK]


def test_open_split_cards_and_conversation_for_run(api):
    later = (NOW + timedelta(hours=4)).isoformat()
    api.db.tables["assistant_approvals"] = [
        approval(action_kind="split_files_into_project", expires_at=later,
                 payload={"args": {"file_ids": ["short"]}, "request": {"json": {"file_ids": [STAGED]}}}),
        approval(id="a2", action_kind="split_files_into_project", expires_at=later,
                 payload={"args": {"file_ids": [STAGED2]}}),
        approval(id="a3", action_kind="split_files_into_project", status="rejected",
                 payload={"request": {"json": {"file_ids": ["gone"]}}}),
        approval(id="a4", action_kind="probe_write", payload={"request": {"json": {"file_ids": ["other"]}}}),
        approval(id="a5", action_kind="split_files_into_project", expires_at=_age(minutes=1),
                 payload={"request": {"json": {"file_ids": ["stale"]}}}),
        approval(id="a6", action_kind="split_files_into_project", status="approved", expires_at=_age(minutes=1),
                 payload={"request": {"json": {"file_ids": ["approved-one"]}}}),
    ]
    assert repo.open_split_card_file_ids(USER) == {STAGED, STAGED2}
    assert repo.open_split_card_file_ids(OTHER_USER) == set()
    # DELETE /assistant/files also counts decided cards whose execute job has
    # not run yet; rejected and other people's cards still never count.
    assert repo.open_split_card_file_ids(USER, include_decided=True) == {STAGED, STAGED2, "approved-one"}
    api.db.tables["assistant_tool_calls"] = [{"id": "c1", "user_id": USER, "run_id": RUN, "conversation_id": CONV}]
    assert repo.conversation_for_run(USER, RUN) == CONV
    assert repo.conversation_for_run(OTHER_USER, RUN) is None
    assert repo.conversation_for_run(USER, None) is None


# ── spend reservations over the fake rpcs ────────────────────────────────────


def _reserve(amount, kind="turn_call", **kw):
    return repo.rpc_spend_reserve(USER, kind=kind, amount=Decimal(amount), ttl_seconds=600,
                                  user_cap_default=Decimal("5"), global_cap_default=Decimal("50"), **kw)


def test_rpc_arguments_travel_as_strings(api):
    api.db.tables["assistant_spend_reservations"] = []
    out = _reserve("0.25", task_id=None, run_id=RUN)
    assert out["ok"] is True and out["id"]
    name, params = api.db.rpc_calls[-1]
    assert name == "assistant_spend_reserve"
    assert params["p_amount"] == "0.25" and params["p_user_cap_default"] == "5"
    assert params["p_ttl_seconds"] == 600 and params["p_run_id"] == RUN and params["p_task_id"] is None
    state = repo.rpc_spend_state(USER, Decimal("5"), Decimal("50"))
    assert api.db.rpc_calls[-1] == ("assistant_spend_state", {"p_user_id": USER, "p_user_cap_default": "5",
                                                              "p_global_cap_default": "50"})
    assert Decimal(str(state["user_held"])) == Decimal("0.25")


def test_reserve_refuses_at_the_user_cap_and_the_global_cap(api):
    api.db.tables["assistant_spend_reservations"] = []
    api.db.tables["assistant_profiles"] = [{"user_id": USER, "daily_cost_cap_usd": 1}]
    api.db.tables["llm_call_log"] = [{"id": "l1", "created_at": NOW.isoformat(), "cost_usd": 0.5,
                                      "user_id": USER, "feature": "assistant"}]
    assert _reserve("0.4")["ok"] is True
    refused = _reserve("0.1")
    assert refused == {"ok": False, "reason": "user_cap", "id": None, "state": refused["state"]}
    api.db.tables["assistant_profiles"] = [{"user_id": USER, "daily_cost_cap_usd": 100}]
    api.db.tables["assistant_settings"][0]["global_daily_cap_usd"] = 1
    assert _reserve("0.2")["reason"] == "global_cap"
    # Bad arguments are the function's check_violation.
    with pytest.raises(Exception, match="bad arguments"):
        repo.rpc_spend_reserve(USER, kind="turn_call", amount="0.1", ttl_seconds=5,
                               user_cap_default="5", global_cap_default="50")


def test_a_hold_counts_until_settled_or_expired(api):
    api.db.tables["assistant_spend_reservations"] = []
    hold = _reserve("1.5", kind="split_task", task_id=TASK)["id"]
    state = repo.rpc_spend_state(USER, "5", "50")
    assert Decimal(str(state["user_held"])) == Decimal("1.5") and Decimal(str(state["global_held"])) == Decimal("1.5")
    # A split's own calls reduce its hold instead of counting twice.
    api.db.tables["llm_call_log"] = [
        {"id": "l1", "created_at": NOW.isoformat(), "cost_usd": 0.5, "user_id": USER, "feature": "bid_split",
         "assistant_run_id": RUN, "spend_reservation_id": hold},
    ]
    state = repo.rpc_spend_state(USER, "5", "50")
    assert Decimal(str(state["user_held"])) == Decimal("1.0")
    assert Decimal(str(state["user_spent"])) == Decimal("0.5") and Decimal(str(state["global_spent"])) == Decimal("0.5")
    usage = repo.hold_usage(USER, hold)
    assert usage["status"] == "active" and usage["amount_usd"] == Decimal("1.5") and usage["logged_usd"] == Decimal("0.5")
    assert repo.hold_usage(OTHER_USER, hold) is None
    # Settle records the logged cost, once.
    assert repo.close_reservation(USER, hold, "settled") is True
    row = repo.get_reservation(USER, hold)
    assert row["status"] == "settled" and row["actual_usd"] == "0.5" and row["settled_at"]
    assert repo.close_reservation(USER, hold, "released") is False
    assert Decimal(str(repo.rpc_spend_state(USER, "5", "50")["user_held"])) == Decimal("0")
    # An expired active hold stops counting by itself.
    api.db.tables["assistant_spend_reservations"].append(reservation(id="h2", task_id="t2", amount_usd=2.0,
                                                                     expires_at=_age(minutes=1)))
    assert Decimal(str(repo.rpc_spend_state(USER, "5", "50")["user_held"])) == Decimal("0")
    with pytest.raises(ValueError):
        repo.close_reservation(USER, HOLD, "active")
    assert repo.close_reservation(USER, None, "settled") is False
    assert repo.logged_cost_for_hold(USER, None) == Decimal("0")


# ── phase 4: AI Estimating tasks (docs/ASSISTANT_PHASE4_PLAN.md 3.5.4, 3.6.2) ─

AE_RUN = "14141414-1414-4414-8414-141414141411"
AE_TASK = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
AE_APPROVAL = "99999999-9999-4999-8999-999999999998"


@pytest.fixture
def ae(api, monkeypatch):
    """A page-read task, a live delegation and a fake plans_tasks.advance
    (WI-11 fills the real one)."""
    from app.assistant import plans_tasks
    from tests.assistant_fakes import delegation

    monkeypatch.setattr(router_mod, "_ae_finalize_last", {})
    clock = {"t": 1000.0}
    monkeypatch.setattr(router_mod, "_ae_finalize_clock", lambda: clock["t"])
    api.db.tables["assistant_delegations"] = [delegation()]
    api.db.tables["assistant_tasks"].append(task(
        id=AE_TASK, kind="plans_read_pages", status="running", title="Page read on Alpha run",
        split_job_id=None, approval_id=AE_APPROVAL,
        payload={"plans_run_id": AE_RUN, "document_id": "doc-1", "pages": [14], "plans_message_id": "m-1"},
    ))
    calls: list[dict] = []
    outcome = {"changed": False, "error": None}

    def advance(user_id, t):
        calls.append({"user_id": user_id, "task": dict(t)})
        if outcome["error"]:
            raise outcome["error"]
        if outcome["changed"]:
            row = next(r for r in api.db.tables["assistant_tasks"] if r["id"] == t["id"])
            row.update(status="done", result={"cost_usd": "1.42", "pages": [14]}, finished_at=NOW.isoformat())
        return outcome["changed"]

    monkeypatch.setattr(plans_tasks, "advance", advance)

    def no_progress(t):
        raise AssertionError("an AI Estimating task has no split progress")

    monkeypatch.setattr(split_handoff, "task_progress_for", no_progress)
    return SimpleNamespace(calls=calls, outcome=outcome, clock=clock)


def test_an_ae_task_answers_its_run_and_result_and_is_finalized_lazily(api, ae):
    ae.outcome["changed"] = True
    res = api.client.get(f"/assistant/tasks/{AE_TASK}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "plans_read_pages" and body["status"] == "done"
    assert body["plans_run_id"] == AE_RUN and body["result"] == {"cost_usd": "1.42", "pages": [14]}
    assert body["job"] is None and body["files"] == [] and body["project"] == {"id": PROJECT, "number": "26.9.7130"}
    assert [c["user_id"] for c in ae.calls] == [USER] and ae.calls[0]["task"]["id"] == AE_TASK
    # Never the split repair: no continuation is enqueued for it.
    assert api.jobs == []


def test_the_lazy_finalize_runs_at_most_once_per_ten_seconds(api, ae):
    for _ in range(3):
        assert api.client.get(f"/assistant/tasks/{AE_TASK}").status_code == 200
    assert len(ae.calls) == 1
    ae.clock["t"] += 9.9
    api.client.get(f"/assistant/tasks/{AE_TASK}")
    assert len(ae.calls) == 1
    ae.clock["t"] += 0.2
    body = api.client.get(f"/assistant/tasks/{AE_TASK}").json()
    assert len(ae.calls) == 2 and body["status"] == "running" and body["result"] == {"pages": 1}


def test_the_lazy_finalize_skips_finished_tasks_and_offboarded_owners(api, ae):
    row = next(r for r in api.db.tables["assistant_tasks"] if r["id"] == AE_TASK)
    row["status"] = "done"
    api.client.get(f"/assistant/tasks/{AE_TASK}")
    assert ae.calls == []
    row["status"] = "waiting_external"
    api.db.tables["assistant_delegations"][0]["revoked_at"] = NOW.isoformat()
    assert api.client.get(f"/assistant/tasks/{AE_TASK}").status_code == 200
    assert ae.calls == []


def test_a_failing_finalize_still_answers_the_task(api, ae):
    ae.outcome["error"] = NotImplementedError("plans_tasks.advance is filled by WI-11")
    res = api.client.get(f"/assistant/tasks/{AE_TASK}")
    assert res.status_code == 200 and res.json()["status"] == "running"
    assert len(ae.calls) == 1 and api.jobs == []


def test_an_ae_run_task_links_the_run_the_composite_route_stored(api, ae):
    row = next(r for r in api.db.tables["assistant_tasks"] if r["id"] == AE_TASK)
    row.update(kind="ai_estimating_run", status="waiting_external",
               payload={"phase": "started", "run_id": AE_RUN, "hold_usd": "4.10"})
    body = api.client.get(f"/assistant/tasks/{AE_TASK}").json()
    assert body["kind"] == "ai_estimating_run" and body["plans_run_id"] == AE_RUN
    row["payload"] = {"phase": "reserved"}                      # no run yet
    ae.clock["t"] += 60
    assert api.client.get(f"/assistant/tasks/{AE_TASK}").json()["plans_run_id"] is None


def _as_assistant(api, via=RUN):
    api.client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True, aal="aal2", mfa_enrolled=True,
        via_assistant_run_id=via)


def test_by_approval_is_the_assistants_recover_read(api, ae):
    path = f"/assistant/tasks/by-approval/{AE_APPROVAL}"
    res = api.client.get(path)                                     # a human
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    _as_assistant(api)
    res = api.client.get(path)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == AE_TASK and body["plans_run_id"] == AE_RUN and body["approval_id"] == AE_APPROVAL
    # A plain read: nothing is finalized or repaired.
    assert ae.calls == [] and api.jobs == []
    for key in ("nope", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"):
        res = api.client.get(f"/assistant/tasks/by-approval/{key}")
        assert res.status_code == 404 and res.headers["X-Error-Code"] == "task_not_found"
    row = next(r for r in api.db.tables["assistant_tasks"] if r["id"] == AE_TASK)
    row["user_id"] = OTHER_USER
    assert api.client.get(path).status_code == 404
    # A split task answers its phase 2 shape.
    body = api.client.get(f"/assistant/tasks/by-approval/{APPROVAL_ID}").json()
    assert body["id"] == TASK and "plans_run_id" not in body


@pytest.mark.parametrize("status", ["failed", "canceled"])
def test_by_approval_answers_404_for_a_task_the_route_closed_without_effect(api, ae, status):
    # ROUTES-1 / SPEND-4: the recover read reports it missing, so the card fails.
    _as_assistant(api)
    row = next(r for r in api.db.tables["assistant_tasks"] if r["id"] == AE_TASK)
    row.update(status=status, failure_reason="route_failed")
    res = api.client.get(f"/assistant/tasks/by-approval/{AE_APPROVAL}")
    assert res.status_code == 404 and res.headers["X-Error-Code"] == "task_not_found"
    row.update(status="done", failure_reason=None)
    assert api.client.get(f"/assistant/tasks/by-approval/{AE_APPROVAL}").status_code == 200


def test_the_split_task_shape_is_unchanged_by_phase_4(api):
    body = api.client.get(f"/assistant/tasks/{TASK}").json()
    assert "plans_run_id" not in body and "result" not in body


def test_route_literals():
    assert router_mod.TASK_BY_APPROVAL_ROUTE == "/assistant/tasks/by-approval/{approval_id}"
    paths = [r.path for r in router_mod.router.routes]
    assert router_mod.TASK_BY_APPROVAL_ROUTE in paths and router_mod.FIND_TOOLS_ROUTE in paths


def test_a_running_page_read_reports_its_page_count_before_it_settles(api, ae):
    # Review FE-3: the headline says "Reading N pages" while the read runs;
    # no cost key, so the card never reads as done.
    body = api.client.get(f"/assistant/tasks/{AE_TASK}").json()
    assert body["status"] == "running" and body["result"] == {"pages": 1}
