"""Guard and executor against real headless Chromium and the Procore fixture
(spec 10.2): read-only network, websockets, popups, redirects, navigation
cap, refs and hidden values, type_text refusals, masked screenshots, secret
scrubbing, the ledger row, and the login-page-only POST rule."""

import json

import pytest

from bdr_agent import protocol as P
from bdr_agent.browser.executor import MASK_SELECTORS, Executor
from bdr_agent.client import Lease
from bdr_agent.heartbeat import JobTracker
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import JobContext
from bdr_agent.spool import Spool, StepLog

from .agent_bdr import AgentBdr
from .agent_harness import SEED, claim, make_provider, needs_browser
from .procore_site import ProcoreSite

pytestmark = needs_browser

SHOW = "https://app.procore.com/3167322/project/rfi/show/27914290"


@pytest.fixture
def site():
    s = ProcoreSite().start()
    yield s
    s.stop()


@pytest.fixture
async def provider(site):
    p = await make_provider(site)
    yield p
    await p.stop()


@pytest.fixture
async def env(site, provider, paths):
    bdr = AgentBdr()
    ac, sc, job = await claim(bdr, "rfi_from_email", input=SEED)
    secrets = LiveSecrets()
    tracker = JobTracker()
    tracker.register(job.job_id, job.epoch, job.agent_type)
    steps = StepLog(Spool(paths.spool_dir).for_job(job.job_id, job.epoch), secrets)
    jb = await provider.new_context(job)
    jb.attach_steps(steps.log)
    ctx = JobContext(job=job, lease=Lease(job.job_id, job.epoch), input=P.parse_input(job.agent_type, job.input),
                     type_config=P.TypeConfig.model_validate(bdr.types["rfi_from_email"]), client=ac, steps=steps,
                     secrets=secrets, tracker=tracker, browser_context=jb, api_host="bdr.test")
    # Logged in as a stored jar would be.
    await jb.context.add_cookies([{k: v for k, v in c.items() if k != "expires"} for c in site.mint_session()])
    ex = Executor(ctx, jb)
    yield bdr, site, ctx, jb, ex, steps
    await jb.close()
    await steps.flush(ac)
    await ac.aclose()
    sc.close()


def call(tool: str, n: int = 1, **args) -> P.ToolCall:
    return P.ToolCall(call_id=f"c{n}", tool=tool, args=args)


async def run(ex, tool, n=1, **args):
    out = await ex.run_tool(call(tool, n, **args), turn_no=n)
    return out, json.loads(out.content)


async def test_page_writes_and_sockets_never_leave(env):
    bdr, site, ctx, jb, ex, steps = env
    out, res = await run(ex, "navigate", url=SHOW)
    assert out.ok and res["title"] == "RFI Const-198"
    page = jb.page
    # The page tries more writes from script: a fetch PUT and a form submit.
    await page.evaluate("fetch('/rest/v1.0/projects/3167322/rfis/27914290', {method: 'PUT', body: '{}'})"
                        ".catch(() => {})")
    await page.evaluate("fetch('https://login.procore.com/sessions/submit_login_password', "
                        "{method: 'POST', body: 'x'}).catch(() => {})")
    await page.wait_for_timeout(300)
    assert [r for r in site.state.requests if r.method not in ("GET", "HEAD", "OPTIONS")] == []
    assert jb.guard.websockets_closed >= 1
    assert not [r for r in site.state.requests if r.path == "/cable"]
    blocked = jb.guard.blocked
    assert ("app.procore.com", "POST") in blocked and ("app.procore.com", "PUT") in blocked
    assert ("login.procore.com", "POST") in blocked
    rows = [json.loads(x)["s"] for x in steps.spool.path.read_text().splitlines() if '"s"' in x]
    http_rows = [r for r in rows if r["kind"] == "http.blocked"]
    # One row per host and method, no query strings.
    assert len(http_rows) == len({(r["result"]["host"], r["result"]["method"]) for r in http_rows})
    assert all("?" not in r["result"]["path"] for r in http_rows)


async def test_form_submit_click_is_blocked(env):
    bdr, site, ctx, jb, ex, steps = env
    await run(ex, "navigate", url=SHOW)
    _, page = await run(ex, "read_page", 2)
    ref = next(line.split("]")[0].strip(" [") for line in page["text"].splitlines() if 'button "Respond"' in line)
    out, res = await run(ex, "click", 3, ref=ref)
    assert not [r for r in site.state.requests if r.method == "POST"]


async def test_redirect_off_ceiling_goes_blank(env):
    bdr, site, ctx, jb, ex, steps = env
    out, res = await run(ex, "navigate", url="https://app.procore.com/3167322/project/rfi/show/1")
    assert not out.ok and res["error"] == "navigation_blocked"
    assert res["blocked"][0]["host"] == "evil.example"
    assert jb.page.url == "about:blank"


async def test_popups_are_closed(env):
    bdr, site, ctx, jb, ex, steps = env
    await run(ex, "navigate", url=SHOW)
    _, page = await run(ex, "read_page", 2)
    ref = next(line.split("]")[0].strip(" [") for line in page["text"].splitlines() if "Open in new window" in line)
    await run(ex, "click", 3, ref=ref)
    await jb.page.wait_for_timeout(500)
    await jb.guard.settle()
    assert jb.guard.popups_closed == 1
    assert [p for p in jb.context.pages if not p.is_closed()] == [jb.page]


async def test_refs_hidden_values_and_type_text_rules(env):
    bdr, site, ctx, jb, ex, steps = env
    await run(ex, "navigate", url=SHOW)
    await jb.page.fill("#vc", "999111")
    _, page = await run(ex, "read_page", 2)
    lines = page["text"].splitlines()
    ref_of = {name: line.split("]")[0].strip(" [") for line in lines for name in ("Search", "Verification code",
                                                                                     "Response") if f'"{name}"' in line}
    assert "999111" not in page["text"]
    assert any('"Verification code" value="[hidden]"' in line for line in lines)
    out, res = await run(ex, "type_text", 3, ref=ref_of["Verification code"], text="123456")
    assert res == {"error": "refused", "detail": "this looks like a password or code field"}
    out, res = await run(ex, "type_text", 4, ref=ref_of["Search"], text="Const-198")
    assert out.ok and res["typed"] == 9
    assert await jb.page.input_value("#search") == "Const-198"
    out, res = await run(ex, "select_option", 5, ref=next(line.split("]")[0].strip(" [") for line in lines
                                                          if "combobox" in line), value="Closed")
    assert out.ok and await jb.page.input_value("#status") == "closed"
    # A navigation makes old refs stale.
    await run(ex, "navigate", 6, url="https://app.procore.com/3167322/project/rfi/list")
    out, res = await run(ex, "click", 7, ref=ref_of["Search"])
    assert res["error"] == "stale_ref"
    out, res = await run(ex, "click", 8, ref="ref_999")
    assert res["error"] == "stale_ref"
    out, res = await run(ex, "go_back", 9)
    assert out.ok and "rfi/show" in res["url"]
    out, res = await run(ex, "find_text", 10, query="EF3")
    assert res["count"] >= 1


async def test_screenshots_are_masked_jpegs(env, monkeypatch):
    bdr, site, ctx, jb, ex, steps = env
    await run(ex, "navigate", url=SHOW)
    seen_masks = []
    real = jb.page.screenshot

    async def spy(**kw):
        seen_masks.append(kw.get("mask"))
        return await real(**kw)

    monkeypatch.setattr(jb.page, "screenshot", spy)
    ex.mark_secret_field("#search")
    out, _ = await run(ex, "scroll", 2, direction="down", amount=1)
    assert out.screenshot_artifact_id is not None
    [masks] = seen_masks
    assert len(masks) == len(MASK_SELECTORS) + 1
    counts = [await m.count() for m in masks]
    assert sum(counts) >= 2   # the code field and the secret-filled search field
    art = bdr.artifacts[str(out.screenshot_artifact_id)]
    assert art["kind"] == "screenshot" and art["data"][:3] == b"\xff\xd8\xff"


async def test_secret_in_observation_fails_the_job(env):
    bdr, site, ctx, jb, ex, steps = env
    await run(ex, "navigate", url=SHOW)
    ex.register_secret("EF3 Circuit Clarification")
    out, res = await run(ex, "read_page", 2)
    assert res == {"error": "secret_leak_blocked"}
    assert out.terminal is not None and out.terminal.error_kind == "secret_leak_blocked"
    rows = [json.loads(x)["s"] for x in steps.spool.path.read_text().splitlines() if '"s"' in x]
    assert any(r["kind"] == "security.secret_scrubbed" for r in rows)
    assert "EF3 Circuit Clarification" not in steps.spool.path.read_text()


async def test_ledger_row_per_tool(env):
    bdr, site, ctx, jb, ex, steps = env
    await run(ex, "navigate", url="https://app.procore.com/3167322/project/rfi/list")
    rows = [json.loads(x)["s"] for x in steps.spool.path.read_text().splitlines() if '"s"' in x]
    [row] = [r for r in rows if r["kind"] == "tool.navigate"]
    assert row["actor"] == "model" and row["tool_name"] == "navigate" and row["turn_no"] == 1
    assert row["url_before"] == "about:blank"
    assert row["url_after"] == "https://app.procore.com/3167322/project/rfi/list"
    assert row["duration_ms"] >= 0 and row["screenshot_artifact_id"]
    assert row["ok"] is True


async def test_navigation_cap(env):
    bdr, site, ctx, jb, ex, steps = env
    jb.guard.max_navigations = 2
    await run(ex, "navigate", 1, url=SHOW)
    await run(ex, "navigate", 2, url="https://app.procore.com/3167322/project/rfi/list")
    out, res = await run(ex, "navigate", 3, url=SHOW)
    assert res["error"] == "navigation_cap"


async def test_spa_url_rewrites_do_not_count_or_get_blanked(env):
    """pushState/replaceState (SPA tabs, filters, subpaths) are not page loads:
    no cap, no path/query re-check; only the origin is re-checked."""
    bdr, site, ctx, jb, ex, steps = env
    jb.guard.max_navigations = 2
    out, _res = await run(ex, "navigate", 1, url=SHOW)
    assert out.ok and jb.guard.nav_count == 1
    page = jb.page
    await page.evaluate("""() => {
        for (let i = 0; i < 30; i++) {
            history.pushState({}, '', location.pathname + '/general?filters[cost_code_id]=' + i);
            history.replaceState({}, '', location.pathname.replace(/\\/general.*$/, '') + '#tab' + i);
        }
    }""")
    out, res = await run(ex, "read_page", 2)
    assert out.ok and "error" not in res
    assert jb.guard.nav_count == 1 and not jb.guard.violations
    assert page.url.startswith("https://app.procore.com/")
    # A real page load still counts and gets the full check.
    await run(ex, "navigate", 3, url="https://app.procore.com/3167322/project/rfi/list")
    assert jb.guard.nav_count == 2


async def test_scrub_only_value_in_observation_does_not_fail_the_job(env):
    bdr, site, ctx, jb, ex, steps = env
    await run(ex, "navigate", url=SHOW)
    ex.register_secret("EF3 Circuit Clarification", tripwire=False)   # e.g. the login email
    out, res = await run(ex, "read_page", 2)
    assert out.ok and out.terminal is None and "error" not in res
    assert "EF3 Circuit Clarification" not in steps.spool.path.read_text()


async def test_login_post_only_from_the_login_page(env):
    bdr, site, ctx, jb, ex, steps = env
    login = await jb.open_login_page()
    await login.goto("https://app.procore.com/auth/procore")
    assert login.url.startswith("https://login.procore.com/")
    await login.fill('input[name="session[email]"]', site.state.email)
    async with login.expect_navigation():
        await login.press('input[name="session[email]"]', "Enter")
    posts = site.seen(method="POST")
    assert [(r.host, r.path) for r in posts] == [("login.procore.com", "/sessions/submit_login_email")]
    # The same POST from the model page is refused.
    await run(ex, "navigate", url=SHOW)
    await jb.page.evaluate("fetch('https://login.procore.com/sessions/submit_login_email', "
                           "{method: 'POST', body: 'x'}).catch(() => {})")
    await jb.page.wait_for_timeout(200)
    assert len(site.seen(method="POST")) == 1
    await jb.close_login_page()
    # Once released, that page object is just another page: its POSTs are blocked too.
    assert jb.guard.login_page is None


async def test_type_text_refused_on_login_host(env):
    bdr, site, ctx, jb, ex, steps = env
    await jb.model_page()
    jb.guard.ceiling = jb.guard.ceiling.__class__(**{**jb.guard.ceiling.__dict__,
                                                     "nav_hosts": ("app.procore.com", "login.procore.com"),
                                                     "nav_paths": (*jb.guard.ceiling.nav_paths,
                                                                   *jb.guard.ceiling.login_paths)})
    await jb.page.goto("https://login.procore.com/")
    _, page = await run(ex, "read_page", 1)
    ref = next(line.split("]")[0].strip(" [") for line in page["text"].splitlines() if "textbox" in line)
    out, res = await run(ex, "type_text", 2, ref=ref, text="someone@example.com")
    assert res == {"error": "refused", "detail": "typing on a login host is not allowed"}


async def test_clearing_procore_cookies_clears_every_procore_domain(env):
    """A rejected stored jar must not leak stale cookies into the fresh jar."""
    from bdr_agent.agents.pm_apps.procore.identity import ProcoreIdentity

    bdr, site, ctx, jb, ex, steps = env
    await jb.context.add_cookies([
        {"name": "__cf_bm", "value": "stale-cf-value", "domain": ".procore.com", "path": "/"},
        {"name": "old", "value": "stale-app-value", "domain": "app.procore.com", "path": "/"},
        {"name": "lh", "value": "stale-login-value", "domain": "login.procore.com", "path": "/"},
        {"name": "keep", "value": "other-site", "domain": "example.com", "path": "/"},
    ])
    ident = ProcoreIdentity(ctx, ex, None, host="app.procore.com")
    await ident._clear_procore_cookies()
    left = {(c["name"], c["domain"]) for c in await jb.context.cookies()}
    assert left == {("keep", "example.com")}
    assert await ident.export_cookies() == []
