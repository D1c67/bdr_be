"""services/pm_aggregates and the two cross-project PM routes
(`GET /pm/rfis/open`, `GET /pm/billing/summary`; docs/ASSISTANT_PHASE4_PLAN.md
3.13). An in-memory Supabase stand-in serves the service tests; the route
tests run the real app with the user dependency overridden."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import pm as pm_router
from app.services import pm_aggregates as agg
from app.services import pm_financials as fin

TODAY = date(2026, 10, 24)


class _Not:
    def __init__(self, q):
        self.q = q

    def is_(self, col, val):
        self.q.filters.append(("notnull", col, None))
        return self.q


class _Q:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters = []
        self._order = None
        self._range = None
        self.not_ = _Not(self)

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals)))
        return self

    def is_(self, col, val):
        self.filters.append(("null", col, None))
        return self

    def order(self, col, **_k):
        self._order = col
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def execute(self):
        self.db.queries.append(self.table)
        rows = [r for r in self.db.tables.get(self.table, []) if self._ok(r)]
        if self._order:
            rows.sort(key=lambda r: str(r.get(self._order)))
        if self._range:
            rows = rows[self._range[0] : self._range[1] + 1]
        return SimpleNamespace(data=[dict(r) for r in rows])

    def _ok(self, r):
        for kind, col, val in self.filters:
            if kind == "eq" and r.get(col) != val:
                return False
            if kind == "in" and r.get(col) not in val:
                return False
            if kind == "null" and r.get(col) is not None:
                return False
            if kind == "notnull" and r.get(col) is None:
                return False
        return True


class FakeSB:
    def __init__(self, tables):
        self.tables = tables
        self.queries: list[str] = []

    def table(self, name):
        return _Q(self, name)


def _proj(pid, number, name, *, pm_stage="active", completed=None):
    return {"id": pid, "number": number, "name": name, "pm_stage": pm_stage, "pm_completed_at": completed}


def _rfi(rid, pid, **kw):
    base = {
        "id": rid, "project_id": pid, "rfi_number": 1, "external_number": None, "subject": "S",
        "status": "open", "ball_in_court": None, "due_at": None, "external_due_date": None,
        "sent_at": None, "initiated_at": None, "created_at": "2026-10-20T12:00:00+00:00", "source": "internal",
        "question": "SECRET QUESTION", "answer": "SECRET ANSWER",
    }
    base.update(kw)
    return base


# -- open_rfis ----------------------------------------------------------------


def test_open_rfis_groups_by_project_and_skips_completed_and_bid_only_projects():
    sb = FakeSB({
        "projects": [
            _proj("p1", "26.1", "Alpha"),
            _proj("p2", "26.2", "Beta"),
            _proj("p3", "26.3", "Done", completed="2026-10-01T00:00:00+00:00"),
            _proj("p4", "26.4", "Bid only", pm_stage=None),
        ],
        "rfis": [
            _rfi("r1", "p1", rfi_number=2, due_at="2026-10-30"),
            _rfi("r2", "p1", rfi_number=1, due_at="2026-10-20", sent_at="2026-10-10"),
            _rfi("r3", "p2", rfi_number=None, external_number="55", source="procore",
                 external_due_date="2026-11-01", ball_in_court="GC"),
            _rfi("r4", "p3"),
            _rfi("r5", "p4"),
            _rfi("r6", "p1", rfi_number=3, status="closed"),
        ],
    })
    out = agg.open_rfis(sb, today=TODAY)
    assert out["truncated"] is False
    assert [g["project"]["number"] for g in out["projects"]] == ["26.1", "26.2"]
    p1 = out["projects"][0]["rfis"]
    assert [r["id"] for r in p1] == ["r2", "r1"]  # soonest due first
    assert p1[0]["overdue"] is True and p1[0]["age_days"] == 14
    assert p1[1]["overdue"] is False
    p2 = out["projects"][1]["rfis"][0]
    assert p2["number"] == "55" and p2["source"] == "procore" and p2["read_only"] is True
    assert p2["due"] == "2026-11-01" and p2["ball_in_court"] == "GC"
    assert set(p2) == {
        "id", "number", "subject", "status", "ball_in_court", "due", "sent_at",
        "age_days", "overdue", "source", "read_only",
    }


def test_open_rfis_never_carries_question_or_answer():
    sb = FakeSB({"projects": [_proj("p1", "26.1", "A")], "rfis": [_rfi("r1", "p1")]})
    assert "SECRET" not in repr(agg.open_rfis(sb, today=TODAY))


def test_open_rfis_clips_subject_and_truncates_at_limit():
    rfis = [_rfi(f"r{i:03}", "p1", rfi_number=i, subject="x" * 500) for i in range(1, 6)]
    rfis += [_rfi("z1", "p2")]
    sb = FakeSB({"projects": [_proj("p1", "26.1", "A"), _proj("p2", "26.2", "B")], "rfis": rfis})
    out = agg.open_rfis(sb, limit=3, today=TODAY)
    assert out["truncated"] is True
    assert sum(len(g["rfis"]) for g in out["projects"]) == 3
    assert len(out["projects"][0]["rfis"][0]["subject"]) == 200
    exact = agg.open_rfis(sb, limit=6, today=TODAY)
    assert exact["truncated"] is False and sum(len(g["rfis"]) for g in exact["projects"]) == 6
    over = agg.open_rfis(sb, limit=5, today=TODAY)
    assert over["truncated"] is True and [g["project"]["number"] for g in over["projects"]] == ["26.1"]


def test_open_rfis_pages_past_the_postgrest_cap():
    rfis = [_rfi(f"r{i:05}", "p1", rfi_number=i) for i in range(1, 1501)]
    sb = FakeSB({"projects": [_proj("p1", "26.1", "A")], "rfis": rfis})
    agg.open_rfis(sb, limit=200, today=TODAY)
    assert sb.queries.count("rfis") == 2  # two pages


def test_open_rfis_with_no_projects_makes_no_rfi_query():
    sb = FakeSB({"projects": [], "rfis": []})
    assert agg.open_rfis(sb, today=TODAY) == {"projects": [], "truncated": False}
    assert "rfis" not in sb.queries


# -- billing_summary ----------------------------------------------------------


def _app(aid, pid, n, status="approved", pct="10"):
    return {"id": aid, "project_id": pid, "app_number": n, "status": status, "retainage_percent": pct}


def _line(lid, aid, prev, this, stored="0"):
    return {"id": lid, "pay_app_id": aid, "previous_completed": prev, "this_period": this, "stored_materials": stored}


def test_billing_summary_matches_the_per_project_financials_math():
    sb = FakeSB({
        "projects": [_proj("p1", "26.1", "Alpha"), _proj("p2", "26.2", "Beta"), _proj("p3", "26.3", "Gone", completed="x")],
        "pm_details": [
            {"project_id": "p1", "original_contract_value": "100000.00"},
            {"project_id": "p2", "original_contract_value": None},
        ],
        "change_orders": [
            {"id": "c1", "project_id": "p1", "amount": "5000", "status": "approved"},
            {"id": "c2", "project_id": "p1", "amount": "900", "status": "draft"},
            {"id": "c3", "project_id": "p1", "amount": "700", "status": "submitted"},
            {"id": "c4", "project_id": "p1", "amount": "50", "status": "rejected"},
        ],
        "pay_applications": [
            _app("a1", "p1", 1), _app("a2", "p1", 2), _app("a3", "p1", 3, status="rejected"),
        ],
        "pay_app_lines": [
            _line("l1", "a1", "0", "20000"),
            _line("l2", "a2", "0", "10000"),
            _line("l3", "a2", "20000", "0"),
            _line("l4", "a3", "30000", "99999"),
        ],
    })
    out = agg.billing_summary(sb)
    assert [r["project"]["number"] for r in out["projects"]] == ["26.1", "26.2"]
    p1, p2 = out["projects"]
    # latest non-rejected app (2): completed 20000 + 10000 + 0 = 30000, retainage 3000
    assert p1["billed_to_date"] == "30000.00" and p1["retainage_held"] == "3000.00"
    assert p1["original_contract_value"] == "100000.00"
    assert p1["approved_change_total"] == "5000.00"
    assert p1["current_contract_value"] == "105000.00"
    assert p1["balance_to_finish"] == "75000.00"
    assert p1["open_change_order_count"] == 2 and p1["pay_app_count"] == 3
    assert p2["original_contract_value"] is None and p2["current_contract_value"] is None
    assert p2["balance_to_finish"] is None and p2["billed_to_date"] == "0.00" and p2["pay_app_count"] == 0
    assert "sov_total" not in p1
    # the same function as the per-project route
    ref = fin.financials_summary(
        "100000.00", Decimal("5000"), Decimal(0), Decimal("30000.00"), Decimal("3000.00"), 2, 3
    )
    for key in ("current_contract_value", "balance_to_finish", "approved_change_total"):
        assert p1[key] == ref[key]


def test_billing_summary_batches_queries_not_one_per_project():
    projects = [_proj(f"p{i}", f"26.{i}", f"P{i}") for i in range(30)]
    sb = FakeSB({"projects": projects, "pm_details": [], "change_orders": [], "pay_applications": []})
    agg.billing_summary(sb)
    assert sb.queries.count("pm_details") == 1 and sb.queries.count("change_orders") == 1
    assert sb.queries.count("pay_applications") == 1
    assert "pay_app_lines" not in sb.queries  # no apps, no line query


# -- routes -------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    from app.main import app

    state = SimpleNamespace(role=Role.EXECUTIVE)
    sb = FakeSB({
        "projects": [_proj("p1", "26.1", "Alpha")],
        "rfis": [_rfi("r1", "p1")],
        "pm_details": [{"project_id": "p1", "original_contract_value": "10.00"}],
        "change_orders": [], "pay_applications": [],
    })
    monkeypatch.setattr(pm_router, "get_supabase", lambda: sb)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id="u1", email="u@g3.com", role=state.role, is_active=True
    )
    c = TestClient(app, raise_server_exceptions=True)
    c.state = state
    yield c
    app.dependency_overrides.pop(get_current_user, None)


def _first_full_match(app, method, path):
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "headers": []}
    for route in app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


def test_new_routes_resolve_to_the_aggregate_handlers_not_a_project_pattern():
    from app.main import app

    rfis = _first_full_match(app, "GET", "/pm/rfis/open")
    billing = _first_full_match(app, "GET", "/pm/billing/summary")
    assert rfis is not None and rfis.endpoint is pm_router.open_rfis_across_projects
    assert billing is not None and billing.endpoint is pm_router.billing_summary_across_projects


def test_handlers_are_plain_def_not_async():
    import inspect

    assert not inspect.iscoroutinefunction(pm_router.open_rfis_across_projects)
    assert not inspect.iscoroutinefunction(pm_router.billing_summary_across_projects)


@pytest.mark.parametrize("path", ["/pm/rfis/open", "/pm/billing/summary"])
def test_pm_read_roles_including_accountant_are_allowed(client, path):
    for role in (Role.EXECUTIVE, Role.IT_ADMIN, Role.ACCOUNTANT, Role.ESTIMATING_ADMIN):
        client.state.role = role
        assert client.get(path).status_code == 200, (role, path)


@pytest.mark.parametrize("path", ["/pm/rfis/open", "/pm/billing/summary"])
def test_estimator_is_refused(client, path):
    client.state.role = Role.ESTIMATOR
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", ["/pm/rfis/open", "/pm/billing/summary"])
def test_pm_module_off_is_a_404(client, monkeypatch, path):
    from app.core import features

    monkeypatch.setattr(features, "is_enabled", lambda sub_app: False)
    from app.core import deps

    monkeypatch.setattr(deps, "is_enabled", lambda sub_app: False)
    assert client.get(path).status_code == 404


def test_rfis_limit_is_validated(client):
    assert client.get("/pm/rfis/open?limit=0").status_code == 422
    assert client.get("/pm/rfis/open?limit=201").status_code == 422
    assert client.get("/pm/rfis/open?limit=5").status_code == 200


def test_route_bodies(client):
    body = client.get("/pm/rfis/open").json()
    assert body["projects"][0]["project"] == {"id": "p1", "number": "26.1", "name": "Alpha"}
    bill = client.get("/pm/billing/summary").json()
    assert bill["projects"][0]["current_contract_value"] == "10.00"
