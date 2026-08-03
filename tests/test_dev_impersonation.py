"""Dev "view the portal as" — the X-Impersonate-Estimator header.

Server-side guarantees under test:

1. `get_current_user` honors the header ONLY for is_dev callers, and only for
   an active, non-dev, estimator-role target — the effective user becomes that
   estimator (is_dev=False, `impersonated_by` = the real dev id) so every
   portal gate behaves exactly as it would for them.
2. A denied project access while impersonating never files the probing alert
   against the real estimator.
3. Durable self-profile writes (PATCH /users/me, DELETE /users/me/mfa) are
   refused while impersonating — they'd rewrite the REAL estimator's account.
4. The targets picker is dev-only (including mid-impersonation, when the
   effective user is no longer is_dev) and never offers dev accounts.

Fast unit tests: `decode_token` and the Supabase client are stubbed, handlers
are called directly.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core import deps
from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import UpdateMeIn
from app.routers import estimator as est_mod
from app.routers import users


def _run(value):
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _request(method="GET", path="/estimator/projects", headers=None) -> Request:
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": hdrs,
            "query_string": b"",
        }
    )


# ── chainable Supabase fake serving multiple profiles by id ───────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self._id = None
        self._single = False
        self._update = None

    def select(self, *a, **k):
        return self

    def update(self, patch):
        self._update = patch
        return self

    def eq(self, col, val):
        if col == "id":
            self._id = val
        return self

    def is_(self, *a):
        return self

    def or_(self, *a):
        return self

    def in_(self, *a):
        return self

    def limit(self, *a):
        return self

    def order(self, *a, **k):
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        rows = self.db.tables.get(self.table_name, [])
        if self._update is not None:
            self.db.updates.append((self.table_name, self._id, self._update))
            return SimpleNamespace(data=rows)
        if self._id is not None:
            rows = [r for r in rows if r.get("id") == self._id]
        if self._single:
            return SimpleNamespace(data=rows[0] if rows else None)
        return SimpleNamespace(data=list(rows))


class _FakeDB:
    def __init__(self, **tables):
        self.tables = tables
        self.updates = []

    def table(self, name):
        return _Query(self, name)


def _dev_profile(**over):
    base = {
        "id": "dev1",
        "email": "dev@g3.com",
        # Deliberately NOT estimator: the header works from any role the dev
        # happens to be switched into.
        "role": "executive",
        "is_active": True,
        "is_dev": True,
        "mfa_enrolled": True,
        "invite_accepted_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(over)
    return base


def _target_profile(**over):
    base = {
        "id": "est1",
        "email": "est@ext.com",
        "full_name": "Ext Estimator",
        "role": "estimator",
        "is_active": True,
        "is_dev": False,
    }
    base.update(over)
    return base


def _setup(monkeypatch, caller, *others):
    db = _FakeDB(profiles=[caller, *others])
    monkeypatch.setattr(deps, "decode_token", lambda token: {"sub": caller["id"], "aal": "aal2"})
    monkeypatch.setattr(deps, "get_supabase", lambda: db)
    return db


def _current_user(headers=None, method="GET"):
    return _run(
        deps.get_current_user(_request(method=method, headers=headers), "Bearer t")
    )


IMP = {"X-Impersonate-Estimator": "est1"}


# ── get_current_user: header resolution ───────────────────────────────────


def test_dev_header_swaps_identity_to_the_estimator(monkeypatch):
    _setup(monkeypatch, _dev_profile(), _target_profile())
    user = _current_user(headers=IMP)
    assert user.id == "est1"
    assert user.email == "est@ext.com"
    assert user.role == Role.ESTIMATOR
    assert user.is_dev is False  # every gate must treat them as the estimator
    assert user.impersonated_by == "dev1"


def test_no_header_leaves_dev_untouched(monkeypatch):
    _setup(monkeypatch, _dev_profile(), _target_profile())
    user = _current_user()
    assert user.id == "dev1"
    assert user.is_dev is True
    assert user.impersonated_by is None


def test_non_dev_header_is_ignored(monkeypatch):
    _setup(
        monkeypatch,
        _dev_profile(id="u1", email="u@g3.com", is_dev=False, role="estimating_admin"),
        _target_profile(),
    )
    user = _current_user(headers=IMP)
    assert user.id == "u1"
    assert user.role == Role.ESTIMATING_ADMIN
    assert user.impersonated_by is None


@pytest.mark.parametrize(
    "target",
    [
        None,  # no such profile
        _target_profile(role="estimating_admin"),  # internal role
        _target_profile(is_active=False),  # deactivated
        _target_profile(is_dev=True),  # another dev
    ],
)
def test_invalid_target_is_refused_with_the_fe_code(monkeypatch, target):
    others = [target] if target else []
    _setup(monkeypatch, _dev_profile(), *others)
    with pytest.raises(HTTPException) as ei:
        _current_user(headers=IMP)
    assert ei.value.status_code == 403
    assert ei.value.detail == "impersonation_target_invalid"


# ── require_project_assignment: no probing alert while impersonating ──────


def _impersonated(**over):
    kw = dict(
        id="est1", email="est@ext.com", role=Role.ESTIMATOR, is_active=True,
        is_dev=False, aal="aal2", mfa_enrolled=True, impersonated_by="dev1",
    )
    kw.update(over)
    return CurrentUser(**kw)


def test_denied_access_while_impersonating_skips_the_security_alert(monkeypatch):
    from app.services import security_alerts

    db = _FakeDB(estimator_assignments=[], projects=[])
    monkeypatch.setattr(deps, "get_supabase", lambda: db)
    calls = []
    monkeypatch.setattr(
        security_alerts, "record_denied_access", lambda *a, **k: calls.append(a)
    )
    with pytest.raises(HTTPException) as ei:
        _run(deps.require_project_assignment("p1", _impersonated()))
    assert ei.value.status_code == 403
    assert calls == []


def test_denied_access_for_a_real_estimator_still_alerts(monkeypatch):
    from app.services import security_alerts

    db = _FakeDB(estimator_assignments=[], projects=[])
    monkeypatch.setattr(deps, "get_supabase", lambda: db)
    calls = []
    monkeypatch.setattr(
        security_alerts, "record_denied_access", lambda *a, **k: calls.append(a)
    )
    with pytest.raises(HTTPException):
        _run(deps.require_project_assignment("p1", _impersonated(impersonated_by=None)))
    assert len(calls) == 1


# ── users.py: durable self-profile writes refused while impersonating ─────


def test_update_me_refused_while_impersonating():
    with pytest.raises(HTTPException) as ei:
        _run(users.update_me(UpdateMeIn(full_name="Oops"), _impersonated()))
    assert ei.value.status_code == 403


def test_reset_my_mfa_refused_while_impersonating():
    with pytest.raises(HTTPException) as ei:
        _run(users.reset_my_mfa(_impersonated()))
    assert ei.value.status_code == 403


# ── the targets picker ────────────────────────────────────────────────────


def _dev_user(**over):
    kw = dict(
        id="dev1", email="dev@g3.com", role=Role.ESTIMATOR, is_active=True,
        is_dev=True, aal="aal2", mfa_enrolled=True,
    )
    kw.update(over)
    return CurrentUser(**kw)


def test_targets_requires_a_dev(monkeypatch):
    monkeypatch.setattr(est_mod, "get_supabase", lambda: _FakeDB(profiles=[]))
    with pytest.raises(HTTPException) as ei:
        _run(est_mod.impersonation_targets(_dev_user(is_dev=False)))
    assert ei.value.status_code == 403


def test_targets_allowed_mid_impersonation(monkeypatch):
    monkeypatch.setattr(est_mod, "get_supabase", lambda: _FakeDB(profiles=[]))
    assert _run(est_mod.impersonation_targets(_impersonated())) == []


def test_targets_lists_estimators_but_never_devs(monkeypatch):
    rows = [
        _target_profile(),
        _target_profile(id="dev2", email="dev2@g3.com", is_dev=True),
    ]
    monkeypatch.setattr(est_mod, "get_supabase", lambda: _FakeDB(profiles=rows))
    out = _run(est_mod.impersonation_targets(_dev_user()))
    assert out == [
        {"id": "est1", "full_name": "Ext Estimator", "email": "est@ext.com"}
    ]
