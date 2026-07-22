"""Pure unit tests for app/services/payroll_ot.py — no DB, no network.

Covers the quarter-hour rounding table, the Sunday-start day index, chronological
daily ST/OT/DT allocation across projects, the min-threshold rule for mixed
shift types, the early-start (>3h) reclassification with its overnight guard,
and weekly summary accumulation — including the synthetic merged-model project
keys ("bdr:…", "ext:…", "raw:…") other CP packages feed through allocation.
"""

from datetime import date, datetime, time
from decimal import Decimal

import pytest

from app.services.payroll_ot import (
    EmployeeProjectWeekSummary,
    HourSplit,
    TimeEntryInput,
    _day_index,
    calculate_daily_splits,
    calculate_weekly_summaries,
    round_quarter_hour,
)

SUNDAY = date(2026, 7, 12)  # a Sunday
MONDAY = date(2026, 7, 13)
SATURDAY = date(2026, 7, 18)

EMP = "emp-1"
PROJ = "proj-1"


def entry(
    hours,
    start=None,
    *,
    emp=EMP,
    project=PROJ,
    work_date=SUNDAY,
    shift_type="regular",
    shift_start=None,
):
    if start is None:
        start = datetime.combine(work_date, time(7, 0))
    return TimeEntryInput(
        employee_id=emp,
        project_id=project,
        work_date=work_date,
        start_time=start,
        total_hours=Decimal(str(hours)),
        shift_type=shift_type,
        shift_start_time=shift_start,
    )


# ── round_quarter_hour ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.0", "0"),
        ("0.1", "0"),
        ("0.125", "0.25"),  # exact midpoint rounds up (ROUND_HALF_UP)
        ("0.25", "0.25"),
        ("0.26", "0.25"),
        ("8.1", "8.0"),
        ("8.2", "8.25"),
        ("8.0", "8.0"),
        ("8.375", "8.50"),  # another midpoint
        ("-1", "0"),  # non-positive clamps to zero
    ],
)
def test_round_quarter_hour_table(raw, expected):
    assert round_quarter_hour(Decimal(raw)) == Decimal(expected)


# ── _day_index (Sunday = 0) ─────────────────────────────────────────────────


def test_day_index_sunday_start():
    assert SUNDAY.weekday() == 6  # sanity: fixture really is a Sunday
    assert _day_index(SUNDAY) == 0
    assert _day_index(MONDAY) == 1
    assert _day_index(SATURDAY) == 6


# ── calculate_daily_splits: thresholds ──────────────────────────────────────


def test_empty_input_returns_empty_list():
    # Bugfix over the legacy port: used to return {} where callers expect a list.
    result = calculate_daily_splits([])
    assert result == []
    assert isinstance(result, list)


def test_single_entry_under_threshold_all_st():
    [(e, split)] = calculate_daily_splits([entry(8)])
    assert (split.st, split.ot, split.dt) == (Decimal("8"), Decimal("0"), Decimal("0"))


def test_single_entry_crosses_st_ot_dt():
    [(e, split)] = calculate_daily_splits([entry(13)])
    assert (split.st, split.ot, split.dt) == (Decimal("8"), Decimal("4"), Decimal("1"))


def test_zero_hour_entry_gets_zero_split_and_does_not_advance_cumulative():
    zero = entry(0, start=datetime.combine(SUNDAY, time(6, 0)))
    work = entry(9, start=datetime.combine(SUNDAY, time(7, 0)))
    splits = calculate_daily_splits([work, zero])
    by_start = {e.start_time: s for e, s in splits}
    assert by_start[zero.start_time] == HourSplit()
    # The 9h entry still splits from cumulative 0: 8 ST + 1 OT
    s = by_start[work.start_time]
    assert (s.st, s.ot, s.dt) == (Decimal("8"), Decimal("1"), Decimal("0"))


def test_chronological_allocation_across_projects():
    # Passed out of order; must be sorted by start_time before allocating.
    late = entry(4, start=datetime.combine(SUNDAY, time(14, 0)), project="proj-B")
    early = entry(6, start=datetime.combine(SUNDAY, time(7, 0)), project="proj-A")
    splits = calculate_daily_splits([late, early])
    assert [e.project_id for e, _ in splits] == ["proj-A", "proj-B"]
    (_, first), (_, second) = splits
    assert (first.st, first.ot, first.dt) == (Decimal("6"), Decimal("0"), Decimal("0"))
    # Cumulative 6 -> 10: 2h ST (to the 8h line) then 2h OT
    assert (second.st, second.ot, second.dt) == (Decimal("2"), Decimal("2"), Decimal("0"))


def test_four_tens_alone_uses_ten_hour_threshold():
    [(_, split)] = calculate_daily_splits([entry(11, shift_type="four_tens")])
    assert (split.st, split.ot, split.dt) == (Decimal("10"), Decimal("1"), Decimal("0"))


def test_four_tens_mixed_with_regular_pulls_threshold_down_to_eight():
    # Per-day threshold is the MIN across that day's projects: 8, not 10.
    reg = entry(5, start=datetime.combine(SUNDAY, time(7, 0)), shift_type="regular")
    tens = entry(
        6, start=datetime.combine(SUNDAY, time(12, 30)), project="proj-B", shift_type="four_tens"
    )
    splits = calculate_daily_splits([reg, tens])
    (_, first), (_, second) = splits
    assert (first.st, first.ot, first.dt) == (Decimal("5"), Decimal("0"), Decimal("0"))
    assert (second.st, second.ot, second.dt) == (Decimal("3"), Decimal("3"), Decimal("0"))


def test_unknown_shift_type_defaults_to_regular_thresholds():
    [(_, split)] = calculate_daily_splits([entry(9, shift_type="mystery")])
    assert (split.st, split.ot) == (Decimal("8"), Decimal("1"))


# ── early-start reclassification ────────────────────────────────────────────


def test_early_start_exactly_three_hours_is_all_st():
    e = entry(8, start=datetime.combine(SUNDAY, time(4, 0)), shift_start=time(7, 0))
    [(_, split)] = calculate_daily_splits([e])
    assert (split.st, split.ot, split.dt) == (Decimal("8"), Decimal("0"), Decimal("0"))


def test_early_start_past_three_hours_reclassifies_excess_to_ot():
    # 3.5h early -> 0.5h moved from ST to OT.
    e = entry(8, start=datetime.combine(SUNDAY, time(3, 30)), shift_start=time(7, 0))
    [(_, split)] = calculate_daily_splits([e])
    assert (split.st, split.ot, split.dt) == (Decimal("7.5"), Decimal("0.5"), Decimal("0"))


def test_early_start_capped_at_entry_total_hours():
    # 12h early (guard allows exactly 12) -> excess 9h, capped at the 2h worked.
    e = entry(2, start=datetime.combine(SUNDAY, time(8, 0)), shift_start=time(20, 0))
    [(_, split)] = calculate_daily_splits([e])
    assert (split.st, split.ot, split.dt) == (Decimal("0"), Decimal("2"), Decimal("0"))


def test_early_start_overnight_guard_over_twelve_hours():
    # 14h "early" is treated as an overnight date-boundary artifact: no reclass.
    e = entry(8, start=datetime.combine(SUNDAY, time(8, 0)), shift_start=time(22, 0))
    [(_, split)] = calculate_daily_splits([e])
    assert (split.st, split.ot, split.dt) == (Decimal("8"), Decimal("0"), Decimal("0"))


def test_start_at_or_after_shift_start_no_reclass():
    e = entry(8, start=datetime.combine(SUNDAY, time(7, 0)), shift_start=time(7, 0))
    [(_, split)] = calculate_daily_splits([e])
    assert split.ot == Decimal("0")


# ── calculate_weekly_summaries ──────────────────────────────────────────────


def test_weekly_summary_accumulates_across_days():
    entries = [
        entry(9, work_date=SUNDAY, start=datetime.combine(SUNDAY, time(7, 0))),
        entry(4, work_date=MONDAY, start=datetime.combine(MONDAY, time(7, 0))),
        entry(13, work_date=SATURDAY, start=datetime.combine(SATURDAY, time(5, 0))),
    ]
    [summary] = calculate_weekly_summaries(entries, week_start=SUNDAY)
    assert summary.employee_id == EMP
    assert summary.project_id == PROJ
    assert summary.daily_st == [Decimal(x) for x in ("8", "4", "0", "0", "0", "0", "8")]
    assert summary.daily_ot == [Decimal(x) for x in ("1", "0", "0", "0", "0", "0", "4")]
    assert summary.daily_dt == [Decimal(x) for x in ("0", "0", "0", "0", "0", "0", "1")]
    assert summary.total_st == Decimal("20")
    assert summary.total_ot == Decimal("5")
    assert summary.total_dt == Decimal("1")
    assert summary.total_hours == Decimal("26")


def test_weekly_summary_splits_per_project_and_employee():
    entries = [
        entry(6, project="proj-A", start=datetime.combine(SUNDAY, time(7, 0))),
        entry(4, project="proj-B", start=datetime.combine(SUNDAY, time(14, 0))),
        entry(8, emp="emp-2", project="proj-A", start=datetime.combine(SUNDAY, time(7, 0))),
    ]
    summaries = {
        (s.employee_id, s.project_id): s for s in calculate_weekly_summaries(entries, SUNDAY)
    }
    assert set(summaries) == {(EMP, "proj-A"), (EMP, "proj-B"), ("emp-2", "proj-A")}
    a = summaries[(EMP, "proj-A")]
    b = summaries[(EMP, "proj-B")]
    assert (a.daily_st[0], a.daily_ot[0]) == (Decimal("6"), Decimal("0"))
    assert (b.daily_st[0], b.daily_ot[0]) == (Decimal("2"), Decimal("2"))
    assert summaries[("emp-2", "proj-A")].total_hours == Decimal("8")


def test_synthetic_project_keys_flow_through_allocation():
    # The merged model uses "bdr:<uuid>" / "ext:<id>" / "raw:<name>" keys; they
    # must count toward OT allocation exactly like any other project key.
    entries = [
        entry(5, project="bdr:x", start=datetime.combine(SUNDAY, time(6, 0))),
        entry(4, project="ext:y", start=datetime.combine(SUNDAY, time(11, 30))),
        entry(4, project="raw:z", start=datetime.combine(SUNDAY, time(16, 0))),
    ]
    summaries = {s.project_id: s for s in calculate_weekly_summaries(entries, SUNDAY)}
    assert set(summaries) == {"bdr:x", "ext:y", "raw:z"}
    assert (summaries["bdr:x"].total_st, summaries["bdr:x"].total_ot) == (
        Decimal("5"),
        Decimal("0"),
    )
    # ext:y crosses the 8h line at cumulative 5 -> 9: 3 ST + 1 OT
    assert (summaries["ext:y"].total_st, summaries["ext:y"].total_ot) == (
        Decimal("3"),
        Decimal("1"),
    )
    # raw:z spans 9 -> 13: 3 OT (to the 12h line) + 1 DT
    assert (
        summaries["raw:z"].total_st,
        summaries["raw:z"].total_ot,
        summaries["raw:z"].total_dt,
    ) == (Decimal("0"), Decimal("3"), Decimal("1"))


def test_weekly_summary_empty_input():
    assert calculate_weekly_summaries([], SUNDAY) == []


def test_summary_totals_start_at_zero():
    s = EmployeeProjectWeekSummary(employee_id=EMP, project_id=PROJ)
    assert s.total_hours == Decimal("0")
