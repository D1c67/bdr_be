"""Streaming JPEG-into-PDF writer for the RFP Ingestion Sandbox.

The sandbox renders every page of a quarantined RFP to a JPEG (thumb tier and
reading tier). The thumb tier is also republished as one or more browsable PDFs
(`images-NNN.pdf` in the derived bucket) so a reviewer, and later the agent
slice, can page through a 2000 page spec book in a normal PDF viewer instead of
fetching two thousand signed image URLs.

Why a hand written writer instead of pypdf or Pillow:

* Memory. A 2000 page thumb set is roughly 300 MB of JPEG. Both pypdf and
  Pillow's multi page save build the whole document in memory before writing a
  byte, which is exactly the resource bomb the sandbox exists to prevent. This
  writer appends each page to an open file and keeps only integers (byte
  offsets and object numbers) between pages, so peak memory is one page's JPEG
  plus a few kilobytes.
* No re-encoding. The JPEG the sandbox produced is embedded verbatim as a
  /DCTDecode image stream. Nothing here decodes, inspects or re-compresses the
  bytes: the caller passes dimensions that the parent already validated with
  its own marker walk (`rfp_sanitize.jpeg_dimensions`), and this module is
  therefore not a parser and not part of the attack surface.
* Part caps. The derived bucket has a hard per object size limit, so the output
  has to roll into numbered parts at a byte cap. A page is never split across
  parts: a part always holds at least one page, and a single page bigger than
  the cap gets a part of its own.

Output shape, per part (PDF 1.4, one flat page tree, no compression):

    %PDF-1.4
    %<four high bytes>            binary comment, so tools treat the file as binary
    3 0 obj  page dict            /MediaBox [0 0 width_pt height_pt]
    4 0 obj  content stream       "q W 0 0 H 0 0 cm /Im0 Do Q"
    5 0 obj  image XObject        /Filter /DCTDecode, the JPEG bytes verbatim
    ... three objects per page, written as add_page() is called ...
    1 0 obj  catalog              written at finalize time
    2 0 obj  page tree            /Kids collected from the page objects
    xref + trailer + startxref

Object 1 (catalog) and object 2 (page tree) are pre-allocated but written last,
which is what lets the page objects reference `2 0 R` as their /Parent while
still streaming. The xref table needs a byte offset per object, so the writer
keeps one int per object and formats the table at finalize time. Cross reference
offsets do not have to be ordered in the file, only correct.

Sizing is exact rather than estimated: before a page is written the writer knows
how many bytes the page contributes AND how many bytes the eventual catalog,
page tree, xref and trailer will add for that page count, so the finished part
is guaranteed to be at or under `part_bytes` whenever it holds more than one
page. That means the caller can hand `part_bytes` straight from the bucket limit
without a fudge factor.

Everything raised here is an app-authored ValueError; a caller failure is
recorded by the ingestion pipeline as `bounds_hit: images_pdf` and never fails
the file. Nothing in this module imports anything beyond the standard library.
"""

from __future__ import annotations

import math
import os
import pathlib

__all__ = ["ImagesPdfWriter"]

# ── Fixed byte strings of the PDF skeleton ───────────────────────────────────
# The header's second line is a comment holding four bytes above 127 so that
# every tool that sniffs text vs binary treats the file as binary (PDF 32000-1
# section 7.5.2 recommends it).
_HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
_CATALOG_OBJ = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
_PAGES_HEAD = b"2 0 obj\n<< /Type /Pages /Count "
_PAGES_KIDS = b" /Kids ["
_PAGES_TAIL = b"] >>\nendobj\n"
_XREF_HEAD = b"xref\n0 "
_FREE_ENTRY = b"0000000000 65535 f \n"
_TRAILER_HEAD = b"trailer\n<< /Size "
_TRAILER_MID = b" /Root 1 0 R >>\nstartxref\n"
_TRAILER_TAIL = b"\n%%EOF\n"

# Object numbering inside one part: 1 = catalog, 2 = page tree, then three
# objects per page (page dict, content stream, image XObject) from 3 up.
_OBJ_CATALOG = 1
_OBJ_PAGES = 2
_FIRST_PAGE_OBJ = 3
_OBJS_PER_PAGE = 3
# Every xref entry is exactly 20 bytes ("nnnnnnnnnn ggggg n \n").
_XREF_ENTRY_BYTES = 20
# Decimals kept when a point dimension is written into the file.
_PT_DECIMALS = 4

_DEFAULT_NAME_TEMPLATE = "images-{part:03d}.pdf"


def _fmt_pt(value: float) -> str:
    """A point dimension as PDF number syntax: fixed point, up to four decimals,
    no exponent (PDF has no exponent notation), no trailing zeros."""
    if not math.isfinite(value):
        raise ValueError("Page dimensions must be finite numbers.")
    text = f"{value:.{_PT_DECIMALS}f}".rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        return "0"
    return text


def _check_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer.")
    if value <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return value


def _check_positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite number greater than zero.")
    return number


class ImagesPdfWriter:
    """Append JPEG pages to numbered PDF parts, one page at a time.

    Use it as a context manager (the open part is finalized on exit, including
    the exception path, so a partial run still leaves valid PDFs behind for the
    caller to discard) or drive it by hand and call close().

        with ImagesPdfWriter(out_dir, part_bytes=150 * 1024 * 1024) as writer:
            for page in pages:
                writer.add_page(page.jpeg, width_px=..., height_px=...,
                                width_pt=..., height_pt=...)
        paths = writer.parts

    Attributes:
      parts          finished part paths, in page order. The part currently
                     being written joins the list only when it is finalized (by
                     a roll to the next part, or by close), so a caller may
                     upload anything the list already holds while the writer
                     runs, and a part that failed mid write never appears.
      pages_written  pages accepted so far, across every part.
      bytes_written  bytes handed to the filesystem so far, including the
                     header and trailer of every part and any part that was
                     dropped after a write error.
    """

    def __init__(
        self,
        dest_dir: str | os.PathLike,
        *,
        part_bytes: int,
        name_template: str = _DEFAULT_NAME_TEMPLATE,
    ) -> None:
        self.dest_dir = pathlib.Path(os.fspath(dest_dir))
        self.part_bytes = _check_positive_int(part_bytes, "part_bytes")
        self.name_template = _validated_template(name_template)
        self.parts: list[pathlib.Path] = []
        self.pages_written = 0
        self.bytes_written = 0
        self._closed = False
        # Per part state, all reset by _open_part().
        self._handle = None
        self._path: pathlib.Path | None = None
        self._pos = 0
        self._offsets: dict[int, int] = {}
        self._page_objs: list[int] = []
        self._kids_bytes = 0
        self._part_index = 0

    # ── Public surface ───────────────────────────────────────────────────────

    def add_page(
        self,
        jpeg: bytes,
        *,
        width_px: int,
        height_px: int,
        width_pt: float,
        height_pt: float,
    ) -> None:
        """Append one JPEG as a full bleed page of width_pt x height_pt.

        The bytes are stored verbatim as the image stream; width_px/height_px go
        into the XObject dictionary and must be the dimensions the JPEG really
        declares (the parent validates that separately, this writer never looks
        inside the bytes). Rolls to a new part first when the finished part
        would otherwise pass `part_bytes`, unless the part is still empty.
        """
        if self._closed:
            raise ValueError("The images PDF writer is closed.")
        if isinstance(jpeg, (bytearray, memoryview)):
            jpeg = bytes(jpeg)
        if not isinstance(jpeg, bytes):
            raise ValueError("Page image data must be JPEG bytes.")
        if not jpeg:
            raise ValueError("Page image data must not be empty.")
        _check_positive_int(width_px, "width_px")
        _check_positive_int(height_px, "height_px")
        _check_positive_number(width_pt, "width_pt")
        _check_positive_number(height_pt, "height_pt")

        if self._handle is None:
            self._open_part()
        blocks = self._page_blocks(
            self._next_page_obj(), len(jpeg), width_px, height_px, width_pt, height_pt
        )
        if self._page_objs and self._would_overflow(blocks, len(jpeg)):
            self._finalize_part()
            self._open_part()
            blocks = self._page_blocks(
                _FIRST_PAGE_OBJ, len(jpeg), width_px, height_px, width_pt, height_pt
            )
        try:
            self._write_page(blocks, jpeg)
        except Exception:
            # A half written page (a full disk, most likely) leaves an object
            # the xref table cannot describe. Drop the whole open part rather
            # than publish a corrupt PDF; the caller records the builder
            # failure and the scratch directory is deleted anyway.
            self._discard_part()
            raise

    def close(self) -> list[pathlib.Path]:
        """Finalize the open part and return every part path in order. Safe to
        call repeatedly; later calls return the same list."""
        try:
            if self._handle is not None:
                self._finalize_part()
        finally:
            self._closed = True
        return list(self.parts)

    def __enter__(self) -> ImagesPdfWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── Part lifecycle ───────────────────────────────────────────────────────

    def _open_part(self) -> None:
        self._part_index += 1
        path = self.dest_dir / self.name_template.format(part=self._part_index)
        os.makedirs(self.dest_dir, exist_ok=True)
        # O_EXCL: a part name that already exists means the caller reused a
        # directory, and silently overwriting somebody else's output is exactly
        # the class of mistake this subsystem refuses to make.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            self._handle = os.fdopen(fd, "wb")
        except OSError:
            os.close(fd)
            raise
        self._path = path
        self._pos = 0
        self._offsets = {}
        self._page_objs = []
        self._kids_bytes = 0
        self._write(_HEADER)

    def _finalize_part(self) -> None:
        """Write the catalog, the page tree, the xref table and the trailer, and
        move the part into `parts`. The handle is released even when a write
        fails, in which case the half written part is NOT published."""
        path = self._path
        try:
            pages = len(self._page_objs)
            self._offsets[_OBJ_CATALOG] = self._pos
            self._write(_CATALOG_OBJ)
            kids = b" ".join(f"{num} 0 R".encode("ascii") for num in self._page_objs)
            self._offsets[_OBJ_PAGES] = self._pos
            self._write(
                _PAGES_HEAD + str(pages).encode("ascii") + _PAGES_KIDS + kids + _PAGES_TAIL
            )
            xref_offset = self._pos
            size = _OBJS_PER_PAGE * pages + 3  # object 0 (free) + catalog + tree + pages
            table = [_XREF_HEAD + str(size).encode("ascii") + b"\n", _FREE_ENTRY]
            for num in range(1, size):
                table.append(f"{self._offsets[num]:010d} 00000 n \n".encode("ascii"))
            self._write(b"".join(table))
            self._write(
                _TRAILER_HEAD
                + str(size).encode("ascii")
                + _TRAILER_MID
                + str(xref_offset).encode("ascii")
                + _TRAILER_TAIL
            )
        finally:
            handle = self._handle
            self._handle = None
            self._path = None
            if handle is not None:
                handle.close()
        self.parts.append(path)

    def _discard_part(self) -> None:
        """Abandon the open part without publishing it. The file itself is left
        on disk for the caller's scratch cleanup to remove."""
        handle = self._handle
        self._handle = None
        self._path = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    # ── Page bytes ───────────────────────────────────────────────────────────

    def _next_page_obj(self) -> int:
        return _FIRST_PAGE_OBJ + _OBJS_PER_PAGE * len(self._page_objs)

    def _page_blocks(
        self,
        page_obj: int,
        jpeg_len: int,
        width_px: int,
        height_px: int,
        width_pt: float,
        height_pt: float,
    ) -> tuple[int, bytes, bytes, bytes, bytes]:
        """Every byte of one page except the JPEG payload itself, as
        (page object number, page dict, content stream object, image object
        header, image object footer)."""
        content_obj = page_obj + 1
        image_obj = page_obj + 2
        width = _fmt_pt(width_pt)
        height = _fmt_pt(height_pt)
        # The image occupies the whole media box: the unit square of an image
        # XObject is scaled by the cm matrix to the page dimensions.
        content = f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode("ascii")
        page_dict = (
            f"{page_obj} 0 obj\n"
            f"<< /Type /Page /Parent {_OBJ_PAGES} 0 R "
            f"/MediaBox [0 0 {width} {height}] "
            f"/Resources << /ProcSet [/PDF /ImageC] "
            f"/XObject << /Im0 {image_obj} 0 R >> >> "
            f"/Contents {content_obj} 0 R >>\nendobj\n"
        ).encode("ascii")
        content_stream = (
            f"{content_obj} 0 obj\n<< /Length {len(content)} >>\nstream\n".encode("ascii")
            + content
            + b"\nendstream\nendobj\n"
        )
        image_head = (
            f"{image_obj} 0 obj\n"
            f"<< /Type /XObject /Subtype /Image /Width {width_px} /Height {height_px} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
            f"/Length {jpeg_len} >>\nstream\n"
        ).encode("ascii")
        image_tail = b"\nendstream\nendobj\n"
        return page_obj, page_dict, content_stream, image_head, image_tail

    def _write_page(self, blocks: tuple[int, bytes, bytes, bytes, bytes], jpeg: bytes) -> None:
        page_obj, page_dict, content_stream, image_head, image_tail = blocks
        self._offsets[page_obj] = self._pos
        self._write(page_dict)
        self._offsets[page_obj + 1] = self._pos
        self._write(content_stream)
        self._offsets[page_obj + 2] = self._pos
        self._write(image_head)
        # The JPEG is written on its own so it is never concatenated into a
        # second copy of itself.
        self._write(jpeg)
        self._write(image_tail)
        self._page_objs.append(page_obj)
        self._kids_bytes += len(f"{page_obj} 0 R")
        self.pages_written += 1

    def _write(self, data: bytes) -> None:
        self._handle.write(data)
        self._pos += len(data)
        self.bytes_written += len(data)

    # ── Size accounting ──────────────────────────────────────────────────────

    def _would_overflow(self, blocks: tuple[int, bytes, bytes, bytes, bytes], jpeg_len: int) -> bool:
        """True when finishing the part with this page would pass `part_bytes`."""
        page_obj, page_dict, content_stream, image_head, image_tail = blocks
        body = self._pos + len(page_dict) + len(content_stream) + len(image_head)
        body += jpeg_len + len(image_tail)
        tail = _tail_bytes(
            pages=len(self._page_objs) + 1,
            kids_bytes=self._kids_bytes + len(f"{page_obj} 0 R"),
            body_end=body,
        )
        return body + tail > self.part_bytes


def _tail_bytes(*, pages: int, kids_bytes: int, body_end: int) -> int:
    """Exact size of everything a finalize would still append: catalog, page
    tree, xref table and trailer, for a part that holds `pages` pages whose
    /Kids references total `kids_bytes` and whose last page object ends at byte
    `body_end`."""
    size = _OBJS_PER_PAGE * pages + 3
    catalog = len(_CATALOG_OBJ)
    kids_inner = kids_bytes + max(pages - 1, 0)  # single spaces between the refs
    tree = (
        len(_PAGES_HEAD)
        + len(str(pages))
        + len(_PAGES_KIDS)
        + kids_inner
        + len(_PAGES_TAIL)
    )
    xref_offset = body_end + catalog + tree
    xref = len(_XREF_HEAD) + len(str(size)) + 1 + _XREF_ENTRY_BYTES * size
    trailer = (
        len(_TRAILER_HEAD)
        + len(str(size))
        + len(_TRAILER_MID)
        + len(str(xref_offset))
        + len(_TRAILER_TAIL)
    )
    return catalog + tree + xref + trailer


def _validated_template(template: object) -> str:
    """A part name template that yields distinct, directory-local file names."""
    if not isinstance(template, str) or not template:
        raise ValueError("The part name template must be a non-empty string.")
    try:
        first = template.format(part=1)
        second = template.format(part=2)
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError("The part name template must accept a {part} field.") from exc
    if first == second:
        raise ValueError("The part name template must include the {part} number.")
    for name in (first, second):
        if not name or name in (".", "..") or os.sep in name or "/" in name:
            raise ValueError("The part name template must produce a plain file name.")
        if os.altsep and os.altsep in name:
            raise ValueError("The part name template must produce a plain file name.")
    return template
