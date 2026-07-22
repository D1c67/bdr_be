"""Certified Payroll — BusyBusy timesheet parser (CSV / Excel exports).

Parses the weekly BB timesheet export into ParsedTimeEntry rows. Entry point is
parse_timesheet(file_content: bytes, filename: str); dispatch is by extension.
Header names are the exports' verbatim column titles — note "Subproject 1  #"
really does contain a double space. Times are stripped to naive datetimes (the
DB columns are TIMESTAMP WITHOUT TIME ZONE and already represent local time).
"""

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ParsedTimeEntry:
    """Parsed time entry from CSV/Excel file."""

    employee_id: Optional[str]
    first_name: str
    last_name: str
    work_date: date
    start_time: datetime
    end_time: datetime
    break_total_minutes: int
    total_hours: Decimal
    customer: Optional[str]
    project_number: Optional[str]
    project_name: Optional[str]
    subproject_1_number: Optional[str]
    subproject_1_name: Optional[str]
    cost_code: Optional[str]
    cost_code_desc: Optional[str]
    description: Optional[str]


def parse_time_string(time_str: str) -> int:
    """Parse time string like '00:30' to minutes."""
    if not time_str or time_str == "":
        return 0
    try:
        parts = time_str.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return 0
    except (ValueError, AttributeError):
        return 0


def parse_hours_string(hours_str: str) -> Decimal:
    """Parse a BB 'Total' hours cell to decimal hours.

    Accepts the "H:MM" clock form ('10:00', '08:30') and a plain decimal form
    ('10', '10.5') — an export whose Total column is numeric rather than clock
    formatted must not silently zero every worked hour. Genuinely unparseable
    values still return 0.00.
    """
    if hours_str is None:
        return Decimal("0.00")
    s = str(hours_str).strip()
    if not s:
        return Decimal("0.00")
    try:
        if ":" in s:
            parts = s.split(":")
            if len(parts) == 2:
                hours = int(parts[0])
                minutes = int(parts[1])
                return Decimal(str(hours)) + Decimal(str(minutes)) / Decimal("60")
            return Decimal("0.00")
        # Plain decimal hours (e.g. "10", "10.5").
        return Decimal(s)
    except (ValueError, AttributeError, InvalidOperation):
        return Decimal("0.00")


def parse_datetime(dt_str: str) -> Optional[datetime]:
    """Parse ISO datetime string, returning a naive (tz-unaware) datetime."""
    if not dt_str:
        return None
    try:
        # Handle ISO format with timezone
        # e.g., "2026-01-26T05:03:00.000-08:00"
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        # Strip timezone info — the DB column is TIMESTAMP WITHOUT TIME ZONE
        # and the times already represent local time
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, AttributeError):
        return None


def _cell(row: dict, key: str) -> str:
    """A stripped string for a CSV cell. A short data row makes csv.DictReader
    fill the missing columns with None (restval), so `row.get(key, "")` returns
    None, not the default — `(row.get(key) or "")` guards the .strip()."""
    return (row.get(key) or "").strip()


def parse_csv_timesheet(file_content: bytes) -> List[ParsedTimeEntry]:
    """Parse BB timesheet CSV file."""
    entries = []
    dropped = 0

    # Decode and read CSV
    content = file_content.decode("utf-8-sig")  # Handle BOM if present
    reader = csv.DictReader(io.StringIO(content))

    for row in reader:
        # Skip empty rows
        if not _cell(row, "First Name") and not _cell(row, "Last Name"):
            continue

        # Parse start and end times
        start_time = parse_datetime(_cell(row, "Start"))
        end_time = parse_datetime(_cell(row, "End"))

        if not start_time or not end_time:
            # A named row we cannot place in time — count it so the drop is
            # visible rather than silently losing the hours.
            dropped += 1
            continue

        # Determine work date from start time
        work_date = start_time.date()

        # Parse break and total hours
        break_minutes = parse_time_string(_cell(row, "Break Total"))
        total_hours = parse_hours_string(_cell(row, "Total"))

        # Extract project number from format like "6370" or "6370 - Terminal..."
        project_number = _cell(row, "Project #") or None

        entry = ParsedTimeEntry(
            employee_id=_cell(row, "Employee ID") or None,
            first_name=_cell(row, "First Name"),
            last_name=_cell(row, "Last Name"),
            work_date=work_date,
            start_time=start_time,
            end_time=end_time,
            break_total_minutes=break_minutes,
            total_hours=total_hours,
            customer=_cell(row, "Customer") or None,
            project_number=project_number,
            project_name=_cell(row, "Project") or None,
            subproject_1_number=_cell(row, "Subproject 1  #") or None,
            subproject_1_name=_cell(row, "Subproject 1") or None,
            cost_code=_cell(row, "Cost Code") or None,
            cost_code_desc=_cell(row, "Cost Code Desc.") or None,
            description=_cell(row, "Description") or None,
        )
        entries.append(entry)

    if dropped:
        logger.warning(
            "parse_csv_timesheet: dropped %d row(s) with unparseable Start/End times", dropped
        )
    return entries


def parse_excel_timesheet(file_content: bytes) -> List[ParsedTimeEntry]:
    """Parse BB timesheet Excel file."""
    entries = []

    # Read Excel file
    df = pd.read_excel(io.BytesIO(file_content))

    for _, row in df.iterrows():
        # Skip empty rows
        if pd.isna(row.get("First Name")) and pd.isna(row.get("Last Name")):
            continue

        first_name = (
            str(row.get("First Name", "")).strip() if not pd.isna(row.get("First Name")) else ""
        )
        last_name = (
            str(row.get("Last Name", "")).strip() if not pd.isna(row.get("Last Name")) else ""
        )

        if not first_name and not last_name:
            continue

        # Parse datetime values
        start_time = row.get("Start")
        end_time = row.get("End")

        if pd.isna(start_time) or pd.isna(end_time):
            continue

        # Convert to datetime if needed
        if isinstance(start_time, str):
            start_time = parse_datetime(start_time)
        if isinstance(end_time, str):
            end_time = parse_datetime(end_time)

        if not start_time or not end_time:
            continue

        # Strip timezone info for DB compatibility
        if hasattr(start_time, "tzinfo") and start_time.tzinfo is not None:
            start_time = start_time.replace(tzinfo=None)
        if hasattr(end_time, "tzinfo") and end_time.tzinfo is not None:
            end_time = end_time.replace(tzinfo=None)

        work_date = start_time.date() if hasattr(start_time, "date") else start_time

        # Parse break and total
        break_str = str(row.get("Break Total", "")) if not pd.isna(row.get("Break Total")) else ""
        total_str = str(row.get("Total", "")) if not pd.isna(row.get("Total")) else ""

        break_minutes = parse_time_string(break_str)
        total_hours = parse_hours_string(total_str)

        project_number = (
            str(row.get("Project #", "")).strip() if not pd.isna(row.get("Project #")) else None
        )

        entry = ParsedTimeEntry(
            employee_id=(
                str(row.get("Employee ID", "")).strip()
                if not pd.isna(row.get("Employee ID"))
                else None
            ),
            first_name=first_name,
            last_name=last_name,
            work_date=work_date,
            start_time=start_time,
            end_time=end_time,
            break_total_minutes=break_minutes,
            total_hours=total_hours,
            customer=(
                str(row.get("Customer", "")).strip() if not pd.isna(row.get("Customer")) else None
            ),
            project_number=project_number,
            project_name=(
                str(row.get("Project", "")).strip() if not pd.isna(row.get("Project")) else None
            ),
            subproject_1_number=(
                str(row.get("Subproject 1  #", "")).strip()
                if not pd.isna(row.get("Subproject 1  #"))
                else None
            ),
            subproject_1_name=(
                str(row.get("Subproject 1", "")).strip()
                if not pd.isna(row.get("Subproject 1"))
                else None
            ),
            cost_code=(
                str(row.get("Cost Code", "")).strip()
                if not pd.isna(row.get("Cost Code"))
                else None
            ),
            cost_code_desc=(
                str(row.get("Cost Code Desc.", "")).strip()
                if not pd.isna(row.get("Cost Code Desc."))
                else None
            ),
            description=(
                str(row.get("Description", "")).strip()
                if not pd.isna(row.get("Description"))
                else None
            ),
        )
        entries.append(entry)

    return entries


def parse_timesheet(file_content: bytes, filename: str) -> List[ParsedTimeEntry]:
    """Parse timesheet file based on extension.

    A corrupt/truncated upload (BadZipFile from pandas, a decode error, an
    unreadable workbook) is normalized to ValueError so the upload handler
    returns a clean 400 instead of leaking a CORS-less 500 — the same contract
    the caller already relies on for unsupported formats.
    """
    filename_lower = filename.lower()

    if filename_lower.endswith(".csv"):
        parse = parse_csv_timesheet
    elif filename_lower.endswith((".xlsx", ".xls")):
        parse = parse_excel_timesheet
    else:
        raise ValueError(f"Unsupported file format: {filename}")

    try:
        return parse(file_content)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — corrupt/unreadable upload → 400, not 500
        raise ValueError(f"Could not read timesheet file: {exc}") from exc


def get_week_dates(selected_date: date) -> tuple:
    """Get week start (Sunday) and end (Saturday) dates for a given date."""
    # Find the Sunday of the week
    days_since_sunday = selected_date.weekday() + 1
    if days_since_sunday == 7:  # If it's Sunday
        days_since_sunday = 0

    week_start = selected_date - timedelta(days=days_since_sunday)
    week_end = week_start + timedelta(days=6)

    return week_start, week_end
