"""Personal render diagnostics: a bounded resource census and a render probe.

Answers "why is the page blank?" without persisting anything the page or a
URL could carry. Every field is a code-fixed enum or a clamped integer:

  - a census row per (page kind, resource type, host class, path family,
    endpoint template, extension, query kind, method, guard verdict, status
    class, content type, size bucket, Origin/ACAO/CORP classes, Chromium
    outcome) with a count and the number of distinct hosts and URLs behind
    it (counted in memory from digests; no host or URL is kept or emitted);
  - console errors and warnings counted by category from the first line of
    the text, classified in memory and never retained; never msg.location;
    info lines are counted only when they match a module loader pattern;
  - page errors counted by a name/message enum (a page can name its errors);
  - CSP violations counted by directive enum;
  - a render probe: numbers and booleans from one bounded page.evaluate,
    including spa_not_found (an in-page match of Procore's fixed not-found
    sentence; no text leaves the page).

Host classes name only code-fixed labels: the allowed pages hosts, or a
refused one from the shell's public registry list; any other pages label is
procore_pages_other. Endpoint templates come from a code-fixed list with the
seed ids as {c} and {p} and other numbers as {id}; never a query value.

No raw URL, host outside the fixed classes, query, header value, cookie,
console or page text, DOM text or title ever enters a step. Organization
jobs never construct this; only UserGuard does.
"""
import asyncio
import functools
import hashlib
import json
import logging
import math
import re
from typing import Any
from urllib.parse import urlsplit

from ...browser.guard import token_query_keys
from ..pm_apps.procore.paths import LOGIN_LOGIN_PATHS
from .guard import (
    APP_ASSET_HOSTS,
    APP_HOSTS,
    FLAGS_STUB_VERDICT,
    LOGIN,
    LOGIN_ASSET_HOSTS,
    MFE_SCRIPT_PATH,
    PAGES_HOSTS,
    PAGES_SUFFIX,
    STATIC_PATH,
    pages_label,
)

log = logging.getLogger("bdr_agent.agents.user_browser.diagnostics")

TRIGGERS = ("navigate", "empty_read", "final")
TRIGGER_ALIASES = {"nav": "navigate", "read_empty": "empty_read"}
MAX_DIAG_STEPS = 16          # census + probe steps per job
FINAL_RESERVE = 2            # always room for the final census and probe
MAX_CENSUS_KEYS = 160        # distinct rows per window; the rest count in keys_overflow
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_PENDING = 2048           # routed requests awaiting a Chromium outcome
MAX_EVENTS = 1000            # console and page errors per job
COUNT_CAP = 100_000
MAX_REFUSED_NAVS = 256       # distinct model-page documents with a refused remote entry
PROBE_TIMEOUT_S = 3
PROBE_INT_MAX = 10_000_000
READABLE_MIN_BOXES = 5
CONSOLE_LINE_MAX = 512
DISTINCT_CAP = 256           # distinct hosts / URLs counted per row (saturates)
# Procore's fixed not-found sentence (ui-service-shell procoreHost.pageNotFound).
NOT_FOUND_TEXT = "Sorry, this page may have been moved or deleted"

PAGES_LABELS = frozenset(host.removesuffix(PAGES_SUFFIX) for host in PAGES_HOSTS)
# Code-fixed third-party tags: (tag, registrable suffixes).
THIRD_PARTY = (
    ("launchdarkly", ("launchdarkly.com",)),
    ("datadog", ("datadoghq.com", "datadoghq.eu", "browser-intake-datadoghq.com",
                 "datadoghq-browser-agent.com")),
    ("pendo", ("pendo.io",)),
    ("segment", ("segment.com", "segment.io")),
    ("sentry", ("sentry.io", "sentry-cdn.com")),
    ("newrelic", ("newrelic.com", "nr-data.net")),
    ("split", ("split.io",)),
    ("fullstory", ("fullstory.com",)),
    ("google", ("google.com", "googleapis.com", "gstatic.com", "googletagmanager.com",
                "google-analytics.com", "doubleclick.net")),
    ("cloudfront", ("cloudfront.net",)),
    ("amazonaws", ("amazonaws.com",)),
    ("cloudflare", ("cloudflare.com", "cloudflareinsights.com")),
    ("pages_dev", ("pages.dev",)),
    ("fastly", ("fastly.net",)),
)
RESOURCE_TYPES = frozenset({"document", "stylesheet", "image", "media", "font", "script", "texttrack",
                            "xhr", "fetch", "eventsource", "websocket", "manifest", "other"})
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
REFUSAL_REASONS = frozenset({
    "page_not_owned", "asset_navigation_not_allowed", "origin_not_allowed", "path_not_allowed",
    "asset_request_not_allowed", "asset_query_not_allowed", "method_not_allowed", "token_query",
    "host_not_allowed", "scheme_not_allowed", "credentials_in_url", "port_not_allowed", "bad_url",
    "resource_type_not_allowed", "read_query_not_allowed", "refused"})
TRANSPORT_CATEGORIES = frozenset({"header_overflow", "invalid_header", "timeout", "tls", "connection",
                                  "decompression", "parse", "other"})
# FLAGS_STUB_VERDICT: the LaunchDarkly flag GET answered locally (guard.py), never sent.
VERDICTS = frozenset({"fulfilled", "redirect_followed", "redirect_refused", "too_large", FLAGS_STUB_VERDICT})

_EXT = {"js": "js", "mjs": "mjs", "css": "css", "json": "json", "map": "map",
        "woff": "font", "woff2": "font", "ttf": "font", "otf": "font", "eot": "font",
        "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "svg": "image",
        "ico": "image", "webp": "image", "avif": "image", "html": "html_none", "htm": "html_none"}
# Path families, first match wins. Entries flagged "app" apply only to the
# app hosts, "pages" only to *.pages.procore.com.
_FAMILIES = (
    ("legacy_project", None, re.compile(r"^/\d{1,20}/project(?:/|$)")),
    ("webclients_static", None, re.compile(r"^/webclients/[\w-]+/(?:static|assets|build|dist)/")),
    ("webclients_manifest", None, re.compile(r"^/webclients/.*\.json$")),
    ("webclients_doc", None, re.compile(r"^/webclients/host/companies/")),
    ("webclients_other", None, re.compile(r"^/webclients(?:/|$)")),
    ("mfe_erb_js_ok", None, MFE_SCRIPT_PATH),
    ("mfe_erb_other", None, re.compile(r"^/mfe_erb(?:/|$)")),
    ("mfe_other", None, re.compile(r"^/mfe[_/-]")),
    ("root_remote_entry", None, re.compile(r"^/remoteEntry\.js$")),
    ("mf_manifest", "pages", re.compile(r"\.json$")),
    ("favicon_manifest", None, re.compile(
        r"^/(?:favicon\.ico|manifest\.json|site\.webmanifest|robots\.txt|apple-touch-icon[\w.-]*\.png)$")),
    ("static_ok", None, STATIC_PATH),
    ("static_prefix_refused", None, re.compile(r"^/(?:assets|packs|static|webpack)(?:/|$)")),
    ("rest_v1_me", None, re.compile(r"^/rest/v1\.\d+/me$")),
    ("rest_v1_companies", None, re.compile(r"^/rest/v1\.\d+/companies(?:/|$)")),
    ("rest_v1_projects", None, re.compile(r"^/rest/v1\.\d+/projects(?:/|$)")),
    ("rest_v1_other", None, re.compile(r"^/rest/v1(?:\.|/|$)")),
    ("rest_v2", None, re.compile(r"^/rest/v2(?:\.|/|$)")),
    ("graphql", None, re.compile(r"(?i)graphql")),
    ("api", None, re.compile(r"^/api(?:/|$)")),
    ("cdn_cgi", None, re.compile(r"^/cdn-cgi(?:/|$)")),
    ("socket", None, re.compile(r"^/(?:cable|socket|pusher|ws)(?:/|$|\.)")),
    ("feature_flags", None, re.compile(r"(?i)feature|flag|toggle")),
    ("config", None, re.compile(r"(?i)/(?:config|settings|bootstrap|env)(?:/|\.|$)")),
    ("session_user", None, re.compile(r"(?i)^/(?:sessions?|current_user|users?/me|me)(?:/|\.|$)")),
    ("telemetry", None, re.compile(
        r"(?i)/(?:rum|telemetry|metrics|beacon|collect|track|events?|logs?|analytics)(?:/|\.|$)")),
)
_ROOT_OTHER = re.compile(r"^/[^/]+$")

_NET = (
    ("blocked_by_client", re.compile(r"^net::ERR_BLOCKED_BY_CLIENT$")),
    ("blocked_by_csp", re.compile(r"^net::ERR_BLOCKED_BY_CSP$")),
    ("blocked_by_orb", re.compile(r"^net::ERR_BLOCKED_BY_ORB$")),
    ("blocked_by_response", re.compile(r"^net::ERR_BLOCKED_BY_RESPONSE$")),
    ("aborted", re.compile(r"^net::ERR_ABORTED$")),
    ("failed", re.compile(r"^net::ERR_FAILED$")),
    ("timeout", re.compile(r"^net::ERR_(?:CONNECTION_)?TIMED_OUT$")),
    ("dns", re.compile(r"^net::ERR_NAME_(?:NOT_RESOLVED|RESOLUTION_FAILED)$")),
    ("cert", re.compile(r"^net::ERR_(?:CERT|SSL)_[A-Z0-9_]{1,60}$")),
    ("decoding", re.compile(r"^net::ERR_CONTENT_DECODING_FAILED$")),
    ("invalid_response", re.compile(r"^net::ERR_INVALID_(?:HTTP_)?RESPONSE$")),
    ("http2", re.compile(r"^net::ERR_HTTP2_[A-Z0-9_]{1,60}$")),
    ("connection", re.compile(
        r"^net::ERR_(?:CONNECTION_[A-Z_]{1,40}|EMPTY_RESPONSE|ADDRESS_UNREACHABLE|INTERNET_DISCONNECTED"
        r"|NETWORK_CHANGED|PROXY_CONNECTION_FAILED|TUNNEL_CONNECTION_FAILED|SOCKET_NOT_CONNECTED)$")),
)
# Console categories over the lowercased first line, first match wins.
_CONSOLE = (
    ("trusted_types", re.compile(r"trusted ?types?|trustedscript|trustedhtml|require-trusted-types-for")),
    ("csp", re.compile(r"content[ -]security[ -]policy")),
    ("cors", re.compile(r"\bcors\b|access-control-allow-origin|cross-origin request blocked")),
    ("module_mime", re.compile(r"failed to load module script")),
    ("mime", re.compile(r"mime type|refused to (?:execute|apply)")),
    ("sri", re.compile(r"integrity|subresource")),
    ("coep_corp_orb", re.compile(
        r"cross-origin-(?:embedder|resource)-policy|err_blocked_by_orb|opaque response blocking"
        r"|err_blocked_by_response")),
    ("chunk_load", re.compile(r"loading (?:css )?chunk|chunkloaderror")),
    ("script_external_load", re.compile(r"scriptexternalloaderror|loading script failed")),
    ("script_load", re.compile(r"scriptloaderror|remote entry script failed to load")),
    ("remote_scope", re.compile(r"remotescopeerror|remote(?: entry script)? loaded successfully but")),
    ("remote_load_retry", re.compile(r"^attempt \d{1,4} of \d{1,4} to load\b")),
    ("mf_runtime", re.compile(r"federation runtime|\bruntime-0\d\d\b")),
    ("import_map", re.compile(r"import ?map|resolve module specifier|bare specifier")),
    ("blocked_by_client", re.compile(r"err_blocked_by_client")),
    ("http_status_4xx", re.compile(r"status of 4\d\d\b")),
    ("http_status_5xx", re.compile(r"status of 5\d\d\b")),
    ("net_error", re.compile(r"net::err_")),
    ("mixed_content", re.compile(r"mixed content")),
    ("syntax", re.compile(r"syntaxerror|unexpected token")),
    ("type_error", re.compile(r"typeerror")),
    ("reference_error", re.compile(r"referenceerror|is not defined")),
)
_CSP_DIRECTIVE = re.compile(r'directive:?\s*"([a-z-]{1,40})')
CSP_DIRECTIVES = frozenset({
    "script-src", "script-src-elem", "script-src-attr", "style-src", "style-src-elem", "connect-src",
    "img-src", "font-src", "frame-src", "child-src", "worker-src", "default-src", "trusted-types",
    "require-trusted-types-for"})
# Info lines are counted only in these categories (module loader progress).
LOADER_CONSOLE = frozenset({"script_external_load", "script_load", "remote_scope", "remote_load_retry",
                            "mf_runtime", "chunk_load"})
_PAGE_ERROR_NAMES = {"SyntaxError": "syntax_error", "TypeError": "type_error",
                     "ReferenceError": "reference_error", "RangeError": "range_error",
                     "ChunkLoadError": "chunk_load", "ScriptLoadError": "script_load",
                     "ScriptExternalLoadError": "script_external_load", "RemoteScopeError": "remote_scope",
                     "Error": "error"}
_PAGE_ERROR_MESSAGE = (
    ("chunk_load", re.compile(r"(?i)loading (?:css )?chunk|chunkloaderror")),
    ("script_external_load", re.compile(r"(?i)scriptexternalloaderror|loading script failed")),
    ("script_load", re.compile(r"(?i)scriptloaderror|remote entry script failed to load")),
    ("remote_scope", re.compile(r"(?i)remotescopeerror|remote(?: entry script)? loaded successfully but")),
    ("import_map", re.compile(r"(?i)import ?map|resolve module specifier")),
    ("cors", re.compile(r"(?i)\bcors\b|cross-origin")),
)

# Numbers and booleans only. Every value is re-validated by sanitize_probe.
PROBE_JS = r"""(NOT_FOUND) => {
  const out = {};
  try {
    const d = document, b = d.body, all = d.getElementsByTagName("*");
    const ready = {loading: 0, interactive: 1, complete: 2}[d.readyState];
    out.ready = ready === undefined ? -1 : ready;
    out.ms_since_nav = Math.floor(performance.now());
    out.body = !!b;
    out.body_children = b ? b.children.length : 0;
    out.element_count = all.length;
    const text = b ? String(b.innerText || "") : "";
    out.text_len = text.length;
    out.spa_not_found = text.indexOf(NOT_FOUND) >= 0;
    out.title_len = String(d.title || "").length;
    const box = b ? b.getBoundingClientRect() : null;
    out.body_w = box ? Math.floor(box.width) : 0;
    out.body_h = box ? Math.floor(box.height) : 0;
    const skip = new Set(["HTML", "HEAD", "BODY", "SCRIPT", "STYLE", "LINK", "META", "TITLE", "NOSCRIPT", "TEMPLATE"]);
    let visible = 0, contents = 0, shadow = 0, undef = 0;
    const limit = Math.min(all.length, 20000);
    for (let i = 0; i < limit; i++) {
      const el = all[i];
      if (el.shadowRoot) shadow++;
      const tag = el.tagName;
      if (tag.indexOf("-") > 0 && !customElements.get(tag.toLowerCase())) undef++;
      if (skip.has(tag) || visible >= 5000) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        visible++;
      } else if (getComputedStyle(el).display === "contents") {
        contents++;
      }
    }
    out.visible_boxes = visible;
    out.display_contents = contents;
    out.shadow_roots_open = shadow;
    out.undefined_custom = undef;
    let ariaHidden = 0;
    for (const host of [b, d.getElementById("root")]) {
      if (!host) continue;
      for (const child of host.children) if (child.getAttribute("aria-hidden") === "true") ariaHidden++;
    }
    out.aria_hidden_roots = ariaHidden;
    let hidden = false;
    for (const el of [d.documentElement, b]) {
      if (!el) continue;
      const s = getComputedStyle(el);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") hidden = true;
    }
    out.hidden_root = hidden;
    const frames = d.querySelectorAll("iframe");
    let same = 0, frameText = 0;
    for (const f of frames) {
      try {
        const fd = f.contentDocument;
        if (fd) { same++; frameText += fd.body ? String(fd.body.innerText || "").length : 0; }
      } catch (e) {}
    }
    out.iframes = frames.length;
    out.iframes_same_origin = same;
    out.iframe_text_len = frameText;
    const scripts = d.querySelectorAll("script");
    let external = 0, module = 0, nomodule = 0, cors = 0, sri = 0, importmaps = 0;
    for (const s of scripts) {
      if (s.hasAttribute("src")) external++;
      if (s.type === "module") module++;
      if (s.type === "importmap") importmaps++;
      if (s.hasAttribute("nomodule")) nomodule++;
      if (s.hasAttribute("crossorigin")) cors++;
      if (s.hasAttribute("integrity")) sri++;
    }
    out.scripts = scripts.length;
    out.scripts_external = external;
    out.module_scripts = module;
    out.nomodule_scripts = nomodule;
    out.crossorigin_scripts = cors;
    out.integrity_scripts = sri;
    out.importmaps = importmaps;
    out.modulepreloads = d.querySelectorAll('link[rel="modulepreload"]').length;
    out.stylesheets = d.styleSheets.length;
    out.noscript = d.getElementsByTagName("noscript").length;
    const rc = {};
    for (const id of ["root", "app", "main"]) {
      const el = d.getElementById(id);
      rc[id] = el ? el.childElementCount : -1;
    }
    rc.single_spa = d.querySelectorAll('[id^="single-spa-application"]').length;
    out.root_children = rc;
    const perf = performance.getEntriesByType("resource");
    out.perf_resources = perf.length;
    let zero = 0;
    for (const e of perf) if (e.transferSize === 0) zero++;
    out.perf_zero_transfer = zero;
  } catch (e) {
    out.probe_error = true;
  }
  return out;
}"""

PROBE_INTS = ("ms_since_nav", "body_children", "element_count", "text_len", "title_len", "body_w", "body_h",
              "visible_boxes", "display_contents", "shadow_roots_open", "undefined_custom",
              "aria_hidden_roots", "iframes", "iframes_same_origin", "iframe_text_len", "scripts",
              "scripts_external", "module_scripts", "nomodule_scripts", "crossorigin_scripts",
              "integrity_scripts", "importmaps", "modulepreloads", "stylesheets", "noscript",
              "perf_resources", "perf_zero_transfer")
PROBE_BOOLS = ("body", "hidden_root", "probe_error", "spa_not_found")
ROOT_CHILD_KEYS = ("root", "app", "main", "single_spa")


def _int(value: Any, low: int, high: int, default: int) -> int:
    # bool is an int subclass; a page can forge any JSON type.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return max(low, min(high, int(value)))


def sanitize_probe(raw: Any) -> dict | None:
    """Keep only known keys; ints clamped, booleans only when strictly True."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {"ready": _int(raw.get("ready"), -1, 2, -1)}
    for key in PROBE_INTS:
        out[key] = _int(raw.get(key), 0, PROBE_INT_MAX, 0)
    for key in PROBE_BOOLS:
        out[key] = raw.get(key) is True
    rc = raw.get("root_children")
    rc = rc if isinstance(rc, dict) else {}
    out["root_children"] = {key: _int(rc.get(key), -1, PROBE_INT_MAX, -1) for key in ROOT_CHILD_KEYS}
    out["root_children"]["single_spa"] = max(0, out["root_children"]["single_spa"])
    return out


def readable(probe: dict | None) -> bool:
    """True when the page rendered something a reader could use: any visible
    text, or several visible boxes. A blank SPA shell (#root empty) is False."""
    if not isinstance(probe, dict):
        return False
    text = _int(probe.get("text_len"), 0, PROBE_INT_MAX, 0)
    boxes = _int(probe.get("visible_boxes"), 0, PROBE_INT_MAX, 0)
    return text >= 1 or boxes >= READABLE_MIN_BOXES


# Classifiers: all return code-fixed strings -----------------------------------------


def host_class(guard: Any, scheme: str, host: str | None) -> str:
    scheme = (scheme or "").lower()
    if scheme in ("data", "blob", "about", "javascript", "chrome-extension", "chrome"):
        return "local_scheme"
    host = (host or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return "invalid"
    if host == getattr(guard, "app_host", None):
        return "app_seed"
    if host in APP_HOSTS:
        return "app_other"
    if host == LOGIN:
        return "login"
    if host in APP_ASSET_HOSTS:
        return "app_asset"
    if host in LOGIN_ASSET_HOSTS:
        return "login_asset"
    if host.endswith(PAGES_SUFFIX):
        # Only code-listed labels are ever emitted: allowed hosts as known,
        # refused public registry names as listed, every other label as other.
        label = host[:-len(PAGES_SUFFIX)]
        if label in PAGES_LABELS:
            return f"procore_pages_known:{label}"
        listed = pages_label(host)
        return f"procore_pages_listed:{listed}" if listed is not None else "procore_pages_other"
    if host.endswith(".cdn.procore.com"):
        return "procore_cdn_other"
    if host == "procore.com" or host.endswith(".procore.com"):
        return "procore_other"
    for tag, suffixes in THIRD_PARTY:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return tag
    return "third_party_other"


def path_family(guard: Any, hc: str, path: str) -> str:
    path = path or "/"
    app = hc in ("app_seed", "app_other")
    pages = hc.startswith("procore_pages")
    if app and any(p.match(path) for p in getattr(getattr(guard, "ceiling", None), "nav_paths", ())):
        return "nav_seed"
    if app and any(p.fullmatch(path) for p in getattr(guard, "read_paths", ())):
        return "rest_read_ok"
    if hc == "login" and any(p.fullmatch(path) for p in LOGIN_LOGIN_PATHS):
        return "login_path"
    for name, scope, pattern in _FAMILIES:
        if scope == "pages" and not pages:
            continue
        if pattern.search(path):
            return name
    return "root_other" if _ROOT_OTHER.fullmatch(path) else "other"


# Code-fixed app-host endpoint templates (Procore shell, hub, project header and
# RFIs tool bundles). {c} and {p} match only the seed company and project ({c}
# matches any company id for a legacy seed, which binds none); {id} is any
# other number. The template itself is the emitted value, never the path.
ENDPOINT_TEMPLATES = (
    "/rest/v1.0/me",
    "/rest/v1.0/companies",
    "/rest/v1.1/environment_metadata",
    "/rest/v1.0/settings/permissions",
    "/rest/v1.0/analytic_events",
    "/rest/v1.0/projects/{p}",
    "/rest/v1.0/projects/{p}/procore_environment",
    "/rest/v1.0/companies/{c}/procore_environment",
    "/rest/v1.0/projects/{p}/rfis",
    "/rest/v1.0/projects/{p}/rfis/{id}",
    "/rest/v1.1/projects/{p}/rfis",
    "/rest/v1.1/projects/{p}/rfis/{id}",
    "/rest/v1.0/projects/{p}/rfis/recycle_bin",
    "/rest/v1.0/projects/{p}/rfis/priorities",
    "/rest/v1.0/projects/{p}/rfis/open_closed_chart_data",
    "/rest/v1.0/projects/{p}/rfis/export",
    "/rest/v1.0/projects/{p}/client_config/rfis/",
    "/rest/v1.0/projects/{p}/configurable_field_sets",
    "/{p}/project/rfi/list_client_config.json",
    "/{p}/project/rfi/onboarding_list_data",
    "/{p}/project/rfi/onboarding_configure_tab_banners_data",
    "/rest/v2.0/hubs/cards",
    "/rest/v2.0/companies/{c}/early_access/enrollees_for_user",
    "/rest/v2.0/companies/{c}/home_experience_settings",
    "/rest/v2.0/companies/{c}/projects/{p}/hubs/containers",
    "/rest/v2.0/companies/{c}/projects/{p}/hubs/views",
    "/rest/v2.0/companies/{c}/projects/{p}/hubs/template_views",
    "/rest/v2.0/companies/{c}/projects/{p}/saved_views",
    "/rest/v2.0/companies/{c}/projects/{p}/saved_views/permissions",
    "/rest/v2.0/companies/{c}/projects/{p}/kickoff_onboarding/sidepanel/permitted",
    "/rest/v2.0/companies/{c}/projects/{p}/rfis",
    "/rest/v2.0/companies/{c}/projects/{p}/rfis/options",
    "/rest/v2.0/companies/{c}/projects/{p}/workflows/instances",
    "/{c}/company/mindtouch/help_link",
    "/cdn-cgi/challenge-platform/scripts/jsd/main.js",
)
_PLACEHOLDER = re.compile(r"\{(c|p|id)\}")


@functools.lru_cache(maxsize=8)
def _compile_templates(company: str | None, project: str | None) -> tuple:
    ids = {"c": re.escape(company) if company else r"\d{1,20}",
           "p": re.escape(project) if project else None, "id": r"\d{1,20}"}
    out = []
    for template in ENDPOINT_TEMPLATES:
        parts, pos, ok = [], 0, True
        for match in _PLACEHOLDER.finditer(template):
            value = ids[match[1]]
            if value is None:
                ok = False
                break
            parts.append(re.escape(template[pos:match.start()]) + value)
            pos = match.end()
        if ok:
            parts.append(re.escape(template[pos:]))
            out.append((template, re.compile("".join(parts))))
    return tuple(out)


def endpoint_template(guard: Any, hc: str, path: str) -> str:
    """The code-fixed template an app-host path matches, else "-"."""
    if hc not in ("app_seed", "app_other"):
        return "-"
    company = getattr(guard, "company", None)
    project = getattr(guard, "project", None)
    company = company if isinstance(company, str) and company.isdigit() else None
    project = project if isinstance(project, str) and project.isdigit() else None
    for template, pattern in _compile_templates(company, project):
        if pattern.fullmatch(path or "/"):
            return template
    return "-"


def extension(path: str) -> str:
    name = (path or "/").rsplit("/", 1)[-1]
    if "." not in name:
        return "html_none"
    return _EXT.get(name.rsplit(".", 1)[-1].lower(), "other")


def query_kind(url: str, query: str) -> str:
    if not query:
        return "none"
    return "token_like" if token_query_keys(url) else "plain"


def net_failure(text: Any) -> str:
    if not isinstance(text, str):
        return "other"
    # Chromium tags requests the DevTools route aborted with ".Inspector".
    primary = text.split("\n", 1)[0][:128].strip().removesuffix(".Inspector")
    for name, pattern in _NET:
        if pattern.fullmatch(primary):
            return name
    return "other"


def console_category(first_line: str) -> str:
    line = first_line.lower()
    for name, pattern in _CONSOLE:
        if pattern.search(line):
            return name
    return "other"


def csp_directive(first_line: str) -> str | None:
    match = _CSP_DIRECTIVE.search(first_line.lower())
    if match is None:
        return None
    return match[1] if match[1] in CSP_DIRECTIVES else "other"


def page_error_category(name: Any, message: Any) -> str:
    if isinstance(message, str):
        line = message.split("\n", 1)[0][:CONSOLE_LINE_MAX]
        for category, pattern in _PAGE_ERROR_MESSAGE:
            if pattern.search(line):
                return category
    return _PAGE_ERROR_NAMES.get(name, "other") if isinstance(name, str) else "other"


def _header(headers: Any, name: str) -> str | None:
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            return value if isinstance(value, str) else None
    return None


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.hostname:
            port = f":{parts.port}" if parts.port is not None else ""
            return f"{parts.scheme.lower()}://{parts.hostname.lower()}{port}"
    except ValueError:
        pass
    return None


def content_class(value: str | None) -> str:
    if value is None:
        return "absent"
    value = value.lower()
    for name, needles in (("js", ("javascript", "ecmascript")), ("css", ("css",)), ("json", ("json",)),
                          ("html", ("html",)), ("font", ("font", "woff")), ("image", ("image/",))):
        if any(n in value for n in needles):
            return name
    return "other"


def size_bucket(size: int | None) -> str:
    if not isinstance(size, int) or size < 0:
        return "unknown"
    if size < 64 * 1024:
        return "lt64k"
    if size < 1024 * 1024:
        return "lt1m"
    if size <= 8 * 1024 * 1024:
        return "lt8m"
    return "gt8m"


def _bump(counts: dict, key: str, n: int = 1) -> None:
    counts[key] = min(COUNT_CAP, counts.get(key, 0) + n)


ROW_FIELDS = ("pg", "rt", "hc", "pf", "ep", "ext", "q", "m", "v", "sc", "ct", "sz", "oh", "ac", "cr", "co")


def _digest(value: str) -> bytes:
    # In memory only, to count distinct hosts and URLs; never emitted.
    return hashlib.blake2b(value.encode("utf-8", "replace"), digest_size=8).digest()


def _ident(url: Any) -> tuple[bytes, bytes] | None:
    try:
        if not isinstance(url, str):
            return None
        parts = urlsplit(url)
        return _digest((parts.hostname or "").lower()), _digest(url.split("#", 1)[0])
    except ValueError:
        return None


class _Window:
    """One census window: rows since the last emit (delta) or for the job (total)."""

    def __init__(self) -> None:
        self.rows: dict[tuple, int] = {}
        self.distinct: dict[tuple, tuple[set, set]] = {}
        self.keys_overflow = 0
        self.unrouted_failed: dict[str, int] = {}
        self.console: dict[str, dict[str, int]] = {"error": {}, "warning": {}, "info": {}}
        self.pageerror: dict[str, int] = {}
        self.csp: dict[str, dict[str, int]] = {"enforced": {}, "report_only": {}}
        self.spa_nav_blanked: dict[str, int] = {}
        self.events_dropped = 0

    def add(self, key: tuple, ident: tuple[bytes, bytes] | None = None) -> None:
        if key in self.rows:
            self.rows[key] = min(COUNT_CAP, self.rows[key] + 1)
        elif len(self.rows) >= MAX_CENSUS_KEYS:
            self.keys_overflow = min(COUNT_CAP, self.keys_overflow + 1)
            return
        else:
            self.rows[key] = 1
        if ident is not None:
            hosts, urls = self.distinct.setdefault(key, (set(), set()))
            if len(hosts) < DISTINCT_CAP:
                hosts.add(ident[0])
            if len(urls) < DISTINCT_CAP:
                urls.add(ident[1])

    def distinct_counts(self, key: tuple) -> tuple[int, int]:
        hosts, urls = self.distinct.get(key, ((), ()))
        return len(hosts), len(urls)


class RenderDiagnostics:
    """Per-job census and probe for one UserGuard. Every hook is synchronous
    or bounded, swallows its own errors and never raises into browsing."""

    def __init__(self, guard: Any) -> None:
        self._guard = guard
        self._pending: dict[int, tuple[Any, tuple, Any]] = {}
        self._delta = _Window()
        self._total = _Window()
        self._events = 0
        self._steps = 0
        self._emitted: set[tuple[str, int]] = set()
        self._final = False
        # Job-level: model-page /remoteEntry.js scripts the guard refused on a
        # pages host (census count), the model-page document numbers they were
        # refused on, and the last probe's not-found marker and document number.
        self.remote_entry_refused = 0
        self._refused_navs: set[int] = set()
        self._last_not_found = False
        self._last_probe_nav: int | None = None

    # Recording -----------------------------------------------------------------------

    def _windows(self) -> tuple[_Window, _Window]:
        return self._delta, self._total

    def _commit(self, fields: tuple, outcome: str, ident: Any = None) -> None:
        key = (*fields, outcome)
        for window in self._windows():
            window.add(key, ident)

    def record(self, request: Any, *, page: Any, login: bool, verdict: str, status: int | None = None,
               headers: dict | None = None, size: int | None = None) -> None:
        """One guard decision for one routed request. Held until Chromium
        reports the outcome (requestfinished / requestfailed)."""
        try:
            fields = self._row(request, page=page, login=login, verdict=verdict, status=status,
                               headers=headers, size=size)
            row = dict(zip(ROW_FIELDS, fields))
            if (row["pg"] == "model" and row["hc"].startswith("procore_pages") and row["pf"] == "root_remote_entry"
                    and row["v"].startswith("refused:")):
                self.remote_entry_refused = min(COUNT_CAP, self.remote_entry_refused + 1)
                if len(self._refused_navs) < MAX_REFUSED_NAVS:
                    self._refused_navs.add(self._nav_no())
            ident = _ident(getattr(request, "url", None))
            if page is None:
                self._commit(fields, "pending", ident)   # no page events will report it
                return
            stale = self._pending.pop(id(request), None)
            if stale is not None and stale[0] is not request:
                self._commit(stale[1], "pending", stale[2])   # a reused id(): keep the old row
            self._pending[id(request)] = (request, fields, ident)
            while len(self._pending) > MAX_PENDING:
                _req, oldest, oldest_ident = self._pending.pop(next(iter(self._pending)))
                self._commit(oldest, "pending", oldest_ident)
        except Exception as exc:
            log.debug("census record skipped: %s", type(exc).__name__)

    def _take(self, request: Any) -> tuple | None:
        entry = self._pending.get(id(request))
        if entry is None or entry[0] is not request:
            return None
        del self._pending[id(request)]
        return entry[1], entry[2]

    def request_finished(self, request: Any) -> None:
        try:
            taken = self._take(request)
            if taken is not None:
                self._commit(taken[0], "finished", taken[1])
        except Exception as exc:
            log.debug("census finish skipped: %s", type(exc).__name__)

    def request_failed(self, request: Any) -> None:
        try:
            net = net_failure(getattr(request, "failure", None))
            taken = self._take(request)
            if taken is not None:
                self._commit(taken[0], f"failed:{net}", taken[1])
            else:
                for window in self._windows():
                    _bump(window.unrouted_failed, net)
        except Exception as exc:
            log.debug("census failure skipped: %s", type(exc).__name__)

    def _event_allowed(self) -> bool:
        if self._events >= MAX_EVENTS:
            for window in self._windows():
                window.events_dropped = min(COUNT_CAP, window.events_dropped + 1)
            return False
        self._events += 1
        return True

    def console(self, message: Any) -> None:
        # Only msg.type and the first line of msg.text are read, classified
        # here and dropped. msg.location (a URL) and msg.args are never read.
        try:
            kind = message.type
            if kind not in ("error", "warning", "info"):
                return
            if kind != "info" and not self._event_allowed():
                return
            text = message.text
            first = text.split("\n", 1)[0][:CONSOLE_LINE_MAX] if isinstance(text, str) else ""
            category = console_category(first)
            if kind == "info":
                # Info lines count only as module loader progress.
                if category not in LOADER_CONSOLE or not self._event_allowed():
                    return
                for window in self._windows():
                    _bump(window.console["info"], category)
                return
            directive = csp_directive(first) if category in ("csp", "trusted_types") else None
            mode = "report_only" if "[report only]" in first.lower() else "enforced"
            for window in self._windows():
                _bump(window.console[kind], category)
                if directive is not None:
                    _bump(window.csp[mode], directive)
        except Exception as exc:
            log.debug("console count skipped: %s", type(exc).__name__)

    def page_error(self, error: Any) -> None:
        try:
            if not self._event_allowed():
                return
            category = page_error_category(getattr(error, "name", None), getattr(error, "message", None))
            for window in self._windows():
                _bump(window.pageerror, category)
        except Exception as exc:
            log.debug("page error count skipped: %s", type(exc).__name__)

    def spa_blanked(self, url: str) -> None:
        """The strict same-document re-check sent the page to about:blank."""
        try:
            parts = urlsplit(url)
            family = path_family(self._guard, host_class(self._guard, parts.scheme, parts.hostname), parts.path)
        except Exception:
            family = "other"
        for window in self._windows():
            _bump(window.spa_nav_blanked, family)

    def _row(self, request: Any, *, page: Any, login: bool, verdict: str, status: int | None,
             headers: dict | None, size: int | None) -> tuple:
        url = request.url
        try:
            parts = urlsplit(url)
            scheme, host, path, query = parts.scheme, parts.hostname, parts.path, parts.query
        except ValueError:
            scheme, host, path, query = "", None, "/", ""
        hc = host_class(self._guard, scheme, host)
        ep = endpoint_template(self._guard, hc, path) if hc == "app_seed" else "-"
        if login:
            pg = "login"
        elif page is None or id(page) not in self._guard.model_pages:
            pg = "unowned"
        else:
            pg = "model"
        try:
            rt = request.resource_type
        except Exception:
            rt = None
        rt = rt if isinstance(rt, str) and rt in RESOURCE_TYPES else "other"
        method = str(getattr(request, "method", "")).upper()
        if verdict.startswith("refused:"):
            reason = verdict.split(":", 1)[1]
            verdict = "refused:" + (reason if reason in REFUSAL_REASONS else "other")
        elif verdict.startswith("transport:"):
            category = verdict.split(":", 1)[1]
            verdict = "transport:" + (category if category in TRANSPORT_CATEGORIES else "other")
        elif verdict not in VERDICTS:
            verdict = "other"
        try:
            request_origin = _header(request.headers, "origin")
        except Exception:
            request_origin = None
        if request_origin is None:
            oh = "absent"
        else:
            oh = "present_same" if request_origin.lower() == _origin_of(url) else "present_cross"
        acao = _header(headers, "access-control-allow-origin")
        if acao is None:
            ac = "absent"
        elif acao.strip() == "*":
            ac = "star"
        elif acao.strip().lower() == f"https://{getattr(self._guard, 'app_host', None)}":
            ac = "app_origin"
        else:
            ac = "other"
        corp = _header(headers, "cross-origin-resource-policy")
        corp = corp.strip().lower() if corp is not None else None
        if corp is None:
            cr = "absent"
        else:
            cr = corp.replace("-", "_") if corp in ("same-origin", "same-site", "cross-origin") else "other"
        sc = f"{status // 100}xx" if isinstance(status, int) and 100 <= status < 600 else "none"
        return (pg, rt, hc, path_family(self._guard, hc, path), ep, extension(path), query_kind(url, query),
                method if method in METHODS else "OTHER", verdict, sc,
                content_class(_header(headers, "content-type")) if headers is not None else "absent",
                size_bucket(size), oh, ac, cr)

    # Probe and emission --------------------------------------------------------------

    async def probe(self, page: Any) -> dict | None:
        """Render shape only, no step. None when the page is gone or the
        evaluate fails or exceeds PROBE_TIMEOUT_S."""
        try:
            if page is None or page.is_closed():
                return None
            raw = await asyncio.wait_for(page.evaluate(PROBE_JS, NOT_FOUND_TEXT), PROBE_TIMEOUT_S)
        except Exception as exc:
            log.debug("render probe skipped: %s", type(exc).__name__)
            return None
        probe = sanitize_probe(raw)
        if probe is not None:
            self._last_not_found = probe["spa_not_found"]
            self._last_probe_nav = self._nav_no()
        return probe

    def _nav_no(self) -> int:
        """The model page's main-frame document number (guard.nav_count)."""
        try:
            return int(getattr(self._guard, "nav_count", 0) or 0)
        except Exception:
            return -1

    def remote_entry_refused_here(self) -> bool:
        """A remote entry was refused on the current model-page document."""
        return self._nav_no() in self._refused_navs

    def readable(self, probe: dict | None) -> bool:
        return readable(probe)

    def page_code_blocked(self, probe: dict | None = None) -> bool:
        """The guard refused a model-page /remoteEntry.js on a pages host and
        the page shows Procore's not-found screen, both on the current
        main-frame document (a refusal on an earlier page never counts: the
        RFIs tool shows the same sentence for its own missing records).
        Without a fresh probe the last probe's marker is used."""
        if isinstance(probe, dict):
            not_found = probe.get("spa_not_found") is True
        else:
            not_found = self._last_not_found is True
        nav = self._nav_no()
        return (not_found and nav in self._refused_navs
                and (self._last_probe_nav is None or self._last_probe_nav == nav))

    def _location(self, page: Any) -> tuple[str, str, bool]:
        try:
            parts = urlsplit(page.url)
            hc = host_class(self._guard, parts.scheme, parts.hostname)
            pf = path_family(self._guard, hc, parts.path)
            return hc, pf, hc == "app_seed" and pf == "nav_seed"
        except Exception:
            return "invalid", "other", False

    def _flush_pending(self) -> None:
        pending, self._pending = self._pending, {}
        for _req, fields, ident in pending.values():
            self._commit(fields, "pending", ident)

    def _census(self, trigger: str, nav_no: int, window: _Window, name: str) -> dict:
        rows = []
        for key, n in window.rows.items():
            dh, du = window.distinct_counts(key)
            rows.append(dict(zip(ROW_FIELDS, key), n=n, dh=dh, du=du))
        rows.sort(key=lambda row: (-row["n"], [row[f] for f in ROW_FIELDS]))
        out = {
            "v": 2, "trigger": trigger, "nav_no": nav_no, "window": name, "rows": rows, "rows_dropped": 0,
            "keys_overflow": window.keys_overflow, "pending": len(self._pending),
            "unrouted_failed": dict(window.unrouted_failed),
            "console": {k: dict(v) for k, v in window.console.items()},
            "pageerror": dict(window.pageerror), "csp": {k: dict(v) for k, v in window.csp.items()},
            "spa_nav_blanked": dict(window.spa_nav_blanked), "events_dropped": window.events_dropped,
            "remote_entry_refused": self.remote_entry_refused,
            "ws_closed": min(COUNT_CAP, int(getattr(self._guard, "websockets_closed", 0) or 0)),
            "popups_closed": min(COUNT_CAP, int(getattr(self._guard, "popups_closed", 0) or 0)),
        }
        size = _size(out)
        if size > MAX_PAYLOAD_BYTES:
            # Drop the least informative rows first: low-count clean loads.
            order = sorted(range(len(rows)), key=lambda i: (
                not (rows[i]["v"] == "fulfilled" and rows[i]["co"] == "finished"), rows[i]["n"]))
            dropped = set()
            for i in order:
                if size <= MAX_PAYLOAD_BYTES:
                    break
                dropped.add(i)
                size -= _size(rows[i]) + 1
            out["rows"] = [row for i, row in enumerate(rows) if i not in dropped]
            out["rows_dropped"] = len(dropped)
        return out

    def _log(self, kind: str, summary: str, result: dict) -> None:
        self._steps += 1
        self._guard._step(kind, summary, ok=True, result=result)

    async def emit(self, page: Any, trigger: str) -> dict | None:
        """Log one browser.resource_census and one browser.render_probe step
        for this trigger and navigation number, within the job's caps.
        Returns the sanitized probe (None when the page is closed, the probe
        failed, or this emission is capped or a repeat)."""
        try:
            trigger = TRIGGER_ALIASES.get(trigger, trigger)
            if trigger not in TRIGGERS:
                return None
            nav_no = int(getattr(self._guard, "nav_count", 0) or 0)
            final = trigger == "final"
            if final:
                if self._final:
                    return None
                self._final = True
            else:
                if ((trigger, nav_no) in self._emitted
                        or self._steps + 2 > MAX_DIAG_STEPS - FINAL_RESERVE):
                    return None
                self._emitted.add((trigger, nav_no))
            probe = await self.probe(page)
            if final:
                self._flush_pending()
                census = self._census(trigger, nav_no, self._total, "total")
            else:
                census = self._census(trigger, nav_no, self._delta, "delta")
                self._delta = _Window()
            refused = sum(r["n"] for r in census["rows"] if r["v"].startswith("refused:"))
            failed = sum(r["n"] for r in census["rows"] if r["v"] == "fulfilled" and r["co"].startswith("failed:"))
            self._log("browser.resource_census",
                      f"Resource census ({trigger} #{nav_no}): {len(census['rows'])} rows, {refused} refused, "
                      f"{failed} failed after fulfil", census)
            if probe is not None and self._steps < MAX_DIAG_STEPS:
                hc, pf, seed = self._location(page)
                self._log("browser.render_probe",
                          f"Render probe ({trigger} #{nav_no}): {probe['text_len']} text chars, "
                          f"{probe['element_count']} elements, {probe['visible_boxes']} visible boxes",
                          {"v": 2, "trigger": trigger, "nav_no": nav_no, "loc_hc": hc, "loc": pf,
                           "loc_is_seed": seed, "readable": readable(probe), "probe": probe})
            return probe
        except Exception as exc:
            log.debug("render diagnostics skipped: %s", type(exc).__name__)
            return None


def _size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")))
