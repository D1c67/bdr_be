"""Certified Payroll weekly pipeline (services/payroll_reports + payroll_matching).

Week snapping, the one-report-per-week 409, the upload status machine and its
optimistic lock, matching semantics (enrolled-first order, registry hits, alt
names), the finalize gates and race guard, and the processed→submitted hand-off.
The parsers and the OT helper are stubbed via sys.modules fakes — these tests
exercise the service logic, not pandas. Extends the shared in-memory fake from
test_pm_workflow with real `.in_()` semantics (the CP services batch-read with
it)."""

import io
import sys
import types
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import payroll_matching, payroll_reports
from app.services.payroll_matching import (
    find_employee,
    find_project,
    match_ignored,
    rematch_report,
)
from app.services.payroll_reports import (
    build_report_detail,
    check_stale_since_finalization,
    create_report,
    delete_report,
    finalize_gate_issues,
    finalize_report,
    get_week_dates,
    process_payroll_detail,
    process_timesheet,
    submit_report,
)
from tests import test_pm_workflow as pmw
from tests.test_pm_workflow import FakeDB, install


# ── Fake extensions: real .in_() matching (base fake compares equality only) ──


class _CpQuery(pmw._Query):
    def __init__(self, db, table):
        super().__init__(db, table)
        self._in_filters = []

    def in_(self, col, vals):
        self._in_filters.append((col, list(vals)))
        return self

    def _matches(self, row):
        if not super()._matches(row):
            return False
        return all(row.get(col) in vals for col, vals in self._in_filters)


class CpFakeDB(FakeDB):
    def table(self, name):
        return _CpQuery(self, name)


def _install(monkeypatch, db, parsed_timesheet=(), parsed_detail=()):
    """Wire the fake DB into every CP module, stub storage (recording paths),
    and inject fake parser/OT modules so the lazy imports resolve without the
    concurrently-built B1/B2 packages (or pandas)."""
    install(monkeypatch, db)
    for mod in (payroll_matching, payroll_reports):
        monkeypatch.setattr(mod, "get_supabase", lambda db=db: db)

    uploads, deletes = [], []
    monkeypatch.setattr(
        payroll_reports.storage, "upload_file", lambda path, content, ct: uploads.append(path)
    )
    monkeypatch.setattr(payroll_reports.storage, "delete_file", lambda path: deletes.append(path))

    ts_mod = types.ModuleType("app.services.payroll_timesheet_parser")
    ts_mod.parse_timesheet = lambda content, filename: list(parsed_timesheet)
    monkeypatch.setitem(sys.modules, "app.services.payroll_timesheet_parser", ts_mod)
    pd_mod = types.ModuleType("app.services.payroll_detail_parser")
    pd_mod.parse_payroll_detail = lambda content, filename: list(parsed_detail)
    monkeypatch.setitem(sys.modules, "app.services.payroll_detail_parser", pd_mod)
    ot_mod = types.ModuleType("app.services.payroll_ot")
    ot_mod.round_quarter_hour = lambda hours: Decimal(str(hours))  # rounding is B2's concern
    monkeypatch.setitem(sys.modules, "app.services.payroll_ot", ot_mod)
    return SimpleNamespace(db=db, uploads=uploads, deletes=deletes)


# ── Seed data ──────────────────────────────────────────────────────────────────

T0 = "2026-07-13T00:00:00+00:00"


def _report(**over):
    row = {
        "id": "r1",
        "week_start_date": "2026-07-12",
        "week_end_date": "2026-07-18",
        "status": "draft",
        "timesheet_filename": None,
        "timesheet_storage_path": None,
        "payroll_detail_filename": None,
        "payroll_detail_storage_path": None,
        "total_hours": None,
        "total_employees": None,
        "finalized_at": None,
        "submitted_at": None,
        "created_at": T0,
        "updated_at": T0,
    }
    row.update(over)
    return row


def _processing_report(**over):
    fields = {
        "status": "processing",
        "timesheet_filename": "week.csv",
        "payroll_detail_filename": "gusto.xlsx",
    }
    fields.update(over)
    return _report(**fields)


def _entry_row(**over):
    row = {
        "id": "t1",
        "payroll_report_id": "r1",
        "employee_id": "e1",
        "is_employee_matched": True,
        "project_id": "p1",
        "is_project_matched": True,
        "raw_employee_first_name": "John",
        "raw_employee_last_name": "Smith",
        "raw_project_number": "6370",
        "raw_project_name": "6370 - Terminal 3",
        "work_date": "2026-07-13",
        "start_time": "2026-07-13T07:00:00",
        "end_time": "2026-07-13T15:30:00",
        "break_duration_minutes": 30,
        "total_hours": "8.00",
    }
    row.update(over)
    return row


def _db(**tables):
    base = {
        "cp_payroll_reports": [],
        "cp_time_entries": [],
        "cp_payroll_detail_entries": [],
        "cp_records": [],
        "cp_record_files": [],
        "cp_rates": [],
        "cp_details": [],
        "employees": [
            {
                "id": "e1",
                "first_name": "John",
                "last_name": "Smith",
                "alt_ee_name": None,
                "classification_id": "c1",
                "updated_at": T0,
            }
        ],
        "cp_classifications": [
            {"id": "c1", "code": "EL", "name": "Electrician", "is_field": True, "updated_at": T0}
        ],
        "projects": [
            {"id": "p1", "name": "Terminal 3", "number": "6370", "cp_enrolled_at": T0,
             "updated_at": T0},
            {"id": "p2", "name": "Warehouse", "number": "9001", "cp_enrolled_at": None,
             "updated_at": T0},
        ],
        "cp_ignored_projects": [
            {"id": "ig1", "raw_number": None, "raw_name": "G3 Office", "shift_type": "regular"}
        ],
        "audit_log": [],
    }
    base.update(tables)
    return CpFakeDB(base)


def _ts_entry(first="John", last="Smith", number="6370", name="6370 - Terminal 3", hours="8"):
    return SimpleNamespace(
        employee_id=None,
        first_name=first,
        last_name=last,
        work_date=date(2026, 7, 13),
        start_time=datetime(2026, 7, 13, 7, 0),
        end_time=datetime(2026, 7, 13, 15, 30),
        break_total_minutes=30,
        total_hours=Decimal(hours),
        customer=None,
        project_number=number,
        project_name=name,
        subproject_1_number=None,
        subproject_1_name=None,
        cost_code=None,
        cost_code_desc=None,
        description=None,
    )


def _detail_entry(name="SMITH, JOHN M"):
    return SimpleNamespace(
        employee_name=name,
        pay_date=date(2026, 7, 17),
        time_period="Jul 12 - Jul 18",
        hours_total=Decimal("40"),
        gross_pay_total=Decimal("2000.00"),
        net_pay=Decimal("1500.00"),
    )


# ── Week snap math ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected_start"),
    [
        (date(2026, 7, 15), date(2026, 7, 12)),  # Wednesday → that week's Sunday
        (date(2026, 7, 12), date(2026, 7, 12)),  # Sunday is already the start
        (date(2026, 7, 18), date(2026, 7, 12)),  # Saturday stays in the same week
    ],
)
def test_get_week_dates_snaps_to_sun_sat(given, expected_start):
    start, end = get_week_dates(given)
    assert (start, end) == (expected_start, expected_start + timedelta(days=6))


# ── Create + duplicate week ────────────────────────────────────────────────────


def test_create_report_snaps_week(monkeypatch):
    _install(monkeypatch, _db())
    row = create_report(date(2026, 7, 15), "u1")
    assert row["week_start_date"] == "2026-07-12"
    assert row["week_end_date"] == "2026-07-18"
    assert row["status"] == "draft"
    assert row["created_by"] == "u1"


def test_create_report_duplicate_week_409_carries_existing_id(monkeypatch):
    _install(monkeypatch, _db(cp_payroll_reports=[_report()]))
    with pytest.raises(HTTPException) as ei:
        create_report(date(2026, 7, 16), "u1")  # any date in the taken week
    assert ei.value.status_code == 409
    assert ei.value.detail["message"] == "A report for that week already exists"
    assert ei.value.detail["existing_id"] == "r1"


def test_create_report_unique_violation_race_is_409(monkeypatch):
    env = _install(monkeypatch, _db())
    env.db.raise_on_insert["cp_payroll_reports"] = Exception(
        'duplicate key value violates unique constraint "cp_payroll_reports_week_unique_idx"'
    )
    with pytest.raises(HTTPException) as ei:
        create_report(date(2026, 7, 15), "u1")
    assert ei.value.status_code == 409
    assert ei.value.detail["message"] == "A report for that week already exists"


# ── Upload status machine ──────────────────────────────────────────────────────


def test_timesheet_only_awaits_payroll_detail(monkeypatch):
    env = _install(
        monkeypatch, _db(cp_payroll_reports=[_report()]), parsed_timesheet=[_ts_entry()]
    )
    summary = process_timesheet(env.db.tables["cp_payroll_reports"][0], "week.csv", b"x", "u1")
    assert summary["status"] == "awaiting_payroll_detail"
    assert summary["total_entries"] == 1
    assert summary["matched_employees"] == 1
    assert summary["unmatched_employees"] == []

    row = env.db.tables["cp_payroll_reports"][0]
    assert row["status"] == "awaiting_payroll_detail"
    assert row["timesheet_filename"] == "week.csv"
    assert row["timesheet_storage_path"].startswith("payroll/reports/r1/uploads/")
    assert row["total_hours"] == "8.00"
    assert row["total_employees"] == 1
    assert env.uploads == [row["timesheet_storage_path"]]

    [entry] = env.db.tables["cp_time_entries"]
    assert (entry["employee_id"], entry["is_employee_matched"]) == ("e1", True)
    assert (entry["project_id"], entry["is_project_matched"]) == ("p1", True)
    assert entry["total_hours"] == "8"


def test_timesheet_after_detail_sets_processing(monkeypatch):
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_report(payroll_detail_filename="gusto.xlsx")]),
        parsed_timesheet=[_ts_entry()],
    )
    summary = process_timesheet(env.db.tables["cp_payroll_reports"][0], "week.csv", b"x", "u1")
    assert summary["status"] == "processing"
    assert env.db.tables["cp_payroll_reports"][0]["status"] == "processing"


def test_timesheet_parse_failure_stamps_nothing(monkeypatch):
    # Parsing happens BEFORE any persistence: a malformed upload must not leave
    # the report claiming a timesheet (which would let an empty report be filed).
    env = _install(monkeypatch, _db(cp_payroll_reports=[_report()]))

    def _boom(content, filename):
        raise ValueError("malformed CSV")

    sys.modules["app.services.payroll_timesheet_parser"].parse_timesheet = _boom
    with pytest.raises(HTTPException) as ei:
        process_timesheet(env.db.tables["cp_payroll_reports"][0], "week.csv", b"x", "u1")
    assert ei.value.status_code == 400
    row = env.db.tables["cp_payroll_reports"][0]
    assert row["timesheet_filename"] is None
    assert row["timesheet_storage_path"] is None
    assert row["status"] == "draft"
    assert env.uploads == []  # nothing was stored


def test_finalize_gate_blocks_report_with_no_entries(monkeypatch):
    # Both files present but zero time entries — must not be finalizable (else it
    # clears every other gate and can be filed empty).
    env = _install(monkeypatch, _db(cp_payroll_reports=[_processing_report()]))
    issues = finalize_gate_issues(env.db.tables["cp_payroll_reports"][0])
    assert any("No time entries" in i for i in issues)


def test_rematch_processed_is_409(monkeypatch):
    # Matching closes at finalize: a 'processed' report has generated CPR files
    # bound to the current matching and must not be silently re-matched.
    _install(monkeypatch, _db(cp_payroll_reports=[_report(status="processed")]))
    with pytest.raises(HTTPException) as ei:
        rematch_report("r1")
    assert ei.value.status_code == 409


def test_timesheet_optimistic_lock_conflict_409_sweeps_orphan(monkeypatch):
    env = _install(
        monkeypatch, _db(cp_payroll_reports=[_report()]), parsed_timesheet=[_ts_entry()]
    )
    env.db.update_returns_empty.add("cp_payroll_reports")
    with pytest.raises(HTTPException) as ei:
        process_timesheet(env.db.tables["cp_payroll_reports"][0], "week.csv", b"x", "u1")
    assert ei.value.status_code == 409
    assert ei.value.detail == "cp_report_conflict"
    assert env.deletes == env.uploads  # the just-stored object was removed
    assert env.db.tables["cp_time_entries"] == []


def test_timesheet_reupload_replaces_entries(monkeypatch):
    stale = _entry_row(id="old", raw_employee_first_name="Ghost")
    other = _entry_row(id="keep", payroll_report_id="r2")
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_report()], cp_time_entries=[stale, other]),
        parsed_timesheet=[_ts_entry()],
    )
    process_timesheet(env.db.tables["cp_payroll_reports"][0], "week.csv", b"x", "u1")
    ids = [e["id"] for e in env.db.tables["cp_time_entries"]]
    assert "old" not in ids
    assert "keep" in ids
    assert len(ids) == 2  # the other report's row + the fresh one


def test_detail_only_awaits_timesheet(monkeypatch):
    env = _install(
        monkeypatch, _db(cp_payroll_reports=[_report()]), parsed_detail=[_detail_entry()]
    )
    summary = process_payroll_detail(
        env.db.tables["cp_payroll_reports"][0], "gusto.xlsx", b"x", "u1"
    )
    assert summary["status"] == "awaiting_timesheet"
    assert summary["matched_employees"] == ["SMITH, JOHN M"]  # Gusto-format name resolved
    row = env.db.tables["cp_payroll_reports"][0]
    assert row["status"] == "awaiting_timesheet"
    assert row["payroll_detail_filename"] == "gusto.xlsx"
    [detail] = env.db.tables["cp_payroll_detail_entries"]
    assert (detail["employee_id"], detail["is_employee_matched"]) == ("e1", True)
    assert detail["hours_total"] == "40"
    assert detail["gross_pay_total"] == "2000.00"


def test_detail_after_timesheet_sets_processing(monkeypatch):
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_report(timesheet_filename="week.csv")]),
        parsed_detail=[_detail_entry()],
    )
    summary = process_payroll_detail(
        env.db.tables["cp_payroll_reports"][0], "gusto.xlsx", b"x", "u1"
    )
    assert summary["status"] == "processing"


# ── Matching semantics ─────────────────────────────────────────────────────────


def test_registry_hit_is_not_unmatched_and_gets_no_project(monkeypatch):
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_report()]),
        parsed_timesheet=[_ts_entry(number=None, name="G3 Office")],
    )
    summary = process_timesheet(env.db.tables["cp_payroll_reports"][0], "week.csv", b"x", "u1")
    assert summary["unmatched_projects"] == []
    [entry] = env.db.tables["cp_time_entries"]
    assert entry["project_id"] is None
    assert entry["is_project_matched"] is False


def test_unknown_project_is_unmatched(monkeypatch):
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_report()]),
        parsed_timesheet=[_ts_entry(number=None, name="Mystery Job")],
    )
    summary = process_timesheet(env.db.tables["cp_payroll_reports"][0], "week.csv", b"x", "u1")
    assert summary["unmatched_projects"] == ["Mystery Job"]


def test_find_project_enrolled_wins_over_non_enrolled():
    projects = [
        {"id": "a", "name": "Old Job", "number": "6370", "cp_enrolled_at": None},
        {"id": "b", "name": "Live Job", "number": "6370", "cp_enrolled_at": T0},
    ]
    assert find_project("6370", None, projects)["id"] == "b"


def test_find_project_extracted_number_then_title():
    projects = [{"id": "a", "name": "Terminal 3", "number": "6370", "cp_enrolled_at": None}]
    assert find_project(None, "6370 - Terminal Building", projects)["id"] == "a"
    assert find_project(None, "  terminal 3 ", projects)["id"] == "a"
    assert find_project(None, "Terminal 9", projects) is None


def test_find_employee_nickname_and_alt_name():
    employees = [
        {"id": "e1", "first_name": "Bernard", "last_name": "Diaz", "alt_ee_name": None},
        {"id": "e2", "first_name": "Jose", "last_name": "Ruiz", "alt_ee_name": "Pepe Ruiz"},
    ]
    assert find_employee("Bernard (Bernie)", "Diaz", employees)["id"] == "e1"
    assert find_employee("Pepe", "Ruiz", employees)["id"] == "e2"
    assert find_employee("Ghost", "Nobody", employees) is None


def test_match_ignored_by_number_then_name():
    ignored = [{"id": "i1", "raw_number": "999", "raw_name": "Shop Time"}]
    assert match_ignored(" 999 ", None, ignored)["id"] == "i1"
    assert match_ignored(None, "shop time", ignored)["id"] == "i1"
    assert match_ignored("1", "Office", ignored) is None


def test_rematch_resolves_new_employee_and_updates_totals(monkeypatch):
    entries = [
        _entry_row(
            id="t1",
            employee_id=None,
            is_employee_matched=False,
            raw_employee_first_name="Maria",
            raw_employee_last_name="Lopez",
        )
    ]
    db = _db(cp_payroll_reports=[_processing_report()], cp_time_entries=entries)
    db.tables["employees"].append(
        {"id": "e9", "first_name": "Maria", "last_name": "Lopez", "alt_ee_name": None,
         "classification_id": None, "updated_at": T0}
    )
    env = _install(monkeypatch, db)
    summary = rematch_report("r1")
    assert summary["unmatched_employees"] == []
    [entry] = env.db.tables["cp_time_entries"]
    assert (entry["employee_id"], entry["is_employee_matched"]) == ("e9", True)
    assert env.db.tables["cp_payroll_reports"][0]["total_employees"] == 1


def test_rematch_submitted_is_409(monkeypatch):
    _install(monkeypatch, _db(cp_payroll_reports=[_report(status="submitted")]))
    with pytest.raises(HTTPException) as ei:
        rematch_report("r1")
    assert ei.value.status_code == 409


def test_rematch_missing_report_is_404(monkeypatch):
    _install(monkeypatch, _db())
    with pytest.raises(HTTPException) as ei:
        rematch_report("ghost")
    assert ei.value.status_code == 404


# ── Finalize gates ─────────────────────────────────────────────────────────────


def test_finalize_gates_flag_missing_uploads(monkeypatch):
    env = _install(monkeypatch, _db(cp_payroll_reports=[_report()]))
    issues = finalize_gate_issues(env.db.tables["cp_payroll_reports"][0])
    assert "Timesheet has not been uploaded" in issues
    assert "Payroll detail has not been uploaded" in issues


def test_finalize_gates_count_unmatched_and_unknown(monkeypatch):
    entries = [
        _entry_row(),
        _entry_row(id="t2", employee_id=None, is_employee_matched=False),
        _entry_row(id="t3", project_id=None, is_project_matched=False,
                   raw_project_number=None, raw_project_name="Mystery Job"),
        # Registry hit: intentionally non-CP, never blocks.
        _entry_row(id="t4", project_id=None, is_project_matched=False,
                   raw_project_number=None, raw_project_name="G3 Office"),
    ]
    env = _install(
        monkeypatch, _db(cp_payroll_reports=[_processing_report()], cp_time_entries=entries)
    )
    issues = finalize_gate_issues(env.db.tables["cp_payroll_reports"][0])
    assert issues == [
        "1 time entry with unmatched employees",
        "1 time entry with unknown projects",
    ]


def test_finalize_gates_clean(monkeypatch):
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_processing_report()], cp_time_entries=[_entry_row()]),
    )
    assert finalize_gate_issues(env.db.tables["cp_payroll_reports"][0]) == []


def test_finalize_blocked_400_carries_issues(monkeypatch):
    env = _install(
        monkeypatch,
        _db(
            cp_payroll_reports=[_processing_report(payroll_detail_filename=None)],
            cp_time_entries=[_entry_row()],
        ),
    )
    with pytest.raises(HTTPException) as ei:
        finalize_report(env.db.tables["cp_payroll_reports"][0], "u1")
    assert ei.value.status_code == 400
    assert ei.value.detail["message"] == "Report is not ready to finalize"
    assert ei.value.detail["issues"] == ["Payroll detail has not been uploaded"]
    assert env.db.tables["cp_payroll_reports"][0]["status"] == "processing"


def test_finalize_requires_processing_status(monkeypatch):
    env = _install(monkeypatch, _db(cp_payroll_reports=[_report(status="draft")]))
    with pytest.raises(HTTPException) as ei:
        finalize_report(env.db.tables["cp_payroll_reports"][0], "u1")
    assert ei.value.status_code == 409


def test_finalize_race_is_409(monkeypatch):
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_processing_report()], cp_time_entries=[_entry_row()]),
    )
    env.db.update_returns_empty.add("cp_payroll_reports")  # conditioned update loses
    with pytest.raises(HTTPException) as ei:
        finalize_report(env.db.tables["cp_payroll_reports"][0], "u1")
    assert ei.value.status_code == 409


def test_finalize_ok_sets_processed_and_stamps(monkeypatch):
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_processing_report()], cp_time_entries=[_entry_row()]),
    )
    row = finalize_report(env.db.tables["cp_payroll_reports"][0], "u1")
    assert row["status"] == "processed"
    assert row["finalized_by"] == "u1"
    assert row["finalized_at"]


# ── Submit ─────────────────────────────────────────────────────────────────────


def test_submit_requires_processed(monkeypatch):
    env = _install(monkeypatch, _db(cp_payroll_reports=[_processing_report()]))
    with pytest.raises(HTTPException) as ei:
        submit_report(env.db.tables["cp_payroll_reports"][0], "u1")
    assert ei.value.status_code == 409


def test_submit_ok(monkeypatch):
    env = _install(
        monkeypatch,
        _db(cp_payroll_reports=[_processing_report(status="processed", finalized_at=T0)]),
    )
    row = submit_report(env.db.tables["cp_payroll_reports"][0], "u1")
    assert row["status"] == "submitted"
    assert row["submitted_by"] == "u1"
    assert row["submitted_at"]


# ── Stale-since-finalization ───────────────────────────────────────────────────


def test_stale_after_employee_edit(monkeypatch):
    report = _processing_report(status="processed", finalized_at="2026-07-14T00:00:00+00:00")
    db = _db(cp_payroll_reports=[report], cp_time_entries=[_entry_row()])
    env = _install(monkeypatch, db)
    assert check_stale_since_finalization(report) == []  # everything predates finalize
    env.db.tables["employees"][0]["updated_at"] = "2026-07-15T00:00:00+00:00"
    assert check_stale_since_finalization(report) == ["employees"]


# ── The GET /{id} payload ──────────────────────────────────────────────────────


def test_report_detail_buckets_and_flags(monkeypatch):
    entries = [
        _entry_row(),  # enrolled project → certified bucket
        _entry_row(id="t2", project_id="p2", raw_project_number="9001",
                   raw_project_name="Warehouse", total_hours="4.00"),  # non-enrolled BDR job
        _entry_row(id="t3", project_id=None, is_project_matched=False,
                   raw_project_number=None, raw_project_name="G3 Office", total_hours="2.00"),
        _entry_row(id="t4", project_id=None, is_project_matched=False,
                   raw_project_number=None, raw_project_name="Mystery Job", total_hours="1.00"),
    ]
    db = _db(
        cp_payroll_reports=[_processing_report()],
        cp_time_entries=entries,
        cp_records=[
            {"id": "cr1", "payroll_report_id": "r1", "revision_number": 0, "created_at": T0}
        ],
        cp_record_files=[
            {"id": "cf1", "record_id": "cr1", "filename": "x.xlsx",
             "storage_path": "payroll/reports/r1/cpr/cr1/x.xlsx",
             "content_type": "application/vnd.ms-excel", "size_bytes": 10}
        ],
    )
    env = _install(monkeypatch, db)
    detail = build_report_detail(env.db.tables["cp_payroll_reports"][0])

    assert len(detail["time_entries"]) == 4
    first = detail["time_entries"][0]
    assert first["employee_name"] == "John Smith"
    assert (first["classification_code"], first["is_field"]) == ("EL", True)
    assert (first["project_number"], first["cp_enrolled"]) == ("6370", True)

    assert detail["unmatched_employees"] == []
    # Registry hit (G3 Office) excluded; only the true unknown shows.
    assert detail["unmatched_projects"] == [
        {"raw_number": None, "raw_name": "Mystery Job", "entry_count": 1}
    ]

    [bucket] = detail["non_cp_hours"]
    assert bucket["employee_id"] == "e1"
    assert bucket["employee_name"] == "John Smith"
    assert bucket["hours"] == "7.00"
    assert bucket["sources"] == ["G3 Office", "Mystery Job", "Warehouse"]

    assert detail["stale_reasons"] == []  # not finalized
    assert detail["finalize_issues"] == ["1 time entry with unknown projects"]

    [record] = detail["records"]
    assert [f["filename"] for f in record["files"]] == ["x.xlsx"]


# ── persist_record file→project tagging ───────────────────────────────────────


def test_persist_record_tags_files_with_their_projects(monkeypatch):
    """An aggregate file (eComply) lands in cp_record_file_projects once per
    covered project; a per-project LCP file exactly once. Correlation must use
    the ORIGINAL filename key even though the stored name carries the
    'Revised ' revision prefix (one cp_records row is pre-seeded)."""
    from app.services import cpr_generation

    db = _db(cp_records=[{"id": "cr0", "payroll_report_id": "r1", "revision_number": 0}])
    env = _install(monkeypatch, db)
    monkeypatch.setattr(cpr_generation, "get_supabase", lambda: env.db)

    files = {
        "eComply CPR Upload.csv": io.BytesIO(b"aggregate"),
        "6370 LCP CPR Upload.csv": io.BytesIO(b"single"),
    }
    file_projects = {
        "eComply CPR Upload.csv": {"p1", "p2"},
        "6370 LCP CPR Upload.csv": {"p1"},
    }
    result = cpr_generation.persist_record(
        "r1", files, [], None, "u1", file_projects=file_projects
    )

    assert result["record"]["revision_number"] == 1  # → "Revised " prefix
    by_name = {f["filename"]: f for f in result["files"]}
    assert set(by_name) == {
        "Revised eComply CPR Upload.csv",
        "Revised 6370 LCP CPR Upload.csv",
    }

    agg_id = by_name["Revised eComply CPR Upload.csv"]["id"]
    lcp_id = by_name["Revised 6370 LCP CPR Upload.csv"]["id"]
    tagged = {
        (r["record_file_id"], r["project_id"])
        for r in env.db.tables["cp_record_file_projects"]
    }
    assert tagged == {(agg_id, "p1"), (agg_id, "p2"), (lcp_id, "p1")}


def test_persist_record_without_file_projects_writes_no_tags(monkeypatch):
    from app.services import cpr_generation

    env = _install(monkeypatch, _db())
    monkeypatch.setattr(cpr_generation, "get_supabase", lambda: env.db)

    result = cpr_generation.persist_record(
        "r1", {"eComply CPR Upload.csv": io.BytesIO(b"x")}, [], None, "u1"
    )
    assert [f["filename"] for f in result["files"]] == ["eComply CPR Upload.csv"]
    assert env.db.tables.get("cp_record_file_projects", []) == []


# ── Delete ─────────────────────────────────────────────────────────────────────


def test_delete_report_sweeps_storage(monkeypatch):
    report = _processing_report(
        timesheet_storage_path="payroll/reports/r1/uploads/a-week.csv",
        payroll_detail_storage_path="payroll/reports/r1/uploads/b-gusto.xlsx",
    )
    db = _db(
        cp_payroll_reports=[report],
        cp_records=[{"id": "cr1", "payroll_report_id": "r1"}],
        cp_record_files=[
            {"id": "cf1", "record_id": "cr1",
             "storage_path": "payroll/reports/r1/cpr/cr1/x.xlsx"}
        ],
    )
    env = _install(monkeypatch, db)
    delete_report(env.db.tables["cp_payroll_reports"][0], "u1")
    assert env.db.tables["cp_payroll_reports"] == []
    assert set(env.deletes) == {
        "payroll/reports/r1/uploads/a-week.csv",
        "payroll/reports/r1/uploads/b-gusto.xlsx",
        "payroll/reports/r1/cpr/cr1/x.xlsx",
    }


def test_delete_submitted_report_is_409(monkeypatch):
    """A submitted report is a filed prevailing-wage record — deletion is refused
    (same submitted-immutability invariant as rematch), so the row and its stored
    CPR files survive."""
    report = _report(status="submitted", timesheet_storage_path="payroll/reports/r1/uploads/x.csv")
    env = _install(monkeypatch, _db(cp_payroll_reports=[report]))
    with pytest.raises(HTTPException) as ei:
        delete_report(env.db.tables["cp_payroll_reports"][0], "u1")
    assert ei.value.status_code == 409
    assert env.db.tables["cp_payroll_reports"] == [report]  # nothing deleted
    assert env.deletes == []  # no storage object swept
