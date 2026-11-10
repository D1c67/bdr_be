"""UserGuard v2: the network gate for user_browser jobs (plan 6, 10; WI-9).

Signed worker code. Written fresh for v2; nothing is imported from the v1
personal guard. One guard serves one job's BrowserContext (every page, tab,
popup, frame and dedicated Worker of the job, because the route is installed
on the context).

What it enforces
----------------
Floor (cannot be widened by the server, 10.1 item 4, 10.2):
  - only http(s) (and ws(s) for sockets); file:, chrome:, devtools:,
    javascript: and other schemes refused; data: and blob: never top level;
  - IP-literal hosts in private ranges (after unwrapping IPv4-mapped, 6to4,
    Teredo, NAT64 64:ff9b::/96 and IPv4-compatible forms, and after parsing
    decimal, octal and hex IPv4 literals), localhost, single-label hosts and
    .local, .lan, .internal, .home.arpa names; URLs with credentials;
  - beacons and pings, and a fixed analytics host list: dropped (6.2);
  - WebSockets refused unless the socket's site is curated and its profile
    sets websocket_ok (UserPolicySite.websocket_allowed); messages to a host
    other than the start origin are still checked for typed strings. Worker
    scopes have no socket API at all (exfil.WORKER_SCOPE_JS: every Worker is
    started through a bootstrap that removes it), and WebSocketStream and
    WebTransport are removed from every frame;
  - the exfiltration rules of exfil.py on every request and redirect hop:
    the typed-string rule reads the URL, the body and the header values
    (custom headers, Referer; not Cookie). A subresource GET that carries
    typed text is fetched by the guard rather than continued, even on the
    start origin, so a redirect it gets is re-decided hop by hop (Chromium
    follows redirects of a continued request without asking the route).

Per-task site scope (5.3, 5.4, 10.4), from the job policy (UserJobPolicy):
  - a top-level navigation outside the scope raises a site hold before any
    request is sent (`UserSiteHoldRequest`, trigger navigate, link,
    redirect or popup); released "full" continues it after a policy refetch,
    "site_only" opens the origin root instead; declined aborts it;
  - an identity-provider host outside the scope is not site-held: the
    navigation is refused and the model is told to ask for help with "sso";
  - sub-frames and subresources may load any public host.

Final actions (6.2, 6.3, 6.5), held at the network:
  - every non-GET request (navigations, form posts, fetch, XHR, dedicated
    Worker requests) is held unless the open login exemption, a profile
    safe_mutation or one of the owner's safe rules releases it; a held request
    stays pending until a verified UserHoldToken releases exactly it;
  - a GET matching a profile final_get rule is held;
  - an out-of-scope non-GET carrying a typed string is refused without a card;
  - click-level holds (`check_click`) on elements whose label, value, title,
    alt, aria-label or href matches the D2 verb list, before the click.

Redirects (6.1): every request that is not a plain subresource GET goes
through route.fetch(max_redirects=0). A 3xx is never handed to Chromium:
  - a navigation whose follow-up is a GET gets an inert document whose meta
    refresh starts a fresh routed navigation to the Location, so scope, holds
    and the exfiltration rules apply again to that hop;
  - a non-navigation request follows GET hops itself, re-deciding each hop;
  - 307 and 308 after a non-GET (released, exempt or safe) re-send the body:
    that hop is a new hold, never an automatic follow.

Login exemption (6.3): `open_login_attempt` is called by fill_credential
(WI-10) after it fills a field. While open, only these non-GET requests to the
credential's login origins pass without a card: on curated sites requests
whose path matches the site's login_paths; on ad hoc sites the first form
submission (a navigation) from the frame holding the filled element, or the
first fetch/XHR from that frame whose body carries a value the worker filled
(`markers`). The exemption closes on the first response to an exempt
request, or when the page leaves the login origins. While open, the model's
click, type_text, select_option and press_key outside the filled form are
refused (`check_click`, `check_interaction`).

Interface for WI-10 (runner, tools, identity)
---------------------------------------------
  guard = build_guard(policy, send=client_send, job_id=..., lease_epoch=lambda: job.epoch,
                      redact=secrets.scrub_text, log_step=steps.log, screenshot=upload_screenshot,
                      refresh_policy=fetch_policy)
  await guard.install(context)              # before the first page loads
  guard.typed.add(text)                     # every type_text / select_option value
  guard.set_action_description(text)        # the browser model's one-line description
  v = guard.prepare_navigation(page, url, page_links=links, start_url=job_input.start_url)  # navigate, before goto
  out = await guard.check_click(page, handle, action="click" | "enter")
  out = await guard.check_interaction(page, handle)            # type_text, select_option, other keys
  await guard.open_login_attempt(credential_id=..., page=page, element=handle, markers=(username,))
  guard.close_login_attempt("result")       # after login/result
  await guard.wait_holds()                  # after every tool, before the next model turn
  guard.take_notices()                      # model-facing lines for the next observation
  guard.set_policy(new_policy)              # after a fence code says the policy changed
  guard.holds_open, guard.side_effects, guard.blocked
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

from ...protocol import (
    BLOCKED_HOST_SUFFIXES,
    ORIGIN_RE,
    URL_MAX,
    UserHoldRequest,
    UserJobPolicy,
    UserPolicyCredential,
    UserPolicySite,
    UserSiteHoldRequest,
    is_identity_provider_host,
    url_origin,
)
from .exfil import (
    DROP_RESOURCE_TYPES,
    EXFIL_INIT_SCRIPT,
    split_worker_marker,
    worker_bootstrap,
    QueryBudget,
    TypedStrings,
    Verdict,
    check_navigate_url,
    is_analytics_host,
    is_offsite,
    trace_url,
)
from .holds import (
    CLICK_METHOD,
    EndpointHoldTransport,
    HoldBroker,
    HoldResult,
    HoldTokenVerifier,
    Send,
    click_descriptor_bytes,
    request_hashes,
    sha256_hex,
    summarize_body,
)

log = logging.getLogger("bdr_agent.user_browser_v2.guard")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
MAX_HOPS = 10
FETCH_TIMEOUT_MS = 120_000
SCREENSHOT_TIMEOUT_S = 10.0
MIN_RESPONSE_CAP = 16 * 1024 * 1024
STRIP_RESPONSE_HEADERS = frozenset({"location", "refresh", "content-encoding", "content-length",
                                    "transfer-encoding", "set-cookie", "connection", "keep-alive"})
INERT_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'"
_ALLOWED_ABOUT = ("about:blank", "about:srcdoc")
_NUMERIC_PART = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPAT = ipaddress.ip_network("::/96")
_URL_SAFE_CHARS = ":/?#[]@!$&'()*+,;=%~"

# D2 verb list (6.2 item 4) with common inflections, matched as whole words
# (letters on either side break the match, so "assignment" or "payment" do
# not match, while "delete_item", "/rfis/9/delete" and "Submitted" do).
_VERBS = "submit|send|accept|decline|delete|remove|pay|approve|answer|respond|sign|award|confirm"
VERB_RE = re.compile(rf"(?<![a-z])(?:{_VERBS})(?:s|es|ed|d|ing|ting|ted)?(?![a-z])")
# Phrases removed before matching: signing in or out is not a final action.
VERB_EXCLUDE_RE = re.compile(r"(?<![a-z])(?:sign|log)[\s_-]?(?:in|on|out)(?![a-z])")
_GRAPHQL_MUTATION_RE = re.compile(r"(?:^|[\s{}])mutation\b", re.IGNORECASE)


# Floor -------------------------------------------------------------------------------


def _deny(reason: str) -> Verdict:
    return Verdict(False, reason)


_OK = Verdict(True)


def _loose_ipv4(host: str) -> ipaddress.IPv4Address | None | bool:
    """inet_aton-style IPv4 (decimal, octal, hex, 1 to 4 parts). Returns the
    address, None when the host is not numeric, or False when it looks
    numeric but is not a valid address (refused)."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or not all(_NUMERIC_PART.match(p) for p in parts):
        return None
    values: list[int] = []
    for p in parts:
        try:
            if p.startswith("0x"):
                values.append(int(p[2:] or "0", 16))
            elif len(p) > 1 and p.startswith("0"):
                values.append(int(p, 8))
            else:
                values.append(int(p, 10))
        except ValueError:
            return False
    *head, last = values
    if any(v > 255 for v in head) or last >= 256 ** (4 - len(head)):
        return False
    number = 0
    for v in head:
        number = number * 256 + v
    number = number * 256 ** (4 - len(head)) + last
    return ipaddress.IPv4Address(number)


def _ip_candidates(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[Any]:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return [ip.ipv4_mapped]
        if ip.sixtofour is not None:
            return [ip.sixtofour]
        if ip.teredo is not None:
            return list(ip.teredo)
        if ip in _NAT64:
            return [ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)]
        if ip in _V4_COMPAT:
            return [ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)]
    return [ip]


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    for cand in _ip_candidates(ip):
        if (not cand.is_global or cand.is_multicast or cand.is_reserved or cand.is_unspecified
                or cand.is_loopback or cand.is_link_local or cand.is_private):
            return False
        if isinstance(cand, ipaddress.IPv4Address) and cand == ipaddress.IPv4Address("255.255.255.255"):
            return False
    return True


def host_floor_verdict(host: str | None) -> Verdict:
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return _deny("bad_url")
    bare = h.strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        ip = None
    if ip is None:
        loose = _loose_ipv4(h)
        if loose is False:
            return _deny("bad_host")
        ip = loose
    if ip is not None:
        return _OK if ip_is_public(ip) else _deny("private_network")
    if h == "localhost" or h.endswith(".localhost"):
        return _deny("private_network")
    if "." not in h:
        return _deny("single_label_host")
    for suffix in BLOCKED_HOST_SUFFIXES:
        if h.endswith(suffix) or h == suffix.lstrip("."):
            return _deny("private_network")
    return _OK


def floor_verdict(url: str, *, top_level: bool) -> Verdict:
    """The signed floor for one URL (plan 10.1 item 4)."""
    if not isinstance(url, str) or not url:
        return _deny("bad_url")
    if any(ord(c) < 32 for c in url) or "\\" in url.split("?", 1)[0]:
        return _deny("bad_url")
    try:
        parts = urlsplit(url)
    except ValueError:
        return _deny("bad_url")
    scheme = (parts.scheme or "").lower()
    if scheme == "about":
        return _OK if url in _ALLOWED_ABOUT else _deny("scheme_not_allowed")
    if scheme in ("data", "blob"):
        return _deny("scheme_not_allowed") if top_level else _OK
    if scheme not in ("http", "https", "ws", "wss"):
        return _deny("scheme_not_allowed")
    if "@" in (parts.netloc or ""):
        return _deny("credentials_in_url")
    try:
        parts.port
    except ValueError:
        return _deny("bad_port")
    return host_floor_verdict(parts.hostname)


def canonical_url(url: str) -> str:
    """Percent-encode what Chromium would (spaces, non-ASCII) so a redirect
    target compares equal to the request Chromium then sends."""
    return quote(url, safe=_URL_SAFE_CHARS)


# Scope -------------------------------------------------------------------------------


def _path_in(path: str, prefix: str | None) -> bool:
    if not prefix:
        return True
    if prefix.endswith("/"):
        return path.startswith(prefix) or path == prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/") or path.startswith(prefix)


def site_covers(site: UserPolicySite, url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme in ("ws", "wss"):
        url = ("https" if scheme == "wss" else "http") + url[len(scheme):]
        parts = urlsplit(url)
        scheme = "https" if scheme == "wss" else "http"
    origin = url_origin(url)
    if origin is None:
        return False
    path = parts.path or "/"
    if origin in site.exact_origins:
        return _path_in(path, site.path_prefix)
    if site.scope == "domain" and scheme == "https":
        host = (parts.hostname or "").lower().rstrip(".")
        return any(host == d or host.endswith("." + d) for d in site.registrable_domains)
    return False


def _fullmatch(pattern: str, value: str) -> bool:
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        return False


# Facts and decisions -----------------------------------------------------------------


@dataclass
class RequestFacts:
    method: str
    url: str
    resource_type: str = "other"
    navigation: bool = False
    frame: str = "main"                 # main, sub, worker
    body: bytes = b""
    content_type: str | None = None
    page: Any = None
    frame_obj: Any = None
    hop: bool = False                   # a redirect hop the guard started
    model_nav: bool = False             # the navigate tool asked for this URL
    popup: bool = False                 # first navigation of a page the site opened
    counted: bool = False               # off-site query bytes already spent
    headers: dict[str, str] = field(default_factory=dict)   # request headers (typed-string rule)

    @property
    def top_level(self) -> bool:
        return self.navigation and self.frame == "main"

    def header_text(self) -> str:
        """Header values a page can set or steer (custom headers, Referer);
        Cookie is skipped (a cross-site page cannot set it)."""
        return "\n".join(v for k, v in self.headers.items() if k.lower() != "cookie" and isinstance(v, str))


@dataclass(frozen=True)
class Decision:
    action: str                         # continue, fetch, login, drop, refuse, hold, site_hold
    reason: str | None = None
    out_of_scope: bool = False
    site: UserPolicySite | None = None
    profile_label: str | None = None
    trigger: str | None = None


@dataclass
class ClickOutcome:
    """`proceed` False means: do not perform the action; tell the model `notice`."""
    proceed: bool
    held: bool = False
    label: str | None = None
    notice: str | None = None


@dataclass
class LoginAttempt:
    credential: UserPolicyCredential
    page: Any
    frame: Any
    element: Any
    login_paths: tuple[str, ...]
    markers: tuple[str, ...]
    used: bool = False
    open: bool = True


CLICK_INFO_JS = r"""
(el, enter) => {
  const clean = s => (s == null ? "" : String(s)).replace(/\s+/g, " ").trim().slice(0, 300);
  const out = {tag: "", label: "", texts: [], hrefs: [], form: false};
  if (!el || !el.tagName) return out;
  const pick = 'a[href],area[href],button,[role=button],[role=link],[role=menuitem],' +
               'input[type=submit],input[type=button],input[type=image],summary';
  const target = (!enter && el.closest) ? (el.closest(pick) || el) : el;
  const add = (node, primary) => {
    if (!node || !node.getAttribute) return;
    const tag = node.tagName.toLowerCase();
    const type = (node.getAttribute("type") || "").toLowerCase();
    const aria = clean(node.getAttribute("aria-label"));
    let labelled = "";
    for (const id of (node.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean)) {
      const n = node.ownerDocument.getElementById(id);
      if (n) labelled += " " + (n.innerText || n.textContent || "");
    }
    labelled = clean(labelled);
    const field = tag === "input" || tag === "textarea" || tag === "select";
    const buttonInput = tag === "input" && ["submit", "button", "reset", "image"].includes(type);
    const text = field ? "" : clean(node.innerText || node.textContent);
    const value = buttonInput ? clean(node.value) : "";
    const title = clean(node.getAttribute("title"));
    const img = node.querySelector ? node.querySelector("img[alt]") : null;
    const alt = clean(node.getAttribute("alt") || (img ? img.getAttribute("alt") : ""));
    for (const t of [aria, labelled, text, value, title, alt]) if (t) out.texts.push(t);
    const href = (tag === "a" || tag === "area") && node.href ? String(node.href).slice(0, 2048) : "";
    if (href) out.hrefs.push(href);
    if (primary) {
      out.tag = tag;
      out.href = href;
      out.label = aria || labelled || text || value || title || alt || "";
    }
  };
  add(target, true);
  if (target !== el) add(el, false);
  const form = target.form || (target.closest ? target.closest("form") : null);
  const tag = target.tagName.toLowerCase();
  const type = (target.getAttribute("type") || "").toLowerCase();
  const submits = (tag === "button" && (type === "" || type === "submit")) ||
                  (tag === "input" && (type === "submit" || type === "image"));
  if (form && (enter || submits)) {
    out.form = true;
    if (form.getAttribute("action")) out.hrefs.push(String(form.action).slice(0, 2048));
    if (enter) {
      for (const b of form.querySelectorAll("button:not([type]),button[type=submit],input[type=submit],input[type=image]")) {
        add(b, false);
        if (!out.label) out.label = out.texts[out.texts.length - 1] || "";
      }
    }
  }
  return out;
}
"""

SAME_FORM_JS = r"""
(b, a) => {
  if (!a || !b || a.ownerDocument !== b.ownerDocument) return false;
  const fa = a.form || (a.closest ? a.closest("form") : null);
  if (!fa) return true;
  const fb = b.form || (b.closest ? b.closest("form") : null);
  return fa === fb || fa.contains(b);
}
"""


def label_is_final(texts: Iterable[str], hrefs: Iterable[str] = ()) -> bool:
    """Does any label or link match the D2 verb list?"""
    for text in texts:
        low = (text or "").lower()
        if "cookie" in low:
            continue
        if VERB_RE.search(VERB_EXCLUDE_RE.sub(" ", low)):
            return True
    for href in hrefs:
        try:
            parts = urlsplit(href)
        except ValueError:
            continue
        tail = ((parts.path or "") + ("?" + parts.query if parts.query else "")).lower()
        if VERB_RE.search(VERB_EXCLUDE_RE.sub(" ", tail)):
            return True
    return False


# The guard ---------------------------------------------------------------------------


class UserGuardV2:
    """The network gate for one user_browser job (see the module docstring)."""

    def __init__(self, policy: UserJobPolicy, broker: HoldBroker, *,
                 typed: TypedStrings | None = None,
                 redact: Callable[[str], str] | None = None,
                 log_step: Callable[..., Any] | None = None,
                 screenshot: Callable[[Any], Awaitable[UUID | None]] | None = None,
                 refresh_policy: Callable[[], Awaitable[UserJobPolicy]] | None = None) -> None:
        if str(policy.job_id) != str(broker.job_id):
            raise ValueError("policy is for another job")
        self.broker = broker
        self.typed = typed or TypedStrings()
        self.redact = redact
        self.log_step = log_step
        self.screenshot = screenshot
        self.refresh_policy = refresh_policy
        self.budget = QueryBudget(policy.caps.offsite_query_bytes)
        self.policy = policy
        self.set_policy(policy)
        self.operator_mode = False
        self.action_description: str | None = None
        self.side_effects = False
        self.released_requests = 0
        self.dropped = 0
        self.blocked: dict[tuple[str, str], dict[str, Any]] = {}
        self._notices: list[str] = []
        self._hops: dict[int, str] = {}          # id(frame) -> canonical next hop URL
        self._model_navs: dict[int, str] = {}    # id(page) -> canonical URL the model asked for
        self._popups: set[int] = set()           # id(page) of pages the site opened
        self._navigated: set[int] = set()        # id(page) that made a first navigation
        self._attempt: LoginAttempt | None = None
        self._tasks: set[asyncio.Task] = set()

    # Policy ------------------------------------------------------------------------

    def set_policy(self, policy: UserJobPolicy) -> None:
        if str(policy.job_id) != str(self.broker.job_id):
            raise ValueError("policy is for another job")
        self.policy = policy
        self.start_origin = policy.start_origin
        self.budget.set_cap(policy.caps.offsite_query_bytes)
        self.broker.timeout_seconds = float(policy.caps.hold_timeout_seconds)
        self.max_response_bytes = max(MIN_RESPONSE_CAP, policy.caps.download_file_bytes)

    def site_for(self, url: str) -> UserPolicySite | None:
        return next((s for s in self.policy.sites if site_covers(s, url)), None)

    def in_scope(self, url: str) -> bool:
        return self.site_for(url) is not None

    async def _refresh(self, version: int | None) -> None:
        if self.refresh_policy is None:
            return
        if version is not None and version <= self.policy.policy_version:
            return
        try:
            self.set_policy(await self.refresh_policy())
        except Exception as exc:
            log.warning("policy refresh failed: %s", type(exc).__name__)

    # Model-facing helpers ------------------------------------------------------------

    def set_action_description(self, text: str | None) -> None:
        self.action_description = (text or "")[:300] or None

    def _notice(self, text: str) -> None:
        if self.redact is not None:
            text = self.redact(text)
        if text not in self._notices:
            self._notices.append(text)

    def take_notices(self) -> list[str]:
        out, self._notices = self._notices, []
        return out

    @property
    def holds_open(self) -> int:
        return self.broker.open_count

    async def wait_holds(self, timeout: float | None = None) -> bool:
        """Wait until no hold is open (the model loop is paused meanwhile)."""
        ok = await self.broker.wait_idle(timeout)
        await self.settle()
        return ok

    async def settle(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _step(self, kind: str, summary: str, **fields: Any) -> None:
        if self.log_step is None:
            return
        try:
            self.log_step(kind, summary, **fields)
        except Exception as exc:
            log.warning("guard step %s not logged: %s", kind, exc)

    def _record_block(self, method: str, url: str, reason: str) -> None:
        where = trace_url(url)
        key = (where, method)
        rec = self.blocked.get(key)
        if rec is not None:
            rec["count"] += 1
            return
        self.blocked[key] = {"method": method, "url": where, "reason": reason, "count": 1}
        self._step("http.blocked", f"Blocked {method} {where} ({reason})", ok=False,
                   result={"method": method, "url": where, "reason": reason})

    def prepare_navigation(self, page: Any, url: str, *, page_links: Iterable[str] = (),
                           start_url: str | None = None) -> Verdict:
        """The navigate tool calls this before page.goto(url). Runs the floor
        and the navigate URL rule; on success the next main-frame request for
        exactly this URL on `page` is treated as the model's own navigation
        (site hold trigger "navigate")."""
        floor = floor_verdict(url, top_level=True)
        if not floor:
            return floor
        verdict = check_navigate_url(url, page_links=page_links, start_url=start_url)
        if not verdict:
            self._record_block("NAVIGATE", url, verdict.reason or "refused")
            return verdict
        if page is not None:
            self._model_navs[id(page)] = canonical_url(url)
        return verdict

    # Decision (pure apart from the off-site query budget) ------------------------------

    def decide(self, f: RequestFacts, *, force_hold: bool = False) -> Decision:
        method = f.method.upper()
        top = f.top_level
        floor = floor_verdict(f.url, top_level=top)
        if not floor:
            return Decision("refuse", floor.reason)
        scheme = urlsplit(f.url).scheme.lower()
        if scheme in ("data", "blob", "about"):
            return Decision("continue")
        host = (urlsplit(f.url).hostname or "").lower()
        if f.resource_type in DROP_RESOURCE_TYPES:
            return Decision("drop", "beacon")
        if is_analytics_host(host):
            return Decision("drop", "analytics")
        safe = method in SAFE_METHODS
        if len(f.url) > URL_MAX and (not safe or f.navigation):
            return Decision("refuse", "url_too_long")
        site = self.site_for(f.url)
        in_scope = site is not None
        offsite = is_offsite(f.url, self.start_origin)
        # URL, body and header values (a page can put typed text in a custom
        # header, or in its own URL and then send it as Referer).
        carries_typed = self.typed.found_in(f.url, f.body) or self.typed.found_in("", f.header_text())
        typed_hit = offsite and carries_typed
        if typed_hit and (safe or not in_scope):
            # In-scope non-GETs carrying typed text still go to the owner's
            # card below (never auto-released); everything else is refused.
            return Decision("refuse", "typed_string_offsite", out_of_scope=not in_scope)
        if top and safe:
            if offsite and not f.hop and not f.counted:
                f.counted = True
                if not self.budget.spend(f.url):
                    return Decision("refuse", "offsite_query_cap")
            if not in_scope:
                if is_identity_provider_host(host):
                    return Decision("refuse", "identity_provider", out_of_scope=True)
                trigger = ("redirect" if f.hop else "navigate" if f.model_nav else
                           "popup" if f.popup else "link")
                return Decision("site_hold", "out_of_scope", out_of_scope=True, trigger=trigger)
        if not safe:
            if not force_hold and not typed_hit:
                if self._login_exempt(f):
                    return Decision("login", "login_form", site=site)
                if in_scope and self._safe_mutation(method, f.url, f.body, site):
                    return Decision("fetch", "safe_mutation", site=site)
            return Decision("hold", "final_action", out_of_scope=not in_scope, site=site,
                            profile_label=self._profile_label(method, f.url, site))
        if site is not None and method == "GET":
            label = self._final_get(f.url, site)
            if label is not None:
                return Decision("hold", "final_get", site=site, profile_label=label or None)
        if f.navigation:
            return Decision("fetch")
        if carries_typed:
            # On the start origin typed text may go out, but Chromium would
            # follow a redirect of a continued request itself, unseen (an
            # open redirect launders the text off-site). Fetch it instead:
            # _deliver re-decides every hop, and the off-site one is refused.
            return Decision("fetch", "typed_on_start_origin")
        return Decision("continue")

    def _safe_mutation(self, method: str, url: str, body: bytes, site: UserPolicySite | None) -> bool:
        if site is None:
            return False
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path or "/"
        if VERB_RE.search(VERB_EXCLUDE_RE.sub(" ", path.lower())):
            return False   # floor: a path naming a final verb is never auto-released
        for rule in site.profile.safe_mutations:
            if rule.method != method or rule.host != host or not _fullmatch(rule.path_regex, path):
                continue
            if rule.graphql_operations is None:
                return True
            if _graphql_ops_allowed(body, rule.graphql_operations):
                return True
        for rule in self.policy.safe_rules:
            if (rule.site_key == site.site_key and rule.method == method and rule.host == host
                    and _fullmatch(rule.path_pattern, path)):
                return True
        return False

    def _profile_label(self, method: str, url: str, site: UserPolicySite | None) -> str | None:
        if site is None:
            return None
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        for rule in site.profile.final_actions:
            if rule.method == method and rule.host == host and _fullmatch(rule.path_regex, parts.path or "/"):
                return rule.label
        return None

    def _final_get(self, url: str, site: UserPolicySite) -> str | None:
        """'' or the rule label when a profile final_get rule matches, else None."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        for rule in site.profile.final_get:
            if rule.host == host and _fullmatch(rule.path_regex, parts.path or "/"):
                return rule.label or ""
        return None

    def _login_exempt(self, f: RequestFacts) -> bool:
        a = self._attempt
        if a is None or not a.open:
            return False
        if url_origin(f.url) not in a.credential.login_origins:
            return False
        if f.page is None or f.page is not a.page:
            return False
        path = urlsplit(f.url).path or "/"
        if a.login_paths:
            return any(_fullmatch(p, path) for p in a.login_paths)
        if a.used or f.frame_obj is None or f.frame_obj is not a.frame:
            return False
        if f.navigation:
            a.used = True
            return True
        if f.resource_type in ("fetch", "xhr") and a.markers and f.body:
            text = f.body.decode("utf-8", errors="ignore")
            if any(m and m in text for m in a.markers):
                a.used = True
                return True
        return False

    # Login attempts ----------------------------------------------------------------

    async def open_login_attempt(self, *, credential_id: UUID | str, page: Any, element: Any,
                                 markers: Iterable[str] = ()) -> None:
        """fill_credential (WI-10) calls this after it filled a field. Raises
        ValueError when the credential is not in the job or the page is not on
        one of its login origins."""
        cred = self.policy.credential(credential_id)
        if cred is None:
            raise ValueError("credential is not in this job")
        if url_origin(getattr(page, "url", "") or "") not in cred.login_origins:
            raise ValueError("page is not on the credential's login origin")
        handle = await _as_handle(element)
        frame = await handle.owner_frame() if handle is not None else None
        site = self.policy.site(cred.site_key)
        paths = tuple(site.login_paths) if site is not None and site.kind == "curated" else ()
        self._attempt = LoginAttempt(credential=cred, page=page, frame=frame, element=handle,
                                     login_paths=paths, markers=tuple(m for m in markers if m))
        self._step("login.exemption_open", "Login form exemption opened",
                   result={"origin": url_origin(page.url), "kind": "curated" if paths else "ad_hoc"})

    def close_login_attempt(self, reason: str = "closed") -> None:
        a = self._attempt
        if a is not None and a.open:
            a.open = False
            a.markers = ()
            self._step("login.exemption_closed", f"Login form exemption closed ({reason})")
        self._attempt = None

    @property
    def login_open(self) -> bool:
        return self._attempt is not None and self._attempt.open

    async def _in_login_form(self, element: Any) -> bool:
        a = self._attempt
        if a is None or a.element is None:
            return False
        handle = await _as_handle(element)
        if handle is None:
            return False
        try:
            return bool(await handle.evaluate(SAME_FORM_JS, a.element))
        except Exception:
            return False

    # Click-level holds and interaction checks ------------------------------------------

    async def check_interaction(self, page: Any, element: Any) -> ClickOutcome:
        """type_text, select_option and press_key (not Enter): refused outside the
        filled form while a login attempt is open."""
        if self.login_open and not await self._in_login_form(element):
            return ClickOutcome(False, notice="A sign-in is in progress: only the sign-in form can be used now.")
        return ClickOutcome(True)

    async def check_click(self, page: Any, element: Any, *, action: str = "click") -> ClickOutcome:
        """Before the worker clicks `element` (action "click") or presses Enter
        on it ("enter"): a click-level final-action hold when its label or link
        matches the D2 verb list (6.2 item 4)."""
        if self.login_open:
            if await self._in_login_form(element):
                return ClickOutcome(True)
            return ClickOutcome(False, notice="A sign-in is in progress: only the sign-in form can be used now.")
        handle = await _as_handle(element)
        if handle is None:
            return ClickOutcome(True)
        try:
            info = await handle.evaluate(CLICK_INFO_JS, action == "enter")
        except Exception:
            return ClickOutcome(True)
        texts = [t for t in info.get("texts", []) if isinstance(t, str)]
        hrefs = [h for h in info.get("hrefs", []) if isinstance(h, str)]
        if not label_is_final(texts, hrefs):
            return ClickOutcome(True)
        label = (info.get("label") or next(iter(texts), "") or next(iter(hrefs), "") or "this button")[:200]
        if self.redact is not None:
            label = self.redact(label)[:200]
        href = info.get("href") or None
        card_url = href if href and url_origin(href) else getattr(page, "url", "")
        origin = url_origin(card_url or "")
        if origin is None or not ORIGIN_RE.fullmatch(origin) or len(card_url) > URL_MAX:
            self._record_block("CLICK", card_url or "", "click_hold_unavailable")
            return ClickOutcome(False, held=True, label=label,
                                notice=f"Not clicked: \"{label}\" looks like a final action and cannot be approved here.")
        body = click_descriptor_bytes(action=action, tag=str(info.get("tag") or ""), label=label, href=href)
        req = UserHoldRequest(trigger="click", frame=_frame_kind(await _handle_frame(handle)), method=CLICK_METHOD,
                              url=card_url, origin=origin, url_sha256=sha256_hex(card_url),
                              body_sha256=sha256_hex(body), element_label=label,
                              model_description=self._description(),
                              out_of_scope=not self.in_scope(card_url),
                              screenshot_artifact_id=await self._screenshot(page))
        self._step("hold.click", f"Click held: {label}", result={"url": trace_url(card_url)})
        result = await self.broker.hold_final(req, label=label)
        self.side_effects = self.side_effects or result.status in ("released", "declined")
        if result.released:
            return ClickOutcome(True, held=True, label=label)
        if result.notice:
            self._notice(result.notice)
        return ClickOutcome(False, held=True, label=label, notice=result.notice)

    def _description(self) -> str | None:
        text = self.action_description
        if text and self.redact is not None:
            text = self.redact(text)
        return text[:300] if text else None

    async def _screenshot(self, page: Any) -> UUID | None:
        """Never on a login origin or identity provider, never in operator mode."""
        if self.screenshot is None or page is None or self.operator_mode:
            return None
        origin = url_origin(getattr(page, "url", "") or "")
        if origin is None:
            return None
        if any(origin in c.login_origins for c in self.policy.credentials):
            return None
        if is_identity_provider_host(urlsplit(origin).hostname or ""):
            return None
        try:
            return await asyncio.wait_for(self.screenshot(page), SCREENSHOT_TIMEOUT_S)
        except Exception as exc:
            log.info("hold screenshot failed: %s", type(exc).__name__)
            return None

    # Wiring ------------------------------------------------------------------------

    async def install(self, context: Any) -> None:
        """Wire the guard into one job's BrowserContext (before any page loads)."""
        await context.add_init_script(EXFIL_INIT_SCRIPT)
        await context.route("**/*", self._on_route)
        await context.route_web_socket(re.compile(r".*"), self._on_websocket)
        context.on("page", self._adopt_page)
        for page in list(getattr(context, "pages", [])):
            self._adopt_page(page)
        with contextlib.suppress(Exception):
            await context.clear_permissions()

    def _adopt_page(self, page: Any) -> None:
        if getattr(page, "_bdr_guard_v2", False):
            return
        with contextlib.suppress(Exception):
            page._bdr_guard_v2 = True
        page.on("popup", lambda p: self._popups.add(id(p)))
        page.on("framenavigated", lambda frame, p=page: self._on_navigated(p, frame))

    def _on_navigated(self, page: Any, frame: Any) -> None:
        a = self._attempt
        if a is None or not a.open or page is not a.page:
            return
        try:
            if frame.parent_frame is not None:
                return
            origin = url_origin(frame.url)
        except Exception:
            return
        if origin not in a.credential.login_origins:
            self.close_login_attempt("left_login_page")

    # Route handling ----------------------------------------------------------------

    def _facts(self, request: Any) -> RequestFacts:
        method = (request.method or "GET").upper()
        url = request.url
        try:
            rtype = request.resource_type or "other"
        except Exception:
            rtype = "other"
        try:
            navigation = bool(request.is_navigation_request())
        except Exception:
            navigation = False
        frame_obj = page = None
        # A navigation whose frame cannot be read (a popup's first request
        # arrives before its page object exists) is treated as top level.
        kind = "main" if navigation else "worker"
        try:
            frame_obj = request.frame
        except Exception:
            frame_obj = None
        if frame_obj is not None:
            try:
                kind = "main" if frame_obj.parent_frame is None else "sub"
            except Exception:
                kind = "main" if navigation else "worker"
            try:
                page = frame_obj.page
            except Exception:
                page = None
        try:
            body = request.post_data_buffer or b""
        except Exception:
            body = b""
        try:
            headers = {str(k): str(v) for k, v in (request.headers or {}).items()}
        except Exception:
            headers = {}
        ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), None)
        f = RequestFacts(method=method, url=url, resource_type=rtype, navigation=navigation, frame=kind,
                         body=body, content_type=ctype, page=page, frame_obj=frame_obj, headers=headers)
        if navigation and frame_obj is not None:
            expected = self._hops.pop(id(frame_obj), None)
            if expected is not None and expected == canonical_url(url):
                f.hop = True
            if kind == "main" and page is not None:
                wanted = self._model_navs.pop(id(page), None)
                f.model_nav = wanted is not None and wanted == canonical_url(url)
                if id(page) not in self._navigated:
                    self._navigated.add(id(page))
                    f.popup = id(page) in self._popups
        if navigation and kind == "main" and page is None:
            f.popup = True    # a new page's first request, before its page object exists
        return f

    async def _on_route(self, route: Any) -> None:
        try:
            request = route.request
            if getattr(request, "service_worker", None) is not None:
                await self._abort(route)
                return
            f = self._facts(request)
            kind, clean = split_worker_marker(f.url)
            if kind is not None and f.method == "GET" and not f.navigation:
                await self._worker_script(route, replace(f, url=clean), kind)
                return
            d = self.decide(f)
            await self._execute(route, f, d)
        except Exception as exc:   # never let a request through on an error
            log.warning("guard route failed: %s", type(exc).__name__)
            await self._abort(route)

    async def _worker_script(self, route: Any, f: RequestFacts, kind: str) -> None:
        """A worker main script marked by the Worker wrapper (exfil.WORKER_SCOPE_JS):
        the request for the real script is decided as usual; when it may load,
        the worker gets a bootstrap that removes its socket APIs first, then
        loads the real script (a second, ordinary routed request)."""
        d = self.decide(f)
        if d.action not in ("continue", "fetch"):
            if d.action == "refuse":
                self._refused(f, d)
            await self._abort(route)
            return
        await route.fulfill(status=200, body=worker_bootstrap(kind, f.url), headers={
            "content-type": "text/javascript; charset=utf-8", "cache-control": "no-store"})

    async def _execute(self, route: Any, f: RequestFacts, d: Decision) -> None:
        if d.action == "continue":
            try:
                await route.continue_()
            except Exception as exc:
                log.debug("route continue failed: %s", exc)
        elif d.action == "drop":
            self.dropped += 1
            await self._abort(route)
        elif d.action == "refuse":
            self._refused(f, d)
            await self._abort(route)
        elif d.action == "fetch":
            await self._deliver(route, f)
        elif d.action == "login":
            self._step("login.exempt", "Login form request released",
                       result={"method": f.method, "url": trace_url(f.url)})
            await self._deliver(route, f, login=True)
        elif d.action == "hold":
            await self._hold_and_deliver(route, f, d)
        elif d.action == "site_hold":
            await self._site_hold(route, f, d)
        else:
            await self._abort(route)

    @staticmethod
    async def _abort(route: Any) -> None:
        try:
            await route.abort("blockedbyclient")
        except Exception as exc:
            log.debug("route abort failed: %s", exc)

    def _refused(self, f: RequestFacts, d: Decision) -> None:
        reason = d.reason or "refused"
        self._record_block(f.method, f.url, reason)
        where = trace_url(f.url)
        if reason == "typed_string_offsite":
            self._notice(f"Blocked: the page tried to send text you typed to another site ({where}).")
        elif reason == "identity_provider":
            self._notice(f"The site sent the browser to a single sign-on page ({where}). "
                         "Use user_request_help with reason sso.")
        elif reason == "offsite_query_cap":
            self._notice("Blocked: this task has sent its limit of data in links to other sites.")
        elif f.navigation or f.method not in SAFE_METHODS:
            self._notice(f"Blocked {f.method} {where} ({reason}).")

    def _hold_request(self, f: RequestFacts, d: Decision) -> UserHoldRequest | None:
        origin = url_origin(f.url)
        if origin is None or not ORIGIN_RE.fullmatch(origin) or len(f.url) > URL_MAX:
            return None
        url_sha, body_sha = request_hashes(f.url, f.body)
        summary = summarize_body(f.body, f.content_type, self.typed, redact=self.redact)
        try:
            return UserHoldRequest(trigger="network", frame=f.frame, method=f.method.upper(), url=f.url,
                                   origin=origin, url_sha256=url_sha, body_sha256=body_sha, body_summary=summary,
                                   profile_label=d.profile_label, model_description=self._description(),
                                   out_of_scope=d.out_of_scope)
        except Exception as exc:
            log.info("hold request invalid: %s", type(exc).__name__)
            return None

    async def _hold(self, f: RequestFacts, d: Decision) -> HoldResult:
        req = self._hold_request(f, d)
        if req is None:
            return HoldResult("refused", reason="hold_unavailable",
                              notice=f"Blocked {f.method} {trace_url(f.url)}: it cannot be approved.")
        shot = await self._screenshot(f.page)
        if shot is not None:
            req = req.model_copy(update={"screenshot_artifact_id": shot})
        label = d.profile_label or f"{f.method} {trace_url(f.url)}"
        self._step("hold.final_action", f"Held {f.method} {trace_url(f.url)}",
                   result={"method": f.method, "url": trace_url(f.url), "out_of_scope": d.out_of_scope,
                           "reason": d.reason})
        result = await self.broker.hold_final(req, label=label)
        if result.status in ("released", "declined"):
            self.side_effects = True
        return result

    async def _hold_and_deliver(self, route: Any, f: RequestFacts, d: Decision) -> None:
        result = await self._hold(f, d)
        if not result.released:
            self._record_block(f.method, f.url, f"hold_{result.status}")
            if result.notice:
                self._notice(result.notice)
            await self._abort(route)
            return
        self.released_requests += 1
        await self._deliver(route, f, released=True)

    async def _site_hold(self, route: Any, f: RequestFacts, d: Decision) -> None:
        target_origin = url_origin(f.url)
        if target_origin is None or not ORIGIN_RE.fullmatch(target_origin):
            self._refused(f, Decision("refuse", "bad_origin"))
            await self._abort(route)
            return
        from_origin = None
        with contextlib.suppress(Exception):
            current = url_origin(f.page.url) if f.page is not None else None
            if current and ORIGIN_RE.fullmatch(current) and current != target_origin:
                from_origin = current
        parts = urlsplit(f.url)
        req = UserSiteHoldRequest(trigger=d.trigger or "link", url=f.url, target_origin=target_origin,
                                  from_origin=from_origin, carries_data=bool(parts.query or parts.fragment))
        self._step("hold.site", f"Site held: {target_origin}",
                   result={"url": trace_url(f.url), "trigger": req.trigger})
        result = await self.broker.hold_site(req, label=target_origin)
        if not result.released:
            self._record_block("NAVIGATE", f.url, f"site_{result.status}")
            if result.notice:
                self._notice(result.notice)
            await self._abort(route)
            return
        await self._refresh(result.policy_version)
        if result.site_release == "site_only":
            await self._abort(route)
            root = target_origin + "/"
            if not self.in_scope(root):
                self._notice(f"{target_origin} was approved but is not in this task's sites yet.")
                return
            self._notice(f"The owner approved {target_origin}; its home page was opened instead of the exact link.")
            if f.frame_obj is not None:
                self._spawn(_goto_quietly(f.frame_obj, root))
            return
        again = self.decide(replace(f, counted=True))
        if again.action == "site_hold":
            self._refused(f, Decision("refuse", "still_out_of_scope"))
            await self._abort(route)
            return
        await self._execute(route, replace(f, counted=True), again)

    async def _deliver(self, route: Any, f: RequestFacts, *, login: bool = False, released: bool = False) -> None:
        """route.fetch(max_redirects=0) and deliver, handling each 3xx hop."""
        cur_url, cur_method, cur_body = f.url, f.method.upper(), f.body
        hops = 0
        while True:
            kwargs: dict[str, Any] = {"max_redirects": 0, "timeout": FETCH_TIMEOUT_MS}
            if cur_url != f.url:
                kwargs["url"] = cur_url
            if cur_method != f.method.upper():
                kwargs["method"] = cur_method
            if cur_body != f.body:
                kwargs["post_data"] = cur_body
            response = None
            try:
                try:
                    response = await route.fetch(**kwargs)
                except Exception as exc:
                    log.info("guarded fetch failed: %s", type(exc).__name__)
                    if released:
                        self._notice("The page stopped waiting; repeat the action to ask again.")
                    self._record_block(cur_method, cur_url, "transport_failed")
                    await self._abort(route)
                    return
                if login:
                    login = False
                    self.close_login_attempt("response")
                status = response.status
                headers = {k.lower(): v for k, v in response.headers.items()}
                location = headers.get("location")
                if 300 <= status < 400 and location:
                    hops += 1
                    target = canonical_url(urljoin(cur_url, location.strip()))
                    if hops > MAX_HOPS:
                        self._refused(f, Decision("refuse", "redirect_limit"))
                        await self._abort(route)
                        return
                    if status in (307, 308) and cur_method not in SAFE_METHODS:
                        # The body would be re-sent: a new hold, never an automatic follow.
                        hop = replace(f, url=target, hop=True, method=cur_method, body=cur_body, counted=True)
                        d2 = self.decide(hop, force_hold=True)
                        if d2.action != "hold":
                            self._refused(hop, d2 if d2.action == "refuse" else Decision("refuse", "redirect_refused"))
                            await self._abort(route)
                            return
                        result = await self._hold(hop, d2)
                        if not result.released:
                            self._record_block(cur_method, target, f"hold_{result.status}")
                            if result.notice:
                                self._notice(result.notice)
                            await self._abort(route)
                            return
                        self.released_requests += 1
                        released = True
                        cur_url = target
                        continue
                    if f.navigation and f.frame_obj is not None:
                        self._hops[id(f.frame_obj)] = target
                        body = ('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url='
                                + html.escape(target, quote=True) + '"><title>Continuing</title>')
                        await route.fulfill(status=200, body=body, headers={
                            "content-type": "text/html; charset=utf-8", "content-security-policy": INERT_CSP,
                            "cache-control": "no-store"})
                        return
                    hop = RequestFacts(method="GET", url=target, resource_type=f.resource_type, navigation=False,
                                       frame=f.frame, page=f.page, frame_obj=f.frame_obj, hop=True, counted=True,
                                       headers=_hop_headers(f.headers))
                    d2 = self.decide(hop)
                    if d2.action == "hold":
                        result = await self._hold(hop, d2)
                        if not result.released:
                            if result.notice:
                                self._notice(result.notice)
                            await self._abort(route)
                            return
                    elif d2.action not in ("continue", "fetch"):
                        if d2.action == "refuse":
                            self._refused(hop, d2)
                        await self._abort(route)
                        return
                    cur_url, cur_method, cur_body = target, "GET", b""
                    continue
                length = headers.get("content-length", "")
                if length.isdigit() and int(length) > self.max_response_bytes:
                    self._refused(f, Decision("refuse", "response_too_large"))
                    await self._abort(route)
                    return
                data = await response.body()
                if len(data) > self.max_response_bytes:
                    self._refused(f, Decision("refuse", "response_too_large"))
                    await self._abort(route)
                    return
                out_headers = {k: v for k, v in response.headers.items() if k.lower() not in STRIP_RESPONSE_HEADERS}
                await route.fulfill(status=status, headers=out_headers, body=data)
                return
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):
                        await response.dispose()

    # WebSockets --------------------------------------------------------------------

    async def _on_websocket(self, ws: Any) -> None:
        url = ws.url
        try:
            http_url = re.sub(r"^ws", "http", url, count=1, flags=re.IGNORECASE)
            floor = floor_verdict(url, top_level=False)
            site = self.site_for(http_url) if floor else None
            offsite = is_offsite(http_url, self.start_origin)
            reason = None
            if not floor:
                reason = floor.reason or "refused"
            elif offsite and self.typed.found_in(url):
                reason = "typed_string_offsite"
            elif site is None or not site.websocket_allowed:
                reason = "websocket"
            if reason is not None:
                self._record_block("WEBSOCKET", http_url, reason)
                await ws.close()
                return
            server = ws.connect_to_server()

            def from_page(message: str | bytes) -> None:
                if offsite and self.typed.found_in("", message if isinstance(message, (bytes, str)) else b""):
                    self._record_block("WEBSOCKET", http_url, "typed_string_offsite")
                    self._notice(f"Blocked: the page tried to send text you typed to another site ({trace_url(http_url)}).")
                    self._spawn(_close_ws(ws, server))
                    return
                server.send(message)

            ws.on_message(from_page)
            self._step("http.websocket", f"WebSocket allowed {trace_url(http_url)}",
                       result={"url": trace_url(http_url), "site": site.site_key})
        except Exception as exc:
            log.info("websocket handling failed: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await ws.close()


# Helpers -------------------------------------------------------------------------------


def _hop_headers(headers: dict[str, str]) -> dict[str, str]:
    """Headers a GET redirect hop carries: route.fetch re-sends the request's
    headers (without a body)."""
    return {k: v for k, v in headers.items() if k.lower() not in ("content-type", "content-length")}


def _graphql_ops_allowed(body: bytes, allowed: list[str]) -> bool:
    import json

    try:
        data = json.loads((body or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    ops = data if isinstance(data, list) else [data]
    if not ops:
        return False
    for op in ops:
        if not isinstance(op, dict):
            return False
        name = op.get("operationName")
        query = op.get("query")
        if not isinstance(name, str) or name not in allowed:
            return False
        if VERB_RE.search(name.lower()):
            return False
        if isinstance(query, str) and _GRAPHQL_MUTATION_RE.search(query):
            return False
    return True


def _frame_kind(frame: Any) -> str:
    if frame is None:
        return "worker"
    try:
        return "main" if frame.parent_frame is None else "sub"
    except Exception:
        return "main"


async def _as_handle(element: Any) -> Any:
    """An ElementHandle from an ElementHandle or a Locator."""
    if element is None:
        return None
    if hasattr(element, "owner_frame"):
        return element
    if hasattr(element, "element_handle"):
        try:
            return await element.element_handle(timeout=5_000)
        except Exception:
            return None
    return None


async def _handle_frame(handle: Any) -> Any:
    try:
        return await handle.owner_frame()
    except Exception:
        return None


async def _goto_quietly(frame: Any, url: str) -> None:
    try:
        await frame.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as exc:
        log.debug("guard navigation failed: %s", type(exc).__name__)


async def _close_ws(ws: Any, server: Any) -> None:
    with contextlib.suppress(Exception):
        await server.close()
    with contextlib.suppress(Exception):
        await ws.close()


# Factory -------------------------------------------------------------------------------


def build_guard(policy: UserJobPolicy, *, send: Send, lease_epoch: Callable[[], int],
                verifier: HoldTokenVerifier | None = None, typed: TypedStrings | None = None,
                redact: Callable[[str], str] | None = None, log_step: Callable[..., Any] | None = None,
                screenshot: Callable[[Any], Awaitable[UUID | None]] | None = None,
                refresh_policy: Callable[[], Awaitable[UserJobPolicy]] | None = None) -> UserGuardV2:
    """The guard for one job, its holds bound to USER_V2_ENDPOINTS through the
    worker client's lease-fenced `send(method, path, json, query) -> dict`."""
    broker = HoldBroker(transport=EndpointHoldTransport(send, policy.job_id), verifier=verifier or HoldTokenVerifier(),
                        job_id=policy.job_id, lease_epoch=lease_epoch,
                        timeout_seconds=float(policy.caps.hold_timeout_seconds))
    return UserGuardV2(policy, broker, typed=typed, redact=redact, log_step=log_step, screenshot=screenshot,
                       refresh_policy=refresh_policy)


__all__ = [
    "ClickOutcome", "Decision", "RequestFacts", "UserGuardV2", "VERB_RE", "build_guard", "canonical_url",
    "floor_verdict", "host_floor_verdict", "ip_is_public", "label_is_final", "site_covers",
]
