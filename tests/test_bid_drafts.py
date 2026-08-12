"""Saved New Bid drafts (/bid-drafts) - writer-shared CRUD plus the
confidential actual-bid-date rules.

A draft has no side effects (no project row, no number reservation, no emails),
so the router is a thin CRUD surface; what these tests pin is the trust model -
every route writer-gated, any writer touches any draft - and the
confidentiality rule on the otherwise-opaque blob: `data.fields.actual_bid_at`
is stripped from responses for roles outside ACTUAL_BID_VIEWER_ROLES, dropped
from POSTs by roles outside ACTUAL_BID_EDITOR_ROLES, and carried forward
untouched through those roles' PUTs (no seeing, clearing, or smuggling).
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.deps import CurrentUser, require_writer
from app.core.roles import Role
from app.routers import bid_drafts as bd
from app.routers.bid_drafts import BidDraftIn


# ── Fake Supabase ────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._sel = "*"
        self._order = None
        self._desc = False
        self._limit = None

    def select(self, sel="*", *a, **k):
        self._op, self._sel = "select", sel
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def order(self, col, desc=False, **k):
        self._order, self._desc = col, desc
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    def _matches(self, row):
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._order:
                hits = sorted(hits, key=lambda r: r.get(self._order) or "", reverse=self._desc)
            if self._limit is not None:
                hits = hits[: self._limit]
            if self._sel == "*":
                hits = [dict(r) for r in hits]
            else:
                cols = [c.strip() for c in self._sel.split(",")]
                hits = [{c: r.get(c) for c in cols} for r in hits]
            return SimpleNamespace(data=hits)
        if self._op == "insert":
            row = dict(self._payload)
            row.setdefault("id", f"d{len(rows) + 1}")
            stamp = self.db.stamp()
            row.setdefault("created_at", stamp)
            row.setdefault("updated_at", stamp)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    r["updated_at"] = self.db.stamp()  # the set_updated_at trigger
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            # Return the deleted rows, like the real API - the draft-file
            # conditional delete inspects them to detect a lost race.
            hits = [dict(r) for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=hits)
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self):
        self.tables = {}
        self._tick = 0

    def table(self, name):
        return _Query(self, name)

    def stamp(self):
        self._tick += 1
        return f"2026-08-12T00:00:{self._tick:02d}+00:00"


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(bd, "get_supabase", lambda: fake)
    # delete_draft best-effort sweeps the draft's storage prefix (0109); keep
    # these CRUD tests off the network. test_bid_draft_files.py asserts on it.
    monkeypatch.setattr(bd.storage, "delete_draft_prefix", lambda _id: None)
    return fake


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True)


SECRET = "2026-08-20T18:00:00+00:00"


def _blob(**over):
    data = {
        "fields": {"internal_bid_at": "2026-08-18T17:00:00+00:00"},
        "isNgem": False,
        "noBiddingUrl": True,
        "selectedGcs": ["gc-1"],
        "gcNeedsBy": {},
    }
    data.update(over)
    return data


def _draft_in(name="Lab Fit-Out", number="26-101", data=None):
    return BidDraftIn(name=name, number=number, data=data if data is not None else _blob())


# ── Role gate: writer-only, on every route ───────────────────────────────────


def test_every_route_requires_writer():
    for route in bd.router.routes:
        assert any(
            d.call is require_writer for d in route.dependant.dependencies
        ), f"{route.path} missing require_writer"


@pytest.mark.parametrize("role", [Role.ACCOUNTANT, Role.ESTIMATOR])
def test_accountant_and_estimator_are_403(role):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_writer(user=_user(role)))
    assert exc.value.status_code == 403


# ── CRUD flow ────────────────────────────────────────────────────────────────


def test_create_stamps_the_caller(db):
    created = bd.create_draft(_draft_in(), user=_user(uid="admin-1"))
    assert created["created_by"] == "admin-1"
    assert created["name"] == "Lab Fit-Out" and created["number"] == "26-101"
    assert created["data"] == _blob()


def test_list_is_light_and_most_recently_touched_first(db):
    a = bd.create_draft(_draft_in(name="A"), user=_user())
    bd.create_draft(_draft_in(name="B"), user=_user())
    rows = bd.list_drafts(user=_user())
    assert [r["name"] for r in rows] == ["B", "A"]
    assert all("data" not in r for r in rows)
    # Touching A moves it back to the top.
    bd.update_draft(a["id"], _draft_in(name="A2"), user=_user())
    assert [r["name"] for r in bd.list_drafts(user=_user())] == ["A2", "B"]


def test_any_writer_reads_and_updates_any_draft(db):
    created = bd.create_draft(_draft_in(), user=_user(uid="admin-1"))
    other = _user(role=Role.ESTIMATING_ENGINEER_LABOR, uid="eng-1")
    assert bd.get_draft(created["id"], user=other)["data"] == _blob()
    updated = bd.update_draft(
        created["id"], _draft_in(name="Renamed", number="26-102"), user=other
    )
    assert updated["name"] == "Renamed" and updated["number"] == "26-102"
    # created_by is untouched by later editors.
    assert db.tables["bid_drafts"][0]["created_by"] == "admin-1"


def test_put_replaces_the_blob(db):
    created = bd.create_draft(_draft_in(), user=_user())
    new_blob = _blob(isNgem=True, selectedGcs=["gc-2", "gc-3"])
    bd.update_draft(created["id"], _draft_in(data=new_blob), user=_user())
    assert bd.get_draft(created["id"], user=_user())["data"] == new_blob


def test_delete_then_get_404s(db):
    created = bd.create_draft(_draft_in(), user=_user())
    assert bd.delete_draft(created["id"], user=_user()) is None  # 204, no body
    assert db.tables["bid_drafts"] == []
    with pytest.raises(HTTPException) as exc:
        bd.get_draft(created["id"], user=_user())
    assert exc.value.status_code == 404


def test_unknown_id_404s_on_get_put_delete(db):
    for call in (
        lambda: bd.get_draft("missing", user=_user()),
        lambda: bd.update_draft("missing", _draft_in(), user=_user()),
        lambda: bd.delete_draft("missing", user=_user()),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


# ── Validation ───────────────────────────────────────────────────────────────


def test_name_and_number_are_stripped():
    d = BidDraftIn(name="  Lab Fit-Out  ", number=" 26-101 ")
    assert d.name == "Lab Fit-Out" and d.number == "26-101"


@pytest.mark.parametrize("field", ["name", "number"])
@pytest.mark.parametrize("value", ["", "   \n\t "])
def test_blank_name_or_number_rejected(field, value):
    body = {"name": "x", "number": "1", field: value}
    with pytest.raises(ValidationError):
        BidDraftIn.model_validate(body)


def test_oversize_name_and_number_rejected():
    with pytest.raises(ValidationError):
        BidDraftIn(name="x" * (bd.DRAFT_NAME_MAX + 1), number="1")
    with pytest.raises(ValidationError):
        BidDraftIn(name="x", number="1" * (bd.DRAFT_NUMBER_MAX + 1))


@pytest.mark.parametrize("data", [[1, 2], "nope", 7, True])
def test_data_must_be_an_object(data):
    with pytest.raises(ValidationError):
        BidDraftIn.model_validate({"name": "x", "number": "1", "data": data})


def test_data_defaults_to_empty_object():
    assert BidDraftIn(name="x", number="1").data == {}


# ── Confidential fields.actual_bid_at ────────────────────────────────────────

EXEC = _user(role=Role.EXECUTIVE, uid="exec-1")
ENGINEER = _user(role=Role.ESTIMATING_ENGINEER_MATERIALS, uid="eng-1")


def _secret_blob(**fields):
    return _blob(fields={"internal_bid_at": "2026-08-18T17:00:00+00:00",
                         "actual_bid_at": SECRET, **fields})


def test_viewer_roles_receive_actual_bid_at(db):
    created = bd.create_draft(_draft_in(data=_secret_blob()), user=EXEC)
    for role in (Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN):
        got = bd.get_draft(created["id"], user=_user(role=role))
        assert got["data"]["fields"]["actual_bid_at"] == SECRET


def test_engineer_responses_never_carry_actual_bid_at(db):
    created = bd.create_draft(_draft_in(data=_secret_blob()), user=EXEC)
    got = bd.get_draft(created["id"], user=ENGINEER)
    assert "actual_bid_at" not in got["data"]["fields"]
    # The rest of the blob is untouched, and redaction copies rather than
    # mutating the stored row.
    assert got["data"]["fields"]["internal_bid_at"] == "2026-08-18T17:00:00+00:00"
    assert got["data"]["selectedGcs"] == ["gc-1"]
    assert db.tables["bid_drafts"][0]["data"]["fields"]["actual_bid_at"] == SECRET


def test_engineer_post_cannot_store_actual_bid_at(db):
    created = bd.create_draft(_draft_in(data=_secret_blob()), user=ENGINEER)
    assert "actual_bid_at" not in created["data"]["fields"]
    assert "actual_bid_at" not in db.tables["bid_drafts"][0]["data"]["fields"]


def test_engineer_put_preserves_the_stored_value(db):
    created = bd.create_draft(_draft_in(data=_secret_blob()), user=EXEC)
    # The engineer edits other fields; their blob has no actual_bid_at at all
    # (they never received it), which must NOT clear the stored one.
    theirs = _blob(fields={"internal_bid_at": "2026-08-19T17:00:00+00:00"}, isNgem=True)
    updated = bd.update_draft(created["id"], _draft_in(data=theirs), user=ENGINEER)
    assert "actual_bid_at" not in updated["data"]["fields"]
    # Re-read as executive: the confidential value survived, the edits took.
    got = bd.get_draft(created["id"], user=EXEC)
    assert got["data"]["fields"]["actual_bid_at"] == SECRET
    assert got["data"]["fields"]["internal_bid_at"] == "2026-08-19T17:00:00+00:00"
    assert got["data"]["isNgem"] is True


def test_engineer_put_cannot_smuggle_a_value(db):
    created = bd.create_draft(_draft_in(data=_secret_blob()), user=EXEC)
    forged = _secret_blob()
    forged["fields"]["actual_bid_at"] = "1999-01-01T00:00:00+00:00"
    bd.update_draft(created["id"], _draft_in(data=forged), user=ENGINEER)
    got = bd.get_draft(created["id"], user=EXEC)
    assert got["data"]["fields"]["actual_bid_at"] == SECRET


def test_engineer_put_carries_the_value_even_without_a_fields_dict(db):
    created = bd.create_draft(_draft_in(data=_secret_blob()), user=EXEC)
    bd.update_draft(created["id"], _draft_in(data={"isNgem": True}), user=ENGINEER)
    got = bd.get_draft(created["id"], user=EXEC)
    assert got["data"]["fields"]["actual_bid_at"] == SECRET


def test_editor_put_may_change_or_clear_the_value(db):
    created = bd.create_draft(_draft_in(data=_secret_blob()), user=EXEC)
    later = "2026-08-25T18:00:00+00:00"
    bd.update_draft(created["id"], _draft_in(data=_secret_blob(actual_bid_at=later)), user=EXEC)
    assert bd.get_draft(created["id"], user=EXEC)["data"]["fields"]["actual_bid_at"] == later
    # Clearing: an editor's blob without the key removes it.
    cleared = _blob(fields={"internal_bid_at": "2026-08-18T17:00:00+00:00"})
    bd.update_draft(created["id"], _draft_in(data=cleared), user=EXEC)
    assert "actual_bid_at" not in bd.get_draft(created["id"], user=EXEC)["data"]["fields"]


def test_malformed_fields_shapes_are_tolerated(db):
    # The blob is client-supplied: a non-dict `fields` must neither crash a
    # write nor a redacted read.
    created = bd.create_draft(_draft_in(data={"fields": ["not", "a", "dict"]}), user=ENGINEER)
    got = bd.get_draft(created["id"], user=ENGINEER)
    assert got["data"]["fields"] == ["not", "a", "dict"]
