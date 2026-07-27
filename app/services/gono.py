"""Go/No-Go decision logic — the score decides.

Rule (confirmed with the user): the intake scoring rubric drives the outcome.
Score >= 30 is a Go (the project passes straight through to To Estimator),
20-29 parks the project in review at the go_no_go stage, and below 20 is a
No-Go (declined). Any writer role may push a project to review, go, or no_go
regardless of its score — at the send-to-Go/No-Go step, or (for projects in
review) with the decide endpoint. Voting is retired.

A recorded decision can be undone (see the Undo section at the foot of this
file), which drops it and puts the project back in review at Go/No-Go.

The points below mirror the frontend rubric (bdr_fe/lib/gonoScoring.ts) value
for value — that file renders the per-question reference table, this one is
what actually decides. Keep the two in sync.
"""

import logging
import math
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import workflow
from app.services.notifications import audit, dismiss_notifications, notify_role

logger = logging.getLogger(__name__)

# Points per answer value, per question column on `projects`. Unanswered (null
# or an unknown value) scores 0, same as the frontend's null rows.
ANSWER_POINTS: dict[str, dict[str, int]] = {
    "project_type": {
        "new_construction": 5,
        "ti": 4,
        "multi_family": 5,
        "casino_strip": 4,
        "casino_other": 1,
        "lighting": 3,
        "roadway": 5,
        "generator": 4,
        "other": 0,
        "unknown": 0,
    },
    "owner_type": {
        "rtc": 5,
        "doa": 5,
        "ccsd": 2,
        "public_other": 4,
        "casino_strip": 4,
        "casino_other": 4,
        "private_commercial": 3,
        "private_residential": 4,
        "other": 0,
        "unknown": 0,
    },
    "labor_needed": {
        "union": 4,
        "ce_cw": 5,
        "ce": 5,
        "cw": 5,
        "non_union": 0,
        "other": 0,
        "unknown": 0,
    },
    "bid_method": {
        "hard_bid": 2,
        "cmar": 5,
        "single_gc_hard_bid": 4,
        "other": 0,
        "unknown": 0,
    },
    "competitor_known": {
        "yes_1_2": 3,
        "yes_3_plus": 0,
        "no_unknown": 2,
        "only_ec_bidding": 5,
        "other": 0,
    },
    "gc_known": {
        "yes_1_2": 3,
        "yes_3_plus": 1,
        "no_unknown": 0,
        "only_gc_bidding": 5,
        "no_gc_needed": 5,
        "other": 0,
    },
    "subs_needed": {
        "no": 5,
        "yes_underground": 2,
        "yes_low_voltage": 2,
        "yes_fire_alarm": 2,
        "two_subs": 1,
        "three_plus_subs": 0,
        "other": 0,
        "unknown": 0,
    },
    "est_value_band": {
        "under_50k": 1,
        "50k_150k": 2,
        "150k_500k": 3,
        "500k_1m": 4,
        "1m_3m": 4,
        "over_3m": 5,
        "other": 0,
        "unknown": 0,
    },
    "scope_fit": {"yes": 5, "no": 1, "maybe": 2, "other": 0, "unknown": 0},
}

# Days-until-bid bands: first band whose `min` the day count reaches wins;
# past due (negative) falls through to 0.
BID_DAYS_BANDS: tuple[tuple[int, int], ...] = ((30, 5), (15, 4), (8, 2), (0, 0))

THRESHOLDS = {"go": 30, "review": 20}


def _bid_days(project: dict) -> int | None:
    """Whole days until the bid date (actual preferred, else internal), or None
    when no bid date is set. Matches the frontend's floor((bid - now) / 1 day)."""
    iso = project.get("actual_bid_at") or project.get("internal_bid_at")
    if not iso:
        return None
    due = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return math.floor((due - datetime.now(timezone.utc)).total_seconds() / 86_400)


def _bid_days_points(days: int | None) -> int:
    if days is None:
        return 0
    for minimum, points in BID_DAYS_BANDS:
        if days >= minimum:
            return points
    return 0  # past due


def compute_score(project: dict) -> int:
    """Total rubric score for a project row (nine answers + the bid-days band)."""
    total = sum(
        points.get(project.get(key) or "", 0) for key, points in ANSWER_POINTS.items()
    )
    return total + _bid_days_points(_bid_days(project))


def outcome_for_score(score: int) -> str:
    """'go' / 'review' / 'no_go' per the thresholds."""
    if score >= THRESHOLDS["go"]:
        return "go"
    if score >= THRESHOLDS["review"]:
        return "review"
    return "no_go"


def finalize(
    project_id: str,
    outcome: str,
    method: str,
    decided_by: str | None,
    score: int | None = None,
) -> dict:
    """Record a go/no_go decision and move the project on.

    `method` is 'score' (auto-applied from the rubric) or 'manual' (a user
    pushed the outcome). The transition runs first so an illegal state (e.g. a
    concurrent decision already moved the project) aborts before any decision
    row is written. Returns the updated project row.

    A "go" advances the intake category's head (go_no_go → to_estimator). A "no_go"
    is a project-global kill (decline_project).
    """
    note = f"Go/No-Go: {outcome} ({method})"
    if outcome == "go":
        updated = workflow.advance_category(project_id, "intake", decided_by, note)
    else:
        updated = workflow.decline_project(project_id, decided_by, note)
    get_supabase().table("go_no_go_decisions").upsert(
        {"project_id": project_id, "outcome": outcome, "method": method, "decided_by": decided_by},
        on_conflict="project_id",
    ).execute()
    if outcome == "go":
        notify_role(Role.ESTIMATING_ADMIN, project_id, "gono_go", "Project accepted — send to estimator")
    audit(decided_by, f"gono.{outcome}", "project", project_id, {"method": method, "score": score})
    return updated


def apply_entry_action(
    project_id: str, actor_id: str | None, action: str
) -> tuple[str | None, dict | None]:
    """Run the Go/No-Go gate for a project that just entered the stage.

    `action` is the sender's choice: 'score' (default — the thresholds decide),
    'review' (hold for a manual decision regardless of score), or 'go'/'no_go'
    (push the outcome regardless of score). Returns (outcome, updated project
    row); (None, None) means the project stays in review.
    """
    if action == "review":
        return None, None
    project = (
        get_supabase().table("projects").select("*").eq("id", project_id).single().execute()
    ).data or {}
    score = compute_score(project)
    if action in ("go", "no_go"):
        return action, finalize(project_id, action, "manual", actor_id, score=score)
    outcome = outcome_for_score(score)
    if outcome == "review":
        return None, None
    return outcome, finalize(project_id, outcome, "score", actor_id, score=score)


# ── Undo ──────────────────────────────────────────────────────────────────────
# A recorded decision can be taken back, which puts the project back in review at
# Go/No-Go (no decision row, so the panel offers Go / No-Go again).
#
# A No-Go is always undoable — a declined project is frozen, so nothing can have
# happened since. A Go is only undoable while it has changed nothing outside the
# gate: the intake lane must still be parked at To Estimator (never advanced past
# it) with no estimator assignment and no file package emailed. Once drawings have
# left the building, the recorded Go is history — revoke the assignment first, or
# abandon the bid.

UNDO_NO_DECISION = "There is no recorded Go/No-Go decision to undo"
UNDO_BLOCKED_ADVANCED = (
    "This project has already moved past To Estimator — the Go can no longer be undone"
)
UNDO_BLOCKED_SENT = (
    "The file package has already gone out to an estimator — revoke the assignment "
    "before undoing the Go (or abandon the bid instead)"
)


def _estimator_engaged(project_id: str) -> bool:
    """True once an estimator has been let in on this project: an assignment that
    is still live, or an initial package already emailed (even if later revoked)."""
    from app.services import file_sends

    active = (
        get_supabase()
        .table("estimator_assignments")
        .select("id")
        .eq("project_id", project_id)
        .is_("revoked_at", "null")
        .limit(1)
        .execute()
    ).data or []
    return bool(active) or file_sends.has_initial_send(project_id)


def undo_blocker(project: dict, state: dict[str, dict]) -> str | None:
    """Why this project's decision cannot be undone, or None when it can be."""
    if project.get("current_stage") == "declined":
        return None  # a No-Go froze everything; always reversible
    intake = state.get("intake", {})
    if intake.get("status") != "active" or intake.get("current_task") != "to_estimator":
        return UNDO_BLOCKED_ADVANCED
    if _estimator_engaged(project["id"]):
        return UNDO_BLOCKED_SENT
    return None


def undo_status(project: dict, state: dict[str, dict], decision: dict | None) -> dict:
    """The `can_undo` / `undo_blocked` pair the status endpoint reports."""
    if not decision:
        return {"can_undo": False, "undo_blocked": None}
    blocker = undo_blocker(project, state)
    return {"can_undo": blocker is None, "undo_blocked": blocker}


def undo(project_id: str, actor_id: str | None) -> dict:
    """Take back a recorded decision and put the project back in review.

    The lane move runs first (so a conflicting state aborts before the decision row
    is dropped), then the decision row is deleted — `audit_log` and the reverse
    `stage_events` row keep the history. Note for analytics: an undo + re-decide
    writes a second decision event, so the decline rate counts the final decision
    plus each earlier one; that is deliberate (each was really made).
    """
    sb = get_supabase()
    project = (
        sb.table("projects").select("*").eq("id", project_id).single().execute()
    ).data
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    rows = (
        sb.table("go_no_go_decisions").select("*").eq("project_id", project_id).execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_409_CONFLICT, UNDO_NO_DECISION)
    decision = rows[0]

    blocker = undo_blocker(project, workflow.load_category_state(project_id))
    if blocker:
        raise HTTPException(status.HTTP_409_CONFLICT, blocker)

    from_task = "declined" if project.get("current_stage") == "declined" else "to_estimator"
    updated = workflow.reopen_go_no_go(
        project_id, from_task, actor_id, f"Go/No-Go undone (was {decision['outcome']})"
    )
    sb.table("go_no_go_decisions").delete().eq("project_id", project_id).execute()
    # The "send to estimator" prompt the Go raised is stale now. Best-effort — the
    # project is already back in review and must not roll back over a cleanup.
    try:
        dismiss_notifications(project_id=project_id, types=["gono_go", "stage_handoff"])
    except Exception:  # noqa: BLE001
        logger.exception("Notification dismissal failed after gono undo on %s", project_id)
    audit(
        actor_id,
        "gono.undo",
        "project",
        project_id,
        {"outcome": decision["outcome"], "method": decision["method"]},
    )
    return updated
