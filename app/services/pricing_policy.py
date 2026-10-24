"""G3 pricing policy: the numbers a human sets, not the library's
(AI_ESTIMATING_BUILD_SPEC WS6 `pricing_policy`, owner answers Q5, Q6, Q10,
Q11).

The policy is a plain document (`PricingPolicy.to_dict` / `from_dict`) so the
`ai_pricing_settings.policy` row of migration ME can store it and VERIFY_ROLES
can edit it through the settings route; the code defaults below are what a
missing row means.

- labor rates by wage type (Q5): prevailing wage 125, non-prevailing 100, with
  per-GC overrides keyed by `general_contractors.id`; a bid that goes to
  several GCs with different effective rates uses the default rate and warns
  `multi_gc_rate`;
- material markup 10% per section and labor markup 0 (Q6), both EDITABLE
  fields of the document;
- tax 8.375% on material only (`pricing_formula.DEFAULT_TAX_RATE`);
- the reviewer hour round-up `nice_ceil` with the steps of spec WS6
  [[200, 10], [1000, 25], [2000, 50], [5000, 100], [10000, 500], [null, 1000]];
- the hour uplift, calibrated by `scripts/pricing_eval.py --mode calibrate`
  against ENGINE basis hours on the training pool only, clean band 0.95 to
  1.6 (`calibrate_uplift` is the pure calculation), one value per coverage
  regime of the estimate (`coverage_class`): `covered` when the engine priced
  at most `fallback_share_max` of the basis hours at a fallback and at most
  `unpriced_line_share_max` of the lines at nothing, else `under_covered`;
  `calibrate_uplift_by_coverage` derives both and falls back to the pooled
  value for a regime with fewer than three clean pairs;
- grade seeds (Q11): hospital-grade duplex and GFCI unit material, keyed on
  both the hospital-grade library key and the base key, applied by the engine
  only with a cited SpecFact (`grade_seed_for`); every line priced from a seed
  is labeled `policy`.

Pure and sync: no I/O, no database, no model.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, Decimal
from typing import Any

from app.services.cost_library import DEFAULT_THRESHOLDS, QualityThresholds
from app.services.estimating_contracts import WAGE_TYPES
from app.services.pricing_formula import DEFAULT_TAX_RATE, SECTION_KEYS
from app.services.pricing_types import DEFAULT_HOUR_ROUNDUP_STEPS, SpecFact

POLICY_VERSION = 1

DEFAULT_LABOR_RATES: dict[str, Decimal] = {
    "prevailing_wage": Decimal("125"),
    "non_prevailing_wage": Decimal("100"),
}
DEFAULT_WAGE_TYPE = "prevailing_wage"  # Q13: unknown wage type prices as PW with a review flag
DEFAULT_MATERIAL_MARKUP_PCT = Decimal("10")
DEFAULT_LABOR_MARKUP_PCT = Decimal("0")
# Hour uplift (G3 reviewer hours over engine basis hours), one value per
# coverage regime. Re-derived 2026-10-22 by scripts/pricing_eval.py --mode
# calibrate with every library built through
# eval_manifest.excluded_projects("pricing_library", case_id) (holdout and the
# pair's own project out) over the 23 training pairs (golden pool and smoke,
# never core or holdout), clean band 0.95 to 1.6:
#
# - the single 1.20 of 2026-10-21 had gone stale with the library code (the
#   same calibration on the current library gives 1.15, 13 clean of 23) and it
#   conflated two things: on estimates the engine covers well the ratio is the
#   reviewer's own round-up, on under-covered estimates (fallback-priced or
#   unpriced work) the ratio also carries the hours the basis misses;
# - covered regime (fallback hours share <= 10% and unpriced lines <= 10%):
#   10 pairs, 9 clean, median 1.10 (p25 0.99, p75 1.17), LOO median abs error
#   5.4% against 8.3% for the single uplift on the same pairs;
# - under-covered regime: 13 pairs, only 4 clean (the rest sit above 1.6
#   because the basis misses a third or more of the work), median 1.32 (p25
#   1.22, p75 1.46); thin, and such an estimate is provisional anyway;
# - LOO over all 23 pairs: median abs error 15.4% against 23.1%, clean-band
#   pairs 7.1% against 8.3% (within 10%: 69% against 62%).
# The settings row replaces both after migration ME.
DEFAULT_HOUR_UPLIFT = Decimal("1.10")
DEFAULT_HOUR_UPLIFT_RANGE: tuple[Decimal, Decimal] = (Decimal("0.99"), Decimal("1.17"))
DEFAULT_HOUR_UPLIFT_UNDER_COVERED = Decimal("1.32")
DEFAULT_HOUR_UPLIFT_UNDER_COVERED_RANGE: tuple[Decimal, Decimal] = (Decimal("1.22"), Decimal("1.46"))
DEFAULT_CLEAN_BAND: tuple[Decimal, Decimal] = (Decimal("0.95"), Decimal("1.6"))
# Coverage regimes of an estimate (which uplift applies).
COVERAGE_COVERED = "covered"
COVERAGE_UNDER_COVERED = "under_covered"
COVERAGE_CLASSES: tuple[str, ...] = (COVERAGE_COVERED, COVERAGE_UNDER_COVERED)
# Share of priced lines the library matched to nothing before the estimate
# counts as under-covered (the hours side; the status rule is the engine's).
DEFAULT_UNPRICED_LINE_SHARE_MAX = Decimal("0.10")
# The estimator template's rate and OH&P (workbook basis for comparisons).
DEFAULT_WORKBOOK_LABOR_RATE = Decimal("108")
DEFAULT_WORKBOOK_OHP_PCT: dict[str, Decimal] = {"labor": Decimal("25"), "material": Decimal("25")}
# Categories whose material is a placeholder until a vendor quote exists.
DEFAULT_QUOTE_REQUIRED_CATEGORIES: tuple[str, ...] = ("Lighting", "Switchgear")
# What a quote replaces in its category: material | material_and_labor | adds.
DEFAULT_QUOTE_REPLACES: dict[str, str] = {
    "Lighting": "material",
    "Switchgear": "material",
    "Generator & Equipment": "material",
    "Trenching": "material",
    "Low Voltage": "material",
    "EV Chargers": "material",
    "Solar": "material",
    "Demolition": "material",
    "General Material": "material",
    "Other": "material",
}
QUOTE_REPLACE_MODES: tuple[str, ...] = ("material", "material_and_labor", "adds")
# Pricing section per material category (material_categories.pricing_section in
# the backup; everything else is the residual materials section).
DEFAULT_CATEGORY_SECTIONS: dict[str, str] = {
    "Switchgear": "gear",
    "Generator & Equipment": "gear",
    "Trenching": "underground",
    "Low Voltage": "low_voltage",
}
# Share of basis hours or material dollars priced at a fallback before the
# estimate is only provisional.
DEFAULT_FALLBACK_SHARE_MAX = Decimal("0.10")
ALLOWANCE_SECTION = "materials"  # Q10: allowances untaxed, in materials, marked up

# Q11 grade seeds, owner approved until real observations exist. Keys are
# cost_library.library_key values: the base key and the hospital-grade key of
# the same device. The code default carries the KEYS and the fact only, never
# an amount (spec 0.2: a seed unit cost is estimator pricing data and stays
# out of the repo); a seed without `material_usd` is inert. The settings row
# (migration ME) supplies the amounts in production and scripts/pricing_eval.py
# reads them from EVAL_ROOT/references/7154/expected.json seeds.* for the
# 7154 counterfactual.
_HG = "grade=hospital"
DEFAULT_GRADE_SEEDS: tuple[GradeSeed, ...] = ()  # filled below GradeSeed


@dataclass(frozen=True)
class GradeSeed:
    """A policy unit material for a device grade, applied only with a cited
    fact `fact_key=fact_value` (spec facts come from WS5)."""

    item_keys: tuple[str, ...]
    fact_key: str
    fact_value: str
    material_usd: Decimal | None = None  # None: the seed is inert until an amount is supplied
    labor_factor: Decimal = Decimal("1")
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_keys", tuple(str(k) for k in self.item_keys))
        if self.material_usd is not None:
            object.__setattr__(self, "material_usd", Decimal(str(self.material_usd)))
        object.__setattr__(self, "labor_factor", Decimal(str(self.labor_factor)))

    @property
    def active(self) -> bool:
        """A seed prices only when it carries an amount."""
        return self.material_usd is not None

    def with_amount(self, material_usd: Any) -> GradeSeed:
        """The same seed with an amount (the settings row or the eval script)."""
        return GradeSeed(
            item_keys=self.item_keys, fact_key=self.fact_key, fact_value=self.fact_value,
            material_usd=material_usd, labor_factor=self.labor_factor, note=self.note,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_keys": list(self.item_keys),
            "fact_key": self.fact_key,
            "fact_value": self.fact_value,
            "material_usd": None if self.material_usd is None else str(self.material_usd),
            "labor_factor": str(self.labor_factor),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GradeSeed:
        return cls(
            item_keys=tuple(data.get("item_keys") or ()),
            fact_key=str(data["fact_key"]),
            fact_value=str(data["fact_value"]),
            material_usd=data.get("material_usd"),
            labor_factor=data.get("labor_factor", "1"),
            note=str(data.get("note") or ""),
        )


DEFAULT_GRADE_SEEDS = (
    GradeSeed(
        item_keys=("receptacle:duplex|mod=DUPLEX", f"receptacle:duplex|{_HG}|mod=DUPLEX"),
        fact_key="device_grade",
        fact_value="hospital",
        note="Q11 owner-approved hospital-grade duplex seed (amount from settings)",
    ),
    GradeSeed(
        item_keys=(
            "receptacle:duplex|mod=DUPLEX|mod=GFCI",
            f"receptacle:duplex|{_HG}|mod=DUPLEX|mod=GFCI",
        ),
        fact_key="device_grade",
        fact_value="hospital",
        note="Q11 owner-approved hospital-grade GFCI seed (amount from settings)",
    ),
)
# Fact keys that mean the same thing as device_grade (WS5 may spell either).
_GRADE_FACT_KEYS = frozenset({"device_grade", "grade", "receptacle_grade"})
_GRADE_VALUE_ALIASES = {"hospital_grade": "hospital", "hg": "hospital", "hospital grade": "hospital"}


def _dec(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name}: expected a number")
    out = value if isinstance(value, Decimal) else Decimal(str(value))
    if not out.is_finite():
        raise ValueError(f"{name}: not finite")
    return out


def _steps(raw: Iterable[Any]) -> tuple[tuple[int | None, int], ...]:
    out: list[tuple[int | None, int]] = []
    for bound, step in raw:
        out.append((None if bound is None else int(bound), int(step)))
    if not out or out[-1][0] is not None or any(b is None for b, _s in out[:-1]):
        raise ValueError("hour_roundup_steps: ascending bounds with only the last None")
    bounds = [b for b, _s in out if b is not None]
    if bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
        raise ValueError("hour_roundup_steps: bounds must strictly ascend")
    if any(s <= 0 for _b, s in out):
        raise ValueError("hour_roundup_steps: steps must be positive")
    return tuple(out)


@dataclass(frozen=True)
class PricingPolicy:
    """The policy document. Every field is editable through the settings row;
    `validate` runs on construction."""

    version: int = POLICY_VERSION
    labor_rates: Mapping[str, Decimal] = field(default_factory=lambda: dict(DEFAULT_LABOR_RATES))
    rate_overrides: Mapping[str, Decimal] = field(default_factory=dict)  # gc_id -> rate
    default_wage_type: str = DEFAULT_WAGE_TYPE
    material_markup_pct: Mapping[str, Decimal] = field(
        default_factory=lambda: {k: DEFAULT_MATERIAL_MARKUP_PCT for k in SECTION_KEYS}
    )
    labor_markup_pct: Decimal = DEFAULT_LABOR_MARKUP_PCT
    tax_rate_pct: Decimal = DEFAULT_TAX_RATE
    hour_roundup_steps: tuple[tuple[int | None, int], ...] = DEFAULT_HOUR_ROUNDUP_STEPS
    hour_uplift: Decimal = DEFAULT_HOUR_UPLIFT  # covered regime
    hour_uplift_range: tuple[Decimal, Decimal] = DEFAULT_HOUR_UPLIFT_RANGE
    hour_uplift_under_covered: Decimal = DEFAULT_HOUR_UPLIFT_UNDER_COVERED
    hour_uplift_under_covered_range: tuple[Decimal, Decimal] = DEFAULT_HOUR_UPLIFT_UNDER_COVERED_RANGE
    unpriced_line_share_max: Decimal = DEFAULT_UNPRICED_LINE_SHARE_MAX
    clean_band: tuple[Decimal, Decimal] = DEFAULT_CLEAN_BAND
    workbook_labor_rate: Decimal = DEFAULT_WORKBOOK_LABOR_RATE
    workbook_ohp_pct: Mapping[str, Decimal] = field(
        default_factory=lambda: dict(DEFAULT_WORKBOOK_OHP_PCT)
    )
    quote_required_categories: tuple[str, ...] = DEFAULT_QUOTE_REQUIRED_CATEGORIES
    quote_replaces: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_QUOTE_REPLACES))
    category_sections: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_SECTIONS)
    )
    fallback_share_max: Decimal = DEFAULT_FALLBACK_SHARE_MAX
    allowance_section: str = ALLOWANCE_SECTION
    grade_seeds: tuple[GradeSeed, ...] = DEFAULT_GRADE_SEEDS
    quality: QualityThresholds = DEFAULT_THRESHOLDS
    note: str = ""

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "labor_rates", {str(k): _dec(v, f"labor_rates[{k}]") for k, v in self.labor_rates.items()})
        for wt in WAGE_TYPES:
            if wt not in self.labor_rates:
                raise ValueError(f"labor_rates: missing {wt}")
        set_(self, "rate_overrides", {str(k): _dec(v, f"rate_overrides[{k}]") for k, v in self.rate_overrides.items()})
        if self.default_wage_type not in WAGE_TYPES:
            raise ValueError(f"default_wage_type: {self.default_wage_type!r}")
        markups = {str(k): _dec(v, f"material_markup_pct[{k}]") for k, v in self.material_markup_pct.items()}
        for key in SECTION_KEYS:
            markups.setdefault(key, DEFAULT_MATERIAL_MARKUP_PCT)
        set_(self, "material_markup_pct", markups)
        set_(self, "labor_markup_pct", _dec(self.labor_markup_pct, "labor_markup_pct"))
        set_(self, "tax_rate_pct", _dec(self.tax_rate_pct, "tax_rate_pct"))
        set_(self, "hour_roundup_steps", _steps(self.hour_roundup_steps))
        set_(self, "hour_uplift", _dec(self.hour_uplift, "hour_uplift"))
        if self.hour_uplift <= 0:
            raise ValueError("hour_uplift must be positive")
        lo, hi = self.hour_uplift_range
        set_(self, "hour_uplift_range", (_dec(lo, "hour_uplift_range[0]"), _dec(hi, "hour_uplift_range[1]")))
        set_(self, "hour_uplift_under_covered", _dec(self.hour_uplift_under_covered, "hour_uplift_under_covered"))
        if self.hour_uplift_under_covered <= 0:
            raise ValueError("hour_uplift_under_covered must be positive")
        lo, hi = self.hour_uplift_under_covered_range
        set_(self, "hour_uplift_under_covered_range",
             (_dec(lo, "hour_uplift_under_covered_range[0]"), _dec(hi, "hour_uplift_under_covered_range[1]")))
        set_(self, "unpriced_line_share_max", _dec(self.unpriced_line_share_max, "unpriced_line_share_max"))
        if not (0 <= self.unpriced_line_share_max <= 1):
            raise ValueError("unpriced_line_share_max must lie in 0..1")
        lo, hi = self.clean_band
        set_(self, "clean_band", (_dec(lo, "clean_band[0]"), _dec(hi, "clean_band[1]")))
        set_(self, "workbook_labor_rate", _dec(self.workbook_labor_rate, "workbook_labor_rate"))
        set_(self, "workbook_ohp_pct", {str(k): _dec(v, f"workbook_ohp_pct[{k}]") for k, v in self.workbook_ohp_pct.items()})
        set_(self, "quote_required_categories", tuple(str(c) for c in self.quote_required_categories))
        replaces = {str(k): str(v) for k, v in self.quote_replaces.items()}
        for cat, mode in replaces.items():
            if mode not in QUOTE_REPLACE_MODES:
                raise ValueError(f"quote_replaces[{cat}]: {mode!r} not in {QUOTE_REPLACE_MODES}")
        set_(self, "quote_replaces", replaces)
        sections = {str(k): str(v) for k, v in self.category_sections.items()}
        for cat, sec in sections.items():
            if sec not in SECTION_KEYS:
                raise ValueError(f"category_sections[{cat}]: {sec!r} not in {SECTION_KEYS}")
        set_(self, "category_sections", sections)
        set_(self, "fallback_share_max", _dec(self.fallback_share_max, "fallback_share_max"))
        if self.allowance_section not in SECTION_KEYS:
            raise ValueError(f"allowance_section: {self.allowance_section!r}")
        set_(self, "grade_seeds", tuple(
            s if isinstance(s, GradeSeed) else GradeSeed.from_dict(s) for s in self.grade_seeds
        ))
        if not isinstance(self.quality, QualityThresholds):
            set_(self, "quality", QualityThresholds.from_dict(self.quality))

    # Document ---------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "labor_rates": {k: str(v) for k, v in sorted(self.labor_rates.items())},
            "rate_overrides": {k: str(v) for k, v in sorted(self.rate_overrides.items())},
            "default_wage_type": self.default_wage_type,
            "material_markup_pct": {k: str(v) for k, v in sorted(self.material_markup_pct.items())},
            "labor_markup_pct": str(self.labor_markup_pct),
            "tax_rate_pct": str(self.tax_rate_pct),
            "hour_roundup_steps": [[b, s] for b, s in self.hour_roundup_steps],
            "hour_uplift": str(self.hour_uplift),
            "hour_uplift_range": [str(self.hour_uplift_range[0]), str(self.hour_uplift_range[1])],
            "hour_uplift_under_covered": str(self.hour_uplift_under_covered),
            "hour_uplift_under_covered_range": [
                str(self.hour_uplift_under_covered_range[0]), str(self.hour_uplift_under_covered_range[1]),
            ],
            "unpriced_line_share_max": str(self.unpriced_line_share_max),
            "clean_band": [str(self.clean_band[0]), str(self.clean_band[1])],
            "workbook_labor_rate": str(self.workbook_labor_rate),
            "workbook_ohp_pct": {k: str(v) for k, v in sorted(self.workbook_ohp_pct.items())},
            "quote_required_categories": list(self.quote_required_categories),
            "quote_replaces": dict(sorted(self.quote_replaces.items())),
            "category_sections": dict(sorted(self.category_sections.items())),
            "fallback_share_max": str(self.fallback_share_max),
            "allowance_section": self.allowance_section,
            "grade_seeds": [s.to_dict() for s in self.grade_seeds],
            "quality": self.quality.to_dict(),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> PricingPolicy:
        """A policy from a stored document; missing keys take the defaults."""
        data = dict(data or {})
        kwargs: dict[str, Any] = {}
        simple = (
            "version", "labor_rates", "rate_overrides", "default_wage_type",
            "material_markup_pct", "labor_markup_pct", "tax_rate_pct", "hour_roundup_steps",
            "hour_uplift", "hour_uplift_under_covered", "unpriced_line_share_max",
            "workbook_labor_rate", "workbook_ohp_pct",
            "quote_required_categories", "quote_replaces", "category_sections",
            "fallback_share_max", "allowance_section", "note",
        )
        for key in simple:
            if key in data and data[key] is not None:
                kwargs[key] = data[key]
        for key in ("hour_uplift_range", "hour_uplift_under_covered_range", "clean_band"):
            if data.get(key):
                kwargs[key] = (data[key][0], data[key][1])
        if data.get("grade_seeds") is not None:
            kwargs["grade_seeds"] = tuple(GradeSeed.from_dict(s) for s in data["grade_seeds"])
        if data.get("quality") is not None:
            kwargs["quality"] = QualityThresholds.from_dict(data["quality"])
        return cls(**kwargs)

    def replace(self, **changes: Any) -> PricingPolicy:
        return replace(self, **changes)

    # Rates ------------------------------------------------------------------------------

    def labor_rate_for(
        self, wage_type: str | None, gc_ids: Iterable[str] = (),
        extra_overrides: Mapping[str, Any] | None = None,
    ) -> RateDecision:
        """The loaded labor rate for a bid (Q5). `source` is `policy:<wage>`,
        `gc_override:<gc_id>` or `default` (no wage type: the default wage
        type's rate, which the engine reports as provisional)."""
        warnings: list[str] = []
        wage = wage_type if wage_type in self.labor_rates else None
        base = self.labor_rates[wage or self.default_wage_type]
        source = f"policy:{wage}" if wage else "default"
        overrides = dict(self.rate_overrides)
        for k, v in (extra_overrides or {}).items():
            overrides[str(k)] = _dec(v, f"rate_overrides[{k}]")
        ids = [str(g) for g in gc_ids]
        if not ids:
            return RateDecision(rate=base, source=source, warnings=tuple(warnings))
        effective = {gc: overrides.get(gc, base) for gc in ids}
        distinct = set(effective.values())
        if len(distinct) > 1:
            warnings.append("multi_gc_rate")
            return RateDecision(rate=base, source=source, warnings=tuple(warnings))
        only = next(iter(distinct))
        if only != base:
            gc = next(gc for gc in ids if overrides.get(gc) == only)
            return RateDecision(rate=only, source=f"gc_override:{gc}", warnings=tuple(warnings))
        return RateDecision(rate=base, source=source, warnings=tuple(warnings))

    def coverage_class(self, fallback_share_hours: Any, unpriced_line_share: Any) -> str:
        """The hours regime of an estimate from what the engine knows about
        its own coverage: `covered` when at most `fallback_share_max` of the
        basis hours came from a class fallback and at most
        `unpriced_line_share_max` of the priced lines matched nothing, else
        `under_covered` (the basis is missing work and G3's hours historically
        sit further above it)."""
        fallback = _dec(fallback_share_hours or 0, "fallback_share_hours")
        unpriced = _dec(unpriced_line_share or 0, "unpriced_line_share")
        if fallback > self.fallback_share_max or unpriced > self.unpriced_line_share_max:
            return COVERAGE_UNDER_COVERED
        return COVERAGE_COVERED

    def uplift_for(self, coverage: str = COVERAGE_COVERED) -> Decimal:
        """The hour uplift of a coverage regime."""
        if coverage not in COVERAGE_CLASSES:
            raise ValueError(f"coverage {coverage!r} not in {COVERAGE_CLASSES}")
        return self.hour_uplift_under_covered if coverage == COVERAGE_UNDER_COVERED else self.hour_uplift

    def uplift_range_for(self, coverage: str = COVERAGE_COVERED) -> tuple[Decimal, Decimal]:
        """The (low, high) uplift range of a coverage regime."""
        if coverage not in COVERAGE_CLASSES:
            raise ValueError(f"coverage {coverage!r} not in {COVERAGE_CLASSES}")
        if coverage == COVERAGE_UNDER_COVERED:
            return self.hour_uplift_under_covered_range
        return self.hour_uplift_range

    def reviewer_hours(
        self, basis_hours: Decimal, *, uplift: Decimal | None = None, coverage: str = COVERAGE_COVERED,
    ) -> Decimal:
        """nice_ceil(basis x uplift): G3's round reviewer hours. The uplift is
        the regime's unless pinned."""
        u = self.uplift_for(coverage) if uplift is None else uplift
        return nice_ceil(Decimal(basis_hours) * u, self.hour_roundup_steps)

    def quote_replace_mode(self, category: str) -> str:
        return self.quote_replaces.get(category, "material")

    def section_of(self, category: str | None) -> str:
        return self.category_sections.get(category or "", "materials")


@dataclass(frozen=True)
class RateDecision:
    rate: Decimal
    source: str
    warnings: tuple[str, ...] = ()


DEFAULT_POLICY = PricingPolicy()


# Hours ------------------------------------------------------------------------------------


def nice_ceil(hours: Decimal | float | int, steps: Sequence[tuple[int | None, int]] = DEFAULT_HOUR_ROUNDUP_STEPS) -> Decimal:
    """Round hours UP to the step of the first bound they fall at or under:
    588 -> 600, 637.1 -> 650, 1614 -> 1650, 4495 -> 4500, 6931 -> 7000,
    25627 -> 26000; an exact multiple stays; 0 stays 0."""
    h = Decimal(str(hours)) if not isinstance(hours, Decimal) else hours
    if h <= 0:
        return Decimal(0)
    for bound, step in steps:
        if bound is None or h <= bound:
            units = (h / Decimal(step)).to_integral_value(rounding=ROUND_CEILING)
            return units * Decimal(step)
    raise ValueError("hour_roundup_steps has no open-ended last step")


# Grade seeds --------------------------------------------------------------------------------


def _norm_grade(value: str) -> str:
    v = str(value).strip().lower()
    return _GRADE_VALUE_ALIASES.get(v, v)


def grade_seed_for(
    policy: PricingPolicy, item_keys: Iterable[str], spec_facts: Iterable[SpecFact],
) -> tuple[GradeSeed, SpecFact] | None:
    """The seed that prices an item and the CITED fact that allows it, or
    None. A fact without `source_ref` never applies (Q11, E13); a seed whose
    keys do not contain one of `item_keys` never applies; a seed without an
    amount (the code default) is inert."""
    keys = {str(k) for k in item_keys}
    for seed in policy.grade_seeds:
        if not seed.active or not keys.intersection(seed.item_keys):
            continue
        for fact in spec_facts:
            if not fact.source_ref:
                continue
            if fact.key in _GRADE_FACT_KEYS or fact.key == seed.fact_key:
                if _norm_grade(fact.value) == _norm_grade(seed.fact_value):
                    return seed, fact
    return None


def inert_grade_seed_for(
    policy: PricingPolicy, item_keys: Iterable[str], spec_facts: Iterable[SpecFact],
) -> tuple[GradeSeed, SpecFact] | None:
    """The seed WITHOUT an amount that would price an item under a cited
    fact, or None: the diagnostic twin of `grade_seed_for` (a line whose
    documents cite a device grade but whose policy seed carries no amount
    is priced from the library and reported, never seeded)."""
    keys = {str(k) for k in item_keys}
    facts = [f for f in spec_facts if f.source_ref]
    for seed in policy.grade_seeds:
        if seed.active or not keys.intersection(seed.item_keys):
            continue
        for fact in facts:
            if fact.key in _GRADE_FACT_KEYS or fact.key == seed.fact_key:
                if _norm_grade(fact.value) == _norm_grade(seed.fact_value):
                    return seed, fact
    return None


# Calibration -------------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationResult:
    """What `calibrate_uplift` measured over the clean band. `pairs` are
    (engine basis hours, G3 reviewer hours); ratios = G3 / basis."""

    n_pairs: int
    n_clean: int
    median: Decimal | None
    p25: Decimal | None
    p75: Decimal | None
    loo_median_abs_error_median_u: float | None
    loo_within_10_median_u: float | None
    loo_median_abs_error_p25_u: float | None
    loo_within_10_p25_u: float | None
    chosen: str  # "median" | "p25" | "none"
    chosen_uplift: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_pairs": self.n_pairs,
            "n_clean": self.n_clean,
            "median": None if self.median is None else str(self.median),
            "p25": None if self.p25 is None else str(self.p25),
            "p75": None if self.p75 is None else str(self.p75),
            "loo_median_abs_error_median_u": self.loo_median_abs_error_median_u,
            "loo_within_10_median_u": self.loo_within_10_median_u,
            "loo_median_abs_error_p25_u": self.loo_median_abs_error_p25_u,
            "loo_within_10_p25_u": self.loo_within_10_p25_u,
            "chosen": self.chosen,
            "chosen_uplift": str(self.chosen_uplift),
        }


def _q(values: Sequence[float], frac: float) -> float:
    srt = sorted(values)
    if len(srt) == 1:
        return srt[0]
    pos = (len(srt) - 1) * frac
    lo = math.floor(pos)
    hi = min(lo + 1, len(srt) - 1)
    return srt[lo] + (srt[hi] - srt[lo]) * (pos - lo)


def calibrate_uplift(
    pairs: Sequence[tuple[float, float]], *,
    band: tuple[Decimal, Decimal] = DEFAULT_CLEAN_BAND,
    steps: Sequence[tuple[int | None, int]] = DEFAULT_HOUR_ROUNDUP_STEPS,
    fallback: Decimal = DEFAULT_HOUR_UPLIFT,
) -> CalibrationResult:
    """Uplift statistics over the pairs whose ratio G3 / basis lies in the
    clean band, plus the leave-one-out error of hours = nice_ceil(basis x u)
    for u = LOO median and u = LOO p25. The chosen uplift is the one with the
    lower LOO median absolute error (ties: the median)."""
    lo, hi = float(band[0]), float(band[1])
    clean = [(b, g) for b, g in pairs if b > 0 and g > 0 and lo <= g / b <= hi]
    if len(clean) < 3:
        return CalibrationResult(len(pairs), len(clean), None, None, None, None, None, None, None,
                                 "none", fallback)
    ratios = [g / b for b, g in clean]

    def loo(frac: float) -> tuple[float, float]:
        errs: list[float] = []
        for i, (b, g) in enumerate(clean):
            others = [r for j, r in enumerate(ratios) if j != i]
            u = _q(others, frac)
            pred = float(nice_ceil(Decimal(repr(b * u)), steps))
            errs.append(abs(pred - g) / g)
        return float(statistics.median(errs)), sum(1 for e in errs if e <= 0.10) / len(errs)

    med_err, med_in = loo(0.5)
    p25_err, p25_in = loo(0.25)
    median = Decimal(repr(round(statistics.median(ratios), 4)))
    p25 = Decimal(repr(round(_q(ratios, 0.25), 4)))
    p75 = Decimal(repr(round(_q(ratios, 0.75), 4)))
    chosen = "p25" if p25_err < med_err else "median"
    return CalibrationResult(
        n_pairs=len(pairs), n_clean=len(clean), median=median, p25=p25, p75=p75,
        loo_median_abs_error_median_u=med_err, loo_within_10_median_u=med_in,
        loo_median_abs_error_p25_u=p25_err, loo_within_10_p25_u=p25_in,
        chosen=chosen, chosen_uplift=p25 if chosen == "p25" else median,
    )


@dataclass(frozen=True)
class CoverageCalibration:
    """`calibrate_uplift` per coverage regime plus the pooled result. A
    regime with fewer than three clean pairs takes the pooled uplift
    (`*_source` says `regime` or `pooled`)."""

    pooled: CalibrationResult
    covered: CalibrationResult
    under_covered: CalibrationResult
    covered_source: str
    under_covered_source: str
    chosen_covered: Decimal
    chosen_under_covered: Decimal

    def uplift_for(self, coverage: str) -> Decimal:
        if coverage not in COVERAGE_CLASSES:
            raise ValueError(f"coverage {coverage!r} not in {COVERAGE_CLASSES}")
        return self.chosen_under_covered if coverage == COVERAGE_UNDER_COVERED else self.chosen_covered

    def to_dict(self) -> dict[str, Any]:
        return {
            "pooled": self.pooled.to_dict(),
            "covered": self.covered.to_dict(),
            "under_covered": self.under_covered.to_dict(),
            "covered_source": self.covered_source,
            "under_covered_source": self.under_covered_source,
            "chosen_covered": str(self.chosen_covered),
            "chosen_under_covered": str(self.chosen_under_covered),
        }


def calibrate_uplift_by_coverage(
    pairs: Sequence[tuple[float, float, str]], *,
    band: tuple[Decimal, Decimal] = DEFAULT_CLEAN_BAND,
    steps: Sequence[tuple[int | None, int]] = DEFAULT_HOUR_ROUNDUP_STEPS,
    fallback_covered: Decimal = DEFAULT_HOUR_UPLIFT,
    fallback_under_covered: Decimal = DEFAULT_HOUR_UPLIFT_UNDER_COVERED,
) -> CoverageCalibration:
    """The uplift per coverage regime from (engine basis hours, G3 reviewer
    hours, coverage class) triples: `calibrate_uplift` on each regime's pairs
    and on all of them. A regime with fewer than three clean pairs takes the
    pooled uplift; when the pooled set is thin too, the regime's fallback."""
    for _b, _g, cov in pairs:
        if cov not in COVERAGE_CLASSES:
            raise ValueError(f"coverage {cov!r} not in {COVERAGE_CLASSES}")
    pooled = calibrate_uplift([(b, g) for b, g, _c in pairs], band=band, steps=steps, fallback=fallback_covered)
    out: dict[str, tuple[CalibrationResult, str, Decimal]] = {}
    for cov, fallback in ((COVERAGE_COVERED, fallback_covered), (COVERAGE_UNDER_COVERED, fallback_under_covered)):
        own = [(b, g) for b, g, c in pairs if c == cov]
        res = calibrate_uplift(own, band=band, steps=steps, fallback=fallback)
        if res.chosen != "none":
            out[cov] = (res, "regime", res.chosen_uplift)
        elif pooled.chosen != "none":
            out[cov] = (res, "pooled", pooled.chosen_uplift)
        else:
            out[cov] = (res, "fallback", fallback)
    return CoverageCalibration(
        pooled=pooled,
        covered=out[COVERAGE_COVERED][0],
        under_covered=out[COVERAGE_UNDER_COVERED][0],
        covered_source=out[COVERAGE_COVERED][1],
        under_covered_source=out[COVERAGE_UNDER_COVERED][1],
        chosen_covered=out[COVERAGE_COVERED][2],
        chosen_under_covered=out[COVERAGE_UNDER_COVERED][2],
    )


__all__ = [
    "POLICY_VERSION", "PricingPolicy", "GradeSeed", "RateDecision", "CalibrationResult",
    "CoverageCalibration", "DEFAULT_POLICY", "DEFAULT_LABOR_RATES", "DEFAULT_GRADE_SEEDS",
    "DEFAULT_HOUR_UPLIFT", "DEFAULT_HOUR_UPLIFT_UNDER_COVERED", "DEFAULT_UNPRICED_LINE_SHARE_MAX",
    "COVERAGE_COVERED", "COVERAGE_UNDER_COVERED", "COVERAGE_CLASSES",
    "DEFAULT_CATEGORY_SECTIONS", "DEFAULT_QUOTE_REPLACES", "nice_ceil", "grade_seed_for", "inert_grade_seed_for",
    "calibrate_uplift", "calibrate_uplift_by_coverage",
]
