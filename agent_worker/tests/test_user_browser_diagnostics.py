"""Personal render diagnostics: enum-only census rows, console counts and a
numbers-only render probe, all bounded. Nothing raw enters a step."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser import diagnostics as D
from bdr_agent.agents.user_browser.diagnostics import RenderDiagnostics, readable, sanitize_probe
from bdr_agent.agents.user_browser.guard import UserGuard
from bdr_agent.browser.guard import Guard
from bdr_agent.protocol import worker_step_kind_allowed

SEED = "https://us02.procore.com/webclients/host/companies/5/projects/123/tools/hubs/project-hub/views/overview"
SHELL = "https://ui-service-shell.pages.procore.com/static/js/index.bundle.33ab5944.js"
BLANK = {"ready": 2, "text_len": 0, "element_count": 9, "visible_boxes": 0, "body": True,
         "root_children": {"root": 0, "app": -1, "main": -1, "single_spa": 0}}
RENDERED = {**BLANK, "text_len": 812, "element_count": 400, "visible_boxes": 120,
            "root_children": {"root": 1, "app": -1, "main": -1, "single_spa": 0}}


def guard():
    value = UserGuard(ceiling_for("user_browser_read"))
    value.bind_seed(SEED)
    value.log_step = Mock()
    return value


def model_page(firewall, *, url=SEED, probe=None, closed=False):
    page = NS(main_frame=None, url=url, is_closed=lambda: closed,
              evaluate=AsyncMock(return_value=probe if probe is not None else BLANK))
    page.main_frame = NS(page=page, parent_frame=None)
    firewall.model_pages.add(id(page))
    return page


def request(page, url, *, method="GET", rt="script", main=False, origin=None, failure=None):
    headers = {"origin": origin} if origin else {}
    return NS(method=method, url=url, frame=page.main_frame, resource_type=rt, headers=headers,
              is_navigation_request=lambda: main, failure=failure)


def route(req, response=None):
    return NS(request=req, fetch=AsyncMock(return_value=response), fulfill=AsyncMock(), abort=AsyncMock(),
              continue_=AsyncMock())


def response(status=200, headers=None, body=b"/* js */"):
    return NS(status=status, headers=headers or {}, body=AsyncMock(return_value=body), dispose=AsyncMock())


def steps(firewall, kind=None):
    return [(call.args, call.kwargs) for call in firewall.log_step.call_args_list
            if kind is None or call.args[0] == kind]


def results(firewall, kind):
    return [kwargs["result"] for _args, kwargs in steps(firewall, kind)]


def rows(census):
    return census["rows"]


def one(census, **match):
    found = [r for r in rows(census) if all(r[k] == v for k, v in match.items())]
    assert len(found) == 1, (match, rows(census))
    return found[0]


# Classifiers ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,hc,pf,ext,q", [
    (SEED, "app_seed", "nav_seed", "html_none", "none"),
    ("https://app.procore.com/webclients/host/companies/5/projects/123/tools/rfis", "app_other", "nav_seed",
     "html_none", "none"),
    ("https://us02.procore.com/webclients/host/static/js/main.1.js", "app_seed", "webclients_static", "js", "none"),
    ("https://us02.procore.com/webclients/host/config/remotes.json", "app_seed", "webclients_manifest", "json",
     "none"),
    ("https://us02.procore.com/webclients/other", "app_seed", "webclients_other", "html_none", "none"),
    ("https://us02.procore.com/rest/v1.0/companies", "app_seed", "rest_read_ok", "html_none", "none"),
    ("https://us02.procore.com/rest/v1.0/me", "app_seed", "rest_v1_me", "html_none", "none"),
    ("https://us02.procore.com/rest/v1.0/companies/5/users", "app_seed", "rest_v1_companies", "html_none", "none"),
    ("https://us02.procore.com/rest/v1.1/projects/9", "app_seed", "rest_v1_projects", "html_none", "none"),
    ("https://us02.procore.com/rest/v2.0/companies/5/x", "app_seed", "rest_v2", "html_none", "none"),
    ("https://us02.procore.com/graphql?op=Hub", "app_seed", "graphql", "html_none", "plain"),
    ("https://us02.procore.com/cdn-cgi/challenge-platform/scripts/jsd/main.js?ray=1", "app_seed", "cdn_cgi",
     "js", "plain"),
    ("https://us02.procore.com/x/feature_toggles", "app_seed", "feature_flags", "html_none", "none"),
    ("https://us02.procore.com/a?access_token=secret", "app_seed", "root_other", "html_none", "token_like"),
    ("https://us02.procore.com/a/b/c", "app_seed", "other", "html_none", "none"),
    ("https://ui-service-shell.pages.procore.com/static/js/a.mjs", "procore_pages_known:ui-service-shell",
     "static_prefix_refused", "mjs", "none"),
    ("https://header-ui-service.pages.procore.com/remoteEntry.js", "procore_pages_known:header-ui-service",
     "root_remote_entry", "js", "none"),
    ("https://sidepanel-ui-service.pages.procore.com/mf-manifest.json",
     "procore_pages_known:sidepanel-ui-service", "mf_manifest", "json", "none"),
    ("https://secret-label.pages.procore.com/static/js/a.js", "procore_pages_other", "static_ok", "js", "none"),
    ("https://secret-label.pages.procore.com/remote.json", "procore_pages_other", "mf_manifest", "json", "none"),
    ("https://us02.procore.com/remote.json", "app_seed", "root_other", "json", "none"),
    ("https://assets1.procore.com/mfe_erb/abc/static/js/main.1.js", "app_asset", "mfe_erb_js_ok", "js", "none"),
    ("https://assets1.procore.com/mfe_erb/abc/static/css/main.1.css", "app_asset", "mfe_erb_other", "css",
     "none"),
    ("https://production-login-assets0.cdn.procore.com/assets/a.woff2", "login_asset", "static_ok", "font", "none"),
    ("https://other.cdn.procore.com/x.map", "procore_cdn_other", "root_other", "map", "none"),
    ("https://events.procore.com/collect", "procore_other", "telemetry", "html_none", "none"),
    ("https://login.procore.com/oauth/authorize?code=1", "login", "login_path", "html_none", "token_like"),
    ("https://app.launchdarkly.com/sdk/evalx/x", "launchdarkly", "other", "html_none", "none"),
    ("https://static.cloudflareinsights.com/beacon.min.js", "cloudflare", "telemetry", "js", "none"),
    ("https://browser-intake-datadoghq.com/api/v2/rum", "datadog", "api", "html_none", "none"),
    ("https://private-host.example/x.png", "third_party_other", "root_other", "image", "none"),
    ("data:image/png;base64,AAAA", "local_scheme", "other", "html_none", "none"),
    ("ftp://example.test/x", "invalid", "root_other", "html_none", "none"),
])
def test_classifiers(url, hc, pf, ext, q):
    from urllib.parse import urlsplit
    firewall = guard()
    parts = urlsplit(url)
    host = D.host_class(firewall, parts.scheme, parts.hostname)
    assert host == hc
    assert D.path_family(firewall, host, parts.path) == pf
    assert D.extension(parts.path) == ext
    assert D.query_kind(url, parts.query) == q


@pytest.mark.parametrize("text,expected", [
    ("net::ERR_BLOCKED_BY_CLIENT", "blocked_by_client"), ("net::ERR_BLOCKED_BY_CLIENT.Inspector", "blocked_by_client"),
    ("net::ERR_FAILED", "failed"), ("net::ERR_FAILED.Other", "other"),
    ("net::ERR_ABORTED", "aborted"), ("net::ERR_BLOCKED_BY_CSP", "blocked_by_csp"),
    ("net::ERR_BLOCKED_BY_ORB", "blocked_by_orb"), ("net::ERR_BLOCKED_BY_RESPONSE", "blocked_by_response"),
    ("net::ERR_CONNECTION_RESET", "connection"), ("net::ERR_NAME_NOT_RESOLVED", "dns"),
    ("net::ERR_TIMED_OUT", "timeout"), ("net::ERR_CERT_AUTHORITY_INVALID", "cert"),
    ("net::ERR_CONTENT_DECODING_FAILED", "decoding"), ("net::ERR_INVALID_RESPONSE", "invalid_response"),
    ("net::ERR_HTTP2_PROTOCOL_ERROR", "http2"), ("net::ERR_SOMETHING https://private.test", "other"),
    (None, "other"), (42, "other"),
])
def test_net_failure_enum(text, expected):
    assert D.net_failure(text) == expected


# Census rows ------------------------------------------------------------------------------


async def test_refused_paths_share_one_blocked_step_but_get_distinct_census_rows():
    firewall = guard()
    page = model_page(firewall)
    for path in ("/rest/v2.0/companies/5/private-a", "/cdn-cgi/challenge-platform/private-b"):
        req = request(page, "https://us02.procore.com" + path, rt="fetch")
        await firewall._on_route(route(req))
        req.failure = "net::ERR_BLOCKED_BY_CLIENT"
        firewall.diagnostics.request_failed(req)
    firewall.diagnostics.request_failed(NS(failure="net::ERR_BLOCKED_BY_CSP"))   # never routed
    assert len(steps(firewall, "http.blocked")) == 1
    await firewall.diagnostics.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    assert {r["pf"] for r in rows(census)} == {"rest_v2", "cdn_cgi"}
    assert all(r["co"] == "failed:blocked_by_client" for r in rows(census))
    assert census["unrouted_failed"] == {"blocked_by_csp": 1} and census["pending"] == 0


async def test_census_rows_separate_path_families_and_pages_and_commit_on_chromium_outcome():
    firewall = guard()
    page = model_page(firewall)
    login_page = NS(main_frame=None, url="https://login.procore.com/", is_closed=lambda: False)
    login_page.main_frame = NS(page=login_page, parent_frame=None)
    firewall.login_page = login_page
    reqs = [request(page, "https://us02.procore.com/rest/v2.0/companies/5/x", rt="fetch"),
            request(page, "https://us02.procore.com/cdn-cgi/challenge-platform/x", rt="script"),
            request(login_page, "https://us02.procore.com/rest/v2.0/companies/5/x", rt="fetch")]
    for req in reqs:
        await firewall._on_route(route(req))
        req.failure = "net::ERR_BLOCKED_BY_CLIENT"
        firewall.diagnostics.request_failed(req)
    await firewall.diagnostics.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    assert one(census, pg="model", pf="rest_v2")["v"] == "refused:path_not_allowed"
    assert one(census, pg="model", pf="cdn_cgi")["co"] == "failed:blocked_by_client"
    assert one(census, pg="login", pf="rest_v2")["n"] == 1
    assert census["window"] == "delta" and census["pending"] == 0


async def test_fulfilled_bundle_without_acao_then_failed_shows_cors_shape():
    firewall = guard()
    page = model_page(firewall)
    req = request(page, SHELL, origin="https://us02.procore.com")
    firewall.asset_request = NS(get=AsyncMock(return_value=response(headers={
        "content-type": "application/javascript", "cross-origin-resource-policy": "same-site"})))
    assert req.resource_type == "script"
    await firewall._on_route(route(req))
    req.failure = "net::ERR_FAILED"
    firewall.diagnostics.request_failed(req)
    await firewall.diagnostics.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    row = one(census, hc="procore_pages_known:ui-service-shell")
    assert row == {"pg": "model", "rt": "script", "hc": "procore_pages_known:ui-service-shell",
                   "pf": "static_ok", "ep": "-", "ext": "js", "q": "none", "m": "GET", "v": "fulfilled",
                   "sc": "2xx", "ct": "js", "sz": "lt64k", "oh": "present_cross", "ac": "absent", "cr": "same_site",
                   "co": "failed:failed", "n": 1, "dh": 1, "du": 1}
    assert "1 failed after fulfil" in steps(firewall, "browser.resource_census")[0][0][1]


@pytest.mark.parametrize("acao,expected", [("*", "star"), ("https://us02.procore.com", "app_origin"),
                                           ("https://private-origin.test", "other")])
async def test_acao_classes_and_finished_outcome(acao, expected):
    firewall = guard()
    page = model_page(firewall)
    req = request(page, SHELL, origin="https://ui-service-shell.pages.procore.com")
    firewall.asset_request = NS(get=AsyncMock(return_value=response(headers={
        "content-type": "application/javascript", "access-control-allow-origin": acao})))
    await firewall._on_route(route(req))
    firewall.diagnostics.request_finished(req)
    await firewall.diagnostics.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    row = one(census, v="fulfilled")
    assert row["ac"] == expected and row["oh"] == "present_same" and row["co"] == "finished"
    assert "private-origin" not in json.dumps(steps(firewall))


async def test_too_large_transport_redirect_and_non_2xx_rows():
    firewall = guard()
    page = model_page(firewall)
    cases = [
        (request(page, "https://us02.procore.com/assets/a.js"),
         response(headers={"content-length": str(9 * 1024 * 1024)})),
        (request(page, "https://us02.procore.com/assets/b.js"), RuntimeError("Route.fetch: socket hang up")),
        (request(page, SEED, rt="document", main=True), response(302, {"location": SEED})),
        (request(page, SEED, rt="document", main=True), response(302, {"location": "https://private.test/x"})),
        (request(page, "https://us02.procore.com/assets/c.js"), response(404, {"content-type": "text/html"})),
    ]
    for req, outcome in cases:
        r = route(req, outcome if not isinstance(outcome, Exception) else None)
        if isinstance(outcome, Exception):
            r.fetch.side_effect = outcome
        await firewall._on_route(r)
        firewall.diagnostics.request_finished(req)
    await firewall.diagnostics.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    assert one(census, v="too_large")["sz"] == "gt8m"
    assert one(census, v="transport:connection")["sc"] == "none"
    assert one(census, v="redirect_followed")["sc"] == "3xx"
    assert one(census, v="redirect_refused")["pf"] == "nav_seed"
    assert one(census, v="fulfilled")["sc"] == "4xx"
    assert "private" not in json.dumps(steps(firewall, "browser.resource_census"))


async def test_unowned_requests_commit_immediately_and_never_raise():
    firewall = guard()
    stray = NS(method="GET", url="https://us02.procore.com/private-worker.js", frame=None,
               is_navigation_request=lambda: False)
    await firewall._on_route(route(stray))
    firewall.diagnostics.record(object(), page=None, login=False, verdict="fulfilled")   # no url attribute
    await firewall.diagnostics.emit(None, "final")
    [census] = results(firewall, "browser.resource_census")
    row = one(census, pg="unowned")
    assert row["v"] == "refused:page_not_owned" and row["co"] == "pending" and row["rt"] == "other"


# Console, page errors, CSP ------------------------------------------------------------


class Message:
    """A console message whose location must never be read."""

    def __init__(self, kind, text):
        self.type, self.text = kind, text

    @property
    def location(self):
        raise AssertionError("msg.location must never be read")

    @property
    def args(self):
        raise AssertionError("msg.args must never be read")


@pytest.mark.parametrize("line,category", [
    ("Access to script at 'https://x' from origin 'https://y' has been blocked by CORS policy", "cors"),
    (("Refused to load the script 'https://x/private' because it violates the following Content Security "
      "Policy directive: \"script-src 'self'\"."), "csp"),
    ("This document requires 'TrustedScript' assignment.", "trusted_types"),
    (("Failed to load module script: Expected a JavaScript module script but the server responded with a MIME "
      "type of \"text/html\"."), "module_mime"),
    ("Refused to execute script from 'https://x' because its MIME type ('text/html') is not executable",
     "mime"),
    ("Failed to find a valid digest in the 'integrity' attribute for resource", "sri"),
    ("net::ERR_BLOCKED_BY_ORB", "coep_corp_orb"),
    ("ChunkLoadError: Loading chunk 12 failed.", "chunk_load"),
    ("Failed to resolve module specifier \"react\". Relative references must start with", "import_map"),
    ("Failed to load resource: net::ERR_BLOCKED_BY_CLIENT", "blocked_by_client"),
    ("Failed to load resource: the server responded with a status of 404 ()", "http_status_4xx"),
    ("Failed to load resource: the server responded with a status of 503 ()", "http_status_5xx"),
    ("Failed to load resource: net::ERR_CONNECTION_RESET", "net_error"),
    ("Mixed Content: The page at was loaded over HTTPS", "mixed_content"),
    ("Uncaught SyntaxError: Unexpected token '<'", "syntax"),
    ("Uncaught TypeError: Cannot read properties of undefined", "type_error"),
    ("Uncaught ReferenceError: x is not defined", "reference_error"),
    ("private garbage token=abc", "other"),
])
def test_console_category(line, category):
    assert D.console_category(line) == category


async def test_console_and_page_errors_count_enums_only_and_ignore_later_lines():
    firewall = guard()
    page = model_page(firewall)
    diag = firewall.diagnostics
    diag.console(Message("error", "Refused to load the script 'https://private-host.test/p?token=private-q' "
                         "because it violates the following Content Security Policy directive: "
                         "\"script-src-elem 'self'\".\nAccess blocked by CORS policy private-second-line"))
    diag.console(Message("warning", "[Report Only] Refused to connect because it violates the following "
                         "Content Security Policy directive: \"connect-src https://private.test\"."))
    diag.console(Message("warning", "Content Security Policy directive: \"x-private-made-up 'self'\""))
    diag.console(Message("log", "private log line"))
    diag.console(Message("info", "private info line"))
    diag.console(Message("error", "x" * 600 + " CORS policy"))   # past the first 512 chars: ignored
    diag.console(Message("error", None))
    diag.page_error(NS(name="TypeError", message="Cannot read private-value of undefined\nat private.js"))
    diag.page_error(NS(name="PrivateCustomError", message="private message"))
    diag.page_error(NS(name="Error", message="Loading chunk 7 failed. (https://private.test/7.js)"))
    diag.page_error(NS(name="Error", message="x\nChunkLoadError on a later line"))
    diag.page_error(object())
    await diag.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    assert census["console"] == {"error": {"csp": 1, "other": 2}, "warning": {"csp": 2}, "info": {}}
    assert census["csp"] == {"enforced": {"script-src-elem": 1, "other": 1}, "report_only": {"connect-src": 1}}
    assert census["pageerror"] == {"type_error": 1, "other": 2, "chunk_load": 1, "error": 1}
    assert "private" not in json.dumps(steps(firewall))


async def test_event_cap_counts_drops():
    firewall = guard()
    page = model_page(firewall)
    for _ in range(D.MAX_EVENTS + 7):
        firewall.diagnostics.console(Message("error", "Uncaught TypeError: x"))
    firewall.diagnostics.page_error(NS(name="TypeError", message="x"))
    await firewall.diagnostics.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    assert census["console"]["error"]["type_error"] == D.MAX_EVENTS
    assert census["events_dropped"] == 8 and census["pageerror"] == {}


# Privacy decoys across every recorder ---------------------------------------------------


async def test_no_decoy_reaches_any_serialized_step():
    firewall = guard()
    page = model_page(firewall, url=SEED + "?secret_query=decoy-q1", probe={**BLANK, "title": "decoy-title",
                      "text": "decoy-dom-text", "loc": "decoy-loc"})
    decoys = ["https://us02.procore.com/decoy-path-1/token-decoy2",
              "https://decoy-host-3.example/x?decoy4=decoy5",
              "https://decoy-label-6.pages.procore.com/static/js/decoy7.js",
              "https://ui-service-shell.pages.procore.com/static/js/a.js?decoy8=1",
              "https://us02.procore.com/rest/v1.0/projects/123/rfis?access_token=decoy9",
              SHELL]
    firewall.asset_request = NS(get=AsyncMock(return_value=response(headers={
        "set-cookie": "s=decoy10", "access-control-allow-origin": "https://decoy11.test",
        "content-type": "decoy12/thing", "x-private": "decoy13"})))
    for url in decoys:
        req = request(page, url, origin="https://decoy14.test")
        await firewall._on_route(route(req, response(headers={"x-private": "decoy15"})))
        req.failure = "net::ERR_DECOY16 https://decoy17.test"
        firewall.diagnostics.request_failed(req)
    firewall.diagnostics.console(Message("error", "decoy18 https://decoy19.test/?t=decoy20"))
    firewall.diagnostics.page_error(NS(name="decoy21", message="decoy22 at https://decoy23.test"))
    firewall.diagnostics.spa_blanked("https://us02.procore.com/decoy24?decoy25=1")
    firewall.diagnostics.spa_blanked("https://decoy26.pages.procore.com/decoy27")
    await firewall.diagnostics.emit(page, "navigate")
    await firewall.diagnostics.emit(page, "final")
    persisted = json.dumps(steps(firewall))
    assert steps(firewall, "browser.resource_census") and steps(firewall, "browser.render_probe")
    assert "decoy" not in persisted
    assert "secret_query" not in persisted and "?" not in persisted.replace('"?"', "")


# Probe -------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "decoy", [], 7, {"text_len": "decoy"}])
def test_sanitize_probe_non_dict_and_forged_fields(raw):
    probe = sanitize_probe(raw)
    if not isinstance(raw, dict):
        assert probe is None
        return
    assert probe["text_len"] == 0 and probe["element_count"] == 0 and probe["ready"] == -1


def test_sanitize_probe_clamps_and_drops_forged_types():
    probe = sanitize_probe({
        "text_len": float("nan"), "element_count": 10 ** 30, "visible_boxes": -5, "body": "true",
        "hidden_root": 1, "probe_error": True, "ready": 9, "title_len": True, "body_w": float("inf"),
        "scripts": 3.9, "unknown": "decoy", "title": "decoy",
        "root_children": {"root": "decoy", "app": 4, "main": -9, "single_spa": -1, "x": "decoy"}})
    assert probe["text_len"] == 0 and probe["element_count"] == D.PROBE_INT_MAX
    assert probe["visible_boxes"] == 0 and probe["body"] is False and probe["hidden_root"] is False
    assert probe["probe_error"] is True and probe["ready"] == 2 and probe["title_len"] == 0
    assert probe["body_w"] == 0 and probe["scripts"] == 3
    assert probe["root_children"] == {"root": -1, "app": 4, "main": -1, "single_spa": 0}
    assert "unknown" not in probe and "title" not in probe and "decoy" not in json.dumps(probe)
    assert set(probe) == {"ready", *D.PROBE_INTS, *D.PROBE_BOOLS, "root_children"}
    assert not any(isinstance(v, str) for v in probe.values())


@pytest.mark.parametrize("probe,expected", [
    (None, False), ("x", False), (sanitize_probe(BLANK), False), (sanitize_probe(RENDERED), True),
    ({"text_len": 1}, True), ({"text_len": 0, "visible_boxes": D.READABLE_MIN_BOXES}, True),
    ({"text_len": 0, "visible_boxes": D.READABLE_MIN_BOXES - 1}, False), ({"text_len": "999"}, False),
])
def test_readable(probe, expected):
    assert readable(probe) is expected
    assert guard().diagnostics.readable(probe) is expected


async def test_probe_is_bounded_and_swallows_errors(monkeypatch):
    firewall = guard()
    monkeypatch.setattr(D, "PROBE_TIMEOUT_S", 0.01)

    async def hang(_js):
        await asyncio.sleep(10)

    page = model_page(firewall)
    page.evaluate = hang
    assert await firewall.diagnostics.probe(page) is None
    page.evaluate = AsyncMock(side_effect=RuntimeError("Target closed https://private.test"))
    assert await firewall.diagnostics.probe(page) is None
    assert await firewall.diagnostics.probe(model_page(firewall, closed=True)) is None
    assert await firewall.diagnostics.probe(None) is None
    probe = await firewall.diagnostics.probe(model_page(firewall, probe=RENDERED))
    assert probe["text_len"] == 812 and probe["element_count"] == 400
    assert isinstance(probe["text_len"], int) and isinstance(probe["element_count"], int)
    assert not firewall.log_step.called   # probe() logs nothing


# Emission and caps ------------------------------------------------------------------------


async def test_emit_once_per_trigger_and_navigation_and_final_once():
    firewall = guard()
    page = model_page(firewall)
    diag = firewall.diagnostics
    probe = await diag.emit(page, "navigate")
    assert probe == sanitize_probe(BLANK)
    assert await diag.emit(page, "navigate") is None          # same nav_no
    assert await diag.emit(page, "empty_read") is not None
    firewall.nav_count += 1
    assert await diag.emit(page, "navigate") is not None
    assert await diag.emit(page, "bogus") is None
    assert await diag.emit(page, "final") is not None
    assert await diag.emit(page, "final") is None
    kinds = [args[0] for args, _ in steps(firewall)]
    assert kinds == ["browser.resource_census", "browser.render_probe"] * 4
    assert all(worker_step_kind_allowed(k) for k in kinds)
    probes = results(firewall, "browser.render_probe")
    assert [p["trigger"] for p in probes] == ["navigate", "empty_read", "navigate", "final"]
    assert probes[0] == {"v": 2, "trigger": "navigate", "nav_no": 0, "loc_hc": "app_seed", "loc": "nav_seed",
                         "loc_is_seed": True, "readable": False, "probe": sanitize_probe(BLANK)}
    windows = [c["window"] for c in results(firewall, "browser.resource_census")]
    assert windows == ["delta", "delta", "delta", "total"]
    assert all(kwargs["ok"] is True for _args, kwargs in steps(firewall))


async def test_emit_accepts_runner_trigger_aliases():
    firewall = guard()
    page = model_page(firewall)
    await firewall.diagnostics.emit(page, "nav")
    await firewall.diagnostics.emit(page, "read_empty")
    assert [p["trigger"] for p in results(firewall, "browser.render_probe")] == ["navigate", "empty_read"]


async def test_closed_page_still_gets_a_census_but_no_probe():
    firewall = guard()
    assert await firewall.diagnostics.emit(model_page(firewall, closed=True), "navigate") is None
    assert await firewall.diagnostics.emit(None, "final") is None
    kinds = [args[0] for args, _ in steps(firewall)]
    assert kinds == ["browser.resource_census", "browser.resource_census"]


async def test_step_cap_reserves_the_final_pair():
    firewall = guard()
    page = model_page(firewall)
    for _ in range(20):
        firewall.nav_count += 1
        await firewall.diagnostics.emit(page, "navigate")
        await firewall.diagnostics.emit(page, "empty_read")
    assert len(steps(firewall)) == D.MAX_DIAG_STEPS - D.FINAL_RESERVE
    assert await firewall.diagnostics.emit(page, "navigate") is None
    assert await firewall.diagnostics.emit(page, "final") is not None
    assert len(steps(firewall)) == D.MAX_DIAG_STEPS
    assert steps(firewall)[-1][0][0] == "browser.render_probe"


async def test_step_logging_failure_never_raises():
    firewall = guard()
    firewall.log_step = Mock(side_effect=RuntimeError("spool full"))
    assert await firewall.diagnostics.emit(model_page(firewall, probe=RENDERED), "navigate") is not None


def fill_rows(diag, count, *, outcome="finished", verdict="fulfilled"):
    for i in range(count):
        diag._commit(("model", "script", "app_seed", f"pf{i}", "-", "js", "none", "GET", verdict, "2xx", "js",
                      "lt64k", "absent", "absent", "absent"), outcome)


async def test_row_cap_overflow_and_count_saturation():
    firewall = guard()
    page = model_page(firewall)
    diag = firewall.diagnostics
    fill_rows(diag, D.MAX_CENSUS_KEYS + 25)
    key = next(iter(diag._total.rows))
    diag._total.rows[key] = D.COUNT_CAP - 1
    diag._delta.rows[key] = D.COUNT_CAP - 1
    for _ in range(3):
        diag._commit(key[:-1], key[-1])
    await diag.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    assert census["keys_overflow"] == 25
    assert max(r["n"] for r in census["rows"]) == D.COUNT_CAP
    assert len(census["rows"]) + census["rows_dropped"] == D.MAX_CENSUS_KEYS


async def test_payload_cap_drops_clean_low_count_rows_first():
    firewall = guard()
    page = model_page(firewall)
    diag = firewall.diagnostics
    fill_rows(diag, 150)
    for i in range(10):
        for _ in range(i + 1):
            diag._commit(("model", "script", "procore_pages_other", f"bad{i}", "-", "js", "none", "GET",
                          "refused:origin_not_allowed", "none", "absent", "unknown", "absent", "absent",
                          "absent"), "failed:blocked_by_client")
    await diag.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    size = len(json.dumps(census, sort_keys=True, separators=(",", ":")))
    assert size <= D.MAX_PAYLOAD_BYTES and census["rows_dropped"] > 0
    assert sum(r["v"].startswith("refused:") for r in census["rows"]) == 10


async def test_pending_map_is_bounded_and_evicts_oldest_as_pending():
    firewall = guard()
    page = model_page(firewall)
    diag = firewall.diagnostics
    reqs = [request(page, f"https://us02.procore.com/assets/a{i}.js") for i in range(D.MAX_PENDING + 3)]
    for req in reqs:
        diag.record(req, page=page, login=False, verdict="fulfilled", status=200, headers={}, size=1)
    assert len(diag._pending) == D.MAX_PENDING
    diag.request_finished(reqs[0])       # already evicted: ignored, not double counted
    diag.request_finished(reqs[-1])
    await diag.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    assert one(census, co="pending")["n"] == 3 and one(census, co="finished")["n"] == 1
    assert census["pending"] == D.MAX_PENDING - 1
    await diag.emit(page, "final")
    total = results(firewall, "browser.resource_census")[-1]
    assert total["window"] == "total" and total["pending"] == 0
    assert one(total, co="pending")["n"] == D.MAX_PENDING + 2


# Hooks ---------------------------------------------------------------------------------------


def listened(page):
    return [call.args[0] for call in page.on.call_args_list]


def test_org_guard_adds_no_new_listeners_and_has_no_diagnostics():
    org = Guard(ceiling_for("rfi_from_email"))
    page = NS(on=Mock())
    org._adopt(page, login=False)
    assert listened(page) == ["request", "requestfailed", "framenavigated", "popup", "dialog"]
    assert not hasattr(org, "diagnostics")


def test_user_guard_adds_diagnostic_listeners_model_and_login():
    firewall = guard()
    model, login = NS(on=Mock()), NS(on=Mock())
    firewall._adopt(model, login=False)
    firewall._adopt(login, login=True)
    base = ["request", "requestfailed", "framenavigated", "popup", "dialog"]
    assert listened(model) == [*base, "requestfinished", "requestfailed", "console", "pageerror"]
    assert listened(login) == [*base, "requestfinished", "requestfailed"]
    assert isinstance(UserGuard(ceiling_for("user_browser_read")).diagnostics, RenderDiagnostics)


def test_listeners_never_raise_on_hostile_objects():
    diag = guard().diagnostics

    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError("private")

    for hook in (diag.request_finished, diag.request_failed, diag.console, diag.page_error):
        hook(Hostile())
        hook(None)


async def test_spa_nav_blanked_counts_same_document_blanks_only():
    firewall = guard()
    page = model_page(firewall)
    firewall._spawn = Mock(side_effect=lambda coro: coro.close())
    off_route = "https://us02.procore.com/webclients/host/companies/5/projects/123/tools/private-tool"
    firewall._on_navigated(page, NS(parent_frame=None, url=off_route))           # pushState rewrite
    firewall._on_navigated(page, NS(parent_frame=None, url=SEED))                 # allowed: not counted
    firewall._doc_loads.add(id(page))
    firewall._on_navigated(page, NS(parent_frame=None, url=off_route))           # document load: not counted
    assert firewall._spawn.call_count == 3   # still strict: both off-route URLs blanked (base + user)
    await firewall.diagnostics.emit(page, "final")
    [census] = results(firewall, "browser.resource_census")
    assert census["spa_nav_blanked"] == {"webclients_doc": 1}
    assert "private" not in json.dumps(steps(firewall))


def test_strict_same_document_blank_is_reported_as_one_model_violation():
    firewall = guard()
    page = model_page(firewall)
    firewall._spawn = Mock(side_effect=lambda coro: coro.close())
    off_route = "https://us02.procore.com/webclients/host/companies/5/projects/123/tools/private-tool"
    firewall._on_navigated(page, NS(parent_frame=None, url=off_route))           # pushState rewrite
    [same_doc] = firewall.take_violations("model")
    assert same_doc.page == "model" and same_doc.reason == "path_not_allowed"
    firewall._doc_loads.add(id(page))
    firewall._on_navigated(page, NS(parent_frame=None, url=off_route))           # base check already reports
    assert len(firewall.take_violations("model")) == 1
    firewall._on_navigated(page, NS(parent_frame=None, url=SEED))
    assert firewall.take_violations("model") == []
