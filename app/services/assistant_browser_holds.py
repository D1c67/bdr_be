"""Final-action holds and release tokens (docs/ASSISTANT_BROWSER_V2_PLAN.md 5.4, 6.5, 6.6).

Owner: WI-6. Signatures of sign_release, verify_release, open_hold, decide,
release_status and consume are binding for WI-4 (decision route) and WI-13
(cards).

Tokens are Ed25519 signatures by BROWSER_HOLD_SIGNING_KEY (the 32 byte
private seed, base64, backend env only), valid 2 minutes, single use. The
only canonical form is protocol.canonical_hold_token_bytes over a
protocol.UserHoldToken (the signed worker verifies exactly those bytes);
this module never builds its own encoding. Only final_action holds carry a
token; a site hold is released by scope extension (5.4) and carries none.

Delivery: hold_decide stores the token; the worker long-polls
GET /jobs/{job_id}/user/holds/{hold_id}/release, which answers the token
once and marks the hold consumed in the same call (deliver once: a lost
answer makes the worker see `aborted`, and the model asks again). The
worker also keeps used hold ids, so the token is single use twice over.

SQL: assistant_browser_hold_open, _hold_decide, _hold_release_get,
_hold_consume (0192 A4.2).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from app.services.browser_agents.protocol import UserHoldToken, canonical_hold_token_bytes

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 120
Decision = Literal["approve", "approve_site_only", "decline"]
LONG_POLL_MAX_SECONDS = 25
LONG_POLL_STEP_SECONDS = 2.0
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
# Release answers the RPC can give, mapped to UserHoldRelease.status.
_DONE = {"declined": "declined", "expired": "expired", "aborted": "aborted", "consumed": "aborted"}


class HoldRefused(ValueError):
    """A refused hold call. `code` is the RPC code (hold_aborted,
    hold_expired, hold_changed, not_found, invalid, forbidden, help_busy,
    signing_unavailable)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReleaseClaims:
    job_id: str
    lease_epoch: int
    hold_id: str
    method: str
    origin: str
    url_sha256: str
    body_sha256: str
    exp: datetime


def canonical_claims(claims: ReleaseClaims) -> bytes:
    """The exact bytes signed: protocol.canonical_hold_token_bytes over the
    claims (ids lowercased, exp as whole epoch seconds). Delegates; never a
    second encoder."""
    return canonical_hold_token_bytes({
        "job_id": claims.job_id, "lease_epoch": claims.lease_epoch, "hold_id": claims.hold_id,
        "method": claims.method, "origin": claims.origin, "url_sha256": claims.url_sha256,
        "body_sha256": claims.body_sha256, "exp": int(claims.exp.timestamp()),
    })


# ── Keys ─────────────────────────────────────────────────────────────────────


def _seed(raw: str) -> bytes:
    text = (raw or "").strip()
    if not text:
        raise HoldRefused("signing_unavailable")
    try:
        data = base64.b64decode((text + "=" * (-len(text) % 4)).replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        raise HoldRefused("signing_unavailable") from None
    if len(data) != 32:
        raise HoldRefused("signing_unavailable")
    return data


def _private_key():
    """(Ed25519PrivateKey, key_id) from settings. Never logged or returned."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from app.core.config import get_settings

    settings = get_settings()
    key = Ed25519PrivateKey.from_private_bytes(_seed(getattr(settings, "browser_hold_signing_key", "")))
    return key, str(getattr(settings, "browser_hold_signing_key_id", "") or "h1")


def public_key_bytes() -> bytes:
    """The raw 32 byte public key the signed worker release config carries
    (WI-20 compiles it in; tests verify with it)."""
    from cryptography.hazmat.primitives import serialization

    key, _ = _private_key()
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign_release(claims: ReleaseClaims) -> UserHoldToken:
    """The signed token (protocol.UserHoldToken: claims, key_id, base64
    signature over canonical_claims). Stored by hold_decide as its compact
    JSON, returned in UserHoldRelease.token."""
    if claims.exp.tzinfo is None:
        raise HoldRefused("invalid")
    key, key_id = _private_key()
    signature = base64.b64encode(key.sign(canonical_claims(claims))).decode("ascii")
    return UserHoldToken(job_id=claims.job_id, lease_epoch=int(claims.lease_epoch), hold_id=claims.hold_id,
                         method=claims.method, origin=claims.origin, url_sha256=claims.url_sha256,
                         body_sha256=claims.body_sha256, exp=int(claims.exp.timestamp()), key_id=key_id,
                         signature=signature)


def verify_release(token: UserHoldToken, public_key: bytes, *, now: datetime | None = None) -> ReleaseClaims:
    """Server-side mirror of the worker check (tests). Raises ValueError:
    a bad signature, a token whose exp has passed, or one that lives longer
    than the TTL allows."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(bytes(public_key))
        key.verify(base64.b64decode(token.signature, validate=True), canonical_hold_token_bytes(token))
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        raise ValueError("bad_signature") from None
    moment = int((now or datetime.now(timezone.utc)).timestamp())
    if token.exp <= moment:
        raise ValueError("expired")
    if token.exp > moment + TOKEN_TTL_SECONDS + 5:
        raise ValueError("exp_too_far")
    return ReleaseClaims(job_id=str(token.job_id), lease_epoch=int(token.lease_epoch), hold_id=str(token.hold_id),
                         method=token.method, origin=token.origin, url_sha256=token.url_sha256,
                         body_sha256=token.body_sha256,
                         exp=datetime.fromtimestamp(token.exp, tz=timezone.utc))


# ── Records ──────────────────────────────────────────────────────────────────


def _record_id(hold_id: str, tag: str) -> str:
    return str(uuid.uuid5(uuid.UUID(hold_id), f"hold-record:{tag}"))


def _summary_record(job: dict, hold_id: str, payload: dict) -> dict:
    """The encrypted hold summary (kind request). Full URL with its query
    lives only here and on the owner's card (6.5 step 1). The job's
    SecretSet and consumed-code hashes still run over it; URLs are kept."""
    from app.services import assistant_browser_records as records
    from app.services import assistant_browser_runtime as runtime

    clean = runtime.sanitize(job, {"hold": payload}, trace=False)
    return records.pack_record(job["principal_user_id"], job["id"], "request", clean,
                               record_id=_record_id(hold_id, "summary"), seq=0)


def _screenshot_record(job: dict, hold_id: str, artifact_id: str | None) -> dict | None:
    """A copy of the screenshot the worker uploaded for this hold, as the
    hold's own encrypted record. Never on a login origin or in operator mode
    (6.5 step 1, 9 step 5); an unreadable upload gives no screenshot."""
    from app.assistant import repo
    from app.services import assistant_browser_records as records
    from app.services import assistant_browser_runtime as runtime

    if not artifact_id or job.get("control_mode") == "operator" or runtime.login_open(job):
        return None
    owner = str(job["principal_user_id"])
    try:
        row = repo.browser_record(owner, str(job["id"]), str(artifact_id))
        if not row or row.get("kind") != "screenshot":
            return None
        content = records.unpack_record(owner, str(job["id"]), row)
        # "hold" marks the copy owner-only: IT Admin inspection skips it (10.7).
        return records.pack_record(owner, job["id"], "screenshot", {**content, "hold": True},
                                   record_id=_record_id(hold_id, "screenshot"), seq=0)
    except Exception as exc:  # noqa: BLE001 - the card still shows method, URL and fields
        logger.info("browser hold screenshot unavailable (%s)", type(exc).__name__)
        return None


def summary(user_id: str, hold: dict) -> dict:
    """The decrypted hold summary for the owner's card (the UserHoldRequest
    or UserSiteHoldRequest fields), or {} when unreadable."""
    from app.assistant import repo
    from app.services import assistant_browser_records as records

    record_id = hold.get("summary_record_id")
    if not record_id:
        return {}
    try:
        row = repo.browser_record(user_id, str(hold["job_id"]), str(record_id))
        content = records.unpack_record(user_id, str(hold["job_id"]), row) if row else {}
    except Exception as exc:  # noqa: BLE001 - the card degrades to method and origin
        logger.info("browser hold summary unreadable (%s)", type(exc).__name__)
        return {}
    inner = content.get("hold") if isinstance(content.get("hold"), dict) else content
    return inner if isinstance(inner, dict) else {}


# ── Worker side ──────────────────────────────────────────────────────────────


def _params(job_id: str, worker_id: str, boot_id: str, epoch: int) -> dict:
    return {"p_job": str(job_id), "p_worker": str(worker_id), "p_boot": str(boot_id), "p_epoch": int(epoch)}


def _owner(job: dict) -> str:
    if (not isinstance(job, dict) or job.get("agent_type") != "user_browser" or job.get("principal_kind") != "user"
            or not job.get("principal_user_id")):
        raise HoldRefused("invalid")
    return str(job["principal_user_id"])


def _join_open(owner: str, job: dict, fields: dict) -> str | None:
    """A retried hold for the same request joins the open one (the first
    answer was lost); any other open help or hold is busy."""
    from app.assistant import repo

    for row in repo.browser_holds(owner, str(job["id"]), status="open", limit=5):
        if all(str(row.get(key) or "") == str(fields.get(key) or "")
               for key in ("method", "origin", "url_sha256", "body_sha256")):
            return str(row["id"])
    return None


def _open(job: dict, worker_id: str, boot_id: str, epoch: int, kind: str, fields: dict, summary_payload: dict,
          artifact_id: str | None) -> str:
    from app.assistant import repo

    owner = _owner(job)
    hold_id = str(uuid.uuid4())
    result = repo.browser_rpc(owner, "assistant_browser_hold_open", {
        **_params(job["id"], worker_id, boot_id, epoch), "p_hold_id": hold_id, "p_kind": kind, "p_hold": fields,
        "p_summary_record": _summary_record(job, hold_id, summary_payload),
        "p_screenshot_record": _screenshot_record(job, hold_id, artifact_id) if kind == "final_action" else None,
    })
    if result.get("ok") is True and isinstance(result.get("hold"), dict):
        return str(result["hold"]["id"])
    code = str(result.get("code") or "invalid")
    if code == "help_busy":
        joined = _join_open(owner, job, fields)
        if joined:
            return joined
    raise HoldRefused(code)


def open_hold(job: dict[str, Any], worker_id: str, boot_id: str, epoch: int, body: dict[str, Any]) -> dict[str, Any]:
    """Worker route POST /user/holds: encrypt the summary and screenshot,
    call hold_open, answer UserHoldRelease (pending). The chat card and the
    Teams alert come from the outbox row hold_open writes (WI-13)."""
    from app.services.browser_agents import protocol

    req = protocol.UserHoldRequest.model_validate(body)
    # The token signs url_sha256 while the card shows req.url: they must be
    # the same URL (6.5 step 1: the hash of the full URL, query included).
    # protocol.py has a byte-identical worker copy, so the check lives here.
    if hashlib.sha256(req.url.encode()).hexdigest() != req.url_sha256:
        raise HoldRefused("invalid")
    fields = {"method": req.method, "origin": req.origin, "url_sha256": req.url_sha256,
              "body_sha256": req.body_sha256, "out_of_scope": req.out_of_scope,
              "label": (req.profile_label or req.element_label or f"{req.method} {req.origin}")[:200]}
    payload = req.model_dump(mode="json", exclude={"screenshot_artifact_id"})
    hold_id = _open(job, worker_id, boot_id, epoch, "final_action", fields, payload,
                    str(req.screenshot_artifact_id) if req.screenshot_artifact_id else None)
    return release_status(str(job["id"]), worker_id, boot_id, epoch, hold_id, wait_seconds=0,
                          user_id=str(job["principal_user_id"]))


def _release(hold_id: str, *, kind: str, label: str | None = None, site_release: str | None = None,
             policy_version: int | None = None) -> dict:
    from app.services.browser_agents import protocol

    return protocol.UserHoldRelease(hold_id=hold_id, kind=kind, status="released", site_release=site_release,
                                    policy_version=policy_version, label=label).model_dump(mode="json")


def open_site_hold(job: dict[str, Any], worker_id: str, boot_id: str, epoch: int,
                   body: dict[str, Any]) -> dict[str, Any]:
    """Worker route POST /user/site-hold (5.4). A site the owner already
    approved (and not multi-tenant) joins the task scope at once and the
    navigation continues; otherwise a site hold parks the task until the
    owner answers the site card. A URL the registry refuses (IP literal,
    private name, userinfo) is declined without a card."""
    from app.services import assistant_browser_grants as grants
    from app.services import assistant_browser_sites as sites
    from app.services.browser_agents import protocol

    owner = _owner(job)
    req = protocol.UserSiteHoldRequest.model_validate(body)
    try:
        parsed = sites.parse_url(req.url, top_level=True)
    except sites.UrlRefused:
        return protocol.UserHoldRelease(hold_id=str(uuid.uuid4()), kind="site", status="declined",
                                        label="This address is not allowed").model_dump(mode="json")
    site, grant = grants.active_grant_for_url(owner, parsed.url)
    if site is not None and grant is not None:
        try:
            version = grants.extend_job_scope(owner, str(job["id"]), str(site["site_key"]))
            return _release(str(uuid.uuid4()), kind="site", label=str(site.get("label") or parsed.host)[:200],
                            site_release="full", policy_version=max(1, version) if version else None)
        except grants.GrantRefused as exc:
            # multi_tenant and the rest: the owner answers a card instead.
            logger.info("browser site hold: scope not extended (%s)", exc)
    target = sites.grant_target_for_site(site, url=parsed.url) if site is not None else sites.grant_target_for_url(parsed.url)
    fields = {"method": "GET", "origin": parsed.origin, "url_sha256": hashlib.sha256(req.url.encode()).hexdigest(),
              "body_sha256": EMPTY_SHA256, "out_of_scope": True, "label": f"Open {target.label}"[:200],
              "target_site_key": target.site_key}
    payload = {**req.model_dump(mode="json"), "registrable_domain": parsed.registrable_domain,
               "site_key": target.site_key, "site_label": target.label, "scope": target.scope,
               "value": target.value, "path_prefix": target.path_prefix, "encrypted": target.encrypted,
               "provenance": "page"}
    request_id = _site_request(owner, job, target.site_key, parsed.url) if grant is None else None
    if request_id:
        payload["request_id"] = request_id
    try:
        hold_id = _open(job, worker_id, boot_id, epoch, "site", fields, payload, None)
    except HoldRefused:
        if request_id:
            _cancel_request(owner, request_id)
        raise
    return release_status(str(job["id"]), worker_id, boot_id, epoch, hold_id, wait_seconds=0, user_id=owner)


def _site_request(owner: str, job: dict, site_key: str, url: str) -> str | None:
    """5.4 step 2: a site the owner never approved gets a site access card
    request bound to the job (reason site_hold), so the grant route can
    approve it, extend the scope and release the hold. A retried hold joins
    the open request for the same site."""
    from app.assistant import repo

    try:
        for row in repo.browser_requests_open(owner, job_id=str(job["id"]), kind="site_access", limit=20):
            if row.get("job_id") == str(job["id"]) and row.get("site_key") == site_key:
                return str(row["id"])
        record = {"kind": "site_access", "job_id": str(job["id"]), "site_key": site_key, "url": url[:2048],
                  "reason_code": "site_hold", "provenance": "page"}
        if job.get("assistant_conversation_id"):
            record["conversation_id"] = str(job["assistant_conversation_id"])
        result = repo.browser_request_create(owner, record)
    except Exception as exc:  # noqa: BLE001 - the hold card still shows; Settings can approve the site
        logger.warning("browser site hold: site request not created (%s)", type(exc).__name__)
        return None
    row = result.get("request") if isinstance(result, dict) and result.get("ok") is True else None
    return str(row["id"]) if isinstance(row, dict) and row.get("id") else None


def _cancel_request(owner: str, request_id: str) -> None:
    from app.assistant import repo

    try:
        repo.browser_request_decide(owner, request_id, "cancel")
    except Exception as exc:  # noqa: BLE001 - it expires on its own
        logger.info("browser site hold: request not cancelled (%s)", type(exc).__name__)


# ── Human side ───────────────────────────────────────────────────────────────


def decide(user_id: str, hold_id: str, decision: Decision) -> dict[str, Any]:
    """Human route: sign a token on approve and call hold_decide (CAS).
    Raises HoldRefused (a ValueError) with code hold_aborted, hold_expired,
    hold_changed, not_found, invalid, forbidden or signing_unavailable."""
    from app.assistant import repo

    if decision not in ("approve", "approve_site_only", "decline"):
        raise HoldRefused("invalid")
    hold = repo.browser_hold(user_id, str(hold_id))
    if not hold or str(hold.get("user_id") or user_id) != str(user_id):
        raise HoldRefused("not_found")
    token, exp = None, None
    if decision != "decline" and hold.get("kind") == "final_action":
        if hold.get("status") != "open":
            raise HoldRefused("hold_aborted" if hold.get("status") == "aborted" else "hold_changed")
        exp = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=TOKEN_TTL_SECONDS)
        signed = sign_release(ReleaseClaims(
            job_id=str(hold["job_id"]), lease_epoch=int(hold["lease_epoch"]), hold_id=str(hold["id"]),
            method=str(hold["method"]), origin=str(hold["origin"]), url_sha256=str(hold["url_sha256"]),
            body_sha256=str(hold["body_sha256"]), exp=exp))
        token = signed.model_dump_json()
    result = repo.browser_hold_decide(user_id, str(hold_id), decision, token=token, token_exp=exp)
    if result.get("ok") is not True:
        raise HoldRefused(str(result.get("code") or "hold_changed"))
    return result


# ── Release long poll and consume (worker side) ──────────────────────────────


def _answer(owner: str, job_id: str, worker_id: str, boot_id: str, epoch: int, hold_id: str,
            data: dict) -> dict | None:
    """One release_get answer as UserHoldRelease, or None while pending."""
    from app.assistant import repo
    from app.services.browser_agents import protocol

    status = data.get("status")
    kind = data.get("kind")
    if kind not in ("final_action", "site"):
        raise HoldRefused("invalid")
    if status == "pending":
        return None
    hold = repo.browser_hold(owner, hold_id) or {}
    label = hold.get("label") if isinstance(hold.get("label"), str) else None
    if status in _DONE:
        return protocol.UserHoldRelease(hold_id=hold_id, kind=kind, status=_DONE[status],
                                        label=label).model_dump(mode="json")
    if status != "approved":
        raise HoldRefused("hold_changed")
    if kind == "site":
        # The fence returns the job row with the policy_version the scope
        # extension bumped, so the worker refetches its policy (10.2).
        job = repo.browser_rpc(owner, "assistant_browser_user_fence_v2", _params(job_id, worker_id, boot_id, epoch))
        site_release = "site_only" if data.get("decision") == "approve_site_only" else "full"
        version = job.get("policy_version")
        return _release(hold_id, kind="site", label=label, site_release=site_release,
                        policy_version=int(version) if isinstance(version, int) and version >= 1 else None)
    raw = data.get("token")
    if not isinstance(raw, str) or not raw:
        # The token's exp passed before the worker asked: the owner asks again.
        return protocol.UserHoldRelease(hold_id=hold_id, kind=kind, status="expired",
                                        label=label).model_dump(mode="json")
    token = UserHoldToken.model_validate(json.loads(raw))
    if str(token.hold_id) != str(hold_id) or str(token.job_id) != str(job_id) or token.lease_epoch != int(epoch):
        raise HoldRefused("hold_changed")
    # Consume first: only the poll whose consume succeeds hands out the
    # token (deliver once). A poll that lost the race answers aborted, one
    # whose token expired in between answers expired, neither with a token.
    code = _consume_code(job_id, worker_id, boot_id, epoch, hold_id, owner)
    if code != "ok":
        status = {"already_consumed": "aborted", "token_expired": "expired"}.get(code)
        if status is None:
            raise HoldRefused(code)
        return protocol.UserHoldRelease(hold_id=hold_id, kind=kind, status=status,
                                        label=label).model_dump(mode="json")
    return protocol.UserHoldRelease(hold_id=hold_id, kind=kind, status="released", token=token,
                                    label=label).model_dump(mode="json")


def _consume_code(job_id: str, worker_id: str, boot_id: str, epoch: int, hold_id: str, owner: str) -> str:
    """assistant_browser_hold_consume's answer: "ok" or its refusal code."""
    from app.assistant import repo

    result = repo.browser_rpc(owner, "assistant_browser_hold_consume",
                              {**_params(job_id, worker_id, boot_id, epoch), "p_hold_id": str(hold_id)})
    if result.get("ok") is True:
        return "ok"
    return str(result.get("code") or "hold_changed")


def release_status(job_id: str, worker_id: str, boot_id: str, epoch: int, hold_id: str,
                   *, wait_seconds: int = 25, user_id: str | None = None) -> dict[str, Any]:
    """Long poll for UserHoldRelease: token, decline or pending. Polls the
    fenced RPC every 2 s for up to `wait_seconds` (at most 25). A pending
    answer carries retry_after. `user_id` is the job owner (the caller has
    fenced the lease already)."""
    from app.assistant import repo
    from app.services.browser_agents import protocol

    owner = str(user_id or "")
    deadline = time.monotonic() + max(0, min(int(wait_seconds), LONG_POLL_MAX_SECONDS))
    while True:
        data = repo.browser_rpc(owner, "assistant_browser_hold_release_get",
                                {**_params(job_id, worker_id, boot_id, epoch), "p_hold_id": str(hold_id)})
        if data.get("ok") is not True:
            raise HoldRefused(str(data.get("code") or "not_found"))
        answer = _answer(owner, job_id, worker_id, boot_id, epoch, str(hold_id), data)
        if answer is not None:
            return answer
        left = deadline - time.monotonic()
        if left <= 0:
            return protocol.UserHoldRelease(hold_id=str(hold_id), kind=data["kind"], status="pending",
                                            retry_after=1).model_dump(mode="json")
        time.sleep(min(LONG_POLL_STEP_SECONDS, left))


def consume(job_id: str, worker_id: str, boot_id: str, epoch: int, hold_id: str, *, user_id: str | None = None) -> None:
    """Mark the token used (single use); sets side_effects_at. A second
    consume of the same hold is a no-op."""
    from app.assistant import repo

    result = repo.browser_rpc(str(user_id or ""), "assistant_browser_hold_consume",
                              {**_params(job_id, worker_id, boot_id, epoch), "p_hold_id": str(hold_id)})
    if result.get("ok") is True or result.get("code") == "already_consumed":
        return
    raise HoldRefused(str(result.get("code") or "hold_changed"))
