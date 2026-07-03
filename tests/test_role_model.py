"""The consolidated role model: the writer/internal/verify role sets and the
read-only-accountant guards introduced with the PA→Estimating Admin / PM+PE→
Estimating Engineer refactor.

`require_writer` and `require_internal` are plain async dependencies — calling
them with an explicit CurrentUser bypasses FastAPI's Depends wiring, so these are
fast unit tests with no app/DB.
"""

import asyncio

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser, require_internal, require_writer
from app.core.roles import (
    ACTUAL_BID_VIEWER_ROLES,
    INTERNAL_ROLES,
    VERIFY_ROLES,
    WRITER_ROLES,
    Role,
)


def _user(role: Role) -> CurrentUser:
    return CurrentUser(id="u1", email="u@g3.com", role=role, is_active=True)


# ── Role-set invariants ────────────────────────────────────────────────────


def test_writer_roles_are_internal_minus_accountant():
    assert WRITER_ROLES == {
        Role.ESTIMATING_ADMIN,
        Role.ESTIMATING_ENGINEER,
        Role.EXECUTIVE,
        Role.IT_ADMIN,
    }
    # Read access = writers plus the read-only accountant; the estimator is external.
    assert INTERNAL_ROLES == WRITER_ROLES | {Role.ACCOUNTANT}
    assert Role.ACCOUNTANT not in WRITER_ROLES
    assert Role.ESTIMATOR not in INTERNAL_ROLES


def test_verify_roles_are_executive_and_it_admin():
    assert VERIFY_ROLES == {Role.EXECUTIVE, Role.IT_ADMIN}
    # The merged engineer (former PM) can no longer verify.
    assert Role.ESTIMATING_ENGINEER not in VERIFY_ROLES


def test_accountant_can_view_actual_bid_but_engineer_cannot():
    assert Role.ACCOUNTANT in ACTUAL_BID_VIEWER_ROLES
    assert Role.ESTIMATING_ENGINEER not in ACTUAL_BID_VIEWER_ROLES


def test_old_role_codes_are_gone():
    values = {r.value for r in Role}
    assert {"pm", "pe", "pa"}.isdisjoint(values)
    assert {"estimating_engineer", "estimating_admin"} <= values


# ── require_writer: writers pass, accountant + estimator are blocked ───────


@pytest.mark.parametrize(
    "role", [Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER, Role.EXECUTIVE, Role.IT_ADMIN]
)
def test_require_writer_allows_writers(role):
    user = _user(role)
    assert asyncio.run(require_writer(user=user)) is user


@pytest.mark.parametrize("role", [Role.ACCOUNTANT, Role.ESTIMATOR])
def test_require_writer_blocks_readonly_and_estimator(role):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_writer(user=_user(role)))
    assert exc.value.status_code == 403


# ── require_internal: accountant keeps read access, estimator does not ─────


def test_require_internal_allows_accountant_read():
    user = _user(Role.ACCOUNTANT)
    assert asyncio.run(require_internal(user=user)) is user


def test_require_internal_blocks_estimator():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_internal(user=_user(Role.ESTIMATOR)))
    assert exc.value.status_code == 403
