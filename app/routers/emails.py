"""Ingested emails: per-project lists, the Unknown triage pool, detail +
attachment downloads, and manual assign/unassign (learn-back).

Emails attach to ANY project (bidding or PM), so this router lives at /emails
rather than under /pm. Reads are all internal roles (accountant read-only);
assign/unassign are writer roles. The external estimator never reaches these
routes (require_internal rejects it).

Handlers are plain `def` — the sync Supabase SDK runs in FastAPI's threadpool.
"""

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.deps import CurrentUser, require_internal, require_writer
from app.core.supabase_client import get_supabase
from app.models.schemas import EmailAssignIn
from app.services import email_ingest, storage

router = APIRouter(prefix="/emails", tags=["emails"])

_PENDING_STATUSES = ["received", "id_r1", "id_r2", "id_r3"]
_TERMINAL_STATUSES = ["processed", "failed"]

# List rows exclude body_text (capped but potentially large) — the detail
# endpoint serves it lazily on expand.
_LIST_SELECT = (
    "id, mailbox, folder, direction, conversation_id, from_name, from_address, "
    "to_recipients, cc_recipients, subject, body_preview, message_at, "
    "has_attachments, status, error, project_id, matched_by, match_confidence, "
    "match_model, pipeline_round, suggested_project_id, suggested_confidence, "
    "assigned_at, processed_at, created_at"
)
# The suggested-project embed must name the FK — ingested_emails has two FKs
# to projects (project_id and suggested_project_id).
_SUGGESTED_EMBED = (
    ", suggested_project:projects!ingested_emails_suggested_project_id_fkey"
    "(id, name, number)"
)


def _require_uuid(value: str, what: str) -> None:
    """A malformed uuid would surface from PostgREST as a 22P02 APIError — an
    unhandled 500 that loses its CORS headers (the 'Failed to fetch' trap).
    Reject it up front as a clean 404 instead."""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found") from None


def _sanitize_query(q: str) -> str:
    """Strip characters that would break the PostgREST or_() filter syntax or
    act as ilike wildcards (% _ and the * alias); plain substring search only,
    capped so a huge q can't inflate the request."""
    return re.sub(r"[,()%_*\\]", " ", q[:200]).strip()


def _search_filter(query, q: str | None):
    if q:
        term = _sanitize_query(q)
        if term:
            query = query.or_(
                f"subject.ilike.%{term}%,"
                f"from_address.ilike.%{term}%,"
                f"from_name.ilike.%{term}%"
            )
    return query


@router.get("")
def list_project_emails(
    project_id: str,
    q: str | None = None,
    direction: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    user: CurrentUser = Depends(require_internal),
):
    """Emails assigned to one project, newest first."""
    _require_uuid(project_id, "Project")
    if direction is not None and direction not in ("inbound", "outbound"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown direction: {direction}")
    query = (
        get_supabase()
        .table("ingested_emails")
        .select(_LIST_SELECT, count="exact")
        .eq("project_id", project_id)
    )
    if direction:
        query = query.eq("direction", direction)
    query = _search_filter(query, q)
    resp = (
        query.order("message_at", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {
        "rows": resp.data or [],
        "total": resp.count or 0,
        "offset": offset,
        "limit": limit,
    }


@router.get("/unknown")  # declared before /{email_id} so it isn't shadowed
def list_unknown_emails(
    q: str | None = None,
    include_failed: bool = True,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    user: CurrentUser = Depends(require_internal),
):
    """The Unknown pool: unassigned terminal emails (failed included so they
    stay triageable), plus whole-mailbox pipeline-health counts."""
    sb = get_supabase()
    statuses = _TERMINAL_STATUSES if include_failed else ["processed"]
    query = (
        sb.table("ingested_emails")
        .select(_LIST_SELECT + _SUGGESTED_EMBED, count="exact")
        .is_("project_id", "null")
        .in_("status", statuses)
    )
    query = _search_filter(query, q)
    resp = (
        query.order("message_at", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )

    pending = (
        sb.table("ingested_emails")
        .select("id", count="exact")
        .in_("status", _PENDING_STATUSES)
        .limit(1)
        .execute()
    )
    failed = (
        sb.table("ingested_emails")
        .select("id", count="exact")
        .is_("project_id", "null")
        .eq("status", "failed")
        .limit(1)
        .execute()
    )
    return {
        "rows": resp.data or [],
        "total": resp.count or 0,
        "offset": offset,
        "limit": limit,
        "pending_count": pending.count or 0,
        "failed_count": failed.count or 0,
    }


def _fetch_email(email_id: str) -> dict:
    _require_uuid(email_id, "Email")
    rows = (
        get_supabase()
        .table("ingested_emails")
        .select("*")
        .eq("id", email_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Email not found")
    return rows[0]


@router.get("/{email_id}")
def get_email(email_id: str, user: CurrentUser = Depends(require_internal)):
    """Full email incl. body_text and attachment metadata."""
    email = _fetch_email(email_id)
    attachments = (
        get_supabase()
        .table("ingested_email_attachments")
        .select("id, filename, mime_type, size_bytes, storage_path, skipped_reason")
        .eq("email_id", email_id)
        .order("created_at", desc=False)
        .execute()
    ).data or []
    email["attachments"] = [
        {
            "id": a["id"],
            "filename": a["filename"],
            "mime_type": a.get("mime_type"),
            "size_bytes": a.get("size_bytes"),
            "stored": a.get("storage_path") is not None,
            "skipped_reason": a.get("skipped_reason"),
        }
        for a in attachments
    ]
    return email


@router.get("/{email_id}/attachments/{attachment_id}/download")
def download_attachment(
    email_id: str,
    attachment_id: str,
    user: CurrentUser = Depends(require_internal),
):
    _require_uuid(email_id, "Email")
    _require_uuid(attachment_id, "Attachment")
    rows = (
        get_supabase()
        .table("ingested_email_attachments")
        .select("id, filename, storage_path")
        .eq("id", attachment_id)
        .eq("email_id", email_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    att = rows[0]
    if not att.get("storage_path"):
        # Metadata-only row (too large / too many / item attachment) — the
        # content was never stored.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment_not_stored")
    # download= serves Content-Disposition: attachment, defusing stored
    # HTML/SVG masquerading as another type.
    url = storage.signed_url(att["storage_path"], download=att["filename"])
    return {"url": url, "filename": att["filename"]}


@router.post("/{email_id}/assign")
def assign_email(
    email_id: str,
    body: EmailAssignIn,
    user: CurrentUser = Depends(require_writer),
):
    """Manually assign an email to a project (learn-back: teaches the
    conversation map and retro-assigns the rest of the conversation)."""
    sb = get_supabase()
    email = _fetch_email(email_id)
    _require_uuid(body.project_id, "Project")
    project = (
        sb.table("projects").select("id").eq("id", body.project_id).execute()
    ).data
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    updated, retro_count = email_ingest.assign_manual(sb, email, body.project_id, user.id)
    return {"email": updated, "retro_assigned_count": retro_count}


@router.post("/{email_id}/unassign")
def unassign_email(
    email_id: str,
    user: CurrentUser = Depends(require_writer),
):
    """Return an email to the Unknown pool for re-triage."""
    sb = get_supabase()
    email = _fetch_email(email_id)
    updated = email_ingest.unassign(sb, email, user.id)
    return {"email": updated}
