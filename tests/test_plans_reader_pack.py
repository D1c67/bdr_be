"""plans_reader_pack: document planning, group packing under the token and
file-byte budgets, and the pre-run cost estimate. Pure functions; no
network, no database."""

from __future__ import annotations

import pytest

from app.services import plans_reader_images as pri
from app.services import plans_reader_pack as pack

SHEET_42X30_TOKENS = pri.image_tokens(6510, 4650)  # 155 ppi, 29,784 patches


def test_plan_document_adaptive():
    doc = pack.plan_document("d1", "electrical_drawings", [(42, 30), (8.5, 11), (36, 24)])
    assert doc.kind == "drawing" and doc.page_count == 3
    p1, p2, p3 = doc.pages
    assert (p1.page, p1.ppi, p1.w_px, p1.h_px) == (1, 155, 6510, 4650)
    assert p1.est_tokens == SHEET_42X30_TOKENS == 35_741
    assert p1.tiles is None
    assert p2.ppi == 260 and (p2.w_px, p2.h_px) == (2210, 2860)
    assert p3.ppi == 188
    assert doc.est_tokens == sum(p.est_tokens for p in doc.pages)


def test_plan_document_tiles_260():
    doc = pack.plan_document("d1", "electrical_drawings", [(42, 30)], render_mode="tiles_260")
    page = doc.pages[0]
    assert page.ppi == 260 and (page.w_px, page.h_px) == (10_920, 7_800)
    assert page.tiles and len(page.tiles) >= 3
    assert page.est_tokens == sum(
        pri.image_tokens(x1 - x0, y1 - y0) for x0, y0, x1, y1 in page.tiles
    )
    # Tiles cost a multiple of one native image, as the doc measured.
    assert page.est_tokens > 2.5 * SHEET_42X30_TOKENS


def test_plan_document_rejects_unknown_mode_and_empty():
    with pytest.raises(ValueError):
        pack.plan_document("d1", "x", [(42, 30)], render_mode="jpeg")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        pack.plan_document("d1", "x", [])


def test_pack_respects_budget_and_order():
    docs = [
        pack.plan_document("a", "electrical_drawings", [(42, 30)] * 7),
        pack.plan_document("b", "electrical_drawings", [(42, 30)] * 4),
    ]
    groups = pack.pack_groups(docs, budget_tokens=240_000, ledger_tokens=40_000)
    room = 240_000 - 40_000 - 3_000
    per_group = room // SHEET_42X30_TOKENS  # 5 sheets
    assert per_group == 5
    assert [len(g.items) for g in groups] == [5, 5, 1]
    assert [g.group_index for g in groups] == [0, 1, 2]
    flat = [(i["document_id"], i["page"]) for g in groups for i in g.items]
    assert flat == [("a", p) for p in range(1, 8)] + [("b", p) for p in range(1, 5)]
    for g in groups:
        assert g.est_input_tokens <= 240_000
        assert g.est_input_tokens == 43_000 + len(g.items) * SHEET_42X30_TOKENS
        assert not g.oversize
        item = g.items[0]
        assert item["detail"] == "original" and item["kind"] == "image"
        assert item["ppi"] == 155 and item["tiles"] is None


def test_pack_groups_per_category_in_first_appearance_order():
    docs = [
        pack.plan_document("m", "mechanical_drawings", [(36, 24)]),
        pack.plan_document("e", "electrical_drawings", [(42, 30)]),
        pack.plan_document("m2", "mechanical_drawings", [(36, 24)]),
    ]
    groups = pack.pack_groups(docs, ledger_tokens=10_000)
    assert [(g.category, g.group_index) for g in groups] == [
        ("mechanical_drawings", 0),
        ("electrical_drawings", 0),
    ]
    assert [i["document_id"] for i in groups[0].items] == ["m", "m2"]


def test_pack_oversize_page_gets_its_own_group():
    docs = [
        pack.plan_document(
            "t", "electrical_drawings", [(42, 30), (42, 30), (42, 30)], render_mode="tiles_260"
        )
    ]
    # Room of 60k tokens: one tiled sheet (about 100k) never fits.
    groups = pack.pack_groups(docs, budget_tokens=100_000, ledger_tokens=37_000)
    assert len(groups) == 3
    assert all(g.oversize and len(g.items) == 1 for g in groups)
    tiles = groups[0].items[0]["tiles"]
    assert tiles and all(len(t) == 4 and all(0 <= v <= 1 for v in t) for t in tiles)
    assert tiles[0][0] == 0 and tiles[-1][2] == 1


def test_pack_file_docs_by_chunks_tokens_and_bytes():
    spec = pack.plan_file_document("s", "specifications", 400, 80 * 1024 * 1024)
    # Estimated chunks: 80 MB / 400 pages = 200 KB a page, 50 MB cap -> 256
    # pages, then the 220-page cap.
    assert [(c.start_page, c.end_page) for c in spec.chunks] == [(1, 220), (221, 400)]
    groups = pack.pack_groups([spec], ledger_tokens=40_000)
    room = 240_000 - 43_000
    for g in groups:
        assert g.est_input_tokens <= 240_000
        assert g.file_bytes <= 50 * 1024 * 1024
        assert all(i["kind"] == "file" and i["ppi"] is None for i in g.items)
        assert len(g.items) == sum(e - s + 1 for _, s, e, _ in g.file_ranges)
    pages = [i["page"] for g in groups for i in g.items]
    assert pages == list(range(1, 401))
    assert max(len(g.items) for g in groups) == room // pack.FILE_TOKENS_PER_PAGE


def test_pack_long_spec_fills_open_group_and_matches_real_cut():
    # 400 pages, 40k ledger: 179 pages a group. The estimated 220-page
    # chunks and a real cut at 179 pages both give 3 groups (179, 179, 42).
    est = pack.plan_file_document("s", "specifications", 400, 80 * 1024 * 1024)
    real = pack.plan_file_document(
        "s", "specifications", 400, 80 * 1024 * 1024,
        max_pages=pack.max_file_pages_for_budget(240_000, 40_000),
    )
    for doc in (est, real):
        groups = pack.pack_groups([doc], ledger_tokens=40_000)
        assert [len(g.items) for g in groups] == [179, 179, 42]
        # Pro-rated bytes add up exactly to the file size.
        assert sum(r[3] for g in groups for r in g.file_ranges) == 80 * 1024 * 1024
        for g in groups:
            assert g.est_input_tokens <= 240_000
    est_run = pack.estimate_run([est], ledger_tokens=40_000, input_per_m=10, output_per_m=50)
    real_run = pack.estimate_run([real], ledger_tokens=40_000, input_per_m=10, output_per_m=50)
    assert est_run["groups"] == real_run["groups"] == 3


def test_pack_short_file_after_drawings_is_kept_whole():
    docs = [
        pack.plan_document("d", "other", [(42, 30)] * 4),  # 142,964 tokens
        pack.plan_file_document("f", "other", 60, 6_000),  # 66,000 tokens
    ]
    groups = pack.pack_groups(docs, ledger_tokens=40_000)
    # 54k of room is left after the sheets; the 60-page file fits a fresh
    # group whole, so it is not spread across two.
    assert len(groups) == 2
    assert groups[1].file_ranges == [("f", 1, 60, 6_000)]


def test_plan_file_document_accepts_pdf_chunks_output():
    from pypdf import PdfWriter
    import io

    writer = PdfWriter()
    for _ in range(6):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    chunks = pri.pdf_chunks(buf.getvalue(), max_pages=4)
    assert isinstance(chunks[0][2], bytes)
    doc = pack.plan_file_document("s", "specifications", 6, len(buf.getvalue()), chunks=chunks)
    assert [(c.start_page, c.end_page, c.bytes) for c in doc.chunks] == [
        (s, e, len(b)) for s, e, b in chunks
    ]
    assert doc.est_tokens == 6 * pack.FILE_TOKENS_PER_PAGE


def test_pack_image_count_cap_closes_group():
    docs = [pack.plan_document("d", "other", [(8.5, 11)] * 10)]
    groups = pack.pack_groups(docs, ledger_tokens=0, max_images=8, ledger_images=4)
    assert [len(g.items) for g in groups] == [4, 4, 2]
    assert [g.images for g in groups] == [4, 4, 2]
    tiled = [pack.plan_document("t", "other", [(42, 30)], render_mode="tiles_260")]
    n_tiles = len(tiled[0].pages[0].tiles or [])
    [g] = pack.pack_groups(tiled, ledger_tokens=0, max_images=n_tiles - 1)
    assert g.oversize and g.images == n_tiles
    with pytest.raises(ValueError):
        pack.pack_groups(docs, ledger_tokens=0, max_images=5, ledger_images=5)


def test_pack_file_bytes_cap_closes_group():
    a = pack.plan_file_document("a", "specifications", 10, 30 * 1024 * 1024)
    b = pack.plan_file_document("b", "specifications", 10, 30 * 1024 * 1024)
    groups = pack.pack_groups([a, b], ledger_tokens=0)
    assert len(groups) == 2
    assert groups[0].file_ranges == [("a", 1, 10, 30 * 1024 * 1024)]
    assert groups[1].file_ranges == [("b", 1, 10, 30 * 1024 * 1024)]


def test_pack_real_chunks_and_oversized_chunk_errors():
    doc = pack.plan_file_document("s", "specifications", 30, 1000, chunks=[(1, 20, 600), (21, 30, 400)])
    groups = pack.pack_groups([doc], ledger_tokens=0)
    assert len(groups) == 1 and groups[0].file_bytes == 1000
    big = pack.plan_file_document("x", "specifications", 5, 0, chunks=[(1, 5, 60 * 1024 * 1024)])
    with pytest.raises(ValueError):
        pack.pack_groups([big], ledger_tokens=0)


def test_pack_mixed_drawings_and_files_keep_document_order():
    docs = [
        pack.plan_document("d", "other", [(8.5, 11)] * 2),
        pack.plan_file_document("f", "other", 3, 3000),
        pack.plan_document("d2", "other", [(8.5, 11)]),
    ]
    groups = pack.pack_groups(docs, ledger_tokens=0)
    assert len(groups) == 1
    assert [(i["document_id"], i["kind"]) for i in groups[0].items] == [
        ("d", "image"), ("d", "image"),
        ("f", "file"), ("f", "file"), ("f", "file"),
        ("d2", "image"),
    ]


def test_pack_rejects_budget_without_room():
    with pytest.raises(ValueError):
        pack.pack_groups([], budget_tokens=10_000, ledger_tokens=9_000)


def test_max_file_pages_for_budget():
    assert pack.max_file_pages_for_budget(240_000, 40_000) == 179


def test_estimate_run_totals():
    docs = [pack.plan_document("a", "electrical_drawings", [(42, 30)] * 7)]
    est = pack.estimate_run(
        docs, ledger_tokens=40_000, input_per_m=10.0, output_per_m=50.0,
    )
    assert est["groups"] == 2
    sub_in = 2 * 43_000 + 7 * SHEET_42X30_TOKENS
    assert est["sub_input_tokens"] == sub_in
    # Output scales with sheets: 5 x 2,500 and max(4,000, 2 x 2,500).
    assert est["sub_output_tokens"] == 12_500 + 5_000
    assert est["rollup_input_tokens"] == 17_500 + 3_000
    assert est["rollup_output_tokens"] == 17_500
    # Main reads ledger + prompt + the roll-up output, floored at 200k.
    assert est["main_input_tokens"] == 200_000
    assert est["input_tokens"] == sub_in + 20_500 + 200_000
    assert est["output_tokens"] == 17_500 + 17_500 + 30_000
    expected = (est["input_tokens"] * 10 + est["output_tokens"] * 50) / 1_000_000
    assert est["cost_usd"] == pytest.approx(expected, abs=0.01)
    assert est["per_category"]["electrical_drawings"]["groups"] == 2
    assert est["per_category"]["electrical_drawings"]["output_tokens"] == 17_500
    # Escalation ceiling: half the first pass, added only to cost_usd_max.
    assert est["escalation_max_input_tokens"] == round(sub_in * 0.5)
    assert est["escalation_max_output_tokens"] == round(17_500 * 0.5)
    esc = (round(sub_in * 0.5) * 10 + round(17_500 * 0.5) * 50) / 1_000_000
    assert est["cost_usd_max"] == pytest.approx(expected + esc, abs=0.01)


def test_estimate_run_flat_output_override_and_no_escalation():
    docs = [pack.plan_document("a", "electrical_drawings", [(42, 30)] * 7)]
    est = pack.estimate_run(
        docs, ledger_tokens=40_000, input_per_m=10.0, output_per_m=50.0,
        output_tokens_per_group=4_000, escalation_max_fraction=0,
    )
    assert est["sub_output_tokens"] == 8_000
    assert est["rollup_input_tokens"] == 8_000 + 3_000
    assert est["cost_usd_max"] == est["cost_usd"]
    with pytest.raises(ValueError):
        pack.estimate_run(docs, ledger_tokens=0, input_per_m=1, output_per_m=1,
                          escalation_max_fraction=1.5)


def test_estimate_run_main_input_grows_with_the_set():
    # Three categories of 100 sheets: each roll-up returns the output cap, so
    # main input (ledger + prompt + category outputs) passes the 200k floor
    # and the 272k long-context line, and is priced at the surcharge.
    docs = [
        pack.plan_document(cat, cat, [(42, 30)] * 100)
        for cat in ("electrical_drawings", "mechanical_drawings", "civil_drawings")
    ]
    est = pack.estimate_run(docs, ledger_tokens=40_000, input_per_m=10.0, output_per_m=50.0)
    assert est["groups"] == 60
    assert est["rollup_output_tokens"] == 3 * pack.MAX_OUTPUT_TOKENS_PER_CALL
    main_in = 43_000 + 3 * pack.MAX_OUTPUT_TOKENS_PER_CALL
    assert est["main_input_tokens"] == main_in
    small = pack.estimate_run(docs[:1], ledger_tokens=40_000, input_per_m=10.0, output_per_m=50.0)
    assert small["main_input_tokens"] == 200_000  # the floor
    assert pack._call_cost(main_in, 30_000, 10, 50, pack.LONG_CONTEXT_THRESHOLD) == pytest.approx(
        main_in * 20 / 1e6 + 30_000 * 75 / 1e6
    )


def test_group_output_tokens_bounds():
    g = pack.Group("specifications", 0, items=[{"kind": "file"}] * 179)
    assert pack.group_output_tokens(g) == 179 * 150
    one = pack.Group("electrical_drawings", 0, items=[{"kind": "image"}])
    assert pack.group_output_tokens(one) == pack.MIN_OUTPUT_TOKENS_PER_CALL
    many = pack.Group("electrical_drawings", 0, items=[{"kind": "image"}] * 100)
    assert pack.group_output_tokens(many) == pack.MAX_OUTPUT_TOKENS_PER_CALL


def test_estimate_run_long_context_surcharge_and_empty():
    est = pack.estimate_run(
        [pack.plan_document("a", "e", [(8.5, 11)])],
        ledger_tokens=0, input_per_m=10.0, output_per_m=50.0,
        main_overhead_tokens=300_000, main_output_tokens=0,
    )
    sub = (3_000 + pri.image_tokens(2210, 2860)) * 10 / 1e6 + 4_000 * 50 / 1e6
    main = 300_000 * 10 * 2 / 1e6
    assert est["cost_usd"] == pytest.approx(sub + main, abs=0.01)
    empty = pack.estimate_run([], ledger_tokens=0, input_per_m=10.0, output_per_m=50.0)
    assert empty["groups"] == 0 and empty["cost_usd"] == 0
