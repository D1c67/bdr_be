"""scripts/review_gauntlet.py, the live path of the WS7 reviewer gauntlet
(docs/AI_ESTIMATING_BUILD_SPEC.md WS7; 0.4 ledger rules 2 and 6).

Pinned here:
  in process   a pack runs end to end in this process: the spine's review
               builder (pack text and pre-calls), the WS2 tool loop on an
               in-memory store with sb=None (the daily cap gate replaced by
               LocalCap, so nothing reads a database), a scripted provider
               through llm.complete_turn, the spine's finalizer with the
               bundle's page text; findings, detection on a seeded variant
               and false criticals on a clean control are recorded;
  missing PDF  a bundle whose PDFs are absent (no inputs/, not in the
               backup) gets the text-layer fallback with a note, never a
               failed pack;
  ledger       arming refuses without an open sweep, with a sweep that is
               not WS7 dev_probe and off the dev database; a WS7 dev_probe
               sweep arms EVAL_SWEEP_ID, the guard and record_missing; a plan
               whose summed review caps exceed what the sweep has left is
               refused before any service is built or any call is made.
No network, no database, a scripted model, synthetic packs only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings, get_settings
from app.services import ai_spend, eval_ledger, llm, llm_gate, llm_queue
from app.services import plans_reader_tools as T

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_gauntlet.py"
DEV_URL = "https://bpidntbyvoooqvaispup.supabase.co"
PROD_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
DOC = "11111111-1111-4111-8111-111111111111"
ARMED = ("EVAL_SWEEP_ID", "AI_EVAL_LEDGER_ENFORCE", "LLM_CASSETTE_MODE", "LLM_CASSETTE_DIR")
ROUTE = {"step": "review", "provider": "openai", "model": "gpt-6-astra", "effort": "medium", "budget_usd": 4.0,
         "tools": True, "explicit": False}
PAGE_TEXT = "BRANCH CIRCUIT WIRING SHALL BE INSTALLED IN 3/4 EMT CONDUIT THROUGHOUT THE SUITE"


def _load():
    if "review_gauntlet" in sys.modules:
        return sys.modules["review_gauntlet"]
    spec = importlib.util.spec_from_file_location("review_gauntlet", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["review_gauntlet"] = mod
    spec.loader.exec_module(mod)
    return mod


g = _load()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No database client may be built; the armed env, the ledger store and
    the gates are put back after every test."""

    def no_db(*_a, **_k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("a database client was requested")

    import app.core.supabase_client as supabase_client

    monkeypatch.setattr(supabase_client, "get_supabase", no_db)
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: None)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    llm_gate.reset_gates()
    saved = {k: os.environ.get(k) for k in ARMED}
    hook = ai_spend.ledger_hook()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    eval_ledger.use_store(None)
    ai_spend.set_ledger_hook(hook)
    llm_gate.reset_gates()


def _settings(**over) -> Settings:
    base = dict(_env_file=None, openai_api_key="o-key", plans_reader_enabled=True, supabase_url=DEV_URL)
    base.update(over)
    return Settings(**base)


def _ledger(workstream: str = "WS7", purpose: str = "dev_probe", budget: str = "15") -> dict:
    eval_ledger.use_store(eval_ledger.MemoryLedgerStore())
    return eval_ledger.open_sweep(label="gauntlet test", workstream=workstream, purpose=purpose, budget_usd=Decimal(budget))


# ── synthetic packs and services ────────────────────────────────────────────


def _pack(variant: str):
    run = "run-clean" if variant == "clean" else "run-seeded"
    pack = {
        "review_id": f"gauntlet-T-{variant}", "run_id": run, "boq_version": 1, "base_pricing_version": 0,
        "project_name": "Synthetic suite", "wage_type": "prevailing_wage",
        "documents": [{"id": DOC, "name": "Electrical Drawings", "category": "electrical_drawings", "pages": 2}],
        "lines": [
            {"line_id": "B1", "section": "NEW WORK", "group": "WIRING", "subgroup": "BRANCH", "description": "3/4 EMT",
             "quantity": 100, "unit": "FT", "sheet_refs": ["E1.0"]},
            {"line_id": "B2", "section": "NEW WORK", "group": "DEVICES", "subgroup": "RECEPTACLES",
             "description": "DUPLEX RECEPTACLE", "quantity": 12, "unit": "EA", "sheet_refs": ["E1.0"]},
        ],
        "rule_findings": [{"finding_id": "RF1", "severity": "warning", "origin": "rules", "rule_id": "R1",
                           "kind": "missing_scope", "line_ids": [], "title": "Branch wiring thin for the device count"}],
        "bands": [],
    }
    kinds = () if variant == "clean" else g.EXPECTED_KINDS[variant]
    lines = () if variant == "clean" else ("B1",)
    return g.Pack(case_id="T", bundle="bundle01", variant=variant, pack=pack, boq_spec={}, artifact=None,
                  expected_kinds=kinds, expected_lines=lines)


class _Policy(T.FakeScopePolicyService):
    def facts(self, *, run_id: str = "", system: str | None = None) -> dict:
        return {"facts": [], "total": 0}


def _services(page_text=None) -> object:
    svc = T.fake_services(text=T.FakeTextIndex({(DOC, 1): [{"text": PAGE_TEXT, "bbox": [0, 0, 1, 1]}]}), policy=_Policy())
    reader = page_text or (lambda d, p: PAGE_TEXT if (str(d), int(p)) == (DOC, 1) else None)
    return g.BundleServices(run_id="run-synthetic", services=svc, page_text=reader, mode="test")


def _finding(kind: str, severity: str, line: str) -> dict:
    return {
        "ref": "RV-A", "kind": kind, "severity": severity, "title": f"{kind} on {line}", "detail": "seen on E1.0",
        "line_ids": [line],
        "evidence": [
            {"type": "rule", "document_id": None, "page": None, "quote": None, "ref_id": "RF1"},
            {"type": "page_quote", "document_id": DOC, "page": 1, "quote": "BRANCH CIRCUIT WIRING SHALL BE INSTALLED IN 3/4 EMT",
             "ref_id": None},
            {"type": "tool", "document_id": None, "page": None, "quote": None, "ref_id": "call_1_0"},
        ],
        "proposed_ops": [], "qty_delta_hint": None, "confidence": 0.8,
    }


ANSWERS = {
    "seeded": {"summary": "Branch wiring short.", "findings": [_finding("missing_scope", "warning", "B1")], "systems_checked": []},
    "clean": {"summary": "Receptacles overstated.", "findings": [_finding("quantity_suspect", "critical", "B2")], "systems_checked": []},
}


class ScriptedReviewer:
    """An OpenAI Responses transport: round 1 calls get_scope_policy, the
    final round answers per pack (clean control or seeded variant, told
    apart by the run id in the first user turn)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def with_options(self, **_kw):
        return self

    def _create(self, **kw):
        self.calls.append(kw)
        prev = kw.get("previous_response_id")
        if prev:
            _resp, variant, r = prev.split("_")
            round_no = int(r) + 1
        else:
            variant = "clean" if "run-clean" in json.dumps(kw.get("input"), default=str) else "seeded"
            round_no = 1
        usage = SimpleNamespace(input_tokens=1000, input_tokens_details=SimpleNamespace(cached_tokens=0), output_tokens=100,
                                output_tokens_details=SimpleNamespace(reasoning_tokens=0))
        rid = f"resp_{variant}_{round_no}"
        if round_no == 1 and "tools" in kw:
            items = [SimpleNamespace(type="function_call", call_id="call_1_0", name="get_scope_policy",
                                     arguments=json.dumps({"topic": None, "item_description": None}))]
            return SimpleNamespace(id=rid, status="completed", incomplete_details=None, output_text="", output=items, usage=usage)
        return SimpleNamespace(id=rid, status="completed", incomplete_details=None, output_text=json.dumps(ANSWERS[variant]),
                               output=[], usage=usage)


# ── in process, end to end ──────────────────────────────────────────────────


def test_a_pack_runs_end_to_end_in_process_and_scores_detection_and_false_criticals(monkeypatch):
    row = _ledger()
    s = _settings(ai_eval_ledger_enforce=True, eval_sweep_id=row["id"])
    client = ScriptedReviewer()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    budget = g.pack_budget(s, ROUTE, "1.00")
    packs = [_pack("clean"), _pack("E2")]
    logs: list[str] = []
    results, summary = g.run_packs(packs, {"bundle01": _services()}, settings=s, route=ROUTE, budget=budget,
                                   sweep_id=row["id"], limit_usd=Decimal("2.00"), dollars=True, log=logs.append)
    assert [r["error"] for r in results] == [None, None]
    clean, seeded = results
    # the loop ran: one tool round (pre-calls stored apart) and the final answer
    assert clean["tool_calls"] == 1 and seeded["tool_calls"] == 1
    assert clean["stop_reason"] and seeded["stop_reason"]
    assert len(client.calls) == 4
    # findings came back through the spine's finalizer with verified evidence
    f = seeded["findings"][0]
    assert f["kind"] == "missing_scope" and f["severity"] == "warning" and f["evidence_verified"]
    by_type = {e["type"]: e["verified"] for e in f["evidence"]}
    assert by_type == {"rule": True, "page_quote": True, "tool": True}
    assert seeded["detected"] is True and seeded["false_criticals"] == 0
    assert clean["detected"] is False and clean["false_criticals"] == 1
    assert summary["detected"] == 1 and summary["variants"] == 1 and summary["false_criticals"] == 1
    assert summary["detection_rate"] == 1.0 and summary["false_criticals_per_control"] == 1.0
    # the ledger admitted both packs' calls under the sweep (in memory) and the spend is the loop's
    assert eval_ledger.spent_for_sweep(sweep_id=row["id"]) >= Decimal("0")
    assert clean["spent_usd"] > 0 and seeded["spent_usd"] > 0
    # the real daily cap is back after the run
    assert ai_spend.check_cap.__module__ == "app.services.ai_spend"


def test_the_local_cap_stops_the_run_before_a_pack_that_no_longer_fits(monkeypatch):
    s = _settings()
    client = ScriptedReviewer()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    budget = g.pack_budget(s, ROUTE, "0.50")
    logs: list[str] = []
    results, _summary = g.run_packs([_pack("clean"), _pack("E2")], {"bundle01": _services()}, settings=s, route=ROUTE,
                                    budget=budget, sweep_id=None, limit_usd=Decimal("0.50"), log=logs.append)
    assert len(results) == 1
    assert any("skipped" in line for line in logs)
    assert results[0]["spent_usd"] is None  # no dollars without --dollars


def test_a_bundle_without_pdfs_gets_the_text_layer_fallback_and_its_pack_still_runs(monkeypatch, tmp_path):
    def no_pdf(document_id, backup_dir=None):
        raise FileNotFoundError(f"no source PDF for document {document_id}")

    bundle = SimpleNamespace(run={"id": "run-7172"}, run_id8="3979444b", documents=[
        {"id": DOC, "name": "Architectural Drawings", "page_count": 3}], doc_bytes=no_pdf)
    artifacts = {"ledger": {"legend": [{"tag": "A", "description": "DUPLEX RECEPTACLE"}]}}

    def prepare_fails(*_a, **_k):
        raise FileNotFoundError("no source PDF for any document")

    bsvc = g.bundle_services(bundle, artifacts, settings=_settings(), backup=tmp_path, prepare=prepare_fails)
    assert bsvc.mode == "text_layer"
    assert any("no PDF for Architectural Drawings" in n for n in bsvc.notes)
    assert any("text-layer fallback" in n for n in bsvc.notes)
    assert bsvc.page_text(DOC, 1) is None
    assert bsvc.services.legend.lookup("duplex")["entries"][0]["tag"] == "A"
    client = ScriptedReviewer()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    s = _settings()
    results, _summary = g.run_packs([_pack("E2")], {"bundle01": bsvc}, settings=s, route=ROUTE,
                                    budget=g.pack_budget(s, ROUTE, "0.50"), sweep_id=None, limit_usd=Decimal("1"),
                                    log=lambda _line: None)
    res = results[0]
    assert res["error"] is None and res["services"] == "text_layer"
    quote = next(e for e in res["findings"][0]["evidence"] if e["type"] == "page_quote")
    assert quote["verified"] is False  # no page text for a document without its PDF
    assert res["detected"] is True  # the rule evidence still verifies


# ── the ledger guard and the plan ───────────────────────────────────────────


def test_arming_refuses_without_an_open_ws7_dev_probe_sweep_or_off_dev(tmp_path):
    eval_ledger.use_store(eval_ledger.MemoryLedgerStore())
    before = os.environ.get("EVAL_SWEEP_ID")
    with pytest.raises(g.GauntletRefused, match="no sweep is open"):
        g.arm_sweep("open", eval_root=tmp_path, settings=_settings())
    eval_ledger.open_sweep(label="ab", workstream="WS2", purpose="ab", budget_usd=Decimal("5"))
    with pytest.raises(g.GauntletRefused, match="WS7 dev_probe"):
        g.arm_sweep("open", eval_root=tmp_path, settings=_settings())
    with pytest.raises(g.GauntletRefused, match="dev project"):
        g.arm_sweep("open", eval_root=tmp_path, settings=_settings(supabase_url=PROD_URL))
    assert os.environ.get("EVAL_SWEEP_ID") == before  # nothing armed


def test_arming_a_ws7_dev_probe_sweep_sets_the_guard_and_record_missing(tmp_path):
    row = _ledger(budget="15")
    armed = g.arm_sweep("open", eval_root=tmp_path, settings=_settings())
    assert armed["id"] == row["id"] and armed["remaining_usd"] == Decimal("15")
    assert os.environ["EVAL_SWEEP_ID"] == row["id"]
    assert os.environ["AI_EVAL_LEDGER_ENFORCE"] == "true"
    assert os.environ["LLM_CASSETTE_MODE"] == "record_missing"
    assert os.environ["LLM_CASSETTE_DIR"] == str(tmp_path / "cassettes")


def test_the_plan_sums_the_review_caps_and_refuses_past_the_sweep_budget():
    s = _settings()
    packs = [_pack("clean")] * 24
    tools: list = []
    default = g.build_live_plan(packs, settings=s, route=ROUTE, budget=g.pack_budget(s, ROUTE), tools=tools)
    assert default["pack_cap_usd"] == Decimal("1.50") and default["cap_usd"] == Decimal("36.00")
    with pytest.raises(g.GauntletRefused, match="--pack-cap-usd 0.62"):
        g.check_plan(default, Decimal("15"))
    fitted = g.build_live_plan(packs, settings=s, route=ROUTE, budget=g.pack_budget(s, ROUTE, "0.60"), tools=tools)
    assert fitted["cap_usd"] == Decimal("14.40")
    g.check_plan(fitted, Decimal("15"))
    four = g.build_live_plan(packs[:12], settings=s, route=ROUTE, budget=g.pack_budget(s, ROUTE, "1.25"), tools=tools)
    assert four["cap_usd"] == Decimal("15.00")
    g.check_plan(four, Decimal("15"))
    assert all(r["min_answer_usd"] is not None and r["min_answer_usd"] < Decimal("0.60") for r in fitted["packs"])
    with pytest.raises(g.GauntletRefused, match="positive"):
        g.pack_budget(s, ROUTE, "0")


def test_run_live_refuses_without_a_sweep_before_any_service_or_call(tmp_path, capsys, monkeypatch):
    eval_ledger.use_store(eval_ledger.MemoryLedgerStore())
    monkeypatch.setattr(g, "review_route", lambda settings: dict(ROUTE))

    def boom(*_a, **_k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("services were built")

    with pytest.raises(g.GauntletRefused, match="no sweep is open"):
        g.run_live([_pack("clean")], {}, {}, backup=tmp_path, out_dir=tmp_path, eval_root=tmp_path,
                   settings=_settings(), prepare=boom)
    assert "live plan: 1 packs" in capsys.readouterr().out
    _ledger(budget="15")
    with pytest.raises(g.GauntletRefused, match="exceed what the sweep has left"):
        g.run_live([_pack("clean")] * 24, {}, {}, backup=tmp_path, out_dir=tmp_path, eval_root=tmp_path,
                   settings=_settings(), prepare=boom)
