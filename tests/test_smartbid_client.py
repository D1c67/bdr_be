"""The SmartBid client (app/services/smartbid_client) driven through
httpx.MockTransport over the scrubbed captures in tests/fixtures_smartbid,
with an in-memory session store and the pace's sleep, clock and rng
patched so nothing waits (docs/RFP_SMARTBID.md sections 2, 3 and 8).

The fake platform reproduces what was captured 2026-10-01:

  POST apicc.smartinsight.co/token with the email's passport key -> 200
  JSON with the bearer token; with a wrong key (or another project's bid
  id) -> 500 text/html, the ASP.NET error page. The two project reads and
  the security-token POST answer only with that bearer token (else 401).
  The direct-URL lookup on apicc.smartbidnet.com answers only with the
  per-file security token (else 401) and returns one text/plain Azure blob
  URL; the blob answers with no cookies and no auth. The email's read
  receipt and open pixel answer 200 image/gif; the View the Project link
  answers 302 to the second hop, which the client never follows.

Pinned: reference parsing (st=105 preferred, `iR` links never chosen even
when they come first, either hex case, missing or malformed pieces -> None,
wrapped and bare links, the repr without the key); tracking parsing (both
pixels, the click, never the Yes / No anchors); the login (the exact form,
the key in one request body only, invalid_grant -> Forbidden without a
count, 5xx / transport -> Transient without a count, unexpected answers
counted to the lock with one bell, the min interval); the gate (every
captured answer open, each agreement shape refused, AlowedDetail); project
parsing over all four captures (folders, dedupe across PlanRoom and
SetPlanRoom, restricted inheritance, KB, bid_due_at for PT, CT and an
unknown zone); the download chain (Value cross-check, the direct-URL host
and path rules, the cap, html-as-file, 401 -> SessionExpired); the allow /
deny list over every URL in the emails and a route list; pings; the pace;
and no secret in any httpx log line.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, unquote_plus

import httpx
import pytest

from app.services import pipelinesuite_client as psc
from app.services import procore_client as pc
from app.services import smartbid_client as sbc
from tests import fixtures_smartbid as fx

NOW = datetime(2026, 10, 1, 21, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-10-01T21:00:00.000Z"
DOCX = b"PK\x03\x04" + b"d" * 4000
GIF = b"GIF89a" + b"\x00" * 37
API = "https://apicc.smartinsight.co"
BLOB_HOST = "azsblivenstorage.blob.core.windows.net"
SECOND_HOP = "https://securecc.smartbidnet.com/Login.aspx?UYBmHTV0hJh8BRyw.."
PROJECT_874974 = sbc.parse_project(fx.load_json(fx.BP_874974))
FILE_NAMES = {f["file_id"]: f["name"] for f in PROJECT_874974.files}
EXHIBIT_F = next(f for f in PROJECT_874974.files if f["file_id"] == fx.EXHIBIT_F_ID)


def _blob_url(file_id, name, bid=fx.BID):
    return (
        f"https://{BLOB_HOST}/sbproductionstorage/Files/System_{fx.SYSTEM_ID}/BidsProjectFiles/"
        f"BidProject_{bid}/{file_id}/{quote(name)}?sv=2018-03-28&sr=b&sig={fx.SAS_SIGNATURE}"
        "&st=2026-10-01T21%3A07%3A36Z&se=2026-10-01T23%3A08%3A36Z&sp=r"
    )


# ── The fake platform ────────────────────────────────────────────────────


def _raw_query(request: httpx.Request) -> dict[str, str]:
    """The query as sent, no `+` decoding (the security token is raw)."""
    out = {}
    for part in request.url.query.decode().split("&"):
        if "=" in part:
            name, value = part.split("=", 1)
            out[name] = value
    return out


class FakeSmartBid:
    """MockTransport handler for the SmartBid API, the file store and the
    email's tracking, with the failure modes the tests arm. Records every
    request so the sequence can be asserted."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.key = fx.KEY
        self.token_mode = "ok"        # ok | invalid_grant | 500 | 429 | transport | no_token | no_account | 401 | 400 | html
        self.gate_answer = fx.CA_OPEN[0]
        self.gate_mode = "ok"         # ok | 401 | 404 | 500 | junk
        self.project_mode = "ok"      # ok | 401 | 404 | 500 | junk | other_id
        self.sectoken_mode = "ok"     # ok | 401 | 500 | junk | 403
        self.direct_mode = "ok"       # ok | 401 | 404 | 500 | foreign | wrong_bid | wrong_file | http | html | quoted | junk
        self.blob_mode = "ok"         # ok | 404 | 403 | html | 500 | redirect | transport
        self.track_mode = "ok"        # ok | 404 | transport
        self.blob_bytes = DOCX
        self.direct_answer: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "apicc.smartinsight.co":
            return self._api(request, path)
        if host == "apicc.smartbidnet.com" and path == "/project/fileMgmt/download":
            return self._direct(request)
        if host == BLOB_HOST:
            return self._blob(request)
        if host in ("securecc.smartbidnet.com", "em.smartinsight.co"):
            return self._track(request, path)
        return httpx.Response(500, text=f"unexpected host {host}")

    @property
    def paths(self) -> list[tuple[str, str, str]]:
        return [(r.method, r.url.host, r.url.path) for r in self.requests]

    def _authorized(self, request):
        return request.headers.get("authorization") == f"Bearer {fx.BEARER}"

    def _api(self, request, path):
        if request.method == "POST" and path == "/token":
            return self._token(request)
        if not self._authorized(request):
            return httpx.Response(401, json={"Message": "Authorization has been denied for this request."})
        if request.method == "GET" and path == "/api/projects/getconfidentialagreement":
            if self.gate_mode in ("401", "404", "500"):
                return httpx.Response(int(self.gate_mode), text="no")
            if self.gate_mode == "junk":
                return httpx.Response(200, json=["not", "it"])
            return httpx.Response(200, json=fx.load_json(self.gate_answer))
        if request.method == "GET" and path == "/api/projects/getbidproject":
            if self.project_mode in ("401", "404", "500"):
                return httpx.Response(int(self.project_mode), text="no")
            if self.project_mode == "junk":
                return httpx.Response(200, text="<html>not json</html>", headers={"content-type": "text/html"})
            bid = parse_qs(request.url.query.decode())["bidProjectId"][0]
            name = {"874974": fx.BP_874974, "876398": fx.BP_876398, "874512": fx.BP_874512,
                    "876488": fx.BP_876488}[bid]
            if self.project_mode == "other_id":
                name = fx.BP_876398
            return httpx.Response(200, json=fx.load_json(name))
        if request.method == "POST" and path == "/api/admin/getSecurityToken":
            if self.sectoken_mode in ("401", "403", "500"):
                return httpx.Response(int(self.sectoken_mode), text="no")
            body = json.loads(request.content.decode())
            if not isinstance(body, str) or not body.endswith(STAMP):
                return httpx.Response(400, json={"Message": "bad"})
            if self.sectoken_mode == "junk":
                return httpx.Response(200, json={"token": "nested"})
            return httpx.Response(200, json=fx.SECURITY_TOKEN)
        return httpx.Response(404, json={"Message": "No HTTP resource was found."})

    def _token(self, request):
        if self.token_mode == "transport":
            raise httpx.ConnectError("token host unreachable", request=request)
        form = {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}
        if self.token_mode in ("500", "429") or form.get("key") != self.key:
            # The captured answer to a bad key: the ASP.NET error page.
            status = 429 if self.token_mode == "429" else 500
            return httpx.Response(status, text="<html><title>Runtime Error</title></html>",
                                  headers={"content-type": "text/html"})
        if self.token_mode == "invalid_grant":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "The key is invalid."})
        if self.token_mode == "400":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if self.token_mode == "401":
            return httpx.Response(401, json={"error": "nope"})
        if self.token_mode == "html":
            return httpx.Response(200, text="<html>maintenance</html>", headers={"content-type": "text/html"})
        answer = fx.load_json(fx.TOKEN_ANSWER)
        if self.token_mode == "no_token":
            answer.pop("access_token")
        if self.token_mode == "no_account":
            answer.pop("account_id")
        return httpx.Response(200, json=answer)

    def _direct(self, request):
        if not self._authorized(request):
            return httpx.Response(401, text="")
        query = _raw_query(request)
        if query.get("token") != fx.SECURITY_TOKEN or query.get("timestamp") != STAMP:
            return httpx.Response(401, text="")
        if self.direct_mode in ("401", "404", "500"):
            return httpx.Response(int(self.direct_mode), text="")
        file_id, bid, _system = base64.b64decode(query["Value"]).decode().split(".")
        url = self.direct_answer or _blob_url(file_id, FILE_NAMES.get(file_id, "x.pdf"), bid)
        if self.direct_mode == "foreign":
            url = url.replace(BLOB_HOST, "evil.example.com")
        elif self.direct_mode == "wrong_bid":
            url = url.replace(f"BidProject_{bid}", "BidProject_1")
        elif self.direct_mode == "wrong_file":
            url = url.replace(f"/{file_id}/", "/99999999/")
        elif self.direct_mode == "http":
            url = url.replace("https://", "http://")
        elif self.direct_mode == "html":
            return httpx.Response(200, text="<html>error</html>", headers={"content-type": "text/html"})
        elif self.direct_mode == "quoted":
            url = json.dumps(url)
        elif self.direct_mode == "junk":
            url = "not a url"
        return httpx.Response(200, text=url, headers={"content-type": "text/plain; charset=utf-8"})

    def _blob(self, request):
        if self.blob_mode == "transport":
            raise httpx.ReadTimeout("blob slow", request=request)
        if self.blob_mode in ("403", "404", "500"):
            return httpx.Response(int(self.blob_mode), text="<Error/>", headers={"content-type": "application/xml"})
        if self.blob_mode == "html":
            return httpx.Response(200, text="<html>not the file</html>", headers={"content-type": "text/html"})
        if self.blob_mode == "redirect":
            return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})
        return httpx.Response(200, content=self.blob_bytes, headers={
            "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "server": "Windows-Azure-Blob/1.0",
        })

    def _track(self, request, path):
        if self.track_mode == "transport":
            raise httpx.ReadTimeout("slow tracker", request=request)
        if self.track_mode == "404":
            return httpx.Response(404, text="gone")
        if path == "/Main/Login.aspx":
            return httpx.Response(302, headers={"location": SECOND_HOP})
        return httpx.Response(200, content=GIF, headers={"content-type": "image/gif"})


class Clock:
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
    monkeypatch.setattr(sbc, "_last_request_at", 0.0)


@pytest.fixture
def fake():
    return FakeSmartBid()


@pytest.fixture
def clock():
    return Clock()


REF = sbc.parse_reference(fx.load(fx.EMAIL_874974_TEXT))


def _store(**state):
    store = pc.MemorySessionStore(max_failures=3, lock_seconds=21600, now=lambda: NOW)
    store.state = dict(state)
    return store


def _config(**over):
    base = dict(min_request_interval=0.0, login_min_interval=30)
    base.update(over)
    return sbc.SmartBidConfig(**base)


def _session(fake, clock, store=None, config=None, ref=REF, **kw):
    return sbc.SmartBidSession(
        config or _config(),
        store if store is not None else _store(),
        ref,
        transport=httpx.MockTransport(fake),
        sleep=clock.sleep,
        clock=clock,
        rng=lambda a, b: 1.0,
        now=lambda: NOW,
        **kw,
    )


def _logged_in(fake, clock, **kw):
    session = _session(fake, clock, **kw)
    session.login()
    return session


# ── Reference parsing (pure) ─────────────────────────────────────────────


def test_parse_reference_over_the_captured_bodies():
    ref = sbc.parse_reference(fx.load(fx.EMAIL_874974_TEXT))
    assert ref == sbc.SmartBidRef(fx.BID, fx.COMM, fx.KEY, fx.VIEW_URL)
    assert ref.click_url == fx.VIEW_URL and "iR" not in ref.click_url
    assert ref.external_key == "smartbid:874974"
    assert ref.session_provider == "smartbid"
    assert ref.external_url == "https://gocc.smartbid.co/#/projectlist"
    assert ref.fingerprint == sbc.key_fingerprint(fx.KEY) and len(ref.fingerprint) == 16
    for name, bid in ((fx.EMAIL_876398_TEXT, "876398"), (fx.EMAIL_876488_TEXT, "876488")):
        other = sbc.parse_reference(fx.load(name))
        assert (other.bid_project_id, other.comm_detail_id, other.passport_key) == (
            bid, fx.comm_for(bid), fx.key_for(bid)
        )
        assert other.click_url.endswith(f"&sBidId={bid}&st=105&e=1")


def test_reference_repr_and_str_never_carry_the_key_or_the_link():
    for text in (repr(REF), str(REF), f"{REF}", repr([REF]), repr({"ref": REF})):
        assert fx.KEY not in text and "Login.aspx" not in text
        assert fx.BID in text and fx.COMM in text
    assert fx.KEY not in REF.fingerprint
    assert sbc.key_fingerprint("a") != sbc.key_fingerprint("b")
    tracking = sbc.parse_tracking(fx.load(fx.EMAIL_874974_HTML))
    assert fx.KEY not in repr(tracking)


def test_parse_reference_never_chooses_an_ir_link_even_when_it_comes_first():
    body = f"Yes <{fx.YES_URL}> No <{fx.NO_URL}> image <{fx.IMAGE_LINK_URL}>"
    ref = sbc.parse_reference(body)
    assert ref.click_url == fx.IMAGE_LINK_URL            # st=101: the only link without iR
    assert sbc.parse_reference(f"Yes <{fx.YES_URL}> No <{fx.NO_URL}>") is None
    # iR in any case, any value, anywhere in the query.
    for variant in ("IR=1", "ir=0", "Ir=2", "iR="):
        url = fx.VIEW_URL.replace("&st=105", f"&{variant}&st=105")
        assert sbc.parse_reference(url) is None, variant
    assert sbc.parse_reference(fx.VIEW_URL.replace("cId=", "iR=1&cId=")) is None


def test_parse_reference_prefers_st_105_over_the_first_link():
    body = f"{fx.IMAGE_LINK_URL}\n{fx.YES_URL}\n{fx.VIEW_URL}\n"
    assert sbc.parse_reference(body).click_url == fx.VIEW_URL
    assert sbc.parse_reference(fx.IMAGE_LINK_URL).click_url == fx.IMAGE_LINK_URL


def test_parse_reference_reads_wrapped_escaped_and_bare_links_in_either_hex_case():
    lower = fx.VIEW_URL.replace(fx.KEY, fx.KEY.lower())
    ref = sbc.parse_reference(f"Click Here to View the Project {lower} thanks")
    assert ref.passport_key == fx.KEY.lower() and ref.bid_project_id == fx.BID
    wrapped = (
        "https://nam09.safelinks.protection.outlook.com/?url=" + quote(fx.VIEW_URL, safe="")
        + "&data=DUMMY&sdata=DUMMY&reserved=0"
    )
    assert sbc.parse_reference(f"View <{wrapped}>").click_url == fx.VIEW_URL
    escaped = fx.VIEW_URL.replace("&", "&amp;")
    assert sbc.parse_reference(f'<a href="{escaped}">x</a>').click_url == fx.VIEW_URL
    assert sbc.parse_reference(fx.VIEW_URL.replace("Main/Login.aspx", "main/login.ASPX")) is not None


@pytest.mark.parametrize(
    "url",
    [
        fx.VIEW_URL.replace(f"cId=bp_{fx.COMM}&", ""),
        fx.VIEW_URL.replace(f"cId=bp_{fx.COMM}", f"cId={fx.COMM}"),
        fx.VIEW_URL.replace(f"cId=bp_{fx.COMM}", "cId=bp_1234567890123"),
        fx.VIEW_URL.replace(fx.KEY, fx.KEY[:-1]),
        fx.VIEW_URL.replace(fx.KEY, fx.KEY + "0"),
        fx.VIEW_URL.replace(fx.KEY, fx.KEY[:-1] + "G"),
        fx.VIEW_URL.replace(f"&sBidId={fx.BID}", ""),
        fx.VIEW_URL.replace(f"sBidId={fx.BID}", "sBidId=87x974"),
        fx.VIEW_URL.replace("https://", "http://"),
        fx.VIEW_URL.replace("securecc.smartbidnet.com", "securecc.smartbidnet.com.evil.example"),
        fx.VIEW_URL.replace("/Main/Login.aspx", "/Login.aspx"),
        fx.VIEW_URL.replace("/Main/Login.aspx", "/External/ViewOnDigitalBidBoard.aspx"),
        fx.VIEW_URL + "&utm=1",
        fx.VIEW_URL + f"&sBidId={fx.BID}",
        fx.VIEW_URL.replace("st=105", "st=abc"),
        fx.VIEW_URL + "#frag",
    ],
)
def test_parse_reference_refuses_missing_or_malformed_pieces(url):
    assert sbc.parse_reference(url) is None


def test_parse_reference_on_nothing():
    assert sbc.parse_reference(None) is None and sbc.parse_reference("") is None
    assert sbc.parse_reference("Please bid on our project.") is None
    no_view = re.sub(r"Click Here to View the Project<[^>]+>", "", fx.load(fx.EMAIL_874974_TEXT))
    # The image link (st=101, no iR) still names the project.
    assert sbc.parse_reference(no_view).click_url == fx.IMAGE_LINK_URL


# ── Tracking (pure) ──────────────────────────────────────────────────────


def test_parse_tracking_finds_both_pixels_and_the_view_link_only():
    tracking = sbc.parse_tracking(fx.load(fx.EMAIL_874974_HTML))
    assert tracking == sbc.Tracking(fx.READ_RECEIPT_URL, fx.OPEN_PIXEL_URL, fx.VIEW_URL)
    assert "safelinks" not in tracking.click_url and "iR" not in tracking.click_url
    assert sbc.parse_tracking(None) == sbc.Tracking(None, None, None)
    assert sbc.parse_tracking("<html><body>no links</body></html>") == sbc.Tracking(None, None, None)


def test_parse_tracking_never_returns_a_yes_or_no_anchor():
    html = f"""
    <a href="{fx.YES_URL}">Click Here to View the Project</a>
    <a href="{fx.VIEW_URL}">Yes, I'll Bid All Codes (view the project)</a>
    <a href="{fx.VIEW_URL}">No, I Won't Bid this Job - View the Project</a>
    <a href="https://example.com/view">Click Here to View the Project</a>
    """
    assert sbc.parse_tracking(html).click_url is None
    # Outlook's originalsrc is read when the href is not the link; the first
    # proper anchor wins; tags inside the anchor and nbsp are text.
    wrapped = "https://nam09.safelinks.protection.outlook.com/?url=" + quote(fx.NO_URL, safe="")
    html = f"""
    <a href="{wrapped}">No, I Won't Bid this Job</a>
    <a href="https://example.com/x" originalsrc="{fx.VIEW_URL.replace('&', '&amp;')}">
      <b>Click&nbsp;Here</b> to View   the Project</a>
    <a href="{fx.IMAGE_LINK_URL}">View the project again</a>
    <img src="https://securecc.smartbidnet.com/External/RequestReadReceipt.aspx?sCommunicationId=abc">
    <img src="https://em.smartinsight.co/wf/open">
    """
    tracking = sbc.parse_tracking(html)
    assert tracking == sbc.Tracking(None, None, fx.VIEW_URL)


# ── Allowlist and deny list ──────────────────────────────────────────────


_ATTR_RE = re.compile(r'\b(?:href|src|originalsrc)\s*=\s*"([^"]+)"', re.IGNORECASE)
_TEXT_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)


def _email_urls():
    """Every URL in the three email texts and the 874974 HTML, as written
    and unwrapped."""
    out = set()
    for name in (fx.EMAIL_874974_TEXT, fx.EMAIL_876398_TEXT, fx.EMAIL_876488_TEXT):
        out.update(_TEXT_URL_RE.findall(fx.load(name)))
    out.update(raw.replace("&amp;", "&") for raw in _ATTR_RE.findall(fx.load(fx.EMAIL_874974_HTML)))
    return out | {sbc.unwrap_link(u) for u in out}


def test_every_url_in_the_emails_is_refused_but_the_tracking_shapes():
    """The emails link to the Yes / No bid answers, the digital bid board,
    the access-key page, Unsubscribe, ConstructConnect's terms, images and
    the safelinks wrappers. The allowlist admits only the read receipt, the
    open pixel and the Login.aspx links WITHOUT iR (the View the Project
    link and the image above it), for GET only."""
    urls = _email_urls()
    assert len(urls) > 30
    admitted = set()
    for url in urls:
        with pytest.raises(ValueError):
            sbc.assert_allowed("POST", url)
        try:
            sbc.assert_allowed("GET", url)
            admitted.add(url)
        except ValueError as exc:
            assert "DEADBEEF" not in str(exc) and "upn" not in str(exc)
    expected = {fx.READ_RECEIPT_URL, fx.OPEN_PIXEL_URL}
    for bid in ("874974", "876398", "876488"):
        view = fx.VIEW_URL.replace(fx.KEY, fx.key_for(bid)).replace(fx.COMM, fx.comm_for(bid)).replace(
            f"sBidId={fx.BID}", f"sBidId={bid}"
        )
        expected |= {view, view.replace("st=105", "st=101")}
    assert admitted == expected
    for answer in (fx.YES_URL, fx.NO_URL):
        assert answer in urls
        with pytest.raises(ValueError):
            sbc.assert_allowed("GET", answer)
    assert fx.BIDBOARD_URL in urls and fx.UNSUBSCRIBE_URL in urls


@pytest.mark.parametrize(
    "method, url",
    [
        ("GET", fx.VIEW_URL),
        ("GET", fx.IMAGE_LINK_URL),
        ("GET", fx.READ_RECEIPT_URL),
        ("GET", f"https://securecc.smartbidnet.com/External/RequestReadReceipt.aspx?sCommunicationId={fx.COMM}"),
        ("GET", fx.OPEN_PIXEL_URL),
        ("GET", fx.OPEN_PIXEL_URL.replace("http://", "https://")),
        ("POST", f"{API}/token"),
        ("GET", f"{API}/api/projects/getconfidentialagreement?bidProjectId=874974&personId=20000001&isCcbc=false"),
        ("GET", f"{API}/api/projects/getbidproject?bidProjectId=874974&personId=20000001&bidProjectType=Invited&isIframe=false"),
        ("POST", f"{API}/api/admin/getSecurityToken"),
        ("GET", f"https://apicc.smartbidnet.com/project/fileMgmt/download?Value={fx.EXHIBIT_F_VALUE}&token={fx.SECURITY_TOKEN}&timestamp={STAMP}"),
        ("GET", fx.load(fx.DIRECT_URL_ANSWER).strip()),
    ],
)
def test_allowlist_admits_the_nine_shapes(method, url):
    sbc.assert_allowed(method, url)


@pytest.mark.parametrize(
    "method, url",
    [
        # The bid answer, every way it can be reached.
        ("GET", fx.YES_URL),
        ("GET", fx.NO_URL),
        ("POST", fx.VIEW_URL),
        ("POST", f"{API}/api/projects/setallcodesanswer"),
        ("GET", f"{API}/api/projects/setallcodesanswer?bidProjectId=874974"),
        ("GET", f"{API}/api/projects/linkwontbidthisjob?bidProjectId=874974&personId=1"),
        ("POST", f"{API}/api/projects/linkwontbidthisjob"),
        ("POST", f"{API}/api/projects/setisuploadedproposalfile"),
        # Any other API route, and the right routes with the wrong verb.
        ("GET", f"{API}/api/projects/getbidprojectlist?bidProjectId=874974"),
        ("GET", f"{API}/api/admin/getSecurityToken"),
        ("GET", f"{API}/token"),
        ("POST", f"{API}/api/projects/getbidproject?bidProjectId=874974"),
        ("POST", f"{API}/api/projects/getconfidentialagreement?bidProjectId=874974"),
        ("GET", f"{API}/api/projects/getbidproject?bidProjectId=874974&acceptAgreement=true"),
        ("GET", f"{API}/api/projects/getbidproject?bidProjectId=abc"),
        ("GET", f"{API}/api/projects/getbidproject"),
        ("POST", f"{API}/token?grant_type=x"),
        ("POST", "http://apicc.smartinsight.co/token"),
        # The email's other links.
        ("GET", fx.UNSUBSCRIBE_URL),
        ("GET", fx.BIDBOARD_URL),
        ("GET", "https://securecc.smartbidnet.com/LEHW?st=102"),
        ("GET", SECOND_HOP),
        ("GET", "https://securecc.smartbidnet.com/ImagesAtProject/Icons/ClickHereBids189.gif"),
        ("GET", f"https://gocc.smartbid.co/#/sbnpassport/874974?&goto=bp&cId=bp_{fx.COMM}&passportkey={fx.KEY}"),
        ("GET", "https://gocc.smartbid.co/"),
        ("GET", "https://www.constructconnect.com/terms-of-use"),
        ("GET", "https://nam09.safelinks.protection.outlook.com/?url=" + quote(fx.VIEW_URL, safe="")),
        ("GET", "http://sbn.cc/2qjmxcy"),
        # Near misses on the admitted shapes.
        ("GET", fx.VIEW_URL + "&x=1"),
        ("GET", fx.VIEW_URL.replace("https://", "http://")),
        ("GET", fx.VIEW_URL.replace("securecc.smartbidnet.com", "securecc.smartbidnet.com:8443")),
        ("GET", fx.VIEW_URL.replace("https://", "https://user:pw@")),
        ("GET", "https://securecc.smartbidnet.com/External/RequestReadReceipt.aspx?sCommunicationId=abc"),
        ("GET", "https://securecc.smartbidnet.com/External/RequestReadReceipt.aspx?sCommunicationId=1&x=2"),
        ("GET", "http://em.smartinsight.co/wf/open"),
        ("GET", "http://em.smartinsight.co/ls/click?upn=u001.x"),
        ("GET", "http://em.smartinsight.co.evil.example/wf/open?upn=u001.x"),
        ("GET", f"https://apicc.smartbidnet.com/project/fileMgmt/download?Value={fx.EXHIBIT_F_VALUE}"),
        ("GET", f"https://apicc.smartbidnet.com/project/fileMgmt/download?Value={fx.EXHIBIT_F_VALUE}&token=t&timestamp=s&x=1"),
        ("GET", f"https://apicc.smartbidnet.com/project/fileMgmt/upload?Value={fx.EXHIBIT_F_VALUE}&token=t&timestamp=s"),
        ("POST", f"https://apicc.smartbidnet.com/project/fileMgmt/download?Value={fx.EXHIBIT_F_VALUE}&token=t&timestamp=s"),
        ("GET", fx.load(fx.DIRECT_URL_ANSWER).strip().replace("https://", "http://")),
        ("GET", fx.load(fx.DIRECT_URL_ANSWER).strip().split("?")[0]),
        ("GET", fx.load(fx.DIRECT_URL_ANSWER).strip().replace("BidsProjectFiles", "Other")),
        ("GET", fx.load(fx.DIRECT_URL_ANSWER).strip().replace("blob.core.windows.net", "blob.core.windows.net.evil.example")),
        ("POST", fx.load(fx.DIRECT_URL_ANSWER).strip()),
        ("GET", "https://storage.procore.com/api/v5/files/x?sig=test"),
        ("GET", ""),
        ("GET", "not a url"),
    ],
)
def test_allowlist_refuses_everything_else(method, url):
    with pytest.raises(ValueError) as exc:
        sbc.assert_allowed(method, url)
    message = str(exc.value)
    assert "DEADBEEF" not in message and "sig=" not in message and "token=" not in message


def test_requests_check_the_allowlist_before_anything_leaves(fake, clock):
    with _session(fake, clock) as session:
        for method, url in (
            ("GET", fx.YES_URL),
            ("POST", f"{API}/api/projects/setallcodesanswer"),
            ("GET", f"{API}/api/projects/linkwontbidthisjob?bidProjectId=874974"),
            ("GET", fx.UNSUBSCRIBE_URL),
        ):
            with pytest.raises(ValueError):
                session._request(method, url)
    assert fake.requests == []


# ── Login ────────────────────────────────────────────────────────────────


def test_login_posts_the_passport_key_once_and_keeps_the_token_in_memory(fake, clock):
    store = _store()
    session = _session(fake, clock, store)
    with session:
        session.login()
        assert session.account_id == fx.ACCOUNT_ID and session.logged_in_this_session
        assert session._token == fx.BEARER
    assert fake.paths == [("POST", "apicc.smartinsight.co", "/token")]
    post = fake.requests[0]
    form = {k: v[0] for k, v in parse_qs(post.content.decode(), keep_blank_values=True).items()}
    assert form == {
        "grant_type": "passport_key", "bidid": fx.BID, "commdetailid": fx.COMM, "key": fx.KEY,
        "typepassportkey": "bidproject_passportkey", "bfId": "", "isIframe": "false",
    }
    assert post.headers["origin"] == "https://gocc.smartbid.co"
    assert post.headers["referer"] == "https://gocc.smartbid.co/"
    assert post.headers["content-type"] == "application/x-www-form-urlencoded"
    assert "Mozilla/5.0" in post.headers["user-agent"]
    # Nothing secret stored: the row says "passport", no cookies; failures cleared.
    assert store.state["account"] == "passport" and store.state["cookies"] == []
    assert store.state["logged_in_at"] == NOW.isoformat()
    assert store.state["last_login_attempt_at"] == NOW.isoformat()
    assert store.state["login_failures"] == 0 and store.state["locked_until"] is None
    dumped = repr(store.state) + repr(session)
    assert fx.KEY not in dumped and fx.BEARER not in dumped
    assert session._token is None                       # dropped on close
    assert session.provider == "smartbid"


def test_the_key_leaves_in_one_request_body_and_the_token_only_in_headers(fake, clock, tmp_path, monkeypatch):
    session = _logged_in(fake, clock)
    session.gate()
    session.get_project()
    _route_downloads(monkeypatch, fake)
    session.download(EXHIBIT_F, tmp_path / "f.bin", max_bytes=10_000)
    carrying_key = [r for r in fake.requests if fx.KEY in unquote_plus(r.content.decode(errors="ignore") + str(r.url))]
    assert [(r.method, r.url.path) for r in carrying_key] == [("POST", "/token")]
    with_bearer = [r.url.path for r in fake.requests if "authorization" in r.headers]
    assert with_bearer == [
        "/api/projects/getconfidentialagreement", "/api/projects/getbidproject",
        "/api/admin/getSecurityToken", "/project/fileMgmt/download",
    ]
    assert all(fx.BEARER not in str(r.url) for r in fake.requests)
    session.close()


def test_invalid_grant_is_forbidden_and_not_counted(fake, clock):
    fake.token_mode = "invalid_grant"
    store = _store()
    bells = []
    with _session(fake, clock, store, on_lock=lambda *a: bells.append(a)) as session:
        with pytest.raises(sbc.SmartBidForbidden) as exc:
            session.login()
    assert str(exc.value) == (
        "SmartBid rejected this email's project link (the invitation may have been "
        "withdrawn or the link expired)."
    )
    assert store.state == {} and bells == []


@pytest.mark.parametrize("mode", ["500", "429", "transport", "badkey"])
def test_token_5xx_429_and_transport_are_transient_and_never_counted(fake, clock, mode):
    """A dead or foreign link answers 500, exactly like an outage: one
    stale email must not pause every SmartBid harvest."""
    if mode == "badkey":
        fake.key = "0" * 40
    else:
        fake.token_mode = mode
    store = _store()
    for _ in range(5):
        with _session(fake, clock, store, config=_config(login_min_interval=10)) as session:
            with pytest.raises(sbc.SmartBidTransient) as exc:
                session.login()
    if mode in ("500", "badkey"):
        assert str(exc.value) == "SmartBid refused the project link or is down (HTTP 500)."
    elif mode == "transport":
        assert "ConnectError" in str(exc.value)
    assert store.state == {} and len(fake.requests) == 5


@pytest.mark.parametrize(
    "mode, needle",
    [
        ("no_token", "without an access token"),
        ("no_account", "without an account id"),
        ("401", "HTTP 401"),
        ("400", "HTTP 400"),
        ("html", "without an access token"),
    ],
)
def test_unexpected_login_answers_are_counted(fake, clock, mode, needle):
    fake.token_mode = mode
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(sbc.SmartBidLoginFailed) as exc:
        session.login()
    assert exc.value.step == "token" and needle in str(exc.value)
    assert store.state["login_failures"] == 1 and store.state["last_error"] == f"token: {exc.value}"
    assert session._token is None


def test_unexpected_answers_lock_after_n_with_one_bell_and_availability_says_so(fake, clock):
    fake.token_mode = "no_token"
    store = _store()
    bells = []
    session = _session(
        fake, clock, store, config=_config(login_min_interval=10),
        on_lock=lambda until, error, portal: bells.append((until, error, portal)),
    )
    with session:
        for _ in range(2):
            with pytest.raises(sbc.SmartBidLoginFailed):
                session.login()
            session._last_login_attempt = None            # another harvest, later
            store.state["last_login_attempt_at"] = (NOW - timedelta(minutes=5)).isoformat()
        assert session.availability() == (True, None, None)
        with pytest.raises(sbc.SmartBidLoginLocked) as locked:
            session.login()
        until = NOW + timedelta(seconds=21600)
        assert locked.value.locked_until == until
        assert str(locked.value) == "SmartBid logins are locked after repeated failures."
        assert store.state["login_failures"] == 3 and store.state["locked_until"] == until.isoformat()
        assert bells == [(until, "SmartBid answered the login without an access token.", None)]
        before = len(fake.requests)
        with pytest.raises(sbc.SmartBidLoginLocked):
            session.login()
        assert len(fake.requests) == before and len(bells) == 1
        assert session.availability() == (False, "SmartBid logins are locked after repeated failures.", until)


def test_a_bell_that_raises_never_masks_the_lock(fake, clock):
    fake.token_mode = "no_token"
    store = _store(login_failures=2)

    def boom(until, error, portal):
        raise RuntimeError("notifications down")

    with _session(fake, clock, store, on_lock=boom) as session, pytest.raises(sbc.SmartBidLoginLocked):
        session.login()
    assert store.state["login_failures"] == 3


def test_an_expired_lock_no_longer_stops_a_login(fake, clock):
    store = _store(locked_until=(NOW - timedelta(seconds=1)).isoformat(), login_failures=3)
    with _session(fake, clock, store) as session:
        assert session.availability() == (True, None, None)
        session.login()
    assert store.state["login_failures"] == 0


def test_logins_inside_the_min_interval_are_refused_without_a_request(fake, clock):
    store = _store()
    with _session(fake, clock, store) as session:
        session.login()
        with pytest.raises(sbc.SmartBidUnavailable) as exc:
            session.login()
    assert "attempted recently" in str(exc.value)
    assert exc.value.locked_until == NOW + timedelta(seconds=30)
    assert len(fake.requests) == 1
    # An attempt another worker recorded defers this one too.
    fake.requests.clear()
    other = _store(last_login_attempt_at=(NOW - timedelta(seconds=12)).isoformat())
    with _session(fake, clock, other) as session, pytest.raises(sbc.SmartBidUnavailable) as exc:
        session.login()
    assert exc.value.locked_until == NOW + timedelta(seconds=18) and fake.requests == []
    # A transient failure is not stored but still spaces this session's attempts.
    fake.token_mode = "500"
    with _session(fake, clock, _store()) as session:
        with pytest.raises(sbc.SmartBidTransient):
            session.login()
        with pytest.raises(sbc.SmartBidUnavailable):
            session.login()
    assert len(fake.requests) == 1


# ── The gate ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("answer", list(fx.CA_OPEN) + [fx.CA_GENERAL_ACCEPTED])
def test_the_gate_is_open_over_every_captured_answer(fake, clock, answer):
    fake.gate_answer = answer
    with _logged_in(fake, clock) as session:
        result = session.gate()
    assert result["BidProjectConfidentialAgreement"]["ConfidentialityAgreement"] in (False, True)
    gate = fake.requests[-1]
    assert gate.method == "GET" and gate.url.path == "/api/projects/getconfidentialagreement"
    assert parse_qs(gate.url.query.decode()) == {
        "bidProjectId": [fx.BID], "personId": [fx.ACCOUNT_ID], "isCcbc": ["false"],
    }
    assert gate.headers["authorization"] == f"Bearer {fx.BEARER}"
    assert gate.headers["origin"] == "https://gocc.smartbid.co"


@pytest.mark.parametrize(
    "answer, sentence",
    [
        (fx.CA_GENERAL, "needs a confidentiality agreement accepted in SmartBid first"),
        (fx.CA_SPECIFIC, "needs a confidentiality agreement accepted in SmartBid first"),
        (fx.CA_PAD, "needs a confidentiality agreement accepted in SmartBid first"),
        (fx.CA_NOT_ALLOWED, "SmartBid does not allow this project: Your company is not on the bidders list for this project."),
    ],
)
def test_the_gate_refuses_every_agreement_shape_without_accepting_anything(fake, clock, answer, sentence):
    fake.gate_answer = answer
    with _logged_in(fake, clock) as session, pytest.raises(sbc.SmartBidForbidden) as exc:
        session.gate()
    assert sentence in str(exc.value)
    # Read only: the token, then one GET. Nothing posted, nothing accepted.
    assert fake.paths == [
        ("POST", "apicc.smartinsight.co", "/token"),
        ("GET", "apicc.smartinsight.co", "/api/projects/getconfidentialagreement"),
    ]


def test_agreement_refusal_reads_each_shape_fail_safe():
    open_ = fx.load_json(fx.CA_OPEN[0])
    assert sbc.agreement_refusal(open_) is None
    assert sbc.agreement_refusal(None) == sbc._MSG_DATA
    assert sbc.agreement_refusal({"AlowedDetail": ""}) == sbc._MSG_DATA

    def with_(**over):
        doc = json.loads(json.dumps(open_))
        head = over.pop("head", {})
        doc["BidProjectConfidentialAgreement"].update(head)
        doc.update(over)
        return doc

    # A general agreement with no person row at all: owed (ask a human).
    assert sbc.agreement_refusal(with_(head={"ConfidentialityAgreement": True})) == sbc._MSG_AGREEMENT
    for status in (1, 3, 6):
        accepted = with_(head={"ConfidentialityAgreement": True},
                         ConfidentialAgreementPerson=[{"typeCA": 1, "StatusCA": status}])
        assert sbc.agreement_refusal(accepted) is None
    wrong_type = with_(head={"ConfidentialityAgreement": True},
                       ConfidentialAgreementPerson=[{"typeCA": 2, "StatusCA": 1}])
    assert sbc.agreement_refusal(wrong_type) == sbc._MSG_AGREEMENT
    # Specific, mode 2: only when the sub is in a code that needs it; a
    # missing flag counts as yes.
    assert sbc.agreement_refusal(with_(head={"ConfidentialityAgreementSCA": 2})) == sbc._MSG_AGREEMENT
    assert sbc.agreement_refusal(with_(head={"ConfidentialityAgreementSCA": 2, "subIsInCodeWithSCA": False})) is None
    assert sbc.agreement_refusal(with_(head={"ConfidentialityAgreementSCA": 2}, subIsInCodeWithSCA=True)) == sbc._MSG_AGREEMENT
    assert sbc.agreement_refusal(with_(
        head={"ConfidentialityAgreementSCA": 1}, ConfidentialAgreementPerson=[{"typeCA": 2, "StatusCA": 6}]
    )) is None
    # AlowedDetail is shown as text, tags dropped, capped, one period.
    detail = sbc.agreement_refusal(with_(AlowedDetail="<b>Closed</b> to&nbsp;new bidders."))
    assert detail == "SmartBid does not allow this project: Closed to new bidders."
    long = sbc.agreement_refusal(with_(AlowedDetail="x" * 900))
    assert len(long) < 300


@pytest.mark.parametrize(
    "mode, exc_type", [("401", sbc.SmartBidSessionExpired), ("404", sbc.SmartBidForbidden),
                       ("500", sbc.SmartBidTransient), ("junk", sbc.SmartBidUnavailable)],
)
def test_the_gate_maps_its_other_answers(fake, clock, mode, exc_type):
    fake.gate_mode = mode
    with _logged_in(fake, clock) as session, pytest.raises(exc_type):
        session.gate()


def test_project_reads_without_a_token_are_session_expired_before_any_request(fake, clock):
    with _session(fake, clock) as session:
        for call in (session.gate, session.get_project):
            with pytest.raises(sbc.SmartBidSessionExpired):
                call()
    assert fake.requests == []


# ── The project ──────────────────────────────────────────────────────────


def test_get_project_returns_the_answer_and_learns_the_system(fake, clock):
    store = _store()
    with _logged_in(fake, clock, store=store) as session:
        body = session.get_project()
        assert body["BidProject"]["BidProjectId"] == 874974
        assert session.project_system_id == "3766"
    read = fake.requests[-1]
    assert parse_qs(read.url.query.decode()) == {
        "bidProjectId": [fx.BID], "personId": [fx.ACCOUNT_ID],
        "bidProjectType": ["Invited"], "isIframe": ["false"],
    }
    assert store.state["last_used_at"] == NOW.isoformat()


@pytest.mark.parametrize(
    "mode, exc_type, needle",
    [
        ("401", sbc.SmartBidSessionExpired, ""),
        ("404", sbc.SmartBidForbidden, "SmartBid has no project 874974 for this email's project link."),
        ("500", sbc.SmartBidTransient, "answered 500"),
        ("junk", sbc.SmartBidUnavailable, "does not understand"),
        ("other_id", sbc.SmartBidUnavailable, "does not understand"),
    ],
)
def test_get_project_maps_its_other_answers(fake, clock, mode, exc_type, needle):
    fake.project_mode = mode
    with _logged_in(fake, clock) as session, pytest.raises(exc_type) as exc:
        session.get_project()
    assert needle in str(exc.value)


def test_parse_project_over_the_874974_capture():
    project = PROJECT_874974
    assert project.bid_project_id == 874974 and project.system_id == 3766
    assert project.title == "Nevada State University @ NLV Gateway"
    assert project.gc_name == "DC Building Group, LLC."
    assert (project.manager, project.phone, project.fax) == ("Nicole Burguin", "(702) 434-9991 x214", "(702) 243-5556")
    assert (project.address1, project.address2, project.city, project.state, project.zip) == (
        "800 E Lake Mead Blvd", None, "North Las Vegas", "NV", "89030"
    )
    assert (project.owner, project.architect, project.project_status) == (None, "SCA Design", "Open to Bid")
    assert project.bid_due_local == "2026-10-01T17:00:00" and project.time_zone_short == "CT"
    assert project.bid_due_text == "10-01-2026 5:00 PM"
    assert sbc.bid_due_at(project) == "2026-10-01T17:00:00-05:00"
    assert project.past_due is False and project.allow_late_proposal is False and project.pre_bid is None
    assert project.invitations == [{
        "code": "26 00 00", "name": "Electrical", "package": "26 00 00 - Electrical",
        "status": "Accepted", "invited_on": "2026-09-21T12:06:03.643",
    }]
    assert project.response_recorded is True
    assert project.description_html.startswith("<p")
    files = project.files
    assert len(files) == 45 and len({f["file_id"] for f in files}) == 45
    folders = {f["folder"] for f in files}
    assert folders == {
        "Additional Information 9.28.26", "DCBG Contract Terms & Conditions", "DCBG ITB",
        "Owner Bid Form", "RFI's", "RFI's/MASTER RFI RESPONSE LIST", "Schedule", "Shell Bid Set",
        "Shell Bid Set/REVISED CIVILS 9.24.26", "TI Bid Set",
    }
    assert not any(f["folder"].startswith("root") for f in files)
    # Depth-first in order: a folder's subfolder before the files beside it.
    rfi = [f["folder"] for f in files if f["folder"].startswith("RFI's")]
    assert rfi[:5] == ["RFI's/MASTER RFI RESPONSE LIST"] * 5 and rfi[5:] == ["RFI's"] * 9
    assert EXHIBIT_F == {
        "file_id": fx.EXHIBIT_F_ID, "name": fx.EXHIBIT_F_NAME, "folder": "DCBG Contract Terms & Conditions",
        "size_kb": 15, "ext": "docx", "uploaded_on": EXHIBIT_F["uploaded_on"], "version": 1,
        "href": f"https://apicc.smartbidnet.com/project/fileMgmt/download?Value={fx.EXHIBIT_F_VALUE}",
        "value": fx.EXHIBIT_F_VALUE, "restricted": False,
    }
    assert {f["ext"] for f in files} == {"pdf", "docx", "xlsx"}
    assert sum(f["size_kb"] for f in files) == 763_174
    assert not any(f["restricted"] for f in files)
    for f in files:
        assert f["value"] == sbc.file_value(f["file_id"], 874974, 3766)
    assert fx.KEY not in repr(project)


def test_parse_project_over_the_other_captures():
    small = sbc.parse_project(fx.load_json(fx.BP_876398))
    assert small.gc_name == "Martin-Harris Construction- LV" and small.time_zone_short == "PT"
    assert sbc.bid_due_at(small) == "2026-10-29T11:00:00-07:00"
    assert small.response_recorded is False and [i["status"] for i in small.invitations] == ["Invited"]
    assert [f["folder"] for f in small.files] == ["Bid Documents"] * 3 + ["Scope Sheets"] * 4
    assert [f["size_kb"] for f in small.files][:3] == [975, 21483, 228]
    addenda = sbc.parse_project(fx.load_json(fx.BP_874512))
    assert len(addenda.files) == 19 and addenda.gc_name == "R&O Construction"
    assert addenda.files[0]["folder"] == "Addenda NSU Gateway Site 3/Addendum 1 NSU Gateway Site 3"
    assert sum(f["size_kb"] for f in addenda.files) == 757_096
    foods = sbc.parse_project(fx.load_json(fx.BP_876488))
    assert len(foods.files) == 6 and foods.files[0]["folder"] == "General Documents/Contractors Rules & Regulations"
    for project, bid in ((small, 876398), (addenda, 874512), (foods, 876488)):
        assert project.bid_project_id == bid
        for f in project.files:
            assert f["value"] == sbc.file_value(f["file_id"], bid, project.system_id)


def _node(name, *, folders=None, files=None, **over):
    node = {"Name": name, "isFile": False, "Folders": folders or [], "SCARequired": False, "PQRequired": False}
    if files is not None:
        node["Files"] = files
    node.update(over)
    return node


def _file(file_id, name, size=10, **over):
    value = sbc.file_value(file_id, 1, 2)
    node = {
        "Name": name, "isFile": True, "FileId": file_id, "Size": size, "FileIcon": None,
        "Href": f"https://apicc.smartbidnet.com/project/fileMgmt/download?Value={value}",
        "UploadedOn": "2026-09-01T10:00:00", "Version": 2, "SCARequired": False, "PQRequired": False,
    }
    node.update(over)
    return node


def test_parse_project_dedupes_across_both_rooms_and_inherits_restrictions():
    data = {
        "BidProject": {"BidProjectId": 1, "SystemId": 2, "PreBidMeetingDate": "10/05/2026 09:00 AM",
                       "PreBidMeetingTimeZone": "(PT)", "IsPreBidMeetingMandatory": True,
                       "BidDueDate": "0001-01-01T00:00:00", "TimeZoneShort": ""},
        "PlanRoom": [_node("root", folders=[
            _node("Specs ", folders=[_file(11, "A.pdf"), _file(12, "B.PDF", size=None)]),
            _node("NDA only", SCARequired=True, folders=[
                _node("Deep", folders=[_file(13, "C.pdf")]),
            ]),
            _file(14, "D.pdf", PQRequired=True),
            _file(15, "no-href.pdf", Href=None),
            {"Name": "no id", "isFile": True, "Href": "https://apicc.smartbidnet.com/x"},
            "junk",
        ])],
        "SetPlanRoom": [_node("root", folders=[
            _node("Set 1", files=[_file(11, "A.pdf"), _file(16, "E.dwg")]),
        ])],
    }
    project = sbc.parse_project(data)
    assert [(f["file_id"], f["folder"], f["restricted"]) for f in project.files] == [
        ("11", "Specs", False), ("12", "Specs", False), ("13", "NDA only/Deep", True),
        ("14", "", True), ("16", "Set 1", False),
    ]
    assert project.files[1]["size_kb"] is None and project.files[1]["ext"] == "pdf"
    assert project.files[4]["ext"] == "dwg" and project.files[4]["version"] == 2
    assert project.pre_bid == {"date": "10/05/2026 09:00 AM", "time_zone": "PT", "mandatory": True}
    assert project.bid_due_local is None and sbc.bid_due_at(project) is None
    empty = sbc.parse_project(None)
    assert empty.bid_project_id == 0 and empty.files == [] and empty.title is None
    assert sbc.parse_project({"BidProject": "x", "PlanRoom": "y"}).files == []


def test_bid_due_at_takes_the_stated_zone_and_assumes_pacific_otherwise():
    def at(local, zone):
        p = sbc.parse_project({"BidProject": {"BidProjectId": 1, "BidDueDate": local, "TimeZoneShort": zone}})
        return sbc.bid_due_at(p)

    assert at("2026-10-01T17:00:00", "(PT)") == "2026-10-01T17:00:00-07:00"
    assert at("2026-12-01T17:00:00", "(PT)") == "2026-12-01T17:00:00-08:00"
    assert at("2026-10-01T17:00:00", "(CT)") == "2026-10-01T17:00:00-05:00"
    assert at("2026-10-01T17:00:00", "(MT)") == "2026-10-01T17:00:00-06:00"
    assert at("2026-10-01T17:00:00", "(MST)") == "2026-10-01T17:00:00-07:00"
    assert at("2026-10-01T17:00:00", "(AZ)") == "2026-10-01T17:00:00-07:00"
    assert at("2026-10-01T17:00:00", "(ET)") == "2026-10-01T17:00:00-04:00"
    assert at("2026-10-01T17:00:00.500", "(HT)") == "2026-10-01T17:00:00-10:00"
    assert at("2026-10-01T17:00:00", "(XYZ)") == "2026-10-01T17:00:00-07:00"
    assert at("2026-10-01T17:00:00", None) == "2026-10-01T17:00:00-07:00"
    assert at("someday", "(PT)") is None and at(None, "(PT)") is None
    assert sbc.bid_due_zone("CT") == ("America/Chicago", False)
    assert sbc.bid_due_zone("(XYZ)") == ("America/Los_Angeles", True)
    assert sbc.bid_due_zone(None) == ("America/Los_Angeles", True)
    assert sbc.classify_name is psc.classify_name


# ── Downloads ────────────────────────────────────────────────────────────


def _route_downloads(monkeypatch, fake):
    """The production download client (a fresh httpx.Client with no jar and
    no auth) routed to the fake, so the cookie-free property is the real
    code's."""
    real_client = httpx.Client

    def routed(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(fake))
        return real_client(**kwargs)

    monkeypatch.setattr(sbc.httpx, "Client", routed)


@pytest.fixture
def downloader(fake, clock, monkeypatch):
    session = _logged_in(fake, clock)
    session.get_project()
    fake.requests.clear()
    _route_downloads(monkeypatch, fake)
    yield session
    session.close()


def test_download_runs_the_three_requests_and_streams_the_blob_without_auth(downloader, fake, tmp_path):
    dest = tmp_path / "0001.bin"
    written = downloader.download(EXHIBIT_F, dest, max_bytes=len(DOCX))
    assert written == len(DOCX) and dest.read_bytes() == DOCX
    assert fake.paths == [
        ("POST", "apicc.smartinsight.co", "/api/admin/getSecurityToken"),
        ("GET", "apicc.smartbidnet.com", "/project/fileMgmt/download"),
        ("GET", BLOB_HOST, f"/sbproductionstorage/Files/System_3766/BidsProjectFiles/BidProject_874974/{fx.EXHIBIT_F_ID}/{fx.EXHIBIT_F_NAME}"),
    ]
    token_post, lookup, blob = fake.requests
    assert json.loads(token_post.content) == fx.EXHIBIT_F_VALUE + STAMP
    assert token_post.headers["content-type"] == "application/json; charset=utf-8"
    assert token_post.headers["authorization"] == f"Bearer {fx.BEARER}"
    assert _raw_query(lookup) == {"Value": fx.EXHIBIT_F_VALUE, "token": fx.SECURITY_TOKEN, "timestamp": STAMP}
    assert "authorization" not in blob.headers and "cookie" not in blob.headers
    assert "origin" not in blob.headers
    assert f"sig={fx.SAS_SIGNATURE}" in str(blob.url)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(file_id="53529896"),
        lambda e: e.update(href=e["href"].replace(fx.EXHIBIT_F_VALUE, sbc.file_value(53529895, 874975, 3766))),
        lambda e: e.update(href=e["href"].replace("apicc.smartbidnet.com", "evil.example.com")),
        lambda e: e.update(href=e["href"].replace("/download", "/other")),
        lambda e: e.update(href=e["href"].replace("https://", "http://")),
        lambda e: e.update(value="bm90IGl0"),
        lambda e: e.update(href=None),
        lambda e: e.update(file_id=None),
    ],
)
def test_download_cross_checks_the_value_before_any_request(downloader, fake, tmp_path, mutate):
    entry = dict(EXHIBIT_F)
    mutate(entry)
    dest = tmp_path / "x.bin"
    with pytest.raises(sbc.SmartBidForbidden) as exc:
        downloader.download(entry, dest, max_bytes=10_000)
    assert str(exc.value) == "The SmartBid file link does not belong to this project."
    assert fake.requests == [] and not dest.exists()
    with pytest.raises(sbc.SmartBidForbidden):
        downloader.download("https://apicc.smartbidnet.com/x", dest, max_bytes=10)


def test_download_needs_the_projects_system(fake, clock, tmp_path, monkeypatch):
    session = _logged_in(fake, clock)                   # get_project never ran
    session.token_system_id = None
    with session, pytest.raises(sbc.SmartBidForbidden):
        session.download(EXHIBIT_F, tmp_path / "x.bin", max_bytes=10)


@pytest.mark.parametrize("mode", ["foreign", "wrong_bid", "wrong_file", "http", "html", "junk"])
def test_download_refuses_a_direct_url_off_the_project(downloader, fake, tmp_path, mode):
    fake.direct_mode = mode
    dest = tmp_path / "x.bin"
    with pytest.raises(sbc.SmartBidForbidden) as exc:
        downloader.download(EXHIBIT_F, dest, max_bytes=10_000)
    assert str(exc.value) == "SmartBid answered with a file location the harvest does not accept."
    assert [h for _, h, _ in fake.paths] == ["apicc.smartinsight.co", "apicc.smartbidnet.com"]
    assert not dest.exists()


def test_download_accepts_a_json_quoted_direct_url(downloader, fake, tmp_path):
    fake.direct_mode = "quoted"
    assert downloader.download(EXHIBIT_F, tmp_path / "x.bin", max_bytes=10_000) == len(DOCX)


@pytest.mark.parametrize(
    "mode, exc_type, needle",
    [
        ("404", sbc.SmartBidForbidden, "refused the file (404)"),
        ("403", sbc.SmartBidForbidden, "refused the file (403)"),
        ("html", sbc.SmartBidForbidden, "a page instead of the file"),
        ("redirect", sbc.SmartBidForbidden, "redirected"),
        ("500", sbc.SmartBidTransient, "answered 500"),
        ("transport", sbc.SmartBidTransient, "ReadTimeout"),
    ],
)
def test_download_maps_the_blob_answers(downloader, fake, tmp_path, mode, exc_type, needle):
    fake.blob_mode = mode
    dest = tmp_path / "x.bin"
    with pytest.raises(exc_type) as exc:
        downloader.download(EXHIBIT_F, dest, max_bytes=10_000)
    assert needle in str(exc.value) and not dest.exists()
    assert "sig=" not in str(exc.value) and fx.SECURITY_TOKEN not in str(exc.value)
    assert not any(r.url.host == "elsewhere.example" for r in fake.requests)


def test_download_enforces_the_byte_cap_and_unlinks(downloader, fake, tmp_path):
    dest = tmp_path / "x.bin"
    with pytest.raises(sbc.SmartBidForbidden) as exc:
        downloader.download(EXHIBIT_F, dest, max_bytes=len(DOCX) - 1)
    assert "larger than the harvest accepts" in str(exc.value)
    assert not dest.exists()


@pytest.mark.parametrize("where", ["sectoken", "direct"])
def test_download_401_is_session_expired_and_unlinks(downloader, fake, tmp_path, where):
    if where == "sectoken":
        fake.sectoken_mode = "401"
    else:
        fake.direct_mode = "401"
    dest = tmp_path / "x.bin"
    with pytest.raises(sbc.SmartBidSessionExpired):
        downloader.download(EXHIBIT_F, dest, max_bytes=10_000)
    assert not dest.exists() and not any(r.url.host == BLOB_HOST for r in fake.requests)


@pytest.mark.parametrize("mode, exc_type", [("500", sbc.SmartBidTransient), ("403", sbc.SmartBidForbidden),
                                            ("junk", sbc.SmartBidForbidden)])
def test_download_maps_the_security_token_answers(downloader, fake, tmp_path, mode, exc_type):
    fake.sectoken_mode = mode
    with pytest.raises(exc_type):
        downloader.download(EXHIBIT_F, tmp_path / "x.bin", max_bytes=10_000)


def test_download_refuses_to_overwrite_an_existing_file(downloader, fake, tmp_path):
    dest = tmp_path / "x.bin"
    dest.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        downloader.download(EXHIBIT_F, dest, max_bytes=10_000)
    assert fake.requests == [] and dest.read_bytes() == b"keep"


# ── Pings ────────────────────────────────────────────────────────────────


def test_ping_hits_each_tracking_shape_once_without_following_or_cookies(downloader, fake):
    assert downloader.ping(fx.READ_RECEIPT_URL) == 200
    assert downloader.ping(fx.OPEN_PIXEL_URL) == 200
    assert downloader.ping(fx.VIEW_URL) == 302
    wrapped = "https://nam09.safelinks.protection.outlook.com/?url=" + quote(fx.VIEW_URL, safe="") + "&data=DUMMY"
    assert downloader.ping(wrapped) == 302
    assert fake.paths == [
        ("GET", "securecc.smartbidnet.com", "/External/RequestReadReceipt.aspx"),
        ("GET", "em.smartinsight.co", "/wf/open"),
        ("GET", "securecc.smartbidnet.com", "/Main/Login.aspx"),
        ("GET", "securecc.smartbidnet.com", "/Main/Login.aspx"),
    ]
    assert all("cookie" not in r.headers and "authorization" not in r.headers for r in fake.requests)
    assert not any(r.url.path == "/Login.aspx" for r in fake.requests)     # the 302 is never followed
    assert str(fake.requests[3].url) == fx.VIEW_URL


def test_ping_answers_none_on_transport_trouble_and_refuses_everything_else(downloader, fake):
    fake.track_mode = "transport"
    assert downloader.ping(fx.OPEN_PIXEL_URL) is None
    fake.track_mode = "404"
    assert downloader.ping(fx.READ_RECEIPT_URL) == 404
    before = len(fake.requests)
    for url in (
        fx.YES_URL, fx.NO_URL, fx.UNSUBSCRIBE_URL, fx.BIDBOARD_URL, SECOND_HOP,
        f"{API}/api/projects/getbidproject?bidProjectId=874974",
        fx.load(fx.DIRECT_URL_ANSWER).strip(),
        "https://nam09.safelinks.protection.outlook.com/?url=" + quote(fx.YES_URL, safe=""),
    ):
        with pytest.raises(ValueError):
            downloader.ping(url)
    assert len(fake.requests) == before


# ── Pace ─────────────────────────────────────────────────────────────────


def test_pace_waits_at_least_the_interval_and_has_its_own_state(monkeypatch):
    clock = Clock()
    sbc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    sbc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0]
    clock.t += 0.5
    sbc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.75)
    assert clock.sleeps == [2.0, 3.0]
    clock.t += 10
    sbc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    sbc._pace(0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 2.0)
    assert clock.sleeps == [2.0, 3.0]
    monkeypatch.setattr(pc, "_last_request_at", 5000.0)
    monkeypatch.setattr(psc, "_last_request_at", 6000.0)
    sbc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert pc._last_request_at == 5000.0 and psc._last_request_at == 6000.0


def test_every_request_is_paced(fake, clock, tmp_path):
    session = _session(fake, clock, config=_config(min_request_interval=2.0))
    session._download_client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(fake), follow_redirects=False
    )
    with session:
        session.ping(fx.READ_RECEIPT_URL)
        session.login()
        session.gate()
        session.get_project()
        session.download(EXHIBIT_F, tmp_path / "a.bin", max_bytes=10_000)
    assert len(fake.requests) == 7
    assert len(clock.sleeps) == len(fake.requests) - 1
    assert all(gap == 2.0 for gap in clock.sleeps)


# ── Secrets in logs, fixtures and code ───────────────────────────────────


def test_no_secret_reaches_any_log_line(fake, clock, tmp_path, monkeypatch, caplog):
    """httpx logs every request URL at INFO; the client's filter drops the
    query of every SmartBid URL first (the click ping carries the passport
    key, the lookup the security token, the blob the SAS signature)."""
    caplog.set_level(logging.DEBUG)
    session = _logged_in(fake, clock)
    session.gate()
    session.get_project()
    _route_downloads(monkeypatch, fake)
    session.download(EXHIBIT_F, tmp_path / "f.bin", max_bytes=10_000)
    session.ping(fx.VIEW_URL)
    session.ping(fx.OPEN_PIXEL_URL)
    session.close()
    fake.token_mode = "no_token"
    with _session(fake, clock) as other, pytest.raises(sbc.SmartBidLoginFailed):
        other.login()
    assert "HTTP Request" in caplog.text and "?<redacted>" in caplog.text
    for secret in (fx.KEY, fx.BEARER, fx.SECURITY_TOKEN, fx.SAS_SIGNATURE, "sig=", "u001.DUMMYOPEN"):
        assert secret not in caplog.text, secret
        assert not any(secret in str(r.args) for r in caplog.records), secret
    # Other hosts' records pass untouched.
    assert sbc._redacted("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert sbc._redacted(httpx.URL(fx.VIEW_URL)) == "https://securecc.smartbidnet.com/Main/Login.aspx?<redacted>"
    assert sbc._redacted(7) == 7
    sbc._install_log_redaction()
    assert sum(1 for f in logging.getLogger("httpx").filters if getattr(f, "bdr_smartbid_redact", False)) == 1


# The live capture's keys, tokens and signature fragments, assembled so this
# file is not itself a hit for them.
_REAL_NEEDLES = tuple("".join(parts) for parts in (
    ("8FEDB5", "6EC8B2B272"), ("827F11", "D786A7CF"), ("D45A35", "2A1BFF89"), ("6D8B46", "EF5EB5C8"),
    ("49b732", "a6c06c182"), ("c4e825", "330c6e182"), ("c4ec90", "27216e182"),
    ("16087", "67482"), ("16081", "61163"), ("16083", "57803"), ("1891", "7961"), ("1784", "1092"),
    ("QJ%2BM", "Gj37rq"), ("OODRK", "wYDHvx"), ("vmad", "rid@"),
))
_HEX40_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")


def test_no_fixture_or_source_carries_a_real_key_token_or_signature():
    for name in fx.ALL_FILES:
        text = fx.load(name)
        for needle in _REAL_NEEDLES:
            assert needle not in text, (name, needle)
        for hit in _HEX40_RE.findall(text):
            assert hit.startswith("DEADBEEF"), (name, hit)
        for sig in re.findall(r"sig=([^&\s\"]+)", text):
            assert sig == fx.SAS_SIGNATURE, (name, sig)
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "test_smartbid_client.py"),
        os.path.join(here, "fixtures_smartbid", "__init__.py"),
        os.path.join(here, "test_rfp_harvest.py"),
        os.path.join(here, "..", "app", "services", "smartbid_client.py"),
        os.path.join(here, "..", "app", "services", "rfp_harvest.py"),
    ):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for needle in _REAL_NEEDLES:
            assert needle not in text, (path, needle)


def test_unlink_quietly_ignores_a_missing_file(tmp_path):
    sbc._unlink_quietly(tmp_path / "missing")
    path = tmp_path / "there"
    path.write_bytes(b"x")
    sbc._unlink_quietly(path)
    assert not path.exists()
