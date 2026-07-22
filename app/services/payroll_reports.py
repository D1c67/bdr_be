"""Certified Payroll — the weekly report pipeline service.

One company-wide report per payroll week (Sun–Sat), enforced by a unique index
on week_start_date (0064). The lifecycle is a status machine: draft →
awaiting_payroll_detail / awaiting_timesheet (one file in) → processing (both
in) → processed (finalized, all gates green) → submitted. Raw uploads land in
storage first, then an optimistic-locked claim on the report row (conditioned
on updated_at) serializes concurrent editors before the parse/replace pass.

The parser modules (payroll_timesheet_parser / payroll_detail_parser) and the
OT helper are imported lazily: they drag pandas in, and nothing else here needs
it. Only unknown projects (project_id NULL and no cp_ignored_projects hit) block
finalize — non-enrolled and registry hours still count toward OT allocation and
pay proration, they just never produce certified report rows.
"""

import importlib
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException, status

from app.core.supabase_client import get_supabase
from app.services import payroll_matching, storage

_WEEK_TAKEN = "A report for that week already exists"

_UPLOAD_MIME_BY_EXT = {
    ".csv": "text/csv",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# cp_payroll_detail_entries numeric columns, 1:1 with the parser's dataclass
# attributes (0064 copied them verbatim from the legacy model).
_DETAIL_NUMERIC_FIELDS = (
    "hours_total",
    "hours_regular",
    "hours_grave_shift",
    "hours_ot",
    "hours_holiday",
    "hours_foreman",
    "hours_gf",
    "hours_sal",
    "hours_regular_pay",
    "hours_overtime_pay",
    "hours_salary",
    "hours_holiday_pay",
    "gross_pay_total",
    "gross_pay_regular",
    "gross_pay_grave_shift",
    "gross_pay_ot",
    "gross_pay_holiday",
    "gross_pay_foreman",
    "gross_pay_reimb",
    "gross_pay_gf",
    "gross_pay_sal",
    "gross_pay_regular_pay",
    "gross_pay_overtime_pay",
    "gross_pay_reimbursement",
    "gross_pay_salary",
    "gross_pay_holiday_pay",
    "pretax_deductions_total",
    "pretax_401k",
    "pretax_401k_catchup",
    "adjusted_gross",
    "other_pay_total",
    "other_pay_qot",
    "employee_taxes_total",
    "employee_taxes_fit",
    "employee_taxes_ss",
    "employee_taxes_med",
    "aftertax_deductions_total",
    "aftertax_working_dues",
    "aftertax_roth_401k",
    "net_pay",
    "employer_taxes_contributions_total",
    "employer_taxes_total",
    "employer_taxes_futa",
    "employer_taxes_ss",
    "employer_taxes_med",
    "employer_taxes_sui",
    "employer_taxes_cep",
    "company_contributions_total",
    "company_contributions_pension",
    "company_contributions_401k",
    "company_contributions_401k_catchup",
    "company_contributions_dental_vision",
    "total_payroll_cost",
)


def _iso(value) -> str | None:
    """date/datetime → ISO string for PostgREST; strings pass through."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else value


def _num(value) -> str | None:
    """Numeric → str(Decimal) for PostgREST; None stays None."""
    if value is None:
        return None
    return str(Decimal(str(value)))


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _upload_mime(filename: str | None) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return _UPLOAD_MIME_BY_EXT.get(ext, "application/octet-stream")


def _entry_phrase(n: int) -> str:
    return f"{n} time entr{'y' if n == 1 else 'ies'}"


def _report_entries(report_id: str) -> list[dict]:
    return (
        get_supabase().table("cp_time_entries").select("*").eq("payroll_report_id", report_id)
    ).execute().data or []


# ── Week math and lookups (get_week_dates/require_cp_report are pinned) ────────


def get_week_dates(d: date) -> tuple[date, date]:
    """Snap any date to its payroll week: (Sunday, Saturday)."""
    days_since_sunday = (d.weekday() + 1) % 7
    week_start = d - timedelta(days=days_since_sunday)
    return week_start, week_start + timedelta(days=6)


def require_cp_report(report_id: str) -> dict:
    rows = (
        get_supabase().table("cp_payroll_reports").select("*").eq("id", report_id).limit(1)
    ).execute().data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payroll report not found")
    return rows[0]


# ── Create ─────────────────────────────────────────────────────────────────────


def _get_by_week(week_start: date) -> dict | None:
    rows = (
        get_supabase()
        .table("cp_payroll_reports")
        .select("id")
        .eq("week_start_date", week_start.isoformat())
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _is_duplicate_week(exc: Exception) -> bool:
    msg = str(exc)
    return "cp_payroll_reports_week_unique_idx" in msg or "23505" in msg


def create_report(week_date: date, actor_id: str) -> dict:
    """Create the report for whichever week `week_date` falls in. The unique
    week index backs up the pre-check; a lost race still yields the same 409
    shape (the FE links to existing_id either way)."""
    week_start, week_end = get_week_dates(week_date)
    existing = _get_by_week(week_start)
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": _WEEK_TAKEN, "existing_id": existing["id"]},
        )
    try:
        return (
            get_supabase()
            .table("cp_payroll_reports")
            .insert(
                {
                    "week_start_date": week_start.isoformat(),
                    "week_end_date": week_end.isoformat(),
                    "status": "draft",
                    "created_by": actor_id,
                }
            )
            .execute()
        ).data[0]
    except Exception as exc:  # noqa: BLE001 — unique violation → week already taken
        if _is_duplicate_week(exc):
            existing = _get_by_week(week_start)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "message": _WEEK_TAKEN,
                    "existing_id": existing["id"] if existing else None,
                },
            ) from exc
        raise


# ── Uploads (timesheet / payroll detail) ───────────────────────────────────────


def _current_filenames(report_id: str) -> dict:
    """The report's two upload-filename columns as currently persisted — used to
    derive status from live state rather than a claim-time snapshot."""
    rows = (
        get_supabase()
        .table("cp_payroll_reports")
        .select("timesheet_filename, payroll_detail_filename")
        .eq("id", report_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else {}


def _claim_upload(report: dict, patch: dict, upload_path: str) -> dict:
    """Optimistic lock: condition the claim on the updated_at the caller read.
    Zero rows = someone else edited the report since — drop the just-stored
    object (best-effort) and surface the FE's conflict sentinel."""
    claimed = (
        get_supabase()
        .table("cp_payroll_reports")
        .update(patch)
        .eq("id", report["id"])
        .eq("updated_at", report["updated_at"])
        .execute()
    ).data
    if not claimed:
        try:
            storage.delete_file(upload_path)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status.HTTP_409_CONFLICT, "cp_report_conflict")
    return claimed[0]


def process_timesheet(report: dict, filename: str, content: bytes, actor_id: str) -> dict:
    """Parse first, then store the raw upload, claim, and replace the report's
    time entries: parse → quarter-hour round → match → insert → totals + status.

    Parsing happens BEFORE anything is persisted: a malformed upload must not
    leave the report stamped with timesheet_filename while zero cp_time_entries
    were written — that combination would slip an empty report past the finalize
    gate and be filed as a certified report."""
    report_id = report["id"]
    parser = importlib.import_module("app.services.payroll_timesheet_parser")
    try:
        parsed = parser.parse_timesheet(content, filename)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    path = storage.build_object_path("payroll", f"reports/{report_id}/uploads", filename)
    storage.upload_file(path, content, _upload_mime(filename))
    _claim_upload(
        report, {"timesheet_filename": filename, "timesheet_storage_path": path}, path
    )

    round_quarter_hour = importlib.import_module("app.services.payroll_ot").round_quarter_hour

    sb = get_supabase()
    sb.table("cp_time_entries").delete().eq("payroll_report_id", report_id).execute()

    entries: list[dict] = []
    total_hours = Decimal("0.00")
    for p in parsed:
        hours = round_quarter_hour(Decimal(str(p.total_hours)))
        total_hours += hours
        entries.append(
            {
                "payroll_report_id": report_id,
                "raw_employee_first_name": p.first_name,
                "raw_employee_last_name": p.last_name,
                "raw_project_number": p.project_number,
                "raw_project_name": p.project_name,
                "work_date": _iso(p.work_date),
                "start_time": _iso(p.start_time),
                "end_time": _iso(p.end_time),
                "break_duration_minutes": getattr(p, "break_total_minutes", 0) or 0,
                "total_hours": str(hours),
                "customer": p.customer,
                "cost_code": p.cost_code,
                "cost_code_desc": p.cost_code_desc,
                "description": p.description,
                "subproject_1_number": p.subproject_1_number,
                "subproject_1_name": p.subproject_1_name,
            }
        )
    summary = payroll_matching.match_entries(
        entries,
        payroll_matching.load_employees(),
        payroll_matching.load_projects(),
        payroll_matching.load_ignored_projects(),
    )
    if entries:
        sb.table("cp_time_entries").insert(entries).execute()

    # Derive status from the report's CURRENT persisted filenames, not the
    # claim-time snapshot: a concurrent payroll-detail upload may have set
    # payroll_detail_filename between our claim and here, and writing a stale
    # "awaiting_payroll_detail" would clobber its "processing".
    new_status = (
        "processing" if _current_filenames(report_id).get("payroll_detail_filename")
        else "awaiting_payroll_detail"
    )
    sb.table("cp_payroll_reports").update(
        {
            "status": new_status,
            "total_hours": str(total_hours),
            "total_employees": len(summary["unique_employee_ids"]),
        }
    ).eq("id", report_id).execute()

    return {
        "payroll_report_id": report_id,
        "status": new_status,
        "total_entries": len(entries),
        "matched_employees": summary["matched_employees"],
        "unmatched_employees": summary["unmatched_employees"],
        "matched_projects": summary["matched_projects"],
        "unmatched_projects": summary["unmatched_projects"],
        "message": f"Processed {len(entries)} time entries from {filename}",
    }


def process_payroll_detail(report: dict, filename: str, content: bytes, actor_id: str) -> dict:
    """The Gusto pay-period file: same store→claim→parse→replace shape as the
    timesheet, but rows are per-employee money/hour buckets, matched by the
    "LASTNAME, FIRSTNAME M" name format."""
    report_id = report["id"]
    parser = importlib.import_module("app.services.payroll_detail_parser")
    try:
        parsed = parser.parse_payroll_detail(content, filename)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    path = storage.build_object_path("payroll", f"reports/{report_id}/uploads", filename)
    storage.upload_file(path, content, _upload_mime(filename))
    _claim_upload(
        report,
        {"payroll_detail_filename": filename, "payroll_detail_storage_path": path},
        path,
    )

    sb = get_supabase()
    sb.table("cp_payroll_detail_entries").delete().eq("payroll_report_id", report_id).execute()

    employees = payroll_matching.load_employees()
    matched: list[str] = []
    unmatched: list[str] = []
    rows: list[dict] = []
    for p in parsed:
        employee = payroll_matching.match_detail_employee(p.employee_name, employees)
        (matched if employee else unmatched).append(p.employee_name)
        row = {
            "payroll_report_id": report_id,
            "employee_name": p.employee_name,
            "employee_id": employee["id"] if employee else None,
            "is_employee_matched": employee is not None,
            "pay_date": _iso(getattr(p, "pay_date", None)),
            "time_period": getattr(p, "time_period", None),
        }
        for field in _DETAIL_NUMERIC_FIELDS:
            row[field] = _num(getattr(p, field, None))
        rows.append(row)
    if rows:
        sb.table("cp_payroll_detail_entries").insert(rows).execute()

    # Fresh-read the report's filenames (see process_timesheet): don't let a
    # stale "awaiting_timesheet" clobber a concurrent timesheet upload.
    new_status = (
        "processing" if _current_filenames(report_id).get("timesheet_filename")
        else "awaiting_timesheet"
    )
    sb.table("cp_payroll_reports").update({"status": new_status}).eq("id", report_id).execute()

    return {
        "payroll_report_id": report_id,
        "status": new_status,
        "payroll_detail_filename": filename,
        "total_entries": len(rows),
        "matched_employees": sorted(matched),
        "unmatched_employees": sorted(unmatched),
        "message": f"Processed {len(rows)} detail entries from {filename}",
    }


# ── Finalization gates (both pinned) ───────────────────────────────────────────


def check_stale_since_finalization(report: dict, entries: list[dict] | None = None) -> list[str]:
    """Reference data edited after finalized_at makes the finalized numbers
    stale: employees, their classifications, those classifications' rates, and
    the referenced projects (spine row or cp_details). Empty list = fresh."""
    finalized = _parse_ts(report.get("finalized_at"))
    if finalized is None:
        return []
    sb = get_supabase()
    if entries is None:
        entries = (
            sb.table("cp_time_entries")
            .select("employee_id, project_id")
            .eq("payroll_report_id", report["id"])
            .execute()
        ).data or []
    employee_ids = sorted({e["employee_id"] for e in entries if e.get("employee_id")})
    project_ids = sorted({e["project_id"] for e in entries if e.get("project_id")})

    def _any_stale(rows: list[dict]) -> bool:
        return any(
            ts is not None and ts > finalized
            for ts in (_parse_ts(r.get("updated_at")) for r in rows)
        )

    reasons: list[str] = []
    classification_ids: list[str] = []
    if employee_ids:
        emps = (
            sb.table("employees")
            .select("id, classification_id, updated_at")
            .in_("id", employee_ids)
            .execute()
        ).data or []
        if _any_stale(emps):
            reasons.append("employees")
        classification_ids = sorted({e["classification_id"] for e in emps if e.get("classification_id")})
    if classification_ids:
        cls = (
            sb.table("cp_classifications")
            .select("id, updated_at")
            .in_("id", classification_ids)
            .execute()
        ).data or []
        if _any_stale(cls):
            reasons.append("classifications")
        rates = (
            sb.table("cp_rates")
            .select("classification_id, updated_at")
            .in_("classification_id", classification_ids)
            .execute()
        ).data or []
        if _any_stale(rates):
            reasons.append("rates")
    if project_ids:
        projs = (
            sb.table("projects").select("id, updated_at").in_("id", project_ids).execute()
        ).data or []
        details = (
            sb.table("cp_details")
            .select("project_id, updated_at")
            .in_("project_id", project_ids)
            .execute()
        ).data or []
        if _any_stale(projs) or _any_stale(details):
            reasons.append("projects")
    return reasons


def finalize_gate_issues(report: dict, entries: list[dict] | None = None) -> list[str]:
    """Hard gates: both files uploaded, every entry's employee matched, and no
    unknown projects (project_id NULL with no registry hit). Non-enrolled and
    registry hours never block — they just stay off the certified reports."""
    issues: list[str] = []
    if not report.get("timesheet_filename"):
        issues.append("Timesheet has not been uploaded")
    if not report.get("payroll_detail_filename"):
        issues.append("Payroll detail has not been uploaded")
    if entries is None:
        entries = _report_entries(report["id"])
    # A report with both files stamped but zero time entries (e.g. a timesheet
    # that parsed to nothing) must never be finalizable — otherwise an empty
    # report clears every other gate (0 unmatched, 0 unknown) and can be filed.
    if not entries:
        issues.append("No time entries have been uploaded")
    unmatched_employees = sum(1 for e in entries if not e.get("is_employee_matched"))
    if unmatched_employees:
        issues.append(f"{_entry_phrase(unmatched_employees)} with unmatched employees")
    ignored = payroll_matching.load_ignored_projects()
    unknown = sum(
        1
        for e in entries
        if e.get("project_id") is None
        and payroll_matching.match_ignored(
            e.get("raw_project_number"), e.get("raw_project_name"), ignored
        )
        is None
    )
    if unknown:
        issues.append(f"{_entry_phrase(unknown)} with unknown projects")
    return issues


# ── Status transitions ─────────────────────────────────────────────────────────


def finalize_report(report: dict, actor_id: str) -> dict:
    if report["status"] != "processing":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a report in processing can be finalized"
        )
    issues = finalize_gate_issues(report)
    if issues:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"message": "Report is not ready to finalize", "issues": issues},
        )
    # Conditioned on status: doubles as the race guard against a concurrent
    # finalize/upload flipping the report out of processing.
    updated = (
        get_supabase()
        .table("cp_payroll_reports")
        .update(
            {
                "status": "processed",
                "finalized_at": datetime.now(UTC).isoformat(),
                "finalized_by": actor_id,
            }
        )
        .eq("id", report["id"])
        .eq("status", "processing")
        .execute()
    ).data
    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Report changed while finalizing — reload and retry"
        )
    return updated[0]


def submit_report(report: dict, actor_id: str) -> dict:
    if report["status"] != "processed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a finalized (processed) report can be submitted"
        )
    updated = (
        get_supabase()
        .table("cp_payroll_reports")
        .update(
            {
                "status": "submitted",
                "submitted_at": datetime.now(UTC).isoformat(),
                "submitted_by": actor_id,
            }
        )
        .eq("id", report["id"])
        .eq("status", "processed")
        .execute()
    ).data
    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Report changed while submitting — reload and retry"
        )
    return updated[0]


def delete_report(report: dict, actor_id: str) -> None:
    """Delete the report row (children cascade in the DB) and best-effort sweep
    every storage object it knows about — the raw uploads and generated CPR
    files. A missing object never blocks the delete.

    A submitted report is a filed prevailing-wage record the company is legally
    required to retain, so deletion honors the same submitted-immutability
    invariant every other whole-report mutation enforces (see rematch). Enforced
    here — not just in the router — so no caller can bypass it."""
    if report["status"] == "submitted":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Report has been submitted — it can no longer be deleted",
        )
    sb = get_supabase()
    report_id = report["id"]
    paths = [
        p
        for p in (report.get("timesheet_storage_path"), report.get("payroll_detail_storage_path"))
        if p
    ]
    records = (
        sb.table("cp_records").select("id").eq("payroll_report_id", report_id).execute()
    ).data or []
    record_ids = [r["id"] for r in records]
    if record_ids:
        files = (
            sb.table("cp_record_files").select("storage_path").in_("record_id", record_ids)
        ).execute().data or []
        paths += [f["storage_path"] for f in files if f.get("storage_path")]
    sb.table("cp_payroll_reports").delete().eq("id", report_id).execute()
    for path in paths:
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001
            pass


# ── The GET /{id} payload (the FE contract) ────────────────────────────────────


def build_report_detail(report: dict) -> dict:
    """All report columns plus the joined/derived views the FE renders. Every
    lookup is batched with .in_() — never per-entry queries."""
    sb = get_supabase()
    report_id = report["id"]
    entries = (
        sb.table("cp_time_entries")
        .select("*")
        .eq("payroll_report_id", report_id)
        .order("work_date")
        .order("start_time")
        .execute()
    ).data or []
    detail_entries = (
        sb.table("cp_payroll_detail_entries")
        .select("*")
        .eq("payroll_report_id", report_id)
        .order("employee_name")
        .execute()
    ).data or []

    employee_ids = sorted({e["employee_id"] for e in entries if e.get("employee_id")})
    project_ids = sorted({e["project_id"] for e in entries if e.get("project_id")})

    employees_by_id: dict[str, dict] = {}
    classifications_by_id: dict[str, dict] = {}
    if employee_ids:
        emps = (
            sb.table("employees")
            .select("id, first_name, last_name, classification_id")
            .in_("id", employee_ids)
            .execute()
        ).data or []
        employees_by_id = {e["id"]: e for e in emps}
        classification_ids = sorted(
            {e["classification_id"] for e in emps if e.get("classification_id")}
        )
        if classification_ids:
            cls = (
                sb.table("cp_classifications")
                .select("id, code, name, is_field")
                .in_("id", classification_ids)
                .execute()
            ).data or []
            classifications_by_id = {c["id"]: c for c in cls}
    projects_by_id: dict[str, dict] = {}
    if project_ids:
        projs = (
            sb.table("projects")
            .select("id, name, number, cp_enrolled_at")
            .in_("id", project_ids)
            .execute()
        ).data or []
        projects_by_id = {p["id"]: p for p in projs}

    ignored = payroll_matching.load_ignored_projects()

    time_entries: list[dict] = []
    unmatched_emp_counts: dict[tuple, int] = {}
    unmatched_proj_counts: dict[tuple, int] = {}
    non_cp: dict[str, dict] = {}
    for e in entries:
        emp = employees_by_id.get(e.get("employee_id"))
        cls = classifications_by_id.get(emp.get("classification_id")) if emp else None
        proj = projects_by_id.get(e.get("project_id"))
        enrolled = bool(proj and proj.get("cp_enrolled_at"))
        enriched = dict(e)
        enriched["employee_name"] = f"{emp['first_name']} {emp['last_name']}" if emp else None
        enriched["classification_code"] = cls["code"] if cls else None
        enriched["classification_name"] = cls["name"] if cls else None
        enriched["is_field"] = cls["is_field"] if cls else None
        enriched["project_number"] = proj["number"] if proj else None
        enriched["project_name"] = proj["name"] if proj else None
        enriched["cp_enrolled"] = enrolled
        time_entries.append(enriched)

        raw_first = e.get("raw_employee_first_name")
        raw_last = e.get("raw_employee_last_name")
        if not e.get("is_employee_matched"):
            unmatched_emp_counts[(raw_first, raw_last)] = (
                unmatched_emp_counts.get((raw_first, raw_last), 0) + 1
            )

        raw_number = e.get("raw_project_number")
        raw_name = e.get("raw_project_name")
        registry_hit = None
        if proj is None:
            registry_hit = payroll_matching.match_ignored(raw_number, raw_name, ignored)
            if registry_hit is None:
                unmatched_proj_counts[(raw_number, raw_name)] = (
                    unmatched_proj_counts.get((raw_number, raw_name), 0) + 1
                )

        # Non-CP buckets: everything that is not an enrolled project — those
        # hours matter for OT/proration but never reach a certified report.
        if not enrolled:
            if proj is not None:
                source = proj["name"]
            elif registry_hit is not None:
                source = registry_hit["raw_name"]
            else:
                source = raw_name or raw_number or "Unknown"
            key = e.get("employee_id") or f"raw:{raw_first} {raw_last}"
            bucket = non_cp.setdefault(
                key,
                {
                    "employee_id": e.get("employee_id"),
                    "employee_name": (
                        f"{emp['first_name']} {emp['last_name']}"
                        if emp
                        else f"{raw_first} {raw_last}"
                    ),
                    "hours": Decimal("0"),
                    "sources": set(),
                },
            )
            bucket["hours"] += Decimal(str(e.get("total_hours") or 0))
            bucket["sources"].add(source)

    record_rows = (
        sb.table("cp_records")
        .select("*")
        .eq("payroll_report_id", report_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []
    files_by_record: dict[str, list[dict]] = {}
    if record_rows:
        file_rows = (
            sb.table("cp_record_files")
            .select("*")
            .in_("record_id", [r["id"] for r in record_rows])
            .execute()
        ).data or []
        for f in file_rows:
            files_by_record.setdefault(f["record_id"], []).append(f)
    records = [{**r, "files": files_by_record.get(r["id"], [])} for r in record_rows]

    return {
        **report,
        "time_entries": time_entries,
        "detail_entries": detail_entries,
        "unmatched_employees": [
            {"first_name": first, "last_name": last, "entry_count": count}
            for (first, last), count in sorted(
                unmatched_emp_counts.items(),
                key=lambda kv: ((kv[0][0] or "").lower(), (kv[0][1] or "").lower()),
            )
        ],
        "unmatched_projects": [
            {"raw_number": number, "raw_name": name, "entry_count": count}
            for (number, name), count in sorted(
                unmatched_proj_counts.items(),
                key=lambda kv: ((kv[0][0] or "").lower(), (kv[0][1] or "").lower()),
            )
        ],
        "non_cp_hours": [
            {
                "employee_id": b["employee_id"],
                "employee_name": b["employee_name"],
                "hours": str(b["hours"]),
                "sources": sorted(b["sources"]),
            }
            for b in sorted(non_cp.values(), key=lambda b: (b["employee_name"] or "").lower())
        ],
        "stale_reasons": (
            check_stale_since_finalization(report, entries) if report.get("finalized_at") else []
        ),
        "finalize_issues": (
            finalize_gate_issues(report, entries) if report.get("status") == "processing" else []
        ),
        "records": records,
    }
