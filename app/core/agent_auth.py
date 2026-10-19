"""Worker authentication and fencing for /agent-worker/v1 (SPEC.md 3.1).

The Windows worker never holds a user JWT. It enrolls once with a single-use
code an IT Admin minted, receives a `bdrw_` token, and sends it as a bearer
on every call. BDR stores only sha256(token), looks the worker up by that
hash and compares in constant time. A user JWT is never accepted here, and
`get_current_user` never accepts a `bdrw_` token (it is not a JWT).

Layers, in order, for every authenticated call:
  1. token  -> the worker row (401 unauthorized / revoked; pending_approval
               unless the route admits pending workers: /hello and /config);
  2. IP     -> the optional allowlist (403 ip_not_allowed), read through
               `client_ip`, which trusts X-Forwarded-For only when
               TRUST_FORWARDED_FOR is set (Railway);
  3. rate   -> 600 per minute per worker, in memory per process (429);
  4. fence  -> job-scoped calls carry X-Agent-Lease "<job_id>:<epoch>" and
               X-Agent-Boot; the RPCs check owner, epoch, boot and status in
               one transaction and raise an ErrorCode the API maps 1:1.

Everything here is plain `def`: the Supabase SDK is synchronous, so FastAPI
runs these in its threadpool (never call it inside `async def`).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, HTTPException, Path, Request
from postgrest.exceptions import APIError

from app.core import ratelimit, supabase_client
from app.core.config import get_settings
from app.services.browser_agents.protocol import (
    ERROR_HTTP_STATUS,
    HEADER_ERROR,
    WORKER_TOKEN_PREFIX,
    WORKER_TOKEN_RE,
    ErrorCode,
)

logger = logging.getLogger(__name__)

_ENROLL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"   # RFC 4648 base32, no padding
ENROLL_CODE_LENGTH = 20
# How much of a token is kept in clear (token_prefix) so a revoked worker,
# whose hash the revoke RPC nulls, can still be told "revoked" (and stop its
# service) instead of a generic 401. 8 random chars: identifies, never authenticates.
TOKEN_PREFIX_LENGTH = len(WORKER_TOKEN_PREFIX) + 8
WORKER_COLUMNS = (
    "id, name, status, token_hash, token_prefix, draining, max_slots, allowed_types, "
    "allowed_ips, boot_id, version, capabilities, bad_versions, last_seen_at"
)
RATE_LIMIT_SCOPE = "agent_worker"
_ERROR_CODE_VALUES = frozenset(c.value for c in ErrorCode)


# ── Errors ─────────────────────────────────────────────────────────────────


def agent_error(code: ErrorCode | str, detail: str | None = None,
                headers: dict[str, str] | None = None) -> HTTPException:
    """The one error shape of the worker API: the status from
    ERROR_HTTP_STATUS, the code in X-Error-Code, never cacheable. `detail`
    is a short fixed text, never a secret, never upstream error text."""
    code = ErrorCode(code)
    return HTTPException(
        status_code=ERROR_HTTP_STATUS[code],
        detail=detail or code.value,
        headers={HEADER_ERROR: code.value, "Cache-Control": "no-store", **(headers or {})},
    )


def rpc(name: str, params: dict[str, Any]) -> Any:
    """Call a 0157 RPC. The functions raise with the message set to an
    ErrorCode value (stale_boot, lease_lost, ...); those map straight to the
    worker error. Anything else propagates (a real server fault, 500)."""
    try:
        return supabase_client.get_supabase().rpc(name, params).execute().data
    except APIError as exc:
        message = (getattr(exc, "message", None) or "").strip()
        if message in _ERROR_CODE_VALUES:
            raise agent_error(message) from None
        raise


# ── Secrets: tokens and enrollment codes ─────────────────────────────────────


def sha256_bytes(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def bytea_literal(data: bytes) -> str:
    """How PostgREST takes (and returns) a bytea value: '\\x' + hex."""
    return "\\x" + data.hex()


def bytea_value(value: Any) -> bytes | None:
    """A bytea column as PostgREST returns it ('\\x...'), as raw bytes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    text = str(value)
    if text.startswith("\\x"):
        try:
            return bytes.fromhex(text[2:])
        except ValueError:
            return None
    return None


def new_worker_token() -> tuple[str, bytes, str]:
    """(token, sha256 hash, clear prefix). The token is shown once, to the
    enrolling worker, and never stored."""
    token = WORKER_TOKEN_PREFIX + secrets.token_urlsafe(32)
    assert WORKER_TOKEN_RE.match(token), "token_urlsafe(32) must give 43 chars"
    return token, sha256_bytes(token), token[:TOKEN_PREFIX_LENGTH]


def normalize_enroll_code(raw: str) -> str:
    """What an IT Admin may have pasted ("abcd-efgh ...") as the canonical
    20-char upper-case code."""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def new_enroll_code() -> tuple[str, bytes]:
    """(code, sha256 hash). 20 base32 chars = 100 bits; single use, 30 min."""
    code = "".join(secrets.choice(_ENROLL_ALPHABET) for _ in range(ENROLL_CODE_LENGTH))
    return code, sha256_bytes(code)


# ── Client IP and the allowlist ───────────────────────────────────────────────


def client_ip(request: Request) -> str | None:
    """The caller's IP. Behind Railway's edge the socket peer is the proxy,
    so with TRUST_FORWARDED_FOR the RIGHTMOST X-Forwarded-For entry (the one
    the trusted proxy appended) is used; entries to its left are client
    supplied and never trusted. Without the setting: the socket peer."""
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        entries = [part.strip() for part in forwarded.split(",") if part.strip()]
        if entries:
            candidate = entries[-1]
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                return None
    peer = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(peer)) if peer else None
    except ValueError:
        return None


def ip_allowed(ip: str | None, allowed: list[str] | None) -> bool:
    """Empty allowlist = any IP (O5: optional, off by default). Otherwise
    the IP must fall inside one of the CIDRs; an unknown IP is refused."""
    if not allowed:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in allowed:
        try:
            if addr in ipaddress.ip_network(str(cidr), strict=False):
                return True
        except ValueError:
            continue
    return False


# ── The authenticated worker ──────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentWorker:
    id: str
    name: str
    status: str
    boot_id: str | None
    draining: bool
    max_slots: int
    allowed_types: tuple[str, ...]
    allowed_ips: tuple[str, ...]
    version: str | None
    capabilities: dict[str, Any] = field(default_factory=dict)
    ip: str | None = None


def _bearer(authorization: str) -> str:
    if not authorization.lower().startswith("bearer "):
        raise agent_error(ErrorCode.UNAUTHORIZED)
    token = authorization.split(" ", 1)[1].strip()
    # Shape first: a user JWT (or anything else) never reaches the lookup.
    if not WORKER_TOKEN_RE.match(token):
        raise agent_error(ErrorCode.UNAUTHORIZED)
    return token


def _lookup(token: str) -> dict[str, Any]:
    digest = sha256_bytes(token)
    sb = supabase_client.get_supabase()
    rows = (
        sb.table("browser_agent_workers").select(WORKER_COLUMNS)
        .eq("token_hash", bytea_literal(digest)).limit(1).execute()
    ).data or []
    if rows and hmac.compare_digest(bytea_value(rows[0].get("token_hash")) or b"", digest):
        return rows[0]
    # The revoke RPC nulls token_hash; the clear prefix still says "revoked"
    # so the worker stops its service instead of retrying forever.
    revoked = (
        sb.table("browser_agent_workers").select("id")
        .eq("token_prefix", token[:TOKEN_PREFIX_LENGTH]).eq("status", "revoked").limit(1).execute()
    ).data or []
    raise agent_error(ErrorCode.REVOKED if revoked else ErrorCode.UNAUTHORIZED)


def authenticate(request: Request, authorization: str, *, allow_pending: bool) -> AgentWorker:
    """Token, status, IP allowlist and rate limit. See the module docstring."""
    row = _lookup(_bearer(authorization))
    status_ = row.get("status")
    if status_ == "revoked":
        raise agent_error(ErrorCode.REVOKED)
    if status_ == "pending" and not allow_pending:
        raise agent_error(ErrorCode.PENDING_APPROVAL)
    if status_ not in ("active", "pending"):
        raise agent_error(ErrorCode.UNAUTHORIZED)
    ip = client_ip(request)
    allowed_ips = tuple(str(c) for c in (row.get("allowed_ips") or []))
    if not ip_allowed(ip, list(allowed_ips)):
        logger.warning("agent worker %s refused from an address outside its allowlist", row.get("id"))
        raise agent_error(ErrorCode.IP_NOT_ALLOWED)
    try:
        ratelimit._check(RATE_LIMIT_SCOPE, str(row["id"]),
                         get_settings().browser_agent_rate_limit_per_minute)
    except HTTPException as exc:
        raise agent_error(ErrorCode.RATE_LIMITED, headers=dict(exc.headers or {})) from None
    return AgentWorker(
        id=str(row["id"]),
        name=row.get("name") or "",
        status=status_,
        boot_id=str(row["boot_id"]) if row.get("boot_id") else None,
        draining=bool(row.get("draining")),
        max_slots=int(row.get("max_slots") or 1),
        allowed_types=tuple(row.get("allowed_types") or ()),
        allowed_ips=allowed_ips,
        version=row.get("version"),
        capabilities=row.get("capabilities") or {},
        ip=ip,
    )


def require_agent_worker(request: Request, authorization: str = Header(default="")) -> AgentWorker:
    """An ACTIVE worker. Every route except /enroll, /hello and /config."""
    return authenticate(request, authorization, allow_pending=False)


def require_agent_worker_or_pending(
    request: Request, authorization: str = Header(default="")
) -> AgentWorker:
    """An active or a pending (enrolled, not yet approved) worker: /hello
    and /config only (protocol.PENDING_ALLOWED_ENDPOINTS)."""
    return authenticate(request, authorization, allow_pending=True)


# ── Boot and lease fencing ────────────────────────────────────────────────────


def parse_boot(value: str | None, *, required: bool) -> str | None:
    """X-Agent-Boot as a canonical uuid string."""
    if not value:
        if required:
            raise agent_error(ErrorCode.INVALID_REQUEST, "X-Agent-Boot is required")
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        raise agent_error(ErrorCode.INVALID_REQUEST, "X-Agent-Boot is not a uuid") from None


def check_boot(worker: AgentWorker, boot_id: str) -> None:
    """A call from any boot but the one the last /hello registered is a
    zombie or a duplicate process: stale_boot (the worker exits code 3)."""
    if worker.boot_id is None or worker.boot_id != boot_id:
        raise agent_error(ErrorCode.STALE_BOOT)


@dataclass(frozen=True)
class AgentLease:
    """What a job-scoped call proves: this worker, this boot, this epoch.
    The RPC re-checks all of it inside its own transaction (the real fence);
    the API checks only the cheap, row-free parts up front."""
    worker: AgentWorker
    job_id: str
    epoch: int
    boot_id: str

    def params(self) -> dict[str, Any]:
        return {"p_job": self.job_id, "p_worker": self.worker.id,
                "p_boot": self.boot_id, "p_epoch": self.epoch}


def parse_lease(value: str | None, job_id: str) -> int:
    """X-Agent-Lease "<job_id>:<epoch>" for this path's job; the epoch."""
    raw = (value or "").strip()
    head, sep, tail = raw.rpartition(":")
    if not sep or not tail.isdigit():
        raise agent_error(ErrorCode.INVALID_REQUEST, "X-Agent-Lease must be <job_id>:<epoch>")
    try:
        lease_job = str(uuid.UUID(head))
    except ValueError:
        raise agent_error(ErrorCode.INVALID_REQUEST, "X-Agent-Lease must be <job_id>:<epoch>") from None
    if lease_job != job_id:
        raise agent_error(ErrorCode.INVALID_REQUEST, "X-Agent-Lease names another job")
    return int(tail)


def require_agent_lease(
    request: Request,
    job_id: uuid.UUID = Path(...),
    authorization: str = Header(default=""),
    x_agent_lease: str = Header(default=""),
    x_agent_boot: str = Header(default=""),
) -> AgentLease:
    """Dependency for every /jobs/{job_id}/... route. Boot mismatches are
    left to the RPC on purpose: an old-epoch late step from a previous boot
    is still accepted when a claim step proves that boot held the epoch."""
    worker = authenticate(request, authorization, allow_pending=False)
    jid = str(job_id)
    epoch = parse_lease(x_agent_lease, jid)
    boot = parse_boot(x_agent_boot, required=True)
    return AgentLease(worker=worker, job_id=jid, epoch=epoch, boot_id=boot)
