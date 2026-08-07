"""A company name may exist once in each directory, compared case-insensitively.

Both directories are written from several forms (Contacts page, New Bid modal,
a project's GC panel, the "new company" option in the RFQ step), so the guard
lives on the two POST routes all of them funnel through rather than in each
form. These tests pin it there, and pin the deliberate non-guarantee: PEOPLE
are not deduped, only companies.
"""

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import GCContactIn, GCIn, VendorContactIn, VendorIn
from app.routers import reference as ref
from app.routers import vendors as ven
from app.services.directory import normalize_company_name


# ── Fake Supabase ────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
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

    def in_(self, col, vals):
        self._filters[col] = ("in", list(vals))
        return self

    def limit(self, _n):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.table_name, [])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            made = []
            for p in payloads:
                row = {"id": f"{self.table_name}-{len(rows) + 1}", **p}
                rows.append(row)
                self.db.inserts.append((self.table_name, p))
                made.append(row)
            return type("R", (), {"data": made})()

        def keep(r):
            for col, val in self._filters.items():
                if isinstance(val, tuple) and val[0] == "in":
                    if r.get(col) not in val[1]:
                        return False
                elif r.get(col) != val:
                    return False
            return True

        return type("R", (), {"data": [r for r in rows if keep(r)]})()


class _DB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.inserts: list[tuple[str, dict]] = []

    def table(self, name):
        return _Query(self, name)

    def seed(self, table, *rows):
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)


@pytest.fixture
def db(monkeypatch):
    store = _DB()
    monkeypatch.setattr(ref, "get_supabase", lambda: store)
    monkeypatch.setattr(ven, "get_supabase", lambda: store)
    return store


def _user() -> CurrentUser:
    return CurrentUser(id="u1", email="u@g3.com", role=Role.EXECUTIVE, is_active=True)


def _names(db, table):
    return [r["name"] for r in db.tables.get(table, [])]


# ── Normalization ────────────────────────────────────────────────────────────


def test_names_compare_ignoring_case_and_surplus_whitespace():
    same = ["ABC Electric", "abc electric", "  ABC   Electric  ", "aBc\tElectric"]
    assert len({normalize_company_name(n) for n in same}) == 1


def test_genuinely_different_names_stay_different():
    # The guard is exact apart from case/whitespace: near-misses are real
    # companies, not typos we may silently merge.
    assert normalize_company_name("G3 Electric") != normalize_company_name("G3 Electrical")
    assert normalize_company_name("ABC Electric") != normalize_company_name("ABCElectric")


# ── General contractors ──────────────────────────────────────────────────────


def test_new_gc_is_created(db):
    out = ref.create_gc(GCIn(name="ABC Electric"), _=_user())
    assert out["name"] == "ABC Electric"
    assert _names(db, "general_contractors") == ["ABC Electric"]


def test_gc_with_an_existing_name_in_any_case_is_refused(db):
    db.seed("general_contractors", {"id": "g9", "name": "ABC Electric"})
    for attempt in ("ABC Electric", "abc electric", "  AbC   ElEcTrIc "):
        with pytest.raises(HTTPException) as exc:
            ref.create_gc(GCIn(name=attempt), _=_user())
        assert exc.value.status_code == 409
        # The stored spelling is quoted back, so the user can find the row they
        # were told already exists.
        assert "ABC Electric" in exc.value.detail
        assert "already in the system" in exc.value.detail
    assert db.inserts == []


def test_gc_name_is_stored_normalized_so_a_later_twin_cannot_slip_through(db):
    ref.create_gc(GCIn(name="  ABC   Electric  "), _=_user())
    assert _names(db, "general_contractors") == ["ABC Electric"]
    with pytest.raises(HTTPException) as exc:
        ref.create_gc(GCIn(name="ABC Electric"), _=_user())
    assert exc.value.status_code == 409


def test_blank_gc_name_is_rejected_before_any_insert(db):
    with pytest.raises(HTTPException) as exc:
        ref.create_gc(GCIn(name="   "), _=_user())
    assert exc.value.status_code == 400
    assert db.inserts == []


# ── Vendors ──────────────────────────────────────────────────────────────────


def test_new_vendor_is_created(db):
    out = ven.create_vendor(VendorIn(name="Graybar"), _=_user())
    assert out["name"] == "Graybar"
    assert out["material_category_ids"] == []


def test_vendor_with_an_existing_name_in_any_case_is_refused(db):
    db.seed("vendors", {"id": "v9", "name": "Graybar"})
    for attempt in ("Graybar", "graybar", " GRAYBAR "):
        with pytest.raises(HTTPException) as exc:
            ven.create_vendor(VendorIn(name=attempt), _=_user())
        assert exc.value.status_code == 409
        assert "Graybar" in exc.value.detail
    assert db.inserts == []


def test_blank_vendor_name_is_rejected_before_any_insert(db):
    with pytest.raises(HTTPException) as exc:
        ven.create_vendor(VendorIn(name=" "), _=_user())
    assert exc.value.status_code == 400
    assert db.inserts == []


def test_the_two_directories_are_independent(db):
    # A GC and a vendor may share a name: a company can both hire us and sell
    # to us, and they are separate rows in separate tables.
    db.seed("general_contractors", {"id": "g9", "name": "Sturgeon"})
    out = ven.create_vendor(VendorIn(name="Sturgeon"), _=_user())
    assert out["name"] == "Sturgeon"


# ── People are deliberately NOT deduped ──────────────────────────────────────


def test_two_gc_contacts_may_share_a_name(db):
    db.seed("general_contractors", {"id": "g1", "name": "ABC Electric"})
    ref.create_gc_contact(GCContactIn(gc_id="g1", name="John Smith"), _=_user())
    ref.create_gc_contact(GCContactIn(gc_id="g1", name="John Smith"), _=_user())
    assert _names(db, "gc_contacts") == ["John Smith", "John Smith"]


def test_two_vendor_contacts_may_share_a_name(db):
    db.seed("vendors", {"id": "v1", "name": "Graybar"})
    for _ in range(2):
        ven.create_contact(
            VendorContactIn(vendor_id="v1", name="John Smith", email="js@graybar.com"),
            _=_user(),
        )
    assert _names(db, "vendor_contacts") == ["John Smith", "John Smith"]
