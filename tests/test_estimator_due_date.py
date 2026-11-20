"""The estimator's due-back date reaches the estimator.

Prod 26.10.7309B, 2026-11-17: the team moved the project's estimator due date
to 11-19 for a revision round and sent the revised drawings. The estimator's
portal kept showing 10-19, the date they were assigned with, and no email named
the new one. Nothing had failed: the portal read the assignment's own copy of
the date, which is always filled and never rewritten, and neither the update
email nor a date edit said anything about a deadline.

This file pins the rule (`estimator_due`), the notice sent when the date is
edited on its own, and the PATCH wiring. The portal reads, the update email
and the send route are pinned next to their own tests
(test_estimator_portal_dashboard, test_file_send_log, test_file_updates,
test_estimator_assign).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import ProjectUpdate
from app.routers import projects as proj_mod
from app.services import datetime_format, estimator_due, notification_email
from app.services import estimator_lifecycle as lc
from app.services.notifications import ESTIMATOR_NOTIFICATION_TYPES
from tests.test_internal_bid_date_email import _FakeDB

NOW = datetime(2026, 11, 17, 17, 31, tzinfo=timezone.utc)
ORIGINAL = "2026-10-19T18:00:00+00:00"   # Mon Oct 19th 11:00 AM PDT
REVISION = "2026-11-19T22:00:00+00:00"   # Thu Nov 19th 2:00 PM PST
AHEAD = "2099-11-19T22:00:00+00:00"      # a Thursday too
PROJECT = {
    "id": "p1",
    "name": "MT Charleston Cabin refresh",
    "number": "26.10.7309B",
    "abandoned_at": None,
    "due_from_estimator_at": AHEAD,
}


@pytest.fixture(autouse=True)
def _pacific(monkeypatch):
    monkeypatch.setattr(
        datetime_format, "get_settings",
        lambda: SimpleNamespace(display_timezone="America/Los_Angeles"),
    )


# ── the rule ───────────────────────────────────────────────────────────────


def test_the_projects_date_wins_over_the_assignments_copy():
    assert estimator_due.current_due(REVISION, ORIGINAL) == REVISION


def test_the_assignments_copy_stands_in_for_a_project_with_no_date():
    assert estimator_due.current_due(None, ORIGINAL) == ORIGINAL
    assert estimator_due.current_due(None, None) is None


def test_same_instant_ignores_how_the_moment_is_spelled():
    assert estimator_due.same_instant("2026-11-19T22:00:00Z", REVISION)
    assert estimator_due.same_instant("2026-11-19T14:00:00-08:00", REVISION)
    assert estimator_due.same_instant(None, None)
    assert not estimator_due.same_instant(ORIGINAL, REVISION)
    assert not estimator_due.same_instant(None, REVISION)


def test_a_date_still_ahead_is_stated_in_company_time():
    assert (
        estimator_due.upcoming_due_text(REVISION, now=NOW)
        == "Thursday, November 19th 2:00 PM PST"
    )


def test_a_date_already_passed_is_not_stated():
    assert estimator_due.upcoming_due_text(ORIGINAL, now=NOW) is None
    assert estimator_due.upcoming_due_text(None, now=NOW) is None
    assert estimator_due.upcoming_due_text("not a date", now=NOW) is None


def test_due_text_never_shows_the_stored_string():
    assert estimator_due.due_text(REVISION) == "Thursday, November 19th 2:00 PM PST"
    assert estimator_due.due_text(None) == "TBD"
    assert estimator_due.due_text("not a date") == "not a date"


# ── the notice for a date edited on its own ────────────────────────────────


@pytest.fixture
def notices(monkeypatch):
    log = SimpleNamespace(bells=[], dismissed=[])
    monkeypatch.setattr(
        lc, "notify_user",
        lambda uid, pid, type_, msg, **kw: log.bells.append(
            {"user_id": uid, "project_id": pid, "type": type_, "message": msg, **kw}
        ),
    )
    monkeypatch.setattr(lc, "dismiss_notifications", lambda **kw: log.dismissed.append(kw))
    return log


def _assignees(monkeypatch, *est_ids):
    monkeypatch.setattr(
        lc, "_active_assignees", lambda pid: [{"estimator_id": e} for e in est_ids]
    )


def test_every_active_assignee_is_told_the_new_date(monkeypatch, notices):
    _assignees(monkeypatch, "e1", "e2")
    lc.notify_due_changed(PROJECT)

    assert {b["user_id"] for b in notices.bells} == {"e1", "e2"}
    for b in notices.bells:
        assert b["type"] == "estimator_due_changed"
        assert b["project_id"] == "p1"
        assert b["message"] == (
            "Due date changed: #26.10.7309B · MT Charleston Cabin refresh "
            "is now due back Thursday, November 19th 2:00 PM PST."
        )
        # The email mirror is the point: a bell alone waits for a portal visit.
        assert "mirror_email" not in b
    # An earlier notice names a date that is no longer true.
    assert notices.dismissed == [{"project_id": "p1", "types": ["estimator_due_changed"]}]


def test_an_estimator_assigned_twice_is_told_once(monkeypatch, notices):
    _assignees(monkeypatch, "e1", "e1")
    lc.notify_due_changed(PROJECT)
    assert len(notices.bells) == 1


def test_a_date_moved_into_the_past_is_a_correction_not_a_notice(monkeypatch, notices):
    _assignees(monkeypatch, "e1")
    lc.notify_due_changed({**PROJECT, "due_from_estimator_at": "2020-01-01T00:00:00+00:00"})
    lc.notify_due_changed({**PROJECT, "due_from_estimator_at": None})
    assert notices.bells == [] and notices.dismissed == []


def test_an_abandoned_project_sends_nothing(monkeypatch, notices):
    _assignees(monkeypatch, "e1")
    lc.notify_due_changed({**PROJECT, "abandoned_at": "2026-11-01T00:00:00+00:00"})
    assert notices.bells == []


def test_one_failed_notice_never_stops_the_rest_or_raises(monkeypatch, notices):
    _assignees(monkeypatch, "e1", "e2")
    sent = []

    def _notify(uid, *a, **k):
        if uid == "e1":
            raise RuntimeError("postgrest blip")
        sent.append(uid)

    monkeypatch.setattr(lc, "notify_user", _notify)
    lc.notify_due_changed(PROJECT)
    assert sent == ["e2"]


def test_a_failed_lookup_never_raises(monkeypatch, notices):
    def _boom(pid):
        raise RuntimeError("down")

    monkeypatch.setattr(lc, "_active_assignees", _boom)
    lc.notify_due_changed(PROJECT)
    assert notices.bells == []


def test_the_notice_is_titled_and_swept_like_other_estimator_notices():
    assert notification_email._TYPE_META[lc.DUE_CHANGED_TYPE] == (
        "Estimate due date changed", "Open your assignment",
    )
    # Abandon clears it with the rest of the estimator's bells for the project.
    assert lc.DUE_CHANGED_TYPE in ESTIMATOR_NOTIFICATION_TYPES


# ── the PATCH wiring ───────────────────────────────────────────────────────


def _writer():
    return CurrentUser(
        id="u1", email="pa@g3.com", role=Role.ESTIMATING_ADMIN, is_active=True,
        aal="aal2", mfa_enrolled=True,
    )


def _patch_router(monkeypatch, db):
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(proj_mod, "audit", lambda *a, **k: None)
    monkeypatch.setattr(proj_mod, "_present", lambda row, role, *a: row)
    monkeypatch.setattr(proj_mod, "_settle_rfp_intake", lambda *a, **k: False)
    told = []
    monkeypatch.setattr(proj_mod.estimator_lifecycle, "notify_due_changed", told.append)
    return told


def test_patch_that_moves_the_estimator_due_date_tells_the_estimator(monkeypatch):
    db = _FakeDB()
    db.queue("projects", "select", [{"due_from_estimator_at": ORIGINAL}])
    db.queue("projects", "update", [{**PROJECT, "due_from_estimator_at": REVISION}])
    told = _patch_router(monkeypatch, db)

    proj_mod.update_project("p1", ProjectUpdate(due_from_estimator_at=REVISION), _writer())

    assert told == [{**PROJECT, "due_from_estimator_at": REVISION}]


def test_patch_that_resends_the_same_date_is_silent(monkeypatch):
    # Edit project details saves every field of the form, changed or not.
    db = _FakeDB()
    db.queue("projects", "select", [{"due_from_estimator_at": REVISION}])
    db.queue("projects", "update", [{**PROJECT, "due_from_estimator_at": REVISION}])
    told = _patch_router(monkeypatch, db)

    proj_mod.update_project(
        "p1", ProjectUpdate(due_from_estimator_at="2026-11-19T14:00:00-08:00"), _writer()
    )

    assert told == []


def test_patch_without_the_date_never_reads_it_or_tells_anyone(monkeypatch):
    db = _FakeDB()
    db.queue("projects", "update", [{**PROJECT, "address": "1 Main St"}])
    told = _patch_router(monkeypatch, db)

    proj_mod.update_project("p1", ProjectUpdate(address="1 Main St"), _writer())

    assert db.ops("projects", "select") == []
    assert told == []
