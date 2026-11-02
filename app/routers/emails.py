"""Ingested emails: per-project lists, the Unknown triage pool, detail +
attachment downloads, and manual assign/unassign (learn-back).

Emails attach to ANY project (bidding or PM), so this router lives at /emails
rather than under /pm. Reads are all internal roles (accountant read-only);
assign/unassign are writer roles. The external estimator never reaches these
routes (require_internal rejects it).

Every list, count, detail, attachment, assign and unassign route is scoped by
mailbox through app/services/mailbox_visibility (docs/ASSISTANT.md, "PM
mailbox scoping fix"): a person sees their own mailboxes plus the shared set,
Executives and IT Admins see everything except the exclusion list and leave an
email.viewed_other_mailbox audit row whenever they open a detail row or an
attachment outside their own plus shared set. A row outside the scope 404s
with the same body a missing id gets.

PM email classification (docs/EMAIL_CLASSIFICATION.md): the project list
and the detail carry each email's `classification` (one extra query per
page, never per row), and PM writers can correct the labels (PUT), drop a
correction (DELETE .../override) or queue a fresh AI run (POST .../rerun).

Handlers are plain `def` — the sync Supabase SDK runs in FastAPI's threadpool.
"""

import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.deps import CurrentUser, require_internal, require_pm_write, require_writer
from app.core.supabase_client import get_supabase
from app.models.schemas import EmailAssignIn, EmailClassificationIn
from app.services import email_classification, email_ingest, file_access, storage
from app.services import mailbox_visibility as visibility
from app.services.notifications import audit

logger = logging.getLogger(__name__)

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


def _empty_envelope(offset: int, limit: int, **counts) -> dict:
    """Zero rows without a query: a viewer with nothing mapped and no shared
    mailbox configured may not see anything, and an empty `in_` filter is
    not something anyone should send."""
    return {"rows": [], "total": 0, "offset": offset, "limit": limit, **counts}


def _audit_other_mailbox(email: dict, user: CurrentUser) -> None:
    """The mandatory record of a full-view role (Executive, IT Admin) reading
    outside its own plus shared set. Never skipped: the human UI path carries
    the same trail the assistant's read_mailbox tool will."""
    if visibility.is_other_mailbox(email, user):
        audit(
            user.id,
            "email.viewed_other_mailbox",
            entity="ingested_email",
            entity_id=email["id"],
            payload={"mailbox": email.get("mailbox"), "email_id": email["id"]},
        )


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
    if visibility.visible_mailboxes(user).allowed == frozenset():
        return _empty_envelope(offset, limit)
    query = (
        get_supabase()
        .table("ingested_emails")
        .select(_LIST_SELECT, count="exact")
        .eq("project_id", project_id)
    )
    # Scope before the search filter so `total` counts only visible rows.
    query = visibility.apply_scope(query, user)
    if direction:
        query = query.eq("direction", direction)
    query = _search_filter(query, q)
    resp = (
        query.order("message_at", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = resp.data or []
    email_classification.attach(get_supabase(), rows)
    return {
        "rows": rows,
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
    stay triageable), plus pipeline-health counts. Rows and both counts are
    scoped to the viewer's mailboxes, so nobody learns another mailbox's
    volume from the badges."""
    if visibility.visible_mailboxes(user).allowed == frozenset():
        return _empty_envelope(offset, limit, pending_count=0, failed_count=0)
    sb = get_supabase()
    statuses = _TERMINAL_STATUSES if include_failed else ["processed"]
    query = (
        sb.table("ingested_emails")
        .select(_LIST_SELECT + _SUGGESTED_EMBED, count="exact")
        .is_("project_id", "null")
        .in_("status", statuses)
    )
    query = visibility.apply_scope(query, user)
    query = _search_filter(query, q)
    resp = (
        query.order("message_at", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )

    pending = (
        visibility.apply_scope(
            sb.table("ingested_emails")
            .select("id", count="exact")
            .in_("status", _PENDING_STATUSES),
            user,
        )
        .limit(1)
        .execute()
    )
    failed = (
        visibility.apply_scope(
            sb.table("ingested_emails")
            .select("id", count="exact")
            .is_("project_id", "null")
            .eq("status", "failed"),
            user,
        )
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


def _fetch_email(email_id: str, user: CurrentUser) -> dict:
    """Load one row the viewer may see. A row outside the scope raises the
    same 404 a missing id does (visibility.EMAIL_NOT_FOUND is this body, byte
    for byte); a full-view read outside own plus shared is audited."""
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
    email = rows[0]
    visibility.assert_visible(email, user)
    _audit_other_mailbox(email, user)
    return email


@router.get("/{email_id}")
def get_email(email_id: str, user: CurrentUser = Depends(require_internal)):
    """Full email incl. body_text and attachment metadata."""
    email = _fetch_email(email_id, user)
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
    email_classification.attach(get_supabase(), [email])
    return email


@router.get("/{email_id}/attachments/{attachment_id}/download")
def download_attachment(
    email_id: str,
    attachment_id: str,
    user: CurrentUser = Depends(require_internal),
):
    _require_uuid(email_id, "Email")
    _require_uuid(attachment_id, "Attachment")
    # The parent first: a refusal mints no signed URL and records no access,
    # and the parent's project_id is what the access row needs anyway.
    sb = get_supabase()
    erows = (
        sb.table("ingested_emails")
        .select("id, mailbox, project_id")
        .eq("id", email_id)
        .execute()
    ).data
    if not erows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Email not found")
    email = erows[0]
    visibility.assert_visible(email, user)
    _audit_other_mailbox(email, user)
    rows = (
        sb.table("ingested_email_attachments")
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
    file_access.record(
        user,
        action="download",
        source="email_attachment",
        file_id=attachment_id,
        project_id=email.get("project_id"),
        filename=att.get("filename"),
        detail={"email_id": email_id},
    )
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
    email = _fetch_email(email_id, user)
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
    email = _fetch_email(email_id, user)
    updated = email_ingest.unassign(sb, email, user.id)
    return {"email": updated}


# ── PM email classification (docs/EMAIL_CLASSIFICATION.md) ───────────────────


def _classified_email(email_id: str, user: CurrentUser) -> dict:
    """The email for a classification write: visible to the caller and filed
    to a PM project (409 otherwise: labels belong to PM mail)."""
    email = _fetch_email(email_id, user)
    if not email_classification.is_pm_project(get_supabase(), email.get("project_id")):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This email is not filed to a Project Management project."
        )
    return email


def _classification_out(email_id: str) -> dict:
    email = {"id": email_id}
    email_classification.attach(get_supabase(), [email])
    return {"email_id": email_id, "classification": email["classification"]}


@router.put("/{email_id}/classification")
def set_email_classification(
    email_id: str,
    body: EmailClassificationIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Replace the email's labels with a writer's correction. The AI labels
    are kept beside it and the change is logged (email_label_corrections)."""
    _classified_email(email_id, user)
    try:
        labels = email_classification.normalize_user_labels(
            [lab.model_dump() for lab in body.labels]
        )
    except email_classification.InvalidLabels as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    email_classification.set_user_labels(get_supabase(), email_id, labels, user.id)
    audit(user.id, "email.classification_override", "ingested_email", email_id,
          {"labels": [lab["category"] for lab in labels]})
    return _classification_out(email_id)


@router.delete("/{email_id}/classification/override")
def reset_email_classification(
    email_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    """Drop the writer's correction so the AI labels show again (logged)."""
    _classified_email(email_id, user)
    email_classification.reset_to_ai(get_supabase(), email_id, user.id)
    return _classification_out(email_id)


@router.post("/{email_id}/classification/rerun")
def rerun_email_classification(
    email_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    """Queue a fresh AI classification, even for an email that already has
    labels. A correction stays in force over the new AI labels."""
    _classified_email(email_id, user)
    if not email_classification.llm.is_configured(email_classification.FEATURE):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Email classification is switched off or the AI model server is not set up.",
        )
    email_classification.rerun(get_supabase(), email_id, user.id)
    return _classification_out(email_id)
