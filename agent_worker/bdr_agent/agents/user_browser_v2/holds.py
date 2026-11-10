"""Final-action and site holds for user_browser v2 (plan 6.5, 6.6, 5.4; WI-9).

Signed worker code. The guard (guard.py) decides WHAT is held; this module
runs the hold itself:

  1. Build the hold record: `request_hashes` (url_sha256 over the full URL with
     its query, body_sha256 over the exact body bytes) and `summarize_body`
     (form field names; a value is shown only when it equals a string the
     model typed in this job, then the job redactor runs; everything else is
     `[hidden]` whatever the field is named).
  2. `HoldBroker.hold_final` / `hold_site`: POST the hold
     (USER_V2_ENDPOINTS["user_holds"] / ["user_site_hold"]), then long-poll
     ["user_hold_release"] until the owner decides or the policy hold timeout
     passes. While any hold is open `HoldBroker.idle` is clear, so the runner
     does not ask the model for the next step.
  3. On release, `HoldTokenVerifier.verify` checks the Ed25519 signature over
     `canonical_hold_token_bytes` with the hold public key compiled into this
     release, and that job, lease epoch, hold id, method, origin, URL hash and
     body hash all match the request the guard is still holding, that `exp`
     has not passed, and that the hold id was never used before in this job.
     Only then does the guard continue exactly that request.

The transport is injected (`HoldTransport`); `EndpointHoldTransport` binds the
three v2 endpoints to any lease-fenced `send(method, path, json, query)`
coroutine, which WI-10's client provides. Tests use fakes.

Click holds (6.2 item 4) use method "CLICK", the URL of the page (or the
anchor's href), and body_sha256 over `click_descriptor_bytes`; the server
signs whatever the hold record says, the worker re-derives it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qsl
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ...protocol import (
    USER_HOLD_FIELDS_MAX,
    USER_HOLD_RELEASE_POLL_SECONDS,
    USER_HOLD_TOKEN_TTL_SECONDS,
    USER_V2_ENDPOINTS,
    UserHoldField,
    UserHoldRelease,
    UserHoldRequest,
    UserHoldToken,
    UserSiteHoldRequest,
    canonical_hold_token_bytes,
)
from .exfil import TypedStrings

log = logging.getLogger("bdr_agent.user_browser_v2.holds")

CLICK_METHOD = "CLICK"
# Clock skew allowed when checking exp, and the longest exp ahead of now a
# token may carry (the server issues exp = now + 120 s).
TOKEN_SKEW_SECONDS = 30
# The worker waits this long past the policy hold timeout for the server to
# say "expired" before it gives up on its own.
HOLD_GRACE_SECONDS = 30
FIELD_NAME_MAX = 200
FIELD_VALUE_MAX = 500

# Hold signing public keys compiled into the signed release (plan 6.5 step 4,
# 10.2): key_id -> base64 of the 32 raw Ed25519 public key bytes (or PEM).
# Filled by the owner at release time from BROWSER_HOLD_SIGNING_KEY's public
# half; empty means every release token is refused (fail closed).
HOLD_SIGNING_PUBLIC_KEYS: dict[str, str] = {"h1": "DtnOxEC6W1f7Nz827YwU9HdE2bQfBPEt5E9Ni+Vj2sc="}


# Hashes and summaries --------------------------------------------------------------


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def request_hashes(url: str, body: bytes | None) -> tuple[str, str]:
    """(url_sha256, body_sha256): the full URL including its query, and the
    exact body bytes (empty for a GET)."""
    return sha256_hex(url), sha256_hex(body or b"")


def click_descriptor_bytes(*, action: str, tag: str, label: str, href: str | None) -> bytes:
    """What a click hold's body_sha256 covers: the element as the card shows it."""
    data = {"action": action, "tag": tag, "label": label, "href": href or ""}
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _field(name: str, value: Any, typed: TypedStrings, redact: Callable[[str], str] | None) -> UserHoldField:
    name = str(name)[:FIELD_NAME_MAX]
    if redact is not None:
        name = redact(name)[:FIELD_NAME_MAX]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and typed.is_typed(value):
        shown = value[:FIELD_VALUE_MAX]
        if redact is not None:
            scrubbed = redact(shown)
            if scrubbed != shown:   # a secret is never shown, even if typed
                return UserHoldField(name=name)
        return UserHoldField(name=name, value=shown, hidden=False)
    return UserHoldField(name=name)


def _flatten_json(data: Any, prefix: str, out: list[tuple[str, Any]], depth: int = 0) -> None:
    if len(out) >= USER_HOLD_FIELDS_MAX or depth > 8:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            _flatten_json(value, f"{prefix}.{key}" if prefix else str(key), out, depth + 1)
    elif isinstance(data, list):
        for i, value in enumerate(data[:50]):
            _flatten_json(value, f"{prefix}[{i}]", out, depth + 1)
    else:
        out.append((prefix or "(body)", data))


def _multipart_fields(body: bytes, content_type: str) -> list[tuple[str, Any]]:
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"')
    if not boundary:
        return [("(body)", None)]
    out: list[tuple[str, Any]] = []
    for chunk in body.split(b"--" + boundary.encode("latin-1")):
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = head.decode("latin-1", errors="ignore")
        name = None
        is_file = "filename=" in headers
        for piece in headers.replace("\r\n", ";").split(";"):
            piece = piece.strip()
            if piece.lower().startswith("name="):
                name = piece.split("=", 1)[1].strip().strip('"')
        if name is None:
            continue
        value = None if is_file else payload.rstrip(b"\r\n").decode("utf-8", errors="ignore")
        out.append((name + (" (file)" if is_file else ""), value))
        if len(out) >= USER_HOLD_FIELDS_MAX:
            break
    return out


def summarize_body(body: bytes | None, content_type: str | None, typed: TypedStrings, *,
                   redact: Callable[[str], str] | None = None) -> list[UserHoldField]:
    """Field names of a held body; values only when the model typed them (6.5)."""
    if not body:
        return []
    ctype = (content_type or "").lower()
    pairs: list[tuple[str, Any]]
    if "application/x-www-form-urlencoded" in ctype:
        try:
            pairs = parse_qsl(body.decode("utf-8", errors="ignore"), keep_blank_values=True,
                              max_num_fields=USER_HOLD_FIELDS_MAX * 2)
        except ValueError:
            pairs = [("(body)", None)]
    elif "multipart/form-data" in ctype:
        pairs = _multipart_fields(body, content_type or "")
    elif "json" in ctype or body.lstrip()[:1] in (b"{", b"["):
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            pairs = [("(body)", None)]
        else:
            pairs = []
            _flatten_json(data, "", pairs)
    else:
        pairs = [("(body)", None)]
    return [_field(name, value, typed, redact) for name, value in pairs[:USER_HOLD_FIELDS_MAX]]


# Token verification ----------------------------------------------------------------


class TokenRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def load_hold_public_keys(mapping: Mapping[str, str | bytes]) -> dict[str, Ed25519PublicKey]:
    """key_id -> Ed25519PublicKey from base64 raw 32 bytes, raw bytes or PEM."""
    out: dict[str, Ed25519PublicKey] = {}
    for key_id, value in mapping.items():
        raw = value.encode("ascii") if isinstance(value, str) else bytes(value)
        if raw.lstrip().startswith(b"-----BEGIN"):
            key = serialization.load_pem_public_key(raw)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError(f"hold key {key_id} is not an Ed25519 key")
            out[key_id] = key
            continue
        if len(raw) != 32:
            try:
                raw = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"hold key {key_id} is not base64") from None
        if len(raw) != 32:
            raise ValueError(f"hold key {key_id} must be 32 raw Ed25519 bytes")
        out[key_id] = Ed25519PublicKey.from_public_bytes(raw)
    return out


@dataclass(frozen=True)
class HeldTarget:
    """The request the guard is still holding: what a token must name."""
    job_id: UUID
    lease_epoch: int
    hold_id: UUID
    method: str
    origin: str
    url_sha256: str
    body_sha256: str


class HoldTokenVerifier:
    """Verifies release tokens for one job and makes each hold id single use."""

    def __init__(self, public_keys: Mapping[str, Ed25519PublicKey] | None = None, *,
                 clock: Callable[[], float] = time.time) -> None:
        if public_keys is None:
            public_keys = load_hold_public_keys(HOLD_SIGNING_PUBLIC_KEYS)
        self._keys = dict(public_keys)
        self._clock = clock
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def verify(self, token: UserHoldToken, target: HeldTarget) -> None:
        """Raise TokenRefused unless `token` releases exactly `target` now.
        On success the hold id is spent."""
        key = self._keys.get(token.key_id)
        if key is None:
            raise TokenRefused("unknown_key")
        try:
            signature = base64.b64decode(token.signature, validate=True)
        except (binascii.Error, ValueError):
            raise TokenRefused("bad_signature") from None
        try:
            key.verify(signature, canonical_hold_token_bytes(token))
        except InvalidSignature:
            raise TokenRefused("bad_signature") from None
        checks = (
            ("job", str(token.job_id).lower(), str(target.job_id).lower()),
            ("epoch", str(int(token.lease_epoch)), str(int(target.lease_epoch))),
            ("hold", str(token.hold_id).lower(), str(target.hold_id).lower()),
            ("method", token.method, target.method.upper()),
            ("origin", token.origin, target.origin),
            ("url", token.url_sha256, target.url_sha256),
            ("body", token.body_sha256, target.body_sha256),
        )
        for name, got, want in checks:
            if not hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8")):
                raise TokenRefused(f"{name}_mismatch")
        now = self._clock()
        if token.exp <= now:
            raise TokenRefused("expired")
        if token.exp > now + USER_HOLD_TOKEN_TTL_SECONDS + TOKEN_SKEW_SECONDS:
            raise TokenRefused("exp_too_far")
        hold = str(target.hold_id).lower()
        with self._lock:
            if hold in self._used:
                raise TokenRefused("already_used")
            self._used.add(hold)

    def used(self, hold_id: UUID | str) -> bool:
        with self._lock:
            return str(hold_id).lower() in self._used


# Transport ---------------------------------------------------------------------------


class HoldTransport(Protocol):
    async def open_hold(self, req: UserHoldRequest) -> UserHoldRelease: ...

    async def poll_release(self, hold_id: UUID) -> UserHoldRelease: ...

    async def open_site_hold(self, req: UserSiteHoldRequest) -> UserHoldRelease: ...


Send = Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None], Awaitable[dict[str, Any]]]


class EndpointHoldTransport:
    """Binds USER_V2_ENDPOINTS to a lease-fenced JSON `send(method, path,
    json_body, query) -> dict` (paths are relative to /agent-worker/v1)."""

    def __init__(self, send: Send, job_id: UUID | str) -> None:
        self._send = send
        self._job_id = str(job_id)

    async def _call(self, key: str, body: Any = None, **path: str) -> UserHoldRelease:
        method, template, _req, _resp, _status = USER_V2_ENDPOINTS[key]
        url = template.format(job_id=self._job_id, **path)
        payload = body.model_dump(mode="json", exclude_none=True) if body is not None else None
        data = await self._send(method, url, payload, None)
        return UserHoldRelease.model_validate(data)

    async def open_hold(self, req: UserHoldRequest) -> UserHoldRelease:
        return await self._call("user_holds", req)

    async def poll_release(self, hold_id: UUID) -> UserHoldRelease:
        return await self._call("user_hold_release", hold_id=str(hold_id))

    async def open_site_hold(self, req: UserSiteHoldRequest) -> UserHoldRelease:
        return await self._call("user_site_hold", req)


# Broker ------------------------------------------------------------------------------


@dataclass
class HoldResult:
    """Outcome of one hold. `released` is True only for a verified token (final
    action) or an owner release (site). `notice` is the model-facing line."""
    status: str                       # released, declined, expired, aborted, refused, error
    hold_id: UUID | None = None
    label: str | None = None
    site_release: str | None = None   # full or site_only (site holds)
    policy_version: int | None = None
    reason: str | None = None         # token refusal or transport reason
    notice: str | None = None

    @property
    def released(self) -> bool:
        return self.status == "released"


@dataclass
class HoldBroker:
    """Runs holds for one job. `lease_epoch` is read at verification time, so a
    re-leased job never accepts a token minted for an older epoch."""
    transport: HoldTransport
    verifier: HoldTokenVerifier
    job_id: UUID
    lease_epoch: Callable[[], int]
    timeout_seconds: float = 600.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    on_change: Callable[[int], Any] | None = None
    open_count: int = 0
    decided: int = 0
    _idle: asyncio.Event | None = field(default=None, repr=False)

    @property
    def idle(self) -> asyncio.Event:
        if self._idle is None:
            self._idle = asyncio.Event()
            if self.open_count == 0:
                self._idle.set()
        return self._idle

    def _enter(self) -> None:
        self.open_count += 1
        self.idle.clear()
        self._notify()

    def _leave(self) -> None:
        self.open_count = max(0, self.open_count - 1)
        if self.open_count == 0:
            self.idle.set()
        self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self.open_count)
            except Exception as exc:   # a callback must never break a hold
                log.debug("hold on_change failed: %s", exc)

    async def wait_idle(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self.idle.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _await_decision(self, first: UserHoldRelease) -> UserHoldRelease | None:
        deadline = self.clock() + self.timeout_seconds + HOLD_GRACE_SECONDS
        current = first
        failures = 0
        while current.status == "pending":
            if self.clock() >= deadline:
                return None
            try:
                current = await self.transport.poll_release(first.hold_id)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log.info("hold release poll failed (%s): %s", failures, type(exc).__name__)
                await self.sleep(min(15.0, 1.0 * 2 ** min(failures, 4)))
                continue
            if current.hold_id != first.hold_id:
                raise TokenRefused("hold_mismatch")
            if current.status == "pending":
                # The server long-polls (up to 25 s); a quick pending answer
                # says when to ask again.
                wait = current.retry_after if current.retry_after is not None else 1
                await self.sleep(max(0.2, min(float(wait), float(USER_HOLD_RELEASE_POLL_SECONDS))))
        return current

    async def hold_final(self, req: UserHoldRequest, *, label: str) -> HoldResult:
        """Hold a final action until the owner decides. `label` names the action
        in the model notice."""
        self._enter()
        try:
            try:
                first = await self.transport.open_hold(req)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("hold open failed: %s", type(exc).__name__)
                return HoldResult("error", reason="hold_open_failed",
                                  notice=f"The action was not sent: the approval request failed ({label}).")
            if first.kind != "final_action":
                return HoldResult("error", hold_id=first.hold_id, reason="wrong_kind",
                                  notice=f"The action was not sent ({label}).")
            try:
                final = await self._await_decision(first)
            except TokenRefused as exc:
                return HoldResult("refused", hold_id=first.hold_id, reason=exc.reason,
                                  notice=f"The action was not sent: the approval did not match ({label}).")
            if final is None:
                return HoldResult("expired", hold_id=first.hold_id, reason="worker_timeout",
                                  notice=f"The owner did not answer in time: {label}.")
            self.decided += 1
            return self._final_result(req, final, label)
        finally:
            self._leave()

    def _final_result(self, req: UserHoldRequest, rel: UserHoldRelease, label: str) -> HoldResult:
        shown = rel.label or label
        if rel.status == "released":
            if rel.token is None:
                return HoldResult("refused", hold_id=rel.hold_id, reason="no_token",
                                  notice=f"The action was not sent: the approval did not match ({shown}).")
            target = HeldTarget(job_id=self.job_id, lease_epoch=int(self.lease_epoch()), hold_id=rel.hold_id,
                                method=req.method, origin=req.origin, url_sha256=req.url_sha256,
                                body_sha256=req.body_sha256)
            try:
                self.verifier.verify(rel.token, target)
            except TokenRefused as exc:
                log.warning("hold token refused: %s", exc.reason)
                return HoldResult("refused", hold_id=rel.hold_id, reason=exc.reason,
                                  notice=f"The action was not sent: the approval did not match ({shown}).")
            return HoldResult("released", hold_id=rel.hold_id, label=shown)
        if rel.status == "declined":
            return HoldResult("declined", hold_id=rel.hold_id, label=shown, notice=f"The owner declined: {shown}.")
        if rel.status == "expired":
            return HoldResult("expired", hold_id=rel.hold_id, label=shown,
                              notice=f"The owner did not answer in time: {shown}.")
        return HoldResult("aborted", hold_id=rel.hold_id, label=shown, notice=f"The action was cancelled: {shown}.")

    async def hold_site(self, req: UserSiteHoldRequest, *, label: str) -> HoldResult:
        """Hold a top-level navigation to a site outside the task scope (5.4)."""
        self._enter()
        try:
            try:
                first = await self.transport.open_site_hold(req)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("site hold open failed: %s", type(exc).__name__)
                return HoldResult("error", reason="hold_open_failed",
                                  notice=f"Opening {label} needs the owner's approval, and the request failed.")
            if first.kind != "site":
                return HoldResult("error", hold_id=first.hold_id, reason="wrong_kind",
                                  notice=f"{label} was not opened.")
            try:
                final = await self._await_decision(first)
            except TokenRefused as exc:
                return HoldResult("refused", hold_id=first.hold_id, reason=exc.reason,
                                  notice=f"{label} was not opened.")
            if final is None:
                return HoldResult("expired", hold_id=first.hold_id, reason="worker_timeout",
                                  notice=f"The owner did not approve opening {label} in time.")
            self.decided += 1
            if final.kind != "site" or final.token is not None:
                return HoldResult("refused", hold_id=final.hold_id, reason="wrong_kind",
                                  notice=f"{label} was not opened.")
            if final.status == "released":
                return HoldResult("released", hold_id=final.hold_id, label=final.label or label,
                                  site_release=final.site_release, policy_version=final.policy_version)
            if final.status == "declined":
                return HoldResult("declined", hold_id=final.hold_id,
                                  notice=f"The owner declined opening {label}.")
            if final.status == "expired":
                return HoldResult("expired", hold_id=final.hold_id,
                                  notice=f"The owner did not approve opening {label} in time.")
            return HoldResult("aborted", hold_id=final.hold_id, notice=f"Opening {label} was cancelled.")
        finally:
            self._leave()
