"""The assistant ASGI shim and firewall (app/assistant/shim.py,
app/assistant/firewall.py).

Invariants pinned here (docs/ASSISTANT.md, "The shim is never the served
app" and Permissions item 4):

  served app   app.main.app stays the FastAPI instance; app/main.py never
               imports app.assistant; the shim is only an ASGITransport
               target.
  DELETE       refused outright, allowlisted or not.
  deny-list    every (method, path_format) on DENY_LIST is refused, even
               for an IT Admin, and every pair names a live route.
  walk         every route carrying an IT Admin only or dev only dependency
               is on DENY_LIST; every write under /agent-worker/v1,
               /browser-agents, /users and /payroll is on it.
  allowlist    a route the registry did not declare is refused; an
               allowlisted GET passes, under the principal, with the
               provenance contextvar visible to the sync dependency.
  refusal      a JSON 403 with code assistant_firewall, no handler run.

The deps.py hook is simulated with app.dependency_overrides, which is
exactly what the hook will do: hand the bdra_ bearer to
principal.resolve_bdra_principal with the request scope.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

import app.main
from app.assistant import firewall, principal, shim
from app.core import deps, provenance, supabase_client
from app.core.roles import Role

from tests.test_assistant_principal import (
    APPROVAL,
    NOW,
    RUN,
    SECRET,
    USER,
    _delegation,
    _FakeDB,
    _profile,
)

ALLOW = (("GET", "/features"), ("GET", "/users/me"), ("GET", "/health"))


# ── wiring ───────────────────────────────────────────────────────────────────


@pytest.fixture
def settings(monkeypatch):
    s = SimpleNamespace(assistant_token_secret=SECRET, assistant_enabled=True)
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    return s


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDB(assistant_delegations=[_delegation()], profiles=[_profile()])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def hook(db):
    """The deps.py hook, as a dependency override: a bdra_ bearer goes to
    the principal with the request scope; anything else is refused. Records
    what the dependency saw of the provenance contextvar."""
    seen: list[dict] = []

    def _load(user_id):
        rows = [r for r in db.tables["profiles"] if r["id"] == user_id]
        return rows[0] if rows else None

    def _hooked(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
        seen.append(provenance.fields())
        if token.startswith("bdra_"):
            return principal.resolve_bdra_principal(request.scope, token, _load)
        raise deps.HTTPException(401, "Invalid or expired token")

    app.main.app.dependency_overrides[deps.get_current_user] = _hooked
    try:
        yield seen
    finally:
        app.main.app.dependency_overrides.pop(deps.get_current_user, None)


def _call(shim_app, method, path, token=None, **kw):
    """One request through the shim the way the executor runs it: a fresh
    loop per call, never the server loop."""
    headers = dict(kw.pop("headers", {}) or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def go():
        transport = httpx.ASGITransport(app=shim_app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://assistant") as client:
            return await client.request(method, path, headers=headers, **kw)

    return asyncio.run(go())


def _shim(allow=ALLOW, user_id=USER, run_id=RUN, approval_id=None):
    return shim.make_shim(allow, user_id=user_id, run_id=run_id, approval_id=approval_id)


def _route_table() -> dict[tuple[str, str], APIRoute]:
    table = {}
    for route in app.main.app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods or ():
                table[(method, route.path_format)] = route
    return table


# ── the served app is untouched ──────────────────────────────────────────────


def _is_shim(value) -> bool:
    if isinstance(value, shim.AssistantShim):
        return True
    return isinstance(value, type) and issubclass(value, shim.AssistantShim)


def _walk_routes(routes):
    for route in routes:
        yield route
        inner = getattr(route, "routes", None)
        if inner:
            yield from _walk_routes(inner)


def test_served_app_is_the_fastapi_instance_and_never_serves_the_shim():
    """main.py reaches app.assistant only transitively, through
    app.routers.assistant; it never imports the package itself, and nothing
    it serves, mounts or registers as middleware is the shim."""
    assert isinstance(app.main.app, FastAPI)
    assert shim.served_app() is app.main.app
    assert not _is_shim(app.main.app)
    source = Path(app.main.__file__).read_text(encoding="utf-8")
    assert "app.assistant" not in source and "assistant.shim" not in source
    for m in app.main.app.user_middleware:
        assert not _is_shim(m.cls), m
        assert not any(_is_shim(v) for v in (getattr(m, "kwargs", None) or {}).values()), m
        assert not any(_is_shim(v) for v in (getattr(m, "args", None) or ())), m
    for route in _walk_routes(app.main.app.router.routes):
        for attr in ("app", "endpoint"):
            assert not _is_shim(getattr(route, attr, None)), route
    stack = getattr(app.main.app, "middleware_stack", None)
    assert not _is_shim(stack)


def test_importing_app_main_never_loads_the_loop_or_the_shim():
    """app.routers.assistant imports only the pure assistant modules at load
    time and the agent loop lazily, so a fresh interpreter that imports
    app.main has neither app.assistant.loop nor app.assistant.shim loaded."""
    import subprocess
    import sys

    code = (
        "import sys, app.main; "
        "bad = [m for m in ('app.assistant.shim', 'app.assistant.loop', 'app.assistant.executor', "
        "'app.assistant.jobs') if m in sys.modules]; "
        "print(','.join(bad))"
    )
    root = Path(app.main.__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", out.stdout


def test_router_job_type_matches_the_job_module():
    from app.assistant import jobs
    from app.routers import assistant as router_mod

    assert router_mod.JOB_TYPE == jobs.JOB_TYPE


def test_make_shim_needs_an_identity_and_normalizes_the_allowlist():
    with pytest.raises(ValueError):
        shim.make_shim(ALLOW, user_id="", run_id=RUN)
    with pytest.raises(ValueError):
        shim.make_shim(ALLOW, user_id=USER, run_id="")
    s = shim.make_shim([("get", "/features"), ["post", "/projects"]], user_id=USER, run_id=RUN)
    assert s.allowlist == {("GET", "/features"), ("POST", "/projects")}
    assert s.approval_id is None
    assert _shim(approval_id=APPROVAL).approval_id == APPROVAL


# ── the firewall as pure functions ───────────────────────────────────────────


def test_check_orders_delete_then_deny_then_allowlist():
    allow = {("GET", "/features"), ("POST", "/users"), ("DELETE", "/todos/{todo_id}")}
    assert firewall.check("GET", "/features", allow).allowed
    assert firewall.check("get", "/features", allow).allowed  # method case
    v = firewall.check("DELETE", "/todos/{todo_id}", allow)
    assert not v.allowed and v.reason == "method_refused"
    v = firewall.check("POST", "/users", allow)
    assert not v.allowed and v.reason == "deny_listed"
    v = firewall.check("GET", "/users/me", allow)
    assert not v.allowed and v.reason == "not_allowlisted"
    v = firewall.check("GET", None, allow)
    assert not v.allowed and v.reason == "no_route"
    v = firewall.check("POST", "/agent-worker/v1/anything/new", allow | {("POST", "/agent-worker/v1/anything/new")})
    assert not v.allowed and v.reason == "deny_listed"


def test_deny_list_is_exact_pairs_of_method_and_path_format():
    for method, path_format in firewall.DENY_LIST:
        assert method == method.upper() and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert path_format.startswith("/") and not path_format.endswith("/")
        assert "?" not in path_format
        # A template, never a concrete path: ids appear only as placeholders.
        assert not re.search(r"/[0-9a-f]{8}-[0-9a-f]{4}-", path_format)


def test_every_deny_pair_names_a_live_route():
    table = _route_table()
    stale = sorted(p for p in firewall.DENY_LIST if p not in table)
    assert stale == [], f"deny-list pairs that no longer match a route: {stale}"
    for pair, why in firewall.NOT_DENIED.items():
        assert pair in table and pair not in firewall.DENY_LIST, (pair, why)


def _admin_only(route: APIRoute) -> set[str]:
    """The IT Admin only or dev only dependencies a route carries, by name,
    including those merged from its router."""
    calls = []

    def walk(dep):
        calls.append(dep.call)
        for d in dep.dependencies:
            walk(d)

    walk(route.dependant)
    found = set()
    for call in calls:
        name = getattr(call, "__name__", "")
        if name in {"require_dev", "require_dev_it_admin", "require_agent_admin",
                    "require_model_admin", "require_block_admin"}:
            found.add(name)
        if getattr(call, "__qualname__", "").startswith("require_role.<locals>._dep"):
            for cell in call.__closure__ or ():
                try:
                    allowed = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(allowed, set) and allowed == {Role.IT_ADMIN}:
                    found.add("require_role(it_admin)")
    return found


def test_every_it_admin_only_or_dev_only_route_is_deny_listed():
    missing = []
    for (method, path_format), route in _route_table().items():
        if method in {"HEAD", "OPTIONS"}:
            continue
        gates = _admin_only(route)
        if gates and (method, path_format) not in firewall.DENY_LIST:
            missing.append((method, path_format, sorted(gates)))
    assert missing == [], f"admin-only routes missing from DENY_LIST: {missing}"


def test_every_write_under_the_protected_prefixes_is_deny_listed():
    prefixes = ("/agent-worker/v1", "/browser-agents", "/users", "/payroll")
    missing = []
    for method, path_format in _route_table():
        if method in {"GET", "HEAD", "OPTIONS"}:
            continue
        if path_format.startswith(prefixes) and (method, path_format) not in firewall.DENY_LIST:
            # Self-service edits under /users/me (profile, mfa, language,
            # notification prefs, the estimator tour) are the user's own, not
            # user management; only /users/me/role is on the design's list.
            if path_format.startswith("/users/me") and path_format != "/users/me/role":
                continue
            missing.append((method, path_format))
    assert missing == [], f"writes missing from DENY_LIST: {missing}"
    # Every worker-protocol route, reads included, sits behind the prefix rule.
    for method, path_format in _route_table():
        if path_format.startswith("/agent-worker/v1"):
            assert firewall.is_deny_listed(method, path_format), (method, path_format)


def test_resolve_path_format_matches_like_the_app():
    scope = {"type": "http", "method": "GET", "path": "/users/me", "headers": [], "query_string": b""}
    assert firewall.resolve_path_format(app.main.app, scope) == "/users/me"
    scope["path"] = "/projects/abc-123"
    assert firewall.resolve_path_format(app.main.app, scope) == "/projects/{project_id}"
    scope["method"] = "DELETE"
    assert firewall.resolve_path_format(app.main.app, scope) == "/projects/{project_id}"
    scope["path"] = "/no/such/route"
    assert firewall.resolve_path_format(app.main.app, scope) is None
    scope["path"], scope["method"] = "/features", "POST"   # partial match: wrong method
    assert firewall.resolve_path_format(app.main.app, scope) is None


# ── through the shim ─────────────────────────────────────────────────────────


def test_allowlisted_get_passes_under_the_principal(settings, db, hook):
    token = principal.mint_token(USER, RUN, now=NOW)
    resp = _call(_shim(), "GET", "/features", token)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) >= {"bidding", "pm", "certified_payroll"}
    # The sync dependency ran inside the provenance context the shim set.
    assert hook == [{"actor_kind": "assistant", "assistant_run_id": RUN}]
    assert db.calls == ["assistant_delegations"]
    # ... and the context is gone once the call returns.
    assert provenance.fields() == {}


def test_the_principal_inside_the_shim_is_the_owner_never_dev_never_impersonated(settings, db, hook):
    db.tables["profiles"] = [_profile(is_dev=True, role="it_admin")]
    token = principal.mint_token(USER, RUN, now=NOW)
    resp = _call(_shim(), "GET", "/features", token,
                 headers={deps.IMPERSONATE_HEADER: "est-1"})
    assert resp.status_code == 200, resp.text
    # /features answers for the owner's role: an it_admin sees every module.
    assert resp.json()["bidding"] is True


def test_token_for_another_run_is_refused_inside_the_shim(settings, db, hook):
    token = principal.mint_token(USER, "other-run", now=NOW)
    resp = _call(_shim(), "GET", "/features", token)
    assert resp.status_code == 401
    resp = _call(_shim(), "GET", "/features", principal.mint_token("other-user", RUN, now=NOW))
    assert resp.status_code == 401
    assert db.calls == []


def test_revoked_delegation_is_refused_inside_the_shim(settings, db, hook):
    db.tables["assistant_delegations"] = [_delegation(revoked_at="2026-10-20T00:00:00+00:00")]
    token = principal.mint_token(USER, RUN, now=NOW)
    resp = _call(_shim(), "GET", "/features", token)
    assert resp.status_code == 403 and resp.json()["detail"] == "assistant_delegation_missing"


def _assert_firewalled(resp, reason):
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["code"] == "assistant_firewall" and body["detail"] == "assistant_firewall"
    assert body["reason"] == reason
    assert resp.headers["x-error-code"] == "assistant_firewall"
    assert resp.headers["cache-control"] == "no-store"


def test_delete_is_refused_outright_even_when_allowlisted(settings, db, hook):
    token = principal.mint_token(USER, RUN, now=NOW)
    allow = ALLOW + (("DELETE", "/todos/{todo_id}"), ("DELETE", "/users/{user_id}"))
    for path in ("/todos/abc", "/users/abc", "/no/such/route"):
        resp = _call(_shim(allow), "DELETE", path, token)
        _assert_firewalled(resp, "method_refused")
    assert hook == [] and db.calls == []   # nothing reached the app


def test_deny_listed_routes_are_refused_even_for_it_admin(settings, db, hook):
    db.tables["profiles"] = [_profile(role="it_admin")]
    token = principal.mint_token(USER, RUN, now=NOW)
    # The whole deny-list, allowlisted on purpose: the deny-list still wins.
    allow = tuple(firewall.DENY_LIST) + ALLOW
    s = _shim(allow)
    samples = {
        ("POST", "/users"): "/users",
        ("PATCH", "/users/{user_id}"): "/users/abc",
        ("POST", "/users/{user_id}/reset-mfa"): "/users/abc/reset-mfa",
        ("PATCH", "/auto-send/settings/enabled"): "/auto-send/settings/enabled",
        ("POST", "/projects/{project_id}/delete"): "/projects/abc/delete",
        ("POST", "/projects/{project_id}/gono/decide"): "/projects/abc/gono/decide",
        ("POST", "/projects/{project_id}/verify"): "/projects/abc/verify",
        ("PUT", "/model-status/target"): "/model-status/target",
        ("POST", "/browser-agents/admin/canary"): "/browser-agents/admin/canary",
        ("POST", "/agent-worker/v1/claim"): "/agent-worker/v1/claim",
        ("GET", "/deleted-projects"): "/deleted-projects",
        ("POST", "/payroll/reports/{report_id}/finalize"): "/payroll/reports/abc/finalize",
        ("PATCH", "/training/boq/{example_id}/review"): "/training/boq/abc/review",
    }
    for (method, path_format), path in samples.items():
        assert (method, path_format) in firewall.DENY_LIST, (method, path_format)
        resp = _call(s, method, path, token, json={})
        _assert_firewalled(resp, "method_refused" if method == "DELETE" else "deny_listed")
    assert hook == [] and db.calls == []
    # And every pair, as the pure check sees it, with the full allowlist.
    for method, path_format in firewall.DENY_LIST:
        verdict = firewall.check(method, path_format, s.allowlist)
        assert not verdict.allowed and verdict.reason in ("deny_listed", "method_refused")


def test_route_outside_the_allowlist_is_refused(settings, db, hook):
    token = principal.mint_token(USER, RUN, now=NOW)
    s = _shim((("GET", "/features"),))
    _assert_firewalled(_call(s, "GET", "/users/me", token), "not_allowlisted")
    _assert_firewalled(_call(s, "GET", "/projects/abc", token), "not_allowlisted")
    _assert_firewalled(_call(s, "POST", "/projects", token, json={}), "not_allowlisted")
    _assert_firewalled(_call(s, "GET", "/no/such/route", token), "no_route")
    _assert_firewalled(_call(s, "POST", "/features", token), "no_route")   # wrong method
    assert hook == [] and db.calls == []


def test_allowlist_matches_on_path_format_not_the_raw_path(settings, db, hook):
    token = principal.mint_token(USER, RUN, now=NOW)
    # The raw path is allowlisted, the template is not: refused.
    _assert_firewalled(_call(_shim((("GET", "/projects/abc"),)), "GET", "/projects/abc", token),
                       "not_allowlisted")


def test_shim_marks_the_scope_internal_and_strips_impersonation(settings, db):
    seen = {}

    async def spy(scope, receive, send):
        seen.update(scope)
        await shim._refuse(send, firewall.Verdict(False, "spy", scope["method"], None))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shim, "served_app", lambda: app.main.app)
        mp.setattr(firewall, "check_scope",
                   lambda a, sc, al: firewall.Verdict(True, "allowed", "GET", "/features"))
        mp.setattr(app.main, "app", spy)
        mp.setattr(shim, "served_app", lambda: spy)
        resp = _call(_shim(), "GET", "/features", "bdra_x", headers={deps.IMPERSONATE_HEADER: "est-1"})
    assert resp.status_code == 403
    assert seen[principal.SCOPE_MARKER] is True
    names = {name for name, _ in seen["headers"]}
    assert deps.IMPERSONATE_HEADER.encode() not in names and b"authorization" in names


def test_shim_serves_http_only():
    s = _shim()
    with pytest.raises(RuntimeError):
        asyncio.run(s({"type": "websocket", "path": "/x", "headers": []}, None, None))
