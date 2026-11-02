"""The assistant offboarding call sites in app/routers/users.py
(docs/ASSISTANT_PHASE3_PLAN.md 3.10.4): the five user-admin paths call the
step once with the right mode, an internal role change runs only the
reconcile, a no-op role PATCH and a reactivation run nothing, a hard delete
is refused by legal hold and by a critical failure and runs before the auth
delete, the dev role switch calls nothing, and while the assistant is off
only deactivation and role changes wait (an MFA reset and a delete still
run their step, legal hold included, unless the tables are missing). The handlers are called directly over the in-memory
profiles stub of tests/test_user_admin_management.py."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.assistant import repo
from app.core.roles import Role
from app.models.schemas import AdminUpdateUserIn, RoleSwitchIn
from app.routers import users
from app.services import assistant_offboarding
from tests.test_user_admin_management import _ADMIN, _profile, _setup

EM_DASH = chr(0x2014)
# The real legal hold reader (tests/conftest.py stubs it for every other test).
REAL_ASSISTANT_TABLES = users._assistant_tables


@pytest.fixture
def calls(monkeypatch):
    """Records every offboarding call (and the store state at that moment)."""
    log: list[dict] = []
    state: dict = {"ok": True, "held": False, "store": None}
    monkeypatch.setattr(users, "get_settings", lambda: SimpleNamespace(assistant_enabled=True))
    monkeypatch.setattr(users, "_delete_user_factors", lambda user_id: None)
    monkeypatch.setattr(users, "_assistant_tables", REAL_ASSISTANT_TABLES)

    def offboard(user_id, mode, *, actor_id, old_role=None, new_role=None, prior_revoked=0):
        store = state["store"] or {}
        log.append({"call": "offboard", "user_id": user_id, "mode": mode, "actor_id": actor_id,
                    "old_role": old_role, "new_role": new_role, "prior_revoked": prior_revoked,
                    "profile": dict((store.get("profiles") or {}).get(user_id) or {}),
                    "auth_deleted": list(store.get("auth_deleted") or [])})
        return assistant_offboarding.OffboardResult(ok=state["ok"], failures=[] if state["ok"] else ["cards"])

    def revoke(user_id, reason):
        store = state["store"] or {}
        log.append({"call": "revoke", "user_id": user_id, "reason": reason,
                    "profile": dict((store.get("profiles") or {}).get(user_id) or {})})
        return 1

    monkeypatch.setattr(assistant_offboarding, "offboard", offboard)
    monkeypatch.setattr(assistant_offboarding, "revoke_delegation", revoke)
    def legal_hold(user_id):
        if isinstance(state["held"], Exception):
            raise state["held"]
        return state["held"]

    monkeypatch.setattr(repo, "legal_hold", legal_hold)
    return SimpleNamespace(log=log, state=state)


def _store(monkeypatch, calls, *rows):
    store = _setup(monkeypatch, *rows)
    calls.state["store"] = store
    return store


def _modes(calls) -> list[tuple]:
    return [(c["call"], c.get("mode") or c.get("reason")) for c in calls.log]


# ── MFA resets ───────────────────────────────────────────────────────────────


def test_an_admin_mfa_reset_offboards_in_mfa_reset_mode(monkeypatch, calls):
    _store(monkeypatch, calls, _profile("u1"))
    users.reset_user_mfa(user_id="u1", admin=_ADMIN)
    assert _modes(calls) == [("offboard", "mfa_reset")]
    assert calls.log[0]["actor_id"] == "admin1"
    assert calls.log[0]["profile"]["mfa_enrolled"] is False      # after the profile write


def test_a_self_mfa_reset_runs_the_same_step(monkeypatch, calls):
    _store(monkeypatch, calls, _profile("u1"))
    me = SimpleNamespace(id="u1", impersonated_by=None)
    users.reset_my_mfa(user=me)
    assert _modes(calls) == [("offboard", "mfa_reset")] and calls.log[0]["actor_id"] == "u1"


# ── update_user ──────────────────────────────────────────────────────────────


def test_deactivation_revokes_before_the_write_and_offboards_after(monkeypatch, calls):
    _store(monkeypatch, calls, _profile("u1"))
    users.update_user(user_id="u1", body=AdminUpdateUserIn(is_active=False), admin=_ADMIN)
    assert _modes(calls) == [("revoke", "deactivated"), ("offboard", "deactivated")]
    assert calls.log[0]["profile"]["is_active"] is True             # revoke: before the write
    assert calls.log[1]["profile"]["is_active"] is False            # offboard: after it
    assert calls.log[1]["old_role"] == "estimating_admin"
    # The revoke before the write is counted in the offboarding audit (OFB-7).
    assert calls.log[1]["prior_revoked"] == 1


def test_demotion_to_the_external_estimator_offboards_as_demoted_external(monkeypatch, calls):
    _store(monkeypatch, calls, _profile("u1"))
    users.update_user(user_id="u1", role=Role.ESTIMATOR, admin=_ADMIN)
    assert _modes(calls) == [("revoke", "role_changed"), ("offboard", "demoted_external")]
    assert (calls.log[1]["old_role"], calls.log[1]["new_role"]) == ("estimating_admin", "estimator")


def test_an_internal_role_change_runs_only_the_reconcile(monkeypatch, calls):
    _store(monkeypatch, calls, _profile("u1"))
    users.update_user(user_id="u1", role=Role.ACCOUNTANT, admin=_ADMIN)
    assert _modes(calls) == [("offboard", "role_changed")]
    assert (calls.log[0]["old_role"], calls.log[0]["new_role"]) == ("estimating_admin", "accountant")


def test_a_promotion_from_the_estimator_runs_nothing(monkeypatch, calls):
    _store(monkeypatch, calls, _profile("u1", role="estimator"))
    users.update_user(user_id="u1", role=Role.ACCOUNTANT, admin=_ADMIN)
    assert calls.log == []


@pytest.mark.parametrize("kw", [
    {"role": Role.ESTIMATING_ADMIN},                                 # the same role: a no-op
    {"body": AdminUpdateUserIn(full_name="Pat Rivera")},
    {"body": AdminUpdateUserIn(is_active=True)},                     # reactivation does nothing
])
def test_a_no_op_role_patch_a_rename_and_a_reactivation_run_nothing(monkeypatch, calls, kw):
    active = "body" not in kw or kw["body"].is_active is not True
    _store(monkeypatch, calls, _profile("u1", is_active=active))
    users.update_user(user_id="u1", admin=_ADMIN, **kw)
    assert calls.log == []


def test_deactivating_and_demoting_at_once_is_one_deactivation(monkeypatch, calls):
    _store(monkeypatch, calls, _profile("u1"))
    users.update_user(user_id="u1", body=AdminUpdateUserIn(is_active=False, role=Role.ESTIMATOR), admin=_ADMIN)
    assert _modes(calls) == [("revoke", "deactivated"), ("offboard", "deactivated")]


def test_an_offboarding_error_never_blocks_the_account_change(monkeypatch, calls):
    store = _store(monkeypatch, calls, _profile("u1"))

    def boom(*a, **k):
        raise RuntimeError("assistant tables missing")

    monkeypatch.setattr(assistant_offboarding, "offboard", boom)
    monkeypatch.setattr(assistant_offboarding, "revoke_delegation", boom)
    out = users.update_user(user_id="u1", body=AdminUpdateUserIn(is_active=False), admin=_ADMIN)
    assert out["is_active"] is False and store["profiles"]["u1"]["is_active"] is False


# ── delete_user ──────────────────────────────────────────────────────────────


def test_delete_offboards_before_the_auth_delete(monkeypatch, calls):
    store = _store(monkeypatch, calls, _profile("u1"))
    users.delete_user(user_id="u1", admin=_ADMIN)
    assert _modes(calls) == [("offboard", "deleted")]
    assert calls.log[0]["auth_deleted"] == [] and store["auth_deleted"] == ["u1"]


def test_delete_is_refused_under_legal_hold(monkeypatch, calls):
    store = _store(monkeypatch, calls, _profile("u1"))
    calls.state["held"] = True
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="u1", admin=_ADMIN)
    assert ei.value.status_code == 409 and ei.value.headers["X-Error-Code"] == "legal_hold"
    assert calls.log == [] and store["auth_deleted"] == []


def test_delete_is_refused_when_a_critical_step_failed(monkeypatch, calls):
    store = _store(monkeypatch, calls, _profile("u1"))
    calls.state["ok"] = False
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="u1", admin=_ADMIN)
    assert ei.value.status_code == 502 and ei.value.headers["X-Error-Code"] == "offboarding_failed"
    assert store["auth_deleted"] == [] and "u1" in store["profiles"]
    assert EM_DASH not in str(ei.value.detail)


def test_delete_is_refused_when_offboarding_could_not_run(monkeypatch, calls):
    store = _store(monkeypatch, calls, _profile("u1"))
    monkeypatch.setattr(assistant_offboarding, "offboard", lambda *a, **k: (_ for _ in ()).throw(ImportError()))
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="u1", admin=_ADMIN)
    assert ei.value.status_code == 502 and store["auth_deleted"] == []


# ── Paths that call nothing ──────────────────────────────────────────────────


def test_the_dev_role_switch_calls_nothing(monkeypatch, calls):
    _store(monkeypatch, calls, _profile("u1", is_dev=True))
    me = SimpleNamespace(id="u1", is_dev=True)
    users.switch_own_role(body=RoleSwitchIn(role=Role.ESTIMATOR), user=me)
    assert calls.log == []


def test_with_the_assistant_off_only_deactivation_and_role_changes_wait_for_the_sweep(monkeypatch, calls):
    store = _store(monkeypatch, calls, _profile("u1"), _profile("u2"))
    monkeypatch.setattr(users, "get_settings", lambda: SimpleNamespace(assistant_enabled=False))
    users.update_user(user_id="u1", body=AdminUpdateUserIn(is_active=False), admin=_ADMIN)
    users.update_user(user_id="u2", role=Role.ACCOUNTANT, admin=_ADMIN)
    assert calls.log == [] and store["auth_deleted"] == []


def test_an_mfa_reset_offboards_while_the_assistant_is_off(monkeypatch, calls):
    """SEC-3 / OFB-3: the orphan sweep never catches an MFA reset (the
    person stays active and internal), so the old grant and cards go now."""
    _store(monkeypatch, calls, _profile("u1"))
    monkeypatch.setattr(users, "get_settings", lambda: SimpleNamespace(assistant_enabled=False))
    users.reset_user_mfa(user_id="u1", admin=_ADMIN)
    users.reset_my_mfa(user=SimpleNamespace(id="u1", impersonated_by=None))
    assert _modes(calls) == [("offboard", "mfa_reset"), ("offboard", "mfa_reset")]


def test_a_held_delete_is_refused_while_the_assistant_is_off(monkeypatch, calls):
    """OFB-1: the cascade would destroy the held data, so the flag never
    skips the legal hold check."""
    store = _store(monkeypatch, calls, _profile("u2"))
    monkeypatch.setattr(users, "get_settings", lambda: SimpleNamespace(assistant_enabled=False))
    calls.state["held"] = True
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="u2", admin=_ADMIN)
    assert ei.value.status_code == 409 and ei.value.headers["X-Error-Code"] == "legal_hold"
    assert calls.log == [] and store["auth_deleted"] == []


def test_a_delete_offboards_while_the_assistant_is_off(monkeypatch, calls):
    store = _store(monkeypatch, calls, _profile("u2"))
    monkeypatch.setattr(users, "get_settings", lambda: SimpleNamespace(assistant_enabled=False))
    users.delete_user(user_id="u2", admin=_ADMIN)
    assert _modes(calls) == [("offboard", "deleted")] and store["auth_deleted"] == ["u2"]
    calls.state["ok"] = False
    store = _store(monkeypatch, calls, _profile("u3"))
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="u3", admin=_ADMIN)
    assert ei.value.status_code == 502 and store["auth_deleted"] == []


class _MissingTable(Exception):
    code = "PGRST205"


def test_a_database_without_assistant_tables_skips_the_step(monkeypatch, calls):
    """No assistant tables (an environment before 0170): nothing to offboard,
    and neither a delete nor an MFA reset is blocked."""
    store = _store(monkeypatch, calls, _profile("u1"), _profile("u2"))
    monkeypatch.setattr(users, "get_settings", lambda: SimpleNamespace(assistant_enabled=False))
    calls.state["held"] = _MissingTable("Could not find the table 'public.assistant_profiles'")
    users.reset_user_mfa(user_id="u1", admin=_ADMIN)
    users.delete_user(user_id="u2", admin=_ADMIN)
    assert calls.log == [] and store["auth_deleted"] == ["u2"]


def test_a_failed_legal_hold_read_refuses_the_delete(monkeypatch, calls):
    store = _store(monkeypatch, calls, _profile("u2"))
    monkeypatch.setattr(users, "get_settings", lambda: SimpleNamespace(assistant_enabled=False))
    calls.state["held"] = RuntimeError("connection reset")
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="u2", admin=_ADMIN)
    assert ei.value.status_code == 502 and ei.value.headers["X-Error-Code"] == "offboarding_failed"
    assert store["auth_deleted"] == []


def test_browser_revoke_failure_blocks_mfa_factor_deletion(monkeypatch, calls):
    from app.services import assistant_browser
    _store(monkeypatch, calls, _profile('u1', mfa_enrolled=True))
    monkeypatch.setattr(assistant_browser, 'revoke_before_mfa_reset',
                        lambda user_id: (_ for _ in ()).throw(RuntimeError('unavailable')))
    monkeypatch.setattr(users, '_delete_user_factors', lambda user_id: pytest.fail('must revoke before factors'))
    with pytest.raises(HTTPException) as exc:
        users.reset_user_mfa(user_id='u1', admin=_ADMIN)
    assert exc.value.status_code == 502
    assert calls.state['store']['profiles']['u1']['mfa_enrolled'] is True
