"""PipelineSuite (PreconSuite) portal client for the RFP harvest
(docs/RFP_PIPELINESUITE.md, sections 2 and 3).

A GC on PipelineSuite runs its own plan room at `<gc>.pipelinesuite.com`.
The invitation email carries the portal host, a Project ID and a
company-wide Security Key; the harvest logs in with those (a ColdFusion form
POST, no CSRF token), reads the server-rendered project page and downloads
every file from `opr.pipelinesuite.com`, which needs no cookies at all.
Captured live 2026-09-16 on the CG&B and SHF portals.

Three rules this module enforces on its own, so no caller can break them:

- Allowlist. A request leaves this process only for the five shapes in
  `assert_allowed` (section 3.4): the login page, the login POST, the
  project page, a file on the file host and the email's own tracker (the
  open pixel and a click link, never followed). The confirmation form
  (`confirmResponse`, the bid question), the email buttons' auto-login
  route (`login/enc/...`), `submitRFI`, `uploadBid`, `dspUpdateInfo`,
  `logout`, the zip routes, the All Projects list and the PipelineBid links
  are unreachable from code. A violation is a ValueError: a test failure,
  never a runtime branch.
- Pace. Every request waits its turn behind `_pace`: at least
  `pipelinesuite_min_request_interval_seconds` times a random 1.0 to 2.0
  since the previous PipelineSuite request in this process. Logins, the
  page, downloads and pings alike.
- Login discipline. A login is attempted only after a request proved the
  session gone, never twice within `pipelinesuite_login_min_interval_seconds`
  for the same portal, never while the store says the portal's logins are
  locked. The Security Key appears in the login POST body and nowhere
  else: not in logs, not in errors, not in the stored row (the row's
  `account` is a fingerprint of the key), not in a `repr`.

The session store (cookies, failure counter, lock) is the same protocol
Procore's client takes (`procore_client.SessionStore`), injected, so this
module has no Supabase import and the tests drive it with
`httpx.MockTransport` and an in-memory store. Page parsing is pure and
runs on the standard library's html.parser (no third-party HTML parser is a
declared dependency of this project).
"""

from __future__ import annotations

import base64
import hashlib
import html as html_lib
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from app.services.procore_client import (
    MemorySessionStore,
    SessionStore,
    lock_state,
    parse_login_form,
    unwrap_link,
)

__all__ = [
    "MemorySessionStore",
    "SessionStore",
    "PipelineSuiteConfig",
    "PipelineSuiteError",
    "PipelineSuiteForbidden",
    "PipelineSuiteLoginFailed",
    "PipelineSuiteLoginLocked",
    "PipelineSuiteRef",
    "PipelineSuiteSession",
    "PipelineSuiteSessionExpired",
    "PipelineSuiteTransient",
    "PipelineSuiteUnavailable",
    "ProjectPage",
    "Tracking",
    "assert_allowed",
    "bid_due_at",
    "classify_name",
    "key_fingerprint",
    "lock_state",
    "parse_click_link",
    "parse_reference",
    "parse_project_page",
    "parse_tracking",
    "portal_label",
    "unwrap_link",
]

logger = logging.getLogger(__name__)

PROVIDER = "pipelinesuite"                     # the sandbox source kind
SESSION_PROVIDER_PREFIX = "pipelinesuite:"     # rfp_harvest_sessions.provider = prefix + host

PORTAL_SUFFIX = ".pipelinesuite.com"
FILE_HOST = "opr.pipelinesuite.com"
TRACK_HOST = "go.pipelinesuite.com"
_RESERVED_LABELS = frozenset({"go", "opr", "cdn", "www"})
_LABEL_RE = re.compile(r"^[a-z0-9-]{1,63}$")

KIND_DRAWING = "drawing"
KIND_SPECIFICATION = "specification"
KIND_OTHER = "other"

LOCKED_MESSAGE = "PipelineSuite logins for {host} are locked after repeated failures."
_MSG_PAGE = "The portal answered with a page the harvest does not understand."
_MSG_BAD_KEY = "The portal rejected the Project ID and Security Key."

# A real browser's request headers.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_HTML_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
_BASE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

_ID = r"\d{1,12}"
_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_HTML_BYTES = 2 * 1024 * 1024
_DOWNLOAD_CHUNK = 1024 * 1024
_PING_TIMEOUT = 10.0
_MAX_FILES = 2000

# Allowlist shapes (section 3.4).
_NEXT_PATH_RE = re.compile(r"^/general/index/next/[A-Za-z0-9_-]+$")
_LOGIN_POST_PATH = "/ehPipelineSubs/login/"
_PROJECT_PATH_RE = re.compile(rf"^/ehPipelineSubs/dspProject/projectID/{_ID}$")
_FILE_PATH_RE = re.compile(rf"^/{_ID}/{_ID}/[^/]+$")
_TRACK_PATHS = ("/wf/open", "/ls/click")

# The page's data-file-path, host included.
_DATA_FILE_PATH_RE = re.compile(rf"^{re.escape(FILE_HOST)}/({_ID})/({_ID})/([^/]+)$")

_PACIFIC = ZoneInfo("America/Los_Angeles")


# ── Errors ───────────────────────────────────────────────────────────────────


class PipelineSuiteError(RuntimeError):
    """Base: the message is app-authored and safe to store."""


class PipelineSuiteTransient(PipelineSuiteError):
    """Network trouble, a 5xx or a 429: worth retrying later."""


class PipelineSuiteUnavailable(PipelineSuiteError):
    """The portal cannot be used right now for a reason a retry does not fix
    on its own: logins locked, a login just attempted, an unexpected page."""

    def __init__(self, message: str, *, locked_until: datetime | None = None) -> None:
        super().__init__(message)
        self.locked_until = locked_until


class PipelineSuiteLoginLocked(PipelineSuiteUnavailable):
    """Consecutive login failures reached the cap; `locked_until` is set."""


class PipelineSuiteLoginFailed(PipelineSuiteError):
    """One login attempt failed; `step` names where."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


class PipelineSuiteSessionExpired(PipelineSuiteError):
    """Internal: a request proved the session gone. The caller logs in once
    and retries once."""


class PipelineSuiteForbidden(PipelineSuiteError):
    """The portal has no such project for the key, a file is missing, or a
    file is over the cap. Permanent."""


# ── Reference parsing (pure, section 2) ──────────────────────────────────────


def portal_label(host: str) -> str | None:
    """The GC's label for a `<label>.pipelinesuite.com` host, or None for any
    other host and for the platform's own hosts (go, opr, cdn, www)."""
    host = (host or "").strip().lower()
    if not host.endswith(PORTAL_SUFFIX):
        return None
    label = host[: -len(PORTAL_SUFFIX)]
    if not _LABEL_RE.match(label) or label in _RESERVED_LABELS:
        return None
    return label


def key_fingerprint(security_key: str) -> str:
    """What the session store keeps as `account`: never the key itself."""
    return hashlib.sha256(security_key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PipelineSuiteRef:
    """The portal, project and key an invitation names. `repr` and `str`
    never include the key."""

    host: str
    project_id: str
    security_key: str = field(repr=False)

    def __str__(self) -> str:
        return repr(self)

    @property
    def label(self) -> str:
        return portal_label(self.host) or self.host.split(".")[0]

    @property
    def external_key(self) -> str:
        return f"{PROVIDER}:{self.label}:{self.project_id}"

    @property
    def session_provider(self) -> str:
        return f"{SESSION_PROVIDER_PREFIX}{self.host}"

    @property
    def origin(self) -> str:
        return f"https://{self.host}"

    @property
    def project_path(self) -> str:
        return f"/ehPipelineSubs/dspProject/projectID/{self.project_id}"

    @property
    def project_url(self) -> str:
        return f"{self.origin}{self.project_path}"

    @property
    def external_url(self) -> str:
        return self.project_url

    @property
    def next_token(self) -> str:
        raw = f"ehPipelineSubs/dspProject/projectID/{self.project_id}".encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @property
    def login_page_url(self) -> str:
        return f"{self.origin}/general/index/next/{self.next_token}"

    @property
    def login_post_url(self) -> str:
        return f"{self.origin}{_LOGIN_POST_PATH}"

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.security_key)


_HOST_RE = re.compile(
    r"https?://([a-z0-9-]+\.pipelinesuite\.com)(?![\w-])(?!\.[\w-])", re.IGNORECASE
)
_PROJECT_ID_RE = re.compile(r"Project ID:\s*(\d{1,12})(?!\d)", re.IGNORECASE)
_KEY_RE = re.compile(r"Security Key:\s*(\S{1,64})(?!\S)", re.IGNORECASE)


def parse_reference(body_text: str | None) -> PipelineSuiteRef | None:
    """The portal host, Project ID and Security Key from the email's text
    body, or None when any of the three is missing. The host is the first
    `https://<label>.pipelinesuite.com` whose label is a GC (the email prints
    it upper-case; the tracker and asset hosts are skipped)."""
    if not body_text:
        return None
    host: str | None = None
    for candidate in _HOST_RE.findall(body_text):
        if portal_label(candidate) is not None:
            host = candidate.lower()
            break
    if host is None:
        return None
    project = _PROJECT_ID_RE.search(body_text)
    key = _KEY_RE.search(body_text)
    if not project or not key:
        return None
    return PipelineSuiteRef(host=host, project_id=project.group(1), security_key=key.group(1))


# ── Tracking links (pure, section 2) ─────────────────────────────────────────


@dataclass(frozen=True)
class Tracking:
    open_url: str | None
    click_url: str | None


_RESPONSE_TEXTS = frozenset({"yes", "no", "unsure"})
_VIEW_FILES_TEXT = "view files"
_TEXT_CLICK_RE = re.compile(
    r"View Files and Project Details\s*<\s*(https?://[^\s<>]+)\s*>", re.IGNORECASE
)


def _tracker_path(url: str | None) -> str | None:
    """The tracker path (`/wf/open` or `/ls/click`) when the unwrapped URL is
    on go.pipelinesuite.com, else None."""
    if not url:
        return None
    target = unwrap_link(html_lib.unescape(url.strip()))
    try:
        parsed = urlparse(target)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if (parsed.netloc or "").lower() != TRACK_HOST:
        return None
    return parsed.path if parsed.path in _TRACK_PATHS else None


def _unwrapped(url: str) -> str:
    return unwrap_link(html_lib.unescape(url.strip()))


def parse_tracking(page_html: str | None) -> Tracking:
    """The email's open pixel and its "View Files and Project Details" click
    link. The pixel is the first `img` whose unwrapped src is
    `go.pipelinesuite.com/wf/open`; the click link is the first anchor whose
    visible text contains "view files" and whose unwrapped href (or Outlook's
    `originalsrc`) is `go.pipelinesuite.com/ls/click`. An anchor reading Yes,
    No or Unsure is never returned: those are the bid answer."""
    if not page_html:
        return Tracking(None, None)
    doc = _parse_html(page_html)
    open_url: str | None = None
    click_url: str | None = None
    for node in doc.iter():
        if node.tag == "img" and open_url is None:
            src = node.get("src")
            if src and _tracker_path(src) == "/wf/open":
                open_url = _unwrapped(src)
        elif node.tag == "a" and click_url is None:
            text = _text(node).lower()
            if not text or text in _RESPONSE_TEXTS:
                continue
            if _VIEW_FILES_TEXT not in text:
                continue
            for candidate in (node.get("href"), node.get("originalsrc")):
                if candidate and _tracker_path(candidate) == "/ls/click":
                    click_url = _unwrapped(candidate)
                    break
        if open_url is not None and click_url is not None:
            break
    return Tracking(open_url, click_url)


def parse_click_link(body_text: str | None) -> str | None:
    """The View Files click link from the plain text body (the
    `View Files and Project Details <url>` line), the fallback when the
    email HTML cannot be fetched. Never a Yes / No / Unsure link: those lines
    carry their own labels."""
    if not body_text:
        return None
    for raw in _TEXT_CLICK_RE.findall(body_text):
        if _tracker_path(raw) == "/ls/click":
            return _unwrapped(raw)
    return None


# ── Allowlist (section 3.4) ──────────────────────────────────────────────────

_KEY_IN_PATH_RE = re.compile(r"(securityKey/)[^/]+", re.IGNORECASE)


def assert_allowed(method: str, url: str) -> None:
    """Raise ValueError unless (method, url) is one of the five request
    shapes the harvest may send. Runs before every request; a violation is a
    test failure at development time, never a runtime branch."""
    verb = (method or "").upper()
    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    if parsed is not None and not parsed.fragment:
        scheme = (parsed.scheme or "").lower()
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        query = parsed.query or ""
        if host == TRACK_HOST:
            if verb == "GET" and scheme in ("http", "https") and path in _TRACK_PATHS and "upn=" in query:
                return
        elif host == FILE_HOST:
            if verb == "GET" and scheme == "https" and _FILE_PATH_RE.match(path) and not query and not parsed.fragment:
                return
        elif portal_label(host) is not None and scheme == "https":
            if verb == "GET" and _NEXT_PATH_RE.match(path) and not query:
                return
            if verb == "POST" and path == _LOGIN_POST_PATH and not query:
                return
            if verb == "GET" and _PROJECT_PATH_RE.match(path) and not query:
                return
    # Never echo a query string or a key-bearing path segment into the error.
    shown_host = (parsed.netloc or "?") if parsed is not None else "?"
    shown_path = _KEY_IN_PATH_RE.sub(r"\1***", (parsed.path or "/") if parsed is not None else "?")
    raise ValueError(
        f"PipelineSuite request is not on the harvest allowlist: {verb} {shown_host}{shown_path}"
    )


# ── Pace (section 3.3) ───────────────────────────────────────────────────────

_pace_lock = threading.Lock()
_last_request_at = 0.0


def _pace(min_interval: float, *, sleep: Callable[[float], None], clock: Callable[[], float],
          rng: Callable[[float, float], float]) -> None:
    """Wait until at least `min_interval * U(1, 2)` seconds have passed since
    the previous PipelineSuite request in this process. The reservation is
    made under the lock so two threads cannot both leave at once."""
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


# ── A small DOM over html.parser ─────────────────────────────────────────────

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
_BLOCK_TAGS = frozenset({
    "p", "div", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "section", "article", "header", "footer",
})
_CELL_TAGS = frozenset({"td", "th"})
_SKIP_TAGS = frozenset({"script", "style", "noscript"})


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "_Node | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Any] = []
        self.parent = parent

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.iter()

    def elements(self, tag: str | None = None):
        """Direct element children, optionally by tag."""
        for child in self.children:
            if isinstance(child, _Node) and (tag is None or child.tag == tag):
                yield child

    def find_all(self, tag: str | None = None, *, id: str | None = None,
                 cls: str | None = None) -> list["_Node"]:
        out: list[_Node] = []
        for node in self.iter():
            if node is self:
                continue
            if tag is not None and node.tag != tag:
                continue
            if id is not None and node.get("id") != id:
                continue
            if cls is not None and cls not in node.classes:
                continue
            out.append(node)
        return out

    def find(self, tag: str | None = None, *, id: str | None = None,
             cls: str | None = None) -> "_Node | None":
        for node in self.iter():
            if node is self:
                continue
            if tag is not None and node.tag != tag:
                continue
            if id is not None and node.get("id") != id:
                continue
            if cls is not None and cls not in node.classes:
                continue
            return node
        return None


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("[document]", {}, None)
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def _parse_html(page_html: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(page_html)
    builder.close()
    return builder.root


def _collect_text(node: _Node, out: list[str], skip: Callable[[_Node], bool] | None) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
            continue
        if child.tag in _SKIP_TAGS or (skip is not None and skip(child)):
            continue
        if child.tag == "br":
            out.append("\n")
            continue
        if child.tag in _BLOCK_TAGS:
            out.append("\n")
            _collect_text(child, out, skip)
            out.append("\n")
        elif child.tag in _CELL_TAGS:
            out.append(" ")
            _collect_text(child, out, skip)
            out.append(" ")
        else:
            _collect_text(child, out, skip)


_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _text(node: _Node | None, *, multiline: bool = False,
          skip: Callable[[_Node], bool] | None = None) -> str:
    """Visible text: tags dropped, entities already resolved by the parser,
    nbsp to space, whitespace collapsed (newlines kept, per line, when
    `multiline`)."""
    if node is None:
        return ""
    parts: list[str] = []
    _collect_text(node, parts, skip)
    text = "".join(parts).replace("\xa0", " ")
    if not multiline:
        return " ".join(text.split())
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return _MULTI_BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def _is_button_link(node: _Node) -> bool:
    """The "Map It" style button anchors inside info cells."""
    return node.tag == "a" and any(c.startswith("btn-flat") for c in node.classes)


# ── Page parsing (pure, section 3.5) ─────────────────────────────────────────


@dataclass
class ProjectPage:
    logged_in: bool
    gc_name: str | None
    title: str | None
    invited_name: str | None
    response_recorded: bool
    trades: list[dict]
    info: dict[str, str | None]
    notices: list[dict]
    contacts: list[dict]
    files: list[dict]


_INFO_KEYS = {
    "project #": "project_number",
    "project number": "project_number",
    "project name": "project_name",
    "location": "location",
    "address": "address",
    "city": "city",
    "state": "state",
    "zip": "zip",
    "bid date": "bid_date",
    "bid time": "bid_time",
    "scope": "scope",
    "plans": "plans",
    "other info": "other_info",
}
_MULTILINE_INFO = frozenset({"scope", "plans", "other_info"})
_CONTACT_KEYS = {
    "company": "company",
    "contact": "name",
    "name": "name",
    "title": "title",
    "phone": "phone",
    "extension": "extension",
    "fax": "fax",
    "email": "email",
}
_NOTICE_KEYS = {"title": "title", "created by": "created_by", "created on": "created_on"}
_TRAILING_EXT_RE = re.compile(r"\s+(\.[^.\s/]{1,16})$")


def _snake(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def is_project_page(page_html: str) -> bool:
    return 'id="projectInfo"' in page_html


def is_login_page(page_html: str) -> bool:
    return 'name="portalLogin"' in page_html or 'id="portalLogin"' in page_html


def _parse_trades(confirmation: _Node) -> list[dict]:
    trades: list[dict] = []
    for trade in confirmation.find_all("div", cls="trade"):
        name_node = trade.find(cls="tradeName")
        if name_node is None:
            continue
        strong = name_node.find("strong")
        code = _text(strong) or None
        full = _text(name_node)
        rest = full[len(code):].strip() if code and full.startswith(code) else full
        if code is None:
            m = re.match(r"^(\S+)\s*(.*)$", rest)
            if m:
                code, rest = m.group(1), m.group(2)
        inner = rest.strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1]
        parts = [p.strip() for p in inner.split(":") if p.strip()]
        name = parts[-1] if parts else (inner.strip() or None)
        trades.append({"code": code, "name": name, "text": full})
    return trades


def _parse_info(doc: _Node) -> dict[str, str | None]:
    info: dict[str, str | None] = {key: None for key in dict.fromkeys(_INFO_KEYS.values())}
    section = doc.find(id="projectInfo")
    if section is None:
        return info
    for row in section.find_all("tr"):
        th = next(row.elements("th"), None)
        td = next(row.elements("td"), None)
        if th is None or td is None:
            continue
        label = _text(th).lower()
        key = _INFO_KEYS.get(label) or _snake(label)
        if not key:
            continue
        value = _text(td, multiline=key in _MULTILINE_INFO, skip=_is_button_link)
        info[key] = value or None
    return info


def _table_rows(table: _Node, keys: dict[str, str]) -> list[dict]:
    """Rows of a `thead`/`tbody` table as dicts keyed by the header labels
    (through `keys`), in page order."""
    headers: list[str] = []
    rows: list[dict] = []
    for tr in table.find_all("tr"):
        ths = list(tr.elements("th"))
        tds = list(tr.elements("td"))
        if ths and not headers:
            headers = [keys.get(_text(th).lower(), _snake(_text(th))) for th in ths]
            continue
        if not tds or not headers:
            continue
        row = {key: None for key in headers}
        for key, td in zip(headers, tds):
            value = _text(td)
            row[key] = value or None
        rows.append(row)
    return rows


def _parse_notices(doc: _Node) -> list[dict]:
    table = doc.find("table", id="addendaTable")
    if table is None:
        section = doc.find(id="addenda")
        table = section.find("table") if section is not None else None
    if table is None:
        return []
    out: list[dict] = []
    for row in _table_rows(table, _NOTICE_KEYS):
        out.append({
            "title": row.get("title"),
            "created_by": row.get("created_by"),
            "created_on": row.get("created_on"),
        })
    return out


def _parse_contacts(doc: _Node) -> list[dict]:
    section = doc.find(id="projectContacts")
    table = section.find("table") if section is not None else None
    if table is None:
        return []
    out: list[dict] = []
    for row in _table_rows(table, _CONTACT_KEYS):
        email = row.get("email")
        out.append({
            "company": row.get("company"),
            "name": row.get("name"),
            "title": row.get("title"),
            "phone": row.get("phone"),
            "extension": row.get("extension"),
            "fax": row.get("fax"),
            "email": email.lower() if email else None,
        })
    return out


def squeeze_name(raw_name: str) -> str:
    """A file's basename with the trailing spaces before its extension
    squeezed (`IFB 112-27 .pdf` -> `IFB 112-27.pdf`)."""
    return _TRAILING_EXT_RE.sub(r"\1", raw_name.strip()).strip()


def _file_entry(li: _Node, folder: str) -> dict:
    path = (li.get("data-file-path") or "").strip()
    m = _DATA_FILE_PATH_RE.match(path)
    raw_name = m.group(3) if m else path.rsplit("/", 1)[-1]
    name = squeeze_name(raw_name) or (li.get("data-text") or "").strip() or "document"
    url = f"https://{FILE_HOST}/{m.group(1)}/{m.group(2)}/{quote(raw_name, safe='')}" if m else None
    size_text = (li.get("data-file-size") or "").strip().replace(",", "")
    size_kb = int(size_text) if size_text.isdigit() else None
    return {
        "file_id": (li.get("data-file-id") or "").strip() or None,
        "name": name,
        "file_path": path or None,
        "folder": folder,
        "size_kb": size_kb,
        "uploaded_on": (li.get("data-uploaded-on") or "").strip() or None,
        "url": url,
    }


def _walk_files(ul: _Node, folders: list[str], out: list[dict]) -> None:
    for li in ul.elements("li"):
        classes = li.classes
        if "folder" in classes:
            label = (li.get("data-text") or "").strip()
            if not label:
                label = _text(li.find(cls="folderName"))
            parts = folders + [label] if label else folders
            for sub in li.elements("ul"):
                _walk_files(sub, parts, out)
        elif "file" in classes:
            if len(out) >= _MAX_FILES:
                return
            out.append(_file_entry(li, "/".join(folders)))


def _parse_files(doc: _Node) -> list[dict]:
    tree = doc.find(cls="opr_files")
    if tree is None:
        return []
    out: list[dict] = []
    for ul in tree.elements("ul"):
        _walk_files(ul, [], out)
    return out


def parse_project_page(page_html: str) -> ProjectPage:
    """The project page as facts (section 3.5). Pure; never raises on a page
    of another shape (every field is None or empty then, `logged_in` says
    whether `#projectInfo` was there)."""
    doc = _parse_html(page_html[:_MAX_HTML_BYTES])
    header = doc.find(id="rightHeader")
    gc_name = _text(header.find("h2")) if header is not None and header.find("h2") else ""
    title = _text(doc.find("h1"))
    confirmation = doc.find(id="confirmation")
    invited_name = ""
    response_recorded = False
    trades: list[dict] = []
    if confirmation is not None:
        first_p = confirmation.find("p")
        invited_name = _text(first_p).rstrip(":").strip()
        response_recorded = any(
            (node.get("type") or "").lower() == "radio" and "checked" in node.attrs
            for node in confirmation.find_all("input")
        )
        trades = _parse_trades(confirmation)
    return ProjectPage(
        logged_in=doc.find(id="projectInfo") is not None,
        gc_name=gc_name or None,
        title=title or None,
        invited_name=invited_name or None,
        response_recorded=response_recorded,
        trades=trades,
        info=_parse_info(doc),
        notices=_parse_notices(doc),
        contacts=_parse_contacts(doc),
        files=_parse_files(doc),
    )


# ── Dates and kinds (pure) ───────────────────────────────────────────────────

_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")
_TIME_FORMATS = ("%I:%M %p", "%I:%M:%S %p", "%I %p", "%H:%M", "%H:%M:%S")


def bid_due_at(info: dict) -> str | None:
    """`bid_date` and `bid_time` combined in America/Los_Angeles as an ISO
    instant; the bare `YYYY-MM-DD` when the time is missing or unreadable;
    None when the date is."""
    date_text = " ".join(str(info.get("bid_date") or "").split())
    if not date_text:
        return None
    day = None
    for fmt in _DATE_FORMATS:
        try:
            day = datetime.strptime(date_text, fmt).date()
            break
        except ValueError:
            continue
    if day is None:
        return None
    time_text = " ".join(str(info.get("bid_time") or "").replace(".", "").upper().split())
    clock = None
    for fmt in _TIME_FORMATS:
        try:
            clock = datetime.strptime(time_text, fmt).time()
            break
        except ValueError:
            continue
    if clock is None:
        return day.isoformat()
    return datetime.combine(day, clock, tzinfo=_PACIFIC).isoformat()


_DRAWING_RE = re.compile(r"\b(dwg|drawings?|plans?|sheets?|bid set|compiled set)\b", re.IGNORECASE)
_SPEC_RE = re.compile(
    r"\b(spec|specs|specifications?|manual|addend\w*|amend\w*|itb|ifb|rfp|scope)\b", re.IGNORECASE
)
# A spreadsheet or a Word file is never a drawing ("Bid_Sheet_CSI_Divisions.xlsx").
_NEVER_DRAWING_EXTS = frozenset({".xls", ".xlsx", ".xlsm", ".csv", ".doc", ".docx", ".txt"})


def classify_name(name: str | None) -> str:
    """drawing / specification / other from the file name alone (the page
    carries no discipline). Underscores count as spaces: plan rooms often
    name files `NSU_Grey_Shell_SPECS_-_Project_Manual.pdf`, and `\\b` does not
    break on `_`."""
    text = (name or "").replace("_", " ").strip()
    if not text:
        return KIND_OTHER
    ext = os.path.splitext(text)[1].lower()
    if ext == ".dwg" or (ext not in _NEVER_DRAWING_EXTS and _DRAWING_RE.search(text)):
        return KIND_DRAWING
    if _SPEC_RE.search(text):
        return KIND_SPECIFICATION
    return KIND_OTHER


# ── Session (section 3.2) ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PipelineSuiteConfig:
    account: str                         # the key fingerprint, never the key
    min_request_interval: float = 2.0
    login_min_interval: int = 600
    timeout: float = 30.0


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


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def _path(url: str) -> str:
    return urlparse(url).path or "/"


class PipelineSuiteSession:
    """One logged-in session on one portal over a single httpx client.

    `transport`, `sleep`, `clock`, `rng` and `now` are test seams. `on_lock`
    is called once as `(until, error, host)` when the portal's logins lock."""

    provider = PROVIDER

    def __init__(
        self,
        config: PipelineSuiteConfig,
        store: SessionStore,
        ref: PipelineSuiteRef,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = _now,
        on_lock: Callable[[datetime, str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.ref = ref
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
        self._jar_loaded_at: datetime | None = None
        self._last_login_attempt: datetime | None = None
        self.logged_in_this_session = False

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PipelineSuiteSession":
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
        if state.get("account") and state["account"] != self.config.account:
            # The key rotated: the stored jar belongs to the old key.
            return state
        self._jar_loaded_at = _parse_ts(state.get("logged_in_at"))
        now = self._now()
        for c in state.get("cookies") or []:
            exp = c.get("expires")
            if exp and isinstance(exp, (int, float)) and exp > 0 and exp < now.timestamp():
                continue
            try:
                self._client.cookies.set(
                    c["name"], c["value"], domain=c.get("domain") or self.ref.host,
                    path=c.get("path") or "/",
                )
            except (KeyError, TypeError, ValueError):
                continue
        return state

    def _export_cookies(self) -> list[dict]:
        out: list[dict] = []
        for cookie in self._client.cookies.jar:
            domain = (cookie.domain or "").lower().lstrip(".")
            if not domain.endswith("pipelinesuite.com"):
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

    def _has_portal_cookies(self) -> bool:
        for cookie in self._client.cookies.jar:
            if (cookie.domain or "").lower().lstrip(".") == self.ref.host:
                return True
        return False

    # -- requests -------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert_allowed(method, url)
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise PipelineSuiteTransient(
                f"The portal did not answer ({type(exc).__name__})."
            ) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PipelineSuiteTransient(
                f"The portal answered {resp.status_code}; the harvest will be retried."
            )
        return resp

    def get_project_page(self) -> str:
        """The project page HTML with the session. Logs in once (when
        allowed) and retries once if the session is gone."""
        if not self._loaded:
            self._load_store()
        try:
            return self._get_page_once()
        except PipelineSuiteSessionExpired:
            self.ensure_session()
            return self._get_page_once()

    def _get_page_once(self) -> str:
        resp = self._request("GET", self.ref.project_url, headers={"Accept": _HTML_ACCEPT})
        if resp.status_code in _REDIRECTS:
            target = urljoin(str(resp.url), resp.headers.get("location", ""))
            if _path(target).startswith("/general/index"):
                raise PipelineSuiteSessionExpired()
            raise PipelineSuiteUnavailable(_MSG_PAGE)
        if resp.status_code in (403, 404):
            raise PipelineSuiteForbidden(
                f"The portal has no project {self.ref.project_id} for this Security Key."
            )
        if resp.status_code != 200:
            raise PipelineSuiteUnavailable(
                f"The portal answered {resp.status_code} for the project page."
            )
        page = resp.text[:_MAX_HTML_BYTES]
        if is_project_page(page):
            self.store.touch()
            return page
        if is_login_page(page):
            raise PipelineSuiteSessionExpired()
        raise PipelineSuiteUnavailable(_MSG_PAGE)

    # -- login ----------------------------------------------------------------

    def availability(self) -> tuple[bool, str | None, datetime | None]:
        """(usable, reason, locked_until) without touching the portal."""
        state = self.store.load()
        until = lock_state(state, self._now())
        if until:
            return False, LOCKED_MESSAGE.format(host=self.ref.host), until
        return True, None, None

    def ensure_session(self) -> None:
        """Log in, subject to the discipline in the module docstring. Called
        only after a request proved the session gone."""
        state = self.store.load()
        now = self._now()
        until = lock_state(state, now)
        if until:
            raise PipelineSuiteLoginLocked(
                LOCKED_MESSAGE.format(host=self.ref.host), locked_until=until
            )
        # A jar another worker saved since we loaded ours is worth one try
        # before spending a login.
        if state and state.get("cookies") and state.get("account") == self.config.account:
            if not self.logged_in_this_session and self._jar_is_newer(state):
                self._client.cookies.clear()
                self._load_store()
                if self._has_portal_cookies():
                    return
        last = _parse_ts((state or {}).get("last_login_attempt_at")) or self._last_login_attempt
        if last and (now - last).total_seconds() < self.config.login_min_interval:
            wait_until = last + timedelta(seconds=self.config.login_min_interval)
            raise PipelineSuiteUnavailable(
                f"PipelineSuite login for {self.ref.host} was attempted recently; "
                "waiting before trying again.",
                locked_until=wait_until,
            )
        try:
            self._login()
        except PipelineSuiteLoginFailed as exc:
            state = self.store.record_login(ok=False, error=f"{exc.step}: {exc}")
            until = lock_state(state, self._now())
            logger.warning(
                "PipelineSuite login (%s) failed at %s: %s", self.ref.host, exc.step, exc
            )
            if until:
                if self._on_lock:
                    try:
                        self._on_lock(until, str(exc), self.ref.host)
                    except Exception:  # noqa: BLE001 - the bell must never mask the lock
                        logger.exception("PipelineSuite lock notification failed")
                raise PipelineSuiteLoginLocked(
                    LOCKED_MESSAGE.format(host=self.ref.host), locked_until=until
                ) from exc
            raise PipelineSuiteUnavailable(
                f"PipelineSuite login for {self.ref.host} failed; it will be retried later.",
                locked_until=self._now() + timedelta(seconds=self.config.login_min_interval),
            ) from exc
        self.store.record_login(ok=True, error=None)
        self.store.save_cookies(self.config.account, self._export_cookies())
        self.logged_in_this_session = True

    def _jar_is_newer(self, state: dict) -> bool:
        saved_at = _parse_ts(state.get("logged_in_at"))
        return bool(saved_at and (self._jar_loaded_at is None or saved_at > self._jar_loaded_at))

    def _login(self) -> None:
        self._last_login_attempt = self._now()
        self._client.cookies.clear()
        # 1. The login page plants the ColdFusion cookies and carries the form.
        resp = self._request("GET", self.ref.login_page_url, headers={"Accept": _HTML_ACCEPT})
        if resp.status_code != 200:
            raise PipelineSuiteLoginFailed(
                "login_page", f"The portal login page did not load (HTTP {resp.status_code})."
            )
        page = resp.text[:_MAX_HTML_BYTES]
        form = parse_login_form(page, "/ehPipelineSubs/login")
        if form is None:
            raise PipelineSuiteLoginFailed(
                "login_page", "The portal answered with an unexpected page instead of the login form."
            )
        _, hidden = form
        # 2. The POST: every hidden field carried forward, `next` ours (the
        #    page's own points at the email buttons' confirmResponse landing).
        resp = self._request(
            "POST",
            self.ref.login_post_url,
            data={
                **hidden,
                "next": self.ref.next_token,
                "portalProjectID": self.ref.project_id,
                "portalSecurityKey": self.ref.security_key,
            },
            headers={
                "Accept": _HTML_ACCEPT,
                "Origin": self.ref.origin,
                "Referer": self.ref.login_page_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code not in _REDIRECTS:
            raise PipelineSuiteLoginFailed(
                "login", f"The portal did not answer the login with a redirect (HTTP {resp.status_code})."
            )
        target = urljoin(str(resp.url), resp.headers.get("location", ""))
        host, path = _host(target), _path(target)
        if host not in ("", self.ref.host):
            raise PipelineSuiteLoginFailed("redirect", "The portal login redirected off the portal.")
        if path in (self.ref.project_path, self.ref.project_path + "/"):
            logger.info("PipelineSuite login succeeded on %s", self.ref.host)
            return
        if path.startswith("/general/index"):
            raise PipelineSuiteLoginFailed("key", _MSG_BAD_KEY)
        raise PipelineSuiteLoginFailed(
            "redirect", f"The portal login redirected somewhere unexpected ({path})."
        )

    # -- downloads and pings (cookie-less) ------------------------------------

    def _make_download_client(self, *, timeout: httpx.Timeout | None = None) -> httpx.Client:
        if self._download_client_factory is not None:
            return self._download_client_factory()
        return httpx.Client(
            timeout=timeout or httpx.Timeout(connect=10, read=120, write=60, pool=30),
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        )

    def download(self, url: str, dest: Path, *, max_bytes: int) -> int:
        """Stream one file from opr.pipelinesuite.com into `dest` (created
        O_EXCL) with a fresh cookie-less client, no redirects, under a
        running byte cap. Returns bytes written. PipelineSuiteForbidden for a
        404, a page where a file was expected or an oversize body (the file
        is unlinked); PipelineSuiteTransient for network trouble or a 5xx."""
        assert_allowed("GET", url)
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(dest, flags, 0o644)
        written = 0
        try:
            with os.fdopen(fd, "wb") as fh, self._make_download_client() as client:
                headers = {"Referer": self.ref.project_url}
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in _REDIRECTS:
                        raise PipelineSuiteForbidden(
                            "A project file link redirected; the harvest does not follow it."
                        )
                    if resp.status_code == 404:
                        raise PipelineSuiteForbidden("The file host has no such file (404).")
                    if resp.status_code == 403:
                        raise PipelineSuiteForbidden("The file host refused the file (403).")
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise PipelineSuiteTransient(f"The file host answered {resp.status_code}.")
                    if resp.status_code != 200:
                        raise PipelineSuiteTransient(f"The file host answered {resp.status_code}.")
                    if "text/html" in (resp.headers.get("content-type") or "").lower():
                        raise PipelineSuiteForbidden(
                            "The file host answered with a page instead of the file."
                        )
                    for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            raise PipelineSuiteForbidden(
                                "The project file is larger than the harvest accepts."
                            )
                        fh.write(chunk)
                    return written
        except httpx.TransportError as exc:
            _unlink_quietly(dest)
            raise PipelineSuiteTransient(
                f"The file host did not answer ({type(exc).__name__})."
            ) from exc
        except BaseException:
            _unlink_quietly(dest)
            raise

    def ping(self, url: str) -> int | None:
        """One GET on the email's own tracker (the open pixel or the View
        Files click link), no redirects followed, 10 s timeout, cookie-less.
        Returns the status code, or None when the tracker did not answer.
        Anything but go.pipelinesuite.com/wf/open or /ls/click is a
        ValueError."""
        target = _unwrapped(url)
        if _tracker_path(target) is None:
            raise ValueError("Only the PipelineSuite open pixel and click links can be pinged.")
        assert_allowed("GET", target)
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        try:
            with self._make_download_client(timeout=httpx.Timeout(_PING_TIMEOUT)) as client:
                resp = client.get(target)
                return resp.status_code
        except httpx.TransportError as exc:
            logger.info("PipelineSuite tracker did not answer (%s)", type(exc).__name__)
            return None

    # Test seam: a factory for the cookie-less download/ping client.
    _download_client_factory: Callable[[], httpx.Client] | None = None


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
