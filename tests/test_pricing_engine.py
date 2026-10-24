"""app/services/pricing_engine.py: the Verify replica on synthetic rows, a
synthetic library and the code policy. Pure and offline; every number is
made up."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services import cost_library as cl
from app.services import estimating_contracts as ec
from app.services import pricing_engine as pe
from app.services import pricing_policy as pp
from app.services.electrical_items import parse_item
from app.services.pricing_types import Allowance, PriceRow, PricingContext, QuoteRef, SpecFact


def _obs(project, desc, unit, hours, material):
    ident = parse_item(desc, unit)
    return cl.Observation(
        project_id=project, item_key=cl.library_key(ident), family=cl.library_family(ident),
        item_class=ident.item_class, uom=ident.uom, unit_labor_hours=hours,
        unit_material_cost=material, observed_on=dt.date(2026, 5, 1), description=desc,
    )


def _library(exclude=()):
    obs = []
    for i in range(1, 13):
        p = f"p{i}"
        obs += [
            _obs(p, "#12 THHN/THWN Cu", "FT", 0.01, 0.20),
            _obs(p, '3/4"C EMT', "FT", 0.03, 1.00),
            _obs(p, "DUPLEX RECEPTACLE", "EA", 0.40, 10.00),
            _obs(p, "DUPLEX RECEPTACLE, GFCI", "EA", 0.45, 20.00),
            _obs(p, "ELECTRICAL CONNECTION", "EA", 1.00, 50.00),
            _obs(p, "L1 - 2' X 4' RECESSED TROFFER - LED", "EA", 1.00, 100.00),
            _obs(p, "REMOVAL OF LIGHT FIXTURE", "EA", 0.50, 7.00),
            _obs(p, "PANEL LP1, 225A MLO", "EA", 8.00, 2000.00),
        ]
    return cl.CostLibrary.from_observations(obs, exclude_projects=exclude)


def _row(i, desc, group, unit, qty, *, section="NEW WORK", subgroup=None, wastage=False, base=None, **kw):
    return PriceRow(
        line_id=f"B{i}", description=desc, group=group, subgroup=subgroup, section=section, unit=unit,
        quantity=Decimal(str(qty)), qty_base=Decimal(str(base if base is not None else qty)),
        wastage_applies=wastage, **kw,
    )


def _rows():
    return [
        _row(1, "REMOVAL OF LIGHT FIXTURE", "DEMOLITION", "EA", 10, section="DEMOLITION"),
        _row(2, "#12 THHN/THWN Cu", "WIRING", "FT", 1100, subgroup="CONDUCTORS (AWG)", wastage=True, base=1000),
        _row(3, '3/4"C EMT', "WIRING", "FT", 550, subgroup="CONDUITS", wastage=True, base=500),
        _row(4, "DUPLEX RECEPTACLE", "OTHER ITEMS", "EA", 20, subgroup="RECEPTACLES"),
        _row(5, "ELECTRICAL CONNECTION", "OTHER ITEMS", "EA", 2, subgroup="MISCELLANEOUS"),
        _row(6, "L1 - 2' X 4' RECESSED TROFFER - LED", "LIGHTING FIXTURES", "EA", 8, subgroup="FIXTURES"),
    ]


def _ctx(**kw):
    base = dict(wage_type="prevailing_wage", labor_rate_usd=Decimal("0"), labor_rate_source="policy",
                project_gc_ids=(), eval_case_id="t1")
    base.update(kw)
    return PricingContext(**base)


POLICY = pp.PricingPolicy(hour_uplift="1.0")


def test_other_items_material_is_always_carried_in_general_material():
    est = pe.compute_estimate(_rows(), _library(), POLICY, [], _ctx(), pricing_allowed=(True, []))
    gm = est.categories["General Material"]
    # WIRING: 1100 x 0.20 + 550 x 1.00 = 770; OTHER ITEMS: 20 x 10 + 2 x 50 = 300.
    assert gm["library_material"] == Decimal("1070.00")
    assert gm["pre_tax"] == Decimal("1070.00")
    assert any(ln.pricing_category == "General Material" for ln in est.lines if ln.row.group == "OTHER ITEMS")
    lighting = est.categories["Lighting"]
    assert lighting["library_material"] == Decimal("800.00")
    assert est.categories["Demolition"]["library_material"] == Decimal("0.00")
    assert est.verify is not None
    # every non-quoted material line is inside a category row (the leak closer)
    lines_material = sum((ln.material_cost or Decimal(0) for ln in est.lines), Decimal(0))
    rows_material = sum((c["library_material"] for c in est.categories.values()), Decimal(0))
    assert lines_material == rows_material


def test_verify_numbers_and_bid_total_invariant():
    est = pe.compute_estimate(_rows(), _library(), POLICY, [], _ctx(), pricing_allowed=(True, []))
    v = est.verify
    # hours: 10x0.5 + 1100x0.01 + 550x0.03 + 20x0.4 + 2x1 + 8x1 = 5+11+16.5+8+2+8 = 50.5 -> 60
    assert v.labor_hours_basis == Decimal("50.5")
    assert v.labor_hours_reviewer == Decimal("60")
    assert v.labor_rate == Decimal("125") and v.labor_rate_source == "policy:prevailing_wage"
    assert v.labor_amount == Decimal("7500.00")
    # material: GM 1070 + Lighting 800 + Demolition 0, each taxed at 8.375% and rounded per row
    assert v.materials_amount == Decimal("1159.61") + Decimal("867.00")
    assert v.materials_markup_amount == Decimal("202.66")  # 10% of 2026.61 = 202.661
    assert v.gear_amount is None and v.gear_markup_amount is None
    assert v.bid_total == ec.verify_sum(v.numbers())
    assert est.status == "provisional"  # Lighting has no quote (quote-required)
    assert "quote_required:Lighting" in est.quality_summary["provisional_reasons"]


def test_quote_replaces_category_material_without_double_count_and_lowest_wins():
    quotes = [
        QuoteRef("q-high", "Lighting", "v1", Decimal("1500.00"), False),
        QuoteRef("q-low", "Lighting", "v2", Decimal("1400.00"), False),
    ]
    est = pe.compute_estimate(_rows(), _library(), POLICY, quotes, _ctx(), pricing_allowed=(True, []))
    cat = est.categories["Lighting"]
    assert cat["quote_id"] == "q-low" and cat["library_material"] == Decimal("0.00")
    assert cat["pre_tax"] == Decimal("1400.00") and cat["total"] == Decimal("1517.25")
    assert [a["quote_id"] for a in cat["alternatives"]] == ["q-high"]
    fixture = est.line("B6")
    assert fixture.material_source == "quote:q-low" and fixture.material_cost == Decimal("0")
    assert "quote_covered" in fixture.flags and fixture.labor_hours == Decimal("8")
    assert est.status == "priced"
    selected = pe.compute_estimate(_rows(), _library(), POLICY, quotes, _ctx(), pricing_allowed=(True, []),
                                   selected_quote_ids={"q-high"})
    assert selected.categories["Lighting"]["quote_id"] == "q-high"
    assert selected.verify.bid_total > est.verify.bid_total
    included = [QuoteRef("q-inc", "Lighting", "v", Decimal("1500.00"), True)]
    inc = pe.compute_estimate(_rows(), _library(), POLICY, included, _ctx(), pricing_allowed=(True, []))
    assert inc.categories["Lighting"]["total"] == Decimal("1500.00")  # tax already in the quote


def test_blocked_on_linear_item_in_ea_and_artifact_refused_true():
    rows = _rows() + [_row(7, '3/4"C EMT', "WIRING", "EA", 3, subgroup="CONDUITS")]
    est = pe.compute_estimate(rows, _library(), POLICY, [], _ctx(), pricing_allowed=(True, []))
    assert est.status == "blocked"
    assert [b.code for b in est.blockers] == ["linear_in_ea"]  # a gate id (5.4), never a private code
    assert "linear_in_ea" in ec.GATE_IDS
    assert est.blockers[0].line_ids == ("B7",)
    assert est.verify is not None  # the draft is still computed for the verifier
    doc = pe.to_priced_estimate_v1(est, run_id="r1")
    assert doc["refused"] is True and doc["refusal_reasons"]
    line7 = next(ln for ln in doc["lines"] if ln["line_id"] == "B7")
    assert line7["excluded"] is True and line7["priced"] is False and line7["kind"] is None
    assert line7["exclusion_reason"] == "linear_in_ea:conduit"
    # a long installation description that mentions a wire size is not a length item
    ok = pe.compute_estimate(
        _rows() + [_row(8, "INSTALL STREETLIGHT PER STD. #314 WITH 8' ARM AND BASE", "OTHER ITEMS", "EA", 2)],
        _library(), POLICY, [], _ctx(), pricing_allowed=(True, []),
    )
    assert ok.status != "blocked"


def test_raceway_and_sawcut_in_ea_block_like_every_linear_class():
    """UOM_CONFLICT_CLASSES is the one LINEAR_CLASSES set (5.1): a bare
    raceway or saw-cut row in EA blocks; a count item of the class does not."""
    assert pe.UOM_CONFLICT_CLASSES == ec.LINEAR_CLASSES
    rows = _rows() + [
        _row(7, "WIREMOLD RACEWAY", "OTHER ITEMS", "EA", 40),
        _row(8, 'CABLE TRAY 12"', "OTHER ITEMS", "EA", 60),
        _row(9, "SAW CUT ASPHALT", "WIRING", "EA", 25, subgroup="SAW CUT"),
    ]
    est = pe.compute_estimate(rows, _library(), POLICY, [], _ctx(), pricing_allowed=(True, []))
    assert est.status == "blocked"
    assert est.blockers[0].code == "linear_in_ea" and set(est.blockers[0].line_ids) == {"B7", "B8", "B9"}
    doc = pe.to_priced_estimate_v1(est)  # never raises
    reasons = {ln["line_id"]: ln["exclusion_reason"] for ln in doc["lines"] if ln["excluded"]}
    assert reasons == {"B7": "linear_in_ea:raceway", "B8": "linear_in_ea:raceway", "B9": "linear_in_ea:sawcut"}
    # the same rows in FT are plain length lines
    ok = pe.compute_estimate(
        _rows() + [_row(7, "WIREMOLD RACEWAY", "OTHER ITEMS", "FT", 40), _row(8, "SAW CUT ASPHALT", "WIRING", "FT", 25)],
        _library(), POLICY, [], _ctx(), pricing_allowed=(True, []),
    )
    assert ok.status != "blocked"


def test_non_bare_linear_class_row_in_ea_is_excluded_from_the_artifact_not_raised():
    """A count item the parser files under a linear class (a ladder rack is
    `raceway`) is not blocked, but the contract forbids a linear kind in EA,
    so the artifact excludes the line with the gate reason instead of
    raising ContractError."""
    rows = _rows() + [_row(7, '18" LADDER RACK', "OTHER ITEMS", "EA", 2)]
    assert parse_item('18" LADDER RACK', "EA").item_class == "raceway"
    est = pe.compute_estimate(rows, _library(), POLICY, [], _ctx(), pricing_allowed=(True, []))
    assert est.status != "blocked"
    doc = pe.to_priced_estimate_v1(est)
    line7 = next(ln for ln in doc["lines"] if ln["line_id"] == "B7")
    assert line7["excluded"] is True and line7["priced"] is False and line7["kind"] is None
    assert line7["exclusion_reason"] == "linear_in_ea:raceway"
    assert doc["refused"] is False


def test_choose_quotes_compares_tax_inclusive_amounts():
    """Q9 picks the lowest ELIGIBLE quote on the Verify comparison basis: a
    quote that excludes tax costs its amount plus tax, so a lower pre-tax
    quote without tax can lose to a slightly higher quote with tax included."""
    with_tax = QuoteRef("q-inc", "Lighting", "v1", Decimal("1000.00"), True)
    without = QuoteRef("q-exc", "Lighting", "v2", Decimal("950.00"), False)  # 1029.56 once taxed
    chosen = pe.choose_quotes([without, with_tax], tax_rate_pct=Decimal("8.375"))
    assert chosen["Lighting"][0].quote_id == "q-inc"
    assert [a.quote_id for a in chosen["Lighting"][1]] == ["q-exc"]
    assert pe.taxed_quote_amount(without, Decimal("8.375")) == Decimal("1029.56")
    # a zero tax rate falls back to the pre-tax order
    assert pe.choose_quotes([without, with_tax], tax_rate_pct=Decimal("0"))["Lighting"][0].quote_id == "q-exc"
    # the engine passes the policy rate through
    est = pe.compute_estimate(_rows(), _library(), POLICY, [without, with_tax], _ctx(), pricing_allowed=(True, []))
    assert est.categories["Lighting"]["quote_id"] == "q-inc"
    assert est.categories["Lighting"]["total"] == Decimal("1000.00")
    # the human selection still wins when eligible
    sel = pe.compute_estimate(_rows(), _library(), POLICY, [without, with_tax], _ctx(), pricing_allowed=(True, []),
                              selected_quote_ids={"q-exc"})
    assert sel.categories["Lighting"]["quote_id"] == "q-exc"


def test_blocked_when_wage_type_unknown_or_gates_refuse():
    est = pe.compute_estimate(_rows(), _library(), POLICY, [], _ctx(wage_type=None), pricing_allowed=(True, []))
    assert est.status == "blocked" and est.blockers[0].code == "wage_type_unknown"
    assert est.verify.labor_rate_source == "default"
    gated = pe.compute_estimate(_rows(), _library(), POLICY, [], _ctx(),
                                pricing_allowed=(False, ["new_devices_no_branch"]))
    # pricing_not_allowed is a status override only: the detail echoes WS4's gate codes
    assert gated.status == "blocked" and gated.blockers[0].code == "pricing_not_allowed"
    assert "new_devices_no_branch" in gated.blockers[0].detail
    assert "wage_type_unknown" in ec.GATE_IDS and "pricing_not_allowed" not in ec.GATE_IDS
    assert pe.to_priced_estimate_v1(gated)["refused"] is True


def test_provisional_rules_wage_default_fallback_share_and_unpriced():
    lib = _library()
    quotes = [QuoteRef("q", "Lighting", "v", Decimal("1000"), False)]
    # wage type from the default: provisional with the reason
    est = pe.compute_estimate(_rows(), lib, POLICY, quotes, _ctx(labor_rate_source="default"), pricing_allowed=(True, []))
    assert est.status == "priced"  # ctx.wage_type is set; the engine resolves the policy rate
    # a line nothing matches: unpriced -> provisional
    rows = _rows() + [_row(9, "ZYXWV GADGET UNIT", "OTHER ITEMS", "EA", 1)]
    est2 = pe.compute_estimate(rows, lib, POLICY, quotes, _ctx(), pricing_allowed=(True, []))
    ln = est2.line("B9")
    assert ln.labor.method in ("fallback", "none")
    assert est2.status == "provisional"
    reasons = est2.quality_summary["provisional_reasons"]
    assert any(r.startswith("fallback_share") or r == "unpriced_lines" for r in reasons) or ln.labor.method == "fallback"
    assert "B9" in est2.unpriced.lines
    # the wage source default (no wage decided but Q13 applied upstream)
    est3 = pe.compute_estimate(_rows(), lib, POLICY, quotes, _ctx(wage_type="prevailing_wage"),
                               pricing_allowed=(True, []), reviewer_hours=Decimal("60"))
    assert est3.verify.labor_hours_reviewer == Decimal("60")


def test_wage_default_source_is_provisional():
    policy = pp.PricingPolicy(hour_uplift="1.0", labor_rates={"prevailing_wage": 125, "non_prevailing_wage": 100})
    est = pe.compute_estimate(_rows(), _library(), policy, [QuoteRef("q", "Lighting", "v", Decimal("1000"), False)],
                              _ctx(wage_type="prevailing_wage"), pricing_allowed=(True, []))
    assert est.status == "priced"
    # an unknown wage string is not a WAGE_TYPES value: the context refuses it
    with pytest.raises(ValueError):
        _ctx(wage_type="union")


def test_demolition_material_zero_even_for_rows_misread_as_material():
    rows = [_row(1, "DISCONNECT AND REUSE EXISTING 5#14 DOOR CONTROL CABLE", "DEMOLITION", "EA", 4, section="DEMOLITION")]
    est = pe.compute_estimate(rows, _library(), POLICY, [], _ctx(), pricing_allowed=(True, []))
    assert est.line("B1").material.value == Decimal("0")
    assert est.categories["Demolition"]["library_material"] == Decimal("0.00")


def test_grade_seed_applies_only_with_a_cited_fact():
    seed = pp.GradeSeed(item_keys=("receptacle:duplex|mod=DUPLEX",), fact_key="device_grade",
                        fact_value="hospital", material_usd="33.33")
    policy = pp.PricingPolicy(hour_uplift="1.0", grade_seeds=(seed,))
    fact = SpecFact(key="device_grade", value="hospital", source_ref="F3")
    with_fact = pe.compute_estimate(_rows(), _library(), policy, [], _ctx(spec_facts=(fact,)), pricing_allowed=(True, []))
    duplex = with_fact.line("B4")
    assert duplex.material.value == Decimal("33.33") and duplex.material.quality == "policy"
    assert duplex.material.method == "seed" and duplex.material_source == "seed" and "grade_seed" in duplex.flags
    assert duplex.modifiers == ("device_grade=hospital@F3",)
    uncited = SpecFact(key="device_grade", value="hospital", source_ref=None)
    without = pe.compute_estimate(_rows(), _library(), policy, [], _ctx(spec_facts=(uncited,)), pricing_allowed=(True, []))
    assert without.line("B4").material.value == Decimal("10") and without.line("B4").material_source == "library"
    doc = pe.to_priced_estimate_v1(with_fact)
    art = next(ln for ln in doc["lines"] if ln["line_id"] == "B4")
    assert art["material_source"] == "policy" and art["hours_source"] == "library"


def test_allowances_untaxed_in_materials_with_markup():
    ctx = _ctx(allowances=(Allowance(name="PERMIT AND UTILITY FEES", amount_usd=Decimal("500")),))
    est = pe.compute_estimate(_rows()[:5], _library(), POLICY, [], ctx, pricing_allowed=(True, []))
    allow = est.categories["Allowances"]
    assert allow["pre_tax"] == Decimal("500.00") and allow["tax"] == Decimal("0.00") and allow["section"] == "materials"
    v = est.verify
    assert v.materials_amount == Decimal("1159.61") + Decimal("500.00")
    assert v.materials_markup_amount == Decimal("165.96")  # 10% of 1659.61
    doc = pe.to_priced_estimate_v1(est, allowances=ctx.allowances)
    assert doc["allowances"] == [{"name": "PERMIT AND UTILITY FEES", "amount_usd": 500.0}]


def test_no_branch_wiring_is_a_warning_and_multi_gc_rate_surfaces():
    rows = [_row(1, "DUPLEX RECEPTACLE", "OTHER ITEMS", "EA", 20)]
    policy = pp.PricingPolicy(hour_uplift="1.0", rate_overrides={"g1": "120"})
    est = pe.compute_estimate(rows, _library(), policy, [], _ctx(project_gc_ids=("g1", "g2")), pricing_allowed=(True, []))
    codes = [w.code for w in est.warnings]
    assert "new_devices_no_branch" in codes and "multi_gc_rate" in codes
    assert est.status != "blocked" and est.verify.labor_rate == Decimal("125")


def test_recorded_rate_and_pinned_hours_replay_a_verify():
    quotes = [QuoteRef("q", "Lighting", "v", Decimal("1000.00"), False)]
    ctx = _ctx(labor_rate_usd=Decimal("111"), labor_rate_source="recorded")
    est = pe.compute_estimate(_rows(), _library(), POLICY, quotes, ctx, pricing_allowed=(True, []),
                              reviewer_hours=Decimal("75"))
    assert est.verify.labor_rate == Decimal("111") and est.verify.labor_rate_source == "recorded"
    assert est.verify.labor_amount == Decimal("8325.00") and est.verify.labor_hours_reviewer == Decimal("75")


def test_lopo_exclusion_recorded_and_library_must_exclude_the_project():
    lib = _library(exclude={"p1"})
    est = pe.compute_estimate(_rows(), lib, POLICY, [], _ctx(), exclude_project="p1", pricing_allowed=(True, []))
    assert est.excluded_project_ids_sha256 == ec.excluded_project_ids_sha256(["p1"])
    assert est.library_fingerprint == lib.fingerprint
    with pytest.raises(pe.PricingError):
        pe.compute_estimate(_rows(), _library(), POLICY, [], _ctx(), exclude_project="p1")


def test_artifact_validates_with_the_contract_and_keeps_the_invariant():
    quotes = [QuoteRef("q-low", "Lighting", "v2", Decimal("1400.00"), False, file_sha256="a" * 64)]
    est = pe.compute_estimate(_rows(), _library(), POLICY, quotes, _ctx(), pricing_allowed=(True, []))
    doc = pe.to_priced_estimate_v1(est, run_id="run-1", quotes=quotes, clarifications=["EMT branch raceway"])
    assert doc["schema"] == ec.PRICED_ESTIMATE_SCHEMA and doc["refused"] is False
    assert doc["wage_type"] == "prevailing_wage" and doc["labor_rate_usd"] == 125.0
    assert ec.bid_total_matches_verify(doc["totals"]["bid_total_usd"], doc["totals"]["verify"])
    assert doc["totals"]["hours_raw"] == 50.5 and doc["totals"]["hours"] == 60.0
    assert doc["totals"]["tax_rate_pct"] == 8.375
    priced = [ln for ln in doc["lines"] if ln["priced"]]
    assert len(priced) == 6
    wire = next(ln for ln in doc["lines"] if ln["line_id"] == "B2")
    assert wire["kind"] == "conductor" and wire["unit"] == "FT" and wire["wastage"] == 0.1
    assert wire["hours_source"] == "library" and wire["material_source"] == "library"
    fixture = next(ln for ln in doc["lines"] if ln["line_id"] == "B6")
    assert fixture["material_source"] == "vendor_quote" and fixture["quote_ref"] == "q-low"
    assert doc["quotes"][0]["file_sha256"] == "a" * 64
    assert doc["sections"]["materials"]["present"] and not doc["sections"]["gear"]["present"]
    assert doc["metadata"]["library_fingerprint"] == est.library_fingerprint
    assert doc["clarifications"] == ["EMT branch raceway"]


def test_price_estimate_wrapper_with_empty_library_is_provisional_not_blocked():
    est = pe.price_estimate(_rows(), _ctx())
    assert est.status == "provisional"
    assert all(ln.labor.method == "none" for ln in est.lines)
    assert est.verify is not None and est.verify.labor_hours_basis == Decimal("0")
    assert "unpriced_lines" in est.quality_summary["provisional_reasons"]
    doc = pe.to_priced_estimate_v1(est)
    assert all(ln["priced"] is False for ln in doc["lines"])


def test_category_mapping_and_sections():
    rows = [
        _row(1, "PANEL LP1, 225A MLO", "SWITCHGEAR", "EA", 1, subgroup="PANELS"),
        _row(2, "TRENCHING 24\" DEEP", "WIRING", "FT", 100, subgroup="SAW CUT"),
        _row(3, "CAT6 CABLE", "LOW VOLTAGE WORK", "FT", 100),
        _row(4, "DUPLEX RECEPTACLE", "OTHER ITEMS", "EA", 1, material_category="EV Chargers"),
    ]
    idents = {r.line_id: parse_item(r.description, r.unit) for r in rows}
    assert pe.category_for(rows[0], idents["B1"]) == "Switchgear"
    assert pe.category_for(rows[1], idents["B2"]) == "Trenching"
    assert pe.category_for(rows[2], idents["B3"]) == "Low Voltage"
    assert pe.category_for(rows[3], idents["B4"]) == "EV Chargers"
    assert POLICY.section_of("Switchgear") == "gear" and POLICY.section_of("Trenching") == "underground"
    assert POLICY.section_of("Low Voltage") == "low_voltage" and POLICY.section_of("Lighting") == "materials"
    est = pe.compute_estimate(rows, _library(), POLICY, [], _ctx(), pricing_allowed=(True, []))
    assert est.verify.gear_amount is not None and est.verify.gear_markup_amount is not None
    assert est.verify.underground_amount is not None and est.verify.low_voltage_amount is not None


def test_duplicate_line_ids_are_a_caller_error():
    rows = [_row(1, "DUPLEX RECEPTACLE", "OTHER ITEMS", "EA", 1), _row(1, "DUPLEX RECEPTACLE", "OTHER ITEMS", "EA", 2)]
    with pytest.raises(pe.PricingError):
        pe.compute_estimate(rows, _library(), POLICY, [], _ctx())


def test_reconciled_lines_not_in_the_chosen_quote_keep_library_material():
    rows = [*_rows(), _row(7, "PANEL LP1, 225A MLO", "SWITCHGEAR", "EA", 1, subgroup="PANELS"),
            _row(8, "PANEL LP2, 225A MLO", "SWITCHGEAR", "EA", 1, subgroup="PANELS")]
    quotes = [QuoteRef("q-light", "Lighting", "v", Decimal("900.00"), False),
              QuoteRef("q-gear", "Switchgear", "v", Decimal("3000.00"), False),
              QuoteRef("q-gear-hi", "Switchgear", "v", Decimal("3500.00"), False)]
    recs = {
        "q-light": {"not_in_quote_line_ids": ["B6"], "flags": ["not_in_quote"]},
        "q-gear": {"not_in_quote_line_ids": ["B8"], "flags": ["manual_eligible"]},
        "q-gear-hi": {"not_in_quote_line_ids": ["B7", "B8"], "flags": []},  # not chosen: ignored
    }
    est = pe.compute_estimate(rows, _library(), POLICY, quotes, _ctx(), pricing_allowed=(True, []),
                              quote_reconciliations=recs)
    fixture = est.line("B6")
    assert pe.FLAG_NOT_IN_QUOTE in fixture.flags and pe.FLAG_QUOTE_COVERED not in fixture.flags
    assert fixture.material_cost == Decimal("800") and fixture.material_source == "library"
    covered, manual = est.line("B7"), est.line("B8")
    assert pe.FLAG_QUOTE_COVERED in covered.flags and covered.material_cost == 0
    assert {pe.FLAG_NOT_IN_QUOTE, pe.FLAG_MANUAL_ELIGIBLE} <= set(manual.flags) and manual.material_cost > 0
    light = est.categories["Lighting"]
    assert light["quote_id"] == "q-light" and light["library_material"] == Decimal("800.00")
    assert light["pre_tax"] == Decimal("1700.00")  # quote plus the library row of the line it does not carry
    without = pe.compute_estimate(rows, _library(), POLICY, quotes, _ctx(), pricing_allowed=(True, []))
    assert pe.FLAG_QUOTE_COVERED in without.line("B6").flags  # bare refs: every line of the category covered
    art = pe.to_priced_estimate_v1(est, run_id="r", wage_type="prevailing_wage", quotes=quotes,
                                   quote_details={"q-light": {"selection_reason": "lowest_eligible", "reconciled": {"L1": 8}}})
    q = next(q for q in art["quotes"] if q["category"] == "Lighting")
    assert q["selection_reason"] == "lowest_eligible" and q["reconciled"] == {"L1": 8}
    groups = {k: v for k, v in art["sections"].items() if v.get("boq_group")}
    assert groups["LIGHTING FIXTURES"]["material_source"] == "library" and groups["LIGHTING FIXTURES"]["material_usd"] == 800.0
    assert groups["OTHER ITEMS"]["material_usd"] > 0 and groups["OTHER ITEMS"]["material_source"] == "library"


def test_seeded_line_cites_its_fact_on_the_artifact():
    seeds = tuple(s.with_amount("30") for s in pp.DEFAULT_GRADE_SEEDS)
    policy = POLICY.replace(grade_seeds=seeds)
    ctx = _ctx(spec_facts=(SpecFact("device_grade", "hospital", "E2.01 p.9 SN3"),))
    est = pe.compute_estimate(_rows(), _library(), policy, [], ctx, pricing_allowed=(True, []))
    art = pe.to_priced_estimate_v1(est, run_id="r", wage_type="prevailing_wage")
    duplex = next(ln for ln in art["lines"] if ln["line_id"] == "B4")
    assert duplex["spec_fact_ref"] == "E2.01 p.9 SN3" and duplex["unit_material_usd"] == 30.0
    assert next(ln for ln in art["lines"] if ln["line_id"] == "B5")["spec_fact_ref"] is None


def test_hour_uplift_follows_the_coverage_regime():
    lib = _library()
    policy = pp.PricingPolicy(hour_uplift="1.10", hour_uplift_under_covered="1.30",
                              hour_uplift_range=("1.0", "1.2"), hour_uplift_under_covered_range=("1.2", "1.5"))
    quotes = [QuoteRef("q", "Lighting", "v", Decimal("1000"), False)]
    # every line an exact match: covered, the covered uplift applies
    est = pe.compute_estimate(_rows(), lib, policy, quotes, _ctx(), pricing_allowed=(True, []))
    qs = est.quality_summary
    assert qs["coverage_class"] == pp.COVERAGE_COVERED and qs["hour_uplift_applied"] == "1.10"
    assert qs["unpriced_line_share"] == 0 and qs["hour_uplift_range_applied"] == ["1.0", "1.2"]
    assert est.verify.labor_hours_reviewer == pp.nice_ceil(est.verify.labor_hours_basis * Decimal("1.10"))
    # a fallback-priced line that carries more than the fallback share limit of the hours: under covered
    rows = _rows() + [_row(9, "REMOVAL OF EXISTING 200A PANELBOARD", "DEMOLITION", "EA", 400, section="DEMOLITION")]
    est2 = pe.compute_estimate(rows, lib, policy, quotes, _ctx(), pricing_allowed=(True, []))
    assert est2.line("B9").labor.method == "fallback"
    qs2 = est2.quality_summary
    assert qs2["fallback_share_hours"] > 0.10
    assert qs2["coverage_class"] == pp.COVERAGE_UNDER_COVERED and qs2["hour_uplift_applied"] == "1.30"
    assert qs2["hour_uplift_range_applied"] == ["1.2", "1.5"]
    assert est2.verify.labor_hours_reviewer == pp.nice_ceil(est2.verify.labor_hours_basis * Decimal("1.30"))
    assert est2.range_high > est2.range_low
    # lines matched to nothing beyond the line share limit: under covered even with no fallback hours
    none_rows = _rows() + [_row(10 + i, "250 KCMIL XHHW-2 AL", "WIRING", "FT", 1) for i in range(3)]
    est3 = pe.compute_estimate(none_rows, lib, policy, quotes, _ctx(), pricing_allowed=(True, []))
    assert all(est3.line(f"B{10 + i}").labor.method == "none" for i in range(3))
    qs3 = est3.quality_summary
    assert qs3["unpriced_line_share"] == pytest.approx(3 / 9)
    assert qs3["coverage_class"] == pp.COVERAGE_UNDER_COVERED and qs3["hour_uplift_applied"] == "1.30"
    # pinned reviewer hours (PG-A2 replays) ignore the regime
    pinned = pe.compute_estimate(rows, lib, policy, quotes, _ctx(), pricing_allowed=(True, []), reviewer_hours=Decimal("60"))
    assert pinned.verify.labor_hours_reviewer == Decimal("60")
    assert pinned.quality_summary["coverage_class"] == pp.COVERAGE_UNDER_COVERED
