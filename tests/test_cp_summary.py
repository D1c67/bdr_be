"""services/cp_summary and `GET /payroll/reports/summary`
(docs/ASSISTANT_PHASE4_PLAN.md 3.13): a certified payroll week with totals
only. No names, wages, rates, paths or user ids at any depth, and per-project
cells under three people never show alone."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import payroll_reports as rt
from app.services import cp_summary, payroll_matching, payroll_reports

WEEK = date(2026, 10, 18)  # a Sunday
FORBIDDEN_KEYS = {
    "ssn", "wage", "gross", "net", "rate", "storage_path", "filename", "employee_name",
    "first_name", "last_name", "raw_employee_first_name", "raw_employee_last_name", "employee_id",
}


class _Q:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters = []
        self._order = None
        self._range = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals)))
        return self

    def limit(self, *_a, **_k):
        return self

    def order(self, col, **_k):
        self._order = col
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def execute(self):
        rows = [
            r for r in self.db.tables.get(self.table, [])
            if all((k == "eq" and r.get(c) == v) or (k == "in" and r.get(c) in v) for k, c, v in self.filters)
        ]
        if self._order:
            rows.sort(key=lambda r: str(r.get(self._order)))
        if self._range:
            rows = rows[self._range[0] : self._range[1] + 1]
        return SimpleNamespace(data=[dict(r) for r in rows])


class FakeSB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self, name)


REPORT = {
    "id": "rep-1", "week_start_date": "2026-10-18", "week_end_date": "2026-10-24", "status": "processing",
    "timesheet_filename": "SECRET-timesheet.xlsx", "timesheet_storage_path": "payroll/secret/path",
    "payroll_detail_filename": "SECRET-detail.xlsx", "payroll_detail_storage_path": "payroll/secret/p2",
    "total_hours": "999", "total_employees": 6, "finalized_at": None, "submitted_at": None,
    "created_by": "user-secret", "finalized_by": None, "submitted_by": None,
}


def _entry(i, emp, proj, hours, *, matched=True, first=None, last=None):
    return {
        "id": f"e{i:04}", "payroll_report_id": "rep-1", "employee_id": emp, "project_id": proj,
        "total_hours": hours, "is_employee_matched": matched,
        "raw_employee_first_name": first or f"First{emp}", "raw_employee_last_name": last or f"Last{emp}",
        "raw_project_number": None, "raw_project_name": None,
    }


def _projects():
    enrolled = "2026-01-01T00:00:00+00:00"
    return [
        {"id": "A", "number": "26.1", "name": "Alpha", "cp_enrolled_at": enrolled},
        {"id": "B", "number": "26.2", "name": "Beta", "cp_enrolled_at": enrolled},
        {"id": "C", "number": "26.3", "name": "Gamma", "cp_enrolled_at": enrolled},
        {"id": "D", "number": "26.4", "name": "Delta", "cp_enrolled_at": enrolled},
        {"id": "N", "number": "26.9", "name": "Not enrolled", "cp_enrolled_at": None},
    ]


def _install(monkeypatch, entries, report=None, records=None, projects=None):
    sb = FakeSB({
        "cp_payroll_reports": [dict(report or REPORT)],
        "cp_time_entries": entries,
        "projects": projects or _projects(),
        "cp_records": records or [],
        "cp_ignored_projects": [],
    })
    for mod in (payroll_reports, payroll_matching):
        monkeypatch.setattr(mod, "get_supabase", lambda: sb)
    return sb


def _sub(summary):
    return (
        Decimal(summary["total_hours"])
        - sum((Decimal(r["hours"]) for r in summary["hours_by_project"]), Decimal("0"))
        - Decimal(summary["other_hours"])
    )


def _walk_keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def test_no_report_for_the_week_is_none(monkeypatch):
    sb = _install(monkeypatch, [])
    sb.tables["cp_payroll_reports"] = []
    assert cp_summary.week_summary(sb, WEEK) is None


def test_projects_with_three_or_more_people_show_and_the_rest_merge(monkeypatch):
    entries = []
    n = 0
    for emp in ("1", "2", "3", "4"):  # A: 4 people
        n += 1
        entries.append(_entry(n, emp, "A", "8.00"))
    for emp in ("1", "2"):  # B: 2 people
        n += 1
        entries.append(_entry(n, emp, "B", "4.25"))
    n += 1
    entries.append(_entry(n, "5", "C", "2.50"))  # C: 1 person (merged with B => 3 people total)
    n += 1
    entries.append(_entry(n, "1", "N", "6.00"))  # not enrolled
    n += 1
    entries.append(_entry(n, None, None, "1.25", matched=False, first="Zed", last="Nobody"))  # unknown job
    sb = _install(monkeypatch, entries)
    out = cp_summary.week_summary(sb, WEEK)
    shown = {r["name"]: r for r in out["hours_by_project"]}
    assert shown["Alpha"] == {"number": "26.1", "name": "Alpha", "hours": "32.00", "employees": 4}
    assert shown["Other enrolled projects"] == {
        "number": None, "name": "Other enrolled projects", "hours": "11.00", "employees": 3,
    }
    assert "Beta" not in shown and "Gamma" not in shown
    assert out["other_hours"] == "7.25"  # not enrolled 6.00 + unknown 1.25
    assert out["total_hours"] == "50.25"
    assert _sub(out) == 0


def test_a_merged_row_under_the_minimum_joins_other_hours(monkeypatch):
    entries = [_entry(1, "1", "A", "8.00"), _entry(2, "2", "A", "8.00"), _entry(3, "3", "A", "8.00"),
               _entry(4, "1", "B", "4.00"), _entry(5, "2", "C", "3.00")]  # B+C: 2 people
    out = cp_summary.week_summary(_install(monkeypatch, entries), WEEK)
    assert [r["name"] for r in out["hours_by_project"]] == ["Alpha"]
    assert out["other_hours"] == "7.00"
    assert _sub(out) == 0


def test_a_single_small_cell_never_shows_alone(monkeypatch):
    entries = [_entry(1, "1", "A", "8.00"), _entry(2, "1", "B", "5.50")]
    out = cp_summary.week_summary(_install(monkeypatch, entries), WEEK)
    assert out["hours_by_project"] == []
    assert out["other_hours"] == "13.50" and out["total_hours"] == "13.50"
    assert _sub(out) == 0


def test_subtraction_is_zero_for_a_spread_of_fixtures(monkeypatch):
    import random

    rng = random.Random(7)
    for trial in range(25):
        entries = []
        for i in range(rng.randint(0, 40)):
            proj = rng.choice(["A", "B", "C", "D", "N", None])
            emp = str(rng.randint(1, 7))
            entries.append(_entry(i, emp, proj, f"{rng.choice([0.25, 1, 4.5, 8, 10.75]):.2f}"))
        out = cp_summary.week_summary(_install(monkeypatch, entries), WEEK)
        assert _sub(out) == 0, trial


def test_no_forbidden_key_or_secret_value_at_any_depth(monkeypatch):
    entries = [_entry(i, str(i % 4), "A", "8.00") for i in range(8)]
    entries.append(_entry(99, None, None, "2.00", matched=False, first="Hidden", last="Person"))
    out = cp_summary.week_summary(_install(monkeypatch, entries), WEEK)
    keys = set(_walk_keys(out))
    assert not (keys & FORBIDDEN_KEYS), keys & FORBIDDEN_KEYS
    assert not any(k.endswith("_by") for k in keys)
    blob = json.dumps(out)
    for secret in ("SECRET", "user-secret", "Hidden", "Person", "First1", "Last1"):
        assert secret not in blob
    assert out["timesheet_uploaded"] is True and out["payroll_detail_uploaded"] is True


def test_top_level_keys_are_exactly_the_contract(monkeypatch):
    out = cp_summary.week_summary(_install(monkeypatch, [_entry(1, "1", "A", "1.00")]), WEEK)
    assert set(out) == {
        "id", "week_start", "week_end", "status", "timesheet_uploaded", "payroll_detail_uploaded",
        "total_hours", "total_employees", "entry_count", "unmatched_employee_entries",
        "unmatched_project_entries", "finalize_issues", "stale_reasons", "finalized_at",
        "submitted_at", "record_count", "latest_revision", "hours_by_project", "other_hours",
    }
    assert out["week_start"] == "2026-10-18" and out["entry_count"] == 1


def test_unmatched_counts_and_finalize_issues_for_a_processing_report(monkeypatch):
    entries = [_entry(1, "1", "A", "1.00"), _entry(2, None, None, "2.00", matched=False)]
    out = cp_summary.week_summary(_install(monkeypatch, entries), WEEK)
    assert out["unmatched_employee_entries"] == 1 and out["unmatched_project_entries"] == 1
    assert out["finalize_issues"] == [
        "1 time entry with unmatched employees",
        "1 time entry with unknown projects",
    ]
    assert out["stale_reasons"] == []


def test_ignored_registry_projects_are_not_unknown(monkeypatch):
    entries = [{**_entry(1, "1", None, "2.00"), "raw_project_number": "99", "raw_project_name": "Shop"}]
    sb = _install(monkeypatch, entries)
    sb.tables["cp_ignored_projects"] = [{"raw_number": "99", "raw_name": "Shop"}]
    out = cp_summary.week_summary(sb, WEEK)
    assert out["unmatched_project_entries"] == 0
    assert out["other_hours"] == "2.00"


def test_finalized_report_carries_stale_reasons_and_revisions(monkeypatch):
    report = {**REPORT, "status": "processed", "finalized_at": "2026-10-20T00:00:00+00:00"}
    sb = _install(
        monkeypatch, [_entry(1, "1", "A", "1.00")], report=report,
        records=[
            {"payroll_report_id": "rep-1", "revision_number": 0},
            {"payroll_report_id": "rep-1", "revision_number": 2},
            {"payroll_report_id": "other", "revision_number": 9},
        ],
    )
    sb.tables["employees"] = [{"id": "1", "classification_id": None, "updated_at": "2026-10-21T00:00:00+00:00"}]
    out = cp_summary.week_summary(sb, WEEK)
    assert out["stale_reasons"] == ["employees"]
    assert out["finalize_issues"] == []
    assert out["record_count"] == 2 and out["latest_revision"] == 2


def test_entries_are_paged_past_the_postgrest_cap(monkeypatch):
    entries = [_entry(i, str(i % 5), "A", "1.00") for i in range(2300)]
    out = cp_summary.week_summary(_install(monkeypatch, entries), WEEK)
    assert out["entry_count"] == 2300 and out["total_hours"] == "2300.00"
    assert out["hours_by_project"][0]["employees"] == 5


# -- route --------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    from app.main import app

    state = SimpleNamespace(role=Role.EXECUTIVE)
    sb = _install(monkeypatch, [_entry(i, str(i % 4), "A", "2.00") for i in range(8)])
    monkeypatch.setattr(rt, "get_supabase", lambda: sb)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id="u1", email="u@g3.com", role=state.role, is_active=True
    )
    c = TestClient(app, raise_server_exceptions=True)
    c.state = state
    c.sb = sb
    yield c
    app.dependency_overrides.pop(get_current_user, None)


def test_summary_route_resolves_before_the_report_id_route():
    from app.main import app

    scope = {"type": "http", "method": "GET", "path": "/payroll/reports/summary", "root_path": "",
             "query_string": b"", "headers": []}
    first = next(r for r in app.routes if r.matches(scope)[0] == Match.FULL)
    assert first.endpoint is rt.week_summary


def test_handler_is_plain_def():
    import inspect

    assert not inspect.iscoroutinefunction(rt.week_summary)


def test_route_returns_the_summary_and_snaps_any_date_to_its_week(client):
    for day in ("2026-10-18", "2026-10-21", "2026-10-24"):
        r = client.get(f"/payroll/reports/summary?week_start={day}")
        assert r.status_code == 200, day
        assert r.json()["week_start"] == "2026-10-18"
    assert r.json()["total_hours"] == "16.00"


def test_missing_report_is_404_no_report(client):
    client.sb.tables["cp_payroll_reports"] = []
    r = client.get("/payroll/reports/summary?week_start=2026-10-18")
    assert r.status_code == 404 and r.json()["detail"] == {"code": "no_report"}


def test_bad_or_missing_week_start_is_422(client):
    assert client.get("/payroll/reports/summary").status_code == 422
    assert client.get("/payroll/reports/summary?week_start=nope").status_code == 422


def test_cp_read_roles_including_accountant_read_and_estimator_is_refused(client):
    for role in (Role.ACCOUNTANT, Role.EXECUTIVE, Role.IT_ADMIN):
        client.state.role = role
        assert client.get("/payroll/reports/summary?week_start=2026-10-18").status_code == 200
    client.state.role = Role.ESTIMATOR
    assert client.get("/payroll/reports/summary?week_start=2026-10-18").status_code == 403
