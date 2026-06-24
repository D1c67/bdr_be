"""Security alerting for the hardened estimator: denied-access bursts.

Every denial is written to audit_log. If a single account accumulates more than
`denied_access_alert_threshold` denials within the window, IT Admins get an
in-app notification so a possible probing attempt is surfaced quickly.
"""

from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.services.notifications import audit, notify_role


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
    if count >= settings.denied_access_alert_threshold:
        notify_role(
            Role.IT_ADMIN,
            project_id,
            "security_alert",
            f"Estimator account {user_id} hit {count} denied-access attempts "
            f"in {settings.denied_access_alert_window_min} min.",
        )
