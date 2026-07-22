"""Certified Payroll — classifications and prevailing-wage rates (thin CRUD).

Rates are strictly 1:1 with a classification (unique FK). total_hourly is
never accepted from the client — it is always recomputed server-side as
hourly_rate + pension + health_welfare + training + other (the legacy CPR
formula: overtime/doubletime and dues do not participate). The legacy /seed
endpoints are intentionally not ported — reference data arrives via the
migration script.
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_cp_read, require_cp_write
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    CpClassificationCreate,
    CpClassificationPatch,
    CpRateCreate,
    CpRatePatch,
)
from app.services.notifications import audit

router = APIRouter(prefix="/payroll", tags=["payroll"])

_CODE_TAKEN = "Classification code is already in use"
_RATE_EXISTS = "Classification already has a rate"
# total_hourly inputs: base wage + the four fringes.
_TOTAL_COMPONENTS = ("hourly_rate", "pension", "health_welfare", "training", "other")


def _is_duplicate_code(exc: Exception) -> bool:
    msg = str(exc)
    return "cp_classifications_code_unique_idx" in msg or ("23505" in msg and "code" in msg)


def _is_duplicate_rate(exc: Exception) -> bool:
    msg = str(exc)
    return "cp_rates_classification_id" in msg or ("23505" in msg and "classification_id" in msg)


def _code_taken(code: str, exclude: str | None = None) -> bool:
    """Pre-check against the lower(btrim(code)) unique index; the table is tiny
    so normalized comparison in Python is exact and cheap."""
    key = code.strip().lower()
    rows = get_supabase().table("cp_classifications").select("id, code").execute().data or []
    return any(
        (r.get("code") or "").strip().lower() == key and r["id"] != exclude for r in rows
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


# ── Classifications ──────────────────────────────────────────────────────────


@router.get("/classifications")
def list_classifications(user: CurrentUser = Depends(require_cp_read)):
    return (
        get_supabase()
        .table("cp_classifications")
        .select("*, cp_rates(*)")
        .order("display_order")
        .order("code")
        .execute()
    ).data or []


@router.post("/classifications", status_code=status.HTTP_201_CREATED)
def create_classification(
    body: CpClassificationCreate,
    user: CurrentUser = Depends(require_cp_write),
):
    if _code_taken(body.code):
        raise HTTPException(status.HTTP_409_CONFLICT, _CODE_TAKEN)
    insert = get_supabase().table("cp_classifications").insert(body.model_dump(mode="json"))
    try:
        created = insert.execute().data[0]
    except Exception as exc:  # noqa: BLE001 — re-raised unless it's the dup race
        if _is_duplicate_code(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _CODE_TAKEN) from exc
        raise
    audit(user.id, "cp.classification_create", "classification", created["id"],
          {"code": created["code"]})
    return created


@router.patch("/classifications/{classification_id}")
def update_classification(
    classification_id: str,
    body: CpClassificationPatch,
    user: CurrentUser = Depends(require_cp_write),
):
    """exclude_unset semantics: an explicit null clears a nullable field."""
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if patch.get("code") and _code_taken(patch["code"], exclude=classification_id):
        raise HTTPException(status.HTTP_409_CONFLICT, _CODE_TAKEN)
    update = get_supabase().table("cp_classifications").update(patch).eq("id", classification_id)
    try:
        updated = update.execute().data
    except Exception as exc:  # noqa: BLE001 — re-raised unless it's the dup race
        if _is_duplicate_code(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _CODE_TAKEN) from exc
        raise
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classification not found")
    audit(user.id, "cp.classification_update", "classification", classification_id, patch)
    return updated[0]


# ── Rates ────────────────────────────────────────────────────────────────────


@router.get("/rates")
def list_rates(
    classification_id: str | None = None,
    user: CurrentUser = Depends(require_cp_read),
):
    q = get_supabase().table("cp_rates").select("*, cp_classifications(id, code, name)")
    if classification_id is not None:
        q = q.eq("classification_id", classification_id)
    return q.execute().data or []


@router.post("/rates", status_code=status.HTTP_201_CREATED)
def create_rate(
    body: CpRateCreate,
    user: CurrentUser = Depends(require_cp_write),
):
    _require_classification(body.classification_id)
    existing = (
        get_supabase()
        .table("cp_rates")
        .select("id")
        .eq("classification_id", body.classification_id)
        .limit(1)
        .execute()
    ).data
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, _RATE_EXISTS)

    row = body.model_dump(mode="json")
    total = body.hourly_rate + body.pension + body.health_welfare + body.training + body.other
    row["total_hourly"] = str(total)
    if row.get("effective_date") is None:
        row["effective_date"] = date.today().isoformat()
    insert = get_supabase().table("cp_rates").insert(row)
    try:
        created = insert.execute().data[0]
    except Exception as exc:  # noqa: BLE001 — re-raised unless it's the dup race
        if _is_duplicate_rate(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _RATE_EXISTS) from exc
        raise
    audit(user.id, "cp.rate_create", "rate", created["id"],
          {"classification_id": body.classification_id})
    return created


@router.patch("/rates/{rate_id}")
def update_rate(
    rate_id: str,
    body: CpRatePatch,
    user: CurrentUser = Depends(require_cp_write),
):
    rows = (
        get_supabase().table("cp_rates").select("*").eq("id", rate_id).limit(1).execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rate not found")
    existing = rows[0]
    # exclude_none too: every cp_rates column is NOT NULL, so an explicit null
    # is dropped (keep the current value) rather than erroring in Postgres.
    patch = body.model_dump(exclude_unset=True, exclude_none=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    # Recompute total_hourly from the merged (existing + patch) components —
    # PostgREST numerics arrive as strings/floats, hence Decimal(str(...)).
    merged = {k: patch.get(k, existing.get(k)) for k in _TOTAL_COMPONENTS}
    patch["total_hourly"] = str(sum(Decimal(str(merged[k])) for k in _TOTAL_COMPONENTS))
    updated = (
        get_supabase().table("cp_rates").update(patch).eq("id", rate_id).execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rate not found")
    audit(user.id, "cp.rate_update", "rate", rate_id, patch)
    return updated[0]
