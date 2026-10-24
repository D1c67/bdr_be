"""app/services/estimating_contracts.py: the S3 day-1 contracts of the AI
Estimating build (artifact validators, gate ids, check registry, findings,
facts). Synthetic numbers only."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app.services import estimating_contracts as ec
from app.services.estimating_contracts import ContractError

# Fixtures ----------------------------------------------------------------------


def _verify() -> dict:
    return {
        "labor_amount": 1000.0,
        "materials_amount": 500.25,
        "gear_amount": None,
        "underground_amount": None,
        "low_voltage_amount": 0.0,
        "labor_markup_amount": None,
        "materials_markup_amount": 50.03,
        "gear_markup_amount": None,
        "underground_markup_amount": None,
        "low_voltage_markup_amount": None,
    }


def _line(line_id: str = "B1", **over) -> dict:
    line = {
        "line_id": line_id,
        "section": "NEW WORK",
        "group": "WIRING",
        "subgroup": "CONDUCTORS (AWG)",
        "description": "#12 THHN CU",
        "kind": "conductor",
        "unit": "FT",
        "qty_base": 100.0,
        "wastage": 0.10,
        "qty": 110.0,
        "quantity_basis": "measured",
        "unit_hours": 0.01,
        "unit_material_usd": 0.5,
        "hours_source": "library",
        "material_source": "library",
        "n": 4,
        "p25": 0.009,
        "p75": 0.011,
        "quote_ref": None,
        "priced": True,
        "excluded": False,
        "exclusion_reason": None,
    }
    line.update(over)
    return line


def _priced(**over) -> dict:
    doc = {
        "schema": ec.PRICED_ESTIMATE_SCHEMA,
        "run_id": "run-1",
        "refused": False,
        "refusal_reasons": [],
        "wage_type": "prevailing_wage",
        "labor_rate_usd": 100.0,
        "labor_rate_basis": "synthetic",
        "lines": [_line()],
        "allowances": [{"name": "SYNTHETIC ALLOWANCE", "amount_usd": 0, "included": False}],
        "quotes": [],
        "sections": {},
        "totals": {
            "hours_raw": 9.5,
            "hours": 10,
            "hour_roundup": 1.05,
            "labor_usd": 1000.0,
            "material_pretax_usd": 461.6,
            "tax_rate_pct": 8.375,
            "material_usd": 500.25,
            "verify": _verify(),
            "bid_total_usd": 1550.28,
        },
        "exclusions": ["Synthetic exclusion."],
        "clarifications": [],
        "metadata": {
            "excluded_project_ids_sha256": ec.excluded_project_ids_sha256(["p2", "p1"]),
            "library_fingerprint": "lib-test",
        },
    }
    doc.update(over)
    return doc


def _gate_report(**over) -> dict:
    doc = {
        "schema": ec.GATE_REPORT_SCHEMA,
        "blocking": False,
        "checks": [
            {"code": "branch_ratio", "severity": "warn", "detail": "ratio low", "rows": ["B1"]}
        ],
    }
    doc.update(over)
    return doc


def _fn(*_args, **_kwargs) -> ec.CheckResult:
    return ec.CheckResult(id="E1", status="pass")


# Priced estimate ----------------------------------------------------------------


def test_priced_estimate_schema():
    doc = _priced()
    assert ec.priced_estimate_errors(doc) == []
    assert ec.validate_priced_estimate(doc) is doc
    assert ec.validate_artifact(doc) is doc


def test_priced_estimate_bid_equals_verify_sum():
    doc = _priced()
    doc["totals"]["bid_total_usd"] = 1550.29
    errors = ec.priced_estimate_errors(doc)
    assert any("sum(verify)" in e for e in errors)
    with pytest.raises(ContractError):
        ec.validate_priced_estimate(doc)
    # None counts as 0 and the comparison is to the cent.
    assert ec.verify_sum(_verify()) == ec.verify_sum({k: v for k, v in _verify().items()})
    assert ec.bid_total_matches_verify(1550.28, _verify())
    assert ec.bid_total_matches_verify(1550.2849, _verify())
    assert not ec.bid_total_matches_verify(None, _verify())


def test_verify_keys_match_router_tuple():
    from app.routers.pricing import VERIFY_NUMBERS

    assert ec.VERIFY_KEYS == tuple(VERIFY_NUMBERS)


def test_verify_must_carry_exactly_the_ten_keys():
    doc = _priced()
    del doc["totals"]["verify"]["gear_amount"]
    assert any("ten Verify numbers" in e for e in ec.priced_estimate_errors(doc))


@pytest.mark.parametrize(
    "path,value",
    [
        (("wage_type",), "union_scale"),
        (("lines", 0, "hours_source"), "seed"),
        (("lines", 0, "material_source"), "quote"),
        (("lines", 0, "quantity_basis"), "guessed"),
    ],
)
def test_priced_estimate_rejects_wrong_enum(path, value):
    doc = _priced()
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert ec.priced_estimate_errors(doc)


def test_refused_follows_blocked_status():
    doc = _priced()
    assert ec.priced_estimate_errors(doc, status="priced") == []
    assert any("refused must be True" in e for e in ec.priced_estimate_errors(doc, status="blocked"))
    refused = _priced(refused=True, refusal_reasons=["census_floor"], status="blocked")
    refused["totals"]["verify"] = None
    refused["totals"]["bid_total_usd"] = None
    assert ec.priced_estimate_errors(refused) == []
    assert ec.refused_for_status("blocked") is True
    assert ec.refused_for_status("provisional") is False
    # A refusal needs a reason, and null totals are only for refusals.
    no_reason = copy.deepcopy(refused)
    no_reason["refusal_reasons"] = []
    assert ec.priced_estimate_errors(no_reason)
    not_refused = _priced()
    not_refused["totals"]["bid_total_usd"] = None
    assert ec.priced_estimate_errors(not_refused)


def test_priced_line_rules():
    doc = _priced(lines=[_line(unit_hours=None)])
    assert any("priced lines need" in e for e in ec.priced_estimate_errors(doc))
    doc = _priced(lines=[_line(material_source="vendor_quote")])
    assert any("quote_ref" in e for e in ec.priced_estimate_errors(doc))
    doc = _priced(lines=[_line(unit="EA")])
    assert any("linear class" in e for e in ec.priced_estimate_errors(doc))
    doc = _priced(lines=[_line("B1"), _line("B1")])
    assert any("duplicate" in e for e in ec.priced_estimate_errors(doc))
    doc = _priced(lines=[_line("B012")])
    assert any("line_id" in e for e in ec.priced_estimate_errors(doc))


def test_source_mappings():
    for method in ("exact", "compatible", "context", "fallback"):
        assert ec.hours_source_for(method) == "library"
    assert ec.hours_source_for("seed") == "policy"
    assert ec.hours_source_for("none") is None
    assert ec.hours_source_for("none", manual=True) == "manual"
    with pytest.raises(ContractError):
        ec.hours_source_for("quote")
    assert ec.material_source_for("library") == ("library", None)
    assert ec.material_source_for("fallback") == ("library", None)
    assert ec.material_source_for("seed") == ("policy", None)
    assert ec.material_source_for("quote:q-7") == ("vendor_quote", "q-7")
    assert ec.material_source_for("none") == ("none", None)
    assert ec.material_source_for("library", allowance=True) == ("allowance", None)
    assert ec.material_source_for("library", manual=True) == ("manual", None)
    with pytest.raises(ContractError):
        ec.material_source_for("quote:")


def test_excluded_project_ids_sha256_is_order_free():
    a = ec.excluded_project_ids_sha256(["b", "a", "a"])
    assert a == ec.excluded_project_ids_sha256(["a", "b"])
    assert len(a) == 64


# Gate report --------------------------------------------------------------------


def test_gate_report_schema():
    doc = _gate_report()
    assert ec.gate_report_errors(doc) == []
    assert ec.validate_artifact(doc) is doc


def test_gate_report_rejections():
    assert ec.gate_report_errors(_gate_report(blocking=True))  # no block check
    bad_code = _gate_report()
    bad_code["checks"][0]["code"] = "no_branch_wiring"
    assert ec.gate_report_errors(bad_code)
    warn_only = _gate_report()
    warn_only["checks"][0]["severity"] = "block"
    assert any("warn-only" in e for e in ec.gate_report_errors(warn_only))
    bad_rows = _gate_report()
    bad_rows["checks"][0]["rows"] = ["C3"]
    assert ec.gate_report_errors(bad_rows)


def test_wage_refusal_validates_in_a_gate_report():
    """A GC3 wage refusal: the pricing-stage gate wage_type_unknown blocks."""
    f = ec.GateFinding("run-1", 1, "pricing", "wage_type_unknown", "block",
                       message="projects.wage_type is null")
    report = ec.gate_report_from_findings([f])
    assert report["blocking"] is True
    assert report["checks"][0]["code"] == "wage_type_unknown"
    assert ec.gate_report_errors(report) == []
    refused = _priced(refused=True, refusal_reasons=["wage_type_unknown"], wage_type=None)
    assert ec.artifact_pair_errors(refused, report) == []


def test_blocking_gate_report_requires_refusal():
    report = _gate_report(
        blocking=True,
        checks=[{"code": "linear_in_ea", "severity": "block", "detail": "x", "rows": ["B1"]}],
    )
    assert ec.gate_report_errors(report) == []
    assert ec.artifact_pair_errors(_priced(), report)
    refused = _priced(refused=True, refusal_reasons=["linear_in_ea"])
    assert ec.artifact_pair_errors(refused, report) == []


def test_gate_ids():
    assert "branch_ratio" in ec.GATE_IDS
    assert "fact_conflict" in ec.GATE_IDS
    assert "wage_type_unknown" in ec.GATE_IDS
    assert ec.PRICING_STAGE_GATES <= set(ec.GATE_IDS)
    assert len(set(ec.GATE_IDS)) == len(ec.GATE_IDS) == 18
    # needs_review spells it wage_unknown (5.1); the gate code is wage_type_unknown (3.6).
    assert ec.WAGE_UNKNOWN_REVIEW == "wage_unknown"
    assert ec.WAGE_UNKNOWN_REVIEW not in ec.GATE_IDS
    assert ec.canonical_gate_id("branch_ft_empty") == "new_devices_no_branch"
    with pytest.raises(ContractError):
        ec.validate_gate_severity("block", "branch_ratio")


# Findings -----------------------------------------------------------------------


def test_fingerprint_stable():
    a = ec.finding_fingerprint("census_floor", {"sheet_key": "D1P8", "row_ids": ["C2", "C1"]})
    b = ec.finding_fingerprint(
        "census_floor", {"row_ids": ["C1", "C2"], "family": None, "sheet_key": "D1P8"}
    )
    assert a == b
    assert a == ec.finding_fingerprint("census_floor", {"sheet_key": "D1P8", "row_ids": ["C1", "C2"]})
    assert a != ec.finding_fingerprint("partial_basis", {"sheet_key": "D1P8", "row_ids": ["C1", "C2"]})
    f = ec.GateFinding("run-1", 2, "final", "linear_in_ea", "block", target={"line_id": "B4"})
    assert f.fingerprint == ec.finding_fingerprint("linear_in_ea", {"line_id": "B4"})
    assert ec.GateFinding.from_row(f.to_row()) == f


def test_gate_finding_validation():
    with pytest.raises(ContractError):
        ec.GateFinding("r", 1, "middle", "linear_in_ea", "block")
    with pytest.raises(ContractError):
        ec.GateFinding("r", 1, "final", "not_a_gate", "block")
    with pytest.raises(ContractError):
        ec.GateFinding("r", 1, "final", "branch_ratio", "block")
    with pytest.raises(ContractError):
        ec.GateFinding("r", 1, "final", "linear_in_ea", "block", route="later")
    with pytest.raises(ContractError):
        ec.GateFinding("r", 1, "final", "linear_in_ea", "block", status="closed")
    aliased = ec.GateFinding("r", 1, "pre", "branch_ft_empty", "block")
    assert aliased.gate_id == "new_devices_no_branch"


def test_record_findings_in_memory_and_report():
    store = ec.InMemoryFindingsStore()
    assert isinstance(store, ec.RecordFindings)
    f = ec.GateFinding(None, 1, "final", "uom_conflict_quote", "block", target={"line_id": "B9"})
    stored = store.record_findings(None, {"id": "run-9"}, [f], "pricing")
    assert stored[0].run_id == "run-9" and stored[0].stage == "pricing"
    stored[0].status = "overridden"
    again = store(None, "run-9", [f], "pricing")
    assert len(store.rows) == 1 and again[0].status == "overridden"
    assert store.for_run("run-9", stage="pricing") == again
    report = ec.gate_report_from_findings(store.for_run("run-9"))
    assert report == {"schema": ec.GATE_REPORT_SCHEMA, "blocking": False, "checks": []}
    w = ec.GateFinding("run-9", 1, "final", "linear_in_ea", "block", target={"line_id": "B2"})
    report = ec.gate_report_from_findings([w])
    assert report["blocking"] is True and report["checks"][0]["rows"] == ["B2"]
    assert ec.gate_report_errors(report) == []
    assert ec.null_record_findings(None, "run-9", [w], "final") == [w]
    with pytest.raises(ContractError):
        ec.null_record_findings(None, "run-9", [w], "later")


# Facts, excluded lines, pricing_allowed ------------------------------------------


def test_fact_and_protocols():
    cite = ec.FactCite(document_id="doc-1", page=2, quote="fire alarm by others")
    fact = ec.Fact("F1", "fire_alarm", "Fire alarm by others", "others", True, cite,
                   kind="provided_by_others")
    assert fact.excludes
    assert ec.Fact.from_dict(fact.to_dict()) == fact
    assert not ec.Fact("F2", "fire_alarm", "s", None, False, cite).excludes
    with pytest.raises(ContractError):
        ec.Fact("B1", "fire_alarm", "s", None, True, cite)
    assert isinstance(ec.no_facts, ec.FactsForRun)
    assert ec.no_facts(None, "run-1") == []
    assert isinstance(ec.pricing_allowed_unavailable, ec.PricingAllowed)
    assert ec.pricing_allowed_unavailable({"id": "r"}) == (False, [ec.PRICING_GATES_UNAVAILABLE])


def test_excluded_line():
    line = ec.ExcludedLine("B3", "SMOKE DETECTOR", 4, "EA", "fire_alarm", "by others",
                           fact_id="F1", party="Synthetic Vendor")
    assert line.to_dict()["quantity"] == 4
    with pytest.raises(ContractError):
        ec.ExcludedLine("B3", "X", 4, "EA", "fire_alarm", "by others")  # fact without fact_id
    with pytest.raises(ContractError):
        ec.ExcludedLine("B3", "X", -1, "EA", None, "policy", source="policy")
    note = ec.excluded_note("fire_alarm", "Synthetic Vendor", "D1 P2", "by vendor")
    assert note == 'EXCLUDED: fire_alarm by Synthetic Vendor per D1 P2: "by vendor"'


# Enums and row ids ----------------------------------------------------------------


def test_enum_validators():
    assert ec.validate_wage_type(None) is None
    assert ec.validate_wage_type("non_prevailing_wage") == "non_prevailing_wage"
    with pytest.raises(ContractError):
        ec.validate_wage_type(None, allow_none=False)
    with pytest.raises(ContractError):
        ec.validate_price_quality("great")
    assert ec.validate_price_quality("quote") == "quote"
    with pytest.raises(ContractError):
        ec.validate_quantity_basis("guess")
    assert ec.line_basis_for_measure("measured_route") == ("measured", None)
    assert ec.line_basis_for_measure("assumed_density") == ("assumed_route", "(ASSUMED)")
    assert ec.validate_priced_line_material_source("quote:abc") == "quote:abc"
    with pytest.raises(ContractError):
        ec.validate_priced_line_material_source("quote")
    with pytest.raises(ContractError):
        ec.validate_check_status("ok")


def test_row_ids_and_linear_classes():
    assert ec.make_row_id("B", 12) == "B12"
    assert ec.parse_row_id("F3") == ("F", 3)
    for bad in ("B0", "B012", "Z1", "b1", 12, "B"):
        assert not ec.is_row_id(bad)
    assert ec.is_row_id("K7", "K") and not ec.is_row_id("K7", "B")
    assert set(ec.ROW_ID_PREFIXES) == set("CRSPODQMFBK")
    assert "lighting_track" in ec.LINEAR_CLASSES
    from app.services import electrical_items as ei

    # One definition (spec 5.1): electrical_items is the home.
    assert ec.LINEAR_CLASSES is ei.LINEAR_CLASSES
    assert ec.LINEAR_LUMINAIRE_WORDS is ei.LINEAR_LUMINAIRE_WORDS
    ec.validate_item_class_unit("conduit", "FT")
    with pytest.raises(ContractError):
        ec.validate_item_class_unit("conduit", "EA")


# Check registry -------------------------------------------------------------------


def test_check_specs_cover_every_fixed_id():
    ids = [f"E{i}" for i in range(1, 26)] + [f"GC{i}" for i in range(1, 5)]
    assert list(ec.CHECK_SPECS) == ids
    assert ec.CHECK_SPECS["E3"].replayable is False
    assert ec.CHECK_SPECS["E11"].replayable is True
    assert ec.CHECK_SPECS["GC2"].gates_7154_smoke is False
    assert ec.CHECK_SPECS["E1"].threshold["priced_share_min"] == 0.98


def test_check_specs_carry_no_real_dollar_figures():
    """E9 and E13 dollar figures are reference keys resolved from
    EVAL_ROOT/references/7154/expected.json, never inline numbers."""
    e9 = ec.CHECK_SPECS["E9"].threshold["pretax_targets_usd"]
    assert all(set(v) == {"ref"} for v in e9.values())
    e13 = ec.CHECK_SPECS["E13"].threshold
    assert set(e13["duplex_unit_material_min_usd"]) == {"ref"}
    assert set(e13["gfci_unit_material_min_usd"]) == {"ref"}
    refs = [k for spec in ec.CHECK_SPECS.values() for k in ec.threshold_reference_keys(spec)]
    assert sorted(refs) == sorted(ec.REFERENCE_KEYS)
    assert ec.CHECK_SPECS["E4"].threshold["labor_rate_usd"] == 125.00  # Q5 policy rate


def _synthetic_references() -> dict:
    # Synthetic numbers only; one nested key exercises the dotted lookup.
    return {
        "e9.lowest_eligible_quote_pretax_usd": 1000.0,
        "e9.alternate_quote_pretax_usd": 1010.0,
        "e13": {"duplex_unit_material_min_usd": 7.0, "gfci_unit_material_min_usd": 14.0},
    }


def test_resolve_reference_thresholds():
    refs = _synthetic_references()
    e9 = ec.resolve_reference_thresholds(ec.CHECK_SPECS["E9"], refs)
    assert e9["pretax_targets_usd"] == {"lowest_eligible": 1000.0, "alternate_selection": 1010.0}
    assert e9["rel_tol"] == 0.02
    e13 = ec.resolve_reference_thresholds(ec.CHECK_SPECS["E13"].threshold, refs)
    assert e13["duplex_unit_material_min_usd"] == 7.0
    assert e13["gfci_unit_material_min_usd"] == 14.0
    # The spec itself is untouched; a threshold without refs comes back equal.
    assert set(ec.CHECK_SPECS["E13"].threshold["duplex_unit_material_min_usd"]) == {"ref"}
    assert ec.resolve_reference_thresholds(ec.CHECK_SPECS["E1"], {}) == dict(
        ec.CHECK_SPECS["E1"].threshold
    )
    with pytest.raises(ContractError, match="missing"):
        ec.resolve_reference_thresholds(ec.CHECK_SPECS["E9"], {})
    bad = dict(refs, **{"e9.alternate_quote_pretax_usd": "lots"})
    with pytest.raises(ContractError, match="finite number"):
        ec.resolve_reference_thresholds(ec.CHECK_SPECS["E9"], bad)


def test_registry_resolves_references_at_registration():
    reg = ec.CheckRegistry()
    refs = _synthetic_references()
    check = reg.register("E13", _fn, gates_7154_smoke=True, replayable=True, references=refs)
    assert check.threshold["gfci_unit_material_min_usd"] == 14.0
    # The raw spec threshold and the resolved one are both accepted; anything else is not.
    reg.register("E13", _fn, gates_7154_smoke=True, replayable=True, references=refs,
                 threshold=dict(ec.CHECK_SPECS["E13"].threshold))
    changed = ec.resolve_reference_thresholds(ec.CHECK_SPECS["E13"], refs)
    changed["gfci_unit_material_min_usd"] = 1.0
    with pytest.raises(ContractError, match="fixed"):
        reg.register("E13", _fn, gates_7154_smoke=True, replayable=True, references=refs,
                     threshold=changed)


def test_material_source_enum_names():
    assert ec.ARTIFACT_MATERIAL_SOURCES is ec.MATERIAL_SOURCES
    assert "vendor_quote" in ec.ARTIFACT_MATERIAL_SOURCES
    assert ec.PRICED_LINE_MATERIAL_SOURCES == ("library", "seed", "fallback", "none")


def test_shared_vocabularies_are_single_definitions():
    from app.services import plans_reader_review_schemas as rs
    from app.services import plans_reader_tools as tools
    from app.services import pricing_types as pt

    assert pt.PRICED_LINE_MATERIAL_SOURCES is ec.PRICED_LINE_MATERIAL_SOURCES
    assert pt.MATERIAL_SOURCES is ec.PRICED_LINE_MATERIAL_SOURCES
    assert pt.QUANTITY_BASES is ec.QUANTITY_BASES
    assert pt.WAGE_TYPES is ec.WAGE_TYPES
    assert pt.VERIFY_NUMBERS is ec.VERIFY_KEYS
    assert pt.PRICE_QUALITIES is ec.PRICE_QUALITIES
    assert pt.VALUE_METHODS is ec.VALUE_STAT_METHODS
    assert pt.ESTIMATE_STATUSES is ec.PRICED_STATUSES
    assert pt.excluded_projects_sha256(["b", "a"]) == ec.excluded_project_ids_sha256(["a", "b"])
    assert tools.BASES is ec.QUANTITY_BASES
    assert rs.QUANTITY_BASES is ec.QUANTITY_BASES
    assert rs.COMBINE_MODES is ec.COMBINE_MODES
    assert rs.WAGE_TYPES is ec.WAGE_TYPES


def test_registry_refuses_threshold_change():
    reg = ec.CheckRegistry()
    check = reg.register("E1", _fn, gates_7154_smoke=True, replayable=True)
    assert check.threshold == ec.CHECK_SPECS["E1"].threshold
    changed = dict(ec.CHECK_SPECS["E1"].threshold, priced_share_min=0.9)
    with pytest.raises(ContractError, match="fixed"):
        reg.register("E1", _fn, gates_7154_smoke=True, replayable=True, threshold=changed)
    with pytest.raises(ContractError):
        reg.register("E3", _fn, gates_7154_smoke=True, replayable=True)  # E3 is not replayable
    with pytest.raises(ContractError):
        reg.register("GC2", _fn, gates_7154_smoke=True, replayable=True)
    # Same threshold re-registers (detector replaced).
    reg.register("E1", _fn, gates_7154_smoke=True, replayable=True,
                 threshold=dict(ec.CHECK_SPECS["E1"].threshold))
    # Extra ids need a threshold and keep it.
    with pytest.raises(ContractError):
        reg.register("G1", _fn, gates_7154_smoke=False, replayable=True)
    reg.register("G1", _fn, gates_7154_smoke=False, replayable=True, threshold={"max": 1})
    with pytest.raises(ContractError, match="different threshold"):
        reg.register("G1", _fn, gates_7154_smoke=False, replayable=True, threshold={"max": 2})
    assert "E2" in reg.missing_fixed() and "E1" not in reg.missing_fixed()


def test_check_run_turns_exceptions_into_error():
    def boom(*_a):
        raise RuntimeError("detector crashed")

    reg = ec.CheckRegistry()
    result = reg.register("G9", boom, gates_7154_smoke=False, replayable=True,
                          threshold={"x": 1}).run({})
    assert result.status == "error" and "detector crashed" in result.measured["exception"]
    ok = reg.register("E1", _fn, gates_7154_smoke=True, replayable=True).run()
    assert ok.status == "pass"
    assert reg.get("E1").id == "E1"
    with pytest.raises(ContractError):
        reg.get("E99")


def test_module_registry_is_checks_dict():
    assert ec.CHECKS is ec._REGISTRY.checks


# Hygiene ----------------------------------------------------------------------------


def test_module_imports_only_stdlib_and_electrical_items_and_has_no_dashes():
    dashes = (chr(0x2013), chr(0x2014))
    for path in (Path(ec.__file__), Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert not any(d in text for d in dashes), path
    for line in Path(ec.__file__).read_text(encoding="utf-8").splitlines():
        if line.startswith(("import ", "from ")):
            assert "openai" not in line and "supabase" not in line
            if "app." in line:
                # The only app import: the one home of the linear vocabularies.
                assert line.startswith("from app.services.electrical_items import ")
