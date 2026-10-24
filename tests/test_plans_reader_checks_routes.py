"""Checks routes (app/routers/plans_reader_checks.py, WS4): the role matrix
with AI_ESTIMATING_STATE=live patched (every internal role reads the report,
the Accountant is read-only, overrides need AI_GATE_OVERRIDE_ROLES), the
10-character override reason, the gate_summary recompute after an override,
the recount request (engineers, dev only) and the gate status rows.
Supabase is faked in memory; nothing reaches a database or a model."""

from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import features
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import plans_reader_checks as router_mod
from app.services import plans_reader_gates as G

RUN_ID = "11111111-1111-4111-8111-111111111111"
OTHER_RUN = "22222222-2222-4222-8222-222222222222"
USER_ID = "33333333-3333-4333-8333-333333333333"
ALL_INTERNAL = [Role.EXECUTIVE, Role.IT_ADMIN, Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER_MATERIALS,
                Role.ESTIMATING_ENGINEER_LABOR, Role.ACCOUNTANT]
OVERRIDERS = {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN}


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload, self.filters = db, table, "select", None, []

    def select(self, *_a, **_k):
        return self

    def update(self, p):
        self.op, self.payload = "update", p
        return self

    def insert(self, p):
        self.op, self.payload = "insert", p
        return self

    def eq(self, c, v):
        self.filters.append((c, v))
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        match = [r for r in rows if all(str(r.get(c)) == str(v) for c, v in self.filters)]
        if self.op == "insert":
            row = dict(self.payload)
            row.setdefault("id", f"{self.table[:4]}-{len(rows) + 1:04d}")
            rows.append(row)
            self.db.writes.append((self.table, "insert", row))
            return _Res([dict(row)])
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            self.db.writes.append((self.table, "update", dict(self.payload), list(self.filters)))
            return _Res([dict(r) for r in match])
        return _Res([dict(r) for r in match])


class FakeSB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.writes: list = []

    def table(self, name):
        return _Q(self, name)


def _summary(*, blocking_codes=("new_devices_no_branch", "linear_in_ea"), warnings=("census_floor",), enforced=True) -> dict:
    return {
        "schema": "bdr.gate_report/1", "stage": "final", "gates_version": G.GATE_VERSION, "output_version": 1, "mode": "tools",
        "enforced": enforced, "blocking": bool(blocking_codes), "codes": sorted(blocking_codes), "warnings": sorted(warnings),
        "counts": {"block": len(blocking_codes), "warn": len(warnings)},
        "evaluated": ["new_devices_no_branch", "linear_in_ea", "unit_class", "census_floor", "partial_basis", "capability_denial",
                      "excluded_system", "fact_conflict", "unsourced_line", "orphan_rows", "arithmetic"],
        "not_applicable": ["branch_ratio"], "manual_lines_unapproved": 0, "moved_allowances": [], "evaluated_at": "2026-10-22T09:00:00-07:00",
    }


def _findings() -> list[dict]:
    rows = [
        {"id": "f-1", "run_id": RUN_ID, "output_version": 1, "stage": "final", "gate_id": "new_devices_no_branch", "severity": "block", "route": "human",
         "status": "open", "target": {"family": "branch", "row_ids": ["B1", "B2"]}, "message": "148 EA of new devices and 0 FT of branch conductor", "evidence": {"devices": 148}},
        {"id": "f-2", "run_id": RUN_ID, "output_version": 1, "stage": "final", "gate_id": "linear_in_ea", "severity": "block", "route": "human",
         "status": "open", "target": {"line_id": "B7", "item_class": "lighting_track"}, "message": "B7: lighting_track must be FT", "evidence": {}},
        {"id": "f-3", "run_id": RUN_ID, "output_version": 1, "stage": "final", "gate_id": "census_floor", "severity": "warn", "route": "none",
         "status": "open", "target": {"sheet_key": "D1P8", "family": "demo_x"}, "message": "ED3.01 demo_x 34 of 39", "evidence": {"floor": 39, "accounted": 34}},
        {"id": "f-4", "run_id": RUN_ID, "output_version": 1, "stage": "pricing", "gate_id": "uom_conflict_quote", "severity": "block", "route": "human",
         "status": "open", "target": {"line_id": "B9"}, "message": "quote is per FT, line is EA", "evidence": {}},
        {"id": "f-5", "run_id": RUN_ID, "output_version": 1, "stage": "final", "gate_id": "partial_basis", "severity": "block", "route": "human",
         "status": "resolved", "target": {"sheet_key": "D1P8", "row_ids": ["B3"]}, "message": "resolved earlier", "evidence": {}, "resolved_by": USER_ID},
        {"id": "f-9", "run_id": OTHER_RUN, "output_version": 1, "stage": "final", "gate_id": "orphan_rows", "severity": "block", "route": "human",
         "status": "open", "target": {"row_ids": ["C1"]}, "message": "other run", "evidence": {}},
    ]
    for r in rows:
        r["fingerprint"] = G.GateFinding.from_row(r).fingerprint
    return rows


class Api:
    def __init__(self, monkeypatch, *, state: str = "live", enabled: bool = True, environment: str = "development", summary: dict | None = None):
        self.settings = get_settings().model_copy(update={"plans_reader_enabled": enabled, "ai_estimating_state": state, "environment": environment,
                                                           "plans_reader_gates_blocking": True})
        monkeypatch.setattr(router_mod, "get_settings", lambda: self.settings)
        monkeypatch.setattr(features, "get_settings", lambda: self.settings)
        self.sb = FakeSB({
            "plans_reader_runs": [{"id": RUN_ID, "status": "done", "autonomy_state": "blocked", "gate_summary": _summary() if summary is None else summary},
                                  {"id": OTHER_RUN, "status": "done", "autonomy_state": "not_evaluated", "gate_summary": None}],
            "plans_reader_gate_findings": _findings(),
        })
        monkeypatch.setattr(router_mod, "get_supabase", lambda: self.sb)
        self.audits: list = []
        monkeypatch.setattr(router_mod, "audit", lambda *a, **k: self.audits.append(a))
        self.autonomy: list = []
        from app.services import plans_reader_runs

        monkeypatch.setattr(plans_reader_runs, "recompute_autonomy", lambda sb, run_id, **kw: self.autonomy.append(run_id))
        monkeypatch.setattr(router_mod, "RECOUNT_REQUESTER", None)
        self.role = Role.EXECUTIVE
        app = FastAPI()
        app.include_router(router_mod.router)
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=USER_ID, email="u@g3.com", role=self.role, is_active=True)
        self.client = TestClient(app)

    def as_(self, role: Role) -> TestClient:
        self.role = role
        return self.client


# ---------------------------------------------------------------------------
# GET /checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ALL_INTERNAL)
def test_every_internal_role_reads_the_report(monkeypatch, role):
    api = Api(monkeypatch)
    r = api.as_(role).get(f"/ai-estimating/runs/{RUN_ID}/checks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == RUN_ID and body["run_status"] == "done" and body["autonomy_state"] == "blocked"
    assert body["pricing_allowed"] == {"allowed": False, "reasons": ["linear_in_ea", "new_devices_no_branch", "uom_conflict_quote"]} or \
        body["pricing_allowed"]["allowed"] is False
    assert {f["id"] for f in body["findings"]} == {"f-1", "f-2", "f-3", "f-4", "f-5"}  # never another run's rows
    assert body["findings"][0]["status"] == "open" and body["findings"][0]["severity"] == "block"
    assert body["findings"][-1]["id"] == "f-5"  # resolved rows sort last
    assert body["stages"] == {"final": 4, "pricing": 1}
    assert body["gate_report"]["blocking"] is True and {c["code"] for c in body["gate_report"]["checks"]} == {"new_devices_no_branch", "linear_in_ea", "census_floor", "uom_conflict_quote"}
    by_gate = {g["gate_id"]: g for g in body["gates"]}
    assert by_gate["new_devices_no_branch"] == {"gate_id": "new_devices_no_branch", "status": "block", "count": 1}
    assert by_gate["census_floor"]["status"] == "warn" and by_gate["unit_class"]["status"] == "pass"
    assert by_gate["branch_ratio"]["status"] == "not_applicable" and by_gate["lost_sheets"]["status"] == "not_run"
    assert by_gate["uom_conflict_quote"]["status"] == "block" and by_gate["partial_basis"]["status"] == "pass"  # resolved rows are not active
    assert body["can_override"] is (role in OVERRIDERS)
    assert body["can_request_recount"] is (role != Role.ACCOUNTANT)
    assert body["enforced"] is True
    f1 = next(f for f in body["findings"] if f["id"] == "f-1")
    assert f1["line_ids"] == ["B1", "B2"] and f1["blocking"] is True and f1["route"] == "human"


def test_report_without_a_gate_summary_reads_the_findings_and_refuses_pricing_by_absence(monkeypatch):
    api = Api(monkeypatch)
    r = api.as_(Role.IT_ADMIN).get(f"/ai-estimating/runs/{OTHER_RUN}/checks")
    assert r.status_code == 200
    body = r.json()
    assert body["gate_summary"] is None and body["pricing_allowed"]["allowed"] is False and body["pricing_allowed"]["reasons"] == ["orphan_rows"]
    assert {g["status"] for g in body["gates"]} <= {"block", "not_run"}
    empty = Api(monkeypatch)
    empty.sb.tables["plans_reader_gate_findings"] = []
    r = empty.as_(Role.IT_ADMIN).get(f"/ai-estimating/runs/{OTHER_RUN}/checks")
    assert r.json()["pricing_allowed"] == {"allowed": False, "reasons": ["gates_not_run"]}


def test_unknown_run_and_bad_ids_are_404(monkeypatch):
    api = Api(monkeypatch)
    assert api.as_(Role.EXECUTIVE).get("/ai-estimating/runs/not-a-uuid/checks").status_code == 404
    assert api.as_(Role.EXECUTIVE).get("/ai-estimating/runs/99999999-9999-4999-8999-999999999999/checks").status_code == 404


def test_feature_gates(monkeypatch):
    off = Api(monkeypatch, enabled=False)
    assert off.as_(Role.EXECUTIVE).get(f"/ai-estimating/runs/{RUN_ID}/checks").status_code == 404
    test_state = Api(monkeypatch, state="test")
    assert test_state.as_(Role.ESTIMATING_ENGINEER_LABOR).get(f"/ai-estimating/runs/{RUN_ID}/checks").status_code == 403
    assert test_state.as_(Role.EXECUTIVE).get(f"/ai-estimating/runs/{RUN_ID}/checks").status_code == 200
    assert test_state.as_(Role.ESTIMATOR).get(f"/ai-estimating/runs/{RUN_ID}/checks").status_code == 403


# ---------------------------------------------------------------------------
# POST override
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ALL_INTERNAL)
def test_override_role_matrix(monkeypatch, role):
    api = Api(monkeypatch)
    r = api.as_(role).post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/f-1/override", json={"reason": "Existing circuits confirmed on site walk"})
    if role in OVERRIDERS:
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["finding"]["status"] == "overridden" and body["finding"]["resolved_by"] == USER_ID and body["finding"]["note"].startswith("Existing")
        assert body["finding"]["resolved_at"].endswith(("-07:00", "-08:00"))
        # the run's gate_summary is recomputed from the remaining active final findings
        assert body["gate_summary"]["codes"] == ["linear_in_ea"] and body["gate_summary"]["enforced"] is True
        assert body["gate_summary"]["evaluated"] == _summary()["evaluated"] and body["gate_summary"]["mode"] == "tools"
        run = api.sb.tables["plans_reader_runs"][0]
        assert run["gate_summary"]["codes"] == ["linear_in_ea"]
        assert api.autonomy == [RUN_ID]
        assert api.audits and api.audits[0][1] == "ai_estimating_check_overridden" and api.audits[0][4]["finding_id"] == "f-1"
        assert G.pricing_allowed(run) == (False, ["linear_in_ea"])
    else:
        assert r.status_code == 403, r.text
        assert api.sb.tables["plans_reader_gate_findings"][0]["status"] == "open" and not api.sb.writes


def test_override_reason_length_and_states(monkeypatch):
    api = Api(monkeypatch)
    c = api.as_(Role.EXECUTIVE)
    assert c.post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/f-1/override", json={"reason": "too short"}).status_code == 422
    assert c.post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/f-1/override", json={"reason": "          x"}).status_code == 422
    assert c.post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/f-1/override", json={}).status_code == 422
    assert not api.sb.writes
    assert c.post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/f-5/override", json={"reason": "already resolved, trying again"}).status_code == 409
    assert c.post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/f-9/override", json={"reason": "belongs to another run"}).status_code == 404
    assert c.post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/nope/override", json={"reason": "no such finding here"}).status_code == 404
    assert c.post(f"/ai-estimating/runs/{OTHER_RUN}/checks/findings/f-9/override", json={"reason": "no summary on this run"}).status_code == 200
    assert api.sb.tables["plans_reader_runs"][1]["gate_summary"] is None  # nothing to recompute without the marker
    # overriding every block makes pricing allowed again (reasons empty)
    assert c.post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/f-1/override", json={"reason": "existing circuits confirmed"}).status_code == 200
    body = c.post(f"/ai-estimating/runs/{RUN_ID}/checks/findings/f-2/override", json={"reason": "track removed by the each per the GC"}).json()
    assert body["gate_summary"]["codes"] == [] and body["gate_summary"]["blocking"] is False
    assert G.pricing_allowed(api.sb.tables["plans_reader_runs"][0]) == (True, [])
    report = c.get(f"/ai-estimating/runs/{RUN_ID}/checks").json()
    assert {f["id"]: f["status"] for f in report["findings"]}["f-2"] == "overridden"
    assert next(g for g in report["gates"] if g["gate_id"] == "linear_in_ea")["status"] == "pass"


# ---------------------------------------------------------------------------
# POST recount
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ALL_INTERNAL)
def test_recount_request_role_matrix_in_dev(monkeypatch, role):
    api = Api(monkeypatch)
    r = api.as_(role).post(f"/ai-estimating/runs/{RUN_ID}/checks/recount", json={"sheet_key": "D1P8", "family": "demo_x", "note": "please recount the X marks"})
    if role == Role.ACCOUNTANT:
        assert r.status_code == 403 and not api.sb.writes
        return
    assert r.status_code == 202, r.text
    body = r.json()
    assert [f["id"] for f in body["requested"]] == ["f-3"] and body["queued"] == 0
    assert body["requested"][0]["status"] == "routed" and body["requested"][0]["route"] == "recount" and body["requested"][0]["note"] == "please recount the X marks"
    assert api.audits[-1][1] == "ai_estimating_recount_requested"


def test_recount_request_is_404_in_production_and_404_without_a_match(monkeypatch):
    prod = Api(monkeypatch, environment="production")
    assert prod.as_(Role.EXECUTIVE).post(f"/ai-estimating/runs/{RUN_ID}/checks/recount", json={"note": "x"}).status_code == 404
    api = Api(monkeypatch)
    assert api.as_(Role.EXECUTIVE).post(f"/ai-estimating/runs/{RUN_ID}/checks/recount", json={"sheet_key": "D9P9"}).status_code == 404
    assert api.as_(Role.EXECUTIVE).post(f"/ai-estimating/runs/{RUN_ID}/checks/recount", json={"finding_id": "f-1"}).status_code == 404  # not a census finding
    seen: list = []
    monkeypatch.setattr(router_mod, "RECOUNT_REQUESTER", lambda sb, run, findings, user, note: seen.append((run["id"], [f.gate_id for f in findings], note)) or ["g1"])
    r = api.as_(Role.ESTIMATING_ENGINEER_LABOR).post(f"/ai-estimating/runs/{RUN_ID}/checks/recount", json={"finding_id": "f-3", "note": ""})
    assert r.status_code == 202 and r.json()["queued"] == 1 and seen == [(RUN_ID, ["census_floor"], None)]


def test_gate_statuses_helper_covers_every_gate_id():
    from app.services.estimating_contracts import GATE_IDS

    rows = router_mod.gate_statuses(None, [])
    assert [r["gate_id"] for r in rows] == list(GATE_IDS) and {r["status"] for r in rows} == {"not_run"}
    summary = copy.deepcopy(_summary(blocking_codes=(), warnings=()))
    rows = {r["gate_id"]: r["status"] for r in router_mod.gate_statuses(summary, [])}
    assert rows["new_devices_no_branch"] == "pass" and rows["branch_ratio"] == "not_applicable" and rows["uom_conflict_quote"] == "not_run"
    assert set(router_mod.GATE_STATUSES) == {"pass", "block", "warn", "not_applicable", "not_run"}
