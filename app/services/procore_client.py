"""Procore bid sheet client for the RFP harvest (docs/RFP_HARVEST.md, section 3).

Plain HTTP against the endpoints the Procore bid sheet page itself calls
(captured live 2026-09-14). No browser: the JSON endpoints answer to the
session cookies, the login is a Rails form, and every bidding document is a
signed storage URL that downloads with no cookies at all.

Three rules this module enforces on its own, so no caller can break them:

- Allowlist. A request leaves this process only for a path in ALLOWED_PATHS
  (five GET endpoints) or the login flow (two POSTs, a handful of GETs on the
  two Procore hosts). The bid intent ("Will Bid" / "Will Not Bid"), submit,
  NDA sign, "Email documents" and upload routes are unreachable from code.
- Pace. Every request waits its turn behind `_pace`: at least
  `procore_min_request_interval_seconds` times a random 1.0 to 2.0 since the
  previous Procore request in this process. Logins, JSON calls and downloads
  alike.
- Login discipline. A login is attempted only after a request proved the
  session gone, never twice within `procore_login_min_interval_seconds`,
  never while the store says logins are locked. The password appears in the
  login POST body and nowhere else: not in logs, not in errors, not in rows.

The session store (cookies, failure counter, lock) is injected as a small
protocol so this module has no Supabase import and the tests drive it with
`httpx.MockTransport` and an in-memory store.
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

APP_HOST = "app.procore.com"
LOGIN_HOST = "login.procore.com"
STORAGE_HOST = "storage.procore.com"
APP_BASE = f"https://{APP_HOST}"
LOGIN_BASE = f"https://{LOGIN_HOST}"

PROVIDER = "procore"

# A real browser's request headers. The login page is behind Cloudflare's
# passive JS detection; the JSON endpoints look at Accept.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_HTML_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
_JSON_ACCEPT = "application/json, text/plain, */*"
_BASE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

_ID = r"\d{1,12}"
_ID_RE = re.compile(rf"^{_ID}$")

# The five GET endpoints the harvest may call (path only, no query).
ALLOWED_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^/{_ID}/company/planroom/route_to_bid_sheet/{_ID}$"),
    re.compile(rf"^/rest/v1\.0/companies/{_ID}/bid_packages/{_ID}$"),
    re.compile(rf"^/rest/v1\.0/companies/{_ID}/bids/{_ID}$"),
    re.compile(rf"^/rest/v1\.0/companies/{_ID}/planroom/bid_packages/{_ID}/documents$"),
    re.compile(rf"^/rest/v1\.0/companies/{_ID}/bid/{_ID}/bid_forms/{_ID}$"),
)
# Paths the login chain may pass through, per host. Anything else in a
# redirect ends the chain with ProcoreLoginFailed.
_LOGIN_APP_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/auth/procore$"),
    re.compile(r"^/auth/procore/callback$"),
    re.compile(r"^/account/select_company$"),
    re.compile(rf"^/{_ID}/company/planroom/route_to_bid_sheet/{_ID}$"),
    re.compile(rf"^/{_ID}/company/planroom/bid_packages/{_ID}/bids/{_ID}$"),
    re.compile(rf"^/webclients/host/companies/{_ID}/tools/planroom/bid-packages/{_ID}/bids/{_ID}$"),
)
_LOGIN_LOGIN_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/$"),
    re.compile(r"^/oauth/authorize$"),
    re.compile(r"^/login/password$"),
    re.compile(r"^/sessions/submit_login_email$"),
    re.compile(r"^/sessions/submit_login_password$"),
)
_MAX_HOPS = 8                # docs 3.2: at most 8 hops on the login chain
_MAX_HTML_BYTES = 2 * 1024 * 1024
_MAX_JSON_BYTES = 8 * 1024 * 1024
_DOWNLOAD_CHUNK = 1024 * 1024
_ERROR_MAX_CHARS = 300

_SESSION_COOKIE = "_session_id"           # app.procore.com, set by the OAuth callback


# ── Errors ───────────────────────────────────────────────────────────────────


class ProcoreError(RuntimeError):
    """Base: the message is app-authored and safe to store."""


class ProcoreTransient(ProcoreError):
    """Network trouble, a 5xx or a 429: worth retrying later."""


class ProcoreUnavailable(ProcoreError):
    """Procore cannot be used right now for a reason a retry does not fix on
    its own: no credentials, logins locked, an interstitial page."""

    def __init__(self, message: str, *, locked_until: datetime | None = None) -> None:
        super().__init__(message)
        self.locked_until = locked_until


class ProcoreLoginLocked(ProcoreUnavailable):
    """Consecutive login failures reached the cap; `locked_until` is set."""


class ProcoreLoginFailed(ProcoreError):
    """One login attempt failed; `step` names where."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


class ProcoreSessionExpired(ProcoreError):
    """Internal: a request proved the session gone. The caller logs in once
    and retries once."""


class ProcoreForbidden(ProcoreError):
    """403 or 404 on a bid: the account cannot see it. Permanent."""


# ── Reference parsing (pure) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ProcoreRef:
    company_id: str
    bid_id: str
    package_id: str | None = None
    project_id: str | None = None

    @property
    def bid_sheet_url(self) -> str:
        return f"{APP_BASE}/{self.company_id}/company/planroom/route_to_bid_sheet/{self.bid_id}"

    @property
    def external_url(self) -> str:
        """What rfp_harvests.external_url stores; the one name every
        platform reference answers to (PipelineSuiteRef has it too)."""
        return self.bid_sheet_url

    @property
    def external_key(self) -> str:
        return f"{PROVIDER}:{self.company_id}:{self.bid_id}"


_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)
_ROUTE_RE = re.compile(rf"^/({_ID})/company/planroom/route_to_bid_sheet/({_ID})$")
_ZIP_RE = re.compile(rf"^/({_ID})/company/planroom/download_zip$")
_PACKAGE_RE = re.compile(rf"^/({_ID})/company/planroom/bid_packages/({_ID})/bids/({_ID})$")
_INTENT_RE = re.compile(rf"^/({_ID})/project/public/bid/({_ID})/intents/")


def unwrap_link(url: str) -> str:
    """Outlook rewrites every link through `*.safelinks.protection.outlook.com/
    ?url=<encoded>`; give back the original. Harmless on any other link."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.netloc or "").lower()
    if host.endswith("safelinks.protection.outlook.com"):
        inner = parse_qs(parsed.query).get("url")
        return inner[0] if inner else url
    return url


def parse_reference(body_text: str | None) -> ProcoreRef | None:
    """The Procore bid this email is about, from the links in its text body.
    The bid sheet route is preferred; the zip link and the bid page are
    fallbacks; the bid intent links only ever contribute the ids (they are
    never fetched). Ids are digits, at most 12 characters. None when the
    body carries no usable Procore link."""
    if not body_text:
        return None
    company: str | None = None
    bid: str | None = None
    package: str | None = None
    project: str | None = None
    fallback: tuple[str, str] | None = None
    for raw in _URL_RE.findall(body_text):
        url = unwrap_link(html_lib.unescape(raw))
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if (parsed.netloc or "").lower() != APP_HOST:
            continue
        path = parsed.path or ""
        m = _ROUTE_RE.match(path)
        if m:
            if company is None or bid is None:
                company, bid = m.group(1), m.group(2)
            continue
        m = _PACKAGE_RE.match(path)
        if m:
            if company is None or bid is None:
                company, bid = m.group(1), m.group(3)
            package = package or m.group(2)
            continue
        m = _ZIP_RE.match(path)
        if m:
            qs = parse_qs(parsed.query)
            bid_id = (qs.get("bid_id") or [""])[0]
            if _ID_RE.match(bid_id) and fallback is None:
                fallback = (m.group(1), bid_id)
            continue
        m = _INTENT_RE.match(path)
        if m:
            # Names the project and the bid but not the company; only a route,
            # bid page or zip link can complete the reference.
            project = project or m.group(1)
            continue
    if company is None or bid is None:
        if fallback is None:
            return None
        company, bid = fallback
    return ProcoreRef(company_id=company, bid_id=bid, package_id=package, project_id=project)


# ── Session store protocol ───────────────────────────────────────────────────


class SessionStore(Protocol):
    """Where the one shared Procore session lives (rfp_harvest_sessions).
    `load` returns None when nothing is stored."""

    def load(self) -> dict | None: ...
    def save_cookies(self, account: str, cookies: list[dict]) -> None: ...
    def record_login(self, *, ok: bool, error: str | None) -> dict: ...
    def touch(self) -> None: ...


@dataclass
class MemorySessionStore:
    """In-memory store for tests and one-off scripts. Mirrors the columns of
    rfp_harvest_sessions, including the lock policy."""

    max_failures: int = 3
    lock_seconds: int = 21600
    state: dict = field(default_factory=dict)
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def load(self) -> dict | None:
        return dict(self.state) if self.state else None

    def save_cookies(self, account: str, cookies: list[dict]) -> None:
        self.state.update(
            account=account,
            cookies=cookies,
            logged_in_at=self.now().isoformat(),
            login_failures=0,
            locked_until=None,
            last_error=None,
        )

    def record_login(self, *, ok: bool, error: str | None) -> dict:
        now = self.now()
        self.state["last_login_attempt_at"] = now.isoformat()
        if ok:
            self.state["login_failures"] = 0
            self.state["locked_until"] = None
            self.state["last_error"] = None
        else:
            failures = int(self.state.get("login_failures") or 0) + 1
            self.state["login_failures"] = failures
            self.state["last_error"] = error
            if failures >= self.max_failures:
                self.state["locked_until"] = (
                    now + timedelta(seconds=self.lock_seconds)
                ).isoformat()
        return dict(self.state)

    def touch(self) -> None:
        self.state["last_used_at"] = self.now().isoformat()


# ── Pace ─────────────────────────────────────────────────────────────────────

_pace_lock = threading.Lock()
_last_request_at = 0.0


def _pace(min_interval: float, *, sleep: Callable[[float], None], clock: Callable[[], float],
          rng: Callable[[float, float], float]) -> None:
    """Wait until at least `min_interval * U(1, 2)` seconds have passed since
    the previous Procore request in this process. The reservation is made
    under the lock so two threads cannot both leave at once."""
    global _last_request_at
    if min_interval <= 0:
        return
    with _pace_lock:
        gap = min_interval * rng(1.0, 2.0)
        now = clock()
        wait = _last_request_at + gap - now
        _last_request_at = max(now, _last_request_at + gap) if wait > 0 else now
    if wait > 0:
        sleep(wait)


# ── Small parsers ────────────────────────────────────────────────────────────

_FORM_RE = re.compile(r"<form\b[^>]*>.*?</form>", re.IGNORECASE | re.DOTALL)
_TAG_ATTR_RE = re.compile(r'([\w:\-\[\]]+)\s*=\s*"([^"]*)"')
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ACTION_RE = re.compile(r'<form\b[^>]*\baction="([^"]*)"', re.IGNORECASE)


def parse_login_form(page_html: str, action_hint: str) -> tuple[str, dict[str, str]] | None:
    """The form whose action contains `action_hint`, as (action, hidden
    fields). Hidden inputs are all carried forward so an added field does not
    break the flow; the visible ones are filled by the caller."""
    for form in _FORM_RE.findall(page_html):
        action_match = _ACTION_RE.search(form)
        action = html_lib.unescape(action_match.group(1)) if action_match else ""
        if action_hint not in action:
            continue
        fields: dict[str, str] = {}
        for tag in _INPUT_RE.findall(form):
            attrs = {k.lower(): html_lib.unescape(v) for k, v in _TAG_ATTR_RE.findall(tag)}
            name = attrs.get("name")
            if not name:
                continue
            if attrs.get("type", "text").lower() == "hidden":
                fields[name] = attrs.get("value", "")
        return action, fields
    return None


_ERROR_SNIPPET_RE = re.compile(
    r'<[^>]+class="[^"]*(?:error|alert|flash)[^"]*"[^>]*>(.*?)</', re.IGNORECASE | re.DOTALL
)


def page_error_text(page_html: str) -> str | None:
    """The first visible error/alert text on a login page, tags stripped and
    capped, for the failure sentence. None when there is none."""
    m = _ERROR_SNIPPET_RE.search(page_html)
    if not m:
        return None
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    text = " ".join(html_lib.unescape(text).split())
    return text[:_ERROR_MAX_CHARS] or None


def _looks_like_challenge(resp: httpx.Response) -> bool:
    """A Cloudflare interstitial in place of the page we asked for."""
    if resp.headers.get("cf-mitigated"):
        return True
    ctype = resp.headers.get("content-type", "")
    if "text/html" not in ctype:
        return False
    head = resp.text[:4000].lower()
    return "challenge-platform" in head and ("just a moment" in head or "cf-chl" in head)


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def _path(url: str) -> str:
    return urlparse(url).path or "/"


def _allowed(path: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.match(path) for p in patterns)


def assert_allowed_get(path: str) -> None:
    """Raise ValueError for a GET path outside ALLOWED_PATHS. A test failure
    at development time, never a runtime branch a caller could route around."""
    if not _allowed(path, ALLOWED_PATHS):
        raise ValueError(f"Procore path is not on the harvest allowlist: {path}")


# ── Session ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProcoreConfig:
    email: str
    password: str
    min_request_interval: float = 2.0
    login_min_interval: int = 600
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.email and self.password)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lock_state(state: dict | None, now: datetime | None = None) -> datetime | None:
    """When the store says logins are locked, the instant the lock ends."""
    if not state:
        return None
    until = _parse_ts(state.get("locked_until"))
    if until and until > (now or _now()):
        return until
    return None


class ProcoreSession:
    """One logged-in Procore session over a single httpx client.

    `transport`, `sleep`, `clock` and `rng` are test seams. `renew` is an
    optional callback run before each paced request (the queue lease)."""

    provider = PROVIDER          # the sandbox source kind the harvest records

    def __init__(
        self,
        config: ProcoreConfig,
        store: SessionStore,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = _now,
        on_lock: Callable[[datetime, str], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._now = now
        self._on_lock = on_lock
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(config.timeout),
            follow_redirects=False,
            headers=_BASE_HEADERS,
        )
        self._loaded = False
        self._continue_url: str | None = None
        self._jar_loaded_at: datetime | None = None
        self._last_login_attempt: datetime | None = None
        self.logged_in_this_session = False

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ProcoreSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- cookies --------------------------------------------------------------

    def _load_store(self) -> dict | None:
        state = self.store.load()
        self._loaded = True
        self._last_login_attempt = _parse_ts((state or {}).get("last_login_attempt_at"))
        if not state:
            return None
        if state.get("account") and state["account"] != self.config.email:
            # A rotated account: the stored jar belongs to someone else.
            return state
        self._jar_loaded_at = _parse_ts(state.get("logged_in_at"))
        now = self._now()
        for c in state.get("cookies") or []:
            exp = c.get("expires")
            if exp and isinstance(exp, (int, float)) and exp > 0 and exp < now.timestamp():
                continue
            try:
                self._client.cookies.set(
                    c["name"], c["value"], domain=c.get("domain") or APP_HOST, path=c.get("path") or "/"
                )
            except (KeyError, TypeError, ValueError):
                continue
        return state

    def _export_cookies(self) -> list[dict]:
        out: list[dict] = []
        for cookie in self._client.cookies.jar:
            domain = (cookie.domain or "").lower()
            if not domain.endswith("procore.com"):
                continue
            out.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                    "expires": cookie.expires,
                    "secure": bool(cookie.secure),
                }
            )
        return out

    def _has_app_session(self) -> bool:
        for cookie in self._client.cookies.jar:
            if cookie.name == _SESSION_COOKIE and (cookie.domain or "").lower().lstrip(".") == APP_HOST:
                return True
        return False

    # -- requests -------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = _host(url)
        if host not in (APP_HOST, LOGIN_HOST):
            raise ValueError(f"Procore request to an unexpected host: {host}")
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise ProcoreTransient(f"Procore did not answer ({type(exc).__name__}).") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProcoreTransient(f"Procore answered {resp.status_code}; the harvest will be retried.")
        return resp

    def _json_headers(self, referer: str | None) -> dict[str, str]:
        headers = {"Accept": _JSON_ACCEPT, "X-Requested-With": "XMLHttpRequest"}
        if referer:
            headers["Referer"] = referer
        return headers

    def get_json(self, path: str, *, params: dict | None = None, referer: str | None = None) -> Any:
        """GET an allowlisted app.procore.com JSON endpoint with the session.
        Logs in once (when allowed) and retries once if the session is gone."""
        assert_allowed_get(path)
        if not self._loaded:
            self._load_store()
        try:
            return self._get_json_once(path, params=params, referer=referer)
        except ProcoreSessionExpired:
            self.ensure_session()
            return self._get_json_once(path, params=params, referer=referer)

    def _get_json_once(self, path: str, *, params: dict | None, referer: str | None) -> Any:
        resp = self._request("GET", f"{APP_BASE}{path}", params=params, headers=self._json_headers(referer))
        if resp.status_code == 401:
            raise ProcoreSessionExpired()
        if resp.status_code in (301, 302, 303, 307, 308):
            target = urljoin(str(resp.url), resp.headers.get("location", ""))
            if _host(target) == LOGIN_HOST or _path(target).startswith("/auth/procore"):
                raise ProcoreSessionExpired()
            raise ProcoreForbidden("Procore redirected the bid request somewhere unexpected.")
        if resp.status_code in (403, 404):
            raise ProcoreForbidden(
                "Procore refused the bid (403/404): the account cannot see it, or it was removed."
            )
        if resp.status_code != 200:
            raise ProcoreTransient(f"Procore answered {resp.status_code} on a bid request.")
        if _looks_like_challenge(resp):
            raise ProcoreUnavailable("Procore answered with a verification page instead of data.")
        if "json" not in resp.headers.get("content-type", ""):
            raise ProcoreUnavailable("Procore answered with a page instead of data.")
        if len(resp.content) > _MAX_JSON_BYTES:
            raise ProcoreForbidden("Procore answered with more data than the harvest accepts.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProcoreUnavailable("Procore answered with unreadable data.") from exc
        self.store.touch()
        return data

    def resolve_bid_sheet(self, ref: ProcoreRef) -> str:
        """The bid package id behind the bid sheet route (its Location header
        names it). Logs in once and retries once if the session is gone."""
        if not self._loaded:
            self._load_store()
        self._continue_url = ref.bid_sheet_url
        try:
            return self._resolve_once(ref)
        except ProcoreSessionExpired:
            self.ensure_session()
            return self._resolve_once(ref)

    def _resolve_once(self, ref: ProcoreRef) -> str:
        path = f"/{ref.company_id}/company/planroom/route_to_bid_sheet/{ref.bid_id}"
        assert_allowed_get(path)
        resp = self._request("GET", f"{APP_BASE}{path}", headers={"Accept": _HTML_ACCEPT})
        if resp.status_code == 401:
            raise ProcoreSessionExpired()
        if resp.status_code in (301, 302, 303, 307, 308):
            target = urljoin(str(resp.url), resp.headers.get("location", ""))
            if _host(target) == LOGIN_HOST or _path(target).startswith("/auth/procore"):
                raise ProcoreSessionExpired()
            m = _PACKAGE_RE.match(_path(target))
            if m and _host(target) == APP_HOST and m.group(3) == ref.bid_id:
                return m.group(2)
            raise ProcoreForbidden("Procore did not route the bid sheet to a bid package.")
        if resp.status_code in (403, 404):
            raise ProcoreForbidden(
                "Procore refused the bid sheet (403/404): the account cannot see it, or it was removed."
            )
        if _looks_like_challenge(resp):
            raise ProcoreUnavailable("Procore answered with a verification page instead of the bid sheet.")
        raise ProcoreForbidden(f"Procore answered {resp.status_code} for the bid sheet route.")

    # -- login ----------------------------------------------------------------

    def availability(self) -> tuple[bool, str | None, datetime | None]:
        """(usable, reason, locked_until) without touching Procore."""
        if not self.config.configured:
            return False, "Procore credentials are not configured.", None
        state = self.store.load()
        until = lock_state(state, self._now())
        if until:
            return False, "Procore logins are locked after repeated failures.", until
        return True, None, None

    def ensure_session(self) -> None:
        """Log in, subject to the discipline in the module docstring. Called
        only after a request proved the session gone."""
        if not self.config.configured:
            raise ProcoreUnavailable("Procore credentials are not configured.")
        state = self.store.load()
        now = self._now()
        until = lock_state(state, now)
        if until:
            raise ProcoreLoginLocked(
                "Procore logins are locked after repeated failures.", locked_until=until
            )
        # A jar another worker saved since we loaded ours is worth one try
        # before spending a login.
        if state and state.get("cookies") and state.get("account") == self.config.email:
            if not self.logged_in_this_session and self._jar_is_newer(state):
                self._client.cookies.clear()
                self._load_store()
                if self._has_app_session():
                    return
        last = _parse_ts((state or {}).get("last_login_attempt_at")) or self._last_login_attempt
        if last and (now - last).total_seconds() < self.config.login_min_interval:
            wait_until = last + timedelta(seconds=self.config.login_min_interval)
            raise ProcoreUnavailable(
                "Procore login was attempted recently; waiting before trying again.",
                locked_until=wait_until,
            )
        try:
            self._login()
        except ProcoreLoginFailed as exc:
            state = self.store.record_login(ok=False, error=f"{exc.step}: {exc}")
            until = lock_state(state, self._now())
            logger.warning("Procore login failed at %s: %s", exc.step, exc)
            if until:
                if self._on_lock:
                    try:
                        self._on_lock(until, str(exc))
                    except Exception:  # noqa: BLE001 - the bell must never mask the lock
                        logger.exception("Procore lock notification failed")
                raise ProcoreLoginLocked(
                    "Procore logins are locked after repeated failures.", locked_until=until
                ) from exc
            raise ProcoreUnavailable(
                "Procore login failed; it will be retried later.",
                locked_until=self._now() + timedelta(seconds=self.config.login_min_interval),
            ) from exc
        self.store.record_login(ok=True, error=None)
        self.store.save_cookies(self.config.email, self._export_cookies())
        self.logged_in_this_session = True

    def _jar_is_newer(self, state: dict) -> bool:
        """Another worker logged in after we loaded our jar."""
        saved_at = _parse_ts(state.get("logged_in_at"))
        return bool(saved_at and (self._jar_loaded_at is None or saved_at > self._jar_loaded_at))

    def _follow(self, resp: httpx.Response, *, accept: str) -> httpx.Response:
        """Follow redirects by hand, same-site only, on the login chain."""
        hops = 0
        while resp.status_code in (301, 302, 303, 307, 308):
            hops += 1
            if hops > _MAX_HOPS:
                raise ProcoreLoginFailed("redirect", "The Procore login redirected too many times.")
            target = urljoin(str(resp.url), resp.headers.get("location", ""))
            host, path = _host(target), _path(target)
            if host == APP_HOST:
                if not _allowed(path, _LOGIN_APP_PATHS):
                    raise ProcoreLoginFailed("redirect", f"The Procore login left the expected flow ({path}).")
            elif host == LOGIN_HOST:
                if not _allowed(path, _LOGIN_LOGIN_PATHS):
                    raise ProcoreLoginFailed("redirect", f"The Procore login left the expected flow ({path}).")
            else:
                raise ProcoreLoginFailed("redirect", "The Procore login redirected off Procore.")
            if host == APP_HOST and (
                _PACKAGE_RE.match(path) or _path(str(resp.url)) == "/auth/procore/callback"
            ):
                # The OAuth callback has set the app session and is handing
                # the browser on (to the bid sheet route, or to the account's
                # company chooser when no continue URL was planted). Nothing
                # past this point is needed, so it is not requested.
                return resp
            resp = self._request("GET", target, headers={"Accept": accept})
        return resp

    def _login(self) -> None:
        self._last_login_attempt = self._now()
        self._client.cookies.clear()
        # 1. The bid sheet route bounces through /auth/procore to the login
        #    page. Starting at the route (when one is known) plants Procore's
        #    continue-URL cookie, so the OAuth callback lands back on the
        #    route instead of the account's company chooser.
        resp = self._request(
            "GET",
            self._continue_url or f"{APP_BASE}/auth/procore",
            headers={"Accept": _HTML_ACCEPT},
        )
        resp = self._follow(resp, accept=_HTML_ACCEPT)
        if _looks_like_challenge(resp):
            raise ProcoreLoginFailed("challenge", "Procore showed a verification page at login.")
        if resp.status_code != 200 or _host(str(resp.url)) != LOGIN_HOST:
            raise ProcoreLoginFailed("login_page", "The Procore login page did not load.")
        page = resp.text[:_MAX_HTML_BYTES]
        form = parse_login_form(page, "submit_login_email")
        if form is None:
            raise ProcoreLoginFailed("login_page", "The Procore login page had no email form.")
        action, hidden = form
        email_url = urljoin(str(resp.url), action)
        if _host(email_url) != LOGIN_HOST:
            raise ProcoreLoginFailed("login_page", "The Procore email form posts off Procore.")
        # 2. Email.
        resp = self._request(
            "POST",
            email_url,
            data={**hidden, "session[email]": self.config.email, "session[remember_me]": "true"},
            headers={
                "Accept": _HTML_ACCEPT,
                "Origin": LOGIN_BASE,
                "Referer": str(resp.url),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code not in (301, 302, 303, 307, 308):
            raise ProcoreLoginFailed(
                "email", page_error_text(resp.text[:_MAX_HTML_BYTES]) or "Procore did not accept the email."
            )
        target = urljoin(str(resp.url), resp.headers.get("location", ""))
        if _host(target) != LOGIN_HOST or not _path(target).startswith("/login/password"):
            raise ProcoreLoginFailed("email", "Procore did not ask for a password after the email.")
        resp = self._request("GET", target, headers={"Accept": _HTML_ACCEPT, "Referer": LOGIN_BASE + "/"})
        resp = self._follow(resp, accept=_HTML_ACCEPT)
        if resp.status_code != 200:
            raise ProcoreLoginFailed("password_page", "The Procore password page did not load.")
        page = resp.text[:_MAX_HTML_BYTES]
        form = parse_login_form(page, "submit_login_password")
        if form is None:
            raise ProcoreLoginFailed("password_page", "The Procore password page had no password form.")
        action, hidden = form
        password_url = urljoin(str(resp.url), action)
        if _host(password_url) != LOGIN_HOST:
            raise ProcoreLoginFailed("password_page", "The Procore password form posts off Procore.")
        # 3. Password.
        resp = self._request(
            "POST",
            password_url,
            data={**hidden, "session[password]": self.config.password},
            headers={
                "Accept": _HTML_ACCEPT,
                "Origin": LOGIN_BASE,
                "Referer": str(resp.url),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code not in (301, 302, 303, 307, 308):
            raise ProcoreLoginFailed(
                "password",
                page_error_text(resp.text[:_MAX_HTML_BYTES]) or "Procore did not accept the password.",
            )
        target = urljoin(str(resp.url), resp.headers.get("location", ""))
        if _host(target) == LOGIN_HOST and _path(target).startswith("/login/password"):
            raise ProcoreLoginFailed("password", "Procore rejected the password.")
        if _host(target) == LOGIN_HOST and _path(target) == "/":
            raise ProcoreLoginFailed("password", "Procore sent the login back to the start.")
        # 4. OAuth callback into the app: follow until the app session exists.
        resp = self._follow(resp, accept=_HTML_ACCEPT)
        if not self._has_app_session():
            raise ProcoreLoginFailed("callback", "Procore did not establish an app session.")
        logger.info("Procore login succeeded for the harvest account")

    # -- downloads ------------------------------------------------------------

    def _make_download_client(self) -> httpx.Client:
        if self._download_client_factory is not None:
            return self._download_client_factory()
        return httpx.Client(
            timeout=httpx.Timeout(connect=10, read=120, write=60, pool=30),
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        )

    def download(self, url: str, dest: Path, *, max_bytes: int) -> int:
        """Stream a signed storage.procore.com URL into `dest` (created
        O_EXCL), following exactly one redirect and only to amazonaws.com,
        under a running byte cap. No cookies are sent (a fresh client with
        no jar). Returns bytes written; raises ProcoreForbidden for a bad
        host or an oversize body (the file is unlinked), ProcoreTransient
        for network trouble or a 5xx."""
        if not url.startswith("https://") or _host(url) != STORAGE_HOST:
            raise ProcoreForbidden("A bid document link pointed outside Procore storage.")
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(dest, flags, 0o644)
        written = 0
        try:
            with os.fdopen(fd, "wb") as fh, self._make_download_client() as client:
                target = url
                for hop in range(2):
                    with client.stream("GET", target) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            if hop == 1:
                                raise ProcoreForbidden("A bid document link redirected twice.")
                            target = urljoin(target, resp.headers.get("location", ""))
                            if not target.startswith("https://") or not _host(target).endswith(
                                ".amazonaws.com"
                            ):
                                raise ProcoreForbidden(
                                    "A bid document link redirected off Procore storage."
                                )
                            continue
                        if resp.status_code in (403, 404):
                            raise ProcoreForbidden("Procore storage refused the document link.")
                        if resp.status_code == 429 or resp.status_code >= 500:
                            raise ProcoreTransient(f"Procore storage answered {resp.status_code}.")
                        if resp.status_code != 200:
                            raise ProcoreTransient(f"Procore storage answered {resp.status_code}.")
                        for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                            written += len(chunk)
                            if written > max_bytes:
                                raise ProcoreForbidden(
                                    "The bid document is larger than the harvest accepts."
                                )
                            fh.write(chunk)
                        return written
                raise ProcoreForbidden("A bid document link redirected twice.")
        except httpx.TransportError as exc:
            _unlink_quietly(dest)
            raise ProcoreTransient(
                f"Procore storage did not answer ({type(exc).__name__})."
            ) from exc
        except BaseException:
            _unlink_quietly(dest)
            raise

    # Test seam: a factory for the cookie-less download client.
    _download_client_factory: Callable[[], httpx.Client] | None = None


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
