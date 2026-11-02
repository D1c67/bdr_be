"""The payroll namespace (docs/ASSISTANT_PHASE4_PLAN.md 3.13, 3.14): aggregate
reads of Certified Payroll. Never an employee, a wage, a rate, an SSN, a
file name, a storage path or a person's id.

  cp_week_summary    GET /payroll/reports/summary?week_start=  one week's totals
  cp_report_status   GET /payroll/reports                      weekly report states

Both are deferred, tagged sub_app "certified_payroll", and carry
retention_class "payroll_aggregate", so a stored result expires after 7 days.
Each trimmer is a whitelist: a key the route adds later never reaches the
model until it is listed here, and the firewall denies the payroll routes
that do carry personal data (app/assistant/firewall.py).

Like tools/splits.py this module imports no router or service.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from app.assistant.registry import (
    EFFECT_READ,
    LIMIT_PROP,
    RESPONSE_FORMAT_PROP,
    RETENTION_PAYROLL_AGGREGATE,
    ToolRequest,
    ToolSpec,
    TrimContext,
    nullable,
    register,
    schema,
)
from app.assistant.tools._common import (
    PT,
    ToolArgumentError,
    clip,
    compact,
    narrowing_hint,
    parse_date,
    pt,
    rows_of,
)
from app.core.roles import INTERNAL_ROLES

# The route side's literals (app/routers/payroll_reports.py); a test asserts equality.
SUMMARY_GET = ("GET", "/payroll/reports/summary")
REPORTS_GET = ("GET", "/payroll/reports")

# payroll_reports statuses (CpPayrollStatus; a test asserts equality).
REPORT_STATUSES = ("draft", "awaiting_timesheet", "awaiting_payroll_detail", "processing", "processed", "submitted")
WEEKS = ("last_week", "this_week")

NO_REPORT_HINT = "No payroll report exists for that week."
SMALL_CELL_HINT = (
    "Projects with fewer than 3 employees that week are not listed on their own; their hours sit in "
    "other_hours. Do not try to work out who they belong to."
)


def sunday_of(day: date) -> date:
    """The Sunday that starts `day`'s payroll week (Sunday to Saturday)."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def week_start_for(week: Any, *, today: date | None = None) -> str:
    """The Sunday to ask for: `last_week`, `this_week` or any date of the
    wanted week (snapped to its Sunday). Null means last week."""
    now = today or datetime.now(PT).date()
    text = str(week or "last_week").strip()
    if text == "this_week":
        return sunday_of(now).isoformat()
    if text == "last_week":
        return (sunday_of(now) - timedelta(days=7)).isoformat()
    return sunday_of(date.fromisoformat(parse_date(text, "week"))).isoformat()


def _summary_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    try:
        start = week_start_for(args.get("week"))
    except ToolArgumentError:
        raise ToolArgumentError("week must be last_week, this_week or a date as YYYY-MM-DD.") from None
    return [ToolRequest(route=SUMMARY_GET, path="/payroll/reports/summary", params={"week_start": start},
                        optional=True)]  # a week with no report answers 404 no_report: a plain answer, not a failure


_SUMMARY_KEYS = (
    "week_start", "week_end", "status", "timesheet_uploaded", "payroll_detail_uploaded", "total_hours",
    "total_employees", "entry_count", "unmatched_employee_entries", "unmatched_project_entries",
    "record_count", "latest_revision", "other_hours",
)


def _summary_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = payloads[0] if payloads else None
    if not isinstance(body, Mapping) or not body:
        return {"found": False, "hint": NO_REPORT_HINT}
    out: dict[str, Any] = {k: body.get(k) for k in _SUMMARY_KEYS if k in body}
    out["id"] = ctx.short(body.get("id")) if body.get("id") else None
    out["finalized"] = pt(body.get("finalized_at"))
    out["submitted"] = pt(body.get("submitted_at"))
    out["finalize_issues"] = [clip(i, 160) for i in (body.get("finalize_issues") or []) if isinstance(i, str)][:10]
    out["stale_reasons"] = [clip(i, 80) for i in (body.get("stale_reasons") or []) if isinstance(i, str)][:10]
    rows = rows_of(body.get("hours_by_project"))
    shown = [
        compact({"number": r.get("number"), "name": clip(r.get("name"), 120), "hours": r.get("hours"),
                 "employees": r.get("employees")})
        for r in rows[: max(ctx.limit, 1)]
    ]
    out["hours_by_project"] = shown
    out["hours_by_project_hint"] = narrowing_hint(len(rows), len(shown), "Raise limit to see more projects.")
    out["hint"] = SMALL_CELL_HINT
    # Empty lists are answers (nothing blocks finalizing), so only None goes.
    return {k: v for k, v in out.items() if v is not None}


register(ToolSpec(
    name="cp_week_summary",
    namespace="payroll",
    sub_app="certified_payroll",
    route=SUMMARY_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "week": nullable({"type": "string", "description": (
            "last_week (default when null), this_week, or any date of the wanted week as YYYY-MM-DD.")}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_summary_trim,
    progress_label="Checking the payroll week",
    roles=INTERNAL_ROLES,
    description=(
        "One Certified Payroll week in aggregate: status, uploads, total hours, headcount, entries still unmatched, "
        "what blocks finalizing, and hours by project. Never names an employee or a wage. Projects with fewer than "
        "3 employees are folded into other_hours; do not guess who they are."
    ),
    requests=_summary_requests,
    taint_fields=("name",),
    retention_class=RETENTION_PAYROLL_AGGREGATE,
    deferred=True,
    keywords=("payroll", "certified", "hours", "week", "timesheet", "headcount", "cpr", "finalize"),
))


# ── cp_report_status ─────────────────────────────────────────────────────────


def _reports_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    status = args.get("status")
    params: dict[str, Any] = {}
    if status not in (None, ""):
        if status not in REPORT_STATUSES:
            raise ToolArgumentError(f"status must be one of {', '.join(REPORT_STATUSES)}.")
        params["status"] = status
    return [ToolRequest(route=REPORTS_GET, path="/payroll/reports", params=params)]


def _reports_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    rows = rows_of(payloads[0] if payloads else None)
    shown = [
        compact({
            "id": ctx.short(r.get("id")) if r.get("id") else None,
            "week_start": r.get("week_start_date"),
            "week_end": r.get("week_end_date"),
            "status": r.get("status"),
            "total_hours": r.get("total_hours"),
            "total_employees": r.get("total_employees"),
            "finalized": pt(r.get("finalized_at")),
            "submitted": pt(r.get("submitted_at")),
            "timesheet_uploaded": bool(r.get("timesheet_filename")),
            "payroll_detail_uploaded": bool(r.get("payroll_detail_filename")),
        })
        for r in rows[: ctx.limit]
    ]
    # Booleans are answers even when false, so they are put back after compact.
    for item, r in zip(shown, rows[: ctx.limit], strict=False):
        item["timesheet_uploaded"] = bool(r.get("timesheet_filename"))
        item["payroll_detail_uploaded"] = bool(r.get("payroll_detail_filename"))
    out = {"reports": shown, "total": len(rows),
           "hint": narrowing_hint(len(rows), len(shown), "Filter by status or raise limit.")}
    return compact(out) | {"reports": shown}


register(ToolSpec(
    name="cp_report_status",
    namespace="payroll",
    sub_app="certified_payroll",
    route=REPORTS_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "status": nullable({"type": "string", "enum": list(REPORT_STATUSES),
                            "description": "Only reports in this state, or null for all."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_reports_trim,
    progress_label="Checking payroll reports",
    roles=INTERNAL_ROLES,
    description=(
        "The weekly Certified Payroll reports, newest first: week, status, total hours, headcount, whether the "
        "timesheet and payroll detail were uploaded, and when each was finalized or submitted. Use it to see which "
        "weeks are open. No employee, wage or file detail."
    ),
    requests=_reports_requests,
    retention_class=RETENTION_PAYROLL_AGGREGATE,
    deferred=True,
    keywords=("payroll", "certified", "reports", "weeks", "status", "submitted", "finalized", "open"),
))
