"""Model-facing write tools and review schemas for AI Estimating (build spec 5.8).

This is an S3 contract module: data only, no I/O, imported by WS3 (measure
write tools), WS4 (consolidator write tools), WS7 (recount, review, revise and
the typed op union) and WS8. The shapes here are normative; implementers
register them unchanged and never redefine them.

Every schema is OpenAI strict structured-output compatible in the sense of
`plans_reader_schemas.validate_strict`: objects list every property as
required and forbid additional properties; optional values are nullable
(`["<type>", "null"]` for scalars and arrays, `anyOf [enum, null]` for
enums, as in plans_reader_schemas). Tool args are also validated server-side
with `jsonschema`.

What lives here:

- EVIDENCE, the OP union (one object per op, each with `op` as a
  single-value enum, combined with anyOf) and OP_SCHEMAS by name;
- RECOUNT_SCHEMA (WS7 recount), REVIEW_SCHEMA (WS7 reviewer) and
  REVISE_SCHEMA (WS7 revise);
- CONSOLIDATOR_WRITE_TOOL_ARGS (WS4, kind M only) and
  MEASURE_WRITE_TOOL_ARGS (WS3, kinds D and C): strict args schemas by tool
  name, ready for `ToolSpec.args_schema`;
- REFUSAL_CODES: the structured refusal codes each write tool may return
  (`{refused: true, code, message, detail}`; a refusal never changes state);
- BOQ_ITEM_V2_ASSEMBLY (spec 5.4): the BOQ line v2 shape. It is CODE-SIDE
  ONLY: the consolidator assembles it from tool results, no model is ever
  asked for it, and the legacy MAIN_AGENT_SCHEMA / BOQ_ITEM stay unchanged.

Things the schemas cannot express and code must enforce (WS4, WS7):
group and subgroup vocabulary (refusal `vocab`), EA whole numbers, the
300-character clarification limit (CLARIFICATION_MAX_CHARS), the 40-op cap
per review (MAX_OPS_PER_REVIEW), from_qty matching within 1e-6, and that a
write_brief `value` has the shape of BRIEF_SPEC[field] (brief_value_schema).

No schema here carries a price, cost or rate field. Quantities only.
"""

from __future__ import annotations

from typing import Any

# Shared vocabularies come from the root contract module (spec 5.1, S3);
# re-exported here under their existing names.
from app.services.estimating_contracts import COMBINE_MODES as COMBINE_MODES
from app.services.estimating_contracts import QUANTITY_BASES as QUANTITY_BASES
from app.services.estimating_contracts import WAGE_TYPES as WAGE_TYPES
from app.services.plans_reader_schemas import (
    BOQ_ITEM,
    BOQ_LEVEL0,
    BRIEF_SPEC,
    MATERIAL_CATEGORIES,
    MATERIAL_NOTE,
    UNITS,
    _arr,
    _bool,
    _enum,
    _int,
    _num,
    _obj,
    _str,
)

# Vocabularies -----------------------------------------------------------------

# QUANTITY_BASES (quantity basis of a BOQ line v2, spec 5.1), COMBINE_MODES
# (how a line's quantity is computed from its source rows) and WAGE_TYPES are
# imported from estimating_contracts above.
# Models never claim a human value: `manual` is reserved for human modify or
# override with a user id (spec 5.4), so model-facing ops omit it.
MODEL_QUANTITY_BASES: tuple[str, ...] = tuple(b for b in QUANTITY_BASES if b != "manual")

MODEL_COMBINE_MODES: tuple[str, ...] = tuple(c for c in COMBINE_MODES if c != "manual")

CONDITION_KEYS: tuple[str, ...] = ("wage_type",)

EVIDENCE_TYPES: tuple[str, ...] = ("page_quote", "band", "rule", "recount", "tool")

FINDING_KINDS: tuple[str, ...] = (
    "missing_scope",
    "excluded_scope_carried",
    "quantity_suspect",
    "unit_wrong",
    "price_outlier",
    "band_outlier",
    "spec_conflict",
    "bid_condition",
    "double_count",
    "false_rfi",
    "other",
)
FINDING_SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")
SYSTEM_STATUSES: tuple[str, ...] = ("covered", "missing", "excluded", "unclear")
REVISE_DECISIONS: tuple[str, ...] = ("apply", "modify", "decline")

REMOVE_ROW_REASONS: tuple[str, ...] = (
    "by_others",
    "not_used",
    "policy_exclusion",
    "duplicate",
    "not_in_scope",
)
LINE_FLAGS: tuple[str, ...] = ("excluded", "by_others", "alternate")

ROW_DISPOSITIONS: tuple[str, ...] = (
    "existing_to_remain",
    "existing_circuit",
    "by_others",
    "other_trade",
    "duplicate_view",
    "not_electrical",
    "info_only",
    "superseded_by_schedule",
)

BRIEF_FIELDS: tuple[str, ...] = (
    "project_summary",
    "electrical_scope_summary",
    "systems",
    "other_trades_affecting_electrical",
    "sheet_summaries",
    "spec_highlights",
    "missing_documents",
)

# Limits code enforces (strict structured outputs cannot carry them).
CLARIFICATION_MAX_CHARS = 300
MAX_OPS_PER_REVIEW = 40
FINISH_MAX_CALLS = 3


# Local helpers ----------------------------------------------------------------

def _op_name(name: str) -> dict[str, Any]:
    """The discriminator: a single-value enum (strict mode has no `const`)."""
    return {"type": "string", "enum": [name]}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """A nullable copy of a property schema, following plans_reader_schemas:
    enums become anyOf [enum, null], plain types gain "null"."""
    if "enum" in schema:
        return {"anyOf": [dict(schema), {"type": "null"}]}
    if "anyOf" in schema:
        alts = list(schema["anyOf"])
        if {"type": "null"} not in alts:
            alts.append({"type": "null"})
        return {"anyOf": alts}
    out = dict(schema)
    kind = out.get("type")
    if isinstance(kind, str):
        out["type"] = [kind, "null"]
    elif isinstance(kind, list) and "null" not in kind:
        out["type"] = [*kind, "null"]
    return out


_STR_LIST = _arr(_str())
_POINT = _arr(_num())  # [x, y] as page fractions (spec 5.1)
_SEGMENT = _arr(_POINT)  # [[x, y], [x, y]]

SOURCE_REF = _obj({"ref": _str(), "multiplier": _num()})


# Evidence ---------------------------------------------------------------------

EVIDENCE = _obj(
    {
        "type": _enum(EVIDENCE_TYPES),
        "document_id": _str(True),
        "page": _int(True),
        "quote": _str(True),
        "ref_id": _str(True),
    }
)


# Typed op union (plans_reader_ops.py applies these; all keyed on line_id) ------

OP_SET_QUANTITY = _obj(
    {
        "op": _op_name("set_quantity"),
        "line_id": _str(),
        "from_qty": _num(),
        "to_qty": _num(),
        "basis": _enum(MODEL_QUANTITY_BASES),
        "note": _str(True),
    }
)

OP_SET_UNIT = _obj(
    {
        "op": _op_name("set_unit"),
        "line_id": _str(),
        "from_unit": _enum(UNITS),
        "to_unit": _enum(UNITS),
        "to_qty": _num(),
        "basis": _enum(MODEL_QUANTITY_BASES),
    }
)

OP_ADD_ROW = _obj(
    {
        "op": _op_name("add_row"),
        "area": _str(),
        "section": _enum(BOQ_LEVEL0),
        "group": _str(),
        "subgroup": _str(),
        "description": _str(),
        "qty": _num(),
        "unit": _enum(UNITS),
        "basis": _enum(MODEL_QUANTITY_BASES),
        "sheet_refs": _STR_LIST,
        "item_key": _str(True),
    }
)

OP_REMOVE_ROW = _obj(
    {
        "op": _op_name("remove_row"),
        "line_id": _str(),
        "from_qty": _num(),
        "reason_code": _enum(REMOVE_ROW_REASONS),
    }
)

OP_SET_FLAG = _obj(
    {
        "op": _op_name("set_flag"),
        "line_id": _str(),
        "flag": _enum(LINE_FLAGS),
        "policy_rule_id": _str(True),
    }
)

OP_ADD_ALLOWANCE = _obj(
    {"op": _op_name("add_allowance"), "name": _str(), "reason_code": _str()}
)

OP_REMOVE_ALLOWANCE = _obj(
    {"op": _op_name("remove_allowance"), "name": _str(), "reason_code": _str()}
)

OP_SET_CONDITION = _obj(
    {
        "op": _op_name("set_condition"),
        "key": _enum(CONDITION_KEYS),
        # Unknown wage type is null (spec 5.1), so the current value may be null.
        "from_value": _enum(WAGE_TYPES, nullable=True),
        "to_value": _enum(WAGE_TYPES),
    }
)

OP_ADD_CLARIFICATION = _obj(
    {"op": _op_name("add_clarification"), "text": _str()}  # <= 300 chars, code-checked
)

OP_ADD_RFI = _obj(
    {
        "op": _op_name("add_rfi"),
        "question": _str(),
        "document_id": _str(),
        "page": _int(),
    }
)

OP_SCHEMAS: dict[str, dict[str, Any]] = {
    "set_quantity": OP_SET_QUANTITY,
    "set_unit": OP_SET_UNIT,
    "add_row": OP_ADD_ROW,
    "remove_row": OP_REMOVE_ROW,
    "set_flag": OP_SET_FLAG,
    "add_allowance": OP_ADD_ALLOWANCE,
    "remove_allowance": OP_REMOVE_ALLOWANCE,
    "set_condition": OP_SET_CONDITION,
    "add_clarification": OP_ADD_CLARIFICATION,
    "add_rfi": OP_ADD_RFI,
}
OP_NAMES: tuple[str, ...] = tuple(OP_SCHEMAS.keys())

OP: dict[str, Any] = {"anyOf": list(OP_SCHEMAS.values())}


# WS7 recount, review, revise --------------------------------------------------

RECOUNT_MARK = _obj({"tile_id": _str(), "x": _num(), "y": _num(), "confidence": _num()})
RECOUNT_UNCERTAIN_REGION = _obj(
    {
        "tile_id": _str(),
        "x0": _num(),
        "y0": _num(),
        "x1": _num(),
        "y1": _num(),
        "reason": _str(),
    }
)
RECOUNT_TARGET = _obj(
    {
        "target_id": _str(),
        "marks": _arr(RECOUNT_MARK),
        "uncertain_regions": _arr(RECOUNT_UNCERTAIN_REGION),
        "symbol_seen_in_legend": _bool(),
        "notes": _str(True),
    }
)
RECOUNT_SCHEMA: dict[str, Any] = _obj({"targets": _arr(RECOUNT_TARGET)})

REVIEW_FINDING = _obj(
    {
        "ref": _str(),
        "kind": _enum(FINDING_KINDS),
        "severity": _enum(FINDING_SEVERITIES),
        "title": _str(),
        "detail": _str(),
        "line_ids": _STR_LIST,
        "evidence": _arr(EVIDENCE),
        "proposed_ops": _arr(OP),
        "qty_delta_hint": _num(True),
        "confidence": _num(),
    }
)
SYSTEM_CHECK = _obj(
    {"system": _str(), "status": _enum(SYSTEM_STATUSES), "line_ids": _STR_LIST}
)
REVIEW_SCHEMA: dict[str, Any] = _obj(
    {
        "summary": _str(),
        "findings": _arr(REVIEW_FINDING),
        "systems_checked": _arr(SYSTEM_CHECK),
    }
)

REVISE_DISPOSITION = _obj(
    {
        "finding_id": _str(),
        "decision": _enum(REVISE_DECISIONS),
        "reason": _str(),
        "evidence": _arr(EVIDENCE),
        "ops": _arr(OP),
    }
)
REVISE_SCHEMA: dict[str, Any] = _obj({"dispositions": _arr(REVISE_DISPOSITION)})


# WS4 consolidator write tools (kind M only) -----------------------------------

_ADD_BOQ_LINE_PROPS: dict[str, Any] = {
    "area": _str(),
    "section": _enum(BOQ_LEVEL0),
    "group": _str(),  # vocabulary checked in code (refusal `vocab`)
    "subgroup": _str(),
    "description": _str(),
    "unit": _enum(UNITS),
    "source_refs": _arr(SOURCE_REF),
    "combine": _enum(MODEL_COMBINE_MODES),
    "caveat": _str(True),
    "material_note": _str(True),
    "material_category": _enum(MATERIAL_CATEGORIES),
}

ADD_BOQ_LINE_ARGS = _obj(_ADD_BOQ_LINE_PROPS)

CANDIDATE_OVERRIDE = _obj(
    {
        "id": _str(),
        "description": _str(True),
        "group": _str(True),
        "subgroup": _str(True),
        "caveat": _str(True),
        "material_note": _str(True),
    }
)
ACCEPT_CANDIDATES_ARGS = _obj({"ids": _STR_LIST, "overrides": _arr(CANDIDATE_OVERRIDE)})

# null = unchanged. A field that is already nullable on add (caveat,
# material_note) is cleared with an empty string.
UPDATE_BOQ_LINE_ARGS = _obj(
    {"line_id": _str(), **{k: _nullable(v) for k, v in _ADD_BOQ_LINE_PROPS.items()}}
)

REMOVE_BOQ_LINE_ARGS = _obj({"line_id": _str(), "reason": _str()})

ACCOUNT_ROWS_ARGS = _obj(
    {
        "row_ids": _STR_LIST,
        "disposition": _enum(ROW_DISPOSITIONS),
        "reason": _str(),
        "fact_id": _str(True),
        "duplicate_of": _str(True),
        "note_ref": _str(True),
    }
)

ALLOWANCE_CITE = _obj({"row_id": _str(True), "fact_id": _str(True)})
ADD_ALLOWANCE_ARGS = _obj({"name": _str(), "cite": ALLOWANCE_CITE})

# cite: a row id, fact id, ledger sheet number or spec TOC section.
_CITED_TEXT_ARGS = _obj({"text": _str(), "cite": _str()})

WRITE_BRIEF_ARGS = _obj(
    {
        "field": _enum(BRIEF_FIELDS),
        # The union of the BRIEF_SPEC shapes of the allowed fields; code checks
        # the value against brief_value_schema(field).
        "value": {
            "anyOf": [
                _str(),
                _STR_LIST,
                BRIEF_SPEC["properties"]["systems"],
                BRIEF_SPEC["properties"]["sheet_summaries"],
                BRIEF_SPEC["properties"]["spec_highlights"],
            ]
        },
    }
)

SET_ESTIMATE_NOTES_ARGS = _obj(
    {
        "general_notes": _STR_LIST,
        "area_notes": _arr(_obj({"area": _str(), "notes": _STR_LIST})),
    }
)

SET_MATERIAL_NOTES_ARGS = _obj({"notes": _arr(MATERIAL_NOTE)})

FINISH_ARGS = _obj({"confidence_notes": _STR_LIST})

CONSOLIDATOR_WRITE_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "add_boq_line": ADD_BOQ_LINE_ARGS,
    "accept_candidates": ACCEPT_CANDIDATES_ARGS,
    "update_boq_line": UPDATE_BOQ_LINE_ARGS,
    "remove_boq_line": REMOVE_BOQ_LINE_ARGS,
    "account_rows": ACCOUNT_ROWS_ARGS,
    "add_allowance": ADD_ALLOWANCE_ARGS,
    "add_exclusion": _CITED_TEXT_ARGS,
    "add_assumption": _CITED_TEXT_ARGS,
    "add_rfi": _CITED_TEXT_ARGS,
    "add_risk": _CITED_TEXT_ARGS,
    "write_brief": WRITE_BRIEF_ARGS,
    "set_estimate_notes": SET_ESTIMATE_NOTES_ARGS,
    "set_material_notes": SET_MATERIAL_NOTES_ARGS,
    "finish": FINISH_ARGS,
}


def brief_value_schema(field: str) -> dict[str, Any]:
    """The exact shape a write_brief `value` must have for `field`."""
    if field not in BRIEF_FIELDS:
        raise KeyError(field)
    return BRIEF_SPEC["properties"][field]


# WS3 measure write tools (kinds D and C) --------------------------------------

SET_LINEAR_ITEM_ARGS = _obj(
    {
        "document_id": _str(),
        "page": _int(),
        "tag": _str(),
        "candidate_ids": _STR_LIST,
        # [[[x, y], [x, y]], ...] page fractions, or null to use the candidates.
        "endpoints": _nullable(_arr(_SEGMENT)),
        "view_id": _str(True),
    }
)

SET_PANEL_LOCATION_ARGS = _obj(
    {
        "panel": _str(),
        "document_id": _str(),
        "page": _int(),
        "view_id": _str(True),
        "point": _POINT,
    }
)

MEASURE_WRITE_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "set_linear_item": SET_LINEAR_ITEM_ARGS,
    "set_panel_location": SET_PANEL_LOCATION_ARGS,
}


# Refusal codes ----------------------------------------------------------------

# Every write tool may also refuse `invalid_args` (the args failed the strict
# schema) and `unknown_line` where it takes a line_id. Codes named verbatim in
# spec 5.8 are listed first per tool; the rest name refusals 5.8 describes
# without a code (account_rows, allowances, cites, prose checks, finish, the
# measure writes) and are this contract's names for them.
COMMON_REFUSAL_CODES: tuple[str, ...] = ("invalid_args",)

_LINE_REFUSALS: tuple[str, ...] = (
    "unknown_ref",
    "unverifiable_ref",
    "superseded_ref",
    "unit_mismatch",
    "ref_consumed",
    "partial_requires_floor",
    "excluded_by_fact",
    "vocab",
    "conduit_form", "luminaire_tag",
)
_PROSE_REFUSALS: tuple[str, ...] = ("brief_numbers", "pricing_terms", "capability_denial")
_CITE_REFUSALS: tuple[str, ...] = ("unresolved_cite",)

REFUSAL_CODES: dict[str, tuple[str, ...]] = {
    "add_boq_line": _LINE_REFUSALS,
    "accept_candidates": (*_LINE_REFUSALS, "unknown_candidate"),
    "update_boq_line": (*_LINE_REFUSALS, "unknown_line"),
    "remove_boq_line": ("unknown_line",),
    "account_rows": (
        "unknown_ref",
        "duplicate_view_requires_duplicate_of",
        "by_others_unverified",
        "existing_circuit_uncited",
        "unit_mismatch",
    ),
    "add_allowance": ("excluded_by_fact", "excluded_by_policy", *_CITE_REFUSALS),
    "add_exclusion": _CITE_REFUSALS,
    "add_assumption": _CITE_REFUSALS,
    "add_rfi": (*_CITE_REFUSALS, "decided_by_schedule"),
    "add_risk": _CITE_REFUSALS,
    "write_brief": (*_PROSE_REFUSALS, "brief_value_shape"),
    "set_estimate_notes": _PROSE_REFUSALS,
    "set_material_notes": ("vocab",),
    "finish": ("finish_limit",),
    "set_linear_item": ("writer_role_required", "unknown_document", "unknown_page", "unknown_candidate"),
    "set_panel_location": ("writer_role_required", "unknown_document", "unknown_page", "unknown_panel"),
}

# Keys of a structured refusal; `refused` is always true and `detail` is an
# object whose keys depend on the code (e.g. the consuming line for ref_consumed).
REFUSAL_KEYS: tuple[str, ...] = ("refused", "code", "message", "detail")


# BOQ line v2 (spec 5.4): CODE-SIDE ONLY ----------------------------------------

# Never offered to a model and never part of MAIN_AGENT_SCHEMA; the
# consolidator assembles lines in this shape and the renderer ignores the
# extra keys. `combine: manual` and `basis: manual` are allowed only for a
# human modify or override with a user id.
BOQ_ITEM_V2_ASSEMBLY: dict[str, Any] = _obj(
    {
        **BOQ_ITEM["properties"],
        "line_id": _str(),
        "source_refs": _arr(SOURCE_REF),
        "combine": _enum(COMBINE_MODES),
        "basis": _enum(QUANTITY_BASES),
        "needs_recount": _bool(),
    }
)
