"""The two routes added for the assistant's read tools (docs/ASSISTANT.md,
Skills and tools): GET /projects/search and GET /projects/due-checks.

  order      both literal paths sit above GET /projects/{project_id}, so
             "search" and "due-checks" are never read as a project id;
  search     name or number, paginated, PostgREST reserved characters
             stripped from the value before it enters the or=() filter,
             GCs and vendors by name, project rows redacted per role;
  due-checks bidding-only, internal roles only, the digest's membership
             and readiness rows with the project redacted per role.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.main
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import projects as projects_mod
from app.services import due_digest

from tests.assistant_fakes import NOW, PROJECT, PROJECT2, USER, FakeDB


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        projects=[
            {"id": PROJECT, "name": "Alpha Clinic", "number": "26.10.7267", "current_stage": "verify",
             "abandoned_at": None, "failed_to_bid_at": None, "internal_bid_at": NOW.isoformat(),
             "actual_bid_at": NOW.isoformat(), "created_at": NOW.isoformat(), "bid_outcomes": None},
            {"id": PROJECT2, "name": "Beta School", "number": "26.10.7268", "current_stage": "cp_only",
             "abandoned_at": None, "failed_to_bid_at": None, "internal_bid_at": None,
             "actual_bid_at": None, "created_at": NOW.isoformat(), "bid_outcomes": None},
        ],
        general_contractors=[{"id": "gc1", "name": "Alpha Builders"}, {"id": "gc2", "name": "Zeta Corp"}],
        vendors=[{"id": "v1", "name": "Alpha Supply"}],
    )
    monkeypatch.setattr(projects_mod, "get_supabase", lambda: db)
    who = {"role": Role.EXECUTIVE}

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=who["role"], is_active=True)

    app.main.app.dependency_overrides[get_current_user] = _user
    client = TestClient(app.main.app, raise_server_exceptions=True)
    yield SimpleNamespace(client=client, db=db, who=who)
    app.main.app.dependency_overrides.pop(get_current_user, None)


def test_literal_paths_are_registered_above_the_project_id_route():
    order = [r.path_format for r in app.main.app.routes if isinstance(r, APIRoute) and "GET" in (r.methods or ())]
    assert order.index("/projects/search") < order.index("/projects/{project_id}")
    assert order.index("/projects/due-checks") < order.index("/projects/{project_id}")


def test_search_strips_reserved_characters_and_redacts(api):
    res = api.client.get("/projects/search", params={"q": 'al"pha,(x)*%', "limit": 10})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["q"] == "alphax" and body["limit"] == 10 and body["offset"] == 0
    assert api.db.or_exprs[-1] == 'name.ilike."*alphax*",number.ilike."*alphax*"'
    assert [p["number"] for p in body["projects"]] == ["26.10.7267"]   # cp_only stays out
    assert body["projects"][0]["actual_bid_at"] == NOW.isoformat()      # executive sees it
    assert body["projects"][0]["status"] == "active" and "bid_outcomes" not in body["projects"][0]
    assert [g["name"] for g in body["gcs"]] == []                       # ilike on the cleaned term
    res = api.client.get("/projects/search", params={"q": "alpha"})
    body = res.json()
    assert [g["name"] for g in body["gcs"]] == ["Alpha Builders"]
    assert [v["name"] for v in body["vendors"]] == ["Alpha Supply"]
    api.who["role"] = Role.ESTIMATING_ENGINEER_LABOR
    body = api.client.get("/projects/search", params={"q": "alpha"}).json()
    assert body["projects"][0]["actual_bid_at"] is None


def test_search_validates_q_and_refuses_the_estimator(api):
    assert api.client.get("/projects/search").status_code == 422
    assert api.client.get("/projects/search", params={"q": ""}).status_code == 422
    assert api.client.get("/projects/search", params={"q": "x" * 121}).status_code == 422
    assert api.client.get("/projects/search", params={"q": "(),"}).status_code == 400
    assert api.client.get("/projects/search", params={"q": "a", "limit": 51}).status_code == 422
    api.who["role"] = Role.ESTIMATOR
    assert api.client.get("/projects/search", params={"q": "alpha"}).status_code == 403


def test_due_checks_returns_digest_rows_redacted(api, monkeypatch):
    rows = [{"project": {"id": PROJECT, "name": "Alpha Clinic", "number": "26.10.7267",
                         "internal_bid_at": NOW.isoformat(), "current_stage": "verify",
                         "actual_bid_at": NOW.isoformat()},
             "checks": {"has_quotes": False, "has_labor": True, "has_markups": True}, "severity": "red"}]
    seen = []
    monkeypatch.setattr(due_digest, "due_rows_for_today", lambda sb, now=None: seen.append(sb) or rows)
    res = api.client.get("/projects/due-checks")
    assert res.status_code == 200, res.text
    assert res.json()[0]["severity"] == "red" and res.json()[0]["project"]["actual_bid_at"] == NOW.isoformat()
    assert seen == [api.db]
    api.who["role"] = Role.ESTIMATING_ENGINEER_MATERIALS
    assert api.client.get("/projects/due-checks").json()[0]["project"]["actual_bid_at"] is None
    assert rows[0]["project"]["actual_bid_at"] == NOW.isoformat()   # the source row is not mutated
    api.who["role"] = Role.ESTIMATOR
    assert api.client.get("/projects/due-checks").status_code == 403


def test_due_checks_is_bidding_only(api, monkeypatch):
    monkeypatch.setenv("BIDDING_ENABLED", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        res = api.client.get("/projects/due-checks")
        assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    finally:
        monkeypatch.setenv("BIDDING_ENABLED", "true")
        get_settings.cache_clear()


def test_due_digest_helpers_compose_the_poller(monkeypatch):
    """poll_once's membership and readiness now come from the shared
    helpers; the helpers answer at any hour."""
    calls = []
    db = FakeDB(projects=[{"id": PROJECT, "name": "A", "number": "1", "internal_bid_at": NOW.isoformat(),
                           "current_stage": "verify", "abandoned_at": None}],
                rfqs=[], labor_reviews=[{"project_id": PROJECT, "labor_amount": 5}], markups=[])
    rows = due_digest.due_rows_for_today(db, NOW)
    assert len(rows) == 1 and rows[0]["checks"] == {"has_quotes": False, "has_labor": True, "has_markups": False}
    assert rows[0]["severity"] == "red"
    start, end = due_digest.day_bounds(NOW.astimezone(due_digest.DIGEST_TZ))
    assert (end - start).total_seconds() == 86400 or (end - start).total_seconds() in (82800, 90000)
    monkeypatch.setattr(due_digest, "due_today_projects", lambda sb, s, e: calls.append("members") or [])
    monkeypatch.setattr(due_digest, "in_send_window", lambda local, settings: True)
    monkeypatch.setattr(due_digest, "get_supabase", lambda: db)
    due_digest.poll_once()
    assert calls == ["members"]
