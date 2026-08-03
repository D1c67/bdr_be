"""In-app notifications for the current user."""

from fastapi import APIRouter, Depends

from app.core.deps import CurrentUser, get_current_user
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def my_notifications(user: CurrentUser = Depends(get_current_user)):
    return (
        get_supabase()
        .table("notifications")
        # Explicit list, not "*": the bell serves every role including the
        # external estimator, so infrastructure columns (email_log_id) stay
        # server-side rather than riding along to the least-trusted client.
        .select(
            "id, user_id, project_id, type, message, read_at, created_at,"
            " dismissed_at, rfq_id"
        )
        .eq("user_id", user.id)
        # Auto-dismissed (task complete) rows drop out of the bell entirely.
        # Filter before the limit so they don't consume the 50-row budget.
        .is_("dismissed_at", "null")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    ).data or []


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, user: CurrentUser = Depends(get_current_user)):
    get_supabase().table("notifications").update({"read_at": "now()"}).eq(
        "id", notification_id
    ).eq("user_id", user.id).execute()
    return {"read": True}


@router.post("/read-all")
def mark_all_read(user: CurrentUser = Depends(get_current_user)):
    get_supabase().table("notifications").update({"read_at": "now()"}).eq(
        "user_id", user.id
    ).is_("read_at", "null").execute()
    return {"read": True}
