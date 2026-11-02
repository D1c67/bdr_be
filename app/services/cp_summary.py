"""Certified payroll week summary (docs/ASSISTANT_PHASE4_PLAN.md 3.13).

STUB created by WI-1 (Wave 0). WI-7 fills it (with
`GET /payroll/reports/summary` in `app/routers/payroll_reports.py`); nobody
else edits it. The plan names the module and the body but no function, so
WI-1 fixed the name below; WI-7 owns both caller and callee.

Never in the output: names, wage fields, SSN, rates, storage paths,
filenames, user ids. Per-project cells with fewer than CP_SMALL_CELL_MIN
distinct employees merge into one "Other enrolled projects" row; when that
row itself is below the minimum its hours join `other_hours`, so
`total_hours - sum(shown project hours) - other_hours == 0`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.services import payroll_matching, payroll_reports

# The smallest employee count a per-project cell may show (3.13; the
# ASSISTANT_CP_SMALL_CELL_MIN setting carries the same default).
CP_SMALL_CELL_MIN = 3


# Page size for the time-entry read (PostgREST caps one response).
_PAGE = 1000
_MAX_PAGES = 100
_OTHER_ENROLLED = "Other enrolled projects"

_ENTRY_COLUMNS = (
    "id, employee_id, project_id, total_hours, is_employee_matched, "
    "raw_employee_first_name, raw_employee_last_name, raw_project_number, raw_project_name"
)


def _hours(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _entries(sb: Any, report_id: str) -> list[dict]:
    out: list[dict] = []
    for page in range(_MAX_PAGES):
        rows = (
            sb.table("cp_time_entries")
            .select(_ENTRY_COLUMNS)
            .eq("payroll_report_id", report_id)
            .order("id")
            .range(page * _PAGE, page * _PAGE + _PAGE - 1)
            .execute()
        ).data or []
        out.extend(rows)
        if len(rows) < _PAGE:
            break
    return out


def _person_key(entry: dict) -> str:
    """Who an entry belongs to, for counting only. Never leaves this module."""
    if entry.get("employee_id"):
        return f"e:{entry['employee_id']}"
    first = (entry.get("raw_employee_first_name") or "").strip().lower()
    last = (entry.get("raw_employee_last_name") or "").strip().lower()
    return f"r:{first} {last}"


def _by_project(sb: Any, entries: list[dict]) -> tuple[dict[str, dict], Decimal]:
    """Hours and distinct people per enrolled project, plus every hour that is
    not on an enrolled project (non-CP jobs, registry jobs, unknown jobs)."""
    project_ids = sorted({e["project_id"] for e in entries if e.get("project_id")})
    projects: dict[str, dict] = {}
    for i in range(0, len(project_ids), 100):
        rows = (
            sb.table("projects")
            .select("id, number, name, cp_enrolled_at")
            .in_("id", project_ids[i : i + 100])
            .execute()
        ).data or []
        projects.update({p["id"]: p for p in rows})
    cells: dict[str, dict] = {}
    not_shown = Decimal("0")
    for e in entries:
        hours = _hours(e.get("total_hours"))
        proj = projects.get(e.get("project_id"))
        if proj is None or not proj.get("cp_enrolled_at"):
            not_shown += hours
            continue
        cell = cells.setdefault(
            proj["id"],
            {"number": proj.get("number"), "name": proj.get("name"), "hours": Decimal("0"), "people": set()},
        )
        cell["hours"] += hours
        cell["people"].add(_person_key(e))
    return cells, not_shown


def suppress_small_cells(cells: dict[str, dict], other_hours: Decimal) -> tuple[list[dict], Decimal]:
    """Apply the small-cell rule. Cells with fewer than CP_SMALL_CELL_MIN
    distinct people merge into one "Other enrolled projects" row; when that
    row is itself below the minimum, its hours join `other_hours` and it is
    not shown. Returns (rows shown, other_hours)."""
    shown: list[dict] = []
    merged_hours = Decimal("0")
    merged_people: set[str] = set()
    merged_any = False
    for cell in cells.values():
        if len(cell["people"]) >= CP_SMALL_CELL_MIN:
            shown.append(
                {
                    "number": cell["number"],
                    "name": cell["name"],
                    "hours": _fmt(cell["hours"]),
                    "employees": len(cell["people"]),
                }
            )
        else:
            merged_any = True
            merged_hours += cell["hours"]
            merged_people |= cell["people"]
    shown.sort(key=lambda r: (str(r["number"] or ""), str(r["name"] or "")))
    if merged_any:
        if len(merged_people) >= CP_SMALL_CELL_MIN:
            shown.append(
                {
                    "number": None,
                    "name": _OTHER_ENROLLED,
                    "hours": _fmt(merged_hours),
                    "employees": len(merged_people),
                }
            )
        else:
            other_hours += merged_hours
    return shown, other_hours


def week_summary(sb: Any, week_start: date) -> dict | None:
    """Body of `GET /payroll/reports/summary?week_start=`: `{id, week_start,
    week_end, status, timesheet_uploaded, payroll_detail_uploaded,
    total_hours, total_employees, entry_count, unmatched_employee_entries,
    unmatched_project_entries, finalize_issues, stale_reasons, finalized_at,
    submitted_at, record_count, latest_revision, hours_by_project: [{number,
    name, hours, employees}], other_hours}` built with
    `payroll_reports._get_by_week`, `finalize_gate_issues` and
    `check_stale_since_finalization`. None when the week has no report (the
    route answers 404 `no_report`). Hours are 2 dp strings, so
    `total_hours - sum(shown hours) - other_hours` is exactly 0."""
    found = payroll_reports._get_by_week(week_start)
    if not found:
        return None
    report = payroll_reports.require_cp_report(found["id"])
    entries = _entries(sb, report["id"])

    cells, not_shown = _by_project(sb, entries)
    rows, other_hours = suppress_small_cells(cells, not_shown)
    total_hours = sum((_hours(e.get("total_hours")) for e in entries), Decimal("0"))

    ignored = payroll_matching.load_ignored_projects()
    unknown_projects = sum(
        1
        for e in entries
        if e.get("project_id") is None
        and payroll_matching.match_ignored(
            e.get("raw_project_number"), e.get("raw_project_name"), ignored
        )
        is None
    )
    records = (
        sb.table("cp_records")
        .select("revision_number")
        .eq("payroll_report_id", report["id"])
        .execute()
    ).data or []
    revisions = [r["revision_number"] for r in records if r.get("revision_number") is not None]

    stored_employees = report.get("total_employees")
    return {
        "id": report["id"],
        "week_start": report["week_start_date"],
        "week_end": report["week_end_date"],
        "status": report["status"],
        "timesheet_uploaded": bool(report.get("timesheet_filename")),
        "payroll_detail_uploaded": bool(report.get("payroll_detail_filename")),
        "total_hours": _fmt(total_hours),
        "total_employees": (
            stored_employees
            if stored_employees is not None
            else len({_person_key(e) for e in entries})
        ),
        "entry_count": len(entries),
        "unmatched_employee_entries": sum(1 for e in entries if not e.get("is_employee_matched")),
        "unmatched_project_entries": unknown_projects,
        "finalize_issues": (
            payroll_reports.finalize_gate_issues(report, entries)
            if report["status"] == "processing"
            else []
        ),
        "stale_reasons": (
            payroll_reports.check_stale_since_finalization(report, entries)
            if report.get("finalized_at")
            else []
        ),
        "finalized_at": report.get("finalized_at"),
        "submitted_at": report.get("submitted_at"),
        "record_count": len(records),
        "latest_revision": max(revisions) if revisions else None,
        "hours_by_project": rows,
        "other_hours": _fmt(other_hours),
    }
