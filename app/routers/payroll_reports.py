"""Certified Payroll — weekly report router: list/create/detail/delete, the two
file uploads (timesheet + Gusto payroll detail), rematch, and the finalize →
submit hand-off.

Reads are any CP-read role (accountant included, the external estimator never);
writes are CP-write roles. Upload handlers are the module's only `async def`
routes (multipart needs `await file.read()`); every Supabase/storage touch
inside them goes through run_in_threadpool — the pm_documents.py shape.
"""

from typing import get_args

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_cp_read, require_cp_write
from app.core.ratelimit import upload_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import CpPayrollStatus, CpReportCreate
from app.routers.files import _read_capped
from app.services import payroll_matching, payroll_reports
from app.services.notifications import audit

router = APIRouter(prefix="/payroll/reports", tags=["payroll"])

_STATUSES = frozenset(get_args(CpPayrollStatus))

_ALLOWED_EXTENSIONS = (".csv", ".xlsx", ".xls")
# Strict allowlist — application/octet-stream is deliberately rejected so a
# mislabeled binary never reaches the pandas parsers.
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


def _validate_spreadsheet_upload(file: UploadFile) -> None:
    if not (file.filename or "").lower().endswith(_ALLOWED_EXTENSIONS):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid file type — CSV, XLSX or XLS only"
        )
    if (file.content_type or "") not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid content type — CSV, XLSX or XLS only"
        )


def _require_not_submitted(report: dict) -> None:
    if report["status"] == "submitted":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Report has been submitted — it can no longer be edited"
        )


@router.get("")
def list_reports(
    status_filter: str | None = Query(None, alias="status"),
    user: CurrentUser = Depends(require_cp_read),
):
    if status_filter is not None and status_filter not in _STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown status: {status_filter}")
    query = get_supabase().table("cp_payroll_reports").select("*")
    if status_filter is not None:
        query = query.eq("status", status_filter)
    return query.order("week_start_date", desc=True).execute().data or []


@router.post("", status_code=status.HTTP_201_CREATED)
def create_report(body: CpReportCreate, user: CurrentUser = Depends(require_cp_write)):
    """Any date in the target week — the service snaps it to Sun–Sat. A taken
    week 409s with existing_id so the FE can link to the existing report."""
    row = payroll_reports.create_report(body.week_start_date, user.id)
    audit(
        user.id,
        "cp_report.create",
        "cp_payroll_report",
        row["id"],
        {"week_start_date": row["week_start_date"]},
    )
    return row


@router.get("/{report_id}")
def get_report(report_id: str, user: CurrentUser = Depends(require_cp_read)):
    report = payroll_reports.require_cp_report(report_id)
    return payroll_reports.build_report_detail(report)


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report(
    report_id: str,
    confirmed: bool = False,
    user: CurrentUser = Depends(require_cp_write),
):
    """Permanent — entries, detail rows, CPR records and stored files all go."""
    if not confirmed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Delete requires confirmation — pass confirmed=true"
        )
    report = payroll_reports.require_cp_report(report_id)
    # delete_report enforces the submitted-immutability invariant (a submitted
    # report is a legally-retained record) and 409s if it is already submitted.
    payroll_reports.delete_report(report, user.id)
    audit(
        user.id,
        "cp_report.delete",
        "cp_payroll_report",
        report_id,
        {"week_start_date": report["week_start_date"]},
    )


@router.post("/{report_id}/timesheet", dependencies=[Depends(upload_rate_limit)])
async def upload_timesheet(
    report_id: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_cp_write),
):
    report = await run_in_threadpool(payroll_reports.require_cp_report, report_id)
    _require_not_submitted(report)
    _validate_spreadsheet_upload(file)
    content = await _read_capped(file, get_settings().upload_max_bytes)
    summary = await run_in_threadpool(
        payroll_reports.process_timesheet, report, file.filename, content, user.id
    )
    await run_in_threadpool(
        audit,
        user.id,
        "cp_report.timesheet_upload",
        "cp_payroll_report",
        report_id,
        {
            "filename": file.filename,
            "total_entries": summary["total_entries"],
            "unmatched_employees": len(summary["unmatched_employees"]),
            "unmatched_projects": len(summary["unmatched_projects"]),
        },
    )
    return summary


@router.post("/{report_id}/payroll-detail", dependencies=[Depends(upload_rate_limit)])
async def upload_payroll_detail(
    report_id: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_cp_write),
):
    report = await run_in_threadpool(payroll_reports.require_cp_report, report_id)
    _require_not_submitted(report)
    _validate_spreadsheet_upload(file)
    content = await _read_capped(file, get_settings().upload_max_bytes)
    summary = await run_in_threadpool(
        payroll_reports.process_payroll_detail, report, file.filename, content, user.id
    )
    await run_in_threadpool(
        audit,
        user.id,
        "cp_report.detail_upload",
        "cp_payroll_report",
        report_id,
        {
            "filename": file.filename,
            "total_entries": summary["total_entries"],
            "unmatched_employees": len(summary["unmatched_employees"]),
        },
    )
    return summary


@router.post("/{report_id}/rematch")
def rematch(report_id: str, user: CurrentUser = Depends(require_cp_write)):
    """Re-run matching from the stored raw names — no re-upload needed after
    registry/employee/project edits. 404/409 (submitted) raised by the service."""
    summary = payroll_matching.rematch_report(report_id)
    audit(
        user.id,
        "cp_report.rematch",
        "cp_payroll_report",
        report_id,
        {
            "total_entries": summary["total_entries"],
            "unmatched_employees": len(summary["unmatched_employees"]),
            "unmatched_projects": len(summary["unmatched_projects"]),
        },
    )
    return summary


@router.post("/{report_id}/finalize")
def finalize(report_id: str, user: CurrentUser = Depends(require_cp_write)):
    report = payroll_reports.require_cp_report(report_id)
    row = payroll_reports.finalize_report(report, user.id)
    audit(user.id, "cp_report.finalize", "cp_payroll_report", report_id, None)
    return row


@router.post("/{report_id}/submit")
def submit(report_id: str, user: CurrentUser = Depends(require_cp_write)):
    report = payroll_reports.require_cp_report(report_id)
    row = payroll_reports.submit_report(report, user.id)
    audit(user.id, "cp_report.submit", "cp_payroll_report", report_id, None)
    return row


@router.get("/{report_id}/entries")
def list_entries(
    report_id: str,
    employee_id: str | None = None,
    project_id: str | None = None,
    user: CurrentUser = Depends(require_cp_read),
):
    payroll_reports.require_cp_report(report_id)
    query = get_supabase().table("cp_time_entries").select("*").eq(
        "payroll_report_id", report_id
    )
    if employee_id is not None:
        query = query.eq("employee_id", employee_id)
    if project_id is not None:
        query = query.eq("project_id", project_id)
    return query.order("work_date").order("start_time").execute().data or []
