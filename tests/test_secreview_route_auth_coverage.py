"""Route-table auth coverage (security review finding 12).

Walks every APIRoute mounted on app.main.app and pins two invariants so a
future route cannot ship without a gate:

1. Every route has `get_current_user` somewhere in its dependency tree
   (directly, or through a `require_*` gate that depends on it), except the
   explicit PUBLIC_ROUTES allowlist.
2. Every POST/PUT/PATCH/DELETE route carries a role-class gate (a dependency
   that rejects by role or dev flag), or appears on INLINE_ROLE_CHECK_WRITES:
   the reviewed list of write routes that take only `get_current_user` or
   `require_project_assignment` and check the role inside the handler (or are
   self-service on the caller's own rows).

Both allowlists are EXACT: a new unguarded route fails, and so does a stale
entry once its route is removed or gains a real gate, which keeps the lists
reviewed rather than grown by habit. Adding a route to either list is a
security decision; say why in the comment next to it.
"""

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

import app.main as app_main
from app.core import deps
from app.routers import rfp_emails, rfp_testing

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Routes with no authentication at all.
PUBLIC_ROUTES = {
    ("GET", "/health"),  # liveness probe, returns a constant
    # The browser's return from Autodesk OAuth: the signed state is the proof
    # (rfp_bc.py module docstring), and the feature gate still runs first.
    ("GET", "/rfp-portal/buildingconnected/callback"),
}

# Dependencies that reject a caller by role or dev flag before the handler runs.
ROLE_GATES = {
    deps.require_internal,
    deps.require_writer,
    deps.require_dev,
    deps.require_pm_read,
    deps.require_pm_write,
    deps.require_cp_read,
    deps.require_cp_write,
    rfp_emails.require_block_admin,
    rfp_testing.require_dev_it_admin,
}

# Reviewed write routes without a role-class dependency. Each one checks the
# role inline or only touches the caller's own rows.
INLINE_ROLE_CHECK_WRITES = {
    # require_project_assignment + inline `role != ESTIMATOR` 403.
    ("POST", "/estimator/projects/{project_id}/submit"),
    # Self-service: the caller's own notification rows.
    ("POST", "/notifications/read-all"),
    ("POST", "/notifications/{notification_id}/read"),
    # Inline WRITER_ROLES check (after state reads; the mutation is gated).
    ("POST", "/projects/{project_id}/advance"),
    # require_project_assignment + inline estimator category / uploader checks.
    ("POST", "/projects/{project_id}/files"),
    ("POST", "/projects/{project_id}/files/export"),
    ("DELETE", "/projects/{project_id}/files/{file_id}"),
    # require_project_assignment + inline WRITER_ROLES check.
    ("PATCH", "/projects/{project_id}/files/{file_id}/note"),
    # require_project_assignment + inline WRITER_ROLES-or-estimator check.
    ("POST", "/projects/{project_id}/notes"),
    ("POST", "/projects/{project_id}/notes/read"),
    # Self-service on the caller's own profile / MFA / tour flag.
    ("PATCH", "/users/me"),
    ("POST", "/users/me/estimator-tour"),
    ("DELETE", "/users/me/mfa"),
    # Inline `is_dev` 403 (dev role switcher).
    ("PATCH", "/users/me/role"),
}


def _calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _calls(d)


def _is_role_gate(call) -> bool:
    if call in ROLE_GATES:
        return True
    # Every require_role(...) closure, including module-level aliases such as
    # rfp_bc.require_bc_connect.
    return getattr(call, "__module__", "") == deps.__name__ and getattr(
        call, "__qualname__", ""
    ) == "require_role.<locals>._dep"


def _classify(application):
    """Return (unauthenticated, writes_without_role_gate) as sets of (method, path)."""
    unauth, weak = set(), set()
    for route in application.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = list(_calls(route.dependant))
        keys = {(m, route.path) for m in route.methods}
        if deps.get_current_user not in calls:
            unauth |= keys
            continue
        if not any(_is_role_gate(c) for c in calls):
            weak |= {k for k in keys if k[0] in WRITE_METHODS}
    return unauth, weak


def test_every_route_is_authenticated_except_the_public_allowlist():
    unauth, _ = _classify(app_main.app)
    assert unauth == PUBLIC_ROUTES


def test_every_write_route_has_a_role_gate_or_is_reviewed():
    _, weak = _classify(app_main.app)
    assert weak == INLINE_ROLE_CHECK_WRITES


def test_route_table_is_not_trivially_empty():
    n = sum(1 for r in app_main.app.routes if isinstance(r, APIRoute))
    assert n > 300


# ── The checker itself catches the regressions it exists for ─────────────────


def _toy_app():
    toy = FastAPI()
    r = APIRouter()

    @r.get("/open")
    def open_route():
        return {}

    @r.post("/weak")
    def weak_route(user=Depends(deps.get_current_user)):
        return {}

    @r.post("/assigned/{project_id}")
    def assigned_route(user=Depends(deps.require_project_assignment)):
        return {}

    @r.post("/writer")
    def writer_route(user=Depends(deps.require_writer)):
        return {}

    @r.delete("/exec", dependencies=[Depends(deps.require_role("executive"))])
    def exec_route(user=Depends(deps.get_current_user)):
        return {}

    @r.get("/read")
    def read_route(user=Depends(deps.get_current_user)):
        return {}

    toy.include_router(r)
    return toy


def test_checker_flags_unauthenticated_and_ungated_write_routes():
    unauth, weak = _classify(_toy_app())
    assert unauth == {("GET", "/open")}
    # get_current_user alone and require_project_assignment alone are not role
    # gates on a write; require_writer and require_role are; GETs are not
    # held to the write rule.
    assert weak == {("POST", "/weak"), ("POST", "/assigned/{project_id}")}
