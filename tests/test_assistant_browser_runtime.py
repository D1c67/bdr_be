"""Private turn accounting and identity boundaries, without external I/O."""
import base64
import hashlib
import hmac
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.agent_auth import AgentLease, AgentWorker
from app.services import assistant_browser_runtime as runtime
from app.services import assistant_browser_vault as vault
from app.services.browser_agents import protocol, llm
from app.services.browser_agents.llm.base import LlmResult, LlmUsage, ToolCall


@pytest.fixture
def env(monkeypatch):
    owner, jid, cid, boot, wid = (str(uuid4()) for _ in range(5))
    worker = AgentWorker(wid, "personal", "active", boot, False, 1, ("user_browser_read",), (), "1.0.0",
                         principal_kind="user")
    lease = AgentLease(worker, jid, 3, boot)
    config = SimpleNamespace(assistant_browser_enabled=True, user_cred_kek=base64.b64encode(b"a"*32).decode(),
                             user_cred_kek_id="u1", user_cred_old_keys="")
    monkeypatch.setattr(runtime, "get_settings", lambda: config)
    monkeypatch.setattr(vault, "get_settings", lambda: config)
    monkeypatch.setattr(runtime.browser, "EXECUTION_READY", True)
    job = {"id": jid, "principal_user_id": owner, "principal_kind": "user", "status": "running",
           "user_credential_id": cid, "user_credential_version": 1, "cost_usd": "0",
           "input": {"service": "procore", "url": "https://app.procore.com/123/project/home",
                     "objective": "Read RFI status", "max_cost_usd": "2", "authority": {"model": "test-model"}}}
    cred = {"id": cid, "user_id": owner, "version": 1, "status": "active", "username": "personal@example.test",
            "origins": ["https://app.procore.com", "https://login.procore.com", "https://us02.procore.com"],
            **vault.encrypt_payload(owner, cid, 1, "credential", b"PRIVATE_PASSWORD")}
    monkeypatch.setattr(runtime, "rpc", lambda *a: job)
    monkeypatch.setattr(vault, "job_credential", lambda j: cred)
    monkeypatch.setattr(runtime.repo, "browser_credential_by_id", lambda *a: cred)
    monkeypatch.setattr(runtime.repo, "browser_session", lambda *a: None)
    monkeypatch.setattr(runtime.repo, "browser_secret_hashes", lambda *a: {})
    monkeypatch.setattr(runtime.repo, "browser_turn_history", lambda *a, **kw: [])
    from app.services import assistant_browser_help
    monkeypatch.setattr(assistant_browser_help, "worker_state", lambda *a: {"status": "none", "messages": []})
    kind = {"provider": "openai", "model": "test-model", "model_probe_ok_at": "today", "enabled": True,
            "price_in_per_mtok": "1", "price_out_per_mtok": "2", "max_steps": 40}
    monkeypatch.setattr(runtime.repo, "browser_platform_config", lambda *a: (kind, {"enabled": True}))
    monkeypatch.setattr(runtime, "get_settings", lambda: config)
    return SimpleNamespace(owner=owner, lease=lease, job=job, credential=cred, kind=kind, config=config)


def test_fence_refuses_wrong_pool_and_flag_off(env):
    env.config.assistant_browser_enabled = False
    with pytest.raises(HTTPException):
        runtime.fenced(env.lease)


def test_personal_redaction_does_not_load_org_secrets(env, monkeypatch):
    monkeypatch.setattr(runtime.redaction, "load_secrets", lambda *a, **kw: pytest.fail("org secrets"))
    digest = hmac.new(b"a"*32, b"012345", hashlib.sha256).hexdigest()
    monkeypatch.setattr(runtime.repo, "browser_secret_hashes", lambda *a: {"u1": [digest]})
    value = runtime.sanitize(env.job, {"text": "PRIVATE_PASSWORD 012345", "url": env.job["input"]["url"]})
    assert "PRIVATE_PASSWORD" not in str(value) and "012345" not in str(value)
    assert value["url"] == env.job["input"]["url"]


@pytest.mark.parametrize("origin", ["https://app.procore.com", "https://login.procore.com.evil.test", "https://login.microsoftonline.com"])
def test_login_origin_checked_before_rpc(env, monkeypatch, origin):
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: pytest.fail("must not release"))
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.identity_begin(env.lease, protocol.UserLoginBeginRequest(host="app.procore.com", origin=origin))


def test_login_release_uses_transaction_attempt_owner(env, monkeypatch):
    seen = []
    def rpc(owner, name, params):
        seen.append((owner, name, params))
        return {"granted": True, "attempt_id": str(uuid4()), "credential": env.credential}
    monkeypatch.setattr(runtime.repo, "browser_rpc", rpc)
    response = runtime.identity_begin(env.lease, protocol.UserLoginBeginRequest(host="app.procore.com", origin="https://login.procore.com"))
    assert response["password"] == "PRIVATE_PASSWORD"
    assert seen[0][0] == env.owner and seen[0][2]["p_epoch"] == 3
    env.credential["user_id"] = str(uuid4())
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.identity_begin(env.lease, protocol.UserLoginBeginRequest(host="app.procore.com", origin="https://login.procore.com"))


@pytest.mark.parametrize("domain,secure", [("evil.test", True), (".procore.com.evil.test", True), ("app.procore.com", False)])
def test_cookie_scope_refused(env, domain, secure):
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.checked_cookies([{"name": "session", "value": "secret", "domain": domain, "secure": secure}], "https://app.procore.com")


def _model(monkeypatch, action):
    calls = []
    def complete(args):
        calls.append(args)
        return LlmResult("", [ToolCall("call1", action[0], action[1])], LlmUsage(100, 20), None, None)
    monkeypatch.setattr(llm, "get_adapter", lambda provider: SimpleNamespace(complete=complete))
    from app.services import llm_gate
    monkeypatch.setattr(llm_gate, "slot", lambda *a: nullcontext())
    return calls


def test_turn_reserves_before_call_and_persists_only_envelopes(env, monkeypatch):
    calls = _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    rpc_calls = []
    tid = str(uuid4())
    def rpc(owner, name, params):
        rpc_calls.append((owner, name, params))
        if name.endswith("_begin"):
            assert not calls
            return {"claimed": True, "turn_id": tid, "token": "token", "type": env.kind}
        assert len(calls) == 1
        assert params["p_user_id"] == env.owner
        assert "PRIVATE_PASSWORD" not in str(params)
        return {"ok": True}
    monkeypatch.setattr(runtime.repo, "browser_rpc", rpc)
    status, result = runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0, title="PRIVATE_PASSWORD"))
    assert status == 200 and result["action"]["tool"] == "navigate"
    assert "PRIVATE_PASSWORD" not in calls[0].messages[0].text
    assert len(rpc_calls) == 2 and rpc_calls[0][2]["p_turn_no"] == 1
    stored = rpc_calls[-1][2]
    assert runtime._turn_content(env.job, tid, 1, "response", stored["p_response_envelope"]) == result
    assert Decimal(stored["p_actual_cost"]) < Decimal(rpc_calls[0][2]["p_estimated_cost"])


def test_turn_replay_does_not_call_model(env, monkeypatch):
    calls = _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    tid = str(uuid4())
    saved = {"stop": True, "reason": "cap_reached"}
    envelope = runtime._turn_envelope(env.job, tid, 1, "response", saved)
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"turn_id": tid, "replay_envelope": envelope})
    assert runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0)) == (200, saved)
    assert not calls


@pytest.mark.parametrize("url", ["https://evil.test/", "https://app.procore.com/999/project/home"])
def test_model_cannot_expand_project_or_origin(env, monkeypatch, url):
    _model(monkeypatch, ("navigate", {"url": url}))
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda owner, name, params:
        {"claimed": True, "turn_id": str(uuid4()), "token": "token", "type": env.kind} if name.endswith("_begin") else {"ok": True})
    assert runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0))[1]["stop"] is True


def test_begin_ambiguity_does_not_issue_second_paid_call(env, monkeypatch):
    calls = _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"code": "thinking", "claimed": False})
    assert runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0))[0] == 202
    assert calls == []


def test_configuration_race_does_not_call_provider_or_charge(env, monkeypatch):
    calls = _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    finished = []
    def rpc(owner, name, params):
        if name.endswith("_begin"):
            assert params["p_estimated_tokens"] > 4096
            return {"claimed": True, "turn_id": str(uuid4()), "token": "token",
                    "type": {**env.kind, "price_out_per_mtok": "100"}}
        finished.append(params)
        return {"ok": True}
    monkeypatch.setattr(runtime.repo, "browser_rpc", rpc)
    assert runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0))[1]["reason"] == "provider_config"
    assert not calls and finished[0]["p_actual_cost"] == "0" and finished[0]["p_actual_tokens"] == 0


def test_turn_envelope_rejects_job_or_number_transplant(env):
    tid = str(uuid4())
    envelope = runtime._turn_envelope(env.job, tid, 1, "response", {"safe": True})
    with pytest.raises(runtime.BrowserUnavailable):
        runtime._turn_content(env.job, tid, 2, "response", envelope)
    other = {**env.job, "id": str(uuid4())}
    with pytest.raises(runtime.BrowserUnavailable):
        runtime._turn_content(other, tid, 1, "response", envelope)


def _no_turn_work(monkeypatch):
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: pytest.fail("turn_begin"))
    monkeypatch.setattr(runtime.repo, "browser_turn_history", lambda *a, **kw: pytest.fail("history decrypted"))
    monkeypatch.setattr(runtime, "sanitize", lambda *a, **kw: pytest.fail("sanitized"))
    from app.services import assistant_browser_help
    monkeypatch.setattr(assistant_browser_help, "worker_state", lambda *a: pytest.fail("help state read"))


@pytest.mark.parametrize("used,snapshot", [
    ({"turn_count": 40}, {"type": {"max_steps": 40}}),
    ({"turn_count": 30}, {"type": {"max_steps": 30}}),
    ({"turn_count": 3, "tokens_in": 400000}, {"type": {"max_total_tokens": 400000}}),
    ({"turn_count": 3, "active_seconds": 1200}, {"type": {"max_active_seconds": 1200}}),
    ({"turn_count": 3, "cost_usd": "0.5"}, {"per_job_cost_cap_usd": "0.5"}),
])
def test_capped_resume_never_begins_turn_or_calls_model(env, monkeypatch, used, snapshot):
    calls = _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    env.job.update(used, config_snapshot=snapshot)
    _no_turn_work(monkeypatch)
    turn_no = int(used["turn_count"])
    assert runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=turn_no)) == (200, {"stop": True, "reason": "cap_reached"})
    assert calls == []


def test_capped_job_still_replays_its_last_turn(env, monkeypatch):
    calls = _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    env.job.update(turn_count=40, config_snapshot={"type": {"max_steps": 40}})
    tid = str(uuid4())
    saved = {"turn_no": 40, "action": {"tool": "read_page"}}
    envelope = runtime._turn_envelope(env.job, tid, 40, "response", saved)
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"turn_id": tid, "replay_envelope": envelope})
    assert runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=39)) == (200, saved)
    assert not calls


def test_uncapped_job_is_not_stopped_early(env):
    env.job.update(turn_count=39, tokens_in=1000, cost_usd="1.5", config_snapshot={"type": {"max_steps": 40}})
    assert runtime.job_capped(env.job, env.kind, {"per_job_cost_cap_usd": "2"}) is False
    env.kind["max_steps"] = 39
    assert runtime.job_capped(env.job, env.kind, {}) is True


@pytest.mark.parametrize("begun,live,expected", [
    ({"code": "cost_cap"}, None, "daily_cap_reached"),
    ({"code": "type_unavailable"}, None, "cap_reached"),
    ({"code": "type_unavailable"}, {"model": "replacement-model"}, "provider_config"),
    ({"code": "type_unavailable"}, {"provider": "other"}, "provider_config"),
    ({"code": "turn_conflict"}, None, "cap_reached"),
    ({"code": "uncertain", "claimed": False}, None, "cap_reached"),
    ({"ok": False}, None, "cap_reached"),
])
def test_unclaimed_turn_codes_map_precisely(env, monkeypatch, begun, live, expected):
    calls = _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    reads = []
    def config(*a):
        reads.append(a)
        return ({**env.kind, **(live or {})} if len(reads) > 1 else env.kind), {"enabled": True}
    monkeypatch.setattr(runtime.repo, "browser_platform_config", config)
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: begun)
    status, result = runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0))
    assert (status, result) == (200, {"stop": True, "reason": expected})
    assert result["reason"] != "provider_outage" and not calls


def test_cost_cap_over_the_job_limit_is_a_job_cap(env, monkeypatch):
    _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    env.job["cost_usd"] = "1.99"
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"code": "cost_cap"})
    assert runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0))[1]["reason"] == "cap_reached"


def test_forbidden_turn_is_unavailable_not_a_stop(env, monkeypatch):
    calls = _model(monkeypatch, ("navigate", {"url": env.job["input"]["url"]}))
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"ok": False, "code": "forbidden"})
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0))
    assert not calls
