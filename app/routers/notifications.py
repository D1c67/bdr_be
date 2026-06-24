"""In-app notifications for the current user."""

from fastapi import APIRouter, Depends

from app.core.deps import CurrentUser, get_current_user
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def my_notifications(user: CurrentUser = Depends(get_current_user)):
    return (
        get_supabase()
        .table("notifications")
        .select("*")
        .eq("user_id", user.id)
        # Auto-dismissed (task complete) rows drop out of the bell entirely.
        # Filter before the limit so they don't consume the 50-row budget.
        .is_("dismissed_at", "null")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    ).data or []


@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, user: CurrentUser = Depends(get_current_user)):
    get_supabase().table("notifications").update({"read_at": "now()"}).eq(
        "id", notification_id
    ).eq("user_id", user.id).execute()
    return {"read": True}


@router.post("/read-all")
async def mark_all_read(user: CurrentUser = Depends(get_current_user)):
    get_supabase().table("notifications").update({"read_at": "now()"}).eq(
        "user_id", user.id
    ).is_("read_at", "null").execute()
    return {"read": True}
