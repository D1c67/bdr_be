"""app/routers/ai_pricing.py: the gates (404 while AI_PRICING_ENABLED is
off, 403 for the estimator and the accountant, the engineers only in
`live`), the price request (202, 409 while a job runs), the file_access
sources of the workbook and the quote downloads, the audited settings PUT
(Verify roles only) and the quote upload. Supabase, storage, the queue
and the audit log are faked; no network."""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import deps, features
from app.core.config import Settings
from app.core.deps import CurrentUser, get_current_user, require_ai_estimating, require_pricing_view
from app.core.features import require_plans_reader
from app.core.roles import Role
from app.routers import ai_pricing as rt
from app.services import file_access, llm_queue, pricing_run, storage
from tests import ai_pricing_fakes as fx

RUN_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "55555555-5555-4555-8555-555555555555"
PRICING_ID = "22222222-2222-4222-8222-222222222222"
QUOTE_ID = "33333333-3333-4333-8333-333333333333"


def _settings(**over) -> Settings:
    base = {"plans_reader_enabled": True, "ai_estimating_state": "live", "ai_pricing_enabled": True,
            "ai_pricing_state": "live", "ai_pricing_autorun": False}
    base.update(over)
    return Settings(_env_file=None, **base)


class Api:
    def __init__(self, monkeypatch, settings: Settings | None = None):
        self.settings = settings or _settings()
        self.role = Role.EXECUTIVE
        self.db = fx.FakeDB({
            "plans_reader_runs": [{"id": RUN_ID, "name": "Run", "status": "done", "project_id": PROJECT_ID}],
            "plans_reader_pricing": [{"id": PRICING_ID, "run_id": RUN_ID, "version": 1, "status": "priced", "stale": False,
                                      "storage_path": f"plans-reader/{RUN_ID}/pricing/v1/x.xlsx", "filename": "x.xlsx",
                                      "verify_draft": {"bid_total": "123.45"}, "categories": {}}],
            "plans_reader_priced_lines": [{"pricing_id": PRICING_ID, "line_id": "B1", "line": {"line_id": "B1", "labor_hours": "1"}}],
            "vendor_quote_extractions": [{"id": QUOTE_ID, "run_id": RUN_ID, "file_sha256": "f" * 64, "filename": "q.pdf",
                                          "storage_path": f"plans-reader/{RUN_ID}/quotes/q.pdf", "category": "Lighting",
                                          "status": "parsed", "vendor": "V", "selected": False}],
            "ai_pricing_settings": [{"id": 1, "policy": {}, "version": 1}],
            "llm_jobs": [],
        })
        self.audits: list[tuple] = []
        self.accesses: list[dict] = []
        self.enqueued: list[dict] = []
        self.uploads: list[tuple[str, int]] = []
        for mod in (rt, features, deps):
            monkeypatch.setattr(mod, "get_settings", lambda: self.settings)
        monkeypatch.setattr(rt, "get_supabase", lambda: self.db)
        monkeypatch.setattr(rt, "audit", lambda *a: self.audits.append(a))
        monkeypatch.setattr(file_access, "record", lambda user, **kw: self.accesses.append(kw))
        monkeypatch.setattr(storage, "signed_url", lambda path, ttl=None, **k: f"https://signed/{path}?dl={k.get('download')}")
        monkeypatch.setattr(storage, "upload_file", lambda path, data, ct, **k: self.uploads.append((path, len(data))))
        monkeypatch.setattr(llm_queue, "enqueue", lambda job_type, **kw: self.enqueued.append({"job_type": job_type, **kw}) or {"id": "job-1", "status": "queued"})
        monkeypatch.setattr(rt.CostLibrary, "load", classmethod(lambda cls, sb, *, exclude_projects=(), **k: fx.library()))
        rt._LIBRARY_CACHE.clear()
        app = FastAPI()
        app.include_router(rt.router)
        app.dependency_overrides[get_current_user] = self._user
        self.client = TestClient(app, raise_server_exceptions=True)

    def _user(self) -> CurrentUser:
        return CurrentUser(id="user-1", email="u@g3.com", role=self.role, is_active=True)


@pytest.fixture
def api(monkeypatch):
    return Api(monkeypatch)


def test_every_route_carries_the_three_gates():
    for route in rt.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert {require_plans_reader, require_ai_estimating, require_pricing_view} <= calls, route.path


def test_estimator_is_refused_and_accountant_never_sees_money(api):
    api.role = Role.ESTIMATOR
    r = api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}")
    assert r.status_code in (403, 404)
    api.role = Role.ACCOUNTANT
    r = api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}")
    assert r.status_code == 403
    assert "verify_draft" not in r.text and "bid_total" not in r.text and "123.45" not in r.text
    r = api.client.get(f"/ai-estimating/pricing/versions/{PRICING_ID}/lines")
    assert r.status_code == 403 and "labor_hours" not in r.text


def test_404_while_ai_pricing_is_off_and_engineers_only_in_live(monkeypatch):
    api = Api(monkeypatch, _settings(ai_pricing_enabled=False))
    r = api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}")
    assert r.status_code == 404 and r.json()["detail"] == "Not Found"
    api = Api(monkeypatch, _settings(ai_pricing_state="test"))
    api.role = Role.ESTIMATING_ENGINEER_LABOR
    assert api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}").status_code == 403
    api.role = Role.EXECUTIVE
    assert api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}").status_code == 200
    live = Api(monkeypatch)
    live.role = Role.ESTIMATING_ENGINEER_MATERIALS
    r = live.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}")
    assert r.status_code == 200 and r.json()["latest"]["verify_draft"]["bid_total"] == "123.45"


def test_run_status_and_lines(api):
    r = api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}")
    body = r.json()
    assert r.status_code == 200 and body["latest"]["version"] == 1 and body["job"] is None and body["quotes"] == 1
    assert "categories" not in body["latest"]
    r = api.client.get(f"/ai-estimating/pricing/versions/{PRICING_ID}/lines")
    assert r.status_code == 200 and r.json()["lines"] == [{"line_id": "B1", "labor_hours": "1"}]
    assert api.client.get(f"/ai-estimating/pricing/runs/{uuid.uuid4()}").status_code == 404
    assert api.client.get("/ai-estimating/pricing/runs/not-a-uuid").status_code == 404
    assert api.client.get(f"/ai-estimating/pricing/versions/{uuid.uuid4()}/lines").status_code == 404


def test_version_lines_carry_the_chosen_quote_reconciliation_and_unpriced(api):
    """The pricing tab reads categories (the chosen quote per category),
    quality_summary (the reconciliation) and unpriced from the lines route;
    the light listings (GET runs/{id}) keep dropping them."""
    row = api.db.tables["plans_reader_pricing"][0]
    row["categories"] = {"Lighting": {"source": "quote", "quote_id": QUOTE_ID, "vendor": "V"}}
    row["quality_summary"] = {"strong": 1, "reconciliation": {"quoted_total": "10"}}
    row["unpriced"] = [{"line_id": "B9", "reason": "no history"}]
    version = api.client.get(f"/ai-estimating/pricing/versions/{PRICING_ID}/lines").json()["version"]
    assert version["categories"]["Lighting"]["quote_id"] == QUOTE_ID
    assert version["quality_summary"]["reconciliation"] == {"quoted_total": "10"}
    assert version["unpriced"] == [{"line_id": "B9", "reason": "no history"}]
    assert version["verify_draft"]["bid_total"] == "123.45" and version["id"] == PRICING_ID
    latest = api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}").json()["latest"]
    assert not {"categories", "quality_summary", "unpriced"} & set(latest)
    # The Accountant never reaches it (require_pricing_view).
    api.role = Role.ACCOUNTANT
    assert api.client.get(f"/ai-estimating/pricing/versions/{PRICING_ID}/lines").status_code == 403


def test_price_request_queues_once_and_409s_while_a_job_runs(api):
    r = api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/price")
    assert r.status_code == 202 and r.json()["job"]["id"] == "job-1"
    assert api.enqueued[0]["job_type"] == "ai_pricing" and api.enqueued[0]["target_id"] == RUN_ID
    assert api.audits[-1][1] == "ai_pricing.price_requested"
    api.db.tables["llm_jobs"].append({"id": "job-1", "job_type": "ai_pricing", "target_id": RUN_ID, "status": "queued"})
    assert api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/price").status_code == 409
    api.role = Role.ESTIMATING_ENGINEER_LABOR
    assert api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/price").status_code == 403
    api.role = Role.EXECUTIVE
    api.db.tables["llm_jobs"].clear()
    api.db.tables["plans_reader_runs"][0]["status"] = "reading"
    assert api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/price").status_code == 409


def test_price_request_503_when_the_queue_is_not_wired(api, monkeypatch):
    def unwired(job_type, **kw):
        raise KeyError(job_type)

    monkeypatch.setattr(llm_queue, "enqueue", unwired)
    assert api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/price").status_code == 503


def test_workbook_and_quote_downloads_record_their_file_access_sources(api):
    r = api.client.get(f"/ai-estimating/pricing/versions/{PRICING_ID}/xlsx")
    assert r.status_code == 200 and r.json()["url"].endswith("dl=x.xlsx")
    assert api.accesses[-1]["source"] == rt.SOURCE_ESTIMATE == "ai_pricing_estimate"
    assert api.accesses[-1]["file_id"] == PRICING_ID and api.accesses[-1]["project_id"] == PROJECT_ID
    r = api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}/quotes/{QUOTE_ID}/download")
    assert r.status_code == 200 and "q.pdf" in r.json()["url"]
    assert api.accesses[-1]["source"] == rt.SOURCE_QUOTE == "ai_pricing_quote" and api.accesses[-1]["file_id"] == QUOTE_ID
    api.db.tables["plans_reader_pricing"][0]["storage_path"] = None
    assert api.client.get(f"/ai-estimating/pricing/versions/{PRICING_ID}/xlsx").status_code == 404


def test_quote_upload_list_select_and_duplicate(api):
    from tests.test_vendor_quotes import GRAYBAR_STYLE, make_pdf

    pdf = make_pdf(GRAYBAR_STYLE)
    r = api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/quotes", files={"file": ("Graybar Quote.pdf", io.BytesIO(pdf), "application/pdf")},
                        data={"category": "Lighting"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "parsed" and body["vendor"] == "Graybar" and body["amount_pretax"] == "13000.00"
    assert "storage_path" not in body and api.uploads[0][0].startswith(f"plans-reader/{RUN_ID}/quotes/")
    assert api.audits[-1][1] == "ai_pricing.quote_uploaded"
    dup = api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/quotes", files={"file": ("again.pdf", io.BytesIO(pdf), "application/pdf")})
    assert dup.status_code == 409
    bad = api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/quotes", files={"file": ("x.pdf", io.BytesIO(b"hello"), "application/pdf")})
    assert bad.status_code == 422
    unknown_cat = api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/quotes", files={"file": ("x.pdf", io.BytesIO(pdf), "application/pdf")},
                                  data={"category": "Nope"})
    assert unknown_cat.status_code == 422
    listed = api.client.get(f"/ai-estimating/pricing/runs/{RUN_ID}/quotes").json()["quotes"]
    assert len(listed) == 2 and all("storage_path" not in q for q in listed)
    sel = api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/quotes/{QUOTE_ID}/select", json={"selected": True})
    assert sel.status_code == 200 and sel.json()["selected"] is True
    api.role = Role.ESTIMATING_ENGINEER_LABOR
    assert api.client.post(f"/ai-estimating/pricing/runs/{RUN_ID}/quotes", files={"file": ("x.pdf", io.BytesIO(pdf), "application/pdf")}).status_code == 403


def test_lookup_returns_aggregates_only(api):
    r = api.client.get("/ai-estimating/pricing/lookup", params={"description": "DUPLEX RECEPTACLE", "uom": "EA", "quantity": 4})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok" and body["labor_hours"]["n_projects"] == 12
    assert body["extended"]["hours"] == pytest.approx(1.6)
    assert not {"project_id", "project_name", "gc_name"} & set(body)
    r = api.client.get("/ai-estimating/pricing/lookup", params={"description": "#12 THHN", "uom": "EA"})
    assert r.json()["status"] == "uom_conflict"


def test_settings_get_and_audited_put_for_verify_roles_only(api):
    r = api.client.get("/ai-estimating/pricing/settings")
    assert r.status_code == 200 and r.json()["source"] == "default"
    body = {"policy": {"hour_uplift": "1.25"}}
    api.role = Role.ESTIMATING_ADMIN  # a pricing writer, not a Verify role
    assert api.client.put("/ai-estimating/pricing/settings", json=body).status_code == 403
    api.role = Role.ESTIMATING_ENGINEER_LABOR
    assert api.client.put("/ai-estimating/pricing/settings", json=body).status_code == 403
    api.role = Role.EXECUTIVE
    r = api.client.put("/ai-estimating/pricing/settings", json=body)
    assert r.status_code == 200 and r.json()["policy"]["hour_uplift"] == "1.25" and r.json()["version"] == 2
    audit_row = api.audits[-1]
    assert audit_row[1] == "ai_pricing.settings_updated" and audit_row[2] == "ai_pricing_settings"
    assert audit_row[4]["version"] == 2 and "hour_uplift" in audit_row[4]["changed"]
    assert api.client.get("/ai-estimating/pricing/settings").json()["source"] == "table"
    bad = api.client.put("/ai-estimating/pricing/settings", json={"policy": {"labor_rates": {"prevailing_wage": "1"}}})
    assert bad.status_code == 422
    api.role = Role.IT_ADMIN
    assert api.client.put("/ai-estimating/pricing/settings", json={"policy": {}}).status_code == 200


def test_pricing_run_module_is_what_the_router_calls():
    assert rt.pricing_run is pricing_run
