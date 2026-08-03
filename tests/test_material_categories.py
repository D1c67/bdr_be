"""Adding a material category is open to every writer role.

The BOQ-extraction panel and the PM materials panel both create a category
inline when a group doesn't fit an existing bucket, so gating the POST on
IT Admin broke that button for everyone else (Executive included). Editing or
retiring a category is narrower — that rewrites the taxonomy under live
projects — and belongs to CATEGORY_ADMIN_ROLES (Executive + IT Admin), which
also gates the Contacts → Categories tab in the FE.

Widening the writer set makes duplicate names likely (two people naming the
same bucket), which would split one material group across two RFQs, so the
create is idempotent on an active name+kind.
"""

import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser, require_writer
from app.core.roles import (
    CATEGORY_ADMIN_ROLES,
    INTERNAL_ROLES,
    WRITER_ROLES,
    Role,
)
from app.routers import reference as ref


# ── Fake Supabase ────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db):
        self.db = db
        self._op = None
        self._payload = None
        self._filters = {}

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        if self._op == "insert":
            row = {"id": f"c{len(self.db.rows) + 1}", "is_active": True, **self._payload}
            self.db.rows.append(row)
            self.db.inserts.append(self._payload)
            return type("R", (), {"data": [row]})()
        rows = [
            r
            for r in self.db.rows
            if all(r.get(c) == v for c, v in self._filters.items())
        ]
        return type("R", (), {"data": rows})()


class _DB:
    def __init__(self, rows=()):
        self.rows = [dict(r) for r in rows]
        self.inserts = []

    def table(self, _name):
        return _Query(self)


@pytest.fixture
def db(monkeypatch):
    store = _DB()
    monkeypatch.setattr(ref, "get_supabase", lambda: store)
    return store


def _user(role: Role) -> CurrentUser:
    return CurrentUser(id="u1", email="u@g3.com", role=role, is_active=True)


# ── Who may add one ──────────────────────────────────────────────────────────


def test_every_writer_role_may_add_a_category():
    # The dependency the route now uses — Executive must pass, not just IT Admin.
    for role in WRITER_ROLES:
        assert asyncio.run(require_writer(_user(role))) is not None
    assert Role.EXECUTIVE in WRITER_ROLES


def test_read_only_and_external_roles_still_cannot_add():
    for role in (Role.ACCOUNTANT, Role.ESTIMATOR):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_writer(_user(role)))
        assert exc.value.status_code == 403
    assert Role.ACCOUNTANT in INTERNAL_ROLES  # reads the list, can't extend it


def test_editing_a_category_is_executive_and_it_admin_only():
    # Rename/reorder/deactivate rewrites the taxonomy under live projects, so
    # PATCH is NOT widened to every writer along with POST — it backs the
    # Contacts → Categories tab, whose FE gate lists the same two roles.
    assert CATEGORY_ADMIN_ROLES == {Role.EXECUTIVE, Role.IT_ADMIN}
    guard = inspect.signature(ref.update_material_category).parameters["_"].default.dependency
    for role in CATEGORY_ADMIN_ROLES:
        assert asyncio.run(guard(_user(role))) is not None
    for role in WRITER_ROLES - CATEGORY_ADMIN_ROLES:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(guard(_user(role)))
        assert exc.value.status_code == 403


# ── Create behaviour ─────────────────────────────────────────────────────────


def test_executive_creates_a_new_category(db):
    out = ref.create_material_category(name="Switchgear", _=_user(Role.EXECUTIVE))
    assert out["name"] == "Switchgear"
    assert db.inserts == [{"name": "Switchgear", "kind": "material", "sort_order": 0}]


def test_duplicate_active_name_returns_the_existing_row(db):
    db.rows.append(
        {"id": "c9", "name": "Switchgear", "kind": "material", "is_active": True}
    )
    out = ref.create_material_category(name="  switchgear ", _=_user(Role.EXECUTIVE))
    assert out["id"] == "c9"
    assert db.inserts == []  # no twin taxonomy entry


def test_same_name_under_a_different_kind_is_a_real_create(db):
    db.rows.append(
        {"id": "c9", "name": "Overhead", "kind": "material", "is_active": True}
    )
    out = ref.create_material_category(
        name="Overhead", kind="markup", _=_user(Role.EXECUTIVE)
    )
    assert out["id"] != "c9"
    assert db.inserts == [{"name": "Overhead", "kind": "markup", "sort_order": 0}]


def test_retired_category_is_not_resurrected_by_a_writer(db):
    db.rows.append(
        {"id": "c9", "name": "Switchgear", "kind": "material", "is_active": False}
    )
    out = ref.create_material_category(name="Switchgear", _=_user(Role.EXECUTIVE))
    assert out["id"] != "c9"
    assert db.rows[0]["is_active"] is False  # IT Admin's retirement stands


def test_blank_name_and_bad_kind_are_rejected(db):
    with pytest.raises(HTTPException) as exc:
        ref.create_material_category(name="   ", _=_user(Role.EXECUTIVE))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        ref.create_material_category(name="X", kind="labor", _=_user(Role.EXECUTIVE))
    assert exc.value.status_code == 400
    assert db.inserts == []
