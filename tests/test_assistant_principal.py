"""The bdra_ assistant principal (app/assistant/principal.py) and the actor
provenance contextvar (app/core/provenance.py).

Invariants pinned here (docs/ASSISTANT.md, Architecture > Principal):

  token        bdra_ + HMAC-SHA256 over the payload, 5 minute TTL, bound to
               (user_id, run_id, approval_id); a tampered, expired, foreign
               or unsigned token never verifies; no secret, no minting.
  network      a valid bdra_ token sent through TestClient(app.main.app)
               is refused 401: the scope has no bdr_internal marker.
  principal    the owner's live profile is re-read; inactive, external and
               unknown profiles are refused; a revoked or missing delegation
               is refused; is_dev is forced False; the impersonation header
               is ignored; the token must match the provenance contextvar.
  provenance   fields() gives the 0172 columns inside a run and {} outside;
               display_actor derives
               "<name>'s assistant" at render time.

No network: the Supabase client is a chainable in-memory fake and the
profile loader is a dict lookup.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.assistant import access, principal
from app.core import deps, provenance, supabase_client
from app.core.roles import Role

SECRET = "unit-test-assistant-token-secret-0123456789abcdef"
USER = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
APPROVAL = "33333333-3333-4333-8333-333333333333"
# The real clock: resolve_bdra_principal verifies expiry against now(),
# so a token minted here must still be live when the principal reads it.
NOW = datetime.now(timezone.utc)


# ── fakes ────────────────────────────────────────────────────────────────────


class _Query:
    """The slice of the sync Supabase builder principal.active_delegation
    uses: select / eq / is_ / or_ / limit / execute."""

    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.filters = []

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def is_(self, col, val):
        assert val == "null"
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def or_(self, expr):
        assert expr == "expires_at.is.null,expires_at.gt.now()"
        self.filters.append(lambda r: r.get("expires_at") is None or r["expires_at"] > self.db.now)
        return self

    def limit(self, n):
        return self

    def execute(self):
        self.db.calls.append(self.table_name)
        rows = [r for r in self.db.tables.get(self.table_name, []) if all(f(r) for f in self.filters)]
        return SimpleNamespace(data=rows)


class _FakeDB:
    def __init__(self, now=NOW, **tables):
        self.tables, self.now, self.calls = tables, now, []

    def table(self, name):
        return _Query(self, name)


def _profile(**over):
    base = {
        "id": USER, "email": "tom@g3.test", "role": "executive", "is_active": True,
        "is_dev": False, "mfa_enrolled": True, "invite_accepted_at": "2026-01-01T00:00:00+00:00",
        "rfp_mailboxes": ["Bids@G3.test ", ""],
    }
    base.update(over)
    return base


def _delegation(**over):
    base = {"id": "del-1", "user_id": USER, "created_at": "2026-10-01T00:00:00+00:00",
            "expires_at": None, "revoked_at": None}
    base.update(over)
    return base


@pytest.fixture
def settings(monkeypatch):
    s = SimpleNamespace(assistant_token_secret=SECRET, assistant_enabled=True)
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    return s


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDB(assistant_delegations=[_delegation()])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


def _loader(profile=None, **over):
    row = profile if profile is not None else _profile(**over)
    return lambda user_id: row if row.get("id") == user_id else None


def _scope(internal=True, headers=None):
    scope = {"type": "http", "method": "GET", "path": "/features",
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    if internal:
        scope[principal.SCOPE_MARKER] = True
    return scope


def _resolve(token, scope=None, loader=None):
    return principal.resolve_bdra_principal(scope if scope is not None else _scope(), token,
                                            loader or _loader())


# ── tokens ───────────────────────────────────────────────────────────────────


def test_token_round_trip_and_shape(settings):
    token = principal.mint_token(USER, RUN, APPROVAL, now=NOW)
    assert token.startswith("bdra_") and "." in token
    claims = principal.verify_token(token, now=NOW + timedelta(seconds=299))
    assert (claims.user_id, claims.run_id, claims.approval_id) == (USER, RUN, APPROVAL)
    assert claims.expires_at - claims.issued_at == principal.DEFAULT_TTL_SECONDS
    plain = principal.verify_token(principal.mint_token(USER, RUN, now=NOW), now=NOW)
    assert plain.approval_id is None


def test_token_expires_and_ttl_is_capped(settings):
    token = principal.mint_token(USER, RUN, ttl=60, now=NOW)
    assert principal.verify_token(token, now=NOW + timedelta(seconds=59))
    with pytest.raises(principal.TokenError):
        principal.verify_token(token, now=NOW + timedelta(seconds=60))
    with pytest.raises(ValueError):
        principal.mint_token(USER, RUN, ttl=principal.MAX_TTL_SECONDS + 1)
    with pytest.raises(ValueError):
        principal.mint_token(USER, RUN, ttl=0)


def test_tampered_or_foreign_tokens_never_verify(settings):
    token = principal.mint_token(USER, RUN, now=NOW)
    payload, sig = token[len("bdra_"):].split(".")
    # Payload swapped for another user's with the old signature.
    other = principal.mint_token("9" * 36, RUN, now=NOW)[len("bdra_"):].split(".")[0]
    for bad in (
        f"bdra_{other}.{sig}",
        f"bdra_{payload}.{sig[:-2]}AA",
        f"bdra_{payload}",
        "bdra_",
        "bdrw_" + "x" * 43,
        token + "x",
        "",
    ):
        with pytest.raises(principal.TokenError):
            principal.verify_token(bad, now=NOW)
    # A different secret: same shape, no trust.
    with pytest.raises(principal.TokenError):
        principal.verify_token(token, settings=SimpleNamespace(assistant_token_secret="b" * 40), now=NOW)


def test_no_or_short_secret_refuses_minting_and_verifying(settings):
    for bad in ("", "short", None):
        s = SimpleNamespace(assistant_token_secret=bad)
        with pytest.raises(principal.TokenError):
            principal.mint_token(USER, RUN, settings=s)
    token = principal.mint_token(USER, RUN, now=NOW)
    with pytest.raises(principal.TokenError):
        principal.verify_token(token, settings=SimpleNamespace(), now=NOW)


# ── the principal ────────────────────────────────────────────────────────────


def test_valid_token_resolves_the_owner_with_assistant_markers(settings, db):
    # Approval-free (a turn token): an approval-bound token also re-reads
    # its approval row (tests/test_assistant_principal_approval.py).
    token = principal.mint_token(USER, RUN, None, now=NOW)
    with provenance.acting_as_assistant(USER, RUN):
        user = _resolve(token)
    assert isinstance(user, deps.CurrentUser)
    assert user.id == USER and user.email == "tom@g3.test" and user.role is Role.EXECUTIVE
    assert user.is_active and user.aal == "aal2" and user.mfa_enrolled
    assert user.impersonated_by is None
    assert user.rfp_mailboxes == ("bids@g3.test",)
    assert user.via_assistant_run_id == RUN
    assert db.calls == ["assistant_delegations"]


def test_is_dev_is_forced_false(settings, db):
    token = principal.mint_token(USER, RUN, now=NOW)
    with provenance.acting_as_assistant(USER, RUN):
        user = _resolve(token, loader=_loader(is_dev=True, role="it_admin"))
    assert user.is_dev is False and user.role is Role.IT_ADMIN


def test_impersonation_header_is_ignored(settings, db):
    token = principal.mint_token(USER, RUN, now=NOW)
    scope = _scope(headers={deps.IMPERSONATE_HEADER: "est-1"})
    with provenance.acting_as_assistant(USER, RUN):
        user = _resolve(token, scope=scope, loader=_loader(is_dev=True))
    assert user.id == USER and user.impersonated_by is None and user.is_dev is False


def test_refused_without_the_internal_scope_marker(settings, db):
    token = principal.mint_token(USER, RUN, now=NOW)
    with provenance.acting_as_assistant(USER, RUN):
        for scope in (_scope(internal=False), {**_scope(), principal.SCOPE_MARKER: "true"},
                      {**_scope(), principal.SCOPE_MARKER: 1}):
            with pytest.raises(HTTPException) as exc:
                _resolve(token, scope=scope)
            assert exc.value.status_code == 401
        # No scope at all (called directly: _resolve would substitute the
        # default internal scope for None).
        with pytest.raises(HTTPException) as exc:
            principal.resolve_bdra_principal(None, token, _loader())
        assert exc.value.status_code == 401
    assert db.calls == []  # refused before any lookup


def test_contextvar_mismatch_is_refused(settings, db):
    token = principal.mint_token(USER, RUN, now=NOW)
    # No run entered at all.
    with pytest.raises(HTTPException) as exc:
        _resolve(token)
    assert exc.value.status_code == 401
    # Another run, another user, or an approval the token does not carry.
    for actor in ((USER, "other-run", None), ("other-user", RUN, None), (USER, RUN, APPROVAL)):
        with provenance.acting_as_assistant(*actor):
            with pytest.raises(HTTPException) as exc:
                _resolve(token)
            assert exc.value.status_code == 401
    assert db.calls == []


def test_revoked_or_missing_delegation_is_refused(settings, db):
    token = principal.mint_token(USER, RUN, now=NOW)
    db.tables["assistant_delegations"] = [_delegation(revoked_at="2026-10-20T00:00:00+00:00")]
    with provenance.acting_as_assistant(USER, RUN):
        with pytest.raises(HTTPException) as exc:
            _resolve(token)
        assert exc.value.status_code == 403 and exc.value.detail == "assistant_delegation_missing"
        db.tables["assistant_delegations"] = [_delegation(expires_at=NOW - timedelta(days=1))]
        with pytest.raises(HTTPException) as exc:
            _resolve(token)
        assert exc.value.status_code == 403
        db.tables["assistant_delegations"] = [_delegation(user_id="someone-else")]
        with pytest.raises(HTTPException) as exc:
            _resolve(token)
        assert exc.value.status_code == 403
        db.tables["assistant_delegations"] = [_delegation(expires_at=NOW + timedelta(days=1))]
        assert _resolve(token).id == USER


def test_live_profile_gates(settings, db):
    token = principal.mint_token(USER, RUN, now=NOW)
    with provenance.acting_as_assistant(USER, RUN):
        with pytest.raises(HTTPException) as exc:
            _resolve(token, loader=_loader(is_active=False))
        assert exc.value.status_code == 403 and "disabled" in exc.value.detail
        with pytest.raises(HTTPException) as exc:
            _resolve(token, loader=_loader(role="estimator"))
        assert exc.value.status_code == 403 and exc.value.detail == "Not permitted"
        with pytest.raises(HTTPException) as exc:
            _resolve(token, loader=_loader(role="not-a-role"))
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            _resolve(token, loader=lambda _uid: None)
        assert exc.value.status_code == 403
    assert db.calls == []  # every refusal above happens before the delegation read


def test_expired_token_is_refused_even_inside_a_run(settings, db, monkeypatch):
    token = principal.mint_token(USER, RUN, ttl=5, now=NOW - timedelta(minutes=10))
    with provenance.acting_as_assistant(USER, RUN):
        with pytest.raises(HTTPException) as exc:
            _resolve(token)
        assert exc.value.status_code == 401


# ── over the network ─────────────────────────────────────────────────────────


def test_valid_token_over_the_network_is_refused_401(settings, db, monkeypatch):
    """A bdra_ token is only ever a bearer inside the shim. Sent through the
    served app, the scope carries no bdr_internal marker, so it is refused
    like a bdrw_ token: 401 and nothing read. Holds today (the token is not
    a JWT) and after the deps.py hook lands (the marker is absent)."""
    import app.main

    monkeypatch.setattr(deps, "get_supabase", lambda: db)
    token = principal.mint_token(USER, RUN, now=NOW)
    client = TestClient(app.main.app, raise_server_exceptions=True)
    with provenance.acting_as_assistant(USER, RUN):
        for path in ("/features", "/users/me", "/projects"):
            resp = client.get(path, headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401, (path, resp.text)
            assert resp.json()["detail"] == "Invalid or expired token"
    assert db.calls == []


def test_hook_hands_bdra_tokens_to_the_principal(settings, db, monkeypatch):
    """Once core/deps.get_current_user carries the hook from
    app/assistant/INTEGRATION_TODO.md, a bdra_ bearer with the marker set
    resolves through this module."""
    source = inspect.getsource(deps.get_current_user)
    if "bdra_" not in source or "resolve_bdra_principal" not in source:
        pytest.skip("deps.get_current_user has no bdra_ hook yet (INTEGRATION_TODO.md)")
    monkeypatch.setattr(deps, "get_supabase", lambda: _FakeDB(profiles=[_profile()]))
    token = principal.mint_token(USER, RUN, now=NOW)
    request = SimpleNamespace(scope=_scope(), headers={}, method="GET",
                              url=SimpleNamespace(path="/features"))
    with provenance.acting_as_assistant(USER, RUN):
        user = deps.get_current_user(request, authorization=f"Bearer {token}")
    assert user.id == USER and user.via_assistant_run_id == RUN and user.is_dev is False


# ── provenance ───────────────────────────────────────────────────────────────


def test_provenance_fields_follow_the_context():
    assert provenance.current() is None and not provenance.is_assistant()
    assert provenance.fields() == {}
    with provenance.acting_as_assistant(USER, RUN, APPROVAL) as actor:
        assert actor == provenance.AssistantActor(USER, RUN, APPROVAL)
        assert provenance.is_assistant()
        assert provenance.fields() == {"actor_kind": "assistant", "assistant_run_id": RUN}
        with pytest.raises(RuntimeError):
            with provenance.acting_as_assistant(USER, "another-run"):
                pass
    assert provenance.current() is None
    with pytest.raises(ValueError):
        with provenance.acting_as_assistant("", RUN):
            pass


def test_provenance_resets_after_an_error():
    with pytest.raises(KeyError):
        with provenance.acting_as_assistant(USER, RUN):
            raise KeyError("boom")
    assert provenance.fields() == {}


def test_display_actor_is_derived_not_stored():
    assert provenance.display_actor("Tom Moore", "assistant") == "Tom Moore's assistant"
    assert provenance.display_actor("Tom Moore", "human") == "Tom Moore"
    assert provenance.display_actor("Tom Moore") == "Tom Moore"
    assert provenance.display_actor("Tom Moore", "system") == "Tom Moore"
    assert provenance.display_actor(None, "assistant") == "Unknown user's assistant"
    assert provenance.display_actor("  ", "human") == "Unknown user"
    assert provenance.ACTOR_KINDS == {"human", "assistant", "system"}


# ── kill switches ────────────────────────────────────────────────────────────


def _user(role="executive", is_active=True):
    return SimpleNamespace(role=role, is_active=is_active)


def test_assistant_enabled_for_needs_every_switch_on():
    on = {"enabled": True}
    assert access.assistant_enabled_for(_user(), on, {"enabled": True}, env=True)
    # A missing profile row counts as enabled (lazy row, column default true).
    assert access.assistant_enabled_for(_user(), on, None, env=True)
    assert access.assistant_enabled_for(_user(), on, {"enabled": None}, env=True)
    # Any one switch off: no assistant.
    assert not access.assistant_enabled_for(_user(), on, {"enabled": True}, env=False)
    assert not access.assistant_enabled_for(_user(), {"enabled": False}, None, env=True)
    assert not access.assistant_enabled_for(_user(), None, None, env=True)  # no settings row
    assert not access.assistant_enabled_for(_user(), {"enabled": None}, None, env=True)
    assert not access.assistant_enabled_for(_user(), on, {"enabled": False}, env=True)
    # The person: inactive, external, unknown role, or no user at all.
    assert not access.assistant_enabled_for(_user(is_active=False), on, None, env=True)
    assert not access.assistant_enabled_for(_user(role="estimator"), on, None, env=True)
    assert not access.assistant_enabled_for(_user(role="nope"), on, None, env=True)
    assert not access.assistant_enabled_for(None, on, None, env=True)
    # Every internal role, the Accountant included, may have one.
    for role in ("executive", "it_admin", "estimating_admin", "accountant"):
        assert access.assistant_enabled_for(_user(role=role), on, None, env=True), role


def test_assistant_enabled_for_reads_the_env_switch_fail_closed(monkeypatch):
    monkeypatch.setattr(access, "get_settings", lambda: SimpleNamespace())
    assert not access.env_enabled()
    assert not access.assistant_enabled_for(_user(), {"enabled": True}, None)
    monkeypatch.setattr(access, "get_settings", lambda: SimpleNamespace(assistant_enabled=True))
    assert access.assistant_enabled_for(_user(), {"enabled": True}, None)


def test_disabled_tools_hide_single_tools():
    row = {"enabled": True, "disabled_tools": ["send_email", " read_mailbox ", ""]}
    assert access.disabled_tools(row) == {"send_email", "read_mailbox"}
    assert not access.tool_enabled(row, "send_email")
    assert access.tool_enabled(row, "list_projects")
    assert access.disabled_tools(None) == frozenset()
    assert access.tool_enabled({"enabled": True}, "anything")
