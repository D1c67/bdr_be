"""Model-facing write tools and review schemas (services/plans_reader_review_schemas.py,
build spec 5.8).

What these tests pin:
- every exported schema passes validate_strict and is a valid JSON Schema;
- the op union carries exactly the ten ops of the 5.8 table with exactly
  their fields, each discriminated by a single-value `op` enum;
- the consolidator and measure write tools are the 5.8 set, update_boq_line
  mirrors every add_boq_line field as nullable, and every write tool has a
  refusal code list;
- sample recount, review and revise documents and tool args validate; an
  unknown op, set_unit without to_qty and write_brief with a field outside
  the enum fail;
- BOQ_ITEM_V2_ASSEMBLY is BOQ_ITEM plus the five v2 fields.

All numbers are synthetic. No network, no database.
"""

from __future__ import annotations

import copy
from pathlib import Path

import jsonschema
import pytest

from app.services import plans_reader_review_schemas as rs
from app.services.plans_reader_schemas import BOQ_ITEM, validate_strict

_VALIDATOR = jsonschema.Draft202012Validator


def _all_schemas() -> list[tuple[str, dict]]:
    out = [
        ("EVIDENCE", rs.EVIDENCE),
        ("OP", rs.OP),
        ("RECOUNT_SCHEMA", rs.RECOUNT_SCHEMA),
        ("REVIEW_SCHEMA", rs.REVIEW_SCHEMA),
        ("REVISE_SCHEMA", rs.REVISE_SCHEMA),
        ("BOQ_ITEM_V2_ASSEMBLY", rs.BOQ_ITEM_V2_ASSEMBLY),
    ]
    out += [(f"op:{k}", v) for k, v in rs.OP_SCHEMAS.items()]
    out += [(f"tool:{k}", v) for k, v in rs.CONSOLIDATOR_WRITE_TOOL_ARGS.items()]
    out += [(f"tool:{k}", v) for k, v in rs.MEASURE_WRITE_TOOL_ARGS.items()]
    return out


def _valid(schema: dict, doc: object) -> bool:
    return _VALIDATOR(schema).is_valid(doc)


def _errors(schema: dict, doc: object) -> list[str]:
    return [e.message for e in _VALIDATOR(schema).iter_errors(doc)]


# Strictness -------------------------------------------------------------------


@pytest.mark.parametrize(("name", "schema"), _all_schemas(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_schema_is_strict_and_well_formed(name, schema):
    validate_strict(schema)
    _VALIDATOR.check_schema(schema)


def test_validate_strict_still_catches_a_loose_op():
    loose = copy.deepcopy(rs.OP)
    del loose["anyOf"][0]["required"][-1]
    with pytest.raises(ValueError):
        validate_strict(loose)


def test_no_unsupported_keywords_in_model_facing_schemas():
    banned = {"const", "maxLength", "minLength", "pattern", "oneOf", "$ref", "default"}

    def walk(node):
        if isinstance(node, dict):
            assert not banned & set(node), node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for _name, schema in _all_schemas():
        walk(schema)


# Op union ---------------------------------------------------------------------

_OP_FIELDS = {
    "set_quantity": {"line_id", "from_qty", "to_qty", "basis", "note"},
    "set_unit": {"line_id", "from_unit", "to_unit", "to_qty", "basis"},
    "add_row": {
        "area", "section", "group", "subgroup", "description", "qty", "unit",
        "basis", "sheet_refs", "item_key",
    },
    "remove_row": {"line_id", "from_qty", "reason_code"},
    "set_flag": {"line_id", "flag", "policy_rule_id"},
    "add_allowance": {"name", "reason_code"},
    "remove_allowance": {"name", "reason_code"},
    "set_condition": {"key", "from_value", "to_value"},
    "add_clarification": {"text"},
    "add_rfi": {"question", "document_id", "page"},
}


def test_op_union_has_exactly_the_spec_ops_and_fields():
    assert set(rs.OP_NAMES) == set(_OP_FIELDS)
    assert len(rs.OP["anyOf"]) == len(_OP_FIELDS)
    for alt in rs.OP["anyOf"]:
        op = alt["properties"]["op"]
        assert op["type"] == "string" and len(op["enum"]) == 1
        name = op["enum"][0]
        assert set(alt["properties"]) == _OP_FIELDS[name] | {"op"}


def test_model_ops_never_claim_manual_basis():
    assert "manual" not in rs.OP_SET_QUANTITY["properties"]["basis"]["enum"]
    assert "manual" not in rs.ADD_BOQ_LINE_ARGS["properties"]["combine"]["enum"]


_SAMPLE_OPS = [
    {"op": "set_quantity", "line_id": "B3", "from_qty": 10, "to_qty": 12, "basis": "counted", "note": None},
    {"op": "set_unit", "line_id": "B4", "from_unit": "EA", "to_unit": "FT", "to_qty": 120.5, "basis": "measured"},
    {
        "op": "add_row", "area": "AREA 1", "section": "NEW WORK", "group": "OTHER ITEMS",
        "subgroup": "MISCELLANEOUS", "description": "JUNCTION BOX", "qty": 4, "unit": "EA",
        "basis": "counted", "sheet_refs": ["E1.01"], "item_key": None,
    },
    {"op": "remove_row", "line_id": "B5", "from_qty": 2, "reason_code": "duplicate"},
    {"op": "set_flag", "line_id": "B6", "flag": "by_others", "policy_rule_id": "R-TEMP-POWER"},
    {"op": "add_allowance", "name": "CORE DRILL", "reason_code": "policy"},
    {"op": "remove_allowance", "name": "ARC FLASH STUDY", "reason_code": "excluded"},
    {"op": "set_condition", "key": "wage_type", "from_value": None, "to_value": "prevailing_wage"},
    {"op": "add_clarification", "text": "Pricing assumes daytime work."},
    {"op": "add_rfi", "question": "Confirm panel LP-2 location.", "document_id": "doc-1", "page": 3},
]


@pytest.mark.parametrize("op", _SAMPLE_OPS, ids=lambda o: o["op"])
def test_each_sample_op_validates(op):
    assert _valid(rs.OP, op), _errors(rs.OP, op)


def test_unknown_op_fails():
    bad = {"op": "delete_everything", "line_id": "B1"}
    assert not _valid(rs.OP, bad)


def test_set_unit_without_to_qty_fails():
    bad = {"op": "set_unit", "line_id": "B4", "from_unit": "EA", "to_unit": "FT", "basis": "measured"}
    assert not _valid(rs.OP, bad)
    assert not _valid(rs.OP_SET_UNIT, bad)


def test_op_with_extra_field_fails():
    bad = dict(_SAMPLE_OPS[0], price=100)
    assert not _valid(rs.OP, bad)


def test_op_fields_cannot_cross_variants():
    # remove_row fields under the set_flag discriminator.
    bad = {"op": "set_flag", "line_id": "B5", "from_qty": 2, "reason_code": "duplicate"}
    assert not _valid(rs.OP, bad)


# Recount, review, revise ------------------------------------------------------

_EVIDENCE = {
    "type": "page_quote",
    "document_id": "doc-1",
    "page": 2,
    "quote": "ALL RECEPTACLES SHALL BE 20A",
    "ref_id": None,
}


def test_sample_recount_validates():
    doc = {
        "targets": [
            {
                "target_id": "T1",
                "marks": [{"tile_id": "r1c1", "x": 0.25, "y": 0.5, "confidence": 0.9}],
                "uncertain_regions": [
                    {"tile_id": "r1c2", "x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2, "reason": "overlap"}
                ],
                "symbol_seen_in_legend": True,
                "notes": None,
            }
        ]
    }
    assert _valid(rs.RECOUNT_SCHEMA, doc), _errors(rs.RECOUNT_SCHEMA, doc)
    doc["targets"][0]["symbol_seen_in_legend"] = None
    assert not _valid(rs.RECOUNT_SCHEMA, doc)


def _review_doc() -> dict:
    # Deep-copied so a test that mutates it never leaks into the shared samples.
    return copy.deepcopy({
        "summary": "Two issues.",
        "findings": [
            {
                "ref": "f1",
                "kind": "quantity_suspect",
                "severity": "warning",
                "title": "Receptacle count low",
                "detail": "Plan shows more duplex receptacles than the BOQ carries.",
                "line_ids": ["B3"],
                "evidence": [_EVIDENCE, {"type": "tool", "document_id": None, "page": None, "quote": None, "ref_id": "v:abc"}],
                "proposed_ops": [_SAMPLE_OPS[0], _SAMPLE_OPS[8]],
                "qty_delta_hint": 2,
                "confidence": 0.7,
            }
        ],
        "systems_checked": [{"system": "fire alarm", "status": "excluded", "line_ids": []}],
    })


def test_sample_review_validates():
    doc = _review_doc()
    assert _valid(rs.REVIEW_SCHEMA, doc), _errors(rs.REVIEW_SCHEMA, doc)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("kind",), "pricing_opinion"),
        (("severity",), "blocker"),
        (("evidence", 0, "type"), "hunch"),
        (("proposed_ops", 0, "op"), "rewrite_line"),
    ],
)
def test_review_rejects_values_outside_enums(path, value):
    doc = _review_doc()
    node = doc["findings"][0]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert not _valid(rs.REVIEW_SCHEMA, doc)


def test_review_status_enum():
    doc = _review_doc()
    doc["systems_checked"][0]["status"] = "maybe"
    assert not _valid(rs.REVIEW_SCHEMA, doc)


def test_sample_revise_validates():
    doc = {
        "dispositions": [
            {
                "finding_id": "f1",
                "decision": "apply",
                "reason": "Recount confirms 12.",
                "evidence": [{"type": "recount", "document_id": "doc-1", "page": 2, "quote": None, "ref_id": "T1"}],
                "ops": [_SAMPLE_OPS[0]],
            },
            {"finding_id": "f2", "decision": "decline", "reason": "By others.", "evidence": [], "ops": []},
        ]
    }
    assert _valid(rs.REVISE_SCHEMA, doc), _errors(rs.REVISE_SCHEMA, doc)
    doc["dispositions"][1]["decision"] = "defer"
    assert not _valid(rs.REVISE_SCHEMA, doc)


# Write tools ------------------------------------------------------------------

_CONSOLIDATOR_TOOLS = {
    "add_boq_line", "accept_candidates", "update_boq_line", "remove_boq_line",
    "account_rows", "add_allowance", "add_exclusion", "add_assumption", "add_rfi",
    "add_risk", "write_brief", "set_estimate_notes", "set_material_notes", "finish",
}


def test_tool_sets_match_spec_and_have_refusal_codes():
    assert set(rs.CONSOLIDATOR_WRITE_TOOL_ARGS) == _CONSOLIDATOR_TOOLS
    assert set(rs.MEASURE_WRITE_TOOL_ARGS) == {"set_linear_item", "set_panel_location"}
    tools = set(rs.CONSOLIDATOR_WRITE_TOOL_ARGS) | set(rs.MEASURE_WRITE_TOOL_ARGS)
    assert set(rs.REFUSAL_CODES) == tools
    for name, codes in rs.REFUSAL_CODES.items():
        assert codes and all(isinstance(c, str) and c for c in codes), name
        assert len(set(codes)) == len(codes), name


def test_add_boq_line_refusals_are_the_spec_list():
    assert set(rs.REFUSAL_CODES["add_boq_line"]) == {
        "unknown_ref", "unverifiable_ref", "superseded_ref", "unit_mismatch", "ref_consumed",
        "partial_requires_floor", "excluded_by_fact", "vocab", "conduit_form",
        # Wave 4 addition (integration run 1): a fixture line naming a tag
        # other than its cited rows' tag.
        "luminaire_tag",
    }


def test_update_boq_line_mirrors_add_fields_as_nullable():
    add_props = rs.ADD_BOQ_LINE_ARGS["properties"]
    upd_props = rs.UPDATE_BOQ_LINE_ARGS["properties"]
    assert set(upd_props) == set(add_props) | {"line_id"}
    nulls = {k: None for k in add_props}
    assert _valid(rs.UPDATE_BOQ_LINE_ARGS, {"line_id": "B7", **nulls})
    assert _valid(
        rs.UPDATE_BOQ_LINE_ARGS,
        {"line_id": "B7", **nulls, "unit": "FT", "source_refs": [{"ref": "R2", "multiplier": 1}]},
    )
    assert not _valid(rs.UPDATE_BOQ_LINE_ARGS, {"line_id": "B7", **nulls, "unit": "MILES"})
    assert not _valid(rs.UPDATE_BOQ_LINE_ARGS, {"line_id": None, **nulls})


_TOOL_SAMPLES = {
    "add_boq_line": {
        "area": "AREA 1", "section": "NEW WORK", "group": "WIRING", "subgroup": "CONDUITS",
        "description": '3/4" EMT', "unit": "FT", "source_refs": [{"ref": "M1", "multiplier": 1}],
        "combine": "sum", "caveat": "(ASSUMED)", "material_note": None,
        "material_category": "General Material",
    },
    "accept_candidates": {
        "ids": ["K1", "K2"],
        "overrides": [
            {"id": "K2", "description": None, "group": None, "subgroup": "MISCELLANEOUS", "caveat": None, "material_note": None}
        ],
    },
    "remove_boq_line": {"line_id": "B9", "reason": "duplicate of B3"},
    "account_rows": {
        "row_ids": ["C4", "C5"], "disposition": "duplicate_view", "reason": "enlarged plan",
        "fact_id": None, "duplicate_of": "C1", "note_ref": None,
    },
    "add_allowance": {"name": "CORE DRILL", "cite": {"row_id": "O3", "fact_id": None}},
    "add_exclusion": {"text": "Temporary power by others.", "cite": "F2"},
    "add_assumption": {"text": "Daytime work.", "cite": "E0.01"},
    "add_rfi": {"text": "Confirm feeder size.", "cite": "S2"},
    "add_risk": {"text": "Long lead switchgear.", "cite": "26 24 13"},
    "write_brief": {"field": "systems", "value": [{"system": "lighting", "in_scope": "yes", "notes": None}]},
    "set_estimate_notes": {"general_notes": ["Note."], "area_notes": [{"area": "AREA 1", "notes": []}]},
    "set_material_notes": {
        "notes": [
            {"material_category": "Lighting", "notes": [], "specified_manufacturers": ["Acme"], "lead_time_items": []}
        ]
    },
    "finish": {"confidence_notes": ["Counts verified by census."]},
    "set_linear_item": {
        "document_id": "doc-1", "page": 4, "tag": "L1", "candidate_ids": ["c1"],
        "endpoints": [[[0.1, 0.2], [0.3, 0.2]]], "view_id": None,
    },
    "set_panel_location": {"panel": "LP-1", "document_id": "doc-1", "page": 4, "view_id": "v1", "point": [0.5, 0.5]},
}
_TOOL_SAMPLES["update_boq_line"] = {
    "line_id": "B3", **{k: None for k in _TOOL_SAMPLES["add_boq_line"]}, "description": '1" EMT',
}


@pytest.mark.parametrize("tool", sorted(_TOOL_SAMPLES))
def test_each_tool_sample_validates(tool):
    schema = rs.CONSOLIDATOR_WRITE_TOOL_ARGS.get(tool) or rs.MEASURE_WRITE_TOOL_ARGS[tool]
    args = _TOOL_SAMPLES[tool]
    assert _valid(schema, args), _errors(schema, args)


def test_every_tool_has_a_sample():
    assert set(_TOOL_SAMPLES) == set(rs.CONSOLIDATOR_WRITE_TOOL_ARGS) | set(rs.MEASURE_WRITE_TOOL_ARGS)


def test_write_brief_field_outside_enum_fails():
    args = {"field": "labor_hours", "value": "120"}
    assert not _valid(rs.WRITE_BRIEF_ARGS, args)
    assert _valid(rs.WRITE_BRIEF_ARGS, {"field": "project_summary", "value": "A clinic."})


def test_brief_value_schema_pins_the_field_shape():
    systems = rs.brief_value_schema("systems")
    assert not _valid(systems, "prose")
    assert _valid(rs.brief_value_schema("project_summary"), "prose")
    with pytest.raises(KeyError):
        rs.brief_value_schema("assumptions")


def test_account_rows_disposition_enum():
    args = dict(_TOOL_SAMPLES["account_rows"], disposition="ignored")
    assert not _valid(rs.ACCOUNT_ROWS_ARGS, args)


def test_add_boq_line_rejects_manual_combine_and_unknown_unit():
    base = _TOOL_SAMPLES["add_boq_line"]
    assert not _valid(rs.ADD_BOQ_LINE_ARGS, dict(base, combine="manual"))
    assert not _valid(rs.ADD_BOQ_LINE_ARGS, dict(base, unit="LF"))


# BOQ line v2 ------------------------------------------------------------------


def test_boq_item_v2_assembly_extends_boq_item():
    props = rs.BOQ_ITEM_V2_ASSEMBLY["properties"]
    assert set(props) == set(BOQ_ITEM["properties"]) | {
        "line_id", "source_refs", "combine", "basis", "needs_recount",
    }
    assert "manual" in props["combine"]["enum"]
    assert set(props["basis"]["enum"]) == set(rs.QUANTITY_BASES)
    line = {
        "description": "DUPLEX RECEPTACLE", "quantity": 12, "unit": "EA", "sheet_refs": ["E1.01"],
        "caveat": None, "material_note": None, "material_category": "General Material",
        "wastage_applies": False, "is_accessory": False, "line_id": "B3",
        "source_refs": [{"ref": "C1", "multiplier": 1}], "combine": "sum", "basis": "counted",
        "needs_recount": False,
    }
    assert _valid(rs.BOQ_ITEM_V2_ASSEMBLY, line), _errors(rs.BOQ_ITEM_V2_ASSEMBLY, line)
    # The legacy model schema is untouched.
    assert "line_id" not in BOQ_ITEM["properties"]


# House rules ------------------------------------------------------------------


def test_module_has_no_long_dashes():
    src = Path(rs.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in src and chr(0x2013) not in src
