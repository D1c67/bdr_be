"""The bidding → PM seam (services/pm activation + services/outcome corrections).

A recorded WON activates PM at Preconstruction, seeded from what the bid knows;
correcting away from won auto-retracts an untouched Precon shell and is refused
once any PM work exists. Uses the shared in-memory fake from test_pm_workflow.
"""

from datetime import datetime, timezone

import pytest

from app.models.schemas import BidGcOutcomeIn, BidOutcomeIn
from app.services import pm
from app.services.outcome import OutcomeError, record_outcome
from tests.test_pm_workflow import FakeDB, audit_actions, install


def _bid_project(**overrides):
    row = {
        "id": "p1",
        "name": "Job A",
        "current_stage": "bid_outcome",
        "pm_stage": None,
        "pm_origin": None,
        "pm_completed_at": None,
        "abandoned_at": None,
        "est_start_date": "2026-08-01",
        "est_finish_date": "2026-12-15",
    }
    row.update(overrides)
    return row


def _sent(gc_id="g1", material="4000.00", labor="711.50"):
    return {
        "project_id": "p1",
        "gc_id": gc_id,
        "gc_name": "Acme Builders",
        "status": "sent",
        "material_amount": material,
        "labor_amount": labor,
    }


# ── activate_pm_for_win: seeding ───────────────────────────────────────────────


def test_activation_seeds_details_from_gc_outcome_snapshot(monkeypatch):
    db = install(monkeypatch, FakeDB({
        "projects": [_bid_project()],
        "bid_gc_outcomes": [{"project_id": "p1", "gc_id": "g1", "our_amount": "4711.50"}],
    }))
    assert pm.activate_pm_for_win("p1", "u1", "g1") is True

    proj = db.tables["projects"][0]
    assert proj["pm_stage"] == "precon"
    assert proj["pm_origin"] == "bid"

    [details] = db.tables["pm_details"]
    assert details["customer_gc_id"] == "g1"
    assert details["original_contract_value"] == "4711.50"  # Decimal → string
    assert details["awarded_at"] == datetime.now(timezone.utc).date().isoformat()
    assert details["planned_start_date"] == "2026-08-01"
    assert details["planned_finish_date"] == "2026-12-15"
    assert details["activated_by"] == "u1"

    [ev] = db.tables["pm_stage_events"]
    assert (ev["from_stage"], ev["to_stage"]) == (None, "precon")
    assert "pm.activate" in audit_actions(db)


def test_activation_falls_back_to_proposal_send_amounts(monkeypatch):
    db = install(monkeypatch, FakeDB({
        "projects": [_bid_project()],
        "proposal_sends": [_sent(material="4000.00", labor="711.50")],
    }))
    assert pm.activate_pm_for_win("p1", "u1", "g1") is True
    assert db.tables["pm_details"][0]["original_contract_value"] == "4711.50"


def test_activation_contract_value_null_when_winner_unknown(monkeypatch):
    db = install(monkeypatch, FakeDB({"projects": [_bid_project()]}))
    assert pm.activate_pm_for_win("p1", "u1", None) is True
    [details] = db.tables["pm_details"]
    assert details["original_contract_value"] is None
    assert details["customer_gc_id"] is None


def test_activation_is_idempotent(monkeypatch):
    db = install(monkeypatch, FakeDB({
        "projects": [_bid_project()],
        "bid_gc_outcomes": [{"project_id": "p1", "gc_id": "g1", "our_amount": "100"}],
    }))
    assert pm.activate_pm_for_win("p1", "u1", "g1") is True
    assert pm.activate_pm_for_win("p1", "u1", "g1") is False
    assert len(db.tables["pm_stage_events"]) == 1
    assert len(db.tables["pm_details"]) == 1


def test_abandoned_project_is_not_activated(monkeypatch):
    db = install(monkeypatch, FakeDB({
        "projects": [_bid_project(abandoned_at="2026-06-01T00:00:00Z")],
    }))
    assert pm.activate_pm_for_win("p1", "u1", "g1") is False
    assert db.tables["projects"][0]["pm_stage"] is None
    assert db.tables.get("pm_details", []) == []


def test_activation_lost_race_returns_false(monkeypatch):
    db = install(monkeypatch, FakeDB({"projects": [_bid_project()]}))
    db.update_returns_empty.add("projects")
    assert pm.activate_pm_for_win("p1", "u1", None) is False
    assert db.tables.get("pm_stage_events", []) == []


# ── activate_pm_if_won (reactivate path) ───────────────────────────────────────


def test_activate_if_won_reads_outcome_and_activates(monkeypatch):
    db = install(monkeypatch, FakeDB({
        "projects": [_bid_project()],
        "bid_outcomes": [{"project_id": "p1", "result": "won", "winning_gc_id": "g1"}],
        "bid_gc_outcomes": [{"project_id": "p1", "gc_id": "g1", "our_amount": "250000"}],
    }))
    assert pm.activate_pm_if_won("p1", "u1") is True
    assert db.tables["projects"][0]["pm_stage"] == "precon"
    assert db.tables["pm_details"][0]["original_contract_value"] == "250000"


def test_activate_if_won_ignores_lost(monkeypatch):
    db = install(monkeypatch, FakeDB({
        "projects": [_bid_project()],
        "bid_outcomes": [{"project_id": "p1", "result": "lost", "winning_gc_id": None}],
    }))
    assert pm.activate_pm_if_won("p1", "u1") is False
    assert db.tables["projects"][0]["pm_stage"] is None


def test_activate_if_won_no_outcome_row(monkeypatch):
    db = install(monkeypatch, FakeDB({"projects": [_bid_project()]}))
    assert pm.activate_pm_if_won("p1", "u1") is False
    assert db.tables["projects"][0]["pm_stage"] is None


# ── record_outcome corrections ─────────────────────────────────────────────────


def _won_in_pm_db(extra=None):
    """A project whose won outcome already activated PM (untouched Precon shell)."""
    tables = {
        "projects": [_bid_project(pm_stage="precon", pm_origin="bid")],
        "bid_outcomes": [{"project_id": "p1", "result": "won", "winning_gc_id": "g1"}],
        "bid_gc_outcomes": [{"project_id": "p1", "gc_id": "g1", "our_amount": "4711.50"}],
        "pm_details": [{"project_id": "p1", "customer_gc_id": "g1"}],
        "pm_stage_events": [{"project_id": "p1", "from_stage": None, "to_stage": "precon"}],
        "proposal_sends": [_sent()],
    }
    tables.update(extra or {})
    return FakeDB(tables)


def test_rerecord_won_does_not_duplicate_activation(monkeypatch):
    db = install(monkeypatch, _won_in_pm_db())
    body = BidOutcomeIn(
        result="won",
        winning_gc_id="g1",
        gcs=[BidGcOutcomeIn(gc_id="g1", gc_award_result="won", our_bid_selection="used_us")],
    )
    record_outcome("p1", "u1", body)
    assert len(db.tables["pm_stage_events"]) == 1
    assert len(db.tables["pm_details"]) == 1
    assert len(db.tables["bid_outcomes"]) == 1  # upsert, not a second row
    assert len(db.tables["bid_gc_outcomes"]) == 1


def test_won_to_lost_retracts_untouched_precon_shell(monkeypatch):
    db = install(monkeypatch, _won_in_pm_db())
    record_outcome("p1", "u1", BidOutcomeIn(result="lost"))

    proj = db.tables["projects"][0]
    assert proj["pm_stage"] is None
    assert proj["pm_origin"] is None
    assert db.tables["pm_details"] == []
    assert db.tables["pm_stage_events"] == []
    assert db.tables["bid_outcomes"][0]["result"] == "lost"
    assert "pm.retract" in audit_actions(db)


def test_won_to_lost_blocked_by_pm_work(monkeypatch):
    db = install(monkeypatch, _won_in_pm_db({
        "change_orders": [{"id": "co1", "project_id": "p1", "amount": "5000"}],
        "profiles": [{"id": "exec1", "role": "executive", "is_active": True}],
    }))
    with pytest.raises(OutcomeError) as ei:
        record_outcome("p1", "u1", BidOutcomeIn(result="lost"))
    assert ei.value.status_code == 409

    # Nothing mutated, and the executive was warned.
    assert db.tables["bid_outcomes"][0]["result"] == "won"
    assert db.tables["projects"][0]["pm_stage"] == "precon"
    assert len(db.tables["pm_details"]) == 1
    assert any(
        n["type"] == "pm_outcome_conflict" for n in db.tables.get("notifications", [])
    )


def test_won_to_lost_blocked_after_a_stage_move(monkeypatch):
    # >1 pm_stage_events means the project moved past activation — PM work exists.
    db = install(monkeypatch, _won_in_pm_db({
        "projects": [_bid_project(pm_stage="precon", pm_origin="bid")],
        "pm_stage_events": [
            {"project_id": "p1", "from_stage": None, "to_stage": "precon"},
            {"project_id": "p1", "from_stage": "precon", "to_stage": "active_construction"},
        ],
    }))
    with pytest.raises(OutcomeError) as ei:
        record_outcome("p1", "u1", BidOutcomeIn(result="lost"))
    assert ei.value.status_code == 409
    assert db.tables["projects"][0]["pm_stage"] == "precon"
    assert len(db.tables["pm_stage_events"]) == 2


def test_lost_correction_without_pm_life_is_plain(monkeypatch):
    # A won never activated (pm_stage None) corrects freely — no retract branch.
    db = install(monkeypatch, FakeDB({
        "projects": [_bid_project()],
        "bid_outcomes": [{"project_id": "p1", "result": "won", "winning_gc_id": None}],
    }))
    record_outcome("p1", "u1", BidOutcomeIn(result="no_award"))
    assert db.tables["bid_outcomes"][0]["result"] == "no_award"
    assert "pm.retract" not in audit_actions(db)


# ── is_retractable ─────────────────────────────────────────────────────────────


def test_is_retractable_false_when_completed(monkeypatch):
    install(monkeypatch, FakeDB({"pm_stage_events": []}))
    project = {"id": "p1", "pm_stage": "precon", "pm_completed_at": "2026-07-01T00:00:00Z"}
    assert pm.is_retractable(project) is False


def test_is_retractable_true_for_untouched_shell(monkeypatch):
    install(monkeypatch, FakeDB({
        "pm_stage_events": [{"project_id": "p1", "from_stage": None, "to_stage": "precon"}],
    }))
    assert pm.is_retractable({"id": "p1", "pm_stage": "precon", "pm_completed_at": None}) is True


def test_is_retractable_false_once_module_rows_exist(monkeypatch):
    install(monkeypatch, FakeDB({
        "pm_stage_events": [{"project_id": "p1", "from_stage": None, "to_stage": "precon"}],
        "daily_logs": [{"id": "d1", "project_id": "p1"}],
    }))
    assert pm.is_retractable({"id": "p1", "pm_stage": "precon", "pm_completed_at": None}) is False
