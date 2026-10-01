"""The streaming images PDF writer (app/services/rfp_image_pdf.py).

This is the RFP Ingestion Sandbox component that republishes a file's thumb
tier as browsable `images-NNN.pdf` parts. It is hand written rather than
delegated to pypdf or Pillow because a 2000 page spec book must never be held
in memory, so the invariants that matter are structural rather than behavioral
and every one of them is pinned here:

* The bytes really are a PDF. Every part is opened with pypdf (strict mode
  included, which validates the cross reference table byte offsets) and must
  report the page count and the media boxes the caller asked for. A writer that
  produced almost-valid files would be caught by nothing else in the suite.
* The JPEG is embedded verbatim. The image XObject stream read back through
  pypdf has to equal the input bytes exactly: the sandbox already validated
  those bytes with its own marker walk, and any re-encoding here would both
  break that chain of custody and waste minutes of CPU per file.
* Parts roll at the byte cap and a page is NEVER split. The cap exists because
  the derived Storage bucket has a hard per object limit, so a part that
  overshoots is an upload failure later. The split test derives its cap from a
  measured three page part, which also proves the writer's size projection is
  exact rather than approximate (an approximate projection would land the split
  in the wrong place and the page distribution assertion would fail).
* A single page larger than the cap still gets a part of its own, because
  dropping a page would silently lose a drawing sheet.
* Zero pages writes nothing at all, and close() is idempotent, because the
  caller (rfp_ingest.execute) closes in a finally block after a possible error.

No database, no network, no sandbox subprocess: the module under test is pure
standard library. JPEGs are made with Pillow (a runtime dependency) from
deterministic pseudo random noise, because a flat color image compresses to a
few hundred bytes and would make the part cap arithmetic meaningless.
"""

import io
import os
import random

import pytest

from app.services.rfp_image_pdf import ImagesPdfWriter

# ── Fixtures built in process ────────────────────────────────────────────────

LETTER = (612.0, 792.0)


def _jpeg(width: int, height: int, seed: int = 0, quality: int = 70) -> bytes:
    """A JPEG of pseudo random noise, which does not compress away and so gives
    the part cap something real to measure."""
    from PIL import Image

    rng = random.Random(seed)
    noise = bytes(rng.getrandbits(8) for _ in range(width * height * 3))
    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), noise).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _add(writer: ImagesPdfWriter, jpeg: bytes, px=(80, 100), pt=LETTER) -> None:
    writer.add_page(jpeg, width_px=px[0], height_px=px[1], width_pt=pt[0], height_pt=pt[1])


def _reader(path, strict: bool = False):
    from pypdf import PdfReader

    return PdfReader(str(path), strict=strict)


def _page_counts(paths) -> list[int]:
    return [len(_reader(p).pages) for p in paths]


def _stream(path, index: int) -> bytes:
    """The raw image stream of one page, as pypdf hands it back. pypdf leaves a
    /DCTDecode stream untouched, so this is the JPEG the writer embedded."""
    page = _reader(path).pages[index]
    return page["/Resources"]["/XObject"]["/Im0"].get_object().get_data()


# ── One page ─────────────────────────────────────────────────────────────────


def test_a_single_page_produces_one_part_that_opens_in_pypdf(tmp_path):
    jpeg = _jpeg(120, 160, seed=1)
    with ImagesPdfWriter(tmp_path, part_bytes=10_000_000) as writer:
        writer.add_page(jpeg, width_px=120, height_px=160, width_pt=612, height_pt=792)
    parts = writer.parts

    assert [p.name for p in parts] == ["images-001.pdf"]
    assert writer.pages_written == 1
    reader = _reader(parts[0], strict=True)
    assert len(reader.pages) == 1
    assert [float(v) for v in reader.pages[0].mediabox] == [0.0, 0.0, 612.0, 792.0]


def test_the_part_starts_with_the_pdf_header_and_a_binary_comment(tmp_path):
    with ImagesPdfWriter(tmp_path, part_bytes=10_000_000) as writer:
        _add(writer, _jpeg(40, 40))
    head = writer.parts[0].read_bytes()[:16]

    # Line one identifies the version; line two must hold bytes above 127 so
    # every downstream tool treats the file as binary rather than text.
    assert head.startswith(b"%PDF-1.4\n%")
    assert max(head[10:14]) > 127


def test_the_image_object_declares_the_jpeg_without_re_encoding_it(tmp_path):
    jpeg = _jpeg(64, 48, seed=2)
    with ImagesPdfWriter(tmp_path, part_bytes=10_000_000) as writer:
        writer.add_page(jpeg, width_px=64, height_px=48, width_pt=200, height_pt=150)

    image = _reader(writer.parts[0]).pages[0]["/Resources"]["/XObject"]["/Im0"].get_object()
    assert image["/Subtype"] == "/Image"
    assert image["/Filter"] == "/DCTDecode"
    assert image["/ColorSpace"] == "/DeviceRGB"
    assert image["/BitsPerComponent"] == 8
    assert (image["/Width"], image["/Height"]) == (64, 48)
    # Byte for byte: the parent validated these exact bytes with its marker
    # walk, so anything but a verbatim copy breaks the chain of custody.
    assert image.get_data() == jpeg


def test_every_media_box_carries_that_pages_own_point_size(tmp_path):
    sizes = [(612, 792), (1224, 792), (841.89, 595.276), (2448.5, 1584.25)]
    with ImagesPdfWriter(tmp_path, part_bytes=10_000_000) as writer:
        for index, (width, height) in enumerate(sizes):
            _add(writer, _jpeg(40, 40, seed=index), pt=(width, height))

    boxes = [[float(v) for v in page.mediabox] for page in _reader(writer.parts[0]).pages]
    assert boxes == [[0.0, 0.0, float(w), float(h)] for w, h in sizes]


# ── Part rolling ─────────────────────────────────────────────────────────────


def _three_page_part_size(tmp_path, jpegs) -> int:
    """The exact byte size of a finished three page part. Used as the cap for
    the split test, which therefore also asserts the writer's size projection
    is exact: an estimate that ran even one byte high would push page three
    into the second part."""
    with ImagesPdfWriter(tmp_path / "measure", part_bytes=10**9) as writer:
        for jpeg in jpegs[:3]:
            _add(writer, jpeg)
    return writer.parts[0].stat().st_size


def test_pages_roll_into_a_new_part_at_the_byte_cap_and_never_split(tmp_path):
    jpegs = [_jpeg(80, 100, seed=index) for index in range(5)]
    cap = _three_page_part_size(tmp_path, jpegs)

    dest = tmp_path / "out"
    with ImagesPdfWriter(dest, part_bytes=cap) as writer:
        for jpeg in jpegs:
            _add(writer, jpeg)
    parts = writer.parts

    assert [p.name for p in parts] == ["images-001.pdf", "images-002.pdf"]
    assert _page_counts(parts) == [3, 2]
    # The finished part lands exactly on the cap, never over it.
    assert [p.stat().st_size for p in parts][0] == cap
    assert all(p.stat().st_size <= cap for p in parts)
    assert writer.pages_written == 5
    assert writer.bytes_written == sum(p.stat().st_size for p in parts)

    # Each page kept its own bytes, in order, across the part boundary.
    embedded = [_stream(parts[0], i) for i in range(3)] + [_stream(parts[1], i) for i in range(2)]
    assert embedded == jpegs


def test_a_page_bigger_than_the_cap_still_gets_a_part_of_its_own(tmp_path):
    # The cap is a bucket limit, not a licence to drop a drawing sheet: a part
    # always holds at least one page even when that page busts the cap.
    jpegs = [_jpeg(80, 100, seed=7), _jpeg(80, 100, seed=8)]
    with ImagesPdfWriter(tmp_path, part_bytes=100) as writer:
        for jpeg in jpegs:
            _add(writer, jpeg)
    parts = writer.parts

    assert _page_counts(parts) == [1, 1]
    assert all(p.stat().st_size > 100 for p in parts)
    assert [_stream(parts[i], 0) for i in (0, 1)] == jpegs


def test_the_open_part_joins_parts_only_once_it_is_finalized(tmp_path):
    # The caller may upload anything already in `parts` while the writer runs,
    # so a half written part must not appear there.
    jpegs = [_jpeg(80, 100, seed=index) for index in range(3)]
    writer = ImagesPdfWriter(tmp_path, part_bytes=100)
    _add(writer, jpegs[0])
    assert writer.parts == []
    _add(writer, jpegs[1])
    assert [p.name for p in writer.parts] == ["images-001.pdf"]
    _add(writer, jpegs[2])
    assert [p.name for p in writer.parts] == ["images-001.pdf", "images-002.pdf"]
    assert [p.name for p in writer.close()] == [f"images-{n:03d}.pdf" for n in (1, 2, 3)]


def test_the_part_name_template_is_configurable(tmp_path):
    with ImagesPdfWriter(tmp_path, part_bytes=100, name_template="thumbs-{part:02d}.pdf") as w:
        _add(w, _jpeg(80, 100, seed=1))
        _add(w, _jpeg(80, 100, seed=2))
    assert [p.name for p in w.parts] == ["thumbs-01.pdf", "thumbs-02.pdf"]


@pytest.mark.parametrize("template", ["images.pdf", "", "sub/dir-{part}.pdf", "{missing}.pdf"])
def test_a_template_that_cannot_name_distinct_local_files_is_refused(tmp_path, template):
    with pytest.raises(ValueError):
        ImagesPdfWriter(tmp_path, part_bytes=1000, name_template=template)


# ── Closing ──────────────────────────────────────────────────────────────────


def test_close_is_idempotent_and_keeps_returning_the_same_parts(tmp_path):
    writer = ImagesPdfWriter(tmp_path, part_bytes=10_000_000)
    _add(writer, _jpeg(80, 100, seed=3))
    first = writer.close()
    second = writer.close()

    assert [p.name for p in first] == ["images-001.pdf"]
    assert second == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["images-001.pdf"]
    # The list handed back is a copy; mutating it must not corrupt the writer.
    first.append("junk")
    assert writer.close() == second


def test_zero_pages_writes_nothing_at_all(tmp_path):
    dest = tmp_path / "never-created"
    writer = ImagesPdfWriter(dest, part_bytes=1_000_000)

    assert writer.close() == []
    assert writer.parts == []
    assert writer.pages_written == 0 and writer.bytes_written == 0
    # Not even the destination directory is created for a file with no pages.
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_adding_a_page_after_close_is_refused(tmp_path):
    writer = ImagesPdfWriter(tmp_path, part_bytes=1_000_000)
    writer.close()
    with pytest.raises(ValueError):
        _add(writer, _jpeg(40, 40))


def test_a_failed_write_drops_the_open_part_instead_of_publishing_it(tmp_path, monkeypatch):
    # A page that only got halfway to disk (a full scratch volume) leaves an
    # object the cross reference table cannot describe, so the part is dropped
    # whole. Publishing it would hand the derived bucket a corrupt PDF.
    writer = ImagesPdfWriter(tmp_path, part_bytes=10_000_000)
    _add(writer, _jpeg(40, 40, seed=1))
    real_write = writer._write
    seen = {"writes": 0}

    def _explode(data):
        seen["writes"] += 1
        if seen["writes"] == 3:
            raise OSError(28, "No space left on device")
        real_write(data)

    monkeypatch.setattr(writer, "_write", _explode)
    with pytest.raises(OSError):
        _add(writer, _jpeg(40, 40, seed=2))

    assert writer.parts == []
    assert writer.close() == []


def test_the_context_manager_finalizes_the_part_even_when_the_body_raises(tmp_path):
    # rfp_ingest builds the PDF inside the per file try/except; a failure
    # halfway through must still leave readable parts behind for cleanup.
    writer = ImagesPdfWriter(tmp_path, part_bytes=10_000_000)
    with pytest.raises(RuntimeError):
        with writer:
            _add(writer, _jpeg(80, 100, seed=4))
            raise RuntimeError("caller exploded")

    assert _page_counts(writer.parts) == [1]


# ── Argument validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width_px": 0},
        {"height_px": -1},
        {"width_px": 1.5},
        {"width_pt": 0},
        {"height_pt": float("nan")},
        {"height_pt": float("inf")},
        {"width_pt": "612"},
    ],
)
def test_a_page_with_impossible_dimensions_is_refused(tmp_path, kwargs):
    writer = ImagesPdfWriter(tmp_path, part_bytes=1_000_000)
    call = {"width_px": 80, "height_px": 100, "width_pt": 612, "height_pt": 792, **kwargs}
    with pytest.raises(ValueError):
        writer.add_page(_jpeg(80, 100), **call)


@pytest.mark.parametrize("payload", [b"", "not bytes", None])
def test_a_page_without_jpeg_bytes_is_refused(tmp_path, payload):
    writer = ImagesPdfWriter(tmp_path, part_bytes=1_000_000)
    with pytest.raises(ValueError):
        _add(writer, payload)


@pytest.mark.parametrize("cap", [0, -1, 1.5, True])
def test_a_nonsense_part_cap_is_refused(tmp_path, cap):
    with pytest.raises(ValueError):
        ImagesPdfWriter(tmp_path, part_bytes=cap)


def test_an_existing_part_name_is_never_overwritten(tmp_path):
    # Two writers pointed at one directory is a caller bug; silently clobbering
    # the other one's output would lose pages with no error anywhere.
    (tmp_path / "images-001.pdf").write_bytes(b"someone else's part")
    writer = ImagesPdfWriter(tmp_path, part_bytes=1_000_000)
    with pytest.raises(FileExistsError):
        _add(writer, _jpeg(40, 40))
    assert (tmp_path / "images-001.pdf").read_bytes() == b"someone else's part"


def test_a_bytearray_page_is_stored_as_the_same_bytes(tmp_path):
    jpeg = _jpeg(48, 48, seed=9)
    with ImagesPdfWriter(tmp_path, part_bytes=1_000_000) as writer:
        writer.add_page(bytearray(jpeg), width_px=48, height_px=48, width_pt=100, height_pt=100)
    assert _stream(writer.parts[0], 0) == jpeg


# ── Scale ────────────────────────────────────────────────────────────────────


def test_many_pages_keep_a_valid_cross_reference_table(tmp_path):
    # Object numbers and xref offsets grow past three and four digits here,
    # which is where an off by one in the size projection or the table would
    # show up. strict=True makes pypdf verify the offsets it reads.
    jpegs = [_jpeg(24, 24, seed=index) for index in range(40)]
    with ImagesPdfWriter(tmp_path, part_bytes=10_000_000) as writer:
        for jpeg in jpegs:
            _add(writer, jpeg, px=(24, 24))

    reader = _reader(writer.parts[0], strict=True)
    assert len(reader.pages) == 40
    assert _stream(writer.parts[0], 39) == jpegs[39]
    assert writer.bytes_written == os.path.getsize(writer.parts[0])
