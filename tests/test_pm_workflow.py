"""PM stage machine (services/pm_workflow) — legality matrix and completion.

Forward moves are adjacent-only; backward moves reach any earlier stage but
demand a note; completion is a marker (pm_completed_at), not a stage. The
Supabase client is faked with the same in-memory chained-builder pattern as
test_reverify, extended here (is_/delete/auto-ids/failure injection) for the PM
code paths; test_pm_handoff and test_pm_direct_create import it from this file.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import pm_workflow
from app.services.pm_workflow import (
    PM_TRANSITIONS,
    complete_pm_project,
    transition_pm_project,
)


# ── Fake Supabase (shared with the other PM test files) ───────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._conflict = None
        self._filters = []
        self._neq_filters = []
        self._null_filters = []
        self._single = False

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

    def upsert(self, payload, on_conflict=None):
        self._op, self._payload, self._conflict = "upsert", payload, on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def neq(self, col, val):
        self._neq_filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, list(vals)))
        return self

    def is_(self, col, val):
        # Only ever called with "null" in the code under test.
        self._null_filters.append(col)
        return self

    def single(self):
        self._single = True
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    # execution
    def _matches(self, row):
        return (
            all(row.get(c) == v for c, v in self._filters)
            and all(row.get(c) != v for c, v in self._neq_filters)
            and all(row.get(c) is None for c in self._null_filters)
        )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._single:
                return SimpleNamespace(data=(dict(hits[0]) if hits else None))
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            exc = self.db.raise_on_insert.get(self.table)
            if exc is not None:
                raise exc
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", f"{self.table}-{self.db.next_id()}")
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "update":
            if self.table in self.db.update_returns_empty:
                return SimpleNamespace(data=[])  # simulated lost optimistic-lock race
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "upsert":
            keys = [k.strip() for k in (self._conflict or "").split(",") if k.strip()]
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                existing = None
                if keys:
                    existing = next(
                        (r for r in rows if all(r.get(k) == p.get(k) for k in keys)), None
                    )
                if existing is not None:
                    existing.update(p)
                    out.append(dict(existing))
                else:
                    row = dict(p)
                    rows.append(row)
                    out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            hits = [r for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=[dict(r) for r in hits])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.raise_on_insert = {}       # table -> exception to raise on insert
        self.update_returns_empty = set()  # tables whose updates "lose the race"
        self._id = 0

    def next_id(self):
        self._id += 1
        return self._id

    def table(self, name):
        return _Query(self, name)


def install(monkeypatch, db):
    """Point every DB-touching module at the fake. audit/notify write into it
    (notify_role no-ops unless a profiles row is seeded); the email mirror is
    already disabled session-wide by conftest."""
    from app.services import notifications, outcome, pm, workflow

    for mod in (pm, pm_workflow, outcome, workflow, notifications):
        monkeypatch.setattr(mod, "get_supabase", lambda db=db: db)
    return db


def audit_actions(db):
    return [r["action"] for r in db.tables.get("audit_log", [])]


def _pm_project(stage="precon", completed_at=None):
    return {"id": "p1", "name": "Job A", "pm_stage": stage, "pm_completed_at": completed_at}


def _db_at(stage, completed_at=None):
    return FakeDB({"projects": [_pm_project(stage, completed_at)], "pm_stage_events": []})


def _events(db):
    return db.tables.get("pm_stage_events", [])


# ── Legality matrix ────────────────────────────────────────────────────────────


def test_forward_edges_are_adjacent_only():
    assert PM_TRANSITIONS["precon"] == {"active_construction"}
    assert PM_TRANSITIONS["active_construction"] == {"closeout"}
    assert PM_TRANSITIONS["closeout"] == set()  # no forward moves out of closeout


def test_precon_to_active_ok(monkeypatch):
    db = install(monkeypatch, _db_at("precon"))
    row = transition_pm_project("p1", "active_construction", "u1")
    assert row["pm_stage"] == "active_construction"
    assert db.tables["projects"][0]["pm_stage"] == "active_construction"
    [ev] = _events(db)
    assert (ev["from_stage"], ev["to_stage"], ev["actor_id"]) == (
        "precon", "active_construction", "u1",
    )
    assert ev["note"] is None
    assert "pm.stage_change" in audit_actions(db)


def test_precon_to_closeout_skip_is_409(monkeypatch):
    db = install(monkeypatch, _db_at("precon"))
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("p1", "closeout", "u1")
    assert ei.value.status_code == 409
    assert db.tables["projects"][0]["pm_stage"] == "precon"
    assert _events(db) == []


def test_active_to_closeout_ok(monkeypatch):
    db = install(monkeypatch, _db_at("active_construction"))
    row = transition_pm_project("p1", "closeout", "u1")
    assert row["pm_stage"] == "closeout"
    assert [e["to_stage"] for e in _events(db)] == ["closeout"]


# ── Backward moves ─────────────────────────────────────────────────────────────


def test_backward_without_note_is_400(monkeypatch):
    db = install(monkeypatch, _db_at("active_construction"))
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("p1", "precon", "u1")
    assert ei.value.status_code == 400
    assert db.tables["projects"][0]["pm_stage"] == "active_construction"
    assert _events(db) == []


def test_backward_with_whitespace_note_is_400(monkeypatch):
    install(monkeypatch, _db_at("active_construction"))
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("p1", "precon", "u1", note="   ")
    assert ei.value.status_code == 400


def test_backward_with_note_ok_and_note_logged(monkeypatch):
    db = install(monkeypatch, _db_at("active_construction"))
    row = transition_pm_project("p1", "precon", "u1", note="Kicked back: permits stalled")
    assert row["pm_stage"] == "precon"
    [ev] = _events(db)
    assert ev["note"] == "Kicked back: permits stalled"
    assert (ev["from_stage"], ev["to_stage"]) == ("active_construction", "precon")


def test_backward_can_skip_stages_with_note(monkeypatch):
    # Backward is any-earlier-stage, not adjacent-only.
    db = install(monkeypatch, _db_at("closeout"))
    row = transition_pm_project("p1", "precon", "u1", note="Scope reset")
    assert row["pm_stage"] == "precon"
    assert _events(db)[0]["from_stage"] == "closeout"


# ── Guard conditions ───────────────────────────────────────────────────────────


def test_same_stage_is_409(monkeypatch):
    install(monkeypatch, _db_at("precon"))
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("p1", "precon", "u1")
    assert ei.value.status_code == 409


def test_not_in_pm_is_409(monkeypatch):
    install(monkeypatch, _db_at(None))
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("p1", "active_construction", "u1")
    assert ei.value.status_code == 409


def test_completed_project_blocks_transition_409(monkeypatch):
    db = install(monkeypatch, _db_at("closeout", completed_at="2026-07-01T00:00:00Z"))
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("p1", "active_construction", "u1", note="reopen")
    assert ei.value.status_code == 409
    assert _events(db) == []


def test_unknown_stage_is_400(monkeypatch):
    install(monkeypatch, _db_at("precon"))
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("p1", "warranty", "u1")
    assert ei.value.status_code == 400


def test_unknown_project_is_404(monkeypatch):
    install(monkeypatch, FakeDB({"projects": []}))
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("ghost", "active_construction", "u1")
    assert ei.value.status_code == 404


def test_concurrent_move_is_409_and_logs_nothing(monkeypatch):
    db = install(monkeypatch, _db_at("precon"))
    db.update_returns_empty.add("projects")
    with pytest.raises(HTTPException) as ei:
        transition_pm_project("p1", "active_construction", "u1")
    assert ei.value.status_code == 409
    assert _events(db) == []
    assert audit_actions(db) == []


# ── Completion ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage", ["precon", "active_construction"])
def test_complete_requires_closeout(monkeypatch, stage):
    db = install(monkeypatch, _db_at(stage))
    with pytest.raises(HTTPException) as ei:
        complete_pm_project("p1", "u1")
    assert ei.value.status_code == 409
    assert db.tables["projects"][0].get("pm_completed_at") is None


def test_complete_from_closeout_stamps_and_preserves_stage(monkeypatch):
    db = install(monkeypatch, _db_at("closeout"))
    row = complete_pm_project("p1", "exec1")
    assert row["pm_completed_at"]
    assert row["pm_completed_by"] == "exec1"
    assert db.tables["projects"][0]["pm_stage"] == "closeout"  # marker, not a stage
    assert "pm.complete" in audit_actions(db)


def test_second_complete_is_409(monkeypatch):
    install(monkeypatch, _db_at("closeout"))
    complete_pm_project("p1", "exec1")
    with pytest.raises(HTTPException) as ei:
        complete_pm_project("p1", "exec1")
    assert ei.value.status_code == 409


def test_complete_race_is_409(monkeypatch):
    db = install(monkeypatch, _db_at("closeout"))
    db.update_returns_empty.add("projects")
    with pytest.raises(HTTPException) as ei:
        complete_pm_project("p1", "exec1")
    assert ei.value.status_code == 409
    assert db.tables["projects"][0].get("pm_completed_at") is None
