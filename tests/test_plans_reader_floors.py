"""plans_reader_floors: census floors from the WS1 facts and instance
accounting (build spec WS4). The 7154 numbers come from the committed page
facts fixture (ED2.01 34 and ED3.01 39 printed "(X)" tags); everything else
is synthetic."""

from __future__ import annotations

import json

import pytest

from app.services import plans_reader_floors as F
from app.services import plans_reader_rows as R
from tests.plans_reader_fixtures import ELEC_DOC_7154, FIXTURE_7154, load_7154, rows_meta

SETTINGS = {"plans_reader_census_tol_strong": 0.05, "plans_reader_census_tol_weak": 0.10}


@pytest.fixture(scope="module")
def run7154() -> dict:
    return load_7154()


@pytest.fixture(scope="module")
def elec_pages() -> dict[tuple[str, int], dict]:
    facts = json.loads((FIXTURE_7154 / "page_facts_elec.json").read_text())
    elec = facts["documents"][0]["facts"]
    return {(ELEC_DOC_7154, p["page"]): p for p in elec["pages"]}


@pytest.fixture(scope="module")
def floors(elec_pages) -> list[F.Floor]:
    return F.compute_floors(elec_pages, doc_numbers={ELEC_DOC_7154: 1})


# Floors on 7154 -------------------------------------------------------------------------


def test_floors_34_and_39(floors):
    by = F.floors_by_sheet(floors)
    ed2 = by[("D1P7", "demo_x")]
    ed3 = by[("D1P8", "demo_x")]
    assert (ed2.floor, ed2.sheet_number, ed2.strength, ed2.method) == (34, "ED2.01", "strong", "text_layer")
    assert (ed3.floor, ed3.sheet_number, ed3.strength, ed3.method) == (39, "ED3.01", "strong", "text_layer")
    assert len(ed3.positions) == 39 and all(0 <= x <= 1 and 0 <= y <= 1 for x, y in ed3.positions)
    assert ed3.known and ed3.key == "demo_x"
    # weak families are measured and labeled weak
    assert by[("D1P9", "circuit")].floor == 74 and by[("D1P9", "circuit")].strength == "weak"
    assert by[("D1P10", "fixture_mark:L5")].floor == 7 and by[("D1P10", "fixture_mark:L5")].tag == "L5"
    assert by[("D1P9", "device_mod:G")].floor == 9
    # modifier letters that are not device modifiers are not families
    assert not [k for k in by if k[1] in ("device_mod:A", "device_mod:O", "device_mod:L5")]
    # none found is unknown: no demo floor on the new-work sheets
    assert ("D1P9", "demo_x") not in by and ("D1P10", "demo_x") not in by
    # schedule and legend pages carry no device instances
    assert not [f for f in floors if f.page in (1, 2, 3, 4, 5)]


def test_floor_round_trip_and_tolerance(floors):
    fl = F.floors_by_sheet(floors)[("D1P8", "demo_x")]
    again = F.Floor.from_dict(json.loads(json.dumps(fl.to_dict())))
    assert again.to_dict() == fl.to_dict()
    assert F.tolerance(39, "strong", settings=SETTINGS) == 2
    assert F.tolerance(39, "weak", settings=SETTINGS) == 4
    assert F.tolerance(3, "strong", settings=SETTINGS) == 1  # never below 1
    assert F.tolerances({"plans_reader_census_tol_strong": 0.02, "plans_reader_census_tol_weak": 0.2}) == (0.02, 0.2)


def test_none_found_is_unknown_and_scanned_pages_are_unavailable():
    page = {"page": 3, "w_in": 42, "h_in": 30, "text_layer_quality": "full", "roles": ["plan"], "tags": {"circuit": [], "status": {}, "type_marks": {}, "panel_labels": []}}
    assert F.floors_for_page(page, document_id="d", sheet_key="D1P3") == []
    scanned = dict(page, text_layer_quality="none")
    out = F.floors_for_page(scanned, document_id="d", sheet_key="D1P3")
    assert out and all(f.floor is None and f.method == "unavailable" and not f.known for f in out)
    assert {f.family for f in out} == {"demo_x", "relocate_r", "circuit"}


def test_vector_text_and_sparse_pages_have_no_usable_text_layer():
    """Spec 5.1: none, sparse and vector_text are not usable text layers; the
    census is unavailable there, never a count, even when tags were found."""
    tags = {"circuit": [], "status": {"X": [[0.2, 0.2], [0.4, 0.4]]}, "type_marks": {}, "panel_labels": []}
    page = {"page": 3, "w_in": 42, "h_in": 30, "roles": ["plan"], "tags": tags}
    assert F.USABLE_TEXT_QUALITIES == {"full"}
    known = F.floors_for_page(dict(page, text_layer_quality="full"), document_id="d", sheet_key="D1P3")
    assert [(f.key, f.floor) for f in known] == [("demo_x", 2)]
    for quality, text_class in (("vector_text", "vector_text"), ("sparse", "sparse"), ("none", "scanned")):
        for pg in (dict(page, text_layer_quality=quality), dict(page, text_class=text_class)):
            assert F.text_usable(pg) is False, pg
            out = F.floors_for_page(pg, document_id="d", sheet_key="D1P3")
            assert out and all(f.floor is None and f.method == "unavailable" for f in out), pg
    assert F.text_class_usable("cad_text") and F.text_class_usable("scanned_ocr")
    assert not F.text_class_usable("vector_text") and not F.text_class_usable("skipped_budget") and not F.text_class_usable(None)


def test_family_points_and_view_dedupe():
    page = {
        "page": 2, "w_in": 42, "h_in": 30, "text_layer_quality": "full", "roles": ["plan"],
        "views": [
            {"view_id": "v1", "title": "OVERALL POWER PLAN", "kind": "plan", "region": [0, 0, 0.6, 1]},
            {"view_id": "v2", "title": "ENLARGED PHARMACY PLAN", "kind": "plan", "region": [0.6, 0, 1, 1]},
        ],
        "tags": {
            "circuit": [{"panel": "LA", "circuits": [1], "mods": ["G"], "x": 0.1, "y": 0.1}, {"panel": "LA", "circuits": [1], "mods": ["G"], "x": 0.1, "y": 0.1}, {"panel": "LA", "circuits": [3], "mods": [], "x": 0.8, "y": 0.2}],
            "status": {"X": [[0.1, 0.5], [0.2, 0.5], [0.7, 0.5]], "R": [[0.3, 0.3]], "RL": [[0.35, 0.3]]},
            "type_marks": {"L1": [[0.1, 0.2], [0.9, 0.2]]},
            "panel_labels": [],
        },
    }
    # the enlarged view's copies are dropped when an overall view exists
    assert F.family_points(page, "demo_x") == [[0.1, 0.5], [0.2, 0.5]]
    assert F.family_points(page, "relocate_r") == [[0.3, 0.3], [0.35, 0.3]]
    assert F.family_points(page, "circuit") == [[0.1, 0.1]]  # each token once; the enlarged copy dropped
    assert F.family_points(page, "fixture_mark", "L1") == [[0.1, 0.2]]
    assert F.family_points(page, "device_mod", "G") == [[0.1, 0.1], [0.1, 0.1]]
    no_overall = dict(page, views=[page["views"][1]])
    assert len(F.family_points(no_overall, "demo_x")) == 3
    floors = F.floors_for_page(page, document_id="d", sheet_key="D1P2")
    assert {f.key: f.floor for f in floors} == {"demo_x": 2, "relocate_r": 2, "circuit": 1, "fixture_mark:L1": 1, "device_mod:G": 2}
    with pytest.raises(ValueError):
        F.family_points(page, "nope")


# Accounting ------------------------------------------------------------------------------


def test_accounting_counts_instances_legacy_and_v2(run7154, floors, elec_pages):
    by = F.floors_by_sheet(floors)
    lines = list(R.iter_rows(run7154["main"], kinds="B"))
    ed3 = by[("D1P8", "demo_x")]
    legacy = F.accounted(ed3, lines, mode="legacy")
    # 13 + 5 + 2 + 12 + 2: the 3 EA track rows are a linear_in_ea finding, not instances
    assert legacy.count == 34 and legacy.mode == "legacy"
    track = [i for i in legacy.items if "TRACK ROW" in i["description"]][0]
    assert track["instances"] == 0
    assert F.census_status(ed3, legacy, settings=SETTINGS) == {"status": "short", "floor": 39, "accounted": 34, "tolerance": 2, "strength": "strong"}
    ed2 = by[("D1P7", "demo_x")]
    assert F.accounted(ed2, lines, mode="legacy").count == 34
    assert F.census_status(ed2, F.accounted(ed2, lines, mode="legacy"), settings=SETTINGS)["status"] == "pass"
    # v2 rows: the track row's 3 segments are 3 instances (34 + 3)
    v2 = R.upgrade_category_output(run7154["electrical"], rows_meta(run7154))
    acc = F.accounted(ed3, v2["counts"], mode="v2")
    assert acc.count == 37 and len(acc.ids) == 6
    assert F.accounted(ed2, v2["counts"], mode="v2").count == 34


def test_accounting_skips_alternates_and_unknown_is_unknown():
    fl = F.Floor(family="demo_x", sheet_key="D1P1", document_id="d", page=1, sheet_number="ED1.01", floor=10, strength="strong", method="text_layer")
    rows = [
        {"row_id": "C1", "description": "Remove existing receptacle", "quantity": 4, "unit": "EA", "status": "active", "sheet": {"sheet_key": "D1P1", "sheet_number": "ED1.01"}, "family_tags": ["demo_x@D1P1"]},
        {"row_id": "C2", "description": "Remove existing receptacle", "quantity": 9, "unit": "EA", "status": "alternate", "sheet": {"sheet_key": "D1P1", "sheet_number": "ED1.01"}},
        {"row_id": "C3", "description": "Remove existing fixture", "quantity": 3, "unit": "EA", "status": "unverifiable", "sheet": {"sheet_key": "D1P1", "sheet_number": "ED1.01"}},
        {"row_id": "C4", "description": "Remove existing track", "quantity": 40, "unit": "FT", "status": "superseded", "superseded": [{"reason": "superseded by measure M1"}], "segments": [1, 2], "sheet": {"sheet_key": "D1P1", "sheet_number": "ED1.01"}},
        {"row_id": "C5", "description": "Remove existing switch", "quantity": 2, "unit": "EA", "status": "active", "sheet": {"sheet_key": "D1P2", "sheet_number": "ED1.02"}},
        {"row_id": "C6", "description": "Duplex receptacle", "quantity": 7, "unit": "EA", "status": "active", "sheet": {"sheet_key": "D1P1", "sheet_number": "ED1.01"}},
    ]
    acc = F.accounted(fl, rows)
    assert acc.count == 6 and acc.ids == ["C1", "C4"]
    assert F.accounted(fl, []).count is None
    assert F.census_status(fl, F.accounted(fl, []), settings=SETTINGS)["status"] == "unknown"
    unknown = F.Floor(family="demo_x", sheet_key="D1P1", document_id="d", page=1, sheet_number="ED1.01", floor=None, strength="strong", method="unavailable")
    assert F.census_status(unknown, acc, settings=SETTINGS)["status"] == "unknown"
    with pytest.raises(ValueError):
        F.accounted(fl, rows, mode="nope")


def test_family_keys():
    assert F.family_key("fixture_mark", "L5") == "fixture_mark:L5"
    assert F.split_family_key("fixture_mark:L5") == ("fixture_mark", "L5")
    assert F.split_family_key("demo_x") == ("demo_x", None)
    assert F.strength_of("demo_x") == "strong" and F.strength_of("circuit") == "weak"
    with pytest.raises(ValueError):
        F.family_key("nope")
