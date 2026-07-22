"""Certified Payroll — employee / project matching for the weekly report pipeline.

Raw timesheet rows carry free-text employee and project names; matching resolves
them against the shared employee registry and the BDR projects spine. The core
merged-model rule: a raw project that matches ANY BDR project gets project_id +
is_project_matched=true — CP-enrolled projects are tried first (by number, then
by the number extracted from "6370 - Title" raw strings, then by exact title),
then every other BDR project in the same order. The cp_ignored_projects registry
never sets project_id: hits are recognized at read time, so office/shop codes
stop nagging without pretending to be projects. "Unknown project" = project_id
NULL and no registry hit — the only project state that hard-blocks finalize.
"""

from fastapi import HTTPException, status

from app.core.supabase_client import get_supabase

# ── Keys and reference loads (normalize_key/load_ignored_projects/match_ignored
#    are pinned interfaces — other CP modules import them by these names) ───────


def normalize_key(value: str | None) -> str:
    """Case/whitespace-insensitive comparison key; "" for None."""
    return (value or "").strip().lower()


def _number_key(value: str | None) -> str:
    """normalize_key for PROJECT NUMBERS, tolerating pandas float inference: a
    purely-numeric Excel "Project #" column with blanks arrives as "6370.0"
    (see test_parse_excel_numeric_project_column_arrives_as_float_string) —
    strip the ".0" so it still matches project number "6370". Names must never
    go through this (a real title could legitimately end in ".0")."""
    key = normalize_key(value)
    if key.endswith(".0") and key[:-2].isdigit():
        return key[:-2]
    return key


def load_ignored_projects() -> list[dict]:
    return get_supabase().table("cp_ignored_projects").select("*").execute().data or []


def match_ignored(
    raw_number: str | None, raw_name: str | None, ignored: list[dict]
) -> dict | None:
    """Registry lookup: by raw number first, then by raw name."""
    number = _number_key(raw_number)
    if number:
        for row in ignored:
            if _number_key(row.get("raw_number")) == number:
                return row
    name = normalize_key(raw_name)
    if name:
        for row in ignored:
            if normalize_key(row.get("raw_name")) == name:
                return row
    return None


def load_employees() -> list[dict]:
    return get_supabase().table("employees").select("*").execute().data or []


def load_projects() -> list[dict]:
    """All BDR projects, only the columns matching needs — cp_enrolled_at drives
    the enrolled-first pass."""
    return (
        get_supabase().table("projects").select("id, name, number, cp_enrolled_at").execute()
    ).data or []


# ── Employee matching ──────────────────────────────────────────────────────────


def _by_name(first: str, last: str, employees: list[dict]) -> dict | None:
    for emp in employees:
        if (
            normalize_key(emp.get("first_name")) == first
            and normalize_key(emp.get("last_name")) == last
        ):
            return emp
    return None


def find_employee(
    first_name: str | None, last_name: str | None, employees: list[dict]
) -> dict | None:
    """Exact lower(first)+lower(last), then the "Bernard (Bernie)" nickname forms,
    then alt_ee_name against the full raw name (the timesheet display override)."""
    first, last = normalize_key(first_name), normalize_key(last_name)
    if not first and not last:
        return None
    emp = _by_name(first, last, employees)
    if emp:
        return emp
    raw_first = first_name or ""
    if "(" in raw_first and ")" in raw_first:
        base = normalize_key(raw_first.split("(")[0])
        emp = _by_name(base, last, employees)
        if emp:
            return emp
        nickname = normalize_key(raw_first.split("(")[1].split(")")[0])
        emp = _by_name(nickname, last, employees)
        if emp:
            return emp
    full = normalize_key(f"{first_name or ''} {last_name or ''}")
    for emp in employees:
        alt = normalize_key(emp.get("alt_ee_name"))
        if alt and alt == full:
            return emp
    return None


def match_detail_employee(raw_name: str | None, employees: list[dict]) -> dict | None:
    """Match a Gusto-format name — "LASTNAME, FIRSTNAME M" (middle initial
    optional); a comma-less name falls back to first/last word."""
    if not raw_name:
        return None
    parts = raw_name.split(",", 1)
    if len(parts) == 2:
        last_name = parts[0].strip()
        first_parts = parts[1].strip().split()
        first_name = first_parts[0] if first_parts else ""
    else:
        name_parts = raw_name.strip().split()
        if len(name_parts) < 2:
            return None
        first_name, last_name = name_parts[0], name_parts[-1]
    return find_employee(first_name, last_name, employees)


# ── Project matching ───────────────────────────────────────────────────────────


def extract_project_number(raw_name: str | None) -> str | None:
    """Pull the leading number out of a "6370 - Terminal 3" style raw string."""
    if raw_name and " - " in raw_name:
        return raw_name.split(" - ")[0].strip()
    return None


def _match_in(raw_number: str | None, raw_name: str | None, projects: list[dict]) -> dict | None:
    number = _number_key(raw_number)
    if number:
        for project in projects:
            if _number_key(project.get("number")) == number:
                return project
    extracted = _number_key(extract_project_number(raw_name))
    if extracted:
        for project in projects:
            if _number_key(project.get("number")) == extracted:
                return project
    title = normalize_key(raw_name)
    if title:
        for project in projects:
            if normalize_key(project.get("name")) == title:
                return project
    return None


def find_project(
    raw_number: str | None, raw_name: str | None, projects: list[dict]
) -> dict | None:
    """The pinned matching order: CP-enrolled projects get every pass first so a
    number/title collision with a non-enrolled job can never shadow a live CP job."""
    enrolled = [p for p in projects if p.get("cp_enrolled_at")]
    hit = _match_in(raw_number, raw_name, enrolled)
    if hit:
        return hit
    others = [p for p in projects if not p.get("cp_enrolled_at")]
    return _match_in(raw_number, raw_name, others)


# ── The shared matching pass ───────────────────────────────────────────────────


def match_entries(
    entries: list[dict],
    employees: list[dict],
    projects: list[dict],
    ignored: list[dict],
) -> dict:
    """Mutate each time-entry dict in place (employee_id / project_id / matched
    flags) and return the summary counts. Registry hits never set project_id and
    never count as unmatched — they are intentionally non-CP."""
    matched_employees: set[str] = set()
    unmatched_employees: set[str] = set()
    matched_projects: set[str] = set()
    unmatched_projects: set[str] = set()
    unique_employee_ids: set[str] = set()

    for entry in entries:
        employee = find_employee(
            entry.get("raw_employee_first_name"), entry.get("raw_employee_last_name"), employees
        )
        employee_key = (
            f"{entry.get('raw_employee_first_name')} {entry.get('raw_employee_last_name')}"
        )
        if employee:
            matched_employees.add(employee_key)
            unique_employee_ids.add(employee["id"])
        else:
            unmatched_employees.add(employee_key)
        entry["employee_id"] = employee["id"] if employee else None
        entry["is_employee_matched"] = employee is not None

        raw_number = entry.get("raw_project_number")
        raw_name = entry.get("raw_project_name")
        project = find_project(raw_number, raw_name, projects)
        project_key = raw_name or raw_number or "Unknown"
        if project:
            matched_projects.add(project_key)
        elif (raw_number or raw_name) and match_ignored(raw_number, raw_name, ignored) is None:
            unmatched_projects.add(project_key)
        entry["project_id"] = project["id"] if project else None
        entry["is_project_matched"] = project is not None

    return {
        "matched_employees": len(matched_employees),
        "unmatched_employees": sorted(unmatched_employees),
        "matched_projects": len(matched_projects),
        "unmatched_projects": sorted(unmatched_projects),
        "unique_employee_ids": unique_employee_ids,
    }


def rematch_report(report_id: str) -> dict:
    """Re-run employee + project matching over a report's stored raw names (no
    re-upload needed after registry/employee/project edits). Persists only rows
    whose match actually changed, then refreshes the report's total_employees."""
    sb = get_supabase()
    rows = (
        sb.table("cp_payroll_reports").select("*").eq("id", report_id).limit(1).execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payroll report not found")
    report = rows[0]
    # A finalized ('processed') report has generated CPR files bound to the
    # current matching; re-matching underneath them would silently desync the
    # filed figures from the assignments. Matching closes at finalize, same as
    # the submitted-immutability invariant.
    if report["status"] in ("processed", "submitted"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Report has been finalized — matching is closed",
        )

    entries = (
        sb.table("cp_time_entries").select("*").eq("payroll_report_id", report_id).execute()
    ).data or []
    details = (
        sb.table("cp_payroll_detail_entries")
        .select("id, employee_name, employee_id, is_employee_matched")
        .eq("payroll_report_id", report_id)
        .execute()
    ).data or []

    employees = load_employees()
    projects = load_projects()
    ignored = load_ignored_projects()

    before = {
        e["id"]: (
            e.get("employee_id"),
            e.get("is_employee_matched"),
            e.get("project_id"),
            e.get("is_project_matched"),
        )
        for e in entries
    }
    summary = match_entries(entries, employees, projects, ignored)
    for entry in entries:
        after = (
            entry["employee_id"],
            entry["is_employee_matched"],
            entry["project_id"],
            entry["is_project_matched"],
        )
        if before[entry["id"]] != after:
            sb.table("cp_time_entries").update(
                {
                    "employee_id": entry["employee_id"],
                    "is_employee_matched": entry["is_employee_matched"],
                    "project_id": entry["project_id"],
                    "is_project_matched": entry["is_project_matched"],
                }
            ).eq("id", entry["id"]).execute()

    for detail in details:
        employee = match_detail_employee(detail.get("employee_name"), employees)
        employee_id = employee["id"] if employee else None
        matched = employee is not None
        if (detail.get("employee_id"), detail.get("is_employee_matched")) != (employee_id, matched):
            sb.table("cp_payroll_detail_entries").update(
                {"employee_id": employee_id, "is_employee_matched": matched}
            ).eq("id", detail["id"]).execute()

    if entries:
        sb.table("cp_payroll_reports").update(
            {"total_employees": len(summary["unique_employee_ids"])}
        ).eq("id", report_id).execute()

    unresolved = len(summary["unmatched_employees"]) + len(summary["unmatched_projects"])
    if unresolved:
        message = (
            f"Re-processed {len(entries)} time entries — "
            f"{len(summary['unmatched_employees'])} unmatched employee(s) and "
            f"{len(summary['unmatched_projects'])} unmatched project(s) remaining"
        )
    else:
        message = f"Re-processed {len(entries)} time entries — all employees and projects matched"

    return {
        "payroll_report_id": report_id,
        "status": report["status"],
        "total_entries": len(entries),
        "matched_employees": summary["matched_employees"],
        "unmatched_employees": summary["unmatched_employees"],
        "matched_projects": summary["matched_projects"],
        "unmatched_projects": summary["unmatched_projects"],
        "message": message,
    }
