"""plans_reader_images: patch math, native ppi, tiles, regions, rendering,
PDF chunking and request parts. Numbers come from docs/PLANS_READER.md
section 1 (measured 2026-10-17). No network, no database."""

from __future__ import annotations

import base64
import glob
import io
import os

import pytest
from PIL import Image

from app.services import plans_reader_images as pri


def _pdf(sizes_pt: list[tuple[float, float]], *, content: bytes | None = None,
         filler_bytes: int = 0) -> bytes:
    """A PDF with one page per size. `content` is drawn on every page;
    `filler_bytes` adds incompressible comment text to each page so byte
    sizes are predictable for chunking tests."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    for i, (w, h) in enumerate(sizes_pt):
        page = writer.add_blank_page(width=w, height=h)
        data = content or b""
        if filler_bytes:
            data += b"\n% " + os.urandom(filler_bytes // 2).hex().encode() + f" {i}\n".encode()
        if data:
            stream = DecodedStreamObject()
            stream.set_data(data)
            page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# Patch and token math ---------------------------------------------------------


def test_patch_math_matches_measured_42x30_at_150():
    w, h = pri.px_for_inches(42, 150), pri.px_for_inches(30, 150)
    assert (w, h) == (6300, 4500)
    assert pri.patches_for(w, h) == 27_777
    assert pri.tokens_for_patches(27_777) == 33_333
    assert pri.image_tokens(6300, 4500) == 33_333


def test_patches_alias_matches_doc_name():
    assert pri.patches is pri.patches_for


def test_patch_math_edges():
    assert pri.patches_for(32, 32) == 1
    assert pri.patches_for(33, 32) == 2
    assert pri.patches_for(0, 100) == 0
    assert pri.tokens_for_patches(0) == 0
    assert pri.tokens_for_patches(1) == 2  # ceil(1.2)
    assert pri.tokens_for_patches(5) == 6  # exactly 6.0, no float hair
    # 260 ppi full 42x30 sheet from the doc: 10,920 x 7,800 = 83,448 patches.
    assert pri.patches_for(10_920, 7_800) == 83_448


def test_native_ppi_for_common_sheets():
    assert 153 <= pri.native_ppi_for(42, 30) <= 155
    assert 186 <= pri.native_ppi_for(36, 24) <= 188
    assert 131 <= pri.native_ppi_for(48, 36) <= 133
    assert pri.native_ppi_for(8.5, 11) == 260
    assert pri.native_ppi_for(11, 17) == 260


@pytest.mark.parametrize("w,h", [(42, 30), (36, 24), (48, 36), (30, 42), (24, 18)])
def test_native_ppi_is_the_largest_that_fits(w, h):
    ppi = pri.native_ppi_for(w, h)
    assert pri.patches_for(pri.px_for_inches(w, ppi), pri.px_for_inches(h, ppi)) <= 30_000
    if ppi < 260:
        nxt = ppi + 1
        assert pri.patches_for(pri.px_for_inches(w, nxt), pri.px_for_inches(h, nxt)) > 30_000


def test_native_ppi_for_rejects_bad_input():
    with pytest.raises(ValueError):
        pri.native_ppi_for(0, 30)
    with pytest.raises(ValueError):
        pri.native_ppi_for(50_000, 50_000)  # over the cap even at 1 ppi


# Tiles and regions ------------------------------------------------------------


def _covers(boxes, w, h) -> bool:
    img = Image.new("1", (w // 10, h // 10), 0)
    for x0, y0, x1, y1 in boxes:
        img.paste(1, (x0 // 10, y0 // 10, -(-x1 // 10), -(-y1 // 10)))
    return all(img.get_flattened_data())


def test_plan_tiles_full_42x30_at_260():
    w, h = 10_920, 7_800
    tiles = pri.plan_tiles(w, h)
    # Fewest grid tiles under 30,000 patches each: three full-height strips
    # of about 3,730 x 7,800 px (28,548 patches with the 5% overlap); 2x1 is
    # over the cap.
    assert len(tiles) == 3
    assert all(pri.patches_for(x1 - x0, y1 - y0) <= 30_000 for x0, y0, x1, y1 in tiles)
    assert _covers(tiles, w, h)
    # Overlap: neighbours share pixels.
    assert tiles[0][2] > tiles[1][0]
    assert tiles[1][2] > tiles[2][0]
    grid = pri.plan_tiles(w, h, max_patches=24_000)
    assert len(grid) == 4
    assert grid[0][3] > grid[2][1]


def test_plan_tiles_six_under_a_tighter_cap():
    # The 3x2 grid the doc measured (112,685 tokens) comes out when each tile
    # must stay near 16k patches.
    tiles = pri.plan_tiles(10_920, 7_800, max_patches=17_000)
    assert len(tiles) == 6
    assert all(pri.patches_for(x1 - x0, y1 - y0) <= 17_000 for x0, y0, x1, y1 in tiles)
    assert _covers(tiles, 10_920, 7_800)


def test_plan_tiles_small_image_is_one_tile():
    assert pri.plan_tiles(2_210, 2_860) == [(0, 0, 2_210, 2_860)]


def test_plan_tiles_other_sizes_stay_under_cap():
    for w_in, h_in in ((36, 24), (48, 36)):
        w, h = pri.px_for_inches(w_in, 260), pri.px_for_inches(h_in, 260)
        tiles = pri.plan_tiles(w, h)
        assert all(pri.patches_for(x1 - x0, y1 - y0) <= 30_000 for x0, y0, x1, y1 in tiles)
        assert _covers(tiles, w, h)


def test_region_to_box_forms():
    assert pri.region_to_box("whole", 900, 600) == (0, 0, 900, 600)
    assert pri.region_to_box("r1c1", 900, 600) == (0, 0, 300, 200)
    assert pri.region_to_box("R3C3", 900, 600) == (600, 400, 900, 600)
    assert pri.region_to_box("r2c3", 900, 600) == (600, 200, 900, 400)
    assert pri.region_to_box("0.5, 0.25, 1, 0.75", 1000, 800) == (500, 200, 1000, 600)
    assert pri.region_to_box("[0,0,0.1,0.1]", 1000, 800) == (0, 0, 100, 80)
    for bad in ("r4c1", "left half", "0.5,0.5,0.4,0.9", "1,2,3", ""):
        with pytest.raises(ValueError):
            pri.region_to_box(bad, 1000, 800)


def test_region_error_message_is_short_and_ascii():
    hostile = "<script>" + "x" * 500 + "\u00e9"
    with pytest.raises(ValueError) as err:
        pri.region_to_box(hostile, 1000, 800)
    msg = str(err.value)
    assert len(msg) < 80
    assert msg.isascii()
    assert "x" * 41 not in msg


def test_fraction_round_trip_and_region_tiles():
    box = (1200, 300, 5600, 4100)
    frac = pri.box_to_fractions(box, 10_920, 7_800)
    assert pri.box_from_fractions(tuple(frac), 10_920, 7_800) == box
    tiles = pri.plan_region_tiles(10_920, 7_800, (0, 0, 10_920, 7_800))
    assert tiles == pri.plan_tiles(10_920, 7_800)
    sub = pri.plan_region_tiles(10_920, 7_800, (3640, 2600, 7280, 5200))
    assert sub == [(3640, 2600, 7280, 5200)]


# Rendering --------------------------------------------------------------------


def test_render_page_size_matches_planner_and_crops_exactly():
    # 42x30 in sheet with a black 1 in square at the top-left corner.
    w_pt, h_pt = 3024.0, 2160.0
    square = f"0 0 0 rg 0 {h_pt - 72} 72 72 re f".encode()
    pdf = _pdf([(w_pt, h_pt), (612, 792)], content=square)
    assert pri.page_count(pdf) == 2
    sizes = pri.page_sizes_in(pdf)
    assert sizes[0] == pytest.approx((42, 30))
    assert sizes[1] == pytest.approx((8.5, 11))

    ppi = pri.native_ppi_for(*sizes[0])
    png, w, h = pri.render_page_png(pdf, 0, ppi)
    assert (w, h) == (pri.px_for_points(w_pt, ppi), pri.px_for_points(h_pt, ppi))
    assert pri.patches_for(w, h) <= 30_000
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size == (w, h)

    # Crop a box straddling the square edge at 100 ppi (square = 100 px).
    box = (50, 50, 250, 170)
    png, cw, ch = pri.render_page_png(pdf, 0, 100, box)
    assert (cw, ch) == (200, 120)
    tile = Image.open(io.BytesIO(png)).convert("L")
    assert tile.getpixel((10, 10)) < 50  # inside the square
    assert tile.getpixel((190, 110)) > 200  # outside it


def test_render_tile_never_exceeds_cap_and_rejects_bad_page():
    pdf = _pdf([(3024.0, 2160.0)])
    w, h = pri.px_for_points(3024.0, 260), pri.px_for_points(2160.0, 260)
    tiles = pri.plan_tiles(w, h)
    _, tw, th = pri.render_page_png(pdf, 0, 260, tiles[-1], max_patches=30_000)
    x0, y0, x1, y1 = tiles[-1]
    assert (tw, th) == (x1 - x0, y1 - y0)
    with pytest.raises(ValueError):
        pri.render_page_png(pdf, 3, 100)


def test_render_forces_trim_when_cap_is_below_the_tile():
    pdf = _pdf([(3024.0, 2160.0)])
    box = (0, 0, 1000, 700)  # 32 x 22 = 704 patches
    assert pri.patches_for(1000, 700) == 704
    _, tw, th = pri.render_page_png(pdf, 0, 100, box, max_patches=703)
    assert pri.patches_for(tw, th) <= 703
    assert 0 < 1000 - tw < 32 or 0 < 700 - th < 32
    assert tw <= 1000 and th <= 700


def _pdf_rotated_cropped(rotate: int) -> bytes:
    """A 42x30 in MediaBox with a 36x24 in CropBox offset from the corner,
    rotated by `rotate`, and a black 2 in square at the CropBox's lower-left
    corner (unrotated page space)."""
    from pypdf.generic import NameObject, NumberObject, RectangleObject
    from pypdf import PdfReader, PdfWriter

    square = b"0 0 0 rg 216 144 144 144 re f"
    base = _pdf([(3024.0, 2160.0)], content=square)
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(base)))
    page = writer.pages[0]
    page[NameObject("/CropBox")] = RectangleObject([216, 144, 216 + 2592, 144 + 1728])
    page[NameObject("/Rotate")] = NumberObject(rotate)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_rotated_cropped_page_tiles_match_full_render(rotate):
    pdf = _pdf_rotated_cropped(rotate)
    (w_pt, h_pt) = pri.page_sizes_pt(pdf)[0]
    expected = (2592.0, 1728.0) if rotate in (0, 180) else (1728.0, 2592.0)
    assert (w_pt, h_pt) == pytest.approx(expected)
    ppi = 50
    full_png, fw, fh = pri.render_page_png(pdf, 0, ppi)
    assert (fw, fh) == (pri.px_for_points(w_pt, ppi), pri.px_for_points(h_pt, ppi))
    full = Image.open(io.BytesIO(full_png)).convert("L")
    # Stored fractions (as plans_reader_groups.pages keeps them) rebuild the
    # same pixel boxes, and each tile matches the same area of the full page.
    tiles = pri.plan_tiles(fw, fh, max_patches=40)
    assert len(tiles) > 1
    for box in tiles:
        frac = pri.box_to_fractions(box, fw, fh)
        rebuilt = pri.box_from_fractions(tuple(frac), fw, fh)
        assert rebuilt == box
        png, tw, th = pri.render_page_png(pdf, 0, ppi, rebuilt, max_patches=40)
        assert (tw, th) == (box[2] - box[0], box[3] - box[1])
        tile = Image.open(io.BytesIO(png)).convert("L")
        ref = full.crop(box)
        diff = [abs(a - b) for a, b in zip(tile.get_flattened_data(), ref.get_flattened_data())]
        assert max(diff) <= 64  # anti-aliasing at the square's edge only
        assert sum(1 for d in diff if d > 8) <= 0.01 * len(diff) + 4
    # The square sits at the visual corner the rotation moves it to.
    dark = [(x, y) for y in range(fh) for x in range(fw) if full.getpixel((x, y)) < 50]
    assert dark
    cx = sum(x for x, _ in dark) / len(dark) / fw
    cy = sum(y for _, y in dark) / len(dark) / fh
    corner = {0: (0, 1), 90: (0, 0), 180: (1, 0), 270: (1, 1)}[rotate]
    assert abs(cx - corner[0]) < 0.2 and abs(cy - corner[1]) < 0.2


def test_render_failure_becomes_value_error(monkeypatch):
    import pypdfium2 as pdfium

    pdf = _pdf([(612, 792)])

    def boom(self, *args, **kwargs):
        raise pdfium.PdfiumError("Failed to render page.")

    monkeypatch.setattr(pdfium.PdfPage, "render", boom)
    with pytest.raises(ValueError, match="Page 1 of the PDF could not be rendered"):
        pri.render_page_png(pdf, 0, 72)


def test_write_failure_becomes_value_error(monkeypatch):
    from pypdf import PdfWriter
    from pypdf.errors import PyPdfError

    pdf = _pdf([(612, 792)] * 4)

    def boom(self, stream):
        raise PyPdfError("broken object")

    monkeypatch.setattr(PdfWriter, "write", boom)
    with pytest.raises(ValueError, match="could not be copied"):
        pri.pdf_chunks(pdf, max_pages=2)


def test_trim_to_patches_shaves_under_32px():
    img = Image.new("RGB", (6400 + 5, 4500))
    trimmed = pri._trim_to_patches(img, 200 * 141)
    assert trimmed.size == (6400, 4500)


def test_render_real_sheet_from_backup_when_available():
    root = "/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01/storage/project-files"
    candidates = sorted(
        p for p in glob.glob(f"{root}/*/drawing/*.pdf") if os.path.getsize(p) < 8_000_000
    )
    if not candidates:
        pytest.skip("production backup drawings are not on this machine")
    with open(candidates[0], "rb") as fh:
        pdf = fh.read()
    (w_pt, h_pt) = pri.page_sizes_pt(pdf)[0]
    ppi = pri.native_ppi_for(w_pt / 72, h_pt / 72)
    _, w, h = pri.render_page_png(pdf, 0, ppi, max_patches=30_000)
    assert abs(w - pri.px_for_points(w_pt, ppi)) <= 1
    assert abs(h - pri.px_for_points(h_pt, ppi)) <= 1
    assert pri.patches_for(w, h) <= 30_000


# PDF chunking -----------------------------------------------------------------


def test_pdf_chunks_small_file_is_returned_whole():
    pdf = _pdf([(612, 792)] * 3)
    assert pri.pdf_chunks(pdf) == [(1, 3, pdf)]


def test_pdf_chunks_respects_page_and_byte_caps():
    pdf = _pdf([(612, 792)] * 30, filler_bytes=20_000)
    chunks = pri.pdf_chunks(pdf, max_bytes=120_000, max_pages=8)
    assert chunks[0][0] == 1 and chunks[-1][1] == 30
    for (s, e, data), nxt in zip(chunks, chunks[1:] + [None]):
        assert e - s + 1 <= 8
        assert len(data) <= 120_000
        assert pri.page_count(data) == e - s + 1
        if nxt is not None:
            assert nxt[0] == e + 1
    # 20 KB a page under a 120 KB cap: about five pages a chunk.
    assert 4 <= max(e - s + 1 for s, e, _ in chunks) <= 6


def test_pdf_chunks_page_cap_only():
    pdf = _pdf([(612, 792)] * 10)
    chunks = pri.pdf_chunks(pdf, max_pages=4)
    assert [(s, e) for s, e, _ in chunks] == [(1, 4), (5, 8), (9, 10)]


def test_pdf_chunks_prefers_section_breaks():
    pdf = _pdf([(612, 792)] * 10)
    chunks = pri.pdf_chunks(pdf, max_pages=6, breaks=[4, 9])
    assert [(s, e) for s, e, _ in chunks] == [(1, 3), (4, 8), (9, 10)]


def test_pdf_chunks_ignores_a_break_that_would_leave_a_sliver():
    pdf = _pdf([(612, 792)] * 40)
    chunks = pri.pdf_chunks(pdf, max_pages=22, breaks=[2])
    assert [(s, e) for s, e, _ in chunks] == [(1, 22), (23, 40)]
    # A break past half the span is still honoured.
    chunks = pri.pdf_chunks(pdf, max_pages=22, breaks=[15])
    assert [(s, e) for s, e, _ in chunks] == [(1, 14), (15, 36), (37, 40)]


def test_pdf_chunks_rewrites_an_owner_password_file_decrypted():
    from pypdf import PdfReader, PdfWriter

    plain = _pdf([(612, 792)] * 3)
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(plain)))
    writer.encrypt(user_password="", owner_password="owner-secret")
    buf = io.BytesIO()
    writer.write(buf)
    encrypted = buf.getvalue()
    assert PdfReader(io.BytesIO(encrypted)).is_encrypted
    [(s, e, data)] = pri.pdf_chunks(encrypted)
    assert (s, e) == (1, 3)
    assert data != encrypted
    assert not PdfReader(io.BytesIO(data)).is_encrypted
    assert pri.page_count(data) == 3


def test_pdf_chunks_single_page_over_cap_raises():
    pdf = _pdf([(612, 792)] * 2, filler_bytes=60_000)
    with pytest.raises(ValueError, match="Page 1"):
        pri.pdf_chunks(pdf, max_bytes=30_000)


def test_pdf_chunks_broken_pdf_raises_value_error():
    with pytest.raises(ValueError):
        pri.pdf_chunks(b"not a pdf")


# Request parts ----------------------------------------------------------------


def test_parts_shapes():
    png = b"\x89PNG fake"
    url = pri.data_url(png)
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == png
    part = pri.image_part(png)
    assert part == {"type": "input_image", "image_url": url, "detail": "original"}
    with pytest.raises(ValueError):
        pri.image_part(png, detail="ultra")
    fp = pri.file_part("Specs Div 26", b"%PDF-1.7")
    assert fp["type"] == "input_file"
    assert fp["filename"] == "Specs Div 26.pdf"
    assert fp["file_data"].startswith("data:application/pdf;base64,")
    text = {"type": "input_text", "text": "count the receptacles"}
    assert pri.payload_bytes([part, fp, text]) == (
        len(part["image_url"]) + len(fp["file_data"]) + len(text["text"])
    )
    assert pri.REQUEST_MAX_IMAGES == 1_500
    assert pri.REQUEST_MAX_BYTES == 512 * 1024 * 1024
