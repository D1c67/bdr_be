from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import registry
from app.assistant.loop import _Turn
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import INTERNAL_ROLES, Role
from app.routers import assistant_phase5 as routes

FLAGS = {name: True for name in registry.SUB_APPS}


@pytest.mark.parametrize("role", list(Role))
def test_phase5_mode_role_and_unknown_mode_boundaries(role):
    user = SimpleNamespace(role=role, is_active=True)
    normal = {s.name for s in registry.toolset_for(user, FLAGS)}
    review = {s.name for s in registry.toolset_for(user, FLAGS, mode="mailbox_review")}
    assert registry.toolset_for(user, FLAGS, mode="unavailable") == []
    if role not in INTERNAL_ROLES:
        assert normal == review == set()
        return
    assert {"email_search", "email_read", "email_draft", "web_search", "web_fetch"} <= normal
    assert "read_mailbox" not in normal
    assert not review.intersection({"email_draft", "email_search", "email_read", "email_attachment_to_staging",
                                    "web_search", "web_fetch", "memory_save", "schedule_create", "run_ai_estimating"})
    if role in {Role.IT_ADMIN, Role.EXECUTIVE}:
        assert "read_mailbox" in review
    else:
        assert not review


def test_mail_function_output_is_not_copied_into_transcript():
    ex = SimpleNamespace(specs={"email_read": registry.get("email_read")})
    turn = SimpleNamespace(executor=ex)
    item = {"type": "function_call_output", "call_id": "x", "output": "FULL RAW BODY SECRET"}
    stored = _Turn._stored_output(turn, {"name": "email_read"}, item)
    assert "FULL RAW BODY SECRET" not in str(stored)
    assert item["output"] == "FULL RAW BODY SECRET"


@pytest.fixture
def api(monkeypatch):
    state = SimpleNamespace(role=Role.IT_ADMIN, via=None)
    def user():
        return CurrentUser(id="u", email="owner@example.test", role=state.role,
                           is_active=True, via_assistant_run_id=state.via)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = user
    monkeypatch.setattr(routes, "_require_assistant", lambda u: (None, None))
    return TestClient(app), state


@pytest.mark.parametrize("role", list(INTERNAL_ROLES))
def test_access_logs_human_it_admin_only(api, monkeypatch, role):
    client, state = api
    state.role = role
    calls = []
    monkeypatch.setattr(routes.repo, "mailbox_access_list", lambda *a, **k: calls.append(a) or [])
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(mailbox_exclusion_set={"excluded@example.test"}))
    response = client.get("/assistant/mailbox-access")
    assert response.status_code == (200 if role == Role.IT_ADMIN else 403)
    assert bool(calls) == (role == Role.IT_ADMIN)
    state.via = "assistant-run"
    assert client.get("/assistant/mailbox-access").status_code == 403


def test_calendar_human_only_no_graph_from_status(api, monkeypatch):
    client, state = api
    monkeypatch.setattr(routes.assistant_calendar, "preferences", lambda u: {
        "available": False, "enabled": False, "mode": "context", "lead_minutes": 15})
    assert client.get("/assistant/calendar/preferences").json()["enabled"] is False
    state.via = "assistant-run"
    assert client.patch("/assistant/calendar/preferences", json={"enabled": True}).status_code == 403


def test_evidence_log_maps_persisted_labels_and_stable_cursor(api, monkeypatch):
    client, _ = api
    stamp = "2026-10-27T12:00:00+00:00"
    monkeypatch.setattr(routes.repo, "evidence_access_list", lambda *a, **k: [
        {"id": "b", "reader_label": "Reader", "subject_label": "Subject", "created_at": stamp},
        {"id": "a", "reader_label": "Reader", "subject_label": "Subject", "created_at": stamp},
    ])
    response = client.get("/assistant/evidence-access", params={"limit": 1})
    assert response.status_code == 200
    assert response.json()["items"][0]["user_label"] == "Reader"
    assert response.json()["items"][0]["target_label"] == "Subject"
    assert response.json()["next_before"] == stamp + "|b"


@pytest.mark.parametrize("path,method", [("mailbox-access", "mailbox_access_list"),
                                         ("evidence-access", "evidence_access_list")])
def test_access_log_invalid_cursor_is_safe_client_error(api, monkeypatch, path, method):
    client, _ = api
    def invalid(*args, **kwargs):
        raise ValueError("private error details")
    monkeypatch.setattr(routes.repo, method, invalid)
    response = client.get("/assistant/" + path, params={"before": "invalid"})
    assert response.status_code == 422
    assert "private error" not in response.text


def test_web_tool_missing_or_review_provenance_cannot_call_provider(api, monkeypatch):
    client, state = api
    state.via = "assistant-run"
    calls = []
    monkeypatch.setattr(routes.assistant_web, "search", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(routes, "_trusted_turn", lambda *a: {"mode": "mailbox_review", "mailbox_tainted": True})
    assert client.get("/assistant/web/search", params={"q": "secret"}).status_code == 409
    assert calls == []
