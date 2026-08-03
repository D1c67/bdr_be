"""An abandoned bid is out of Go/No-Go entirely.

Abandon preserves `current_stage` (test_abandon_lockout pins why), so the
Go/No-Go surfaces must read the marker themselves. Three doors, all shut:

- the queue: `/projects?stage=go_no_go` (the Go/No-Go page) drops abandoned rows
  — any stage-filtered list is a work queue, and an abandoned bid is on no one's
  plate. The unfiltered dashboard list keeps them (status badge + filter).
- the decision: `/gono/decide` refuses an abandoned project parked in review.
- the entrance: the generic `/advance` refuses an abandoned project, so one
  can't be pushed INTO Go/No-Go (or anywhere else) while abandoned.

Reuses the in-memory Supabase fake from test_reverify.
"""

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import GonoDecisionIn, TransitionIn
from app.routers import gono as gono_router
from app.routers import projects as proj_mod
from app.routers import workflow as wf_router
from app.services import workflow
from tests.test_reverify import FakeDB

ABANDONED = "2026-07-29T17:00:00+00:00"


def _writer():
    return CurrentUser(
        id="u1", email="pa@g3.com", role=Role.ESTIMATING_ADMIN, is_active=True,
        aal="aal2", mfa_enrolled=True,
    )


def _proj(pid, **kw):
    row = {
        "id": pid,
        "name": f"Project {pid}",
        "number": pid,
        "current_stage": "go_no_go",
        "abandoned_at": None,
        "created_at": "2026-07-01T09:00:00+00:00",
    }
    row.update(kw)
    return row


def test_abandoned_project_drops_off_the_go_no_go_queue(monkeypatch):
    db = FakeDB({
        "projects": [_proj("p1"), _proj("p2", abandoned_at=ABANDONED)],
        "project_category_state": [],
    })
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(workflow, "get_supabase", lambda: db)

    rows = proj_mod.list_projects(stage="go_no_go", user=_writer())

    assert [r["id"] for r in rows] == ["p1"]


def test_unfiltered_dashboard_list_keeps_abandoned_projects(monkeypatch):
    """Only the stage queues drop them — the dashboard still shows the bid,
    carrying its derived 'abandoned' status for the badge and filter."""
    db = FakeDB({
        "projects": [_proj("p2", abandoned_at=ABANDONED)],
        "project_category_state": [],
    })
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(workflow, "get_supabase", lambda: db)

    rows = proj_mod.list_projects(stage=None, user=_writer())

    assert [r["id"] for r in rows] == ["p2"]
    assert rows[0]["status"] == "abandoned"


def test_decide_refuses_an_abandoned_project(monkeypatch):
    db = FakeDB({"projects": [_proj("p1", abandoned_at=ABANDONED)]})
    monkeypatch.setattr(gono_router, "get_supabase", lambda: db)

    with pytest.raises(HTTPException) as exc:
        gono_router.decide("p1", GonoDecisionIn(outcome="go"), _writer())

    assert exc.value.status_code == 409
    assert "reactivate" in exc.value.detail


def test_advance_refuses_an_abandoned_project(monkeypatch):
    db = FakeDB({"projects": [_proj("p1", current_stage="intake", abandoned_at=ABANDONED)]})
    monkeypatch.setattr(wf_router, "get_supabase", lambda: db)

    with pytest.raises(HTTPException) as exc:
        wf_router.advance("p1", TransitionIn(category="intake"), _writer())

    assert exc.value.status_code == 409
    assert "reactivate" in exc.value.detail
