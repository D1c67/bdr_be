"""Send-batch lifecycle + the role-shaped Plans & Specs Log / hand-off readers
(app/services/file_sends.py).

The critical invariants under test:
  * claim-before-send: the batch row AND its recipient AND its file-link rows are
    written in one call, BEFORE any email; a racing initial send 409s here.
  * first-send-wins: stamp_sent carries the sent_to_estimators_at NULL guard so a
    reassign re-sending an already-sent file never resets the stamp.
  * two role-shaped projections, never one payload post-filtered: the estimator
    log omits the recipients/sent_by_name keys entirely and collapses reassign to
    initial, and the hand-off returns only the caller's own assignment.

Uses a chainable Supabase fake that records ops and serves queued responses per
(table, op) — the same shape as tests/test_estimator_rounds.py."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import files as files_mod
from app.services import file_sends


# ── chainable Supabase fake ────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name, self.op = db, table, "select"
        self.payload = None
        self.filters: list[tuple] = []

    def select(self, *cols, count=None):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, tuple(vals)))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def single(self):
        return self

    def execute(self):
        self.db.calls.append(self)
        queue = self.db.queues.get((self.table_name, self.op)) or []
        resp = queue.pop(0) if queue else []
        if isinstance(resp, Exception):
            raise resp
        count = len(resp) if isinstance(resp, list) else None
        return SimpleNamespace(data=resp, count=count)


class _FakeDB:
    def __init__(self):
        self.queues: dict[tuple[str, str], list] = {}
        self.calls: list[_Query] = []

    def queue(self, table, op, *responses):
        self.queues.setdefault((table, op), []).extend(responses)

    def table(self, name):
        return _Query(self, name)

    def ops(self, table, op):
        return [c for c in self.calls if c.table_name == table and c.op == op]


def _patch(monkeypatch, db):
    monkeypatch.setattr(file_sends, "get_supabase", lambda: db)


def _writer(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(
        id=uid, email="w@g3.com", role=role, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )


def _estimator(uid="e2"):
    return CurrentUser(
        id=uid, email="est@ext.com", role=Role.ESTIMATOR, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )


def _file_row(fid, category, **kw):
    row = {
        "id": fid, "category": category, "filename": f"{fid}.pdf", "size_bytes": 10,
        "note": None, "addendum_number": None, "addendum_issued_on": None,
        "sent_to_estimators_at": None, "uploaded_by": "x",
    }
    row.update(kw)
    return row


# ── claim_batch ────────────────────────────────────────────────────────────


def test_claim_batch_writes_batch_recipients_and_files_before_send(monkeypatch):
    db = _FakeDB()
    db.queue("file_send_batches", "insert", [{"id": "b1"}])
    db.queue("file_send_recipients", "insert", [])
    db.queue("file_send_batch_files", "insert", [])
    _patch(monkeypatch, db)

    batch = file_sends.claim_batch(
        project_id="p1", kind="initial", sent_by="u1", message="hi",
        recipients=[{"estimator_id": "e1", "email": "a@x.com", "full_name": "A"}],
        file_ids=["f1", "f2"], summary={"drawing": 2},
    )
    assert batch == {"id": "b1"}

    bi = db.ops("file_send_batches", "insert")[0]
    assert bi.payload["kind"] == "initial"
    assert bi.payload["summary"] == {"drawing": 2}

    ri = db.ops("file_send_recipients", "insert")[0]
    assert ri.payload[0]["batch_id"] == "b1"
    assert ri.payload[0]["email"] == "a@x.com"

    fi = db.ops("file_send_batch_files", "insert")[0]
    assert {r["file_id"] for r in fi.payload} == {"f1", "f2"}

    # Ordering: the batch row is committed BEFORE its children (and before any
    # email the caller would compose).
    order = [c.table_name for c in db.calls]
    assert order.index("file_send_batches") < order.index("file_send_recipients")
    assert order.index("file_send_batches") < order.index("file_send_batch_files")


def test_claim_batch_dedupes_recipients_by_email(monkeypatch):
    db = _FakeDB()
    db.queue("file_send_batches", "insert", [{"id": "b1"}])
    db.queue("file_send_recipients", "insert", [])
    db.queue("file_send_batch_files", "insert", [])
    _patch(monkeypatch, db)

    file_sends.claim_batch(
        project_id="p1", kind="initial", sent_by="u1", message=None,
        recipients=[
            {"email": "a@x.com", "estimator_id": "e1"},
            {"email": "a@x.com", "estimator_id": "e2"},  # dup address → collapsed
            {"email": "", "estimator_id": "e3"},          # no address → dropped
        ],
        file_ids=["f1", "f1"], summary={},   # dup file id → collapsed
    )
    ri = db.ops("file_send_recipients", "insert")[0]
    assert len(ri.payload) == 1
    assert ri.payload[0]["email"] == "a@x.com"
    fi = db.ops("file_send_batch_files", "insert")[0]
    assert len(fi.payload) == 1


def test_claim_batch_racing_initial_is_409_before_children(monkeypatch):
    # The unique partial index turns a second racing initial send into a 23505,
    # surfaced as 409 HERE (before any email is composed) — and because the batch
    # row is written first, no recipient/file rows are written either.
    db = _FakeDB()
    db.queue(
        "file_send_batches", "insert",
        Exception('duplicate key value violates unique constraint "..._one_initial_idx" (23505)'),
    )
    _patch(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        file_sends.claim_batch(
            project_id="p1", kind="initial", sent_by="u1", message=None,
            recipients=[{"email": "a@x.com"}], file_ids=["f1"], summary={},
        )
    assert exc.value.status_code == 409
    assert db.ops("file_send_recipients", "insert") == []
    assert db.ops("file_send_batch_files", "insert") == []


# ── abandon_batch / has_initial_send ───────────────────────────────────────


def test_abandon_batch_deletes_the_batch(monkeypatch):
    db = _FakeDB()
    db.queue("file_send_batches", "delete", [])
    _patch(monkeypatch, db)
    file_sends.abandon_batch("b1")
    d = db.ops("file_send_batches", "delete")[0]
    assert ("eq", "id", "b1") in d.filters


def test_has_initial_send_reflects_the_batch_table(monkeypatch):
    # abandon_batch removing the sole initial batch → has_initial_send False, so
    # "Upload plans and specs" stays visible (§8.2 #4, verified via the table read).
    db = _FakeDB()
    db.queue("file_send_batches", "select", [])
    db.queue("file_send_batches", "select", [{"id": "b1"}])
    _patch(monkeypatch, db)
    assert file_sends.has_initial_send("p1") is False
    assert file_sends.has_initial_send("p1") is True


# ── stamp_sent (first-send-wins NULL guard) ────────────────────────────────


def test_stamp_sent_carries_null_guard(monkeypatch):
    db = _FakeDB()
    db.queue("project_files", "update", [])
    _patch(monkeypatch, db)
    file_sends.stamp_sent(["f1", "f2"])
    u = db.ops("project_files", "update")[0]
    assert ("is", "sent_to_estimators_at", "null") in u.filters   # a reassign never resets it
    assert ("in", "id", ("f1", "f2")) in u.filters


def test_stamp_sent_noop_on_empty(monkeypatch):
    db = _FakeDB()
    _patch(monkeypatch, db)
    file_sends.stamp_sent([])
    assert db.ops("project_files", "update") == []


# ── batch_stats ────────────────────────────────────────────────────────────


def test_batch_stats_package_sent_at_is_the_initial(monkeypatch):
    db = _FakeDB()
    db.queue("file_send_batches", "select", [
        {"id": "b1", "kind": "initial", "sent_at": "2026-07-01T00:00:00Z"},
        {"id": "b2", "kind": "revision", "sent_at": "2026-07-03T00:00:00Z"},
    ])
    _patch(monkeypatch, db)
    s = file_sends.batch_stats("p1")
    assert s["batch_count"] == 2
    assert s["package_sent_at"] == "2026-07-01T00:00:00Z"
    assert s["first_sent_at"] == "2026-07-01T00:00:00Z"
    assert s["last_sent_at"] == "2026-07-03T00:00:00Z"


def test_batch_stats_scoped_to_recipient(monkeypatch):
    # §8.2 #11: an estimator is never counted for a batch they did not receive —
    # a project-wide count would leak that earlier sends (and other recipients)
    # exist.
    db = _FakeDB()
    db.queue("file_send_batches", "select", [
        {"id": "b1", "kind": "initial", "sent_at": "2026-07-01T00:00:00Z"},
        {"id": "b2", "kind": "revision", "sent_at": "2026-07-03T00:00:00Z"},
    ])
    db.queue("file_send_recipients", "select", [{"batch_id": "b2"}])  # only in b2
    _patch(monkeypatch, db)
    s = file_sends.batch_stats("p1", estimator_id="e2")
    assert s["batch_count"] == 1
    assert s["package_sent_at"] is None  # the initial batch is not theirs
    assert s["last_sent_at"] == "2026-07-03T00:00:00Z"


# ── prior_batches ──────────────────────────────────────────────────────────


def test_prior_batches_returns_summaries_in_order(monkeypatch):
    db = _FakeDB()
    db.queue("file_send_batches", "select", [
        {"kind": "initial", "sent_at": "2026-07-01T00:00:00Z", "summary": {"drawing": 2}},
        {"kind": "revision", "sent_at": "2026-07-03T00:00:00Z", "summary": {"revision": 1}},
    ])
    _patch(monkeypatch, db)
    out = file_sends.prior_batches("p1")
    assert [b["kind"] for b in out] == ["initial", "revision"]
    assert out[0]["summary"] == {"drawing": 2}
    assert out[1]["summary"] == {"revision": 1}


# ── build_log ──────────────────────────────────────────────────────────────


def _queue_internal_log(db):
    db.queue("file_send_batches", "select", [{
        "id": "b1", "kind": "initial", "sent_at": "2026-07-01T00:00:00Z",
        "message": "hi", "reconstructed": False,
        "summary": {"drawing": 2, "addendum_numbers": ["3A"]}, "sent_by": "u1",
    }])
    db.queue("file_send_batch_files", "select", [{"batch_id": "b1", "file_id": "f1"}])
    db.queue("project_files", "select", [_file_row("f1", "drawing")])
    db.queue("file_send_recipients", "select", [
        {"batch_id": "b1", "estimator_id": "e1", "email": "a@x.com", "full_name": "A Bidder"},
    ])
    db.queue("profiles", "select", [{"id": "u1", "full_name": "Staff One"}])


def test_build_log_internal_has_recipients_and_sender(monkeypatch):
    db = _FakeDB()
    _queue_internal_log(db)
    _patch(monkeypatch, db)
    out = file_sends.build_log("p1", _writer())
    assert out["viewer"] == "internal"
    b = out["batches"][0]
    assert b["kind"] == "initial"
    assert b["counts"] == {"drawing": 2}                # addendum_numbers list dropped
    assert b["recipients"][0]["email"] == "a@x.com"
    assert b["sent_by_name"] == "Staff One"
    assert b["files"][0]["available"] is True


def test_build_log_accountant_matches_writer_shape(monkeypatch):
    # §8.2 #7: the read-only accountant is an internal viewer — identical shape.
    db = _FakeDB()
    _queue_internal_log(db)
    _patch(monkeypatch, db)
    out = file_sends.build_log("p1", _writer(role=Role.ACCOUNTANT, uid="acc"))
    assert out["viewer"] == "internal"
    b = out["batches"][0]
    assert "recipients" in b
    assert "sent_by_name" in b


def test_build_log_estimator_omits_identity_and_collapses_reassign(monkeypatch):
    # §8.2 #8/#9/#10: estimator sees only batches addressed to them; recipients
    # and sent_by_name keys are ABSENT; reassign collapses to initial; a hidden
    # file is emitted as available:false (not omitted, not a live link); counts
    # come from the snapshot even when a linked file is gone. The rival rows and
    # profiles are queued but never popped — if a regression loaded them for the
    # estimator, they would leak into the JSON and fail the privacy assertion.
    db = _FakeDB()
    db.queue("file_send_batches", "select", [
        {"id": "b1", "kind": "reassign", "sent_at": "2026-07-05T00:00:00Z",
         "message": None, "reconstructed": False, "summary": {"drawing": 2}, "sent_by": "u1"},
        {"id": "b0", "kind": "initial", "sent_at": "2026-07-01T00:00:00Z",
         "message": None, "reconstructed": False, "summary": {"drawing": 2}, "sent_by": "u9"},
    ])
    db.queue("file_send_recipients", "select", [{"batch_id": "b1"}])          # only b1 is theirs
    db.queue("file_send_recipients", "select", [
        {"batch_id": "b1", "email": "rival@example.com", "full_name": "Rival Bidder"},
    ])  # a regression's recipient-output load would pop THIS
    db.queue("profiles", "select", [{"id": "u1", "full_name": "Rival Staff"}])  # …or this
    db.queue("file_send_batch_files", "select", [
        {"batch_id": "b1", "file_id": "f1"},
        {"batch_id": "b1", "file_id": "f2"},
    ])
    db.queue("project_files", "select", [
        _file_row("f1", "drawing"),
        _file_row("f2", "revision", sent_to_estimators_at=None),  # unsent → hidden
    ])
    _patch(monkeypatch, db)

    out = file_sends.build_log("p1", _estimator("e2"))
    assert out["viewer"] == "estimator"
    assert len(out["batches"]) == 1               # only the batch addressed to them
    b = out["batches"][0]
    assert b["kind"] == "initial"                 # reassign collapsed
    assert "recipients" not in b
    assert "sent_by_name" not in b
    assert b["counts"] == {"drawing": 2}          # from the snapshot
    avail = {f["file_id"]: f["available"] for f in b["files"]}
    assert avail == {"f1": True, "f2": False}     # unsent revision hidden but PRESENT

    blob = json.dumps(out)
    assert "rival@example.com" not in blob
    assert "Rival Bidder" not in blob
    assert "Rival Staff" not in blob


def test_build_log_counts_from_snapshot_survive_a_deleted_file(monkeypatch):
    # §8.2 #10: counts come from batch.summary, never the live join — deleting a
    # linked file leaves the headline counts unchanged.
    db = _FakeDB()
    db.queue("file_send_batches", "select", [{
        "id": "b1", "kind": "initial", "sent_at": "2026-07-01T00:00:00Z",
        "message": None, "reconstructed": False, "summary": {"drawing": 2}, "sent_by": "u1",
    }])
    db.queue("file_send_batch_files", "select", [
        {"batch_id": "b1", "file_id": "f1"},
        {"batch_id": "b1", "file_id": "f2"},   # f2 was deleted → no project_files row
    ])
    db.queue("project_files", "select", [_file_row("f1", "drawing")])
    db.queue("file_send_recipients", "select", [])
    db.queue("profiles", "select", [])
    _patch(monkeypatch, db)
    out = file_sends.build_log("p1", _writer())
    b = out["batches"][0]
    assert b["counts"] == {"drawing": 2}   # snapshot, not the 1 surviving row
    assert len(b["files"]) == 1            # the orphaned link is skipped


# ── build_handoff ──────────────────────────────────────────────────────────


def test_build_handoff_estimator_is_scoped_to_the_caller(monkeypatch):
    # §8.2 #13: assignees holds EXACTLY the caller's own row, email blanked;
    # staged is {}; my_access_expires_at is the caller's own; no co-assignee
    # identity anywhere.
    db = _FakeDB()
    db.queue("projects", "select", [{"due_from_estimator_at": "2026-07-10T00:00:00Z"}])
    db.queue("file_send_batches", "select", [
        {"id": "b1", "kind": "initial", "sent_at": "2026-07-01T00:00:00Z"},
    ])
    db.queue("file_send_recipients", "select", [{"batch_id": "b1"}])
    db.queue("file_send_batch_files", "select", [{"file_id": "f1"}])
    db.queue("project_files", "select", [
        {"id": "f1", "category": "drawing", "addendum_number": None, "addendum_issued_on": None},
    ])
    db.queue("estimator_assignments", "select", [
        {"id": "a1", "estimator_id": "e2", "due_at": "2026-07-08T00:00:00Z",
         "expires_at": "2026-07-20T00:00:00Z", "revoked_at": None,
         "sent_to_estimator_at": "2026-07-01T00:00:00Z",
         "profiles": {"full_name": "Me", "email": "me@x.com"}},
        {"id": "a0", "estimator_id": "other", "due_at": None, "expires_at": None,
         "revoked_at": None, "sent_to_estimator_at": "2026-07-01T00:00:00Z",
         "profiles": {"full_name": "Other Bidder", "email": "other@x.com"}},
    ])
    _patch(monkeypatch, db)
    monkeypatch.setattr(files_mod, "handoff_locked", lambda pid: True)

    out = file_sends.build_handoff("p1", _estimator("e2"))
    assert len(out["assignees"]) == 1
    a = out["assignees"][0]
    assert a["estimator_id"] == "e2"
    assert a["email"] is None                       # blanked for the estimator
    assert out["staged"] == {}
    assert out["my_access_expires_at"] == "2026-07-20T00:00:00Z"
    assert out["locked"] is True

    blob = json.dumps(out)
    assert "other@x.com" not in blob
    assert "Other Bidder" not in blob
