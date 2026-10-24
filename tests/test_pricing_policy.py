"""app/services/pricing_policy.py: rates and overrides (Q5), editable
markups (Q6), nice_ceil, grade seeds only with a cited fact (Q11, E13),
calibration. Synthetic numbers; pure and offline."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import pricing_policy as pp
from app.services.pricing_types import SpecFact


def test_rates_by_wage_type_and_default_when_unknown():
    p = pp.DEFAULT_POLICY
    assert p.labor_rate_for("prevailing_wage").rate == Decimal("125")
    assert p.labor_rate_for("non_prevailing_wage").rate == Decimal("100")
    d = p.labor_rate_for(None)
    assert d.rate == Decimal("125") and d.source == "default"
    assert p.labor_rate_for("prevailing_wage").source == "policy:prevailing_wage"


def test_gc_override_applies_when_every_gc_agrees_else_multi_gc_rate_warning():
    p = pp.PricingPolicy(rate_overrides={"gc-a": "120", "gc-b": "120"})
    one = p.labor_rate_for("prevailing_wage", ["gc-a"])
    assert one.rate == Decimal("120") and one.source == "gc_override:gc-a" and one.warnings == ()
    both = p.labor_rate_for("prevailing_wage", ["gc-a", "gc-b"])
    assert both.rate == Decimal("120")
    mixed = p.labor_rate_for("prevailing_wage", ["gc-a", "gc-c"])  # gc-c has no override: 125 vs 120
    assert mixed.rate == Decimal("125") and mixed.warnings == ("multi_gc_rate",)
    extra = p.labor_rate_for("prevailing_wage", ["gc-c"], {"gc-c": "110"})
    assert extra.rate == Decimal("110") and extra.source == "gc_override:gc-c"


@pytest.mark.parametrize(
    "hours, expected",
    [(588, 600), (637.1, 650), (1614, 1650), (4495, 4500), (6931, 7000), (25627, 26000),
     (200, 200), (201, 225), (1000, 1000), (0, 0), (3, 10)],
)
def test_nice_ceil_steps(hours, expected):
    assert pp.nice_ceil(hours) == Decimal(expected)


def test_reviewer_hours_uses_the_uplift():
    p = pp.PricingPolicy(hour_uplift="1.10")
    assert p.reviewer_hours(Decimal("588")) == Decimal("650")  # 646.8 -> 650
    assert p.reviewer_hours(Decimal("588"), uplift=Decimal("1")) == Decimal("600")


def test_markup_fields_are_editable_and_round_trip_through_the_document():
    p = pp.DEFAULT_POLICY
    assert p.material_markup_pct["materials"] == Decimal("10")
    assert p.labor_markup_pct == Decimal("0")
    doc = p.to_dict()
    doc["material_markup_pct"]["gear"] = "15"
    doc["labor_markup_pct"] = "5"
    doc["rate_overrides"] = {"gc-x": "120"}
    edited = pp.PricingPolicy.from_dict(doc)
    assert edited.material_markup_pct["gear"] == Decimal("15")
    assert edited.material_markup_pct["materials"] == Decimal("10")
    assert edited.labor_markup_pct == Decimal("5")
    assert edited.rate_overrides == {"gc-x": Decimal("120")}
    assert pp.PricingPolicy.from_dict(edited.to_dict()) == edited
    assert pp.PricingPolicy.from_dict(None) == pp.DEFAULT_POLICY
    assert p.replace(hour_uplift="1.2").hour_uplift == Decimal("1.2")


def test_policy_validation_rejects_bad_values():
    with pytest.raises(ValueError):
        pp.PricingPolicy(labor_rates={"prevailing_wage": 1})
    with pytest.raises(ValueError):
        pp.PricingPolicy(quote_replaces={"Lighting": "everything"})
    with pytest.raises(ValueError):
        pp.PricingPolicy(category_sections={"Lighting": "misc"})
    with pytest.raises(ValueError):
        pp.PricingPolicy(hour_roundup_steps=[[200, 10], [100, 25], [None, 50]])
    with pytest.raises(ValueError):
        pp.PricingPolicy(hour_uplift="0")


def test_grade_seed_only_with_a_cited_fact():
    seed = pp.GradeSeed(item_keys=("widget|mod=X", "widget|grade=hospital|mod=X"), fact_key="device_grade",
                        fact_value="hospital", material_usd="12.34")
    p = pp.PricingPolicy(grade_seeds=(seed,))
    cited = SpecFact(key="device_grade", value="hospital", source_ref="F7")
    uncited = SpecFact(key="device_grade", value="hospital", source_ref=None)
    assert pp.grade_seed_for(p, ["widget|mod=X"], [cited]) == (seed, cited)
    assert pp.grade_seed_for(p, ["widget|grade=hospital|mod=X"], [cited]) == (seed, cited)
    assert pp.grade_seed_for(p, ["widget|mod=X"], [uncited]) is None
    assert pp.grade_seed_for(p, ["widget|mod=X"], []) is None
    assert pp.grade_seed_for(p, ["other|mod=X"], [cited]) is None
    assert pp.grade_seed_for(p, ["widget|mod=X"], [SpecFact("device_grade", "spec", "F1")]) is None
    assert pp.grade_seed_for(p, ["widget|mod=X"], [SpecFact("grade", "Hospital Grade", "F1")]) == (seed, cited.__class__("grade", "Hospital Grade", "F1"))
    assert pp.GradeSeed.from_dict(seed.to_dict()) == seed


def test_default_seeds_cover_base_and_hospital_grade_keys_and_carry_no_amount():
    keys = {k for s in pp.DEFAULT_GRADE_SEEDS for k in s.item_keys}
    assert any("grade=hospital" in k for k in keys) and any("grade=" not in k for k in keys)
    assert all(s.fact_key == "device_grade" and s.fact_value == "hospital" for s in pp.DEFAULT_GRADE_SEEDS)
    # spec 0.2: no seed unit cost in the repo; the code default is inert until an amount is supplied
    assert all(s.material_usd is None and not s.active for s in pp.DEFAULT_GRADE_SEEDS)
    cited = SpecFact(key="device_grade", value="hospital", source_ref="E2.01 note 10")
    base_key = next(k for k in keys if "GFCI" not in k and "grade=" not in k)
    assert pp.grade_seed_for(pp.DEFAULT_POLICY, [base_key], [cited]) is None
    # an amount (settings row or the eval script) activates the seed; round trip keeps None
    seeded = tuple(s.with_amount("11.11") for s in pp.DEFAULT_GRADE_SEEDS)
    p = pp.PricingPolicy(grade_seeds=seeded)
    hit = pp.grade_seed_for(p, [base_key], [cited])
    assert hit is not None and hit[0].material_usd == Decimal("11.11") and hit[0].active
    inert = pp.GradeSeed.from_dict(pp.DEFAULT_GRADE_SEEDS[0].to_dict())
    assert inert == pp.DEFAULT_GRADE_SEEDS[0] and inert.to_dict()["material_usd"] is None
    assert pp.PricingPolicy.from_dict(pp.DEFAULT_POLICY.to_dict()).grade_seeds == pp.DEFAULT_GRADE_SEEDS


def test_calibrate_uplift_clean_band_and_loo():
    pairs = [(100, 110), (200, 230), (300, 310), (400, 460), (500, 1200), (150, 160), (90, 20)]
    r = pp.calibrate_uplift(pairs)
    assert r.n_pairs == 7 and r.n_clean == 5  # 2.4x and 0.22x fall outside 0.95..1.6
    assert r.median == Decimal("1.1") and r.p25 <= r.median <= r.p75
    assert r.chosen in ("median", "p25") and r.chosen_uplift in (r.median, r.p25)
    assert 0 <= r.loo_median_abs_error_median_u <= 1
    few = pp.calibrate_uplift([(100, 110)])
    assert few.chosen == "none" and few.chosen_uplift == pp.DEFAULT_HOUR_UPLIFT
    assert pp.CalibrationResult.to_dict(r)["n_clean"] == 5


def test_inert_grade_seed_is_reported_only_without_an_amount_and_with_a_cited_fact():
    base_key = "receptacle:duplex|mod=DUPLEX"
    cited = SpecFact("device_grade", "hospital", "E2.01 p.9 SN10")
    hit = pp.inert_grade_seed_for(pp.DEFAULT_POLICY, [base_key], [cited])
    assert hit is not None and hit[1] == cited and not hit[0].active
    assert pp.inert_grade_seed_for(pp.DEFAULT_POLICY, [base_key], [SpecFact("device_grade", "hospital", "")]) is None
    assert pp.inert_grade_seed_for(pp.DEFAULT_POLICY, ["control|desc=occupancy|mod=DUPLEX"], [cited]) is None
    seeded = pp.PricingPolicy(grade_seeds=tuple(s.with_amount("9.5") for s in pp.DEFAULT_GRADE_SEEDS))
    assert pp.inert_grade_seed_for(seeded, [base_key], [cited]) is None
    assert pp.grade_seed_for(seeded, [base_key], [cited])[1] == cited


def test_coverage_class_from_fallback_share_and_unpriced_line_share():
    p = pp.DEFAULT_POLICY
    assert p.coverage_class(0, 0) == pp.COVERAGE_COVERED
    assert p.coverage_class("0.10", "0.10") == pp.COVERAGE_COVERED  # at the limits: still covered
    assert p.coverage_class("0.101", 0) == pp.COVERAGE_UNDER_COVERED  # fallback hours over the limit
    assert p.coverage_class(0, "0.11") == pp.COVERAGE_UNDER_COVERED  # unpriced lines over the limit
    assert p.coverage_class(None, None) == pp.COVERAGE_COVERED
    tight = pp.PricingPolicy(fallback_share_max="0.02", unpriced_line_share_max="0")
    assert tight.coverage_class("0.03", 0) == pp.COVERAGE_UNDER_COVERED
    assert tight.coverage_class(0, "0.01") == pp.COVERAGE_UNDER_COVERED
    with pytest.raises(ValueError):
        pp.PricingPolicy(unpriced_line_share_max="1.5")
    with pytest.raises(ValueError):
        pp.PricingPolicy(hour_uplift_under_covered="0")


def test_reviewer_hours_pick_the_regime_uplift_and_the_document_round_trips_it():
    p = pp.PricingPolicy(hour_uplift="1.10", hour_uplift_under_covered="1.30",
                         hour_uplift_range=("1.0", "1.2"), hour_uplift_under_covered_range=("1.2", "1.5"))
    assert p.uplift_for(pp.COVERAGE_COVERED) == Decimal("1.10")
    assert p.uplift_for(pp.COVERAGE_UNDER_COVERED) == Decimal("1.30")
    assert p.uplift_range_for(pp.COVERAGE_UNDER_COVERED) == (Decimal("1.2"), Decimal("1.5"))
    assert p.reviewer_hours(Decimal("588")) == Decimal("650")  # 646.8 -> 650 (covered is the default)
    assert p.reviewer_hours(Decimal("588"), coverage=pp.COVERAGE_UNDER_COVERED) == Decimal("775")  # 764.4 -> 775
    assert p.reviewer_hours(Decimal("588"), uplift=Decimal("1"), coverage=pp.COVERAGE_UNDER_COVERED) == Decimal("600")
    with pytest.raises(ValueError):
        p.uplift_for("sometimes")
    doc = p.to_dict()
    assert doc["hour_uplift_under_covered"] == "1.30" and doc["hour_uplift_under_covered_range"] == ["1.2", "1.5"]
    assert doc["unpriced_line_share_max"] == str(pp.DEFAULT_UNPRICED_LINE_SHARE_MAX)
    assert pp.PricingPolicy.from_dict(doc) == p
    # a stored document from before the regime split keeps working: the new fields take the defaults
    legacy = {k: v for k, v in doc.items() if not k.startswith("hour_uplift_under") and k != "unpriced_line_share_max"}
    loaded = pp.PricingPolicy.from_dict(legacy)
    assert loaded.hour_uplift == Decimal("1.10")
    assert loaded.hour_uplift_under_covered == pp.DEFAULT_HOUR_UPLIFT_UNDER_COVERED
    assert pp.DEFAULT_HOUR_UPLIFT < pp.DEFAULT_HOUR_UPLIFT_UNDER_COVERED


def test_calibrate_uplift_by_coverage_per_regime_with_pooled_fallback():
    covered = [(100, 105, "covered"), (200, 220, "covered"), (300, 300, "covered"), (400, 460, "covered")]
    under = [(100, 130, "under_covered"), (200, 280, "under_covered"), (300, 390, "under_covered"), (50, 500, "under_covered")]
    r = pp.calibrate_uplift_by_coverage(covered + under)
    assert r.pooled.n_pairs == 8 and r.covered.n_pairs == 4 and r.under_covered.n_pairs == 4
    assert r.covered_source == "regime" and r.under_covered_source == "regime"
    assert r.chosen_covered == r.covered.chosen_uplift and r.chosen_under_covered == r.under_covered.chosen_uplift
    assert r.chosen_covered < r.chosen_under_covered
    assert r.uplift_for("covered") == r.chosen_covered and r.uplift_for("under_covered") == r.chosen_under_covered
    # a regime with fewer than three clean pairs takes the pooled value
    thin = pp.calibrate_uplift_by_coverage(covered + under[:2])
    assert thin.under_covered.chosen == "none" and thin.under_covered_source == "pooled"
    assert thin.chosen_under_covered == thin.pooled.chosen_uplift
    # nothing clean anywhere: the regime fallbacks
    empty = pp.calibrate_uplift_by_coverage([(100, 1000, "covered")], fallback_covered=Decimal("1.1"),
                                            fallback_under_covered=Decimal("1.3"))
    assert empty.covered_source == "fallback" and empty.chosen_covered == Decimal("1.1")
    assert empty.chosen_under_covered == Decimal("1.3")
    with pytest.raises(ValueError):
        pp.calibrate_uplift_by_coverage([(100, 110, "partial")])
    d = r.to_dict()
    assert set(d) >= {"pooled", "covered", "under_covered", "chosen_covered", "chosen_under_covered"}
