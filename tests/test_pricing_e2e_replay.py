"""WS6 wave 3 end to end (AI_ESTIMATING_BUILD_SPEC WS6, section 7 wave 3):
the imported 7154 run priced with both backup vendor quotes, code only.

Two paths over the same stored weekend bundle (EVAL_ROOT) and the local
production backup (read only):

1. the replay `price` stage through `plans_reader_replay.replay` (stages
   before `price` are the stored artifacts): the priced artifact validates
   as `bdr.priced_estimate/1`, E9 reconciles at the lowest eligible quote,
   the stored v1 BOQ (L5 counted EA) blocks with `uom_conflict_quote`,
   GC1 carries OTHER ITEMS material, GC4 records the excluded-projects
   sha256, and E13 grade seeds apply only with a cited fact;
2. `pricing_run.price_run` on a fake Supabase client seeded as the
   imported run (status done, the stored BOQ deliverable v1, the stored
   main output, both quote PDFs uploaded as pending
   vendor_quote_extractions rows): the job parses both quotes from storage,
   reconciles, records the pricing-stage finding, stores version 1 with
   its lines and the priced workbook (recap BASE BID equals the Verify bid
   total in the written cells), hands the version to review, and the
   pricing routes serve the shapes bdr_fe/lib/aiPricing.ts reads.

Every dollar target comes from EVAL_ROOT/references/7154/expected.json
(never committed); the golden tests skip when the bundle, the reference or
the backup is absent. A synthetic contract test (no EVAL_ROOT) pins the
route shapes the frontend reads. No network, no model call.
"""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.core import deps, features
from app.core.config import Settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import ai_pricing as rt
from app.services import estimating_contracts as ec
from app.services import file_access, plans_reader_replay, storage
from app.services import plans_reader_acceptance as acc
from app.services import pricing_engine as pe
from app.services import pricing_render as pr
from app.services import pricing_run as prun
from app.services import vendor_quotes as vq
from app.services.plans_reader_render import RECAP_SHEET
from app.services.pricing_policy import PricingPolicy
from app.services.pricing_types import VERIFY_NUMBERS
from tests import ai_pricing_fakes as fx

EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
BUNDLE = EVAL_ROOT / "bundles" / "7154" / "ff8fad25"
EXPECTED = EVAL_ROOT / "references" / "7154" / "expected.json"
HAVE_7154 = BUNDLE.is_dir() and EXPECTED.is_file() and BACKUP.is_dir()
golden_7154 = pytest.mark.skipif(not HAVE_7154, reason="EVAL_ROOT 7154 bundle, expected.json or the backup not present")
CENT = Decimal("0.01")
DATE = dt.date(2026, 10, 22)

# The keys bdr_fe/lib/aiPricing.ts reads; a rename on the backend must fail here.
FE_VERSION_KEYS = {"id", "run_id", "version", "boq_version", "status", "verify_draft", "range_low", "range_high",
                   "blockers", "warnings", "stale", "storage_path", "filename", "created_at"}
# The heavy columns the pricing tab reads from GET versions/{id}/lines only
# (aiPricing.ts PricingVersion categories, quality_summary, unpriced).
FE_VERSION_DETAIL_KEYS = FE_VERSION_KEYS | {"categories", "quality_summary", "unpriced"}
FE_VERIFY_KEYS = set(VERIFY_NUMBERS) | {"bid_total", "labor_hours_basis", "labor_hours_reviewer", "labor_rate",
                                         "labor_rate_source"}
FE_LINE_KEYS = {"line_id", "row", "item_key", "family", "uom", "qty_eff", "labor", "material", "modifiers",
                "material_source", "labor_hours", "material_cost", "pricing_category", "pricing_section", "flags",
                "candidates"}
FE_STAT_KEYS = {"value", "p25", "p75", "n_obs", "n_projects", "latest_seen", "quality", "method"}
FE_QUOTE_KEYS = {"id", "run_id", "filename", "category", "vendor", "quote_number", "amount_pretax", "tax_included",
                 "parser", "status", "lines", "notes", "warnings", "selected", "created_at"}
FE_STATUS_KEYS = {"run_id", "latest", "versions", "job", "quotes"}


def _dec(v) -> Decimal:
    return Decimal(str(v if v is not None else 0))


def _recap_value_sum(ws, layout) -> Decimal:
    """The recap's VALUE cells (D E F G I J; H and K are formulas over them)
    summed: what TOTAL COST WITH OVERHEADS + PROFIT evaluates to."""
    total = Decimal(0)
    for r in range(layout["first_row"], layout["last_row"] + 1):
        for col in (4, 5, 6, 7, 9, 10):
            total += _dec(ws.cell(row=r, column=col).value)
    return total.quantize(CENT)


def _recap_rows_by_label(ws) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in ws.iter_rows(min_col=1, max_col=3):
        for c in row:
            if isinstance(c.value, str) and c.value.strip():
                out.setdefault(c.value.strip(), c.row)
    return out


class _RouterHarness:
    """The ai_pricing router over a FakeDB (storage, audit and file access
    faked; Executive under AI_PRICING_STATE=live)."""

    def __init__(self, monkeypatch, db: fx.FakeDB):
        self.db = db
        self.accesses: list[dict] = []
        settings = Settings(_env_file=None, plans_reader_enabled=True, ai_estimating_state="live",
                            ai_pricing_enabled=True, ai_pricing_state="live", ai_pricing_autorun=False)
        for mod in (rt, features, deps):
            monkeypatch.setattr(mod, "get_settings", lambda: settings)
        monkeypatch.setattr(rt, "get_supabase", lambda: db)
        monkeypatch.setattr(rt, "audit", lambda *a: None)
        monkeypatch.setattr(file_access, "record", lambda user, **kw: self.accesses.append(kw))
        monkeypatch.setattr(storage, "signed_url", lambda path, ttl=None, **k: f"https://signed/{path}")
        app = FastAPI()
        app.include_router(rt.router)
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            id="user-1", email="u@g3.com", role=Role.EXECUTIVE, is_active=True)
        self.client = TestClient(app, raise_server_exceptions=True)

    def get(self, path: str) -> dict:
        res = self.client.get(f"/ai-estimating/pricing{path}")
        assert res.status_code == 200, res.text
        return res.json()


def _assert_fe_contract(api: _RouterHarness, run_id: str) -> tuple[dict, dict, dict]:
    status = api.get(f"/runs/{run_id}")
    assert FE_STATUS_KEYS <= set(status)
    latest = status["latest"]
    assert latest is not None and FE_VERSION_KEYS <= set(latest)
    assert FE_VERIFY_KEYS <= set(latest["verify_draft"])
    assert all(FE_VERSION_KEYS <= set(v) for v in status["versions"])
    lines = api.get(f"/versions/{latest['id']}/lines")
    assert set(lines) >= {"version", "lines"} and lines["lines"]
    assert FE_VERSION_DETAIL_KEYS <= set(lines["version"])
    for ln in lines["lines"]:
        assert FE_LINE_KEYS <= set(ln)
        assert FE_STAT_KEYS <= set(ln["labor"]) and FE_STAT_KEYS <= set(ln["material"])
        assert ln["labor"]["quality"] in ec.PRICE_QUALITIES and ln["material"]["quality"] in ec.PRICE_QUALITIES
    quotes = api.get(f"/runs/{run_id}/quotes")
    assert all(FE_QUOTE_KEYS <= set(q) and "storage_path" not in q for q in quotes["quotes"])
    xlsx = api.get(f"/versions/{latest['id']}/xlsx")
    assert xlsx["url"].startswith("https://signed/") and api.accesses[-1]["source"] == rt.SOURCE_ESTIMATE
    return status, lines, quotes


# ── Synthetic contract (always runs) ──────────────────────────────────────


def test_price_run_then_routes_serve_the_shapes_the_pricing_tab_reads(monkeypatch):
    """Synthetic numbers: price_run over the fake client, then the four
    reads the Pricing tab makes. Pins the wire keys of lib/aiPricing.ts."""
    from tests.test_vendor_quotes import CODALE_STYLE, make_pdf

    run_id = "11111111-1111-4111-8111-111111111111"
    project_id = "55555555-5555-4555-8555-555555555555"
    pdf = make_pdf(CODALE_STYLE)
    db = fx.FakeDB({
        "plans_reader_runs": [{"id": run_id, "name": "Run", "status": "done", "project_id": project_id}],
        "projects": [{"id": project_id, "wage_type": "prevailing_wage", "name": "Synthetic"}],
        "plans_reader_deliverables": [{"id": "d1", "run_id": run_id, "kind": "boq", "version": 1, "spec_json": fx.boq_spec()}],
        "plans_reader_outputs": [],
        "vendor_quote_extractions": [{"id": "q-pending", "run_id": run_id, "file_sha256": vq.sha256_bytes(pdf),
                                      "filename": "Acme Lighting Quote.pdf", "storage_path": "quotes/a.pdf",
                                      "category": "Lighting", "status": "pending", "parser": "text", "selected": False,
                                      "created_at": "2026-10-22T12:00:00+00:00"}],
        "ai_pricing_settings": [],
        "llm_jobs": [],
    })
    store = ec.InMemoryFindingsStore()
    out = prun.price_run(
        db, run_id, settings=Settings(_env_file=None, plans_reader_enabled=True, ai_pricing_enabled=True),
        policy=PricingPolicy(hour_uplift="1.2"), library=fx.library(exclude={project_id}),
        pricing_allowed=lambda run: (True, []), record_findings=store.record_findings, review_begin=lambda *a, **k: None,
        upload=lambda path, data, ct: None, download=lambda path: pdf, now=DATE,
    )
    assert out["inserted"] is True and out["version"] == 1
    api = _RouterHarness(monkeypatch, db)
    status, lines, quotes = _assert_fe_contract(api, run_id)
    assert status["quotes"] == 1 and quotes["quotes"][0]["status"] == "parsed"
    assert len(lines["lines"]) == len(prun.boq_rows(fx.boq_spec()))


# ── 7154: the replay price stage ──────────────────────────────────────────


@pytest.fixture(scope="module")
def case_7154():
    from app.services import eval_bundle
    from app.services import eval_reference as R

    reference = R.load_reference(EVAL_ROOT, "7154")
    expected = json.loads(EXPECTED.read_text())
    bundle = eval_bundle.load_bundle(BUNDLE)
    return {"reference": reference, "expected": expected, "bundle": bundle}


def _replay(case, tmp_path, *, main_override=None):
    prun.register_replay_stage()  # another suite's reset_stages() may have dropped it
    bundle = case["bundle"]
    if main_override is not None:
        bundle = copy.copy(bundle)
        bundle.outputs = {**bundle.outputs, "main": main_override}
    return plans_reader_replay.replay(
        bundle, reference=case["reference"], expected=case["expected"], case_id="7154",
        from_stage="price", to_stage="price", out_dir=tmp_path, backup_dir=BACKUP,
    )


@pytest.mark.golden
@golden_7154
def test_replay_price_stage_on_the_7154_bundle_with_both_quotes(case_7154, tmp_path):
    res = _replay(case_7154, tmp_path)
    ran = [s for s in res.stages if s.mode != "stored"]
    assert [(s.name, s.owner, s.error) for s in ran] == [("price", "WS6", None)]
    art = res.artifacts
    priced = art["priced_estimate"]
    expected = case_7154["expected"]

    # The artifact validates (blocked status = refused) and closes on the Verify sum.
    ec.validate_priced_estimate(priced)
    assert priced["schema"] == ec.PRICED_ESTIMATE_SCHEMA and priced["refused"] is True and priced["status"] == "blocked"
    totals = priced["totals"]
    assert set(totals["verify"]) == set(VERIFY_NUMBERS)
    assert sum((_dec(v) for v in totals["verify"].values()), Decimal(0)).quantize(CENT) == _dec(totals["bid_total_usd"]).quantize(CENT)

    inp = acc.AcceptanceInputs(artifacts=dict(art), case_id="7154", reference=case_7154["reference"], expected=expected)

    # E9: both quotes parsed by code, the lowest eligible chosen with its reason, counts reconciled.
    e9 = acc.check_e9(inp)
    assert e9.status == "pass", e9.measured
    lighting = [q for q in priced["quotes"] if q["category"] == "Lighting"]
    assert [q["selection_reason"] for q in lighting] == [prun.SELECTION_LOWEST, prun.SELECTION_ALTERNATIVE]
    lowest = expected["e9"]["lowest_eligible_quote_pretax_usd"]
    alternate = expected["e9"]["alternate_quote_pretax_usd"]
    assert abs(lighting[0]["amount_usd"] - lowest) / lowest <= 0.02
    assert abs(lighting[1]["amount_usd"] - alternate) / alternate <= 0.02
    rec = lighting[0]["reconciled"]
    assert (rec["L1"], rec["L3"], rec["L5_ft"], rec["collapse_warning"]) == (21, 16, 50, True)
    assert vq.FLAG_EM_PREMIUM in rec["flags"]
    assert all(q["file_sha256"] for q in lighting)

    # uom_conflict_quote on the stored v1 BOQ: L5 counted 7 EA, the quote prices 50 FT.
    uom = [f for f in art["pricing_findings"] if f["gate_id"] == vq.GATE_UOM_CONFLICT]
    assert len(uom) == 1 and uom[0]["stage"] == "pricing" and uom[0]["severity"] == "block"
    ev = uom[0]["evidence"]
    assert (ev["boq_uom"], ev["quote_uom"], Decimal(ev["boq_qty"]), Decimal(ev["quote_qty"])) == ("EA", "FT", Decimal(7), Decimal(50))
    l5 = next(ln for ln in priced["lines"] if ln["line_id"] == uom[0]["target"]["line_id"])
    assert l5["unit"] == "EA" and l5["priced"] is False and l5["excluded"] is True
    assert art["gate_report"]["blocking"] is True
    assert vq.GATE_UOM_CONFLICT in {c["code"] for c in art["gate_report"]["checks"]}

    # GC1: OTHER ITEMS material is carried (WIRING is the quantity-side gap of the stored v1 BOQ, E2).
    gc1 = acc.check_gc1(inp)
    m = gc1.measured["non_quoted_material"]
    assert "OTHER ITEMS" in m["checked"] and "OTHER ITEMS" not in m["missing"]
    assert _dec(priced["sections"]["OTHER ITEMS"]["material_usd"]) > 0
    assert m["missing"] == ["WIRING"]  # recorded, not fixed here: the stored BOQ has no branch wiring

    # GC4: the excluded-projects digest is recorded and includes the case's own project.
    gc4 = acc.check_gc4(inp)
    assert gc4.status == "pass", gc4.measured
    assert len(priced["metadata"]["excluded_project_ids_sha256"]) == 64

    # E13 with the stored main output: the hospital-grade material note is the cited fact.
    seeded = [ln for ln in priced["lines"] if ln.get("spec_fact_ref")]
    summary = art["pricing_summary"]
    assert summary["seed_lines"] == len(seeded) == 3
    assert all(ln["spec_fact_ref"].startswith("material_note:") and ln["material_source"] == "policy" for ln in seeded)
    seeds = expected["seeds"]
    amounts = {Decimal(str(seeds["duplex_hospital_grade_usd"])), Decimal(str(seeds["gfci_hospital_grade_usd"]))}
    assert {_dec(ln["unit_material_usd"]) for ln in seeded} <= amounts


@pytest.mark.golden
@golden_7154
def test_replay_e13_seeds_apply_only_with_a_cited_fact(case_7154, tmp_path):
    """The same bundle with the grade words removed from the material notes:
    no cited fact, so no seed applies and no line carries a fact ref."""
    main = copy.deepcopy(case_7154["bundle"].main_output)
    for note in main.get("material_notes") or ():
        note["notes"] = [n for n in note.get("notes") or () if "HOSPITAL" not in str(n).upper()]
        note["specified_manufacturers"] = [s for s in note.get("specified_manufacturers") or ()
                                           if "HOSPITAL" not in str(s).upper()]
    assert prun.spec_facts_from_material_notes(main) == []
    res = _replay(case_7154, tmp_path, main_override=main)
    priced = res.artifacts["priced_estimate"]
    assert res.artifacts["pricing_summary"]["seed_lines"] == 0 and res.artifacts["pricing_summary"]["spec_facts"] == []
    assert not any(ln.get("spec_fact_ref") for ln in priced["lines"])
    seeds = case_7154["expected"]["seeds"]
    seed_amounts = {Decimal(str(seeds["duplex_hospital_grade_usd"])), Decimal(str(seeds["gfci_hospital_grade_usd"]))}
    plain = [ln for ln in priced["lines"] if ln["description"] in ("DUPLEX RECEPTACLE", "DUPLEX RECEPTACLE, GFCI")]
    assert plain and all(ln["material_source"] != "policy" for ln in plain)
    assert all(_dec(ln["unit_material_usd"]) not in seed_amounts for ln in plain)


# ── 7154: the imported run through price_run, the workbook and the routes ─


def _imported_run_db(case) -> tuple[fx.FakeDB, str, dict[str, bytes]]:
    """A fake client holding the imported 7154 run after its main step:
    status done, the stored BOQ v1 and main output, both backup quote PDFs
    uploaded as pending rows (the router's upload shape)."""
    bundle, reference = case["bundle"], case["reference"]
    run = dict(bundle.run)
    run_id = str(run["id"])
    project_id = str(run.get("project_id") or reference["project_id"])
    art = plans_reader_replay.stored_artifacts(bundle)
    files: dict[str, bytes] = {}
    quote_rows = []
    for i, f in enumerate(prun.backup_quote_files(reference, BACKUP, project_id)):
        data = (BACKUP / "storage" / "project-files" / f["storage_path"]).read_bytes()
        path = f"plans-reader/{run_id}/quotes/{f['filename']}"
        files[path] = data
        quote_rows.append({"id": f"7154-quote-{i + 1}", "run_id": run_id, "file_sha256": vq.sha256_bytes(data),
                           "filename": f["filename"], "storage_path": path, "size_bytes": len(data),
                           "category": f["category"], "status": "pending", "parser": "text", "selected": False,
                           "created_at": f"2026-10-22T12:00:0{i}+00:00"})
    db = fx.FakeDB({
        "plans_reader_runs": [{**run, "status": "done", "project_id": project_id}],
        "projects": [{"id": project_id, "wage_type": (reference.get("conditions") or {}).get("wage_type"), "name": "7154"}],
        "project_gcs": [],
        "plans_reader_deliverables": [{"id": "boq-v1", "run_id": run_id, "kind": "boq", "version": 1,
                                       "spec_json": art["boq_spec"]}],
        "plans_reader_outputs": [{"id": "main-v1", "run_id": run_id, "scope": "main", "version": 1,
                                  "output": art.get("main") or {}}],
        "vendor_quote_extractions": quote_rows,
        "ai_pricing_settings": [],
        "llm_jobs": [],
    })
    return db, run_id, files


@pytest.mark.golden
@golden_7154
def test_imported_7154_run_priced_end_to_end_with_both_quotes(case_7154, monkeypatch):
    from app.services import eval_manifest
    from app.services.cost_library import CostLibrary

    db, run_id, files = _imported_run_db(case_7154)
    assert len(files) == 2
    run = db.rows("plans_reader_runs")[0]
    project_id = str(run["project_id"])
    excluded = set(eval_manifest.excluded_projects(prun.LIBRARY_PURPOSE, "7154")) | {project_id}
    library = CostLibrary.from_backup(BACKUP, exclude_projects=excluded)
    policy = prun._seeded_from_expected(PricingPolicy(), case_7154["expected"])
    store = ec.InMemoryFindingsStore()
    uploads: dict[str, bytes] = {}
    reviews: list[int] = []
    out = prun.price_run(
        db, run_id, settings=Settings(_env_file=None, plans_reader_enabled=True, ai_pricing_enabled=True,
                                      quote_lines_llm_fallback_enabled=False),
        policy=policy, library=library, pricing_allowed=lambda r: (True, []),
        record_findings=store.record_findings,
        review_begin=lambda sb, r, pricing_version: reviews.append(pricing_version),
        upload=lambda path, data, ct: uploads.__setitem__(path, data), download=files.__getitem__, now=DATE,
    )

    # The version, its lines and the review hand-off.
    assert out["inserted"] is True and out["version"] == 1 and out["status"] == "blocked" and reviews == [1]
    version = db.rows("plans_reader_pricing")[0]
    lines = [r["line"] for r in db.rows("plans_reader_priced_lines")]
    assert len(lines) == len(prun.boq_rows(db.rows("plans_reader_deliverables")[0]["spec_json"])) == 41
    assert version["excluded_project_ids_sha256"] == ec.excluded_project_ids_sha256(excluded | set(library.excluded_projects))

    # Both quotes parsed from storage by code (no fallback), the lowest eligible prices Lighting.
    quotes = db.rows("vendor_quote_extractions")
    assert [q["status"] for q in quotes] == ["parsed", "parsed"] and all(q["parser"] == "text" for q in quotes)
    lighting = version["categories"]["Lighting"]
    expected = case_7154["expected"]
    lowest = expected["e9"]["lowest_eligible_quote_pretax_usd"]
    assert abs(float(lighting["quote_amount"]) - lowest) / lowest <= 0.02
    assert lighting["quote_selection_reason"] == prun.SELECTION_LOWEST and len(lighting["alternatives"]) == 1
    rec = lighting["quote_reconciled"]
    assert (rec["L1"], rec["L3"], rec["L5_ft"], rec["collapse_warning"]) == (21, 16, 50, True)

    # The pricing-stage findings: uom_conflict_quote on L5 (plus the engine's linear_in_ea on the track row).
    rows = [f.to_row() for f in store.for_run(run_id, stage="pricing")]
    assert rows == out["findings"]
    uom = [f for f in rows if f["gate_id"] == vq.GATE_UOM_CONFLICT]
    assert len(uom) == 1 and all(f["stage"] == "pricing" for f in rows)
    l5 = next(ln for ln in lines if ln["line_id"] == uom[0]["target"]["line_id"])
    assert pe.FLAG_BLOCKED in l5["flags"] and vq.GATE_UOM_CONFLICT in l5["flags"]
    assert vq.GATE_UOM_CONFLICT in {b["code"] for b in version["blockers"]}

    # The priced workbook: banner on every sheet, recap BASE BID equals the Verify bid total to the cent.
    assert list(uploads) == [version["storage_path"]]
    wb = load_workbook(io.BytesIO(uploads[version["storage_path"]]))
    assert RECAP_SHEET in wb.sheetnames and pr.VERIFY_SHEET in wb.sheetnames and pr.CHECKS_SHEET in wb.sheetnames
    for ws in wb.worksheets[1:]:
        assert any(c.value == pr.DRAFT_BANNER for row in ws.iter_rows(max_row=3) for c in row), ws.title
    bid_total = _dec(version["verify_draft"]["bid_total"]).quantize(CENT)
    recap = wb[RECAP_SHEET]
    labels = _recap_rows_by_label(recap)
    base_row = labels["BASE BID PRICE"]
    assert _dec(recap.cell(row=base_row, column=5).value).quantize(CENT) == bid_total
    first = labels["DEMOLITION"]
    last = labels[pr.LABEL_MARKUP_ADJ]
    layout = {"first_row": first, "last_row": last}
    assert _recap_value_sum(recap, layout) == bid_total
    assert any(lbl.startswith("VENDOR QUOTE - LIGHTING") for lbl in labels)

    # Idempotent on the same inputs; the routes serve what the Pricing tab reads.
    again = prun.price_run(
        db, run_id, settings=Settings(_env_file=None, plans_reader_enabled=True, ai_pricing_enabled=True),
        policy=policy, library=library, pricing_allowed=lambda r: (True, []), record_findings=store.record_findings,
        review_begin=lambda *a, **k: reviews.append(-1), upload=lambda *a: None, download=files.__getitem__, now=DATE,
    )
    assert again["inserted"] is False and reviews == [1]
    api = _RouterHarness(monkeypatch, db)
    status, served, quote_list = _assert_fe_contract(api, run_id)
    assert status["quotes"] == 2 and status["latest"]["status"] == "blocked"
    assert {q["vendor"] for q in quote_list["quotes"]} and len(quote_list["quotes"]) == 2
    assert len(served["lines"]) == 41


# ── scripts/pricing_recalc_check.py (pure parts; the integrator runs the container check) ──


def _recalc_check_module():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "pricing_recalc_check.py"
    spec = importlib.util.spec_from_file_location("pricing_recalc_check", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _recalculated_like(total: float, base: float, check: float, *, error: str | None = None) -> bytes:
    """A workbook shaped like a recalculated priced workbook (cached values
    in the recap summary and the Checks cell). Synthetic numbers."""
    from openpyxl import Workbook

    wb = Workbook()
    recap = wb.active
    recap.title = RECAP_SHEET
    recap.cell(row=10, column=3, value="TOTAL COST WITH OVERHEADS + PROFIT")
    recap.cell(row=10, column=5, value=total)
    recap.cell(row=11, column=3, value="BID SECURITY")
    recap.cell(row=11, column=5, value=0.0)
    recap.cell(row=13, column=3, value="BASE BID PRICE")
    recap.cell(row=13, column=5, value=base)
    checks = wb.create_sheet(pr.CHECKS_SHEET)
    checks.cell(row=4, column=1, value="recap_base_bid_equals_verify")
    checks.cell(row=4, column=2, value=check)
    if error:
        checks.cell(row=5, column=2, value=error)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_recalc_checker_passes_a_closed_workbook_and_names_each_failure():
    mod = _recalc_check_module()
    ok = mod.check_recalculated(_recalculated_like(1500.25, 1500.25, 0.0), "1500.25")
    assert ok["ok"] is True and [c["name"] for c in ok["checks"]] == [
        "summary_equals_base_bid", "base_bid_equals_bid_total", "checks_sheet_formula_zero", "no_formula_errors"]
    bad = mod.check_recalculated(_recalculated_like(1500.00, 1500.25, -0.25, error="#REF!"), "1500.30")
    by = {c["name"]: c for c in bad["checks"]}
    assert bad["ok"] is False
    assert by["summary_equals_base_bid"]["diff_cents"] == -25 and by["base_bid_equals_bid_total"]["diff_cents"] == -5
    assert by["checks_sheet_formula_zero"]["ok"] is False and by["no_formula_errors"]["detail"] == [f"{pr.CHECKS_SHEET}!B5=#REF!"]


def test_recalc_check_skips_cleanly_without_the_container(monkeypatch, capsys):
    mod = _recalc_check_module()
    monkeypatch.setattr(mod, "container_running", lambda container=mod.CONTAINER: False)
    assert mod.main(["--case", "7154"]) == mod.EXIT_OK
    assert capsys.readouterr().out.startswith("SKIP:")
    with pytest.raises(mod.Skip):
        mod.recalc_in_container(b"PK")
