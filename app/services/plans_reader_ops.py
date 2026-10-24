"""Typed review ops applied to a BOQ spec (docs/AI_ESTIMATING_BUILD_SPEC.md
5.8 OP table and the WS7 evidence rules).

`apply_ops(boq_spec, ops) -> (spec, applied, rejected)` is pure: it never
touches the database, never calls a model, and returns a deep copy of the
spec with the accepted ops applied. Every op is keyed on the BOQ `line_id`
(`B<n>`); the ops of one finding apply all or none.

Preconditions (5.8 table), checked before anything is written:

- set_quantity: the line exists and `from_qty` equals the current quantity
  within 1e-6; EA quantities stay whole numbers.
- set_unit: the current unit equals `from_unit`, `to_unit` is in UNITS, and
  `to_qty` comes from a measurement tool result or a verified quote: the
  finding's evidence carries a measurement or quote signal AND one of the
  quantities those tool results returned (`FindingEvidence.quantities`,
  filled by the reviewer from the group's tool-call trace) equals `to_qty`
  within UNIT_QTY_TOLERANCE (relative 2%). Without that evidence, or with
  no quantity on record (fail closed), the line stays and the finding
  routes to a human.
- add_row: section, group and subgroup in the BOQ vocabulary; EA whole numbers.
- remove_row: the line exists and `from_qty` matches.
- set_flag: the line exists.
- add_allowance, remove_allowance: the allowance vocabulary or a
  policy-approved name (`allowance_names`).
- set_condition: the current value equals `from_value`.
- add_clarification: at most CLARIFICATION_MAX_CHARS characters.
- add_rfi: the page exists on the document (`pages_of`).

Evidence rules (WS7): a decrease (lower quantity, a removed row, an
excluded or by-others flag) needs two independent signals or a policy hit;
an addition (higher quantity, a new row) needs a verified quote, a recount
raise, a census floor or a measurement tool result; a unit change never
applies without a measured or quoted quantity; a wage change needs a
verified statute quote (NRS 338, prevailing wage, Davis-Bacon, public
works); at most MAX_OPS_PER_REVIEW ops per review; a total move over
LARGE_REVISION_SHARE of the priced total adds the `large_revision` flag.

Signals are code-derived by the reviewer (`plans_reader_reviewer
.evidence_signals`) from verified evidence and handed in per finding as
`FindingEvidence`; this module only counts them. Ops that are not BOQ
line edits (allowances, the wage condition, clarifications, RFIs) land in
the spec as data keys the renderer ignores (`review_allowances`,
`review_conditions`, `review_rfis`) or as `closing_notes`.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services import plans_reader_rows as rows_mod
from app.services.electrical_items import is_linear, parse_item
from app.services.estimating_contracts import BOQ_LINE_PREFIX, is_row_id
from app.services.plans_reader_review_schemas import (
    CLARIFICATION_MAX_CHARS,
    CONDITION_KEYS,
    LINE_FLAGS,
    MAX_OPS_PER_REVIEW,
    MODEL_QUANTITY_BASES,
    OP_NAMES,
    REMOVE_ROW_REASONS,
    WAGE_TYPES,
)
from app.services.plans_reader_schemas import (
    BOQ_GROUPS,
    BOQ_LEVEL0,
    BOQ_SUBGROUPS,
    ESTIMATE_ALLOWANCES,
    MATERIAL_CATEGORIES,
    UNITS,
)

OPS_VERSION = "ws7-ops-2"
QTY_TOLERANCE = 1e-6
# set_unit: to_qty must equal a measured or quoted quantity of the finding's
# verified tool evidence within this RELATIVE tolerance (2% of the larger of
# the two; a model may round 43.46 FT to 43.5, never 43.46 to 60).
UNIT_QTY_TOLERANCE = 0.02
LARGE_REVISION_SHARE = 0.15
LARGE_REVISION_FLAG = "large_revision"
REVIEW_ORIGIN = "review"
DEFAULT_MATERIAL_CATEGORY = "General Material"

# Signal names (the reviewer derives them from verified evidence).
SIGNAL_PAGE_QUOTE = "page_quote"
SIGNAL_BAND = "band"
SIGNAL_RULE = "rule"
SIGNAL_RECOUNT = "recount"
SIGNAL_RECOUNT_RAISE = "recount_raise"
SIGNAL_CENSUS_FLOOR = "census_floor"
SIGNAL_MEASUREMENT = "measurement"
SIGNAL_QUOTE = "quote"  # a verified vendor quote (tool result)
SIGNAL_POLICY = "policy"
SIGNAL_STATUTE_QUOTE = "statute_quote"
SIGNALS: tuple[str, ...] = (
    SIGNAL_PAGE_QUOTE, SIGNAL_BAND, SIGNAL_RULE, SIGNAL_RECOUNT, SIGNAL_RECOUNT_RAISE, SIGNAL_CENSUS_FLOOR,
    SIGNAL_MEASUREMENT, SIGNAL_QUOTE, SIGNAL_POLICY, SIGNAL_STATUTE_QUOTE,
)
ADDITION_SIGNALS: frozenset[str] = frozenset({SIGNAL_PAGE_QUOTE, SIGNAL_QUOTE, SIGNAL_RECOUNT_RAISE,
                                               SIGNAL_CENSUS_FLOOR, SIGNAL_MEASUREMENT})
UNIT_CHANGE_SIGNALS: frozenset[str] = frozenset({SIGNAL_MEASUREMENT, SIGNAL_QUOTE})
DECREASE_FLAGS: frozenset[str] = frozenset({"excluded", "by_others"})

# Rejection codes.
REJECT_UNKNOWN_OP = "unknown_op"
REJECT_INVALID_ARGS = "invalid_args"
REJECT_UNKNOWN_LINE = "unknown_line"
REJECT_QTY_MISMATCH = "qty_mismatch"
REJECT_UNIT_MISMATCH = "unit_mismatch"
REJECT_VOCAB = "vocab"
REJECT_EA_WHOLE = "ea_whole_number"
REJECT_LINEAR_UNIT = "linear_in_ea"
REJECT_EVIDENCE_DECREASE = "decrease_needs_two_signals"
REJECT_EVIDENCE_ADDITION = "addition_needs_evidence"
REJECT_EVIDENCE_UNIT = "unit_change_needs_measurement"
REJECT_EVIDENCE_UNIT_QTY = "unit_qty_not_measured"
REJECT_EVIDENCE_WAGE = "wage_needs_statute_quote"
REJECT_CONDITION_MISMATCH = "condition_mismatch"
REJECT_ALLOWANCE_VOCAB = "allowance_vocab"
REJECT_CLARIFICATION_LENGTH = "clarification_too_long"
REJECT_UNKNOWN_PAGE = "unknown_page"
REJECT_OP_CAP = "op_cap"
REJECT_FINDING_GROUP = "finding_all_or_none"
REJECT_NO_CHANGE = "no_change"


@dataclass
class FindingEvidence:
    """What the reviewer verified for one finding: the distinct signals
    (SIGNALS), whether any policy rule fired on it, and the quantities the
    measurement and quote tool results of its verified evidence returned
    (`quantities`, the only values a set_unit `to_qty` may take)."""

    signals: frozenset[str] = frozenset()
    policy_hit: bool = False
    notes: tuple[str, ...] = ()
    quantities: tuple[float, ...] = ()

    @property
    def independent(self) -> int:
        """Independent signals: a recount raise and a plain recount count once."""
        s = set(self.signals)
        if SIGNAL_RECOUNT_RAISE in s:
            s.discard(SIGNAL_RECOUNT)
        return len(s)

    def has(self, *names: str) -> bool:
        return any(n in self.signals for n in names)

    def quantity_matches(self, value: float | None, *, tolerance: float = UNIT_QTY_TOLERANCE) -> bool:
        """True when `value` equals one of the measured or quoted quantities
        within the relative `tolerance`; False with no quantity on record."""
        if value is None or not math.isfinite(value):
            return False
        for q in self.quantities:
            q = _f(q)
            if q is None:
                continue
            if abs(value - q) <= tolerance * max(abs(value), abs(q), 1e-9):
                return True
        return False


@dataclass
class Rejection:
    op: dict[str, Any]
    finding_id: str | None
    code: str
    message: str
    route: str = "human"

    def to_dict(self) -> dict[str, Any]:
        return {"op": dict(self.op), "finding_id": self.finding_id, "code": self.code,
                "message": self.message, "route": self.route}


@dataclass
class Applied:
    op: dict[str, Any]
    finding_id: str | None
    line_id: str | None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"op": dict(self.op), "finding_id": self.finding_id, "line_id": self.line_id,
                "before": dict(self.before), "after": dict(self.after)}


class _Reject(Exception):
    def __init__(self, code: str, message: str, route: str = "human") -> None:
        super().__init__(message)
        self.code = code
        self.route = route


# Helpers ---------------------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _near(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= QTY_TOLERANCE


def _is_whole(value: float) -> bool:
    return abs(value - round(value)) <= QTY_TOLERANCE


def _str(value: Any) -> str:
    return str(value or "").strip()


def _item_at(spec: dict[str, Any], path: str) -> tuple[list[dict[str, Any]], int]:
    """The items list and index a `rows_mod.iter_boq_lines` path points at."""
    parts = path.replace("]", "").split(".")
    node: Any = spec
    for part in parts[:-1]:
        name, idx = part.split("[")
        node = node[name][int(idx)]
    name, idx = parts[-1].split("[")
    return node[name], int(idx)


def _index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """line_id -> the iter_boq_lines row (with its path) of the spec."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows_mod.iter_boq_lines(spec):
        out[str(row["line_id"])] = row
    return out


def _stamp_ids(spec: dict[str, Any]) -> None:
    """Give legacy items the transient ids iter_boq_lines assigns, so the
    ops' line ids resolve to the same items on the next read."""
    for row in rows_mod.iter_boq_lines(spec):
        items, i = _item_at(spec, row["path"])
        if not is_row_id(items[i].get("line_id"), BOQ_LINE_PREFIX):
            items[i]["line_id"] = row["line_id"]


def _qty_of(item: Mapping[str, Any]) -> float | None:
    for key in ("quantity", "qty"):
        q = _f(item.get(key))
        if q is not None:
            return q
    return None


def _flags_of(item: Mapping[str, Any]) -> list[str]:
    return [str(f) for f in item.get("flags") or [] if f]


def _ensure_list(spec: dict[str, Any], key: str) -> list[Any]:
    value = spec.get(key)
    if not isinstance(value, list):
        value = []
        spec[key] = value
    return value


def _subgroup_ok(group: str, subgroup: str) -> bool:
    allowed = BOQ_SUBGROUPS.get(group)
    if not allowed:
        return bool(subgroup)
    return subgroup in allowed


def _material_category(group: str) -> str:
    table = {
        "WIRING": "General Material",
        "SWITCHGEAR": "Switchgear",
        "OTHER ITEMS": "General Material",
        "LIGHTING FIXTURES": "Lighting",
        "LIGHTING CONTROLS": "Lighting",
        "LOW VOLTAGE WORK": "Low Voltage",
        "SOLAR WORK": "Solar",
        "LIGHTING POLE WORK": "Lighting",
        "NV ENERGY": "Other",
        "ALTERNATES": "Other",
    }
    out = table.get(group, DEFAULT_MATERIAL_CATEGORY)
    return out if out in MATERIAL_CATEGORIES else DEFAULT_MATERIAL_CATEGORY


def _find_or_make(spec: dict[str, Any], area: str, section: str, group: str, subgroup: str) -> list[dict[str, Any]]:
    areas = _ensure_list(spec, "areas")
    a = next((x for x in areas if isinstance(x, dict) and _str(x.get("area")).upper() == area.upper()), None)
    if a is None:
        a = {"area": area, "notes": [], "sections": []}
        areas.append(a)
    sections = _ensure_list(a, "sections")
    s = next((x for x in sections if isinstance(x, dict) and _str(x.get("heading")).upper() == section.upper()), None)
    if s is None:
        s = {"heading": section, "groups": []}
        sections.append(s)
    groups = _ensure_list(s, "groups")
    g = next((x for x in groups if isinstance(x, dict) and _str(x.get("name")).upper() == group.upper()), None)
    if g is None:
        g = {"name": group, "subgroups": []}
        groups.append(g)
    subs = _ensure_list(g, "subgroups")
    u = next((x for x in subs if isinstance(x, dict) and _str(x.get("name")).upper() == subgroup.upper()), None)
    if u is None:
        u = {"name": subgroup, "items": []}
        subs.append(u)
    return _ensure_list(u, "items")


def is_decrease(op: Mapping[str, Any]) -> bool:
    name = op.get("op")
    if name == "set_quantity":
        f, t = _f(op.get("from_qty")), _f(op.get("to_qty"))
        return f is not None and t is not None and t < f - QTY_TOLERANCE
    if name == "remove_row":
        return True
    if name == "set_flag":
        return str(op.get("flag")) in DECREASE_FLAGS
    return False


def is_addition(op: Mapping[str, Any]) -> bool:
    name = op.get("op")
    if name == "set_quantity":
        f, t = _f(op.get("from_qty")), _f(op.get("to_qty"))
        return f is not None and t is not None and t > f + QTY_TOLERANCE
    return name == "add_row"


# Validation -----------------------------------------------------------------------------------


def _check_evidence(op: Mapping[str, Any], ev: FindingEvidence) -> None:
    name = op.get("op")
    if name == "set_unit":
        if not ev.has(*UNIT_CHANGE_SIGNALS):
            raise _Reject(REJECT_EVIDENCE_UNIT, "a unit change needs a measured or quoted quantity; the row stays")
        if not ev.quantity_matches(_f(op.get("to_qty"))):
            raise _Reject(REJECT_EVIDENCE_UNIT_QTY, "to_qty is not a quantity the measurement or quote tool results "
                          f"returned (within {UNIT_QTY_TOLERANCE:.0%}); the row stays")
    if name == "set_condition" and not ev.has(SIGNAL_STATUTE_QUOTE):
        raise _Reject(REJECT_EVIDENCE_WAGE, "a wage change needs a verified statute quote (NRS 338, prevailing wage, "
                      "Davis-Bacon or public works)")
    if is_decrease(op) and not (ev.policy_hit or ev.has(SIGNAL_POLICY) or ev.independent >= 2):
        raise _Reject(REJECT_EVIDENCE_DECREASE, "a decrease needs two independent signals or a policy hit")
    if is_addition(op) and not ev.has(*ADDITION_SIGNALS):
        raise _Reject(REJECT_EVIDENCE_ADDITION, "an addition needs a verified quote, a recount raise, a census floor "
                      "or a measurement tool result")


def _validate(op: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]], spec: Mapping[str, Any], *,
              allowance_names: frozenset[str], pages_of: Mapping[str, int] | None,
              conditions: Mapping[str, Any]) -> None:
    name = op.get("op")
    if name not in OP_NAMES:
        raise _Reject(REJECT_UNKNOWN_OP, f"unknown op {name!r}")
    if name in ("set_quantity", "set_unit", "remove_row", "set_flag"):
        lid = _str(op.get("line_id"))
        row = index.get(lid)
        if row is None:
            raise _Reject(REJECT_UNKNOWN_LINE, f"line {lid or '?'} is not in the BOQ")
        current = _qty_of(row)
        if name == "set_quantity":
            f, t = _f(op.get("from_qty")), _f(op.get("to_qty"))
            if f is None or t is None or t < 0:
                raise _Reject(REJECT_INVALID_ARGS, "set_quantity needs numeric from_qty and to_qty")
            if not _near(f, current):
                raise _Reject(REJECT_QTY_MISMATCH, f"line {lid} is {current:g}, not {f:g}")
            if _near(f, t):
                raise _Reject(REJECT_NO_CHANGE, "to_qty equals from_qty")
            if str(row.get("unit")) == "EA" and not _is_whole(t):
                raise _Reject(REJECT_EA_WHOLE, "EA quantities are whole numbers")
            if _str(op.get("basis")) not in MODEL_QUANTITY_BASES:
                raise _Reject(REJECT_INVALID_ARGS, "basis is not a quantity basis")
        elif name == "set_unit":
            if _str(op.get("to_unit")) not in UNITS or _str(op.get("from_unit")) not in UNITS:
                raise _Reject(REJECT_INVALID_ARGS, "units must be in UNITS")
            if _str(row.get("unit")) != _str(op.get("from_unit")):
                raise _Reject(REJECT_UNIT_MISMATCH, f"line {lid} is in {row.get('unit')}, not {op.get('from_unit')}")
            t = _f(op.get("to_qty"))
            if t is None or t <= 0:
                raise _Reject(REJECT_INVALID_ARGS, "set_unit needs a positive to_qty")
            if _str(op.get("to_unit")) == "EA" and not _is_whole(t):
                raise _Reject(REJECT_EA_WHOLE, "EA quantities are whole numbers")
            ident = parse_item(str(row.get("description") or ""), _str(op.get("to_unit")))
            if is_linear(ident) and _str(op.get("to_unit")) != "FT":
                raise _Reject(REJECT_LINEAR_UNIT, "a linear item is always FT")
        elif name == "remove_row":
            f = _f(op.get("from_qty"))
            if not _near(f, current):
                raise _Reject(REJECT_QTY_MISMATCH, f"line {lid} is {current:g}, not {f if f is not None else '?'}")
            if _str(op.get("reason_code")) not in REMOVE_ROW_REASONS:
                raise _Reject(REJECT_INVALID_ARGS, "reason_code is not a remove reason")
        elif name == "set_flag":
            if _str(op.get("flag")) not in LINE_FLAGS:
                raise _Reject(REJECT_INVALID_ARGS, "flag is not a line flag")
            if _str(op.get("flag")) in _flags_of(row):
                raise _Reject(REJECT_NO_CHANGE, f"line {lid} already carries {op.get('flag')}")
        return
    if name == "add_row":
        section, group, subgroup = _str(op.get("section")), _str(op.get("group")), _str(op.get("subgroup"))
        if section not in BOQ_LEVEL0 or group not in BOQ_GROUPS or not _subgroup_ok(group, subgroup):
            raise _Reject(REJECT_VOCAB, "section, group or subgroup is outside the BOQ vocabulary")
        if _str(op.get("unit")) not in UNITS:
            raise _Reject(REJECT_INVALID_ARGS, "unit must be in UNITS")
        q = _f(op.get("qty"))
        if q is None or q <= 0:
            raise _Reject(REJECT_INVALID_ARGS, "add_row needs a positive qty")
        if _str(op.get("unit")) == "EA" and not _is_whole(q):
            raise _Reject(REJECT_EA_WHOLE, "EA quantities are whole numbers")
        if not _str(op.get("description")) or not _str(op.get("area")):
            raise _Reject(REJECT_INVALID_ARGS, "add_row needs an area and a description")
        if _str(op.get("basis")) not in MODEL_QUANTITY_BASES:
            raise _Reject(REJECT_INVALID_ARGS, "basis is not a quantity basis")
        ident = parse_item(_str(op.get("description")), _str(op.get("unit")))
        if is_linear(ident) and _str(op.get("unit")) != "FT":
            raise _Reject(REJECT_LINEAR_UNIT, "a linear item is always FT")
        return
    if name in ("add_allowance", "remove_allowance"):
        if _str(op.get("name")).upper() not in allowance_names:
            raise _Reject(REJECT_ALLOWANCE_VOCAB, "the allowance name is not in the vocabulary or the policy")
        if not _str(op.get("reason_code")):
            raise _Reject(REJECT_INVALID_ARGS, "a reason_code is required")
        return
    if name == "set_condition":
        key = _str(op.get("key"))
        if key not in CONDITION_KEYS or _str(op.get("to_value")) not in WAGE_TYPES:
            raise _Reject(REJECT_INVALID_ARGS, "set_condition takes key wage_type and a wage type value")
        current = conditions.get(key)
        if (current or None) != (op.get("from_value") or None):
            raise _Reject(REJECT_CONDITION_MISMATCH, f"{key} is {current!r}, not {op.get('from_value')!r}")
        if op.get("from_value") == op.get("to_value"):
            raise _Reject(REJECT_NO_CHANGE, "to_value equals from_value")
        return
    if name == "add_clarification":
        text = _str(op.get("text"))
        if not text:
            raise _Reject(REJECT_INVALID_ARGS, "add_clarification needs text")
        if len(text) > CLARIFICATION_MAX_CHARS:
            raise _Reject(REJECT_CLARIFICATION_LENGTH, f"at most {CLARIFICATION_MAX_CHARS} characters")
        return
    if name == "add_rfi":
        if not _str(op.get("question")) or not _str(op.get("document_id")):
            raise _Reject(REJECT_INVALID_ARGS, "add_rfi needs a question and a document")
        page = op.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise _Reject(REJECT_INVALID_ARGS, "page must be a positive integer")
        if pages_of is not None:
            n = pages_of.get(_str(op.get("document_id")))
            if n is None or page > int(n):
                raise _Reject(REJECT_UNKNOWN_PAGE, f"page {page} does not exist on that document")
        return


# Application ----------------------------------------------------------------------------------


def _apply_one(spec: dict[str, Any], op: Mapping[str, Any], index: dict[str, dict[str, Any]],
               seq: rows_mod.RowSeq, finding_id: str | None) -> Applied:
    name = op.get("op")
    stamp = {"op": name, "finding_id": finding_id, "origin": REVIEW_ORIGIN}
    if name in ("set_quantity", "set_unit", "remove_row", "set_flag"):
        lid = _str(op.get("line_id"))
        row = index[lid]
        items, i = _item_at(spec, row["path"])
        item = items[i]
        before = {"quantity": _qty_of(item), "unit": item.get("unit"), "flags": _flags_of(item)}
        if name == "set_quantity":
            item["quantity"] = float(op["to_qty"]) if not _is_whole(float(op["to_qty"])) else int(round(float(op["to_qty"])))
            item["basis"] = op.get("basis")
            note = _str(op.get("note"))
            if note:
                item["caveat"] = (f"{_str(item.get('caveat'))} " if _str(item.get("caveat")) else "") + f"(REVIEW: {note[:120]})"
        elif name == "set_unit":
            item["unit"] = op["to_unit"]
            item["quantity"] = float(op["to_qty"]) if not _is_whole(float(op["to_qty"])) else int(round(float(op["to_qty"])))
            item["basis"] = op.get("basis")
        elif name == "remove_row":
            removed = items.pop(i)
            removed_log = _ensure_list(spec, "review_removed_lines")
            removed_log.append({**stamp, "line_id": lid, "reason_code": op.get("reason_code"), "item": removed})
            index.pop(lid, None)
            return Applied(op=dict(op), finding_id=finding_id, line_id=lid, before=before, after={"removed": True})
        elif name == "set_flag":
            flags = _flags_of(item)
            flags.append(str(op["flag"]))
            item["flags"] = flags
            if op.get("policy_rule_id"):
                item["policy_rule_id"] = op["policy_rule_id"]
        item["review_op"] = stamp
        # keep the index fresh for a later op of the same review
        index[lid] = {**row, **{k: item.get(k) for k in ("quantity", "unit", "flags")}}
        after = {"quantity": _qty_of(item), "unit": item.get("unit"), "flags": _flags_of(item)}
        return Applied(op=dict(op), finding_id=finding_id, line_id=lid, before=before, after=after)
    if name == "add_row":
        group = _str(op.get("group"))
        items = _find_or_make(spec, _str(op.get("area")), _str(op.get("section")), group, _str(op.get("subgroup")))
        lid = seq.next(BOQ_LINE_PREFIX)
        q = float(op["qty"])
        ident = parse_item(_str(op.get("description")), _str(op.get("unit")))
        item = {
            "line_id": lid,
            "description": _str(op.get("description")),
            "quantity": int(round(q)) if _is_whole(q) else q,
            "unit": op["unit"],
            "sheet_refs": [str(s) for s in op.get("sheet_refs") or []],
            "caveat": None,
            "material_note": f"Added by the independent review ({op.get('basis')})",
            "material_category": _material_category(group),
            "wastage_applies": bool(is_linear(ident)),
            "is_accessory": False,
            "basis": op.get("basis"),
            "item_key": op.get("item_key"),
            "review_op": stamp,
        }
        items.append(item)
        index[lid] = {**item, "area": op.get("area"), "section": op.get("section"), "group": group,
                      "subgroup": op.get("subgroup"), "path": ""}
        return Applied(op=dict(op), finding_id=finding_id, line_id=lid, before={}, after={"quantity": q, "unit": op["unit"]})
    if name in ("add_allowance", "remove_allowance"):
        log = _ensure_list(spec, "review_allowances")
        log.append({**stamp, "name": _str(op.get("name")).upper(), "reason_code": op.get("reason_code")})
        return Applied(op=dict(op), finding_id=finding_id, line_id=None, after={"name": _str(op.get("name")).upper()})
    if name == "set_condition":
        conds = spec.get("review_conditions")
        if not isinstance(conds, dict):
            conds = {}
            spec["review_conditions"] = conds
        conds[str(op["key"])] = {"from": op.get("from_value"), "to": op.get("to_value"), **stamp}
        return Applied(op=dict(op), finding_id=finding_id, line_id=None,
                       before={"value": op.get("from_value")}, after={"value": op.get("to_value")})
    if name == "add_clarification":
        notes = _ensure_list(spec, "closing_notes")
        text = _str(op.get("text"))
        if text not in notes:
            notes.append(text)
        return Applied(op=dict(op), finding_id=finding_id, line_id=None, after={"text": text})
    if name == "add_rfi":
        log = _ensure_list(spec, "review_rfis")
        log.append({**stamp, "question": _str(op.get("question")), "document_id": _str(op.get("document_id")),
                    "page": int(op["page"])})
        return Applied(op=dict(op), finding_id=finding_id, line_id=None, after={"question": _str(op.get("question"))})
    raise _Reject(REJECT_UNKNOWN_OP, f"unknown op {name!r}")


def total_move(applied: Sequence[Applied], line_values: Mapping[str, float] | None, total_usd: float | None) -> float | None:
    """The share of the priced total the applied ops move (sum of absolute
    extended-value deltas over the total), None without unit values or a
    positive total. `line_values` maps line_id to the extended USD of the
    line BEFORE the ops (unit value = extended / quantity)."""
    if not line_values or not total_usd or total_usd <= 0:
        return None
    moved = 0.0
    for a in applied:
        if not a.line_id or a.line_id not in line_values:
            continue
        ext = _f(line_values[a.line_id]) or 0.0
        before_q, after_q = _f(a.before.get("quantity")), _f(a.after.get("quantity"))
        if a.after.get("removed") or "excluded" in (a.after.get("flags") or []) or "by_others" in (a.after.get("flags") or []):
            moved += abs(ext)
        elif before_q and after_q is not None and before_q > 0:
            moved += abs(ext / before_q * (after_q - before_q))
    return moved / float(total_usd)


def apply_ops(
    boq_spec: Mapping[str, Any],
    ops: Iterable[Mapping[str, Any]],
    *,
    evidence: Mapping[str, FindingEvidence] | None = None,
    allowance_names: Iterable[str] = (),
    pages_of: Mapping[str, int] | None = None,
    conditions: Mapping[str, Any] | None = None,
    line_values: Mapping[str, float] | None = None,
    total_usd: float | None = None,
    max_ops: int = MAX_OPS_PER_REVIEW,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply typed ops to a copy of the BOQ spec. Each op may carry a
    `finding_id`; the ops of one finding apply all or none, in order.
    `evidence` maps finding ids to what the reviewer verified (an op without
    a finding, or whose finding has no entry, has no signals). Returns the
    new spec, the applied ops and the rejected ops (each with a code and a
    message). The new spec carries `review_revision` with the counts and the
    `large_revision` flag when the ops move more than LARGE_REVISION_SHARE
    of the priced total."""
    spec = copy.deepcopy(dict(boq_spec))
    _stamp_ids(spec)
    index = _index(spec)
    seq = rows_mod.RowSeq()
    for lid in index:
        seq.observe(lid)
    names = frozenset(_str(n).upper() for n in (*ESTIMATE_ALLOWANCES, *allowance_names) if _str(n))
    ev = dict(evidence or {})
    conds = dict(conditions or {})
    applied: list[Applied] = []
    rejected: list[Rejection] = []
    ordered = [dict(o) for o in ops if isinstance(o, Mapping)]
    if len(ordered) > max_ops:
        for extra in ordered[max_ops:]:
            rejected.append(Rejection(extra, extra.get("finding_id"), REJECT_OP_CAP,
                                      f"at most {max_ops} ops per review"))
        ordered = ordered[:max_ops]
    # group by finding, keeping first-seen order; ops without a finding stand alone
    groups: list[tuple[str | None, list[dict[str, Any]]]] = []
    by_finding: dict[str, list[dict[str, Any]]] = {}
    for i, op in enumerate(ordered):
        fid = _str(op.get("finding_id")) or None
        if fid is None:
            groups.append((None, [op]))
            continue
        if fid not in by_finding:
            by_finding[fid] = []
            groups.append((fid, by_finding[fid]))
        by_finding[fid].append(op)
    for fid, group_ops in groups:
        trial = copy.deepcopy(spec)
        trial_index = _index(trial)
        trial_seq = rows_mod.RowSeq(seq.as_dict())
        trial_applied: list[Applied] = []
        failure: tuple[dict[str, Any], _Reject] | None = None
        fe = ev.get(fid or "", FindingEvidence())
        for op in group_ops:
            clean = {k: v for k, v in op.items() if k != "finding_id"}
            try:
                _validate(clean, trial_index, trial, allowance_names=names, pages_of=pages_of, conditions=conds)
                _check_evidence(clean, fe)
                trial_applied.append(_apply_one(trial, clean, trial_index, trial_seq, fid))
                if clean.get("op") == "set_condition":
                    conds[str(clean["key"])] = clean.get("to_value")
            except _Reject as exc:
                failure = (op, exc)
                break
        if failure is not None:
            bad, exc = failure
            rejected.append(Rejection({k: v for k, v in bad.items() if k != "finding_id"}, fid, exc.code, str(exc), exc.route))
            for other in group_ops:
                if other is bad:
                    continue
                rejected.append(Rejection({k: v for k, v in other.items() if k != "finding_id"}, fid, REJECT_FINDING_GROUP,
                                          f"another op of finding {fid or '?'} was rejected ({exc.code})"))
            continue
        spec, index, seq = trial, trial_index, trial_seq
        applied.extend(trial_applied)
    move = total_move(applied, line_values, total_usd)
    flags = [LARGE_REVISION_FLAG] if move is not None and move > LARGE_REVISION_SHARE else []
    spec["review_revision"] = {
        "version": OPS_VERSION,
        "applied": len(applied),
        "rejected": len(rejected),
        "move_share": None if move is None else round(move, 4),
        "flags": flags,
    }
    return spec, [a.to_dict() for a in applied], [r.to_dict() for r in rejected]


__all__ = [
    "OPS_VERSION", "LARGE_REVISION_SHARE", "LARGE_REVISION_FLAG", "UNIT_QTY_TOLERANCE", "SIGNALS", "ADDITION_SIGNALS",
    "UNIT_CHANGE_SIGNALS", "FindingEvidence", "Applied", "Rejection", "apply_ops", "is_decrease", "is_addition",
    "total_move",
    "SIGNAL_PAGE_QUOTE", "SIGNAL_BAND", "SIGNAL_RULE", "SIGNAL_RECOUNT", "SIGNAL_RECOUNT_RAISE",
    "SIGNAL_CENSUS_FLOOR", "SIGNAL_MEASUREMENT", "SIGNAL_QUOTE", "SIGNAL_POLICY", "SIGNAL_STATUTE_QUOTE",
    "REJECT_UNKNOWN_OP", "REJECT_INVALID_ARGS", "REJECT_UNKNOWN_LINE", "REJECT_QTY_MISMATCH", "REJECT_UNIT_MISMATCH",
    "REJECT_VOCAB", "REJECT_EA_WHOLE", "REJECT_LINEAR_UNIT", "REJECT_EVIDENCE_DECREASE", "REJECT_EVIDENCE_ADDITION",
    "REJECT_EVIDENCE_UNIT", "REJECT_EVIDENCE_UNIT_QTY", "REJECT_EVIDENCE_WAGE", "REJECT_CONDITION_MISMATCH",
    "REJECT_ALLOWANCE_VOCAB",
    "REJECT_CLARIFICATION_LENGTH", "REJECT_UNKNOWN_PAGE", "REJECT_OP_CAP", "REJECT_FINDING_GROUP", "REJECT_NO_CHANGE",
]
