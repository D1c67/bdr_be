"""Direct creation in PM (services/pm.create_direct_project).

A never-bid project: current_stage='pm_only', pm_origin='direct', zero
stage_events (the bidding lifecycle must stay untouched), one pm_stage_events
row, and a compensating delete when seeding fails after the projects insert.
Uses the shared in-memory fake from test_pm_workflow.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.schemas import PMProjectCreate
from app.services.pm import create_direct_project
from tests.test_pm_workflow import FakeDB, audit_actions, install


def _body(**overrides):
    fields = {
        "name": "Warehouse TI",
        "number": "PM-100",
        "initial_stage": "active_construction",
        "customer_gc_id": "g1",
        "customer_name": "Acme Builders",
        "original_contract_value": Decimal("125000.50"),
        "awarded_at": date(2026, 5, 1),
        "planned_start_date": date(2026, 6, 1),
        "planned_finish_date": date(2026, 11, 30),
    }
    fields.update(overrides)
    return PMProjectCreate(**fields)


def _db(**tables):
    base = {
        "general_contractors": [{"id": "g1", "name": "Acme Builders"}],
        "stage_events": [],
    }
    base.update(tables)
    return FakeDB(base)


def test_direct_create_happy_path(monkeypatch):
    db = install(monkeypatch, _db())
    created = create_direct_project(_body(), "u1")

    [proj] = db.tables["projects"]
    assert proj["id"] == created["id"]
    assert proj["current_stage"] == "pm_only"
    assert proj["current_owner_role"] is None
    assert proj["pm_origin"] == "direct"
    assert proj["pm_stage"] == "active_construction"  # onboarding mid-flight honored
    assert proj["created_by"] == "u1"

    [details] = db.tables["pm_details"]
    assert details["project_id"] == created["id"]
    assert details["customer_gc_id"] == "g1"
    assert details["original_contract_value"] == "125000.50"  # Decimal → string
    assert details["awarded_at"] == "2026-05-01"
    assert details["planned_start_date"] == "2026-06-01"
    assert details["planned_finish_date"] == "2026-11-30"
    assert details["activated_by"] == "u1"

    [ev] = db.tables["pm_stage_events"]
    assert (ev["from_stage"], ev["to_stage"]) == (None, "active_construction")
    assert db.tables["stage_events"] == []  # never enters the bidding pipeline
    assert "pm.project_create" in audit_actions(db)


def test_direct_create_defaults_to_precon(monkeypatch):
    db = install(monkeypatch, _db())
    create_direct_project(
        PMProjectCreate(name="Small Job", number="PM-101", customer_gc_id=None), "u1"
    )
    assert db.tables["projects"][0]["pm_stage"] == "precon"
    assert db.tables["pm_details"][0]["original_contract_value"] is None


def test_direct_create_without_customer_gc_skips_validation(monkeypatch):
    # No general_contractors seeded at all: a null customer must not 404.
    db = install(monkeypatch, FakeDB({"stage_events": []}))
    create_direct_project(_body(customer_gc_id=None), "u1")
    assert db.tables["pm_details"][0]["customer_gc_id"] is None


def test_direct_create_unknown_customer_gc_404(monkeypatch):
    db = install(monkeypatch, _db())
    with pytest.raises(HTTPException) as ei:
        create_direct_project(_body(customer_gc_id="ghost"), "u1")
    assert ei.value.status_code == 404
    assert db.tables.get("projects", []) == []  # validated before any insert


def test_direct_create_duplicate_number_propagates(monkeypatch):
    # The router translates 23505 into a 409; the service must let it surface.
    db = install(monkeypatch, _db())
    db.raise_on_insert["projects"] = Exception(
        'duplicate key value violates unique constraint "projects_number_unique_idx"'
    )
    with pytest.raises(Exception, match="projects_number_unique_idx"):
        create_direct_project(_body(), "u1")
    assert db.tables.get("pm_details", []) == []


def test_direct_create_compensating_delete_on_details_failure(monkeypatch):
    db = install(monkeypatch, _db())
    db.raise_on_insert["pm_details"] = RuntimeError("pm_details insert exploded")
    with pytest.raises(RuntimeError, match="pm_details insert exploded"):
        create_direct_project(_body(), "u1")
    assert db.tables["projects"] == []  # no orphan half-created project
    assert audit_actions(db) == []


def test_direct_create_compensating_delete_on_event_failure(monkeypatch):
    db = install(monkeypatch, _db())
    db.raise_on_insert["pm_stage_events"] = RuntimeError("event insert exploded")
    with pytest.raises(RuntimeError, match="event insert exploded"):
        create_direct_project(_body(), "u1")
    assert db.tables["projects"] == []
