"""plans_reader_geometry (WS3 B1 and B4): the scale parser cases of spec 6
WS3, viewports from frames, the reduced-print check, the vector segment
index on a hand-built PDF, registration (identity frame, median solve over
the four rotations and the mirror, single anchor with the callout check,
chains) and the golden 7154 chain E2.01 -> ED2.01 (identity) -> E1.00
(single anchor) on the backup PDFs through WS1 (marked `golden`; skipped
when the backup is absent).

No network, no database, no model.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from app.services import plans_reader_geometry as G
from app.services.pagefacts import extract
from app.services.pagefacts.contracts import PageFacts, PageWords, Region, View, Word

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
ELEC_PDF = (
    BACKUP
    / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011"
    / "drawing/c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(not ELEC_PDF.exists(), reason="7154 backup PDF not on this machine")


# Scale parser -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "ft"),
    [
        ('1/4" = 1\'-0"', 4.0),
        ('1/16" = 1\'-0"', 16.0),
        ('3/64" = 1\'-0"', 21.3333),
        ('1-1/2" = 1\'-0"', 0.6667),
        ('1 1/2" = 1\'-0"', 0.6667),
        ('3" = 1\'-0"', 0.3333),
        ('1/8"=1\'', 8.0),
        ("1/8 IN = 1 FT", 8.0),
        ('SCALE: 1/4" = 1\'-0"', 4.0),
    ],
)
def test_architectural_scales(text, ft):
    parsed = G.parse_scale_text(text)
    assert parsed is not None and parsed.kind == "architectural"
    assert parsed.ft_per_in == pytest.approx(ft, abs=1e-4) and parsed.nts is False
    assert G.ft_per_in(text) == pytest.approx(ft, abs=1e-4)


@pytest.mark.parametrize(
    ("text", "ft"),
    [("1\" = 20'", 20.0), ('1" = 100 FT', 100.0), ('1"=30\'-0"', 30.0), ("1 IN = 50 FEET", 50.0)],
)
def test_engineering_scales(text, ft):
    parsed = G.parse_scale_text(text)
    assert parsed is not None and parsed.kind == "engineering" and parsed.ft_per_in == ft


def test_ratio_only_with_a_scale_word():
    assert G.parse_scale_text("SCALE: 1:48").ft_per_in == 4.0
    assert G.parse_scale_text("SCALE 1:100").ft_per_in == pytest.approx(8.3333, abs=1e-4)
    assert G.parse_scale_text("SCALE 1:20").kind == "ratio"
    assert G.parse_scale_text("1:48") is None
    assert G.parse_scale_text("DETAIL 1:48") is None


@pytest.mark.parametrize("text", ["1:36:22 PM", "SCALE 1:30 PM", "10:30", "SCALE 1:36:22", "PLOTTED 6/4/2026 1:36:22 PM"])
def test_timestamps_are_rejected(text):
    assert G.parse_scale_text(text) is None


@pytest.mark.parametrize(
    "text",
    ["N.T.S.", "NTS", "N T S", "NOT TO SCALE", "NO SCALE", "SCALE: NONE", "SCALE: AS NOTED", "SCALE AS SHOWN"],
)
def test_nts_variants_give_none_ft_per_in(text):
    parsed = G.parse_scale_text(text)
    assert parsed is not None and parsed.nts is True and parsed.ft_per_in is None and parsed.kind == "nts"
    assert G.ft_per_in(text) is None


def test_bare_none_and_as_noted_need_the_scale_word():
    assert G.parse_scale_text("NONE") is None
    assert G.parse_scale_text("AS NOTED") is None
    assert G.parse_scale_text("") is None and G.parse_scale_text(None) is None
    assert G.parse_scale_text("ELECTRICAL OVERALL POWER PLAN") is None
    assert G.parse_scale_text("6/4/2026") is None


# Transforms -----------------------------------------------------------------------------------


def test_transform_compose_and_inverse_are_exact_for_right_angles():
    rng = random.Random(7)
    for _ in range(300):
        a = G.Transform(k=rng.choice([0.25, 1.0, 4.0]), rot=rng.choice(G.ROTATIONS), mirror=rng.random() < 0.5, tx=rng.uniform(-9, 9), ty=rng.uniform(-9, 9))
        b = G.Transform(k=rng.choice([0.25, 1.0, 4.0]), rot=rng.choice(G.ROTATIONS), mirror=rng.random() < 0.5, tx=rng.uniform(-9, 9), ty=rng.uniform(-9, 9))
        p = (rng.uniform(0, 42), rng.uniform(0, 30))
        assert math.dist(a.compose(b).apply(*p), a.apply(*b.apply(*p))) < 1e-6
        inv = G._inverse(a)
        assert math.dist(inv.apply(*a.apply(*p)), p) < 1e-6
    assert G.Transform().is_identity()
    assert G.Transform(rot=90).apply(1.0, 0.0) == (0.0, 1.0)


# Tokens and registration ------------------------------------------------------------------


def _tokens(key: str, words: list[tuple[str, float, float]], *, ft: float = 4.0, w_in: float = 42.0, h_in: float = 30.0) -> G.SheetTokens:
    toks = [G.Token(text=t, x_in=x, y_in=y, h_pt=10.0) for t, x, y in words]
    xs = [t.x_in for t in toks]
    ys = [t.y_in for t in toks]
    return G.SheetTokens(key, w_in, h_in, ft, toks, [min(xs), min(ys), max(xs), max(ys)] if toks else None)


ROOMS = [("PHARMACY", 10.0, 12.0), ("BREAKROOM", 18.5, 9.0), ("CONSULT", 22.0, 16.0), ("STORAGE", 6.0, 20.0), ("OFFICE", 14.0, 24.0), ("VESTIBULE", 27.0, 21.0), ("CORRIDOR", 20.0, 27.0)]


def test_identity_frame_needs_three_identical_tokens_and_half_of_the_common_ones():
    src = _tokens("A", ROOMS + [("(E)", 5.0, 5.0), ("(E)", 6.0, 5.0)])
    dst = _tokens("B", ROOMS[:4] + [("(E)", 5.0, 5.0), ("(E)", 6.0, 5.0), ("TYP", 1.0, 1.0)])
    ok, identical, common = G.identity_frame(src, dst)
    assert ok and identical == 6 and common == 6
    reg = G.register_sheets(src, dst)
    assert reg.method == "identity_frame" and reg.confidence == 1.0 and reg.transform.is_identity()
    # Same words, moved: common but not identical -> no identity frame.
    moved = _tokens("C", [(t, x + 3.0, y) for t, x, y in ROOMS])
    ok, identical, common = G.identity_frame(src, moved)
    assert not ok and identical == 0 and common == len(ROOMS)
    # Two identical tokens are not enough.
    two = _tokens("D", ROOMS[:2] + [("ELSEWHERE", 30.0, 2.0)])
    assert G.identity_frame(src, two)[0] is False


@pytest.mark.parametrize("rot", G.ROTATIONS)
@pytest.mark.parametrize("mirror", [False, True])
def test_median_solve_recovers_every_rotation_and_the_mirror(rot, mirror):
    truth = G.Transform(k=1.0, rot=rot, mirror=mirror, tx=5.5, ty=-2.25)
    src = _tokens("S", ROOMS)
    dst_words = [(t, *truth.apply(x, y)) for t, x, y in ROOMS]
    dst = _tokens("T", dst_words + [("NOISE", 3.3, 3.3)])
    reg = G.register_sheets(src, dst)
    assert reg.method == "median" and reg.confidence == 0.9 and reg.residual_in < 1e-6
    assert reg.transform.rot == rot and reg.transform.mirror == mirror
    for t, x, y in ROOMS:
        assert math.dist(reg.transform.apply(x, y), truth.apply(x, y)) < 1e-6
    assert ("mirrored" in reg.flags) == mirror


def test_median_solve_fixes_k_from_the_scale_ratio_or_estimates_it():
    truth = G.Transform(k=0.25, rot=90, tx=12.0, ty=4.0)
    src = _tokens("S", ROOMS, ft=4.0)
    dst = _tokens("T", [(t, *truth.apply(x, y)) for t, x, y in ROOMS], ft=16.0)
    reg = G.register_sheets(src, dst)
    assert reg.method == "median" and reg.transform.k == 0.25 and "k_estimated" not in reg.flags
    unknown = _tokens("U", [(t, *truth.apply(x, y)) for t, x, y in ROOMS], ft=None)
    reg2 = G.register_sheets(src, unknown)
    assert reg2.method == "median" and reg2.transform.k == pytest.approx(0.25, abs=1e-6)
    assert "k_estimated" in reg2.flags and reg2.transform.rot == 90


def test_median_solve_keeps_the_lowest_residual_and_refuses_a_bad_fit():
    src = _tokens("S", ROOMS)
    rng = random.Random(3)
    scrambled = _tokens("T", [(t, rng.uniform(0, 40), rng.uniform(0, 28)) for t, _x, _y in ROOMS])
    reg = G.register_sheets(scrambled, src, allow_single_anchor=False)
    assert reg.method == "none" and reg.transform is None and reg.n_tokens == len(ROOMS)


def test_single_anchor_registration_and_callout_check():
    k = 0.25
    src_words = ROOMS[:2] + [("ADMIN.", 19.3, 27.0), ("ASSISTANT", 19.5, 27.3)]
    src = _tokens("ED2.01", src_words, ft=4.0)
    truth = G.Transform(k=k, tx=9.36, ty=11.68)
    ax, ay = truth.apply(19.4, 27.15)
    dst_words = [("ADMIN.", ax - 0.03, ay - 0.04), ("ASSISTANT", ax + 0.03, ay + 0.04), ("KITCHEN", 30.0, 5.0), ("LOBBY", 33.0, 7.0)]
    dst = _tokens("E1.00", dst_words, ft=16.0)
    reg = G.register_sheets(src, dst)
    assert reg.method == "single_anchor" and reg.anchor == "ADMIN. ASSISTANT"
    assert reg.transform.k == k and reg.transform.rot == 0
    assert math.dist(reg.transform.apply(10.0, 12.0), truth.apply(10.0, 12.0)) < 0.01
    assert reg.confidence == 0.5 and "callout_unchecked" in reg.flags
    # A callout rectangle around the mapped content confirms it; the confidence stays 0.5 (spec 6 WS3).
    mapped = reg.transform.apply_box(src.content_bbox_in)
    rect = [(mapped[0] - 0.5) / 42.0, (mapped[1] - 0.5) / 30.0, (mapped[2] + 0.5) / 42.0, (mapped[3] + 0.5) / 30.0]
    checked = G.register_sheets(src, dst, dst_rectangles=[rect])
    assert checked.confidence == 0.5 and "callout_checked" in checked.flags
    assert checked.callout["center_inside"] and checked.callout["overlap"] >= 0.5
    # A rectangle elsewhere is a mismatch (0.3).
    off = G.register_sheets(src, dst, dst_rectangles=[[0.8, 0.8, 0.95, 0.95]])
    assert off.confidence == 0.3 and "callout_mismatch" in off.flags
    # Without a scale on either sheet no single anchor is possible.
    assert G.register_sheets(_tokens("X", src_words, ft=None), dst).method == "none"


def test_chain_through_an_identity_frame():
    a = _tokens("E2.01", ROOMS + [("(E)", 5.0, 5.0), ("(E)", 6.0, 5.0)], ft=4.0)
    b = _tokens("ED2.01", ROOMS[:3] + [("(E)", 5.0, 5.0), ("(E)", 6.0, 5.0), ("ADMIN.", 19.3, 27.0), ("ASSISTANT", 19.5, 27.3)], ft=4.0)
    truth = G.Transform(k=0.25, tx=9.36, ty=11.68)
    ax, ay = truth.apply(19.4, 27.15)
    c = _tokens("E1.00", [("ADMIN.", ax - 0.03, ay - 0.04), ("ASSISTANT", ax + 0.03, ay + 0.04), ("LOBBY", 33.0, 7.0)], ft=16.0)
    r1 = G.register_sheets(a, b)
    r2 = G.register_sheets(b, c)
    assert r1.method == "identity_frame" and r2.method == "single_anchor"
    chain = G.chain_registrations([r1, r2], "E2.01", "E1.00")
    assert chain is not None and chain.method == "chain" and chain.chain == ["E2.01", "ED2.01", "E1.00"]
    assert chain.confidence == 0.5 and chain.transform.k == 0.25
    assert math.dist(chain.transform.apply(10.0, 12.0), truth.apply(10.0, 12.0)) < 0.01
    # Reverse direction uses the inverse; a missing path is None.
    back = G.chain_registrations([r1, r2], "E1.00", "E2.01")
    assert back is not None and math.dist(back.transform.apply(*truth.apply(10.0, 12.0)), (10.0, 12.0)) < 0.01
    assert G.chain_registrations([r1], "E2.01", "E1.00") is None
    assert G.chain_registrations([r1, r2], "E2.01", "ED2.01").method == "identity_frame"


def _facts(page: int, views: list[View], regions: list[Region], *, w_in: float = 42.0, h_in: float = 30.0, primary: float | None = None, conflict: bool = False, number: str = "E2.01") -> PageFacts:
    return PageFacts(page=page, w_in=w_in, h_in=h_in, rotation=0, text_class="cad_text", sheet_number=number, roles=["plan"], page_role="plan", views=views, regions=regions, primary_ft_per_in=primary, scale_conflict=conflict)


def _word(text: str, x_in: float, y_in: float, *, r: int = 0, w_in: float = 42.0, h_in: float = 30.0) -> Word:
    return Word(t=text, b=[x_in / w_in - 0.005, y_in / h_in - 0.003, x_in / w_in + 0.005, y_in / h_in + 0.003], h=10.0, r=r)


def test_sheet_tokens_drop_title_block_margin_numbers_dates_and_view_titles():
    regions = [Region("title_block", [0.9, 0.0, 1.0, 1.0]), Region("keynotes", [0.82, 0.4, 0.9, 0.5]), Region("plan_area", [0.0, 0.0, 0.9, 1.0])]
    views = [View("v1", "OVERALL POWER PLAN", "1", '1/4" = 1\'-0"', 4.0, "plan")]
    facts = _facts(9, views, regions)
    words = [
        _word("PHARMACY", 10.0, 12.0),
        _word("UMC", 40.0, 5.0),  # title block
        _word("KEYNOTE", 36.0, 13.5),  # keynotes region
        _word("STAMP", 0.4, 20.0),  # margin
        _word("ROTATED", 12.0, 12.0, r=90),
        _word("12", 11.0, 12.0),
        _word("6/4/2026", 11.0, 13.0),
        _word("1:36:22", 11.0, 14.0),
        _word("OVERALL", 15.0, 28.5),  # view title word
        _word("1/4\"", 15.0, 29.0),  # scale text
        _word("(E)", 20.0, 20.0),
    ]
    toks = G.sheet_tokens(facts, words, sheet_key="E2.01")
    assert sorted(t.text for t in toks.tokens) == ["(E)", "PHARMACY"]
    assert toks.ft_per_in == 4.0 and toks.sheet_key == "E2.01" and toks.content_bbox_in is not None
    assert toks.tokens[0].x_in == pytest.approx(10.0, abs=0.01) and toks.tokens[0].y_in == pytest.approx(12.0, abs=0.01)


# Viewports and scales -------------------------------------------------------------------------


def test_detect_viewports_assigns_frames_and_skips_examples_and_legend_views():
    regions = [Region("title_block", [0.9, 0.0, 1.0, 1.0]), Region("legend", [0.0, 0.0, 0.3, 0.3]), Region("plan_area", [0.0, 0.0, 0.9, 1.0])]
    views = [
        View("v1", "ENLARGED POWER PLAN", "1", '1/4" = 1\'-0"', 4.0, "enlarged_plan", anchor=[0.12, 0.93, 0.15, 0.94]),
        View("v2", "OVERALL POWER PLAN", "2", '1/16" = 1\'-0"', 16.0, "plan", anchor=[0.55, 0.93, 0.58, 0.94]),
        View("v3", None, None, '1/8" = 1\'-0"', 8.0, "other", anchor=[0.1, 0.1, 0.15, 0.11], example=True),
        View("v4", None, None, '1/8" = 1\'-0"', 8.0, "other", anchor=[0.2, 0.2, 0.25, 0.21]),  # inside the legend
    ]
    frames = [[0.03, 0.4, 0.4, 0.9], [0.45, 0.35, 0.85, 0.9], [0.02, 0.02, 0.9, 0.98]]  # third = sheet border
    facts = _facts(3, views, regions)
    vps = {vp.view_id: vp for vp in G.detect_viewports(facts, frames=frames)}
    assert vps["v1"].region == [0.03, 0.4, 0.4, 0.9] and vps["v1"].source == "frame"
    assert vps["v2"].region == [0.45, 0.35, 0.85, 0.9]
    assert vps["v3"].skipped == "legend_example" and vps["v3"].region is None
    assert vps["v4"].skipped == "in_legend" and vps["v4"].region is None
    scales = G.scales_for_page(facts, frames=frames)
    assert scales.conflict is False and scales.primary_ft_per_in == 16.0  # framed views disagree: the largest plan frame
    assert scales.scale_at(0.2, 0.6) == (4.0, vps["v1"]) and scales.scale_at(0.6, 0.6)[0] == 16.0
    assert scales.nts is False and any("skipped" in n for n in scales.notes)
    # Without frames the two scales conflict and the plan view region stays None.
    bare = G.scales_for_page(facts, frames=[])
    assert bare.conflict is True and bare.scale_at(0.2, 0.6) == (None, None)


def test_single_plan_view_owns_the_plan_area_and_nts_pages_have_no_scale():
    regions = [Region("title_block", [0.9, 0.0, 1.0, 1.0]), Region("plan_area", [0.0, 0.0, 0.9, 1.0])]
    one = _facts(9, [View("v1", "OVERALL POWER PLAN - NEW WORK", "1", '1/4" = 1\'-0"', 4.0, "plan", anchor=[0.36, 0.96, 0.37, 0.97])], regions, primary=4.0)
    scales = G.scales_for_page(one, frames=[[0.02, 0.02, 0.9, 0.98]])
    assert scales.primary_ft_per_in == 4.0 and scales.viewports[0].source == "plan_area"
    assert scales.viewports[0].region == [0.0, 0.0, 0.9, 1.0] and scales.scale_at(0.5, 0.5)[0] == 4.0
    nts = _facts(3, [View("v1", "SINGLE LINE DIAGRAM", "1", "NO SCALE", None, "diagram", anchor=[0.4, 0.93, 0.42, 0.94], nts=True)], regions, number="E0.02")
    s = G.scales_for_page(nts, frames=[])
    assert s.primary_ft_per_in is None and s.nts is True and s.conflict is False
    assert s.scale_at(0.5, 0.5) == (None, None)


def test_reduced_print_check_flags_half_size_sheets_and_cues_without_scaling():
    regions = [Region("plan_area", [0.0, 0.0, 1.0, 1.0])]
    full = _facts(1, [], regions, w_in=42.0, h_in=30.0, primary=4.0)
    assert G.reduced_print_check(full) == {"suspected": False, "reasons": [], "page_size_in": [42.0, 30.0], "half_of": None, "auto_scaled": False}
    half = _facts(1, [], regions, w_in=21.0, h_in=15.0, primary=4.0)
    out = G.reduced_print_check(half)
    assert out["suspected"] and out["half_of"] == "ARCH E1" and out["auto_scaled"] is False
    words = PageWords(page_w_in=42.0, page_h_in=30.0, rotation=0, words=[], lines=[], text_class="cad_text")
    from app.services.pagefacts.contracts import Line

    words.lines.append(Line(t="IF THIS BAR DOES NOT MEASURE 1 INCH THE DRAWING IS REDUCED", b=[0.1, 0.9, 0.3, 0.91], w=[0, 0], h=6.0))
    cued = G.reduced_print_check(full, words)
    assert cued["suspected"] and cued["reasons"][0].startswith("text cue")
    scales = G.scales_for_page(half, frames=[], words=None)
    assert scales.primary_ft_per_in == 4.0 and scales.reduced_print["suspected"]  # never auto-scaled


# Vector segments ------------------------------------------------------------------------------


def _line_pdf(w_in: float, h_in: float, lines: list[tuple[float, float, float, float]], rects: list[tuple[float, float, float, float]] = ()) -> bytes:
    """A one-page PDF with stroked lines and rectangles given in displayed
    inches from the top-left (no rotation, no crop offset)."""
    pt = 72.0
    ops: list[str] = ["0.5 w"]
    for x0, y0, x1, y1 in lines:
        ops.append(f"{x0 * pt:.2f} {(h_in - y0) * pt:.2f} m {x1 * pt:.2f} {(h_in - y1) * pt:.2f} l S")
    for x0, y0, x1, y1 in rects:
        ops.append(f"{x0 * pt:.2f} {(h_in - y1) * pt:.2f} {(x1 - x0) * pt:.2f} {(y1 - y0) * pt:.2f} re S")
    content = "\n".join(ops).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w_in * pt:.2f} {h_in * pt:.2f}] /Contents 4 0 R >>".encode(),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_index_segments_reads_lines_rectangles_and_dashes():
    pdf = _line_pdf(
        42.0,
        30.0,
        lines=[(2.0, 5.0, 10.0, 5.0), (10.0, 5.0, 10.0, 12.0), (10.0, 12.0, 2.0, 12.0), (2.0, 12.0, 2.0, 5.0)]
        + [(20.0 + i * 0.5, 20.0, 20.0 + i * 0.5 + 0.4, 20.0) for i in range(10)]  # dashed line 20..24.9
        + [(30.0, 2.0, 33.0, 4.0)],  # a diagonal
        rects=[(14.0, 14.0, 20.0, 19.0)],
    )
    idx = G.index_segments(pdf, 0)
    assert idx.page == 1 and idx.w_in == 42.0 and idx.h_in == 30.0 and not idx.truncated
    assert len(idx) == 4 + 10 + 1 + 4 and idx.paths == 16
    hs, vs = idx.axis_aligned(1.0)
    assert any(abs(s.length_in(42.0, 30.0) - 4.9) < 0.05 for s in hs)  # dashes merged
    rects = idx.rectangles(min_w_in=3.0, min_h_in=3.0)
    assert len(rects) == 2
    first, second = sorted(rects)
    assert first == pytest.approx([2 / 42, 5 / 30, 10 / 42, 12 / 30], abs=1e-3)
    assert second == pytest.approx([14 / 42, 14 / 30, 20 / 42, 19 / 30], abs=1e-3)
    near = idx.near(30.0 / 42.0, 2.0 / 30.0, 0.2)
    assert len(near) == 1 and near[0].x1 == pytest.approx(33 / 42, abs=1e-3)
    with pytest.raises(ValueError):
        G.index_segments(pdf, 3)


def test_segment_cache_indexes_a_page_once_per_run():
    calls: list[int] = []

    def loader(page: int) -> G.SegmentIndex:
        calls.append(page)
        return G.SegmentIndex(page=page, w_in=42.0, h_in=30.0, segments=[G.Segment(0, 0, 0.1, 0)])

    cache = G.SegmentCache()
    a = cache.get("doc1", 3, loader)
    b = cache.get("doc1", 3, loader)
    assert a is b and calls == [3]

    def broken(page: int) -> G.SegmentIndex:
        raise RuntimeError("no vectors")

    assert cache.get("doc1", 4, broken) is None and cache.get("doc1", 4, loader) is None
    assert cache.stats() == {"pages": 2, "segments": 1, "failed": 1}


# Golden 7154 ($0, the backup PDF through WS1) ----------------------------------------------


@pytest.fixture(scope="module")
def elec():
    if not ELEC_PDF.exists():
        pytest.skip("7154 backup PDF not on this machine")
    pdf = ELEC_PDF.read_bytes()
    facts = extract.extract_document(pdf, label="electrical_drawings", page_budget_s=4.0, doc_budget_s=240.0)
    return pdf, facts


@golden
@needs_backup
def test_7154_scales_per_view(elec):
    pdf, facts = elec
    by = {p.sheet_number: p for p in facts.pages}
    for number, ft in (("E2.01", 4.0), ("ED2.01", 4.0), ("E3.01", 4.0), ("ED3.01", 4.0), ("E1.00", 16.0)):
        page = by[number]
        scales = G.scales_for_page(page, G.index_segments(pdf, page.page - 1))
        assert scales.primary_ft_per_in == ft and not scales.conflict and not scales.nts, number
        assert scales.scale_at(0.5, 0.5)[0] == ft
        assert scales.reduced_print["suspected"] is False
    nts = G.scales_for_page(by["E0.02"], G.index_segments(pdf, by["E0.02"].page - 1))
    assert nts.primary_ft_per_in is None and nts.nts is True and nts.scale_at(0.5, 0.5) == (None, None)
    legend = G.scales_for_page(by["E0.00"], frames=[])
    assert legend.primary_ft_per_in is None and legend.viewports[0].skipped == "legend_example"


@golden
@needs_backup
def test_7154_chain_e201_ed201_identity_then_ed201_e100_single_anchor(elec):
    pdf, facts = elec
    by = {p.sheet_number: p for p in facts.pages}
    words = extract.extract_words(pdf, pages=[by[n].page for n in ("E2.01", "ED2.01", "E1.00")])
    toks = {n: G.sheet_tokens(by[n], words[by[n].page], sheet_key=n) for n in ("E2.01", "ED2.01", "E1.00")}
    r1 = G.register_sheets(toks["E2.01"], toks["ED2.01"])
    print(f"WS3 measured: E2.01->ED2.01 {r1.method} identical {r1.n_identical} of common {r1.n_common}")
    assert r1.method == "identity_frame" and r1.confidence == 1.0
    assert r1.n_identical >= 3 and r1.n_identical >= 0.5 * r1.n_common
    rects = G.index_segments(pdf, by["E1.00"].page - 1).rectangles(min_w_in=3.0, min_h_in=3.0)
    r2 = G.register_sheets(toks["ED2.01"], toks["E1.00"], dst_rectangles=rects)
    print(f"WS3 measured: ED2.01->E1.00 {r2.method} anchor {r2.anchor} k {r2.transform.k} callout {r2.callout}")
    assert r2.method == "single_anchor" and r2.transform.k == 0.25 and r2.transform.rot == 0
    assert r2.anchor == "ADMIIN. ASSISTANT" and r2.confidence == 0.5 and "callout_checked" in r2.flags
    assert r2.callout["overlap"] >= 0.5 and r2.callout["center_inside"]
    # The callout box on E1.00 is about 7.75 x 9.68 in; the mapped ED2.01 content lies inside it.
    rw = r2.callout["rect_in"][2] - r2.callout["rect_in"][0]
    rh = r2.callout["rect_in"][3] - r2.callout["rect_in"][1]
    assert 7.0 <= rw <= 8.5 and 9.0 <= rh <= 10.5
    chain = G.chain_registrations([r1, r2], "E2.01", "E1.00")
    assert chain is not None and chain.method == "chain" and chain.chain == ["E2.01", "ED2.01", "E1.00"]
    assert chain.transform.k == 0.25 and chain.confidence == 0.5
    # A point on E2.01 lands where the same point of ED2.01 lands (identity first).
    assert chain.transform.apply(20.0, 15.0) == pytest.approx(r2.transform.apply(20.0, 15.0), abs=1e-6)
