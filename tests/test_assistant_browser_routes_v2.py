"""Browser v2 human routes (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.3, 5.2, 5.4,
5.5, 6.3, 6.5, 7.2, 7.3, 7.6, 16.2; WI-4). Fakes only: the repo layer is
monkeypatched per test and the real Supabase client is never built
(conftest guard)."""
from __future__ import annotations

import base64
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import firewall
from app.core.deps import get_current_user, require_writer
from app.routers import assistant_browser as routes
from app.services import assistant_browser as browser
from app.services import assistant_browser_credentials as credentials
from app.services import assistant_browser_sites as sites
from app.services import assistant_browser_vault as vault
from tests.assistant_fakes import USER, credential_v2_row, curated_site_rows, grant_row, site_row

JOB = "13131313-1313-4131-8131-131313131313"
ADMISSION = "14141414-1414-4141-8141-141414141414"
HOLD = "16161616-1616-4161-8161-161616161616"
REQUEST = "17171717-1717-4171-8171-171717171717"
SENTINEL = "SENTINEL_SECRET_9x"
FUTURE = "2999-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"

HUMAN_ROUTES = [
    ("GET", "/assistant/browser-sites"),
    ("GET", "/assistant/browser-sites/signins"),
    ("GET", "/assistant/browser-sites/grants"),
    ("POST", "/assistant/browser-sites/grants"),
    ("DELETE", "/assistant/browser-sites/grants/{grant_id}"),
    ("GET", "/assistant/browser-sites/sessions"),
    ("DELETE", "/assistant/browser-sites/sessions/{session_id}"),
    ("GET", "/assistant/browser-sites/safe-rules"),
    ("DELETE", "/assistant/browser-sites/safe-rules/{rule_id}"),
    ("GET", "/assistant/browser-sites/requests/{request_id}"),
    ("POST", "/assistant/browser-sites/requests/{request_id}/decision"),
    ("GET", "/assistant/browser-credentials"),
    ("PUT", "/assistant/browser-credentials/{site_key}"),
    ("DELETE", "/assistant/browser-credentials/{site_key}"),
    ("GET", "/assistant/browser-holds/{hold_id}"),
    ("POST", "/assistant/browser-holds/{hold_id}/decision"),
    ("POST", "/assistant/browser-holds/{hold_id}/safe-rule"),
    ("POST", "/assistant/browser-holds/start/{admission_id}/decision"),
]


class World:
    """The repo state the routes read, as plain dicts."""

    def __init__(self) -> None:
        self.sites = {r["site_key"]: r for r in curated_site_rows()}
        self.grants: list[dict] = []
        self.credentials: list[dict] = []
        self.requests: dict[str, dict] = {}
        self.holds: dict[str, dict] = {}
        self.admissions: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.grant_put_result: dict | None = None
        self.continued: list[str] = []
        self.hold_decisions: list[tuple] = []
        self.hold_decide_error: Exception | None = None
        self.summary: dict = {}


@pytest.fixture
def world(monkeypatch):
    w = World()
    repo = routes.repo
    config = SimpleNamespace(assistant_browser_enabled=True, assistant_browser_v2_enabled=True,
                             procore_login_email="rfi-bot@g3electrical.example",
                             user_cred_kek=base64.b64encode(b"a" * 32).decode(), user_cred_kek_id="u1",
                             user_cred_old_keys="")
    w.config = config
    for module in (routes, credentials, vault):
        monkeypatch.setattr(module, "get_settings", lambda: config)

    monkeypatch.setattr(repo, "browser_site", lambda uid, key: w.sites.get(key))
    monkeypatch.setattr(repo, "browser_sites", lambda uid, **k: list(w.sites.values()))
    monkeypatch.setattr(repo, "browser_sites_by_keys", lambda uid, keys: {k: w.sites[k] for k in keys if k in w.sites})
    monkeypatch.setattr(repo, "browser_sites_for_url", lambda uid, **k: list(w.sites.values()))
    monkeypatch.setattr(repo, "browser_site_grants",
                        lambda uid, status="active", limit=200: [g for g in w.grants if status is None
                                                                 or g["status"] == status])
    monkeypatch.setattr(repo, "browser_site_grant",
                        lambda uid, key: next((g for g in w.grants if g["site_key"] == key
                                               and g["status"] == "active"), None))

    def grant_put(uid, site_key, **kw):
        w.calls.append(("grant_put", site_key, kw))
        if w.grant_put_result is not None:
            return w.grant_put_result
        if site_key not in w.sites and kw.get("ad_hoc"):
            w.sites[site_key] = site_row(site_key=site_key, label=kw["ad_hoc"]["label"], kind="ad_hoc",
                                         registrable_domains=[kw["value"]] if kw["scope"] == "domain" else [],
                                         exact_origins=[kw["value"]] if kw["scope"] == "origin" else [],
                                         login_origins=[], cookie_domains=[])
        grant = grant_row(site_key=site_key, scope=kw["scope"], value=kw["value"], path_prefix=kw["path_prefix"],
                          granted_via=kw["granted_via"], provenance=kw["provenance"])
        w.grants.append(grant)
        return {"ok": True, "grant": grant, "waiting_admissions": [ADMISSION]}

    monkeypatch.setattr(repo, "browser_site_grant_put", grant_put)

    def revoke(uid, grant_id, expected):
        w.calls.append(("revoke", grant_id, expected))
        return {"ok": True, "version": expected + 1, "canceled_jobs": 1, "sessions_removed": 2}

    monkeypatch.setattr(repo, "browser_site_revoke", revoke)

    def scope_extend(uid, job_id, key):
        w.calls.append(("scope_extend", job_id, key))
        return {"ok": True, "policy_version": 3}

    monkeypatch.setattr(repo, "browser_scope_extend", scope_extend)
    monkeypatch.setattr(repo, "browser_credentials", lambda uid: list(w.credentials))
    monkeypatch.setattr(repo, "browser_credentials_v2_all", lambda uid: list(w.credentials))
    monkeypatch.setattr(repo, "browser_credential_v2",
                        lambda uid, key, label=None, **k: next(
                            (c for c in w.credentials if c["site_key"] == key and c["status"] == "active"
                             and (label is None or c["account_label"] == label)), None))

    def credential_put(uid, site_key, label, expected, record, request_id=None):
        w.calls.append(("credential_put", site_key, label, expected, dict(record), request_id))
        row = credential_v2_row(id=record["id"], site_key=site_key, account_label=label, version=expected + 1,
                                username=record["username"], fingerprint=record["fingerprint"])
        return {"ok": True, "credential": row, "woken_jobs": [JOB], "waiting_admissions": [ADMISSION]}

    monkeypatch.setattr(repo, "browser_credential_put_v2", credential_put)

    def credential_revoke(uid, credential_id, expected):
        w.calls.append(("credential_revoke", credential_id, expected))
        return {"ok": True, "version": expected + 1}

    monkeypatch.setattr(repo, "browser_credential_revoke_v2", credential_revoke)
    monkeypatch.setattr(repo, "browser_sessions_v2", lambda uid, **k: [
        {"id": "s1", "site_key": "procore", "account_label": "default", "source": "login",
         "created_at": "2026-11-01T00:00:00+00:00", "expires_at": FUTURE, "cookies_ciphertext": "x"}])
    monkeypatch.setattr(repo, "browser_session_signout", lambda uid, sid: {"ok": sid == "11111111-0000-4000-8000-000000000001"})
    monkeypatch.setattr(repo, "browser_safe_rules", lambda uid, **k: [])
    monkeypatch.setattr(repo, "browser_safe_rule_remove", lambda uid, rid: {"ok": False})
    monkeypatch.setattr(repo, "browser_request", lambda uid, rid: w.requests.get(rid))
    monkeypatch.setattr(repo, "browser_admission", lambda uid, aid: w.admissions.get(aid))
    monkeypatch.setattr(repo, "browser_url_email_sources",
                        lambda uid, urls: [{"source": "rfp_harvest", "sender_domain": "gc-builders.net"}])

    def request_decide(uid, rid, decision):
        w.calls.append(("request_decide", rid, decision))
        row = w.requests.get(rid)
        if not row or row["status"] != "requested":
            return {"ok": False, "code": "request_changed"}
        if decision == "approve" and row["kind"] != "link_open":
            return {"ok": False, "code": "invalid"}
        row["status"] = {"approve": "approved", "decline": "declined", "cancel": "cancelled"}[decision]
        return {"ok": True, "request": row, "needs": []}

    monkeypatch.setattr(repo, "browser_request_decide", request_decide)

    def start_decide(uid, aid, decision):
        w.calls.append(("start_decide", aid, decision))
        if aid not in w.admissions:
            return {"ok": False, "code": "admission_changed"}
        status = "declined" if decision == "decline" else "waiting_credential"
        return {"ok": True, "admission": {**w.admissions[aid], "status": status},
                "needs": [] if decision == "decline" else ["credential"]}

    monkeypatch.setattr(repo, "browser_start_decide", start_decide)
    monkeypatch.setattr(repo, "browser_hold", lambda uid, hid: w.holds.get(hid))
    monkeypatch.setattr(repo, "browser_holds",
                        lambda uid, job_id, status=None: [h for h in w.holds.values()
                                                          if h["job_id"] == job_id and (status is None
                                                                                        or h["status"] == status)])

    def safe_rule_add(uid, hold_id, **kw):
        w.calls.append(("safe_rule_add", hold_id, kw))
        return {"ok": True, "rule": {"id": "r1", "site_key": "procore", **kw}}

    monkeypatch.setattr(repo, "browser_safe_rule_add", safe_rule_add)
    monkeypatch.setattr(repo, "browser_record", lambda uid, job, rid: {"id": rid} if w.summary else None)
    from app.services import assistant_browser_records as records

    monkeypatch.setattr(records, "unpack_record", lambda uid, job, row, **k: {"hold": dict(w.summary)})

    def cont(uid, aid):
        w.continued.append(aid)
        return {"status": "enqueued", "task_id": JOB}

    monkeypatch.setattr(browser, "continue_admission", cont)

    def decide(uid, hid, decision):
        w.hold_decisions.append((hid, decision))
        if w.hold_decide_error is not None:
            raise w.hold_decide_error
        return {"hold": {"id": hid, "status": "declined" if decision == "decline" else "approved"}}

    from app.services import assistant_browser_holds as holds

    monkeypatch.setattr(holds, "decide", decide)
    return w


@pytest.fixture
def client(world, monkeypatch):
    user = SimpleNamespace(id=USER, via_assistant_run_id=None, role="executive")
    monkeypatch.setattr(routes, "_require_assistant", lambda user: None)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_writer] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), user


def _concrete(path: str) -> str:
    return re.sub(r"\{[a-z_]+\}", lambda m: "procore" if m.group(0) == "{site_key}" else str(uuid4()), path)


def _code(result) -> str | None:
    return result.headers.get("x-error-code")


# ── Firewall and mounting ────────────────────────────────────────────────────


@pytest.mark.parametrize("pair", HUMAN_ROUTES)
def test_human_routes_are_deny_listed_for_the_assistant(pair):
    assert firewall.is_deny_listed(*pair)
    verdict = firewall.check(pair[0], pair[1], {pair})
    assert not verdict.allowed


@pytest.mark.parametrize("pair", [("POST", "/assistant/browser-results/{message_id}/carry"),
                                  ("GET", "/assistant/browser-live/{session_id}/frame"),
                                  ("POST", "/assistant/browser-live/{session_id}/input"),
                                  ("POST", "/assistant/browser-results/downloads/{download_id}/add-to-project")])
def test_new_prefixes_are_denied(pair):
    assert firewall.is_deny_listed(*pair)


@pytest.mark.parametrize("pair", [("POST", "/assistant/browser-requests/tasks"),
                                  ("GET", "/assistant/browser-requests/tasks/{job_id}"),
                                  ("POST", "/assistant/browser-requests/tasks/{job_id}/cancel"),
                                  ("GET", "/assistant/browser-requests/sites"),
                                  ("POST", "/assistant/browser-requests/site-access"),
                                  ("POST", "/assistant/browser-requests/credential")])
def test_model_facing_routes_stay_assistant_callable(pair):
    assert not firewall.is_deny_listed(*pair)
    assert firewall.check(pair[0], pair[1], {pair}).allowed


def test_main_mounts_every_human_route_and_the_v2_routers():
    import app.main as app_main
    from fastapi.routing import APIRoute

    table = {(m, r.path) for r in app_main.app.routes if isinstance(r, APIRoute) for m in r.methods}
    for pair in HUMAN_ROUTES:
        assert pair in table, pair
    assert ("POST", "/assistant/browser-requests/tasks") in table
    assert ("POST", "/assistant/browser-results/{message_id}/carry") in table

    def calls(dependant):
        out = set()
        for dep in dependant.dependencies:
            out.add(dep.call)
            out |= calls(dep)
        return out

    for route in app_main.app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(("/assistant/browser-requests",
                                                                  "/assistant/browser-results")):
            assert routes.require_browser_v2 in calls(route.dependant), route.path


def test_require_browser_v2_is_a_bare_404_when_off(world):
    from fastapi import HTTPException

    world.config.assistant_browser_v2_enabled = False
    with pytest.raises(HTTPException) as exc:
        routes.require_browser_v2(SimpleNamespace(id=USER))
    assert exc.value.status_code == 404
    world.config.assistant_browser_v2_enabled = True
    assert routes.require_browser_v2(SimpleNamespace(id=USER)).id == USER


@pytest.mark.parametrize("pair", HUMAN_ROUTES)
def test_assistant_principal_is_refused_on_every_human_route(client, pair):
    http, user = client
    user.via_assistant_run_id = str(uuid4())
    result = http.request(pair[0], _concrete(pair[1]), json={})
    assert result.status_code == 403


@pytest.mark.parametrize("pair", [p for p in HUMAN_ROUTES if p[1] != "/assistant/browser-sites"])
def test_v2_routes_are_404_while_v2_is_off(client, world, pair):
    http, _ = client
    world.config.assistant_browser_v2_enabled = False
    result = http.request(pair[0], _concrete(pair[1]) + ("?expected_version=1" if pair[0] == "DELETE" else ""),
                          json={})
    assert result.status_code == 404
    assert result.headers["cache-control"] == "no-store"


def test_site_list_says_disabled_while_v2_is_off(client, world):
    http, _ = client
    world.config.assistant_browser_v2_enabled = False
    assert http.get("/assistant/browser-sites").json() == {"enabled": False, "items": []}


@pytest.mark.parametrize("method,path", [("POST", "/assistant/browser-tasks"),
                                         ("POST", "/assistant/browser-tasks/preview"),
                                         ("GET", f"/assistant/browser-tasks/by-approval/{uuid4()}")])
def test_phase6_task_routes_are_retired(client, method, path):
    http, _ = client
    result = http.request(method, path, json={"url": "https://app.procore.com/", "objective": "x"})
    assert result.status_code == 410
    assert _code(result) == "retired"


# ── Sites and grants (5.1, 5.2) ──────────────────────────────────────────────


def test_site_list_shows_curated_and_own_ad_hoc_sites_only(client, world):
    http, _ = client
    world.sites["domain:mine.example"] = site_row(site_key="domain:mine.example", kind="ad_hoc", label="mine")
    world.sites["domain:other.example"] = site_row(site_key="domain:other.example", kind="ad_hoc", label="other")
    world.grants.append(grant_row(site_key="domain:mine.example", value="mine.example"))
    keys = [s["site_key"] for s in http.get("/assistant/browser-sites").json()["items"]]
    assert "procore" in keys and "domain:mine.example" in keys and "domain:other.example" not in keys


def test_settings_grant_for_a_curated_site_uses_the_registry_scope(client, world):
    http, _ = client
    result = http.post("/assistant/browser-sites/grants",
                       json={"site_key": "procore", "scope": "origin", "value": "https://evil.example"})
    assert result.status_code == 200, result.text
    _, key, kw = world.calls[0]
    assert key == "procore" and kw["scope"] == "domain" and kw["value"] == "procore.com"
    assert kw["granted_via"] == "settings" and kw["provenance"] == "settings"
    assert result.json()["continued"] == [{"admission_id": ADMISSION, "status": "enqueued", "task_id": JOB}]
    assert world.continued == [ADMISSION]


def test_settings_grant_needs_a_site(client, world):
    http, _ = client
    result = http.post("/assistant/browser-sites/grants", json={"site_key": "procore"})
    assert result.status_code == 422 and _code(result) == "invalid"


@pytest.mark.parametrize("body", [None, [], {"site_key": "BAD KEY"}, {"request_id": "nope"}, {"extra": 1}])
def test_malformed_grant_body_is_refused(client, body):
    http, _ = client
    result = http.post("/assistant/browser-sites/grants", json=body)
    assert result.status_code == 422 and _code(result) == "invalid"


def test_grant_refusal_codes_map_to_status(client, world):
    http, _ = client
    world.grant_put_result = {"ok": False, "code": "ineligible"}
    result = http.post("/assistant/browser-sites/grants",
                       json={"site_key": "procore", "scope": "domain", "value": "procore.com"})
    assert result.status_code == 403 and _code(result) == "ineligible"
    assert world.continued == []


def test_card_grant_for_a_new_site_computes_the_ad_hoc_scope(client, world):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "site_access", "status": "requested", "site_key": None,
                               "url": "https://portal.gc-builders.net/bids/7?x=1", "provenance": "email",
                               "admission_id": ADMISSION, "job_id": None, "expires_at": FUTURE}
    result = http.post("/assistant/browser-sites/grants",
                       json={"request_id": REQUEST, "site_key": "procore", "scope": "domain", "value": "x.com"})
    assert result.status_code == 200, result.text
    _, key, kw = world.calls[0]
    assert key == "domain:gc-builders.net" and kw["scope"] == "domain" and kw["value"] == "gc-builders.net"
    assert kw["granted_via"] == "chat_card" and kw["provenance"] == "email" and kw["request_id"] == REQUEST
    assert kw["ad_hoc"] == {"label": "gc-builders.net"} or kw["ad_hoc"]["label"]


def test_card_grant_on_a_multi_tenant_domain_is_the_exact_origin(client, world):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "site_access", "status": "requested", "site_key": None,
                               "url": "https://acme.sharepoint.com/sites/bids/Shared", "provenance": "page",
                               "admission_id": None, "job_id": None, "expires_at": FUTURE}
    assert http.post("/assistant/browser-sites/grants", json={"request_id": REQUEST}).status_code == 200
    _, key, kw = world.calls[0]
    assert kw["scope"] == "origin" and kw["value"] == "https://acme.sharepoint.com"
    assert kw["path_prefix"] and key.startswith("origin:")


@pytest.mark.parametrize("change", [{"status": "declined"}, {"expires_at": PAST}, {"kind": "credential"}])
def test_card_grant_needs_an_open_site_request(client, world, change):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "site_access", "status": "requested", "site_key": "procore",
                               "url": None, "provenance": "typed", "admission_id": None, "job_id": None,
                               "expires_at": FUTURE, **change}
    result = http.post("/assistant/browser-sites/grants", json={"request_id": REQUEST})
    assert result.status_code in (404, 409)
    assert not [c for c in world.calls if c[0] == "grant_put"]


def test_mid_task_card_grant_extends_scope_and_releases_the_site_hold(client, world):
    http, _ = client
    world.sites["domain:gc-builders.net"] = site_row(site_key="domain:gc-builders.net", kind="ad_hoc",
                                                          label="gc-builders.net",
                                                          registrable_domains=["gc-builders.net"],
                                                          login_origins=[], cookie_domains=[])
    world.requests[REQUEST] = {"id": REQUEST, "kind": "site_access", "status": "requested",
                               "site_key": "domain:gc-builders.net", "url": "https://gc-builders.net/a",
                               "provenance": "page", "admission_id": None, "job_id": JOB, "expires_at": FUTURE}
    world.holds[HOLD] = {"id": HOLD, "job_id": JOB, "kind": "site", "status": "open",
                         "target_site_key": "domain:gc-builders.net"}
    result = http.post("/assistant/browser-sites/grants", json={"request_id": REQUEST, "site_only": True})
    assert result.status_code == 200, result.text
    assert ("scope_extend", JOB, "domain:gc-builders.net") in world.calls
    assert world.hold_decisions == [(HOLD, "approve_site_only")]
    assert result.json()["job"]["status"] == "approved"


def test_revoke_grant_needs_a_version(client, world):
    http, _ = client
    gid = str(uuid4())
    assert http.delete(f"/assistant/browser-sites/grants/{gid}").status_code == 422
    result = http.delete(f"/assistant/browser-sites/grants/{gid}?expected_version=2")
    assert result.status_code == 200
    assert result.json() == {"version": 3, "canceled_jobs": 1, "sessions_removed": 2}
    assert ("revoke", gid, 2) in world.calls


def test_signins_have_no_cookie_or_secret_columns(client, world):
    http, _ = client
    world.grants.append(grant_row())
    world.credentials.append(credential_v2_row(fingerprint="abc", ciphertext="zzz"))
    body = http.get("/assistant/browser-sites/signins").json()
    assert body["sessions"][0]["site_key"] == "procore" and "cookies_ciphertext" not in body["sessions"][0]
    account = body["accounts"][0]
    assert "fingerprint" not in account and "ciphertext" not in account
    assert body["grants"][0]["site_key"] == "procore"


def test_session_sign_out_and_safe_rule_remove(client):
    http, _ = client
    assert http.delete("/assistant/browser-sites/sessions/11111111-0000-4000-8000-000000000001").status_code == 204
    assert http.delete(f"/assistant/browser-sites/sessions/{uuid4()}").status_code == 404
    assert http.delete(f"/assistant/browser-sites/safe-rules/{uuid4()}").status_code == 404


# ── Card requests (5.5) ──────────────────────────────────────────────────────


def test_email_link_request_view_shows_the_sender_domain(client, world):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "link_open", "status": "requested", "site_key": "procore",
                               "url": "https://app.procore.com/123/project/bids?token=" + "a" * 70,
                               "provenance": "email", "admission_id": ADMISSION, "job_id": None,
                               "expires_at": FUTURE}
    world.admissions[ADMISSION] = {"id": ADMISSION, "goal": "Read the bid", "project_id": None,
                                   "max_cost_usd": "5.00", "tainted": False, "start_url": None}
    view = http.get(f"/assistant/browser-sites/requests/{REQUEST}").json()
    assert view["provenance"] == "email" and view["source_domain"] == "gc-builders.net"
    assert view["registrable_domain"] == "procore.com" and view["site_label"] == "Procore"
    assert view["carries_data"] is True and view["not_encrypted"] is False
    assert view["goal"] == "Read the bid" and view["max_cost_usd"] == 5.0


def test_email_link_from_a_mail_tool_result_shows_its_sender_domain(client, world, monkeypatch):
    """Review finding: a link the model read through the mail tools had no
    sender domain on its card."""
    http, _ = client
    seen = []

    def provenance(uid, run_id, url, *, conversation_id=None, **kw):
        seen.append((url, conversation_id))
        return sites.ProvenanceResult("email", "mail_tool", "gc.example.org")

    monkeypatch.setattr(sites, "provenance_detail", provenance)
    monkeypatch.setattr(routes.repo, "browser_url_email_sources", lambda uid, urls: [])
    conv = str(uuid4())
    world.requests[REQUEST] = {"id": REQUEST, "kind": "link_open", "status": "requested", "site_key": "procore",
                               "url": "https://app.procore.com/123/project/bids", "provenance": "email",
                               "conversation_id": conv, "admission_id": None, "job_id": None, "expires_at": FUTURE}
    view = http.get(f"/assistant/browser-sites/requests/{REQUEST}").json()
    assert view["source_domain"] == "gc.example.org"
    assert seen == [("https://app.procore.com/123/project/bids", conv)]


def test_ad_hoc_lookalike_request_is_flagged(client, world):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "site_access", "status": "requested", "site_key": None,
                               "url": "http://procore-login.net/", "provenance": "page", "admission_id": None,
                               "job_id": None, "expires_at": FUTURE}
    view = http.get(f"/assistant/browser-sites/requests/{REQUEST}").json()
    assert view["lookalike_of"] == "Procore" and view["ad_hoc"] is True and view["not_encrypted"] is True


def test_approving_a_link_card_continues_the_admission(client, world):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "link_open", "status": "requested", "admission_id": ADMISSION,
                               "job_id": None, "expires_at": FUTURE}
    result = http.post(f"/assistant/browser-sites/requests/{REQUEST}/decision", json={"decision": "approve"})
    assert result.status_code == 200 and result.json()["status"] == "approved"
    assert world.continued == [ADMISSION]


def test_declining_a_mid_task_site_card_declines_the_held_navigation(client, world):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "site_access", "status": "requested", "admission_id": None,
                               "job_id": JOB, "site_key": "domain:x.example", "expires_at": FUTURE}
    world.holds[HOLD] = {"id": HOLD, "job_id": JOB, "kind": "site", "status": "open",
                         "target_site_key": "domain:x.example"}
    result = http.post(f"/assistant/browser-sites/requests/{REQUEST}/decision", json={"decision": "decline"})
    assert result.status_code == 200 and world.hold_decisions == [(HOLD, "decline")]
    assert world.continued == []


def test_a_site_card_cannot_be_approved_through_the_decision_route(client, world):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "site_access", "status": "requested", "admission_id": None,
                               "job_id": None, "expires_at": FUTURE}
    result = http.post(f"/assistant/browser-sites/requests/{REQUEST}/decision", json={"decision": "approve"})
    assert result.status_code == 422 and _code(result) == "invalid"


# ── Credentials v2 (7.2, 7.3, 7.5, 7.6) ──────────────────────────────────────


def _cred_body(**over) -> dict:
    body = {"account_label": "default", "username": "me@example.com", "password": SENTINEL,
            "expected_version": 0, "acknowledge_personal_password": True}
    body.update(over)
    return body


@pytest.mark.parametrize("body", [None, [SENTINEL], {"password": SENTINEL}, {"password": {"x": SENTINEL}}, SENTINEL,
                                  _cred_body(account_label="bad/label"), _cred_body(expected_version="0"),
                                  _cred_body(extra=SENTINEL), _cred_body(request_id="not-a-uuid")])
def test_malformed_credential_body_is_never_reflected(client, world, body):
    http, _ = client
    result = http.put("/assistant/browser-credentials/procore", json=body)
    assert result.status_code == 422 and SENTINEL not in result.text
    assert result.headers["cache-control"] == "no-store"
    assert not [c for c in world.calls if c[0] == "credential_put"]


def test_curated_save_encrypts_and_returns_metadata_only(client, world, caplog):
    http, _ = client
    rid = str(uuid4())
    world.requests[rid] = {"id": rid, "kind": "credential", "site_key": "procore", "account_label": None,
                           "status": "requested"}
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body(request_id=rid))
    assert result.status_code == 200, result.text
    assert SENTINEL not in result.text and SENTINEL not in caplog.text
    body = result.json()
    assert "username" not in body["credential"] and body["credential"]["version"] == 1
    assert body["woken_jobs"] == [JOB] and body["waiting_admissions"] == [ADMISSION]
    assert body["continued"][0]["admission_id"] == ADMISSION and body["warnings"] == []
    _, key, label, expected, record, request_id = world.calls[0]
    assert (key, label, expected, request_id) == ("procore", "default", 0, rid)
    assert "confirmed_login_origin" not in record
    assert vault.decrypt_payload(USER, record["id"], 1, "credential", record).decode() == SENTINEL


def test_personal_password_acknowledgement_is_required(client, world):
    http, _ = client
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body(acknowledge_personal_password=False))
    assert result.status_code == 422 and _code(result) == "personal_password_needs_ack"


def test_org_procore_account_is_a_warning_only(client, world):
    http, _ = client
    result = http.put("/assistant/browser-credentials/procore",
                      json=_cred_body(username="RFI-Bot@g3electrical.example"))
    assert result.status_code == 200 and result.json()["warnings"] == ["org_account"]


def test_settings_list_keeps_showing_the_org_account_warning(client, world):
    http, _ = client
    world.credentials.append(credential_v2_row(username="RFI-Bot@g3electrical.example"))
    world.credentials.append(credential_v2_row(account_label="Mine", username="me@example.com"))
    items = {i["account_label"]: i for i in http.get("/assistant/browser-credentials").json()["items"]}
    assert items[credential_v2_row()["account_label"]]["warnings"] == ["org_account"]
    assert items["Mine"]["warnings"] == []


def test_stale_version_never_writes(client, world):
    http, _ = client
    world.credentials.append(credential_v2_row(version=2))
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body(expected_version=1))
    assert result.status_code == 409 and _code(result) == "version_conflict"
    assert not [c for c in world.calls if c[0] == "credential_put"]


def test_password_reuse_across_sites_needs_acknowledgement(client, world):
    http, _ = client
    world.credentials.append(credential_v2_row(site_key="smartbid", fingerprint=vault.password_fingerprint(USER, SENTINEL)))
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body())
    assert result.status_code == 409 and _code(result) == "reused_password_needs_ack"
    assert result.json()["detail"]["site_label"] == "SmartBid" and SENTINEL not in result.text
    ok = http.put("/assistant/browser-credentials/procore", json=_cred_body(acknowledge_reused_password=True))
    assert ok.status_code == 200


def test_same_site_password_is_not_reuse(client, world):
    http, _ = client
    world.credentials.append(credential_v2_row(account_label="other", fingerprint=vault.password_fingerprint(USER, SENTINEL)))
    assert http.put("/assistant/browser-credentials/procore", json=_cred_body()).status_code == 200


@pytest.fixture
def ad_hoc(world):
    world.sites["domain:gc-builders.net"] = site_row(site_key="domain:gc-builders.net", kind="ad_hoc",
                                                          label="gc-builders.net",
                                                          registrable_domains=["gc-builders.net"],
                                                          login_origins=[], cookie_domains=[])
    return "domain:gc-builders.net"


def test_ad_hoc_save_uses_the_confirmed_exact_origin(client, world, ad_hoc):
    http, _ = client
    result = http.put(f"/assistant/browser-credentials/{ad_hoc}",
                      json=_cred_body(confirmed_login_origin="https://login.gc-builders.net"))
    assert result.status_code == 200, result.text
    assert world.calls[0][4]["confirmed_login_origin"] == "https://login.gc-builders.net"


@pytest.mark.parametrize("origin,code", [
    (None, "origin_invalid"), ("http://login.gc-builders.net", "origin_invalid"),
    ("https://login.gc-builders.net/path", "origin_invalid"), ("https://other-site.net", "origin_invalid"),
    ("https://login.microsoftonline.com", "identity_provider_refused"),
    ("https://accounts.google.com", "identity_provider_refused"),
])
def test_ad_hoc_save_refuses_bad_origins(client, world, ad_hoc, origin, code):
    http, _ = client
    result = http.put(f"/assistant/browser-credentials/{ad_hoc}", json=_cred_body(confirmed_login_origin=origin))
    assert result.status_code == 422 and _code(result) == code
    assert not [c for c in world.calls if c[0] == "credential_put"]


def test_curated_identity_provider_login_origin_is_refused(client, world):
    http, _ = client
    world.sites["procore"]["login_origins"] = ["https://login.microsoftonline.com"]
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body())
    assert result.status_code == 422 and _code(result) == "identity_provider_refused"


def test_unknown_site_is_refused(client):
    http, _ = client
    assert _code(http.put("/assistant/browser-credentials/nosuch", json=_cred_body())) == "unknown_site"
    assert http.put("/assistant/browser-credentials/BAD KEY", json=_cred_body()).status_code == 404


def test_vault_unavailable_is_a_fixed_409(client, world):
    http, _ = client
    world.config.user_cred_kek = ""
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body())
    assert result.status_code == 409 and _code(result) == "unavailable" and SENTINEL not in result.text


def test_delete_credential_by_label(client, world):
    http, _ = client
    row = credential_v2_row(account_label="work", version=3)
    world.credentials.append(row)
    assert http.delete("/assistant/browser-credentials/procore?account_label=work").status_code == 422
    result = http.delete("/assistant/browser-credentials/procore?account_label=work&expected_version=3")
    assert result.status_code == 204
    assert ("credential_revoke", row["id"], 3) in world.calls
    assert http.delete("/assistant/browser-credentials/procore?account_label=none&expected_version=1").status_code == 404


def test_credential_list_has_no_secret_columns(client, world):
    http, _ = client
    world.credentials.append(credential_v2_row(fingerprint="f", ciphertext="c", wrapped_dek="w"))
    item = http.get("/assistant/browser-credentials").json()["items"][0]
    assert not {"fingerprint", "ciphertext", "wrapped_dek", "password"} & item.keys()


# ── Holds and the start card (6.3, 6.5, 4.3) ─────────────────────────────────


def _final_hold(**over) -> dict:
    hold = {"id": HOLD, "job_id": JOB, "kind": "final_action", "status": "open", "method": "POST",
            "origin": "https://app.procore.com", "out_of_scope": False, "label": None, "target_site_key": "procore",
            "summary_record_id": str(uuid4()), "screenshot_record_id": str(uuid4()), "expires_at": FUTURE}
    hold.update(over)
    return hold


def test_hold_view_shows_the_full_url_and_hides_values(client, world):
    http, _ = client
    world.holds[HOLD] = _final_hold()
    world.summary = {"url": "https://app.procore.com/rest/v1/rfis/12/replies?draft=0",
                     "body_summary": [{"name": "body", "value": "Typed reply", "hidden": False},
                                      {"name": "passwd", "value": "leak", "hidden": True}],
                     "model_description": "Send the RFI reply", "profile_label": "Answer RFI"}
    view = http.get(f"/assistant/browser-holds/{HOLD}").json()
    assert view["url"].endswith("?draft=0") and view["label"] == "Answer RFI"
    assert view["fields"] == [{"name": "body", "value": "Typed reply"}, {"name": "passwd", "value": None}]
    assert "leak" not in str(view) and view["description"] == "Send the RFI reply"
    assert view["registrable_domain"] == "procore.com" and view["site_label"] == "Procore"


def test_approving_a_final_action_goes_through_the_hold_service(client, world):
    http, _ = client
    world.holds[HOLD] = _final_hold()
    result = http.post(f"/assistant/browser-holds/{HOLD}/decision", json={"decision": "approve"})
    assert result.status_code == 200 and result.json() == {"status": "approved"}
    assert world.hold_decisions == [(HOLD, "approve")]


def test_approve_site_only_is_refused_on_a_final_action(client, world):
    http, _ = client
    world.holds[HOLD] = _final_hold()
    result = http.post(f"/assistant/browser-holds/{HOLD}/decision", json={"decision": "approve_site_only"})
    assert result.status_code == 422 and world.hold_decisions == []


@pytest.mark.parametrize("code,status", [("hold_aborted", 409), ("hold_expired", 409), ("hold_changed", 409),
                                         ("not_found", 404), ("weird", 409)])
def test_hold_service_refusals_map_to_fixed_codes(client, world, code, status):
    http, _ = client
    world.holds[HOLD] = _final_hold()
    error = ValueError(code)
    error.code = code
    world.hold_decide_error = error
    result = http.post(f"/assistant/browser-holds/{HOLD}/decision", json={"decision": "decline"})
    assert result.status_code == status and _code(result) == (code if code != "weird" else "hold_changed")


def test_hold_service_not_built_yet_is_unavailable(client, world):
    http, _ = client
    world.holds[HOLD] = _final_hold()
    world.hold_decide_error = NotImplementedError("WI-6")
    result = http.post(f"/assistant/browser-holds/{HOLD}/decision", json={"decision": "approve"})
    assert result.status_code == 409 and _code(result) == "unavailable"


def test_site_hold_approve_needs_an_existing_grant(client, world):
    http, _ = client
    world.holds[HOLD] = _final_hold(kind="site", target_site_key="smartbid")
    result = http.post(f"/assistant/browser-holds/{HOLD}/decision", json={"decision": "approve"})
    assert result.status_code == 409 and _code(result) == "no_grant" and world.hold_decisions == []
    world.grants.append(grant_row(site_key="smartbid", value="smartbidnet.com"))
    result = http.post(f"/assistant/browser-holds/{HOLD}/decision", json={"decision": "approve_site_only"})
    assert result.status_code == 200
    assert ("scope_extend", JOB, "smartbid") in world.calls and world.hold_decisions == [(HOLD, "approve_site_only")]


def test_unknown_hold_is_404(client):
    http, _ = client
    assert http.get(f"/assistant/browser-holds/{uuid4()}").status_code == 404
    assert http.post(f"/assistant/browser-holds/{uuid4()}/decision", json={"decision": "decline"}).status_code == 404


def test_safe_rule_pattern_generalizes_numbers_and_tokens():
    assert routes.safe_path_pattern("/api/v1/projects/123/filters") == r"^/api/v1/projects/[0-9]+/filters$"
    assert routes.safe_path_pattern("/q/" + "a1B2" * 6) == r"^/q/[A-Za-z0-9_-]+$"
    assert routes.safe_path_pattern("/x/0f8fad5b-d9cb-469f-a165-70867728950e") == r"^/x/[A-Za-z0-9_-]+$"
    assert routes.safe_path_pattern("/a.b") == r"^/a\.b$"


def test_safe_rule_pattern_keeps_long_word_segments_exact():
    """A long word-only segment is not a token: the rule for a filter save
    must never also match a sibling final action such as replies."""
    import re

    pattern = routes.safe_path_pattern("/rest/v1.0/projects/1/rfis/12/bulk_filter_preferences")
    assert pattern == r"^/rest/v1\.0/projects/[0-9]+/rfis/[0-9]+/bulk_filter_preferences$"
    assert re.fullmatch(pattern, "/rest/v1.0/projects/1/rfis/12/replies") is None
    assert routes.safe_path_pattern("/s/abcdefghijklmnopqrstuvwxyz") == "^/s/abcdefghijklmnopqrstuvwxyz$"


def test_mark_safe_creates_an_owner_rule_from_the_held_request(client, world):
    http, _ = client
    world.holds[HOLD] = _final_hold(origin="https://app.procore.com:8443")
    world.summary = {"url": "https://app.procore.com:8443/rest/v1/projects/55/search?q=1", "graphql_operation": "Find"}
    result = http.post(f"/assistant/browser-holds/{HOLD}/safe-rule")
    assert result.status_code == 200, result.text
    _, hold_id, kw = world.calls[0]
    assert hold_id == HOLD and kw == {"method": "POST", "host": "app.procore.com:8443",
                                      "path_pattern": r"^/rest/v1/projects/[0-9]+/search$",
                                      "graphql_operation": "Find"}
    assert world.hold_decisions == []  # marking safe never releases the held request


@pytest.mark.parametrize("url,op", [("https://app.procore.com/rfis/1/submit", None),
                                    ("https://app.procore.com/graphql", "DeleteRfi")])
def test_mark_safe_refuses_final_verbs(client, world, url, op):
    http, _ = client
    world.holds[HOLD] = _final_hold()
    world.summary = {"url": url, "graphql_operation": op}
    result = http.post(f"/assistant/browser-holds/{HOLD}/safe-rule")
    assert result.status_code == 422 and _code(result) == "final_verb"


@pytest.mark.parametrize("over", [{"out_of_scope": True}, {"kind": "site"}])
def test_mark_safe_refuses_out_of_scope_and_site_holds(client, world, over):
    http, _ = client
    world.holds[HOLD] = _final_hold(**over)
    world.summary = {"url": "https://app.procore.com/x"}
    assert http.post(f"/assistant/browser-holds/{HOLD}/safe-rule").status_code == 422


def test_mark_safe_needs_a_summary_on_the_hold_origin(client, world):
    http, _ = client
    world.holds[HOLD] = _final_hold()
    world.summary = {"url": "https://other-site.net/x"}
    assert _code(http.post(f"/assistant/browser-holds/{HOLD}/safe-rule")) == "unavailable"


def test_start_card_start_continues_and_decline_does_not(client, world):
    http, _ = client
    world.admissions[ADMISSION] = {"id": ADMISSION, "status": "waiting_start"}
    result = http.post(f"/assistant/browser-holds/start/{ADMISSION}/decision", json={"decision": "start"})
    assert result.status_code == 200 and world.continued == [ADMISSION]
    assert result.json()["needs"] == ["credential"]
    result = http.post(f"/assistant/browser-holds/start/{ADMISSION}/decision", json={"decision": "decline"})
    assert result.json()["status"] == "declined" and world.continued == [ADMISSION]


def test_start_card_changed_admission_is_409(client):
    http, _ = client
    result = http.post(f"/assistant/browser-holds/start/{uuid4()}/decision", json={"decision": "start"})
    assert result.status_code == 409 and _code(result) == "admission_changed"


def test_start_card_body_is_strict(client):
    http, _ = client
    result = http.post(f"/assistant/browser-holds/start/{ADMISSION}/decision", json={"decision": "approve"})
    assert result.status_code == 422


def test_a_failed_continue_never_undoes_the_decision(client, world, monkeypatch):
    http, _ = client

    def boom(uid, aid):
        raise RuntimeError("queue down")

    monkeypatch.setattr(browser, "continue_admission", boom)
    world.admissions[ADMISSION] = {"id": ADMISSION, "status": "waiting_start"}
    result = http.post(f"/assistant/browser-holds/start/{ADMISSION}/decision", json={"decision": "start"})
    assert result.status_code == 200
    assert result.json()["continued"] == [{"admission_id": ADMISSION, "status": "waiting", "task_id": None}]


def test_card_grant_ignores_the_body_scope_fields(client, world):
    http, _ = client
    world.requests[REQUEST] = {"id": REQUEST, "kind": "site_access", "status": "requested", "site_key": "smartbid",
                               "url": None, "provenance": "typed", "admission_id": None, "job_id": None,
                               "expires_at": FUTURE}
    result = http.post("/assistant/browser-sites/grants",
                       json={"request_id": REQUEST, "site_key": None, "scope": "origin", "value": ""})
    assert result.status_code == 200, result.text
    _, key, kw = world.calls[0]
    assert key == "smartbid" and kw["scope"] == "domain" and kw["value"] == "smartbidnet.com"


def test_a_cap_refusal_after_the_click_is_shown_on_the_card(client, world, monkeypatch):
    """Review finding: a concurrency or day-cap refusal after the owner's
    click used to be swallowed into a silent 'waiting'."""
    http, _ = client

    def capped(uid, aid):
        raise browser.AdmissionRefused("user_cap")

    monkeypatch.setattr(browser, "continue_admission", capped)
    world.admissions[ADMISSION] = {"id": ADMISSION, "status": "waiting_start"}
    result = http.post(f"/assistant/browser-holds/start/{ADMISSION}/decision", json={"decision": "start"})
    assert result.status_code == 200
    assert result.json()["continued"] == [{"admission_id": ADMISSION, "status": "refused", "task_id": None,
                                           "code": "user_cap",
                                           "message": "Your daily assistant budget is used up."}]


def test_site_hold_view_carries_its_site_access_request(client, world):
    http, _ = client
    rid = str(uuid4())
    world.holds[HOLD] = _final_hold(kind="site", screenshot_record_id=None)
    world.summary = {"url": "https://new-site.example.net/a", "request_id": rid}
    assert http.get(f"/assistant/browser-holds/{HOLD}").json()["request_id"] == rid
    world.summary = {"url": "https://new-site.example.net/a", "request_id": "not-a-uuid"}
    assert http.get(f"/assistant/browser-holds/{HOLD}").json()["request_id"] is None
    world.holds[HOLD] = _final_hold()
    world.summary = {"url": "https://app.procore.com/x", "request_id": rid}
    assert http.get(f"/assistant/browser-holds/{HOLD}").json()["request_id"] is None


def test_credential_save_refuses_a_request_for_another_site_or_label(client, world):
    """Review finding: a stale or mis-wired card must not mark another
    request saved or pin its job to the wrong site."""
    http, _ = client
    rid = str(uuid4())
    world.requests[rid] = {"id": rid, "kind": "credential", "site_key": "smartbid", "account_label": None,
                           "status": "requested"}
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body(request_id=rid))
    assert result.status_code == 409 and _code(result) == "request_changed"
    world.requests[rid].update(site_key="procore", account_label="Work")
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body(request_id=rid))
    assert result.status_code == 409 and _code(result) == "request_changed"
    assert not [c for c in world.calls if c[0] == "credential_put"]
    world.requests[rid]["account_label"] = None
    result = http.put("/assistant/browser-credentials/procore", json=_cred_body(request_id=rid))
    assert result.status_code == 200
    assert [c for c in world.calls if c[0] == "credential_put"][-1][-1] == rid


def test_grant_list_returns_only_active_grants(client, world):
    """Review finding: revoked grants came back to a type with no status."""
    http, _ = client
    active = grant_row(site_key="procore")
    revoked = grant_row(site_key="smartbid", status="revoked")
    world.grants.extend([active, revoked])
    items = http.get("/assistant/browser-sites/grants").json()["items"]
    assert [i["site_key"] for i in items] == ["procore"]
