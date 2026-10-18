"""Image and PDF plumbing for AI Estimating (docs/PLANS_READER.md sections 1-2).

Why this module exists: the plans reader sends drawing sheets to OpenAI as
native-resolution PNGs with `detail: original`, and spec books as `input_file`
PDFs. Both have hard limits that would otherwise surface as a failed call
after the money is spent:

- OpenAI bills an image as `ceil(patches * 1.2)` tokens, where patches are
  32 px squares (`ceil(w/32) * ceil(h/32)`), and REJECTS a `detail: original`
  image over 30,000 patches. A 42x30 in sheet at 260 ppi is 83,448 patches,
  so a sheet goes either as one image at the largest ppi that fits
  (`native_ppi_for`) or as several tiles that each fit (`plan_tiles`).
- `input_file` PDFs are capped at 50 MB per request, and long books cost
  about 1,100 tokens per page, so they are cut into chunks (`pdf_chunks`).

The pixel math mirrors pypdfium2 exactly: `page.render(scale=s)` makes a
bitmap of `math.ceil(width_pt * s)` by `math.ceil(height_pt * s)` pixels, and
every render here passes `scale = ppi / 72`. Planning and rendering therefore
agree on the patch count before anything is rasterized.

PDFium is not thread-safe and the llm_queue worker runs jobs in threads, so
every PDFium touch takes pdf_split._PDFIUM_LOCK (one lock for the whole app).
Raised errors are app-authored ValueErrors so llm_errors classifies them as
permanent bad_input with a user-safe message.
"""

from __future__ import annotations

import base64
import io
import math
import re

from app.services import pdf_split

PATCH_PX = 32
TOKENS_PER_PATCH = 1.2
MAX_PATCHES_ORIGINAL = 30_000
DEFAULT_CAP_PPI = 260
DEFAULT_TILE_OVERLAP = 0.05
FILE_MAX_BYTES = 50 * 1024 * 1024
FILE_MAX_PAGES = 220
# Per-request limits OpenAI enforces on the Responses API (docs/PLANS_READER.md
# section 1): a request over either is rejected after the render work is done,
# and a retry fails the same way, so packing and the caller check them first.
REQUEST_MAX_IMAGES = 1_500
REQUEST_MAX_BYTES = 512 * 1024 * 1024
IMAGE_DETAILS = ("low", "high", "auto", "original")

Box = tuple[int, int, int, int]


# Patch and token math ---------------------------------------------------------


def patches_for(w_px: int, h_px: int) -> int:
    """32 px patches OpenAI counts for a w x h image."""
    if w_px <= 0 or h_px <= 0:
        return 0
    return math.ceil(w_px / PATCH_PX) * math.ceil(h_px / PATCH_PX)


patches = patches_for  # the name docs/PLANS_READER.md section 6 uses


def tokens_for_patches(patches: int, multiplier: float = TOKENS_PER_PATCH) -> int:
    """Input tokens billed for `patches` patches (`ceil(patches * 1.2)`).
    Rounded on a 1e-9 grid first so 27,777 * 1.2 is 33,333 and not 33,333
    plus a float hair that would ceil to 33,334."""
    if patches <= 0:
        return 0
    return math.ceil(round(patches * multiplier, 9))


def image_tokens(w_px: int, h_px: int) -> int:
    """Input tokens for one `detail: original` image of w x h pixels."""
    return tokens_for_patches(patches_for(w_px, h_px))


def px_for_points(size_pt: float, ppi: float) -> int:
    """Pixels PDFium produces for a side of `size_pt` points rendered at `ppi`
    (same float operations as pypdfium2 PdfPage.render with scale=ppi/72)."""
    scale = ppi / 72
    return math.ceil(size_pt * scale)


def px_for_inches(size_in: float, ppi: float) -> int:
    """Pixels for a side of `size_in` inches at `ppi`, via points."""
    return px_for_points(size_in * 72, ppi)


def native_ppi_for(
    w_in: float,
    h_in: float,
    *,
    max_patches: int = MAX_PATCHES_ORIGINAL,
    cap_ppi: int = DEFAULT_CAP_PPI,
) -> int:
    """Largest integer ppi <= `cap_ppi` at which a w_in x h_in sheet renders
    to at most `max_patches` patches. 155 for 42x30, 188 for 36x24, 133 for
    48x36, 260 (the cap) for letter and tabloid."""
    if w_in <= 0 or h_in <= 0:
        raise ValueError("The page has no size and cannot be rendered.")
    if cap_ppi < 1:
        raise ValueError("The render resolution cap must be at least 1 ppi.")

    def fits(ppi: int) -> bool:
        return patches_for(px_for_inches(w_in, ppi), px_for_inches(h_in, ppi)) <= max_patches

    if fits(cap_ppi):
        return cap_ppi
    if not fits(1):
        raise ValueError("The page is too large to send as one image at any resolution.")
    lo, hi = 1, cap_ppi  # fits(lo) and not fits(hi)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid
    return lo


# Tiles and regions ------------------------------------------------------------


def _edges(total: int, parts: int) -> list[int]:
    """`parts + 1` integer cut points splitting 0..total into near-equal
    cells (rounded, so the cells differ by at most one pixel)."""
    return [round(i * total / parts) for i in range(parts + 1)]


def _grid_boxes(w_px: int, h_px: int, cols: int, rows: int, overlap: float) -> list[Box]:
    """Row-major tiles of a cols x rows grid. Adjacent tiles share `overlap`
    of a cell: each interior side grows by half of it."""
    xs = _edges(w_px, cols)
    ys = _edges(h_px, rows)
    pad_x = math.ceil(overlap * (w_px / cols) / 2) if cols > 1 else 0
    pad_y = math.ceil(overlap * (h_px / rows) / 2) if rows > 1 else 0
    boxes: list[Box] = []
    for r in range(rows):
        for c in range(cols):
            x0 = max(0, xs[c] - (pad_x if c > 0 else 0))
            x1 = min(w_px, xs[c + 1] + (pad_x if c < cols - 1 else 0))
            y0 = max(0, ys[r] - (pad_y if r > 0 else 0))
            y1 = min(h_px, ys[r + 1] + (pad_y if r < rows - 1 else 0))
            boxes.append((x0, y0, x1, y1))
    return boxes


def _box_patches(box: Box) -> int:
    """Patches OpenAI counts for the image cut out by a pixel box."""
    x0, y0, x1, y1 = box
    return patches_for(x1 - x0, y1 - y0)


def plan_tiles(
    w_px: int,
    h_px: int,
    *,
    max_patches: int = MAX_PATCHES_ORIGINAL,
    overlap: float = DEFAULT_TILE_OVERLAP,
) -> list[Box]:
    """Pixel boxes (x0, y0, x1, y1), row-major, covering a w x h image with
    the fewest grid tiles whose every tile (overlap included) stays at or
    under `max_patches`. Ties go to the grid with fewer total patches. An
    image that already fits is one tile."""
    if w_px <= 0 or h_px <= 0:
        raise ValueError("The image has no size and cannot be tiled.")
    if not 0 <= overlap < 0.5:
        raise ValueError("Tile overlap must be between 0 and 0.5.")
    if max_patches < 4:
        raise ValueError("The tile patch budget is too small.")
    best: tuple[int, int, list[Box]] | None = None  # (count, total patches, boxes)
    max_cols = math.ceil(w_px / PATCH_PX)
    max_rows = math.ceil(h_px / PATCH_PX)
    for cols in range(1, max_cols + 1):
        if best is not None and cols > best[0]:
            break
        for rows in range(1, max_rows + 1):
            if best is not None and cols * rows > best[0]:
                break
            boxes = _grid_boxes(w_px, h_px, cols, rows, overlap)
            per_tile = [_box_patches(b) for b in boxes]
            if max(per_tile) > max_patches:
                continue
            key = (cols * rows, sum(per_tile))
            if best is None or key < (best[0], best[1]):
                best = (key[0], key[1], boxes)
            break  # more rows for this column count only adds tiles
    if best is None:
        raise ValueError("The image cannot be tiled under the patch budget.")
    return best[2]


def plan_region_tiles(
    w_px: int,
    h_px: int,
    box: Box,
    *,
    max_patches: int = MAX_PATCHES_ORIGINAL,
    overlap: float = DEFAULT_TILE_OVERLAP,
) -> list[Box]:
    """Tiles for one region of a w x h page render, in full-page pixel
    coordinates. Used by escalation: a flagged region re-rendered at 260 ppi
    can itself exceed the patch cap."""
    x0, y0, x1, y1 = _clamp_box(box, w_px, h_px)
    return [
        (x0 + a, y0 + b, x0 + c, y0 + d)
        for (a, b, c, d) in plan_tiles(x1 - x0, y1 - y0, max_patches=max_patches, overlap=overlap)
    ]


def _clamp_box(box: Box, w_px: int, h_px: int) -> Box:
    """Clip a pixel box to the w x h image; ValueError when nothing is left
    (a region the model named entirely off the page)."""
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, x1 = max(0, min(x0, w_px)), max(0, min(x1, w_px))
    y0, y1 = max(0, min(y0, h_px)), max(0, min(y1, h_px))
    if x1 <= x0 or y1 <= y0:
        raise ValueError("The requested page region is empty.")
    return (x0, y0, x1, y1)


def box_from_fractions(frac: tuple[float, float, float, float], w_px: int, h_px: int) -> Box:
    """Pixel box for (x0, y0, x1, y1) page fractions (origin top-left)."""
    fx0, fy0, fx1, fy1 = (min(1.0, max(0.0, float(v))) for v in frac)
    if fx1 <= fx0 or fy1 <= fy0:
        raise ValueError("The requested page region is empty.")
    box = (round(fx0 * w_px), round(fy0 * h_px), round(fx1 * w_px), round(fy1 * h_px))
    return _clamp_box(box, w_px, h_px)


def box_to_fractions(box: Box, w_px: int, h_px: int) -> list[float]:
    """[x0, y0, x1, y1] page fractions for a pixel box, the form stored on
    plans_reader_groups.pages[].tiles so it survives any render ppi."""
    x0, y0, x1, y1 = box
    return [round(x0 / w_px, 6), round(y0 / h_px, 6), round(x1 / w_px, 6), round(y1 / h_px, 6)]


_GRID_RE = re.compile(r"^r([1-3])c([1-3])$")


def region_to_box(region: str, w_px: int, h_px: int) -> Box:
    """Pixel box for a resolution-flag region: a 3x3 grid cell 'r1c1'..'r3c3'
    (row from the top, column from the left), 'whole', or 'x0,y0,x1,y1' page
    fractions. Unknown forms raise ValueError."""
    text = (region or "").strip().lower().replace(" ", "")
    if w_px <= 0 or h_px <= 0:
        raise ValueError("The page has no size.")
    if text in ("whole", "all", "page"):
        return (0, 0, w_px, h_px)
    m = _GRID_RE.match(text)
    if m:
        r, c = int(m.group(1)) - 1, int(m.group(2)) - 1
        xs = _edges(w_px, 3)
        ys = _edges(h_px, 3)
        return (xs[c], ys[r], xs[c + 1], ys[r + 1])
    parts = text.strip("[]()").split(",")
    if len(parts) == 4:
        try:
            frac = tuple(float(p) for p in parts)
        except ValueError:
            frac = None
        if frac is not None and all(math.isfinite(v) for v in frac):
            return box_from_fractions(frac, w_px, h_px)  # type: ignore[arg-type]
    # The region string comes from the model: cap its length and escape it to
    # ASCII before it reaches a message a user can see.
    raise ValueError(f"Unrecognized page region {ascii(str(region)[:40])}.")


# PDF reading and rendering ----------------------------------------------------


def page_count(pdf_bytes: bytes) -> int:
    """Number of pages, after proving the PDF opens (ValueError otherwise)."""
    return pdf_split.page_count(pdf_bytes)


def page_sizes_pt(pdf_bytes: bytes) -> list[tuple[float, float]]:
    """(width, height) in points for every page, as PDFium renders them
    (crop box and /Rotate applied)."""
    import pypdfium2 as pdfium

    pdf_split.page_count(pdf_bytes)  # user-safe errors for broken files
    with pdf_split._PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(pdf_bytes)
        except Exception as exc:  # noqa: BLE001 - PDFium raises PdfiumError variants
            raise ValueError(f"The PDF could not be opened for rendering ({exc}).") from exc
        try:
            sizes: list[tuple[float, float]] = []
            for i in range(len(doc)):
                # Pages are closed explicitly: a page freed by the garbage
                # collector while doc.close() walks its children raises
                # "Set changed size during iteration" inside pypdfium2.
                try:
                    page = doc[i]
                    try:
                        w, h = page.get_size()
                    finally:
                        page.close()
                except Exception as exc:  # noqa: BLE001 - PdfiumError is a RuntimeError
                    raise ValueError(f"Page {i + 1} of the PDF could not be read.") from exc
                sizes.append((w, h))
            return sizes
        finally:
            doc.close()


def page_sizes_in(pdf_bytes: bytes) -> list[tuple[float, float]]:
    """(width, height) in inches for every page."""
    return [(w / 72, h / 72) for (w, h) in page_sizes_pt(pdf_bytes)]


def _trim_to_patches(image, max_patches: int):
    """Shave the right or bottom edge (always under 32 px) when a render
    lands a hair over the patch cap because of float rounding."""
    w, h = image.size
    if patches_for(w, h) <= max_patches:
        return image
    for nw, nh in (
        ((math.ceil(w / PATCH_PX) - 1) * PATCH_PX, h),
        (w, (math.ceil(h / PATCH_PX) - 1) * PATCH_PX),
        ((math.ceil(w / PATCH_PX) - 1) * PATCH_PX, (math.ceil(h / PATCH_PX) - 1) * PATCH_PX),
    ):
        if nw > 0 and nh > 0 and patches_for(nw, nh) <= max_patches:
            return image.crop((0, 0, nw, nh))
    raise ValueError("The rendered page is over the image size limit.")


def render_page_png(
    pdf_bytes: bytes,
    page_index0: int,
    ppi: float,
    box_px: Box | None = None,
    *,
    max_patches: int | None = None,
) -> tuple[bytes, int, int]:
    """Render one 0-based page at `ppi` to an optimized PNG, or only the
    pixel box `box_px` of that render (full-page pixel coordinates at the
    same ppi). Returns (png_bytes, w_px, h_px).

    A box is rendered through PDFium's bitmap crop, so a 260 ppi tile never
    allocates the full 10,920 x 7,800 bitmap. When `max_patches` is given the
    output is guaranteed to be at or under it."""
    import pypdfium2 as pdfium

    if ppi <= 0:
        raise ValueError("The render resolution must be positive.")
    scale = ppi / 72
    with pdf_split._PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(pdf_bytes)
        except Exception as exc:  # noqa: BLE001 - PDFium raises PdfiumError variants
            raise ValueError(f"The PDF could not be opened for rendering ({exc}).") from exc
        try:
            total = len(doc)
            if not 0 <= page_index0 < total:
                raise ValueError(f"Page {page_index0 + 1} is outside the document (1-{total}).")
            # A broken page raises PdfiumError (a RuntimeError), which
            # llm_errors would treat as transient and retry forever; it can
            # never succeed, so it becomes a permanent, user-safe ValueError.
            # MemoryError stays as is (it may pass on a less busy worker).
            page = None
            try:
                page = doc[page_index0]
                w_pt, h_pt = page.get_size()
                full_w = math.ceil(w_pt * scale)
                full_h = math.ceil(h_pt * scale)
                crop = (0.0, 0.0, 0.0, 0.0)
                want: tuple[int, int] | None = None
                if box_px is not None:
                    x0, y0, x1, y1 = _clamp_box(box_px, full_w, full_h)
                    # PDFium crops ceil(c * scale) pixels per side (left,
                    # bottom, right, top); (px - 0.5) / scale lands exactly on px.
                    def to_pt(px: int) -> float:
                        return (px - 0.5) / scale if px > 0 else 0.0

                    crop = (to_pt(x0), to_pt(full_h - y1), to_pt(full_w - x1), to_pt(y0))
                    want = (x1 - x0, y1 - y0)
                bitmap = page.render(scale=scale, crop=crop)
                image = bitmap.to_pil().convert("RGB")
            except (ValueError, MemoryError):
                raise
            except Exception as exc:  # noqa: BLE001 - PdfiumError and PIL errors
                raise ValueError(
                    f"Page {page_index0 + 1} of the PDF could not be rendered."
                ) from exc
            finally:
                if page is not None:
                    page.close()
        finally:
            doc.close()
    if want is not None and (image.width > want[0] or image.height > want[1]):
        image = image.crop((0, 0, min(image.width, want[0]), min(image.height, want[1])))
    if max_patches is not None:
        image = _trim_to_patches(image, max_patches)
    buf = io.BytesIO()
    image.save(buf, "PNG", optimize=True)
    return buf.getvalue(), image.width, image.height


# Spec PDF chunking ------------------------------------------------------------


def _write_span(reader, start1: int, end1: int) -> bytes:
    """Write 1-based pages start1..end1 of an open pypdf reader as a new,
    unencrypted PDF. A broken object raises PyPdfError (not a ValueError),
    which would be retried as transient, so it becomes a user-safe
    ValueError here."""
    from pypdf import PdfWriter

    try:
        writer = PdfWriter()
        for i in range(start1 - 1, end1):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
    except (ValueError, MemoryError):
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises a wide variety here
        raise ValueError(
            f"Pages {start1}-{end1} of the PDF could not be copied into a smaller file."
        ) from exc
    return buf.getvalue()


def pdf_chunks(
    pdf_bytes: bytes,
    *,
    max_bytes: int = FILE_MAX_BYTES,
    max_pages: int = FILE_MAX_PAGES,
    breaks: list[int] | None = None,
) -> list[tuple[int, int, bytes]]:
    """Cut a PDF into consecutive chunks (start_page1, end_page1, bytes), each
    at most `max_pages` pages and `max_bytes` bytes, for `input_file` parts.

    A file already under both caps comes back whole, unchanged, unless it is
    encrypted (an owner-password-only file opens with an empty password):
    then it is re-written decrypted, since the model's file parser may not
    read encrypted PDFs. Otherwise each chunk takes the longest page span
    from its start that fits (binary search on the written size). `breaks`
    are optional 1-based pages that start a new section (for example spec
    section starts); when one falls inside the span the chunk ends just
    before the last such page, so a section is not cut in two unless it
    alone exceeds a cap. A break that would leave the chunk under half of
    its span is ignored, so a section start near the chunk's own start does
    not produce a sliver chunk. A single page over `max_bytes` raises
    ValueError."""
    if max_bytes <= 0 or max_pages <= 0:
        raise ValueError("The PDF chunk limits must be positive.")
    reader = pdf_split._readable_reader(pdf_bytes)
    total = len(reader.pages)
    if total <= max_pages and len(pdf_bytes) <= max_bytes:
        if not reader.is_encrypted:
            return [(1, total, pdf_bytes)]
        data = _write_span(reader, 1, total)
        if len(data) <= max_bytes:
            return [(1, total, data)]
    break_set = sorted({b for b in (breaks or []) if 1 < b <= total})
    out: list[tuple[int, int, bytes]] = []
    start = 1
    while start <= total:
        hi = min(total, start + max_pages - 1)
        data = _write_span(reader, start, hi)
        end = hi
        if len(data) > max_bytes:
            single = _write_span(reader, start, start)
            if len(single) > max_bytes:
                mb = max_bytes / (1024 * 1024)
                raise ValueError(
                    f"Page {start} of the PDF is larger than {mb:.0f} MB on its own and "
                    "cannot be sent to the model."
                )
            lo, lo_data = start, single  # lo fits, hi does not
            while hi - lo > 1:
                mid = (lo + hi) // 2
                mid_data = _write_span(reader, start, mid)
                if len(mid_data) <= max_bytes:
                    lo, lo_data = mid, mid_data
                else:
                    hi = mid
            end, data = lo, lo_data
        if end < total and break_set:
            min_len = max(1, (end - start + 1) // 2)
            inside = [b for b in break_set if start + min_len <= b <= end]
            if inside:
                end = inside[-1] - 1
                data = _write_span(reader, start, end)
        out.append((start, end, data))
        start = end + 1
    return out


# Request parts ----------------------------------------------------------------


def data_url(png_bytes: bytes, mime: str = "image/png") -> str:
    """Base64 data URL for an image."""
    return f"data:{mime};base64,{base64.b64encode(png_bytes).decode('ascii')}"


def image_part(png_bytes: bytes, detail: str = "original") -> dict:
    """OpenAI Responses `input_image` content part."""
    if detail not in IMAGE_DETAILS:
        raise ValueError(f"Unknown image detail '{detail}'.")
    return {"type": "input_image", "image_url": data_url(png_bytes), "detail": detail}


def payload_bytes(parts: list[dict]) -> int:
    """Encoded bytes the image and file data of these content parts add to a
    request (the base64 data URLs, which is what OpenAI measures). Compare
    with REQUEST_MAX_BYTES after rendering a group and before sending it: a
    sheet with an aerial or photo background compresses poorly as PNG."""
    total = 0
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key in ("image_url", "file_data", "text"):
            value = part.get(key)
            if isinstance(value, str):
                total += len(value)
    return total


def file_part(filename: str, pdf_bytes: bytes) -> dict:
    """OpenAI Responses `input_file` content part for a PDF."""
    name = (filename or "document.pdf").strip() or "document.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return {
        "type": "input_file",
        "filename": name,
        "file_data": data_url(pdf_bytes, "application/pdf"),
    }
