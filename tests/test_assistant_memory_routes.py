"""The memory API (app/routers/assistant_memory.py) against
docs/ASSISTANT_PHASE3_PLAN.md 3.8 and 5.3, over the fake
assistant_memory_apply RPC.

Invariants pinned here:

  provenance  memory_save derives status, source and origin_tainted from
              the begun ledger row's turn: words the user typed in the
              current message of an untainted turn save active
              (user_stated); anything else waits for the Save (inferred);
              a tainted or mailbox-tainted turn marks origin_tainted; the
              step key makes a repeat a replay;
  refusals    no turn context, Mailbox review mode and Incognito are 409;
              the secret filter answers 422 memory_sensitive with the kind
              and never the text, on save, on revise, on create and on
              PATCH; 51 pending notes is 409 memory_cap_pending; forget is
              refused in a tainted turn and on a pinned note; a pinned note
              is never revised by the assistant;
  revise      a pending note is revised in place; an active one gets a
              proposed revision, and a newer one supersedes the older;
  settings    every Memory tab op; approve and reject need
              expected_updated_at (422 without) and a stale one is 409
              memory_stale with the current row; pin of an untrusted note
              warns; delete is 204, scrubs used_memory_ids and leaves a
              text-free tombstone, and is 409 legal_hold under a hold;
  who         the tool routes 404 a human; the Memory tab refuses an
              assistant principal.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access
from app.core import supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant_memory as memory_routes

from tests.assistant_fakes import (
    CONV, MEMORY, MEMORY2, MSG_ASSISTANT, NOW, PROJECT, RUN, USER, FakeDB, conversation, memory_row, message,
    pending_answer, profile, settings_row,
)

HEADER = "X-Assistant-Idempotency-Key"
KEY = "ledger-key-1"
SECRET_TEXT = "The gate password: hunter2hunter2"


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_conversations=[], assistant_messages=[], assistant_tool_calls=[], assistant_tool_results=[],
        assistant_memories=[], assistant_memory_events=[], assistant_memory_revisions=[], audit_log=[],
        projects=[{"id": PROJECT, "number": "26.10.7267", "name": "Alpha Clinic"}],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    monkeypatch.setattr(get_settings(), "bidding_enabled", True)
    from app.services import notifications

    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    state = SimpleNamespace(role=Role.EXECUTIVE, via=None)

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    app = FastAPI()
    app.include_router(memory_routes.router)
    app.dependency_overrides[get_current_user] = _user
    return SimpleNamespace(client=TestClient(app), db=db, state=state)


def _turn(api, tool: str, *, said="Remember that I want bids sorted by due time.", tainted=False,
          mailbox=False, incognito=False, mode="normal", key=KEY):
    """The turn whose begun ledger row the tool routes trust."""
    api.state.via = RUN
    api.db.tables["assistant_conversations"] = [conversation(
        mode=mode, mailbox_tainted_at=NOW.isoformat() if mailbox else None)]
    api.db.tables["assistant_messages"] = [message(content=said, incognito=incognito),
                                           pending_answer(status="running", incognito=incognito)]
    api.db.tables["assistant_tool_calls"].append({
        "id": f"tc-{len(api.db.tables['assistant_tool_calls'])}", "user_id": USER, "run_id": RUN,
        "idempotency_key": key, "phase": "begun", "tool_name": tool, "conversation_id": CONV,
        "message_id": MSG_ASSISTANT, "tainted_context": tainted, "seq": 1,
    })
    return {HEADER: key}


def _human(api):
    api.state.via = None


def _save(api, headers, **body):
    payload = {"kind": "preference", "title": "Bids sorted by due time", "body": "", "project_id": None}
    payload.update(body)
    return api.client.post("/assistant/tools/memory/save", json=payload, headers=headers)


def _events(api) -> list[str]:
    return [e["event"] for e in api.db.tables["assistant_memory_events"]]


# ── memory_save ──────────────────────────────────────────────────────────────


def test_save_of_the_users_own_words_is_active_and_a_repeat_is_a_replay(api):
    headers = _turn(api, "memory_save")
    res = _save(api, headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "active" and body["source"] == "user_stated" and body["origin_tainted"] is False
    assert body["show_note"] is True and body["message"] == "Saved." and "pinned" not in body
    row = api.db.tables["assistant_memories"][0]
    assert row["id"] == body["memory_id"] and row["conversation_id"] == CONV and row["message_id"] == MSG_ASSISTANT
    assert row["idempotency_key"] == KEY and _events(api) == ["created"]
    again = _save(api, headers)
    assert again.status_code == 200 and again.json()["memory_id"] == body["memory_id"]
    assert len(api.db.tables["assistant_memories"]) == 1


def test_a_retry_suffix_still_maps_to_the_same_step(api):
    _turn(api, "memory_save", key=f"{KEY}:r1")
    res = _save(api, {HEADER: f"{KEY}:r1"})
    assert res.status_code == 200, res.text
    assert api.db.tables["assistant_memories"][0]["idempotency_key"] == KEY


def test_save_of_words_the_user_never_typed_waits_for_the_save(api):
    headers = _turn(api, "memory_save", said="What is due today?")
    body = _save(api, headers).json()
    assert body["status"] == "pending_review" and body["source"] == "inferred" and body["origin_tainted"] is False
    assert body["message"] == "Waiting for the user to click Save."


@pytest.mark.parametrize("taint", ["begun", "mailbox"])
def test_save_in_a_tainted_turn_is_pending_and_origin_tainted(api, taint):
    headers = _turn(api, "memory_save", tainted=taint == "begun", mailbox=taint == "mailbox")
    body = _save(api, headers).json()
    assert body["status"] == "pending_review" and body["origin_tainted"] is True
    assert api.db.tables["assistant_memories"][0]["origin_tainted"] is True


def test_save_returns_near_duplicates_but_never_itself(api):
    api.db.tables["assistant_memories"] = [memory_row(title="Bids sorted by due time please")]
    headers = _turn(api, "memory_save")
    body = _save(api, headers).json()
    assert [s["id"] for s in body["similar"]] == [MEMORY]
    again = _save(api, headers).json()
    assert body["memory_id"] not in [s["id"] for s in again["similar"]]


def test_save_refusals(api):
    res = _save(api, _turn(api, "memory_save", mode="mailbox_review"))
    assert res.status_code == 409 and res.json()["detail"]["code"] == "mailbox_review"
    res = _save(api, _turn(api, "memory_save", incognito=True, key="k2"))
    assert res.status_code == 409 and res.json()["detail"]["code"] == "incognito"
    headers = _turn(api, "memory_save", key="k3")
    res = _save(api, headers, kind="secret")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_memory"
    res = _save(api, headers, title="   ")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_memory"
    res = _save(api, headers, kind="project_note")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_memory"
    res = _save(api, headers, kind="project_note", project_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "project_not_found"
    res = _save(api, headers, status="active")                     # no status can ride along
    assert res.status_code == 422
    res = _save(api, headers, title="Gate code", body=SECRET_TEXT)
    assert res.status_code == 422 and res.json()["detail"]["code"] == "memory_sensitive"
    assert res.json()["detail"]["kind"] == "credential" and "hunter2" not in res.text
    res = api.client.post("/assistant/tools/memory/save", json={"kind": "preference", "title": "x"})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "turn_context_unavailable"
    res = _save(api, _turn(api, "memory_forget", key="k4"))          # begun for another tool
    assert res.status_code == 409 and res.json()["detail"]["code"] == "turn_context_unavailable"
    assert api.db.tables["assistant_memories"] == []


def test_a_project_note_names_its_project(api):
    headers = _turn(api, "memory_save", said="Note for this job: the owner wants Square D gear only.")
    res = _save(api, headers, kind="project_note", title="Owner wants Square D gear only", project_id=PROJECT)
    assert res.status_code == 200, res.text
    assert api.db.tables["assistant_memories"][0]["project_id"] == PROJECT
    assert res.json()["status"] == "active"


def test_the_51st_pending_note_is_refused(api):
    api.db.tables["assistant_memories"] = [
        memory_row(id=f"cdcdcdcd-cdcd-4dcd-8dcd-{i:012d}", status="pending_review", source="inferred",
                   title=f"Pending {i}") for i in range(50)]
    headers = _turn(api, "memory_save", said="What is due today?")
    res = _save(api, headers, title="Something new")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_cap_pending"
    assert len(api.db.tables["assistant_memories"]) == 50


# ── memory_update ────────────────────────────────────────────────────────────


def test_revise_a_pending_note_in_place(api):
    api.db.tables["assistant_memories"] = [memory_row(status="pending_review", source="inferred")]
    headers = _turn(api, "memory_update", tainted=True)
    res = api.client.post(f"/assistant/tools/memory/{MEMORY}/revise",
                          json={"title": "Sort bids by internal due time"}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"memory_id": MEMORY, "revision_id": None, "status": "pending_review", "show_note": True}
    row = api.db.tables["assistant_memories"][0]
    assert row["title"] == "Sort bids by internal due time" and row["status"] == "pending_review"
    assert row["origin_tainted"] is True


def test_revise_an_active_note_proposes_and_a_newer_proposal_supersedes(api):
    api.db.tables["assistant_memories"] = [memory_row()]
    first = api.client.post(f"/assistant/tools/memory/{MEMORY}/revise", json={"body": "Oldest first."},
                            headers=_turn(api, "memory_update")).json()
    assert first["status"] == "revision_pending" and first["revision_id"]
    assert api.db.tables["assistant_memories"][0]["title"] == "Sort bids by due time"    # live text stays
    second = api.client.post(f"/assistant/tools/memory/{MEMORY}/revise", json={"body": "Newest first."},
                             headers=_turn(api, "memory_update", key="k2")).json()
    revs = {r["id"]: r for r in api.db.tables["assistant_memory_revisions"]}
    assert revs[first["revision_id"]]["decision"] == "rejected"
    assert revs[second["revision_id"]]["decided_at"] is None
    assert "revision_rejected" in _events(api)


def test_revise_refusals(api):
    api.db.tables["assistant_memories"] = [memory_row(pinned=True), memory_row(id=MEMORY2, status="archived",
                                                                               archived_at=NOW.isoformat())]
    headers = _turn(api, "memory_update")
    url = f"/assistant/tools/memory/{MEMORY}/revise"
    res = api.client.post(url, json={"title": "New"}, headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_pinned_user_only"
    res = api.client.post(f"/assistant/tools/memory/{MEMORY2}/revise", json={"title": "New"}, headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_archived"
    api.db.tables["assistant_memories"][0]["pinned"] = False
    res = api.client.post(url, json={}, headers=headers)
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_memory"
    res = api.client.post(url, json={"body": SECRET_TEXT}, headers=headers)
    assert res.status_code == 422 and res.json()["detail"]["code"] == "memory_sensitive"
    res = api.client.post(url, json={"body": "x", "expected_updated_at": NOW.isoformat()}, headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_stale"
    assert res.json()["detail"]["memory"]["id"] == MEMORY
    res = api.client.post("/assistant/tools/memory/cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc9/revise",
                          json={"title": "x"}, headers=headers)
    assert res.status_code == 404 and res.json()["detail"]["code"] == "memory_not_found"
    assert api.db.tables["assistant_memory_revisions"] == []


# ── memory_forget and memory_list ────────────────────────────────────────────


def test_forget_archives_but_never_in_a_tainted_turn_nor_a_pinned_note(api):
    api.db.tables["assistant_memories"] = [memory_row(), memory_row(id=MEMORY2, pinned=True)]
    res = api.client.post(f"/assistant/tools/memory/{MEMORY}/forget", headers=_turn(api, "memory_forget",
                                                                                     tainted=True))
    assert res.status_code == 409 and res.json()["detail"]["code"] == "tainted_turn"
    assert api.db.tables["assistant_memories"][0]["status"] == "active"
    headers = _turn(api, "memory_forget", key="k2")
    res = api.client.post(f"/assistant/tools/memory/{MEMORY2}/forget", headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_pinned_user_only"
    res = api.client.post(f"/assistant/tools/memory/{MEMORY}/forget", headers=headers)
    assert res.status_code == 200 and res.json() == {"memory_id": MEMORY, "status": "archived"}
    assert api.db.tables["assistant_memories"][0]["status"] == "archived"


def test_the_tool_list_holds_live_notes_with_cut_bodies(api):
    api.db.tables["assistant_memories"] = [
        memory_row(body="x" * 500), memory_row(id=MEMORY2, status="archived", archived_at=NOW.isoformat()),
        memory_row(id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc3", status="pending_review", source="inferred",
                   origin_tainted=True),
    ]
    api.state.via = RUN
    items = api.client.get("/assistant/tools/memory").json()["items"]
    assert {i["id"] for i in items} == {MEMORY, "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc3"}
    assert len(next(i for i in items if i["id"] == MEMORY)["body"]) == 300
    assert next(i for i in items if i["origin_tainted"])["status"] == "pending_review"
    only = api.client.get("/assistant/tools/memory?status=active").json()["items"]
    assert [i["id"] for i in only] == [MEMORY]
    assert api.client.get("/assistant/tools/memory?status=archived").status_code == 422


@pytest.mark.parametrize("method,path", [
    ("get", "/assistant/tools/memory"),
    ("post", "/assistant/tools/memory/save"),
    ("post", f"/assistant/tools/memory/{MEMORY}/revise"),
    ("post", f"/assistant/tools/memory/{MEMORY}/forget"),
])
def test_tool_routes_404_a_human(api, method, path):
    api.db.tables["assistant_memories"] = [memory_row()]
    _human(api)
    res = api.client.get(path) if method == "get" else api.client.post(
        path, json={"kind": "preference", "title": "x"} if path.endswith("save") else {"title": "x"},
        headers={HEADER: KEY})
    assert res.status_code == 404


# ── the Memory tab ───────────────────────────────────────────────────────────


def test_create_by_hand_is_active_user_settings_and_filtered(api):
    res = api.client.post("/assistant/memories", json={"kind": "about_me", "title": "  I run   estimating ",
                                                       "body": "Since 2019."})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "active" and body["source"] == "user_settings" and body["title"] == "I run estimating"
    assert "idempotency_key" not in body
    res = api.client.post("/assistant/memories", json={"kind": "about_me", "title": "Pay", "body": "$45/hr"})
    assert res.status_code == 422 and res.json()["detail"] == {
        "code": "memory_sensitive", "kind": "wage",
        "message": "That looks like a password, ID number, bank or pay detail, so it was not saved."}
    res = api.client.post("/assistant/memories", json={"kind": "about_me", "title": "x", "pinned": True})
    assert res.status_code == 422
    assert len(api.db.tables["assistant_memories"]) == 1


def test_list_summary_and_one_note(api):
    api.db.tables["assistant_memories"] = [
        memory_row(), memory_row(id=MEMORY2, kind="project_note", project_id=PROJECT, title="Square D only"),
    ]
    api.db.tables["assistant_memory_revisions"] = [{
        "id": "rev-1", "memory_id": MEMORY, "user_id": USER, "title": "Sort by due", "body": "",
        "origin_tainted": False, "decision": None, "decided_at": None, "created_at": NOW.isoformat(),
        "base_updated_at": api.db.tables["assistant_memories"][0]["updated_at"],
    }]
    items = {m["id"]: m for m in api.client.get("/assistant/memories").json()["items"]}
    assert items[MEMORY]["pending_revision"]["id"] == "rev-1"
    assert items[MEMORY2]["project"] == {"id": PROJECT, "number": "26.10.7267", "name": "Alpha Clinic"}
    only = api.client.get("/assistant/memories", params={"kind": "project_note"}).json()["items"]
    assert [m["id"] for m in only] == [MEMORY2]
    assert api.client.get("/assistant/memories", params={"status": "nope"}).status_code == 422
    summary = api.client.get("/assistant/memories/summary")
    assert summary.status_code == 200 and summary.json()["active"] == 2 and summary.json()["pending_revisions"] == 1
    one = api.client.get(f"/assistant/memories/{MEMORY}").json()
    assert one["id"] == MEMORY and one["pending_revision"]["title"] == "Sort by due"
    assert api.client.get("/assistant/memories/nope").status_code == 404


def test_archived_list_pages_past_one_page_with_next_before(api):
    # Archived has no cap: a full page carries next_before, and `before`
    # reads the older notes (FE-P3-4).
    ids = [f"0b9f8c1e-3a5d-4c2b-9d7e-{i:012d}" for i in range(5)]
    api.db.tables["assistant_memories"] = [
        memory_row(id=mid, status="archived", title=f"Old {i}", updated_at=(NOW - timedelta(days=i + 1)).isoformat())
        for i, mid in enumerate(ids)
    ] + [memory_row(id=MEMORY2, title="Live")]
    first = api.client.get("/assistant/memories", params={"status": "archived", "limit": 2}).json()
    assert [m["id"] for m in first["items"]] == ids[:2]
    assert first["next_before"] == (NOW - timedelta(days=2)).isoformat()
    second = api.client.get("/assistant/memories", params={"status": "archived", "limit": 2,
                                                           "before": first["next_before"]}).json()
    assert [m["id"] for m in second["items"]] == ids[2:4] and second["next_before"]
    last = api.client.get("/assistant/memories", params={"status": "archived", "limit": 2,
                                                         "before": second["next_before"]}).json()
    assert [m["id"] for m in last["items"]] == ids[4:] and last["next_before"] is None
    # The default page is the whole list (one page, no cursor) and a bad
    # cursor or limit is refused.
    whole = api.client.get("/assistant/memories", params={"status": "archived"}).json()
    assert len(whole["items"]) == 5 and whole["next_before"] is None
    assert api.client.get("/assistant/memories", params={"before": "nope"}).status_code == 422
    assert api.client.get("/assistant/memories", params={"limit": 301}).status_code == 422


def test_patch_edits_only_what_was_sent_and_runs_the_filter(api):
    api.db.tables["assistant_memories"] = [memory_row(body="Oldest first.")]
    stamp = api.db.tables["assistant_memories"][0]["updated_at"]
    res = api.client.patch(f"/assistant/memories/{MEMORY}", json={"title": "Sort by due", "expected_updated_at": stamp})
    assert res.status_code == 200, res.text
    assert res.json()["title"] == "Sort by due" and res.json()["body"] == "Oldest first."
    assert res.json()["source"] == "user_settings"
    res = api.client.patch(f"/assistant/memories/{MEMORY}",
                           json={"body": "My SSN is 123-45-6789", "expected_updated_at": res.json()["updated_at"]})
    assert res.status_code == 422 and res.json()["detail"]["kind"] == "ssn" and "6789" not in res.text
    res = api.client.patch(f"/assistant/memories/{MEMORY}", json={"title": "Again", "expected_updated_at": stamp})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_stale"
    assert res.json()["detail"]["memory"]["title"] == "Sort by due"
    assert api.client.patch(f"/assistant/memories/{MEMORY}", json={"title": "x"}).status_code == 422


def test_approve_and_reject_need_the_updated_at_the_person_saw(api):
    api.db.tables["assistant_memories"] = [memory_row(status="pending_review", source="inferred")]
    url = f"/assistant/memories/{MEMORY}"
    assert api.client.post(f"{url}/approve", json={}).status_code == 422
    assert api.client.post(f"{url}/reject", json={}).status_code == 422
    seen = api.db.tables["assistant_memories"][0]["updated_at"]
    # A later turn revises the pending note: the Save on the old text is stale.
    headers = _turn(api, "memory_update")
    api.client.post(f"/assistant/tools/memory/{MEMORY}/revise", json={"body": "Planted text"}, headers=headers)
    _human(api)
    for action in ("approve", "reject"):
        res = api.client.post(f"{url}/{action}", json={"expected_updated_at": seen})
        assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_stale"
        assert res.json()["detail"]["memory"]["body"] == "Planted text"
    fresh = api.db.tables["assistant_memories"][0]["updated_at"]
    res = api.client.post(f"{url}/approve?via=card", json={"expected_updated_at": fresh})
    assert res.status_code == 200 and res.json()["status"] == "active" and res.json()["source"] == "user_confirmed"
    assert _events(api)[-1] == "confirmed"
    res = api.client.post(f"{url}/reject", json={"expected_updated_at": res.json()["updated_at"]})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_wrong_status"


def test_pin_unpin_archive_restore(api):
    api.db.tables["assistant_memories"] = [memory_row(origin_tainted=True)]
    url = f"/assistant/memories/{MEMORY}"
    res = api.client.post(f"{url}/pin")
    assert res.status_code == 200 and res.json()["pinned"] is True and res.json()["warning"] == "untrusted_origin"
    res = api.client.post(f"{url}/unpin")
    assert res.json()["pinned"] is False and res.json()["warning"] is None
    res = api.client.post(f"{url}/archive")
    assert res.json()["status"] == "archived" and res.json()["archived_at"]
    res = api.client.post(f"{url}/pin")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "memory_wrong_status"
    res = api.client.post(f"{url}/restore")
    assert res.json()["status"] == "active"
    assert _events(api) == ["pinned", "unpinned", "archived", "restored"]


def test_revision_decisions(api):
    api.db.tables["assistant_memories"] = [memory_row()]
    rev = api.client.post(f"/assistant/tools/memory/{MEMORY}/revise", json={"body": "Newest first."},
                          headers=_turn(api, "memory_update")).json()["revision_id"]
    _human(api)
    url = f"/assistant/memories/{MEMORY}/revisions"
    assert api.client.post(f"{url}/nope/approve").status_code == 404
    res = api.client.post(f"{url}/{rev}/approve")
    assert res.status_code == 200 and res.json()["body"] == "Newest first." and res.json()["pending_revision"] is None
    res = api.client.post(f"{url}/{rev}/reject")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "revision_not_found"


def test_delete_scrubs_used_memory_ids_and_leaves_a_tombstone(api):
    api.db.tables["assistant_memories"] = [memory_row(), memory_row(id=MEMORY2)]
    api.db.tables["assistant_messages"] = [pending_answer(status="done", used_memory_ids=[MEMORY, MEMORY2])]
    res = api.client.delete(f"/assistant/memories/{MEMORY}")
    assert res.status_code == 204, res.text
    assert [m["id"] for m in api.db.tables["assistant_memories"]] == [MEMORY2]
    assert api.db.tables["assistant_messages"][0]["used_memory_ids"] == [MEMORY2]
    audit = api.db.tables["audit_log"][-1]
    assert audit["action"] == "assistant.memory_deleted" and audit["entity"] == "assistant_memory"
    assert audit["payload"] == {"kind": "preference", "origin_tainted": False}
    assert "Sort bids" not in str(audit)
    assert api.client.delete(f"/assistant/memories/{MEMORY}").status_code == 404


def test_delete_under_legal_hold_is_409_and_changes_nothing(api):
    api.db.tables["assistant_profiles"] = [{"user_id": USER, "enabled": True, "legal_hold": True}]
    api.db.tables["assistant_memories"] = [memory_row()]
    res = api.client.delete(f"/assistant/memories/{MEMORY}")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "legal_hold"
    assert len(api.db.tables["assistant_memories"]) == 1 and api.db.tables["audit_log"] == []


def test_the_memory_tab_refuses_an_assistant_principal(api):
    api.db.tables["assistant_memories"] = [memory_row()]
    api.state.via = RUN
    for method, path, body in (
        ("get", "/assistant/memories", None), ("get", "/assistant/memories/summary", None),
        ("get", f"/assistant/memories/{MEMORY}", None),
        ("post", "/assistant/memories", {"kind": "preference", "title": "x"}),
        ("patch", f"/assistant/memories/{MEMORY}", {"title": "x", "expected_updated_at": NOW.isoformat()}),
        ("post", f"/assistant/memories/{MEMORY}/pin", {}), ("post", f"/assistant/memories/{MEMORY}/archive", {}),
        ("post", f"/assistant/memories/{MEMORY}/approve", {"expected_updated_at": NOW.isoformat()}),
        ("delete", f"/assistant/memories/{MEMORY}", None),
    ):
        res = getattr(api.client, method)(path, **({"json": body} if body is not None else {}))
        assert res.status_code == 403, (method, path)
    assert api.db.tables["assistant_memories"][0]["status"] == "active"


# ── phase 4: memory_search (docs/ASSISTANT_PHASE4_PLAN.md 3.15) ────────────


def _search(api, q="bids sorted", **params):
    return api.client.get("/assistant/tools/memory/search", params={"q": q, **params})


def test_memory_search_reads_own_live_notes_only(api):
    from tests.assistant_fakes import OTHER_USER

    api.db.tables["assistant_memories"] = [
        memory_row(),
        memory_row(id=MEMORY2, title="Bids sorted by GC name", status="pending_review", origin_tainted=True,
                   body="x" * 400, updated_at=NOW.isoformat()),
        memory_row(id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc1", title="Bids sorted archived", status="archived"),
        memory_row(id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc2", user_id=OTHER_USER, title="Bids sorted by someone else"),
    ]
    _human(api)
    res = _search(api)
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    res = api.client.get("/assistant/tools/memory/search")         # no query: still the 404
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    api.state.via = RUN
    res = _search(api)
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert sorted(i["id"] for i in items) == sorted([MEMORY, MEMORY2])
    tainted = next(i for i in items if i["id"] == MEMORY2)
    assert tainted["origin_tainted"] is True and len(tainted["body"]) == 300 and tainted["status"] == "pending_review"
    assert all(isinstance(i["rank"], float) for i in items)
    assert "[M" not in res.text
    [(name, params)] = [c for c in api.db.rpc_calls if c[0] == "assistant_memory_search"]
    assert params["p_user_id"] == USER and params["p_statuses"] == ["active", "pending_review"]


def test_memory_search_filters_and_bounds(api):
    api.db.tables["assistant_memories"] = [
        memory_row(), memory_row(id=MEMORY2, title="Bids sorted for review", status="pending_review")]
    api.state.via = RUN
    assert [i["id"] for i in _search(api, status="active").json()["items"]] == [MEMORY]
    assert [i["id"] for i in _search(api, status="pending_review").json()["items"]] == [MEMORY2]
    assert len(_search(api, limit=1).json()["items"]) == 1
    assert _search(api, kind="preference").status_code == 200
    res = _search(api, kind="secret")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_memory"
    for bad in ({"q": "x" * 201}, {"limit": 26}, {"limit": 0}, {"status": "archived"}):
        assert api.client.get("/assistant/tools/memory/search",
                              params={"q": "bids sorted", **bad}).status_code == 422, bad
    for short in ("x", ""):
        res = _search(api, q=short)
        assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_memory"
    res = _search(api, q="  a ")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_memory"


def test_memory_search_route_literal():
    assert memory_routes.TOOL_SEARCH_ROUTE == "/assistant/tools/memory/search"
    assert memory_routes.TOOL_SEARCH_ROUTE in [r.path for r in memory_routes.router.routes]
