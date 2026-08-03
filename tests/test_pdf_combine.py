"""services/pdf_combine — normalizing heterogeneous files into one PDF.

This is the machinery behind "the GC gets one combined PDF per category", so the
properties that matter are about what happens to the awkward inputs, not the easy
ones: an unmergeable file must raise UnmergeableFile (never ConversionError,
never a silent drop) so the caller can attach it separately, and a file that
survives to_pdf must be genuinely parseable so merge() can treat a failure as a
real bug rather than the expected case.

Unlike test_submittal_approval (which stubs this module out), these run against
real PDFs built with pypdf. Gotenberg and the office converter are monkeypatched
— the network round trip is not what's under test.
"""

import io

import pytest
from pypdf import PdfReader, PdfWriter

from app.services import office_preview, pdf_combine, submittal_pdf
from app.services.office_preview import ConversionError

# A 1x1 JPEG and PNG — enough for the magic-byte dispatch to fire on.
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def _pdf(pages: int = 1, **encrypt) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    if encrypt:
        w.encrypt(**encrypt)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _pages(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


# ── merge ────────────────────────────────────────────────────────────────────


def test_merge_concatenates_in_order():
    out = pdf_combine.merge([_pdf(1), _pdf(3), _pdf(2)])
    assert out.startswith(b"%PDF")
    assert _pages(out) == 6


def test_merge_needs_at_least_one_part():
    with pytest.raises(ValueError):
        pdf_combine.merge([])


def test_merge_opens_an_owner_password_protected_part():
    """Cut sheets are routinely owner-locked (print/copy restrictions) with an
    empty user password — those must still merge."""
    locked = _pdf(2, user_password="", owner_password="lockme")
    assert _pages(pdf_combine.merge([_pdf(1), locked])) == 3


# ── to_pdf: the easy path ────────────────────────────────────────────────────


def test_to_pdf_passes_a_real_pdf_through_unchanged():
    src = _pdf(2)
    assert pdf_combine.to_pdf(src, "cutsheet.pdf") == src


def test_to_pdf_trusts_magic_bytes_over_the_extension():
    """A vendor's "spec.docx" that is really a PDF merges as a PDF rather than
    being pushed through LibreOffice."""
    src = _pdf(1)
    assert pdf_combine.to_pdf(src, "spec.docx") == src


# ── to_pdf: office files ─────────────────────────────────────────────────────


def test_to_pdf_converts_an_office_file(monkeypatch):
    converted = _pdf(4)
    seen = {}

    def _convert(content, filename):
        seen["filename"] = filename
        return converted

    monkeypatch.setattr(office_preview, "convert_for_send", _convert)
    assert pdf_combine.to_pdf(b"PK\x03\x04junk", "spec.docx") == converted
    assert seen["filename"] == "spec.docx"


def test_a_converter_outage_becomes_unmergeable_not_conversion_error(monkeypatch):
    """Callers catch exactly one expected-failure type; a Gotenberg outage on one
    office file must not look different from an unsupported format."""

    def _boom(content, filename):
        raise ConversionError("gotenberg unreachable")

    monkeypatch.setattr(office_preview, "convert_for_send", _boom)
    with pytest.raises(pdf_combine.UnmergeableFile, match="could not convert"):
        pdf_combine.to_pdf(b"PK\x03\x04junk", "spec.docx")


# ── to_pdf: images ───────────────────────────────────────────────────────────


def test_to_pdf_renders_an_image_to_a_page(monkeypatch):
    rendered = _pdf(1)
    seen = {}

    def _render(doc):
        seen["doc"] = doc
        return rendered

    monkeypatch.setattr(submittal_pdf, "html_to_pdf", _render)
    assert pdf_combine.to_pdf(PNG, "nameplate.png") == rendered
    assert "data:image/png;base64," in seen["doc"]


def test_an_image_misnamed_as_a_pdf_is_still_rendered(monkeypatch):
    rendered = _pdf(1)
    monkeypatch.setattr(submittal_pdf, "html_to_pdf", lambda doc: rendered)
    # ".pdf" extension, JPEG bytes — the magic bytes win.
    assert pdf_combine.to_pdf(JPEG, "cutsheet.pdf") == rendered


def test_an_image_render_failure_becomes_unmergeable(monkeypatch):
    def _boom(doc):
        raise ConversionError("gotenberg unreachable")

    monkeypatch.setattr(submittal_pdf, "html_to_pdf", _boom)
    with pytest.raises(pdf_combine.UnmergeableFile, match="could not render"):
        pdf_combine.to_pdf(PNG, "nameplate.png")


# ── to_pdf: what it refuses ──────────────────────────────────────────────────


def test_an_archive_is_refused():
    with pytest.raises(pdf_combine.UnmergeableFile, match="combined into a PDF"):
        pdf_combine.to_pdf(b"PK\x03\x04junk", "shop-drawings.zip")


def test_an_unknown_binary_is_refused():
    with pytest.raises(pdf_combine.UnmergeableFile):
        pdf_combine.to_pdf(b"\x00\x01\x02\x03", "panel.dwg")


def test_a_truncated_pdf_is_refused_rather_than_breaking_the_merge():
    """It claims to be a PDF and isn't. Catching it here — not at merge time — is
    what lets the caller attach it separately instead of failing the send."""
    with pytest.raises(pdf_combine.UnmergeableFile, match="could not be read"):
        pdf_combine.to_pdf(b"%PDF-1.7\nnot really", "cutsheet.pdf")


def test_a_user_password_protected_pdf_is_refused():
    """We can't open it, so we can't merge it — but it still reaches the GC as its
    own attachment rather than disappearing."""
    with pytest.raises(pdf_combine.UnmergeableFile, match="password-protected"):
        pdf_combine.to_pdf(_pdf(1, user_password="secret"), "locked.pdf")


def test_a_converter_returning_a_broken_pdf_is_refused(monkeypatch):
    """to_pdf validates its own output, so merge() only ever sees parseable
    parts."""
    monkeypatch.setattr(office_preview, "convert_for_send", lambda c, f: b"%PDF-1.4 garbage")
    with pytest.raises(pdf_combine.UnmergeableFile, match="could not be read"):
        pdf_combine.to_pdf(b"PK\x03\x04junk", "spec.docx")
