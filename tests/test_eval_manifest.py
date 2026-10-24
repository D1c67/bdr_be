"""app/services/eval_manifest.py: tiers, the 66 golden ids, excluded_projects,
the blind guard and the manifest round trip. No backup, no EVAL_ROOT."""

from __future__ import annotations

import json

import pytest

from app.services import eval_manifest as M


def _case(**over):
    base = dict(
        case_id="7154",
        project_id="7dfc64e6-36ef-4c67-9d02-0d7942015011",
        number="26.8.7154",
        name="Test case",
        tier="smoke",
        project_type="other",
        wage_type="prevailing_wage",
        inputs=(),
        answers=(M.CaseAnswer("f1", "BOQ - COUNTS.xlsx", "boq", "a" * 64, "p/boq/x.xlsx"),),
        reference_path="",
        reviewed=False,
        blind_overrides=(),
        bundles=("ff8fad25",),
        notes="",
        big_sheets=18,
        spec_pages=170,
    )
    base.update(over)
    return M.Case(**base)


def _upload(filename, category="drawing", sha="b" * 64):
    return {"filename": filename, "category": category, "sha256": sha}


# tiers -----------------------------------------------------------------------------


def test_tiers_are_fixed_and_golden_ids_are_66():
    assert len(M.GOLDEN_IDS) == 66 and len(set(M.GOLDEN_IDS)) == 66
    assert [c for c, _p, _n in M.TIER_CASES["smoke"]] == ["7154", "7177", "7172"]
    assert [c for c, _p, _n in M.TIER_CASES["core"]] == ["7181", "7158", "7196", "7179", "7167"]
    assert [c for c, _p, _n in M.TIER_CASES["holdout"]] == ["7188", "7173", "7178", "7148"]
    assert sorted(p[:8] for p in M.HOLDOUT_IDS) == ["078c470d", "220937f8", "346a995e", "ba9f0bc2"]
    assert len(M.TIER_IDS["pool"]) == 54
    assert M.tier_of("7154") == "smoke" and M.tier_of("7179") == "core" and M.tier_of("ba9f0bc2") == "holdout"
    assert M.tier_of("026b2dc5") == "pool" and M.tier_of("nope") == "none"
    assert M.CORE_DROP_ORDER == ("7179", "7158")
    assert set(M.TIER_IDS["smoke"]) | set(M.TIER_IDS["core"]) | set(M.TIER_IDS["holdout"]) | set(M.TIER_IDS["pool"]) == set(M.GOLDEN_IDS)


def test_excluded_projects_includes_holdout_and_own_case():
    for purpose in M.PURPOSES:
        excluded = M.excluded_projects(purpose, "7154")
        assert M.HOLDOUT_IDS <= excluded
        assert "7dfc64e6-36ef-4c67-9d02-0d7942015011" in excluded
        assert len(excluded) == 5
    assert M.excluded_projects("mining") == M.HOLDOUT_IDS
    # a holdout case excludes itself and the holdout (still 4)
    assert len(M.excluded_projects("pricing_library", "7188")) == 4
    # project id, prefix and case id resolve to the same set
    full = "c45e3f91-133e-4b1b-bc1b-49e2c8b36f02"
    assert M.excluded_projects("bands", full) == M.excluded_projects("bands", "c45e3f91") == M.excluded_projects("bands", "7179")
    # a non-golden project excludes itself as given
    assert "zzz-not-golden" in M.excluded_projects("thresholds", "zzz-not-golden")
    with pytest.raises(ValueError):
        M.excluded_projects("bogus", "7154")
    sha = M.excluded_project_ids_sha256(M.excluded_projects("mining", "7154"))
    assert len(sha) == 64 and sha == M.excluded_project_ids_sha256(sorted(M.excluded_projects("mining", "7154"), reverse=True))


# blind guard ------------------------------------------------------------------------


def test_blind_guard_hash_name_category_quote():
    case = _case()
    ok = [_upload("ELEC drawings.pdf"), _upload("SPECS.pdf", "specification"), _upload("Exhibit A.pdf", "drawing")]
    M.blind_guard(case, ok, stage="quantity")
    with pytest.raises(M.BlindViolation) as e:
        M.blind_guard(case, [_upload("innocent.pdf", "drawing", "a" * 64)], stage="quantity")
    assert e.value.code == "answer_hash_leak"
    # a hash of any golden case's answer (manifest-wide table)
    with pytest.raises(M.BlindViolation) as e:
        M.blind_guard(case, [_upload("x.pdf", "drawing", "c" * 64)], stage="quantity", answer_hashes={"c" * 64: "7177:BOQ"})
    assert e.value.code == "answer_hash_leak"
    with pytest.raises(M.BlindViolation) as e:
        M.blind_guard(case, [_upload("Lighting_RFQ.xlsx", "rfq_split")], stage="pricing")
    assert e.value.code == "answer_category"
    with pytest.raises(M.BlindViolation) as e:
        M.blind_guard(case, [_upload("Marked Plans.pdf", "drawing")], stage="quantity")
    assert e.value.code == "answer_name"
    with pytest.raises(M.BlindViolation) as e:
        M.blind_guard(case, [_upload("Graybar Lighting Quote.pdf", "quote")], stage="quantity")
    assert e.value.code == "quote_in_quantity_stage"
    # quotes are fine in the pricing stage; a name override needs the manifest
    M.blind_guard(case, [_upload("Graybar Lighting Quote.pdf", "quote")], stage="pricing")
    M.blind_guard(_case(blind_overrides=("Marked Plans.pdf",)), [_upload("Marked Plans.pdf", "drawing")], stage="quantity")
    with pytest.raises(ValueError):
        M.blind_guard(case, [], stage="reading")
    # CaseInput values work too
    inp = M.CaseInput("f9", "BOQ - COUNTS.xlsx", "drawing", "d" * 64, None, "quantity", "p")
    with pytest.raises(M.BlindViolation):
        M.blind_guard(None, [inp], stage="quantity")


# manifest round trip ---------------------------------------------------------------------


def test_manifest_round_trip_and_case_lookup(tmp_path):
    inp_ok = M.CaseInput("f2", "ELEC.pdf", "electrical_drawing", "e" * 64, "electrical_drawings", "quantity", "p/e.pdf", pages=10, big_pages=10)
    inp_bad = M.CaseInput("f3", "Addendum LVCC.pdf", "addendum", "f" * 64, "addenda", "quantity", "p/a.pdf", pages=5, big_pages=2, misfiled=True, note="belongs elsewhere")
    quote = M.CaseInput("f4", "Quote.pdf", "quote", "1" * 64, None, "pricing", "p/q.pdf")
    case = _case(inputs=(inp_ok, inp_bad, quote), misfiled_inputs=("Addendum LVCC.pdf",))
    assert [i.filename for i in case.quantity_inputs] == ["ELEC.pdf"]
    assert [i.filename for i in case.pricing_inputs] == ["Quote.pdf"]
    manifest = M.Manifest(M.MANIFEST_SCHEMA, "2026-10-21T10:00:00-07:00", "/backup", [case], list(M.GOLDEN_IDS), sorted(M.HOLDOUT_IDS), {"a" * 64: "7154:BOQ - COUNTS.xlsx"}, {"golden": {"ids": list(M.GOLDEN_IDS), "n": 66}}, ["note"])
    path = M.write_manifest(manifest, tmp_path)
    assert path == tmp_path / "manifest" / "manifest.json"
    doc = json.loads(path.read_text())
    assert doc["tiers"]["smoke"] == ["7154"] and doc["schema"] == M.MANIFEST_SCHEMA
    loaded = M.load_manifest(tmp_path)
    assert loaded.case("7154").big_sheets == 18
    assert loaded.case("7dfc64e6").misfiled_inputs == ("Addendum LVCC.pdf",)
    assert loaded.case("7154").inputs[1].misfiled is True
    assert M.cases("smoke", tmp_path)[0].case_id == "7154" and M.cases("core", tmp_path) == []
    assert M.answer_hashes(tmp_path) == {"a" * 64: "7154:BOQ - COUNTS.xlsx"}
    with pytest.raises(KeyError):
        loaded.case("9999")
    assert M.is_big_sheet(42, 30) and M.is_big_sheet(18, 24) and not M.is_big_sheet(11, 8.5) and not M.is_big_sheet(36, 12)
    assert chr(0x2014) not in json.dumps(doc) and chr(0x2013) not in json.dumps(doc)
