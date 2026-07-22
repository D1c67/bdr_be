"""CP enrollment (services/payroll_projects) — the enroll/unenroll lifecycle.

Enrollment is optimistically locked on cp_enrolled_at IS NULL (the
pm.activate_pm_for_win idiom), compensates the markers when the cp_details
insert fails, and tolerates a retried duplicate insert as already-enrolled.
Unenrollment is refused once any cp_time_entries row references the project.
Uses the shared in-memory fake from test_pm_workflow; install() patches a fixed
module list, so the payroll module is pointed at the fake separately.
"""

from datetime import time

import pytest
from fastapi import HTTPException

from app.models.schemas import CpEnrollBody, CpProjectCreate
from app.services import payroll_projects
from app.services.payroll_projects import (
    create_direct_cp_project,
    enroll_project,
    unenroll_project,
)
from tests.test_pm_workflow import FakeDB, audit_actions, install


def _install(monkeypatch, db):
    install(monkeypatch, db)  # audit writes into the fake via notifications
    monkeypatch.setattr(payroll_projects, "get_supabase", lambda db=db: db)
    return db


def _body(**overrides):
    fields = {
        "contract_id": "C-100",
        "report_type": "lcp_tracker",
        "shift_type": "regular",
        "shift_start_time": time(6, 0),
        "pwp_number": "PWP-2026-001",
        "public_body_awarding_contract": "Clark County",
        "contractor_address_street": "123 Main St",
        "contractor_address_city": "Las Vegas",
        "contractor_address_state": "NV",
        "contractor_address_zip": "89101",
    }
    fields.update(overrides)
    return CpEnrollBody(**fields)


def _db(**tables):
    base = {
        "projects": [{"id": "p1", "name": "Job A", "cp_enrolled_at": None}],
        "cp_details": [],
        "cp_time_entries": [],
    }
    base.update(tables)
    return FakeDB(base)


# ── Enroll ─────────────────────────────────────────────────────────────────────


def test_enroll_happy_path(monkeypatch):
    db = _install(monkeypatch, _db())
    created = enroll_project("p1", _body(), "u1")

    [proj] = db.tables["projects"]
    assert proj["cp_enrolled_at"] is not None
    assert proj["cp_enrolled_by"] == "u1"

    [details] = db.tables["cp_details"]
    assert details["id"] == created["id"]
    assert details["project_id"] == "p1"
    assert details["contract_id"] == "C-100"
    assert details["report_type"] == "lcp_tracker"
    assert details["shift_type"] == "regular"
    assert details["shift_start_time"] == "06:00:00"  # time → JSON string
    assert details["pwp_number"] == "PWP-2026-001"
    assert details["contractor_address_state"] == "NV"
    assert "cp.enroll" in audit_actions(db)


def test_enroll_unknown_project_is_404(monkeypatch):
    db = _install(monkeypatch, _db())
    with pytest.raises(HTTPException) as ei:
        enroll_project("ghost", _body(), "u1")
    assert ei.value.status_code == 404
    assert db.tables["cp_details"] == []


def test_double_enroll_is_409(monkeypatch):
    db = _install(monkeypatch, _db())
    enroll_project("p1", _body(), "u1")
    with pytest.raises(HTTPException) as ei:
        enroll_project("p1", _body(), "u2")
    assert ei.value.status_code == 409
    assert len(db.tables["cp_details"]) == 1  # no second row
    assert db.tables["projects"][0]["cp_enrolled_by"] == "u1"


def test_enroll_race_lost_lock_is_409(monkeypatch):
    # The optimistic update loses (cp_enrolled_at flipped between the existence
    # check and the update) — no cp_details row, no audit.
    db = _install(monkeypatch, _db())
    db.update_returns_empty.add("projects")
    with pytest.raises(HTTPException) as ei:
        enroll_project("p1", _body(), "u1")
    assert ei.value.status_code == 409
    assert db.tables["cp_details"] == []
    assert audit_actions(db) == []


def test_enroll_details_failure_compensates_markers(monkeypatch):
    db = _install(monkeypatch, _db())
    db.raise_on_insert["cp_details"] = RuntimeError("cp_details insert exploded")
    with pytest.raises(RuntimeError, match="cp_details insert exploded"):
        enroll_project("p1", _body(), "u1")
    [proj] = db.tables["projects"]
    assert proj["cp_enrolled_at"] is None  # markers cleared — no half-enrollment
    assert proj["cp_enrolled_by"] is None
    assert audit_actions(db) == []


def test_enroll_duplicate_details_is_409_and_keeps_markers(monkeypatch):
    # A retried insert against the unique project_id: the row already exists,
    # so the enrollment stands (markers stay) and the caller sees 409.
    db = _install(monkeypatch, _db())
    db.raise_on_insert["cp_details"] = Exception(
        'duplicate key value violates unique constraint "cp_details_project_id_key"'
    )
    with pytest.raises(HTTPException) as ei:
        enroll_project("p1", _body(), "u1")
    assert ei.value.status_code == 409
    assert db.tables["projects"][0]["cp_enrolled_at"] is not None


# ── Direct create (project born inside Certified Payroll) ───────────────────────


def _create_body(**overrides):
    fields = {
        "name": "New PW Job",
        "number": "7001",
        "address": "500 S 4th St, Las Vegas, NV",
        "contract_id": "C-700",
        "report_type": "comply",
        "shift_type": "four_tens",
        "shift_start_time": time(5, 30),
        "pwp_number": "PWP-2026-700",
        "public_body_awarding_contract": "City of Henderson",
        "contractor_address_street": "123 Main St",
        "contractor_address_city": "Las Vegas",
        "contractor_address_state": "NV",
        "contractor_address_zip": "89101",
    }
    fields.update(overrides)
    return CpProjectCreate(**fields)


def test_create_direct_happy_path(monkeypatch):
    # Empty projects table — the project is born here, not enrolled from a bid.
    db = _install(monkeypatch, FakeDB({"projects": [], "cp_details": []}))
    created = create_direct_cp_project(_create_body(), "u1")

    [proj] = db.tables["projects"]
    assert proj["id"] == created["id"]
    assert proj["name"] == "New PW Job"
    assert proj["number"] == "7001"
    assert proj["address"] == "500 S 4th St, Las Vegas, NV"
    assert proj["current_stage"] == "cp_only"  # never a bid — off bidding surfaces
    assert proj["current_owner_role"] is None
    assert proj["cp_enrolled_at"] is not None  # enrolled in the same shot
    assert proj["cp_enrolled_by"] == "u1"
    assert proj["created_by"] == "u1"

    [details] = db.tables["cp_details"]
    assert details["project_id"] == created["id"]
    assert details["contract_id"] == "C-700"
    assert details["report_type"] == "comply"
    assert details["shift_type"] == "four_tens"
    assert details["shift_start_time"] == "05:30:00"  # time → JSON string
    assert details["contractor_address_zip"] == "89101"
    # Spine-only fields must NOT leak into cp_details columns.
    assert "name" not in details and "number" not in details and "address" not in details
    assert "cp.project_create" in audit_actions(db)


def test_create_direct_details_failure_compensates(monkeypatch):
    # cp_details insert explodes AFTER the projects insert — the project must be
    # rolled back so no stage='cp_only' row is stranded without its detail record.
    db = _install(monkeypatch, FakeDB({"projects": [], "cp_details": []}))
    db.raise_on_insert["cp_details"] = RuntimeError("cp_details insert exploded")
    with pytest.raises(RuntimeError, match="cp_details insert exploded"):
        create_direct_cp_project(_create_body(), "u1")
    assert db.tables["projects"] == []  # compensating delete ran
    assert db.tables["cp_details"] == []
    assert audit_actions(db) == []


# ── Unenroll ───────────────────────────────────────────────────────────────────


def _enrolled_db(**tables):
    base = {
        "projects": [
            {
                "id": "p1",
                "name": "Job A",
                "cp_enrolled_at": "2026-07-01T00:00:00+00:00",
                "cp_enrolled_by": "u1",
            }
        ],
        "cp_details": [{"id": "d1", "project_id": "p1", "contract_id": "C-100"}],
        "cp_time_entries": [],
    }
    base.update(tables)
    return FakeDB(base)


def test_unenroll_happy_path(monkeypatch):
    db = _install(monkeypatch, _enrolled_db())
    unenroll_project("p1", "u2")
    assert db.tables["cp_details"] == []
    [proj] = db.tables["projects"]
    assert proj["cp_enrolled_at"] is None
    assert proj["cp_enrolled_by"] is None
    assert "cp.unenroll" in audit_actions(db)


def test_unenroll_blocked_by_time_entries_409(monkeypatch):
    db = _install(
        monkeypatch,
        _enrolled_db(
            cp_time_entries=[{"id": "t1", "project_id": "p1", "payroll_report_id": "r1"}]
        ),
    )
    with pytest.raises(HTTPException) as ei:
        unenroll_project("p1", "u2")
    assert ei.value.status_code == 409
    assert db.tables["cp_details"] != []  # nothing deleted
    assert db.tables["projects"][0]["cp_enrolled_at"] is not None
    assert "cp.unenroll" not in audit_actions(db)


def test_unenroll_not_enrolled_is_404(monkeypatch):
    db = _install(monkeypatch, _db())
    with pytest.raises(HTTPException) as ei:
        unenroll_project("p1", "u2")
    assert ei.value.status_code == 404
    assert db.tables["projects"][0].get("cp_enrolled_at") is None
