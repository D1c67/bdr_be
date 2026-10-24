"""app/services/plans_reader_score.py: scorer v2 (classify_line, aliases,
many-to-one assign, bands, dollar formulas, refusal counting). Synthetic
numbers only; no backup, no EVAL_ROOT, no network."""

from __future__ import annotations

import pytest

from app.services import plans_reader_score as S
from app.services.electrical_items import BoqRow


def _row(desc, qty, unit="EA", *, section="NEW WORK", group="OTHER ITEMS", subgroup="", area="A", ref=""):
    return BoqRow(area, section, group, subgroup, desc, float(qty), unit, False, ref or desc)


def _truth(desc, qty, unit="EA", *, section="NEW WORK", group="OTHER ITEMS", subgroup="", uic=None, line_id=None, area="A"):
    return S.TruthLine(
        line_id=line_id or desc[:12],
        area=area,
        section=section,
        group=group,
        subgroup=subgroup,
        description=desc,
        unit=unit,
        qty=float(qty),
        value=None if uic is None else float(qty) * uic,
        unit_item_cost=uic,
    )


def _item(desc, qty, unit="EA", *, wastage=False, caveat=None, accessory=False):
    return {
        "description": desc,
        "quantity": qty,
        "unit": unit,
        "sheet_refs": [],
        "caveat": caveat,
        "material_note": None,
        "material_category": "General Material",
        "wastage_applies": wastage,
        "is_accessory": accessory,
    }


def _spec(section, group, subgroup, items):
    return {
        "title": "T",
        "subtitle": None,
        "closing_notes": [],
        "areas": [
            {
                "area": "A",
                "notes": [],
                "sections": [
                    {"heading": section, "note": None, "groups": [{"name": group, "note": None, "subgroups": [{"name": subgroup, "note": None, "items": items}]}]}
                ],
            }
        ],
    }


# classify_line ------------------------------------------------------------------


def test_matcher_version_is_2():
    assert S.MATCHER_VERSION == 2


def test_classify_fixture_em_aliases():
    base = S.classify_line("L1, 2X4 RECESSED LED TROFFER", "NEW WORK", "LIGHTING FIXTURES")
    em = S.classify_line("L1, 2X4 RECESSED LED TROFFER, LIFE SAFETY", "NEW WORK", "LIGHTING FIXTURES")
    tagged = S.classify_line("L1-EM - 2' X 4' RECESSED TROFFER - RE - LED", "NEW WORK", "LIGHTING FIXTURES")
    assert base == S.LineKey("fixture", frozenset({"L1"}))
    assert em == S.LineKey("fixture", frozenset({"L1-EM"}))
    assert tagged == em
    # SHADED is a case alias, not a global one.
    shaded = "L3, 6-INCH RECESSED LED DOWNLIGHT, SHADED"
    assert S.classify_line(shaded, "NEW WORK", "LIGHTING FIXTURES") == S.LineKey("fixture", frozenset({"L3"}))
    case = S.merge_aliases({"SHADED": "EM"})
    assert S.classify_line(shaded, "NEW WORK", "LIGHTING FIXTURES", aliases=case) == S.LineKey("fixture", frozenset({"L3-EM"}))
    with pytest.raises(ValueError):
        S.merge_aliases({"SHADED": "BOGUS"})


def test_classify_receptacle_style_words_dropped():
    plain = S.classify_line("DUPLEX RECEPTACLE, 125V")
    assert plain == S.LineKey("receptacle", frozenset({"DUPLEX"}))
    assert S.classify_line("DUPLEX RECEPTACLE, EMERGENCY, DEDICATED (NEED TO CONFIRM DETAILS)") == plain
    assert S.classify_line("DUPLEX RECEPTACLE, 20A, RED") == plain
    gfci = S.classify_line("DUPLEX RECEPTACLE, 125V, GFCI")
    assert gfci.attrs == frozenset({"DUPLEX", "GFCI"})
    assert S.classify_line("DUPLEX RECEPTACLE, 125V, TR").attrs == frozenset({"DUPLEX", "TR"})
    assert S.classify_line("DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY").attrs == frozenset({"DUPLEX", "CTRL"})
    assert S.classify_line("QUADPLEX RECEPTACLE").attrs == frozenset({"QUAD"})
    assert S.classify_line("NEMA L6-30R RECEPTACLE, 30A").attrs >= {"SPECIAL", "L6-30R"}


def test_classify_other_kinds():
    assert S.classify_line("REMOVAL OF LIGHTING FIXTURE AND CONTROLS W/ ASSOCIATE WIRE & CONDUIT BACK TO SOURCE", "DEMOLITION", "DEMOLITION").kind == "demo_lighting"
    assert S.classify_line("REMOVAL OF POWER DEVICES W/ BACKBOX & ASSOCIATE WIRE, CONDUIT BACK TO SOURCE", "DEMOLITION", "DEMOLITION").kind == "demo_power"
    assert S.classify_line("REMOVAL OF LIGHTING TRACK", "DEMOLITION", "DEMOLITION").kind == "demo_track"
    assert S.classify_line("REMOVE TRACK-LIGHT HEAD ASSEMBLY", "DEMOLITION", "DEMOLITION").kind == "demo_lighting"
    assert S.classify_line("MAKE SAFE DAMPER ELECTRICAL CONNECTION", "DEMOLITION", "DEMOLITION").kind == "demo_other"
    assert S.classify_line("#12 THHN/THWN Cu", "NEW WORK", "WIRING", "FT", subgroup="CONDUCTORS (AWG)") == S.LineKey("conductor", frozenset({"#12", "CU", "THHN/THWN"}))
    assert S.classify_line('3/4"C EMT', "NEW WORK", "WIRING", "FT", subgroup="CONDUITS") == S.LineKey("conduit", frozenset({'3/4"', "EMT"}))
    assert S.classify_line('3/4"C EMT', "NEW WORK", "LOW VOLTAGE WORK", "FT").attrs == frozenset({'3/4"', "EMT", "SEC:LV"})
    assert S.classify_line("#18/4C CONTROL CABLE", "NEW WORK", "OTHER ITEMS", "FT").kind == "control_cable"
    assert S.classify_line("20A/1P GFCI CIRCUIT BREAKER", "NEW WORK", "SWITCHGEAR") == S.LineKey("breaker", frozenset({"20A/1P", "GFCI"}))
    assert S.classify_line("20A/1P CIRCUIT BREAKER (NEED TO CONFIRM SCOPE OF WORK)", "NEW WORK", "SWITCHGEAR").attrs == frozenset({"20A/1P"})
    assert S.classify_line("DIMMER SWITCH W/ LOCKABLE COVER", "NEW WORK", "LIGHTING CONTROLS") == S.LineKey("dimmer", frozenset({"LOCK"}))
    assert S.classify_line("DIMMER & OCCUPANCY SENSOR SWITCH", "NEW WORK", "LIGHTING CONTROLS") == S.LineKey("dimmer", frozenset({"OCC"}))
    assert S.classify_line("OCCUPANCY SENSOR", "NEW WORK", "LIGHTING CONTROLS") == S.LineKey("occ_sensor", frozenset({"CEILING"}))
    assert S.classify_line("WALL OCCUPANCY-SENSOR SWITCH", "NEW WORK", "LIGHTING CONTROLS") == S.LineKey("occ_sensor", frozenset({"WALL"}))
    assert S.classify_line("VAV POWER CONNECTION", "NEW WORK", "OTHER ITEMS").kind == "equipment_connection"
    assert S.classify_line("ELECTRICAL CONECTION", "NEW WORK", "OTHER ITEMS").kind == "equipment_connection"
    assert S.classify_line("RELOCATION OF THERMOSTAT TO NEW LOCATION").kind == "thermostat"
    assert S.classify_line("45 KVA TRANSFORMER", "NEW WORK", "SWITCHGEAR").attrs == frozenset({"45KVA"})
    assert S.classify_line("CAT6 CABLE PLENUM RATED", "NEW WORK", "LOW VOLTAGE WORK", "FT").kind == "cable"
    assert S.classify_line("DATA OUTLET", "NEW WORK", "LOW VOLTAGE WORK").kind == "low_voltage"
    assert S.classify_line('5/8"X10\' LONG COPPER GROUND ROD', "NEW WORK", "WIRING", subgroup="GROUNDING").kind == "grounding"
    assert S.classify_line("MOBILIZATION AND DEMOBILIZATION ALLOWANCE").kind == "allowance"
    assert S.classify_line("SOME UNKNOWN WIDGET THING").kind == "other"
    for key in (S.classify_line(d) for d in ("DUPLEX RECEPTACLE", "#12 THHN", "L1, TROFFER")):
        assert key.kind in S.KINDS


# assign --------------------------------------------------------------------------


def test_assign_many_to_one_sums_split_rows():
    ai = [
        _row("DUPLEX RECEPTACLE", 26),
        _row("DUPLEX RECEPTACLE, EMERGENCY", 28),
        _row("DUPLEX RECEPTACLE, EMERGENCY, DEDICATED", 3),
    ]
    truth = [_truth("DUPLEX RECEPTACLE, 125V", 54), _truth("DUPLEX RECEPTACLE, 125V, GFCI", 11)]
    a = S.assign(ai, truth)
    assert [a.pairs[i] for i in range(3)] == [0, 0, 0]
    score = S.score_case(ai, truth=truth)
    duplex = next(ln for ln in score.lines if "GFCI" not in ln.description)
    assert duplex.ai_qty == 57 and duplex.status == "within_10pct"
    assert score.count_lines_total == 2 and score.count_lines_exact == 0


def test_assign_prefers_dropping_low_priority_modifier():
    ai = [_row("DUPLEX RECEPTACLE, GFCI, OCCUPANCY CONTROLLED", 1)]
    truth = [_truth("DUPLEX RECEPTACLE, GFCI", 10), _truth("DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY", 4)]
    a = S.assign(ai, truth)
    assert a.pairs[0] == 0  # keeps GFCI, loses CTRL
    assert a.tiers[0] == 2
    # A less specific AI row never lands on a more specific truth line.
    a2 = S.assign([_row("DUPLEX RECEPTACLE", 5)], [_truth("DUPLEX RECEPTACLE, GFCI", 5)])
    assert a2.pairs[0] is None


def test_assign_never_crosses_kind():
    ai = [_row("REMOVE DUPLEX RECEPTACLE", 10, section="DEMOLITION", group="DEMOLITION")]
    truth = [_truth("DUPLEX RECEPTACLE, 125V, TR", 3), _truth("REMOVAL OF POWER DEVICES W/ BACKBOX", 35, section="DEMOLITION", group="DEMOLITION")]
    a = S.assign(ai, truth)
    assert a.pairs[0] == 1
    # identity conflicts (#12 vs #10) never pair
    a2 = S.assign([_row("#12 THHN/THWN Cu", 100, "FT", group="WIRING")], [_truth("#10 THHN/THWN Cu", 100, "FT", group="WIRING")])
    assert a2.pairs[0] is None
    a3 = S.assign([_row("L1, 2X4 LED TROFFER, LIFE SAFETY", 5, group="LIGHTING FIXTURES")], [_truth("L1 - 2' X 4' RECESSED TROFFER", 16, group="LIGHTING FIXTURES"), _truth("L1-EM - 2' X 4' RECESSED TROFFER", 5, group="LIGHTING FIXTURES")])
    assert a3.pairs[0] == 1


def test_unit_mismatch_counted_not_summed():
    ai = [_row("L5, UNDERCABINET LED LIGHTING RUN", 7, "EA", group="LIGHTING FIXTURES")]
    truth = [_truth("L5 - UNDERCABINET LED LIGHTING - UC - LED", 43.46, "FT", group="LIGHTING FIXTURES", uic=76.3)]
    score = S.score_case(ai, truth=truth)
    line = score.lines[0]
    assert line.status == "unit_mismatch" and line.ai_qty == 0 and line.unit_mismatch_rows == 1
    assert score.unit_mismatches == 1
    assert score.length_lines_total == 1 and score.length_lines_produced == 0
    assert score.length_bands["unit_mismatch"] == 1
    assert score.extras_count == 0  # assigned, so not an extra


def test_length_bands_with_wastage_applied_once():
    spec = _spec("NEW WORK", "WIRING", "CONDUCTORS (AWG)", [_item("#12 THHN/THWN Cu", 1000, "FT", wastage=True), _item("#10 THHN/THWN Cu", 1200, "FT", wastage=True), _item('3/4"C EMT', 700, "FT", wastage=True)])
    ai = S.rows_from_spec(spec)
    assert ai[0].quantity == pytest.approx(1100)
    truth = [
        _truth("#12 THHN/THWN Cu", 1100, "FT", group="WIRING", subgroup="CONDUCTORS (AWG)", uic=1.0),
        _truth("#10 THHN/THWN Cu", 1100, "FT", group="WIRING", subgroup="CONDUCTORS (AWG)", uic=1.0),
        _truth('3/4"C EMT', 1100, "FT", group="WIRING", subgroup="CONDUITS", uic=1.0),
    ]
    score = S.score_case(ai, truth=truth)
    by = {ln.description: ln for ln in score.lines}
    assert by["#12 THHN/THWN Cu"].status == "good" and by["#12 THHN/THWN Cu"].rel_error == pytest.approx(0.0)
    assert by["#10 THHN/THWN Cu"].status == "acceptable"  # 1320 vs 1100 = 20%
    assert by['3/4"C EMT'].status == "miss"  # 770 vs 1100 = 30%
    assert score.length_bands == {"good": 1, "acceptable": 1, "miss": 1, "none": 0, "unit_mismatch": 0}
    assert score.length_lines_produced == 3
    assert S.GOOD_BAND == 0.10 and S.ACCEPTABLE_BAND == 0.25


def test_dollar_metrics_formula():
    # v = qty x unit item cost; synthetic costs.
    truth = [
        _truth("DUPLEX RECEPTACLE, 125V", 50, uic=10.0),  # v 500
        _truth("DUPLEX RECEPTACLE, 125V, GFCI", 10, uic=20.0),  # v 200
        _truth("RELOCATION OF THERMOSTAT TO NEW LOCATION", 5, uic=60.0),  # v 300, unmatched
    ]
    ai = [_row("DUPLEX RECEPTACLE", 40), _row("DUPLEX RECEPTACLE, GFCI", 12), _row("20A/1P CIRCUIT BREAKER", 7, group="SWITCHGEAR")]
    score = S.score_case(ai, truth=truth, price_lookup=lambda r: 5.0)
    assert score.truth_cost == pytest.approx(1000.0)
    # covered = min(40,50)x10 + min(12,10)x20 + 0 = 600
    assert score.covered_cost == pytest.approx(600.0)
    # partial credit (the prototype formula E25 was measured with)
    assert score.dollar_coverage_partial == pytest.approx(0.6)
    # 3.6 in band: 40 vs 50 and 12 vs 10 are both misses, so nothing is in band
    assert score.dollar_coverage == pytest.approx(0.0)
    exact = S.score_case([_row("DUPLEX RECEPTACLE", 50), _row("DUPLEX RECEPTACLE, GFCI", 12)], truth=truth)
    assert exact.dollar_coverage == pytest.approx(0.5)  # only the 500 duplex line is in band
    assert exact.dollar_coverage_partial == pytest.approx(0.7)  # 500 + min(12, 10) x 20
    assert score.under_cost == pytest.approx(400.0) and score.over_cost == pytest.approx(40.0)
    # weighted error = (10/50 x 500 + 2/10 x 200 + 1 x 300) / 1000 = (100 + 40 + 300) / 1000
    assert score.dollar_weighted_error == pytest.approx(0.44)
    assert score.extras_count == 1 and score.extras_value == pytest.approx(35.0)
    assert score.extras[0]["kind"] == "breaker"


def test_exact_tolerance_relative():
    truth = [_truth("DUPLEX RECEPTACLE, 125V", 100), _truth("QUADPLEX RECEPTACLE", 1)]
    ai = [_row("DUPLEX RECEPTACLE", 102), _row("QUADPLEX RECEPTACLE", 1)]
    score = S.score_case(ai, truth=truth)
    assert score.count_lines_exact == 2
    score2 = S.score_case([_row("DUPLEX RECEPTACLE", 103), _row("QUADPLEX RECEPTACLE", 2)], truth=truth)
    assert score2.count_lines_exact == 0
    assert score2.count_lines_within_1 == 1


def test_bid_total_bands_and_safe_refusal():
    reference = {
        "case_id": "T",
        "quantity_lines": [{"line_id": "Q001", "area": "A", "section": "NEW WORK", "group": "OTHER ITEMS", "subgroup": "", "description": "DUPLEX RECEPTACLE", "unit": "EA", "qty": 10, "unit_item_cost": 10.0}],
        "g3_sent": {"total": 100000.0},
        "derived": {"g3_corrected_total": 104000.0},
        "estimator_recap": {"totals": {"bid": 101000.0, "hours": 500.0}},
        "g3_labor": {"hours": 520.0, "rate": 125.0},
    }
    ai = [_row("DUPLEX RECEPTACLE", 10)]
    priced = {"refused": False, "refusal_reasons": [], "labor_rate_usd": 125.0, "totals": {"bid_total_usd": 104000.0, "hours": 520.0}}
    score = S.score_case(ai, reference, priced=priced)
    assert score.bid is not None and score.bid.status == "pass"
    assert score.bid.vs_sent["error"] == pytest.approx(0.04) and score.bid.vs_sent["band"] == "good"
    assert score.bid.vs_corrected["error"] == pytest.approx(0.0)
    assert score.bid.vs_estimator["band"] == "good"
    assert score.bid.hours_vs_g3["error"] == pytest.approx(0.0) and score.bid.hours_vs_estimator["band"] == "good"
    miss = S.score_case(ai, reference, priced={**priced, "totals": {"bid_total_usd": 120000.0, "hours": 600.0}})
    assert miss.bid.status == "miss" and miss.bid.vs_sent["band"] == "miss"
    refused = S.score_case(ai, reference, priced={"refused": True, "refusal_reasons": ["new_devices_no_branch"], "totals": {"bid_total_usd": None, "hours": None}})
    assert refused.bid.status == "safe_refusal" and refused.bid.bid_total is None
    assert refused.bid.vs_sent["band"] == "none" and refused.bid.refusal_reasons == ["new_devices_no_branch"]
    assert S.score_case(ai, reference).bid is None
    # JSON keeps ratios and drops absolute dollars unless asked.
    data = S.score_json(score)
    assert "truth_cost" not in data and data["dollar_coverage"] == 1.0
    assert "bid_total" not in data["bid"] and data["bid"]["vs_sent"]["error"] == pytest.approx(0.04)
    full = S.score_json(score, include_dollars=True)
    assert full["truth_cost"] == pytest.approx(100.0) and full["bid"]["bid_total"] == 104000.0


def test_truth_key_collisions_merge_same_unit_and_report_unit_split():
    truth = [
        _truth("DUPLEX RECEPTACLE", 10, line_id="Q1"),
        _truth("DUPLEX RECEPTACLE, 125V", 5, line_id="Q2"),
        _truth("L8, TRACK CHANNEL", 20, "FT", group="LIGHTING FIXTURES", line_id="Q3"),
        _truth("L8, TRACK HEAD", 4, "EA", group="LIGHTING FIXTURES", line_id="Q4"),
    ]
    lines, report = S.classify_truth(truth, S.GLOBAL_ALIASES)
    # the L8 FT and EA lines share a LineKey (unit is not part of it): a
    # real collision, resolved by the UNIT attr, never silently ignored
    assert report.merged == 1 and report.unit_split == 1 and report.resolved == 2 and report.unresolved == 0
    split = next(d for d in report.detail if d["status"] == "unit_split")
    assert split["line_ids"] == ["Q3", "Q4"] and split["units"] == ["FT", "EA"]
    merged = next(ln for ln in lines if ln.line_id == "Q1")
    assert merged.qty == 15 and merged.merged_from == ["Q2"]
    assert len(lines) == 3  # Q2 folded into Q1; the FT and EA L8 lines stay distinct
    by = {ln.line_id: ln for ln in lines}
    assert by["Q3"].key.attrs == frozenset({"L8", "UNIT:FT"}) and by["Q4"].key.attrs == frozenset({"L8", "UNIT:EA"})
    assert truth[2].key is None  # the caller's lines are not mutated
    score = S.score_case([_row("DUPLEX RECEPTACLE", 15)], truth=truth)
    assert score.collisions.resolved == 2 and score.count_lines_exact == 1
    # AI rows route to the split line of their own unit, a third unit falls back as a unit mismatch
    ai = [_row("L8, TRACK", 20, "FT", group="LIGHTING FIXTURES"), _row("L8 HEAD", 4, group="LIGHTING FIXTURES")]
    sc = S.score_case(ai, truth=truth[2:])
    st = {ln.line_id: (ln.status, ln.ai_qty) for ln in sc.lines}
    assert st["Q3"] == ("good", 20) and st["Q4"] == ("exact", 4) and sc.unit_mismatches == 0
    odd = S.score_case([_row("L8 HEAD", 4, "LOT", group="LIGHTING FIXTURES")], truth=truth[2:])
    assert odd.unit_mismatches == 1 and odd.extras_count == 0


def test_truth_key_collision_without_a_unit_is_unresolved():
    truth = [_truth("L5 UNDERCABINET LED", 10, "EA", line_id="Q1"), _truth("L5 UNDERCABINET LED", 30, "", line_id="Q2")]
    _lines, report = S.classify_truth(truth, S.GLOBAL_ALIASES)
    assert report.unresolved == 1 and report.resolved == 0
    assert report.detail[0]["status"] == "unresolved" and report.detail[0]["units"] == ["EA", ""]
    third = truth + [_truth("L5 UNDERCABINET LED", 4, "EA", line_id="Q3")]
    _lines, report = S.classify_truth(third, S.GLOBAL_ALIASES)
    assert report.unresolved == 1 and report.merged == 1  # Q3 still merges into Q1


def test_refused_estimates_count_as_misses_in_markdown_and_rows_from_spec_caveats():
    spec = _spec("NEW WORK", "OTHER ITEMS", "", [_item("DUPLEX RECEPTACLE", 3, caveat="(NEED TO CONFIRM DETAILS)"), _item("STRAP", 5, accessory=True)])
    rows = S.rows_from_spec(spec)
    assert rows[0].description.endswith("(NEED TO CONFIRM DETAILS)") and rows[1].is_accessory
    score = S.score_case(rows, truth=[_truth("DUPLEX RECEPTACLE, 125V", 3)], priced={"refused": True, "refusal_reasons": ["census_floor"], "totals": {}})
    md = S.score_markdown(score)
    assert "safe_refusal" in md and "Count lines exact | 1 of 1" in md
    assert chr(0x2014) not in md and chr(0x2013) not in md


# Estimator vocabulary of the consolidator (7154 run 51ec302b) ---------------------------------


def test_estimator_vocabulary_lines_match_the_7154_truth_keys():
    """The names the consolidator now writes classify to the truth keys of
    spec 3.4 (fixture by tag, EM variant by suffix, track demolition by the
    word TRACK); the run's own wording did not (no tag: an extra; "including
    heads": a lighting demolition in FT, a unit mismatch)."""
    aliases = S.merge_aliases({"SHADED": "EM"})
    cases = {
        "L1 - 2' X 4' RECESSED TROFFER (CONTROL 0-10V)": {"L1"},
        "L1-EM - 2' X 4' RECESSED TROFFER (CONTROL 0-10V)": {"L1-EM"},
        "L2 - 2' X 2' RECESSED TROFFER (CONTROL 0-10V)": {"L2"},
        'L3-EM - 6" RECESSED DOWNLIGHT (CONTROL 0-10V)': {"L3-EM"},
        "L4 - TASK LED LIGHTING (CONTROL LOCAL)": {"L4"},
        "X1 - LED EDGE LIT EXIT SIGN. NUMBER OF FACES AND CHEVRONS PER PLANS": {"X1"},
    }
    for desc, attrs in cases.items():
        key = S.classify_line(desc, "NEW WORK", "LIGHTING FIXTURES", "EA", subgroup="FIXTURES", aliases=aliases)
        assert (key.kind, set(key.attrs)) == ("fixture", attrs), desc
    l5 = S.classify_line("L5 - UNDERCABINET LED LIGHTING (CONTROL LOCAL)", "NEW WORK", "LIGHTING FIXTURES", "FT", subgroup="FIXTURES", aliases=aliases)
    assert (l5.kind, set(l5.attrs)) == ("fixture", {"L5"})
    extra = S.classify_line("2X4 RECESSED LED TROFFER, NORMAL BRANCH", "NEW WORK", "LIGHTING FIXTURES", "EA", subgroup="FIXTURES", aliases=aliases)
    assert extra.kind == "fixture" and "L1" not in extra.attrs
    assert S.classify_line("REMOVAL OF LIGHTING TRACK", "DEMOLITION", "DEMOLITION", "FT", aliases=aliases).kind == "demo_track"
    assert S.classify_line("REMOVE EXISTING TRACK-LIGHTING ASSEMBLIES", "DEMOLITION", "DEMOLITION", "FT", aliases=aliases).kind == "demo_track"
    assert S.classify_line("REMOVE EXISTING TRACK-LIGHTING ASSEMBLIES INCLUDING HEADS", "DEMOLITION", "DEMOLITION", "FT", aliases=aliases).kind == "demo_lighting"


def test_device_vocabulary_names_score_against_the_7154_controls_truth():
    """7154 run 2 scored the dimmer lines as 13 against DIMMER & OCCUPANCY
    SENSOR SWITCH 2 and 0 against DIMMER SWITCH 10 because the reader's
    wording carried the word OCCUPANCY. The consolidator now writes the
    estimator vocabulary (electrical_items.canonical_device_description),
    which the scorer pairs line for line: 11 vs 10 within_1 (the rows
    counted 11; the lockable-cover dimmer sits in that count) and 2 vs 2
    exact. The reader wording survives in material_note, which the scorer
    never folds into the description; a caveat would be."""
    from app.services.electrical_items import canonical_device_description

    grp = dict(group="LIGHTING CONTROLS", subgroup="CONTROLS")
    truth = [_truth("DIMMER SWITCH", 10, **grp), _truth("DIMMER SWITCH W/ LOCKABLE COVER", 1, **grp),
             _truth("DIMMER & OCCUPANCY SENSOR SWITCH", 2, **grp)]
    run2 = [_row("WALL DIMMER, INCLUDING OCCUPANCY-SENSOR OVERRIDE DIMMERS", 11, **grp),
            _row("WALL OCCUPANCY-SENSING DIMMER", 2, **grp),
            _row("LOCKABLE DIMMER COVER", 1, group="OTHER ITEMS", subgroup="MISCELLANEOUS")]
    before = {ln.description: ln for ln in S.score_case(run2, truth=truth).lines}
    assert (before["DIMMER & OCCUPANCY SENSOR SWITCH"].ai_qty, before["DIMMER & OCCUPANCY SENSOR SWITCH"].status) == (13, "miss")
    assert (before["DIMMER SWITCH"].ai_qty, before["DIMMER SWITCH"].status) == (0, "miss")
    fixed = [_row(canonical_device_description(r.description), r.quantity, **grp) for r in run2[:2]] + run2[2:]
    assert [r.description for r in fixed[:2]] == ["DIMMER SWITCH", "DIMMER & OCCUPANCY SENSOR SWITCH"]
    score = S.score_case(fixed, truth=truth)
    after = {ln.description: ln for ln in score.lines}
    assert (after["DIMMER SWITCH"].ai_qty, after["DIMMER SWITCH"].status) == (11, "within_1")
    assert (after["DIMMER & OCCUPANCY SENSOR SWITCH"].ai_qty, after["DIMMER & OCCUPANCY SENSOR SWITCH"].status) == (2, "exact")
    assert (after["DIMMER SWITCH W/ LOCKABLE COVER"].ai_qty, after["DIMMER SWITCH W/ LOCKABLE COVER"].status) == (1, "exact")
    assert score.count_lines_exact == 2 and score.count_lines_total == 3
    # material_note stays out of the scored description; a caveat is folded in
    item = _item("DIMMER SWITCH", 11)
    item["material_note"] = "Reader wording: WALL DIMMER, INCLUDING OCCUPANCY-SENSOR OVERRIDE DIMMERS."
    rows = S.rows_from_spec(_spec("NEW WORK", "LIGHTING CONTROLS", "CONTROLS", [item]))
    assert rows[0].description == "DIMMER SWITCH"
    rows = S.rows_from_spec(_spec("NEW WORK", "LIGHTING CONTROLS", "CONTROLS", [_item("DIMMER SWITCH", 11, caveat="WALL DIMMER, INCLUDING OCCUPANCY-SENSOR OVERRIDE DIMMERS")]))
    assert "OCCUPANCY" in rows[0].description and S.classify_line(rows[0].description).attrs == frozenset({"OCC"})


# Leading fixture tags (Five Star 7172 smoke) ------------------------------------------


_FIX = dict(group="LIGHTING FIXTURES")


def test_leading_tag_forms_key_on_the_tag():
    """`<TAG> - <desc>` and `<TAG>, <desc>` key on the tag for one to three
    letters with an optional number; the 7172 truth wording and the 7154
    wording key as before."""
    def key(desc):
        return S.classify_line(desc, "NEW WORK", "LIGHTING FIXTURES", "EA")

    def fx(tag):
        return S.LineKey("fixture", frozenset({tag}))

    assert key("A, 2' X 4' CEILING LIGHT, LITHONIA LIGHTING, CPX 2X4 3000LM 80CRI 35K") == fx("A")
    assert key("A - 2' X 4' CEILING LIGHT") == fx("A")
    assert key("EX1, EXIT SIGN, LITHONIA LIGHTING, LQM S W RG MVOLT EL HO M6, 5 VA") == fx("EX1")
    assert key("EX1 - EXIT SIGN") == fx("EX1")
    assert key("AE1 - TROFFER") == fx("AE1")
    assert key("D - Long linear luminaire") == fx("D")
    assert key("A " + chr(0x2013) + " 2X4 CEILING LIGHT") == fx("A")  # an en dash normalizes to the hyphen form
    assert key("AE, 2' X 4' CEILING LIGHT, PROVIDE 10W EMERGENCY BATTERY PACK") == fx("AE-EM")
    # 7154 forms unchanged
    assert key("L1 - 2X4 LED TROFFER") == fx("L1")
    assert key("L1-EM - 2' X 4' RECESSED TROFFER - RE - LED") == fx("L1-EM")
    assert key("X1 - EXIT SIGN") == fx("X1")
    # a plain word is never a tag: the tag must lead and be followed by " - " or ", "
    for desc in ("LIGHT FIXTURE TYPE A", "A 2X4 CEILING LIGHT", "PROVIDE A - TROFFER", "LIGHT FIXTURE A, TROFFER", "ABCD - TROFFER"):
        assert key(desc).attrs.isdisjoint({"A", "ABCD"}), desc
        assert S._leading_tag(S.normalize_description(desc)) is None, desc


def test_variant_fallback_when_the_case_prices_one_variant_of_a_tag():
    """7172: the AE schedule row says EMERGENCY BATTERY PACK, so the truth
    keys `AE-EM`; a tag-first AI line without that wording still lands on
    it (and the reverse) because the case prices no plain `AE` line."""
    truth = [
        _truth("A, 2' X 4' CEILING LIGHT, LITHONIA LIGHTING, CPX", 26, **_FIX),
        _truth("AE, 2' X 4' CEILING LIGHT, LITHONIA LIGHTING, CPX, PROVIDE 10W EMERGENCY BATTERY PACK", 29, **_FIX),
        _truth("EX1, EXIT SIGN, LITHONIA LIGHTING", 6, **_FIX),
    ]
    ai = [_row("A - 2' X 4' CEILING LIGHT", 26, **_FIX), _row("AE - 2' X 4' CEILING LIGHT", 29, **_FIX), _row("EX1 - EXIT SIGN", 6, **_FIX)]
    score = S.score_case(ai, truth=truth)
    assert score.count_lines_exact == 3 and score.extras_count == 0
    # reverse: AI says emergency, truth text does not; still the AE line
    truth_plain = [_truth("AE, 2' X 4' CEILING LIGHT", 29, **_FIX)]
    score = S.score_case([_row("AE - 2' X 4' CEILING LIGHT WITH EMERGENCY BATTERY PACK", 29, **_FIX)], truth=truth_plain)
    assert score.count_lines_exact == 1
    # trailing-tag wording stays unmatched (the pipeline must write the tag first)
    score = S.score_case([_row("LIGHT FIXTURE TYPE A", 26, **_FIX)], truth=truth[:1])
    assert score.count_lines_exact == 0 and score.extras_count == 1


def test_variant_fallback_never_crosses_a_priced_variant_pair():
    """7154: L1 and L1-EM are both priced, so each AI line keeps its own
    truth line and a plain L1 overcount never spills onto L1-EM."""
    truth = [_truth("L1, 2X4 RECESSED LED TROFFER", 40, **_FIX), _truth("L1-EM - 2' X 4' RECESSED TROFFER", 8, **_FIX)]
    ai = [_row("L1 - 2X4 LED TROFFER", 40, **_FIX), _row("L1-EM - 2X4 LED TROFFER", 8, **_FIX)]
    score = S.score_case(ai, truth=truth)
    assert score.count_lines_exact == 2
    only_plain = S.score_case([_row("L1 - 2X4 LED TROFFER", 48, **_FIX)], truth=truth)
    by = {ln.description: ln for ln in only_plain.lines}
    assert by["L1-EM - 2' X 4' RECESSED TROFFER"].ai_qty == 0
    assert by["L1, 2X4 RECESSED LED TROFFER"].ai_qty == 48
    # a different tag never falls back (B is not a variant of A)
    assert S._variant_fallback_tier(S.LineKey("fixture", frozenset({"B"})), S.LineKey("fixture", frozenset({"A-EM"})), frozenset()) is None
    assert S._variant_fallback_tier(S.LineKey("receptacle", frozenset({"A"})), S.LineKey("receptacle", frozenset({"A-EM"})), frozenset()) is None
