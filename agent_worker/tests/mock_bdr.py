"""A stateful fake BDR for worker tests, served through httpx.MockTransport.

It speaks the C1 contract exactly:
  - every request body is validated with the protocol request model (requests
    forbid unknown fields, so a worker drifting from the contract fails here);
  - responses start from the contract fixtures in
    bdr_be/tests/fixtures_browser_agents/ and are validated with the response
    model before they are sent;
  - auth, boot and lease fencing mirror 0157 (browser_agent_fence): wrong boot
    is stale_boot, wrong epoch/owner is lease_lost, steps for an older epoch the
    worker really held (a claim proves it) are accepted as late;
  - /hello requeues leases of older boots, /claim honours allowed_types,
    enabled types, max_concurrency and draining like claim_browser_agent_jobs.

Test hooks: `add_worker`, `add_enroll_code`, `add_job`, `inject(endpoint,...)`,
`down` (every call raises ConnectError), and per-beat extras (`hb_lost`,
`hb_cancel`, `hb_orphans`, `hb_help_updates`). Every call is recorded in `calls`.
"""

import base64
import copy
import hashlib
import json
import re
import secrets as pysecrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from bdr_agent import protocol as P

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures_browser_agents"

DEFAULT_FIXTURE = {
    "enroll": "enroll", "hello": "hello", "config": "config", "heartbeat": "heartbeat",
    "claim": "claim_rfi", "steps": "steps", "checkpoint": "checkpoint", "artifacts": "artifacts",
    "turns": "turn_action_navigate", "identity_session": "session_ok",
    "login_begin": "login_begin_granted", "login_result": "login_result_ok", "help_open": "help_open",
    "help_messages": "help_messages", "help_consume": "help_consume", "help_reply": "help_reply",
    "help_secret_use": "help_secret_use", "park": "park", "contributions": "contributions",
    "rfi_capture": "rfi_capture", "finish": "finish_failed", "requeue": "requeue", "release": "release",
    "canary_page": "canary_page",
}

CANARY_HTML = "<!doctype html><html><body><p>BDR canary ok</p></body></html>"


def load_fixtures() -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES_DIR.glob("*.json"))}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@dataclass
class Worker:
    worker_id: UUID
    token: str
    status: str = "active"
    boot_id: UUID | None = None
    draining: bool = False
    max_slots: int = 3
    allowed_types: list[str] = field(default_factory=lambda: ["rfi_from_email", "canary"])
    capabilities: dict[str, Any] | None = None
    hellos: int = 0


@dataclass
class Job:
    job_id: UUID
    agent_type: str
    input: dict[str, Any]
    status: str = "queued"
    epoch: int = 0
    attempts: int = 0
    interrupted_count: int = 0
    resume_reason: str | None = None
    last_resume_reason: str | None = None
    lease_owner: UUID | None = None
    lease_boot: UUID | None = None
    lease_expires_at: datetime | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    checkpoint_seq: int = 0
    finish: dict[str, Any] | None = None
    requeues: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False


@dataclass
class Call:
    endpoint: str
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    status: int


class ApiError(Exception):
    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status or P.ERROR_HTTP_STATUS[P.ErrorCode(code)]


def _compile_routes() -> list[tuple[str, str, re.Pattern[str]]]:
    routes = []
    for key, (method, template, *_rest) in P.ENDPOINTS.items():
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template)
        routes.append((key, method, re.compile("^" + re.escape(P.API_PREFIX) + pattern + "$")))
    return routes


ROUTES = _compile_routes()


class MockBdr:
    def __init__(self) -> None:
        self.fixtures = load_fixtures()
        self.lock = threading.RLock()
        self.workers: dict[str, Worker] = {}
        self.enroll_codes: dict[str, str] = {}
        self.jobs: dict[UUID, Job] = {}
        self.steps: dict[UUID, dict[str, dict[str, Any]]] = {}
        self.late_steps: dict[UUID, int] = {}
        self.duplicate_steps: dict[UUID, int] = {}
        self.claim_proofs: set[tuple[UUID, int, UUID]] = set()
        self.calls: list[Call] = []
        self.down = False
        self.injected: dict[str, list[Any]] = {}
        self.enabled = True
        self.config_version = 12
        self.types: dict[str, dict[str, Any]] = copy.deepcopy(self.fixtures["config"]["response"]["types"])
        self.help_hold_seconds = 900
        self.hb_lost: list[UUID] = []
        self.hb_cancel: list[UUID] = []
        self.hb_orphans: list[dict[str, Any]] = []
        self.hb_help_updates: list[dict[str, Any]] = []
        self.claim_requests: list[dict[str, Any]] = []

    # Setup hooks -----------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_worker(self, *, status: str = "active", **kw: Any) -> Worker:
        token = P.WORKER_TOKEN_PREFIX + base64.urlsafe_b64encode(pysecrets.token_bytes(32)).decode().rstrip("=")
        w = Worker(worker_id=uuid.uuid4(), token=token, status=status, **kw)
        with self.lock:
            self.workers[token] = w
        return w

    def add_enroll_code(self, code: str = "ABCDEFGHJKMNPQRS2345") -> str:
        with self.lock:
            self.enroll_codes[code] = "worker"
        return code

    def add_job(self, agent_type: str, input: dict[str, Any] | None = None) -> UUID:
        if input is None:
            input = (copy.deepcopy(self.fixtures["claim_rfi"]["response"]["jobs"][0]["input"])
                     if agent_type == "rfi_from_email" else {"expect_text": "BDR canary ok"})
        job = Job(job_id=uuid.uuid4(), agent_type=agent_type, input=input)
        with self.lock:
            self.jobs[job.job_id] = job
        return job.job_id

    def inject(self, endpoint: str, *responses: Any) -> None:
        """Queue responses for an endpoint: an ErrorCode/str code, an int status,
        an httpx.Response, or an Exception instance (raised as a transport error)."""
        with self.lock:
            self.injected.setdefault(endpoint, []).extend(responses)

    def bump_config(self) -> None:
        with self.lock:
            self.config_version += 1

    def calls_to(self, endpoint: str) -> list[Call]:
        with self.lock:
            return [c for c in self.calls if c.endpoint == endpoint]

    # Plumbing --------------------------------------------------------------------------------

    def _json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> httpx.Response:
        h = {"Cache-Control": "no-store"}
        h.update(headers or {})
        if data is None:
            return httpx.Response(status, headers=h)
        h["Content-Type"] = "application/json"
        return httpx.Response(status, headers=h, content=json.dumps(data, default=str).encode())

    def _error(self, code: str, status: int | None = None) -> httpx.Response:
        st = status or P.ERROR_HTTP_STATUS.get(P.ErrorCode(code), 400)
        return self._json(st, {"detail": code}, {P.HEADER_ERROR: code})

    def _validated(self, endpoint: str, data: Any) -> Any:
        """Validate a response with the contract model before it goes out."""
        _m, _p, _req, resp_model, _s = P.ENDPOINTS[endpoint]
        if endpoint == "turns":
            P.parse_turn_response(200, data)
        elif endpoint == "config" and data.get("pending"):
            P.PendingResponse.model_validate(data)
        elif isinstance(resp_model, type):
            resp_model.model_validate(data)
        return data

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        endpoint, params = None, {}
        for key, method, rx in ROUTES:
            m = rx.match(path)
            if m and method == request.method:
                endpoint, params = key, m.groupdict()
                break
        if endpoint is None:
            return self._json(404, {"detail": "not found"})
        body = self._body(endpoint, request)
        with self.lock:
            if self.down:
                raise httpx.ConnectError("mock BDR is down", request=request)
            queue = self.injected.get(endpoint)
            if queue:
                item = queue.pop(0)
                resp = self._from_injected(item, request)
                self._record(endpoint, request, body, resp.status_code)
                return resp
            try:
                resp = self._dispatch(endpoint, params, request, body)
            except ApiError as exc:
                resp = self._error(exc.code, exc.status)
            except P.ValidationError as exc:
                resp = self._json(422, {"detail": "invalid_request", "errors": str(exc)[:2000]},
                                  {P.HEADER_ERROR: "invalid_request"})
            self._record(endpoint, request, body, resp.status_code)
            return resp

    def _from_injected(self, item: Any, request: httpx.Request) -> httpx.Response:
        if isinstance(item, BaseException):
            raise httpx.ConnectError(str(item), request=request)
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, int):
            return self._json(item, {"detail": "injected"})
        return self._error(str(item))

    def _record(self, endpoint: str, request: httpx.Request, body: Any, status: int) -> None:
        self.calls.append(Call(endpoint, request.method, request.url.path, dict(request.headers), body, status))

    def _body(self, endpoint: str, request: httpx.Request) -> Any:
        if endpoint == "artifacts":
            return self._multipart(request)
        raw = request.content
        if not raw:
            return None
        return json.loads(raw)

    @staticmethod
    def _multipart(request: httpx.Request) -> dict[str, Any]:
        ctype = request.headers.get("content-type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return {}
        boundary = m.group(1).strip('"').encode()
        out: dict[str, Any] = {}
        for part in request.content.split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            head, _, value = part.partition(b"\r\n\r\n")
            name = re.search(rb'name="([^"]+)"', head)
            if not name:
                continue
            value = value[:-2] if value.endswith(b"\r\n") else value
            key = name.group(1).decode()
            out[key] = value if key == "file" else value.decode()
        return out

    # Auth and fencing --------------------------------------------------------------------

    def _auth(self, endpoint: str, request: httpx.Request) -> Worker:
        auth = request.headers.get(P.HEADER_AUTH, "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        w = self.workers.get(token)
        if w is None:
            raise ApiError("unauthorized")
        if w.status == "revoked":
            raise ApiError("revoked")
        if w.status == "pending" and endpoint not in P.PENDING_ALLOWED_ENDPOINTS:
            raise ApiError("pending_approval")
        return w

    def _boot(self, w: Worker, request: httpx.Request) -> None:
        if w.status != "active" or str(w.boot_id) != request.headers.get(P.HEADER_BOOT):
            raise ApiError("stale_boot")

    def _lease(self, request: httpx.Request, job_id: str) -> tuple[UUID, int]:
        raw = request.headers.get(P.HEADER_LEASE, "")
        jid, _, epoch = raw.rpartition(":")
        if jid != job_id or not epoch.isdigit():
            raise ApiError("lease_lost")
        return UUID(jid), int(epoch)

    def _fence(self, w: Worker, request: httpx.Request, job_id: str,
               statuses: tuple[str, ...] = ("running", "waiting_help")) -> Job:
        self._boot(w, request)
        jid, epoch = self._lease(request, job_id)
        job = self.jobs.get(jid)
        if job is None or job.lease_owner != w.worker_id or job.epoch != epoch or job.lease_boot != w.boot_id:
            raise ApiError("lease_lost")
        if job.status not in statuses:
            raise ApiError("job_not_running")
        return job

    def _need_idem(self, request: httpx.Request) -> None:
        if request.method in ("POST", "PUT") and not request.headers.get(P.HEADER_IDEMPOTENCY):
            raise ApiError("invalid_request")

    # Dispatch -------------------------------------------------------------------------------

    def _dispatch(self, endpoint: str, params: dict[str, str], request: httpx.Request, body: Any) -> httpx.Response:
        _m, _p, req_model, _resp, status = P.ENDPOINTS[endpoint]
        if req_model is not None and endpoint != "artifacts":
            req_model.model_validate(body)
        if endpoint == "enroll":
            return self._enroll(body)
        w = self._auth(endpoint, request)
        self._need_idem(request)
        handler = getattr(self, "_ep_" + endpoint, None)
        if handler is not None:
            return handler(w, params, request, body)
        # Generic job-scoped endpoint: fence, then the contract fixture.
        if "job_id" in params:
            self._fence(w, request, params["job_id"])
        fx = self.fixtures[DEFAULT_FIXTURE[endpoint]]
        if status == 204 or fx["response"] is None and endpoint != "canary_page":
            return self._json(204, None)
        return self._json(fx["status"], self._validated(endpoint, copy.deepcopy(fx["response"])))

    def _enroll(self, body: dict[str, Any]) -> httpx.Response:
        code = body["code"]
        if code not in self.enroll_codes:
            raise ApiError("enroll_invalid")
        del self.enroll_codes[code]
        w = self.add_worker(status="pending")
        return self._json(200, self._validated("enroll", {"worker_id": str(w.worker_id), "token": w.token,
                                                          "status": "pending"}))

    def _ep_hello(self, w: Worker, params, request, body) -> httpx.Response:
        w.boot_id = UUID(body["boot_id"])
        w.capabilities = body["capabilities"]
        w.hellos += 1
        requeued = 0
        for job in self.jobs.values():
            if job.lease_owner == w.worker_id and job.lease_boot != w.boot_id and job.status in (
                    "running", "waiting_help"):
                job.status = "queued"
                job.resume_reason = "interrupted"
                job.interrupted_count += 1
                job.epoch += 1
                job.lease_owner = job.lease_boot = None
                requeued += 1
        return self._json(200, self._validated("hello", {
            "status": "pending" if w.status == "pending" else "active",
            "config_version": self.config_version, "desired_version": "1.0.0", "min_version": "1.0.0",
            "server_time": _iso(_now()), "requeued": requeued}))

    def _config_body(self, w: Worker) -> dict[str, Any]:
        base = copy.deepcopy(self.fixtures["config"]["response"])
        base.update({
            "config_version": self.config_version, "enabled": self.enabled,
            "worker": {"status": w.status, "max_slots": w.max_slots, "draining": w.draining,
                       "allowed_types": list(w.allowed_types)},
            "types": copy.deepcopy(self.types), "help_hold_seconds": self.help_hold_seconds})
        return base

    def _ep_config(self, w: Worker, params, request, body) -> httpx.Response:
        if w.status == "pending":
            return self._json(200, {"pending": True})
        if w.boot_id is not None:
            self._boot(w, request)
        if request.headers.get(P.HEADER_CONFIG_ETAG) == str(self.config_version):
            return self._json(304, None)
        return self._json(200, self._validated("config", self._config_body(w)))

    def _ep_heartbeat(self, w: Worker, params, request, body) -> httpx.Response:
        self._boot(w, request)
        if UUID(body["boot_id"]) != w.boot_id:
            raise ApiError("stale_boot")
        renewed, lost, cancel, listed = [], [], [], set()
        for item in body.get("jobs") or []:
            jid = UUID(item["job_id"])
            listed.add(jid)
            job = self.jobs.get(jid)
            if (job and job.lease_owner == w.worker_id and job.epoch == item["epoch"]
                    and job.lease_boot == w.boot_id and job.status in ("running", "waiting_help")):
                job.lease_expires_at = _now() + timedelta(seconds=P.LEASE_SECONDS)
                renewed.append({"job_id": str(jid), "lease_expires_at": _iso(job.lease_expires_at)})
                if job.cancel_requested:
                    cancel.append(str(jid))
            else:
                lost.append(str(jid))
        lost += [str(j) for j in self.hb_lost if str(j) not in lost]
        cancel += [str(j) for j in self.hb_cancel if str(j) not in cancel]
        self.hb_lost, self.hb_cancel = [], []
        orphans = [{"job_id": str(j.job_id), "epoch": j.epoch} for j in self.jobs.values()
                   if j.lease_owner == w.worker_id and j.lease_boot == w.boot_id
                   and j.status in ("running", "waiting_help") and j.job_id not in listed]
        orphans += self.hb_orphans
        updates, self.hb_help_updates = self.hb_help_updates, []
        return self._json(200, self._validated("heartbeat", {
            "config_version": self.config_version, "renewed": renewed, "lost": lost, "cancel": cancel,
            "orphans": orphans, "help_updates": updates, "drain": w.draining, "controls": []}))

    def _ep_claim(self, w: Worker, params, request, body) -> httpx.Response:
        self._boot(w, request)
        self.claim_requests.append(body)
        out: list[dict[str, Any]] = []
        if w.draining or not self.enabled:
            return self._json(200, {"jobs": []})
        for key, want in (body.get("capacity") or {}).items():
            want = max(0, min(int(want), 8))
            t = self.types.get(key)
            if want <= 0 or key not in w.allowed_types or not t or not t.get("enabled"):
                continue
            if not w.capabilities or key not in w.capabilities.get("types", {}):
                continue
            busy = sum(1 for j in self.jobs.values() if j.agent_type == key and j.lease_owner is not None)
            take = min(want, int(t["max_concurrency"]) - busy)
            for job in [j for j in self.jobs.values() if j.agent_type == key and j.status == "queued"][:max(0, take)]:
                job.status = "running"
                job.lease_owner, job.lease_boot = w.worker_id, w.boot_id
                job.epoch += 1
                if (job.resume_reason or "retry") == "retry":
                    job.attempts += 1
                job.last_resume_reason, job.resume_reason = job.resume_reason, None
                job.lease_expires_at = _now() + timedelta(seconds=P.LEASE_SECONDS)
                self.claim_proofs.add((job.job_id, job.epoch, w.worker_id))
                out.append({"job_id": str(job.job_id), "agent_type": job.agent_type, "epoch": job.epoch,
                            "lease_expires_at": _iso(job.lease_expires_at), "input": job.input,
                            "checkpoint": job.checkpoint, "checkpoint_seq": job.checkpoint_seq,
                            "attempts": job.attempts, "last_resume_reason": job.last_resume_reason,
                            "turn_count": 0, "unconsumed_help": []})
        return self._json(200, self._validated("claim", {"jobs": out}))

    def _ep_steps(self, w: Worker, params, request, body) -> httpx.Response:
        late = False
        try:
            self._fence(w, request, params["job_id"])
        except ApiError as exc:
            jid, epoch = self._lease(request, params["job_id"])
            if exc.code in ("lease_lost", "job_not_running", "stale_boot") and (
                    (jid, epoch, w.worker_id) in self.claim_proofs):
                late = True
            else:
                raise
        jid = UUID(params["job_id"])
        rows = self.steps.setdefault(jid, {})
        inserted = 0
        for s in body["steps"]:
            if s["idem_key"] in rows:
                self.duplicate_steps[jid] = self.duplicate_steps.get(jid, 0) + 1
                continue
            rows[s["idem_key"]] = dict(s, _late=late)
            inserted += 1
        if late:
            self.late_steps[jid] = self.late_steps.get(jid, 0) + inserted
        total = len(body["steps"])
        return self._json(200, self._validated("steps", {
            "appended": 0 if late else inserted, "duplicates": total - inserted, "late": inserted if late else 0}))

    def _ep_checkpoint(self, w: Worker, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        if body["seq"] > job.checkpoint_seq:
            job.checkpoint_seq, job.checkpoint = body["seq"], body["data"]
        return self._json(204, None)

    def _ep_artifacts(self, w: Worker, params, request, body) -> httpx.Response:
        self._fence(w, request, params["job_id"])
        P.ArtifactUploadForm.model_validate({k: body.get(k) for k in ("kind", "sha256", "filename")})
        if hashlib.sha256(body.get("file", b"")).hexdigest() != body["sha256"]:
            raise ApiError("artifact_invalid")
        aid = uuid.uuid5(uuid.NAMESPACE_URL, f"{params['job_id']}:{body['kind']}:{body['sha256']}")
        return self._json(200, self._validated("artifacts", {"artifact_id": str(aid), "existed": False}))

    def _ep_finish(self, w: Worker, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        job.status = body["status"]
        job.finish = body
        job.lease_owner = job.lease_boot = None
        return self._json(204, None)

    def _ep_requeue(self, w: Worker, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        if body["reason"] in ("shutdown", "drain") and not (w.draining or body.get("service_stop")):
            raise ApiError("invalid_request")
        job.requeues.append(body)
        job.status = "queued"
        job.resume_reason = "retry" if body["reason"] == "crash" else body["reason"]
        job.epoch += 1
        job.lease_owner = job.lease_boot = None
        return self._json(204, None)

    def _ep_park(self, w: Worker, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"], ("waiting_help",))
        job.lease_owner = job.lease_boot = None
        return self._json(204, None)

    def _ep_canary_page(self, w: Worker, params, request, body) -> httpx.Response:
        self._boot(w, request)
        return httpx.Response(200, headers={"Content-Type": "text/html", "Cache-Control": "no-store"},
                              content=CANARY_HTML.encode())
