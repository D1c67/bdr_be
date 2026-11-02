"""Human-only application metadata, with no Microsoft sign-in routes."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import INTERNAL_ROLES, Role
from app.routers import assistant_outlook as routes

USER = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def api(monkeypatch):
    state = SimpleNamespace(role=Role.EXECUTIVE, via=None, available=True, calls=[], failure=False)

    def user():
        return CurrentUser(id=USER, email="owner@example.test", role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    def status(user_id):
        state.calls.append(user_id)
        if state.failure:
            raise routes.assistant_ms.OutlookError("provider-token-secret")
        return {"available": state.available, "authentication": "application",
                "mailbox": "owner@example.test", "shared_mailboxes": ["bids@example.test"]}

    monkeypatch.setattr(routes.assistant_ms, "status", status)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = user
    return SimpleNamespace(client=TestClient(app), state=state)


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES, key=str))
def test_internal_humans_get_metadata_without_provider_redirects(api, role):
    api.state.role = role
    response = api.client.get("/assistant/outlook", follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"available": True, "authentication": "application",
                               "mailbox": "owner@example.test", "shared_mailboxes": ["bids@example.test"]}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers
    assert api.state.calls == [USER]
    for key in ["access_token", "refresh_token", "authorization_url", "state", "code", "connected", "upn"]:
        assert key not in response.json()


@pytest.mark.parametrize("kind", ["assistant", "estimator"])
def test_assistants_and_external_users_cannot_inspect_status(api, kind):
    if kind == "assistant":
        api.state.via = "22222222-2222-4222-8222-222222222222"
    else:
        api.state.role = Role.ESTIMATOR
    response = api.client.get("/assistant/outlook")
    assert response.status_code == 403
    assert api.state.calls == []
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers


def test_unconfigured_integration_does_not_claim_a_microsoft_connection(api):
    api.state.available = False
    response = api.client.get("/assistant/outlook")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["authentication"] == "application"
    assert "connected" not in response.json()
    assert response.headers["cache-control"] == "no-store"


def test_service_errors_are_generic_and_not_cached(api):
    api.state.failure = True
    response = api.client.get("/assistant/outlook")
    assert response.status_code == 409
    assert response.json() == {"detail": "Outlook application access is unavailable."}
    assert "provider-token-secret" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-error-code"] == "outlook_unavailable"
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers


@pytest.mark.parametrize("path,method", [("/connect", "post"), ("/disconnect", "post"),
                                          ("/callback?state=secret&code=secret", "get"),
                                          ("/callback", "post"), ("", "post")])
def test_old_oauth_routes_set_no_cookies_or_redirects(api, path, method):
    response = getattr(api.client, method)("/assistant/outlook" + path, follow_redirects=False)
    assert response.status_code in {404, 405}
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers
    assert api.state.calls == []
    assert "secret" not in response.text
