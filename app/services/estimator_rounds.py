"""Estimator submission rounds — the estimator→team half of the file-updates
story (the team→estimator half lives in files.py / estimator_email.py).

Round 1 is the original deliverables hand-off (estimate / BOQ / markup). Once
sent, those files are immutable to the estimator; anything later goes in as a
draft (`estimator_deliverable` set, `submission_round` NULL) and is sealed into
the next numbered round when the estimator presses Send. Sealing is a single
conditional UPDATE, so an upload racing the send simply lands in the following
round — a file can never be half-in a sent round.

Machine consumers (the latest-estimate picker, BOQ pickers, RFQ attachment
loading, exports, due-reminder completion) must never act on an unsent draft —
they filter with `SENT_OR_INTERNAL`. Human file lists keep drafts visible so
nothing is invisibly orphaned if an estimator is revoked mid-round.

Staleness ("a newer file arrived after this step consumed the older one") is
computed dynamically from the anchors each consumer already records —
`general_material_estimates.estimate_file_id`, the latest
`boq_analyses.boq_file_id`, and the newest sent Trenching RFQ send — never
stored, so it can't drift.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status

from app.core.roles import CHANGE_REVIEW_ROLES, Role
from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# PostgREST or-filter: everything that is NOT an unsent estimator draft.
# Internal uploads never set estimator_deliverable, so they always pass.
SENT_OR_INTERNAL = "estimator_deliverable.eq.false,submission_round.not.is.null"

# What the estimator hands the team, in display order. `estimator_additional`
# is the estimator's "Additional Files" box — distinct from the team-side
# 'additional' category (0048), which flows the other way.
DELIVERABLE_CATEGORIES = ("estimate", "boq", "markup", "estimator_additional")

# Spam guard, not a product limit: no real bid needs anywhere near this many
# hand-offs, but each round fans out high-importance email to the whole review
# team, so a runaway (or malicious) account must hit a wall.
MAX_ROUNDS = 30


def exclude_unsent(q):
    """Restrict a project_files query to rows machine consumers may act on."""
    return q.or_(SENT_OR_INTERNAL)


def resolve_boq_file_id(project_id: str, explicit_id: str | None) -> str:
    """The BOQ a processor may consume: the caller's explicit pick — validated
    to be this project's, category boq, and not an unsent draft — or else the
    newest such file. Explicit ids get the same draft screen as the fallback so
    a stale UI (or a handcrafted request) can't route an unsent draft into
    analysis/proposals."""
    sb = get_supabase()
    if explicit_id:
        q = (
            sb.table("project_files")
            .select("id")
            .eq("id", explicit_id)
            .eq("project_id", project_id)
            .eq("category", "boq")
        )
        if not (exclude_unsent(q).limit(1).execute().data or []):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "That BOQ file is not available (it may not have been sent by the estimator yet)",
            )
        return explicit_id
    q = (
        sb.table("project_files")
        .select("id")
        .eq("project_id", project_id)
        .eq("category", "boq")
    )
    latest = exclude_unsent(q).order("created_at", desc=True).limit(1).execute().data
    if not latest:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No BOQ file uploaded for this project"
        )
    return latest[0]["id"]


def latest_submission(project_id: str) -> dict[str, Any] | None:
    rows = (
        get_supabase()
        .table("estimator_submissions")
        .select("id, round, submitted_at, summary, estimator_id")
        .eq("project_id", project_id)
        .order("round", desc=True)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def open_drafts(project_id: str) -> list[dict[str, Any]]:
    """The not-yet-sent deliverable files (the open round)."""
    return (
        get_supabase()
        .table("project_files")
        .select("id, category, filename")
        .eq("project_id", project_id)
        .eq("estimator_deliverable", True)
        .is_("submission_round", "null")
        .order("created_at")
        .execute()
    ).data or []


def create_submission_round(
    project_id: str, estimator_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Seal the open drafts into the next round. Returns (submission, sealed).

    The UNIQUE (project_id, round) constraint is the double-send guard: two
    racing sends compute the same round number and the second insert fails.
    The seal UPDATE is conditional on `submission_round IS NULL`, so a file
    uploaded mid-send stays a draft for the next round instead of slipping
    into an already-announced one.

    The seal is also scoped to `uploaded_by = estimator_id`: with more than one
    active assignee (which "Re-assign" makes routine) estimator B pressing Send
    must seal only their own drafts. Sealing A's in-progress files would announce
    them under B's round and freeze them to A (files.py delete/read scoping).
    `open_drafts` and `freshness.open_draft_count` stay project-wide on purpose —
    they feed the internal team view, which wants the project total.
    """
    sb = get_supabase()
    latest = latest_submission(project_id)
    round_no = (latest["round"] + 1) if latest else 1
    if round_no > MAX_ROUNDS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Too many revision rounds on this project — contact the G3 team directly",
        )

    try:
        submission = (
            sb.table("estimator_submissions")
            .insert(
                {
                    "project_id": project_id,
                    "estimator_id": estimator_id,
                    "round": round_no,
                }
            )
            .execute()
        ).data[0]
    except Exception as exc:  # noqa: BLE001 — unique violation → concurrent send
        if "23505" in str(exc) or "duplicate key" in str(exc):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "These files were just sent — refresh to see the result",
            ) from exc
        raise

    try:
        sealed = (
            sb.table("project_files")
            .update({"submission_round": round_no})
            .eq("project_id", project_id)
            .eq("estimator_deliverable", True)
            .eq("uploaded_by", estimator_id)
            .is_("submission_round", "null")
            .execute()
        ).data or []
    except Exception:
        # Best-effort compensation. The UPDATE may have been applied even
        # though the response was lost (timeout), so first un-seal anything
        # stamped with this round, then drop the submission row — never leave
        # files claiming a round that was never announced.
        try:
            sb.table("project_files").update({"submission_round": None}).eq(
                "project_id", project_id
            ).eq("submission_round", round_no).execute()
            sb.table("estimator_submissions").delete().eq("id", submission["id"]).execute()
        except Exception:  # noqa: BLE001
            logger.warning("create_submission_round: orphan cleanup failed", exc_info=True)
        raise

    if not sealed:
        sb.table("estimator_submissions").delete().eq("id", submission["id"]).execute()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Add at least one revised or additional file first",
        )

    counts: dict[str, int] = {}
    for f in sealed:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    submission["summary"] = counts
    # Best-effort snapshot — the round stands even if the summary write fails.
    try:
        sb.table("estimator_submissions").update({"summary": counts}).eq(
            "id", submission["id"]
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning("create_submission_round: summary write failed", exc_info=True)
    return submission, sealed


def _newest_visible(sb, project_id: str, category: str) -> dict[str, Any] | None:
    rows = (
        sb.table("project_files")
        .select("id, filename, created_at")
        .eq("project_id", project_id)
        .eq("category", category)
        .or_(SENT_OR_INTERNAL)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _estimate_stale(sb, project_id: str) -> bool:
    """A newer estimate exists than the one general-material extraction used.
    Only an anchored row counts (estimate_file_id set): no extraction yet, a
    run that never consumed a file, or a manual override are all "nothing
    consumed" — the step simply picks the newest file when it runs. Mid-run is
    never stale either (the anchor is about to move)."""
    rows = (
        sb.table("general_material_estimates")
        .select("estimate_file_id, status")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if not rows or rows[0].get("status") == "running" or not rows[0].get("estimate_file_id"):
        return False
    newest = _newest_visible(sb, project_id, "estimate")
    return bool(newest and newest["id"] != rows[0]["estimate_file_id"])


def _boq_stale(sb, project_id: str) -> bool:
    rows = (
        sb.table("boq_analyses")
        .select("boq_file_id")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return False
    newest = _newest_visible(sb, project_id, "boq")
    return bool(newest and newest["id"] != rows[0].get("boq_file_id"))


def _markup_stale(sb, project_id: str) -> bool:
    """A newer markup arrived after the last Trenching RFQ send (markups are
    auto-attached to Trenching RFQ emails only — rfq_sending.py). No Trenching
    send yet → nothing consumed the markups → not stale."""
    newest = _newest_visible(sb, project_id, "markup")
    if not newest:
        return False
    from app.services.rfq_sending import _is_trenching

    # Trenching RFQ ids first, then the single newest sent send among them —
    # exact regardless of how many non-Trenching sends exist.
    rfqs = (
        sb.table("rfqs")
        .select("id, material_categories(name)")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    trenching_ids = [
        r["id"]
        for r in rfqs
        if _is_trenching(((r.get("material_categories") or {}).get("name")) or "")
    ]
    if not trenching_ids:
        return False
    sends = (
        sb.table("rfq_sends")
        .select("sent_at, created_at")
        .in_("rfq_id", trenching_ids)
        .eq("status", "sent")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data or []
    if not sends:
        return False
    last_sent = sends[0].get("sent_at") or sends[0].get("created_at")
    return _ts(newest["created_at"]) > _ts(last_sent)


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def freshness(project_id: str) -> dict[str, Any]:
    """The project-page payload: per-type staleness + latest-round facts."""
    sb = get_supabase()
    latest = latest_submission(project_id)
    draft_count = (
        sb.table("project_files")
        .select("id", count="exact")
        .eq("project_id", project_id)
        .eq("estimator_deliverable", True)
        .is_("submission_round", "null")
        .execute()
    ).count or 0
    return {
        "estimate_stale": _estimate_stale(sb, project_id),
        "boq_stale": _boq_stale(sb, project_id),
        "markup_stale": _markup_stale(sb, project_id),
        "latest_round": latest["round"] if latest else None,
        "latest_submitted_at": latest["submitted_at"] if latest else None,
        "latest_summary": latest.get("summary") if latest else None,
        "open_draft_count": draft_count,
    }


def needs_review(project_id: str, user_id: str, role: Role) -> bool:
    """Whether this user still has to acknowledge the latest revision round.

    Only rounds ≥ 2 need acks (round 1 is the normal hand-off, announced by the
    estimate_submitted notification). A user with no ack row needs review as
    soon as any revision round exists — new hires included, deliberately.
    """
    if role not in CHANGE_REVIEW_ROLES:
        return False
    latest = latest_submission(project_id)
    if not latest or latest["round"] < 2:
        return False
    rows = (
        get_supabase()
        .table("change_review_acks")
        .select("last_reviewed_at")
        .eq("project_id", project_id)
        .eq("user_id", user_id)
        .execute()
    ).data or []
    if not rows:
        return True
    return _ts(rows[0]["last_reviewed_at"]) < _ts(latest["submitted_at"])
