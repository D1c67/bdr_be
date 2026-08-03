"""Bulk material entry (POST /pm/projects/{id}/materials/bulk).

The add-materials modal lets a writer type many lines before saving, so the
router takes the whole batch: every category is validated up front (a typo in
one line must not leave the earlier lines behind), the insert is one call, and
the batch is one audit entry. Uses the shared in-memory fake from
test_pm_workflow.
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import PmMaterialBulkIn, PmMaterialIn
from app.routers import pm_materials
from tests.test_pm_workflow import FakeDB, install

USER = CurrentUser(
    id="u1", email="pm@example.com", role=Role.ESTIMATING_ADMIN, is_active=True,
    is_dev=False, aal="aal2", mfa_enrolled=True,
)


def _install(monkeypatch, db):
    install(monkeypatch, db)
    monkeypatch.setattr(pm_materials, "get_supabase", lambda: db)
    return db


def _db(**tables):
    base = {
        "projects": [{"id": "p1", "name": "Job A", "pm_stage": "precon"}],
        "material_categories": [{"id": "c1", "name": "Conduit", "kind": "material"}],
        "pm_materials": [],
    }
    base.update(tables)
    return FakeDB(base)


def _line(desc, **over):
    fields = {"description": desc, "material_category_id": "c1", "quantity": "10", "unit": "ft"}
    fields.update(over)
    return PmMaterialIn(**fields)


def test_bulk_inserts_every_line_scoped_to_the_project(monkeypatch):
    db = _install(monkeypatch, _db())
    created = pm_materials.create_materials_bulk(
        "p1",
        PmMaterialBulkIn(
            materials=[
                _line("EMT 3/4"),
                _line("EMT 1", quantity=None, unit=None),
                _line("Misc", material_category_id=None, notes="verify"),
            ]
        ),
        USER,
    )

    assert [r["description"] for r in created] == ["EMT 3/4", "EMT 1", "Misc"]
    rows = db.tables["pm_materials"]
    assert len(rows) == 3
    assert {r["project_id"] for r in rows} == {"p1"}
    assert {r["created_by"] for r in rows} == {"u1"}
    assert rows[1]["quantity"] is None and rows[1]["unit"] is None
    assert rows[2]["material_category_id"] is None and rows[2]["notes"] == "verify"

    # One audit row for the batch, carrying the count.
    [entry] = [r for r in db.tables["audit_log"] if r["action"] == "pm_material.create_bulk"]
    assert entry["payload"]["count"] == 3
    assert entry["payload"]["descriptions"][0] == "EMT 3/4"


def test_bulk_unknown_category_is_400_and_inserts_nothing(monkeypatch):
    db = _install(monkeypatch, _db())
    with pytest.raises(HTTPException) as ei:
        pm_materials.create_materials_bulk(
            "p1",
            PmMaterialBulkIn(materials=[_line("EMT 3/4"), _line("Bad", material_category_id="nope")]),
            USER,
        )
    assert ei.value.status_code == 400
    assert db.tables["pm_materials"] == []  # all-or-nothing


def test_bulk_on_a_bid_only_project_is_409(monkeypatch):
    db = _install(monkeypatch, _db(projects=[{"id": "p1", "name": "Bid", "pm_stage": None}]))
    with pytest.raises(HTTPException) as ei:
        pm_materials.create_materials_bulk("p1", PmMaterialBulkIn(materials=[_line("EMT")]), USER)
    assert ei.value.status_code == 409
    assert db.tables["pm_materials"] == []


def test_bulk_batch_is_bounded():
    with pytest.raises(ValidationError):
        PmMaterialBulkIn(materials=[])
    with pytest.raises(ValidationError):
        PmMaterialBulkIn(materials=[_line(f"row {i}") for i in range(201)])
    # The cap itself is fine.
    assert len(PmMaterialBulkIn(materials=[_line(f"row {i}") for i in range(200)]).materials) == 200
