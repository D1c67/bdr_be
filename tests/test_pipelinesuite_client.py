"""The PipelineSuite portal client (app/services/pipelinesuite_client) driven
through httpx.MockTransport over the scrubbed captures in
tests/fixtures_pipelinesuite, with an in-memory session store and the
pace's sleep, clock and rng patched so nothing waits
(docs/RFP_PIPELINESUITE.md sections 2, 3 and 8).

The fake portal reproduces what was captured 2026-09-16:

  GET /ehPipelineSubs/dspProject/projectID/<id> anonymous -> 302 to
  /general/index/next/<b64 of the route>; GET that -> 200 the portalLogin
  form (sets CFID / CFTOKEN / JSESSIONID); POST /ehPipelineSubs/login/ with
  the right Project ID and Security Key -> 302 to the project page, with a
  wrong key -> 302 /general/index; the project page with the session -> 200.
  Files answer on opr.pipelinesuite.com with no cookies; the tracker on
  go.pipelinesuite.com answers 200 image/gif (open) or 302 to the portal's
  per-contact auto-login (click), which the client never follows.

Pinned: reference parsing (the upper-case host lowercased, the tracker and
asset hosts skipped, the key with punctuation, each missing piece -> None,
the repr without the key); the login chain (the exact request sequence, the
key in one POST body only, our own `next` and not the page's, the jar saved
under the key fingerprint); every login refusal; one login and one retry
after the session proves gone; the login-interval discipline; the failure
counter, the lock, the one bell naming the portal and availability(); page
parsing over the three captures; bid_due_at in Pacific; tracking parsing
(the pixel, the View Files click, never Yes / No / Unsure, safelinks
unwrapped); the allow / deny list over every URL on both pages and in the
email; the download host rules, the cap, 404 and HTML-as-file; pings; the
pace.
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote_plus, urljoin

import httpx
import pytest

from app.services import pipelinesuite_client as psc
from app.services import procore_client as pc
from tests import fixtures_pipelinesuite as fx

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
PDF = b"%PDF-1.7\n" + b"x" * 3000 + b"\n%%EOF\n"
GIF = b"GIF89a" + b"\x00" * 37

PORTAL = f"https://{fx.CGB_HOST}"
PROJECT_PATH = f"/ehPipelineSubs/dspProject/projectID/{fx.CGB_PROJECT_ID}"
PROJECT_URL = f"{PORTAL}{PROJECT_PATH}"
LOGIN_PAGE_URL = f"{PORTAL}/general/index/next/{fx.CGB_NEXT_TOKEN}"
LOGIN_POST_URL = f"{PORTAL}/ehPipelineSubs/login/"
FILE_URL = f"https://opr.pipelinesuite.com/{fx.CGB_CLIENT_ID}/{fx.CGB_PROJECT_ID}/ITB.pdf"
AUTO_LOGIN = f"{PORTAL}/ehPipelineSubs/login/enc/{fx.CGB_PROJECT_ID}/cne/1000001/c/_dummy_c"
_PROJECT_RE = re.compile(r"^/ehPipelineSubs/dspProject/projectID/(\d+)$")


# ── The fake portal ──────────────────────────────────────────────────────


def _html(status, body, **headers):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


def _redirect(location, **headers):
    return httpx.Response(302, headers={"location": location, **headers})


class FakePortal:
    """MockTransport handler for one PipelineSuite portal, its file host and
    the tracker, with the failure modes the tests arm. Records every request
    so the sequence can be asserted."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.sessions: set[str] = set()      # CFID values past a good login
        self.seq = 0
        self.key = fx.CGB_KEY
        self.project_id = fx.CGB_PROJECT_ID
        self.login_page_mode = "ok"          # ok | noform | 500
        self.login_mode = "ok"               # ok | badkey | elsewhere | offsite | 200
        self.page_mode = "ok"                # ok | 404 | other | login_200 | 500 | elsewhere
        self.file_mode = "ok"                # ok | 404 | 403 | html | 500 | redirect | transport
        self.track_mode = "ok"               # ok | 404 | transport
        self.pdf = PDF

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == fx.CGB_HOST:
            return self._portal(request, path)
        if host == "opr.pipelinesuite.com":
            return self._file(request)
        if host == "go.pipelinesuite.com":
            return self._track(request, path)
        return httpx.Response(500, text=f"unexpected host {host}")

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

    def _form(self, request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def _logged_in(self, request: httpx.Request) -> bool:
        return self._cookies(request).get("CFID") in self.sessions

    def _portal(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "GET" and path.startswith("/general/index/next/"):
            if self.login_page_mode == "500":
                return httpx.Response(502, text="bad gateway")
            if self.login_page_mode == "noform":
                return _html(200, "<html><body>Maintenance</body></html>")
            self.seq += 1
            return httpx.Response(
                200,
                text=fx.load(fx.LOGIN_PAGE_WITH_NEXT),
                headers=[
                    ("content-type", "text/html; charset=utf-8"),
                    ("set-cookie", f"CFID=cf-{self.seq}; path=/"),
                    ("set-cookie", f"CFTOKEN=tok-{self.seq}; path=/"),
                    ("set-cookie", f"JSESSIONID=js{self.seq}cfusion; path=/; HttpOnly"),
                ],
            )
        if request.method == "POST" and path == "/ehPipelineSubs/login/":
            form = self._form(request)
            if self.login_mode == "200":
                return _html(200, fx.load(fx.LOGIN_PAGE_ROOT))
            if self.login_mode == "offsite":
                return _redirect("https://evil.example.com/")
            if self.login_mode == "elsewhere":
                return _redirect(f"{PORTAL}/ehPipelineSubs/dspAllProjects")
            if (
                self.login_mode == "badkey"
                or form.get("portalSecurityKey") != self.key
                or form.get("portalProjectID") != self.project_id
            ):
                return _redirect(f"{PORTAL}/general/index")
            cfid = self._cookies(request).get("CFID")
            if cfid:
                self.sessions.add(cfid)
            decoded = base64.urlsafe_b64decode(form.get("next", "") + "==").decode()
            return _redirect(f"{PORTAL}/{decoded}")
        if request.method == "GET" and _PROJECT_RE.match(path):
            if self.page_mode == "500":
                return httpx.Response(503, text="down")
            if self.page_mode == "404":
                return _html(404, "<html>Not found</html>")
            if not self._logged_in(request):
                token = base64.urlsafe_b64encode(path[1:].encode()).decode().rstrip("=")
                return _redirect(f"{PORTAL}/general/index/next/{token}")
            if self.page_mode == "login_200":
                return _html(200, fx.load(fx.LOGIN_PAGE_ROOT))
            if self.page_mode == "other":
                return _html(200, "<html><body>Something else entirely</body></html>")
            if self.page_mode == "elsewhere":
                return _redirect(f"{PORTAL}/ehPipelineSubs/dspAllProjects")
            return _html(200, fx.load(fx.CGB_PROJECT_PAGE))
        return httpx.Response(404, text="nope")

    def _file(self, request: httpx.Request) -> httpx.Response:
        if self.file_mode == "transport":
            raise httpx.ConnectError("file host unreachable", request=request)
        if self.file_mode in ("403", "404", "500"):
            return _html(int(self.file_mode), "<html>refused</html>")
        if self.file_mode == "html":
            return _html(200, "<html>Not the file</html>")
        if self.file_mode == "redirect":
            return _redirect("https://cdn.pipelinesuite.com/elsewhere.pdf")
        return httpx.Response(200, content=self.pdf, headers={"content-type": "application/pdf"})

    def _track(self, request: httpx.Request, path: str) -> httpx.Response:
        if self.track_mode == "transport":
            raise httpx.ReadTimeout("slow tracker", request=request)
        if self.track_mode == "404":
            return httpx.Response(404, text="gone")
        if path == "/wf/open":
            return httpx.Response(200, content=GIF, headers={"content-type": "image/gif"})
        return _redirect(AUTO_LOGIN)


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
    monkeypatch.setattr(psc, "_last_request_at", 0.0)


@pytest.fixture
def fake():
    return FakePortal()


@pytest.fixture
def clock():
    return Clock()


def _ref(key=fx.CGB_KEY, host=fx.CGB_HOST, project_id=fx.CGB_PROJECT_ID):
    return psc.PipelineSuiteRef(host=host, project_id=project_id, security_key=key)


REF = _ref()


def _store(**state):
    store = pc.MemorySessionStore(max_failures=3, lock_seconds=21600, now=lambda: NOW)
    store.state = dict(state)
    return store


def _config(**over):
    base = dict(account=REF.fingerprint, min_request_interval=0.0, login_min_interval=600)
    base.update(over)
    return psc.PipelineSuiteConfig(**base)


def _session(fake, clock, store=None, config=None, ref=REF, **kw):
    return psc.PipelineSuiteSession(
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


def _stored_jar(cfid="stored", account=REF.fingerprint, **over):
    state = dict(
        account=account,
        cookies=[
            {"name": "CFID", "value": cfid, "domain": fx.CGB_HOST, "path": "/"},
            {"name": "CFTOKEN", "value": "t", "domain": fx.CGB_HOST, "path": "/"},
        ],
        logged_in_at=(NOW - timedelta(days=1)).isoformat(),
    )
    state.update(over)
    return _store(**state)


# ── Reference parsing (pure) ─────────────────────────────────────────────


def test_parse_reference_over_both_captured_bodies():
    ref = psc.parse_reference(fx.load(fx.CGB_EMAIL_TEXT))
    assert ref == psc.PipelineSuiteRef(fx.CGB_HOST, fx.CGB_PROJECT_ID, fx.CGB_KEY)
    assert ref.label == fx.CGB_LABEL
    assert ref.external_key == f"pipelinesuite:{fx.CGB_LABEL}:{fx.CGB_PROJECT_ID}"
    assert ref.session_provider == f"pipelinesuite:{fx.CGB_HOST}"
    assert ref.project_url == PROJECT_URL and ref.external_url == PROJECT_URL
    assert ref.next_token == fx.CGB_NEXT_TOKEN and "=" not in ref.next_token
    assert base64.urlsafe_b64decode(ref.next_token + "==") == (
        f"ehPipelineSubs/dspProject/projectID/{fx.CGB_PROJECT_ID}".encode()
    )
    assert ref.login_page_url == LOGIN_PAGE_URL and ref.login_post_url == LOGIN_POST_URL
    assert ref.origin == PORTAL
    shf = psc.parse_reference(fx.load(fx.SHF_EMAIL_TEXT))
    assert shf == psc.PipelineSuiteRef(fx.SHF_HOST, fx.SHF_PROJECT_ID, fx.SHF_KEY)
    assert shf.next_token == fx.SHF_NEXT_TOKEN
    assert shf.external_key == "pipelinesuite:shfcontracting:377691"


def test_reference_repr_and_str_never_carry_the_key():
    ref = _ref(key="gp!SECRET@x")
    for text in (repr(ref), str(ref), f"{ref}", repr([ref]), repr({"ref": ref})):
        assert "SECRET" not in text
        assert fx.CGB_HOST in text and fx.CGB_PROJECT_ID in text
    assert ref.security_key == "gp!SECRET@x"
    assert ref.fingerprint == psc.key_fingerprint("gp!SECRET@x")
    assert len(ref.fingerprint) == 16 and "SECRET" not in ref.fingerprint
    assert psc.key_fingerprint("a") != psc.key_fingerprint("b")


def test_parse_reference_skips_the_platform_hosts_and_reads_the_upper_case_host():
    body = (
        "See http://go.pipelinesuite.com/ls/click?upn=x and https://opr.pipelinesuite.com/1/2/a.pdf "
        "and https://cdn.pipelinesuite.com/x.png and https://www.pipelinesuite.com/ first.\n"
        "Note: you can login at HTTPS://CGANDBINC.PIPELINESUITE.COM\n"
        "Project ID:     377363\nSecurity Key:   KEY!DUMMY@\n"
    )
    ref = psc.parse_reference(body)
    assert ref.host == fx.CGB_HOST and ref.project_id == "377363" and ref.security_key == "KEY!DUMMY@"
    # Only platform hosts: no reference.
    only_platform = body.replace("HTTPS://CGANDBINC.PIPELINESUITE.COM", "")
    assert psc.parse_reference(only_platform) is None
    # A lookalike host is not a portal.
    assert psc.parse_reference(body.replace("CGANDBINC.PIPELINESUITE.COM", "cgandbinc.pipelinesuite.com.evil.example")) is None
    assert psc.portal_label("go.pipelinesuite.com") is None
    assert psc.portal_label("Cgandbinc.Pipelinesuite.com") == "cgandbinc"
    assert psc.portal_label("bad_label.pipelinesuite.com") is None
    assert psc.portal_label("pipelinesuite.com") is None


@pytest.mark.parametrize(
    "drop",
    ["HTTPS://CGANDBINC.PIPELINESUITE.COM", "Project ID:     377363", "Security Key:   KEY!DUMMY@"],
)
def test_parse_reference_needs_all_three_pieces(drop):
    body = fx.load(fx.CGB_EMAIL_TEXT).replace(drop, "")
    assert psc.parse_reference(body) is None
    assert psc.parse_reference(None) is None
    assert psc.parse_reference("") is None


def test_parse_reference_bounds_the_id_and_the_key():
    base = "login at https://cgandbinc.pipelinesuite.com\nProject ID: {pid}\nSecurity Key: {key}\n"
    assert psc.parse_reference(base.format(pid="1" * 13, key="k")) is None
    assert psc.parse_reference(base.format(pid="1" * 12, key="k")).project_id == "1" * 12
    assert psc.parse_reference(base.format(pid="1", key="k" * 65)) is None
    assert psc.parse_reference(base.format(pid="1", key="k" * 64)).security_key == "k" * 64
    assert psc.parse_reference(base.format(pid="abc", key="k")) is None
    # Punctuation-heavy keys are taken whole; a trailing note does not join them.
    assert psc.parse_reference(base.format(pid="7", key="a!b@c#d$%^&*")).security_key == "a!b@c#d$%^&*"


# ── Login flow ───────────────────────────────────────────────────────────


def test_login_flow_follows_the_captured_chain_exactly(fake, clock):
    store = _store()
    session = _session(fake, clock, store)
    with session:
        page = session.get_project_page()
    assert 'id="projectInfo"' in page
    assert fake.paths == [
        ("GET", fx.CGB_HOST, PROJECT_PATH),                              # 302: the session is gone
        ("GET", fx.CGB_HOST, f"/general/index/next/{fx.CGB_NEXT_TOKEN}"),
        ("POST", fx.CGB_HOST, "/ehPipelineSubs/login/"),
        ("GET", fx.CGB_HOST, PROJECT_PATH),                              # the one retry
    ]
    # The key appears in exactly one request body and nowhere else.
    carrying = [
        r for r in fake.requests
        if fx.CGB_KEY in unquote_plus(r.content.decode()) or fx.CGB_KEY in unquote_plus(str(r.url))
    ]
    assert [(r.method, r.url.path) for r in carrying] == [("POST", "/ehPipelineSubs/login/")]
    form = fake._form(fake.requests[2])
    assert form == {
        "next": fx.CGB_NEXT_TOKEN,                 # ours, not the page's confirmResponse landing
        "portalProjectID": fx.CGB_PROJECT_ID,
        "portalSecurityKey": fx.CGB_KEY,
    }
    assert fx.LOGIN_PAGE_NEXT not in fake.requests[2].content.decode()
    post = fake.requests[2]
    assert post.headers["origin"] == PORTAL and post.headers["referer"] == LOGIN_PAGE_URL
    assert post.headers["content-type"] == "application/x-www-form-urlencoded"
    assert fake.requests[0].headers["accept"].startswith("text/html")
    assert "Mozilla/5.0" in fake.requests[0].headers["user-agent"]
    # The login page's cookies rode along on the POST and the retry.
    assert fake._cookies(post)["CFID"] == "cf-1"
    assert fake._cookies(fake.requests[3])["CFID"] == "cf-1"
    # The jar is saved under the key fingerprint, never the key; failures cleared.
    assert store.state["account"] == REF.fingerprint
    names = {(c["name"], c["domain"]) for c in store.state["cookies"]}
    assert ("CFID", fx.CGB_HOST) in names and ("CFTOKEN", fx.CGB_HOST) in names
    assert store.state["login_failures"] == 0 and store.state["locked_until"] is None
    assert store.state["logged_in_at"] == NOW.isoformat()
    assert store.state["last_login_attempt_at"] == NOW.isoformat()
    assert session.logged_in_this_session is True
    assert fx.CGB_KEY not in repr(store.state)
    assert session.provider == "pipelinesuite"


def test_login_does_not_happen_while_the_stored_jar_still_works(fake, clock):
    fake.sessions.add("stored")
    store = _stored_jar()
    with _session(fake, clock, store) as session:
        assert 'id="projectInfo"' in session.get_project_page()
    assert fake.paths == [("GET", fx.CGB_HOST, PROJECT_PATH)]
    assert fake._cookies(fake.requests[0])["CFID"] == "stored"
    assert store.state.get("last_used_at") == NOW.isoformat()


def test_a_jar_saved_for_a_rotated_key_is_ignored(fake, clock):
    fake.sessions.add("theirs")
    store = _stored_jar(cfid="theirs", account=psc.key_fingerprint("old-key"))
    with _session(fake, clock, store) as session:
        session.get_project_page()
    assert "cookie" not in fake.requests[0].headers
    assert ("POST", fx.CGB_HOST, "/ehPipelineSubs/login/") in fake.paths
    assert store.state["account"] == REF.fingerprint


def test_bad_key_fails_the_login_at_the_key_step(fake, clock):
    fake.login_mode = "badkey"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(psc.PipelineSuiteUnavailable) as exc:
        session.get_project_page()
    cause = exc.value.__cause__
    assert isinstance(cause, psc.PipelineSuiteLoginFailed) and cause.step == "key"
    assert str(cause) == "The portal rejected the Project ID and Security Key."
    assert exc.value.locked_until == NOW + timedelta(seconds=600)
    assert store.state["login_failures"] == 1 and store.state["last_error"] == f"key: {cause}"
    assert fx.CGB_KEY not in str(exc.value) and fx.CGB_KEY not in repr(store.state)
    assert fake.paths[-1] == ("POST", fx.CGB_HOST, "/ehPipelineSubs/login/")


def test_a_wrong_key_in_the_reference_is_rejected_by_the_portal(fake, clock):
    with _session(fake, clock, ref=_ref(key="not-the-key")) as session:
        with pytest.raises(psc.PipelineSuiteUnavailable) as exc:
            session.get_project_page()
    assert exc.value.__cause__.step == "key"


def test_unexpected_login_page_fails_at_login_page(fake, clock):
    fake.login_page_mode = "noform"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(psc.PipelineSuiteUnavailable) as exc:
        session.get_project_page()
    assert exc.value.__cause__.step == "login_page"
    assert "unexpected page" in str(exc.value.__cause__)
    assert store.state["login_failures"] == 1
    assert not any(m == "POST" for m, _, _ in fake.paths)


@pytest.mark.parametrize(
    "mode, step, needle",
    [
        ("200", "login", "did not answer the login with a redirect"),
        ("offsite", "redirect", "off the portal"),
        ("elsewhere", "redirect", "somewhere unexpected"),
    ],
)
def test_other_login_answers_name_their_step(fake, clock, mode, step, needle):
    fake.login_mode = mode
    with _session(fake, clock) as session, pytest.raises(psc.PipelineSuiteUnavailable) as exc:
        session.get_project_page()
    assert exc.value.__cause__.step == step and needle in str(exc.value.__cause__)
    assert not any(r.url.host == "evil.example.com" for r in fake.requests)
    assert not any(p.endswith("/dspAllProjects") for _, _, p in fake.paths)


def test_a_5xx_during_the_login_is_transient_and_records_no_failure(fake, clock):
    fake.login_page_mode = "500"
    store = _store()
    with _session(fake, clock, store) as session, pytest.raises(psc.PipelineSuiteTransient):
        session.get_project_page()
    assert store.state.get("login_failures", 0) == 0


# ── Session expiry, retry and the login discipline ───────────────────────


@pytest.mark.parametrize("mode", ["redirect", "login_200"])
def test_session_gone_means_one_login_and_one_retry(fake, clock, mode):
    fake.sessions.add("stored")
    store = _stored_jar()
    if mode == "login_200":
        fake.page_mode = "login_200"
        fake.sessions.discard("stored")

        original = fake._portal

        def once(request, path):
            resp = original(request, path)
            if _PROJECT_RE.match(path) and fake._logged_in(request):
                fake.page_mode = "ok"
                return original(request, path)
            return resp

        fake._portal = once
    else:
        fake.sessions.discard("stored")        # the stored jar is stale
    with _session(fake, clock, store) as session:
        assert 'id="projectInfo"' in session.get_project_page()
        gets = [p for p in fake.paths if p[2] == PROJECT_PATH]
        assert len(gets) == 2
        assert sum(1 for p in fake.paths if p[0] == "POST") == 1
        before = len(fake.paths)
        # A later call reuses the jar: no second login.
        session.get_project_page()
    assert fake.paths[before:] == [("GET", fx.CGB_HOST, PROJECT_PATH)]


def test_second_login_inside_the_interval_is_refused_without_a_request(fake, clock):
    fake.login_mode = "badkey"
    store = _store()
    with _session(fake, clock, store) as session:
        with pytest.raises(psc.PipelineSuiteUnavailable):
            session.get_project_page()
        after_first = len(fake.paths)
        with pytest.raises(psc.PipelineSuiteUnavailable) as exc:
            session.get_project_page()
    assert "attempted recently" in str(exc.value)
    assert exc.value.locked_until == NOW + timedelta(seconds=600)
    assert fake.paths[after_first:] == [("GET", fx.CGB_HOST, PROJECT_PATH)]
    assert store.state["login_failures"] == 1


def test_a_recent_attempt_recorded_by_another_worker_also_defers_the_login(fake, clock):
    store = _store(last_login_attempt_at=(NOW - timedelta(seconds=30)).isoformat())
    with _session(fake, clock, store) as session, pytest.raises(psc.PipelineSuiteUnavailable) as exc:
        session.get_project_page()
    assert exc.value.locked_until == NOW - timedelta(seconds=30) + timedelta(seconds=600)
    assert fake.paths == [("GET", fx.CGB_HOST, PROJECT_PATH)]


def test_failure_counter_locks_logins_rings_once_naming_the_portal_and_reports_availability(fake, clock):
    fake.login_mode = "badkey"
    store = _store()
    bells = []
    session = _session(
        fake, clock, store, config=_config(login_min_interval=0),
        on_lock=lambda until, error, host: bells.append((until, error, host)),
    )
    with session:
        for _ in range(2):
            with pytest.raises(psc.PipelineSuiteUnavailable) as exc:
                session.get_project_page()
            assert not isinstance(exc.value, psc.PipelineSuiteLoginLocked)
        assert session.availability() == (True, None, None)
        with pytest.raises(psc.PipelineSuiteLoginLocked) as locked:
            session.get_project_page()
        until = NOW + timedelta(seconds=21600)
        assert locked.value.locked_until == until
        assert str(locked.value) == f"PipelineSuite logins for {fx.CGB_HOST} are locked after repeated failures."
        assert store.state["login_failures"] == 3 and store.state["locked_until"] == until.isoformat()
        assert bells == [(until, "The portal rejected the Project ID and Security Key.", fx.CGB_HOST)]
        before = len(fake.paths)
        with pytest.raises(psc.PipelineSuiteLoginLocked):
            session.get_project_page()
        assert fake.paths[before:] == [("GET", fx.CGB_HOST, PROJECT_PATH)]
        assert bells[1:] == []
        assert session.availability() == (
            False, f"PipelineSuite logins for {fx.CGB_HOST} are locked after repeated failures.", until
        )
    assert fx.CGB_KEY not in repr(store.state) and fx.CGB_KEY not in repr(bells)


def test_a_bell_that_raises_never_masks_the_lock(fake, clock):
    fake.login_mode = "badkey"
    store = _store(login_failures=2)

    def boom(until, error, host):
        raise RuntimeError("notifications down")

    with _session(fake, clock, store, config=_config(login_min_interval=0), on_lock=boom) as session:
        with pytest.raises(psc.PipelineSuiteLoginLocked):
            session.get_project_page()
    assert store.state["login_failures"] == 3


def test_an_expired_lock_no_longer_stops_a_login(fake, clock):
    store = _store(locked_until=(NOW - timedelta(seconds=1)).isoformat(), login_failures=3)
    with _session(fake, clock, store) as session:
        assert session.availability() == (True, None, None)
        session.get_project_page()
    assert store.state["login_failures"] == 0


def test_a_newer_jar_from_another_worker_is_tried_before_a_login(fake, clock):
    fake.sessions.add("fresh")
    store = _stored_jar(cfid="stale", logged_in_at=(NOW - timedelta(hours=2)).isoformat())
    session = _session(fake, clock, store)
    session._load_store()
    store.state["cookies"] = [{"name": "CFID", "value": "fresh", "domain": fx.CGB_HOST, "path": "/"}]
    store.state["logged_in_at"] = (NOW - timedelta(minutes=1)).isoformat()
    with session:
        session.get_project_page()
    assert fake.paths == [("GET", fx.CGB_HOST, PROJECT_PATH)] * 2
    assert fake._cookies(fake.requests[0])["CFID"] == "stale"
    assert fake._cookies(fake.requests[1])["CFID"] == "fresh"
    assert session.logged_in_this_session is False


def test_expired_cookies_in_the_store_are_skipped(fake, clock):
    fake.sessions.add("old")
    store = _stored_jar(
        cfid="old",
        cookies=[{
            "name": "CFID", "value": "old", "domain": fx.CGB_HOST, "path": "/",
            "expires": (NOW - timedelta(days=1)).timestamp(),
        }],
    )
    with _session(fake, clock, store) as session:
        session.get_project_page()
    assert "cookie" not in fake.requests[0].headers


# ── Project page answers ─────────────────────────────────────────────────


def _logged_in(fake, clock, **kw):
    fake.sessions.add("ok")
    return _session(fake, clock, _stored_jar(cfid="ok"), **kw)


def test_project_page_404_is_forbidden_and_names_the_project(fake, clock):
    fake.page_mode = "404"
    with _logged_in(fake, clock) as session, pytest.raises(psc.PipelineSuiteForbidden) as exc:
        session.get_project_page()
    assert str(exc.value) == f"The portal has no project {fx.CGB_PROJECT_ID} for this Security Key."
    assert not any(m == "POST" for m, _, _ in fake.paths)


@pytest.mark.parametrize("mode", ["other", "elsewhere"])
def test_project_page_of_another_shape_is_unavailable(fake, clock, mode):
    fake.page_mode = mode
    with _logged_in(fake, clock) as session, pytest.raises(psc.PipelineSuiteUnavailable) as exc:
        session.get_project_page()
    assert str(exc.value) == "The portal answered with a page the harvest does not understand."
    assert not any(p.endswith("/dspAllProjects") for _, _, p in fake.paths)


def test_project_page_5xx_and_transport_trouble_are_transient(fake, clock):
    fake.page_mode = "500"
    with _logged_in(fake, clock) as session, pytest.raises(psc.PipelineSuiteTransient):
        session.get_project_page()

    def dead(request):
        raise httpx.ConnectError("no route", request=request)

    session = psc.PipelineSuiteSession(
        _config(), _store(), REF, transport=httpx.MockTransport(dead),
        sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0, now=lambda: NOW,
    )
    with session, pytest.raises(psc.PipelineSuiteTransient) as exc:
        session.get_project_page()
    assert "ConnectError" in str(exc.value)


# ── Page parsing (pure) ──────────────────────────────────────────────────


def test_parse_project_page_over_the_cgb_capture():
    page = psc.parse_project_page(fx.load(fx.CGB_PROJECT_PAGE))
    assert page.logged_in is True
    assert page.gc_name == "CG&B Inc."
    assert page.title == "Install Scoreboard on Soccer Field Cimarron Memorial High School"
    assert page.invited_name == "Thomas Moore with G3 Electrical Technologies"
    assert page.response_recorded is True                      # "Yes" pre-checked
    assert [(t["code"], t["name"]) for t in page.trades] == [("26000", "Electrical")]
    assert page.info == {
        "project_number": "MPID#0019122",
        "project_name": "Install Scoreboard on Soccer Field Cimarron Memorial High School",
        "location": None,
        "address": "2301 N Tenaya Way",                        # the Map It button dropped
        "city": "Las Vegas",
        "state": "NV",
        "zip": "89128",
        "bid_date": "September 22, 2026",
        "bid_time": "1:00 PM",
        "scope": (
            '***************************CLICK "YES", "NO", OR "UNSURE" ABOVE FOR YOUR RESPONSE'
            "*********************\n\n"
            "Installation, excavation and trenching for underground electrical, concrete "
            "foundations and anchor systems, structural steel support poles, mounting and "
            "securing scoreboard assemblly and restoration of disturbed areas.\n\n"
            "CONTACT JEFF WASSON WITH RFI'S J.WASSON@CGANDBINC.COM"
        ),
        "plans": None,
        "other_info": "PRIVATE WAGES",
    }
    assert page.notices == [] and page.contacts == []
    assert [f["name"] for f in page.files] == list(fx.CGB_FILE_NAMES)
    assert [f["size_kb"] for f in page.files] == list(fx.CGB_FILE_SIZES_KB)
    assert [f["file_id"] for f in page.files] == list(fx.CGB_FILE_IDS)
    assert all(f["folder"] == "" and f["uploaded_on"] == "9/10/2026" for f in page.files)
    assert page.files[3]["url"] == (
        f"https://opr.pipelinesuite.com/{fx.CGB_CLIENT_ID}/{fx.CGB_PROJECT_ID}/Specs%20-%20Score%20Board.pdf"
    )
    assert page.files[3]["file_path"] == f"opr.pipelinesuite.com/{fx.CGB_CLIENT_ID}/{fx.CGB_PROJECT_ID}/Specs - Score Board.pdf"
    for f in page.files:
        psc.assert_allowed("GET", f["url"])
    assert psc.bid_due_at(page.info) == "2026-09-22T13:00:00-07:00"
    # No form token, no key anywhere in the parsed facts.
    dumped = repr(page)
    assert "_dummy_c" not in dumped and "KEY%21DUMMY" not in dumped and "1000001" not in dumped


def test_parse_project_page_over_the_notices_and_contacts_capture():
    page = psc.parse_project_page(fx.load(fx.CGB_PROJECT_PAGE_NOTICES))
    assert page.title == "Access Road & Security Fence Repairs - Floyd Edsall Training Center"
    assert page.response_recorded is False                     # nothing checked
    assert page.info["project_number"] == "43ADG-S3999"
    assert page.info["address"] is None and page.info["city"] is None
    assert page.info["bid_date"] == "September 23, 2026" and page.info["bid_time"] == "11:00 AM"
    assert page.info["other_info"] is None
    assert page.notices == [{"title": "Amendment # 2", "created_by": "Camilot Bradburn", "created_on": "9/15/2026"}]
    assert page.contacts == [{
        "company": "CG&B Enterprises, Inc.", "name": "Jeff Wasson", "title": "Estimator",
        "phone": "702-565-6564", "extension": None, "fax": None, "email": "j.wasson@cgandbinc.com",
    }]
    names = [f["name"] for f in page.files]
    assert len(names) == 7 and "21081-_1.PDF" in names            # the upper-case extension kept
    assert names[1] == "2026-06-05 NVARNG ACC Gate Replacement  Bid Set.pdf"
    assert psc.bid_due_at(page.info) == "2026-09-23T11:00:00-07:00"


def test_parse_project_page_over_the_shf_capture_walks_the_folder():
    page = psc.parse_project_page(fx.load(fx.SHF_PROJECT_PAGE))
    assert page.gc_name == "SHF International LLC"
    assert page.invited_name == "Thomas Moore(702-555-0100) with G3 Electrical"
    assert [(t["code"], t["name"]) for t in page.trades] == [("26100", "Electrical")]
    assert page.info["location"] == "Henderson, Nevada"
    assert page.info["address"] == "2300 Pebble Road" and page.info["zip"] == "89074"
    assert page.info["scope"].startswith("Tenant improvements to existing fire station")
    assert len(page.files) == 13
    assert {f["folder"] for f in page.files} == {"Addendum 01"}
    first = page.files[0]
    # The trailing space before .pdf is squeezed in the name and kept, encoded, in the URL.
    assert first["name"] == "Addendum 1 - Scope of Work - Fire Station 95 Interior Renovation - IFB 112-27.pdf"
    assert first["url"].endswith("IFB%20112-27%20.pdf")
    assert first["url"].startswith(f"https://opr.pipelinesuite.com/{fx.SHF_CLIENT_ID}/{fx.SHF_PROJECT_ID}/")
    # An extension-less data-text: the name still comes from data-file-path.
    docx = [f for f in page.files if f["name"].endswith(".docx")]
    assert len(docx) == 6 and docx[0]["name"] == "Apprenticeship Utilization Act Project Workforce Checklist.docx"
    assert [f["size_kb"] for f in page.files][:3] == [135, 1738, 17]
    for f in page.files:
        psc.assert_allowed("GET", f["url"])
    assert psc.bid_due_at(page.info) == "2026-09-24T11:00:00-07:00"


def test_parse_project_page_over_the_login_page_and_junk_is_empty():
    page = psc.parse_project_page(fx.load(fx.LOGIN_PAGE_WITH_NEXT))
    assert page.logged_in is False and page.files == [] and page.trades == []
    assert page.info["project_name"] is None and page.gc_name is None
    page = psc.parse_project_page("<html><body><p>nothing</p></body></html>")
    assert page.logged_in is False and page.title is None and page.contacts == []
    assert psc.parse_project_page("").logged_in is False


def test_parse_files_handles_nested_folders_and_odd_paths():
    html = """
    <div class="opr_files"><ul>
      <li class="folder" data-text="A"><span class="folderName">A</span>
        <ul>
          <li class="folder" data-text="B"><ul>
            <li class="file" data-file-path="opr.pipelinesuite.com/1/2/deep &amp; file .PDF" data-file-id="9"
                data-text="deep" data-file-size="1,024" data-uploaded-on="1/2/2026"></li>
          </ul></li>
          <li class="file" data-file-path="opr.pipelinesuite.com/1/2/x.pdf" data-file-id="10" data-file-size=""></li>
        </ul>
      </li>
      <li class="file" data-file-path="elsewhere.example/1/2/y.pdf" data-file-id="11" data-file-size="5"></li>
      <li class="file" data-file-path="" data-text="named only" data-file-size="5"></li>
    </ul></div>"""
    files = psc.parse_project_page(html).files
    assert [(f["folder"], f["name"], f["size_kb"]) for f in files] == [
        ("A/B", "deep & file.PDF", 1024), ("A", "x.pdf", None), ("", "y.pdf", 5), ("", "named only", 5),
    ]
    assert files[0]["url"] == "https://opr.pipelinesuite.com/1/2/deep%20%26%20file%20.PDF"
    assert files[2]["url"] is None and files[3]["url"] is None
    assert files[3]["file_path"] is None and files[3]["file_id"] is None
    assert psc.squeeze_name("  name  .docx ") == "name.docx"
    assert psc.squeeze_name("no ext ") == "no ext"


def test_bid_due_at_combines_in_pacific_and_degrades():
    assert psc.bid_due_at({"bid_date": "September 22, 2026", "bid_time": "1:00 PM"}) == "2026-09-22T13:00:00-07:00"
    assert psc.bid_due_at({"bid_date": "January 5, 2027", "bid_time": "2:30 PM"}) == "2027-01-05T14:30:00-08:00"
    assert psc.bid_due_at({"bid_date": "9/22/2026", "bid_time": "13:00"}) == "2026-09-22T13:00:00-07:00"
    assert psc.bid_due_at({"bid_date": "Sep 22, 2026", "bid_time": "1:00 p.m."}) == "2026-09-22T13:00:00-07:00"
    assert psc.bid_due_at({"bid_date": "September 22, 2026", "bid_time": None}) == "2026-09-22"
    assert psc.bid_due_at({"bid_date": "September 22, 2026", "bid_time": "noonish"}) == "2026-09-22"
    assert psc.bid_due_at({"bid_date": "TBD", "bid_time": "1:00 PM"}) is None
    assert psc.bid_due_at({}) is None


@pytest.mark.parametrize(
    "name, kind",
    [
        ("CIMARRON MEMORIAL HS PROJECT MANUAL DWG.pdf", "drawing"),
        ("Compiled Set-sm.pdf", "drawing"),
        ("2026-06-05 NVARNG ACC Gate Replacement  Bid Set.pdf", "drawing"),
        ("E-101 plans.pdf", "drawing"),
        ("site.dwg", "drawing"),
        ("Sheets A1-A9.pdf", "drawing"),
        ("Project Manual.pdf", "specification"),
        ("ITB.pdf", "specification"),
        ("Specs - Score Board.pdf", "specification"),
        ("Addendum 1 - Scope of Work.pdf", "specification"),
        ("Amendment 2.pdf", "specification"),
        ("Construction IFB Terms  Conditions.pdf", "specification"),
        ("RFP 12.docx", "specification"),
        ("Bidders Preference Affidavit.pdf", "other"),
        ("Ownership Disclosure Form.docx", "other"),
        ("21081-_1.PDF", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_classify_name(name, kind):
    assert psc.classify_name(name) == kind


# ── Tracking (pure) ──────────────────────────────────────────────────────


def test_parse_tracking_finds_the_pixel_and_the_view_files_click_only():
    tracking = psc.parse_tracking(fx.load(fx.CGB_EMAIL_HTML))
    assert tracking.open_url == fx.OPEN_PIXEL_URL
    assert tracking.click_url == fx.VIEW_FILES_URL          # safelinks unwrapped to the bare tracker
    assert "safelinks" not in tracking.click_url
    for token in fx.RESPONSE_TOKENS:
        assert token not in (tracking.click_url or "") and token not in (tracking.open_url or "")
    assert psc.parse_tracking(None) == psc.Tracking(None, None)
    assert psc.parse_tracking("<html><body>no links</body></html>") == psc.Tracking(None, None)


def test_parse_tracking_never_returns_a_yes_no_unsure_anchor():
    html = """
    <a href="http://go.pipelinesuite.com/ls/click?upn=u001.YES"><span>Yes</span> </a>
    <a href="http://go.pipelinesuite.com/ls/click?upn=u001.NO">No</a>
    <a href="http://go.pipelinesuite.com/ls/click?upn=u001.UNSURE">Unsure</a>
    <a href="https://example.com/view">View Files and Project Details</a>
    """
    assert psc.parse_tracking(html) == psc.Tracking(None, None)
    # The anchor text decides; the View Files label on a tracker link wins,
    # whatever its position, and Outlook's originalsrc is read when the href
    # was not a tracker link.
    html = """
    <a href="http://go.pipelinesuite.com/ls/click?upn=u001.YES">Yes</a>
    <a href="https://example.com/x" originalsrc="http://go.pipelinesuite.com/ls/click?upn=u001.VIEW">
      <span>View  Files and Project Details</span></a>
    <img src="https://cdn.pipelinesuite.com/logo.png"><img src="http://go.pipelinesuite.com/wf/open?upn=u001.OPEN">
    """
    assert psc.parse_tracking(html) == psc.Tracking(
        "http://go.pipelinesuite.com/wf/open?upn=u001.OPEN",
        "http://go.pipelinesuite.com/ls/click?upn=u001.VIEW",
    )
    # A tracker path other than the two is never a match.
    html = '<a href="http://go.pipelinesuite.com/ls/other?upn=1">view files</a><img src="http://go.pipelinesuite.com/wf/beacon?upn=2">'
    assert psc.parse_tracking(html) == psc.Tracking(None, None)


def test_parse_click_link_reads_the_text_body_fallback():
    assert psc.parse_click_link(fx.load(fx.CGB_EMAIL_TEXT)) == fx.VIEW_FILES_URL
    assert psc.parse_click_link(fx.load(fx.SHF_EMAIL_TEXT)) == "http://go.pipelinesuite.com/ls/click?upn=u001.DUMMYSHF"
    assert psc.parse_click_link("Yes <http://go.pipelinesuite.com/ls/click?upn=u001.YES>") is None
    assert psc.parse_click_link("View Files and Project Details <https://example.com/>") is None
    assert psc.parse_click_link(None) is None and psc.parse_click_link("") is None


# ── Allowlist and deny list ──────────────────────────────────────────────


_ATTR_RE = re.compile(r'\b(?:href|src|action|originalsrc)\s*=\s*"([^"]+)"', re.IGNORECASE)


def _page_urls(html: str, base: str) -> list[str]:
    out = []
    for raw in _ATTR_RE.findall(html):
        raw = raw.replace("&amp;", "&").strip()
        if raw.startswith("#") or raw.startswith("mailto:") or raw.startswith("javascript:"):
            out.append(raw)
            continue
        out.append(urljoin(base, raw))
    return out


def test_every_url_on_both_project_pages_and_the_login_page_is_refused(fake):
    """The pages link to the bid question, the RFI and bid upload forms, the
    account routes, the zip download, the All Projects list, PipelineBid
    with the key in the path, Google Maps and the CDN. None of it is
    reachable; only the login form's own action (a POST) is."""
    for name, project_id in ((fx.CGB_PROJECT_PAGE, fx.CGB_PROJECT_ID), (fx.CGB_PROJECT_PAGE_NOTICES, fx.CGB_PROJECT_ID_NOTICES), (fx.SHF_PROJECT_PAGE, fx.SHF_PROJECT_ID)):
        html = fx.load(name)
        base = f"{PORTAL}/ehPipelineSubs/dspProject/projectID/{project_id}"
        urls = _page_urls(html, base)
        assert len(urls) > 20
        for url in urls:
            for method in ("GET", "POST"):
                with pytest.raises(ValueError) as exc:
                    psc.assert_allowed(method, url)
                assert "DUMMY" not in str(exc.value) and "KEY" not in str(exc.value)
        assert any("confirmResponse" in u for u in urls)
        assert any("/allFiles/1" in u for u in urls)
        assert any("submitRFI" in u for u in urls) and any("uploadBid" in u for u in urls)
        if name != fx.SHF_PROJECT_PAGE:
            assert any("pipelinebid.com/register" in u for u in urls)
    login = _page_urls(fx.load(fx.LOGIN_PAGE_WITH_NEXT), LOGIN_PAGE_URL)
    allowed = []
    for url in login:
        for method in ("GET", "POST"):
            try:
                psc.assert_allowed(method, url)
                allowed.append((method, url))
            except ValueError:
                pass
    assert allowed == [("POST", LOGIN_POST_URL)]


def test_the_email_links_the_allowlist_admits_are_only_the_tracker_and_the_choice_is_by_text():
    """By URL shape the allowlist admits every go.pipelinesuite.com click
    link (the Yes / No / Unsure ones look exactly like View Files); the
    refusal of the bid-answer links is by anchor text in parse_tracking,
    which is the only source of URLs the job pings. Safelinks wrappers
    themselves are refused outright."""
    urls = _page_urls(fx.load(fx.CGB_EMAIL_HTML), "https://mail.example/")
    admitted = set()
    refused = set()
    for url in urls:
        try:
            psc.assert_allowed("GET", url)
            admitted.add(url)
        except ValueError:
            refused.add(url)
    # As written in the HTML: the safelinks hrefs are refused, the bare
    # originalsrc trackers and the pixel admitted, nothing else.
    assert all(u.startswith("http://go.pipelinesuite.com/") for u in admitted)
    assert all("safelinks" in u or "cdn.pipelinesuite.com" in u or "opr.pipelinesuite.com" in u
               or u.startswith("mailto:") for u in refused)
    assert fx.OPEN_PIXEL_URL in admitted
    assert f"http://go.pipelinesuite.com/ls/click?upn={fx.VIEW_FILES_ORIGINALSRC_TOKEN}" in admitted
    # Unwrapped, every click link (Yes, No, Unsure and View Files alike) has
    # the admitted shape: the allowlist cannot tell them apart by URL.
    unwrapped = {psc.unwrap_link(u) for u in refused if "safelinks" in u}
    for u in unwrapped:
        psc.assert_allowed("GET", u)
    response_links = {u for u in admitted | unwrapped if any(t in u for t in fx.RESPONSE_TOKENS)}
    assert len(response_links) == 12
    # What the job pings is what parse_tracking picked: never one of those.
    tracking = psc.parse_tracking(fx.load(fx.CGB_EMAIL_HTML))
    assert tracking.click_url in unwrapped and tracking.open_url in admitted
    assert tracking.click_url not in response_links and tracking.open_url not in response_links


@pytest.mark.parametrize(
    "method, url",
    [
        ("GET", LOGIN_PAGE_URL),
        ("POST", LOGIN_POST_URL),
        ("GET", PROJECT_URL),
        ("GET", "https://shfcontracting.pipelinesuite.com/ehPipelineSubs/dspProject/projectID/377691"),
        ("GET", FILE_URL),
        ("GET", "https://opr.pipelinesuite.com/1618/377691/Compiled%20Set-sm.pdf"),
        ("GET", fx.OPEN_PIXEL_URL),
        ("GET", fx.VIEW_FILES_URL),
        ("GET", "https://go.pipelinesuite.com/ls/click?upn=u001.x"),
    ],
)
def test_allowlist_admits_the_five_shapes(method, url):
    psc.assert_allowed(method, url)


@pytest.mark.parametrize(
    "method, url",
    [
        ("POST", f"{PORTAL}/ehPipelineSubs/confirmResponse"),
        ("GET", f"{PORTAL}/ehPipelineSubs/confirmResponse"),
        ("GET", AUTO_LOGIN),
        # The response-submit URLs: the auto-login destination plus /r/1|2|3
        # (Yes|No|Unsure). This is what actually records a bid answer; the
        # harvest must never be able to reach it (dev incident 2026-09-16).
        ("GET", f"{AUTO_LOGIN}/r/1"),
        ("GET", f"{AUTO_LOGIN}/r/2"),
        ("GET", f"{AUTO_LOGIN}/r/3"),
        ("POST", f"{AUTO_LOGIN}/r/1"),
        ("POST", f"{PORTAL}/ehPipelineSubs/submitRFI"),
        ("POST", f"{PORTAL}/ehPipelineSubs/uploadBid"),
        ("GET", f"{PORTAL}/ehPipelineSubs/dspUpdateInfo"),
        ("GET", f"{PORTAL}/ehPipelineSubs/logout"),
        ("GET", f"{PROJECT_URL}/allFiles/1"),
        ("POST", f"{PROJECT_URL}/"),
        ("GET", f"{PORTAL}/ehPipelineSubs/dspAllProjects"),
        ("GET", f"{PORTAL}/ehPipelineSubs/dspAllProjects/"),
        ("GET", "https://pipelinebid.com/register/projectID/377363/securityKey/KEY%21DUMMY%40"),
        ("GET", "https://nam09.safelinks.protection.outlook.com/?url=http%3A%2F%2Fgo.pipelinesuite.com%2Fls%2Fclick%3Fupn%3Dx"),
        ("GET", f"http://{fx.CGB_HOST}{PROJECT_PATH}"),
        ("POST", PROJECT_URL),
        ("GET", LOGIN_POST_URL),
        ("POST", LOGIN_PAGE_URL),
        ("GET", f"{PROJECT_URL}/"),
        ("GET", f"{PROJECT_URL}?x=1"),
        ("GET", f"{PROJECT_URL}#viewRespond"),
        ("GET", f"{PORTAL}/general/index"),
        ("GET", f"{PORTAL}/general/index/next/"),
        ("GET", f"{PORTAL}/general/index/next/{fx.CGB_NEXT_TOKEN}?x=1"),
        ("GET", "https://www.pipelinesuite.com/general/index/next/abc"),
        ("GET", "https://go.pipelinesuite.com/ehPipelineSubs/dspProject/projectID/1"),
        ("GET", "http://go.pipelinesuite.com/wf/open"),
        ("GET", "http://go.pipelinesuite.com/ls/other?upn=x"),
        ("GET", "http://go.pipelinesuite.com.evil.example/wf/open?upn=x"),
        ("GET", "http://opr.pipelinesuite.com/1090/377363/ITB.pdf"),
        ("GET", "https://opr.pipelinesuite.com/1090/377363/a/b.pdf"),
        ("GET", "https://opr.pipelinesuite.com/1090/ITB.pdf"),
        ("GET", "https://opr.pipelinesuite.com/1090/377363/ITB.pdf?x=1"),
        ("GET", "https://opr.pipelinesuite.com/1090/CG&BTencate.png"),
        ("GET", "https://storage.procore.com/api/v5/files/x?sig=test"),
        ("GET", "https://cgandbinc.pipelinesuite.com.evil.example" + PROJECT_PATH),
        ("GET", "ftp://cgandbinc.pipelinesuite.com" + PROJECT_PATH),
        ("GET", ""),
        ("GET", "not a url"),
    ],
)
def test_allowlist_refuses_everything_else(method, url):
    with pytest.raises(ValueError) as exc:
        psc.assert_allowed(method, url)
    assert "DUMMY" not in str(exc.value)


def test_requests_check_the_allowlist_before_anything_leaves(fake, clock):
    with _logged_in(fake, clock) as session:
        for method, url in (
            ("POST", f"{PORTAL}/ehPipelineSubs/confirmResponse"),
            ("GET", AUTO_LOGIN),
            ("GET", f"{PROJECT_URL}/allFiles/1"),
            ("GET", "https://evil.example.com" + PROJECT_PATH),
        ):
            with pytest.raises(ValueError):
                session._request(method, url)
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

    monkeypatch.setattr(psc.httpx, "Client", routed)
    return session


def test_download_streams_the_file_without_cookies(downloader, fake, tmp_path):
    dest = tmp_path / "0001.bin"
    with downloader as session:
        assert session._has_portal_cookies()
        written = session.download(FILE_URL, dest, max_bytes=len(PDF))
    assert written == len(PDF) and dest.read_bytes() == PDF
    assert fake.paths == [("GET", "opr.pipelinesuite.com", f"/{fx.CGB_CLIENT_ID}/{fx.CGB_PROJECT_ID}/ITB.pdf")]
    assert "cookie" not in fake.requests[0].headers
    assert fake.requests[0].headers["referer"] == PROJECT_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://opr.pipelinesuite.com/1090/377363/ITB.pdf",
        "https://opr.pipelinesuite.com/1090/ITB.pdf",
        "https://opr.pipelinesuite.com/1090/377363/a/ITB.pdf",
        "https://opr.pipelinesuite.com.evil.example/1090/377363/ITB.pdf",
        "https://storage.procore.com/api/v5/files/x?sig=test",
        f"{PROJECT_URL}/allFiles/1",
    ],
)
def test_download_accepts_only_a_file_on_the_file_host(downloader, fake, tmp_path, url):
    dest = tmp_path / "x.bin"
    with downloader as session, pytest.raises(ValueError):
        session.download(url, dest, max_bytes=10_000)
    assert fake.requests == [] and not dest.exists()


@pytest.mark.parametrize("mode, needle", [("404", "no such file"), ("html", "page instead of the file"), ("403", "refused"), ("redirect", "redirected")])
def test_download_maps_misses_pages_and_redirects_to_forbidden(downloader, fake, tmp_path, mode, needle):
    fake.file_mode = mode
    dest = tmp_path / "x.bin"
    with downloader as session, pytest.raises(psc.PipelineSuiteForbidden) as exc:
        session.download(FILE_URL, dest, max_bytes=10_000)
    assert needle in str(exc.value)
    assert not dest.exists()
    assert [h for _, h, _ in fake.paths] == ["opr.pipelinesuite.com"]


def test_download_enforces_the_byte_cap_and_unlinks(downloader, fake, tmp_path):
    dest = tmp_path / "x.bin"
    with downloader as session, pytest.raises(psc.PipelineSuiteForbidden) as exc:
        session.download(FILE_URL, dest, max_bytes=len(PDF) - 1)
    assert "larger than the harvest accepts" in str(exc.value)
    assert not dest.exists()


def test_download_maps_5xx_and_transport_trouble_to_transient(downloader, fake, tmp_path):
    dest = tmp_path / "x.bin"
    with downloader as session:
        fake.file_mode = "500"
        with pytest.raises(psc.PipelineSuiteTransient):
            session.download(FILE_URL, dest, max_bytes=10_000)
        assert not dest.exists()
        fake.file_mode = "transport"
        with pytest.raises(psc.PipelineSuiteTransient) as exc:
            session.download(FILE_URL, dest, max_bytes=10_000)
        assert "ConnectError" in str(exc.value) and not dest.exists()


def test_download_refuses_to_overwrite_an_existing_file(downloader, fake, tmp_path):
    dest = tmp_path / "x.bin"
    dest.write_bytes(b"keep")
    with downloader as session, pytest.raises(FileExistsError):
        session.download(FILE_URL, dest, max_bytes=10_000)
    assert fake.requests == [] and dest.read_bytes() == b"keep"


# ── Pings ────────────────────────────────────────────────────────────────


def test_ping_hits_the_tracker_once_without_following_or_cookies(downloader, fake):
    with downloader as session:
        assert session.ping(fx.OPEN_PIXEL_URL) == 200
        assert session.ping(fx.VIEW_FILES_URL) == 302
        # A safelinks-wrapped URL is unwrapped before the ping.
        wrapped = (
            "https://nam09.safelinks.protection.outlook.com/?url=http%3A%2F%2Fgo.pipelinesuite.com"
            "%2Fls%2Fclick%3Fupn%3Du001.DUMMY2650&data=DUMMY"
        )
        assert session.ping(wrapped) == 302
    assert fake.paths == [
        ("GET", "go.pipelinesuite.com", "/wf/open"),
        ("GET", "go.pipelinesuite.com", "/ls/click"),
        ("GET", "go.pipelinesuite.com", "/ls/click"),
    ]
    assert all("cookie" not in r.headers for r in fake.requests)
    assert not any("/login/enc/" in p for _, _, p in fake.paths)      # the 302 is never followed
    assert str(fake.requests[2].url) == fx.VIEW_FILES_URL


def test_ping_answers_none_on_transport_trouble_and_refuses_anything_else(downloader, fake):
    with downloader as session:
        fake.track_mode = "transport"
        assert session.ping(fx.OPEN_PIXEL_URL) is None
        fake.track_mode = "404"
        assert session.ping(fx.VIEW_FILES_URL) == 404
        before = len(fake.requests)
        for url in (
            AUTO_LOGIN,
            f"{AUTO_LOGIN}/r/1",
            PROJECT_URL,
            "http://go.pipelinesuite.com/ls/other?upn=x",
            "https://example.com/wf/open?upn=x",
            "http://go.pipelinesuite.com.evil.example/wf/open?upn=x",
        ):
            with pytest.raises(ValueError):
                session.ping(url)
        assert len(fake.requests) == before


# ── Pace ─────────────────────────────────────────────────────────────────


def test_pace_waits_at_least_the_interval_between_requests():
    clock = Clock()
    psc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == []
    psc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0]
    clock.t += 0.5
    psc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0, 1.5]
    clock.t += 10
    psc._pace(2.0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 1.0)
    assert clock.sleeps == [2.0, 1.5]
    for _ in range(2):
        psc._pace(0, sleep=clock.sleep, clock=clock, rng=lambda a, b: 2.0)
    assert clock.sleeps == [2.0, 1.5]


def test_pace_applies_the_random_multiplier_and_has_its_own_state(monkeypatch):
    clock = Clock()
    draws = []

    def rng(lo, hi):
        draws.append((lo, hi))
        return 1.75

    monkeypatch.setattr(pc, "_last_request_at", 5000.0)     # Procore's clock is not ours
    psc._pace(2.0, sleep=clock.sleep, clock=clock, rng=rng)
    psc._pace(2.0, sleep=clock.sleep, clock=clock, rng=rng)
    assert draws == [(1.0, 2.0), (1.0, 2.0)] and clock.sleeps == [3.5]
    assert pc._last_request_at == 5000.0


def test_every_request_of_a_login_a_download_and_a_ping_is_paced(fake, clock, tmp_path):
    session = _session(fake, clock, config=_config(min_request_interval=2.0))
    session._download_client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(fake), follow_redirects=False
    )
    with session:
        session.get_project_page()                     # 302, login page, POST, retry
        session.download(FILE_URL, tmp_path / "a.pdf", max_bytes=10_000)
        session.ping(fx.OPEN_PIXEL_URL)
    assert len(fake.requests) == 6
    assert len(clock.sleeps) == len(fake.requests) - 1
    assert all(gap == 2.0 for gap in clock.sleeps)


# ── Small things ─────────────────────────────────────────────────────────


def test_export_cookies_keeps_pipelinesuite_cookies_only(fake, clock):
    with _logged_in(fake, clock) as session:
        session._load_store()
        session._client.cookies.set("tracker", "x", domain="evil.example.com", path="/")
        exported = session._export_cookies()
    assert sorted(c["name"] for c in exported) == ["CFID", "CFTOKEN"]
    assert set(exported[0]) == {"name", "value", "domain", "path", "expires", "secure"}


# The live capture's Security Key, contact token and tracker token fragments,
# assembled so this file is not itself a hit for them.
_REAL_NEEDLES = tuple("".join(parts) for parts in (("gp!6", "Z6Df"), ("TJ9", "rC"), ("3734", "798"), ("_h1k", "_j2p")))


def test_no_fixture_carries_a_real_key_or_token():
    for name in fx.ALL_FILES:
        text = fx.load(name)
        for needle in _REAL_NEEDLES:
            assert needle not in text, (name, needle)
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "test_pipelinesuite_client.py"),
        os.path.join(here, "fixtures_pipelinesuite", "__init__.py"),
        os.path.join(here, "test_rfp_harvest.py"),
        os.path.join(here, "..", "app", "services", "pipelinesuite_client.py"),
        os.path.join(here, "..", "app", "services", "rfp_harvest.py"),
    ):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for needle in _REAL_NEEDLES:
            assert needle not in text, (path, needle)


def test_unlink_quietly_ignores_a_missing_file(tmp_path):
    psc._unlink_quietly(tmp_path / "missing")
    path = tmp_path / "there"
    path.write_bytes(b"x")
    psc._unlink_quietly(path)
    assert not path.exists()
