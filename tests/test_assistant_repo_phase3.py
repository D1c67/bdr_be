"""Phase 3 repo functions over the in-memory fake (docs/ASSISTANT_PHASE3_PLAN.md
3.3 to 3.10 and section 2): the memory RPC for every op and actor, the
caps, the pin guard, the pending cap, legal hold and the delete rewrite;
schedules and their caps; the claim with skip locked and the occurrence key;
the run channel ledger; the trusted turn context; the offboarding reads and
writes; and the system sweeps."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import pytest

from app.assistant import repo
from app.core import supabase_client
from tests.assistant_fakes import (
    CONV,
    MEMORY,
    MEMORY2,
    MSG_ASSISTANT,
    MSG_USER,
    NOW,
    OTHER_USER,
    PROJECT,
    RUN,
    SCHEDULE,
    SCHEDULE2,
    USER,
    WEEKDAYS_7AM,
    CheckViolation,
    FakeDB,
    approval,
    conversation,
    delegation,
    memory_row,
    message,
    pending_answer,
    profile,
    reservation,
    schedule,
    schedule_run,
    settings_row,
    task,
)

MSG_OTHER = "56565656-5656-4656-8656-565656565656"
MEMORY3 = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc3"
KEY = f"{MSG_ASSISTANT}:1:memory_save:0123456789abcdef"


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(
        profiles=[profile()], assistant_profiles=[{"user_id": USER, "legal_hold": False}],
        assistant_settings=[settings_row()], assistant_conversations=[conversation()],
        assistant_messages=[message(), pending_answer(status="running")],
        assistant_memories=[], assistant_memory_events=[], assistant_memory_revisions=[],
        assistant_schedules=[], assistant_schedule_runs=[], assistant_tool_results=[], assistant_tool_calls=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


def _events(db, memory_id=MEMORY) -> list[str]:
    return [e["event"] for e in db.tables["assistant_memory_events"] if e["memory_id"] == memory_id]


def _mem(db, memory_id=MEMORY) -> dict | None:
    return next((m for m in db.tables["assistant_memories"] if m["id"] == memory_id), None)


def _create(db, *, actor="assistant", status="active", source="user_stated", key=KEY, **fields):
    body = {"kind": "preference", "title": "Sort bids by due time", "body": "", "status": status,
            "source": source, **fields}
    return repo.memory_apply(USER, "create", actor=actor, fields=body, conversation_id=CONV,
                             message_id=MSG_ASSISTANT, idempotency_key=key)


# ── memory_apply: create, replay, actors ─────────────────────────────────────


def test_create_records_an_event_without_text_and_replays_by_key(db):
    out = _create(db)
    assert out["ok"] and out["replay"] is False and out["revision"] is None
    mid = out["memory"]["id"]
    assert out["memory"]["status"] == "active" and out["memory"]["source"] == "user_stated"
    event = db.tables["assistant_memory_events"][0]
    assert event["event"] == "created" and event["actor"] == "assistant" and event["message_id"] == MSG_ASSISTANT
    assert "Sort" not in json.dumps(event["payload"])
    again = _create(db, title="Something else")
    assert again["replay"] is True and again["memory"]["id"] == mid
    assert len(db.tables["assistant_memories"]) == 1 and len(db.tables["assistant_memory_events"]) == 1


def test_user_create_needs_user_settings_and_the_status_guard_holds(db):
    assert _create(db, actor="user", source="user_settings", key=None)["memory"]["source"] == "user_settings"
    for actor, status, source in (("assistant", "active", "user_settings"), ("assistant", "archived", "inferred"),
                                  ("user", "pending_review", "user_settings"), ("assistant", None, None)):
        with pytest.raises(CheckViolation, match="bad status or source"):
            _create(db, actor=actor, status=status, source=source, key=None)
    assert len(db.tables["assistant_memories"]) == 1


@pytest.mark.parametrize("actor,op", [
    ("assistant", "edit"), ("assistant", "pin"), ("assistant", "delete"), ("assistant", "approve"),
    ("assistant", "archive"), ("user", "forget"), ("user", "revise_pending"), ("user", "propose_revision"),
    ("user", "move"), ("system", "create"), ("system", "pin"), ("system", "forget"), ("system", "approve"),
])
def test_each_actor_is_refused_the_ops_it_does_not_own(db, actor, op):
    db.tables["assistant_memories"].append(memory_row())
    before = json.dumps(db.tables, sort_keys=True, default=str)
    with pytest.raises(CheckViolation, match="is not allowed for actor"):
        repo.memory_apply(USER, op, actor=actor, memory_id=MEMORY, fields={"kind": "preference", "title": "x"})
    assert json.dumps(db.tables, sort_keys=True, default=str) == before


def test_not_found_and_another_users_row(db):
    db.tables["assistant_memories"].append(memory_row(user_id=OTHER_USER))
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "archive", actor="user", memory_id=MEMORY)
    assert exc.value.code == "memory_not_found"


# ── memory_apply: the user's ops ─────────────────────────────────────────────


def test_pending_note_approve_confirm_reject(db):
    pending = _create(db, status="pending_review", source="inferred")["memory"]
    out = repo.memory_apply(USER, "approve", actor="user", memory_id=pending["id"],
                            expected_updated_at=pending["updated_at"])
    assert out["memory"]["status"] == "active" and out["memory"]["source"] == "user_confirmed"
    second = _create(db, status="pending_review", source="inferred", key="k2" * 8)["memory"]
    assert repo.memory_apply(USER, "confirm", actor="user", memory_id=second["id"])["memory"]["status"] == "active"
    third = _create(db, status="pending_review", source="inferred", key="k3" * 8)["memory"]
    out = repo.memory_apply(USER, "reject", actor="user", memory_id=third["id"])
    assert out["memory"]["status"] == "archived" and out["memory"]["archived_at"]
    assert _events(db, pending["id"]) == ["created", "approved"]
    assert _events(db, second["id"]) == ["created", "confirmed"]
    assert _events(db, third["id"]) == ["created", "rejected"]
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "approve", actor="user", memory_id=pending["id"])
    assert exc.value.code == "memory_wrong_status"


def test_edit_archive_restore_pin_unpin(db):
    db.tables["assistant_memories"].append(memory_row())
    out = repo.memory_apply(USER, "edit", actor="user", memory_id=MEMORY, fields={"title": "Sort by due date"})
    assert out["memory"]["title"] == "Sort by due date" and out["memory"]["source"] == "user_settings"
    edit_event = db.tables["assistant_memory_events"][-1]
    assert edit_event["payload"] == {"fields": ["title"]}
    assert repo.memory_apply(USER, "pin", actor="user", memory_id=MEMORY)["memory"]["pinned"] is True
    assert repo.memory_apply(USER, "unpin", actor="user", memory_id=MEMORY)["memory"]["pinned"] is False
    repo.memory_apply(USER, "pin", actor="user", memory_id=MEMORY)
    archived = repo.memory_apply(USER, "archive", actor="user", memory_id=MEMORY)["memory"]
    assert archived["status"] == "archived" and archived["pinned"] is False
    assert repo.memory_apply(USER, "archive", actor="user", memory_id=MEMORY)["replay"] is True
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "edit", actor="user", memory_id=MEMORY, fields={"title": "x"})
    assert exc.value.code == "memory_archived"
    assert repo.memory_apply(USER, "restore", actor="user", memory_id=MEMORY)["memory"]["status"] == "active"
    assert _events(db) == ["updated", "pinned", "unpinned", "pinned", "archived", "restored"]


def test_only_the_pin_op_pins(db):
    db.tables["assistant_memories"].append(memory_row())
    with pytest.raises(CheckViolation, match="memory_pin_user_only"):
        supabase_client.get_supabase().table("assistant_memories").update({"pinned": True}).eq("id", MEMORY).execute()
    with pytest.raises(CheckViolation, match="memory_pin_user_only"):
        supabase_client.get_supabase().table("assistant_memories").insert(
            memory_row(id=MEMORY2, pinned=True)).execute()
    assert _mem(db)["pinned"] is False
    repo.memory_apply(USER, "pin", actor="user", memory_id=MEMORY)
    assert _mem(db)["pinned"] is True
    # The flag is transaction-local: the next plain update cannot pin.
    db.tables["assistant_memories"].append(memory_row(id=MEMORY2))
    with pytest.raises(CheckViolation):
        supabase_client.get_supabase().table("assistant_memories").update({"pinned": True}).eq("id", MEMORY2).execute()


def test_pin_caps_map_to_memory_refused(db):
    rows = db.tables["assistant_memories"]
    rows.extend(memory_row(id=f"00000000-0000-4000-8000-{i:012d}", pinned=True, title=f"P{i}") for i in range(40))
    rows.append(memory_row())
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "pin", actor="user", memory_id=MEMORY)
    assert exc.value.code == "memory_cap_pinned" and _mem(db)["pinned"] is False
    del rows[1:40]
    _mem(db)["body"] = "x" * 4000
    rows[0]["body"] = "y" * 2500
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "pin", actor="user", memory_id=MEMORY)
    assert exc.value.code == "memory_cap_pinned_chars"


def test_restore_over_the_active_cap(db):
    rows = db.tables["assistant_memories"]
    rows.extend(memory_row(id=f"00000000-0000-4000-8000-{i:012d}", title=f"A{i}") for i in range(300))
    rows.append(memory_row(status="archived", archived_at=NOW.isoformat()))
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "restore", actor="user", memory_id=MEMORY)
    assert exc.value.code == "memory_cap_active" and _mem(db)["status"] == "archived"
    assert _events(db) == []


def test_the_fifty_first_pending_note_is_refused_and_a_replay_still_answers(db):
    rows = db.tables["assistant_memories"]
    rows.extend(memory_row(id=f"00000000-0000-4000-8000-{i:012d}", status="pending_review", source="inferred",
                           idempotency_key=f"key-{i:04d}-abcdef") for i in range(50))
    with pytest.raises(repo.MemoryRefused) as exc:
        _create(db, status="pending_review", source="inferred", key="key-new-abcdef")
    assert exc.value.code == "memory_cap_pending" and len(rows) == 50
    replay = _create(db, status="pending_review", source="inferred", key="key-0007-abcdef")
    assert replay["replay"] is True and replay["memory"]["id"].endswith("000000000007")
    assert _create(db, key="key-active-abcdef")["memory"]["status"] == "active"   # active notes are not capped here


# ── memory_apply: the assistant's ops and revisions ──────────────────────────


def test_forget_archives_and_refuses_a_pinned_note(db):
    db.tables["assistant_memories"].extend([memory_row(), memory_row(id=MEMORY2, pinned=True)])
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "forget", actor="assistant", memory_id=MEMORY2)
    assert exc.value.code == "memory_pinned_user_only" and _mem(db, MEMORY2)["status"] == "active"
    assert repo.memory_apply(USER, "forget", actor="assistant", memory_id=MEMORY)["memory"]["status"] == "archived"
    assert db.tables["assistant_memory_events"][-1]["payload"] == {"by": "forget"}
    assert repo.memory_apply(USER, "forget", actor="assistant", memory_id=MEMORY)["replay"] is True


def test_a_new_proposal_supersedes_the_pending_one(db):
    db.tables["assistant_memories"].append(memory_row())
    first = repo.memory_apply(USER, "propose_revision", actor="assistant", memory_id=MEMORY,
                              fields={"title": "Sort by due date", "origin_tainted": True},
                              idempotency_key="step-1-abcdef")
    assert first["revision"]["base_updated_at"] == _mem(db)["updated_at"]
    assert _mem(db)["title"] == "Sort bids by due time"          # the live text stays until approved
    replay = repo.memory_apply(USER, "propose_revision", actor="assistant", memory_id=MEMORY,
                               fields={"title": "ignored"}, idempotency_key="step-1-abcdef")
    assert replay["replay"] is True and replay["revision"]["id"] == first["revision"]["id"]
    second = repo.memory_apply(USER, "propose_revision", actor="assistant", memory_id=MEMORY,
                               fields={"title": "Sort by bid date"}, idempotency_key="step-2-abcdef")
    revs = {r["id"]: r for r in db.tables["assistant_memory_revisions"]}
    assert revs[first["revision"]["id"]]["decision"] == "rejected"
    assert [r for r in revs.values() if r["decided_at"] is None] == [revs[second["revision"]["id"]]]
    superseded = [e for e in db.tables["assistant_memory_events"] if e["event"] == "revision_rejected"]
    assert superseded[0]["actor"] == "system" and superseded[0]["payload"]["superseded"] is True
    out = repo.memory_apply(USER, "approve_revision", actor="user", memory_id=MEMORY,
                            fields={"revision_id": second["revision"]["id"]})
    assert out["memory"]["title"] == "Sort by bid date" and out["revision"]["decision"] == "approved"
    assert out["memory"]["origin_tainted"] is False
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "reject_revision", actor="user", memory_id=MEMORY,
                          fields={"revision_id": second["revision"]["id"]})
    assert exc.value.code == "revision_not_found"


def test_a_revision_goes_stale_when_the_note_changes_and_pinned_notes_take_none(db):
    db.tables["assistant_memories"].extend([memory_row(), memory_row(id=MEMORY2, pinned=True)])
    rev = repo.memory_apply(USER, "propose_revision", actor="assistant", memory_id=MEMORY,
                            fields={"body": "earliest first"})["revision"]
    repo.memory_apply(USER, "edit", actor="user", memory_id=MEMORY, fields={"body": "latest first"})
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "approve_revision", actor="user", memory_id=MEMORY, fields={"revision_id": rev["id"]})
    assert exc.value.code == "memory_stale" and exc.value.memory["body"] == "latest first"
    assert repo.memory_apply(USER, "reject_revision", actor="user", memory_id=MEMORY,
                             fields={"revision_id": rev["id"]})["revision"]["decision"] == "rejected"
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "propose_revision", actor="assistant", memory_id=MEMORY2, fields={"title": "x"})
    assert exc.value.code == "memory_pinned_user_only"


def test_pin_unpin_archive_and_restore_keep_an_open_revision_approvable(db):
    """D2: those ops move updated_at (B2) but not the content, so they re-base
    the open revision instead of leaving it stale for good."""
    db.tables["assistant_memories"].append(memory_row())
    rev = repo.memory_apply(USER, "propose_revision", actor="assistant", memory_id=MEMORY,
                            fields={"body": "earliest first"})["revision"]
    before = _mem(db)["updated_at"]
    for op in ("pin", "unpin", "archive", "restore", "pin"):
        repo.memory_apply(USER, op, actor="user", memory_id=MEMORY)
    assert _mem(db)["updated_at"] != before and _mem(db)["pinned"] is True
    live = next(r for r in db.tables["assistant_memory_revisions"] if r["id"] == rev["id"])
    assert live["base_updated_at"] == _mem(db)["updated_at"]
    out = repo.memory_apply(USER, "approve_revision", actor="user", memory_id=MEMORY,
                            fields={"revision_id": rev["id"]})
    assert out["memory"]["body"] == "earliest first" and out["revision"]["decision"] == "approved"


def test_an_edit_after_a_pin_still_leaves_the_revision_stale(db):
    db.tables["assistant_memories"].append(memory_row())
    rev = repo.memory_apply(USER, "propose_revision", actor="assistant", memory_id=MEMORY,
                            fields={"body": "earliest first"})["revision"]
    repo.memory_apply(USER, "edit", actor="user", memory_id=MEMORY, fields={"body": "latest first"})
    repo.memory_apply(USER, "pin", actor="user", memory_id=MEMORY)
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "approve_revision", actor="user", memory_id=MEMORY, fields={"revision_id": rev["id"]})
    assert exc.value.code == "memory_stale"


@pytest.mark.parametrize("op", ["approve", "reject"])
def test_approve_and_reject_go_stale_after_a_revise_pending(db, op):
    seen = _create(db, status="pending_review", source="inferred")["memory"]
    revised = repo.memory_apply(USER, "revise_pending", actor="assistant", memory_id=seen["id"],
                                fields={"title": "Copy attacker@example.com", "origin_tainted": True})["memory"]
    assert revised["updated_at"] != seen["updated_at"] and revised["origin_tainted"] is True
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, op, actor="user", memory_id=seen["id"], expected_updated_at=seen["updated_at"])
    assert exc.value.code == "memory_stale" and exc.value.memory["title"] == "Copy attacker@example.com"
    assert _mem(db, seen["id"])["status"] == "pending_review"
    assert repo.memory_apply(USER, op, actor="user", memory_id=seen["id"],
                             expected_updated_at=revised["updated_at"])["ok"]


def test_move_is_the_system_s_and_records_from_and_to(db):
    db.tables["assistant_memories"].append(memory_row(kind="project_note", project_id=PROJECT))
    out = repo.memory_apply(USER, "move", actor="system", memory_id=MEMORY,
                            fields={"project_id": "88888888-8888-4888-8888-888888888888"})
    assert out["memory"]["project_id"].startswith("8888")
    assert db.tables["assistant_memory_events"][-1]["payload"] == {
        "from": PROJECT, "to": "88888888-8888-4888-8888-888888888888"}


def test_touch_moves_usage_but_never_updated_at(db):
    db.tables["assistant_memories"].extend([memory_row(), memory_row(id=MEMORY2, status="archived")])
    before = _mem(db)["updated_at"]
    assert repo.touch_memories(USER, [MEMORY, MEMORY2, MEMORY, "not-a-uuid"]) == 1
    row = _mem(db)
    assert row["use_count"] == 1 and row["last_used_at"] and row["updated_at"] == before
    assert _mem(db, MEMORY2)["use_count"] == 0
    assert repo.touch_memories(USER, []) == 0
    touch = [c for c in db.rpc_calls if c[0] == "assistant_memory_touch"]
    assert touch[0][1]["p_ids"] == [MEMORY, MEMORY2]


def test_memory_similar_skips_archived_rows(db):
    db.tables["assistant_memories"].extend([
        memory_row(title="Sort bids by due time"), memory_row(id=MEMORY2, title="Sort bids by due date"),
        memory_row(id=MEMORY3, title="Sort bids by due time please", status="archived"),
    ])
    rows = repo.memory_similar(USER, "sort bids by due time")
    assert [r["id"] for r in rows] == [MEMORY, MEMORY2] and rows[0]["similarity"] == 1.0
    assert repo.memory_similar(USER, "Graybar gear") == [] and repo.memory_similar(USER, "  ") == []


# ── memory_apply: permanent delete ───────────────────────────────────────────


@pytest.mark.parametrize("actor", ["user", "system"])
def test_delete_is_refused_under_legal_hold_and_changes_nothing(db, actor):
    db.tables["assistant_profiles"][0]["legal_hold"] = True
    db.tables["assistant_memories"].append(memory_row(status="archived", archived_at=NOW.isoformat()))
    db.tables["assistant_memory_events"].append({"id": "e1", "memory_id": MEMORY, "user_id": USER,
                                                 "event": "archived", "actor": "user", "payload": {}})
    db.tables["assistant_messages"][1]["used_memory_ids"] = [MEMORY]
    before = json.dumps(db.tables, sort_keys=True, default=str)
    with pytest.raises(repo.MemoryRefused) as exc:
        repo.memory_apply(USER, "delete", actor=actor, memory_id=MEMORY)
    assert exc.value.code == "legal_hold"
    assert json.dumps(db.tables, sort_keys=True, default=str) == before


def test_delete_finds_a_listing_by_full_ids_and_redacts_by_call_id(db):
    short = MEMORY[:8]
    title = "Gate code at trailer"
    db.tables["assistant_memories"].append(memory_row(title=title, message_id=MSG_USER))
    listing = [
        {"type": "function_call", "call_id": "c1", "name": "memory_list", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1",
         "output": json.dumps({"items": [{"id": short, "title": title}]})},
        {"type": "function_call", "call_id": "c2", "name": "whats_due", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "{\"rows\": []}"},
    ]
    db.tables["assistant_messages"].append(message(
        id=MSG_OTHER, role="assistant", content="You have one note.",
        blocks=[{"type": "text", "text": "You have one note."}, {"type": "transcript", "items": listing}],
        used_memory_ids=[MEMORY, MEMORY2]))
    db.tables["assistant_tool_results"].extend([
        {"id": "tr1", "user_id": USER, "message_id": MSG_OTHER, "tool_name": "memory_list", "tainted": False,
         "payload": {"items": [{"id": short, "title": title}]}, "payload_sha256": "a" * 64,
         "taint_corpus": [{"f": "title", "t": title}], "memory_ids": [MEMORY]},
        {"id": "tr2", "user_id": USER, "message_id": MSG_OTHER, "tool_name": "whats_due", "tainted": False,
         "payload": {"rows": []}, "payload_sha256": "b" * 64, "taint_corpus": None},
    ])
    db.tables["assistant_memory_events"].append({"id": "e1", "memory_id": MEMORY, "user_id": USER,
                                                 "event": "created", "actor": "user", "payload": {}})
    out = repo.memory_apply(USER, "delete", actor="user", memory_id=MEMORY)
    assert out == {"ok": True, "deleted": True, "memory_id": MEMORY}
    assert _mem(db) is None and db.tables["assistant_memory_events"] == []
    msg = next(m for m in db.tables["assistant_messages"] if m["id"] == MSG_OTHER)
    items = msg["blocks"][1]["items"]
    assert items[0] == {"type": "function_call", "call_id": "c1", "name": "memory_list",
                        "arguments": '{"redacted":true}'}
    assert items[1] == {"type": "function_call_output", "call_id": "c1", "output": '{"redacted":true}'}
    assert items[2:] == listing[2:]
    assert msg["blocks"][0] == {"type": "text", "text": "You have one note."}
    assert msg["used_memory_ids"] == [MEMORY2]
    results = {t["id"]: t for t in db.tables["assistant_tool_results"]}
    assert results["tr1"]["payload"] == {"redacted": True}
    assert results["tr1"]["taint_corpus"] is None and results["tr1"]["payload_sha256"] is None
    assert results["tr2"]["payload"] == {"rows": []} and results["tr2"]["payload_sha256"] == "b" * 64
    assert title not in json.dumps(db.tables, default=str)


def test_delete_finds_the_saving_turn_by_the_memory_s_message(db):
    title = "Gate code at trailer"
    db.tables["assistant_memories"].append(memory_row(title=title, message_id=MSG_OTHER))
    save = [
        {"type": "function_call", "call_id": "c9", "name": "memory_save",
         "arguments": json.dumps({"kind": "reference", "title": title, "body": ""})},
        {"type": "function_call_output", "call_id": "c9", "output": json.dumps({"memory_id": MEMORY[:8]})},
    ]
    db.tables["assistant_messages"].append(message(
        id=MSG_OTHER, role="assistant", content="Saved.", blocks=[{"type": "transcript", "items": save}]))
    repo.memory_apply(USER, "delete", actor="user", memory_id=MEMORY)
    items = next(m for m in db.tables["assistant_messages"] if m["id"] == MSG_OTHER)["blocks"][0]["items"]
    assert [i.get("arguments", i.get("output")) for i in items] == ['{"redacted":true}', '{"redacted":true}']
    assert title not in json.dumps(db.tables, default=str)


# ── Schedules ────────────────────────────────────────────────────────────────


def _new_schedule(**over) -> dict:
    row = schedule(**over)
    for col in ("id", "user_id", "created_at", "updated_at"):
        row.pop(col, None)
    return row


@pytest.mark.parametrize("rule,code", [
    ("FREQ=HOURLY;BYMINUTE=0;BYSECOND=0,30", "schedule_interval"),
    ("FREQ=HOURLY;BYMINUTE=0;BYSECOND=0", "schedule_interval"),
    ("FREQ=MINUTELY;INTERVAL=30", "schedule_interval"),
    ("FREQ=HOURLY;BYMINUTE=0,30", "schedule_interval"),
    ("FREQ=DAILY;BYHOUR=7;BYMINUTE=0;COUNT=3", "schedule_rule"),
    ("FREQ=DAILY;BYHOUR=7;BYMINUTE=0;UNTIL=20271231T000000Z", "schedule_rule"),
])
def test_the_caps_trigger_refuses_rules_the_builder_never_writes(db, rule, code):
    with pytest.raises(repo.ScheduleRefused) as exc:
        repo.insert_schedule(USER, _new_schedule(rrule=rule))
    assert exc.value.code == code and db.tables["assistant_schedules"] == []


def test_insert_the_cap_and_the_approval_replay(db):
    row = repo.insert_schedule(USER, _new_schedule(approval_id="a" * 8 + "-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert row["status"] == "active" and row["rrule"] == WEEKDAYS_7AM
    with pytest.raises(repo.ScheduleExists):
        repo.insert_schedule(USER, _new_schedule(approval_id="a" * 8 + "-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert repo.schedule_by_approval(USER, "a" * 8 + "-aaaa-4aaa-8aaa-aaaaaaaaaaaa")["id"] == row["id"]
    for _ in range(9):
        repo.insert_schedule(USER, _new_schedule())
    assert repo.count_active_schedules(USER) == 10
    with pytest.raises(repo.ScheduleRefused) as exc:
        repo.insert_schedule(USER, _new_schedule())
    assert exc.value.code == "schedule_cap_active"
    assert repo.insert_schedule(USER, _new_schedule(status="paused", paused_reason="user"))["status"] == "paused"


def test_update_is_fenced_and_a_resume_over_the_cap_is_refused(db):
    db.tables["assistant_schedules"].extend(
        schedule(id=f"00000000-0000-4000-8000-{i:012d}") for i in range(10))
    db.tables["assistant_schedules"].append(schedule(id=SCHEDULE2, status="paused", paused_reason="user"))
    assert repo.update_schedule(USER, SCHEDULE2, {"status": "ended"}, expect_status="active") is None
    with pytest.raises(repo.ScheduleRefused) as exc:
        repo.update_schedule(USER, SCHEDULE2, {"status": "active", "paused_reason": None}, expect_status="paused")
    assert exc.value.code == "schedule_cap_active"
    assert repo.get_schedule(USER, SCHEDULE2)["status"] == "paused"
    ended = repo.update_schedule(USER, SCHEDULE2, {"status": "ended", "ended_at": NOW.isoformat(),
                                                   "ended_reason": "user"}, expect_status=("paused", "active"))
    assert ended["status"] == "ended" and ended["ended_reason"] == "user"
    with pytest.raises(CheckViolation, match="ended_chk"):
        repo.update_schedule(USER, "00000000-0000-4000-8000-000000000000", {"status": "ended"})


def test_fill_after_slot_never_moves_next_and_is_fenced(db):
    db.tables["assistant_schedules"].append(schedule(after_next_run_at=None))
    read = repo.schedules_to_refill(None)[0]
    later = (NOW + timedelta(days=2)).isoformat()
    assert repo.fill_after_slot(USER, SCHEDULE, next_run_at=read["next_run_at"], after_next_run_at=later)
    assert repo.get_schedule(USER, SCHEDULE)["next_run_at"] == read["next_run_at"]
    assert not repo.fill_after_slot(USER, SCHEDULE, next_run_at=read["next_run_at"], after_next_run_at=later)
    assert repo.schedules_to_refill(None) == []


def test_the_schedule_conversation_is_linked_once(db):
    db.tables["assistant_schedules"].append(schedule())
    assert repo.set_schedule_conversation(USER, SCHEDULE, CONV) is True
    assert repo.set_schedule_conversation(USER, SCHEDULE, MSG_OTHER) is False
    assert repo.get_schedule(USER, SCHEDULE)["conversation_id"] == CONV
    assert repo.schedule_for_conversation(USER, CONV)["id"] == SCHEDULE


def test_pause_schedules_moves_only_what_could_restart_on_its_own(db):
    ids = {name: f"00000000-0000-4000-8000-00000000000{i}" for i, name in enumerate(
        ("active", "feature_off", "mfa", "no_delegation", "user", "failures", "ended"))}
    db.tables["assistant_schedules"].extend([
        schedule(id=ids["active"]),
        schedule(id=ids["feature_off"], status="paused_feature_off"),
        schedule(id=ids["mfa"], status="paused", paused_reason="mfa_reset"),
        schedule(id=ids["no_delegation"], status="paused", paused_reason="no_delegation"),
        schedule(id=ids["user"], status="paused", paused_reason="user"),
        schedule(id=ids["failures"], status="paused", paused_reason="failures"),
        schedule(id=ids["ended"], status="ended", ended_at=NOW.isoformat(), ended_reason="user"),
    ])
    assert repo.pause_schedules(USER, "mfa_reset") == 1
    assert repo.get_schedule(USER, ids["active"])["paused_reason"] == "mfa_reset"
    assert repo.pause_schedules(USER, "owner_offboarded", include_resumable_paused=True) == 4
    rows = {r["id"]: r for r in db.tables["assistant_schedules"]}
    for name in ("active", "feature_off", "mfa", "no_delegation"):
        assert (rows[ids[name]]["status"], rows[ids[name]]["paused_reason"]) == ("paused", "owner_offboarded")
        assert rows[ids[name]]["paused_at"]
    assert rows[ids["user"]]["paused_reason"] == "user" and rows[ids["failures"]]["paused_reason"] == "failures"
    assert rows[ids["ended"]]["status"] == "ended"


def test_seen_lines_and_lists(db):
    db.tables["assistant_schedules"].extend([
        schedule(unread_deliveries=3), schedule(id=SCHEDULE2, status="paused", paused_reason="user",
                                                unread_deliveries=2),
        schedule(id=MEMORY3, user_id=OTHER_USER, unread_deliveries=5),
    ])
    assert repo.mark_schedules_seen(USER, [SCHEDULE, SCHEDULE2, MEMORY3]) == 2
    assert {r["id"]: r["unread_deliveries"] for r in db.tables["assistant_schedules"]} == {
        SCHEDULE: 0, SCHEDULE2: 0, MEMORY3: 5}
    assert [r["id"] for r in repo.schedule_lines(USER)] == [SCHEDULE, SCHEDULE2]
    assert [r["id"] for r in repo.list_schedules(USER, status=("paused", "paused_feature_off"))] == [SCHEDULE2]


# ── The claim (0176 A4) ──────────────────────────────────────────────────────


def _due(**over) -> dict:
    return schedule(**{"next_run_at": (NOW - timedelta(minutes=1)).isoformat(),
                       "after_next_run_at": (NOW + timedelta(days=1)).isoformat(), **over})


def test_two_workers_claim_each_due_occurrence_once(db):
    db.tables["assistant_schedules"].extend([_due(), _due(id=SCHEDULE2)])
    got: list[list[dict]] = []
    threads = [threading.Thread(target=lambda w=w: got.append(
        repo.claim_due_schedules(None, worker=w, limit=20, catchup_seconds=10800))) for w in ("a", "b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    runs = [r for batch in got for r in batch]
    assert sorted(r["schedule_id"] for r in runs) == sorted([SCHEDULE, SCHEDULE2])
    assert all(r["status"] == "claimed" and r["user_label"] == "Tom Moore" for r in runs)
    row = repo.get_schedule(USER, SCHEDULE)
    assert row["next_run_at"] == (NOW + timedelta(days=1)).isoformat() and row["after_next_run_at"] is None
    assert row["last_run_at"] == (NOW - timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError):
        repo.claim_due_schedules(USER, worker="a", limit=20, catchup_seconds=10800)


def test_a_locked_schedule_is_skipped_and_a_held_occurrence_is_not_claimed_twice(db):
    db.tables["assistant_schedules"].append(_due())
    db.locked_schedules.add(SCHEDULE)
    assert repo.claim_due_schedules(None, worker="b", limit=20, catchup_seconds=10800) == []
    db.locked_schedules.clear()
    # The occurrence already has a run (the other worker's insert): advanced, nothing new returned.
    db.tables["assistant_schedule_runs"].append(schedule_run(scheduled_for=(NOW - timedelta(minutes=1)).isoformat()))
    assert repo.claim_due_schedules(None, worker="b", limit=20, catchup_seconds=10800) == []
    assert repo.get_schedule(USER, SCHEDULE)["after_next_run_at"] is None
    assert len(db.tables["assistant_schedule_runs"]) == 1


def test_an_unfilled_second_slot_is_not_claimable(db):
    db.tables["assistant_schedules"].append(_due(after_next_run_at=None))
    assert repo.claim_due_schedules(None, worker="a", limit=20, catchup_seconds=10800) == []


def test_missed_and_overlap_are_recorded_as_skipped(db):
    db.tables["assistant_schedules"].extend([
        _due(next_run_at=(NOW - timedelta(days=2)).isoformat()),
        _due(id=SCHEDULE2),
    ])
    db.tables["assistant_schedule_runs"].append(schedule_run(
        schedule_id=SCHEDULE2, status="running", scheduled_for=(NOW - timedelta(hours=1)).isoformat()))
    runs = {r["schedule_id"]: r for r in repo.claim_due_schedules(None, worker="a", limit=20, catchup_seconds=10800)}
    assert (runs[SCHEDULE]["status"], runs[SCHEDULE]["error_kind"]) == ("skipped", "missed")
    assert (runs[SCHEDULE2]["status"], runs[SCHEDULE2]["error_kind"]) == ("skipped", "overlap")
    assert runs[SCHEDULE]["finished_at"]


def test_run_now_rows_wait_while_another_run_is_held_and_block_a_scheduled_slot(db):
    db.tables["assistant_schedules"].append(schedule())
    manual = schedule_run(id="r-now", kind="run_now", claimed_by=None, started_at=None,
                          scheduled_for=NOW.replace(microsecond=NOW.microsecond or 1).isoformat())
    held = schedule_run(id="r-held", status="running", scheduled_for=(NOW - timedelta(hours=1)).isoformat())
    db.tables["assistant_schedule_runs"].extend([manual, held])
    assert repo.claim_due_schedules(None, worker="a", limit=20, catchup_seconds=10800) == []
    db.tables["assistant_schedule_runs"][1]["status"] = "delivered"
    taken = repo.claim_due_schedules(None, worker="a", limit=20, catchup_seconds=10800)
    assert [(r["id"], r["claimed_by"]) for r in taken] == [("r-now", "a")]
    # While that Run now row is open, a scheduled slot of the schedule comes due: skipped overlap.
    sched = db.tables["assistant_schedules"][0]
    sched["next_run_at"] = (NOW - timedelta(minutes=1)).isoformat()
    runs = repo.claim_due_schedules(None, worker="b", limit=20, catchup_seconds=10800)
    assert [(r["kind"], r["status"], r["error_kind"]) for r in runs] == [("scheduled", "skipped", "overlap")]
    db.locked_runs.add("r-other")
    assert repo.claim_due_schedules(None, worker="b", limit=20, catchup_seconds=10800) == []


# ── Runs and the channel ledger ──────────────────────────────────────────────


def test_runs_insert_fence_forward_and_never_delete(db):
    db.tables["assistant_schedules"].append(schedule())
    when = NOW.replace(microsecond=NOW.microsecond or 1).isoformat()
    row = repo.insert_run(USER, {"schedule_id": SCHEDULE, "user_label": "Tom Moore", "scheduled_for": when,
                                 "kind": "run_now", "status": "claimed", "claimed_by": None})
    with pytest.raises(repo.RunExists):
        repo.insert_run(USER, {"schedule_id": SCHEDULE, "user_label": "Tom Moore", "scheduled_for": when,
                               "kind": "run_now"})
    assert repo.open_run(USER, SCHEDULE)["id"] == row["id"]
    assert repo.update_run(USER, row["id"], {"status": "running"}, expect_status="claimed")
    assert not repo.update_run(USER, row["id"], {"status": "running"}, expect_status="claimed")
    assert repo.update_run(USER, row["id"], {"status": "delivered", "finished_at": NOW.isoformat()})
    with pytest.raises(Exception, match="forward only"):
        repo.update_run(USER, row["id"], {"status": "failed"})
    with pytest.raises(Exception, match="immutable"):
        repo.update_run(USER, row["id"], {"kind": "scheduled"})
    with pytest.raises(Exception, match="append-only"):
        supabase_client.get_supabase().table("assistant_schedule_runs").delete().eq("id", row["id"]).execute()
    assert repo.open_run(USER, SCHEDULE) is None and repo.last_run(USER, SCHEDULE)["id"] == row["id"]
    assert repo.last_runs(USER, [SCHEDULE, SCHEDULE2]) == {SCHEDULE: repo.get_run(USER, row["id"])}
    assert [r["id"] for r in repo.list_runs(USER, SCHEDULE)] == [row["id"]]


def test_each_channel_is_claimed_once(db):
    db.tables["assistant_schedule_runs"].append(schedule_run(status="running"))
    run = schedule_run()["id"]
    assert repo.run_channel(USER, run, "chat", "claim") is True
    assert repo.run_channel(USER, run, "chat", "claim") is False
    assert repo.run_channel(USER, run, "chat", "done") is True
    assert repo.run_channel(USER, run, "chat", "done") is False
    assert repo.run_channel(USER, run, "email", "done", "log-1") is False        # never claimed
    assert repo.run_channel(USER, run, "email", "claim") is True
    assert repo.run_channel(USER, run, "email", "failed") is True
    assert repo.run_channel(USER, run, "bell", "claim") and repo.run_channel(USER, run, "bell", "done", "x")
    assert repo.get_run(USER, run)["delivered"] == {"chat": True, "email": False, "bell": "x"}
    assert repo.run_channel(OTHER_USER, run, "bell", "claim") is False
    with pytest.raises(CheckViolation):
        repo.run_channel(USER, run, "sms", "claim")


def test_stale_runs_and_paused_rows_for_the_sweeps(db):
    old = (NOW - timedelta(minutes=20)).isoformat()
    db.tables["assistant_schedule_runs"].extend([
        schedule_run(id="taken", status="running", started_at=old, scheduled_for=old),
        schedule_run(id="unpicked", kind="run_now", claimed_by=None, started_at=None, created_at=old,
                     scheduled_for=(NOW - timedelta(minutes=19)).isoformat()),
        schedule_run(id="fresh", scheduled_for=NOW.isoformat()),
    ])
    stale = repo.stale_runs(None, older_than_seconds=900)
    assert {r["id"]: r["claimed_by"] for r in stale} == {"taken": "worker-a", "unpicked": None}
    db.tables["assistant_schedules"].extend([
        schedule(id=SCHEDULE, status="paused_feature_off", paused_at=old),
        schedule(id=SCHEDULE2, status="paused", paused_reason="no_delegation", paused_at=NOW.isoformat()),
        schedule(id=MEMORY3, status="paused", paused_reason="user"),
    ])
    assert [r["id"] for r in repo.paused_to_check(None)] == [SCHEDULE, SCHEDULE2]


# ── Trusted turn context (3.7.3) ─────────────────────────────────────────────


def _begun(key: str, **over) -> dict:
    row = {"id": "tc-1", "user_id": USER, "user_label": "Tom Moore", "conversation_id": CONV,
           "message_id": MSG_ASSISTANT, "run_id": RUN, "seq": 1, "idempotency_key": key, "phase": "begun",
           "lease_owner": "lease", "tool_name": "memory_save", "effect": "housekeeping",
           "tainted_context": False, "created_at": NOW.isoformat()}
    row.update(over)
    return row


def test_turn_context_reads_only_the_server_s_rows(db):
    db.tables["assistant_tool_calls"].append(_begun(KEY))
    db.tables["assistant_messages"][0]["content"] = "Remember that I want bids sorted by due time"
    ctx = repo.turn_context(USER, RUN, KEY, tool_name="memory_save")
    assert ctx == {
        "conversation_id": CONV, "message_id": MSG_ASSISTANT, "tool_name": "memory_save", "tainted": False,
        "mailbox_tainted": False, "mode": "normal", "incognito": False,
        "user_text": "Remember that I want bids sorted by due time",
        "user_texts": ["Remember that I want bids sorted by due time"], "step_key": KEY, "idempotency_key": KEY,
    }
    assert repo.turn_context(USER, RUN, KEY, tool_name="memory_forget") is None
    assert repo.turn_context(USER, RUN, KEY + ":r1") is None
    assert repo.turn_context(USER, "other-run", KEY) is None
    assert repo.turn_context(OTHER_USER, RUN, KEY) is None
    assert repo.turn_context(USER, None, KEY) is None


def test_turn_context_takes_taint_from_the_begun_row_when_the_conversation_flag_is_missing(db):
    retry = KEY + ":r2"
    db.tables["assistant_tool_calls"].append(_begun(retry, tainted_context=True))
    ctx = repo.turn_context(USER, RUN, retry)
    assert ctx["tainted"] is True and ctx["step_key"] == KEY
    assert db.tables["assistant_conversations"][0]["tainted_at"] is None


def test_turn_context_mailbox_mode_incognito_and_the_latest_question(db):
    db.tables["assistant_tool_calls"].append(_begun(KEY))
    db.tables["assistant_conversations"][0].update(mailbox_tainted_at=NOW.isoformat(), mode="mailbox_review")
    db.tables["assistant_messages"][0]["incognito"] = True
    db.tables["assistant_messages"].insert(0, message(id=MSG_OTHER, content="older question",
                                                      created_at=(NOW - timedelta(minutes=3)).isoformat()))
    db.tables["assistant_messages"].append(message(id="later", content="a later question",
                                                   created_at=(NOW + timedelta(minutes=3)).isoformat()))
    ctx = repo.turn_context(USER, RUN, KEY)
    assert ctx["mailbox_tainted"] is True and ctx["mode"] == "mailbox_review" and ctx["incognito"] is True
    assert ctx["user_text"] == "What is due today?"
    db.tables["assistant_conversations"][0]["tainted_at"] = NOW.isoformat()
    assert repo.turn_context(USER, RUN, KEY)["tainted"] is True


# ── Reads the routes and the loop use ────────────────────────────────────────


def test_conversations_by_surface_and_memories_by_ids(db):
    db.tables["assistant_conversations"].append(conversation(id=MSG_OTHER, surface="schedule"))
    assert {c["id"] for c in repo.list_conversations(USER)} == {CONV, MSG_OTHER}
    assert [c["id"] for c in repo.list_conversations(USER, surfaces=("panel", "page", "task"))] == [CONV]
    db.tables["assistant_memories"].extend([memory_row(), memory_row(id=MEMORY2, title="Second", kind="reference")])
    assert repo.memories_by_ids(USER, [MEMORY2, MEMORY3, MEMORY, MEMORY2]) == [
        {"id": MEMORY2, "title": "Second", "kind": "reference"},
        {"id": MEMORY, "title": "Sort bids by due time", "kind": "preference"}]
    assert repo.memories_by_ids(USER, []) == []
    assert [r["id"] for r in repo.load_prompt_memories(USER)] == [MEMORY, MEMORY2]


def test_insert_tool_result_passes_memory_ids_through(db):
    repo.insert_tool_result(USER, {"tool_name": "memory_list", "payload": {}, "memory_ids": [MEMORY, MEMORY, MEMORY2]})
    repo.insert_tool_result(USER, {"tool_name": "memory_list", "payload": {}, "memory_ids": []})
    repo.insert_tool_result(USER, {"tool_name": "whats_due", "payload": {}})
    rows = db.tables["assistant_tool_results"]
    assert rows[0]["memory_ids"] == [MEMORY, MEMORY2] and rows[1]["memory_ids"] is None
    assert "memory_ids" not in rows[2]


def test_summary_and_pending_revisions(db):
    db.tables["assistant_memories"].extend([
        memory_row(pinned=True, title="abc", body="de"), memory_row(id=MEMORY2, status="pending_review"),
        memory_row(id=MEMORY3, status="archived")])
    db.tables["assistant_memory_revisions"].append({"id": "rv1", "memory_id": MEMORY, "user_id": USER, "title": "x",
                                                    "body": "", "decided_at": None, "decision": None,
                                                    "created_at": NOW.isoformat()})
    assert repo.memory_summary(USER) == {"active": 1, "active_cap": 300, "pinned": 1, "pinned_cap": 40,
                                         "pinned_chars": 5, "pinned_chars_cap": 6000, "pending_review": 1,
                                         "pending_revisions": 1}
    assert repo.pending_revisions(USER, [MEMORY, MEMORY2]) == {MEMORY: repo.pending_revisions(USER, [MEMORY])[MEMORY]}
    assert {m["id"] for m in repo.list_memories(USER, status=("active", "pending_review"))} == {MEMORY, MEMORY2}
    assert [m["id"] for m in repo.list_memories(USER, kind="preference", status="archived")] == [MEMORY3]


def test_holidays_are_stored_sorted_with_the_admin(db):
    out = repo.update_holidays(USER, ["2027-01-01", "2026-12-25", "2027-01-01"])
    assert out["holidays"] == ["2026-12-25", "2027-01-01"] and out["updated_at"]
    assert db.tables["assistant_settings"][0]["updated_by"] == USER


def test_a_sweep_is_claimed_once_per_day(db):
    day = NOW.date()
    assert repo.claim_sweep(None, "nightly", day, worker="a") is True
    assert repo.claim_sweep(None, "nightly", day, worker="b") is False
    assert repo.claim_sweep(None, "orphans", day, worker="b") is True
    assert repo.finish_sweep(None, "nightly", day, {"archived": 2}) is True
    assert repo.finish_sweep(None, "nightly", day, {"archived": 3}) is False
    row = next(r for r in db.tables["assistant_sweeps"] if r["step"] == "nightly")
    assert row["claimed_by"] == "a" and row["result"] == {"archived": 2}
    with pytest.raises(ValueError):
        repo.claim_sweep(USER, "nightly", day, worker="a")


# ── Offboarding (3.10.3) and the system sweeps ───────────────────────────────


def test_offboarding_reads_and_writes(db):
    db.tables["assistant_delegations"] = [delegation()]
    db.tables["assistant_approvals"] = [
        approval(), approval(id="appr-2", status="approved"), approval(id="appr-3", status="executed"),
        approval(id="appr-4", user_id=OTHER_USER)]
    db.tables["assistant_tasks"] = [task(), task(id="t2", status="waiting_approval", approval_id=None),
                                    task(id="t3", status="done", approval_id=None)]
    db.tables["assistant_spend_reservations"] = [reservation(), reservation(id="h2", status="released", task_id=None)]
    assert repo.revoke_delegation(USER, "deactivated") == 1 and repo.revoke_delegation(USER, "deactivated") == 0
    assert db.tables["assistant_delegations"][0]["revoked_reason"] == "deactivated"
    assert [a["id"] for a in repo.open_approvals(USER)] == [approval()["id"], "appr-2"]
    moved = repo.expire_approvals(USER, [approval()["id"], "appr-2", "appr-3", "appr-4"], "owner_offboarded")
    assert sorted(m["id"] for m in moved) == sorted([approval()["id"], "appr-2"])
    statuses = {a["id"]: (a["status"], a["expired_reason"]) for a in db.tables["assistant_approvals"]}
    assert statuses["appr-2"] == ("expired", "owner_offboarded") and statuses["appr-3"][0] == "executed"
    assert statuses["appr-4"] == ("pending", None)
    assert {t["id"] for t in repo.open_tasks(USER)} == {task()["id"], "t2"}
    assert [h["id"] for h in repo.active_holds(USER)] == [reservation()["id"]]


def test_owners_with_open_state_and_the_purge_reads_skip_legal_hold(db):
    db.tables["assistant_delegations"] = [delegation(), delegation(id="d2", user_id=OTHER_USER,
                                                                   revoked_at=NOW.isoformat())]
    db.tables["assistant_schedules"].append(schedule(user_id=OTHER_USER))
    assert repo.owners_with_open_state(None) == sorted([USER, OTHER_USER])
    assert repo.owners_with_open_state(USER) == [USER]
    expired = (NOW - timedelta(days=1)).isoformat()
    db.tables["assistant_tool_results"].extend([
        {"id": "tr1", "user_id": USER, "tool_name": "x", "payload": {}, "expires_at": expired},
        {"id": "tr2", "user_id": OTHER_USER, "tool_name": "x", "payload": {}, "expires_at": expired},
        {"id": "tr3", "user_id": USER, "tool_name": "x", "payload": {}, "expires_at": (NOW + timedelta(days=1)).isoformat()},
    ])
    year_ago = (NOW - timedelta(days=400)).isoformat()
    db.tables["assistant_memories"].extend([
        memory_row(status="archived", archived_at=year_ago),
        memory_row(id=MEMORY2, user_id=OTHER_USER, status="archived", archived_at=year_ago)])
    db.tables["assistant_profiles"].append({"user_id": OTHER_USER, "legal_hold": True})
    assert [r["id"] for r in repo.tool_results_to_purge(None)] == ["tr1"]
    assert repo.tool_results_to_purge(OTHER_USER) == []
    assert [r["id"] for r in repo.memories_to_purge(None)] == [MEMORY]
    assert repo.delete_tool_results(USER, ["tr1", "tr2"]) == 1
    assert {t["id"] for t in db.tables["assistant_tool_results"]} == {"tr2", "tr3"}


def test_auto_archive_candidates(db):
    old = (NOW - timedelta(days=200)).isoformat()
    db.tables["assistant_memories"].extend([
        memory_row(id="m-used-long-ago", last_used_at=old),
        memory_row(id="m-never-used", updated_at=old),
        memory_row(id="m-pinned", pinned=True, updated_at=old),
        memory_row(id="m-recent", last_used_at=NOW.isoformat(), updated_at=old),
        memory_row(id="m-pending", status="pending_review", updated_at=(NOW - timedelta(days=31)).isoformat()),
        memory_row(id="m-pending-new", status="pending_review", updated_at=NOW.isoformat()),
        memory_row(id="m-note", kind="project_note", project_id=PROJECT),
    ])
    assert {r["id"] for r in repo.memories_to_auto_archive(None)} == {"m-used-long-ago", "m-never-used", "m-pending"}
    assert [r["id"] for r in repo.project_notes_for_review(None)] == ["m-note"]


def test_the_approval_sweep_reads(db):
    decided = (NOW - timedelta(minutes=20)).isoformat()
    db.tables["assistant_approvals"] = [
        approval(id="p1", expires_at=(NOW - timedelta(minutes=1)).isoformat(), project_id=None),
        approval(id="p2"),
        approval(id="a1", status="approved", decided_at=decided),
        approval(id="a2", status="approved", decided_at=NOW.isoformat()),
    ]
    assert [a["id"] for a in repo.pending_approvals_due(None)] == ["p1", "p2"]
    assert [a["id"] for a in repo.pending_approvals_with_project(None)] == ["p2"]
    assert [a["id"] for a in repo.approved_stranded(None)] == ["a1"]


def test_pending_approvals_due_takes_an_optional_lower_bound_on_expires_at(db):
    """Without expires_after every pending card is read, expired first (the
    approvals sweep); with it, the long-expired backlog is skipped so a
    bounded page still reaches the cards about to expire."""
    db.tables["assistant_approvals"] = [
        approval(id=f"old{n}", expires_at=(NOW - timedelta(hours=5, minutes=n)).isoformat()) for n in range(3)
    ] + [
        approval(id="edge", expires_at=(NOW - timedelta(minutes=5)).isoformat()),
        approval(id="soon", expires_at=(NOW + timedelta(minutes=30)).isoformat()),
        approval(id="done", status="approved", expires_at=(NOW + timedelta(minutes=10)).isoformat()),
    ]
    assert [a["id"] for a in repo.pending_approvals_due(None, limit=2)] == ["old2", "old1"]
    bounded = repo.pending_approvals_due(None, limit=2, expires_after=NOW - timedelta(minutes=5))
    assert [a["id"] for a in bounded] == ["edge", "soon"]
    assert repo.pending_approvals_due(None, expires_after=NOW + timedelta(hours=1)) == []
    with pytest.raises(ValueError):
        repo.pending_approvals_due(None, expires_after=datetime(2026, 10, 30, 12, 0))  # naive time refused
