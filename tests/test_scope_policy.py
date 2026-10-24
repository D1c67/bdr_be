"""WS5 scope policy document: validation, seed, hashing, matching and the
active-policy accessor (docs/AI_ESTIMATING_BUILD_SPEC.md 5.8)."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.services import scope_policy as sp
from app.services.plans_reader_schemas import ESTIMATE_ALLOWANCES
from app.services.scope_policy_seed import (
    SEED_POLICY,
    SEED_RULE_IDS,
    SEED_SHA,
    TEMPLATE_LINES,
    THERMOSTAT_CLARIFICATION,
)

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mine_scope_policy.py"


@pytest.fixture(autouse=True)
def _reset_active():
    sp.reset_active_policy()
    yield
    sp.reset_active_policy()


def _doc() -> dict:
    return copy.deepcopy(SEED_POLICY)


def _rule(doc: dict, rule_id: str) -> dict:
    return next(r for r in doc["rules"] if r["rule_id"] == rule_id)


def _errors(doc: dict) -> list[str]:
    return sp.validation_errors(doc)


# Seed ------------------------------------------------------------------------------------


def test_seed_validates():
    info = sp.validate(SEED_POLICY)
    assert info.version == 1
    assert info.sha256 == sp.policy_sha(SEED_POLICY)
    assert info.size_bytes < sp.MAX_DOC_BYTES


def test_seed_sha_stable():
    # A seed change is a deliberate policy change: bump SEED_SHA with it.
    assert sp.policy_sha(SEED_POLICY) == SEED_SHA


def test_seed_rule_ids_stable_and_unique():
    ids = tuple(r["rule_id"] for r in SEED_POLICY["rules"]) + tuple(
        i["rule_id"] for i in SEED_POLICY["inclusions"]
    )
    assert ids == SEED_RULE_IDS
    assert len(set(ids)) == len(ids)


def test_seed_rule_ids_carry_the_e5_topics():
    # wave 1 fix: ANCILLARY and FIRESTOPPING joined the seed (27 ids in all)
    assert len(SEED_RULE_IDS) == 27
    assert SEED_RULE_IDS.index("EX-FIRESTOPPING") == SEED_RULE_IDS.index("EX-FIRESTOP-LABELS") + 1
    assert SEED_RULE_IDS.index("EX-ANCILLARY") == SEED_RULE_IDS.index("EX-PHASING-PREMIUM") + 1
    assert SEED_SHA == "c4f755d31776804f7219317a76f1a6b836918b61f88f6806b08d75811c1929a6"


def test_seed_ascii_no_dashes():
    text = json.dumps(SEED_POLICY, ensure_ascii=False)
    assert "\u2013" not in text and "\u2014" not in text
    assert all(ord(c) < 128 for c in text)


def test_seed_hard_rules_carry_no_unless():
    for rule in SEED_POLICY["rules"]:
        if rule["kind"] == "hard":
            assert rule["unless"] == [], rule["rule_id"]


def test_seed_every_regex_is_word_bounded():
    patterns = []
    for rule in SEED_POLICY["rules"]:
        patterns += rule["match"]["patterns"] + rule["match"]["exempt_patterns"]
    for inc in SEED_POLICY["inclusions"]:
        patterns += [inc["pattern"]] + ([inc["also_pattern"]] if inc.get("also_pattern") else [])
    for std in SEED_POLICY["standards"].values():
        if isinstance(std, dict):
            for plist in (std.get("value_patterns") or {}).values():
                patterns += plist
            patterns += std.get("neutral_patterns") or []
    patterns += SEED_POLICY["allowances"]["document_amount_patterns"]
    assert patterns
    for pattern in patterns:
        assert sp.regex_problem(pattern) is None, pattern


def test_seed_template_proposal_lines_are_the_four_sent_sentences():
    lines = {r["proposal_line"] for r in SEED_POLICY["rules"] if r.get("template_covered")}
    assert set(TEMPLATE_LINES) <= lines
    for rid in ("EX-TEMP-POWER", "EX-TEMPLATE-SCOPE", "EX-CATCH-ALL", "EX-BOND"):
        rule = _rule(SEED_POLICY, rid)
        assert rule["proposal_line"] in TEMPLATE_LINES
        assert rule["evidence"][0]["support"] == rule["evidence"][0]["of"]


def test_seed_q12_cutting_patching_hard_with_clarification():
    rule = _rule(SEED_POLICY, "EX-CUT-PATCH-PAINT")
    assert rule["kind"] == "hard" and rule["decision"] == "exclude"
    assert rule["proposal_line"] == "Cutting, patching and painting by others."
    text = "THE ELECTRICAL CONTRACTOR SHALL BE RESPONSIBLE FOR ALL CUTTING, PATCHING AND PAINTING"
    assert sp.match_rule(rule, text)


def test_seed_q14_thermostats_and_control_cable_included():
    rule = _rule(SEED_POLICY, "IN-THERMOSTATS")
    assert rule["decision"] == "include" and rule["kind"] == "hard"
    assert rule["proposal_line"] == THERMOSTAT_CLARIFICATION
    assert THERMOSTAT_CLARIFICATION == (
        "Thermostat relocations and control cable included; ATC devices and programming by "
        "others."
    )
    by_id = {i["rule_id"]: i for i in SEED_POLICY["inclusions"]}
    reloc, cable = by_id["IN-THERMOSTAT-RELOC"], by_id["IN-CONTROL-CABLE"]
    assert reloc["line_template"]["description"] == "RELOCATION OF THERMOSTAT TO NEW LOCATION"
    assert reloc["line_template"]["unit"] == "EA"
    assert reloc["clarification"] == THERMOSTAT_CLARIFICATION
    assert cable["companion_of"] == "IN-THERMOSTAT-RELOC"
    assert cable["line_template"]["description"] == "#18/4C CONTROL CABLE"
    assert cable["line_template"]["unit"] == "FT"
    assert cable["line_template"]["wastage_applies"] is True


def test_seed_standards_raceway_and_demolition():
    std = SEED_POLICY["standards"]
    assert std["precedence"][0] == "addendum" and std["precedence"][-1] == "house"
    assert std["raceway"]["value"] == "EMT"
    assert std["raceway"]["fittings"] == "SET_SCREW_STEEL"
    assert '3/4" EMT' in std["raceway"]["text"]
    assert std["branch_conduit"]["value"] == '3/4"C EMT'
    demo = std["demo_disposition"]
    assert demo["value"] == "REMOVE_TO_SOURCE"
    assert {"winner": "keynote", "loser": "sheet_note"} in demo["level_beats"]
    remove = [sp.compile_pattern(p) for p in demo["value_patterns"]["REMOVE_TO_SOURCE"]]
    cap = [sp.compile_pattern(p) for p in demo["value_patterns"]["CAP_AND_ABANDON"]]
    keynote = (
        "REMOVAL OF LIGHT FIXTURE, CONDUIT, CONDUCTORS, ETC FROM LIGHT FIXTURE TO POINT OF SOURCE"
    )
    sheet_note = "MAINTAIN AND CAP OFF EXISTING CONDUIT AND CONDUCTORS FOR USE IN NEW CONSTRUCTION"
    assert any(p.search(keynote) for p in remove)
    assert any(p.search(sheet_note) for p in cap)
    assert not any(p.search(sheet_note) for p in remove)


def test_seed_raceway_code_reference_is_neutral():
    raceway = SEED_POLICY["standards"]["raceway"]
    note = "FOR CONDUCTORS WITHIN PVC FOLLOW NEC 352, AND EMT FOLLOW NEC 358"
    assert any(sp.compile_pattern(p).search(note) for p in raceway["neutral_patterns"])


def test_seed_allowances_nine_at_zero():
    allowances = SEED_POLICY["allowances"]
    assert allowances["standard"] == list(ESTIMATE_ALLOWANCES)
    assert len(allowances["standard"]) == 9
    assert allowances["standard_amount_usd"] == 0
    assert allowances["extra_requires_document_amount"] is True
    pats = [sp.compile_pattern(p) for p in allowances["document_amount_patterns"]]
    assert any(p.search("Include an allowance of $5,000 for signage") for p in pats)
    assert not any(p.search("Temporary HEPA power allowance") for p in pats)


def test_seed_bid_conditions_block():
    bc = SEED_POLICY["bid_conditions"]
    assert bc["default_wage_type"] == "prevailing_wage"
    assert bc["rate_table"] == {"prevailing_wage": "pw", "non_prevailing_wage": "open_shop"}


# Matching ---------------------------------------------------------------------------------


def test_permit_does_not_match_permitted():
    rule = _rule(SEED_POLICY, "EX-PERMITS")
    assert sp.match_rule(rule, "EMT IS PERMITTED IN DRY LOCATIONS") is None
    assert sp.match_rule(rule, "ELECTRICAL PERMIT FEES") is not None


def test_paint_skips_low_voltage_backboard_and_painted_finishes():
    rule = _rule(SEED_POLICY, "EX-CUT-PATCH-PAINT")
    backboard = "PAINTED PLYWOOD BACKBOARD"
    assert sp.match_rule(rule, backboard, subgroup="RACK DETAILS") is None
    assert sp.match_rule(rule, backboard, group="LOW VOLTAGE WORK") is None
    assert sp.match_rule(rule, "PAINTED STEEL POLE") is None
    assert sp.match_rule(rule, "ELECTRICAL CUTTING, PATCHING AND FINISH RESTORATION")


def test_patch_skips_patch_panels_and_cords():
    rule = _rule(SEED_POLICY, "EX-CUT-PATCH-PAINT")
    assert sp.match_rule(rule, "CAT6 PATCH PANEL, 48 PORT") is None
    assert sp.match_rule(rule, "PATCH CORDS 3 FT") is None
    assert sp.match_rule(rule, "PATCH AND REPAIR GYP BOARD") == "PATCH"


def test_standard_allowance_rows_never_match_a_rule():
    for name in ESTIMATE_ALLOWANCES:
        for rule in SEED_POLICY["rules"]:
            assert sp.match_rule(rule, name) is None, (name, rule["rule_id"])


def test_temp_power_matches_the_7154_extra_allowances():
    rule = _rule(SEED_POLICY, "EX-TEMP-POWER")
    assert sp.match_rule(rule, "Temporary distribution and switched lighting")
    assert sp.match_rule(rule, "Temporary HEPA power and shutoff")
    assert sp.match_rule(rule, "TEMPERATURE SENSOR") is None


def _seed_hits(text: str) -> list[str]:
    return [
        r["rule_id"]
        for r in SEED_POLICY["rules"]
        if r["decision"] != "include" and sp.match_rule(r, text) is not None
    ]


def test_every_7154_e5_extra_allowance_matches_a_seed_rule():
    import re

    from app.services.estimating_contracts import CHECK_SPECS
    from tests.plans_reader_fixtures import load_7154

    e5 = re.compile(CHECK_SPECS["E5"].threshold["house_exclusion_pattern"], re.I)
    extra = load_7154()["main"]["estimate"]["extra_allowances"]
    house = [a for a in extra if e5.search(a)]
    assert len(house) == 8 and len(extra) == 10
    missing = [a for a in house if not _seed_hits(a)]
    assert missing == []
    assert "EX-ANCILLARY" in _seed_hits("LIGHTING AND RECEPTACLE CONTROL ANCILLARY EQUIPMENT")


@pytest.mark.parametrize(
    "text,rule_id",
    [
        ("HEPA FILTRATION POWER", "EX-TEMP-POWER"),
        ("FIRESTOPPING OF PENETRATIONS", "EX-FIRESTOPPING"),
        ("PHASING", "EX-PHASING-PREMIUM"),
        ("ANCILLARY ELECTRICAL WORK", "EX-ANCILLARY"),
    ],
)
def test_e5_topic_probes_match(text, rule_id):
    assert rule_id in _seed_hits(text)


def test_firestopping_is_soft_and_spares_caulking_and_labels():
    rule = _rule(SEED_POLICY, "EX-FIRESTOPPING")
    assert rule["kind"] == "soft" and rule["unless"]
    assert sp.match_rule(rule, "CAULKING SEALANT") is None
    assert sp.match_rule(rule, "FIRESTOP CAULKING AT PENETRATIONS") is None
    assert sp.match_rule(rule, "RATED BOX PADS AND FIRESTOP SYSTEM LABELING") is None
    assert _seed_hits("RATED BOX PADS AND FIRESTOP SYSTEM LABELING") == ["EX-FIRESTOP-LABELS"]
    assert _rule(SEED_POLICY, "EX-ANCILLARY")["kind"] == "hard"


def test_fa_by_vendor_never_fires_on_division_not_used_text():
    rule = _rule(SEED_POLICY, "EX-FA-BY-VENDOR")
    assert rule["match"]["patterns"] == []
    assert rule["match"]["systems"] == ["fire_alarm", "access_control"]
    text = "DIVISION 28 ELECTRONIC SAFETY AND SECURITY: NOT USED"
    assert sp.match_rule(rule, text) is None
    assert "EX-FA-BY-VENDOR" not in [r["rule_id"] for r in sp.rules_for(SEED_POLICY, item_description=text)]
    assert sp.match_rule(rule, text, system="access_control") == "access_control"


def test_disabled_rule_never_fires():
    rule = dict(_rule(SEED_POLICY, "EX-BIM"), enabled=False)
    assert sp.match_rule(rule, "BIM COORDINATION") is None


def test_system_match_and_rules_for():
    rule = _rule(SEED_POLICY, "EX-FA-BY-VENDOR")
    assert sp.match_rule(rule, "", system="fire_alarm") == "fire_alarm"
    assert [r["rule_id"] for r in sp.rules_for(SEED_POLICY, topic="bonds")] == ["EX-BOND"]
    hits = sp.rules_for(SEED_POLICY, item_description="ARC FLASH STUDY BY OTHERS")
    assert [r["rule_id"] for r in hits] == ["EX-STUDIES"]
    assert len(sp.rules_for(SEED_POLICY)) == len(SEED_POLICY["rules"])
    assert sp.rule_by_id(SEED_POLICY, "IN-CONTROL-CABLE")["trigger_family"] == "thermostat"
    assert sp.rule_by_id(SEED_POLICY, "NOPE-X") is None


# The exact 5.8 shape ------------------------------------------------------------------------


def _spec_shape_doc() -> dict:
    """A policy in exactly the 5.8 shape: no evidence on bid_conditions,
    inclusions and allowances, and no inclusion rule_id."""
    doc = _doc()
    doc["bid_conditions"] = {
        "rate_table": {"prevailing_wage": "pw", "non_prevailing_wage": "open_shop"},
        "default_wage_type": "prevailing_wage",
    }
    doc["inclusions"] = [
        {
            "trigger_family": "thermostat",
            "pattern": r"\bthermostats?\b",
            "line_template": {
                "section": "NEW WORK",
                "group": "OTHER ITEMS",
                "subgroup": "MISCELLANEOUS",
                "description": "RELOCATION OF THERMOSTAT TO NEW LOCATION",
                "unit": "EA",
                "quantity": "sum_trigger_counts",
            },
            "dedupe_scope": "run_new_work_wins",
        },
        {
            "trigger_family": "thermostat",
            "pattern": r"\bt-?stats?\b",
            "line_template": {
                "section": "NEW WORK",
                "group": "OTHER ITEMS",
                "subgroup": "MISCELLANEOUS",
                "description": "#18/4C CONTROL CABLE",
                "unit": "FT",
                "quantity": "measured_else_per_trigger",
            },
            "dedupe_scope": "run",
        },
    ]
    doc["allowances"] = {"standard": list(ESTIMATE_ALLOWANCES), "extra_requires_document_amount": True}
    return doc


def test_validate_accepts_the_exact_5_8_shape_and_derives_inclusion_ids():
    doc = _spec_shape_doc()
    assert _errors(doc) == []
    info = sp.validate(doc)
    assert info.rule_ids[-2:] == ("IN-THERMOSTAT-1", "IN-THERMOSTAT-2")
    found = sp.rule_by_id(doc, "IN-THERMOSTAT-2")
    assert found is not None and found["rule_id"] == "IN-THERMOSTAT-2" and found["dedupe_scope"] == "run"
    assert "rule_id" not in doc["inclusions"][1]  # derivation never mutates the doc


def test_derived_inclusion_ids_skip_taken_ids_and_companions_resolve():
    doc = _spec_shape_doc()
    doc["inclusions"][1]["rule_id"] = "IN-THERMOSTAT-1"
    assert sp.inclusion_rule_ids(doc["inclusions"]) == ["IN-THERMOSTAT-2", "IN-THERMOSTAT-1"]
    doc["inclusions"][1]["companion_of"] = "IN-THERMOSTAT-2"
    assert _errors(doc) == []
    doc["inclusions"][0]["rule_id"] = "IN-THERMOSTAT-1"
    assert any("duplicate rule ids" in e for e in _errors(doc))


def test_extended_shape_still_valid_and_bad_optional_evidence_still_refused():
    assert _errors(_doc()) == []
    doc = _spec_shape_doc()
    doc["allowances"]["evidence"] = []
    assert any("allowances.evidence" in e for e in _errors(doc))
    doc = _spec_shape_doc()
    doc["bid_conditions"]["evidence"] = [{"source": "x", "support": 1}]
    assert any("bid_conditions.evidence" in e for e in _errors(doc))
    doc = _spec_shape_doc()
    doc["inclusions"][0]["rule_id"] = "bad id"
    assert any("rule_id" in e for e in _errors(doc))


# Validation refusals ------------------------------------------------------------------------


def test_validate_refuses_unless_on_hard_rules():
    doc = _doc()
    _rule(doc, "EX-TEMP-POWER")["unless"] = ["owner_allowance_amount"]
    with pytest.raises(sp.PolicyInvalid) as exc:
        sp.validate(doc)
    assert any("hard rule may not carry unless" in e for e in exc.value.errors)


def test_validate_accepts_unless_on_soft_rules():
    doc = _doc()
    rule = _rule(doc, "EX-TEMP-POWER")
    rule["kind"] = "soft"
    rule["unless"] = ["owner_allowance_amount"]
    sp.validate(doc)


def test_validate_refuses_unknown_unless_condition():
    doc = _doc()
    _rule(doc, "EX-SAWCUT-CONCRETE")["unless"] = ["whenever"]
    assert any("unknown conditions" in e for e in _errors(doc))


@pytest.mark.parametrize(
    "pattern,reason",
    [
        (r"\bfoo(", "does not compile"),
        (r"permit", "start"),
        (r"\bpermit", "end"),
        (r"\bpatch(?!\s*(panel|cord))", "end"),
        (r"(?:\bfoo|bar)\b", "start"),
        ("", "non-empty"),
    ],
)
def test_validate_refuses_bad_or_unbounded_regex(pattern, reason):
    doc = _doc()
    _rule(doc, "EX-BIM")["match"]["patterns"] = [pattern]
    errors = _errors(doc)
    assert any(reason in e for e in errors), errors


@pytest.mark.parametrize(
    "pattern",
    [
        r"\bpatch(?:ing)?\b(?!\s*(?:panels?|cords?)\b)",
        r"^\s*core\s+drill\s*$",
        r"(?<![A-Z])emt\b",
        r"\$\s?[\d,]+\s+allowance\b",
        r"#18/4C\b",
        r"(?:\bfoo|\bbar)\b",
    ],
)
def test_word_bounded_forms_accepted(pattern):
    assert sp.regex_problem(pattern) is None


def test_validate_refuses_missing_evidence():
    doc = _doc()
    _rule(doc, "EX-BIM")["evidence"] = []
    assert any("evidence required" in e for e in _errors(doc))
    doc = _doc()
    doc["inclusions"][0]["evidence"] = [{"source": "x", "support": 3, "of": 2, "note": None}]
    assert any("exceeds" in e for e in _errors(doc))
    doc = _doc()
    doc["standards"]["raceway"].pop("evidence")
    assert any("standards.raceway.evidence" in e for e in _errors(doc))


def test_validate_refuses_em_and_en_dash_and_non_ascii():
    for ch in ("\u2014", "\u2013", "\u00e9"):
        doc = _doc()
        _rule(doc, "EX-BIM")["default_text"] = f"BIM {ch} coordination"
        assert _errors(doc), ch


def test_validate_refuses_unknown_vocabulary():
    doc = _doc()
    doc["inclusions"][0]["line_template"]["group"] = "MISC STUFF"
    assert any("not a BOQ group" in e for e in _errors(doc))
    doc = _doc()
    doc["inclusions"][0]["line_template"]["subgroup"] = "PANELS"
    assert any("not a subgroup" in e for e in _errors(doc))
    doc = _doc()
    doc["inclusions"][0]["line_template"]["unit"] = "BOX"
    assert any(".unit" in e for e in _errors(doc))


def test_validate_refuses_oversize():
    doc = _doc()
    doc["notes"] = "x" * (sp.MAX_DOC_BYTES + 1)
    assert any("limit" in e for e in _errors(doc))


def test_validate_refuses_structural_problems():
    doc = _doc()
    doc["rules"].append(copy.deepcopy(doc["rules"][0]))
    assert any("duplicate rule ids" in e for e in _errors(doc))
    doc = _doc()
    _rule(doc, "EX-BIM")["decision"] = "maybe"
    assert any(".decision" in e for e in _errors(doc))
    doc = _doc()
    _rule(doc, "EX-BIM")["rule_id"] = "bim"
    assert any("rule_id" in e for e in _errors(doc))
    doc = _doc()
    _rule(doc, "EX-BIM")["surprise"] = 1
    assert any("unknown keys" in e for e in _errors(doc))
    doc = _doc()
    del doc["standards"]["demo_disposition"]
    assert any("demo_disposition" in e for e in _errors(doc))
    doc = _doc()
    doc["standards"]["precedence"] = ["house", "addendum"]
    assert any("permutation" in e for e in _errors(doc))
    doc = _doc()
    doc["inclusions"][1]["companion_of"] = "IN-NOPE"
    assert any("companion_of" in e for e in _errors(doc))
    doc = _doc()
    _rule(doc, "EX-BOND")["proposal_line"] = "No period at the end"
    assert any("period" in e for e in _errors(doc))
    doc = _doc()
    doc["bid_conditions"]["default_wage_type"] = "union_scale"
    assert any("default_wage_type" in e for e in _errors(doc))
    doc = _doc()
    doc["version"] = 0
    assert any("version" in e for e in _errors(doc))
    assert sp.validation_errors(["not", "a", "dict"]) == ["policy must be a JSON object"]


def test_policy_invalid_lists_every_error():
    doc = _doc()
    _rule(doc, "EX-BIM")["evidence"] = []
    _rule(doc, "EX-HAZMAT")["evidence"] = []
    with pytest.raises(sp.PolicyInvalid) as exc:
        sp.validate(doc)
    assert len(exc.value.errors) >= 2
    assert "invalid scope policy" in str(exc.value)


# Hashing, load and dump ---------------------------------------------------------------------


def test_sha_ignores_key_order_and_tracks_content():
    doc = _doc()
    reordered = json.loads(json.dumps(doc, sort_keys=True))
    assert sp.policy_sha(reordered) == sp.policy_sha(doc)
    doc["version"] = 2
    assert sp.policy_sha(doc) != SEED_SHA


def test_dump_load_round_trip(tmp_path):
    text = sp.dump(SEED_POLICY)
    assert text.endswith("\n")
    assert sp.loads(text) == SEED_POLICY
    path = tmp_path / "policy.json"
    path.write_text(text, encoding="utf-8")
    assert sp.policy_sha(sp.load(path)) == SEED_SHA


def test_loads_refuses_invalid_json_and_invalid_docs():
    with pytest.raises(sp.PolicyInvalid):
        sp.loads("{not json")
    doc = _doc()
    _rule(doc, "EX-TEMP-POWER")["unless"] = ["owner_allowance_amount"]
    with pytest.raises(sp.PolicyInvalid):
        sp.loads(json.dumps(doc))


# Active policy accessor -----------------------------------------------------------------------


def test_active_policy_defaults_to_seed():
    active = sp.active_policy()
    assert active.source == "seed"
    assert active.sha256 == SEED_SHA
    assert sp.seed_policy().sha256 == SEED_SHA


def test_set_active_policy_pins_a_valid_doc():
    doc = _doc()
    doc["version"] = 2
    active = sp.set_active_policy(doc, policy_id="p-2")
    assert sp.active_policy() is active
    assert active.version == 2 and active.policy_id == "p-2" and active.source == "memory"
    sp.reset_active_policy()
    assert sp.active_policy().source == "seed"


def test_set_active_policy_refuses_invalid_doc():
    doc = _doc()
    _rule(doc, "EX-TEMP-POWER")["unless"] = ["owner_allowance_amount"]
    with pytest.raises(sp.PolicyInvalid):
        sp.set_active_policy(doc)
    assert sp.active_policy().source == "seed"


def test_loader_backs_the_accessor_and_falls_back_to_seed():
    doc = _doc()
    doc["version"] = 3
    sp.set_active_policy_loader(lambda: doc)
    active = sp.active_policy()
    assert active.source == "table" and active.version == 3
    bad = _doc()
    bad["rules"] = []
    sp.set_active_policy_loader(lambda: bad)
    assert sp.active_policy().source == "seed"
    sp.set_active_policy_loader(lambda: None)
    assert sp.active_policy().source == "seed"
    pinned = sp.ActivePolicy(doc=doc, sha256="x", version=3, source="table", policy_id="id-3")
    sp.set_active_policy_loader(lambda: pinned)
    assert sp.active_policy() is pinned


# Golden: mined sent-proposal shares match the seed evidence -----------------------------------


def _load_miner():
    name = "mine_scope_policy"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.golden
@pytest.mark.skipif(not (BACKUP / "data" / "projects.json").exists(), reason="backup not on disk")
def test_golden_sent_proposal_template_lines_on_every_project():
    miner = _load_miner()
    excluded, _source = miner.excluded_projects("scope_policy")
    result = miner.mine_proposals(BACKUP, excluded)
    n = result["projects"]
    # Measured 2026-10-21 with the holdout excluded: 39 of 39 for all four.
    assert n == _rule(SEED_POLICY, "EX-TEMP-POWER")["evidence"][0]["of"]
    for line, count in result["template_lines"].items():
        assert count == n, line
