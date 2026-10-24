"""plans_reader_ops: every op precondition of spec 5.8, the WS7 evidence
rules, set_unit without a measured or quoted quantity never applies (the
to_qty must equal a quantity a measurement or quote tool result returned,
within UNIT_QTY_TOLERANCE), the ops of one finding apply all or none, the
op cap and the large_revision flag. Pure, synthetic numbers only."""

from __future__ import annotations

import copy

import pytest

from app.services import plans_reader_ops as ops
from app.services import plans_reader_rows as rows_mod
from app.services.plans_reader_review_schemas import MAX_OPS_PER_REVIEW

FE = ops.FindingEvidence


def _item(desc, qty, unit="EA", line_id=None, **extra):
    item = {"description": desc, "quantity": qty, "unit": unit, "sheet_refs": ["E2.01"], "caveat": None,
            "material_note": None, "material_category": "General Material", "wastage_applies": unit == "FT",
            "is_accessory": False, **extra}
    if line_id:
        item["line_id"] = line_id
    return item


def _spec():
    return {
        "closing_notes": [],
        "areas": [{"area": "BASE BID", "notes": [], "sections": [
            {"heading": "NEW WORK", "groups": [
                {"name": "OTHER ITEMS", "subgroups": [
                    {"name": "RECEPTACLES (RECEPTACLES INCLUDES 4\"S DEEP COMB BOX AND COVER PLATE)", "items": [
                        _item("DUPLEX RECEPTACLE", 74, line_id="B1"),
                        _item("GFCI RECEPTACLE", 4, line_id="B2"),
                    ]},
                ]},
                {"name": "WIRING", "subgroups": [
                    {"name": "CONDUCTORS (AWG)", "items": [_item("#12 THHN", 6000, unit="FT", line_id="B3")]},
                    {"name": "CONDUITS", "items": [_item('3/4"C EMT', 5000, unit="FT", line_id="B4")]},
                ]},
                {"name": "LIGHTING FIXTURES", "subgroups": [
                    {"name": "FIXTURES", "items": [
                        _item("L5 UNDERCABINET LED", 7, line_id="B5"),
                        _item("L1 2X4 LED TROFFER", 40, line_id="B6"),
                    ]},
                ]},
            ]},
        ]}],
    }


def _lines(spec):
    return {r["line_id"]: r for r in rows_mod.iter_boq_lines(spec)}


def _apply(spec, op_list, **kw):
    return ops.apply_ops(spec, op_list, **kw)


# Preconditions ---------------------------------------------------------------------------


def test_set_quantity_needs_the_current_quantity_within_tolerance():
    spec = _spec()
    ev = {"F": FE(signals=frozenset({ops.SIGNAL_RECOUNT_RAISE}))}
    _new, applied, rejected = _apply(spec, [{"op": "set_quantity", "line_id": "B1", "from_qty": 70, "to_qty": 80,
                                             "basis": "counted", "note": None, "finding_id": "F"}], evidence=ev)
    assert applied == [] and rejected[0]["code"] == ops.REJECT_QTY_MISMATCH
    new, applied, rejected = _apply(spec, [{"op": "set_quantity", "line_id": "B1", "from_qty": 74.0000001, "to_qty": 80,
                                            "basis": "counted", "note": "recount", "finding_id": "F"}], evidence=ev)
    assert rejected == [] and _lines(new)["B1"]["quantity"] == 80
    assert _lines(new)["B1"]["basis"] == "counted" and "REVIEW: recount" in _lines(new)["B1"]["caveat"]
    assert _lines(spec)["B1"]["quantity"] == 74  # the input spec is never mutated


def test_set_quantity_unknown_line_and_ea_whole_numbers():
    spec = _spec()
    ev = {"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}))}
    _n, applied, rejected = _apply(spec, [{"op": "set_quantity", "line_id": "B99", "from_qty": 1, "to_qty": 2,
                                           "basis": "counted", "note": None, "finding_id": "F"}], evidence=ev)
    assert rejected[0]["code"] == ops.REJECT_UNKNOWN_LINE
    _n, applied, rejected = _apply(spec, [{"op": "set_quantity", "line_id": "B1", "from_qty": 74, "to_qty": 75.5,
                                           "basis": "counted", "note": None, "finding_id": "F"}], evidence=ev)
    assert rejected[0]["code"] == ops.REJECT_EA_WHOLE
    new, applied, rejected = _apply(spec, [{"op": "set_quantity", "line_id": "B3", "from_qty": 6000, "to_qty": 6718.4,
                                            "basis": "measured", "note": None, "finding_id": "F"}], evidence=ev)
    assert rejected == [] and _lines(new)["B3"]["quantity"] == pytest.approx(6718.4)


def test_set_unit_without_a_measured_or_quoted_quantity_never_applies():
    spec = _spec()
    op = {"op": "set_unit", "line_id": "B5", "from_unit": "EA", "to_unit": "FT", "to_qty": 43.46, "basis": "measured",
          "finding_id": "F"}
    for ev in (FE(), FE(signals=frozenset({ops.SIGNAL_PAGE_QUOTE, ops.SIGNAL_BAND, ops.SIGNAL_RULE}), quantities=(43.46,)),
               FE(signals=frozenset({ops.SIGNAL_RECOUNT_RAISE, ops.SIGNAL_CENSUS_FLOOR}), policy_hit=True, quantities=(43.46,))):
        new, applied, rejected = _apply(spec, [op], evidence={"F": ev})
        assert applied == []
        assert rejected[0]["code"] == ops.REJECT_EVIDENCE_UNIT and rejected[0]["route"] == "human"
        assert _lines(new)["B5"] == _lines(spec)["B5"]  # the row stays
    for ev in (FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}), quantities=(43.46,)),
               FE(signals=frozenset({ops.SIGNAL_QUOTE}), quantities=(12.0, 43.46))):
        new, applied, rejected = _apply(spec, [op], evidence={"F": ev})
        assert rejected == [] and _lines(new)["B5"]["unit"] == "FT" and _lines(new)["B5"]["quantity"] == pytest.approx(43.46)


def test_set_unit_to_qty_must_equal_a_measured_or_quoted_quantity():
    """H3: the measurement signal alone is not enough; to_qty must be one of
    the quantities the measurement or quote tool results returned, within
    UNIT_QTY_TOLERANCE (relative 2%). No quantity on record fails closed."""
    spec = _spec()
    op = {"op": "set_unit", "line_id": "B5", "from_unit": "EA", "to_unit": "FT", "to_qty": 43.5, "basis": "measured",
          "finding_id": "F"}
    # a matching measurement: 43.46 measured, 43.5 proposed (0.1%): applies
    new, applied, rejected = _apply(spec, [op], evidence={"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}), quantities=(43.46,))})
    assert rejected == [] and _lines(new)["B5"]["unit"] == "FT" and _lines(new)["B5"]["quantity"] == pytest.approx(43.5)
    # a different measured quantity: refused, routed to a human, the row stays
    new, applied, rejected = _apply(spec, [op], evidence={"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}), quantities=(60.0,))})
    assert applied == [] and rejected[0]["code"] == ops.REJECT_EVIDENCE_UNIT_QTY and rejected[0]["route"] == "human"
    assert _lines(new)["B5"] == _lines(spec)["B5"]
    # the signal without any quantity on record (fail closed)
    assert _apply(spec, [op], evidence={"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}))})[2][0]["code"] == ops.REJECT_EVIDENCE_UNIT_QTY
    assert _apply(spec, [op], evidence={"F": FE(signals=frozenset({ops.SIGNAL_QUOTE}))})[2][0]["code"] == ops.REJECT_EVIDENCE_UNIT_QTY
    # the tolerance edge: 2% of the larger value
    assert ops.UNIT_QTY_TOLERANCE == 0.02
    ev = FE(signals=frozenset({ops.SIGNAL_QUOTE}), quantities=(100.0,))
    assert ev.quantity_matches(98.0) and ev.quantity_matches(102.0) and not ev.quantity_matches(97.0) and not ev.quantity_matches(103.0)
    assert not ev.quantity_matches(None) and not FE(quantities=()).quantity_matches(100.0)
    # the quantity check is part of the finding's all-or-none group
    note = {"op": "add_clarification", "text": "L5 is a continuous row.", "finding_id": "F"}
    new, applied, rejected = _apply(spec, [op, note], evidence={"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}), quantities=(60.0,))})
    assert applied == [] and {r["code"] for r in rejected} == {ops.REJECT_EVIDENCE_UNIT_QTY, ops.REJECT_FINDING_GROUP}
    assert new.get("closing_notes", []) == []


def test_set_unit_preconditions():
    spec = _spec()
    ev = {"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}))}
    bad_from = {"op": "set_unit", "line_id": "B5", "from_unit": "FT", "to_unit": "EA", "to_qty": 3, "basis": "measured", "finding_id": "F"}
    assert _apply(spec, [bad_from], evidence=ev)[2][0]["code"] == ops.REJECT_UNIT_MISMATCH
    linear_ea = {"op": "set_unit", "line_id": "B3", "from_unit": "FT", "to_unit": "EA", "to_qty": 3, "basis": "measured", "finding_id": "F"}
    assert _apply(spec, [linear_ea], evidence=ev)[2][0]["code"] == ops.REJECT_LINEAR_UNIT
    bad_unit = {"op": "set_unit", "line_id": "B5", "from_unit": "EA", "to_unit": "LF", "to_qty": 3, "basis": "measured", "finding_id": "F"}
    assert _apply(spec, [bad_unit], evidence=ev)[2][0]["code"] == ops.REJECT_INVALID_ARGS


def test_add_row_vocabulary_whole_numbers_and_linear_unit():
    spec = _spec()
    ev = {"F": FE(signals=frozenset({ops.SIGNAL_CENSUS_FLOOR}))}
    base = {"op": "add_row", "area": "BASE BID", "section": "NEW WORK", "group": "OTHER ITEMS", "subgroup": "CONTROLS",
            "description": "THERMOSTAT RELOCATION", "qty": 5, "unit": "EA", "basis": "counted", "sheet_refs": ["M2.01"],
            "item_key": None, "finding_id": "F"}
    new, applied, rejected = _apply(spec, [base], evidence=ev)
    assert rejected == [] and applied[0]["line_id"] == "B7"
    row = _lines(new)["B7"]
    assert row["group"] == "OTHER ITEMS" and row["subgroup"] == "CONTROLS" and row["quantity"] == 5
    assert row["material_category"] == "General Material" and row["review_op"]["finding_id"] == "F"
    assert _apply(spec, [{**base, "group": "PLUMBING"}], evidence=ev)[2][0]["code"] == ops.REJECT_VOCAB
    assert _apply(spec, [{**base, "subgroup": "NOT A SUBGROUP"}], evidence=ev)[2][0]["code"] == ops.REJECT_VOCAB
    assert _apply(spec, [{**base, "section": "ALTERNATE"}], evidence=ev)[2][0]["code"] == ops.REJECT_VOCAB
    assert _apply(spec, [{**base, "qty": 2.5}], evidence=ev)[2][0]["code"] == ops.REJECT_EA_WHOLE
    conduit = {**base, "group": "WIRING", "subgroup": "CONDUITS", "description": '1"C EMT', "qty": 3, "unit": "EA"}
    assert _apply(spec, [conduit], evidence=ev)[2][0]["code"] == ops.REJECT_LINEAR_UNIT
    conduit_ft = {**conduit, "unit": "FT", "qty": 120.5, "basis": "measured"}
    new, applied, rejected = _apply(spec, [conduit_ft], evidence={"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}))})
    assert rejected == [] and _lines(new)[applied[0]["line_id"]]["wastage_applies"] is True


def test_remove_row_needs_the_quantity_and_a_reason_and_two_signals():
    spec = _spec()
    op = {"op": "remove_row", "line_id": "B2", "from_qty": 4, "reason_code": "by_others", "finding_id": "F"}
    two = FE(signals=frozenset({ops.SIGNAL_PAGE_QUOTE, ops.SIGNAL_RULE}))
    new, applied, rejected = _apply(spec, [op], evidence={"F": two})
    assert rejected == [] and "B2" not in _lines(new) and new["review_removed_lines"][0]["line_id"] == "B2"
    assert _apply(spec, [{**op, "from_qty": 5}], evidence={"F": two})[2][0]["code"] == ops.REJECT_QTY_MISMATCH
    assert _apply(spec, [{**op, "reason_code": "because"}], evidence={"F": two})[2][0]["code"] == ops.REJECT_INVALID_ARGS
    one = FE(signals=frozenset({ops.SIGNAL_PAGE_QUOTE}))
    assert _apply(spec, [op], evidence={"F": one})[2][0]["code"] == ops.REJECT_EVIDENCE_DECREASE


def test_set_flag_line_exists_and_decrease_flags_need_evidence():
    spec = _spec()
    policy = FE(signals=frozenset({ops.SIGNAL_POLICY}), policy_hit=True)
    new, applied, rejected = _apply(spec, [{"op": "set_flag", "line_id": "B6", "flag": "by_others", "policy_rule_id": "owner_furnished",
                                            "finding_id": "F"}], evidence={"F": policy})
    assert rejected == [] and _lines(new)["B6"]["flags"] == ["by_others"] and _lines(new)["B6"]["policy_rule_id"] == "owner_furnished"
    again = _apply(new, [{"op": "set_flag", "line_id": "B6", "flag": "by_others", "policy_rule_id": None, "finding_id": "F"}],
                   evidence={"F": policy})
    assert again[2][0]["code"] == ops.REJECT_NO_CHANGE
    assert _apply(spec, [{"op": "set_flag", "line_id": "B42", "flag": "excluded", "policy_rule_id": None}])[2][0]["code"] == ops.REJECT_UNKNOWN_LINE
    weak = FE(signals=frozenset({ops.SIGNAL_RULE}))
    assert _apply(spec, [{"op": "set_flag", "line_id": "B6", "flag": "excluded", "policy_rule_id": None, "finding_id": "F"}],
                  evidence={"F": weak})[2][0]["code"] == ops.REJECT_EVIDENCE_DECREASE
    # alternate is not a decrease: one signal suffices
    alt = _apply(spec, [{"op": "set_flag", "line_id": "B6", "flag": "alternate", "policy_rule_id": None, "finding_id": "F"}],
                 evidence={"F": weak})
    assert alt[2] == [] and _lines(alt[0])["B6"]["flags"] == ["alternate"]


def test_allowances_use_the_vocabulary_or_a_policy_name():
    spec = _spec()
    ok = {"op": "add_allowance", "name": "Arc Flash Study", "reason_code": "spec_260573"}
    new, applied, rejected = _apply(spec, [ok])
    assert rejected == [] and new["review_allowances"][0]["name"] == "ARC FLASH STUDY"
    bad = {"op": "remove_allowance", "name": "Honeywell fire alarm", "reason_code": "excluded"}
    assert _apply(spec, [bad])[2][0]["code"] == ops.REJECT_ALLOWANCE_VOCAB
    new, applied, rejected = _apply(spec, [bad], allowance_names=["HONEYWELL FIRE ALARM"])
    assert rejected == [] and new["review_allowances"][0]["op"] == "remove_allowance"
    assert _apply(spec, [{**ok, "reason_code": ""}])[2][0]["code"] == ops.REJECT_INVALID_ARGS


def test_set_condition_needs_the_current_value_and_a_statute_quote():
    spec = _spec()
    op = {"op": "set_condition", "key": "wage_type", "from_value": "non_prevailing_wage", "to_value": "prevailing_wage",
          "finding_id": "F"}
    statute = FE(signals=frozenset({ops.SIGNAL_PAGE_QUOTE, ops.SIGNAL_STATUTE_QUOTE}))
    assert _apply(spec, [op], evidence={"F": statute}, conditions={"wage_type": "prevailing_wage"})[2][0]["code"] == ops.REJECT_CONDITION_MISMATCH
    plain = FE(signals=frozenset({ops.SIGNAL_PAGE_QUOTE, ops.SIGNAL_RULE}))
    assert _apply(spec, [op], evidence={"F": plain}, conditions={"wage_type": "non_prevailing_wage"})[2][0]["code"] == ops.REJECT_EVIDENCE_WAGE
    new, applied, rejected = _apply(spec, [op], evidence={"F": statute}, conditions={"wage_type": "non_prevailing_wage"})
    assert rejected == [] and new["review_conditions"]["wage_type"]["to"] == "prevailing_wage"
    # unknown wage: from_value null
    null_op = {**op, "from_value": None}
    new, applied, rejected = _apply(spec, [null_op], evidence={"F": statute}, conditions={"wage_type": None})
    assert rejected == [] and applied[0]["before"] == {"value": None}


def test_clarification_length_and_rfi_page():
    spec = _spec()
    new, applied, rejected = _apply(spec, [{"op": "add_clarification", "text": "Thermostat relocation by EC; control cable by EC."}])
    assert rejected == [] and new["closing_notes"] == ["Thermostat relocation by EC; control cable by EC."]
    assert _apply(spec, [{"op": "add_clarification", "text": "x" * 301}])[2][0]["code"] == ops.REJECT_CLARIFICATION_LENGTH
    rfi = {"op": "add_rfi", "question": "Confirm the L4 control type.", "document_id": "doc-1", "page": 3}
    new, applied, rejected = _apply(spec, [rfi], pages_of={"doc-1": 10})
    assert rejected == [] and new["review_rfis"][0]["page"] == 3
    assert _apply(spec, [{**rfi, "page": 11}], pages_of={"doc-1": 10})[2][0]["code"] == ops.REJECT_UNKNOWN_PAGE
    assert _apply(spec, [{**rfi, "document_id": "doc-2"}], pages_of={"doc-1": 10})[2][0]["code"] == ops.REJECT_UNKNOWN_PAGE
    assert _apply(spec, [{**rfi, "page": 0}])[2][0]["code"] == ops.REJECT_INVALID_ARGS


def test_unknown_op_is_rejected():
    assert _apply(_spec(), [{"op": "set_price", "line_id": "B1"}])[2][0]["code"] == ops.REJECT_UNKNOWN_OP


# Evidence rules --------------------------------------------------------------------------


def test_additions_need_a_quote_recount_census_or_measurement():
    spec = _spec()
    op = {"op": "set_quantity", "line_id": "B1", "from_qty": 74, "to_qty": 80, "basis": "counted", "note": None, "finding_id": "F"}
    for bad in (FE(), FE(signals=frozenset({ops.SIGNAL_BAND, ops.SIGNAL_RULE})), FE(signals=frozenset({ops.SIGNAL_RECOUNT}))):
        assert _apply(spec, [op], evidence={"F": bad})[2][0]["code"] == ops.REJECT_EVIDENCE_ADDITION
    for good in ops.ADDITION_SIGNALS:
        assert _apply(spec, [op], evidence={"F": FE(signals=frozenset({good}))})[2] == []


def test_decreases_need_two_signals_or_a_policy_hit():
    spec = _spec()
    op = {"op": "set_quantity", "line_id": "B1", "from_qty": 74, "to_qty": 60, "basis": "counted", "note": None, "finding_id": "F"}
    assert _apply(spec, [op], evidence={"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}))})[2][0]["code"] == ops.REJECT_EVIDENCE_DECREASE
    # recount and recount_raise are the same signal
    one = FE(signals=frozenset({ops.SIGNAL_RECOUNT, ops.SIGNAL_RECOUNT_RAISE}))
    assert one.independent == 1
    assert _apply(spec, [op], evidence={"F": one})[2][0]["code"] == ops.REJECT_EVIDENCE_DECREASE
    two = FE(signals=frozenset({ops.SIGNAL_RECOUNT, ops.SIGNAL_PAGE_QUOTE}))
    assert _apply(spec, [op], evidence={"F": two})[2] == []
    assert _apply(spec, [op], evidence={"F": FE(policy_hit=True)})[2] == []
    assert _apply(spec, [op], evidence={"F": FE(signals=frozenset({ops.SIGNAL_POLICY}))})[2] == []


def test_ops_without_a_finding_have_no_evidence():
    spec = _spec()
    op = {"op": "set_quantity", "line_id": "B1", "from_qty": 74, "to_qty": 80, "basis": "counted", "note": None}
    assert _apply(spec, [op])[2][0]["code"] == ops.REJECT_EVIDENCE_ADDITION


# All or none, cap, large revision --------------------------------------------------------


def test_ops_of_one_finding_apply_all_or_none():
    spec = _spec()
    ev = {"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}))}
    good = {"op": "set_quantity", "line_id": "B3", "from_qty": 6000, "to_qty": 6718, "basis": "measured", "note": None, "finding_id": "F"}
    bad = {"op": "set_quantity", "line_id": "B4", "from_qty": 1, "to_qty": 6338, "basis": "measured", "note": None, "finding_id": "F"}
    other = {"op": "add_clarification", "text": "Branch wire measured from the panel schedules.", "finding_id": "G"}
    new, applied, rejected = _apply(spec, [good, bad, other], evidence=ev)
    assert [a["finding_id"] for a in applied] == ["G"]
    assert {r["code"] for r in rejected} == {ops.REJECT_QTY_MISMATCH, ops.REJECT_FINDING_GROUP}
    assert _lines(new)["B3"]["quantity"] == 6000  # the good op of the failed finding was rolled back
    assert new["closing_notes"] == ["Branch wire measured from the panel schedules."]
    assert new["review_revision"] == {"version": ops.OPS_VERSION, "applied": 1, "rejected": 2, "move_share": None, "flags": []}
    both, applied, rejected = _apply(spec, [good, {**bad, "from_qty": 5000}], evidence=ev)
    assert rejected == [] and _lines(both)["B3"]["quantity"] == 6718 and _lines(both)["B4"]["quantity"] == 6338


def test_at_most_forty_ops_per_review():
    spec = _spec()
    many = [{"op": "add_clarification", "text": f"Note {i}", "finding_id": f"F{i}"} for i in range(MAX_OPS_PER_REVIEW + 3)]
    new, applied, rejected = _apply(spec, many)
    assert len(applied) == MAX_OPS_PER_REVIEW and len(rejected) == 3
    assert all(r["code"] == ops.REJECT_OP_CAP for r in rejected)
    assert len(new["closing_notes"]) == MAX_OPS_PER_REVIEW


def test_large_revision_flag_over_fifteen_percent_of_the_total():
    spec = _spec()
    ev = {"F": FE(signals=frozenset({ops.SIGNAL_MEASUREMENT}))}
    values = {"B1": 2_000.0, "B3": 10_000.0, "B4": 8_000.0}
    small = {"op": "set_quantity", "line_id": "B3", "from_qty": 6000, "to_qty": 6600, "basis": "measured", "note": None, "finding_id": "F"}
    new, _a, _r = _apply(spec, [small], evidence=ev, line_values=values, total_usd=20_000.0)
    assert new["review_revision"]["move_share"] == pytest.approx(0.05) and new["review_revision"]["flags"] == []
    big = {"op": "set_quantity", "line_id": "B3", "from_qty": 6000, "to_qty": 9000, "basis": "measured", "note": None, "finding_id": "F"}
    new, _a, _r = _apply(spec, [big], evidence=ev, line_values=values, total_usd=20_000.0)
    assert new["review_revision"]["move_share"] == pytest.approx(0.25) and new["review_revision"]["flags"] == [ops.LARGE_REVISION_FLAG]
    removal = {"op": "remove_row", "line_id": "B4", "from_qty": 5000, "reason_code": "duplicate", "finding_id": "F"}
    new, _a, _r = _apply(spec, [removal], evidence={"F": FE(policy_hit=True)}, line_values=values, total_usd=20_000.0)
    assert new["review_revision"]["move_share"] == pytest.approx(0.4)
    assert _apply(spec, [big], evidence=ev)[0]["review_revision"]["move_share"] is None


def test_legacy_spec_without_line_ids_gets_transient_ids_that_resolve():
    spec = _spec()
    for area in spec["areas"]:
        for section in area["sections"]:
            for group in section["groups"]:
                for sub in group["subgroups"]:
                    for item in sub["items"]:
                        item.pop("line_id")
    ids = list(_lines(spec))
    assert ids == ["B1", "B2", "B3", "B4", "B5", "B6"]
    new, applied, rejected = _apply(spec, [{"op": "set_quantity", "line_id": "B6", "from_qty": 40, "to_qty": 41, "basis": "counted",
                                            "note": None, "finding_id": "F"}], evidence={"F": FE(signals=frozenset({ops.SIGNAL_RECOUNT_RAISE}))})
    assert rejected == [] and _lines(new)["B6"]["quantity"] == 41 and _lines(new)["B6"]["description"] == "L1 2X4 LED TROFFER"
    assert all(is_id for is_id in (r.get("line_id") for r in rows_mod.iter_boq_lines(new)))


def test_input_spec_is_never_mutated_by_rejections_or_applications():
    spec = _spec()
    frozen = copy.deepcopy(spec)
    _apply(spec, [{"op": "remove_row", "line_id": "B1", "from_qty": 74, "reason_code": "duplicate", "finding_id": "F"},
                  {"op": "add_clarification", "text": "note", "finding_id": "G"}], evidence={"F": FE(policy_hit=True)})
    assert spec == frozen
