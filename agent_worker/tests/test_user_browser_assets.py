"""Observed Procore assets use a separate cookie jar; navigation stays fenced."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser.guard import (
    APP_ASSET_HOSTS,
    APP_HOSTS,
    LOGIN_ASSET_HOSTS,
    MAX_MAIN_DIAGNOSTICS,
    MAX_RESPONSE_BYTES,
    PAGES_HOSTS,
    PAGES_SHELL_HOSTS,
    UserGuard,
    origin,
    transport_error_category,
)
from bdr_agent.browser.browser import ChromiumProvider

SEED = "https://us02.procore.com/webclients/host/companies/5/projects/123/tools/hubs/project-hub/views/overview"


def guard():
    value = UserGuard(ceiling_for("user_browser_read"))
    value.bind_seed(SEED)
    value.log_step = Mock()
    return value


def route_for(firewall, url=SEED, *, login=False, main=True, method="GET", response=None, rt=None):
    page = NS(main_frame=None)
    frame = NS(page=page, parent_frame=None)
    page.main_frame = frame
    if login:
        firewall.login_page = page
    else:
        firewall.model_pages.add(id(page))
    request = NS(method=method, url=url, frame=frame, is_navigation_request=lambda: main)
    if rt is not None:
        request.resource_type = rt
    return NS(request=request,
              fetch=AsyncMock(return_value=response), fulfill=AsyncMock(), abort=AsyncMock(), continue_=AsyncMock())


@pytest.mark.parametrize("host", sorted(APP_ASSET_HOSTS | LOGIN_ASSET_HOSTS))
def test_exact_asset_hosts_allow_only_static_get_on_the_correct_owned_page(host):
    firewall = guard()
    url = f"https://{host}/assets/app-123.js"
    assert firewall.policy("GET", url, login=True, main=False)
    assert bool(firewall.policy("GET", url, login=False, main=False)) == (host in APP_ASSET_HOSTS)
    for main in (False, True):
        for method in ("POST", "PUT", "DELETE", "HEAD", "OPTIONS"):
            assert not firewall.policy(method, url, login=True, main=main)
    assert not firewall.policy("GET", url, login=True, main=True)
    for extra in ("?token=secret", "?v=1", "#private"):
        assert not firewall.policy("GET", url + extra, login=True, main=False)
    assert not firewall.policy("GET", f"https://{host}/rest/v1.0/companies", login=True, main=False)
    with pytest.raises(ValueError):
        origin(url)  # Asset hosts never become identity origins.


@pytest.mark.parametrize("url,allowed", [
    ("https://assets1.procore.com/mfe_erb/abc012345/static/js/main.123.js", True),
    ("https://assets3.procore.com/mfe_erb/abc012345/static/js/123.chunk.js", True),
    ("https://assets2.procore.com/assets/style.123.css", True),
    ("https://assets4.procore.com/assets/app.js", False),
    ("https://assets1.procore.com.evil.test/assets/app.js", False),
    ("https://production-login-assets4.cdn.procore.com/assets/app.js", False),
    ("https://another.cdn.procore.com/assets/app.js", False),
    ("https://production-login-assets0.cdn.procore.com/mfe_erb/abc/static/js/app.js", False),
    ("https://assets1.procore.com/mfe_erb/not-hex/static/js/app.js", False),
    ("https://assets1.procore.com/mfe_erb/abc/static/js/app.js/../evil.js", False),
    ("https://assets1.procore.com/assets/%2e%2e/app.js", False),
    ("https://assets1.procore.com:443/assets/app.js", False),
    ("https://owner:private@assets1.procore.com/assets/app.js", False),
    ("http://assets1.procore.com/assets/app.js", False),
    ("https://js-agent.newrelic.com/assets/app.js", False),
])
def test_asset_path_and_host_ceiling(url, allowed):
    assert bool(guard().policy("GET", url, login=True, main=False)) is allowed


async def test_cdn_request_never_uses_authenticated_transport_or_returns_cookies():
    firewall = guard()
    url = "https://assets1.procore.com/mfe_erb/abc012345/static/js/main.123.js"
    response = NS(status=200, headers={"set-cookie": "private=discard; Domain=.procore.com",
                  "content-type": "application/javascript", "refresh": "0;url=https://evil.test",
                  "location": "https://evil.test"}, body=AsyncMock(return_value=b"/* asset */"), dispose=AsyncMock())
    firewall.asset_request = NS(get=AsyncMock(return_value=response))
    route = route_for(firewall, url, main=False)
    await firewall._on_route(route)
    firewall.asset_request.get.assert_awaited_once_with(url, max_redirects=0, timeout=30000,
        headers={"Cookie": "", "Authorization": "", "Referer": ""})
    route.fetch.assert_not_called()
    route.continue_.assert_not_called()
    assert route.fulfill.call_args.kwargs["headers"] == {"content-type": "application/javascript"}
    response.dispose.assert_awaited_once()


async def test_cdn_redirect_aborts_without_fetching_another_host():
    firewall = guard()
    url = "https://assets1.procore.com/assets/app.js"
    response = NS(status=302, headers={"location": SEED}, dispose=AsyncMock())
    firewall.asset_request = NS(get=AsyncMock(return_value=response))
    route = route_for(firewall, url, main=False)
    await firewall._on_route(route)
    assert firewall.asset_request.get.await_count == 1
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_called()
    assert not firewall.redirects


async def test_cdn_child_frame_document_is_not_a_static_subresource():
    firewall = guard()
    firewall.asset_request = NS(get=AsyncMock())
    route = route_for(firewall, "https://assets1.procore.com/assets/app.js", main=False)
    route.request.frame.parent_frame = NS()
    route.request.is_navigation_request = lambda: True
    await firewall._on_route(route)
    route.abort.assert_awaited_once()
    firewall.asset_request.get.assert_not_called()
    route.fetch.assert_not_called()


def diagnostics(firewall):
    return [call.kwargs["result"] for call in firewall.log_step.call_args_list if call.args[0] == "nav.failed"]


@pytest.mark.parametrize("primary,expected", [
    ("Route.fetch: Parse Error: Header overflow", "header_overflow"),
    ("APIRequestContext.get: Parse Error: Header overflow", "header_overflow"),
    ("APIRequestContext._inner_fetch: Parse Error: Header overflow", "header_overflow"),
    ("ROUTE.FETCH: PARSE ERROR: HEADER OVERFLOW", "header_overflow"),
    ("Parse Error: Invalid header value char", "invalid_header"),
    ("Route.fetch: Parse Error: Invalid header token", "invalid_header"),
    ('Route.fetch: Invalid character in header content ["cookie"]', "invalid_header"),
    ("Route.fetch: Request timed out after 30000ms", "timeout"),
    ("Route.fetch: Timeout 30000ms exceeded.", "timeout"),
    ("Route.fetch: certificate has expired", "tls"),
    ("Route.fetch: unable to verify the first certificate", "tls"),
    ("Route.fetch: self-signed certificate in certificate chain", "tls"),
    ("Route.fetch: write EPROTO synthetic", "tls"),
    ("Route.fetch: socket hang up", "connection"),
    ("Route.fetch: read ECONNRESET", "connection"),
    ("Route.fetch: getaddrinfo ENOTFOUND fixture.invalid", "connection"),
    ("Route.fetch: failed to decompress 'gzip' encoding: incorrect header check", "decompression"),
    ("Route.fetch: Parse Error: Invalid character in chunk size", "parse"),
    ("Route.fetch: unexpected failure", "other"),
    ("Route.fetch: x-response-header: synthetic", "other"),
    ("Route.fetch: a cookie named certificate has expired", "other"),
    ("Route.fetch: " + "x" * 600 + " Parse Error: Header overflow", "other"),
])
def test_transport_category_uses_primary_error_and_ignores_all_call_log_header_decoys(primary, expected):
    decoys = ("\nCall log:\n  - access-control-allow-headers: X-Test\n"
              "  - x-debug: Parse Error: Header overflow\n"
              "  - cookie: private-cookie\n  - authorization: private-bearer\n"
              "  - x-note: TLS certificate timed out socket connection header overflow\n")
    error = RuntimeError(primary + decoys)
    assert transport_error_category(error) == expected
    firewall = guard()
    firewall._main_failure("GET", SEED, login=False, stage="fetch", reason="transport_failed", error=error)
    [entry] = diagnostics(firewall)
    assert entry["error_category"] == expected
    persisted = json.dumps([(call.args, call.kwargs) for call in firewall.log_step.call_args_list])
    assert "private" not in persisted and "Call log" not in persisted
    assert "access-control" not in persisted and primary not in persisted


def test_timeout_exception_class_still_maps_without_reading_its_call_log():
    assert transport_error_category(TimeoutError("\nCall log:\nPrivate headers")) == "timeout"


@pytest.mark.parametrize("stage", ["fetch", "read_body", "fulfill"])
async def test_main_failure_survives_prior_resource_dedup_and_never_logs_exception_text(stage):
    firewall = guard()
    firewall._record_block("GET", "https://us02.procore.com/cdn-cgi/jsd", "path_not_allowed")
    response = NS(status=200, headers={}, body=AsyncMock(return_value=b"body"), dispose=AsyncMock())
    route = route_for(firewall, SEED + "?filter=private-query", response=response)
    error = RuntimeError("Route.fetch: certificate has expired\nCall log:\n"
                         "Cookie: private-session; https://site.test/?token=private")
    {"fetch": route.fetch, "read_body": response.body, "fulfill": route.fulfill}[stage].side_effect = error
    await firewall._on_route(route)
    [entry] = diagnostics(firewall)
    assert entry["stage"] == stage and entry["error_category"] == "tls"
    assert entry["reason"] == "transport_failed" and entry["path"] == SEED.removeprefix("https://us02.procore.com")
    assert "private" not in json.dumps(entry)
    route.abort.assert_awaited_once()


@pytest.mark.parametrize("declared", [True, False])
async def test_oversize_main_response_is_diagnosed_and_never_fulfilled(declared):
    firewall = guard()
    headers = {"content-length": str(MAX_RESPONSE_BYTES + 1)} if declared else {}
    response = NS(status=200, headers=headers, body=AsyncMock(return_value=b"x" * (MAX_RESPONSE_BYTES + 1)), dispose=AsyncMock())
    route = route_for(firewall, response=response)
    await firewall._on_route(route)
    [entry] = diagnostics(firewall)
    assert entry["reason"] == "response_too_large" and entry["bytes"] == MAX_RESPONSE_BYTES + 1
    assert entry["stage"] == ("headers" if declared else "read_body")
    if declared:
        response.body.assert_not_called()
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_called()


async def test_main_diagnostics_are_bounded_and_queries_are_omitted():
    firewall = guard()
    for _ in range(MAX_MAIN_DIAGNOSTICS + 3):
        await firewall._on_route(route_for(firewall, SEED + "?access_token=private"))
    assert len(diagnostics(firewall)) == MAX_MAIN_DIAGNOSTICS
    assert "private" not in json.dumps(diagnostics(firewall))


def test_rejected_destination_and_exception_names_cannot_leak_path_secrets():
    firewall = guard()
    unusual_error = type("private_secret_exception", (Exception,), {})
    for url in ("https://us02.procore.com/private-path-token",
                "https://private-host-token.example/private-path-token?code=private-query"):
        firewall._main_failure("GET", url, login=False, stage="redirect", reason="redirect_refused",
                               error=unusual_error("private-body"))
    entries = diagnostics(firewall)
    assert all(row["path"] == "[refused_path]" and row["error_type"] == "Exception" for row in entries)
    assert "private" not in json.dumps(entries)


@pytest.mark.parametrize("redirect", [False, True])
async def test_all_personal_failure_logs_redact_unapproved_hosts_paths_and_queries(redirect):
    firewall = guard()
    for url in ("https://us02.procore.com/private-path-token?code=private-query",
                "https://private-host-token.example/private-path-token?code=private-query"):
        if redirect:
            response = NS(status=302, headers={"location": url}, dispose=AsyncMock())
            route = route_for(firewall, response=response)
        else:
            route = route_for(firewall, url)
        await firewall._on_route(route)
    logged = [(call.args, call.kwargs) for call in firewall.log_step.call_args_list]
    assert any(args[0] == "http.blocked" for args, _ in logged)
    assert any(args[0] == "nav.failed" for args, _ in logged)
    assert "private" not in json.dumps(logged)


@pytest.mark.parametrize("failure", [None, "setup", "settle", "setup_cancel", "settle_cancel"])
async def test_isolated_asset_context_lifetime_and_no_browser_storage_state(failure):
    provider = ChromiumProvider(ceilings=ceiling_for)
    page = NS(on=Mock())
    context = NS(set_default_timeout=Mock(), set_default_navigation_timeout=Mock(), on=Mock(),
        route=AsyncMock(), route_web_socket=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())
    asset_request = NS(dispose=AsyncMock())
    provider._ensure_browser = AsyncMock(return_value=NS(new_context=AsyncMock(return_value=context)))
    provider._pw = NS(request=NS(new_context=AsyncMock(return_value=asset_request)))
    job = NS(principal_kind="user", principal_user_id=uuid4(), agent_type="user_browser_read", input={"url": SEED})
    error = asyncio.CancelledError if failure and failure.endswith("_cancel") else RuntimeError
    if failure in ("setup", "setup_cancel"):
        context.new_page.side_effect = error("page setup failed")
        with pytest.raises(error):
            await provider.new_context(job)
    else:
        jb = await provider.new_context(job)
        assert jb.guard.asset_request is asset_request
        if failure in ("settle", "settle_cancel"):
            jb.guard.settle = AsyncMock(side_effect=error("settle failed"))
            with pytest.raises(error):
                await jb.close()
        else:
            await jb.close()
        await jb.close()
    provider._pw.request.new_context.assert_awaited_once_with(ignore_https_errors=False)
    context.close.assert_awaited_once()
    asset_request.dispose.assert_awaited_once()


# Modern hub shell: exact *.pages.procore.com hosts, model page only ---------------------

SHELL = "https://ui-service-shell.pages.procore.com/static/js/index.bundle.33ab5944.js"
RESOURCE_TYPES = ("document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch",
                  "eventsource", "websocket", "manifest", "other", None)


PAGES_STATIC_CASES = [("/static/js/index.bundle.33ab5944.js", "script"),
                      ("/static/css/12.88906fbb.chunk.css", "stylesheet"),
                      ("/static/media/logo.1a2b.svg", "image"),
                      ("/static/media/font.9f.woff2", "font")]


# /static/media stays a 1.0.5 shell allowance; the 1.0.6 remotes get js and css only.
@pytest.mark.parametrize("host,path,rt", [(h, p, t) for h in sorted(PAGES_HOSTS) for p, t in PAGES_STATIC_CASES
                                          if h in PAGES_SHELL_HOSTS or not p.startswith("/static/media/")])
def test_pages_hosts_allow_static_get_on_the_model_page_only(host, path, rt):
    firewall = guard()
    url = f"https://{host}{path}"
    assert firewall.policy("GET", url, login=False, main=False, rtype=rt)
    for other in RESOURCE_TYPES:
        if other != rt:
            assert firewall.policy("GET", url, login=False, main=False, rtype=other).reason == \
                "resource_type_not_allowed"
    assert not firewall.policy("GET", url, login=True, main=False, rtype=rt)
    assert not firewall.policy("GET", url, login=False, main=True, rtype=rt)
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        assert not firewall.policy(method, url, login=False, main=False, rtype=rt)
    for extra in ("?v=1", "?", "#frag", "?token=secret"):
        assert not firewall.policy("GET", url + extra, login=False, main=False, rtype=rt)
    with pytest.raises(ValueError):
        origin(url)


@pytest.mark.parametrize("url", [
    "https://ui-service-shell.pages.procore.com/remoteEntry.js",
    "https://ui-service-shell.pages.procore.com/mf-manifest.json",
    "https://ui-service-shell.pages.procore.com/static/js/mf-manifest.json",
    "https://ui-service-shell.pages.procore.com/static/js/sub/app.js",
    "https://ui-service-shell.pages.procore.com/static/js/app.mjs",
    "https://ui-service-shell.pages.procore.com/static/media/photo.jpg",
    "https://ui-service-shell.pages.procore.com/static/fonts/a.woff2",
    "https://ui-service-shell.pages.procore.com/assets/app.js",
    "https://ui-service-shell.pages.procore.com/static/js/" + "a" * 201 + ".js",
    "https://ui-service-shell.pages.procore.com/static/js/a~b.js",
    "https://ui-service-shell.pages.procore.com/static/js/%2e%2e.js",
    "https://ui-service-shell.pages.procore.com/static/js/..app.js",
    "https://ui-service-shell.pages.procore.com:443/static/js/app.js",
    "https://owner:private@ui-service-shell.pages.procore.com/static/js/app.js",
    "http://ui-service-shell.pages.procore.com/static/js/app.js",
    "https://other-ui-service.pages.procore.com/static/js/app.js",
    "https://pages.procore.com/static/js/app.js",
    "https://ui-service-shell.pages.procore.com.evil.test/static/js/app.js",
    "https://document-viewer-ui-service.pages.dev/static/js/app.js",
])
def test_pages_paths_hosts_and_shapes_outside_the_allowance_are_refused(url):
    for rt in RESOURCE_TYPES:
        assert not guard().policy("GET", url, login=False, main=False, rtype=rt)


def test_pages_hosts_never_become_seeds_nav_hosts_or_app_hosts():
    firewall = guard()
    assert not PAGES_HOSTS & APP_HOSTS
    assert not PAGES_HOSTS & set(firewall.ceiling.nav_hosts) and not PAGES_HOSTS & set(firewall.ceiling.resource_hosts)
    for host in PAGES_HOSTS:
        with pytest.raises(ValueError):
            UserGuard(ceiling_for("user_browser_read")).bind_seed(
                f"https://{host}/webclients/host/companies/5/projects/123/tools/projecthome")
    with pytest.raises(ValueError):
        firewall.bind_seed(SHELL)


async def test_pages_bundle_uses_isolated_transport_with_empty_credentials_and_no_cookies():
    firewall = guard()
    response = NS(status=200, headers={"set-cookie": "private=discard; Domain=.procore.com",
                  "content-type": "application/javascript", "access-control-allow-origin": "*"},
                  body=AsyncMock(return_value=b"/* shell */"), dispose=AsyncMock())
    firewall.asset_request = NS(get=AsyncMock(return_value=response))
    route = route_for(firewall, SHELL, main=False, rt="script")
    await firewall._on_route(route)
    firewall.asset_request.get.assert_awaited_once_with(SHELL, max_redirects=0, timeout=30000,
        headers={"Cookie": "", "Authorization": "", "Referer": ""})
    route.fetch.assert_not_called()
    route.continue_.assert_not_called()
    assert route.fulfill.call_args.kwargs["headers"] == {"content-type": "application/javascript",
                                                         "access-control-allow-origin": "*"}
    assert route.fulfill.call_args.kwargs["body"] == b"/* shell */"


@pytest.mark.parametrize("declared", [True, False])
async def test_pages_bundle_keeps_the_size_cap(declared):
    firewall = guard()
    headers = {"content-length": str(MAX_RESPONSE_BYTES + 1)} if declared else {}
    response = NS(status=200, headers=headers, body=AsyncMock(return_value=b"x" * (MAX_RESPONSE_BYTES + 1)),
                  dispose=AsyncMock())
    firewall.asset_request = NS(get=AsyncMock(return_value=response))
    route = route_for(firewall, SHELL, main=False, rt="script")
    await firewall._on_route(route)
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_called()


@pytest.mark.parametrize("case", ["login", "main", "iframe", "post", "query", "unowned", "redirect", "rtype"])
async def test_pages_bundle_refusals_never_fetch(case):
    firewall = guard()
    response = NS(status=302, headers={"location": SEED}, dispose=AsyncMock())
    firewall.asset_request = NS(get=AsyncMock(return_value=response))
    url = SHELL + ("?v=1" if case == "query" else "")
    route = route_for(firewall, url, login=case == "login", main=case == "main",
                      method="POST" if case == "post" else "GET", rt="xhr" if case == "rtype" else "script")
    if case == "iframe":
        route.request.frame.parent_frame = NS()
        route.request.is_navigation_request = lambda: True
    if case == "unowned":
        firewall.model_pages.clear()
    await firewall._on_route(route)
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_called()
    route.fetch.assert_not_called()
    if case == "redirect":
        assert firewall.asset_request.get.await_count == 1 and not firewall.redirects
    else:
        firewall.asset_request.get.assert_not_called()


async def test_pages_redirect_is_never_followed_even_to_an_allowed_page():
    firewall = guard()
    response = NS(status=302, headers={"location": SEED}, dispose=AsyncMock())
    firewall.asset_request = NS(get=AsyncMock(return_value=response))
    route = route_for(firewall, SHELL, main=False, rt="script")
    await firewall._on_route(route)
    assert firewall.asset_request.get.await_count == 1
    route.abort.assert_awaited_once()
    assert not firewall.redirects


def blocked_rows(firewall):
    return [call.kwargs["result"] for call in firewall.log_step.call_args_list if call.args[0] == "http.blocked"]


def test_record_block_names_known_pages_hosts_and_labels_every_other_pages_host():
    firewall = guard()
    firewall._record_block("GET", SHELL + "?token=private-query", "asset_query_not_allowed")
    firewall._record_block("GET", "https://header-ui-service.pages.procore.com/private-root.js", "path_not_allowed")
    firewall._record_block("GET", "https://private-label-token.pages.procore.com/static/js/a.js", "origin_not_allowed")
    firewall._record_block("GET", "https://a.private-two.pages.procore.com/remoteEntry.js", "origin_not_allowed")
    firewall._record_block("POST", "https://other.pages.procore.com/static/js/a.js?x=private", "origin_not_allowed")
    rows = blocked_rows(firewall)
    assert rows[0] == {"method": "GET", "host": "ui-service-shell.pages.procore.com",
                       "path": "/static/js/index.bundle.33ab5944.js", "reason": "asset_query_not_allowed"}
    assert rows[1]["host"] == "header-ui-service.pages.procore.com" and rows[1]["path"] == "[refused_path]"
    assert rows[2] == {"method": "GET", "host": "[procore_pages_other]", "path": "[refused_path]",
                       "reason": "origin_not_allowed"}
    # Every other pages subdomain shares one fixed label, so the second GET dedups.
    assert len(rows) == 4 and rows[3]["method"] == "POST" and rows[3]["host"] == "[procore_pages_other]"
    assert firewall.blocked[("[procore_pages_other]", "GET")].count == 2
    persisted = json.dumps([(call.args, call.kwargs) for call in firewall.log_step.call_args_list])
    assert "private" not in persisted and "?" not in persisted


def test_record_block_still_masks_existing_hosts_as_before():
    firewall = guard()
    firewall._record_block("GET", "https://assets1.procore.com/assets/app.js?v=private", "asset_query_not_allowed")
    firewall._record_block("GET", "https://us02.procore.com/private-path", "path_not_allowed")
    firewall._record_block("GET", "https://private-host.example/x", "origin_not_allowed")
    rows = blocked_rows(firewall)
    assert [(r["host"], r["path"]) for r in rows] == [
        ("assets1.procore.com", "/assets/app.js"), ("us02.procore.com", "[refused_path]"),
        ("[unapproved_origin]", "[refused_path]")]
