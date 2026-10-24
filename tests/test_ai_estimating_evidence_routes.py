"""routers/ai_estimating_evidence.py (WS8 wave 3) and the AI Estimating
security matrix (spec 5.5, section 6 WS8 acceptance):

- the external estimator gets 403 or 404 on EVERY AI Estimating route
  (plans_reader, ai_pricing and the evidence router), reads and writes;
- the Accountant never receives a money field from any route (a recursive
  key scan over every JSON answer of the evidence router), from the chat
  tool explain_row, or from a thread read (the restricted answer comes back
  redacted through the legacy chat route, which passes no viewer);
- pricing viewers get the price chips, the pricing params of the autonomy
  status and the sign-off; the sign-off rules (Verify roles only, latest
  priced version, `verified` needs ready or an explicit note per failing
  check, `rejected` needs a note) are enforced and audited;
- the annotated plans code kind: list, download, preview and a re-render
  from the stored evidence (claims and releases the run's change slot);
  the legacy deliverable routes 404 the kind;
- the crop route renders a PNG of one document page.

Supabase, storage, the audit log and the file access log are faked; the
numbers are synthetic; no network.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.core import deps, features
from app.core.config import Settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import ai_estimating_evidence as ev_rt
from app.routers import ai_pricing as pricing_rt
from app.routers import plans_reader as pr_rt
from app.services import file_access, storage
from app.services import plans_reader_chat as chat
from app.services import plans_reader_chat_tools as chat_tools
from app.services import plans_reader_evidence as evidence_mod
from app.services import plans_reader_tools as tools
from tests import ai_pricing_fakes as fx

RUN_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "55555555-5555-4555-8555-555555555555"
DOC_ID = "33333333-3333-4333-8333-333333333333"
PRICING_ID = "22222222-2222-4222-8222-222222222222"
THREAD_ID = "44444444-4444-4444-8444-444444444444"
MSG_ID = "66666666-6666-4666-8666-666666666666"
GROUP_ID = "77777777-7777-4777-8777-777777777777"
MONEY_KEY_RE = re.compile(
    r"(?i)price|cost|usd|dollar|amount|bid_total|unit_hours|unit_material|labor|material_source|verify|markup|"
    r"range_low|range_high|workbook_basis|labor_rate"
)


def _pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=42 * 72, height=30 * 72)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _evidence() -> dict:
    return {
        "schema": "bdr.evidence/1", "run_id": RUN_ID, "main_version": 1, "boq_version": 1, "inputs_hash": "h",
        "built_at": "2026-10-22T08:00:00-07:00",
        "lines": [
            {"line_id": "B1", "description": "DUPLEX RECEPTACLE", "unit": "EA", "quantity": 4, "group": "OTHER ITEMS",
             "sheets": ["D1P1"], "sheet_refs": ["E-101"], "source_refs": [{"ref": "C1", "multiplier": 1}],
             "marks": 3, "marks_by_method": {"anchor": 3}, "not_located": 1, "polylines": None, "polyline_ft": None,
             "qty_chip": {"axis": "qty", "kind": "counted", "tone": "ok", "params": {"q": 4, "m": 3, "k": 1},
                          "text": "Counted 4, marked 3, 1 not located"}},
            {"line_id": "B2", "description": "#12 THHN/THWN Cu", "unit": "FT", "quantity": 120, "group": "WIRING",
             "sheets": [], "sheet_refs": [], "source_refs": [], "marks": 0, "marks_by_method": {}, "not_located": None,
             "polylines": 0, "polyline_ft": None,
             "qty_chip": {"axis": "qty", "kind": "unsourced", "tone": "danger", "params": {"q": 120, "unit": "FT"},
                          "text": "No source, 120 FT"}},
        ],
        "sheets": [{
            "sheet_key": "D1P1", "document_id": DOC_ID, "doc_index": 1, "page": 1, "sheet_number": "E-101",
            "w_in": 42, "h_in": 30, "scale": {"ft_per_in": 4.0},
            "legend": [{"line_id": "B1", "prefix": "", "description": "DUPLEX RECEPTACLE", "quantity": 4, "unit": "EA",
                        "marked": 3, "not_located": 1}],
            "marks": [{"mark_id": "K1", "kind": "dot", "style": "solid", "method": "anchor", "legend_index": 0,
                       "line_id": "B1", "points": [[0.2, 0.3]], "count": 1}],
        }],
        "stats": {"lines": 2, "lines_with_qty_chip": 2, "ea_lines": 1, "ea_quantity": 4, "ea_marked": 3,
                  "provenance_completeness": 0.5, "sheets": 1},
        "notes": [],
    }


def _priced_line() -> dict:
    return {"line_id": "B1", "row": {"line_id": "B1"}, "item_key": "duplex", "family": "receptacle", "uom": "EA",
            "qty_eff": "4", "labor": {"value": "0.4", "quality": "strong", "n_obs": 9, "n_projects": 5, "method": "exact"},
            "material": {"value": "10", "quality": "strong", "n_obs": 9, "n_projects": 5, "method": "exact"},
            "material_source": "library", "labor_hours": "1.6", "material_cost": "40", "flags": [], "candidates": []}


def _settings(**over) -> Settings:
    base = {"plans_reader_enabled": True, "ai_estimating_state": "live", "ai_pricing_enabled": True,
            "ai_pricing_state": "live", "ai_pricing_autorun": False, "ai_pricing_chat_tools_enabled": True,
            "llm_queue_enabled": False}
    base.update(over)
    return Settings(_env_file=None, **base)


class Api:
    def __init__(self, monkeypatch, settings: Settings | None = None):
        self.settings = settings or _settings()
        self.role = Role.EXECUTIVE
        self.db = fx.FakeDB(
            {
                "plans_reader_runs": [{"id": RUN_ID, "name": "Run", "status": "done", "project_id": PROJECT_ID,
                                       "ledger": {"project": {"name": "P"}}, "autonomy_state": "blocked"}],
                "plans_reader_documents": [{"id": DOC_ID, "run_id": RUN_ID, "name": "E.pdf", "category": "electrical_drawings",
                                            "storage_path": "plans-reader/x/source/e.pdf", "page_count": 1}],
                "plans_reader_outputs": [{"id": "o1", "run_id": RUN_ID, "scope": "evidence", "version": 1, "output": _evidence()}],
                "plans_reader_deliverables": [
                    {"id": "dl1", "run_id": RUN_ID, "kind": "boq", "version": 1, "filename": "BOQ - COUNTS.xlsx",
                     "storage_path": "plans-reader/x/deliverables/v1/boq.xlsx", "size_bytes": 10, "spec_json": {"areas": []}},
                    {"id": "dl2", "run_id": RUN_ID, "kind": "annotated_plans", "version": 1, "filename": "Annotated Plans.pdf",
                     "storage_path": "plans-reader/x/deliverables/v1/annotated.pdf", "size_bytes": 10,
                     "spec_json": {"counts": {"Circle": 1}}},
                ],
                "plans_reader_pricing": [{"id": PRICING_ID, "run_id": RUN_ID, "version": 1, "status": "priced", "stale": False,
                                          "boq_version": 1, "storage_path": "plans-reader/x/pricing/v1/x.xlsx", "filename": "x.xlsx",
                                          "verify_draft": {"bid_total": "1234.56", "labor_hours_basis": "10", "labor_rate": "100"},
                                          "workbook_basis": "2000", "range_low": "1100", "range_high": "1400",
                                          "quality_summary": {}, "categories": {}, "blockers": [], "warnings": []}],
                "plans_reader_priced_lines": [{"pricing_id": PRICING_ID, "line_id": "B1", "line": _priced_line()}],
                "plans_reader_gate_findings": [{"id": "g1", "run_id": RUN_ID, "output_version": 1, "stage": "final",
                                                "gate_id": "unsourced_line", "severity": "block", "route": "human",
                                                "status": "open", "target": {"line_id": "B2"}, "message": "no source",
                                                "evidence": {}, "fingerprint": "f1"}],
                "plans_reader_chat_threads": [{"id": THREAD_ID, "run_id": RUN_ID, "title": "price?", "created_by": "u1",
                                               "created_at": "2026-10-22T10:00:00+00:00", "updated_at": "2026-10-22T10:00:00+00:00"}],
                "plans_reader_chat_messages": [
                    {"id": "m0", "thread_id": THREAD_ID, "role": "user", "content": "price?", "status": "done",
                     "created_at": "2026-10-22T10:00:00+00:00", "created_by": "u1", "pro_max": False},
                    {"id": MSG_ID, "thread_id": THREAD_ID, "role": "assistant", "status": "done", "pro_max": False,
                     "content": "Labor 1.6 hours and material $40 for B1.", "pricing_restricted": True,
                     "sender_role": "executive", "created_at": "2026-10-22T10:00:01+00:00", "created_by": "u1",
                     "tool_calls": [{"name": "get_price_summary", "arguments": {}, "result_summary": "ok",
                                     "pricing_restricted": True}]},
                ],
                "plans_reader_price_signoffs": [],
                "plans_reader_groups": [],
                "plans_reader_reviews": [],
                "plans_reader_review_findings": [],
                "llm_jobs": [],
                "vendor_quote_extractions": [],
                "ai_pricing_settings": [{"id": 1, "policy": {}, "version": 1}],
                "profiles": [{"id": "u1", "email": "u@g3.com", "role": "executive", "is_active": True}],
            },
            unique={"plans_reader_deliverables": (("run_id", "kind", "version"),),
                    "plans_reader_groups": (("run_id", "kind", "status"),)},
        )
        self.audits: list[tuple] = []
        self.accesses: list[dict] = []
        self.uploads: list[tuple[str, int]] = []
        for mod in (ev_rt, pricing_rt, pr_rt, features, deps, chat):
            monkeypatch.setattr(mod, "get_settings", lambda: self.settings, raising=False)
        from app.core import config

        monkeypatch.setattr(config, "get_settings", lambda: self.settings)
        for mod in (ev_rt, pricing_rt, pr_rt):
            monkeypatch.setattr(mod, "get_supabase", lambda: self.db)
        for mod in (ev_rt, pricing_rt):
            monkeypatch.setattr(mod, "audit", lambda *a: self.audits.append(a))
        monkeypatch.setattr(file_access, "record", lambda user, **kw: self.accesses.append(kw))
        monkeypatch.setattr(storage, "signed_url", lambda path, ttl=None, **k: f"https://signed/{path}?dl={k.get('download')}")
        monkeypatch.setattr(storage, "download_file", lambda path: _pdf())
        monkeypatch.setattr(storage, "upload_file", lambda path, data, ct, **k: self.uploads.append((path, len(data))))
        monkeypatch.setattr(pricing_rt.CostLibrary, "load", classmethod(lambda cls, sb, *, exclude_projects=(), **k: fx.library()))
        pricing_rt._LIBRARY_CACHE.clear()
        app = FastAPI()
        app.include_router(pr_rt.router)
        app.include_router(pricing_rt.router)
        app.include_router(ev_rt.router)
        app.dependency_overrides[get_current_user] = self._user
        self.client = TestClient(app, raise_server_exceptions=True)

    def _user(self) -> CurrentUser:
        return CurrentUser(id="u1", email="u@g3.com", role=self.role, is_active=True)


@pytest.fixture
def api(monkeypatch):
    return Api(monkeypatch)


def money_paths(obj: Any, path: str = "") -> list[str]:
    """Every key path whose key looks like money or price provenance."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if MONEY_KEY_RE.search(str(k)):
                out.append(p)
            out.extend(money_paths(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(money_paths(v, f"{path}[{i}]"))
    return out


def _fill(path: str) -> str:
    return (
        path.replace("{run_id}", RUN_ID).replace("{doc_id}", DOC_ID).replace("{document_id}", DOC_ID)
        .replace("{thread_id}", THREAD_ID).replace("{group_id}", GROUP_ID).replace("{pricing_id}", PRICING_ID)
        .replace("{quote_id}", str(uuid.uuid4())).replace("{line_id}", "B1").replace("{kind}", "boq")
        .replace("{version}", "1").replace("{category}", "main")
    )


def _all_routes() -> list[tuple[str, str]]:
    out = []
    for rt in (pr_rt.router, pricing_rt.router, ev_rt.router):
        for r in rt.routes:
            if isinstance(r, APIRoute):
                for m in sorted(r.methods):
                    out.append((m, _fill(r.path)))
    return out


# ── The matrix ────────────────────────────────────────────────────────────


def test_every_evidence_route_carries_the_three_gates():
    from app.core.deps import require_ai_estimating, require_internal
    from app.core.features import require_plans_reader

    for route in ev_rt.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert {require_plans_reader, require_internal, require_ai_estimating} <= calls, route.path


def test_the_estimator_is_refused_on_every_ai_estimating_and_pricing_route(api):
    api.role = Role.ESTIMATOR
    routes = _all_routes()
    assert len(routes) >= 40
    for method, path in routes:
        r = api.client.request(method, path + ("?document_id=x&page=1&x0=0&y0=0&x1=1&y1=1" if path.endswith("crop.png") else ""))
        assert r.status_code in (403, 404), (method, path, r.status_code)
        assert not money_paths(r.json()) if r.headers.get("content-type", "").startswith("application/json") else True


def test_the_accountant_never_receives_a_money_field(api):
    api.role = Role.ACCOUNTANT
    _seed_review(api)
    seen = 0
    for route in ev_rt.router.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        path = _fill(route.path)
        if path.endswith("crop.png"):
            path += "?document_id=" + DOC_ID + "&page=1&x0=0.1&y0=0.1&x1=0.5&y1=0.5&ppi=40"
        r = api.client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        seen += 1
        if r.headers["content-type"].startswith("image/png"):
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
            continue
        body = r.json()
        assert money_paths(body) == [], (path, money_paths(body))
        assert "1234.56" not in r.text and "1.6" not in r.text and "$40" not in r.text
    assert seen == 8
    # Pricing routes: refused outright.
    for path in (f"/ai-estimating/pricing/runs/{RUN_ID}", f"/ai-estimating/pricing/versions/{PRICING_ID}/lines"):
        r = api.client.get(path)
        assert r.status_code == 403 and "1234.56" not in r.text
    # The autonomy status keeps the states but drops the pricing params and the sign-off note.
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/autonomy").json()
    assert body["pricing_visible"] is False and body["can_sign_off"] is False and body["pricing_version"] is None
    pricing = next(c for c in body["checks"] if c["key"] == "pricing_coverage")
    assert pricing["params"] == {}
    # The evidence carries no price chips for the accountant.
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence").json()
    assert body["pricing_visible"] is False and all("price_chip" not in ln for ln in body["lines"])
    assert body["lines"][0]["qty_chip"]["text"] == "Counted 4, marked 3, 1 not located"
    # A thread read through the legacy chat route redacts the restricted answer.
    r = api.client.get(f"/ai-estimating/runs/{RUN_ID}/chat/threads/{THREAD_ID}")
    assert r.status_code == 200
    answer = next(m for m in r.json()["messages"] if m["id"] == MSG_ID)
    assert answer["content"] == chat.PRICING_REDACTED_MESSAGE and "$40" not in r.text and "1.6" not in r.text
    assert answer["tool_calls"] == [{"name": "get_price_summary", "pricing_restricted": True, "result_summary": "redacted"}]
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/chat/threads").json()[0]["pricing_restricted"] is True
    # The explain_row chat tool for an accountant: no price block anywhere.
    chat_tools.set_sources_factory(lambda ctx: chat_tools.FakeExplainSources(evidence=_evidence(), priced={"B1": _priced_line()}))
    try:
        ctx = tools.ToolContext(run_id=RUN_ID, kind="chat", role="accountant", services=tools.fake_services())
        result = chat_tools.explain_row(ctx, {"line": "B1", "include_marks": True})
        assert not result.refused and money_paths(result.data) == [] and "price" not in result.data
        exec_ctx = tools.ToolContext(run_id=RUN_ID, kind="chat", role="executive", services=tools.fake_services())
        exec_result = chat_tools.explain_row(exec_ctx, {"line": "B1", "include_marks": False})
        assert exec_result.data["price"]["labor_hours"] == "1.6" and getattr(exec_ctx, "pricing_served", False)
    finally:
        chat_tools.set_sources_factory(None)


def test_engineer_under_pricing_state_test_reads_like_the_accountant(monkeypatch):
    api = Api(monkeypatch, _settings(ai_pricing_state="test"))
    api.role = Role.ESTIMATING_ENGINEER_MATERIALS
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence").json()
    assert body["pricing_visible"] is False and money_paths(body) == []
    r = api.client.get(f"/ai-estimating/runs/{RUN_ID}/chat/threads/{THREAD_ID}")
    assert next(m for m in r.json()["messages"] if m["id"] == MSG_ID)["content"] == chat.PRICING_REDACTED_MESSAGE
    assert api.client.post(f"/ai-estimating/runs/{RUN_ID}/signoff", json={"decision": "rejected", "pricing_version": 1, "note": "not this one"}).status_code == 403


# ── Pricing viewers ───────────────────────────────────────────────────────


def test_pricing_viewers_get_price_chips_and_the_line_view(api):
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence").json()
    assert body["pricing_visible"] is True
    chips = {ln["line_id"]: ln["price_chip"] for ln in body["lines"]}
    assert chips["B1"]["kind"] == "library" and chips["B1"]["params"]["n"] == 9 and chips["B2"]["kind"] == "missing"
    assert body["annotated_plans"]["version"] == 1 and "storage_path" not in body["annotated_plans"]
    assert evidence_mod.money_keys(body["sheets"]) == []
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence?sheets=0").json()
    assert body["sheets"] == []
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence?sheet=nope").json()
    assert body["sheets"] == []
    line = api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence/lines/B2").json()
    assert line["line"]["qty_chip"]["kind"] == "unsourced" and line["gate_findings"][0]["gate_id"] == "unsourced_line"
    line = api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence/lines/B1").json()
    assert line["marks"][0]["sheet_number"] == "E-101" and line["marks"][0]["document_id"] == DOC_ID
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence/lines/B9").status_code == 404
    assert api.client.get(f"/ai-estimating/runs/{uuid.uuid4()}/evidence").status_code == 404
    api.db.tables["plans_reader_outputs"].clear()
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence").status_code == 404


def test_crop_validates_and_records_a_document_view(api):
    base = f"/ai-estimating/runs/{RUN_ID}/evidence/crop.png?document_id={DOC_ID}&page=1"
    r = api.client.get(base + "&x0=0.1&y0=0.1&x1=0.6&y1=0.5&ppi=40")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    # The 0161 CHECK allows view|download|export|prefetch only: a crop is a view.
    assert api.accesses[-1]["source"] == "ai_estimating_document" and api.accesses[-1]["action"] == "view"
    assert api.accesses[-1]["action"] in ("view", "download", "export", "prefetch")
    assert api.accesses[-1]["detail"]["crop"] == [0.1, 0.1, 0.6, 0.5] and api.accesses[-1]["detail"]["ppi"] == 40
    assert api.client.get(base + "&x0=0.5&y0=0.1&x1=0.5&y1=0.5").status_code == 400
    assert api.client.get(base + "&x0=0&y0=0&x1=1&y1=1&ppi=999").status_code == 422
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/evidence/crop.png?document_id={uuid.uuid4()}&page=1&x0=0&y0=0&x1=1&y1=1").status_code == 404
    assert api.client.get(base.replace("page=1", "page=9") + "&x0=0&y0=0&x1=1&y1=1").status_code == 404


REVIEW_ID = "88888888-8888-4888-8888-888888888888"


def _seed_review(api: "Api") -> None:
    """One stored independent review (plans_reader_review.result_for shape)
    with a money band, a count band, a finding carrying impact dollars and
    money facts in its evidence, and a revision dollar move."""
    api.db.tables["plans_reader_reviews"].append({
        "id": REVIEW_ID, "run_id": RUN_ID, "base_pricing_version": 1, "revised_pricing_version": None,
        "mode": "full", "stage": "done", "verdict": "advisory", "reasons": ["one warning"], "cost_usd": 1.25,
        "models": {"review": "m"}, "created_at": "2026-10-22T10:00:00+00:00", "finished_at": "2026-10-22T10:05:00+00:00",
        "result": {
            "bands": [
                {"metric": "bid_per_device", "value": 812.5, "stratum": "ti", "n": 12, "p10": 400.0, "p25": 500.0,
                 "p50": 600.0, "p75": 700.0, "p90": 800.0, "position": "high", "percentile": 91, "severity": "warning",
                 "level": "stratum", "reason": "", "band_version": "1", "line_ids": []},
                {"metric": "hours_per_device", "value": 1.9, "stratum": "ti", "n": 12, "p10": 1.0, "p25": 1.2,
                 "p50": 1.5, "p75": 1.8, "p90": 2.1, "position": "inside", "percentile": 70, "severity": "info",
                 "level": "stratum", "reason": "", "band_version": "1", "line_ids": []},
            ],
            "stages": {"rules": {"status": "done"}, "reviewer": {"status": "done"}},
            "revision": {"summary": {"move_share": 0.04, "applied": 1}},
        },
    })
    api.db.tables["plans_reader_review_findings"].append({
        "review_id": REVIEW_ID, "run_id": RUN_ID, "finding_id": "RF1", "origin": "rule", "rule_id": "gfci",
        "kind": "missing_scope", "severity": "warning", "title": "GFCI receptacles missing", "detail": "wet areas",
        "line_ids": ["B1"], "evidence": [{"ref": "C1", "facts": {"unit_price": 12.0, "count": 4}}],
        "proposed_ops": [], "impact_usd": 1500.0, "impact_hours": 2.0, "disposition": "open",
        "post_revision": False, "fingerprint": "fp1",
    })


def test_review_404s_before_any_review_and_shows_money_to_pricing_viewers(api):
    url = f"/ai-estimating/runs/{RUN_ID}/review"
    r = api.client.get(url)
    assert r.status_code == 404 and r.json()["detail"] == ev_rt.MSG_NO_REVIEW
    assert api.client.get(f"/ai-estimating/runs/{uuid.uuid4()}/review").status_code == 404
    _seed_review(api)
    body = api.client.get(url).json()
    assert body["pricing_visible"] is True and body["review_id"] == REVIEW_ID and body["verdict"] == "advisory"
    money = next(b for b in body["bands"] if b["metric"] == "bid_per_device")
    assert money["value"] == 812.5 and money["p50"] == 600.0
    assert body["findings"][0]["impact_usd"] == 1500.0 and body["findings"][0]["evidence"][0]["facts"]["unit_price"] == 12.0
    assert body["cost_usd"] == 1.25 and body["revision"]["summary"]["move_share"] == 0.04


@pytest.mark.parametrize("role", [Role.ACCOUNTANT, Role.ESTIMATING_ENGINEER_MATERIALS])
def test_review_for_a_non_pricing_reader_nulls_money_bands_and_drops_money_keys(monkeypatch, role):
    api = Api(monkeypatch, _settings(ai_pricing_state="test") if role != Role.ACCOUNTANT else None)
    api.role = role
    _seed_review(api)
    r = api.client.get(f"/ai-estimating/runs/{RUN_ID}/review")
    assert r.status_code == 200
    body = r.json()
    assert body["pricing_visible"] is False
    assert money_paths(body) == [], money_paths(body)
    money = next(b for b in body["bands"] if b["metric"] == "bid_per_device")
    assert all(money[k] is None for k in ("value", "p10", "p25", "p50", "p75", "p90"))
    counts = next(b for b in body["bands"] if b["metric"] == "hours_per_device")
    assert counts["value"] == 1.9 and counts["p50"] == 1.5
    finding = body["findings"][0]
    assert finding["title"] == "GFCI receptacles missing" and finding["impact_hours"] == 2.0
    assert finding["evidence"][0]["facts"] == {"count": 4}
    assert "move_share" not in body["revision"]["summary"]
    assert "1500" not in r.text and "812.5" not in r.text and "1.25" not in r.text


def test_autonomy_for_a_pricing_viewer_and_the_signoff_rules(api):
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/autonomy").json()
    assert body["pricing_visible"] is True and body["can_sign_off"] is True and body["pricing_version"] == 1
    assert body["overall"] in ("blocked", "needs_review") and len(body["checks"]) == 8
    url = f"/ai-estimating/runs/{RUN_ID}/signoff"
    # Not ready: verified without notes is refused; the failing checks must each carry a note.
    r = api.client.post(url, json={"decision": "verified", "pricing_version": 1})
    assert r.status_code == 422 and "not passing" in r.json()["detail"]
    r = api.client.post(url, json={"decision": "rejected", "pricing_version": 1, "note": "short"})
    assert r.status_code == 422
    r = api.client.post(url, json={"decision": "rejected", "pricing_version": 2, "note": "the quantities are off"})
    assert r.status_code == 422 and "latest" in r.json()["detail"]
    r = api.client.post(url, json={"decision": "rejected", "pricing_version": 1, "note": "the quantities are off"})
    assert r.status_code == 201 and r.json()["decision"] == "rejected"
    row = api.db.rows("plans_reader_price_signoffs")[0]
    assert row["totals_snapshot"]["bid_total"] == "1234.56" and row["autonomy_snapshot"]["overall"] == body["overall"]
    assert api.audits[-1][1] == "ai_pricing.signoff_rejected"
    failing = [c["key"] for c in body["checks"] if c["state"] in ("fail", "warn", "skipped", "pending")]
    accepted = {k: f"accepted after a manual check of {k} on the plans" for k in failing}
    r = api.client.post(url, json={"decision": "verified", "pricing_version": 1, "note": "ok", "accepted": accepted})
    assert r.status_code == 201 and r.json()["autonomy"]["signoff"]["decision"] == "verified"
    assert api.db.rows("plans_reader_price_signoffs")[-1]["autonomy_snapshot"]["accepted"] == accepted
    # Only the Verify roles sign; an Estimating Admin sees prices but may not.
    api.role = Role.ESTIMATING_ADMIN
    assert api.client.post(url, json={"decision": "rejected", "pricing_version": 1, "note": "the quantities are off"}).status_code == 403
    api.role = Role.EXECUTIVE
    api.db.tables["plans_reader_pricing"].clear()
    assert api.client.post(url, json={"decision": "rejected", "pricing_version": 1, "note": "the quantities are off"}).status_code == 409


def test_signoff_404s_while_pricing_is_off(monkeypatch):
    api = Api(monkeypatch, _settings(ai_pricing_enabled=False))
    r = api.client.post(f"/ai-estimating/runs/{RUN_ID}/signoff", json={"decision": "rejected", "pricing_version": 1, "note": "the quantities are off"})
    assert r.status_code == 404
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/autonomy").json()
    assert body["pricing_visible"] is False and next(c for c in body["checks"] if c["key"] == "pricing_coverage")["state"] == "na"


# ── Annotated plans (code kind) ───────────────────────────────────────────


def test_annotated_plans_list_download_preview_and_rerender(api):
    base = f"/ai-estimating/runs/{RUN_ID}/annotated-plans"
    body = api.client.get(base).json()
    assert body["kind"] == "annotated_plans" and body["versions"][0]["version"] == 1 and "storage_path" not in body["versions"][0]
    r = api.client.get(f"{base}/1/download")
    assert r.status_code == 200 and r.json()["url"].endswith("?dl=Annotated Plans.pdf")
    assert api.accesses[-1]["source"] == "ai_estimating_deliverable" and api.accesses[-1]["category"] == "annotated_plans"
    r = api.client.get(f"{base}/1/preview-url")
    assert r.status_code == 200 and r.json()["preview_status"] == "ready"
    assert api.client.get(f"{base}/9/download").status_code == 404
    # The legacy deliverable routes do not know the code kind.
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/deliverables/annotated_plans/1/download").status_code == 404
    assert api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/annotated_plans/rerender").status_code == 404
    # Re-render from the stored evidence: version 2, slot claimed and released, audited.
    r = api.client.post(f"{base}/rerender")
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2 and r.json()["spec_json"]["counts"]["Circle"] == 1
    assert api.uploads and api.uploads[-1][0].startswith(f"plans-reader/{RUN_ID}/deliverables/v2/")
    groups = api.db.rows("plans_reader_groups")
    assert len(groups) == 1 and groups[0]["status"] == "done" and groups[0]["output"]["version"] == 2
    assert api.audits[-1][1] == "ai_estimating.annotated_plans_rerendered"
    # Accountant: read only.
    api.role = Role.ACCOUNTANT
    assert api.client.post(f"{base}/rerender").status_code == 403
    assert api.client.get(f"{base}/2/preview-url").status_code == 200
    api.role = Role.EXECUTIVE
    api.db.tables["plans_reader_runs"][0]["status"] = "reading"
    assert api.client.post(f"{base}/rerender").status_code == 409
    api.db.tables["plans_reader_runs"][0]["status"] = "done"
    api.db.tables["plans_reader_outputs"].clear()
    r = api.client.post(f"{base}/rerender")
    assert r.status_code == 404 and api.db.rows("plans_reader_groups")[-1]["status"] == "failed"


def test_payloads_are_json_without_dashes(api):
    for path in (f"/ai-estimating/runs/{RUN_ID}/evidence", f"/ai-estimating/runs/{RUN_ID}/autonomy"):
        text = json.dumps(api.client.get(path).json())
        assert chr(0x2013) not in text and chr(0x2014) not in text


def test_review_for_the_accountant_hides_the_dollars_inside_band_and_r12_findings(api):
    """K1 (spec 5.5): a money band finding (bid_per_device) and an R12 unit
    material finding stored on the review carry dollars in their evidence
    facts and detail; the Accountant's GET /review shows none of them and
    no money key, while the Executive reads them whole."""
    from tests.test_plans_reader_review_rules import (
        BAND_DOLLARS,
        R12_DOLLARS,
        R12_SHOWN,
        numbers_in,
        seeded_money_band_finding,
        seeded_r12_unit_material_finding,
    )

    _seed_review(api)
    for finding in (seeded_r12_unit_material_finding(), seeded_money_band_finding()):
        api.db.tables["plans_reader_review_findings"].append(finding.to_row(review_id=REVIEW_ID, run_id=RUN_ID))
    seeded = R12_DOLLARS | BAND_DOLLARS
    url = f"/ai-estimating/runs/{RUN_ID}/review"
    full = api.client.get(url).json()
    assert full["pricing_visible"] is True and (R12_SHOWN | BAND_DOLLARS) <= numbers_in(full)  # the Executive sees them
    r12_full = next(f for f in full["findings"] if f["rule_id"] == "R12")
    assert r12_full["evidence"][0]["facts"]["unit_material_usd"] == 400.0 and r12_full["impact_usd"] == 3000.0
    api.role = Role.ACCOUNTANT
    r = api.client.get(url)
    assert r.status_code == 200
    body = r.json()
    assert body["pricing_visible"] is False and money_paths(body) == [], money_paths(body)
    assert not (numbers_in(body) & seeded), numbers_in(body) & seeded
    assert {f["finding_id"] for f in body["findings"]} == {"RF1", "RF7", "RF8"}
    r12 = next(f for f in body["findings"] if f["rule_id"] == "R12")
    assert r12["evidence"][0]["facts"] == {"n_projects": 9} and "400" not in r12["detail"] and "180" not in r12["detail"]
    band = next(f for f in body["findings"] if f["origin"] == "band")
    facts = band["evidence"][0]["facts"]
    assert facts["metric"] == "bid_per_device" and facts["n"] == 29
    assert all(facts[k] is None for k in ("value", "p10", "p25", "p50", "p75", "p90"))
