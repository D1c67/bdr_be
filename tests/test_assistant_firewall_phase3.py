"""The phase 3 routes on app.main and the firewall
(docs/ASSISTANT_PHASE3_PLAN.md 3.3.6 and WI-4).

Invariants pinned here:

  deny      every /assistant route whose handler calls
            _refuse_assistant_principal is on DENY_LIST (or is a DELETE,
            which the firewall refuses outright), so the person's own
            Routines and Memory tab actions are never reachable by the
            assistant; the phase 2 human reads keep their exemption;
  open      what the assistant needs stays open: POST /assistant/schedules
            (the execute job), the reads, the recipe rows and every
            housekeeping tool route;
  tools     every /assistant/tools route answers a human with the bare 404;
  order     resolve_path_format maps the literal paths (draft, preview,
            by-approval, seen, memories/summary) to their own templates,
            never to the {schedule_id} or {memory_id} route;
  literals  the route constants the tool specs repeat name live routes.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.main
from app.assistant import access, firewall
from app.core import supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant_memory, assistant_routines

from tests.assistant_fakes import APPROVAL_ID, MEMORY, SCHEDULE, USER, FakeDB, memory_row, profile, schedule, settings_row

# Phase 2 human reads whose handlers refuse a bdra_ principal (403) and that
# the deny-list leaves to the handler: no tool allowlists them.
PHASE2_HANDLER_ONLY = {
    ("GET", "/assistant/files"): "the staging tray; the handler refuses the assistant",
    ("GET", "/assistant/tasks/{task_id}"): "the task card's poll; the handler refuses the assistant",
}


def _assistant_routes():
    for route in app.main.app.routes:
        if isinstance(route, APIRoute) and route.path_format.startswith("/assistant"):
            for method in sorted(route.methods or ()):
                if method not in ("HEAD", "OPTIONS"):
                    yield method, route


def test_every_route_that_refuses_the_assistant_is_deny_listed():
    missing = []
    checked = 0
    for method, route in _assistant_routes():
        source = inspect.getsource(route.endpoint)
        if "_refuse_assistant_principal(" not in source:
            continue
        checked += 1
        pair = (method, route.path_format)
        if pair in PHASE2_HANDLER_ONLY or method in firewall.REFUSED_METHODS:
            continue
        if pair not in firewall.DENY_LIST:
            missing.append(pair)
    assert checked >= 25
    assert missing == [], f"human-only assistant routes missing from DENY_LIST: {missing}"
    for pair in PHASE2_HANDLER_ONLY:
        assert pair not in firewall.DENY_LIST


def test_what_the_assistant_needs_stays_open():
    table = {(m, r.path_format) for m, r in _assistant_routes()}
    open_pairs = [
        ("POST", "/assistant/schedules"),
        ("GET", "/assistant/schedules"),
        ("GET", "/assistant/schedules/{schedule_id}"),
        ("GET", "/assistant/schedules/{schedule_id}/runs"),
        ("GET", "/assistant/schedules/{schedule_id}/preview"),
        ("GET", "/assistant/schedules/draft"),
        ("GET", "/assistant/schedules/preview"),
        ("GET", "/assistant/schedules/by-approval/{approval_id}"),
        ("GET", "/assistant/recipes/{recipe}/rows"),
        ("GET", "/assistant/tools/memory"),
        ("POST", "/assistant/tools/memory/save"),
        ("POST", "/assistant/tools/memory/{memory_id}/revise"),
        ("POST", "/assistant/tools/memory/{memory_id}/forget"),
        ("POST", "/assistant/tools/schedules/{schedule_id}/pause"),
        ("POST", "/assistant/tools/schedules/{schedule_id}/resume"),
        ("POST", "/assistant/tools/schedules/{schedule_id}/end"),
        ("POST", "/assistant/tools/schedules/{schedule_id}/run-now"),
    ]
    for pair in open_pairs:
        assert pair in table, pair
        assert not firewall.is_deny_listed(*pair), pair


def test_the_route_literals_the_tool_specs_repeat_are_live():
    table = {r.path_format for _, r in _assistant_routes()}
    literals = [
        assistant_routines.RECIPE_ROWS_ROUTE, assistant_routines.SCHEDULES_ROUTE,
        assistant_routines.SCHEDULE_DRAFT_ROUTE, assistant_routines.SCHEDULE_PREVIEW_ROUTE,
        assistant_routines.SCHEDULE_BY_APPROVAL_ROUTE, assistant_routines.SCHEDULE_ROUTE,
        assistant_routines.SCHEDULE_ID_PREVIEW_ROUTE, assistant_routines.TOOL_PAUSE_ROUTE,
        assistant_routines.TOOL_RESUME_ROUTE, assistant_routines.TOOL_END_ROUTE,
        assistant_routines.TOOL_RUN_NOW_ROUTE, assistant_memory.TOOL_LIST_ROUTE,
        assistant_memory.TOOL_SAVE_ROUTE, assistant_memory.TOOL_REVISE_ROUTE, assistant_memory.TOOL_FORGET_ROUTE,
    ]
    assert [lit for lit in literals if lit not in table] == []
    from app.assistant import recipes

    assert recipes.RECIPE_ROWS_ROUTE == ("GET", assistant_routines.RECIPE_ROWS_ROUTE)


def test_the_idempotency_header_matches_the_executor():
    from app.assistant import executor
    from app.routers import assistant as router_mod

    assert router_mod.ASSISTANT_IDEMPOTENCY_HEADER == executor.IDEMPOTENCY_HEADER


@pytest.mark.parametrize("path,expected", [
    ("/assistant/schedules/draft", "/assistant/schedules/draft"),
    ("/assistant/schedules/preview", "/assistant/schedules/preview"),
    (f"/assistant/schedules/by-approval/{APPROVAL_ID}", "/assistant/schedules/by-approval/{approval_id}"),
    ("/assistant/memories/summary", "/assistant/memories/summary"),
    (f"/assistant/schedules/{SCHEDULE}", "/assistant/schedules/{schedule_id}"),
    (f"/assistant/schedules/{SCHEDULE}/preview", "/assistant/schedules/{schedule_id}/preview"),
    (f"/assistant/memories/{MEMORY}", "/assistant/memories/{memory_id}"),
])
def test_literal_paths_resolve_to_their_own_templates(path, expected):
    scope = {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""}
    assert firewall.resolve_path_format(app.main.app, scope) == expected


def test_seen_resolves_to_its_own_template():
    scope = {"type": "http", "method": "POST", "path": "/assistant/schedules/seen", "headers": [],
             "query_string": b""}
    assert firewall.resolve_path_format(app.main.app, scope) == "/assistant/schedules/seen"
    v = firewall.check_scope(app.main.app, scope, {("POST", "/assistant/schedules/seen")})
    assert not v.allowed and v.reason == "deny_listed"


@pytest.fixture
def human(monkeypatch):
    db = FakeDB(profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
                assistant_schedules=[schedule()], assistant_memories=[memory_row()], assistant_tool_calls=[],
                assistant_messages=[], assistant_conversations=[])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    monkeypatch.setattr(get_settings(), "bidding_enabled", True)
    app.main.app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    yield SimpleNamespace(client=TestClient(app.main.app), db=db)
    app.main.app.dependency_overrides.pop(get_current_user, None)


def test_every_tool_route_answers_a_human_with_404(human):
    tool_routes = [(m, r) for m, r in _assistant_routes() if r.path_format.startswith("/assistant/tools/")]
    assert len(tool_routes) == 11  # phase 4 adds find, memory/search and reports
    for method, route in tool_routes:
        path = route.path_format.replace("{schedule_id}", SCHEDULE).replace("{memory_id}", MEMORY)
        body = {"kind": "preference", "title": "x"} if path.endswith("/save") else {}
        res = human.client.request(method, path, json=body if method != "GET" else None,
                                   headers={"X-Assistant-Idempotency-Key": "k"})
        assert res.status_code == 404, (method, path, res.text)
    assert human.db.tables["assistant_schedules"][0]["status"] == "active"
    assert human.db.tables["assistant_memories"][0]["status"] == "active"


def test_the_by_approval_read_and_the_create_answer_a_human_with_404(human):
    assert human.client.get(f"/assistant/schedules/by-approval/{APPROVAL_ID}").status_code == 404
    res = human.client.post("/assistant/schedules", json={"recipe": "due_today", "cadence": {"frequency": "daily",
                                                                                              "hour": 7}},
                            headers={"X-Assistant-Idempotency-Key": APPROVAL_ID})
    assert res.status_code == 404
    assert len(human.db.tables["assistant_schedules"]) == 1
