"""Certified Payroll — overtime calculation engine.

Implements chronological ST/OT/DT allocation across multiple projects per day
(daily threshold = min across that day's shift types, quarter-hour rounding,
early-start reclassification). Pure functions with no database access.

Shift types are the plain strings "regular" / "nights" / "swing" / "four_tens"
(CpShiftType in app/models/schemas.py). project_id is any opaque string key —
callers pass BDR project UUIDs or the synthetic merged-model keys ("bdr:…",
"ext:…", "raw:…"); allocation treats them all identically.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Tuple

# Shift type -> (ST threshold, DT threshold)
# OT range is [st_threshold, dt_threshold]
SHIFT_THRESHOLDS: Dict[str, Tuple[Decimal, Decimal]] = {
    "regular": (Decimal("8"), Decimal("12")),
    "nights": (Decimal("8"), Decimal("12")),
    "swing": (Decimal("8"), Decimal("12")),
    "four_tens": (Decimal("10"), Decimal("12")),
}


@dataclass
class HourSplit:
    """Hours split into ST/OT/DT for a single time entry."""

    st: Decimal = Decimal("0")
    ot: Decimal = Decimal("0")
    dt: Decimal = Decimal("0")


@dataclass
class TimeEntryInput:
    """Input data for a single time entry."""

    employee_id: str  # UUID as string
    project_id: str  # opaque project key (UUID or synthetic "bdr:/ext:/raw:" key)
    work_date: date
    start_time: datetime
    total_hours: Decimal
    shift_type: str
    shift_start_time: Optional[time] = None


@dataclass
class DailyProjectHours:
    """Aggregated ST/OT/DT hours for one (employee, project) on one day."""

    work_date: date
    st: Decimal = Decimal("0")
    ot: Decimal = Decimal("0")
    dt: Decimal = Decimal("0")


@dataclass
class EmployeeProjectWeekSummary:
    """Weekly summary of hours for one (employee, project) combination.

    daily_st/ot/dt arrays are indexed 0=Sunday through 6=Saturday.
    """

    employee_id: str
    project_id: str
    daily_st: List[Decimal] = field(default_factory=lambda: [Decimal("0")] * 7)
    daily_ot: List[Decimal] = field(default_factory=lambda: [Decimal("0")] * 7)
    daily_dt: List[Decimal] = field(default_factory=lambda: [Decimal("0")] * 7)

    @property
    def total_st(self) -> Decimal:
        return sum(self.daily_st, Decimal("0"))

    @property
    def total_ot(self) -> Decimal:
        return sum(self.daily_ot, Decimal("0"))

    @property
    def total_dt(self) -> Decimal:
        return sum(self.daily_dt, Decimal("0"))

    @property
    def total_hours(self) -> Decimal:
        return self.total_st + self.total_ot + self.total_dt


def round_quarter_hour(hours: Decimal) -> Decimal:
    """Round to the nearest 0.25 (quarter hour), rounding half up.

    Examples:
        0.0   -> 0.0
        0.1   -> 0.0
        0.125 -> 0.25   (exact midpoint rounds up)
        0.25  -> 0.25
        0.26  -> 0.25
        8.1   -> 8.0
        8.2   -> 8.25
        8.0   -> 8.0
    """
    if hours <= 0:
        return Decimal("0")
    quarter = Decimal("0.25")
    # Divide by 0.25, round to nearest integer (half up), multiply back
    quarters = (hours / quarter).to_integral_value(rounding=ROUND_HALF_UP)
    return quarters * quarter


def _day_index(work_date: date) -> int:
    """Convert a date to day-of-week index: 0=Sunday, 6=Saturday."""
    # Python: Monday=0 ... Sunday=6
    # We want: Sunday=0 ... Saturday=6
    py_weekday = work_date.weekday()  # Mon=0, Tue=1, ... Sun=6
    return (py_weekday + 1) % 7  # Sun=0, Mon=1, ... Sat=6


EARLY_START_GRACE = Decimal("3")  # Hours before shift start that are regular pay


def _calculate_early_start_ot(entry: TimeEntryInput) -> Decimal:
    """Calculate overtime hours due to starting more than 3h before shift start.

    Returns the number of hours that should be reclassified from ST to OT.
    """
    if entry.shift_start_time is None:
        return Decimal("0")
    if entry.total_hours <= 0:
        return Decimal("0")

    shift_start_dt = datetime.combine(entry.work_date, entry.shift_start_time)
    actual_start = entry.start_time

    # Ensure both datetimes have matching timezone awareness
    if actual_start.tzinfo is not None and shift_start_dt.tzinfo is None:
        shift_start_dt = shift_start_dt.replace(tzinfo=actual_start.tzinfo)
    elif actual_start.tzinfo is None and shift_start_dt.tzinfo is not None:
        shift_start_dt = shift_start_dt.replace(tzinfo=None)

    # How many hours early did the employee start?
    diff = shift_start_dt - actual_start
    early_seconds = diff.total_seconds()

    if early_seconds <= 0:
        # Employee started at or after shift start — no early-start OT
        return Decimal("0")

    early_hours = Decimal(str(early_seconds)) / Decimal("3600")

    # Guard: if early_hours > 12, likely an overnight date-boundary issue
    if early_hours > Decimal("12"):
        return Decimal("0")

    # Subtract 3-hour grace period
    excess = early_hours - EARLY_START_GRACE
    if excess <= 0:
        return Decimal("0")

    # Cap at entry's total hours
    return min(excess, entry.total_hours)


def calculate_daily_splits(
    entries: List[TimeEntryInput],
) -> List[Tuple[TimeEntryInput, HourSplit]]:
    """Calculate ST/OT/DT splits for all entries on a single day for one employee.

    Args:
        entries: All time entries for one (employee, day), across all projects.
                 Must all have the same employee_id and work_date.

    Returns:
        List of (TimeEntryInput, HourSplit) tuples, sorted chronologically.
    """
    if not entries:
        return []

    # Determine the lowest ST threshold among all projects worked that day
    st_threshold = Decimal("12")  # Start high
    dt_threshold = Decimal("12")
    for entry in entries:
        thresholds = SHIFT_THRESHOLDS.get(entry.shift_type, (Decimal("8"), Decimal("12")))
        if thresholds[0] < st_threshold:
            st_threshold = thresholds[0]
        if thresholds[1] < dt_threshold:
            dt_threshold = thresholds[1]

    # Pre-calculate early-start OT for each entry
    early_start_ot: Dict[str, Decimal] = {}
    for entry in entries:
        key = f"{entry.employee_id}:{entry.project_id}:{entry.start_time.isoformat()}"
        early_start_ot[key] = _calculate_early_start_ot(entry)

    # Sort by start_time chronologically
    sorted_entries = sorted(entries, key=lambda e: e.start_time)

    # Walk entries, tracking cumulative hours
    cumulative = Decimal("0")
    splits: List[Tuple[TimeEntryInput, HourSplit]] = []

    for entry in sorted_entries:
        hours = entry.total_hours
        split = HourSplit()

        if hours <= 0:
            splits.append((entry, split))
            continue

        entry_start = cumulative
        entry_end = cumulative + hours

        # Calculate ST portion: hours in [0, st_threshold]
        st_start = max(entry_start, Decimal("0"))
        st_end = min(entry_end, st_threshold)
        if st_end > st_start:
            split.st = st_end - st_start

        # Calculate OT portion: hours in [st_threshold, dt_threshold]
        ot_start = max(entry_start, st_threshold)
        ot_end = min(entry_end, dt_threshold)
        if ot_end > ot_start:
            split.ot = ot_end - ot_start

        # Calculate DT portion: hours beyond dt_threshold
        dt_start = max(entry_start, dt_threshold)
        dt_end = entry_end
        if dt_end > dt_start:
            split.dt = dt_end - dt_start

        # Apply early-start OT: reclassify hours from ST -> OT
        key = f"{entry.employee_id}:{entry.project_id}:{entry.start_time.isoformat()}"
        early_ot = early_start_ot.get(key, Decimal("0"))
        if early_ot > 0 and split.st > 0:
            reclassify = min(early_ot, split.st)
            split.st -= reclassify
            split.ot += reclassify

        cumulative = entry_end
        splits.append((entry, split))

    return splits


def calculate_weekly_summaries(
    all_entries: List[TimeEntryInput],
    week_start: date,
) -> List[EmployeeProjectWeekSummary]:
    """Calculate weekly OT summaries for all (employee, project) combinations.

    Args:
        all_entries: All time entries for the week across all employees and projects.
        week_start: The Sunday that starts the week.

    Returns:
        List of EmployeeProjectWeekSummary, one per (employee, project).

    Entries whose work_date falls outside the Sun–Sat window [week_start,
    week_start+6] are dropped: the summary is indexed purely by weekday, so a
    stray day from an adjacent week (a BusyBusy export that straddles the payroll
    week) would otherwise fold into the same weekday column and inflate the
    certified totals. Callers that need to surface the drop use
    out_of_week_entries().
    """
    week_end = week_start + timedelta(days=6)
    # Group entries by (employee_id, work_date), keeping only in-week days.
    by_employee_day: Dict[str, Dict[date, List[TimeEntryInput]]] = {}
    for entry in all_entries:
        if not (week_start <= entry.work_date <= week_end):
            continue
        emp_key = entry.employee_id
        if emp_key not in by_employee_day:
            by_employee_day[emp_key] = {}
        day_entries = by_employee_day[emp_key]
        if entry.work_date not in day_entries:
            day_entries[entry.work_date] = []
        day_entries[entry.work_date].append(entry)

    # For each employee, process each day, accumulate into (employee, project) summaries
    summaries: Dict[Tuple[str, str], EmployeeProjectWeekSummary] = {}

    for emp_id, days in by_employee_day.items():
        for work_date, day_entries in days.items():
            day_idx = _day_index(work_date)

            # Calculate splits for this employee's day
            splits = calculate_daily_splits(day_entries)

            # Accumulate into per-(employee, project) summaries
            for entry, split in splits:
                key = (emp_id, entry.project_id)
                if key not in summaries:
                    summaries[key] = EmployeeProjectWeekSummary(
                        employee_id=emp_id,
                        project_id=entry.project_id,
                    )
                summary = summaries[key]
                summary.daily_st[day_idx] += split.st
                summary.daily_ot[day_idx] += split.ot
                summary.daily_dt[day_idx] += split.dt

    return list(summaries.values())


def out_of_week_entries(
    all_entries: List[TimeEntryInput],
    week_start: date,
) -> List[TimeEntryInput]:
    """Entries whose work_date falls outside [week_start, week_start+6].

    calculate_weekly_summaries silently drops these (they would corrupt the
    weekday-indexed columns); this exposes them so an upload path can surface a
    "N entries outside the report week were ignored" warning instead of quietly
    losing hours.
    """
    week_end = week_start + timedelta(days=6)
    return [e for e in all_entries if not (week_start <= e.work_date <= week_end)]
