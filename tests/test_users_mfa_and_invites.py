"""Two server-side guarantees added with the 2FA + branded-invite work:

1. `get_current_user` is the real 2FA boundary — it rejects any non-aal2 token
   except the tiny allowlist a not-yet-enrolled user needs to enroll.
2. `invite_user`/`reinvite_user` send a branded Graph email when configured
   (rolling the invite back if the send fails) and fall back to Supabase email
   otherwise.

Fast unit tests: `decode_token` and the Supabase client are stubbed (no real JWT,
no DB), and the async dependencies/handlers are awaited directly.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core import deps
from app.core.roles import Role
from app.models.schemas import InviteUserIn
from app.routers import users
from app.services import invite_email


def _run(value):
    # The deps/handlers under test are now sync (FastAPI threadpools them); run
    # any that are still coroutines, pass plain results through.
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


# ── get_current_user: AAL2 enforcement ─────────────────────────────────────


class _DepsQuery:
    def __init__(self, store):
        self._store = store
        self._update = None

    def select(self, *a, **k):
        return self

    def update(self, patch):
        self._update = patch
        return self

    def eq(self, *a, **k):
        return self

    def single(self):
        return self

    def execute(self):
        if self._update is not None:
            self._store["updates"].append(self._update)
            return SimpleNamespace(data=[{**self._store["profile"], **self._update}])
        return SimpleNamespace(data=self._store["profile"])


class _DepsSB:
    def __init__(self, store):
        self._store = store

    def table(self, name):
        return _DepsQuery(self._store)


def _setup_deps(monkeypatch, *, aal, profile):
    store = {"profile": profile, "updates": []}
    monkeypatch.setattr(
        deps, "decode_token", lambda token: {"sub": profile["id"], "aal": aal}
    )
    monkeypatch.setattr(deps, "get_supabase", lambda: _DepsSB(store))
    return store


def _profile(**over):
    base = {
        "id": "u1",
        "email": "u@g3.com",
        "role": "estimating_admin",
        "is_active": True,
        "is_dev": False,
        "mfa_enrolled": False,
        # set so the invite-acceptance stamp doesn't also fire and muddy `updates`
        "invite_accepted_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(over)
    return base


def test_aal2_passes_and_self_stamps_mfa_enrolled(monkeypatch):
    store = _setup_deps(monkeypatch, aal="aal2", profile=_profile(mfa_enrolled=False))
    user = _run(deps.get_current_user(_request("GET", "/projects"), "Bearer t"))
    assert user.aal == "aal2"
    assert user.mfa_enrolled is True
    # reaching aal2 with the flag unset self-stamps it true
    assert {"mfa_enrolled": True} in store["updates"]


def test_aal1_unenrolled_blocked_with_enrollment_code(monkeypatch):
    _setup_deps(monkeypatch, aal="aal1", profile=_profile(mfa_enrolled=False))
    with pytest.raises(HTTPException) as ei:
        _run(deps.get_current_user(_request("GET", "/projects"), "Bearer t"))
    assert ei.value.status_code == 403
    assert ei.value.detail == "mfa_enrollment_required"


def test_aal1_enrolled_blocked_with_step_up_code(monkeypatch):
    _setup_deps(monkeypatch, aal="aal1", profile=_profile(mfa_enrolled=True))
    with pytest.raises(HTTPException) as ei:
        _run(deps.get_current_user(_request("GET", "/projects"), "Bearer t"))
    assert ei.value.detail == "mfa_step_up_required"


def test_get_users_me_is_allowed_at_aal1(monkeypatch):
    _setup_deps(monkeypatch, aal="aal1", profile=_profile(mfa_enrolled=False))
    user = _run(deps.get_current_user(_request("GET", "/users/me"), "Bearer t"))
    assert user.aal == "aal1"
    assert user.role == Role.ESTIMATING_ADMIN


def test_mfa_required_false_bypasses_enforcement(monkeypatch):
    _setup_deps(monkeypatch, aal="aal1", profile=_profile(mfa_enrolled=False))
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(mfa_required=False))
    user = _run(deps.get_current_user(_request("GET", "/projects"), "Bearer t"))
    assert user.aal == "aal1"  # no exception — enforcement disabled


def test_disabled_account_still_403(monkeypatch):
    _setup_deps(monkeypatch, aal="aal2", profile=_profile(is_active=False))
    with pytest.raises(HTTPException) as ei:
        _run(deps.get_current_user(_request("GET", "/projects"), "Bearer t"))
    assert ei.value.status_code == 403
    assert ei.value.detail == "Account is disabled"


# ── invite_user / reinvite_user: branded Graph send + rollback ──────────────


class _AdminAPI:
    def __init__(self, store):
        self._store = store

    def generate_link(self, params):
        self._store["generate_link"].append(params)
        return SimpleNamespace(
            user=SimpleNamespace(id="newid"),
            properties=SimpleNamespace(
                hashed_token="hash123", verification_type=params["type"]
            ),
        )

    def invite_user_by_email(self, email, options=None):
        self._store["invite_by_email"].append((email, options))
        return SimpleNamespace(user=SimpleNamespace(id="newid"))

    def delete_user(self, uid):
        self._store["deleted_users"].append(uid)


class _RouterTable:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._op = None
        self._payload = None

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def select(self, *a, **k):
        self._op = "select"
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._name == "profiles" and self._op == "insert":
            self._store["inserted"].append(self._payload)
            return SimpleNamespace(data=[self._payload])
        if self._name == "profiles" and self._op == "delete":
            self._store["profile_deleted"] = True
            return SimpleNamespace(data=[])
        if self._name == "profiles" and self._op == "select":
            return SimpleNamespace(data=self._store.get("existing_rows", []))
        return SimpleNamespace(data=[])


class _RouterSB:
    def __init__(self, store):
        self._store = store
        self.auth = SimpleNamespace(admin=_AdminAPI(store))

    def table(self, name):
        return _RouterTable(self._store, name)


def _setup_router(monkeypatch, *, graph, send_raises=False):
    store = {
        "generate_link": [],
        "invite_by_email": [],
        "deleted_users": [],
        "inserted": [],
        "sent": [],
        "profile_deleted": False,
    }
    monkeypatch.setattr(users, "get_supabase", lambda: _RouterSB(store))
    monkeypatch.setattr(users, "audit", lambda *a, **k: None)
    monkeypatch.setattr(invite_email, "graph_configured", lambda: graph)

    def _fake_send(**kwargs):
        store["sent"].append(kwargs)
        if send_raises:
            raise RuntimeError("graph down")

    monkeypatch.setattr(invite_email, "send_invite_email", _fake_send)
    return store


_ADMIN = SimpleNamespace(id="admin1")


def test_invite_graph_path_builds_confirm_url_and_sends(monkeypatch):
    store = _setup_router(monkeypatch, graph=True)
    body = InviteUserIn(email="new@g3.com", full_name="New Person", role=Role.ESTIMATING_ADMIN)
    _run(users.invite_user(body=body, admin=_ADMIN))
    assert store["generate_link"][0]["type"] == "invite"
    assert store["generate_link"][0]["options"]["data"] == {"full_name": "New Person"}
    assert len(store["inserted"]) == 1
    cta = store["sent"][0]["cta_url"]
    assert "/auth/confirm?" in cta
    assert "token_hash=hash123" in cta
    assert "type=invite" in cta


def test_invite_rolls_back_when_email_fails(monkeypatch):
    store = _setup_router(monkeypatch, graph=True, send_raises=True)
    body = InviteUserIn(email="new@g3.com", full_name="New Person", role=Role.ESTIMATING_ADMIN)
    with pytest.raises(HTTPException) as ei:
        _run(users.invite_user(body=body, admin=_ADMIN))
    assert ei.value.status_code == 502
    assert store["profile_deleted"] is True
    assert store["deleted_users"] == ["newid"]


def test_invite_falls_back_to_supabase_email_without_graph(monkeypatch):
    store = _setup_router(monkeypatch, graph=False)
    body = InviteUserIn(email="new@g3.com", full_name="New Person", role=Role.ESTIMATING_ADMIN)
    _run(users.invite_user(body=body, admin=_ADMIN))
    assert store["invite_by_email"]  # used Supabase's own invite email
    assert store["sent"] == []  # no branded Graph send
    assert len(store["inserted"]) == 1


def test_reinvite_uses_magiclink(monkeypatch):
    store = _setup_router(monkeypatch, graph=True)
    store["existing_rows"] = [
        {
            "id": "u1",
            "email": "u@g3.com",
            "full_name": "Pat Doe",
            "role": "estimating_admin",
            "invite_accepted_at": None,
        }
    ]
    _run(users.reinvite_user(user_id="u1", admin=_ADMIN))
    assert store["generate_link"][0]["type"] == "magiclink"
    assert store["sent"][0]["to"] == "u@g3.com"


def test_reinvite_blocked_after_acceptance(monkeypatch):
    store = _setup_router(monkeypatch, graph=True)
    store["existing_rows"] = [
        {"id": "u1", "email": "u@g3.com", "full_name": "Pat", "role": "estimating_admin",
         "invite_accepted_at": "2026-01-01T00:00:00+00:00"}
    ]
    with pytest.raises(HTTPException) as ei:
        _run(users.reinvite_user(user_id="u1", admin=_ADMIN))
    assert ei.value.status_code == 400
