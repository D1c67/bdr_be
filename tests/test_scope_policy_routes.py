"""Scope policy routes (app/routers/scope_policy.py): role matrix with
AI_ESTIMATING_STATE=live patched (Q20, Q24), drafts only in live, the
10-character override reason, activation validation, preview without a
write. Supabase is faked in memory; nothing reaches a database."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import features
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import scope_policy as router_mod
from app.services import scope_policy
from app.services.scope_policy_seed import SEED_POLICY

FIXTURE = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7154"
RUN_ID = "11111111-1111-4111-8111-111111111111"


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload, self.filters, self.ord, self.lim = db, table, "select", None, [], None, None

    def select(self, *_a, **_k):
        return self

    def insert(self, p):
        self.op, self.payload = "insert", p
        return self

    def update(self, p):
        self.op, self.payload = "update", p
        return self

    def upsert(self, p, on_conflict=None):
        self.op, self.payload = "upsert", (p, on_conflict)
        return self

    def eq(self, c, v):
        self.filters.append((c, v))
        return self

    def order(self, c, desc=False):
        self.ord = (c, desc)
        return self

    def limit(self, n):
        self.lim = n
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        match = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
        if self.op == "insert":
            row = dict(self.payload)
            row.setdefault("id", f"{self.table[:3]}-{len(rows) + 1:04d}-aaaa")
            if self.table == "ai_scope_policies":
                row["id"] = f"22222222-2222-4222-8222-{len(rows) + 1:012d}"
            rows.append(row)
            self.db.writes.append((self.table, "insert", row))
            return _Res([dict(row)])
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            self.db.writes.append((self.table, "update", self.payload))
            return _Res(match)
        if self.op == "upsert":
            p, conflict = self.payload
            keys = conflict.split(",")
            for r in rows:
                if all(r.get(k) == p.get(k) for k in keys):
                    r.update(p)
                    self.db.writes.append((self.table, "upsert", p))
                    return _Res([dict(r)])
            rows.append(dict(p))
            self.db.writes.append((self.table, "upsert", p))
            return _Res([dict(p)])
        out = [dict(r) for r in match]
        if self.ord:
            out.sort(key=lambda r: r.get(self.ord[0]) or 0, reverse=self.ord[1])
        return _Res(out[: self.lim] if self.lim else out)


class FakeSB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.writes: list = []

    def table(self, name):
        return _Q(self, name)


class Api:
    def __init__(self, monkeypatch, *, state: str = "live", enabled: bool = True):
        self.settings = get_settings().model_copy(update={"plans_reader_enabled": enabled, "ai_estimating_state": state})
        monkeypatch.setattr(router_mod, "get_settings", lambda: self.settings)
        monkeypatch.setattr(features, "get_settings", lambda: self.settings)
        self.sb = FakeSB({
            "plans_reader_runs": [{"id": RUN_ID}],
            "plans_reader_outputs": [{"run_id": RUN_ID, "scope": "main", "version": 1,
                                      "output": json.loads((FIXTURE / "outputs" / "main_none_v1.json").read_text())}],
        })
        monkeypatch.setattr(router_mod, "get_supabase", lambda: self.sb)
        self.audits: list = []
        monkeypatch.setattr(router_mod, "audit", lambda *a, **k: self.audits.append(a))
        self.role = Role.EXECUTIVE
        app = FastAPI()
        app.include_router(router_mod.router)
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="33333333-3333-4333-8333-333333333333",
                                                                         email="u@g3.com", role=self.role, is_active=True)
        self.client = TestClient(app)

    def as_(self, role: Role) -> TestClient:
        self.role = role
        return self.client


@pytest.fixture(autouse=True)
def _clean_policy():
    scope_policy.reset_active_policy()
    yield
    scope_policy.reset_active_policy()


ALL_INTERNAL = [Role.EXECUTIVE, Role.IT_ADMIN, Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER_MATERIALS,
                Role.ESTIMATING_ENGINEER_LABOR, Role.ACCOUNTANT]
EDITORS = {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN}


def _draft_doc() -> dict:
    doc = copy.deepcopy(SEED_POLICY)
    doc["notes"] = "draft from the test"
    return doc


@pytest.mark.parametrize("role", ALL_INTERNAL)
def test_active_policy_is_readable_by_every_internal_role_in_live(monkeypatch, role):
    api = Api(monkeypatch)
    res = api.as_(role).get("/ai-estimating/scope-policy")
    assert res.status_code == 200
    assert res.json()["sha256"] == scope_policy.policy_sha(SEED_POLICY) and res.json()["source"] == "seed"


def test_estimator_and_disabled_flag(monkeypatch):
    api = Api(monkeypatch)
    assert api.as_(Role.ESTIMATOR).get("/ai-estimating/scope-policy").status_code == 403
    off = Api(monkeypatch, enabled=False)
    assert off.as_(Role.EXECUTIVE).get("/ai-estimating/scope-policy").status_code == 404


@pytest.mark.parametrize("role", ALL_INTERNAL)
def test_validate_and_drafts_role_matrix(monkeypatch, role):
    api = Api(monkeypatch)
    c = api.as_(role)
    expect_ok = role in EDITORS
    res = c.post("/ai-estimating/scope-policy/validate", json={"doc": SEED_POLICY})
    assert res.status_code == (200 if expect_ok else 403)
    if expect_ok:
        assert res.json()["valid"] is True
    res = c.post("/ai-estimating/scope-policy/drafts", json={"doc": _draft_doc(), "notes": "first draft"})
    assert res.status_code == (201 if expect_ok else 403)
    if expect_ok:
        draft = res.json()
        assert draft["status"] == "draft" and draft["version"] == 2
        assert c.get("/ai-estimating/scope-policy/drafts").status_code == 200
        did = draft["id"]
        assert c.get(f"/ai-estimating/scope-policy/drafts/{did}").status_code == 200
        res = c.put(f"/ai-estimating/scope-policy/drafts/{did}", json={"notes": "edited"})
        assert res.status_code == 200 and res.json()["notes"] == "edited"


@pytest.mark.parametrize("role", ALL_INTERNAL)
def test_only_verify_roles_activate(monkeypatch, role):
    from app.services import scope_engine

    api = Api(monkeypatch)
    # every worker reads the active row through the table loader (nothing is pinned in process)
    scope_policy.set_active_policy_loader(scope_engine.table_policy_loader(lambda: api.sb, ttl_s=3600))
    assert scope_policy.active_policy().source == "seed"
    created = api.as_(Role.ESTIMATING_ADMIN).post("/ai-estimating/scope-policy/drafts", json={"doc": _draft_doc()}).json()
    res = api.as_(role).post(f"/ai-estimating/scope-policy/drafts/{created['id']}/activate")
    if role in (Role.EXECUTIVE, Role.IT_ADMIN):
        assert res.status_code == 200 and res.json()["status"] == "active"
        # the activation expired this process's loader cache, so the new row is read at once
        assert scope_policy.active_policy().version == 2 and scope_policy.active_policy().source == "table"
        assert any(a[1] == "scope_policy_activated" for a in api.audits)
        assert [r["status"] for r in api.sb.tables["ai_scope_policies"]] == ["active"]
    else:
        assert res.status_code == 403
        assert scope_policy.active_policy().source == "seed"


def test_activation_never_pins_and_replaces_the_active_row(monkeypatch):
    api = Api(monkeypatch)
    c = api.as_(Role.EXECUTIVE)
    first = c.post("/ai-estimating/scope-policy/drafts", json={"doc": _draft_doc()}).json()
    assert c.post(f"/ai-estimating/scope-policy/drafts/{first['id']}/activate").status_code == 200
    second = c.post("/ai-estimating/scope-policy/drafts", json={"doc": _draft_doc()}).json()
    assert c.post(f"/ai-estimating/scope-policy/drafts/{second['id']}/activate").status_code == 200
    statuses = {r["id"]: r["status"] for r in api.sb.tables["ai_scope_policies"]}
    assert statuses == {first["id"]: "archived", second["id"]: "active"}
    # no loader installed and nothing pinned: this process still answers the seed
    assert scope_policy.active_policy().source == "seed"


def test_activation_that_matches_no_draft_restores_the_active_row(monkeypatch):
    api = Api(monkeypatch)
    c = api.as_(Role.EXECUTIVE)
    first = c.post("/ai-estimating/scope-policy/drafts", json={"doc": _draft_doc()}).json()
    assert c.post(f"/ai-estimating/scope-policy/drafts/{first['id']}/activate").status_code == 200
    second = c.post("/ai-estimating/scope-policy/drafts", json={"doc": _draft_doc()}).json()
    real_draft = router_mod._draft

    def stale_draft(sb, did):
        row = real_draft(sb, did)
        for r in sb.tables["ai_scope_policies"]:
            if r["id"] == did:
                r["status"] = "archived"  # another request changed it after the read
        return row

    monkeypatch.setattr(router_mod, "_draft", stale_draft)
    res = c.post(f"/ai-estimating/scope-policy/drafts/{second['id']}/activate")
    assert res.status_code == 409
    statuses = {r["id"]: r["status"] for r in api.sb.tables["ai_scope_policies"]}
    assert statuses[first["id"]] == "active" and statuses[second["id"]] == "archived"


def test_drafts_answer_404_outside_live(monkeypatch):
    api = Api(monkeypatch, state="test")
    c = api.as_(Role.EXECUTIVE)
    assert c.get("/ai-estimating/scope-policy").status_code == 200
    assert c.get("/ai-estimating/scope-policy/drafts").status_code == 404
    assert c.post("/ai-estimating/scope-policy/drafts", json={"doc": _draft_doc()}).status_code == 404
    assert api.as_(Role.ESTIMATING_ADMIN).get("/ai-estimating/scope-policy").status_code == 403


def test_invalid_draft_is_refused_and_cannot_be_activated(monkeypatch):
    api = Api(monkeypatch)
    bad = _draft_doc()
    bad["rules"][0]["unless"] = ["drawn_with_quantity"]  # a hard rule may not carry unless
    res = api.as_(Role.EXECUTIVE).post("/ai-estimating/scope-policy/drafts", json={"doc": bad})
    assert res.status_code == 422
    api.sb.tables.setdefault("ai_scope_policies", []).append(
        {"id": "44444444-4444-4444-8444-444444444444", "version": 9, "status": "draft", "doc": bad, "sha256": "0" * 64})
    res = api.as_(Role.IT_ADMIN).post("/ai-estimating/scope-policy/drafts/44444444-4444-4444-8444-444444444444/activate")
    assert res.status_code == 422


@pytest.mark.parametrize("role", ALL_INTERNAL)
def test_overrides_role_matrix_and_reason(monkeypatch, role):
    api = Api(monkeypatch)
    c = api.as_(role)
    body = {"decision_key": "standard:raceway", "action": "accept", "note": "EMT confirmed by the engineer of record"}
    res = c.post(f"/ai-estimating/scope-policy/runs/{RUN_ID}/overrides", json=body)
    if role in (Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN):
        assert res.status_code == 201
        rows = api.sb.tables["plans_reader_scope_overrides"]
        assert len(rows) == 1 and rows[0]["action"] == "accept"
        again = c.post(f"/ai-estimating/scope-policy/runs/{RUN_ID}/overrides", json={**body, "action": "include"})
        assert again.status_code == 201 and len(api.sb.tables["plans_reader_scope_overrides"]) == 1
        short = c.post(f"/ai-estimating/scope-policy/runs/{RUN_ID}/overrides", json={**body, "note": "too short"})
        assert short.status_code == 422
        bad = c.post(f"/ai-estimating/scope-policy/runs/{RUN_ID}/overrides", json={**body, "action": "drop"})
        assert bad.status_code == 422
        assert c.get(f"/ai-estimating/scope-policy/runs/{RUN_ID}/overrides").json()["overrides"]
    else:
        assert res.status_code == 403
        assert "plans_reader_scope_overrides" not in api.sb.tables


def test_override_is_applied_at_once_only_with_enforcement_on(monkeypatch):
    api = Api(monkeypatch)
    calls: list = []
    monkeypatch.setattr(router_mod.scope_engine, "apply_override",
                        lambda sb, rid, row, settings=None: calls.append((rid, row["decision_key"]))
                        or ({"applied": True} if getattr(settings, "ai_scope_policy_enforce", False)
                            else {"applied": False, "reason": "enforcement_off"}))
    body = {"decision_key": "line:B12", "action": "exclude", "note": "dimmers by the lighting vendor"}
    res = api.as_(Role.EXECUTIVE).post(f"/ai-estimating/scope-policy/runs/{RUN_ID}/overrides", json=body)
    assert res.status_code == 201 and res.json()["applies"] == "at the next enforcement of this run"
    api.settings = api.settings.model_copy(update={"ai_scope_policy_enforce": True})
    res = api.as_(Role.EXECUTIVE).post(f"/ai-estimating/scope-policy/runs/{RUN_ID}/overrides", json=body)
    assert res.status_code == 201 and res.json()["applies"] == "now"
    assert calls == [(RUN_ID, "line:B12"), (RUN_ID, "line:B12")]


def test_preview_enforces_without_writing(monkeypatch):
    api = Api(monkeypatch)
    res = api.as_(Role.ESTIMATING_ADMIN).post("/ai-estimating/scope-policy/preview", json={"run_id": RUN_ID})
    assert res.status_code == 200
    data = res.json()
    assert data["counts"]["excluded_allowances"] == 10
    assert api.sb.writes == []
    assert api.as_(Role.ESTIMATING_ENGINEER_LABOR).post("/ai-estimating/scope-policy/preview",
                                                         json={"run_id": RUN_ID}).status_code == 403


def test_run_scope_read_and_unknown_ids(monkeypatch):
    api = Api(monkeypatch)
    c = api.as_(Role.ACCOUNTANT)
    res = c.get(f"/ai-estimating/scope-policy/runs/{RUN_ID}")
    assert res.status_code == 200 and res.json()["context"] is None
    assert c.get("/ai-estimating/scope-policy/runs/not-a-uuid").status_code == 404
    res = api.as_(Role.EXECUTIVE).post("/ai-estimating/scope-policy/runs/99999999-9999-4999-8999-999999999999/overrides",
                                        json={"decision_key": "x:y", "action": "accept", "note": "reason long enough"})
    assert res.status_code == 404
