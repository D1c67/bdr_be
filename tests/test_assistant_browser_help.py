"""Human help codes are write-only, generation-bound and consumed once in SQL."""
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import assistant_browser_help as help_flow
from app.services import assistant_browser_records as records
from app.services import assistant_browser_vault as vault


@pytest.fixture
def context(monkeypatch):
    owner, job_id, credential_id, help_id, attempt = [str(uuid4()) for _ in range(5)]
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
    job = {"id": job_id, "principal_kind": "user", "principal_user_id": owner,
           "user_credential_id": credential_id, "user_credential_version": 2, "status": "waiting_help"}
    state = {"id": help_id, "status": "open", "is_login_scope": True, "login_attempt_id": attempt, "expires_at": expiry}
    credential = {"id": credential_id, "version": 2, "status": "active", "login_attempt_id": attempt, "login_attempt_expires_at": expiry}
    monkeypatch.setattr(vault, "get_settings", lambda: SimpleNamespace(
        user_cred_kek=base64.b64encode(b"k" * 32).decode(), user_cred_kek_id="u1", user_cred_old_keys=""))
    monkeypatch.setattr(records.repo, "browser_job", lambda u, j: job if (u, j) == (owner, job_id) else None)
    monkeypatch.setattr(records.repo, "browser_help_state", lambda *a: state)
    monkeypatch.setattr(records.repo, "browser_credential_by_id", lambda *a: credential)
    stored = []
    monkeypatch.setattr(records.repo, "browser_help_records", lambda *a: stored)
    monkeypatch.setattr(vault, "decrypt_credential", lambda row: "PERSONAL_PASSWORD")
    from app.services import assistant_browser_runtime as runtime
    monkeypatch.setattr(runtime, "sanitize", lambda job, payload: payload)
    def reply(user_id, job, help_id, client_id, action, row):
        stored.append({**row, "user_id": owner, "job_id": job_id, "created_at": expiry})
        return {"ok": True, "record": stored[-1], "help": state}
    monkeypatch.setattr(records.repo, "browser_help_reply", reply)
    return SimpleNamespace(owner=owner, job_id=job_id, job=job, state=state, credential=credential,
                           help_id=help_id, attempt=attempt, stored=stored)


def test_code_is_encrypted_and_only_placeholder_returned(context):
    result = help_flow.owner_reply(context.owner, context.job_id, expected_help_id=context.help_id,
        action="message", client_msg_id=str(uuid4()), secret_value="123456")
    assert "123456" not in str(result) and "123456" not in str(context.stored)
    assert result["messages"][0]["is_secret"] is True and result["messages"][0]["body"] is None
    assert result["secret_available"] is False
    assert len(context.stored[0]["secret_hash"]) == 64 and context.stored[0]["secret_key_id"] == "u1"


@pytest.mark.parametrize("value", ["abc123", "123", "1" * 13, "12 3456", "123456\n"])
def test_code_formats_align_with_durable_redactor(context, value):
    with pytest.raises(records.BrowserContentUnavailable):
        help_flow.owner_reply(context.owner, context.job_id, expected_help_id=context.help_id,
            action="message", client_msg_id=str(uuid4()), secret_value=value)
    assert context.stored == []


@pytest.mark.parametrize("body", ["123456", "Here is PERSONAL_PASSWORD please use it"])
def test_codes_and_passwords_do_not_enter_help_transcript(context, body):
    with pytest.raises(records.BrowserContentUnavailable):
        help_flow.owner_reply(context.owner, context.job_id, expected_help_id=context.help_id,
            action="message", client_msg_id=str(uuid4()), body=body)
    assert context.stored == []


def test_replaced_help_refuses_stale_human_submission(context):
    with pytest.raises(records.BrowserContentUnavailable):
        help_flow.owner_reply(context.owner, context.job_id, expected_help_id=str(uuid4()),
            action="give_up", client_msg_id=str(uuid4()))
    assert context.stored == []


def test_consumption_decrypts_only_rpc_returned_preclear_envelope(context, monkeypatch):
    help_flow.owner_reply(context.owner, context.job_id, expected_help_id=context.help_id,
        action="message", client_msg_id=str(uuid4()), secret_value="123456")
    row = context.stored[0]
    calls = []
    def consume(*args):
        calls.append(args)
        return {"ok": len(calls) == 1, "record": row}
    monkeypatch.setattr(records.repo, "browser_help_consume", consume)
    kwargs = dict(help_id=context.help_id, record_id=row["id"], login_attempt_id=context.attempt,
                  worker_id=str(uuid4()), boot_id=str(uuid4()), lease_epoch=2)
    assert help_flow.consume_secret(context.job, **kwargs) == "123456"
    with pytest.raises(records.BrowserContentUnavailable):
        help_flow.consume_secret(context.job, **kwargs)
    assert calls[0][0:2] == (context.owner, context.job_id)


def test_changed_credential_disables_code_entry(context):
    context.credential["version"] = 3
    assert help_flow.owner_help(context.owner, context.job_id)["secret_available"] is False


def test_open_help_never_persists_plaintext_and_passes_lease(context, monkeypatch):
    calls = []
    monkeypatch.setattr(records.repo, "browser_help_open", lambda *a: calls.append(a) or {"ok": True, "help": context.state})
    result = help_flow.open_help(context.job, reason="mfa", title="PRIVATE_TITLE", message="PRIVATE_MESSAGE",
        is_login_scope=True, help_id=context.help_id, record_id=str(uuid4()), seq=4,
        worker_id=str(uuid4()), boot_id=str(uuid4()), lease_epoch=8, login_attempt_id=context.attempt)
    assert result["id"] == context.help_id
    assert "PRIVATE_TITLE" not in str(calls) and "PRIVATE_MESSAGE" not in str(calls)
    assert calls[0][4] == 8


def _capped(context):
    context.job.update(turn_count=40, config_snapshot={"type": {"max_steps": 40}})
    context.state.update(is_login_scope=False, login_attempt_id=None)


@pytest.mark.parametrize("action,body", [("retry", None), ("message", "Please try once more")])
def test_capped_job_refuses_requeueing_replies(context, monkeypatch, action, body):
    _capped(context)
    monkeypatch.setattr(records.repo, "browser_help_reply", lambda *a: pytest.fail("requeued a capped job"))
    with pytest.raises(help_flow.BrowserTaskCapped, match="reached its limit"):
        help_flow.owner_reply(context.owner, context.job_id, expected_help_id=context.help_id,
            action=action, client_msg_id=str(uuid4()), body=body)
    assert context.stored == []


def test_capped_job_can_still_be_stopped(context):
    _capped(context)
    result = help_flow.owner_reply(context.owner, context.job_id, expected_help_id=context.help_id,
        action="give_up", client_msg_id=str(uuid4()))
    assert context.stored[0]["kind"] == "help_message" and result["retry_allowed"] is False


def test_retry_allowed_reflects_job_caps(context):
    assert help_flow.owner_help(context.owner, context.job_id)["retry_allowed"] is True
    context.job.update(cost_usd="2", input={"max_cost_usd": "2"})
    assert help_flow.owner_help(context.owner, context.job_id)["retry_allowed"] is False


def test_uncapped_job_keeps_retry_and_message(context):
    context.job.update(turn_count=39, config_snapshot={"type": {"max_steps": 40}})
    context.state.update(is_login_scope=False, login_attempt_id=None)
    for action, body in (("retry", None), ("message", "Try the RFI list")):
        help_flow.owner_reply(context.owner, context.job_id, expected_help_id=context.help_id,
            action=action, client_msg_id=str(uuid4()), body=body)
    assert [row["kind"] for row in context.stored] == ["help_message", "help_message"]


def test_capped_job_still_takes_a_private_login_code(context):
    context.job.update(turn_count=40, config_snapshot={"type": {"max_steps": 40}})
    help_flow.owner_reply(context.owner, context.job_id, expected_help_id=context.help_id,
        action="message", client_msg_id=str(uuid4()), secret_value="123456")
    assert context.stored[0]["kind"] == "help_secret"
