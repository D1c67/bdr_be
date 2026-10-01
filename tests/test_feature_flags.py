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

The experimental tool flags (bid_file_splitter, rfp_ingest) are not sub-apps
but ride along in GET /features and 404 their routers the same way; the RFP
Ingestion sandbox's gate, boot-time settings guard and read rate limiter are
pinned at the bottom.
"""


import asyncio
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.features import (
    PM_NOTIFICATION_TYPES,
    SubApp,
    enabled_map,
    home_path,
    notification_sub_app,
    require_rfp_ingest,
)

FLAG_ENV = {
    SubApp.BIDDING: "BIDDING_ENABLED",
    SubApp.PM: "PM_ENABLED",
    SubApp.CERTIFIED_PAYROLL: "CERTIFIED_PAYROLL_ENABLED",
    # Experimental tool flags (not sub-apps); keyed by their /features name.
    "bid_file_splitter": "BID_FILE_SPLITTER_ENABLED",
    "rfp_ingest": "RFP_INGEST_ENABLED",
}


@contextmanager
def flags(**overrides: bool):
    """Run the block with the named sub-apps (or tool flags) switched off/on."""
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
    forgotten var never silently kills a working module. bid_file_splitter and
    rfp_ingest are the odd ones out: they default FALSE (experimental) and read
    True here only because conftest pins them on for the suite."""
    assert enabled_map() == {
        "bidding": True,
        "pm": True,
        "certified_payroll": True,
        "bid_file_splitter": True,
        "rfp_ingest": True,
        "rfp_email_ingest": True,
        "rfp_ngem": False,
        "rfp_buildingconnected": False,
        "rfp_testing": False,
    }
    # The field default (not the pinned env) is what production inherits.
    assert Settings.model_fields["rfp_ingest_enabled"].default is False
    assert Settings.model_fields["bid_file_splitter_enabled"].default is False
    assert Settings.model_fields["rfp_testing_enabled"].default is False


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


# ── RFP Ingestion sandbox: flag, gate, settings guard, rate limit ──────────


def test_rfp_ingest_flag_rides_along_in_features():
    """Not a sub-app (no tile, no home route, not counted by the boot
    validator), but the frontend learns about it the same way."""
    assert enabled_map()["rfp_ingest"] is True
    with flags(rfp_ingest=False):
        assert enabled_map()["rfp_ingest"] is False
        assert home_path() == "/dashboard"  # a tool flag never moves home


def test_require_rfp_ingest_404s_with_the_bare_body_while_off():
    """Same contract as the splitter gate: while the flag is off the router
    must look like a path that was never implemented (404, not 403, and the
    bare FastAPI body), and it resolves before any token is read."""
    with flags(rfp_ingest=False):
        with pytest.raises(HTTPException) as exc:
            require_rfp_ingest()
        assert exc.value.status_code == 404
        assert exc.value.detail == "Not Found"
    assert require_rfp_ingest() is None


def test_rfp_ingest_settings_defaults_match_the_design_record():
    s = Settings(_env_file=None)
    # 450 MB since 0132 (docs/RFP_SPLIT.md section 1: the 300 MB allowance
    # raised 50%), and never above upload_max_bytes.
    assert s.rfp_ingest_max_file_bytes == 450 * 1024 * 1024
    assert s.upload_max_bytes == 450 * 1024 * 1024
    assert s.rfp_ingest_max_file_bytes <= s.upload_max_bytes
    assert s.max_request_body_bytes > s.upload_max_bytes
    assert s.rfp_ingest_max_files_per_run == 250
    assert s.rfp_ingest_max_pages_per_file == 3000
    assert s.rfp_ingest_max_pages_per_run == 20000
    assert s.rfp_ingest_max_page_side_pt == 14400
    assert (
        s.rfp_ingest_thumb_long_side,
        s.rfp_ingest_full_small_long_side,
        s.rfp_ingest_full_long_side,
    ) == (1568, 2200, 4000)
    assert (s.rfp_ingest_thumb_jpeg_quality, s.rfp_ingest_full_jpeg_quality) == (70, 85)
    assert s.rfp_ingest_full_small_threshold_pt == 1300
    assert s.rfp_ingest_max_text_chars_per_page == 200_000
    assert s.rfp_ingest_max_text_bytes_per_file == 64 * 1024 * 1024
    assert s.rfp_ingest_images_pdf_part_bytes == 150 * 1024 * 1024
    assert (
        s.rfp_ingest_open_timeout_seconds,
        s.rfp_ingest_page_stall_seconds,
        s.rfp_ingest_file_timeout_base_seconds,
        s.rfp_ingest_file_timeout_per_page_ms,
        s.rfp_ingest_file_timeout_max_seconds,
    ) == (300, 120, 300, 1500, 14400)
    # The memory default is address space, sized above the measured 1.18 GB RSS
    # peak for a raster-heavy drawing set: below it a legitimate small file
    # spends its whole failed-page allowance on `memory` pages and is rejected.
    assert (
        s.rfp_ingest_sandbox_memory_mb,
        s.rfp_ingest_sandbox_cpu_seconds,
        s.rfp_ingest_sandbox_output_file_mb,
        s.rfp_ingest_sandbox_disk_mb,
        s.rfp_ingest_scratch_reserve_mb,
    ) == (3072, 1800, 64, 3072, 2048)
    assert s.rfp_ingest_max_child_restarts == 5
    assert s.rfp_ingest_max_failed_page_ratio == 0.05
    assert s.rfp_ingest_min_failed_pages_allowed == 2
    assert s.rfp_ingest_sandbox_concurrency == 1
    assert (
        s.rfp_ingest_sandbox_uid,
        s.rfp_ingest_sandbox_uid_pool_base,
        s.rfp_ingest_sandbox_uid_pool_size,
    ) == (65534, 60100, 4)
    assert s.rfp_ingest_scratch_dir == ""
    assert s.rfp_ingest_queue_priority == 200
    assert s.rfp_ingest_retention_days == 14
    assert s.rfp_ingest_rate_limit_per_min == 120


@pytest.mark.parametrize(
    "overrides,names_var",
    [
        # Rendering tiers out of order, both edges.
        ({"rfp_ingest_thumb_long_side": 2300}, "RFP_INGEST_THUMB_LONG_SIDE"),
        ({"rfp_ingest_full_small_long_side": 4100}, "RFP_INGEST_FULL_SMALL_LONG_SIDE"),
        ({"rfp_ingest_max_failed_page_ratio": 1.5}, "RFP_INGEST_MAX_FAILED_PAGE_RATIO"),
        ({"rfp_ingest_max_failed_page_ratio": -0.1}, "RFP_INGEST_MAX_FAILED_PAGE_RATIO"),
        ({"rfp_ingest_sandbox_memory_mb": 256}, "RFP_INGEST_SANDBOX_MEMORY_MB"),
        ({"rfp_ingest_sandbox_concurrency": 0}, "RFP_INGEST_SANDBOX_CONCURRENCY"),
        ({"rfp_ingest_sandbox_uid_pool_size": 1}, "RFP_INGEST_SANDBOX_UID_POOL_SIZE"),
        (
            {"rfp_ingest_sandbox_concurrency": 2, "rfp_ingest_sandbox_uid_pool_size": 3},
            "RFP_INGEST_SANDBOX_UID_POOL_SIZE",
        ),
        ({"rfp_ingest_max_file_bytes": 451 * 1024 * 1024}, "RFP_INGEST_MAX_FILE_BYTES"),
        ({"upload_max_bytes": 100 * 1024 * 1024}, "RFP_INGEST_MAX_FILE_BYTES"),
        ({"rfp_ingest_sandbox_output_file_mb": 4000}, "RFP_INGEST_SANDBOX_OUTPUT_FILE_MB"),
        ({"rfp_ingest_page_stall_seconds": 450}, "RFP_INGEST_PAGE_STALL_SECONDS"),
        ({"llm_queue_lease_seconds": 240}, "RFP_INGEST_PAGE_STALL_SECONDS"),
        (
            {"rfp_ingest_images_pdf_part_bytes": 200 * 1024 * 1024 + 1},
            "RFP_INGEST_IMAGES_PDF_PART_BYTES",
        ),
    ],
)
def test_rfp_ingest_settings_guard_refuses_each_bad_combination(overrides, names_var):
    """Every rule in the design record refuses to boot and names the env var
    the operator has to fix. Enforced whether or not the flag is on, so a
    bad value surfaces at deploy time rather than when the flag flips."""
    with pytest.raises(ValueError, match=names_var):
        Settings(_env_file=None, rfp_ingest_enabled=False, **overrides)


def test_rfp_ingest_settings_guard_accepts_the_boundaries():
    Settings(
        _env_file=None,
        rfp_ingest_thumb_long_side=2200,          # thumb == full_small is fine
        rfp_ingest_full_small_long_side=2200,
        rfp_ingest_full_long_side=2200,           # == full is fine too
        rfp_ingest_max_failed_page_ratio=1.0,
        rfp_ingest_sandbox_memory_mb=512,
        rfp_ingest_sandbox_concurrency=2,
        rfp_ingest_sandbox_uid_pool_size=4,
        rfp_ingest_sandbox_output_file_mb=3072,   # == disk quota
        rfp_ingest_page_stall_seconds=449,        # just under lease / 2
        rfp_ingest_images_pdf_part_bytes=200 * 1024 * 1024,
    )


def test_rfp_ingest_rate_limit_scope_is_cataloged_in_code_and_docs():
    from app.core.error_codes import RATE_LIMIT_HELP, RateLimitScope

    assert RateLimitScope.RFP_INGEST == "rfp_ingest"
    assert "RFP Ingestion" in RATE_LIMIT_HELP[RateLimitScope.RFP_INGEST]
    docs = (Path(__file__).resolve().parents[1] / "docs" / "ERROR_CODES.md").read_text()
    assert "| `rfp_ingest` |" in docs
    # Every scope with help text has a docs row; the two lists cannot drift.
    for scope in RATE_LIMIT_HELP:
        if scope is not RateLimitScope.DEFAULT:
            assert f"| `{scope}` |" in docs, f"{scope} missing from docs/ERROR_CODES.md"


def test_rfp_ingest_rate_limit_reads_its_setting_and_counts_every_role(monkeypatch):
    from app.core import ratelimit
    from app.core.deps import CurrentUser
    from app.core.roles import Role

    seen = []
    monkeypatch.setattr(
        ratelimit, "_check", lambda scope, uid, limit, window: seen.append((scope, uid, limit, window))
    )
    admin = CurrentUser(id="u1", email="e@g3.com", role=Role.ESTIMATING_ADMIN, is_active=True)
    asyncio.run(ratelimit.rfp_ingest_rate_limit(user=admin))
    assert seen == [("rfp_ingest", "u1", 120, 60)]
    # No role narrowing: the routes gate on is_dev alone, so an estimator-role
    # dev account reaches them and has to spend the same budget.
    accountant = CurrentUser(id="u2", email="a@g3.com", role=Role.ACCOUNTANT, is_active=True)
    asyncio.run(ratelimit.rfp_ingest_rate_limit(user=accountant))
    assert seen[-1][1] == "u2"
    estimator = CurrentUser(id="e1", email="x@y.com", role=Role.ESTIMATOR, is_active=True)
    asyncio.run(ratelimit.rfp_ingest_rate_limit(user=estimator))
    assert seen[-1] == ("rfp_ingest", "e1", 120, 60)
    assert len(seen) == 3


# ── RFP harvest settings (docs/RFP_HARVEST.md section 8) ────────────────────


def _harvest_settings(**over):
    return Settings(_env_file=None, **over)


def test_rfp_harvest_settings_defaults_match_the_design_record():
    s = _harvest_settings()
    assert s.rfp_harvest_enabled is False
    assert s.procore_login_email == "" and s.procore_login_password == ""
    assert s.procore_min_request_interval_seconds == 2.0
    assert s.procore_login_min_interval_seconds == 600
    assert s.procore_login_max_failures == 3
    assert s.procore_login_lock_seconds == 21600
    assert s.procore_request_timeout_seconds == 30
    assert s.rfp_harvest_concurrency == 1
    assert s.rfp_harvest_queue_priority == 150
    assert s.rfp_harvest_poll_seconds == 60
    assert s.rfp_harvest_reuse_days == 14
    assert s.rfp_harvest_max_files == 250
    assert s.rfp_harvest_max_total_bytes == 2 * 1024 * 1024 * 1024
    # The priority sits between user-facing LLM work (100) and the sandbox (200).
    assert 100 < s.rfp_harvest_queue_priority < s.rfp_ingest_queue_priority


def test_procore_configured_needs_both_credentials_non_blank():
    assert _harvest_settings(procore_login_email="bot@example.com", procore_login_password="pw").procore_configured
    assert not _harvest_settings(procore_login_email="bot@example.com").procore_configured
    assert not _harvest_settings(procore_login_password="pw").procore_configured
    assert not _harvest_settings(procore_login_email="   ", procore_login_password="pw").procore_configured
    assert not _harvest_settings().procore_configured
    # rfp_harvest_active is the whole gate: master switch, slice switch, a
    # harvester. Since 2026-09-16 the PipelineSuite harvester and the email
    # harvester count as harvesters and need no credentials (both default
    # on), so the gate is open without Procore credentials unless both are
    # switched off too (docs/RFP_PIPELINESUITE.md section 6, RFP_HARVEST.md
    # 2.5).
    creds = dict(procore_login_email="bot@example.com", procore_login_password="pw")
    assert _harvest_settings(rfp_ingest_enabled=True, rfp_harvest_enabled=True, **creds).rfp_harvest_active
    assert not _harvest_settings(rfp_ingest_enabled=True, rfp_harvest_enabled=False, **creds).rfp_harvest_active
    assert not _harvest_settings(rfp_ingest_enabled=False, rfp_harvest_enabled=True, **creds).rfp_harvest_active
    assert _harvest_settings(rfp_ingest_enabled=True, rfp_harvest_enabled=True).rfp_harvest_active
    assert _harvest_settings(
        rfp_ingest_enabled=True, rfp_harvest_enabled=True, pipelinesuite_enabled=False
    ).rfp_harvest_active
    assert not _harvest_settings(
        rfp_ingest_enabled=True, rfp_harvest_enabled=True, pipelinesuite_enabled=False,
        rfp_harvest_email_enabled=False,
    ).rfp_harvest_active
    assert _harvest_settings(
        rfp_ingest_enabled=True, rfp_harvest_enabled=True, pipelinesuite_enabled=False,
        rfp_harvest_email_enabled=False, **creds
    ).rfp_harvest_active
    # The two upstream switches still gate PipelineSuite alone.
    assert not _harvest_settings(rfp_ingest_enabled=True, rfp_harvest_enabled=False).rfp_harvest_active
    assert not _harvest_settings(rfp_ingest_enabled=False, rfp_harvest_enabled=True).rfp_harvest_active


def test_pipelinesuite_settings_defaults_match_the_design_record():
    s = _harvest_settings()
    assert s.pipelinesuite_enabled is True
    assert s.pipelinesuite_tracking_pings_enabled is True
    assert s.pipelinesuite_min_request_interval_seconds == 2.0
    assert s.pipelinesuite_login_min_interval_seconds == 600
    assert s.pipelinesuite_login_max_failures == 3
    assert s.pipelinesuite_login_lock_seconds == 21600
    assert s.pipelinesuite_request_timeout_seconds == 30
    # No credential fields: the Project ID and Security Key come from the email.
    assert not any(name.startswith("pipelinesuite_login_") and name.endswith(("email", "password", "key"))
                   for name in Settings.model_fields)


@pytest.mark.parametrize(
    "overrides,names_var",
    [
        ({"pipelinesuite_min_request_interval_seconds": 0.4}, "PIPELINESUITE_MIN_REQUEST_INTERVAL_SECONDS"),
        ({"pipelinesuite_login_lock_seconds": 59}, "PIPELINESUITE_LOGIN_LOCK_SECONDS"),
        ({"pipelinesuite_login_min_interval_seconds": 59}, "PIPELINESUITE_LOGIN_MIN_INTERVAL_SECONDS"),
        ({"pipelinesuite_login_max_failures": 0}, "PIPELINESUITE_LOGIN_MAX_FAILURES"),
        ({"pipelinesuite_request_timeout_seconds": 0}, "PIPELINESUITE_REQUEST_TIMEOUT_SECONDS"),
    ],
)
def test_pipelinesuite_settings_guard_refuses_each_bad_value(overrides, names_var):
    """Validation mirrors procore_*, whether or not the harvester is on."""
    with pytest.raises(ValueError, match=names_var):
        _harvest_settings(pipelinesuite_enabled=False, **overrides)
    with pytest.raises(ValueError, match=names_var):
        _harvest_settings(pipelinesuite_enabled=True, **overrides)


def test_pipelinesuite_env_names_carry_the_documented_prefix(monkeypatch):
    monkeypatch.setenv("PIPELINESUITE_ENABLED", "false")
    monkeypatch.setenv("PIPELINESUITE_TRACKING_PINGS_ENABLED", "false")
    monkeypatch.setenv("PIPELINESUITE_LOGIN_LOCK_SECONDS", "3600")
    s = Settings(_env_file=None)
    assert s.pipelinesuite_enabled is False and s.pipelinesuite_tracking_pings_enabled is False
    assert s.pipelinesuite_login_lock_seconds == 3600


def test_rfp_harvest_file_cap_clamps_to_the_sandbox_run_cap():
    assert _harvest_settings().rfp_harvest_file_cap == 250
    assert _harvest_settings(rfp_harvest_max_files=500).rfp_harvest_file_cap == 250
    assert _harvest_settings(rfp_harvest_max_files=50).rfp_harvest_file_cap == 50
    assert _harvest_settings(rfp_harvest_max_files=500, rfp_ingest_max_files_per_run=300).rfp_harvest_file_cap == 300
    assert _harvest_settings(rfp_harvest_max_files=1, rfp_ingest_max_files_per_run=1).rfp_harvest_file_cap == 1


@pytest.mark.parametrize(
    "overrides,names_var",
    [
        ({"procore_min_request_interval_seconds": 0.4}, "PROCORE_MIN_REQUEST_INTERVAL_SECONDS"),
        ({"procore_min_request_interval_seconds": 0}, "PROCORE_MIN_REQUEST_INTERVAL_SECONDS"),
        ({"procore_login_lock_seconds": 59}, "PROCORE_LOGIN_LOCK_SECONDS"),
        ({"procore_login_min_interval_seconds": 59}, "PROCORE_LOGIN_MIN_INTERVAL_SECONDS"),
        ({"procore_login_max_failures": 0}, "PROCORE_LOGIN_MAX_FAILURES"),
        ({"procore_request_timeout_seconds": 0}, "PROCORE_REQUEST_TIMEOUT_SECONDS"),
        ({"rfp_harvest_concurrency": 0}, "RFP_HARVEST_CONCURRENCY"),
        ({"rfp_harvest_max_files": 0}, "RFP_HARVEST_MAX_FILES"),
        ({"rfp_harvest_max_total_bytes": 0}, "RFP_HARVEST_MAX_TOTAL_BYTES"),
        ({"rfp_harvest_poll_seconds": 4}, "RFP_HARVEST_POLL_SECONDS"),
        ({"rfp_harvest_reuse_days": -1}, "RFP_HARVEST_REUSE_DAYS"),
    ],
)
def test_rfp_harvest_settings_guard_refuses_each_bad_value(overrides, names_var):
    """Whether or not the slice is on, so a bad value surfaces at deploy
    time rather than when RFP_HARVEST_ENABLED flips."""
    with pytest.raises(ValueError, match=names_var):
        _harvest_settings(rfp_harvest_enabled=False, **overrides)
    with pytest.raises(ValueError, match=names_var):
        _harvest_settings(rfp_harvest_enabled=True, **overrides)


def test_rfp_harvest_settings_guard_accepts_the_boundaries():
    s = _harvest_settings(
        procore_min_request_interval_seconds=0.5,
        procore_login_lock_seconds=60,
        procore_login_min_interval_seconds=60,
        procore_login_max_failures=1,
        procore_request_timeout_seconds=0.1,
        rfp_harvest_concurrency=1,
        rfp_harvest_max_files=1,
        rfp_harvest_max_total_bytes=1,
        rfp_harvest_poll_seconds=5,
        rfp_harvest_reuse_days=0,
    )
    assert s.rfp_harvest_file_cap == 1


def test_rfp_harvest_env_names_carry_the_documented_prefixes(monkeypatch):
    monkeypatch.setenv("RFP_HARVEST_ENABLED", "true")
    monkeypatch.setenv("PROCORE_LOGIN_EMAIL", "bot@example.com")
    monkeypatch.setenv("PROCORE_LOGIN_PASSWORD", "pw")
    monkeypatch.setenv("RFP_HARVEST_MAX_FILES", "25")
    monkeypatch.setenv("PROCORE_LOGIN_LOCK_SECONDS", "3600")
    s = Settings(_env_file=None)
    assert s.rfp_harvest_enabled is True and s.procore_configured
    assert s.rfp_harvest_max_files == 25 and s.procore_login_lock_seconds == 3600


# ── NGEM portal settings (docs/RFP_NGEM_PORTAL.md section 8) ───────────────


def _ngem_settings(**over):
    return Settings(_env_file=None, **over)


def test_rfp_ngem_settings_defaults_match_the_design_record():
    s = _ngem_settings()
    assert s.rfp_ngem_enabled is False
    assert s.ngem_login_username == "" and s.ngem_login_password == "" and s.ngem_entry_url == ""
    assert s.rfp_ngem_schedule_times == "06:30,12:00" and s.rfp_ngem_schedule == [(6, 30), (12, 0)]
    assert s.rfp_ngem_catchup_hours == 4 and s.rfp_ngem_poll_seconds == 60
    assert s.rfp_ngem_auto_resolve_enabled is True
    assert s.rfp_ngem_max_list_pages == 20 and s.rfp_ngem_scan_timeout_seconds == 1800
    assert s.rfp_ngem_sweep_batch == 50 and s.rfp_ngem_queue_priority == 150
    # The scan rides ahead of the harvest backlog (they share the third claim pass).
    assert s.rfp_ngem_scan_queue_priority == 140 < s.rfp_ngem_queue_priority
    assert s.ngem_min_request_interval_seconds == 2.0 and s.ngem_login_min_interval_seconds == 600
    assert s.ngem_login_max_failures == 3 and s.ngem_login_lock_seconds == 21600
    assert s.ngem_request_timeout_seconds == 30.0
    assert s.ngem_configured is False and s.rfp_ngem_active is False


def test_rfp_ngem_configured_and_active_derivations():
    url = "https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=abc"
    creds = dict(ngem_login_username="acct", ngem_login_password="pw", ngem_entry_url=url)
    assert _ngem_settings(**creds).ngem_configured
    assert not _ngem_settings(ngem_login_username="acct", ngem_login_password="pw").ngem_configured
    assert not _ngem_settings(ngem_login_username="  ", ngem_login_password="pw", ngem_entry_url=url).ngem_configured
    assert not _ngem_settings(ngem_login_username="acct", ngem_entry_url=url).ngem_configured
    on = dict(rfp_ingest_enabled=True, rfp_ngem_enabled=True, **creds)
    assert _ngem_settings(**on).rfp_ngem_active
    assert not _ngem_settings(**{**on, "rfp_ngem_enabled": False}).rfp_ngem_active
    assert not _ngem_settings(**{**on, "rfp_ingest_enabled": False}).rfp_ngem_active
    assert not _ngem_settings(**{**on, "llm_queue_enabled": False}).rfp_ngem_active
    assert not _ngem_settings(rfp_ingest_enabled=True, rfp_ngem_enabled=True).rfp_ngem_active


@pytest.mark.parametrize(
    "raw, parsed",
    [
        ("06:30,12:00", [(6, 30), (12, 0)]),
        ("6:30", [(6, 30)]),
        (" 23:59 , 00:00 ", [(23, 59), (0, 0)]),
        (",".join(f"{h:02d}:00" for h in range(12)), [(h, 0) for h in range(12)]),
    ],
)
def test_rfp_ngem_schedule_parses_hh_mm_entries(raw, parsed):
    assert _ngem_settings(rfp_ngem_schedule_times=raw).rfp_ngem_schedule == parsed


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "noon", "24:00", "06:60", "06:30:00", "06:30,06:30", "6.30",
     ",".join(f"{h:02d}:00" for h in range(13))],
)
def test_rfp_ngem_schedule_refuses_junk(raw):
    with pytest.raises(ValueError, match="RFP_NGEM_SCHEDULE_TIMES"):
        _ngem_settings(rfp_ngem_schedule_times=raw)


@pytest.mark.parametrize(
    "overrides, names_var",
    [
        ({"ngem_entry_url": "https://evil.example/ResponseList.aspx"}, "NGEM_ENTRY_URL"),
        ({"ngem_entry_url": "http://supplier.ionwave.net/x"}, "NGEM_ENTRY_URL"),
        ({"rfp_ngem_catchup_hours": 0}, "RFP_NGEM_CATCHUP_HOURS"),
        ({"rfp_ngem_catchup_hours": 25}, "RFP_NGEM_CATCHUP_HOURS"),
        ({"rfp_ngem_poll_seconds": 4}, "RFP_NGEM_POLL_SECONDS"),
        ({"rfp_ngem_max_list_pages": 0}, "RFP_NGEM_MAX_LIST_PAGES"),
        ({"rfp_ngem_sweep_batch": 0}, "RFP_NGEM_SWEEP_BATCH"),
        ({"rfp_ngem_scan_timeout_seconds": 59}, "RFP_NGEM_SCAN_TIMEOUT_SECONDS"),
        ({"ngem_min_request_interval_seconds": 0.4}, "NGEM_MIN_REQUEST_INTERVAL_SECONDS"),
        ({"ngem_login_min_interval_seconds": 59}, "NGEM_LOGIN_MIN_INTERVAL_SECONDS"),
        ({"ngem_login_max_failures": 0}, "NGEM_LOGIN_MAX_FAILURES"),
        ({"ngem_login_lock_seconds": 59}, "NGEM_LOGIN_LOCK_SECONDS"),
        ({"ngem_request_timeout_seconds": 0}, "NGEM_REQUEST_TIMEOUT_SECONDS"),
    ],
)
def test_rfp_ngem_settings_guard_refuses_each_bad_value(overrides, names_var):
    """Whether or not the slice is on, so a bad value surfaces at deploy
    time rather than when RFP_NGEM_ENABLED flips."""
    with pytest.raises(ValueError, match=names_var):
        _ngem_settings(rfp_ngem_enabled=False, **overrides)
    with pytest.raises(ValueError, match=names_var):
        _ngem_settings(rfp_ngem_enabled=True, **overrides)


def test_rfp_ngem_settings_guard_accepts_the_boundaries():
    s = _ngem_settings(
        ngem_entry_url="https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=x",
        rfp_ngem_catchup_hours=1, rfp_ngem_poll_seconds=5, rfp_ngem_max_list_pages=1,
        rfp_ngem_sweep_batch=1, rfp_ngem_scan_timeout_seconds=60,
        ngem_min_request_interval_seconds=0.5, ngem_login_min_interval_seconds=60,
        ngem_login_max_failures=1, ngem_login_lock_seconds=60, ngem_request_timeout_seconds=0.1,
    )
    assert s.rfp_ngem_catchup_hours == 1


def test_rfp_ngem_env_names_carry_the_documented_prefixes(monkeypatch):
    monkeypatch.setenv("RFP_NGEM_ENABLED", "true")
    monkeypatch.setenv("NGEM_LOGIN_USERNAME", "acct")
    monkeypatch.setenv("NGEM_LOGIN_PASSWORD", "pw")
    monkeypatch.setenv("NGEM_ENTRY_URL", "https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=x")
    monkeypatch.setenv("RFP_NGEM_SCHEDULE_TIMES", "07:00")
    monkeypatch.setenv("NGEM_LOGIN_LOCK_SECONDS", "3600")
    s = Settings(_env_file=None)
    assert s.rfp_ngem_enabled is True and s.ngem_configured
    assert s.rfp_ngem_schedule == [(7, 0)] and s.ngem_login_lock_seconds == 3600
