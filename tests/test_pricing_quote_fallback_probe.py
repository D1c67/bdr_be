"""scripts/pricing_quote_fallback_probe.py: the WS6 quote fallback probe
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS6; 0.4 ledger rules 2, 3, 6).

Pinned here:
  discovery  quote PDFs through pricing_run.backup_quote_files (the WS0
             reference's files for a case, else the project's quote rows
             named by category); holdout never scanned;
  classes    text_parsed / needs_fallback by the text parser alone; RFQ-
             named files, byte-identical copies and drawing sheets are left
             out of the calls; the fallback request is captured at $0;
  console    the dry run prints counts, flags and API estimates only, never
             a quote amount;
  refusals   no --sweep, a sweep on another workstream, PLANS_READER_ENABLED
             off; nothing armed, no call;
  live path  scripted complete_turn and the in-memory ledger: one call per
             candidate, parse_quote_pdf takes the fallback's answer, the
             parsed total is compared with the reference amount, dollar
             values land in results.json under the out dir only.
No network, no database, no model; amounts are synthetic.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.services import eval_ledger, llm
from tests import probe_fixtures as pf

probe = pf.load_script("pricing_quote_fallback_probe")

TEXT_QUOTE = [
    "Acme Electric Supply",
    "Project TEST",
    "QTY Type",
    "4 L1 LED TROFFER",
    "2 L2 DOWNLIGHT",
    "SUBTOTAL $1,234.00",
    "1. SALES TAX IS NOT INCLUDED",
]
SCANNED_AMOUNT = 987.65
TEXT_ID = "aaaaaaaa-0000-4000-8000-000000000001"
SCANNED_ID = "bbbbbbbb-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def boom(*_a, **_k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("a model call was attempted")

    monkeypatch.setattr(llm, "complete_turn", boom)
    monkeypatch.setattr(llm, "complete_parts", boom)
    saved = {k: os.environ.get(k) for k in pf.ARMED}
    yield
    pf.restore_env(saved)
    get_settings.cache_clear()
    eval_ledger.use_store(None)


def _setup(tmp_path):
    fb = pf.FakeBackup(tmp_path / "backup")
    for pid in (pf.P7154, pf.P7188, pf.POOL1):
        fb.project(pid)
    scanned = pf.make_pdf([])
    fb.file(pf.P7154, "quote", "Acme Lighting Quote.pdf", pf.make_pdf(TEXT_QUOTE), file_id=TEXT_ID)
    fb.file(pf.P7154, "quote", "Scanned Lighting Quote.pdf", scanned, file_id=SCANNED_ID)
    fb.file(pf.P7188, "quote", "Holdout Lighting Quote.pdf", scanned)
    fb.file(pf.POOL1, "quote", "Lighting_RFQ.pdf", pf.make_pdf(["REQUEST FOR QUOTE", "Please quote the fixtures below."]))
    fb.file(pf.POOL1, "quote", "Lighting Quote copy.pdf", scanned)
    fb.file(pf.POOL1, "quote", "E2.01 Lighting Plan.pdf", pf.make_pdf(["E2.01 LIGHTING PLAN"], width=2592, height=1728))
    fb.file(pf.POOL1, "quote", "Switchgear Quote.xlsx", b"not a pdf")
    data = fb.data()
    eval_root = tmp_path / "eval"
    ref_dir = eval_root / "references" / "7154"
    ref_dir.mkdir(parents=True)

    def f(fid, name):
        return {"filename": name, "storage_path": f"{pf.P7154}/quote/{name}", "sha256": None, "id": fid}

    ref = {"g3_materials": [{
        "category": "Lighting",
        "selected": {"quote_id": SCANNED_ID, "amount": SCANNED_AMOUNT, "tax_included": False, "tax_rate": 8.375,
                     "is_selected": True, "file": f(SCANNED_ID, "Scanned Lighting Quote.pdf")},
        "alternatives": [{"quote_id": TEXT_ID, "amount": 1337.35, "tax_included": True, "tax_rate": 8.375,
                          "is_selected": False, "file": f(TEXT_ID, "Acme Lighting Quote.pdf")}],
    }]}
    (ref_dir / "reference.json").write_text(json.dumps(ref))
    return data, eval_root


def test_discovery_and_classification(tmp_path):
    data, eval_root = _setup(tmp_path)
    quotes, passed = probe.collect_quotes(data, eval_root)
    names = [(q.case, q.filename) for q in quotes]
    assert ("7154", "Acme Lighting Quote.pdf") in names and ("7154", "Scanned Lighting Quote.pdf") in names
    assert all(q.project_id != pf.P7188 for q in quotes)
    assert not any(q.filename.endswith(".xlsx") for q in quotes)
    by_name = {q.filename: q for q in quotes}
    assert by_name["Scanned Lighting Quote.pdf"].ref["role"] == "selected"
    plan, state = probe.build_plan(quotes, passed, settings=pf.settings())
    rows = {r["filename"]: r for r in plan["quotes"]}
    assert rows["Acme Lighting Quote.pdf"]["cls"] == "text_parsed"
    # 1337.35 recorded with tax included at 8.375% is 1234.00 pre-tax.
    assert rows["Acme Lighting Quote.pdf"]["text_ref_match"] is True
    assert rows["Scanned Lighting Quote.pdf"]["cls"] == "needs_fallback"
    assert rows["Scanned Lighting Quote.pdf"]["skip_reason"] is None
    assert rows["Scanned Lighting Quote.pdf"]["input_tokens"] > 0 and rows["Scanned Lighting Quote.pdf"]["images"] == 1
    assert rows["Scanned Lighting Quote.pdf"]["max_tokens"] == 32_000
    assert "RFQ" in rows["Lighting_RFQ.pdf"]["skip_reason"]
    assert rows["Lighting Quote copy.pdf"]["skip_reason"] == f"duplicate of {SCANNED_ID[:8]}"
    assert rows["E2.01 Lighting Plan.pdf"]["skip_reason"].startswith("drawing sheet")
    assert plan["fallback_candidates"] == [SCANNED_ID] and plan["planned"] == [SCANNED_ID]
    assert plan["route"]["feature"] == "pricing_quote" and plan["route"]["step"] == "quote"
    assert llm.complete_turn.__name__ == "boom"


def test_dry_run_console_has_no_quote_amounts(tmp_path, capsys):
    data, eval_root = _setup(tmp_path)
    before = {k: os.environ.get(k) for k in pf.ARMED}
    code = probe.main(["--dry-run", "--eval-root", str(eval_root), "--verbose"], settings=pf.settings(), data=data)
    out = capsys.readouterr().out
    assert code == 0
    assert "model calls made: 0" in out
    for amount in ("1,234", "1234", "987.65", "1337.35"):
        assert amount not in out
    assert "text parser vs reference quote amounts: 1 of 1 within 1%" in out
    assert {k: os.environ.get(k) for k in pf.ARMED} == before


@pytest.mark.parametrize("case", ["no_sweep", "workstream", "plans_off"])
def test_live_refusals_arm_nothing(tmp_path, capsys, case):
    data, eval_root = _setup(tmp_path)
    s = pf.settings()
    argv = ["--eval-root", str(eval_root), "--sweep", "open"]
    if case == "no_sweep":
        argv = ["--eval-root", str(eval_root)]
        pf.open_sweep(workstream="WS6", budget="4")
    elif case == "workstream":
        pf.open_sweep(workstream="WS5")
    else:
        pf.open_sweep(workstream="WS6", budget="4")
        s = pf.settings(plans_reader_enabled=False)
    before = {k: os.environ.get(k) for k in pf.ARMED}
    code = probe.main(argv, settings=s, data=data)
    assert code == 2 and "refused:" in capsys.readouterr().err
    assert {k: os.environ.get(k) for k in pf.ARMED} == before


def test_live_run_calls_only_the_unparseable_vendor_quote(tmp_path, capsys, monkeypatch):
    data, eval_root = _setup(tmp_path)
    sweep = pf.open_sweep(workstream="WS6", budget="4")
    calls: list = []

    def answer(req):
        assert req.feature == "pricing_quote" and req.step == "quote"
        return {"vendor": "Scanned Vendor", "quote_number": "S-1", "amount_pretax": SCANNED_AMOUNT,
                "tax_included": False,
                "lines": [{"qty": 4, "mark": "L1", "uom": "EA", "text": "", "kind": "fixture", "lot": False}],
                "notes": {"tax_included": False, "em_batteries_excluded": False, "controls_included": None,
                          "commissioning": "unknown"}}

    monkeypatch.setattr(llm, "complete_turn", pf.scripted_turn(answer, cost="0.21", calls=calls))
    out_dir = tmp_path / "out"
    code = probe.main(["--sweep", "open", "--eval-root", str(eval_root), "--out", str(out_dir)],
                      settings=pf.settings(), data=data)
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert len(calls) == 1
    assert str(SCANNED_AMOUNT) not in printed
    assert os.environ["EVAL_SWEEP_ID"] == str(sweep["id"]) and os.environ["LLM_CASSETTE_MODE"] == "record_missing"
    res = json.loads((out_dir / "results.json").read_text())
    (r,) = res["results"]
    assert r["quote_id"] == SCANNED_ID and r["status_before"] == "needs_review"
    assert r["status_after"] == "parsed" and r["parser"] == "llm" and r["lines"] == 1
    assert Decimal(str(r["amount_pretax"])) == Decimal(str(SCANNED_AMOUNT))
    assert r["ref_match"] is True and r["ref_selected"] is True
    assert r["cost_usd"] == pytest.approx(0.21)
    assert res["spent_usd"] == pytest.approx(0.21)
    assert "987.65" in (out_dir / "table.md").read_text()


def test_budget_stops_calls_that_do_not_fit(tmp_path, capsys, monkeypatch):
    data, eval_root = _setup(tmp_path)
    pf.open_sweep(workstream="WS6", budget="4")
    monkeypatch.setattr(llm, "complete_turn", pf.scripted_turn(lambda _r: None))
    code = probe.main(["--sweep", "open", "--eval-root", str(eval_root), "--max-usd", "0.01"],
                      settings=pf.settings(), data=data)
    assert code == 2 and "reserve" in capsys.readouterr().err


def test_ref_compare_handles_tax_and_missing_values():
    ref = {"role": "selected", "amount": 1083.75, "tax_included": True, "tax_rate": 8.375, "is_selected": True}
    assert probe.ref_compare(Decimal("1000.00"), ref)["ref_match"] is True
    assert probe.ref_compare(Decimal("1083.75"), ref)["ref_match"] is True
    miss = probe.ref_compare(Decimal("900"), ref)
    assert miss["ref_match"] is False and miss["delta_pct"] == 10.0
    assert probe.ref_compare(None, ref)["ref_match"] is None
    assert probe.ref_compare(Decimal("1"), None) == {"ref_role": None, "ref_match": None, "delta_pct": None}
