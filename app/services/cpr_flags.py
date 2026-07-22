"""Certified Payroll — CPR flag generation.

Analyzes report data to detect discrepancies, overtime patterns, shift
mismatches, and compliance issues before CPR download. Pure functions over the
in-memory structures built by services/cpr_generation.py — no database access.

Ported from the legacy cpr_flag_service with the merged-model changes:

- The legacy "project_group == PREVAILING_WAGE" guards became "the bucket key
  is an enrolled CP project" (report_data.projects holds enrolled projects
  only, so synthetic "bdr:/ext:/raw:" keys skip every compliance, overtime and
  shift-pattern project lookup).
- The discrepancy checks compare per-employee totals that now INCLUDE non-CP
  buckets — the timesheet side finally matches the Gusto side for employees
  with office/non-CP hours, so those no longer false-positive.
- Two new flag types: NON_CP_HOURS (info; one per employee/bucket — hours that
  count for OT allocation and pay proration but never reach a certified
  report) and UNKNOWN_PROJECT (error; "raw:" buckets — nothing matched a BDR
  project or the non-payroll registry). Unknown projects hard-block finalize
  upstream (services/payroll_reports); flagging here is defense in depth for
  registry edits between finalize and generate.
"""

import logging
from datetime import date
from decimal import Decimal

from app.services.payroll_ot import (
    TimeEntryInput,
    _calculate_early_start_ot,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
TOLERANCE = Decimal("0.25")


def generate_cpr_flags(
    report_data,
    ot_summaries,
    rows,
    bucket_sources: dict[str, str] | None = None,
) -> list[dict]:
    """Generate all CPR flags for a report.

    Args:
        report_data: ReportData with time_entries, detail_entries, employees,
                     projects (ENROLLED only), etc.
        ot_summaries: list[EmployeeProjectWeekSummary] from the OT calculator,
                      spanning enrolled AND non-CP buckets.
        rows: list[CprRowData] built rows (successfully matched employees).
        bucket_sources: non-enrolled bucket key -> human-readable source name.

    Returns:
        List of flag dicts ready for JSONB storage.
    """
    flags: list[dict] = []
    first_work_dates = _first_work_dates(report_data)

    flags.extend(_check_compliance(report_data, ot_summaries, rows, first_work_dates))
    flags.extend(_check_discrepancies(report_data, ot_summaries, rows, first_work_dates))
    flags.extend(_check_overtime(report_data, ot_summaries, first_work_dates))
    flags.extend(_check_shift_patterns(report_data, ot_summaries, first_work_dates))
    flags.extend(
        _check_non_cp(report_data, ot_summaries, bucket_sources or {}, first_work_dates)
    )

    return flags


def _first_work_dates(report_data) -> dict[str, date]:
    """Get earliest work_date per employee from time entries."""
    result: dict[str, date] = {}
    for entry in report_data.time_entries:
        if not entry.employee_id:
            continue
        emp_id = entry.employee_id
        if emp_id not in result or entry.work_date < result[emp_id]:
            result[emp_id] = entry.work_date
    return result


def _make_flag(
    flag_type: str,
    category: str,
    severity: str,
    employee_id: str,
    employee_name: str,
    message: str,
    first_work_date: date | None = None,
    project_id: str | None = None,
    project_number: str | None = None,
    project_title: str | None = None,
    hours: str | None = None,
) -> dict:
    flag = {
        "flag_type": flag_type,
        "category": category,
        "severity": severity,
        "employee_id": employee_id,
        "employee_name": employee_name,
        "project_id": project_id,
        "project_number": project_number,
        "project_title": project_title,
        "first_work_date": first_work_date.isoformat() if first_work_date else None,
        "message": message,
    }
    if hours is not None:
        flag["hours"] = hours
    return flag


def _check_compliance(report_data, ot_summaries, rows, first_work_dates) -> list[dict]:
    """Check for missing classification, rate, payroll detail, jurisdiction,
    and below-PW-rate — enrolled CP projects only."""
    flags: list[dict] = []

    # Index built rows by (emp_id, proj_id)
    built_keys = {(r.employee.id, r.project.id) for r in rows}

    # Index detail entries by employee_id
    detail_by_emp = {}
    for d in report_data.detail_entries:
        if d.employee_id:
            detail_by_emp[d.employee_id] = d

    for summary in ot_summaries:
        emp_id = summary.employee_id
        proj_id = summary.project_id

        # Enrolled projects only — bdr:/ext:/raw: buckets never carry
        # compliance obligations (see _check_non_cp for their flags).
        project = report_data.projects.get(proj_id)
        if not project:
            continue

        employee = report_data.employees.get(emp_id)
        if not employee:
            continue

        emp_name = employee.full_name
        fwd = first_work_dates.get(emp_id)
        proj_kwargs = {
            "project_id": proj_id,
            "project_number": project.project_number,
            "project_title": project.project_title,
        }

        classification = report_data.classifications.get(emp_id)

        # MISSING_CLASSIFICATION
        if not classification:
            flags.append(_make_flag(
                "MISSING_CLASSIFICATION", "compliance", "error",
                emp_id, emp_name,
                f"{emp_name} is on prevailing wage project {project.project_title} "
                "but has no classification assigned.",
                first_work_date=fwd, **proj_kwargs,
            ))
            continue

        # Skip non-field employees for rate/detail checks (they don't appear on CPR)
        if not classification.is_field:
            continue

        rate = report_data.rates.get(classification.id)

        # MISSING_RATE
        if not rate:
            flags.append(_make_flag(
                "MISSING_RATE", "compliance", "error",
                emp_id, emp_name,
                f"No prevailing wage rate found for {emp_name}'s classification "
                f"({classification.name}).",
                first_work_date=fwd, **proj_kwargs,
            ))
            continue

        # MISSING_PAYROLL_DETAIL
        if emp_id not in detail_by_emp:
            flags.append(_make_flag(
                "MISSING_PAYROLL_DETAIL", "compliance", "error",
                emp_id, emp_name,
                f"No payroll detail entry found for {emp_name}. Upload payroll detail data.",
                first_work_date=fwd, **proj_kwargs,
            ))
            continue

        # MISSING_JURISDICTION (paper reports only)
        if project.report_type == "paper":
            if not employee.jurisdiction:
                flags.append(_make_flag(
                    "MISSING_JURISDICTION", "compliance", "error",
                    emp_id, emp_name,
                    f"{emp_name} has no jurisdiction set (required for paper CPR on "
                    f"{project.project_title}).",
                    first_work_date=fwd, **proj_kwargs,
                ))

        # BELOW_PREVAILING_WAGE
        if (emp_id, proj_id) in built_keys:
            row = next(
                r for r in rows if r.employee.id == emp_id and r.project.id == proj_id
            )
            if row.hours_on_project > ZERO and row.prorated_gross > ZERO:
                effective_rate = (row.prorated_gross / row.hours_on_project).quantize(
                    Decimal("0.01")
                )
                if effective_rate < rate.hourly_rate:
                    flags.append(_make_flag(
                        "BELOW_PREVAILING_WAGE", "compliance", "error",
                        emp_id, emp_name,
                        f"{emp_name}'s effective rate (${effective_rate}/hr) is below the "
                        f"prevailing wage rate (${rate.hourly_rate}/hr) for "
                        f"{classification.name}.",
                        first_work_date=fwd, **proj_kwargs,
                    ))

    return flags


def _check_discrepancies(report_data, ot_summaries, rows, first_work_dates) -> list[dict]:
    """Check for hours mismatches between timesheet OT calculations and payroll
    detail. Per-employee totals span ALL buckets (enrolled + non-CP), matching
    the whole-week Gusto figures."""
    flags: list[dict] = []

    # Aggregate OT summaries per employee (across all buckets)
    emp_totals: dict[str, dict[str, Decimal]] = {}
    for s in ot_summaries:
        if s.employee_id not in emp_totals:
            emp_totals[s.employee_id] = {"st": ZERO, "ot": ZERO, "dt": ZERO, "total": ZERO}
        t = emp_totals[s.employee_id]
        t["st"] += s.total_st
        t["ot"] += s.total_ot
        t["dt"] += s.total_dt
        t["total"] += s.total_hours

    # Index detail entries by employee_id
    detail_by_emp = {}
    for d in report_data.detail_entries:
        if d.employee_id:
            detail_by_emp[d.employee_id] = d

    for emp_id, totals in emp_totals.items():
        employee = report_data.employees.get(emp_id)
        if not employee:
            continue

        detail = detail_by_emp.get(emp_id)
        if not detail:
            continue

        emp_name = employee.full_name
        fwd = first_work_dates.get(emp_id)

        # HOURS_MISMATCH — total hours
        detail_total = detail.hours_total or ZERO
        calc_total = totals["total"]
        if abs(detail_total - calc_total) > TOLERANCE:
            flags.append(_make_flag(
                "HOURS_MISMATCH", "discrepancy", "error",
                emp_id, emp_name,
                f"{emp_name}: Timesheet total ({calc_total}h) differs from payroll detail "
                f"total ({detail_total}h).",
                first_work_date=fwd,
            ))

        # OT_HOURS_MISMATCH
        detail_ot = (detail.hours_ot or ZERO) + (detail.hours_overtime_pay or ZERO)
        calc_ot = totals["ot"] + totals["dt"]
        if abs(detail_ot - calc_ot) > TOLERANCE:
            flags.append(_make_flag(
                "OT_HOURS_MISMATCH", "discrepancy", "error",
                emp_id, emp_name,
                f"{emp_name}: Calculated OT/DT ({calc_ot}h) differs from payroll detail "
                f"OT ({detail_ot}h).",
                first_work_date=fwd,
            ))

        # REGULAR_HOURS_MISMATCH — the timesheet allocates every worked hour to
        # ST/OT/DT with no pay-type buckets, so calc_reg (straight time) must be
        # compared against ALL of Gusto's non-overtime hour buckets, not just
        # "regular": grave-shift/holiday/foreman/GF/salary hours are straight
        # time too. Summing only regular+regular_pay flags a mismatch for every
        # employee who has any premium-bucket hours.
        detail_reg = (
            (detail.hours_regular or ZERO)
            + (detail.hours_regular_pay or ZERO)
            + (detail.hours_grave_shift or ZERO)
            + (detail.hours_holiday or ZERO)
            + (detail.hours_holiday_pay or ZERO)
            + (detail.hours_foreman or ZERO)
            + (detail.hours_gf or ZERO)
            + (detail.hours_sal or ZERO)
            + (detail.hours_salary or ZERO)
        )
        calc_reg = totals["st"]
        if abs(detail_reg - calc_reg) > TOLERANCE:
            flags.append(_make_flag(
                "REGULAR_HOURS_MISMATCH", "discrepancy", "error",
                emp_id, emp_name,
                f"{emp_name}: Calculated regular hours ({calc_reg}h) differs from payroll "
                f"detail ({detail_reg}h).",
                first_work_date=fwd,
            ))

    return flags


def _check_overtime(report_data, ot_summaries, first_work_dates) -> list[dict]:
    """Check for overtime and double-time occurrences, and early-start OT.
    Per-employee OT/DT totals span all buckets (the full-day allocation);
    early-start checks apply to enrolled projects only — they are the only
    buckets that carry a shift_start_time."""
    flags: list[dict] = []
    seen_ot = set()
    seen_dt = set()

    for summary in ot_summaries:
        emp_id = summary.employee_id
        employee = report_data.employees.get(emp_id)
        if not employee:
            continue

        emp_name = employee.full_name
        fwd = first_work_dates.get(emp_id)

        # OVERTIME_DETECTED (once per employee)
        if summary.total_ot > ZERO and emp_id not in seen_ot:
            seen_ot.add(emp_id)
            total_ot_emp = sum(
                s.total_ot for s in ot_summaries if s.employee_id == emp_id
            )
            flags.append(_make_flag(
                "OVERTIME_DETECTED", "overtime", "warning",
                emp_id, emp_name,
                f"{emp_name} has {total_ot_emp}h of overtime this week.",
                first_work_date=fwd,
            ))

        # DOUBLE_TIME_DETECTED (once per employee)
        if summary.total_dt > ZERO and emp_id not in seen_dt:
            seen_dt.add(emp_id)
            total_dt_emp = sum(
                s.total_dt for s in ot_summaries if s.employee_id == emp_id
            )
            flags.append(_make_flag(
                "DOUBLE_TIME_DETECTED", "overtime", "warning",
                emp_id, emp_name,
                f"{emp_name} has {total_dt_emp}h of double time this week (>12h day).",
                first_work_date=fwd,
            ))

    # EARLY_START_OT — check individual time entries (enrolled projects only:
    # they are the only buckets that carry a shift_start_time)
    seen_early = set()
    for entry in report_data.time_entries:
        if not entry.employee_id or not entry.project_id:
            continue
        emp_id = entry.employee_id
        if emp_id in seen_early:
            continue

        project = report_data.projects.get(entry.project_id)
        if not project or not project.shift_start_time:
            continue

        shift_type = project.shift_type or "regular"
        te_input = TimeEntryInput(
            employee_id=emp_id,
            project_id=entry.project_id,
            work_date=entry.work_date,
            start_time=entry.start_time,
            total_hours=entry.total_hours,
            shift_type=shift_type,
            shift_start_time=project.shift_start_time,
        )
        early_ot = _calculate_early_start_ot(te_input)
        if early_ot > ZERO:
            seen_early.add(emp_id)
            employee = report_data.employees.get(emp_id)
            if employee:
                flags.append(_make_flag(
                    "EARLY_START_OT", "overtime", "warning",
                    emp_id, employee.full_name,
                    f"{employee.full_name} started >3h before shift start time "
                    f"({project.shift_start_time.strftime('%H:%M')}) on "
                    f"{entry.work_date.isoformat()}, triggering early-start OT.",
                    first_work_date=first_work_dates.get(emp_id),
                    project_id=entry.project_id,
                    project_number=project.project_number,
                    project_title=project.project_title,
                ))

    return flags


def _check_shift_patterns(report_data, ot_summaries, first_work_dates) -> list[dict]:
    """Check for shift pattern mismatches (e.g., four_tens project but 5-day
    schedule) — enrolled CP projects only."""
    flags: list[dict] = []

    # Group time entries by (employee, project) and count distinct work days
    emp_proj_days: dict[tuple[str, str], set] = {}
    emp_proj_hours: dict[tuple[str, str], Decimal] = {}
    for entry in report_data.time_entries:
        if not entry.employee_id or not entry.project_id:
            continue
        key = (entry.employee_id, entry.project_id)
        if key not in emp_proj_days:
            emp_proj_days[key] = set()
            emp_proj_hours[key] = ZERO
        emp_proj_days[key].add(entry.work_date)
        emp_proj_hours[key] += entry.total_hours

    seen = set()
    for (emp_id, proj_id), days in emp_proj_days.items():
        if emp_id in seen:
            continue

        project = report_data.projects.get(proj_id)
        if not project:
            continue

        if project.shift_type == "four_tens":
            num_days = len(days)
            total_hrs = emp_proj_hours[(emp_id, proj_id)]
            avg_daily = total_hrs / num_days if num_days > 0 else ZERO

            # Flag if worked 5+ days with avg < 9h/day (not consistent with 4x10 pattern)
            if num_days >= 5 and avg_daily < Decimal("9"):
                seen.add(emp_id)
                employee = report_data.employees.get(emp_id)
                if employee:
                    flags.append(_make_flag(
                        "SHIFT_PATTERN_MISMATCH", "shift", "warning",
                        emp_id, employee.full_name,
                        f"{employee.full_name} worked {num_days} days (avg {avg_daily:.1f}h/day) "
                        f"on 4x10 project {project.project_title}.",
                        first_work_date=first_work_dates.get(emp_id),
                        project_id=proj_id,
                        project_number=project.project_number,
                        project_title=project.project_title,
                    ))

    return flags


def _check_non_cp(report_data, ot_summaries, bucket_sources, first_work_dates) -> list[dict]:
    """Surface the non-CP buckets of the merged model.

    NON_CP_HOURS (info) for known non-CP work (bdr:/ext: buckets): the hours
    counted for OT allocation and pay proration but are excluded from certified
    reports. UNKNOWN_PROJECT (error) for raw: buckets — nothing matched.
    One flag per (employee, bucket).
    """
    flags: list[dict] = []

    for summary in ot_summaries:
        key = summary.project_id
        if key in report_data.projects:
            continue

        hours = summary.total_hours
        if hours <= ZERO:
            continue

        employee = report_data.employees.get(summary.employee_id)
        if not employee:
            continue

        emp_name = employee.full_name
        fwd = first_work_dates.get(summary.employee_id)
        source = bucket_sources.get(key, key)

        if key.startswith("raw:"):
            flags.append(_make_flag(
                "UNKNOWN_PROJECT", "compliance", "error",
                summary.employee_id, emp_name,
                f"{emp_name} has {hours}h on unknown project '{source}'. Match it to a "
                "BDR project or add it to the non-payroll registry.",
                first_work_date=fwd,
                project_title=source,
                hours=str(hours),
            ))
        else:
            flags.append(_make_flag(
                "NON_CP_HOURS", "non_cp", "info",
                summary.employee_id, emp_name,
                f"{emp_name} has {hours}h on {source} — counted for overtime and pay "
                "proration, excluded from certified reports.",
                first_work_date=fwd,
                project_title=source,
                hours=str(hours),
            ))

    return flags
