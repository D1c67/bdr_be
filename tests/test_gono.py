"""Unit tests for the Go/No-Go majority decision logic."""

from app.core.roles import Role
from app.services.gono import tally_decision


def test_undecided_with_one_vote():
    assert tally_decision({Role.PM: "go"}) is None


def test_undecided_when_split_and_incomplete():
    # PM go, PA no_go, Executive hasn't voted → 1-1, undecided.
    assert tally_decision({Role.PM: "go", Role.PA: "no_go"}) is None


def test_majority_go_with_two_votes():
    assert tally_decision({Role.PM: "go", Role.PA: "go"}) == "go"


def test_majority_no_go_with_two_votes():
    assert tally_decision({Role.PM: "no_go", Role.EXECUTIVE: "no_go"}) == "no_go"


def test_three_way_resolves_to_majority():
    assert tally_decision({Role.PM: "go", Role.PA: "no_go", Role.EXECUTIVE: "go"}) == "go"


def test_non_voting_roles_ignored():
    # An accountant's vote (shouldn't happen) is ignored by the tally.
    assert tally_decision({Role.PM: "go", Role.ACCOUNTANT: "go"}) is None
