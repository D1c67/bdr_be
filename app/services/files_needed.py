"""The files flag on a project (docs/RFP_BUILDINGCONNECTED.md 3.8, scratchpad
BUILD_CONTRACT.md D19).

BuildingConnected has no file API, so a project it creates (and a project a
BuildingConnected invitation merges onto when the project has no drawings
or specifications yet) carries a "pull the files" flag with the deep link:
`projects.files_needed_source`, `files_needed_url`, `files_needed_set_at`,
`files_needed_cleared_at`, `files_needed_cleared_by` (migration 0136).

The flag clears in two ways:

- automatically, from every path that inserts a `project_files` row
  (routers/files.upload_file, rfp_create_files._promote_one,
  rfp_split._insert_row, routers/bid_drafts transfer) when the new row's
  category is a drawing set or a specification (CLEAR_CATEGORIES is
  workflow's DRAWING_CATEGORIES plus `specification`, the same set the
  "upload at least one drawing" intake gate reads); best effort, never a
  reason for the insert to fail;
- by hand, `POST /projects/{id}/files-needed/dismiss`.

Everything here uses the sync Supabase client: callers are plain `def`
routes, the queue worker, or `run_in_threadpool` from the async upload
handler; never an `async def` directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.file_categories import DRAWING_CATEGORIES
from app.services.notifications import audit

logger = logging.getLogger(__name__)

SOURCE_BC = "buildingconnected"
CLEAR_CATEGORIES = frozenset(DRAWING_CATEGORIES | {"specification"})

AUDIT_CLEARED = "project.files_needed_cleared"
AUDIT_DISMISSED = "project.files_needed_dismiss"

_FLAG_SELECT = "id, files_needed_source, files_needed_url, files_needed_set_at, files_needed_cleared_at"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_flagged(project: dict | None) -> bool:
    """Set and not cleared, off a projects row (or any dict carrying the
    two columns)."""
    if not project:
        return False
    return bool(project.get("files_needed_set_at")) and not project.get("files_needed_cleared_at")


def set_needed(sb, project_id: str, *, source: str, url: str | None) -> None:
    """Raise the flag. Idempotent: a project whose flag is already set and
    uncleared is left exactly as it is (the first source and link stand); a
    project with no flag, or one whose earlier flag was cleared, gets a fresh
    one. A missing project is a no-op."""
    rows = (
        sb.table("projects").select(_FLAG_SELECT).eq("id", project_id).limit(1).execute()
    ).data or []
    if not rows:
        return
    if is_flagged(rows[0]):
        return
    sb.table("projects").update(
        {
            "files_needed_source": source,
            "files_needed_url": url,
            "files_needed_set_at": _now_iso(),
            "files_needed_cleared_at": None,
            "files_needed_cleared_by": None,
        }
    ).eq("id", project_id).execute()


def project_has_files(sb, project_id: str) -> bool:
    """Whether any project_files row in CLEAR_CATEGORIES exists: the test
    an `exists` merge runs before raising the flag (D19)."""
    rows = (
        sb.table("project_files")
        .select("id")
        .eq("project_id", project_id)
        .in_("category", sorted(CLEAR_CATEGORIES))
        .limit(1)
        .execute()
    ).data or []
    return bool(rows)


def _clear(sb, project_id: str, actor_id: str | None) -> dict | None:
    """The conditional update: only a set-and-uncleared flag moves. Returns
    the updated projects row, or None when there was nothing to clear."""
    rows = (
        sb.table("projects")
        .update({"files_needed_cleared_at": _now_iso(), "files_needed_cleared_by": actor_id})
        .eq("id", project_id)
        .not_.is_("files_needed_set_at", "null")
        .is_("files_needed_cleared_at", "null")
        .execute()
    ).data or []
    return rows[0] if rows else None


def clear_if_satisfied(sb, project_id: str | None, category: str | None, actor_id: str | None) -> bool:
    """After a project_files insert: clear the flag when the new row's
    category satisfies it. Best effort in every direction (D19): a category
    outside CLEAR_CATEGORIES, a missing project, an unflagged one, or any
    failure is False, logged, and never raised; the file is already in."""
    if not project_id or category not in CLEAR_CATEGORIES:
        return False
    try:
        row = _clear(sb, project_id, actor_id)
    except Exception:  # noqa: BLE001 - the insert stands whatever happens here
        logger.exception("files needed: auto-clear failed for project %s", project_id)
        return False
    if row is None:
        return False
    try:
        audit(actor_id, AUDIT_CLEARED, "project", project_id,
              {"category": category, "source": row.get("files_needed_source")})
    except Exception:  # noqa: BLE001
        logger.exception("files needed: audit failed for project %s", project_id)
    return True


def dismiss(sb, project_id: str, actor_id: str | None) -> dict | None:
    """The Dismiss button: clear the flag by hand. Returns the updated
    projects row, or None when the flag was not set (or already cleared);
    audited only when something moved."""
    row = _clear(sb, project_id, actor_id)
    if row is None:
        return None
    audit(actor_id, AUDIT_DISMISSED, "project", project_id, {"source": row.get("files_needed_source")})
    return row
