"""PM field router — milestones, daily logs, RFIs, manpower.

Covers the per-project rfi_number sequencing (including the recompute-once race
path), the answer→answered status convenience, manpower↔daily-log project
integrity, project-scoped 404s (no cross-project id probing), and the date
window filters. The Supabase client is faked with the in-memory builder from
test_reverify, extended with delete/gte/lte/order/limit and the rfis
(project_id, rfi_number) unique constraint.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.schemas import (
    DailyLogIn,
    DailyLogUpdate,
    ManpowerIn,
    MilestoneIn,
    MilestoneUpdate,
    RFIIn,
    RFIUpdate,
)
from app.routers import pm_field
from app.services import pm as pm_svc


# ── Fake Supabase ─────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._cmp_filters = []  # (col, op, val) for gte/lte
        self._single = False
        self._orders = []
        self._limit = None

    # builders
    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def gte(self, col, val):
        self._cmp_filters.append((col, "ge", val))
        return self

    def lte(self, col, val):
        self._cmp_filters.append((col, "le", val))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, col, desc=False, **k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    # execution
    def _matches(self, row):
        if not all(row.get(c) == v for c, v in self._filters):
            return False
        for col, op, val in self._cmp_filters:
            rv = row.get(col)
            if rv is None:
                return False
            if op == "ge" and not rv >= val:
                return False
            if op == "le" and not rv <= val:
                return False
        return True

    def _check_rfi_unique(self, payload):
        for r in self.db.tables.get("rfis", []):
            if (
                r.get("project_id") == payload.get("project_id")
                and r.get("rfi_number") == payload.get("rfi_number")
            ):
                raise Exception(
                    'duplicate key value violates unique constraint '
                    '"rfis_project_id_rfi_number_key" (23505)'
                )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            # PostgREST semantics: the first .order() is the primary key, so
            # apply keys last-to-first with stable sorts.
            for col, desc in reversed(self._orders):
                hits.sort(key=lambda r: r.get(col), reverse=desc)
            if self._limit is not None:
                hits = hits[: self._limit]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None))
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            for p in payloads:
                if self.table == "rfis":
                    self._check_rfi_unique(p)
                rows.append(dict(p))
            return SimpleNamespace(data=[dict(p) for p in payloads])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            keep, removed = [], []
            for r in rows:
                (removed if self._matches(r) else keep).append(r)
            self.db.tables[self.table] = keep
            return SimpleNamespace(data=[dict(r) for r in removed])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def table(self, name):
        return _Query(self, name)


# ── Setup ─────────────────────────────────────────────────────────────────────

USER = SimpleNamespace(id="u1")

_PROJECTS = [
    {"id": "p1", "name": "Job One", "pm_stage": "construction", "pm_completed_at": None},
    {"id": "p2", "name": "Job Two", "pm_stage": "precon", "pm_completed_at": None},
    {"id": "bid", "name": "Bid Only", "pm_stage": None, "pm_completed_at": None},
]


def _db(**tables):
    return FakeDB({"projects": _PROJECTS, **tables})


def _install(monkeypatch, db):
    """Point the router AND the require_pm_project guard at the fake; capture audits."""
    audits = []
    monkeypatch.setattr(pm_field, "get_supabase", lambda: db)
    monkeypatch.setattr(pm_svc, "get_supabase", lambda: db)
    monkeypatch.setattr(
        pm_field, "audit", lambda actor, action, *a, **k: audits.append(action)
    )
    return audits


def _today():
    return datetime.now(timezone.utc).date().isoformat()


# ── The per-project guard ─────────────────────────────────────────────────────


def test_unknown_project_404s(monkeypatch):
    _install(monkeypatch, _db())
    with pytest.raises(HTTPException) as exc:
        pm_field.list_milestones("nope", USER)
    assert exc.value.status_code == 404


def test_bid_only_project_409s(monkeypatch):
    _install(monkeypatch, _db())
    with pytest.raises(HTTPException) as exc:
        pm_field.list_rfis("bid", USER)
    assert exc.value.status_code == 409


# ── Milestones ────────────────────────────────────────────────────────────────


def test_milestones_ordered_sort_order_then_date_nulls_last(monkeypatch):
    _install(monkeypatch, _db(pm_milestones=[
        {"id": "m1", "project_id": "p1", "sort_order": 1, "planned_date": None},
        {"id": "m2", "project_id": "p1", "sort_order": 0, "planned_date": "2026-08-01"},
        {"id": "m3", "project_id": "p1", "sort_order": 0, "planned_date": "2026-07-01"},
        {"id": "m4", "project_id": "p1", "sort_order": 0, "planned_date": None},
        {"id": "other", "project_id": "p2", "sort_order": 0, "planned_date": "2026-01-01"},
    ]))
    assert [m["id"] for m in pm_field.list_milestones("p1", USER)] == [
        "m3", "m2", "m4", "m1"
    ]


def test_milestone_create_and_patch(monkeypatch):
    db = _db(pm_milestones=[])
    audits = _install(monkeypatch, db)
    created = pm_field.create_milestone(
        "p1", MilestoneIn(name="Rough-in complete", sort_order=2), USER
    )
    assert created["project_id"] == "p1" and created["created_by"] == "u1"
    db.tables["pm_milestones"][0]["id"] = "m1"

    updated = pm_field.update_milestone(
        "p1", "m1", MilestoneUpdate(actual_date=date(2026, 7, 10)), USER
    )
    assert updated["actual_date"] == "2026-07-10"
    assert audits == ["milestone.create", "milestone.update"]


def test_milestone_patch_cannot_clear_name(monkeypatch):
    _install(monkeypatch, _db(pm_milestones=[
        {"id": "m1", "project_id": "p1", "name": "Rough-in"},
    ]))
    with pytest.raises(HTTPException) as exc:
        pm_field.update_milestone(
            "p1", "m1", MilestoneUpdate.model_validate({"name": None}), USER
        )
    assert exc.value.status_code == 400


def test_milestone_empty_patch_400s(monkeypatch):
    _install(monkeypatch, _db(pm_milestones=[
        {"id": "m1", "project_id": "p1", "name": "Rough-in"},
    ]))
    with pytest.raises(HTTPException) as exc:
        pm_field.update_milestone("p1", "m1", MilestoneUpdate(), USER)
    assert exc.value.status_code == 400


def test_milestone_delete_removes_row(monkeypatch):
    db = _db(pm_milestones=[{"id": "m1", "project_id": "p1", "name": "Rough-in"}])
    audits = _install(monkeypatch, db)
    pm_field.delete_milestone("p1", "m1", USER)
    assert db.tables["pm_milestones"] == []
    assert audits == ["milestone.delete"]


# ── Cross-project id probing ──────────────────────────────────────────────────


def test_patch_from_another_project_404s_and_leaves_row(monkeypatch):
    db = _db(pm_milestones=[{"id": "m1", "project_id": "p1", "name": "Rough-in"}])
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        pm_field.update_milestone("p2", "m1", MilestoneUpdate(name="Hijack"), USER)
    assert exc.value.status_code == 404
    assert db.tables["pm_milestones"][0]["name"] == "Rough-in"


def test_delete_from_another_project_404s_and_leaves_row(monkeypatch):
    db = _db(rfis=[{"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open"}])
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        pm_field.delete_rfi("p2", "r1", USER)
    assert exc.value.status_code == 404
    assert len(db.tables["rfis"]) == 1


# ── Daily logs: date window + ordering ───────────────────────────────────────


_LOGS = [
    {"id": "d1", "project_id": "p1", "log_date": "2026-07-01", "created_at": "2026-07-01T08:00:00Z"},
    {"id": "d2", "project_id": "p1", "log_date": "2026-07-02", "created_at": "2026-07-02T08:00:00Z"},
    {"id": "d3", "project_id": "p1", "log_date": "2026-07-02", "created_at": "2026-07-02T17:00:00Z"},
    {"id": "d4", "project_id": "p1", "log_date": "2026-07-05", "created_at": "2026-07-05T08:00:00Z"},
    {"id": "other", "project_id": "p2", "log_date": "2026-07-02", "created_at": "2026-07-02T09:00:00Z"},
]


def test_daily_logs_ordered_desc_then_created_desc(monkeypatch):
    _install(monkeypatch, _db(daily_logs=_LOGS))
    assert [r["id"] for r in pm_field.list_daily_logs("p1", None, None, USER)] == [
        "d4", "d3", "d2", "d1"
    ]


def test_daily_logs_date_window(monkeypatch):
    _install(monkeypatch, _db(daily_logs=_LOGS))
    rows = pm_field.list_daily_logs("p1", date(2026, 7, 2), date(2026, 7, 4), USER)
    assert [r["id"] for r in rows] == ["d3", "d2"]
    rows = pm_field.list_daily_logs("p1", date(2026, 7, 3), None, USER)
    assert [r["id"] for r in rows] == ["d4"]


def test_daily_log_create_and_required_field_guard(monkeypatch):
    db = _db(daily_logs=[])
    audits = _install(monkeypatch, db)
    pm_field.create_daily_log(
        "p1", DailyLogIn(log_date=date(2026, 7, 10), work_performed="Pulled feeders"), USER
    )
    assert db.tables["daily_logs"][0]["work_performed"] == "Pulled feeders"
    db.tables["daily_logs"][0]["id"] = "d1"
    with pytest.raises(HTTPException) as exc:
        pm_field.update_daily_log(
            "p1", "d1", DailyLogUpdate.model_validate({"work_performed": None}), USER
        )
    assert exc.value.status_code == 400
    assert audits == ["dailylog.create"]


# ── RFIs: numbering ───────────────────────────────────────────────────────────


def test_rfi_numbers_sequence_per_project(monkeypatch):
    db = _db(rfis=[])
    _install(monkeypatch, db)
    a = pm_field.create_rfi("p1", RFIIn(subject="Panel schedule", question="Q1"), USER)
    b = pm_field.create_rfi("p1", RFIIn(subject="Feeder size", question="Q2"), USER)
    c = pm_field.create_rfi("p2", RFIIn(subject="Trench depth", question="Q3"), USER)
    assert (a["rfi_number"], b["rfi_number"], c["rfi_number"]) == (1, 2, 1)


def test_rfi_number_fills_from_max_not_count(monkeypatch):
    # A deleted RFI leaves a gap — the next number continues from the max.
    _install(monkeypatch, _db(rfis=[
        {"id": "r5", "project_id": "p1", "rfi_number": 5, "status": "open"},
    ]))
    created = pm_field.create_rfi("p1", RFIIn(subject="s", question="q"), USER)
    assert created["rfi_number"] == 6


def test_rfi_number_race_recomputes_once(monkeypatch):
    db = _db(rfis=[{"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open"}])
    _install(monkeypatch, db)
    real = pm_field._next_rfi_number
    calls = {"n": 0}

    def stale_then_real(pid):
        calls["n"] += 1
        return 1 if calls["n"] == 1 else real(pid)  # stale number → 23505 → retry

    monkeypatch.setattr(pm_field, "_next_rfi_number", stale_then_real)
    created = pm_field.create_rfi("p1", RFIIn(subject="s", question="q"), USER)
    assert created["rfi_number"] == 2 and calls["n"] == 2


def test_rfi_number_double_conflict_is_409(monkeypatch):
    _install(monkeypatch, _db(rfis=[
        {"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open"},
    ]))
    monkeypatch.setattr(pm_field, "_next_rfi_number", lambda pid: 1)
    with pytest.raises(HTTPException) as exc:
        pm_field.create_rfi("p1", RFIIn(subject="s", question="q"), USER)
    assert exc.value.status_code == 409


def test_rfis_listed_by_number(monkeypatch):
    _install(monkeypatch, _db(rfis=[
        {"id": "r2", "project_id": "p1", "rfi_number": 2, "status": "open"},
        {"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open"},
        {"id": "rx", "project_id": "p2", "rfi_number": 1, "status": "open"},
    ]))
    assert [r["id"] for r in pm_field.list_rfis("p1", USER)] == ["r1", "r2"]


# ── RFIs: the answer → answered convenience ───────────────────────────────────


def _open_rfi():
    return {"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open",
            "answer": None, "answered_at": None}


def test_answering_open_rfi_flips_status_and_stamps_date(monkeypatch):
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(answer="Use 3/4 EMT"), USER)
    assert updated["status"] == "answered"
    assert updated["answered_at"] == _today()


def test_explicit_answered_at_is_kept(monkeypatch):
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi(
        "p1", "r1", RFIUpdate(answer="Yes", answered_at=date(2026, 7, 1)), USER
    )
    assert updated["status"] == "answered"
    assert updated["answered_at"] == "2026-07-01"


def test_explicit_status_wins_over_convenience(monkeypatch):
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi(
        "p1", "r1", RFIUpdate(answer="Resolved on site", status="closed"), USER
    )
    assert updated["status"] == "closed"
    assert updated["answered_at"] is None  # only the convenience path stamps it


def test_editing_answer_on_closed_rfi_keeps_status(monkeypatch):
    db = _db(rfis=[{**_open_rfi(), "status": "closed"}])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(answer="typo fix"), USER)
    assert updated["status"] == "closed"


def test_empty_answer_does_not_flip_status(monkeypatch):
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(subject="Re-worded"), USER)
    assert updated["status"] == "open"


# ── Manpower ──────────────────────────────────────────────────────────────────


def _manpower_in(**over):
    base = dict(work_date=date(2026, 7, 10), classification="journeyman", workers=4)
    base.update(over)
    return ManpowerIn(**base)


def test_manpower_rejects_daily_log_from_other_project(monkeypatch):
    _install(monkeypatch, _db(daily_logs=[{"id": "d2", "project_id": "p2"}]))
    with pytest.raises(HTTPException) as exc:
        pm_field.create_manpower("p1", _manpower_in(daily_log_id="d2"), USER)
    assert exc.value.status_code == 400
    assert "different project" in exc.value.detail


def test_manpower_rejects_missing_daily_log(monkeypatch):
    _install(monkeypatch, _db(daily_logs=[]))
    with pytest.raises(HTTPException) as exc:
        pm_field.create_manpower("p1", _manpower_in(daily_log_id="ghost"), USER)
    assert exc.value.status_code == 404


def test_manpower_links_same_project_daily_log(monkeypatch):
    db = _db(daily_logs=[{"id": "d1", "project_id": "p1"}], manpower_entries=[])
    audits = _install(monkeypatch, db)
    created = pm_field.create_manpower("p1", _manpower_in(daily_log_id="d1"), USER)
    assert created["daily_log_id"] == "d1" and created["project_id"] == "p1"
    assert audits == ["manpower.create"]


def test_manpower_date_window_and_order(monkeypatch):
    _install(monkeypatch, _db(manpower_entries=[
        {"id": "e1", "project_id": "p1", "work_date": "2026-07-01"},
        {"id": "e2", "project_id": "p1", "work_date": "2026-07-03"},
        {"id": "e3", "project_id": "p1", "work_date": "2026-07-05"},
        {"id": "ex", "project_id": "p2", "work_date": "2026-07-03"},
    ]))
    assert [r["id"] for r in pm_field.list_manpower("p1", None, None, USER)] == [
        "e3", "e2", "e1"
    ]
    rows = pm_field.list_manpower("p1", date(2026, 7, 2), date(2026, 7, 4), USER)
    assert [r["id"] for r in rows] == ["e2"]
