"""Submittal-response ingestion — match_send / record_response and the
email_ingest._step_r1 hook, against an in-memory fake Supabase.

Guards under test: only an INBOUND email whose from-address matches the send's
contact flips the response (the outbound Sent-Items copy assigns the project but
is not a reply); recording is idempotent; the first-response timestamp is set
once and never overwritten.
"""

from types import SimpleNamespace

import pytest

from app.services import email_ingest
from app.services import submittal_ingest as si


# ── Fake Supabase (same query surface as test_email_ingest's) ───────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._op, self._payload = None, None
        self._eq, self._neq, self._null, self._notnull, self._in = [], [], [], [], []
        self._negate_next = False

    def select(self, *a, **k):
        self._op = "select"
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

    @property
    def not_(self):
        self._negate_next = True
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def neq(self, col, val):
        self._neq.append((col, val))
        return self

    def is_(self, col, val):
        if self._negate_next:
            self._negate_next = False
            self._notnull.append(col)
        else:
            self._null.append(col)
        return self

    def in_(self, col, vals):
        self._in.append((col, list(vals)))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def _matches(self, row):
        return (
            all(row.get(c) == v for c, v in self._eq)
            and all(row.get(c) != v for c, v in self._neq)
            and all(row.get(c) is None for c in self._null)
            and all(row.get(c) is not None for c in self._notnull)
            and all(row.get(c) in v for c, v in self._in)
        )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [dict(r) for r in rows if self._matches(r)]
            return SimpleNamespace(data=hits, count=len(hits))
        if self._op == "insert":
            exc = self.db.raise_on_insert.get(self.table)
            if exc is not None:
                raise exc
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", f"{self.table}-{self.db.next_id()}")
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            hits = [dict(r) for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=hits)
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.raise_on_insert = {}
        self._id = 0

    def next_id(self):
        self._id += 1
        return self._id

    def table(self, name):
        return _Query(self, name)


MAILBOX = "t.moorejr@g3electrical.com"


def _send_row(**over):
    row = {
        "id": "s1",
        "request_id": "r1",
        "conversation_id": "c1",
        "response_received_at": None,
        "response_count": 0,
        "sent_by": None,  # None → _notify returns early, no real supabase touch
        "submittal_requests": {"project_id": "p1"},
        "vendor_contacts": {"email": "sales@acme.com"},
    }
    row.update(over)
    return row


def _match(**over):
    m = {
        "id": "s1",
        "request_id": "r1",
        "project_id": "p1",
        "contact_email": "sales@acme.com",
        "response_received_at": None,
        "sent_by": None,
    }
    m.update(over)
    return m


# ── match_send ─────────────────────────────────────────────────────────────


def test_match_send_none_when_absent():
    db = FakeDB({"submittal_request_sends": []})
    assert si.match_send(db, "c1") is None
    assert si.match_send(db, None) is None


def test_match_send_returns_project_and_contact():
    db = FakeDB({"submittal_request_sends": [_send_row()]})
    m = si.match_send(db, "c1")
    assert m["id"] == "s1"
    assert m["project_id"] == "p1"
    assert m["contact_email"] == "sales@acme.com"


# ── record_response ────────────────────────────────────────────────────────


def test_record_response_first_time_sets_received_and_count():
    db = FakeDB({"submittal_request_sends": [_send_row()], "submittal_response_emails": []})
    si.record_response(db, _match(), {"id": "e1", "from_address": "sales@acme.com"})
    links = db.tables["submittal_response_emails"]
    assert len(links) == 1 and links[0]["email_id"] == "e1"
    send = db.tables["submittal_request_sends"][0]
    assert send["response_received_at"] is not None
    assert send["response_count"] == 1


def test_record_response_idempotent():
    db = FakeDB({"submittal_request_sends": [_send_row()], "submittal_response_emails": []})
    si.record_response(db, _match(), {"id": "e1", "from_address": "sales@acme.com"})
    first_ts = db.tables["submittal_request_sends"][0]["response_received_at"]
    # Same email again — no-op.
    si.record_response(db, _match(), {"id": "e1", "from_address": "sales@acme.com"})
    assert len(db.tables["submittal_response_emails"]) == 1
    assert db.tables["submittal_request_sends"][0]["response_count"] == 1
    assert db.tables["submittal_request_sends"][0]["response_received_at"] == first_ts


def test_record_response_keeps_first_timestamp():
    db = FakeDB(
        {
            "submittal_request_sends": [_send_row(response_received_at="2026-01-01T00:00:00+00:00")],
            "submittal_response_emails": [{"id": "l0", "send_id": "s1", "email_id": "e0"}],
        }
    )
    # A second, distinct reply arrives.
    si.record_response(
        db,
        _match(response_received_at="2026-01-01T00:00:00+00:00"),
        {"id": "e1", "from_address": "sales@acme.com"},
    )
    send = db.tables["submittal_request_sends"][0]
    assert send["response_received_at"] == "2026-01-01T00:00:00+00:00"  # unchanged
    assert send["response_count"] == 2  # e0 + e1


# ── _step_r1 hook integration ──────────────────────────────────────────────


def _email(**over):
    row = {
        "id": "e1",
        "mailbox": MAILBOX,
        "direction": "inbound",
        "conversation_id": "c1",
        "from_address": "sales@acme.com",
        "status": "id_r1",
        "project_id": None,
    }
    row.update(over)
    return row


def _fresh_db(email):
    return FakeDB(
        {
            "submittal_request_sends": [_send_row()],
            "submittal_response_emails": [],
            "ingested_emails": [email],
            "email_conversation_projects": [],
        }
    )


def test_step_r1_inbound_reply_records_and_assigns():
    email = _email()
    db = _fresh_db(email)
    out = email_ingest._step_r1(db, email)
    assert out == "processed"
    # Email assigned to the request's project.
    row = db.tables["ingested_emails"][0]
    assert row["project_id"] == "p1" and row["status"] == "processed"
    assert row["matched_by"] == "conversation"
    # Response recorded on the send.
    assert len(db.tables["submittal_response_emails"]) == 1
    assert db.tables["submittal_request_sends"][0]["response_received_at"] is not None


def test_step_r1_outbound_copy_assigns_but_no_response():
    email = _email(direction="outbound")
    db = _fresh_db(email)
    out = email_ingest._step_r1(db, email)
    assert out == "processed"
    assert db.tables["ingested_emails"][0]["project_id"] == "p1"
    # The Sent-Items copy must NOT count as a vendor reply.
    assert db.tables["submittal_response_emails"] == []
    assert db.tables["submittal_request_sends"][0]["response_received_at"] is None


def test_step_r1_wrong_sender_assigns_but_no_response():
    email = _email(from_address="stranger@elsewhere.com")
    db = _fresh_db(email)
    out = email_ingest._step_r1(db, email)
    assert out == "processed"
    assert db.tables["ingested_emails"][0]["project_id"] == "p1"
    assert db.tables["submittal_response_emails"] == []
    assert db.tables["submittal_request_sends"][0]["response_received_at"] is None
