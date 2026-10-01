"""The Procore bid sheet client (app/services/procore_client) driven through
httpx.MockTransport, with an in-memory session store and the pace's sleep,
clock and rng patched so nothing waits (docs/RFP_HARVEST.md section 3 and
section 9).

The fake Procore reproduces the login chain captured 2026-09-14:

  GET app/auth/procore -> 302 login/oauth/authorize -> 302 login/?cookies_enabled
  -> 200 email form (sets login_session_id); POST submit_login_email -> 302
  /login/password -> 200 password form; POST submit_login_password -> 302
  oauth/authorize -> 302 app/auth/procore/callback (sets _session_id) -> 302
  route_to_bid_sheet -> 308 to the bid page, which the client never fetches.

The JSON endpoints answer 401 (or, when armed, a redirect into the login
host) without a valid _session_id; the route answers 308 with a session and
302 to /auth/procore without one; storage answers 302 to S3, S3 the bytes.

Pinned: reference parsing over a body shaped like the real invitation
(safelinks unwrapped, the intent link never enough on its own, ids bounded);
the exact request sequence of a login (the password in one POST body only,
remember_me sent, the jar saved with the account); every login refusal
(email re-rendered or redirected elsewhere, password rejected, a Cloudflare
interstitial, an off-site redirect, too many hops); one login and one retry
after a request proves the session gone; the login-interval discipline; the
failure counter, the lock, the one bell and availability(); a newer jar from
another worker tried before a login; the pace; the allowlist; the download
host rules, its single S3 hop, cookie-free requests and the byte cap.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote_plus, urlencode

import httpx
import pytest

from app.services import procore_client as pc
from tests import fixtures_procore as fx

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
EMAIL = "harvest-bot@example.com"
PASSWORD = "correct horse battery staple"
PDF = b"%PDF-1.7\n" + b"x" * 3000 + b"\n%%EOF\n"

APP = "https://app.procore.com"
LOGIN = "https://login.procore.com"
ROUTE_PATH = f"/{fx.COMPANY_ID}/company/planroom/route_to_bid_sheet/{fx.BID_ID}"
BID_PAGE_PATH = f"/{fx.COMPANY_ID}/company/planroom/bid_packages/{fx.PACKAGE_ID}/bids/{fx.BID_ID}"
PACKAGE_PATH = f"/rest/v1.0/companies/{fx.COMPANY_ID}/bid_packages/{fx.PACKAGE_ID}"
BID_PATH = f"/rest/v1.0/companies/{fx.COMPANY_ID}/bids/{fx.BID_ID}"
FORM_PATH = f"/rest/v1.0/companies/{fx.COMPANY_ID}/bid/{fx.BID_ID}/bid_forms/{fx.BID_FORM_ID}"
DOCS_PATH = f"/rest/v1.0/companies/{fx.COMPANY_ID}/planroom/bid_packages/{fx.PACKAGE_ID}/documents"
AUTHORIZE = (
    f"{LOGIN}/oauth/authorize?client_id=x&redirect_uri=https%3A%2F%2Fapp.procore.com%2Fauth"
    "%2Fprocore%2Fcallback&response_type=code&state=s"
)
STORAGE_URL = fx.DRAWING_ELEC["s3_source"]
S3_URL = "https://s3.amazonaws.com/procore-bucket/1787766088_c54655759_p28.pdf?X-Amz-Signature=test"


# ── The fake Procore ─────────────────────────────────────────────────────


def _html(status, body, **headers):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


def _redirect(location, **headers):
    return httpx.Response(302, headers={"location": location, **headers})


class FakeProcore:
    """MockTransport handler: the captured login chain, the five JSON
    endpoints, the route and the storage hop, with the failure modes the
    tests arm. Records every request so the sequence can be asserted."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.sessions: set[str] = set()          # valid app _session_id values
        self.authenticated: set[str] = set()     # login_session_id values past the password
        self.login_seq = 0
        self.email_mode = "ok"        # ok | rerender | elsewhere
        self.password_mode = "ok"     # ok | rejected | start | offsite | loop
        self.login_page_mode = "ok"   # ok | challenge | offsite
        self.expired_mode = "401"     # 401 | redirect | auth_redirect
        self.route_mode = "ok"        # ok | 404 | elsewhere | challenge
        self.callback_mode = "route"  # route | chooser | elsewhere: where the callback hands on
        self.json_mode = "ok"         # ok | challenge | html | 500
        self.storage_mode = "ok"      # ok | 403 | 404 | 500 | offsite | double | transport
        self.s3_mode = "ok"           # ok | 500 | transport
        self.pdf = PDF

    # -- helpers ---------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "app.procore.com":
            return self._app(request, path)
        if host == "login.procore.com":
            return self._login(request, path)
        if host == "storage.procore.com":
            return self._storage(request)
        if host.endswith(".amazonaws.com"):
            return self._s3(request)
        return httpx.Response(500, text=f"unexpected host {host}")

    @property
    def sequence(self) -> list[tuple[str, str]]:
        return [(r.method, str(r.url)) for r in self.requests]

    @property
    def paths(self) -> list[tuple[str, str, str]]:
        return [(r.method, r.url.host, r.url.path) for r in self.requests]

    def _cookies(self, request: httpx.Request) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in (request.headers.get("cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _has_session(self, request: httpx.Request) -> bool:
        return self._cookies(request).get("_session_id") in self.sessions

    def _form(self, request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    # -- app.procore.com -------------------------------------------------

    def _app(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method != "GET":
            return httpx.Response(405)
        if path == "/auth/procore":
            return _redirect(AUTHORIZE)
        if path == "/auth/procore/callback":
            self.login_seq += 1
            sid = f"app-session-{self.login_seq}"
            self.sessions.add(sid)
            target = {
                "route": f"{APP}{ROUTE_PATH}",
                "chooser": f"{APP}/account/select_company",
                "elsewhere": f"{APP}/{fx.COMPANY_ID}/company/planroom/not_found",
            }[self.callback_mode]
            return _redirect(
                target,
                **{"set-cookie": f"_session_id={sid}; path=/; secure; HttpOnly"},
            )
        if path == ROUTE_PATH:
            if self.route_mode == "404":
                return _html(404, "<html>Not found</html>")
            if not self._has_session(request):
                return _redirect(f"{APP}/auth/procore")
            if self.route_mode == "elsewhere":
                return _redirect(f"{APP}/{fx.COMPANY_ID}/company/planroom/not_found")
            if self.route_mode == "challenge":
                return _html(200, fx.CHALLENGE_PAGE)
            return httpx.Response(308, headers={"location": BID_PAGE_PATH})
        if path == BID_PAGE_PATH:
            return _html(200, "<html>the bid page: never fetched</html>")
        if path.startswith("/rest/"):
            return self._json(request, path)
        return httpx.Response(404, text="nope")

    def _json(self, request: httpx.Request, path: str) -> httpx.Response:
        if not self._has_session(request):
            if self.expired_mode == "redirect":
                return _redirect(AUTHORIZE)
            if self.expired_mode == "auth_redirect":
                return _redirect("/auth/procore")
            return httpx.Response(401, json={"error": "unauthorized"})
        if self.json_mode == "challenge":
            return _html(200, fx.CHALLENGE_PAGE, **{"cf-mitigated": "challenge"})
        if self.json_mode == "html":
            return _html(200, "<html>Something else</html>")
        if self.json_mode == "500":
            return httpx.Response(500, text="boom")
        body = {
            PACKAGE_PATH: fx.bid_package(),
            BID_PATH: fx.bid(),
            FORM_PATH: fx.bid_form(),
            DOCS_PATH: fx.documents(),
        }.get(path)
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)

    # -- login.procore.com -----------------------------------------------

    def _login(self, request: httpx.Request, path: str) -> httpx.Response:
        cookies = self._cookies(request)
        if request.method == "GET" and path == "/oauth/authorize":
            if self.password_mode == "loop" and "loop" in request.url.query.decode():
                return _redirect(f"{LOGIN}/oauth/authorize?loop=again")
            if cookies.get("login_session_id") in self.authenticated:
                return _redirect(f"{APP}/auth/procore/callback?code=c&state=s")
            return _redirect(f"{LOGIN}/?cookies_enabled=true")
        if request.method == "GET" and path == "/":
            if self.login_page_mode == "challenge":
                return _html(200, fx.CHALLENGE_PAGE)
            if self.login_page_mode == "offsite":
                return _redirect("https://evil.example.com/login")
            self.login_seq += 1
            return _html(
                200, fx.EMAIL_PAGE,
                **{"set-cookie": f"login_session_id=login-{self.login_seq}; path=/; secure"},
            )
        if request.method == "POST" and path == "/sessions/submit_login_email":
            form = self._form(request)
            if form.get("authenticity_token") != fx.EMAIL_TOKEN:
                return _html(422, "<html>bad token</html>")
            if self.email_mode == "rerender":
                return _html(200, fx.EMAIL_REJECTED_PAGE)
            if self.email_mode == "elsewhere":
                return _redirect("/")
            return _redirect("/login/password")
        if request.method == "GET" and path == "/login/password":
            return _html(200, fx.PASSWORD_PAGE)
        if request.method == "POST" and path == "/sessions/submit_login_password":
            form = self._form(request)
            if form.get("authenticity_token") != fx.PASSWORD_TOKEN:
                return _html(422, "<html>bad token</html>")
            if self.password_mode == "rejected" or form.get("session[password]") != PASSWORD:
                return _redirect("/login/password")
            if self.password_mode == "start":
                return _redirect("/")
            if self.password_mode == "offsite":
                return _redirect("https://evil.example.com/callback")
            if self.password_mode == "loop":
                return _redirect(f"{LOGIN}/oauth/authorize?loop=1")
            self.authenticated.add(cookies.get("login_session_id", ""))
            return _redirect(AUTHORIZE)
        return httpx.Response(404, text="nope")

    # -- storage ---------------------------------------------------------

    def _storage(self, request: httpx.Request) -> httpx.Response:
        if self.storage_mode == "transport":
            raise httpx.ConnectError("storage unreachable", request=request)
        if self.storage_mode in ("403", "404", "500"):
            return httpx.Response(int(self.storage_mode), text="refused")
        if self.storage_mode == "offsite":
            return _redirect("https://evil.example.com/file.pdf")
        return _redirect(S3_URL)

    def _s3(self, request: httpx.Request) -> httpx.Response:
        if self.storage_mode == "double":
            return _redirect("https://s3-us-west-2.amazonaws.com/other/file.pdf")
        if self.s3_mode == "transport":
            raise httpx.ReadTimeout("slow", request=request)
        if self.s3_mode == "500":
            return httpx.Response(503, text="slow down")
        return httpx.Response(200, content=self.pdf, headers={"content-type": "application/pdf"})


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
    monkeypatch.setattr(pc, "_last_request_at", 0.0)


@pytest.fixture
def fake():
    return FakeProcore()


@pytest.fixture
def clock():
    return Clock()


def _store(**state):
    store = pc.MemorySessionStore(max_failures=3, lock_seconds=21600, now=lambda: NOW)
    store.state = dict(state)
    return store


def _config(**over):
    base = dict(email=EMAIL, password=PASSWORD, min_request_interval=0.0, login_min_interval=600)
    base.update(over)
    return pc.ProcoreConfig(**base)


def _session(fake, clock, store=None, config=None, **kw):
    return pc.ProcoreSession(
        config or _config(),
        store if store is not None else _store(),
        transport=httpx.MockTransport(fake),
        sleep=clock.sleep,
        clock=clock,
        rng=lambda a, b: 1.0,
        now=lambda: NOW,
        **kw,
    )


def _ref():
    return pc.ProcoreRef(company_id=fx.COMPANY_ID, bid_id=fx.BID_ID)


# ── Reference parsing (pure) ─────────────────────────────────────────────


def test_unwrap_link_unwraps_safelinks_and_leaves_other_links_alone():
    assert pc.unwrap_link(fx._safelink(fx.ROUTE_URL)) == fx.ROUTE_URL
    assert pc.unwrap_link(fx.ROUTE_URL) == fx.ROUTE_URL
    assert pc.unwrap_link("https://example.com/?url=x") == "https://example.com/?url=x"
    # A safelink without an inner url is given back as it is.
    bare = "https://nam09.safelinks.protection.outlook.com/?data=x"
    assert pc.unwrap_link(bare) == bare


def test_parse_reference_over_the_real_email_body():
    ref = pc.parse_reference(fx.EMAIL_BODY)
    assert ref == pc.ProcoreRef(
        company_id=fx.COMPANY_ID, bid_id=fx.BID_ID, package_id=None, project_id=fx.PROJECT_ID
    )
    assert ref.external_key == f"procore:{fx.COMPANY_ID}:{fx.BID_ID}"
    # Rebuilt from the parts, never the email's own URL (no ?from_email, no safelink).
    assert ref.bid_sheet_url == f"{APP}{ROUTE_PATH}"


def test_parse_reference_falls_back_to_the_zip_link_and_reads_the_bid_page():
    zip_only = f"Download <{fx._safelink(fx.ZIP_URL)}>"
    assert pc.parse_reference(zip_only) == pc.ProcoreRef(company_id=fx.COMPANY_ID, bid_id=fx.BID_ID)
    page = f"See {APP}{BID_PAGE_PATH}"
    assert pc.parse_reference(page) == pc.ProcoreRef(
        company_id=fx.COMPANY_ID, bid_id=fx.BID_ID, package_id=fx.PACKAGE_ID
    )
    # HTML-escaped ampersands in a text body still parse.
    escaped = fx.ZIP_URL.replace("&", "&amp;")
    assert pc.parse_reference(escaped).bid_id == fx.BID_ID


def test_parse_reference_intent_link_alone_is_not_a_reference():
    body = f"Will Bid <{fx._safelink(fx.INTENT_WILL_BID)}>\nWill Not Bid <{fx._safelink(fx.INTENT_WILL_NOT_BID)}>"
    assert pc.parse_reference(body) is None
    assert pc.parse_reference(None) is None
    assert pc.parse_reference("") is None
    assert pc.parse_reference("no links here") is None
    # Another host with the same path shape is ignored.
    assert pc.parse_reference(f"https://evil.example.com{ROUTE_PATH}") is None
    assert pc.parse_reference(f"https://app.procore.com.evil.example{ROUTE_PATH}") is None


def test_parse_reference_rejects_ids_over_twelve_digits():
    long_id = "1" * 13
    assert pc.parse_reference(f"{APP}/{fx.COMPANY_ID}/company/planroom/route_to_bid_sheet/{long_id}") is None
    assert pc.parse_reference(f"{APP}/{long_id}/company/planroom/route_to_bid_sheet/{fx.BID_ID}") is None
    assert pc.parse_reference(f"{APP}/{fx.COMPANY_ID}/company/planroom/download_zip?bid_id={long_id}") is None
    twelve = "9" * 12
    assert pc.parse_reference(f"{APP}/{twelve}/company/planroom/route_to_bid_sheet/{twelve}") == pc.ProcoreRef(
        company_id=twelve, bid_id=twelve
    )
    # Non-digits never parse.
    assert pc.parse_reference(f"{APP}/5662/company/planroom/route_to_bid_sheet/abc") is None


# ── Login flow ───────────────────────────────────────────────────────────


def test_login_flow_follows_the_captured_chain_exactly(fake, clock):
    store = _store()
    session = _session(fake, clock, store)
    with session:
        data = session.get_json(PACKAGE_PATH, referer=f"{APP}{ROUTE_PATH}")
    assert data["id"] == 1376188
    assert fake.paths == [
        ("GET", "app.procore.com", PACKAGE_PATH),                    # 401: the session is gone
        ("GET", "app.procore.com", "/auth/procore"),
        ("GET", "login.procore.com", "/oauth/authorize"),
        ("GET", "login.procore.com", "/"),
        ("POST", "login.procore.com", "/sessions/submit_login_email"),
        ("GET", "login.procore.com", "/login/password"),
        ("POST", "login.procore.com", "/sessions/submit_login_password"),
        ("GET", "login.procore.com", "/oauth/authorize"),
        ("GET", "app.procore.com", "/auth/procore/callback"),        # sets the app session; chain ends
        ("GET", "app.procore.com", PACKAGE_PATH),                    # the one retry
    ]
    # Nothing past the callback is requested: neither the route it hands on
    # to nor the bid page behind that route's 308.
    assert not any(p in (ROUTE_PATH, BID_PAGE_PATH) for _, _, p in fake.paths)
    # The password appears in exactly one request body and nowhere else.
    carrying = [
        r for r in fake.requests
        if PASSWORD in unquote_plus(r.content.decode()) or PASSWORD in unquote_plus(str(r.url))
    ]
    assert [(r.method, r.url.path) for r in carrying] == [("POST", "/sessions/submit_login_password")]
    email_form = fake._form(fake.requests[4])
    assert email_form == {
        "authenticity_token": fx.EMAIL_TOKEN,
        "session[email]": EMAIL,
        "session[remember_me]": "true",
    }
    password_form = fake._form(fake.requests[6])
    assert password_form == {
        "authenticity_token": fx.PASSWORD_TOKEN,
        "session[login_step]": "password",      # every hidden field carried forward
        "session[password]": PASSWORD,
    }
    # Browser-shaped requests: HTML accept on the login chain, JSON accept on the API.
    assert fake.requests[1].headers["accept"].startswith("text/html")
    assert fake.requests[-1].headers["accept"].startswith("application/json")
    assert fake.requests[-1].headers["referer"] == f"{APP}{ROUTE_PATH}"
    assert "Mozilla/5.0" in fake.requests[-1].headers["user-agent"]
    # The jar is saved under the account with both hosts' cookies; failures cleared.
    assert store.state["account"] == EMAIL
    names = {(c["name"], c["domain"]) for c in store.state["cookies"]}
    assert ("_session_id", "app.procore.com") in names
    assert ("login_session_id", "login.procore.com") in names
    assert store.state["login_failures"] == 0 and store.state["locked_until"] is None
    assert store.state["logged_in_at"] == NOW.isoformat()
    assert store.state["last_login_attempt_at"] == NOW.isoformat()
    assert session.logged_in_this_session is True
    # The password never reached the store.
    assert PASSWORD not in repr(store.state)


def test_login_does_not_happen_while_the_stored_jar_still_works(fake, clock):
    fake.sessions.add("stored")
    store = _store(
        account=EMAIL,
        cookies=[{"name": "_session_id", "value": "stored", "domain": "app.procore.com", "path": "/"}],
        logged_in_at=(NOW - timedelta(days=1)).isoformat(),
    )
    with _session(fake, clock, store) as session:
        assert session.get_json(BID_PATH, params={"view": "planroom_redesign"})["id"] == 64706611
        assert session.get_json(FORM_PATH)["id"] == 11278843
    assert [p for _, _, p in fake.paths] == [BID_PATH, FORM_PATH]
    assert fake.requests[0].url.params["view"] == "planroom_redesign"
    assert store.state.get("last_used_at") == NOW.isoformat()


def test_a_jar_saved_for_another_account_is_not_loaded(fake, clock):
    fake.sessions.add("theirs")
    store = _store(
        account="someone-else@example.com",
        cookies=[{"name": "_session_id", "value": "theirs", "domain": "app.procore.com", "path": "/"}],
        logged_in_at=NOW.isoformat(),
    )
    with _session(fake, clock, store) as session:
        session.get_json(PACKAGE_PATH)
    # The first request went out without their cookie, so a login followed.
    assert "cookie" not in fake.requests[0].headers
    assert ("POST", "login.procore.com", "/sessions/submit_login_password") in fake.paths
    assert store.state["account"] == EMAIL


@pytest.mark.parametrize("mode", ["rerender", "elsewhere"])
def test_email_rejected_fails_the_login_at_the_email_step(fake, clock, mode):
    fake.email_mode = mode
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    cause = exc.value.__cause__
    assert isinstance(cause, pc.ProcoreLoginFailed) and cause.step == "email"
    if mode == "rerender":
        assert str(cause) == "We could not find an account for that email."
    else:
        assert str(cause) == "Procore did not ask for a password after the email."
    assert exc.value.locked_until == NOW + timedelta(seconds=600)
    assert store.state["login_failures"] == 1 and store.state["last_error"].startswith("email: ")
    assert PASSWORD not in store.state["last_error"]
    # Nothing past the email POST was requested.
    assert fake.paths[-1] == ("POST", "login.procore.com", "/sessions/submit_login_email")


def test_password_rejected_fails_the_login_at_the_password_step(fake, clock):
    fake.password_mode = "rejected"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    cause = exc.value.__cause__
    assert cause.step == "password" and str(cause) == "Procore rejected the password."
    assert fake.paths[-1] == ("POST", "login.procore.com", "/sessions/submit_login_password")
    assert store.state["login_failures"] == 1
    assert PASSWORD not in str(exc.value) and PASSWORD not in str(cause)


def test_password_sent_back_to_the_start_is_a_password_failure(fake, clock):
    fake.password_mode = "start"
    with _session(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert exc.value.__cause__.step == "password"
    assert "back to the start" in str(exc.value.__cause__)


def test_cloudflare_interstitial_at_login_is_a_challenge_failure(fake, clock):
    fake.login_page_mode = "challenge"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert exc.value.__cause__.step == "challenge"
    assert store.state["login_failures"] == 1
    # No form was posted to the challenge page.
    assert not any(m == "POST" for m, _, _ in fake.paths)


def test_off_site_redirect_is_refused_on_the_login_chain(fake, clock):
    fake.login_page_mode = "offsite"
    with _session(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert exc.value.__cause__.step == "redirect"
    assert "off Procore" in str(exc.value.__cause__)
    assert not any(r.url.host == "evil.example.com" for r in fake.requests)


def test_off_site_redirect_after_the_password_is_refused(fake, clock):
    fake.password_mode = "offsite"
    with _session(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert exc.value.__cause__.step == "redirect"
    assert not any(r.url.host == "evil.example.com" for r in fake.requests)


def test_a_procore_path_outside_the_login_flow_ends_the_chain(fake, clock):
    fake.callback_mode = "elsewhere"   # the callback hands on to a planroom page, not the route
    with _session(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert exc.value.__cause__.step == "redirect"
    assert "left the expected flow" in str(exc.value.__cause__)
    assert not any(p.endswith("/not_found") for _, _, p in fake.paths)


def test_login_started_from_the_bid_sheet_route_plants_the_continue_url(fake, clock):
    """resolve_bid_sheet knows the route, so the login chain starts there
    (live 2026-09-15: starting at /auth/procore lands the callback on the
    account's company chooser instead of the bid sheet)."""
    store = _store()
    session = _session(fake, clock, store)
    with session:
        assert session.resolve_bid_sheet(_ref()) == fx.PACKAGE_ID
    assert fake.paths[:3] == [
        ("GET", "app.procore.com", ROUTE_PATH),          # 302 /auth/procore: the session is gone
        ("GET", "app.procore.com", ROUTE_PATH),          # the login chain starts at the route
        ("GET", "app.procore.com", "/auth/procore"),
    ]
    assert fake.paths[-2:] == [
        ("GET", "app.procore.com", "/auth/procore/callback"),   # chain ends
        ("GET", "app.procore.com", ROUTE_PATH),                 # the one retry: 308
    ]
    assert not any(p == BID_PAGE_PATH for _, _, p in fake.paths)


def test_callback_landing_on_the_company_chooser_still_counts_as_logged_in(fake, clock):
    fake.callback_mode = "chooser"
    store = _store()
    with _session(fake, clock, store) as session:
        assert session.get_json(PACKAGE_PATH)["id"] == 1376188
    assert store.state["login_failures"] == 0
    assert not any(p == "/account/select_company" for _, _, p in fake.paths)


def test_too_many_hops_ends_the_login(fake, clock):
    fake.password_mode = "loop"
    with _session(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert exc.value.__cause__.step == "redirect"
    assert "too many times" in str(exc.value.__cause__)
    password_post = next(i for i, p in enumerate(fake.paths) if p[2] == "/sessions/submit_login_password")
    followed = fake.paths[password_post + 1:]
    assert len(followed) == pc._MAX_HOPS == 8
    assert all(p == ("GET", "login.procore.com", "/oauth/authorize") for p in followed)


def test_login_page_without_a_form_fails_at_login_page(fake, clock, monkeypatch):
    monkeypatch.setattr(fx, "EMAIL_PAGE", "<html><body>Maintenance</body></html>")
    with _session(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert exc.value.__cause__.step == "login_page"
    assert "no email form" in str(exc.value.__cause__)


def test_a_5xx_during_the_login_is_transient_and_records_no_failure(fake, clock, monkeypatch):
    store = _store()
    original = fake._login

    def flaky(request, path):
        if path == "/login/password":
            return httpx.Response(502, text="bad gateway")
        return original(request, path)

    monkeypatch.setattr(fake, "_login", flaky)
    with _session(fake, clock, store) as session, pytest.raises(pc.ProcoreTransient):
        session.get_json(PACKAGE_PATH)
    assert store.state.get("login_failures", 0) == 0


# ── Session expiry, retry and the login discipline ───────────────────────


@pytest.mark.parametrize("mode", ["401", "redirect", "auth_redirect"])
def test_session_gone_on_a_json_get_means_one_login_and_one_retry(fake, clock, mode):
    fake.expired_mode = mode
    with _session(fake, clock) as session:
        assert session.get_json(DOCS_PATH)["id"] == 1376188
        json_gets = [p for p in fake.paths if p[2] == DOCS_PATH]
        assert len(json_gets) == 2
        assert sum(1 for p in fake.paths if p[2] == "/sessions/submit_login_password") == 1
        # A later call on the same session reuses the jar: no second login.
        before = len(fake.paths)
        assert session.get_json(FORM_PATH)["id"] == 11278843
    assert fake.paths[before:] == [("GET", "app.procore.com", FORM_PATH)]


def test_session_gone_on_the_bid_sheet_route_means_one_login_and_one_retry(fake, clock):
    with _session(fake, clock) as session:
        assert session.resolve_bid_sheet(_ref()) == fx.PACKAGE_ID
    routes = [p for p in fake.paths if p[2] == ROUTE_PATH]
    # The first 302 to /auth/procore, the 308 that ends the login chain, and the retry's 308.
    assert len(routes) == 3
    assert sum(1 for p in fake.paths if p[2] == "/sessions/submit_login_password") == 1
    assert not any(p == BID_PAGE_PATH for _, _, p in fake.paths)


def test_second_login_inside_the_interval_is_refused_without_a_request(fake, clock):
    fake.password_mode = "rejected"
    store = _store()
    with _session(fake, clock, store) as session:
        with pytest.raises(pc.ProcoreUnavailable):
            session.get_json(PACKAGE_PATH)
        requests_after_first = len(fake.paths)
        with pytest.raises(pc.ProcoreUnavailable) as exc:
            session.get_json(PACKAGE_PATH)
    assert "attempted recently" in str(exc.value)
    assert exc.value.locked_until == NOW + timedelta(seconds=600)
    # Only the 401 GET went out the second time: no login chain at all.
    assert fake.paths[requests_after_first:] == [("GET", "app.procore.com", PACKAGE_PATH)]
    assert store.state["login_failures"] == 1


def test_a_recent_attempt_recorded_by_another_worker_also_defers_the_login(fake, clock):
    store = _store(last_login_attempt_at=(NOW - timedelta(seconds=30)).isoformat())
    with _session(fake, clock, store) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert exc.value.locked_until == NOW - timedelta(seconds=30) + timedelta(seconds=600)
    assert fake.paths == [("GET", "app.procore.com", PACKAGE_PATH)]


def test_failure_counter_locks_logins_rings_once_and_reports_through_availability(fake, clock):
    fake.password_mode = "rejected"
    store = _store()
    bells = []
    session = _session(
        fake, clock, store, config=_config(login_min_interval=0),
        on_lock=lambda until, error: bells.append((until, error)),
    )
    with session:
        for _ in range(2):
            with pytest.raises(pc.ProcoreUnavailable) as exc:
                session.get_json(PACKAGE_PATH)
            assert not isinstance(exc.value, pc.ProcoreLoginLocked)
        assert session.availability() == (True, None, None)
        with pytest.raises(pc.ProcoreLoginLocked) as locked:
            session.get_json(PACKAGE_PATH)
        until = NOW + timedelta(seconds=21600)
        assert locked.value.locked_until == until
        assert store.state["login_failures"] == 3 and store.state["locked_until"] == until.isoformat()
        assert bells == [(until, "Procore rejected the password.")]
        # While locked: no login chain, the bell does not ring again, availability says so.
        before = len(fake.paths)
        with pytest.raises(pc.ProcoreLoginLocked):
            session.get_json(PACKAGE_PATH)
        assert fake.paths[before:] == [("GET", "app.procore.com", PACKAGE_PATH)]
        assert bells == [(until, "Procore rejected the password.")]
        assert session.availability() == (False, "Procore logins are locked after repeated failures.", until)
    assert PASSWORD not in repr(store.state)


def test_a_bell_that_raises_never_masks_the_lock(fake, clock):
    fake.password_mode = "rejected"
    store = _store(login_failures=2)

    def boom(until, error):
        raise RuntimeError("notifications down")

    with _session(fake, clock, store, config=_config(login_min_interval=0), on_lock=boom) as session:
        with pytest.raises(pc.ProcoreLoginLocked):
            session.get_json(PACKAGE_PATH)
    assert store.state["login_failures"] == 3


def test_lock_state_reads_the_store_and_expires(fake, clock):
    until = NOW + timedelta(hours=1)
    assert pc.lock_state({"locked_until": until.isoformat()}, NOW) == until
    assert pc.lock_state({"locked_until": (NOW - timedelta(seconds=1)).isoformat()}, NOW) is None
    assert pc.lock_state({"locked_until": "garbage"}, NOW) is None
    assert pc.lock_state(None, NOW) is None
    # An expired lock in the store no longer stops a login.
    fake.password_mode = "ok"
    store = _store(locked_until=(NOW - timedelta(seconds=1)).isoformat(), login_failures=3)
    with _session(fake, clock, store) as session:
        assert session.availability() == (True, None, None)
        session.get_json(PACKAGE_PATH)
    assert store.state["login_failures"] == 0


def test_unconfigured_credentials_never_attempt_a_login(fake, clock):
    session = _session(fake, clock, config=_config(password=""))
    with session:
        assert session.availability() == (False, "Procore credentials are not configured.", None)
        with pytest.raises(pc.ProcoreUnavailable) as exc:
            session.get_json(PACKAGE_PATH)
    assert "not configured" in str(exc.value)
    assert fake.paths == [("GET", "app.procore.com", PACKAGE_PATH)]


def test_a_newer_jar_from_another_worker_is_tried_before_a_login(fake, clock):
    fake.sessions.add("fresh")
    store = _store(
        account=EMAIL,
        cookies=[{"name": "_session_id", "value": "stale", "domain": "app.procore.com", "path": "/"}],
        logged_in_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    session = _session(fake, clock, store)
    session._load_store()   # this worker loaded the stale jar earlier
    # Meanwhile the other worker logged in and saved a fresh jar.
    store.state["cookies"] = [
        {"name": "_session_id", "value": "fresh", "domain": "app.procore.com", "path": "/"}
    ]
    store.state["logged_in_at"] = (NOW - timedelta(minutes=1)).isoformat()
    with session:
        assert session.get_json(PACKAGE_PATH)["id"] == 1376188
    assert fake.paths == [
        ("GET", "app.procore.com", PACKAGE_PATH),   # 401 with the stale cookie
        ("GET", "app.procore.com", PACKAGE_PATH),   # 200 with the fresh one
    ]
    assert fake._cookies(fake.requests[0])["_session_id"] == "stale"
    assert fake._cookies(fake.requests[1])["_session_id"] == "fresh"
    assert session.logged_in_this_session is False


def test_a_jar_no_newer_than_ours_leads_to_a_login(fake, clock):
    store = _store(
        account=EMAIL,
        cookies=[{"name": "_session_id", "value": "stale", "domain": "app.procore.com", "path": "/"}],
        logged_in_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    with _session(fake, clock, store) as session:
        session.get_json(PACKAGE_PATH)
    assert ("POST", "login.procore.com", "/sessions/submit_login_password") in fake.paths
    assert session.logged_in_this_session is True


def test_expired_cookies_in_the_store_are_skipped(fake, clock):
    fake.sessions.add("old")
    store = _store(
        account=EMAIL,
        cookies=[{
            "name": "_session_id", "value": "old", "domain": "app.procore.com", "path": "/",
            "expires": (NOW - timedelta(days=1)).timestamp(),
        }],
        logged_in_at=(NOW - timedelta(days=2)).isoformat(),
    )
    with _session(fake, clock, store) as session:
        session.get_json(PACKAGE_PATH)
    assert "cookie" not in fake.requests[0].headers


# ── JSON GET answers ─────────────────────────────────────────────────────


def _logged_in(fake, clock, **kw):
    fake.sessions.add("ok")
    store = _store(
        account=EMAIL,
        cookies=[{"name": "_session_id", "value": "ok", "domain": "app.procore.com", "path": "/"}],
        logged_in_at=NOW.isoformat(),
    )
    return _session(fake, clock, store, **kw)


def test_json_get_maps_a_verification_page_to_unavailable(fake, clock):
    fake.json_mode = "challenge"
    with _logged_in(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert "verification page" in str(exc.value)


def test_json_get_maps_a_plain_html_page_to_unavailable(fake, clock):
    fake.json_mode = "html"
    with _logged_in(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable) as exc:
        session.get_json(PACKAGE_PATH)
    assert "page instead of data" in str(exc.value)


def test_json_get_maps_403_404_to_forbidden_and_5xx_429_to_transient(fake, clock):
    with _logged_in(fake, clock) as session:
        with pytest.raises(pc.ProcoreForbidden):
            session.get_json(f"/rest/v1.0/companies/{fx.COMPANY_ID}/bids/999")   # the fake 404s it
        fake.json_mode = "500"
        with pytest.raises(pc.ProcoreTransient):
            session.get_json(PACKAGE_PATH)
    # No login was attempted for any of them.
    assert not any(h == "login.procore.com" for _, h, _ in fake.paths)


def test_json_get_maps_a_redirect_elsewhere_to_forbidden(fake, clock, monkeypatch):
    monkeypatch.setattr(fake, "_json", lambda request, path: _redirect(f"{APP}/somewhere/else"))
    with _logged_in(fake, clock) as session, pytest.raises(pc.ProcoreForbidden):
        session.get_json(PACKAGE_PATH)


def test_transport_error_on_a_json_get_is_transient(fake, clock, monkeypatch):
    def dead(request):
        raise httpx.ConnectError("no route", request=request)

    session = pc.ProcoreSession(
        _config(), _store(), transport=httpx.MockTransport(dead),
        sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0, now=lambda: NOW,
    )
    with session, pytest.raises(pc.ProcoreTransient) as exc:
        session.get_json(PACKAGE_PATH)
    assert "ConnectError" in str(exc.value)


def test_bid_sheet_route_404_and_challenge_map_as_documented(fake, clock):
    fake.route_mode = "404"
    with _logged_in(fake, clock) as session, pytest.raises(pc.ProcoreForbidden):
        session.resolve_bid_sheet(_ref())
    fake.route_mode = "challenge"
    with _logged_in(fake, clock) as session, pytest.raises(pc.ProcoreUnavailable):
        session.resolve_bid_sheet(_ref())
    fake.route_mode = "elsewhere"
    with _logged_in(fake, clock) as session, pytest.raises(pc.ProcoreForbidden):
        session.resolve_bid_sheet(_ref())


def test_bid_sheet_route_to_another_bids_page_is_refused(fake, clock, monkeypatch):
    other = f"/{fx.COMPANY_ID}/company/planroom/bid_packages/{fx.PACKAGE_ID}/bids/1"
    monkeypatch.setattr(
        fake, "_app", lambda request, path: httpx.Response(308, headers={"location": other})
    )
    with _logged_in(fake, clock) as session, pytest.raises(pc.ProcoreForbidden):
        session.resolve_bid_sheet(_ref())


# ── Pace ─────────────────────────────────────────────────────────────────


def test_pace_waits_at_least_the_interval_between_requests():
    clock = Clock()
    pc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == []            # the first request goes at once
    pc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0]
    clock.t += 0.5                       # half a second later
    pc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0, 1.5]
    clock.t += 10                        # long after: no wait
    pc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0, 1.5]


def test_pace_applies_the_random_multiplier_between_one_and_two():
    clock = Clock()
    draws = []

    def rng(lo, hi):
        draws.append((lo, hi))
        return 1.75

    pc._pace(2.0, sleep=clock.sleep, clock=clock, rng=rng)
    pc._pace(2.0, sleep=clock.sleep, clock=clock, rng=rng)
    assert draws == [(1.0, 2.0), (1.0, 2.0)]
    assert clock.sleeps == [3.5]


def test_pace_never_sleeps_when_the_interval_is_zero_or_negative():
    clock = Clock()
    for _ in range(3):
        pc._pace(0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 2.0)
        pc._pace(-1.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 2.0)
    assert clock.sleeps == []


def test_every_request_of_a_login_and_a_download_is_paced(fake, clock, tmp_path):
    session = _session(fake, clock, config=_config(min_request_interval=2.0))
    session._download_client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(fake), follow_redirects=False
    )
    with session:
        session.get_json(PACKAGE_PATH)
        session.download(STORAGE_URL, tmp_path / "a.pdf", max_bytes=10_000)
    # One gap per request after the first; the download counts as one paced
    # request (its S3 hop rides the same gap).
    assert len(clock.sleeps) == len(fake.requests) - 2
    assert all(gap == 2.0 for gap in clock.sleeps)


# ── Allowlist ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [ROUTE_PATH, PACKAGE_PATH, BID_PATH, FORM_PATH, DOCS_PATH])
def test_allowlist_accepts_the_five_harvest_endpoints(path):
    pc.assert_allowed_get(path)


@pytest.mark.parametrize(
    "path",
    [
        f"/{fx.PROJECT_ID}/project/public/bid/{fx.BID_ID}/intents/public_set_bid_intent",
        f"/{fx.PROJECT_ID}/project/bidding/bid_packages/{fx.PACKAGE_ID}/add_to_bid_list",
        f"/webclients/host/companies/{fx.COMPANY_ID}/tools/planroom/bid-packages/{fx.PACKAGE_ID}/bids/{fx.BID_ID}/sign",
        f"/{fx.COMPANY_ID}/company/planroom/email_bid_docs",
        f"/{fx.COMPANY_ID}/company/bid/{fx.BID_ID}/uploads",
        f"/{fx.COMPANY_ID}/company/planroom/download_bid_docs_zip",
        f"/{fx.COMPANY_ID}/company/planroom/download_zip",
        f"/rest/v1.0/companies/{fx.COMPANY_ID}/bids/{fx.BID_ID}.pdf",
        f"/rest/v1.0/companies/{fx.COMPANY_ID}/bids/{fx.BID_ID}/cost_codes",
        BID_PAGE_PATH,
        f"/rest/v1.0/companies/{'1' * 13}/bid_packages/{fx.PACKAGE_ID}",
        "/rest/v1.0/companies/5662/bid_packages/1376188/",
        "",
    ],
)
def test_allowlist_refuses_everything_else(path):
    with pytest.raises(ValueError):
        pc.assert_allowed_get(path)


def test_get_json_checks_the_allowlist_before_any_request(fake, clock):
    with _logged_in(fake, clock) as session, pytest.raises(ValueError):
        session.get_json(f"/{fx.PROJECT_ID}/project/public/bid/{fx.BID_ID}/intents/public_set_bid_intent")
    assert fake.requests == []


def test_requests_never_leave_the_two_procore_hosts(fake, clock):
    with _logged_in(fake, clock) as session, pytest.raises(ValueError):
        session._request("GET", "https://evil.example.com/rest/v1.0/companies/5662/bid_packages/1")
    assert fake.requests == []


# ── Downloads ────────────────────────────────────────────────────────────


@pytest.fixture
def downloader(fake, clock, monkeypatch):
    """A logged-in session whose production download client (a fresh
    httpx.Client with no jar) is routed to the fake, so the cookie-free
    property is the real code's."""
    session = _logged_in(fake, clock)
    session._load_store()
    real_client = httpx.Client

    def routed(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(fake))
        return real_client(**kwargs)

    monkeypatch.setattr(pc.httpx, "Client", routed)
    return session


def test_download_follows_one_hop_to_s3_without_cookies(downloader, fake, tmp_path):
    dest = tmp_path / "0001.bin"
    with downloader as session:
        assert session._has_app_session()
        written = session.download(STORAGE_URL, dest, max_bytes=len(PDF))
    assert written == len(PDF) and dest.read_bytes() == PDF
    assert [(m, h) for m, h, _ in fake.paths] == [
        ("GET", "storage.procore.com"), ("GET", "s3.amazonaws.com"),
    ]
    assert all("cookie" not in r.headers for r in fake.requests)
    assert str(fake.requests[1].url) == S3_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://storage.procore.com/api/v5/files/x?sig=test",
        "https://storage.procore.com.evil.example/x?sig=test",
        "https://app.procore.com/rest/v1.0/companies/5662/bids/64706611.pdf",
        "https://s3.amazonaws.com/direct/file.pdf",
        "ftp://storage.procore.com/x",
    ],
)
def test_download_accepts_only_https_procore_storage(downloader, fake, tmp_path, url):
    dest = tmp_path / "x.bin"
    with downloader as session, pytest.raises(pc.ProcoreForbidden):
        session.download(url, dest, max_bytes=10_000)
    assert fake.requests == [] and not dest.exists()


def test_download_refuses_a_redirect_off_amazonaws(downloader, fake, tmp_path):
    fake.storage_mode = "offsite"
    dest = tmp_path / "x.bin"
    with downloader as session, pytest.raises(pc.ProcoreForbidden) as exc:
        session.download(STORAGE_URL, dest, max_bytes=10_000)
    assert "off Procore storage" in str(exc.value)
    assert [h for _, h, _ in fake.paths] == ["storage.procore.com"]
    assert not dest.exists()


def test_download_refuses_a_second_redirect(downloader, fake, tmp_path):
    fake.storage_mode = "double"
    dest = tmp_path / "x.bin"
    with downloader as session, pytest.raises(pc.ProcoreForbidden) as exc:
        session.download(STORAGE_URL, dest, max_bytes=10_000)
    assert "twice" in str(exc.value)
    assert [h for _, h, _ in fake.paths] == ["storage.procore.com", "s3.amazonaws.com"]
    assert not dest.exists()


def test_download_enforces_the_byte_cap_and_unlinks(downloader, fake, tmp_path):
    dest = tmp_path / "x.bin"
    with downloader as session, pytest.raises(pc.ProcoreForbidden) as exc:
        session.download(STORAGE_URL, dest, max_bytes=len(PDF) - 1)
    assert "larger than the harvest accepts" in str(exc.value)
    assert not dest.exists()


@pytest.mark.parametrize("mode", ["403", "404"])
def test_download_maps_storage_refusals_to_forbidden(downloader, fake, tmp_path, mode):
    fake.storage_mode = mode
    dest = tmp_path / "x.bin"
    with downloader as session, pytest.raises(pc.ProcoreForbidden):
        session.download(STORAGE_URL, dest, max_bytes=10_000)
    assert not dest.exists()


def test_download_maps_5xx_and_transport_trouble_to_transient(downloader, fake, tmp_path):
    dest = tmp_path / "x.bin"
    with downloader as session:
        fake.storage_mode = "500"
        with pytest.raises(pc.ProcoreTransient):
            session.download(STORAGE_URL, dest, max_bytes=10_000)
        assert not dest.exists()
        fake.storage_mode = "ok"
        fake.s3_mode = "500"
        with pytest.raises(pc.ProcoreTransient):
            session.download(STORAGE_URL, dest, max_bytes=10_000)
        assert not dest.exists()
        fake.s3_mode = "transport"
        with pytest.raises(pc.ProcoreTransient) as exc:
            session.download(STORAGE_URL, dest, max_bytes=10_000)
        assert "ReadTimeout" in str(exc.value) and not dest.exists()
        fake.storage_mode = "transport"
        with pytest.raises(pc.ProcoreTransient):
            session.download(STORAGE_URL, dest, max_bytes=10_000)
        assert not dest.exists()


def test_download_refuses_to_overwrite_an_existing_file(downloader, fake, tmp_path):
    dest = tmp_path / "x.bin"
    dest.write_bytes(b"keep")
    with downloader as session, pytest.raises(FileExistsError):
        session.download(STORAGE_URL, dest, max_bytes=10_000)
    assert fake.requests == []
    assert dest.read_bytes() == b"keep"


# ── Small parsers ────────────────────────────────────────────────────────


def test_parse_login_form_carries_every_hidden_field_and_only_those():
    action, hidden = pc.parse_login_form(fx.EMAIL_PAGE, "submit_login_email")
    assert action == "/sessions/submit_login_email"
    assert hidden == {"authenticity_token": fx.EMAIL_TOKEN, "session[sso_target_url]": ""}
    action, hidden = pc.parse_login_form(fx.PASSWORD_PAGE, "submit_login_password")
    assert action == "/sessions/submit_login_password"
    assert hidden == {"authenticity_token": fx.PASSWORD_TOKEN, "session[login_step]": "password"}
    assert pc.parse_login_form(fx.EMAIL_PAGE, "submit_login_password") is None
    assert pc.parse_login_form("<html>no form</html>", "submit_login_email") is None
    # Entities in an action or a value are resolved; a nameless input is skipped.
    page = (
        '<form action="/sessions/submit_login_email?a=1&amp;b=2" method="post">'
        '<input type="hidden" name="tok" value="a&amp;b"><input type="hidden" value="x">'
        '<input type="HIDDEN" name="upper" value="1"></form>'
    )
    assert pc.parse_login_form(page, "submit_login_email") == (
        "/sessions/submit_login_email?a=1&b=2", {"tok": "a&b", "upper": "1"}
    )


def test_page_error_text_reads_the_first_alert_and_caps_it():
    assert pc.page_error_text(fx.EMAIL_REJECTED_PAGE) == "We could not find an account for that email."
    assert pc.page_error_text(fx.EMAIL_PAGE) is None
    long = '<div class="alert alert-danger">Oops ' + "x" * 500 + "</div>"
    text = pc.page_error_text(long)
    assert text.startswith("Oops x") and len(text) == pc._ERROR_MAX_CHARS
    # Nested tags: the text up to the first closing tag, tags stripped.
    assert pc.page_error_text('<div class="alert"><b>Bad</b> login</div>') == "Bad"
    assert pc.page_error_text('<p class="error">  &amp;  </p>') == "&"
    assert pc.page_error_text('<p class="error"></p>') is None


def test_looks_like_challenge_needs_both_markers_or_the_header():
    assert pc._looks_like_challenge(_html(200, fx.CHALLENGE_PAGE))
    assert pc._looks_like_challenge(httpx.Response(200, json={}, headers={"cf-mitigated": "challenge"}))
    assert not pc._looks_like_challenge(_html(200, "<html>challenge-platform only</html>"))
    assert not pc._looks_like_challenge(httpx.Response(200, json={"just a moment": "challenge-platform"}))


def test_memory_store_mirrors_the_lock_policy():
    store = pc.MemorySessionStore(max_failures=2, lock_seconds=100, now=lambda: NOW)
    assert store.load() is None
    assert store.record_login(ok=False, error="email: no")["login_failures"] == 1
    state = store.record_login(ok=False, error="password: no")
    assert state["login_failures"] == 2
    assert state["locked_until"] == (NOW + timedelta(seconds=100)).isoformat()
    store.save_cookies("a@example.com", [{"name": "_session_id", "value": "v"}])
    assert store.load()["locked_until"] is None and store.load()["login_failures"] == 0
    assert store.load()["account"] == "a@example.com"


def test_export_cookies_keeps_procore_cookies_only(fake, clock):
    with _logged_in(fake, clock) as session:
        session._load_store()
        session._client.cookies.set("tracker", "x", domain="evil.example.com", path="/")
        exported = session._export_cookies()
    assert [c["name"] for c in exported] == ["_session_id"]
    assert set(exported[0]) == {"name", "value", "domain", "path", "expires", "secure"}


def test_form_encoding_matches_what_rails_reads():
    """The email POST body is application/x-www-form-urlencoded with the
    bracketed Rails names intact (a sanity check on the fake's parser)."""
    body = urlencode({"session[email]": EMAIL, "session[remember_me]": "true"})
    assert parse_qs(body)["session[email]"] == [EMAIL]


def test_unlink_quietly_ignores_a_missing_file(tmp_path):
    pc._unlink_quietly(tmp_path / "missing")
    path = tmp_path / "there"
    path.write_bytes(b"x")
    pc._unlink_quietly(path)
    assert not os.path.exists(path)
