"""Certified Payroll — company-wide employee registry (thin CRUD).

Employees are shared HR data (the `employees` table is deliberately
unprefixed): CP consumes them for certified reports, but other BDR modules may
reuse them. Deactivation is SOFT (is_active=false) — payroll history rows FK
into employees and must survive. SSN policy: last four digits only; no
encrypted SSN is stored anywhere.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_cp_read, require_cp_write
from app.core.supabase_client import get_supabase
from app.models.schemas import CpEmployeeCreate, CpEmployeePatch
from app.services.notifications import audit

router = APIRouter(prefix="/payroll/employees", tags=["payroll"])

_EMPLOYEE_SELECT = "*, cp_classifications(id, code, name, is_field)"
_ID_TAKEN = "Employee ID is already in use"


def _is_duplicate_employee_id(exc: Exception) -> bool:
    msg = str(exc)
    return "employees_employee_id_unique_idx" in msg or (
        "23505" in msg and "employee_id" in msg
    )


def _employee_id_taken(value: str, exclude: str | None = None) -> bool:
    """Pre-check against the lower(btrim(employee_id)) unique index. The
    registry is small (the FE fetches it whole), so normalized comparison in
    Python is exact and cheap. `exclude` is a row id to ignore (edit flows)."""
    key = value.strip().lower()
    if not key:
        return False
    rows = get_supabase().table("employees").select("id, employee_id").execute().data or []
    return any(
        (r.get("employee_id") or "").strip().lower() == key and r["id"] != exclude
        for r in rows
    )


def _require_classification(classification_id: str) -> None:
    rows = (
        get_supabase()
        .table("cp_classifications")
        .select("id")
        .eq("id", classification_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classification not found")


@router.get("")
def list_employees(
    active: bool | None = None,
    user: CurrentUser = Depends(require_cp_read),
):
    """The whole registry (the FE filters/searches client-side)."""
    q = get_supabase().table("employees").select(_EMPLOYEE_SELECT)
    if active is not None:
        q = q.eq("is_active", active)
    return q.order("last_name").order("first_name").execute().data or []


# Declared before /{employee_id} so "check-id" never matches as a row id.
@router.get("/check-id")
def check_employee_id(
    employee_id: str,
    exclude: str | None = None,
    user: CurrentUser = Depends(require_cp_read),
):
    """Live uniqueness probe for the employee-ID field on the create/edit forms."""
    return {"taken": _employee_id_taken(employee_id, exclude)}


@router.get("/{employee_id}")
def get_employee(
    employee_id: str,
    user: CurrentUser = Depends(require_cp_read),
):
    rows = (
        get_supabase()
        .table("employees")
        .select(_EMPLOYEE_SELECT)
        .eq("id", employee_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    return rows[0]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_employee(
    body: CpEmployeeCreate,
    user: CurrentUser = Depends(require_cp_write),
):
    if body.classification_id:
        _require_classification(body.classification_id)
    if body.employee_id and _employee_id_taken(body.employee_id):
        raise HTTPException(status.HTTP_409_CONFLICT, _ID_TAKEN)
    insert = (
        get_supabase()
        .table("employees")
        .insert({**body.model_dump(mode="json"), "created_by": user.id})
    )
    try:
        created = insert.execute().data[0]
    except Exception as exc:  # noqa: BLE001 — re-raised unless it's the dup race
        if _is_duplicate_employee_id(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _ID_TAKEN) from exc
        raise
    audit(
        user.id,
        "cp.employee_create",
        "employee",
        created["id"],
        {"name": f"{created['first_name']} {created['last_name']}"},
    )
    return created


@router.patch("/{employee_id}")
def update_employee(
    employee_id: str,
    body: CpEmployeePatch,
    user: CurrentUser = Depends(require_cp_write),
):
    """exclude_unset semantics: an explicit null clears a nullable field."""
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if patch.get("classification_id"):
        _require_classification(patch["classification_id"])
    if patch.get("employee_id") and _employee_id_taken(patch["employee_id"], exclude=employee_id):
        raise HTTPException(status.HTTP_409_CONFLICT, _ID_TAKEN)
    update = get_supabase().table("employees").update(patch).eq("id", employee_id)
    try:
        updated = update.execute().data
    except Exception as exc:  # noqa: BLE001 — re-raised unless it's the dup race
        if _is_duplicate_employee_id(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _ID_TAKEN) from exc
        raise
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    audit(user.id, "cp.employee_update", "employee", employee_id, patch)
    return updated[0]


@router.delete("/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_employee(
    employee_id: str,
    confirmed: bool = False,
    user: CurrentUser = Depends(require_cp_write),
):
    """Soft deactivate — never a row delete (payroll history FKs must survive).
    The confirmation flag is legacy CPR parity: the FE asks twice."""
    if not confirmed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Deactivation requires confirmed=true")
    updated = (
        get_supabase()
        .table("employees")
        .update({"is_active": False})
        .eq("id", employee_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    audit(user.id, "cp.employee_deactivate", "employee", employee_id, None)
