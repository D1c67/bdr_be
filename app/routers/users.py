"""User management: IT Admin and the Executive own the full account lifecycle.

Invites go through Supabase Auth (admin API, service-role). A `profiles` row is
created with the assigned role. From there an admin can correct a name, move the
login email, change the role, enable/disable the account, resend a pending
invite, reset a locked-out user's 2FA, send a password-reset link, and (last
resort) delete the account outright.

Two guardrails run across the destructive edits, since the same admins hold the
only keys to this surface:

* an admin may not delete or disable their OWN account, and
* the last active account that can manage users may not be deleted, disabled or
  demoted, otherwise the deployment locks itself out of user management with
  no in-app recovery path.
"""

import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_internal, require_role
from app.core.ratelimit import outbound_email_rate_limit
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import invite_email
from app.models.schemas import (
    AdminUpdateUserIn,
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
# The roles that may administer accounts. Kept as a tuple so the auth gate and
# the "don't strand the deployment without an admin" guard below can never drift.
MANAGE_USER_ROLES = (Role.IT_ADMIN, Role.EXECUTIVE)
_MANAGE_USERS = require_role(*MANAGE_USER_ROLES)

# Supabase verifies the invite link, then redirects the user here. The /auth/confirm
# route handler establishes the session cookie and forwards to `next` — the
# accept-invite page where the user sets a password. Must be registered in
# Supabase → Auth → URL Configuration → Redirect URLs, or Supabase falls back to
# the Site URL (which would drop the user on /login with no session — the bug).
_ACCEPT_INVITE_PATH = "/auth/accept-invite"
_INVITE_REDIRECT_PATH = f"/auth/confirm?next={_ACCEPT_INVITE_PATH}"
# Where an admin-sent password reset lands: the same confirm handler (it already
# knows the `recovery` OTP type), forwarding to the set-a-new-password screen.
_RESET_PASSWORD_PATH = "/auth/reset-password"
_RECOVERY_REDIRECT_PATH = f"/auth/confirm?next={_RESET_PASSWORD_PATH}"


def _invite_options() -> dict:
    return {"redirect_to": f"{get_settings().frontend_url}{_INVITE_REDIRECT_PATH}"}


def _confirm_url(props, next_path: str = _ACCEPT_INVITE_PATH) -> str:
    """Build the frontend link we email for an admin-minted invite/magiclink/recovery.

    Points at our own ``/auth/confirm`` carrying the verified server-side
    ``token_hash`` — NOT GoTrue's ``action_link``, whose verify response comes
    back in a URL *fragment* that a server route handler cannot read. The confirm
    route runs ``verifyOtp({type, token_hash})`` and forwards to ``next_path``, so
    ``invite``, ``magiclink`` and ``recovery`` all flow through unchanged.
    """
    qs = urlencode(
        {
            "token_hash": props.hashed_token,
            "type": props.verification_type,
            "next": next_path,
        }
    )
    return f"{get_settings().frontend_url}/auth/confirm?{qs}"


def _refuse_while_impersonating(user: CurrentUser) -> None:
    """Durable self-profile edits (name/locale, MFA reset) target `user.id` —
    under dev impersonation that is the REAL estimator's account, so refuse
    rather than let a dev testing the portal rewrite someone's identity."""
    if user.impersonated_by:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not available while viewing as an estimator"
        )


def _load_profile(user_id: str) -> dict:
    """Fetch one profile or 404: the read every admin write starts from."""
    rows = (
        get_supabase().table("profiles").select("*").eq("id", user_id).limit(1).execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return rows[0]


def _another_admin_remains(user_id: str) -> bool:
    """True when some OTHER enabled account can still manage users."""
    return bool(
        (
            get_supabase()
            .table("profiles")
            .select("id")
            .in_("role", [r.value for r in MANAGE_USER_ROLES])
            .eq("is_active", True)
            .neq("id", user_id)
            .limit(1)
            .execute()
        ).data
    )


def _guard_admin_coverage(
    target: dict,
    *,
    role: Role | None = None,
    is_active: bool | None = None,
    deleting: bool = False,
) -> None:
    """Refuse an edit that would leave nobody able to manage users.

    Only bites when `target` is currently a *working* admin (enabled and holding
    a manage role) and the edit takes that away: deleting them, disabling them,
    or moving them to a role that cannot administer accounts. Losing the last one
    is unrecoverable from inside the app, since the Settings > Users page 403s for
    everyone, and the only fix left is a hand-written SQL update against the
    database.
    """
    if not target.get("is_active") or Role(target["role"]) not in MANAGE_USER_ROLES:
        return
    losing_admin = (
        deleting
        or is_active is False
        or (role is not None and role not in MANAGE_USER_ROLES)
    )
    if losing_admin and not _another_admin_remains(target["id"]):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This is the last account that can manage users. Promote another "
            "IT Admin or Executive first.",
        )


@router.get("/me", response_model=ProfileOut)
def me(user: CurrentUser = Depends(get_current_user)):
    return (
        get_supabase().table("profiles").select("*").eq("id", user.id).single().execute()
    ).data


@router.patch("/me", response_model=ProfileOut)
def update_me(body: UpdateMeIn, user: CurrentUser = Depends(get_current_user)):
    """Any signed-in user (estimator included) may edit their own display name and
    UI language.

    Declared before `PATCH /{user_id}` so the literal path wins. Email and role
    stay admin-managed via the endpoints below.
    """
    _refuse_while_impersonating(user)
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
def reset_my_mfa(user: CurrentUser = Depends(get_current_user)):
    """Remove the caller's own TOTP factor(s) so they can set up a new
    authenticator (e.g. a new phone). Requires a fully-authenticated (aal2)
    session — reaching Settings already implies it. The frontend then routes the
    user to /auth/mfa to re-enroll; `mfa_enrolled` is reset so the AAL1 hint stays
    correct. Declared before `PATCH /{user_id}` so the literal path wins.
    """
    _refuse_while_impersonating(user)
    _delete_user_factors(user.id)
    updated = sb_update(user.id, {"mfa_enrolled": False})
    audit(user.id, "user.mfa.self_reset", "profile", user.id, {})
    return updated


@router.post("/me/estimator-tour", response_model=ProfileOut)
def complete_estimator_tour(user: CurrentUser = Depends(get_current_user)):
    """Stop offering the external estimator portal's first-run tour to the caller.

    Called when the tour is finished AND when it is dismissed part way: from the
    portal's point of view those are the same answer ("don't ask me again"), and
    a tour that re-opened itself because someone closed it would be a worse
    failure than one that never re-opened. Replaying it from the documentation
    page does not clear the stamp, so the value stays the FIRST completion.

    Not audited: unlike the self-writes above it changes nothing about the
    account's identity or access. Declared before `PATCH /{user_id}` with the
    rest of the /me routes so the literal path wins.
    """
    _refuse_while_impersonating(user)
    sb = get_supabase()
    rows = sb.table("profiles").select("*").eq("id", user.id).limit(1).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if rows[0].get("estimator_tour_completed_at"):
        # Already stamped — a replay must not rewrite when they first finished.
        return rows[0]
    return sb_update(
        user.id, {"estimator_tour_completed_at": datetime.now(timezone.utc).isoformat()}
    )


@router.get("/me/notification-prefs", response_model=NotificationPrefsOut)
def get_notification_prefs(user: CurrentUser = Depends(require_internal)):
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
def update_notification_prefs(
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
def reset_notification_prefs(user: CurrentUser = Depends(require_internal)):
    """Reset to role defaults — deletes the stored row and returns the presets."""
    get_supabase().table("notification_prefs").delete().eq(
        "user_id", user.id
    ).execute()
    audit(user.id, "user.notification_prefs.reset", "profile", user.id, {})
    return NotificationPrefsOut(prefs=default_prefs(user.role), is_customized=False)


@router.get("", response_model=list[ProfileOut])
def list_users(_: CurrentUser = Depends(_MANAGE_USERS)):
    return get_supabase().table("profiles").select("*").order("created_at").execute().data or []


@router.get("/teammates", response_model=list[TeammateOut])
def list_teammates(_: CurrentUser = Depends(require_internal)):
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
def invite_user(
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
def reinvite_user(
    user_id: str, admin: CurrentUser = Depends(_MANAGE_USERS)
):
    """Resend the invite email to a user who hasn't accepted yet."""
    sb = get_supabase()
    profile = _load_profile(user_id)
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
def reset_user_mfa(
    user_id: str, admin: CurrentUser = Depends(_MANAGE_USERS)
):
    """IT Admin / Executive removes a user's TOTP factor(s) — the recovery path
    for a locked-out user (Supabase TOTP has no backup codes). The user is forced
    to re-enroll on their next login.

    NOTE: deleting factors does not revoke an already-issued aal2 token (it stays
    valid up to its ~1h TTL); enforcement re-applies once that token expires and
    the next sign-in finds no factor.
    """
    _load_profile(user_id)
    _delete_user_factors(user_id)
    updated = sb_update(user_id, {"mfa_enrolled": False})
    audit(admin.id, "user.mfa.admin_reset", "profile", user_id, {})
    return updated


@router.post(
    "/{user_id}/reset-password",
    response_model=ProfileOut,
    dependencies=[Depends(outbound_email_rate_limit)],
)
def reset_user_password(user_id: str, admin: CurrentUser = Depends(_MANAGE_USERS)):
    """Email a user a password-reset link on their behalf.

    The self-service route is /auth/forgot-password; this is for the user who
    cannot get that far: wrong address on file, mail never arrived, or they are
    simply locked out and on the phone with an admin. The link carries a
    `recovery` token through the same /auth/confirm handler as invites and lands
    on /auth/reset-password.

    Sending to a not-yet-accepted invite is refused: a recovery link would drop
    that user into the set-a-password screen without ever stamping
    `invite_accepted_at`, leaving the admin list showing "Invited" forever. Resend
    the invite instead.
    """
    profile = _load_profile(user_id)
    if profile.get("invite_accepted_at") is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "User hasn't accepted their invite yet. Resend the invite instead.",
        )

    redirect_to = f"{get_settings().frontend_url}{_RECOVERY_REDIRECT_PATH}"
    if invite_email.graph_configured():
        try:
            link = get_supabase().auth.admin.generate_link(
                {
                    "type": "recovery",
                    "email": profile["email"],
                    "options": {"redirect_to": redirect_to},
                }
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("bdr.users").exception("Recovery link generation failed")
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Password reset failed. Try again."
            ) from exc
        try:
            invite_email.send_password_reset_email(
                to=profile["email"],
                full_name=profile["full_name"],
                cta_url=_confirm_url(link.properties, _RESET_PASSWORD_PATH),
                sent_by=admin.id,
            )
        except Exception as exc:  # noqa: BLE001 (nothing to roll back; admin retries)
            logging.getLogger("bdr.users").exception("Password reset send failed")
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Reset email could not be sent. Check Graph configuration.",
            ) from exc
    else:
        # Local/test fallback: Supabase sends its own (unbranded) recovery email.
        try:
            get_supabase().auth.reset_password_email(
                profile["email"], {"redirect_to": redirect_to}
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("bdr.users").exception("Password reset send failed")
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "Reset email could not be sent."
            ) from exc

    audit(admin.id, "user.password_reset_sent", "profile", user_id, {})
    return profile


@router.patch("/me/role", response_model=ProfileOut)
def switch_own_role(
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
def update_user(
    user_id: str,
    body: AdminUpdateUserIn | None = None,
    role: Role | None = None,
    is_active: bool | None = None,
    is_dev: bool | None = None,
    admin: CurrentUser = Depends(_MANAGE_USERS),
):
    """Edit another user's account: name, login email, role, enabled, dev flag.

    Role/is_active/is_dev are accepted BOTH as query params (the original wire
    format, still used by older callers) and in the JSON body; the body wins when
    a field appears in both. Name and email are body-only, since an email does not
    belong in a query string that lands in access logs.
    """
    edit = body or AdminUpdateUserIn()
    role = edit.role if edit.role is not None else role
    is_active = edit.is_active if edit.is_active is not None else is_active
    is_dev = edit.is_dev if edit.is_dev is not None else is_dev

    target = _load_profile(user_id)
    if user_id == admin.id and is_active is False:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You cannot disable your own account"
        )
    _guard_admin_coverage(target, role=role, is_active=is_active)

    patch: dict = {}
    if role is not None:
        patch["role"] = role.value
    if is_active is not None:
        patch["is_active"] = is_active
    # Admin grant/revoke of the dev flag (the role-switch backdoor). Having a
    # revocation path here means a stray is_dev can be cleared without a DB edit.
    if is_dev is not None:
        patch["is_dev"] = is_dev
    if edit.full_name is not None:
        patch["full_name"] = edit.full_name

    # GoTrue stores addresses lowercased; normalize to match so the profile row
    # and the auth identity can never drift apart (the email-ingest matcher and
    # the login lookup both key off the address).
    new_email: str | None = None
    if edit.email is not None:
        candidate = edit.email.strip().lower()
        if candidate != (target.get("email") or "").strip().lower():
            new_email = candidate

    if not patch and new_email is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to update")

    if new_email is not None:
        _assert_email_available(new_email, user_id)
        _set_auth_email(user_id, new_email)
        patch["email"] = new_email

    try:
        updated = sb_update(user_id, patch)
    except Exception:
        # The auth identity already moved; put it back rather than leave the user
        # signing in with an address their profile doesn't know about.
        if new_email is not None:
            _set_auth_email(user_id, target["email"], best_effort=True)
        raise

    audit(admin.id, "user.update", "profile", user_id, patch)
    return updated


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: str, admin: CurrentUser = Depends(_MANAGE_USERS)):
    """Permanently delete an account: the last resort behind Disable.

    Deleting the Supabase Auth user cascades the `profiles` row (migration 0002),
    and migration 0012 already shaped every actor FK for this: nullable "who did
    it" columns go NULL and NOT NULL ownership rows cascade, so project history,
    emails sent and the audit trail all outlive the account. What does NOT
    survive is the person's go/no-go votes and estimator assignments, which is
    why disabling remains the recommended action for someone who has left.
    """
    if user_id == admin.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You cannot delete your own account"
        )
    target = _load_profile(user_id)
    _guard_admin_coverage(target, deleting=True)

    sb = get_supabase()
    try:
        sb.auth.admin.delete_user(user_id)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("bdr.users").exception("User deletion failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Could not delete the account. Try again."
        ) from exc
    # The auth delete cascades the profile; this covers a profile row that has
    # somehow outlived its auth user, and is a no-op in the normal case.
    sb.table("profiles").delete().eq("id", user_id).execute()

    # Recorded after the fact so a failed delete leaves no misleading entry. The
    # email and role go into the metadata because the row they came from is gone.
    audit(
        admin.id,
        "user.delete",
        "profile",
        user_id,
        {"email": target.get("email"), "role": target.get("role")},
    )


def _assert_email_available(email: str, user_id: str) -> None:
    """409 if another profile already holds this address (profiles.email is unique).

    Checked before touching Supabase Auth so the common typo (retyping a
    teammate's address) fails cleanly instead of half-applying.
    """
    taken = (
        get_supabase()
        .table("profiles")
        .select("id")
        .eq("email", email)
        .neq("id", user_id)
        .limit(1)
        .execute()
    ).data
    if taken:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That email is already used by another user"
        )


def _set_auth_email(user_id: str, email: str, *, best_effort: bool = False) -> None:
    """Move the Supabase Auth login address for a user.

    `email_confirm` marks the new address verified on the spot: an admin typing a
    colleague's corrected address IS the verification, and without it the user
    would be unable to sign in until they clicked a confirmation mail sent to the
    address that was wrong in the first place.

    `best_effort` is for the rollback path, where raising would mask the original
    error that triggered it.
    """
    try:
        get_supabase().auth.admin.update_user_by_id(
            user_id, {"email": email, "email_confirm": True}
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("bdr.users").exception("Auth email change failed")
        if best_effort:
            return
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Could not change the email address. It may already be in use.",
        ) from exc


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
