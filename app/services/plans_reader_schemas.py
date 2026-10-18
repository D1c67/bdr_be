"""Shared JSON contracts for AI Estimating (docs/PLANS_READER.md).

Every schema here is OpenAI strict structured-output compatible: objects list
every property as required and forbid additional properties; optional values
are nullable types. Four consumers depend on these shapes and must not drift:

- services/plans_reader_agents.py builds the sub, roll-up, escalation and main
  prompts around SUB_AGENT_SCHEMA and MAIN_AGENT_SCHEMA;
- services/plans_reader_render.py renders MAIN_AGENT_SCHEMA['boq'],
  'estimate', 'markups', 'marked_plans' and 'brief' into files;
- services/plans_reader_chat.py exposes the stored outputs through tools and
  asks for a replacement of ONE deliverable spec (DELIVERABLE_SPEC_SCHEMAS);
- the frontend renders the same shapes read-only.

Pages are addressed as (doc_index, page): `doc_index` is the 1-based position
of the document in the group's prompt ("D1", "D2", ...), `page` is the 1-based
page within that document. The app maps them back to plans_reader_documents.
Coordinates on a sheet are fractions of the page width and height (0..1,
origin top-left) so they survive any render ppi.

No schema carries a price, cost or rate field. Quantities only.
"""

from __future__ import annotations

from typing import Any

# Vocabularies -----------------------------------------------------------------

CATEGORIES: tuple[str, ...] = (
    "general_drawings",
    "civil_drawings",
    "structural_drawings",
    "architectural_drawings",
    "mechanical_drawings",
    "plumbing_drawings",
    "electrical_drawings",
    "fire_protection_drawings",
    "low_voltage_drawings",
    "specifications",
    "addenda",
    "rfp",
    "other",
)

PAGE_ROLES: tuple[str, ...] = (
    "cover_index",
    "legend_symbols",
    "general_notes",
    "schedule",
    "plan",
    "detail",
    "diagram",
    "site",
    "spec_text",
    "other",
)

UNITS: tuple[str, ...] = ("EA", "FT", "CU FT", "SQ FT", "LS", "CY")

# The G3 material categories as the RFQ flow names them (material_categories).
MATERIAL_CATEGORIES: tuple[str, ...] = (
    "General Material",
    "Switchgear",
    "Generator & Equipment",
    "Lighting",
    "Low Voltage",
    "Trenching",
    "EV Chargers",
    "Demolition",
    "Solar",
    "Other",
)

# The BOQ template tree the estimators deliver (docs/PLANS_READER.md 3.6).
BOQ_LEVEL0: tuple[str, ...] = ("DEMOLITION", "NEW WORK")
BOQ_GROUPS: tuple[str, ...] = (
    "WIRING",
    "SWITCHGEAR",
    "OTHER ITEMS",
    "LIGHTING FIXTURES",
    "LIGHTING CONTROLS",
    "LOW VOLTAGE WORK",
    "SOLAR WORK",
    "LIGHTING POLE WORK",
    "NV ENERGY",
    "ALTERNATES",
)
BOQ_SUBGROUPS: dict[str, tuple[str, ...]] = {
    "WIRING": (
        "CONDUCTORS (AWG)",
        "CONDUITS",
        "OTHER CONDUIT ACCESSORIES",
        "UNDERGROUND CONDUITS",
        "SAW CUT",
        "GROUNDING",
    ),
    "SWITCHGEAR": ("PANELS", "DISCONNECT SWITCHES", "CIRCUIT BREAKERS", "TRANSFORMERS", "OTHER GEAR"),
    "OTHER ITEMS": (
        "RECEPTACLES (RECEPTACLES INCLUDES 4\"S DEEP COMB BOX AND COVER PLATE)",
        "CONTROLS",
        "MISCELLANEOUS",
        "POLE W/ ACCESSORIES",
        "FIRE ALARM ROUGHINS",
    ),
    "LIGHTING FIXTURES": ("FIXTURES",),
    "LIGHTING CONTROLS": ("CONTROLS",),
    "LOW VOLTAGE WORK": ("LOW VOLTAGE WIRING", "OUTLETS/DEVICES", "CAMERAS", "RACK DETAILS", "PUBLIC ADDRESS SYSTEM"),
}
WIRING_WASTAGE_NOTE = (
    "(10% wastage is included in the quantities of all conductors/cables, and conduits in this sheet)"
)
LOW_VOLTAGE_NOTE = "(OUTLETS/DEVICES INCLUDES 4\" S BACKBOX, MUD RINGS AND COVER PLATES)"
STANDARD_REVIEW_NOTE = (
    "We recommend thoroughly reviewing the entire project to identify any necessary "
    "adjustments to scope of work if required."
)
ESTIMATE_ALLOWANCES: tuple[str, ...] = (
    "MOBILIZATION AND DEMOBILIZATION",
    "ARC FLASH STUDY",
    "PROTECTION DEVICES COORDINATION STUDY",
    "PERMIT AND UTILITY FEES",
    "TESTING AND COMMISSIONING",
    "TAGS & DIRECTORY",
    "AS BUILT DOCUMENTS",
    "CORE DRILL",
    "CAULKING SEALANT",
)
CAVEATS: tuple[str, ...] = (
    "(ASSUMED)",
    "(NEED TO CONFIRM SCOPE OF WORK)",
    "(NEED TO CONFIRM DETAILS)",
    "(RATING ASSUMED)",
    "(BREAKERS DETAILS NOT GIVEN)",
    "(BY OTHERS, NEED TO CONFIRM SCOPE)",
)


# Strict-schema helpers --------------------------------------------------------

def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(props.keys()),
        "properties": props,
    }


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _str(nullable: bool = False) -> dict[str, Any]:
    return {"type": ["string", "null"] if nullable else "string"}


def _num(nullable: bool = False) -> dict[str, Any]:
    return {"type": ["number", "null"] if nullable else "number"}


def _int(nullable: bool = False) -> dict[str, Any]:
    return {"type": ["integer", "null"] if nullable else "integer"}


def _bool() -> dict[str, Any]:
    return {"type": "boolean"}


def _enum(values: tuple[str, ...], nullable: bool = False) -> dict[str, Any]:
    if nullable:
        return {"anyOf": [{"type": "string", "enum": list(values)}, {"type": "null"}]}
    return {"type": "string", "enum": list(values)}


_STR_LIST = _arr(_str())
_POINT = _arr(_num())  # [x, y] as page fractions
_POINTS = _arr(_POINT)

_PAGE_REF = {"doc_index": _int(), "page": _int(), "sheet_number": _str(True)}


# Sub agent (one group of one category) ----------------------------------------

RESOLUTION_FLAG = _obj(
    {
        "doc_index": _int(),
        "page": _int(),
        # A 3x3 grid cell "r1c1".."r3c3", "whole", or "x0,y0,x1,y1" fractions.
        "region": _str(),
        "reason": _str(),
    }
)

SUB_PAGE = _obj(
    {
        **_PAGE_REF,
        "title": _str(True),
        "page_role": _enum(PAGE_ROLES),
        "scale": _str(True),
        "tags": _STR_LIST,
        "notes": _STR_LIST,
        "resolution_flags": _arr(RESOLUTION_FLAG),
    }
)

COUNT_ITEM = _obj(
    {
        "description": _str(),
        "symbol_or_tag": _str(True),
        "quantity": _num(),
        "unit": _enum(UNITS),
        **_PAGE_REF,
        "location": _str(True),
        "basis": _enum(("counted_symbols", "schedule_text", "keyed_note", "stated_quantity", "inferred")),
        "confidence": _num(),
        "material_category_hint": _enum(MATERIAL_CATEGORIES, nullable=True),
        "notes": _str(True),
    }
)

RUN_ITEM = _obj(
    {
        "description": _str(),
        "conduit_size": _str(True),
        "conduit_type": _str(True),
        "conductors": _str(True),
        "from_location": _str(True),
        "to_location": _str(True),
        "length_ft": _num(),
        "basis": _enum(("stated", "scaled_from_drawing", "assumed")),
        "underground": _bool(),
        **_PAGE_REF,
        "confidence": _num(),
        "notes": _str(True),
    }
)

SCOPE_ITEM = _obj(
    {
        "statement": _str(),
        "relevance": _enum(("electrical", "other_trade_affecting_electrical", "other")),
        **_PAGE_REF,
        "section": _str(True),
        "quote": _str(True),
    }
)

SPEC_ITEM = _obj(
    {
        "item": _str(),
        "manufacturer": _str(True),
        "basis_of_design": _str(True),
        "approved_equals": _STR_LIST,
        "substitution_rule": _str(True),
        "vendor_or_organization_requirements": _str(True),
        "material_category_hint": _enum(MATERIAL_CATEGORIES, nullable=True),
        "section": _str(True),
        **_PAGE_REF,
    }
)

SCHEDULE_ITEM = _obj(
    {
        "schedule_type": _enum(
            ("panel", "fixture", "equipment_connection", "feeder", "mechanical_equipment", "fire_alarm", "low_voltage", "other")
        ),
        "name": _str(),
        **_PAGE_REF,
        "columns": _STR_LIST,
        "rows": _arr(_STR_LIST),
        "notes": _str(True),
    }
)

DETAIL_ITEM = _obj(
    {
        "detail_ref": _str(),
        "title": _str(True),
        "electrical_relevance": _str(),
        **_PAGE_REF,
    }
)

QUESTION_ITEM = _obj({"question": _str(), "why": _str(True), **_PAGE_REF})

SUB_AGENT_SCHEMA: dict[str, Any] = _obj(
    {
        "category": _enum(CATEGORIES),
        "summary": _str(),
        "pages": _arr(SUB_PAGE),
        "counts": _arr(COUNT_ITEM),
        "runs": _arr(RUN_ITEM),
        "scope": _arr(SCOPE_ITEM),
        "specs": _arr(SPEC_ITEM),
        "schedules": _arr(SCHEDULE_ITEM),
        "details": _arr(DETAIL_ITEM),
        "questions": _arr(QUESTION_ITEM),
    }
)

# A roll-up (merging several groups of one category) and an escalation patch
# return the same shape as a sub agent group.
ROLLUP_SCHEMA = SUB_AGENT_SCHEMA
ESCALATION_SCHEMA = SUB_AGENT_SCHEMA


# Main agent -------------------------------------------------------------------

BOQ_ITEM = _obj(
    {
        "description": _str(),
        "quantity": _num(),
        "unit": _enum(UNITS),
        "sheet_refs": _STR_LIST,
        "caveat": _str(True),
        "material_note": _str(True),
        "material_category": _enum(MATERIAL_CATEGORIES),
        "wastage_applies": _bool(),
        "is_accessory": _bool(),
    }
)

BOQ_SUBGROUP = _obj({"name": _str(), "note": _str(True), "items": _arr(BOQ_ITEM)})
BOQ_GROUP = _obj({"name": _str(), "note": _str(True), "subgroups": _arr(BOQ_SUBGROUP)})
BOQ_SECTION = _obj({"heading": _enum(BOQ_LEVEL0), "note": _str(True), "groups": _arr(BOQ_GROUP)})
BOQ_AREA = _obj({"area": _str(), "notes": _STR_LIST, "sections": _arr(BOQ_SECTION)})

BOQ_SPEC = _obj(
    {
        "title": _str(),
        "subtitle": _str(True),
        "areas": _arr(BOQ_AREA),
        "closing_notes": _STR_LIST,
    }
)

ESTIMATE_SPEC = _obj(
    {
        "project_name": _str(),
        "address": _str(True),
        "include_standard_allowances": _bool(),
        "extra_allowances": _STR_LIST,
        "general_notes": _STR_LIST,
        "area_notes": _arr(_obj({"area": _str(), "notes": _STR_LIST})),
    }
)

MARKUP_LINE = _obj({"points": _POINTS, "length_ft": _num(True), "label": _str(True)})
MARKUP_SHEET = _obj(
    {
        "kind": _enum(("trench", "sawcut")),
        **_PAGE_REF,
        "label": _str(),
        "lines": _arr(MARKUP_LINE),
        "notes": _str(True),
    }
)
MARKUPS_SPEC = _obj({"sheets": _arr(MARKUP_SHEET)})

LEGEND_ROW = _obj(
    {
        "prefix": _str(),
        "description": _str(),
        "quantity": _num(),
        "unit": _enum(UNITS),
    }
)
PLAN_MARK = _obj(
    {
        "legend_index": _int(),
        "kind": _enum(("dot", "polyline")),
        "points": _POINTS,
    }
)
MARKED_SHEET = _obj(
    {
        "area": _str(),
        **_PAGE_REF,
        "legend": _arr(LEGEND_ROW),
        "marks": _arr(PLAN_MARK),
    }
)
MARKED_PLANS_SPEC = _obj({"sheets": _arr(MARKED_SHEET)})

BRIEF_SPEC = _obj(
    {
        "project_summary": _str(),
        "electrical_scope_summary": _str(),
        "systems": _arr(
            _obj(
                {
                    "system": _str(),
                    "in_scope": _enum(("yes", "no", "partial", "unclear")),
                    "notes": _str(True),
                }
            )
        ),
        "assumptions": _STR_LIST,
        "exclusions": _STR_LIST,
        "risks": _arr(_obj({"risk": _str(), "severity": _enum(("low", "medium", "high")), "source": _str(True)})),
        "rfis": _arr(_obj({"question": _str(), "source": _str(True)})),
        "missing_documents": _STR_LIST,
        "other_trades_affecting_electrical": _STR_LIST,
        "sheet_summaries": _arr(_obj({"sheet_number": _str(), "title": _str(True), "summary": _str()})),
        "spec_highlights": _arr(_obj({"section": _str(), "title": _str(True), "summary": _str()})),
    }
)

MATERIAL_NOTE = _obj(
    {
        "material_category": _enum(MATERIAL_CATEGORIES),
        "notes": _STR_LIST,
        "specified_manufacturers": _STR_LIST,
        "lead_time_items": _STR_LIST,
    }
)

MAIN_AGENT_SCHEMA: dict[str, Any] = _obj(
    {
        "brief": BRIEF_SPEC,
        "boq": BOQ_SPEC,
        "estimate": ESTIMATE_SPEC,
        "markups": MARKUPS_SPEC,
        "marked_plans": MARKED_PLANS_SPEC,
        "material_notes": _arr(MATERIAL_NOTE),
        "confidence_notes": _STR_LIST,
    }
)

# Deliverable kind -> the spec the renderer consumes and a modify call rewrites.
DELIVERABLE_SPEC_SCHEMAS: dict[str, dict[str, Any]] = {
    "boq": BOQ_SPEC,
    # The estimate workbook is rendered from the BOQ spec plus ESTIMATE_SPEC;
    # a modify call on the estimate receives and returns both.
    "estimate": _obj({"boq": BOQ_SPEC, "estimate": ESTIMATE_SPEC}),
    "trench_markup": MARKUPS_SPEC,
    "sawcut_markup": MARKUPS_SPEC,
    "marked_plans": MARKED_PLANS_SPEC,
    "brief": BRIEF_SPEC,
}

DELIVERABLE_KINDS: tuple[str, ...] = tuple(DELIVERABLE_SPEC_SCHEMAS.keys())

# Chat -------------------------------------------------------------------------

# Tool definitions the chat exposes (Responses API function tools). The app
# serves every tool; read_pages and modify_deliverable are gated by Pro Max
# and by the spend cap respectively (services/plans_reader_chat.py).
CHAT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_brief",
        "description": "The main agent's deep brief for this run.",
        "parameters": _obj({}),
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_documents",
        "description": "Every document in the run with its category, name, page range and page roles.",
        "parameters": _obj({}),
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_output",
        "description": "The stored sub agent output for one category (counts, runs, scope, specs, schedules, page notes), or the main output.",
        "parameters": _obj({"category": _enum(CATEGORIES + ("main",))}),
        "strict": True,
    },
    {
        "type": "function",
        "name": "search_outputs",
        "description": "Case-insensitive text search across all stored outputs; returns matching items with their category, document and page.",
        "parameters": _obj({"query": _str(), "limit": _int()}),
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_deliverable",
        "description": "The JSON spec of a generated deliverable (latest version unless a version is given).",
        "parameters": _obj({"kind": _enum(DELIVERABLE_KINDS), "version": _int(True)}),
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_pages",
        "description": "Pro Max only: look at the actual rendered pages of a document (up to the per-message cap).",
        "parameters": _obj({"document_id": _str(), "pages": _arr(_int()), "ppi": _int(True)}),
        "strict": True,
    },
    {
        "type": "function",
        "name": "modify_deliverable",
        "description": "Rewrite one deliverable per the user's instruction; the app re-renders the file as a new version.",
        "parameters": _obj({"kind": _enum(DELIVERABLE_KINDS), "instruction": _str()}),
        "strict": True,
    },
]


def validate_strict(schema: dict[str, Any]) -> None:
    """Raise ValueError if `schema` breaks the strict-output rules (every object
    lists all properties as required and forbids extras). Used by tests."""

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties", {})
            if node.get("additionalProperties") is not False:
                raise ValueError(f"{path}: additionalProperties must be false")
            if set(node.get("required", [])) != set(props.keys()):
                raise ValueError(f"{path}: required must list every property")
            for k, v in props.items():
                walk(v, f"{path}.{k}")
        if "items" in node:
            walk(node["items"], f"{path}[]")
        for alt in node.get("anyOf", []):
            walk(alt, f"{path}|")

    walk(schema, "$")
