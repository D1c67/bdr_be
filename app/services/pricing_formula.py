"""The pure arithmetic of G3's Verify step (AI_ESTIMATING_BUILD_SPEC WS6).

These functions and constants used to live in `app/routers/pricing.py`. They
moved here unchanged so the human pricing pipeline (the router) and the AI
pricing engine (`pricing_engine.verify_totals`) run the SAME code: a
verify-replay test that reproduces a committed Verify snapshot therefore
tests the real formula, not a copy. The router imports every name back, so
existing importers (`routers/rfqs.py`, `services/proposal_send.py`,
`services/analytics_metrics.py`, the pricing tests) keep working.

Rules reproduced here (checked against the 2026-10-01 production backup):

- tax (`DEFAULT_TAX_RATE`, 8.375% Clark County) applies to material, never to
  labor, per priced row (a vendor quote or the General Material figure) and is
  rounded to cents ROUND_HALF_UP per row; a quote that already includes tax
  stands as quoted; an unanswered tax question counts as "not included";
- `section_summary` partitions the materials rows into the four pricing
  sections (`SECTION_KEYS`); `materials` is the residual and always present,
  the breakouts exist only while a row maps to them;
- `VERIFY_NUMBERS` are the ten figures the Verify step resolves, snapshots and
  audits; `VERIFY_SECTION_NUMBERS` is the section-scoped subset whose NULL on
  a committed snapshot means "not part of the decomposition", never 0.

Pure and sync: no I/O, no database, no FastAPI.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Clark County (Las Vegas) sales tax, the default rate applied when a vendor's
# quote did not already include it.
DEFAULT_TAX_RATE = Decimal("8.375")

# Pricing sections: the named buckets material categories roll into for markup,
# verify, per-GC pricing and the proposal box. 'materials' is the residual (and
# always present); the breakouts exist on a project only while it has an RFQ in
# a category mapped to them (material_categories.pricing_section).
SECTION_KEYS = ("materials", "gear", "underground", "low_voltage")

# The ten figures the verify step resolves, snapshots and audits. Order matters
# for the audit delta and the FE row order. materials_amount is the RESIDUAL
# materials figure; the three breakout amounts complete the partition.
VERIFY_NUMBERS = (
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

# The section-scoped subset: on a COMMITTED snapshot a NULL here means "this
# section was not part of the committed decomposition" (absent section, or the
# legacy pre-sections snapshot) and resolves to None, never 0. The four legacy
# keys keep resolving NULL to 0.
VERIFY_SECTION_NUMBERS = (
    "gear_amount",
    "underground_amount",
    "low_voltage_amount",
    "gear_markup_amount",
    "underground_markup_amount",
    "low_voltage_markup_amount",
)

CENT = Decimal("0.01")


def round_cents(value: Decimal) -> Decimal:
    """Cents, ROUND_HALF_UP: the rounding every Verify figure uses."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def apply_tax(
    amount: Decimal | None, tax_included: bool | None, tax_rate: Decimal | None
) -> tuple[Decimal | None, Decimal]:
    """Pure: the tax-inclusive cost of one vendor quote and the tax added.
    When the vendor already priced tax in (or there's no amount) the quote
    stands and the tax added is 0; otherwise the rate (a percent) is applied
    and rounded to cents: the real cost G3 incurs on the materials. A NULL
    tax_included (unanswered) is treated as "not yet included" so the figure is
    never understated before the estimator answers."""
    if amount is None:
        return None, Decimal("0.00")
    if tax_included:  # vendor already included tax in the quote
        return amount, Decimal("0.00")
    rate = tax_rate if tax_rate is not None else DEFAULT_TAX_RATE
    tax = (amount * rate / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
    return amount + tax, tax


def tax_info(row: dict) -> dict:
    """Pure: the tax breakdown of one priced row (a vendor quote or the General
    Material estimate, anything with amount/tax_included/tax_rate): pre-tax
    amount, tax added at its rate, and the tax-inclusive total that pricing
    compares and carries."""
    pre = Decimal(str(row["amount"]))
    rate = Decimal(str(row["tax_rate"])) if row.get("tax_rate") is not None else DEFAULT_TAX_RATE
    total, tax = apply_tax(pre, row.get("tax_included"), rate)
    return {
        "pre_tax": pre,
        "total": total,
        "tax": tax,
        "rate": rate,
        "included": row.get("tax_included"),
    }


def taxed_amount(row: dict) -> Decimal:
    """Pure: the tax-inclusive amount of one priced row, the comparison basis."""
    return tax_info(row)["total"]


def section_summary(rows: list[dict]) -> dict[str, dict]:
    """Pure: the section decomposition of the materials rows (_materials_rows).
    A section is present iff the project has a row mapped to it ('materials' is
    always present, the residual bucket). amount is the sum of that section's
    priced rows: Decimal(0) when present but unpriced, None when absent, so the
    sections partition the materials total exactly (no row counted twice, none
    dropped). The gear entry also says whether a generator category is on the
    project (drives the proposal's "*Includes Generator/s" caption)."""
    out: dict[str, dict] = {}
    for key in SECTION_KEYS:
        mine = [r for r in rows if (r.get("pricing_section") or "materials") == key]
        present = key == "materials" or bool(mine)
        amount = (
            sum(
                (Decimal(r["amount"]) for r in mine if r.get("amount") is not None),
                Decimal(0),
            )
            if present
            else None
        )
        mine.sort(key=lambda r: (r.get("category_sort_order") is None,
                                 r.get("category_sort_order") or 0))
        entry: dict = {
            "present": present,
            "amount": amount,
            "categories": [r["category_name"] for r in mine if r.get("category_name")],
        }
        if key == "gear":
            entry["includes_generator"] = any(
                "generator" in (r.get("category_name") or "").lower() for r in mine
            )
        out[key] = entry
    return out


def markup_amount(amount: Decimal, pct: Decimal) -> Decimal:
    """Pure: a markup of `pct` percent on `amount`, in cents ROUND_HALF_UP (the
    Verify markup is applied to the TAX-INCLUSIVE section amount)."""
    return round_cents(amount * pct / Decimal(100))


def verify_sum(numbers: dict[str, Decimal | None]) -> Decimal:
    """Pure: the bid total of a Verify decomposition, None counted as 0."""
    return round_cents(sum((v for v in numbers.values() if v is not None), Decimal(0)))
