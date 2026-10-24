"""The pricing engine: a BOQ priced the way G3's Verify step prices it
(AI_ESTIMATING_BUILD_SPEC WS6 `pricing_engine`, 5.4 PricedEstimate, the WS0
artifact `bdr.priced_estimate/1`).

`compute_estimate(rows, library, policy, quotes, ctx)` is pure and
deterministic: every number comes from the rows, the cost library, the
policy document, the vendor quotes and the context the caller injected.
Nothing here touches a database or a model; `price_run` (the I/O wrapper
that loads a run's BOQ and stores a version) is wave 2.

Order of work inside `compute_estimate`:

1. pre-gates: `pricing_allowed` (the WS4 final-gate verdict, passed in; a
   status override that echoes WS4's own gate codes), `linear_in_ea` (a bare
   wire, cable, conduit, raceway, trench, saw-cut or track rail row in EA,
   the LINEAR_CLASSES of spec 5.1; linear luminaires are judged by evidence
   elsewhere), `wage_type_unknown` (no wage type at all). Blocker codes are
   the gate ids of `estimating_contracts.GATE_IDS` (5.4) so GC3 can read
   them. A blocker makes the status `blocked`; the draft is still computed so
   the verifier sees the number and the reasons;
2. per line: identity (`electrical_items.parse_item`), library lookup by the
   ladder, grade seeds only with a cited SpecFact, class fallback (quality
   weak, flag `fallback_price`) INSIDE the totals, demolition material 0,
   extended hours and material on the wastage-inclusive quantity;
3. category and section: every non-quoted material line lands in exactly one
   material category (General Material for wiring and other items), each
   category in one pricing section; an assertion checks the category pre-tax
   sum equals the line material sum plus the chosen quotes, which is what
   closes the Verify leak (GC1: OTHER ITEMS can never fall out);
4. quotes: per category the human-selected quote when eligible, else the
   lowest on the tax-inclusive amount (Q9; `pricing_formula.apply_tax` is the
   comparison basis, as in the Verify step); a quote replaces its category's
   material (or material and labor, or adds, per policy); alternatives
   recorded. The chosen quote's reconciliation (`quote_reconciliations`,
   vendor_quotes) drives coverage: a line the quote does not carry (control
   rows outside a lighting quote, an unmatched switchgear designation,
   subcontract conduit and wire) keeps its library material inside the
   category's library row and carries the flag `not_in_quote` (plus
   `manual_eligible` when the reconciliation raised it);
5. `verify_totals`: tax on material per category row (cents, ROUND_HALF_UP,
   `pricing_formula.apply_tax`), sections, markup on the tax-inclusive
   section amount, labor = nice_ceil(basis x uplift) x the policy rate, the
   uplift being the one of the estimate's coverage regime
   (`PricingPolicy.coverage_class` from the fallback share of the basis
   hours and the share of lines matched to nothing; recorded in
   `quality_summary.coverage_class` and `hour_uplift_applied`);
   bid total = the sum of the ten Verify numbers to the cent;
6. status: `blocked` on a blocker; `provisional` when a quote-required
   category has no quote, the fallback share exceeds the policy limit, lines
   are unpriced, the wage source is the default, or the out-of-band detector
   fires (labor only, gear without devices, quote only); else `priced`. The
   no-branch-wiring check is a warning here (WS4 owns the blocking gate).

`to_priced_estimate_v1` maps the result onto the WS0 artifact and validates
it with `estimating_contracts.validate_priced_estimate`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services import estimating_contracts as ec
from app.services.cost_library import CostLibrary, Lookup, library_key
from app.services.electrical_items import ItemIdentity, canon_uom, parse_item
from app.services.pricing_formula import (
    DEFAULT_TAX_RATE,
    SECTION_KEYS,
    apply_tax,
    markup_amount,
    round_cents,
    verify_sum,
)
from app.services.pricing_policy import DEFAULT_POLICY, PricingPolicy, grade_seed_for
from app.services.pricing_types import (
    Allowance,
    PricedEstimate,
    PricedLine,
    PriceRow,
    PricingContext,
    PricingIssue,
    QuoteRef,
    UnpricedSummary,
    ValueStat,
    VerifyDraft,
)

ENGINE_VERSION = 1

# Pre-gate and status codes raised by this module. The two gate blockers are
# gate ids (5.4); pricing_not_allowed is a status override only, its detail
# carries WS4's gate codes.
BLOCK_LINEAR_IN_EA = "linear_in_ea"
BLOCK_WAGE_TYPE_UNKNOWN = "wage_type_unknown"
BLOCK_PRICING_NOT_ALLOWED = "pricing_not_allowed"
assert BLOCK_LINEAR_IN_EA in ec.GATE_IDS and BLOCK_WAGE_TYPE_UNKNOWN in ec.GATE_IDS
WARN_NO_BRANCH = "new_devices_no_branch"
WARN_MULTI_GC = "multi_gc_rate"
PROV_QUOTE_REQUIRED = "quote_required"
PROV_FALLBACK_SHARE = "fallback_share"
PROV_UNPRICED = "unpriced_lines"
PROV_WAGE_DEFAULT = "wage_type_default"
PROV_OUT_OF_BAND = "out_of_band"
FLAG_FALLBACK = "fallback_price"
FLAG_MISSING = "missing_price"
FLAG_QUOTE_COVERED = "quote_covered"
FLAG_SEED = "grade_seed"
FLAG_LS = "ls_not_priced"
FLAG_BLOCKED = "blocked"
FLAG_DEMOLITION = "demolition_material_zero"
FLAG_NOT_IN_QUOTE = "not_in_quote"
FLAG_MANUAL_ELIGIBLE = "manual_eligible"

# Item classes that are always lengths: a bare EA row of one blocks (5.1,
# WS6): the one LINEAR_CLASSES set of electrical_items. Linear luminaires
# (undercabinet, strip, tape) are judged by evidence (WS3, WS4), never here.
UOM_CONFLICT_CLASSES: frozenset[str] = ec.LINEAR_CLASSES
DEVICE_CLASSES: frozenset[str] = frozenset(
    {"receptacle", "switch", "luminaire", "luminaire_linear", "control"}
)
GEAR_CLASSES: frozenset[str] = frozenset({"panel", "breaker"})
BRANCH_CLASSES: frozenset[str] = frozenset({"conductor", "cable"})

# BOQ group -> material category when the row carries no category of its own.
GROUP_CATEGORY: dict[str, str] = {
    "DEMOLITION": "Demolition",
    "WIRING": "General Material",
    "OTHER ITEMS": "General Material",
    "SWITCHGEAR": "Switchgear",
    "LIGHTING FIXTURES": "Lighting",
    "LIGHTING CONTROLS": "Lighting",
    "LOW VOLTAGE WORK": "Low Voltage",
    "SOLAR WORK": "Solar",
    "LIGHTING POLE WORK": "General Material",
    "NV ENERGY": "General Material",
    "ALTERNATES": "General Material",
}
CLASS_CATEGORY: dict[str, str] = {"trench": "Trenching", "sawcut": "Trenching"}
GENERAL_MATERIAL = "General Material"
ALLOWANCES_CATEGORY = "Allowances"
_ZERO = Decimal(0)
_SIX = Decimal("0.000001")


class PricingError(ValueError):
    """A caller error (not a pricing verdict): bad inputs to the engine."""


# Verify formula -----------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryRow:
    """One taxable material row of the Verify step: a vendor quote, the
    General Material figure, or the engine's library material of a category."""

    category: str
    amount_pretax: Decimal
    tax_included: bool | None = False
    section: str = "materials"
    source: str = "library"
    quote_id: str | None = None
    taxable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount_pretax", Decimal(str(self.amount_pretax)))
        if self.section not in SECTION_KEYS:
            raise PricingError(f"CategoryRow.section {self.section!r} not in {SECTION_KEYS}")


@dataclass
class VerifyBreakdown:
    """verify_totals' working: per row tax and total, per section amount and
    markup, labor, and the ten numbers."""

    rows: list[dict[str, Any]]
    sections: dict[str, dict[str, Any]]
    labor_amount: Decimal
    labor_markup_amount: Decimal
    numbers: dict[str, Decimal | None]
    bid_total: Decimal


def verify_totals(
    *,
    labor_hours: Decimal,
    labor_rate: Decimal,
    category_rows: Sequence[CategoryRow],
    tax_rate_pct: Decimal,
    material_markup_pct: Mapping[str, Decimal],
    labor_markup_pct: Decimal = _ZERO,
    present_sections: Iterable[str] = (),
    markup_amounts: Mapping[str, Decimal | None] | None = None,
    labor_markup_amount: Decimal | None = None,
) -> VerifyBreakdown:
    """G3's Verify formula to the cent.

    labor = hours x rate (cents); each category row is taxed on its own
    (`apply_tax`, cents ROUND_HALF_UP, no tax when the row says tax included
    or is untaxable such as an allowance); a section's amount is the sum of
    its rows' tax-inclusive totals; the section markup is `pct` of that
    tax-inclusive amount (cents); the labor markup is `pct` of labor; the bid
    total is the sum of the ten numbers. A breakout section is None when no
    row maps to it and it is not in `present_sections`; `materials` is
    always present. `markup_amounts` (section -> amount) and
    `labor_markup_amount` replace the percentage markups with recorded
    figures (the Verify step commits the markup AMOUNTS a human set on the
    Markup step, which need not equal pct x the current section amount)."""
    labor_amount = round_cents(Decimal(labor_hours) * Decimal(labor_rate))
    labor_markup = (
        round_cents(Decimal(labor_markup_amount)) if labor_markup_amount is not None
        else markup_amount(labor_amount, Decimal(labor_markup_pct))
    )
    recorded = dict(markup_amounts or {})
    present = set(present_sections) | {"materials"}
    rows_out: list[dict[str, Any]] = []
    totals: dict[str, Decimal] = {key: _ZERO for key in SECTION_KEYS}
    for row in category_rows:
        present.add(row.section)
        if row.taxable:
            total, tax = apply_tax(row.amount_pretax, row.tax_included, Decimal(tax_rate_pct))
        else:
            total, tax = row.amount_pretax, Decimal("0.00")
        assert total is not None
        totals[row.section] += total
        rows_out.append(
            {
                "category": row.category,
                "section": row.section,
                "source": row.source,
                "quote_id": row.quote_id,
                "pre_tax": row.amount_pretax,
                "tax_included": row.tax_included,
                "taxable": row.taxable,
                "tax": tax,
                "total": total,
            }
        )
    sections: dict[str, dict[str, Any]] = {}
    numbers: dict[str, Decimal | None] = {
        "labor_amount": labor_amount,
        "labor_markup_amount": labor_markup,
    }
    for key in SECTION_KEYS:
        if key in present:
            amount = round_cents(totals[key])
            pct = Decimal(material_markup_pct.get(key, _ZERO))
            markup = (
                round_cents(Decimal(recorded[key])) if recorded.get(key) is not None
                else markup_amount(amount, pct)
            )
            sections[key] = {"present": True, "amount": amount, "markup_pct": pct, "markup": markup}
            numbers[f"{key}_amount"] = amount
            numbers[f"{key}_markup_amount"] = markup
        else:
            sections[key] = {"present": False, "amount": None, "markup_pct": None, "markup": None}
            numbers[f"{key}_amount"] = None
            numbers[f"{key}_markup_amount"] = None
    ordered = {key: numbers[key] for key in ec.VERIFY_KEYS}
    return VerifyBreakdown(
        rows=rows_out,
        sections=sections,
        labor_amount=labor_amount,
        labor_markup_amount=labor_markup,
        numbers=ordered,
        bid_total=verify_sum(ordered),
    )


# Line pricing --------------------------------------------------------------------------------


@dataclass
class _Priced:
    row: PriceRow
    identity: ItemIdentity
    uom: str
    category: str
    section: str
    lookup: Lookup
    labor: ValueStat
    material: ValueStat
    material_source: str
    flags: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    labor_hours: Decimal | None = None
    material_cost: Decimal | None = None
    blocked: bool = False
    is_ls: bool = False
    seed_note: str | None = None


def _identity(row: PriceRow) -> tuple[ItemIdentity, str]:
    identity = parse_item(row.description, row.unit)
    try:
        uom = canon_uom(row.unit)
    except ValueError:
        uom = ""
    return identity, uom


def category_for(row: PriceRow, identity: ItemIdentity) -> str:
    """The material category a line rolls into: the row's own category when
    it names one, else trench and saw-cut rows to Trenching, else by group;
    unknown groups land in General Material (never dropped)."""
    if row.material_category:
        return str(row.material_category)
    cls_cat = CLASS_CATEGORY.get(identity.item_class)
    if cls_cat:
        return cls_cat
    group = (row.group or "").strip().upper()
    if group in GROUP_CATEGORY:
        return GROUP_CATEGORY[group]
    if (row.section or "").strip().upper() == "DEMOLITION":
        return "Demolition"
    return GENERAL_MATERIAL


def _price_line(
    row: PriceRow, library: CostLibrary, policy: PricingPolicy, ctx: PricingContext,
) -> _Priced:
    identity, uom = _identity(row)
    category = category_for(row, identity)
    section = policy.section_of(category)
    base = _Priced(
        row=row, identity=identity, uom=uom or row.unit, category=category, section=section,
        lookup=Lookup(method="none", labor=ValueStat.missing(), material=ValueStat.missing()),
        labor=ValueStat.missing(), material=ValueStat.missing(), material_source="none",
    )
    if not uom or uom == "LS" or identity.item_class == "allowance":
        base.is_ls = True
        base.flags.append(FLAG_LS)
        return base
    if uom == "EA" and is_bare_length_item(identity):
        base.blocked = True
        base.flags.append(BLOCK_LINEAR_IN_EA)
        base.flags.append(FLAG_BLOCKED)
        return base
    lookup = library.lookup(identity, uom, thresholds=policy.quality)
    if not lookup.found:
        lookup = library.fallback(identity, uom)
    base.lookup = lookup
    base.labor = lookup.labor
    base.material = lookup.material
    base.flags.extend(lookup.flags)
    if lookup.method == "fallback":
        if FLAG_FALLBACK not in base.flags:
            base.flags.append(FLAG_FALLBACK)
    if lookup.method == "none":
        base.flags.append(FLAG_MISSING)
    # Grade seeds: only with a cited fact (Q11, E13), keyed on base and HG keys.
    key = library_key(identity)
    seed_hit = grade_seed_for(policy, (key, identity.item_key), ctx.spec_facts)
    if seed_hit is not None:
        seed, fact = seed_hit
        base.material = ValueStat(
            value=seed.material_usd, p25=seed.material_usd, p75=seed.material_usd,
            n_obs=0, n_projects=0, latest_seen=None, quality="policy", method="seed",
        )
        if base.labor.value is not None and seed.labor_factor != 1:
            base.labor = ValueStat(
                value=(base.labor.value * seed.labor_factor).quantize(_SIX),
                p25=base.labor.p25, p75=base.labor.p75, n_obs=base.labor.n_obs,
                n_projects=base.labor.n_projects, latest_seen=base.labor.latest_seen,
                quality="policy", method="seed",
            )
        base.material_source = "seed"
        base.flags.append(FLAG_SEED)
        base.modifiers.append(f"{fact.key}={fact.value}@{fact.source_ref}")
        base.seed_note = seed.note
    elif identity.item_class == "demolition" or _in_demolition(row):
        base.material = ValueStat(
            value=_ZERO, p25=_ZERO, p75=_ZERO, n_obs=base.labor.n_obs,
            n_projects=base.labor.n_projects, latest_seen=base.labor.latest_seen,
            quality=base.labor.quality if base.labor.value is not None else "policy",
            method=base.labor.method if base.labor.value is not None else "seed",
        )
        base.material_source = "seed" if base.labor.value is None else _source_of(base.labor.method)
        base.flags.append(FLAG_DEMOLITION)
    elif base.material.value is not None:
        base.material_source = _source_of(base.material.method)
    qty = row.quantity
    if base.labor.value is not None:
        base.labor_hours = (base.labor.value * qty).quantize(_SIX)
    if base.material.value is not None:
        base.material_cost = (base.material.value * qty).quantize(_SIX)
    return base


_BARE_CONDUCTOR_RE = re.compile(r"^#\s?(?:\d+/0|\d{1,2})\b|^\d{3,4}\s?(?:KCMIL|MCM)\b|^#?\d{3,4}\s?KCMIL\b")
_BARE_CONDUIT_RE = re.compile(r"^\d+(?:-\d+/\d+)?(?:/\d+)?\s?\"\s?C?\b")
_BARE_CABLE_RE = re.compile(r"^#?\d+/\d+C\b|^CAT\s?\d|^(?:MC|AC|NM-?B?|RG\d+)\b|\bCONTROL CABLE\b|\bCABLE\b")
_BARE_TRENCH_RE = re.compile(r"^(?:TRENCH|EXCAVAT|SAW\s?CUT|DIRECTIONAL BOR|BOR)")
_BARE_SAWCUT_RE = re.compile(r"\bSAW\s?CUT(?:TING)?\b")
_BARE_RACEWAY_RE = re.compile(r"\b(?:WIREMOLD|RACEWAY|WIREWAY|CABLE\s?TRAY)\b")
_BARE_TRACK_RE = re.compile(r"\bTRACK\b")
_BARE_MAX_WORDS = 8
# Count items that carry a wire size or a cable word but are not a length.
_NOT_LENGTH_WORDS = (
    "LUG|LUGS|JUMPER|TERMINAL|TERMINATION|SPLICE|PIGTAIL|KIT|CONNECTOR|CLAMP|TAP|WHIP|"
    "CORD|ASSEMBLY|LIU|ENCLOSURE|STORAGE|PATCH|TESTER|REEL|ROLL|BOX|STRAP|STRAPS|"
    "MANAGER|MANAGEMENT|ORGANIZER|CONNECTION|LADDER|RACK|BRACKET|HANGER|SUPPORT|"
    "DRAPERY|DRAPE|CURTAIN|DOOR|GATE"
)
_NOT_LENGTH_RE = re.compile(rf"\b(?:{_NOT_LENGTH_WORDS}|TRAY)\b")
# A cable tray IS the raceway length, so TRAY stays a length word for raceway
# rows; fittings, covers and the like are counted.
_NOT_LENGTH_RACEWAY_RE = re.compile(
    rf"\b(?:{_NOT_LENGTH_WORDS}|FITTING|FITTINGS|COVER|COVERS|ELBOW|TEE|OUTLET|DEVICE|"
    r"ADAPTER|COUPLING|COUPLINGS|END|CAP|CAPS|PLATE|SPLITTER|REDUCER)\b"
)
# "SAW CUT AND PATCH" is still a length of cut: PATCH is a count word only
# for cables (patch panel, patch cord).
_NOT_LENGTH_SAWCUT_RE = re.compile(
    r"\b(?:" + "|".join(w for w in _NOT_LENGTH_WORDS.split("|") if w != "PATCH") + r"|TRAY)\b"
)


def is_bare_length_item(identity: ItemIdentity) -> bool:
    """True for a plain wire, cable, conduit, raceway, trench, saw-cut or
    track RAIL line (or the demolition of one): the LINEAR_CLASSES rows that
    are always feet. A long installation description that merely mentions a
    wire size ('INSTALL STREETLIGHT ... WITH 8' ARM', '#8 GROUND LUG') is not
    one, nor is a count item of the class (a ladder rack, raceway fittings)."""
    cls = identity.item_class
    norm = identity.norm
    if cls == "demolition":
        subject = identity.attrs.get("subject", "")
        if subject not in UOM_CONFLICT_CLASSES:
            return False
        rest = re.sub(r"^(?:REMOVAL|REMOVE|DEMO|DEMOLITION|DEMOLISH)\b(?:\s+OF)?(?:\s+(?:THE|ALL))?"
                      r"(?:\s+EXISTING|\s+\(E\))?\s*", "", norm, count=1)
        return _bare_by_class(subject, rest, identity.attrs, demolition=True)
    if cls not in UOM_CONFLICT_CLASSES:
        return False
    return _bare_by_class(cls, norm, identity.attrs, demolition=False)


def _bare_by_class(cls: str, text: str, attrs: Mapping[str, str], *, demolition: bool) -> bool:
    """The per-class bare-length test on `text` (the description with any
    demolition prefix removed)."""
    not_length = {"raceway": _NOT_LENGTH_RACEWAY_RE, "sawcut": _NOT_LENGTH_SAWCUT_RE}.get(cls, _NOT_LENGTH_RE)
    if len(text.split()) > _BARE_MAX_WORDS or not_length.search(text):
        return False
    if cls == "conductor":
        if demolition:
            return bool(_BARE_CONDUCTOR_RE.search(text) or _BARE_CONDUIT_RE.search(text)
                        or _BARE_CABLE_RE.search(text))
        return bool(_BARE_CONDUCTOR_RE.search(text)) and "conductor_size" in attrs
    if cls == "conduit":
        if demolition:
            return bool(_BARE_CONDUCTOR_RE.search(text) or _BARE_CONDUIT_RE.search(text)
                        or _BARE_CABLE_RE.search(text))
        return bool(_BARE_CONDUIT_RE.search(text)) and "conduit_type" in attrs
    if cls == "cable":
        if demolition:
            return bool(_BARE_CONDUCTOR_RE.search(text) or _BARE_CONDUIT_RE.search(text)
                        or _BARE_CABLE_RE.search(text))
        return bool(_BARE_CABLE_RE.search(text))
    if cls == "raceway":
        return bool(_BARE_RACEWAY_RE.search(text))
    if cls == "trench":
        return bool(_BARE_TRENCH_RE.search(text))
    if cls == "sawcut":
        return bool(_BARE_SAWCUT_RE.search(text) or _BARE_TRENCH_RE.search(text))
    if cls == "lighting_track":
        return bool(_BARE_TRACK_RE.search(text)) and "HEAD" not in text
    return False


def _in_demolition(row: PriceRow) -> bool:
    """Rows of the DEMOLITION section or group price labor only (material 0
    by policy), whatever their wording."""
    return (row.section or "").strip().upper() == "DEMOLITION" or (
        (row.group or "").strip().upper() == "DEMOLITION"
    )


def _source_of(method: str) -> str:
    if method in ("exact", "compatible", "context"):
        return "library"
    if method == "fallback":
        return "fallback"
    if method == "seed":
        return "seed"
    return "none"


# Quotes -------------------------------------------------------------------------------------


def taxed_quote_amount(quote: QuoteRef, tax_rate_pct: Decimal) -> Decimal:
    """The tax-inclusive amount of a quote: the comparison basis of the Verify
    step (a quote that excludes tax costs its amount plus tax)."""
    total, _tax = apply_tax(Decimal(str(quote.amount_usd)), quote.tax_included, Decimal(str(tax_rate_pct)))
    assert total is not None
    return total


def choose_quotes(
    quotes: Sequence[QuoteRef], *, selected_quote_ids: Iterable[str] = (),
    tax_rate_pct: Decimal = DEFAULT_TAX_RATE,
) -> dict[str, tuple[QuoteRef, list[QuoteRef]]]:
    """Per category: (the chosen quote, the alternatives). The human-selected
    quote wins when one is eligible; else the lowest TAX-INCLUSIVE amount
    (Q9; ties: by id). Alternatives are listed in the same order."""
    selected = {str(s) for s in selected_quote_ids}
    by_cat: dict[str, list[QuoteRef]] = {}
    for q in quotes:
        by_cat.setdefault(q.category, []).append(q)
    out: dict[str, tuple[QuoteRef, list[QuoteRef]]] = {}
    for cat, items in by_cat.items():
        items = sorted(items, key=lambda q: (taxed_quote_amount(q, tax_rate_pct), q.quote_id))
        chosen = next((q for q in items if q.quote_id in selected), items[0])
        out[cat] = (chosen, [q for q in items if q is not chosen])
    return out


# The estimate ---------------------------------------------------------------------------------


def _share(part: Decimal, whole: Decimal) -> float:
    return float(part / whole) if whole else 0.0


def _rec_field(rec: Any, name: str) -> Any:
    if isinstance(rec, Mapping):
        return rec.get(name)
    return getattr(rec, name, None)


def quote_exclusions(
    reconciliations: Mapping[str, Any] | None,
) -> dict[str, tuple[frozenset[str], tuple[str, ...]]]:
    """quote id -> (line ids the quote does not carry, extra line flags),
    from vendor_quotes Reconciliation objects or their `to_dict()`. The
    engine applies only the entry of the quote it chose for a category."""
    out: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {}
    for quote_id, rec in (reconciliations or {}).items():
        ids = frozenset(str(x) for x in (_rec_field(rec, "not_in_quote_line_ids") or ()))
        flags = tuple(f for f in (_rec_field(rec, "flags") or ()) if f == FLAG_MANUAL_ELIGIBLE)
        out[str(quote_id)] = (ids, flags)
    return out


def compute_estimate(
    rows: Sequence[PriceRow],
    library: CostLibrary,
    policy: PricingPolicy,
    quotes: Sequence[QuoteRef],
    ctx: PricingContext,
    *,
    exclude_project: str | None = None,
    pricing_allowed: tuple[bool, Sequence[str]] | None = None,
    selected_quote_ids: Iterable[str] = (),
    reviewer_hours: Decimal | None = None,
    quote_reconciliations: Mapping[str, Any] | None = None,
) -> PricedEstimate:
    """Price `rows` (see the module docstring). `exclude_project` is the run's
    own project for leave-one-out pricing: the library must already exclude
    it (a library that still carries it is a caller error). `pricing_allowed`
    is WS4's `(allowed, reasons)`; None means the gates were not consulted
    (the engine then relies on its own pre-gates). `reviewer_hours` pins the
    reviewer hours (PG-A2 replays with G3's recorded hours).
    `quote_reconciliations` maps a quote id to its vendor_quotes
    reconciliation (object or dict): the lines in the chosen quote's
    `not_in_quote_line_ids` skip quote coverage, keep library material and
    carry `not_in_quote`. Without it every line of a quoted category is
    covered (the replay of a bare QuoteRef)."""
    if exclude_project and library.n_projects and exclude_project not in library.excluded_projects:
        raise PricingError(
            f"library was not built with exclude_projects containing {exclude_project}"
        )
    blockers: list[PricingIssue] = []
    warnings: list[PricingIssue] = []
    provisional: list[str] = []

    # 1. pre-gates --------------------------------------------------------------------
    if pricing_allowed is not None and not pricing_allowed[0]:
        reasons = [str(r) for r in pricing_allowed[1]] or [BLOCK_PRICING_NOT_ALLOWED]
        blockers.append(PricingIssue(code=BLOCK_PRICING_NOT_ALLOWED, detail="; ".join(reasons)))
    if ctx.wage_type is None:
        blockers.append(PricingIssue(code=BLOCK_WAGE_TYPE_UNKNOWN, detail="no wage type decided"))

    # 2. lines -----------------------------------------------------------------------------
    priced = [_price_line(row, library, policy, ctx) for row in rows]
    seen: set[str] = set()
    for p in priced:
        if p.row.line_id in seen:
            raise PricingError(f"duplicate line_id {p.row.line_id}")
        seen.add(p.row.line_id)
    conflicts = [p.row.line_id for p in priced if BLOCK_LINEAR_IN_EA in p.flags]
    if conflicts:
        blockers.append(PricingIssue(
            code=BLOCK_LINEAR_IN_EA,
            detail=(
                "length items in EA: wire, cable, conduit, raceway, trench, saw cut and "
                "track rail are always FT"
            ),
            line_ids=tuple(conflicts),
        ))

    # 3. quotes and categories ----------------------------------------------------------
    chosen = choose_quotes(quotes, selected_quote_ids=selected_quote_ids, tax_rate_pct=policy.tax_rate_pct)
    exclusions = quote_exclusions(quote_reconciliations)
    for p in priced:
        if p.is_ls or p.blocked:
            continue
        hit = chosen.get(p.category)
        if hit is None:
            continue
        quote, _alts = hit
        not_carried, extra_flags = exclusions.get(quote.quote_id, (frozenset(), ()))
        if p.row.line_id in not_carried:
            p.flags.append(FLAG_NOT_IN_QUOTE)
            p.flags.extend(extra_flags)
            continue
        mode = policy.quote_replace_mode(p.category)
        if mode in ("material", "material_and_labor"):
            p.material_source = quote.material_source
            p.material_cost = _ZERO
            p.flags.append(FLAG_QUOTE_COVERED)
        if mode == "material_and_labor":
            p.labor_hours = _ZERO
            p.flags.append("quote_covers_labor")

    # Library material is summed per BOQ group and rounded to cents there (a
    # recap row is cents), then the group figures add up into the category.
    categories: dict[str, dict[str, Any]] = {}
    group_material: dict[tuple[str, str], Decimal] = {}
    for p in priced:
        if p.is_ls or p.blocked:
            continue
        entry = categories.setdefault(p.category, {
            "section": p.section, "library_material": _ZERO, "lines": 0,
            "quote": None, "alternatives": [], "replaces": None,
        })
        entry["lines"] += 1
        if p.material_cost is not None and FLAG_QUOTE_COVERED not in p.flags:
            gkey = (p.category, (p.row.group or "").upper())
            group_material[gkey] = group_material.get(gkey, _ZERO) + p.material_cost
    for (cat, _group), amount in group_material.items():
        categories[cat]["library_material"] += round_cents(amount)
    for cat, (quote, alts) in chosen.items():
        entry = categories.setdefault(cat, {
            "section": policy.section_of(cat), "library_material": _ZERO, "lines": 0,
            "quote": None, "alternatives": [], "replaces": None,
        })
        entry["quote"] = quote
        entry["alternatives"] = alts
        entry["replaces"] = policy.quote_replace_mode(cat)
    for allowance in ctx.allowances:
        entry = categories.setdefault(ALLOWANCES_CATEGORY, {
            "section": policy.allowance_section, "library_material": _ZERO, "lines": 0,
            "quote": None, "alternatives": [], "replaces": None, "allowances": _ZERO,
        })
        entry.setdefault("allowances", _ZERO)
        entry["allowances"] += allowance.amount_usd

    category_rows: list[CategoryRow] = []
    for cat, entry in sorted(categories.items()):
        lib_mat = round_cents(entry["library_material"])
        if lib_mat or (entry["quote"] is None and entry["lines"] and cat != ALLOWANCES_CATEGORY):
            category_rows.append(CategoryRow(
                category=cat, amount_pretax=lib_mat, tax_included=False,
                section=entry["section"], source="library",
            ))
        quote = entry["quote"]
        if quote is not None:
            category_rows.append(CategoryRow(
                category=cat, amount_pretax=round_cents(quote.amount_usd),
                tax_included=quote.tax_included, section=entry["section"],
                source="quote", quote_id=quote.quote_id,
            ))
        if entry.get("allowances") is not None:
            category_rows.append(CategoryRow(
                category=cat, amount_pretax=round_cents(entry["allowances"]), tax_included=True,
                section=entry["section"], source="allowance", taxable=False,
            ))
    # The leak closer: every non-quoted material line is inside a category row.
    lines_material = sum((round_cents(v) for v in group_material.values()), _ZERO)
    rows_material = round_cents(sum(
        (r.amount_pretax for r in category_rows if r.source == "library"), _ZERO
    ))
    assert lines_material == rows_material, (lines_material, rows_material)

    # 4. labor ------------------------------------------------------------------------------
    basis_hours = sum((p.labor_hours for p in priced if p.labor_hours is not None), _ZERO)
    live = [p for p in priced if not p.is_ls and not p.blocked]
    fallback_basis = sum((p.labor_hours or _ZERO for p in live if FLAG_FALLBACK in p.flags), _ZERO)
    unpriced_line_share = (
        sum(1 for p in live if p.lookup.method == "none") / len(live) if live else 0.0
    )
    coverage = policy.coverage_class(
        Decimal(repr(_share(fallback_basis, basis_hours))), Decimal(repr(unpriced_line_share))
    )
    uplift_applied = policy.uplift_for(coverage)
    if ctx.labor_rate_source in ("recorded", "manual", "override"):
        rate, rate_source = ctx.labor_rate_usd, ctx.labor_rate_source
    else:
        decision = policy.labor_rate_for(ctx.wage_type, ctx.project_gc_ids, ctx.rate_overrides)
        rate, rate_source = decision.rate, decision.source
        for w in decision.warnings:
            warnings.append(PricingIssue(code=w, detail="GCs on this bid carry different labor rates"))
        if rate_source == "default":
            provisional.append(PROV_WAGE_DEFAULT)
    hours_reviewer = (
        Decimal(str(reviewer_hours)) if reviewer_hours is not None
        else policy.reviewer_hours(basis_hours, coverage=coverage)
    )
    breakdown = verify_totals(
        labor_hours=hours_reviewer, labor_rate=rate, category_rows=category_rows,
        tax_rate_pct=policy.tax_rate_pct, material_markup_pct=policy.material_markup_pct,
        labor_markup_pct=policy.labor_markup_pct,
    )
    verify = VerifyDraft(
        **{k: v for k, v in breakdown.numbers.items()},
        bid_total=breakdown.bid_total,
        labor_hours_basis=basis_hours.quantize(_SIX),
        labor_hours_reviewer=hours_reviewer,
        labor_rate=rate,
        labor_rate_source=rate_source,
    )

    # 5. status, shares, warnings ---------------------------------------------------------
    material_total = sum(
        (p.material_cost for p in priced if p.material_cost is not None and not p.is_ls), _ZERO
    )
    hours_by_quality: dict[str, Decimal] = {}
    material_by_quality: dict[str, Decimal] = {}
    fallback_hours = fallback_material = _ZERO
    unpriced_lines: list[str] = []
    for p in priced:
        if p.is_ls or p.blocked:
            continue
        if p.labor_hours is not None:
            hours_by_quality[p.labor.quality] = hours_by_quality.get(p.labor.quality, _ZERO) + p.labor_hours
        if p.material_cost is not None:
            q = "quote" if FLAG_QUOTE_COVERED in p.flags else p.material.quality
            material_by_quality[q] = material_by_quality.get(q, _ZERO) + p.material_cost
        if FLAG_FALLBACK in p.flags:
            fallback_hours += p.labor_hours or _ZERO
            fallback_material += p.material_cost or _ZERO
            unpriced_lines.append(p.row.line_id)
        if p.lookup.method == "none":
            unpriced_lines.append(p.row.line_id)
    if any(p.lookup.method == "none" for p in priced if not p.is_ls and not p.blocked):
        provisional.append(PROV_UNPRICED)
    if basis_hours and _share(fallback_hours, basis_hours) > float(policy.fallback_share_max):
        provisional.append(f"{PROV_FALLBACK_SHARE}:hours")
    if material_total and _share(fallback_material, material_total) > float(policy.fallback_share_max):
        provisional.append(f"{PROV_FALLBACK_SHARE}:material")
    for cat in policy.quote_required_categories:
        entry = categories.get(cat)
        if entry and entry["lines"] and entry["quote"] is None:
            provisional.append(f"{PROV_QUOTE_REQUIRED}:{cat}")
            warnings.append(PricingIssue(
                code=PROV_QUOTE_REQUIRED,
                detail=f"{cat} material is a library placeholder until a vendor quote exists",
            ))
    for code, detail in _out_of_band(priced, categories):
        provisional.append(f"{PROV_OUT_OF_BAND}:{code}")
        warnings.append(PricingIssue(code=f"{PROV_OUT_OF_BAND}:{code}", detail=detail))
    devices = sum((p.row.quantity for p in priced if p.identity.item_class in DEVICE_CLASSES), _ZERO)
    branch_ft = sum(
        (p.row.quantity for p in priced if p.identity.item_class in BRANCH_CLASSES and p.uom == "FT"),
        _ZERO,
    )
    if devices > 0 and branch_ft == 0:
        warnings.append(PricingIssue(
            code=WARN_NO_BRANCH,
            detail="new devices without branch wire or cable (WS4 owns the blocking gate)",
        ))
    status = "blocked" if blockers else ("provisional" if provisional else "priced")

    # 6. ranges and summaries ---------------------------------------------------------------
    low_hours = sum((((p.labor.p25 if p.labor.p25 is not None else p.labor.value) or _ZERO) * p.row.quantity
                     for p in priced if p.labor_hours is not None and "quote_covers_labor" not in p.flags), _ZERO)
    high_hours = sum((((p.labor.p75 if p.labor.p75 is not None else p.labor.value) or _ZERO) * p.row.quantity
                      for p in priced if p.labor_hours is not None and "quote_covers_labor" not in p.flags), _ZERO)
    uplift_low, uplift_high = policy.uplift_range_for(coverage)
    range_low = _range_total(priced, category_rows, policy, rate, low_hours, "p25", uplift_low, reviewer_hours)
    range_high = _range_total(priced, category_rows, policy, rate, high_hours, "p75", uplift_high, reviewer_hours)

    lines_out: list[PricedLine] = []
    for p in priced:
        lines_out.append(PricedLine(
            line_id=p.row.line_id, row=p.row, item_key=library_key(p.identity),
            family=p.identity.family, uom=p.uom or p.row.unit, qty_eff=p.row.quantity,
            labor=p.labor, material=p.material, modifiers=tuple(p.modifiers),
            material_source=p.material_source, labor_hours=p.labor_hours,
            material_cost=p.material_cost, pricing_category=p.category,
            pricing_section=p.section, flags=tuple(dict.fromkeys(p.flags)),
            candidates=_candidates(p.lookup),
        ))
    cat_out: dict[str, Any] = {}
    for cat, entry in sorted(categories.items()):
        rows_for = [r for r in breakdown.rows if r["category"] == cat]
        quote = entry["quote"]
        cat_out[cat] = {
            "section": entry["section"],
            "lines": entry["lines"],
            "library_material": round_cents(entry["library_material"]),
            "quote_id": quote.quote_id if quote else None,
            "quote_amount": round_cents(quote.amount_usd) if quote else None,
            "quote_tax_included": quote.tax_included if quote else None,
            "quote_replaces": entry["replaces"],
            "alternatives": [
                {"quote_id": a.quote_id, "amount_usd": round_cents(a.amount_usd),
                 "tax_included": a.tax_included} for a in entry["alternatives"]
            ],
            "allowances": round_cents(entry["allowances"]) if entry.get("allowances") is not None else None,
            "pre_tax": round_cents(sum((r["pre_tax"] for r in rows_for), _ZERO)),
            "tax": round_cents(sum((r["tax"] for r in rows_for), _ZERO)),
            "total": round_cents(sum((r["total"] for r in rows_for), _ZERO)),
            "rows": rows_for,
        }
    workbook_material = sum(
        (((p.material.value or _ZERO) * p.row.quantity) for p in priced
         if not p.is_ls and not p.blocked and p.material.value is not None), _ZERO
    )
    wb_rate = policy.workbook_labor_rate
    wb_labor = round_cents(basis_hours * wb_rate)
    wb_mat = round_cents(workbook_material)
    ohp_l = policy.workbook_ohp_pct.get("labor", _ZERO)
    ohp_m = policy.workbook_ohp_pct.get("material", _ZERO)
    workbook_basis = {
        "labor_rate": wb_rate,
        "hours": basis_hours.quantize(_SIX),
        "labor_cost": wb_labor,
        "material_pretax": wb_mat,
        "ohp_labor_pct": ohp_l,
        "ohp_material_pct": ohp_m,
        "total_bid": round_cents(wb_labor * (1 + ohp_l / 100) + wb_mat * (1 + ohp_m / 100)),
    }
    quality_summary = {
        "hours_by_quality": {k: str(v.quantize(_SIX)) for k, v in sorted(hours_by_quality.items())},
        "hours_share_by_quality": {k: _share(v, basis_hours) for k, v in sorted(hours_by_quality.items())},
        "material_by_quality": {k: str(round_cents(v)) for k, v in sorted(material_by_quality.items())},
        "material_share_by_quality": {k: _share(v, material_total) for k, v in sorted(material_by_quality.items())},
        "methods": _count(p.lookup.method for p in priced if not p.is_ls and not p.blocked),
        "fallback_share_hours": _share(fallback_hours, basis_hours),
        "fallback_share_material": _share(fallback_material, material_total),
        "unpriced_line_share": unpriced_line_share,
        "coverage_class": coverage,
        "hour_uplift_applied": str(uplift_applied),
        "hour_uplift_range_applied": [str(uplift_low), str(uplift_high)],
        "provisional_reasons": provisional,
        "context": {
            "wage_type": ctx.wage_type,
            "labor_rate_source": rate_source,
            "project_gc_ids": list(ctx.project_gc_ids),
            "eval_case_id": ctx.eval_case_id,
            "exclude_project": exclude_project,
            "pricing_allowed": None if pricing_allowed is None else bool(pricing_allowed[0]),
            "tax_rate_pct": str(policy.tax_rate_pct),
        },
        "sections": {k: {kk: (str(vv) if isinstance(vv, Decimal) else vv) for kk, vv in v.items()}
                     for k, v in breakdown.sections.items()},
        "engine_version": ENGINE_VERSION,
    }
    excluded_ids = set(ctx.exclude_project_ids) | set(library.excluded_projects)
    if exclude_project:
        excluded_ids.add(exclude_project)
    return PricedEstimate(
        status=status,
        lines=lines_out,
        categories=cat_out,
        verify=verify,
        workbook_basis=workbook_basis,
        range_low=range_low,
        range_high=range_high,
        quality_summary=quality_summary,
        unpriced=UnpricedSummary(
            lines=tuple(dict.fromkeys(unpriced_lines)),
            est_hours=fallback_hours.quantize(_SIX),
            est_material=round_cents(fallback_material),
        ),
        blockers=blockers,
        warnings=warnings,
        policy_version=str(ctx.policy_version or policy.version),
        library_fingerprint=library.fingerprint,
        excluded_project_ids_sha256=ec.excluded_project_ids_sha256(excluded_ids),
    )


def _candidates(lookup: Lookup) -> tuple[dict[str, Any], ...]:
    """The matched key first (flagged `matched`), then the library's other
    candidates: aggregates only, never a project or GC name."""
    out: list[dict[str, Any]] = []
    if lookup.matched_key and lookup.method in ("exact", "compatible", "context"):
        out.append({
            "item_key": lookup.matched_key, "uom": None, "n_projects": lookup.labor.n_projects,
            "method": lookup.method, "matched": True, "conversion": lookup.conversion,
        })
        if lookup.matched_uom:
            out[0]["uom"] = lookup.matched_uom
    out.extend(dict(c) for c in lookup.candidates)
    return tuple(out)


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def _range_total(
    priced: Sequence[_Priced], category_rows: Sequence[CategoryRow], policy: PricingPolicy,
    rate: Decimal, hours: Decimal, which: str, uplift: Decimal, pinned_hours: Decimal | None,
) -> Decimal:
    """The bid at every library line's p25 (or p75) and the uplift range end."""
    adjusted: list[CategoryRow] = []
    by_cat: dict[str, Decimal] = {}
    for p in priced:
        if p.is_ls or p.blocked or p.material_cost is None or FLAG_QUOTE_COVERED in p.flags:
            continue
        stat = p.material
        value = getattr(stat, which)
        if value is None:
            value = stat.value
        by_cat[p.category] = by_cat.get(p.category, _ZERO) + (value or _ZERO) * p.row.quantity
    for row in category_rows:
        if row.source == "library":
            adjusted.append(CategoryRow(
                category=row.category, amount_pretax=round_cents(by_cat.get(row.category, _ZERO)),
                tax_included=row.tax_included, section=row.section, source=row.source,
            ))
        else:
            adjusted.append(row)
    reviewer = pinned_hours if pinned_hours is not None else policy.reviewer_hours(hours, uplift=uplift)
    return verify_totals(
        labor_hours=Decimal(str(reviewer)), labor_rate=rate, category_rows=adjusted,
        tax_rate_pct=policy.tax_rate_pct, material_markup_pct=policy.material_markup_pct,
        labor_markup_pct=policy.labor_markup_pct,
    ).bid_total


def _out_of_band(priced: Sequence[_Priced], categories: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Shapes the library was not built for: labor only, gear installs
    without devices, quote-only categories."""
    live = [p for p in priced if not p.is_ls and not p.blocked]
    if not live:
        return []
    out: list[tuple[str, str]] = []
    any_material = any((p.material_cost or _ZERO) > 0 for p in live)
    any_quote = any(e.get("quote") is not None for e in categories.values())
    if not any_material and not any_quote:
        out.append(("labor_only", "every line prices at zero material and there is no quote"))
    gear = sum((p.row.quantity for p in live if p.identity.item_class in GEAR_CLASSES), _ZERO)
    devices = sum((p.row.quantity for p in live if p.identity.item_class in DEVICE_CLASSES), _ZERO)
    if gear > 0 and devices == 0:
        out.append(("gear_without_devices", "gear or breakers without any device line"))
    library_lines = [p for p in live if FLAG_QUOTE_COVERED not in p.flags]
    if any_quote and not library_lines:
        out.append(("quote_only", "every category is priced by a vendor quote"))
    return out


# Pure wrapper ----------------------------------------------------------------------------------


def price_estimate(
    rows: Sequence[PriceRow],
    ctx: PricingContext,
    *,
    library: CostLibrary | None = None,
    policy: PricingPolicy | None = None,
    quotes: Sequence[QuoteRef] = (),
    **kwargs: Any,
) -> PricedEstimate:
    """`compute_estimate` with defaults: an empty library (every line falls to
    `none`, status provisional) and the code policy. All I/O injected."""
    lib = library if library is not None else CostLibrary(())
    pol = policy if policy is not None else DEFAULT_POLICY
    return compute_estimate(rows, lib, pol, list(quotes), ctx, **kwargs)


# Artifact -----------------------------------------------------------------------------------------


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def to_priced_estimate_v1(
    priced: PricedEstimate, *, run_id: str | None = None, wage_type: str | None = None,
    quotes: Sequence[QuoteRef] = (), allowances: Sequence[Allowance] = (),
    exclusions: Sequence[str] = (), clarifications: Sequence[str] = (),
    manual_line_ids: Iterable[str] = (),
    quote_details: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The WS0 artifact `bdr.priced_estimate/1` (spec 5.4), validated by
    estimating_contracts. `refused` is true exactly when the status is
    blocked; the totals stay so the verifier sees the number.

    Additive fields the acceptance detectors read (3.5): a line priced from
    a grade seed carries `spec_fact_ref` (the cited fact, E13); `sections`
    also holds one entry per BOQ group (`boq_group: true`) with
    `material_usd` (pre-tax library material of the group's lines not
    covered by a quote) and `material_source` (`vendor_quote` when every
    priced line of the group is quote covered, else `library`; E9, GC1);
    `quote_details[quote_id]` (reconciled counts, selection reason, chosen)
    is merged into that quote's entry (E9)."""
    manual = {str(m) for m in manual_line_ids}
    ctx = (priced.quality_summary or {}).get("context") or {}
    wage = wage_type if wage_type is not None else ctx.get("wage_type")
    verify = priced.verify
    lines: list[dict[str, Any]] = []
    for ln in priced.lines:
        row = ln.row
        is_manual = ln.line_id in manual
        labor_method = ln.labor.method
        hours_source = ec.hours_source_for(labor_method, manual=is_manual) if (
            labor_method in ec.VALUE_STAT_METHODS and labor_method != "quote"
        ) else ("manual" if is_manual else None)
        allowance_line = FLAG_LS in ln.flags
        material_source, quote_ref = ec.material_source_for(
            ln.material_source, allowance=allowance_line, manual=is_manual
        )
        blocked = FLAG_BLOCKED in ln.flags
        kind = parse_item(row.description, row.unit).item_class
        unit = ln.uom
        # The contract forbids a linear kind in any unit but FT (5.1, 5.4). A
        # bare row was blocked by the engine; a non-bare one (a ladder rack
        # parsed as raceway) is excluded from the artifact with the same
        # reason instead of raising at artifact time.
        linear_in_ea = kind in ec.LINEAR_CLASSES and unit != "FT"
        kind_out: str | None = None if linear_in_ea else kind
        excluded_line = blocked or linear_in_ea
        wastage = _ZERO
        if row.wastage_applies and row.qty_base:
            wastage = (row.quantity - row.qty_base) / row.qty_base
        priced_flag = (
            not excluded_line and not allowance_line
            and ln.labor.value is not None and ln.material.value is not None
        )
        fact_refs = [m.rsplit("@", 1)[1] for m in ln.modifiers if "@" in m] if FLAG_SEED in ln.flags else []
        lines.append({
            "line_id": ln.line_id,
            "spec_fact_ref": fact_refs[0] if fact_refs else None,
            "section": row.section or "",
            "group": row.group or "",
            "subgroup": row.subgroup or "",
            "description": row.description,
            "kind": kind_out,
            "unit": unit,
            "qty_base": _f(row.qty_base),
            "wastage": _f(wastage),
            "qty": _f(row.quantity),
            "quantity_basis": row.quantity_basis,
            "unit_hours": _f(ln.labor.value) if priced_flag else None,
            "unit_material_usd": _f(ln.material.value) if priced_flag else None,
            "hours_source": hours_source if priced_flag else None,
            "material_source": material_source if (priced_flag or allowance_line) else "none",
            "n": ln.labor.n_projects if priced_flag else None,
            "p25": _f(ln.labor.p25) if priced_flag else None,
            "p75": _f(ln.labor.p75) if priced_flag else None,
            "quote_ref": quote_ref if priced_flag else None,
            "priced": bool(priced_flag),
            "excluded": bool(excluded_line or allowance_line),
            "exclusion_reason": (
                f"{BLOCK_LINEAR_IN_EA}:{kind}" if excluded_line
                else (FLAG_LS if allowance_line else None)
            ),
        })
    sections: dict[str, Any] = {}
    for key, entry in ((priced.quality_summary or {}).get("sections") or {}).items():
        sections[key] = {
            "present": bool(entry.get("present")),
            "amount": None if entry.get("amount") is None else float(Decimal(str(entry["amount"]))),
            "markup_pct": None if entry.get("markup_pct") is None else float(Decimal(str(entry["markup_pct"]))),
            "markup": None if entry.get("markup") is None else float(Decimal(str(entry["markup"]))),
            "categories": sorted(c for c, v in priced.categories.items() if v.get("section") == key),
        }
    groups: dict[str, dict[str, Any]] = {}
    for ln in priced.lines:
        name = (ln.row.group or "").strip().upper()
        if not name or FLAG_LS in ln.flags or name in sections:
            continue
        entry = groups.setdefault(name, {"present": True, "boq_group": True, "lines": 0, "covered": 0,
                                         "material": _ZERO, "quote_ids": set()})
        entry["lines"] += 1
        if FLAG_QUOTE_COVERED in ln.flags:
            entry["covered"] += 1
            if ln.material_source.startswith("quote:"):
                entry["quote_ids"].add(ln.material_source.split(":", 1)[1])
        elif FLAG_BLOCKED not in ln.flags and ln.material_cost is not None:
            entry["material"] += ln.material_cost
    for name, entry in groups.items():
        all_quoted = entry["lines"] > 0 and entry["covered"] == entry["lines"]
        sections[name] = {
            "present": True,
            "boq_group": True,
            "material_usd": float(round_cents(entry["material"])),
            "material_source": "vendor_quote" if all_quoted else "library",
            "quote_ids": sorted(entry["quote_ids"]),
            "lines": entry["lines"],
            "quote_covered_lines": entry["covered"],
        }
    pre_tax = sum((Decimal(str(v["pre_tax"])) for v in priced.categories.values()), _ZERO)
    material_usd = sum(
        (verify.numbers()[k] or _ZERO for k in ("materials_amount", "gear_amount",
                                                 "underground_amount", "low_voltage_amount")),
        _ZERO,
    ) if verify else _ZERO
    totals = {
        "hours_raw": _f(verify.labor_hours_basis) if verify else None,
        "hours": _f(verify.labor_hours_reviewer) if verify else None,
        "hour_roundup": (
            _f((verify.labor_hours_reviewer / verify.labor_hours_basis).quantize(Decimal("0.0001")))
            if verify and verify.labor_hours_basis else None
        ),
        "labor_usd": _f(verify.labor_amount) if verify else None,
        "material_pretax_usd": _f(round_cents(pre_tax)),
        "tax_rate_pct": float(Decimal(str(ctx.get("tax_rate_pct") or DEFAULT_TAX_RATE))),
        "material_usd": _f(round_cents(material_usd)) if verify else None,
        "verify": {k: _f(v) for k, v in verify.numbers().items()} if verify else None,
        "bid_total_usd": _f(verify.bid_total) if verify else None,
    }
    doc = {
        "schema": ec.PRICED_ESTIMATE_SCHEMA,
        "run_id": run_id,
        "status": priced.status,
        "refused": ec.refused_for_status(priced.status),
        "refusal_reasons": [f"{b.code}: {b.detail}".rstrip(": ") for b in priced.blockers],
        "wage_type": wage,
        "labor_rate_usd": _f(verify.labor_rate) if verify else None,
        "labor_rate_basis": verify.labor_rate_source if verify else None,
        "lines": lines,
        "allowances": [{"name": a.name, "amount_usd": _f(a.amount_usd)} for a in allowances],
        "quotes": [
            {
                **dict((quote_details or {}).get(q.quote_id) or {}),
                "category": q.category, "vendor": q.vendor or "", "amount_usd": _f(q.amount_usd),
                "tax_included": bool(q.tax_included), "replaces_sections": list(q.replaces_sections),
                "file_sha256": q.file_sha256,
            }
            for q in quotes
        ],
        "sections": sections,
        "totals": totals,
        "exclusions": [str(e) for e in exclusions],
        "clarifications": [str(c) for c in clarifications],
        "metadata": {
            "excluded_project_ids_sha256": priced.excluded_project_ids_sha256,
            "library_fingerprint": priced.library_fingerprint,
        },
        "warnings": [w.to_dict() for w in priced.warnings],
        "provisional_reasons": list((priced.quality_summary or {}).get("provisional_reasons") or []),
    }
    return ec.validate_priced_estimate(doc, status=priced.status)


__all__ = [
    "ENGINE_VERSION", "CategoryRow", "VerifyBreakdown", "verify_totals", "compute_estimate",
    "price_estimate", "to_priced_estimate_v1", "category_for", "choose_quotes", "taxed_quote_amount",
    "PricingError", "GROUP_CATEGORY", "UOM_CONFLICT_CLASSES", "is_bare_length_item",
    "BLOCK_LINEAR_IN_EA", "BLOCK_WAGE_TYPE_UNKNOWN", "BLOCK_PRICING_NOT_ALLOWED",
    "FLAG_NOT_IN_QUOTE", "FLAG_MANUAL_ELIGIBLE", "quote_exclusions",
]
