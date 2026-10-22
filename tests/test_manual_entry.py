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
                                               once any proposal was sent;
                                               refused over existing `done`
                                               lines; a queued generation
                                               blocks it unless the caller
                                               confirms dropping it (then the
                                               job is canceled). Writers only.
"""

from decimal import Decimal

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from app.core.deps import require_writer
from app.core.roles import Role
from app.models.schemas import (
    BoqConfirmIn,
    BoqGroupMapIn,
    BoqManualGroupIn,
    BoqManualIn,
    BoqManualItemIn,
    ProposalLinesIn,
    ProposalManualIn,
    RFQGroupIn,
    RFQLineItemIn,
)
from app.routers import boq_analysis as boq_router
from app.routers import proposals as proposals_router
from app.services import estimator_rounds, llm_queue, proposal_scope
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
    settings = type("S", (), {"llm_queue_enabled": queue})()
    monkeypatch.setattr(proposals_router, "get_supabase", lambda: db)
    monkeypatch.setattr(proposals_router, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(proposals_router, "get_settings", lambda: settings)
    # fail_if_stale (the manual start's in-flight check) reads and marks rows.
    monkeypatch.setattr(proposal_scope, "get_supabase", lambda: db)
    monkeypatch.setattr(proposal_scope, "get_settings", lambda: settings)
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
    db2 = FakeDB({"proposal_drafts": [], "proposal_sends": []})
    _install_proposals(monkeypatch, db2)
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

    proposals_router.start_manual_lines(
        "p1", ProposalManualIn(replace_generation=True), _user()
    )
    assert canceled == ["job9"]


def test_manual_lines_schema_limits():
    with pytest.raises(ValidationError):
        ProposalManualIn(lines=["a <b> c"])
    with pytest.raises(ValidationError):
        ProposalManualIn(lines=["x" * 501])


def _recent():
    """A created_at inside the stale window, so fail_if_stale keeps the row."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def test_manual_lines_refused_over_existing_done_lines(monkeypatch):
    # The SQL workaround's row shape (model='manual') and a model draft both
    # count: starting over would bury lines the team already has.
    for model in ("manual", "gpt-6"):
        db = FakeDB({
            "proposal_drafts": [
                {"id": "d1", "project_id": "p1", "status": "done", "model": model,
                 "lines_json": ["Furnish and install"], "created_at": "2026-09-26T00:00:00Z"},
            ],
            "proposal_sends": [],
        })
        audits = _install_proposals(monkeypatch, db)
        with pytest.raises(HTTPException) as ei:
            proposals_router.start_manual_lines("p1", ProposalManualIn(), _user())
        assert ei.value.status_code == 409
        assert len(db.tables["proposal_drafts"]) == 1
        assert audits == []


def test_manual_lines_allowed_after_a_failed_generation(monkeypatch):
    db = FakeDB({
        "proposal_drafts": [
            {"id": "f1", "project_id": "p1", "status": "failed", "error": "model down",
             "created_at": "2026-09-26T00:00:00Z"},
        ],
        "proposal_sends": [],
    })
    _install_proposals(monkeypatch, db)
    d = proposals_router.start_manual_lines("p1", ProposalManualIn(), _user())
    assert d["status"] == "done" and d["model"] == "manual"


@pytest.mark.parametrize("state", ["pending", "running"])
def test_manual_lines_refused_while_a_generation_is_in_flight(monkeypatch, state):
    db = FakeDB({
        "proposal_drafts": [
            {"id": "live", "project_id": "p1", "status": state, "created_at": _recent()},
        ],
        "proposal_sends": [],
    })
    _install_proposals(monkeypatch, db, queue=True)
    canceled = []
    monkeypatch.setattr(llm_queue, "active_job", lambda jt, tid: {"id": "job1"})
    monkeypatch.setattr(llm_queue, "cancel", lambda jid: canceled.append(jid))

    with pytest.raises(HTTPException) as ei:
        proposals_router.start_manual_lines("p1", ProposalManualIn(), _user())
    assert ei.value.status_code == 409
    assert canceled == []
    assert [r["id"] for r in db.tables["proposal_drafts"]] == ["live"]

    # Confirmed: the job is dropped and the manual draft lands.
    d = proposals_router.start_manual_lines(
        "p1", ProposalManualIn(replace_generation=True), _user()
    )
    assert d["model"] == "manual"
    assert canceled == ["job1"]


def test_manual_lines_stranded_generation_does_not_block(monkeypatch):
    # A pending row past the stale window with no live job is released by
    # fail_if_stale, so it never blocks manual entry.
    db = FakeDB({
        "proposal_drafts": [
            {"id": "old", "project_id": "p1", "status": "pending",
             "created_at": "2026-01-01T00:00:00Z"},
        ],
        "proposal_sends": [],
    })
    _install_proposals(monkeypatch, db, queue=False)
    d = proposals_router.start_manual_lines("p1", ProposalManualIn(), _user())
    assert d["model"] == "manual"
    assert next(r for r in db.tables["proposal_drafts"] if r["id"] == "old")["status"] == "failed"


def test_manual_lines_draft_takes_line_edits_and_approval(monkeypatch):
    db = FakeDB({"projects": [{"id": "p1"}], "proposal_drafts": [], "proposal_sends": []})
    _install_proposals(monkeypatch, db)
    monkeypatch.setattr(proposals_router, "dismiss_notifications", lambda **k: None)
    d = proposals_router.start_manual_lines("p1", ProposalManualIn(), _user())

    saved = proposals_router.save_lines(
        "p1", d["id"], ProposalLinesIn(lines=["Furnish and install lighting", "Trenching"]),
        _user(),
    )
    assert saved["lines"] == ["Furnish and install lighting", "Trenching"]
    approved = proposals_router.approve_lines("p1", d["id"], _user())
    assert approved["approved_at"] and approved["approved_by"] == "u1"

    # An edit after approval un-approves, exactly as on a model draft.
    again = proposals_router.save_lines(
        "p1", d["id"], ProposalLinesIn(lines=["Trenching"]), _user()
    )
    assert again["approved_at"] is None and again["approved_by"] is None

    # And the send lock applies to it too.
    db.tables["proposal_sends"].append({"id": "s1", "project_id": "p1", "status": "sent"})
    with pytest.raises(HTTPException) as ei:
        proposals_router.save_lines("p1", d["id"], ProposalLinesIn(lines=["X"]), _user())
    assert ei.value.status_code == 409


def test_manual_lines_route_uses_the_line_edit_auth():
    import asyncio

    routes = {
        (m, r.path): r
        for r in proposals_router.router.routes
        for m in getattr(r, "methods", ())
    }
    manual = routes[("POST", "/projects/{project_id}/proposal-lines/manual")]
    save = routes[("PUT", "/projects/{project_id}/proposal-lines/{draft_id}/lines")]
    deps = {d.call for d in manual.dependant.dependencies}
    assert require_writer in deps
    assert require_writer in {d.call for d in save.dependant.dependencies}

    for role in (Role.ACCOUNTANT, Role.ESTIMATOR):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(require_writer(_user(role=role)))
        assert ei.value.status_code == 403
    assert asyncio.run(require_writer(_user())).id == "u1"
