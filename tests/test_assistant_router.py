"""The assistant API (app/routers/assistant.py) against the phase 0 contract.

Invariants pinned here (docs/ASSISTANT.md, UI, Permissions items 2, 3 and
15):

  gates     every route needs an internal role; a caller whose assistant is
            off (env, company, profile) gets a bare 404; health reports why;
  settings  GET creates the profile lazily; PATCH validates 1 to 40 chars,
            trimmed, no em dash, null resets;
  turns     POST messages creates the user row and the pending assistant
            row, enqueues one assistant_turn job targeting the conversation
            with max_attempts 1, and answers 202; a second send while a turn
            is pending or running is 409 turn_in_progress; a queue failure
            fails the row instead of leaving it pending;
  shapes    Conversation, Message and Block match the contract; failed
            messages carry error; transcript blocks never leave the server;
  skills    the index lists only skills the caller's flags and role admit;
  files     (phase 2, docs/ASSISTANT_PHASE2_PLAN.md 3.2) a send carries at
            most 10 distinct staged file ids, each live, unclaimed and
            unlinked, linked after the user row and before the assistant
            row; a short link withdraws the question and starts no turn;
            user messages show their files; health counts live holds;
  phase 3   (docs/ASSISTANT_PHASE3_PLAN.md 3.9.3) incognito is stored on
            both rows of a send; answers carry the notes they used,
            resolved at read time (a deleted note disappears); the Chat
            list holds panel, page and task conversations only; reading a
            routine's conversation names its schedule and marks it seen.

The router is mounted on a private FastAPI app here, as the integration
stage will mount it on app.main.app; the hook is a dependency override.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, llm_messages
from app.core import supabase_client
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant as router_mod
from app.services import llm_queue

from tests.assistant_fakes import (
    CONV, MSG_ASSISTANT, MSG_USER, NOW, OTHER_USER, PROJECT, STAGED, STAGED2, TASK, USER, FakeDB, conversation,
    delegation, message, pending_answer, profile, reservation, settings_row, staged_file,
)

EM_DASH = chr(0x2014)


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[], assistant_conversations=[], assistant_messages=[], llm_call_log=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    monkeypatch.setattr(router_mod, "_model_available", lambda: True)
    monkeypatch.setattr(router_mod, "_queue_available", lambda: True)
    who = {"role": Role.EXECUTIVE, "aal": "aal2", "mfa_enrolled": True}

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=who["role"], is_active=True, aal=who["aal"],
                           mfa_enrolled=who["mfa_enrolled"])

    jobs: list[dict] = []

    def enqueue(job_type, **kw):
        jobs.append({"job_type": job_type, **kw})
        return {"id": f"job-{len(jobs)}"}

    monkeypatch.setattr(llm_queue, "enqueue", enqueue)
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app, raise_server_exceptions=True)
    return SimpleNamespace(client=client, db=db, who=who, jobs=jobs)


# ── gates and health ─────────────────────────────────────────────────────────


def test_estimator_is_refused_everywhere(api):
    api.who["role"] = Role.ESTIMATOR
    for method, path in (("get", "/assistant/health"), ("get", "/assistant/settings"),
                         ("get", "/assistant/conversations"), ("post", "/assistant/conversations")):
        res = api.client.post(path, json={}) if method == "post" else api.client.get(path)
        assert res.status_code == 403, (path, res.text)


def test_health_reports_disabled_over_cap_and_available(api):
    assert api.client.get("/assistant/health").json() == {"available": True, "reason": None}
    api.db.tables["assistant_settings"][0]["enabled"] = False
    assert api.client.get("/assistant/health").json() == {"available": False, "reason": "disabled"}
    api.db.tables["assistant_settings"][0]["enabled"] = True
    api.db.tables["assistant_profiles"] = [{"user_id": USER, "enabled": False}]
    assert api.client.get("/assistant/health").json()["reason"] == "disabled"
    api.db.tables["assistant_profiles"] = [{"user_id": USER, "enabled": True, "daily_cost_cap_usd": 1}]
    api.db.tables["llm_call_log"] = [{"id": "x", "created_at": NOW.isoformat(), "cost_usd": 1.5,
                                      "user_id": USER, "feature": "assistant"}]
    assert api.client.get("/assistant/health").json() == {"available": False, "reason": "over_cap"}


def test_health_reports_model_unavailable(api, monkeypatch):
    monkeypatch.setattr(router_mod, "_model_available", lambda: False)
    assert api.client.get("/assistant/health").json() == {"available": False, "reason": "model_unavailable"}


def test_model_check_is_false_until_the_feature_is_registered():
    # services/llm has no `assistant` feature until the integration stage;
    # the check must answer False then, never raise.
    import app.services.llm as llm

    if "assistant" not in llm._FEATURES:
        assert router_mod._model_available() is False


def test_switched_off_assistant_gives_a_bare_404(api):
    api.db.tables["assistant_settings"][0]["enabled"] = False
    for path in ("/assistant/settings", "/assistant/conversations", "/assistant/skills"):
        res = api.client.get(path)
        assert res.status_code == 404 and res.json() == {"detail": "Not Found"}, path


# ── settings ─────────────────────────────────────────────────────────────────


def test_settings_get_creates_the_profile_and_patch_validates(api):
    res = api.client.get("/assistant/settings")
    assert res.status_code == 200, res.text
    assert res.json() == {"display_name": None, "default_display_name": "Tom's assistant", "enabled": True,
                          "timezone": "America/Los_Angeles", "daily_cost_cap_usd": 5.0}
    assert len(api.db.tables["assistant_profiles"]) == 1
    res = api.client.patch("/assistant/settings", json={"display_name": "  Bo   Peep "})
    assert res.status_code == 200 and res.json()["display_name"] == "Bo Peep"
    assert api.client.patch("/assistant/settings", json={"display_name": "x" * 41}).status_code == 422
    assert api.client.patch("/assistant/settings", json={"display_name": f"a {EM_DASH} b"}).status_code == 422
    res = api.client.patch("/assistant/settings", json={"display_name": None})
    assert res.status_code == 200 and res.json()["display_name"] is None
    res = api.client.patch("/assistant/settings", json={"display_name": "   "})
    assert res.status_code == 200 and res.json()["display_name"] is None


# ── conversations and messages ───────────────────────────────────────────────


def test_conversation_create_list_get(api):
    res = api.client.post("/assistant/conversations", json={"surface": "page", "project_id": None, "path": "/assistant"})
    assert res.status_code == 201, res.text
    conv = res.json()
    assert set(conv) == {"id", "title", "surface", "project_id", "created_at", "last_message_at", "schedule_id", "mode", "mailbox_tainted_at"}
    assert conv["schedule_id"] is None
    assert conv["surface"] == "page" and conv["title"] is None
    assert api.client.post("/assistant/conversations", json={"surface": "email"}).status_code == 422
    items = api.client.get("/assistant/conversations?limit=5").json()["items"]
    assert [c["id"] for c in items] == [conv["id"]]
    detail = api.client.get(f"/assistant/conversations/{conv['id']}").json()
    assert detail["conversation"]["id"] == conv["id"] and detail["messages"] == []
    assert api.client.get("/assistant/conversations/nope").status_code == 404
    # Another user's conversation is a 404, never a read.
    api.db.tables["assistant_conversations"].append(conversation(id="other", user_id="someone-else"))
    assert api.client.get("/assistant/conversations/other").status_code == 404
    assert [c["id"] for c in api.client.get("/assistant/conversations").json()["items"]] == [conv["id"]]


def test_send_message_creates_rows_and_enqueues_one_turn(api):
    api.db.tables["assistant_conversations"] = [conversation()]
    res = api.client.post(f"/assistant/conversations/{CONV}/messages",
                          json={"content": "What is due today?", "context": {"path": "/dashboard", "project_id": None}})
    assert res.status_code == 202, res.text
    body = res.json()
    assert set(body) == {"user_message", "assistant_message", "job_id", "browser_code_card"}
    assert body["browser_code_card"] is False
    assert body["user_message"]["role"] == "user" and body["user_message"]["status"] == "done"
    assert body["user_message"]["content"] == "What is due today?"
    assert body["assistant_message"]["role"] == "assistant" and body["assistant_message"]["status"] == "pending"
    assert body["assistant_message"]["tool_calls"] == [] and body["assistant_message"]["blocks"] == []
    assert body["job_id"] == "job-1"
    assert set(body["assistant_message"]) == {"id", "conversation_id", "role", "content", "status", "tool_calls",
                                              "blocks", "error", "created_at", "files", "incognito",
                                              "used_memories"}
    assert body["assistant_message"]["incognito"] is False and body["assistant_message"]["used_memories"] == []
    assert body["user_message"]["files"] == [] and body["assistant_message"]["files"] == []
    job = api.jobs[0]
    assert job["job_type"] == "assistant_turn" and job["target_id"] == CONV and job["raise_on_active"] is True
    assert job["payload"] == {"message_id": body["assistant_message"]["id"], "user_id": USER, "conversation_id": CONV}
    assert job["created_by"] == USER
    # The delegation grant was created under the aal2 session, once.
    assert len(api.db.tables["assistant_delegations"]) == 1
    assert api.db.tables["assistant_delegations"][0]["created_session_aal"] == "aal2"
    stored = [m for m in api.db.tables["assistant_messages"] if m["role"] == "assistant"][0]
    assert stored["llm_job_id"] == "job-1" and stored["context"] == {"path": "/dashboard", "project_id": None}
    assert api.db.tables["assistant_conversations"][0]["last_message_at"] is not None


def test_second_send_while_a_turn_is_active_is_409(api):
    api.db.tables["assistant_conversations"] = [conversation()]
    api.db.tables["assistant_messages"] = [message(), pending_answer()]
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "again"})
    assert res.status_code == 409 and res.json() == {"detail": {"code": "turn_in_progress"}}
    assert api.jobs == []
    assert len(api.db.tables["assistant_messages"]) == 2


def test_unique_index_race_is_409_and_queue_collision_is_409(api, monkeypatch):
    api.db.tables["assistant_conversations"] = [conversation()]
    # The pre-check passes but the insert hits the partial unique index.
    from app.assistant import repo

    real_active = repo.active_turn
    monkeypatch.setattr(repo, "active_turn", lambda user_id, cid: None)
    api.db.tables["assistant_messages"] = [pending_answer(id="elsewhere")]
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "x"})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "turn_in_progress"
    assert res.headers["X-Error-Code"] == "turn_in_progress"
    # The question is withdrawn: no orphan user row to replay as history.
    assert [m["id"] for m in api.db.tables["assistant_messages"]] == ["elsewhere"]
    monkeypatch.setattr(repo, "active_turn", real_active)
    # The queue already has an active job for the conversation.
    api.db.tables["assistant_messages"] = []

    def collide(job_type, **kw):
        raise llm_queue.JobAlreadyActive({"id": "old"})

    monkeypatch.setattr(llm_queue, "enqueue", collide)
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "x"})
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "turn_in_progress"
    # No job was created for the pair, so both rows are withdrawn.
    assert api.db.tables["assistant_messages"] == []


def test_archived_conversation_409_carries_its_own_code(api):
    api.db.tables["assistant_conversations"] = [conversation(archived_at=NOW.isoformat())]
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "x"})
    assert res.status_code == 409 and res.json()["detail"] == "This conversation is archived"
    assert res.headers["X-Error-Code"] == "conversation_archived"
    assert api.db.tables["assistant_messages"] == [] and api.jobs == []


def test_send_refuses_while_the_queue_cannot_run_a_turn(api, monkeypatch):
    api.db.tables["assistant_conversations"] = [conversation()]
    monkeypatch.setattr(router_mod, "_queue_available", lambda: False)
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "x"})
    assert res.status_code == 503 and res.json() == {"detail": {"code": "assistant_unavailable"}}
    assert res.headers["X-Error-Code"] == "assistant_unavailable"
    # Nothing was inserted and nothing enqueued: no row can sit pending forever.
    assert api.db.tables["assistant_messages"] == [] and api.jobs == []
    assert api.client.get("/assistant/health").json() == {"available": False, "reason": "queue_off"}


def test_queue_available_reads_the_switch_and_the_claim_types(monkeypatch):
    def settings(**over):
        base = {"llm_queue_enabled": True, "llm_queue_claim_job_types": ()}
        base.update(over)
        return SimpleNamespace(**base)

    monkeypatch.setattr(router_mod, "get_settings", lambda: settings())
    assert router_mod._queue_available() is True
    monkeypatch.setattr(router_mod, "get_settings", lambda: settings(llm_queue_enabled=False))
    assert router_mod._queue_available() is False
    monkeypatch.setattr(router_mod, "get_settings", lambda: settings(llm_queue_claim_job_types=("boq",)))
    assert router_mod._queue_available() is False
    monkeypatch.setattr(router_mod, "get_settings",
                        lambda: settings(llm_queue_claim_job_types=("boq", "assistant_turn")))
    assert router_mod._queue_available() is True


def test_project_id_must_be_a_uuid(api):
    res = api.client.post("/assistant/conversations", json={"surface": "panel", "project_id": "nope"})
    assert res.status_code == 422
    res = api.client.post("/assistant/conversations", json={"surface": "panel", "project_id": PROJECT})
    assert res.status_code == 201 and res.json()["project_id"] == PROJECT
    res = api.client.post("/assistant/conversations", json={"surface": "panel", "project_id": "  "})
    assert res.status_code == 201 and res.json()["project_id"] is None
    api.db.tables["assistant_conversations"] = [conversation()]
    res = api.client.post(f"/assistant/conversations/{CONV}/messages",
                          json={"content": "x", "context": {"path": "/projects/nope", "project_id": "nope"}})
    assert res.status_code == 422
    assert api.db.tables["assistant_messages"] == [] and api.jobs == []


def test_expired_delegation_is_revoked_and_replaced(api):
    api.db.tables["assistant_conversations"] = [conversation()]
    stale = delegation(id="old", expires_at=(NOW - timedelta(hours=1)).isoformat())
    api.db.tables["assistant_delegations"] = [stale]
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "x"})
    assert res.status_code == 202
    rows = api.db.tables["assistant_delegations"]
    assert len(rows) == 2
    old = next(r for r in rows if r["id"] == "old")
    assert old["revoked_at"] and old["revoked_reason"] == "expired"
    fresh = next(r for r in rows if r["id"] != "old")
    assert fresh["user_id"] == USER and fresh.get("revoked_at") is None


def test_queue_failure_fails_the_row_instead_of_hanging(api, monkeypatch):
    api.db.tables["assistant_conversations"] = [conversation()]

    def boom(job_type, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(llm_queue, "enqueue", boom)
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "x"})
    assert res.status_code == 503 and res.json()["detail"] == {"code": "assistant_unavailable"}
    row = [m for m in api.db.tables["assistant_messages"] if m["role"] == "assistant"][0]
    assert row["status"] == "failed"


def test_queue_failure_with_files_unlinks_them_for_the_retry(api, monkeypatch):
    """A 503 after the link must not strand the files on an orphan user row:
    the retry with the same chips would answer file_not_available."""
    _two_staged(api)

    def boom(job_type, **kw):
        raise RuntimeError("db down")

    real = llm_queue.enqueue
    monkeypatch.setattr(llm_queue, "enqueue", boom)
    url = f"/assistant/conversations/{CONV}/messages"
    res = api.client.post(url, json={"content": "Split these.", "file_ids": [STAGED, STAGED2]})
    assert res.status_code == 503 and res.json()["detail"] == {"code": "assistant_unavailable"}
    assert api.db.tables["assistant_messages"] == []
    assert all(r["message_id"] is None for r in api.db.tables["assistant_files"])
    monkeypatch.setattr(llm_queue, "enqueue", real)
    again = api.client.post(url, json={"content": "Split these.", "file_ids": [STAGED, STAGED2]})
    assert again.status_code == 202, again.text


def test_send_needs_aal2_and_content_bounds(api):
    api.db.tables["assistant_conversations"] = [conversation()]
    assert api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": ""}).status_code == 422
    assert api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "   "}).status_code == 422
    assert api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "x" * 8001}).status_code == 422
    api.who["aal"] = "aal1"
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "x"})
    assert res.status_code == 403 and res.json()["detail"] == "mfa_step_up_required"
    assert api.client.post("/assistant/conversations/nope/messages", json={"content": "x"}).status_code == 404


def test_get_message_shapes_done_and_failed_rows(api):
    api.db.tables["assistant_conversations"] = [conversation()]
    done = pending_answer(status="done", content="26.10.7267 is in verify.", tool_calls=[
        {"seq": 1, "tool_name": "whats_due", "progress_label": "Checking what is due", "ok": True, "duration_ms": 12}],
        blocks=[{"type": "text", "text": "26.10.7267 is in verify."},
                {"type": "citation", "label": "26.10.7267", "project_id": "p1", "path": "/projects/p1"},
                {"type": "transcript", "items": [{"type": "reasoning", "encrypted_content": "secret"}]}])
    failed = pending_answer(id="f1", status="failed", content="",
                            blocks=[{"type": "error", "text": "The assistant hit a problem."}], error_kind="timeout")
    api.db.tables["assistant_messages"] = [message(), done, failed]
    res = api.client.get(f"/assistant/messages/{MSG_ASSISTANT}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "done" and body["error"] is None
    assert [b["type"] for b in body["blocks"]] == ["text", "citation"]
    assert "secret" not in res.text
    assert body["tool_calls"][0] == {"seq": 1, "tool_name": "whats_due", "progress_label": "Checking what is due",
                                     "ok": True, "duration_ms": 12}
    res = api.client.get("/assistant/messages/f1").json()
    assert res["status"] == "failed" and res["error"] == "The assistant hit a problem." and res["content"] == ""
    assert api.client.get("/assistant/messages/nope").status_code == 404
    detail = api.client.get(f"/assistant/conversations/{CONV}").json()
    assert [m["id"] for m in detail["messages"]] == [MSG_USER, MSG_ASSISTANT, "f1"]


# ── skills ───────────────────────────────────────────────────────────────────


def test_skills_index_and_body(api, monkeypatch):
    res = api.client.get("/assistant/skills")
    assert res.status_code == 200, res.text
    names = {s["name"] for s in res.json()}
    assert {"bdr-glossary", "answering-status-questions"} <= names
    body = api.client.get("/assistant/skills/bdr-glossary").json()
    assert body["name"] == "bdr-glossary" and "Internal bid date" in body["body"] and EM_DASH not in body["body"]
    assert api.client.get("/assistant/skills/nope").status_code == 404
    # A skill that needs Bidding disappears when the flag is off.
    monkeypatch.setattr(router_mod, "enabled_map", lambda role: {"bidding": False, "pm": True, "certified_payroll": True})
    names = {s["name"] for s in api.client.get("/assistant/skills").json()}
    assert "answering-status-questions" not in names and "bdr-glossary" in names
    assert api.client.get("/assistant/skills/answering-status-questions").status_code == 404


def test_attribution_contextvar_round_trips():
    assert llm_messages.current_attribution() is None
    with llm_messages.attributed(USER, "run-1"):
        assert llm_messages.current_attribution() == llm_messages.Attribution(USER, "run-1")
    assert llm_messages.current_attribution() is None


def test_features_flag_fails_closed_and_follows_the_switches(api, monkeypatch):
    user = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    env = {"on": True}
    monkeypatch.setattr(router_mod, "get_settings", lambda: SimpleNamespace(assistant_enabled=env["on"]))
    assert router_mod.features_flag(user) is True
    assert router_mod.features_flag(CurrentUser(id=USER, email="e", role=Role.ESTIMATOR, is_active=True)) is False
    api.db.tables["assistant_profiles"] = [{"user_id": USER, "enabled": False}]
    assert router_mod.features_flag(user) is False
    api.db.tables["assistant_profiles"] = []
    env["on"] = False
    assert router_mod.features_flag(user) is False
    env["on"] = True

    def boom(user_id):
        raise RuntimeError("db down")

    from app.assistant import repo

    monkeypatch.setattr(repo, "settings_row", boom)
    assert router_mod.features_flag(user) is False


# ── phase 2: files on messages ───────────────────────────────────────────────


def _two_staged(api):
    api.db.tables["assistant_conversations"] = [conversation()]
    api.db.tables["assistant_files"] = [
        staged_file(), staged_file(id=STAGED2, filename="E-201.pdf", sha256="b" * 64, storage_path="p2",
                                   page_count=2, size_bytes=99),
    ]


def test_send_with_file_ids_links_them_between_the_two_rows(api, monkeypatch):
    from app.assistant import repo

    _two_staged(api)
    order: list[str] = []
    real_insert, real_link = repo.insert_message, repo.link_files

    def insert(user_id, row):
        order.append(f"insert {row['role']}")
        return real_insert(user_id, row)

    def link(*a, **k):
        order.append("link")
        return real_link(*a, **k)

    monkeypatch.setattr(repo, "insert_message", insert)
    monkeypatch.setattr(repo, "link_files", link)
    res = api.client.post(f"/assistant/conversations/{CONV}/messages",
                          json={"content": "Split these into this project.", "file_ids": [STAGED, STAGED2]})
    assert res.status_code == 202, res.text
    assert order == ["insert user", "link", "insert assistant"]
    body = res.json()
    assert body["user_message"]["files"] == [
        {"id": STAGED, "filename": "A-101.pdf", "size_bytes": 1234, "pages": 3, "state": "staged"},
        {"id": STAGED2, "filename": "E-201.pdf", "size_bytes": 99, "pages": 2, "state": "staged"},
    ]
    assert body["assistant_message"]["files"] == []
    user_id = body["user_message"]["id"]
    for row in api.db.tables["assistant_files"]:
        assert row["message_id"] == user_id and row["conversation_id"] == CONV
    assert len(api.jobs) == 1
    # The files come back on the conversation and on the message read.
    detail = api.client.get(f"/assistant/conversations/{CONV}").json()
    user_msg = next(m for m in detail["messages"] if m["id"] == user_id)
    assert [f["id"] for f in user_msg["files"]] == [STAGED, STAGED2]
    assert all(m["files"] == [] for m in detail["messages"] if m["role"] == "assistant")
    one = api.client.get(f"/assistant/messages/{user_id}").json()
    assert [f["state"] for f in one["files"]] == ["staged", "staged"]
    # A consumed or retired file reads as such.
    api.db.tables["assistant_files"][0]["consumed_task_id"] = TASK
    api.db.tables["assistant_files"][1]["purged_at"] = NOW.isoformat()
    one = api.client.get(f"/assistant/messages/{user_id}").json()
    assert [f["state"] for f in one["files"]] == ["consumed", "expired"]


def test_send_file_ids_are_bounded_distinct_uuids(api):
    _two_staged(api)
    url = f"/assistant/conversations/{CONV}/messages"
    ids = [f"aaaaaaaa-aaaa-4aaa-8aaa-{i:012d}" for i in range(11)]
    assert api.client.post(url, json={"content": "x", "file_ids": ids}).status_code == 422
    assert api.client.post(url, json={"content": "x", "file_ids": [STAGED, STAGED]}).status_code == 422
    assert api.client.post(url, json={"content": "x", "file_ids": ["nope"]}).status_code == 422
    assert api.client.post(url, json={"content": "  ", "file_ids": [STAGED]}).status_code == 422
    assert api.db.tables["assistant_messages"] == [] and api.jobs == []


@pytest.mark.parametrize("over", [
    {"consumed_at": NOW.isoformat(), "consumed_task_id": TASK},
    {"consumed_task_id": TASK},
    {"user_id": OTHER_USER},
    {"message_id": MSG_USER, "conversation_id": CONV},
    {"expires_at": (NOW - timedelta(minutes=1)).isoformat()},
    {"purged_at": NOW.isoformat()},
])
def test_send_refuses_a_file_that_is_not_available(api, over):
    _two_staged(api)
    api.db.tables["assistant_files"][1].update(over)
    res = api.client.post(f"/assistant/conversations/{CONV}/messages",
                          json={"content": "Split these.", "file_ids": [STAGED, STAGED2]})
    assert res.status_code == 409, res.text
    assert res.headers["X-Error-Code"] == "file_not_available"
    detail = res.json()["detail"]
    assert detail["code"] == "file_not_available" and detail["file_ids"] == [STAGED2]
    assert api.db.tables["assistant_messages"] == [] and api.jobs == []
    assert api.db.tables["assistant_files"][0]["message_id"] is None


def test_a_short_link_withdraws_the_question_and_starts_no_turn(api, monkeypatch):
    from app.assistant import repo

    _two_staged(api)
    # The pre-check passes, then a split card claims one file before the link.
    rows = [dict(r) for r in api.db.tables["assistant_files"]]
    calls = {"n": 0}
    real_for_link = repo.staged_files_for_link

    def for_link(user_id, ids):
        calls["n"] += 1
        if calls["n"] == 1:
            api.db.tables["assistant_files"][1]["consumed_task_id"] = TASK
            return rows
        return real_for_link(user_id, ids)

    monkeypatch.setattr(repo, "staged_files_for_link", for_link)
    res = api.client.post(f"/assistant/conversations/{CONV}/messages",
                          json={"content": "Split these.", "file_ids": [STAGED, STAGED2]})
    assert res.status_code == 409 and res.json()["detail"]["file_ids"] == [STAGED2]
    assert api.db.tables["assistant_messages"] == [] and api.jobs == []
    # The withdrawn message unlinked the file it had taken (on delete set null).
    assert api.db.tables["assistant_files"][0]["message_id"] is None


def test_attach_send_reattach_send_again(api):
    """The same staged bytes can ride a second message: a linked row is
    never reused, so the second upload makes a new row that links fine."""
    _two_staged(api)
    url = f"/assistant/conversations/{CONV}/messages"
    first = api.client.post(url, json={"content": "First.", "file_ids": [STAGED]})
    assert first.status_code == 202
    api.db.tables["assistant_messages"] = [m for m in api.db.tables["assistant_messages"] if m["role"] == "user"]
    again = staged_file(id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa9", storage_path="p9")
    api.db.tables["assistant_files"].append(again)
    second = api.client.post(url, json={"content": "Again.", "file_ids": [again["id"]]})
    assert second.status_code == 202, second.text
    assert second.json()["user_message"]["files"][0]["id"] == again["id"]
    # Re-sending the first (already linked) id is refused.
    api.db.tables["assistant_messages"] = [m for m in api.db.tables["assistant_messages"] if m["role"] == "user"]
    third = api.client.post(url, json={"content": "Third.", "file_ids": [STAGED]})
    assert third.status_code == 409


def test_health_counts_live_holds(api):
    api.db.tables["assistant_profiles"] = [{"user_id": USER, "enabled": True, "daily_cost_cap_usd": 1}]
    api.db.tables["assistant_spend_reservations"] = [reservation(amount_usd=1.0)]
    assert api.client.get("/assistant/health").json() == {"available": False, "reason": "over_cap"}
    api.db.tables["assistant_spend_reservations"] = [reservation(amount_usd=1.0, status="settled")]
    assert api.client.get("/assistant/health").json() == {"available": True, "reason": None}
    api.db.tables["assistant_spend_reservations"] = [
        reservation(amount_usd=1.0, expires_at=(NOW - timedelta(minutes=1)).isoformat())]
    assert api.client.get("/assistant/health").json() == {"available": True, "reason": None}


# ── phase 3: incognito, used memories, the list filter, seen ─────────────────


def test_send_stores_incognito_on_both_rows(api):
    api.db.tables["assistant_conversations"] = [conversation()]
    res = api.client.post(f"/assistant/conversations/{CONV}/messages",
                          json={"content": "Remember nothing of this.", "incognito": True})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["user_message"]["incognito"] is True and body["assistant_message"]["incognito"] is True
    rows = api.db.tables["assistant_messages"]
    assert len(rows) == 2 and all(r["incognito"] is True for r in rows)
    # Default false, and stored as such.
    api.db.tables["assistant_messages"] = []
    res = api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "Hello."})
    assert res.json()["user_message"]["incognito"] is False
    assert all(r["incognito"] is False for r in api.db.tables["assistant_messages"])
    assert api.client.post(f"/assistant/conversations/{CONV}/messages",
                           json={"content": "x", "incognito": "maybe"}).status_code == 422


def test_answers_carry_the_notes_they_used(api, monkeypatch):
    from tests.assistant_fakes import MEMORY, MEMORY2, memory_row

    api.db.tables["assistant_conversations"] = [conversation()]
    api.db.tables["assistant_memories"] = [memory_row(), memory_row(id=MEMORY2, title="Gear from Graybar",
                                                                    kind="reference", user_id=OTHER_USER)]
    gone = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc9"
    done = pending_answer(status="done", content="Sorted by due time.", used_memory_ids=[MEMORY, gone, MEMORY2])
    api.db.tables["assistant_messages"] = [message(used_memory_ids=[MEMORY]), done]
    one = api.client.get(f"/assistant/messages/{MSG_ASSISTANT}").json()
    # A deleted note and another user's note simply disappear.
    assert one["used_memories"] == [{"id": MEMORY, "title": "Sort bids by due time", "kind": "preference"}]
    detail = api.client.get(f"/assistant/conversations/{CONV}").json()
    by_id = {m["id"]: m for m in detail["messages"]}
    assert by_id[MSG_ASSISTANT]["used_memories"][0]["id"] == MEMORY
    assert by_id[MSG_USER]["used_memories"] == []          # only answers carry the chip
    # A failed read drops the chips, never the answer.
    from app.assistant import repo

    def boom(user_id, ids):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "memories_by_ids", boom)
    res = api.client.get(f"/assistant/messages/{MSG_ASSISTANT}")
    assert res.status_code == 200 and res.json()["used_memories"] == []


def test_the_chat_list_holds_panel_page_and_task_conversations_only(api):
    api.db.tables["assistant_conversations"] = [
        conversation(id="c-panel", surface="panel"), conversation(id="c-page", surface="page"),
        conversation(id="c-task", surface="task"), conversation(id="c-sched", surface="schedule"),
    ]
    ids = {c["id"] for c in api.client.get("/assistant/conversations").json()["items"]}
    assert ids == {"c-panel", "c-page", "c-task"}
    assert all(c["schedule_id"] is None for c in api.client.get("/assistant/conversations").json()["items"])


def test_reading_a_routine_conversation_names_its_schedule_and_marks_it_seen(api):
    from tests.assistant_fakes import SCHEDULE, schedule

    api.db.tables["assistant_conversations"] = [conversation(surface="schedule", title="Bids due today")]
    api.db.tables["assistant_schedules"] = [schedule(conversation_id=CONV, unread_deliveries=3)]
    detail = api.client.get(f"/assistant/conversations/{CONV}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["conversation"]["schedule_id"] == SCHEDULE
    row = api.db.tables["assistant_schedules"][0]
    assert row["unread_deliveries"] == 0 and row["last_seen_at"] is not None
    # A panel conversation names no schedule and marks nothing.
    api.db.tables["assistant_conversations"] = [conversation()]
    row["unread_deliveries"] = 2
    detail = api.client.get(f"/assistant/conversations/{CONV}").json()
    assert detail["conversation"]["schedule_id"] is None and row["unread_deliveries"] == 2


def test_an_assistant_reading_a_routine_conversation_marks_nothing(api):
    from tests.assistant_fakes import RUN, SCHEDULE, schedule

    api.db.tables["assistant_conversations"] = [conversation(surface="schedule")]
    api.db.tables["assistant_schedules"] = [schedule(conversation_id=CONV, unread_deliveries=3)]
    app = api.client.app
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True, via_assistant_run_id=RUN)
    detail = api.client.get(f"/assistant/conversations/{CONV}").json()
    assert detail["conversation"]["schedule_id"] == SCHEDULE
    assert api.db.tables["assistant_schedules"][0]["unread_deliveries"] == 3
