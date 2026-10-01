"""The NGEM portal client (app/services/ngem_client) driven through
httpx.MockTransport over the scrubbed captures in tests/fixtures_ngem, with an
in-memory session store and the pace's sleep, clock and rng patched so
nothing waits (docs/RFP_NGEM_PORTAL.md sections 3, 7 and 9).

The fake portal reproduces the login chain captured 2026-09-15:

  GET ResponseList.aspx?e=<entry> (no session) -> 302 /Login.aspx -> 302
  /VendorLogin.aspx -> 200 the login form; POST VendorLogin.aspx with every
  served field, the credentials and the exact chkAgree client state -> 302
  the entry URL + procData cookie; then the entry page 200 with the grid.

With a session the entry page answers page 1, the pager postback page 2 (and
page 2 again for pages 3 and 4, a grid that shifted), the event page and the
attachments page answer to their tokens, a stale token bounces to
/BadRequest.aspx, and Extract.aspx streams the file with its
Content-Disposition name.

Pinned: the small parsers over the captured strings (bid numbers incl. the
Henderson one, Pacific datetimes, dates, sizes); the grid parser over the
real pages 1 and 2 (37 items, 4 pages, the pager targets, every column by
header text, a reordered grid, the other grid never read); the event page
(labels incl. the sic cutoff, the scrubbed contact, notes text and sanitized
HTML, the tab strip links); the attachments page (8 rows, names, sizes,
descriptions, links, the zip anchor never a link); the login chain and every
refusal; one login and one retry when a page proves the session gone; the
login interval, the failure counter, the lock, the one bell and
availability; paging as a full postback with the target allowlist and no
filter text; downloads (filename, byte cap, unlink, HTML answer, session
expiry); the pace; the allowlist; and that the password never reaches a log
line, an error message or a stored row.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote_plus
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services import ngem_client as nc
from tests import fixtures_ngem as fx

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
USERNAME = "VENDORUSER"
PASSWORD = "correct horse battery staple"
PDF = b"%PDF-1.7\n" + b"x" * 3000 + b"\n%%EOF\n"
PDF_NAME = "Exhibit D - UNLV VSS Standards & Specs 04292024.pdf"
LA = ZoneInfo("America/Los_Angeles")

BASE = fx.BASE
ENTRY_URL = fx.ENTRY_URL
EVENT_URL = fx.EVENT_URL
ATTACHMENTS_URL = fx.ATTACHMENTS_URL
FILE_URL = fx.DOWNLOAD_URLS[6]           # Exhibit D - UNLV VSS Standards & Specs
AGREE_STATE = (
    '{"text":"I agree to the terms and conditions of using this website","value":"",'
    '"enabled":true,"autoPostBack":true,"commandName":"","commandArgument":"",'
    '"validationGroup":null,"checked":false}'
)
PAGE_2_TARGET = fx.PAGE_TARGETS[2]
ATTACH_PAGE_2_TARGET = f"{fx.ATTACHMENTS_PAGER_PREFIX}ctl06"

_VIEWSTATE_RE = re.compile(r'name="__VIEWSTATE" id="__VIEWSTATE" value="([^"]*)"')


def _viewstate(page: str) -> str:
    m = _VIEWSTATE_RE.search(page)
    assert m, "fixture without a __VIEWSTATE"
    return m.group(1)


LOGIN_PAGE = fx.load(fx.LOGIN_PAGE)
LIST_1 = fx.load(fx.LIST_PAGE_1)
LIST_2 = fx.load(fx.LIST_PAGE_2)
EVENT_PAGE = fx.load(fx.EVENT_PAGE)
ATTACHMENTS_PAGE = fx.load(fx.ATTACHMENTS_PAGE)
BAD_REQUEST_PAGE = fx.load(fx.BAD_REQUEST_PAGE)
ERROR_PAGE = fx.load(fx.ERROR_PAGE)
VIEWSTATES = {_viewstate(LIST_1), _viewstate(LIST_2), _viewstate(ATTACHMENTS_PAGE)}


# ── Page variants built from the captures ────────────────────────────────


def login_page_with(*, message: str | None = None, mfa: bool = False) -> str:
    """The login form re-rendered after a rejected POST (`#divLoginMessage`)
    or with the MFA prompt window opened on load."""
    page = LOGIN_PAGE
    if message is not None:
        page = page.replace(
            '<div class="checkboxAgree">',
            f'<div id="divLoginMessage">{message}</div><div class="checkboxAgree">',
            1,
        )
    if mfa:
        page = page.replace(
            "</form>",
            '<script type="text/javascript">Sys.Application.add_load(function() { tryMfa(); });'
            "</script></form>",
            1,
        )
    return page


def list_page_without_grid() -> str:
    return LIST_1.replace(f'id="{nc.INVITED_GRID_ID}"', 'id="ctl00_mainContent_somethingElse"', 1)


def list_page_2_short_pager() -> str:
    """Page 2 whose pager stopped offering pages 3 and 4."""
    page = LIST_2
    for n in (3, 4):
        page = re.sub(r"<a [^>]*" + re.escape(fx.PAGE_TARGETS[n]) + r"[^>]*>\s*" + str(n) + r"\s*</a>", "", page)
    assert fx.PAGE_TARGETS[3] not in page and fx.PAGE_TARGETS[4] not in page
    return page


_ATTACH_ROW_RE = re.compile(
    r'<tr class="rg(?:Alt)?Row" valign="top" id="ctl00_mainContent_rgBidAttachments_ctl00__\d+">'
    r".*?</tr>",
    re.DOTALL,
)
_ATTACH_INFO_OLD = "<strong>8</strong> items in <strong>1</strong> pages"
_ATTACH_INFO_NEW = "<strong>8</strong> items in <strong>2</strong> pages"
_ATTACH_P1_CURRENT = (
    '<a onclick="return false;" class="rgCurrentPage" href="javascript:__doPostBack('
    "'ctl00$mainContent$rgBidAttachments$ctl00$ctl03$ctl01$ctl05','')\"><span>1</span></a>"
)
_ATTACH_P1_PLAIN = (
    '<a href="javascript:__doPostBack('
    "'ctl00$mainContent$rgBidAttachments$ctl00$ctl03$ctl01$ctl05','')\"><span>1</span></a>"
)
_ATTACH_P2_CURRENT = (
    '<a onclick="return false;" class="rgCurrentPage" href="javascript:__doPostBack('
    "'ctl00$mainContent$rgBidAttachments$ctl00$ctl03$ctl01$ctl06','')\"><span>2</span></a>"
)
_ATTACH_P2_PLAIN = (
    '<a href="javascript:__doPostBack('
    "'ctl00$mainContent$rgBidAttachments$ctl00$ctl03$ctl01$ctl06','')\"><span>2</span></a>"
)


def attachments_pages() -> tuple[str, str]:
    """The captured 8-file grid split into two pages of four."""
    rows = _ATTACH_ROW_RE.findall(ATTACHMENTS_PAGE)
    assert len(rows) == 8 and _ATTACH_P1_CURRENT in ATTACHMENTS_PAGE

    def build(keep: list[str], pager: str) -> str:
        page = ATTACHMENTS_PAGE.replace(_ATTACH_INFO_OLD, _ATTACH_INFO_NEW, 1)
        page = page.replace(_ATTACH_P1_CURRENT, pager, 1)
        for row in rows:
            if row not in keep:
                page = page.replace(row, "", 1)
        return page

    return (
        build(rows[:4], _ATTACH_P1_CURRENT + _ATTACH_P2_PLAIN),
        build(rows[4:], _ATTACH_P1_PLAIN + _ATTACH_P2_CURRENT),
    )


def synthetic_list_page(columns: list[str], rows: list[dict[str, str]]) -> str:
    """A minimal invitations page with the grid's columns in the given order
    (the empty header is the view-link column)."""
    head = "".join(f"<th>{c}</th>" for c in columns)
    body = ""
    for row in rows:
        cells = ""
        for c in columns:
            if c == "":
                cells += f'<td><a id="r_aHrfView" href="{row.get("", "")}">v</a></td>'
            else:
                cells += f"<td>{row.get(c, '')}</td>"
        body += f'<tr class="rgRow">{cells}</tr>'
    return (
        '<html><body><form method="post" action="./ResponseList.aspx?e=list01" id="aspnetForm">'
        '<input type="hidden" name="__EVENTTARGET" value="">'
        '<input type="hidden" name="__VIEWSTATE" value="vs">'
        f'<table class="rgMasterTable" id="{nc.INVITED_GRID_ID}"><thead><tr>{head}</tr></thead>'
        '<tfoot><tr class="rgPager"><td><div class="rgInfoPart"><strong>1</strong> items in '
        "<strong>1</strong> pages</div></td></tr></tfoot>"
        f"<tbody>{body}</tbody></table></form></body></html>"
    )


# ── The fake portal ──────────────────────────────────────────────────────


def _html(status, body, **headers):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


def _redirect(location, **headers):
    return httpx.Response(302, headers={"location": location, **headers})


class FakeNgem:
    """MockTransport handler: the captured login chain, the grid with its
    pager, the event and attachments pages, the file download, with the
    failure modes the tests arm. Records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.sessions: set[str] = set()      # valid procData values
        self.login_seq = 0
        self.entry_mode = "redirect_login"   # redirect_login | redirect_vendor | form: an anonymous GET
        self.login_mode = "ok"               # ok | rejected | echo | error | mfa | offsite | elsewhere | nocookie
        self.login_page_mode = "ok"          # ok | mfa | noform | loop | offsite | 502
        self.list_mode = "ok"                # ok | nogrid | error_redirect | error_page | offsite | 500 | transport
        self.pager_mode = "ok"               # ok | short | expire_once
        self.event_mode = "ok"               # ok | expire_once
        self.attach_mode = "ok"              # ok | two_pages
        self.file_mode = "ok"                # ok | html | 500 | 404 | transport | expire_once | badrequest | offsite | nodisp | star | huge
        self.pdf = PDF
        self._expired_once: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host != nc.HOST:
            return httpx.Response(500, text=f"unexpected host {host}")
        handler = {
            nc.ENTRY_PATH: self._entry,
            nc.LOGIN_PATH: self._login_redirect,
            nc.VENDOR_LOGIN_PATH: self._vendor_login,
            nc.EVENT_PATH: self._event,
            nc.ATTACHMENTS_PATH: self._attachments,
            nc.EXTRACT_PATH: self._extract,
            nc.ERROR_PATH: lambda r: _html(200, ERROR_PAGE),
            nc.BAD_REQUEST_PATH: lambda r: _html(200, BAD_REQUEST_PAGE),
        }.get(path)
        if handler is None:
            return httpx.Response(404, text="nope")
        return handler(request)

    @property
    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def _cookies(self, request: httpx.Request) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in (request.headers.get("cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _has_session(self, request: httpx.Request) -> bool:
        return self._cookies(request).get("procData") in self.sessions

    @staticmethod
    def form(request: httpx.Request) -> dict[str, str]:
        return {k: v[-1] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}

    def _expire_once(self, key: str) -> bool:
        if key in self._expired_once:
            return False
        self._expired_once.add(key)
        return True

    def _anonymous(self) -> httpx.Response:
        if self.entry_mode == "redirect_vendor":
            return _redirect(f"{BASE}{nc.VENDOR_LOGIN_PATH}")
        if self.entry_mode == "form":
            return _html(200, LOGIN_PAGE)
        return _redirect(nc.LOGIN_PATH)

    # -- ResponseList.aspx -----------------------------------------------

    def _entry(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return self._pager(request)
        if not self._has_session(request):
            return self._anonymous()
        if self.list_mode == "nogrid":
            return _html(200, list_page_without_grid())
        if self.list_mode == "error_redirect":
            return _redirect(nc.ERROR_PATH)
        if self.list_mode == "error_page":
            return _html(200, ERROR_PAGE)
        if self.list_mode == "offsite":
            return _redirect("https://evil.example.com/list")
        if self.list_mode == "http":
            return _redirect(f"http://{nc.HOST}{nc.ENTRY_PATH}?e=downgraded")
        if self.list_mode == "huge":
            return _html(200, LIST_1 + ("<!-- " + "x" * 1024 * 1024 + " -->") * 5)
        if self.list_mode == "500":
            return httpx.Response(503, text="down")
        if self.list_mode == "transport":
            raise httpx.ConnectError("no route", request=request)
        return _html(200, LIST_1, **{"set-cookie": "__AntiXsrfToken=x; path=/; HttpOnly"})

    def _pager(self, request: httpx.Request) -> httpx.Response:
        if not self._has_session(request):
            return _redirect(nc.LOGIN_PATH)
        form = self.form(request)
        if (
            form.get("__VIEWSTATE") not in VIEWSTATES
            or form.get("__EVENTARGUMENT") != ""
            or "__ASYNCPOST" in form
            or any(v for k, v in form.items() if "FilterTextBox_" in k)
        ):
            return _redirect(nc.ERROR_PATH)
        target = form.get("__EVENTTARGET")
        page = next((n for n, t in fx.PAGE_TARGETS.items() if t == target), None)
        if page is None:
            return _redirect(nc.ERROR_PATH)
        if self.pager_mode == "expire_once" and self._expire_once(f"page{page}"):
            return _redirect(nc.LOGIN_PATH)
        if page == 1:
            return _html(200, LIST_1)
        if self.pager_mode == "short":
            return _html(200, list_page_2_short_pager())
        return _html(200, LIST_2)      # pages 3 and 4 answer page 2 again: a shifted grid

    # -- the login chain -------------------------------------------------

    def _login_redirect(self, request: httpx.Request) -> httpx.Response:
        return _redirect("/VendorLogin.aspx")

    def _vendor_login(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if self.login_page_mode == "mfa":
                return _html(200, login_page_with(mfa=True))
            if self.login_page_mode == "noform":
                return _html(200, "<html><body>Maintenance</body></html>")
            if self.login_page_mode == "loop":
                return _redirect("/Login.aspx")
            if self.login_page_mode == "offsite":
                return _redirect("https://evil.example.com/login")
            if self.login_page_mode == "http":
                return _redirect(f"http://{nc.HOST}{nc.VENDOR_LOGIN_PATH}")
            if self.login_page_mode == "http_action":
                return _html(200, LOGIN_PAGE.replace('action="./VendorLogin.aspx"',
                                                     f'action="http://{nc.HOST}/VendorLogin.aspx"'))
            if self.login_page_mode == "502":
                return httpx.Response(502, text="bad gateway")
            return _html(200, LOGIN_PAGE, **{"set-cookie": "ASP.NET_SessionId=asp-1; path=/; HttpOnly"})
        form = self.form(request)
        if form.get("__VIEWSTATE") != _viewstate(LOGIN_PAGE) or "btnLogin" not in form:
            return _redirect(nc.ERROR_PATH)
        if form.get("chkAgree_ClientState") != AGREE_STATE:
            return _redirect(nc.ERROR_PATH)      # captured: a partial JSON answers /Error.aspx
        if any(k in form for k in ("hdnBtnLogin", "hdnBtnMfa", "btnForgotPassword")):
            return _redirect(nc.ERROR_PATH)
        mode = self.login_mode
        if mode == "ok" and (form.get("txtUserName") != USERNAME or form.get("txtPassword") != PASSWORD):
            mode = "rejected"
        if mode == "rejected":
            return _html(200, login_page_with(message="Invalid user name or password."))
        if mode == "echo":
            return _html(200, login_page_with(message=f"The password {form.get('txtPassword')} is wrong."))
        if mode == "error":
            return _redirect(nc.ERROR_PATH)
        if mode == "mfa":
            return _html(200, login_page_with(mfa=True))
        if mode == "offsite":
            return _redirect("https://evil.example.com/callback")
        if mode == "http":
            return _redirect(f"http://{nc.HOST}{nc.ENTRY_PATH}?e=downgraded")
        if mode == "elsewhere":
            return _redirect(f"{BASE}/VendorAdmin/Profile.aspx")
        if mode == "nocookie":
            return _redirect(ENTRY_URL)
        self.login_seq += 1
        sid = f"proc-{self.login_seq}"
        self.sessions.add(sid)
        return _redirect(ENTRY_URL, **{"set-cookie": f"procData={sid}; path=/; secure; HttpOnly"})

    # -- the bid pages ---------------------------------------------------

    def _event(self, request: httpx.Request) -> httpx.Response:
        if not self._has_session(request):
            return self._anonymous()
        if self.event_mode == "expire_once" and self._expire_once("event"):
            return _redirect(nc.LOGIN_PATH)
        if request.url.params.get("e") != fx.EVENT_TOKEN:
            return _redirect(nc.BAD_REQUEST_PATH)
        return _html(200, EVENT_PAGE)

    def _attachments(self, request: httpx.Request) -> httpx.Response:
        if not self._has_session(request):
            return self._anonymous()
        if request.url.params.get("e") != fx.EVENT_TAB_TOKEN:
            return _redirect(nc.BAD_REQUEST_PATH)
        page_one, page_two = attachments_pages() if self.attach_mode == "two_pages" else (ATTACHMENTS_PAGE, None)
        if request.method == "GET":
            return _html(200, page_one)
        form = self.form(request)
        if form.get("__VIEWSTATE") not in VIEWSTATES or form.get("ctl00$mainContent$hfDocZipAction"):
            return _redirect(nc.ERROR_PATH)
        if form.get("__EVENTTARGET") == ATTACH_PAGE_2_TARGET and page_two is not None:
            return _html(200, page_two)
        if form.get("__EVENTTARGET") == f"{fx.ATTACHMENTS_PAGER_PREFIX}ctl05":
            return _html(200, page_one)
        return _redirect(nc.ERROR_PATH)

    # -- Extract.aspx ----------------------------------------------------

    def _extract(self, request: httpx.Request) -> httpx.Response:
        if not self._has_session(request):
            return _redirect(nc.LOGIN_PATH)
        if self.file_mode == "expire_once" and self._expire_once("file"):
            return _redirect(nc.LOGIN_PATH)
        if self.file_mode == "transport":
            raise httpx.ReadTimeout("slow", request=request)
        if self.file_mode == "500":
            return httpx.Response(503, text="slow down")
        if self.file_mode == "404":
            return httpx.Response(404, text="gone")
        if self.file_mode == "html":
            return _html(200, LIST_1)
        if self.file_mode == "badrequest":
            return _redirect(nc.BAD_REQUEST_PATH)
        if self.file_mode == "offsite":
            return _redirect("https://cdn.example.com/file.pdf")
        if self.file_mode == "http":
            return _redirect(f"http://{nc.HOST}{nc.EXTRACT_PATH}?e=file01")
        if request.url.params.get("e") not in fx.FILE_TOKENS:
            return _redirect(nc.BAD_REQUEST_PATH)
        headers = {"content-type": "application/pdf"}
        if self.file_mode == "star":
            headers["content-disposition"] = "attachment; filename*=UTF-8''Plan%20%C3%A9t%C3%A9.pdf"
        elif self.file_mode != "nodisp":
            headers["content-disposition"] = f'attachment;filename="{PDF_NAME}"'
        if self.file_mode == "huge":
            headers["content-length"] = str(10**9)
        return httpx.Response(200, content=self.pdf, headers=headers)


class Clock:
    """A monotonic clock that only advances when the (patched) sleep runs."""

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture(autouse=True)
def _fresh_pace(monkeypatch):
    monkeypatch.setattr(nc, "_last_request_at", 0.0)


@pytest.fixture
def fake():
    return FakeNgem()


@pytest.fixture
def clock():
    return Clock()


def _store(**state):
    store = nc.MemorySessionStore(max_failures=3, lock_seconds=21600, now=lambda: NOW)
    store.state = dict(state)
    return store


def _config(**over):
    base = dict(
        username=USERNAME, password=PASSWORD, entry_url=ENTRY_URL,
        min_request_interval=0.0, login_min_interval=600,
    )
    base.update(over)
    return nc.NgemConfig(**base)


def _session(fake, clock, store=None, config=None, **kw):
    return nc.NgemSession(
        config or _config(),
        store if store is not None else _store(),
        transport=httpx.MockTransport(fake),
        sleep=clock.sleep,
        clock=clock,
        rng=lambda a, b: 1.0,
        now=lambda: NOW,
        **kw,
    )


def _logged_in(fake, clock, **kw):
    fake.sessions.add("stored")
    store = _store(
        account=USERNAME,
        cookies=[{"name": "procData", "value": "stored", "domain": nc.HOST, "path": "/"}],
        logged_in_at=NOW.isoformat(),
    )
    return _session(fake, clock, store, **kw)


# ── Small parsers (pure) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("607992-26 Addendum 2", ("607992-26", 2)),
        ("1790", ("1790", None)),
        ("27300209", ("27300209", None)),
        (
            "IFB 112-27 Fire Station 95 Interior Renovation Addendum 1",
            ("IFB 112-27 Fire Station 95 Interior Renovation", 1),
        ),
        ("26.MWA267.C2-DG Addendum 1", ("26.MWA267.C2-DG", 1)),
        ("607991-26 ADDENDUM 9", ("607991-26", 9)),
        ("607992-26  addendum  12 ", ("607992-26", 12)),
        ("607992-26 Addendum 2.", ("607992-26", 2)),
        ("PWP-WA-2026-493\tAddendum 4", ("PWP-WA-2026-493", 4)),
        ("Addendum 3", ("Addendum 3", None)),
        ("Bid Addendum", ("Bid Addendum", None)),
        ("", ("", None)),
        (None, ("", None)),
    ],
)
def test_parse_bid_number_strips_a_trailing_addendum(raw, expected):
    assert nc.parse_bid_number(raw) == expected


def test_parse_pt_datetime_reads_both_captured_forms_as_pacific():
    close = nc.parse_pt_datetime("9/17/2026 02:00 PM (PT)")
    assert close == datetime(2026, 9, 17, 14, 0, tzinfo=LA)
    assert close.tzinfo is LA and close.utcoffset() == timedelta(hours=-7)
    issued = nc.parse_pt_datetime("8/17/2026 08:00:01 AM (PT)")
    assert issued == datetime(2026, 8, 17, 8, 0, 1, tzinfo=LA)
    assert nc.parse_pt_datetime("12/1/2026 12:15 AM (PT)") == datetime(2026, 12, 1, 0, 15, tzinfo=LA)
    assert nc.parse_pt_datetime("12/1/2026 12:15 PM (PST)") == datetime(2026, 12, 1, 12, 15, tzinfo=LA)
    assert nc.parse_pt_datetime("  9/17/2026 02:00 PM  ") == datetime(2026, 9, 17, 14, 0, tzinfo=LA)
    assert nc.parse_pt_datetime("9/17/2026 14:00") == datetime(2026, 9, 17, 14, 0, tzinfo=LA)
    mountain = nc.parse_pt_datetime("9/17/2026 02:00 PM (MT)")
    assert mountain.tzinfo == ZoneInfo("America/Denver")
    for junk in (None, "", "TBD", "9/17/2026", "13/45/2026 02:00 PM (PT)", "9/17/2026 13:00 PM (PT)",
                 "9/17/2026 02:00 PM (XX)", "2026-09-17T14:00:00"):
        assert nc.parse_pt_datetime(junk) is None


def test_parse_date_reads_the_grid_dates():
    assert nc.parse_date("8/25/2026") == date(2026, 8, 25)
    assert nc.parse_date(" 06/30/2026 ") == date(2026, 6, 30)
    assert nc.parse_date("9/3/26") == date(2026, 9, 3)
    for junk in (None, "", "2/30/2026", "8/25/2026 02:00 PM (PT)", "August 25", "2026-08-25"):
        assert nc.parse_date(junk) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("(286 KB)", 292864),
        ("(1.60 MB)", 1677722),
        ("(29.44 MB)", 30870077),
        ("(14.48 MB)", 15183380),
        ("(2 GB)", 2 * 1024**3),
        ("(512 bytes)", 512),
        ("(1,024 KB)", 1048576),
        ("286\xa0KB", 292864),
        ("", None),
        (None, None),
        ("(large)", None),
        ("(12 TB)", None),
        ("(KB)", None),
    ],
)
def test_parse_size_uses_binary_units(text, expected):
    assert nc.parse_size(text) == expected


def test_split_file_cell_separates_the_name_from_the_size():
    assert nc.split_file_cell("Addendum 1 to 5584-GS.pdf (286\xa0KB)") == ("Addendum 1 to 5584-GS.pdf", 292864)
    assert nc.split_file_cell("Plan (rev 2).pdf (1.60 MB)") == ("Plan (rev 2).pdf", 1677722)
    assert nc.split_file_cell("Plan (rev 2).pdf") == ("Plan (rev 2).pdf", None)
    assert nc.split_file_cell("  ") == ("", None)


# ── The invitations grid ─────────────────────────────────────────────────


def test_parse_invitations_page_1_reads_the_pager_and_the_form():
    grid = nc.parse_invitations_page(LIST_1)
    assert (grid.total_items, grid.total_pages, grid.current_page) == (37, 4, 1)
    assert grid.pager_targets == fx.PAGE_TARGETS
    assert grid.form_action == ENTRY_URL
    fields = grid.form_fields
    # Every non-button input of the served form, in document order.
    assert list(fields)[:6] == [
        "ctl00_ScriptManager_TSM", "__EVENTTARGET", "__EVENTARGUMENT",
        "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED",
    ]
    assert list(fields)[-3:] == ["ctl00$hdnCompanyID", "ctl00$hdnParentForm", "ctl00$hdnVendorID"]
    assert len(fields) == 43
    assert fields["__VIEWSTATEGENERATOR"] == "75C38BE9" and len(fields["__VIEWSTATE"]) > 10_000
    # The combobox inputs and client states exactly as served, the filter boxes empty.
    prefix = "ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00"
    assert fields[f"{prefix}$ctl02$ctl02$rcbAgencyName"] == "All"
    assert fields[f"{prefix}$ctl03$ctl01$PageSizeComboBox"] == "10"
    assert fields["ctl00_mainContent_rtsListTypes_ClientState"].startswith('{"selectedIndexes":')
    filters = [k for k in fields if "FilterTextBox_" in k]
    assert len(filters) == 8 and all(fields[k] == "" for k in filters)
    # No pager buttons, no filter buttons, no submit inputs.
    assert not any(k.endswith(("$ctl02", "$ctl03", "$ctl10", "$ctl11")) for k in fields)
    assert not any("Filter_col" in k for k in fields)


def test_parse_invitations_page_1_rows_by_header():
    rows = nc.parse_invitations_page(LIST_1).rows
    assert len(rows) == 10
    first = rows[0]
    assert first == nc.InvitationRow(
        agency="Clark County, Nevada",
        bid_number_raw="607992-26 Addendum 2",
        bid_number="607992-26",
        addendum_no=2,
        title="SLOAN ROAD - DECATUR BOULEVARD TO LAS VEGAS BOULEVARD",
        issued_on=date(2026, 8, 25),
        close_at=datetime(2026, 9, 15, 14, 15, tzinfo=LA),
        time_left="34 Mins",
        bid_status="Issued",
        response_status="No Response",
        response_code=None,
        status_code="OPEN",
        view_url=f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx?e=event01",
    )
    unlv = rows[2]
    assert unlv.agency == "UNLV (NSHE-Business Center South)"
    assert (unlv.bid_number_raw, unlv.bid_number, unlv.addendum_no) == ("5584-GS Addendum 1", "5584-GS", 1)
    assert unlv.title == "Emergency Phone Tower Installations and Upgrades"
    assert unlv.close_at == datetime(2026, 9, 17, 14, 0, tzinfo=LA)
    assert (unlv.response_status, unlv.response_code) == ("Viewed", "VIEW")
    assert unlv.view_url == EVENT_URL
    assert [r.view_url.rsplit("=", 1)[1] for r in rows] == [f"event{n:02d}" for n in range(1, 11)]
    assert rows[8].title == "Robert “Bob” Price Recreation Center Storage Expansion"
    assert rows[1].response_code == "EDIT" and rows[1].time_left == "1 Day"


def test_parse_invitations_page_2_rows():
    grid = nc.parse_invitations_page(LIST_2)
    assert (grid.total_items, grid.total_pages, grid.current_page) == (37, 4, 2)
    assert grid.pager_targets == fx.PAGE_TARGETS
    assert [r.agency for r in grid.rows] == [
        "Carson City, Nevada",
        "Truckee Meadows Water Authority",
        "Clark County, Nevada",
        "City of North Las Vegas",
        "City of Henderson",
        "Las Vegas Convention & Visitors Authority",
        "Las Vegas Valley Water District",
        "Las Vegas Valley Water District",
        "Las Vegas Convention & Visitors Authority",
        "City of Las Vegas, Nevada",
    ]
    assert [(r.bid_number, r.addendum_no) for r in grid.rows] == [
        ("27300209", None),
        ("PWP-WA-2026-507", 1),
        ("607991-26", 9),
        ("1738", None),
        ("IFB 112-27 Fire Station 95 Interior Renovation", 1),
        ("100025", None),
        ("010277", None),
        ("015483", None),
        ("27-0909", None),
        ("26.MWA267.C2-DG", 1),
    ]
    henderson = grid.rows[4]
    assert henderson.bid_number_raw == "IFB 112-27 Fire Station 95 Interior Renovation Addendum 1"
    assert henderson.title == "IFB 112-27 Fire Station 95 Interior Renovation"
    assert henderson.close_at == datetime(2026, 9, 28, 11, 0, tzinfo=LA)
    assert [r.view_url.rsplit("=", 1)[1] for r in grid.rows] == [f"event{n}" for n in range(21, 31)]


def test_parse_invitations_never_reads_the_other_bid_opportunities_grid():
    """Both list pages carry a second grid (ucNotInvitedListGrid, 24 items in
    3 pages, links event11..event20 / event31..event40); none of it leaks."""
    for page in (LIST_1, LIST_2):
        assert "ucNotInvitedListGrid" in page and "<strong>24</strong> items in" in page
        grid = nc.parse_invitations_page(page)
        assert grid.total_items == 37 and grid.total_pages == 4
        assert all("ucInvitedListGrid" in t for t in grid.pager_targets.values())
    tokens = {r.view_url.rsplit("=", 1)[1] for r in nc.parse_invitations_page(LIST_1).rows}
    assert not tokens & {f"event{n}" for n in range(11, 21)}


def test_parse_invitations_maps_columns_by_header_text_not_position():
    columns = ["Close Date", "Title", "", "Agency", "Status Code", "Bid Number", "Issue Date",
               "Response Status", "Response Status Code", "Time Left", "Bid Status"]
    row = {
        "": EVENT_URL, "Agency": "City of Henderson", "Bid Number": "IFB 1 Addendum 2",
        "Title": "Fire Station", "Issue Date": "9/10/2026", "Close Date": "9/28/2026 11:00 AM (PT)",
        "Time Left": "12 Days", "Bid Status": "Issued", "Response Status": "No Response",
        "Response Status Code": "", "Status Code": "OPEN",
    }
    grid = nc.parse_invitations_page(synthetic_list_page(columns, [row]))
    assert grid.rows == [
        nc.InvitationRow(
            agency="City of Henderson", bid_number_raw="IFB 1 Addendum 2", bid_number="IFB 1",
            addendum_no=2, title="Fire Station", issued_on=date(2026, 9, 10),
            close_at=datetime(2026, 9, 28, 11, 0, tzinfo=LA), time_left="12 Days",
            bid_status="Issued", response_status="No Response", response_code=None,
            status_code="OPEN", view_url=EVENT_URL,
        )
    ]
    assert grid.total_items == 1 and grid.total_pages == 1 and grid.pager_targets == {}
    # Optional columns may go missing; the three that name a bid may not.
    fewer = nc.parse_invitations_page(synthetic_list_page(["Agency", "Bid Number", "Title"], [row]))
    assert fewer.rows[0].close_at is None and fewer.rows[0].view_url is None
    with pytest.raises(nc.NgemParseError, match="missing column: bid number"):
        nc.parse_invitations_page(synthetic_list_page(["Agency", "Title"], [row]))


def test_parse_invitations_requires_the_invited_grid():
    with pytest.raises(nc.NgemParseError, match="invitations grid"):
        nc.parse_invitations_page(list_page_without_grid())
    with pytest.raises(nc.NgemParseError):
        nc.parse_invitations_page(LOGIN_PAGE)
    with pytest.raises(nc.NgemParseError):
        nc.parse_invitations_page("<html></html>")


def test_parse_invitations_accepts_links_only_to_the_event_page():
    columns = ["", "Agency", "Bid Number", "Title"]
    for href in (
        f"{BASE}/VendorResponse/Bid/VResponseSubmission.aspx?e=x",
        "https://evil.example.com/VendorResponse/Bid/VResponseEvent.aspx?e=x",
        f"http://{nc.HOST}/VendorResponse/Bid/VResponseEvent.aspx?e=x",
        f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx?e=x&other=1",
        f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx",
    ):
        page = synthetic_list_page(columns, [{"": href, "Agency": "A", "Bid Number": "1", "Title": "T"}])
        assert nc.parse_invitations_page(page).rows[0].view_url is None
    relative = "Bid/VResponseEvent.aspx?e=rel1"
    page = synthetic_list_page(columns, [{"": relative, "Agency": "A", "Bid Number": "1", "Title": "T"}])
    assert nc.parse_invitations_page(page).rows[0].view_url == f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx?e=rel1"


# ── The event page ───────────────────────────────────────────────────────


def test_parse_event_page_reads_labels_notes_contact_and_tab_links():
    facts = nc.parse_event_page(EVENT_PAGE)
    assert facts.bid_number_raw == "5584-GS Addendum 1"
    assert (facts.bid_number, facts.addendum_no) == ("5584-GS", 1)
    assert facts.title == "Emergency Phone Tower Installations and Upgrades"
    assert facts.bid_type == "Invitation for Bid" and facts.status == "Issued"
    assert facts.issued_at == datetime(2026, 8, 17, 8, 0, 1, tzinfo=LA)
    assert facts.close_at == datetime(2026, 9, 17, 14, 0, 0, tzinfo=LA)
    # The label is misspelled on the portal ("Question Cuttoff Date & Time").
    assert "Question Cuttoff Date &amp; Time" in EVENT_PAGE
    assert facts.question_cutoff_at == datetime(2026, 9, 3, 14, 0, 0, tzinfo=LA)
    assert facts.contact == {
        "workgroup": "UNLV (NSHE-Business Center South)",
        "name": "Pat Example Purchasing Analyst",
        "address": "Las Vegas, NV 89154-1033 USA",
        "phone": "(702) 555-0100",
        "email": "contact@example.gov",
    }
    assert facts.attachments_url == ATTACHMENTS_URL
    assert facts.event_url == f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx?e={fx.EVENT_TAB_TOKEN}"
    # Notes: text through html_to_text, HTML through the nh3 allowlist.
    assert facts.notes_text.startswith("PLEASE NOTE: FOR PUBLIC WORKS PROJECTS, A BIDDER MUST BE QUALIFIED")
    assert "Two (2) MANDATORY Pre-Bid Conference and Site Walks" in facts.notes_text
    assert "\n" in facts.notes_text and "<" not in facts.notes_text
    html = facts.notes_html
    assert html.startswith("<p><strong>PLEASE NOTE:")
    assert "<strong>MANDATORY</strong>" in html
    assert '<a href="https://www.unlv.edu/maps/csb" rel="noopener noreferrer">CSB Map</a>' in html
    for forbidden in ("<span", "<div", "style=", "<script", "text-align"):
        assert forbidden not in html
    assert len(html) < 2000 and len(facts.notes_text) < 1500


def test_parse_event_page_scopes_labels_to_their_tables():
    """The page header repeats the description and short dates in divs; the
    parser reads the Bid Information table's labels, nothing else."""
    assert 'id="ctl00_mainContent_ctlTabStrip_lblCloseDate">9/17/26 @ 2:00pm' in EVENT_PAGE
    facts = nc.parse_event_page(EVENT_PAGE)
    assert facts.close_at.second == 0 and facts.issued_at.second == 1
    with pytest.raises(nc.NgemParseError, match="Bid Information"):
        nc.parse_event_page(LIST_1)
    with pytest.raises(nc.NgemParseError):
        nc.parse_event_page("<html><body>nothing</body></html>")


def _event_page_with_notes(notes_html: str) -> str:
    start = EVENT_PAGE.index('<span id="ctl00_mainContent_lblNotes">') + len('<span id="ctl00_mainContent_lblNotes">')
    end = EVENT_PAGE.index("</span>", EVENT_PAGE.index('<a href="http://https://www.unlv.edu/parking">', start))
    return EVENT_PAGE[:start] + notes_html + EVENT_PAGE[end:]


def test_event_notes_are_sanitized_and_capped():
    hostile = (
        '<p onclick="x()">Site walk <b>Monday</b><script>steal()</script>'
        '<a href="javascript:alert(1)">bad</a> <a href="mailto:a@b.c">mail</a> '
        '<a href="https://example.gov/plans" target="_blank">plans</a>'
        '<img src="x" onerror="y()"><iframe src="z"></iframe><u>u</u><em>e</em></p>'
        "<ul><li>one</li></ul><ol><li>two</li></ol><br><i>i</i>"
    )
    facts = nc.parse_event_page(_event_page_with_notes(hostile))
    html = facts.notes_html
    assert "steal" not in html and "javascript:" not in html and "onclick" not in html
    assert "<img" not in html and "<iframe" not in html and "mailto" not in html
    # https only, the same allowlist the service keeps: an http link loses its href.
    plain = nc.sanitize_notes_html('<a href="http://example.gov/x">http</a> <a href="https://example.gov/y">https</a>')
    assert plain == '<a rel="noopener noreferrer">http</a> <a href="https://example.gov/y" rel="noopener noreferrer">https</a>'
    assert nc._NOTES_SCHEMES == {"https"}
    assert '<a href="https://example.gov/plans" rel="noopener noreferrer">plans</a>' in html
    assert "<a rel=\"noopener noreferrer\">bad</a>" in html
    for tag in ("<b>", "<u>", "<em>", "<ul>", "<li>", "<ol>", "<br>", "<i>"):
        assert tag in html
    # The text side is rfp_harvest.html_to_text: tags gone, blocks on their own lines.
    assert facts.notes_text == "Site walk Mondaybad mail plansue\none\ntwo\n\ni"
    assert "steal" not in facts.notes_text and "<" not in facts.notes_text
    # Caps: 40,000 characters of HTML, 20,000 of text, tags left balanced.
    long = "<p>" + "<b>word</b> " * 20_000 + "</p>"
    facts = nc.parse_event_page(_event_page_with_notes(long))
    assert len(facts.notes_html) <= nc._NOTES_HTML_MAX
    assert facts.notes_html.endswith("</p>") and facts.notes_html.count("<b>") == facts.notes_html.count("</b>")
    assert len(facts.notes_text) == nc._NOTES_TEXT_MAX
    # No notes at all.
    facts = nc.parse_event_page(_event_page_with_notes("   "))
    assert facts.notes_html is None and facts.notes_text is None


# ── The attachments page ─────────────────────────────────────────────────


def test_parse_attachments_page_reads_the_eight_files():
    parsed = nc.parse_attachments_page(ATTACHMENTS_PAGE)
    assert (parsed.total_items, parsed.total_pages, parsed.current_page) == (8, 1, 1)
    assert parsed.pager_targets == {1: f"{fx.ATTACHMENTS_PAGER_PREFIX}ctl05"}
    assert parsed.form_action == ATTACHMENTS_URL
    assert [(r.index, r.file_name, r.size_bytes) for r in parsed.rows] == [
        (1, "Addendum 1 to 5584-GS.pdf", 292864),
        (2, "5584-GS Bid Document.docx.pdf", 1677722),
        (3, "Attachment 9 - Apprentice Request Form.pdf", 272384),
        (4, "Exhibit A - Form Contract.pdf", 524288),
        (5, "Exhibit B Drawings - Bid Set 2026-07-14 - KDP Signed.pdf", 30870077),
        (6, "Exhibit C Specifications - Bid Set 2026-07-14 - KDP Signed.pdf", 1656750),
        (7, "Exhibit D - UNLV VSS Standards & Specs 04292024.pdf", 133120),
        (8, "Exhibit D - UNLV BA Emergency Phone Standards v3.0.pdf", 15183380),
    ]
    assert [r.description for r in parsed.rows] == [
        "Addendum 1 - Site walk sign in sheets",
        "Bid Documents",
        "Attachment 9 - Apprentice Request Form",
        "Exhibit A - Form Contract",
        "Exhibit B Drawings - Bid Set 2026-07-14 - KDP Signed",
        "Exhibit C Specifications - Bid Set 2026-07-14 - KDP Signed",
        "Exhibit D - UNLV VSS Standards & Specs 04292024",
        "Exhibit D - UNLV BA Emergency Phone Standards v3.0",
    ]
    assert [r.download_url for r in parsed.rows] == list(fx.DOWNLOAD_URLS)
    # The "Download All" header anchor (hfDocZipAction + __doPostBack('',''))
    # is never a link; the zip field is carried empty.
    assert "hfDocZipAction').value=" in ATTACHMENTS_PAGE
    assert parsed.form_fields["ctl00$mainContent$hfDocZipAction"] == ""
    assert not any("javascript" in (r.download_url or "") for r in parsed.rows)
    assert "ctl00$mainContent$ctlTabStrip$btnHiddenRetract" not in parsed.form_fields
    assert not any(k.endswith(("$ctl02", "$ctl03", "$ctl08", "$ctl09")) for k in parsed.form_fields)
    assert parsed.form_fields["ctl00$mainContent$rgBidAttachments$ctl00$ctl03$ctl01$PageSizeComboBox"] == "30"


def test_parse_attachments_page_two_page_variant_and_refusals():
    one, two = attachments_pages()
    first = nc.parse_attachments_page(one)
    assert (first.total_pages, first.current_page) == (2, 1)
    assert first.pager_targets == {
        1: f"{fx.ATTACHMENTS_PAGER_PREFIX}ctl05", 2: ATTACH_PAGE_2_TARGET,
    }
    assert [r.index for r in first.rows] == [1, 2, 3, 4]
    second = nc.parse_attachments_page(two)
    assert (second.total_pages, second.current_page) == (2, 2)
    assert [r.index for r in second.rows] == [5, 6, 7, 8]
    with pytest.raises(nc.NgemParseError, match="attachments grid"):
        nc.parse_attachments_page(EVENT_PAGE)


def test_content_disposition_filename_handles_both_forms_and_sanitizes():
    cd = nc.content_disposition_filename
    assert cd(f'attachment;filename="{PDF_NAME}"') == PDF_NAME
    assert cd("attachment; filename=plain.pdf") == "plain.pdf"
    assert cd("attachment; filename*=UTF-8''Plan%20%C3%A9t%C3%A9.pdf") == "Plan été.pdf"
    assert cd("attachment; filename=\"fallback.pdf\"; filename*=UTF-8''pref%C3%A9.pdf") == "prefé.pdf"
    assert cd('attachment; filename="..\\..\\evil\\x.pdf"') == "x.pdf"
    assert cd('attachment; filename="/etc/passwd"') == "passwd"
    assert cd('attachment; filename="..."') is None
    assert cd('attachment; filename="a\x00b\x1f c.pdf"') == "ab c.pdf"
    assert cd('attachment; filename="' + "x" * 500 + '.pdf"') == "x" * nc._FILENAME_MAX
    assert cd("inline") is None and cd(None) is None and cd("") is None
    # A UTF-8 name that came through as latin-1 (what httpx hands back).
    assert cd('attachment; filename="cafÃ©.pdf"') == "café.pdf"


# ── The login form ───────────────────────────────────────────────────────


def test_parse_login_form_carries_every_served_field_and_the_login_button_only():
    action, fields = nc.parse_login_form(LOGIN_PAGE)
    assert action == "./VendorLogin.aspx"
    assert list(fields) == [
        "ScriptManager_TSM", "__EVENTTARGET", "__EVENTARGUMENT", "__VIEWSTATE",
        "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED", "modalPopupID_ClientState",
        "rwMfaPrompt_ClientState", "RadWindowManager1_ClientState", "txtUserName",
        "txtPassword", "chkAgree_ClientState", "btnLogin", "hdnCaptchaResponse",
    ]
    assert fields["__VIEWSTATEGENERATOR"] == "563744BC" and fields["btnLogin"] == "Login"
    assert fields["__VIEWSTATE"] == _viewstate(LOGIN_PAGE)
    body = nc.build_login_body(fields, USERNAME, PASSWORD, nc.checkbox_text(LOGIN_PAGE))
    assert list(body) == list(fields)
    assert body["txtUserName"] == USERNAME and body["txtPassword"] == PASSWORD
    assert body["chkAgree_ClientState"] == AGREE_STATE
    assert body["hdnCaptchaResponse"] == ""
    assert not any(k in body for k in ("hdnBtnLogin", "hdnBtnMfa", "btnForgotPassword"))
    assert nc.parse_login_form(LIST_1) is None
    assert nc.parse_login_form("<html>no form</html>") is None


def test_checkbox_text_comes_from_the_initializer_with_a_fallback():
    assert nc.checkbox_text(LOGIN_PAGE) == fx.AGREE_TEXT
    changed = LOGIN_PAGE.replace(f'"text":"{fx.AGREE_TEXT}"', '"text":"I accept the \\"new\\" terms"', 1)
    assert changed != LOGIN_PAGE
    assert nc.checkbox_text(changed) == 'I accept the "new" terms'
    assert nc.checkbox_text("<html>no telerik</html>") == fx.AGREE_TEXT
    assert nc.agree_client_state("T") == (
        '{"text":"T","value":"","enabled":true,"autoPostBack":true,"commandName":"",'
        '"commandArgument":"","validationGroup":null,"checked":false}'
    )


def test_mfa_prompt_detection_needs_the_window_opened():
    assert not nc._mfa_prompt_shown(LOGIN_PAGE)          # definitions and {"close":tryMfa} only
    assert nc._mfa_prompt_shown(login_page_with(mfa=True))
    assert nc._mfa_prompt_shown(LOGIN_PAGE.replace("</form>", "<script>OpenMfaPopup();</script></form>"))
    visible = LOGIN_PAGE.replace('"name":"rwMfaPrompt",', '"name":"rwMfaPrompt","visibleOnPageLoad":true,')
    assert nc._mfa_prompt_shown(visible)
    assert not nc._mfa_prompt_shown(LIST_1)
    assert nc.login_message(login_page_with(message="  Invalid <b>user</b> name. ")) == "Invalid user name."
    assert nc.login_message(LOGIN_PAGE) is None


# ── Login flow ───────────────────────────────────────────────────────────


def test_login_flow_follows_the_captured_chain_exactly(fake, clock):
    store = _store()
    session = _session(fake, clock, store)
    with session:
        page = session.fetch_invitations(max_pages=1)
    assert page.total_items == 37 and page.pages_fetched == 1 and len(page.rows) == 10
    assert fake.paths == [
        ("GET", nc.ENTRY_PATH),            # 302 /Login.aspx: the session is gone
        ("GET", nc.ENTRY_PATH),            # the login chain starts at the entry URL
        ("GET", nc.LOGIN_PATH),
        ("GET", nc.VENDOR_LOGIN_PATH),
        ("POST", nc.VENDOR_LOGIN_PATH),
        ("GET", nc.ENTRY_PATH),            # the 302 target: the grid is required
        ("GET", nc.ENTRY_PATH),            # the one retry
    ]
    # The POST body: every served hidden field, the credentials, the exact
    # checkbox state, btnLogin, in document order; never the hidden buttons.
    post = fake.requests[4]
    form = fake.form(post)
    assert list(form) == [
        "ScriptManager_TSM", "__EVENTTARGET", "__EVENTARGUMENT", "__VIEWSTATE",
        "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED", "modalPopupID_ClientState",
        "rwMfaPrompt_ClientState", "RadWindowManager1_ClientState", "txtUserName",
        "txtPassword", "chkAgree_ClientState", "btnLogin", "hdnCaptchaResponse",
    ]
    assert form["__VIEWSTATE"] == _viewstate(LOGIN_PAGE)
    assert form["__VIEWSTATEGENERATOR"] == "563744BC"
    assert form["txtUserName"] == USERNAME and form["txtPassword"] == PASSWORD
    assert form["chkAgree_ClientState"] == AGREE_STATE and form["btnLogin"] == "Login"
    assert post.headers["origin"] == BASE
    assert post.headers["referer"] == f"{BASE}{nc.VENDOR_LOGIN_PATH}"
    assert post.headers["content-type"] == "application/x-www-form-urlencoded"
    assert post.headers["upgrade-insecure-requests"] == "1"
    assert "Mozilla/5.0" in post.headers["user-agent"]
    assert fake.requests[0].headers["accept"].startswith("text/html")
    # The password appears in exactly one request body and nowhere else.
    carrying = [
        r for r in fake.requests
        if PASSWORD in unquote_plus(r.content.decode()) or PASSWORD in unquote_plus(str(r.url))
    ]
    assert [(r.method, r.url.path) for r in carrying] == [("POST", nc.VENDOR_LOGIN_PATH)]
    # The session cookie rode the landing GET and the retry.
    assert fake._cookies(fake.requests[5])["procData"] == "proc-1"
    assert fake._cookies(fake.requests[6])["procData"] == "proc-1"
    # The jar is saved under the account with the portal's cookies; failures cleared.
    assert store.state["account"] == USERNAME
    names = {(c["name"], c["domain"]) for c in store.state["cookies"]}
    assert ("procData", nc.HOST) in names and ("ASP.NET_SessionId", nc.HOST) in names
    assert set(store.state["cookies"][0]) == {"name", "value", "domain", "path", "expires", "secure"}
    assert store.state["login_failures"] == 0 and store.state["locked_until"] is None
    assert store.state["logged_in_at"] == NOW.isoformat()
    assert store.state["last_login_attempt_at"] == NOW.isoformat()
    assert store.state["last_used_at"] == NOW.isoformat()
    assert session.logged_in_this_session is True
    assert PASSWORD not in repr(store.state)


@pytest.mark.parametrize("mode", ["redirect_login", "redirect_vendor", "form"])
def test_every_way_the_session_can_prove_gone_means_one_login_and_one_retry(fake, clock, mode):
    fake.entry_mode = mode
    with _session(fake, clock) as session:
        assert len(session.fetch_invitations(max_pages=1).rows) == 10
        assert sum(1 for p in fake.paths if p == ("POST", nc.VENDOR_LOGIN_PATH)) == 1
        entry_gets = [p for p in fake.paths if p == ("GET", nc.ENTRY_PATH)]
        assert len(entry_gets) == 4          # gone, chain start, landing, retry
        before = len(fake.paths)
        # A later call on the same session reuses the jar: no second login.
        assert len(session.fetch_invitations(max_pages=1).rows) == 10
    assert fake.paths[before:] == [("GET", nc.ENTRY_PATH)]


def test_login_does_not_happen_while_the_stored_jar_still_works(fake, clock):
    with _logged_in(fake, clock) as session:
        page = session.fetch_invitations(max_pages=1)
    assert page.pages_fetched == 1 and fake.paths == [("GET", nc.ENTRY_PATH)]
    assert fake._cookies(fake.requests[0])["procData"] == "stored"


def test_a_jar_saved_for_another_account_is_not_loaded(fake, clock):
    fake.sessions.add("theirs")
    store = _store(
        account="SOMEONEELSE",
        cookies=[{"name": "procData", "value": "theirs", "domain": nc.HOST, "path": "/"}],
        logged_in_at=NOW.isoformat(),
    )
    with _session(fake, clock, store) as session:
        session.fetch_invitations(max_pages=1)
    assert "cookie" not in fake.requests[0].headers
    assert ("POST", nc.VENDOR_LOGIN_PATH) in fake.paths
    assert store.state["account"] == USERNAME


def test_credentials_rejected_reads_the_login_message(fake, clock):
    fake.login_mode = "rejected"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    cause = exc.value.__cause__
    assert isinstance(cause, nc.NgemLoginFailed)
    assert str(cause) == "Invalid user name or password."
    assert exc.value.locked_until == NOW + timedelta(seconds=600)
    assert store.state["login_failures"] == 1
    assert store.state["last_error"] == "Invalid user name or password."
    assert fake.paths[-1] == ("POST", nc.VENDOR_LOGIN_PATH)


def test_credentials_rejected_without_a_message_says_so(fake, clock):
    fake.login_mode = "ok"
    store = _store()
    with _session(fake, clock, store, config=_config(password="wrong one")) as session:
        with pytest.raises(nc.NgemUnavailable) as exc:
            session.fetch_invitations()
    assert str(exc.value.__cause__) == "Invalid user name or password."
    # A re-rendered form with no #divLoginMessage at all.
    fake.login_mode = "rejected"
    fake.requests.clear()
    with _session(fake, clock, _store()) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert str(exc.value.__cause__) == "Invalid user name or password."
    page = login_page_with(message=None)
    assert nc.login_message(page) is None
    session = _session(fake, clock, _store())
    with session:
        with pytest.raises(nc.NgemLoginFailed, match="credentials rejected"):
            session._client = httpx.Client(
                transport=httpx.MockTransport(lambda r: _html(200, page) if r.method == "POST" else fake(r)),
                follow_redirects=False,
            )
            session._login()


def test_error_page_redirect_after_the_post_is_an_unexpected_page(fake, clock):
    fake.login_mode = "error"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert str(exc.value.__cause__) == "unexpected page"
    assert store.state["login_failures"] == 1 and store.state["last_error"] == "unexpected page"
    # /Error.aspx itself is never requested (it emails the portal's admins).
    assert not any(p == nc.ERROR_PATH for _, p in fake.paths)


def test_mfa_prompt_after_the_post_fails_the_login(fake, clock):
    fake.login_mode = "mfa"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert str(exc.value.__cause__) == "the account asks for MFA"
    assert store.state["last_error"] == "the account asks for MFA"


def test_mfa_prompt_on_the_login_page_itself_fails_before_any_post(fake, clock):
    fake.login_page_mode = "mfa"
    with _session(fake, clock) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert str(exc.value.__cause__) == "the account asks for MFA"
    assert not any(m == "POST" for m, _ in fake.paths)


def test_off_site_redirect_is_refused_on_the_login_chain(fake, clock):
    fake.login_page_mode = "offsite"
    with _session(fake, clock) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert "off the portal" in str(exc.value.__cause__)
    assert not any(r.url.host == "evil.example.com" for r in fake.requests)


def test_off_site_redirect_after_the_post_is_refused(fake, clock):
    fake.login_mode = "offsite"
    with _session(fake, clock) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert "off the portal" in str(exc.value.__cause__)
    assert not any(r.url.host == "evil.example.com" for r in fake.requests)
    assert fake.paths[-1] == ("POST", nc.VENDOR_LOGIN_PATH)


@pytest.mark.parametrize(
    "arm, text",
    [
        ({"login_page_mode": "http"}, "off the portal"),
        ({"login_mode": "http"}, "off the portal"),
        ({"login_page_mode": "http_action"}, "posts somewhere unexpected"),
    ],
)
def test_an_http_downgrade_anywhere_in_the_login_chain_is_refused(fake, clock, arm, text):
    """A Location or a form action the portal downgraded to http never
    reaches the allowlist (a ValueError the service would classify as
    unknown trouble): it is the same refusal as an off-site redirect, a
    login failure that is counted."""
    for name, value in arm.items():
        setattr(fake, name, value)
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert text in str(exc.value.__cause__) and "somewhere it will not go" in str(exc.value.__cause__)
    assert not any(str(r.url).startswith("http://") for r in fake.requests)
    assert store.state["login_failures"] == 1


def test_a_portal_path_outside_the_flow_ends_the_chain(fake, clock):
    fake.login_mode = "elsewhere"     # 302 to a VendorAdmin page
    with _session(fake, clock) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert "left the expected flow (/VendorAdmin/Profile.aspx)" in str(exc.value.__cause__)
    assert not any(p.startswith("/VendorAdmin") for _, p in fake.paths)


def test_a_302_to_the_entry_without_a_session_cookie_fails_the_login(fake, clock):
    fake.login_mode = "nocookie"
    with _session(fake, clock) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert "no session cookie" in str(exc.value.__cause__)
    assert fake.paths[-1] == ("POST", nc.VENDOR_LOGIN_PATH)


def test_a_landing_page_without_the_grid_fails_the_login(fake, clock):
    fake.list_mode = "nogrid"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert "did not show the invitations grid" in str(exc.value.__cause__)
    assert store.state["login_failures"] == 1


def test_too_many_hops_ends_the_login(fake, clock):
    fake.login_page_mode = "loop"     # /VendorLogin.aspx bounces back to /Login.aspx forever
    with _session(fake, clock) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert "too many times" in str(exc.value.__cause__)
    chain = fake.paths[2:]
    assert len(chain) == nc._MAX_HOPS == 6
    assert set(chain) == {("GET", nc.LOGIN_PATH), ("GET", nc.VENDOR_LOGIN_PATH)}


def test_login_page_without_a_form_fails_the_login(fake, clock):
    fake.login_page_mode = "noform"
    with _session(fake, clock) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert "no login form" in str(exc.value.__cause__)


def test_a_5xx_during_the_login_is_transient_stamps_the_attempt_and_counts_no_failure(fake, clock):
    """The chain broke on a 502: nothing moves toward the lock (it says
    nothing about the credentials), but the attempt IS stamped in the store
    so another process honours the minimum interval too (the credentials
    may already have been posted)."""
    fake.login_page_mode = "502"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(nc.NgemTransient):
        session.fetch_invitations()
    assert store.state.get("login_failures", 0) == 0 and store.state.get("locked_until") is None
    assert store.state["last_login_attempt_at"] == NOW.isoformat()
    assert store.state["last_error"].startswith("transient: NGEM answered 502")
    assert PASSWORD not in store.state["last_error"]
    # Availability is untouched (no failure), yet another worker sharing the
    # store defers its login inside the interval instead of posting again.
    assert nc.availability_from(store.load(), NOW, login_min_interval=600) == (True, None, None)
    fake.login_page_mode = "ok"
    posted_before = fake.paths.count(("POST", nc.VENDOR_LOGIN_PATH))
    with _session(fake, clock, store) as other, pytest.raises(nc.NgemUnavailable) as exc:
        other.fetch_invitations()
    assert "attempted recently" in str(exc.value)
    assert exc.value.locked_until == NOW + timedelta(seconds=600)
    assert fake.paths.count(("POST", nc.VENDOR_LOGIN_PATH)) == posted_before == 0
    # A timeout on the chain is stamped the same way.
    fake.login_page_mode = "ok"
    fake.list_mode = "transport"
    fresh = _store()
    with _session(fake, clock, fresh) as session, pytest.raises(nc.NgemTransient):
        session.fetch_invitations()
    assert fresh.state["last_login_attempt_at"] == NOW.isoformat() and fresh.state.get("login_failures", 0) == 0


# ── The login discipline ─────────────────────────────────────────────────


def test_second_login_inside_the_interval_is_refused_without_a_request(fake, clock):
    fake.login_mode = "rejected"
    store = _store()
    with _session(fake, clock, store) as session:
        with pytest.raises(nc.NgemUnavailable):
            session.fetch_invitations()
        after_first = len(fake.paths)
        with pytest.raises(nc.NgemUnavailable) as exc:
            session.fetch_invitations()
    assert "attempted recently" in str(exc.value)
    assert exc.value.locked_until == NOW + timedelta(seconds=600)
    assert fake.paths[after_first:] == [("GET", nc.ENTRY_PATH)]
    assert store.state["login_failures"] == 1


def test_a_recent_attempt_recorded_by_another_worker_also_defers_the_login(fake, clock):
    store = _store(last_login_attempt_at=(NOW - timedelta(seconds=30)).isoformat())
    with _session(fake, clock, store) as session, pytest.raises(nc.NgemUnavailable) as exc:
        session.fetch_invitations()
    assert exc.value.locked_until == NOW - timedelta(seconds=30) + timedelta(seconds=600)
    assert fake.paths == [("GET", nc.ENTRY_PATH)]


def test_failure_counter_locks_logins_rings_once_and_reports_availability(fake, clock):
    fake.login_mode = "rejected"
    store = _store()
    bells = []
    session = _session(
        fake, clock, store, config=_config(login_min_interval=0),
        on_lock=lambda until, error: bells.append((until, error)),
    )
    with session:
        for _ in range(2):
            with pytest.raises(nc.NgemUnavailable) as exc:
                session.fetch_invitations()
            assert not isinstance(exc.value, nc.NgemLoginLocked)
        assert session.availability() == (True, None, None)
        assert nc.availability_from(store.load(), NOW) == (True, None, None)
        with pytest.raises(nc.NgemLoginLocked) as locked:
            session.fetch_invitations()
        until = NOW + timedelta(seconds=21600)
        assert locked.value.locked_until == until
        assert store.state["login_failures"] == 3 and store.state["locked_until"] == until.isoformat()
        assert bells == [(until, "Invalid user name or password.")]
        # While locked: no login chain, the bell does not ring again.
        before = len(fake.paths)
        with pytest.raises(nc.NgemLoginLocked):
            session.fetch_invitations()
        assert fake.paths[before:] == [("GET", nc.ENTRY_PATH)]
        assert bells == [(until, "Invalid user name or password.")]
        assert session.availability() == (False, "NGEM logins are locked after repeated failures.", until)
        assert nc.availability_from(store.load(), NOW) == (
            False, "NGEM logins are locked after repeated failures.", until,
        )
    assert PASSWORD not in repr(store.state)


def test_a_bell_that_raises_never_masks_the_lock(fake, clock):
    fake.login_mode = "rejected"
    store = _store(login_failures=2)

    def boom(until, error):
        raise RuntimeError("notifications down")

    with _session(fake, clock, store, config=_config(login_min_interval=0), on_lock=boom) as session:
        with pytest.raises(nc.NgemLoginLocked):
            session.fetch_invitations()
    assert store.state["login_failures"] == 3


def test_lock_state_reads_the_store_and_expires(fake, clock):
    until = NOW + timedelta(hours=1)
    assert nc.lock_state({"locked_until": until.isoformat()}, NOW) == until
    assert nc.lock_state({"locked_until": (NOW - timedelta(seconds=1)).isoformat()}, NOW) is None
    assert nc.lock_state({"locked_until": "garbage"}, NOW) is None
    assert nc.lock_state(None, NOW) is None
    store = _store(locked_until=(NOW - timedelta(seconds=1)).isoformat(), login_failures=3)
    with _session(fake, clock, store) as session:
        assert session.availability() == (True, None, None)
        session.fetch_invitations(max_pages=1)
    assert store.state["login_failures"] == 0


def test_availability_from_the_store_row_alone():
    until = NOW + timedelta(hours=2)
    assert nc.availability_from(None, NOW) == (True, None, None)
    assert nc.availability_from({}, NOW) == (True, None, None)
    assert nc.availability_from({"locked_until": until.isoformat()}, NOW) == (
        False, "NGEM logins are locked after repeated failures.", until,
    )
    recent = {"login_failures": 1, "last_login_attempt_at": (NOW - timedelta(seconds=100)).isoformat()}
    assert nc.availability_from(recent, NOW) == (True, None, None)
    ok, reason, wait = nc.availability_from(recent, NOW, login_min_interval=600)
    assert ok is False and "failed recently" in reason and wait == NOW + timedelta(seconds=500)
    healthy = {"login_failures": 0, "last_login_attempt_at": (NOW - timedelta(seconds=100)).isoformat()}
    assert nc.availability_from(healthy, NOW, login_min_interval=600) == (True, None, None)


def test_unconfigured_credentials_never_attempt_a_login(fake, clock):
    with _session(fake, clock, config=_config(password="")) as session:
        assert session.availability() == (False, "NGEM credentials are not configured.", None)
        with pytest.raises(nc.NgemUnavailable, match="not configured"):
            session.fetch_invitations()
    assert fake.paths == [("GET", nc.ENTRY_PATH)]
    fake.requests.clear()
    bad = _config(entry_url="https://evil.example.com/VendorResponse/ResponseList.aspx?e=x")
    with _session(fake, clock, config=bad) as session:
        assert session.availability()[1].startswith("NGEM_ENTRY_URL")
        with pytest.raises(nc.NgemUnavailable, match="NGEM_ENTRY_URL"):
            session.fetch_invitations()
    assert fake.requests == []
    bad = _config(entry_url=f"{BASE}/VendorAdmin/Profile.aspx")
    with _session(fake, clock, config=bad) as session, pytest.raises(nc.NgemUnavailable):
        session.fetch_invitations()
    assert fake.requests == []


def test_a_newer_jar_from_another_worker_is_tried_before_a_login(fake, clock):
    fake.sessions.add("fresh")
    store = _store(
        account=USERNAME,
        cookies=[{"name": "procData", "value": "stale", "domain": nc.HOST, "path": "/"}],
        logged_in_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    session = _session(fake, clock, store)
    session._load_store()
    store.state["cookies"] = [{"name": "procData", "value": "fresh", "domain": nc.HOST, "path": "/"}]
    store.state["logged_in_at"] = (NOW - timedelta(minutes=1)).isoformat()
    with session:
        assert len(session.fetch_invitations(max_pages=1).rows) == 10
    assert fake.paths == [("GET", nc.ENTRY_PATH), ("GET", nc.ENTRY_PATH)]
    assert fake._cookies(fake.requests[0])["procData"] == "stale"
    assert fake._cookies(fake.requests[1])["procData"] == "fresh"
    assert session.logged_in_this_session is False


def test_a_jar_no_newer_than_ours_leads_to_a_login(fake, clock):
    store = _store(
        account=USERNAME,
        cookies=[{"name": "procData", "value": "stale", "domain": nc.HOST, "path": "/"}],
        logged_in_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    with _session(fake, clock, store) as session:
        session.fetch_invitations(max_pages=1)
    assert ("POST", nc.VENDOR_LOGIN_PATH) in fake.paths
    assert session.logged_in_this_session is True


def test_expired_cookies_in_the_store_are_skipped(fake, clock):
    fake.sessions.add("old")
    store = _store(
        account=USERNAME,
        cookies=[{
            "name": "procData", "value": "old", "domain": nc.HOST, "path": "/",
            "expires": (NOW - timedelta(days=1)).timestamp(),
        }],
        logged_in_at=(NOW - timedelta(days=2)).isoformat(),
    )
    with _session(fake, clock, store) as session:
        session.fetch_invitations(max_pages=1)
    assert "cookie" not in fake.requests[0].headers


def test_the_password_never_reaches_a_log_line_an_error_or_a_row(fake, clock, caplog):
    """Every failure path, with the portal (adversarially) echoing the
    password back in its login message: nothing raised, logged or stored
    carries it."""
    caplog.set_level(logging.DEBUG, logger="app.services.ngem_client")
    raised: list[BaseException] = []
    stores: list[nc.MemorySessionStore] = []
    for mode in ("echo", "rejected", "error", "mfa", "offsite", "elsewhere", "nocookie"):
        fake.login_mode = mode
        store = _store()
        stores.append(store)
        with _session(fake, clock, store) as session:
            try:
                session.fetch_invitations()
            except nc.NgemError as exc:
                raised.append(exc)
    assert len(raised) == 7
    echoed = raised[0]
    assert str(echoed.__cause__) == "The password [redacted] is wrong."
    for exc in raised:
        chain = []
        cur: BaseException | None = exc
        while cur is not None:
            chain.append(cur)
            cur = cur.__cause__ or cur.__context__
        for item in chain:
            assert PASSWORD not in str(item) and PASSWORD not in repr(item)
    for record in caplog.records:
        assert PASSWORD not in record.getMessage() and PASSWORD not in str(record.args)
    assert any("NGEM login failed" in r.getMessage() for r in caplog.records)
    for store in stores:
        assert PASSWORD not in repr(store.state)
    assert stores[0].state["last_error"] == "The password [redacted] is wrong."


# ── Paging ───────────────────────────────────────────────────────────────


def test_fetch_invitations_pages_with_a_full_postback_and_dedupes(fake, clock):
    with _logged_in(fake, clock) as session:
        page = session.fetch_invitations(max_pages=20)
    assert page.pages_fetched == 4 and page.total_pages == 4 and page.total_items == 37
    # Pages 3 and 4 answered page 2 again (a grid that shifted): 10 + 10 + 0 + 0.
    assert len(page.rows) == 20
    assert [r.agency for r in page.rows[:2]] == ["Clark County, Nevada", "Clark County, Nevada"]
    assert page.rows[10].bid_number == "27300209"
    keys = {(r.agency, r.bid_number) for r in page.rows}
    assert len(keys) == 20
    assert fake.paths == [
        ("GET", nc.ENTRY_PATH),
        ("POST", nc.ENTRY_PATH), ("POST", nc.ENTRY_PATH), ("POST", nc.ENTRY_PATH),
    ]
    posts = fake.requests[1:]
    assert [fake.form(p)["__EVENTTARGET"] for p in posts] == [fx.PAGE_TARGETS[n] for n in (2, 3, 4)]
    first = fake.form(posts[0])
    served = nc.parse_invitations_page(LIST_1).form_fields
    # Every non-button input of the served page, the filter boxes empty, the
    # combobox states as served; only the two event fields changed.
    expected = dict(served, __EVENTTARGET=PAGE_2_TARGET, __EVENTARGUMENT="")
    assert first == expected
    assert list(first) == list(served)
    assert "__ASYNCPOST" not in first
    assert first["ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl02$ctl02$rcbAgencyName"] == "All"
    assert all(v == "" for k, v in first.items() if "FilterTextBox_" in k)
    # Page 3 is posted with page 2's served fields.
    assert fake.form(posts[1])["__VIEWSTATE"] == _viewstate(LIST_2)
    for p in posts:
        assert str(p.url) == ENTRY_URL
        assert p.headers["referer"] == ENTRY_URL and p.headers["origin"] == BASE
        assert p.headers["content-type"] == "application/x-www-form-urlencoded"
        assert fake._cookies(p)["procData"] == "stored"


def test_fetch_invitations_honors_max_pages(fake, clock):
    with _logged_in(fake, clock) as session:
        page = session.fetch_invitations(max_pages=2)
        assert page.pages_fetched == 2 and page.total_pages == 4 and len(page.rows) == 20
        assert len(fake.paths) == 2
        one = session.fetch_invitations(max_pages=0)
    assert one.pages_fetched == 1 and len(one.rows) == 10


def test_fetch_invitations_session_gone_mid_scan_logs_in_once_and_resumes(fake, clock):
    fake.pager_mode = "expire_once"      # the page-2 postback answers 302 /Login.aspx once
    with _logged_in(fake, clock) as session:
        page = session.fetch_invitations(max_pages=2)
    assert page.pages_fetched == 2 and len(page.rows) == 20
    assert fake.paths == [
        ("GET", nc.ENTRY_PATH),
        ("POST", nc.ENTRY_PATH),           # 302 /Login.aspx
        ("GET", nc.ENTRY_PATH),            # the login chain
        ("GET", nc.LOGIN_PATH),
        ("GET", nc.VENDOR_LOGIN_PATH),
        ("POST", nc.VENDOR_LOGIN_PATH),
        ("GET", nc.ENTRY_PATH),            # landing
        ("GET", nc.ENTRY_PATH),            # fresh fields for the retry
        ("POST", nc.ENTRY_PATH),           # page 2, once more
    ]
    assert fake._cookies(fake.requests[-1])["procData"] == "proc-1"


def test_fetch_invitations_without_the_grid_after_a_valid_session_is_a_parse_error(fake, clock):
    fake.list_mode = "nogrid"
    with _logged_in(fake, clock) as session, pytest.raises(nc.NgemParseError, match="invitations grid"):
        session.fetch_invitations()
    assert fake.paths == [("GET", nc.ENTRY_PATH)]


def test_fetch_invitations_pager_that_stops_offering_a_page_is_a_parse_error(fake, clock):
    fake.pager_mode = "short"
    with _logged_in(fake, clock) as session, pytest.raises(nc.NgemParseError, match="did not offer page 3"):
        session.fetch_invitations()
    assert len(fake.paths) == 2


@pytest.mark.parametrize(
    "mode, exc_type, text",
    [
        ("error_redirect", nc.NgemTransient, "error page"),
        ("error_page", nc.NgemTransient, "error page"),
        ("500", nc.NgemTransient, "503"),
        ("transport", nc.NgemTransient, "ConnectError"),
        ("offsite", nc.NgemForbidden, "off the portal"),
        ("http", nc.NgemForbidden, "somewhere it will not go"),
        ("huge", nc.NgemParseError, "larger page than the client accepts"),
    ],
)
def test_fetch_invitations_maps_portal_trouble(fake, clock, mode, exc_type, text):
    fake.list_mode = mode
    with _logged_in(fake, clock) as session, pytest.raises(exc_type, match=text):
        session.fetch_invitations()
    assert fake.paths == [("GET", nc.ENTRY_PATH)]
    assert not any(p == nc.ERROR_PATH for _, p in fake.paths)


def test_postback_refuses_every_target_that_is_not_a_grid_pager(fake, clock):
    grid = nc.parse_invitations_page(LIST_1)
    with _logged_in(fake, clock) as session:
        session._load_store()
        for target in (
            "",                                                                     # the zip postback
            "ctl00$mainContent$ucNotInvitedListGrid$rgResponse$ctl00$ctl03$ctl01$ctl06",  # the other grid
            "ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl02$ctl02$Filter_colStatus",
            "ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl04$aHrfView",
            "ctl00$mainContent$ctlTabStrip$btnHiddenRetract",
            "chkAgree",
            PAGE_2_TARGET + "$ctl00",
        ):
            with pytest.raises(ValueError):
                session._postback(grid.form_action, grid.form_fields, target, referer=ENTRY_URL)
        # Text in a filter box, or an armed zip action, is refused as well.
        filtered = dict(grid.form_fields)
        filtered["ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl02$ctl02$FilterTextBox_colTitle"] = "x"
        with pytest.raises(ValueError, match="filter field"):
            session._postback(grid.form_action, grid.form_fields | filtered, PAGE_2_TARGET, referer=ENTRY_URL)
        armed = dict(grid.form_fields, **{"ctl00$mainContent$hfDocZipAction": "dSYX+86f"})
        with pytest.raises(ValueError, match="zip"):
            session._postback(grid.form_action, armed, PAGE_2_TARGET, referer=ENTRY_URL)
        # A form whose action (portal-controlled) is not a grid page, is on
        # another host, or has been downgraded to http: refused as
        # NgemForbidden before the allowlist would raise for it.
        for action in (
            f"{BASE}{nc.EVENT_PATH}?e=x",
            f"http://{nc.HOST}{nc.ENTRY_PATH}?e=x",
            "https://evil.example.com/VendorResponse/ResponseList.aspx?e=x",
            "ftp://supplier.ionwave.net/VendorResponse/ResponseList.aspx",
        ):
            with pytest.raises(nc.NgemForbidden, match="somewhere it will not go"):
                session._postback(action, grid.form_fields, PAGE_2_TARGET, referer=None)
    assert fake.requests == []


# ── Event and attachments ────────────────────────────────────────────────


def test_fetch_event_reads_the_captured_page(fake, clock):
    with _logged_in(fake, clock) as session:
        facts = session.fetch_event(EVENT_URL)
    assert facts.bid_number_raw == "5584-GS Addendum 1" and facts.attachments_url == ATTACHMENTS_URL
    assert facts.contact["email"] == "contact@example.gov"
    assert fake.paths == [("GET", nc.EVENT_PATH)]
    assert str(fake.requests[0].url) == EVENT_URL
    assert fake.requests[0].headers["referer"] == ENTRY_URL


def test_fetch_event_session_gone_means_one_login_and_one_retry(fake, clock):
    fake.event_mode = "expire_once"
    with _logged_in(fake, clock) as session:
        facts = session.fetch_event(EVENT_URL)
    assert facts.title == "Emergency Phone Tower Installations and Upgrades"
    assert [p for p in fake.paths if p == ("GET", nc.EVENT_PATH)] == [("GET", nc.EVENT_PATH)] * 2
    assert sum(1 for p in fake.paths if p == ("POST", nc.VENDOR_LOGIN_PATH)) == 1


def test_fetch_event_stale_token_is_forbidden_with_the_stale_reason(fake, clock):
    stale = f"{BASE}{nc.EVENT_PATH}?e=event99"
    with _logged_in(fake, clock) as session, pytest.raises(nc.NgemForbidden) as exc:
        session.fetch_event(stale)
    assert exc.value.reason == nc.STALE_LINK == "stale_link"
    assert fake.paths == [("GET", nc.EVENT_PATH)]     # BadRequest.aspx itself is never fetched
    # A 200 that is the Bad Request page reads the same way.
    fake.requests.clear()
    with _logged_in(fake, clock) as session, pytest.raises(nc.NgemForbidden) as exc:
        session._client = httpx.Client(
            transport=httpx.MockTransport(lambda r: _html(200, BAD_REQUEST_PAGE)), follow_redirects=False,
        )
        session.fetch_event(EVENT_URL)
    assert exc.value.reason == nc.STALE_LINK


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/VendorResponse/Bid/VResponseSubmission.aspx?e=event03",
        f"{BASE}/VendorResponse/Bid/VResponseQuestion.aspx?e=event03",
        f"{BASE}/VendorResponse/Bid/VResponseAttachments.aspx?e=event03",
        f"{BASE}/VendorResponse/Bid/VResponseLine.aspx?e=event03",
        f"{BASE}/VendorResponse/Bid/VResponseBidAttachments.aspx?e=event41",
        f"{BASE}/VendorAdmin/Profile.aspx?e=event03",
        f"http://{nc.HOST}/VendorResponse/Bid/VResponseEvent.aspx?e=event03",
        "https://evil.example.com/VendorResponse/Bid/VResponseEvent.aspx?e=event03",
        f"https://{nc.HOST}.evil.example/VendorResponse/Bid/VResponseEvent.aspx?e=event03",
        f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx?e=event03&x=1",
        f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx",
        f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx?e=",
        f"{BASE}/VendorResponse/Bid/vresponseevent.aspx?e=event03",
        "",
    ],
)
def test_fetch_event_accepts_only_the_event_page_link(fake, clock, url):
    with _logged_in(fake, clock) as session, pytest.raises(nc.NgemForbidden):
        session.fetch_event(url)
    assert fake.requests == []


def test_fetch_attachments_reads_the_grid_behind_the_tab_link(fake, clock):
    with _logged_in(fake, clock) as session:
        facts = session.fetch_event(EVENT_URL)
        rows = session.fetch_attachments(facts, max_pages=20)
    assert [r.file_name for r in rows][:2] == ["Addendum 1 to 5584-GS.pdf", "5584-GS Bid Document.docx.pdf"]
    assert len(rows) == 8 and [r.download_url for r in rows] == list(fx.DOWNLOAD_URLS)
    assert fake.paths == [("GET", nc.EVENT_PATH), ("GET", nc.ATTACHMENTS_PATH)]
    assert str(fake.requests[1].url) == ATTACHMENTS_URL
    assert fake.requests[1].headers["referer"] == facts.event_url


def test_fetch_attachments_pages_with_the_grids_own_pager(fake, clock):
    fake.attach_mode = "two_pages"
    with _logged_in(fake, clock) as session:
        facts = session.fetch_event(EVENT_URL)
        rows = session.fetch_attachments(facts, max_pages=20)
        capped = session.fetch_attachments(facts, max_pages=1)
    assert [r.index for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [r.index for r in capped] == [1, 2, 3, 4]
    assert fake.paths == [
        ("GET", nc.EVENT_PATH), ("GET", nc.ATTACHMENTS_PATH), ("POST", nc.ATTACHMENTS_PATH),
        ("GET", nc.ATTACHMENTS_PATH),
    ]
    post = fake.requests[2]
    form = fake.form(post)
    served = nc.parse_attachments_page(attachments_pages()[0]).form_fields
    assert form == dict(served, __EVENTTARGET=ATTACH_PAGE_2_TARGET, __EVENTARGUMENT="")
    assert form["ctl00$mainContent$hfDocZipAction"] == ""
    assert str(post.url) == ATTACHMENTS_URL and post.headers["referer"] == ATTACHMENTS_URL


def test_fetch_attachments_needs_the_tab_link(fake, clock):
    facts = nc.parse_event_page(EVENT_PAGE)
    no_tab = nc.EventFacts(**{**facts.__dict__, "attachments_url": None})
    with _logged_in(fake, clock) as session:
        with pytest.raises(nc.NgemParseError, match="no Attachments tab"):
            session.fetch_attachments(no_tab, max_pages=1)
        for bad in (
            f"{BASE}/VendorResponse/Bid/VResponseAttachments.aspx?e=event41",
            f"{BASE}/VendorResponse/Bid/VResponseSubmission.aspx?e=event41",
            f"https://evil.example.com{nc.ATTACHMENTS_PATH}?e=event41",
        ):
            with pytest.raises(nc.NgemForbidden):
                session.fetch_attachments(nc.EventFacts(**{**facts.__dict__, "attachments_url": bad}), max_pages=1)
    assert fake.requests == []


# ── Downloads ────────────────────────────────────────────────────────────


def test_download_streams_the_file_with_the_session_and_reads_the_filename(fake, clock, tmp_path):
    dest = tmp_path / "0007.bin"
    with _logged_in(fake, clock) as session:
        result = session.download(FILE_URL, dest, max_bytes=len(PDF), referer=ATTACHMENTS_URL)
    assert result == nc.DownloadResult(bytes_written=len(PDF), filename=PDF_NAME)
    assert dest.read_bytes() == PDF
    assert fake.paths == [("GET", nc.EXTRACT_PATH)]
    req = fake.requests[0]
    assert str(req.url) == FILE_URL
    assert fake._cookies(req)["procData"] == "stored"
    assert req.headers["referer"] == ATTACHMENTS_URL
    # Without a referer, the attachments page path stands in.
    fake.requests.clear()
    with _logged_in(fake, clock) as session:
        session.download(FILE_URL, tmp_path / "again.bin", max_bytes=len(PDF))
    assert fake.requests[0].headers["referer"] == f"{BASE}{nc.ATTACHMENTS_PATH}"


def test_download_filename_variants(fake, clock, tmp_path):
    with _logged_in(fake, clock) as session:
        fake.file_mode = "nodisp"
        assert session.download(FILE_URL, tmp_path / "a", max_bytes=10_000).filename is None
        fake.file_mode = "star"
        assert session.download(FILE_URL, tmp_path / "b", max_bytes=10_000).filename == "Plan été.pdf"


@pytest.mark.parametrize(
    "url",
    [
        f"http://{nc.HOST}/Extract.aspx?e=file01",
        f"https://{nc.HOST}.evil.example/Extract.aspx?e=file01",
        "https://evil.example.com/Extract.aspx?e=file01",
        f"{BASE}/VendorResponse/Bid/VResponseBidAttachments.aspx?e=event41",
        f"{BASE}/Extract.aspx",
        f"{BASE}/Extract.aspx?e=file01&x=1",
        f"{BASE}/extract.aspx?e=file01",
        "ftp://supplier.ionwave.net/Extract.aspx?e=file01",
        "",
    ],
)
def test_download_accepts_only_extract_links(fake, clock, tmp_path, url):
    dest = tmp_path / "x.bin"
    with _logged_in(fake, clock) as session, pytest.raises(nc.NgemForbidden):
        session.download(url, dest, max_bytes=10_000)
    assert fake.requests == [] and not dest.exists()


def test_download_enforces_the_byte_cap_and_unlinks(fake, clock, tmp_path):
    dest = tmp_path / "x.bin"
    with _logged_in(fake, clock) as session:
        with pytest.raises(nc.NgemForbidden, match="larger than the harvest accepts"):
            session.download(FILE_URL, dest, max_bytes=len(PDF) - 1)
        assert not dest.exists()
        fake.file_mode = "huge"           # declared up front
        with pytest.raises(nc.NgemForbidden, match="larger"):
            session.download(FILE_URL, dest, max_bytes=len(PDF))
        assert not dest.exists()
        fake.file_mode = "ok"
        fake.pdf = b"y" * (3 * 1024 * 1024 + 7)
        assert session.download(FILE_URL, dest, max_bytes=4 * 1024 * 1024).bytes_written == len(fake.pdf)
        assert dest.read_bytes() == fake.pdf


def test_download_html_answer_is_transient_and_unlinks(fake, clock, tmp_path):
    fake.file_mode = "html"
    dest = tmp_path / "x.bin"
    with _logged_in(fake, clock) as session, pytest.raises(nc.NgemTransient, match="page instead of the file"):
        session.download(FILE_URL, dest, max_bytes=10_000)
    assert not dest.exists()
    assert fake.paths == [("GET", nc.EXTRACT_PATH)]     # no login: the session was fine


@pytest.mark.parametrize(
    "mode, exc_type",
    [("500", nc.NgemTransient), ("transport", nc.NgemTransient), ("404", nc.NgemForbidden),
     ("badrequest", nc.NgemForbidden), ("offsite", nc.NgemForbidden), ("http", nc.NgemForbidden)],
)
def test_download_maps_trouble_and_unlinks(fake, clock, tmp_path, mode, exc_type):
    fake.file_mode = mode
    dest = tmp_path / "x.bin"
    with _logged_in(fake, clock) as session, pytest.raises(exc_type) as exc:
        session.download(FILE_URL, dest, max_bytes=10_000)
    assert not dest.exists()
    assert len(fake.requests) == 1
    if mode == "badrequest":
        assert exc.value.reason == nc.STALE_LINK
    if mode == "transport":
        assert "ReadTimeout" in str(exc.value)
    if mode in ("offsite", "http"):
        assert "somewhere it will not go" in str(exc.value)


def test_download_session_gone_means_one_login_and_one_retry(fake, clock, tmp_path):
    fake.file_mode = "expire_once"
    dest = tmp_path / "x.bin"
    with _logged_in(fake, clock) as session:
        result = session.download(FILE_URL, dest, max_bytes=len(PDF))
    assert result.bytes_written == len(PDF) and dest.read_bytes() == PDF
    assert fake.paths == [
        ("GET", nc.EXTRACT_PATH),          # 302 /Login.aspx
        ("GET", nc.ENTRY_PATH), ("GET", nc.LOGIN_PATH), ("GET", nc.VENDOR_LOGIN_PATH),
        ("POST", nc.VENDOR_LOGIN_PATH), ("GET", nc.ENTRY_PATH),
        ("GET", nc.EXTRACT_PATH),          # the one retry
    ]
    assert fake._cookies(fake.requests[-1])["procData"] == "proc-1"


def test_download_refuses_to_overwrite_an_existing_file(fake, clock, tmp_path):
    dest = tmp_path / "x.bin"
    dest.write_bytes(b"keep")
    with _logged_in(fake, clock) as session, pytest.raises(FileExistsError):
        session.download(FILE_URL, dest, max_bytes=10_000)
    assert fake.requests == [] and dest.read_bytes() == b"keep"


# ── Pace ─────────────────────────────────────────────────────────────────


def test_pace_waits_at_least_the_interval_between_requests():
    clock = Clock()
    nc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == []
    nc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0]
    clock.t += 0.5
    nc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0, 1.5]
    clock.t += 10
    nc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0, 1.5]


def test_pace_applies_the_random_multiplier_between_one_and_two():
    clock = Clock()
    draws = []

    def rng(lo, hi):
        draws.append((lo, hi))
        return 1.75

    nc._pace(2.0, sleep=clock.sleep, clock=clock, rng=rng)
    nc._pace(2.0, sleep=clock.sleep, clock=clock, rng=rng)
    assert draws == [(1.0, 2.0), (1.0, 2.0)]
    assert clock.sleeps == [3.5]


def test_pace_never_sleeps_when_the_interval_is_zero_or_negative():
    clock = Clock()
    for _ in range(3):
        nc._pace(0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 2.0)
        nc._pace(-1.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 2.0)
    assert clock.sleeps == []


def test_every_request_of_a_login_a_scan_and_a_download_is_paced(fake, clock, tmp_path):
    renewed = []
    session = _session(fake, clock, config=_config(min_request_interval=2.0), renew=lambda: renewed.append(1))
    with session:
        session.fetch_invitations(max_pages=3)
        facts = session.fetch_event(EVENT_URL)
        session.fetch_attachments(facts, max_pages=1)
        session.download(FILE_URL, tmp_path / "a.pdf", max_bytes=10_000)
    assert len(fake.requests) == 12
    assert len(clock.sleeps) == len(fake.requests) - 1
    assert all(gap == 2.0 for gap in clock.sleeps)
    assert len(renewed) == len(fake.requests)     # the lease is renewed before every request


# ── Allowlist ────────────────────────────────────────────────────────────


def test_allowlist_enumerates_the_pages_the_client_may_touch():
    assert nc.ALLOWED_GET_PATHS == frozenset({
        "/VendorResponse/ResponseList.aspx",
        "/Login.aspx",
        "/VendorLogin.aspx",
        "/VendorResponse/Bid/VResponseEvent.aspx",
        "/VendorResponse/Bid/VResponseBidAttachments.aspx",
        "/Extract.aspx",
    })
    assert nc.ALLOWED_POST_PATHS == frozenset({
        "/VendorLogin.aspx",
        "/VendorResponse/ResponseList.aspx",
        "/VendorResponse/Bid/VResponseBidAttachments.aspx",
    })
    for path in nc.ALLOWED_GET_PATHS:
        nc.assert_allowed_get(path)
    for path in nc.ALLOWED_POST_PATHS:
        nc.assert_allowed_post(path)
    for target in list(fx.PAGE_TARGETS.values()) + [
        f"{fx.PAGER_PREFIX}ctl10", f"{fx.PAGER_PREFIX}ctl11",
        f"{fx.ATTACHMENTS_PAGER_PREFIX}ctl05", ATTACH_PAGE_2_TARGET,
    ]:
        nc.assert_allowed_postback_target(target)
    assert [p.pattern for p in nc.ALLOWED_POSTBACK_TARGETS] == [
        r"^ctl00\$mainContent\$ucInvitedListGrid\$rgResponse\$ctl00\$ctl03\$ctl01\$ctl\d{2}$",
        r"^ctl00\$mainContent\$rgBidAttachments\$ctl00\$ctl03\$ctl01\$ctl\d{2}$",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/VendorResponse/Bid/VResponseSubmission.aspx",
        "/VendorResponse/Bid/VResponseQuestion.aspx",
        "/VendorResponse/Bid/VResponseAttachments.aspx",
        "/VendorResponse/Bid/VResponseLine.aspx",
        "/VendorResponse/Bid/VResponseAttribute.aspx",
        "/VendorResponse/Bid/VResponseActivity.aspx",
        "/VendorResponse/Bid/VResponseBidAudit.aspx",
        "/VendorResponse/Bid/VResponseDocumentList.aspx",
        "/VendorResponse/AuctionList.aspx",
        "/VendorAdmin/Profile.aspx",
        "/VendorAdmin/Vendor/BuyerProfile/ProfileCommodity.aspx",
        "/MfaPromptPopup.aspx",
        "/Error.aspx",
        "/BadRequest.aspx",
        "/VendorResponse/ResponseList.aspx/",
        "/vendorresponse/responselist.aspx",
        "/",
        "",
    ],
)
def test_allowlist_refuses_everything_else(path):
    with pytest.raises(ValueError):
        nc.assert_allowed_get(path)
    with pytest.raises(ValueError):
        nc.assert_allowed_post(path)


@pytest.mark.parametrize("path", ["/Login.aspx", "/VendorResponse/Bid/VResponseEvent.aspx", "/Extract.aspx"])
def test_get_only_pages_are_not_post_targets(path):
    nc.assert_allowed_get(path)
    with pytest.raises(ValueError):
        nc.assert_allowed_post(path)


@pytest.mark.parametrize(
    "target",
    [
        "",                                                                          # hfDocZipAction: __doPostBack('','')
        "ctl00$mainContent$ucNotInvitedListGrid$rgResponse$ctl00$ctl03$ctl01$ctl06",  # Other Bid Opportunities
        "ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl02$ctl02$Filter_colStatus",
        "ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl03$ctl01$ctl6",
        "ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl03$ctl01$PageSizeComboBox",
        "ctl00$mainContent$rgBidAttachments$ctl00$ctl04$lnkDownload",
        "ctl00$mainContent$ctlTabStrip$btnHiddenRetract",
        "chkAgree",
    ],
)
def test_postback_targets_outside_the_two_pagers_are_refused(target):
    with pytest.raises(ValueError):
        nc.assert_allowed_postback_target(target)


def test_requests_never_leave_the_portal_host_or_https(fake, clock):
    with _logged_in(fake, clock) as session:
        for url in (
            "https://evil.example.com/VendorResponse/ResponseList.aspx?e=x",
            f"http://{nc.HOST}/VendorResponse/ResponseList.aspx?e=x",
            f"https://{nc.HOST}.evil.example/VendorResponse/ResponseList.aspx?e=x",
            f"{BASE}/VendorResponse/Bid/VResponseSubmission.aspx?e=x",
        ):
            with pytest.raises(ValueError):
                session._request("GET", url)
        with pytest.raises(ValueError):
            session._request("POST", f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx?e=x", data={})
    assert fake.requests == []


# ── Store and cookies ────────────────────────────────────────────────────


def test_config_repr_never_shows_the_password():
    cfg = _config(password="s3cret-pw")
    text = repr(cfg) + str(cfg) + f"{cfg!r}" + f"{cfg}"
    assert "s3cret-pw" not in text and "password" not in text.lower()
    assert USERNAME in repr(cfg) and cfg.password == "s3cret-pw" and cfg.configured


def test_memory_store_mirrors_the_lock_policy():
    store = nc.MemorySessionStore(max_failures=2, lock_seconds=100, now=lambda: NOW)
    assert store.load() is None
    # An attempt that does not count toward the lock: stamped, not counted.
    state = store.record_login(ok=False, error="transient: 502", counts_toward_lock=False)
    assert state["last_login_attempt_at"] == NOW.isoformat() and state.get("login_failures", 0) == 0
    assert state["last_error"] == "transient: 502" and state.get("locked_until") is None
    assert store.record_login(ok=False, error="no")["login_failures"] == 1
    state = store.record_login(ok=False, error="no")
    assert state["login_failures"] == 2
    assert state["locked_until"] == (NOW + timedelta(seconds=100)).isoformat()
    store.save_cookies(USERNAME, [{"name": "procData", "value": "v"}])
    assert store.load()["locked_until"] is None and store.load()["login_failures"] == 0
    assert store.load()["account"] == USERNAME


def test_export_cookies_keeps_portal_cookies_only(fake, clock):
    with _logged_in(fake, clock) as session:
        session._load_store()
        session._client.cookies.set("tracker", "x", domain="evil.example.com", path="/")
        session._client.cookies.set("__cf_bm", "y", domain=".ionwave.net", path="/")
        exported = session._export_cookies()
    assert sorted(c["name"] for c in exported) == ["__cf_bm", "procData"]
    assert all(set(c) == {"name", "value", "domain", "path", "expires", "secure"} for c in exported)
    assert session._has_session()


def test_unlink_quietly_ignores_a_missing_file(tmp_path):
    nc._unlink_quietly(tmp_path / "missing")
    path = tmp_path / "there"
    path.write_bytes(b"x")
    nc._unlink_quietly(path)
    assert not path.exists()
