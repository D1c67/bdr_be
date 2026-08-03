"""Sub-app feature flags — BIDDING_ENABLED / PM_ENABLED / CERTIFIED_PAYROLL_ENABLED.

One deployment serves three sub-apps; these flags decide which of them it
actually serves, so the whole codebase can ship to production while an untested
module stays dark. What matters, and what these tests pin:

  * a disabled sub-app's routes 404 — before auth, so they look like paths that
    were never implemented rather than ones the caller lacks rights to;
  * the SHARED SPINE keeps working, because switching one module off must not
    break the other two;
  * the seams where one module's data reaches another (certified-payroll files
    in the PM documents hub, the won-bid → PM handoff, notification deep links)
    respect the flag rather than leaking through it.

Routes are exercised through a TestClient with no credentials: a 404 proves the
feature guard ran first, and a 401 proves the route is still mounted and merely
wants a token. Flag changes go through the env + get_settings.cache_clear(),
matching how conftest pins the rest of the security-critical settings.
"""


import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.features import (
    PM_NOTIFICATION_TYPES,
    SubApp,
    enabled_map,
    home_path,
    is_enabled,
    notification_sub_app,
)

FLAG_ENV = {
    SubApp.BIDDING: "BIDDING_ENABLED",
    SubApp.PM: "PM_ENABLED",
    SubApp.CERTIFIED_PAYROLL: "CERTIFIED_PAYROLL_ENABLED",
}


@contextmanager
def flags(**overrides: bool):
    """Run the block with the named sub-apps switched off/on."""
    previous = {}
    for sub_app, value in overrides.items():
        var = FLAG_ENV[sub_app]
        previous[var] = os.environ.get(var)
        os.environ[var] = "true" if value else "false"
    get_settings.cache_clear()
    try:
        yield
    finally:
        for var, old in previous.items():
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old
        get_settings.cache_clear()


@pytest.fixture(scope="module")
def client() -> TestClient:
    import app.main

    return TestClient(app.main.app, raise_server_exceptions=False)


# Every route below is unauthenticated: 404 = the feature guard fired first,
# 401 = mounted and asking for a token.
BIDDING_ROUTES = [
    "/projects/p1/stage-events",
    "/projects/p1/gono",
    "/projects/p1/rfqs",
    "/projects/p1/pricing-summary",
    "/projects/p1/outcome",
    "/projects/p1/files",
    "/projects/p1/notes",
    "/analytics/summary",
    "/estimator/projects",
    "/training/boq",
]
PM_ROUTES = [
    "/pm/projects",
    "/pm/projects/p1/financials",
    "/pm/projects/p1/documents/all",
    "/pm/projects/p1/materials",
    "/pm/projects/p1/submittals/requests",
    "/emails",
]
CP_ROUTES = [
    "/payroll/reports",
    "/payroll/projects",
    "/payroll/employees",
    "/payroll/rates",
    "/payroll/settings",
]
# The spine all three hang off. None of these may ever 404 on a flag.
SHARED_ROUTES = [
    "/users/me",
    "/notifications",
    "/projects",
    "/vendors",
    "/gcs",
    "/material-categories",
    "/todos",
    "/submittals",
    "/projects/p1/notification-log",
    "/features",
]


def _status(client: TestClient, path: str) -> int:
    return client.get(path).status_code


# ── Defaults ───────────────────────────────────────────────────────────────


def test_all_sub_apps_enabled_by_default():
    """Unset means served — dev, staging and this suite are unaffected, and a
    forgotten var never silently kills a working module."""
    assert enabled_map() == {"bidding": True, "pm": True, "certified_payroll": True}


def test_every_route_is_reachable_with_all_flags_on(client: TestClient):
    for path in BIDDING_ROUTES + PM_ROUTES + CP_ROUTES + SHARED_ROUTES:
        assert _status(client, path) != 404, f"{path} 404s with every module enabled"


# ── A disabled sub-app is gone ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "sub_app,gated",
    [
        (SubApp.BIDDING, BIDDING_ROUTES),
        (SubApp.PM, PM_ROUTES),
        (SubApp.CERTIFIED_PAYROLL, CP_ROUTES),
    ],
)
def test_disabled_sub_app_routes_404(client: TestClient, sub_app: SubApp, gated: list[str]):
    with flags(**{sub_app: False}):
        for path in gated:
            assert _status(client, path) == 404, f"{path} still served with {sub_app} off"


@pytest.mark.parametrize("sub_app", list(SubApp))
def test_shared_spine_survives_any_flag(client: TestClient, sub_app: SubApp):
    with flags(**{sub_app: False}):
        for path in SHARED_ROUTES:
            assert _status(client, path) != 404, f"{path} broke with {sub_app} off"


def test_disabled_route_404s_before_authentication(client: TestClient):
    """404 not 403, and without a token: a module this deployment doesn't serve
    must be indistinguishable from a path that doesn't exist."""
    with flags(**{SubApp.PM: False}):
        resp = client.get("/pm/projects")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}  # FastAPI's own unmatched-route body


def test_bidding_only_routes_on_the_shared_projects_router(client: TestClient):
    """`/projects` is the spine PM and CP also use, but bid intake, the bid
    lifecycle and bid-invitation membership on it are bidding-only."""
    with flags(**{SubApp.BIDDING: False}):
        assert client.post("/projects", json={}).status_code == 404
        assert client.post("/projects/p1/abandon", json={}).status_code == 404
        assert client.post("/projects/p1/reactivate").status_code == 404
        assert _status(client, "/projects/p1/gcs") == 404
        # …while reading and renaming a project row still works, because that is
        # how a PM- or CP-only deployment manages the rows it owns.
        assert _status(client, "/projects") != 404
        assert client.patch("/projects/p1", json={}).status_code != 404


def test_dependency_choke_points_also_enforce_the_flag():
    """main.py guards every PM/CP router at mount time; require_pm_read and
    friends are the second lock, so a future route missing from that table still
    fails closed. They are used by the PM/CP routers and by nothing else."""
    import asyncio

    from fastapi import HTTPException

    from app.core.deps import (
        CurrentUser,
        require_cp_read,
        require_cp_write,
        require_pm_read,
        require_pm_write,
    )
    from app.core.roles import Role

    user = CurrentUser(id="u1", email="u@g3.com", role=Role.EXECUTIVE, is_active=True)

    with flags(**{SubApp.PM: False, SubApp.CERTIFIED_PAYROLL: False}):
        for dep in (require_pm_read, require_pm_write, require_cp_read, require_cp_write):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(dep(user))
            assert exc.value.status_code == 404, dep.__name__


def test_all_three_disabled_refuses_to_boot():
    """A deployment serving nothing is a config typo, and every route 404ing is
    far harder to diagnose after the fact than a refused boot."""
    from app.core.config import Settings

    with pytest.raises(ValueError, match="serve no application at all"):
        Settings(bidding_enabled=False, pm_enabled=False, certified_payroll_enabled=False)


# ── Cross-module seams ─────────────────────────────────────────────────────


def test_cp_documents_vanish_from_the_pm_hub_when_payroll_is_off(monkeypatch):
    """The containment boundary for the CP flag.

    The PM documents hub's /all, /file and /export routes all resolve through
    list_project_documents, so without this the module being 'off' would still
    hand PM readers signed URLs to certified-payroll files — and worse, let a
    `cp:` hub key be attached to an outbound vendor submittal email.
    """
    from app.services import pm_folders

    def _boom(*_args, **_kwargs):
        raise AssertionError("_cp_documents queried the CP tables while CP was off")

    monkeypatch.setattr(pm_folders, "get_supabase", _boom)
    with flags(**{SubApp.CERTIFIED_PAYROLL: False}):
        assert pm_folders._cp_documents("p1") == []


def _activate(monkeypatch) -> tuple[bool, list[str], list[dict]]:
    """Run activate_pm_for_win against a fake DB. Returns (activated, bell types,
    the rows it wrote)."""
    from app.services import pm, pm_workflow

    written: list[dict] = []
    bells: list[str] = []

    class _Query:
        def __init__(self, db, table):
            self.db, self.table_name = db, table

        def insert(self, row, *a, **k):
            written.append({"table": self.table_name, **row})
            return self

        def update(self, row, *a, **k):
            written.append({"table": self.table_name, **row})
            return self

        def __getattr__(self, _name):
            return lambda *a, **k: self

        def execute(self):
            queue = self.db.queues.get(self.table_name) or []
            return type("R", (), {"data": queue.pop(0) if queue else []})()

    class _FakeDB:
        def __init__(self, **tables):
            self.queues = {name: list(rows) for name, rows in tables.items()}

        def table(self, name):
            return _Query(self, name)

    db = _FakeDB(
        projects=[
            [{"id": "p1", "name": "Acme Clinic", "pm_stage": None, "abandoned_at": None,
              "est_start_date": "2026-09-01", "est_finish_date": "2026-12-01"}],
            [{"id": "p1"}],  # the optimistic pm_stage flip succeeded
        ],
        bid_gc_outcomes=[[{"our_amount": "125000"}]],
        general_contractors=[[{"name": "Turner"}]],
        pm_details=[[]],
        pm_stage_events=[[]],
    )
    monkeypatch.setattr(pm, "get_supabase", lambda: db)
    monkeypatch.setattr(pm_workflow, "get_supabase", lambda: db)
    monkeypatch.setattr(pm, "seed_pm_materials_from_boq", lambda _pid: 0)
    monkeypatch.setattr(pm, "audit", lambda *a, **k: None)
    monkeypatch.setattr(pm, "notify_role", lambda *a, **k: bells.append(a[2]))

    activated = pm.activate_pm_for_win("p1", "u1", "gc1")
    return activated, bells, written


def test_won_bid_still_enters_pm_while_the_module_is_dark(monkeypatch):
    """Activation is DATA, not UI. Recording it as the win happens is what lets
    PM be switched on later with its history already correct — no backfill, no
    won job silently missing from the module. Only the bell is suppressed, since
    it deep-links to a PM page that doesn't render."""
    with flags(**{SubApp.PM: False}):
        activated, bells, written = _activate(monkeypatch)

    assert activated is True
    tables = {row["table"] for row in written}
    assert "pm_details" in tables          # the PM record was created
    assert "pm_stage_events" in tables     # and its history started
    assert any(row.get("pm_stage") == "precon" for row in written)
    assert bells == []                     # …but nobody was told about it


def test_won_bid_notifies_when_pm_is_served(monkeypatch):
    """The other half: with the module on, the bell fires as it always has."""
    activated, bells, _ = _activate(monkeypatch)
    assert activated is True
    assert bells == ["pm_activated"]


# ── Notification routing ───────────────────────────────────────────────────


def test_pm_notification_types_are_declared_not_prefix_matched():
    """`submittal.response_received` is a PM notification without a pm_ prefix —
    the reason ownership is an explicit set rather than a startswith test."""
    assert notification_sub_app("pm_activated") is SubApp.PM
    assert notification_sub_app("submittal.response_received") is SubApp.PM
    assert notification_sub_app("bid_outcome") is SubApp.BIDDING
    assert notification_sub_app(None) is SubApp.BIDDING
    assert "submittal.response_received" in PM_NOTIFICATION_TYPES


def test_notification_deep_links_never_point_at_a_disabled_module():
    """These URLs land in a mailbox and are permanent — a link built for a module
    this deployment doesn't serve is dead forever, not a redirect the shell can
    quietly fix."""
    from app.services.notification_email import _deep_link

    with flags(**{SubApp.PM: False}):
        link = _deep_link("p1", "executive", "pm_activated")
        assert "/pm/projects/" not in link
        assert link.endswith("/projects/p1")

    with flags(**{SubApp.BIDDING: False}):
        # Bidding dark: nothing project-shaped is renderable, so fall back to the
        # first module that is served.
        assert _deep_link("p1", "executive", "bid_outcome").endswith("/pm")
        assert _deep_link(None, "executive", None).endswith("/pm")


def test_home_path_follows_the_enabled_modules():
    """Mirrors homePath() in bdr_fe/lib/features.ts."""
    assert home_path() == "/dashboard"
    with flags(**{SubApp.BIDDING: False}):
        assert home_path() == "/pm"
    with flags(**{SubApp.BIDDING: False, SubApp.PM: False}):
        assert home_path() == "/payroll"
