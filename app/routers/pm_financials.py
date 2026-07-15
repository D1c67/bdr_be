"""Project Management — financials module: change orders, the schedule of
values, and G702/G703 pay applications.

Everything derived (contract value, per-line progress, certificates) is
computed on read by services/pm_financials; the only stored derivation is the
previous_completed snapshot taken when a pay app is created. Reads are any
PM-read role (accountant included, external estimator never); writes are
PM-write roles.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    ChangeOrderIn,
    ChangeOrderUpdate,
    PayAppCreate,
    PayAppLineUpdate,
    PayAppUpdate,
    SovLineIn,
    SovLineUpdate,
)
from app.services import pm_financials as fin
from app.services.notifications import audit
from app.services.pm import require_pm_project

router = APIRouter(prefix="/pm/projects/{project_id}", tags=["pm-financials"])

_CO_NUMBER_TAKEN = "That CO number is already in use on this project."
_LINE_NUMBER_TAKEN = "That line number is already in use on this project."


def _is_duplicate_co_number(exc: Exception) -> bool:
    msg = str(exc)
    return "change_orders_project_id_co_number_key" in msg or (
        "23505" in msg and "co_number" in msg
    )


def _is_duplicate_line_number(exc: Exception) -> bool:
    msg = str(exc)
    return "sov_lines_project_id_line_number_key" in msg or (
        "23505" in msg and "line_number" in msg
    )


def _is_duplicate_app_number(exc: Exception) -> bool:
    msg = str(exc)
    return "pay_applications_project_id_app_number_key" in msg or (
        "23505" in msg and "app_number" in msg
    )


def _is_sov_line_referenced(exc: Exception) -> bool:
    # The pay_app_lines→sov_lines FK is ON DELETE RESTRICT (23503).
    msg = str(exc)
    return "23503" in msg or "pay_app_lines" in msg


# ── Change orders ────────────────────────────────────────────────────────────


@router.get("/change-orders")
def list_change_orders(project_id: str, user: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    return (
        get_supabase()
        .table("change_orders")
        .select("*")
        .eq("project_id", project_id)
        .order("co_number")
        .execute()
    ).data or []


@router.post("/change-orders", status_code=status.HTTP_201_CREATED)
def create_change_order(
    project_id: str,
    body: ChangeOrderIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    payload["project_id"] = project_id
    payload["created_by"] = user.id
    try:
        created = get_supabase().table("change_orders").insert(payload).execute().data[0]
    except Exception as exc:  # noqa: BLE001 — unique violation → CO number re-use
        if _is_duplicate_co_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _CO_NUMBER_TAKEN) from exc
        raise
    audit(user.id, "co.create", "project", project_id,
          {"co_number": created["co_number"], "amount": payload["amount"]})
    return created


@router.patch("/change-orders/{co_id}")
def update_change_order(
    project_id: str,
    co_id: str,
    body: ChangeOrderUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    try:
        updated = (
            get_supabase()
            .table("change_orders")
            .update(patch)
            .eq("id", co_id)
            .eq("project_id", project_id)
            .execute()
        ).data
    except Exception as exc:  # noqa: BLE001 — unique violation → CO number re-use
        if _is_duplicate_co_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _CO_NUMBER_TAKEN) from exc
        raise
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change order not found")
    audit(user.id, "co.update", "project", project_id, patch)
    return updated[0]


@router.delete("/change-orders/{co_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_change_order(
    project_id: str,
    co_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    sb = get_supabase()
    rows = (
        sb.table("change_orders")
        .select("id, co_number, status")
        .eq("id", co_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change order not found")
    if rows[0]["status"] != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Approved or submitted change orders are part of the contract record "
            "and can't be deleted.",
        )
    sb.table("change_orders").delete().eq("id", co_id).execute()
    audit(user.id, "co.delete", "project", project_id, {"co_number": rows[0]["co_number"]})


# ── Schedule of values ───────────────────────────────────────────────────────


def _assert_co_on_project(project_id: str, change_order_id: str) -> None:
    rows = (
        get_supabase()
        .table("change_orders")
        .select("id")
        .eq("id", change_order_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That change order is not on this project"
        )


@router.get("/sov-lines")
def list_sov_lines(project_id: str, user: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    return (
        get_supabase()
        .table("sov_lines")
        .select("*")
        .eq("project_id", project_id)
        .order("sort_order")
        .order("line_number")
        .execute()
    ).data or []


@router.post("/sov-lines", status_code=status.HTTP_201_CREATED)
def create_sov_line(
    project_id: str,
    body: SovLineIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    if body.change_order_id:
        _assert_co_on_project(project_id, body.change_order_id)
    payload = body.model_dump(mode="json")
    payload["project_id"] = project_id
    payload["created_by"] = user.id
    try:
        created = get_supabase().table("sov_lines").insert(payload).execute().data[0]
    except Exception as exc:  # noqa: BLE001 — unique violation → line number re-use
        if _is_duplicate_line_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _LINE_NUMBER_TAKEN) from exc
        raise
    audit(user.id, "sov.create", "project", project_id,
          {"line_number": created["line_number"], "scheduled_value": payload["scheduled_value"]})
    return created


@router.patch("/sov-lines/{line_id}")
def update_sov_line(
    project_id: str,
    line_id: str,
    body: SovLineUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if patch.get("change_order_id"):
        _assert_co_on_project(project_id, patch["change_order_id"])
    try:
        updated = (
            get_supabase()
            .table("sov_lines")
            .update(patch)
            .eq("id", line_id)
            .eq("project_id", project_id)
            .execute()
        ).data
    except Exception as exc:  # noqa: BLE001 — unique violation → line number re-use
        if _is_duplicate_line_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _LINE_NUMBER_TAKEN) from exc
        raise
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOV line not found")
    audit(user.id, "sov.update", "project", project_id, patch)
    return updated[0]


@router.delete("/sov-lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sov_line(
    project_id: str,
    line_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    sb = get_supabase()
    rows = (
        sb.table("sov_lines")
        .select("id, line_number")
        .eq("id", line_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOV line not found")
    try:
        sb.table("sov_lines").delete().eq("id", line_id).execute()
    except Exception as exc:  # noqa: BLE001 — RESTRICT FK → line already billed
        if _is_sov_line_referenced(exc):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This line has billing history and can't be deleted.",
            ) from exc
        raise
    audit(user.id, "sov.delete", "project", project_id,
          {"line_number": rows[0]["line_number"]})


# ── Pay applications (G702/G703) ─────────────────────────────────────────────


def _series_totals(project_id: str) -> tuple[list[dict], dict[str, list[dict]], dict[str, dict]]:
    """All pay apps of a project + their lines (one query) + computed totals."""
    sb = get_supabase()
    apps = (
        sb.table("pay_applications")
        .select("*")
        .eq("project_id", project_id)
        .order("app_number")
        .execute()
    ).data or []
    lines_by_app: dict[str, list[dict]] = {}
    if apps:
        line_rows = (
            sb.table("pay_app_lines")
            .select("*")
            .in_("pay_app_id", [a["id"] for a in apps])
            .execute()
        ).data or []
        for row in line_rows:
            lines_by_app.setdefault(row["pay_app_id"], []).append(row)
    return apps, lines_by_app, fin.app_series_totals(apps, lines_by_app)


def _worksheet(project_id: str, app_id: str) -> dict:
    """The G703 worksheet: the app, its totals, and its lines joined with SOV
    line info + per-line computed columns."""
    apps, lines_by_app, totals = _series_totals(project_id)
    app = next((a for a in apps if a["id"] == app_id), None)
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pay application not found")
    sov = {
        s["id"]: s
        for s in (
            get_supabase()
            .table("sov_lines")
            .select("*")
            .eq("project_id", project_id)
            .execute()
        ).data
        or []
    }
    lines = []
    for row in lines_by_app.get(app_id, []):
        s = sov.get(row["sov_line_id"]) or {}
        lines.append(
            {
                **row,
                "line_number": s.get("line_number"),
                "description": s.get("description"),
                "scheduled_value": s.get("scheduled_value"),
                "sort_order": s.get("sort_order"),
                **fin.line_totals(row, s.get("scheduled_value")),
            }
        )
    lines.sort(key=lambda ln: (ln.get("sort_order") or 0, ln.get("line_number") or ""))
    # Computed figures ride flattened into the row (the FE PayApp type's
    # canonical shape); no raw column shares their names, so the spread is safe.
    return {**app, **totals[app_id], "lines": lines}


@router.get("/pay-applications")
def list_pay_applications(project_id: str, user: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    apps, _, totals = _series_totals(project_id)
    return [{**a, **totals[a["id"]]} for a in apps]


@router.post("/pay-applications", status_code=status.HTTP_201_CREATED)
def create_pay_application(
    project_id: str,
    body: PayAppCreate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    sb = get_supabase()
    existing = (
        sb.table("pay_applications")
        .select("id, app_number, status")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    payload = body.model_dump(mode="json")
    payload["project_id"] = project_id
    payload["app_number"] = max((a["app_number"] for a in existing), default=0) + 1
    payload["status"] = "draft"
    payload["created_by"] = user.id
    if payload.get("retainage_percent") is None:
        # Project default (pm_details); frozen onto the app so later default
        # edits don't rewrite issued applications.
        details = (
            sb.table("pm_details")
            .select("retainage_percent")
            .eq("project_id", project_id)
            .execute()
        ).data or []
        payload["retainage_percent"] = details[0]["retainage_percent"] if details else None
    try:
        created = sb.table("pay_applications").insert(payload).execute().data[0]
    except Exception as exc:  # noqa: BLE001 — unique violation → concurrent create
        if _is_duplicate_app_number(exc):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Another pay application was just created — try again.",
            ) from exc
        raise

    # One worksheet row per current SOV line; previous_completed snapshots
    # everything billed as this_period on prior non-rejected apps.
    prior_ids = [a["id"] for a in existing if a["status"] != "rejected"]
    prior_lines = []
    if prior_ids:
        prior_lines = (
            sb.table("pay_app_lines")
            .select("sov_line_id, this_period")
            .in_("pay_app_id", prior_ids)
            .execute()
        ).data or []
    previous = fin.previous_completed_by_line(prior_lines)
    sov_rows = (
        sb.table("sov_lines").select("id").eq("project_id", project_id).execute()
    ).data or []
    if sov_rows:
        sb.table("pay_app_lines").insert(
            [
                {
                    "pay_app_id": created["id"],
                    "sov_line_id": s["id"],
                    "previous_completed": str(previous.get(s["id"], Decimal(0))),
                    "this_period": "0",
                    "stored_materials": "0",
                }
                for s in sov_rows
            ]
        ).execute()

    audit(user.id, "payapp.create", "project", project_id,
          {"app_number": created["app_number"]})
    return _worksheet(project_id, created["id"])


@router.get("/pay-applications/{app_id}")
def get_pay_application(
    project_id: str, app_id: str, user: CurrentUser = Depends(require_pm_read)
):
    require_pm_project(project_id)
    return _worksheet(project_id, app_id)


@router.patch("/pay-applications/{app_id}")
def update_pay_application(
    project_id: str,
    app_id: str,
    body: PayAppUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    sb = get_supabase()
    # Crossing the rejected boundary on a NON-latest app rewrites billing
    # history: later apps snapshotted previous_completed from the non-rejected
    # set at their creation, so rejecting (or un-rejecting) an earlier app would
    # silently desync their G703 'previous' columns from the recomputed chain.
    if "status" in patch:
        apps = (
            sb.table("pay_applications")
            .select("id, app_number, status")
            .eq("project_id", project_id)
            .execute()
        ).data or []
        target = next((a for a in apps if a["id"] == app_id), None)
        if not target:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Pay application not found")
        crosses_rejected = (patch["status"] == "rejected") != (target["status"] == "rejected")
        if crosses_rejected and any(a["app_number"] > target["app_number"] for a in apps):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Later pay applications were built on this one — its rejected "
                "status can no longer change.",
            )
    updated = (
        sb.table("pay_applications")
        .update(patch)
        .eq("id", app_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pay application not found")
    audit(user.id, "payapp.update", "project", project_id, patch)
    return _worksheet(project_id, app_id)


@router.delete("/pay-applications/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pay_application(
    project_id: str,
    app_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    sb = get_supabase()
    apps = (
        sb.table("pay_applications")
        .select("id, app_number, status")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    app = next((a for a in apps if a["id"] == app_id), None)
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pay application not found")
    if app["status"] != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only draft pay applications can be deleted."
        )
    # Later apps' previous_completed snapshots include this one's this_period.
    if any(a["app_number"] > app["app_number"] for a in apps):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Later pay applications build on this one — delete them first.",
        )
    sb.table("pay_applications").delete().eq("id", app_id).execute()
    audit(user.id, "payapp.delete", "project", project_id,
          {"app_number": app["app_number"]})


@router.put("/pay-applications/{app_id}/lines/{line_id}")
def update_pay_app_line(
    project_id: str,
    app_id: str,
    line_id: str,
    body: PayAppLineUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    sb = get_supabase()
    apps = (
        sb.table("pay_applications")
        .select("id, app_number, status")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    target = next((a for a in apps if a["id"] == app_id), None)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pay application not found")
    if target["status"] != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only draft pay applications can be edited."
        )
    # Same reason deletes are blocked: later apps snapshotted previous_completed
    # from this app's this_period at their creation — editing it now would
    # silently desync their G703 'previous' columns from the recomputed chain.
    if any(a["app_number"] > target["app_number"] for a in apps):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Later pay applications build on this one — edit those instead.",
        )
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    updated = (
        sb.table("pay_app_lines")
        .update(patch)
        .eq("id", line_id)
        .eq("pay_app_id", app_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pay application line not found")
    audit(user.id, "payapp.update", "project", project_id,
          {"app_number": target["app_number"], "sov_line_id": updated[0]["sov_line_id"], **patch})
    return _worksheet(project_id, app_id)


# ── Module summary ───────────────────────────────────────────────────────────


@router.get("/financials")
def get_financials_summary(
    project_id: str, user: CurrentUser = Depends(require_pm_read)
):
    require_pm_project(project_id)
    sb = get_supabase()
    details = (
        sb.table("pm_details")
        .select("original_contract_value")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    original = details[0]["original_contract_value"] if details else None
    cos = (
        sb.table("change_orders")
        .select("amount, status")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    sov_rows = (
        sb.table("sov_lines")
        .select("scheduled_value")
        .eq("project_id", project_id)
        .execute()
    ).data or []

    apps, _, totals = _series_totals(project_id)
    billed = retainage = Decimal(0)
    latest = max(
        (a for a in apps if a.get("status") != "rejected"),
        key=lambda a: a["app_number"],
        default=None,
    )
    if latest:
        billed = fin.dec(totals[latest["id"]]["total_completed_and_stored"])
        retainage = fin.dec(totals[latest["id"]]["retainage_held"])

    return fin.financials_summary(
        original,
        fin.total_of([c for c in cos if c["status"] == "approved"], "amount"),
        fin.total_of(sov_rows, "scheduled_value"),
        billed,
        retainage,
        sum(1 for c in cos if c["status"] in ("draft", "submitted")),
        len(apps),
    )
