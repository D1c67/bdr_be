"""Go/No-Go decision logic — the score decides.

Rule (confirmed with the user): the intake scoring rubric drives the outcome.
Score >= 30 is a Go (the project passes straight through to To Estimator),
20-29 parks the project in review at the go_no_go stage, and below 20 is a
No-Go (declined). Any writer role may push a project to review, go, or no_go
regardless of its score — at the send-to-Go/No-Go step, or (for projects in
review) with the decide endpoint. Voting is retired.

The points below mirror the frontend rubric (bdr_fe/lib/gonoScoring.ts) value
for value — that file renders the per-question reference table, this one is
what actually decides. Keep the two in sync.
"""

import math
from datetime import datetime, timezone

from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import workflow
from app.services.notifications import audit, notify_role

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
    """
    to_stage = "to_estimator" if outcome == "go" else "declined"
    updated = workflow.transition_project(
        project_id, to_stage, decided_by, f"Go/No-Go: {outcome} ({method})"
    )
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
