"""plans_reader_regions: code-computed escalation and recount regions (build
spec WS4; E19 text anchors, E11 containment). The 7154 checks read the
committed page facts fixture (tag positions, schedule rows, views); the
one test that needs the words of the backup PDF is golden."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import plans_reader_floors as F
from app.services import plans_reader_regions as RG
from app.services.plans_reader_images import region_to_box
from tests.plans_reader_fixtures import ELEC_DOC_7154, FIXTURE_7154, load_7154

SETTINGS = {"plans_reader_region_pad": 0.03, "plans_reader_cluster_gap": 0.06}
ELEC_PDF = Path(
    "/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01/storage/project-files/"
    "7dfc64e6-36ef-4c67-9d02-0d7942015011/drawing/"
    "c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
# The tiles the weekend run's model chose (groups.json pages[].tiles).
MODEL_TILES_ED3 = [[0.17, 0.38, 0.41, 0.57], [0.45, 0.40, 0.62, 0.55]]
MODEL_TILES_ED2 = [[0.16, 0.37, 0.63, 0.59]]


@pytest.fixture(scope="module")
def elec() -> dict:
    facts = json.loads((FIXTURE_7154 / "page_facts_elec.json").read_text())
    return facts["documents"][0]["facts"]


@pytest.fixture(scope="module")
def pages(elec) -> dict[int, dict]:
    return {p["page"]: p for p in elec["pages"]}


@pytest.fixture(scope="module")
def flags() -> dict[int, list[dict]]:
    d = load_7154()
    out: dict[int, list[dict]] = {}
    for g in d["groups"]:
        if g["kind"] == "sub" and g["category"] == "electrical_drawings":
            for pg in g["output"]["pages"]:
                for f in pg.get("resolution_flags") or []:
                    out.setdefault(pg["page"], []).append(f)
    return out


def _contains_all(regions: list[RG.CodeRegion], points: list[list[float]]) -> int:
    return RG.points_inside(points, [r.box for r in regions])


# 7154 ---------------------------------------------------------------------------------------


def test_regions_contain_39_of_39_and_34_of_34(pages, flags):
    ed3 = RG.regions_for_flags(flags[8], page=pages[8], settings=SETTINGS)
    ed2 = RG.regions_for_flags(flags[7], page=pages[7], settings=SETTINGS)
    x39 = F.family_points(pages[8], "demo_x")
    x34 = F.family_points(pages[7], "demo_x")
    assert len(x39) == 39 and len(x34) == 34
    assert all(r.kind == "tag_cluster" and r.family == "demo_x" for r in ed3 + ed2)
    assert _contains_all(ed3, x39) == 39
    assert _contains_all(ed2, x34) == 34
    assert sum(r.contains for r in ed3) == 39 and all(r.targets == 39 for r in ed3)
    assert sum(r.contains for r in ed2) == 34 and all(r.targets == 34 for r in ed2)
    assert 1 <= len(ed3) <= RG.MAX_REGIONS and 1 <= len(ed2) <= RG.MAX_REGIONS
    # every region string feeds the existing tile cutter unchanged
    for r in ed3 + ed2:
        box = region_to_box(r.region, 4200, 3000)
        assert box[2] > box[0] and box[3] > box[1]
    assert all(r.view_id == "v1" for r in ed3 + ed2)


def test_model_tiles_held_22_of_39_and_16_of_34(pages):
    assert RG.containment(pages[8], "demo_x", MODEL_TILES_ED3) == {"family": "demo_x", "targets": 39, "inside": 22, "complete": False}
    assert RG.containment(pages[7], "demo_x", MODEL_TILES_ED2) == {"family": "demo_x", "targets": 34, "inside": 16, "complete": False}


def test_l4_flag_anchors_to_the_schedule_row(pages, flags, elec):
    regions = RG.regions_for_flags(flags[5], page=pages[5], schedules=elec["schedules"], settings=SETTINGS)
    assert len(regions) == 1
    r = regions[0]
    assert r.kind == "text_anchor" and r.anchors == ["luminaire L4"]
    lum = [s for s in elec["schedules"] if s["kind"] == "luminaire"][0]
    row = {x["tag"]: x["bbox"] for x in lum["luminaires"]}
    # the L4 row band (tag to the CONTROL column) is inside, the L1 row is not
    assert RG.point_in(((row["L4"][0] + row["L4"][2]) / 2, (row["L4"][1] + row["L4"][3]) / 2), r.box)
    assert r.box[0] <= row["L4"][0] and r.box[2] >= row["L4"][2]
    assert not RG.point_in(((row["L1"][0] + row["L1"][2]) / 2, (row["L1"][1] + row["L1"][3]) / 2), r.box)
    cy = (r.box[1] + r.box[3]) / 2
    assert abs(cy - (row["L4"][1] + row["L4"][3]) / 2) < abs(cy - (row["L1"][1] + row["L1"][3]) / 2)
    # the model's own crop (0.78,0.045,0.82,0.06) sat on the L1 row and missed the tag column
    assert not (r.box[0] >= 0.78)


@pytest.mark.golden
@pytest.mark.skipif(not ELEC_PDF.exists(), reason="7154 backup PDF not on this machine")
def test_l4_region_contains_local_in_the_words(pages, flags, elec):
    from app.services.pagefacts import extract

    words = extract.extract_words(ELEC_PDF.read_bytes(), pages=[5])[5]
    regions = RG.regions_for_flags(flags[5], page=pages[5], words=words, schedules=elec["schedules"], settings=SETTINGS)
    r = regions[0]
    local = [w for w in words.words if w.t.upper() == "LOCAL" and RG.point_in((w.cx, w.cy), r.box)]
    assert local, "LOCAL (the L4 control method) must be inside the L4 region"
    l4 = [w for w in words.words if w.t.upper() == "L4" and RG.point_in((w.cx, w.cy), r.box)]
    assert l4
    # the words-only path (no facts schedules) finds the same row
    only_words = RG.text_anchor_regions(flags[5][0]["reason"], page=dict(pages[5], tags={"type_marks": {"L4": [[0.5, 0.5]]}}), words=words, schedules=(), pad=0.03)
    assert only_words and any(RG.point_in((w.cx, w.cy), only_words[0].box) for w in local)


def test_for_recount_uses_the_floor_positions(pages):
    facts = {(ELEC_DOC_7154, p): pages[p] for p in pages}
    floors = F.compute_floors(facts, doc_numbers={ELEC_DOC_7154: 1})
    regs = RG.for_recount("D1P8", "demo_x", floors, page=pages[8], settings=SETTINGS)
    assert regs and sum(r.contains for r in regs) == 39
    assert all("recount demo_x on ED3.01: floor 39" in (r.reason or "") for r in regs)
    assert RG.for_recount("D1P8", "relocate_r", floors, page=pages[8], settings=SETTINGS) == []  # unknown floor: no guess
    assert RG.for_recount("D1P10", "fixture_mark:L5", floors, page=pages[10], settings=SETTINGS)[0].contains == 7
    # words-only recount (no facts page): status tags straight from the words
    class _W:
        def __init__(self, t, b):
            self.t, self.b = t, b
    words = type("PW", (), {"words": [_W("(X)", [0.1, 0.1, 0.12, 0.12]), _W("(X)", [0.5, 0.5, 0.52, 0.52])], "text_class": "cad_text"})()
    fl = F.Floor(family="demo_x", sheet_key="D9P1", document_id="d9", page=1, sheet_number="ED9", floor=2, strength="strong", method="text_layer")
    regs = RG.for_recount("D9P1", "demo_x", [fl], words, settings=SETTINGS)
    assert sum(r.contains for r in regs) == 2


# Synthetic ------------------------------------------------------------------------------------


def test_cluster_boxes_contain_every_point_even_past_max_regions():
    pts = [[0.05 + 0.2 * i, 0.5] for i in range(5)] + [[0.06 + 0.2 * i, 0.5] for i in range(5)]
    boxes = RG.cluster_boxes(pts, 42.0, 30.0, pad=0.03, gap=0.06, max_regions=4)
    assert len(boxes) == 4 and RG.points_inside(pts, boxes) == len(pts)
    one = RG.cluster_boxes([[0.5, 0.5]], 42.0, 30.0, pad=0.0, gap=0.06)
    assert one[0][2] - one[0][0] >= RG.MIN_SIDE - 1e-9
    assert RG.cluster_boxes([], 42.0, 30.0) == []
    merged = RG.merge_overlapping([[0, 0, 0.5, 0.5], [0.4, 0.4, 0.8, 0.8], [0.9, 0.9, 1, 1]])
    assert merged == [[0.0, 0.0, 0.8, 0.8], [0.9, 0.9, 1.0, 1.0]]


def test_region_settings_and_fallback_to_the_model_region():
    assert RG.region_settings({"plans_reader_region_pad": 0.01, "plans_reader_cluster_gap": 0.02}) == (0.01, 0.02)
    page = {"page": 1, "w_in": 42, "h_in": 30, "tags": {"status": {}, "circuit": [], "type_marks": {}, "panel_labels": []}, "views": [], "regions": []}
    regs = RG.regions_for_flags([{"region": "r2c2", "reason": "blurry symbols"}, {"region": "whole", "reason": "x"}, {"region": "nonsense", "reason": "y"}], page=page, settings=SETTINGS)
    assert [r.kind for r in regs] == ["model", "model"]
    assert regs[0].box == [round(1 / 3, 4), round(1 / 3, 4), round(2 / 3, 4), round(2 / 3, 4)]
    assert regs[1].region == "0.0000,0.0000,1.0000,1.0000"


def test_overall_vs_enlarged_views_deduped():
    page = {
        "page": 1, "w_in": 42, "h_in": 30, "regions": [],
        "views": [
            {"view_id": "v1", "title": "OVERALL LIGHTING PLAN", "kind": "plan", "region": [0, 0, 0.6, 1]},
            {"view_id": "v2", "title": "ENLARGED LOBBY PLAN", "kind": "plan", "region": [0.6, 0, 1, 1]},
        ],
        "tags": {"status": {"X": [[0.1, 0.5], [0.15, 0.5], [0.8, 0.5]]}, "circuit": [], "type_marks": {}, "panel_labels": []},
    }
    regs = RG.regions_for_flags([{"region": "whole", "reason": "confirm the demolition (X) count"}], page=page, settings=SETTINGS)
    assert len(regs) == 1 and regs[0].targets == 2 and regs[0].contains == 2 and regs[0].view_id == "v1"
    assert RG.containment(page, "demo_x", [[0, 0, 1, 1]]) == {"family": "demo_x", "targets": 2, "inside": 2, "complete": True}


def test_anchor_tokens_and_family_for_reason():
    page = {"tags": {"type_marks": {"L4": [[0.1, 0.1]]}, "panel_labels": [{"panel": "1LH"}]}}
    assert RG.anchor_tokens("L4 CONTROL METHOD cell appears malformed", page) == ["L4"]
    assert RG.anchor_tokens("confirm 1LH-4 and panel 1LH", page) == ["1LH", "1LH-4"]
    assert RG.anchor_tokens("nothing here", page) == []
    assert RG.family_for_reason("Confirm small demolition receptacle graphics") == "demo_x"
    assert RG.family_for_reason("relocated (R) devices") == "relocate_r"
    assert RG.family_for_reason("blurry") is None
