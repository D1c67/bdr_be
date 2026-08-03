"""Abandoning a bid takes it off everyone's plate.

Abandon deliberately preserves `current_stage` (so we always know where the bid
died), which means nothing downstream can infer "no longer work" from the stage
— every surface has to read the abandon marker itself. These pin the two halves
of that: the external estimator loses the project entirely (access gate, portal
dashboard, and every path that would push new material at them), and the team's
bells for that estimator are swept so nothing deep-links into the 403.

Real tables are replaced with a chainable fake that serves queued rows in query
order, so these pin the rules, not Postgres.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import deps as deps_mod
from app.core.deps import CurrentUser, require_project_assignment
from app.core.roles import Role
from app.routers import estimator as est_mod
from app.routers import projects as proj_mod
from app.services import file_sends as fs_mod


# ── chainable Supabase fake (serves queued rows per table) ────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.one = False  # .single() — PostgREST returns the row, not a list

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

    def limit(self, *a):
        return self

    def single(self):
        self.one = True
        return self

    def execute(self):
        queue = self.db.queues.get(self.table_name) or []
        rows = queue.pop(0) if queue else []
        return SimpleNamespace(data=(rows[0] if rows else None) if self.one else rows)


class _FakeDB:
    def __init__(self, **tables):
        self.queues = {name: list(rows) for name, rows in tables.items()}

    def table(self, name):
        return _Query(self, name)


ABANDONED = "2026-07-20T17:00:00+00:00"


def _estimator(**kw):
    row = dict(
        id="e1", email="e@x.com", role=Role.ESTIMATOR, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )
    row.update(kw)
    return CurrentUser(**row)


def _writer():
    return CurrentUser(
        id="u1", email="pa@g3.com", role=Role.ESTIMATING_ADMIN, is_active=True,
        aal="aal2", mfa_enrolled=True,
    )


# ── the access gate ───────────────────────────────────────────────────────


def test_estimator_is_locked_out_of_an_abandoned_project(monkeypatch):
    """A live assignment is no longer enough: the abandon marker closes the door."""
    db = _FakeDB(
        estimator_assignments=[[{"id": "a1", "expires_at": None, "revoked_at": None}]],
        projects=[[{"abandoned_at": ABANDONED}]],
    )
    monkeypatch.setattr(deps_mod, "get_supabase", lambda: db)
    with pytest.raises(HTTPException) as exc:
        require_project_assignment("p1", _estimator())
    assert exc.value.status_code == 403
    assert "no longer available" in exc.value.detail


def test_abandon_lockout_is_not_a_security_signal(monkeypatch):
    """An expected denial must not feed the denied-access burst alert — an
    estimator refreshing a bookmarked page can't be made to look like probing."""
    db = _FakeDB(
        estimator_assignments=[[{"id": "a1", "expires_at": None, "revoked_at": None}]],
        projects=[[{"abandoned_at": ABANDONED}]],
    )
    monkeypatch.setattr(deps_mod, "get_supabase", lambda: db)
    recorded: list = []
    import app.services.security_alerts as alerts_mod

    monkeypatch.setattr(
        alerts_mod, "record_denied_access", lambda *a: recorded.append(a)
    )
    with pytest.raises(HTTPException):
        require_project_assignment("p1", _estimator())
    assert recorded == []


def test_live_project_still_passes_the_gate(monkeypatch):
    db = _FakeDB(
        estimator_assignments=[[{"id": "a1", "expires_at": None, "revoked_at": None}]],
        projects=[[{"abandoned_at": None}]],
    )
    monkeypatch.setattr(deps_mod, "get_supabase", lambda: db)
    assert require_project_assignment("p1", _estimator()).id == "e1"


def test_internal_user_never_reads_the_marker(monkeypatch):
    """Non-estimators pass through untouched — abandoned projects stay readable
    internally (the project page shows the "abandoned at <stage>" notice)."""
    db = _FakeDB()
    monkeypatch.setattr(deps_mod, "get_supabase", lambda: db)
    assert require_project_assignment("p1", _writer()).id == "u1"


# ── the portal dashboard ──────────────────────────────────────────────────


def _portal_env(monkeypatch, projects):
    db = _FakeDB(
        estimator_assignments=[
            [
                {
                    "project_id": "p1",
                    "due_at": None,
                    "expires_at": None,
                    "created_at": "2026-07-01T09:00:00+00:00",
                    "returned_at": None,
                }
            ]
        ],
        projects=[projects],
        estimator_submissions=[[]],
        file_send_batches=[[]],
        file_send_recipients=[[]],
    )
    monkeypatch.setattr(est_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(fs_mod, "get_supabase", lambda: db)
    return db


def _proj(**kw):
    row = {
        "id": "p1",
        "name": "Acme",
        "number": "42",
        "due_from_estimator_at": None,
        "abandoned_at": None,
    }
    row.update(kw)
    return row


def test_abandoned_project_is_reported_withdrawn_on_the_portal_dashboard(monkeypatch):
    """The assignment is still live (abandon is reversible, so it isn't revoked)
    and the row STAYS — as the estimator's standing record that G3 stopped work.
    The portal renders it inert; the project itself still 403s."""
    _portal_env(monkeypatch, [_proj(abandoned_at=ABANDONED)])
    rows = est_mod.my_assigned_projects(_estimator())
    assert [(r["id"], r["status"]) for r in rows] == [("p1", "withdrawn")]
    assert rows[0]["withdrawn"] is True
    assert rows[0]["withdrawn_at"] == ABANDONED


def test_withdrawn_row_never_nags_about_changes(monkeypatch):
    """Nothing is left to review on a dead bid — the changes flag (and the red
    dot it drives) is cleared rather than pointing at a page that 403s."""
    db = _portal_env(monkeypatch, [_proj(abandoned_at=ABANDONED)])
    db.queues["estimator_submissions"] = [
        [{"project_id": "p1", "submitted_at": "2026-07-01T10:00:00+00:00"}]
    ]
    monkeypatch.setattr(
        fs_mod, "last_sent_at_by_project", lambda ids, uid: {"p1": "2026-07-10T10:00:00+00:00"}
    )
    row = est_mod.my_assigned_projects(_estimator())[0]
    assert row["status"] == "withdrawn"
    assert row["has_changes"] is False


def test_live_project_still_shows_on_the_portal_dashboard(monkeypatch):
    _portal_env(monkeypatch, [_proj()])
    rows = est_mod.my_assigned_projects(_estimator())
    assert [r["id"] for r in rows] == ["p1"]
    assert rows[0]["withdrawn"] is False
    assert rows[0]["status"] == "assigned"


# ── nothing new gets pushed at an estimator ───────────────────────────────


def test_refuse_if_abandoned_is_a_409():
    with pytest.raises(HTTPException) as exc:
        est_mod._refuse_if_abandoned({"abandoned_at": ABANDONED})
    assert exc.value.status_code == 409
    assert "reactivate" in exc.value.detail
    # A live bid passes silently.
    assert est_mod._refuse_if_abandoned({"abandoned_at": None}) is None


@pytest.mark.parametrize(
    "route", ["assign_estimator", "send_to_estimator", "send_file_updates"]
)
def test_every_send_path_guards_on_the_marker(route):
    """All three outbound paths read `abandoned_at` and run it through the guard
    — a new assignment, a re-send and a revision email are equally dead."""
    import inspect

    src = inspect.getsource(getattr(est_mod, route))
    assert "abandoned_at" in src
    assert "_refuse_if_abandoned(proj)" in src


def test_assign_estimator_refuses_an_abandoned_project(monkeypatch):
    db = _FakeDB(projects=[[_proj(abandoned_at=ABANDONED)]])
    monkeypatch.setattr(est_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(est_mod, "project_has_drawing", lambda pid: True)
    monkeypatch.setattr(est_mod.estimator_email, "graph_configured", lambda: True)
    body = est_mod.AssignIn(estimator_id="e1")
    with pytest.raises(HTTPException) as exc:
        est_mod.assign_estimator("p1", body, _writer())
    assert exc.value.status_code == 409
    # Refused BEFORE any assignment row or send batch was touched.
    assert db.queues["projects"] == []


# ── the estimator's bells ─────────────────────────────────────────────────


def test_abandon_sweeps_the_assigned_estimators_bells(monkeypatch):
    db = _FakeDB(
        estimator_assignments=[[{"estimator_id": "e1"}, {"estimator_id": "e2"}]]
    )
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)
    calls: list[dict] = []
    monkeypatch.setattr(proj_mod, "dismiss_notifications", lambda **kw: calls.append(kw))

    proj_mod._sweep_estimator_notifications("p1")

    assert {c["user_id"] for c in calls} == {"e1", "e2"}
    for c in calls:
        assert c["project_id"] == "p1"
        # Only estimator-facing types — the internal side of a shared thread
        # (estimator_note) keeps its own bells.
        assert c["types"] == proj_mod.ESTIMATOR_NOTIFICATION_TYPES
        assert "estimator_note" in c["types"] and "assigned" in c["types"]


def test_sweep_failure_never_breaks_the_abandon(monkeypatch):
    """The abandon is already committed when the sweep runs — a bell that won't
    clear must not turn a successful abandon into a 500."""
    db = _FakeDB(estimator_assignments=[[{"estimator_id": "e1"}]])
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)

    def _boom(**kw):
        raise RuntimeError("postgrest down")

    monkeypatch.setattr(proj_mod, "dismiss_notifications", _boom)
    proj_mod._sweep_estimator_notifications("p1")  # no raise
