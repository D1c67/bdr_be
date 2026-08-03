"""The per-project notification log.

One route, read-only, serving both modules: a project keeps the same
`projects.id` when a won bid enters Project Management, so the bidding page and
the PM page read the identical log — nothing is copied or forked at handoff.

Access is `require_internal` (the writer roles plus the read-only accountant),
matching every other project-wide read view. The external estimator is
deliberately excluded: this log names every other recipient of every notice,
including vendor and GC contact addresses, and an estimator is scoped to their
own assignments.

Handlers are plain `def` — the sync Supabase SDK runs in FastAPI's threadpool.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_internal
from app.core.ratelimit import notification_log_rate_limit
from app.core.supabase_client import get_supabase
from app.services import notification_log

router = APIRouter(prefix="/projects", tags=["notification-log"])


@router.get(
    "/{project_id}/notification-log",
    dependencies=[Depends(notification_log_rate_limit)],
)
def project_notification_log(
    project_id: str,
    user: CurrentUser = Depends(require_internal),
):
    """Every notification and email this project sent, newest event first."""
    try:
        # A malformed uuid reaches PostgREST as a 22P02 APIError — an unhandled
        # 500 that loses its CORS headers (the "Failed to fetch" trap). Reject
        # it up front as a clean 404 instead.
        uuid.UUID(project_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from None

    exists = (
        get_supabase().table("projects").select("id").eq("id", project_id).execute()
    ).data
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    return notification_log.build(project_id)
