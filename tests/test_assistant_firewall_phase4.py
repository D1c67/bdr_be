"""The phase 4 firewall additions (app/assistant/firewall.py;
docs/ASSISTANT_PHASE4_PLAN.md 3.13, 3.22, WI-9a) and the main wiring.

Invariants pinned here:

  payroll   every payroll GET of 3.13 is deny-listed, and a walk of the live
            route table finds no /payroll GET that is neither deny-listed,
            declared by a registered spec, one of the two aggregate reads
            the cp tools declare, nor one of the three /payroll/projects
            enrollment reads;
  analytics GET /analytics/activity is deny-listed (the per-person feed is
            reached only through the person_evidence card);
  reports   the person's report download and list are deny-listed, the
            build_report tool route is not;
  open      the plans assistant routes, the evidence routes and the new PM
            and CP aggregate routes are not deny-listed;
  main      app.main mounts the three phase 4 routers and, in a fresh
            interpreter, loads none of app.assistant.loop, shim, executor,
            jobs, spend or continue_job.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

import app.main
from app.assistant import firewall, registry

PAYROLL_DENIED = [
    ("GET", "/payroll/reports/{report_id}"),
    ("GET", "/payroll/reports/{report_id}/entries"),
    ("GET", "/payroll/reports/{report_id}/cpr/records"),
    ("GET", "/payroll/reports/{report_id}/cpr/records/{record_id}/download"),
    ("GET", "/payroll/reports/{report_id}/cpr/records/{record_id}/files/{file_id}/download"),
    ("GET", "/payroll/employees"),
    ("GET", "/payroll/employees/check-id"),
    ("GET", "/payroll/employees/{employee_id}"),
    ("GET", "/payroll/employees/{employee_id}/documents"),
    ("GET", "/payroll/employees/{employee_id}/documents/{doc_id}/download"),
    ("GET", "/payroll/classifications"),
    ("GET", "/payroll/rates"),
    ("GET", "/payroll/settings"),
    ("GET", "/payroll/profile"),
    ("GET", "/payroll/ignored-projects"),
]
HUMAN_REPORT_ROUTES = [
    ("GET", "/assistant/reports/{report_id}/download"),
    ("GET", "/assistant/reports"),
]
PLANS_ROUTES = [
    ("GET", "/ai-estimating/projects/{project_id}/runs"),
    ("GET", "/ai-estimating/runs/{run_id}/brief"),
    ("GET", "/ai-estimating/runs/{run_id}/documents"),
    ("GET", "/ai-estimating/runs/{run_id}/outputs/{category}/digest"),
    ("GET", "/ai-estimating/notes/search"),
    ("GET", "/ai-estimating/runs/{run_id}/read-pages/estimate"),
    ("POST", "/ai-estimating/runs/{run_id}/assistant-page-reads"),
    ("GET", "/ai-estimating/projects/{project_id}/estimate-preview"),
    ("POST", "/ai-estimating/projects/{project_id}/assistant-runs"),
]
OPEN_PHASE4 = PLANS_ROUTES + [
    ("POST", "/assistant/tools/reports"),
    ("GET", "/pm/rfis/open"),
    ("GET", "/pm/billing/summary"),
    ("GET", "/payroll/reports/summary"),
    ("GET", "/payroll/reports"),
    ("POST", "/projects/{project_id}/advance"),
]


def _route_table() -> set[tuple[str, str]]:
    out = set()
    for route in app.main.app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods or ():
                out.add((method, route.path_format))
    return out


@pytest.mark.parametrize("pair", PAYROLL_DENIED)
def test_every_payroll_read_of_3_13_is_denied(pair):
    assert pair in firewall.DENY_LIST
    assert pair in _route_table(), pair
    verdict = firewall.check(pair[0], pair[1], {pair})
    assert not verdict.allowed and verdict.reason == firewall.Verdict.DENY_LISTED


def test_the_payroll_route_walk_finds_nothing_open_by_accident():
    declared = registry.allowlist_for(registry.all_tools().values())
    walked = 0
    loose = []
    for method, path in sorted(_route_table()):
        if method != "GET" or not path.startswith("/payroll"):
            continue
        walked += 1
        pair = (method, path)
        if firewall.is_deny_listed(method, path):
            continue
        if pair in declared or pair in firewall.PAYROLL_SPEC_READS or pair in firewall.PAYROLL_OPEN_READS:
            continue
        loose.append(pair)
    assert walked >= 20
    assert loose == [], f"/payroll GETs neither denied, declared nor allowlisted: {loose}"


def test_the_payroll_allowlists_are_live_and_open():
    table = _route_table()
    assert len(firewall.PAYROLL_OPEN_READS) == 3
    assert all(p[1].startswith("/payroll/projects") for p in firewall.PAYROLL_OPEN_READS)
    for pair in firewall.PAYROLL_SPEC_READS | firewall.PAYROLL_OPEN_READS:
        assert pair in table, pair
        assert not firewall.is_deny_listed(*pair), pair
        assert pair in firewall.NOT_DENIED, pair


def test_the_per_person_activity_feed_is_denied():
    pair = ("GET", "/analytics/activity")
    assert pair in firewall.DENY_LIST and pair in _route_table()
    assert not firewall.check("GET", "/analytics/activity", {pair}).allowed


@pytest.mark.parametrize("pair", HUMAN_REPORT_ROUTES)
def test_the_human_report_routes_are_denied(pair):
    assert pair in firewall.DENY_LIST and pair in _route_table()
    verdict = firewall.check(pair[0], pair[1], {pair})
    assert not verdict.allowed and verdict.reason == firewall.Verdict.DENY_LISTED


def test_the_download_path_resolves_to_its_template_and_is_refused():
    scope = {"type": "http", "method": "GET", "headers": [], "query_string": b"",
             "path": "/assistant/reports/12121212-1212-4212-8212-121212121211/download"}
    assert firewall.resolve_path_format(app.main.app, scope) == "/assistant/reports/{report_id}/download"
    verdict = firewall.check_scope(app.main.app, scope, set(HUMAN_REPORT_ROUTES))
    assert not verdict.allowed and verdict.reason == "deny_listed"


@pytest.mark.parametrize("pair", OPEN_PHASE4)
def test_what_the_phase_4_tools_need_stays_open(pair):
    assert pair in _route_table(), pair
    assert not firewall.is_deny_listed(*pair), pair


def test_the_evidence_routes_are_mounted_and_open():
    evidence = [p for p in _route_table() if p[1].startswith("/analytics/evidence/")]
    assert len(evidence) >= 8
    assert [p for p in evidence if firewall.is_deny_listed(*p)] == []


def test_every_deny_pair_still_names_a_live_route():
    table = _route_table()
    assert sorted(p for p in firewall.DENY_LIST if p not in table) == []
    for pair in firewall.NOT_DENIED:
        assert pair in table and pair not in firewall.DENY_LIST, pair


def test_main_mounts_the_phase_4_routers_and_never_names_the_assistant_package():
    source = Path(app.main.__file__).read_text(encoding="utf-8")
    for name in ("plans_reader_assistant", "analytics_evidence", "assistant_reports"):
        assert f"app.include_router({name}.router" in source, name
    assert "app.assistant" not in source


def test_a_fresh_import_of_main_loads_no_engine_module():
    code = (
        "import sys, app.main\n"
        "bad = [m for m in ('app.assistant.loop', 'app.assistant.shim', 'app.assistant.executor',"
        " 'app.assistant.jobs', 'app.assistant.spend', 'app.assistant.continue_job') if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    root = Path(app.main.__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1:] in ([""], []), out.stdout
