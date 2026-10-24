"""app/services/pricing_formula.py: the Verify arithmetic moved out of the
pricing router, and pricing_engine.verify_totals built on it. Synthetic
numbers only; pure and offline."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.routers import pricing as router
from app.services import pricing_formula as pf
from app.services.pricing_engine import CategoryRow, verify_totals


def test_router_re_exports_the_moved_names_unchanged():
    # The router keeps every name so its importers (rfqs, proposal_send,
    # analytics, the pricing tests) keep working; they are the same objects.
    assert router.apply_tax is pf.apply_tax
    assert router.tax_info is pf.tax_info
    assert router.taxed_amount is pf.taxed_amount
    assert router.section_summary is pf.section_summary
    assert router.VERIFY_NUMBERS is pf.VERIFY_NUMBERS
    assert router.VERIFY_SECTION_NUMBERS is pf.VERIFY_SECTION_NUMBERS
    assert router.DEFAULT_TAX_RATE is pf.DEFAULT_TAX_RATE
    assert router.SECTION_KEYS is pf.SECTION_KEYS
    assert len(pf.VERIFY_NUMBERS) == 10
    assert set(pf.VERIFY_SECTION_NUMBERS) < set(pf.VERIFY_NUMBERS)


def test_apply_tax_rounds_each_row_half_up():
    total, tax = pf.apply_tax(Decimal("1000.05"), False, Decimal("8.375"))
    assert tax == Decimal("83.75")  # 83.754 rounds down
    assert total == Decimal("1083.80")
    total, tax = pf.apply_tax(Decimal("100.06"), None, Decimal("8.375"))
    assert tax == Decimal("8.38")  # 8.380 exactly; NULL counts as not included
    assert pf.apply_tax(Decimal("50"), True, Decimal("8.375")) == (Decimal("50"), Decimal("0.00"))
    assert pf.apply_tax(None, False, None) == (None, Decimal("0.00"))


def test_markup_and_round_cents():
    assert pf.round_cents(Decimal("1.005")) == Decimal("1.01")
    assert pf.markup_amount(Decimal("35758.33"), Decimal("10")) == Decimal("3575.83")
    assert pf.markup_amount(Decimal("100"), Decimal("0")) == Decimal("0.00")
    assert pf.verify_sum({"a": Decimal("1.10"), "b": None, "c": Decimal("2.20")}) == Decimal("3.30")


def test_verify_totals_formula_to_the_cent_with_per_category_rounding():
    # Two taxable material rows in the residual section, taxed separately
    # (per-category cent rounding), one quote with tax included, one gear
    # row; markup on the tax-inclusive section amount; labor = hours x rate.
    rows = [
        CategoryRow("General Material", Decimal("1234.56"), False),
        CategoryRow("Lighting", Decimal("2000.01"), False, quote_id="q1", source="quote"),
        CategoryRow("Demolition", Decimal("300.00"), True),
        CategoryRow("Switchgear", Decimal("5000.00"), False, section="gear"),
    ]
    out = verify_totals(
        labor_hours=Decimal("150"), labor_rate=Decimal("90"), category_rows=rows,
        tax_rate_pct=Decimal("8.375"),
        material_markup_pct={"materials": Decimal("10"), "gear": Decimal("5")},
        labor_markup_pct=Decimal("0"),
    )
    # 1234.56 -> tax 103.39 (103.394) ; 2000.01 -> 167.50 (167.5008) ; 300 untaxed
    assert out.numbers["materials_amount"] == Decimal("1337.95") + Decimal("2167.51") + Decimal("300.00")
    assert out.numbers["materials_markup_amount"] == pf.markup_amount(out.numbers["materials_amount"], Decimal("10"))
    assert out.numbers["gear_amount"] == Decimal("5418.75")  # 5000 x 1.08375 exactly
    assert out.numbers["gear_markup_amount"] == Decimal("270.94")  # 270.9375 half up
    assert out.numbers["labor_amount"] == Decimal("13500.00")
    assert out.numbers["labor_markup_amount"] == Decimal("0.00")
    assert out.numbers["underground_amount"] is None
    assert out.numbers["low_voltage_markup_amount"] is None
    assert list(out.numbers) == list(pf.VERIFY_NUMBERS)
    assert out.bid_total == pf.verify_sum(out.numbers)
    assert out.bid_total == Decimal("13500.00") + Decimal("3805.46") + Decimal("380.55") + Decimal("5418.75") + Decimal("270.94")


def test_verify_totals_present_sections_and_recorded_markup_amounts():
    rows = [CategoryRow("General Material", Decimal("100.00"), False)]
    out = verify_totals(
        labor_hours=Decimal("10"), labor_rate=Decimal("100"), category_rows=rows,
        tax_rate_pct=Decimal("8.375"), material_markup_pct={"materials": Decimal("10")},
        present_sections=("low_voltage",),
        markup_amounts={"materials": Decimal("7.77")}, labor_markup_amount=Decimal("1.11"),
    )
    assert out.numbers["low_voltage_amount"] == Decimal("0.00")
    assert out.numbers["low_voltage_markup_amount"] == Decimal("0.00")
    assert out.numbers["materials_markup_amount"] == Decimal("7.77")  # recorded wins over pct
    assert out.numbers["labor_markup_amount"] == Decimal("1.11")
    assert out.bid_total == Decimal("1000.00") + Decimal("108.38") + Decimal("7.77") + Decimal("1.11")


def test_category_row_rejects_unknown_section():
    with pytest.raises(ValueError):
        CategoryRow("X", Decimal("1"), False, section="misc")
