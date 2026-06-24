"""Estimator notes: a per-project thread between the internal team and the
external estimator (stage-agnostic — communication can happen at any step).

Both sides read and write the same thread. The estimator is gated by
`require_project_assignment` (+ rate limit, like files); a new note notifies
the other side through the in-app notification bell:
  internal author → the actively-assigned estimator(s)
  estimator author → PA/PM plus any other internal user already in the thread
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from app.core.deps import CurrentUser, require_project_assignment
from app.core.ratelimit import estimator_rate_limit
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import notification_email
from app.services.notifications import audit, dismiss_notifications

router = APIRouter(
    prefix="/projects/{project_id}/notes",
    tags=["notes"],
    dependencies=[Depends(estimator_rate_limit)],
)

NOTE_MAX_CHARS = 4000
_AUTHOR_JOIN = "author:profiles!estimator_notes_author_id_fkey"


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=NOTE_MAX_CHARS)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Note cannot be empty")
        return v


class ReadIn(BaseModel):
    # The created_at of the newest note the client actually rendered — echoed
    # server data, so no clock-skew concerns. Aware-only so comparisons with
    # stored timestamptz values can never raise.
    up_to: AwareDatetime


def advance_read_mark(existing_iso: str | None, up_to: datetime) -> str:
    """The new read high-water mark: max(existing, up_to), as ISO.

    Never moves backwards, so a stale or racing mark (second tab, slow
    request) can't resurrect already-read notes.
    """
    if existing_iso is not None:
        existing = datetime.fromisoformat(existing_iso)
        if existing >= up_to:
            return existing_iso
    return up_to.isoformat()


def note_preview(body: str, limit: int = 120) -> str:
    """One-line preview of a note body for notification messages."""
    flat = " ".join(body.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def recipient_ids(author_id: str, *groups: list[str]) -> list[str]:
    """Merge recipient id groups, deduped, never notifying the author."""
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for uid in group:
            if uid and uid != author_id and uid not in seen:
                seen.add(uid)
                out.append(uid)
    return out


@router.get("")
async def list_notes(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    return (
        get_supabase()
        .table("estimator_notes")
        .select(f"id, body, created_at, author_id, {_AUTHOR_JOIN}(full_name, role)")
        .eq("project_id", project_id)
        .order("created_at")
        .limit(500)
        .execute()
    ).data or []


def _last_read_at(sb, project_id: str, user_id: str) -> str | None:
    rows = (
        sb.table("estimator_note_reads")
        .select("last_read_at")
        .eq("project_id", project_id)
        .eq("user_id", user_id)
        .execute()
    ).data
    return rows[0]["last_read_at"] if rows else None


def _unread_count(sb, project_id: str, user_id: str, last_read_iso: str | None) -> int:
    q = (
        sb.table("estimator_notes")
        .select("id", count="exact")
        .eq("project_id", project_id)
        # Your own notes are never "unread"; authorless notes (deleted user)
        # still count — someone else wrote them.
        .or_(f"author_id.neq.{user_id},author_id.is.null")
    )
    if last_read_iso is not None:
        q = q.gt("created_at", last_read_iso)
    return q.execute().count or 0


@router.get("/unread")
async def unread_count(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    """How many notes the caller hasn't read yet — drives the side-menu badge."""
    sb = get_supabase()
    return {"count": _unread_count(sb, project_id, user.id, _last_read_at(sb, project_id, user.id))}


@router.post("/read")
async def mark_read(
    project_id: str,
    payload: ReadIn,
    user: CurrentUser = Depends(require_project_assignment),
):
    """Advance the caller's read mark to the newest note they've rendered."""
    sb = get_supabase()
    proj = (
        sb.table("projects").select("id").eq("id", project_id).execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    mark = advance_read_mark(_last_read_at(sb, project_id, user.id), payload.up_to)
    sb.table("estimator_note_reads").upsert(
        {"project_id": project_id, "user_id": user.id, "last_read_at": mark},
        on_conflict="project_id,user_id",
    ).execute()
    # The caller has read the thread, so their reply notifications are handled.
    dismiss_notifications(project_id=project_id, types=["estimator_note"], user_id=user.id)
    return {"count": _unread_count(sb, project_id, user.id, mark)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_note(
    project_id: str,
    payload: NoteIn,
    user: CurrentUser = Depends(require_project_assignment),
):
    sb = get_supabase()
    proj = (
        sb.table("projects").select("name, number").eq("id", project_id).single().execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    row = (
        sb.table("estimator_notes")
        .insert({"project_id": project_id, "author_id": user.id, "body": payload.body})
        .execute()
    ).data[0]
    audit(user.id, "note.create", "project", project_id, {"note_id": row["id"]})

    author = (
        sb.table("profiles").select("full_name").eq("id", user.id).single().execute()
    ).data
    name = (author or {}).get("full_name") or user.email
    msg = (
        f"Note from {name} on {proj['name']} ({proj['number']}): "
        f"{note_preview(payload.body)}"
    )

    if user.role == Role.ESTIMATOR:
        owners = (
            sb.table("profiles")
            .select("id")
            .in_("role", [Role.PA.value, Role.PM.value])
            .eq("is_active", True)
            .execute()
        ).data or []
        prior = (
            sb.table("estimator_notes")
            .select(f"author_id, {_AUTHOR_JOIN}(role, is_active)")
            .eq("project_id", project_id)
            .execute()
        ).data or []
        internal_values = {r.value for r in INTERNAL_ROLES}
        participants = [
            p["author_id"]
            for p in prior
            if p.get("author")
            and p["author"].get("is_active")
            and p["author"].get("role") in internal_values
        ]
        targets = recipient_ids(user.id, [o["id"] for o in owners], participants)
    else:
        # Same active-assignment window as the estimator's access gate — no point
        # notifying someone who can no longer open the project.
        assigns = (
            sb.table("estimator_assignments")
            .select("estimator_id")
            .eq("project_id", project_id)
            .is_("revoked_at", "null")
            .or_("expires_at.is.null,expires_at.gt.now()")
            .execute()
        ).data or []
        targets = recipient_ids(user.id, [a["estimator_id"] for a in assigns])

    if targets:
        notif_rows = [
            {
                "user_id": uid,
                "project_id": project_id,
                "type": "estimator_note",
                "message": msg,
            }
            for uid in targets
        ]
        sb.table("notifications").insert(notif_rows).execute()
        notification_email.queue(notif_rows)
    return row
