"""The PM Emails router (app/routers/emails) called as plain functions against
an in-memory fake Supabase, the pattern of tests/test_rfp_emails_router.py,
pinning the mailbox scoping of docs/ASSISTANT.md, "PM mailbox scoping fix".

What is pinned:

- The project list and the Unknown pool return only scoped rows, and total,
  pending_count and failed_count are scoped too.
- get_email on another person's mailbox gives a 404 identical to a random
  uuid, body included.
- download_attachment on another mailbox gives a 404 with no signed URL and no
  file_access_events row; a permitted download records the parent's project.
- assign and unassign on another mailbox give a 404 with no write.
- Executive and IT Admin see every mailbox except the excluded ones, and
  exactly one email.viewed_other_mailbox audit row is written per detail or
  attachment open outside own plus shared, none for own or shared rows.
- A viewer with an empty scope gets the empty envelope without a query.
- The route table still puts exactly one of require_internal,
  require_writer or (the classification writes) require_pm_write on every
  /emails route.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser, require_internal, require_pm_write, require_writer
from app.core.roles import Role
from app.models.schemas import EmailAssignIn
from app.routers import emails as rr
from app.services import file_access, notifications
from app.services import mailbox_visibility as vis

BIDS = "bids@g3electrical.com"
OFFICE = "office@g3electrical.com"
ADMIN_ASSISTANT = "adminassistant@g3electrical.com"
SHARED = {BIDS, OFFICE, ADMIN_ASSISTANT}
TOM = "tmoore@g3electrical.com"
TIESHA = "tiesha@g3electrical.com"
HR = "hr@g3electrical.com"

P1 = "5a000000-0000-4000-8000-000000000001"
E_TOM = "3f1c0b00-0000-4000-8000-000000000001"        # Tom's mailbox, on P1
E_TIESHA = "3f1c0b00-0000-4000-8000-000000000002"     # Tiesha's mailbox, on P1
E_BIDS = "3f1c0b00-0000-4000-8000-000000000003"       # shared, on P1
E_HR = "3f1c0b00-0000-4000-8000-000000000004"         # excluded, on P1
U_TOM = "3f1c0b00-0000-4000-8000-000000000011"        # Tom's mailbox, Unknown pool
U_TIESHA = "3f1c0b00-0000-4000-8000-000000000012"     # Tiesha's mailbox, Unknown pool
U_TIESHA_FAILED = "3f1c0b00-0000-4000-8000-000000000013"
U_HR = "3f1c0b00-0000-4000-8000-000000000014"
PEND_TOM = "3f1c0b00-0000-4000-8000-000000000021"     # pending, Tom's mailbox
PEND_TIESHA = "3f1c0b00-0000-4000-8000-000000000022"  # pending, Tiesha's mailbox
MISSING = "3f1c0b00-0000-4000-8000-0000000000ff"
A_TOM = "4e000000-0000-4000-8000-000000000001"
A_TIESHA = "4e000000-0000-4000-8000-000000000002"


# ── Fake Supabase ────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._ins = []
        self._neqs = []
        self._nulls = []
        self._count = None
        self._orders = []
        self._limit = None
        self._range = None

    def select(self, sel="*", count=None, **k):
        self._op, self._count = "select", count
        self.db.selects.append((self.table, sel))
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        self.db.updates.append((self.table, payload))
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def neq(self, col, val):
        self._neqs.append((col, val))
        return self

    def in_(self, col, vals):
        self._ins.append((col, list(vals)))
        return self

    def is_(self, col, val):
        assert val == "null"
        self._nulls.append(col)
        return self

    def or_(self, expr):
        self.db.ors.append(expr)
        return self

    def order(self, col, desc=False, **k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, **k):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def _matches(self, row):
        return (
            all(row.get(c) == v for c, v in self._filters)
            and all(row.get(c) != v for c, v in self._neqs)
            and all(row.get(c) in vals for c, vals in self._ins)
            and all(row.get(c) is None for c in self._nulls)
        )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            self.db.executed.append((self.table, list(self._ins), list(self._neqs)))
            hits = [r for r in rows if self._matches(r)]
            total = len(hits)
            for col, desc in reversed(self._orders):
                hits = sorted(hits, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._range is not None:
                start, end = self._range
                hits = hits[start : end + 1]
            if self._limit is not None:
                hits = hits[: self._limit]
            return SimpleNamespace(
                data=[copy.deepcopy(r) for r in hits],
                count=total if self._count else None,
            )
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            made = []
            for item in items:
                row = dict(item)
                row.setdefault("id", f"{self.table}-{len(rows) + len(made) + 1}")
                made.append(row)
            rows.extend(made)
            return SimpleNamespace(data=[dict(r) for r in made], count=None)
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out, count=None)
        return SimpleNamespace(data=[], count=None)


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.selects: list[tuple[str, str]] = []
        self.executed: list[tuple] = []
        self.updates: list[tuple[str, dict]] = []
        self.ors: list[str] = []

    def table(self, name):
        return _Query(self, name)


class _Ingest:
    """Stand-in for app/services/email_ingest: records the writes the router
    asks for, so a refused call is provably write-free."""

    def __init__(self):
        self.calls = []

    def assign_manual(self, sb, email, project_id, user_id):
        self.calls.append(("assign", email["id"], project_id, user_id))
        return {**email, "project_id": project_id}, 0

    def unassign(self, sb, email, user_id):
        self.calls.append(("unassign", email["id"], user_id))
        return {**email, "project_id": None}


def _email(eid, mailbox, *, project_id=P1, status="processed", at="2026-10-20T10:00:00+00:00"):
    return {
        "id": eid,
        "mailbox": mailbox,
        "folder": "inbox",
        "direction": "inbound",
        "conversation_id": f"conv-{eid[-2:]}",
        "from_address": "gc@example.com",
        "subject": f"Subject {eid[-2:]}",
        "body_text": "hello",
        "message_at": at,
        "created_at": at,
        "status": status,
        "project_id": project_id,
    }


@pytest.fixture()
def settings(monkeypatch):
    state = SimpleNamespace(shared_mailbox_set=set(SHARED), mailbox_exclusion_set={HR})
    monkeypatch.setattr(vis, "get_settings", lambda: state)
    return state


@pytest.fixture()
def db(monkeypatch, settings):
    fake = FakeDB()
    monkeypatch.setattr(rr, "get_supabase", lambda: fake)
    monkeypatch.setattr(notifications, "get_supabase", lambda: fake)
    monkeypatch.setattr(file_access, "get_supabase", lambda: fake)
    fake.tables["ingested_emails"] = [
        _email(E_TOM, TOM, at="2026-10-20T10:04:00+00:00"),
        _email(E_TIESHA, TIESHA, at="2026-10-20T10:03:00+00:00"),
        _email(E_BIDS, BIDS, at="2026-10-20T10:02:00+00:00"),
        _email(E_HR, HR, at="2026-10-20T10:01:00+00:00"),
        _email(U_TOM, TOM, project_id=None),
        _email(U_TIESHA, TIESHA, project_id=None),
        _email(U_TIESHA_FAILED, TIESHA, project_id=None, status="failed"),
        _email(U_HR, HR, project_id=None, status="failed"),
        _email(PEND_TOM, TOM, project_id=None, status="id_r1"),
        _email(PEND_TIESHA, TIESHA, project_id=None, status="received"),
    ]
    fake.tables["ingested_email_attachments"] = [
        {"id": A_TOM, "email_id": E_TOM, "filename": "tom.pdf", "storage_path": "emails/tom.pdf", "created_at": "x"},
        {"id": A_TIESHA, "email_id": E_TIESHA, "filename": "t.pdf", "storage_path": "emails/t.pdf", "created_at": "x"},
    ]
    fake.tables["projects"] = [{"id": P1, "name": "Job", "number": "26.10.7300"}]
    fake.tables["audit_log"] = []
    fake.tables["file_access_events"] = []
    return fake


@pytest.fixture()
def ingest(monkeypatch):
    stub = _Ingest()
    monkeypatch.setattr(rr, "email_ingest", stub)
    return stub


@pytest.fixture()
def signed(monkeypatch):
    calls = []

    def _signed_url(path, **kw):
        calls.append((path, kw))
        return f"https://signed/{path}"

    monkeypatch.setattr(rr.storage, "signed_url", _signed_url)
    return calls


def _user(role, email, uid="u1", *, is_dev=False, mailboxes=()):
    return CurrentUser(
        id=uid, email=email, role=role, is_active=True, is_dev=is_dev,
        rfp_mailboxes=tuple(mailboxes),
    )


def _tom(role=Role.EXECUTIVE):
    return _user(role, TOM, uid="tom")


def _tiesha(role=Role.ESTIMATING_ENGINEER_LABOR, **kw):
    return _user(role, "tiesha.login@g3electrical.com", uid="tiesha", mailboxes=(TIESHA,), **kw)


def _ids(resp):
    return [r["id"] for r in resp["rows"]]


def _audits(db):
    return [r for r in db.tables["audit_log"] if r["action"] == "email.viewed_other_mailbox"]


# ── Lists and counts ─────────────────────────────────────────────────────


def test_project_list_returns_only_scoped_rows_and_a_scoped_total(db):
    resp = rr.list_project_emails(project_id=P1, limit=50, offset=0, user=_tiesha())
    assert _ids(resp) == [E_TIESHA, E_BIDS]
    assert resp["total"] == 2


def test_project_list_scope_survives_the_search_filter(db):
    resp = rr.list_project_emails(project_id=P1, limit=50, offset=0, q="Subject", user=_tiesha())
    assert _ids(resp) == [E_TIESHA, E_BIDS]
    assert resp["total"] == 2
    assert db.ors, "the search filter still went on the query"


@pytest.mark.parametrize("role", [Role.ESTIMATING_ADMIN, Role.ACCOUNTANT, Role.ESTIMATING_ENGINEER_MATERIALS])
def test_scoped_roles_never_see_an_executives_mailbox_in_the_list(db, role):
    resp = rr.list_project_emails(project_id=P1, limit=50, offset=0, user=_tiesha(role))
    assert E_TOM not in _ids(resp)
    assert E_HR not in _ids(resp)


def test_a_dev_wearing_a_scoped_role_is_scoped(db):
    resp = rr.list_project_emails(project_id=P1, limit=50, offset=0, user=_tiesha(Role.ESTIMATING_ADMIN, is_dev=True))
    assert _ids(resp) == [E_TIESHA, E_BIDS]


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.IT_ADMIN])
def test_full_view_roles_see_every_mailbox_except_the_excluded_ones(db, role):
    resp = rr.list_project_emails(project_id=P1, limit=50, offset=0, user=_tom(role))
    assert _ids(resp) == [E_TOM, E_TIESHA, E_BIDS]
    assert resp["total"] == 3
    # One neq per exclusion, no in_ filter.
    # The last ingested_emails query (the classification attach reads its
    # own table after it).
    table, ins, neqs = [e for e in db.executed if e[0] == "ingested_emails"][-1]
    assert table == "ingested_emails" and ins == [] and neqs == [("mailbox", HR)]
    # A list is not a detail open: nothing is audited.
    assert _audits(db) == []


def test_unknown_pool_rows_and_both_counts_are_scoped(db):
    resp = rr.list_unknown_emails(limit=50, offset=0, user=_tiesha())
    assert set(_ids(resp)) == {U_TIESHA, U_TIESHA_FAILED}
    assert resp["total"] == 2
    assert resp["pending_count"] == 1    # PEND_TIESHA only, never PEND_TOM
    assert resp["failed_count"] == 1     # U_TIESHA_FAILED only, never U_HR


def test_unknown_pool_for_a_full_view_role_counts_everything_but_the_exclusions(db):
    resp = rr.list_unknown_emails(limit=50, offset=0, user=_tom(Role.IT_ADMIN))
    assert set(_ids(resp)) == {U_TOM, U_TIESHA, U_TIESHA_FAILED}
    assert resp["pending_count"] == 2
    assert resp["failed_count"] == 1
    assert _audits(db) == []


def test_an_empty_scope_answers_the_empty_envelope_without_a_query(db, settings):
    settings.shared_mailbox_set = set()
    nobody = _user(Role.ESTIMATING_ADMIN, "   ", uid="n")
    listed = rr.list_project_emails(project_id=P1, limit=50, offset=0, user=nobody)
    unknown = rr.list_unknown_emails(limit=50, offset=0, user=nobody)
    assert listed == {"rows": [], "total": 0, "offset": 0, "limit": 50}
    assert unknown == {
        "rows": [], "total": 0, "offset": 0, "limit": 50,
        "pending_count": 0, "failed_count": 0,
    }
    assert db.executed == []


# ── Detail ───────────────────────────────────────────────────────────────


def test_get_email_on_another_mailbox_is_the_same_404_as_a_random_uuid(db):
    with pytest.raises(HTTPException) as not_yours:
        rr.get_email(E_TOM, user=_tiesha())
    with pytest.raises(HTTPException) as missing:
        rr.get_email(MISSING, user=_tiesha())
    assert not_yours.value.status_code == missing.value.status_code == 404
    assert not_yours.value.detail == missing.value.detail == "Email not found"
    assert not_yours.value.headers == missing.value.headers


def test_get_email_on_an_own_or_shared_row_works_for_a_scoped_role(db):
    assert rr.get_email(E_TIESHA, user=_tiesha())["id"] == E_TIESHA
    assert rr.get_email(E_BIDS, user=_tiesha(Role.ACCOUNTANT))["id"] == E_BIDS
    assert _audits(db) == []


def test_an_excluded_mailbox_404s_for_an_executive(db):
    with pytest.raises(HTTPException) as exc:
        rr.get_email(E_HR, user=_tom())
    assert exc.value.status_code == 404 and exc.value.detail == "Email not found"
    assert _audits(db) == []


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.IT_ADMIN])
def test_full_view_detail_open_outside_own_plus_shared_writes_exactly_one_audit_row(db, role):
    email = rr.get_email(E_TIESHA, user=_tom(role))
    assert email["id"] == E_TIESHA
    rows = _audits(db)
    assert len(rows) == 1
    assert rows[0]["actor_id"] == "tom"
    assert rows[0]["entity"] == "ingested_email"
    assert rows[0]["entity_id"] == E_TIESHA
    assert rows[0]["payload"] == {"mailbox": TIESHA, "email_id": E_TIESHA}


def test_full_view_detail_open_on_own_or_shared_rows_writes_no_audit_row(db):
    rr.get_email(E_TOM, user=_tom())
    rr.get_email(E_BIDS, user=_tom(Role.IT_ADMIN))
    assert _audits(db) == []


# ── Attachments ──────────────────────────────────────────────────────────


def test_download_on_another_mailbox_mints_no_url_and_records_no_access(db, signed):
    with pytest.raises(HTTPException) as exc:
        rr.download_attachment(E_TOM, A_TOM, user=_tiesha())
    assert exc.value.status_code == 404 and exc.value.detail == "Email not found"
    assert signed == []
    assert db.tables["file_access_events"] == []


def test_download_on_a_missing_email_is_the_same_404(db, signed):
    with pytest.raises(HTTPException) as exc:
        rr.download_attachment(MISSING, A_TOM, user=_tiesha())
    assert exc.value.status_code == 404 and exc.value.detail == "Email not found"
    assert signed == []


def test_download_on_an_own_row_signs_and_records_the_parents_project(db, signed):
    out = rr.download_attachment(E_TIESHA, A_TIESHA, user=_tiesha())
    assert out == {"url": "https://signed/emails/t.pdf", "filename": "t.pdf"}
    assert signed == [("emails/t.pdf", {"download": "t.pdf"})]
    [access] = db.tables["file_access_events"]
    assert access["project_id"] == P1
    assert access["file_id"] == A_TIESHA
    assert access["source"] == "email_attachment"
    assert _audits(db) == []


def test_download_cannot_pair_an_attachment_with_a_visible_but_wrong_email(db, signed):
    with pytest.raises(HTTPException) as exc:
        rr.download_attachment(E_TIESHA, A_TOM, user=_tiesha())
    assert exc.value.status_code == 404 and exc.value.detail == "Attachment not found"
    assert signed == []


def test_full_view_attachment_open_outside_own_plus_shared_writes_exactly_one_audit_row(db, signed):
    rr.download_attachment(E_TIESHA, A_TIESHA, user=_tom(Role.IT_ADMIN))
    assert len(signed) == 1
    rows = _audits(db)
    assert len(rows) == 1
    assert rows[0]["entity_id"] == E_TIESHA
    assert rows[0]["payload"]["mailbox"] == TIESHA


def test_full_view_attachment_open_on_own_row_writes_no_audit_row(db, signed):
    rr.download_attachment(E_TOM, A_TOM, user=_tom())
    assert _audits(db) == []


# ── Assign and unassign ──────────────────────────────────────────────────


def test_assign_on_another_mailbox_is_a_404_with_no_write(db, ingest):
    with pytest.raises(HTTPException) as exc:
        rr.assign_email(U_TOM, EmailAssignIn(project_id=P1), user=_tiesha())
    assert exc.value.status_code == 404 and exc.value.detail == "Email not found"
    assert ingest.calls == []
    assert db.updates == []


def test_unassign_on_another_mailbox_is_a_404_with_no_write(db, ingest):
    with pytest.raises(HTTPException) as exc:
        rr.unassign_email(E_TOM, user=_tiesha())
    assert exc.value.status_code == 404 and exc.value.detail == "Email not found"
    assert ingest.calls == []
    assert db.updates == []


def test_assign_and_unassign_on_own_rows_reach_the_service(db, ingest):
    out = rr.assign_email(U_TIESHA, EmailAssignIn(project_id=P1), user=_tiesha())
    assert out["email"]["project_id"] == P1 and out["retro_assigned_count"] == 0
    rr.unassign_email(E_TIESHA, user=_tiesha())
    assert ingest.calls == [("assign", U_TIESHA, P1, "tiesha"), ("unassign", E_TIESHA, "tiesha")]


def test_full_view_assign_outside_own_plus_shared_is_audited_once(db, ingest):
    rr.assign_email(U_TIESHA, EmailAssignIn(project_id=P1), user=_tom(Role.IT_ADMIN))
    assert ingest.calls == [("assign", U_TIESHA, P1, "tom")]
    assert len(_audits(db)) == 1


# ── Route table ──────────────────────────────────────────────────────────


def test_route_table_is_unchanged_and_every_route_has_exactly_one_role_gate():
    pairs = {(m, r.path) for r in rr.router.routes for m in r.methods}
    assert pairs == {
        ("GET", "/emails"),
        ("GET", "/emails/unknown"),
        ("GET", "/emails/{email_id}"),
        ("GET", "/emails/{email_id}/attachments/{attachment_id}/download"),
        ("POST", "/emails/{email_id}/assign"),
        ("POST", "/emails/{email_id}/unassign"),
        # PM email classification writes (docs/EMAIL_CLASSIFICATION.md).
        ("PUT", "/emails/{email_id}/classification"),
        ("DELETE", "/emails/{email_id}/classification/override"),
        ("POST", "/emails/{email_id}/classification/rerun"),
    }
    for route in rr.router.routes:
        calls = [d.call for d in route.dependant.dependencies]
        gates = [c for c in calls if c in (require_internal, require_writer, require_pm_write)]
        assert len(gates) == 1, route.path
        if "/classification" in route.path:
            expected = require_pm_write
        elif route.path.endswith(("/assign", "/unassign")):
            expected = require_writer
        else:
            expected = require_internal
        assert gates == [expected], route.path
