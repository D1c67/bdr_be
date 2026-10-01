"""Per-page work inside the sandbox child: geometry, the two JPEG tiers, the
sanitized text file, and the page hazard inventory, folded into one `page`
event.

`process_page(doc, index, out_dir=..., limits=...)` is the only entry point.
It returns the complete body of the `page` event (spec section 3.2) plus the
number of artifact bytes it wrote, and it NEVER raises: every failure becomes
a `status: "failed"` event with an app-authored `detail`, and any artifact
already written for that index is unlinked so a failed page leaves nothing
behind for the parent's directory walk to trip on. The caller (`__main__`)
owns the `page_start` event, which must be on disk before this function is
called, and the progress log.

Rendering (section 3.3):
  * thumb: long side `thumb_long_side` px at `thumb_jpeg_quality`, its own
    PDFium pass (faster than downscaling the reading tier).
  * full: `full_small_long_side` px when the page's long side is at most
    `full_small_threshold_pt` points (letter/tabloid), else `full_long_side`
    px, at `full_jpeg_quality`. The tier used is recorded as
    `tier` ("full_small" | "full").
  * `draw_annots=True`, `may_draw_forms=False`, and the document never gets
    a form environment (init_forms is never called), so widget appearance
    streams render as static annotations and no form-fill code runs.
  * The scale is chosen so the long side lands on EXACTLY the target pixel
    count (pypdfium2 ceils width x scale; a bare division can round to one
    pixel over, which the parent's tier check would refuse).
  * Bitmaps are BGR (3 channels) and become an RGB Pillow image; the JPEG is
    written baseline (no progressive), optimize off, no EXIF, no ICC, so the
    stream is SOI, APP0, DQT, SOF0, DHT, SOS, EOI with exactly
    `protocol.JPEG_COMPONENTS` components, which is all the parent's marker
    walk admits.

Text: the PDFium text page, capped at `max_text_chars_per_page` characters
BEFORE extraction (so a page with a million characters costs one bounded
buffer), sanitized by `textclean.sanitize`, and written as UTF-8. The event
records `chars` (code points written), `truncated` (the cap bit) and the
hazard counts.

Artifacts are written to derived names only (`protocol.page_file`), through
O_NOFOLLOW|O_CREAT|O_TRUNC file descriptors (mode 0600) so a re-render of an
index that already has files simply overwrites them. sha256 and byte counts
are computed from the exact bytes handed to the kernel.

Failure mapping: a page whose long side exceeds `max_page_side_pt` fails as
`page_size` before any render; MemoryError anywhere is `memory`; any other
exception (PDFium refusing to load the page, an EFBIG from RLIMIT_FSIZE, an
encoder error) is `render_error` with the exception CLASS name in the detail
and nothing else (never str(exc), which could echo file content).

Imports: standard library, pypdfium2 and Pillow only.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import time

import pypdfium2 as pdfium
import pypdfium2.raw as raw
from PIL import Image  # noqa: F401 (Pillow and its JPEG plugin must load before `ready`)

from app.sandbox import hazards, textclean
from app.sandbox.protocol import (
    DETAIL_MAX_CHARS,
    FULL_DIR,
    PAGE_FAIL_MEMORY,
    PAGE_FAIL_RENDER,
    PAGE_FAIL_SIZE,
    PAGE_FAILED,
    PAGE_OK,
    TEXT_DIR,
    THUMB_DIR,
    page_file,
)

TIER_FULL = "full"
TIER_FULL_SMALL = "full_small"

_ROTATION_DEGREES = {0: 0, 1: 90, 2: 180, 3: 270}
_ARTIFACT_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC


def tier_for(long_side_pt: float, limits: dict) -> tuple[str, int]:
    """(tier name, long side in px) for the reading tier of a page."""
    if long_side_pt <= limits["full_small_threshold_pt"]:
        return TIER_FULL_SMALL, int(limits["full_small_long_side"])
    return TIER_FULL, int(limits["full_long_side"])


def render_scale(long_side_pt: float, long_side_px: int) -> float:
    """A scale whose ceil(long_side_pt x scale) is exactly long_side_px."""
    return (long_side_px - 0.5) / long_side_pt


def _rotation_degrees(page) -> int:
    return _ROTATION_DEGREES.get(int(raw.FPDFPage_GetRotation(page)), 0)


def _write_artifact(out_dir: str, rel: str, data: bytes, written: list[str]) -> tuple[int, str]:
    """Write `data` to `<out_dir>/<rel>`, remembering the path for cleanup,
    and return (bytes, sha256)."""
    path = os.path.join(out_dir, rel)
    written.append(path)
    fd = os.open(path, _ARTIFACT_FLAGS, 0o600)
    try:
        view = memoryview(data)
        while len(view):
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)
    return len(data), hashlib.sha256(data).hexdigest()


def encode_jpeg(page, *, long_side_pt: float, long_side_px: int, quality: int) -> tuple[bytes, int, int]:
    """Render one tier and return (jpeg bytes, width px, height px)."""
    bitmap = page.render(
        scale=render_scale(long_side_pt, long_side_px),
        may_draw_forms=False,
        draw_annots=True,
    )
    try:
        image = bitmap.to_pil()
    finally:
        bitmap.close()
    if image.mode != "RGB":
        image = image.convert("RGB")
    width, height = image.size
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=int(quality), optimize=False, progressive=False)
    image.close()
    return buf.getvalue(), width, height


def _render_tier(
    page, *, kind_dir: str, index: int, long_side_pt: float, long_side_px: int, quality: int,
    out_dir: str, written: list[str],
) -> dict:
    data, width, height = encode_jpeg(
        page, long_side_pt=long_side_pt, long_side_px=long_side_px, quality=quality
    )
    rel = page_file(kind_dir, index, "jpg")
    nbytes, digest = _write_artifact(out_dir, rel, data, written)
    return {"file": rel, "w": width, "h": height, "bytes": nbytes, "sha256": digest}


def extract_text(page, max_chars: int) -> tuple[str, bool]:
    """Raw PDFium text for the page, capped at `max_chars` characters before
    the buffer is even allocated; second value is the truncation flag."""
    textpage = page.get_textpage()
    try:
        total = textpage.count_chars()
        take = min(total, max_chars)
        text = textpage.get_text_range(0, take) if take > 0 else ""
    finally:
        textpage.close()
    return text, total > max_chars


def _write_text(page, *, index: int, max_chars: int, out_dir: str, written: list[str]) -> tuple[dict, int]:
    raw_text, truncated = extract_text(page, max_chars)
    clean, counts = textclean.sanitize(raw_text)
    rel = page_file(TEXT_DIR, index, "txt")
    nbytes, digest = _write_artifact(out_dir, rel, clean.encode("utf-8"), written)
    event = {
        "file": rel,
        "chars": len(clean),
        "truncated": truncated,
        "sha256": digest,
        "hazards": counts,
    }
    return event, nbytes


def _cleanup(written: list[str]) -> None:
    for path in written:
        try:
            os.unlink(path)
        except OSError:
            pass


def _failed(index: int, code: str, detail: str) -> dict:
    return {
        "status": PAGE_FAILED,
        "index": index,
        "code": code,
        "detail": detail[:DETAIL_MAX_CHARS],
    }


def process_page(doc: pdfium.PdfDocument, index: int, *, out_dir: str, limits: dict) -> tuple[dict, int]:
    """Render, extract and inventory page `index`. Returns (page event body,
    artifact bytes written). Never raises."""
    started = time.monotonic()
    written: list[str] = []
    page = None
    try:
        page = doc.get_page(index)
        width, height = page.get_size()
        if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
            return _failed(index, PAGE_FAIL_RENDER, "The page reports an invalid size."), 0
        rotation = _rotation_degrees(page)
        long_side_pt = max(width, height)
        if long_side_pt > limits["max_page_side_pt"]:
            return (
                _failed(index, PAGE_FAIL_SIZE, "The page is larger than the configured maximum."),
                0,
            )
        tier, full_px = tier_for(long_side_pt, limits)
        thumb = _render_tier(
            page, kind_dir=THUMB_DIR, index=index, long_side_pt=long_side_pt,
            long_side_px=int(limits["thumb_long_side"]), quality=limits["thumb_jpeg_quality"],
            out_dir=out_dir, written=written,
        )
        full = _render_tier(
            page, kind_dir=FULL_DIR, index=index, long_side_pt=long_side_pt,
            long_side_px=full_px, quality=limits["full_jpeg_quality"],
            out_dir=out_dir, written=written,
        )
        text, text_bytes = _write_text(
            page, index=index, max_chars=int(limits["max_text_chars_per_page"]),
            out_dir=out_dir, written=written,
        )
        page_hazards = hazards.page_hazards(page)
        event = {
            "status": PAGE_OK,
            "index": index,
            "width_pt": float(width),
            "height_pt": float(height),
            "rotation": rotation,
            "tier": tier,
            "thumb": thumb,
            "full": full,
            "text": text,
            "hazards": page_hazards,
            "render_ms": int((time.monotonic() - started) * 1000),
        }
        return event, thumb["bytes"] + full["bytes"] + text_bytes
    except MemoryError:
        _cleanup(written)
        return _failed(index, PAGE_FAIL_MEMORY, "The page could not be rendered within memory."), 0
    except Exception as exc:  # noqa: BLE001 (every failure becomes a page event)
        _cleanup(written)
        detail = f"The page could not be rendered ({type(exc).__name__})."
        return _failed(index, PAGE_FAIL_RENDER, detail), 0
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001 (closing a broken page must not mask the event)
                pass
