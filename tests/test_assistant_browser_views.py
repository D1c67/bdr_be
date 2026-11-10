"""Owner content and secret-safe human help routes, plus audited admin access."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import CurrentUser, require_internal, require_writer
from app.core.roles import Role
from app.routers import assistant_browser, assistant_browser_views as routes


@pytest.fixture
def api(monkeypatch):
    user = CurrentUser(id=str(uuid4()), email="test@example.test", role=Role.IT_ADMIN, is_active=True)
    monkeypatch.setattr(assistant_browser, "_require_assistant", lambda user: None)
    monkeypatch.setattr(routes, "_enabled", lambda: None)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_writer] = lambda: user
    app.dependency_overrides[require_internal] = lambda: user
    return SimpleNamespace(http=TestClient(app), user=user, job=str(uuid4()))


@pytest.mark.parametrize("body", [None, ["SECRET_SENTINEL"], {"secret_value": "SECRET_SENTINEL"}, "SECRET_SENTINEL"])
def test_help_validation_never_reflects_secret(api, body):
    response = api.http.post(f"/assistant/browser-tasks/{api.job}/help", json=body)
    assert response.status_code == 422 and "SECRET_SENTINEL" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_help_binds_owner_and_live_help_id(api, monkeypatch):
    calls = []
    monkeypatch.setattr(routes.help_flow, "owner_reply", lambda *a, **kw: calls.append((a, kw)) or {"id": "help"})
    help_id, client_id = str(uuid4()), str(uuid4())
    response = api.http.post(f"/assistant/browser-tasks/{api.job}/help", json={
        "expected_help_id": help_id, "client_msg_id": client_id, "action": "message", "secret_value": "123456"})
    assert response.status_code == 200 and calls[0][0] == (api.user.id, api.job)
    assert calls[0][1]["expected_help_id"] == help_id and calls[0][1]["secret_value"] == "123456"
    assert "123456" not in response.text


def test_help_text_secret_refusal_carries_the_error_code_header(api, monkeypatch):
    """Review finding: the frontend reads the code from X-Error-Code only."""
    from app.services.assistant_browser_secrets import SECRET_IN_MESSAGE_TEXT, SecretInMessage

    def refuse(*a, **kw):
        raise SecretInMessage()

    monkeypatch.setattr(routes.help_flow, "owner_reply", refuse)
    response = api.http.post(f"/assistant/browser-tasks/{api.job}/help", json={
        "expected_help_id": str(uuid4()), "client_msg_id": str(uuid4()), "action": "message",
        "body": "my password is Summer2026!"})
    assert response.status_code == 422
    assert response.headers["x-error-code"] == "secret_in_message"
    assert response.headers["cache-control"] == "no-store"
    detail = response.json()["detail"]
    assert detail["code"] == "secret_in_message" and detail["msg"] == SECRET_IN_MESSAGE_TEXT
    assert "Summer2026" not in response.text


def test_assistant_cannot_read_or_reply_human_content(api):
    object.__setattr__(api.user, "via_assistant_run_id", str(uuid4()))
    assert api.http.get(f"/assistant/browser-tasks/{api.job}/trace").status_code == 403
    assert api.http.get(f"/assistant/browser-tasks/{api.job}/help").status_code == 403


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.ACCOUNTANT])
def test_admin_inspection_has_no_executive_bypass(api, role, monkeypatch):
    object.__setattr__(api.user, "role", role)
    monkeypatch.setattr(routes.records, "inspect", lambda *a: pytest.fail("must not read"))
    assert api.http.post(f"/assistant/inspection/browser-tasks/{api.job}/read", json={
        "owner_id": str(uuid4()), "reason": "Investigate a reported failure"}).status_code == 403


def test_admin_reason_precedes_decryption_service(api, monkeypatch):
    calls = []
    monkeypatch.setattr(routes.records, "inspect", lambda *a: calls.append(a) or {"audit_id": "audit", "items": []})
    owner = str(uuid4())
    response = api.http.post(f"/assistant/inspection/browser-tasks/{api.job}/read", json={
        "owner_id": owner, "reason": "Investigate a reported failure"})
    assert response.status_code == 200 and calls == [(api.user.id, owner, api.job, "Investigate a reported failure")]
    assert response.headers["referrer-policy"] == "no-referrer"


def test_private_failures_are_sanitized(api, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("PRIVATE_DATABASE_CONTENT")
    monkeypatch.setattr(routes.records, "owner_trace", fail)
    response = api.http.get(f"/assistant/browser-tasks/{api.job}/trace")
    assert response.status_code == 503 and "PRIVATE_DATABASE_CONTENT" not in response.text


def test_capped_help_reply_returns_the_fixed_stop_guidance(api, monkeypatch):
    def capped(*a, **k):
        raise routes.help_flow.BrowserTaskCapped(routes.help_flow.CAPPED)
    monkeypatch.setattr(routes.help_flow, "owner_reply", capped)
    response = api.http.post(f"/assistant/browser-tasks/{api.job}/help", json={
        "expected_help_id": str(uuid4()), "client_msg_id": str(uuid4()), "action": "retry"})
    assert response.status_code == 409 and response.json()["detail"] == routes.help_flow.CAPPED
    monkeypatch.setattr(routes.help_flow, "owner_reply",
                        lambda *a, **k: (_ for _ in ()).throw(routes.records.BrowserContentUnavailable("x")))
    response = api.http.post(f"/assistant/browser-tasks/{api.job}/help", json={
        "expected_help_id": str(uuid4()), "client_msg_id": str(uuid4()), "action": "retry"})
    assert response.status_code == 409 and response.json()["detail"] == "Browser content is unavailable."
