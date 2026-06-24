"""FastAPI dependencies for authentication and role-based authorization.

The service-role Supabase client bypasses RLS, so authorization MUST be enforced
here on every protected route. `require_project_assignment` additionally gates the
external estimator to only their actively-assigned projects.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from fastapi import Depends, Header, HTTPException, status

from app.core.roles import INTERNAL_ROLES, Role
from app.core.security import decode_token
from app.core.supabase_client import get_supabase


@dataclass
class CurrentUser:
    id: str
    email: str
    role: Role
    is_active: bool
    is_dev: bool = False  # dev account: may switch its own role; bypasses estimator gates


async def get_current_user(authorization: str = Header(default="")) -> CurrentUser:
    """Verify the bearer token and load the caller's profile (role)."""
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

    resp = (
        get_supabase()
        .table("profiles")
        .select("id, email, role, is_active, is_dev, invite_accepted_at")
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

    return CurrentUser(
        id=profile["id"],
        email=profile["email"],
        role=Role(profile["role"]),
        is_active=profile["is_active"],
        is_dev=profile.get("is_dev", False),
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
    """Allow any internal role; the external estimator is rejected."""
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return user


async def require_project_assignment(
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
