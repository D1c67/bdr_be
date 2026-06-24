"""The actual (to-GC) bid date is confidential to the PA, Executive, and IT
Admin. Every project row that leaves the API passes through redact_for_role,
which nulls the field for everyone else; the internal bid date stays visible
to the whole team."""

from app.core.roles import ACTUAL_BID_VIEWER_ROLES, Role
from app.routers.projects import redact_for_role

PROJECT = {
    "id": "p1",
    "name": "Lab Fit-Out",
    "internal_bid_at": "2026-06-20T17:00:00+00:00",
    "actual_bid_at": "2026-06-24T18:00:00+00:00",
}


def test_viewer_set_is_exactly_pa_executive_it_admin():
    assert ACTUAL_BID_VIEWER_ROLES == {Role.PA, Role.EXECUTIVE, Role.IT_ADMIN}


def test_viewer_roles_see_the_actual_bid_date():
    for role in ACTUAL_BID_VIEWER_ROLES:
        assert redact_for_role(PROJECT, role)["actual_bid_at"] == PROJECT["actual_bid_at"]


def test_other_roles_get_the_actual_bid_date_nulled():
    for role in set(Role) - ACTUAL_BID_VIEWER_ROLES:
        assert redact_for_role(PROJECT, role)["actual_bid_at"] is None


def test_internal_bid_date_is_never_redacted():
    for role in Role:
        assert redact_for_role(PROJECT, role)["internal_bid_at"] == PROJECT["internal_bid_at"]


def test_redaction_copies_rather_than_mutates():
    redact_for_role(PROJECT, Role.PM)
    assert PROJECT["actual_bid_at"] == "2026-06-24T18:00:00+00:00"
