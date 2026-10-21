"""notify_roles (docs/AUTO_SEND.md 6.6): one bell insert for several roles.

The notification log groups bell rows into one event by (type, message,
created_at), so a multi-role broadcast must be ONE insert. The estimator is
dropped from the set (same reason notify_role refuses it), and the default is
bell only: auto send sends its own digest email.
"""

from types import SimpleNamespace

from app.core.roles import AUTO_SEND_NOTIFY_ROLES, Role
from app.services import notifications as n


class _Recorder:
    def __init__(self, profiles):
        self._profiles = profiles
        self._returning = profiles
        self.inserted = []
        self.role_filters = []
        self.eq_filters = []

    def table(self, name):
        return self

    def select(self, *a, **k):
        self._returning = self._profiles
        return self

    def in_(self, col, vals):
        self.role_filters.append((col, list(vals)))
        return self

    def eq(self, col, val):
        self.eq_filters.append((col, val))
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


def test_one_insert_for_every_role_and_bell_only_by_default(monkeypatch):
    rec, queued = _patch(monkeypatch, [{"id": "u1"}, {"id": "u2"}, {"id": "u3"}])
    n.notify_roles(AUTO_SEND_NOTIFY_ROLES, "p1", "auto_send.sent", "Sent",
                   metadata={"run_id": "r1"})
    assert rec.role_filters == [("role", sorted(r.value for r in AUTO_SEND_NOTIFY_ROLES))]
    assert ("is_active", True) in rec.eq_filters
    assert len(rec.inserted) == 1 and len(rec.inserted[0]) == 3
    row = rec.inserted[0][0]
    assert row["type"] == "auto_send.sent" and row["project_id"] == "p1"
    assert row["metadata"] == {"run_id": "r1"}
    assert queued == []


def test_mirror_email_queues_the_inserted_rows(monkeypatch):
    rec, queued = _patch(monkeypatch, [{"id": "u1"}])
    n.notify_roles([Role.ESTIMATING_ADMIN], "p1", "auto_send.failed", "Failed",
                   mirror_email=True)
    assert len(queued) == 1 and queued[0][0]["id"] == "n0"
    assert "metadata" not in rec.inserted[0][0]


def test_estimator_is_dropped(monkeypatch):
    rec, _ = _patch(monkeypatch, [{"id": "u1"}])
    n.notify_roles([Role.ESTIMATOR, Role.EXECUTIVE], "p1", "auto_send.sent", "Sent")
    assert rec.role_filters == [("role", ["executive"])]


def test_estimator_only_does_nothing(monkeypatch):
    rec, queued = _patch(monkeypatch, [{"id": "u1"}])
    n.notify_roles([Role.ESTIMATOR], "p1", "auto_send.sent", "Sent")
    assert rec.role_filters == [] and rec.inserted == [] and queued == []


def test_no_recipients_inserts_nothing(monkeypatch):
    rec, queued = _patch(monkeypatch, [])
    n.notify_roles(AUTO_SEND_NOTIFY_ROLES, "p1", "auto_send.sent", "Sent")
    assert rec.inserted == [] and queued == []
