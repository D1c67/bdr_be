"""POST /users/me/estimator-tour — the portal's "don't offer the tour again" stamp.

Guarantees under test:

1. A first call stamps `profiles.estimator_tour_completed_at` (migration 0092).
2. A replay does NOT restamp it — the column records the FIRST completion, so
   an estimator re-reading the tour from the documentation page cannot rewrite
   their own onboarding date.
3. It is refused while a dev is impersonating an estimator, like every other
   durable self-profile write — it would stamp the REAL estimator's row.
4. A caller with no profile row is a 404, not a silent no-op.

Fast unit tests: the Supabase client is stubbed and the handler called directly.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import users


def _run(value):
    return asyncio.run(value) if asyncio.iscoroutine(value) else value


# ── chainable Supabase fake that actually applies the update ──────────────


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

    def limit(self, *a):
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        rows = [r for r in self.db.tables.get(self.table_name, []) if r.get("id") == self._id]
        if self._update is not None:
            for r in rows:
                r.update(self._update)
            self.db.updates.append((self.table_name, self._id, self._update))
        if self._single:
            return SimpleNamespace(data=rows[0] if rows else None)
        return SimpleNamespace(data=list(rows))


class _FakeDB:
    def __init__(self, **tables):
        self.tables = tables
        self.updates = []

    def table(self, name):
        return _Query(self, name)


def _profile(**over):
    base = {
        "id": "est1",
        "email": "est@ext.com",
        "full_name": "Ext Estimator",
        "role": "estimator",
        "is_active": True,
        "is_dev": False,
        "estimator_tour_completed_at": None,
    }
    base.update(over)
    return base


def _user(**over):
    kw = dict(
        id="est1", email="est@ext.com", role=Role.ESTIMATOR, is_active=True,
        is_dev=False, aal="aal2", mfa_enrolled=True,
    )
    kw.update(over)
    return CurrentUser(**kw)


def _setup(monkeypatch, *profiles):
    db = _FakeDB(profiles=list(profiles))
    monkeypatch.setattr(users, "get_supabase", lambda: db)
    return db


# ── stamping ──────────────────────────────────────────────────────────────


def test_first_call_stamps_the_completion(monkeypatch):
    db = _setup(monkeypatch, _profile())
    out = _run(users.complete_estimator_tour(_user()))
    assert out["estimator_tour_completed_at"] is not None
    assert [u[0] for u in db.updates] == ["profiles"]


def test_replay_does_not_restamp(monkeypatch):
    first = "2026-01-02T03:04:05+00:00"
    db = _setup(monkeypatch, _profile(estimator_tour_completed_at=first))
    out = _run(users.complete_estimator_tour(_user()))
    assert out["estimator_tour_completed_at"] == first
    assert db.updates == []  # no write at all — the stamp is write-once


# ── the guards ────────────────────────────────────────────────────────────


def test_refused_while_impersonating(monkeypatch):
    db = _setup(monkeypatch, _profile())
    with pytest.raises(HTTPException) as ei:
        _run(users.complete_estimator_tour(_user(impersonated_by="dev1")))
    assert ei.value.status_code == 403
    assert db.updates == []


def test_missing_profile_is_a_404(monkeypatch):
    _setup(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _run(users.complete_estimator_tour(_user()))
    assert ei.value.status_code == 404
