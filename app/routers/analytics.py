"""Analytics — pipeline snapshot and time-in-stage, computed from stage_events.

Durations are derived by diffing consecutive stage_events per project (the time a
project spent in a stage = when it left it minus when it entered). The current
(last) stage of an in-flight project is still open and excluded from averages.
"""

from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import INTERNAL_ROLES
from app.core.supabase_client import get_supabase
from app.services import analytics_metrics as metrics
from app.services.analytics_metrics import WindowData
from app.services.workflow import STAGES

router = APIRouter(prefix="/analytics", tags=["analytics"])

# Shared query params for every windowed endpoint.
RangeParam = Query("month", pattern="^(day|week|month|quarter|year|custom)$")
# Optional lifecycle-status filter; prunes the project cohort across all sections.
StatusParam = Query(None, pattern="^(active|sent|won|lost|no_award|declined|abandoned)$")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _gate(user: CurrentUser) -> None:
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")


def _load(
    range_: str,
    date_from: str | None,
    date_to: str | None,
    project_id: str | None,
    status_: str | None = None,
) -> WindowData:
    try:
        df, dt = metrics.resolve_range(range_, date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return metrics.load_window(df, dt, project_id, status_)


@router.get("/summary")
async def summary(user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    sb = get_supabase()

    projects = sb.table("projects").select("id, current_stage").execute().data or []
    by_stage: dict[str, int] = defaultdict(int)
    for p in projects:
        by_stage[p["current_stage"]] += 1

    # Time-in-stage from the event log.
    events = (
        sb.table("stage_events")
        .select("project_id, to_stage, entered_at")
        .order("project_id")
        .order("entered_at")
        .execute()
    ).data or []
    per_project: dict[str, list] = defaultdict(list)
    for e in events:
        per_project[e["project_id"]].append(e)

    durations: dict[str, list[float]] = defaultdict(list)  # stage -> [seconds]
    for evs in per_project.values():
        for cur, nxt in zip(evs, evs[1:]):
            secs = (_parse(nxt["entered_at"]) - _parse(cur["entered_at"])).total_seconds()
            durations[cur["to_stage"]].append(secs)

    time_in_stage = []
    for key, defn in sorted(STAGES.items(), key=lambda kv: kv[1].order):
        samples = durations.get(key, [])
        avg_hours = round(sum(samples) / len(samples) / 3600, 2) if samples else None
        time_in_stage.append(
            {"stage": key, "label": defn.label, "avg_hours": avg_hours, "samples": len(samples)}
        )

    # Win/Loss outcomes (recorded at the final bid_outcome step).
    outcomes = sb.table("bid_outcomes").select("result").execute().data or []
    by_result: dict[str, int] = defaultdict(int)
    for o in outcomes:
        by_result[o["result"]] += 1
    decided = by_result.get("won", 0) + by_result.get("lost", 0)
    win_rate = round(by_result.get("won", 0) / decided, 4) if decided else None

    return {
        "total_projects": len(projects),
        "by_current_stage": dict(by_stage),
        "submitted": by_stage.get("submitted", 0),
        "declined": by_stage.get("declined", 0),
        "time_in_stage": time_in_stage,
        "outcomes": {
            "won": by_result.get("won", 0),
            "lost": by_result.get("lost", 0),
            "no_award": by_result.get("no_award", 0),
            # Win rate over decided (won/lost) bids; None until at least one decides.
            "win_rate": win_rate,
        },
    }


# ── Windowed analytics (time-frame filtered, with per-project drill-down) ──
#
# All routes share the range/date_from/date_to/project_id params and are gated to
# INTERNAL_ROLES. Responses are plain dicts (like /summary) — the heavy lifting is
# in services/analytics_metrics.py.


@router.get("/overview")
async def overview(
    range_: str = RangeParam,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    status_: str | None = StatusParam,
    user: CurrentUser = Depends(get_current_user),
):
    _gate(user)
    return metrics.overview(_load(range_, date_from, date_to, project_id, status_), range_)


@router.get("/send-out")
async def send_out(
    range_: str = RangeParam,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    status_: str | None = StatusParam,
    benchmark: str = Query("internal_bid_at", pattern="^(internal_bid_at|actual_bid_at)$"),
    user: CurrentUser = Depends(get_current_user),
):
    _gate(user)
    w = _load(range_, date_from, date_to, project_id, status_)
    return metrics.send_out(w, range_, benchmark, user.role)


@router.get("/estimator")
async def estimator(
    range_: str = RangeParam,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    status_: str | None = StatusParam,
    user: CurrentUser = Depends(get_current_user),
):
    _gate(user)
    return metrics.estimator(_load(range_, date_from, date_to, project_id, status_), range_)


@router.get("/quotes")
async def quotes(
    range_: str = RangeParam,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    status_: str | None = StatusParam,
    user: CurrentUser = Depends(get_current_user),
):
    _gate(user)
    return metrics.quotes(_load(range_, date_from, date_to, project_id, status_), range_)


@router.get("/cycle-time")
async def cycle_time(
    range_: str = RangeParam,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    status_: str | None = StatusParam,
    user: CurrentUser = Depends(get_current_user),
):
    _gate(user)
    return metrics.cycle_time(_load(range_, date_from, date_to, project_id, status_), range_)


@router.get("/win-loss")
async def win_loss(
    range_: str = RangeParam,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    status_: str | None = StatusParam,
    user: CurrentUser = Depends(get_current_user),
):
    _gate(user)
    return metrics.win_loss(_load(range_, date_from, date_to, project_id, status_), range_)


@router.get("/projects/{project_id}")
async def project_detail(
    project_id: str,
    range_: str = RangeParam,
    date_from: str | None = None,
    date_to: str | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """All sections for one project — drives the drill-down modal. The window only
    bounds the trend/cohort context; the project's own history is always returned."""
    _gate(user)
    w = _load(range_, date_from, date_to, project_id)
    return metrics.project_detail(w, range_, user.role)
