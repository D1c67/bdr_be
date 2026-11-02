"""Human inspection reason/audit boundary and owner-only view notices."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant_inspection as routes

ADMIN = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
CONV = "33333333-3333-4333-8333-333333333333"
BASE = f"/assistant/inspection/conversations/{CONV}"
BODY = {"owner_id": OWNER, "reason_kind": "support", "reason": "Investigate the reported issue"}


@pytest.fixture
def api(monkeypatch):
    state = SimpleNamespace(role=Role.IT_ADMIN, via=None, calls=[], own=True, fail=False, code=None)

    def user():
        return CurrentUser(id=ADMIN, email="admin@example.test", role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    def inspect(user_id, owner_id, conversation_id, **kwargs):
        state.calls.append((user_id, owner_id, conversation_id, kwargs))
        if state.fail:
            raise RuntimeError("private database and message content")
        if state.code:
            return {"ok": False, "code": state.code}
        return {"ok": True, "conversations": [], "messages": [], "tool_results": [], "bounded": True}

    def owned(user_id, conversation_id):
        state.calls.append(("owned", user_id, conversation_id))
        return {"id": conversation_id} if state.own else None

    def views(user_id, conversation_id):
        state.calls.append(("views", user_id, conversation_id))
        return [{"id": "view-1", "viewer_label": "IT Admin", "action": "read", "created_at": "2026-10-27T12:00:00Z"}]

    monkeypatch.setattr(routes.repo, "admin_conversation_read", inspect)
    monkeypatch.setattr(routes.repo, "get_conversation", owned)
    monkeypatch.setattr(routes.repo, "conversation_views", views)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = user
    return SimpleNamespace(client=TestClient(app), state=state)


@pytest.mark.parametrize("action", ["read", "export"])
@pytest.mark.parametrize("reason_kind", ["legal", "support", "offboarding", "other"])
def test_inspection_passes_human_reason_and_action_to_atomic_audited_repo(api, action, reason_kind):
    response = api.client.post(f"{BASE}/{action}", json={**BODY, "reason_kind": reason_kind, "reason": "  A human-entered investigation reason  "})
    assert response.status_code == 200
    assert api.state.calls == [(ADMIN, OWNER, CONV, {"reason_kind": reason_kind, "reason": "A human-entered investigation reason", "action": action})]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    if action == "export":
        assert response.headers["content-disposition"] == f'attachment; filename="assistant-conversation-{CONV}.json"'
        assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("action", ["read", "export"])
@pytest.mark.parametrize("kind", ["assistant", "executive", "accountant", "estimator"])
def test_inspection_never_grants_executive_or_assistant_conversation_access(api, action, kind):
    if kind == "assistant":
        api.state.via = CONV
    else:
        api.state.role = Role(kind)
    assert api.client.post(f"{BASE}/{action}", json=BODY).status_code == 403
    assert api.state.calls == []


@pytest.mark.parametrize("change", [{"reason": "short"}, {"reason": " " * 20}, {"reason": "x" * 501},
                                     {"reason_kind": "made_up"}, {"owner_id": "not-uuid"}])
def test_invalid_reason_or_owner_never_reaches_inspection_rpc(api, change):
    assert api.client.post(f"{BASE}/read", json={**BODY, **change}).status_code == 422
    assert api.state.calls == []


@pytest.mark.parametrize("code,status", [("forbidden", 403), ("not_found", 404), ("reason_required", 422), ("unknown", 409)])
def test_rpc_refusal_exposes_no_database_details(api, code, status):
    api.state.code = code
    response = api.client.post(f"{BASE}/read", json=BODY)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


def test_audit_rpc_failure_releases_no_content(api):
    api.state.fail = True
    response = api.client.post(f"{BASE}/export", json=BODY)
    assert response.status_code == 503
    assert "private database" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_owner_sees_metadata_only_after_owner_scoped_lookup(api):
    api.state.role = Role.ACCOUNTANT
    response = api.client.get(f"/assistant/conversations/{CONV}/views")
    assert response.status_code == 200
    assert api.state.calls == [("owned", ADMIN, CONV), ("views", ADMIN, CONV)]
    assert response.json()["items"][0]["viewer_label"] == "IT Admin"
    assert "reason" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_other_owner_and_missing_conversation_are_identical_and_do_not_read_views(api):
    api.state.own = False
    response = api.client.get(f"/assistant/conversations/{CONV}/views")
    assert response.status_code == 404
    assert api.state.calls == [("owned", ADMIN, CONV)]


def test_assistant_cannot_read_owner_view_notices(api):
    api.state.via = CONV
    assert api.client.get(f"/assistant/conversations/{CONV}/views").status_code == 403
    assert api.state.calls == []


@pytest.mark.parametrize("kind", ["human", "assistant"])
def test_attachment_preview_is_owner_authorized_and_never_a_model_link(api, monkeypatch, kind):
    calls = []
    monkeypatch.setattr(routes, "_require_assistant", lambda user: calls.append(("enabled", user.id)))
    def files(user_id, ids):
        calls.append(("files", user_id, ids))
        return [{"storage_path": "owner/file.pdf"}]
    def signed(path, **kwargs):
        calls.append(("signed", path, kwargs))
        return "https://example.supabase.co/storage/v1/object/sign/owner/file.pdf?token=opaque"
    monkeypatch.setattr(routes.assistant_mail_send, "files", files)
    monkeypatch.setattr(routes.storage, "signed_url", signed)
    if kind == "assistant":
        api.state.via = CONV
    response = api.client.get(f"/assistant/files/{CONV}/preview")
    if kind == "assistant":
        assert response.status_code == 403
        assert calls == []
    else:
        assert response.status_code == 200
        assert response.json()["mime_type"] == "application/pdf"
        assert calls == [("enabled", ADMIN), ("files", ADMIN, [CONV]), ("signed", "owner/file.pdf", {"ttl_seconds": 60, "use_cache": False})]
        assert response.headers["cache-control"] == "no-store"


def test_attachment_missing_expired_and_unowned_have_one_generic_refusal(api, monkeypatch):
    monkeypatch.setattr(routes, "_require_assistant", lambda user: None)
    def refused(*args):
        raise ValueError("private filename or storage path")
    monkeypatch.setattr(routes.assistant_mail_send, "files", refused)
    response = api.client.get(f"/assistant/files/{CONV}/preview")
    assert response.status_code == 404
    assert "private" not in response.text
    assert response.headers["cache-control"] == "no-store"
