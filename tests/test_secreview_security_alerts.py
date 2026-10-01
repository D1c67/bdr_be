"""Security review: denied-access alerts are latched per estimator account.

Before the fix, every denial past the threshold called notify_role(IT_ADMIN),
so a looping estimator flooded the IT Admin bell and mirror emails. Now at
most one alert per account per ALERT_COOLDOWN_MIN; every denial is still
audited.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import security_alerts


class _Query:
    def __init__(self, store):
        self.store = store
        self.filters = []
        self._count = False
        self._limit = None
        self._desc = False
        self._insert = None

    def select(self, *_a, count=None):
        self._count = count == "exact"
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def gte(self, col, val):
        self.filters.append(lambda r: r.get(col) >= val)
        return self

    def order(self, _col, desc=False):
        self._desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, row):
        self._insert = row
        return self

    def execute(self):
        if self._insert is not None:
            row = dict(self._insert)
            self.store.clock += timedelta(seconds=1)
            row["created_at"] = self.store.now().isoformat()
            self.store.rows.append(row)
            return SimpleNamespace(data=[row], count=None)
        rows = [r for r in self.store.rows if all(f(r) for f in self.filters)]
        rows.sort(key=lambda r: r["created_at"], reverse=self._desc)
        count = len(rows)
        if self._limit is not None:
            rows = rows[: self._limit]
        return SimpleNamespace(data=rows, count=count if self._count else None)


class _FakeDB:
    def __init__(self):
        self.rows = []
        self.clock = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.clock

    def table(self, name):
        assert name == "audit_log"
        return _Query(self)


def _setup(monkeypatch):
    import app.services.notifications as notif

    db = _FakeDB()
    monkeypatch.setattr(security_alerts, "get_supabase", lambda: db)
    monkeypatch.setattr(notif, "get_supabase", lambda: db)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return db.clock

    monkeypatch.setattr(security_alerts, "datetime", _FrozenDT)
    alerts = []
    monkeypatch.setattr(
        security_alerts, "notify_role", lambda *a, **k: alerts.append(a)
    )
    return db, alerts


def test_looping_denials_raise_one_alert_per_cooldown(monkeypatch):
    db, alerts = _setup(monkeypatch)
    for _ in range(50):
        security_alerts.record_denied_access("est-1", "p1", "not_assigned")
    denied = [r for r in db.rows if r["action"] == "access.denied"]
    assert len(denied) == 50  # every denial still audited
    assert len(alerts) == 1
    assert "hit 5 denied-access attempts" in alerts[0][3]


def test_alert_fires_again_after_cooldown_with_suppressed_count(monkeypatch):
    db, alerts = _setup(monkeypatch)
    for _ in range(8):
        security_alerts.record_denied_access("est-1", "p1", "not_assigned")
    assert len(alerts) == 1
    db.clock += timedelta(minutes=security_alerts.ALERT_COOLDOWN_MIN + 1)
    for _ in range(5):
        security_alerts.record_denied_access("est-1", "p1", "not_assigned")
    assert len(alerts) == 2
    # 3 inside the cooldown (6th to 8th) + 5 after it.
    assert "8 denied attempts since the previous alert" in alerts[1][3]


def test_latch_is_per_account(monkeypatch):
    db, alerts = _setup(monkeypatch)
    for _ in range(6):
        security_alerts.record_denied_access("est-1", "p1", "not_assigned")
        security_alerts.record_denied_access("est-2", "p1", "not_assigned")
    assert len(alerts) == 2


def test_below_threshold_never_alerts(monkeypatch):
    db, alerts = _setup(monkeypatch)
    for _ in range(4):
        security_alerts.record_denied_access("est-1", "p1", "not_assigned")
    assert alerts == []
