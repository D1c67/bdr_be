"""PM field operations — milestones, daily logs, RFIs, and manpower entries
(migration 0060). Reads are any PM-read role (accountant included, the external
estimator never); writes are PM-write roles. Every endpoint runs through
require_pm_project first, and every row lookup is scoped to the project, so an
id from another project is indistinguishable from a missing one.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    DailyLogIn,
    DailyLogUpdate,
    ManpowerIn,
    ManpowerUpdate,
    MilestoneIn,
    MilestoneUpdate,
    RFIIn,
    RFIUpdate,
)
from app.services.notifications import audit
from app.services.pm import require_pm_project

router = APIRouter(prefix="/pm/projects/{project_id}", tags=["pm-field"])


def _row_or_404(table: str, row_id: str, project_id: str, label: str) -> dict:
    rows = (
        get_supabase()
        .table(table)
        .select("*")
        .eq("id", row_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label} not found")
    return rows[0]


def _patch_of(body) -> dict:
    # exclude_unset (not exclude_none) so an explicit null clears a field.
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    return patch


def _reject_cleared(patch: dict, *fields: str) -> None:
    """NOT NULL columns: an explicit null would surface as a raw DB error."""
    cleared = sorted(f for f in fields if f in patch and patch[f] is None)
    if cleared:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{', '.join(cleared)} cannot be cleared"
        )


def _today() -> str:
    # G3 operates in Las Vegas: an RFI answered at 6pm local must not be
    # stamped with tomorrow's (UTC) date. The FE stamps local dates the same way.
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


# ── Milestones ───────────────────────────────────────────────────────────────


@router.get("/milestones")
def list_milestones(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    rows = (
        get_supabase()
        .table("pm_milestones")
        .select("*")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    # sort_order, then planned_date with undated milestones last.
    rows.sort(
        key=lambda r: (
            r.get("sort_order") or 0,
            r.get("planned_date") is None,
            r.get("planned_date") or "",
        )
    )
    return rows


@router.post("/milestones", status_code=status.HTTP_201_CREATED)
def create_milestone(
    project_id: str,
    body: MilestoneIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    payload.update({"project_id": project_id, "created_by": user.id})
    created = get_supabase().table("pm_milestones").insert(payload).execute().data[0]
    audit(user.id, "milestone.create", "project", project_id,
          {"milestone_id": created.get("id"), "name": body.name})
    return created


@router.patch("/milestones/{milestone_id}")
def update_milestone(
    project_id: str,
    milestone_id: str,
    body: MilestoneUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    # Existence is guarded by the scoped UPDATE's empty result (404 below).
    patch = _patch_of(body)
    _reject_cleared(patch, "name", "sort_order")
    updated = (
        get_supabase()
        .table("pm_milestones")
        .update(patch)
        .eq("id", milestone_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Milestone not found")
    audit(user.id, "milestone.update", "project", project_id,
          {"milestone_id": milestone_id, "fields": sorted(patch)})
    return updated[0]


@router.delete("/milestones/{milestone_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_milestone(
    project_id: str,
    milestone_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    row = _row_or_404("pm_milestones", milestone_id, project_id, "Milestone")
    get_supabase().table("pm_milestones").delete().eq("id", milestone_id).eq(
        "project_id", project_id
    ).execute()
    audit(user.id, "milestone.delete", "project", project_id,
          {"milestone_id": milestone_id, "name": row.get("name")})


# ── Daily logs ───────────────────────────────────────────────────────────────


@router.get("/daily-logs")
def list_daily_logs(
    project_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    _: CurrentUser = Depends(require_pm_read),
):
    require_pm_project(project_id)
    query = get_supabase().table("daily_logs").select("*").eq("project_id", project_id)
    if date_from is not None:
        query = query.gte("log_date", date_from.isoformat())
    if date_to is not None:
        query = query.lte("log_date", date_to.isoformat())
    return (
        query.order("log_date", desc=True).order("created_at", desc=True).execute()
    ).data or []


@router.post("/daily-logs", status_code=status.HTTP_201_CREATED)
def create_daily_log(
    project_id: str,
    body: DailyLogIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    payload.update({"project_id": project_id, "created_by": user.id})
    created = get_supabase().table("daily_logs").insert(payload).execute().data[0]
    audit(user.id, "dailylog.create", "project", project_id,
          {"daily_log_id": created.get("id"), "log_date": payload["log_date"]})
    return created


@router.patch("/daily-logs/{log_id}")
def update_daily_log(
    project_id: str,
    log_id: str,
    body: DailyLogUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    # Existence is guarded by the scoped UPDATE's empty result (404 below).
    patch = _patch_of(body)
    _reject_cleared(patch, "log_date", "work_performed")
    updated = (
        get_supabase()
        .table("daily_logs")
        .update(patch)
        .eq("id", log_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Daily log not found")
    audit(user.id, "dailylog.update", "project", project_id,
          {"daily_log_id": log_id, "fields": sorted(patch)})
    return updated[0]


@router.delete("/daily-logs/{log_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_daily_log(
    project_id: str,
    log_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    row = _row_or_404("daily_logs", log_id, project_id, "Daily log")
    # Linked manpower_entries survive (FK is ON DELETE SET NULL).
    get_supabase().table("daily_logs").delete().eq("id", log_id).eq(
        "project_id", project_id
    ).execute()
    audit(user.id, "dailylog.delete", "project", project_id,
          {"daily_log_id": log_id, "log_date": row.get("log_date")})


# ── RFIs ─────────────────────────────────────────────────────────────────────

_RFI_NUMBER_CONFLICT = "RFI numbering conflicted with a concurrent save — please retry"


def _is_rfi_number_conflict(exc: Exception) -> bool:
    msg = str(exc)
    return "rfis_project_id_rfi_number_key" in msg or (
        "23505" in msg and "rfi_number" in msg
    )


def _next_rfi_number(project_id: str) -> int:
    rows = (
        get_supabase()
        .table("rfis")
        .select("rfi_number")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    return max((r["rfi_number"] for r in rows if r.get("rfi_number") is not None), default=0) + 1


@router.get("/rfis")
def list_rfis(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    return (
        get_supabase()
        .table("rfis")
        .select("*")
        .eq("project_id", project_id)
        .order("rfi_number")
        .execute()
    ).data or []


@router.post("/rfis", status_code=status.HTTP_201_CREATED)
def create_rfi(
    project_id: str,
    body: RFIIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    payload.update({"project_id": project_id, "created_by": user.id})
    # max+1 can race a concurrent create into the (project_id, rfi_number)
    # unique — one recompute absorbs it, a second collision surfaces as a 409.
    created = None
    last_exc: Exception | None = None
    for _ in range(2):
        payload["rfi_number"] = _next_rfi_number(project_id)
        try:
            created = get_supabase().table("rfis").insert(payload).execute().data[0]
            break
        except Exception as exc:  # noqa: BLE001 — unique violation → recompute
            if not _is_rfi_number_conflict(exc):
                raise
            last_exc = exc
    if created is None:
        raise HTTPException(status.HTTP_409_CONFLICT, _RFI_NUMBER_CONFLICT) from last_exc
    audit(user.id, "rfi.create", "project", project_id,
          {"rfi_id": created.get("id"), "rfi_number": payload["rfi_number"],
           "subject": body.subject})
    return created


@router.patch("/rfis/{rfi_id}")
def update_rfi(
    project_id: str,
    rfi_id: str,
    body: RFIUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    current = _row_or_404("rfis", rfi_id, project_id, "RFI")
    patch = _patch_of(body)
    _reject_cleared(patch, "subject", "question", "status")
    # Recording an answer on an open RFI marks it answered without a second
    # status edit; an explicit status in the same patch wins.
    if patch.get("answer") and "status" not in patch and current.get("status") == "open":
        patch["status"] = "answered"
        if not patch.get("answered_at"):
            patch["answered_at"] = _today()
    updated = (
        get_supabase()
        .table("rfis")
        .update(patch)
        .eq("id", rfi_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFI not found")
    audit(user.id, "rfi.update", "project", project_id,
          {"rfi_id": rfi_id, "rfi_number": current.get("rfi_number"),
           "fields": sorted(patch)})
    return updated[0]


@router.delete("/rfis/{rfi_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rfi(
    project_id: str,
    rfi_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    row = _row_or_404("rfis", rfi_id, project_id, "RFI")
    # Numbers are NOT re-sequenced: a deleted RFI leaves a gap, keeping every
    # number ever referenced in correspondence unambiguous.
    get_supabase().table("rfis").delete().eq("id", rfi_id).eq(
        "project_id", project_id
    ).execute()
    audit(user.id, "rfi.delete", "project", project_id,
          {"rfi_id": rfi_id, "rfi_number": row.get("rfi_number")})


# ── Manpower ─────────────────────────────────────────────────────────────────


def _validate_daily_log(daily_log_id: str, project_id: str) -> None:
    rows = (
        get_supabase()
        .table("daily_logs")
        .select("id, project_id")
        .eq("id", daily_log_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Daily log not found")
    if rows[0].get("project_id") != project_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That daily log belongs to a different project"
        )


@router.get("/manpower")
def list_manpower(
    project_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    _: CurrentUser = Depends(require_pm_read),
):
    require_pm_project(project_id)
    query = (
        get_supabase().table("manpower_entries").select("*").eq("project_id", project_id)
    )
    if date_from is not None:
        query = query.gte("work_date", date_from.isoformat())
    if date_to is not None:
        query = query.lte("work_date", date_to.isoformat())
    return (query.order("work_date", desc=True).execute()).data or []


@router.post("/manpower", status_code=status.HTTP_201_CREATED)
def create_manpower(
    project_id: str,
    body: ManpowerIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    if payload.get("daily_log_id"):
        _validate_daily_log(payload["daily_log_id"], project_id)
    payload.update({"project_id": project_id, "created_by": user.id})
    created = get_supabase().table("manpower_entries").insert(payload).execute().data[0]
    audit(user.id, "manpower.create", "project", project_id,
          {"manpower_id": created.get("id"), "work_date": payload["work_date"],
           "classification": body.classification, "workers": body.workers})
    return created


@router.patch("/manpower/{entry_id}")
def update_manpower(
    project_id: str,
    entry_id: str,
    body: ManpowerUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    # Existence is guarded by the scoped UPDATE's empty result (404 below).
    patch = _patch_of(body)
    _reject_cleared(patch, "work_date", "classification", "workers")
    if patch.get("daily_log_id"):  # explicit null just unlinks — always allowed
        _validate_daily_log(patch["daily_log_id"], project_id)
    updated = (
        get_supabase()
        .table("manpower_entries")
        .update(patch)
        .eq("id", entry_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Manpower entry not found")
    audit(user.id, "manpower.update", "project", project_id,
          {"manpower_id": entry_id, "fields": sorted(patch)})
    return updated[0]


@router.delete("/manpower/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_manpower(
    project_id: str,
    entry_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    row = _row_or_404("manpower_entries", entry_id, project_id, "Manpower entry")
    get_supabase().table("manpower_entries").delete().eq("id", entry_id).eq(
        "project_id", project_id
    ).execute()
    audit(user.id, "manpower.delete", "project", project_id,
          {"manpower_id": entry_id, "work_date": row.get("work_date")})
