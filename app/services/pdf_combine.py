"""Fold a heterogeneous set of files into one PDF.

Written for submittal approval packages: the GC gets ONE combined PDF per
material category (a G3 cover page followed by every submittal in that
category), not a scatter of loose attachments. What we hold for a category is
whatever vendors, the Submittal Bank and our own uploads produced — mostly PDFs,
but routinely a .docx spec sheet or a photographed nameplate — so the inputs are
normalized to PDF before they can be concatenated.

Two steps, deliberately separate:

  to_pdf(content, filename)  one file → PDF bytes that pypdf has already proven
                             it can open. Raises UnmergeableFile for anything
                             that can't get there (a .zip, a .dwg, a converter
                             outage, a password-protected PDF).
  merge(parts)               concatenate proven-readable parts.

to_pdf validating its own output is what makes merge safe to treat as fatal: by
the time parts reach the writer every one of them has been parsed, so a failure
there is a genuine surprise rather than the expected case of "the GC's vendor
sent us something weird". Callers handle the expected case by attaching that one
file loose alongside the combined PDF — never by dropping it.
"""

import base64
import io
import logging
import mimetypes

from app.services import office_preview
from app.services.office_preview import ConversionError

logger = logging.getLogger(__name__)

# Rendered to a page via Chromium rather than a raster library: it keeps the
# aspect ratio and page fitting in one place (the same engine that draws the
# cover sheet) instead of adding an imaging dependency.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


class UnmergeableFile(Exception):
    """This file can't be folded into a combined PDF — send it alongside one."""


def _ext(filename: str | None) -> str:
    name = (filename or "").lower()
    dot = name.rfind(".")
    return name[dot:] if dot != -1 else ""


def to_pdf(content: bytes, filename: str) -> bytes:
    """Normalize one file to merge-ready PDF bytes.

    Dispatches on the MAGIC BYTES first and the extension only as a fallback: a
    vendor's "cutsheet.pdf" that is really a JPEG (and vice versa) is common
    enough that trusting the name would fail merges we can trivially do.

    Raises UnmergeableFile — never ConversionError — so callers have one
    expected-failure type to catch, whatever the underlying cause.
    """
    if content[:5] == b"%PDF-":
        return _ensure_readable(content, filename)

    ext = _ext(filename)
    if ext in office_preview.CONVERTIBLE_EXTS:
        try:
            # Fails closed and retries once internally; a converter outage lands
            # here as ConversionError.
            return _ensure_readable(office_preview.convert_for_send(content, filename), filename)
        except ConversionError as exc:
            raise UnmergeableFile(f"{filename}: could not convert to PDF ({exc})") from exc

    if ext in IMAGE_EXTS or content[:4] in (b"\x89PNG", b"GIF8") or content[:3] == b"\xff\xd8\xff":
        try:
            return _ensure_readable(_image_to_pdf(content, filename), filename)
        except ConversionError as exc:
            raise UnmergeableFile(f"{filename}: could not render to PDF ({exc})") from exc

    raise UnmergeableFile(f"{filename}: not a format that can be combined into a PDF")


def _image_to_pdf(content: bytes, filename: str) -> bytes:
    """One image → one letter page, scaled to fit and centered."""
    from app.services.submittal_pdf import html_to_pdf

    mime = mimetypes.guess_type(filename)[0] or "image/png"
    if not mime.startswith("image/"):  # e.g. a .tif the OS doesn't know
        mime = "image/png"
    uri = f"data:{mime};base64,{base64.b64encode(content).decode()}"
    doc = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:0;">'
        '<div style="display:flex;align-items:center;justify-content:center;'
        'width:100%;height:10in;">'
        f'<img src="{uri}" style="max-width:100%;max-height:100%;">'
        "</div></body></html>"
    )
    return html_to_pdf(doc)


def _ensure_readable(pdf: bytes, filename: str) -> bytes:
    """Prove pypdf can parse (and if need be decrypt) this PDF before it is
    allowed anywhere near a merge.

    Vendor cut sheets are frequently protected with an OWNER password only —
    those open with an empty user password and merge fine. A real user password
    can't be opened, and a file that can't be opened must not silently vanish
    from the package, so it becomes an UnmergeableFile the caller attaches loose.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnmergeableFile(f"{filename}: the PDF is password-protected")
        if not reader.pages:
            raise UnmergeableFile(f"{filename}: the PDF has no pages")
    except UnmergeableFile:
        raise
    except Exception as exc:  # noqa: BLE001 — pypdf raises a wide variety here
        raise UnmergeableFile(f"{filename}: the PDF could not be read ({exc})") from exc
    return pdf


def merge(parts: list[bytes]) -> bytes:
    """Concatenate PDFs, in order, into one document.

    Every part must have come through `to_pdf`, so a failure here is exceptional
    rather than routine and is allowed to propagate — the alternative (skipping
    the part) would drop a submittal from the package without telling anyone.
    """
    from pypdf import PdfReader, PdfWriter

    if not parts:
        raise ValueError("merge() needs at least one PDF")

    writer = PdfWriter()
    for part in parts:
        reader = PdfReader(io.BytesIO(part))
        if reader.is_encrypted:
            reader.decrypt("")
        writer.append(reader)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
