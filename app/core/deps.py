"""FastAPI dependencies for authentication and role-based authorization.

The service-role Supabase client bypasses RLS, so authorization MUST be enforced
here on every protected route. `require_project_assignment` additionally gates the
external estimator to only their actively-assigned projects.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role
from app.core.security import decode_token
from app.core.supabase_client import get_supabase

# Endpoints a user may call before reaching aal2 (i.e. before enrolling/passing
# 2FA). Kept to the bare minimum the frontend needs to render the app shell and
# the MFA gate: loading the caller's own profile. Everything else requires aal2.
AAL1_ALLOWED: frozenset[tuple[str, str]] = frozenset({("GET", "/users/me")})


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

    return CurrentUser(
        id=profile["id"],
        email=profile["email"],
        role=Role(profile["role"]),
        is_active=profile["is_active"],
        is_dev=profile.get("is_dev", False),
        aal=aal,
        mfa_enrolled=mfa_enrolled,
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


def require_project_assignment(
    project_id: str, user: CurrentUser = Depends(get_current_user)
) -> CurrentUser:
    """Gate estimator access to a project via an active assignment.

    Non-estimators pass through (their own role guards apply). Estimators must
    have an assignment row that is not revoked and not expired.
    """
    if user.role != Role.ESTIMATOR:
        return user

    # Dev accounts impersonating the estimator have no real assignments — let them
    # through so the portal UI is testable. (No assigned project data will appear.)
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
        # and alert IT if denials are bursting.
        from app.services.security_alerts import record_denied_access

        record_denied_access(user.id, project_id, "no_active_assignment")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not assigned to this project")
    return user
