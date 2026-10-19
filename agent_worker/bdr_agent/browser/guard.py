"""Domain ceiling and read-only network guard (spec 4.3, D12).

Two layers share one policy:

  Pure policy (`Ceiling`, `check_navigation`, `check_request`): what a URL,
  method and page may do. Tested without a browser.

  `Guard`: wires that policy into one job's BrowserContext:
    - context.route('**/*'): GET, HEAD and OPTIONS to ceiling hosts pass;
      POST passes only when the request comes from the login page object, to
      a login-chain path on the login host or to a login challenge host;
      everything else aborts with one `http.blocked` step per host+method;
    - context.route_web_socket: every socket is closed before it connects;
    - popups and pages this code did not open are closed and logged;
    - dialogs are dismissed;
    - every main-frame navigation of the model page is re-checked after the
      fact (Playwright does not route the follow-up request of a redirect),
      and a violation sends the page to about:blank;
    - main-frame navigations are counted against MAX_NAVIGATIONS_PER_JOB.

The ceiling is fixed in code per agent type (protocol.CEILINGS plus the
vendored login paths); Settings `host_narrowing` can only shrink nav hosts.
A new host means a new signed release. Requests made with
`context.request` (APIRequestContext) bypass routing entirely, so the REST
site tools enforce their own allowlist (agents/pm_apps/procore/rest.py).
"""

import asyncio
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..protocol import CEILINGS, MAX_NAVIGATIONS_PER_JOB, TOKEN_QUERY_KEY_RE

log = logging.getLogger("bdr_agent.browser.guard")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_LOCAL_SCHEMES = ("data", "blob", "about")


# Policy ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.ok


ALLOW = Verdict(True)


def _deny(reason: str) -> Verdict:
    return Verdict(False, reason)


def normalize_host(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


def host_matches(host: str, pattern: str) -> bool:
    """Exact match, or `*.example.com` for any subdomain (not the apex)."""
    host, pattern = normalize_host(host), normalize_host(pattern)
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and len(host) > len(pattern) - 1
    return host == pattern


def host_in(host: str, patterns: Iterable[str]) -> bool:
    return any(host_matches(host, p) for p in patterns)


def token_query_keys(url: str) -> list[str]:
    """Query keys that look like tokens, signatures or sessions (refused)."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return []
    return [k for k, _v in parse_qsl(query, keep_blank_values=True) if TOKEN_QUERY_KEY_RE.search(k)]


@dataclass(frozen=True)
class Ceiling:
    """The code-fixed reach of one agent type."""
    agent_type: str
    nav_hosts: tuple[str, ...]
    nav_paths: tuple[re.Pattern[str], ...]
    resource_hosts: tuple[str, ...] = ()
    login_hosts: tuple[str, ...] = ()
    login_challenge_hosts: tuple[str, ...] = ()
    # Paths the login page object may load (GET, main frame) and POST to on a
    # login host (the vendored _LOGIN_LOGIN_PATHS).
    login_paths: tuple[re.Pattern[str], ...] = ()
    # Paths the login page object may pass through on a nav host (OAuth
    # entry and callback). Never available to the model page.
    login_app_paths: tuple[re.Pattern[str], ...] = ()

    @classmethod
    def from_protocol(cls, agent_type: str, *, api_host: str = "",
                      login_paths: Iterable[re.Pattern[str]] = (),
                      login_app_paths: Iterable[re.Pattern[str]] = ()) -> "Ceiling":
        raw = CEILINGS[agent_type]
        hosts = tuple(api_host if h == "$api_host" else h for h in raw["nav_hosts"])
        return cls(
            agent_type=agent_type,
            nav_hosts=tuple(normalize_host(h) for h in hosts if h),
            nav_paths=tuple(re.compile(p) for p in raw["nav_paths"]),
            resource_hosts=tuple(raw.get("resource_hosts") or ()),
            login_hosts=tuple(raw.get("login_hosts") or ()),
            login_challenge_hosts=tuple(raw.get("login_challenge_hosts") or ()),
            login_paths=tuple(login_paths),
            login_app_paths=tuple(login_app_paths),
        )

    def narrowed(self, host_narrowing: list[str] | None) -> "Ceiling":
        """Settings can only remove nav hosts: keep the intersection. An entry
        outside the code ceiling is ignored (it can never widen reach)."""
        if host_narrowing is None:
            return self
        keep = {normalize_host(h) for h in host_narrowing}
        return replace(self, nav_hosts=tuple(h for h in self.nav_hosts if h in keep))


def _url_parts(url: str) -> tuple[str, str, str, int | None, bool] | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    has_userinfo = "@" in (parts.netloc or "")
    return parts.scheme.lower(), normalize_host(parts.hostname), parts.path or "/", port, has_userinfo


def _path_ok(path: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(p.match(path) for p in patterns)


def check_navigation(ceiling: Ceiling, url: str) -> Verdict:
    """May the model page's main frame show this URL? (navigate, clicks,
    redirects). about:blank is always fine."""
    if url in ("about:blank", ""):
        return ALLOW
    parsed = _url_parts(url)
    if parsed is None:
        return _deny("bad_url")
    scheme, host, path, port, userinfo = parsed
    if scheme != "https":
        return _deny("scheme_not_allowed")
    if userinfo:
        return _deny("credentials_in_url")
    if port not in (None, 443):
        return _deny("port_not_allowed")
    if not host_in(host, ceiling.nav_hosts):
        return _deny("host_not_allowed")
    if not _path_ok(path, ceiling.nav_paths):
        return _deny("path_not_allowed")
    if token_query_keys(url):
        return _deny("token_query")
    return ALLOW


def check_same_document_navigation(ceiling: Ceiling, url: str) -> Verdict:
    """A same-document URL change (history.pushState/replaceState, a hash
    change) on the model page: no document is fetched, the page's own data
    requests still pass the route ceiling, so only the origin is re-checked.
    SPA routers rewrite paths and filter queries freely (spec 4.3 re-check)."""
    if url in ("about:blank", ""):
        return ALLOW
    parsed = _url_parts(url)
    if parsed is None:
        return _deny("bad_url")
    scheme, host, _path, port, userinfo = parsed
    if scheme != "https":
        return _deny("scheme_not_allowed")
    if userinfo:
        return _deny("credentials_in_url")
    if port not in (None, 443):
        return _deny("port_not_allowed")
    if not host_in(host, ceiling.nav_hosts):
        return _deny("host_not_allowed")
    return ALLOW


def check_login_navigation(ceiling: Ceiling, url: str) -> Verdict:
    """May the login page object's main frame load this URL?"""
    if url in ("about:blank", ""):
        return ALLOW
    parsed = _url_parts(url)
    if parsed is None:
        return _deny("bad_url")
    scheme, host, path, port, userinfo = parsed
    if scheme != "https" or userinfo or port not in (None, 443):
        return _deny("scheme_not_allowed")
    if host_in(host, ceiling.login_hosts):
        return ALLOW if _path_ok(path, ceiling.login_paths) else _deny("login_path_not_allowed")
    if host_in(host, ceiling.nav_hosts):
        if _path_ok(path, ceiling.login_app_paths) or _path_ok(path, ceiling.nav_paths):
            return ALLOW
        return _deny("path_not_allowed")
    return _deny("host_not_allowed")


def check_request(ceiling: Ceiling, method: str, url: str, *, from_login_page: bool,
                  main_frame_navigation: bool) -> Verdict:
    """The network rule for one request the browser is about to send."""
    method = (method or "").upper()
    parsed = _url_parts(url)
    if parsed is None:
        return _deny("bad_url")
    scheme, host, path, port, userinfo = parsed
    if scheme in _LOCAL_SCHEMES:
        return ALLOW
    if scheme != "https":
        return _deny("scheme_not_allowed")
    if userinfo:
        return _deny("credentials_in_url")
    if port not in (None, 443):
        return _deny("port_not_allowed")

    if from_login_page:
        if main_frame_navigation:
            nav = check_login_navigation(ceiling, url)
            if not nav:
                return nav
            if method == "POST":
                if host_in(host, ceiling.login_hosts) and _path_ok(path, ceiling.login_paths):
                    return ALLOW
                return _deny("method_not_allowed")
            return ALLOW if method in SAFE_METHODS else _deny("method_not_allowed")
        if host_in(host, ceiling.login_challenge_hosts):
            return ALLOW if method in SAFE_METHODS + ("POST",) else _deny("method_not_allowed")
        if host_in(host, ceiling.login_hosts):
            if method in SAFE_METHODS:
                return ALLOW
            if method == "POST" and _path_ok(path, ceiling.login_paths):
                return ALLOW
            return _deny("method_not_allowed")
        if host_in(host, ceiling.nav_hosts) or host_in(host, ceiling.resource_hosts):
            return ALLOW if method in SAFE_METHODS else _deny("method_not_allowed")
        return _deny("host_not_allowed")

    if method not in SAFE_METHODS:
        return _deny("method_not_allowed")
    if main_frame_navigation:
        return check_navigation(ceiling, url)
    if not (host_in(host, ceiling.nav_hosts) or host_in(host, ceiling.resource_hosts)):
        return _deny("host_not_allowed")
    if token_query_keys(url):
        return _deny("token_query")
    return ALLOW


# Guard: the policy wired into one BrowserContext ---------------------------------------


StepSink = Callable[..., Any]   # StepLog.log signature: (kind, summary, **fields)


@dataclass
class BlockedRecord:
    method: str
    host: str
    path: str
    reason: str
    count: int = 1


@dataclass
class NavViolation:
    url: str
    reason: str
    page: str   # "model" | "login"


@dataclass
class Guard:
    ceiling: Ceiling
    log_step: StepSink | None = None
    max_navigations: int = MAX_NAVIGATIONS_PER_JOB
    login_page: Any = None
    model_pages: set[int] = field(default_factory=set)       # id() of pages this code opened
    blocked: dict[tuple[str, str], BlockedRecord] = field(default_factory=dict)
    violations: list[NavViolation] = field(default_factory=list)
    nav_count: int = 0
    popups_closed: int = 0
    websockets_closed: int = 0
    _creating: int = 0
    _closing: set[int] = field(default_factory=set)
    _tasks: set[asyncio.Task] = field(default_factory=set)
    _doc_loads: set[int] = field(default_factory=set)   # id() of pages with a document load in flight

    # Wiring ------------------------------------------------------------------------

    async def install(self, context: Any) -> None:
        await context.route("**/*", self._on_route)
        await context.route_web_socket(re.compile(r".*"), self._on_websocket)
        context.on("page", self._on_page)

    async def new_page(self, context: Any, *, login: bool = False) -> Any:
        """Open a page this code owns. Pages that appear any other way
        (window.open, target=_blank) are closed as popups."""
        self._creating += 1
        try:
            page = await context.new_page()
        finally:
            self._creating -= 1
        self._adopt(page, login=login)
        return page

    def _adopt(self, page: Any, *, login: bool) -> None:
        if login:
            self.login_page = page
        else:
            self.model_pages.add(id(page))
        page.on("request", lambda req, p=page: self._on_request(p, req))
        page.on("requestfailed", lambda req, p=page: self._on_request_failed(p, req))
        page.on("framenavigated", lambda frame, p=page: self._on_navigated(p, frame))
        page.on("popup", self._on_popup)
        page.on("dialog", self._on_dialog)

    def release_login_page(self) -> None:
        self.login_page = None

    def is_login_page(self, page: Any) -> bool:
        return page is not None and page is self.login_page

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _step(self, kind: str, summary: str, **fields: Any) -> None:
        if self.log_step is None:
            return
        try:
            self.log_step(kind, summary, **fields)
        except Exception as exc:   # logging must never break browsing
            log.warning("guard step %s not logged: %s", kind, exc)

    # Network -----------------------------------------------------------------------

    def _record_block(self, method: str, url: str, reason: str) -> None:
        parsed = _url_parts(url)
        host = parsed[1] if parsed else "?"
        path = parsed[2] if parsed else "?"
        key = (host, method)
        rec = self.blocked.get(key)
        if rec is not None:
            rec.count += 1
            return
        self.blocked[key] = BlockedRecord(method, host, path, reason)
        # One ledger row per host and method per job: method, host, path, no query.
        self._step("http.blocked", f"Blocked {method} {host}{path} ({reason})", ok=False,
                   result={"method": method, "host": host, "path": path, "reason": reason})

    def _request_page(self, request: Any) -> Any:
        try:
            frame = request.frame
        except Exception:
            return None   # service worker or detached: no page
        try:
            return frame.page
        except Exception:
            return None

    def _is_main_frame_navigation(self, request: Any) -> bool:
        try:
            return bool(request.is_navigation_request()) and request.frame.parent_frame is None
        except Exception:
            return False

    async def _on_route(self, route: Any) -> None:
        request = route.request
        page = self._request_page(request)
        verdict = check_request(self.ceiling, request.method, request.url,
                                from_login_page=page is not None and page is self.login_page,
                                main_frame_navigation=self._is_main_frame_navigation(request))
        if verdict:
            try:
                await route.continue_()
            except Exception as exc:   # page closed mid-flight
                log.debug("route continue failed: %s", exc)
            return
        self._record_block(request.method.upper(), request.url, verdict.reason or "blocked")
        try:
            await route.abort("blockedbyclient")
        except Exception as exc:
            log.debug("route abort failed: %s", exc)

    async def _on_websocket(self, ws: Any) -> None:
        self.websockets_closed += 1
        self._record_block("WEBSOCKET", ws.url.replace("wss://", "https://").replace("ws://", "http://"),
                           "websocket")
        try:
            await ws.close()
        except Exception as exc:
            log.debug("websocket close failed: %s", exc)

    # Pages -------------------------------------------------------------------------

    def _on_page(self, page: Any) -> None:
        if self._creating > 0:
            return   # one of ours; adopted by new_page()
        self._close_stray(page, "page")

    def _on_popup(self, page: Any) -> None:
        self._close_stray(page, "popup")

    def _close_stray(self, page: Any, how: str) -> None:
        if id(page) in self.model_pages or page is self.login_page or id(page) in self._closing:
            return
        self._closing.add(id(page))   # the context `page` and the opener's `popup` both fire
        self.popups_closed += 1
        self._step("browser.popup_closed", f"Closed a {how} the page opened", ok=False)
        self._spawn(self._close_quietly(page))

    @staticmethod
    async def _close_quietly(page: Any) -> None:
        try:
            await page.close()
        except Exception as exc:
            log.debug("closing stray page failed: %s", exc)

    def _on_dialog(self, dialog: Any) -> None:
        self._step("browser.dialog_dismissed", f"Dismissed a {getattr(dialog, 'type', 'dialog')} dialog")
        self._spawn(self._dismiss(dialog))

    @staticmethod
    async def _dismiss(dialog: Any) -> None:
        try:
            await dialog.dismiss()
        except Exception as exc:
            log.debug("dialog dismiss failed: %s", exc)

    # Navigation re-check -----------------------------------------------------------

    @staticmethod
    def _is_main_document(page: Any, req: Any) -> bool:
        try:
            return bool(req.is_navigation_request()) and req.frame == page.main_frame
        except Exception:
            return False

    def _on_request(self, page: Any, req: Any) -> None:
        # A real document load issues a navigation request before it commits;
        # a same-document change (pushState, replaceState, hash) does not.
        if self._is_main_document(page, req):
            self._doc_loads.add(id(page))

    def _on_request_failed(self, page: Any, req: Any) -> None:
        if self._is_main_document(page, req):
            self._doc_loads.discard(id(page))

    def _on_navigated(self, page: Any, frame: Any) -> None:
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
        except Exception:
            return
        document_load = id(page) in self._doc_loads
        self._doc_loads.discard(id(page))
        if page is self.login_page:
            verdict = check_login_navigation(self.ceiling, url)
            if not verdict:
                self.violations.append(NavViolation(url, verdict.reason or "blocked", "login"))
            return
        if url == "about:blank":
            return
        if document_load:
            # Only real page loads count toward the cap and get the full check.
            self.nav_count += 1
            verdict = check_navigation(self.ceiling, url)
        else:
            verdict = check_same_document_navigation(self.ceiling, url)
        if not verdict:
            # A redirect (or a click) took the model page off the ceiling: the
            # one GET already happened; nothing more is read from it.
            self.violations.append(NavViolation(url, verdict.reason or "blocked", "model"))
            self._record_block("NAVIGATE", url, verdict.reason or "blocked")
            self._spawn(self._blank(page))

    @staticmethod
    async def _blank(page: Any) -> None:
        try:
            await page.goto("about:blank")
        except Exception as exc:
            log.debug("about:blank after violation failed: %s", exc)

    def take_violations(self, page_kind: str = "model") -> list[NavViolation]:
        out = [v for v in self.violations if v.page == page_kind]
        self.violations = [v for v in self.violations if v.page != page_kind]
        return out

    @property
    def navigations_left(self) -> int:
        return max(0, self.max_navigations - self.nav_count)

    async def settle(self) -> None:
        """Let queued about:blank / close tasks finish (called after each tool)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
