"""The Deleted Projects page: /deleted-projects (docs/PROJECT_DELETE.md 7).

Every project deletion (services/project_delete, migration 0141), newest
first, with who deleted it, when, the reason and the note, whether RFP
ingestion made it, and its restore. Restore puts the project back exactly as
it was, one transaction in the database; the archive row stays (restored_at,
restored_by filled) so the trail is permanent.

Access: a Bidding route (mounted with the BIDDING feature guard in
app/main.py), IT Admin only on every route. Every handler is a plain `def`
(the Supabase SDK is sync).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_role
from app.core.error_codes import RateLimitScope
from app.core.ratelimit import rate_limit
from app.core.roles import PROJECT_RESTORE_ROLES
from app.core.supabase_client import get_supabase
from app.services import project_delete

router = APIRouter(prefix="/deleted-projects", tags=["deleted-projects"])

require_restore = require_role(*PROJECT_RESTORE_ROLES)
deleted_projects_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

_NOT_FOUND = "Deleted project record not found."
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

Restored = Literal["false", "true", "all"]


def _uuid_or(value: str | None, message: str, code: int) -> str:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(code, message) from None
    return str(value)


def _parse_before(value: str | None) -> str | None:
    """An ISO timestamp (naive = UTC) as a UTC ISO string; blank = None, junk = 400."""
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "before must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _error(exc: project_delete.ProjectDeleteError) -> HTTPException:
    headers = {"X-Error-Code": f"project_delete_{exc.code}"} if exc.code else None
    return HTTPException(exc.status_code, str(exc), headers=headers)


# Literal paths first (the codebase's literal-before-param ordering rule).


@router.get("/summary", dependencies=[Depends(deleted_projects_rate_limit)])
def deleted_projects_summary(_: CurrentUser = Depends(require_restore)) -> dict:
    """`{total, not_restored, restored, by_reason: {code: n}}`: exact counts
    (by_reason covers every deletion, restored or not, so "how often RFP
    ingestion was wrong" is `by_reason.rfp_should_not_exist`)."""
    return project_delete.summary(get_supabase())


@router.get("", dependencies=[Depends(deleted_projects_rate_limit)])
def list_deleted_projects(
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    before: str | None = Query(default=None, max_length=64),
    before_id: str | None = Query(default=None, max_length=64),
    reason: str | None = Query(default=None, max_length=64),
    restored: Restored = "false",
    _: CurrentUser = Depends(require_restore),
) -> list[dict]:
    """Newest first (`deleted_at desc, id desc`), paged on the last row's
    `(deleted_at, id)`: `before` is its `deleted_at`, `before_id` its id.
    `restored=false` (default) lists the projects still deleted, `true` the
    restored ones, `all` both; `reason` narrows to one reason code (400 on
    an unknown one)."""
    if reason is not None and reason not in project_delete.REASONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown reason")
    before_iso = _parse_before(before)
    before_uuid = (
        _uuid_or(before_id, "before_id must be a deleted project id", status.HTTP_400_BAD_REQUEST)
        if before_id else None
    )
    return project_delete.list_deleted(
        get_supabase(), limit=limit, before=before_iso, before_id=before_uuid, reason=reason,
        restored=restored,
    )


@router.post("/{archive_id}/restore", dependencies=[Depends(deleted_projects_rate_limit)])
def restore_deleted_project(archive_id: str, user: CurrentUser = Depends(require_restore)) -> dict:
    """Put the project back exactly as it was (its stage, files, RFQs,
    quotes and history), relink what pointed at it, and let its RFP source
    link to it again. Nothing is emailed. 404 unknown record; 409 with a
    sentence when it was already restored, its number is now another
    project's, or a record it depends on is gone. Audited `project.restore`
    (or `project.restore_failed`, with the failure kept on the record)."""
    _uuid_or(archive_id, _NOT_FOUND, status.HTTP_404_NOT_FOUND)
    try:
        return project_delete.restore(get_supabase(), archive_id, actor_id=user.id)
    except project_delete.ProjectDeleteError as exc:
        raise _error(exc) from exc
