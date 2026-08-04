"""Dev-only AI monitor: queue state, failure stats, and job actions.

Feeds the sidebar's AI Monitor page (dev accounts only). Reads aggregate the
llm_call_log ledger (one row per LLM call, every feature and tier) and the
llm_jobs queue; actions are limited to what is safe from a dashboard: retry a
terminally failed job (fresh attempt cycle) and cancel a still-queued one.
Running jobs cannot be canceled - the model call is already in flight.

Days are bucketed on the Los Angeles calendar, matching the analytics
convention (bid-invitations report).
"""

import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.deps import CurrentUser, require_dev
from app.core.ratelimit import llm_monitor_rate_limit
from app.core.supabase_client import get_supabase
from app.services import llm_queue
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-monitor", tags=["llm-monitor"])

_LA = ZoneInfo("America/Los_Angeles")

# Aggregation reads page through the ledger; bound the work per request.
_PAGE = 1000
_MAX_PAGES = 25


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _fetch_calls(since_iso: str, columns: str) -> tuple[list[dict], bool]:
    """Page through llm_call_log since the cutoff. Returns (rows, truncated)."""
    sb = get_supabase()
    rows: list[dict] = []
    for page in range(_MAX_PAGES):
        chunk = (
            sb.table("llm_call_log")
            .select(columns)
            .gte("created_at", since_iso)
            .order("created_at", desc=True)
            .range(page * _PAGE, (page + 1) * _PAGE - 1)
            .execute()
        ).data or []
        rows.extend(chunk)
        if len(chunk) < _PAGE:
            return rows, False
    return rows, True


def _la_day(created_at: str) -> str:
    try:
        stamp = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    return stamp.astimezone(_LA).date().isoformat()


def _project_labels(project_ids: list[str]) -> dict[str, dict]:
    ids = [p for p in dict.fromkeys(project_ids) if p]
    if not ids:
        return {}
    rows = (
        get_supabase()
        .table("projects")
        .select("id, number, name")
        .in_("id", ids)
        .execute()
    ).data or []
    return {r["id"]: {"number": r.get("number"), "name": r.get("name")} for r in rows}


def _job_view(job: dict, projects: dict[str, dict]) -> dict:
    proj = projects.get(job.get("project_id") or "") or {}
    return {
        "id": job["id"],
        "job_type": job["job_type"],
        "feature": job["feature"],
        "status": job["status"],
        "target_id": job["target_id"],
        "project_id": job.get("project_id"),
        "project_number": proj.get("number"),
        "project_name": proj.get("name"),
        "priority": job.get("priority"),
        "attempts": job.get("attempts"),
        "max_attempts": job.get("max_attempts"),
        "next_attempt_at": job.get("next_attempt_at"),
        "error_kind": job.get("error_kind"),
        "last_error": job.get("last_error"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


@router.get("/summary", dependencies=[Depends(llm_monitor_rate_limit)])
def summary(
    days: int = Query(default=30, ge=1, le=90),
    _: CurrentUser = Depends(require_dev),
) -> dict:
    """Aggregated call/job stats for the window: per-day series (LA calendar),
    per-feature and per-error-kind breakdowns, live queue counts."""
    calls, truncated = _fetch_calls(
        _cutoff(days), "feature, provider, tier, ok, error_kind, duration_ms, created_at"
    )

    by_day: dict[str, dict] = defaultdict(lambda: {"calls": 0, "failures": 0})
    by_feature: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "failures": 0, "duration_ms_total": 0, "duration_ms_count": 0}
    )
    by_kind: dict[str, int] = defaultdict(int)
    by_tier: dict[str, dict] = defaultdict(lambda: {"calls": 0, "failures": 0})
    for row in calls:
        day = _la_day(row.get("created_at") or "")
        ok = bool(row.get("ok"))
        by_day[day]["calls"] += 1
        feat = by_feature[row.get("feature") or "unknown"]
        feat["calls"] += 1
        tier = by_tier[row.get("tier") or "unknown"]
        tier["calls"] += 1
        if row.get("duration_ms") is not None:
            feat["duration_ms_total"] += row["duration_ms"]
            feat["duration_ms_count"] += 1
        if not ok:
            by_day[day]["failures"] += 1
            feat["failures"] += 1
            tier["failures"] += 1
            by_kind[row.get("error_kind") or "unknown"] += 1

    sb = get_supabase()
    queued = (
        sb.table("llm_jobs").select("id", count="exact").eq("status", "queued").limit(1).execute()
    ).count or 0
    running = (
        sb.table("llm_jobs").select("id", count="exact").eq("status", "running").limit(1).execute()
    ).count or 0
    job_rows = (
        sb.table("llm_jobs")
        .select("status, job_type, created_at")
        .gte("created_at", _cutoff(days))
        .limit(5000)
        .execute()
    ).data or []
    jobs_by_status: dict[str, int] = defaultdict(int)
    for job in job_rows:
        jobs_by_status[job["status"]] += 1

    total_calls = len(calls)
    total_failures = sum(d["failures"] for d in by_day.values())
    return {
        "days": days,
        "truncated": truncated,
        "totals": {
            "calls": total_calls,
            "failures": total_failures,
            "success_rate": (
                round((total_calls - total_failures) / total_calls, 4) if total_calls else None
            ),
        },
        "by_day": [
            {"date": day, **counts} for day, counts in sorted(by_day.items())
        ],
        "by_feature": [
            {
                "feature": feature,
                "calls": f["calls"],
                "failures": f["failures"],
                "avg_duration_ms": (
                    round(f["duration_ms_total"] / f["duration_ms_count"])
                    if f["duration_ms_count"]
                    else None
                ),
            }
            for feature, f in sorted(by_feature.items())
        ],
        "by_error_kind": [
            {"kind": kind, "count": count}
            for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1])
        ],
        "by_tier": [{"tier": tier, **counts} for tier, counts in sorted(by_tier.items())],
        "queue": {"queued": queued, "running": running, "jobs_by_status": dict(jobs_by_status)},
    }


@router.get("/failures", dependencies=[Depends(llm_monitor_rate_limit)])
def failures(
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=100, ge=1, le=500),
    _: CurrentUser = Depends(require_dev),
) -> dict:
    """Recent failed calls and terminally failed/canceled jobs, newest first."""
    sb = get_supabase()
    calls = (
        sb.table("llm_call_log")
        .select("id, feature, provider, model, tier, job_id, error_kind, error, duration_ms, created_at")
        .eq("ok", False)
        .gte("created_at", _cutoff(days))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    ).data or []
    jobs = (
        sb.table("llm_jobs")
        .select("*")
        .in_("status", ["failed", "canceled"])
        .gte("created_at", _cutoff(days))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    ).data or []
    projects = _project_labels([j.get("project_id") for j in jobs])
    return {
        "calls": calls,
        "jobs": [_job_view(j, projects) for j in jobs],
    }


@router.get("/queue", dependencies=[Depends(llm_monitor_rate_limit)])
def queue_state(_: CurrentUser = Depends(require_dev)) -> dict:
    """Everything queued or running right now, in claim order. queued/running
    are exact counts (the jobs list is capped at 200 rows)."""
    sb = get_supabase()
    jobs = (
        sb.table("llm_jobs")
        .select("*")
        .in_("status", ["queued", "running"])
        .order("priority")
        .order("created_at")
        .limit(200)
        .execute()
    ).data or []
    queued_count = (
        sb.table("llm_jobs").select("id", count="exact").eq("status", "queued").limit(1).execute()
    ).count or 0
    running_count = (
        sb.table("llm_jobs").select("id", count="exact").eq("status", "running").limit(1).execute()
    ).count or 0
    projects = _project_labels([j.get("project_id") for j in jobs])
    out = []
    position = 0
    for job in jobs:
        view = _job_view(job, projects)
        if job["status"] == "queued":
            position += 1
            view["position"] = position
        else:
            view["position"] = None
        out.append(view)
    return {"jobs": out, "queued": queued_count, "running": running_count}


def _job_or_404(job_id: str) -> dict:
    # A malformed id would 500 inside PostgREST (uuid cast); same clean 404 as
    # a missing job, mirroring the codebase's uuid-guard convention.
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found") from None
    rows = (
        get_supabase().table("llm_jobs").select("*").eq("id", job_id).limit(1).execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return rows[0]


@router.post("/jobs/{job_id}/retry", dependencies=[Depends(llm_monitor_rate_limit)])
def retry_job(job_id: str, user: CurrentUser = Depends(require_dev)) -> dict:
    """Fresh attempt cycle for a failed/canceled job. The old row stays as
    history; the domain row goes back to pending so the FE resumes polling."""
    job = _job_or_404(job_id)
    if job.get("status") not in ("failed", "canceled"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only failed or canceled jobs can be retried."
        )
    try:
        fresh = llm_queue.requeue_terminal(job, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit(user.id, "llm_monitor.retry", "llm_job", job_id, {"new_job_id": fresh["id"]})
    return _job_view(fresh, _project_labels([fresh.get("project_id")]))


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(llm_monitor_rate_limit)])
def cancel_job(job_id: str, user: CurrentUser = Depends(require_dev)) -> dict:
    """Cancel a QUEUED job. 409 once it is running or already terminal."""
    _job_or_404(job_id)
    canceled = llm_queue.cancel(job_id)
    if canceled is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only queued jobs can be canceled (this one already started or finished).",
        )
    audit(user.id, "llm_monitor.cancel", "llm_job", job_id, None)
    return _job_view(canceled, _project_labels([canceled.get("project_id")]))
