"""CPR flags + merged-model row building (services/cpr_flags, cpr_generation).

Pure tests over hand-built in-memory structures — no database, no network. The
dataclass seam in cpr_generation makes ReportData trivially constructible, so
_build_row_data (proration over the FULL-week denominator, the non-CP skip, the
zero-rows message) and generate_cpr_flags (enrolled-only compliance checks, the
new NON_CP_HOURS / UNKNOWN_PROJECT flags, discrepancy totals that include
non-CP buckets) are exercised directly.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.services import cpr_generation
from app.services.cpr_flags import generate_cpr_flags
from app.services.cpr_generation import (
    ClassificationRec,
    DetailRec,
    EmployeeRec,
    ProjectRec,
    RateRec,
    ReportData,
    ReportRec,
    TimeEntryRec,
    _build_row_data,
)
from app.services.payroll_ot import EmployeeProjectWeekSummary

WEEK_START = date(2026, 7, 5)  # Sunday
WEEK_END = date(2026, 7, 11)  # Saturday


# ── Builders ───────────────────────────────────────────────────────────────────


def _report():
    return ReportRec(
        id="r1",
        week_start_date=WEEK_START,
        week_end_date=WEEK_END,
        status="processed",
        timesheet_filename="timesheet.xlsx",
        payroll_detail_filename="detail.csv",
        finalized_at="2026-07-13T00:00:00Z",
    )


def _employee(emp_id="e1"):
    return EmployeeRec(id=emp_id, first_name="Alma", last_name="Reyes")


def _project(pid="p1", report_type="lcp_tracker", **overrides):
    fields = {
        "id": pid,
        "project_number": "6370",
        "project_title": "Fire Station 12",
        "contract_id": "C-100",
        "report_type": report_type,
    }
    fields.update(overrides)
    return ProjectRec(**fields)


def _classification(cls_id="c1", is_field=True):
    return ClassificationRec(id=cls_id, code="JW", name="Journeyman Wireman", is_field=is_field)


def _rate(hourly="50.00"):
    return RateRec(
        hourly_rate=Decimal(hourly),
        overtime_rate=Decimal("75.00"),
        doubletime_rate=Decimal("100.00"),
    )


def _detail(emp_id="e1", gross="1000.00", **overrides):
    fields = {
        "employee_name": "Alma Reyes",
        "employee_id": emp_id,
        "gross_pay_total": Decimal(gross),
        "net_pay": Decimal("800.00"),
    }
    fields.update({k: (Decimal(v) if isinstance(v, str) else v) for k, v in overrides.items()})
    return DetailRec(**fields)


def _summary(emp_id, key, st=None, ot=None, dt=None):
    """Hand-built weekly summary. st/ot/dt map day index (0=Sun) -> hours."""
    s = EmployeeProjectWeekSummary(employee_id=emp_id, project_id=key)
    for day, hours in (st or {}).items():
        s.daily_st[day] = Decimal(str(hours))
    for day, hours in (ot or {}).items():
        s.daily_ot[day] = Decimal(str(hours))
    for day, hours in (dt or {}).items():
        s.daily_dt[day] = Decimal(str(hours))
    return s


def _data(**overrides):
    fields = {
        "report": _report(),
        "employees": {"e1": _employee()},
        "projects": {"p1": _project()},
        "classifications": {"e1": _classification()},
        "rates": {"c1": _rate()},
        "detail_entries": [_detail()],
    }
    fields.update(overrides)
    return ReportData(**fields)


def _by_type(flags, flag_type):
    return [f for f in flags if f["flag_type"] == flag_type]


# ── BELOW_PREVAILING_WAGE: enrolled keys only ──────────────────────────────────


def test_below_prevailing_wage_fires_on_enrolled_key():
    # 40h at $1000 gross → $25/hr effective, below the $50 prevailing rate.
    data = _data()
    summaries = [_summary("e1", "p1", st={1: 8, 2: 8, 3: 8, 4: 8, 5: 8})]
    rows = _build_row_data(data, summaries)

    flags = generate_cpr_flags(data, summaries, rows, {})
    [flag] = _by_type(flags, "BELOW_PREVAILING_WAGE")
    assert flag["severity"] == "error"
    assert flag["project_id"] == "p1"
    assert "$25.00/hr" in flag["message"]


def test_below_prevailing_wage_skips_synthetic_keys():
    # The same underpaid hours living entirely on non-enrolled buckets must not
    # trip any compliance check (no classification/rate obligations either).
    data = _data()
    summaries = [
        _summary("e1", "bdr:p9", st={1: 8}),
        _summary("e1", "ext:reg1", st={2: 8}),
        _summary("e1", "raw:g3office", st={3: 8}),
    ]
    sources = {"bdr:p9": "6401 - Warehouse", "ext:reg1": "G3 Office", "raw:g3office": "Mystery"}

    flags = generate_cpr_flags(data, summaries, [], sources)
    assert _by_type(flags, "BELOW_PREVAILING_WAGE") == []
    assert _by_type(flags, "MISSING_CLASSIFICATION") == []
    assert _by_type(flags, "MISSING_RATE") == []


# ── NON_CP_HOURS / UNKNOWN_PROJECT ─────────────────────────────────────────────


def test_non_cp_hours_flag_per_bucket_with_hours():
    data = _data()
    summaries = [
        _summary("e1", "p1", st={1: 6}),
        _summary("e1", "bdr:p9", st={1: 4}),
        _summary("e1", "ext:reg1", st={2: 3}),
    ]
    sources = {"bdr:p9": "6401 - Warehouse", "ext:reg1": "G3 Office"}
    rows = _build_row_data(data, summaries)

    flags = generate_cpr_flags(data, summaries, rows, sources)
    non_cp = _by_type(flags, "NON_CP_HOURS")
    assert len(non_cp) == 2
    by_source = {f["project_title"]: f for f in non_cp}
    assert by_source["6401 - Warehouse"]["hours"] == "4"
    assert by_source["G3 Office"]["hours"] == "3"
    assert all(f["severity"] == "info" for f in non_cp)
    # Known non-CP buckets are informational, never unknown-project errors.
    assert _by_type(flags, "UNKNOWN_PROJECT") == []


def test_unknown_project_flag_on_raw_keys():
    data = _data()
    summaries = [
        _summary("e1", "p1", st={1: 6}),
        _summary("e1", "raw:mysteryjob", st={2: 4}),
    ]
    sources = {"raw:mysteryjob": "Mystery Job"}
    rows = _build_row_data(data, summaries)

    flags = generate_cpr_flags(data, summaries, rows, sources)
    [flag] = _by_type(flags, "UNKNOWN_PROJECT")
    assert flag["severity"] == "error"
    assert flag["hours"] == "4"
    assert "Mystery Job" in flag["message"]
    # A raw bucket is flagged as the error, not doubly as NON_CP_HOURS.
    assert _by_type(flags, "NON_CP_HOURS") == []


# ── Discrepancies include non-CP buckets ───────────────────────────────────────


def test_hours_mismatch_silent_when_full_week_totals_agree():
    # 6h enrolled + 4h non-CP = 10h — exactly the Gusto total. Before the
    # merged model this employee would have false-positived at 6h vs 10h.
    data = _data(
        detail_entries=[_detail(hours_total="10", hours_regular="10", hours_ot="0")]
    )
    summaries = [
        _summary("e1", "p1", st={1: 6}),
        _summary("e1", "bdr:p9", st={1: 4}),
    ]
    rows = _build_row_data(data, summaries)

    flags = generate_cpr_flags(data, summaries, rows, {"bdr:p9": "6401 - Warehouse"})
    assert _by_type(flags, "HOURS_MISMATCH") == []
    assert _by_type(flags, "REGULAR_HOURS_MISMATCH") == []
    assert _by_type(flags, "OT_HOURS_MISMATCH") == []


def test_hours_mismatch_fires_on_real_discrepancy():
    # Gusto says 8h; the timesheet (incl. non-CP) says 10h.
    data = _data(
        detail_entries=[_detail(hours_total="8", hours_regular="8", hours_ot="0")]
    )
    summaries = [
        _summary("e1", "p1", st={1: 6}),
        _summary("e1", "bdr:p9", st={1: 4}),
    ]
    rows = _build_row_data(data, summaries)

    flags = generate_cpr_flags(data, summaries, rows, {"bdr:p9": "6401 - Warehouse"})
    [flag] = _by_type(flags, "HOURS_MISMATCH")
    assert "(10h)" in flag["message"] and "(8h)" in flag["message"]


def test_regular_hours_mismatch_silent_across_premium_buckets():
    # 8h straight time that Gusto splits across regular/grave-shift/foreman
    # buckets. Summing only regular+regular_pay would false-positive; every
    # straight-time bucket must count toward the comparison.
    data = _data(
        detail_entries=[
            _detail(
                hours_total="8", hours_ot="0",
                hours_regular="3", hours_grave_shift="3", hours_foreman="2",
            )
        ]
    )
    summaries = [_summary("e1", "p1", st={1: 8})]
    rows = _build_row_data(data, summaries)
    flags = generate_cpr_flags(data, summaries, rows, {})
    assert _by_type(flags, "REGULAR_HOURS_MISMATCH") == []
    assert _by_type(flags, "HOURS_MISMATCH") == []


def test_regular_hours_mismatch_still_fires_on_real_gap():
    # Total agrees (8h) but the straight-time buckets only account for 6h — a
    # genuine internal inconsistency the flag must still surface.
    data = _data(
        detail_entries=[_detail(hours_total="8", hours_regular="6", hours_ot="0")]
    )
    summaries = [_summary("e1", "p1", st={1: 8})]
    rows = _build_row_data(data, summaries)
    flags = generate_cpr_flags(data, summaries, rows, {})
    assert _by_type(flags, "REGULAR_HOURS_MISMATCH")
    assert _by_type(flags, "HOURS_MISMATCH") == []


# ── Proration uses the full-week denominator ───────────────────────────────────


def test_proration_denominator_includes_non_cp_hours():
    # 6h enrolled + 4h on a non-enrolled BDR project → ratio 6/10 = 0.6, and
    # every prorated money figure follows it. Only the enrolled bucket builds
    # a certified row.
    data = _data(
        detail_entries=[
            _detail(
                gross="1000.00",
                employee_taxes_fit="100.00",
                net_pay="800.00",
            )
        ]
    )
    summaries = [
        _summary("e1", "p1", st={1: 6}),
        _summary("e1", "bdr:p9", st={1: 4}),
    ]

    [row] = _build_row_data(data, summaries)
    assert row.project.id == "p1"
    assert row.hours_on_project == Decimal("6")
    assert row.total_hours_all_projects == Decimal("10")
    assert row.prorate_ratio == Decimal("0.6")
    assert row.prorated_gross == Decimal("600.00")
    assert row.prorated_fit == Decimal("60.00")
    assert row.prorated_net == Decimal("480.00")


def test_zero_rows_message_when_everything_is_non_cp():
    data = _data()
    summaries = [
        _summary("e1", "bdr:p9", st={1: 8}),
        _summary("e1", "raw:mysteryjob", st={2: 4}),
    ]
    with pytest.raises(ValueError, match="No hours matched a Certified Payroll project"):
        _build_row_data(data, summaries)


# ── generate_all file→project mapping ──────────────────────────────────────────


def _time_entry(pid, day, hours="8"):
    d = WEEK_START + timedelta(days=day)
    return TimeEntryRec(
        employee_id="e1",
        project_id=pid,
        raw_project_number=None,
        raw_project_name=None,
        work_date=d,
        start_time=datetime.combine(d, time(7, 0)),
        total_hours=Decimal(hours),
    )


def test_generate_all_maps_every_file_to_its_projects(monkeypatch):
    # Two eComply projects + one LCP project. The aggregate files (PVW sheet /
    # eComply CSV) must be tagged with EVERY project they cover; the
    # per-project LCP CSV with exactly its one project.
    data = _data(
        projects={
            "p1": _project("p1", report_type="comply", project_number="6370"),
            "p2": _project("p2", report_type="comply", project_number="6371"),
            "p3": _project("p3", report_type="lcp_tracker", project_number="7000"),
        },
        time_entries=[_time_entry("p1", 1), _time_entry("p2", 2), _time_entry("p3", 3)],
    )
    monkeypatch.setattr(cpr_generation, "load_report_data", lambda rid: data)

    files, flags, non_cp_hours, file_projects = cpr_generation.generate_all("r1", "u1")

    assert set(files) == {
        "PVW Sheet Old School.xlsx",
        "eComply CPR Upload.csv",
        "7000 LCP CPR Upload.csv",
    }
    # Every generated file carries a project mapping, keyed identically.
    assert set(file_projects) == set(files)
    assert file_projects["PVW Sheet Old School.xlsx"] == {"p1", "p2", "p3"}
    assert file_projects["eComply CPR Upload.csv"] == {"p1", "p2"}
    assert file_projects["7000 LCP CPR Upload.csv"] == {"p3"}
