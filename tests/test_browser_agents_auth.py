"""Browser agent worker auth (core/agent_auth.py, routers/agent_worker.py).

Invariants pinned here:

  tokens      a worker token is bdrw_ + 43 b64url chars; BDR stores only its
              sha256 (never the token) and compares in constant time.
  enroll      a code is single use, expires, and counts 5 attempts per IP
              per hour in the DB; the worker starts pending; IT Admin is
              belled and an audit row is written.
  pending     a pending worker may /hello and /config ({pending:true}) and
              nothing else (401 pending_approval).
  revoked     a revoked worker (hash nulled by the revoke RPC) is told
              `revoked` through its clear prefix; an unknown token is 401
              unauthorized.
  tokens x2   a JWT never passes the worker routes and a worker token never
              passes get_current_user.
  ip          the optional allowlist reads the rightmost X-Forwarded-For
              entry only when TRUST_FORWARDED_FOR is set.
  headers     every response (errors too) is Cache-Control: no-store and
              every refusal carries X-Error-Code.
  gate        everything 404s (bare) while BROWSER_AGENTS_ENABLED is off.
  config      the boot validator refuses unusable key, dependency and
              production settings.
"""

from __future__ import annotations

import base64
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import agent_auth
from app.core.config import Settings
from app.core.deps import get_current_user
from app.core.features import enabled_map
from app.core.roles import (
    BROWSER_AGENT_ADMIN_ROLES,
    BROWSER_AGENT_HELPER_ROLES,
    BROWSER_AGENT_VIEW_ROLES,
    Role,
)
from app.services.browser_agents import protocol

from tests.browser_agents_fake import iso
from tests.browser_agents_harness import API, KEY_A, KEY_B, Harness, agent_settings, error_code


def _enrollment(h: Harness, *, minutes: int = 30, used: bool = False, name: str = "Office PC") -> str:
    code, digest = agent_auth.new_enroll_code()
    h.db.table("browser_agent_enrollments").insert({
        "code_hash": agent_auth.bytea_literal(digest), "worker_name": name, "created_by": "admin-1",
        "expires_at": iso(h.db.now + timedelta(minutes=minutes)),
        "used_at": iso(h.db.now) if used else None,
    }).execute()
    return code


def _enroll(h: Harness, code: str, **headers):
    return h.client.post(f"{API}/enroll", json={"code": code, "hostname": "G3-AGENT-01",
                                                "os_info": {"os": "Windows 11"}, "version": "1.0.0"},
                         headers=headers)


# ── token and code helpers ──────────────────────────────────────────────────


def test_worker_token_shape_and_hash():
    token, digest, prefix = agent_auth.new_worker_token()
    assert protocol.WORKER_TOKEN_RE.match(token)
    assert digest == agent_auth.sha256_bytes(token) and len(digest) == 32
    assert prefix == token[: agent_auth.TOKEN_PREFIX_LENGTH] and prefix.startswith("bdrw_")
    assert agent_auth.bytea_value(agent_auth.bytea_literal(digest)) == digest


def test_enroll_code_shape_and_normalization():
    code, digest = agent_auth.new_enroll_code()
    assert protocol.ENROLL_CODE_RE.match(code)
    assert digest == agent_auth.sha256_bytes(code)
    pasted = "-".join(code[i:i + 4] for i in range(0, 20, 4)).lower()
    assert agent_auth.normalize_enroll_code(f"  {pasted} ") == code


# ── enroll ───────────────────────────────────────────────────────────────────


def test_enroll_creates_a_pending_worker_with_only_a_token_hash(monkeypatch):
    h = Harness(monkeypatch)
    code = _enrollment(h)
    resp = _enroll(h, code)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending" and protocol.WORKER_TOKEN_RE.match(body["token"])
    assert resp.headers["cache-control"] == "no-store"
    worker = h.db.one("browser_agent_workers", id=body["worker_id"])
    assert worker["status"] == "pending" and worker["name"] == "Office PC"
    assert worker["token_hash"] == agent_auth.bytea_literal(agent_auth.sha256_bytes(body["token"]))
    assert body["token"] not in str(h.db.tables)          # the clear token is never stored
    enrollment = h.db.tables["browser_agent_enrollments"][0]
    assert enrollment["used_at"] and enrollment["worker_id"] == worker["id"]
    assert [n["type"] for n in h.notices] == ["browser_agent.worker_pending"]
    assert h.notices[0]["metadata"]["hostname"] == "G3-AGENT-01"
    assert [a["action"] for a in h.audits] == ["browser_agent.worker_enrolled"]


def test_enroll_code_is_single_use_and_expires(monkeypatch):
    h = Harness(monkeypatch)
    code = _enrollment(h)
    assert _enroll(h, code).status_code == 200
    again = _enroll(h, code)
    assert again.status_code == 400 and error_code(again) == "enroll_invalid"
    expired = _enrollment(h, minutes=-1)
    resp = _enroll(h, expired)
    assert resp.status_code == 400 and error_code(resp) == "enroll_invalid"
    unknown = _enroll(h, "A" * 20)
    assert unknown.status_code == 400 and error_code(unknown) == "enroll_invalid"


def test_enroll_accepts_a_pasted_code_with_dashes(monkeypatch):
    h = Harness(monkeypatch)
    code = _enrollment(h)
    pasted = "-".join(code[i:i + 5] for i in range(0, 20, 5)).lower()
    assert _enroll(h, pasted).status_code == 200


def test_enroll_suffixes_a_taken_worker_name(monkeypatch):
    h = Harness(monkeypatch)
    h.worker(name="Office PC", status="revoked")
    body = _enroll(h, _enrollment(h)).json()
    assert h.db.one("browser_agent_workers", id=body["worker_id"])["name"] == "Office PC (2)"


def test_enroll_counts_five_attempts_per_ip_per_hour_in_the_db(monkeypatch):
    h = Harness(monkeypatch)
    for _ in range(5):
        assert _enroll(h, "B" * 20).status_code == 400
    blocked = _enroll(h, _enrollment(h))                  # even a good code
    assert blocked.status_code == 429 and error_code(blocked) == "rate_limited"
    assert int(blocked.headers["retry-after"]) > 0
    assert h.db.tables["browser_agent_enroll_attempts"][0]["count"] == 5
    h.db.advance(3600)                                     # next clock hour
    assert _enroll(h, _enrollment(h)).status_code == 200


def test_enroll_rejects_a_malformed_body_without_echoing_it(monkeypatch):
    h = Harness(monkeypatch)
    resp = h.client.post(f"{API}/enroll", json={"code": "short", "hostname": "x", "version": "1.0.0",
                                                "password": "hunter2"})
    assert resp.status_code == 422 and error_code(resp) == "invalid_request"
    assert "hunter2" not in resp.text


# ── pending / revoked / unknown ──────────────────────────────────────────────


def test_pending_worker_may_only_hello_and_config(monkeypatch):
    h = Harness(monkeypatch)
    _, token = h.worker(status="pending")
    hello, boot = h.hello(token)
    assert hello.status_code == 200 and hello.json()["status"] == "pending"
    config = h.client.get(f"{API}/config", headers=h.auth(token))
    assert config.status_code == 200 and config.json() == {"pending": True}
    claim = h.claim(token, boot)
    assert claim.status_code == 401 and error_code(claim) == "pending_approval"
    beat = h.client.post(f"{API}/heartbeat", json={"boot_id": boot, "config_version": 1}, headers=h.auth(token))
    assert beat.status_code == 401 and error_code(beat) == "pending_approval"


def test_revoked_worker_is_told_revoked_even_with_its_hash_nulled(monkeypatch):
    h = Harness(monkeypatch)
    worker, token = h.worker()
    worker.update(status="revoked", token_hash=None)        # what the revoke RPC does
    resp = h.client.get(f"{API}/config", headers=h.auth(token))
    assert resp.status_code == 401 and error_code(resp) == "revoked"
    assert resp.headers["cache-control"] == "no-store"


def test_unknown_or_missing_token_is_unauthorized(monkeypatch):
    h = Harness(monkeypatch)
    h.worker()
    other, _, _ = agent_auth.new_worker_token()
    for headers in ({}, h.auth(other), {"Authorization": "Basic abc"}):
        resp = h.client.get(f"{API}/config", headers=headers)
        assert resp.status_code == 401 and error_code(resp) == "unauthorized"


def test_a_user_jwt_never_passes_the_worker_routes(monkeypatch):
    h = Harness(monkeypatch)
    jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.c2ln"
    resp = h.client.get(f"{API}/config", headers=h.auth(jwt_like))
    assert resp.status_code == 401 and error_code(resp) == "unauthorized"
    assert not [c for c in h.db.rpc_calls]                 # refused before any lookup


def test_a_worker_token_never_passes_user_auth():
    token, _, _ = agent_auth.new_worker_token()
    request = SimpleNamespace(headers={}, url=SimpleNamespace(path="/features"), method="GET")
    with pytest.raises(HTTPException) as exc:
        get_current_user(request, authorization=f"Bearer {token}")
    assert exc.value.status_code == 401


# ── IP allowlist ─────────────────────────────────────────────────────────────


def test_client_ip_trusts_only_the_rightmost_forwarded_entry(monkeypatch):
    h = Harness(monkeypatch, trust_forwarded_for=True)
    req = SimpleNamespace(headers={"x-forwarded-for": "6.6.6.6, 10.1.2.3"},
                          client=SimpleNamespace(host="172.16.0.1"))
    assert agent_auth.client_ip(req) == "10.1.2.3"
    h.settings = agent_settings(trust_forwarded_for=False)
    assert agent_auth.client_ip(req) == "172.16.0.1"


def test_ip_allowed_rules():
    assert agent_auth.ip_allowed("1.2.3.4", [])
    assert agent_auth.ip_allowed("10.1.2.3", ["10.0.0.0/8"])
    assert not agent_auth.ip_allowed("11.1.2.3", ["10.0.0.0/8"])
    assert not agent_auth.ip_allowed(None, ["10.0.0.0/8"])
    assert agent_auth.ip_allowed("2001:db8::1", ["2001:db8::/32"])


def test_allowlist_refuses_a_spoofed_left_entry(monkeypatch):
    h = Harness(monkeypatch, trust_forwarded_for=True)
    _, token = h.worker(allowed_ips=["10.0.0.0/8"])
    ok = h.client.get(f"{API}/config", headers=h.auth(token, **{"X-Forwarded-For": "6.6.6.6, 10.9.9.9"}))
    assert ok.status_code == 200
    spoofed = h.client.get(f"{API}/config", headers=h.auth(token, **{"X-Forwarded-For": "10.9.9.9, 6.6.6.6"}))
    assert spoofed.status_code == 403 and error_code(spoofed) == "ip_not_allowed"


# ── rate limit, headers, gate ────────────────────────────────────────────────


def test_per_worker_rate_limit(monkeypatch):
    h = Harness(monkeypatch, browser_agent_rate_limit_per_minute=60)
    _, token = h.worker()
    codes = [h.client.get(f"{API}/config", headers=h.auth(token)).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60
    last = h.client.get(f"{API}/config", headers=h.auth(token))
    assert last.status_code == 429 and error_code(last) == "rate_limited" and last.headers.get("retry-after")


def test_everything_404s_while_agents_are_off(monkeypatch):
    h = Harness(monkeypatch)
    _, token = h.worker()
    h.settings = agent_settings(browser_agents_enabled=False)
    for method, path in (("get", "/config"), ("post", "/enroll"), ("get", "/canary/page"),
                         ("get", "/python-runtime.ps1"),
                         ("post", f"/jobs/{uuid.uuid4()}/finish")):
        resp = getattr(h.client, method)(f"{API}{path}", headers=h.auth(token))
        assert resp.status_code == 404 and resp.json() == {"detail": "Not Found"}


def test_live_view_seam_is_not_served(monkeypatch):
    h = Harness(monkeypatch)
    _, token = h.worker()
    assert h.client.get(f"{API}/jobs/{uuid.uuid4()}/live", headers=h.auth(token)).status_code == 404


def test_job_routes_need_a_matching_lease_header(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    url = f"{API}/jobs/{job['id']}/park"
    missing = h.client.post(url, headers=h.auth(token, **{"X-Agent-Boot": boot}))
    assert missing.status_code == 422 and error_code(missing) == "invalid_request"
    other = h.client.post(url, headers=h.lease_headers(token, str(uuid.uuid4()), 1, boot))
    assert other.status_code == 422 and error_code(other) == "invalid_request"
    no_boot = h.client.post(url, headers=h.auth(token, **{"X-Agent-Lease": f"{job['id']}:1"}))
    assert no_boot.status_code == 422 and error_code(no_boot) == "invalid_request"


# ── roles and flags ──────────────────────────────────────────────────────────


def test_role_sets_match_the_human_api():
    assert BROWSER_AGENT_VIEW_ROLES == {Role.ESTIMATING_ADMIN, Role.IT_ADMIN, Role.EXECUTIVE}
    assert BROWSER_AGENT_HELPER_ROLES == {Role.ESTIMATING_ADMIN, Role.IT_ADMIN}
    assert BROWSER_AGENT_ADMIN_ROLES == {Role.IT_ADMIN}
    for role in (Role.ESTIMATOR, Role.ACCOUNTANT):
        assert role not in BROWSER_AGENT_VIEW_ROLES


def test_features_flag_follows_the_switch(monkeypatch):
    h = Harness(monkeypatch)
    assert enabled_map(Role.IT_ADMIN)["browser_agents"] is True
    h.settings = agent_settings(browser_agents_enabled=False)
    assert enabled_map(Role.IT_ADMIN)["browser_agents"] is False


# ── boot validator ───────────────────────────────────────────────────────────


def test_validator_accepts_a_complete_config():
    s = agent_settings()
    assert s.browser_agents_active and s.browser_agent_old_keys == {}


@pytest.mark.parametrize("over, needle", [
    ({"browser_agent_secrets_key": ""}, "BROWSER_AGENT_SECRETS_KEY must be set"),
    ({"browser_agent_secrets_key": "not base64!"}, "BROWSER_AGENT_SECRETS_KEY is not valid base64"),
    ({"browser_agent_secrets_key": base64.b64encode(b"x" * 16).decode()}, "exactly 32 bytes"),
    ({"browser_agent_fingerprint_key": KEY_A}, "must differ"),
    ({"browser_agent_fingerprint_key": ""}, "BROWSER_AGENT_FINGERPRINT_KEY must be set"),
    ({"browser_agent_secrets_old_keys": "k1:" + KEY_B}, "must not reuse"),
    ({"browser_agent_secrets_old_keys": "nokey"}, "<key_id>:<base64>"),
    ({"browser_agent_lease_seconds": 10}, "BROWSER_AGENT_LEASE_SECONDS"),
    ({"browser_agent_release_pubkey": "abc"}, "BROWSER_AGENT_RELEASE_PUBKEY"),
])
def test_validator_refuses(over, needle):
    with pytest.raises(ValueError) as exc:
        agent_settings(**over)
    assert needle in str(exc.value)


def test_validator_needs_forwarded_for_in_production(monkeypatch):
    prod = dict(environment="production", supabase_service_role_key="srk")
    with pytest.raises(ValueError) as exc:
        agent_settings(**prod)
    assert "TRUST_FORWARDED_FOR" in str(exc.value)


def test_validator_ignores_keys_while_off():
    s = Settings(browser_agents_enabled=False, browser_agent_secrets_key="", environment="test")
    assert not s.browser_agents_active


def test_old_keys_parse():
    s = agent_settings(browser_agent_secrets_old_keys=f"k0:{KEY_B}")
    assert s.browser_agent_old_keys == {"k0": b"B" * 32}


def test_browser_platform_boot_is_independent_of_org_rfi_configuration():
    settings = agent_settings(procore_login_email="", pm_rfi_sync_enabled=False)
    assert settings.browser_agents_enabled
    assert not settings.pm_rfi_sync_active
