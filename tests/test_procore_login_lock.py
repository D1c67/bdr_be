"""The shared Procore login: the atomic reservation, the atomic failure
count, and the browser agents' identity endpoints (docs/browser_agents/
SPEC.md 5.5 as replaced by override O2, 10.1).

Driven through the real routes (TestClient, worker token, lease headers)
over the in-memory FakeDB with the 0157 RPC twins. Pinned: the stored jar
handout and every refusal (no_jar, locked, help_open, not_configured); the
login release (password only in that no-store response, never in a row, a
step or a notice), the shared 600 s spacing and lock refusals; result ok
saves a jar the sweep and the harvest adopt and resolves the login help;
password_rejected counts toward the shared lock and rings the Settings
bell; the other outcomes only record last_error; IDOR, idempotent resends,
and the unreported-login sweep; ensure_session honoring a reservation the
agent made.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
import pytest

from app.services import pm_rfi_sync, procore_creds, rfp_harvest
from app.services import procore_client as pc
from app.services.browser_agents import identities, protocol, secrets
from tests.browser_agents_fake import iso
from tests.browser_agents_harness import API, Harness, error_code

PASSWORD = "Pr0core-Passw0rd"
EMAIL = "agent@g3.test"


class Agent:
    def __init__(self, monkeypatch, **settings_over):
        self.h = Harness(monkeypatch, **settings_over)
        h = self.h
        for mod in (secrets, procore_creds, identities, rfp_harvest):
            monkeypatch.setattr(mod, "get_settings", lambda: h.settings)
        monkeypatch.setattr(rfp_harvest, "get_supabase", lambda: h.db)
        monkeypatch.setattr(pm_rfi_sync, "get_supabase", lambda: h.db)
        monkeypatch.setattr(rfp_harvest, "_now", lambda: h.db.now)

        def bell(role, project_id, type_, message, **k):
            h.notices.append({"role": role, "type": type_, "message": message, "metadata": k.get("metadata")})

        monkeypatch.setattr(rfp_harvest, "notify_role", bell)
        procore_creds.reset_cache()
        self.worker, self.token, self.boot, self.job = h.active_with_job("rfi_from_email")

    def headers(self, job=None):
        job = job or self.job
        return self.h.lease_headers(self.token, job["id"], int(job["lease_epoch"]), self.boot)

    def post(self, what: str, body: dict, job=None, identity: str = "procore"):
        job = job or self.job
        return self.h.client.post(f"{API}/jobs/{job['id']}/identities/{identity}/{what}", json=body,
                                  headers=self.headers(job))

    def set_password(self, value: str = PASSWORD):
        secrets.set_secret("procore_password", value, actor_id=None, actor_label="IT Admin")
        procore_creds.reset_cache()

    def session_row(self, provider="procore") -> dict:
        rows = self.h.db.rows("rfp_harvest_sessions", provider=provider)
        return rows[0] if rows else {}

    def store_jar(self, **over):
        row = {"provider": "procore", "account": EMAIL, "logged_in_at": iso(self.h.db.now - timedelta(hours=1)),
               "cookies": [
                   {"name": "_session_id", "value": "jar-value-1", "domain": "app.procore.com", "path": "/",
                    "expires": None, "secure": True},
                   {"name": "login_session_id", "value": "jar-value-2", "domain": "login.procore.com",
                    "path": "/", "expires": (self.h.db.now + timedelta(days=1)).timestamp(), "secure": True},
                   {"name": "old", "value": "expired", "domain": "app.procore.com", "path": "/",
                    "expires": (self.h.db.now - timedelta(days=1)).timestamp()},
                   {"name": "tracker", "value": "x", "domain": "evil.example.com", "path": "/"},
               ],
               "login_failures": 0, "locked_until": None, "last_login_attempt_at": None, **over}
        self.h.db.tables.setdefault("rfp_harvest_sessions", []).append(row)
        return row

    def open_login_help(self, holder_job_id=None, project_id=None):
        help_id = str(uuid.uuid4())
        self.h.db.tables.setdefault("browser_agent_help_requests", []).append({
            "id": help_id, "principal_kind": "org", "scope_key": "login:procore", "reason_code": "mfa", "title": "Procore code needed",
            "status": "open", "holder_job_id": holder_job_id, "project_id": project_id,
            "expires_at": iso(self.h.db.now + timedelta(hours=48)),
        })
        return help_id


def _attempts(a: Agent, **eq):
    return a.h.db.rows("browser_agent_login_attempts", **eq)


# ── session ─────────────────────────────────────────────────────────────────


def test_session_hands_out_the_stored_jar_filtered_and_logs_it(monkeypatch):
    a = Agent(monkeypatch)
    jar = a.store_jar()
    resp = a.post("session", {"host": "app.procore.com"})
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    body = protocol.SessionResponse.model_validate(resp.json())
    assert body.status == "ok" and body.source == "stored"
    assert sorted(c.name for c in body.cookies) == ["_session_id", "login_session_id"]
    assert body.logged_in_at.isoformat().startswith(jar["logged_in_at"][:19])
    [att] = _attempts(a, kind="session")
    assert (att["granted"], att["outcome"], att["refused_reason"]) == (True, "stored_jar", None)
    assert att["reported_at"] and att["worker_id"] == a.worker["id"]
    step = a.h.db.rows("browser_agent_steps", job_id=a.job["id"], kind="login.session")[0]
    assert "jar-value" not in json.dumps(step)


def test_session_refuses_the_jar_the_worker_just_rejected_or_another_accounts(monkeypatch):
    a = Agent(monkeypatch)
    jar = a.store_jar()
    resp = a.post("session", {"host": "app.procore.com", "reject_jar_logged_in_at": jar["logged_in_at"]})
    assert resp.json()["status"] == "no_jar" and resp.json().get("cookies") is None
    jar["account"] = "someone-else@g3.test"
    assert a.post("session", {"host": "app.procore.com"}).json()["status"] == "no_jar"
    assert [r["refused_reason"] for r in _attempts(a, kind="session")] == ["no_jar", "no_jar"]


def test_session_refusals_locked_help_open_not_configured(monkeypatch):
    a = Agent(monkeypatch)
    until = a.h.db.now + timedelta(hours=6)
    a.h.db.tables["rfp_harvest_sessions"] = [{"provider": "procore", "account": EMAIL, "cookies": [],
                                              "locked_until": iso(until), "login_failures": 3}]
    body = a.post("session", {"host": "app.procore.com"}).json()
    assert body["status"] == "locked" and body["retry_at"].startswith(iso(until)[:19])
    help_id = a.open_login_help(holder_job_id=a.h.job(status="waiting_help")["id"])
    body = a.post("session", {"host": "app.procore.com"}).json()
    assert body == {**body, "status": "help_open", "help_id": help_id}
    a.h.settings = a.h.settings.model_copy(update={"procore_login_email": ""})
    assert a.post("session", {"host": "app.procore.com"}).json()["status"] == "not_configured"


def test_the_help_holder_still_gets_the_jar(monkeypatch):
    a = Agent(monkeypatch)
    a.store_jar()
    a.open_login_help(holder_job_id=a.job["id"])
    assert a.post("session", {"host": "app.procore.com"}).json()["status"] == "ok"


def test_us02_jobs_read_the_us02_cookie_row(monkeypatch):
    a = Agent(monkeypatch)
    a.store_jar(provider="procore:us02.procore.com")
    # The job's host must be the seed host: an app.procore.com job asking for us02 is refused.
    resp = a.post("session", {"host": "us02.procore.com"})
    assert resp.status_code == 422 and error_code(resp) == "invalid_request"
    a.job["input"]["host"] = "us02.procore.com"
    assert a.post("session", {"host": "us02.procore.com"}).json()["status"] == "ok"


def test_wrong_identity_and_stale_lease_are_refused(monkeypatch):
    a = Agent(monkeypatch)
    resp = a.post("session", {"host": "app.procore.com"}, identity="buildingconnected")
    assert resp.status_code == 422 and error_code(resp) == "invalid_request"
    stale = {**a.job, "lease_epoch": int(a.job["lease_epoch"]) + 5}
    resp = a.post("session", {"host": "app.procore.com"}, job=stale)
    assert resp.status_code == 409 and error_code(resp) == "lease_lost"


# ── login/begin ───────────────────────────────────────────────────────────────


def test_begin_releases_the_password_once_no_store_and_nowhere_else(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    resp = a.post("login/begin", {"host": "app.procore.com"})
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["granted"] is True and body["email"] == EMAIL and body["password"] == PASSWORD
    parsed = protocol.LoginBeginResponse.model_validate(body)
    assert PASSWORD not in repr(parsed)                      # SecretStr on the worker side too
    [att] = _attempts(a, kind="browser")
    assert str(att["id"]) == body["attempt_id"] and att["granted"] and att["secret_version"] == 1
    assert att["reported_at"] is None
    row = a.session_row()
    assert row["last_login_source"] == f"agent:{a.worker['id']}" and row["last_login_attempt_at"]
    # The attempt row and the ledger step are the record: no bell per granted login.
    assert not [n for n in a.h.notices if n["type"] == "browser_agent.login_released"]
    [step] = a.h.db.rows("browser_agent_steps", job_id=a.job["id"], kind="login.begin")
    assert step["result"] == {"attempt_id": body["attempt_id"]}
    everything = json.dumps({k: v for k, v in a.h.db.tables.items() if k != "browser_agent_secrets"},
                            default=str) + json.dumps(a.h.notices, default=str)
    assert PASSWORD not in everything


def test_begin_uses_the_env_password_when_none_is_stored(monkeypatch):
    a = Agent(monkeypatch, procore_login_password="env-password")
    body = a.post("login/begin", {"host": "app.procore.com"}).json()
    assert body["password"] == "env-password"
    assert _attempts(a, kind="browser")[0]["secret_version"] is None


def test_begin_honors_the_shared_spacing_and_lock(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    assert a.post("login/begin", {"host": "app.procore.com"}).json()["granted"] is True
    body = a.post("login/begin", {"host": "app.procore.com"}).json()
    assert (body["granted"], body["reason"]) == (False, "spacing") and body.get("password") is None
    assert body["retry_at"].startswith(iso(a.h.db.now + timedelta(seconds=600))[:19])
    # A login the server-side sweep started is spacing too: one gate for everyone.
    a.h.db.advance(601)
    store = rfp_harvest._SessionStore(a.h.settings)
    assert store.reserve_login(600, "backend")["granted"] is True
    assert a.post("login/begin", {"host": "app.procore.com"}).json()["reason"] == "spacing"
    a.session_row()["locked_until"] = iso(a.h.db.now + timedelta(hours=6))
    a.h.db.advance(601)
    body = a.post("login/begin", {"host": "app.procore.com"}).json()
    assert (body["granted"], body["reason"]) == (False, "locked")
    assert [r["refused_reason"] for r in _attempts(a, kind="browser", granted=False)] == [
        "spacing", "spacing", "locked"]


def test_begin_refuses_help_open_for_non_holders_and_not_configured(monkeypatch):
    a = Agent(monkeypatch)
    body = a.post("login/begin", {"host": "app.procore.com"}).json()
    assert (body["granted"], body["reason"]) == (False, "not_configured")    # no password anywhere
    a.set_password()
    help_id = a.open_login_help(holder_job_id=a.h.job(status="waiting_help")["id"])
    body = a.post("login/begin", {"host": "app.procore.com"}).json()
    assert (body["reason"], body["help_id"]) == ("help_open", help_id)
    a.h.db.rows("browser_agent_help_requests", id=help_id)[0]["holder_job_id"] = a.job["id"]
    assert a.post("login/begin", {"host": "app.procore.com"}).json()["granted"] is True


def test_a_login_help_whose_holder_ended_is_taken_over_by_the_asking_job(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    holder = a.h.job(status="canceled")
    help_id = a.open_login_help(holder_job_id=holder["id"])
    body = a.post("login/begin", {"host": "app.procore.com"}).json()
    assert body["granted"] is True                                   # not refused help_open
    assert a.h.db.one("browser_agent_help_requests", id=help_id)["holder_job_id"] == a.job["id"]
    [step] = a.h.db.rows("browser_agent_steps", job_id=a.job["id"], kind="help.holder_moved")
    assert step["result"]["previous_holder"] == holder["id"]
    # A live holder (even parked) keeps the request: others are still refused.
    a.h.db.rows("browser_agent_help_requests", id=help_id)[0]["holder_job_id"] = a.h.job(status="waiting_help")["id"]
    assert a.post("session", {"host": "app.procore.com"}).json()["status"] == "help_open"


# ── login/result ──────────────────────────────────────────────────────────────


def _begin(a: Agent) -> str:
    body = a.post("login/begin", {"host": "app.procore.com"}).json()
    assert body["granted"], body
    return body["attempt_id"]


COOKIES = [
    {"name": "_session_id", "value": "fresh-1", "domain": "app.procore.com", "path": "/", "expires": -1,
     "secure": True, "httpOnly": True, "sameSite": "Lax"},
    {"name": "login_session_id", "value": "fresh-2", "domain": ".procore.com", "path": "/",
     "expires": 1893456000, "secure": True, "httpOnly": True, "sameSite": "None"},
    {"name": "ad", "value": "x", "domain": "ads.example.com", "path": "/", "expires": -1, "secure": True,
     "httpOnly": False, "sameSite": "Lax"},
]


def test_result_ok_saves_a_jar_every_procore_client_adopts(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    old_logged_in = a.store_jar(login_failures=2, last_error="password: no")["logged_in_at"]
    # A server-side session that loaded the old jar earlier.
    sweep = pc.ProcoreSession(procore_creds.config(a.h.settings), rfp_harvest._SessionStore(a.h.settings),
                              transport=httpx.MockTransport(lambda r: pytest.fail("no Procore call")),
                              now=lambda: a.h.db.now)
    sweep._load_store()
    a.h.db.advance(5)
    attempt = _begin(a)
    resp = a.post("login/result", {"attempt_id": attempt, "outcome": "ok", "step": "done", "cookies": COOKIES})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"locked_until": None, "login_failures": 0, "help_reason": None}
    row = a.session_row()
    assert row["account"] == EMAIL and [c["name"] for c in row["cookies"]] == ["_session_id", "login_session_id"]
    assert row["login_failures"] == 0 and row["last_error"] is None
    assert row["last_login_source"] == f"agent:{a.worker['id']}"
    assert row["logged_in_at"] > old_logged_in
    assert sweep._jar_is_newer(rfp_harvest._SessionStore(a.h.settings).load())
    # The adopting session loads the agent's jar into httpx.
    sweep._client.cookies.clear()
    sweep._load_store()
    assert sweep._has_app_session()
    att = _attempts(a, kind="browser")[0]
    assert (att["outcome"], att["reported_at"] is not None, att["counted_as_failure"]) == ("ok", True, False)
    sweep.close()


def test_result_ok_resolves_the_login_help_and_requeues_parked_waiters(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    help_id = a.open_login_help(holder_job_id=a.job["id"], project_id=a.h.PROJECT_ID)
    parked = a.h.job("rfi_from_email")
    parked.update(status="waiting_help", waiting_help_id=help_id, lease_owner=None)
    a.job.update(status="waiting_help", waiting_help_id=help_id)
    a.h.db.tables.setdefault("browser_agent_help_waiters", []).extend([
        {"help_id": help_id, "job_id": a.job["id"]}, {"help_id": help_id, "job_id": parked["id"]}])
    dismissed = []
    monkeypatch.setattr("app.services.notifications.dismiss_notifications", lambda **k: dismissed.append(k))
    attempt = _begin(a)
    a.post("login/result", {"attempt_id": attempt, "outcome": "ok", "cookies": COOKIES[:1]})
    help_row = a.h.db.one("browser_agent_help_requests", id=help_id)
    assert (help_row["status"], help_row["resolution"]) == ("resolved", "auto_resolved")
    assert (parked["status"], parked["resume_reason"], parked["priority"]) == ("queued", "help_answered", 10)
    assert a.job["status"] == "running" and a.job["waiting_help_id"] is None
    assert dismissed and dismissed[0]["metadata_eq"] == {"help_id": help_id}


def test_password_rejected_counts_toward_the_shared_lock_and_rings_settings(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    reasons = []
    for _ in range(3):
        attempt = _begin(a)
        body = a.post("login/result", {"attempt_id": attempt, "outcome": "password_rejected",
                                       "step": "password"}).json()
        reasons.append(body["help_reason"])
        a.h.db.advance(601)
    assert reasons == ["login_failed", "login_failed", "login_locked"]
    row = a.session_row()
    assert row["login_failures"] == 3 and row["locked_until"]
    assert body["locked_until"] and body["login_failures"] == 3
    [bell] = [n for n in a.h.notices if n["type"] == rfp_harvest.NOTIFY_LOGIN_FAILED]
    assert "Check the Procore password in Settings > Browser agents." in bell["message"]
    assert all(r["counted_as_failure"] for r in _attempts(a, kind="browser"))
    # Locked: the next begin is refused, and so is the server-side sweep's login.
    assert a.post("login/begin", {"host": "app.procore.com"}).json()["reason"] == "locked"
    session = pc.ProcoreSession(procore_creds.config(a.h.settings), rfp_harvest._SessionStore(a.h.settings),
                                transport=httpx.MockTransport(lambda r: pytest.fail("no Procore call")),
                                now=lambda: a.h.db.now)
    with pytest.raises(pc.ProcoreLoginLocked):
        session.ensure_session()
    session.close()


@pytest.mark.parametrize("outcome,reason", [
    ("mfa", "mfa"), ("captcha", "captcha"), ("challenge", "captcha"), ("sso", "sso"),
    ("unexpected", "login_failed"), ("timeout", "login_failed"),
])
def test_other_outcomes_record_last_error_only(monkeypatch, outcome, reason):
    a = Agent(monkeypatch)
    a.set_password()
    attempt = _begin(a)
    body = a.post("login/result", {"attempt_id": attempt, "outcome": outcome, "step": "code_page"}).json()
    assert body == {"locked_until": None, "login_failures": 0, "help_reason": reason}
    row = a.session_row()
    assert row["login_failures"] == 0 and row["last_error"].startswith(f"{outcome}:")
    assert not _attempts(a, kind="browser")[0]["counted_as_failure"]


def test_result_idor_resend_and_bad_jars(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    attempt = _begin(a)
    resp = a.post("login/result", {"attempt_id": attempt, "outcome": "ok",
                                   "cookies": [COOKIES[2]]})                   # nothing on Procore
    assert resp.status_code == 422 and error_code(resp) == "invalid_request"
    assert _attempts(a, kind="browser")[0]["reported_at"] is None              # still open
    first = a.post("login/result", {"attempt_id": attempt, "outcome": "password_rejected"}).json()
    again = a.post("login/result", {"attempt_id": attempt, "outcome": "password_rejected"}).json()
    assert first["login_failures"] == again["login_failures"] == 1             # counted once
    resp = a.post("login/result", {"attempt_id": str(uuid.uuid4()), "outcome": "mfa"})
    assert resp.status_code == 403 and error_code(resp) == "idor"
    # Another job's attempt is not this job's to report.
    other = a.h.job("rfi_from_email")
    a.h.db.tables["browser_agent_login_attempts"].append({
        "id": str(uuid.uuid4()), "identity": "procore", "host": "app.procore.com", "kind": "browser",
        "job_id": other["id"], "granted": True, "reported_at": None, "created_at": iso(a.h.db.now)})
    foreign = a.h.db.tables["browser_agent_login_attempts"][-1]["id"]
    resp = a.post("login/result", {"attempt_id": foreign, "outcome": "ok", "cookies": COOKIES[:1]})
    assert resp.status_code == 403 and error_code(resp) == "idor"


def test_mfa_then_ok_on_the_same_attempt_saves_the_jar(monkeypatch):
    """The worker reports `mfa` first (help opens), then `ok` with cookies
    once the helper's code went in: the later report is not a resend."""
    a = Agent(monkeypatch)
    a.set_password()
    attempt = _begin(a)
    first = a.post("login/result", {"attempt_id": attempt, "outcome": "mfa", "step": "code"}).json()
    assert first["help_reason"] == "mfa"
    again = a.post("login/result", {"attempt_id": attempt, "outcome": "mfa", "step": "code"}).json()
    assert again["help_reason"] == "mfa"                                       # wrong code: still mfa
    resp = a.post("login/result", {"attempt_id": attempt, "outcome": "ok", "cookies": COOKIES})
    assert resp.status_code == 200 and resp.json()["help_reason"] is None
    row = a.session_row()
    assert [c["name"] for c in row["cookies"]] == ["_session_id", "login_session_id"]
    assert row["last_login_source"] == f"agent:{a.worker['id']}"
    assert _attempts(a, kind="browser")[0]["outcome"] == "ok"
    # Final now: a later report changes nothing.
    late = a.post("login/result", {"attempt_id": attempt, "outcome": "password_rejected"}).json()
    assert late["login_failures"] == 0


def test_an_unreported_login_counts_as_a_failure_after_five_minutes(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    attempt = _begin(a)
    assert identities.sweep_unreported_logins(a.h.db.now, {}) == 0            # too early
    a.h.db.advance(protocol.LOGIN_RESULT_DEADLINE_SECONDS + 1)
    assert identities.sweep_unreported_logins(a.h.db.now, {}) == 1
    assert identities.sweep_unreported_logins(a.h.db.now, {}) == 0            # once
    att = _attempts(a, kind="browser")[0]
    assert (att["outcome"], att["counted_as_failure"]) == ("unreported", True)
    assert a.session_row()["login_failures"] == 1
    # A late report changes nothing.
    body = a.post("login/result", {"attempt_id": attempt, "outcome": "password_rejected"}).json()
    assert body["login_failures"] == 1


def test_the_sweeper_runs_the_unreported_step(monkeypatch):
    from app.routers import agent_worker_rfi  # noqa: F401 - registers the steps
    from app.services.browser_agents import queue

    a = Agent(monkeypatch)
    a.set_password()
    _begin(a)
    a.h.db.advance(protocol.LOGIN_RESULT_DEADLINE_SECONDS + 1)
    report = queue.sweep_once(a.h.db.now)
    assert report["unreported_logins"] == 1 and "contributions" in report


# ── The shared gate under the server-side sessions ────────────────────────────


def test_ensure_session_never_logs_in_right_after_an_agent_reserved(monkeypatch):
    a = Agent(monkeypatch)
    a.set_password()
    _begin(a)                                                     # the agent holds the slot
    session = pc.ProcoreSession(procore_creds.config(a.h.settings), rfp_harvest._SessionStore(a.h.settings),
                                transport=httpx.MockTransport(lambda r: pytest.fail("no login")),
                                now=lambda: a.h.db.now)
    with pytest.raises(pc.ProcoreUnavailable) as exc:
        session.ensure_session()
    assert "attempted recently" in str(exc.value) and not isinstance(exc.value, pc.ProcoreLoginLocked)
    session.close()
    # us02 sessions reserve on the shared row too.
    store = pm_rfi_sync.session_store("us02.procore.com", a.h.settings)
    assert store.reserve_login(600, "backend")["reason"] == "spacing"


def test_failures_from_two_stores_both_count(monkeypatch):
    a = Agent(monkeypatch)
    one, two = rfp_harvest._SessionStore(a.h.settings), rfp_harvest._SessionStore(a.h.settings)
    one.record_login(ok=False, error="password: a")
    state = two.record_login(ok=False, error="password: b")
    assert state["login_failures"] == 2 and state["last_login_attempt_at"]
    assert [c[0] for c in a.h.db.rpc_calls].count("procore_record_login_failure") == 2


def test_a_failed_server_login_names_its_step(monkeypatch):
    store = pc.MemorySessionStore()

    def handler(request):
        return httpx.Response(200, text="<html>Just a moment... cf-chl</html>",
                              headers={"content-type": "text/html", "cf-mitigated": "challenge"})

    session = pc.ProcoreSession(pc.ProcoreConfig("a@example.com", "pw", min_request_interval=0),
                                store, transport=httpx.MockTransport(handler))
    with pytest.raises(pc.ProcoreUnavailable) as exc:
        session.ensure_session()
    assert exc.value.step in ("challenge", "login_page")
    session.close()
