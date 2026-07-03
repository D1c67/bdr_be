"""User management — IT Admin and the Executive invite users and assign roles.

Invites go through Supabase Auth (admin API, service-role). A `profiles` row is
created with the assigned role.
"""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_internal, require_role
from app.core.ratelimit import outbound_email_rate_limit
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import invite_email
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
_ACCEPT_INVITE_PATH = "/auth/accept-invite"
_INVITE_REDIRECT_PATH = f"/auth/confirm?next={_ACCEPT_INVITE_PATH}"


def _invite_options() -> dict:
    return {"redirect_to": f"{get_settings().frontend_url}{_INVITE_REDIRECT_PATH}"}


def _confirm_url(props) -> str:
    """Build the frontend link we email for an admin-minted invite/magiclink.

    Points at our own ``/auth/confirm`` carrying the verified server-side
    ``token_hash`` — NOT GoTrue's ``action_link``, whose verify response comes
    back in a URL *fragment* that a server route handler cannot read. The confirm
    route runs ``verifyOtp({type, token_hash})`` and forwards to the accept-invite
    page, so both ``invite`` and ``magiclink`` types flow through unchanged.
    """
    qs = urlencode(
        {
            "token_hash": props.hashed_token,
            "type": props.verification_type,
            "next": _ACCEPT_INVITE_PATH,
        }
    )
    return f"{get_settings().frontend_url}/auth/confirm?{qs}"


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


@router.delete("/me/mfa", response_model=ProfileOut)
async def reset_my_mfa(user: CurrentUser = Depends(get_current_user)):
    """Remove the caller's own TOTP factor(s) so they can set up a new
    authenticator (e.g. a new phone). Requires a fully-authenticated (aal2)
    session — reaching Settings already implies it. The frontend then routes the
    user to /auth/mfa to re-enroll; `mfa_enrolled` is reset so the AAL1 hint stays
    correct. Declared before `PATCH /{user_id}` so the literal path wins.
    """
    _delete_user_factors(user.id)
    updated = sb_update(user.id, {"mfa_enrolled": False})
    audit(user.id, "user.mfa.self_reset", "profile", user.id, {})
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

    Non-Estimating-Admin callers may not hold actual-bid alerts: the key is
    stripped rather than rejected, so a stale tab saved after a dev role-switch
    keeps the user's other edits instead of 403ing the whole document.
    """
    if user.role != Role.ESTIMATING_ADMIN:
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


@router.post(
    "",
    response_model=ProfileOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(outbound_email_rate_limit)],
)
async def invite_user(
    body: InviteUserIn, admin: CurrentUser = Depends(_MANAGE_USERS)
):
    sb = get_supabase()
    cta_url: str | None = None
    if invite_email.graph_configured():
        # Mint the auth user + an invite link, then send our own G3-branded email
        # (greeting by name). `data` rides along as user_metadata so the
        # accept-invite page can greet without an extra backend call.
        try:
            link = sb.auth.admin.generate_link(
                {
                    "type": "invite",
                    "email": body.email,
                    "options": {
                        "redirect_to": f"{get_settings().frontend_url}{_INVITE_REDIRECT_PATH}",
                        "data": {"full_name": body.full_name},
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("bdr.users").exception("Invite user creation failed")
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Invite failed — check the email address and try again.",
            ) from exc
        auth_user = link.user
        cta_url = _confirm_url(link.properties)
    else:
        # Local/test fallback: Supabase mints the user and sends its own email.
        try:
            auth_user = sb.auth.admin.invite_user_by_email(
                body.email, _invite_options()
            ).user
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("bdr.users").exception("Invite user creation failed")
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Invite failed — check the email address and try again.",
            ) from exc

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

    if cta_url is not None:
        # Branded path: send the invite, and roll the whole thing back if the
        # email can't go out — a failed send must never leave a half-created
        # account that can't be re-invited.
        try:
            invite_email.send_invite_email(
                to=body.email,
                full_name=body.full_name,
                role=body.role,
                cta_url=cta_url,
                sent_by=admin.id,
            )
        except Exception as exc:  # noqa: BLE001
            sb.table("profiles").delete().eq("id", auth_user.id).execute()
            try:
                sb.auth.admin.delete_user(auth_user.id)
            except Exception:  # noqa: BLE001 — best effort; profile is already gone
                pass
            logging.getLogger("bdr.users").exception("Invite email send failed")
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Invite email could not be sent — check Graph configuration.",
            ) from exc

    audit(admin.id, "user.invite", "profile", auth_user.id, {"role": body.role.value})
    return profile


@router.post(
    "/{user_id}/reinvite",
    response_model=ProfileOut,
    dependencies=[Depends(outbound_email_rate_limit)],
)
async def reinvite_user(
    user_id: str, admin: CurrentUser = Depends(_MANAGE_USERS)
):
    """Resend the invite email to a user who hasn't accepted yet."""
    sb = get_supabase()
    rows = sb.table("profiles").select("*").eq("id", user_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    profile = rows[0]
    if profile.get("invite_accepted_at") is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "User has already accepted their invite"
        )

    if invite_email.graph_configured():
        # The user already exists, so an "invite" link would 422 — mint a
        # magiclink (passwordless finish-signup link) and send the branded email.
        try:
            link = sb.auth.admin.generate_link(
                {
                    "type": "magiclink",
                    "email": profile["email"],
                    "options": {
                        "redirect_to": f"{get_settings().frontend_url}{_INVITE_REDIRECT_PATH}"
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("bdr.users").exception("Reinvite link generation failed")
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Reinvite failed — try again.",
            ) from exc
        try:
            invite_email.send_invite_email(
                to=profile["email"],
                full_name=profile["full_name"],
                role=Role(profile["role"]),
                cta_url=_confirm_url(link.properties),
                sent_by=admin.id,
            )
        except Exception as exc:  # noqa: BLE001 — nothing to roll back; admin retries
            logging.getLogger("bdr.users").exception("Invite email send failed")
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Invite email could not be sent — check Graph configuration.",
            ) from exc
    else:
        try:
            # Re-inviting an unconfirmed user resends the Supabase invite link.
            sb.auth.admin.invite_user_by_email(profile["email"], _invite_options())
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("bdr.users").exception("Reinvite link generation failed")
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Reinvite failed — try again.",
            ) from exc

    audit(admin.id, "user.reinvite", "profile", user_id, {})
    return profile


@router.post("/{user_id}/reset-mfa", response_model=ProfileOut)
async def reset_user_mfa(
    user_id: str, admin: CurrentUser = Depends(_MANAGE_USERS)
):
    """IT Admin / Executive removes a user's TOTP factor(s) — the recovery path
    for a locked-out user (Supabase TOTP has no backup codes). The user is forced
    to re-enroll on their next login.

    NOTE: deleting factors does not revoke an already-issued aal2 token (it stays
    valid up to its ~1h TTL); enforcement re-applies once that token expires and
    the next sign-in finds no factor.
    """
    rows = (
        get_supabase().table("profiles").select("id").eq("id", user_id).limit(1).execute().data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    _delete_user_factors(user_id)
    updated = sb_update(user_id, {"mfa_enrolled": False})
    audit(admin.id, "user.mfa.admin_reset", "profile", user_id, {})
    return updated


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
    is_dev: bool | None = None,
    admin: CurrentUser = Depends(_MANAGE_USERS),
):
    patch: dict = {}
    if role is not None:
        patch["role"] = role.value
    if is_active is not None:
        patch["is_active"] = is_active
    # Admin grant/revoke of the dev flag (the role-switch backdoor). Having a
    # revocation path here means a stray is_dev can be cleared without a DB edit.
    if is_dev is not None:
        patch["is_dev"] = is_dev
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


def _delete_user_factors(user_id: str) -> None:
    """Delete every MFA factor on a user via the GoTrue admin API (service role).

    Listing failure is surfaced (502) so a reset doesn't silently no-op; deleting
    individual factors is best-effort so one bad factor can't block the rest.
    """
    sb = get_supabase()
    try:
        factors = sb.auth.admin.mfa.list_factors({"user_id": user_id})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not list MFA factors: {exc}"
        )
    for factor in factors:
        try:
            sb.auth.admin.mfa.delete_factor({"user_id": user_id, "id": factor.id})
        except Exception:  # noqa: BLE001 — keep removing the remaining factors
            pass
