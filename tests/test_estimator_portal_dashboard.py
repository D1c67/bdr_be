"""GET /estimator/projects — the external portal's dashboard rows.

The row is the estimator's own clock (assigned_at / due_at / turned_in_at) plus
a three-value status, and every one of those is a derivation the UI can't
recompute: "turned in" is the FIRST hand-off while "changes to review" compares
the team's newest send against the LATEST round, the send scope is per-recipient
(a project you were added to late must not report sends you never got), and the
project-wide due date is only a fallback for an assignment with none.

Real tables are replaced with a chainable fake that serves queued rows in query
order, so these pin the derivations, not Postgres.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import estimator as est_mod
from app.services import file_sends as fs_mod


# ── chainable Supabase fake (serves queued rows per table) ────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table

    def select(self, *a, **k):
        return self

    def eq(self, *a):
        return self

    def is_(self, *a):
        return self

    def in_(self, *a):
        return self

    def or_(self, *a):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        queue = self.db.queues.get(self.table_name) or []
        return SimpleNamespace(data=queue.pop(0) if queue else [])


class _FakeDB:
    def __init__(self, **tables):
        self.queues = {name: [rows] for name, rows in tables.items()}

    def table(self, name):
        return _Query(self, name)


def _estimator():
    return CurrentUser(
        id="e1", email="e@x.com", role=Role.ESTIMATOR, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )


def _env(
    monkeypatch,
    *,
    assignments,
    projects,
    submissions=(),
    batches=(),
    recipients=(),
):
    db = _FakeDB(
        estimator_assignments=list(assignments),
        projects=list(projects),
        estimator_submissions=list(submissions),
        file_send_batches=list(batches),
        file_send_recipients=list(recipients),
    )
    monkeypatch.setattr(est_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(fs_mod, "get_supabase", lambda: db)
    return db


def _assign(**kw):
    row = {
        "project_id": "p1",
        "due_at": None,
        "expires_at": None,
        "created_at": "2026-07-01T09:00:00+00:00",
        "returned_at": None,
    }
    row.update(kw)
    return row


def _proj(**kw):
    row = {"id": "p1", "name": "Acme", "number": "42", "due_from_estimator_at": None}
    row.update(kw)
    return row


# ── access ────────────────────────────────────────────────────────────────


def test_non_estimator_is_403(monkeypatch):
    user = CurrentUser(
        id="u1", email="w@g3.com", role=Role.ESTIMATING_ADMIN, is_active=True,
        is_dev=False, aal="aal2", mfa_enrolled=True,
    )
    with pytest.raises(HTTPException) as ei:
        est_mod.my_assigned_projects(user=user)
    assert ei.value.status_code == 403


def test_no_assignments_returns_empty_without_further_reads(monkeypatch):
    db = _env(monkeypatch, assignments=[], projects=[_proj()])
    assert est_mod.my_assigned_projects(user=_estimator()) == []
    # The projects row stayed queued — nothing downstream ran.
    assert db.queues["projects"] == [[_proj()]]


# ── the three statuses ────────────────────────────────────────────────────


def test_never_submitted_is_assigned_to_you(monkeypatch):
    _env(monkeypatch, assignments=[_assign(due_at="2026-08-01T17:00:00+00:00")], projects=[_proj()])
    (row,) = est_mod.my_assigned_projects(user=_estimator())
    assert row["status"] == "assigned"
    assert row["turned_in_at"] is None
    assert row["has_changes"] is False
    assert row["assigned_at"] == "2026-07-01T09:00:00+00:00"
    assert row["due_at"] == "2026-08-01T17:00:00+00:00"
    # Stage / task-for / bid-due are internal — they must not ride along.
    assert "current_stage" not in row


def test_submitted_is_sent(monkeypatch):
    _env(
        monkeypatch,
        assignments=[_assign(returned_at="2026-07-10T12:00:00+00:00")],
        projects=[_proj()],
    )
    (row,) = est_mod.my_assigned_projects(user=_estimator())
    assert row["status"] == "sent"
    assert row["turned_in_at"] == "2026-07-10T12:00:00+00:00"


def test_send_after_last_round_is_changes_to_review(monkeypatch):
    _env(
        monkeypatch,
        assignments=[_assign(returned_at="2026-07-10T12:00:00+00:00")],
        projects=[_proj()],
        submissions=[{"project_id": "p1", "submitted_at": "2026-07-10T12:00:00+00:00"}],
        batches=[{"id": "b1", "project_id": "p1", "sent_at": "2026-07-12T08:00:00+00:00"}],
        recipients=[{"batch_id": "b1"}],
    )
    (row,) = est_mod.my_assigned_projects(user=_estimator())
    assert row["status"] == "changes"
    assert row["has_changes"] is True
    # "Turned in" keeps reporting the ORIGINAL hand-off — that is what on-time
    # is measured against, so a pending change must not blank or move it.
    assert row["turned_in_at"] == "2026-07-10T12:00:00+00:00"


def test_revision_round_after_the_send_clears_changes(monkeypatch):
    """Round 2 answers the change: newest send is older than my latest round."""
    _env(
        monkeypatch,
        assignments=[_assign(returned_at="2026-07-10T12:00:00+00:00")],
        projects=[_proj()],
        submissions=[
            {"project_id": "p1", "submitted_at": "2026-07-10T12:00:00+00:00"},
            {"project_id": "p1", "submitted_at": "2026-07-13T09:00:00+00:00"},
        ],
        batches=[{"id": "b1", "project_id": "p1", "sent_at": "2026-07-12T08:00:00+00:00"}],
        recipients=[{"batch_id": "b1"}],
    )
    (row,) = est_mod.my_assigned_projects(user=_estimator())
    assert row["status"] == "sent"
    assert row["has_changes"] is False
    # Still the FIRST round, not the revision.
    assert row["turned_in_at"] == "2026-07-10T12:00:00+00:00"


def test_package_before_any_submission_is_not_changes(monkeypatch):
    """The initial package is their package, not a change to review."""
    _env(
        monkeypatch,
        assignments=[_assign()],
        projects=[_proj()],
        batches=[{"id": "b1", "project_id": "p1", "sent_at": "2026-07-02T08:00:00+00:00"}],
        recipients=[{"batch_id": "b1"}],
    )
    (row,) = est_mod.my_assigned_projects(user=_estimator())
    assert row["status"] == "assigned"
    assert row["has_changes"] is False


def test_send_to_another_estimator_is_not_my_change(monkeypatch):
    """A batch with no recipient row for me is outside my scope entirely."""
    _env(
        monkeypatch,
        assignments=[_assign(returned_at="2026-07-10T12:00:00+00:00")],
        projects=[_proj()],
        submissions=[{"project_id": "p1", "submitted_at": "2026-07-10T12:00:00+00:00"}],
        batches=[{"id": "b1", "project_id": "p1", "sent_at": "2026-07-12T08:00:00+00:00"}],
        recipients=[],
    )
    (row,) = est_mod.my_assigned_projects(user=_estimator())
    assert row["status"] == "sent"
    assert row["has_changes"] is False


# ── field derivations ─────────────────────────────────────────────────────


def test_project_due_date_is_only_a_fallback(monkeypatch):
    _env(
        monkeypatch,
        assignments=[_assign(due_at="2026-08-01T17:00:00+00:00"), _assign(project_id="p2")],
        projects=[
            _proj(due_from_estimator_at="2026-09-09T17:00:00+00:00"),
            _proj(id="p2", number="43", due_from_estimator_at="2026-09-09T17:00:00+00:00"),
        ],
    )
    rows = {r["id"]: r for r in est_mod.my_assigned_projects(user=_estimator())}
    assert rows["p1"]["due_at"] == "2026-08-01T17:00:00+00:00"  # per-assignment wins
    assert rows["p2"]["due_at"] == "2026-09-09T17:00:00+00:00"  # fallback


def test_reassignment_renders_from_the_newest_row(monkeypatch):
    _env(
        monkeypatch,
        assignments=[
            _assign(created_at="2026-07-01T09:00:00+00:00", due_at="2026-07-20T17:00:00+00:00"),
            _assign(created_at="2026-07-05T09:00:00+00:00", due_at="2026-07-25T17:00:00+00:00"),
        ],
        projects=[_proj()],
    )
    (row,) = est_mod.my_assigned_projects(user=_estimator())
    assert row["assigned_at"] == "2026-07-05T09:00:00+00:00"
    assert row["due_at"] == "2026-07-25T17:00:00+00:00"


def test_first_submission_backs_up_a_null_returned_at(monkeypatch):
    """Assignments made before 0036 carry no returned_at; the estimator's own
    earliest round still dates the hand-off."""
    _env(
        monkeypatch,
        assignments=[_assign()],
        projects=[_proj()],
        submissions=[
            {"project_id": "p1", "submitted_at": "2026-07-14T09:00:00+00:00"},
            {"project_id": "p1", "submitted_at": "2026-07-10T12:00:00+00:00"},
        ],
    )
    (row,) = est_mod.my_assigned_projects(user=_estimator())
    assert row["turned_in_at"] == "2026-07-10T12:00:00+00:00"
    assert row["status"] == "sent"


def test_newest_send_per_project_wins(monkeypatch):
    """Several batches reached me — only the latest one dates the comparison."""
    assert fs_mod.last_sent_at_by_project([], "e1") == {}  # no ids → no reads

    _env(
        monkeypatch,
        assignments=[],
        projects=[],
        batches=[
            {"id": "b1", "project_id": "p1", "sent_at": "2026-07-02T08:00:00+00:00"},
            {"id": "b2", "project_id": "p1", "sent_at": "2026-07-12T08:00:00+00:00"},
            {"id": "b3", "project_id": "p2", "sent_at": "2026-07-03T08:00:00+00:00"},
            {"id": "b4", "project_id": "p2", "sent_at": None},
        ],
        recipients=[{"batch_id": "b1"}, {"batch_id": "b2"}, {"batch_id": "b4"}],
    )
    assert fs_mod.last_sent_at_by_project(["p1", "p2"], "e1") == {
        "p1": "2026-07-12T08:00:00+00:00"
    }
