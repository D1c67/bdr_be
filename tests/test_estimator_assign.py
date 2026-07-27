"""The estimator hand-off routes (app/routers/estimator.py): assign_estimator,
send_file_updates and the RETAINED send_to_estimator re-send.

These are the claim-before-send + one-email-per-recipient guarantees at the route
level. The heavy collaborators (file_sends, estimator_email) are replaced with
recording fakes so the tests pin the ORCHESTRATION — batch claimed before the
email, a failed initial email rolls back the row it inserted (but never a merely
reactivated one), reassign carries the prior-batch catch-up, the ride-along is a
real per-recipient send, and every send records its own batch — without touching
Graph, storage or a real database. The direct table reads the routes still do go
through a chainable Supabase fake that records ops and serves queued rows."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import estimator as est_mod


# ── chainable Supabase fake (records ops, serves queued rows) ──────────────


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

    def or_(self, expr):
        self.filters.append(("or", expr))
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


# ── recording collaborator fakes ───────────────────────────────────────────


class _FS:
    """Stand-in for the file_sends module."""

    def __init__(self):
        self.claimed: list[dict] = []
        self.abandoned: list[str] = []
        self.stamped: list[list] = []
        self.has_initial = False
        self.claim_error: Exception | None = None

    def has_initial_send(self, project_id):
        return self.has_initial

    def claim_batch(self, **kw):
        if self.claim_error:
            raise self.claim_error
        self.claimed.append(kw)
        return {"id": f"batch{len(self.claimed)}"}

    def prior_batches(self, project_id):
        return [{"kind": "initial", "sent_at": "2026-07-01", "summary": {"drawing": 1}}]

    def stamp_sent(self, file_ids):
        self.stamped.append(list(file_ids))

    def abandon_batch(self, batch_id):
        self.abandoned.append(batch_id)

    def attach_email_log(self, batch_id, email, email_log_id):
        pass


class _EE:
    """Stand-in for the estimator_email module."""

    def __init__(self):
        self.graph = True
        self.packages: list[dict] = []
        self.updates: list[dict] = []
        self.package_error: Exception | None = None
        self.updates_error: Exception | None = None

    def graph_configured(self):
        return self.graph

    def send_package(self, **kw):
        self.packages.append(kw)
        if self.package_error:
            raise self.package_error
        return {"id": f"log{len(self.packages)}"}

    def send_updates(self, **kw):
        self.updates.append(kw)
        if self.updates_error:
            raise self.updates_error
        return {"id": f"ulog{len(self.updates)}"}

    def updates_label(self, files):
        return "Changes/Revisions"


class _Env:
    def __init__(self, db, fs, ee):
        self.db, self.fs, self.ee = db, fs, ee
        self.pending: list[dict] = []
        self.active: list[dict] = []
        self.package_files: list[dict] = [{"id": "f1", "category": "drawing", "addendum_number": None}]


def _assign_env(monkeypatch, *, pending=None, active=None, package_files=None):
    db, fs, ee = _FakeDB(), _FS(), _EE()
    env = _Env(db, fs, ee)
    if pending is not None:
        env.pending = pending
    if active is not None:
        env.active = active
    if package_files is not None:
        env.package_files = package_files

    monkeypatch.setattr(est_mod, "project_has_drawing", lambda pid: True)
    monkeypatch.setattr(est_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(est_mod, "file_sends", fs)
    monkeypatch.setattr(est_mod, "estimator_email", ee)
    monkeypatch.setattr(est_mod, "_unsent_updates", lambda pid: env.pending)
    monkeypatch.setattr(est_mod, "_package_files", lambda pid: env.package_files)
    monkeypatch.setattr(est_mod, "_active_assignments", lambda pid: env.active)
    monkeypatch.setattr(est_mod, "notify_user", lambda *a, **k: None)
    monkeypatch.setattr(est_mod, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(est_mod, "audit", lambda *a, **k: None)
    monkeypatch.setattr(est_mod, "dismiss_notifications", lambda *a, **k: None)
    return env


def _writer():
    return CurrentUser(
        id="u1", email="w@g3.com", role=Role.ESTIMATING_ADMIN, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )


def _body(**kw):
    return est_mod.AssignIn(estimator_id="e1", **kw)


def _est_profile():
    return {"email": "e@x.com", "full_name": "E", "role": "estimator", "is_dev": False, "is_active": True}


def _proj():
    return {"id": "p1", "name": "Acme", "number": "42", "due_from_estimator_at": None}


def _queue_assign_insert_happy(env):
    """projects + profiles + (no active dupe) + (no reusable) → INSERT."""
    env.db.queue("projects", "select", _proj())
    env.db.queue("profiles", "select", [_est_profile()])
    env.db.queue("estimator_assignments", "select", [])   # active dupe: none
    env.db.queue("estimator_assignments", "select", [])   # reusable: none → INSERT
    env.db.queue("estimator_assignments", "insert", [{"id": "a1", "estimator_id": "e1"}])


# ── assign_estimator ───────────────────────────────────────────────────────


def test_assign_503_without_graph(monkeypatch):
    # §8.2 #14: no assignment row is inserted when Graph is unconfigured — this is
    # what removes the old "assigned but nothing sent → drawings frozen" state.
    env = _assign_env(monkeypatch)
    env.ee.graph = False
    with pytest.raises(HTTPException) as exc:
        est_mod.assign_estimator("p1", _body(), _writer())
    assert exc.value.status_code == 503
    assert env.fs.claimed == []
    assert env.db.ops("estimator_assignments", "insert") == []


def test_assign_duplicate_active_is_409(monkeypatch):
    # §8.2 #15: an already-active assignee is refused with no second row/recipient.
    env = _assign_env(monkeypatch)
    env.db.queue("projects", "select", _proj())
    env.db.queue("profiles", "select", [_est_profile()])
    env.db.queue("estimator_assignments", "select", [{"id": "a1"}])  # active dupe present
    with pytest.raises(HTTPException) as exc:
        est_mod.assign_estimator("p1", _body(), _writer())
    assert exc.value.status_code == 409
    assert env.fs.claimed == []
    assert env.db.ops("estimator_assignments", "insert") == []


def test_assign_email_failure_rolls_back_inserted_row_and_502(monkeypatch):
    # §8.2 #17 (the claim-before-send core): the batch is claimed BEFORE the email;
    # a failed initial email abandons the batch, deletes the row THIS request
    # inserted, and 502s for a clean retry. Nothing is left marked sent.
    env = _assign_env(monkeypatch)
    env.ee.package_error = RuntimeError("smtp down")
    _queue_assign_insert_happy(env)
    with pytest.raises(HTTPException) as exc:
        est_mod.assign_estimator("p1", _body(), _writer())
    assert exc.value.status_code == 502
    assert len(env.fs.claimed) == 1            # claimed before the email
    assert env.fs.abandoned == ["batch1"]      # abandoned on failure
    dels = env.db.ops("estimator_assignments", "delete")
    assert dels and ("eq", "id", "a1") in dels[0].filters


def test_assign_reactivates_and_does_not_delete_on_failure(monkeypatch):
    # §8.2 #16/#17-tail: an expired-but-unrevoked row is UPDATEd in place (one row,
    # reactivated) — no second INSERT — and a merely-reactivated row is NOT deleted
    # when the email then fails (recover it via Re-send, don't strand the project).
    env = _assign_env(monkeypatch)
    env.ee.package_error = RuntimeError("smtp down")
    env.db.queue("projects", "select", _proj())
    env.db.queue("profiles", "select", [_est_profile()])
    env.db.queue("estimator_assignments", "select", [])              # active dupe: none
    env.db.queue("estimator_assignments", "select", [{"id": "a0"}])   # reusable → UPDATE
    env.db.queue("estimator_assignments", "update", [{"id": "a0", "estimator_id": "e1"}])
    with pytest.raises(HTTPException) as exc:
        est_mod.assign_estimator("p1", _body(), _writer())
    assert exc.value.status_code == 502
    assert env.db.ops("estimator_assignments", "insert") == []       # reactivated, not inserted
    assert env.db.ops("estimator_assignments", "update")             # an update happened
    assert env.db.ops("estimator_assignments", "delete") == []       # NOT deleted


def test_assign_initial_kind_when_no_prior_send(monkeypatch):
    # §8.2 #18: kind='initial' when no initial batch exists; the package email
    # carries no catch-up history.
    env = _assign_env(monkeypatch)
    env.fs.has_initial = False
    _queue_assign_insert_happy(env)
    out = est_mod.assign_estimator("p1", _body(), _writer())
    assert out["kind"] == "initial"
    assert env.ee.packages[0]["kind"] == "initial"
    assert env.ee.packages[0]["prior"] is None


def test_assign_reassign_carries_prior_catchup(monkeypatch):
    # §8.2 #18 + the re-assign catch-up: kind='reassign' when an initial batch
    # exists, and the new estimator's package carries the prior-batch "Update
    # history" (file_sends.prior_batches) so every earlier change is included.
    env = _assign_env(monkeypatch)
    env.fs.has_initial = True
    _queue_assign_insert_happy(env)
    out = est_mod.assign_estimator("p1", _body(), _writer())
    assert out["kind"] == "reassign"
    assert env.ee.packages[0]["kind"] == "reassign"
    assert env.ee.packages[0]["prior"] == [
        {"kind": "initial", "sent_at": "2026-07-01", "summary": {"drawing": 1}}
    ]


def test_assign_turnaround_stamp_is_null_guarded(monkeypatch):
    # §8.2 #19: re-assigning an already-sent estimator must not reset
    # sent_to_estimator_at (protects analytics turnaround). The post-send stamp is
    # NULL-guarded.
    env = _assign_env(monkeypatch)
    env.fs.has_initial = False
    _queue_assign_insert_happy(env)
    est_mod.assign_estimator("p1", _body(), _writer())
    stamp = env.db.ops("estimator_assignments", "update")[0]
    assert stamp.payload == {"sent_to_estimator_at": "now()"}
    assert ("is", "sent_to_estimator_at", "null") in stamp.filters


def test_assign_ride_along_emails_other_assignees(monkeypatch):
    # §8.2 #20 (B2 fix): with pending drafts AND other active assignees, a SECOND
    # kind='revision' batch is claimed for those assignees and each is actually
    # EMAILED (one message per recipient) — not merely belled — so the files are
    # reachable from their own log.
    env = _assign_env(
        monkeypatch,
        pending=[{"id": "u1", "category": "revision", "note": "x", "addendum_number": None}],
        active=[{"estimator_id": "e9", "profiles": {"email": "e9@x.com", "full_name": "Nine"}}],
    )
    env.fs.has_initial = False
    _queue_assign_insert_happy(env)
    est_mod.assign_estimator("p1", _body(), _writer())
    assert [c["kind"] for c in env.fs.claimed] == ["initial", "revision"]
    ride = env.fs.claimed[1]
    assert ride["file_ids"] == ["u1"]
    assert ride["recipients"][0]["email"] == "e9@x.com"
    assert len(env.ee.updates) == 1
    assert env.ee.updates[0]["to"] == ["e9@x.com"]     # one email per recipient, never to=[all]


def test_assign_ride_along_failure_is_swallowed(monkeypatch):
    # §8.2 #21: the primary package is delivered, so a ride-along hiccup must not
    # 502 the assign.
    env = _assign_env(
        monkeypatch,
        pending=[{"id": "u1", "category": "revision", "note": "x", "addendum_number": None}],
        active=[{"estimator_id": "e9", "profiles": {"email": "e9@x.com", "full_name": "Nine"}}],
    )
    env.fs.has_initial = False
    env.ee.updates_error = RuntimeError("ride email failed")
    _queue_assign_insert_happy(env)
    out = est_mod.assign_estimator("p1", _body(), _writer())
    assert out["package_sent"] is True


def test_assign_revokes_apply_after_successful_send(monkeypatch):
    # §8.2 #22: revocations apply only AFTER a successful send, so a failed send
    # never strands a project with no assignee.
    env = _assign_env(monkeypatch)
    env.fs.has_initial = False
    _queue_assign_insert_happy(env)
    # First update = the post-send turnaround stamp; second = the revoke.
    env.db.queue("estimator_assignments", "update", [], [{"estimator_id": "old"}])
    est_mod.assign_estimator("p1", _body(revoke_assignment_ids=["old-a"]), _writer())
    revokes = [
        u for u in env.db.ops("estimator_assignments", "update")
        if u.payload.get("revoked_at") == "now()"
    ]
    assert revokes and ("eq", "id", "old-a") in revokes[0].filters


# ── send_file_updates ──────────────────────────────────────────────────────


def test_send_updates_409_without_initial_send(monkeypatch):
    # §8.2 #23: the Revisions batch only exists relative to an initial hand-off.
    env = _assign_env(
        monkeypatch,
        active=[{"estimator_id": "e9", "profiles": {"email": "e9@x.com", "full_name": "Nine"}}],
    )
    env.fs.has_initial = False
    env.db.queue("projects", "select", _proj())
    with pytest.raises(HTTPException) as exc:
        est_mod.send_file_updates("p1", None, _writer())
    assert exc.value.status_code == 409
    assert env.fs.claimed == []


def test_send_updates_rejects_a_foreign_file_id(monkeypatch):
    # §8.2 #24: send EXACTLY the staged subset — a requested id that isn't an
    # unsent update of this project is a 400 (also the double-click guard).
    env = _assign_env(
        monkeypatch,
        active=[{"estimator_id": "e9", "profiles": {"email": "e9@x.com", "full_name": "Nine"}}],
        pending=[{"id": "u1", "category": "revision", "note": "x", "addendum_number": None}],
    )
    env.fs.has_initial = True
    env.db.queue("projects", "select", _proj())
    body = est_mod.UpdatesIn(file_ids=["u1", "nope"])
    with pytest.raises(HTTPException) as exc:
        est_mod.send_file_updates("p1", body, _writer())
    assert exc.value.status_code == 400
    assert env.fs.claimed == []


def test_send_updates_one_email_per_recipient(monkeypatch):
    # §8.2 #25: one 'revision' batch, one email per active assignee (never to=[all],
    # which would leak every estimator's address to the others).
    env = _assign_env(
        monkeypatch,
        active=[
            {"estimator_id": "e9", "profiles": {"email": "e9@x.com", "full_name": "Nine"}},
            {"estimator_id": "e8", "profiles": {"email": "e8@x.com", "full_name": "Eight"}},
        ],
        pending=[{"id": "u1", "category": "revision", "note": "x", "addendum_number": None}],
    )
    env.fs.has_initial = True
    env.db.queue("projects", "select", _proj())
    out = est_mod.send_file_updates("p1", None, _writer())
    assert len(env.fs.claimed) == 1
    assert env.fs.claimed[0]["kind"] == "revision"
    assert env.fs.stamped == [["u1"]]
    tos = [u["to"] for u in env.ee.updates]
    assert ["e9@x.com"] in tos and ["e8@x.com"] in tos
    assert len(env.ee.updates) == 2
    assert out["batch_id"] == "batch1"


# ── send_to_estimator (the RETAINED re-send route) ─────────────────────────


def test_send_to_estimator_503_without_graph(monkeypatch):
    env = _assign_env(monkeypatch)
    env.ee.graph = False
    with pytest.raises(HTTPException) as exc:
        est_mod.send_to_estimator("p1", _writer())
    assert exc.value.status_code == 503
    assert env.fs.claimed == []


def test_send_to_estimator_records_its_own_batch(monkeypatch):
    # Owner decision #6: send-to-estimator is KEPT and records a batch of its own
    # (visible in the Plans & Specs Log), one email per recipient, with a
    # NULL-guarded turnaround stamp.
    env = _assign_env(
        monkeypatch,
        active=[{"estimator_id": "e9", "profiles": {"email": "e9@x.com", "full_name": "Nine"}}],
    )
    env.fs.has_initial = True   # initial already exists → this re-send is a 'reassign'
    env.db.queue("projects", "select", _proj())
    out = est_mod.send_to_estimator("p1", _writer())
    assert len(env.fs.claimed) == 1
    assert env.fs.claimed[0]["kind"] == "reassign"
    assert env.ee.packages[0]["to"] == ["e9@x.com"]
    assert out["batch_id"] == "batch1"
    stamp = env.db.ops("estimator_assignments", "update")[0]
    assert ("is", "sent_to_estimator_at", "null") in stamp.filters
