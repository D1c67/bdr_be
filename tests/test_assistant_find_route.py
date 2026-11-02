"""GET /assistant/tools/find (app/routers/assistant.py) against
docs/ASSISTANT_PHASE4_PLAN.md 3.16.2.

Invariants pinned here:

  who       the assistant's own route: a human gets the bare 404, and a
            switched-off assistant too;
  toolset   the resolver sees exactly the caller's current toolset (live
            role, sub-app flags, disabled tools, the conversation mode read
            from the run's own ledger rows), and the answer names only the
            deferred tools in it, best first, at most `limit`;
  shape     {tools: [{name, description}], hint}; no em dash anywhere.

find.resolve is WI-10's; it is faked here (the route filters its answer
again, so a resolver that returned a core tool still never leaks one).
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, find, registry
from app.core import supabase_client
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, WRITER_ROLES, Role
from app.routers import assistant as router_mod

from tests.assistant_fakes import CONV, RUN, USER, FakeDB, conversation, delegation, profile, settings_row

EM_DASH = chr(0x2014)


def _deferred(base: registry.ToolSpec, name: str, **over) -> registry.ToolSpec:
    return dataclasses.replace(base, name=name, deferred=True, keywords=("plans",),
                               description=f"Use {name} for plan questions.", **over)


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[delegation()], assistant_conversations=[conversation()],
        assistant_tool_calls=[{"id": "tc-1", "user_id": USER, "run_id": RUN, "conversation_id": CONV,
                               "phase": "begun", "tool_name": "find_tools", "idempotency_key": "k"}],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    flags = {"bidding": True, "pm": True, "certified_payroll": True, "ai_estimating": True}
    monkeypatch.setattr(router_mod, "enabled_map", lambda role=None: dict(flags))
    # Only the probe specs below are deferred: the real phase 4 deferred
    # tools leave the registry for this test.
    for name, spec in registry.all_tools().items():
        if spec.deferred:
            monkeypatch.delitem(registry._REGISTRY, name)
    core = registry.get("whats_due")
    write = next(s for s in registry.all_tools().values() if s.effect == registry.EFFECT_WRITE_INTERNAL
                 and s.offered and Role.EXECUTIVE in s.roles and s.sub_app == "bidding")
    extra = {
        "plans_alpha": _deferred(core, "plans_alpha", sub_app="ai_estimating"),
        "plans_beta": _deferred(core, "plans_beta", sub_app="ai_estimating"),
        "pm_gamma": _deferred(core, "pm_gamma", sub_app="pm"),
        "viewer_only": _deferred(core, "viewer_only", roles=ACTUAL_BID_VIEWER_ROLES - {Role.EXECUTIVE}),
        "hidden_one": _deferred(core, "hidden_one", offered=False),
        "lane_write": _deferred(write, "lane_write", roles=WRITER_ROLES),
    }
    for spec in extra.values():
        monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    seen: dict = {}

    def resolve(query, specs, limit=6):
        seen.update(query=query, names=sorted(s.name for s in specs), limit=limit)
        # Best first by name, core tools included: the route drops them.
        return sorted(specs, key=lambda s: s.name)

    monkeypatch.setattr(find, "resolve", resolve)
    state = SimpleNamespace(role=Role.EXECUTIVE, via=RUN)

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = _user
    return SimpleNamespace(client=TestClient(app), db=db, state=state, flags=flags, seen=seen, core=core)


def _find(api, query="plan sheets", **params):
    return api.client.get("/assistant/tools/find", params={"query": query, **params})


def test_a_human_and_a_switched_off_assistant_get_the_bare_404(api):
    api.state.via = None
    res = _find(api)
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    res = api.client.get("/assistant/tools/find")                 # no query: still the 404
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    api.state.via = RUN
    api.db.tables["assistant_settings"][0]["enabled"] = False
    res = _find(api)
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    assert api.seen == {}


def test_only_deferred_tools_of_the_callers_toolset(api):
    res = _find(api, "  plan sheets  ")
    assert res.status_code == 200, res.text
    body = res.json()
    names = [t["name"] for t in body["tools"]]
    assert names == ["lane_write", "plans_alpha", "plans_beta", "pm_gamma"]
    assert body["tools"][1] == {"name": "plans_alpha", "description": "Use plans_alpha for plan questions."}
    assert body["hint"] == router_mod.FIND_HINT_FOUND
    # The resolver saw the whole current toolset (core tools too), never a
    # tool of another role or one that is not offered.
    assert api.seen["query"] == "plan sheets" and api.seen["limit"] == 6
    assert "whats_due" in api.seen["names"] and "viewer_only" not in api.seen["names"]
    assert "hidden_one" not in api.seen["names"]
    assert all(registry.get(n).deferred for n in names)


def test_sub_app_flags_disabled_tools_and_the_limit(api):
    api.flags["ai_estimating"] = False
    api.db.tables["assistant_settings"][0]["disabled_tools"] = ["pm_gamma"]
    assert [t["name"] for t in _find(api).json()["tools"]] == ["lane_write"]
    api.flags["ai_estimating"] = True
    assert [t["name"] for t in _find(api, limit=2).json()["tools"]] == ["lane_write", "plans_alpha"]
    assert api.seen["limit"] == 2
    assert _find(api, limit=11).status_code == 422
    for empty in ("", "   "):
        res = _find(api, empty)
        assert res.status_code == 422 and res.json()["detail"]["code"] == "query_required"
    assert api.client.get("/assistant/tools/find").status_code == 422
    assert _find(api, "x" * 201).status_code == 422


def test_mailbox_review_mode_comes_from_the_runs_conversation(api):
    api.db.tables["assistant_conversations"][0]["mode"] = registry.MODE_MAILBOX_REVIEW
    names = [t["name"] for t in _find(api).json()["tools"]]
    assert names == ["plans_alpha", "plans_beta", "pm_gamma"]          # the write tool is gone
    # Missing server mode provenance fails closed.
    api.db.tables["assistant_tool_calls"] = []
    assert _find(api).json()["tools"] == []


def test_no_match_says_so(api, monkeypatch):
    monkeypatch.setattr(find, "resolve", lambda q, specs, limit=6: [])
    body = _find(api).json()
    assert body == {"tools": [], "hint": router_mod.FIND_HINT_NONE}
    assert EM_DASH not in router_mod.FIND_HINT_FOUND + router_mod.FIND_HINT_NONE


def test_the_role_is_the_live_one(api):
    api.state.role = Role.ACCOUNTANT
    names = [t["name"] for t in _find(api).json()["tools"]]
    assert "lane_write" not in names
