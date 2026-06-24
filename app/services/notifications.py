"""In-app notifications and audit logging helpers.

Every notification we create is also mirrored to the recipient's inbox as a
branded email with a deep-link button (services/notification_email) — the email
send is fire-and-forget and best-effort, so it never blocks or breaks the
creating flow.
"""

import logging
from typing import Any

from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import notification_email

logger = logging.getLogger(__name__)


def notify_role(
    role: Role,
    project_id: str | None,
    type_: str,
    message: str,
    rfq_id: str | None = None,
) -> None:
    """Create a notification for every active user holding `role`.

    Refuses the estimator role outright: the estimator is an external, untrusted
    user scoped to assigned projects only, so an unscoped broadcast would leak a
    project's identity to estimators who aren't assigned to it (and, via the
    email mirror, to their external inbox). Estimators are notified exclusively
    through assignment-scoped `notify_user` calls (see files.py / notes.py).

    `rfq_id` ties the notification to a specific RFQ so it can be auto-dismissed
    when that category is priced (see `dismiss_notifications`).
    """
    if role == Role.ESTIMATOR:
        logger.warning(
            "Refusing to broadcast notification to all estimators (type=%s); "
            "estimator notifications must be assignment-scoped.",
            type_,
        )
        return
    sb = get_supabase()
    users = (
        sb.table("profiles")
        .select("id")
        .eq("role", role.value)
        .eq("is_active", True)
        .execute()
    ).data or []
    if not users:
        return
    rows = [
        {
            "user_id": u["id"],
            "project_id": project_id,
            "type": type_,
            "message": message,
            "rfq_id": rfq_id,
        }
        for u in users
    ]
    sb.table("notifications").insert(rows).execute()
    notification_email.queue(rows)


def notify_user(
    user_id: str,
    project_id: str | None,
    type_: str,
    message: str,
    rfq_id: str | None = None,
) -> None:
    row = {
        "user_id": user_id,
        "project_id": project_id,
        "type": type_,
        "message": message,
        "rfq_id": rfq_id,
    }
    get_supabase().table("notifications").insert(row).execute()
    notification_email.queue([row])


def dismiss_notifications(
    *,
    project_id: str | None = None,
    rfq_id: str | None = None,
    types: list[str] | None = None,
    type_prefixes: list[str] | None = None,
    user_id: str | None = None,
) -> None:
    """Soft-dismiss matching notifications so they drop out of the bell.

    Sets `dismissed_at` (distinct from `read_at`, which the user sets manually
    and which only greys the row); GET /notifications filters dismissed rows
    out entirely. Scope by `project_id` and/or `rfq_id`, optionally narrowed to
    a single `user_id` (per-user dismissals such as a read reply). `types`
    matches exact type strings; `type_prefixes` matches via SQL LIKE `<prefix>%`
    (for the `due.<kind>.<offset>` family). Requires a project_id or rfq_id
    scope so a dismissal can never sweep the whole table. The email mirror is
    already sent and is intentionally left untouched.

    Exact types and each prefix run as separate UPDATEs (in_ / like) rather than
    one combined or-filter — these are infrequent cleanup writes, and the native
    builder methods avoid any wildcard-escaping ambiguity.
    """
    if not (project_id or rfq_id):
        raise ValueError("dismiss_notifications needs a project_id or rfq_id scope")
    sb = get_supabase()

    def _scoped():
        q = sb.table("notifications").update({"dismissed_at": "now()"}).is_(
            "dismissed_at", "null"
        )
        if project_id:
            q = q.eq("project_id", project_id)
        if rfq_id:
            q = q.eq("rfq_id", rfq_id)
        if user_id:
            q = q.eq("user_id", user_id)
        return q

    if types:
        _scoped().in_("type", types).execute()
    for prefix in type_prefixes or []:
        _scoped().like("type", f"{prefix}%").execute()


def audit(
    actor_id: str | None,
    action: str,
    entity: str | None = None,
    entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append an audit-log entry (used heavily for estimator activity)."""
    get_supabase().table("audit_log").insert(
        {
            "actor_id": actor_id,
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "payload": payload,
        }
    ).execute()
