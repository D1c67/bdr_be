"""Security review, group buildingconnected (findings 16, 17, 18).

- 16: RFP_BC_EXPECTED_COMPANY_ID pins the OAuth callback to one Autodesk
  company (audited refusal, nothing stored), and a connect that replaces a
  different Autodesk user or company rings IT Admins.
- 17: a manual disconnect revokes the refresh and access tokens at Autodesk
  (best effort) before clearing them; the automatic disconnects do not.
- 18: the unauthenticated callback has a per-process limiter and refuses a
  state that is not token_urlsafe(32) shaped before any DB call.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import secrets

import httpx
import pytest
from fastapi import HTTPException

from app.routers import rfp_bc as rb
from app.services import bc_client
from tests.test_rfp_bc_router import ACCESS, REFRESH, S1, SECRET, _FakeBc, _settings, _user


def _query(resp):
    assert resp.status_code == 302
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}


@pytest.fixture
def audits(monkeypatch):
    out = []
    monkeypatch.setattr(rb, "get_settings", lambda: _settings())
    monkeypatch.setattr(rb, "_audit", lambda actor, action, payload=None: out.append((actor, action, payload)))
    rb._callback_hits.clear()
    yield out
    rb._callback_hits.clear()


@pytest.fixture
def bc(monkeypatch, audits):
    fake = _FakeBc()
    monkeypatch.setattr(rb, "_bc_client", lambda: fake)
    monkeypatch.setattr(rb, "get_supabase", lambda: "sb")
    return fake


# ── 18: limiter and state shape ──────────────────────────────────────────


def test_malformed_state_is_refused_before_any_db_call(bc, monkeypatch):
    def _no_db():
        raise AssertionError("the DB must not be reached for a malformed state")

    monkeypatch.setattr(rb, "get_supabase", _no_db)
    for bad in ("x", "S1", S1 + "a", S1[:-1] + "!", S1[:-1] + "=", "a" * 400):
        assert _query(rb.oauth_callback(code="C1", state=bad, error=None)) == {
            "bc": "error", "reason": "rfp_bc_state_invalid"}
    assert bc.calls == []


def test_consume_state_does_not_query_for_a_malformed_state():
    class _Boom:
        def table(self, name):
            raise AssertionError("no query for a malformed state")

    assert bc_client.consume_state(_Boom(), "junk") is None
    assert bc_client.state_well_formed(secrets.token_urlsafe(32))
    assert not bc_client.state_well_formed("junk")


def test_the_callback_limiter_trips_after_the_limit(bc, monkeypatch):
    monkeypatch.setattr(rb, "get_settings", lambda: _settings(rfp_bc_callback_rate_limit_per_min=3))
    for _ in range(3):
        rb.oauth_callback(code="C1", state="junk", error=None)
    with pytest.raises(HTTPException) as exc:
        rb.oauth_callback(code="C1", state=S1, error=None)
    assert exc.value.status_code == 429
    assert exc.value.headers["X-RateLimit-Scope"] == "rfp_bc_callback"
    assert int(exc.value.headers["Retry-After"]) >= 1
    assert bc.calls == []  # the limited hit never reached consume_state


def test_the_callback_limiter_honours_the_master_switch(bc, monkeypatch):
    monkeypatch.setattr(rb, "get_settings", lambda: _settings(
        rfp_bc_callback_rate_limit_per_min=1, rate_limit_enabled=False))
    for _ in range(5):
        rb.oauth_callback(code="C1", state="junk", error=None)


# ── 16: company pin ──────────────────────────────────────────────────────


def test_expected_company_refuses_another_company_and_stores_nothing(bc, audits, monkeypatch):
    monkeypatch.setattr(rb, "get_settings", lambda: _settings(rfp_bc_expected_company_id="g3-co"))
    bc.me = {"id": "other-user", "companyId": "other-co", "bidBoardPermissions": {"viewAll": True}}
    assert _query(rb.oauth_callback(code="C1", state=S1, error=None)) == {
        "bc": "error", "reason": "rfp_bc_not_connected"}
    assert "store_connection" not in [c[0] for c in bc.calls]
    assert audits == [("u-admin", "rfp_bc.connect_refused", {
        "reason": "company_mismatch", "external_user_id": "other-user", "external_company_id": "other-co"})]
    # No companyId at all is refused too.
    bc.me = {"id": "x", "bidBoardPermissions": {"viewAll": True}}
    assert _query(rb.oauth_callback(code="C1", state=S1, error=None))["reason"] == "rfp_bc_not_connected"


def test_expected_company_accepts_the_matching_company(bc, audits, monkeypatch):
    monkeypatch.setattr(rb, "get_settings", lambda: _settings(rfp_bc_expected_company_id="g3-co"))
    bc.me = {"id": "bc-user", "companyId": "g3-co", "bidBoardPermissions": {"viewAll": True}}
    assert _query(rb.oauth_callback(code="C1", state=S1, error=None)) == {"bc": "connected"}
    assert audits[-1] == ("u-admin", "rfp_bc.connect", {"external_user_id": "bc-user", "external_company_id": "g3-co"})


def _fake_db(rows):
    from tests.test_rfp_email_ingest import FakeDB

    return FakeDB({"rfp_oauth_connections": rows})


def _bells(monkeypatch):
    bells = []
    monkeypatch.setattr(bc_client, "notify_role", lambda role, project_id, type_, message, **kw: bells.append(
        (role, type_, kw.get("metadata"))))
    return bells


TOKENS = {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599}
ME = {"id": "u-new", "email": "new@x.test", "companyId": "co-new", "bidBoardPermissions": {"viewAll": True}}


def test_store_connection_rings_it_admins_when_the_account_changes(monkeypatch):
    from app.core.roles import Role

    bells = _bells(monkeypatch)
    sb = _fake_db([{"provider": "buildingconnected", "status": "disconnected",
                    "external_user_id": "u-old", "external_company_id": "co-old",
                    "external_user_email": "old@g3.test"}])
    bc_client.store_connection(sb, TOKENS, ME, "actor")
    assert len(bells) == 1
    role, type_, metadata = bells[0]
    assert role == Role.IT_ADMIN and type_ == bc_client.NOTIFY_ACCOUNT_CHANGED
    assert metadata["previous_company_id"] == "co-old" and metadata["company_id"] == "co-new"
    assert ACCESS not in str(bells) and REFRESH not in str(bells)


def test_store_connection_is_quiet_on_first_connect_and_same_account(monkeypatch):
    bells = _bells(monkeypatch)
    bc_client.store_connection(_fake_db([]), TOKENS, ME, "actor")
    same = _fake_db([{"provider": "buildingconnected", "status": "connected",
                      "external_user_id": "u-new", "external_company_id": "co-new"}])
    bc_client.store_connection(same, TOKENS, ME, "actor")
    assert bells == []


# ── 17: revoke on disconnect ─────────────────────────────────────────────


def _config():
    return bc_client.config_from_settings(_settings())


def _connected_db():
    return _fake_db([{"provider": "buildingconnected", "status": "connected",
                      "access_token": ACCESS, "refresh_token": REFRESH}])


def test_manual_disconnect_revokes_both_tokens_then_clears(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, parse_qs(request.content.decode()), request.headers["authorization"]))
        return httpx.Response(200)

    sb = _connected_db()
    bc_client.disconnect(sb, config=_config(), transport=httpx.MockTransport(handler))
    assert [s[0] for s in seen] == ["/authentication/v2/revoke"] * 2
    assert seen[0][1] == {"token": [REFRESH], "token_type_hint": ["refresh_token"]}
    assert seen[1][1] == {"token": [ACCESS], "token_type_hint": ["access_token"]}
    assert all(s[2].startswith("Basic ") for s in seen)
    (row,) = sb.tables["rfp_oauth_connections"]
    assert row["status"] == "disconnected" and row["refresh_token"] is None and row["access_token"] is None


def test_a_failed_revoke_still_clears_the_row(monkeypatch):
    for handler in (lambda r: httpx.Response(500), lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))):
        sb = _connected_db()
        bc_client.disconnect(sb, config=_config(), transport=httpx.MockTransport(handler))
        (row,) = sb.tables["rfp_oauth_connections"]
        assert row["status"] == "disconnected" and row["refresh_token"] is None


def test_automatic_disconnect_makes_no_revoke_call(monkeypatch):
    def _no_client(*a, **kw):
        raise AssertionError("no network call without a config")

    monkeypatch.setattr(bc_client.httpx, "Client", _no_client)
    sb = _connected_db()
    bc_client.disconnect(sb, error="refresh rejected")
    assert sb.tables["rfp_oauth_connections"][0]["status"] == "disconnected"


def test_the_router_disconnect_passes_the_config(bc, audits):
    rb.disconnect(_user(uid="u9"))
    assert bc.calls == [("disconnect", None, True)]
    assert SECRET not in str(audits)
