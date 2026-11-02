"""Worker 1.0.6 personal allowance: the hub (A1-A3, B1-B5) and RFIs tool
(R-A1, R-B1-R-B5) page code and reads. Exact hosts, GET only, exact queries,
resource types checked, modern seed only, and nothing added from responses."""
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser.guard import (
    MAX_RESPONSE_BYTES,
    PAGES_HOSTS,
    PAGES_PUBLIC_LABELS,
    PAGES_REMOTE_HOSTS,
    PAGES_SHELL_HOSTS,
    REMOTE_ENTRY_HOSTS,
    SHELL_HOST,
    QuerySpec,
    UserGuard,
    origin,
    pages_label,
)
from bdr_agent.browser.guard import Guard

C, P = "562949953430361", "562949955106559"
APP = "https://us02.procore.com"
MODERN = f"{APP}/webclients/host/companies/{C}/projects/{P}/tools/hubs/project-hub/views/overview"
RFIS_TOOL = f"{APP}/webclients/host/companies/{C}/projects/{P}/tools/rfis"
LEGACY = f"https://app.procore.com/{P}/project/rfi/list"
RESOURCE_TYPES = ("document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch",
                  "eventsource", "websocket", "manifest", "other", None)
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
RFI_LIST = ("as_datetimes=true&locale=en&serializer_view=web_index&view=list&page=1&per_page=150"
            "&sort%5Battribute%5D=position&sort%5Bdirection%5D=desc")

# Every 1.0.6 us02 read: (id, path, exact allowed query).
READS = [
    ("B1", f"/rest/v2.0/companies/{C}/projects/{P}/hubs/containers", ""),
    ("B2", f"/rest/v2.0/companies/{C}/projects/{P}/hubs/views", ""),
    ("B2q", f"/rest/v2.0/companies/{C}/projects/{P}/hubs/views", "is_personal=true"),
    ("B3", "/rest/v2.0/hubs/cards", f"company_id={C}"),
    ("B3q", "/rest/v2.0/hubs/cards", f"company_id={C}&filter_by_entitlement=true"),
    ("B4", "/rest/v1.0/settings/permissions", f"company_id={C}&project_id={P}"),
    ("B5", "/rest/v1.1/environment_metadata", f"company_id={C}&project_id={P}"),
    ("R-B1", f"/rest/v1.0/projects/{P}/procore_environment", ""),
    ("R-B2", f"/rest/v1.0/projects/{P}/client_config/rfis/", ""),
    ("R-B3", f"/{P}/project/rfi/list_client_config.json", ""),
    ("R-B4", f"/rest/v1.0/projects/{P}/configurable_field_sets",
     "types=ConfigurableFieldSet%3A%3ARfi%3A%3AHeader&include_lov_entries=false"),
    ("R-B5", f"/rest/v1.0/projects/{P}/rfis", RFI_LIST),
    ("header", f"/rest/v1.0/projects/{P}", f"company_id={C}&view=extended"),
]
READ_IDS = [r[0] for r in READS]


def guard(seed=MODERN):
    value = UserGuard(ceiling_for("user_browser_read"))
    value.bind_seed(seed)
    value.log_step = Mock()
    return value


def url_of(path, query):
    return APP + path + ("?" + query if query else "")


def page_route(firewall, url, *, rt, method="GET", main=False, login=False, response=None, nav=None):
    page = NS(main_frame=None)
    frame = NS(page=page, parent_frame=None)
    page.main_frame = frame
    if login:
        firewall.login_page = page
    else:
        firewall.model_pages.add(id(page))
    is_nav = main if nav is None else nav
    request = NS(method=method, url=url, frame=frame, resource_type=rt, headers={},
                 is_navigation_request=lambda: is_nav)
    return NS(request=request, fetch=AsyncMock(return_value=response), fulfill=AsyncMock(), abort=AsyncMock(),
              continue_=AsyncMock())


def ok_response(body=b"{}", headers=None):
    return NS(status=200, headers=headers or {"content-type": "application/json"},
              body=AsyncMock(return_value=body), dispose=AsyncMock())


def logged(firewall):
    return json.dumps([(c.args, c.kwargs) for c in firewall.log_step.call_args_list])


def blocked(firewall):
    return [c.kwargs["result"] for c in firewall.log_step.call_args_list if c.args[0] == "http.blocked"]


# Pages hosts (A1, A2, A3, R-A1) ---------------------------------------------------------


def test_host_sets_are_exact_and_never_wildcards():
    assert PAGES_REMOTE_HOSTS == {"hubs-ui-service.pages.procore.com", "project-home-ui-service.pages.procore.com",
                                  "rfis-ui-service.pages.procore.com"}
    assert PAGES_SHELL_HOSTS == {"ui-service-shell.pages.procore.com", "header-ui-service.pages.procore.com",
                                 "sidepanel-ui-service.pages.procore.com"}
    assert REMOTE_ENTRY_HOSTS == PAGES_HOSTS - {SHELL_HOST}
    assert all("*" not in h and h.count(".") == 3 for h in PAGES_HOSTS)
    assert len(PAGES_PUBLIC_LABELS) == 147 and {h.split(".")[0] for h in PAGES_HOSTS} <= PAGES_PUBLIC_LABELS


@pytest.mark.parametrize("host", sorted(REMOTE_ENTRY_HOSTS))
def test_remote_entry_is_script_only_get_on_the_modern_model_page(host):
    firewall = guard()
    url = f"https://{host}/remoteEntry.js"
    assert firewall.policy("GET", url, login=False, main=False, rtype="script")
    for rt in RESOURCE_TYPES:
        if rt != "script":
            assert firewall.policy("GET", url, login=False, main=False, rtype=rt).reason == \
                "resource_type_not_allowed"
    assert not firewall.policy("GET", url, login=True, main=False, rtype="script")
    assert not firewall.policy("GET", url, login=False, main=True, rtype="script")
    assert not firewall.policy("GET", url, login=False, main=True, rtype="document")
    for method in WRITE_METHODS:
        assert not firewall.policy(method, url, login=False, main=False, rtype="script")
    for extra in ("?v=1", "?", "#x", "?t=123", "?token=decoy-secret"):
        assert firewall.policy("GET", url + extra, login=False, main=False, rtype="script").reason == \
            "asset_query_not_allowed"
    with pytest.raises(ValueError):
        origin(url)


def test_shell_has_no_remote_entry():
    for seed in (MODERN, LEGACY):
        assert not guard(seed).policy("GET", f"https://{SHELL_HOST}/remoteEntry.js", login=False, main=False,
                                      rtype="script")


@pytest.mark.parametrize("host", sorted(PAGES_REMOTE_HOSTS))
@pytest.mark.parametrize("path,rt", [("/static/js/52086.d9903000.chunk.js", "script"),
                                     ("/static/js/__federation_expose_HubsApp.19cf96b9.chunk.js", "script"),
                                     ("/static/css/__federation_expose_HubsApp.d134735d.chunk.css", "stylesheet")])
def test_remote_static_files_pair_directory_extension_and_resource_type(host, path, rt):
    firewall = guard()
    url = f"https://{host}{path}"
    assert firewall.policy("GET", url, login=False, main=False, rtype=rt)
    for other in RESOURCE_TYPES:
        if other != rt:
            assert not firewall.policy("GET", url, login=False, main=False, rtype=other)
    assert not firewall.policy("GET", url, login=True, main=False, rtype=rt)


@pytest.mark.parametrize("path", [
    "/mf-manifest.json", "/asset-manifest.json", "/index.html", "/", "/service-worker.js", "/favicon.ico",
    "/0123456789abcdef0123456789abcdef.svg", "/static/js/52086.d9903000.chunk.js.map", "/remoteEntry.js.map",
    "/static/js/a.css", "/static/css/a.js", "/static/media/a.js", "/static/js/sub/a.js", "/remoteentry.js",
    "/RemoteEntry.js", "/remoteEntry.mjs", "/static/js/%2e%2e/a.js", "/static/js/../a.js", "/x/remoteEntry.js",
])
@pytest.mark.parametrize("host", sorted(PAGES_REMOTE_HOSTS))
def test_remote_hosts_refuse_every_other_path(host, path):
    firewall = guard()
    for rt in RESOURCE_TYPES:
        assert not firewall.policy("GET", f"https://{host}{path}", login=False, main=False, rtype=rt)


@pytest.mark.parametrize("host", sorted(PAGES_REMOTE_HOSTS))
@pytest.mark.parametrize("path", ["/static/media/icon.1a2b3c4d.svg", "/static/media/logo.1a2b3c4d.png",
                                  "/static/media/inter.1a2b3c4d.woff2", "/static/media/inter.1a2b3c4d.woff"])
def test_remote_hosts_refuse_static_media(host, path):
    # The remotes' manifests list only /static/js and /static/css.
    firewall = guard()
    for rt in RESOURCE_TYPES:
        assert not firewall.policy("GET", f"https://{host}{path}", login=False, main=False, rtype=rt)
    assert not firewall.approved_path(host, path)


@pytest.mark.parametrize("host", sorted(PAGES_SHELL_HOSTS))
def test_shell_hosts_keep_their_105_static_media(host):
    for seed in (MODERN, LEGACY):
        firewall = guard(seed)
        assert firewall.policy("GET", f"https://{host}/static/media/icon.1a2b3c4d.svg", login=False, main=False,
                               rtype="image")
        assert firewall.policy("GET", f"https://{host}/static/media/inter.1a2b3c4d.woff2", login=False,
                               main=False, rtype="font")


@pytest.mark.parametrize("seed", [MODERN, RFIS_TOOL])
@pytest.mark.parametrize("query", ["", "view=extended", f"view=extended&company_id={C}&view=extended",
                                   f"company_id={C}"])
def test_modern_project_header_read_needs_company_and_view_extended(seed, query):
    # The project-home ProjectHeader builds only ?company_id={c}&view=extended
    # (bare ?view=extended only without a company, never on a modern seed).
    verdict = guard(seed).policy("GET", url_of(f"/rest/v1.0/projects/{P}", query), login=False, main=False,
                                 rtype="fetch")
    assert verdict.reason == "read_query_not_allowed"


@pytest.mark.parametrize("seed", [LEGACY, f"https://app.procore.com/{P}/project/home"])
def test_legacy_seed_gets_no_remote_host_and_no_remote_entry(seed):
    firewall = guard(seed)
    for host in PAGES_REMOTE_HOSTS:
        for path, rt in (("/remoteEntry.js", "script"), ("/static/js/a.js", "script")):
            assert firewall.policy("GET", f"https://{host}{path}", login=False, main=False,
                                   rtype=rt).reason == "origin_not_allowed"
    for host in REMOTE_ENTRY_HOSTS & PAGES_SHELL_HOSTS:
        assert firewall.policy("GET", f"https://{host}/remoteEntry.js", login=False, main=False,
                               rtype="script").reason == "path_not_allowed"
        # The 1.0.5 shell static allowance is unchanged for legacy seeds.
        assert firewall.policy("GET", f"https://{host}/static/js/a.js", login=False, main=False, rtype="script")


@pytest.mark.parametrize("url", [
    "https://custom-configurable-fields-ui-service.pages.procore.com/remoteEntry.js",
    "https://portfolio-hub-ui-service.pages.procore.com/remoteEntry.js",
    "https://submittals-ui-service.pages.procore.com/static/js/a.js",
    "https://decoy-secret-label.pages.procore.com/remoteEntry.js",
    "https://rfis-ui-service.pages.procore.com.evil.test/remoteEntry.js",
    "https://rfis-ui-service.pages.dev/remoteEntry.js",
    "https://x.rfis-ui-service.pages.procore.com/remoteEntry.js",
    "http://rfis-ui-service.pages.procore.com/remoteEntry.js",
    "https://rfis-ui-service.pages.procore.com:443/remoteEntry.js",
    "https://u:p@rfis-ui-service.pages.procore.com/remoteEntry.js",
    "https://core.cdn.procore.com/latest/core-icons/icons.css",
    "https://translations.cdn.procore.com/en.json.br",
    f"https://app.launchdarkly.com/sdk/evalx/abc/contexts/{'e' * 40}",
    "https://api.procore.com/rest/v1.0/projects/1/rfis",
])
def test_unlisted_and_third_party_hosts_stay_refused(url):
    firewall = guard()
    for rt in RESOURCE_TYPES:
        for method in ("GET", "POST"):
            assert not firewall.policy(method, url, login=False, main=False, rtype=rt)


async def test_iframe_navigation_to_a_remote_host_is_refused_without_fetch():
    firewall = guard()
    firewall.asset_request = NS(get=AsyncMock())
    route = page_route(firewall, "https://rfis-ui-service.pages.procore.com/remoteEntry.js", rt="script", nav=True)
    route.request.frame.parent_frame = NS()
    await firewall._on_route(route)
    route.abort.assert_awaited_once()
    firewall.asset_request.get.assert_not_called()
    route.fetch.assert_not_called()


def test_pages_hosts_are_never_seeds_or_nav_hosts():
    for host in PAGES_HOSTS:
        with pytest.raises(ValueError):
            guard(f"https://{host}/webclients/host/companies/{C}/projects/{P}/tools/rfis")
        assert host not in guard().ceiling.nav_hosts and host not in guard().ceiling.resource_hosts


async def test_remote_entry_uses_the_isolated_transport_and_strips_cookies():
    firewall = guard()
    url = "https://rfis-ui-service.pages.procore.com/remoteEntry.js"
    response = ok_response(b"/* entry */", {"content-type": "application/javascript", "access-control-allow-origin": "*",
                                            "set-cookie": "decoy=1; Domain=.procore.com"})
    firewall.asset_request = NS(get=AsyncMock(return_value=response))
    route = page_route(firewall, url, rt="script")
    await firewall._on_route(route)
    firewall.asset_request.get.assert_awaited_once_with(url, max_redirects=0, timeout=30000,
                                                        headers={"Cookie": "", "Authorization": "", "Referer": ""})
    route.fetch.assert_not_called()
    assert "set-cookie" not in {k.lower() for k in route.fulfill.call_args.kwargs["headers"]}


@pytest.mark.parametrize("declared", [True, False])
async def test_remote_entry_keeps_the_8_mib_cap(declared):
    firewall = guard()
    headers = {"content-length": str(MAX_RESPONSE_BYTES + 1)} if declared else {}
    response = NS(status=200, headers=headers, body=AsyncMock(return_value=b"x" * (MAX_RESPONSE_BYTES + 1)),
                  dispose=AsyncMock())
    firewall.asset_request = NS(get=AsyncMock(return_value=response))
    route = page_route(firewall, "https://hubs-ui-service.pages.procore.com/remoteEntry.js", rt="script")
    await firewall._on_route(route)
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_called()


# us02 reads (B1-B5, R-B1-R-B5) ------------------------------------------------------------


@pytest.mark.parametrize("seed", [MODERN, RFIS_TOOL])
@pytest.mark.parametrize("rid,path,query", READS, ids=READ_IDS)
def test_each_read_is_get_fetch_or_xhr_on_the_model_page_with_its_exact_query(seed, rid, path, query):
    firewall = guard(seed)
    url = url_of(path, query)
    for rt in ("fetch", "xhr"):
        assert firewall.policy("GET", url, login=False, main=False, rtype=rt), (rid, rt)
    for rt in RESOURCE_TYPES:
        if rt not in ("fetch", "xhr"):
            assert firewall.policy("GET", url, login=False, main=False, rtype=rt).reason == \
                "resource_type_not_allowed"
    assert not firewall.policy("GET", url, login=True, main=False, rtype="fetch")
    assert not firewall.policy("GET", url, login=False, main=True, rtype="document")
    for method in WRITE_METHODS:
        assert not firewall.policy(method, url, login=False, main=False, rtype="fetch")
    assert not firewall.policy("GET", url.replace("us02.", "app."), login=False, main=False, rtype="fetch")


@pytest.mark.parametrize("rid,path,query", READS, ids=READ_IDS)
def test_each_read_refuses_extra_missing_or_changed_query_keys(rid, path, query):
    firewall = guard()

    def ok(q):
        return bool(firewall.policy("GET", url_of(path, q), login=False, main=False, rtype="fetch"))

    sep = "&" if query else ""
    assert not ok(query + sep + "extra=1")
    assert not ok(query + sep + "decoy_secret_token=abc")
    assert not ok(query + sep + "x")
    if query:
        pairs = query.split("&")
        assert not ok("&".join(pairs + pairs[:1]))                  # duplicate key
        assert not ok(query.replace("=", "=x", 1))                  # changed value
        assert not ok(query.replace(C, "99").replace(P, "98")) or C not in query and P not in query
        if rid not in ("B2q", "B3q", "R-B5"):
            assert not ok("&".join(pairs[1:]))                      # missing a required key


@pytest.mark.parametrize("rid,path,query", READS, ids=READ_IDS)
def test_reads_never_cross_company_or_project(rid, path, query):
    firewall = guard()
    swapped = path.replace(P, "123").replace(C, "456")
    q = query.replace(C, "456").replace(P, "123")
    if swapped != path or q != query:
        assert not firewall.policy("GET", url_of(swapped, q), login=False, main=False, rtype="fetch")


@pytest.mark.parametrize("seed", [LEGACY, f"https://app.procore.com/{P}/project/home",
                                  f"https://us02.procore.com/{P}/project/rfi/show/7"])
@pytest.mark.parametrize("rid,path,query", [r for r in READS if not r[0].startswith(("R-B5", "header"))],
                         ids=[r[0] for r in READS if not r[0].startswith(("R-B5", "header"))])
def test_legacy_seed_gets_none_of_the_new_reads(seed, rid, path, query):
    firewall = guard(seed)
    host = "https://" + firewall.app_host
    for any_company in (C, "1", "99999"):
        for rt in RESOURCE_TYPES:
            assert not firewall.policy("GET", host + path.replace(C, any_company) + ("?" + query if query else ""),
                                       login=False, main=False, rtype=rt), (rid, rt)


def test_legacy_seed_keeps_the_105_reads_unchanged():
    firewall = guard(LEGACY)
    for path in (f"/rest/v1.0/projects/{P}", f"/rest/v1.0/projects/{P}/rfis", f"/rest/v1.0/projects/{P}/rfis/4",
                 "/rest/v1.0/companies"):
        for query in ("", "?page=2&anything=else", "?per_page=1000"):
            assert firewall.policy("GET", "https://app.procore.com" + path + query, login=False, main=False)
            assert firewall.policy("GET", "https://app.procore.com" + path + query, login=True, main=False)
        assert not firewall.policy("GET", "https://app.procore.com" + path + "?access_token=x", login=False,
                                   main=False)


@pytest.mark.parametrize("query,allowed", [
    (RFI_LIST, True),
    ("as_datetimes=true&locale=en&serializer_view=web_index&view=list&page=1&per_page=150", True),
    ("per_page=150&page=1&view=list&serializer_view=web_index&locale=en&as_datetimes=true", True),
    (RFI_LIST + "&filters%5Bcurrent_revision%5D%5B%5D=true", True),
    (RFI_LIST + "&filters[current_revision][]=true", True),
    (RFI_LIST + "&filters%5Bstatus%5D%5B%5D=open&filters%5Bstatus%5D%5B%5D=closed", True),
    (RFI_LIST + "&filters%5Bstatus%5D%5B%5D=open&filters%5Bstatus%5D%5B%5D=open", False),
    (RFI_LIST + "&filters%5Bstatus%5D%5B%5D=private", False),
    (RFI_LIST.replace("position", "due_date").replace("desc", "asc"), True),
    (RFI_LIST.replace("position", "assignee"), False),
    (RFI_LIST.replace("desc", "sideways"), False),
    (RFI_LIST.replace("locale=en", "locale=en-US"), True),
    (RFI_LIST.replace("locale=en", "locale=zh-Hans-CN"), True),
    (RFI_LIST.replace("locale=en", "locale=es-419"), True),
    (RFI_LIST.replace("locale=en", "locale=en-specialty-contractor"), True),
    (RFI_LIST.replace("locale=en", "locale=es-%D9%A4%D9%A1%D9%A9"), False),        # Arabic-Indic digits
    (RFI_LIST.replace("locale=en", "locale=en_US"), False),
    (RFI_LIST.replace("locale=en", "locale="), False),
    (RFI_LIST.replace("page=1&", "page=9999&"), True),
    (RFI_LIST.replace("page=1&", "page=0&"), False),
    (RFI_LIST.replace("page=1&", "page=1%D9%A3&"), False),                           # U+0663
    (RFI_LIST.replace("page=1&", "page=%D9%A1&"), False),                            # U+0661
    (RFI_LIST.replace("page=1&", "page=%EF%BC%91&"), False),                         # fullwidth 1
    (RFI_LIST.replace("per_page=150", "per_page=1%D9%A0"), False),
    (RFI_LIST.replace("per_page=150", "per_page=%EF%BC%95"), False),
    (RFI_LIST.replace("per_page=150", "per_page=1%D9%A4%D9%A0"), False),
    (RFI_LIST.replace("page=1&", "page=10000&"), False),
    (RFI_LIST.replace("per_page=150", "per_page=1"), True),
    (RFI_LIST.replace("per_page=150", "per_page=149"), True),
    (RFI_LIST.replace("per_page=150", "per_page=151"), False),
    (RFI_LIST.replace("per_page=150", "per_page=1000"), False),
    (RFI_LIST.replace("view=list", "view=recycle_bin"), False),
    (RFI_LIST.replace("serializer_view=web_index", "serializer_view=full"), False),
    (RFI_LIST + "&search=decoy", False),
    (RFI_LIST + "&filters%5Bassignee_id%5D%5B%5D=4", False),
    (RFI_LIST + "&filters%5Bcost_code_id%5D%5B%5D=4", False),
    (RFI_LIST.replace("as_datetimes=true&", ""), False),
    (RFI_LIST + "&view=list", False),
    ("", False),
])
def test_rfi_list_query_rules(query, allowed):
    url = url_of(f"/rest/v1.0/projects/{P}/rfis", query)
    assert bool(guard().policy("GET", url, login=False, main=False, rtype="fetch")) is allowed


def test_query_spec_parsing_edges():
    spec = QuerySpec(required=(("a", "1"),))
    assert spec.admits("a=1") and not spec.admits("") and not spec.admits("a=1&a=1")
    assert not spec.admits("a") and not spec.admits("a=1;b=2") and not spec.admits("a=1&&")
    assert not spec.admits("a=1&b") and not spec.admits("=1&a=1") and not spec.admits("a=1+")
    assert not spec.admits("a=1&" + "&".join(f"k{i}=1" for i in range(40)))
    assert not spec.admits("a=1" + "&b=" + "x" * 3000)
    assert QuerySpec().admits("") and not QuerySpec().admits("a=1")


@pytest.mark.parametrize("method,path", [
    ("PATCH", f"/rest/v1.0/projects/{P}/rfis"),
    ("POST", f"/rest/v1.1/projects/{P}/rfis?as_datetimes=true"),
    ("PATCH", f"/rest/v1.1/projects/{P}/rfis/4?as_datetimes=true"),
    ("DELETE", f"/rest/v1.0/projects/{P}/rfis/4"),
    ("POST", "/rest/v1.0/analytic_events"),
    ("POST", f"/rest/v2.0/companies/{C}/projects/{P}/saved_views"),
    ("PUT", f"/rest/v2.0/companies/{C}/projects/{P}/saved_views/1"),
    ("DELETE", f"/rest/v2.0/companies/{C}/projects/{P}/saved_views/1"),
    ("POST", f"/rest/v2.0/companies/{C}/projects/{P}/hubs/views"),
    ("PATCH", f"/rest/v2.0/companies/{C}/projects/{P}/hubs/views"),
    ("PATCH", f"/rest/v2.0/companies/{C}/home_experience_settings"),
    ("POST", f"/rest/v2.0/companies/{C}/projects/{P}/workflows/instances"),
])
@pytest.mark.parametrize("seed", [MODERN, LEGACY])
def test_writes_stay_refused(seed, method, path):
    firewall = guard(seed)
    url = "https://" + firewall.app_host + path
    for rt in ("fetch", "xhr", None):
        for login in (False, True):
            assert not firewall.policy(method, url, login=login, main=False, rtype=rt)


@pytest.mark.parametrize("path", [
    f"/rest/v2.0/companies/{C}/projects/{P}/saved_views?table_name=list&permissions_domain=rfi&per_page=150",
    f"/rest/v2.0/companies/{C}/projects/{P}/saved_views/permissions?permissions_domain=rfi",
    f"/rest/v2.0/companies/{C}/projects/{P}/kickoff_onboarding/sidepanel/permitted?view=rfi.list",
    f"/rest/v2.0/companies/{C}/early_access/enrollees_for_user?project_id={P}",
    f"/rest/v2.0/companies/{C}/home_experience_settings",
    f"/rest/v1.0/companies/{C}/procore_environment",
    "/rest/v1.0/me",
    f"/rest/v1.0/settings/permissions?project_id={P}&filter_correspondence_types=false",
    f"/rest/v1.0/projects/{P}/rfis/filter_options/status",
    f"/rest/v1.0/projects/{P}/rfis/priorities",
    f"/rest/v1.0/projects/{P}/rfis/open_closed_chart_data",
    f"/rest/v1.0/projects/{P}/rfis/recycle_bin?view=recycle_bin",
    f"/rest/v1.0/projects/{P}/rfis/export",
    f"/rest/v2.0/companies/{C}/projects/{P}/rfis",
    f"/rest/v2.0/companies/{C}/projects/{P}/rfis/options",
    f"/rest/v2.0/companies/{C}/projects/{P}/external_rfis",
    f"/{P}/project/rfi/onboarding_list_data",
    f"/rest/v2.0/companies/{C}/projects/{P}/hubs/template_views",
    f"/{C}/company/mindtouch/help_link",
    "/cdn-cgi/challenge-platform/scripts/jsd/main.js",
    "/mf-manifest.json",
])
def test_reads_not_justified_stay_refused(path):
    firewall = guard()
    for rt in ("fetch", "xhr", "script", None):
        assert not firewall.policy("GET", APP + path, login=False, main=False, rtype=rt)


@pytest.mark.parametrize("path", [p for _rid, p, _q in READS])
def test_read_paths_are_never_navigation_targets(path):
    firewall = guard()
    assert not firewall.policy("GET", APP + path, login=False, main=True, rtype="document")


async def test_allowed_read_uses_the_page_route_fetch_not_the_isolated_transport():
    firewall = guard()
    firewall.asset_request = NS(get=AsyncMock())
    url = url_of("/rest/v1.1/environment_metadata", f"company_id={C}&project_id={P}")
    route = page_route(firewall, url, rt="fetch", response=ok_response())
    await firewall._on_route(route)
    route.fetch.assert_awaited_once_with(max_redirects=0, timeout=30000)
    firewall.asset_request.get.assert_not_called()
    route.fulfill.assert_awaited_once()


@pytest.mark.parametrize("declared", [True, False])
async def test_reads_keep_the_8_mib_cap(declared):
    firewall = guard()
    headers = {"content-length": str(MAX_RESPONSE_BYTES + 1)} if declared else {}
    response = NS(status=200, headers=headers, body=AsyncMock(return_value=b"x" * (MAX_RESPONSE_BYTES + 1)),
                  dispose=AsyncMock())
    route = page_route(firewall, url_of(f"/rest/v1.0/projects/{P}/rfis", RFI_LIST), rt="xhr", response=response)
    await firewall._on_route(route)
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_called()


async def test_refused_read_never_fetches():
    firewall = guard()
    route = page_route(firewall, url_of("/rest/v1.1/environment_metadata", f"company_id={C}"), rt="fetch",
                       response=ok_response())
    await firewall._on_route(route)
    route.fetch.assert_not_called()
    route.abort.assert_awaited_once()


async def test_nothing_is_added_from_response_data():
    """A cards or containers body naming new hosts or paths never widens the allowance."""
    firewall = guard()
    body = json.dumps({"data": [{"url": "https://decoy-card-ui-service.pages.procore.com",
                                 "remote": "https://custom-configurable-fields-ui-service.pages.procore.com",
                                 "api": f"/rest/v2.0/companies/{C}/projects/{P}/hubs/cards/decoy"}]}).encode()
    before = (firewall.read_rules, firewall.ceiling, frozenset(PAGES_HOSTS))
    route = page_route(firewall, url_of("/rest/v2.0/hubs/cards", f"company_id={C}"), rt="fetch",
                       response=ok_response(body))
    await firewall._on_route(route)
    route.fulfill.assert_awaited_once()
    assert (firewall.read_rules, firewall.ceiling, frozenset(PAGES_HOSTS)) == before
    for url, rt in (("https://decoy-card-ui-service.pages.procore.com/remoteEntry.js", "script"),
                    ("https://custom-configurable-fields-ui-service.pages.procore.com/remoteEntry.js", "script"),
                    (APP + f"/rest/v2.0/companies/{C}/projects/{P}/hubs/cards/decoy", "fetch")):
        assert not firewall.policy("GET", url, login=False, main=False, rtype=rt)


# Logging: approved paths only, code-listed host names only ---------------------------------


def test_record_block_names_listed_hosts_and_masks_everything_else():
    firewall = guard()
    firewall._record_block("GET", "https://custom-configurable-fields-ui-service.pages.procore.com/remoteEntry.js"
                                  "?decoy_q=1", "origin_not_allowed")
    firewall._record_block("GET", "https://decoy-label.pages.procore.com/remoteEntry.js", "origin_not_allowed")
    firewall._record_block("GET", "https://rfis-ui-service.pages.procore.com/remoteEntry.js?v=decoy",
                           "asset_query_not_allowed")
    firewall._record_block("GET", "https://hubs-ui-service.pages.procore.com/mf-manifest.json", "path_not_allowed")
    firewall._record_block("POST", APP + "/rest/v1.1/environment_metadata?company_id=decoy", "method_not_allowed")
    firewall._record_block("PATCH", APP + "/rest/decoy-path", "method_not_allowed")
    rows = blocked(firewall)
    assert rows[0] == {"method": "GET", "host": "custom-configurable-fields-ui-service.pages.procore.com",
                       "path": "[refused_path]", "reason": "origin_not_allowed"}
    assert rows[1] == {"method": "GET", "host": "[procore_pages_other]", "path": "[refused_path]",
                       "reason": "origin_not_allowed"}
    assert rows[2]["host"] == "rfis-ui-service.pages.procore.com" and rows[2]["path"] == "/remoteEntry.js"
    assert rows[3]["host"] == "hubs-ui-service.pages.procore.com" and rows[3]["path"] == "[refused_path]"
    assert rows[4]["path"] == "/rest/v1.1/environment_metadata"
    assert rows[5]["path"] == "[refused_path]"
    assert "decoy" not in logged(firewall) and "?" not in logged(firewall)


def test_legacy_seed_masks_modern_read_paths():
    firewall = guard(LEGACY)
    firewall._record_block("GET", f"https://app.procore.com/rest/v2.0/companies/{C}/projects/{P}/hubs/containers",
                           "path_not_allowed")
    assert blocked(firewall)[0]["path"] == "[refused_path]"
    firewall._record_block("GET", "https://rfis-ui-service.pages.procore.com/remoteEntry.js", "origin_not_allowed")
    assert blocked(firewall)[1]["path"] == "[refused_path]"


@pytest.mark.parametrize("host,label", [
    ("rfis-ui-service.pages.procore.com", "rfis-ui-service"),
    ("Custom-Configurable-Fields-UI-Service.pages.procore.com.", "custom-configurable-fields-ui-service"),
    ("decoy.pages.procore.com", None), ("a.rfis-ui-service.pages.procore.com", None),
    ("rfis-ui-service.procore.com", None), (None, None), ("", None),
])
def test_pages_label(host, label):
    assert pages_label(host) == label


async def test_decoy_secrets_never_reach_any_step():
    firewall = guard()
    firewall.asset_request = NS(get=AsyncMock(return_value=ok_response(b"/* decoy-body */")))
    urls = [
        ("https://decoy-secret-host.pages.procore.com/remoteEntry.js?token=decoy-q", "script"),
        ("https://rfis-ui-service.pages.procore.com/remoteEntry.js?code=decoy-q", "script"),
        ("https://rfis-ui-service.pages.procore.com/static/js/abc.js", "fetch"),
        (url_of("/rest/v1.1/environment_metadata", f"company_id={C}&project_id={P}&decoy_x=decoy-v"), "fetch"),
        (url_of(f"/rest/v1.0/projects/{P}/rfis", RFI_LIST + "&search=decoy-search"), "xhr"),
        (url_of(f"/rest/v1.0/projects/{P}/rfis", RFI_LIST + "&session=decoy-session"), "xhr"),
        (APP + "/rest/v9/decoy-path/decoy-token", "fetch"),
        ("https://app.launchdarkly.com/sdk/evalx/decoy-client/contexts/decoy-email", "fetch"),
    ]
    for url, rt in urls:
        await firewall._on_route(page_route(firewall, url, rt=rt, response=ok_response(b"decoy-body")))
    await firewall.diagnostics.emit(NS(url=MODERN, is_closed=lambda: True), "final")
    persisted = logged(firewall)
    assert "decoy" not in persisted and "launchdarkly.com" not in persisted


# The 1.0.5 properties hold ---------------------------------------------------------------


async def test_organization_guard_still_continues_reads_without_any_new_check():
    org = Guard(ceiling_for("rfi_from_email"))
    assert not hasattr(org, "read_rules") and not hasattr(org, "diagnostics")
    page = NS(main_frame=None)
    frame = NS(page=page, parent_frame=None)
    route = NS(request=NS(method="GET", url="https://app.procore.com/rest/v1.0/projects/1/rfis?decoy=1",
                          frame=frame, is_navigation_request=lambda: False),
               continue_=AsyncMock(), abort=AsyncMock())
    await org._on_route(route)
    assert route.continue_.await_count + route.abort.await_count == 1


def test_same_document_navigation_to_the_not_found_route_is_still_blanked():
    firewall = guard()
    page = NS(main_frame=None, url=MODERN)
    frame = NS(page=page, parent_frame=None, url=f"{APP}/webclients/host/error/not-found")
    page.main_frame = frame
    firewall.model_pages.add(id(page))
    firewall._spawn = Mock(side_effect=lambda coro: coro.close())
    firewall._on_navigated(page, frame)
    assert firewall.violations and firewall.violations[-1].page == "model"
    assert any(c.args[0] == "http.blocked" for c in firewall.log_step.call_args_list)


@pytest.mark.parametrize("url", [
    RFIS_TOOL + "?sort%5Battribute%5D=position&per_page=150",
    RFIS_TOOL + "/12",
])
def test_rfis_tool_replace_state_query_still_passes_navigation(url):
    assert guard().policy("GET", url, login=False, main=True)
