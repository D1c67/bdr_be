"""WS8 autonomy status (spec 5.4 AutonomyStatus, 1.4; WS8 acceptance
"autonomy on the stored 7154 reports blocked with integrity_gates and
lengths_produced failing"). Every source is a fake fed from fixtures or
synthetic data; the Supabase provider is exercised with a fake client. No
database, no model."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services import plans_reader_autonomy as AU
from app.services import plans_reader_evidence as E
from app.services.estimating_contracts import GateFinding
from app.services.plans_reader_evidence_inputs import UnavailableEvidenceInputs
from app.services.plans_reader_rows import iter_boq_lines
from app.services.pricing_types import PricedLine, PriceRow, ValueStat

FIX = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7154"


def _load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def _real_gate_findings(run: dict, boq: dict, main: dict, cats: list[dict]) -> list[GateFinding]:
    """What WS4's final gates actually raise on the stored v1 BOQ: the real
    plans_reader_gates.run_final over the run_7154 fixtures (legacy mode,
    no measure output)."""
    from app.services import plans_reader_gates as pg

    report = pg.run_final(boq=boq, run=run, main=main, category_outputs=cats)
    assert report.mode == "legacy"
    return list(report.findings)


def _stored_7154_sources() -> AU.FakeAutonomySources:
    run = _load("run.json")
    boq = _load("deliverables/boq_v1.json")
    docs = _load("documents.json")
    main = _load("outputs/main_none_v1.json")
    cats = [_load(f"outputs/category_{c}_v1.json") for c in ("electrical_drawings", "rfp", "specifications")]
    evidence = E.build(
        None, run["id"], main_version=1, boq_version=1,
        inputs=E.FakeRunInputs(documents=docs, main=main, boq=boq, categories=cats,
                               marked=_load("deliverables/marked_plans_v1.json")),
        sheets=UnavailableEvidenceInputs(),
    ).to_dict()
    return AU.FakeAutonomySources(
        run=run,
        groups=_load("groups.json"),
        gate_findings=_real_gate_findings(run, boq, main, cats),
        boq=(1, boq),
        measure=None,
        scope=None,
        pricing=None,
        review=None,
        evidence=evidence,
    )


def test_stored_7154_bundle_is_blocked_with_integrity_gates_and_lengths_failing():
    src = _stored_7154_sources()
    st = AU.status(None, "ff8fad25-ee33-4a0e-b817-09f5ee5f562f", include_pricing=False, sources=src,
                   pricing_enabled=False)
    assert st.overall == "blocked"
    states = {c.key: c.state for c in st.checks}
    assert states["integrity_gates"] == "fail" and states["lengths_produced"] == "fail"
    gates = st.check("integrity_gates")
    # The real WS4 gates on the stored v1: the E2 and E10 blocks plus the
    # ED3.01 tile subtotal (E11) and the capability-denial prose (E1).
    assert {r.split(":")[1] for r in gates.reasons} == {
        "linear_in_ea", "new_devices_no_branch", "partial_basis", "capability_denial"}
    lengths = st.check("lengths_produced")
    assert "boq:linear_in_ea:B8" in lengths.reasons and "boq:no_branch_lengths" in lengths.reasons
    assert any(r.startswith("gate:new_devices_no_branch:") for r in lengths.reasons)
    assert lengths.params["measure_available"] is False and lengths.params["linear_in_ea"] == 1
    assert states["reading_complete"] == "pass"
    assert states["evidence"] == "fail"  # legacy lines carry no provenance
    assert states["pricing_coverage"] == "na"
    assert states["scope_conditions"] == states["independent_review"] == states["history_bands"] == "skipped"
    assert st.passed == 1 and st.total == 7
    assert set(st.failing()) >= {"integrity_gates", "lengths_produced", "evidence"}
    d = st.to_dict()
    assert d["overall"] == "blocked" and len(d["checks"]) == 8
    assert d["checks"][1]["summary_key"] == "aiEstimating.autonomy.check.integrity_gates.fail"
    assert d["checks"][1]["tab"] == "checks"


def test_boq_code_checks_work_without_gate_findings():
    boq = _load("deliverables/boq_v1.json")
    reasons, params = AU.boq_length_problems(boq)
    assert reasons == ["boq:linear_in_ea:B8", "boq:no_branch_lengths"]
    assert params["devices"] > 0 and params["branch_ft_lines"] == 0.0
    check = AU.check_lengths(None, (1, boq), None, None, active=False)
    assert check.state == "fail"


def _boq_with(*items: tuple[str, float, str]) -> dict:
    lines = [{"line_id": f"B{i}", "description": d, "quantity": q, "unit": u} for i, (d, q, u) in enumerate(items, start=1)]
    return {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [
        {"name": "OTHER ITEMS", "subgroups": [{"name": "", "items": lines}]}]}]}]}


@pytest.mark.parametrize(
    ("extra", "fails"),
    [
        ([("#12 THHN/THWN Cu", 900, "FT"), ('3/4"C EMT', 300, "FT")], False),
        ([("#12 THHN/THWN Cu", 900, "FT")], True),  # conductor alone
        ([('3/4"C EMT', 300, "FT")], True),  # conduit alone
        ([("#18/4C CONTROL CABLE", 141, "FT")], True),  # control cable is not branch wiring
        ([("#18/4C CONTROL CABLE", 141, "FT"), ("CAT6 CABLE", 500, "FT")], True),
        ([("#12/2 MC CABLE", 600, "FT")], False),  # a power cable is conductor and sheath
        ([("#12 THHN/THWN Cu", 900, "FT"), ("SURFACE RACEWAY", 40, "FT")], False),
    ],
)
def test_new_devices_need_branch_conductor_and_conduit_feet(extra, fails):
    boq = _boq_with(("DUPLEX RECEPTACLE", 148, "EA"), *extra)
    reasons, params = AU.boq_length_problems(boq)
    assert ("boq:no_branch_lengths" in reasons) is fails, (extra, params)
    assert params["devices"] == 148


def test_stored_7154_with_a_control_cable_line_still_has_no_branch_lengths():
    boq = _load("deliverables/boq_v1.json")
    items = boq["areas"][0]["sections"][-1]["groups"][-1]["subgroups"][0]["items"]
    items.append({"line_id": "B99", "description": "#18/4C CONTROL CABLE", "quantity": 141, "unit": "FT"})
    assert "B99" in {ln["line_id"] for ln in iter_boq_lines(boq)}
    reasons, params = AU.boq_length_problems(boq)
    assert "boq:no_branch_lengths" in reasons
    assert params["branch_conductor_ft"] == 0.0 and params["branch_raceway_ft"] == 0.0


# Synthetic runs ---------------------------------------------------------------------


def _boq_ok() -> dict:
    return {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [
        {"name": "OTHER ITEMS", "subgroups": [{"name": "", "items": [
            {"line_id": "B1", "description": "DUPLEX RECEPTACLE", "quantity": 10, "unit": "EA"}]}]},
        {"name": "WIRING", "subgroups": [{"name": "", "items": [
            {"line_id": "B2", "description": "#12 THHN/THWN Cu", "quantity": 900, "unit": "FT"},
            {"line_id": "B3", "description": '3/4"C EMT', "quantity": 300, "unit": "FT"}]}]},
    ]}]}]}


def _priced_line(lid: str, quality: str = "strong", source: str = "library", flags: tuple[str, ...] = ()) -> dict:
    row = PriceRow(line_id=lid, description="X", group="OTHER ITEMS", subgroup=None, section=None, unit="EA",
                   quantity=Decimal(1), qty_base=Decimal(1), wastage_applies=False)
    method = "seed" if quality == "policy" else "exact"
    stat = ValueStat(value=Decimal("1"), n_obs=12, n_projects=9, quality=quality, method=method)
    return PricedLine(line_id=lid, row=row, item_key="k", family="f", uom="EA", qty_eff=Decimal(1), labor=stat,
                      material=stat, material_source=source, labor_hours=Decimal("2"), material_cost=Decimal("50"),
                      flags=flags).to_dict()


def _sources(**over: Any) -> AU.FakeAutonomySources:
    base: dict[str, Any] = {
        "run": {"id": "r1", "status": "done", "gate_summary": {"blocking": False}},
        "groups": [{"id": "g1", "kind": "sub", "status": "done", "category": "electrical_drawings"}],
        "gate_findings": [],
        "boq": (1, _boq_ok()),
        "measure": {"routes": [{"basis": "measured_route", "total_ft": 300.0}]},
        "scope": {"bid_conditions": {"wage_type": "prevailing_wage", "wage_basis": "project_record", "needs_review": []},
                  "scope_report": {"needs_review": []}},
        "pricing": {"version": 2, "boq_version": 1, "status": "priced", "verify_draft": {"labor_rate": "125"},
                    "lines": [_priced_line("B1"), _priced_line("B2"), _priced_line("B3", source="quote:Q1", quality="quote")]},
        "review": {"verdict": "clear", "findings": [], "bands": [{"metric": "hours_per_device", "severity": "none"}]},
        "evidence": {"stats": {"provenance_completeness": 1.0, "density": 0.97, "anchor_share": 0.95,
                               "ft_lines": 2, "ft_lines_with_polyline": 2, "length_mismatch": 0}},
        "signoff": None,
    }
    base.update(over)
    return AU.FakeAutonomySources(**base)


def _status(src: AU.FakeAutonomySources, **kw: Any) -> AU.AutonomyStatus:
    kw.setdefault("include_pricing", True)
    kw.setdefault("pricing_enabled", True)
    return AU.status(None, "r1", sources=src, **kw)


def test_fully_passing_run_is_ready_for_verification():
    st = _status(_sources())
    assert {c.key: c.state for c in st.checks} == dict.fromkeys(AU.CHECK_KEYS, "pass")
    assert st.overall == "ready_for_verification" and st.passed == st.total == 8
    assert st.pricing_version == 2 and st.boq_version == 1


def test_missing_providers_are_pending_while_active_then_skipped():
    src = _sources(scope=None, review=None, evidence=None, pricing=None,
                   run={"id": "r1", "status": "rendering", "gate_summary": {}})
    active = _status(src)
    assert active.check("scope_conditions").state == "pending" and active.overall == "in_progress"
    done = _status(_sources(scope=None, review=None, evidence=None, pricing=None))
    states = {c.key: c.state for c in done.checks}
    assert states["scope_conditions"] == states["independent_review"] == states["evidence"] == "skipped"
    assert states["pricing_coverage"] == "skipped"
    assert done.check("evidence").reasons == ("provider_unavailable",)
    assert done.overall == "needs_review"
    chain = _status(_sources(review=None), chain_active=True)
    assert chain.check("independent_review").state == "pending" and chain.overall == "in_progress"


def test_legacy_run_without_gates_or_evidence_is_not_evaluated():
    st = _status(_sources(gate_findings=None, evidence=None, run={"id": "r1", "status": "done"}))
    assert st.overall == "not_evaluated"
    st2 = _status(_sources(gate_findings=[], evidence=None, run={"id": "r1", "status": "done"}))
    assert st2.check("integrity_gates").reasons == ("gates_not_run",) and st2.overall == "not_evaluated"


def test_overall_precedence():
    def c(state: str, overridden: bool = False) -> AU.AutonomyCheck:
        return AU.AutonomyCheck("evidence", state, overridden=overridden)

    assert AU.overall_state([c("pass"), c("fail")], legacy=True) == "not_evaluated"
    assert AU.overall_state([c("fail"), c("pending")], legacy=False) == "in_progress"
    assert AU.overall_state([c("warn"), c("fail")], legacy=False) == "blocked"
    assert AU.overall_state([c("pass"), c("fail", overridden=True)], legacy=False) == "needs_review"
    assert AU.overall_state([c("pass"), c("skipped")], legacy=False) == "needs_review"
    assert AU.overall_state([c("pass"), c("na")], legacy=False) == "ready_for_verification"


def test_overridden_block_is_a_warning_and_needs_review():
    f = GateFinding(run_id="r1", output_version=1, stage="final", gate_id="census_floor", severity="block",
                    status="overridden", target={"family": "L1"})
    st = _status(_sources(gate_findings=[f]))
    gates = st.check("integrity_gates")
    assert gates.state == "warn" and gates.overridden and st.overall == "needs_review"
    open_warn = GateFinding(run_id="r1", output_version=1, stage="final", gate_id="branch_ratio", severity="warn")
    assert _status(_sources(gate_findings=[open_warn])).check("integrity_gates").state == "warn"
    pre = GateFinding(run_id="r1", output_version=1, stage="pre", gate_id="census_floor", severity="block")
    assert _status(_sources(gate_findings=[pre])).check("integrity_gates").state == "pass"  # pre stage only


def test_reading_and_lengths_states():
    failed_drawing = [{"id": "g1", "kind": "sub", "status": "failed", "category": "electrical_drawings"}]
    failed_specs = [{"id": "g2", "kind": "sub", "status": "failed", "category": "specifications"}]
    assert _status(_sources(groups=failed_drawing)).check("reading_complete").state == "fail"
    assert _status(_sources(groups=failed_specs)).check("reading_complete").state == "warn"
    lost = GateFinding(run_id="r1", output_version=1, stage="final", gate_id="lost_sheets", severity="block")
    assert _status(_sources(gate_findings=[lost])).check("reading_complete").state == "fail"
    assumed = _status(_sources(measure={"routes": [{"basis": "assumed_route", "total_ft": 80.0},
                                                   {"basis": "measured_route", "total_ft": 20.0}]}))
    assert assumed.check("lengths_produced").state == "warn"
    assert "measure:assumed_routes_over_half" in assumed.check("lengths_produced").reasons
    assert _status(_sources(measure=None)).check("lengths_produced").state == "warn"
    undrawn = _status(_sources(evidence={"stats": {"provenance_completeness": 1.0, "density": 1.0, "ft_lines": 2,
                                                    "ft_lines_with_polyline": 1, "length_mismatch": 0}}))
    assert undrawn.check("lengths_produced").state == "warn" and undrawn.check("evidence").state == "warn"


def test_scope_states():
    def scope(wage: Any, basis: Any, needs: list[str] | None = None) -> dict:
        return {"bid_conditions": {"wage_type": wage, "wage_basis": basis, "needs_review": needs or []},
                "scope_report": {"needs_review": []}}

    assert _status(_sources(scope=scope(None, None))).check("scope_conditions").reasons == ("wage_unknown",)
    assert _status(_sources(scope=scope("non_prevailing_wage", "default"))).check("scope_conditions").state == "warn"
    assert _status(_sources(scope=scope("prevailing_wage", "document_cue", ["bond"]))).check("scope_conditions").state == "warn"
    conflict = GateFinding(run_id="r1", output_version=1, stage="final", gate_id="fact_conflict", severity="block",
                           target={"fact_id": "F2"})
    assert _status(_sources(gate_findings=[conflict])).check("scope_conditions").state == "fail"


def test_pricing_coverage_states_and_audience():
    weak = [_priced_line("B1", quality="weak"), _priced_line("B2"), _priced_line("B3")]
    st = _status(_sources(pricing={"version": 1, "boq_version": 1, "status": "priced", "lines": weak}))
    assert st.check("pricing_coverage").state == "warn" and st.check("pricing_coverage").params["share"] == pytest.approx(0.6667, abs=1e-3)
    low = [_priced_line("B1", quality="weak"), _priced_line("B2", quality="weak"), _priced_line("B3")]
    assert _status(_sources(pricing={"version": 1, "boq_version": 1, "status": "priced", "lines": low})).check(
        "pricing_coverage").state == "fail"
    mismatch = [_priced_line("B1", flags=("uom_conflict",)), _priced_line("B2")]
    assert "pricing:uom_mismatch" in _status(_sources(pricing={"version": 1, "boq_version": 1, "status": "priced",
                                                               "lines": mismatch})).check("pricing_coverage").reasons
    stale = _status(_sources(pricing={"version": 1, "boq_version": 0, "status": "priced", "lines": weak}))
    assert stale.check("pricing_coverage").reasons[0] == "pricing:stale"
    blocked = _status(_sources(pricing={"version": 1, "boq_version": 1, "status": "blocked", "lines": weak}))
    assert "pricing:blocked" in blocked.check("pricing_coverage").reasons and blocked.overall == "blocked"
    provisional = _status(_sources(pricing={"version": 1, "boq_version": 1, "status": "provisional",
                                            "lines": [_priced_line("B1")]}))
    assert provisional.check("pricing_coverage").state == "warn"
    # Outside the pricing roles: the state stays, the numbers and the note go.
    so = {"decision": "verified", "pricing_version": 1, "note": "checked against quotes", "created_by": "u1",
          "created_at": "2026-10-21T22:04:00+00:00"}
    hidden = _status(_sources(signoff=so), include_pricing=False)
    assert hidden.check("pricing_coverage").state == "pass" and hidden.check("pricing_coverage").params == {}
    d = hidden.to_dict()
    assert d["pricing_version"] is None and "note" not in d["signoff"] and "pricing_version" not in d["signoff"]
    assert d["signoff"]["superseded"] is True and d["signoff"]["created_at"] == "2026-10-21T15:04:00-07:00"
    shown = _status(_sources(signoff=so)).to_dict()
    assert shown["signoff"]["note"] == "checked against quotes" and shown["pricing_version"] == 2
    assert E.money_keys(d) == []
    assert _status(_sources(), pricing_enabled=False).check("pricing_coverage").state == "na"


def test_review_and_band_states():
    def review(verdict: str, sev: str = "none") -> dict:
        return {"verdict": verdict, "findings": [], "bands": [{"metric": "bid_per_device", "severity": sev}]}

    assert _status(_sources(review=review("advisory"))).check("independent_review").state == "warn"
    assert _status(_sources(review=review("needs_human"))).check("independent_review").state == "fail"
    assert _status(_sources(review=review("incomplete"))).check("independent_review").state == "fail"
    assert _status(_sources(review=review("stale"))).check("independent_review").state == "warn"
    assert _status(_sources(review=review("clear", "warning"))).check("history_bands").state == "warn"
    crit = _status(_sources(review=review("clear", "critical")))
    assert crit.check("history_bands").state == "fail" and crit.check("history_bands").reasons == ("band:bid_per_device",)


def test_evidence_thresholds():
    def ev(pc: float, density: float) -> dict:
        return {"stats": {"provenance_completeness": pc, "density": density, "ft_lines": 0, "ft_lines_with_polyline": 0}}

    assert _status(_sources(evidence=ev(0.95, 0.92))).check("evidence").state == "pass"
    assert _status(_sources(evidence=ev(0.8, 0.95))).check("evidence").state == "warn"
    assert _status(_sources(evidence=ev(0.95, 0.7))).check("evidence").state == "fail"


# Persistence and the Supabase provider ----------------------------------------------


class _Q:
    def __init__(self, sb: "_Sb", table: str) -> None:
        self.sb, self.table, self.op, self.payload, self.filters = sb, table, "select", None, []

    def select(self, _c: str) -> "_Q":
        return self

    def update(self, payload: dict) -> "_Q":
        self.op, self.payload = "update", payload
        return self

    def eq(self, k: str, v: Any) -> "_Q":
        self.filters.append((k, v))
        return self

    def execute(self) -> Any:
        self.sb.calls.append((self.table, self.op, self.payload, list(self.filters)))
        if self.table in self.sb.missing:
            raise RuntimeError(f'relation "{self.table}" does not exist')
        rows = [r for r in self.sb.rows.get(self.table, []) if all(r.get(k) == v for k, v in self.filters)]
        return type("R", (), {"data": rows})()


class _Sb:
    def __init__(self, rows: dict[str, list[dict]] | None = None, missing: set[str] | None = None) -> None:
        self.rows = rows or {}
        self.missing = missing or set()
        self.calls: list[tuple] = []

    def table(self, name: str) -> _Q:
        return _Q(self, name)


def test_recompute_persists_the_overall_state_and_tolerates_a_missing_column():
    sb = _Sb()
    assert AU.recompute(sb, "r1", sources=_sources(), pricing_enabled=True) == "ready_for_verification"
    assert sb.calls[-1] == ("plans_reader_runs", "update", {"autonomy_state": "ready_for_verification"}, [("id", "r1")])
    broken = _Sb(missing={"plans_reader_runs"})
    assert AU.recompute(broken, "r1", sources=_sources(), pricing_enabled=True) == "ready_for_verification"
    assert AU.recompute(None, "r1", sources=_sources(review=None), pricing_enabled=True) == "needs_review"


def test_supabase_sources_degrade_to_skipped_before_the_migrations():
    run = {"id": "r1", "status": "done"}
    sb = _Sb(rows={"plans_reader_runs": [run],
                   "plans_reader_groups": [{"id": "g1", "run_id": "r1", "kind": "sub", "status": "done",
                                            "category": "electrical_drawings"}]},
             missing={"plans_reader_gate_findings", "plans_reader_pricing", "plans_reader_reviews",
                      "plans_reader_price_signoffs", "plans_reader_outputs", "plans_reader_deliverables"})
    st = AU.status(sb, "r1", include_pricing=True, pricing_enabled=True)
    assert st.overall == "not_evaluated"
    assert st.check("reading_complete").state == "pass"
    assert st.check("pricing_coverage").state == "skipped"
    f = GateFinding(run_id="r1", output_version=1, stage="final", gate_id="linear_in_ea", severity="block",
                    target={"line_id": "B3"})
    sb2 = _Sb(rows={"plans_reader_runs": [run], "plans_reader_gate_findings": [f.to_row()],
                    "plans_reader_outputs": [{"run_id": "r1", "scope": "evidence", "version": 1,
                                              "output": {"stats": {"provenance_completeness": 0.5, "density": 0.5}}}]})
    st2 = AU.status(sb2, run, include_pricing=False, pricing_enabled=False)
    assert st2.overall == "blocked" and st2.check("integrity_gates").state == "fail"
    assert st2.check("evidence").state == "fail"


# Sign-off rules ------------------------------------------------------------------------


def test_signoff_rules_and_row():
    ready = _status(_sources())
    assert AU.validate_signoff(ready, "verified", None, pricing_version=2) == []
    assert AU.validate_signoff(ready, "verified", None, pricing_version=1) == ["only the latest priced version can be signed"]
    assert AU.validate_signoff(ready, "rejected", "short", pricing_version=2)
    assert AU.validate_signoff(ready, "approved", None, pricing_version=2)
    review = _status(_sources(review={"verdict": "advisory", "findings": [], "bands": []}))
    errs = AU.validate_signoff(review, "verified", None, pricing_version=2)
    assert errs and "independent_review" in errs[0]
    ok = AU.validate_signoff(review, "verified", None, pricing_version=2,
                             accepted={"independent_review": "Advisory finding reviewed against the plans."})
    assert ok == []
    row = AU.signoff_row("r1", 2, "verified", None, review, {"hours": "625"}, created_by=None,
                         accepted={"independent_review": "Advisory finding reviewed against the plans."})
    assert set(row) == {"run_id", "pricing_version", "decision", "autonomy_snapshot", "totals_snapshot"}
    assert row["autonomy_snapshot"]["overall"] == "needs_review"
    assert row["autonomy_snapshot"]["accepted"]["independent_review"].startswith("Advisory")
    full = AU.signoff_row("r1", 2, "rejected", "Quotes are missing for lighting.", review, {}, created_by="u1")
    assert full["note"] and full["created_by"] == "u1"


def test_module_has_no_dashes():
    for path in (Path(AU.__file__), Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert chr(0x2013) not in text and chr(0x2014) not in text
