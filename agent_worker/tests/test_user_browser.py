"""Personal worker transport boundaries without credentials or external network."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from uuid import uuid4
from unittest.mock import AsyncMock

import httpx
import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser.client import UserClient
from bdr_agent.agents.user_browser.guard import UserGuard
from bdr_agent.agents.user_browser.identity import UserIdentity
from bdr_agent.agents.user_browser.spool import MemorySpool, UserStepLog
from bdr_agent.agents.user_browser.help import UserHelpDesk
from bdr_agent.client import BdrClient, BdrRejected, Lease, RetryPolicy
from bdr_agent.heartbeat import JobTracker, HeartbeatThread
from bdr_agent.protocol import (
    ClaimedJob, TypeConfig, LoginBeginRequest, LoginBeginResponse, SessionRequest,
    HelpMessagesResponse, UserHelpMessagesResponse, HeartbeatResponse,
)
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Slot, SlotDeps, Finish
from bdr_agent.spool import Spool

SEED = "https://app.procore.com/123/project/rfi/list"


def guard():
    value = UserGuard(ceiling_for("user_browser_read"))
    value.bind_seed(SEED)
    return value


@pytest.mark.parametrize("method,url,login,main,allowed", [
    ("GET", SEED, False, True, True),
    ("GET", "https://app.procore.com/124/project/rfi/list", False, True, False),
    ("GET", "https://app.procore.com/123/project/rfi/show/4/edit", False, True, False),
    ("GET", "https://us02.procore.com/123/project/rfi/list", False, True, False),
    ("GET", "https://app.procore.com:443/123/project/rfi/list", False, True, False),
    ("GET", "https://app.procore.com@evil.test/123/project/rfi/list", False, True, False),
    ("GET", "https://app.procore.com/123/project/rfi/list?access_token=secret", False, True, False),
    ("GET", "https://app.procore.com/rest/v1.0/projects/123/rfis/2", False, False, True),
    ("GET", "https://app.procore.com/rest/v1.0/projects/124/rfis/2", False, False, False),
    ("POST", "https://app.procore.com/rest/v1.0/projects/123/rfis", False, False, False),
    ("HEAD", SEED, False, True, False),
    ("GET", "https://login.microsoftonline.com/common/oauth2/authorize", True, True, False),
    ("GET", "https://cdn.procore.com/assets/app.js", False, False, False),
    ("GET", "https://app.procore.com/assets/app.js", False, False, True),
    ("GET", "https://app.procore.com/assets/%2e%2e/account/delete.js", False, False, False),
    ("POST", "https://login.procore.com/sessions/submit_login_password", True, True, True),
    ("POST", "https://login.procore.com/sessions/submit_login_password", False, True, False),
    ("POST", "https://login.procore.com/account/delete", True, False, False),
])
def test_personal_ceiling(method, url, login, main, allowed):
    assert bool(guard().policy(method, url, login=login, main=main)) is allowed


@pytest.mark.parametrize("destination,allowed", [
    ("https://attacker.test/steal", False),
    ("https://login.microsoftonline.com/", False),
    ("https://app.procore.com/124/project/rfi/list", False),
    ("https://app.procore.com/123/project/rfi/show/4", True),
])
async def test_redirect_never_automatically_follows_or_delivers_location(destination, allowed):
    firewall = guard()
    page = NS(main_frame=None)
    frame = NS(page=page, parent_frame=None)
    page.main_frame = frame
    firewall.model_pages.add(id(page))
    response = NS(status=302, headers={"location": destination, "set-cookie": "a=b; Secure"},
                  dispose=AsyncMock())
    request = NS(method="GET", url=SEED, frame=frame, is_navigation_request=lambda: True)
    route = NS(request=request, fetch=AsyncMock(return_value=response),
               fulfill=AsyncMock(), abort=AsyncMock(), continue_=AsyncMock())
    await firewall._on_route(route)
    route.fetch.assert_awaited_once_with(max_redirects=0, timeout=30000)
    route.continue_.assert_not_called()
    response.dispose.assert_awaited_once()
    if allowed:
        route.fulfill.assert_awaited_once()
        assert "location" not in route.fulfill.call_args.kwargs["headers"]
        assert firewall.redirects[id(page)] == destination
    else:
        route.abort.assert_awaited_once()
        route.fulfill.assert_not_called()
        assert not firewall.redirects


async def test_refused_request_aborts_before_any_fetch():
    firewall = guard()
    page = NS(main_frame=None)
    frame = NS(page=page, parent_frame=None)
    page.main_frame = frame
    firewall.model_pages.add(id(page))
    route = NS(request=NS(method="POST",url=SEED,frame=frame,is_navigation_request=lambda:True),
               fetch=AsyncMock(), abort=AsyncMock())
    await firewall._on_route(route)
    route.fetch.assert_not_called()
    route.abort.assert_awaited_once()


async def test_credentials_require_preflight_and_uuid_attempt_binds_secret():
    attempt = uuid4()
    base = NS(in_job_retry=RetryPolicy(max_attempts=1), call=AsyncMock(return_value=LoginBeginResponse(
        granted=True, attempt_id=attempt,email="owner@example.test",password="private")))
    client = UserClient(base)
    client.bind_host("app.procore.com")
    lease = Lease(uuid4(), 2)
    with pytest.raises(BdrRejected):
        await client.login_begin(lease,"procore",LoginBeginRequest(host="app.procore.com"))
    base.call.assert_not_called()
    client.login_preflight = AsyncMock(return_value="https://login.procore.com")
    await client.login_begin(lease,"procore",LoginBeginRequest(host="app.procore.com"))
    assert client.login_attempt_id == attempt
    assert base.call.call_args.args == ("user_login_begin",)
    assert base.call.call_args.kwargs["body"].origin == "https://login.procore.com"
    await client.help_secret_use(lease,uuid4(),uuid4())
    assert base.call.call_args.args == ("user_help_secret_use",)
    assert base.call.call_args.kwargs["body"].login_attempt_id == attempt
    with pytest.raises(BdrRejected):
        await client.call("rfi_capture",lease=lease)
    with pytest.raises(BdrRejected):
        await client.identity_session(lease,"microsoft",SessionRequest(host="app.procore.com"))


async def test_probe_disables_redirects_and_disposes_response():
    response = NS(status=302, dispose=AsyncMock())
    identity = object.__new__(UserIdentity)
    identity.host = "app.procore.com"
    request = NS(get=AsyncMock(return_value=response))
    identity.ex = NS(jb=NS(context=NS(request=request)))
    assert await identity.probe() is False
    request.get.assert_awaited_once_with("https://app.procore.com/rest/v1.0/companies",max_redirects=0,timeout=15000)
    response.dispose.assert_awaited_once()


def test_memory_spool_never_opens_file_and_caps_pending(monkeypatch):
    spool = MemorySpool().for_job(uuid4(),1)
    def no_file(*a,**kw):
        raise AssertionError("personal spool tried disk")
    monkeypatch.setattr("builtins.open",no_file)
    spool.append([{"idem_key":"one","summary":"private"}])
    assert spool.unacked()[0]["summary"] == "private"
    spool.ack(["one"])
    assert spool.unacked() == []
    spool.mark_done()
    assert spool.remove_if_settled()
    with pytest.raises(RuntimeError,match="memory cap"):
        spool.append([{"idem_key":"large","summary":"x" * (8*1024*1024)}])


async def test_personal_slot_uses_only_user_routes_and_no_disk_spool(paths):
    calls=[]
    def transport(req):
        calls.append(req.url.path)
        if req.url.path.endswith('/steps'):
            return httpx.Response(200,json={"appended":1,"duplicates":0,"late":0})
        return httpx.Response(204)
    base=BdrClient("https://bdr.test",token="bdrw_private",boot_id=uuid4(),
                   transport=httpx.MockTransport(transport),retry=RetryPolicy(max_attempts=1))
    class Runner:
        needs_browser=False
        async def run(self,ctx):
            ctx.steps.log("note","Private project finding")
            return Finish("succeeded","Private task result",outcome="nothing_to_do")
    job=ClaimedJob(job_id=uuid4(),agent_type="user_browser_read",principal_kind="user",
        principal_user_id=uuid4(),epoch=1,attempts=1,lease_expires_at=datetime.now(timezone.utc)+timedelta(minutes=3),
        input={"service":"procore","url":SEED,"objective":"Summarize the RFIs"},checkpoint={})
    deps=SlotDeps(client=base,tracker=JobTracker(),spool=Spool(paths.spool_dir),secrets=LiveSecrets(),
                  runners={"user_browser_read":Runner()},tick=.01,flush_interval=.01)
    tc=TypeConfig(enabled=True,max_concurrency=1,max_steps=10,max_active_seconds=60)
    result=await Slot("user_browser_read",1,deps).run(job,tc)
    assert result.terminal == "finish:succeeded"
    assert calls and all("/user/" in path for path in calls)
    assert not list(paths.spool_dir.rglob("*.jsonl"))
    await base.aclose()


def test_user_heartbeat_and_orphans_never_use_org_routes():
    from .test_heartbeat import Sink
    calls=[]
    response=HeartbeatResponse(config_version=1,orphans=[{"job_id":uuid4(),"epoch":2}])
    client=NS(call=lambda name,**kw:(calls.append(name) or response))
    thread=HeartbeatThread(client,JobTracker(),Sink(),boot_id=uuid4(),config_version=lambda:1,
                           slots_free=lambda:{"user_browser_read":1},principal_kind=lambda:"user")
    thread.beat_once();thread.beat_once()
    assert calls == ["user_heartbeat","user_heartbeat","user_requeue"]


def test_projecthome_redirect_stays_in_project_and_company():
    modern='https://app.procore.com/webclients/host/companies/5/projects/123/tools/projecthome'
    firewall=guard()
    assert firewall.policy('GET',modern,login=False,main=True)
    assert not firewall.policy('GET',modern.replace('projects/123','projects/124'),login=False,main=True)
    firewall.bind_seed(modern)
    assert firewall.policy('GET',modern.replace('projecthome','rfis/2'),login=False,main=True)
    assert not firewall.policy('GET',modern.replace('companies/5','companies/6'),login=False,main=True)
    assert not firewall.policy('GET',modern+'/edit',login=False,main=True)
