"""FastAPI dependencies for authentication and role-based authorization.

The service-role Supabase client bypasses RLS, so authorization MUST be enforced
here on every protected route. `require_project_assignment` additionally gates the
external estimator to only their actively-assigned projects.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import get_settings
from app.core.features import SubApp, feature_404, is_enabled
from app.core.roles import (
    CP_READ_ROLES,
    CP_WRITE_ROLES,
    INTERNAL_ROLES,
    PM_READ_ROLES,
    PM_WRITE_ROLES,
    WRITER_ROLES,
    Role,
)
from app.core.security import decode_token
from app.core.supabase_client import get_supabase

# Endpoints a user may call before reaching aal2 (i.e. before enrolling/passing
# 2FA). Kept to the bare minimum the frontend needs to render the app shell and
# the MFA gate: loading the caller's own profile, and the sub-app feature flags
# the shell reads alongside it (three booleans naming which modules this
# deployment serves — no user or project data). Everything else requires aal2.
AAL1_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {("GET", "/users/me"), ("GET", "/features")}
)

# Dev-only "view the portal as" — the frontend sends the target estimator's
# profile id here and, for is_dev callers ONLY, the request runs as that
# estimator (see `_impersonated_estimator`). Ignored for everyone else.
IMPERSONATE_HEADER = "x-impersonate-estimator"

logger = logging.getLogger(__name__)


@dataclass
class CurrentUser:
    id: str
    email: str
    role: Role
    is_active: bool
    is_dev: bool = False  # dev account: may switch its own role; bypasses estimator gates
    # Assurance level from the JWT `aal` claim — "aal2" once a TOTP factor has
    # been verified this session, else "aal1". Defaults to "aal2" so unit tests
    # that build CurrentUser directly (test_role_model) aren't treated as
    # un-stepped-up; real requests always pass the decoded value.
    aal: str = "aal2"
    mfa_enrolled: bool = False  # cached: user has a verified TOTP factor (profiles.mfa_enrolled)
    # Set when a dev account is acting as an external estimator via
    # IMPERSONATE_HEADER: `id`/`email`/`role` are the ESTIMATOR's (with
    # `is_dev=False`, so every gate behaves exactly as it would for them) and
    # this holds the real dev profile id behind the request.
    impersonated_by: str | None = None


def get_current_user(
    request: Request, authorization: str = Header(default="")
) -> CurrentUser:
    """Verify the bearer token, load the caller's profile (role), and enforce 2FA.

    Deliberately sync (`def`): the Supabase SDK calls below block, so FastAPI must
    run this in its threadpool. As `async def` they would run on the event loop and
    every request would stall the whole (single-worker) server for its DB round
    trips — the production-wide hang. The same rule applies to the routers.

    2FA is required for every user. The real enforcement boundary lives here (the
    service-role client bypasses RLS, so this is the choke point): any request
    whose token is not `aal2` is rejected, EXCEPT the small `AAL1_ALLOWED` set a
    not-yet-enrolled user needs to reach the enrollment screen. MFA enrollment and
    step-up happen client-side directly against Supabase Auth — FastAPI is never
    in that path; it only refuses under-assured tokens.
    """
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token missing subject")
    aal = claims.get("aal", "aal1")  # the assurance level; missing → treat as aal1 (fail-closed)

    resp = (
        get_supabase()
        .table("profiles")
        .select("id, email, role, is_active, is_dev, mfa_enrolled, invite_accepted_at")
        .eq("id", user_id)
        .single()
        .execute()
    )
    profile = resp.data
    if not profile:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No profile for this user")
    if not profile.get("is_active", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")

    # A valid bearer token means the user accepted their invite (set a password
    # and signed in). Stamp the acceptance once so admins can distinguish a
    # working account from a still-pending invite.
    if profile.get("invite_accepted_at") is None:
        now = datetime.now(timezone.utc).isoformat()
        get_supabase().table("profiles").update({"invite_accepted_at": now}).eq(
            "id", user_id
        ).execute()

    # Reaching aal2 proves a verified TOTP factor exists — cache that locally so
    # the AAL1 rejection below can pick the right signal and the admin list can
    # show a 2FA badge without querying GoTrue. Mirrors the invite stamp above.
    mfa_enrolled = bool(profile.get("mfa_enrolled", False))
    if aal == "aal2" and not mfa_enrolled:
        get_supabase().table("profiles").update({"mfa_enrolled": True}).eq(
            "id", user_id
        ).execute()
        mfa_enrolled = True

    # Enforce 2FA (fail-closed): a non-aal2 token may only hit the allowlist.
    if (
        get_settings().mfa_required
        and aal != "aal2"
        and (request.method, request.url.path) not in AAL1_ALLOWED
    ):
        # The FE maps these detail codes to the right screen: enrolled users are
        # sent to step-up (enter a code), un-enrolled users to enrollment (QR).
        code = "mfa_step_up_required" if mfa_enrolled else "mfa_enrollment_required"
        raise HTTPException(status.HTTP_403_FORBIDDEN, code)

    # Dev-only impersonation: honored strictly for is_dev callers — anyone else
    # sending the header just gets their own identity (no privilege to gain, and
    # a stale header in a shared browser must not brick a normal account).
    impersonate_id = request.headers.get(IMPERSONATE_HEADER, "").strip()
    if impersonate_id and profile.get("is_dev"):
        return _impersonated_estimator(
            impersonate_id, dev=profile, aal=aal, method=request.method,
            path=request.url.path,
        )

    return CurrentUser(
        id=profile["id"],
        email=profile["email"],
        role=Role(profile["role"]),
        is_active=profile["is_active"],
        is_dev=profile.get("is_dev", False),
        aal=aal,
        mfa_enrolled=mfa_enrolled,
    )


def _impersonated_estimator(
    target_id: str, *, dev: dict, aal: str, method: str, path: str
) -> CurrentUser:
    """Act as the given EXTERNAL estimator for this request (dev accounts only).

    The effective user carries the estimator's id/email/role with is_dev=False,
    so assignment gates, file visibility and submissions all behave exactly as
    they would for the real estimator — that fidelity is the point. Only active,
    non-dev estimator-role profiles are valid targets (fail-closed with a code
    the FE recognises and clears): a dev can never become an internal role or
    another dev this way. The dev's own 2FA has already been enforced above;
    the target's MFA state is irrelevant since it's the dev's token.
    """
    rows = (
        get_supabase()
        .table("profiles")
        .select("id, email, role, is_active, is_dev")
        .eq("id", target_id)
        .limit(1)
        .execute()
    ).data or []
    target = rows[0] if rows else None
    if (
        not target
        or target.get("role") != Role.ESTIMATOR.value
        or not target.get("is_active")
        or target.get("is_dev")
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "impersonation_target_invalid")
    if method not in ("GET", "HEAD", "OPTIONS"):
        # Writes land attributed to the estimator (uploaded_by, rounds, bells) —
        # keep a greppable trace of who was really behind them, plus a durable
        # audit_log row: stdout rotates, and the row is the only counter-evidence
        # clearing the estimator if an impersonated write is ever disputed.
        logger.info(
            "dev %s acting as estimator %s: %s %s", dev["id"], target_id, method, path
        )
        try:
            get_supabase().table("audit_log").insert(
                {
                    "actor_id": dev["id"],
                    "action": "impersonation.write",
                    "entity": "profile",
                    "entity_id": target_id,
                    "payload": {"method": method, "path": path},
                }
            ).execute()
        except Exception:  # noqa: BLE001 — the trace must never fail the request
            logger.exception("impersonation audit write failed")
    return CurrentUser(
        id=target["id"],
        email=target["email"],
        role=Role.ESTIMATOR,
        is_active=True,
        is_dev=False,
        aal=aal,
        mfa_enrolled=True,
        impersonated_by=dev["id"],
    )


def require_role(*allowed: Role):
    """Dependency factory: allow only the given roles."""
    allowed_set = set(allowed)

    async def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed_set:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return _dep


async def require_internal(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow any internal role (read access); the external estimator is rejected.

    Use this for READ endpoints — it admits the read-only accountant. For writes
    use `require_writer`, which additionally excludes the accountant.
    """
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return user


async def require_writer(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow any role that may perform a pipeline write/step.

    This is every internal role EXCEPT the read-only accountant (and the external
    estimator, which is never internal). Use it for any write that an internal
    teammate is allowed to perform on any stage.
    """
    if user.role not in WRITER_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only or insufficient role")
    return user


async def require_dev(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow only dev accounts (profiles.is_dev), regardless of role.

    Gates the Training surface: it exposes raw model inputs/outputs and exists
    for model-improvement work, not day-to-day operations.
    """
    if not user.is_dev:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dev account required")
    return user


async def require_pm_read(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow any role with read access to the PM module.

    Today identical to `require_internal` (accountant included, estimator never),
    but PM routes must depend on THIS so future PM-specific roles are a
    roles.py-only change.

    Also enforces PM_ENABLED. main.py already guards every PM router at mount
    time, so this is the second lock: these two dependencies are used by the PM
    routers and by nothing else, which makes them the choke point where a future
    PM route that was never added to main.py's table still fails closed.
    """
    if not is_enabled(SubApp.PM):
        raise feature_404(SubApp.PM)
    if user.role not in PM_READ_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return user


async def require_pm_write(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow any role that may write in the PM module (excludes the read-only
    accountant and the external estimator). PM mirror of `require_writer`.
    Enforces PM_ENABLED for the same reason as `require_pm_read`."""
    if not is_enabled(SubApp.PM):
        raise feature_404(SubApp.PM)
    if user.role not in PM_WRITE_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only or insufficient role")
    return user


async def require_cp_read(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow any role with read access to the Certified Payroll module.

    Today identical to `require_internal` (accountant included, estimator never),
    but CP routes must depend on THIS so future CP-specific roles are a
    roles.py-only change.

    Also enforces CERTIFIED_PAYROLL_ENABLED — same second-lock reasoning as
    `require_pm_read`: every CP endpoint takes one of these two and nothing else
    does, so a CP route missing from main.py's table still fails closed.
    """
    if not is_enabled(SubApp.CERTIFIED_PAYROLL):
        raise feature_404(SubApp.CERTIFIED_PAYROLL)
    if user.role not in CP_READ_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return user


async def require_cp_write(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow any role that may write in the Certified Payroll module (excludes
    the read-only accountant and the external estimator). CP mirror of
    `require_writer`. Enforces CERTIFIED_PAYROLL_ENABLED for the same reason as
    `require_cp_read`."""
    if not is_enabled(SubApp.CERTIFIED_PAYROLL):
        raise feature_404(SubApp.CERTIFIED_PAYROLL)
    if user.role not in CP_WRITE_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only or insufficient role")
    return user


def require_project_assignment(
    project_id: str, user: CurrentUser = Depends(get_current_user)
) -> CurrentUser:
    """Gate estimator access to a project via an active assignment.

    Non-estimators pass through (their own role guards apply). Estimators must
    have an assignment row that is not revoked and not expired, AND the project
    must not be abandoned.
    """
    if user.role != Role.ESTIMATOR:
        return user

    # A dev browsing the portal as THEMSELVES (role-switched, not impersonating)
    # has no real assignments — let them through so the portal UI is testable.
    # (No assigned project data will appear.) When impersonating an estimator,
    # is_dev is False and the real assignment gate below runs — full fidelity.
    if user.is_dev:
        return user

    # Active assignment = not revoked AND (no expiry OR expiry in the future).
    # `now()` comparison is done DB-side so server/client clock skew can't widen access.
    resp = (
        get_supabase()
        .table("estimator_assignments")
        .select("id, expires_at, revoked_at")
        .eq("project_id", project_id)
        .eq("estimator_id", user.id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .execute()
    )
    rows = resp.data or []
    if not rows:
        # Denied access is a security signal for the external estimator — audit it
        # and alert IT if denials are bursting. NOT when a dev is merely viewing
        # the portal as them: that would pin a probing alert on the real person.
        if user.impersonated_by is None:
            from app.services.security_alerts import record_denied_access

            record_denied_access(user.id, project_id, "no_active_assignment")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not assigned to this project")

    # An abandoned bid drops off the estimator's desk entirely — detail, files,
    # notes, submit. The assignment row is deliberately left alone (abandon is
    # reversible, so /reactivate restores access exactly as it was) and this is
    # the one gate that reads the marker for every estimator route.
    #
    # Not routed through record_denied_access: this denial is expected, not a
    # security signal — an estimator refreshing a bookmarked project the team
    # just killed must not burst the IT probing alert.
    proj = (
        get_supabase()
        .table("projects")
        .select("abandoned_at")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    if proj and proj[0].get("abandoned_at"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This project is no longer available"
        )
    return user
