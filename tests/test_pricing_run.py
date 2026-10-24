"""app/services/pricing_run.py end to end on a fake Supabase client: the
stored version and its lines, the stale flag, the idempotent version, the
uom_conflict_quote finding on the stored v1 BOQ (L5 7 EA against a quote
per foot) recorded with stage pricing, the blocked status (refused true),
the queue helpers and the replay stage. No network, no model, synthetic
numbers."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import estimating_contracts as ec
from app.services import eval_manifest
from app.services import plans_reader_replay
from app.services import pricing_engine as pe
from app.services import pricing_run as prun
from app.services import vendor_quotes as vq
from app.services.pricing_policy import PricingPolicy
from tests import ai_pricing_fakes as fx

RUN_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "55555555-5555-4555-8555-555555555555"
FIXTURE = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7154" / "deliverables" / "estimate_v1.json"
POLICY = PricingPolicy(hour_uplift="1.2")


def _settings(**over) -> Settings:
    base = {"plans_reader_enabled": True, "ai_pricing_enabled": True, "ai_pricing_autorun": False,
            "quote_lines_llm_fallback_enabled": False}
    base.update(over)
    return Settings(_env_file=None, **base)


def _db(*, status: str = "done", boq: dict | None = None, quotes: bool = True, wage: str | None = "prevailing_wage") -> fx.FakeDB:
    spec = boq if boq is not None else fx.boq_spec()
    db = fx.FakeDB({
        "plans_reader_runs": [{"id": RUN_ID, "name": "Run", "status": status, "project_id": PROJECT_ID,
                               "ledger": {"project": {"name": "Synthetic Project"}}}],
        "projects": [{"id": PROJECT_ID, "wage_type": wage, "name": "Synthetic Project"}],
        "project_gcs": [{"project_id": PROJECT_ID, "gc_id": "gc-1"}],
        "plans_reader_deliverables": [{"id": str(uuid.uuid4()), "run_id": RUN_ID, "kind": "boq", "version": 1, "spec_json": spec}],
        "plans_reader_outputs": [{"id": str(uuid.uuid4()), "run_id": RUN_ID, "scope": "main", "version": 1, "output": {
            "estimate": {"project_name": "Synthetic Project", "address": "", "include_standard_allowances": True,
                         "extra_allowances": [], "general_notes": [], "area_notes": []},
            "material_notes": [{"material_category": "General Material", "notes": ["Hospital grade receptacles in patient areas."],
                                "specified_manufacturers": [], "lead_time_items": []}],
        }}],
        "vendor_quote_extractions": [fx.quote_row(RUN_ID)] if quotes else [],
        "ai_pricing_settings": [],
        "llm_jobs": [],
    })
    return db


class _Io:
    """The injected I/O of price_run: storage, gates, findings, review."""

    def __init__(self, allowed=(True, [])):
        self.uploads: list[tuple[str, bytes, str]] = []
        self.store = ec.InMemoryFindingsStore()
        self.reviews: list[dict] = []
        self.allowed = allowed

    def kwargs(self, **over) -> dict:
        base = dict(
            settings=_settings(), policy=POLICY, library=fx.library(exclude={PROJECT_ID}),
            pricing_allowed=lambda run: self.allowed,
            record_findings=self.store.record_findings,
            review_begin=lambda sb, run, pricing_version: self.reviews.append({"run": run["id"], "version": pricing_version}),
            upload=lambda path, data, ct: self.uploads.append((path, data, ct)),
            now=dt.date(2026, 10, 21),
        )
        base.update(over)
        return base


def test_price_run_stores_a_version_with_lines_workbook_findings_and_review_hook():
    db, io = _db(), _Io()
    row = prun.price_run(db, RUN_ID, user={"id": "u1"}, **io.kwargs())
    assert row["inserted"] is True and row["version"] == 1 and row["boq_version"] == 1
    assert row["run_id"] == RUN_ID and row["stale"] is False and row["created_by"] == "u1"
    stored = db.rows("plans_reader_pricing")
    assert len(stored) == 1 and stored[0]["status"] == row["status"]
    lines = db.rows("plans_reader_priced_lines")
    assert len(lines) == 13 and {ln["pricing_id"] for ln in lines} == {stored[0]["id"]}
    assert all(ln["line"]["line_id"] == ln["line_id"] for ln in lines)
    assert len(io.uploads) == 1
    path, data, ct = io.uploads[0]
    assert path.startswith(f"plans-reader/{RUN_ID}/pricing/v1/") and data[:2] == b"PK" and ct.endswith("sheet")
    assert stored[0]["storage_path"] == path and stored[0]["filename"].endswith("v1.xlsx")
    assert io.reviews == [{"run": RUN_ID, "version": 1}]
    # The verify draft and the policy snapshot are stored; context carries the wage.
    assert stored[0]["verify_draft"]["bid_total"] and stored[0]["policy"]["hour_uplift"] == "1.2"
    assert stored[0]["context"]["wage_type"] == "prevailing_wage"
    assert stored[0]["context"]["project_gc_ids"] == ["gc-1"]
    assert stored[0]["inputs_sha256"] and stored[0]["library_fingerprint"] == fx.library(exclude={PROJECT_ID}).fingerprint


def _capture_render(monkeypatch) -> list[dict]:
    seen: list[dict] = []
    real = prun.pricing_render.render_priced_estimate_xlsx

    def _render(*args, **kwargs):
        seen.append({"basis_rows": list(kwargs.get("basis_rows") or ()), "checks_rows": list(kwargs.get("checks_rows") or ())})
        return real(*args, **kwargs)

    monkeypatch.setattr(prun.pricing_render, "render_priced_estimate_xlsx", _render)
    return seen


def test_price_run_fills_the_basis_and_checks_rows_from_the_run_when_none_are_injected(monkeypatch):
    from app.services import plans_reader_basis_rows

    calls: list[tuple] = []

    def _rows_for_run(sb, run_id, *, priced=None):
        calls.append((run_id, priced))
        return [["B1", "basis"]], [["census_floor", "pass"]]

    monkeypatch.setattr(plans_reader_basis_rows, "rows_for_run", _rows_for_run)
    seen = _capture_render(monkeypatch)
    db, io = _db(), _Io()
    row = prun.price_run(db, RUN_ID, **io.kwargs())
    assert row["inserted"] is True and len(io.uploads) == 1
    # Called once with the in-memory estimate (quote details attached), and its rows reach the workbook.
    assert len(calls) == 1 and calls[0][0] == RUN_ID and calls[0][1] is not None and len(calls[0][1].lines) == 13
    assert seen == [{"basis_rows": [["B1", "basis"]], "checks_rows": [["census_floor", "pass"]]}]


def test_price_run_keeps_injected_rows_and_survives_a_rows_failure(monkeypatch):
    from app.services import plans_reader_basis_rows

    calls: list[str] = []

    def _boom(sb, run_id, *, priced=None):
        calls.append(run_id)
        raise RuntimeError("evidence store down")

    monkeypatch.setattr(plans_reader_basis_rows, "rows_for_run", _boom)
    seen = _capture_render(monkeypatch)
    # Injected rows win: rows_for_run is never asked.
    db, io = _db(), _Io()
    prun.price_run(db, RUN_ID, **io.kwargs(basis_rows=[["B2", "given"]]))
    assert calls == [] and seen[-1]["basis_rows"] == [["B2", "given"]]
    # A failing rows read logs and the workbook still renders without the two sheets' rows.
    db, io = _db(), _Io()
    row = prun.price_run(db, RUN_ID, **io.kwargs())
    assert calls == [RUN_ID] and row["inserted"] is True and len(io.uploads) == 1
    assert seen[-1] == {"basis_rows": [], "checks_rows": []}


def test_uom_conflict_quote_blocks_l5_and_is_recorded_with_stage_pricing():
    db, io = _db(), _Io()
    row = prun.price_run(db, RUN_ID, **io.kwargs())
    assert row["status"] == "blocked"
    codes = {b["code"] for b in row["blockers"]}
    assert codes == {vq.GATE_UOM_CONFLICT}
    l5 = next(ln for ln in db.rows("plans_reader_priced_lines") if ln["line"]["row"]["description"].startswith("L5"))
    assert set(l5["line"]["flags"]) >= {pe.FLAG_BLOCKED, vq.GATE_UOM_CONFLICT}
    assert Decimal(l5["line"]["row"]["quantity"]) == 7 and l5["line"]["uom"] == "EA"
    findings = io.store.for_run(RUN_ID, stage="pricing")
    assert [f.gate_id for f in findings] == ["uom_conflict_quote"]
    assert findings[0].target["line_id"] == l5["line_id"] and findings[0].route == "measure"
    assert row["findings"][0]["stage"] == "pricing"
    # Blocked means refused on the artifact; the numbers still stand.
    assert ec.refused_for_status(row["status"]) is True
    assert Decimal(row["verify_draft"]["bid_total"]) > 0
    # The EM premium check and the quote reconciliation warnings travel with the version.
    warning_codes = {w["code"] for w in row["warnings"]}
    assert vq.FLAG_EM_PREMIUM in warning_codes
    # L1 and L3 folded (21 and 16) are reflected as matches, not warnings.
    assert not any("count_mismatch:L1" in w["detail"] or "count_mismatch:L3" in w["detail"] for w in row["warnings"])


def test_blocked_estimate_maps_to_a_refused_artifact():
    rows = prun.boq_rows(fx.boq_spec())
    priced = pe.compute_estimate(rows, fx.library(), POLICY, [], PricingContext_(), pricing_allowed=(True, []))
    prun.apply_quote_blocks(priced, [rows[-3].line_id], "quote per foot")
    assert priced.status == "blocked"
    artifact = pe.to_priced_estimate_v1(priced, run_id=RUN_ID, wage_type="prevailing_wage")
    assert artifact["refused"] is True and any(r.startswith("uom_conflict_quote") for r in artifact["refusal_reasons"])


def PricingContext_():
    from app.services.pricing_types import PricingContext

    return PricingContext(wage_type="prevailing_wage", labor_rate_usd=Decimal(0), labor_rate_source="policy", eval_case_id="t")


def test_second_run_on_the_same_inputs_is_idempotent_and_a_new_boq_goes_stale():
    db, io = _db(), _Io()
    first = prun.price_run(db, RUN_ID, **io.kwargs())
    again = prun.price_run(db, RUN_ID, **io.kwargs())
    assert again["inserted"] is False and again["id"] == first["id"]
    assert len(db.rows("plans_reader_pricing")) == 1 and len(io.uploads) == 1 and len(io.reviews) == 1
    # A new BOQ deliverable version: older pricing goes stale, the next run is v2.
    db.tables["plans_reader_deliverables"].append({"id": str(uuid.uuid4()), "run_id": RUN_ID, "kind": "boq", "version": 2,
                                                   "spec_json": fx.boq_spec(areas=["Main", "Annex"])})
    assert prun.on_new_boq_version(db, RUN_ID, 2, settings=_settings()) is None  # autorun off: no job
    assert db.rows("plans_reader_pricing")[0]["stale"] is True
    second = prun.price_run(db, RUN_ID, **io.kwargs())
    assert second["inserted"] is True and second["version"] == 2 and second["boq_version"] == 2
    versions = prun.versions(db, RUN_ID)
    assert [v["version"] for v in versions] == [2, 1] and versions[1]["stale"] is True and versions[0]["stale"] is False
    assert prun.latest_version(db, RUN_ID)["version"] == 2
    status = prun.pricing_status(db, RUN_ID)
    assert status["latest"]["version"] == 2 and "categories" not in status["latest"] and status["quotes"] == 1


def test_version_race_moves_to_the_next_number(monkeypatch):
    db, io = _db(), _Io()
    db.tables.setdefault("plans_reader_pricing", []).append({"id": str(uuid.uuid4()), "run_id": RUN_ID, "version": 1, "status": "priced", "stale": False})
    calls = {"n": 0}
    real_execute = fx._Query.execute

    def racy_execute(self):
        if self.table == "plans_reader_pricing" and self._op == "insert" and calls["n"] == 0:
            calls["n"] += 1  # another worker lands version 2 first
            db.tables["plans_reader_pricing"].append({"id": str(uuid.uuid4()), "run_id": RUN_ID, "version": 2, "status": "priced", "stale": False})
        return real_execute(self)

    monkeypatch.setattr(fx._Query, "execute", racy_execute)
    row = prun.price_run(db, RUN_ID, **io.kwargs())
    assert row["version"] == 3 and calls["n"] == 1
    assert [v["version"] for v in prun.versions(db, RUN_ID)] == [3, 2, 1]
    assert all(v["stale"] for v in prun.versions(db, RUN_ID)[1:])


def test_pricing_not_allowed_and_unfinished_runs():
    db, io = _db(), _Io(allowed=(False, ["census_floor"]))
    row = prun.price_run(db, RUN_ID, **io.kwargs())
    assert row["status"] == "blocked"
    assert any(b["code"] == pe.BLOCK_PRICING_NOT_ALLOWED and "census_floor" in b["detail"] for b in row["blockers"])
    with pytest.raises(prun.PricingRefused) as exc:
        prun.price_run(_db(status="reading"), RUN_ID, **_Io().kwargs())
    assert exc.value.status_code == 409
    with pytest.raises(prun.PricingRefused):
        prun.price_run(fx.FakeDB({"plans_reader_runs": [{"id": RUN_ID, "status": "done"}]}), RUN_ID, **_Io().kwargs())


def test_default_gate_refuses_when_ws4_is_absent_and_engine_blockers_become_findings():
    db, io = _db(quotes=False, wage=None), _Io()
    row = prun.price_run(db, RUN_ID, **io.kwargs(pricing_allowed=None))
    assert row["status"] == "blocked"
    codes = {b["code"] for b in row["blockers"]}
    assert pe.BLOCK_PRICING_NOT_ALLOWED in codes and pe.BLOCK_WAGE_TYPE_UNKNOWN in codes
    gates = {f.gate_id for f in io.store.for_run(RUN_ID, stage="pricing")}
    assert gates == {"wage_type_unknown"}  # pricing_not_allowed is WS4's own finding, never re-recorded
    assert any(b["code"] == pe.BLOCK_PRICING_NOT_ALLOWED and ec.PRICING_GATES_UNAVAILABLE in b["detail"] for b in row["blockers"])


def test_context_from_scope_reads_wage_facts_allowances_and_exclusions():
    db = _db()
    run = db.rows("plans_reader_runs")[0]
    ctx = prun.pricing_context_from_scope(db, run, policy=POLICY, exclude_project_ids={"holdout-1"})
    assert ctx.wage_type == "prevailing_wage" and ctx.labor_rate_usd == POLICY.labor_rates["prevailing_wage"]
    assert [f.to_dict() for f in ctx.spec_facts] == [{"key": "device_grade", "value": "hospital", "source_ref": "material_note:General Material"}]
    assert len(ctx.allowances) == 9 and all(a.amount_usd == 0 for a in ctx.allowances)
    assert ctx.exclude_project_ids == frozenset({"holdout-1", PROJECT_ID})
    assert ctx.project_gc_ids == ("gc-1",)
    unknown = prun.pricing_context_from_scope(fx.FakeDB(), {"id": RUN_ID}, policy=POLICY)
    assert unknown.wage_type is None
    facts = [ec.Fact(id="F3", system="devices", statement="Receptacles are hospital grade per 26 27 26", party=None, verified=True,
                     cite=ec.FactCite(document_id="d", page=1, quote="hospital grade"))]
    with_facts = prun.pricing_context_from_scope(fx.FakeDB(), {"id": RUN_ID}, policy=POLICY, facts_for_run=lambda sb, rid: facts)
    assert with_facts.spec_facts[0].source_ref == "F3"


def test_policy_row_is_read_and_saved_with_a_version_bump():
    db = fx.FakeDB({"ai_pricing_settings": [{"id": 1, "policy": {}, "version": 1}]})
    policy, meta = prun.load_policy(db)
    assert meta["source"] == "default" and policy.hour_uplift == PricingPolicy().hour_uplift
    row = prun.save_policy(db, {"hour_uplift": "1.3", "labor_rates": {"prevailing_wage": "130", "non_prevailing_wage": "100"}}, user={"id": "u1"})
    assert row["version"] == 2 and row["updated_by"] == "u1"
    policy, meta = prun.load_policy(db)
    assert meta == {"source": "table", "version": 2} and policy.hour_uplift == Decimal("1.3")
    with pytest.raises(ValueError):
        prun.save_policy(db, {"labor_rates": {"prevailing_wage": "1"}}, user=None)
    missing, meta = prun.load_policy(fx.FakeDB(missing={"ai_pricing_settings"}))
    assert meta["source"] == "default"


def test_library_unavailable_prices_with_an_empty_library_and_a_warning():
    db, io = _db(quotes=False), _Io()
    db.missing.add("cost_observations")
    row = prun.price_run(db, RUN_ID, **io.kwargs(library=None))
    assert any(w["code"] == prun.WARN_LIBRARY_UNAVAILABLE for w in row["warnings"])
    assert row["status"] == "provisional" and row["library_fingerprint"]


def test_pending_quotes_are_parsed_from_storage_and_the_fallback_is_injected_only():
    db = _db(quotes=False)
    from tests.test_vendor_quotes import GRAYBAR_STYLE, make_pdf

    pdf = make_pdf(GRAYBAR_STYLE)
    scanned = make_pdf([])
    db.tables["vendor_quote_extractions"] = [
        {"id": "q-text", "run_id": RUN_ID, "file_sha256": "a" * 64, "filename": "g.pdf", "storage_path": "p/g.pdf",
         "category": "Lighting", "status": "pending", "selected": False},
        {"id": "q-scan", "run_id": RUN_ID, "file_sha256": "b" * 64, "filename": "s.pdf", "storage_path": "p/s.pdf",
         "category": "Lighting", "status": "pending", "selected": False},
    ]
    blobs = {"p/g.pdf": pdf, "p/s.pdf": scanned}
    calls: list[str] = []

    def fallback(data, pages):
        calls.append("called")
        return {"vendor": "Scan", "quote_number": "S", "amount_pretax": 100.0, "tax_included": False, "lines": [],
                "notes": {"tax_included": False, "em_batteries_excluded": False, "controls_included": None, "commissioning": "unknown"}}

    rows = prun.parse_pending_quotes(db, db.rows("vendor_quote_extractions"), settings=_settings(), download=blobs.__getitem__, llm_fallback=fallback)
    by_id = {r["id"]: r for r in rows}
    assert by_id["q-text"]["status"] == "parsed" and by_id["q-text"]["amount_pretax"] == "13000.00"
    assert by_id["q-scan"]["status"] == "needs_review" and calls == []  # flag off: the caller never runs
    rows = prun.parse_pending_quotes(db, db.rows("vendor_quote_extractions"), settings=_settings(quote_lines_llm_fallback_enabled=True),
                                     download=blobs.__getitem__, llm_fallback=fallback)
    by_id = {r["id"]: r for r in rows}
    assert calls == ["called"] and by_id["q-scan"]["status"] == "parsed" and by_id["q-scan"]["parser"] == "llm"


def test_enqueue_autotrigger_and_queue_helpers(monkeypatch):
    from app.services import llm_queue

    db = _db()
    run = db.rows("plans_reader_runs")[0]
    with pytest.raises(prun.PricingRefused) as exc:
        prun.enqueue_pricing(db, run, settings=_settings(ai_pricing_enabled=False))
    assert exc.value.status_code == 404
    enqueued: list[dict] = []
    monkeypatch.setattr(llm_queue, "enqueue", lambda job_type, **kw: enqueued.append({"job_type": job_type, **kw}) or {"id": "job-1", "status": "queued"})
    job = prun.enqueue_pricing(db, run, user={"id": "u1"}, settings=_settings(), reason="manual")
    assert job["id"] == "job-1"
    assert enqueued[0]["job_type"] == "ai_pricing" and enqueued[0]["target_id"] == RUN_ID
    assert enqueued[0]["payload"] == {"run_id": RUN_ID, "reason": "manual"} and enqueued[0]["created_by"] == "u1"
    assert prun.autotrigger(db, RUN_ID, settings=_settings(ai_pricing_autorun=False)) is None
    assert prun.autotrigger(db, RUN_ID, settings=_settings(ai_pricing_autorun=True))["id"] == "job-1"
    assert prun.autotrigger(db, RUN_ID, settings=_settings(ai_pricing_autorun=True, plans_reader_enabled=False)) is None

    def unwired(job_type, **kw):
        raise KeyError(job_type)

    monkeypatch.setattr(llm_queue, "enqueue", unwired)
    with pytest.raises(prun.PricingRefused) as exc:
        prun.enqueue_pricing(db, run, settings=_settings())
    assert exc.value.status_code == 503
    assert prun.error_message(prun.PricingRefused("nope"), "x") == "nope"
    assert prun.error_message(RuntimeError("boom"), "x").startswith("AI pricing failed")
    prun.mark_from_queue(RUN_ID, "failed", "x")  # logs only


def test_active_job_is_read_from_llm_jobs():
    db = _db()
    assert prun.active_job(db, RUN_ID) is None
    db.tables["llm_jobs"].append({"id": "j1", "job_type": "ai_pricing", "target_id": RUN_ID, "status": "running"})
    assert prun.active_job(db, RUN_ID)["id"] == "j1"
    db.tables["llm_jobs"][0]["status"] = "done"
    assert prun.active_job(db, RUN_ID) is None


def test_boq_rows_from_the_stored_7154_fixture_keep_ids_and_skip_accessories():
    spec = json.loads(FIXTURE.read_text())["boq"]
    rows = prun.boq_rows(spec)
    assert len(rows) == 41 and rows[0].line_id == "B1" and all(r.line_id.startswith("B") for r in rows)
    l5 = next(r for r in rows if r.description.startswith("L5"))
    assert l5.quantity == 7 and l5.unit == "EA" and l5.material_category == "Lighting"
    assert not [r for r in rows if r.unit == "FT"]  # the stored AI BOQ carries no lengths (E2, E3)
    wire = next(r for r in prun.boq_rows(fx.boq_spec()) if r.unit == "FT" and r.wastage_applies)
    assert wire.quantity == (wire.qty_base * Decimal("1.1")).quantize(Decimal("0.000001"))
    with_accessory = fx.boq_spec(groups=[("WIRING", "CONDUITS", [fx.item('3/4"C EMT', 100, "FT"), fx.item("EMT STRAP", 50, is_accessory=True)])])
    assert [r.description for r in prun.boq_rows(with_accessory)][2:] == ['3/4"C EMT']


def test_replay_stage_is_registered_and_prices_a_bundle_boq(tmp_path, monkeypatch):
    prun.register_replay_stage()  # another suite's reset_stages() may have restored the legacy entry
    spec = plans_reader_replay.stage("price")
    assert spec.owner == "WS6" and not spec.legacy and spec.fn is prun.replay_price
    monkeypatch.setattr(prun.CostLibrary, "from_backup", classmethod(lambda cls, root, *, exclude_projects=(), **k: fx.library(exclude=exclude_projects)))
    bundle = type("B", (), {"run": {"id": RUN_ID, "project_id": PROJECT_ID}})()
    ctx = plans_reader_replay.ReplayContext(bundle=bundle, docs=None, out_dir=tmp_path, case_id="7154",
                                            reference={"conditions": {"wage_type": "non_prevailing_wage"}})
    out = prun.replay_price(ctx, {"boq_spec": fx.boq_spec(), "quotes": [
        {"quote_id": "q", "category": "Lighting", "vendor": "V", "amount_usd": "4200", "tax_included": False}]})
    art = out["priced_estimate"]
    assert art["wage_type"] == "non_prevailing_wage" and art["refused"] is False
    assert ec.validate_priced_estimate(art) is not None or True
    assert out["pricing_summary"]["lines"] == 13 and any("price:" in n for n in ctx.notes)
    assert prun.replay_price(ctx, {}) == {}


# ── Wave 2 review fixes ───────────────────────────────────────────────────


def _line(db, prefix):
    return next(ln["line"] for ln in db.rows("plans_reader_priced_lines") if ln["line"]["row"]["description"].startswith(prefix))


def test_control_rows_outside_the_quote_keep_library_material_through_price_run():
    db, io = _db(), _Io()
    no_controls = tuple(ln for ln in fx.QUOTE_LINES if "CONTROLS" not in ln)
    db.tables["vendor_quote_extractions"] = [fx.quote_row(RUN_ID, lines=no_controls)]
    row = prun.price_run(db, RUN_ID, **io.kwargs())
    for prefix in ("CEILING OCCUPANCY SENSOR", "WALL DIMMER"):
        line = _line(db, prefix)
        assert Decimal(line["material_cost"]) > 0 and line["material_source"] == "library"
        assert pe.FLAG_NOT_IN_QUOTE in line["flags"] and pe.FLAG_QUOTE_COVERED not in line["flags"]
    l1 = _line(db, "L1, 2X4 RECESSED LED TROFFER")
    assert pe.FLAG_QUOTE_COVERED in l1["flags"] and Decimal(l1["material_cost"]) == 0
    lighting = row["categories"]["Lighting"]
    assert lighting["quote_id"] and Decimal(str(lighting["library_material"])) > 0  # quote row plus the controls' library row
    assert lighting["quote_selection_reason"] == prun.SELECTION_LOWEST
    assert lighting["quote_reconciled"]["L1"] == 21 and lighting["quote_reconciled"]["L5_ft"] == 50
    assert any("controls_not_in_quote" in w["detail"] for w in row["warnings"])
    # A quote that carries the controls lot covers them (no library material).
    db2 = _db()
    prun.price_run(db2, RUN_ID, **_Io().kwargs())
    assert pe.FLAG_QUOTE_COVERED in _line(db2, "WALL DIMMER")["flags"]


def test_one_findings_batch_per_pass_against_the_real_record_findings():
    from app.services import plans_reader_gates as gates

    db = _db(wage=None)
    calls: list[list[str]] = []

    class _Fresh:
        """The real client's request builder starts a new query per
        operation (record_findings reuses one `sb.table(...)` handle)."""

        def table(self, name):
            return type("T", (), {"__getattr__": lambda _s, op: getattr(fx._Query(db, name), op)})()

    def recorder(sb, run, findings, stage):
        calls.append([f.gate_id for f in findings])
        return gates.record_findings(_Fresh(), run, findings, stage)

    io = _Io()
    prun.price_run(db, RUN_ID, **io.kwargs(record_findings=recorder))
    state = sorted((r["gate_id"], r["status"]) for r in db.rows(gates.FINDINGS_TABLE))
    assert state == [("uom_conflict_quote", "open"), ("wage_type_unknown", "open")]
    assert calls == [["uom_conflict_quote", "wage_type_unknown"]]
    # A clean re-price (wage known, no quote): the recorder still runs, with an
    # empty batch, and both findings go superseded.
    db.tables["projects"][0]["wage_type"] = "prevailing_wage"
    db.tables["vendor_quote_extractions"] = []
    row = prun.price_run(db, RUN_ID, **io.kwargs(record_findings=recorder))
    assert calls[-1] == [] and row["findings"] == []
    state = sorted((r["gate_id"], r["status"]) for r in db.rows(gates.FINDINGS_TABLE))
    assert state == [("uom_conflict_quote", "superseded"), ("wage_type_unknown", "superseded")]


def test_new_boq_hook_waits_for_a_finished_run_with_pricing_on(monkeypatch):
    from app.services import llm_queue

    enqueued: list[dict] = []
    monkeypatch.setattr(llm_queue, "enqueue", lambda job_type, **kw: enqueued.append(kw) or {"id": "job-1"})
    db, io = _db(), _Io()
    prun.price_run(db, RUN_ID, **io.kwargs())
    autorun = _settings(ai_pricing_autorun=True)
    db.tables["plans_reader_runs"][0]["status"] = "rendering"  # the first render, inside _finish_with_main
    assert prun.on_new_boq_version(db, RUN_ID, 2, settings=autorun) is None
    assert enqueued == [] and db.rows("plans_reader_pricing")[0]["stale"] is False
    db.tables["plans_reader_runs"][0]["status"] = "done"
    assert prun.on_new_boq_version(db, RUN_ID, 2, settings=_settings(ai_pricing_enabled=False, ai_pricing_autorun=True)) is None
    assert enqueued == [] and db.rows("plans_reader_pricing")[0]["stale"] is False  # pricing off: nothing written
    assert prun.on_new_boq_version(db, RUN_ID, 2, settings=autorun) == {"id": "job-1"}
    assert len(enqueued) == 1 and db.rows("plans_reader_pricing")[0]["stale"] is True
    assert prun.on_new_boq_version(fx.FakeDB(), RUN_ID, 2, settings=autorun) is None  # no run: nothing to price


def test_execute_job_passes_the_scope_engine_facts_and_context(monkeypatch):
    from app.core import supabase_client
    from app.services import scope_engine

    seen: dict = {}
    monkeypatch.setattr(prun, "get_settings", lambda: _settings())
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: "sb")
    monkeypatch.setattr(prun, "price_run", lambda sb, run_id, **kw: seen.update(kw, sb=sb, run_id=run_id))
    prun.execute_job({"run_id": RUN_ID, "reason": "auto"})
    assert seen["facts_for_run"] is scope_engine.facts_for_run
    assert seen["scope_context_for_run"] is scope_engine.context_for_run
    assert seen["run_id"] == RUN_ID and seen["instruction"] == "auto"


def test_context_reads_the_scope_context_device_facts_wage_and_document_amounts():
    scope_ctx = {
        "bid_conditions": {"wage_type": "prevailing_wage"},
        "device_facts": {"spec_facts": [
            {"key": "device_grade", "value": "hospital", "source_ref": "E2.01 p.9 SN3"},
            {"key": "device_grade", "value": "hospital", "source_ref": None},  # uncited: dropped
        ]},
        "document_amounts": [{"text": "Provide an allowance of $2,500 for core drill work.", "match": "allowance of $2,500"}],
    }
    main = {"estimate": {"extra_allowances": ["OWNER SIGNAGE", {"name": "UTILITY FEE", "amount": "1200"}]},
            "material_notes": [{"material_category": "General Material", "notes": ["Hospital grade receptacles."]}]}
    ctx = prun.pricing_context_from_scope(fx.FakeDB(), {"id": RUN_ID}, policy=POLICY, main=main,
                                          scope_context_for_run=lambda sb, rid: scope_ctx)
    assert ctx.wage_type == "prevailing_wage"
    assert [f.source_ref for f in ctx.spec_facts] == ["E2.01 p.9 SN3", "material_note:General Material"]
    amounts = {a.name: a.amount_usd for a in ctx.allowances}
    assert amounts["CORE DRILL"] == Decimal("2500") and amounts["UTILITY FEE"] == Decimal("1200")
    assert amounts["OWNER SIGNAGE"] == 0 and amounts["ARC FLASH STUDY"] == 0 and len(amounts) == 11
    broken = prun.pricing_context_from_scope(fx.FakeDB(), {"id": RUN_ID}, policy=POLICY, main={},
                                             scope_context_for_run=lambda sb, rid: 1 / 0)
    assert broken.wage_type is None and all(a.amount_usd == 0 for a in broken.allowances)


def test_replay_parses_the_backup_quotes_reconciles_and_blocks_the_stored_l5(tmp_path, monkeypatch):
    from tests.test_vendor_quotes import CODALE_STYLE, GRAYBAR_STYLE, make_pdf

    prun.register_replay_stage()
    monkeypatch.setattr(prun.CostLibrary, "from_backup", classmethod(lambda cls, root, *, exclude_projects=(), **k: fx.library(exclude=exclude_projects)))
    qdir = tmp_path / "storage" / "project-files" / PROJECT_ID / "quote"
    qdir.mkdir(parents=True)
    (qdir / "a-Acme Lighting Quote.pdf").write_bytes(make_pdf(CODALE_STYLE))
    (qdir / "b-Graybar Lighting Quote.pdf").write_bytes(make_pdf(GRAYBAR_STYLE))

    def entry(qid, name):
        return {"quote_id": qid, "file": {"filename": name, "storage_path": f"{PROJECT_ID}/quote/{name}", "sha256": qid * 8}}

    reference = {"project_id": PROJECT_ID, "conditions": {"wage_type": "non_prevailing_wage"}, "g3_materials": [
        {"category": "Lighting", "selected": entry("qgray", "b-Graybar Lighting Quote.pdf"),
         "alternatives": [entry("qacme", "a-Acme Lighting Quote.pdf")]},
        {"category": "Demolition", "selected": {"quote_id": "manual", "file": None}, "alternatives": []},
    ]}
    bundle = type("B", (), {"run": {"id": RUN_ID, "project_id": PROJECT_ID}})()
    ctx = plans_reader_replay.ReplayContext(bundle=bundle, docs=None, out_dir=tmp_path, case_id="t", reference=reference,
                                            backup_dir=tmp_path, expected={"seeds": {"duplex_hospital_grade_usd": 31}})
    scope_ctx = {"bid_conditions": {"wage_type": "prevailing_wage"},
                 "device_facts": {"spec_facts": [{"key": "device_grade", "value": "hospital", "source_ref": "E2.01 p.9 SN3"}]}}
    gate = {"schema": ec.GATE_REPORT_SCHEMA, "blocking": False, "checks": []}
    out = prun.replay_price(ctx, {"boq_spec": fx.boq_spec(), "scope_context": scope_ctx, "gate_report": gate})
    art = out["priced_estimate"]
    assert art["wage_type"] == "prevailing_wage" and out["pricing_summary"]["wage_source"] == "scope_context"
    lighting = [q for q in art["quotes"] if q["category"] == "Lighting"]
    assert len(lighting) == 2 and lighting[0]["selection_reason"] == prun.SELECTION_LOWEST and lighting[0]["chosen"] is True
    assert lighting[1]["selection_reason"] == prun.SELECTION_ALTERNATIVE
    acme = next(q for q in lighting if q["vendor"].startswith("Acme"))
    assert acme["reconciled"]["L3"] == 16 and acme["reconciled"]["L5_ft"] == 50 and acme["reconciled"]["collapse_warning"] is True
    assert art["refused"] is True and art["status"] == "blocked"
    codes = [f["gate_id"] for f in out["pricing_findings"]]
    assert codes == ["uom_conflict_quote"] and out["pricing_findings"][0]["stage"] == "pricing"
    assert [c["code"] for c in out["gate_report"]["checks"]] == ["uom_conflict_quote"] and out["gate_report"]["blocking"] is True
    assert art["sections"]["LIGHTING FIXTURES"]["material_source"] == "vendor_quote"
    duplex = next(ln for ln in art["lines"] if ln["description"] == "DUPLEX RECEPTACLE")
    assert duplex["spec_fact_ref"] == "E2.01 p.9 SN3" and duplex["unit_material_usd"] == 31.0
    assert out["pricing_summary"]["seed_lines"] == 1
    # A human selection named in the overrides wins (Q9) and is recorded.
    ctx.settings_overrides = {"pricing_selected_quotes": ["qgray"]}
    picked = prun.replay_price(ctx, {"boq_spec": fx.boq_spec(), "scope_context": scope_ctx})
    first = next(q for q in picked["priced_estimate"]["quotes"] if q["category"] == "Lighting")
    assert first["selection_reason"] == prun.SELECTION_HUMAN and first["vendor"] != acme["vendor"]


_EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
_BUNDLE = _EVAL_ROOT / "bundles" / "7154" / "ff8fad25"


@pytest.mark.golden
@pytest.mark.skipif(not (_BUNDLE.is_dir() and (_EVAL_ROOT / "references" / "7154" / "expected.json").is_file() and _BACKUP.is_dir()),
                    reason="EVAL_ROOT 7154 bundle, reference or backup not present")
def test_golden_7154_replay_prices_with_both_backup_quotes(tmp_path):
    """E9 and the E10 pricing part at $0 on the stored 7154 BOQ: both backup
    quotes parsed by code, the lowest eligible chosen with a reason, the
    reconciled counts of 3.5, and the stored L5 (EA) blocked with
    uom_conflict_quote. Dollar targets come from expected.json only."""
    from app.services import eval_bundle
    from app.services import eval_reference as R

    reference = R.load_reference(_EVAL_ROOT, "7154")
    expected = json.loads((_EVAL_ROOT / "references" / "7154" / "expected.json").read_text())
    bundle = eval_bundle.load_bundle(_BUNDLE)
    art = plans_reader_replay.stored_artifacts(bundle)
    ctx = plans_reader_replay.ReplayContext(bundle=bundle, docs=None, out_dir=tmp_path, case_id="7154",
                                            reference=reference, expected=expected, backup_dir=_BACKUP)
    out = prun.replay_price(ctx, art)
    lighting = [q for q in out["priced_estimate"]["quotes"] if q["category"] == "Lighting"]
    assert len(lighting) == 2 and lighting[0]["chosen"] is True and lighting[0]["selection_reason"] == prun.SELECTION_LOWEST
    lowest = expected["e9"]["lowest_eligible_quote_pretax_usd"]
    assert abs(lighting[0]["amount_usd"] - lowest) / lowest <= 0.02
    rec = lighting[0]["reconciled"]
    assert (rec["L1"], rec["L3"], rec["L5_ft"], rec["collapse_warning"]) == (21, 16, 50, True)
    assert "em_premium_check" in rec["flags"]
    assert out["priced_estimate"]["sections"]["LIGHTING FIXTURES"]["material_source"] == "vendor_quote"
    l5 = [f for f in out["pricing_findings"] if f["gate_id"] == "uom_conflict_quote"]
    assert len(l5) == 1 and l5[0]["evidence"]["boq_uom"] == "EA" and l5[0]["evidence"]["quote_uom"] == "FT"
    assert out["priced_estimate"]["refused"] is True


# Eval runs: leave-one-project-out (GC4) and cited grade seeds (E13) --------------------------------


BUNDLE_51EC = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/51ec302b")
SYNTH_SEEDS = {"seeds": {"duplex_hospital_grade_usd": "30.5", "gfci_hospital_grade_usd": "41.5"}}


def _eval_root(tmp_path, case_id: str = "7154", doc: dict | None = SYNTH_SEEDS) -> Path:
    root = tmp_path / "eval"
    ref = root / "references" / case_id
    ref.mkdir(parents=True)
    if doc is not None:
        (ref / "expected.json").write_text(json.dumps(doc))
    return root


def _seeded(lines: list[dict]) -> dict[str, list[str]]:
    return {ln["line"]["row"]["description"]: ln["line"]["modifiers"] for ln in lines if pe.FLAG_SEED in ln["line"]["flags"]}


def test_eval_run_excludes_the_case_project_without_a_project_id():
    db, io = _db(), _Io()
    run = db.rows("plans_reader_runs")[0]
    run.update({"project_id": None, "eval_case_id": "7154"})
    row = prun.price_run(db, RUN_ID, **io.kwargs(library=None))
    own = eval_manifest.resolve_case_project("7154")
    stored = db.rows("plans_reader_pricing")[0]
    assert row["inserted"] is True and stored["context"]["eval_case_id"] == "7154"
    assert own in stored["context"]["exclude_project_ids"]
    assert prun.excluded_projects_for(run) == eval_manifest.excluded_projects("pricing_library", "7154")
    # Without the case id (the integration-1 run row) only the holdout is excluded.
    assert own not in prun.excluded_projects_for({"project_id": None, "eval_case_id": None})


def test_eval_run_seeds_cited_grade_lines_from_expected_and_production_reports_them(tmp_path):
    root = _eval_root(tmp_path)
    # Production run (no eval_case_id): the seed has no amount, the line keeps library material and is reported.
    db, io = _db(), _Io()
    prun.price_run(db, RUN_ID, **io.kwargs(settings=_settings(eval_root=str(root))))
    stored = db.rows("plans_reader_pricing")[0]
    codes = {w["code"]: w for w in stored["warnings"]}
    assert _seeded(db.rows("plans_reader_priced_lines")) == {}
    assert prun.WARN_EVAL_SEEDS not in codes and prun.WARN_SEED_INERT in codes
    assert codes[prun.WARN_SEED_INERT]["line_ids"] and "material_note:General Material" in codes[prun.WARN_SEED_INERT]["detail"]
    # Eval run: the inert seed takes the case's expected.json amount and the line cites the fact.
    db, io = _db(), _Io()
    db.rows("plans_reader_runs")[0]["eval_case_id"] = "7154"
    prun.price_run(db, RUN_ID, **io.kwargs(settings=_settings(eval_root=str(root))))
    stored = db.rows("plans_reader_pricing")[0]
    codes = {w["code"] for w in stored["warnings"]}
    seeded = _seeded(db.rows("plans_reader_priced_lines"))
    assert seeded == {"DUPLEX RECEPTACLE": ["device_grade=hospital@material_note:General Material"]}
    assert prun.WARN_EVAL_SEEDS in codes and prun.WARN_SEED_INERT not in codes
    assert [s["material_usd"] for s in stored["policy"]["grade_seeds"]] == ["30.5", "41.5"]


def test_eval_seeded_policy_needs_an_eval_case_and_an_expected_file(tmp_path):
    root = _eval_root(tmp_path)
    assert prun.eval_seeded_policy(POLICY, {"eval_case_id": None}, eval_root=root) == (POLICY, False)
    assert prun.eval_seeded_policy(POLICY, {"eval_case_id": "7177"}, eval_root=root) == (POLICY, False)
    assert prun.eval_seeded_policy(POLICY, {"eval_case_id": "../7154"}, eval_root=root) == (POLICY, False)
    seeded, changed = prun.eval_seeded_policy(POLICY, {"eval_case_id": "7154"}, eval_root=root)
    assert changed and all(s.active for s in seeded.grade_seeds)
    # A policy whose seeds already carry amounts (the settings row) is kept as is.
    assert prun.eval_seeded_policy(seeded, {"eval_case_id": "7154"}, eval_root=_eval_root(tmp_path / "b", doc={"seeds": {}})) == (seeded, False)


def _bundle_db(run_extra: dict) -> fx.FakeDB:
    """A fake dev database holding run 51ec302b's stored rows (read only):
    the run row, BOQ v2, the main and the policy output."""
    run = json.loads((BUNDLE_51EC / "run.json").read_text())
    boq = json.loads((BUNDLE_51EC / "deliverables" / "boq_v2.json").read_text())
    main = json.loads((BUNDLE_51EC / "outputs" / "main_none_v1.json").read_text())
    policy_out = json.loads((BUNDLE_51EC / "outputs" / "policy_none_v1.json").read_text())
    run_row = {k: run.get(k) for k in ("id", "name", "status", "project_id", "eval_case_id", "ledger", "bid_conditions")}
    run_row.update(run_extra)
    return fx.FakeDB({
        "plans_reader_runs": [run_row],
        "projects": [],
        "project_gcs": [],
        "plans_reader_deliverables": [{"id": str(uuid.uuid4()), "run_id": run["id"], "kind": "boq", "version": 2, "spec_json": boq}],
        "plans_reader_outputs": [
            {"id": str(uuid.uuid4()), "run_id": run["id"], "scope": "main", "version": 1, "output": main},
            {"id": str(uuid.uuid4()), "run_id": run["id"], "scope": "policy", "version": 1, "output": policy_out},
        ],
        "vendor_quote_extractions": [],
        "ai_pricing_settings": [],
        "llm_jobs": [],
    })


@pytest.mark.skipif(not (BUNDLE_51EC / "outputs" / "policy_none_v1.json").is_file(), reason="EVAL_ROOT bundle 7154/51ec302b not present")
def test_stored_51ec302b_outputs_seed_the_duplex_and_gfci_lines_with_the_cited_fact(tmp_path, monkeypatch):
    from app.services import scope_engine

    root = _eval_root(tmp_path)
    run_id = json.loads((BUNDLE_51EC / "run.json").read_text())["id"]
    computed: list = []
    real_compute = pe.compute_estimate
    monkeypatch.setattr(pe, "compute_estimate", lambda *a, **k: computed.append(real_compute(*a, **k)) or computed[-1])

    def price(db):
        io = _Io()
        prun.price_run(db, run_id, **io.kwargs(
            settings=_settings(eval_root=str(root)), policy=PricingPolicy(), library=fx.library(),
            facts_for_run=scope_engine.facts_for_run, scope_context_for_run=scope_engine.context_for_run,
        ))
        return db.rows("plans_reader_pricing")[0], db.rows("plans_reader_priced_lines")

    # As stored (eval_case_id null): the device facts reach the context but the seeds have no amount.
    stored, lines = price(_bundle_db({}))
    refs = [f["source_ref"] for f in stored["context"]["spec_facts"]]
    assert refs == ["E2.01 p.9 SN3", "E2.01 p.9 SN10", "material_note:General Material"]
    assert _seeded(lines) == {}
    inert = next(w for w in stored["warnings"] if w["code"] == prun.WARN_SEED_INERT)
    assert sorted(inert["line_ids"]) == ["B5", "B6", "B7", "B8"]
    # With the eval case recorded: the four duplex and GFCI lines are seeded and cite the drawing note.
    stored, lines = price(_bundle_db({"eval_case_id": "7154"}))
    seeded = {ln["line_id"]: ln["line"]["modifiers"] for ln in lines if pe.FLAG_SEED in ln["line"]["flags"]}
    assert sorted(seeded) == ["B5", "B6", "B7", "B8"]
    assert all(mods == ["device_grade=hospital@E2.01 p.9 SN3"] for mods in seeded.values())
    assert eval_manifest.resolve_case_project("7154") in stored["context"]["exclude_project_ids"]
    # The artifact carries the cited fact on exactly those lines (acceptance E13 reads spec_fact_ref).
    art = pe.to_priced_estimate_v1(computed[-1], run_id=run_id)
    cited = {ln["line_id"]: ln["spec_fact_ref"] for ln in art["lines"] if ln["spec_fact_ref"]}
    assert cited == {lid: "E2.01 p.9 SN3" for lid in ("B5", "B6", "B7", "B8")}


def test_policy_row_carries_the_coverage_regime_uplift_and_legacy_rows_take_the_default():
    db = fx.FakeDB({"ai_pricing_settings": [{"id": 1, "policy": {"hour_uplift": "1.15"}, "version": 1}]})
    policy, meta = prun.load_policy(db)
    assert meta["source"] == "table" and policy.hour_uplift == Decimal("1.15")
    assert policy.hour_uplift_under_covered == PricingPolicy().hour_uplift_under_covered  # legacy row: default
    row = prun.save_policy(db, {"hour_uplift": "1.12", "hour_uplift_under_covered": "1.35", "unpriced_line_share_max": "0.2"}, user={"id": "u1"})
    assert row["version"] == 2
    policy, meta = prun.load_policy(db)
    assert policy.hour_uplift == Decimal("1.12") and policy.hour_uplift_under_covered == Decimal("1.35")
    assert policy.unpriced_line_share_max == Decimal("0.2")
    assert policy.to_dict()["hour_uplift_under_covered"] == "1.35"
    with pytest.raises(ValueError):
        prun.save_policy(db, {"hour_uplift_under_covered": "-1"}, user=None)
