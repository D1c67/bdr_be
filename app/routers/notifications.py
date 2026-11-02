"""In-app notifications for the current user."""

from fastapi import APIRouter, Depends

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def my_notifications(user: CurrentUser = Depends(get_current_user)):
    rows = (
        get_supabase()
        .table("notifications")
        # Explicit list, not "*": the bell serves every role including the
        # external estimator, so infrastructure columns (email_log_id) stay
        # server-side rather than riding along to the least-trusted client.
        # `metadata` is the deep-link payload (gc_id for gc_pricing.* rows,
        # 0118); only internal roles ever receive rows that carry it.
        .select(
            "id, user_id, project_id, type, message, read_at, created_at,"
            " dismissed_at, rfq_id, metadata"
        )
        .eq("user_id", user.id)
        # Auto-dismissed (task complete) rows drop out of the bell entirely.
        # Filter before the limit so they don't consume the 50-row budget.
        .is_("dismissed_at", "null")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    ).data or []
    if user.role == Role.ESTIMATOR:
        # A row created inside an assistant run carries the provenance pair
        # in its metadata (0172); the run id is internal and never reaches
        # the external estimator (actor_kind may).
        for r in rows:
            md = r.get("metadata")
            if isinstance(md, dict) and "assistant_run_id" in md:
                r["metadata"] = {k: v for k, v in md.items() if k != "assistant_run_id"}
    return rows


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
