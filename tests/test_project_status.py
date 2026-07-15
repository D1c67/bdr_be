"""Derived project status (services/project_status) — pure, no DB."""

from app.services.project_status import derive_status


def test_pm_only_is_no_bid():
    # Created directly in PM: never bid, so no bid status applies.
    assert derive_status("pm_only", None, None) == "no_bid"


def test_abandoned_wins_over_pm_only():
    assert derive_status("pm_only", "2026-07-01T00:00:00Z", None) == "abandoned"


def test_abandoned_wins_over_recorded_outcome():
    assert derive_status("bid_outcome", "2026-07-01T00:00:00Z", "won") == "abandoned"


def test_bid_outcome_reflects_recorded_result():
    assert derive_status("bid_outcome", None, "won") == "won"
    assert derive_status("bid_outcome", None, "lost") == "lost"
    assert derive_status("bid_outcome", None, "no_award") == "no_award"


def test_bid_outcome_without_result_stays_sent():
    assert derive_status("bid_outcome", None, None) == "sent"


def test_submitted_is_sent_and_in_flight_is_active():
    assert derive_status("submitted", None, None) == "sent"
    assert derive_status("markup", None, None) == "active"


def test_declined_at_gono():
    assert derive_status("declined", None, None) == "declined"
