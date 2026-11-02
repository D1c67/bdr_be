"""Finalize for the assistant's AI Estimating tasks
(app/assistant/plans_tasks.py; docs/ASSISTANT_PHASE4_PLAN.md 3.5.4, 3.6.4),
over the FakeDB with fake AE state reads.

Invariants pinned here:

  page read   a finished chat answer posts ONE server-built message
              (server_data and tainted, dashes and control characters gone,
              clipped), settles the hold at the chat cost, finishes the task
              with {cost_usd, pages} and rings one bell; a second advance
              changes nothing; the answer is read with the owner's live role
              (the service redacts a pricing answer for a role that cannot
              see pricing); a failed answer or a timeout closes the hold
              RELEASED with the actual cost so far (it counts toward
              today's caps);
  run         a terminal run settles at its cost (released with that actual
              when failed or canceled, and user_spent rises either way),
              posts the fixed sentence with a task_result block and a
              citation, finishes the task; a run still going stays open at
              any age, and settles later even after the hold's TTL;
  stop        stop_task is fenced and idempotent, and with bell False and no
              text (offboarding) posts nothing;
  source      the module names no client, no em dash.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.assistant import plans_tasks as pt
from app.assistant import repo, spend
from app.core import supabase_client
from app.services import assistant_ai_estimate, llm_queue, notifications
from tests.assistant_fakes import (
    CONV,
    HOLD,
    NOW,
    TASK,
    USER,
    FakeDB,
    conversation,
    delegation,
    profile,
    reservation,
    settings_row,
    task,
)

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
CHAT_MSG = "98989898-9898-4898-8898-989898989891"
RUN_ID = "14141414-1414-4414-8414-141414141411"
DOC = "15151515-1515-4515-8515-151515151511"


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def pages_task(**over) -> dict:
    base = task(
        kind="plans_read_pages", status="running", title="Page read on Substation set",
        project_id=None, split_job_id=None, run_id=None,
        payload={"plans_run_id": RUN_ID, "document_id": DOC, "pages": [14, 15], "thread_id": "t1",
                 "plans_message_id": CHAT_MSG},
        created_at=_ago(minutes=5), updated_at=_ago(minutes=5),
    )
    base.update(over)
    return base


def run_task(**over) -> dict:
    base = task(
        kind="ai_estimating_run", status="waiting_external", title="AI Estimating run for 26.10.7212",
        split_job_id=None, run_id=RUN_ID, payload={"phase": "started", "run_id": RUN_ID},
        created_at=_ago(minutes=30), updated_at=_ago(minutes=30),
    )
    base.update(over)
    return base


@pytest.fixture
def world(monkeypatch):
    db = FakeDB(
        now=NOW, profiles=[profile()], assistant_delegations=[delegation()], assistant_settings=[settings_row()],
        assistant_profiles=[], assistant_conversations=[conversation()], assistant_messages=[],
        assistant_tasks=[], assistant_spend_reservations=[], llm_call_log=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    bells: list[dict] = []
    monkeypatch.setattr(notifications, "notify_user", lambda user_id, project_id, type_, message, **kw:
                        bells.append({"user_id": user_id, "type": type_, "message": message, **kw}))
    state = {
        "chat": {"status": "running", "content": "", "cost_usd": "0.00", "error": None},
        "run": {"status": "reading", "cost_usd": "0.00", "finished_at": None, "error": None},
        "chat_calls": [],
        "run_calls": [],
    }

    def chat_message_state(sb, message_id, *, viewer_role):
        state["chat_calls"].append({"sb": sb, "message_id": message_id, "viewer_role": viewer_role})
        return dict(state["chat"])

    def run_state(sb, run_id):
        state["run_calls"].append({"sb": sb, "run_id": run_id})
        return dict(state["run"])

    monkeypatch.setattr(assistant_ai_estimate, "chat_message_state", chat_message_state)
    monkeypatch.setattr(assistant_ai_estimate, "run_state", run_state)
    # llm_queue binds its own client: fake the two calls the timeout path makes.
    db.tables["llm_jobs"] = []
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
    state["canceled"] = canceled
    return SimpleNamespace(db=db, bells=bells, state=state)


def _seed(world, row: dict, kind: str) -> dict:
    world.db.tables["assistant_tasks"].append(row)
    world.db.tables["assistant_spend_reservations"].append(reservation(kind=kind, amount_usd=2.13))
    return row


def _task(world) -> dict:
    return next(r for r in world.db.tables["assistant_tasks"] if r["id"] == TASK)


def _hold(world) -> dict:
    return next(r for r in world.db.tables["assistant_spend_reservations"] if r["id"] == HOLD)


def _messages(world) -> list[dict]:
    return world.db.tables["assistant_messages"]


# ── plans_read_pages ─────────────────────────────────────────────────────────


def test_a_finished_page_read_posts_once_settles_and_finishes(world):
    _seed(world, pages_task(), "plans_pages")
    world.state["chat"] = {"status": "done", "cost_usd": "1.43", "error": None,
                           "content": f"Panel E-201 is a 480V board {EM_DASH} see detail 3.\x07 The feeder is 4 in conduit."}
    assert pt.advance(USER, pages_task()) is True

    [msg] = _messages(world)
    assert msg["id"] == pt.message_id_for(TASK, pt.KIND_PAGES)
    assert msg["server_data"] is True and msg["tainted"] is True and msg["role"] == "assistant"
    assert msg["status"] == "done" and msg["conversation_id"] == CONV
    assert msg["content"].startswith("Answer from the page read (pages 14, 15):\n\n")
    assert EM_DASH not in msg["content"] and "\x07" not in msg["content"]
    assert "Panel E-201 is a 480V board, see detail 3." in msg["content"]
    assert [b["type"] for b in msg["blocks"]] == ["text", "task_result"]
    assert msg["blocks"][1] == {"type": "task_result", "task_id": TASK}

    assert _hold(world)["status"] == "settled" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("1.43")
    row = _task(world)
    assert row["status"] == "done" and row["failure_reason"] is None and row["finished_at"]
    assert row["result"] == {"cost_usd": "1.43", "pages": 2}
    assert row["result_message_id"] == msg["id"]
    assert [b["type"] for b in world.bells] == [pt.BELL_FINISHED]
    assert world.bells[0]["mirror_email"] is False
    assert world.bells[0]["metadata"]["task_id"] == TASK


def test_a_second_advance_changes_nothing(world):
    _seed(world, pages_task(), "plans_pages")
    world.state["chat"] = {"status": "done", "cost_usd": "0.50", "content": "Yes.", "error": None}
    assert pt.advance(USER, pages_task()) is True
    assert pt.advance(USER, pages_task()) is False
    assert len(_messages(world)) == 1 and len(world.bells) == 1


def test_a_replay_after_a_crash_between_the_message_and_the_task_completes_without_a_second_message(world):
    _seed(world, pages_task(), "plans_pages")
    world.state["chat"] = {"status": "done", "cost_usd": "0.50", "content": "Yes.", "error": None}
    # The first attempt posted the message and died before the settle.
    world.db.tables["assistant_messages"].append({
        "id": pt.message_id_for(TASK, pt.KIND_PAGES), "conversation_id": CONV, "user_id": USER,
        "role": "assistant", "status": "done", "content": "earlier", "blocks": [], "server_data": True,
        "tainted": True, "created_at": NOW.isoformat(),
    })
    assert pt.advance(USER, pages_task()) is True
    assert len(_messages(world)) == 1 and _messages(world)[0]["content"] == "earlier"
    assert _hold(world)["status"] == "settled" and _task(world)["status"] == "done"


def test_the_answer_is_read_with_the_owners_live_role_and_the_service_redaction_is_what_is_posted(world):
    world.db.tables["profiles"][0]["role"] = "accountant"
    _seed(world, pages_task(), "plans_pages")
    redacted = "This answer used pricing information and is not shown for your role."
    world.state["chat"] = {"status": "done", "cost_usd": "0.80", "content": redacted, "error": None}
    assert pt.advance(USER, pages_task()) is True
    [call] = world.state["chat_calls"]
    assert call == {"sb": None, "message_id": CHAT_MSG, "viewer_role": "accountant"}
    assert redacted in _messages(world)[0]["content"]


def test_a_role_that_cannot_be_read_is_passed_as_an_empty_string_so_the_answer_redacts(world):
    world.db.tables["profiles"].clear()
    _seed(world, pages_task(), "plans_pages")
    pt.advance(USER, pages_task())
    assert world.state["chat_calls"][0]["viewer_role"] == ""


def test_a_long_answer_is_clipped_and_an_empty_one_gets_a_fixed_line(world):
    _seed(world, pages_task(), "plans_pages")
    world.state["chat"] = {"status": "done", "cost_usd": "0.10", "content": "x" * 9000, "error": None}
    pt.advance(USER, pages_task())
    content = _messages(world)[0]["content"]
    assert len(content) <= 4000 and content.count("x") == pt.ANSWER_MAX_CHARS
    assert pt.clean_answer("") == "" and pt.clean_answer(None) == ""
    assert pt.clean_answer(f"a {EN_DASH} b {EM_DASH} c") == "a, b, c"


def test_a_page_read_still_running_inside_its_window_is_left_alone(world):
    _seed(world, pages_task(), "plans_pages")
    world.state["chat"] = {"status": "running", "cost_usd": "0.00", "content": "", "error": None}
    assert pt.advance(USER, pages_task()) is False
    assert _task(world)["status"] == "running" and _hold(world)["status"] == "active"
    assert _messages(world) == [] and world.bells == []


def test_a_failed_page_read_releases_the_hold_with_its_actual_and_the_cost_counts(world):
    _seed(world, pages_task(), "plans_pages")
    world.state["chat"] = {"status": "failed", "cost_usd": "0.90", "content": "", "error": "model said no <b>"}
    before = spend.state(USER)
    assert pt.advance(USER, pages_task()) is True
    row = _task(world)
    assert (row["status"], row["failure_reason"]) == ("failed", pt.FAILED_PAGE_READ)
    assert _hold(world)["status"] == "released" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("0.90")
    after = spend.state(USER)
    # The reservation's hold stops counting, the actual 0.90 counts as spent.
    assert after.user_spent - before.user_spent == Decimal("0.90")
    [msg] = _messages(world)
    assert msg["content"] == pt.TEXT_PAGES_FAILED and msg["server_data"] is False and msg["tainted"] is False
    assert "model said no" not in msg["content"]
    assert [b["type"] for b in world.bells] == [pt.BELL_FAILED]


def test_a_page_read_past_its_timeout_fails_timed_out_and_releases_with_the_cost_read_now(world, monkeypatch):
    _seed(world, pages_task(created_at=_ago(hours=3), updated_at=_ago(hours=3)), "plans_pages")
    world.state["chat"] = {"status": "running", "cost_usd": "0.35", "content": "", "error": None}
    assert pt.advance(USER, pages_task()) is True
    row = _task(world)
    assert (row["status"], row["failure_reason"]) == ("failed", pt.FAILED_TIMED_OUT)
    assert _hold(world)["status"] == "released" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("0.35")
    assert _messages(world)[0]["content"] == pt.TEXT_PAGES_TIMED_OUT


def _chat_job(world, status):
    world.db.tables["llm_jobs"].append({"id": "chat-job", "job_type": llm_queue.JOB_PLANS_READER_CHAT,
                                        "target_id": CHAT_MSG, "status": status, "payload": {}})


def test_a_timed_out_page_read_cancels_its_queued_chat_job_before_closing_the_hold(world):
    # SPEND-3: a backed-up queue never runs the paid read after its hold closed.
    _seed(world, pages_task(created_at=_ago(hours=3), updated_at=_ago(hours=3)), "plans_pages")
    world.state["chat"] = {"status": "pending", "cost_usd": "0.00", "content": "", "error": None}
    _chat_job(world, "queued")
    assert pt.advance(USER, pages_task()) is True
    assert world.state["canceled"] == ["chat-job"] and world.db.tables["llm_jobs"][0]["status"] == "canceled"
    assert _task(world)["failure_reason"] == pt.FAILED_TIMED_OUT
    assert _hold(world)["status"] == "released" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("0")


def test_a_timed_out_page_read_never_cancels_a_running_chat_job(world):
    _seed(world, pages_task(created_at=_ago(hours=3), updated_at=_ago(hours=3)), "plans_pages")
    world.state["chat"] = {"status": "running", "cost_usd": "0.35", "content": "", "error": None}
    _chat_job(world, "running")
    assert pt.advance(USER, pages_task()) is True
    assert world.state["canceled"] == [] and world.db.tables["llm_jobs"][0]["status"] == "running"
    assert _hold(world)["status"] == "released" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("0.35")


def test_a_timed_out_page_read_keeps_its_hold_when_the_queue_cannot_be_read(world, monkeypatch):
    _seed(world, pages_task(created_at=_ago(hours=3), updated_at=_ago(hours=3)), "plans_pages")
    monkeypatch.setattr(llm_queue, "active_job", lambda *a: (_ for _ in ()).throw(RuntimeError("db")))
    assert pt.advance(USER, pages_task()) is False
    assert _task(world)["status"] == "running" and _hold(world)["status"] == "active"


def test_a_sent_but_unrecorded_answer_is_adopted_and_settled_at_its_cost(world, monkeypatch):
    # SPEND-1: the route sent the message, then crashed before recording it.
    seen = []

    def adopt(sb, user_id, task):
        seen.append((sb, user_id, task["id"]))
        return CHAT_MSG

    monkeypatch.setattr(assistant_ai_estimate, "adopt_page_read_message", adopt)
    payload = {"plans_run_id": RUN_ID, "document_id": DOC, "pages": [14], "plans_message_id": None,
               "question": "What does sheet E-201 show?"}
    _seed(world, pages_task(payload=payload, created_at=_ago(hours=5), updated_at=_ago(hours=5)), "plans_pages")
    world.state["chat"] = {"status": "done", "cost_usd": "1.42", "content": "Panel 1HD-21 is 400A.", "error": None}
    assert pt.advance(USER, pages_task(payload=payload)) is True
    assert seen == [(None, USER, TASK)]
    row = _task(world)
    assert row["status"] == "done" and row["payload"]["plans_message_id"] == CHAT_MSG
    assert _hold(world)["status"] == "settled" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("1.42")


def test_a_page_read_with_nothing_to_adopt_still_times_out_at_zero(world, monkeypatch):
    monkeypatch.setattr(assistant_ai_estimate, "adopt_page_read_message", lambda sb, user_id, task: None)
    payload = {"plans_run_id": RUN_ID, "document_id": DOC, "pages": [14], "question": "What does E-201 show?"}
    _seed(world, pages_task(payload=payload, created_at=_ago(hours=5), updated_at=_ago(hours=5)), "plans_pages")
    assert pt.advance(USER, pages_task(payload=payload)) is True
    assert _task(world)["failure_reason"] == pt.FAILED_TIMED_OUT
    assert Decimal(str(_hold(world)["actual_usd"])) == Decimal("0")


def test_the_page_read_adoption_finds_the_answer_after_this_cards_exact_text(world):
    db = world.db
    db.tables["plans_reader_chat_threads"] = [{"id": "th1", "run_id": RUN_ID, "created_by": USER,
                                               "title": "Assistant page reads", "created_at": _ago(days=1)}]
    question = "What does sheet E-201 show for panel 1HD-21?"
    text = assistant_ai_estimate.page_read_content(DOC, [14], question)
    db.tables["plans_reader_chat_messages"] = [
        {"id": "u0", "thread_id": "th1", "role": "user", "created_by": USER, "content": text,
         "created_at": _ago(hours=2)},
        {"id": "a0", "thread_id": "th1", "role": "assistant", "created_by": USER, "content": "",
         "created_at": _ago(hours=2)},
        {"id": "u1", "thread_id": "th1", "role": "user", "created_by": USER, "content": text,
         "created_at": _ago(minutes=4)},
        {"id": "a1", "thread_id": "th1", "role": "assistant", "created_by": USER, "content": "",
         "created_at": _ago(minutes=4)},
    ]
    payload = {"plans_run_id": RUN_ID, "document_id": DOC, "pages": [14], "question": question}
    found = assistant_ai_estimate.adopt_page_read_message(db, USER, pages_task(payload=payload))
    assert found == "a1"   # the answer of an earlier card (before the task) is never taken
    assert assistant_ai_estimate.adopt_page_read_message(db, USER, pages_task(payload={**payload, "question": None})) is None
    assert assistant_ai_estimate.adopt_page_read_message(
        db, USER, pages_task(payload={**payload, "question": "Another question about E-201?"})) is None


def test_a_page_read_whose_chat_message_vanished_fails_plainly(world):
    _seed(world, pages_task(), "plans_pages")
    world.state["chat"] = {"status": "missing", "cost_usd": "0.00", "content": "", "error": None}
    assert pt.advance(USER, pages_task()) is True
    assert _task(world)["failure_reason"] == pt.FAILED_PAGE_MISSING
    assert _hold(world)["status"] == "released"


def test_a_page_read_with_no_stored_message_waits_then_times_out(world):
    _seed(world, pages_task(payload={"pages": [3]}), "plans_pages")
    assert pt.advance(USER, pages_task()) is False
    assert world.state["chat_calls"] == []
    world.db.tables["assistant_tasks"][0]["created_at"] = _ago(hours=5)
    assert pt.advance(USER, pages_task()) is True
    assert _task(world)["failure_reason"] == pt.FAILED_TIMED_OUT
    assert Decimal(str(_hold(world)["actual_usd"])) == Decimal("0")


def test_a_task_without_a_conversation_still_settles_and_finishes(world):
    _seed(world, pages_task(conversation_id=None), "plans_pages")
    world.state["chat"] = {"status": "done", "cost_usd": "0.20", "content": "ok", "error": None}
    assert pt.advance(USER, pages_task()) is True
    assert _messages(world) == [] and _task(world)["status"] == "done" and _hold(world)["status"] == "settled"
    assert _task(world)["result_message_id"] is None


# ── ai_estimating_run ────────────────────────────────────────────────────────


def test_a_finished_run_settles_posts_the_fixed_sentence_and_finishes(world):
    _seed(world, run_task(), "ai_estimate")
    world.state["run"] = {"status": "done", "cost_usd": "14.20", "finished_at": NOW.isoformat(), "error": None}
    assert pt.advance(USER, run_task()) is True
    [msg] = _messages(world)
    assert msg["id"] == pt.message_id_for(TASK, pt.KIND_RUN)
    assert msg["content"] == "AI Estimating finished for 26.10.7212: done, $14.20. Open the run to review."
    assert msg["server_data"] is False and msg["tainted"] is False
    kinds = [b["type"] for b in msg["blocks"]]
    assert kinds == ["text", "task_result", "citation"]
    assert msg["blocks"][2]["path"] == f"/ai-estimating?run={RUN_ID}"
    assert msg["blocks"][1]["task_id"] == TASK
    row = _task(world)
    assert row["status"] == "done" and row["result"] == {"cost_usd": "14.20", "status": "done", "run_id": RUN_ID}
    assert _hold(world)["status"] == "settled" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("14.20")
    assert [b["type"] for b in world.bells] == [pt.BELL_FINISHED]
    assert world.state["run_calls"][0] == {"sb": None, "run_id": RUN_ID}


def test_a_run_done_with_errors_reads_that_way(world):
    _seed(world, run_task(), "ai_estimate")
    world.state["run"] = {"status": "done_with_errors", "cost_usd": "3.10", "finished_at": NOW.isoformat(), "error": None}
    assert pt.advance(USER, run_task()) is True
    assert "done with errors, $3.10" in _messages(world)[0]["content"]
    assert _task(world)["status"] == "done"


def test_a_failed_run_releases_with_its_actual_and_does_not_copy_the_ae_error(world):
    _seed(world, run_task(), "ai_estimate")
    world.state["run"] = {"status": "failed", "cost_usd": "2.00", "finished_at": NOW.isoformat(),
                          "error": "secret <script> from the AE"}
    assert pt.advance(USER, run_task()) is True
    row = _task(world)
    assert (row["status"], row["failure_reason"]) == ("failed", pt.FAILED_RUN)
    assert _hold(world)["status"] == "released" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("2.00")
    assert "secret" not in _messages(world)[0]["content"]
    assert [b["type"] for b in world.bells] == [pt.BELL_FAILED]


def test_user_spent_rises_after_a_canceled_run(world):
    _seed(world, run_task(), "ai_estimate")
    world.state["run"] = {"status": "canceled", "cost_usd": "1.25", "finished_at": NOW.isoformat(), "error": "Canceled."}
    before = spend.state(USER)
    assert pt.advance(USER, run_task()) is True
    after = spend.state(USER)
    row = _task(world)
    assert (row["status"], row["failure_reason"]) == ("canceled", pt.CANCELED_RUN)
    assert _hold(world)["status"] == "released"
    assert after.user_spent - before.user_spent == Decimal("1.25")
    assert after.global_spent - before.global_spent == Decimal("1.25")
    assert after.user_held < before.user_held


def test_a_run_still_going_stays_open_and_a_late_finish_settles_after_the_hold_ttl(world):
    old = _ago(hours=13)
    _seed(world, run_task(created_at=old, updated_at=old), "ai_estimate")
    world.db.tables["assistant_spend_reservations"][0]["expires_at"] = _ago(hours=1)
    assert pt.advance(USER, run_task()) is False
    assert _task(world)["status"] == "waiting_external" and _hold(world)["status"] == "active"
    assert _messages(world) == []
    world.state["run"] = {"status": "done", "cost_usd": "9.90", "finished_at": NOW.isoformat(), "error": None}
    assert pt.advance(USER, run_task()) is True
    assert _hold(world)["status"] == "settled" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("9.90")


def test_a_deleted_run_fails_the_task_and_releases_the_hold(world):
    _seed(world, run_task(), "ai_estimate")
    world.state["run"] = {"status": "missing", "cost_usd": "0.00", "finished_at": None, "error": None}
    assert pt.advance(USER, run_task()) is True
    assert _task(world)["failure_reason"] == pt.FAILED_RUN_MISSING
    assert _hold(world)["status"] == "released"
    assert _messages(world)[0]["content"] == "The AI Estimating run for 26.10.7212 no longer exists."


def test_a_run_task_without_a_run_yet_is_left_to_the_sweep(world):
    _seed(world, run_task(run_id=None, payload={"phase": "reserved"}), "ai_estimate")
    assert pt.advance(USER, run_task()) is False
    assert world.state["run_calls"] == []


def test_the_run_id_comes_from_the_column_or_the_payload(world):
    assert pt.run_id_of({"run_id": RUN_ID}) == RUN_ID
    assert pt.run_id_of({"run_id": None, "payload": {"run_id": "abc"}}) == "abc"
    assert pt.run_id_of({"payload": None}) == ""


def test_the_project_number_is_cleaned_and_falls_back(world):
    assert pt.project_number({"title": "AI Estimating run for 26.10.7212"}) == "26.10.7212"
    assert pt.project_number({"payload": {"project_number": "26.9.7130<b>"}}) == "26.9.7130b"
    assert pt.project_number({"title": "no number here", "payload": {}}) == "the project"


# ── Fencing and the other kinds ──────────────────────────────────────────────


def test_advance_ignores_a_task_that_is_not_open_or_not_an_ae_kind(world):
    _seed(world, pages_task(status="done"), "plans_pages")
    assert pt.advance(USER, pages_task()) is False
    world.db.tables["assistant_tasks"][0].update(status="running", kind="split_into_project")
    assert pt.advance(USER, pages_task()) is False
    assert pt.advance(USER, {}) is False
    assert world.state["chat_calls"] == [] and world.state["run_calls"] == []


def test_advance_reads_the_task_fresh_so_a_stale_copy_cannot_double_finish(world):
    _seed(world, pages_task(), "plans_pages")
    world.state["chat"] = {"status": "done", "cost_usd": "0.30", "content": "ok", "error": None}
    stale = pages_task()
    assert pt.advance(USER, stale) is True
    assert pt.advance(USER, stale) is False
    assert len(_messages(world)) == 1 and len(world.bells) == 1


def test_stop_task_is_fenced_and_a_repeat_is_a_no_op(world):
    _seed(world, pages_task(), "plans_pages")
    first = pt.stop_task(USER, pages_task(), pt.FAILED_OWNER, actual_usd="0.40", bell=False)
    assert first == (True, True)
    assert pt.stop_task(USER, pages_task(), pt.FAILED_OWNER, actual_usd="0.40", bell=False) == (False, False)
    row = _task(world)
    assert (row["status"], row["failure_reason"]) == ("failed", pt.FAILED_OWNER)
    assert _hold(world)["status"] == "released" and Decimal(str(_hold(world)["actual_usd"])) == Decimal("0.40")
    # Offboarding posts nothing and rings nothing.
    assert _messages(world) == [] and world.bells == []


def test_stop_task_with_a_text_posts_one_fixed_message_and_one_bell(world):
    _seed(world, run_task(), "ai_estimate")
    out = pt.stop_task(USER, run_task(), pt.FAILED_ROUTE, actual_usd="0",
                       text=pt.TEXT_ROUTE_INTERRUPTED.format(number="26.10.7212"))
    assert out == (True, True)
    assert _messages(world)[0]["content"] == (
        "The AI Estimating run for 26.10.7212 did not finish starting and was stopped.")
    assert [b["type"] for b in world.bells] == [pt.BELL_FAILED]
    assert _task(world)["result"]["outcome"] == pt.FAILED_ROUTE


def test_a_negative_or_unreadable_actual_never_closes_a_hold_with_a_wrong_number(world):
    # _dec reads anything unreadable or negative as 0; the hold is released at 0.
    _seed(world, pages_task(), "plans_pages")
    pt.stop_task(USER, pages_task(), pt.FAILED_OWNER, actual_usd="-3", bell=False)
    assert Decimal(str(_hold(world)["actual_usd"])) == Decimal("0")


# ── Source ───────────────────────────────────────────────────────────────────


def test_the_module_names_no_client_and_carries_no_em_dash():
    text = Path(pt.__file__).read_text(encoding="utf-8")
    assert EM_DASH not in text and EN_DASH not in text
    assert "get_supabase" not in text and "supabase_client" not in text and ".table(" not in text
    fixed = [pt.TEXT_PAGES_HEADER, pt.TEXT_PAGES_NO_ANSWER, pt.TEXT_PAGES_FAILED, pt.TEXT_PAGES_MISSING,
             pt.TEXT_PAGES_TIMED_OUT, pt.TEXT_RUN_FINISHED, pt.TEXT_RUN_MISSING, pt.TEXT_ROUTE_INTERRUPTED,
             pt.BELL_TEXT_PAGES_DONE, pt.BELL_TEXT_PAGES_FAILED, *pt.RUN_OUTCOME_TEXT.values()]
    assert all(EM_DASH not in t and EN_DASH not in t for t in fixed)


def test_the_repo_reads_the_open_ae_tasks_by_person_for_the_lazy_finalize(world):
    _seed(world, pages_task(), "plans_pages")
    assert [t["id"] for t in repo.open_ae_tasks(USER)] == [TASK]
