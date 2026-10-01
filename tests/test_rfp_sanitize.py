"""rfp_sanitize: the parent-side byte checks of the RFP Ingestion Sandbox.

Everything the API process does to an inbound RFP file or to the sandbox
child's output before trusting it goes through this module, so these tests pin
the exact verdict-or-flag table of docs/RFP_INGESTION_SANDBOX.md section 2, the
JPEG marker walk of section 3.4, and the text contract, with no database, no
network and no parser library anywhere near the code under test:

* Sniff: the two verdicts (`not_pdf`, `polyglot`) plus `empty`, checked in
  that order so a bare ZIP or EXE is `not_pdf` and `polyglot` is reserved for
  files that really hide a PDF behind another format's magic; every other
  oddity (late header, junk before it, "MZ" inside a real PDF, a trailing ZIP,
  a missing %%EOF, active-content byte markers) is a FLAG, never a verdict.
  `sniff_file` must answer exactly like `sniff_bytes` for any chunk size,
  including markers, `%%EOF` and the ZIP EOCD straddling chunk boundaries.
* JPEG walk: a real Pillow baseline and progressive RGB encode pass with the
  right (w, h); grayscale, EXIF/COM segments, a second SOF, truncation,
  trailing bytes, a missing EOI, PNG bytes and zero dimensions all raise
  ValueError. The single JFIF APP0 is pinned field by field, so neither a
  padded APP0 nor a pile of repeated ones can carry child-chosen bytes through
  the walk. Nothing is decoded, and the module never imports Pillow (checked
  in a fresh interpreter).
* Text contract: each hidden category is counted under exactly
  `protocol.TEXT_HAZARD_KEYS` and stripped; control characters are stripped
  without counting (\\r included, so CRLF collapses to LF); \\n and \\t survive;
  re-sanitizing clean text is a no-op with zero hazards (the parent re-runs
  the child's cleaner), and the parent's `sanitize_text` agrees with the
  child's `app.sandbox.textclean.sanitize` code point for code point on a
  generated corpus that covers every Unicode general category.
* Display helpers never raise and never leak controls or bidi overrides.

PDF fixtures come from pypdf, JPEG fixtures from Pillow, both imported inside
the helpers only (reportlab is not installed; the module under test must not
see either library).
"""

import hashlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.sandbox import protocol
from app.services import rfp_sanitize as rs

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _blank_pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


PDF = _blank_pdf()
assert PDF.startswith(b"%PDF-") and PDF.endswith(b"%%EOF\n"), "pypdf output shape changed"


def _jpeg(mode: str = "RGB", size=(37, 23), **save_kwargs) -> bytes:
    from PIL import Image

    fill = (200, 30, 30) if mode == "RGB" else (128 if mode == "L" else (1, 2, 3, 4))
    img = Image.new(mode, size, fill)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70, **save_kwargs)
    return buf.getvalue()


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.txt", "hello")
    return buf.getvalue()


def _all_zero_markers() -> dict[str, int]:
    return {m.decode("ascii"): 0 for m in protocol.BYTE_MARKERS}


# ── Sniff: verdicts ──────────────────────────────────────────────────────────


def test_a_plain_pdf_passes_with_every_flag_quiet():
    res = rs.sniff_bytes(PDF)
    assert res.verdict is None
    assert res.pdf_header_offset == 0
    assert tuple(res.flags) == rs.SNIFF_FLAG_KEYS
    assert res.flags == {
        "pdf_header_offset": 0,
        "markers_in_head": [],
        # pypdf ends the file with "%%EOF\n": an end-of-line after the marker
        # is not "bytes after the last %%EOF", otherwise every real PDF flags.
        "bytes_after_last_eof": 0,
        "zip_eocd_in_tail": False,
        "missing_eof": False,
    }
    assert res.byte_markers == _all_zero_markers()


def test_empty_input_is_the_empty_verdict_with_default_flags():
    res = rs.sniff_bytes(b"")
    assert res.verdict == protocol.REJECT_EMPTY
    assert res.pdf_header_offset is None
    assert res.flags == {
        "pdf_header_offset": None,
        "markers_in_head": [],
        "bytes_after_last_eof": 0,
        "zip_eocd_in_tail": False,
        "missing_eof": True,
    }
    assert res.byte_markers == _all_zero_markers()


def test_header_must_start_within_the_first_1024_bytes():
    # Offset 1023 is the last tolerated start; 1024 is one byte too late.
    late = rs.sniff_bytes(b"x" * 1023 + PDF)
    assert late.verdict is None
    assert late.pdf_header_offset == 1023
    too_late = rs.sniff_bytes(b"x" * 1024 + PDF)
    assert too_late.verdict == protocol.REJECT_NOT_PDF
    assert too_late.pdf_header_offset is None
    assert too_late.flags["pdf_header_offset"] is None


def test_junk_before_the_header_is_a_flag_not_a_verdict():
    res = rs.sniff_bytes(b"some mail gateway banner\r\n" + PDF)
    assert res.verdict is None
    assert res.pdf_header_offset == 26
    assert res.flags["markers_in_head"] == []


def test_a_file_with_no_pdf_header_at_all_is_not_pdf():
    for data in (b"hello world", b"MZ\x90\x00 an executable", _zip_bytes(), b"\x00" * 4096):
        res = rs.sniff_bytes(data)
        # The polyglot verdict means "a PDF hides behind another format"; with
        # no PDF header anywhere the honest answer is simply "not a PDF".
        assert res.verdict == protocol.REJECT_NOT_PDF, data[:8]
        assert res.pdf_header_offset is None


@pytest.mark.parametrize(
    "prefix, name",
    [
        (b"PK\x03\x04\x14\x00\x00\x00", "zip"),
        (b"MZ\x90\x00\x03\x00", "mz"),
        (b"\x7fELF\x02\x01\x01", "elf"),
        (b"GIF89a\x01\x00\x01\x00", "gif"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "jpeg"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"{\\rtf1\\ansi ", "rtf"),
        (b"%!PS-Adobe-3.0\n", "ps"),
        (b"<!DOCTYPE html>\n", "doctype"),
        (b"<html><body>", "html"),
        (b"<SCRIPT>alert(1)</SCRIPT>", "script"),
        (b'<?xml version="1.0"?>', "xml"),
    ],
)
def test_a_foreign_magic_at_offset_zero_with_a_pdf_behind_it_is_polyglot(prefix, name):
    res = rs.sniff_bytes(prefix + PDF)
    assert res.verdict == protocol.REJECT_POLYGLOT
    assert res.pdf_header_offset == len(prefix)
    # The flag list still records what was seen, for the manifest.
    assert name in res.flags["markers_in_head"]


def test_gif_pdf_polyglot_is_rejected_even_with_a_valid_pdf_body():
    # The classic polyglot: a GIF header whose comment block carries the PDF.
    res = rs.sniff_bytes(b"GIF89a" + b"\x00" * 10 + PDF)
    assert res.verdict == protocol.REJECT_POLYGLOT
    assert res.flags["markers_in_head"] == ["gif"]


def test_bom_and_leading_whitespace_do_not_hide_a_text_magic():
    res = rs.sniff_bytes(b"\xef\xbb\xbf \r\n\t<HTML>" + PDF)
    assert res.verdict == protocol.REJECT_POLYGLOT
    assert rs.sniff_bytes(b"\xef\xbb\xbf<?XML" + PDF).verdict == protocol.REJECT_POLYGLOT
    assert rs.sniff_bytes(b"   %!PS" + PDF).verdict == protocol.REJECT_POLYGLOT
    assert rs.sniff_bytes(b"\n{\\rtf1" + PDF).verdict == protocol.REJECT_POLYGLOT


def test_bom_and_whitespace_before_the_pdf_header_itself_pass():
    res = rs.sniff_bytes(b"\xef\xbb\xbf \n" + PDF)
    assert res.verdict is None
    assert res.pdf_header_offset == 5


def test_whitespace_before_a_binary_magic_is_not_offset_zero():
    # Only the text formats get the BOM/whitespace allowance; "MZ" at offset 1
    # is junk before the header, recorded as a flag.
    res = rs.sniff_bytes(b"\nMZ" + PDF)
    assert res.verdict is None
    assert res.flags["markers_in_head"] == ["mz"]


def test_mz_inside_the_first_kb_of_a_real_pdf_passes():
    # A PDF whose first kilobyte merely CONTAINS "MZ" (after the header) is a
    # normal PDF; only an MZ AT OFFSET 0 is a verdict.
    after_header = PDF.replace(b"%PDF-1.3\n", b"%PDF-1.3\n%MZ binary comment\n", 1)
    assert after_header != PDF
    res = rs.sniff_bytes(after_header)
    assert res.verdict is None
    assert res.flags["markers_in_head"] == []
    # Before the header (not at 0) it is still no verdict, but it is flagged.
    before_header = b"xx MZ xx GIF8 xx" + PDF
    res = rs.sniff_bytes(before_header)
    assert res.verdict is None
    assert res.pdf_header_offset == 16
    assert res.flags["markers_in_head"] == ["mz", "gif"]  # first-occurrence order


def test_markers_in_head_only_looks_before_the_header():
    res = rs.sniff_bytes(PDF + b"PK\x03\x04 <html> MZ")
    assert res.verdict is None
    assert res.flags["markers_in_head"] == []


def test_markers_in_head_matches_the_markup_magics_case_insensitively():
    res = rs.sniff_bytes(b"x<!Doctype x<Script x<Html x<?Xml x" + PDF)
    assert res.verdict is None  # none is at offset 0
    assert res.flags["markers_in_head"] == ["doctype", "script", "html", "xml"]


# ── Sniff: office files (section 2.1) ────────────────────────────────────────

OOXML = b"PK\x03\x04\x14\x00\x06\x00" + b"[Content_Types].xml" + b"\x00" * 64
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def test_a_pdf_reports_the_pdf_source_format_with_or_without_a_filename():
    assert rs.sniff_bytes(PDF).source_format == protocol.SOURCE_FORMAT_PDF
    assert rs.sniff_bytes(PDF, filename="Spec Book.pdf").source_format == "pdf"
    # A .docx name over PDF bytes is a PDF: the header rule comes first and
    # the extension never overrides it.
    res = rs.sniff_bytes(PDF, filename="renamed.docx")
    assert res.verdict is None and res.source_format == protocol.SOURCE_FORMAT_PDF
    assert res.pdf_header_offset == 0


@pytest.mark.parametrize(
    "data, name, fmt",
    [
        (OOXML, "Bid Form.docx", protocol.SOURCE_FORMAT_DOCX),
        (OOXML, "SCHEDULE.XLSX", protocol.SOURCE_FORMAT_XLSX),
        (OLE2, "old spec.doc", protocol.SOURCE_FORMAT_DOC),
        (OLE2, "Takeoff.xls", protocol.SOURCE_FORMAT_XLS),
    ],
)
def test_office_magic_with_a_matching_extension_passes_as_that_format(data, name, fmt):
    res = rs.sniff_bytes(data, filename=name)
    assert res.verdict is None
    assert res.source_format == fmt
    assert res.pdf_header_offset is None
    # The flags describe the bytes as a PDF would be read; they are recorded,
    # not judged, for an office file.
    assert res.flags["missing_eof"] is True
    assert tuple(res.flags) == rs.SNIFF_FLAG_KEYS
    assert res.byte_markers == _all_zero_markers()


def test_a_zip_named_pdf_stays_not_pdf():
    res = rs.sniff_bytes(OOXML, filename="Bid Form.pdf")
    assert res.verdict == protocol.REJECT_NOT_PDF and res.source_format is None
    assert rs.sniff_bytes(_zip_bytes(), filename="archive.pdf").verdict == protocol.REJECT_NOT_PDF


def test_magic_and_extension_must_name_the_same_family():
    # OOXML bytes under a legacy name, OLE2 bytes under an OOXML name, and
    # names outside the four: all not_pdf, no guessing across families.
    for data, name in ((OOXML, "x.doc"), (OOXML, "x.xls"), (OLE2, "x.docx"),
                       (OLE2, "x.xlsx"), (OOXML, "x.zip"), (OOXML, "x.pptx"),
                       (OLE2, "x.msg"), (OOXML, "docx"), (OOXML, "")):
        res = rs.sniff_bytes(data, filename=name)
        assert res.verdict == protocol.REJECT_NOT_PDF, name
        assert res.source_format is None


def test_an_office_magic_must_sit_at_offset_zero():
    assert rs.sniff_bytes(b"\n" + OOXML, filename="a.docx").verdict == protocol.REJECT_NOT_PDF
    assert rs.sniff_bytes(b"x" + OLE2, filename="a.xls").verdict == protocol.REJECT_NOT_PDF


def test_without_a_filename_office_bytes_are_not_pdf_which_is_the_flag_off_path():
    # The default keeps the PDF-only table byte for byte: the setting is
    # applied by the callers, which pass no filename while office files are
    # off, and the sniff itself has no other switch.
    for data in (OOXML, OLE2):
        res = rs.sniff_bytes(data)
        assert res.verdict == protocol.REJECT_NOT_PDF and res.source_format is None
        assert rs.sniff_bytes(data, filename=None).verdict == protocol.REJECT_NOT_PDF


def test_an_office_named_polyglot_is_still_polyglot():
    # PK at offset 0 with a PDF header inside the window: the PDF rules win
    # and say polyglot, whatever the name.
    res = rs.sniff_bytes(b"PK\x03\x04" + PDF, filename="a.docx")
    assert res.verdict == protocol.REJECT_POLYGLOT and res.source_format is None


def test_sniff_file_takes_the_declared_name_not_the_path(tmp_path):
    path = tmp_path / "source.pdf"           # the parent's scratch name
    path.write_bytes(OOXML)
    assert rs.sniff_file(path).verdict == protocol.REJECT_NOT_PDF
    res = rs.sniff_file(path, filename="Bid Form.docx", chunk_bytes=3)
    assert res.verdict is None and res.source_format == protocol.SOURCE_FORMAT_DOCX
    assert res == rs.sniff_bytes(OOXML, filename="Bid Form.docx")


def test_declared_extension_and_office_format_helpers():
    assert rs.declared_extension("A.DOCX") == ".docx"
    assert rs.declared_extension("dir/sub/a.tar.xls") == ".xls"
    assert rs.declared_extension("C:\\share\\a.doc") == ".doc"
    assert rs.declared_extension("noext") == ""
    assert rs.declared_extension(None) == ""
    assert rs.office_format(OOXML, "a.xlsx") == protocol.SOURCE_FORMAT_XLSX
    assert rs.office_format(OOXML, None) is None
    assert rs.office_format(b"", "a.xlsx") is None
    assert set(rs._OFFICE_EXTENSIONS) == {".docx", ".xlsx", ".doc", ".xls"}
    assert {fmt for _, fmt in rs._OFFICE_EXTENSIONS.values()} == set(protocol.OFFICE_FORMATS)


# ── Sniff: flags ─────────────────────────────────────────────────────────────


def test_trailing_zip_is_flagged_with_the_byte_count_and_the_eocd():
    zipped = _zip_bytes()
    res = rs.sniff_bytes(PDF + zipped)
    assert res.verdict is None
    assert res.flags["zip_eocd_in_tail"] is True
    # Everything after "%%EOF" counts once the tail is not just an EOL: the
    # newline pypdf wrote plus the whole archive.
    assert res.flags["bytes_after_last_eof"] == len(zipped) + 1
    assert res.flags["missing_eof"] is False


def test_an_eol_only_tail_after_eof_counts_as_zero_bytes_but_junk_counts_fully():
    assert rs.sniff_bytes(PDF + b"\r\n\r\n \t").flags["bytes_after_last_eof"] == 0
    # pypdf's own "\n" after %%EOF plus the three appended bytes.
    assert rs.sniff_bytes(PDF + b"\n\nX").flags["bytes_after_last_eof"] == 4
    assert rs.sniff_bytes(PDF + b"\x00").flags["bytes_after_last_eof"] == 2
    assert rs.sniff_bytes(PDF.rstrip(b"\n")).flags["bytes_after_last_eof"] == 0


def test_the_last_eof_is_the_one_that_counts():
    # An incremental update appends a second xref/trailer and its own %%EOF.
    res = rs.sniff_bytes(PDF + b"3 0 obj << >> endobj\n%%EOF\n")
    assert res.flags["bytes_after_last_eof"] == 0
    assert res.flags["missing_eof"] is False


def test_missing_eof_is_a_flag_not_a_verdict():
    res = rs.sniff_bytes(PDF.replace(b"%%EOF", b"%%EOX"))
    assert res.verdict is None
    assert res.flags["missing_eof"] is True
    assert res.flags["bytes_after_last_eof"] == 0


def test_zip_eocd_is_only_looked_for_in_the_last_64_kb():
    inside = PDF + rs.ZIP_EOCD + b"x" * (rs.ZIP_TAIL_WINDOW - 100)
    outside = PDF + rs.ZIP_EOCD + b"x" * (rs.ZIP_TAIL_WINDOW + 100)
    assert rs.sniff_bytes(inside).flags["zip_eocd_in_tail"] is True
    assert rs.sniff_bytes(outside).flags["zip_eocd_in_tail"] is False


def test_byte_markers_are_counted_over_the_whole_file():
    body = (
        b"<< /Type /Catalog /OpenAction 5 0 R /AA << /O 6 0 R >> >>\n"
        b"<< /S /JavaScript /JS (app.alert(1)) >>\n"
        b"<< /S /Launch /F (cmd.exe) >>\n"
        b"<< /Type /EmbeddedFile >> /XFA 7 0 R /URI (http://x) /URI (http://y)\n"
        b"/RichMedia 8 0 R /GoToR 9 0 R"
    )
    data = PDF.replace(b"%%EOF", body + b"\n%%EOF")
    res = rs.sniff_bytes(data)
    assert res.verdict is None
    assert res.byte_markers == {
        "/OpenAction": 1,
        "/AA": 1,
        "/JavaScript": 1,
        "/JS": 1,
        "/Launch": 1,
        "/EmbeddedFile": 1,
        "/XFA": 1,
        "/URI": 2,
        "/RichMedia": 1,
        "/GoToR": 1,
    }
    assert set(res.byte_markers) == {m.decode("ascii") for m in protocol.BYTE_MARKERS}


def test_byte_markers_are_counted_even_for_a_rejected_file():
    # The manifest records what a rejected file looked like too.
    res = rs.sniff_bytes(b"MZ /JS /JS /OpenAction" + PDF)
    assert res.verdict == protocol.REJECT_POLYGLOT
    assert res.byte_markers["/JS"] == 2
    assert res.byte_markers["/OpenAction"] == 1


def test_sniff_bytes_accepts_bytearray_and_memoryview():
    assert rs.sniff_bytes(bytearray(PDF)) == rs.sniff_bytes(PDF)
    assert rs.sniff_bytes(memoryview(PDF)) == rs.sniff_bytes(PDF)


# ── Sniff: streaming ─────────────────────────────────────────────────────────


def _messy_file() -> bytes:
    """Junk before the header (with magics), markers spread through the body,
    an incremental-update EOF, then a trailing ZIP: every flag lit at once."""
    body = (
        b"/OpenAction 1 0 R /JS (x) /JavaScript /AA /URI /URI /GoToR /XFA /Launch "
        b"/RichMedia /EmbeddedFile"
    )
    return (
        b"gateway MZ banner GIF8 <html>\n"
        + PDF.replace(b"%%EOF", body + b"\n%%EOF")
        + b"update\n%%EOF\r\n"
        + _zip_bytes()
    )


@pytest.mark.parametrize("chunk_bytes", [1, 2, 3, 5, 7, 11, 64, 1000, 1 << 20])
def test_sniff_file_matches_sniff_bytes_for_any_chunk_size(tmp_path, chunk_bytes):
    data = _messy_file()
    path = tmp_path / "source.pdf"
    path.write_bytes(data)
    expected = rs.sniff_bytes(data)
    # Sanity: the fixture really exercises every flag and every marker.
    assert expected.verdict is None
    assert expected.flags["markers_in_head"] == ["mz", "gif", "html"]
    assert expected.flags["zip_eocd_in_tail"] is True
    assert expected.flags["bytes_after_last_eof"] > 0
    assert all(n >= 1 for n in expected.byte_markers.values())
    assert rs.sniff_file(path, chunk_bytes=chunk_bytes) == expected


def test_a_marker_straddling_the_chunk_boundary_is_counted_exactly_once(tmp_path):
    # With 7-byte chunks "/OpenAction" (11 bytes) always crosses a boundary,
    # and so does the "%%EOF" placed to start at offset 12 of a chunk.
    data = b"%PDF-1.4\n/OpenAction /OpenAction\n%%EOF\n"
    path = tmp_path / "s.pdf"
    path.write_bytes(data)
    for chunk in (1, 4, 7, 10):
        res = rs.sniff_file(path, chunk_bytes=chunk)
        assert res.byte_markers["/OpenAction"] == 2, chunk
        assert res.flags["missing_eof"] is False, chunk
        assert res.flags["bytes_after_last_eof"] == 0, chunk
        assert res.verdict is None


def test_sniff_file_handles_an_empty_file_and_refuses_a_zero_chunk(tmp_path):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    assert rs.sniff_file(path).verdict == protocol.REJECT_EMPTY
    with pytest.raises(ValueError):
        rs.sniff_file(path, chunk_bytes=0)


def test_sniff_file_with_a_late_header_split_across_chunks(tmp_path):
    path = tmp_path / "late.pdf"
    path.write_bytes(b"x" * 1023 + PDF)
    res = rs.sniff_file(path, chunk_bytes=512)
    assert res.verdict is None and res.pdf_header_offset == 1023
    path.write_bytes(b"x" * 1024 + PDF)
    assert rs.sniff_file(path, chunk_bytes=512).verdict == protocol.REJECT_NOT_PDF


def test_eocd_straddling_the_chunk_boundary_and_the_tail_window(tmp_path):
    path = tmp_path / "z.pdf"
    path.write_bytes(PDF + b"a" * 3 + rs.ZIP_EOCD + b"b" * 5)
    for chunk in (1, 2, 3):
        assert rs.sniff_file(path, chunk_bytes=chunk).flags["zip_eocd_in_tail"] is True
    path.write_bytes(PDF + rs.ZIP_EOCD + b"b" * (rs.ZIP_TAIL_WINDOW + 1))
    assert rs.sniff_file(path, chunk_bytes=4096).flags["zip_eocd_in_tail"] is False


# ── sha256 ───────────────────────────────────────────────────────────────────


def test_sha256_helpers_agree_with_hashlib_and_report_the_real_size(tmp_path):
    data = _messy_file()
    assert rs.sha256_bytes(data) == hashlib.sha256(data).hexdigest()
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    assert rs.sha256_file(path) == (rs.sha256_bytes(data), len(data))
    assert rs.sha256_file(path, chunk_bytes=3) == (rs.sha256_bytes(data), len(data))
    assert rs.sha256_file(str(path)) == rs.sha256_file(Path(path))
    empty = tmp_path / "e.bin"
    empty.write_bytes(b"")
    assert rs.sha256_file(empty) == (hashlib.sha256(b"").hexdigest(), 0)
    with pytest.raises(ValueError):
        rs.sha256_file(path, chunk_bytes=0)


# ── JPEG marker walk ─────────────────────────────────────────────────────────


def test_a_pillow_baseline_rgb_jpeg_passes_with_its_dimensions():
    assert rs.jpeg_dimensions(_jpeg("RGB", (37, 23))) == (37, 23)
    # Non-square, non-multiple-of-8 sizes read width and height in the right
    # order (the SOF stores height first).
    assert rs.jpeg_dimensions(_jpeg("RGB", (301, 17))) == (301, 17)
    assert rs.jpeg_dimensions(_jpeg("RGB", (1, 1))) == (1, 1)


def test_progressive_and_optimized_encodes_pass():
    # Progressive files carry several SOS segments with DHT tables between
    # them; the walk resumes segment parsing after every scan.
    assert rs.jpeg_dimensions(_jpeg("RGB", (64, 40), progressive=True)) == (64, 40)
    assert rs.jpeg_dimensions(_jpeg("RGB", (64, 40), optimize=True)) == (64, 40)
    assert rs.jpeg_dimensions(
        _jpeg("RGB", (64, 40), progressive=True, optimize=True)
    ) == (64, 40)
    assert rs.jpeg_dimensions(bytearray(_jpeg())) == (37, 23)


def test_wrong_component_count_fails():
    with pytest.raises(ValueError, match="components"):
        rs.jpeg_dimensions(_jpeg("L"))
    # CMYK carries four components (and an Adobe APP14 segment).
    with pytest.raises(ValueError):
        rs.jpeg_dimensions(_jpeg("CMYK"))


def test_an_app1_exif_segment_fails():
    data = _jpeg()
    with_exif = data[:2] + b"\xff\xe1\x00\x08Exif\x00\x00" + data[2:]
    with pytest.raises(ValueError, match="0xE1"):
        rs.jpeg_dimensions(with_exif)
    # Pillow's own exif= path writes the same APP1 marker.
    with pytest.raises(ValueError):
        rs.jpeg_dimensions(_jpeg(exif=b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\x00\x00"))


def test_a_comment_segment_fails_before_and_after_the_scan():
    data = _jpeg()
    with pytest.raises(ValueError, match="0xFE"):
        rs.jpeg_dimensions(data[:2] + b"\xff\xfe\x00\x04hi" + data[2:])
    # After the entropy data, right before EOI.
    with pytest.raises(ValueError, match="0xFE"):
        rs.jpeg_dimensions(data[:-2] + b"\xff\xfe\x00\x04hi" + data[-2:])


def test_trailing_bytes_after_eoi_fail():
    data = _jpeg()
    for tail in (b"\x00", b"\n", b"\xff\xd9", b"junk" * 10):
        with pytest.raises(ValueError, match="trailing"):
            rs.jpeg_dimensions(data + tail)


def test_truncated_files_fail_wherever_the_cut_lands():
    data = _jpeg()
    # Inside the segment table, inside the entropy data, and just before EOI.
    for cut in (3, 5, 20, len(data) // 2, len(data) - 3, len(data) - 1):
        with pytest.raises(ValueError):
            rs.jpeg_dimensions(data[:cut])


def test_missing_eoi_fails():
    with pytest.raises(ValueError):
        rs.jpeg_dimensions(_jpeg()[:-2])


def test_non_jpeg_bytes_fail():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "PNG")
    with pytest.raises(ValueError, match="SOI"):
        rs.jpeg_dimensions(buf.getvalue())
    for junk in (b"", b"\xff", b"\xff\xd8", b"\xff\xd8\xff", b"%PDF-1.4", b"\xff\xd8\xff\xd9"):
        with pytest.raises(ValueError):
            rs.jpeg_dimensions(junk)


def test_a_second_sof_fails():
    data = _jpeg()
    sof_at = data.find(b"\xff\xc0")
    length = (data[sof_at + 2] << 8) | data[sof_at + 3]
    sof_segment = data[sof_at : sof_at + 2 + length]
    with pytest.raises(ValueError, match="more than one SOF"):
        rs.jpeg_dimensions(data[:sof_at] + sof_segment + data[sof_at:])


def test_sos_without_a_sof_and_zero_dimensions_and_bad_precision_fail():
    data = _jpeg()
    sof_at = data.find(b"\xff\xc0")
    # Turn the SOF into a DQT so the SOS arrives with no frame header (DQT,
    # unlike APP0, carries no shape the walk pins).
    masked = bytearray(data)
    masked[sof_at + 1] = 0xDB
    with pytest.raises(ValueError, match="before any SOF"):
        rs.jpeg_dimensions(bytes(masked))
    zero_height = bytearray(data)
    zero_height[sof_at + 5] = 0
    zero_height[sof_at + 6] = 0
    with pytest.raises(ValueError, match="at least 1"):
        rs.jpeg_dimensions(bytes(zero_height))
    twelve_bit = bytearray(data)
    twelve_bit[sof_at + 4] = 12
    with pytest.raises(ValueError, match="precision"):
        rs.jpeg_dimensions(bytes(twelve_bit))


def test_a_pillow_encode_carries_one_jfif_app0_the_walk_accepts():
    # The fixture the other JPEG tests build on really does hold the segment
    # the walk now pins, so these checks cannot silently reject real output.
    data = _jpeg()
    assert data[2:4] == b"\xff\xe0", "Pillow no longer writes APP0 first"
    assert data[4:6] == b"\x00\x10" and data[6:11] == b"JFIF\x00"
    assert rs.jpeg_dimensions(data) == (37, 23)
    # dpi= moves the density fields, which stay free inside the fixed shape.
    assert rs.jpeg_dimensions(_jpeg(dpi=(300, 300))) == (37, 23)


def test_an_app0_carrying_child_chosen_bytes_fails():
    # A compromised child must not be able to smuggle blobs through an
    # artifact that the marker walk signs off on (F-043).
    data = _jpeg()
    blob = b"<script>alert(1)</script>".ljust(600, b"\x41")
    smuggle = b"\xff\xe0" + len(blob + b"\x00\x00").to_bytes(2, "big") + blob
    with pytest.raises(ValueError, match="APP0"):
        rs.jpeg_dimensions(data[:2] + smuggle + data[2:])
    # ...nor by repeating the legitimate JFIF segment, whatever the count.
    jfif = data[2:20]
    for copies in (1, 2, 50):
        with pytest.raises(ValueError, match="APP0"):
            rs.jpeg_dimensions(data[:2] + jfif * copies + data[2:])


def test_a_malformed_jfif_app0_fails_field_by_field():
    data = _jpeg()
    # Right length and identifier, wrong contents in each remaining field.
    for offset, value, message in (
        (6, ord("X"), "not a JFIF"),  # identifier
        (11, 9, "JFIF version"),  # version major
        (12, 9, "JFIF version"),  # version minor
        (13, 9, "density unit"),  # units
        (14, 0, "pixel density"),  # x density high byte, low byte is 1 -> 1
        (16, 0, "pixel density"),  # y density high byte
        (18, 1, "embedded thumbnail"),  # thumbnail width
        (19, 1, "embedded thumbnail"),  # thumbnail height
    ):
        broken = bytearray(data)
        if offset in (14, 16):
            broken[offset] = 0
            broken[offset + 1] = 0
        else:
            broken[offset] = value
        with pytest.raises(ValueError, match=message):
            rs.jpeg_dimensions(bytes(broken))
    # A longer APP0, even one that still starts with the JFIF identifier.
    stretched = bytearray(data)
    stretched[4:6] = (18).to_bytes(2, "big")
    stretched[20:20] = b"\x00\x00"
    with pytest.raises(ValueError, match="wrong length"):
        rs.jpeg_dimensions(bytes(stretched))


def test_a_stray_byte_where_a_marker_belongs_fails():
    data = _jpeg()
    sof_at = data.find(b"\xff\xc0")
    broken = bytearray(data)
    broken[sof_at] = 0x00
    with pytest.raises(ValueError, match="expected a JPEG marker"):
        rs.jpeg_dimensions(bytes(broken))


def test_a_declared_segment_length_past_the_end_fails():
    data = _jpeg()
    dqt_at = data.find(b"\xff\xdb")
    oversized = bytearray(data)
    oversized[dqt_at + 2] = 0xFF
    oversized[dqt_at + 3] = 0xFF
    with pytest.raises(ValueError, match="past the end"):
        rs.jpeg_dimensions(bytes(oversized))
    too_short = bytearray(data)
    too_short[dqt_at + 2] = 0x00
    too_short[dqt_at + 3] = 0x01
    with pytest.raises(ValueError, match="minimum"):
        rs.jpeg_dimensions(bytes(too_short))


def test_the_sanitizer_module_never_imports_pillow_or_pdfium():
    # The parent must not hold a decoder; a fresh interpreter proves the
    # import graph is stdlib + protocol only.
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import app.services.rfp_sanitize;"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('PIL', 'pypdfium2'));"
        "sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code, str(root)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


# ── Text contract ────────────────────────────────────────────────────────────


def test_clean_text_is_returned_unchanged_with_every_hazard_key_at_zero():
    text = "Section 26 05 00\n\tConductors: #12 AWG THHN, café, 漢字, 🙂, x\u00a0y, a b"
    clean, hazards = rs.sanitize_text(text)
    assert clean == text
    assert tuple(hazards) == protocol.TEXT_HAZARD_KEYS
    assert hazards == {
        "format_chars": 0,
        "private_use": 0,
        "unassigned": 0,
        "replacement_chars": 0,
    }


def test_newlines_and_tabs_survive_and_crlf_collapses_to_lf():
    # PDFium emits CRLF line endings; the contract keeps line structure but
    # \r is a control character like any other (the child drops it too).
    clean, hazards = rs.sanitize_text("line one\r\nline two\r\n\tindented\n")
    assert clean == "line one\nline two\n\tindented\n"
    assert sum(hazards.values()) == 0


def test_control_characters_are_stripped_without_being_counted():
    raw = "a\x00b\x07c\x1bd\x7fe\x85f\x0bg\x0ch"
    clean, hazards = rs.sanitize_text(raw)
    assert clean == "abcdefgh"
    assert sum(hazards.values()) == 0


@pytest.mark.parametrize(
    "char, key",
    [
        ("\u200b", "format_chars"),  # zero-width space
        ("\u202e", "format_chars"),  # right-to-left override
        ("\u2066", "format_chars"),  # isolate
        ("\ufeff", "format_chars"),  # BOM / ZWNBSP
        ("\u00ad", "format_chars"),  # soft hyphen
        ("\U000e0041", "format_chars"),  # tag character
        ("\ue000", "private_use"),
        ("\U000f0000", "private_use"),
        ("\u0378", "unassigned"),  # unassigned in every Unicode version so far
        ("\ufffe", "unassigned"),  # noncharacter
        ("\ud800", "unassigned"),  # lone surrogate (Cs counts as unassigned)
        ("\ufffd", "replacement_chars"),
    ],
)
def test_each_hidden_category_is_counted_and_stripped(char, key):
    clean, hazards = rs.sanitize_text(f"ab{char}{char}cd{char}")
    assert clean == "abcd"
    assert hazards[key] == 3
    assert sum(hazards.values()) == 3


def test_mixed_hidden_content_is_counted_per_category():
    raw = "Bid\u200b \u202edue\ue000 2026\ufffd\ufffd\u0378\x00!"
    clean, hazards = rs.sanitize_text(raw)
    assert clean == "Bid due 2026!"
    assert hazards == {
        "format_chars": 2,
        "private_use": 1,
        "unassigned": 1,
        "replacement_chars": 2,
    }


def test_sanitizing_twice_is_a_no_op_with_zero_hazards():
    # The parent re-runs the child's cleaner over already-clean text; the
    # stored hazard counts must not double.
    once, first = rs.sanitize_text("x\u200by\ufffdz\r\n")
    twice, second = rs.sanitize_text(once)
    assert twice == once == "xyz\n"
    assert first["format_chars"] == 1 and first["replacement_chars"] == 1
    assert sum(second.values()) == 0


# Every Unicode general category, with the code points the sandbox is most
# likely to meet in each. The text contract is defined per category, so a
# corpus drawn from this table exercises every branch of both implementations.
_CATEGORY_POOL: dict[str, str] = {
    "Lu": "AZ\u0410\u0416",
    "Ll": "az\u00e9\u03c9",
    "Lt": "\u01c5",
    "Lm": "\u02b0",
    "Lo": "\u4e2d\u6587\u0627\u05d0",
    "Mn": "\u0301\u0e31",
    "Mc": "\u0903",
    "Me": "\u20dd",
    "Nd": "09\u0663\uff11",
    "Nl": "\u2160",
    "No": "\u00b2\u2153",
    "Pc": "_\u203f",
    "Pd": "-\u2013",
    "Ps": "([\u300c",
    "Pe": ")]\u300d",
    "Pi": "\u00ab",
    "Pf": "\u00bb",
    "Po": ".,!/\u00bf",
    "Sm": "+<=>\u2211",
    "Sc": "$\u20ac",
    "Sk": "^\u02c2",
    "So": "\u00a9\u2603\U0001f600",
    "Zs": " \u00a0\u3000",
    "Zl": "\u2028",
    "Zp": "\u2029",
    "Cc": "\n\t\r\x00\x07\x0b\x0c\x1b\x7f\x85\x9f",
    "Cf": "\u200b\u202e\u2066\ufeff\u00ad\U000e0041",
    "Co": "\ue000\U000f0000\U0010fffd",
    "Cn": "\u0378\ufffe\U000e0000",
    "Cs": "\ud800\udbff\udfff",
}
_CATEGORY_POOL["So"] += "\ufffd"  # the replacement character is So but counted on its own


def test_category_pool_really_covers_every_general_category():
    import unicodedata

    for category, chars in _CATEGORY_POOL.items():
        for ch in chars:
            assert unicodedata.category(ch) == category, (category, hex(ord(ch)))
    majors = {c[0] for c in _CATEGORY_POOL}
    assert majors == {"L", "M", "N", "P", "S", "Z", "C"}


def _sanitizer_corpus(count: int = 200) -> list[str]:
    """`count` deterministic strings mixing every category, plus one string per
    category made of that category alone, plus the boundary cases."""
    import random

    rng = random.Random(20260909)
    categories = list(_CATEGORY_POOL)
    alphabet = "".join(_CATEGORY_POOL.values())
    corpus = [chars for chars in _CATEGORY_POOL.values()]
    corpus += ["", "plain ascii only\n", "\r\n", "\ufffd", "\ud800\udc00"]
    while len(corpus) < count:
        length = rng.randint(1, 60)
        if rng.random() < 0.3:
            # a run biased towards one category, so rare ones are not drowned
            pool = _CATEGORY_POOL[rng.choice(categories)] + alphabet[: rng.randint(0, 20)]
        else:
            pool = alphabet
        corpus.append("".join(rng.choice(pool) for _ in range(length)))
    return corpus


def test_parent_sanitize_text_matches_the_child_textclean_exactly():
    # The child writes textclean.sanitize(text) to disk and logs its hazard
    # counts; the parent re-runs sanitize_text over what it reads back and
    # keeps max(child, parent). Any disagreement would either alter the stored
    # text a second time or inflate the counts, so the two must be identical.
    from app.sandbox import textclean

    corpus = _sanitizer_corpus()
    assert len(corpus) >= 200
    for raw in corpus:
        parent_clean, parent_hazards = rs.sanitize_text(raw)
        child_clean, child_hazards = textclean.sanitize(raw)
        assert parent_clean == child_clean, repr(raw)
        assert parent_hazards == child_hazards, repr(raw)
        assert tuple(parent_hazards) == protocol.TEXT_HAZARD_KEYS
        # and both are idempotent over each other's output
        assert textclean.sanitize(parent_clean) == (parent_clean, dict.fromkeys(
            protocol.TEXT_HAZARD_KEYS, 0
        ))
        assert rs.sanitize_text(child_clean) == (child_clean, dict.fromkeys(
            protocol.TEXT_HAZARD_KEYS, 0
        ))


def test_empty_text_and_non_strings():
    assert rs.sanitize_text("") == ("", dict.fromkeys(protocol.TEXT_HAZARD_KEYS, 0))
    with pytest.raises(TypeError):
        rs.sanitize_text(b"bytes")


def test_a_large_page_stays_fast_enough():
    # 200k characters (the per-page cap) of mixed text; the fast path must not
    # regress into something that makes a 3000-page file take minutes.
    import time

    page = ("Spec text with é and 漢字 and \u200b hidden bits. " * 5000)[:200000]
    started = time.perf_counter()
    clean, hazards = rs.sanitize_text(page)
    assert time.perf_counter() - started < 2.0
    assert "\u200b" not in clean and hazards["format_chars"] > 0


# ── sanitize_display ─────────────────────────────────────────────────────────


def test_display_strips_hidden_characters_and_collapses_whitespace():
    name = "\u202eInvoice\x00\t\t 2026 \n\n  \u00a0final\u200b.pdf\x1b[0m"
    assert rs.sanitize_display(name) == "Invoice 2026 final.pdf[0m"


def test_display_caps_at_max_chars_after_cleaning():
    assert rs.sanitize_display("a" * 500) == "a" * 200
    assert rs.sanitize_display("a  b  c", max_chars=3) == "a b"
    assert rs.sanitize_display("abc", max_chars=0) == ""
    assert rs.sanitize_display("abc", max_chars=-5) == ""


def test_display_never_raises_and_coerces_odd_inputs():
    class Hostile:
        def __str__(self):
            raise RuntimeError("no")

    assert rs.sanitize_display(Hostile()) == ""
    assert rs.sanitize_display(None) == ""
    assert rs.sanitize_display(42) == "42"
    assert rs.sanitize_display(3.5) == "3.5"
    assert rs.sanitize_display(b"caf\xc3\xa9 \xff.pdf") == "café \ufffd.pdf"
    assert rs.sanitize_display(bytearray(b"x")) == "x"
    assert rs.sanitize_display("") == ""
    assert rs.sanitize_display("   \n\t  ") == ""


def test_display_keeps_printable_unicode_and_the_replacement_char():
    assert rs.sanitize_display("Plan étage 2 : 漢字 \ufffd") == "Plan étage 2 : 漢字 \ufffd"


# ── sanitize_stderr_tail ─────────────────────────────────────────────────────


def test_stderr_tail_keeps_the_last_bytes_and_the_line_structure():
    data = b"Traceback (most recent call last):\n  File \"x.py\", line 1\nMemoryError\n"
    assert rs.sanitize_stderr_tail(data) == (
        'Traceback (most recent call last):\nFile "x.py", line 1\nMemoryError'
    )
    # Only the tail survives when the log is longer than max_bytes.
    assert rs.sanitize_stderr_tail(b"A" * 100 + b"\nTAIL", max_bytes=6) == "A\nTAIL"


def test_stderr_tail_default_is_the_protocol_constant():
    data = b"x" * (protocol.MAX_STDERR_TAIL_BYTES + 50)
    assert len(rs.sanitize_stderr_tail(data)) == protocol.MAX_STDERR_TAIL_BYTES


def test_stderr_tail_scrubs_hidden_characters_and_blank_lines():
    data = b"\x1b[31mred\x1b[0m\r\n\r\n\r\n\xe2\x80\xae hidden \x00nul\t\ttab\n\n"
    assert rs.sanitize_stderr_tail(data) == "[31mred[0m\nhidden nul tab"


def test_stderr_tail_replaces_invalid_utf8_and_a_cut_multibyte_sequence():
    assert rs.sanitize_stderr_tail(b"bad \xff\xfe byte") == "bad \ufffd\ufffd byte"
    # Cutting inside "é" (0xC3 0xA9) leaves a lone continuation byte.
    assert rs.sanitize_stderr_tail("café!".encode(), max_bytes=2) == "\ufffd!"


def test_stderr_tail_caps_at_4096_chars_from_the_end_and_handles_empty_input():
    data = b"HEAD" + b"y" * 5000 + b"TAIL"
    out = rs.sanitize_stderr_tail(data, max_bytes=10_000)
    assert len(out) == 4096 and out.endswith("TAIL") and "HEAD" not in out
    assert rs.sanitize_stderr_tail(b"") == ""
    assert rs.sanitize_stderr_tail(None) == ""
    assert rs.sanitize_stderr_tail(b"abc", max_bytes=0) == ""
    assert rs.sanitize_stderr_tail("already text\n") == "already text"
    assert rs.sanitize_stderr_tail(bytearray(b"ba")) == "ba"
