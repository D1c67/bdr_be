"""Branded Microsoft Graph send for user invites and admin password resets.

Gives invites the same G3 look as every other app email (navy/silver header, the
inline logo, the office-phone signature) and greets the invitee by name — using
the existing notification-email recipe (`render_notification_email` →
`graph_email.send_mail`) rather than Supabase's plain default invite email.

When Graph isn't configured (local/test), `graph_configured()` is False and the
caller falls back to Supabase's own invite email instead.
"""

from app.core.config import get_settings
from app.core.roles import Role
from app.services import graph_email
from app.services.email_branding import (
    LOGO_CONTENT_ID,
    LOGO_FILENAME,
    logo_bytes,
    render_notification_email,
)

# Human labels for the role line in the invite copy. The frontend has
# lib/labels.roleLabel; the backend has no equivalent, so keep this small map
# local to the invite text.
_ROLE_LABELS: dict[Role, str] = {
    Role.ESTIMATING_ENGINEER_MATERIALS: "Estimating Engineer (Materials)",
    Role.ESTIMATING_ENGINEER_LABOR: "Estimating Engineer (Labor)",
    Role.ESTIMATING_ADMIN: "Estimating Admin",
    Role.EXECUTIVE: "Executive",
    Role.ACCOUNTANT: "Accountant",
    Role.IT_ADMIN: "IT Admin",
    Role.ESTIMATOR: "Estimator",
}


def graph_configured() -> bool:
    """True when Microsoft Graph creds are present (so invites send branded mail).

    False locally and in tests, where the caller falls back to Supabase's own
    invite email — mirroring how `notification_email` self-gates on `ms_client_id`.
    """
    return bool(get_settings().ms_client_id)


def send_invite_email(
    *, to: str, full_name: str, role: Role, cta_url: str, sent_by: str | None
) -> None:
    """Render and send one branded G3 invite email.

    Raises on send failure (`graph_email.send_mail` records the `email_log` row and
    re-raises); the caller uses that to roll the invite back.
    """
    role_label = _ROLE_LABELS.get(role, role.value)
    message = (
        f"You've been invited to BDR, G3 Electrical's bidding workspace, as a "
        f"{role_label}. Set your password to finish setting up your account and "
        f"get started."
    )
    html = render_notification_email(
        recipient_name=full_name,
        heading="You've been invited to BDR",
        message=message,
        cta_label="Accept your invite",
        cta_url=cta_url,
    )
    graph_email.send_mail(
        to=[to],
        subject="G3 BDR · You've been invited",
        body_html=html,
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        sent_by=sent_by,
    )


def send_password_reset_email(
    *, to: str, full_name: str, cta_url: str, sent_by: str | None
) -> None:
    """Render and send one branded G3 password-reset email (admin-initiated).

    Same recipe as the invite above. The copy says an admin asked for this, so a
    reset the user did not request themselves reads as expected rather than as a
    phishing attempt. Raises on send failure; the caller surfaces a 502.
    """
    html = render_notification_email(
        recipient_name=full_name,
        heading="Reset your BDR password",
        message=(
            "An administrator has started a password reset for your BDR account. "
            "Use the link below to set a new password. If you weren't expecting "
            "this, you can ignore this email and your password will stay as it is."
        ),
        cta_label="Set a new password",
        cta_url=cta_url,
    )
    graph_email.send_mail(
        to=[to],
        subject="G3 BDR · Reset your password",
        body_html=html,
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        sent_by=sent_by,
    )
