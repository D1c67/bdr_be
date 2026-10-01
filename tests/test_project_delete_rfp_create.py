# ruff: noqa: F811 - the imported fixtures are parameters by design
"""The deleted-project block inside the RFP creation service
(docs/PROJECT_DELETE.md 5), on the fixtures of tests/test_rfp_create: the
button path and the pipeline path both refuse a source whose project was
deleted, name the deleted project, stamp the row so the screens can say
why, and never insert a project. A restored source (the mark lifted)
creates or links as before.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.services import rfp_create as rc
from tests.test_rfp_create import (
    E1,
    HV,
    NOW,
    _email,
    _email_row,
    _harvest,
    _invitation,
    _projects,
)
# The creation service's fixtures, used here by name (pytest finds them in
# this module's namespace; `_env` is autouse).
from tests.test_rfp_create import _env, db, rec  # noqa: F401

A1 = "7b000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """The tombstone check's window reads the clock: pin it to the fixtures' NOW."""
    from app.services import project_delete

    monkeypatch.setattr(project_delete, "_now", lambda: NOW)


def _archive(**over):
    row = {
        "id": A1, "project_id": "p-gone", "project_number": "26.9.7120",
        "project_name": "Warehouse HVAC", "reason": "rfp_should_not_exist", "note": None,
        "deleted_by": None, "deleted_at": NOW.isoformat(), "restored_at": None,
        "test_session_id": None, "gc_ids": ["gc-1"], "gc_links": [],
        "project_bid_at": "2026-10-01T21:00:00+00:00",
        "project_row": {"name": "Warehouse HVAC", "number": "26.9.7120",
                        "actual_bid_at": "2026-10-01T21:00:00+00:00", "internal_bid_at": None},
    }
    row.update(over)
    return row


@pytest.mark.parametrize("automatic", [True, False])
def test_an_email_whose_harvest_made_a_deleted_project_never_creates_again(db, rec, automatic):
    db.tables["deleted_projects"] = [_archive()]
    db.tables["rfp_harvests"].append(_harvest(create_blocked_archive_id=A1))
    db.tables["rfp_emails"].append(_email(harvest_id=HV, status="create" if automatic else "done"))
    with pytest.raises(rc.CreateBlocked) as exc:
        rc.create_from_email(db, _email_row(db), actor_id=None if automatic else "u1", automatic=automatic)
    assert exc.value.archive_id == A1 and "26.9.7120 Warehouse HVAC" in str(exc.value)
    assert isinstance(exc.value, rc.CreateRefused)   # the routers' existing 409
    assert _projects(db) == [] and db.tables["rfp_created_projects"] == []
    assert _email_row(db)["create_blocked_archive_id"] == A1


def test_a_reminder_on_a_blocked_row_is_refused_before_the_already_created_check(db, rec):
    """A source row that made the project sits at `created` with its project
    id nulled by the delete: the refusal names the deletion, not "already
    created"."""
    db.tables["deleted_projects"] = [_archive()]
    db.tables["rfp_emails"].append(_email(status="created", create_blocked_archive_id=A1))
    with pytest.raises(rc.CreateBlocked):
        rc.create_from_email(db, _email_row(db), actor_id="u1", automatic=False)


def test_an_email_the_matcher_would_link_to_a_deleted_project_is_blocked(db, rec):
    """No shared harvest, no copy: a later invitation for the same bid
    (same name, same bid date) meets the tombstone check."""
    db.tables["deleted_projects"] = [_archive(project_bid_at=(NOW + timedelta(days=15)).isoformat())]
    db.tables["rfp_emails"].append(_email())
    with pytest.raises(rc.CreateBlocked) as exc:
        rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert exc.value.archive_id == A1 and _projects(db) == []


def test_a_restored_source_creates_as_before(db, rec):
    db.tables["deleted_projects"] = [_archive(restored_at=NOW.isoformat())]
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False and len(_projects(db)) == 1


def test_an_unrelated_email_is_not_blocked_by_someone_elses_deletion(db, rec):
    db.tables["deleted_projects"] = [_archive(project_name="Durango Library",
                                              project_row={"name": "Durango Library",
                                                           "number": "26.9.7120",
                                                           "actual_bid_at": "2026-10-01T21:00:00+00:00"})]
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.project_id and len(_projects(db)) == 1


def test_a_portal_invitation_whose_project_was_deleted_never_creates_again(db, rec):
    db.tables["deleted_projects"] = [_archive()]
    db.tables["rfp_portal_invitations"].append(_invitation(create_blocked_archive_id=A1))
    inv = db.tables["rfp_portal_invitations"][0]
    with pytest.raises(rc.CreateBlocked):
        rc.create_from_portal(db, dict(inv), actor_id="u1", automatic=False)
    assert _projects(db) == []
    # Its harvest's mark blocks a second sighting sharing the harvest.
    db.tables["rfp_harvests"].append(_harvest(id="hv-ngem", rfp_email_id=None,
                                              portal_invitation_id="inv-2",
                                              create_blocked_archive_id=A1))
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", harvest_id="hv-ngem"))
    with pytest.raises(rc.CreateBlocked):
        rc.create_from_portal(db, dict(db.tables["rfp_portal_invitations"][1]), actor_id=None,
                              automatic=True)
    assert db.tables["rfp_portal_invitations"][1]["create_blocked_archive_id"] == A1


def test_the_email_row_mark_is_read_fresh_when_the_callers_select_lacks_it(db, rec):
    db.tables["deleted_projects"] = [_archive()]
    db.tables["rfp_emails"].append(_email(create_blocked_archive_id=A1))
    row = {k: v for k, v in _email_row(db).items() if k != "create_blocked_archive_id"}
    assert rc.create_block_for(db, rc.SOURCE_EMAIL, row) == A1
    assert E1 == row["id"]
