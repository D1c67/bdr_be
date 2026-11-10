"""Browser v2 worker-facing runtime (docs/ASSISTANT_BROWSER_V2_PLAN.md 3.3, 7.4, 8.1,
10.2, 10.5, 10.7, 12): credentials by id from the job's set, cookies by the
credential's cookie_domains, the job SecretSet, URL redaction in traces,
caps from settings, the job policy, finish facts and the v2 help channel.
No database: repo calls are monkeypatched per test."""
import base64
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.agent_auth import AgentLease, AgentWorker
from app.services import assistant_browser_help as help_flow
from app.services import assistant_browser_records as records
from app.services import assistant_browser_runtime as runtime
from app.services import assistant_browser_vault as vault
from app.services.assistant_browser_redaction import code_digest, redact_url
from app.services.browser_agents import llm, protocol
from app.services.browser_agents.llm.base import LlmResult, LlmUsage, ToolCall

KEK = base64.b64encode(b"v" * 32).decode()


def _cookie(name, value, domain):
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True}


@pytest.fixture
def env(monkeypatch):
    owner, jid, cid, cid2, adm, conv, boot, wid = (str(uuid4()) for _ in range(8))
    worker = AgentWorker(wid, "personal", "active", boot, False, 3, ("user_browser",), (), "1.1.0",
                         principal_kind="user")
    lease = AgentLease(worker, jid, 4, boot)
    config = SimpleNamespace(assistant_browser_enabled=True, assistant_browser_v2_enabled=True,
                             user_cred_kek=KEK, user_cred_kek_id="u1", user_cred_old_keys="",
                             assistant_daily_cap_usd=15.0, assistant_global_daily_cap_usd=50.0)
    monkeypatch.setattr(runtime, "get_settings", lambda: config)
    monkeypatch.setattr(vault, "get_settings", lambda: config)
    job = {"id": jid, "agent_type": "user_browser", "principal_user_id": owner, "principal_kind": "user",
           "status": "running", "user_credential_id": None, "assistant_admission_id": adm,
           "assistant_conversation_id": conv, "cost_usd": "0", "turn_count": 0, "tokens_in": 0, "active_seconds": 0,
           "policy_version": 2,
           "input": {"admission_id": adm, "goal": "Find the RFI due date", "start_url": "https://app.procore.com/1/home",
                     "start_site_key": "procore",
                     "authority": {"role": "Engineer", "model": "test-model", "conversation_id": conv,
                                   "max_cost_usd": 5, "admission_id": adm}}}

    def cred(cid_, site, label, password, origins, cookie_domains):
        return {"id": cid_, "user_id": owner, "version": 3, "status": "active", "username": f"{label}@example.test",
                "site_key": site, "account_label": label, "login_origins": origins, "cookie_domains": cookie_domains,
                **vault.encrypt_payload(owner, cid_, 3, "credential", password.encode())}

    creds = {cid: cred(cid, "procore", "default", "PROCORE_SECRET_PW", ["https://login.procore.com"], [".procore.com"]),
             cid2: cred(cid2, "autodesk", "work", "AUTODESK_SECRET_PW", ["https://accounts.autodesk.com"],
                        ["accounts.autodesk.com", ".autodesk.com"])}
    pins = [{"job_id": jid, "user_id": owner, "credential_id": c["id"], "site_key": c["site_key"],
             "account_label": c["account_label"], "credential_version": c["version"]} for c in creds.values()]
    sessions: list[dict] = []
    monkeypatch.setattr(runtime, "rpc", lambda *a: job)
    monkeypatch.setattr(vault, "job_credential", lambda j, credential_id=None: (
        creds[str(credential_id)] if credential_id in creds else
        (None if credential_id is None else (_ for _ in ()).throw(vault.VaultUnavailable("x")))))
    monkeypatch.setattr(runtime.repo, "browser_job_credentials", lambda owner_, job_id=None, admission_id=None: pins)
    monkeypatch.setattr(runtime.repo, "browser_credential_by_id", lambda owner_, c: creds.get(c))
    monkeypatch.setattr(runtime.repo, "browser_session_rows_v2",
                        lambda owner_, site_key, account_label, origin=None, limit=20: [
                            s for s in sessions if s["site_key"] == site_key and s["account_label"] == account_label
                            and (origin is None or s["origin"] == origin)])
    monkeypatch.setattr(runtime.repo, "browser_secret_hashes", lambda *a: {})
    monkeypatch.setattr(runtime.repo, "browser_turn_history", lambda *a, **kw: [])
    return SimpleNamespace(owner=owner, lease=lease, job=job, creds=creds, cid=cid, cid2=cid2, pins=pins,
                           sessions=sessions, config=config, adm=adm)


def _session_row(env, cid, origin, cookies):
    cred = env.creds[cid]
    sid = str(uuid4())
    plain = {"origin": origin, "credential_id": cid, "credential_version": 3, "site_key": cred["site_key"],
             "account_label": cred["account_label"], "cookies": cookies}
    row = {"id": sid, "version": 1, "origin": origin, "credential_id": cid, "site_key": cred["site_key"],
           "account_label": cred["account_label"], "updated_at": datetime.now(timezone.utc).isoformat(),
           **vault.encrypt_payload(env.owner, sid, 1, "session", json.dumps(plain).encode())}
    env.sessions.append(row)
    return row


# ── SecretSet and redaction (10.7) ───────────────────────────────────────────


def test_secret_set_covers_every_credential_and_cookie_of_the_job(env):
    _session_row(env, env.cid, "https://app.procore.com", [_cookie("sid", "COOKIEVALUE_ABCDEFGH", ".procore.com"),
                                                           _cookie("lang", "en", ".procore.com")])
    text = "PROCORE_SECRET_PW then AUTODESK_SECRET_PW and COOKIEVALUE_ABCDEFGH lang en"
    clean = runtime.sanitize(env.job, {"text": text})
    assert "PROCORE_SECRET_PW" not in str(clean) and "AUTODESK_SECRET_PW" not in str(clean)
    assert "COOKIEVALUE_ABCDEFGH" not in str(clean)
    # Short cookie flags stay so page text is not erased.
    assert "lang en" in clean["text"]


def test_stored_urls_keep_origin_and_path_only_model_view_keeps_query(env):
    url = "https://app.procore.com/1/rfis/aGVsbG93b3JsZGhlbGxvd29ybGQx?page=2&q=1#frag"
    stored = runtime.sanitize(env.job, {"url": url, "note": f"opened {url} ok"})
    assert stored["url"] == "https://app.procore.com/1/rfis/:token"
    assert "page=2" not in stored["note"] and "#frag" not in stored["note"] and stored["note"].endswith(" ok")
    model = runtime.sanitize(env.job, {"url": url}, trace=False)
    assert model["url"] == url


def test_redact_url_drops_userinfo_and_default_ports():
    assert redact_url("https://user:pw@example.com:443/a/b?x=1") == "https://example.com/a/b"
    assert redact_url("http://example.com:8080/x#y") == "http://example.com:8080/x"
    assert redact_url("not a url") == "not a url"


def test_alphanumeric_consumed_code_is_scrubbed_in_any_case(env, monkeypatch):
    digest = code_digest(b"v" * 32, "ab12cd")
    monkeypatch.setattr(runtime.repo, "browser_secret_hashes", lambda *a: {"u1": [digest]})
    clean = runtime.sanitize(env.job, {"text": "the code AB12CD and ab12cd and xab12cd"})
    assert "AB12CD" not in clean["text"] and " ab12cd " not in clean["text"]
    assert "xab12cd" in clean["text"]


# ── Cookies by cookie_domains (10.5) ─────────────────────────────────────────


def test_cookies_limited_to_the_credentials_domains():
    kept = runtime.checked_cookies_v2([
        _cookie("a", "1", ".procore.com"), _cookie("b", "2", "app.procore.com"),
        _cookie("c", "3", "tracker.example.net"), _cookie("d", "4", ".procore.com.evil.test"),
    ], [".procore.com"])
    assert [c["name"] for c in kept] == ["a", "b"]
    host_only = runtime.checked_cookies_v2([_cookie("a", "1", "login.site.test"), _cookie("b", "2", "other.site.test")],
                                           ["login.site.test"])
    assert [c["name"] for c in host_only] == ["a"]
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.checked_cookies_v2([_cookie("c", "3", "tracker.example.net")], [".procore.com"])
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.checked_cookies_v2([_cookie("a", "1", ".procore.com")], [])


# ── fill_credential login (7.4) ──────────────────────────────────────────────


def _fill(cid, origin="https://login.procore.com"):
    return protocol.UserCredentialFillRequest(credential_id=cid, origin=origin, frame_origin=origin)


def test_login_begin_refuses_credential_outside_the_job_set(env, monkeypatch):
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: pytest.fail("no RPC"))
    out = runtime.login_begin_v2(env.lease, _fill(str(uuid4())))
    assert out == {"granted": False, "reason": "not_in_job", "attempt_id": None, "username": None,
                   "password": None, "retry_at": None, "help_id": None}


def test_login_begin_refuses_an_origin_outside_login_origins(env, monkeypatch):
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: pytest.fail("no RPC"))
    out = runtime.login_begin_v2(env.lease, _fill(env.cid, "https://app.procore.com"))
    assert out["granted"] is False and out["reason"] == "origin_not_allowed"
    # Another credential's login origin is not this credential's.
    out = runtime.login_begin_v2(env.lease, _fill(env.cid, "https://accounts.autodesk.com"))
    assert out["reason"] == "origin_not_allowed"


def test_login_begin_releases_the_named_credential_only(env, monkeypatch):
    calls = []
    def rpc(owner, name, params):
        calls.append((name, params))
        return {"ok": True, "granted": True, "attempt_id": str(uuid4()), "credential": env.creds[env.cid2]}
    monkeypatch.setattr(runtime.repo, "browser_rpc", rpc)
    out = runtime.login_begin_v2(env.lease, _fill(env.cid2, "https://accounts.autodesk.com"))
    assert out["granted"] is True and out["password"] == "AUTODESK_SECRET_PW"
    assert calls[0][0] == "assistant_browser_login_begin_v2" and calls[0][1]["p_credential_id"] == env.cid2
    # The RPC answering with another credential is refused.
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {
        "ok": True, "granted": True, "attempt_id": str(uuid4()), "credential": env.creds[env.cid]})
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.login_begin_v2(env.lease, _fill(env.cid2, "https://accounts.autodesk.com"))


def test_login_begin_maps_refusals(env, monkeypatch):
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {
        "ok": True, "granted": False, "code": "login_busy", "retry_at": "2026-11-02T10:00:00Z"})
    out = runtime.login_begin_v2(env.lease, _fill(env.cid))
    assert out["granted"] is False and out["reason"] == "login_busy" and out["password"] is None
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"ok": False, "code": "weird"})
    assert runtime.login_begin_v2(env.lease, _fill(env.cid))["reason"] == "not_configured"


def test_v1_identity_routes_refuse_a_v2_job(env):
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.identity_begin(env.lease, protocol.UserLoginBeginRequest(host="app.procore.com",
                                                                         origin="https://login.procore.com"))


def test_login_finish_ok_stores_only_site_cookies_encrypted_with_settings_cap(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(runtime.repo, "browser_platform_config_v2", lambda *a: ({}, {"user_password_session_max_hours": 6}))
    def rpc(owner, name, params):
        seen.update(name=name, params=params)
        return {"ok": True, "login_failures": 0, "attempts_day_count": 0}
    monkeypatch.setattr(runtime.repo, "browser_rpc", rpc)
    body = protocol.UserCredentialResultRequest(
        credential_id=env.cid, attempt_id=str(uuid4()), origin="https://app.procore.com", outcome="ok",
        cookies=[_cookie("sid", "SESSION_SECRET_1", ".procore.com"), _cookie("x", "THIRD", "ads.example.net")])
    out = runtime.login_finish_v2(env.lease, body)
    assert out["attempt_open"] is False and seen["name"] == "assistant_browser_login_finish_v2"
    session = seen["params"]["p_session"]
    assert "SESSION_SECRET_1" not in json.dumps(session)
    plain = json.loads(vault.decrypt_payload(env.owner, session["id"], 1, "session", session["envelope"]))
    assert [c["name"] for c in plain["cookies"]] == ["sid"] and plain["site_key"] == "procore"
    expires = datetime.fromisoformat(session["expires_at"])
    assert expires < datetime.now(timezone.utc) + timedelta(hours=6, minutes=1)


def test_login_finish_mfa_keeps_the_attempt_open(env, monkeypatch):
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"ok": True, "help_required": True})
    body = protocol.UserCredentialResultRequest(credential_id=env.cid, attempt_id=str(uuid4()),
                                                origin="https://login.procore.com", outcome="mfa")
    out = runtime.login_finish_v2(env.lease, body)
    assert out["attempt_open"] is True and out["help_reason"] == "mfa"


def test_session_poll_never_returns_another_accounts_jar(env, monkeypatch):
    row = _session_row(env, env.cid, "https://app.procore.com", [_cookie("sid", "JAR_VALUE_123456", ".procore.com")])
    poll = protocol.UserSessionPoll(credential_id=env.cid, newer_than=datetime.now(timezone.utc) - timedelta(hours=1))
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"ok": True, "session": row})
    out = runtime.session_poll(env.lease, poll)
    assert out["status"] == "ok" and out["cookies"][0]["value"] == "JAR_VALUE_123456"
    other = dict(row, account_label="someone_else")
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"ok": True, "session": other})
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.session_poll(env.lease, poll)
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"ok": True, "session": None})
    assert runtime.session_poll(env.lease, poll)["status"] == "no_jar"


# ── Job policy (10.2) ────────────────────────────────────────────────────────


def _policy(env, **over):
    data = {"ok": True, "policy_version": 3, "job_id": env.job["id"], "start_origin": "https://app.procore.com",
            "sites": [{"site_key": "procore", "label": "Procore", "kind": "curated", "scope": "domain",
                       "registrable_domains": ["procore.com"], "exact_origins": [], "grant_version": 1,
                       "profile": {"websocket_ok": True}},
                      {"site_key": "domain:example.org", "label": "example.org", "kind": "ad_hoc", "scope": "domain",
                       "registrable_domains": ["example.org"], "exact_origins": [], "grant_version": 2,
                       "profile": {"websocket_ok": True}}],
            "credentials": [{"credential_id": env.cid, "site_key": "procore", "account_label": "default",
                             "version": 3, "login_origins": ["https://login.procore.com"],
                             "cookie_domains": [".procore.com"]}],
            "safe_rules": [{"site_key": "procore", "method": "POST", "host": "app.procore.com",
                            "path_pattern": "^/filters$"}, {"site_key": "procore", "method": "nope"}],
            "caps": {"offsite_query_bytes": 4096, "hold_timeout_seconds": 600, "park_max_minutes": 15}}
    data.update(over)
    return data


def test_policy_drops_websocket_ok_on_ad_hoc_and_bad_safe_rules(env, monkeypatch):
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: _policy(env))
    policy = runtime.job_policy(env.lease)
    by_key = {s["site_key"]: s for s in policy["sites"]}
    assert by_key["procore"]["profile"]["websocket_ok"] is True
    assert by_key["domain:example.org"]["profile"]["websocket_ok"] is False
    assert len(policy["safe_rules"]) == 1 and policy["policy_version"] == 3


def test_policy_refused_when_malformed_or_for_another_job(env, monkeypatch):
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: _policy(env, job_id=str(uuid4())))
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.job_policy(env.lease)
    bad = _policy(env)
    bad["credentials"][0]["login_origins"] = ["https://login.microsoftonline.com"]
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: bad)
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.job_policy(env.lease)


# ── Caps from settings (12.1) ────────────────────────────────────────────────


def test_user_job_is_capped_by_the_user_task_cap_not_the_org_cap(env):
    kind = {"max_steps": 150, "max_total_tokens": 2_000_000, "max_active_seconds": 1800}
    settings = {"per_job_cost_cap_usd": 2, "user_per_job_cost_cap_usd": 5}
    env.job.update(cost_usd="2.01", turn_count=41)
    assert runtime.job_capped(env.job, kind, settings) is False
    env.job["cost_usd"] = "5.00"
    assert runtime.job_capped(env.job, kind, settings) is True
    env.job.update(cost_usd="0", turn_count=150)
    assert runtime.job_capped(env.job, kind, settings) is True


def _model(monkeypatch, name, args):
    calls = []
    def complete(a):
        calls.append(a)
        return LlmResult("", [ToolCall("c1", name, args)], LlmUsage(100, 20), None, None)
    monkeypatch.setattr(llm, "get_adapter", lambda provider: SimpleNamespace(complete=complete))
    from app.services import llm_gate
    monkeypatch.setattr(llm_gate, "slot", lambda *a: nullcontext())
    return calls


def _v2_turn_env(env, monkeypatch):
    kind = {"provider": "openai", "model": "test-model", "model_probe_ok_at": "today", "enabled": True,
            "price_in_per_mtok": "1", "price_out_per_mtok": "2", "max_steps": 150, "max_total_tokens": 2_000_000}
    monkeypatch.setattr(runtime.repo, "browser_platform_config_v2",
                        lambda *a: (kind, {"enabled": True, "user_per_job_cost_cap_usd": 5, "per_job_cost_cap_usd": 2}))
    monkeypatch.setattr(runtime.repo, "browser_admission", lambda *a: {"site_scope": [{"site_key": "procore"}]})
    monkeypatch.setattr(runtime.repo, "browser_sites_by_keys", lambda *a: {"procore": {"label": "Procore"}})
    monkeypatch.setattr(help_flow, "worker_state", lambda *a: {"status": "none", "messages": []})
    return kind


def test_v2_turn_uses_turn_begin_v2_with_the_user_cap_default_and_never_the_password(env, monkeypatch):
    kind = _v2_turn_env(env, monkeypatch)
    calls = _model(monkeypatch, "navigate", {"url": "https://app.procore.com/1/rfis?page=2"})
    seen = []
    def rpc(owner, name, params):
        seen.append((name, params))
        if name == "assistant_browser_turn_begin_v2":
            return {"claimed": True, "turn_id": str(uuid4()), "token": "t", "type": kind,
                    "job": {**env.job, "tokens_in": 10}}
        return {"ok": True}
    monkeypatch.setattr(runtime.repo, "browser_rpc", rpc)
    env.job["cost_usd"] = "2.01"
    status, out = runtime.turn(env.lease, protocol.UserTurnRequest(prev_turn_no=0, title="PROCORE_SECRET_PW page"))
    assert status == 200 and out["action"]["tool"] == "navigate"
    # The model sees the full URL it chose; the stored request has no password.
    assert out["action"]["args"]["url"].endswith("?page=2")
    begin = seen[0][1]
    assert seen[0][0] == "assistant_browser_turn_begin_v2" and Decimal(begin["p_user_cap_default"]) == 15
    assert "PROCORE_SECRET_PW" not in calls[0].messages[0].text
    assert 2.98 < out["caps"]["cost_left_usd"] <= 2.99
    assert seen[-1][0] == "assistant_browser_turn_finish"


@pytest.mark.parametrize("reason,stop", [("task_cap", "cap_reached"), ("user_cap", "daily_cap_reached"),
                                         ("org_daily_cap", "daily_cap_reached"), ("reservation_lapsed", "cap_reached")])
def test_v2_turn_refusals_name_the_cap(env, monkeypatch, reason, stop):
    _v2_turn_env(env, monkeypatch)
    calls = _model(monkeypatch, "navigate", {"url": "https://app.procore.com/"})
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda *a: {"ok": False, "code": "cost_cap", "reason": reason})
    status, out = runtime.turn(env.lease, protocol.UserTurnRequest(prev_turn_no=0))
    assert status == 200 and out == {"stop": True, "reason": stop, "detail": reason} and not calls


def test_v2_turn_refuses_a_navigation_to_a_private_address(env, monkeypatch):
    kind = _v2_turn_env(env, monkeypatch)
    _model(monkeypatch, "navigate", {"url": "http://192.168.1.1/admin"})
    monkeypatch.setattr(runtime.repo, "browser_rpc", lambda o, name, p: (
        {"claimed": True, "turn_id": str(uuid4()), "token": "t", "type": kind}
        if name.endswith("begin_v2") else {"ok": True}))
    status, out = runtime.turn(env.lease, protocol.UserTurnRequest(prev_turn_no=0))
    assert out["stop"] is True and out["reason"] == "provider_outage"


# ── Finish facts (4.5, 15.2) ─────────────────────────────────────────────────


def test_finish_keeps_only_facts_with_in_scope_sources(env, monkeypatch):
    tid = str(uuid4())
    action = {"turn_no": 1, "action": {"call_id": "c", "tool": "user_finish", "args": {
        "outcome": "completed", "result": "RFI 12 is due Friday",
        "facts": [{"text": "Due Friday", "source_url": "https://app.procore.com/1/rfis/12?x=1"},
                  {"text": "Leak", "source_url": "https://attacker.example/steal"}]}}}
    env.job["turn_count"] = 1
    monkeypatch.setattr(runtime.repo, "browser_turn_history", lambda *a, **kw: [
        {"id": tid, "turn_no": 1, "response_envelope": runtime._turn_envelope(env.job, tid, 1, "response", action)}])
    saved = {}
    def rpc(owner, name, params):
        if name == "assistant_browser_job_policy":
            return _policy(env)
        saved.update(params)
        return {"ok": True}
    monkeypatch.setattr(runtime.repo, "browser_rpc", rpc)
    runtime.finish(env.lease, "succeeded", "done", outcome="nothing_to_do")
    row = saved["p_result_record"]
    content = records.unpack_record(env.owner, env.job["id"], {**row, "user_id": env.owner, "job_id": env.job["id"]})
    assert content["result"] == "RFI 12 is due Friday" and content["facts_dropped"] == 1
    assert content["facts"] == [{"text": "Due Friday", "source_url": "https://app.procore.com/1/rfis/12"}]


# ── Vault (3.3) ──────────────────────────────────────────────────────────────


def test_vault_job_credential_v2_by_id_and_null_allowed(monkeypatch):
    from app.assistant import access, repo

    owner, jid, cid = str(uuid4()), str(uuid4()), str(uuid4())
    config = SimpleNamespace(assistant_browser_enabled=True, assistant_browser_v2_enabled=True)
    monkeypatch.setattr(vault, "get_settings", lambda: config)
    monkeypatch.setattr(access, "owner_check", lambda *a, **kw: SimpleNamespace(problem=None))
    row = {"id": cid, "user_id": owner, "status": "active", "version": 2, "site_key": "procore",
           "account_label": "default", "service": None}
    pins = [{"credential_id": cid, "user_id": owner, "credential_version": 2, "site_key": "procore",
             "account_label": "default"}]
    monkeypatch.setattr(repo, "browser_job_credentials", lambda *a, **kw: pins)
    monkeypatch.setattr(repo, "browser_credential_by_id", lambda o, c: row if c == cid else None)
    job = {"id": jid, "agent_type": "user_browser", "principal_kind": "user", "principal_user_id": owner}
    assert vault.job_credential(job) is None
    assert vault.job_credential(job, cid) is row
    with pytest.raises(vault.VaultUnavailable):
        vault.job_credential(job, str(uuid4()))
    row["version"] = 3
    with pytest.raises(vault.VaultUnavailable):
        vault.job_credential(job, cid)
    config.assistant_browser_v2_enabled = False
    with pytest.raises(vault.VaultUnavailable):
        vault.job_credential(job)


# ── Help channel v2 (8.1, 8.4) ───────────────────────────────────────────────


@pytest.fixture
def helpenv(monkeypatch):
    owner, jid, help_id, cid = (str(uuid4()) for _ in range(4))
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    job = {"id": jid, "agent_type": "user_browser", "principal_kind": "user", "principal_user_id": owner,
           "status": "waiting_help", "input": {"start_site_key": "procore"}, "cost_usd": "0"}
    state = {"id": help_id, "status": "open", "is_login_scope": False, "reason_code": "mfa_step_up",
             "credential_id": cid, "expires_at": expiry}
    monkeypatch.setattr(vault, "get_settings", lambda: SimpleNamespace(user_cred_kek=KEK, user_cred_kek_id="u1",
                                                                       user_cred_old_keys=""))
    monkeypatch.setattr(records.repo, "browser_job", lambda u, j: job if (u, j) == (owner, jid) else None)
    monkeypatch.setattr(records.repo, "browser_help_state", lambda *a: state)
    monkeypatch.setattr(records.repo, "browser_credential_by_id", lambda *a: {"site_key": "procore", "status": "active"})
    monkeypatch.setattr(records.repo, "browser_site", lambda *a: {"code_email_regex": None})
    stored, actions = [], []
    monkeypatch.setattr(records.repo, "browser_help_records", lambda *a: stored)
    monkeypatch.setattr(runtime, "sanitize", lambda job, payload, **kw: payload)
    def reply(user_id, job_id, help_id_, client_id, action, row):
        actions.append(action)
        stored.append({**row, "user_id": owner, "job_id": jid, "created_at": expiry})
        return {"ok": True}
    monkeypatch.setattr(records.repo, "browser_help_reply_v2", reply)
    monkeypatch.setattr(records.repo, "browser_help_reply", lambda *a: pytest.fail("v1 reply"))
    guarded = []
    from app.services import assistant_browser_secrets as secrets
    monkeypatch.setattr(secrets, "guard_help_text", lambda u, j, text: guarded.append(text))
    return SimpleNamespace(owner=owner, jid=jid, help_id=help_id, state=state, stored=stored, actions=actions,
                           guarded=guarded, job=job)


def _reply(h, **kw):
    return help_flow.owner_reply(h.owner, h.jid, expected_help_id=h.help_id, client_msg_id=str(uuid4()), **kw)


@pytest.mark.parametrize("value,code", [("AB12CD", "AB12CD"), ("123 456", "123456"), ("12-34-56", "123456"),
                                        ("ab12", "ab12")])
def test_v2_alphanumeric_codes_are_accepted_and_hashed_case_free(helpenv, value, code):
    out = _reply(helpenv, action="message", secret_value=value)
    assert helpenv.actions == ["code"] and out["messages"][0]["is_secret"] is True
    row = helpenv.stored[0]
    assert value not in json.dumps(row) and code not in json.dumps(row)
    assert row["secret_hash"] == code_digest(b"v" * 32, code.upper())
    plain = records.unpack_record(helpenv.owner, helpenv.jid, row, allow_secret=True)
    assert plain["secret"] == code


@pytest.mark.parametrize("value", ["abcdef", "12", "1" * 13, "12  34", " 1234", "12_34"])
def test_v2_non_code_shapes_are_refused(helpenv, value):
    with pytest.raises(records.BrowserContentUnavailable):
        _reply(helpenv, action="message", secret_value=value)
    assert helpenv.stored == []


def test_v2_letters_only_code_needs_the_site_regex(helpenv, monkeypatch):
    monkeypatch.setattr(records.repo, "browser_site", lambda *a: {"code_email_regex": "[A-Z]{6}"})
    _reply(helpenv, action="message", secret_value="QWERTY")
    assert helpenv.actions == ["code"]


def test_v2_continue_only_for_push_or_code_helps(helpenv):
    helpenv.state["reason_code"] = "push"
    _reply(helpenv, action="continue")
    assert helpenv.actions == ["continue"]
    helpenv.state["reason_code"] = "needs_info"
    with pytest.raises(records.BrowserContentUnavailable):
        _reply(helpenv, action="continue")


def test_v2_help_text_goes_through_the_secret_guard(helpenv, monkeypatch):
    _reply(helpenv, action="message", body="try the other project")
    assert helpenv.guarded == ["try the other project"]
    from app.services import assistant_browser_secrets as secrets
    def refuse(*a):
        raise secrets.SecretInMessage()
    monkeypatch.setattr(secrets, "guard_help_text", refuse)
    with pytest.raises(secrets.SecretInMessage):
        _reply(helpenv, action="message", body="my password is hunter2")
    assert len(helpenv.stored) == 1


def test_v2_state_offers_the_code_field_for_step_up(helpenv):
    out = help_flow.owner_help(helpenv.owner, helpenv.jid)
    assert out["secret_available"] is True and out["reason"] == "mfa_step_up" and out["continue_allowed"] is True
    helpenv.state["reason_code"] = "needs_info"
    assert help_flow.owner_help(helpenv.owner, helpenv.jid)["secret_available"] is False


def test_v2_consume_secret_allows_step_up_without_attempt(helpenv, monkeypatch):
    _reply(helpenv, action="message", secret_value="AB12CD")
    record = helpenv.stored[0]
    seen = {}
    def rpc(owner, name, params):
        seen.update(name=name, params=params)
        return {"ok": True, "record": record}
    monkeypatch.setattr(records.repo, "browser_rpc", rpc)
    code = help_flow.consume_secret(helpenv.job, help_id=helpenv.help_id, record_id=record["id"],
                                    login_attempt_id=None, worker_id=str(uuid4()), boot_id=str(uuid4()),
                                    lease_epoch=2)
    assert code == "AB12CD" and seen["name"] == "assistant_browser_help_consume_v2"
    assert seen["params"]["p_login_attempt_id"] is None


def test_v2_help_open_refuses_hold_and_checks_the_credential(env, monkeypatch):
    monkeypatch.setattr(runtime, "sanitize", lambda job, payload, **kw: payload)
    with pytest.raises(records.BrowserContentUnavailable):
        help_flow.open_help_v2(env.job, reason="hold", message=None, help_id=str(uuid4()), record_id=str(uuid4()),
                               worker_id=str(uuid4()), boot_id=str(uuid4()), lease_epoch=1)
    with pytest.raises(vault.VaultUnavailable):
        help_flow.open_help_v2(env.job, reason="mfa_step_up", message="code?", help_id=str(uuid4()),
                               record_id=str(uuid4()), worker_id=str(uuid4()), boot_id=str(uuid4()), lease_epoch=1,
                               credential_id=str(uuid4()))
    seen = {}
    def rpc(owner, name, params):
        seen.update(name=name, params=params)
        return {"ok": True, "created": True, "help": {"id": params["p_help_id"], "status": "open"}}
    monkeypatch.setattr(runtime.repo, "browser_rpc", rpc)
    hid = str(uuid4())
    out = help_flow.open_help_v2(env.job, reason="mfa_step_up", message="The site asks for a code", help_id=hid,
                                 record_id=str(uuid4()), worker_id=str(uuid4()), boot_id=str(uuid4()),
                                 lease_epoch=1, credential_id=env.cid)
    assert out == {"id": hid, "status": "open", "created": True}
    assert seen["name"] == "assistant_browser_help_open_v2" and seen["params"]["p_reason_code"] == "mfa_step_up"
    assert seen["params"]["p_credential_id"] == env.cid and seen["params"]["p_login_attempt_id"] is None


# ── Worker routes (protocol.USER_V2_ENDPOINTS) ───────────────────────────────


@pytest.fixture
def routes(monkeypatch):
    from app.core import agent_auth
    from tests.browser_agents_harness import API, Harness

    h = Harness(monkeypatch)
    worker, token, boot, job = h.active_with_job()
    worker["principal_kind"] = "user"
    job.update(principal_kind="user", principal_user_id=str(uuid4()), agent_type="user_browser")

    def fence(lease, *a, **kw):
        if lease.job_id != job["id"] or lease.epoch != job["lease_epoch"] or lease.boot_id != boot:
            raise agent_auth.agent_error(protocol.ErrorCode.LEASE_LOST)
        return job
    monkeypatch.setattr(runtime, "fenced", fence)
    yield SimpleNamespace(h=h, job=job, headers=h.lease_headers(token, job["id"], job["lease_epoch"], boot),
                          path=f"{API}/jobs/{job['id']}", boot=boot, token=token)
    h.client.close()


def test_every_v2_endpoint_is_mounted():
    import app.main as app_main
    mounted = {(m, r.path) for r in app_main.app.routes if hasattr(r, "methods") for m in r.methods}
    for name, (method, path, *_rest) in protocol.USER_V2_ENDPOINTS.items():
        if name.startswith("live_"):
            continue  # wave E (WI-21)
        assert (method, "/agent-worker/v1" + path) in mounted, name


def test_login_and_session_routes_reach_the_v2_runtime(routes, monkeypatch):
    cid = str(uuid4())
    monkeypatch.setattr(runtime, "login_begin_v2", lambda lease, body: {"granted": False, "reason": "spacing"})
    out = routes.h.client.post(routes.path + "/user/login/begin", headers=routes.headers, json={
        "credential_id": cid, "origin": "https://login.procore.com", "frame_origin": "https://login.procore.com"})
    assert out.status_code == 200 and out.json()["reason"] == "spacing" and out.headers["cache-control"] == "no-store"
    bad = routes.h.client.post(routes.path + "/user/login/begin", headers=routes.headers, json={
        "credential_id": cid, "origin": "https://login.microsoftonline.com",
        "frame_origin": "https://login.microsoftonline.com"})
    assert bad.status_code in (400, 422) and "microsoftonline" not in bad.text
    seen = {}
    monkeypatch.setattr(runtime, "session_poll", lambda lease, body: seen.update(body=body) or {"status": "no_jar"})
    out = routes.h.client.get(routes.path + "/user/session", headers=routes.headers,
                              params={"credential_id": cid, "newer_than": "2026-11-02T08:00:00+00:00"})
    assert out.status_code == 200 and out.json() == {"status": "no_jar"} and str(seen["body"].credential_id) == cid
    monkeypatch.setattr(runtime, "login_finish_v2", lambda lease, body: {"attempt_open": True})
    out = routes.h.client.post(routes.path + "/user/login/result", headers=routes.headers, json={
        "credential_id": cid, "attempt_id": str(uuid4()), "origin": "https://login.procore.com", "outcome": "push"})
    assert out.status_code == 200 and out.json() == {"attempt_open": True}


def test_policy_route(routes, monkeypatch):
    monkeypatch.setattr(runtime, "job_policy", lambda lease: {"policy_version": 4})
    out = routes.h.client.get(routes.path + "/user/policy", headers=routes.headers)
    assert out.status_code == 200 and out.json() == {"policy_version": 4}


def test_site_hold_route(routes, monkeypatch):
    from app.services import assistant_browser_holds as holds
    seen = {}
    monkeypatch.setattr(holds, "open_site_hold", lambda job, w, b, e, body: seen.update(body=body) or {
        "hold_id": str(uuid4()), "kind": "site", "status": "pending"})
    out = routes.h.client.post(routes.path + "/user/site-hold", headers=routes.headers, json={
        "trigger": "link", "url": "https://docs.example.org/a?b=1", "target_origin": "https://docs.example.org"})
    assert out.status_code == 200 and out.json()["kind"] == "site" and seen["body"]["url"].endswith("?b=1")


def test_download_route_is_multipart_and_checks_the_meta(routes, monkeypatch):
    from app.services import assistant_browser_downloads as downloads
    seen = {}
    did = str(uuid4())
    def accept(job, w, b, e, *, stream, meta):
        seen.update(meta=meta, data=stream.read())
        return {"download_id": did, "status": "staged"}
    monkeypatch.setattr(downloads, "accept", accept)
    data = b"%PDF-1.7 test"
    form = {"sha256": hashlib_sha(data), "filename": "spec.pdf", "size_bytes": str(len(data)), "sniffed_kind": "pdf",
            "source_origin": "https://acc.autodesk.com", "scan": "clean"}
    out = routes.h.client.post(routes.path + "/user/downloads", headers=routes.headers, data=form,
                               files={"file": ("spec.pdf", data, "application/pdf")})
    assert out.status_code == 200, out.text
    assert out.json()["download_id"] == did and seen["data"] == data and seen["meta"]["sniffed_kind"] == "pdf"
    monkeypatch.setattr(downloads, "accept", lambda *a, **kw: pytest.fail("accepted"))
    bad = routes.h.client.post(routes.path + "/user/downloads", headers=routes.headers,
                               data={**form, "scan": "infected"}, files={"file": ("spec.pdf", data, "application/pdf")})
    assert bad.status_code in (400, 422)


def hashlib_sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def test_help_open_route_takes_the_v2_shape_for_v2_jobs(routes, monkeypatch):
    seen = {}
    def open_v2(job, **kw):
        seen.update(kw)
        return {"id": kw["help_id"], "status": "open", "created": True}
    monkeypatch.setattr(help_flow, "open_help_v2", open_v2)
    monkeypatch.setattr(help_flow, "open_help", lambda *a, **kw: pytest.fail("v1 help"))
    cid = str(uuid4())
    out = routes.h.client.post(routes.path + "/user/help", headers=routes.headers, json={
        "reason_code": "mfa_step_up", "message": "The site asks for a code", "credential_id": cid})
    assert out.status_code == 200 and out.json()["holder"] is True and out.json()["joined_existing"] is False
    assert seen["reason"] == "mfa_step_up" and seen["credential_id"] == cid and seen["login_attempt_id"] is None
    hold = routes.h.client.post(routes.path + "/user/help", headers=routes.headers, json={
        "reason_code": "hold", "hold_id": str(uuid4())})
    assert hold.status_code in (400, 422)


def test_secret_use_route_allows_step_up_without_an_attempt(routes, monkeypatch):
    hid, mid = str(uuid4()), str(uuid4())
    monkeypatch.setattr(help_flow, "worker_state", lambda job: {"id": hid, "status": "open", "messages": []})
    seen = {}
    monkeypatch.setattr(help_flow, "consume_secret", lambda job, **kw: seen.update(kw) or "AB12CD")
    out = routes.h.client.post(routes.path + f"/user/help/{hid}/secrets/{mid}/use", headers=routes.headers, json={})
    assert out.status_code == 200 and out.json() == {"value": "AB12CD"} and seen["login_attempt_id"] is None
    routes.job["agent_type"] = "user_browser_read"
    out = routes.h.client.post(routes.path + f"/user/help/{hid}/secrets/{mid}/use", headers=routes.headers, json={})
    assert out.status_code in (400, 422)


def test_artifact_route_refuses_screenshots_during_login(routes, monkeypatch):
    monkeypatch.setattr(runtime, "login_open", lambda job: True)
    out = routes.h.client.post(routes.path + "/user/artifacts", headers=routes.headers,
                               data={"kind": "screenshot", "sha256": "0" * 64, "filename": "a.png"},
                               files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")})
    assert out.status_code in (400, 422)
