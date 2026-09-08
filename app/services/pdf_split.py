"""Split one PDF along page ranges, and render pages to images.

Companion to pdf_combine.py (which folds many files into one PDF); this module
goes the other way for the Bid File Splitter: prove a source PDF is readable,
rasterize its pages so a vision model can classify them, and cut it into one
output PDF per contiguous page range.

Rendering uses pypdfium2 (PDFium wheels, permissive license, no system
dependencies) — the first rasterizer in the codebase. PDFium is NOT
thread-safe, and the llm_queue worker runs several jobs concurrently in
threads, so every PDFium touch is serialized behind a module lock. A page
render at ~1500px is tens of milliseconds, so the lock is not a bottleneck.

Splitting stays on pypdf (already a dependency; pdf_combine precedent).
All raised errors are app-authored ValueErrors so llm_errors classifies them
as permanent bad_input with a user-safe message.
"""

import io
import threading

_PDFIUM_LOCK = threading.Lock()


def _readable_reader(pdf_bytes: bytes):
    """A pypdf reader proven to open (and if need be empty-password decrypt)
    this PDF. Mirrors pdf_combine._ensure_readable, but returns the reader so
    callers don't parse twice."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("The PDF is password-protected and cannot be processed.")
        if not reader.pages:
            raise ValueError("The PDF has no pages.")
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — pypdf raises a wide variety here
        raise ValueError(f"The PDF could not be read ({exc}).") from exc
    return reader


def page_count(pdf_bytes: bytes) -> int:
    """Number of pages, after proving the PDF opens. Raises ValueError with a
    user-safe message for encrypted/broken/empty files."""
    return len(_readable_reader(pdf_bytes).pages)


def render_pages(
    pdf_bytes: bytes,
    page_indices: list[int],
    *,
    long_side: int,
    jpeg_quality: int,
) -> list[bytes]:
    """Rasterize the given 0-based pages to JPEG bytes, longest side capped at
    `long_side` pixels (drawings are classified from their title blocks, which
    stay legible well below print resolution)."""
    import pypdfium2 as pdfium

    out: list[bytes] = []
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            for idx in page_indices:
                page = doc[idx]
                width, height = page.get_size()  # points (1/72")
                scale = long_side / max(width, height, 1.0)
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil().convert("RGB")
                buf = io.BytesIO()
                image.save(buf, "JPEG", quality=jpeg_quality)
                out.append(buf.getvalue())
        finally:
            doc.close()
    return out


def extract_pages(pdf_bytes: bytes, pages: list[int]) -> bytes:
    """One new PDF holding the given 1-based pages of the source, in the given
    order. Pages need not be contiguous and may repeat - this is how the
    General / Cover Sheets pages ride at the front of every trade segment cut
    from the same file. A page outside the document raises ValueError."""
    from pypdf import PdfWriter

    reader = _readable_reader(pdf_bytes)
    total = len(reader.pages)
    if not pages:
        raise ValueError("No pages were requested from the PDF.")
    for p in pages:
        if not (1 <= p <= total):
            raise ValueError(f"Page {p} is outside the document (1-{total}).")
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def extract_range(pdf_bytes: bytes, page_start: int, page_end: int) -> bytes:
    """One new PDF holding pages `page_start`..`page_end` (1-based, inclusive)
    of the source. Range validity is the caller's job; a range beyond the
    document raises ValueError."""
    from pypdf import PdfWriter

    reader = _readable_reader(pdf_bytes)
    total = len(reader.pages)
    if not (1 <= page_start <= page_end <= total):
        raise ValueError(
            f"Page range {page_start}-{page_end} is outside the document (1-{total})."
        )
    writer = PdfWriter()
    for i in range(page_start - 1, page_end):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
