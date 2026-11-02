"""The Bid File Splitter's assistant hooks (docs/ASSISTANT_PHASE2_PLAN.md
3.5, WI-3): the per-file attribution and provenance scope around a run,
the owner check, the spend hold guard, stage_copy, the continuation wake,
the bell guard, provenance on filed rows, and the human paths that leave
the assistant scope (retry, reprocess, a human drop).

Invariants pinned here:

  scope    an assistant file's run sees current_attribution() with the user,
           run and hold, and provenance.current() with the run; a manual,
           project-drop or plans_reader job's file, a file whose columns a
           human retry cleared, and a file whose task finished see neither;
  owner    an owner problem fails the file with the fixed error before any
           model call, and _after_failed files nothing (but wakes the task);
  guard    _hold_guard raises once the logged cost reaches the hold (or the
           hold closed), so a queue retry of that file makes no model call;
           naming falls back to generated names instead;
  wake     a settled assistant job enqueues assistant_continue (priority 45,
           3 attempts); a processing job, a job without assistant_task_id and
           a failing enqueue do not raise;
  bell     _notify_job_finished rings nothing for an assistant job;
  rows     _base_row carries actor_kind='assistant' inside the scope and no
           provenance key outside it;
  human    retry and reprocess clear the two file columns; a human drop
           with the assistant job's id opens a new job.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.assistant import access, llm_messages
from app.core import provenance, supabase_client
from app.services import bid_split, llm_errors, llm_queue, notifications, rfp_file_drop, rfp_split

from tests.assistant_fakes import HOLD, NOW, PROJECT, RUN, TASK, USER, FakeDB, FakeStorage, reservation, task

JOB = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1"
FILE = "f0000000-0000-4000-8000-000000000001"


def _db(*, file_over=None, job_over=None, task_over=None, hold_over=None, calls=()) -> FakeDB:
    frow = {"id": FILE, "job_id": JOB, "filename": "A-101.pdf", "status": "pending",
            "storage_path": f"bid-splits/{JOB}/source/x-A-101.pdf", "size_bytes": 100, "page_count": 3,
            "assistant_run_id": RUN, "spend_reservation_id": HOLD}
    frow.update(file_over or {})
    job = {"id": JOB, "status": "processing", "source": "project", "project_id": PROJECT, "created_by": USER,
           "assistant_task_id": TASK, "actor_kind": "assistant", "assistant_run_id": RUN,
           "spend_reservation_id": HOLD}
    job.update(job_over or {})
    return FakeDB(
        bid_split_files=[frow], bid_split_jobs=[job],
        assistant_tasks=[task(**(task_over or {}))],
        assistant_spend_reservations=[reservation(**(hold_over or {}))],
        llm_call_log=list(calls),
    )


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(db=_db(), owner_problem=None, runs=[], enqueued=[])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: state.db)
    monkeypatch.setattr(bid_split, "get_supabase", lambda: state.db)
    monkeypatch.setattr(access, "owner_problem", lambda uid, sub_app="bidding", require_writer=True: state.owner_problem)

    def probe(file_id, forced_kind=None):
        state.runs.append({"attribution": llm_messages.current_attribution(), "actor": provenance.current()})

    monkeypatch.setattr(bid_split, "_execute_run", probe)

    def enqueue(job_type, **kw):
        state.enqueued.append({"job_type": job_type, **kw})
        return {"id": "lj"}

    monkeypatch.setattr(llm_queue, "enqueue", enqueue)
    return state


def _seen(env) -> tuple:
    [run] = env.runs
    return run["attribution"], run["actor"]


# ── the scope around a run ───────────────────────────────────────────────────


def test_an_assistant_file_runs_inside_attribution_and_provenance(env):
    bid_split.execute(FILE)
    attribution, actor = _seen(env)
    assert attribution == llm_messages.Attribution(user_id=USER, run_id=RUN, reservation_id=HOLD)
    assert actor == provenance.AssistantActor(user_id=USER, run_id=RUN, approval_id=None)
    # Both scopes are gone once the run returns.
    assert llm_messages.current_attribution() is None and provenance.current() is None


@pytest.mark.parametrize("file_over, job_over, task_over", [
    ({"assistant_run_id": None, "spend_reservation_id": None}, None, None),     # a human retry cleared it
    (None, {"assistant_task_id": None}, None),                                  # a human project drop job
    (None, {"source": "manual", "project_id": None, "actor_kind": "human"}, None),
    (None, {"source": "plans_reader", "actor_kind": "human", "assistant_task_id": None}, None),
    (None, None, {"status": "done"}),                                           # the task already finished
    (None, None, {"status": "failed"}),
])
def test_every_other_run_is_unscoped(env, file_over, job_over, task_over):
    env.db = _db(file_over=file_over, job_over=job_over, task_over=task_over)
    bid_split.execute(FILE)
    assert _seen(env) == (None, None)


def test_a_read_failure_leaves_the_run_unscoped(env, monkeypatch):
    def broken():
        raise RuntimeError("PostgREST down")

    monkeypatch.setattr(bid_split, "get_supabase", broken)
    bid_split.execute(FILE)
    assert _seen(env) == (None, None)


@pytest.mark.parametrize("problem", [access.OWNER_CHANGED, access.FEATURE_OFF])
def test_an_owner_problem_fails_the_file_before_any_model_call(env, problem):
    env.owner_problem = problem
    with pytest.raises(bid_split.AssistantAccessRevoked) as exc:
        bid_split.execute(FILE)
    assert env.runs == []
    assert str(exc.value) == "assistant access was revoked"
    # The queue stores the fixed sentence and does not retry it (a ValueError).
    assert llm_errors.user_message(exc.value, "claude-opus-5") == "assistant access was revoked"
    assert not llm_errors.is_transient(exc.value)


def test_a_bad_forced_kind_is_refused_before_any_read(monkeypatch):
    monkeypatch.setattr(bid_split, "get_supabase", lambda: pytest.fail("no read before the argument check"))
    with pytest.raises(ValueError):
        bid_split.execute(FILE, forced_kind="rfp")


def test_the_scope_never_nests(env):
    with provenance.acting_as_assistant(USER, "another-run"):
        bid_split.execute(FILE)
    attribution, actor = _seen(env)
    assert attribution is None and actor.run_id == "another-run"


# ── the hold guard ───────────────────────────────────────────────────────────


def _log(cost):
    return {"id": f"c-{cost}", "user_id": USER, "cost_usd": cost, "spend_reservation_id": HOLD,
            "feature": "bid_split", "assistant_run_id": RUN, "created_at": NOW.isoformat()}


def test_the_guard_is_a_no_op_outside_the_scope(env):
    env.db = _db(calls=[_log(9)])
    bid_split._hold_guard()


def test_the_guard_lets_calls_run_while_the_hold_has_room(env):
    env.db = _db(calls=[_log(0.1)])
    with llm_messages.attributed(USER, RUN, reservation_id=HOLD):
        bid_split._hold_guard()


@pytest.mark.parametrize("calls, hold_over", [([_log(0.25), _log(0.15)], None), ([], {"status": "settled"}),
                                               ([], {"status": "released"})])
def test_the_guard_refuses_once_the_hold_is_used_up_or_closed(env, calls, hold_over):
    env.db = _db(calls=calls, hold_over=hold_over)
    with llm_messages.attributed(USER, RUN, reservation_id=HOLD):
        with pytest.raises(bid_split.SplitHoldExhausted, match="used up"):
            bid_split._hold_guard()


def test_a_zero_hold_lets_calls_run_on_the_self_hosted_route(env):
    """A 0.00 hold (self-hosted estimate) is admitted by design; the guard
    must not read 0 >= 0 as used up."""
    env.db = _db(hold_over={"amount_usd": 0})
    with llm_messages.attributed(USER, RUN, reservation_id=HOLD):
        bid_split._hold_guard()


@pytest.mark.parametrize("status", ["released", "settled", "expired"])
def test_a_closed_zero_hold_still_refuses(env, status):
    env.db = _db(hold_over={"amount_usd": 0, "status": status})
    with llm_messages.attributed(USER, RUN, reservation_id=HOLD):
        with pytest.raises(bid_split.SplitHoldExhausted):
            bid_split._hold_guard()


def test_an_expired_active_hold_refuses(env):
    """The admission drops a hold past expires_at, so the run may not keep
    spending it."""
    env.db = _db(hold_over={"expires_at": (NOW - timedelta(minutes=1)).isoformat()})
    with llm_messages.attributed(USER, RUN, reservation_id=HOLD):
        with pytest.raises(bid_split.SplitHoldExhausted):
            bid_split._hold_guard()


def test_an_unreadable_expiry_never_blocks(env):
    env.db = _db(hold_over={"expires_at": "not-a-date"})
    with llm_messages.attributed(USER, RUN, reservation_id=HOLD):
        bid_split._hold_guard()


def test_a_guard_read_failure_lets_the_call_run(env, monkeypatch):
    from app.assistant import repo

    monkeypatch.setattr(repo, "hold_usage", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    with llm_messages.attributed(USER, RUN, reservation_id=HOLD):
        bid_split._hold_guard()


def test_a_retried_file_makes_no_model_call_once_the_hold_is_used_up(env, monkeypatch):
    """The real run path: execute -> scope -> _triage. The first call site
    trips the guard; the model is never called."""
    from app.services import llm, pdf_split

    env.db = _db(calls=[_log(0.4)])
    monkeypatch.setattr(bid_split, "_execute_run", lambda fid, fk=None: bid_split._triage(
        b"%PDF-", "A-101.pdf", 3, bid_split.get_settings(), [1], "prompt"))
    monkeypatch.setattr(pdf_split, "render_pages", lambda *a, **k: [b"img"])
    monkeypatch.setattr(llm, "complete_images_json", lambda *a, **k: pytest.fail("no model call"))
    with pytest.raises(bid_split.SplitHoldExhausted):
        bid_split.execute(FILE)
    assert not llm_errors.is_transient(bid_split.SplitHoldExhausted("x"))


def test_naming_falls_back_to_generated_names_when_the_hold_is_used_up(env, monkeypatch):
    from app.services import llm

    env.db = _db(calls=[_log(0.4)])
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: pytest.fail("no model call"))
    seg = {"category": "electrical_drawings", "other_type": None, "page_start": 1, "page_end": 2,
           "titles": ["E-1"]}
    with llm_messages.attributed(USER, RUN, reservation_id=HOLD):
        calls, _ = bid_split._name_segments([seg], "A-101.pdf", bid_split.get_settings())
    assert seg["name"] and calls == 1


# ── _after_failed and the wake ───────────────────────────────────────────────


def test_after_failed_files_whole_inside_the_scope_and_wakes_the_task(env, monkeypatch):
    seen = []
    monkeypatch.setattr(rfp_split, "file_project_job_whole_quietly",
                        lambda fid, reason, sb=None, **k: seen.append((reason, provenance.current(),
                                                                       llm_messages.current_attribution())))
    env.db.tables["bid_split_jobs"][0]["status"] = "done_with_errors"
    bid_split._after_failed(FILE, "the run failed", JOB)
    [(reason, actor, attribution)] = seen
    assert reason == "the run failed" and actor.run_id == RUN and attribution.reservation_id == HOLD
    assert [e["target_id"] for e in env.enqueued] == [TASK]


def test_after_failed_files_nothing_on_an_owner_problem(env, monkeypatch):
    env.owner_problem = access.OWNER_CHANGED
    monkeypatch.setattr(rfp_split, "file_project_job_whole_quietly", lambda *a, **k: pytest.fail("filed"))
    env.db.tables["bid_split_jobs"][0]["status"] = "failed"
    bid_split._after_failed(FILE, "assistant access was revoked", JOB)
    assert [e["target_id"] for e in env.enqueued] == [TASK]


@pytest.mark.parametrize("reason", [access.OWNER_CHANGED, access.FEATURE_OFF])
def test_a_file_canceled_after_an_owner_stop_is_not_filed_whole(env, monkeypatch, reason):
    """The continuation's owner stop fails the task; a queued file it then
    cancels runs _after_failed with the task closed (unscoped), and must
    not land in the project."""
    env.db = _db(task_over={"status": "failed", "failure_reason": reason})
    monkeypatch.setattr(rfp_split, "file_project_job_whole_quietly", lambda *a, **k: pytest.fail("filed"))
    env.db.tables["bid_split_jobs"][0]["status"] = "failed"
    bid_split._after_failed(FILE, "Canceled", JOB)
    assert [e["target_id"] for e in env.enqueued] == [TASK]


@pytest.mark.parametrize("task_over", [{"status": "failed", "failure_reason": "timed_out"},
                                       {"status": "done"}])
def test_other_closed_tasks_still_file_whole(env, monkeypatch, task_over):
    env.db = _db(task_over=task_over)
    seen = []
    monkeypatch.setattr(rfp_split, "file_project_job_whole_quietly", lambda *a, **k: seen.append(a[0]))
    bid_split._after_failed(FILE, "boom", JOB)
    assert seen == [FILE]


def test_mark_failed_runs_the_whole_filing_in_the_scope(env, monkeypatch):
    """The queue's terminal mark runs _after_failed outside execute."""
    seen = []
    monkeypatch.setattr(rfp_split, "file_project_job_whole_quietly",
                        lambda fid, reason, sb=None, **k: seen.append(provenance.current()))
    monkeypatch.setattr(bid_split, "refresh_job", lambda job_id, _recheck=True: None)
    bid_split._mark(FILE, status="failed", error="boom")
    assert seen and seen[0].run_id == RUN


def test_after_done_wakes_the_task_after_the_resync(env, monkeypatch):
    order = []
    monkeypatch.setattr(rfp_split, "resync_after_run", lambda fid: order.append("resync"))
    monkeypatch.setattr(bid_split, "_wake_assistant_task", lambda job_id: order.append(("wake", job_id)))
    bid_split._after_done(FILE, JOB)
    assert order == ["resync", ("wake", JOB)]


def test_wake_enqueues_the_continuation_for_a_settled_assistant_job(env):
    env.db.tables["bid_split_jobs"][0]["status"] = "done"
    bid_split._wake_assistant_task(JOB)
    assert env.enqueued == [{
        "job_type": llm_queue.JOB_ASSISTANT_CONTINUE, "target_id": TASK,
        "payload": {"task_id": TASK, "user_id": USER}, "created_by": USER, "priority": 45,
        "max_attempts_override": 3,
    }]


@pytest.mark.parametrize("job_over", [{"status": "processing"}, {"status": "done", "assistant_task_id": None},
                                      {"status": "done", "created_by": None}])
def test_wake_does_nothing_otherwise(env, job_over):
    env.db.tables["bid_split_jobs"][0].update(job_over)
    bid_split._wake_assistant_task(JOB)
    bid_split._wake_assistant_task(None)
    bid_split._wake_assistant_task("missing")
    assert env.enqueued == []


def test_a_wake_absorbed_by_a_running_continuation_wakes_again(env, monkeypatch):
    """The running continuation may have read before this file's rows
    landed: the hook waits for it to end and enqueues once more."""
    env.db.tables["bid_split_jobs"][0]["status"] = "done"
    answers = [{"id": "c1", "status": "running"}, {"id": "c1", "status": "running"}, {"id": "c2", "status": "queued"}]
    calls, naps = [], []

    def enqueue(job_type, **kw):
        calls.append(kw["target_id"])
        return answers.pop(0)

    monkeypatch.setattr(llm_queue, "enqueue", enqueue)
    monkeypatch.setattr(bid_split.time, "sleep", lambda s: naps.append(s))
    bid_split._wake_assistant_task(JOB)
    assert calls == [TASK, TASK, TASK] and naps == [0.5, 1.0] and answers == []


def test_a_wake_gives_up_after_the_bounded_retries(env, monkeypatch):
    env.db.tables["bid_split_jobs"][0]["status"] = "done"
    calls = []
    monkeypatch.setattr(llm_queue, "enqueue", lambda jt, **kw: calls.append(1) or {"id": "c1", "status": "running"})
    monkeypatch.setattr(bid_split.time, "sleep", lambda s: None)
    bid_split._wake_assistant_task(JOB)
    assert len(calls) == 1 + len(bid_split.ASSISTANT_WAKE_RETRY_DELAYS)


def test_wake_never_raises(env, monkeypatch):
    env.db.tables["bid_split_jobs"][0]["status"] = "done"
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue down")))
    bid_split._wake_assistant_task(JOB)


def test_the_wake_constants_match_the_continuation():
    from app.assistant import continue_job

    assert bid_split.ASSISTANT_CONTINUE_PRIORITY == continue_job.PRIORITY
    assert bid_split.ASSISTANT_CONTINUE_MAX_ATTEMPTS == continue_job.MAX_ATTEMPTS
    assert bid_split.ASSISTANT_OPEN_TASK_STATUSES == ("pending", "waiting_external", "running")


def test_the_splitter_bell_skips_an_assistant_job(monkeypatch):
    rung = []
    monkeypatch.setattr(notifications, "notify_user", lambda *a, **k: rung.append(a))
    bid_split._notify_job_finished({"id": JOB, "created_by": USER, "source": "project", "assistant_task_id": TASK},
                                   "done", [{"status": "done"}])
    assert rung == []
    bid_split._notify_job_finished({"id": JOB, "created_by": USER, "source": "manual", "assistant_task_id": None},
                                   "done", [{"status": "done"}])
    assert len(rung) == 1


# ── stage_copy ───────────────────────────────────────────────────────────────


def test_stage_copy_copies_and_stamps_the_file(monkeypatch):
    db = FakeDB(bid_split_files=[], bid_split_jobs=[{"id": JOB, "status": "done", "file_count": 0}])
    store = FakeStorage().install(monkeypatch)
    store.objects["assistant-files/u/x-A.pdf"] = b"%PDF-1"
    frow = bid_split.stage_copy(db, {"id": JOB}, "A.pdf", "assistant-files/u/x-A.pdf", 6, 3,
                                assistant_run_id=RUN, spend_reservation_id=HOLD)
    assert frow["storage_path"].startswith(f"bid-splits/{JOB}/source/") and store.objects[frow["storage_path"]]
    assert frow["assistant_run_id"] == RUN and frow["spend_reservation_id"] == HOLD
    assert frow["status"] == "pending" and frow["page_count"] == 3 and frow["size_bytes"] == 6
    job = db.tables["bid_split_jobs"][0]
    assert job["file_count"] == 1 and job["status"] == "processing" and job["completed_at"] is None


def test_stage_copy_removes_the_copy_when_the_insert_fails(monkeypatch):
    db = FakeDB(bid_split_files=[], bid_split_jobs=[{"id": JOB}])
    store = FakeStorage().install(monkeypatch)
    store.objects["src"] = b"%PDF-1"
    real_table = db.table

    def failing(name):
        q = real_table(name)
        if name == "bid_split_files":
            q.insert = lambda payload: (_ for _ in ()).throw(RuntimeError("insert failed"))
        return q

    monkeypatch.setattr(db, "table", failing)
    with pytest.raises(RuntimeError):
        bid_split.stage_copy(db, {"id": JOB}, "A.pdf", "src", 6, 3, assistant_run_id=RUN, spend_reservation_id=HOLD)
    assert list(store.objects) == ["src"] and len(store.deleted) == 1


# ── provenance on filed rows ─────────────────────────────────────────────────


def test_base_row_carries_provenance_only_inside_the_scope():
    args = (PROJECT, None, {"id": FILE}, "key", "A.pdf", "application/pdf", 10, None)
    outside = rfp_split._base_row(*args)
    assert "actor_kind" not in outside and "assistant_run_id" not in outside
    with provenance.acting_as_assistant(USER, RUN):
        inside = rfp_split._base_row(*args)
    assert inside["actor_kind"] == "assistant" and inside["assistant_run_id"] == RUN
    assert {k: v for k, v in inside.items() if k not in ("actor_kind", "assistant_run_id")} == outside


def test_a_run_files_assistant_rows_and_a_human_retry_files_human_rows(env, monkeypatch):
    """resync_after_run's rows inherit the scope: inside an assistant run
    they carry the run, after a human retry cleared the columns they carry
    no provenance (the column default records a human)."""
    rows = []

    def run(file_id, forced_kind=None):
        rows.append(rfp_split._base_row(PROJECT, None, {"id": file_id}, "k", "A.pdf", "application/pdf", 1, None))

    monkeypatch.setattr(bid_split, "_execute_run", run)
    bid_split.execute(FILE)
    monkeypatch.setattr(bid_split, "refresh_job", lambda job_id, _recheck=True: None)
    bid_split._mark(FILE, status="pending", error=None, assistant_run_id=None, spend_reservation_id=None)
    assert env.db.tables["bid_split_files"][0]["assistant_run_id"] is None
    bid_split.execute(FILE)
    assert rows[0]["actor_kind"] == "assistant" and "actor_kind" not in rows[1]


# ── the human paths ──────────────────────────────────────────────────────────


def test_retry_and_reprocess_clear_the_assistant_columns(monkeypatch):
    from app.routers import bid_splitter as bs
    from tests.test_bid_split_corrections import _WRITER, FILE_ROW, _install_reprocess, _tables
    from tests.test_bid_split_corrections import FakeDB as CorrectionsDB

    for status, call, kind in (("failed", bs.retry_file, None), ("done", bs.reprocess_file, "mixed")):
        row = {**FILE_ROW, "status": status, "assistant_run_id": RUN, "spend_reservation_id": HOLD}
        if kind:
            row["file_kind"] = kind
        db = CorrectionsDB(_tables(bid_split_files=[row]))
        _install_reprocess(monkeypatch, db)
        call("f1", None, user=_WRITER)
        updated = db.tables["bid_split_files"][0]
        assert updated["status"] == "pending"
        assert updated["assistant_run_id"] is None and updated["spend_reservation_id"] is None


def test_a_human_drop_never_joins_an_assistant_job():
    now = NOW
    job = {"id": JOB, "project_id": PROJECT, "source": "project", "status": "processing",
           "assistant_task_id": TASK}
    assert rfp_file_drop._reusable(job, PROJECT, now) is False
    assert rfp_file_drop._reusable({**job, "assistant_task_id": None}, PROJECT, now) is True
    settled = {**job, "assistant_task_id": None, "status": "done",
               "completed_at": (now - timedelta(seconds=30)).isoformat()}
    assert rfp_file_drop._reusable(settled, PROJECT, now) is True


def test_a_human_drop_with_the_assistant_jobs_id_opens_a_new_job(monkeypatch):
    db = FakeDB(bid_split_jobs=[{"id": JOB, "project_id": PROJECT, "source": "project", "status": "processing",
                                 "assistant_task_id": TASK}], bid_split_files=[])
    from app.services import llm

    monkeypatch.setattr(llm, "active_model", lambda f, s=None: "m")
    job, opened = rfp_file_drop.drop_job(db, PROJECT, USER, JOB, bid_split.get_settings())
    assert opened is True and job["id"] != JOB and job.get("assistant_task_id") is None
    assert "actor_kind" not in job


def test_drop_job_extra_columns_land_on_a_new_job(monkeypatch):
    db = FakeDB(bid_split_jobs=[], bid_split_files=[])
    from app.services import llm

    monkeypatch.setattr(llm, "active_model", lambda f, s=None: "m")
    with provenance.acting_as_assistant(USER, RUN, "approval"):
        job, opened = rfp_file_drop.drop_job(db, PROJECT, USER, None, bid_split.get_settings(),
                                             extra={"assistant_task_id": TASK, "spend_reservation_id": HOLD})
    assert opened is True and job["assistant_task_id"] == TASK and job["spend_reservation_id"] == HOLD
    assert job["actor_kind"] == "assistant" and job["source"] == "project" and job["project_id"] == PROJECT


def test_the_hold_amounts_compare_as_decimals(env):
    """hold_usage returns Decimal amounts: 0.40 logged on a 0.4 hold trips."""
    from app.assistant import repo

    env.db = _db(calls=[_log(0.4)], hold_over={"amount_usd": "0.40"})
    usage = repo.hold_usage(USER, HOLD)
    assert usage["logged_usd"] == Decimal("0.4") and usage["amount_usd"] == Decimal("0.40")
