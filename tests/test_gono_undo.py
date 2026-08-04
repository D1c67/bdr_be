"""Undoing a Go/No-Go decision.

A recorded decision can be taken back, which drops the decision row and parks the
intake lane back on `go_no_go` (in review). A No-Go always reverses — a declined
project is frozen, so nothing can have happened since. A Go only reverses while it
changed nothing outside the gate: still at To Estimator, no live estimator
assignment, no initial package emailed.

Reuses the in-memory Supabase fake from test_reverify.
"""

import pytest
from fastapi import HTTPException

from app.core.roles import Role
from app.services import file_sends, gono, workflow
from tests.test_reverify import FakeDB

# Category state after each decision. A Go moves intake's head to `to_estimator`;
# a No-Go leaves it parked on `go_no_go` and kills the project globally.
_AFTER_GO = {
    "intake": ("to_estimator", "active"),
    "material_numbers": ("estimate_received", "locked"),
    "labor_numbers": ("labor_numbers", "locked"),
    "send_out": ("gc_pricing", "locked"),
}
_AFTER_NO_GO = {**_AFTER_GO, "intake": ("go_no_go", "active")}


def _cat_rows(spec):
    return [
        {"project_id": "p1", "category": c, "current_task": t, "status": s,
         "owner_role": None, "completed_at": ("x" if s == "complete" else None)}
        for c, (t, s) in spec.items()
    ]


def _decision(outcome="go", method="manual"):
    return {
        "id": "d1",
        "project_id": "p1",
        "outcome": outcome,
        "method": method,
        "decided_by": "u1",
    }


def _db(*, outcome="go", cats=None, decisions=None, assignments=None, batches=None):
    return FakeDB({
        "projects": [{
            "id": "p1",
            # A Go left the headline on to_estimator; a No-Go killed the project.
            "current_stage": "declined" if outcome == "no_go" else "to_estimator",
            "current_owner_role": None,
            "abandoned_at": None,
        }],
        "project_category_state": _cat_rows(
            cats or (_AFTER_NO_GO if outcome == "no_go" else _AFTER_GO)
        ),
        "go_no_go_decisions": [_decision(outcome)] if decisions is None else decisions,
        "stage_events": [],
        "estimator_assignments": assignments or [],
        "file_send_batches": batches or [],
    })


def _install(monkeypatch, db):
    """Point gono, workflow and file_sends at the fake DB; capture the audit calls."""
    audits = []
    monkeypatch.setattr(gono, "get_supabase", lambda: db)
    monkeypatch.setattr(workflow, "get_supabase", lambda: db)
    monkeypatch.setattr(file_sends, "get_supabase", lambda: db)
    monkeypatch.setattr(gono, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(gono, "dismiss_notifications", lambda **kw: None)
    monkeypatch.setattr(gono, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(workflow.notifications, "dismiss_notifications", lambda **kw: None)
    return audits


def _state(db):
    return {r["category"]: (r["current_task"], r["status"])
            for r in db.tables["project_category_state"]}


# ── The happy paths ───────────────────────────────────────────────────────────


def test_undo_no_go_reopens_a_declined_project(monkeypatch):
    db = _db(outcome="no_go")
    audits = _install(monkeypatch, db)

    gono.undo("p1", "u2")

    # Back in review at Go/No-Go, and no longer declined.
    assert _state(db)["intake"] == ("go_no_go", "active")
    assert db.tables["projects"][0]["current_stage"] == "go_no_go"
    # The decision is gone, so the panel offers Go / No-Go again.
    assert db.tables["go_no_go_decisions"] == []
    # The reverse move is recorded, not erased.
    back = [e for e in db.tables["stage_events"] if e["to_stage"] == "go_no_go"]
    assert back and back[0]["from_stage"] == "declined"
    assert any(a[1] == "gono.undo" for a in audits)


def test_undo_go_returns_intake_head_from_to_estimator(monkeypatch):
    db = _db(outcome="go")
    _install(monkeypatch, db)

    gono.undo("p1", "u2")

    assert _state(db)["intake"] == ("go_no_go", "active")
    assert db.tables["projects"][0]["current_stage"] == "go_no_go"
    assert db.tables["go_no_go_decisions"] == []
    back = [e for e in db.tables["stage_events"] if e["to_stage"] == "go_no_go"]
    assert back and back[0]["from_stage"] == "to_estimator"


def test_undo_notifies_the_executive_that_the_gate_is_open_again(monkeypatch):
    """An undo re-parks the bid at Go/No-Go, so its owner is up again.

    The notice is raised AFTER the stale-notification sweep — created first, it
    would be dismissed on the way past and nobody would hear that the decision
    was taken back.
    """
    db = _db(outcome="go")
    _install(monkeypatch, db)
    order: list = []
    monkeypatch.setattr(
        gono, "dismiss_notifications", lambda **kw: order.append(("dismiss", kw["types"]))
    )
    monkeypatch.setattr(
        gono, "notify_role", lambda role, pid, type_, msg: order.append(("notify", role, pid, type_))
    )

    gono.undo("p1", "u2")

    assert order == [
        ("dismiss", ["gono_go", "stage_handoff"]),
        ("notify", Role.EXECUTIVE, "p1", "stage_handoff"),
    ]


def test_undo_survives_a_failed_notification_sweep(monkeypatch):
    """Notifications are cleanup: the project is already back in review and must
    not roll back because the bell could not be updated."""
    db = _db(outcome="no_go")
    _install(monkeypatch, db)
    monkeypatch.setattr(
        gono, "dismiss_notifications", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    gono.undo("p1", "u2")

    assert _state(db)["intake"] == ("go_no_go", "active")
    assert db.tables["go_no_go_decisions"] == []


def test_undo_leaves_the_other_lanes_locked(monkeypatch):
    db = _db(outcome="go")
    _install(monkeypatch, db)
    gono.undo("p1", "u2")
    state = _state(db)
    for cat in ("material_numbers", "labor_numbers", "send_out"):
        assert state[cat][1] == "locked"


# ── The guards ────────────────────────────────────────────────────────────────


def test_undo_refused_once_intake_advanced_past_to_estimator(monkeypatch):
    db = _db(outcome="go", cats={**_AFTER_GO, "intake": ("to_estimator", "complete")})
    _install(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        gono.undo("p1", "u2")
    assert exc.value.status_code == 409
    assert exc.value.detail == gono.UNDO_BLOCKED_ADVANCED
    # Nothing moved and the decision survives.
    assert _state(db)["intake"] == ("to_estimator", "complete")
    assert len(db.tables["go_no_go_decisions"]) == 1


def test_undo_refused_while_an_estimator_is_assigned(monkeypatch):
    db = _db(
        outcome="go",
        assignments=[{"id": "a1", "project_id": "p1", "revoked_at": None}],
    )
    _install(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        gono.undo("p1", "u2")
    assert exc.value.status_code == 409
    assert exc.value.detail == gono.UNDO_BLOCKED_SENT
    assert len(db.tables["go_no_go_decisions"]) == 1


def test_undo_refused_after_a_package_was_emailed_even_if_revoked(monkeypatch):
    # The assignment was revoked, but the estimator already has the drawings.
    db = _db(
        outcome="go",
        assignments=[{"id": "a1", "project_id": "p1", "revoked_at": "2026-07-01T00:00:00Z"}],
        batches=[{"id": "b1", "project_id": "p1", "kind": "initial"}],
    )
    _install(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        gono.undo("p1", "u2")
    assert exc.value.detail == gono.UNDO_BLOCKED_SENT


def test_undo_of_a_no_go_is_never_blocked_by_stale_estimator_rows(monkeypatch):
    # A declined project is frozen; a leftover row from an earlier round must not
    # stand in the way of reopening it.
    db = _db(
        outcome="no_go",
        assignments=[{"id": "a1", "project_id": "p1", "revoked_at": None}],
        batches=[{"id": "b1", "project_id": "p1", "kind": "initial"}],
    )
    _install(monkeypatch, db)
    gono.undo("p1", "u2")
    assert db.tables["go_no_go_decisions"] == []


def test_undo_without_a_decision_is_a_conflict(monkeypatch):
    db = _db(outcome="go", decisions=[])
    _install(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        gono.undo("p1", "u2")
    assert exc.value.status_code == 409
    assert exc.value.detail == gono.UNDO_NO_DECISION
    # The lane is untouched — a missing decision must not bounce the project back.
    assert _state(db)["intake"] == ("to_estimator", "active")


def test_undo_on_a_missing_project_is_404(monkeypatch):
    db = _db(outcome="go")
    db.tables["projects"] = []
    _install(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        gono.undo("p1", "u2")
    assert exc.value.status_code == 404


# ── The status the panel renders from ─────────────────────────────────────────


def test_undo_status_reports_nothing_to_undo_without_a_decision(monkeypatch):
    db = _db(outcome="go", decisions=[])
    _install(monkeypatch, db)
    status = gono.undo_status(
        db.tables["projects"][0], workflow.load_category_state("p1"), None
    )
    assert status == {"can_undo": False, "undo_blocked": None}


def test_undo_status_reports_the_blocker(monkeypatch):
    db = _db(
        outcome="go",
        assignments=[{"id": "a1", "project_id": "p1", "revoked_at": None}],
    )
    _install(monkeypatch, db)
    status = gono.undo_status(
        db.tables["projects"][0], workflow.load_category_state("p1"), _decision()
    )
    assert status == {"can_undo": False, "undo_blocked": gono.UNDO_BLOCKED_SENT}


def test_undo_status_allows_an_untouched_go(monkeypatch):
    db = _db(outcome="go")
    _install(monkeypatch, db)
    status = gono.undo_status(
        db.tables["projects"][0], workflow.load_category_state("p1"), _decision()
    )
    assert status == {"can_undo": True, "undo_blocked": None}
