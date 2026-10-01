"""Security alerting for the hardened estimator: denied-access bursts.

Every denial is written to audit_log. If a single account accumulates more than
`denied_access_alert_threshold` denials within the window, IT Admins get an
in-app notification so a possible probing attempt is surfaced quickly. The
alert is latched per account: at most one per ALERT_COOLDOWN_MIN (recorded as
a `security_alert.raised` audit row), so a looping client cannot flood the IT
Admin bell and mailbox. The next alert after the cooldown carries the count of
denials since the previous one.
"""

import threading
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.services.notifications import audit, notify_role

ALERT_COOLDOWN_MIN = 30
ALERT_AUDIT_ACTION = "security_alert.raised"
_alert_lock = threading.Lock()


def record_denied_access(user_id: str, project_id: str | None, reason: str) -> None:
    from app.core.roles import Role  # local import to avoid a cycle

    settings = get_settings()
    audit(user_id, "access.denied", "project", project_id, {"reason": reason})

    window_start = datetime.now(timezone.utc) - timedelta(
        minutes=settings.denied_access_alert_window_min
    )
    recent = (
        get_supabase()
        .table("audit_log")
        .select("id", count="exact")
        .eq("actor_id", user_id)
        .eq("action", "access.denied")
        .gte("created_at", window_start.isoformat())
        .execute()
    )
    count = recent.count or 0
    if count < settings.denied_access_alert_threshold:
        return

    # Latch: at most one IT Admin alert per account per ALERT_COOLDOWN_MIN.
    # Denials inside the cooldown are still audited above; the next alert
    # reports how many piled up since the previous one.
    with _alert_lock:
        sb = get_supabase()
        now = datetime.now(timezone.utc)
        cooldown_start = now - timedelta(minutes=ALERT_COOLDOWN_MIN)
        recent_alert = (
            sb.table("audit_log")
            .select("id")
            .eq("actor_id", user_id)
            .eq("action", ALERT_AUDIT_ACTION)
            .gte("created_at", cooldown_start.isoformat())
            .limit(1)
            .execute()
        )
        if recent_alert.data:
            return

        previous = (
            sb.table("audit_log")
            .select("created_at")
            .eq("actor_id", user_id)
            .eq("action", ALERT_AUDIT_ACTION)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        since_previous: int | None = None
        if previous.data:
            since = (
                sb.table("audit_log")
                .select("id", count="exact")
                .eq("actor_id", user_id)
                .eq("action", "access.denied")
                .gte("created_at", previous.data[0]["created_at"])
                .execute()
            )
            since_previous = since.count or 0

        audit(
            user_id,
            ALERT_AUDIT_ACTION,
            "project",
            project_id,
            {"denials_in_window": count, "denials_since_previous_alert": since_previous},
        )
        message = (
            f"Estimator account {user_id} hit {count} denied-access attempts "
            f"in {settings.denied_access_alert_window_min} min."
        )
        if since_previous is not None:
            message += f" {since_previous} denied attempts since the previous alert."
        notify_role(Role.IT_ADMIN, project_id, "security_alert", message)
