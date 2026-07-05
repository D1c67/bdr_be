"""File freshness + per-user review of estimator revision rounds.

`GET /file-freshness` powers the internal project page: per-type "a newer file
arrived after this step consumed the older one" flags (stepper badges +
reprocess callouts) and the caller's needs-review state for the red banner.

`POST /changes/reviewed` is the banner's "Mark as reviewed" button — a per-user
high-water mark (change_review_acks), so every reviewer clears their own banner
and the next revision round re-trips it for everyone.
"""

from datetime import datetime

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel

from app.core.deps import CurrentUser, require_internal
from app.core.roles import CHANGE_REVIEW_ROLES
from app.core.supabase_client import get_supabase
from app.services import estimator_rounds
from app.services.notifications import audit, dismiss_notifications

router = APIRouter(prefix="/projects/{project_id}", tags=["change-review"])


class ReviewedIn(BaseModel):
    # The round the banner actually showed the user — echoed server data. When
    # a newer round sealed between render and click, only the seen round is
    # acknowledged, so the newer one re-trips the banner instead of being
    # swallowed by the click.
    round: int | None = None


@router.get("/file-freshness")
def file_freshness(
    project_id: str, user: CurrentUser = Depends(require_internal)
):
    data = estimator_rounds.freshness(project_id)
    data["needs_review"] = estimator_rounds.needs_review(project_id, user.id, user.role)
    return data


@router.post("/changes/reviewed")
def mark_changes_reviewed(
    project_id: str,
    body: ReviewedIn | None = Body(default=None),
    user: CurrentUser = Depends(require_internal),
):
    """Acknowledge a revision round for the calling user only.

    The mark stored is the acknowledged round's `submitted_at` — never `now()`
    — and when the client echoes the round it displayed, a newer round sealed
    between render and click stays unacknowledged so the banner correctly
    reappears. Marks only move forward (notes.py's advance_read_mark rule).
    Only the roles required to review may ack; for everyone else (accountant,
    IT admin) the banner never shows and this is a no-op.
    """
    if user.role not in CHANGE_REVIEW_ROLES:
        return {"needs_review": False}
    latest = estimator_rounds.latest_submission(project_id)
    if not latest or latest["round"] < 2:
        return {"needs_review": False}

    acked = latest
    if body and body.round is not None and body.round < latest["round"]:
        rows = (
            get_supabase()
            .table("estimator_submissions")
            .select("round, submitted_at")
            .eq("project_id", project_id)
            .eq("round", body.round)
            .execute()
        ).data or []
        if not rows:
            return {"needs_review": True}
        acked = rows[0]

    sb = get_supabase()
    mark = acked["submitted_at"]
    existing = (
        sb.table("change_review_acks")
        .select("last_reviewed_at")
        .eq("project_id", project_id)
        .eq("user_id", user.id)
        .execute()
    ).data or []
    if existing and datetime.fromisoformat(
        existing[0]["last_reviewed_at"]
    ) >= datetime.fromisoformat(mark):
        mark = existing[0]["last_reviewed_at"]
    sb.table("change_review_acks").upsert(
        {"project_id": project_id, "user_id": user.id, "last_reviewed_at": mark},
        on_conflict="project_id,user_id",
    ).execute()
    # Their bell rows for this event are handled too.
    dismiss_notifications(
        project_id=project_id, types=["estimate_revised"], user_id=user.id
    )
    audit(
        user.id,
        "files.review_changes",
        "project",
        project_id,
        {"round": acked["round"]},
    )
    # Still true when an even newer round sealed mid-request.
    return {
        "needs_review": estimator_rounds.needs_review(project_id, user.id, user.role)
    }
