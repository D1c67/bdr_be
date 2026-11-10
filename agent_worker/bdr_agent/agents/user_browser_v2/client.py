"""Transport for user_browser v2 jobs (plan 15.1, 15.3; WI-10). Signed worker code.

A personal job never reaches an organization job route. The slot gets this
module's parts through the runner's `user_slot_parts(base_client)`:

  UserV2Client   a closed endpoint map over the worker's authenticated
                 BdrClient (its transport is reused, never closed here):
                 - the shared job routes the slot, the loop and the executor
                   use (steps, checkpoint, artifacts, turns, finish, requeue,
                   park, help messages, consume, reply, secret use) go to their
                   `user_*` twins;
                 - the v2 routes of protocol.USER_V2_ENDPOINTS (policy, holds,
                   hold release, site hold, session poll, login begin and
                   result, help open, downloads) through `call_v2`;
                 - everything else (identities of the org agent, the v1 personal
                   identity routes, contributions, RFI capture, live view
                   before wave E) is refused locally with `idor`.
  MemorySpool    personal steps never touch the disk; a restart loses pending
                 steps, never spills them (bounded memory).
  UserV2StepLog  the heartbeat thread's stall traces also use the personal
                 step route.

`sender(lease)` is the lease-fenced JSON `send(method, path, json, query)`
that holds.EndpointHoldTransport needs; it only reaches the three hold routes
of this job. A fence code naming a scope change (`scope_changed`,
`scope_revoked`) calls `on_scope_fence` (the runner refetches the job policy)
and retries the call once.

Nothing here logs a request or response body, a token or a lease header.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel

from ...client import (
    BdrClient,
    BdrRejected,
    BdrUnreachable,
    Lease,
    RetryPolicy,
    SyncBdrClient,
    WorkerUnauthorized,
)
from ...protocol import (
    API_PREFIX,
    HEADER_AUTH,
    HEADER_BOOT,
    HEADER_IDEMPOTENCY,
    HEADER_LEASE,
    USER_HOLD_RELEASE_POLL_SECONDS,
    USER_V2_ENDPOINTS,
    ErrorCode,
    HelpOpenRequest,
    HelpOpenResponse,
    SessionResponse,
    UserCredentialFillRequest,
    UserCredentialFillResponse,
    UserCredentialResultRequest,
    UserCredentialResultResponse,
    UserDownloadMeta,
    UserDownloadResponse,
    UserHelpConsumeRequest,
    UserHelpOpenV2Request,
    UserHelpSecretUseRequest,
    UserJobPolicy,
    UserSessionPoll,
)
from ...spool import FLUSH_RETRY, JobSpool, Spool, StepLog

# Shared job routes a personal job may use, mapped to their private twins.
ENDPOINT_MAP: dict[str, str] = {name: "user_" + name for name in (
    "steps", "checkpoint", "artifacts", "turns", "finish", "requeue", "park",
    "help_messages", "help_consume", "help_reply", "help_secret_use",
)}
# The v2 routes this worker calls. live_* belong to wave E (live view) and are
# refused until that release.
V2_KEYS = ("user_policy", "user_holds", "user_hold_release", "user_site_hold", "user_session_poll",
           "user_login_begin_v2", "user_login_result_v2", "user_help_open_v2", "user_downloads")
HOLD_KEYS = ("user_holds", "user_hold_release", "user_site_hold")
SCOPE_FENCE_CODES = ("scope_changed", "scope_revoked")

V2_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HOLD_POLL_TIMEOUT = httpx.Timeout(float(USER_HOLD_RELEASE_POLL_SECONDS) + 20.0, connect=10.0)
LOGIN_TIMEOUT = httpx.Timeout(90.0, connect=10.0)
UPLOAD_TIMEOUT = httpx.Timeout(600.0, connect=15.0)
_TIMEOUTS = {"user_hold_release": HOLD_POLL_TIMEOUT, "user_login_begin_v2": LOGIN_TIMEOUT,
             "user_login_result_v2": LOGIN_TIMEOUT, "user_session_poll": LOGIN_TIMEOUT,
             "user_downloads": UPLOAD_TIMEOUT}
_RETRY_STATUSES = (408, 425, 429, 500, 502, 503, 504)
LOGIN_BEGIN_RETRY = RetryPolicy(max_attempts=2)
UPLOAD_RETRY = RetryPolicy(max_attempts=3, base_s=2.0, cap_s=30.0)

MAX_MEMORY_BYTES = 8 * 1024 * 1024


def _refused(endpoint: str, detail: str) -> BdrRejected:
    return BdrRejected(endpoint, 403, ErrorCode.IDOR.value, detail)


def _template_re(template: str, job_id: str) -> re.Pattern[str]:
    out = ""
    for part in re.split(r"(\{[a-z_]+\})", template):
        if part == "{job_id}":
            out += re.escape(job_id)
        elif part.startswith("{") and part.endswith("}"):
            out += r"[0-9a-fA-F-]{36}"
        else:
            out += re.escape(part)
    return re.compile(f"^{out}$")


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class UserV2Client(BdrClient):
    """The client a user_browser v2 slot gets. It reuses the base client's
    transport and token; BdrClient's typed methods (append_steps, turn,
    finish, ...) work unchanged because they all go through `call`."""

    def __init__(self, base: BdrClient):
        self.base = base
        self.retry = base.retry
        self.in_job_retry = base.in_job_retry
        self._sleep = getattr(base, "_sleep", asyncio.sleep)
        self.on_scope_fence: Callable[[], Awaitable[None]] | None = None
        self._in_fence = False
        self._result_seq = 0

    # Delegation of the few read-only properties other code asks a client for.

    @property
    def base_url(self) -> str:
        return self.base.base_url

    @property
    def version(self) -> str:
        return self.base.version

    def seconds_since_contact(self) -> float:
        return self.base.seconds_since_contact()

    async def aclose(self) -> None:   # the base client belongs to the supervisor
        return None

    # Closed map over the shared routes ------------------------------------------------

    async def call(self, endpoint: str, *, retry: RetryPolicy | None = None, **kw: Any) -> Any:
        target = ENDPOINT_MAP.get(endpoint)
        if target is None:
            raise _refused(endpoint, "Personal task endpoint refused")
        return await self._fenced(lambda: self.base.call(target, retry=retry, **kw))

    async def _fenced(self, run: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await run()
        except BdrRejected as exc:
            if exc.code not in SCOPE_FENCE_CODES or self.on_scope_fence is None or self._in_fence:
                raise
            self._in_fence = True
            try:
                await self.on_scope_fence()
            finally:
                self._in_fence = False
            return await run()

    # Personal models on the shared help routes (UUID message ids).

    async def help_open(self, lease: Lease, req: HelpOpenRequest) -> HelpOpenResponse:
        body = UserHelpOpenV2Request(reason_code=req.reason_code, message=req.message,
                                     screenshot_artifact_id=req.screenshot_artifact_id)
        return await self.help_open_v2(lease, body)

    async def help_messages(self, lease: Lease, help_id: UUID, after: Any = None) -> Any:
        # Personal help ids are UUIDs: the whole thread comes back, never `after`.
        return await self.call("help_messages", lease=lease, path_params={"help_id": help_id},
                               retry=self.in_job_retry)

    async def help_consume(self, lease: Lease, help_id: UUID, message_ids: list[Any]) -> None:
        await self.call("help_consume", lease=lease, path_params={"help_id": help_id},
                        body=UserHelpConsumeRequest(message_ids=message_ids), retry=self.in_job_retry)

    async def help_secret_use(self, lease: Lease, help_id: UUID, message_id: Any, *,
                              login_attempt_id: UUID | None = None) -> Any:
        if login_attempt_id is None:
            raise _refused("help_secret_use", "A code is redeemed only inside a login attempt")
        return await self.call("help_secret_use", lease=lease,
                               path_params={"help_id": help_id, "message_id": message_id},
                               body=UserHelpSecretUseRequest(login_attempt_id=login_attempt_id),
                               idem_key=f"secret:{help_id}:{message_id}", retry=self.in_job_retry)

    async def identity_session(self, *a: Any, **kw: Any) -> Any:
        raise _refused("identity_session", "Personal v2 jobs use the session poll route")

    async def login_begin(self, *a: Any, **kw: Any) -> Any:
        raise _refused("login_begin", "Personal v2 jobs sign in with fill_credential")

    async def login_result(self, *a: Any, **kw: Any) -> Any:
        raise _refused("login_result", "Personal v2 jobs sign in with fill_credential")

    # v2 routes --------------------------------------------------------------------

    def _headers(self, key: str, lease: Lease, idem_key: str | None) -> dict[str, str]:
        token = getattr(self.base, "_token", None)
        if not token:
            raise WorkerUnauthorized(key, 401, ErrorCode.UNAUTHORIZED.value, "no token")
        h = {"User-Agent": f"bdr-agent/{self.base.version}", "Accept": "application/json",
             HEADER_AUTH: f"Bearer {token}", HEADER_LEASE: lease.header()}
        if self.base.boot_id is not None:
            h[HEADER_BOOT] = str(self.base.boot_id)
        if USER_V2_ENDPOINTS[key][0] in ("POST", "PUT"):
            h[HEADER_IDEMPOTENCY] = idem_key or uuid.uuid4().hex
        return h

    async def call_v2(self, key: str, *, lease: Lease, body: BaseModel | None = None,
                      query: dict[str, Any] | None = None, path_params: dict[str, Any] | None = None,
                      idem_key: str | None = None, retry: RetryPolicy | None = None,
                      form: dict[str, str] | None = None,
                      file_opener: Callable[[], Any] | None = None, raw: bool = False) -> Any:
        """One v2 call. `body` must be the endpoint's request model (sent as
        JSON, or as query parameters on a GET). A multipart call passes `form`
        and `file_opener` (opened again for each attempt, closed after)."""
        if key not in V2_KEYS:
            raise _refused(key, "Personal task endpoint refused")
        return await self._fenced(lambda: self._call_v2(key, lease=lease, body=body, query=query,
                                                        path_params=path_params, idem_key=idem_key,
                                                        retry=retry, form=form, file_opener=file_opener,
                                                        raw=raw))

    async def _call_v2(self, key: str, *, lease: Lease, body: BaseModel | None, query: dict[str, Any] | None,
                       path_params: dict[str, Any] | None, idem_key: str | None, retry: RetryPolicy | None,
                       form: dict[str, str] | None, file_opener: Callable[[], Any] | None, raw: bool) -> Any:
        method, template, req_model, resp_model, _status = USER_V2_ENDPOINTS[key]
        if body is not None and req_model is not None and not isinstance(body, req_model):
            raise TypeError(f"{key} takes {req_model.__name__}")
        pp = {"job_id": lease.job_id, **(path_params or {})}
        url = self.base.base_url + API_PREFIX + template.format(**{k: str(v) for k, v in pp.items()})
        headers = self._headers(key, lease, idem_key)
        params = dict(query or {})
        content = None
        if body is not None and method == "GET":
            params.update({k: (v.isoformat() if isinstance(v, datetime) else str(v))
                           for k, v in body.model_dump(exclude_none=True).items()})
        elif body is not None and file_opener is None:
            content = body.model_dump_json(exclude_none=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method in ("POST", "PUT") and file_opener is None:
            content = b""
        policy = retry or self.in_job_retry
        timeout = _TIMEOUTS.get(key, V2_TIMEOUT)
        http: httpx.AsyncClient = self.base._http
        started = time.monotonic()
        attempt = 0
        while True:
            retry_after = None
            handle = None
            try:
                files = None
                if file_opener is not None:
                    handle = file_opener()
                    files = {"file": handle}
                resp = await http.request(method, url, headers=headers, content=content, data=form,
                                          files=files, params=params or None, timeout=timeout)
            except httpx.TransportError as exc:
                last = type(exc).__name__
            else:
                if resp.status_code < 500:
                    self.base._touch()
                if resp.status_code not in _RETRY_STATUSES:
                    self.base._check(key, resp)
                    return self._parse(resp, resp_model, raw)
                last = f"HTTP {resp.status_code}"
                retry_after = _retry_after(resp)
            finally:
                if handle is not None:
                    _close(handle)
            delay = policy.delay(attempt, retry_after)
            if self.base._out_of_budget(policy, attempt, started, delay):
                raise BdrUnreachable(key, last)
            await self._sleep(delay)
            attempt += 1

    @staticmethod
    def _parse(resp: httpx.Response, resp_model: Any, raw: bool) -> Any:
        if resp.status_code == 204:
            return None if not raw else {}
        data = resp.json()
        if raw:
            if not isinstance(data, dict):
                raise BdrRejected("v2", resp.status_code, ErrorCode.INVALID_REQUEST.value, "not an object")
            return data
        if resp_model is None:
            return None
        return resp_model.model_validate(data)

    # Typed v2 calls ---------------------------------------------------------------

    async def get_policy(self, lease: Lease) -> UserJobPolicy:
        policy = await self.call_v2("user_policy", lease=lease)
        if str(policy.job_id) != str(lease.job_id):
            raise _refused("user_policy", "Policy is for another job")
        return policy

    async def login_begin_v2(self, lease: Lease, req: UserCredentialFillRequest) -> UserCredentialFillResponse:
        # Not retried blindly: a lost response would burn an attempt.
        return await self.call_v2("user_login_begin_v2", lease=lease, body=req, retry=LOGIN_BEGIN_RETRY,
                                  idem_key=f"login_begin:{lease.job_id}:{lease.epoch}:{uuid.uuid4().hex}")

    async def login_result_v2(self, lease: Lease, req: UserCredentialResultRequest) -> UserCredentialResultResponse:
        self._result_seq += 1
        return await self.call_v2("user_login_result_v2", lease=lease, body=req,
                                  idem_key=f"login_result:{req.attempt_id}:{req.outcome}:{self._result_seq}")

    async def session_poll(self, lease: Lease, poll: UserSessionPoll) -> SessionResponse:
        return await self.call_v2("user_session_poll", lease=lease, body=poll)

    async def help_open_v2(self, lease: Lease, req: UserHelpOpenV2Request) -> HelpOpenResponse:
        return await self.call_v2("user_help_open_v2", lease=lease, body=req)

    async def upload_download(self, lease: Lease, meta: UserDownloadMeta, path: Path,
                              *, retry: RetryPolicy | None = None) -> UserDownloadResponse:
        """Multipart upload of one scanned file (the file part is streamed from
        disk, opened again for each attempt)."""
        form = {k: str(v) for k, v in meta.model_dump(mode="json").items()}
        filename = meta.filename

        def opener() -> Any:
            return (filename, open(path, "rb"), "application/octet-stream")

        return await self.call_v2("user_downloads", lease=lease, form=form, file_opener=opener,
                                  idem_key=f"download:{lease.job_id}:{meta.sha256}", retry=retry or UPLOAD_RETRY)

    def sender(self, lease: Lease) -> Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None],
                                                 Awaitable[dict[str, Any]]]:
        """The lease-fenced JSON send for holds.EndpointHoldTransport. Only
        this job's three hold routes are reachable through it."""
        job = str(lease.job_id)
        routes = [(key, USER_V2_ENDPOINTS[key][0], _template_re(USER_V2_ENDPOINTS[key][1], job))
                  for key in HOLD_KEYS]

        async def send(method: str, path: str, json_body: dict[str, Any] | None,
                       query: dict[str, Any] | None) -> dict[str, Any]:
            for key, want, pattern in routes:
                match = pattern.match(path)
                if match and method.upper() == want:
                    break
            else:
                raise _refused("user_holds", "Hold route refused")
            # One key per logical hold (reused by the retries inside _send_raw):
            # two identical page requests are two holds, never one.
            idem = f"hold:{uuid.uuid4().hex}" if want == "POST" else None
            return await self._send_raw(key, want, path, lease, json_body, query, idem)

        return send

    async def _send_raw(self, key: str, method: str, path: str, lease: Lease, json_body: dict[str, Any] | None,
                        query: dict[str, Any] | None, idem: str | None) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            url = self.base.base_url + API_PREFIX + path
            headers = self._headers(key, lease, idem)
            content = None
            if method == "POST":
                content = json.dumps(json_body or {}, separators=(",", ":")).encode("utf-8")
                headers["Content-Type"] = "application/json"
            policy = self.in_job_retry
            started = time.monotonic()
            attempt = 0
            while True:
                retry_after = None
                try:
                    resp = await self.base._http.request(method, url, headers=headers, content=content,
                                                         params=query or None,
                                                         timeout=_TIMEOUTS.get(key, V2_TIMEOUT))
                except httpx.TransportError as exc:
                    last = type(exc).__name__
                else:
                    if resp.status_code < 500:
                        self.base._touch()
                    if resp.status_code not in _RETRY_STATUSES:
                        self.base._check(key, resp)
                        return self._parse(resp, None, True)
                    last = f"HTTP {resp.status_code}"
                    retry_after = _retry_after(resp)
                delay = policy.delay(attempt, retry_after)
                if self.base._out_of_budget(policy, attempt, started, delay):
                    raise BdrUnreachable(key, last)
                await self._sleep(delay)
                attempt += 1

        return await self._fenced(run)


def _close(handle: Any) -> None:
    fh = handle[1] if isinstance(handle, tuple) and len(handle) > 1 else handle
    close = getattr(fh, "close", None)
    if callable(close):
        with contextlib.suppress(OSError):
            close()


# Heartbeat-thread flushes ------------------------------------------------------------


class UserV2SyncClient(SyncBdrClient):
    """Only the personal step route, for the heartbeat thread's stall traces."""

    def __init__(self, base: SyncBdrClient):
        self.base = base

    def call(self, endpoint: str, **kw: Any) -> Any:
        if endpoint != "steps":
            raise _refused(endpoint, "Personal task endpoint refused")
        return self.base.call("user_steps", **kw)


# Memory-only spool -------------------------------------------------------------------


class MemoryJobSpool(JobSpool):
    """A JobSpool that never writes a file. Bounded: past MAX_MEMORY_BYTES of
    unacknowledged rows a new row raises (the job then fails as a crash
    rather than spilling personal steps to disk)."""

    def __init__(self, job_id: UUID, epoch: int, *, max_bytes: int = MAX_MEMORY_BYTES):
        super().__init__(Path("."), job_id, epoch)
        self._loaded = True
        self._bytes = 0
        self._max = max_bytes
        self._sizes: dict[str, int] = {}

    def _load_locked(self) -> None:
        return None

    def _write_locked(self, records: list[dict[str, Any]]) -> None:
        rows = [r["s"] for r in records if isinstance(r.get("s"), dict)]
        sizes = {str(r["idem_key"]): len(json.dumps(r, default=str).encode("utf-8")) for r in rows}
        if self._bytes + sum(sizes.values()) > self._max:
            raise RuntimeError("Personal task log memory cap reached")
        self._bytes += sum(sizes.values())
        self._sizes.update(sizes)

    def ack(self, idem_keys: list[str]) -> None:
        super().ack(idem_keys)
        with self._lock:
            for key in idem_keys:
                if self._rows.pop(key, None) is not None:
                    self._bytes = max(0, self._bytes - self._sizes.pop(key, 0))

    def remove_if_settled(self) -> bool:
        with self._lock:
            if not self._done or any(k not in self._acked for k in self._rows):
                return False
            self._rows.clear()
            self._acked.clear()
            self._sizes.clear()
            self._bytes = 0
            return True


class MemorySpool(Spool):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[tuple[UUID, int], JobSpool] = {}

    def for_job(self, job_id: UUID, epoch: int) -> JobSpool:
        key = (UUID(str(job_id)), int(epoch))
        with self._lock:
            sp = self._open.get(key)
            if sp is None:
                sp = MemoryJobSpool(*key)
                self._open[key] = sp
            return sp

    def on_disk(self) -> list[JobSpool]:
        return []


class UserV2StepLog(StepLog):
    def flush_sync(self, client: SyncBdrClient, *, retry: RetryPolicy = FLUSH_RETRY) -> Any:
        return super().flush_sync(UserV2SyncClient(client), retry=retry)


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "ENDPOINT_MAP",
    "HOLD_KEYS",
    "SCOPE_FENCE_CODES",
    "V2_KEYS",
    "MemorySpool",
    "UserV2Client",
    "UserV2StepLog",
    "UserV2SyncClient",
    "utc_now",
]
