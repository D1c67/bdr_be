"""The 0172 provenance writers (docs/ASSISTANT_PHASE1_PLAN.md 3.12).

Every generic writer spreads `provenance.fields(<the row's actor>)` into its
insert, so a row written inside `acting_as_assistant` carries
{"actor_kind": "assistant", "assistant_run_id": <run>} and a human row sends
neither key (the column defaults record a human). Notifications carry the
pair in `metadata` instead. `_SELF_SCOPING_METADATA_KEYS` lets an approval
card's bells be dismissed without a project.

No database: `get_supabase` is patched with a recording in-memory fake, and
route handlers are called directly, as the other router tests do.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import app.services as services_pkg
from app.core import provenance
from app.core.deps import CurrentUser
from app.core.roles import Role

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "99999999-9999-4999-8999-999999999999"
RUN = "22222222-2222-4222-8222-222222222222"
PROJECT = "44444444-4444-4444-8444-444444444444"
PAIR = {"actor_kind": "assistant", "assistant_run_id": RUN}
KEYS = ("actor_kind", "assistant_run_id")


# ── recording fake ───────────────────────────────────────────────────────────


def _json_path(row, col):
    """metadata->>gc_id -> row["metadata"]["gc_id"]"""
    if "->>" not in col:
        return row.get(col)
    base, key = col.split("->>", 1)
    return (row.get(base) or {}).get(key)


class _Q:
    """Enough of the PostgREST builder: eq/neq/in_/is_ filters are honored,
    select strings are recorded, the rest are chainable no-ops."""

    def __init__(self, db, name):
        self.db, self.name = db, name
        self._op, self._payload = "select", None
        self._filters: list = []
        self._single = False

    def select(self, cols="*", *a, **k):
        self._op = "select"
        self.db.selects.append((self.name, cols))
        return self

    def insert(self, payload, *a, **k):
        self._op, self._payload = "insert", payload
        return self

    def upsert(self, payload, *a, **k):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: _json_path(r, c) == v)
        self.db.eqs.append((self.name, col, val))
        return self

    def neq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: _json_path(r, c) != v)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self._filters.append(lambda r, c=col: _json_path(r, c) in vals)
        return self

    def is_(self, col, val):
        want = None if val == "null" else val
        self._filters.append(lambda r, c=col: _json_path(r, c) == want)
        return self

    def single(self):
        self._single = True
        return self

    maybe_single = single

    def __getattr__(self, attr):
        # order, limit, range, gte, lte, or_, like, ...: chainable no-ops.
        return lambda *a, **k: self

    def _hits(self):
        return [r for r in self.db.tables.setdefault(self.name, [])
                if all(f(r) for f in self._filters)]

    def execute(self):
        rows = self.db.tables.setdefault(self.name, [])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                self.db.inserts.append((self.name, dict(p)))
                row = {"id": f"{self.name}-{len(rows) + 1}", **p}
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "update":
            hits = self._hits()
            self.db.updates.append((self.name, dict(self._payload)))
            for r in hits:
                r.update(self._payload)
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "delete":
            hits = self._hits()
            self.db.tables[self.name] = [r for r in rows if r not in hits]
            return SimpleNamespace(data=hits)
        hits = [dict(r) for r in self._hits()]
        if self._single:
            return SimpleNamespace(data=hits[0] if hits else None)
        return SimpleNamespace(data=hits)


class FakeSB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.inserts: list[tuple[str, dict]] = []
        self.updates: list[tuple[str, dict]] = []
        self.selects: list[tuple[str, str]] = []
        self.eqs: list[tuple[str, str, object]] = []

    def table(self, name):
        return _Q(self, name)

    def inserted(self, name) -> list[dict]:
        return [p for t, p in self.inserts if t == name]


def _user(uid=USER, role=Role.EXECUTIVE):
    return CurrentUser(id=uid, email="tom@g3.test", role=role, is_active=True)


def _patch(monkeypatch, db, *modules):
    for mod in modules:
        monkeypatch.setattr(mod, "get_supabase", lambda: db)


def _assert_human(row):
    assert not any(k in row for k in KEYS), row


def _assert_assistant(row):
    assert {k: row.get(k) for k in KEYS} == PAIR, row


def _both(run_writer, rows_of):
    """Run the writer outside and inside an assistant run; the rows it wrote
    carry no key outside and the pair inside."""
    run_writer()
    outside = rows_of()
    assert outside, "the writer wrote nothing"
    for r in outside:
        _assert_human(r)
    n = len(outside)
    with provenance.acting_as_assistant(USER, RUN):
        run_writer()
    inside = rows_of()[n:]
    assert inside, "the writer wrote nothing inside the run"
    for r in inside:
        _assert_assistant(r)


# ── notifications.py ─────────────────────────────────────────────────────────


@pytest.fixture
def notif_db(monkeypatch):
    from app.services import notifications

    db = FakeSB({
        "profiles": [
            {"id": USER, "role": "executive", "is_active": True},
            {"id": OTHER, "role": "estimating_admin", "is_active": True},
        ]
    })
    _patch(monkeypatch, db, notifications)
    monkeypatch.setattr(notifications.notification_email, "queue", lambda rows: None)
    return db


def test_audit_row_carries_the_pair_only_inside_a_run(notif_db):
    from app.services import notifications

    _both(lambda: notifications.audit(USER, "project.update", "project", PROJECT, {"a": 1}),
          lambda: notif_db.inserted("audit_log"))


def test_audit_row_for_another_actor_is_never_tagged(notif_db):
    from app.services import notifications

    with provenance.acting_as_assistant(USER, RUN):
        notifications.audit(OTHER, "x.y", "project", PROJECT)
    _assert_human(notif_db.inserted("audit_log")[0])


def test_notify_role_metadata_carries_the_pair_only_inside_a_run(notif_db):
    from app.services import notifications

    notifications.notify_role(Role.EXECUTIVE, PROJECT, "t", "m", mirror_email=False)
    assert "metadata" not in notif_db.inserted("notifications")[0]
    with provenance.acting_as_assistant(USER, RUN):
        notifications.notify_role(Role.EXECUTIVE, PROJECT, "t", "m", mirror_email=False,
                                  metadata={"gc_id": "g1"})
    row = notif_db.inserted("notifications")[1]
    assert row["metadata"] == {"gc_id": "g1", **PAIR}
    _assert_human(row)  # the pair rides in metadata, never in columns


def test_notify_roles_metadata_carries_the_pair_only_inside_a_run(notif_db):
    from app.services import notifications

    notifications.notify_roles([Role.EXECUTIVE, Role.ESTIMATING_ADMIN], PROJECT, "t", "m")
    first = notif_db.inserted("notifications")
    assert len(first) == 2 and all("metadata" not in r for r in first)
    with provenance.acting_as_assistant(USER, RUN):
        notifications.notify_roles([Role.EXECUTIVE, Role.ESTIMATING_ADMIN], PROJECT, "t", "m")
    second = notif_db.inserted("notifications")[2:]
    assert len(second) == 2 and all(r["metadata"] == PAIR for r in second)


def test_notify_user_metadata_carries_the_pair_only_inside_a_run(notif_db):
    from app.services import notifications

    md = {"request_id": "r1"}
    notifications.notify_user(OTHER, PROJECT, "t", "m", mirror_email=False, metadata=md)
    assert notif_db.inserted("notifications")[0]["metadata"] == {"request_id": "r1"}
    with provenance.acting_as_assistant(USER, RUN):
        notifications.notify_user(OTHER, PROJECT, "t", "m", mirror_email=False, metadata=md)
        notifications.notify_user(OTHER, None, "t", "m", mirror_email=False)
    rows = notif_db.inserted("notifications")
    assert rows[1]["metadata"] == {"request_id": "r1", **PAIR}
    assert rows[2]["metadata"] == PAIR
    # The caller's dict is never mutated.
    assert md == {"request_id": "r1"}


@pytest.mark.parametrize("key", ["approval_id", "assistant_conversation_id", "help_id"])
def test_self_scoping_keys_dismiss_without_a_project(notif_db, key):
    from app.services import notifications

    assert key in notifications._SELF_SCOPING_METADATA_KEYS
    notif_db.tables["notifications"] = [
        {"id": "n1", "type": "assistant.approval_needed", "dismissed_at": None,
         "metadata": {key: "x1"}},
        {"id": "n2", "type": "assistant.approval_needed", "dismissed_at": None,
         "metadata": {key: "x2"}},
    ]
    notifications.dismiss_notifications(types=["assistant.approval_needed"],
                                        metadata_eq={key: "x1"})
    by_id = {r["id"]: r for r in notif_db.tables["notifications"]}
    assert by_id["n1"]["dismissed_at"] == "now()" and by_id["n2"]["dismissed_at"] is None


def test_an_unscoped_dismissal_still_raises(notif_db):
    from app.services import notifications

    with pytest.raises(ValueError):
        notifications.dismiss_notifications(types=["x"], metadata_eq={"gc_id": "g1"})
    with pytest.raises(ValueError):
        notifications.dismiss_notifications(types=["x"], metadata_eq={"approval_id": ""})


# ── stage events ─────────────────────────────────────────────────────────────


def test_workflow_emit_event(monkeypatch):
    from app.services import workflow

    db = FakeSB()
    _patch(monkeypatch, db, workflow)
    _both(lambda: workflow._emit_event(PROJECT, "intake", None, "intake", USER, None),
          lambda: db.inserted("stage_events"))


def test_pm_stage_event(monkeypatch):
    from app.services import pm_workflow

    db = FakeSB()
    _patch(monkeypatch, db, pm_workflow)
    _both(lambda: pm_workflow.append_pm_stage_event(PROJECT, None, "precon", USER),
          lambda: db.inserted("pm_stage_events"))


def test_rfp_create_seed_state_stage_event():
    from app.services import rfp_create

    db = FakeSB()
    # The system path: no actor at all, so only a run tags it.
    _both(lambda: rfp_create._seed_state(db, PROJECT, None),
          lambda: db.inserted("stage_events"))
    # project_category_state is exempt (current-state rows).
    for r in db.inserted("project_category_state"):
        _assert_human(r)


# ── notes ────────────────────────────────────────────────────────────────────


def test_create_note_row_and_bells(monkeypatch):
    from app.routers import notes
    from app.services import notifications

    est = "55555555-5555-4555-8555-555555555555"
    db = FakeSB({
        "projects": [{"id": PROJECT, "name": "Acme", "number": "26.1.1"}],
        "profiles": [{"id": USER, "full_name": "Tom Moore", "role": "executive",
                      "is_active": True}],
        "estimator_assignments": [{"project_id": PROJECT, "estimator_id": est,
                                   "revoked_at": None}],
    })
    _patch(monkeypatch, db, notes, notifications)
    monkeypatch.setattr(notes.notifications, "deliver", lambda rows, **k: None)

    def write():
        notes.create_note(PROJECT, notes.NoteIn(body="Walk moved to Friday."), _user())

    _both(write, lambda: db.inserted("estimator_notes"))
    audits = db.inserted("audit_log")
    _assert_human(audits[0])
    _assert_assistant(audits[1])
    bells = db.inserted("notifications")
    assert len(bells) == 2
    assert "metadata" not in bells[0]
    assert bells[1]["metadata"] == PAIR


def test_mirror_send_message(monkeypatch):
    from app.services import estimator_notes

    db = FakeSB()
    _patch(monkeypatch, db, estimator_notes)
    _both(lambda: estimator_notes.mirror_send_message(
              project_id=PROJECT, author_id=USER, message="Please price by Friday.",
              source=estimator_notes.SOURCE_PACKAGE_SEND),
          lambda: db.inserted("estimator_notes"))


def test_add_quote_note(monkeypatch):
    from app.models.schemas import QuoteNoteIn
    from app.routers import rfqs
    from app.services import notifications

    db = FakeSB()
    _patch(monkeypatch, db, rfqs, notifications)
    monkeypatch.setattr(rfqs, "_rfq_in_project", lambda sb, pid, rid: {"id": rid})
    monkeypatch.setattr(rfqs, "_quote_in_rfq", lambda sb, rid, qid: {"id": qid})
    _both(lambda: rfqs.add_quote_note(PROJECT, "rfq-1", "q-1", QuoteNoteIn(body="Lead 12 wk"),
                                      _user()),
          lambda: db.inserted("quote_notes"))


# ── files, email, proposals, splitter ────────────────────────────────────────


def test_file_access_row(monkeypatch):
    from app.services import file_access

    db = FakeSB()
    _patch(monkeypatch, db, file_access)
    _both(lambda: file_access.record(_user(), action="view", source="project_file",
                                     file_id="f1", project_id=PROJECT),
          lambda: db.inserted("file_access_events"))
    with provenance.acting_as_assistant(USER, RUN):
        file_access.record(_user(OTHER), action="view", source="project_file",
                           file_id="f1", project_id=PROJECT)
    _assert_human(db.inserted("file_access_events")[-1])


def test_graph_send_mail_log_row(monkeypatch):
    from app.services import graph_email

    db = FakeSB()
    _patch(monkeypatch, db, graph_email)
    monkeypatch.setattr(graph_email, "_active_test_session", lambda sb=None: None)

    def _no_token():
        raise RuntimeError("no Graph in tests")

    monkeypatch.setattr(graph_email, "_acquire_token", _no_token)

    def send():
        with pytest.raises(RuntimeError):
            graph_email.send_mail(to=["gc@example.com"], subject="s", body_html="<p>b</p>",
                                  project_id=PROJECT, sent_by=USER)

    _both(send, lambda: db.inserted("email_log"))


def test_record_send_event(monkeypatch):
    from app.services import proposal_send

    db = FakeSB()
    _patch(monkeypatch, db, proposal_send)
    row = {"id": "ps-1", "project_id": PROJECT, "gc_id": "g1", "file_id": "f1"}
    _both(lambda: proposal_send.record_send_event(
              row, kind="resend", via="email", recipients=["gc@example.com"],
              email_log_id=None, user_id=USER),
          lambda: db.inserted("proposal_send_events"))


@pytest.fixture
def fake_auto_send(monkeypatch):
    """The same stand-in `app.services.auto_send` the auto send tests use."""

    def _install(**funcs):
        mod = types.ModuleType("app.services.auto_send")
        for name, fn in funcs.items():
            setattr(mod, name, fn)
        monkeypatch.setitem(sys.modules, "app.services.auto_send", mod)
        monkeypatch.setattr(services_pkg, "auto_send", mod, raising=False)
        return mod

    return _install


def test_resend_claim_row(monkeypatch, fake_auto_send):
    from app.services import proposal_send as psend
    from tests.test_auto_send_integrations import PID, _send_env

    db, _ = _send_env(monkeypatch, fake_auto_send, "sent")
    monkeypatch.setattr(psend, "send_events_by_proposal", lambda pid: {})
    psend.resend_proposals(PID, USER, ["ps-A"])
    claims = [r for r in db.tables["proposal_send_events"] if r.get("status") == "sending"
              or r.get("kind") == "resend"]
    assert claims and all(not any(k in r for k in KEYS) for r in claims)
    db.tables["proposal_send_events"] = []
    with provenance.acting_as_assistant(USER, RUN):
        psend.resend_proposals(PID, USER, ["ps-A"])
    claims = db.tables["proposal_send_events"]
    assert claims and all({k: r.get(k) for k in KEYS} == PAIR for r in claims)


def test_insert_file_row(monkeypatch):
    from app.routers import files

    db = FakeSB()
    _patch(monkeypatch, db, files)

    def write():
        files._insert_file_row({"project_id": PROJECT, "uploaded_by": USER,
                                "filename": "E1.pdf", "storage_path": "p/E1.pdf"}, "p/E1.pdf")

    _both(write, lambda: db.inserted("project_files"))
    with provenance.acting_as_assistant(USER, RUN):
        files._insert_file_row({"project_id": PROJECT, "uploaded_by": OTHER}, "p/x.pdf")
    _assert_human(db.inserted("project_files")[-1])


def test_bid_splitter_create_job(monkeypatch):
    from app.routers import bid_splitter

    db = FakeSB()
    _patch(monkeypatch, db, bid_splitter)
    monkeypatch.setattr(bid_splitter.llm, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(bid_splitter.llm, "active_model", lambda *a, **k: "m")
    monkeypatch.setattr(bid_splitter, "audit", lambda *a, **k: None)
    _both(lambda: bid_splitter.create_job(bid_splitter.BidSplitJobIn(file_count=1), _user()),
          lambda: db.inserted("bid_split_jobs"))


def test_rfp_file_drop_job(monkeypatch):
    from app.services import rfp_file_drop

    db = FakeSB()
    monkeypatch.setattr(rfp_file_drop.llm, "active_model", lambda *a, **k: "m")
    settings = SimpleNamespace(bid_split_max_files_per_job=10)
    _both(lambda: rfp_file_drop.drop_job(db, PROJECT, USER, None, settings),
          lambda: db.inserted("bid_split_jobs"))
