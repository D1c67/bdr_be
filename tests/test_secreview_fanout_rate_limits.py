"""Security review, group fanout-rate-limits: rate limits on the aggregate
analytics reads and the email fan-out routes, and the accountant no-op on the
project-open beacon. Mock-based: no database, no email."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import ratelimit
from app.core.deps import CurrentUser
from app.core.error_codes import RateLimitScope
from app.core.roles import Role
from app.models.schemas import ProjectOpenIn, ProjectUpdate
from app.routers import analytics as analytics_mod
from app.routers import external_submission as xs_mod
from app.routers import projects as proj_mod
from app.services import bid_date_change_email as bdce

OLD = "2026-08-01T19:00:00+00:00"


@pytest.fixture(autouse=True)
def _clear_buckets():
    ratelimit._buckets.clear()
    yield
    ratelimit._buckets.clear()


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(
        id=uid, email="e@g3.com", role=role, is_active=True, aal="aal2", mfa_enrolled=True,
    )


def _route_deps(router, path, method):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return [d.call for d in route.dependant.dependencies]
    raise AssertionError(f"no route {method} {path}")


# ── finding 13: analytics aggregate reads ──────────────────────────────────


@pytest.mark.parametrize(
    "path", ["/analytics/labor-engineer", "/analytics/calling-in", "/analytics/projects/{project_id}"]
)
def test_aggregate_analytics_reads_carry_the_report_limit(path):
    assert ratelimit.report_rate_limit in _route_deps(analytics_mod.router, path, "GET")


# ── finding 25: external-submission submit and undo ────────────────────────


@pytest.mark.parametrize(
    "path",
    ["/projects/{project_id}/external-submission", "/projects/{project_id}/external-submission/undo"],
)
def test_submit_and_undo_carry_the_outbound_email_limit(path):
    assert ratelimit.outbound_email_rate_limit in _route_deps(xs_mod.router, path, "POST")


# ── finding 28: internal bid date change fan-out ───────────────────────────


class _Q:
    def __init__(self, db, table):
        self.db, self.table_name, self.op = db, table, None

    def select(self, *a, **k):
        self.op = self.op or "select"
        return self

    def update(self, payload):
        self.op = "update"
        self.db.updates.append(payload)
        return self

    def insert(self, payload):
        self.op = "insert"
        self.db.inserts.append((self.table_name, payload))
        return self

    def eq(self, *a):
        return self

    def limit(self, *a):
        return self

    def execute(self):
        if self.table_name == "projects" and self.op == "select":
            return SimpleNamespace(data=[{"id": "p1", "internal_bid_at": self.db.stored}])
        if self.op == "update":
            self.db.stored = self.db.updates[-1].get("internal_bid_at", self.db.stored)
            return SimpleNamespace(data=[{"id": "p1", "internal_bid_at": self.db.stored}])
        return SimpleNamespace(data=[])


class _DB:
    def __init__(self):
        self.stored, self.updates, self.inserts = OLD, [], []

    def table(self, name):
        return _Q(self, name)


def _wire(monkeypatch, db):
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(proj_mod, "audit", lambda *a, **k: None)
    monkeypatch.setattr(proj_mod, "_present", lambda row, role, *a: row)
    monkeypatch.setattr(proj_mod, "_settle_rfp_intake", lambda *a: False)
    queued = []
    monkeypatch.setattr(bdce, "queue_internal_bid_date_change", lambda *a: queued.append(a))
    monkeypatch.setattr(
        ratelimit, "get_settings",
        lambda: SimpleNamespace(rate_limit_enabled=True, outbound_email_rate_limit_per_hour=3),
    )
    return queued


def test_bid_date_flapping_is_capped_by_the_outbound_email_budget(monkeypatch):
    db = _DB()
    queued = _wire(monkeypatch, db)
    dates = ["2026-08-02T19:00:00+00:00", OLD, "2026-08-02T19:00:00+00:00"]
    for d in dates:
        proj_mod.update_project("p1", ProjectUpdate(internal_bid_at=d), _user())
    assert len(queued) == 3

    with pytest.raises(HTTPException) as exc:
        proj_mod.update_project("p1", ProjectUpdate(internal_bid_at=OLD), _user())
    assert exc.value.status_code == 429
    assert exc.value.headers["X-RateLimit-Scope"] == RateLimitScope.OUTBOUND_EMAIL
    # Refused before the write: nothing stored, nothing emailed.
    assert len(db.updates) == 3
    assert len(queued) == 3


def test_same_date_and_other_fields_never_spend_the_budget(monkeypatch):
    db = _DB()
    queued = _wire(monkeypatch, db)
    for _ in range(10):
        proj_mod.update_project("p1", ProjectUpdate(internal_bid_at=OLD), _user())
        proj_mod.update_project("p1", ProjectUpdate(notes="hi"), _user())
    assert queued == []
    assert ratelimit._buckets == {}


# ── finding 41: project-open beacon ────────────────────────────────────────


def test_accountant_open_beacon_is_a_no_op(monkeypatch):
    db = _DB()
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)
    looked_up = []
    monkeypatch.setattr(proj_mod, "_project_or_404", lambda pid: looked_up.append(pid))

    proj_mod.log_project_open("p1", ProjectOpenIn(kind="project"), _user(Role.ACCOUNTANT))
    assert db.inserts == []
    assert looked_up == []

    proj_mod.log_project_open(
        "p1", ProjectOpenIn(kind="project"), _user(Role.ESTIMATING_ENGINEER_LABOR)
    )
    assert db.inserts == [
        ("project_open_events", {"project_id": "p1", "user_id": "u1", "kind": "project"})
    ]
