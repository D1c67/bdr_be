"""Unit tests for the pure workflow state-machine logic (no DB)."""

from app.core.roles import Role
from app.services import workflow
from app.services.workflow import (
    STAGES,
    can_transition,
    internal_owner_role_for,
    owner_role_for,
)


def test_linear_pipeline_transitions_are_legal():
    chain = [
        "intake", "go_no_go", "to_estimator", "estimate_received", "rfqs",
        "receive_quotes", "labor_numbers", "markup", "verify", "send_out", "submitted",
    ]
    for a, b in zip(chain, chain[1:]):
        assert can_transition(a, b), f"{a} → {b} should be legal"


def test_go_no_go_can_decline():
    assert can_transition("go_no_go", "declined")


def test_illegal_transitions_rejected():
    assert not can_transition("intake", "rfqs")
    assert not can_transition("submitted", "send_out")  # terminal
    assert not can_transition("declined", "go_no_go")  # terminal
    assert not can_transition("markup", "intake")  # no going backwards


def test_owner_roles():
    assert owner_role_for("intake") == Role.PA
    assert owner_role_for("rfqs") == Role.PE
    assert owner_role_for("verify") == Role.EXECUTIVE
    # submitted is now PA-owned (the PA records the Win/Loss outcome); declined
    # is the only ownerless terminal.
    assert owner_role_for("submitted") == Role.PA
    assert owner_role_for("declined") is None  # terminal, no owner


def test_internal_owner_skips_estimator_for_handoff():
    # estimate_received is co-owned by (ESTIMATOR, PE); a stage handoff must
    # address the internal PE, never broadcast to every external estimator.
    assert owner_role_for("estimate_received") == Role.ESTIMATOR  # access owner
    assert internal_owner_role_for("estimate_received") == Role.PE  # notify target
    # For every other stage the first owner is already internal, so the two agree.
    for stage in STAGES:
        internal = internal_owner_role_for(stage)
        if stage != "estimate_received":
            assert internal == owner_role_for(stage)
        if internal is not None:
            assert internal != Role.ESTIMATOR


def test_every_stage_defined():
    for key, defn in STAGES.items():
        assert defn.key == key
        assert defn.label


def _sweep(monkeypatch, new_stage):
    """Capture the kwargs the stage sweep passes to dismiss_notifications."""
    captured = {}
    monkeypatch.setattr(
        workflow.notifications, "dismiss_notifications",
        lambda **kw: captured.update(kw),
    )
    workflow._dismiss_stale_notifications("p1", new_stage)
    return captured


def test_stage_sweep_clears_bid_due_reminders_on_submit(monkeypatch):
    cap = _sweep(monkeypatch, "submitted")
    types, prefixes = cap["types"], cap["type_prefixes"]
    # Bid-due reminders and "pricing committed" are done once the bid is out…
    assert "due.internal_bid." in prefixes and "due.actual_bid." in prefixes
    assert "verified" in types
    # …but "submitted" (created this same transition) must survive to Win/Loss.
    assert "submitted" not in types
    # The previous stage's handoff is always cleared.
    assert "stage_handoff" in types


def test_stage_sweep_clears_estimate_reminders_when_estimate_received(monkeypatch):
    cap = _sweep(monkeypatch, "estimate_received")
    assert "due.due_from_estimator." in cap["type_prefixes"]
    assert "gono_go" in cap["types"]          # to_estimator(3) < 4
    assert "assigned" not in cap["types"]     # estimate_received(4) not < 4 → survives
    # Vendor-due / quote notifications are not stage-gated here.
    assert "due.due_from_vendors." not in cap["type_prefixes"]


def test_stage_sweep_quote_types_never_stage_gated(monkeypatch):
    # quote.received / rfq.reply_received are dismissed per-RFQ on pricing, so a
    # late quote after advancing still notifies — they must never be in the sweep.
    for stage in ("labor_numbers", "send_out", "submitted", "bid_outcome"):
        cap = _sweep(monkeypatch, stage)
        assert "quote.received" not in cap["types"]
        assert "rfq.reply_received" not in cap["types"]
        assert "estimator_note" not in cap["types"]
