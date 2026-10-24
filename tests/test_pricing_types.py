"""Contract tests for app/services/pricing_types.py (AI_ESTIMATING_BUILD_SPEC 5.4).

Pure and offline. Every number below is synthetic.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from app.services import pricing_types as pt


def _row(line_id: str = "B1", **kw) -> pt.PriceRow:
    base = dict(
        line_id=line_id,
        description="DUPLEX RECEPTACLE",
        group="DEVICES",
        subgroup="RECEPTACLES",
        section="NEW WORK",
        unit="EA",
        quantity=Decimal("12"),
        qty_base=Decimal("12"),
        wastage_applies=False,
        material_note="HOSPITAL GRADE",
        caveat=None,
        sheet_refs=("E2.01", "E2.02"),
        quantity_basis="counted",
        item_key="receptacle|duplex|20a",
    )
    base.update(kw)
    return pt.PriceRow(**base)


def _line(line_id: str = "B1", **kw) -> pt.PricedLine:
    base = dict(
        line_id=line_id,
        row=_row(line_id),
        item_key="receptacle|duplex|20a",
        family="receptacle",
        uom="EA",
        qty_eff=Decimal("12"),
        labor=pt.ValueStat(
            value=Decimal("0.75"),
            p25=Decimal("0.5"),
            p75=Decimal("1.0"),
            n_obs=9,
            n_projects=7,
            latest_seen="2026-08",
            quality="good",
            method="exact",
        ),
        material=pt.ValueStat(
            value=Decimal("11.11"),
            n_obs=3,
            n_projects=3,
            latest_seen="2026-05",
            quality="weak",
            method="seed",
        ),
        modifiers=("hospital_grade",),
        material_source="seed",
        labor_hours=Decimal("9.00"),
        material_cost=Decimal("133.32"),
        pricing_category="GENERAL MATERIAL",
        pricing_section=None,
        flags=("grade_seed",),
        candidates=({"item_key": "receptacle|duplex|20a", "score": Decimal("0.93")},),
    )
    base.update(kw)
    return pt.PricedLine(**base)


def _estimate() -> pt.PricedEstimate:
    quote_line = _line(
        "B2",
        material_source="quote:q-1",
        material=pt.ValueStat(value=Decimal("100.00"), quality="quote", method="quote"),
    )
    return pt.PricedEstimate(
        status="provisional",
        lines=[_line("B1"), quote_line],
        categories={"GENERAL MATERIAL": {"pretax": Decimal("133.32"), "lines": ["B1"]}},
        verify=pt.VerifyDraft(
            labor_amount=Decimal("1000.00"),
            materials_amount=Decimal("144.49"),
            gear_amount=Decimal("500.00"),
            materials_markup_amount=Decimal("14.45"),
            bid_total=Decimal("1658.94"),
            labor_hours_basis=Decimal("9.00"),
            labor_hours_reviewer=Decimal("10"),
            labor_rate=Decimal("100.00"),
            labor_rate_source="default",
        ),
        workbook_basis={"hours": Decimal("9.00")},
        range_low=Decimal("1500.00"),
        range_high=Decimal("1800.00"),
        quality_summary={"strong": "0.10", "good": "0.60"},
        unpriced=pt.UnpricedSummary(lines=("B9",), est_hours=Decimal("1.5"), est_material=5),
        blockers=[],
        warnings=[pt.PricingIssue("fallback_price", "one line at a fallback", ("B9",))],
        policy_version="policy-test-1",
        library_fingerprint="lib-abc",
        excluded_project_ids_sha256=pt.excluded_projects_sha256(["p2", "p1"]),
    )


# Round trips ------------------------------------------------------------------


def test_priced_estimate_round_trip_through_json():
    est = _estimate()
    data = est.to_dict()
    text = json.dumps(data)  # JSON-safe: no Decimal, tuple or set left
    back = pt.PricedEstimate.from_dict(json.loads(text))
    assert back.to_dict() == data
    assert back.verify is not None
    assert back.verify.bid_total == Decimal("1658.94")
    assert back.verify.underground_amount is None
    assert back.lines[0].labor.value == Decimal("0.75")
    assert back.lines[0].row == est.lines[0].row
    assert back.lines[0].labor == est.lines[0].labor
    assert back.line("B2").quote_id == "q-1"
    assert back.line("B404") is None
    assert back.unpriced.est_material == Decimal("5")
    assert back.warnings[0].line_ids == ("B9",)


def test_money_serializes_as_exact_strings():
    data = _estimate().to_dict()
    assert data["verify"]["materials_amount"] == "144.49"
    assert data["lines"][0]["material_cost"] == "133.32"
    assert data["lines"][0]["candidates"][0]["score"] == "0.93"
    assert data["categories"]["GENERAL MATERIAL"]["pretax"] == "133.32"
    assert data["schema_version"] == pt.SCHEMA_VERSION


def test_pricing_context_round_trip_and_defaults():
    ctx = pt.PricingContext(
        wage_type="prevailing_wage",
        labor_rate_usd=Decimal("125.00"),
        labor_rate_source="project_record",
        rate_overrides={"gc-1": Decimal("110")},
        project_gc_ids=("gc-1",),
        spec_facts=(pt.SpecFact("device_grade", "hospital", "F3"),),
        allowances=(pt.Allowance("PERMIT"),),
        policy_version="policy-test-1",
        exclude_project_ids=frozenset({"p3", "p1"}),
        eval_case_id="case-x",
    )
    assert ctx.tax_rate_pct == Decimal("8.375")
    assert ctx.material_markup_pct == Decimal("10")
    assert ctx.labor_markup_pct == Decimal("0")
    assert ctx.hour_roundup_steps[-1] == (None, 1000)
    data = json.loads(json.dumps(ctx.to_dict()))
    assert data["exclude_project_ids"] == ["p1", "p3"]
    back = pt.PricingContext.from_dict(data)
    assert back == ctx
    assert back.excluded_project_ids_sha256 == pt.excluded_projects_sha256(["p1", "p3"])


def test_quote_ref_round_trip_and_source():
    q = pt.QuoteRef(
        quote_id="q-7",
        category="LIGHTING FIXTURES",
        vendor="Vendor A",
        amount_usd="1234.50",
        tax_included=False,
        replaces_sections=["LIGHTING FIXTURES"],
        file_sha256="0" * 64,
    )
    assert q.amount_usd == Decimal("1234.50")
    assert q.material_source == "quote:q-7"
    assert pt.QuoteRef.from_dict(json.loads(json.dumps(q.to_dict()))) == q


def test_from_dict_accepts_floats_via_str():
    stat = pt.ValueStat.from_dict({"value": 0.1, "quality": "good", "method": "exact"})
    assert stat.value == Decimal("0.1")


# excluded_projects_sha256 (GC4) ----------------------------------------------------


def test_excluded_projects_sha256_is_order_independent():
    a = pt.excluded_projects_sha256(["p2", "p10", "p1"])
    b = pt.excluded_projects_sha256(frozenset({"p1", "p2", "p10"}))
    c = pt.excluded_projects_sha256(["p1", "p1", "p10", "p2"])
    assert a == b == c
    assert a == hashlib.sha256("p1\np10\np2".encode()).hexdigest()


def test_excluded_projects_sha256_changes_with_the_set():
    assert pt.excluded_projects_sha256(["p1"]) != pt.excluded_projects_sha256(["p1", "p2"])
    assert pt.excluded_projects_sha256([]) == hashlib.sha256(b"").hexdigest()


# Enum and value validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quality": "great"},
        {"method": "guess"},
        {"latest_seen": "2026-13"},
        {"latest_seen": "2026/08"},
        {"n_obs": -1},
        {"value": "NaN"},
        {"value": True},
    ],
)
def test_value_stat_rejects_bad_values(kwargs):
    base = {"value": Decimal("1"), "quality": "good", "method": "exact"}
    base.update(kwargs)
    with pytest.raises(ValueError):
        pt.ValueStat(**base)


def test_value_stat_missing_helper():
    stat = pt.ValueStat.missing()
    assert (stat.value, stat.quality, stat.method) == (None, "missing", "none")


def test_estimate_status_validated():
    with pytest.raises(ValueError):
        pt.PricedEstimate(status="done")
    for status in pt.ESTIMATE_STATUSES:
        assert pt.PricedEstimate(status=status).status == status


@pytest.mark.parametrize("source", ["library", "seed", "fallback", "none", "quote:abc"])
def test_material_source_accepts_valid(source):
    assert _line(material_source=source).material_source == source


@pytest.mark.parametrize("source", ["quote:", "vendor_quote", "LIBRARY", ""])
def test_material_source_rejects_invalid(source):
    with pytest.raises(ValueError):
        _line(material_source=source)


def test_wage_type_validated():
    with pytest.raises(ValueError):
        pt.PricingContext(wage_type="union", labor_rate_usd=100, labor_rate_source="x")
    ctx = pt.PricingContext(wage_type=None, labor_rate_usd=100, labor_rate_source="default")
    assert ctx.wage_type is None
    assert ctx.labor_rate_usd == Decimal("100")


def test_quantity_basis_validated():
    with pytest.raises(ValueError):
        _row(quantity_basis="guessed")
    assert _row(quantity_basis=None).quantity_basis is None


def test_line_id_must_match_row():
    with pytest.raises(ValueError):
        _line("B1", row=_row("B2"))


def test_duplicate_line_ids_rejected():
    with pytest.raises(ValueError):
        pt.PricedEstimate(status="priced", lines=[_line("B1"), _line("B1")])


@pytest.mark.parametrize(
    "steps",
    [
        (),
        ((200, 10), (100, 25), (None, 1000)),
        ((200, 10), (1000, 25)),
        ((None, 10), (None, 1000)),
        ((200, 0), (None, 1000)),
    ],
)
def test_hour_roundup_steps_validated(steps):
    with pytest.raises(ValueError):
        pt.PricingContext(
            wage_type=None, labor_rate_usd=100, labor_rate_source="x", hour_roundup_steps=steps
        )


def test_verify_draft_legacy_numbers_never_none():
    with pytest.raises(ValueError):
        pt.VerifyDraft(labor_amount=None)
    draft = pt.VerifyDraft()
    assert draft.numbers()["gear_amount"] is None
    assert draft.numbers()["labor_amount"] == Decimal(0)


def test_issue_from_plain_code_string():
    assert pt.PricingIssue.from_dict("wage_type_unknown").code == "wage_type_unknown"


# Drift guard against the Verify step ----------------------------------------------------


def test_verify_keys_match_the_pricing_router():
    from app.routers import pricing

    assert pt.VERIFY_NUMBERS == tuple(pricing.VERIFY_NUMBERS)
    assert pt.VERIFY_SECTION_NUMBERS == tuple(pricing.VERIFY_SECTION_NUMBERS)
    draft_keys = list(pt.VerifyDraft().to_dict())
    assert draft_keys[:10] == list(pricing.VERIFY_NUMBERS)
    assert draft_keys[10:] == [
        "bid_total",
        "labor_hours_basis",
        "labor_hours_reviewer",
        "labor_rate",
        "labor_rate_source",
    ]
