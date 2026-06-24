"""Go/No-Go decision logic.

Rule (confirmed with the user): the PM, PA, and Executive each get one vote.
A majority decides. The Executive may override at any time to force the outcome,
ending the vote immediately.

We tally by ROLE (one vote per role), using the latest vote cast by any user
holding that role — so "PM/PA/Executive each get 1 vote" holds even if more than
one person shares a role.
"""

from app.core.roles import Role

VOTING_ROLES = (Role.PM, Role.PA, Role.EXECUTIVE)


def tally_decision(role_votes: dict[Role, str]) -> str | None:
    """Given the current vote per voting-role, return 'go'/'no_go'/None.

    None means undecided (not enough votes for a majority yet).
    """
    relevant = {r: v for r, v in role_votes.items() if r in VOTING_ROLES}
    go = sum(1 for v in relevant.values() if v == "go")
    no_go = sum(1 for v in relevant.values() if v == "no_go")
    # Majority of the 3-vote panel = 2.
    if go >= 2:
        return "go"
    if no_go >= 2:
        return "no_go"
    return None
