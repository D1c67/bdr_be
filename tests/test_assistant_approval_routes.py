"""The approval routes of the assistant API (app/routers/assistant.py;
docs/ASSISTANT_PHASE1_PLAN.md 3.8, 3.10 and section 5).

Invariants pinned here:

  read      GET /approvals/{id} expires a timed-out card and fails a stranded
            approved card first; the payload is never served;
  approve   the person's own session only (a bdra_ principal is 403); the
            queue check comes before the compare-and-set; the CAS outcome
            maps to 404 or 409 with X-Error-Code; one execute job at
            priority 40 with max_attempts 1; an enqueue failure fails the
            card and answers 503; bells for the card are dismissed;
  reject    settles the message; the hash is not checked;
  revert    a new pending card pointing at the original; a second one is
            409 revert_exists; a RevertNotPossible is 409 with its sentence,
            and the original's revert_blocked carries it;
  partial   recheck and resolve only for a partial card;
  activity  newest first, keyset pagination, stranded cards repaired;
  firewall  the assistant's own controls are deny-listed and name live
            routes;
  imports   app.main never loads the approval service, the job, the executor
            or the shim.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.assistant import access, approvals, firewall, principal
from app.core import supabase_client
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant as router_mod
from app.services import llm_queue, notifications

from tests.assistant_fakes import (
    CONV, MSG_ASSISTANT, NOW, PROJECT, SECRET, USER,
    FakeDB, approval, conversation, delegation, install_probes, message, pending_answer, profile, settings_row,
)

EM_DASH = chr(0x2014)
A1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
A2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
A3 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
HASH = "a" * 64
PRE = {"v": 1, "target_sha256": "b" * 64, "gate": {"closed": False, "phase": "open"}}
ARGS = {"project_id": PROJECT, "amount": "250.00", "notes": None, "url": None}
OUT_KEYS = {
    "id", "status", "display_status", "action_kind", "project_id", "conversation_id", "message_id",
    "preview", "payload_sha256", "precondition", "expires_at", "expired_reason", "superseded_by",
    "reverts_approval_id", "decided_at", "executed_at", "created_at", "result", "revertible",
    "revert_blocked", "revert", "diff",
}


def _card(**over):
    base = dict(action_kind="probe_set", payload={"v": 1, "tool": "probe_set", "tool_version": "1", "args": ARGS,
                                                  "request": {"json": {"amount": "250.00", "notes": None}}},
                payload_sha256=HASH, precondition=PRE,
                preview={"v": 1, "title": "Set probe on 26.9.7130", "rows": [
                    {"field": "amount", "label": "Amount", "before": "100.00", "after": "250.00", "changed": True}]})
    base.update(over)
    return approval(**base)


def _ledger(approval_id, before_target, after_target, *, completed=True):
    rows = [{"id": f"tc-{approval_id[:4]}-b", "user_id": USER, "approval_id": approval_id, "phase": "begun",
             "idempotency_key": f"{approval_id}:1", "before_snapshot": {"target": before_target, "gate": {}},
             "after_snapshot": None, "created_at": NOW.isoformat()}]
    if completed:
        rows.append({"id": f"tc-{approval_id[:4]}-c", "user_id": USER, "approval_id": approval_id,
                     "phase": "completed", "idempotency_key": f"{approval_id}:1", "before_snapshot": None,
                     "after_snapshot": {"target": after_target}, "created_at": (NOW + timedelta(seconds=1)).isoformat()})
    return rows


BEFORE = {"exists": True, "amount": "50.00", "notes": None, "updated_at": "2026-10-22T09:00:00+00:00"}
AFTER = {"exists": True, "amount": "250.00", "notes": None, "updated_at": "2026-10-22T10:00:00+00:00"}


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[delegation()], assistant_conversations=[conversation()],
        assistant_messages=[message(), pending_answer(status="waiting_approval", content="x",
                                                      blocks=[{"type": "action_card", "approval_id": A1}])],
        assistant_approvals=[], assistant_tool_calls=[], assistant_tool_results=[], llm_call_log=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    monkeypatch.setattr(router_mod, "_execute_queue_available", lambda: True)
    s = SimpleNamespace(assistant_token_secret=SECRET, assistant_enabled=True)
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    bdr, _specs = install_probes(monkeypatch)
    who = {"role": Role.EXECUTIVE, "aal": "aal2", "mfa_enrolled": True, "via": None}

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=who["role"], is_active=True, aal=who["aal"],
                           mfa_enrolled=who["mfa_enrolled"], via_assistant_run_id=who["via"])

    jobs: list[dict] = []

    def enqueue(job_type, **kw):
        jobs.append({"job_type": job_type, **kw})
        return {"id": f"job-{len(jobs)}"}

    monkeypatch.setattr(llm_queue, "enqueue", enqueue)
    latest: dict = {"job": None}
    monkeypatch.setattr(llm_queue, "latest_job", lambda job_type, target: latest["job"])
    dismissed: list[dict] = []
    monkeypatch.setattr(notifications, "dismiss_notifications", lambda **kw: dismissed.append(kw))
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app, raise_server_exceptions=True)
    return SimpleNamespace(client=client, db=db, who=who, jobs=jobs, dismissed=dismissed, bdr=bdr, latest=latest)


def _row(api, approval_id):
    return next(r for r in api.db.tables["assistant_approvals"] if r["id"] == approval_id)


def _answer(api):
    return next(r for r in api.db.tables["assistant_messages"] if r["id"] == MSG_ASSISTANT)


def _code(res, status_code, code):
    assert res.status_code == status_code, res.text
    assert res.headers["X-Error-Code"] == code
    assert res.json()["detail"]["code"] == code


def _approve(api, approval_id=A1, **over):
    body = {"payload_sha256": HASH, "precondition": PRE}
    body.update(over)
    return api.client.post(f"/assistant/approvals/{approval_id}/approve", json=body)


# ── GET /approvals/{id} ──────────────────────────────────────────────────────


def test_get_serves_the_contract_shape_without_the_payload(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]
    res = api.client.get(f"/assistant/approvals/{A1}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == OUT_KEYS
    assert body["status"] == body["display_status"] == "pending"
    assert body["payload_sha256"] == HASH and body["precondition"] == PRE
    assert body["revertible"] is False and body["revert"] is None and body["diff"] is None
    assert "payload" not in body and "args" not in res.text


def test_get_unknown_foreign_or_malformed_ids_are_404(api):
    api.db.tables["assistant_approvals"] = [_card(id=A2, user_id="someone-else")]
    for path in (f"/assistant/approvals/{A1}", f"/assistant/approvals/{A2}", "/assistant/approvals/nope"):
        _code(api.client.get(path), 404, "approval_not_found")


def test_get_expires_a_timed_out_card_first(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1, expires_at=(NOW - timedelta(minutes=1)).isoformat())]
    body = api.client.get(f"/assistant/approvals/{A1}").json()
    assert body["status"] == "expired" and body["expired_reason"] == "timed_out"
    assert _row(api, A1)["status"] == "expired"


def test_get_fails_a_stranded_approved_card(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1, status="approved",
                                                  decided_at=(NOW - timedelta(minutes=10)).isoformat())]
    api.latest["job"] = {"id": "j", "status": "queued"}
    assert api.client.get(f"/assistant/approvals/{A1}").json()["status"] == "approved"
    api.latest["job"] = None
    body = api.client.get(f"/assistant/approvals/{A1}").json()
    assert body["status"] == "failed" and body["result"] == {"ok": False, "error": approvals.STRANDED_ERROR}
    assert _answer(api)["status"] == "done"


# ── approve ──────────────────────────────────────────────────────────────────


def test_approve_moves_the_card_and_enqueues_one_execute_job(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]
    res = _approve(api)
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "approved" and body["display_status"] == "approved"
    assert [j["job_type"] for j in api.jobs] == ["assistant_approval_execute"]
    job = api.jobs[0]
    assert job["target_id"] == A1 and job["project_id"] == PROJECT
    assert job["payload"] == {"approval_id": A1, "user_id": USER} and job["created_by"] == USER
    assert job["priority"] == 40 and job["raise_on_active"] is True and job["max_attempts_override"] == 1
    row = _row(api, A1)
    assert row["execute_job_id"] == "job-1" and row["decided_by"] == USER and row["decided_session_aal"] == "aal2"
    assert api.dismissed == [{"user_id": USER, "metadata_eq": {"approval_id": A1}, "type_prefixes": ["assistant."]}]
    # The message keeps waiting while the card runs.
    assert _answer(api)["status"] == "waiting_approval"


def test_approve_maps_every_cas_refusal(api):
    api.db.tables["assistant_approvals"] = [
        _card(id=A1),
        _card(id=A2, status="expired", expired_reason="changed_since_preview", superseded_by=A1),
        _card(id=A3, expires_at=(NOW - timedelta(minutes=1)).isoformat()),
    ]
    _code(_approve(api, payload_sha256="c" * 64), 409, "approval_stale")
    _code(_approve(api, precondition={**PRE, "target_sha256": "d" * 64}), 409, "approval_stale")
    _code(_approve(api, A2), 409, "approval_superseded")
    _code(_approve(api, A3), 409, "approval_expired")
    assert _row(api, A3)["expired_reason"] == "timed_out"
    _code(_approve(api, "dddddddd-dddd-4ddd-8ddd-dddddddddddd"), 404, "approval_not_found")
    assert _approve(api).status_code == 202
    _code(_approve(api), 409, "approval_not_pending")
    assert len(api.jobs) == 1


def test_approve_validates_its_body(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]
    assert _approve(api, payload_sha256="A" * 64).status_code == 422
    assert _approve(api, payload_sha256="abc").status_code == 422
    assert api.client.post(f"/assistant/approvals/{A1}/approve", json={"payload_sha256": HASH}).status_code == 422
    assert _row(api, A1)["status"] == "pending"


def test_an_assistant_principal_can_never_decide(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]
    api.who["via"] = "run-1"
    for path in ("approve", "reject", "revert", "recheck", "resolve"):
        body = {"payload_sha256": HASH, "precondition": PRE} if path == "approve" else (
            {"outcome": "applied"} if path == "resolve" else {})
        _code(api.client.post(f"/assistant/approvals/{A1}/{path}", json=body), 403, "assistant_cannot_approve")
    assert _row(api, A1)["status"] == "pending" and api.jobs == []


def test_queue_unavailable_is_503_before_any_cas(api, monkeypatch):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]
    monkeypatch.setattr(router_mod, "_execute_queue_available", lambda: False)
    _code(_approve(api), 503, "assistant_unavailable")
    assert _row(api, A1)["status"] == "pending"
    assert not any(name == "assistant_approval_decide" for name, _p in api.db.rpc_calls)


def test_execute_queue_check_reads_the_claim_types(monkeypatch):
    def settings(**over):
        base = {"llm_queue_enabled": True, "llm_queue_claim_job_types": ()}
        base.update(over)
        return SimpleNamespace(**base)

    monkeypatch.setattr(router_mod, "get_settings", lambda: settings())
    assert router_mod._execute_queue_available() is True
    monkeypatch.setattr(router_mod, "get_settings", lambda: settings(llm_queue_claim_job_types=("assistant_turn",)))
    assert router_mod._execute_queue_available() is False and router_mod._queue_available() is True
    monkeypatch.setattr(router_mod, "get_settings",
                        lambda: settings(llm_queue_claim_job_types=("assistant_turn", "assistant_approval_execute")))
    assert router_mod._execute_queue_available() is True
    monkeypatch.setattr(router_mod, "get_settings", lambda: settings(llm_queue_enabled=False))
    assert router_mod._execute_queue_available() is False


def test_enqueue_failure_fails_the_card_and_answers_503(api, monkeypatch):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]

    def boom(job_type, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(llm_queue, "enqueue", boom)
    _code(_approve(api), 503, "assistant_unavailable")
    row = _row(api, A1)
    assert row["status"] == "failed" and row["result"] == {"ok": False, "error": router_mod.ENQUEUE_FAILED}
    assert _answer(api)["status"] == "done"


def test_an_already_active_job_counts_as_success(api, monkeypatch):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]

    def collide(job_type, **kw):
        raise llm_queue.JobAlreadyActive({"id": "job-old"})

    monkeypatch.setattr(llm_queue, "enqueue", collide)
    assert _approve(api).status_code == 202
    assert _row(api, A1)["execute_job_id"] == "job-old"


def test_approve_needs_an_aal2_session_while_mfa_is_required(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]
    api.db.tables["assistant_delegations"] = []
    api.who["aal"] = "aal1"
    res = _approve(api)
    assert res.status_code == 403 and res.json()["detail"] == "mfa_step_up_required"
    assert res.headers["X-Error-Code"] == "mfa_step_up_required"
    api.who["mfa_enrolled"] = False
    assert _approve(api).json()["detail"] == "mfa_enrollment_required"
    assert _row(api, A1)["status"] == "pending"


# ── reject ───────────────────────────────────────────────────────────────────


def test_reject_ignores_the_hash_settles_the_message_and_clears_bells(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]
    res = api.client.post(f"/assistant/approvals/{A1}/reject", json={})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "rejected"
    assert _answer(api)["status"] == "done"
    assert api.dismissed == [{"user_id": USER, "metadata_eq": {"approval_id": A1}, "type_prefixes": ["assistant."]}]
    _code(api.client.post(f"/assistant/approvals/{A1}/reject", json={}), 409, "approval_not_pending")
    _code(api.client.post("/assistant/approvals/nope/reject", json={}), 404, "approval_not_found")


def test_reject_keeps_the_message_waiting_while_another_card_is_live(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1), _card(id=A2)]
    assert api.client.post(f"/assistant/approvals/{A1}/reject", json={}).status_code == 200
    assert _answer(api)["status"] == "waiting_approval"
    assert api.client.post(f"/assistant/approvals/{A2}/reject", json={}).status_code == 200
    assert _answer(api)["status"] == "done"


# ── revert ───────────────────────────────────────────────────────────────────


def _executed(api, before=BEFORE):
    api.db.tables["assistant_approvals"] = [_card(id=A1, status="executed", executed_at=NOW.isoformat(),
                                                  result={"ok": True, "status_code": 200})]
    api.db.tables["assistant_tool_calls"] = _ledger(A1, before, AFTER)


def test_revert_creates_a_pending_undo_card_and_only_one(api):
    _executed(api)
    body = api.client.get(f"/assistant/approvals/{A1}").json()
    assert body["revertible"] is True and body["revert_blocked"] is None
    assert body["diff"] == [{"field": "amount", "label": "Amount", "before": "$50.00", "after": "$250.00",
                             "changed": True}]
    res = api.client.post(f"/assistant/approvals/{A1}/revert", json={})
    assert res.status_code == 201, res.text
    undo = res.json()
    assert undo["status"] == "pending" and undo["reverts_approval_id"] == A1
    assert undo["action_kind"] == "probe_restore" and undo["preview"]["title"].startswith("Undo: ")
    assert undo["preview"]["revert_of"] == A1
    # Nothing was written: only the preview reads went out.
    assert {s["route"][0] for s in api.bdr.sent} == {"GET"}
    after = api.client.get(f"/assistant/approvals/{A1}").json()
    assert after["revertible"] is False and after["revert"] == {"id": undo["id"], "status": "pending"}
    _code(api.client.post(f"/assistant/approvals/{A1}/revert", json={}), 409, "revert_exists")
    # A revert is never itself revertible.
    _code(api.client.post(f"/assistant/approvals/{undo['id']}/revert", json={}), 409, "approval_not_revertible")


def test_revert_refusals(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1)]
    _code(api.client.post(f"/assistant/approvals/{A1}/revert", json={}), 409, "approval_not_revertible")
    _code(api.client.post(f"/assistant/approvals/{A2}/revert", json={}), 404, "approval_not_found")


def test_revert_not_possible_is_409_with_its_sentence_and_revert_blocked(api):
    _executed(api, before={**BEFORE, "exists": False})
    body = api.client.get(f"/assistant/approvals/{A1}").json()
    assert body["revertible"] is False
    assert body["revert_blocked"] == "There was no labor row before this change."
    res = api.client.post(f"/assistant/approvals/{A1}/revert", json={})
    _code(res, 409, "approval_not_revertible")
    assert res.json()["detail"]["message"] == "There was no labor row before this change."


def test_revert_of_a_no_op_is_revert_refused(api):
    _executed(api, before={**BEFORE, "amount": "100.00"})   # the record already holds the before values
    res = api.client.post(f"/assistant/approvals/{A1}/revert", json={})
    _code(res, 409, "revert_refused")
    assert "already have these values" in res.json()["detail"]["message"]


# ── recheck and resolve ──────────────────────────────────────────────────────


def _partial(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1, status="failed", result={
        "ok": False, "status_code": 500, "error": "HTTP 500", "may_have_partially_run": True})]
    api.db.tables["assistant_tool_calls"] = _ledger(A1, BEFORE, None, completed=False)
    _answer(api)["status"] = "waiting_review"


def test_recheck_compares_before_with_live_for_a_partial_card(api):
    _partial(api)
    res = api.client.post(f"/assistant/approvals/{A1}/recheck", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"approval", "rows", "matches_before", "matches_intended"}
    assert body["approval"]["display_status"] == "partial"
    amount = next(r for r in body["rows"] if r["field"] == "amount")
    assert amount == {"field": "amount", "label": "Amount", "before": "$50.00", "live": "$100.00"}
    assert body["matches_before"] is False and body["matches_intended"] is False


def test_recheck_and_resolve_refuse_a_card_that_is_not_partial(api):
    api.db.tables["assistant_approvals"] = [_card(id=A1, status="failed", result={"ok": False, "error": "x"})]
    _code(api.client.post(f"/assistant/approvals/{A1}/recheck", json={}), 409, "approval_not_partial")
    _code(api.client.post(f"/assistant/approvals/{A1}/resolve", json={"outcome": "applied"}),
          409, "approval_not_partial")


def test_resolve_records_the_review_and_finishes_the_message(api):
    _partial(api)
    assert api.client.post(f"/assistant/approvals/{A1}/resolve", json={"outcome": "maybe"}).status_code == 422
    res = api.client.post(f"/assistant/approvals/{A1}/resolve", json={"outcome": "not_applied"})
    assert res.status_code == 200, res.text
    review = res.json()["result"]["review"]
    assert review["outcome"] == "not_applied" and review["by"] == USER and review["at"]
    assert _answer(api)["status"] == "done"


# ── activity ─────────────────────────────────────────────────────────────────


def test_activity_pages_newest_first_and_repairs_stranded_cards(api):
    api.db.tables["assistant_approvals"] = [
        _card(id=A1, created_at=(NOW - timedelta(minutes=3)).isoformat()),
        _card(id=A2, status="approved", decided_at=(NOW - timedelta(minutes=10)).isoformat(),
              created_at=(NOW - timedelta(minutes=2)).isoformat()),
        _card(id=A3, status="rejected", created_at=(NOW - timedelta(minutes=1)).isoformat()),
        _card(id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", user_id="someone-else"),
    ]
    page = api.client.get("/assistant/activity?limit=2").json()
    assert [i["id"] for i in page["items"]] == [A3, A2]
    assert set(page["items"][0]) == OUT_KEYS
    assert page["items"][1]["status"] == "failed"   # stranded, no active job
    assert page["next_before"] == _row(api, A2)["created_at"]
    nxt = api.client.get("/assistant/activity", params={"limit": 2, "before": page["next_before"]}).json()
    assert [i["id"] for i in nxt["items"]] == [A1] and nxt["next_before"] is None


def test_activity_validates_its_query(api):
    assert api.client.get("/assistant/activity?limit=0").status_code == 422
    assert api.client.get("/assistant/activity?limit=51").status_code == 422
    assert api.client.get("/assistant/activity?before=yesterday").status_code == 422
    assert api.client.get("/assistant/activity").json() == {"items": [], "next_before": None}


def test_activity_and_cards_are_internal_only_and_404_when_off(api):
    api.who["role"] = Role.ESTIMATOR
    assert api.client.get("/assistant/activity").status_code == 403
    assert api.client.get(f"/assistant/approvals/{A1}").status_code == 403
    api.who["role"] = Role.EXECUTIVE
    api.db.tables["assistant_settings"][0]["enabled"] = False
    for res in (api.client.get("/assistant/activity"), api.client.get(f"/assistant/approvals/{A1}"),
                _approve(api)):
        assert res.status_code == 404 and res.json() == {"detail": "Not Found"}


# ── messages carry the waiting statuses and the card blocks ─────────────────


def test_messages_pass_waiting_statuses_and_action_cards(api):
    res = api.client.get(f"/assistant/messages/{MSG_ASSISTANT}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "waiting_approval"
    assert body["blocks"] == [{"type": "action_card", "approval_id": A1}]
    _answer(api)["status"] = "waiting_review"
    assert api.client.get(f"/assistant/messages/{MSG_ASSISTANT}").json()["status"] == "waiting_review"
    assert router_mod._status_out("something_new") == "running"
    detail = api.client.get(f"/assistant/conversations/{CONV}").json()
    assert detail["messages"][-1]["status"] == "waiting_review"


# ── firewall, constants and imports ──────────────────────────────────────────


ASSISTANT_DENY = (
    ("POST", "/assistant/approvals/{approval_id}/approve"),
    ("POST", "/assistant/approvals/{approval_id}/reject"),
    ("POST", "/assistant/approvals/{approval_id}/revert"),
    ("POST", "/assistant/approvals/{approval_id}/recheck"),
    ("POST", "/assistant/approvals/{approval_id}/resolve"),
    ("PATCH", "/assistant/settings"),
    ("POST", "/assistant/conversations"),
    ("POST", "/assistant/conversations/{conversation_id}/messages"),
)


def test_the_assistants_own_controls_are_deny_listed_and_live():
    import app.main

    table = {(m, r.path_format) for r in app.main.app.routes if isinstance(r, APIRoute) for m in r.methods or ()}
    allow = frozenset(ASSISTANT_DENY)
    for pair in ASSISTANT_DENY:
        assert pair in firewall.DENY_LIST, pair
        assert pair in table, pair
        verdict = firewall.check(pair[0], pair[1], allow)
        assert not verdict.allowed and verdict.reason == "deny_listed", pair
    # Reads stay reachable for a toolset that names them (none does today).
    assert not firewall.is_deny_listed("GET", "/assistant/approvals/{approval_id}")
    assert not firewall.is_deny_listed("GET", "/assistant/activity")


def test_router_execute_job_type_matches_the_job_module():
    from app.assistant import approvals_job

    assert router_mod.EXECUTE_JOB_TYPE == approvals_job.JOB_TYPE == llm_queue.JOB_ASSISTANT_APPROVAL_EXECUTE


def test_error_codes_and_messages_have_no_em_dash():
    for status_code, code in router_mod.DECIDE_CODES.values():
        assert EM_DASH not in code
    assert EM_DASH not in router_mod.ENQUEUE_FAILED


def test_importing_app_main_never_loads_the_approval_service():
    """The router imports the approval service lazily, so a fresh
    interpreter that imports app.main has none of the write machinery
    loaded."""
    import app.main

    code = (
        "import sys, app.main; "
        "bad = [m for m in ('app.assistant.approvals', 'app.assistant.approvals_job', "
        "'app.assistant.executor', 'app.assistant.shim') if m in sys.modules]; "
        "print(','.join(bad))"
    )
    root = Path(app.main.__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", out.stdout


# ── phase 4: the reason of a sensitive_read card (3.2.2) ───────────────────


REASON = "Checking why the estimate came back late on 26.9.7127."


@pytest.fixture
def sensitive(api, monkeypatch):
    """A registered sensitive_read spec and a pending card of it."""
    from app.assistant import registry
    from tests.test_assistant_sensitive_read import sensitive_spec

    spec = sensitive_spec()
    monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    api.db.tables["assistant_approvals"] = [_card(id=A1, action_kind=spec.name), _card(id=A2)]
    return spec


def test_the_router_names_the_registry_effect():
    from app.assistant import registry

    assert router_mod.SENSITIVE_READ == registry.EFFECT_SENSITIVE_READ
    assert EM_DASH not in router_mod.FIND_HINT_FOUND + router_mod.FIND_HINT_NONE


def test_a_sensitive_card_without_a_reason_is_422_and_stays_pending(api, sensitive):
    for reason in (None, "", "   ", "too short", " 9 chars  "):
        body = {} if reason is None else {"reason": reason}
        _code(_approve(api, **body), 422, "reason_required")
    assert _row(api, A1)["status"] == "pending" and _row(api, A1)["decision_reason"] is None
    assert api.jobs == [] and api.db.rpc_calls == []
    # Over 500 characters is refused by the body schema itself.
    assert _approve(api, reason="x" * 501).status_code == 422
    assert _row(api, A1)["status"] == "pending"


def test_a_sensitive_card_approves_with_the_reason_stored_once(api, sensitive):
    res = _approve(api, reason=f"  {REASON}  ")
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "approved"
    assert REASON not in res.text
    row = _row(api, A1)
    assert row["decision_reason"] == REASON and row["decided_by"] == USER
    assert [name for name, _p in api.db.rpc_calls] == ["assistant_approval_decide_with_reason"]
    assert [j["job_type"] for j in api.jobs] == ["assistant_approval_execute"]
    # A second click cannot set it again.
    _code(_approve(api, reason="A different reason, typed later."), 409, "approval_not_pending")
    assert _row(api, A1)["decision_reason"] == REASON and len(api.jobs) == 1


def test_a_sensitive_card_keeps_the_compare_and_set(api, sensitive):
    _code(_approve(api, payload_sha256="c" * 64, reason=REASON), 409, "approval_stale")
    _code(_approve(api, "dddddddd-dddd-4ddd-8ddd-dddddddddddd", reason=REASON), 404, "approval_not_found")
    assert _row(api, A1)["status"] == "pending" and _row(api, A1)["decision_reason"] is None


def test_an_rpc_reason_refusal_is_422_reason_invalid(api, sensitive, monkeypatch):
    from app.assistant import repo

    monkeypatch.setattr(repo, "decide_with_reason",
                        lambda *a, **k: {"ok": False, "reason": "reason_invalid", "row": None})
    _code(_approve(api, reason=REASON), 422, "reason_invalid")
    assert api.jobs == []


def test_other_cards_ignore_a_reason(api, sensitive):
    res = _approve(api, A2, reason=REASON)
    assert res.status_code == 202, res.text
    assert _row(api, A2)["decision_reason"] is None
    assert [name for name, _p in api.db.rpc_calls] == ["assistant_approval_decide"]
    # An older frontend never sends one.
    api.db.tables["assistant_approvals"].append(_card(id=A3))
    assert _approve(api, A3).status_code == 202
