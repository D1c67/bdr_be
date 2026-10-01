"""The BuildingConnected router (app/routers/rfp_bc) called as plain
functions with the services stubbed (docs/RFP_BUILDINGCONNECTED.md sections
4.3 and 6; build contract D4, D5, D8, D30, section 4).

Pinned:

- The route table of both routers, exactly; every main route carries the
  feature gate, its D8 role dependency and the catch-all rate limit; the
  callback carries the feature gate ONLY (no auth, no rate limit anywhere in
  its dependency tree: the browser returns from Autodesk without a token).
- The switch: RFP_INGESTION_ENABLED and RFP_BC_ENABLED, fail closed, the
  bare 404 while off.
- Roles per route (403 for everyone else).
- connect / disconnect / status / run / gc-aliases delegate with the right
  arguments and map the refusals.
- The callback flow with a fake bc_client (state missing, unknown, expired,
  valid; Autodesk error; exchange failure; viewAll false; storage failure;
  success) and once over the real bc_client state table (single use).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.core.deps import CurrentUser, get_current_user
from app.core.error_codes import ErrorCode
from app.core.roles import RFP_REVIEW_ROLES, Role
from app.routers import rfp_bc as rb
from app.routers import rfp_portal as rp
from app.services.rfp_portal_ingest import RfpPortalError

A1 = "7a000000-0000-4000-8000-000000000001"
MISSING = "7a000000-0000-4000-8000-0000000000ff"
GC1 = "6c000000-0000-4000-8000-000000000001"
FRONTEND = "https://bdr.example.test/"
SECRET = "top-secret-client-secret"
ACCESS = "ACCESS-TOKEN-XYZ"
REFRESH = "REFRESH-TOKEN-XYZ"
# A state shaped like bc_client.new_state mints (token_urlsafe(32), 43 chars).
S1 = "S1" + "a" * 41
OLD = "OLD" + "b" * 40


def _settings(**over):
    base = dict(
        _env_file=None, rfp_ingest_enabled=True, rfp_bc_enabled=True,
        building_connected_client_id="client-id", building_connected_client_secret=SECRET,
        rfp_bc_redirect_url="http://localhost:5051/rfp-portal/buildingconnected/callback",
        frontend_url=FRONTEND,
    )
    base.update(over)
    return Settings(**base)


def _user(role=Role.IT_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


# ── Fakes ────────────────────────────────────────────────────────────────


class _FakeBc:
    """Stand-in for app/services/bc_client: records the calls."""

    class BcPermanent(Exception):
        status = 400

    def __init__(self):
        self.calls = []
        self.state_row = {"state": S1, "actor_id": "u-admin", "provider": "buildingconnected"}
        self.exchange_error = None
        self.me = {"id": "bc-user", "email": "pat@g3electrical.com", "bidBoardPermissions": {"viewAll": True}}
        self.store_error = None

    def config_from_settings(self, settings):
        from app.services import bc_client

        return bc_client.config_from_settings(settings)

    def new_state(self, sb, actor_id, ttl_seconds=600):
        self.calls.append(("new_state", actor_id))
        return S1

    def authorize_url(self, config, state, scope="data:read"):
        self.calls.append(("authorize_url", state))
        return f"https://developer.api.autodesk.com/authentication/v2/authorize?state={state}"

    def consume_state(self, sb, state):
        self.calls.append(("consume_state", state))
        return copy.deepcopy(self.state_row) if state == S1 and self.state_row else None

    def exchange_code(self, config, code, *, transport=None):
        self.calls.append(("exchange_code", code))
        if self.exchange_error is not None:
            raise self.exchange_error
        return {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599}

    def fetch_me(self, access_token, config, *, transport=None):
        self.calls.append(("fetch_me", access_token == ACCESS))
        return copy.deepcopy(self.me)

    def view_all(self, me):
        from app.services import bc_client

        return bc_client.view_all(me)

    def store_connection(self, sb, tokens, me, actor_id):
        self.calls.append(("store_connection", tokens.get("refresh_token") == REFRESH, me.get("id"), actor_id))
        if self.store_error is not None:
            raise self.store_error
        return {"provider": "buildingconnected", "status": "connected"}

    def disconnect(self, sb, *, error=None, config=None, transport=None):
        self.calls.append(("disconnect", error, config is not None and config.client_secret == SECRET))


class _FakeBcPortal:
    def __init__(self):
        self.calls = []

    def status(self, sb, settings=None):
        self.calls.append(("status",))
        return {"connection": {"status": "connected", "external_user_name": "Pat"}, "configured": True,
                "counts": {"needs_action": 2}}


class _PortalError(RfpPortalError):
    def __init__(self, code, message="refused", http_status=None):
        super().__init__(code, message, http_status=http_status)


class _FakeIngest:
    PORTAL_BUILDINGCONNECTED = "buildingconnected"
    RfpPortalError = RfpPortalError

    def __init__(self):
        self.calls = []
        self.raise_error = None

    def run_now(self, sb, actor_id, portal="ngem", kind="incremental"):
        self.calls.append(("run_now", actor_id, portal, kind))
        if self.raise_error is not None:
            raise self.raise_error
        return {"id": "run-1", "status": "queued", "trigger": "manual", "kind": kind, "requested_by": actor_id}

    def _profile_names(self, sb, ids):
        return {i: "Pat Admin" for i in ids if i}

    def _run_view(self, run, names):
        from app.services import rfp_portal_ingest

        return rfp_portal_ingest._run_view(run, names)


PROPAGATED = {"updated": 2, "rematched": 1, "flagged_projects": ["p-1"]}


class _FakeAliases:
    SOURCE_BC = "buildingconnected"

    class AliasNotFound(LookupError):
        pass

    class GcNotFound(LookupError):
        pass

    def __init__(self):
        self.calls = []
        self.items = [{"id": A1, "source": "buildingconnected", "external_id": "c-1", "external_name": "Acme",
                       "gc": {"id": GC1, "name": "Acme GC"}, "confirmed_by": {"id": "u1", "name": "Pat"},
                       "confirmed_at": "2026-09-26T10:00:00+00:00"}]

    def list_aliases(self, sb, source):
        self.calls.append(("list", source))
        return copy.deepcopy(self.items)

    def repoint(self, sb, alias_id, gc_id, actor_id):
        self.calls.append(("repoint", alias_id, gc_id, actor_id))
        if alias_id != A1:
            raise self.AliasNotFound(alias_id)
        if gc_id != GC1:
            raise self.GcNotFound(gc_id)
        return {"id": alias_id, "gc_id": gc_id, "propagated": PROPAGATED}

    def delete(self, sb, alias_id):
        self.calls.append(("delete", alias_id))
        if alias_id != A1:
            raise self.AliasNotFound(alias_id)
        return PROPAGATED


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(rb, "get_settings", lambda: _settings())
    monkeypatch.setattr(rp, "get_settings", lambda: _settings())
    monkeypatch.setattr(rb, "get_supabase", lambda: "sb")
    audits = []
    monkeypatch.setattr(rb, "_audit", lambda actor, action, payload=None: audits.append((actor, action, payload)))
    rb._callback_hits.clear()  # the callback limiter is per process
    return audits


@pytest.fixture
def bc(monkeypatch):
    fake = _FakeBc()
    monkeypatch.setattr(rb, "_bc_client", lambda: fake)
    return fake


@pytest.fixture
def portal(monkeypatch):
    fake = _FakeBcPortal()
    monkeypatch.setattr(rb, "_bc_portal", lambda: fake)
    return fake


@pytest.fixture
def ingest(monkeypatch):
    fake = _FakeIngest()
    monkeypatch.setattr(rb, "_portal_ingest", lambda: fake)
    return fake


@pytest.fixture
def aliases(monkeypatch):
    fake = _FakeAliases()
    monkeypatch.setattr(rb, "_gc_aliases", lambda: fake)
    return fake


# ── Route table, gate, dependencies ──────────────────────────────────────


def _pairs(router):
    return {(m, r.path) for r in router.routes for m in r.methods}


def test_route_tables_are_exactly_the_spec():
    base = "/rfp-portal/buildingconnected"
    assert _pairs(rb.router) == {
        ("GET", f"{base}/connect"),
        ("POST", f"{base}/disconnect"),
        ("GET", f"{base}/status"),
        ("POST", f"{base}/run"),
        ("GET", f"{base}/gc-aliases"),
        ("PATCH", f"{base}/gc-aliases/{{alias_id}}"),
        ("DELETE", f"{base}/gc-aliases/{{alias_id}}"),
    }
    assert _pairs(rb.callback_router) == {("GET", f"{base}/callback")}


_ROLE_DEP = {
    "/connect": "require_bc_connect",
    "/disconnect": "require_bc_connect",
    "/status": "require_review_queue",
    "/run": "require_bc_run",
    "/gc-aliases": "require_bc_aliases",
    "/gc-aliases/{alias_id}": "require_bc_aliases",
}
_ALL_ROLE_DEPS = {"require_bc_connect", "require_bc_run", "require_bc_aliases", "require_review_queue"}


def test_every_main_route_is_gated_role_checked_and_rate_limited():
    for route in rb.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        suffix = route.path.removeprefix(rb.PREFIX)
        assert rb.require_rfp_bc in calls, route.path
        assert rp.rfp_portal_rate_limit in calls, route.path
        want = _ROLE_DEP[suffix]
        assert getattr(rb, want) in calls, route.path
        for other in _ALL_ROLE_DEPS - {want}:
            assert getattr(rb, other) not in calls, (route.path, other)


def _all_calls(dependant):
    out = set()
    for d in dependant.dependencies:
        out.add(d.call)
        out |= _all_calls(d)
    return out


def test_the_callback_carries_the_gate_only_no_auth_no_rate_limit():
    (route,) = rb.callback_router.routes
    direct = {d.call for d in route.dependant.dependencies}
    assert direct == {rb.require_rfp_bc}
    every = _all_calls(route.dependant)
    assert get_current_user not in every and rp.rfp_portal_rate_limit not in every
    for name in _ALL_ROLE_DEPS:
        assert getattr(rb, name) not in every


def test_the_app_mounts_both_routers():
    import app.main

    paths = {(m, r.path) for r in app.main.app.routes for m in getattr(r, "methods", ())}
    assert _pairs(rb.router) | _pairs(rb.callback_router) <= paths


def test_switch_needs_both_flags_and_fails_closed(monkeypatch):
    monkeypatch.setattr(rb, "get_settings", lambda: SimpleNamespace())
    assert rb.rfp_bc_enabled() is False
    with pytest.raises(HTTPException) as exc:
        rb.require_rfp_bc()
    assert exc.value.status_code == 404 and exc.value.detail == "Not Found"
    for flags in ({"rfp_ingest_enabled": True, "rfp_bc_enabled": False, "rfp_ngem_enabled": True},
                  {"rfp_ingest_enabled": False, "rfp_bc_enabled": True}):
        monkeypatch.setattr(rb, "get_settings", lambda f=flags: SimpleNamespace(**f))
        assert rb.rfp_bc_enabled() is False
        with pytest.raises(HTTPException):
            rb.require_rfp_bc()
    # On without a configured app: the status block says so.
    monkeypatch.setattr(rb, "get_settings", lambda: Settings(_env_file=None, rfp_ingest_enabled=True, rfp_bc_enabled=True))
    assert rb.rfp_bc_enabled() is True and rb.require_rfp_bc() is None


# ── Roles ────────────────────────────────────────────────────────────────


_ROLE_SETS = [
    ("require_bc_connect", {Role.IT_ADMIN, Role.EXECUTIVE}),
    ("require_bc_run", {Role.IT_ADMIN, Role.EXECUTIVE, Role.ESTIMATING_ADMIN}),
    ("require_bc_aliases", {Role.IT_ADMIN}),
    ("require_review_queue", set(RFP_REVIEW_ROLES)),
]


@pytest.mark.parametrize("dep_name, allowed", _ROLE_SETS, ids=[r[0] for r in _ROLE_SETS])
async def test_each_role_dependency_admits_exactly_its_roles(dep_name, allowed):
    dep = getattr(rb, dep_name)
    for role in Role:
        if role in allowed:
            assert (await dep(user=_user(role))).role == role
        else:
            with pytest.raises(HTTPException) as exc:
                await dep(user=_user(role))
            assert exc.value.status_code == 403, (dep_name, role)


# ── connect / disconnect / status ────────────────────────────────────────


def test_connect_answers_the_authorize_url_with_a_state_for_the_caller(bc):
    out = rb.connect(_user(Role.EXECUTIVE, uid="u-exec"))
    assert out == {"url": f"https://developer.api.autodesk.com/authentication/v2/authorize?state={S1}"}
    assert bc.calls == [("new_state", "u-exec"), ("authorize_url", S1)]
    assert SECRET not in str(out)


def test_connect_over_the_real_authorize_url_never_carries_the_secret(monkeypatch):
    from app.services import bc_client

    monkeypatch.setattr(bc_client, "new_state", lambda sb, actor_id, ttl_seconds=600: "STATE-1")
    url = rb.connect(_user())["url"]
    query = parse_qs(urlsplit(url).query)
    assert query["state"] == ["STATE-1"] and query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["http://localhost:5051/rfp-portal/buildingconnected/callback"]
    assert SECRET not in url


@pytest.mark.parametrize("blank", ["building_connected_client_id", "building_connected_client_secret",
                                   "rfp_bc_redirect_url"])
def test_connect_is_503_while_the_app_is_not_configured(bc, monkeypatch, blank):
    monkeypatch.setattr(rb, "get_settings", lambda: _settings(**{blank: ""}))
    with pytest.raises(HTTPException) as exc:
        rb.connect(_user())
    assert exc.value.status_code == 503
    assert exc.value.headers["X-Error-Code"] == "rfp_portal_not_available"
    assert bc.calls == []


def test_disconnect_clears_the_connection_and_audits(bc, _env):
    assert rb.disconnect(_user(uid="u9")) == {"status": "disconnected"}
    # The manual disconnect hands bc_client the APS config so it revokes.
    assert bc.calls == [("disconnect", None, True)]
    assert _env == [("u9", "rfp_bc.disconnect", None)]


def test_status_is_the_portal_status_block(portal):
    out = rb.bc_status(_user(Role.ESTIMATING_ENGINEER_LABOR))
    assert out["connection"]["status"] == "connected" and out["counts"]["needs_action"] == 2
    assert portal.calls == [("status",)]


# ── run ──────────────────────────────────────────────────────────────────


def test_run_defaults_to_incremental_and_passes_full_through(ingest):
    out = rb.run_now(None, user=_user(Role.ESTIMATING_ADMIN, uid="u4"))
    assert ingest.calls[-1] == ("run_now", "u4", "buildingconnected", "incremental")
    assert out["id"] == "run-1" and out["requested_by_name"] == "Pat Admin"
    out = rb.run_now(rb.RunIn(kind="full"), user=_user(uid="u4"))
    assert ingest.calls[-1] == ("run_now", "u4", "buildingconnected", "full") and out["kind"] == "full"


def test_run_kind_is_validated():
    assert rb.RunIn().kind == "incremental"
    for bad in ("nightly", "", "FULL"):
        with pytest.raises(ValidationError):
            rb.RunIn(kind=bad)


@pytest.mark.parametrize(
    "code, http_status, expected",
    [
        ("rfp_portal_run_active", None, 409),
        ("rfp_bc_not_connected", 409, 409),
        ("rfp_portal_not_available", 503, 503),
        ("rfp_portal_reason_invalid", None, 400),
    ],
)
def test_run_maps_the_service_refusals(ingest, code, http_status, expected):
    ingest.raise_error = _PortalError(code, "no", http_status=http_status)
    with pytest.raises(HTTPException) as exc:
        rb.run_now(rb.RunIn(kind="full"), user=_user())
    assert exc.value.status_code == expected and exc.value.headers["X-Error-Code"] == code


def test_run_over_the_real_service_while_disconnected_is_409_not_connected(monkeypatch):
    """Through rfp_portal_ingest.run_now and the real BuildingConnected
    adapter: an active slice with no connection row answers 409
    rfp_bc_not_connected and inserts no run."""
    from app.services import rfp_portal_ingest as real
    from tests.test_rfp_portal_ingest import PortalDB

    fake = PortalDB({"rfp_portal_runs": [], "llm_jobs": [], "rfp_oauth_connections": [], "rfp_portal_state": []})
    monkeypatch.setattr(rb, "get_supabase", lambda: fake)
    monkeypatch.setattr(real, "get_supabase", lambda: fake)
    monkeypatch.setattr(real, "get_settings", lambda: _settings(llm_queue_enabled=True))
    from app.services import rfp_bc_portal

    monkeypatch.setattr(rfp_bc_portal, "get_settings", lambda: _settings(llm_queue_enabled=True), raising=False)
    monkeypatch.setattr(rfp_bc_portal.bc_client, "connection_status", lambda sb: {"status": "disconnected"})
    monkeypatch.setattr(rfp_bc_portal.bc_client, "connection", lambda sb: None)
    with pytest.raises(HTTPException) as exc:
        rb.run_now(rb.RunIn(kind="incremental"), user=_user())
    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "rfp_bc_not_connected"
    assert fake.tables["rfp_portal_runs"] == []


# ── GC aliases ───────────────────────────────────────────────────────────


def test_alias_list_is_the_service_list_for_buildingconnected(aliases):
    out = rb.list_gc_aliases(_user())
    assert out == {"items": aliases.items} and aliases.calls == [("list", "buildingconnected")]


def test_alias_repoint_delegates_and_answers_the_list_item(aliases, _env):
    out = rb.repoint_gc_alias(A1, rb.AliasRepointIn(gc_id=GC1), user=_user(uid="u2"))
    assert aliases.calls[0] == ("repoint", A1, GC1, "u2") and out["gc"] == {"id": GC1, "name": "Acme GC"}
    assert _env[-1][1] == "rfp_bc.alias_repoint"
    # The service's follow-through onto the invitations is audited.
    assert _env[-1][2] == {"alias_id": A1, "gc_id": GC1, "propagated": PROPAGATED}


def test_alias_repoint_refusals(aliases):
    for alias_id, gc_id, http, code in (
        (MISSING, GC1, 404, None),
        ("not-a-uuid", GC1, 404, None),
        (A1, "6c000000-0000-4000-8000-0000000000ff", 400, "rfp_portal_gc_required"),
        (A1, "not-a-uuid", 400, "rfp_portal_gc_required"),
    ):
        with pytest.raises(HTTPException) as exc:
            rb.repoint_gc_alias(alias_id, rb.AliasRepointIn(gc_id=gc_id), user=_user())
        assert exc.value.status_code == http
        if code:
            assert exc.value.headers["X-Error-Code"] == code
    assert [c for c in aliases.calls if c[0] == "repoint" and c[1] == "not-a-uuid"] == []


def test_alias_delete(aliases, _env):
    assert rb.delete_gc_alias(A1, user=_user()) == {"id": A1, "deleted": True}
    assert aliases.calls == [("delete", A1)] and _env[-1][1] == "rfp_bc.alias_delete"
    assert _env[-1][2] == {"alias_id": A1, "propagated": PROPAGATED}
    for bad in (MISSING, "nope"):
        with pytest.raises(HTTPException) as exc:
            rb.delete_gc_alias(bad, user=_user())
        assert exc.value.status_code == 404


# ── The callback ─────────────────────────────────────────────────────────


def _redirect(resp):
    assert resp.status_code == 302
    loc = resp.headers["location"]
    parts = urlsplit(loc)
    return loc, {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_callback_without_a_state_is_a_400_json_answer(bc):
    for state in (None, "", "   "):
        with pytest.raises(HTTPException) as exc:
            rb.oauth_callback(code="C1", state=state, error=None)
        assert exc.value.status_code == 400
        assert exc.value.headers["X-Error-Code"] == "rfp_bc_state_invalid"
    assert bc.calls == []


def test_callback_success_stores_the_connection_and_redirects_connected(bc, _env):
    loc, query = _redirect(rb.oauth_callback(code="C1", state=S1, error=None))
    assert loc == "https://bdr.example.test/settings/rfp-ingestion?bc=connected"
    assert query == {"bc": "connected"}
    assert bc.calls == [
        ("consume_state", S1), ("exchange_code", "C1"), ("fetch_me", True),
        ("store_connection", True, "bc-user", "u-admin"),
    ]
    assert _env == [("u-admin", "rfp_bc.connect", {"external_user_id": "bc-user", "external_company_id": None})]
    assert ACCESS not in loc and REFRESH not in loc and SECRET not in loc


def test_callback_unknown_or_expired_state_redirects_state_invalid(bc):
    bc.state_row = None  # consume_state answers None for unknown AND expired
    loc, query = _redirect(rb.oauth_callback(code="C1", state=S1, error=None))
    assert loc.startswith("https://bdr.example.test/settings/rfp-ingestion?")
    assert query == {"bc": "error", "reason": "rfp_bc_state_invalid"}
    assert bc.calls == [("consume_state", S1)]
    _, query = _redirect(rb.oauth_callback(code="C1", state="other", error=None))
    assert query["reason"] == "rfp_bc_state_invalid"


def test_callback_autodesk_error_or_no_code_redirects_not_connected(bc):
    for code, error in ((None, "access_denied"), ("C1", "access_denied"), (None, None), ("  ", None)):
        bc.calls.clear()
        _, query = _redirect(rb.oauth_callback(code=code, state=S1, error=error))
        assert query == {"bc": "error", "reason": "rfp_bc_not_connected"}
        # The state is spent all the same; nothing was exchanged.
        assert bc.calls == [("consume_state", S1)]


def test_callback_exchange_failure_redirects_not_connected(bc):
    from app.services import bc_client

    for err in (bc_client.BcPermanent("invalid_grant", status=400), bc_client.BcTransient("down"),
                RuntimeError("boom")):
        bc.calls.clear()
        bc.exchange_error = err
        _, query = _redirect(rb.oauth_callback(code="C1", state=S1, error=None))
        assert query == {"bc": "error", "reason": "rfp_bc_not_connected"}
        assert [c[0] for c in bc.calls] == ["consume_state", "exchange_code"]


def test_callback_refuses_a_user_without_view_all_and_stores_nothing(bc, _env):
    for me in ({"id": "bc-user", "bidBoardPermissions": {"viewAll": False}}, {"id": "bc-user"}):
        bc.calls.clear()
        bc.me = me
        _, query = _redirect(rb.oauth_callback(code="C1", state=S1, error=None))
        assert query == {"bc": "error", "reason": "rfp_bc_view_all_required"}
        assert "store_connection" not in [c[0] for c in bc.calls]
    assert all(a[1] == "rfp_bc.connect_refused" for a in _env)


def test_callback_storage_failure_redirects_not_connected(bc):
    bc.store_error = RuntimeError("db down")
    _, query = _redirect(rb.oauth_callback(code="C1", state=S1, error=None))
    assert query == {"bc": "error", "reason": "rfp_bc_not_connected"}


def test_callback_without_a_configured_app_redirects_not_connected(bc, monkeypatch):
    monkeypatch.setattr(rb, "get_settings", lambda: _settings(building_connected_client_secret=""))
    _, query = _redirect(rb.oauth_callback(code="C1", state=S1, error=None))
    assert query["reason"] == "rfp_bc_not_connected"
    assert "exchange_code" not in [c[0] for c in bc.calls]


def test_callback_failure_codes_are_registered():
    assert {c.value for c in rb._CODES} == {
        "rfp_bc_disconnected", "rfp_bc_not_connected", "rfp_bc_state_invalid", "rfp_bc_view_all_required",
    }
    from app.services import rfp_bc_portal

    assert ErrorCode.RFP_BC_NOT_CONNECTED.value == rfp_bc_portal.CODE_NOT_CONNECTED


def test_callback_over_the_real_state_table_is_single_use_and_expires(monkeypatch, _env):
    """The real bc_client: connect writes the state bound to the caller, the
    callback consumes it (a replay is state_invalid), an expired state is
    refused, and the stored row carries the tokens only in the table."""
    from app.services import bc_client
    from tests.test_rfp_email_ingest import FakeDB

    fake = FakeDB({"rfp_oauth_states": [], "rfp_oauth_connections": []})
    monkeypatch.setattr(rb, "get_supabase", lambda: fake)
    monkeypatch.setattr(bc_client, "exchange_code",
                        lambda config, code, transport=None: {"access_token": ACCESS, "refresh_token": REFRESH,
                                                              "expires_in": 3599})
    monkeypatch.setattr(bc_client, "fetch_me", lambda token, config, transport=None: {
        "id": "bc-user", "firstName": "Pat", "lastName": "Lee", "email": "pat@g3electrical.com",
        "companyId": "co-1", "bidBoardPermissions": {"viewAll": True}})
    url = rb.connect(_user(Role.EXECUTIVE, uid="u-exec"))["url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert [r["actor_id"] for r in fake.tables["rfp_oauth_states"]] == ["u-exec"]
    _, query = _redirect(rb.oauth_callback(code="C1", state=state, error=None))
    assert query == {"bc": "connected"} and fake.tables["rfp_oauth_states"] == []
    (conn,) = fake.tables["rfp_oauth_connections"]
    assert conn["status"] == "connected" and conn["connected_by"] == "u-exec" and conn["view_all"] is True
    _, query = _redirect(rb.oauth_callback(code="C1", state=state, error=None))
    assert query["reason"] == "rfp_bc_state_invalid"
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    fake.tables["rfp_oauth_states"].append({"state": OLD, "provider": "buildingconnected", "actor_id": "u-exec",
                                            "created_at": (past - timedelta(minutes=10)).isoformat(),
                                            "expires_at": past.isoformat()})
    _, query = _redirect(rb.oauth_callback(code="C1", state=OLD, error=None))
    assert query["reason"] == "rfp_bc_state_invalid" and fake.tables["rfp_oauth_states"] == []
