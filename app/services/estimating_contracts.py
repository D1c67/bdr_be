"""Day-1 contracts of the AI Estimating build (docs/AI_ESTIMATING_BUILD_SPEC.md).

Setup step S3 makes this module one of the roots of the dependency graph:
every workstream imports the shapes defined here instead of a sibling's
implementation, so each cross-workstream edge is satisfied by a contract plus
a fake first and by the real module later. Section numbers below refer to the
build spec.

What lives here:

- the shared vocabularies of 5.1 and 5.4 (wage type, quantity basis, price
  quality, row id prefixes, LINEAR_CLASSES, gate ids, finding enums), each a
  tuple of strings plus a validator;
- the two WS0 artifact types, `bdr.priced_estimate/1` and `bdr.gate_report/1`,
  with validators that enforce the invariants of 5.4 (bid total equals the sum
  of the ten Verify numbers to the cent, `refused` when the status is blocked
  or a gate blocks, the hours_source and material_source mappings);
- the acceptance Check registry of 3.5: the pass thresholds of E1 to E25 and
  GC1 to GC4 are data in CHECK_SPECS and a registration can never change them
  (real-project dollar figures are reference keys, see below);
- GateFinding (the `plans_reader_gate_findings` row shape) with a
  deterministic fingerprint, the `record_findings(sb, run, findings, stage)`
  protocol and an in-memory default;
- Fact, the `facts_for_run(sb, run_id)` protocol, ExcludedLine and the
  `pricing_allowed(run) -> (bool, reasons)` protocol.

Rules: this module imports the standard library plus
`app.services.electrical_items` (the one home of LINEAR_CLASSES and
LINEAR_LUMINAIRE_WORDS, spec 5.1; it imports only plans_reader_schemas, so
there is no cycle). Nothing here imports pricing_types, plans_reader_tools or
plans_reader_review_schemas: those import their shared vocabularies from this
module, which stays the root. It never touches a database, a model or the
network, and it carries no real pricing data.

Reference thresholds. A threshold value that is a real-project dollar figure
(a 7154 vendor quote amount, a figure derived from the 7154 estimator or its
quotes) is never written here. CHECK_SPECS carries a reference dict
`{"ref": "<key>"}` in its place, and the WS0 acceptance module calls
`resolve_reference_thresholds(spec, references)` at registration with the
values loaded from `EVAL_ROOT/references/7154/expected.json`. That file is a
JSON object; each key below is looked up first as a flat key, then as a dotted
path into nested objects (so `{"e9": {"lowest_eligible_quote_pretax_usd": ...}}`
works too). The keys (REFERENCE_KEYS):

- `e9.lowest_eligible_quote_pretax_usd`: LIGHTING FIXTURES pre-tax target of
  the lowest eligible vendor quote (Q9), number in USD;
- `e9.alternate_quote_pretax_usd`: the pre-tax target when the alternate
  vendor selection is supplied, number in USD;
- `e13.duplex_unit_material_min_usd`: the duplex unit material floor
  (0.7 x the spec-grade duplex price), number in USD;
- `e13.gfci_unit_material_min_usd`: the GFCI unit material floor (0.7 x the
  spec-grade GFCI price), number in USD.

The Q5 policy labor rate of E4 is a policy number, not project data, and
stays inline.

Wage spelling. The needs_review value for an unknown wage type is spelled
`wage_unknown` (5.1, WAGE_UNKNOWN_REVIEW) while the gate code WS6 raises at
stage pricing for it is `wage_type_unknown` (3.6, in GATE_IDS). Both spellings
are intentional; do not merge them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol, runtime_checkable

# Re-exported: the one definition lives in electrical_items (spec 5.1).
from app.services.electrical_items import LINEAR_CLASSES as LINEAR_CLASSES
from app.services.electrical_items import LINEAR_LUMINAIRE_WORDS as LINEAR_LUMINAIRE_WORDS


class ContractError(ValueError):
    """A value or artifact does not satisfy an AI Estimating contract."""


# Enum helper ------------------------------------------------------------------


def validate_enum(value: Any, allowed: Sequence[str], name: str) -> str:
    """Return `value` when it is one of `allowed`, else raise ContractError."""
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{name} must be one of {', '.join(allowed)}; got {value!r}")
    return value


# Wage type (5.1) ----------------------------------------------------------------

# The `projects.wage_type` values. Unknown is None plus needs_review wage_unknown.
WAGE_TYPES: tuple[str, ...] = ("prevailing_wage", "non_prevailing_wage")
WAGE_UNKNOWN_REVIEW = "wage_unknown"


def validate_wage_type(value: Any, *, allow_none: bool = True) -> str | None:
    """A wage type, or None (unknown) when `allow_none`."""
    if value is None and allow_none:
        return None
    return validate_enum(value, WAGE_TYPES, "wage_type")


# Quantity basis (5.1) -------------------------------------------------------------

QUANTITY_BASES: tuple[str, ...] = (
    "counted",
    "schedule",
    "measured",
    "assumed_route",
    "stated",
    "census_floor",
    "policy",
    "mixed",
    "manual",
)

# Measure-internal basis (WS3) and how it maps onto the line basis: the first
# maps to `measured`, the rest to `assumed_route` with caveat (ASSUMED).
MEASURE_BASES: tuple[str, ...] = (
    "measured_route",
    "assumed_route",
    "assumed_route_untagged",
    "assumed_allowance",
    "assumed_density",
)
ASSUMED_CAVEAT = "(ASSUMED)"


def validate_quantity_basis(value: Any, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    return validate_enum(value, QUANTITY_BASES, "quantity_basis")


def validate_measure_basis(value: Any) -> str:
    return validate_enum(value, MEASURE_BASES, "measure basis")


def line_basis_for_measure(measure_basis: str) -> tuple[str, str | None]:
    """(line basis, caveat) for a WS3 measure-internal basis."""
    validate_measure_basis(measure_basis)
    if measure_basis == "measured_route":
        return "measured", None
    return "assumed_route", ASSUMED_CAVEAT


# Price quality (5.1) and pricing internals (5.4) -------------------------------

PRICE_QUALITIES: tuple[str, ...] = ("strong", "good", "weak", "policy", "quote", "missing")

# PricedEstimate.status (WS6 pricing_types).
PRICED_STATUSES: tuple[str, ...] = ("priced", "provisional", "blocked")

# ValueStat.method (WS6 pricing_types).
VALUE_STAT_METHODS: tuple[str, ...] = (
    "exact",
    "compatible",
    "context",
    "seed",
    "quote",
    "fallback",
    "none",
)

# PricedLine.material_source (WS6): these plain values, or `quote:<id>` which
# carries the quote id (a bare "quote" is never valid). pricing_types imports
# this tuple from here.
PRICED_LINE_MATERIAL_SOURCES: tuple[str, ...] = ("library", "seed", "fallback", "none")
QUOTE_SOURCE_PREFIX = "quote:"


def validate_price_quality(value: Any) -> str:
    return validate_enum(value, PRICE_QUALITIES, "price quality")


def validate_priced_status(value: Any) -> str:
    return validate_enum(value, PRICED_STATUSES, "priced estimate status")


def validate_value_stat_method(value: Any) -> str:
    return validate_enum(value, VALUE_STAT_METHODS, "ValueStat.method")


def validate_priced_line_material_source(value: Any) -> str:
    """`library | seed | quote:<id> | fallback | none`; returns the value."""
    if isinstance(value, str) and value.startswith(QUOTE_SOURCE_PREFIX):
        if not value[len(QUOTE_SOURCE_PREFIX) :].strip():
            raise ContractError("PricedLine.material_source quote:<id> needs an id")
        return value
    if value == "quote":
        raise ContractError("PricedLine.material_source quote needs the form quote:<id>")
    return validate_enum(value, PRICED_LINE_MATERIAL_SOURCES, "PricedLine.material_source")


# Row ids (5.1) ------------------------------------------------------------------

# Run-unique, never reused. Priced lines, review ops, evidence marks and the
# WS0 artifact all key on the BOQ line id (B<n>).
ROW_ID_PREFIXES: dict[str, str] = {
    "C": "count",
    "R": "run",
    "S": "schedule",
    "P": "spec",
    "O": "scope",
    "D": "detail",
    "Q": "question",
    "M": "measure",
    "F": "fact",
    "B": "boq_line",
    "K": "candidate",
}
BOQ_LINE_PREFIX = "B"
FACT_PREFIX = "F"
_ROW_ID_RE = re.compile(r"^([A-Z])([1-9][0-9]*)$")


def make_row_id(prefix: str, n: int) -> str:
    """Canonical row id: prefix plus a positive integer without leading zeros."""
    if prefix not in ROW_ID_PREFIXES:
        raise ContractError(f"unknown row id prefix {prefix!r}")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ContractError(f"row id number must be a positive int; got {n!r}")
    return f"{prefix}{n}"


def parse_row_id(value: Any) -> tuple[str, int]:
    """(prefix, n) of a canonical row id such as "B12"; raises ContractError."""
    if not isinstance(value, str):
        raise ContractError(f"row id must be a string; got {value!r}")
    m = _ROW_ID_RE.match(value)
    if not m or m.group(1) not in ROW_ID_PREFIXES:
        raise ContractError(f"not a row id: {value!r}")
    return m.group(1), int(m.group(2))


def validate_row_id(value: Any, prefix: str | None = None) -> str:
    got, _n = parse_row_id(value)
    if prefix is not None and got != prefix:
        raise ContractError(f"row id {value!r} must start with {prefix}")
    return value


def is_row_id(value: Any, prefix: str | None = None) -> bool:
    try:
        validate_row_id(value, prefix)
    except ContractError:
        return False
    return True


# Linear classes (5.1) -------------------------------------------------------------

# LINEAR_CLASSES (always FT; an EA row in one of these classes blocks, gate
# linear_in_ea) and LINEAR_LUMINAIRE_WORDS (FT only on evidence, otherwise EA
# with a warning) are defined once in app.services.electrical_items and
# imported at the top of this module.


def validate_item_class_unit(item_class: str, unit: str) -> None:
    """Raise when a linear class carries a unit other than FT."""
    if item_class in LINEAR_CLASSES and unit != "FT":
        raise ContractError(f"{item_class} is a linear class and must be FT; got {unit!r}")


# Category output v2 and BOQ line v2 vocabularies (5.4) --------------------------

ROW_COVERAGES: tuple[str, ...] = (
    "sheet_total",
    "region_subtotal",
    "tile_subtotal",
    "partial",
    "schedule",
    "stated",
    "derived",
    "unknown",
)
ROW_STATUSES: tuple[str, ...] = ("active", "superseded", "alternate", "unverifiable")
COMBINE_MODES: tuple[str, ...] = (
    "sum",
    "max",
    "single",
    "schedule_value",
    "conductor_footage",
    "floor",
    "manual",
)
TEXT_LAYER_QUALITIES: tuple[str, ...] = ("full", "none", "sparse", "vector_text")


def validate_row_coverage(value: Any) -> str:
    return validate_enum(value, ROW_COVERAGES, "coverage")


def validate_row_status(value: Any) -> str:
    return validate_enum(value, ROW_STATUSES, "row status")


def validate_combine_mode(value: Any) -> str:
    return validate_enum(value, COMBINE_MODES, "combine")


def validate_text_layer_quality(value: Any) -> str:
    return validate_enum(value, TEXT_LAYER_QUALITIES, "text_layer_quality")


# Artifacts (WS0) -----------------------------------------------------------------

PRICED_ESTIMATE_SCHEMA = "bdr.priced_estimate/1"
GATE_REPORT_SCHEMA = "bdr.gate_report/1"
ARTIFACT_TYPES: tuple[str, ...] = (PRICED_ESTIMATE_SCHEMA, GATE_REPORT_SCHEMA)

# Artifact names inside a run bundle or a replay.
ARTIFACT_NAMES: tuple[str, ...] = (
    "documents",
    "ledger",
    "groups",
    "category:<cat>",
    "main",
    "boq_spec",
    "estimate_spec",
    "brief_spec",
    "marked_plans_spec",
    "measurements",
    "scope_decisions",
    "priced_estimate",
    "gate_report",
    "chat_probe",
)

# The ten Verify figures, in routers/pricing.py VERIFY_NUMBERS order. Copied
# here so the contract imports nothing; a test pins it to the router tuple.
VERIFY_KEYS: tuple[str, ...] = (
    "labor_amount",
    "materials_amount",
    "gear_amount",
    "underground_amount",
    "low_voltage_amount",
    "labor_markup_amount",
    "materials_markup_amount",
    "gear_markup_amount",
    "underground_markup_amount",
    "low_voltage_markup_amount",
)

HOURS_SOURCES: tuple[str, ...] = ("library", "policy", "manual")
# Artifact-level `material_source` of a bdr.priced_estimate/1 line (5.4). Not
# the PricedLine enum (PRICED_LINE_MATERIAL_SOURCES); material_source_for maps
# one onto the other.
ARTIFACT_MATERIAL_SOURCES: tuple[str, ...] = (
    "library",
    "policy",
    "vendor_quote",
    "allowance",
    "manual",
    "none",
)
# Deprecated alias of ARTIFACT_MATERIAL_SOURCES.
MATERIAL_SOURCES = ARTIFACT_MATERIAL_SOURCES


def validate_hours_source(value: Any, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    return validate_enum(value, HOURS_SOURCES, "hours_source")


def validate_material_source(value: Any) -> str:
    return validate_enum(value, ARTIFACT_MATERIAL_SOURCES, "material_source")


_HOURS_FROM_METHOD: dict[str, str | None] = {
    "exact": "library",
    "compatible": "library",
    "context": "library",
    "fallback": "library",
    "seed": "policy",
    "none": None,
}


def hours_source_for(method: str, *, manual: bool = False) -> str | None:
    """Artifact `hours_source` from the labor ValueStat.method (5.4).

    exact, compatible, context, fallback -> library; seed -> policy; a human or
    override value -> manual; none -> None (the line is `priced: false`).
    """
    if manual:
        return "manual"
    validate_value_stat_method(method)
    if method not in _HOURS_FROM_METHOD:
        raise ContractError(f"labor ValueStat.method {method!r} has no hours_source mapping")
    return _HOURS_FROM_METHOD[method]


def material_source_for(
    priced_line_source: str, *, allowance: bool = False, manual: bool = False
) -> tuple[str, str | None]:
    """Artifact (`material_source`, `quote_ref`) from PricedLine.material_source (5.4).

    library, fallback -> library; seed -> policy; quote:<id> -> vendor_quote with
    quote_ref <id>; allowance rows -> allowance; human value -> manual; none -> none.
    """
    if manual:
        return "manual", None
    if allowance:
        return "allowance", None
    validate_priced_line_material_source(priced_line_source)
    if priced_line_source.startswith(QUOTE_SOURCE_PREFIX):
        return "vendor_quote", priced_line_source[len(QUOTE_SOURCE_PREFIX) :]
    mapping = {"library": "library", "fallback": "library", "seed": "policy", "none": "none"}
    return mapping[priced_line_source], None


def refused_for_status(status: str) -> bool:
    """`refused` is true exactly when the priced status is `blocked`."""
    return validate_priced_status(status) == "blocked"


# Number helpers -------------------------------------------------------------------

_CENT = Decimal("0.01")


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _to_cents(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def verify_sum(verify: Mapping[str, Any]) -> Decimal:
    """Sum of the ten Verify figures to the cent, None counted as 0."""
    total = Decimal("0")
    for key in VERIFY_KEYS:
        value = verify.get(key)
        if value is not None:
            total += Decimal(str(value))
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)


def bid_total_matches_verify(bid_total_usd: Any, verify: Mapping[str, Any]) -> bool:
    """The 5.4 invariant: bid_total_usd == sum(verify) to the cent."""
    if not _is_number(bid_total_usd):
        return False
    return _to_cents(bid_total_usd) == verify_sum(verify)


# Small field checkers that append to an error list.


def _need_keys(obj: Mapping[str, Any], keys: Iterable[str], where: str, errors: list[str]) -> bool:
    missing = [k for k in keys if k not in obj]
    if missing:
        errors.append(f"{where}: missing {', '.join(missing)}")
        return False
    return True


def _num(obj: Mapping[str, Any], key: str, where: str, errors: list[str], *, null: bool) -> None:
    value = obj.get(key)
    if value is None:
        if not null:
            errors.append(f"{where}.{key}: required number")
        return
    if not _is_number(value):
        errors.append(f"{where}.{key}: must be a finite number; got {value!r}")


def _str(obj: Mapping[str, Any], key: str, where: str, errors: list[str], *, null: bool) -> None:
    value = obj.get(key)
    if value is None:
        if not null:
            errors.append(f"{where}.{key}: required string")
        return
    if not isinstance(value, str):
        errors.append(f"{where}.{key}: must be a string; got {value!r}")


def _bool(obj: Mapping[str, Any], key: str, where: str, errors: list[str]) -> None:
    if not isinstance(obj.get(key), bool):
        errors.append(f"{where}.{key}: must be a bool; got {obj.get(key)!r}")


def _enum(
    obj: Mapping[str, Any],
    key: str,
    allowed: Sequence[str],
    where: str,
    errors: list[str],
    *,
    null: bool,
) -> None:
    value = obj.get(key)
    if value is None and null:
        return
    if not isinstance(value, str) or value not in allowed:
        errors.append(f"{where}.{key}: must be one of {', '.join(allowed)}; got {value!r}")


def _str_list(obj: Mapping[str, Any], key: str, where: str, errors: list[str]) -> None:
    value = obj.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where}.{key}: must be a list of strings")


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# bdr.priced_estimate/1 --------------------------------------------------------------

PRICED_ESTIMATE_TOP_KEYS: tuple[str, ...] = (
    "schema",
    "refused",
    "refusal_reasons",
    "wage_type",
    "labor_rate_usd",
    "labor_rate_basis",
    "lines",
    "allowances",
    "quotes",
    "sections",
    "totals",
    "exclusions",
    "clarifications",
    "metadata",
)
PRICED_LINE_KEYS: tuple[str, ...] = (
    "line_id",
    "section",
    "group",
    "subgroup",
    "description",
    "kind",
    "unit",
    "qty_base",
    "wastage",
    "qty",
    "quantity_basis",
    "unit_hours",
    "unit_material_usd",
    "hours_source",
    "material_source",
    "n",
    "p25",
    "p75",
    "quote_ref",
    "priced",
    "excluded",
    "exclusion_reason",
)
QUOTE_KEYS: tuple[str, ...] = (
    "category",
    "vendor",
    "amount_usd",
    "tax_included",
    "replaces_sections",
    "file_sha256",
)
TOTALS_KEYS: tuple[str, ...] = (
    "hours_raw",
    "hours",
    "hour_roundup",
    "labor_usd",
    "material_pretax_usd",
    "tax_rate_pct",
    "material_usd",
    "verify",
    "bid_total_usd",
)
METADATA_KEYS: tuple[str, ...] = ("excluded_project_ids_sha256", "library_fingerprint")


def _priced_line_errors(line: Any, i: int, errors: list[str], seen: set[str]) -> None:
    where = f"lines[{i}]"
    if not isinstance(line, Mapping):
        errors.append(f"{where}: must be an object")
        return
    if not _need_keys(line, PRICED_LINE_KEYS, where, errors):
        return
    line_id = line.get("line_id")
    if not is_row_id(line_id, BOQ_LINE_PREFIX):
        errors.append(f"{where}.line_id: must be a BOQ line id B<n>; got {line_id!r}")
    elif line_id in seen:
        errors.append(f"{where}.line_id: duplicate {line_id}")
    else:
        seen.add(line_id)
    for key in ("section", "group", "subgroup", "description", "unit"):
        _str(line, key, where, errors, null=False)
    _str(line, "kind", where, errors, null=True)
    for key in ("qty_base", "wastage", "qty", "unit_hours", "unit_material_usd", "p25", "p75"):
        _num(line, key, where, errors, null=True)
    n = line.get("n")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        errors.append(f"{where}.n: must be a non-negative int or null; got {n!r}")
    _enum(line, "quantity_basis", QUANTITY_BASES, where, errors, null=True)
    _enum(line, "hours_source", HOURS_SOURCES, where, errors, null=True)
    _enum(line, "material_source", ARTIFACT_MATERIAL_SOURCES, where, errors, null=False)
    _str(line, "quote_ref", where, errors, null=True)
    _bool(line, "priced", where, errors)
    _bool(line, "excluded", where, errors)
    _str(line, "exclusion_reason", where, errors, null=True)
    if line.get("material_source") == "vendor_quote" and not line.get("quote_ref"):
        errors.append(f"{where}.quote_ref: required when material_source is vendor_quote")
    if line.get("priced") is True:
        if line.get("unit_hours") is None or line.get("unit_material_usd") is None:
            errors.append(f"{where}: priced lines need unit_hours and unit_material_usd")
        if line.get("hours_source") is None:
            errors.append(f"{where}.hours_source: required on a priced line")
    unit, kind = line.get("unit"), line.get("kind")
    if isinstance(kind, str) and kind in LINEAR_CLASSES and unit != "FT":
        errors.append(f"{where}: kind {kind} is a linear class and must be FT; got {unit!r}")


def _totals_errors(totals: Any, refused: Any, errors: list[str]) -> None:
    if not isinstance(totals, Mapping):
        errors.append("totals: must be an object")
        return
    if not _need_keys(totals, TOTALS_KEYS, "totals", errors):
        return
    for key in TOTALS_KEYS:
        if key not in ("verify", "bid_total_usd"):
            _num(totals, key, "totals", errors, null=True)
    verify = totals.get("verify")
    bid = totals.get("bid_total_usd")
    if verify is None or bid is None:
        if refused is not True:
            errors.append("totals: verify and bid_total_usd may be null only when refused")
        if verify is None and bid is None:
            return
    if verify is not None:
        if not isinstance(verify, Mapping):
            errors.append("totals.verify: must be an object")
            return
        keys = set(verify)
        if keys != set(VERIFY_KEYS):
            missing = sorted(set(VERIFY_KEYS) - keys)
            extra = sorted(keys - set(VERIFY_KEYS))
            errors.append(f"totals.verify: keys must be the ten Verify numbers ({missing=}, {extra=})")
            return
        for key in VERIFY_KEYS:
            _num(verify, key, "totals.verify", errors, null=True)
    if bid is not None and not _is_number(bid):
        errors.append(f"totals.bid_total_usd: must be a finite number; got {bid!r}")
        return
    if verify is None or bid is None:
        errors.append("totals: verify and bid_total_usd must both be set or both be null")
        return
    if errors and any(e.startswith("totals.verify.") for e in errors):
        return
    if not bid_total_matches_verify(bid, verify):
        errors.append(
            f"totals.bid_total_usd {_to_cents(bid)} != sum(verify) {verify_sum(verify)} "
            "(None counted as 0)"
        )


def priced_estimate_errors(doc: Any, *, status: str | None = None) -> list[str]:
    """Every contract violation of a `bdr.priced_estimate/1` artifact.

    `status` is the PricedEstimate status the artifact was built from, when the
    caller knows it (the artifact may also carry it as an optional top-level
    `status`); `blocked` requires `refused: true` and the reverse.
    """
    errors: list[str] = []
    if not isinstance(doc, Mapping):
        return ["priced_estimate: must be an object"]
    if not _need_keys(doc, PRICED_ESTIMATE_TOP_KEYS, "priced_estimate", errors):
        return errors
    if doc.get("schema") != PRICED_ESTIMATE_SCHEMA:
        errors.append(f"schema: must be {PRICED_ESTIMATE_SCHEMA!r}; got {doc.get('schema')!r}")
    if "run_id" in doc:
        _str(doc, "run_id", "priced_estimate", errors, null=True)
    _bool(doc, "refused", "priced_estimate", errors)
    _str_list(doc, "refusal_reasons", "priced_estimate", errors)
    refused = doc.get("refused")
    if refused is True and not doc.get("refusal_reasons"):
        errors.append("refusal_reasons: a refused estimate needs at least one reason")
    for st, label in ((status, "status argument"), (doc.get("status"), "status")):
        if st is None:
            continue
        if not isinstance(st, str) or st not in PRICED_STATUSES:
            errors.append(f"{label}: must be one of {', '.join(PRICED_STATUSES)}; got {st!r}")
        elif (st == "blocked") != (refused is True):
            errors.append(f"refused must be {st == 'blocked'} when status is {st}")
    _enum(doc, "wage_type", WAGE_TYPES, "priced_estimate", errors, null=True)
    _num(doc, "labor_rate_usd", "priced_estimate", errors, null=True)
    _str(doc, "labor_rate_basis", "priced_estimate", errors, null=True)

    lines = doc.get("lines")
    if not isinstance(lines, list):
        errors.append("lines: must be a list")
    else:
        seen: set[str] = set()
        for i, line in enumerate(lines):
            _priced_line_errors(line, i, errors, seen)

    allowances = doc.get("allowances")
    if not isinstance(allowances, list):
        errors.append("allowances: must be a list")
    else:
        for i, allowance in enumerate(allowances):
            where = f"allowances[{i}]"
            if not isinstance(allowance, Mapping):
                errors.append(f"{where}: must be an object")
                continue
            _str(allowance, "name", where, errors, null=False)
            _num(allowance, "amount_usd", where, errors, null=True)

    quotes = doc.get("quotes")
    if not isinstance(quotes, list):
        errors.append("quotes: must be a list")
    else:
        for i, quote in enumerate(quotes):
            where = f"quotes[{i}]"
            if not isinstance(quote, Mapping):
                errors.append(f"{where}: must be an object")
                continue
            if not _need_keys(quote, QUOTE_KEYS, where, errors):
                continue
            _str(quote, "category", where, errors, null=False)
            _str(quote, "vendor", where, errors, null=False)
            _num(quote, "amount_usd", where, errors, null=False)
            _bool(quote, "tax_included", where, errors)
            _str_list(quote, "replaces_sections", where, errors)
            _str(quote, "file_sha256", where, errors, null=True)

    if not isinstance(doc.get("sections"), Mapping):
        errors.append("sections: must be an object")
    _totals_errors(doc.get("totals"), refused, errors)
    _str_list(doc, "exclusions", "priced_estimate", errors)
    _str_list(doc, "clarifications", "priced_estimate", errors)

    metadata = doc.get("metadata")
    if not isinstance(metadata, Mapping):
        errors.append("metadata: must be an object")
    elif _need_keys(metadata, METADATA_KEYS, "metadata", errors):
        _str(metadata, "library_fingerprint", "metadata", errors, null=True)
        sha = metadata.get("excluded_project_ids_sha256")
        if sha is not None and (not isinstance(sha, str) or not _SHA256_RE.match(sha)):
            errors.append("metadata.excluded_project_ids_sha256: must be 64 lowercase hex or null")
    return errors


def validate_priced_estimate(doc: Any, *, status: str | None = None) -> Any:
    """Return `doc` when it is a valid `bdr.priced_estimate/1`, else raise."""
    errors = priced_estimate_errors(doc, status=status)
    if errors:
        raise ContractError("; ".join(errors))
    return doc


def excluded_project_ids_sha256(project_ids: Iterable[str]) -> str:
    """sha256 of the sorted excluded project ids (GC4), one id per line."""
    joined = "\n".join(sorted({str(p) for p in project_ids}))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# Gate ids (5.4) -------------------------------------------------------------------

# WS4 owns these, the only names. fact_conflict is the conflict form of
# excluded_system; uom_conflict_quote and wage_type_unknown are raised by WS6
# at stage pricing (wage_type_unknown from the bid conditions, 3.6) and
# recorded by WS4; branch_ratio is warn-only. The needs_review value for the
# same condition is spelled wage_unknown (5.1), the gate code wage_type_unknown.
GATE_IDS: tuple[str, ...] = (
    "new_devices_no_branch",
    "linear_in_ea",
    "census_floor",
    "partial_basis",
    "unverifiable_citation",
    "unsourced_line",
    "orphan_rows",
    "arithmetic",
    "unit_class",
    "excluded_system",
    "fact_conflict",
    "brief_numbers",
    "capability_denial",
    "lost_sheets",
    "double_consumption",
    "uom_conflict_quote",
    "wage_type_unknown",
    "branch_ratio",
)
WARN_ONLY_GATES: frozenset[str] = frozenset({"branch_ratio"})
# Gates raised by WS6 at stage pricing (recorded through record_findings).
PRICING_STAGE_GATES: frozenset[str] = frozenset({"uom_conflict_quote", "wage_type_unknown"})
# Producer names that map onto a gate id (WS3 findings).
GATE_ALIASES: dict[str, str] = {"branch_ft_empty": "new_devices_no_branch"}

GATE_SEVERITIES: tuple[str, ...] = ("block", "warn")
FINDING_STAGES: tuple[str, ...] = ("pre", "final", "pricing")
FINDING_ROUTES: tuple[str, ...] = ("recount", "measure", "model_fix", "human", "none")
FINDING_STATUSES: tuple[str, ...] = ("open", "routed", "resolved", "overridden", "superseded")
# Findings in these statuses still count toward `blocking`.
ACTIVE_FINDING_STATUSES: frozenset[str] = frozenset({"open", "routed"})


def canonical_gate_id(value: str) -> str:
    """The gate id for `value`, mapping producer aliases; raises when unknown."""
    gate_id = GATE_ALIASES.get(value, value)
    return validate_enum(gate_id, GATE_IDS, "gate_id")


def validate_gate_id(value: Any) -> str:
    return validate_enum(value, GATE_IDS, "gate_id")


def validate_gate_severity(value: Any, gate_id: str | None = None) -> str:
    validate_enum(value, GATE_SEVERITIES, "severity")
    if gate_id in WARN_ONLY_GATES and value != "warn":
        raise ContractError(f"gate {gate_id} is warn-only")
    return value


def validate_finding_stage(value: Any) -> str:
    return validate_enum(value, FINDING_STAGES, "stage")


def validate_finding_route(value: Any) -> str:
    return validate_enum(value, FINDING_ROUTES, "route")


def validate_finding_status(value: Any) -> str:
    return validate_enum(value, FINDING_STATUSES, "status")


# bdr.gate_report/1 ----------------------------------------------------------------

GATE_CHECK_KEYS: tuple[str, ...] = ("code", "severity", "detail", "rows")


def gate_report_errors(doc: Any) -> list[str]:
    """Every contract violation of a `bdr.gate_report/1` artifact."""
    errors: list[str] = []
    if not isinstance(doc, Mapping):
        return ["gate_report: must be an object"]
    if not _need_keys(doc, ("schema", "blocking", "checks"), "gate_report", errors):
        return errors
    if doc.get("schema") != GATE_REPORT_SCHEMA:
        errors.append(f"schema: must be {GATE_REPORT_SCHEMA!r}; got {doc.get('schema')!r}")
    _bool(doc, "blocking", "gate_report", errors)
    checks = doc.get("checks")
    if not isinstance(checks, list):
        errors.append("checks: must be a list")
        return errors
    any_block = False
    for i, check in enumerate(checks):
        where = f"checks[{i}]"
        if not isinstance(check, Mapping):
            errors.append(f"{where}: must be an object")
            continue
        if not _need_keys(check, GATE_CHECK_KEYS, where, errors):
            continue
        _enum(check, "code", GATE_IDS, where, errors, null=False)
        _enum(check, "severity", GATE_SEVERITIES, where, errors, null=False)
        _str(check, "detail", where, errors, null=False)
        if check.get("code") in WARN_ONLY_GATES and check.get("severity") != "warn":
            errors.append(f"{where}: gate {check.get('code')} is warn-only")
        rows = check.get("rows")
        if not isinstance(rows, list):
            errors.append(f"{where}.rows: must be a list of BOQ line ids")
        else:
            bad = [r for r in rows if not is_row_id(r, BOQ_LINE_PREFIX)]
            if bad:
                errors.append(f"{where}.rows: not BOQ line ids: {bad!r}")
        if check.get("severity") == "block":
            any_block = True
    if doc.get("blocking") is True and not any_block:
        errors.append("blocking is true but no check has severity block")
    return errors


def validate_gate_report(doc: Any) -> Any:
    """Return `doc` when it is a valid `bdr.gate_report/1`, else raise."""
    errors = gate_report_errors(doc)
    if errors:
        raise ContractError("; ".join(errors))
    return doc


def artifact_pair_errors(priced: Mapping[str, Any], gate_report: Mapping[str, Any]) -> list[str]:
    """Cross-artifact rule: a blocking gate report means `refused: true`."""
    if gate_report.get("blocking") is True and priced.get("refused") is not True:
        return ["gate_report is blocking but priced_estimate.refused is not true"]
    return []


def validate_artifact(doc: Any) -> Any:
    """Validate a WS0 artifact by its `schema` field."""
    schema = doc.get("schema") if isinstance(doc, Mapping) else None
    if schema == PRICED_ESTIMATE_SCHEMA:
        return validate_priced_estimate(doc)
    if schema == GATE_REPORT_SCHEMA:
        return validate_gate_report(doc)
    raise ContractError(f"unknown artifact schema {schema!r}")


# Gate findings (WS4 shape of plans_reader_gate_findings) -------------------------


def _canonical_target(target: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(target):
        value = target[key]
        if value is None:
            continue
        if key == "row_ids" and isinstance(value, (list, tuple)):
            value = sorted(str(v) for v in value)
        out[key] = value
    return out


def finding_fingerprint(gate_id: str, target: Mapping[str, Any] | None) -> str:
    """Deterministic sha1 of the gate id plus the canonical target.

    Key order, None values and the order of `row_ids` do not change it, so the
    same problem found again in a later pass upserts onto the same row
    (unique run_id, output_version, stage, fingerprint).
    """
    payload = json.dumps(
        {"gate_id": gate_id, "target": _canonical_target(target or {})},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass
class GateFinding:
    """One integrity gate finding (the `plans_reader_gate_findings` row).

    `target` names what the finding is about: any of line_id, row_ids,
    sheet_key, family, fact_id, field. The fingerprint is computed from the
    gate id and the target when not given. Producer aliases (branch_ft_empty)
    are mapped onto the gate id.
    """

    run_id: str | None
    output_version: int | None
    stage: str
    gate_id: str
    severity: str
    route: str = "none"
    status: str = "open"
    target: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    routed_group_id: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        self.gate_id = canonical_gate_id(self.gate_id)
        validate_finding_stage(self.stage)
        validate_gate_severity(self.severity, self.gate_id)
        validate_finding_route(self.route)
        validate_finding_status(self.status)
        if not isinstance(self.target, dict):
            raise ContractError("target must be a dict")
        line_id = self.target.get("line_id")
        if line_id is not None:
            validate_row_id(line_id, BOQ_LINE_PREFIX)
        if not self.fingerprint:
            self.fingerprint = finding_fingerprint(self.gate_id, self.target)

    @property
    def blocking(self) -> bool:
        return self.severity == "block" and self.status in ACTIVE_FINDING_STATUSES

    @property
    def key(self) -> tuple[str | None, int | None, str, str]:
        """The unique key of the persisted row."""
        return (self.run_id, self.output_version, self.stage, self.fingerprint)

    def line_ids(self) -> list[str]:
        """BOQ line ids the finding targets (for the gate report `rows`)."""
        ids: list[str] = []
        line_id = self.target.get("line_id")
        if isinstance(line_id, str):
            ids.append(line_id)
        for rid in self.target.get("row_ids") or ():
            if is_row_id(rid, BOQ_LINE_PREFIX) and rid not in ids:
                ids.append(rid)
        return ids

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "output_version": self.output_version,
            "stage": self.stage,
            "gate_id": self.gate_id,
            "severity": self.severity,
            "route": self.route,
            "status": self.status,
            "target": dict(self.target),
            "message": self.message,
            "evidence": dict(self.evidence),
            "fingerprint": self.fingerprint,
            "routed_group_id": self.routed_group_id,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
            "note": self.note,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> GateFinding:
        return cls(
            run_id=row.get("run_id"),
            output_version=row.get("output_version"),
            stage=row["stage"],
            gate_id=row["gate_id"],
            severity=row["severity"],
            route=row.get("route") or "none",
            status=row.get("status") or "open",
            target=dict(row.get("target") or {}),
            message=row.get("message") or "",
            evidence=dict(row.get("evidence") or {}),
            fingerprint=row.get("fingerprint") or "",
            routed_group_id=row.get("routed_group_id"),
            resolved_by=row.get("resolved_by"),
            resolved_at=row.get("resolved_at"),
            note=row.get("note"),
        )


def gate_report_from_findings(findings: Iterable[GateFinding]) -> dict[str, Any]:
    """A `bdr.gate_report/1` from findings; resolved, overridden and
    superseded findings are left out, `blocking` is any active block."""
    checks: list[dict[str, Any]] = []
    blocking = False
    for f in findings:
        if f.status not in ACTIVE_FINDING_STATUSES:
            continue
        blocking = blocking or f.severity == "block"
        checks.append(
            {"code": f.gate_id, "severity": f.severity, "detail": f.message, "rows": f.line_ids()}
        )
    return {"schema": GATE_REPORT_SCHEMA, "blocking": blocking, "checks": checks}


def _run_id_of(run: Any) -> str | None:
    if run is None:
        return None
    if isinstance(run, str):
        return run
    if isinstance(run, Mapping):
        value = run.get("id")
        return str(value) if value is not None else None
    value = getattr(run, "id", None)
    return str(value) if value is not None else None


@runtime_checkable
class RecordFindings(Protocol):
    """`plans_reader_gates.record_findings(sb, run, findings, stage)` (WS4 owns
    persistence). Upserts by (run_id, output_version, stage, fingerprint) and
    returns the findings as stored. `stage` overrides each finding's stage."""

    def __call__(
        self, sb: Any, run: Any, findings: Sequence[GateFinding], stage: str
    ) -> list[GateFinding]: ...


def _stamp(run: Any, findings: Sequence[GateFinding], stage: str) -> list[GateFinding]:
    validate_finding_stage(stage)
    run_id = _run_id_of(run)
    out: list[GateFinding] = []
    for f in findings:
        if not isinstance(f, GateFinding):
            raise ContractError(f"record_findings takes GateFinding values; got {type(f).__name__}")
        if f.stage != stage or (f.run_id is None and run_id is not None):
            row = f.to_row()
            row["stage"] = stage
            row["run_id"] = f.run_id if f.run_id is not None else run_id
            f = GateFinding.from_row(row)
        out.append(f)
    return out


def null_record_findings(
    sb: Any, run: Any, findings: Sequence[GateFinding], stage: str
) -> list[GateFinding]:
    """No-op default: validates and stamps the findings, stores nothing."""
    return _stamp(run, findings, stage)


class InMemoryFindingsStore:
    """In-memory `record_findings` for tests and replay (`ctx.sb is None`).

    Same upsert key as the table. A re-recorded finding keeps the stored
    status, route and resolution fields (a human override survives a re-run
    of the gates) and refreshes message and evidence.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str | None, int | None, str, str], GateFinding] = {}

    def record_findings(
        self, sb: Any, run: Any, findings: Sequence[GateFinding], stage: str
    ) -> list[GateFinding]:
        stored: list[GateFinding] = []
        for f in _stamp(run, findings, stage):
            prior = self.rows.get(f.key)
            if prior is not None:
                prior.message = f.message
                prior.evidence = dict(f.evidence)
                f = prior
            else:
                self.rows[f.key] = f
            stored.append(f)
        return stored

    __call__ = record_findings

    def for_run(self, run_id: str, *, stage: str | None = None) -> list[GateFinding]:
        return [
            f
            for f in self.rows.values()
            if f.run_id == run_id and (stage is None or f.stage == stage)
        ]


# Facts (WS5) and excluded lines (WS4) ----------------------------------------------

FACT_TYPES: tuple[str, ...] = (
    "provided_by_others",
    "not_used",
    "existing_to_remain",
    "owner_furnished",
    "nic",
    "by_gc",
)
# Fact types that exclude a system from G3 scope when verified.
EXCLUDING_FACT_TYPES: frozenset[str] = frozenset({"provided_by_others", "not_used", "nic", "by_gc"})
FACT_SOURCES: tuple[str, ...] = ("code", "consolidator", "llm")


def validate_fact_type(value: Any) -> str:
    return validate_enum(value, FACT_TYPES, "fact type")


def validate_fact_source(value: Any) -> str:
    return validate_enum(value, FACT_SOURCES, "fact source")


@dataclass(frozen=True)
class FactCite:
    """Where a fact's quote is printed. `verified` on the Fact is true only
    when the quote matches the cited page text."""

    document_id: str
    page: int
    quote: str
    row_id: str | None = None
    section: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "page": self.page,
            "quote": self.quote,
            "row_id": self.row_id,
            "section": self.section,
        }


@dataclass(frozen=True)
class Fact:
    """A scope fact (row id F<n>). An unverified fact never auto-excludes."""

    id: str
    system: str
    statement: str
    party: str | None
    verified: bool
    cite: FactCite
    source: str = "code"
    kind: str | None = None

    def __post_init__(self) -> None:
        validate_row_id(self.id, FACT_PREFIX)
        validate_fact_source(self.source)
        if self.kind is not None:
            validate_fact_type(self.kind)
        if not isinstance(self.verified, bool):
            raise ContractError("Fact.verified must be a bool")

    @property
    def excludes(self) -> bool:
        """True when this fact may auto-exclude its system."""
        return self.verified and (self.kind is None or self.kind in EXCLUDING_FACT_TYPES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "system": self.system,
            "statement": self.statement,
            "party": self.party,
            "verified": self.verified,
            "cite": self.cite.to_dict(),
            "source": self.source,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Fact:
        cite = data.get("cite") or {}
        return cls(
            id=data["id"],
            system=data["system"],
            statement=data["statement"],
            party=data.get("party"),
            verified=bool(data.get("verified")),
            cite=FactCite(
                document_id=cite.get("document_id", ""),
                page=int(cite.get("page") or 0),
                quote=cite.get("quote", ""),
                row_id=cite.get("row_id"),
                section=cite.get("section"),
            ),
            source=data.get("source") or "code",
            kind=data.get("kind"),
        )


@runtime_checkable
class FactsForRun(Protocol):
    """`scope_engine.facts_for_run(sb, run_id) -> list[Fact]` (WS5)."""

    def __call__(self, sb: Any, run_id: str) -> list[Fact]: ...


def no_facts(sb: Any, run_id: str) -> list[Fact]:
    """Default before WS5 lands: no facts, so nothing is auto-excluded."""
    return []


EXCLUDED_LINE_SOURCES: tuple[str, ...] = ("fact", "policy", "human")


def excluded_note(system: str, party: str | None, cite: str, quote: str) -> str:
    """The estimate general note written for an excluded line (WS4)."""
    who = party or "others"
    return f'EXCLUDED: {system} by {who} per {cite}: "{quote}"'


@dataclass(frozen=True)
class ExcludedLine:
    """A BOQ line moved out of scope with its quantity preserved (visible and
    reversible; exclusion never zeroes a quantity)."""

    line_id: str
    description: str
    quantity: float
    unit: str
    system: str | None
    reason: str
    source: str = "fact"
    party: str | None = None
    fact_id: str | None = None
    rule_id: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        validate_row_id(self.line_id, BOQ_LINE_PREFIX)
        validate_enum(self.source, EXCLUDED_LINE_SOURCES, "ExcludedLine.source")
        if self.fact_id is not None:
            validate_row_id(self.fact_id, FACT_PREFIX)
        if not _is_number(self.quantity) or self.quantity < 0:
            raise ContractError(f"ExcludedLine.quantity must be >= 0; got {self.quantity!r}")
        if not self.reason:
            raise ContractError("ExcludedLine.reason is required")
        if self.source == "fact" and self.fact_id is None:
            raise ContractError("an ExcludedLine from a fact needs fact_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "description": self.description,
            "quantity": self.quantity,
            "unit": self.unit,
            "system": self.system,
            "reason": self.reason,
            "source": self.source,
            "party": self.party,
            "fact_id": self.fact_id,
            "rule_id": self.rule_id,
            "note": self.note,
        }


# pricing_allowed (WS4) --------------------------------------------------------------


@runtime_checkable
class PricingAllowed(Protocol):
    """`plans_reader_gates.pricing_allowed(run) -> (allowed, reasons)`.

    Reads only `stage = final` findings (plus unapproved manual lines).
    """

    def __call__(self, run: Any) -> tuple[bool, list[str]]: ...


PRICING_GATES_UNAVAILABLE = "pricing_gates_unavailable"


def pricing_allowed_unavailable(run: Any) -> tuple[bool, list[str]]:
    """Default before WS4 lands: refuse with a typed reason, never allow."""
    return False, [PRICING_GATES_UNAVAILABLE]


# Acceptance check registry (3.5) -----------------------------------------------------

CHECK_STATUSES: tuple[str, ...] = ("pass", "fail", "skip", "safe_refusal", "error")
REPLAY_KINDS: tuple[str, ...] = ("yes", "partial", "no")


def validate_check_status(value: Any) -> str:
    return validate_enum(value, CHECK_STATUSES, "check status")


_E3_PATTERNS = (
    r"physical page calibration",
    r"do not use per-device averages",
    r"(no|without) .*invented assumed routes",
    r"printed scale alone .* insufficient",
    r"cannot be defensibly measured",
)
_E5_PATTERN = (
    r"TEMP(ORARY)? (POWER|DISTRIBUTION|LIGHTING)|HEPA|COORDINATION STUDY|COMMISSIONING"
    r"|PATCH|FIRESTOP|\bBIM\b|TRAINING|PHASING|ANCILLARY"
)
_E8_PATTERN = (
    r"TELEMETRY|CENTRAK|\bWAP\b|WIRELESS ACCESS|FURNITURE (?!CONNECTION)|DOOR HARDWARE|\bPAINT"
)
_E21_MODIFIERS = (
    "EMERGENCY",
    "ESSENTIAL",
    "NORMAL",
    "LIFE SAFETY",
    "DEDICATED",
    "colors",
)


@dataclass(frozen=True)
class CheckSpec:
    """The fixed row of section 3.5 for one check id."""

    id: str
    threshold: Mapping[str, Any]
    replay: str
    gates_7154_smoke: bool
    skip_rule: str | None = None

    @property
    def replayable(self) -> bool:
        return self.replay != "no"


def _spec(
    cid: str,
    replay: str,
    gates: bool,
    threshold: dict[str, Any],
    skip_rule: str | None = None,
) -> CheckSpec:
    return CheckSpec(cid, threshold, replay, gates, skip_rule)


# Pass thresholds of 3.5 as data. A registration can never change these.
CHECK_SPECS: dict[str, CheckSpec] = {
    s.id: s
    for s in (
        _spec("E1", "yes", True, {
            "artifact": PRICED_ESTIMATE_SCHEMA,
            "refused": False,
            "priced_share_min": 0.98,
            "bid_total_equals_verify_sum": "cent",
            "chat_hours_rel_tol": 0.15,
        }, skip_rule="chat part may skip only when no chat probe ran"),
        _spec("E2", "yes", True, {
            "new_devices_need_branch_ft": True,
            "truth_ft_7154": {"#12": 7390.15, "#10": 13524.28, '3/4" EMT': 6971.48},
            "band": "acceptable",
            "emt_band": "good",
        }),
        _spec("E3", "no", True, {
            "denial_patterns": list(_E3_PATTERNS),
            "max_hits": 0,
            "ft_lines_need_basis": True,
        }),
        _spec("E4", "yes", True, {
            "wage_type": "prevailing_wage",
            "labor_rate_usd": 125.00,
            "forbidden_rfi_pattern": r"prevailing wage|NRS\s*338|Davis.?Bacon",
        }),
        _spec("E5", "yes", True, {
            "house_exclusion_pattern": _E5_PATTERN,
            "max_priced_hits": 0,
            "detected_in_exclusions_or_clarifications": True,
            "max_extra_allowances": 0,
        }),
        _spec("E6", "yes", True, {
            "branch_conduit_type": "EMT",
            "emt_ft_min_ratio": 0.75,
            "forbidden_branch_types": ["RGS", "RMC", "IMC"],
            "clarification_names": "EMT",
            "max_raceway_rfis": 0,
        }),
        _spec("E7", "yes", True, {
            "forbidden_systems": ["fire_alarm", "access_control"],
            "exclusion_party": "Honeywell",
            "exclusion_cite": "Exhibit A",
        }),
        _spec("E8", "yes", True, {
            "forbidden_pattern": _E8_PATTERN,
            "must_be_excluded_or_clarified": [
                "telemetry",
                "centrak",
                "wap",
                "furniture",
                "door hardware",
                "paint",
            ],
            "citation_required": True,
        }),
        _spec("E9", "yes", True, {
            "section": "LIGHTING FIXTURES",
            "material_source": "vendor_quote",
            "pretax_targets_usd": {
                "lowest_eligible": {"ref": "e9.lowest_eligible_quote_pretax_usd"},
                "alternate_selection": {"ref": "e9.alternate_quote_pretax_usd"},
            },
            "rel_tol": 0.02,
            "reason_required": True,
            "reconcile": {"L3_once": 16, "L5_ft": 50, "L1": 21, "L3": 16},
        }),
        _spec("E10", "yes", True, {
            "item": "L5",
            "unit": "FT",
            "truth_ft": 43.46,
            "band": "acceptable",
            "target_band": "good",
            "stored_v1_gate": "uom_conflict_quote",
        }),
        _spec("E11", "partial", True, {
            "demo_lighting_truth": 49,
            "demo_lighting_rel_tol": 0.10,
            "ed3_01_troffers": 17,
            "tile_subtotal_rows_max": 0,
            "census_mismatch_max": 0.10,
        }, skip_rule="gates live"),
        _spec("E12", "yes", True, {
            "breaker_line": "20A/1P CIRCUIT BREAKER, GFCI",
            "breaker_qty": 4,
            "breaker_lines": 1,
            "provisional_on_spare_or_existing": 0,
        }),
        _spec("E13", "yes", True, {
            "duplex_unit_material_min_usd": {"ref": "e13.duplex_unit_material_min_usd"},
            "gfci_unit_material_min_usd": {"ref": "e13.gfci_unit_material_min_usd"},
            "spec_grade_ratio": 0.7,
            "spec_fact_required": True,
        }),
        _spec("E14", "yes", True, {
            "thermostat_relocation_ea": 5,
            "control_cable_truth_ft": 141,
            "control_cable_band": "acceptable",
        }),
        _spec("E15", "yes", True, {
            "unit": "FT",
            "basis": "measured",
            "truth_ft": 50.07,
            "band": "acceptable",
            "target_rel_tol": 0.10,
            "linear_in_ea_human_block": "safe_refusal elsewhere, fail on 7154",
        }, skip_rule="no skip"),
        _spec("E16", "yes", True, {
            "legend_page": "E0.00",
            "legend_entries_min": 60,
            "legend_text_in_every_drawing_request": True,
            "need_to_confirm_caveat_rows": 0,
        }, skip_rule="caveat part may skip on replay"),
        _spec("E17", "yes", True, {
            "median_anchor_ft_max": 1.5,
            "over_5ft_share_max": 0.10,
            "stacked_within_ft": 0.25,
            "stacked_max": 2,
            "ft_lines_need_polyline": True,
        }),
        _spec("E18", "yes", True, {
            "drawing_pages_labeled_share_min": 0.90,
            "facts_status": "done",
            "facts_block_every_page": True,
            "planned_input_tokens_max_7154": 940000,
        }),
        _spec("E19", "no", True, {
            "unreadable_hits_for_text_layer_values": 0,
            "l4_control": "LOCAL",
            "l4_control_questions": 0,
        }, skip_rule="gates live"),
        _spec("E20", "yes", True, {
            "maintain_vs_remove_rfis": 0,
            "demo_disposition_with_citation": True,
        }),
        _spec("E21", "yes", True, {
            "tr_duplex_ea": 3,
            "unpriced_modifier_split_rows": 0,
            "unpriced_modifiers": list(_E21_MODIFIERS),
            "receptacle_total": 74,
        }),
        _spec("E22", "yes", True, {
            "demo_power_truth": 35,
            "sum_tol": 1,
            "max_rows_two_line_style": 1,
            "max_rows_default": 2,
            "make_safe_damper_rows": 0,
        }),
        _spec("E23", "yes", True, {
            "emt_accessories": ["connectors", "couplings", "straps"],
            "couplings_truth": 698,
            "couplings_rel_tol": 0.10,
        }),
        _spec("E24", "yes", True, {
            "m_prefix_share_min": 0.80,
            "spec_pages_sent_share_max": 0.40,
            "spec_pages_sent_max_7154": 68,
            "spec_pages_7154": 170,
        }),
        _spec("E25", "yes", True, {
            "count_lines_exact": 17,
            "count_lines_total": 22,
            "dollar_coverage": 0.319,
            "dollar_coverage_tol": 0.005,
            "matcher_version": 2,
            "unresolved_truth_key_collisions": 0,
        }),
        _spec("GC1", "yes", True, {
            "non_quoted_material_gt_zero_when_truth_has_material": True,
        }),
        _spec("GC2", "yes", False, {
            "topic_recall_min": 0.7,
            "priced_lines_on_excluded_topics": 0,
        }, skip_rule="report only on 7154; core and holdout report the median"),
        _spec("GC3", "yes", True, {
            "false_refusals": 0,
            "refusal_on_7154": False,
        }),
        _spec("GC4", "yes", True, {
            "excluded_project_ids_sha256": "sha256 of sorted excluded ids",
            "includes_own_project": True,
        }),
    )
}
FIXED_CHECK_IDS: tuple[str, ...] = tuple(CHECK_SPECS)

# Keys of EVAL_ROOT/references/7154/expected.json that CHECK_SPECS references
# (see the module docstring). A test pins this to the refs in CHECK_SPECS.
REFERENCE_KEYS: tuple[str, ...] = (
    "e9.lowest_eligible_quote_pretax_usd",
    "e9.alternate_quote_pretax_usd",
    "e13.duplex_unit_material_min_usd",
    "e13.gfci_unit_material_min_usd",
)


def _is_reference(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"ref"}
        and isinstance(value.get("ref"), str)
        and bool(value["ref"])
    )


def threshold_reference_keys(threshold: Mapping[str, Any] | CheckSpec) -> list[str]:
    """Every `{"ref": key}` key inside a threshold (or a CheckSpec), in order."""
    if isinstance(threshold, CheckSpec):
        threshold = threshold.threshold
    out: list[str] = []

    def walk(value: Any) -> None:
        if _is_reference(value):
            out.append(value["ref"])
        elif isinstance(value, Mapping):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(threshold)
    return out


def _lookup_reference(references: Mapping[str, Any], key: str) -> Any:
    if key in references:
        return references[key]
    node: Any = references
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise ContractError(f"reference {key!r} is missing from expected.json")
        node = node[part]
    return node


def resolve_reference_thresholds(
    spec: CheckSpec | Mapping[str, Any], references: Mapping[str, Any]
) -> dict[str, Any]:
    """A deep copy of the threshold of `spec` with every `{"ref": key}` replaced
    by its value from `references` (the parsed EVAL_ROOT/references/7154/
    expected.json). Keys resolve as a flat key first, then as a dotted path.
    A missing key, or a value that is not a finite number, raises ContractError.
    A threshold without references comes back as an equal copy."""
    threshold = spec.threshold if isinstance(spec, CheckSpec) else spec
    if not isinstance(threshold, Mapping):
        raise ContractError("resolve_reference_thresholds needs a CheckSpec or a mapping")
    if not isinstance(references, Mapping):
        raise ContractError("references must be a mapping (the parsed expected.json)")

    def walk(value: Any) -> Any:
        if _is_reference(value):
            got = _lookup_reference(references, value["ref"])
            if not _is_number(got):
                raise ContractError(f"reference {value['ref']!r} must be a finite number")
            return got
        if isinstance(value, Mapping):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value

    return walk(threshold)


def _canonical_threshold(threshold: Mapping[str, Any]) -> str:
    return json.dumps(threshold, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class CheckResult:
    """What a detector returns: status plus the measured values."""

    id: str
    status: str
    measured: dict[str, Any] = field(default_factory=dict)
    threshold: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    owner: str = ""

    def __post_init__(self) -> None:
        validate_check_status(self.status)


CheckFn = Callable[..., CheckResult]


@dataclass(frozen=True)
class Check:
    id: str
    fn: CheckFn
    gates_7154_smoke: bool
    replayable: bool
    threshold: Mapping[str, Any]
    title: str = ""
    owner: str = ""

    def run(self, *args: Any, **kwargs: Any) -> CheckResult:
        """Run the detector; an exception becomes status `error`, never a pass."""
        try:
            result = self.fn(*args, **kwargs)
        except Exception as exc:  # a crashing detector is an error result, never a pass
            return CheckResult(
                id=self.id,
                status="error",
                measured={"exception": f"{type(exc).__name__}: {exc}"},
                threshold=dict(self.threshold),
                owner=self.owner,
            )
        if not isinstance(result, CheckResult):
            return CheckResult(
                id=self.id,
                status="error",
                measured={"exception": f"detector returned {type(result).__name__}"},
                threshold=dict(self.threshold),
                owner=self.owner,
            )
        return result


class CheckRegistry:
    """Registry of acceptance checks keyed by id.

    E1 to E25 and GC1 to GC4 take their threshold, `gates_7154_smoke` and
    `replayable` from CHECK_SPECS; a registration that disagrees is refused.
    With `references` (the parsed EVAL_ROOT/references/7154/expected.json) the
    stored threshold has its `{"ref": key}` values resolved through
    resolve_reference_thresholds; a passed threshold may then be either the
    raw spec threshold or the resolved one. Other ids (G-prefixed extras) must pass a threshold; re-registering an id
    is allowed only with the same threshold (the detector may be replaced).
    """

    def __init__(self) -> None:
        self.checks: dict[str, Check] = {}

    def register(
        self,
        check_id: str,
        fn: CheckFn,
        *,
        gates_7154_smoke: bool,
        replayable: bool,
        threshold: Mapping[str, Any] | None = None,
        title: str = "",
        owner: str = "",
        references: Mapping[str, Any] | None = None,
    ) -> Check:
        if not isinstance(check_id, str) or not check_id:
            raise ContractError("check id must be a non-empty string")
        if not callable(fn):
            raise ContractError(f"check {check_id}: fn must be callable")
        spec = CHECK_SPECS.get(check_id)
        if spec is not None:
            fixed: Mapping[str, Any] = spec.threshold
            if references is not None:
                fixed = resolve_reference_thresholds(spec, references)
            allowed = {_canonical_threshold(spec.threshold), _canonical_threshold(fixed)}
            if threshold is not None and _canonical_threshold(threshold) not in allowed:
                raise ContractError(
                    f"check {check_id}: threshold is fixed by spec section 3.5 and cannot change"
                )
            if gates_7154_smoke != spec.gates_7154_smoke:
                raise ContractError(
                    f"check {check_id}: gates_7154_smoke is fixed at {spec.gates_7154_smoke}"
                )
            if replayable != spec.replayable:
                raise ContractError(f"check {check_id}: replayable is fixed at {spec.replayable}")
            threshold = fixed
        elif threshold is None:
            raise ContractError(f"check {check_id}: a threshold is required")
        existing = self.checks.get(check_id)
        if existing is not None and _canonical_threshold(existing.threshold) != (
            _canonical_threshold(threshold)
        ):
            raise ContractError(
                f"check {check_id} is already registered with a different threshold"
            )
        frozen = json.loads(_canonical_threshold(threshold))
        check = Check(
            id=check_id,
            fn=fn,
            gates_7154_smoke=gates_7154_smoke,
            replayable=replayable,
            threshold=frozen,
            title=title,
            owner=owner,
        )
        self.checks[check_id] = check
        return check

    def get(self, check_id: str) -> Check:
        try:
            return self.checks[check_id]
        except KeyError:
            raise ContractError(f"no check registered as {check_id!r}") from None

    def missing_fixed(self) -> list[str]:
        """Ids of 3.5 with no detector registered yet."""
        return [cid for cid in FIXED_CHECK_IDS if cid not in self.checks]


_REGISTRY = CheckRegistry()
# The process-wide registry (plans_reader_acceptance.CHECKS reads this dict).
CHECKS: dict[str, Check] = _REGISTRY.checks


def register_check(
    check_id: str,
    fn: CheckFn,
    *,
    gates_7154_smoke: bool,
    replayable: bool,
    threshold: Mapping[str, Any] | None = None,
    title: str = "",
    owner: str = "",
    references: Mapping[str, Any] | None = None,
) -> Check:
    """Register a detector in the process-wide CHECKS (see CheckRegistry)."""
    return _REGISTRY.register(
        check_id,
        fn,
        gates_7154_smoke=gates_7154_smoke,
        replayable=replayable,
        threshold=threshold,
        title=title,
        owner=owner,
        references=references,
    )
