"""A vendor contact serves a SET of material categories (migration 0095).

Before 0095 a contact carried one category, so a rep who quoted switchgear AND
lighting had to be entered twice. That put two rows in the directory for one
person, duplicated them across recipient lists, and split their send history.
The link table makes the same contact appear under every category they serve
while staying one row.

The two things worth pinning down: the recipient filter still returns each
contact exactly ONCE per category (RFQ dispatch emails whoever it returns), and
a bad category id is a 400 rather than a raw foreign-key 500.
"""

import copy
import inspect
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import WRITER_ROLES, Role
from app.models.schemas import VendorContactIn, VendorContactUpdate
from app.routers import vendors as v

# Real UUIDs: the router rejects anything that isn't one before it reaches PG.
SWITCHGEAR = str(uuid4())
LIGHTING = str(uuid4())
LOW_VOLTAGE = str(uuid4())
VENDOR_A = str(uuid4())
VENDOR_B = str(uuid4())


# ── Fake Supabase ────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self._op, self._sel, self._payload = "select", "*", None
        self._eq, self._in = {}, {}
        self._order, self._limit = None, None

    def select(self, sel="*", *a, **k):
        self._op, self._sel = "select", sel
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def in_(self, col, vals):
        self._in[col] = list(vals)
        return self

    def order(self, col, **k):
        self._order = col
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, r):
        return all(r.get(c) == val for c, val in self._eq.items()) and all(
            r.get(c) in vals for c, vals in self._in.items()
        )

    def execute(self):
        rows = self.db.tables.setdefault(self.name, [])
        if self._op == "insert":
            batch = self._payload if isinstance(self._payload, list) else [self._payload]
            self.db.inserts.append((self.name, [dict(p) for p in batch]))
            made = []
            for p in batch:
                row = dict(p)
                # Real UUIDs: the router validates ids before they reach PG.
                row.setdefault("id", str(uuid4()))
                rows.append(row)
                made.append(dict(row))
            return _Result(made)
        if self._op == "delete":
            removed = [r for r in rows if self._match(r)]
            self.db.tables[self.name] = [r for r in rows if not self._match(r)]
            self.db.deletes.append((self.name, dict(self._eq)))
            return _Result(removed)

        out = [r for r in rows if self._match(r)]
        if self._order:
            out.sort(key=lambda r: (r.get(self._order) or ""))
        if self._limit is not None:
            out = out[: self._limit]
        # The two PostgREST embeds the router asks for.
        if "vendor_contacts(" in self._sel:
            return _Result(
                [{"vendor_contacts": self.db.contact(r["vendor_contact_id"])} for r in out]
            )
        if "vendors(name)" in self._sel:
            return _Result([self.db.contact(r["id"]) for r in out])
        return _Result(copy.deepcopy(out))


class _DB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.inserts: list[tuple[str, list[dict]]] = []
        self.deletes: list[tuple[str, dict]] = []

    def table(self, name):
        return _Query(self, name)

    def contact(self, contact_id):
        """A contact row with its vendor embedded, as PostgREST would return it."""
        for r in self.tables.get("vendor_contacts", []):
            if r["id"] == contact_id:
                row = copy.deepcopy(r)
                name = next(
                    (x["name"] for x in self.tables.get("vendors", []) if x["id"] == r["vendor_id"]),
                    None,
                )
                row["vendors"] = {"name": name}
                return row
        return None

    def links(self, contact_id):
        return sorted(
            r["material_category_id"]
            for r in self.tables.get("vendor_contact_categories", [])
            if r["vendor_contact_id"] == contact_id
        )


@pytest.fixture
def db(monkeypatch):
    store = _DB()
    store.tables["material_categories"] = [
        {"id": SWITCHGEAR, "name": "Switchgear"},
        {"id": LIGHTING, "name": "Lighting"},
        {"id": LOW_VOLTAGE, "name": "Low Voltage"},
    ]
    store.tables["vendors"] = [
        {"id": VENDOR_A, "name": "Acme Electric Supply"},
        {"id": VENDOR_B, "name": "Border States"},
    ]
    store.tables["vendor_contacts"] = []
    store.tables["vendor_contact_categories"] = []
    monkeypatch.setattr(v, "get_supabase", lambda: store)
    return store


def _user(role: Role = Role.EXECUTIVE) -> CurrentUser:
    return CurrentUser(id="u1", email="u@g3.com", role=role, is_active=True)


def _add(db, vendor_id, name, cats, email=None):
    return v.create_contact(
        VendorContactIn(
            vendor_id=vendor_id,
            name=name,
            email=email or f"{name.split()[0].lower()}@example.com",
            material_category_ids=cats,
        ),
        _=_user(),
    )


# ── Creating a multi-category contact ────────────────────────────────────────


def test_one_contact_can_be_filed_under_several_categories(db):
    out = _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR, LIGHTING])
    assert out["material_category_ids"] == [SWITCHGEAR, LIGHTING]
    # One contact row, two links — not the old "enter the person twice".
    assert len(db.tables["vendor_contacts"]) == 1
    assert db.links(out["id"]) == sorted([SWITCHGEAR, LIGHTING])


def test_no_categories_is_still_allowed_and_writes_no_links(db):
    # The pre-0095 column was nullable; an uncategorized contact stays valid.
    out = _add(db, VENDOR_A, "Sam Ray", [])
    assert out["material_category_ids"] == []
    assert db.tables["vendor_contact_categories"] == []


def test_the_same_category_twice_is_collapsed(db):
    out = _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR, SWITCHGEAR, LIGHTING])
    assert out["material_category_ids"] == [SWITCHGEAR, LIGHTING]
    assert len(db.tables["vendor_contact_categories"]) == 2


def test_the_category_field_is_not_written_onto_the_contact_row(db):
    # 0095 drops vendor_contacts.material_category_id; writing it would 42703.
    _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR])
    payload = next(p for tbl, ps in db.inserts if tbl == "vendor_contacts" for p in ps)
    assert "material_category_id" not in payload
    assert "material_category_ids" not in payload


# ── Bad input is a 400, not a foreign-key 500 ────────────────────────────────


def test_unknown_category_is_rejected_before_the_contact_is_written(db):
    with pytest.raises(HTTPException) as exc:
        _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR, str(uuid4())])
    assert exc.value.status_code == 400
    # Validation runs first, so no half-created contact is left behind.
    assert db.tables["vendor_contacts"] == []


def test_non_uuid_category_id_is_a_400(db):
    with pytest.raises(HTTPException) as exc:
        _add(db, VENDOR_A, "Jane Doe", ["not-a-uuid"])
    assert exc.value.status_code == 400
    assert db.tables["vendor_contacts"] == []


def test_absurd_category_count_is_capped(db):
    with pytest.raises(HTTPException) as exc:
        _add(db, VENDOR_A, "Jane Doe", [str(uuid4()) for _ in range(v._MAX_CATEGORIES + 1)])
    assert exc.value.status_code == 400
    assert db.tables["vendor_contact_categories"] == []


def test_non_uuid_filter_is_a_400(db):
    with pytest.raises(HTTPException) as exc:
        v.list_contacts(material_category_id="../../etc", _=_user())
    assert exc.value.status_code == 400


# ── The recipient filter (what RFQ / submittal dispatch reads) ───────────────


def test_a_multi_category_contact_appears_under_every_category_they_serve(db):
    jane = _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR, LIGHTING])
    for cat in (SWITCHGEAR, LIGHTING):
        rows = v.list_contacts(material_category_id=cat, _=_user())
        assert [r["id"] for r in rows] == [jane["id"]]
        # Every row carries the full set, so the UI can show what else they cover.
        assert sorted(rows[0]["material_category_ids"]) == sorted([SWITCHGEAR, LIGHTING])


def test_the_filter_returns_each_contact_exactly_once(db):
    # RFQ dispatch emails whoever this returns; a duplicate row would be a
    # duplicate email to the same person.
    _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR, LIGHTING, LOW_VOLTAGE])
    rows = v.list_contacts(material_category_id=SWITCHGEAR, _=_user())
    assert len(rows) == 1


def test_a_contact_is_not_returned_for_a_category_they_do_not_serve(db):
    _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR])
    assert v.list_contacts(material_category_id=LIGHTING, _=_user()) == []


def test_uncategorized_contacts_are_in_the_unfiltered_list_only(db):
    _add(db, VENDOR_A, "Sam Ray", [])
    assert [r["name"] for r in v.list_contacts(_=_user())] == ["Sam Ray"]
    for cat in (SWITCHGEAR, LIGHTING, LOW_VOLTAGE):
        assert v.list_contacts(material_category_id=cat, _=_user()) == []


def test_the_filtered_list_still_carries_the_vendor_name(db):
    # RFQSendPanel groups recipients by company off this embed.
    _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR])
    row = v.list_contacts(material_category_id=SWITCHGEAR, _=_user())[0]
    assert row["vendors"]["name"] == "Acme Electric Supply"


# ── A company's categories are the union across its contacts ────────────────


def test_vendor_categories_are_the_union_of_its_contacts(db):
    _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR, LIGHTING])
    _add(db, VENDOR_A, "Bob Roe", [LIGHTING, LOW_VOLTAGE])
    _add(db, VENDOR_B, "Kim Poe", [SWITCHGEAR])
    by_id = {row["id"]: row for row in v.list_vendors(_=_user())}
    assert sorted(by_id[VENDOR_A]["material_category_ids"]) == sorted(
        [SWITCHGEAR, LIGHTING, LOW_VOLTAGE]
    )
    # Lighting is shared by both contacts but listed once for the company.
    assert by_id[VENDOR_A]["material_category_ids"].count(LIGHTING) == 1
    assert by_id[VENDOR_B]["material_category_ids"] == [SWITCHGEAR]


def test_a_company_with_no_contacts_has_no_categories(db):
    assert all(row["material_category_ids"] == [] for row in v.list_vendors(_=_user()))


def test_a_new_company_reports_an_empty_category_list(db):
    from app.models.schemas import VendorIn

    out = v.create_vendor(VendorIn(name="Fresh Co"), _=_user())
    assert out["material_category_ids"] == []


# ── Editing an existing contact's categories ────────────────────────────────


def test_patch_replaces_the_whole_set(db):
    jane = _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR])
    out = v.update_contact_categories(
        jane["id"], VendorContactUpdate(material_category_ids=[LIGHTING, LOW_VOLTAGE]), _=_user()
    )
    assert out["material_category_ids"] == [LIGHTING, LOW_VOLTAGE]
    # Switchgear is gone, not merged: the picker submits the full selection.
    assert db.links(jane["id"]) == sorted([LIGHTING, LOW_VOLTAGE])
    assert v.list_contacts(material_category_id=SWITCHGEAR, _=_user()) == []


def test_patch_is_how_a_pre_0095_contact_gains_more_categories(db):
    jane = _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR])
    v.update_contact_categories(
        jane["id"], VendorContactUpdate(material_category_ids=[SWITCHGEAR, LIGHTING]), _=_user()
    )
    assert db.links(jane["id"]) == sorted([SWITCHGEAR, LIGHTING])


def test_patch_to_empty_clears_the_set(db):
    jane = _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR, LIGHTING])
    v.update_contact_categories(jane["id"], VendorContactUpdate(), _=_user())
    assert db.links(jane["id"]) == []


def test_patch_rejects_an_unknown_category_without_dropping_the_old_ones(db):
    jane = _add(db, VENDOR_A, "Jane Doe", [SWITCHGEAR])
    with pytest.raises(HTTPException) as exc:
        v.update_contact_categories(
            jane["id"], VendorContactUpdate(material_category_ids=[str(uuid4())]), _=_user()
        )
    assert exc.value.status_code == 400
    # Validation precedes the delete, so the existing set survives a bad request.
    assert db.links(jane["id"]) == [SWITCHGEAR]


def test_patch_on_a_missing_contact_is_404(db):
    with pytest.raises(HTTPException) as exc:
        v.update_contact_categories(
            str(uuid4()), VendorContactUpdate(material_category_ids=[SWITCHGEAR]), _=_user()
        )
    assert exc.value.status_code == 404


def test_patch_on_a_non_uuid_id_is_a_400(db):
    with pytest.raises(HTTPException) as exc:
        v.update_contact_categories("' or 1=1", VendorContactUpdate(), _=_user())
    assert exc.value.status_code == 400


# ── Authorization is unchanged ──────────────────────────────────────────────


def test_editing_categories_needs_a_writer_role():
    import asyncio

    guard = inspect.signature(v.update_contact_categories).parameters["_"].default.dependency
    for role in WRITER_ROLES:
        assert asyncio.run(guard(_user(role))) is not None
    for role in (Role.ACCOUNTANT, Role.ESTIMATOR):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(guard(_user(role)))
        assert exc.value.status_code == 403
