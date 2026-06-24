"""Unit tests for the Win/Loss (bid outcome) pure logic — no DB.

Covers the snapshot of what we bid, the grid merge of recorded outcomes onto the
GCs we bid to, the "did we actually win the work" derivation, and the new
submitted → bid_outcome workflow transition.
"""

from decimal import Decimal

from app.services import outcome
from app.services.workflow import STAGES, can_transition, owner_role_for


# ── our_amount_of ────────────────────────────────────────────────────────────


def test_our_amount_sums_material_and_labor():
    assert outcome.our_amount_of("40000", "41950") == Decimal("81950")


def test_our_amount_treats_missing_half_as_zero():
    assert outcome.our_amount_of("40000", None) == Decimal("40000")
    assert outcome.our_amount_of(None, "950") == Decimal("950")


def test_our_amount_is_none_only_when_both_absent():
    # Legacy proposal_sends rows generated before per-GC amounts existed.
    assert outcome.our_amount_of(None, None) is None


# ── won_via_us ───────────────────────────────────────────────────────────────


def test_won_via_us_true_when_a_gc_won_and_used_us():
    rows = [
        {"gc_award_result": "lost", "our_bid_selection": "used_us"},
        {"gc_award_result": "won", "our_bid_selection": "used_us"},
    ]
    assert outcome.won_via_us(rows) is True


def test_won_via_us_false_when_winner_used_a_competitor():
    # The user's scenario: a GC we bid to won the job but went with someone else.
    rows = [{"gc_award_result": "won", "our_bid_selection": "used_other"}]
    assert outcome.won_via_us(rows) is False


def test_won_via_us_false_when_only_a_loser_chose_us():
    # The other scenario: a GC chose us but lost the job — no work, not a win.
    rows = [{"gc_award_result": "lost", "our_bid_selection": "used_us"}]
    assert outcome.won_via_us(rows) is False


# ── merge_gc_outcomes ────────────────────────────────────────────────────────


def test_merge_defaults_unrecorded_gcs_to_unknown():
    sent = [{"gc_id": "g1", "gc_name": "Acme", "our_amount": Decimal("81950"), "emails": ["a@x.com"]}]
    merged = outcome.merge_gc_outcomes(sent, [])
    assert merged == [
        {
            "gc_id": "g1",
            "gc_name": "Acme",
            "emails": ["a@x.com"],
            "our_amount": "81950",  # Decimals serialize as strings over the wire
            "gc_award_result": "unknown",
            "our_bid_selection": "unknown",
            "winning_amount": None,
        }
    ]


def test_merge_overlays_recorded_outcome_onto_the_bid_to_gc():
    sent = [{"gc_id": "g1", "gc_name": "Acme", "our_amount": Decimal("81950"), "emails": []}]
    recorded = [
        {
            "gc_id": "g1",
            "gc_award_result": "won",
            "our_bid_selection": "used_other",
            "winning_amount": "79000.00",
        }
    ]
    [row] = outcome.merge_gc_outcomes(sent, recorded)
    assert row["gc_award_result"] == "won"
    assert row["our_bid_selection"] == "used_other"
    assert row["winning_amount"] == "79000.00"
    assert row["our_amount"] == "81950"  # still the snapshot of what we bid


def test_merge_ignores_recorded_rows_for_gcs_we_did_not_bid_to():
    sent = [{"gc_id": "g1", "gc_name": "Acme", "our_amount": None, "emails": []}]
    recorded = [{"gc_id": "ghost", "gc_award_result": "won", "our_bid_selection": "used_us"}]
    merged = outcome.merge_gc_outcomes(sent, recorded)
    assert [r["gc_id"] for r in merged] == ["g1"]
    assert merged[0]["our_amount"] is None


# ── workflow transition ──────────────────────────────────────────────────────


def test_submitted_advances_to_bid_outcome():
    assert can_transition("submitted", "bid_outcome")


def test_bid_outcome_is_terminal():
    assert can_transition("bid_outcome", "submitted") is False
    assert STAGES["bid_outcome"].owner_roles  # PA owns it (can correct)


def test_submitted_now_owned_by_pa():
    # The outstanding task at Submitted is recording the outcome — the PA's job.
    assert owner_role_for("submitted").value == "pa"
    assert owner_role_for("bid_outcome").value == "pa"
