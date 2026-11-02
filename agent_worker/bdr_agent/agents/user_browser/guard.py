"""Personal transport firewall: exact origins, GET reads and intercepted redirects.

Playwright's normal continue follows redirects without re-routing. Instead,
fetch(max_redirects=0) captures each response. A checked navigation redirect
becomes an inert document; our trusted caller starts a fresh guarded GET.
No redirect Location or Refresh header is ever delivered to Chromium.
"""
import contextlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urljoin, urlsplit

from ...browser.guard import (
    BlockedRecord,
    Guard,
    NavViolation,
    Verdict,
    check_login_navigation,
    check_navigation,
    token_query_keys,
)
from ..pm_apps.procore.paths import LOGIN_LOGIN_PATHS

APP = "app.procore.com"
APP_HOSTS = frozenset({APP, "us02.procore.com"})
LOGIN = "login.procore.com"
APP_ASSET_HOSTS = frozenset(f"assets{i}.procore.com" for i in range(4))
LOGIN_ASSET_HOSTS = frozenset(f"production-login-assets{i}.cdn.procore.com" for i in range(4))
ASSET_HOSTS = APP_ASSET_HOSTS | LOGIN_ASSET_HOSTS
# The modern hub's page code. Exact hosts, model page only, GET only, never
# an identity origin, navigation seed or navigation target, always fetched
# through the isolated asset transport. Never a wildcard.
PAGES_SUFFIX = ".pages.procore.com"
SHELL_HOST = f"ui-service-shell{PAGES_SUFFIX}"
# 1.0.5: the shell and its header and sidepanel: /static files for every seed.
PAGES_SHELL_HOSTS = frozenset(f"{name}{PAGES_SUFFIX}" for name in
                              ("ui-service-shell", "header-ui-service", "sidepanel-ui-service"))
# 1.0.6: the hub, project header and RFIs tool remotes: modern seed only.
PAGES_REMOTE_HOSTS = frozenset(f"{name}{PAGES_SUFFIX}" for name in
                               ("hubs-ui-service", "project-home-ui-service", "rfis-ui-service"))
PAGES_HOSTS = PAGES_SHELL_HOSTS | PAGES_REMOTE_HOSTS
# The shell loads each remote's root /remoteEntry.js by script tag (modern
# seed only). The shell itself has no remote entry.
REMOTE_ENTRY_HOSTS = PAGES_HOSTS - {SHELL_HOST}
REMOTE_ENTRY_PATH = re.compile(r"^/remoteEntry\.js$")
PAGES_STATIC_PATH = re.compile(r"^/static/(?:js|css|media)/[A-Za-z0-9_.-]{1,200}\.(?:js|css|woff2?|png|svg)$")
# Each pages path admits only the Chromium resource types that load it.
PAGES_STATIC_KINDS = (
    (re.compile(r"^/static/js/[A-Za-z0-9_.-]{1,200}\.js$"), frozenset({"script"})),
    (re.compile(r"^/static/css/[A-Za-z0-9_.-]{1,200}\.css$"), frozenset({"stylesheet"})),
    (re.compile(r"^/static/media/[A-Za-z0-9_.-]{1,200}\.woff2?$"), frozenset({"font"})),
    (re.compile(r"^/static/media/[A-Za-z0-9_.-]{1,200}\.(?:png|svg)$"), frozenset({"image"})),
)
# The 1.0.6 remotes' manifests list only /static/js and /static/css: no media.
PAGES_REMOTE_KINDS = PAGES_STATIC_KINDS[:2]
SCRIPT_ONLY = frozenset({"script"})
READ_RESOURCE_TYPES = frozenset({"fetch", "xhr"})
# Public Procore page-host names from the shell's remote registry (ui-service-shell
# 825.9d713fd5, 147 names). Diagnostics only: a refused host whose label is in this
# list is named; every other *.pages.procore.com label stays [procore_pages_other].
# Listing a name here never allows it.
PAGES_PUBLIC_LABELS = frozenset((
    "account-domain-ui-service", "account-onboarding-ui-service", "action-plans-ui-service", "acv-ui-service",
    "admin-pages-ui-service", "analytics-ui-service", "app-management-mfe-ui-service",
    "asset-register-ui-service", "authentication-ui-service", "authorization-ui-service", "authz-ui-service",
    "bidding-admin-ui-service", "bim-model-manager-ui-service", "bim-models-ui-service",
    "bim-platform-admin-ui-service", "budget-changes-ui-service", "budgets-ui-service", "canvas-pro-ui-service",
    "capital-planning-ui-service", "change-events-ui-service", "change-history-ui-service",
    "change-orders-ui-service", "claiming-schemes-ui-service", "classifications-ui-service",
    "company-directory-ui-service", "company-timesheets-ui-service", "compliance-ui-service",
    "connection-hub-ui-service", "contextual-help-ui-service", "contracts-ui-service", "conversations-ui-service",
    "coordination-issues-ui-service", "copilot-ui-service", "correspondence-ui-service",
    "cost-modeling-ui-service", "crews-ui-service", "cris-explorer-ui-service", "currency-configuration-ui",
    "custom-configurable-fields-ui-service", "custom-reports-ui-service", "daily-log-ui-service",
    "data-extracts-ui-service", "datagrid-ui-service", "dev-studio-custom-object-schema-ui-service",
    "dev-studio-ui-service", "direct-costs-ui-service", "directory-ui-service", "doc-control-ui-service",
    "document-security-settings-ui-service", "document-sync-ui-service", "document-viewer-ui-service",
    "documents-ui-service", "drawings-ui-service", "drills-ui-service", "early-access-ui-service",
    "ecomm-ui-service", "edge-ui-service", "emails-ui-service", "embedded-insights-ui-service",
    "enhanced-reports-ui-service", "equipment-roaster-ui-service", "equipment-ui-service",
    "erp-control-plane-ui-service", "erp-ui-service", "estimating-ui-service",
    "field-production-report-ui-service", "forms-ui-service", "free-account-ui-service",
    "funding-sources-ui-service", "generic-tool-ui-service", "header-ui-service", "hello-ngx-ui-service",
    "hubs-ui-service", "i18n-script-inspect-ui-service", "i18n-translations-dashboard-ui-service",
    "incidents-ui-service", "inspections-ui-service", "intake-ui-service", "invoicing-ui-service",
    "kickoff-ui-service", "kiosk-ui-service", "kitchen-sink-ui-service", "lc-web-app-ui-service",
    "lien-rights-ui-service", "location-tool-ui-service", "locations-ui-service", "manifold-ui-service",
    "map-tool-ui-service", "material-management-ui-service", "meetings-ui-service",
    "mfe-switchboard-ui-service", "nexus-ui-service", "notifications-ui-service", "observations-ui-service",
    "open-tasks-ui-service", "optimus-prime-ui-service", "pay-documents-ui-service", "payments-ui-service",
    "permissions-ui-service", "photos-ui-service", "portfolio-hub-ui-service", "portfolio-manager-ui-service",
    "procore-connect-ui-service", "procore-ipaas-ui-service", "procurement-ui-service",
    "product-usage-ui-service", "production-tracking-ui-service", "project-directory-ui-service",
    "project-health-ui-service", "project-home-ui-service", "project-portfolio-ui-service",
    "projects-ui-service", "public-forms-ui-service", "punch-list-ui-service", "qualifications-ui-service",
    "rates-ui-service", "related-items-ui-service", "reporting-applications-ui-service", "rfis-ui-service",
    "rfqs-ui-service", "sandbox-ui-service", "sauce-viewer-poc-ui-service", "sauce-viewer-ui-service",
    "schedule-archive-ui-service", "schedule-management-ui-service", "security-mfa-ui-service",
    "sidepanel-ui-service", "skeleton-ui-service", "specifications-ui-service", "submittals-ui-service",
    "support-portal-ui-service", "tags-management-ui-service", "tasks-ui-service", "tax-ui-service",
    "test-ui-service", "time-and-materials-ui-service", "timesheets-ui-service", "tool-studio-ui-service",
    "ui-service-shell", "unified-viewer-assets-ui-service", "unified-viewer-proof-of-concept-ui-service",
    "unified-viewer-ui-service", "unit-quantity-based-budget-ui-service", "uom-ui-service", "wbs-ui-service",
    "webhooks-ui-service", "workflows-ui-service",
))
# Owner decision 2026-10-29: the RFIs tool's LaunchDarkly flag GET is answered
# here, on this PC, with a fixed empty flag set. Nothing is ever sent to
# LaunchDarkly: no route.fetch, no continue, no asset_request. The SDK builds
# exactly [baseUrl, "/sdk/evalx/", clientSideId, "/contexts/", base64url(context)]
# with no query (rfis-ui-service 94676@1527695: no hash option, evaluationReasons
# and useReport off, 94676@1584485 passes only sendLDHeaders:false). Client-side
# ids are 24 lowercase hex (52086@13323) and base64URLEncode strips "=" and maps
# "+/" to "-_" (94676@1504254). The context segment carries the user's email, so
# this URL never enters a step; the census records only the fixed verdict below.
# The policy itself still refuses every LaunchDarkly URL (events POST, REPORT,
# clientstream, goals, other hosts and paths): only this exact GET is stubbed.
FLAGS_HOST = "app.launchdarkly.com"
FLAGS_EVALX_URL = re.compile(r"https://app\.launchdarkly\.com/sdk/evalx/[0-9a-f]{24}/contexts/[A-Za-z0-9_-]{1,8192}")
FLAGS_STUB_BODY = "{}"
FLAGS_STUB_VERDICT = "stubbed:flags_empty"
STATIC_PATH = re.compile(r"^/(?:assets|packs|static|webpack)/[A-Za-z0-9_./-]+\.(?:js|css|woff2?|png|jpg|svg|ico)$")
MFE_SCRIPT_PATH = re.compile(r"^/mfe_erb/[0-9a-f]{1,64}/static/js/[A-Za-z0-9_.-]{1,240}\.js$")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 8
MAX_MAIN_DIAGNOSTICS = 20


def transport_error_category(error: Exception) -> str:
    """Classify only the bounded primary error, never Playwright's call log.

    Its call log can contain request/response headers and credentials. Return
    a code-fixed enum; neither the primary line nor any suffix is persisted.
    """
    primary = str(error).split("\n", 1)[0][:512].strip().lower()
    primary = re.sub(r"^(?:route\.(?:fetch|fulfill)|apirequestcontext\.(?:get|fetch|_inner_fetch)):\s*", "", primary)
    if primary in {"parse error: header overflow", "hpe_header_overflow"}:
        return "header_overflow"
    if primary in {"parse error: invalid header value char", "parse error: invalid header token",
                   "hpe_invalid_header_token"} or re.fullmatch(
            r'(?:invalid character in header content|header name must be a valid http token) \["[^"\r\n]{1,100}"\]', primary):
        return "invalid_header"
    if type(error).__name__ == "TimeoutError" or re.fullmatch(
            r"(?:request timed out after \d+ms|timeout (?:of )?\d+ms exceeded)\.?", primary):
        return "timeout"
    if primary in {"certificate has expired", "certificate is not yet valid", "self-signed certificate",
                   "self-signed certificate in certificate chain", "unable to verify the first certificate",
                   "unable to get local issuer certificate"} or re.match(
            r"^(?:hostname/ip does not match certificate's altnames:|(?:write )?eproto\b)", primary):
        return "tls"
    if primary == "socket hang up" or re.match(
            r"^(?:connect|read|write|getaddrinfo) (?:econnreset|econnrefused|ehostunreach|enetunreach|enotfound|eai_again|epipe)\b", primary):
        return "connection"
    if re.match(r"^failed to decompress '(?:gzip|deflate|br)' encoding:", primary):
        return "decompression"
    if primary.startswith("parse error: "):
        return "parse"
    return "other"


def pages_label(host: str | None) -> str | None:
    """The fixed public label of a *.pages.procore.com host, else None."""
    host = (host or "").lower().rstrip(".")
    if not host.endswith(PAGES_SUFFIX):
        return None
    label = host[:-len(PAGES_SUFFIX)]
    return label if label in PAGES_PUBLIC_LABELS or host in PAGES_HOSTS else None


@dataclass(frozen=True)
class QuerySpec:
    """One exact query shape. Keys map to an exact value, a fixed enum
    (frozenset) or a bounded pattern. Only `repeat` keys may appear twice
    (distinct values); any unknown key, missing required key, blank pair or
    bad value refuses. An empty spec admits only an empty query."""
    required: tuple[tuple[str, object], ...] = ()
    optional: tuple[tuple[str, object], ...] = ()
    repeat: frozenset = frozenset()

    def admits(self, query: str) -> bool:
        if not query:
            return not self.required
        if len(query) > 2048:
            return False
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        except ValueError:
            return False
        rules = dict(self.optional)
        rules.update(self.required)
        seen: dict[str, list[str]] = {}
        for key, value in pairs:
            if key not in rules or not _value_ok(rules[key], value):
                return False
            values = seen.setdefault(key, [])
            if values and (key not in self.repeat or value in values):
                return False
            values.append(value)
        return all(key in seen for key, _rule in self.required)


def _value_ok(rule, value: str) -> bool:
    if isinstance(rule, str):
        return value == rule
    if isinstance(rule, frozenset):
        return value in rule
    return bool(rule.fullmatch(value))


EMPTY_QUERY = QuerySpec()


@dataclass(frozen=True)
class ReadRule:
    """One app-host GET read. `queries` None is a 1.0.5 rule (any query
    without token keys, any page, any resource type). Otherwise a 1.0.6
    strict read: model page, fetch or xhr only, query admitted by one spec."""
    path: re.Pattern
    queries: tuple[QuerySpec, ...] | None = None

    def check(self, query: str, *, login: bool, rtype: str | None) -> Verdict:
        if self.queries is None:
            return Verdict(True)
        if login:
            return Verdict(False, "path_not_allowed")
        if rtype not in READ_RESOURCE_TYPES:
            return Verdict(False, "resource_type_not_allowed")
        if not any(spec.admits(query) for spec in self.queries):
            return Verdict(False, "read_query_not_allowed")
        return Verdict(True)


# Column attributes the RFIs list may sort by (rfis-ui-service columnAttributeMapper).
RFI_SORT_ATTRIBUTES = frozenset({"position", "revision", "subject", "status", "due_date", "initiated_at",
                                 "time_resolved", "priority", "schedule_impact", "cost_impact", "private",
                                 "overdue"})
RFI_STATUS_FILTERS = frozenset({"draft", "open", "closed", "closed_draft", "closed_with_revision"})
RFI_LIST_QUERY = QuerySpec(
    required=(("as_datetimes", "true"), ("serializer_view", "web_index"), ("view", "list"),
              # ASCII classes only: \d would admit any Unicode digit after parse_qsl decodes.
              ("page", re.compile(r"[1-9][0-9]{0,3}")), ("per_page", re.compile(r"(?:[1-9][0-9]?|1[0-4][0-9]|150)")),
              # Digit subtags too (es-419).
              ("locale", re.compile(r"[a-z]{2,6}(?:-[A-Za-z0-9]{2,24}){0,2}"))),
    optional=(("sort[attribute]", RFI_SORT_ATTRIBUTES), ("sort[direction]", frozenset({"asc", "desc"})),
              ("filters[current_revision][]", "true"), ("filters[status][]", RFI_STATUS_FILTERS)),
    repeat=frozenset({"filters[status][]"}),
)


def modern_read_rules(company: str, project: str) -> tuple[ReadRule, ...]:
    """1.0.6 reads, both ids bound from a modern seed. First path match wins."""
    c, p = re.escape(company), re.escape(project)

    def strict(pattern: str, *queries: QuerySpec) -> ReadRule:
        return ReadRule(re.compile(pattern), queries or (EMPTY_QUERY,))

    return (
        # 1.0.5 reads, unchanged.
        ReadRule(re.compile(r"^/rest/v1\.0/companies$")),
        ReadRule(re.compile(rf"^/rest/v1\.0/projects/{p}/rfis/\d{{1,20}}$")),
        # 1.0.5 reads, now with exact queries (project header, RFIs list).
        strict(rf"^/rest/v1\.0/projects/{p}$",
               QuerySpec(required=(("company_id", company), ("view", "extended")))),
        strict(rf"^/rest/v1\.0/projects/{p}/rfis$", RFI_LIST_QUERY),
        # Shell and hub (B1 to B5).
        strict(rf"^/rest/v2\.0/companies/{c}/projects/{p}/hubs/containers$"),
        strict(rf"^/rest/v2\.0/companies/{c}/projects/{p}/hubs/views$",
               QuerySpec(optional=(("is_personal", "true"),))),
        strict(r"^/rest/v2\.0/hubs/cards$",
               QuerySpec(required=(("company_id", company),), optional=(("filter_by_entitlement", "true"),))),
        strict(r"^/rest/v1\.0/settings/permissions$",
               QuerySpec(required=(("company_id", company), ("project_id", project)))),
        strict(r"^/rest/v1\.1/environment_metadata$",
               QuerySpec(required=(("company_id", company), ("project_id", project)))),
        # RFIs tool (R-B1 to R-B4).
        strict(rf"^/rest/v1\.0/projects/{p}/procore_environment$"),
        strict(rf"^/rest/v1\.0/projects/{p}/client_config/rfis/$"),
        strict(rf"^/{p}/project/rfi/list_client_config\.json$"),
        strict(rf"^/rest/v1\.0/projects/{p}/configurable_field_sets$",
               QuerySpec(required=(("types", "ConfigurableFieldSet::Rfi::Header"),
                                   ("include_lov_entries", "false")))),
    )


def legacy_read_rules(project: str) -> tuple[ReadRule, ...]:
    """1.0.5 reads only. A legacy seed binds no company, so it gets no 1.0.6 read."""
    p = re.escape(project)
    return (
        ReadRule(re.compile(r"^/rest/v1\.0/companies$")),
        ReadRule(re.compile(rf"^/rest/v1\.0/projects/{p}(?:/rfis(?:/\d{{1,20}})?)?$")),
    )


def origin(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in APP_HOSTS | {LOGIN} or parts.port is not None
            or parts.username is not None or parts.password is not None):
        raise ValueError("Personal browser origin refused")
    return "https://" + parts.hostname


class UserGuard(Guard):
    # Separate APIRequestContext: never the browser context's authenticated jar.
    asset_request = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .diagnostics import RenderDiagnostics
        self.diagnostics = RenderDiagnostics(self)

    def _adopt(self, page, *, login):
        super()._adopt(page, login=login)
        # Diagnostics only: counts, never page text or URLs. Console and page
        # errors describe the model page, so the login page does not add them.
        page.on("requestfinished", self.diagnostics.request_finished)
        page.on("requestfailed", self.diagnostics.request_failed)
        if not login:
            page.on("console", self.diagnostics.console)
            page.on("pageerror", self.diagnostics.page_error)

    def bind_seed(self, url: str):
        seed_origin = origin(url)
        if urlsplit(seed_origin).hostname not in APP_HOSTS or not check_navigation(self.ceiling, url):
            raise ValueError("Personal browser destination refused")
        self.app_host = urlsplit(seed_origin).hostname
        path = urlsplit(url).path
        old = re.fullmatch(r"/(\d{1,20})/project/(?:home|rfi(?:/list)?|rfi/show/\d{1,20})/?", path)
        new = re.fullmatch(r"/webclients/host/companies/(\d{1,20})/projects/(\d{1,20})/tools/(?:projecthome|hubs/project-hub/views/overview|rfis(?:/\d{1,20})?)/?", path)
        if old:
            project, company = old[1], r"\d{1,20}"
        elif new:
            company, project = new[1], new[2]
        else:
            raise ValueError("Personal browser destination refused")
        # Only a modern seed binds a company: every 1.0.6 read and remote needs it.
        self.modern = bool(new)
        self.company = company if new else None
        self.project = project
        nav_paths = [
            re.compile(rf"^/webclients/host/companies/{company}/projects/{project}/tools/(projecthome|hubs/project-hub/views/overview|rfis(/\d{{1,20}})?)/?$"),
        ]
        # A modern seed approves both company and project. A legacy URL drops
        # the company component, so it cannot be an equivalent navigation.
        if old:
            nav_paths.append(re.compile(rf"^/{project}/project/(home|rfi(/list)?|rfi/show/\d{{1,20}})/?$"))
        from dataclasses import replace
        self.ceiling = replace(self.ceiling, nav_hosts=(self.app_host,),
                               resource_hosts=(self.app_host, LOGIN), nav_paths=tuple(nav_paths))
        self.read_rules = modern_read_rules(company, project) if new else legacy_read_rules(project)
        # Path patterns only, for diagnostics (path family rest_read_ok).
        self.read_paths = tuple(rule.path for rule in self.read_rules)
        self.redirects = {}
        self._main_diagnostics = 0

    def pages_types(self, host, path):
        """The resource types a pages host path admits, or None when refused."""
        modern = getattr(self, "modern", False)
        if host not in PAGES_HOSTS or (host in PAGES_REMOTE_HOSTS and not modern):
            return None
        if REMOTE_ENTRY_PATH.fullmatch(path):
            return SCRIPT_ONLY if modern and host in REMOTE_ENTRY_HOSTS else None
        kinds = PAGES_REMOTE_KINDS if host in PAGES_REMOTE_HOSTS else PAGES_STATIC_KINDS
        for pattern, types in kinds:
            if pattern.fullmatch(path):
                return types
        return None

    def approved_path(self, host, path) -> bool:
        """May this path enter a step? Only when some page, frame and resource
        type of this job could load it (no query is ever considered)."""
        if host in PAGES_HOSTS:
            return self.pages_types(host, path) is not None
        candidate = "https://" + host + path
        if (self.policy("GET", candidate, login=True, main=True)
                or self.policy("GET", candidate, login=True, main=False)
                or self.policy("GET", candidate, login=False, main=False)):
            return True
        return host == getattr(self, "app_host", None) and any(
            rule.path.fullmatch(path) for rule in getattr(self, "read_rules", ()))

    def policy(self, method, url, *, login, main, rtype=None):
        try:
            parts = urlsplit(url)
            if parts.hostname in ASSET_HOSTS | PAGES_HOSTS:
                if (parts.scheme != "https" or parts.port is not None or parts.username is not None
                        or parts.password is not None):
                    return Verdict(False, "origin_not_allowed")
            else:
                origin(url)
        except ValueError:
            return Verdict(False, "origin_not_allowed")
        if ("\\" in url or any(ord(c) <= 32 for c in url)) or "%" in parts.path or ".." in parts.path:
            return Verdict(False, "path_not_allowed")
        if parts.hostname in PAGES_HOSTS:
            if login or (parts.hostname in PAGES_REMOTE_HOSTS and not getattr(self, "modern", False)):
                return Verdict(False, "origin_not_allowed")
            if main or method != "GET":
                return Verdict(False, "asset_request_not_allowed")
            if parts.query or parts.fragment or "?" in url or "#" in url:
                return Verdict(False, "asset_query_not_allowed")
            types = self.pages_types(parts.hostname, parts.path)
            if types is None:
                return Verdict(False, "path_not_allowed")
            return Verdict(rtype in types, "resource_type_not_allowed")
        if parts.hostname in ASSET_HOSTS:
            if main or method != "GET":
                return Verdict(False, "asset_request_not_allowed")
            if parts.hostname in LOGIN_ASSET_HOSTS and not login:
                return Verdict(False, "origin_not_allowed")
            if parts.query or parts.fragment:
                return Verdict(False, "asset_query_not_allowed")
            valid_path = STATIC_PATH.fullmatch(parts.path) or (
                parts.hostname in APP_ASSET_HOSTS and MFE_SCRIPT_PATH.fullmatch(parts.path))
            return Verdict(bool(valid_path and len(parts.path) <= 1024), "path_not_allowed")
        if parts.hostname != getattr(self, "app_host", None) and not (login and parts.hostname == LOGIN):
            return Verdict(False, "origin_not_allowed")
        if main:
            check = check_login_navigation if login else check_navigation
            allowed = check(self.ceiling, url)
            if not allowed:
                return allowed
        if method == "POST":
            return Verdict(bool(login and parts.hostname == LOGIN
                                and any(p.fullmatch(parts.path) for p in LOGIN_LOGIN_PATHS)), "method_not_allowed")
        if method != "GET":
            return Verdict(False, "method_not_allowed")
        if not login and token_query_keys(url):
            return Verdict(False, "token_query")
        if main:
            return Verdict(True)
        if STATIC_PATH.fullmatch(parts.path):
            return Verdict(True)
        if parts.hostname == self.app_host:
            for rule in getattr(self, "read_rules", ()):
                if rule.path.fullmatch(parts.path):
                    return rule.check(parts.query, login=login, rtype=rtype)
        if login and parts.hostname == LOGIN and any(p.fullmatch(parts.path) for p in LOGIN_LOGIN_PATHS):
            return Verdict(True)
        return Verdict(False, "path_not_allowed")

    def flags_stub_origin(self, request, *, page, login, main, method, url, rtype) -> str | None:
        """The requesting page origin when this request is the RFIs tool's
        LaunchDarkly flag GET to answer locally, else None (normal policy).

        Only a modern seed's model page, never the login page, never any
        navigation, GET only, fetch or xhr only, the exact URL shape with no
        port, userinfo, query or fragment, from a frame on the seed app host."""
        if (login or main or page is None or id(page) not in self.model_pages or not getattr(self, "modern", False)
                or method != "GET" or rtype not in READ_RESOURCE_TYPES or not isinstance(url, str)
                or not FLAGS_EVALX_URL.fullmatch(url)):
            return None
        try:
            if request.is_navigation_request():
                return None
            parts = urlsplit(request.frame.url)
            if (parts.scheme != "https" or parts.hostname != getattr(self, "app_host", None)
                    or parts.port is not None or parts.username is not None or parts.password is not None):
                return None
        except Exception:
            return None
        return "https://" + parts.hostname

    async def _fulfil_flags(self, route, request, page, allow_origin):
        """Answer the flag GET with a fixed empty flag set. No network request."""
        headers = {"content-type": "application/json", "access-control-allow-origin": allow_origin,
                   "cache-control": "no-store"}
        self.diagnostics.record(request, page=page, login=False, verdict=FLAGS_STUB_VERDICT, status=200,
                                headers=headers, size=len(FLAGS_STUB_BODY))
        try:
            await route.fulfill(status=200, headers=headers, body=FLAGS_STUB_BODY)
        except Exception:
            # Page closed mid-flight: abort, never fall back to the network.
            with contextlib.suppress(Exception):
                await route.abort("blockedbyclient")

    def _record_block(self, method, url, reason):
        # The inherited logger records arbitrary refused host/path strings.
        # Personal pages can put credentials there, so only approved locations
        # may enter even the encrypted trace. Query strings are always omitted.
        host, path = "[unapproved_origin]", "[refused_path]"
        try:
            parts = urlsplit(url)
            if (parts.scheme == "https" and parts.hostname in APP_HOSTS | {LOGIN} | ASSET_HOSTS | PAGES_HOSTS
                    and parts.port is None and parts.username is None and parts.password is None):
                host = parts.hostname
                if self.approved_path(host, parts.path):
                    path = parts.path[:512]
            elif (parts.hostname or "").endswith(PAGES_SUFFIX):
                # A subdomain label can carry data: only a code-listed public
                # name is emitted (never its path); every other shares one label.
                label = pages_label(parts.hostname)
                host = label + PAGES_SUFFIX if label is not None else "[procore_pages_other]"
        except ValueError:
            pass
        method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
                                      "CONNECT", "TRACE", "NAVIGATE", "REDIRECT"} else "OTHER"
        key = (host, method)
        rec = self.blocked.get(key)
        if rec is not None:
            rec.count += 1
            return
        self.blocked[key] = BlockedRecord(method, host, path, reason)
        self._step("http.blocked", f"Blocked {method} {host}{path} ({reason})", ok=False,
                   result={"method": method, "host": host, "path": path, "reason": reason})

    def _main_failure(self, method, url, *, login, stage, reason, status=None, size=None, error=None):
        """Main-page evidence must survive subresource deduplication. No bodies,
        query strings, headers or raw exception messages are persisted."""
        count = getattr(self, "_main_diagnostics", 0)
        if count >= MAX_MAIN_DIAGNOSTICS:
            return
        self._main_diagnostics = count + 1
        try:
            parts = urlsplit(url)
            host = parts.hostname if parts.hostname in APP_HOSTS | {LOGIN} else "[unapproved_origin]"
            # A rejected redirect may put a token in its path or hostname.
            # Record only a path that independently passes this job's ceiling.
            candidate = origin(url) + parts.path
            path = parts.path[:512] if self.policy("GET", candidate, login=login, main=True) else "[refused_path]"
        except ValueError:
            host, path = "[unapproved_origin]", "[refused_path]"
        result = {"method": method, "host": host, "path": path, "login": login,
                  "stage": stage, "reason": reason}
        if status is not None:
            result["status"] = status
        if size is not None:
            result["bytes"] = size
        if error is not None:
            name = type(error).__name__
            safe_name = name if name in {"Error", "TimeoutError", "RuntimeError", "ValueError",
                                        "OSError", "ConnectionError", "TypeError"} else "Exception"
            result.update(error_category=transport_error_category(error), error_type=safe_name)
        self._step("nav.failed", f"Personal main navigation refused ({stage}: {reason})", ok=False, result=result)

    async def _on_route(self, route):
        request = route.request
        page = self._request_page(request)
        login = self.is_login_page(page)
        main = self._is_main_frame_navigation(request)
        method, url = request.method.upper(), request.url
        asset = urlsplit(url).hostname in ASSET_HOSTS | PAGES_HOSTS
        census = self.diagnostics
        try:
            rtype = request.resource_type
        except Exception:
            rtype = None
        rtype = rtype if isinstance(rtype, str) else None
        if page is None or (not login and id(page) not in self.model_pages):
            verdict = Verdict(False, "page_not_owned")
        elif asset and request.is_navigation_request():
            verdict = Verdict(False, "asset_navigation_not_allowed")
        else:
            allow_origin = self.flags_stub_origin(request, page=page, login=login, main=main, method=method,
                                                  url=url, rtype=rtype)
            if allow_origin is not None:
                await self._fulfil_flags(route, request, page, allow_origin)
                return
            verdict = self.policy(method, url, login=login, main=main, rtype=rtype)
        if not verdict:
            census.record(request, page=page, login=login, verdict="refused:" + (verdict.reason or "refused"))
            self._record_block(method, url, verdict.reason or "refused")
            if main:
                self._main_failure(method, url, login=login, stage="policy", reason=verdict.reason or "refused")
            await route.abort("blockedbyclient")
            return
        response = None
        stage, status = "fetch", None
        try:
            if asset:
                if self.asset_request is None:
                    raise RuntimeError("Isolated asset transport is unavailable")
                response = await self.asset_request.get(url, max_redirects=0, timeout=30_000,
                    headers={"Cookie": "", "Authorization": "", "Referer": ""})
            else:
                response = await route.fetch(max_redirects=0, timeout=30_000)
            stage = "headers"
            headers = dict(response.headers)
            status = response.status
            if 300 <= status < 400:
                target = urljoin(url, headers.get("location", ""))
                allowed = self.policy("GET", target, login=login, main=True)
                if (not main or status not in (301, 302, 303, 307, 308) or not headers.get("location")
                        or (method == "POST" and status in (307, 308)) or not allowed):
                    census.record(request, page=page, login=login, verdict="redirect_refused", status=status,
                                  headers=headers)
                    self._record_block("REDIRECT", target, "redirect_refused")
                    if main:
                        self._main_failure(method, target, login=login, stage="redirect", reason="redirect_refused", status=status)
                    await route.abort("blockedbyclient")
                    return
                self.redirects[id(page)] = target
                # Preserve cookies from the allowed origin, never deliver a
                # redirect header which Chromium could follow behind our guard.
                safe = {k: v for k, v in headers.items() if k.lower() == "set-cookie"}
                safe["content-type"] = "text/html"
                safe["content-security-policy"] = "default-src 'none'; base-uri 'none'; form-action 'none'"
                stage = "redirect_fulfill"
                census.record(request, page=page, login=login, verdict="redirect_followed", status=status,
                              headers=headers)
                await route.fulfill(status=200, headers=safe, body="<!doctype html><title>Continuing</title>")
                return
            if headers.get("content-length", "").isdigit() and int(headers["content-length"]) > MAX_RESPONSE_BYTES:
                census.record(request, page=page, login=login, verdict="too_large", status=status, headers=headers,
                              size=int(headers["content-length"]))
                self._record_block(method, url, "response_too_large")
                if main:
                    self._main_failure(method, url, login=login, stage="headers", reason="response_too_large",
                                       status=status, size=int(headers["content-length"]))
                await route.abort("blockedbyclient")
                return
            stage = "read_body"
            data = await response.body()
            if len(data) > MAX_RESPONSE_BYTES:
                census.record(request, page=page, login=login, verdict="too_large", status=status, headers=headers,
                              size=len(data))
                self._record_block(method, url, "response_too_large")
                if main:
                    self._main_failure(method, url, login=login, stage="read_body", reason="response_too_large",
                                       status=status, size=len(data))
                await route.abort("blockedbyclient")
                return
            upstream = headers
            headers = {k: v for k, v in upstream.items()
                       if k.lower() not in ("location", "refresh", "content-encoding", "content-length")
                       and not (asset and k.lower() == "set-cookie")}
            stage = "fulfill"
            census.record(request, page=page, login=login, verdict="fulfilled", status=status,
                          headers=upstream, size=len(data))
            await route.fulfill(status=status, headers=headers, body=data)
        except Exception as exc:
            census.record(request, page=page, login=login, verdict="transport:" + transport_error_category(exc),
                          status=status)
            self._record_block(method, url, "transport_failed")
            if main:
                self._main_failure(method, url, login=login, stage=stage, reason="transport_failed", status=status, error=exc)
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass
        finally:
            if response is not None:
                await response.dispose()

    async def follow_redirects(self, page):
        for _ in range(MAX_REDIRECTS):
            target = self.redirects.pop(id(page), None)
            if target is None:
                return
            if self.navigations_left <= 0 or not self.policy("GET", target, login=self.is_login_page(page), main=True):
                raise ValueError("Personal browser redirect refused")
            await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        if self.redirects.pop(id(page), None) is not None:
            raise ValueError("Personal browser redirect limit reached")

    def _on_navigated(self, page, frame):
        same_document = id(page) not in self._doc_loads
        reported = len(self.violations)
        super()._on_navigated(page, frame)
        if page is self.login_page or getattr(frame, "parent_frame", None) is not None:
            return
        verdict = check_navigation(self.ceiling, frame.url)
        if not verdict:
            # Kept strict; the count shows whether a client-side route was blanked.
            if same_document:
                self.diagnostics.spa_blanked(frame.url)
            if len(self.violations) == reported:
                # The base check let this through: report the blank as a block, not a page that never rendered.
                self.violations.append(NavViolation(frame.url, verdict.reason or "path_not_allowed", "model"))
            self._record_block("NAVIGATE", frame.url, "path_not_allowed")
            self._spawn(self._blank(page))
