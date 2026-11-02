"""Real local Chromium: personal login, reads and redirects against the TLS fixture."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from bdr_agent.agents import REGISTRY, ceiling_for
from bdr_agent.browser.browser import ChromiumProvider
from bdr_agent.client import BdrClient, RetryPolicy
from bdr_agent.heartbeat import JobTracker
from bdr_agent.protocol import ClaimedJob, TypeConfig
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Slot, SlotDeps
from bdr_agent.spool import Spool
from .agent_harness import needs_browser
from .procore_site import ProcoreSite, PROJECT_ID

pytestmark = needs_browser
SEED = f"https://app.procore.com/{PROJECT_ID}/project/rfi/list"


@pytest.fixture
def site():
    value=ProcoreSite().start()
    yield value
    value.stop()


def job():
    return ClaimedJob(job_id=uuid4(),agent_type="user_browser_read",principal_kind="user",principal_user_id=uuid4(),
        epoch=1,attempts=1,lease_expires_at=datetime.now(timezone.utc)+timedelta(minutes=3),
        input={"service":"procore","url":SEED,"objective":"Read the RFIs"})


async def test_real_chromium_never_sends_refused_redirect(site):
    provider=ChromiumProvider(ceilings=ceiling_for,proxy={"server":site.proxy_url},ignore_https_errors=True)
    await provider.start()
    try:
        jb=await provider.new_context(job())
        await jb.context.add_cookies(site.mint_session())
        with pytest.raises(Exception):
            await jb.page.goto(f"https://app.procore.com/{PROJECT_ID}/project/rfi/show/1",timeout=5000)
        assert not site.seen(host="evil.example")
        assert jb.guard.blocked
        await jb.close()
    finally:
        await provider.stop()


async def test_personal_runner_password_login_get_read_and_private_routes(site,paths):
    script=[("ensure_login",{"host":"app.procore.com"}), ("navigate",{"url":SEED}),
            ("read_page",{}),("finish",{"outcome":"nothing_to_do","summary":"Read the owner RFIs"})]
    calls=[]; results=[]
    attempt=uuid4()
    def transport(req):
        path=req.url.path
        body=json.loads(req.content) if req.headers.get("content-type","").startswith("application/json") else {}
        calls.append((path,body))
        if path.endswith('/session'):
            return httpx.Response(200,json={"status":"no_jar"})
        if path.endswith('/login/begin'):
            # The network fixture must already have served the exact login form.
            assert site.seen(host="login.procore.com")
            return httpx.Response(200,json={"granted":True,"attempt_id":str(attempt),
                 "email":site.state.email,"password":site.state.password})
        if path.endswith('/login/result'):
            results.append(body)
            return httpx.Response(200,json={"login_failures":0})
        if path.endswith('/turns'):
            tool,args=script.pop(0)
            n=body['prev_turn_no']+1
            return httpx.Response(200,json={"turn_no":n,"action":{"call_id":str(n),"tool":tool,"args":args},
                "caps":{"steps_left":40-n,"tokens_left":10000,"cost_left_usd":1}})
        if path.endswith('/steps'):
            return httpx.Response(200,json={"appended":len(body['steps']),"duplicates":0,"late":0})
        if path.endswith('/artifacts'):
            return httpx.Response(200,json={"artifact_id":str(uuid4()),"existed":False})
        if path.endswith('/help'):
            return httpx.Response(200,json={"suppressed":True})
        return httpx.Response(204)
    base=BdrClient("https://bdr.test",token="bdrw_private",boot_id=uuid4(),transport=httpx.MockTransport(transport),
                   retry=RetryPolicy(max_attempts=1),in_job_retry=RetryPolicy(max_attempts=1))
    provider=ChromiumProvider(ceilings=ceiling_for,proxy={"server":site.proxy_url},ignore_https_errors=True)
    await provider.start()
    try:
        deps=SlotDeps(client=base,tracker=JobTracker(),spool=Spool(paths.spool_dir),secrets=LiveSecrets(),
                      runners=REGISTRY,browser=provider,tick=.01,flush_interval=.1)
        result=await Slot("user_browser_read",1,deps).run(job(),TypeConfig(enabled=True,max_concurrency=1,
                            max_steps=20,max_active_seconds=90))
        assert result.terminal == "finish:succeeded", (result,calls)
        assert results and results[-1]['outcome']=='ok', calls
        assert results[-1]['origin']=='https://app.procore.com'
        assert all('/user/' in p or '/user-identities/' in p for p,_ in calls)
        assert not list(paths.spool_dir.rglob('*.jsonl'))
        posts=site.seen(method='POST')
        assert posts and all(p.host=='login.procore.com' and p.path in
            ('/sessions/submit_login_email','/sessions/submit_login_password') for p in posts)
        turns=[body for path,body in calls if path.endswith('/turns')]
        assert site.state.password not in json.dumps(turns)
        assert not site.seen(host='evil.example')
    finally:
        await base.aclose()
        await provider.stop()
