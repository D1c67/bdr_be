"""Estimator notes: a per-project thread between the internal team and the
external estimator (stage-agnostic — communication can happen at any step).

Both sides read and write the same thread. The estimator is gated by
`require_project_assignment` (+ rate limit, like files); a new note notifies
the other side through the in-app notification bell:
  internal author → the actively-assigned estimator(s)
  estimator author → PA/PM plus any other internal user already in the thread

@mentions (0160): the composer sends the ids of users picked from the "@"
menu. Only ids that are mentionable on the project (see `mentionable_users`)
and whose "@Full Name" is still in the body are kept. Each mentioned user gets
a `note_mention` bell row + email instead of the generic `estimator_note` one,
so an internal user can pull a teammate into the thread too.

Not every row here was typed into this panel: the batch-wide message left on a
package/update send is mirrored in as a note by the sender, tagged with a
non-null `source` (services/estimator_notes, 0080). Those arrive silently — no
bell, no email — because the same text already headed the send's own email.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from app.core.deps import CurrentUser, require_project_assignment
from app.core.ratelimit import estimator_rate_limit
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import notifications
from app.services.estimator_notes import NOTE_MAX_CHARS
from app.services.notifications import audit, dismiss_notifications

router = APIRouter(
    prefix="/projects/{project_id}/notes",
    tags=["notes"],
    dependencies=[Depends(estimator_rate_limit)],
)

_AUTHOR_JOIN = "author:profiles!estimator_notes_author_id_fkey"


# Bell types a read of the thread clears for the reader.
_NOTE_NOTIFICATION_TYPES = ["estimator_note", "note_mention"]


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=NOTE_MAX_CHARS)
    # Users picked from the composer's "@" menu. Filtered server side by
    # resolve_mentions, so a stale or forged id is simply dropped.
    mention_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)

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


def resolve_mentions(
    body: str, mention_ids: list[uuid.UUID] | list[str], mentionable: list[dict]
) -> list[str]:
    """The mention ids worth keeping, in the order they were picked.

    An id survives only if it is mentionable on the project and its
    "@Full Name" is still in the body (the writer may have deleted the text
    after picking the name). Matching ignores case. Deduped.
    """
    names = {str(m["id"]): (m.get("full_name") or "").strip() for m in mentionable}
    lowered = body.lower()
    out: list[str] = []
    for raw in mention_ids:
        uid = str(raw)
        name = names.get(uid)
        if name and f"@{name.lower()}" in lowered and uid not in out:
            out.append(uid)
    return out


def mentionable_users(sb, project_id: str) -> list[dict]:
    """Who can be @mentioned on this project: every active internal user plus
    the actively assigned estimator(s). Same assignment window as the
    estimator's access gate, so nobody is pulled into a thread they can't open.
    Only id, name and role: estimators call this too, so no emails."""
    internal = (
        sb.table("profiles")
        .select("id, full_name, role")
        .in_("role", [r.value for r in INTERNAL_ROLES])
        .eq("is_active", True)
        .execute()
    ).data or []
    assigns = (
        sb.table("estimator_assignments")
        .select("estimator:profiles!estimator_assignments_estimator_id_fkey(id, full_name, role, is_active)")
        .eq("project_id", project_id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .execute()
    ).data or []
    estimators = [
        {k: a["estimator"][k] for k in ("id", "full_name", "role")}
        for a in assigns
        if a.get("estimator") and a["estimator"].get("is_active")
    ]
    seen: set[str] = set()
    out: list[dict] = []
    for p in internal + estimators:
        if p["id"] in seen or not (p.get("full_name") or "").strip():
            continue
        seen.add(p["id"])
        out.append(p)
    out.sort(key=lambda p: p["full_name"].lower())
    return out


@router.get("/mentionable")
def list_mentionable(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    """The composer's "@" menu, minus the caller."""
    return [p for p in mentionable_users(get_supabase(), project_id) if p["id"] != user.id]


@router.get("")
def list_notes(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    sb = get_supabase()
    notes = (
        sb.table("estimator_notes")
        .select(
            "id, body, created_at, author_id, source, mentioned_user_ids, "
            f"{_AUTHOR_JOIN}(full_name, role)"
        )
        .eq("project_id", project_id)
        .order("created_at")
        .limit(500)
        .execute()
    ).data or []
    # Resolve mention ids to names in one query so the thread can highlight
    # "@Full Name". A deleted user just drops out of `mentions`.
    ids = sorted({uid for n in notes for uid in (n.get("mentioned_user_ids") or [])})
    names: dict[str, str] = {}
    if ids:
        names = {
            p["id"]: p["full_name"]
            for p in (
                sb.table("profiles").select("id, full_name").in_("id", ids).execute()
            ).data or []
            if p.get("full_name")
        }
    for n in notes:
        n["mentions"] = [
            {"id": uid, "full_name": names[uid]}
            for uid in (n.pop("mentioned_user_ids", None) or [])
            if uid in names
        ]
    return notes


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
        # still count — someone else wrote them. Validate user_id as a UUID
        # before interpolating it into the PostgREST or-filter so a non-UUID can
        # never smuggle extra filter clauses into the string.
        .or_(f"author_id.neq.{uuid.UUID(user_id)},author_id.is.null")
    )
    if last_read_iso is not None:
        q = q.gt("created_at", last_read_iso)
    return q.execute().count or 0


@router.get("/unread")
def unread_count(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    """How many notes the caller hasn't read yet — drives the side-menu badge."""
    sb = get_supabase()
    return {"count": _unread_count(sb, project_id, user.id, _last_read_at(sb, project_id, user.id))}


@router.post("/read")
def mark_read(
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
    dismiss_notifications(
        project_id=project_id, types=_NOTE_NOTIFICATION_TYPES, user_id=user.id
    )
    return {"count": _unread_count(sb, project_id, user.id, mark)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_note(
    project_id: str,
    payload: NoteIn,
    user: CurrentUser = Depends(require_project_assignment),
):
    # Writers or the (assigned) estimator may post; the read-only accountant may
    # read the thread but not write into it. require_project_assignment only
    # gates the estimator, so the accountant is otherwise let through to here.
    if user.role not in WRITER_ROLES and user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")

    sb = get_supabase()
    proj = (
        sb.table("projects").select("name, number").eq("id", project_id).single().execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    mentioned = (
        recipient_ids(
            user.id,
            resolve_mentions(
                payload.body, payload.mention_ids, mentionable_users(sb, project_id)
            ),
        )
        if payload.mention_ids
        else []
    )

    row = (
        sb.table("estimator_notes")
        .insert(
            {
                "project_id": project_id,
                "author_id": user.id,
                "body": payload.body,
                "mentioned_user_ids": mentioned,
            }
        )
        .execute()
    ).data[0]
    audit(
        user.id,
        "note.create",
        "project",
        project_id,
        {"note_id": row["id"], **({"mentioned_user_ids": mentioned} if mentioned else {})},
    )

    author = (
        sb.table("profiles").select("full_name").eq("id", user.id).single().execute()
    ).data
    name = (author or {}).get("full_name") or user.email
    where = f"{proj['name']} ({proj['number']})"
    preview = note_preview(payload.body)
    msg = f"Note from {name} on {where}: {preview}"

    if user.role == Role.ESTIMATOR:
        owners = (
            sb.table("profiles")
            .select("id")
            .in_(
                "role",
                [
                    Role.ESTIMATING_ADMIN.value,
                    Role.ESTIMATING_ENGINEER_MATERIALS.value,
                    Role.ESTIMATING_ENGINEER_LABOR.value,
                ],
            )
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

    # A mentioned user gets the mention notice INSTEAD of the generic one.
    mention_msg = f"{name} mentioned you on {where}: {preview}"
    notif_rows = [
        {"user_id": uid, "project_id": project_id, "type": "note_mention", "message": mention_msg}
        for uid in mentioned
    ] + [
        {"user_id": uid, "project_id": project_id, "type": "estimator_note", "message": msg}
        for uid in targets
        if uid not in mentioned
    ]
    if notif_rows:
        # The inserted rows carry the ids the mirror emails link back to (0091).
        inserted = (sb.table("notifications").insert(notif_rows).execute()).data or notif_rows
        notifications.deliver(inserted)
    return row
