"""AI pricing roles and visibility (docs/AI_ESTIMATING_BUILD_SPEC.md 5.5).

Pins the day-1 contract every pricing workstream imports: the role sets in
core/roles, `ai_pricing_allowed` and the `ai_pricing` entry of GET /features
in core/features, and the `require_pricing_view` / `require_pricing_write`
dependencies in core/deps. Settings are swapped per test by patching
`get_settings` in both modules with a Settings built from explicit values,
so the local .env never changes what is asserted. No database, no network.
"""

import asyncio

import pytest
from fastapi import HTTPException

from app.core import deps, features
from app.core.config import Settings
from app.core.deps import CurrentUser, require_pricing_view, require_pricing_write
from app.core.features import (
    AI_PRICING_STATE_ROLES,
    ai_pricing_allowed,
    enabled_map,
    require_ai_pricing,
)
from app.core.roles import (
    AI_ADJUDICATION_REVIEW_ROLES,
    AI_GATE_OVERRIDE_ROLES,
    AI_PRICING_POLICY_ROLES,
    AI_PRICING_VIEW_ROLES,
    AI_PRICING_WRITE_ROLES,
    ESTIMATING_ENGINEER_ROLES,
    VERIFY_ROLES,
    Role,
)

ENGINEERS = (Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR)


def _use(monkeypatch, **overrides) -> Settings:
    """Patch get_settings in features and deps with a Settings that has AI
    Estimating open to every internal role and pricing on in `test` state,
    then the given overrides."""
    values = {
        "plans_reader_enabled": True,
        "ai_estimating_state": "live",
        "ai_pricing_enabled": True,
        "ai_pricing_state": "test",
    }
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    monkeypatch.setattr(features, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    return settings


def _user(role: Role) -> CurrentUser:
    return CurrentUser(id="u1", email="u1@example.com", role=role, is_active=True)


# ── Role sets ──────────────────────────────────────────────────────────────


def test_pricing_view_roles_are_the_five_internal_pricing_roles():
    assert AI_PRICING_VIEW_ROLES == frozenset(
        {Role.EXECUTIVE, Role.IT_ADMIN, Role.ESTIMATING_ADMIN, *ENGINEERS}
    )
    assert Role.ACCOUNTANT not in AI_PRICING_VIEW_ROLES
    assert Role.ESTIMATOR not in AI_PRICING_VIEW_ROLES


def test_pricing_write_roles_exclude_engineers_and_are_viewers():
    assert AI_PRICING_WRITE_ROLES == frozenset(
        {Role.EXECUTIVE, Role.IT_ADMIN, Role.ESTIMATING_ADMIN}
    )
    assert AI_PRICING_WRITE_ROLES <= AI_PRICING_VIEW_ROLES
    assert not AI_PRICING_WRITE_ROLES & ESTIMATING_ENGINEER_ROLES


def test_pricing_policy_roles_are_the_verify_roles():
    assert AI_PRICING_POLICY_ROLES == VERIFY_ROLES
    assert AI_PRICING_POLICY_ROLES == frozenset({Role.EXECUTIVE, Role.IT_ADMIN})


def test_gate_override_roles_follow_q20():
    assert AI_GATE_OVERRIDE_ROLES == frozenset(
        {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN}
    )
    assert not AI_GATE_OVERRIDE_ROLES & ESTIMATING_ENGINEER_ROLES


def test_adjudication_review_roles_follow_the_q1_answer():
    assert AI_ADJUDICATION_REVIEW_ROLES == frozenset(
        {Role.EXECUTIVE, Role.IT_ADMIN, *ENGINEERS}
    )
    assert Role.ESTIMATING_ADMIN not in AI_ADJUDICATION_REVIEW_ROLES
    assert Role.ACCOUNTANT not in AI_ADJUDICATION_REVIEW_ROLES


def test_no_ai_role_set_admits_the_estimator_or_the_accountant():
    for roles in (
        AI_PRICING_VIEW_ROLES,
        AI_PRICING_WRITE_ROLES,
        AI_PRICING_POLICY_ROLES,
        AI_GATE_OVERRIDE_ROLES,
        AI_ADJUDICATION_REVIEW_ROLES,
        *AI_PRICING_STATE_ROLES.values(),
    ):
        assert Role.ESTIMATOR not in roles
        assert Role.ACCOUNTANT not in roles


# ── ai_pricing_allowed matrix ──────────────────────────────────────────────

_TEST_STATE = {Role.EXECUTIVE, Role.IT_ADMIN}
_LIVE_STATE = {Role.EXECUTIVE, Role.IT_ADMIN, Role.ESTIMATING_ADMIN, *ENGINEERS}


@pytest.mark.parametrize("role", list(Role))
def test_test_state_admits_executive_and_it_admin_only(monkeypatch, role):
    _use(monkeypatch, ai_pricing_state="test")
    assert ai_pricing_allowed(role) is (role in _TEST_STATE)
    assert enabled_map(role)["ai_pricing"] is (role in _TEST_STATE)


@pytest.mark.parametrize("role", list(Role))
def test_live_state_admits_every_pricing_viewer(monkeypatch, role):
    _use(monkeypatch, ai_pricing_state="live")
    assert ai_pricing_allowed(role) is (role in _LIVE_STATE)
    assert enabled_map(role)["ai_pricing"] is (role in _LIVE_STATE)


@pytest.mark.parametrize("state", ["test", "live"])
@pytest.mark.parametrize("estimating", ["live", "test", "dev"])
def test_estimator_and_accountant_never(monkeypatch, state, estimating):
    _use(monkeypatch, ai_pricing_state=state, ai_estimating_state=estimating)
    assert ai_pricing_allowed(Role.ESTIMATOR) is False
    assert ai_pricing_allowed(Role.ACCOUNTANT) is False


@pytest.mark.parametrize("role", ENGINEERS)
def test_engineers_only_in_live_state(monkeypatch, role):
    _use(monkeypatch, ai_pricing_state="test")
    assert ai_pricing_allowed(role) is False
    _use(monkeypatch, ai_pricing_state="live")
    assert ai_pricing_allowed(role) is True


@pytest.mark.parametrize("role", ENGINEERS)
def test_ai_estimating_state_still_gates_pricing(monkeypatch, role):
    # Pricing live, but AI Estimating itself only open to Executive and IT Admin.
    _use(monkeypatch, ai_pricing_state="live", ai_estimating_state="test")
    assert ai_pricing_allowed(role) is False
    assert ai_pricing_allowed(Role.EXECUTIVE) is True
    _use(monkeypatch, ai_pricing_state="live", ai_estimating_state="dev")
    assert ai_pricing_allowed(Role.EXECUTIVE) is False


@pytest.mark.parametrize("role", list(Role))
def test_pricing_switch_off_admits_nobody(monkeypatch, role):
    _use(monkeypatch, ai_pricing_enabled=False, ai_pricing_state="live")
    assert ai_pricing_allowed(role) is False
    assert enabled_map(role)["ai_pricing"] is False


def test_plans_reader_switch_off_admits_nobody(monkeypatch):
    _use(monkeypatch, plans_reader_enabled=False, ai_pricing_state="live")
    assert ai_pricing_allowed(Role.EXECUTIVE) is False


def test_no_caller_reads_false(monkeypatch):
    _use(monkeypatch, ai_pricing_state="live")
    assert ai_pricing_allowed(None) is False
    assert enabled_map()["ai_pricing"] is False


def test_settings_stand_in_without_the_fields_fails_closed(monkeypatch):
    class _Bare:
        plans_reader_enabled = True
        ai_estimating_state = "live"

    monkeypatch.setattr(features, "get_settings", lambda: _Bare())
    assert ai_pricing_allowed(Role.EXECUTIVE) is False


def test_unknown_state_admits_nobody(monkeypatch):
    class _Odd:
        plans_reader_enabled = True
        ai_estimating_state = "live"
        ai_pricing_enabled = True
        ai_pricing_state = "beta"

    monkeypatch.setattr(features, "get_settings", lambda: _Odd())
    assert ai_pricing_allowed(Role.EXECUTIVE) is False


# ── Dependencies ───────────────────────────────────────────────────────────


def _status_of(dep, role: Role) -> int:
    try:
        asyncio.run(dep(user=_user(role)))
    except HTTPException as exc:
        return exc.status_code
    return 200


@pytest.mark.parametrize("dep", [require_pricing_view, require_pricing_write])
@pytest.mark.parametrize("role", list(Role))
def test_deps_404_while_pricing_is_off(monkeypatch, dep, role):
    _use(monkeypatch, ai_pricing_enabled=False, ai_pricing_state="live")
    assert _status_of(dep, role) == 404


def test_router_gate_404s_while_pricing_is_off(monkeypatch):
    _use(monkeypatch, ai_pricing_enabled=False)
    with pytest.raises(HTTPException) as exc:
        require_ai_pricing()
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not Found"
    _use(monkeypatch, ai_pricing_enabled=True)
    assert require_ai_pricing() is None


@pytest.mark.parametrize("role", list(Role))
def test_require_pricing_view_live(monkeypatch, role):
    _use(monkeypatch, ai_pricing_state="live")
    user = _user(role)
    if role in _LIVE_STATE:
        assert asyncio.run(require_pricing_view(user=user)) is user
    else:
        assert _status_of(require_pricing_view, role) == 403


@pytest.mark.parametrize("role", list(Role))
def test_require_pricing_view_test_state(monkeypatch, role):
    _use(monkeypatch, ai_pricing_state="test")
    assert _status_of(require_pricing_view, role) == (200 if role in _TEST_STATE else 403)


@pytest.mark.parametrize("role", list(Role))
def test_require_pricing_write_live(monkeypatch, role):
    _use(monkeypatch, ai_pricing_state="live")
    expected = 200 if role in AI_PRICING_WRITE_ROLES else 403
    assert _status_of(require_pricing_write, role) == expected


@pytest.mark.parametrize("role", list(Role))
def test_require_pricing_write_test_state(monkeypatch, role):
    # test state: only the Executive and the IT Admin see prices, so the
    # Estimating Admin (a writer in live) is refused too.
    _use(monkeypatch, ai_pricing_state="test")
    assert _status_of(require_pricing_write, role) == (200 if role in _TEST_STATE else 403)
