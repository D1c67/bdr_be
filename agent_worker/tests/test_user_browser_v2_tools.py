"""user_browser v2 tools, identity, downloads and transport (plan 7.4, 11.1, 15.2; WI-10).

Pure tests use fakes (no network). The browser tests drive a real headless
Chromium through WI-9's local fixture site (its HTTP proxy serves every test
host over TLS) with the real UserGuardV2 installed, so fill_credential, the
login exemption, click holds and site holds are exercised end to end. The
runner test goes through the real Slot with a mock BDR (httpx MockTransport)
for the personal and v2 routes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from bdr_agent import agents
from bdr_agent.agents.user_browser_v2 import client as client_mod
from bdr_agent.agents.user_browser_v2 import tools as tools_mod
from bdr_agent.agents.user_browser_v2.client import (
    MemorySpool,
    UserV2Client,
    UserV2StepLog,
    UserV2SyncClient,
)
from bdr_agent.agents.user_browser_v2.downloads import (
    DefenderScanner,
    DownloadManager,
    ScanResult,
    safe_filename,
    sniff_kind,
)
from bdr_agent.agents.user_browser_v2.identity import (
    CONNECTED_JS,
    FIELD_INFO_JS,
    HAS_MARK_JS,
    MARK_JS,
    MARK_SECRET_FIELD_JS,
    PAGE_STATE_JS,
    SECRET_FIELD_SELECTOR,
    SET_VALUE_JS,
    FillRefused,
    UserIdentityV2,
    cookie_allowed,
)
from bdr_agent.agents.user_browser_v2.runner import UserBrowserV2Runner
from bdr_agent.agents.user_browser_v2.tools import V2Executor, V2HelpDesk, V2Tools
from bdr_agent.browser.executor import secret_input_re
from bdr_agent.client import BdrClient, BdrRejected, BdrUnreachable, Lease, RetryPolicy
from bdr_agent.heartbeat import JobTracker
from bdr_agent.protocol import (
    SECRET_INPUT_RE,
    USER_SECRET_INPUT_RE,
    ClaimedJob,
    Cookie,
    HelpOpenResponse,
    HelpSecretUseResponse,
    SessionResponse,
    ToolCall,
    TypeConfig,
    UserCredentialFillResponse,
    UserCredentialResultResponse,
    UserDownloadResponse,
    UserHelpMessagesResponse,
    UserJobPolicy,
    UserPolicyCaps,
    UserPolicyCredential,
    UserSecretRef,
    UserWorkerHelpMessage,
    worker_step_kind_allowed,
)
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Finish, HandedOff, Requeue, Slot, SlotDeps, UserSlotParts
from bdr_agent.spool import Spool

from .agent_harness import needs_browser
from .test_user_browser_v2_guard import HTML, START, Browser, FakeHolds, FixtureSite, make_guard

JOB_ID = UUID("11111111-2222-4333-8444-555555555555")   # the guard fixtures' job
EPOCH = 3
CRED_ID = UUID("99999999-8888-4777-8666-555555555555")
USER_ID = UUID("22222222-3333-4444-8555-666666666666")
LOGIN = "https://login.example.com"
PASSWORD = "Pa55-word-Very-Secret"
USERNAME = "owner@example.com"
BASE = "https://bdr.test"
TOKEN = "bdrw_" + "a" * 43
FAST = RetryPolicy(max_attempts=3, base_s=0.001, cap_s=0.002)


async def _no_sleep(_s: float) -> None:
    await asyncio.sleep(0)


def policy(*, kind: str = "ad_hoc", sso: bool = False, credentials: bool = True, version: int = 1,
           caps: dict | None = None, extra_sites: list | None = None, start: str = START,
           login_origins: list | None = None) -> UserJobPolicy:
    sites = [{"site_key": "site", "label": "Example", "kind": kind, "scope": "domain", "grant_version": 1,
              "registrable_domains": ["example.com"]}] + (extra_sites or [])
    data: dict[str, Any] = {"policy_version": version, "job_id": str(JOB_ID), "start_origin": start, "sites": sites,
                            "caps": {"hold_timeout_seconds": 60, **(caps or {})}}
    if credentials:
        data["credentials"] = [{"credential_id": str(CRED_ID), "site_key": "site", "account_label": "Main",
                                "version": 1, "login_origins": login_origins or [LOGIN],
                                "cookie_domains": [".example.com"], "sso_required": sso}]
    return UserJobPolicy.model_validate(data)


class Steps:
    """StepLog stand-in that enforces the worker step kinds like the real one."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log(self, kind: str, summary: str, **fields: Any) -> str:
        if not worker_step_kind_allowed(kind):
            raise ValueError(kind)
        self.rows.append({"kind": kind, "summary": summary, **fields})
        return str(len(self.rows))

    def kinds(self) -> list[str]:
        return [r["kind"] for r in self.rows]


class Ctx:
    agent_type = "user_browser"

    def __init__(self, client: Any = None) -> None:
        self.client = client
        self.steps = Steps()
        self.secrets = LiveSecrets()
        self.lease = Lease(JOB_ID, EPOCH)
        self.job = NS(job_id=JOB_ID, principal_kind="user", principal_user_id=USER_ID, turn_count=0,
                      unconsumed_help=[], epoch=EPOCH)
        self.job_id = JOB_ID
        self.waiting: list[bool] = []
        self.help_updates: asyncio.Queue = asyncio.Queue()
        self.type_config = NS(max_steps=20)

    def set_waiting_help(self, waiting: bool) -> None:
        self.waiting.append(waiting)


# Transport: the closed endpoint map ---------------------------------------------------


class Recorder:
    def __init__(self, answer: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer or (lambda req: httpx.Response(200, json={}))

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return self.answer(req)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def v2_client(answer: Any = None) -> tuple[UserV2Client, Recorder]:
    rec = Recorder(answer)
    base = BdrClient(BASE, token=TOKEN, boot_id=uuid4(), version="1.1.0", transport=httpx.MockTransport(rec),
                     retry=FAST, sleep=_no_sleep, in_job_retry=FAST)
    return UserV2Client(base), rec


JOB = f"/agent-worker/v1/jobs/{JOB_ID}"
LEASE = Lease(JOB_ID, EPOCH)


async def test_shared_routes_go_to_the_personal_twins():
    c, rec = v2_client(lambda req: httpx.Response(204))
    await c.append_steps(LEASE, [{"idem_key": "k1", "kind": "note", "actor": "worker", "summary": "x",
                                  "at": "2026-11-02T00:00:00Z"}])
    await c.park(LEASE)
    await c.put_checkpoint(LEASE, 1, {"last_url": "https://site.example.com/"})
    from bdr_agent.protocol import FinishRequest, RequeueRequest
    await c.finish(LEASE, FinishRequest(status="succeeded", outcome="nothing_to_do", summary="ok"))
    await c.requeue(LEASE, RequeueRequest(reason="interrupted"))
    assert rec.paths() == [f"{JOB}/user/steps", f"{JOB}/user/park", f"{JOB}/user/checkpoint",
                           f"{JOB}/user/finish", f"{JOB}/user/requeue"]
    first = rec.requests[0]
    assert first.headers["X-Agent-Lease"] == f"{JOB_ID}:{EPOCH}"
    assert first.headers["Authorization"] == f"Bearer {TOKEN}" and first.headers.get("X-Agent-Boot")


@pytest.mark.parametrize("endpoint", ["claim", "hello", "contributions", "rfi_capture", "identity_session",
                                      "login_begin", "login_result", "help_open", "heartbeat", "live_frames"])
async def test_org_and_v1_routes_are_refused_without_a_request(endpoint):
    c, rec = v2_client()
    with pytest.raises(BdrRejected) as err:
        await c.call(endpoint, lease=LEASE)
    assert err.value.code == "idor" and rec.requests == []


async def test_identity_shortcuts_and_v1_login_are_refused():
    c, rec = v2_client()
    for call in (c.identity_session(LEASE, "procore", None), c.login_begin(LEASE, "procore", None),
                 c.login_result(LEASE, "procore", None), c.call_v2("live_input", lease=LEASE)):
        with pytest.raises(BdrRejected):
            await call
    with pytest.raises(BdrRejected):
        await c.help_secret_use(LEASE, uuid4(), uuid4())       # no login attempt: refused locally
    assert rec.requests == []


async def test_v2_calls_bind_their_routes_bodies_and_queries():
    help_id = uuid4()
    pol = policy()

    def answer(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/user/policy"):
            return httpx.Response(200, json=pol.model_dump(mode="json"))
        if path.endswith("/user/login/begin"):
            return httpx.Response(200, json={"granted": False, "reason": "spacing"})
        if path.endswith("/user/session"):
            return httpx.Response(200, json={"status": "no_jar"})
        if path.endswith("/user/help"):
            return httpx.Response(200, json={"help_id": str(help_id), "holder": True})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"status": "open", "messages": []})
        if path.endswith("/use"):
            return httpx.Response(200, json={"value": "123456"})
        return httpx.Response(404)

    c, rec = v2_client(answer)
    assert (await c.get_policy(LEASE)).job_id == JOB_ID
    from bdr_agent.protocol import UserCredentialFillRequest, UserHelpOpenV2Request, UserSessionPoll
    refused = await c.login_begin_v2(LEASE, UserCredentialFillRequest(credential_id=CRED_ID, origin=LOGIN,
                                                                       frame_origin=LOGIN))
    assert refused.reason == "spacing"
    when = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    assert (await c.session_poll(LEASE, UserSessionPoll(credential_id=CRED_ID, newer_than=when))).status == "no_jar"
    opened = await c.help_open_v2(LEASE, UserHelpOpenV2Request(reason_code="needs_credential",
                                                                message="Need a login", credential_id=CRED_ID))
    assert opened.help_id == help_id
    assert (await c.help_messages(LEASE, help_id)).status == "open"
    attempt = uuid4()
    used = await c.help_secret_use(LEASE, help_id, uuid4(), login_attempt_id=attempt)
    assert used.value.get_secret_value() == "123456"
    reqs = rec.requests
    assert [r.method for r in reqs] == ["GET", "POST", "GET", "POST", "GET", "POST"]
    assert reqs[1].url.path == f"{JOB}/user/login/begin"
    assert json.loads(reqs[1].content) == {"credential_id": str(CRED_ID), "origin": LOGIN, "frame_origin": LOGIN}
    assert reqs[2].url.path == f"{JOB}/user/session"
    assert reqs[2].url.params["credential_id"] == str(CRED_ID)
    assert datetime.fromisoformat(reqs[2].url.params["newer_than"]) == when
    assert json.loads(reqs[3].content)["reason_code"] == "needs_credential"
    assert json.loads(reqs[3].content)["credential_id"] == str(CRED_ID)
    assert json.loads(reqs[5].content) == {"login_attempt_id": str(attempt)}
    assert all(r.headers["X-Agent-Lease"] == f"{JOB_ID}:{EPOCH}" for r in reqs)
    assert all("Idempotency-Key" in r.headers for r in reqs if r.method == "POST")


async def test_policy_for_another_job_is_refused():
    other = policy().model_dump(mode="json") | {"job_id": str(uuid4())}
    c, _rec = v2_client(lambda req: httpx.Response(200, json=other))
    with pytest.raises(BdrRejected):
        await c.get_policy(LEASE)


async def test_hold_sender_reaches_only_this_jobs_hold_routes():
    c, rec = v2_client(lambda req: httpx.Response(200, json={"ok": True}))
    send = c.sender(LEASE)
    hold = uuid4()
    assert await send("POST", f"/jobs/{JOB_ID}/user/holds", {"a": 1}, None) == {"ok": True}
    assert await send("GET", f"/jobs/{JOB_ID}/user/holds/{hold}/release", None, None) == {"ok": True}
    assert await send("POST", f"/jobs/{JOB_ID}/user/site-hold", {"b": 2}, None) == {"ok": True}
    for method, path in (("POST", f"/jobs/{uuid4()}/user/holds"), ("GET", f"/jobs/{JOB_ID}/user/policy"),
                         ("GET", f"/jobs/{JOB_ID}/user/holds"), ("POST", f"/jobs/{JOB_ID}/user/finish"),
                         ("POST", f"/jobs/{JOB_ID}/user/holds/../finish")):
        with pytest.raises(BdrRejected):
            await send(method, path, {}, None)
    assert rec.paths() == [f"{JOB}/user/holds", f"{JOB}/user/holds/{hold}/release", f"{JOB}/user/site-hold"]
    keys = [r.headers.get("Idempotency-Key") for r in rec.requests if r.method == "POST"]
    assert len(set(keys)) == 2


async def test_scope_fence_refetches_the_policy_and_retries_once():
    calls = {"turn": 0}

    def answer(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/user/policy"):
            return httpx.Response(409, headers={"X-Error-Code": "scope_changed"}, json={"detail": "scope_changed"})
        calls["turn"] += 1
        if calls["turn"] == 1:
            return httpx.Response(409, headers={"X-Error-Code": "scope_changed"}, json={"detail": "scope_changed"})
        return httpx.Response(204)

    c, rec = v2_client(answer)
    fenced: list[int] = []

    async def on_fence() -> None:
        fenced.append(1)
        with pytest.raises(BdrRejected):
            await c.get_policy(LEASE)      # a fence inside the refresh does not recurse

    c.on_scope_fence = on_fence
    await c.park(LEASE)
    assert fenced == [1] and calls["turn"] == 2


async def test_download_upload_is_multipart_with_the_scanned_metadata(tmp_path):
    from bdr_agent.protocol import UserDownloadMeta
    data = b"%PDF-1.7\nhello"
    f = tmp_path / "x.pdf"
    f.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    answer = {"download_id": str(uuid4()), "status": "saved_to_project", "file_id": str(uuid4())}
    c, rec = v2_client(lambda req: httpx.Response(200, json=answer))
    meta = UserDownloadMeta(sha256=sha, filename="x.pdf", size_bytes=len(data), sniffed_kind="pdf",
                            source_origin="https://site.example.com", scan="clean")
    resp = await c.upload_download(LEASE, meta, f)
    assert resp.status == "saved_to_project"
    req = rec.requests[0]
    assert req.url.path == f"{JOB}/user/downloads"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert data in body and sha.encode() in body and b'name="sniffed_kind"' in body
    assert req.headers["Idempotency-Key"] == f"download:{JOB_ID}:{sha}"


def test_memory_spool_never_writes_and_is_bounded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spool = MemorySpool()
    sp = spool.for_job(JOB_ID, EPOCH)
    assert spool.for_job(JOB_ID, EPOCH) is sp and spool.on_disk() == []
    log = UserV2StepLog(sp, LiveSecrets())
    key = log.log("note", "hello")
    assert list(tmp_path.iterdir()) == []
    sp.ack([key])
    sp.mark_done()
    assert sp.remove_if_settled()
    small = client_mod.MemoryJobSpool(JOB_ID, EPOCH, max_bytes=200)
    with pytest.raises(RuntimeError):
        UserV2StepLog(small, LiveSecrets()).log("note", "x" * 400)


def test_heartbeat_flush_uses_the_personal_step_route():
    seen: list[str] = []
    base = NS(call=lambda endpoint, **kw: seen.append(endpoint))
    sync = UserV2SyncClient(base)  # type: ignore[arg-type]
    sync.call("steps", lease=LEASE)
    with pytest.raises(BdrRejected):
        sync.call("requeue", lease=LEASE)
    assert seen == ["user_steps"]


# Executor rule for the new type --------------------------------------------------------


def test_type_text_secret_rule_is_narrower_for_user_browser_only():
    for words in ("Zip code", "Cost code", "Postal code"):
        assert not secret_input_re("user_browser").search(words)
        assert secret_input_re("rfi_from_email").search(words)
        assert secret_input_re("user_browser_read").search(words)
    for words in ("Password", "One-time passcode", "Verification code", "MFA", "2FA token", "security code"):
        assert secret_input_re("user_browser").search(words)
    assert secret_input_re("rfi_from_email") is SECRET_INPUT_RE
    assert secret_input_re("user_browser") is USER_SECRET_INPUT_RE


def test_registry_and_user_pool_wiring(tmp_path):
    runner = agents.REGISTRY["user_browser"]
    assert isinstance(runner, UserBrowserV2Runner) and runner.needs_browser
    assert "user_browser_read" not in agents.REGISTRY        # v1 retired in 1.1.0 (WI-11)
    parts = runner.user_slot_parts(BdrClient(BASE, token=TOKEN))
    assert isinstance(parts, UserSlotParts) and isinstance(parts.client, UserV2Client)
    assert isinstance(parts.spool, MemorySpool) and parts.step_log_class is UserV2StepLog
    ceiling = agents.ceiling_for("user_browser")
    assert ceiling.nav_hosts == () and ceiling.login_hosts == ()
    from bdr_agent.local_config import Paths
    user = Paths(tmp_path / "BDRAgentUser")
    org = Paths(tmp_path / "BDRAgent")
    assert agents.is_user_pool_home(user.home) and not agents.is_user_pool_home(org.home)
    assert agents.browser_provider(user).user_pool is not None
    assert agents.browser_provider(org).user_pool is None


# Identity with fakes: every refusal types nothing ------------------------------------------


class FakeFrame:
    def __init__(self, page: "FakePage", url: str | None = None, parent: "FakeFrame | None" = None):
        self.page = page
        self._url = url
        self.parent_frame = parent

    @property
    def url(self) -> str:
        return self.page.url if self._url is None else self._url

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        return self.page.eval(js, arg)

    async def evaluate_handle(self, js: str) -> Any:
        return NS(as_element=lambda: self.page.code_handle)


class FakeKeyboard:
    def __init__(self, page: "FakePage"):
        self.page = page

    async def type(self, text: str, delay: int = 0) -> None:
        self.page.typed.append(text)
        if self.page.on_type is not None:
            self.page.on_type(self.page)


class FakePage:
    def __init__(self, url: str, state: dict | None = None):
        self.url = url
        self.main_frame = FakeFrame(self)
        self.keyboard = FakeKeyboard(self)
        self.marks: set[str] = set()
        self.state = state if state is not None else {"password": True}
        self.typed: list[str] = []
        self.on_type: Any = None
        self.code_handle: Any = None
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def eval(self, js: str, arg: Any) -> Any:
        if js == MARK_JS:
            self.marks.add(arg)
            return True
        if js == HAS_MARK_JS:
            return arg in self.marks
        if js == PAGE_STATE_JS:
            return dict(self.state)
        raise AssertionError("unexpected script")

    def navigate(self, url: str, **state: Any) -> None:
        self.url = url
        self.marks.clear()
        self.state = state

    async def wait_for_load_state(self, *a: Any, **k: Any) -> None:
        return None


class FakeHandle:
    def __init__(self, frame: FakeFrame, *, tag: str = "input", type: str = "text", on_press: Any = None):
        self.frame = frame
        self.info = {"tag": tag, "type": type, "disabled": False, "readonly": False}
        self.focused = False
        self.connected = True
        self.value = None
        self.on_press = on_press

    async def evaluate(self, js: str, *args: Any) -> Any:
        if js == FIELD_INFO_JS:
            return self.info
        if js == SET_VALUE_JS:
            page = self.frame.page
            if page.on_type is not None:
                page.on_type(page)
            if not self.connected:
                return False
            self.value = args[0]
            page.typed.append(args[0])
            return True
        if js == MARK_SECRET_FIELD_JS:
            self.secret_mark = True
            return True
        if js == CONNECTED_JS:
            return self.connected
        raise AssertionError("unexpected script")

    async def owner_frame(self) -> FakeFrame:
        return self.frame

    async def fill(self, value: str) -> None:
        self.value = value

    async def focus(self) -> None:
        self.focused = True

    async def press(self, key: str) -> None:
        if self.on_press is not None:
            self.on_press(key)


class FakeLocator:
    def __init__(self, handle: FakeHandle):
        self.handle = handle

    async def element_handle(self, timeout: int = 0) -> FakeHandle:
        return self.handle


class FakeContext:
    def __init__(self, cookies: list[dict] | None = None):
        self.jar = list(cookies or [])
        self.added: list[dict] = []

    async def cookies(self) -> list[dict]:
        return list(self.jar)

    async def add_cookies(self, cookies: list[dict]) -> None:
        self.added += cookies


class FakeEx:
    def __init__(self, page: FakePage, handles: dict[str, FakeHandle], context: FakeContext | None = None):
        self._page = page
        self.handles = handles
        self.jb = NS(context=context or FakeContext(), page=page)
        self.snapshot = None
        self.marked: list[str] = []

    async def page(self) -> FakePage:
        return self._page

    async def _locator(self, ref: str) -> FakeLocator:
        return FakeLocator(self.handles[ref])

    def mark_secret_field(self, selector: str) -> None:
        self.marked.append(selector)


class FakeGuard:
    def __init__(self, pol: UserJobPolicy):
        self.policy = pol
        self.opened: list[dict] = []
        self.closed: list[str] = []
        self._open = False

    async def open_login_attempt(self, *, credential_id: Any, page: Any, element: Any, markers: Any = ()) -> None:
        self.opened.append({"credential_id": credential_id, "markers": tuple(markers)})
        self._open = True

    def close_login_attempt(self, reason: str = "closed") -> None:
        self.closed.append(reason)
        self._open = False

    @property
    def login_open(self) -> bool:
        return self._open

    def in_scope(self, url: str) -> bool:
        return True

    def prepare_navigation(self, *a: Any, **k: Any) -> bool:
        return True

    async def settle(self) -> None:
        return None


class IdClient:
    def __init__(self, *, grant: bool = True, reason: str | None = None, sessions: list | None = None):
        self.grant = grant
        self.reason = reason
        self.sessions = list(sessions or [])
        self.begins: list[Any] = []
        self.results: list[Any] = []
        self.polls: list[Any] = []
        self.helps: list[Any] = []
        self.secret_uses: list[Any] = []
        self.help_messages_resp: Any = None

    async def login_begin_v2(self, lease: Lease, req: Any) -> UserCredentialFillResponse:
        self.begins.append(req)
        if self.grant:
            return UserCredentialFillResponse(granted=True, attempt_id=uuid4(), username=USERNAME, password=PASSWORD)
        return UserCredentialFillResponse(granted=False, reason=self.reason or "spacing",
                                          retry_at=datetime(2026, 11, 2, 9, 0, tzinfo=UTC))

    async def login_result_v2(self, lease: Lease, req: Any) -> UserCredentialResultResponse:
        self.results.append(req)
        return UserCredentialResultResponse(attempt_open=req.outcome == "mfa",
                                            help_reason="mfa" if req.outcome == "mfa" else None)

    async def session_poll(self, lease: Lease, poll: Any) -> SessionResponse:
        self.polls.append(poll)
        return self.sessions.pop(0) if self.sessions else SessionResponse(status="no_jar")

    async def help_open_v2(self, lease: Lease, req: Any) -> HelpOpenResponse:
        self.helps.append(req)
        return HelpOpenResponse(help_id=uuid4(), holder=True)

    async def help_messages(self, lease: Lease, help_id: Any, after: Any = None) -> Any:
        return self.help_messages_resp

    async def help_secret_use(self, lease: Lease, help_id: Any, message_id: Any, *, login_attempt_id: Any) -> Any:
        self.secret_uses.append((help_id, message_id, login_attempt_id))
        return HelpSecretUseResponse(value="123456")

    async def park(self, lease: Lease) -> None:
        return None


def identity_for(url: str, *, pol: UserJobPolicy | None = None, client: IdClient | None = None,
                 field_type: str = "password", frame_url: str | None = None, parent_url: str | None = None,
                 cookies: list[dict] | None = None, state: dict | None = None):
    page = FakePage(url, state)
    frame = page.main_frame
    if frame_url is not None:
        parent = FakeFrame(page, parent_url, page.main_frame) if parent_url else page.main_frame
        frame = FakeFrame(page, frame_url, parent)
    handle = FakeHandle(frame, type=field_type)
    user = FakeHandle(page.main_frame, type="email")
    ctx = Ctx(client or IdClient())
    guard = FakeGuard(pol or policy())
    ex = FakeEx(page, {"ref_1": handle, "ref_2": user}, FakeContext(cookies))
    desk = V2HelpDesk(ctx, sleep=_no_sleep, poll_seconds=0)
    ident = UserIdentityV2(ctx, ex, desk, guard, sleep=_no_sleep, poll_seconds=1, pickup_window=3)
    return ident, page, handle, ctx, guard


@pytest.mark.parametrize("url,kw,reason", [
    ("http://login.example.com/login", {}, "not_https"),
    ("https://owner:pw@login.example.com/login", {}, "origin_not_allowed"),
    ("https://accounts.google.com/signin", {}, "identity_provider"),
    ("https://login.microsoftonline.com/common/login", {}, "identity_provider"),
    ("https://login.example.com.evil.org/login", {}, "origin_not_allowed"),
    ("https://procore.com.example.net/login", {}, "origin_not_allowed"),
    ("https://login.example.com:8443/login", {}, "origin_not_allowed"),
    ("https://site.example.com/login", {}, "origin_not_allowed"),
    ("https://login.example.com/login", {"frame_url": "https://other.example.net/f"}, "cross_origin_frame"),
    ("https://login.example.com/login", {"frame_url": LOGIN + "/inner", "parent_url": "https://evil.example.org/x"},
     "cross_origin_frame"),
    ("https://login.example.com/login", {"field_type": "text"}, "not_a_field"),
    ("https://login.example.com/login", {"pol": policy(sso=True)}, "sso_required"),
])
async def test_fill_credential_refusals_type_nothing(url, kw, reason):
    client = IdClient()
    ident, page, handle, ctx, guard = identity_for(url, client=client, **kw)
    with pytest.raises(FillRefused) as err:
        await ident.fill("ref_1", "password")
    assert err.value.extra["reason"] == reason
    assert client.begins == [] and page.typed == [] and guard.opened == []


async def test_username_goes_only_into_text_like_fields():
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login")
    with pytest.raises(FillRefused) as err:
        await ident.fill("ref_1", "username")           # ref_1 is the password input
    assert err.value.extra["reason"] == "not_a_field"


async def test_identity_provider_refused_even_when_the_policy_lists_it():
    rogue = UserPolicyCredential.model_construct(credential_id=CRED_ID, site_key="site", account_label="Main",
                                                 version=1, login_origins=["https://accounts.google.com"],
                                                 cookie_domains=[".google.com"], sso_required=False)
    pol = policy()
    pol = pol.model_copy(update={"credentials": [rogue]})
    client = IdClient()
    ident, page, *_ = identity_for("https://accounts.google.com/signin", pol=pol, client=client)
    with pytest.raises(FillRefused) as err:
        await ident.fill("ref_1", "password")
    assert err.value.extra["reason"] == "identity_provider" and client.begins == []


async def test_fill_types_after_the_grant_and_opens_the_login_exemption():
    client = IdClient()
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login", client=client)
    out = await ident.fill("ref_2", "username")
    assert out.content["filled"] == "username" and PASSWORD not in json.dumps(out.content)
    out = await ident.fill("ref_1", "password")
    assert len(client.begins) == 1                      # one attempt for both fields
    req = client.begins[0]
    assert (req.credential_id, req.origin, req.frame_origin) == (CRED_ID, LOGIN, LOGIN)
    assert page.typed == [USERNAME, PASSWORD]           # written into the element, never as keystrokes
    assert ctx.secrets.contains("x " + PASSWORD)        # the password trips the leak check
    assert not ctx.secrets.contains(USERNAME)           # the username is scrubbed only
    assert ctx.secrets.scrub_text(USERNAME) != USERNAME
    assert guard.opened[-1]["credential_id"] == CRED_ID and USERNAME in guard.opened[-1]["markers"]
    assert "login.fill" in ctx.steps.kinds() and PASSWORD not in json.dumps(ctx.steps.rows, default=str)


async def test_redirect_after_the_check_stops_typing_and_ends_the_attempt():
    client = IdClient()
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login", client=client)

    def redirect(p: FakePage) -> None:
        p.url = "https://evil.example.org/phish"

    page.on_type = redirect
    with pytest.raises(FillRefused) as err:
        await ident.fill("ref_1", "password")
    assert err.value.extra["reason"] == "page_changed"
    assert handle.value == ""                           # cleared after the redirect
    assert [r.outcome for r in client.results] == ["unexpected"] and client.results[0].step == "page_changed"
    assert ident.attempt is None and guard.opened == []


async def test_a_detached_field_gets_nothing():
    client = IdClient()
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login", client=client)
    handle.connected = False
    with pytest.raises(FillRefused):
        await ident.fill("ref_1", "password")
    assert page.typed == [] and handle.value == ""


def _cookie(name: str, domain: str, secure: bool = True) -> dict:
    return {"name": name, "value": f"v-{name}-123", "domain": domain, "path": "/", "expires": -1,
            "httpOnly": True, "secure": secure, "sameSite": "Lax"}


async def test_login_ok_reports_only_the_credentials_secure_cookies():
    cookies = [_cookie("sid", ".example.com"), _cookie("host", "login.example.com"),
               _cookie("plain", "app.example.com", secure=False), _cookie("track", ".tracker.net"),
               _cookie("look", "example.com.evil.org")]
    client = IdClient()
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login", client=client, cookies=cookies)
    await ident.fill("ref_2", "username")
    await ident.fill("ref_1", "password")
    assert await ident.observe() is None and client.results == []      # not submitted yet
    page.navigate("https://site.example.com/home", password=False)
    assert await ident.observe() is None
    result = client.results[-1]
    assert result.outcome == "ok" and sorted(c.name for c in result.cookies) == ["host", "sid"]
    assert ident.attempt is None and "result" in guard.closed
    assert any("Signed in" in n for n in ident.take_notices())
    assert not ctx.secrets.contains("v-sid-123") and ctx.secrets.scrub_text("v-sid-123") != "v-sid-123"


async def test_multi_step_form_waits_for_the_password_step():
    client = IdClient()
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login", client=client,
                                                   cookies=[_cookie("sid", ".example.com")])
    await ident.fill("ref_2", "username")
    page.navigate("https://login.example.com/password", password=True)      # step two of the form
    assert await ident.observe() is None and client.results == []
    await ident.fill("ref_1", "password")
    assert await ident.observe() is None and client.results == []
    page.navigate("https://login.example.com/welcome", password=False)
    await ident.observe()
    assert [r.outcome for r in client.results] == ["ok"]


@pytest.mark.parametrize("kind,state_url,state,outcome,error", [
    ("ad_hoc", "https://accounts.google.com/o/oauth2", {}, "sso", "sso_required"),
    ("ad_hoc", "https://login.example.com/login", {"captcha": True, "password": True}, "captcha", "site_challenge"),
    ("ad_hoc", "https://login.example.com/login", {"password": True}, "unexpected", "login_unclear"),
    ("curated", "https://login.example.com/login", {"password": True}, "unexpected", None),
])
async def test_login_outcomes(kind, state_url, state, outcome, error):
    client = IdClient()
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login", client=client,
                                                   pol=policy(kind=kind))
    await ident.fill("ref_1", "password")
    page.navigate(state_url, **state)
    terminal = await ident.observe()
    assert client.results[-1].outcome == outcome and client.results[-1].cookies is None
    if error is None:
        assert terminal is None and ident.take_notices()
    else:
        assert isinstance(terminal, Finish) and terminal.error == error and terminal.error_kind == "login_failed"


async def test_login_without_a_session_cookie_is_unclear():
    client = IdClient()
    ident, page, *_ = identity_for("https://login.example.com/login", client=client, cookies=[])
    await ident.fill("ref_1", "password")
    page.navigate("https://site.example.com/", password=False)
    terminal = await ident.observe()
    assert client.results[-1].outcome == "unexpected" and client.results[-1].step == "no_session_cookie"
    assert isinstance(terminal, Finish) and terminal.error == "login_unclear"


async def test_result_deadline_reports_timeout():
    client = IdClient()
    now = [1000.0]
    ident, page, *_ = identity_for("https://login.example.com/login", client=client)
    ident._clock = lambda: now[0]
    await ident.fill("ref_1", "password")
    now[0] += 10_000
    terminal = await ident.observe()
    assert client.results[-1].outcome == "timeout" and isinstance(terminal, Finish)


async def test_code_from_the_owner_is_typed_by_the_worker_never_the_model():
    client = IdClient()
    msg = UserWorkerHelpMessage(id=uuid4(), author_kind="helper", action="message",
                                secret_ref=UserSecretRef(message_id=uuid4(), consumed=False),
                                created_at=datetime.now(UTC))
    client.help_messages_resp = UserHelpMessagesResponse(status="open", messages=[msg])
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login", client=client,
                                                   cookies=[_cookie("sid", ".example.com")])
    await ident.fill("ref_1", "password")
    code_field = FakeHandle(page.main_frame, type="text",
                            on_press=lambda key: page.navigate("https://site.example.com/home", password=False))
    page.code_handle = code_field
    page.navigate("https://login.example.com/mfa", code=True, password=False)
    terminal = await ident.observe()
    assert terminal is None
    assert [r.outcome for r in client.results] == ["mfa", "ok"]
    assert client.results[0].attempt_id == client.results[1].attempt_id
    help_req = client.helps[0]
    assert help_req.reason_code == "mfa" and help_req.login_attempt_id == client.results[0].attempt_id
    assert client.secret_uses[0][2] == client.results[0].attempt_id
    assert page.typed[-1] == "123456" and ctx.secrets.contains("code 123456")
    assert guard.opened[-1]["markers"] == ("123456",)


async def test_spacing_picks_up_the_session_another_task_saved():
    good = Cookie(name="sid", value="shared-sess-1", domain=".example.com")
    other = Cookie(name="x", value="other-1", domain=".tracker.net")
    insecure = Cookie(name="y", value="plain-1", domain="app.example.com", secure=False)
    client = IdClient(grant=False, reason="login_busy",
                      sessions=[SessionResponse(status="no_jar"),
                                SessionResponse(status="ok", cookies=[good, other, insecure],
                                                logged_in_at=datetime.now(UTC))])
    ident, page, handle, ctx, guard = identity_for("https://login.example.com/login", client=client)
    page.reload = _async_none
    out = await ident.fill("ref_1", "password")
    assert out.content["signed_in"] is True and out.terminal is None
    assert [c["name"] for c in ident.ex.jb.context.added] == ["sid"]
    assert all(p.newer_than == ident.started_at for p in client.polls)
    assert page.typed == [] and True in ctx.waiting and ctx.waiting[-1] is False


async def test_spacing_without_a_session_falls_back_to_the_normal_spacing():
    client = IdClient(grant=False, reason="spacing")
    ident, page, *_ = identity_for("https://login.example.com/login", client=client)
    out = await ident.fill("ref_1", "password")
    assert isinstance(out.terminal, Requeue) and out.terminal.reason == "login_spacing"
    assert len(client.polls) >= 2 and page.typed == []


@pytest.mark.parametrize("reason", ["locked", "daily_cap", "sso_required", "not_configured", "help_open"])
async def test_other_refusals_tell_the_model_what_to_do(reason):
    client = IdClient(grant=False, reason=reason)
    ident, page, *_ = identity_for("https://login.example.com/login", client=client)
    out = await ident.fill("ref_1", "password")
    assert not out.ok and out.content["reason"] == reason and out.terminal is None and page.typed == []


async def test_saved_sessions_are_loaded_at_start_and_rejected_jar_is_named_later():
    at = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    client = IdClient(sessions=[SessionResponse(status="ok", logged_in_at=at,
                                                cookies=[Cookie(name="sid", value="old-1", domain=".example.com")])])
    ident, page, *_ = identity_for("https://login.example.com/login", client=client)
    assert await ident.load_saved_sessions() == 1
    await ident.fill("ref_1", "password")
    assert client.begins[0].rejected_jar_logged_in_at == at


async def test_abandon_reports_an_unfinished_attempt():
    client = IdClient()
    ident, page, *_ = identity_for("https://login.example.com/login", client=client)
    await ident.fill("ref_1", "password")
    await ident.abandon()
    assert client.results[-1].outcome == "unexpected" and client.results[-1].step == "job_ended"


def test_cookie_scope():
    assert cookie_allowed(".example.com", [".example.com"]) and cookie_allowed("a.example.com", [".example.com"])
    assert cookie_allowed("example.com", [".example.com"])
    assert not cookie_allowed("example.com.evil.org", [".example.com"])
    assert not cookie_allowed("badexample.com", [".example.com"])
    assert not cookie_allowed("a.example.com", ["example.com"])


async def _async_none(*a: Any, **k: Any) -> None:
    return None


# Downloads ---------------------------------------------------------------------------------


def _zip(names: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, d in names.items():
            zf.writestr(n, d)
    return buf.getvalue()


def _cfb(entries: list[tuple[str, int]]) -> bytes:
    """A minimal Compound File Binary (legacy Office) with these directory
    entries (name, type: 1 storage, 2 stream, 5 root), 512-byte sectors."""
    import struct

    entries = [("Root Entry", 5), *entries]
    dir_sectors = (len(entries) + 3) // 4
    fat = [0xFFFFFFFD] + [i + 1 for i in range(1, dir_sectors)] + [0xFFFFFFFE]
    fat += [0xFFFFFFFF] * (128 - len(fat))
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIII", header, 44, 1, 1, 0, 4096)
    struct.pack_into("<IIII", header, 60, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    struct.pack_into("<109I", header, 76, 0, *([0xFFFFFFFF] * 108))
    directory = bytearray(dir_sectors * 512)
    for i, (name, kind) in enumerate(entries):
        raw = name.encode("utf-16-le") + b"\0\0"
        off = i * 128
        directory[off:off + len(raw)] = raw
        struct.pack_into("<HB", directory, off + 64, len(raw), kind)
    return bytes(header) + struct.pack("<128I", *fat) + bytes(directory)


@pytest.mark.parametrize("name,data,kind", [
    ("a.pdf", b"%PDF-1.7\n...", "pdf"),
    ("a.exe", b"%PDF-1.4 renamed", "pdf"),
    ("a.png", b"\x89PNG\r\n\x1a\nxxxx", "png"),
    ("a.jpg", b"\xff\xd8\xff\xe0xxxx", "jpeg"),
    ("a.tif", b"II*\x00xxxx", "tiff"),
    ("a.gif", b"GIF89axxxx", "gif"),
    ("a.doc", _cfb([("WordDocument", 2), ("1Table", 2), ("SummaryInformation", 2)]), "ole"),
    ("a.xls", _cfb([("Workbook", 2), ("A", 2), ("B", 2), ("C", 2), ("D", 2), ("E", 2)]), "ole"),
    ("a.rtf", b"{\\rtf1\\ansi hello}", "rtf"),
    ("a.dwg", b"AC1032\x00\x00xxxx", "dwg"),
    ("a.dxf", b"  0\nSECTION\n  2\nHEADER\n", "dxf"),
    ("a.csv", b"a,b\n1,2\n", "csv"),
    ("a.txt", "caf\u00e9 notes\n".encode(), "text"),
    ("a.zip", _zip({"x.txt": b"hi"}), "zip"),
    ("a.docx", _zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w/>"}), "ooxml"),
    ("a.bin", _zip({"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<w/>"}), "ooxml"),
])
def test_sniff_allowlist(tmp_path, name, data, kind):
    p = tmp_path / "f"
    p.write_bytes(data)
    assert sniff_kind(p, name)[0] == kind


@pytest.mark.parametrize("name,data,reason", [
    ("setup.pdf", b"MZ\x90\x00", "executable"),
    ("run.txt", b"\x7fELF\x02\x01", "executable"),
    ("x.txt", b"#!/bin/sh\nrm -rf /\n", "executable"),
    ("x.ps1", b"Remove-Item C:\\ -Recurse", "type_not_allowed"),
    ("x.html", b"<html><script>alert(1)</script>", "type_not_allowed"),
    ("x.lnk", b"L\x00\x00\x00\x01\x14\x02\x00", "type_not_allowed"),
    ("x.txt", b"bin\x00ary", "not_text"),
    ("x.xlsm", _zip({"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<w/>", "xl/vbaProject.bin": b"v"}),
     "office_macros"),
    ("x.zip", b"PK\x03\x04broken", "bad_zip"),
    # Review WI-10: legacy Office with a VBA project keeps its .xls/.doc name.
    ("invoice.xls", _cfb([("Workbook", 2), ("_VBA_PROJECT_CUR", 1), ("VBA", 1), ("dir", 2)]), "office_macros"),
    ("letter.doc", _cfb([("WordDocument", 2), ("Macros", 1), ("VBA", 1)]), "office_macros"),
    ("deck.ppt", _cfb([("PowerPoint Document", 2), ("_VBA_PROJECT", 2)]), "office_macros"),
    ("a.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1xxxx", "bad_ole"),
    # A macro project renamed through its relationships is still declared.
    ("x.docx", _zip({"[Content_Types].xml": b'<Override ContentType="application/vnd.ms-office.vbaProject"/>',
                     "word/document.xml": b"<w/>", "word/notvba.dat": b"v"}), "office_macros"),
    ("x.docm", _zip({"[Content_Types].xml": b'<Default ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>',
                     "word/document.xml": b"<w/>"}), "office_macros"),
    ("x.csv", b"", "empty"),
])
def test_sniff_refuses_everything_else(tmp_path, name, data, reason):
    p = tmp_path / "f"
    p.write_bytes(data)
    assert sniff_kind(p, name) == (None, reason)


async def test_concurrent_downloads_share_the_task_byte_cap(tmp_path):
    """Review WI-10: the cap was checked before the scan and counted after it,
    so parallel downloads all passed the check."""
    m, q, uploads, _ = manager(tmp_path, caps={"download_task_bytes": 12})

    class SlowScanner(FakeScanner):
        async def scan(self, path: Path) -> ScanResult:
            await asyncio.sleep(0.05)
            return ScanResult("clean")

    m.scanner = SlowScanner()
    a = FakeDownload(q, "a.pdf", b"%PDF-1.7 aaa")
    b = FakeDownload(q, "b.pdf", b"%PDF-1.7 bbb")
    recs = await asyncio.gather(m.handle(a), m.handle(b))
    assert sum(len(d) for _, d in uploads) <= 12
    assert sorted(r.reason or "" for r in recs) == ["", "task_bytes_cap"]


async def test_a_rejected_download_releases_its_reserved_bytes(tmp_path):
    m, q, uploads, _ = manager(tmp_path, caps={"download_task_bytes": 12})
    bad = await m.handle(FakeDownload(q, "a.exe", b"MZ\x90\x00abcd"))
    assert bad.reason == "executable"
    good = await m.handle(FakeDownload(q, "b.pdf", b"%PDF-1.7 bbb"))
    assert good.status == "saved_to_project" and len(uploads) == 1


def test_safe_filenames():
    assert safe_filename("Bid Docs.pdf", "pdf") == "Bid Docs.pdf"
    assert safe_filename("invoice.pdf.exe", "pdf") == "invoice.pdf.exe.pdf"
    assert safe_filename("../../etc/x.png", "png") == "x.png"
    assert safe_filename("C:\\Users\\a\\plan.dwg", "dwg") == "plan.dwg"
    assert safe_filename('a<b>:"c|?*.csv', "csv") == "a_b___c___.csv"
    assert safe_filename("report", "ooxml", ".xlsx") == "report.xlsx"
    assert safe_filename("", "jpeg") == "download.jpg"
    assert len(safe_filename("x" * 400 + ".pdf", "pdf")) == 200


async def test_defender_scanner_fails_closed(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    exe = tmp_path / "MpCmdRun.exe"
    assert (await DefenderScanner(lambda: None).scan(f)).verdict == "unavailable"
    calls: list[list[str]] = []

    def run(code: int):
        def inner(cmd, **kw):
            calls.append(cmd)
            return NS(returncode=code)
        return inner

    assert (await DefenderScanner(lambda: exe, run=run(0)).scan(f)).verdict == "clean"
    assert calls[0] == [str(exe), "-Scan", "-ScanType", "3", "-File", str(f), "-DisableRemediation"]
    assert (await DefenderScanner(lambda: exe, run=run(2)).scan(f)).verdict == "threat"
    assert (await DefenderScanner(lambda: exe, run=run(5)).scan(f)).verdict == "error"

    import subprocess

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert (await DefenderScanner(lambda: exe, run=slow).scan(f)).verdict == "error"


class FakeDownload:
    def __init__(self, folder: Path, name: str, data: bytes, *, url: str = "https://site.example.com/f",
                 grow: bool = False, page_url: str = "https://site.example.com/"):
        self.folder = folder
        self.suggested_filename = name
        self.url = url
        self.data = data
        self.grow = grow
        self.page = NS(url=page_url)
        self.canceled = False
        self.deleted = False
        self.file = folder / ("dl-" + uuid4().hex)

    async def path(self) -> str:
        if self.grow:
            with open(self.file, "wb") as fh:
                while not self.canceled:
                    fh.write(b"\0" * 4096)
                    fh.flush()
                    await asyncio.sleep(0.005)
            raise RuntimeError("canceled")
        self.file.write_bytes(self.data)
        return str(self.file)

    async def failure(self) -> str | None:
        return "canceled" if self.canceled else None

    async def cancel(self) -> None:
        self.canceled = True

    async def delete(self) -> None:
        self.deleted = True
        self.file.unlink(missing_ok=True)


class FakeScanner:
    def __init__(self, verdict: str = "clean"):
        self.verdict = verdict
        self.scanned: list[Path] = []

    async def scan(self, path: Path) -> ScanResult:
        self.scanned.append(path)
        return ScanResult(self.verdict)


def manager(tmp_path: Path, *, allowed: bool = True, verdict: str = "clean", caps: dict | None = None,
            upload_error: Exception | None = None):
    q = tmp_path / "quarantine"
    q.mkdir(parents=True, exist_ok=True)
    uploads: list[tuple[Any, bytes]] = []

    async def upload(meta: Any, path: Path) -> UserDownloadResponse:
        if upload_error is not None:
            raise upload_error
        uploads.append((meta, path.read_bytes()))
        return UserDownloadResponse(download_id=uuid4(), status="saved_to_project", file_id=uuid4())

    steps = Steps()
    m = DownloadManager(quarantine_dir=q, upload=upload, caps=lambda: UserPolicyCaps(**(caps or {})),
                        allowed=lambda: allowed, scanner=FakeScanner(verdict), log_step=steps.log,
                        monitor_interval=0.01)
    return m, q, uploads, steps


async def test_clean_download_is_uploaded_then_deleted(tmp_path):
    m, q, uploads, steps = manager(tmp_path)
    data = b"%PDF-1.7\nbid"
    d = FakeDownload(q, "Bid.pdf", data)
    rec = await m.handle(d)
    assert rec.status == "saved_to_project" and rec.kind == "pdf"
    meta, sent = uploads[0]
    assert sent == data and meta.sha256 == hashlib.sha256(data).hexdigest() and meta.scan == "clean"
    assert (meta.filename, meta.sniffed_kind, meta.size_bytes) == ("Bid.pdf", "pdf", len(data))
    assert meta.source_origin == "https://site.example.com"
    assert list(q.iterdir()) == [] and d.deleted
    assert "saved to the task's BDR project" in m.take_notices()[0]
    assert "browser.download" in steps.kinds()


@pytest.mark.parametrize("setup,data,name,reason", [
    ({"allowed": False}, b"%PDF-1.7", "a.pdf", "downloads_unavailable"),
    ({}, b"MZ\x90\x00", "a.pdf", "executable"),
    ({}, b"<html>", "a.html", "type_not_allowed"),
    ({"verdict": "threat"}, b"%PDF-1.7", "a.pdf", "scan_threat"),
    ({"verdict": "unavailable"}, b"%PDF-1.7", "a.pdf", "scan_unavailable"),
    ({"verdict": "error"}, b"%PDF-1.7", "a.pdf", "scan_unavailable"),
    ({"caps": {"download_file_bytes": 4}}, b"%PDF-1.7", "a.pdf", "file_too_large"),
    ({"upload_error": BdrUnreachable("user_downloads", "HTTP 503")}, b"%PDF-1.7", "a.pdf", "upload_failed"),
])
async def test_refused_downloads_are_deleted_and_never_uploaded(tmp_path, setup, data, name, reason):
    m, q, uploads, _steps = manager(tmp_path, **setup)
    rec = await m.handle(FakeDownload(q, name, data))
    assert (rec.status, rec.reason) == ("rejected", reason)
    assert uploads == [] and list(q.iterdir()) == []
    assert reason in m.take_notices()[0]


async def test_file_count_and_task_byte_caps(tmp_path):
    m, q, uploads, _ = manager(tmp_path, caps={"download_files": 1})
    assert (await m.handle(FakeDownload(q, "a.pdf", b"%PDF-1"))).status == "saved_to_project"
    second = FakeDownload(q, "b.pdf", b"%PDF-2")
    assert (await m.handle(second)).reason == "file_count_cap" and second.canceled
    m, q, uploads, _ = manager(tmp_path / "t", caps={"download_task_bytes": 10})
    assert (await m.handle(FakeDownload(q, "a.pdf", b"%PDF-1.7"))).status == "saved_to_project"
    assert (await m.handle(FakeDownload(q, "b.pdf", b"%PDF-1.7"))).reason == "task_bytes_cap"
    assert len(uploads) == 1


async def test_oversized_download_is_canceled_while_it_arrives(tmp_path):
    m, q, uploads, _ = manager(tmp_path, caps={"download_file_bytes": 64 * 1024})
    d = FakeDownload(q, "huge.zip", b"", grow=True)
    rec = await asyncio.wait_for(m.handle(d), 10)
    assert d.canceled and rec.reason == "file_too_large" and uploads == []
    assert list(q.iterdir()) == []


async def test_file_outside_the_quarantine_is_refused(tmp_path):
    m, q, uploads, _ = manager(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    d = FakeDownload(elsewhere, "a.pdf", b"%PDF-1.7")
    rec = await m.handle(d)
    assert rec.reason == "outside_quarantine" and uploads == []


async def test_blob_download_takes_the_page_origin_and_drain_waits(tmp_path):
    m, q, uploads, _ = manager(tmp_path)
    m.on_download(FakeDownload(q, "a.csv", b"a,b\n", url="blob:https://site.example.com/uuid"))
    assert m.in_flight == 1
    assert await m.drain(5)
    assert uploads[0][0].source_origin == "https://site.example.com"
    assert m.summary()[0]["status"] == "saved_to_project"


# Browser: the executor with the real guard ---------------------------------------------------


class LoginSite(FixtureSite):
    def respond(self, s):   # type: ignore[override]
        if s.host == "login.example.com" and s.path == "/session" and s.method == "POST":
            return 200, [("Content-Type", HTML),
                         ("Set-Cookie", "sid=sess-value-123; Path=/; Secure; HttpOnly; Domain=example.com")], \
                b"<p id=welcome>welcome</p>"
        if s.host == "login.example.com" and s.path == "/framed":
            return 200, [("Content-Type", HTML)], \
                b'<p>outer</p><iframe id=x src="https://other.example.net/inner"></iframe>'
        if s.host == "login.example.com" and s.path == "/widget":
            return 200, [("Content-Type", HTML)], \
                (b'<form id=lf method=post action="/session"><input id=user name=username>'
                 b'<input id=pw type=password name=password><button id=signin>Sign in</button></form>'
                 b'<iframe id=thief src="https://other.example.net/thief"></iframe>')
        if s.host == "other.example.net" and s.path == "/thief":
            return 200, [("Content-Type", HTML)], \
                b"<input id=steal><script>setInterval(() => document.getElementById('steal').focus(), 5)</script>"
        if s.host == "other.example.net" and s.path == "/inner":
            return 200, [("Content-Type", HTML)], b'<input id=pw type=password name=password>'
        if s.host == "site.example.com" and s.path == "/zip":
            return 200, [("Content-Type", HTML)], b'<form><label for=z>Zip code</label><input id=z name=zip></form>'
        return super().respond(s)


@pytest.fixture
def login_site():
    s = LoginSite()
    yield s
    s.stop()


class JB:
    """The parts of UserJobBrowser the executor uses, over a test context."""

    def __init__(self, context: Any, page: Any, guard: Any):
        self.context = context
        self.page = page
        self.guard = guard

    def pages(self) -> list[Any]:
        return list(self.context.pages)

    async def model_page(self) -> Any:
        if self.page is None or self.page.is_closed():
            self.page = [p for p in self.context.pages if not p.is_closed()][0]
        return self.page


class BrowserClient(IdClient):
    async def upload_artifact(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("screenshots are off in these tests")


def wire(b: Browser, pol: UserJobPolicy, client: Any = None) -> tuple[V2Executor, Ctx, UserIdentityV2]:
    ctx = Ctx(client or BrowserClient())
    jb = JB(b.context, b.page, b.guard)
    ex = V2Executor(ctx, jb, b.guard, start_url=START + "/")
    ex.screenshots = False
    desk = V2HelpDesk(ctx, sleep=_no_sleep, poll_seconds=0)
    ident = UserIdentityV2(ctx, ex, desk, b.guard)
    ex.identity = ident
    ex.extra_tools.update(V2Tools(ex, desk, ident, None).handlers())
    return ex, ctx, ident


async def run(ex: V2Executor, tool: str, **args: Any):
    out = await ex.run_tool(ToolCall(call_id=uuid4().hex, tool=tool, args=args), turn_no=1)
    return out, json.loads(out.content)


async def refs_by_id(ex: V2Executor) -> dict[str, str]:
    page = await ex.page()
    out = {}
    for ref in ex.snapshot.refs:
        el_id = await page.locator(ex.snapshot.selector(ref)).get_attribute("id")
        if el_id:
            out[el_id] = ref
    return out


@needs_browser
async def test_sign_in_end_to_end_with_the_real_guard(login_site):
    fake = FakeHolds()
    pol = policy()
    guard = make_guard(pol, fake)
    client = BrowserClient()
    async with Browser(login_site, guard) as b:
        await b.context.add_cookies([{"name": "track", "value": "t-1", "domain": "other.example.net", "path": "/",
                                      "secure": True}])
        ex, ctx, ident = wire(b, pol, client)
        out, content = await run(ex, "navigate", url=LOGIN + "/login")
        assert out.ok, content
        out, content = await run(ex, "read_page")
        refs = await refs_by_id(ex)
        out, content = await run(ex, "fill_credential", ref=refs["user"], field="username")
        assert out.ok and content["filled"] == "username"
        out, content = await run(ex, "fill_credential", ref=refs["pw"], field="password")
        assert out.ok and PASSWORD not in out.content and USERNAME not in out.content
        assert await b.page.input_value("#user") == USERNAME
        assert await b.page.input_value("#pw") == PASSWORD
        assert guard.login_open
        # Outside the filled form the model may not click while signing in.
        out, content = await run(ex, "click", ref=refs["elsewhere"])
        assert not out.ok and "sign-in" in content["detail"]
        out, content = await run(ex, "click", ref=refs["signin"])
        assert out.ok, content
        assert any("Signed in to Example" in n for n in content.get("notices", []))
    assert len(client.begins) == 1
    result = client.results[-1]
    assert result.outcome == "ok" and [c.name for c in result.cookies] == ["sid"]
    assert len(login_site.hits("login.example.com", "/session", "POST")) == 1
    assert fake.final == []                                  # the login post was exempt, not held
    assert PASSWORD not in json.dumps(ctx.steps.rows, default=str)


@needs_browser
async def test_type_text_rules_on_a_real_page(login_site):
    pol = policy()
    guard = make_guard(pol, FakeHolds())
    async with Browser(login_site, guard) as b:
        ex, ctx, _ = wire(b, pol)
        await run(ex, "navigate", url=LOGIN + "/login")
        await run(ex, "read_page")
        refs = await refs_by_id(ex)
        out, content = await run(ex, "type_text", ref=refs["pw"], text="guess")
        assert not out.ok and content["error"] == "refused"
        typed = "a-typed-value-1234567"
        out, content = await run(ex, "type_text", ref=refs["user"], text=typed)
        assert out.ok and content["typed"] == len(typed)
        assert guard.typed.found_in("https://evil.example.org/?q=" + typed, b"")
        await run(ex, "navigate", url=START + "/zip")
        await run(ex, "read_page")
        refs = await refs_by_id(ex)
        out, content = await run(ex, "type_text", ref=refs["z"], text="89101")
        assert out.ok, content                                 # "Zip code" is typable for the user type


@needs_browser
async def test_click_level_hold_declined_never_clicks(login_site):
    fake = FakeHolds(decide=lambda r: "decline")
    pol = policy(credentials=False)
    guard = make_guard(pol, fake)
    async with Browser(login_site, guard) as b:
        ex, ctx, _ = wire(b, pol)
        await run(ex, "navigate", url=START + "/")
        await run(ex, "read_page")
        refs = await refs_by_id(ex)
        out, content = await run(ex, "click", ref=refs["accept"])
        assert not out.ok and content["error"] == "not_clicked"
        assert any("declined" in n.lower() or "owner" in n.lower() for n in content.get("notices", [content["detail"]]))
    assert len(fake.final) == 1 and fake.final[0].trigger == "click"
    assert login_site.hits("site.example.com", "/bids/7/accept") == []


@needs_browser
async def test_navigate_waits_for_a_site_hold_past_the_goto_timeout(login_site, monkeypatch):
    class SlowHolds(FakeHolds):
        async def poll_release(self, hold_id):
            await asyncio.sleep(1.5)
            return await super().poll_release(hold_id)

    fake = SlowHolds(site_decide=lambda r: "full")
    wider = policy(version=2, extra_sites=[{"site_key": "other", "label": "Other", "kind": "ad_hoc",
                                            "scope": "origin", "grant_version": 1,
                                            "exact_origins": ["https://other.example.net"]}])

    async def refresh():
        return wider

    pol = policy()
    guard = make_guard(pol, fake, refresh_policy=refresh)
    monkeypatch.setattr(tools_mod, "NAV_TIMEOUT_MS", 600)
    async with Browser(login_site, guard) as b:
        ex, ctx, _ = wire(b, pol)
        await run(ex, "navigate", url=START + "/")
        out, content = await run(ex, "navigate", url="https://other.example.net/land")
        assert out.ok, content
        assert content["url"] == "https://other.example.net/land"
        assert True in ctx.waiting and ctx.waiting[-1] is False
    assert len(fake.sites) == 1 and guard.policy.policy_version == 2


@needs_browser
async def test_navigate_refuses_long_query_data_before_any_request(login_site):
    pol = policy()
    guard = make_guard(pol, FakeHolds())
    async with Browser(login_site, guard) as b:
        ex, ctx, _ = wire(b, pol)
        await run(ex, "navigate", url=START + "/")
        out, content = await run(ex, "navigate", url=START + "/search?q=" + "A" * 300)
        assert not out.ok and content["error"] == "navigation_refused"
    assert login_site.hits("site.example.com", "/search") == []


@needs_browser
async def test_fill_credential_refuses_a_cross_origin_iframe_on_a_real_page(login_site):
    pol = policy()
    guard = make_guard(pol, FakeHolds())
    client = BrowserClient()
    async with Browser(login_site, guard) as b:
        ex, ctx, ident = wire(b, pol, client)
        await run(ex, "navigate", url=LOGIN + "/framed")
        frame = b.page.frame_locator("#x")
        handle = await frame.locator("#pw").element_handle()
        with pytest.raises(FillRefused) as err:
            await ident.check_target(b.page, handle, "password")
        assert err.value.extra["reason"] == "cross_origin_frame"
    assert client.begins == []


@needs_browser
async def test_a_focus_stealing_cross_origin_frame_receives_no_credential(login_site):
    """Review WI-10: keystrokes went to whatever had focus, so a cross-origin
    widget that grabbed focus on a timer captured part of the password."""
    pol = policy()
    guard = make_guard(pol, FakeHolds())
    async with Browser(login_site, guard) as b:
        ex, ctx, ident = wire(b, pol, BrowserClient())
        out, content = await run(ex, "navigate", url=LOGIN + "/widget")
        assert out.ok, content
        thief = await (await b.page.wait_for_selector("#thief")).content_frame()
        await thief.wait_for_selector("#steal")
        await run(ex, "read_page")
        refs = await refs_by_id(ex)
        for _ in range(3):
            out, content = await run(ex, "fill_credential", ref=refs["user"], field="username")
            assert out.ok, content
            out, content = await run(ex, "fill_credential", ref=refs["pw"], field="password")
            assert out.ok, content
        stolen = await thief.evaluate("document.getElementById('steal').value")
        assert stolen == ""
        assert await b.page.input_value("#pw") == PASSWORD and await b.page.input_value("#user") == USERNAME


@needs_browser
async def test_filled_fields_stay_secret_across_later_snapshots(login_site):
    """Review WI-10: the mark was the snapshot's own ref selector, which the
    next read_page invalidated, so the username showed and type_text worked."""
    pol = policy()
    guard = make_guard(pol, FakeHolds())
    async with Browser(login_site, guard) as b:
        ex, ctx, ident = wire(b, pol, BrowserClient())
        await run(ex, "navigate", url=LOGIN + "/login")
        await run(ex, "read_page")
        refs = await refs_by_id(ex)
        out, content = await run(ex, "fill_credential", ref=refs["user"], field="username")
        assert out.ok, content
        for _ in range(2):
            out, content = await run(ex, "read_page")
            assert USERNAME not in out.content, out.content[:500]
        refs = await refs_by_id(ex)
        out, content = await run(ex, "type_text", ref=refs["user"], text="someone else")
        assert not out.ok and content["error"] == "refused", content
        # A page that strips the mark is still refused (the element itself is remembered).
        await b.page.evaluate("document.getElementById('user').removeAttribute('data-bdr-secret')")
        await run(ex, "read_page")
        refs = await refs_by_id(ex)
        out, content = await run(ex, "type_text", ref=refs["user"], text="someone else")
        assert not out.ok and content["error"] == "refused", content
        assert await b.page.input_value("#user") == USERNAME
        assert SECRET_FIELD_SELECTOR in ex.secret_fields


@needs_browser
async def test_tabs_are_listed_and_switchable(login_site):
    pol = policy(credentials=False)
    guard = make_guard(pol, FakeHolds())
    async with Browser(login_site, guard) as b:
        ex, ctx, _ = wire(b, pol)
        await run(ex, "navigate", url=START + "/")
        second = await b.context.new_page()
        await second.goto(START + "/landed", wait_until="domcontentloaded")
        out, content = await run(ex, "read_page")
        tabs = {t["tab_id"]: t for t in content["tabs"]}
        assert tabs["tab_0"]["active"] and tabs["tab_1"]["url"] == START + "/landed"
        out, content = await run(ex, "switch_tab", tab_id="tab_1")
        assert out.ok and ex.jb.page is second
        out, content = await run(ex, "switch_tab", tab_id="tab_9")
        assert not out.ok and content["error"] == "unknown_tab"


@needs_browser
async def test_no_screenshot_on_a_login_origin(login_site):
    pol = policy()
    guard = make_guard(pol, FakeHolds())
    async with Browser(login_site, guard) as b:
        ex, ctx, _ = wire(b, pol)
        ex.screenshots = True
        await b.page.goto(LOGIN + "/login", wait_until="domcontentloaded")
        assert await ex.screenshot() is None            # BrowserClient.upload_artifact would raise


# user_finish and user_request_help ----------------------------------------------------------


async def test_user_finish_keeps_only_in_scope_facts_and_maps_the_outcome():
    pol = policy()
    guard = NS(policy=pol, in_scope=lambda url: "example.com" in url, side_effects=True,
               settle=_async_none, holds_open=0, take_notices=lambda: [], wait_holds=_async_none)
    ex = NS(v2=guard, settle_guard=_async_none)
    downloads = NS(drain=_async_none, summary=lambda: [{"filename": "a.pdf", "status": "saved_to_project"}])
    ident = NS(abandon=_async_none)
    tools = V2Tools(ex, None, ident, downloads)  # type: ignore[arg-type]
    from bdr_agent.protocol import UserFinishArgs
    args = UserFinishArgs(outcome="completed", result="R" * 900, facts=[
        {"text": "Bid due Nov 5", "source_url": "https://site.example.com/bids/7?token=abc"},
        {"text": "planted", "source_url": "https://evil.example.org/x"}])
    ret = await tools.user_finish(args)
    assert ret.content == {"finished": True, "facts_kept": 1, "facts_dropped": 1}
    assert ret.ledger["facts"] == [{"text": "Bid due Nov 5", "source": "https://site.example.com/bids/7"}]
    assert ret.ledger["downloads"][0]["filename"] == "a.pdf" and ret.ledger["side_effects"] is True
    assert ret.terminal == Finish("succeeded", "R" * 500, outcome="nothing_to_do")
    declined = await tools.user_finish(UserFinishArgs(outcome="cannot_complete", result="no",
                                                      error_kind="declined"))
    assert declined.terminal.error_kind == "cannot_complete" and declined.terminal.error == "declined"
    other = await tools.user_finish(UserFinishArgs(outcome="cannot_complete", result="no", error_kind="not_found"))
    assert other.terminal.error_kind == "not_found"


async def test_user_request_help_sends_the_credential_and_waits_for_the_owner():
    client = IdClient()
    reply = UserWorkerHelpMessage(id=uuid4(), author_kind="helper", body="Saved it, try again",
                                  created_at=datetime.now(UTC))
    client.help_messages_resp = UserHelpMessagesResponse(status="answered", messages=[reply])
    consumed: list[Any] = []

    async def help_consume(lease, help_id, ids):
        consumed.append(ids)

    client.help_consume = help_consume  # type: ignore[attr-defined]
    ctx = Ctx(client)
    pol = policy()
    page = NS(url="https://site.example.com/x")
    guard = NS(policy=pol, site_for=lambda url: pol.sites[0])
    ex = NS(v2=guard, jb=NS(page=page), screenshot=lambda **k: _async_none())
    desk = V2HelpDesk(ctx, sleep=_no_sleep, poll_seconds=0)
    ident = UserIdentityV2(ctx, NS(), desk, NS(policy=pol))
    tools = V2Tools(ex, desk, ident, None)  # type: ignore[arg-type]
    from bdr_agent.protocol import UserRequestHelpArgs
    ret = await tools.user_request_help(UserRequestHelpArgs(reason_code="needs_credential",
                                                            message="The saved login was rejected"))
    assert client.helps[0].reason_code == "needs_credential" and client.helps[0].credential_id == CRED_ID
    assert ret.content["messages"][0]["text"] == "Saved it, try again"
    assert ret.helper_message_ids == [reply.id] and consumed == [[reply.id]]
    assert ctx.waiting == [True, False]
    client.help_messages_resp = UserHelpMessagesResponse(status="resolved", messages=[])
    ret = await tools.user_request_help(UserRequestHelpArgs(reason_code="needs_site", message="Need ACC"))
    assert client.helps[1].credential_id is None and ret.content["status"] == "resolved"


async def test_help_parks_when_the_hold_runs_out():
    client = IdClient()
    client.help_messages_resp = UserHelpMessagesResponse(status="open", messages=[])
    ctx = Ctx(client)
    desk = V2HelpDesk(ctx, sleep=_no_sleep, poll_seconds=0, default_hold_seconds=0)
    pol = policy(credentials=False)
    ex = NS(v2=NS(policy=pol, site_for=lambda url: None), jb=NS(page=NS(url="")),
            screenshot=lambda **k: _async_none())
    tools = V2Tools(ex, desk, UserIdentityV2(ctx, NS(), desk, NS(policy=pol)), None)  # type: ignore[arg-type]
    from bdr_agent.protocol import UserRequestHelpArgs
    ret = await tools.user_request_help(UserRequestHelpArgs(reason_code="ambiguous", message="Which project?"))
    assert isinstance(ret.terminal, HandedOff)
    with pytest.raises(Exception):
        await tools.user_request_help(UserRequestHelpArgs(reason_code="mfa_step_up", message="code please"))


# Runner through the real Slot -----------------------------------------------------------------


class MockBdr:
    """The personal and v2 routes of one job, answering a scripted model."""

    def __init__(self, pol: UserJobPolicy, actions: list[dict[str, Any]]):
        self.policy = pol
        self.actions = list(actions)
        self.paths: list[str] = []
        self.finish: dict | None = None
        self.steps: list[dict] = []
        self.turns: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix(f"/agent-worker/v1/jobs/{JOB_ID}")
        self.paths.append(path)
        if path == "/user/policy":
            return httpx.Response(200, json=self.policy.model_dump(mode="json"))
        if path == "/user/session":
            return httpx.Response(200, json={"status": "no_jar"})
        if path == "/user/steps":
            body = json.loads(req.content)
            self.steps += body["steps"]
            return httpx.Response(200, json={"appended": len(body["steps"]), "duplicates": 0, "late": 0})
        if path == "/user/turns":
            self.turns.append(json.loads(req.content))
            n = len(self.turns)
            action = self.actions.pop(0)
            return httpx.Response(200, json={"turn_no": n, "assistant_text": "Open the bids page",
                                             "action": {"call_id": f"c{n}", **action},
                                             "caps": {"steps_left": 10, "tokens_left": 1000, "cost_left_usd": 1}})
        if path == "/user/finish":
            self.finish = json.loads(req.content)
            return httpx.Response(204)
        if path in ("/user/checkpoint", "/user/park", "/user/requeue"):
            return httpx.Response(204)
        return httpx.Response(404)


class SlotProvider:
    def __init__(self, site: FixtureSite, tmp: Path):
        self.site = site
        self.tmp = tmp
        self.closed = False

    async def new_context(self, job: Any) -> Any:
        from playwright.async_api import async_playwright

        self.cm = async_playwright()
        self.pw = await self.cm.__aenter__()
        self.browser = await self.pw.chromium.launch(proxy={"server": self.site.proxy_url})
        context = await self.browser.new_context(ignore_https_errors=True, service_workers="block")
        q = self.tmp / "q"
        q.mkdir(exist_ok=True)
        return NS(context=context, page=None, guard=None, headless=True, downloads_dir=q, provider=None,
                  install_guard=lambda g: self._install(context, g), pages=lambda: list(context.pages),
                  model_page=lambda: self._model_page(context))

    async def _install(self, context: Any, guard: Any) -> None:
        await guard.install(context)

    async def _model_page(self, context: Any) -> Any:
        return context.pages[0] if context.pages else await context.new_page()

    async def close_context(self, jb: Any) -> None:
        self.closed = True
        await self.browser.close()
        await self.cm.__aexit__(None, None, None)


@needs_browser
async def test_runner_end_to_end_through_the_slot(login_site, tmp_path):
    pol = policy(credentials=False)
    bdr = MockBdr(pol, [
        {"tool": "navigate", "args": {"url": START + "/"}},
        {"tool": "user_finish", "args": {"outcome": "completed", "result": "Found the bid page", "facts": [
            {"text": "There is an Accept bid link", "source_url": START + "/"},
            {"text": "elsewhere", "source_url": "https://evil.example.org/"}]}},
    ])
    base = BdrClient(BASE, token=TOKEN, boot_id=uuid4(), version="1.1.0", transport=httpx.MockTransport(bdr),
                     retry=FAST, sleep=_no_sleep, in_job_retry=FAST)
    runner = UserBrowserV2Runner(downloads_allowed=lambda: False, scanner=FakeScanner())
    provider = SlotProvider(login_site, tmp_path)
    deps = SlotDeps(client=base, tracker=JobTracker(), spool=Spool(tmp_path / "spool"), secrets=LiveSecrets(),
                    runners={"user_browser": runner}, browser=provider, tick=0.01, flush_interval=0.05)
    job = ClaimedJob(job_id=JOB_ID, agent_type="user_browser", principal_kind="user", principal_user_id=USER_ID,
                     epoch=EPOCH, lease_expires_at=datetime.now(UTC), attempts=1,
                     input={"admission_id": str(uuid4()), "goal": "Find the bid", "start_url": START + "/",
                            "authority": {"role": "Executive", "conversation_id": str(uuid4()),
                                          "max_cost_usd": 5, "admission_id": None}})
    job.input["authority"]["admission_id"] = job.input["admission_id"]
    tc = TypeConfig(enabled=True, max_concurrency=4, max_steps=10, max_active_seconds=120)
    out = await asyncio.wait_for(Slot("user_browser", 0, deps).run(job, tc), 60)
    assert out.terminal == "finish:succeeded", (out, bdr.paths)
    assert bdr.finish["outcome"] == "nothing_to_do" and bdr.finish["summary"] == "Found the bid page"
    assert provider.closed
    calls = [p for p in bdr.paths if p != "/user/steps"]
    assert calls[0] == "/user/policy" and calls[-1] == "/user/finish"
    assert all(p.startswith("/user/") for p in bdr.paths)
    finish_step = next(s for s in bdr.steps if s["kind"] == "tool.user_finish")
    assert finish_step["result"]["facts_dropped"] == 1
    assert finish_step["result"]["facts"] == [{"text": "There is an Accept bid link", "source": START + "/"}]
    assert list((tmp_path / "spool").iterdir()) == []            # personal steps never touched the disk
    assert len(login_site.hits("site.example.com", "/")) == 1
