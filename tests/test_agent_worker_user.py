"""Dedicated personal worker HTTP contract with real bearer/boot/lease parsing."""

import asyncio
from pathlib import Path
from uuid import uuid4, UUID
from types import SimpleNamespace

import httpx
import pytest

from app.core import agent_auth
from app.routers import agent_worker_user as routes
from app.services import assistant_browser_runtime as runtime
from app.services.browser_agents import protocol
from tests.browser_agents_harness import Harness, API


@pytest.fixture
def env(monkeypatch):
    h = Harness(monkeypatch)
    worker, token, boot, job = h.active_with_job()
    worker["principal_kind"] = "user"
    job.update(
        principal_kind="user", principal_user_id=str(uuid4()), agent_type="user_browser_read"
    )

    def fence(lease, *a, **kw):
        if (
            lease.job_id != job["id"]
            or lease.epoch != job["lease_epoch"]
            or lease.worker.id != worker["id"]
            or lease.boot_id != boot
        ):
            raise agent_auth.agent_error(protocol.ErrorCode.LEASE_LOST)
        return job

    monkeypatch.setattr(runtime, "fenced", fence)
    headers = h.lease_headers(token, job["id"], job["lease_epoch"], boot)
    yield SimpleNamespace(
        h=h,
        worker=worker,
        token=token,
        boot=boot,
        job=job,
        headers=headers,
        path=f"{API}/jobs/{job['id']}",
    )
    h.client.close()


def test_org_worker_denied_before_private_runtime(env, monkeypatch):
    env.worker["principal_kind"] = "org"
    monkeypatch.setattr(runtime, "fenced", lambda *a: pytest.fail("private runtime entered"))
    response = env.h.client.post(env.path + "/user/steps", headers=env.headers, json={"steps": []})
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("mode", ["boot", "epoch", "job"])
def test_stale_identity_or_lease_cannot_release_credentials(env, monkeypatch, mode):
    monkeypatch.setattr(runtime, "identity_begin", lambda *a: pytest.fail("credential disclosure"))
    headers = dict(env.headers)
    if mode == "boot":
        headers["X-Agent-Boot"] = str(uuid4())
    elif mode == "epoch":
        headers["X-Agent-Lease"] = f"{env.job['id']}:999"
    else:
        headers["X-Agent-Lease"] = f"{uuid4()}:1"
    response = env.h.client.post(
        env.path + "/user-identities/procore/login/begin",
        headers=headers,
        json={"host": "app.procore.com", "origin": "https://login.procore.com"},
    )
    assert response.status_code in (400, 403, 409, 422)
    assert response.headers["cache-control"] == "no-store"


def test_invalid_payload_does_not_echo_private_input(env):
    response = env.h.client.post(
        env.path + "/user-identities/procore/login/begin",
        headers=env.headers,
        json={"host": "bad host PRIVATE_PASSWORD", "origin": "x", "password": "PRIVATE_PASSWORD"},
    )
    assert response.status_code == 422
    assert "PRIVATE_PASSWORD" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_credential_response_no_store_and_no_org_identity_call(env, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "identity_begin",
        lambda lease, body: {
            "granted": True,
            "attempt_id": str(uuid4()),
            "email": "owner@example.test",
            "password": "PRIVATE_PASSWORD",
        },
    )
    response = env.h.client.post(
        env.path + "/user-identities/procore/login/begin",
        headers=env.headers,
        json={"host": "app.procore.com", "origin": "https://login.procore.com"},
    )
    assert response.status_code == 200 and response.json()["password"] == "PRIVATE_PASSWORD"
    assert response.headers["cache-control"] == "no-store"
    denied = env.h.client.post(
        env.path + "/identities/procore/login/begin",
        headers=env.headers,
        json={"host": "app.procore.com"},
    )
    assert denied.status_code == 403


def test_private_heartbeat_does_not_renew_another_worker_lease(env, monkeypatch):
    other = env.h.job(
        "canary",
        principal_kind="user",
        lease_owner=str(uuid4()),
        lease_boot_id=env.boot,
        status="running",
    )
    monkeypatch.setattr(routes, "get_supabase", lambda: env.h.db)
    pings = []

    def ping(name, params):
        assert name == "assistant_browser_worker_ping"
        assert params == {"p_worker": env.worker["id"], "p_boot": env.boot}
        pings.append(params)
        return True

    monkeypatch.setattr(routes.auth, "rpc", ping)
    calls = []

    def rpc(owner, name, params):
        calls.append((owner, name, params))
        return {"ok": True, "job": env.job}

    monkeypatch.setattr(routes.repo, "browser_rpc", rpc)
    response = env.h.client.post(
        API + "/user/heartbeat",
        headers=env.h.auth(env.token, **{"X-Agent-Boot": env.boot}),
        json={
            "boot_id": env.boot,
            "config_version": 1,
            "slots_free": {"user_browser_read": 1},
            "jobs": [
                {"job_id": other["id"], "epoch": 1},
                {"job_id": env.job["id"], "epoch": env.job["lease_epoch"]},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["lost"] == [other["id"]]
    assert [item["job_id"] for item in response.json()["renewed"]] == [env.job["id"]]
    assert len(pings) == 1
    assert len(calls) == 1 and calls[0][0] == env.job["principal_user_id"]
    assert calls[0][1] == "assistant_browser_job_heartbeat"
    assert response.headers["cache-control"] == "no-store"


def test_worker_userclient_roundtrip_uuid_help_and_private_step_finish(env, monkeypatch):
    # The vendored worker speaks to the real dedicated routes; only repository
    # persistence and the server-owned model response are replaced here.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "agent_worker"))
    from bdr_agent.agents.user_browser.client import UserClient
    from bdr_agent.client import BdrClient, Lease, RetryPolicy
    from bdr_agent import protocol as worker_protocol

    saved = []
    help_id = str(uuid4())
    message_id = str(uuid4())
    attempt_id = str(uuid4())
    monkeypatch.setattr(runtime, "sanitize", lambda job, data, **kw: data)
    monkeypatch.setattr(
        routes.records,
        "append_record",
        lambda job, kind, payload, **kw: saved.append((kind, payload)) or {"id": str(uuid4())},
    )
    monkeypatch.setattr(
        runtime, "finish", lambda lease, status, summary, **kw: saved.append(("finish", summary))
    )
    monkeypatch.setattr(
        routes,
        "_help_state",
        lambda lease, hid: (
            env.job,
            {
                "id": help_id,
                "status": "open",
                "messages": [
                    {
                        "id": message_id,
                        "author_kind": "owner",
                        "action": "message",
                        "body": None,
                        "is_secret": True,
                        "consumed": False,
                        "created_at": "2026-10-27T00:00:00Z",
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(
        routes.help_flow,
        "consume_secret",
        lambda *a, **kw: saved.append(("secret_attempt", kw["login_attempt_id"])) or "123456",
    )

    def transport(request):
        response = env.h.client.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=dict(response.headers)
        )

    async def run():
        base = BdrClient(
            "https://testserver",
            token=env.token,
            boot_id=UUID(env.boot),
            transport=httpx.MockTransport(transport),
            retry=RetryPolicy(max_attempts=1),
        )
        client = UserClient(base)
        client.bind_host("app.procore.com")
        lease = Lease(UUID(env.job["id"]), env.job["lease_epoch"])
        step = await client.append_steps(
            lease,
            [{"idem_key": "s1", "kind": "note", "actor": "worker", "summary": "Owner observation"}],
        )
        assert step.appended == 1
        messages = await client.help_messages(lease, UUID(help_id))
        assert messages.messages[0].id == UUID(message_id)
        client.login_attempt_id = UUID(attempt_id)
        code = await client.help_secret_use(lease, UUID(help_id), UUID(message_id))
        assert code.value.get_secret_value() == "123456"
        await client.finish(
            lease,
            worker_protocol.FinishRequest(
                status="succeeded", outcome="nothing_to_do", summary="Owner result"
            ),
        )
        await base.aclose()

    asyncio.run(run())
    assert any(
        kind == "step" and payload.get("summary") == "Owner observation" for kind, payload in saved
    )
    assert ("secret_attempt", attempt_id) in saved and ("finish", "Owner result") in saved


def _help_open(env, monkeypatch, *, active=None, opened=None):
    calls = []
    monkeypatch.setattr(routes.repo, "browser_help_state",
                        lambda *a: calls.append("state") or active)
    monkeypatch.setattr(routes.help_flow, "open_help",
                        lambda job, **kw: calls.append(("open", kw["reason"], kw["login_attempt_id"]))
                        or opened or {"id": kw["help_id"], "status": "open"})
    return calls


@pytest.mark.parametrize("active", [None, {"id": str(uuid4()), "status": "open"}])
def test_personal_cap_help_is_suppressed_before_joining_open_help(env, monkeypatch, active):
    calls = _help_open(env, monkeypatch, active=active)
    response = env.h.client.post(env.path + "/user/help", headers=env.headers,
                                 json={"reason_code": "cap_reached", "message": "The job reached its cap."})
    assert response.status_code == 200, response.text
    assert response.json() == {"help_id": None, "joined_existing": False, "holder": False, "suppressed": True}
    assert calls == []


def test_login_scoped_and_other_help_still_join_or_open(env, monkeypatch):
    attempt = str(uuid4())
    calls = _help_open(env, monkeypatch)
    opened = env.h.client.post(env.path + "/user/help", headers=env.headers,
                               json={"reason_code": "mfa", "login_attempt_id": attempt})
    assert opened.status_code == 200 and opened.json()["suppressed"] is False
    assert calls == ["state", ("open", "mfa", attempt)]
    # A cap reason carrying a login attempt keeps the login-scoped path.
    calls.clear()
    scoped = env.h.client.post(env.path + "/user/help", headers=env.headers,
                               json={"reason_code": "cap_reached", "login_attempt_id": attempt})
    assert scoped.json()["suppressed"] is False and calls == ["state", ("open", "cap_reached", attempt)]
    active = {"id": str(uuid4()), "status": "open"}
    calls = _help_open(env, monkeypatch, active=active)
    joined = env.h.client.post(env.path + "/user/help", headers=env.headers,
                               json={"reason_code": "provider_config", "message": "Configuration"})
    assert joined.json() == {"help_id": active["id"], "joined_existing": True, "holder": True, "suppressed": False}
    assert calls == ["state"]


@pytest.mark.parametrize("reason", ["cap_reached", "daily_cap_reached"])
def test_running_worker_cap_stop_finishes_failed_without_park(env, monkeypatch, reason):
    # The released 1.0.4 loop against the real routes: a suppressed cap help
    # must end the job failed, never park it for an owner Retry.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "agent_worker"))
    from bdr_agent.agent.help import HelpDesk
    from bdr_agent.agent.loop import AgentLoop
    from bdr_agent.agents.user_browser.client import UserClient
    from bdr_agent.client import BdrClient, Lease, RetryPolicy
    from bdr_agent import protocol as worker_protocol
    from bdr_agent.slot import Finish

    calls = _help_open(env, monkeypatch, active={"id": str(uuid4()), "status": "open"})
    monkeypatch.setattr(routes.repo, "browser_rpc", lambda *a: pytest.fail("parked or requeued"))
    finished = []
    monkeypatch.setattr(runtime, "finish", lambda lease, status, summary, **kw: finished.append((status, summary)))

    def transport(request):
        response = env.h.client.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

    class Executor:
        async def screenshot(self, page=None, *, label="shot"):
            return None

    async def run():
        base = BdrClient("https://testserver", token=env.token, boot_id=UUID(env.boot),
                         transport=httpx.MockTransport(transport), retry=RetryPolicy(max_attempts=1))
        client = UserClient(base)
        lease = Lease(UUID(env.job["id"]), env.job["lease_epoch"])
        ctx = SimpleNamespace(client=client, lease=lease, steps=SimpleNamespace(log=lambda *a, **kw: None),
                              type_config=SimpleNamespace(max_steps=40))
        loop = AgentLoop(ctx, Executor(), HelpDesk(ctx))
        result = await loop._on_stop(worker_protocol.TurnStopResponse(stop=True, reason=reason))
        assert isinstance(result, Finish) and result.status == "failed"
        assert result.error_kind == "cannot_complete"
        await client.finish(lease, worker_protocol.FinishRequest(
            status="failed", error_kind=result.error_kind, summary=result.summary))
        await base.aclose()
        return result

    result = asyncio.run(run())
    assert calls == [] and finished == [("failed", result.summary)]
