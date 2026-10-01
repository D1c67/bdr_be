"""Security review, users-admin group: self-elevation, dev flag scoping, and the
account-takeover chain (email rewrite, MFA reset) on the admin user routes.

Reuses the in-memory Supabase stub from test_user_admin_management.
"""

import pytest
from fastapi import HTTPException

from app.core.roles import Role
from app.models.schemas import AdminUpdateUserIn
from app.routers import users
from app.services import graph_email
from tests.test_user_admin_management import _ADMIN, _profile, _setup


def _actions(store):
    return [a[1] for a, _ in store["audits"]]


# ── Finding 10: no self-elevation ──────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        AdminUpdateUserIn(is_dev=True),
        AdminUpdateUserIn(role=Role.EXECUTIVE),
    ],
    ids=["is_dev", "role"],
)
def test_admin_cannot_change_their_own_role_or_dev_flag(monkeypatch, body):
    store = _setup(
        monkeypatch,
        _profile("admin1", role="it_admin"),
        _profile("a2", role="executive"),
    )
    with pytest.raises(HTTPException) as ei:
        users.update_user(user_id="admin1", body=body, admin=_ADMIN)
    assert ei.value.status_code == 403
    assert ei.value.headers["X-Error-Code"] == "self_privilege_change"
    assert store["profiles"]["admin1"]["role"] == "it_admin"
    assert store["profiles"]["admin1"]["is_dev"] is False


def test_self_query_param_dev_grant_is_refused_too(monkeypatch):
    _setup(monkeypatch, _profile("admin1", role="it_admin"), _profile("a2", role="executive"))
    with pytest.raises(HTTPException) as ei:
        users.update_user(user_id="admin1", is_dev=True, admin=_ADMIN)
    assert ei.value.status_code == 403


def test_admin_may_still_fix_their_own_name_and_resend_same_role(monkeypatch):
    store = _setup(monkeypatch, _profile("admin1", role="it_admin"))
    out = users.update_user(
        user_id="admin1",
        body=AdminUpdateUserIn(full_name="New Name", role=Role.IT_ADMIN),
        admin=_ADMIN,
    )
    assert out["full_name"] == "New Name"
    assert store["profiles"]["admin1"]["role"] == "it_admin"


def test_another_admin_can_grant_dev_and_it_gets_its_own_audit_row(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", role="it_admin"))
    users.update_user(user_id="u1", body=AdminUpdateUserIn(is_dev=True), admin=_ADMIN)
    assert store["profiles"]["u1"]["is_dev"] is True
    assert "user.dev_granted" in _actions(store)


def test_a_mailbox_remap_gets_its_own_audit_row(monkeypatch):
    store = _setup(monkeypatch, _profile("admin1", role="executive"))
    users.update_user(
        user_id="admin1",
        body=AdminUpdateUserIn(rfp_mailboxes=["boss@g3electrical.com"]),
        admin=_ADMIN,
    )
    rows = [(a, k) for a, k in store["audits"] if a[1] == "user.rfp_mailboxes_changed"]
    assert len(rows) == 1
    assert rows[0][0][4] == {"from": [], "to": ["boss@g3electrical.com"]}


# ── Finding 35: dev flag scoping + takeover chain ──────────────────────────


def test_dev_flag_is_never_granted_to_an_external_estimator(monkeypatch):
    store = _setup(monkeypatch, _profile("e1", role="estimator"))
    with pytest.raises(HTTPException) as ei:
        users.update_user(user_id="e1", body=AdminUpdateUserIn(is_dev=True), admin=_ADMIN)
    assert ei.value.status_code == 400
    assert ei.value.headers["X-Error-Code"] == "is_dev_internal_only"
    assert store["profiles"]["e1"]["is_dev"] is False


def test_dev_flag_refused_when_the_same_patch_demotes_to_estimator(monkeypatch):
    _setup(monkeypatch, _profile("u1", role="accountant"))
    with pytest.raises(HTTPException) as ei:
        users.update_user(
            user_id="u1",
            body=AdminUpdateUserIn(role=Role.ESTIMATOR, is_dev=True),
            admin=_ADMIN,
        )
    assert ei.value.status_code == 400


def test_demoting_a_dev_to_estimator_clears_the_flag(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", role="accountant", is_dev=True))
    users.update_user(user_id="u1", body=AdminUpdateUserIn(role=Role.ESTIMATOR), admin=_ADMIN)
    assert store["profiles"]["u1"]["is_dev"] is False
    assert "user.dev_revoked" in _actions(store)


def test_revoking_dev_on_an_estimator_profile_is_allowed(monkeypatch):
    store = _setup(monkeypatch, _profile("e1", role="estimator", is_dev=True))
    users.update_user(user_id="e1", body=AdminUpdateUserIn(is_dev=False), admin=_ADMIN)
    assert store["profiles"]["e1"]["is_dev"] is False


def test_email_change_notifies_the_old_address_and_revokes_sessions(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", email="old@g3.com", role="executive"))
    users.update_user(user_id="u1", body=AdminUpdateUserIn(email="new@g3.com"), admin=_ADMIN)
    assert [m["to"] for m in store["graph_mail"]] == [["old@g3.com"]]
    assert "new@g3.com" in store["graph_mail"][0]["body_html"]
    assert store["rpcs"] == [("admin_revoke_user_sessions", {"p_user_id": "u1"})]
    acts = _actions(store)
    assert "user.email_change_notice" in acts
    assert "user.sessions_revoked" in acts


def test_email_change_still_lands_when_notice_and_revoke_fail(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", email="old@g3.com"))
    store["rpc_fails"] = True

    def boom(**k):
        raise RuntimeError("graph down")

    monkeypatch.setattr(graph_email, "send_mail", boom)
    out = users.update_user(user_id="u1", body=AdminUpdateUserIn(email="new@g3.com"), admin=_ADMIN)
    assert out["email"] == "new@g3.com"
    notice = [a for a, _ in store["audits"] if a[1] == "user.email_change_notice"][0]
    revoke = [a for a, _ in store["audits"] if a[1] == "user.sessions_revoked"][0]
    assert notice[4]["sent"] is False
    assert revoke[4] == {"reason": "email_change", "ok": False}


def test_own_email_change_notifies_but_does_not_sign_the_admin_out(monkeypatch):
    store = _setup(monkeypatch, _profile("admin1", email="me@g3.com", role="it_admin"))
    users.update_user(user_id="admin1", body=AdminUpdateUserIn(email="me2@g3.com"), admin=_ADMIN)
    assert [m["to"] for m in store["graph_mail"]] == [["me@g3.com"]]
    assert store["rpcs"] == []


def test_admin_mfa_reset_revokes_the_users_sessions(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", mfa_enrolled=True))
    monkeypatch.setattr(users, "_delete_user_factors", lambda uid: None)
    users.reset_user_mfa(user_id="u1", admin=_ADMIN)
    assert store["rpcs"] == [("admin_revoke_user_sessions", {"p_user_id": "u1"})]
    assert "user.sessions_revoked" in _actions(store)


# ── Finding 10 retest: non-canonical spellings of the caller's own UUID ─────
#
# profiles.id is a uuid column, so Postgres matches uppercase, braced and
# hyphenless spellings of the same id. The stub below mirrors that, so the
# self guards must normalise the path param rather than string-compare it.

import uuid as _uuid  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

from tests import test_user_admin_management as _base  # noqa: E402

_ADMIN_UUID = "9f1e1e1e-aaaa-4bbb-8ccc-123456789abc"
_UUID_ADMIN = _NS(id=_ADMIN_UUID)


def _pg_uuid(v):
    try:
        return str(_uuid.UUID(str(v)))
    except ValueError:
        return v


class _UuidQuery(_base._Query):
    def eq(self, col, val):
        return super().eq(col, _pg_uuid(val) if col == "id" else val)


class _UuidSB(_base._SB):
    def table(self, name):
        return _UuidQuery(self._store)


def _uuid_setup(monkeypatch):
    store = _setup(
        monkeypatch,
        _profile(_ADMIN_UUID, role="it_admin"),
        _profile("a2", role="executive"),
    )
    monkeypatch.setattr(users, "get_supabase", lambda: _UuidSB(store))
    return store


_ALIASES = [
    _ADMIN_UUID.upper(),
    "{" + _ADMIN_UUID + "}",
    _ADMIN_UUID.replace("-", ""),
]


@pytest.mark.parametrize("alias", _ALIASES, ids=["upper", "braced", "hyphenless"])
def test_self_dev_grant_via_uuid_alias_is_refused(monkeypatch, alias):
    store = _uuid_setup(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        users.update_user(user_id=alias, body=AdminUpdateUserIn(is_dev=True), admin=_UUID_ADMIN)
    assert ei.value.status_code == 403
    assert ei.value.headers["X-Error-Code"] == "self_privilege_change"
    assert store["profiles"][_ADMIN_UUID]["is_dev"] is False


@pytest.mark.parametrize("alias", _ALIASES, ids=["upper", "braced", "hyphenless"])
def test_self_disable_and_delete_via_uuid_alias_are_refused(monkeypatch, alias):
    store = _uuid_setup(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        users.update_user(user_id=alias, body=AdminUpdateUserIn(is_active=False), admin=_UUID_ADMIN)
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id=alias, admin=_UUID_ADMIN)
    assert ei.value.status_code == 403
    assert _ADMIN_UUID in store["profiles"]


def test_uuid_alias_email_change_on_self_does_not_revoke_own_sessions(monkeypatch):
    store = _uuid_setup(monkeypatch)
    users.update_user(
        user_id=_ADMIN_UUID.upper(),
        body=AdminUpdateUserIn(email="me2@g3.com"),
        admin=_UUID_ADMIN,
    )
    assert store["rpcs"] == []
