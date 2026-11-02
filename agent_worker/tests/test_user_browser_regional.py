"""Explicit regional Procore support stays bound to one approved origin/project."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser.client import UserClient
from bdr_agent.agents.user_browser.guard import UserGuard
from bdr_agent.agents.user_browser.identity import PreparedLoginPage, UserIdentity
from bdr_agent.browser.executor import ToolError
from bdr_agent.client import BdrRejected, Lease, RetryPolicy
from bdr_agent.protocol import Cookie, LoginBeginRequest, LoginBeginResponse, LoginResultRequest, SessionRequest

HOST = "us02.procore.com"
PROJECT = "/webclients/host/companies/562949953430361/projects/562949955106559/tools/"
SEED = "https://" + HOST + PROJECT + "hubs/project-hub/views/overview"
RFI_QUERY = "?as_datetimes=true&locale=en&serializer_view=web_index&view=list&page=1&per_page=150"


def guard():
    firewall = UserGuard(ceiling_for("user_browser_read"))
    firewall.bind_seed(SEED)
    return firewall


def identity():
    base = NS(in_job_retry=RetryPolicy(max_attempts=1), call=AsyncMock())
    client = UserClient(base)
    context = NS(request=NS(get=AsyncMock()), cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock())
    jb = NS(context=context, request=context.request, login_page=None, guard=guard())
    ctx = NS(input=NS(url=SEED), client=client, steps=NS(log=Mock()), lease=Lease(uuid4(), 1))
    ex = NS(jb=jb, register_secret=Mock())
    return UserIdentity(ctx, ex, NS())


@pytest.mark.parametrize("url,allowed", [
    (SEED, True), (SEED + "/", True),
    ("https://" + HOST + PROJECT + "rfis/4", True),
    ("https://" + HOST + PROJECT + "projecthome", True),
    ("https://us02.procore.com/562949955106559/project/home", False),
    ("https://us02.procore.com/562949955106559/project/rfi/list", False),
    (SEED.replace("us02.", "app."), False),
    (SEED.replace("us02.", "us03."), False),
    (SEED.replace("companies/562949953430361", "companies/99"), False),
    (SEED.replace("projects/562949955106559", "projects/99"), False),
    (SEED.replace("views/overview", "views/settings"), False),
    (SEED.replace("project-hub", "company-hub"), False),
    (SEED + "/edit", False), (SEED + "?access_token=secret", False),
])
def test_regional_navigation_exact_host_company_project_and_overview(url, allowed):
    firewall = guard()
    assert firewall.ceiling.nav_hosts == (HOST,)
    assert bool(firewall.policy("GET", url, login=False, main=True)) is allowed
    assert not firewall.policy("POST", url, login=False, main=True)


@pytest.mark.parametrize("url,allowed", [
    ("https://us02.procore.com/assets/app.js", True),
    ("https://app.procore.com/assets/app.js", False),
    ("https://us03.procore.com/assets/app.js", False),
    ("https://login.procore.com/assets/app.js", False),
    ("https://us02.procore.com/rest/v1.0/projects/562949955106559/rfis" + RFI_QUERY, True),
    ("https://us02.procore.com/rest/v1.0/projects/562949955106559/rfis", False),   # 1.0.6: exact query only
    ("https://us02.procore.com/rest/v1.0/projects/99/rfis" + RFI_QUERY, False),
])
def test_subresources_cannot_cross_region_or_project(url, allowed):
    assert bool(guard().policy("GET", url, login=False, main=False, rtype="fetch")) is allowed
    assert not guard().policy("POST", url, login=False, main=False, rtype="fetch")


@pytest.mark.parametrize("destination,status,allowed", [
    ("https://us02.procore.com/auth/procore/callback?code=private", 302, True),
    ("https://app.procore.com/auth/procore/callback?code=private", 302, False),
    ("https://us03.procore.com/auth/procore/callback?code=private", 302, False),
    ("https://login.microsoftonline.com/oauth", 302, False),
    (SEED.replace("companies/562949953430361", "companies/99"), 302, False),
    ("https://us02.procore.com/562949955106559/project/home", 302, False),
    ("https://us02.procore.com/auth/procore/callback?code=private", 307, False),
])
async def test_login_redirect_is_intercepted_before_cross_region_request(destination, status, allowed):
    firewall = guard()
    page = NS(main_frame=None)
    frame = NS(page=page, parent_frame=None)
    page.main_frame = frame
    firewall.login_page = page
    response = NS(status=status, headers={"location": destination}, dispose=AsyncMock())
    route = NS(request=NS(method="POST", url="https://login.procore.com/sessions/submit_login_password",
                          frame=frame, is_navigation_request=lambda: True),
               fetch=AsyncMock(return_value=response), fulfill=AsyncMock(), abort=AsyncMock(), continue_=AsyncMock())
    await firewall._on_route(route)
    route.fetch.assert_awaited_once_with(max_redirects=0, timeout=30000)
    route.continue_.assert_not_called()
    if allowed:
        assert firewall.redirects[id(page)] == destination
        assert "location" not in route.fulfill.call_args.kwargs["headers"]
    else:
        route.abort.assert_awaited_once()
        route.fulfill.assert_not_called()
    response.dispose.assert_awaited_once()


async def test_identity_probes_the_unmodified_seed_region_without_redirects():
    value = identity()
    assert value.ctx.input.url == SEED and value.host == HOST and value.ctx.client.host == HOST
    response = NS(status=200, headers={}, body=AsyncMock(return_value=b"[]"), dispose=AsyncMock())
    value.ex.jb.context.request.get.return_value = response
    assert await value.probe()
    value.ex.jb.context.request.get.assert_awaited_once_with(
        "https://us02.procore.com/rest/v1.0/companies", max_redirects=0, timeout=15000)
    response.dispose.assert_awaited_once()


async def test_cookie_import_and_export_are_scoped_to_seed_region():
    value = identity()
    cookies = [Cookie(name="session", value="private", domain=domain) for domain in
               (HOST, "login.procore.com", ".procore.com", "procore.com")]
    await value._inject(cookies)
    assert [c["domain"] for c in value.ex.jb.context.add_cookies.call_args.args[0]] == [c.domain for c in cookies]
    for domain in ("app.procore.com", "us03.procore.com", "evilprocore.com"):
        value.ex.jb.context.add_cookies.reset_mock()
        with pytest.raises(ToolError, match="Cookie scope refused"):
            await value._inject([Cookie(name="session", value="private", domain=domain)])
        value.ex.jb.context.add_cookies.assert_not_called()
    with pytest.raises(ToolError):
        await value._inject([Cookie(name="session", value="private", domain=HOST, secure=False)])
    value.ex.jb.context.cookies.return_value = [c.model_dump() for c in cookies] + [
        Cookie(name="session", value="private", domain="app.procore.com").model_dump()]
    assert [c.domain for c in await value.export_cookies()] == [c.domain for c in cookies]


async def test_private_client_binds_session_begin_and_result_to_approved_region():
    value = identity()
    client, lease = value.ctx.client, value.ctx.lease
    await client.identity_session(lease, "procore", SessionRequest(host=HOST))
    assert client.base.call.call_args.args == ("user_identity_session",)
    assert client.base.call.call_args.kwargs["body"].origin == "https://us02.procore.com"
    client.login_preflight = AsyncMock(return_value="https://login.procore.com")
    attempt = uuid4()
    client.base.call.return_value = LoginBeginResponse(granted=True, attempt_id=attempt,
                                                      email="owner@example.test", password="private")
    await client.login_begin(lease, "procore", LoginBeginRequest(host=HOST))
    body = client.base.call.call_args.kwargs["body"]
    assert body.host == HOST and body.origin == "https://login.procore.com"
    for endpoint, body in (("identity_session", SessionRequest(host="app.procore.com")),
                           ("login_begin", LoginBeginRequest(host="app.procore.com"))):
        client.base.call.reset_mock()
        with pytest.raises(BdrRejected):
            await client.call(endpoint, lease=lease, path_params={"identity": "procore"}, body=body)
        client.base.call.assert_not_called()
    with pytest.raises(BdrRejected):
        client.bind_host("app.procore.com")
    success = LoginResultRequest(attempt_id=attempt, outcome="ok",
        cookies=[Cookie(name="session", value="private", domain=HOST)])
    client.login_origin = lambda: "https://app.procore.com"
    with pytest.raises(BdrRejected):
        await client.call("login_result", lease=lease, path_params={"identity": "procore"}, body=success)
    client.login_origin = lambda: "https://us02.procore.com"
    await client.call("login_result", lease=lease, path_params={"identity": "procore"}, body=success)
    assert client.base.call.call_args.kwargs["body"].origin == "https://us02.procore.com"


async def test_regional_login_entry_and_classifier_preserve_fixed_credential_origin():
    value = identity()
    page = NS(url="about:blank", is_closed=lambda: False)
    async def goto(url, **kwargs):
        assert url == "https://us02.procore.com/auth/procore"
        page.url = "https://login.procore.com/oauth/authorize"
    page.goto = AsyncMock(side_effect=goto)
    value.ex.jb.open_login_page = AsyncMock(return_value=page)
    value.ex.jb.guard.follow_redirects = AsyncMock()
    login = PreparedLoginPage(value.ex.jb, value.ex, HOST, probe=AsyncMock(return_value=True))
    login._await_field = AsyncMock(return_value=True)
    assert await login.open() is None
    login._assert_login_page("password")
    page.url = SEED
    assert (await login.classify("password")).outcome == "ok"
    with pytest.raises(Exception):
        login._assert_login_page("password")


async def test_a_refused_hop_after_login_submit_is_classified_not_tool_failed(monkeypatch, caplog):
    from bdr_agent.agents.pm_apps.procore.login import ProcoreLoginPage
    monkeypatch.setattr(ProcoreLoginPage, "_submit", AsyncMock())
    value = identity()
    login = PreparedLoginPage(value.ex.jb, value.ex, HOST, probe=AsyncMock(return_value=True))
    login.page = NS(url="about:blank", is_closed=lambda: False)
    callback = "https://us02.procore.com/auth/procore/callback?code=DECOY-CODE"
    value.ex.jb.guard.follow_redirects = AsyncMock(side_effect=ValueError(f"net::ERR_BLOCKED_BY_CLIENT {callback}"))
    caplog.set_level("DEBUG")
    await login._submit(object())                    # no raise: login() goes on to classify()
    assert "DECOY-CODE" not in caplog.text
    value.ex.jb.guard.follow_redirects = AsyncMock(side_effect=RuntimeError("Browser has been closed"))
    with pytest.raises(RuntimeError):
        await login._submit(object())
