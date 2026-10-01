"""Security review, group zip-pdf-office: the fixes for

1. the promotion gate's byte-marker scan (`rfp_create_files.pdf_marker_keys`)
   decoding `#xx` name escapes and looking inside FlateDecode streams
   (object streams), failing closed on anything it cannot see through;
2. `rfp_zip.inspect` / `extract_member` reading the archive's end record
   (ZIP64 honoured) before `zipfile` parses the central directory, and the
   archive-magic peek never opening members past the kept cap;
6. `ooxml_container_verdict` using the same end-record pre-check and
   reading inspected members in bounded chunks under a running counter,
   never at their declared size;
15. `rfp_office_convert.convert_to_pdf` refusing a `.docx` / `.xlsx` with
   external relationships, embedded objects or DDE / INCLUDE fields before
   any byte reaches LibreOffice.

No network, no database: in-memory zips and PDFs, httpx.MockTransport.
"""

from __future__ import annotations

import hashlib
import io
import random
import struct
import zipfile
import zlib
from pathlib import Path

import httpx
import pytest

from app.sandbox import protocol
from app.services import rfp_create_files as rcf
from app.services import rfp_office_convert as oc
from app.services import rfp_zip

# ── helpers ──────────────────────────────────────────────────────────────


def _pdf(objs: list[tuple[bytes, bytes | None]]) -> bytes:
    """A PDF-shaped byte string: `objs` are (dictionary, stream body or
    None) in object-number order."""
    out = b"%PDF-1.5\n"
    for i, (head, body) in enumerate(objs, 1):
        out += f"{i} 0 obj\n".encode() + head
        if body is not None:
            out += b"\nstream\n" + body + b"\nendstream"
        out += b"\nendobj\n"
    return out + b"trailer << /Root 1 0 R >>\n%%EOF\n"


def _flate(head: bytes, plain: bytes) -> tuple[bytes, bytes]:
    comp = zlib.compress(plain)
    return head.replace(b"%LEN%", str(len(comp)).encode()), comp


def _zip_bytes(members: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _docx(**extra: bytes) -> dict[str, bytes]:
    members = {
        "[Content_Types].xml": b"<Types/>",
        "_rels/.rels": b"<Relationships/>",
        "word/_rels/document.xml.rels": (
            b'<Relationships><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            b'2006/relationships/hyperlink" Target="https://example.com/spec" TargetMode="External"/></Relationships>'
        ),
        "word/document.xml": b"<w:document/>",
    }
    members.update(extra)
    return members


def _patch_declared_size(data: bytes, name: str, declared: int) -> bytes:
    """Rewrite the uncompressed size of member `name` in both the local
    header (offset 22) and the central directory entry (offset 24): what
    a lying archive declares."""
    out = bytearray(data)
    encoded = name.encode()
    for sig, size_at, name_at in ((b"PK\x03\x04", 22, 30), (b"PK\x01\x02", 24, 46)):
        pos = 0
        while (pos := out.find(sig, pos)) != -1:
            if out[pos + name_at:pos + name_at + len(encoded)] == encoded:
                out[pos + size_at:pos + size_at + 4] = struct.pack("<I", declared)
            pos += 4
    return bytes(out)


def _with_zip64(data: bytes, entries: int, cd_size: int) -> bytes:
    """`data` with a ZIP64 end record and locator planted right before the
    end record, the layout `zipfile` reads them from."""
    out = bytearray(data)
    eocd_at = out.rfind(b"PK\x05\x06")
    record = b"PK\x06\x06" + struct.pack("<QHHIIQQQQ", 44, 45, 45, 0, 0, entries, entries, cd_size, 0)
    locator = b"PK\x06\x07" + struct.pack("<IQI", 0, eocd_at, 1)
    out[eocd_at:eocd_at] = record + locator
    return bytes(out)


def _no_zipfile(monkeypatch, module) -> None:
    """Make the module's `zipfile.ZipFile` blow up: the check under test
    has to answer before the directory is parsed."""
    real = module.zipfile

    class Guard:
        def __getattr__(self, name):
            if name == "ZipFile":
                raise AssertionError("zipfile.ZipFile was constructed before the end-record check")
            return getattr(real, name)

    monkeypatch.setattr(module, "zipfile", Guard())


# ── 1. the byte-marker scan ──────────────────────────────────────────────


def test_escaped_names_are_decoded_before_matching():
    """`/Open#41ction` and `/J#53` are what a PDF parser reads as
    /OpenAction and /JS; a lookalike that decodes to a longer name is not."""
    assert rcf.pdf_marker_keys(b"<< /Open#41ction << /S /Java#53cript /J#53 (x) >> >>") == [
        "/JS", "/JavaScript", "/OpenAction",
    ]
    assert rcf.pdf_marker_keys(b"/Open#41ctions 1 /J#53X /AAPL:Ke#79words /U#52I (x)") == []
    # Fully escaped, and an escape that lands right at the end of the file.
    assert rcf.pdf_marker_keys(b"/#4A#53") == ["/JS"]
    assert rcf.pdf_marker_keys(b"/#4C#61#75#6E#63#68") == ["/Launch"]


def test_object_stream_contents_are_scanned():
    """A catalog with /OpenAction JavaScript inside a FlateDecode /ObjStm
    is found: with a direct /Length, an indirect one, and with the
    stream's own dictionary written with `#xx` escapes."""
    inner = b"1 0 2 60 << /Type /Catalog /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >> << /Type /Pages >>"
    expect = ["/JS", "/JavaScript", "/OpenAction"]
    direct = _pdf([_flate(b"<< /Type /ObjStm /N 2 /First 8 /Filter /FlateDecode /Length %LEN% >>", inner)])
    assert rcf.pdf_marker_keys(direct) == expect
    head, comp = _flate(b"<< /Type /ObjStm /N 2 /First 8 /Filter /FlateDecode /Length 2 0 R >>", inner)
    indirect = _pdf([(head, comp), (str(len(comp)).encode(), None)])
    assert rcf.pdf_marker_keys(indirect) == expect
    escaped = _pdf([_flate(
        b"<< /Type /Obj#53tm /Filter /Flate#44ecode /Length %LEN% >>",
        b"<< /Type /Catalog /Open#41ction << /S /JavaScript /J#53 (x) >> >>",
    )])
    assert rcf.pdf_marker_keys(escaped) == expect
    # A filter array with FlateDecode alone is plain Flate too.
    array = _pdf([_flate(b"<< /Type /ObjStm /Filter [ /FlateDecode ] /Length %LEN% >>", inner)])
    assert rcf.pdf_marker_keys(array) == expect
    # A marker split across the inflate chunk boundary is still one token.
    pad = b"x" * ((1 << 20) - 3)
    assert rcf.pdf_marker_keys(_pdf([_flate(b"<< /Filter /FlateDecode /Length %LEN% >>", pad + b"/JS (x)")])) == ["/JS"]
    assert rcf.pdf_marker_keys(_pdf([_flate(b"<< /Filter /FlateDecode /Length %LEN% >>", pad + b"/JSX")])) == []


def test_a_clean_pdf_with_flate_streams_is_clean():
    """Content streams, a predictor-encoded image, a DCT image, a filter
    chain on an ordinary stream and a clean object stream: nothing
    refused, nothing found."""
    clean = _pdf([
        _flate(b"<< /Type /ObjStm /Filter /FlateDecode /Length %LEN% >>", b"<< /Type /Catalog /Pages 2 0 R >>"),
        _flate(b"<< /Filter /FlateDecode /Length %LEN% >>", b"q 1 0 0 1 0 0 cm BT /F1 12 Tf (Hello) Tj ET Q"),
        _flate(
            b"<< /Type /XObject /Subtype /Image /Filter /FlateDecode "
            b"/DecodeParms << /Predictor 12 /Columns 10 >> /Length %LEN% >>",
            b"\x00" * 1000,
        ),
        (b"<< /Filter /DCTDecode /Length 5 >>", b"\xff\xd8\xff\xe0\x00"),
        (b"<< /Filter [/ASCIIHexDecode /FlateDecode] /Length 4 >>", b"ZZZZ"),
        (b"<< /AAPL:Keywords [(x)] /URI (https://g3electrical.com) >>", None),
    ])
    assert rcf.pdf_marker_keys(clean) == []


@pytest.mark.parametrize("reason, objs", [
    ("objstm_filter", [(b"<< /Type /ObjStm /Filter /LZWDecode /Length 4 >>", b"abcd")]),
    ("objstm_filter", [(b"<< /Type /ObjStm /Filter [/ASCIIHexDecode /FlateDecode] /Length 4 >>", b"abcd")]),
    ("objstm_decode_parms", [_flate(
        b"<< /Type /ObjStm /Filter /FlateDecode /DecodeParms << /Predictor 12 >> /Length %LEN% >>", b"<< >>"
    )]),
    ("stream_corrupt", [(b"<< /Filter /FlateDecode /Length 6 >>", b"\x00\x01\x02\x03\x04\x05")]),
    ("objstm_length", [(b"<< /Type /ObjStm /Filter /FlateDecode /Length 9 0 R >>", zlib.compress(b"<< >>"))]),
])
def test_streams_the_scan_cannot_see_through_fail_closed(reason, objs):
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(_pdf(objs))
    assert exc.value.reason == reason


def test_an_object_stream_without_endstream_fails_closed():
    data = b"1 0 obj << /Type /ObjStm /Filter /FlateDecode >> stream\n" + zlib.compress(b"<< >>")
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(data)
    assert exc.value.reason == "objstm_length"


def test_the_inflate_caps_fail_closed(monkeypatch):
    big = _pdf([_flate(b"<< /Filter /FlateDecode /Length %LEN% >>", b"A" * 5000)])
    monkeypatch.setattr(rcf, "PDF_INFLATE_STREAM_CAP", 1000)
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(big)
    assert exc.value.reason == "stream_too_large"
    monkeypatch.setattr(rcf, "PDF_INFLATE_STREAM_CAP", 10_000)
    monkeypatch.setattr(rcf, "PDF_INFLATE_TOTAL_CAP", 6000)
    two = _pdf([
        _flate(b"<< /Filter /FlateDecode /Length %LEN% >>", b"A" * 4000),
        _flate(b"<< /Filter /FlateDecode /Length %LEN% >>", b"B" * 4000),
    ])
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(two)
    assert exc.value.reason == "streams_too_large"


def test_fetch_verified_never_promotes_a_hidden_or_unscannable_pdf(monkeypatch, tmp_path):
    """End to end through the promotion gate: escaped names and object
    streams are `marker:` skips, an unscannable file is an
    `unscannable:<reason>` skip, and a clean object-stream PDF passes."""
    def _serve(data: bytes):
        def fake_download(bucket, path, dest, *, max_bytes):
            dest.write_bytes(data)
            return len(data)
        monkeypatch.setattr(rcf.rs, "download_to_file", fake_download)

    def _fetch(data: bytes):
        _serve(data)
        decision = rcf.Promote("rfp-quarantine", "r/f/source.pdf", "a.pdf", "application/pdf", True,
                               expected_sha=hashlib.sha256(data).hexdigest())
        return rcf.fetch_verified(decision, tmp_path / "a", 10_000_000)

    escaped = b"%PDF-1.7\n1 0 obj << /Open#41ction << /S /Java#53cript /J#53 (x) >> >> endobj"
    out = _fetch(escaped)
    assert isinstance(out, rcf.Skip) and out.reason == "marker:/JS,/JavaScript,/OpenAction"
    hidden = _pdf([_flate(
        b"<< /Type /ObjStm /Filter /FlateDecode /Length %LEN% >>",
        b"<< /Type /Catalog /OpenAction << /S /JavaScript /JS (x) >> >>",
    )])
    out = _fetch(hidden)
    assert isinstance(out, rcf.Skip) and out.reason == "marker:/JS,/JavaScript,/OpenAction"
    lzw = _pdf([(b"<< /Type /ObjStm /Filter /LZWDecode /Length 4 >>", b"abcd")])
    out = _fetch(lzw)
    assert isinstance(out, rcf.Skip) and out.reason == "unscannable:objstm_filter"
    clean = _pdf([_flate(b"<< /Type /ObjStm /Filter /FlateDecode /Length %LEN% >>", b"<< /Type /Catalog >>")])
    assert _fetch(clean) == clean


# ── 2. rfp_zip: the end record before the directory ──────────────────────


def _inspect(path, **over):
    kw = dict(max_members=200, max_total_bytes=10 * 1024 * 1024, per_member_max_bytes=1024 * 1024,
              is_image_name=lambda name: name.lower().endswith((".jpg", ".png")))
    kw.update(over)
    return rfp_zip.inspect(path, **kw)


def test_central_directory_bounds_read_the_end_record_and_zip64():
    small = _zip_bytes({"a.txt": b"x"})
    assert rfp_zip.central_directory_bounds(io.BytesIO(small)) == (1, 51)
    many = _zip_bytes({f"f{i}.txt": b"x" for i in range(3000)})
    entries, cd_size = rfp_zip.central_directory_bounds(io.BytesIO(many))
    assert entries == 3000 and cd_size > 100_000
    assert rfp_zip.central_directory_bounds(io.BytesIO(b"%PDF-1.7 not a zip")) is None
    # A file object is left where it was.
    buf = io.BytesIO(small)
    buf.seek(7)
    rfp_zip.central_directory_bounds(buf)
    assert buf.tell() == 7
    # A ZIP64 locator wins over the small numbers beside it, the way
    # zipfile itself reads it; a locator with no record is the ceiling.
    assert rfp_zip.central_directory_bounds(io.BytesIO(_with_zip64(small, 5_000_000, 100))) == (5_000_000, 100)
    broken = bytearray(_with_zip64(small, 5, 51))
    broken[broken.rfind(b"PK\x06\x06"):broken.rfind(b"PK\x06\x06") + 4] = b"XXXX"
    entries, _ = rfp_zip.central_directory_bounds(io.BytesIO(bytes(broken)))
    assert entries == 0xFFFFFFFFFFFFFFFF


def test_precheck_names_each_refusal():
    small = _zip_bytes({"a.txt": b"x"})
    assert rfp_zip.precheck(io.BytesIO(small)) is None
    assert rfp_zip.precheck(io.BytesIO(small), max_entries=0) == "too_many_entries"
    assert rfp_zip.precheck(io.BytesIO(small), max_central_directory_bytes=50) == "central_directory_too_large"
    assert rfp_zip.precheck(io.BytesIO(_with_zip64(small, 5_000_000, 100))) == "too_many_entries"
    assert rfp_zip.precheck(io.BytesIO(b"nope")) is None


def test_inspect_refuses_a_many_entry_zip_before_zipfile_parses_it(monkeypatch, tmp_path):
    path = tmp_path / "many.zip"
    path.write_bytes(_zip_bytes({f"f{i}.pdf": b"%PDF" for i in range(rfp_zip.MAX_ENTRIES + 1)}))
    _no_zipfile(monkeypatch, rfp_zip)
    listing = _inspect(path)
    assert listing.members == [] and listing.error_kind == "too_many_entries"
    assert listing.error == "The zip has more entries than the harvest accepts."
    # extract_member is guarded the same way.
    with pytest.raises(rfp_zip.ZipError):
        rfp_zip.extract_member(path, 0, tmp_path / "out.pdf", max_bytes=1000)
    assert not (tmp_path / "out.pdf").exists()


def test_inspect_still_lists_an_archive_under_the_bounds(tmp_path):
    path = tmp_path / "ok.zip"
    path.write_bytes(_zip_bytes({f"f{i}.pdf": b"%PDF-1.7 x" for i in range(rfp_zip.MAX_ENTRIES)}))
    listing = _inspect(path, max_members=rfp_zip.MAX_ENTRIES)
    assert listing.error is None and len(listing.members) == rfp_zip.MAX_ENTRIES


def test_members_past_the_kept_cap_are_never_opened_for_the_magic_peek(monkeypatch, tmp_path):
    """Five members, a kept cap of two: the peek opens the first two and
    the rest are dropped unopened (before, every member was opened)."""
    path = tmp_path / "a.zip"
    path.write_bytes(_zip_bytes({f"f{i}.pdf": b"%PDF-1.7 " + b"x" * 100 for i in range(5)}))
    opened: list[str] = []
    real = rfp_zip._archive_by_magic

    def counting(zf, info):
        opened.append(info.filename)
        return real(zf, info)

    monkeypatch.setattr(rfp_zip, "_archive_by_magic", counting)
    listing = _inspect(path, max_members=2)
    assert listing.truncated
    assert [m.path for m in listing.members] == ["f0.pdf", "f1.pdf"]
    assert opened == ["f0.pdf", "f1.pdf"]


def test_read_member_bounded_counts_the_bytes_that_come_out():
    data = _zip_bytes({"a.txt": b"A" * 300_000, "b.txt": b"B" * 10})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        a, b = zf.infolist()
        assert rfp_zip.read_member_bounded(zf, b, 100) == b"B" * 10
        assert rfp_zip.read_member_bounded(zf, a, 100) is None
        assert rfp_zip.read_member_bounded(zf, a, 300_000) == b"A" * 300_000
    # A lying declared size does not change what is counted: the inflated
    # bytes are bounded by zipfile to the declaration and the CRC then fails.
    lying = _patch_declared_size(_zip_bytes({"a.txt": b"A" * 300_000}), "a.txt", 10)
    with zipfile.ZipFile(io.BytesIO(lying)) as zf:
        info = zf.infolist()[0]
        assert info.compress_size < 1000
        with pytest.raises(zipfile.BadZipFile):
            rfp_zip.read_member_bounded(zf, info, 1000)


# ── 6. the OOXML container scan ──────────────────────────────────────────


def test_ooxml_verdict_refuses_by_the_end_record_before_zipfile_parses(monkeypatch):
    many = _zip_bytes(_docx(**{f"word/media/i{i}.png": b"p" for i in range(rcf.OOXML_MAX_MEMBERS)}))
    _no_zipfile(monkeypatch, rcf)
    assert rcf.ooxml_container_verdict(io.BytesIO(many)) == "too_many_members"
    small = _zip_bytes(_docx())
    monkeypatch.setattr(rcf, "OOXML_MAX_CENTRAL_DIRECTORY_BYTES", 10)
    assert rcf.ooxml_container_verdict(io.BytesIO(small)) == "central_directory_too_large"


def test_ooxml_verdict_reads_inspected_members_in_bounded_chunks(monkeypatch):
    """Members are read through `ZipExtFile.read(n)` with a small `n`,
    never through `ZipFile.read` (which inflates a whole member in one
    call whatever its declared size), so a member declared at 10 bytes
    that inflates to megabytes costs one chunk at a time."""
    reads: list[int] = []
    real_read = zipfile.ZipExtFile.read

    def recording(self, n=-1):
        reads.append(n)
        return real_read(self, n)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", recording)
    monkeypatch.setattr(
        zipfile.ZipFile, "read", lambda self, *a, **k: pytest.fail("ZipFile.read inflates a whole member")
    )
    docx = _zip_bytes(_docx())
    assert rcf.ooxml_container_verdict(io.BytesIO(docx)) is None
    assert reads and all(0 < n <= rfp_zip.MEMBER_READ_CHUNK for n in reads)
    reads.clear()
    lying = _patch_declared_size(
        _zip_bytes(_docx(**{"word/_rels/x.rels": b"<Relationships/>" + b" " * (3 * 1024 * 1024)})),
        "word/_rels/x.rels", 10,
    )
    assert rcf.ooxml_container_verdict(io.BytesIO(lying)) in ("member_too_large", "bad_zip")
    assert reads and all(0 < n <= rfp_zip.MEMBER_READ_CHUNK for n in reads)


def test_ooxml_verdict_refuses_an_inspected_member_by_compressed_or_inflated_size(monkeypatch):
    fat = b"<Relationships>" + b" " * (rcf.OOXML_MAX_MEMBER_BYTES + 1) + b"</Relationships>"
    assert rcf.ooxml_container_verdict(io.BytesIO(_zip_bytes(_docx(**{"word/_rels/a.rels": fat})))) == "member_too_large"
    # Declared small, compressed large (incompressible bytes).
    noisy = random.Random(7).randbytes(rcf.OOXML_MAX_MEMBER_BYTES + 4096)
    lying = _patch_declared_size(_zip_bytes(_docx(**{"word/_rels/n.rels": noisy})), "word/_rels/n.rels", 10)
    assert rcf.ooxml_container_verdict(io.BytesIO(lying)) == "member_too_large"
    # Declared small, compressed small, inflating past the cap: the running
    # counter (a low cap makes the declaration the larger of the two).
    monkeypatch.setattr(rcf, "OOXML_MAX_MEMBER_BYTES", 100)
    rels = b"<Relationships/>" + b" " * 5000
    lying = _patch_declared_size(_zip_bytes(_docx(**{"word/_rels/s.rels": rels})), "word/_rels/s.rels", 10)
    assert rcf.ooxml_container_verdict(io.BytesIO(lying)) in ("member_too_large", "bad_zip")
    monkeypatch.setattr(rcf, "OOXML_MAX_MEMBER_BYTES", 2 * 1024 * 1024)
    assert rcf.ooxml_container_verdict(io.BytesIO(_zip_bytes(_docx()))) is None


# ── 15. the pre-conversion scan ──────────────────────────────────────────

BASE = "http://gotenberg.test:3000"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"


def _convert(tmp_path: Path, data: bytes, handler, fmt=protocol.SOURCE_FORMAT_DOCX):
    src = tmp_path / f"source.{fmt}"
    src.write_bytes(data)
    dest = tmp_path / "source.pdf"
    info = oc.convert_to_pdf(
        src, dest, source_format=fmt, max_bytes=1 << 20, timeout_seconds=30, base_url=BASE,
        transport=httpx.MockTransport(handler),
    )
    return info, dest


def _never(request: httpx.Request) -> httpx.Response:
    pytest.fail("the document reached the converter")


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PDF)


EXTERNAL_IMAGE = (
    b'<Relationships><Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    b'relationships/image" Target="http://10.0.0.5/canary.png" TargetMode="External"/></Relationships>'
)


@pytest.mark.parametrize("scan, members", [
    ("external_rel:image", {"word/_rels/document.xml.rels": EXTERNAL_IMAGE}),
    ("external_rel:attachedtemplate", {"word/_rels/settings.xml.rels": (
        b"<Relationships><Relationship Id='r' Type='.../relationships/attachedTemplate' "
        b"Target='http://x/t.dotm' targetmode='EXTERNAL'/></Relationships>"
    )}),
    ("external_rel:oleobject", {"word/_rels/document.xml.rels": (
        b'<Relationships><Relationship Id="r" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        b'relationships/oleObject" Target="file:///C:/x.bin" TargetMode="External"/></Relationships>'
    )}),
    ("embedding", {"word/embeddings/oleObject1.bin": b"\xd0\xcf"}),
    ("embedding", {"word/media/oleObject7.emf": b"x"}),
    ("external_link", {"xl/externalLinks/externalLink1.xml": b"<x/>"}),
    ("dde", {"word/document.xml": b'<w:fldSimple w:instr=" DDEAUTO c:\\x.exe /c calc "/>'}),
    ("dde", {"word/settings.xml": b"<w:ddeLink/>"}),
    ("include_field", {"word/header1.xml": b"<w:instrText> INCLUDEPICTURE \"http://x/a.png\" </w:instrText>"}),
    ("include_field", {"word/document.xml": b"<w:instrText> INCLUDETEXT \"\\\\host\\share\\x.docx\" </w:instrText>"}),
])
def test_a_docx_that_reaches_outside_itself_never_reaches_the_converter(tmp_path, scan, members):
    with pytest.raises(oc.ConversionRejected) as exc:
        _convert(tmp_path, _zip_bytes(_docx(**members)), _never)
    assert exc.value.info["reason"] == oc.REASON_EXTERNAL_CONTENT
    assert exc.value.info["scan"] == scan
    assert exc.value.info["http_status"] is None
    assert str(exc.value) == f"The document reaches outside itself ({scan}) and was not converted."
    assert not (tmp_path / "source.pdf").exists()


def test_a_plain_docx_with_external_hyperlinks_still_converts(tmp_path):
    info, dest = _convert(tmp_path, _zip_bytes(_docx()), _ok)
    assert info["http_status"] == 200 and dest.read_bytes() == PDF and "scan" not in info


def test_an_xlsx_is_scanned_and_the_legacy_binaries_are_not(tmp_path):
    with pytest.raises(oc.ConversionRejected) as exc:
        _convert(tmp_path, _zip_bytes({
            "[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<w/>",
            "xl/_rels/workbook.xml.rels": EXTERNAL_IMAGE,
        }), _never, fmt=protocol.SOURCE_FORMAT_XLSX)
    assert exc.value.info["scan"] == "external_rel:image"
    # .doc / .xls are OLE2 binaries, not containers: they go as they are.
    ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
    for fmt in (protocol.SOURCE_FORMAT_DOC, protocol.SOURCE_FORMAT_XLS):
        info, dest = _convert(tmp_path, ole2, _ok, fmt=fmt)
        assert info["http_status"] == 200
        dest.unlink()


def test_a_container_the_scan_cannot_open_is_not_converted(tmp_path, monkeypatch):
    with pytest.raises(oc.ConversionRejected) as exc:
        _convert(tmp_path, b"PK\x03\x04" + b"not really a zip" * 20, _never)
    assert exc.value.info["scan"] == "bad_zip"
    # The end record is checked before zipfile parses the directory.
    many = _zip_bytes(_docx(**{f"word/media/i{i}.png": b"p" for i in range(oc.SCAN_MAX_MEMBERS)}))
    _no_zipfile(monkeypatch, oc)
    assert oc.external_content_verdict(_write(tmp_path, many)) == "too_many_members"


def _write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "scan.docx"
    path.write_bytes(data)
    return path


def test_external_content_verdict_bounds_the_parts_it_reads(tmp_path, monkeypatch):
    assert oc.external_content_verdict(_write(tmp_path, _zip_bytes(_docx()))) is None
    monkeypatch.setattr(oc, "SCAN_MAX_PART_BYTES", 100)
    big = _zip_bytes(_docx(**{"word/document.xml": b"<w:document>" + b" " * 5000 + b"</w:document>"}))
    assert oc.external_content_verdict(_write(tmp_path, big)) == "member_too_large"
    monkeypatch.setattr(oc, "SCAN_MAX_PART_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(oc, "SCAN_MAX_RELS_BYTES", 10)
    assert oc.external_content_verdict(_write(tmp_path, _zip_bytes(_docx()))) == "member_too_large"


# ── 1 (retest). the stream walk parses, it never guesses ─────────────────

OBJSTM_INNER = b"<< /Type /Catalog /OpenAction << /S /JavaScript /JS (x) >> >>"
OBJSTM_KEYS = ["/JS", "/JavaScript", "/OpenAction"]


def _objstm(head: bytes, kw: bytes = b"stream\n") -> bytes:
    """One FlateDecode object stream carrying OBJSTM_INNER, `%LEN%` in
    `head` replaced, `kw` the exact bytes written for the stream keyword
    and its line ending."""
    comp = zlib.compress(OBJSTM_INNER)
    head = head.replace(b"%LEN%", str(len(comp)).encode())
    return b"%PDF-1.5\n1 0 obj\n" + head + b"\n" + kw + comp + b"\nendstream\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


@pytest.mark.parametrize("head", [
    # A legal extra key whose name contains "obj" after /Type and /Filter
    # (the old window started after it and saw neither).
    b"<< /Type /ObjStm /Filter /FlateDecode /Xobj 1 /Length %LEN% >>",
    # The words the old window keyed on, inside a string in the dictionary.
    b"<< /Type /ObjStm /Filter /FlateDecode /T (endstream) /Length %LEN% >>",
    b"<< /Type /ObjStm /Filter /FlateDecode /T (1 0 obj << /Length 5 >>) /Length %LEN% >>",
    b"<< /Type /ObjStm /Filter /FlateDecode /T ( << ) /Length %LEN% >>",
    b"<< /Type /ObjStm /Filter /FlateDecode /T <3c3c> /Length %LEN% >> % obj\n",
    # A nested dictionary, an array of references, a hex string.
    b"<< /Type /ObjStm /A [1 0 R 2 0 R <41 42> (x\\)y) << /B [] >>] /Filter /FlateDecode /Length %LEN% >>",
    # /Type absent: /N is what pdf.js and MuPDF load an object stream by.
    b"<< /N 1 /First 5 /Filter /FlateDecode /Length %LEN% >>",
])
def test_the_stream_dictionary_is_parsed_whatever_it_carries(head):
    assert rcf.pdf_marker_keys(_objstm(head)) == OBJSTM_KEYS


@pytest.mark.parametrize("kw", [b"stream\r\n", b"stream\r", b"stream \n", b"stream\t\r\n", b"stream\n"])
def test_every_line_ending_after_the_stream_keyword_is_walked(kw):
    """Lenient readers accept a bare `\\r` and trailing blanks after the
    keyword; the old walk matched `stream\\r?\\n` only."""
    assert rcf.pdf_marker_keys(_objstm(b"<< /Type /ObjStm /Filter /FlateDecode /Length %LEN% >>", kw)) == OBJSTM_KEYS


def test_junk_on_the_stream_keyword_line_is_ambiguous_and_fails_closed():
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(_objstm(b"<< /Type /ObjStm /Filter /FlateDecode /Length %LEN% >>", b"stream junk\n"))
    assert exc.value.reason == "stream_eol"


@pytest.mark.parametrize("reason, objs", [
    # /Type absent behind another filter: an object stream to a reader that
    # never checks /Type, so it is refused, not skipped.
    ("objstm_filter", [(b"<< /N 1 /First 5 /Filter /ASCIIHexDecode /Length 4 >>", b"abcd")]),
    # An extra "obj" key after the filter no longer hides the filter.
    ("objstm_filter", [(b"<< /Type /ObjStm /Filter /ASCIIHexDecode /Xobj 1 /Length 4 >>", b"abcd")]),
    # /Type given indirectly (PDFium and pypdf resolve it), /Filter given
    # indirectly, a predictor through the /DP abbreviation.
    ("objstm_filter", [(b"<< /Type 9 0 R /Filter /LZWDecode /Length 4 >>", b"abcd")]),
    ("objstm_filter", [(b"<< /Type /ObjStm /Filter 9 0 R /Length 4 >>", b"abcd")]),
    ("objstm_decode_parms", [_flate(
        b"<< /Type /ObjStm /Filter /FlateDecode /DP << /Predictor 12 >> /Length %LEN% >>", b"<< >>"
    )]),
    ("objstm_decode_parms", [_flate(
        b"<< /Type /ObjStm /Filter /FlateDecode /DecodeParms 9 0 R /Length %LEN% >>", b"<< >>"
    )]),
    # A deflate stream that ends before its end marker: what a reader loads
    # from it cannot be settled.
    ("objstm_truncated", [(b"<< /Type /ObjStm /Filter /FlateDecode /Length 10 >>", zlib.compress(OBJSTM_INNER)[:10])]),
])
def test_an_object_stream_is_known_by_more_than_a_type_name(reason, objs):
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(_pdf(objs))
    assert exc.value.reason == reason


def test_a_stream_keyword_no_object_accounts_for_fails_closed():
    comp = zlib.compress(OBJSTM_INNER)
    headless = b"%PDF\n<< /Type /ObjStm /Filter /FlateDecode /Length " + str(len(comp)).encode() + b" >>\nstream\n" + comp + b"\nendstream"
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(headless)
    assert exc.value.reason == "stream_unparsed"
    # The keyword inside a string of an object no header reaches.
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(b"%PDF\n1 0 obj << /A 1 >> endobj trailer << /T (a stream) >>")
    assert exc.value.reason == "stream_unparsed"
    # A dictionary past the per-object cap leaves its stream unaccounted.
    huge = b"%PDF\n1 0 obj << /T (" + b"x" * (rcf.PDF_PARSE_OBJECT_CAP + 10) + b") /Filter /FlateDecode >>\nstream\n" + comp + b"\nendstream"
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(huge)
    assert exc.value.reason == "stream_unparsed"


def test_the_keyword_inside_a_parsed_object_or_a_body_is_accounted_for():
    assert rcf.pdf_marker_keys(b"%PDF\n1 0 obj << /T (a stream) >> endobj") == []
    assert rcf.pdf_marker_keys(_pdf([(b"<< /Length 20 >>", b"BT (a stream) Tj ET")])) == []
    # A comment between the dictionary and the keyword, and a truncated
    # image stream, are ordinary.
    comp = zlib.compress(b"<< /Type /Catalog >>")
    assert rcf.pdf_marker_keys(
        b"%PDF\n1 0 obj << /Type /ObjStm /Filter /FlateDecode /Length " + str(len(comp)).encode()
        + b" >> % note\nstream\n" + comp + b"\nendstream endobj"
    ) == []
    assert rcf.pdf_marker_keys(_pdf([(b"<< /Subtype /Image /Filter /FlateDecode /Length 10 >>", comp[:10])])) == []
    # An ICC profile stream carries /N too: plain Flate, so it is scanned, not refused.
    assert rcf.pdf_marker_keys(_pdf([_flate(b"<< /N 3 /Alternate /DeviceRGB /Filter /FlateDecode /Length %LEN% >>", b"icc")])) == []


def test_the_tokenizing_budget_fails_closed(monkeypatch):
    data = _pdf([(b"<< /T (" + b"x" * 5000 + b") >>", None)] * 3)
    monkeypatch.setattr(rcf, "PDF_PARSE_BUDGET", 8000)
    with pytest.raises(rcf.PdfUnscannable) as exc:
        rcf.pdf_marker_keys(data)
    assert exc.value.reason == "scan_budget"


# ── 15 (retest). the OOXML parts are read as XML ─────────────────────────

REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def _rels(body: str, encoding: str = "utf-8", prefix: str = "") -> bytes:
    declaration = f'<?xml version="1.0" encoding="{encoding}"?>' if encoding != "utf-8" else ""
    return f"{declaration}{prefix}{body}".encode(encoding)


@pytest.mark.parametrize("label, members", [
    ("entity-encoded TargetMode", {"word/_rels/document.xml.rels": _rels(
        f'<Relationships><Relationship Id="r" Type="{REL_NS}image" Target="http://example.invalid/c.png" '
        'TargetMode="&#69;xternal"/></Relationships>'
    )}),
    ("prefixed element", {"word/_rels/document.xml.rels": _rels(
        '<p:Relationships xmlns:p="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<p:Relationship Id="r" Type="{REL_NS}image" Target="http://example.invalid/c.png" TargetMode="External"/>'
        "</p:Relationships>"
    )}),
    ("unbound prefix", {"word/_rels/document.xml.rels": _rels(
        f'<Relationships><p:Relationship Id="r" Type="{REL_NS}image" Target="http://example.invalid/c.png" '
        'TargetMode="External"/></Relationships>'
    )}),
    ("UTF-16 part", {"word/_rels/document.xml.rels": _rels(
        f'<Relationships><Relationship Id="r" Type="{REL_NS}image" Target="http://example.invalid/c.png" '
        'TargetMode="External"/></Relationships>', encoding="utf-16",
    )}),
    ("URL target with no TargetMode", {"word/_rels/document.xml.rels": _rels(
        f'<Relationships><Relationship Id="r" Type="{REL_NS}image" Target="http://example.invalid/c.png"/></Relationships>'
    )}),
    ("UNC target", {"word/_rels/document.xml.rels": _rels(
        f'<Relationships><Relationship Id="r" Type="{REL_NS}image" Target="\\\\host\\share\\c.png"/></Relationships>'
    )}),
])
def test_an_external_relationship_is_seen_however_the_xml_spells_it(tmp_path, label, members):
    with pytest.raises(oc.ConversionRejected) as exc:
        _convert(tmp_path, _zip_bytes(_docx(**members)), _never)
    assert exc.value.info["scan"] == "external_rel:image", label


def test_a_hyperlink_lookalike_type_is_not_a_hyperlink(tmp_path):
    rels = _rels(
        f'<Relationships><Relationship Id="r" Type="{REL_NS}image-hyperlink" Target="http://example.invalid/" '
        'TargetMode="External"/></Relationships>'
    )
    assert oc.external_content_verdict(_write(tmp_path, _zip_bytes(_docx(**{"word/_rels/document.xml.rels": rels})))) == (
        "external_rel:imagehyperlink"
    )


@pytest.mark.parametrize("scan, members", [
    ("include_field", {"word/document.xml": (
        b"<w:document><w:r><w:instrText> INCLUDE</w:instrText></w:r>"
        b'<w:r><w:instrText>PICTURE "http://example.invalid/a.png"</w:instrText></w:r></w:document>'
    )}),
    ("dde", {"word/document.xml": b"<w:document><w:fldSimple w:instr=' &#68;DEAUTO c:\\x.exe '/></w:document>"}),
    ("include_field", {"word/document.xml": (
        '<?xml version="1.0" encoding="utf-16"?><w:document><w:instrText>INCLUDETEXT "x"</w:instrText></w:document>'
    ).encode("utf-16")}),
    ("include_field", {"word/glossary/document.xml": b"<w:document><w:instrText>INCLUDEPICTURE x</w:instrText></w:document>"}),
    ("bad_xml", {"word/document.xml": b"<w:document><w:r>"}),
    ("bad_xml", {"word/_rels/document.xml.rels": (
        b'<!DOCTYPE r [<!ENTITY e "External">]><Relationships><Relationship Id="r" Type="x/image" '
        b'Target="http://example.invalid/" TargetMode="&e;"/></Relationships>'
    )}),
    ("data_connection", {"xl/connections.xml": b"<connections/>"}),
    ("data_connection", {"xl/queryTables/queryTable1.xml": b"<queryTable/>"}),
])
def test_a_field_is_seen_split_encoded_or_in_another_encoding_and_bad_xml_fails_closed(tmp_path, scan, members):
    with pytest.raises(oc.ConversionRejected) as exc:
        _convert(tmp_path, _zip_bytes(_docx(**members)), _never)
    assert exc.value.info["scan"] == scan


def test_the_promotion_scan_reads_its_parts_as_xml_too():
    template = (
        f'<Relationships><Relationship Id="r" Type="{REL_NS}attachedTemplate" '
        'Target="http://example.invalid/t.dotm" TargetMode="&#69;xternal"/></Relationships>'
    )
    for rels in (_rels(template), _rels(template, encoding="utf-16")):
        data = _zip_bytes(_docx(**{"word/_rels/settings.xml.rels": rels}))
        assert rcf.ooxml_container_verdict(io.BytesIO(data)) == "external_rel:attachedtemplate"
    types = b'<Types><Override PartName="/word/document.xml" ContentType="application/vnd.ms-word.document.&#109;acroEnabled.main+xml"/></Types>'
    assert rcf.ooxml_container_verdict(io.BytesIO(_zip_bytes(_docx(**{"[Content_Types].xml": types})))) == "macro_enabled"
    assert rcf.ooxml_container_verdict(io.BytesIO(_zip_bytes(_docx(**{"_rels/.rels": b"<Relationships>"})))) == "bad_xml"
    assert rcf.ooxml_container_verdict(io.BytesIO(_zip_bytes(_docx()))) is None


def test_the_inspected_parts_are_bounded_together(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "SCAN_MAX_TOTAL_BYTES", 300)
    many = _docx(**{f"word/part{i}.xml": b"<w:p>" + b"x" * 100 + b"</w:p>" for i in range(5)})
    assert oc.external_content_verdict(_write(tmp_path, _zip_bytes(many))) == "member_too_large"
