"""Unit tests for the score-driven Go/No-Go logic (no DB).

The rubric points mirror bdr_fe/lib/gonoScoring.ts — if these fail after a
rubric tweak, re-sync the two files.
"""

from datetime import datetime, timedelta, timezone

from app.services.gono import (
    ANSWER_POINTS,
    THRESHOLDS,
    _bid_days_points,
    compute_score,
    outcome_for_score,
)

# Highest-scoring answer per question (45 pts) — add a 30+ day bid date for 50.
BEST_ANSWERS = {
    "project_type": "new_construction",  # 5
    "owner_type": "rtc",                 # 5
    "labor_needed": "ce_cw",             # 5
    "bid_method": "cmar",                # 5
    "competitor_known": "only_ec_bidding",  # 5
    "gc_known": "no_gc_needed",          # 5
    "subs_needed": "no",                 # 5
    "est_value_band": "over_3m",         # 5
    "scope_fit": "yes",                  # 5
}


def _iso_in(days: int) -> str:
    # +1h so flooring to whole days can't slip under the band while a test runs.
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=1)).isoformat()


def test_outcome_thresholds():
    assert THRESHOLDS == {"go": 30, "review": 20}
    assert outcome_for_score(50) == "go"
    assert outcome_for_score(30) == "go"      # boundary: >= 30 is a go
    assert outcome_for_score(29) == "review"
    assert outcome_for_score(20) == "review"  # boundary: >= 20 reviews
    assert outcome_for_score(19) == "no_go"
    assert outcome_for_score(0) == "no_go"


def test_max_score_is_50():
    project = {**BEST_ANSWERS, "actual_bid_at": _iso_in(31), "internal_bid_at": None}
    assert compute_score(project) == 50


def test_unanswered_questions_score_zero():
    assert compute_score({"actual_bid_at": None, "internal_bid_at": None}) == 0
    # A lone answer counts only its own points.
    assert compute_score({"scope_fit": "yes"}) == 5


def test_unknown_answer_value_scores_zero():
    # A stale/renamed value must not crash — it just contributes nothing.
    assert compute_score({"scope_fit": "definitely"}) == 0


def test_other_and_unknown_options_score_zero():
    # Every question offers "other"/"unknown" escape hatches worth 0 points
    # ("competitor_known"/"gc_known" carry only "other" — "no_unknown" already
    # covers the unknown case there). None of them should add to the score.
    for key in ANSWER_POINTS:
        assert ANSWER_POINTS[key].get("other") == 0, f"{key} 'other' should be 0"
    for key in set(ANSWER_POINTS) - {"competitor_known", "gc_known"}:
        assert ANSWER_POINTS[key].get("unknown") == 0, f"{key} 'unknown' should be 0"
    all_other = {key: "other" for key in ANSWER_POINTS}
    assert compute_score({**all_other, "actual_bid_at": None, "internal_bid_at": None}) == 0


def test_bid_days_bands():
    assert _bid_days_points(None) == 0   # no bid date set
    assert _bid_days_points(-1) == 0     # past due
    assert _bid_days_points(0) == 0
    assert _bid_days_points(7) == 0
    assert _bid_days_points(8) == 2
    assert _bid_days_points(14) == 2
    assert _bid_days_points(15) == 4
    assert _bid_days_points(29) == 4
    assert _bid_days_points(30) == 5


def test_actual_bid_date_preferred_over_internal():
    project = {
        **BEST_ANSWERS,
        "actual_bid_at": _iso_in(2),    # 0 pts — wins
        "internal_bid_at": _iso_in(45),  # 5 pts — ignored
    }
    assert compute_score(project) == 45


def test_every_frontend_value_has_points():
    # Guard against a half-synced rubric: each question keeps a full points map.
    assert set(ANSWER_POINTS) == {
        "project_type", "owner_type", "labor_needed", "bid_method",
        "competitor_known", "gc_known", "subs_needed", "est_value_band", "scope_fit",
    }
    for key, points in ANSWER_POINTS.items():
        assert points, f"{key} has no options"
        assert all(isinstance(p, int) for p in points.values())


def test_review_band_project_scores_in_band():
    # A realistic mid-band project lands in review.
    project = {
        "project_type": "lighting",      # 3
        "owner_type": "private_commercial",  # 3
        "labor_needed": "union",         # 4
        "bid_method": "hard_bid",        # 2
        "competitor_known": "yes_1_2",   # 3
        "gc_known": "yes_1_2",           # 3
        "subs_needed": "yes_underground",  # 2
        "est_value_band": "150k_500k",   # 3
        "scope_fit": "maybe",            # 2
        "actual_bid_at": None,
        "internal_bid_at": None,         # 0
    }
    score = compute_score(project)
    assert score == 25
    assert outcome_for_score(score) == "review"
