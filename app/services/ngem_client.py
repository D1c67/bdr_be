"""NGEM supplier portal client for the RFP portal ingestion (docs/RFP_NGEM_PORTAL.md,
section 3).

NGEM (the Nevada Government eMarketplace, an Ionwave / Euna supplier portal at
`supplier.ionwave.net`) is a classic ASP.NET WebForms site. Plain HTTP against
the pages a person opens (captured live 2026-09-15): the login form, the "My
Invitations" grid of the Available Bids tab with its pager postback, one bid's
Event Details and Attachments pages, and the per-file `Extract.aspx` download.
No browser and no JavaScript: the pager is replayed as the full postback the
page would send, with every input of the form as served.

Three rules this module enforces on its own, so no caller can break them:

- Allowlist. A request leaves this process only for a path in
  ALLOWED_GET_PATHS (six pages) or ALLOWED_POST_PATHS (the login and the two
  grid pagers), always on `supplier.ionwave.net` over https. A postback only
  ever targets ALLOWED_POSTBACK_TARGETS (the two grids' pagers) and never
  puts text in a filter field. The response, question, submission, line item
  and profile pages, the "Download All" zip postback and the "Other Bid
  Opportunities" grid are unreachable from code: any attempt raises
  ValueError before a request is built.
- Pace. Every request waits its turn behind `_pace`: at least
  `min_request_interval` times a random 1.0 to 2.0 since the previous portal
  request in this process. Logins, pages, postbacks and downloads alike.
- Login discipline. A login is attempted only after a request proved the
  session gone, never twice within `login_min_interval`, never while the
  store says logins are locked. The password appears in the login POST body
  and nowhere else: not in logs, not in errors, not in rows.

The session store (cookies, failure counter, lock) is injected as a small
protocol so this module has no Supabase import and the tests drive it with
`httpx.MockTransport`, an in-memory store and the captured pages in
`tests/fixtures_ngem/`.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
import nh3
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

HOST = "supplier.ionwave.net"
BASE = f"https://{HOST}"
PROVIDER = "ngem"

ENTRY_PATH = "/VendorResponse/ResponseList.aspx"
LOGIN_PATH = "/Login.aspx"
VENDOR_LOGIN_PATH = "/VendorLogin.aspx"
EVENT_PATH = "/VendorResponse/Bid/VResponseEvent.aspx"
ATTACHMENTS_PATH = "/VendorResponse/Bid/VResponseBidAttachments.aspx"
EXTRACT_PATH = "/Extract.aspx"
ERROR_PATH = "/Error.aspx"
BAD_REQUEST_PATH = "/BadRequest.aspx"

# The pages the client may GET (path only, exact, no query), the pages it may
# POST to (the login form and the two grid pagers) and the postback targets
# it may name (the two grids' pager controls). Anything else is a ValueError
# before the request is built: a test failure, never a runtime branch.
ALLOWED_GET_PATHS: frozenset[str] = frozenset(
    {ENTRY_PATH, LOGIN_PATH, VENDOR_LOGIN_PATH, EVENT_PATH, ATTACHMENTS_PATH, EXTRACT_PATH}
)
ALLOWED_POST_PATHS: frozenset[str] = frozenset({VENDOR_LOGIN_PATH, ENTRY_PATH, ATTACHMENTS_PATH})
INVITED_PAGER_TARGET_RE = re.compile(
    r"^ctl00\$mainContent\$ucInvitedListGrid\$rgResponse\$ctl00\$ctl03\$ctl01\$ctl\d{2}$"
)
ATTACHMENTS_PAGER_TARGET_RE = re.compile(
    r"^ctl00\$mainContent\$rgBidAttachments\$ctl00\$ctl03\$ctl01\$ctl\d{2}$"
)
ALLOWED_POSTBACK_TARGETS: tuple[re.Pattern[str], ...] = (
    INVITED_PAGER_TARGET_RE,
    ATTACHMENTS_PAGER_TARGET_RE,
)

INVITED_GRID_ID = "ctl00_mainContent_ucInvitedListGrid_rgResponse_ctl00"
ATTACHMENTS_GRID_ID = "ctl00_mainContent_rgBidAttachments_ctl00"
_LOGIN_PATHS = frozenset({LOGIN_PATH, VENDOR_LOGIN_PATH})
_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_HOPS = 6                       # docs 3.1: at most 6 hops, followed by hand
_MAX_HTML_BYTES = 4 * 1024 * 1024
_DOWNLOAD_CHUNK = 1024 * 1024
_ERROR_MAX_CHARS = 300
_NOTES_TEXT_MAX = 20_000
_NOTES_HTML_MAX = 40_000
_SHORT_MAX = 200
_TITLE_MAX = 500
_FILENAME_MAX = 200
_SESSION_COOKIE = "procData"        # set by a successful login POST
_DEFAULT_AGREE_TEXT = "I agree to the terms and conditions of using this website"
_PACIFIC = ZoneInfo("America/Los_Angeles")
_ZONES = {
    "PT": _PACIFIC,
    "PST": _PACIFIC,
    "PDT": _PACIFIC,
    "MT": ZoneInfo("America/Denver"),
    "MST": ZoneInfo("America/Denver"),
    "MDT": ZoneInfo("America/Denver"),
    "CT": ZoneInfo("America/Chicago"),
    "CST": ZoneInfo("America/Chicago"),
    "CDT": ZoneInfo("America/Chicago"),
    "ET": ZoneInfo("America/New_York"),
    "EST": ZoneInfo("America/New_York"),
    "EDT": ZoneInfo("America/New_York"),
}
_NOTES_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li", "a"}
_NOTES_ATTRIBUTES = {"a": {"href"}}
_NOTES_SCHEMES = {"https"}          # docs 3.3: a[href https only], the same allowlist the service keeps
_MSG_ELSEWHERE = "The portal sent the client somewhere it will not go."

STALE_LINK = "stale_link"           # NgemForbidden.reason for a BadRequest.aspx answer

# A real browser's request headers, as captured.
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


# ── Errors ───────────────────────────────────────────────────────────────────


class NgemError(RuntimeError):
    """Base: the message is app-authored (or portal text scrubbed and capped)
    and safe to store."""


class NgemTransient(NgemError):
    """Network trouble, a 5xx, a 429 or the portal's own error page: worth
    retrying later."""


class NgemUnavailable(NgemError):
    """The portal cannot be used right now for a reason a retry does not fix
    on its own: no credentials, logins locked, a login that just failed."""

    def __init__(self, message: str, *, locked_until: datetime | None = None) -> None:
        super().__init__(message)
        self.locked_until = locked_until


class NgemLoginLocked(NgemUnavailable):
    """Consecutive login failures reached the cap; `locked_until` is set."""


class NgemLoginFailed(NgemError):
    """One login attempt failed. Raised inside the login, recorded by
    `ensure_session`, surfaced as the cause of NgemUnavailable / NgemLoginLocked."""


class NgemSessionExpired(NgemError):
    """Internal: a request proved the session gone (a redirect to the login
    page, or the login form where a page was expected). The caller logs in
    once and retries once; reaching a service means the session stayed gone
    after that one login (transient)."""

    def __init__(self, message: str = "The NGEM session was gone after the one login.") -> None:
        super().__init__(message)


class NgemForbidden(NgemError):
    """Permanent: the portal refused the page or the link is not one the
    client may follow. `reason` is `STALE_LINK` for a BadRequest.aspx answer
    (the session-bound token is no longer valid)."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class NgemParseError(NgemError):
    """Permanent: a page whose shape is not the one captured (no invitations
    grid, a grid without its columns, an event page without its labels)."""


# ── Records ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InvitationRow:
    agency: str
    bid_number_raw: str
    bid_number: str
    addendum_no: int | None
    title: str
    issued_on: date | None
    close_at: datetime | None
    time_left: str | None
    bid_status: str | None
    response_status: str | None
    response_code: str | None
    status_code: str | None
    view_url: str | None


@dataclass(frozen=True)
class EventFacts:
    bid_number_raw: str
    title: str
    bid_type: str | None
    status: str | None
    issued_at: datetime | None
    close_at: datetime | None
    question_cutoff_at: datetime | None
    notes_html: str | None
    notes_text: str | None
    contact: dict
    attachments_url: str | None
    event_url: str | None
    bid_number: str = ""
    addendum_no: int | None = None


@dataclass(frozen=True)
class AttachmentRow:
    index: int
    file_name: str
    size_bytes: int | None
    description: str | None
    download_url: str | None


@dataclass(frozen=True)
class DownloadResult:
    bytes_written: int
    filename: str | None


@dataclass(frozen=True)
class InvitationsPage:
    rows: list[InvitationRow]
    total_items: int
    pages_fetched: int
    total_pages: int = 1


@dataclass(frozen=True)
class ParsedGrid:
    rows: list[InvitationRow]
    total_items: int
    total_pages: int
    pager_targets: dict[int, str]
    form_fields: dict[str, str]
    form_action: str
    current_page: int = 1


@dataclass(frozen=True)
class ParsedAttachments:
    rows: list[AttachmentRow]
    total_pages: int
    pager_targets: dict[int, str]
    form_fields: dict[str, str]
    form_action: str
    total_items: int = 0
    current_page: int = 1


# ── Small parsers (pure) ─────────────────────────────────────────────────────

_ADDENDUM_RE = re.compile(r"^(?P<number>.*?)\s+Addendum\s+(?P<no>\d+)\s*\.?\s*$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\s*$")
_DATETIME_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)?"
    r"\s*(?:\(\s*([A-Za-z]{2,3})\s*\))?\s*$",
    re.IGNORECASE,
)
_SIZE_RE = re.compile(
    r"^\s*\(?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)\s*(bytes?|b|kb|mb|gb)\s*\)?\s*$", re.IGNORECASE
)
_SIZE_UNITS = {"b": 1, "byte": 1, "bytes": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}
_FILE_CELL_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<size>[^()]*)\)\s*$", re.DOTALL)
_PAGER_INFO_RE = re.compile(r"(\d[\d,]*)\s+items?\s+in\s+(\d[\d,]*)\s+pages?", re.IGNORECASE)
_POSTBACK_HREF_RE = re.compile(r"__doPostBack\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")
_PAGE_TITLE_RE = re.compile(r"Go to Page\s+(\d+)", re.IGNORECASE)
_FILTER_FIELD_RE = re.compile(r"\$FilterTextBox_")
_ZIP_FIELD_RE = re.compile(r"hfDocZipAction$")
_HEADER_LINE_RE = re.compile(r"^(?P<number>.*?)\s*\((?P<title>.*)\)\s*$", re.DOTALL)
_MFA_CALL_RE = re.compile(r"(?<![\w$.])(tryMfa|OpenMfaPopup)\s*\(\s*\)")
_MFA_VISIBLE_RE = re.compile(r'"name":"rwMfaPrompt"[^\n]*"visibleOnPageLoad":true', re.IGNORECASE)
_CHECKBOX_TEXT_RE = re.compile(r'"text":"((?:[^"\\]|\\.)*)"')
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _squash(text: str | None) -> str:
    return " ".join((text or "").split())


def _cap(text: str | None, limit: int) -> str | None:
    value = _squash(text)
    return value[:limit] or None


def parse_bid_number(raw: str | None) -> tuple[str, int | None]:
    """`"607992-26 Addendum 2" -> ("607992-26", 2)`, `"1790" -> ("1790",
    None)`, `"IFB 112-27 Fire Station 95 Interior Renovation Addendum 1" ->
    ("IFB 112-27 Fire Station 95 Interior Renovation", 1)`. Case-insensitive,
    double spaces collapsed, a trailing period dropped."""
    text = _squash(raw)
    if text.endswith("."):
        text = text[:-1].rstrip()
    m = _ADDENDUM_RE.match(text)
    if m:
        return m.group("number").strip(), int(m.group("no"))
    return text, None


def _year(value: str) -> int:
    year = int(value)
    return year + 2000 if year < 100 else year


def parse_date(text: str | None) -> date | None:
    """`"8/25/2026"` -> date(2026, 8, 25); None on junk, never raises."""
    m = _DATE_RE.match(text or "")
    if not m:
        return None
    try:
        return date(_year(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def parse_pt_datetime(text: str | None) -> datetime | None:
    """`"9/17/2026 02:00 PM (PT)"` and `"8/17/2026 08:00:01 AM (PT)"` ->
    aware America/Los_Angeles datetimes. A bare time (no meridian) is read
    as 24-hour; a missing zone is Pacific; another US zone abbreviation
    (MT, CT, ET) is honored. None on junk, never raises."""
    m = _DATETIME_RE.match(text or "")
    if not m:
        return None
    month, day, year_text, hour_text, minute_text, second_text, meridian, zone = m.groups()
    hour = int(hour_text)
    if meridian:
        if hour < 1 or hour > 12:
            return None
        hour = hour % 12 + (12 if meridian.upper() == "PM" else 0)
    tz = _PACIFIC if not zone else _ZONES.get(zone.upper())
    if tz is None:
        return None
    try:
        return datetime(
            _year(year_text), int(month), int(day), hour, int(minute_text),
            int(second_text or 0), tzinfo=tz,
        )
    except ValueError:
        return None


def parse_size(text: str | None) -> int | None:
    """`"(286 KB)"` -> 292864, `"(1.60 MB)"` -> 1677722 (KB = 1024, MB =
    1024^2, GB = 1024^3, rounded to whole bytes). None on junk."""
    m = _SIZE_RE.match(text or "")
    if not m:
        return None
    number, unit = m.group(1).replace(",", ""), m.group(2).lower()
    factor = _SIZE_UNITS.get(unit)
    if factor is None:
        return None
    try:
        return int(round(float(number) * factor))
    except (ValueError, OverflowError):
        return None


def split_file_cell(text: str | None) -> tuple[str, int | None]:
    """`"Name.pdf (286 KB)"` -> ("Name.pdf", 292864). A trailing group that
    is not a size stays part of the name."""
    value = _squash(text)
    m = _FILE_CELL_RE.match(value)
    if m:
        size = parse_size(m.group("size"))
        if size is not None:
            return m.group("name").strip(), size
    return value, None


def _load_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text(node: Tag | None) -> str:
    return _squash(node.get_text(" ", strip=True)) if node is not None else ""


def _direct_text(node: Tag) -> str:
    """The element's own text nodes, not its children's (the bid header line
    without its status pill)."""
    return _squash("".join(str(c) for c in node.children if isinstance(c, str)))


def _form_fields(form: Tag, *, include_buttons: frozenset[str] = frozenset()) -> dict[str, str]:
    """Every field a browser would submit for `form`, in document order:
    hidden and text inputs with their served value (empty when none),
    checkboxes and radios only when checked, textareas and selects; no
    submit, button, image, reset or file inputs unless named in
    `include_buttons`. Duplicate names keep the last value."""
    fields: dict[str, str] = {}
    for el in form.find_all(["input", "textarea", "select"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "textarea":
            fields[name] = el.get_text()
            continue
        if el.name == "select":
            chosen = el.find("option", selected=True) or el.find("option")
            if chosen is not None:
                fields[name] = chosen.get("value", chosen.get_text(strip=True))
            continue
        kind = (el.get("type") or "text").lower()
        if kind in ("submit", "button", "image", "reset", "file"):
            if name in include_buttons:
                fields[name] = el.get("value") or ""
            continue
        if kind in ("checkbox", "radio") and not el.has_attr("checked"):
            continue
        fields[name] = el.get("value") or ""
    return fields


def parse_login_form(html: str) -> tuple[str, dict[str, str]] | None:
    """The login form (`id="Form1"`, action `./VendorLogin.aspx`) as (action,
    fields): every non-button input as served plus `btnLogin`, in document
    order. The caller fills the user name, the password and the checkbox
    client state. None when the page carries no login form."""
    soup = _load_html(html)
    form = soup.find("form", id="Form1")
    if form is None:
        form = next(
            (f for f in soup.find_all("form") if f.find("input", attrs={"name": "txtPassword"})),
            None,
        )
    if form is None or form.find("input", attrs={"name": "txtPassword"}) is None:
        return None
    action = html_lib.unescape(form.get("action") or "")
    fields = _form_fields(form, include_buttons=frozenset({"btnLogin"}))
    fields.setdefault("btnLogin", "Login")
    return action, fields


def checkbox_text(html: str) -> str:
    """The `chkAgree` RadCheckBox text from the page's Telerik initializer,
    falling back to the captured sentence."""
    for line in html.splitlines():
        if "Telerik.Web.UI.RadCheckBox" not in line or "chkAgree" not in line:
            continue
        m = _CHECKBOX_TEXT_RE.search(line)
        if m:
            try:
                value = json.loads(f'"{m.group(1)}"')
            except ValueError:
                continue
            if isinstance(value, str) and value.strip():
                return value
    return _DEFAULT_AGREE_TEXT


def agree_client_state(text: str) -> str:
    """The full Telerik client state of the (hidden, unchecked) checkbox,
    serialized without spaces exactly as the browser posts it."""
    state = {
        "text": text,
        "value": "",
        "enabled": True,
        "autoPostBack": True,
        "commandName": "",
        "commandArgument": "",
        "validationGroup": None,
        "checked": False,
    }
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False)


_NEVER_POSTED = ("hdnBtnLogin", "hdnBtnMfa", "btnForgotPassword")


def build_login_body(fields: dict[str, str], username: str, password: str, text: str) -> dict[str, str]:
    """The login POST body: the served fields with the credentials, the
    checkbox client state and `btnLogin=Login`; the hidden MFA/login/forgot
    buttons never."""
    body = dict(fields)
    for name in _NEVER_POSTED:
        body.pop(name, None)
    body["txtUserName"] = username
    body["txtPassword"] = password
    body["chkAgree_ClientState"] = agree_client_state(text)
    body["btnLogin"] = "Login"
    return body


def login_message(html: str) -> str | None:
    """The `#divLoginMessage` text of a re-rendered login page, tags stripped
    and capped, for the failure sentence. None when there is none."""
    soup = _load_html(html)
    node = soup.find(id="divLoginMessage")
    if node is None:
        return None
    return _cap(node.get_text(" ", strip=True), _ERROR_MAX_CHARS)


def _is_login_form(html: str) -> bool:
    return 'name="txtPassword"' in html and "VendorLogin.aspx" in html


def _mfa_prompt_shown(html: str) -> bool:
    """The MFA RadWindow is opened on load: a call to `tryMfa()` /
    `OpenMfaPopup()` outside their definitions, or the window created
    visible."""
    if "rwMfaPrompt" not in html and "MfaPromptPopup" not in html:
        return False
    if _MFA_VISIBLE_RE.search(html):
        return True
    for m in _MFA_CALL_RE.finditer(html):
        head = html[max(0, m.start() - 12) : m.start()].rstrip()
        if not head.endswith("function"):
            return True
    return False


def _is_bad_request_page(html: str) -> bool:
    return "BadRequest.aspx" in html and "Cannot complete request" in html


def _is_error_page(html: str) -> bool:
    return 'id="divError"' in html and "An error has occurred" in html


def _has_invited_grid(html: str) -> bool:
    return f'id="{INVITED_GRID_ID}"' in html


def _pager_targets(grid: Tag, pattern: re.Pattern[str]) -> tuple[dict[int, str], int]:
    """(page -> __EVENTTARGET) from the grid's own pager anchors, and the
    current page. Only targets matching the grid's pager pattern count."""
    targets: dict[int, str] = {}
    current = 1
    tfoot = grid.find("tfoot", recursive=False)
    if tfoot is None:
        return targets, current
    for a in tfoot.find_all("a", href=True):
        m = _POSTBACK_HREF_RE.search(a["href"])
        if not m or not pattern.match(m.group(1)):
            continue
        title = a.get("title") or ""
        tm = _PAGE_TITLE_RE.search(title)
        label = _text(a)
        page = int(tm.group(1)) if tm else (int(label) if label.isdigit() else None)
        if page is None:
            continue
        targets[page] = m.group(1)
        if "rgCurrentPage" in (a.get("class") or []):
            current = page
    return targets, current


def _pager_info(grid: Tag) -> tuple[int | None, int | None]:
    tfoot = grid.find("tfoot", recursive=False)
    m = _PAGER_INFO_RE.search(_text(tfoot)) if tfoot is not None else None
    if not m:
        return None, None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))


def _header_map(grid: Tag) -> dict[str, int]:
    """Header text (casefolded) -> column index, from the thead row whose
    cells are `th` elements (the filter row is `td`s)."""
    thead = grid.find("thead", recursive=False)
    if thead is None:
        return {}
    for tr in thead.find_all("tr", recursive=False):
        cells = tr.find_all(["th", "td"], recursive=False)
        if cells and any(c.name == "th" for c in cells):
            return {_text(c).casefold(): i for i, c in enumerate(cells) if _text(c)}
    return {}


def _body_rows(grid: Tag) -> list[Tag]:
    tbody = grid.find("tbody", recursive=False)
    if tbody is None:
        return []
    rows = []
    for tr in tbody.find_all("tr", recursive=False):
        classes = tr.get("class") or []
        if "rgNoRecords" in classes or "rgPager" in classes:
            continue
        if not tr.find_all("td", recursive=False):
            continue
        rows.append(tr)
    return rows


def _form_of(soup: BeautifulSoup, base: str) -> tuple[str, dict[str, str]]:
    form = soup.find("form", id="aspnetForm") or soup.find("form")
    if form is None:
        raise NgemParseError("The NGEM page carried no form to post back to.")
    action = urljoin(base, html_lib.unescape(form.get("action") or ""))
    return action, _form_fields(form)


def _link(href: str | None, base: str, path: str) -> str | None:
    """`href` resolved against `base` when it is the expected portal page
    with its `e=` token and nothing else; None otherwise."""
    if not href:
        return None
    try:
        url = urljoin(base, html_lib.unescape(href.strip()))
    except ValueError:
        return None
    return url if _is_portal_link(url, path) else None


_LIST_COLUMNS = {
    "agency": "agency",
    "bid number": "bid_number_raw",
    "title": "title",
    "issue date": "issued_on",
    "close date": "close_at",
    "time left": "time_left",
    "bid status": "bid_status",
    "response status": "response_status",
    "response status code": "response_code",
    "status code": "status_code",
}
_LIST_REQUIRED = ("agency", "bid number", "title")
_LIST_BASE = f"{BASE}/VendorResponse/"
_BID_BASE = f"{BASE}/VendorResponse/Bid/"


def parse_invitations_page(html: str) -> ParsedGrid:
    """The "My Invitations" grid of the Available Bids tab: rows mapped by
    header text, the pager's total and page targets, and the form as served
    for the next postback. NgemParseError when the grid is missing or has
    lost its columns."""
    soup = _load_html(html)
    grid = soup.find("table", id=INVITED_GRID_ID)
    if grid is None:
        raise NgemParseError("The NGEM page did not carry the invitations grid.")
    headers = _header_map(grid)
    missing = [h for h in _LIST_REQUIRED if h not in headers]
    if missing:
        raise NgemParseError(
            "The NGEM invitations grid changed shape (missing column: "
            + ", ".join(missing) + ")."
        )
    rows: list[InvitationRow] = []
    for tr in _body_rows(grid):
        cells = tr.find_all("td", recursive=False)

        def cell(header: str) -> str | None:
            i = headers.get(header)
            return _text(cells[i]) if i is not None and i < len(cells) else None

        agency = _cap(cell("agency"), _SHORT_MAX) or ""
        bid_number_raw = _cap(cell("bid number"), _SHORT_MAX) or ""
        title = _cap(cell("title"), _TITLE_MAX) or ""
        if not agency and not bid_number_raw and not title:
            continue
        bid_number, addendum_no = parse_bid_number(bid_number_raw)
        link = tr.find("a", id=re.compile(r"_aHrfView$"))
        rows.append(
            InvitationRow(
                agency=agency,
                bid_number_raw=bid_number_raw,
                bid_number=bid_number,
                addendum_no=addendum_no,
                title=title,
                issued_on=parse_date(cell("issue date")),
                close_at=parse_pt_datetime(cell("close date")),
                time_left=_cap(cell("time left"), _SHORT_MAX),
                bid_status=_cap(cell("bid status"), _SHORT_MAX),
                response_status=_cap(cell("response status"), _SHORT_MAX),
                response_code=_cap(cell("response status code"), _SHORT_MAX),
                status_code=_cap(cell("status code"), _SHORT_MAX),
                view_url=_link(link.get("href") if link else None, _LIST_BASE, EVENT_PATH),
            )
        )
    total_items, total_pages = _pager_info(grid)
    targets, current = _pager_targets(grid, INVITED_PAGER_TARGET_RE)
    action, fields = _form_of(soup, _LIST_BASE)
    return ParsedGrid(
        rows=rows,
        total_items=total_items if total_items is not None else len(rows),
        total_pages=max(1, total_pages or 1),
        pager_targets=targets,
        form_fields=fields,
        form_action=action,
        current_page=current,
    )


def _label_table(soup: BeautifulSoup, label: str) -> Tag | None:
    """The innermost table holding a `td.fieldLabel` that reads `label`."""
    for td in soup.find_all("td", class_="fieldLabel"):
        if _text(td).casefold() == label.casefold():
            return td.find_parent("table")
    return None


def _label_values(table: Tag | None) -> dict[str, Tag]:
    """Label text (casefolded, trailing colon dropped) -> the value cell."""
    out: dict[str, Tag] = {}
    if table is None:
        return out
    tbody = table.find("tbody", recursive=False) or table
    for tr in tbody.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        if "fieldLabel" not in (cells[0].get("class") or []):
            continue
        key = _text(cells[0]).casefold().rstrip(":").strip()
        if key and key not in out:
            out[key] = cells[1]
    return out


def sanitize_notes_html(raw: str | None) -> str | None:
    """The notes as an allowlisted HTML fragment (p, br, b, strong, i, em, u,
    ul, ol, li, a[href https only]; everything else stripped, script and
    style contents removed), capped at 40,000 characters."""
    if not raw or not raw.strip():
        return None
    cleaned = nh3.clean(
        raw[: _NOTES_HTML_MAX * 2],
        tags=_NOTES_TAGS,
        attributes=_NOTES_ATTRIBUTES,
        url_schemes=_NOTES_SCHEMES,
        strip_comments=True,
    )
    if len(cleaned) > _NOTES_HTML_MAX:
        # Cut, then re-clean so the cut closes its tags; the closers of any
        # sane nesting fit in the margin, and a hard slice is the backstop.
        cleaned = nh3.clean(
            cleaned[: _NOTES_HTML_MAX - 256],
            tags=_NOTES_TAGS,
            attributes=_NOTES_ATTRIBUTES,
            url_schemes=_NOTES_SCHEMES,
            strip_comments=True,
        )[:_NOTES_HTML_MAX]
    cleaned = cleaned.strip()
    return cleaned or None


def notes_text(raw: str | None) -> str | None:
    """The notes as plain text through `rfp_harvest.html_to_text`, capped at
    20,000 characters."""
    from app.services.rfp_harvest import html_to_text  # noqa: PLC0415 - avoids an import cycle

    return html_to_text(raw, limit=_NOTES_TEXT_MAX)


def _address_lines(cell: Tag) -> str | None:
    """The nested address table's rows joined with commas (one line in the
    capture, street lines above it on other bids)."""
    lines: list[str] = []
    rows = cell.find_all("tr")
    if rows:
        for tr in rows:
            line = _text(tr)
            if line:
                lines.append(line)
    else:
        for line in cell.get_text("\n").splitlines():
            if _squash(line):
                lines.append(_squash(line))
    return _cap(", ".join(lines), _SHORT_MAX * 2)


def parse_event_page(html: str) -> EventFacts:
    """The bid's Event Details page: the header line, the "Bid Information"
    and "Bid Contact Information" labels (matched by label text), the notes
    as text and as a sanitized fragment, the tab strip's Attachments link."""
    soup = _load_html(html)
    info = _label_values(_label_table(soup, "Bid Type"))
    if not info:
        raise NgemParseError("The NGEM event page did not carry the Bid Information table.")
    contact_table = soup.find("table", id="ctl00_mainContent_CompanyAddress_tblEventAddr")
    if contact_table is None:
        contact_table = _label_table(soup, "Contact Name") or _label_table(soup, "Workgroup")
    contact_cells = _label_values(contact_table)

    header = soup.find(id="ctl00_mainContent_ctlTabStrip_divBidNumber")
    header_line = _direct_text(header) if header is not None else ""
    hm = _HEADER_LINE_RE.match(header_line)
    if hm:
        bid_number_raw = _squash(hm.group("number"))[:_SHORT_MAX]
        title = _squash(hm.group("title"))[:_TITLE_MAX]
    else:
        bid_number_raw = header_line[:_SHORT_MAX]
        title = ""
    bid_number, addendum_no = parse_bid_number(bid_number_raw)

    def info_text(label: str) -> str | None:
        node = info.get(label)
        return _cap(node.get_text(" ", strip=True), _SHORT_MAX) if node is not None else None

    notes_cell = info.get("notes")
    notes_raw = None
    if notes_cell is not None:
        holder = notes_cell.find("span", id="ctl00_mainContent_lblNotes") or notes_cell
        notes_raw = holder.decode_contents()

    def contact_text(label: str) -> str | None:
        node = contact_cells.get(label)
        return _cap(node.get_text(" ", strip=True), _SHORT_MAX) if node is not None else None

    email = None
    email_cell = contact_cells.get("contact email")
    if email_cell is not None:
        mail_link = email_cell.find("a", href=re.compile(r"^mailto:", re.IGNORECASE))
        email = _cap(_text(mail_link) if mail_link is not None else _text(email_cell), _SHORT_MAX)
    address_cell = contact_cells.get("address")
    contact = {
        "workgroup": contact_text("workgroup"),
        "name": contact_text("contact name"),
        "address": _address_lines(address_cell) if address_cell is not None else None,
        "phone": contact_text("contact phone"),
        "email": email,
    }

    attachments_url = None
    event_url = None
    for a in soup.find_all("a", class_="rtsLink", href=True):
        attachments_url = attachments_url or _link(a["href"], _BID_BASE, ATTACHMENTS_PATH)
        event_url = event_url or _link(a["href"], _BID_BASE, EVENT_PATH)
    if event_url is None:
        form = soup.find("form", id="aspnetForm")
        if form is not None:
            event_url = _link(form.get("action"), _BID_BASE, EVENT_PATH)

    return EventFacts(
        bid_number_raw=bid_number_raw,
        title=title,
        bid_type=info_text("bid type"),
        status=info_text("status"),
        issued_at=parse_pt_datetime(info_text("issue date & time")),
        close_at=parse_pt_datetime(info_text("close date & time")),
        question_cutoff_at=parse_pt_datetime(
            info_text("question cuttoff date & time") or info_text("question cutoff date & time")
        ),
        notes_html=sanitize_notes_html(notes_raw),
        notes_text=notes_text(notes_raw),
        contact=contact,
        attachments_url=attachments_url,
        event_url=event_url,
        bid_number=bid_number,
        addendum_no=addendum_no,
    )


_ATTACHMENT_REQUIRED = ("file name",)


def parse_attachments_page(html: str) -> ParsedAttachments:
    """The bid's Attachments grid: one row per file with the name and size
    parsed apart, the description, the per-file Download link; the pager and
    the form as served. The "Download All" anchor is never read as a link."""
    soup = _load_html(html)
    grid = soup.find("table", id=ATTACHMENTS_GRID_ID)
    if grid is None:
        raise NgemParseError("The NGEM page did not carry the attachments grid.")
    headers = _header_map(grid)
    missing = [h for h in _ATTACHMENT_REQUIRED if h not in headers]
    if missing:
        raise NgemParseError(
            "The NGEM attachments grid changed shape (missing column: "
            + ", ".join(missing) + ")."
        )
    rows: list[AttachmentRow] = []
    for ordinal, tr in enumerate(_body_rows(grid), start=1):
        cells = tr.find_all("td", recursive=False)

        def cell(header: str) -> str | None:
            i = headers.get(header)
            return _text(cells[i]) if i is not None and i < len(cells) else None

        file_cell = cell("file name") or ""
        name, size = split_file_cell(file_cell)
        if not name:
            continue
        index_text = cell("#") or ""
        index = int(index_text) if index_text.isdigit() else ordinal
        link = tr.find("a", id=re.compile(r"_lnkDownload$"))
        rows.append(
            AttachmentRow(
                index=index,
                file_name=name[:_TITLE_MAX],
                size_bytes=size,
                description=_cap(cell("description"), _TITLE_MAX),
                download_url=_link(link.get("href") if link else None, _BID_BASE, EXTRACT_PATH),
            )
        )
    total_items, total_pages = _pager_info(grid)
    targets, current = _pager_targets(grid, ATTACHMENTS_PAGER_TARGET_RE)
    action, fields = _form_of(soup, _BID_BASE)
    return ParsedAttachments(
        rows=rows,
        total_pages=max(1, total_pages or 1),
        pager_targets=targets,
        form_fields=fields,
        form_action=action,
        total_items=total_items if total_items is not None else len(rows),
        current_page=current,
    )


# ── Content-Disposition ──────────────────────────────────────────────────────

_CD_EXT_RE = re.compile(r"filename\*\s*=\s*([\w-]+)'[^']*'([^;]+)", re.IGNORECASE)
_CD_QUOTED_RE = re.compile(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', re.IGNORECASE)
_CD_BARE_RE = re.compile(r"filename\s*=\s*([^;\s]+)", re.IGNORECASE)


def sanitize_filename(name: str | None) -> str | None:
    """A display name: no path separators (the part after the last one),
    no control characters, whitespace tidied, no leading dots, capped."""
    if not name:
        return None
    value = html_lib.unescape(name)
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = _CONTROL_CHARS_RE.sub("", value)
    value = _squash(value).lstrip(". ").strip()
    return value[:_FILENAME_MAX] or None


def content_disposition_filename(header: str | None) -> str | None:
    """The file name from a Content-Disposition header (RFC 6266: the
    `filename*=` form wins, then `filename=`), sanitized; None when absent."""
    if not header:
        return None
    m = _CD_EXT_RE.search(header)
    if m:
        charset = m.group(1).lower() or "utf-8"
        try:
            raw = unquote(m.group(2).strip(), encoding=charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            raw = None
        name = sanitize_filename(raw)
        if name:
            return name
    m = _CD_QUOTED_RE.search(header)
    raw = m.group(1).replace('\\"', '"').replace("\\\\", "\\") if m else None
    if raw is None:
        m = _CD_BARE_RE.search(header)
        raw = m.group(1) if m else None
    if raw is not None:
        # httpx decodes headers as latin-1; a UTF-8 name shows up mojibake'd.
        try:
            raw = raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return sanitize_filename(raw)


# ── Session store protocol ───────────────────────────────────────────────────


class SessionStore(Protocol):
    """Where the one shared NGEM session lives (rfp_harvest_sessions,
    provider `ngem`). `load` returns None when nothing is stored.
    `record_login(ok=False, counts_toward_lock=False)` stamps
    `last_login_attempt_at` (and the error) WITHOUT touching the failure
    counter: a login chain that broke on a 5xx or a timeout still spent the
    credentials, so every process must honour the minimum interval, but it
    says nothing about the credentials."""

    def load(self) -> dict | None: ...
    def save_cookies(self, account: str, cookies: list[dict]) -> None: ...
    def record_login(
        self, *, ok: bool, error: str | None, counts_toward_lock: bool = True
    ) -> dict: ...
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

    def record_login(
        self, *, ok: bool, error: str | None, counts_toward_lock: bool = True
    ) -> dict:
        now = self.now()
        self.state["last_login_attempt_at"] = now.isoformat()
        if ok:
            self.state["login_failures"] = 0
            self.state["locked_until"] = None
            self.state["last_error"] = None
        elif not counts_toward_lock:
            self.state["last_error"] = error
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
    the previous portal request in this process. The reservation is made
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


# ── Allowlist helpers ────────────────────────────────────────────────────────


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def _path(url: str) -> str:
    return urlparse(url).path or "/"


def assert_allowed_get(path: str) -> None:
    """Raise ValueError for a GET path outside ALLOWED_GET_PATHS."""
    if path not in ALLOWED_GET_PATHS:
        raise ValueError(f"NGEM path is not on the GET allowlist: {path}")


def assert_allowed_post(path: str) -> None:
    """Raise ValueError for a POST path outside ALLOWED_POST_PATHS."""
    if path not in ALLOWED_POST_PATHS:
        raise ValueError(f"NGEM path is not on the POST allowlist: {path}")


def assert_allowed_postback_target(target: str) -> None:
    """Raise ValueError for a `__EVENTTARGET` outside the two grid pagers."""
    if not target or not any(p.match(target) for p in ALLOWED_POSTBACK_TARGETS):
        raise ValueError(f"NGEM postback target is not a grid pager: {target!r}")


def assert_allowed_url(method: str, url: str) -> None:
    """Raise ValueError unless `url` is https on the portal host and its path
    is allowlisted for `method`."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.netloc or "").lower() != HOST:
        raise ValueError(f"NGEM request to an unexpected origin: {parsed.scheme}://{parsed.netloc}")
    if method.upper() == "POST":
        assert_allowed_post(parsed.path or "/")
    else:
        assert_allowed_get(parsed.path or "/")


def _is_portal_link(url: str | None, path: str) -> bool:
    """`url` is `https://supplier.ionwave.net<path>?e=<token>` and nothing else."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or (parsed.netloc or "").lower() != HOST:
        return False
    if parsed.path != path or parsed.fragment:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    return set(query) == {"e"} and bool(query["e"][0])


# ── Session ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NgemConfig:
    username: str
    password: str = field(repr=False)   # never in a repr, a log line or a traceback
    entry_url: str
    min_request_interval: float = 2.0
    login_min_interval: int = 600
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password and self.entry_url)

    @property
    def entry_url_ok(self) -> bool:
        try:
            parsed = urlparse(self.entry_url or "")
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and (parsed.netloc or "").lower() == HOST
            and parsed.path == ENTRY_PATH
        )


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


_MSG_LOCKED = "NGEM logins are locked after repeated failures."
_MSG_NOT_CONFIGURED = "NGEM credentials are not configured."
_MSG_BAD_ENTRY = "NGEM_ENTRY_URL is not a supplier.ionwave.net ResponseList.aspx link."


def availability_from(
    store_row: dict | None,
    now: datetime | None = None,
    *,
    login_min_interval: int | None = None,
) -> tuple[bool, str | None, datetime | None]:
    """(usable, reason, locked_until) from the stored session row alone, for
    the service's status route: the lock, and (when `login_min_interval` is
    given) a failed attempt still inside the login interval."""
    at = now or _now()
    until = lock_state(store_row, at)
    if until:
        return False, _MSG_LOCKED, until
    if login_min_interval and store_row and int(store_row.get("login_failures") or 0) > 0:
        last = _parse_ts(store_row.get("last_login_attempt_at"))
        if last and (at - last).total_seconds() < login_min_interval:
            wait_until = last + timedelta(seconds=login_min_interval)
            return False, "NGEM login failed recently; the next attempt waits.", wait_until
    return True, None, None


class NgemSession:
    """One logged-in NGEM session over a single httpx client.

    `transport`, `sleep`, `clock`, `rng` and `now` are test seams. `renew` is
    an optional callback run before each paced request (the queue lease).
    `on_lock(until, error)` rings once when the failure counter locks logins."""

    def __init__(
        self,
        config: NgemConfig,
        store: SessionStore,
        *,
        on_lock: Callable[[datetime, str], None] | None = None,
        transport: httpx.BaseTransport | None = None,
        renew: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.config = config
        self.store = store
        self._on_lock = on_lock
        self._renew = renew
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._now = now
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

    def __enter__(self) -> "NgemSession":
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
        if state.get("account") and state["account"] != self.config.username:
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
                    c["name"], c["value"], domain=c.get("domain") or HOST, path=c.get("path") or "/"
                )
            except (KeyError, TypeError, ValueError):
                continue
        return state

    def _export_cookies(self) -> list[dict]:
        out: list[dict] = []
        for cookie in self._client.cookies.jar:
            domain = (cookie.domain or "").lower()
            if not domain.endswith("ionwave.net"):
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

    def _has_session(self) -> bool:
        for cookie in self._client.cookies.jar:
            if cookie.name == _SESSION_COOKIE and (cookie.domain or "").lower().lstrip(".").endswith(
                "ionwave.net"
            ):
                return True
        return False

    def _jar_is_newer(self, state: dict) -> bool:
        """Another worker logged in after we loaded our jar."""
        saved_at = _parse_ts(state.get("logged_in_at"))
        return bool(saved_at and (self._jar_loaded_at is None or saved_at > self._jar_loaded_at))

    # -- requests -------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """Portal-authored text with the password removed, should it ever be
        echoed back."""
        if self.config.password and self.config.password in text:
            return text.replace(self.config.password, "[redacted]")
        return text

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One paced page request. The body is streamed and stops at
        _MAX_HTML_BYTES (NgemParseError: no portal page is that large, and
        the client never buffers more than that); the response handed back
        carries the capped body already read."""
        assert_allowed_url(method, url)
        if self._renew is not None:
            self._renew()
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        body = bytearray()
        try:
            with self._client.stream(method, url, **kwargs) as resp:
                if resp.status_code not in _REDIRECTS:
                    for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                        body.extend(chunk)
                        if len(body) > _MAX_HTML_BYTES:
                            raise NgemParseError(
                                "NGEM answered with a larger page than the client accepts."
                            )
        except httpx.TransportError as exc:
            raise NgemTransient(f"NGEM did not answer ({type(exc).__name__}).") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise NgemTransient(f"NGEM answered {resp.status_code}; the request will be retried.")
        # The body is already decoded, so the transfer headers must not be
        # applied to it a second time.
        headers = httpx.Headers(resp.headers)
        for name in ("content-encoding", "content-length", "transfer-encoding"):
            headers.pop(name, None)
        return httpx.Response(
            resp.status_code, headers=headers, content=bytes(body), request=resp.request
        )

    @staticmethod
    def _html_headers(referer: str | None = None) -> dict[str, str]:
        headers = {"Accept": _HTML_ACCEPT, "Upgrade-Insecure-Requests": "1"}
        if referer:
            headers["Referer"] = referer
        return headers

    @staticmethod
    def _post_headers(referer: str | None) -> dict[str, str]:
        headers = {
            "Accept": _HTML_ACCEPT,
            "Origin": BASE,
            "Content-Type": "application/x-www-form-urlencoded",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _redirect_target(self, resp: httpx.Response) -> tuple[str, str, str]:
        """(target, host, path) of a redirect. A Location whose scheme is not
        https is portal-controlled input the client will not follow: it is
        reported as `host = ""` so every caller's off-portal branch takes it
        (NgemForbidden or NgemLoginFailed), never a ValueError from the
        allowlist, which is reserved for code-authored URLs."""
        target = urljoin(str(resp.url), resp.headers.get("location", ""))
        if urlparse(target).scheme != "https":
            return target, "", _path(target)
        return target, _host(target), _path(target)

    @staticmethod
    def _form_action_ok(action: str, path: str) -> bool:
        """A form's action (portal-controlled) resolves to https on the portal
        host at the page it was served from; anything else is refused before
        the allowlist would raise for it."""
        parsed = urlparse(action)
        return (
            parsed.scheme == "https"
            and (parsed.netloc or "").lower() == HOST
            and (parsed.path or "/") == path
        )

    def _settle(self, resp: httpx.Response) -> tuple[httpx.Response, str]:
        """Follow redirects by hand (same host, allowlisted pages, at most 6
        hops) and classify the landing page. The login page or a redirect to
        it is NgemSessionExpired; `/Error.aspx` is NgemTransient;
        `/BadRequest.aspx` is NgemForbidden(reason=STALE_LINK)."""
        hops = 0
        while resp.status_code in _REDIRECTS:
            hops += 1
            if hops > _MAX_HOPS:
                raise NgemTransient("NGEM redirected too many times.")
            target, host, path = self._redirect_target(resp)
            if host != HOST:
                raise NgemForbidden(f"NGEM redirected off the portal. {_MSG_ELSEWHERE}")
            if path in _LOGIN_PATHS:
                raise NgemSessionExpired()
            if path == ERROR_PATH:
                raise NgemTransient("NGEM answered its error page.")
            if path == BAD_REQUEST_PATH:
                raise NgemForbidden("The NGEM link is stale (BadRequest.aspx).", reason=STALE_LINK)
            if path not in ALLOWED_GET_PATHS:
                raise NgemForbidden(
                    f"NGEM redirected somewhere unexpected ({path}). {_MSG_ELSEWHERE}"
                )
            resp = self._request("GET", target, headers=self._html_headers(str(resp.url)))
        if resp.status_code in (401, 403, 404):
            raise NgemForbidden(f"NGEM refused the page ({resp.status_code}).")
        if resp.status_code != 200:
            raise NgemTransient(f"NGEM answered {resp.status_code}.")
        text = resp.text
        if _is_login_form(text):
            raise NgemSessionExpired()
        if _is_bad_request_page(text):
            raise NgemForbidden("The NGEM link is stale (BadRequest.aspx).", reason=STALE_LINK)
        if _is_error_page(text):
            raise NgemTransient("NGEM answered its error page.")
        self.store.touch()
        return resp, text

    def _get_page(self, url: str, *, referer: str | None = None) -> tuple[httpx.Response, str]:
        return self._settle(self._request("GET", url, headers=self._html_headers(referer)))

    def _postback(self, action: str, fields: dict[str, str], target: str, *, referer: str | None) -> str:
        """The full postback a page would send for a pager click: every field
        as served, `__EVENTTARGET` set to the pager target, `__EVENTARGUMENT`
        empty, no `__ASYNCPOST`. Refuses any target outside the two pagers,
        any text in a filter box and any armed zip action."""
        assert_allowed_postback_target(target)
        if not any(self._form_action_ok(action, path) for path in (ENTRY_PATH, ATTACHMENTS_PATH)):
            raise NgemForbidden(f"The NGEM form posts somewhere unexpected. {_MSG_ELSEWHERE}")
        for name, value in fields.items():
            if _FILTER_FIELD_RE.search(name) and value != "":
                raise ValueError(f"NGEM postback would put text in a filter field: {name}")
            if _ZIP_FIELD_RE.search(name) and value != "":
                raise ValueError("NGEM postback would trigger the Download All zip action")
        body = dict(fields)
        body.pop("__ASYNCPOST", None)
        body["__EVENTTARGET"] = target
        body["__EVENTARGUMENT"] = ""
        resp = self._request("POST", action, data=body, headers=self._post_headers(referer))
        _, text = self._settle(resp)
        return text

    def _with_session(self, fn: Callable[[], Any]) -> Any:
        """Run `fn`; when it proves the session gone, log in once (subject to
        the discipline) and run it once more."""
        if not self._loaded:
            self._load_store()
        try:
            return fn()
        except NgemSessionExpired:
            self.ensure_session()
            return fn()

    def _entry_url(self) -> str:
        if not self.config.entry_url_ok:
            raise NgemUnavailable(_MSG_BAD_ENTRY)
        return self.config.entry_url

    # -- pages ----------------------------------------------------------------

    def fetch_invitations(self, max_pages: int = 20) -> InvitationsPage:
        """The "My Invitations" rows from page 1 through min(total_pages,
        max_pages), de-duplicated by (agency, bid_number) in case the grid
        shifts between pages. Logs in once and retries once when the entry
        page proves the session gone."""
        entry = self._entry_url()
        grid = self._with_session(lambda: parse_invitations_page(self._get_page(entry)[1]))
        rows: list[InvitationRow] = []
        seen: set[tuple[str, str]] = set()

        def take(parsed: ParsedGrid) -> None:
            for row in parsed.rows:
                key = (row.agency, row.bid_number)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)

        take(grid)
        pages_fetched = 1
        page = 2
        while page <= min(grid.total_pages, max(1, max_pages)):
            target = grid.pager_targets.get(page)
            if target is None:
                raise NgemParseError(f"The NGEM invitations pager did not offer page {page}.")
            try:
                html = self._postback(grid.form_action, grid.form_fields, target, referer=entry)
            except NgemSessionExpired:
                # The session went away mid-scan: one login, fresh form fields
                # from page 1, and this page once more.
                self.ensure_session()
                grid = parse_invitations_page(self._get_page(entry)[1])
                take(grid)
                target = grid.pager_targets.get(page)
                if target is None:
                    raise NgemParseError(f"The NGEM invitations pager did not offer page {page}.")
                html = self._postback(grid.form_action, grid.form_fields, target, referer=entry)
            grid = parse_invitations_page(html)
            take(grid)
            pages_fetched += 1
            page += 1
        return InvitationsPage(
            rows=rows,
            total_items=grid.total_items,
            pages_fetched=pages_fetched,
            total_pages=grid.total_pages,
        )

    def fetch_event(self, view_url: str) -> EventFacts:
        """The bid's Event Details page behind the grid's view link (and only
        that page). A stale token answers BadRequest.aspx: NgemForbidden with
        `reason == STALE_LINK`."""
        if not _is_portal_link(view_url, EVENT_PATH):
            raise NgemForbidden("The NGEM view link is not a VResponseEvent.aspx link.")
        referer = self.config.entry_url if self.config.entry_url_ok else None
        _, html = self._with_session(lambda: self._get_page(view_url, referer=referer))
        return parse_event_page(html)

    def fetch_attachments(self, facts: EventFacts, max_pages: int = 20) -> list[AttachmentRow]:
        """Every file on the bid's Attachments page(s), through the tab strip
        link the event page carried, paging with the grid's own pager."""
        url = facts.attachments_url
        if not url:
            raise NgemParseError("The NGEM event page carried no Attachments tab.")
        if not _is_portal_link(url, ATTACHMENTS_PATH):
            raise NgemForbidden("The NGEM attachments link is not a VResponseBidAttachments.aspx link.")
        referer = facts.event_url
        page_one = self._with_session(lambda: parse_attachments_page(self._get_page(url, referer=referer)[1]))
        rows: list[AttachmentRow] = []
        seen: set[tuple[int, str]] = set()

        def take(parsed: ParsedAttachments) -> None:
            for row in parsed.rows:
                key = (row.index, row.file_name)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)

        parsed = page_one
        take(parsed)
        page = 2
        while page <= min(parsed.total_pages, max(1, max_pages)):
            target = parsed.pager_targets.get(page)
            if target is None:
                raise NgemParseError(f"The NGEM attachments pager did not offer page {page}.")
            try:
                html = self._postback(parsed.form_action, parsed.form_fields, target, referer=url)
            except NgemSessionExpired:
                self.ensure_session()
                parsed = parse_attachments_page(self._get_page(url, referer=referer)[1])
                take(parsed)
                target = parsed.pager_targets.get(page)
                if target is None:
                    raise NgemParseError(f"The NGEM attachments pager did not offer page {page}.")
                html = self._postback(parsed.form_action, parsed.form_fields, target, referer=url)
            parsed = parse_attachments_page(html)
            take(parsed)
            page += 1
        return rows

    # -- downloads ------------------------------------------------------------

    def download(
        self, url: str, dest: Path, max_bytes: int, *, referer: str | None = None
    ) -> DownloadResult:
        """Stream an `Extract.aspx?e=` link into `dest` (created O_EXCL) with
        the session jar, in 1 MB chunks under a running byte cap, unlinked on
        every failure. Returns the bytes written and the Content-Disposition
        file name (sanitized) or None. A redirect to the login page means one
        login and one retry; an HTML answer is NgemTransient; an oversize
        body is NgemForbidden."""
        if not _is_portal_link(url, EXTRACT_PATH):
            raise NgemForbidden("The NGEM download link is not an Extract.aspx link.")
        if referer is None:
            referer = f"{BASE}{ATTACHMENTS_PATH}"
        return self._with_session(lambda: self._download_once(url, dest, max_bytes, referer))

    def _download_once(self, url: str, dest: Path, max_bytes: int, referer: str) -> DownloadResult:
        assert_allowed_url("GET", url)
        if self._renew is not None:
            self._renew()
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(dest, flags, 0o644)
        written = 0
        headers = {"Accept": "*/*", "Referer": referer}
        try:
            with os.fdopen(fd, "wb") as fh, self._client.stream("GET", url, headers=headers) as resp:
                if resp.status_code in _REDIRECTS:
                    _, host, path = self._redirect_target(resp)
                    if host == HOST and path in _LOGIN_PATHS:
                        raise NgemSessionExpired()
                    if host == HOST and path == ERROR_PATH:
                        raise NgemTransient("NGEM answered its error page for the file.")
                    if host == HOST and path == BAD_REQUEST_PATH:
                        raise NgemForbidden(
                            "The NGEM download link is stale (BadRequest.aspx).", reason=STALE_LINK
                        )
                    raise NgemForbidden(
                        f"NGEM redirected the download somewhere unexpected. {_MSG_ELSEWHERE}"
                    )
                if resp.status_code in (401, 403, 404):
                    raise NgemForbidden(f"NGEM refused the file ({resp.status_code}).")
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise NgemTransient(f"NGEM answered {resp.status_code} for the file.")
                if resp.status_code != 200:
                    raise NgemTransient(f"NGEM answered {resp.status_code} for the file.")
                ctype = resp.headers.get("content-type", "").lower()
                if "text/html" in ctype:
                    raise NgemTransient("NGEM answered a page instead of the file.")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise NgemForbidden("The NGEM file is larger than the harvest accepts.")
                for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise NgemForbidden("The NGEM file is larger than the harvest accepts.")
                    fh.write(chunk)
                filename = content_disposition_filename(resp.headers.get("content-disposition"))
        except httpx.TransportError as exc:
            _unlink_quietly(dest)
            raise NgemTransient(f"NGEM did not answer the download ({type(exc).__name__}).") from exc
        except BaseException:
            _unlink_quietly(dest)
            raise
        self.store.touch()
        return DownloadResult(bytes_written=written, filename=filename)

    # -- login ----------------------------------------------------------------

    def availability(self) -> tuple[bool, str | None, datetime | None]:
        """(usable, reason, locked_until) without touching the portal."""
        if not self.config.configured:
            return False, _MSG_NOT_CONFIGURED, None
        if not self.config.entry_url_ok:
            return False, _MSG_BAD_ENTRY, None
        return availability_from(self.store.load(), self._now())

    def ensure_session(self) -> None:
        """Log in, subject to the discipline in the module docstring. Called
        only after a request proved the session gone."""
        if not self.config.configured:
            raise NgemUnavailable(_MSG_NOT_CONFIGURED)
        if not self.config.entry_url_ok:
            raise NgemUnavailable(_MSG_BAD_ENTRY)
        state = self.store.load()
        now = self._now()
        until = lock_state(state, now)
        if until:
            raise NgemLoginLocked(_MSG_LOCKED, locked_until=until)
        # A jar another worker saved since we loaded ours is worth one try
        # before spending a login.
        if state and state.get("cookies") and state.get("account") == self.config.username:
            if not self.logged_in_this_session and self._jar_is_newer(state):
                self._client.cookies.clear()
                self._load_store()
                if self._has_session():
                    return
        last = _parse_ts((state or {}).get("last_login_attempt_at")) or self._last_login_attempt
        if last and (now - last).total_seconds() < self.config.login_min_interval:
            wait_until = last + timedelta(seconds=self.config.login_min_interval)
            raise NgemUnavailable(
                "NGEM login was attempted recently; waiting before trying again.",
                locked_until=wait_until,
            )
        try:
            self._login()
        except NgemTransient as exc:
            # The chain broke on a 5xx or a timeout: the credentials may or
            # may not have been posted, so the attempt is stamped for every
            # process (the minimum interval holds across workers) without
            # counting toward the lock, which is about rejected credentials.
            self.store.record_login(
                ok=False, error=f"transient: {self._scrub(str(exc))}", counts_toward_lock=False
            )
            raise
        except NgemLoginFailed as exc:
            message = self._scrub(str(exc))
            state = self.store.record_login(ok=False, error=message)
            until = lock_state(state, self._now())
            logger.warning("NGEM login failed: %s", message)
            if until:
                if self._on_lock:
                    try:
                        self._on_lock(until, message)
                    except Exception:  # noqa: BLE001 - the bell must never mask the lock
                        logger.exception("NGEM lock notification failed")
                raise NgemLoginLocked(_MSG_LOCKED, locked_until=until) from exc
            raise NgemUnavailable(
                "NGEM login failed; it will be retried later.",
                locked_until=self._now() + timedelta(seconds=self.config.login_min_interval),
            ) from exc
        self.store.record_login(ok=True, error=None)
        self.store.save_cookies(self.config.username, self._export_cookies())
        self.logged_in_this_session = True

    def _follow_login_chain(self, resp: httpx.Response) -> httpx.Response:
        """Follow the redirects from the entry URL to the login form by hand:
        same host, allowlisted pages, at most 6 hops."""
        hops = 0
        while resp.status_code in _REDIRECTS:
            hops += 1
            if hops > _MAX_HOPS:
                raise NgemLoginFailed("The NGEM login redirected too many times.")
            target, host, path = self._redirect_target(resp)
            if host != HOST:
                raise NgemLoginFailed(f"The NGEM login redirected off the portal. {_MSG_ELSEWHERE}")
            if path == ERROR_PATH:
                raise NgemLoginFailed("unexpected page")
            if path not in ALLOWED_GET_PATHS:
                raise NgemLoginFailed(f"The NGEM login left the expected flow ({path}).")
            resp = self._request("GET", target, headers=self._html_headers(str(resp.url)))
        return resp

    def _login(self) -> None:
        self._last_login_attempt = self._now()
        self._client.cookies.clear()
        entry = self._entry_url()
        # 1. The entry URL bounces through /Login.aspx to /VendorLogin.aspx.
        resp = self._follow_login_chain(self._request("GET", entry, headers=self._html_headers()))
        if resp.status_code != 200:
            raise NgemLoginFailed(f"The NGEM login page answered {resp.status_code}.")
        page = resp.text
        if _mfa_prompt_shown(page):
            raise NgemLoginFailed("the account asks for MFA")
        if _is_error_page(page):
            raise NgemLoginFailed("unexpected page")
        form = parse_login_form(page)
        if form is None:
            raise NgemLoginFailed("The NGEM login page had no login form.")
        action, fields = form
        login_url = urljoin(str(resp.url), action)
        if _host(login_url) == HOST and _path(login_url) != VENDOR_LOGIN_PATH:
            # The form was rendered in place of another page (docs 3.2: the
            # login form where the grid was expected); its relative action
            # belongs to the site root.
            login_url = urljoin(f"{BASE}/", action)
        if not self._form_action_ok(login_url, VENDOR_LOGIN_PATH):
            raise NgemLoginFailed(f"The NGEM login form posts somewhere unexpected. {_MSG_ELSEWHERE}")
        # 2. The credentials, with every served field and the checkbox state.
        body = build_login_body(fields, self.config.username, self.config.password, checkbox_text(page))
        resp = self._request("POST", login_url, data=body, headers=self._post_headers(str(resp.url)))
        # 3. Success is a 302 to the entry URL with the session cookie set.
        if resp.status_code in _REDIRECTS:
            target, host, path = self._redirect_target(resp)
            if host != HOST:
                raise NgemLoginFailed(f"The NGEM login redirected off the portal. {_MSG_ELSEWHERE}")
            if path == ERROR_PATH:
                raise NgemLoginFailed("unexpected page")
            if path in _LOGIN_PATHS:
                raise NgemLoginFailed("credentials rejected")
            if path != ENTRY_PATH:
                raise NgemLoginFailed(f"The NGEM login left the expected flow ({path}).")
            if not self._has_session():
                raise NgemLoginFailed("The NGEM login set no session cookie.")
            try:
                _, landing = self._get_page(target, referer=str(resp.url))
            except NgemSessionExpired:
                raise NgemLoginFailed("The NGEM login did not stick.") from None
            except NgemForbidden as exc:
                raise NgemLoginFailed("unexpected page") from exc
            if not _has_invited_grid(landing):
                raise NgemLoginFailed("The NGEM entry page did not show the invitations grid.")
            logger.info("NGEM login succeeded for the portal account")
            return
        if resp.status_code == 200:
            page = resp.text
            if _mfa_prompt_shown(page):
                raise NgemLoginFailed("the account asks for MFA")
            if _is_login_form(page):
                raise NgemLoginFailed(self._scrub(login_message(page) or "credentials rejected"))
            raise NgemLoginFailed("unexpected page")
        raise NgemLoginFailed(f"The NGEM login answered {resp.status_code}.")


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
