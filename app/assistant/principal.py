"""The `bdra_` assistant principal (docs/ASSISTANT.md, Architecture >
Principal).

The worker mints a short-lived token bound to (user_id, run_id, approval_id
or null), HMAC-SHA256 signed with ASSISTANT_TOKEN_SECRET, and sends it as
the bearer on every in-process request through the shim. `get_current_user`
(core/deps.py) hands a `bdra_` bearer to `resolve_bdra_principal`, which
accepts it only when the ASGI scope carries the shim's `bdr_internal`
marker: a `bdra_` token arriving over the network is refused exactly like a
`bdrw_` worker token, with the same 401 and the same message.

Token layout: "bdra_" + base64url(payload json) + "." + base64url(hmac).
The payload is compact json with sorted keys: {"a": approval_id or null,
"exp": unix seconds, "iat": unix seconds, "n": nonce, "r": run_id,
"u": user_id}. Verification compares the signature in constant time
(hmac.compare_digest, the core/agent_auth pattern) before reading a field.

Everything here is plain `def`: the delegation lookup uses the sync Supabase
SDK, and FastAPI runs the dependency in its threadpool.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from app.core import provenance, supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser
from app.core.roles import INTERNAL_ROLES, Role

TOKEN_PREFIX = "bdra_"
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 900
MIN_SECRET_CHARS = 32
SCOPE_MARKER = "bdr_internal"
DELEGATIONS_TABLE = "assistant_delegations"
PROFILE_COLUMNS = (
    "id, email, role, is_active, is_dev, mfa_enrolled, invite_accepted_at, rfp_mailboxes"
)

# The exact message deps.get_current_user uses for a bdrw_ token, so a
# refused bdra_ token is indistinguishable from any other bad bearer.
_INVALID = "Invalid or expired token"

ProfileLoader = Callable[[str], Mapping[str, Any] | None]


class TokenError(ValueError):
    """A token that does not verify: shape, signature, expiry or secret.
    The message never carries the token or the secret."""


@dataclass(frozen=True)
class AssistantTokenClaims:
    user_id: str
    run_id: str
    approval_id: str | None
    issued_at: int
    expires_at: int


# ── Secret and encoding ──────────────────────────────────────────────────────


def _secret(settings: Any = None) -> bytes:
    s = settings if settings is not None else get_settings()
    raw = getattr(s, "assistant_token_secret", "") or ""
    if not isinstance(raw, str) or len(raw) < MIN_SECRET_CHARS:
        raise TokenError("ASSISTANT_TOKEN_SECRET is not configured")
    return raw.encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (ValueError, TypeError):
        raise TokenError("malformed token") from None


def _sign(secret: bytes, payload_b64: str) -> bytes:
    return hmac.new(secret, (TOKEN_PREFIX + payload_b64).encode("ascii"), hashlib.sha256).digest()


def _now(now: datetime | None) -> int:
    return int((now or datetime.now(timezone.utc)).timestamp())


# ── Mint and verify ──────────────────────────────────────────────────────────


def mint_token(
    user_id: str,
    run_id: str,
    approval_id: str | None = None,
    ttl: int = DEFAULT_TTL_SECONDS,
    *,
    settings: Any = None,
    now: datetime | None = None,
) -> str:
    """A `bdra_` token for `user_id`'s assistant in `run_id`, good for `ttl`
    seconds (5 minutes by default, 15 at most). Only the worker calls this;
    the token is never stored and never leaves the process."""
    if not user_id or not run_id:
        raise ValueError("mint_token needs a user_id and a run_id")
    if not 1 <= int(ttl) <= MAX_TTL_SECONDS:
        raise ValueError(f"ttl must be between 1 and {MAX_TTL_SECONDS} seconds")
    secret = _secret(settings)
    issued = _now(now)
    payload = {
        "a": str(approval_id) if approval_id else None,
        "exp": issued + int(ttl),
        "iat": issued,
        "n": secrets.token_urlsafe(8),
        "r": str(run_id),
        "u": str(user_id),
    }
    payload_b64 = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{TOKEN_PREFIX}{payload_b64}.{_b64(_sign(secret, payload_b64))}"


def verify_token(
    token: str, *, settings: Any = None, now: datetime | None = None
) -> AssistantTokenClaims:
    """The claims of a token that verifies; TokenError otherwise."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        raise TokenError("not an assistant token")
    body = token[len(TOKEN_PREFIX):]
    payload_b64, sep, sig_b64 = body.partition(".")
    if not sep or not payload_b64 or not sig_b64:
        raise TokenError("malformed token")
    secret = _secret(settings)
    expected = _sign(secret, payload_b64)
    if not hmac.compare_digest(_unb64(sig_b64), expected):
        raise TokenError("bad signature")
    try:
        payload = json.loads(_unb64(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise TokenError("malformed token") from None
    if not isinstance(payload, dict):
        raise TokenError("malformed token")
    user_id, run_id = payload.get("u"), payload.get("r")
    issued, expires = payload.get("iat"), payload.get("exp")
    approval = payload.get("a")
    if not (isinstance(user_id, str) and user_id and isinstance(run_id, str) and run_id):
        raise TokenError("malformed token")
    if not (isinstance(issued, int) and isinstance(expires, int)) or expires <= issued:
        raise TokenError("malformed token")
    if approval is not None and not (isinstance(approval, str) and approval):
        raise TokenError("malformed token")
    if expires - issued > MAX_TTL_SECONDS:
        raise TokenError("token lifetime too long")
    if _now(now) >= expires:
        raise TokenError("token expired")
    return AssistantTokenClaims(
        user_id=user_id, run_id=run_id, approval_id=approval,
        issued_at=issued, expires_at=expires,
    )


# ── Delegation ───────────────────────────────────────────────────────────────


def active_delegation(user_id: str) -> Mapping[str, Any] | None:
    """The user's unrevoked, unexpired assistant_delegations row, or None.
    The expiry comparison runs in the database (expires_at > now()) so
    clock skew between the worker and Postgres cannot widen a grant."""
    rows = (
        supabase_client.get_supabase()
        .table(DELEGATIONS_TABLE)
        .select("id, user_id, created_at, expires_at, revoked_at")
        .eq("user_id", user_id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


# ── The principal ────────────────────────────────────────────────────────────


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID)


def resolve_bdra_principal(
    request_scope: Mapping[str, Any] | None,
    token: str,
    load_profile: ProfileLoader,
) -> CurrentUser:
    """The CurrentUser behind a `bdra_` bearer, or an HTTPException.

    In order: the scope must carry the shim's marker (a network request
    never does), the token must verify, its (user_id, run_id) must equal the
    provenance contextvar the shim set for this request, a token bound to an
    approval needs that approval row (the owner's) in status executing, the
    owner's profile
    (re-read every call through `load_profile`, the same loader
    get_current_user uses) must be active and in INTERNAL_ROLES, and an
    unrevoked delegation must exist. The result is the human with is_dev
    forced False, no impersonation (the X-Impersonate-Estimator header is
    never read here), aal2 because the grant was created under aal2, and
    via_assistant_run_id set.
    """
    if not isinstance(request_scope, Mapping) or request_scope.get(SCOPE_MARKER) is not True:
        raise _unauthorized()
    try:
        claims = verify_token(token)
    except TokenError:
        raise _unauthorized() from None

    actor = provenance.current()
    if actor is None or (actor.user_id, actor.run_id) != (claims.user_id, claims.run_id):
        raise _unauthorized()
    if actor.approval_id != claims.approval_id:
        raise _unauthorized()
    if claims.approval_id is not None:
        # An approval-bound token (the execute job) acts only while its card
        # is executing; the shim's allowlist already holds that tool's
        # routes only (docs/ASSISTANT_PHASE1_PLAN.md 3.8).
        from app.assistant import repo

        row = repo.approval_for_principal(claims.user_id, claims.approval_id)
        if not row or row.get("status") != "executing":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "assistant_approval_not_executing")

    profile = load_profile(claims.user_id)
    if not profile:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No profile for this user")
    if not profile.get("is_active", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")
    try:
        role = Role(profile.get("role"))
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted") from None
    if role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    if active_delegation(claims.user_id) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "assistant_delegation_missing")

    values: dict[str, Any] = dict(
        id=str(profile["id"]),
        email=profile.get("email") or "",
        role=role,
        is_active=True,
        is_dev=False,
        aal="aal2",
        mfa_enrolled=bool(profile.get("mfa_enrolled", False)),
        impersonated_by=None,
        rfp_mailboxes=tuple(
            m.strip().lower()
            for m in (profile.get("rfp_mailboxes") or [])
            if isinstance(m, str) and m.strip()
        ),
    )
    # CurrentUser.via_assistant_run_id lands with the deps.py integration
    # (app/assistant/INTEGRATION_TODO.md); until then the attribute is set on
    # the instance so callers that read it see the same value either way.
    declared = {f.name for f in dataclass_fields(CurrentUser)}
    if "via_assistant_run_id" in declared:
        values["via_assistant_run_id"] = claims.run_id
        return CurrentUser(**values)
    user = CurrentUser(**values)
    user.via_assistant_run_id = claims.run_id  # type: ignore[attr-defined]
    return user


def load_profile_default(user_id: str) -> Mapping[str, Any] | None:
    """The profile loader the hook passes when deps.py has none of its own:
    the same columns get_current_user selects."""
    resp = (
        supabase_client.get_supabase()
        .table("profiles")
        .select(PROFILE_COLUMNS)
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None
