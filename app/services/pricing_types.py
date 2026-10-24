"""Typed contracts of the AI Estimating pricing engine (AI_ESTIMATING_BUILD_SPEC 5.4).

WS6 prices a BOQ into a `PricedEstimate`; WS7 (review) and WS8 (evidence,
autonomy, UI) read it. This module holds only the shapes, their enum
validation and their JSON round trip. It holds no pricing math: unit values,
roll-ups, tax, markup and the Verify draft are computed by
`pricing_engine` (WS6) and only stored here.

Conventions:

- Money and hours are `Decimal`. `to_dict` writes them as strings (the
  convention of `routers/pricing.py`), so the dicts are JSON-safe and exact;
  `from_dict` accepts strings, ints, Decimals and floats (a float goes through
  `str` first, never through binary arithmetic). NaN and infinities raise.
- Tuples and frozensets become lists in `to_dict` (frozensets sorted).
- Every enum value is checked on construction and raises `ValueError`.
- Pricing data is internal only. These objects never reach the estimator
  role, and tests build them from synthetic numbers.
- The shared vocabularies (wage type, quantity basis, price quality, value
  method, material source, status, the ten Verify keys) and the GC4 excluded
  ids hash come from `estimating_contracts`, the root contract module; the
  names below are aliases kept for existing callers. estimating_contracts never
  imports this module, so there is no cycle.

Pure and sync: no I/O, no database, no model calls.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.estimating_contracts import (
    PRICE_QUALITIES,
    PRICED_LINE_MATERIAL_SOURCES,
    PRICED_STATUSES,
    QUANTITY_BASES,
    QUOTE_SOURCE_PREFIX,
    VALUE_STAT_METHODS,
    VERIFY_KEYS,
    WAGE_TYPES,
    excluded_project_ids_sha256,
)

SCHEMA_VERSION = 1

# Vocabularies -----------------------------------------------------------------
# All imported from estimating_contracts (see the module docstring). QUANTITY_BASES
# allows None for legacy lines; a vendor quote material source is "quote:<id>".

VALUE_METHODS: tuple[str, ...] = VALUE_STAT_METHODS

# Plain PricedLine material sources (library, seed, fallback, none).
# Deprecated alias of PRICED_LINE_MATERIAL_SOURCES.
MATERIAL_SOURCES: tuple[str, ...] = PRICED_LINE_MATERIAL_SOURCES

ESTIMATE_STATUSES: tuple[str, ...] = PRICED_STATUSES

# The ten Verify numbers of app/routers/pricing.py (VERIFY_NUMBERS), from the
# contract VERIFY_KEYS; a test pins them equal to the router tuple. Not
# imported from the router so the contract does not pull in FastAPI.
VERIFY_NUMBERS: tuple[str, ...] = VERIFY_KEYS
VERIFY_SECTION_NUMBERS: tuple[str, ...] = (
    "gear_amount",
    "underground_amount",
    "low_voltage_amount",
    "gear_markup_amount",
    "underground_markup_amount",
    "low_voltage_markup_amount",
)

# nice_ceil steps of the pricing policy (WS6): (upper bound in hours or None
# for "no bound", step). Totals at or below a bound round up to that step.
DEFAULT_HOUR_ROUNDUP_STEPS: tuple[tuple[int | None, int], ...] = (
    (200, 10),
    (1000, 25),
    (2000, 50),
    (5000, 100),
    (10000, 500),
    (None, 1000),
)

DEFAULT_TAX_RATE_PCT = Decimal("8.375")
DEFAULT_MATERIAL_MARKUP_PCT = Decimal("10")
DEFAULT_LABOR_MARKUP_PCT = Decimal("0")


# Helpers ------------------------------------------------------------------------


def _dec(value: Any, name: str) -> Decimal:
    """A finite Decimal from a Decimal, int, str or float (via str)."""
    if isinstance(value, bool):
        raise ValueError(f"{name}: expected a number, got a bool")
    try:
        if isinstance(value, Decimal):
            out = value
        elif isinstance(value, int):
            out = Decimal(value)
        elif isinstance(value, float):
            out = Decimal(str(value))
        elif isinstance(value, str):
            out = Decimal(value.strip())
        else:
            raise ValueError(f"{name}: expected a number, got {type(value).__name__}")
    except InvalidOperation as exc:
        raise ValueError(f"{name}: not a number: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"{name}: not finite: {value!r}")
    return out


def _dec_opt(value: Any, name: str) -> Decimal | None:
    return None if value is None else _dec(value, name)


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _check_enum(value: Any, allowed: tuple[str, ...], name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if value not in allowed:
        raise ValueError(f"{name}: {value!r} is not one of {', '.join(allowed)}")


def _str_tuple(values: Iterable[Any] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        return (values,)
    return tuple(str(v) for v in values)


def _json_safe(value: Any) -> Any:
    """Deep copy into JSON types: Decimal -> str, tuple/set -> list."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted(_json_safe(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def excluded_projects_sha256(ids: Iterable[Any]) -> str:
    """sha256 hex of the excluded project ids, deduplicated, sorted and joined
    by a newline (acceptance check GC4). Independent of input order. Delegates
    to estimating_contracts.excluded_project_ids_sha256."""
    return excluded_project_ids_sha256(ids)


def is_valid_material_source(value: Any) -> bool:
    """library, seed, fallback, none, or quote:<non-empty quote id>."""
    if not isinstance(value, str):
        return False
    if value in PRICED_LINE_MATERIAL_SOURCES:
        return True
    return value.startswith(QUOTE_SOURCE_PREFIX) and len(value) > len(QUOTE_SOURCE_PREFIX)


def quote_id_of(material_source: str) -> str | None:
    """The quote id of a `quote:<id>` material source, else None."""
    if material_source.startswith(QUOTE_SOURCE_PREFIX):
        return material_source[len(QUOTE_SOURCE_PREFIX) :] or None
    return None


# Inputs -------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceRow:
    """One BOQ line as the engine prices it, keyed on the BOQ `line_id` (B<n>).

    `quantity` is the wastage-inclusive quantity, `qty_base` the take-off
    quantity before wastage. `area` and `material_category` are optional
    extras carried from the BOQ item when present."""

    line_id: str
    description: str
    group: str
    subgroup: str | None
    section: str | None
    unit: str
    quantity: Decimal
    qty_base: Decimal
    wastage_applies: bool
    material_note: str | None = None
    caveat: str | None = None
    sheet_refs: tuple[str, ...] = ()
    quantity_basis: str | None = None
    item_key: str | None = None
    material_category: str | None = None
    area: str | None = None

    def __post_init__(self) -> None:
        if not self.line_id:
            raise ValueError("PriceRow.line_id is required")
        if not self.unit:
            raise ValueError("PriceRow.unit is required")
        object.__setattr__(self, "quantity", _dec(self.quantity, "PriceRow.quantity"))
        object.__setattr__(self, "qty_base", _dec(self.qty_base, "PriceRow.qty_base"))
        object.__setattr__(self, "sheet_refs", _str_tuple(self.sheet_refs))
        object.__setattr__(self, "wastage_applies", bool(self.wastage_applies))
        _check_enum(self.quantity_basis, QUANTITY_BASES, "PriceRow.quantity_basis", nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "description": self.description,
            "group": self.group,
            "subgroup": self.subgroup,
            "section": self.section,
            "unit": self.unit,
            "quantity": _s(self.quantity),
            "qty_base": _s(self.qty_base),
            "wastage_applies": self.wastage_applies,
            "material_note": self.material_note,
            "caveat": self.caveat,
            "sheet_refs": list(self.sheet_refs),
            "quantity_basis": self.quantity_basis,
            "item_key": self.item_key,
            "material_category": self.material_category,
            "area": self.area,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PriceRow:
        return cls(
            line_id=data["line_id"],
            description=data.get("description") or "",
            group=data.get("group") or "",
            subgroup=data.get("subgroup"),
            section=data.get("section"),
            unit=data["unit"],
            quantity=data["quantity"],
            qty_base=data.get("qty_base", data["quantity"]),
            wastage_applies=bool(data.get("wastage_applies", False)),
            material_note=data.get("material_note"),
            caveat=data.get("caveat"),
            sheet_refs=data.get("sheet_refs") or (),
            quantity_basis=data.get("quantity_basis"),
            item_key=data.get("item_key"),
            material_category=data.get("material_category"),
            area=data.get("area"),
        )


@dataclass(frozen=True)
class SpecFact:
    """A spec or scope fact that can move a price (a hospital-grade device,
    a conduit type). `source_ref` cites where it came from (WS5 F<n> or a
    p: / sp: ref); a grade seed applies only with a cited fact."""

    key: str
    value: str
    source_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "source_ref": self.source_ref}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpecFact:
        return cls(key=data["key"], value=str(data["value"]), source_ref=data.get("source_ref"))


@dataclass(frozen=True)
class Allowance:
    """One allowance row of the estimate (the policy's standard rows are $0
    unless a document names an amount)."""

    name: str
    amount_usd: Decimal = Decimal(0)
    pricing_category: str | None = None
    source_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount_usd", _dec(self.amount_usd, "Allowance.amount_usd"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "amount_usd": _s(self.amount_usd),
            "pricing_category": self.pricing_category,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Allowance:
        return cls(
            name=data["name"],
            amount_usd=data.get("amount_usd", 0),
            pricing_category=data.get("pricing_category"),
            source_ref=data.get("source_ref"),
        )


def _steps(raw: Iterable[Any]) -> tuple[tuple[int | None, int], ...]:
    out: list[tuple[int | None, int]] = []
    for item in raw:
        bound, step = item
        out.append((None if bound is None else int(bound), int(step)))
    if not out:
        raise ValueError("hour_roundup_steps: at least one step is required")
    bounds = [b for b, _ in out]
    if bounds[-1] is not None or any(b is None for b in bounds[:-1]):
        raise ValueError("hour_roundup_steps: only the last bound is None")
    finite = [b for b in bounds if b is not None]
    if finite != sorted(finite) or len(set(finite)) != len(finite):
        raise ValueError("hour_roundup_steps: bounds must strictly ascend")
    if any(s <= 0 for _, s in out):
        raise ValueError("hour_roundup_steps: steps must be positive")
    return tuple(out)


@dataclass(frozen=True)
class PricingContext:
    """Everything about the bid (not the BOQ) that the engine prices with.

    `rate_overrides` maps a GC id to a labor rate; `exclude_project_ids` are
    the projects left out of the library (leave-one-out eval, holdout)."""

    wage_type: str | None
    labor_rate_usd: Decimal
    labor_rate_source: str
    rate_overrides: Mapping[str, Decimal] = field(default_factory=dict)
    project_gc_ids: tuple[str, ...] = ()
    spec_facts: tuple[SpecFact, ...] = ()
    allowances: tuple[Allowance, ...] = ()
    policy_version: str | None = None
    exclude_project_ids: frozenset[str] = frozenset()
    eval_case_id: str | None = None
    tax_rate_pct: Decimal = DEFAULT_TAX_RATE_PCT
    material_markup_pct: Decimal = DEFAULT_MATERIAL_MARKUP_PCT
    labor_markup_pct: Decimal = DEFAULT_LABOR_MARKUP_PCT
    hour_roundup_steps: tuple[tuple[int | None, int], ...] = DEFAULT_HOUR_ROUNDUP_STEPS

    def __post_init__(self) -> None:
        _check_enum(self.wage_type, WAGE_TYPES, "PricingContext.wage_type", nullable=True)
        set_ = object.__setattr__
        set_(self, "labor_rate_usd", _dec(self.labor_rate_usd, "labor_rate_usd"))
        set_(
            self,
            "rate_overrides",
            {str(k): _dec(v, f"rate_overrides[{k}]") for k, v in self.rate_overrides.items()},
        )
        set_(self, "project_gc_ids", _str_tuple(self.project_gc_ids))
        set_(
            self,
            "spec_facts",
            tuple(f if isinstance(f, SpecFact) else SpecFact.from_dict(f) for f in self.spec_facts),
        )
        set_(
            self,
            "allowances",
            tuple(
                a if isinstance(a, Allowance) else Allowance.from_dict(a) for a in self.allowances
            ),
        )
        set_(self, "exclude_project_ids", frozenset(str(i) for i in self.exclude_project_ids))
        set_(self, "tax_rate_pct", _dec(self.tax_rate_pct, "tax_rate_pct"))
        set_(self, "material_markup_pct", _dec(self.material_markup_pct, "material_markup_pct"))
        set_(self, "labor_markup_pct", _dec(self.labor_markup_pct, "labor_markup_pct"))
        set_(self, "hour_roundup_steps", _steps(self.hour_roundup_steps))

    @property
    def excluded_project_ids_sha256(self) -> str:
        return excluded_projects_sha256(self.exclude_project_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wage_type": self.wage_type,
            "labor_rate_usd": _s(self.labor_rate_usd),
            "labor_rate_source": self.labor_rate_source,
            "rate_overrides": {k: _s(v) for k, v in sorted(self.rate_overrides.items())},
            "project_gc_ids": list(self.project_gc_ids),
            "spec_facts": [f.to_dict() for f in self.spec_facts],
            "allowances": [a.to_dict() for a in self.allowances],
            "policy_version": self.policy_version,
            "exclude_project_ids": sorted(self.exclude_project_ids),
            "eval_case_id": self.eval_case_id,
            "tax_rate_pct": _s(self.tax_rate_pct),
            "material_markup_pct": _s(self.material_markup_pct),
            "labor_markup_pct": _s(self.labor_markup_pct),
            "hour_roundup_steps": [[b, s] for b, s in self.hour_roundup_steps],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PricingContext:
        return cls(
            wage_type=data.get("wage_type"),
            labor_rate_usd=data["labor_rate_usd"],
            labor_rate_source=data.get("labor_rate_source") or "",
            rate_overrides=data.get("rate_overrides") or {},
            project_gc_ids=data.get("project_gc_ids") or (),
            spec_facts=tuple(SpecFact.from_dict(f) for f in data.get("spec_facts") or ()),
            allowances=tuple(Allowance.from_dict(a) for a in data.get("allowances") or ()),
            policy_version=data.get("policy_version"),
            exclude_project_ids=frozenset(data.get("exclude_project_ids") or ()),
            eval_case_id=data.get("eval_case_id"),
            tax_rate_pct=data.get("tax_rate_pct", DEFAULT_TAX_RATE_PCT),
            material_markup_pct=data.get("material_markup_pct", DEFAULT_MATERIAL_MARKUP_PCT),
            labor_markup_pct=data.get("labor_markup_pct", DEFAULT_LABOR_MARKUP_PCT),
            hour_roundup_steps=data.get("hour_roundup_steps") or DEFAULT_HOUR_ROUNDUP_STEPS,
        )


@dataclass(frozen=True)
class QuoteRef:
    """A vendor quote the engine may price a category from."""

    quote_id: str
    category: str
    vendor: str
    amount_usd: Decimal
    tax_included: bool = False
    replaces_sections: tuple[str, ...] = ()
    file_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.quote_id:
            raise ValueError("QuoteRef.quote_id is required")
        object.__setattr__(self, "amount_usd", _dec(self.amount_usd, "QuoteRef.amount_usd"))
        object.__setattr__(self, "replaces_sections", _str_tuple(self.replaces_sections))
        object.__setattr__(self, "tax_included", bool(self.tax_included))

    @property
    def material_source(self) -> str:
        """The `PricedLine.material_source` value for a line priced from it."""
        return f"{QUOTE_SOURCE_PREFIX}{self.quote_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "category": self.category,
            "vendor": self.vendor,
            "amount_usd": _s(self.amount_usd),
            "tax_included": self.tax_included,
            "replaces_sections": list(self.replaces_sections),
            "file_sha256": self.file_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QuoteRef:
        return cls(
            quote_id=str(data["quote_id"]),
            category=data["category"],
            vendor=data.get("vendor") or "",
            amount_usd=data["amount_usd"],
            tax_included=bool(data.get("tax_included", False)),
            replaces_sections=data.get("replaces_sections") or (),
            file_sha256=data.get("file_sha256"),
        )


# Outputs ------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueStat:
    """One priced unit value (unit hours or unit material USD) with its
    spread and provenance. `latest_seen` is "YYYY-MM" or None."""

    value: Decimal | None
    p25: Decimal | None = None
    p75: Decimal | None = None
    n_obs: int = 0
    n_projects: int = 0
    latest_seen: str | None = None
    quality: str = "missing"
    method: str = "none"

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _dec_opt(self.value, "ValueStat.value"))
        object.__setattr__(self, "p25", _dec_opt(self.p25, "ValueStat.p25"))
        object.__setattr__(self, "p75", _dec_opt(self.p75, "ValueStat.p75"))
        object.__setattr__(self, "n_obs", int(self.n_obs))
        object.__setattr__(self, "n_projects", int(self.n_projects))
        if self.n_obs < 0 or self.n_projects < 0:
            raise ValueError("ValueStat: n_obs and n_projects must be >= 0")
        _check_enum(self.quality, PRICE_QUALITIES, "ValueStat.quality")
        _check_enum(self.method, VALUE_METHODS, "ValueStat.method")
        ls = self.latest_seen
        if ls is not None and not (
            isinstance(ls, str)
            and len(ls) == 7
            and ls[4] == "-"
            and ls[:4].isdigit()
            and ls[5:].isdigit()
            and 1 <= int(ls[5:]) <= 12
        ):
            raise ValueError(f"ValueStat.latest_seen: expected YYYY-MM, got {ls!r}")

    @classmethod
    def missing(cls) -> ValueStat:
        return cls(value=None, quality="missing", method="none")

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": _s(self.value),
            "p25": _s(self.p25),
            "p75": _s(self.p75),
            "n_obs": self.n_obs,
            "n_projects": self.n_projects,
            "latest_seen": self.latest_seen,
            "quality": self.quality,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ValueStat:
        return cls(
            value=data.get("value"),
            p25=data.get("p25"),
            p75=data.get("p75"),
            n_obs=data.get("n_obs", 0),
            n_projects=data.get("n_projects", 0),
            latest_seen=data.get("latest_seen"),
            quality=data.get("quality", "missing"),
            method=data.get("method", "none"),
        )


@dataclass
class PricedLine:
    """One BOQ line after pricing. `labor` is unit hours, `material` unit
    material USD; `labor_hours` and `material_cost` are the extended values
    the engine computed (None when unpriced). `candidates` are JSON-safe
    library match candidates and never carry project or GC names."""

    line_id: str
    row: PriceRow
    item_key: str | None
    family: str | None
    uom: str
    qty_eff: Decimal
    labor: ValueStat
    material: ValueStat
    modifiers: tuple[str, ...] = ()
    material_source: str = "none"
    labor_hours: Decimal | None = None
    material_cost: Decimal | None = None
    pricing_category: str | None = None
    pricing_section: str | None = None
    flags: tuple[str, ...] = ()
    candidates: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.line_id:
            raise ValueError("PricedLine.line_id is required")
        if self.line_id != self.row.line_id:
            raise ValueError(
                f"PricedLine.line_id {self.line_id!r} != row.line_id {self.row.line_id!r}"
            )
        if not is_valid_material_source(self.material_source):
            raise ValueError(
                f"PricedLine.material_source: {self.material_source!r} is not one of "
                f"{', '.join(PRICED_LINE_MATERIAL_SOURCES)} or quote:<id>"
            )
        self.qty_eff = _dec(self.qty_eff, "PricedLine.qty_eff")
        self.labor_hours = _dec_opt(self.labor_hours, "PricedLine.labor_hours")
        self.material_cost = _dec_opt(self.material_cost, "PricedLine.material_cost")
        self.modifiers = _str_tuple(self.modifiers)
        self.flags = _str_tuple(self.flags)
        self.candidates = tuple(dict(c) for c in self.candidates)

    @property
    def quote_id(self) -> str | None:
        return quote_id_of(self.material_source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "row": self.row.to_dict(),
            "item_key": self.item_key,
            "family": self.family,
            "uom": self.uom,
            "qty_eff": _s(self.qty_eff),
            "labor": self.labor.to_dict(),
            "material": self.material.to_dict(),
            "modifiers": list(self.modifiers),
            "material_source": self.material_source,
            "labor_hours": _s(self.labor_hours),
            "material_cost": _s(self.material_cost),
            "pricing_category": self.pricing_category,
            "pricing_section": self.pricing_section,
            "flags": list(self.flags),
            "candidates": [_json_safe(c) for c in self.candidates],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PricedLine:
        return cls(
            line_id=data["line_id"],
            row=PriceRow.from_dict(data["row"]),
            item_key=data.get("item_key"),
            family=data.get("family"),
            uom=data["uom"],
            qty_eff=data["qty_eff"],
            labor=ValueStat.from_dict(data.get("labor") or {}),
            material=ValueStat.from_dict(data.get("material") or {}),
            modifiers=data.get("modifiers") or (),
            material_source=data.get("material_source", "none"),
            labor_hours=data.get("labor_hours"),
            material_cost=data.get("material_cost"),
            pricing_category=data.get("pricing_category"),
            pricing_section=data.get("pricing_section"),
            flags=data.get("flags") or (),
            candidates=tuple(data.get("candidates") or ()),
        )


@dataclass
class VerifyDraft:
    """The Verify step numbers the engine proposes: the ten `VERIFY_NUMBERS`
    plus the bid total and the labor basis. The four legacy numbers are
    always set; a section-scoped number is None when that section is absent
    (the convention of `routers/pricing.py`)."""

    labor_amount: Decimal = Decimal(0)
    materials_amount: Decimal = Decimal(0)
    gear_amount: Decimal | None = None
    underground_amount: Decimal | None = None
    low_voltage_amount: Decimal | None = None
    labor_markup_amount: Decimal = Decimal(0)
    materials_markup_amount: Decimal = Decimal(0)
    gear_markup_amount: Decimal | None = None
    underground_markup_amount: Decimal | None = None
    low_voltage_markup_amount: Decimal | None = None
    bid_total: Decimal = Decimal(0)
    labor_hours_basis: Decimal = Decimal(0)
    labor_hours_reviewer: Decimal = Decimal(0)
    labor_rate: Decimal = Decimal(0)
    labor_rate_source: str = ""

    def __post_init__(self) -> None:
        for key in VERIFY_NUMBERS:
            raw = getattr(self, key)
            if key in VERIFY_SECTION_NUMBERS:
                setattr(self, key, _dec_opt(raw, f"VerifyDraft.{key}"))
            else:
                if raw is None:
                    raise ValueError(f"VerifyDraft.{key} cannot be None")
                setattr(self, key, _dec(raw, f"VerifyDraft.{key}"))
        for key in ("bid_total", "labor_hours_basis", "labor_hours_reviewer", "labor_rate"):
            setattr(self, key, _dec(getattr(self, key), f"VerifyDraft.{key}"))

    def numbers(self) -> dict[str, Decimal | None]:
        """The ten Verify numbers in `VERIFY_NUMBERS` order."""
        return {key: getattr(self, key) for key in VERIFY_NUMBERS}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {key: _s(val) for key, val in self.numbers().items()}
        out["bid_total"] = _s(self.bid_total)
        out["labor_hours_basis"] = _s(self.labor_hours_basis)
        out["labor_hours_reviewer"] = _s(self.labor_hours_reviewer)
        out["labor_rate"] = _s(self.labor_rate)
        out["labor_rate_source"] = self.labor_rate_source
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerifyDraft:
        kwargs: dict[str, Any] = {}
        for key in VERIFY_NUMBERS:
            if key in data:
                kwargs[key] = data[key]
        for key in ("bid_total", "labor_hours_basis", "labor_hours_reviewer", "labor_rate"):
            if key in data:
                kwargs[key] = data[key]
        kwargs["labor_rate_source"] = data.get("labor_rate_source") or ""
        return cls(**kwargs)


@dataclass(frozen=True)
class PricingIssue:
    """A blocker or warning of a priced estimate: a stable code, a human
    sentence and the BOQ lines it concerns."""

    code: str
    detail: str = ""
    line_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("PricingIssue.code is required")
        object.__setattr__(self, "line_ids", _str_tuple(self.line_ids))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "line_ids": list(self.line_ids)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | str) -> PricingIssue:
        if isinstance(data, str):
            return cls(code=data)
        return cls(
            code=data["code"],
            detail=data.get("detail") or "",
            line_ids=data.get("line_ids") or (),
        )


@dataclass
class UnpricedSummary:
    """Lines priced only at a fallback or not at all, with the hours and
    material the engine estimates for them (reported beside the Verify draft)."""

    lines: tuple[str, ...] = ()
    est_hours: Decimal = Decimal(0)
    est_material: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        self.lines = _str_tuple(self.lines)
        self.est_hours = _dec(self.est_hours, "UnpricedSummary.est_hours")
        self.est_material = _dec(self.est_material, "UnpricedSummary.est_material")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines": list(self.lines),
            "est_hours": _s(self.est_hours),
            "est_material": _s(self.est_material),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UnpricedSummary:
        return cls(
            lines=data.get("lines") or (),
            est_hours=data.get("est_hours", 0),
            est_material=data.get("est_material", 0),
        )


@dataclass
class PricedEstimate:
    """The engine's result for one BOQ version (spec 5.4).

    `categories`, `workbook_basis` and `quality_summary` are JSON-safe
    mappings whose inner shape WS6 owns; Decimals inside them serialize as
    strings and come back as strings."""

    status: str
    lines: list[PricedLine] = field(default_factory=list)
    categories: dict[str, Any] = field(default_factory=dict)
    verify: VerifyDraft | None = None
    workbook_basis: dict[str, Any] | None = None
    range_low: Decimal | None = None
    range_high: Decimal | None = None
    quality_summary: dict[str, Any] = field(default_factory=dict)
    unpriced: UnpricedSummary = field(default_factory=UnpricedSummary)
    blockers: list[PricingIssue] = field(default_factory=list)
    warnings: list[PricingIssue] = field(default_factory=list)
    policy_version: str | None = None
    library_fingerprint: str | None = None
    excluded_project_ids_sha256: str | None = None

    def __post_init__(self) -> None:
        _check_enum(self.status, ESTIMATE_STATUSES, "PricedEstimate.status")
        self.range_low = _dec_opt(self.range_low, "PricedEstimate.range_low")
        self.range_high = _dec_opt(self.range_high, "PricedEstimate.range_high")
        seen: set[str] = set()
        for line in self.lines:
            if line.line_id in seen:
                raise ValueError(f"PricedEstimate: duplicate line_id {line.line_id!r}")
            seen.add(line.line_id)

    def line(self, line_id: str) -> PricedLine | None:
        for item in self.lines:
            if item.line_id == line_id:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "lines": [ln.to_dict() for ln in self.lines],
            "categories": _json_safe(self.categories),
            "verify": None if self.verify is None else self.verify.to_dict(),
            "workbook_basis": _json_safe(self.workbook_basis),
            "range_low": _s(self.range_low),
            "range_high": _s(self.range_high),
            "quality_summary": _json_safe(self.quality_summary),
            "unpriced": self.unpriced.to_dict(),
            "blockers": [b.to_dict() for b in self.blockers],
            "warnings": [w.to_dict() for w in self.warnings],
            "policy_version": self.policy_version,
            "library_fingerprint": self.library_fingerprint,
            "excluded_project_ids_sha256": self.excluded_project_ids_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PricedEstimate:
        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(f"PricedEstimate: unsupported schema_version {version!r}")
        verify = data.get("verify")
        return cls(
            status=data["status"],
            lines=[PricedLine.from_dict(ln) for ln in data.get("lines") or ()],
            categories=dict(data.get("categories") or {}),
            verify=None if verify is None else VerifyDraft.from_dict(verify),
            workbook_basis=data.get("workbook_basis"),
            range_low=data.get("range_low"),
            range_high=data.get("range_high"),
            quality_summary=dict(data.get("quality_summary") or {}),
            unpriced=UnpricedSummary.from_dict(data.get("unpriced") or {}),
            blockers=[PricingIssue.from_dict(b) for b in data.get("blockers") or ()],
            warnings=[PricingIssue.from_dict(w) for w in data.get("warnings") or ()],
            policy_version=data.get("policy_version"),
            library_fingerprint=data.get("library_fingerprint"),
            excluded_project_ids_sha256=data.get("excluded_project_ids_sha256"),
        )
