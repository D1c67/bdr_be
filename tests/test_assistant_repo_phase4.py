"""Phase 4 repo functions over the in-memory fake (docs/ASSISTANT_PHASE4_PLAN.md
sections 2, 3.2, 3.7, 3.9, 3.15 and WI-5): the reason-carrying approve, the
approval slice an evidence route checks, the append-only evidence log, the
report files, memory search, the AI Estimating holds and tasks, and the
server_data column."""

from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

import pytest

from app.assistant import repo
from app.core import supabase_client
from tests.assistant_fakes import (
    APPROVAL_ID,
    CONV,
    HOLD,
    MEMORY,
    MSG_ASSISTANT,
    MSG_USER,
    NOW,
    OTHER_USER,
    PLANS_RUN,
    REPORT,
    RUN,
    TASK,
    USER,
    CheckViolation,
    FakeDB,
    TriggerRefused,
    UniqueViolation,
    approval,
    conversation,
    delegation,
    evidence_access,
    memory_row,
    message,
    plans_note,
    profile,
    report,
    reservation,
    settings_row,
    task,
)

HASH = "a" * 64
PRE = {"v": 1, "target": {"user": OTHER_USER}}
REASON = "Checking why the bid went out late"
APPROVAL2 = "99999999-9999-4999-8999-999999999992"
REPORT2 = "12121212-1212-4212-8212-121212121212"


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(
        profiles=[profile()], assistant_profiles=[{"user_id": USER, "legal_hold": False}],
        assistant_settings=[settings_row()], assistant_conversations=[conversation()],
        assistant_messages=[message()], assistant_delegations=[delegation()],
        assistant_approvals=[], assistant_tool_calls=[], assistant_tasks=[],
        assistant_spend_reservations=[], assistant_memories=[], assistant_evidence_access=[],
        assistant_reports=[], plans_reader_notes=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


def _sensitive(db, **over) -> dict:
    row = approval(action_kind="person_evidence", payload_sha256=HASH, precondition=PRE, **over)
    db.tables["assistant_approvals"].append(row)
    return row


# ── The new functions all take user_id first ────────────────────────────────


NEW_REPO_FUNCTIONS = (
    "decide_with_reason", "approval_for_route", "insert_evidence_access", "evidence_access_for_approval",
    "insert_report", "get_report", "report_for_approval", "list_reports", "owner_reports",
    "reports_to_purge", "mark_report_purged", "search_memories", "tool_names_for_message",
    "task_by_approval", "open_ae_tasks", "close_reservation", "ae_hold_by_approval",
)


def test_every_new_repo_function_takes_user_id_first():
    for name in NEW_REPO_FUNCTIONS:
        params = list(inspect.signature(getattr(repo, name)).parameters)
        assert params[0] == "user_id", name


def test_the_new_repo_source_has_no_em_dash():
    assert chr(0x2014) not in inspect.getsource(repo)


# ── decide_with_reason ───────────────────────────────────────────────────────


def _decide(reason=REASON, **over):
    args = {"payload_sha256": HASH, "precondition": PRE, "session_aal": "aal2", "reason": reason}
    args.update(over)
    return repo.decide_with_reason(USER, APPROVAL_ID, **args)


def test_decide_with_reason_approves_and_stores_the_reason_once(db):
    _sensitive(db)
    res = _decide(reason=f"  {REASON}  ")
    assert res["ok"] is True and res["row"]["status"] == "approved"
    stored = db.tables["assistant_approvals"][0]
    assert stored["decision_reason"] == REASON
    assert stored["decided_by"] == USER and stored["decided_session_aal"] == "aal2"
    assert db.rpc_calls[-1][0] == "assistant_approval_decide_with_reason"
    assert db.rpc_calls[-1][1]["p_reason"] == f"  {REASON}  "
    # Set once: the trigger refuses a change, an identical write passes.
    with pytest.raises(TriggerRefused):
        repo.mark_approval(USER, APPROVAL_ID, {"decision_reason": "A different reason entirely"},
                           expect_status="approved")
    assert db.tables["assistant_approvals"][0]["decision_reason"] == REASON


def test_decide_with_reason_refuses_a_nine_character_reason(db):
    _sensitive(db)
    res = _decide(reason="123456789")
    assert res == {"ok": False, "reason": "reason_invalid", "row": None}
    assert db.tables["assistant_approvals"][0]["status"] == "pending"
    assert db.tables["assistant_approvals"][0]["decision_reason"] is None
    assert _decide(reason="1234567890")["ok"] is True


def test_decide_with_reason_trims_before_counting(db):
    _sensitive(db)
    assert _decide(reason="   short   ")["reason"] == "reason_invalid"
    assert _decide(reason="x" * 501)["reason"] == "reason_invalid"


def test_decide_with_reason_falls_through_to_the_0172_classification(db):
    _sensitive(db)
    stale = _decide(payload_sha256="b" * 64)
    assert stale["ok"] is False and stale["reason"] == "stale"
    assert db.tables["assistant_approvals"][0]["status"] == "pending"
    assert db.tables["assistant_approvals"][0]["decision_reason"] is None
    # The delegation never approves, even with the default all-zero hash.
    db.tables["assistant_approvals"][0]["payload_sha256"] = "0" * 64
    assert _decide(payload_sha256="b" * 64)["reason"] == "stale"
    assert db.tables["assistant_approvals"][0]["status"] == "pending"
    missing = repo.decide_with_reason(USER, "99999999-9999-4999-8999-999999999991", payload_sha256=HASH,
                                      precondition=PRE, session_aal="aal2", reason=REASON)
    assert missing["reason"] == "not_found"


def test_decide_with_reason_classifies_a_used_or_expired_card(db):
    row = _sensitive(db)
    assert _decide()["ok"] is True
    again = _decide()
    assert again["ok"] is False and again["reason"] == "not_pending"
    row["status"], row["decision_reason"] = "pending", None
    row["expires_at"] = (NOW - timedelta(minutes=1)).isoformat()
    expired = _decide()
    assert expired["reason"] == "expired" and expired["row"]["expired_reason"] == "timed_out"


def test_decide_with_reason_is_scoped_to_the_owner(db):
    _sensitive(db)
    res = repo.decide_with_reason(OTHER_USER, APPROVAL_ID, payload_sha256=HASH, precondition=PRE,
                                  session_aal="aal2", reason=REASON)
    assert res["reason"] == "not_found"
    assert db.tables["assistant_approvals"][0]["status"] == "pending"


def test_the_reason_check_rejects_a_short_stored_reason(db):
    _sensitive(db)
    with pytest.raises(CheckViolation):
        repo.mark_approval(USER, APPROVAL_ID, {"decision_reason": "too short"}, expect_status="pending")


# ── approval_for_route ───────────────────────────────────────────────────────


def test_approval_for_route_returns_the_slice_a_route_checks(db):
    _sensitive(db, payload={"v": 1, "args": {"user_id": OTHER_USER, "view": "activity"}}, status="executing",
               decision_reason=REASON)
    got = repo.approval_for_route(USER, APPROVAL_ID)
    assert {"status", "action_kind", "payload", "decision_reason", "conversation_id"} <= set(got)
    assert got["status"] == "executing" and got["action_kind"] == "person_evidence"
    assert got["decision_reason"] == REASON and got["conversation_id"] == CONV
    assert got["payload"]["args"]["user_id"] == OTHER_USER


def test_approval_for_route_selects_only_its_slice_and_the_reason_stays_off_the_card_columns(db, monkeypatch):
    seen: list[str] = []
    real = db.table

    def spy(name):
        query = real(name)
        original = query.select
        query.select = lambda *a, **k: (seen.append(str(a[0]) if a else ""), original(*a, **k))[1]
        return query

    monkeypatch.setattr(db, "table", spy)
    _sensitive(db, status="executing", decision_reason=REASON)
    repo.approval_for_route(USER, APPROVAL_ID)
    assert seen == ["status, action_kind, payload, decision_reason, conversation_id"]
    # The card routes (activity, list for a message) never read the reason.
    assert "decision_reason" not in repo.APPROVAL_COLUMNS


def test_approval_for_route_is_none_for_another_user_or_a_missing_id(db):
    _sensitive(db)
    assert repo.approval_for_route(OTHER_USER, APPROVAL_ID) is None
    assert repo.approval_for_route(USER, "99999999-9999-4999-8999-999999999991") is None
    assert repo.approval_for_route(USER, "") is None


# ── The evidence access log ──────────────────────────────────────────────────


def _access(**over) -> dict:
    row = evidence_access(**over)
    row.pop("id", None), row.pop("created_at", None), row.pop("reader_user_id", None)
    return row


def test_insert_evidence_access_appends_once_per_approval(db):
    first = repo.insert_evidence_access(USER, _access())
    assert first and first["reader_user_id"] == USER and first["approval_id"] == APPROVAL_ID
    assert repo.insert_evidence_access(USER, _access(row_count=99)) is None
    rows = db.tables["assistant_evidence_access"]
    assert len(rows) == 1 and rows[0]["row_count"] == 3
    assert [c for c in db.calls if c == ("insert", "assistant_evidence_access")] == []


def test_insert_evidence_access_forces_the_reader_to_the_caller(db):
    got = repo.insert_evidence_access(USER, {**_access(), "reader_user_id": OTHER_USER})
    assert got["reader_user_id"] == USER


def test_insert_evidence_access_requires_an_approval_and_a_real_reason(db):
    with pytest.raises(ValueError):
        repo.insert_evidence_access(USER, _access(approval_id=None))
    with pytest.raises(CheckViolation):
        repo.insert_evidence_access(USER, _access(reason="too short"))
    assert db.tables["assistant_evidence_access"] == []


def test_a_second_access_row_for_one_approval_is_refused_by_the_index(db):
    db.tables["assistant_evidence_access"].append(evidence_access())
    with pytest.raises(UniqueViolation):
        db._insert("assistant_evidence_access", {**evidence_access(), "id": "13131313-1313-4313-8313-131313131312"})


def test_the_evidence_log_refuses_an_update_and_a_delete(db):
    repo.insert_evidence_access(USER, _access())
    with pytest.raises(TriggerRefused):
        db.table("assistant_evidence_access").update({"reason": "Changed afterwards, quietly"}).execute()
    with pytest.raises(TriggerRefused):
        db.table("assistant_evidence_access").delete().execute()
    assert len(db.tables["assistant_evidence_access"]) == 1


def test_evidence_access_for_approval_reads_the_readers_own_row(db):
    repo.insert_evidence_access(USER, _access(xlsx_report_id=REPORT))
    got = repo.evidence_access_for_approval(USER, APPROVAL_ID)
    assert got["xlsx_report_id"] == REPORT
    assert repo.evidence_access_for_approval(OTHER_USER, APPROVAL_ID) is None
    assert repo.evidence_access_for_approval(USER, "") is None


# ── Report files ─────────────────────────────────────────────────────────────


def _new_report(**over) -> dict:
    row = report(**over)
    row.pop("created_at"), row.pop("user_id")
    return row


def test_insert_report_pins_the_storage_path_to_the_owner_and_the_id(db):
    got = repo.insert_report(USER, _new_report())
    assert got["storage_path"] == f"assistant-reports/{USER}/{REPORT}.xlsx" and got["status"] == "ready"
    with pytest.raises(CheckViolation):
        repo.insert_report(USER, _new_report(id=REPORT2, storage_path=f"assistant-reports/{OTHER_USER}/{REPORT2}.xlsx"))
    with pytest.raises(CheckViolation):
        repo.insert_report(USER, _new_report(id=REPORT2, storage_path="elsewhere/x.xlsx"))
    with pytest.raises(ValueError):
        repo.insert_report(USER, {k: v for k, v in _new_report().items() if k != "id"})


def test_insert_report_forces_the_owner(db):
    got = repo.insert_report(USER, {**_new_report(), "user_id": OTHER_USER})
    assert got["user_id"] == USER


def test_insert_report_refuses_a_second_row_for_one_approval(db):
    repo.insert_report(USER, _new_report(approval_id=APPROVAL_ID))
    with pytest.raises(repo.ReportExists) as exc:
        repo.insert_report(USER, _new_report(id=REPORT2, approval_id=APPROVAL_ID,
                                             storage_path=f"assistant-reports/{USER}/{REPORT2}.xlsx"))
    assert exc.value.code == "23505"
    assert repo.report_for_approval(USER, APPROVAL_ID)["id"] == REPORT
    assert repo.report_for_approval(OTHER_USER, APPROVAL_ID) is None


def test_reports_are_readable_by_their_owner_only(db):
    repo.insert_report(USER, _new_report())
    assert repo.get_report(USER, REPORT)["id"] == REPORT
    assert repo.get_report(OTHER_USER, REPORT) is None
    assert repo.get_report(USER, "") is None
    assert [r["id"] for r in repo.list_reports(USER)] == [REPORT]
    assert repo.list_reports(OTHER_USER) == []


def test_list_reports_is_newest_first_and_capped(db):
    db.tables["assistant_reports"] += [
        report(id=REPORT, created_at=(NOW - timedelta(hours=2)).isoformat()),
        report(id=REPORT2, created_at=NOW.isoformat(), storage_path=f"assistant-reports/{USER}/{REPORT2}.xlsx"),
    ]
    assert [r["id"] for r in repo.list_reports(USER)] == [REPORT2, REPORT]
    assert [r["id"] for r in repo.list_reports(USER, limit=1)] == [REPORT2]


def test_reports_to_purge_takes_expired_ready_rows_oldest_expiry_first(db):
    old = (NOW - timedelta(days=2)).isoformat()
    older = (NOW - timedelta(days=3)).isoformat()
    path2 = f"assistant-reports/{USER}/{REPORT2}.xlsx"
    db.tables["assistant_reports"] += [
        report(id=REPORT, expires_at=old),
        report(id=REPORT2, expires_at=older, storage_path=path2),
        report(id="12121212-1212-4212-8212-121212121213", expires_at=(NOW + timedelta(days=1)).isoformat(),
               storage_path=f"assistant-reports/{USER}/12121212-1212-4212-8212-121212121213.xlsx"),
        report(id="12121212-1212-4212-8212-121212121214", expires_at=old, status="purged", purged_at=old,
               storage_path=f"assistant-reports/{USER}/12121212-1212-4212-8212-121212121214.xlsx"),
    ]
    got = repo.reports_to_purge(None, limit=10)
    assert [r["id"] for r in got] == [REPORT2, REPORT]
    assert {"id", "user_id", "storage_path", "expires_at", "filename"} <= set(got[0])
    assert [r["id"] for r in repo.reports_to_purge(USER)] == [REPORT2, REPORT]
    assert repo.reports_to_purge(OTHER_USER) == []


def test_reports_to_purge_skips_a_legal_hold_owner(db):
    db.tables["assistant_reports"].append(report(expires_at=(NOW - timedelta(days=1)).isoformat()))
    db.tables["assistant_profiles"][0]["legal_hold"] = True
    assert repo.reports_to_purge(None) == []
    assert repo.reports_to_purge(USER) == []
    # An owner without a hold is still swept.
    db.tables["assistant_reports"].append(report(
        id=REPORT2, user_id=OTHER_USER, expires_at=(NOW - timedelta(days=1)).isoformat()))
    assert [r["id"] for r in repo.reports_to_purge(None)] == [REPORT2]


def test_mark_report_purged_moves_a_ready_row_once(db):
    db.tables["assistant_reports"].append(report())
    assert repo.mark_report_purged(OTHER_USER, REPORT) is False
    assert repo.mark_report_purged(USER, REPORT) is True
    row = db.tables["assistant_reports"][0]
    assert row["status"] == "purged" and row["purged_at"]
    assert repo.mark_report_purged(USER, REPORT) is False
    assert repo.mark_report_purged(USER, "") is False


def test_owner_reports_filters_by_status_and_spec(db):
    db.tables["assistant_reports"] += [
        report(id=REPORT, spec="bid_invitations"),
        report(id=REPORT2, spec="project_timeline", storage_path=f"assistant-reports/{USER}/{REPORT2}.xlsx"),
        report(id="12121212-1212-4212-8212-121212121215", user_id=OTHER_USER),
    ]
    assert {r["id"] for r in repo.owner_reports(USER)} == {REPORT, REPORT2}
    assert [r["id"] for r in repo.owner_reports(USER, specs=["project_timeline"])] == [REPORT2]
    assert repo.owner_reports(USER, specs=[]) == []
    assert repo.owner_reports(USER, statuses=("purged",)) == []


# ── memory_search ────────────────────────────────────────────────────────────


def test_search_memories_returns_the_persons_own_live_notes_ranked(db):
    db.tables["assistant_memories"] += [
        memory_row(id=MEMORY, title="Lighting quotes first", body="Always chase lighting quotes before 10"),
        memory_row(id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc2", title="Unrelated", body="Nothing about it",
                   status="pending_review"),
        memory_row(id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc3", title="Lighting archive", body="old",
                   status="archived"),
        memory_row(id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc4", user_id=OTHER_USER, title="Lighting theirs",
                   body="lighting"),
    ]
    got = repo.search_memories(USER, "lighting quotes")
    assert [r["id"] for r in got] == [MEMORY]
    assert set(got[0]) >= {"id", "kind", "status", "title", "body", "project_id", "pinned",
                           "origin_tainted", "updated_at", "rank"}
    name, params = db.rpc_calls[-1]
    assert name == "assistant_memory_search" and params["p_user_id"] == USER
    assert params["p_statuses"] == ["active", "pending_review"] and params["p_limit"] == 10


def test_search_memories_filters_statuses_and_kind_and_clamps(db):
    db.tables["assistant_memories"] += [
        memory_row(id=MEMORY, title="Lighting rule", kind="rule"),
        memory_row(id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc2", title="Lighting draft", kind="preference",
                   status="pending_review"),
    ]
    only_pending = repo.search_memories(USER, "lighting", ["pending_review"])
    assert [r["id"] for r in only_pending] == ["cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc2"]
    assert [r["id"] for r in repo.search_memories(USER, "lighting", None, "rule")] == [MEMORY]
    # An unknown status is dropped; archived is never searchable.
    repo.search_memories(USER, "lighting", ["archived"], limit=500)
    assert db.rpc_calls[-1][1]["p_statuses"] == ["active", "pending_review"]
    assert db.rpc_calls[-1][1]["p_limit"] == 25


def test_search_memories_answers_nothing_for_a_tiny_query_without_a_call(db):
    assert repo.search_memories(USER, "a") == []
    assert repo.search_memories(USER, "   ") == []
    assert repo.search_memories(USER, None) == []
    assert db.rpc_calls == []


# ── tool_names_for_message ───────────────────────────────────────────────────


def test_tool_names_for_message_is_distinct_sorted_and_owner_scoped(db):
    def call(user, name, phase="begun", key=None, msg=MSG_ASSISTANT):
        db.tables["assistant_tool_calls"].append({
            "id": f"t{len(db.tables['assistant_tool_calls'])}", "user_id": user, "message_id": msg,
            "tool_name": name, "phase": phase,
            "idempotency_key": key or f"k{len(db.tables['assistant_tool_calls'])}"})
    call(USER, "plans_search_notes")
    call(USER, "find_tools")
    call(USER, "plans_search_notes")
    call(USER, "plans_search_notes", phase="completed")
    call(USER, "other_message_tool", msg=MSG_USER)
    call(OTHER_USER, "someone_elses_tool")
    assert repo.tool_names_for_message(USER, MSG_ASSISTANT) == ["find_tools", "plans_search_notes"]
    assert repo.tool_names_for_message(OTHER_USER, MSG_ASSISTANT) == ["someone_elses_tool"]
    assert repo.tool_names_for_message(USER, None) == []


# ── AI Estimating tasks and holds ────────────────────────────────────────────


def test_open_ae_tasks_lists_only_running_ae_kinds_and_scopes_by_user(db):
    db.tables["assistant_tasks"] += [
        task(id=TASK, kind="plans_read_pages", status="running", approval_id=None),
        task(id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2", kind="ai_estimating_run", status="waiting_external",
             approval_id=None, user_id=OTHER_USER),
        task(id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3", kind="ai_estimating_run", status="done", approval_id=None),
        task(id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4", kind="split_into_project", status="running",
             approval_id=None),
    ]
    assert {t["id"] for t in repo.open_ae_tasks(None)} == {TASK, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"}
    assert [t["id"] for t in repo.open_ae_tasks(USER)] == [TASK]
    assert repo.open_ae_tasks("33333333-3333-4333-8333-333333333399") == []


def test_task_by_approval_is_owner_scoped(db):
    db.tables["assistant_tasks"].append(task(kind="ai_estimating_run", status="running"))
    assert repo.task_by_approval(USER, APPROVAL_ID)["id"] == TASK
    assert repo.task_by_approval(OTHER_USER, APPROVAL_ID) is None
    assert repo.task_by_approval(USER, "") is None


def _hold(db, kind="plans_pages", **over) -> dict:
    row = reservation(kind=kind, **over)
    db.tables["assistant_spend_reservations"].append(row)
    return row


def test_ae_hold_by_approval_finds_the_hold_in_any_status(db):
    _hold(db, "ai_estimate", status="released", actual_usd=1.5, settled_at=NOW.isoformat())
    got = repo.ae_hold_by_approval(USER, APPROVAL_ID, "ai_estimate")
    assert got["id"] == HOLD and got["status"] == "released"
    assert repo.ae_hold_by_approval(USER, APPROVAL_ID, "plans_pages") is None
    assert repo.ae_hold_by_approval(OTHER_USER, APPROVAL_ID, "ai_estimate") is None
    assert repo.ae_hold_by_approval(USER, "", "ai_estimate") is None
    with pytest.raises(ValueError):
        repo.ae_hold_by_approval(USER, APPROVAL_ID, "split_task")


def test_the_reserve_rpc_takes_the_two_ae_kinds_and_one_hold_per_approval(db):
    got = repo.rpc_spend_reserve(USER, "plans_pages", Decimal("2.13"), 14400, 5, 100, run_id=RUN,
                                 task_id=TASK, approval_id=APPROVAL_ID)
    assert got["ok"] is True
    with pytest.raises(UniqueViolation) as exc:
        repo.rpc_spend_reserve(USER, "ai_estimate", Decimal("1"), 3600, 5, 100, approval_id=APPROVAL_ID)
    assert "approval_ae_uq" in str(exc.value)
    with pytest.raises(UniqueViolation) as exc:
        repo.rpc_spend_reserve(USER, "ai_estimate", Decimal("1"), 3600, 5, 100, task_id=TASK,
                               approval_id=APPROVAL2)
    assert "task_ae_uq" in str(exc.value)
    with pytest.raises(CheckViolation):
        repo.rpc_spend_reserve(USER, "not_a_kind", Decimal("1"), 3600, 5, 100)
    assert repo.ae_hold_by_approval(USER, APPROVAL_ID, "plans_pages")["id"] == got["id"]


def test_close_reservation_with_an_explicit_actual(db):
    _hold(db, "ai_estimate", amount_usd=3.0)
    assert repo.close_reservation(USER, HOLD, "settled", actual_usd=Decimal("2.37")) is True
    row = db.tables["assistant_spend_reservations"][0]
    assert row["status"] == "settled" and Decimal(str(row["actual_usd"])) == Decimal("2.37")
    assert row["settled_at"]
    # Fenced: a second close changes nothing.
    assert repo.close_reservation(USER, HOLD, "released", actual_usd=Decimal("9")) is False
    assert Decimal(str(row["actual_usd"])) == Decimal("2.37")


def test_close_reservation_without_an_actual_keeps_the_logged_cost(db):
    _hold(db, "split_task")
    db.tables.setdefault("llm_call_log", []).append(
        {"id": "c1", "user_id": USER, "spend_reservation_id": HOLD, "cost_usd": 0.12})
    assert repo.close_reservation(USER, HOLD, "settled") is True
    assert Decimal(str(db.tables["assistant_spend_reservations"][0]["actual_usd"])) == Decimal("0.12")


def test_close_reservation_refuses_a_negative_actual_and_a_bad_status(db):
    _hold(db, "ai_estimate")
    with pytest.raises(ValueError):
        repo.close_reservation(USER, HOLD, "settled", actual_usd=Decimal("-1"))
    with pytest.raises(ValueError):
        repo.close_reservation(USER, HOLD, "active")
    assert db.tables["assistant_spend_reservations"][0]["status"] == "active"


def test_a_released_ae_hold_with_an_actual_counts_in_the_spend_state(db):
    _hold(db, "ai_estimate", amount_usd=3.0)
    before = repo.rpc_spend_state(USER, 5, 100)
    assert before["user_spent"] == 0 and before["user_held"] == 3.0
    repo.close_reservation(USER, HOLD, "released", actual_usd=Decimal("1.40"))
    after = repo.rpc_spend_state(USER, 5, 100)
    assert after["user_spent"] == pytest.approx(1.40) and after["user_held"] == 0
    assert after["global_spent"] == pytest.approx(1.40)
    # Another person's state does not include it.
    assert repo.rpc_spend_state(OTHER_USER, 5, 100)["user_spent"] == 0


def test_a_settled_ae_hold_counts_but_a_split_hold_closed_with_no_calls_does_not(db):
    _hold(db, "plans_pages", amount_usd=2.0)
    repo.close_reservation(USER, HOLD, "settled", actual_usd=Decimal("0.60"))
    split = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2"
    db.tables["assistant_spend_reservations"].append(reservation(id=split, kind="split_task", task_id=None,
                                                                 approval_id=None))
    repo.close_reservation(USER, split, "released")
    assert repo.rpc_spend_state(USER, 5, 100)["user_spent"] == pytest.approx(0.60)


def test_an_ae_hold_closed_yesterday_no_longer_counts(db):
    _hold(db, "ai_estimate", status="settled", actual_usd=4.0,
          settled_at=(NOW - timedelta(days=2)).isoformat())
    assert repo.rpc_spend_state(USER, 5, 100)["user_spent"] == 0


# ── server_data ──────────────────────────────────────────────────────────────


def test_server_data_is_selected_defaults_false_and_round_trips(db):
    assert "server_data" in repo.MESSAGE_COLUMNS
    plain = repo.insert_message(USER, {"conversation_id": CONV, "role": "assistant", "content": "hi",
                                       "status": "done"})
    assert plain["server_data"] is False
    delivered = repo.insert_message(USER, {"conversation_id": CONV, "role": "assistant", "content": "rows",
                                           "status": "done", "server_data": True, "tainted": True})
    assert repo.get_message(USER, delivered["id"])["server_data"] is True
    assert {m["id"]: m.get("server_data") for m in repo.list_messages(USER, CONV)}[delivered["id"]] is True


# ── The phase 4 fake tables (so later items can lean on them) ────────────────


def test_the_notes_fake_keeps_the_plain_unique_key_and_upsert_ignores_duplicates(db):
    cols = "run_id,source_scope,category,source_version,source_key"
    note = {k: v for k, v in plans_note().items() if k not in ("id", "created_at")}
    first = db.table("plans_reader_notes").upsert([note], on_conflict=cols, ignore_duplicates=True).execute()
    again = db.table("plans_reader_notes").upsert([{**note, "body": "changed"}], on_conflict=cols,
                                                  ignore_duplicates=True).execute()
    assert len(first.data) == 1 and again.data == []
    assert len(db.tables["plans_reader_notes"]) == 1 and db.tables["plans_reader_notes"][0]["body"] != "changed"
    # Main rows use an empty category (never null), so the key still matches.
    main = {**note, "source_scope": "main"}
    main.pop("category")
    db.table("plans_reader_notes").upsert([main], on_conflict=cols, ignore_duplicates=True).execute()
    db.table("plans_reader_notes").upsert([main], on_conflict=cols, ignore_duplicates=True).execute()
    assert [n["category"] for n in db.tables["plans_reader_notes"]] == ["gear", ""]
    with pytest.raises(UniqueViolation):
        db._insert("plans_reader_notes", {**note})
    with pytest.raises(CheckViolation):
        db._insert("plans_reader_notes", {**note, "source_key": "x2", "kind": "not_a_kind"})


def test_the_notes_search_rpc_stand_in_filters_like_the_function(db):
    other = "14141414-1414-4414-8414-141414141412"
    db.tables["plans_reader_notes"] += [
        plans_note(),
        plans_note(id="15151515-1515-4515-8515-151515151512", source_key="n2", kind="risk", category="",
                   source_scope="main", sheet_number=None, body="Fire alarm panel not on the drawings"),
        plans_note(id="15151515-1515-4515-8515-151515151513", run_id=other, source_key="n3"),
    ]
    rows = db.rpc("search_plans_notes", {"p_q": "1hd-21", "p_run_ids": [PLANS_RUN]}).execute().data
    assert [r["source_key"] for r in rows] == ["notes[0]"]
    by_sheet = db.rpc("search_plans_notes", {"p_q": "E-201", "p_run_ids": [PLANS_RUN, other]}).execute().data
    assert {r["run_id"] for r in by_sheet} == {PLANS_RUN, other}
    risk = db.rpc("search_plans_notes", {"p_q": "alarm", "p_run_ids": [PLANS_RUN], "p_kinds": ["risk"]}).execute().data
    assert [r["kind"] for r in risk] == ["risk"]
    assert db.rpc("search_plans_notes", {"p_q": "alarm", "p_run_ids": [PLANS_RUN], "p_kinds": ["spec"]}
                  ).execute().data == []
    assert db.rpc("search_plans_notes", {"p_q": "alarm", "p_run_ids": [PLANS_RUN], "p_category": "gear"}
                  ).execute().data == []


def test_no_em_dash_in_the_phase4_test_helpers():
    text = open("tests/assistant_fakes.py", encoding="utf-8").read()
    assert chr(0x2014) not in text


# ── Schemas ──────────────────────────────────────────────────────────────────


def test_approve_in_takes_an_optional_reason_of_at_most_500_characters():
    from pydantic import ValidationError

    from app.assistant.schemas import ApproveIn

    base = {"payload_sha256": HASH, "precondition": {}}
    assert ApproveIn(**base).reason is None
    assert ApproveIn(**base, reason="x" * 500).reason == "x" * 500
    with pytest.raises(ValidationError):
        ApproveIn(**base, reason="x" * 501)


def test_schedule_create_in_carries_period_and_week_bounded_to_ten_characters():
    from pydantic import ValidationError

    from app.assistant.schemas import ScheduleCreateIn

    body = {"recipe": "estimator_vs_bid", "cadence": {"frequency": "monthly", "hour": 7, "day_of_month": 1}}
    made = ScheduleCreateIn.model_validate({**body, "period": "last_month", "week": "2026-10-19"})
    assert made.period == "last_month" and made.week == "2026-10-19"
    assert ScheduleCreateIn.model_validate(body).period is None
    with pytest.raises(ValidationError):
        ScheduleCreateIn.model_validate({**body, "period": "x" * 11})
    with pytest.raises(ValidationError):
        ScheduleCreateIn.model_validate({**body, "week": "x" * 11})


def test_recipe_row_out_has_an_optional_extra():
    from app.assistant.schemas import RecipeRowOut

    assert RecipeRowOut(project={"id": "p"}).extra is None
    assert RecipeRowOut(project={"id": "p"}, extra={"verdict": "We bid higher"}).extra == {"verdict": "We bid higher"}


def test_report_build_in_refuses_person_evidence_and_a_bad_project_id():
    from pydantic import ValidationError

    from app.assistant.schemas import REPORT_SPECS, TOOL_REPORT_SPECS, ReportBuildIn

    assert "person_evidence" in REPORT_SPECS and "person_evidence" not in TOOL_REPORT_SPECS
    assert ReportBuildIn(spec="bid_invitations", range="wtd").spec == "bid_invitations"
    with pytest.raises(ValidationError):
        ReportBuildIn(spec="person_evidence")
    with pytest.raises(ValidationError):
        ReportBuildIn(spec="project_timeline", project_id="not-a-uuid")


def test_ae_task_out_extends_the_split_task_shape_without_changing_it():
    from app.assistant.schemas import AE_TASK_KINDS, TASK_KINDS, AeTaskOut, TaskOut

    assert set(AE_TASK_KINDS) <= set(TASK_KINDS) and len(TASK_KINDS) == 4
    assert "plans_run_id" not in TaskOut.model_fields and "result" not in TaskOut.model_fields
    assert issubclass(AeTaskOut, TaskOut)
    out = AeTaskOut(id="t", kind="plans_read_pages", status="running", created_at="2026-10-24T00:00:00Z")
    assert out.plans_run_id is None and out.result is None
