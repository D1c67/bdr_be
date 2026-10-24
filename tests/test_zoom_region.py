"""zoom_region (services/plans_reader_tool_handlers), the WS2 tool of spec
5.2 and the design paragraph of section 6 WS2.

Pinned here:
  E19   a read_text zoom whose bbox misses the L4 row is retargeted to the
        row (anchor L4 widened to its table row) and text_lines carry
        LOCAL; on the WS1 7154 fixture (schedule rows laid out from
        page_facts_elec.json) and, marked golden, on the backup PDF.
  E11   a count zoom with a census ref covering only part of the (X) tags
        on ED3.01 is widened to all of them: coverage ratios are asserted,
        never absolute counts.
  rot   a /Rotate 90 synthetic sheet: the anchored crop (displayed frame)
        holds ink where the flat sheet's does, and an empty corner none.
  $0    read_text or count without a target on a text-layer page returns
        candidates and no image (no PDF bytes read); verify_symbol with a
        bbox renders; no target and no bbox refuses.
  ppi   by purpose, stepped down until at most 4 tiles; a region that does
        not fit at the lowest rung is a $0 refusal.
  spec  provider image specs: on an Anthropic route (ToolContext.image_spec)
        every tile fits the long-edge and pixel caps and the request passes
        llm_parts.check_limits; the OpenAI default keeps the patch tiling.
  text  WordsTextIndex.search loads the pages it is asked about, or reports
        partial coverage, never a silent 0.
  spec  strict args, offered to recount and review and to chat only with
        Pro Max, image cost class.
Renders are stubbed where only the planning is under test (a full-sheet
cluster at 200 ppi is tens of megapixels); the rotated-page test renders
for real. No network, no database, no model, no prices.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from app.services import llm_parts as lp
from app.services import plans_reader_images as images
from app.services import plans_reader_tool_handlers as h
from app.services.plans_reader_evidence_inputs import LocalPdfInputs
from app.services.plans_reader_tools import (
    REGISTRY,
    FakeRowStore,
    FakeTextIndex,
    ToolContext,
    dispatch,
    fake_services,
    offered_tools,
    validate_args,
)
from tests.pagefacts_fixtures import make_pdf, sheet

FIXTURE = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7154" / "page_facts_elec.json"
DOC = "538c95b5-3df0-4d7e-b18b-c01135083d20"
DOC8 = DOC[:8]
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
ELEC_PDF = (
    BACKUP
    / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011/drawing"
    / "c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(not ELEC_PDF.exists(), reason="7154 backup PDF not on this machine")

SCHEDULE_PAGE = 5  # E0.04 LIGHTING FIXTURE SCHEDULE
ED3_PAGE = 8  # ED3.01 OVERALL LIGHTING PLAN - DEMOLITION WORK


# ── helpers ───────────────────────────────────────────────────────────────────


def _facts() -> dict:
    data = json.loads(FIXTURE.read_text())
    return data["documents"][0]["facts"]


def _page(facts: dict, number: str) -> dict:
    return next(p for p in facts["pages"] if p["sheet_number"] == number)


class FakeDocs:
    """DocumentAccess with a fixed sheet size and a byte-read counter."""

    def __init__(self, w_in: float = 42.0, h_in: float = 30.0):
        self.w_in, self.h_in = w_in, h_in
        self.bytes_reads = 0

    def documents(self, run_id):
        return [{"id": DOC}]

    def page_size(self, document_id, page):
        if document_id != DOC:
            return {"error": "unknown_document"}
        return {"w_in": self.w_in, "h_in": self.h_in, "rotation": 0}

    def pdf_bytes(self, document_id):
        self.bytes_reads += 1
        return b"%PDF-fake"


@pytest.fixture
def stub_render(monkeypatch):
    """Replace the PDFium render with a stub that returns the tile size."""
    calls: list[dict] = []

    def render(pdf_bytes, page_index0, ppi, box_px=None, *, max_patches=None):
        x0, y0, x1, y1 = box_px
        calls.append({"page": page_index0, "ppi": ppi, "box": box_px})
        return b"\x89PNG-stub", x1 - x0, y1 - y0

    monkeypatch.setattr(h.images, "render_page_png", render)
    return calls


def _schedule_runs(facts: dict) -> list[dict]:
    """The luminaire schedule rows of E0.04 as text runs laid across each
    row's bbox (the fixture keeps row bboxes and raw cells, not words)."""
    sched = next(s for s in facts["schedules"] if s["kind"] == "luminaire")
    runs: list[dict] = []
    tx0, ty0, tx1, ty1 = sched["bbox"]
    runs.append({"text": "LUMINAIRE SCHEDULE", "bbox": [tx0, ty0, tx0 + 0.05, ty0 + 0.004]})
    for lum, raw in zip(sched["luminaires"], sched["rows_raw"], strict=False):
        x0, y0, x1, y1 = lum["bbox"]
        n = len(raw)
        step = (x1 - x0) / n
        for i, tok in enumerate(raw):
            left = x0 + i * step
            runs.append({"text": tok, "bbox": [round(left, 4), y0, round(left + min(step * 0.8, 0.003 * len(tok) + 0.002), 4), y1]})
    return runs


def _ctx(svc_overrides: dict, call_id: str = "call_1", image_spec=None) -> ToolContext:
    return ToolContext(run_id="run", kind="recount", services=fake_services(**svc_overrides), call_id=call_id, image_spec=image_spec)


def _zoom(ctx: ToolContext, **args):
    base = {"document_id": DOC, "page": 1, "bbox": None, "anchor": None, "expect_text": None, "census_ref": None, "purpose": "read_text", "ppi": None}
    base.update(args)
    return dispatch(REGISTRY["zoom_region"], ctx, base)


# ── spec ──────────────────────────────────────────────────────────────────────


def test_zoom_region_is_registered_strict_and_offered_to_the_right_kinds():
    spec = REGISTRY["zoom_region"]
    assert spec.cost_class == "image" and spec.mode == "read" and spec.needs_pro_max
    assert spec.kinds == frozenset({"recount", "review", "chat"})
    assert "zoom_region" in [s.name for s in offered_tools("recount")]
    assert "zoom_region" in [s.name for s in offered_tools("review", mode="review")]
    assert "zoom_region" not in [s.name for s in offered_tools("chat")]
    assert "zoom_region" in [s.name for s in offered_tools("chat", pro_max=True)]
    assert "zoom_region" not in [s.name for s in offered_tools("main")]
    assert validate_args(spec, {"document_id": DOC, "page": 1, "bbox": None, "anchor": None, "expect_text": None, "census_ref": None, "purpose": "count", "ppi": None}) is None
    bad = validate_args(spec, {"document_id": DOC, "page": 1, "purpose": "count"})
    assert bad and bad["code"] == "invalid_args"
    bad2 = validate_args(spec, {"document_id": DOC, "page": 1, "bbox": [0, 0, 1], "anchor": None, "expect_text": None, "census_ref": None, "purpose": "count", "ppi": None})
    assert bad2 and "bbox" in bad2["message"]


# ── E19 on the WS1 fixture ───────────────────────────────────────────────────


def test_e19_read_text_retargets_to_the_l4_row_with_local(stub_render):
    facts = _facts()
    runs = _schedule_runs(facts)
    docs = FakeDocs()
    ctx = _ctx({"text": FakeTextIndex({(DOC, SCHEDULE_PAGE): runs}), "documents": docs})
    # The model framed the top of the schedule (the L1 and L2 rows) and
    # asked for L4: the L4 row sits below its box.
    wrong = [0.62, 0.05, 0.70, 0.066]
    out = _zoom(ctx, page=SCHEDULE_PAGE, bbox=wrong, anchor={"ref": None, "text": "L4", "occurrence": None}, purpose="read_text")
    assert not out.refused, out.message
    d = out.data
    row = next(lum for lum in facts["schedules"][4]["luminaires"] if lum["tag"] == "L4")["bbox"]
    assert d["retargeted"] is True and d["retarget_reason"] == "targets"
    used = d["bbox_used"][0]
    assert used[0] <= row[0] and used[2] >= row[2] and used[1] <= row[1] and used[3] >= row[3]
    assert d["coverage"] == {"targets": 1, "inside": 1, "complete": True}
    texts = [ln["text"] for ln in d["text_lines"]]
    assert any("LOCAL" in t for t in texts), texts
    assert any(t.startswith("L4") for t in texts)
    l4_line = next(ln for ln in d["text_lines"] if ln["text"].startswith("L4"))
    assert all(r.startswith(f"t:{DOC8}:{SCHEDULE_PAGE}:") for r in l4_line["refs"])
    assert d["ppi"] == h.PPI_BY_PURPOSE["read_text"] and len(d["views"]) >= 1
    assert out.views and out.views[0]["image"] == b"\x89PNG-stub"
    assert set(out.citations) >= {v["ref"] for v in d["views"]}
    assert docs.bytes_reads == 1
    # Measured: the requested box missed the row entirely; the union would
    # have been more than 3x the box, hence the move to the target row.
    requested_area = (wrong[2] - wrong[0]) * (wrong[3] - wrong[1])
    union = [min(wrong[0], row[0]), min(wrong[1], row[1]), max(wrong[2], row[2]), max(wrong[3], row[3])]
    assert (union[2] - union[0]) * (union[3] - union[1]) > h.UNION_AREA_FACTOR * requested_area


def test_e19_a_box_that_nearly_holds_the_row_widens_to_the_union(stub_render):
    facts = _facts()
    runs = _schedule_runs(facts)
    ctx = _ctx({"text": FakeTextIndex({(DOC, SCHEDULE_PAGE): runs}), "documents": FakeDocs()})
    row = next(lum for lum in facts["schedules"][4]["luminaires"] if lum["tag"] == "L4")["bbox"]
    near = [row[0] - 0.01, row[1] - 0.03, row[2] + 0.01, row[1] + 0.001]  # ends just above the row
    out = _zoom(ctx, page=SCHEDULE_PAGE, bbox=near, anchor={"ref": None, "text": "L4", "occurrence": None}, purpose="read_text")
    assert out.data["retargeted"] is True and out.data["retarget_reason"] == "union"
    assert len(out.data["bbox_used"]) == 1 and out.data["coverage"]["complete"] is True
    assert any("LOCAL" in ln["text"] for ln in out.data["text_lines"])


# ── E11 on the WS1 fixture ───────────────────────────────────────────────────


def _demo_x(facts: dict) -> list[list[float]]:
    return _page(facts, "ED3.01")["tags"]["status"]["X"]


def _x_runs_and_census(points: list[list[float]]) -> tuple[list[dict], dict]:
    runs = []
    occurrences = []
    for k, (x, y) in enumerate(points):
        box = [round(x - 0.004, 4), round(y - 0.003, 4), round(x + 0.004, 4), round(y + 0.003, 4)]
        runs.append({"text": "(X)", "bbox": box})
        occurrences.append({"ref": f"c:ed3x:{k}", "bbox": box})
    census = {"census_id": "ed3x", "count": len(points), "method": "text_layer", "family_specific": True, "occurrences": occurrences}
    return runs, census


def test_e11_count_zoom_widens_to_every_x_tag(stub_render):
    facts = _facts()
    points = _demo_x(facts)
    runs, census = _x_runs_and_census(points)
    ctx = _ctx({"text": FakeTextIndex({(DOC, ED3_PAGE): runs}), "rows": FakeRowStore(refs={"c:ed3x": census}), "documents": FakeDocs()})
    partial = [0.1, 0.45, 0.45, 0.65]
    before = sum(1 for x, y in points if partial[0] <= x <= partial[2] and partial[1] <= y <= partial[3]) / len(points)
    assert 0 < before < 1
    out = _zoom(ctx, page=ED3_PAGE, bbox=partial, census_ref="c:ed3x", purpose="count")
    assert not out.refused, out.message
    d = out.data
    assert d["retargeted"] is True
    cov = d["coverage"]
    assert cov["targets"] == len(points)
    after = cov["inside"] / cov["targets"]
    assert after > before
    assert after == 1.0 and cov["complete"] is True, (before, after)
    assert 1 <= len(d["bbox_used"]) <= h.MAX_REGIONS
    assert len(d["views"]) <= h.MAX_TILES and d["ppi"] <= h.PPI_BY_PURPOSE["count"]
    # Census occurrences are not citable as text refs; the views are.
    assert all(c.startswith("v:") for c in out.citations)
    # Measured E11 figures for the report.
    print(f"E11 fixture: requested ratio {before:.3f}, widened ratio {after:.3f}, regions {len(d['bbox_used'])}, tiles {len(d['views'])}, ppi {d['ppi']}")


def test_e11_one_census_occurrence_is_a_single_target(stub_render):
    facts = _facts()
    points = _demo_x(facts)
    runs, census = _x_runs_and_census(points)
    ctx = _ctx({"text": FakeTextIndex({(DOC, ED3_PAGE): runs}), "rows": FakeRowStore(refs={"c:ed3x": census}), "documents": FakeDocs()})
    out = _zoom(ctx, page=ED3_PAGE, bbox=None, census_ref="c:ed3x:3", purpose="verify_symbol")
    assert out.data["coverage"] == {"targets": 1, "inside": 1, "complete": True}
    assert len(out.data["bbox_used"]) == 1 and out.data["retarget_reason"] == "targets"
    unknown = _zoom(ctx, page=ED3_PAGE, bbox=[0.1, 0.1, 0.2, 0.2], census_ref="c:nope", purpose="verify_symbol")
    assert any("not resolvable" in w for w in unknown.data["warnings"]) and unknown.data["coverage"]["targets"] == 0


# ── $0 paths ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("purpose", ["read_text", "count"])
def test_target_required_on_a_text_layer_page_returns_candidates_at_zero_cost(stub_render, purpose):
    facts = _facts()
    runs = _schedule_runs(facts)
    docs = FakeDocs()
    ctx = _ctx({"text": FakeTextIndex({(DOC, SCHEDULE_PAGE): runs}), "documents": docs})
    out = _zoom(ctx, page=SCHEDULE_PAGE, bbox=[0.6, 0.03, 0.9, 0.1], purpose=purpose)
    assert not out.refused and out.views == [] and out.data["views"] == []
    cands = out.data["candidates"]
    assert cands and {c["text"] for c in cands} >= {"L4", "LOCAL"}
    assert all(c["ref"].startswith(f"t:{DOC8}:") for c in cands)
    assert any("needs a target" in w for w in out.data["warnings"])
    assert docs.bytes_reads == 0 and stub_render == []
    assert out.citations == []


def test_no_text_layer_allows_a_bbox_zoom_and_refuses_without_one(stub_render):
    ctx = _ctx({"text": FakeTextIndex({}), "documents": FakeDocs()})
    out = _zoom(ctx, page=3, bbox=[0.2, 0.2, 0.4, 0.4], purpose="count")
    assert not out.refused and len(out.data["views"]) == 1 and out.data["text_layer_quality"] == "none"
    assert any("no usable text layer" in w for w in out.data["warnings"])
    assert out.data["coverage"] == {"targets": 0, "inside": 0, "complete": False}
    refused = _zoom(ctx, page=3, bbox=None, purpose="count")
    assert refused.refused and refused.code == "need_target"
    unknown = _zoom(ctx, document_id="other", page=1, bbox=[0, 0, 0.5, 0.5], purpose="measure")
    assert unknown.refused and unknown.code == "unknown_document"


# ── ppi ──────────────────────────────────────────────────────────────────────


def test_ppi_by_purpose_and_step_down_to_four_tiles(stub_render):
    ctx = _ctx({"text": FakeTextIndex({}), "documents": FakeDocs()})
    whole = [0.0, 0.0, 1.0, 1.0]
    out = _zoom(ctx, page=2, bbox=whole, purpose="measure", ppi=400)
    assert not out.refused
    assert out.data["ppi"] < 400 and len(out.data["views"]) <= h.MAX_TILES
    assert len(stub_render) == len(out.data["views"])
    assert all(images.patches_for(v["w_px"], v["h_px"]) <= images.MAX_PATCHES_ORIGINAL for v in out.data["views"])
    small = _zoom(ctx, page=2, bbox=[0.4, 0.4, 0.45, 0.45], purpose="verify_symbol")
    assert small.data["ppi"] == h.PPI_BY_PURPOSE["verify_symbol"] and len(small.data["views"]) == 1
    assert h.choose_ppi([[0, 0, 0.1, 0.1]], "measure", None, 42.0, 30.0) == h.PPI_BY_PURPOSE["measure"]
    assert h.choose_ppi([[0, 0, 0.1, 0.1]], "count", 9999, 42.0, 30.0) == h.PPI_MAX


def test_region_too_large_is_a_zero_cost_refusal(stub_render):
    docs = FakeDocs(w_in=400.0, h_in=300.0)
    ctx = _ctx({"text": FakeTextIndex({}), "documents": docs})
    out = _zoom(ctx, page=1, bbox=[0.0, 0.0, 1.0, 1.0], purpose="measure")
    assert out.refused and out.code == "region_too_large"
    assert docs.bytes_reads == 0 and stub_render == []
    assert h.choose_ppi([[0, 0, 1, 1]], "measure", None, 400.0, 300.0) is None


# ── plan_views (pure) ────────────────────────────────────────────────────────


def test_plan_views_containment_union_and_clusters():
    t = lambda x, y: h.Target(bbox=[x, y, x + 0.01, y + 0.01], ref="t:x:1:0")  # noqa: E731
    inside, retargeted, reason, cov = h.plan_views([0.1, 0.1, 0.3, 0.3], [t(0.15, 0.15), t(0.2, 0.2)], "count", 42, 30)
    assert inside == [[0.1, 0.1, 0.3, 0.3]] and not retargeted and reason == "none" and cov["complete"]
    union, retargeted, reason, cov = h.plan_views([0.1, 0.1, 0.3, 0.3], [t(0.15, 0.15), t(0.31, 0.2)], "count", 42, 30)
    assert retargeted and reason == "union" and len(union) == 1 and cov["inside"] == 2
    far, retargeted, reason, cov = h.plan_views([0.1, 0.1, 0.12, 0.12], [t(0.11, 0.11), t(0.9, 0.9)], "count", 42, 30)
    assert retargeted and reason == "targets" and len(far) == 2 and cov["complete"]
    none, retargeted, reason, cov = h.plan_views(None, [t(0.5, 0.5)], "read_text", 42, 30)
    assert len(none) == 1 and reason == "targets" and not retargeted
    pad_x = h.PAD_IN["read_text"] / 42
    assert abs((none[0][2] - none[0][0]) - (0.01 + 2 * pad_x)) < 2e-4  # boxes are rounded to 4 decimals


def test_text_matching_folds_cad_doubles():
    assert h.text_matches("L4L4", "L4") and h.text_matches("L5/1LH", "L5")
    assert h.text_matches("TASK LED LIGHTING", "LED LIGHTING")
    assert not h.text_matches("L14", "L1") and not h.text_matches("", "L1")


# ── rotated page, real render ────────────────────────────────────────────────


def _ink(png: bytes) -> int:
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("L")
    return sum(1 for v in im.getdata() if v < 128)


def _tag_sheet(rotate: int):
    return sheet((10.0, 8.0, 14, "L4"), (18.0, 14.0, 14, "L1"), w_in=24.0, h_in=18.0, rotate=rotate)


@pytest.mark.parametrize("rotate", [0, 90])
def test_rotated_page_zoom_lands_on_the_displayed_text(rotate):
    pdf = make_pdf([_tag_sheet(rotate)])
    inputs = LocalPdfInputs({DOC: pdf}, page_budget_s=8.0, doc_budget_s=60.0)
    docs = h.LocalDocuments({DOC: pdf})
    size = docs.page_size(DOC, 1)
    assert (round(size["w_in"]), round(size["h_in"])) == (24, 18)  # displayed frame, whatever /Rotate says
    assert inputs.sheet(DOC, 1).rotation == rotate
    ctx = _ctx({"text": h.WordsTextIndex(inputs), "documents": docs})
    out = _zoom(ctx, page=1, bbox=None, anchor={"ref": None, "text": "L4", "occurrence": 1}, purpose="verify_symbol")
    assert not out.refused, out.message
    box = out.data["bbox_used"][0]
    cx, cy = (box[0] + box[2]) / 2 * 24.0, (box[1] + box[3]) / 2 * 18.0
    assert abs(cx - 10.2) < 0.6 and abs(cy - 7.9) < 0.6, (cx, cy)
    assert out.data["coverage"]["complete"] and out.data["text_lines"][0]["text"] == "L4"
    png = out.views[0]["image"]
    assert _ink(png) > 20
    # An empty corner of the same sheet (bottom-left; L1 sits bottom-right) renders no ink.
    blank = _zoom(ctx, page=1, bbox=[0.05, 0.75, 0.2, 0.9], purpose="measure")
    assert not blank.refused and _ink(blank.views[0]["image"]) == 0
    assert blank.data["text_lines"] == []


def test_words_text_index_search_and_quality():
    pdf = make_pdf([_tag_sheet(0)])
    index = h.WordsTextIndex(LocalPdfInputs({DOC: pdf}, page_budget_s=8.0, doc_budget_s=60.0))
    runs = index.runs(DOC, 1)
    assert [r["text"] for r in runs] == ["L4", "L1"] and runs[0]["ref"] == f"t:{DOC8}:1:0"
    assert index.text_layer_quality(DOC, 1) in ("full", "sparse", "none")  # two words classify as sparse or empty
    assert index.text_layer_quality("missing", 1) == "unknown"
    assert index.search("L1")["total"] == 1 and index.search("L", mode="exact")["total"] == 0
    assert index.runs("missing", 1)["error"] == "unavailable"


# ── golden: the backup PDF ───────────────────────────────────────────────────


@golden
@needs_backup
def test_golden_e19_l4_row_on_the_backup_schedule_sheet():
    pdf = ELEC_PDF.read_bytes()
    inputs = LocalPdfInputs({DOC: pdf}, page_budget_s=8.0, doc_budget_s=120.0)
    ctx = _ctx({"text": h.WordsTextIndex(inputs), "documents": h.LocalDocuments({DOC: pdf})})
    out = _zoom(ctx, page=SCHEDULE_PAGE, bbox=[0.62, 0.05, 0.70, 0.066], anchor={"ref": None, "text": "L4", "occurrence": None}, purpose="read_text")
    assert not out.refused, out.message
    d = out.data
    assert d["retargeted"] is True and d["coverage"]["complete"] is True
    lines = [ln["text"] for ln in d["text_lines"]]
    l4 = [t for t in lines if t.startswith("L4")]
    assert l4, lines
    # LOCAL on the L4 row itself: a LOCAL line whose vertical center lies
    # inside the widened L4 target (the next row's LOCAL does not count).
    tx0, ty0, tx1, ty1 = d["targets"][0]["bbox"]
    on_row = [
        ln for ln in d["text_lines"]
        if "LOCAL" in ln["text"] and ty0 <= (ln["bbox"][1] + ln["bbox"][3]) / 2 <= ty1
    ]
    assert on_row, [(ln["text"], ln["bbox"]) for ln in d["text_lines"] if "LOCAL" in ln["text"]]
    assert out.views and _ink(out.views[0]["image"]) > 0
    print(
        f"E19 golden: targets {d['coverage']['targets']}, regions {len(d['bbox_used'])}, lines {len(lines)}, "
        f"LOCAL lines on the L4 row {len(on_row)}, ppi {d['ppi']}, views {len(d['views'])}"
    )


@golden
@needs_backup
def test_golden_e11_x_tags_on_the_backup_demolition_sheet(stub_render):
    pdf = ELEC_PDF.read_bytes()
    inputs = LocalPdfInputs({DOC: pdf}, page_budget_s=8.0, doc_budget_s=120.0)
    index = h.WordsTextIndex(inputs)
    runs = index.runs(DOC, ED3_PAGE)
    xs = [r for r in runs if r["text"].replace("\r", "").replace("\n", "") == "(X)"]
    assert len(xs) >= 30
    ctx = _ctx({"text": index, "documents": h.LocalDocuments({DOC: pdf})})
    partial = [0.1, 0.45, 0.45, 0.65]
    before = sum(1 for r in xs if partial[0] <= (r["bbox"][0] + r["bbox"][2]) / 2 <= partial[2] and partial[1] <= (r["bbox"][1] + r["bbox"][3]) / 2 <= partial[3]) / len(xs)
    out = _zoom(ctx, page=ED3_PAGE, bbox=partial, expect_text="(X)", purpose="count")
    assert not out.refused, out.message
    cov = out.data["coverage"]
    after = cov["inside"] / cov["targets"]
    assert cov["targets"] == len(xs) and after > before, (before, after)
    assert after == 1.0 and cov["complete"] is True, (before, after)
    print(f"E11 golden: tags {len(xs)}, requested ratio {before:.3f}, widened ratio {after:.3f}, regions {len(out.data['bbox_used'])}, tiles {len(out.data['views'])}")


@golden
@needs_backup
def test_golden_anthropic_route_tiles_the_e19_and_e11_views_under_its_caps(stub_render):
    pdf = ELEC_PDF.read_bytes()
    inputs = LocalPdfInputs({DOC: pdf}, page_budget_s=8.0, doc_budget_s=120.0)
    index = h.WordsTextIndex(inputs)
    docs = h.LocalDocuments({DOC: pdf})
    for spec in (lp.OPENAI_IMAGES, lp.ANTHROPIC_IMAGES):
        stub_render.clear()
        ctx = _ctx({"text": index, "documents": docs}, image_spec=spec)
        e19 = _zoom(ctx, page=SCHEDULE_PAGE, bbox=[0.62, 0.05, 0.70, 0.066], anchor={"ref": None, "text": "L4", "occurrence": None}, purpose="read_text")
        e11 = _zoom(ctx, page=ED3_PAGE, bbox=[0.1, 0.45, 0.45, 0.65], expect_text="(X)", purpose="count")
        for out in (e19, e11):
            assert not out.refused, out.message
            assert out.data["image_provider"] == spec.provider
            assert all(spec.fits(v["w_px"], v["h_px"]) for v in out.data["views"])
            parts = tuple(lp.ImagePart(v["image"], v["w_px"], v["h_px"]) for v in out.views)
            assert lp.check_limits((lp.UserTurn(parts),), spec.provider).ok
        assert e11.data["coverage"]["complete"] is True
        print(
            f"{spec.provider}: E19 views {[(v['w_px'], v['h_px']) for v in e19.data['views']]} ppi {e19.data['ppi']}; "
            f"E11 views {[(v['w_px'], v['h_px']) for v in e11.data['views']]} ppi {e11.data['ppi']}"
        )


# ── provider image specs ─────────────────────────────────────────────────────


def test_anthropic_route_tiles_under_the_long_edge_and_pixel_caps(stub_render):
    facts = _facts()
    runs = _schedule_runs(facts)
    ctx = _ctx({"text": FakeTextIndex({(DOC, SCHEDULE_PAGE): runs}), "documents": FakeDocs()}, image_spec=lp.ANTHROPIC_IMAGES)
    wide = [0.05, 0.05, 0.95, 0.10]  # 37.8 in wide: 11,340 px at 300 ppi
    out = _zoom(ctx, page=SCHEDULE_PAGE, bbox=wide, purpose="verify_symbol")
    assert not out.refused, out.message
    views = out.data["views"]
    assert 1 < len(views) <= h.MAX_TILES
    assert all(lp.ANTHROPIC_IMAGES.fits(v["w_px"], v["h_px"]) for v in views)
    assert max(max(v["w_px"], v["h_px"]) for v in views) <= 2576
    parts = tuple(lp.ImagePart(v["image"], v["w_px"], v["h_px"]) for v in out.views)
    assert lp.check_limits((lp.UserTurn(parts),), "anthropic").ok
    # The same region on the OpenAI default fits its patch cap but not Anthropic's caps.
    openai = _zoom(_ctx({"text": FakeTextIndex({(DOC, SCHEDULE_PAGE): runs}), "documents": FakeDocs()}), page=SCHEDULE_PAGE, bbox=wide, purpose="verify_symbol")
    assert openai.data["image_provider"] == "openai"
    assert all(images.patches_for(v["w_px"], v["h_px"]) <= images.MAX_PATCHES_ORIGINAL for v in openai.data["views"])
    assert not all(lp.ANTHROPIC_IMAGES.fits(v["w_px"], v["h_px"]) for v in openai.data["views"])


def test_plan_tiles_for_matches_openai_and_respects_anthropic():
    assert h.plan_tiles_for(5000, 4000) == images.plan_tiles(5000, 4000)
    tiles = h.plan_tiles_for(5000, 4000, lp.ANTHROPIC_IMAGES)
    assert all(lp.ANTHROPIC_IMAGES.fits(x1 - x0, y1 - y0) for x0, y0, x1, y1 in tiles)
    assert min(t[0] for t in tiles) == 0 and max(t[2] for t in tiles) == 5000 and max(t[3] for t in tiles) == 4000
    assert h.plan_tiles_for(2000, 1500, lp.ANTHROPIC_IMAGES) == [(0, 0, 2000, 1500)]
    # A ppi that fits OpenAI in 4 tiles may need a lower rung on Anthropic.
    box = [[0.0, 0.0, 0.5, 0.5]]
    assert h.choose_ppi(box, "count", None, 42.0, 30.0, lp.ANTHROPIC_IMAGES) <= h.choose_ppi(box, "count", None, 42.0, 30.0)


def test_fit_png_downscales_only_what_does_not_fit():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (3583, 480), (255, 255, 255)).save(buf, "PNG")
    png = buf.getvalue()
    same = h.fit_png(png, 3583, 480, lp.OPENAI_IMAGES)
    assert same == (png, 3583, 480)
    small, w, hh = h.fit_png(png, 3583, 480, lp.ANTHROPIC_IMAGES)
    assert lp.ANTHROPIC_IMAGES.fits(w, hh) and Image.open(io.BytesIO(small)).size == (w, hh)
    with pytest.raises(ValueError):
        h.fit_png(b"not a png", 4000, 4000, lp.ANTHROPIC_IMAGES)


# ── WordsTextIndex.search scope ──────────────────────────────────────────────


def test_words_text_index_search_loads_requested_pages_before_any_runs_call():
    pdf = make_pdf([_tag_sheet(0), sheet((5.0, 5.0, 14, "L1"), w_in=24.0, h_in=18.0)])
    inputs = LocalPdfInputs({DOC: pdf}, page_budget_s=8.0, doc_budget_s=60.0)
    # Explicit scope, no DocumentAccess: the pages are loaded, not skipped.
    index = h.WordsTextIndex(inputs)
    out = index.search("L1", document_ids=[DOC], pages=[1, 2])
    assert out["total"] == 2 and out["pages_searched"] == 2 and out["complete"] is True
    # Open scope with a DocumentAccess: every page of every document.
    index2 = h.WordsTextIndex(inputs, documents=h.LocalDocuments({DOC: pdf}))
    out2 = index2.search("L1")
    assert out2["total"] == 2 and out2["complete"] is True
    # A page that cannot be loaded makes the result partial, never a silent 0.
    out3 = index2.search("L4", document_ids=[DOC], pages=[2, 9])
    assert out3["total"] == 0 and out3["complete"] is False
    assert [p["page"] for p in out3["pages_not_searched"]] == [9] and "never absent" in out3["warning"]
    # Nothing loadable and nothing enumerable: the typed unavailable result.
    assert h.WordsTextIndex(inputs).search("L1")["error"] == "unavailable"
    assert h.WordsTextIndex(inputs).search("L1", document_ids=["missing"], pages=[1])["error"] == "unavailable"
    # services_for_run hands the run's DocumentAccess to the index.
    svc = h.services_for_run(None, "run", pdfs={DOC: pdf}, inputs=inputs)
    if isinstance(svc.text, h.WordsTextIndex):
        assert svc.text.search("L1")["complete"] is True
