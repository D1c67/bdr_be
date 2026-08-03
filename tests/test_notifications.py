"""notify_role / notify_user: the estimator-broadcast guard and email hand-off.

The estimator is external and untrusted, scoped to assigned projects only, so an
unscoped role broadcast must never reach it (it would leak unassigned projects'
identities — and via the email mirror, to an external inbox). Estimators are
notified only through assignment-scoped notify_user calls elsewhere.
"""

from types import SimpleNamespace

from app.core.roles import Role
from app.services import notifications as n


class _Recorder:
    """Minimal Supabase fake that records table() access and insert payloads.

    An insert echoes its rows back with server-assigned ids, as PostgREST does —
    those returned rows (not the ones handed in) are what gets mirrored to email,
    because the id is how each send links its email_log row back to the bell row
    (migration 0091).
    """

    def __init__(self, profiles):
        self._profiles = profiles
        self._returning = profiles
        self.tables_touched = []
        self.inserted = []

    def table(self, name):
        self.tables_touched.append(name)
        return self

    def select(self, *a, **k):
        self._returning = self._profiles
        return self

    def eq(self, *a, **k):
        return self

    def insert(self, payload):
        self.inserted.append(payload)
        rows = payload if isinstance(payload, list) else [payload]
        self._returning = [{**r, "id": f"n{i}"} for i, r in enumerate(rows)]
        return self

    def execute(self):
        return SimpleNamespace(data=self._returning)


def _patch(monkeypatch, profiles):
    rec = _Recorder(profiles)
    queued = []
    monkeypatch.setattr(n, "get_supabase", lambda: rec)
    monkeypatch.setattr(n.notification_email, "queue", lambda rows: queued.append(rows))
    return rec, queued


def test_notify_role_refuses_estimator_broadcast(monkeypatch):
    rec, queued = _patch(monkeypatch, [{"id": "est1"}, {"id": "est2"}])
    n.notify_role(Role.ESTIMATOR, "p1", "stage_handoff", "Project advanced")
    # No profiles queried, no rows inserted, no email queued — fully short-circuited.
    assert rec.tables_touched == []
    assert rec.inserted == []
    assert queued == []


def test_notify_role_internal_inserts_and_queues_email(monkeypatch):
    rec, queued = _patch(monkeypatch, [{"id": "pe1"}, {"id": "pe2"}])
    n.notify_role(Role.ESTIMATING_ENGINEER_MATERIALS, "p1", "stage_handoff", "Project advanced")
    assert rec.inserted and len(rec.inserted[0]) == 2
    assert [r["user_id"] for r in rec.inserted[0]] == ["pe1", "pe2"]
    # The same rows are mirrored to email — as returned by the insert, so each
    # carries the id its mirror email links its email_log row back to.
    assert len(queued) == 1
    assert [r["user_id"] for r in queued[0]] == ["pe1", "pe2"]
    assert all(r.get("id") for r in queued[0])


def test_notify_user_inserts_and_queues_email(monkeypatch):
    rec, queued = _patch(monkeypatch, [])
    n.notify_user("est1", "p1", "assigned", "You were assigned to a project")
    assert rec.inserted == [{"user_id": "est1", "project_id": "p1",
                             "type": "assigned", "message": "You were assigned to a project",
                             "rfq_id": None}]
    assert queued == [[{**rec.inserted[0], "id": "n0"}]]


def test_notify_role_carries_rfq_id(monkeypatch):
    rec, queued = _patch(monkeypatch, [{"id": "pe1"}])
    n.notify_role(Role.ESTIMATING_ENGINEER_MATERIALS, "p1", "quote.received", "Quote in", rfq_id="r1")
    assert rec.inserted[0][0]["rfq_id"] == "r1"


class _UpdateRecorder:
    """Fake Supabase recording every update().filter…().execute() chain. Each
    execute() snapshots the filters applied since the last one, so per-type and
    per-prefix UPDATEs are captured as separate statements."""

    def __init__(self):
        self.statements = []          # one filter-dict per execute()
        self.update_payload = None
        self._current = {}

    def table(self, name):
        return self

    def update(self, payload):
        self.update_payload = payload
        return self

    def is_(self, col, val):
        self._current[("is_", col)] = val
        return self

    def eq(self, col, val):
        self._current[("eq", col)] = val
        return self

    def in_(self, col, vals):
        self._current[("in_", col)] = list(vals)
        return self

    def like(self, col, pattern):
        self._current[("like", col)] = pattern
        return self

    def execute(self):
        self.statements.append(self._current)
        self._current = {}
        return SimpleNamespace(data=[])


def test_dismiss_notifications_scoped_per_user(monkeypatch):
    rec = _UpdateRecorder()
    monkeypatch.setattr(n, "get_supabase", lambda: rec)
    n.dismiss_notifications(project_id="p1", types=["estimator_note"], user_id="u1")
    assert rec.update_payload == {"dismissed_at": "now()"}
    assert len(rec.statements) == 1
    stmt = rec.statements[0]
    assert stmt[("is_", "dismissed_at")] == "null"
    assert stmt[("eq", "project_id")] == "p1"
    assert stmt[("eq", "user_id")] == "u1"
    assert stmt[("in_", "type")] == ["estimator_note"]


def test_dismiss_notifications_mixes_exact_and_prefix(monkeypatch):
    rec = _UpdateRecorder()
    monkeypatch.setattr(n, "get_supabase", lambda: rec)
    n.dismiss_notifications(
        rfq_id="r1",
        types=["quote.received", "rfq.reply_received"],
        type_prefixes=["due.internal_bid.", "due.actual_bid."],
    )
    # One UPDATE for the exact types, one per prefix — all scoped to the rfq.
    assert len(rec.statements) == 3
    for stmt in rec.statements:
        assert stmt[("eq", "rfq_id")] == "r1"
        assert stmt[("is_", "dismissed_at")] == "null"
    assert rec.statements[0][("in_", "type")] == ["quote.received", "rfq.reply_received"]
    # Prefixes match via SQL LIKE '<prefix>%'.
    assert rec.statements[1][("like", "type")] == "due.internal_bid.%"
    assert rec.statements[2][("like", "type")] == "due.actual_bid.%"


def test_dismiss_notifications_requires_a_scope(monkeypatch):
    import pytest

    monkeypatch.setattr(n, "get_supabase", lambda: _UpdateRecorder())
    with pytest.raises(ValueError):
        n.dismiss_notifications(types=["stage_handoff"])
