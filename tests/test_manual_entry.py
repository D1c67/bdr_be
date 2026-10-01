"""Manual entry for the two model-driven line steps.

When the LLM instance is down (or a project needs its own list), the team
must still be able to progress:

  POST /projects/{id}/boq-analysis/manual      hand-typed BOQ items land as a
                                               `done` analysis (model='manual')
                                               in the model's result shape, one
                                               group per category, with the
                                               category mapping pinned in the
                                               draft; confirm then creates the
                                               RFQs as usual but skips training
                                               capture; the stale-BOQ check
                                               ignores these rows.
  POST /projects/{id}/proposal-lines/manual    a `done` proposal draft
                                               (model='manual') seeded with the
                                               given lines (usually none) so
                                               the lines editor opens; locked
                                               once any proposal was sent; a
                                               queued generation is canceled.
"""

from decimal import Decimal

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from app.models.schemas import (
    BoqConfirmIn,
    BoqGroupMapIn,
    BoqManualGroupIn,
    BoqManualIn,
    BoqManualItemIn,
    ProposalManualIn,
    RFQGroupIn,
    RFQLineItemIn,
)
from app.routers import boq_analysis as boq_router
from app.routers import proposals as proposals_router
from app.services import estimator_rounds, llm_queue
from tests.test_boq_corrections import FakeDB, _base_tables, _install_confirm, _user


# ── BOQ items by hand ────────────────────────────────────────────────────────


def _manual_body():
    return BoqManualIn(
        groups=[
            BoqManualGroupIn(
                material_category_id="c1",
                items=[
                    BoqManualItemIn(description="  2x4   Troffer ", quantity=Decimal("10"), unit="EA"),
                    BoqManualItemIn(description="Downlight", quantity=Decimal("2.5"), unit=" "),
                ],
            ),
            BoqManualGroupIn(
                material_category_id="c2",
                items=[BoqManualItemIn(description="Panelboard")],
            ),
        ]
    )


def test_manual_boq_lands_as_done_analysis_in_model_shape(monkeypatch):
    db = FakeDB(_base_tables(boq_analyses=[]))
    audits = _install_confirm(monkeypatch, db)
    monkeypatch.setattr(boq_router, "get_settings", lambda: type("S", (), {"llm_queue_enabled": False})())

    row = boq_router.start_manual_analysis("p1", _manual_body(), _user())

    assert row["status"] == "done"
    assert row["model"] == "manual"
    assert row["boq_file_id"] is None
    sites = row["result_json"]["sites"]
    assert len(sites) == 1 and sites[0]["site_name"] is None
    groups = sites[0]["material_groups"]
    assert [g["group_name"] for g in groups] == ["Lighting", "Switchgear"]
    assert groups[0]["items"][0] == {
        "description": "2x4 Troffer", "quantity": 10, "unit": "EA", "notes": None,
    }
    assert groups[0]["items"][1]["quantity"] == 2.5
    assert groups[0]["items"][1]["unit"] is None  # blank unit dropped
    assert row["result_json"]["total_material_count"] == 3
    assert row["draft_json"] == {
        "overrides": [],
        "group_mappings": {"Lighting": "c1", "Switchgear": "c2"},
    }
    assert row["draft_updated_by"] == "u1"
    assert audits and audits[0][1] == "boq.manual"
    # And the row is what /latest now returns.
    latest = boq_router.latest_analysis("p1", _user())
    assert latest["id"] == row["id"]


def test_manual_boq_rejects_unknown_and_duplicate_categories(monkeypatch):
    db = FakeDB(_base_tables(boq_analyses=[]))
    _install_confirm(monkeypatch, db)
    monkeypatch.setattr(boq_router, "get_settings", lambda: type("S", (), {"llm_queue_enabled": False})())

    body = BoqManualIn(groups=[BoqManualGroupIn(
        material_category_id="nope", items=[BoqManualItemIn(description="x")])])
    with pytest.raises(HTTPException) as ei:
        boq_router.start_manual_analysis("p1", body, _user())
    assert ei.value.status_code == 400
    assert db.tables["boq_analyses"] == []

    with pytest.raises(ValidationError):
        BoqManualIn(groups=[
            BoqManualGroupIn(material_category_id="c1", items=[BoqManualItemIn(description="a")]),
            BoqManualGroupIn(material_category_id="c1", items=[BoqManualItemIn(description="b")]),
        ])
    with pytest.raises(ValidationError):
        BoqManualItemIn(description="   ")
    with pytest.raises(ValidationError):
        BoqManualGroupIn(material_category_id="c1", items=[])


def test_manual_boq_cancels_a_queued_model_job(monkeypatch):
    db = FakeDB(_base_tables(boq_analyses=[
        {"id": "pend", "project_id": "p1", "status": "pending", "created_at": "2026-09-26T00:00:00Z"},
    ]))
    _install_confirm(monkeypatch, db)
    monkeypatch.setattr(boq_router, "get_settings", lambda: type("S", (), {"llm_queue_enabled": True})())
    canceled = []
    monkeypatch.setattr(llm_queue, "active_job",
                        lambda jt, tid: {"id": "job1"} if (jt, tid) == (llm_queue.JOB_BOQ, "pend") else None)
    monkeypatch.setattr(llm_queue, "cancel", lambda jid: canceled.append(jid) or {"id": jid})

    boq_router.start_manual_analysis("p1", _manual_body(), _user())
    assert canceled == ["job1"]


def test_manual_boq_confirm_creates_rfqs_but_skips_training_capture(monkeypatch):
    db = FakeDB(_base_tables(boq_analyses=[]))
    _install_confirm(monkeypatch, db)
    monkeypatch.setattr(boq_router, "get_settings", lambda: type("S", (), {"llm_queue_enabled": False})())
    row = boq_router.start_manual_analysis("p1", _manual_body(), _user())

    body = BoqConfirmIn(
        groups=[
            RFQGroupIn(material_category_id="c1", items=[
                RFQLineItemIn(description="2x4 Troffer", quantity=Decimal("10"), unit="EA"),
                RFQLineItemIn(description="Downlight", quantity=Decimal("2.5")),
            ]),
            RFQGroupIn(material_category_id="c2", items=[RFQLineItemIn(description="Panelboard")]),
        ],
        group_mappings=[BoqGroupMapIn(group_name="Lighting", material_category_id="c1"),
                        BoqGroupMapIn(group_name="Switchgear", material_category_id="c2")],
    )
    out = boq_router.confirm_analysis("p1", row["id"], body, BackgroundTasks(), _user())
    assert len(out["created"]) == 2
    assert len(db.tables["rfqs"]) == 2
    assert len(db.tables["rfq_line_items"]) == 3
    assert db.tables["boq_training_examples"] == []


def test_stale_boq_check_ignores_manual_analyses(monkeypatch):
    db = FakeDB({
        "boq_analyses": [
            {"id": "m1", "project_id": "p1", "model": "manual", "boq_file_id": None,
             "created_at": "2026-09-26T00:00:00Z"},
        ],
        "project_files": [{"id": "bf9", "project_id": "p1", "category": "boq"}],
    })
    called = []
    monkeypatch.setattr(estimator_rounds, "_newest_visible",
                        lambda sb, pid, cat: called.append(cat) or {"id": "bf9"})
    assert estimator_rounds._boq_stale(db, "p1") is False
    assert called == []  # short-circuits before looking at files


# ── Scope lines by hand ──────────────────────────────────────────────────────


class _NoQueue:
    llm_queue_enabled = False


def _install_proposals(monkeypatch, db, queue=False):
    audits = []
    monkeypatch.setattr(proposals_router, "get_supabase", lambda: db)
    monkeypatch.setattr(proposals_router, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(proposals_router, "get_settings",
                        lambda: type("S", (), {"llm_queue_enabled": queue})())
    return audits


def test_manual_lines_opens_a_done_draft(monkeypatch):
    db = FakeDB({"proposal_drafts": [], "proposal_sends": []})
    audits = _install_proposals(monkeypatch, db)

    d = proposals_router.start_manual_lines("p1", ProposalManualIn(), _user())
    assert d["status"] == "done"
    assert d["model"] == "manual"
    assert d["boq_file_id"] is None
    assert d["lines"] == []
    assert d["result_json"] == {"source": "manual", "notes": None}
    assert d.get("approved_at") is None
    assert audits[0][1] == "proposal.lines_manual"

    # Approve refuses an empty list: the team has to type and save first.
    with pytest.raises(HTTPException) as ei:
        proposals_router.approve_lines("p1", d["id"], _user())
    assert ei.value.status_code == 409

    # Seeded lines are cleaned like edits, blanks dropped rather than rejected.
    d2 = proposals_router.start_manual_lines(
        "p1", ProposalManualIn(lines=["  Furnish   and install ", "", "   "]), _user()
    )
    assert d2["lines"] == ["Furnish and install"]


def test_manual_lines_locked_once_a_proposal_went_out(monkeypatch):
    db = FakeDB({
        "proposal_drafts": [],
        "proposal_sends": [{"id": "s1", "project_id": "p1", "status": "sent"}],
    })
    _install_proposals(monkeypatch, db)
    with pytest.raises(HTTPException) as ei:
        proposals_router.start_manual_lines("p1", ProposalManualIn(), _user())
    assert ei.value.status_code == 409
    assert db.tables["proposal_drafts"] == []


def test_manual_lines_cancels_a_queued_generation(monkeypatch):
    db = FakeDB({
        "proposal_drafts": [
            {"id": "pend", "project_id": "p1", "status": "pending", "created_at": "2026-09-26T00:00:00Z"},
        ],
        "proposal_sends": [],
    })
    _install_proposals(monkeypatch, db, queue=True)
    canceled = []
    monkeypatch.setattr(llm_queue, "active_job",
                        lambda jt, tid: {"id": "job9"} if (jt, tid) == (llm_queue.JOB_PROPOSAL, "pend") else None)
    monkeypatch.setattr(llm_queue, "cancel", lambda jid: canceled.append(jid) or {"id": jid})

    proposals_router.start_manual_lines("p1", ProposalManualIn(), _user())
    assert canceled == ["job9"]


def test_manual_lines_schema_limits():
    with pytest.raises(ValidationError):
        ProposalManualIn(lines=["a <b> c"])
    with pytest.raises(ValidationError):
        ProposalManualIn(lines=["x" * 501])
