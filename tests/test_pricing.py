"""Unit tests for the verify-step (9) override delta logic and the per-category
materials price precedence (pure, no DB)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    QuoteIn,
    QuoteOverrideIn,
    TaxIn,
    RfqCustomPriceIn,
    VerifyOverrideIn,
)
from app.routers.pricing import (
    DEFAULT_TAX_RATE,
    VERIFY_NUMBERS,
    apply_tax,
    pick_material_amount,
    pricing_summary_numbers,
    taxed_amount,
    verify_delta_pairs,
)


# ── pick_material_amount: custom > selected > lowest ──────────────────────


def test_custom_price_beats_selected_and_lowest():
    amount, source = pick_material_amount(Decimal("900"), Decimal("1000"), Decimal("800"))
    assert (amount, source) == (Decimal("900"), "manual")


def test_selected_quote_beats_lowest():
    amount, source = pick_material_amount(None, Decimal("1200"), Decimal("1000"))
    assert (amount, source) == (Decimal("1200"), "quote")


def test_lowest_quote_is_the_default():
    amount, source = pick_material_amount(None, None, Decimal("1000"))
    assert (amount, source) == (Decimal("1000"), "quote")


def test_no_quotes_means_no_price():
    assert pick_material_amount(None, None, None) == (None, "none")


def test_zero_amounts_are_real_values_not_missing():
    # Decimal("0") must not be treated as falsy/absent at any precedence level.
    assert pick_material_amount(Decimal("0"), Decimal("5"), Decimal("3")) == (Decimal("0"), "manual")
    assert pick_material_amount(None, Decimal("0"), Decimal("3")) == (Decimal("0"), "quote")


# ── apply_tax: tax-inclusive quote cost (the true cost incurred) ──────────


def test_tax_included_leaves_amount_unchanged():
    # Vendor already priced tax in — no tax added, tax amount is 0.
    assert apply_tax(Decimal("1000"), True, Decimal("8.375")) == (Decimal("1000"), Decimal("0.00"))


def test_tax_not_included_applies_rate_rounded_to_cents():
    # 1000 * 8.375% = 83.75 → total 1083.75.
    total, tax = apply_tax(Decimal("1000"), False, Decimal("8.375"))
    assert (total, tax) == (Decimal("1083.75"), Decimal("83.75"))


def test_tax_rounds_half_up_to_cents():
    # 12.34 * 8.375% = 1.033475 → rounds to 1.03; total 13.37.
    total, tax = apply_tax(Decimal("12.34"), False, Decimal("8.375"))
    assert (total, tax) == (Decimal("13.37"), Decimal("1.03"))


def test_null_tax_included_is_treated_as_not_yet_included():
    # Unanswered must not understate the cost — tax is applied at the given rate.
    total, tax = apply_tax(Decimal("200"), None, Decimal("10"))
    assert (total, tax) == (Decimal("220.00"), Decimal("20.00"))


def test_missing_rate_falls_back_to_default():
    total, tax = apply_tax(Decimal("100"), False, None)
    expected = (Decimal("100") * DEFAULT_TAX_RATE / Decimal(100)).quantize(Decimal("0.01"))
    assert tax == expected
    assert total == Decimal("100") + expected


def test_no_price_means_no_amount_and_no_tax():
    assert apply_tax(None, False, Decimal("8.375")) == (None, Decimal("0.00"))


# ── taxed_amount: the per-quote comparison basis ────────────────────


def test_taxed_amount_adds_tax_when_not_included():
    q = {"amount": "1000", "tax_included": False, "tax_rate": "8.375"}
    assert taxed_amount(q) == Decimal("1083.75")


def test_taxed_amount_unanswered_assumes_tax_not_included():
    # NULL (unanswered) must never understate the cost.
    q = {"amount": "1000", "tax_included": None, "tax_rate": "8.375"}
    assert taxed_amount(q) == Decimal("1083.75")


def test_taxed_amount_included_is_amount_as_is():
    q = {"amount": "1000", "tax_included": True, "tax_rate": "8.375"}
    assert taxed_amount(q) == Decimal("1000")


def test_taxed_comparison_can_reorder_vendors():
    # A, cheaper pre-tax without tax, is truly pricier than B with tax included.
    a = {"amount": "1000", "tax_included": False, "tax_rate": "8.375"}
    b = {"amount": "1050", "tax_included": True, "tax_rate": "8.375"}
    assert taxed_amount(a) > taxed_amount(b)


# ── TaxIn: tax attestation bounds ────────────────────────────────────


def test_tax_rate_defaults_to_clark_county():
    body = TaxIn(tax_included=False)
    assert body.tax_rate == Decimal("8.375")


def test_tax_rate_rejects_over_100_percent():
    with pytest.raises(ValidationError):
        TaxIn(tax_included=False, tax_rate=Decimal("101"))


def test_tax_rate_rejects_negative():
    with pytest.raises(ValidationError):
        TaxIn(tax_included=False, tax_rate=Decimal("-1"))


# ── Hand-entered amounts: no negatives, no numeric(14,2) overflow ──────────


def test_custom_price_rejects_negative_amounts():
    with pytest.raises(ValidationError):
        RfqCustomPriceIn(amount=Decimal("-1"))


def test_custom_price_rejects_numeric_overflow():
    # numeric(14,2) tops out below 10^12 — reject before the DB write 500s.
    with pytest.raises(ValidationError):
        RfqCustomPriceIn(amount=Decimal("1000000000000"))
    # In range but rounds past the column limit at scale 2 — must also reject.
    with pytest.raises(ValidationError):
        RfqCustomPriceIn(amount=Decimal("999999999999.999"))


def test_custom_price_rejects_sub_cent_precision():
    with pytest.raises(ValidationError):
        RfqCustomPriceIn(amount=Decimal("12.345"))


def test_custom_price_accepts_zero_null_and_float_cents():
    assert RfqCustomPriceIn(amount=Decimal("0")).amount == Decimal("0")
    assert RfqCustomPriceIn().amount is None  # null clears the custom price
    # JSON numbers arrive as floats; pydantic must not reject e.g. 1234.56.
    assert RfqCustomPriceIn(amount=1234.56).amount == Decimal("1234.56")


def test_quote_amounts_share_the_bounds():
    with pytest.raises(ValidationError):
        QuoteIn(vendor_id="v", amount=Decimal("-5"))
    with pytest.raises(ValidationError):
        QuoteOverrideIn(amount=Decimal("-5"))


def test_delta_records_changed_number():
    originals = {"labor_amount": Decimal("1000.00")}
    body = VerifyOverrideIn(labor_amount=Decimal("1200.00"))
    pairs = verify_delta_pairs(originals, body)
    assert pairs["labor_amount"] == {"from": "1000.00", "to": "1200.00"}


def test_delta_covers_all_four_numbers():
    originals = {
        "labor_amount": Decimal("100"),
        "materials_amount": Decimal("200"),
        "labor_markup_amount": Decimal("10"),
        "materials_markup_amount": Decimal("20"),
    }
    body = VerifyOverrideIn(
        labor_amount=Decimal("100"),
        materials_amount=Decimal("250"),
        labor_markup_amount=Decimal("10"),
        materials_markup_amount=Decimal("20"),
    )
    pairs = verify_delta_pairs(originals, body)
    assert set(pairs) == set(VERIFY_NUMBERS)
    # Only materials changed; the rest carry equal from/to.
    assert pairs["materials_amount"] == {"from": "200", "to": "250"}
    assert pairs["labor_amount"]["from"] == pairs["labor_amount"]["to"]


def test_delta_handles_missing_original_and_final():
    # No upstream value and no override → both sides null (no spurious change).
    pairs = verify_delta_pairs({}, VerifyOverrideIn())
    for key in VERIFY_NUMBERS:
        assert pairs[key] == {"from": None, "to": None}


def test_delta_records_first_time_value():
    # Materials had no upstream basis but the Exec entered a figure at verify.
    pairs = verify_delta_pairs({"materials_amount": None}, VerifyOverrideIn(materials_amount=Decimal("500")))
    assert pairs["materials_amount"] == {"from": None, "to": "500"}


# ── pricing_summary_numbers: the summary-box headline figures ──────────────


def test_summary_all_blank_before_any_step():
    summary = pricing_summary_numbers({}, None)
    assert summary == {
        "materials_amount": None,
        "labor_amount": None,
        "markup_amount": None,
        "bid_price": None,
    }


def test_summary_fields_fill_in_independently():
    originals = {
        "materials_amount": Decimal("2000"),
        "labor_amount": Decimal("1000"),
        "labor_markup_amount": Decimal("100"),
        # materials markup not set yet — labor markup alone still shows.
    }
    summary = pricing_summary_numbers(originals, None)
    assert summary["materials_amount"] == "2000"
    assert summary["labor_amount"] == "1000"
    assert summary["markup_amount"] == "100"
    assert summary["bid_price"] is None  # nothing committed yet


def test_summary_markup_sums_both_sides():
    originals = {
        "labor_markup_amount": Decimal("100"),
        "materials_markup_amount": Decimal("250.50"),
    }
    assert pricing_summary_numbers(originals, None)["markup_amount"] == "350.50"


def test_summary_bid_price_requires_commit():
    originals = {
        "labor_amount": Decimal("1000"),
        "materials_amount": Decimal("2000"),
        "labor_markup_amount": Decimal("100"),
        "materials_markup_amount": Decimal("200"),
    }
    # Saved but uncommitted verification → still no bid price.
    uncommitted = {"committed_at": None, "labor_amount": "1100"}
    assert pricing_summary_numbers(originals, uncommitted)["bid_price"] is None

    committed = {"committed_at": "2026-06-10T00:00:00Z", "labor_amount": "1100"}
    summary = pricing_summary_numbers(originals, committed)
    # Override wins for labor; the rest fall back to the upstream originals.
    assert summary["bid_price"] == "3400"


def test_summary_bid_price_handles_zero_override():
    # Decimal("0") in the committed snapshot is a real figure, not "unset".
    originals = {"labor_amount": Decimal("1000"), "materials_amount": Decimal("2000")}
    committed = {"committed_at": "2026-06-10T00:00:00Z", "labor_amount": "0"}
    assert pricing_summary_numbers(originals, committed)["bid_price"] == "2000"
