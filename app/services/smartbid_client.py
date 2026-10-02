"""SmartBid (ConstructConnect) client for the RFP harvest
(docs/RFP_SMARTBID.md, sections 2 and 3).

A GC on SmartBid invites through the platform's own address, and every
invitation email carries a "Click Here to View the Project" link with the
recipient's per-project passport key. The harvest trades that key for a
bearer token at the platform's JSON API (`apicc.smartinsight.co`), reads
the confidentiality-agreement gate and the bid project (facts plus the plan
room tree), then fetches each file the way the SPA does: a per-file
security token, a direct-URL lookup, an Azure blob GET. Captured live
2026-10-01 on DC Building Group, Martin-Harris and R&O projects.

Four rules this module enforces on its own, so no caller can break them:

- Allowlist. A request leaves this process only for the nine shapes in
  `assert_allowed` (section 3.4): the email's three tracking hits (the read
  receipt, the open pixel, the View the Project link WITHOUT `iR`, never
  followed), the token POST, the two project reads, the security-token
  POST, the direct-URL lookup and the blob GET. The email's Yes / No links
  (`iR=1` / `iR=0`), `setallcodesanswer`, `linkwontbidthisjob`, every other
  `/api/...` route, Unsubscribe, the digital bid board, the access-key page
  and the SPA itself are unreachable from code. A violation is a
  ValueError: a test failure, never a runtime branch.
- Never answer, never accept. Nothing here can record a bid answer or
  accept an agreement: `gate` reads the agreement state and refuses a
  project that needs one (or a PAD invitation, or any `AlowedDetail`), and
  plan-room entries behind an agreement or a prequalification are flagged
  `restricted` so the harvest skips them.
- Pace. Every request waits its turn behind `_pace`: at least
  `smartbid_min_request_interval_seconds` times a random 1.0 to 2.0 since
  the previous SmartBid request in this process.
- Secrets. The passport key appears in the token POST body (and in the
  email's own View the Project link, pinged as a person would); the bearer
  token in the Authorization header; the security token and the SAS
  signature in one URL each. None of them appears in a log line, an
  exception message, a stored row or a `repr`; httpx's own request log has
  its SmartBid query strings redacted by a filter this module installs.
  Nothing secret is stored: the token lives for one harvest, in memory.

The session store (failure counter, lock) is the protocol Procore's and
PipelineSuite's clients take (`procore_client.SessionStore`), injected, so
this module has no Supabase import and the tests drive it with
`httpx.MockTransport` and an in-memory store. Parsing is pure.
"""

from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
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
from urllib.parse import parse_qsl, urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx

from app.services.pipelinesuite_client import classify_name
from app.services.procore_client import (
    MemorySessionStore,
    SessionStore,
    lock_state,
    unwrap_link,
)

__all__ = [
    "MemorySessionStore",
    "SessionStore",
    "SmartBidConfig",
    "SmartBidError",
    "SmartBidForbidden",
    "SmartBidLoginFailed",
    "SmartBidLoginLocked",
    "SmartBidProject",
    "SmartBidRef",
    "SmartBidSession",
    "SmartBidSessionExpired",
    "SmartBidTransient",
    "SmartBidUnavailable",
    "Tracking",
    "agreement_refusal",
    "assert_allowed",
    "bid_due_at",
    "bid_due_zone",
    "classify_name",
    "file_value",
    "key_fingerprint",
    "lock_state",
    "parse_project",
    "parse_reference",
    "parse_tracking",
    "unwrap_link",
]

logger = logging.getLogger(__name__)

PROVIDER = "smartbid"              # the sandbox source kind
SESSION_PROVIDER = "smartbid"      # rfp_harvest_sessions.provider (one row)
STORE_ACCOUNT = "passport"         # what the store row's `account` says: nothing secret

CLICK_HOST = "securecc.smartbidnet.com"
LOGIN_LINK_PATH = "/Main/Login.aspx"
READ_RECEIPT_PATH = "/External/RequestReadReceipt.aspx"
OPEN_HOST = "em.smartinsight.co"
OPEN_PATH = "/wf/open"
API_HOST = "apicc.smartinsight.co"
API_BASE = f"https://{API_HOST}"
TOKEN_PATH = "/token"
GATE_PATH = "/api/projects/getconfidentialagreement"
PROJECT_PATH = "/api/projects/getbidproject"
SECURITY_TOKEN_PATH = "/api/admin/getSecurityToken"
FILE_HOST = "apicc.smartbidnet.com"
FILE_PATH = "/project/fileMgmt/download"
BLOB_SUFFIX = ".blob.core.windows.net"
SPA_ORIGIN = "https://gocc.smartbid.co"
SPA_REFERER = "https://gocc.smartbid.co/"
PROJECT_LIST_URL = "https://gocc.smartbid.co/#/projectlist"

LOCKED_MESSAGE = "SmartBid logins are locked after repeated failures."
_MSG_LINK_REJECTED = (
    "SmartBid rejected this email's project link (the invitation may have been "
    "withdrawn or the link expired)."
)
_MSG_TOKEN_DOWN = "SmartBid refused the project link or is down (HTTP {status})."
_MSG_RECENT = "A SmartBid login was attempted recently; waiting before trying again."
_MSG_DATA = "SmartBid answered with data the harvest does not understand."
_MSG_NO_PROJECT = "SmartBid has no project {bid} for this email's project link."
_MSG_AGREEMENT = (
    "This SmartBid project needs a confidentiality agreement accepted in SmartBid "
    "first; open it there, then press Harvest again."
)
_MSG_NOT_ALLOWED = "SmartBid does not allow this project: {detail}."
_MSG_FILE_LINK = "The SmartBid file link does not belong to this project."
_MSG_FILE_TOKEN = "SmartBid gave no download token for this file."
_MSG_FILE_LOCATION = "SmartBid answered with a file location the harvest does not accept."

# A real browser's request headers.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_HTML_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
_IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
_JSON_ACCEPT = "application/json, text/plain, */*"
_BASE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

_ID = r"\d{1,12}"
_ID_RE = re.compile(rf"^{_ID}$")
_CID_RE = re.compile(rf"^bp_({_ID})$")
_KEY_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_URL_ANSWER_BYTES = 4096
_DOWNLOAD_CHUNK = 1024 * 1024
_PING_TIMEOUT = 10.0
_MAX_FILES = 2000
_DETAIL_MAX_CHARS = 200

# Allowlist shapes (section 3.4): the query parameter names each may carry.
_LOGIN_LINK_PARAMS = frozenset({"cid", "spassportkey", "sbidid", "st", "e"})
_RECEIPT_PARAMS = frozenset({"scommunicationid", "oimg"})
_GATE_PARAMS = frozenset({"bidProjectId", "personId", "isCcbc"})
_PROJECT_PARAMS = frozenset({"bidProjectId", "personId", "bidProjectType", "isIframe"})
_FILE_PARAMS = ("Value", "token", "timestamp")
_BLOB_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")
_BLOB_PATH_RE = re.compile(
    rf"^/[a-z0-9](?:[a-z0-9-]{{1,61}}[a-z0-9])?/Files/System_{_ID}/BidsProjectFiles/"
    rf"BidProject_({_ID})/({_ID})/[^/]+$"
)

_ZONES = {
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "MT": "America/Denver", "MDT": "America/Denver",
    "AZ": "America/Phoenix", "MST": "America/Phoenix",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "AKT": "America/Anchorage", "AKST": "America/Anchorage", "AKDT": "America/Anchorage",
    "HT": "Pacific/Honolulu", "HST": "Pacific/Honolulu",
}
_DEFAULT_ZONE = "America/Los_Angeles"

# Agreement person-row states that mean "accepted / nothing owed".
_AGREEMENT_OK_STATES = frozenset({1, 3, 6})


# ── Errors ───────────────────────────────────────────────────────────────────


class SmartBidError(RuntimeError):
    """Base: the message is app-authored and safe to store."""


class SmartBidTransient(SmartBidError):
    """Network trouble, a 5xx or a 429: worth retrying later."""


class SmartBidUnavailable(SmartBidError):
    """SmartBid cannot be used right now for a reason a retry does not fix
    on its own: logins locked, a login just attempted, an unexpected answer."""

    def __init__(self, message: str, *, locked_until: datetime | None = None) -> None:
        super().__init__(message)
        self.locked_until = locked_until


class SmartBidLoginLocked(SmartBidUnavailable):
    """Consecutive unexpected login answers reached the cap; `locked_until`
    is set."""


class SmartBidLoginFailed(SmartBidError):
    """One login attempt got an answer it does not understand; `step`
    names where."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


class SmartBidSessionExpired(SmartBidError):
    """Internal: a request proved the bearer token gone. The caller logs in
    once more and retries once."""


class SmartBidForbidden(SmartBidError):
    """The link was refused, the project needs an agreement or is not
    allowed, a file is missing or over the cap. Permanent."""


# ── Reference parsing (pure, section 2) ──────────────────────────────────────


def key_fingerprint(passport_key: str) -> str:
    """A stable, non-reversible name for a passport key: never the key."""
    return hashlib.sha256(passport_key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SmartBidRef:
    """The bid project, the email's comm detail id and the recipient's
    passport key, from the View the Project link. `repr` and `str` never
    include the key or the link (which carries it)."""

    bid_project_id: str
    comm_detail_id: str
    passport_key: str = field(repr=False)
    click_url: str = field(repr=False)

    def __str__(self) -> str:
        return repr(self)

    @property
    def external_key(self) -> str:
        return f"{PROVIDER}:{self.bid_project_id}"

    @property
    def session_provider(self) -> str:
        return SESSION_PROVIDER

    @property
    def external_url(self) -> str:
        """What rfp_harvests.external_url stores: no key (a person with their
        own SmartBid login lands on their project list)."""
        return PROJECT_LIST_URL

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.passport_key)


_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)


def _unwrapped(url: str) -> str:
    return unwrap_link(html_lib.unescape(url.strip()))


def _query_params(query: str) -> list[tuple[str, str]] | None:
    try:
        return parse_qsl(query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None


def _login_link(url: str) -> tuple[str, str, str, str | None] | None:
    """(bid project id, comm detail id, passport key, st) when `url` is the
    View the Project link shape: https, securecc.smartbidnet.com,
    /Main/Login.aspx, a `bp_<digits>` cId, a 40-hex key, a digits sBidId, no
    parameter outside cId / sPassportKey / sBidId / st / e (names compared
    without case), none repeated, and NO `iR` in any case. None otherwise."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.scheme or "").lower() != "https" or parsed.fragment:
        return None
    if (parsed.netloc or "").lower() != CLICK_HOST:
        return None
    if (parsed.path or "").lower() != LOGIN_LINK_PATH.lower():
        return None
    pairs = _query_params(parsed.query or "")
    if pairs is None:
        return None
    params: dict[str, str] = {}
    for name, value in pairs:
        lowered = name.lower()
        if lowered not in _LOGIN_LINK_PARAMS or lowered in params:
            # `iR` (the bid answer) and anything else unexpected end it here.
            return None
        params[lowered] = value
    cid = _CID_RE.match(params.get("cid", ""))
    key = params.get("spassportkey", "")
    bid = params.get("sbidid", "")
    if not cid or not _KEY_RE.match(key) or not _ID_RE.match(bid):
        return None
    for extra in ("st", "e"):
        if extra in params and not _ID_RE.match(params[extra]):
            return None
    return bid, cid.group(1), key, params.get("st")


def parse_reference(body_text: str | None) -> SmartBidRef | None:
    """The bid project and passport key from the email's text body: every
    URL unwrapped, the View the Project link shape kept (`iR` links are
    discarded, never fallen back to), `st=105` preferred, else the first
    kept. None when none is kept."""
    if not body_text:
        return None
    kept: list[tuple[tuple[str, str, str, str | None], str]] = []
    for raw in _URL_RE.findall(body_text):
        url = _unwrapped(raw)
        parts = _login_link(url)
        if parts is not None:
            kept.append((parts, url))
    if not kept:
        return None
    (bid, comm, key, _st), url = next((k for k in kept if k[0][3] == "105"), kept[0])
    return SmartBidRef(
        bid_project_id=str(int(bid)), comm_detail_id=str(int(comm)), passport_key=key, click_url=url
    )


# ── Tracking links (pure, section 2) ─────────────────────────────────────────


@dataclass(frozen=True)
class Tracking:
    read_receipt_url: str | None
    open_url: str | None
    click_url: str | None = field(repr=False)


_VIEW_TEXT = "view the project"
_BID_WORD_RE = re.compile(r"\bbid\b", re.IGNORECASE)
_ANSWER_WORD_RE = re.compile(r"\b(yes|no)\b", re.IGNORECASE)


class _LinkCollector(HTMLParser):
    """Every `img` (attributes) and every `a` (attributes and visible text)
    in document order. Nested anchors each collect the text inside them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[dict[str, str]] = []
        self.anchors: list[tuple[dict[str, str], list[str]]] = []
        self._open: list[tuple[dict[str, str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag == "img":
            self.images.append(values)
        elif tag == "a":
            entry: tuple[dict[str, str], list[str]] = (values, [])
            self.anchors.append(entry)
            self._open.append(entry)
        elif tag == "br":
            for _, parts in self._open:
                parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag == "a" and self._open:
            self._open.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        for _, parts in self._open:
            parts.append(data)


def _is_read_receipt(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if (parsed.scheme or "").lower() != "https" or (parsed.netloc or "").lower() != CLICK_HOST:
        return False
    if (parsed.path or "").lower() != READ_RECEIPT_PATH.lower() or parsed.fragment:
        return False
    pairs = _query_params(parsed.query or "")
    if not pairs:
        return False
    seen: dict[str, str] = {}
    for name, value in pairs:
        lowered = name.lower()
        if lowered not in _RECEIPT_PARAMS or lowered in seen:
            return False
        seen[lowered] = value
    return bool(_ID_RE.match(seen.get("scommunicationid", "")))


def _is_open_pixel(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if (parsed.scheme or "").lower() not in ("http", "https") or parsed.fragment:
        return False
    if (parsed.netloc or "").lower() != OPEN_HOST or parsed.path != OPEN_PATH:
        return False
    pairs = _query_params(parsed.query or "")
    return bool(pairs) and len(pairs) == 1 and pairs[0][0] == "upn" and bool(pairs[0][1])


def _is_bid_answer_text(text: str) -> bool:
    """Anchor text that reads like the bid question's answer ("Yes, I'll Bid
    All Codes", "No, I Won't Bid this Job")."""
    return bool(_BID_WORD_RE.search(text) and _ANSWER_WORD_RE.search(text))


def parse_tracking(page_html: str | None) -> Tracking:
    """The email's read receipt pixel, its SendGrid open pixel and its
    "Click Here to View the Project" link. The pixels are the first `img`
    whose unwrapped src has the shape; the click is the first anchor whose
    visible text contains "view the project" and whose unwrapped href (or
    Outlook's `originalsrc`) is the View the Project link shape. An anchor
    whose text pairs "bid" with "yes" or "no", or whose link carries `iR`,
    is never returned: those are the bid answer."""
    if not page_html:
        return Tracking(None, None, None)
    collector = _LinkCollector()
    try:
        collector.feed(page_html)
        collector.close()
    except Exception:  # noqa: BLE001 - html.parser is lenient; a broken page has no links
        return Tracking(None, None, None)
    receipt: str | None = None
    open_url: str | None = None
    for attrs in collector.images:
        src = attrs.get("src")
        if not src:
            continue
        target = _unwrapped(src)
        if receipt is None and _is_read_receipt(target):
            receipt = target
        elif open_url is None and _is_open_pixel(target):
            open_url = target
    click: str | None = None
    for attrs, parts in collector.anchors:
        text = " ".join("".join(parts).replace("\xa0", " ").split()).lower()
        if not text or _is_bid_answer_text(text) or _VIEW_TEXT not in text:
            continue
        for candidate in (attrs.get("href"), attrs.get("originalsrc")):
            if not candidate:
                continue
            target = _unwrapped(candidate)
            if _login_link(target) is not None:
                click = target
                break
        if click is not None:
            break
    return Tracking(receipt, open_url, click)


# ── Allowlist (section 3.4) ──────────────────────────────────────────────────


def _api_params_ok(query: str, allowed: frozenset[str]) -> bool:
    pairs = _query_params(query)
    if not pairs:
        return False
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names) or not set(names) <= allowed:
        return False
    values = dict(pairs)
    return bool(_ID_RE.match(values.get("bidProjectId", "")))


def _file_params_ok(query: str) -> bool:
    pairs = _query_params(query)
    if not pairs or [name for name, _ in pairs] != list(_FILE_PARAMS):
        return False
    return all(value for _, value in pairs)


def _shape(method: str, url: str) -> str | None:
    """The allowlist shape `(method, url)` is, or None (section 3.4)."""
    verb = (method or "").upper()
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.fragment or parsed.username or parsed.password or port is not None:
        return None
    scheme = (parsed.scheme or "").lower()
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parsed.query or ""
    if verb == "GET" and host == CLICK_HOST:
        if _login_link(url) is not None:
            return "click"
        if _is_read_receipt(url):
            return "read_receipt"
        return None
    if verb == "GET" and host == OPEN_HOST:
        return "open" if _is_open_pixel(url) else None
    if scheme != "https":
        return None
    if host == API_HOST:
        if verb == "POST" and path == TOKEN_PATH and not query:
            return "token"
        if verb == "GET" and path == GATE_PATH and _api_params_ok(query, _GATE_PARAMS):
            return "gate"
        if verb == "GET" and path == PROJECT_PATH and _api_params_ok(query, _PROJECT_PARAMS):
            return "project"
        if verb == "POST" and path == SECURITY_TOKEN_PATH and not query:
            return "security_token"
        return None
    if host == FILE_HOST:
        if verb == "GET" and path == FILE_PATH and _file_params_ok(query):
            return "direct_url"
        return None
    if host.endswith(BLOB_SUFFIX) and _BLOB_ACCOUNT_RE.match(host[: -len(BLOB_SUFFIX)]):
        if verb == "GET" and _BLOB_PATH_RE.match(path) and query:
            return "blob"
    return None


def assert_allowed(method: str, url: str) -> None:
    """Raise ValueError unless (method, url) is one of the nine request
    shapes the harvest may send. Runs before every request; a violation is
    a test failure at development time, never a runtime branch. The error
    never echoes a query string (keys, tokens and signatures live there)."""
    if _shape(method, url) is not None:
        return
    try:
        parsed = urlparse(url)
        shown = f"{parsed.netloc or '?'}{parsed.path or '/'}"
    except ValueError:
        shown = "?"
    raise ValueError(
        f"SmartBid request is not on the harvest allowlist: {(method or '').upper()} {shown}"
    )


# ── httpx log redaction ──────────────────────────────────────────────────────
# httpx logs every request URL at INFO ("HTTP Request: GET <url> ..."). The
# direct-URL lookup carries the security token, the blob GET the SAS
# signature and the click ping the passport key, all in the query string, so
# a filter on the "httpx" logger drops the query of any SmartBid URL before
# a handler sees it. Other hosts' records pass untouched.

_REDACT_HOSTS = (CLICK_HOST, OPEN_HOST, API_HOST, FILE_HOST, "gocc.smartbid.co")


def _redacted(value: Any) -> Any:
    if not isinstance(value, (httpx.URL, str)):
        return value
    text = str(value)
    if "?" not in text or "://" not in text:
        return value
    try:
        parsed = urlparse(text)
    except ValueError:
        return value
    host = (parsed.netloc or "").lower()
    if host in _REDACT_HOSTS or host.endswith(BLOB_SUFFIX):
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?<redacted>"
    return value


class _RedactSmartBidUrls(logging.Filter):
    bdr_smartbid_redact = True

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(_redacted(a) for a in args)
        return True


def _install_log_redaction() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(getattr(f, "bdr_smartbid_redact", False) for f in httpx_logger.filters):
        httpx_logger.addFilter(_RedactSmartBidUrls())


_install_log_redaction()


# ── Pace (section 3.3) ───────────────────────────────────────────────────────

_pace_lock = threading.Lock()
_last_request_at = 0.0


def _pace(min_interval: float, *, sleep: Callable[[float], None], clock: Callable[[], float],
          rng: Callable[[float, float], float]) -> None:
    """Wait until at least `min_interval * U(1, 2)` seconds have passed since
    the previous SmartBid request in this process. The reservation is made
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


# ── Gate and project parsing (pure, sections 3.2 and 3.5) ────────────────────


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _ID_RE.match(value.strip()):
        return int(value.strip())
    return None


def _digits(value: Any) -> str | None:
    number = _int_or_none(value)
    return str(number) if number is not None and number > 0 else None


def _text(value: Any) -> str | None:
    """A single-line string: whitespace collapsed, None when empty."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = " ".join(str(value).replace("\xa0", " ").split())
    return text or None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def is_agreement_answer(answer: Any) -> bool:
    """The `getconfidentialagreement` shape: a dict whose
    `BidProjectConfidentialAgreement` is a dict."""
    return isinstance(answer, dict) and isinstance(answer.get("BidProjectConfidentialAgreement"), dict)


def agreement_refusal(answer: dict) -> str | None:
    """The sentence a `getconfidentialagreement` answer refuses the harvest
    with, or None when the project is open to it (section 3.2): any
    `AlowedDetail`, any PAD row, a general agreement or a specific one
    (`ConfidentialityAgreementSCA` 1, or 2 with `subIsInCodeWithSCA`) not
    yet accepted. Fail-safe: a required agreement counts as owed unless a
    person row of its type says accepted (StatusCA 1, 3 or 6)."""
    if not is_agreement_answer(answer):
        return _MSG_DATA
    detail = _text(answer.get("AlowedDetail"))
    if detail:
        detail = re.sub(r"<[^>]+>", " ", html_lib.unescape(detail))
        detail = " ".join(detail.split()).rstrip(".")[:_DETAIL_MAX_CHARS] or "no reason given"
        return _MSG_NOT_ALLOWED.format(detail=detail)
    if answer.get("ConfidentialAgreementPAD"):
        return _MSG_AGREEMENT
    head = answer["BidProjectConfidentialAgreement"]
    people = [p for p in (answer.get("ConfidentialAgreementPerson") or []) if isinstance(p, dict)]

    def accepted(type_ca: int) -> bool:
        return any(
            _int_or_none(p.get("typeCA")) == type_ca
            and _int_or_none(p.get("StatusCA")) in _AGREEMENT_OK_STATES
            for p in people
        )

    if _truthy(head.get("ConfidentialityAgreement")) and not accepted(1):
        return _MSG_AGREEMENT
    sca = _int_or_none(head.get("ConfidentialityAgreementSCA")) or 0
    if sca == 1 or (sca == 2 and _sub_in_sca_code(answer, head, people)):
        if not accepted(2):
            return _MSG_AGREEMENT
    return None


def _sub_in_sca_code(answer: dict, head: dict, people: list[dict]) -> bool:
    """`subIsInCodeWithSCA` wherever the answer carries it; a missing flag
    counts as true (the fail-safe reading: ask a human)."""
    for source in (answer, head, *people):
        if "subIsInCodeWithSCA" in source:
            return _truthy(source.get("subIsInCodeWithSCA"))
    return True


@dataclass
class SmartBidProject:
    bid_project_id: int
    system_id: int | None
    title: str | None
    gc_name: str | None
    manager: str | None
    phone: str | None
    fax: str | None
    address1: str | None
    address2: str | None
    city: str | None
    state: str | None
    zip: str | None
    owner: str | None
    architect: str | None
    project_status: str | None
    description_html: str | None
    bid_due_local: str | None
    bid_due_text: str | None
    time_zone_short: str | None
    past_due: bool
    allow_late_proposal: bool
    pre_bid: dict | None
    invitations: list[dict]
    files: list[dict]

    @property
    def response_recorded(self) -> bool:
        """Any invited code whose status is not "Invited" (a person answered
        in SmartBid). Informational only: never acted on."""
        return any(
            (inv.get("status") or "").strip().lower() not in ("", "invited")
            for inv in self.invitations
        )


def file_value(file_id: Any, bid_project_id: Any, system_id: Any) -> str | None:
    """The `Value` a plan-room file's download link carries: base64 of
    `<FileId>.<BidProjectId>.<SystemId>`. None when a part is not digits."""
    parts = [p for p in (_digits(file_id), _digits(bid_project_id), _digits(system_id)) if p]
    if len(parts) != 3:
        return None
    return base64.b64encode(".".join(parts).encode("ascii")).decode("ascii")


def _href_value(href: str | None) -> str | None:
    if not href:
        return None
    try:
        parsed = urlparse(href)
    except ValueError:
        return None
    values = [v for k, v in (_query_params(parsed.query or "") or []) if k == "Value"]
    return values[0] if len(values) == 1 and values[0] else None


def _local_due(value: Any) -> str | None:
    """`BidDueDate` as the wall-clock `YYYY-MM-DDTHH:MM:SS` it is, or None
    (missing, unreadable, or the .NET zero date)."""
    text = _text(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.year < 1900:
        return None
    return dt.replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def _zone_short(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    text = text.strip("() ").upper()
    return text or None


def _file_entry(node: dict, folders: list[str], restricted: bool) -> dict | None:
    file_id = _digits(node.get("FileId"))
    href = node.get("Href") if isinstance(node.get("Href"), str) else None
    if file_id is None or not href:
        return None
    name = str(node.get("Name") or node.get("FileName") or "").strip() or f"file-{file_id}"
    ext = _text(node.get("FileIcon")) or os.path.splitext(name)[1].lstrip(".") or None
    size = _int_or_none(node.get("Size"))
    return {
        "file_id": file_id,
        "name": name,
        "folder": "/".join(folders),
        "size_kb": size if size is not None and size >= 0 else None,
        "ext": ext.lower() if ext else None,
        "uploaded_on": _text(node.get("UploadedOn")),
        "version": _int_or_none(node.get("Version")),
        "href": href,
        "value": _href_value(href),
        "restricted": restricted,
    }


def _walk(nodes: Any, folders: list[str], restricted: bool, out: list[dict],
          seen: set[str], *, top: bool) -> None:
    for node in nodes if isinstance(nodes, list) else []:
        if len(out) >= _MAX_FILES:
            return
        if not isinstance(node, dict):
            continue
        locked = restricted or _truthy(node.get("SCARequired")) or _truthy(node.get("PQRequired"))
        if _truthy(node.get("isFile")):
            entry = _file_entry(node, folders, locked)
            if entry is not None and entry["file_id"] not in seen:
                seen.add(entry["file_id"])
                out.append(entry)
            continue
        name = str(node.get("Name") or "").strip()
        is_root = top and name.lower() == "root"
        path = folders if (is_root or not name) else [*folders, name]
        _walk(node.get("Folders"), path, locked, out, seen, top=False)
        _walk(node.get("Files"), path, locked, out, seen, top=False)


def _invitations(rows: Any) -> list[dict]:
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out.append({
            "code": _text(row.get("Code")),
            "name": _text(row.get("CodeName")),
            "package": _text(row.get("CodePackageName")),
            "status": _text(row.get("Status")),
            "invited_on": _text(row.get("InvitedOn")),
        })
    return out


def parse_project(data: Any) -> SmartBidProject:
    """The `getbidproject` answer as facts (section 3.5). Pure; never raises
    on an answer of another shape (every field is None or empty then)."""
    payload = data if isinstance(data, dict) else {}
    bp = payload.get("BidProject") if isinstance(payload.get("BidProject"), dict) else {}
    pre_bid_date = _text(bp.get("PreBidMeetingDate"))
    files: list[dict] = []
    seen: set[str] = set()
    _walk(payload.get("PlanRoom"), [], False, files, seen, top=True)
    _walk(payload.get("SetPlanRoom"), [], False, files, seen, top=True)
    description = bp.get("ProjectDescription")
    return SmartBidProject(
        bid_project_id=_int_or_none(bp.get("BidProjectId")) or 0,
        system_id=_int_or_none(bp.get("SystemId")),
        title=_text(bp.get("Title")),
        gc_name=_text(bp.get("SystemName")) or _text(bp.get("GCSystemName")),
        manager=_text(bp.get("Manager")),
        phone=_text(bp.get("Phone")),
        fax=_text(bp.get("Fax")),
        address1=_text(bp.get("Address1")),
        address2=_text(bp.get("Address2")),
        city=_text(bp.get("City")),
        state=_text(bp.get("State")),
        zip=_text(bp.get("Zip")),
        owner=_text(bp.get("Owner")),
        architect=_text(bp.get("Architect")),
        project_status=_text(bp.get("ProjectStatus")),
        description_html=description if isinstance(description, str) and description.strip() else None,
        bid_due_local=_local_due(bp.get("BidDueDate")),
        bid_due_text=_text(bp.get("FullBidDueDate")),
        time_zone_short=_zone_short(bp.get("TimeZoneShort")),
        past_due=_truthy(bp.get("isPastDueDateTime")),
        allow_late_proposal=_truthy(bp.get("AllowLateProposal")),
        pre_bid=(
            {
                "date": pre_bid_date,
                "time_zone": _zone_short(bp.get("PreBidMeetingTimeZone")),
                "mandatory": _truthy(bp.get("IsPreBidMeetingMandatory")),
            }
            if pre_bid_date else None
        ),
        invitations=_invitations(payload.get("BidInvitation")),
        files=files,
    )


def bid_due_zone(time_zone_short: str | None) -> tuple[str, bool]:
    """(IANA zone, assumed) for SmartBid's short zone. Unknown or missing ->
    America/Los_Angeles, assumed. The zone is taken as SmartBid states it,
    never second-guessed (a Las Vegas job on a Central GC system says CT)."""
    zone = _ZONES.get((time_zone_short or "").strip("() ").upper())
    if zone is None:
        return _DEFAULT_ZONE, True
    return zone, False


def bid_due_at(project: SmartBidProject) -> str | None:
    """`bid_due_local` in the zone `time_zone_short` names, as an ISO
    instant; None without a due date."""
    if not project.bid_due_local:
        return None
    try:
        local = datetime.strptime(project.bid_due_local, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    zone, _ = bid_due_zone(project.time_zone_short)
    return local.replace(tzinfo=ZoneInfo(zone)).isoformat()


# ── Session (section 3.2) ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SmartBidConfig:
    min_request_interval: float = 2.0
    login_min_interval: int = 30
    timeout: float = 60.0


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


def _json(resp: httpx.Response) -> Any:
    if len(resp.content) > _MAX_JSON_BYTES:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _timestamp(now: datetime) -> str:
    """JS `toISOString()`: UTC, milliseconds, `Z`."""
    utc = now.astimezone(timezone.utc)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


class SmartBidSession:
    """One harvest's SmartBid session over a single httpx client: the bearer
    token from the email's passport key, held in memory only.

    `transport`, `sleep`, `clock`, `rng` and `now` are test seams. `on_lock`
    is called once as `(until, error, None)` when SmartBid logins lock."""

    provider = PROVIDER

    def __init__(
        self,
        config: SmartBidConfig,
        store: SessionStore,
        ref: SmartBidRef,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = _now,
        on_lock: Callable[[datetime, str, str | None], None] | None = None,
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
        self._token: str | None = None
        self.account_id: str | None = None
        self.token_system_id: str | None = None
        self.project_system_id: str | None = None
        self._last_login_attempt: datetime | None = None
        self.logged_in_this_session = False

    def __repr__(self) -> str:
        return f"SmartBidSession(bid_project_id={self.ref.bid_project_id!r})"

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._token = None
        self._client.close()

    def __enter__(self) -> "SmartBidSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- requests -------------------------------------------------------------

    def _request(self, method: str, url: str, *, transient: str | None = None,
                 **kwargs: Any) -> httpx.Response:
        assert_allowed(method, url)
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise SmartBidTransient(f"SmartBid did not answer ({type(exc).__name__}).") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            message = transient or "SmartBid answered {status}; the harvest will be retried."
            raise SmartBidTransient(message.format(status=resp.status_code))
        return resp

    def _api_headers(self) -> dict[str, str]:
        if not self._token:
            raise SmartBidSessionExpired()
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": _JSON_ACCEPT,
            "Origin": SPA_ORIGIN,
            "Referer": SPA_REFERER,
        }

    def _api_get(self, path: str, params: dict[str, str]) -> httpx.Response:
        headers = self._api_headers()
        return self._request("GET", f"{API_BASE}{path}?{urlencode(params)}", headers=headers)

    # -- login ----------------------------------------------------------------

    def availability(self) -> tuple[bool, str | None, datetime | None]:
        """(usable, reason, locked_until) without touching SmartBid."""
        state = self.store.load()
        until = lock_state(state, self._now())
        if until:
            return False, LOCKED_MESSAGE, until
        return True, None, None

    def login(self) -> None:
        """Trade the email's passport key for a bearer token. Refused while
        logins are locked or within `login_min_interval` of the last
        attempt. A rejected link (`invalid_grant`), a 429 / 5xx and network
        trouble are NOT counted toward the lock (the key is per email, and a
        dead link answers 500); any other unexpected answer is."""
        state = self.store.load()
        now = self._now()
        until = lock_state(state, now)
        if until:
            raise SmartBidLoginLocked(LOCKED_MESSAGE, locked_until=until)
        stamps = [t for t in (_parse_ts((state or {}).get("last_login_attempt_at")), self._last_login_attempt) if t]
        last = max(stamps) if stamps else None
        if last and (now - last).total_seconds() < self.config.login_min_interval:
            raise SmartBidUnavailable(
                _MSG_RECENT, locked_until=last + timedelta(seconds=self.config.login_min_interval)
            )
        self._last_login_attempt = now
        self._token = None
        try:
            answer = self._token_request()
        except SmartBidLoginFailed as exc:
            state = self.store.record_login(ok=False, error=f"{exc.step}: {exc}")
            until = lock_state(state, self._now())
            logger.warning("SmartBid login failed at %s: %s", exc.step, exc)
            if until:
                if self._on_lock:
                    try:
                        self._on_lock(until, str(exc), None)
                    except Exception:  # noqa: BLE001 - the bell must never mask the lock
                        logger.exception("SmartBid lock notification failed")
                raise SmartBidLoginLocked(LOCKED_MESSAGE, locked_until=until) from exc
            raise
        self._token = answer["access_token"]
        self.account_id = answer["account_id"]
        self.token_system_id = answer.get("bid_project_system_id")
        self.store.record_login(ok=True, error=None)
        self.store.save_cookies(STORE_ACCOUNT, [])
        self.logged_in_this_session = True
        logger.info("SmartBid login succeeded for bid project %s", self.ref.bid_project_id)

    def _token_request(self) -> dict:
        resp = self._request(
            "POST",
            f"{API_BASE}{TOKEN_PATH}",
            transient=_MSG_TOKEN_DOWN,
            data={
                "grant_type": "passport_key",
                "bidid": self.ref.bid_project_id,
                "commdetailid": self.ref.comm_detail_id,
                "key": self.ref.passport_key,
                "typepassportkey": "bidproject_passportkey",
                "bfId": "",
                "isIframe": "false",
            },
            headers={
                "Accept": _JSON_ACCEPT,
                "Origin": SPA_ORIGIN,
                "Referer": SPA_REFERER,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        body = _json(resp)
        if resp.status_code == 400 and isinstance(body, dict) and body.get("error") == "invalid_grant":
            raise SmartBidForbidden(_MSG_LINK_REJECTED)
        if resp.status_code != 200:
            raise SmartBidLoginFailed(
                "token", f"SmartBid answered the login with HTTP {resp.status_code}."
            )
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip() or len(token) > 16384 or any(c.isspace() for c in token):
            raise SmartBidLoginFailed("token", "SmartBid answered the login without an access token.")
        account = _digits(body.get("account_id"))
        if account is None:
            raise SmartBidLoginFailed("token", "SmartBid answered the login without an account id.")
        return {
            "access_token": token,
            "account_id": account,
            "bid_project_system_id": _digits(body.get("bidProjectSystemId")),
        }

    # -- project reads ----------------------------------------------------------

    def gate(self) -> dict:
        """The confidentiality-agreement gate (read only): returns the
        answer when the project is open to the harvest; SmartBidForbidden
        with the sentence `agreement_refusal` gives otherwise. Nothing is
        ever accepted."""
        resp = self._api_get(GATE_PATH, {
            "bidProjectId": self.ref.bid_project_id,
            "personId": self.account_id or "",
            "isCcbc": "false",
        })
        if resp.status_code == 401:
            raise SmartBidSessionExpired()
        if resp.status_code in (403, 404):
            raise SmartBidForbidden(_MSG_NO_PROJECT.format(bid=self.ref.bid_project_id))
        if resp.status_code != 200:
            raise SmartBidUnavailable(
                f"SmartBid answered {resp.status_code} for the agreement check."
            )
        answer = _json(resp)
        if not is_agreement_answer(answer):
            raise SmartBidUnavailable(_MSG_DATA)
        refusal = agreement_refusal(answer)
        if refusal:
            raise SmartBidForbidden(refusal)
        return answer

    def get_project(self) -> dict:
        """The `getbidproject` answer for the reference's project."""
        resp = self._api_get(PROJECT_PATH, {
            "bidProjectId": self.ref.bid_project_id,
            "personId": self.account_id or "",
            "bidProjectType": "Invited",
            "isIframe": "false",
        })
        if resp.status_code == 401:
            raise SmartBidSessionExpired()
        if resp.status_code in (403, 404):
            raise SmartBidForbidden(_MSG_NO_PROJECT.format(bid=self.ref.bid_project_id))
        if resp.status_code != 200:
            raise SmartBidUnavailable(f"SmartBid answered {resp.status_code} for the project.")
        body = _json(resp)
        bp = body.get("BidProject") if isinstance(body, dict) else None
        if not isinstance(bp, dict) or _digits(bp.get("BidProjectId")) != self.ref.bid_project_id:
            raise SmartBidUnavailable(_MSG_DATA)
        self.project_system_id = _digits(bp.get("SystemId")) or self.token_system_id
        self.store.touch()
        return body

    # -- downloads and pings ----------------------------------------------------

    def _make_download_client(self, *, timeout: httpx.Timeout | None = None) -> httpx.Client:
        if self._download_client_factory is not None:
            return self._download_client_factory()
        return httpx.Client(
            timeout=timeout or httpx.Timeout(connect=10, read=120, write=60, pool=30),
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        )

    def _file_value(self, entry: Any) -> str:
        """The entry's `Value`, rebuilt from its FileId, the project id and
        the project's system id, and required to equal the one its Href
        carries (else SmartBidForbidden)."""
        if not isinstance(entry, dict):
            raise SmartBidForbidden(_MSG_FILE_LINK)
        expected = file_value(entry.get("file_id"), self.ref.bid_project_id, self.project_system_id)
        href = entry.get("href") if isinstance(entry.get("href"), str) else ""
        try:
            parsed = urlparse(href)
        except ValueError:
            raise SmartBidForbidden(_MSG_FILE_LINK) from None
        if (
            expected is None
            or (parsed.scheme or "").lower() != "https"
            or (parsed.netloc or "").lower() != FILE_HOST
            or parsed.path != FILE_PATH
            or _href_value(href) != expected
            or entry.get("value") not in (None, expected)
        ):
            raise SmartBidForbidden(_MSG_FILE_LINK)
        return expected

    def _direct_url(self, value: str, file_id: str) -> str:
        """Steps 1 to 3 of section 3.1: the security token, then the
        direct-URL lookup. Returns the blob URL (in memory only)."""
        stamp = _timestamp(self._now())
        headers = {**self._api_headers(), "Content-Type": "application/json; charset=utf-8"}
        resp = self._request(
            "POST", f"{API_BASE}{SECURITY_TOKEN_PATH}",
            content=json.dumps(value + stamp).encode("utf-8"), headers=headers,
        )
        if resp.status_code == 401:
            raise SmartBidSessionExpired()
        if resp.status_code != 200:
            raise SmartBidForbidden(_MSG_FILE_TOKEN)
        token = _json(resp)
        if not isinstance(token, str) or not token or len(token) > 512 or any(c.isspace() for c in token):
            raise SmartBidForbidden(_MSG_FILE_TOKEN)
        # As the SPA builds it: the raw token and timestamp, no extra encoding.
        lookup = f"https://{FILE_HOST}{FILE_PATH}?Value={value}&token={token}&timestamp={stamp}"
        resp = self._request("GET", lookup, headers=self._api_headers())
        if resp.status_code == 401:
            raise SmartBidSessionExpired()
        if resp.status_code in (403, 404):
            raise SmartBidForbidden("SmartBid has no such file (404).")
        if resp.status_code != 200 or len(resp.content) > _MAX_URL_ANSWER_BYTES:
            raise SmartBidForbidden(_MSG_FILE_LOCATION)
        answer = resp.text.strip()
        if len(answer) >= 2 and answer[0] == answer[-1] == '"':
            answer = answer[1:-1]
        try:
            parsed = urlparse(answer)
        except ValueError:
            raise SmartBidForbidden(_MSG_FILE_LOCATION) from None
        match = _BLOB_PATH_RE.match(parsed.path or "")
        if (
            (parsed.scheme or "").lower() != "https"
            or not (parsed.netloc or "").lower().endswith(BLOB_SUFFIX)
            or match is None
            or match.group(1) != self.ref.bid_project_id
            or match.group(2) != file_id
            or _shape("GET", answer) != "blob"
        ):
            raise SmartBidForbidden(_MSG_FILE_LOCATION)
        return answer

    def download(self, entry: Any, dest: Path, *, max_bytes: int) -> int:
        """Stream one plan-room file into `dest` (created O_EXCL): the
        security token and the direct-URL lookup with the bearer token, then
        the blob with a fresh cookie-less, auth-less client, no redirects,
        under a running byte cap. Returns bytes written. SmartBidForbidden
        for a foreign link, a refused or missing file, a page where a file
        was expected or an oversize body (the file is unlinked);
        SmartBidTransient for network trouble or a 5xx; SmartBidSessionExpired
        for a 401 on the token or the lookup."""
        value = self._file_value(entry)
        file_id = str(entry.get("file_id"))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(dest, flags, 0o644)
        written = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                blob_url = self._direct_url(value, file_id)
                assert_allowed("GET", blob_url)
                _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
                with self._make_download_client() as client, client.stream("GET", blob_url) as resp:
                    if resp.status_code in _REDIRECTS:
                        raise SmartBidForbidden(
                            "A SmartBid file location redirected; the harvest does not follow it."
                        )
                    if resp.status_code in (403, 404):
                        raise SmartBidForbidden(
                            f"The SmartBid file store refused the file ({resp.status_code})."
                        )
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise SmartBidTransient(f"The SmartBid file store answered {resp.status_code}.")
                    if resp.status_code != 200:
                        raise SmartBidTransient(f"The SmartBid file store answered {resp.status_code}.")
                    if "text/html" in (resp.headers.get("content-type") or "").lower():
                        raise SmartBidForbidden(
                            "The SmartBid file store answered with a page instead of the file."
                        )
                    for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            raise SmartBidForbidden(
                                "The project file is larger than the harvest accepts."
                            )
                        fh.write(chunk)
                    return written
        except httpx.TransportError as exc:
            _unlink_quietly(dest)
            raise SmartBidTransient(
                f"The SmartBid file store did not answer ({type(exc).__name__})."
            ) from exc
        except BaseException:
            _unlink_quietly(dest)
            raise

    def ping(self, url: str) -> int | None:
        """One GET on the email's own tracking (the read receipt, the open
        pixel or the View the Project link), no redirects followed, 10 s
        timeout, cookie-less. Returns the status code, or None when it did
        not answer. Anything else (an `iR` link above all) is a ValueError."""
        target = _unwrapped(url)
        kind = _shape("GET", target)
        if kind not in ("click", "read_receipt", "open"):
            raise ValueError(
                "Only the SmartBid read receipt, the open pixel and the View the Project "
                "link can be pinged."
            )
        assert_allowed("GET", target)
        _pace(self.config.min_request_interval, sleep=self._sleep, clock=self._clock, rng=self._rng)
        accept = _HTML_ACCEPT if kind == "click" else _IMAGE_ACCEPT
        try:
            with self._make_download_client(timeout=httpx.Timeout(_PING_TIMEOUT)) as client:
                resp = client.get(target, headers={"Accept": accept})
                return resp.status_code
        except httpx.TransportError as exc:
            logger.info("SmartBid tracking did not answer (%s)", type(exc).__name__)
            return None

    # Test seam: a factory for the cookie-less download/ping client.
    _download_client_factory: Callable[[], httpx.Client] | None = None


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
