"""Personal to-dos. Every internal user keeps a simple list; the To-Dos page
lets any internal teammate open another teammate's list read-only.

Reads accept an optional `user_id` (defaults to the caller); writes are
owner-only. The external estimator is excluded entirely (`require_internal`),
matching the rest of the internal shell.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.core.deps import CurrentUser, require_internal
from app.core.roles import INTERNAL_ROLES
from app.core.supabase_client import get_supabase
from app.services.notifications import notify_user

router = APIRouter(prefix="/todos", tags=["todos"])

TODO_MAX_CHARS = 500
_SELECT = "id, user_id, title, due_date, is_done, completed_at, created_at, last_nudged_at"

# How long a to-do stays "recently nudged" — rapid re-nudges are rejected so a
# teammate isn't spammed with duplicate pokes for the same task.
NUDGE_COOLDOWN = timedelta(hours=3)


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=TODO_MAX_CHARS)
    due_date: date | None = None

    @field_validator("title")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Title cannot be empty")
        return v


class TodoPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=TODO_MAX_CHARS)
    due_date: date | None = None
    is_done: bool | None = None

    @field_validator("title")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


def db_patch(payload: TodoPatch) -> dict:
    """Translate a PATCH body into the DB update dict.

    `exclude_unset` distinguishes an omitted field from an explicit null, so a
    client clears `due_date` by sending null (the projects PATCH convention).
    `completed_at` is owned by the `is_done` transition and never set directly.
    """
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to update")
    if "title" in patch and (patch["title"] is None or not patch["title"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Title cannot be empty")
    if isinstance(patch.get("due_date"), date):
        patch["due_date"] = patch["due_date"].isoformat()
    if "is_done" in patch:
        patch["completed_at"] = (
            datetime.now(timezone.utc).isoformat() if patch["is_done"] else None
        )
    return patch


def _nudge_blocked(last_nudged_at: datetime | None, now: datetime) -> bool:
    """True if the to-do was already nudged within the cooldown window."""
    return last_nudged_at is not None and now - last_nudged_at < NUDGE_COOLDOWN


def _own_todo(todo_id: UUID, user: CurrentUser) -> None:
    """404 for an unknown to-do, 403 for someone else's."""
    rows = (
        get_supabase()
        .table("todos")
        .select("id, user_id")
        .eq("id", str(todo_id))
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "To-do not found")
    if rows[0]["user_id"] != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the owner can modify a to-do"
        )


@router.get("")
async def list_todos(
    user_id: UUID | None = None, user: CurrentUser = Depends(require_internal)
):
    """The caller's to-dos, or — read-only by design — a teammate's."""
    target = str(user_id) if user_id else user.id
    if target != user.id:
        owner = (
            get_supabase()
            .table("profiles")
            .select("id, role")
            .eq("id", target)
            .limit(1)
            .execute()
        ).data
        # Estimator ids 404 too: the external estimator has no to-dos here.
        if not owner or owner[0]["role"] not in {r.value for r in INTERNAL_ROLES}:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return (
        get_supabase()
        .table("todos")
        .select(_SELECT)
        .eq("user_id", target)
        .order("created_at")
        .limit(500)
        .execute()
    ).data or []


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_todo(
    payload: TodoIn, user: CurrentUser = Depends(require_internal)
):
    return (
        get_supabase()
        .table("todos")
        .insert(
            {
                "user_id": user.id,
                "title": payload.title,
                "due_date": payload.due_date.isoformat() if payload.due_date else None,
            }
        )
        .execute()
    ).data[0]


@router.patch("/{todo_id}")
async def update_todo(
    todo_id: UUID,
    payload: TodoPatch,
    user: CurrentUser = Depends(require_internal),
):
    _own_todo(todo_id, user)
    return (
        get_supabase()
        .table("todos")
        .update(db_patch(payload))
        .eq("id", str(todo_id))
        .execute()
    ).data[0]


@router.delete("/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_todo(
    todo_id: UUID, user: CurrentUser = Depends(require_internal)
):
    _own_todo(todo_id, user)
    get_supabase().table("todos").delete().eq("id", str(todo_id)).execute()


@router.post("/{todo_id}/nudge")
async def nudge_todo(
    todo_id: UUID, user: CurrentUser = Depends(require_internal)
):
    """Poke the owner of an open to-do. `notify_user` creates the bell row and
    fires the branded email; `last_nudged_at` throttles repeat pokes."""
    sb = get_supabase()
    rows = (
        sb.table("todos")
        .select("id, user_id, title, is_done, last_nudged_at")
        .eq("id", str(todo_id))
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "To-do not found")
    todo = rows[0]
    if todo["user_id"] == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't nudge yourself")
    if todo["is_done"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That to-do is already done")

    now = datetime.now(timezone.utc)
    last = (
        datetime.fromisoformat(todo["last_nudged_at"])
        if todo["last_nudged_at"]
        else None
    )
    if _nudge_blocked(last, now):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "You already nudged this to-do recently",
        )

    me = (
        sb.table("profiles")
        .select("full_name")
        .eq("id", user.id)
        .single()
        .execute()
    ).data
    nudger = (me or {}).get("full_name") or "A teammate"
    notify_user(
        todo["user_id"],
        None,
        "nudge",
        f'{nudger} nudged you about your to-do: "{todo["title"]}"',
    )
    updated = (
        sb.table("todos")
        .update({"last_nudged_at": now.isoformat()})
        .eq("id", str(todo_id))
        .execute()
    ).data[0]
    return {"last_nudged_at": updated["last_nudged_at"]}
