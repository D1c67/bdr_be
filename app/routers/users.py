"""User management — IT Admin and the Executive invite users and assign roles.

Invites go through Supabase Auth (admin API, service-role). A `profiles` row is
created with the assigned role.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_internal, require_role
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    InviteUserIn,
    NotificationPrefsOut,
    ProfileOut,
    RoleSwitchIn,
    TeammateOut,
    UpdateMeIn,
)
from app.services.due_reminder_prefs import (
    NotificationPrefsDoc,
    default_prefs,
    effective_prefs,
)
from app.services.notifications import audit

router = APIRouter(prefix="/users", tags=["users"])
_MANAGE_USERS = require_role(Role.IT_ADMIN, Role.EXECUTIVE)

# Supabase verifies the invite link, then redirects the user here. The /auth/confirm
# route handler establishes the session cookie and forwards to `next` — the
# accept-invite page where the user sets a password. Must be registered in
# Supabase → Auth → URL Configuration → Redirect URLs, or Supabase falls back to
# the Site URL (which would drop the user on /login with no session — the bug).
_INVITE_REDIRECT_PATH = "/auth/confirm?next=/auth/accept-invite"


def _invite_options() -> dict:
    return {"redirect_to": f"{get_settings().frontend_url}{_INVITE_REDIRECT_PATH}"}


@router.get("/me", response_model=ProfileOut)
async def me(user: CurrentUser = Depends(get_current_user)):
    return (
        get_supabase().table("profiles").select("*").eq("id", user.id).single().execute()
    ).data


@router.patch("/me", response_model=ProfileOut)
async def update_me(body: UpdateMeIn, user: CurrentUser = Depends(get_current_user)):
    """Any signed-in user (estimator included) may edit their own display name and
    UI language.

    Declared before `PATCH /{user_id}` so the literal path wins. Email and role
    stay admin-managed via the endpoints below.
    """
    patch: dict = {}
    if body.full_name is not None:
        patch["full_name"] = body.full_name
    if body.locale is not None:
        patch["locale"] = body.locale
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to update")
    updated = sb_update(user.id, patch)
    audit(user.id, "user.update_self", "profile", user.id, patch)
    return updated


@router.get("/me/notification-prefs", response_model=NotificationPrefsOut)
async def get_notification_prefs(user: CurrentUser = Depends(require_internal)):
    """The caller's effective due-date reminder prefs (stored row ?? role defaults).

    Internal users only — the external estimator gets fixed presets via the
    reminder poller and has no settings surface at all.
    """
    row = (
        get_supabase()
        .table("notification_prefs")
        .select("prefs")
        .eq("user_id", user.id)
        .limit(1)
        .execute()
    ).data
    stored = row[0]["prefs"] if row else None
    return NotificationPrefsOut(
        prefs=effective_prefs(user.role, stored), is_customized=bool(row)
    )


@router.put("/me/notification-prefs", response_model=NotificationPrefsOut)
async def update_notification_prefs(
    body: NotificationPrefsDoc, user: CurrentUser = Depends(require_internal)
):
    """Replace the caller's reminder prefs (full document).

    Non-PA callers may not hold actual-bid alerts: the key is stripped rather
    than rejected, so a stale tab saved after a dev role-switch keeps the
    user's other edits instead of 403ing the whole document.
    """
    if user.role != Role.PA:
        body = body.model_copy(update={"actual_bid": None})
    doc = body.model_dump(mode="json", exclude_none=True)
    get_supabase().table("notification_prefs").upsert(
        {"user_id": user.id, "prefs": doc}, on_conflict="user_id"
    ).execute()
    audit(user.id, "user.notification_prefs.update", "profile", user.id, doc)
    return NotificationPrefsOut(
        prefs=effective_prefs(user.role, doc), is_customized=True
    )


@router.delete("/me/notification-prefs", response_model=NotificationPrefsOut)
async def reset_notification_prefs(user: CurrentUser = Depends(require_internal)):
    """Reset to role defaults — deletes the stored row and returns the presets."""
    get_supabase().table("notification_prefs").delete().eq(
        "user_id", user.id
    ).execute()
    audit(user.id, "user.notification_prefs.reset", "profile", user.id, {})
    return NotificationPrefsOut(prefs=default_prefs(user.role), is_customized=False)


@router.get("", response_model=list[ProfileOut])
async def list_users(_: CurrentUser = Depends(_MANAGE_USERS)):
    return get_supabase().table("profiles").select("*").order("created_at").execute().data or []


@router.get("/teammates", response_model=list[TeammateOut])
async def list_teammates(_: CurrentUser = Depends(require_internal)):
    """Active internal users — the To-Dos teammate picker.

    Unlike the admin list above, any internal user may call this; it returns
    only the minimal fields a picker needs. The external estimator is neither
    included nor allowed to call.
    """
    return (
        get_supabase()
        .table("profiles")
        .select("id, full_name, email, role")
        .in_("role", [r.value for r in INTERNAL_ROLES])
        .eq("is_active", True)
        .order("full_name")
        .execute()
    ).data or []


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def invite_user(
    body: InviteUserIn, admin: CurrentUser = Depends(_MANAGE_USERS)
):
    sb = get_supabase()
    # Send a Supabase Auth invite email (user sets their own password).
    try:
        invited = sb.auth.admin.invite_user_by_email(body.email, _invite_options())
        auth_user = invited.user
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invite failed: {exc}")

    profile = (
        sb.table("profiles")
        .insert(
            {
                "id": auth_user.id,
                "email": body.email,
                "full_name": body.full_name,
                "role": body.role.value,
                "is_active": True,
            }
        )
        .execute()
    ).data[0]
    audit(admin.id, "user.invite", "profile", auth_user.id, {"role": body.role.value})
    return profile


@router.post("/{user_id}/reinvite", response_model=ProfileOut)
async def reinvite_user(
    user_id: str, admin: CurrentUser = Depends(_MANAGE_USERS)
):
    """Resend the Supabase Auth invite email to a user who hasn't accepted yet."""
    sb = get_supabase()
    rows = sb.table("profiles").select("*").eq("id", user_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    profile = rows[0]
    if profile.get("invite_accepted_at") is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "User has already accepted their invite"
        )
    try:
        # Re-inviting an unconfirmed user resends the invite link.
        sb.auth.admin.invite_user_by_email(profile["email"], _invite_options())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Reinvite failed: {exc}")
    audit(admin.id, "user.reinvite", "profile", user_id, {})
    return profile


@router.patch("/me/role", response_model=ProfileOut)
async def switch_own_role(
    body: RoleSwitchIn, user: CurrentUser = Depends(get_current_user)
):
    """Dev accounts may change their own role to experience the app as any role.

    Gated on `is_dev` (a flag independent of `role`, so a dev who switched away can
    always switch back). Declared before `/{user_id}` so the literal path wins.
    """
    if not user.is_dev:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dev account required")
    updated = sb_update(user.id, {"role": body.role.value})
    audit(user.id, "user.dev_role_switch", "profile", user.id, {"role": body.role.value})
    return updated


@router.patch("/{user_id}", response_model=ProfileOut)
async def update_user(
    user_id: str,
    role: Role | None = None,
    is_active: bool | None = None,
    admin: CurrentUser = Depends(_MANAGE_USERS),
):
    patch: dict = {}
    if role is not None:
        patch["role"] = role.value
    if is_active is not None:
        patch["is_active"] = is_active
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to update")
    updated = sb_update(user_id, patch)
    audit(admin.id, "user.update", "profile", user_id, patch)
    return updated


def sb_update(user_id: str, patch: dict) -> dict:
    rows = get_supabase().table("profiles").update(patch).eq("id", user_id).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return rows[0]
