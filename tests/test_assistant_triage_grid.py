"""The admin role grid skips catalog entries with `grid: False` (Phase 8 plan
5.6 and 17.4, review finding F4).

The triage escalation joins `proactive.ROUTER_KINDS`, but its severity is the
model's and not an admin choice, so `GET /assistant/alerts/defaults` must omit
it and the defaults routes must refuse it with `unknown_kind`, or an admin role
row (an admin row that is off is final) could silence the kind for a role. The
three Phase 7 kinds stay exactly as listed.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, proactive
from app.core import supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import INTERNAL_ROLES, Role
from app.routers import assistant_inbox as inbox_router

from tests.assistant_fakes import (
    P7_T0, USER, FakeDB, cursor_row, profile, settings_row,
)

KIND = "assistant.triage_escalation"
PHASE7_KINDS = ["bid.due_not_ready", "todo.due", "assistant.approval_expiring"]
NOW = P7_T0 + timedelta(hours=2)


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()],
        assistant_settings=[settings_row(proactive_enabled=True)],
        assistant_profiles=[], assistant_inbox=[], assistant_subscriptions=[], assistant_digests=[],
        assistant_project_mutes=[], assistant_router_cursors=[cursor_row(), cursor_row(name="audit_log")],
        notifications=[], audit_log=[], projects=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    settings = get_settings()
    monkeypatch.setattr(settings, "bidding_enabled", True)
    monkeypatch.setattr(settings, "assistant_proactive_enabled", True)
    monkeypatch.setattr(settings, "assistant_proactive_user_allowlist", "")
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(inbox_router, "_now", lambda: NOW)
    from app.services import notifications

    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    state = SimpleNamespace(role=Role.EXECUTIVE)

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=state.role, is_active=True, via_assistant_run_id=None)

    app = FastAPI()
    app.include_router(inbox_router.router)
    app.dependency_overrides[get_current_user] = _user
    return SimpleNamespace(client=TestClient(app), db=db, state=state)


def _grid_kinds(api, path="/assistant/alerts/defaults"):
    res = api.client.get(path)
    assert res.status_code == 200, res.text
    return [k["event_type"] for k in res.json()["kinds"]]


def test_the_catalog_entry_is_flagged_and_the_phase_7_kinds_are_not():
    assert proactive.EVENT_TYPES[KIND]["grid"] is False
    assert proactive.EVENT_TYPES[KIND]["urgent_allowed"] is False
    assert KIND in proactive.ROUTER_KINDS
    for kind in PHASE7_KINDS:
        assert proactive.EVENT_TYPES[kind]["grid"] is True


def test_the_defaults_grid_omits_the_kind_and_lists_exactly_the_phase_7_kinds(api):
    assert _grid_kinds(api) == PHASE7_KINDS
    for role in (r for r in Role if r in INTERNAL_ROLES):
        assert _grid_kinds(api, f"/assistant/alerts/defaults?role={role.value}") == PHASE7_KINDS


@pytest.mark.parametrize("mode", ["off", "inbox", "digest", "alert", "urgent"])
def test_put_default_for_the_kind_is_unknown_kind_and_writes_nothing(api, mode):
    res = api.client.put(f"/assistant/alerts/defaults/executive/{KIND}", json={"mode": mode})
    assert res.status_code == 404 and res.json()["detail"]["code"] == "unknown_kind"
    assert not api.db.tables.get("assistant_subscriptions")
    assert not api.db.tables.get("audit_log")


def test_reset_default_for_the_kind_is_unknown_kind_and_writes_nothing(api):
    res = api.client.post(f"/assistant/alerts/defaults/executive/{KIND}/reset")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "unknown_kind"
    assert not api.db.tables.get("audit_log")


def test_a_phase_7_kind_still_takes_an_admin_default(api):
    res = api.client.put("/assistant/alerts/defaults/executive/todo.due", json={"mode": "inbox"})
    assert res.status_code == 200
    assert [k["event_type"] for k in res.json()["kinds"]] == PHASE7_KINDS
