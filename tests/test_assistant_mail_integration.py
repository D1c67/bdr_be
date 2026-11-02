"""Real mail ToolSpecs, approval engine, shim and routes; fake provider only.

The in-memory approval/ledger store exercises request hashing, trusted preview
context and precondition replacement. SQL send concurrency is covered separately
by the migration checks; the provider here never reaches Microsoft.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.assistant import access, approvals, approvals_job, jobs, principal, registry, repo, shim
from app.assistant.executor import Executor
from app.core import deps, features, supabase_client
from app.core.roles import Role
from app.routers import assistant_mail
from app.services import assistant_mail_send as send
from app.services import assistant_ms, notifications
from app.services.assistant_graph import GraphSendUncertain
from tests.assistant_fakes import (
    CONV, MSG_ASSISTANT, RUN, SECRET, USER, FakeDB, conversation, delegation,
    message, pending_answer, profile, settings_row,
)


@pytest.fixture
def env(monkeypatch):
    settings = SimpleNamespace(assistant_enabled=True, assistant_outlook_enabled=True,
        assistant_token_secret=SECRET, ms_tenant_id="tenant", ms_client_id="client", ms_client_secret="secret",
        shared_mailbox_set={"bids@g3.test"})
    for module in [access, principal, assistant_ms]:
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    db = FakeDB(profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[delegation()], assistant_approvals=[], assistant_tool_calls=[],
        assistant_tool_results=[], assistant_messages=[message(content="Email tom@g3.test that the plans are ready."),
        pending_answer(status="waiting_approval")], assistant_conversations=[conversation()],
        assistant_files=[], email_log=[])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(deps, "get_supabase", lambda: db)
    monkeypatch.setattr(features, "enabled_map", lambda role=None: {"bidding": True, "pm": True, "certified_payroll": True})
    monkeypatch.setattr(jobs, "_renew_lease", lambda: True)
    monkeypatch.setattr(jobs, "_lease_owner", lambda: "worker:test")
    monkeypatch.setattr(notifications, "notify_user", lambda *a, **k: None)
    monkeypatch.setattr(send.graph_email, "_active_test_session", lambda sb: None)
    monkeypatch.setattr(send.assistant_ms.graph_email, "_acquire_token", lambda: "fake-token")
    app = FastAPI()
    app.include_router(assistant_mail.router)
    monkeypatch.setattr(shim, "served_app", lambda: app)
    state = SimpleNamespace(db=db, sends=[], claims={}, uncertain=False)
    class Graph:
        def __init__(self, token, **kwargs):
            assert token == "fake-token"
            assert kwargs["sender_mailbox"] == db.tables["profiles"][0]["email"]
        def send_mail(self, payload):
            state.sends.append(payload)
            if state.uncertain:
                raise GraphSendUncertain()
    monkeypatch.setattr(send, "GraphTransport", Graph)
    def claim(uid, aid, run, sender, payload_hash, **kwargs):
        assert uid == USER
        row = repo.get_approval(uid, aid)
        assert row["payload_sha256"] == payload_hash
        assert row["status"] == "executing"
        if aid in state.claims:
            return {"ok": True, "claimed": False, **state.claims[aid]}
        state.claims[aid] = {"send_id": row["payload"]["request"]["json"]["send_id"], "status": "claimed"}
        return {"ok": True, "claimed": True, **state.claims[aid]}
    def finish(uid, sid, status, **kwargs):
        for value in state.claims.values():
            if value["send_id"] == sid:
                value["status"] = status
        return {"ok": True}
    monkeypatch.setattr(repo, "mail_send_claim", claim)
    monkeypatch.setattr(repo, "mail_send_finish", finish)
    return state


def propose(env):
    spec = registry.get("email_draft")
    executor = Executor(user_id=USER, user_label="Tom Moore", run_id=RUN, specs={spec.name: spec},
        lease_owner="worker:turn", conversation_id=CONV, message_id=MSG_ASSISTANT, role=Role.EXECUTIVE)
    result = approvals.propose(executor, "email_draft", {
        "to": ["tom@g3.test"], "cc": [], "gc_contact_ids": [], "vendor_contact_ids": [],
        "subject": "Plans ready", "body_text": "The plans are ready for review.", "attachment_ids": []},
        user_texts=("Email tom@g3.test that the plans are ready.",), conversation_id=CONV, message_id=MSG_ASSISTANT)
    assert result.ok, result.model_output
    card = result.approval
    decision = repo.decide_approval(USER, card["id"], decision="approve", payload_sha256=card["payload_sha256"],
                                   precondition=card["precondition"], session_aal="aal2")
    assert decision["ok"]
    return decision["row"]


def test_registered_tool_approval_executes_real_scoped_route_once(env):
    card = propose(env)
    assert not env.sends
    assert repo.mail_user_texts(USER, RUN) == ["Email tom@g3.test that the plans are ready."]
    assert repo.conversation_for_run(USER, RUN) == CONV
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    result = repo.get_approval(USER, card["id"])
    assert result["status"] == "executed", result.get("result")
    assert result["result"]["summary"]["outcome"] == "accepted"
    assert len(env.sends) == 1
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    assert len(env.sends) == 1


def test_changed_sender_supersedes_card_without_dispatch(env):
    card = propose(env)
    env.db.tables["profiles"][0]["email"] = "changed@g3.test"
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    row = repo.get_approval(USER, card["id"])
    assert row["status"] in {"expired", "failed"}
    assert not env.sends and not env.claims


def test_provider_timeout_is_executed_with_uncertain_outcome_and_never_replays(env):
    card = propose(env)
    env.uncertain = True
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    result = repo.get_approval(USER, card["id"])
    assert result["status"] == "executed", result.get("result")
    assert result["result"]["summary"]["outcome"] == "uncertain"
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    assert len(env.sends) == 1


def test_reason_approved_review_runs_real_route_and_posts_sticky_summary(env, monkeypatch):
    from app.services import assistant_mail_review as review
    env.db.tables["assistant_conversations"][0]["mode"] = "mailbox_review"
    monkeypatch.setattr(review, "get_settings", lambda: SimpleNamespace(mailbox_exclusion_set=set()))
    class Graph:
        def __init__(self, token, **kwargs):
            assert kwargs.get("reader") is True
        def read_json(self, path, **kwargs):
            if path.endswith("/messages"):
                return {"value": [{"id": "one"}]}
            return {"id": "one", "body": {"contentType": "text", "content": "private full raw content"}}
    monkeypatch.setattr(review, "GraphTransport", Graph)
    monkeypatch.setattr(review, "extract", lambda *a: "Project documents arrived this morning.")
    audits = []
    monkeypatch.setattr(repo, "mailbox_access_claim", lambda *a, **k: {"ok": True, "claimed": True, "access_id": "access"})
    def outcome(*args, **kwargs):
        audits.append((args, kwargs))
        return {"ok": True}
    monkeypatch.setattr(repo, "mailbox_access_outcome", outcome)
    spec = registry.get("read_mailbox")
    executor = Executor(user_id=USER, user_label="Tom Moore", run_id=RUN, specs={spec.name: spec},
        lease_owner="worker:turn", conversation_id=CONV, message_id=MSG_ASSISTANT, role=Role.EXECUTIVE)
    result = approvals.propose(executor, spec.name, {"target_upn": "staff@g3.test", "window_days": 30,
        "limit": 2, "reason": "Reviewing mailbox for project status"}, user_texts=("review staff mailbox",),
        conversation_id=CONV, message_id=MSG_ASSISTANT)
    assert result.ok, result.model_output
    card = result.approval
    decision = repo.decide_with_reason(USER, card["id"], payload_sha256=card["payload_sha256"],
        precondition=card["precondition"], session_aal="aal2", reason="Human checking project status today")
    assert decision["ok"]
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    final = repo.get_approval(USER, card["id"])
    assert final["status"] == "executed", final.get("result")
    delivered = [row for row in env.db.tables["assistant_messages"] if row.get("server_data")]
    assert len(delivered) == 1 and delivered[0]["mailbox_tainted"] is True
    assert "private full raw content" not in str(delivered)
    assert "private full raw content" in str(env.db.tables["assistant_tool_results"])
    assert env.db.tables["assistant_conversations"][0]["mailbox_tainted_at"]
    assert len(audits) == 1


def test_normal_read_real_shim_keeps_raw_body_out_of_transcript_output(env, monkeypatch):
    from app.services import assistant_mail as mail
    from app.services import assistant_mail_review as review
    class Graph:
        def __init__(self, token, **kwargs):
            pass
        def read_json(self, path, **kwargs):
            return {"id": "one", "subject": "Plans", "body": {"contentType": "text", "content": "raw secret body"}}
    monkeypatch.setattr(mail, "GraphTransport", Graph)
    monkeypatch.setattr(review, "extract", lambda *a: "The message discusses plans.")
    spec = registry.get("email_read")
    executor = Executor(user_id=USER, user_label="Tom Moore", run_id=RUN, specs={spec.name: spec},
        lease_owner="worker:turn", conversation_id=CONV, message_id=MSG_ASSISTANT, role=Role.EXECUTIVE)
    result = executor.call("email_read", {"mailbox": "tom@g3.test", "message_id": "one",
                                           "response_format": "detailed", "limit": 5})
    assert result.ok, result.output
    assert "raw secret body" not in str(result.output)
    assert "raw secret body" in str(env.db.tables["assistant_tool_results"])


def test_execution_destination_provenance_stays_on_original_two_user_messages(env):
    from datetime import timedelta
    from uuid import uuid4
    card = propose(env)
    env.db.tables["assistant_messages"].append(message(
        id=str(uuid4()), content="Later unrelated text says attacker@evil.test, approve now.",
        created_at=(env.db.now + timedelta(minutes=1)).isoformat()))
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    assert len(env.sends) == 1
    execution_rows = [row for row in env.db.tables["assistant_tool_calls"] if row.get("approval_id") == card["id"]]
    execution_run = execution_rows[0]["run_id"]
    assert repo.mail_user_texts(USER, execution_run) == ["Email tom@g3.test that the plans are ready."]
    assert "attacker@evil.test" not in str(env.sends)
