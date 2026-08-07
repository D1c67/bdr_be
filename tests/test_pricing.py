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
    SECTION_KEYS,
    VERIFY_NUMBERS,
    VERIFY_SECTION_NUMBERS,
    apply_tax,
    pick_material_amount,
    pricing_summary_numbers,
    section_summary,
    taxed_amount,
    verify_delta_pairs,
)


# ── pick_material_amount: the selected quote IS the price ─────────────────
# There is no precedence chain. A category is priced by the quote a human picked
# on Select Vendors, or it is not priced at all.


def _quote(amount, *, origin="vendor", tax_included=True, tax_rate=None):
    return {
        "amount": amount,
        "origin": origin,
        "tax_included": tax_included,
        "tax_rate": tax_rate,
    }


def test_selected_quote_is_the_price():
    amount, source = pick_material_amount(_quote("1200"))
    assert (amount, source) == (Decimal("1200"), "vendor")


def test_hand_entered_quote_has_no_special_standing():
    # A manual candidate prices the category the same way a vendor's does — it wins
    # only because it was selected, never because of what it is.
    amount, source = pick_material_amount(_quote("900", origin="manual"))
    assert (amount, source) == (Decimal("900"), "manual")


def test_general_material_estimate_is_just_another_candidate():
    amount, source = pick_material_amount(_quote("450", origin="estimate"))
    assert (amount, source) == (Decimal("450"), "estimate")


def test_nothing_selected_means_no_price():
    # Not "fall back to the cheapest" — unpriced, which is what holds Select Vendors
    # open until someone decides.
    assert pick_material_amount(None) == (None, "none")


def test_price_is_tax_inclusive():
    # The winner's own tax answer applies; an unanswered question is treated as
    # "tax not yet included" so the figure is never understated.
    amount, _ = pick_material_amount(_quote("1000", tax_included=False, tax_rate="10"))
    assert amount == Decimal("1100.00")
    amount, _ = pick_material_amount(_quote("1000", tax_included=None, tax_rate="10"))
    assert amount == Decimal("1100.00")


def test_zero_is_a_real_price_not_a_missing_one():
    assert pick_material_amount(_quote("0")) == (Decimal("0"), "vendor")


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


def test_delta_covers_all_ten_numbers():
    originals = {
        "labor_amount": Decimal("100"),
        "materials_amount": Decimal("200"),
        "gear_amount": Decimal("50"),
        "labor_markup_amount": Decimal("10"),
        "materials_markup_amount": Decimal("20"),
    }
    body = VerifyOverrideIn(
        labor_amount=Decimal("100"),
        materials_amount=Decimal("250"),
        gear_amount=Decimal("75"),
        labor_markup_amount=Decimal("10"),
        materials_markup_amount=Decimal("20"),
    )
    pairs = verify_delta_pairs(originals, body)
    assert set(pairs) == set(VERIFY_NUMBERS)
    assert len(VERIFY_NUMBERS) == 10
    # Only materials and gear changed; the rest carry equal from/to.
    assert pairs["materials_amount"] == {"from": "200", "to": "250"}
    assert pairs["gear_amount"] == {"from": "50", "to": "75"}
    assert pairs["labor_amount"]["from"] == pairs["labor_amount"]["to"]
    # Absent sections carry null on both sides: no spurious change recorded.
    assert pairs["underground_amount"] == {"from": None, "to": None}
    assert pairs["low_voltage_markup_amount"] == {"from": None, "to": None}


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


def test_summary_markup_sums_all_five_parts():
    originals = {
        "labor_markup_amount": Decimal("100"),
        "materials_markup_amount": Decimal("250.50"),
        "gear_markup_amount": Decimal("40"),
        "underground_markup_amount": None,  # section absent, never a 0
        "low_voltage_markup_amount": Decimal("9.50"),
    }
    assert pricing_summary_numbers(originals, None)["markup_amount"] == "400.00"


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

    # A committed snapshot is read as-is (the commit stored every resolved
    # number); the live originals never feed the bid price again.
    committed = {
        "committed_at": "2026-06-10T00:00:00Z",
        "labor_amount": "1100",
        "materials_amount": "2000",
        "labor_markup_amount": "100",
        "materials_markup_amount": "200",
    }
    summary = pricing_summary_numbers(originals, committed)
    assert summary["bid_price"] == "3400"


def test_summary_bid_price_sums_committed_sections():
    committed = {
        "committed_at": "2026-06-10T00:00:00Z",
        "labor_amount": "1000",
        "materials_amount": "500",
        "gear_amount": "300",
        "underground_amount": "150",
        "low_voltage_amount": None,  # section not on the project
        "labor_markup_amount": "100",
        "materials_markup_amount": "50",
        "gear_markup_amount": "30",
        "underground_markup_amount": "0",
        "low_voltage_markup_amount": None,
    }
    assert pricing_summary_numbers({}, committed)["bid_price"] == "2130"


def test_summary_bid_price_legacy_committed_snapshot_is_unchanged():
    # A pre-sections snapshot: materials_amount carries the FULL figure and all
    # six section columns are NULL. The bid price must be exactly the old
    # four-number sum; NULL sections resolve to "not part of the
    # decomposition", never to a live figure that would double count.
    originals = {"gear_amount": Decimal("99999")}  # live figure must be ignored
    committed = {
        "committed_at": "2025-12-01T00:00:00Z",
        "labor_amount": "1000",
        "materials_amount": "2300",
        "labor_markup_amount": "100",
        "materials_markup_amount": "200",
    }
    assert pricing_summary_numbers(originals, committed)["bid_price"] == "3600"


def test_summary_bid_price_handles_zero_override():
    # Decimal("0") in the committed snapshot is a real figure, not "unset".
    committed = {
        "committed_at": "2026-06-10T00:00:00Z",
        "labor_amount": "0",
        "materials_amount": "2000",
    }
    assert pricing_summary_numbers({}, committed)["bid_price"] == "2000"


# ── section_summary: the per-section materials partition ───────────────────


def _row(name, section, amount, sort_order=0):
    return {
        "category_name": name,
        "pricing_section": section,
        "category_sort_order": sort_order,
        "amount": amount,
    }


SECTION_ROWS = [
    _row("General Material", "materials", "1000.00", 10),
    _row("Switchgear", "gear", "500.00", 20),
    _row("Generator & Equipment", "gear", "250.00", 30),
    _row("Lighting", "materials", "300.00", 40),
    _row("Low Voltage", "low_voltage", "120.00", 50),
    _row("Trenching", "underground", None, 55),  # on the project, unpriced
]


def test_sections_partition_the_materials_total():
    # Every row lands in exactly one section, so the section amounts must sum
    # to the sum over ALL rows (the old _materials_total): nothing counted
    # twice, nothing dropped.
    sections = section_summary(SECTION_ROWS)
    total_of_rows = sum(
        (Decimal(r["amount"]) for r in SECTION_ROWS if r["amount"] is not None),
        Decimal(0),
    )
    total_of_sections = sum(
        (s["amount"] for s in sections.values() if s["amount"] is not None),
        Decimal(0),
    )
    assert total_of_sections == total_of_rows == Decimal("2170.00")
    # And the residual excludes the breakouts.
    assert sections["materials"]["amount"] == Decimal("1300.00")
    assert sections["gear"]["amount"] == Decimal("750.00")
    assert sections["low_voltage"]["amount"] == Decimal("120.00")


def test_section_present_but_unpriced_is_zero_not_none():
    sections = section_summary(SECTION_ROWS)
    assert sections["underground"]["present"] is True
    assert sections["underground"]["amount"] == Decimal("0")
    assert sections["underground"]["categories"] == ["Trenching"]


def test_absent_section_is_none_and_materials_always_present():
    sections = section_summary([])
    assert set(sections) == set(SECTION_KEYS)
    assert sections["materials"]["present"] is True
    assert sections["materials"]["amount"] == Decimal("0")
    for key in ("gear", "underground", "low_voltage"):
        assert sections[key]["present"] is False
        assert sections[key]["amount"] is None
        assert sections[key]["categories"] == []


def test_rows_without_section_flag_fall_into_the_residual():
    # Defensive: a row missing pricing_section (older cache, synthesized row)
    # counts as residual materials rather than vanishing.
    sections = section_summary([{"category_name": "Custom", "amount": "10"}])
    assert sections["materials"]["amount"] == Decimal("10")
    assert sections["materials"]["categories"] == ["Custom"]


def test_gear_includes_generator_flag():
    assert section_summary(SECTION_ROWS)["gear"]["includes_generator"] is True
    no_gen = [_row("Switchgear", "gear", "500.00", 20)]
    assert section_summary(no_gen)["gear"]["includes_generator"] is False
    # A generator category NOT on the project (no row) never sets the flag.
    assert section_summary([])["gear"]["includes_generator"] is False


def test_section_categories_listed_in_sort_order():
    rows = [
        _row("Generator & Equipment", "gear", "1", 30),
        _row("Switchgear", "gear", "1", 20),
    ]
    assert section_summary(rows)["gear"]["categories"] == [
        "Switchgear",
        "Generator & Equipment",
    ]


def test_verify_section_numbers_are_the_six_section_keys():
    assert set(VERIFY_SECTION_NUMBERS) <= set(VERIFY_NUMBERS)
    assert len(VERIFY_SECTION_NUMBERS) == 6
