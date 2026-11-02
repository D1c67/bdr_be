"""Worker 1.0.6 diagnostics: listed page-host labels, distinct host/URL counts,
endpoint templates, loader error enums, the spa_not_found probe marker and the
page_code_blocked finish. Every emitted value stays code-fixed."""
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest

from bdr_agent import protocol as P
from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser import diagnostics as D
from bdr_agent.agents.user_browser.diagnostics import NOT_FOUND_TEXT, sanitize_probe
from bdr_agent.agents.user_browser.guard import PAGES_PUBLIC_LABELS, UserGuard
from bdr_agent.agents.user_browser.progress import SUMMARY
from bdr_agent.agents.user_browser.runner import FinishTool, UserExecutor
from bdr_agent.browser.executor import ToolError

C, PR = "562949953430361", "562949955106559"
APP = "https://us02.procore.com"
SEED = f"{APP}/webclients/host/companies/{C}/projects/{PR}/tools/hubs/project-hub/views/overview"
LEGACY = f"https://app.procore.com/{PR}/project/rfi/list"
EM_DASH = chr(0x2014)


def guard(seed=SEED):
    value = UserGuard(ceiling_for("user_browser_read"))
    value.bind_seed(seed)
    value.log_step = Mock()
    return value


def model_page(firewall, probe=None, url=SEED):
    page = NS(main_frame=None, url=url, is_closed=lambda: False,
              evaluate=AsyncMock(return_value=probe if probe is not None else {"text_len": 0}))
    page.main_frame = NS(page=page, parent_frame=None)
    firewall.model_pages.add(id(page))
    return page


def request(page, url, rt="script", method="GET"):
    return NS(method=method, url=url, frame=page.main_frame, resource_type=rt, headers={},
              is_navigation_request=lambda: False, failure="net::ERR_BLOCKED_BY_CLIENT")


async def refuse(firewall, page, url, rt="script"):
    req = request(page, url, rt)
    route = NS(request=req, fetch=AsyncMock(), fulfill=AsyncMock(), abort=AsyncMock(), continue_=AsyncMock())
    await firewall._on_route(route)
    firewall.diagnostics.request_failed(req)
    return route


def results(firewall, kind):
    return [c.kwargs["result"] for c in firewall.log_step.call_args_list if c.args[0] == kind]


def persisted(firewall):
    return json.dumps([(c.args, c.kwargs) for c in firewall.log_step.call_args_list])


# Host classes ------------------------------------------------------------------------------


@pytest.mark.parametrize("host,hc", [
    ("rfis-ui-service.pages.procore.com", "procore_pages_known:rfis-ui-service"),
    ("hubs-ui-service.pages.procore.com", "procore_pages_known:hubs-ui-service"),
    ("project-home-ui-service.pages.procore.com", "procore_pages_known:project-home-ui-service"),
    ("ui-service-shell.pages.procore.com", "procore_pages_known:ui-service-shell"),
    ("custom-configurable-fields-ui-service.pages.procore.com",
     "procore_pages_listed:custom-configurable-fields-ui-service"),
    ("portfolio-hub-ui-service.pages.procore.com", "procore_pages_listed:portfolio-hub-ui-service"),
    ("decoy-secret.pages.procore.com", "procore_pages_other"),
    ("a.rfis-ui-service.pages.procore.com", "procore_pages_other"),
])
def test_host_class_names_only_code_listed_labels(host, hc):
    assert D.host_class(guard(), "https", host) == hc


def test_every_listed_label_maps_to_itself_only():
    firewall = guard()
    for label in PAGES_PUBLIC_LABELS:
        hc = D.host_class(firewall, "https", f"{label}.pages.procore.com")
        assert hc.split(":", 1)[1] == label


# Endpoint templates ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,template", [
    ("/rest/v1.1/environment_metadata", "/rest/v1.1/environment_metadata"),
    (f"/rest/v2.0/companies/{C}/early_access/enrollees_for_user", "/rest/v2.0/companies/{c}/early_access/enrollees_for_user"),
    (f"/rest/v1.0/projects/{PR}/procore_environment", "/rest/v1.0/projects/{p}/procore_environment"),
    (f"/rest/v1.0/companies/{C}/procore_environment", "/rest/v1.0/companies/{c}/procore_environment"),
    (f"/rest/v1.0/projects/{PR}/rfis/77", "/rest/v1.0/projects/{p}/rfis/{id}"),
    (f"/rest/v2.0/companies/{C}/projects/{PR}/hubs/containers", "/rest/v2.0/companies/{c}/projects/{p}/hubs/containers"),
    (f"/{PR}/project/rfi/list_client_config.json", "/{p}/project/rfi/list_client_config.json"),
    (f"/{C}/company/mindtouch/help_link", "/{c}/company/mindtouch/help_link"),
    ("/cdn-cgi/challenge-platform/scripts/jsd/main.js", "/cdn-cgi/challenge-platform/scripts/jsd/main.js"),
    ("/rest/v2.0/companies/999/early_access/enrollees_for_user", "-"),       # not the seed company
    ("/rest/v1.0/projects/999/procore_environment", "-"),                    # not the seed project
    ("/rest/v1.0/decoy-secret-segment", "-"),
    (f"/rest/v1.0/projects/{PR}/rfis/decoy", "-"),
])
def test_endpoint_templates_are_code_fixed(path, template):
    assert D.endpoint_template(guard(), "app_seed", path) == template
    assert D.endpoint_template(guard(), "procore_pages_other", path) == "-"


def test_legacy_seed_templates_use_any_company_but_the_seed_project():
    firewall = guard(LEGACY)
    assert D.endpoint_template(firewall, "app_seed", "/rest/v2.0/companies/5/early_access/enrollees_for_user") == \
        "/rest/v2.0/companies/{c}/early_access/enrollees_for_user"
    assert D.endpoint_template(firewall, "app_seed", f"/rest/v1.0/projects/{PR}") == "/rest/v1.0/projects/{p}"
    assert D.endpoint_template(firewall, "app_seed", "/rest/v1.0/projects/5") == "-"


def test_every_template_is_placeholder_only():
    for template in D.ENDPOINT_TEMPLATES:
        assert not any(ch.isdigit() for ch in template.replace("v1.0", "").replace("v1.1", "").replace("v2.0", ""))


async def test_census_rows_carry_templates_never_query_values():
    firewall = guard()
    page = model_page(firewall)
    await refuse(firewall, page, f"{APP}/rest/v1.1/environment_metadata?company_id=999&project_id=decoy", "fetch")
    await refuse(firewall, page, f"{APP}/rest/v2.0/companies/{C}/early_access/enrollees_for_user?project_id={PR}",
                 "fetch")
    await firewall.diagnostics.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    eps = {r["ep"]: r for r in census["rows"]}
    assert eps["/rest/v1.1/environment_metadata"]["v"] == "refused:read_query_not_allowed"
    assert eps["/rest/v1.1/environment_metadata"]["pf"] == "rest_read_ok"
    assert eps["/rest/v2.0/companies/{c}/early_access/enrollees_for_user"]["v"] == "refused:path_not_allowed"
    text = persisted(firewall)
    assert "decoy" not in text and "999" not in text and C not in text


# Distinct counts ------------------------------------------------------------------------------


async def test_distinct_hosts_and_urls_explain_repeats_without_naming_them():
    firewall = guard()
    page = model_page(firewall)
    hosts = ["decoy-one.pages.procore.com", "decoy-two.pages.procore.com", "decoy-three.pages.procore.com"]
    for i in range(18):
        await refuse(firewall, page, f"https://{hosts[i % 3]}/remoteEntry.js")
    for _ in range(4):
        await refuse(firewall, page, "https://decoy-one.pages.procore.com/remoteEntry.js?t=decoy")
    await firewall.diagnostics.emit(page, "navigate")
    [census] = results(firewall, "browser.resource_census")
    plain = [r for r in census["rows"] if r["hc"] == "procore_pages_other" and r["q"] == "none"]
    assert len(plain) == 1 and plain[0]["n"] == 18 and plain[0]["dh"] == 3 and plain[0]["du"] == 3
    query = [r for r in census["rows"] if r["hc"] == "procore_pages_other" and r["q"] == "plain"]
    assert query[0]["n"] == 4 and query[0]["dh"] == 1 and query[0]["du"] == 1
    assert census["remote_entry_refused"] == 22 and census["v"] == 2
    assert "decoy" not in persisted(firewall)


def test_distinct_counts_saturate():
    window = D._Window()
    for i in range(D.DISTINCT_CAP + 50):
        window.add(("k",), D._ident(f"https://h{i}.example/{i}"))
    assert window.distinct_counts(("k",)) == (D.DISTINCT_CAP, D.DISTINCT_CAP)
    assert window.rows[("k",)] == D.DISTINCT_CAP + 50


def test_overflow_rows_keep_no_identities():
    window = D._Window()
    for i in range(D.MAX_CENSUS_KEYS + 5):
        window.add((f"k{i}",), D._ident(f"https://h{i}.example/"))
    assert window.keys_overflow == 5 and len(window.distinct) == D.MAX_CENSUS_KEYS


# Loader errors ---------------------------------------------------------------------------------


@pytest.mark.parametrize("line,category", [
    ("ScriptExternalLoadError: Loading script failed.", "script_external_load"),
    ("Loading script failed.", "script_external_load"),
    ("ScriptLoadError: Remote entry script failed to load. Check the network tab for more details.", "script_load"),
    ("RemoteScopeError: Remote entry script loaded successfully but the remote scope name could not be found",
     "remote_scope"),
    ("Remote loaded successfully but hubsApp could not be found!", "remote_scope"),
    ("Attempt 1 of 4 to load HubsApp from https://decoy.test failed. Retrying in 300ms...", "remote_load_retry"),
    ("[ Federation Runtime ]: RUNTIME-008 Failed to load script resources", "mf_runtime"),
    ("Loading chunk 77369 failed.", "chunk_load"),
])
def test_loader_console_categories(line, category):
    assert D.console_category(line) == category


@pytest.mark.parametrize("name,message,category", [
    ("ScriptLoadError", "x", "script_load"), ("ScriptExternalLoadError", "x", "script_external_load"),
    ("RemoteScopeError", "x", "remote_scope"),
    ("Error", "Loading script failed.\n(missing: https://decoy.test/remoteEntry.js)", "script_external_load"),
    ("Error", "Remote entry script failed to load.", "script_load"),
    ("DecoyCustomError", "decoy", "other"),
])
def test_loader_page_errors(name, message, category):
    assert D.page_error_category(name, message) == category


async def test_info_lines_count_only_loader_patterns():
    firewall = guard()
    diag = firewall.diagnostics
    diag.console(NS(type="info", text="Attempt 2 of 4 to load RfisUIServiceApp from https://decoy.test failed."))
    diag.console(NS(type="info", text="decoy info chatter with a token=decoy"))
    diag.console(NS(type="log", text="Attempt 1 of 4 to load x"))
    diag.console(NS(type="error", text="ScriptExternalLoadError: Loading script failed.\n(missing: https://decoy)"))
    diag.page_error(NS(name="ScriptLoadError", message="Remote entry script failed to load."))
    await diag.emit(model_page(firewall), "navigate")
    [census] = results(firewall, "browser.resource_census")
    assert census["console"] == {"error": {"script_external_load": 1}, "warning": {},
                                 "info": {"remote_load_retry": 1}}
    assert census["pageerror"] == {"script_load": 1}
    assert diag._events == 3 and "decoy" not in persisted(firewall)


# Probe marker and page_code_blocked -------------------------------------------------------------


def test_probe_js_matches_the_fixed_sentence_in_page_and_returns_a_boolean():
    assert "spa_not_found" in D.PROBE_JS and "NOT_FOUND" in D.PROBE_JS
    assert NOT_FOUND_TEXT == "Sorry, this page may have been moved or deleted"
    assert sanitize_probe({"spa_not_found": True})["spa_not_found"] is True
    for forged in ("true", 1, NOT_FOUND_TEXT, ["x"], {"a": 1}, None):
        assert sanitize_probe({"spa_not_found": forged})["spa_not_found"] is False


async def test_probe_passes_the_fixed_sentence_as_the_only_argument():
    firewall = guard()
    page = model_page(firewall, probe={"text_len": 50, "spa_not_found": True})
    probe = await firewall.diagnostics.probe(page)
    page.evaluate.assert_awaited_once_with(D.PROBE_JS, NOT_FOUND_TEXT)
    assert probe["spa_not_found"] is True and not any(isinstance(v, str) for v in probe.values())


@pytest.mark.parametrize("refused,not_found,expected", [(True, True, True), (True, False, False),
                                                        (False, True, False), (False, False, False)])
async def test_page_code_blocked_needs_a_refused_remote_entry_and_the_not_found_screen(refused, not_found, expected):
    firewall = guard()
    page = model_page(firewall, probe={"text_len": 300, "spa_not_found": not_found})
    if refused:
        await refuse(firewall, page, "https://decoy-host.pages.procore.com/remoteEntry.js")
    else:
        await refuse(firewall, page, "https://decoy-host.pages.procore.com/static/js/a.js")
    probe = await firewall.diagnostics.probe(page)
    assert firewall.diagnostics.page_code_blocked(probe) is expected
    assert firewall.diagnostics.page_code_blocked(None) is expected     # the last probe's marker


async def test_page_code_blocked_needs_the_refusal_on_the_current_document():
    # A remote refused on an earlier page never blames a later page that shows
    # the same not-found sentence (the RFIs tool's own missing-record screen).
    firewall = guard()
    firewall.nav_count = 1
    page = model_page(firewall, probe={"text_len": 300, "spa_not_found": True})
    await refuse(firewall, page, "https://decoy-host.pages.procore.com/remoteEntry.js")
    assert firewall.diagnostics.remote_entry_refused_here()
    firewall.nav_count = 2
    assert not firewall.diagnostics.remote_entry_refused_here()
    probe = await firewall.diagnostics.probe(page)
    assert probe["spa_not_found"] is True and firewall.diagnostics.remote_entry_refused == 1
    assert firewall.diagnostics.page_code_blocked(probe) is False
    assert firewall.diagnostics.page_code_blocked(None) is False
    await refuse(firewall, page, "https://decoy-host.pages.procore.com/remoteEntry.js")
    assert firewall.diagnostics.page_code_blocked(await firewall.diagnostics.probe(page)) is True


async def test_page_code_blocked_ignores_a_not_found_marker_from_an_earlier_document():
    firewall = guard()
    firewall.nav_count = 1
    page = model_page(firewall, probe={"text_len": 300, "spa_not_found": True})
    await firewall.diagnostics.probe(page)                       # marker on document 1
    firewall.nav_count = 2
    await refuse(firewall, page, "https://decoy-host.pages.procore.com/remoteEntry.js")
    assert firewall.diagnostics.page_code_blocked(None) is False   # stale marker
    assert firewall.diagnostics.page_code_blocked(await firewall.diagnostics.probe(page)) is True


async def test_remote_entry_refusals_count_model_page_pages_hosts_only():
    firewall = guard()
    page = model_page(firewall)
    await refuse(firewall, page, f"{APP}/remoteEntry.js")                                  # app host
    await refuse(firewall, page, "https://rfis-ui-service.pages.procore.com/remoteEntry.js", "fetch")
    await refuse(firewall, page, "https://rfis-ui-service.pages.procore.com/remoteEntry.js?v=1")
    assert firewall.diagnostics.remote_entry_refused == 2
    unowned = NS(main_frame=None)
    unowned.main_frame = NS(page=unowned, parent_frame=None)
    await refuse(firewall, unowned, "https://decoy.pages.procore.com/remoteEntry.js")
    assert firewall.diagnostics.remote_entry_refused == 2


# Finish tool ------------------------------------------------------------------------------------


async def blocked_true():
    return True


async def blocked_false():
    return False


async def blocked_raises():
    raise RuntimeError("probe broke with https://decoy.test")


def args(outcome, error_kind=None, summary="model summary https://decoy.test"):
    return NS(outcome=outcome, error_kind=error_kind, summary=summary)


async def test_finish_maps_a_blocked_page_to_page_code_blocked():
    ret = await FinishTool(blocked_true)(args("cannot_complete", "not_found"))
    t = ret.terminal
    assert (t.status, t.error_kind, t.error, t.summary) == ("failed", "cannot_complete", "page_code_blocked",
                                                            SUMMARY["page_code_blocked"])
    P.FinishRequest(status=t.status, error_kind=t.error_kind, error=t.error, summary=t.summary)
    assert EM_DASH not in t.summary and "http" not in t.summary


@pytest.mark.parametrize("blocked", [blocked_false, blocked_raises])
async def test_finish_keeps_the_model_result_otherwise(blocked):
    ret = await FinishTool(blocked)(args("cannot_complete", "not_found"))
    assert ret.terminal.error_kind == "not_found" and ret.terminal.error is None
    ret = await FinishTool(blocked)(args("cannot_complete"))
    assert ret.terminal.error_kind == "cannot_complete"


async def test_finish_success_and_capture_are_unchanged():
    ret = await FinishTool(blocked_true)(args("nothing_to_do"))
    assert ret.terminal.status == "succeeded" and ret.terminal.outcome == "nothing_to_do"
    with pytest.raises(ToolError):
        await FinishTool(blocked_true)(args("captured"))


class _Diag:
    def __init__(self, probe, blocked):
        self._probe, self._blocked, self.seen = probe, blocked, []

    async def probe(self, page):
        return self._probe

    def page_code_blocked(self, probe):
        self.seen.append(probe)
        return self._blocked


@pytest.mark.parametrize("blocked", [True, False])
async def test_executor_page_code_blocked_uses_a_fresh_probe(blocked):
    ex = object.__new__(UserExecutor)
    diag = _Diag({"spa_not_found": True}, blocked)
    ex.jb = NS(page=object(), guard=NS(diagnostics=diag))
    assert await ex.page_code_blocked() is blocked
    assert diag.seen == [{"spa_not_found": True}]


async def test_executor_skips_the_probe_when_nothing_was_refused_on_this_document():
    ex = object.__new__(UserExecutor)
    diag = _Diag({"spa_not_found": True}, True)
    diag.remote_entry_refused_here = lambda: False
    diag.probe = AsyncMock()
    ex.jb = NS(page=object(), guard=NS(diagnostics=diag))
    assert await ex.page_code_blocked() is False
    diag.probe.assert_not_awaited()
    assert diag.seen == []


async def test_executor_without_diagnostics_is_never_blocked():
    ex = object.__new__(UserExecutor)
    ex.jb = NS(page=object(), guard=NS())
    assert await ex.page_code_blocked() is False


def test_census_and_probe_steps_stay_valid_worker_steps():
    for kind in ("browser.resource_census", "browser.render_probe", "http.blocked"):
        assert P.worker_step_kind_allowed(kind)
    assert {"resource_type_not_allowed", "read_query_not_allowed"} <= D.REFUSAL_REASONS
    assert "ep" in D.ROW_FIELDS


@pytest.mark.parametrize("url", [SEED, f"{APP}/webclients/host/companies/{C}/projects/{PR}/tools/rfis"])
def test_hosts_in_steps_parse_as_code_fixed(url):
    firewall = guard(url)
    parts = urlsplit(url)
    assert D.host_class(firewall, parts.scheme, parts.hostname) == "app_seed"
