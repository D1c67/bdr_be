"""BDR API client for the worker (spec 3.1, 3.2; contract C1 in protocol.py).

Two flavours share one request builder and one error mapper:
  BdrClient       async (httpx.AsyncClient), used by the supervisor and slots;
  SyncBdrClient   sync (httpx.Client), owned by the heartbeat thread so a
                  blocked asyncio loop never stops lease renewal.

Every call:
  - sends `Authorization: Bearer bdrw_...` (except /enroll), `X-Agent-Boot`
    once the boot id is known, and `X-Agent-Lease: <job>:<epoch>` on job calls;
  - sends one `Idempotency-Key` per logical POST/PUT, reused across retries;
  - retries transport errors, 5xx, 408 and 429 with capped exponential
    backoff plus jitter (429 honours Retry-After);
  - maps 4xx `X-Error-Code` to typed exceptions: LeaseLost (lease_lost,
    job_not_running), StaleBoot (exit code 3), WorkerRevoked, WorkerUnauthorized,
    PendingApproval, and BdrRejected for the rest.

Nothing here logs request or response bodies, the token, or the Lease header.
"""

import asyncio
import hashlib
import json
import random
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from uuid import UUID

import httpx
from pydantic import BaseModel

from .protocol import (
    API_PREFIX,
    ARTIFACT_RULES,
    BDR_UNREACHABLE_ABORT_SECONDS,
    ENDPOINTS,
    HEADER_AUTH,
    HEADER_BOOT,
    HEADER_CONFIG_ETAG,
    HEADER_ERROR,
    HEADER_IDEMPOTENCY,
    HEADER_LEASE,
    MAX_STEPS_PER_POST,
    NO_AUTH_ENDPOINTS,
    SESSION_CALL_TIMEOUT_SECONDS,
    AppendStepsRequest,
    AppendStepsResponse,
    ArtifactUploadForm,
    ArtifactUploadResponse,
    CheckpointRequest,
    ClaimRequest,
    ClaimResponse,
    ConfigResponse,
    ContributionsRequest,
    ContributionsResponse,
    EnrollRequest,
    EnrollResponse,
    ErrorCode,
    FinishRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    HelloRequest,
    HelloResponse,
    HelpConsumeRequest,
    HelpMessagesResponse,
    HelpOpenRequest,
    HelpOpenResponse,
    HelpReplyRequest,
    HelpReplyResponse,
    HelpSecretUseResponse,
    LoginBeginRequest,
    LoginBeginResponse,
    LoginResultRequest,
    LoginResultResponse,
    PendingResponse,
    ReleaseResponse,
    RequeueRequest,
    RfiCaptureRequest,
    RfiCaptureResponse,
    SessionRequest,
    SessionResponse,
    StepIn,
    TurnActionResponse,
    TurnPendingResponse,
    TurnRequest,
    TurnStopResponse,
    parse_turn_response,
)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
SESSION_TIMEOUT = httpx.Timeout(float(SESSION_CALL_TIMEOUT_SECONDS), connect=10.0)


# Errors --------------------------------------------------------------------------


class BdrApiError(Exception):
    """A definitive 4xx answer from BDR. Never retried."""

    def __init__(self, endpoint: str, status: int, code: str | None, detail: str | None = None):
        self.endpoint = endpoint
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{endpoint}: HTTP {status} {code or ''}".strip())


class LeaseLost(BdrApiError):
    """lease_lost or job_not_running: abort that job at once (late steps still ok)."""


class StaleBoot(BdrApiError):
    """Another process owns this worker's boot. The process exits with code 3."""


class WorkerRevoked(BdrApiError):
    """IT Admin revoked the worker. Stop the service; re-enroll needs the installer."""


class WorkerUnauthorized(BdrApiError):
    """The token is unknown. Same handling as revoked."""


class PendingApproval(BdrApiError):
    """Enrolled but not approved yet: wait."""


class BdrRejected(BdrApiError):
    """Any other definitive 4xx (idor, invalid_request, capture_invalid, ...)."""


class BdrUnreachable(Exception):
    """Retries ran out (transport errors, 5xx, 429) before BDR answered."""

    def __init__(self, endpoint: str, last: str):
        self.endpoint = endpoint
        self.last = last
        super().__init__(f"{endpoint}: BDR unreachable ({last})")


_CODE_EXC: dict[str, type[BdrApiError]] = {
    ErrorCode.LEASE_LOST.value: LeaseLost,
    ErrorCode.JOB_NOT_RUNNING.value: LeaseLost,
    ErrorCode.STALE_BOOT.value: StaleBoot,
    ErrorCode.REVOKED.value: WorkerRevoked,
    ErrorCode.UNAUTHORIZED.value: WorkerUnauthorized,
    ErrorCode.PENDING_APPROVAL.value: PendingApproval,
}

_RETRY_STATUSES = (408, 425, 429, 500, 502, 503, 504)


class Lease(NamedTuple):
    job_id: UUID
    epoch: int

    def header(self) -> str:
        return f"{self.job_id}:{self.epoch}"


@dataclass(frozen=True)
class RetryPolicy:
    """Stop after `max_attempts` tries or `deadline_s` seconds, whichever first.
    None means no limit on that axis (at least one must be set)."""
    max_attempts: int | None = 5
    deadline_s: float | None = None
    base_s: float = 0.5
    cap_s: float = 30.0

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.cap_s * 4)
        ceiling = min(self.cap_s, self.base_s * (2 ** attempt))
        return random.uniform(ceiling / 2, ceiling)


ONE_SHOT = RetryPolicy(max_attempts=1)
DEFAULT_RETRY = RetryPolicy(max_attempts=5)
# In-job calls a live job cannot continue without (turns, help, identity,
# capture, checkpoints). SPEC 3.3: keep retrying with jitter until the 15-minute
# local abort, so a BDR deploy or short outage never kills a live job.
IN_JOB_RETRY = RetryPolicy(max_attempts=None, deadline_s=float(BDR_UNREACHABLE_ABORT_SECONDS), cap_s=30.0)


def steps_idempotency_key(steps: Iterable[dict[str, Any]]) -> str:
    joined = "\n".join(str(s.get("idem_key")) for s in steps)
    return "steps:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:40]


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass
class _Prepared:
    endpoint: str
    method: str
    url: str
    headers: dict[str, str]
    content: bytes | None = None
    data: dict[str, str] | None = None
    files: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    timeout: httpx.Timeout = field(default_factory=lambda: DEFAULT_TIMEOUT)


class _ClientBase:
    def __init__(self, base_url: str, *, token: str | None = None, boot_id: UUID | None = None,
                 version: str = "0.0.0"):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.boot_id = boot_id
        self.version = version
        self._contact_lock = threading.Lock()
        self._last_contact = time.monotonic()

    # Contact tracking (spec 3.3: abort a job after 15 min without BDR) --------

    def seconds_since_contact(self) -> float:
        with self._contact_lock:
            return time.monotonic() - self._last_contact

    def _touch(self) -> None:
        with self._contact_lock:
            self._last_contact = time.monotonic()

    def set_token(self, token: str | None) -> None:
        self._token = token

    # Request building ------------------------------------------------------------

    def prepare(self, endpoint: str, *, path_params: dict[str, Any] | None = None,
                lease: Lease | None = None, body: BaseModel | None = None,
                params: dict[str, Any] | None = None, idem_key: str | None = None,
                headers: dict[str, str] | None = None, form: dict[str, str] | None = None,
                files: dict[str, Any] | None = None) -> _Prepared:
        method, template, _req_model, _resp_model, _status = ENDPOINTS[endpoint]
        pp = dict(path_params or {})
        if lease is not None:
            pp.setdefault("job_id", lease.job_id)
        path = API_PREFIX + template.format(**{k: str(v) for k, v in pp.items()})
        h: dict[str, str] = {"User-Agent": f"bdr-agent/{self.version}", "Accept": "application/json"}
        if endpoint not in NO_AUTH_ENDPOINTS:
            if not self._token:
                raise WorkerUnauthorized(endpoint, 401, ErrorCode.UNAUTHORIZED.value, "no token")
            h[HEADER_AUTH] = f"Bearer {self._token}"
            if self.boot_id is not None:
                h[HEADER_BOOT] = str(self.boot_id)
        if lease is not None:
            h[HEADER_LEASE] = lease.header()
        if method in ("POST", "PUT"):
            h[HEADER_IDEMPOTENCY] = idem_key or uuid.uuid4().hex
        if headers:
            h.update(headers)
        prepared = _Prepared(endpoint=endpoint, method=method, url=self.base_url + path, headers=h,
                             params=params, data=form, files=files)
        # With `files`, httpx builds the multipart body and its Content-Type.
        if files is not None:
            prepared.content = None
        elif body is not None:
            prepared.content = body.model_dump_json().encode("utf-8")
            h["Content-Type"] = "application/json"
        elif method in ("POST", "PUT"):
            prepared.content = b""
        if endpoint == "identity_session" or endpoint.startswith("login_"):
            prepared.timeout = SESSION_TIMEOUT
        return prepared

    # Response handling -------------------------------------------------------------

    def _check(self, endpoint: str, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        code = resp.headers.get(HEADER_ERROR)
        detail = None
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                detail = payload.get("detail") if isinstance(payload.get("detail"), str) else None
                if not code and detail in {c.value for c in ErrorCode}:
                    code = detail
        except (ValueError, json.JSONDecodeError):
            pass
        exc = _CODE_EXC.get(code or "", BdrRejected)
        raise exc(endpoint, resp.status_code, code, detail)

    def _parse(self, endpoint: str, resp: httpx.Response) -> Any:
        if resp.status_code == 304 or resp.status_code == 204:
            return None
        _m, _p, _req, resp_model, _s = ENDPOINTS[endpoint]
        if endpoint == "canary_page":
            return resp.text
        if endpoint in ("turns", "user_turns"):
            return parse_turn_response(resp.status_code, resp.json())
        data = resp.json()
        if endpoint == "config" and isinstance(data, dict) and data.get("pending") is True:
            return PendingResponse.model_validate(data)
        if resp_model is None:
            return None
        return resp_model.model_validate(data)

    @staticmethod
    def _retryable(resp: httpx.Response) -> bool:
        return resp.status_code in _RETRY_STATUSES

    @staticmethod
    def _out_of_budget(policy: RetryPolicy, attempt: int, started: float, next_delay: float) -> bool:
        if policy.max_attempts is not None and attempt + 1 >= policy.max_attempts:
            return True
        if policy.deadline_s is not None and (time.monotonic() - started) + next_delay > policy.deadline_s:
            return True
        return False

    # Typed request bodies shared by both flavours ----------------------------------

    @staticmethod
    def _steps_body(steps: list[dict[str, Any]]) -> AppendStepsRequest:
        if not 1 <= len(steps) <= MAX_STEPS_PER_POST:
            raise ValueError(f"steps per post must be 1..{MAX_STEPS_PER_POST}")
        return AppendStepsRequest(steps=[StepIn.model_validate(s) for s in steps])


class BdrClient(_ClientBase):
    """Async client. One instance per worker process, shared by all slots."""

    def __init__(self, base_url: str, *, token: str | None = None, boot_id: UUID | None = None,
                 version: str = "0.0.0", transport: httpx.AsyncBaseTransport | None = None,
                 retry: RetryPolicy = DEFAULT_RETRY, sleep=asyncio.sleep,
                 in_job_retry: RetryPolicy = IN_JOB_RETRY):
        super().__init__(base_url, token=token, boot_id=boot_id, version=version)
        self.retry = retry
        self.in_job_retry = in_job_retry
        self._sleep = sleep
        self._http = httpx.AsyncClient(transport=transport, timeout=DEFAULT_TIMEOUT, follow_redirects=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, endpoint: str, *, retry: RetryPolicy | None = None, **kw: Any) -> Any:
        prepared = self.prepare(endpoint, **kw)
        policy = retry or self.retry
        started = time.monotonic()
        attempt = 0
        while True:
            retry_after = None
            try:
                resp = await self._http.request(
                    prepared.method, prepared.url, headers=prepared.headers, content=prepared.content,
                    data=prepared.data, files=prepared.files, params=prepared.params,
                    timeout=prepared.timeout)
            except httpx.TransportError as exc:
                last = type(exc).__name__
            else:
                if resp.status_code < 500:
                    self._touch()
                if not self._retryable(resp):
                    self._check(endpoint, resp)
                    return self._parse(endpoint, resp)
                last = f"HTTP {resp.status_code}"
                retry_after = _retry_after(resp)
            delay = policy.delay(attempt, retry_after)
            if self._out_of_budget(policy, attempt, started, delay):
                raise BdrUnreachable(endpoint, last)
            await self._sleep(delay)
            attempt += 1

    # Endpoints -----------------------------------------------------------------

    async def enroll(self, req: EnrollRequest) -> EnrollResponse:
        return await self.call("enroll", body=req)

    async def hello(self, req: HelloRequest, *, retry: RetryPolicy | None = None) -> HelloResponse:
        return await self.call("hello", body=req, retry=retry)

    async def get_config(self, etag: int | None = None, *,
                         retry: RetryPolicy | None = None) -> ConfigResponse | PendingResponse | None:
        """None means 304: the config at `etag` is still current."""
        headers = {HEADER_CONFIG_ETAG: str(etag)} if etag is not None else None
        return await self.call("config", headers=headers, retry=retry)

    async def claim(self, req: ClaimRequest) -> ClaimResponse:
        return await self.call("claim", body=req, retry=ONE_SHOT)

    async def append_steps(self, lease: Lease, steps: list[dict[str, Any]], *,
                           retry: RetryPolicy | None = None) -> AppendStepsResponse:
        return await self.call("steps", lease=lease, body=self._steps_body(steps),
                               idem_key=steps_idempotency_key(steps), retry=retry)

    async def put_checkpoint(self, lease: Lease, seq: int, data: dict[str, Any]) -> None:
        await self.call("checkpoint", lease=lease, body=CheckpointRequest(seq=seq, data=data),
                        idem_key=f"checkpoint:{lease.job_id}:{lease.epoch}:{seq}",
                        retry=self.in_job_retry)

    async def upload_artifact(self, lease: Lease, kind: str, filename: str, data: bytes,
                              mime_type: str, *, retry: RetryPolicy | None = None) -> ArtifactUploadResponse:
        sha = hashlib.sha256(data).hexdigest()
        form = ArtifactUploadForm(kind=kind, sha256=sha, filename=filename)  # type: ignore[arg-type]
        rule = ARTIFACT_RULES[form.kind]
        if len(data) > rule["max_bytes"]:
            raise ValueError(f"{kind} artifact exceeds {rule['max_bytes']} bytes")
        if mime_type not in rule["mime_types"]:
            raise ValueError(f"{kind} artifact must be one of {rule['mime_types']}")
        return await self.call(
            "artifacts", lease=lease, idem_key=f"artifact:{form.kind}:{form.sha256}",
            form={"kind": form.kind, "sha256": form.sha256, "filename": form.filename},
            files={"file": (form.filename, data, mime_type)}, retry=retry)

    async def turn(self, lease: Lease, req: TurnRequest) -> (
            TurnActionResponse | TurnPendingResponse | TurnStopResponse):
        return await self.call("turns", lease=lease, body=req,
                               idem_key=f"turn:{lease.job_id}:{lease.epoch}:{req.prev_turn_no}",
                               retry=self.in_job_retry)

    async def identity_session(self, lease: Lease, identity: str, req: SessionRequest) -> SessionResponse:
        return await self.call("identity_session", lease=lease, path_params={"identity": identity}, body=req,
                               retry=self.in_job_retry)

    async def login_begin(self, lease: Lease, identity: str, req: LoginBeginRequest) -> LoginBeginResponse:
        # Not retried blindly: a lost response would burn a login slot; BDR's
        # spacing answer tells the caller when to come back.
        return await self.call("login_begin", lease=lease, path_params={"identity": identity}, body=req,
                               retry=RetryPolicy(max_attempts=2))

    async def login_result(self, lease: Lease, identity: str, req: LoginResultRequest) -> LoginResultResponse:
        return await self.call("login_result", lease=lease, path_params={"identity": identity}, body=req,
                               idem_key=f"login_result:{req.attempt_id}", retry=self.in_job_retry)

    async def help_open(self, lease: Lease, req: HelpOpenRequest) -> HelpOpenResponse:
        return await self.call("help_open", lease=lease, body=req, retry=self.in_job_retry)

    async def help_messages(self, lease: Lease, help_id: UUID, after: int | None = None) -> HelpMessagesResponse:
        params = {"after": after} if after is not None else None
        return await self.call("help_messages", lease=lease, path_params={"help_id": help_id}, params=params,
                               retry=self.in_job_retry)

    async def help_consume(self, lease: Lease, help_id: UUID, message_ids: list[int]) -> None:
        await self.call("help_consume", lease=lease, path_params={"help_id": help_id},
                        body=HelpConsumeRequest(message_ids=message_ids), retry=self.in_job_retry)

    async def help_reply(self, lease: Lease, help_id: UUID, body: str) -> HelpReplyResponse:
        return await self.call("help_reply", lease=lease, path_params={"help_id": help_id},
                               body=HelpReplyRequest(body=body))

    async def help_secret_use(self, lease: Lease, help_id: UUID, message_id: int) -> HelpSecretUseResponse:
        return await self.call("help_secret_use", lease=lease,
                               path_params={"help_id": help_id, "message_id": message_id},
                               idem_key=f"secret:{help_id}:{message_id}", retry=self.in_job_retry)

    async def park(self, lease: Lease) -> None:
        await self.call("park", lease=lease, idem_key=f"park:{lease.job_id}:{lease.epoch}",
                        retry=self.in_job_retry)

    async def contributions(self, lease: Lease, req: ContributionsRequest) -> ContributionsResponse:
        return await self.call("contributions", lease=lease, body=req)

    async def rfi_capture(self, lease: Lease, req: RfiCaptureRequest) -> RfiCaptureResponse:
        return await self.call("rfi_capture", lease=lease, body=req,
                               idem_key=f"capture:{lease.job_id}:{lease.epoch}:{req.detail_artifact_id}",
                               retry=self.in_job_retry)

    async def finish(self, lease: Lease, req: FinishRequest, *, retry: RetryPolicy | None = None) -> None:
        await self.call("finish", lease=lease, body=req, idem_key=f"finish:{lease.job_id}:{lease.epoch}",
                        retry=retry)

    async def requeue(self, lease: Lease, req: RequeueRequest, *, retry: RetryPolicy | None = None) -> None:
        await self.call("requeue", lease=lease, body=req, idem_key=f"requeue:{lease.job_id}:{lease.epoch}",
                        retry=retry)

    async def release(self, version: str) -> ReleaseResponse:
        return await self.call("release", path_params={"version": version})

    async def canary_page(self) -> str:
        return await self.call("canary_page")


class SyncBdrClient(_ClientBase):
    """Blocking client for the heartbeat thread (its own httpx.Client) and the
    enroll command. Never used from inside the asyncio loop."""

    def __init__(self, base_url: str, *, token: str | None = None, boot_id: UUID | None = None,
                 version: str = "0.0.0", transport: httpx.BaseTransport | None = None,
                 retry: RetryPolicy = DEFAULT_RETRY, sleep=time.sleep):
        super().__init__(base_url, token=token, boot_id=boot_id, version=version)
        self.retry = retry
        self._sleep = sleep
        self._http = httpx.Client(transport=transport, timeout=DEFAULT_TIMEOUT, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def call(self, endpoint: str, *, retry: RetryPolicy | None = None, **kw: Any) -> Any:
        prepared = self.prepare(endpoint, **kw)
        policy = retry or self.retry
        started = time.monotonic()
        attempt = 0
        while True:
            retry_after = None
            try:
                resp = self._http.request(
                    prepared.method, prepared.url, headers=prepared.headers, content=prepared.content,
                    data=prepared.data, files=prepared.files, params=prepared.params,
                    timeout=prepared.timeout)
            except httpx.TransportError as exc:
                last = type(exc).__name__
            else:
                if resp.status_code < 500:
                    self._touch()
                if not self._retryable(resp):
                    self._check(endpoint, resp)
                    return self._parse(endpoint, resp)
                last = f"HTTP {resp.status_code}"
                retry_after = _retry_after(resp)
            delay = policy.delay(attempt, retry_after)
            if self._out_of_budget(policy, attempt, started, delay):
                raise BdrUnreachable(endpoint, last)
            self._sleep(delay)
            attempt += 1

    def enroll(self, req: EnrollRequest) -> EnrollResponse:
        return self.call("enroll", body=req)

    def heartbeat(self, req: HeartbeatRequest, *, retry: RetryPolicy | None = None) -> HeartbeatResponse:
        return self.call("heartbeat", body=req, retry=retry)

    def append_steps(self, lease: Lease, steps: list[dict[str, Any]], *,
                     retry: RetryPolicy | None = None) -> AppendStepsResponse:
        return self.call("steps", lease=lease, body=self._steps_body(steps),
                         idem_key=steps_idempotency_key(steps), retry=retry)

    def requeue(self, lease: Lease, req: RequeueRequest, *, retry: RetryPolicy | None = None) -> None:
        self.call("requeue", lease=lease, body=req, idem_key=f"requeue:{lease.job_id}:{lease.epoch}",
                  retry=retry)
