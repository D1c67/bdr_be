"""Certified Payroll — module settings: the company-wide subcontractor identity
singleton (cp_settings), the caller's signer profile for the paper CPR
Statement of Compliance (cp_signer_profiles), and the non-payroll registry
(cp_ignored_projects — raw timesheet names that are intentionally not CP; see
services/payroll_matching.py for how the registry is consulted at read time).

Signer-profile writes use require_cp_write like everything else: the accountant
is read-only module-wide, and only writers ever generate (and therefore sign)
reports.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_cp_read, require_cp_write
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    CpIgnoredProjectCreate,
    CpSettingsUpdate,
    CpSignerProfileUpdate,
)
from app.services.notifications import audit

router = APIRouter(prefix="/payroll", tags=["payroll"])

_DUPLICATE_IGNORED = "That name is already marked as non-payroll"


def _is_unique_violation(exc: Exception) -> bool:
    msg = str(exc)
    return "23505" in msg or "duplicate" in msg.lower()


@router.get("/settings")
def get_settings(user: CurrentUser = Depends(require_cp_read)):
    """The subcontractor identity printed on every report — get-or-create the
    bool-true-PK singleton so the FE always has a row to edit."""
    sb = get_supabase()
    rows = (sb.table("cp_settings").select("*").execute()).data or []
    if rows:
        return rows[0]
    try:
        return sb.table("cp_settings").insert({"id": True}).execute().data[0]
    except Exception as exc:  # noqa: BLE001 — a concurrent get-or-create won
        if not _is_unique_violation(exc):
            raise
        return (sb.table("cp_settings").select("*").execute()).data[0]


@router.put("/settings")
def update_settings(
    body: CpSettingsUpdate,
    user: CurrentUser = Depends(require_cp_write),
):
    patch = body.model_dump(exclude_unset=True, mode="json")
    row = (
        get_supabase()
        .table("cp_settings")
        .upsert({"id": True, **patch}, on_conflict="id")
        .execute()
    ).data[0]
    audit(user.id, "cp.settings_update", "cp_settings", None, patch)
    return row


@router.get("/profile")
def get_signer_profile(user: CurrentUser = Depends(require_cp_read)):
    """The caller's own signer identity. A missing row presents as empty
    defaults WITHOUT inserting — the row is created on first save only."""
    rows = (
        get_supabase()
        .table("cp_signer_profiles")
        .select("*")
        .eq("profile_id", user.id)
        .execute()
    ).data or []
    if rows:
        return rows[0]
    return {
        "profile_id": user.id,
        "first_name": None,
        "last_name": None,
        "job_title": None,
        "personal_email": None,
        "date_of_birth": None,
        "profile_completed": False,
    }


@router.put("/profile")
def update_signer_profile(
    body: CpSignerProfileUpdate,
    user: CurrentUser = Depends(require_cp_write),
):
    sb = get_supabase()
    patch = body.model_dump(exclude_unset=True, mode="json")
    row = (
        sb.table("cp_signer_profiles")
        .upsert({"profile_id": user.id, **patch}, on_conflict="profile_id")
        .execute()
    ).data[0]
    # profile_completed is computed, never client-supplied: the paper CPR
    # generator gates on it (an unsigned compliance statement is invalid).
    completed = bool(row.get("first_name") and row.get("last_name") and row.get("job_title"))
    row = (
        sb.table("cp_signer_profiles")
        .update({"profile_completed": completed})
        .eq("profile_id", user.id)
        .execute()
    ).data[0]
    audit(user.id, "cp.profile_update", "cp_signer_profile", user.id, patch)
    return row


@router.get("/ignored-projects")
def list_ignored_projects(user: CurrentUser = Depends(require_cp_read)):
    rows = (
        get_supabase()
        .table("cp_ignored_projects")
        .select("*")
        .order("raw_name")
        .execute()
    ).data or []
    return rows


@router.post("/ignored-projects", status_code=status.HTTP_201_CREATED)
def add_ignored_project(
    body: CpIgnoredProjectCreate,
    user: CurrentUser = Depends(require_cp_write),
):
    """Mark a raw timesheet name as intentionally non-payroll. Uniqueness is
    case/whitespace-insensitive (the lower/btrim index) — pre-check in the same
    key space so the common duplicate gets a clean 409, and translate the
    unique violation for the race the pre-check can't catch."""
    sb = get_supabase()
    name_key = body.raw_name.strip().lower()
    existing = (sb.table("cp_ignored_projects").select("raw_name").execute()).data or []
    if any((r.get("raw_name") or "").strip().lower() == name_key for r in existing):
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_IGNORED)
    try:
        row = (
            sb.table("cp_ignored_projects")
            .insert({**body.model_dump(mode="json"), "created_by": user.id})
            .execute()
        ).data[0]
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_IGNORED) from exc
        raise
    audit(
        user.id,
        "cp.ignored_project_add",
        "cp_ignored_project",
        row["id"],
        {"raw_name": body.raw_name},
    )
    return row


@router.delete("/ignored-projects/{ignored_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_ignored_project(
    ignored_id: str,
    user: CurrentUser = Depends(require_cp_write),
):
    deleted = (
        get_supabase()
        .table("cp_ignored_projects")
        .delete()
        .eq("id", ignored_id)
        .execute()
    ).data
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ignored project not found")
    audit(user.id, "cp.ignored_project_remove", "cp_ignored_project", ignored_id, {})
