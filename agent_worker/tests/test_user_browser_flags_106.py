"""Worker 1.0.6, owner decision 2026-10-29: the RFIs tool's LaunchDarkly flag
GET is answered on this PC with a fixed empty flag set and nothing is ever sent
to LaunchDarkly. Exactly one shape is stubbed (modern seed model page, not a
navigation, GET, fetch or xhr, https://app.launchdarkly.com/sdk/evalx/<24 hex>/
contexts/<base64url>, no port, userinfo, query or fragment, from a frame on the
seed app host). Everything else to LaunchDarkly stays refused, and no step ever
carries the URL, client-side id or context (the context holds the user's email)."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser.diagnostics import VERDICTS
from bdr_agent.agents.user_browser.guard import (
    FLAGS_EVALX_URL,
    FLAGS_HOST,
    FLAGS_STUB_BODY,
    FLAGS_STUB_VERDICT,
    UserGuard,
)
from bdr_agent.browser.guard import Guard

from .agent_harness import needs_browser
from .procore_site import ProcoreSite

C, P = "562949953430361", "562949955106559"
APP = "https://us02.procore.com"
RFIS_PATH = f"/webclients/host/companies/{C}/projects/{P}/tools/rfis"
RFIS_TOOL = APP + RFIS_PATH
HUB = f"{APP}/webclients/host/companies/{C}/projects/{P}/tools/hubs/project-hub/views/overview"
LEGACY = f"https://app.procore.com/{P}/project/rfi/list"
CLIENT_ID = "65131ec649bac512b599b608"     # rfis-ui-service production LAUNCH_DARKLY_APP_ID shape
# A base64url context whose text carries decoys: it must never reach any step.
CONTEXT = "decoyEmailSecret_ZGVjb3lAZzMuZXhhbXBsZQ-" + "A" * 40
EVALX = f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/{CONTEXT}"
STUB_HEADERS = {"content-type": "application/json", "access-control-allow-origin": APP,
                "cache-control": "no-store"}
OTHER_METHODS = ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "REPORT", "CONNECT", "TRACE")
OTHER_TYPES = ("document", "stylesheet", "image", "media", "font", "script", "texttrack", "eventsource",
               "websocket", "manifest", "other", None)


def guard(seed=RFIS_TOOL):
    value = UserGuard(ceiling_for("user_browser_read"))
    value.bind_seed(seed)
    value.log_step = Mock()
    value.asset_request = NS(get=AsyncMock())
    return value


def flag_route(firewall, url=EVALX, *, rt="xhr", method="GET", login=False, owned=True, nav=False,
               iframe=False, frame_url=RFIS_TOOL):
    page = NS(main_frame=None)
    frame = NS(page=page, parent_frame=NS() if iframe else None, url=frame_url)
    page.main_frame = frame
    if login:
        firewall.login_page = page
    elif owned:
        firewall.model_pages.add(id(page))
    request = NS(method=method, url=url, frame=frame, resource_type=rt, headers={},
                 is_navigation_request=lambda: nav, failure="net::ERR_BLOCKED_BY_CLIENT")
    return NS(request=request, fetch=AsyncMock(), fulfill=AsyncMock(), abort=AsyncMock(), continue_=AsyncMock())


def assert_no_network(firewall, route):
    route.fetch.assert_not_called()
    route.continue_.assert_not_called()
    firewall.asset_request.get.assert_not_called()


def assert_refused(firewall, route):
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.fulfill.assert_not_called()
    assert_no_network(firewall, route)


def logged(firewall):
    return json.dumps([(c.args, c.kwargs) for c in firewall.log_step.call_args_list])


# The one stubbed shape -------------------------------------------------------------------


@pytest.mark.parametrize("rt", ["xhr", "fetch"])
async def test_exact_flag_get_is_answered_locally_with_an_empty_flag_set(rt):
    firewall = guard()
    route = flag_route(firewall, rt=rt)
    await firewall._on_route(route)
    route.fulfill.assert_awaited_once_with(status=200, headers=STUB_HEADERS, body="{}")
    assert FLAGS_STUB_BODY == "{}" and json.loads(FLAGS_STUB_BODY) == {}
    route.abort.assert_not_called()
    assert_no_network(firewall, route)
    # Not a block: no http.blocked or nav.failed step at all.
    assert not firewall.log_step.called


def test_the_stub_shape_matches_the_rfis_bundle():
    # 94676@1527695: [baseUrl, "/sdk/evalx/", clientSideId, "/contexts/", base64url(context)], no query.
    assert FLAGS_EVALX_URL.fullmatch(EVALX)
    assert FLAGS_EVALX_URL.fullmatch(f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/{'a' * 8192}")
    assert not FLAGS_EVALX_URL.fullmatch(f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/{'a' * 8193}")


async def test_the_hub_seed_also_gets_the_stub_and_the_policy_still_refuses_launchdarkly():
    firewall = guard(HUB)
    route = flag_route(firewall, frame_url=HUB)
    await firewall._on_route(route)
    route.fulfill.assert_awaited_once()
    for main in (False, True):
        for login in (False, True):
            assert not firewall.policy("GET", EVALX, login=login, main=main, rtype="xhr")
    assert not firewall.approved_path(FLAGS_HOST, EVALX.split(FLAGS_HOST, 1)[1])


# Refusals: page, navigation, method, type, seed --------------------------------------------


@pytest.mark.parametrize("case", ["login", "unowned", "main_nav", "iframe_nav", "legacy", "nav_raises",
                                  "frame_raises", "frame_other_origin", "frame_pages_host", "frame_blank",
                                  "frame_http", "frame_port", "frame_userinfo"])
async def test_flag_get_outside_the_model_page_is_refused_without_network(case):
    firewall = guard(LEGACY if case == "legacy" else RFIS_TOOL)
    frame_url = {"frame_other_origin": "https://app.procore.com/x",
                 "frame_pages_host": "https://rfis-ui-service.pages.procore.com/static/js/a.js",
                 "frame_blank": "about:blank", "frame_http": "http://us02.procore.com" + RFIS_PATH,
                 "frame_port": "https://us02.procore.com:8443" + RFIS_PATH,
                 "frame_userinfo": "https://u:p@us02.procore.com" + RFIS_PATH}.get(case, RFIS_TOOL)
    route = flag_route(firewall, login=case == "login", owned=case != "unowned",
                       nav=case in ("main_nav", "iframe_nav"), iframe=case == "iframe_nav", frame_url=frame_url)
    if case == "nav_raises":
        route.request.is_navigation_request = Mock(side_effect=RuntimeError("worker"))
    if case == "frame_raises":
        route.request.frame = NS(page=route.request.frame.page, parent_frame=None)   # no url attribute
        firewall.model_pages.add(id(route.request.frame.page))
    await firewall._on_route(route)
    assert_refused(firewall, route)


@pytest.mark.parametrize("method", OTHER_METHODS)
async def test_other_methods_including_report_and_the_events_post_are_refused(method):
    firewall = guard()
    for url in (EVALX, f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/context",
                f"https://events.launchdarkly.com/events/bulk/{CLIENT_ID}"):
        route = flag_route(firewall, url, method=method)
        await firewall._on_route(route)
        assert_refused(firewall, route)


@pytest.mark.parametrize("rt", OTHER_TYPES)
async def test_other_resource_types_are_refused(rt):
    firewall = guard()
    route = flag_route(firewall, rt=rt)
    await firewall._on_route(route)
    assert_refused(firewall, route)


@pytest.mark.parametrize("url", [
    EVALX + "?withReasons=true",
    EVALX + "?h=abc",
    EVALX + "?",
    EVALX + "#frag",
    EVALX + "/",
    EVALX + "/extra",
    EVALX.replace("https://", "http://"),
    EVALX.replace(FLAGS_HOST, FLAGS_HOST + ":443"),
    EVALX.replace(FLAGS_HOST, "u:p@" + FLAGS_HOST),
    EVALX.replace(FLAGS_HOST, "APP.launchdarkly.com"),
    EVALX.replace(FLAGS_HOST, FLAGS_HOST + "."),
    EVALX.replace(FLAGS_HOST, FLAGS_HOST + ".evil.test"),
    EVALX.replace(FLAGS_HOST, "x." + FLAGS_HOST),
    EVALX.replace(FLAGS_HOST, "clientstream.launchdarkly.com"),
    EVALX.replace(FLAGS_HOST, "events.launchdarkly.com"),
    EVALX.replace(FLAGS_HOST, "mobile.launchdarkly.com"),
    EVALX.replace(FLAGS_HOST, "clientsdk.launchdarkly.com"),
    EVALX.replace(FLAGS_HOST, "clientsdk.launchdarkly.us"),
    EVALX.replace(FLAGS_HOST, "app.launchdarkly.us"),
    f"https://clientstream.launchdarkly.com/eval/{CLIENT_ID}/{CONTEXT}",
    f"https://clientstream.launchdarkly.com/eval/{CLIENT_ID}",
    f"https://events.launchdarkly.com/events/bulk/{CLIENT_ID}",
    f"https://events.launchdarkly.com/events/diagnostic/{CLIENT_ID}",
    f"https://{FLAGS_HOST}/sdk/goals/{CLIENT_ID}",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/context",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/users/{CONTEXT}",
    f"https://{FLAGS_HOST}/sdk/eval/{CLIENT_ID}/contexts/{CONTEXT}",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID.upper()}/contexts/{CONTEXT}",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID[:-1]}/contexts/{CONTEXT}",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}0/contexts/{CONTEXT}",
    f"https://{FLAGS_HOST}/sdk/evalx/decoy-client-id-0123456/contexts/{CONTEXT}",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/{CONTEXT}==",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/{CONTEXT}%2F",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/a+b",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/a.b",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/../../x",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/{'a' * 8193}",
    f"https://{FLAGS_HOST}//sdk/evalx/{CLIENT_ID}/contexts/{CONTEXT}",
    f"https://{FLAGS_HOST}/SDK/evalx/{CLIENT_ID}/contexts/{CONTEXT}",
    f" https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/{CONTEXT}",
    f"https://{FLAGS_HOST}/sdk/evalx/{CLIENT_ID}/contexts/{CONTEXT}\n",
])
async def test_every_other_launchdarkly_url_stays_refused(url):
    firewall = guard()
    for rt in ("xhr", "fetch", "eventsource"):
        route = flag_route(firewall, url, rt=rt)
        await firewall._on_route(route)
        assert_refused(firewall, route)


async def test_a_failed_fulfil_aborts_and_still_never_fetches():
    firewall = guard()
    route = flag_route(firewall)
    route.fulfill = AsyncMock(side_effect=RuntimeError("Target page, context or browser has been closed"))
    await firewall._on_route(route)
    route.abort.assert_awaited_once_with("blockedbyclient")
    assert_no_network(firewall, route)


async def test_the_organization_guard_never_stubs():
    org = Guard(ceiling_for("rfi_from_email"))
    assert not hasattr(org, "flags_stub_origin")
    page = NS(main_frame=None)
    frame = NS(page=page, parent_frame=None, url=RFIS_TOOL)
    route = NS(request=NS(method="GET", url=EVALX, frame=frame, is_navigation_request=lambda: False),
               fulfill=AsyncMock(), continue_=AsyncMock(), abort=AsyncMock())
    await org._on_route(route)
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_called()
    route.continue_.assert_not_called()


# Census and steps: fixed verdict, host class only, never the path --------------------------


async def test_census_records_the_fixed_verdict_and_no_step_carries_the_url():
    assert FLAGS_STUB_VERDICT in VERDICTS
    firewall = guard()
    stubbed = [flag_route(firewall, rt=rt) for rt in ("xhr", "xhr", "fetch")]
    refused = [flag_route(firewall, f"https://events.launchdarkly.com/events/bulk/{CLIENT_ID}", method="POST"),
               flag_route(firewall, EVALX + "?withReasons=true&decoy=decoy-q"),
               flag_route(firewall, f"https://clientstream.launchdarkly.com/eval/{CLIENT_ID}/{CONTEXT}",
                          rt="eventsource")]
    for route in stubbed + refused:
        await firewall._on_route(route)
    for route in stubbed:
        firewall.diagnostics.request_finished(route.request)
    for route in refused:
        firewall.diagnostics.request_failed(route.request)
    await firewall.diagnostics.emit(NS(url=RFIS_TOOL, is_closed=lambda: True), "final")
    [census] = [c.kwargs["result"] for c in firewall.log_step.call_args_list
                if c.args[0] == "browser.resource_census"]
    rows = [r for r in census["rows"] if r["hc"] == "launchdarkly"]
    stub_rows = sorted((r for r in rows if r["v"] == FLAGS_STUB_VERDICT), key=lambda r: r["rt"])
    assert [(r["rt"], r["n"], r["du"]) for r in stub_rows] == [("fetch", 1, 1), ("xhr", 2, 1)]
    for row in stub_rows:
        assert (row["m"], row["sc"], row["ct"], row["ac"], row["co"], row["q"], row["ep"]) == \
            ("GET", "2xx", "json", "app_origin", "finished", "none", "-")
        assert row["pf"] in ("other", "root_other")   # a fixed family enum, never the path
    assert {r["v"] for r in rows if r["v"] != FLAGS_STUB_VERDICT} == {"refused:origin_not_allowed"}
    blocked = [c.kwargs["result"] for c in firewall.log_step.call_args_list if c.args[0] == "http.blocked"]
    assert blocked and all(b["host"] == "[unapproved_origin]" and b["path"] == "[refused_path]" for b in blocked)
    persisted = logged(firewall)
    for secret in ("decoy", CONTEXT, CLIENT_ID, "launchdarkly.com", "/sdk/", "evalx", "contexts", "ZGVjb3"):
        assert secret not in persisted


# Real local Chromium -------------------------------------------------------------------


LIVE_SEED = "https://us02.procore.com/webclients/host/companies/5/projects/123/tools/rfis"
LIVE_PATH = "/webclients/host/companies/5/projects/123/tools/rfis"
# Mirrors the SDK (XHR GET, content-type check, JSON.parse) and also uses fetch().
# The page renders the classic list only when both reads return {} as JSON; the
# events POST, a query variant and a stream URL must each be refused.
LIVE_PAGE = """<div id="root"></div><script>
const root = document.getElementById("root");
const ctx = btoa(JSON.stringify({kind: "multi", user: {kind: "user", key: "decoy-email@g3.example",
  company_id: 5, project_id: 123}})).replace(/=/g, "").replace(/\\+/g, "-").replace(/\\//g, "_");
window.__ctx = ctx;
const base = "https://app.launchdarkly.com/sdk/evalx/CLIENT_ID/contexts/" + ctx;
function viaXhr(url) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("GET", url);
    x.onload = () => resolve({status: x.status, ct: x.getResponseHeader("content-type"), body: x.responseText});
    x.onerror = () => reject(new Error("xhr"));
    x.send();
  });
}
const viaFetch = (url) => fetch(url).then(async (r) => ({status: r.status, ct: r.headers.get("content-type"),
  body: await r.text()}));
const settle = (p) => p.then(() => "sent", () => "refused");
Promise.all([viaFetch(base), viaXhr(base)]).then(([f, x]) => {
  window.__flags = {f, x};
  const ok = [f, x].every((r) => r.status === 200 && r.ct.startsWith("application/json")
    && JSON.stringify(JSON.parse(r.body)) === "{}");
  root.textContent = ok ? "RFIs list ready" : "flags wrong";
}, () => { root.textContent = "flags failed"; });
Promise.all([
  settle(fetch("https://events.launchdarkly.com/events/bulk/CLIENT_ID", {method: "POST", body: "[]"})),
  settle(fetch(base + "?withReasons=true")),
  settle(fetch("https://clientstream.launchdarkly.com/eval/CLIENT_ID/" + ctx)),
]).then((r) => { window.__others = r; });
</script>""".replace("CLIENT_ID", CLIENT_ID)


class FlagsSite(ProcoreSite):
    def respond(self, seen):
        if seen.host == "us02.procore.com" and seen.path == LIVE_PATH:
            return self._html(200, "Procore", LIVE_PAGE)
        if seen.host.endswith("launchdarkly.com"):
            # Reached only if the guard leaked: a decoy flag set the page would show.
            return 200, [("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")], \
                b'{"decoy-flag": {"value": true}}'
        return super().respond(seen)


@pytest.fixture
def site():
    value = FlagsSite().start()
    yield value
    value.stop()


def live_job():
    from bdr_agent.protocol import ClaimedJob
    return ClaimedJob(job_id=uuid4(), agent_type="user_browser_read", principal_kind="user",
                      principal_user_id=uuid4(), epoch=1, attempts=1,
                      lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
                      input={"service": "procore", "url": LIVE_SEED, "objective": "Read the RFIs"})


@needs_browser
async def test_real_chromium_page_gets_empty_flags_locally_and_renders_while_events_post_is_refused(site):
    from bdr_agent.browser.browser import ChromiumProvider
    provider = ChromiumProvider(ceilings=ceiling_for, proxy={"server": site.proxy_url}, ignore_https_errors=True)
    await provider.start()
    jb = await provider.new_context(live_job())
    logged_steps = []
    jb.attach_steps(lambda kind, summary, **fields: logged_steps.append((kind, summary, fields)))
    try:
        await jb.page.goto(LIVE_SEED, wait_until="load", timeout=15_000)
        await jb.page.wait_for_function("() => window.__flags && window.__others", timeout=10_000)
        await asyncio.sleep(0.3)   # requestfinished / requestfailed events
        assert await jb.page.evaluate('() => document.getElementById("root").textContent') == "RFIs list ready"
        flags = await jb.page.evaluate("() => window.__flags")
        for read in flags.values():
            assert read == {"status": 200, "ct": "application/json", "body": "{}"}
        assert await jb.page.evaluate("() => window.__others") == ["refused", "refused", "refused"]
        ctx = await jb.page.evaluate("() => window.__ctx")
        # Nothing reached the network: the proxy saw no LaunchDarkly request at all.
        assert not [r for r in site.seen() if "launchdarkly" in r.host]
        await jb.guard.diagnostics.emit(jb.page, "navigate")
        [census] = [f["result"] for kind, _s, f in logged_steps if kind == "browser.resource_census"]
        rows = [r for r in census["rows"] if r["hc"] == "launchdarkly"]
        stub = sorted((r for r in rows if r["v"] == FLAGS_STUB_VERDICT), key=lambda r: r["rt"])
        assert [(r["rt"], r["n"], r["co"], r["ac"]) for r in stub] == \
            [("fetch", 1, "finished", "app_origin"), ("xhr", 1, "finished", "app_origin")]
        post = [r for r in rows if r["m"] == "POST"]
        assert post and all(r["v"] == "refused:origin_not_allowed" and r["co"].startswith("failed:") for r in post)
        persisted = json.dumps(logged_steps)
        for secret in ("decoy", ctx, CLIENT_ID, "launchdarkly.com", "evalx", "g3.example"):
            assert secret not in persisted
    finally:
        await jb.close()
        await provider.stop()
