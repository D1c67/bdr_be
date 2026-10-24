"""The PDF text layer as positioned words and lines (WS1 step 2; spec 5.1
coordinates, 5.3 Word APIs).

This is the only page-facts module that touches PDFium. Every touch runs
under `pdf_split._PDFIUM_LOCK` (PDFium is not thread-safe and the llm_queue
worker runs jobs in threads) and every page and text page is closed
explicitly, as `plans_reader_images` does.

Pipeline per page:
1. `page_frame`: the 5.1 transform from user space to displayed fractions
   (`u = (x - l) / (r - l)`, `v = (t - y) / (t - b)`, then /Rotate 90:
   `(1 - v, u)`, 180: `(1 - u, 1 - v)`, 270: `(v, 1 - u)`).
2. Char table: text, tight charbox, rotation, generated flag, render mode
   (mode 3 is invisible text) and font weight for every char.
3. Census: chars, visible, invisible (render mode 3), off-crop (center
   outside the displayed page), path and image counts, image cover from
   `PdfObject.get_bounds()`, SHX annotation count, sanitizer hazards.
4. Words: chars joined by rotation, vertical band overlap and horizontal gap
   (never by center line: a 36 pt "E2.01" puts the dot on another center
   line). pdfium's U+FFFE soft-hyphen marker joins as "-" with flag 4.
5. SHX annotations ("AutoCAD SHX Text" comments) become words with flag 1.
6. Sanitize every word through `app.sandbox.textclean` (hazards counted).
7. Lines: upright words banded by baseline and split on wide gaps; rotated
   words are single-word lines. Words are re-ordered so each line's words
   are contiguous (`Line.w = [i0, i1]`).
8. `text_class` from the census table (cad_text, sparse, vector_text,
   scanned, scanned_ocr, empty).

Invisible text is dropped from words (and never reaches a prompt) unless the
page is `scanned_ocr`, where it is the OCR layer (flag 8). The raw char
stream is kept at runtime (`PageWords.stream`) for regexes that span words
(scales) and is not stored.
"""

from __future__ import annotations

import ctypes
import math
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from app.services import pdf_split
from app.services.pagefacts.contracts import (
    F_BOLD,
    F_HYPHEN,
    F_OCR,
    F_SHX,
    Bbox,
    Census,
    Frame,
    Line,
    PageWords,
    Word,
    round_box,
)

try:  # the RFP sandbox's text contract, when present
    from app.sandbox.textclean import sanitize as _sanitize_impl
except Exception:  # noqa: BLE001 - optional module; a local copy of the rules below
    _sanitize_impl = None

PAGE_CHAR_CAP = 300_000  # chars read per page before the census marks `truncated`
PATH_CAP = 200_000  # stop walking page objects here (report >= PATH_CAP)
SHX_ANNOT_CAP = 20_000
SHX_MARK = "AutoCAD SHX Text"
SOFT_HYPHEN_MARK = "￾"
INVISIBLE_RENDER_MODE = 3  # FPDF_TEXTRENDERMODE_INVISIBLE
BOLD_WEIGHT = 600

# text_class thresholds (WS1 5.1)
SCANNED_COVER = 0.6
OCR_MIN_INVISIBLE = 50
VISIBLE_SPARSE = 50
VISIBLE_CAD = 500
VECTOR_PATHS = 200

_CONTROL_TABLE: dict[int, None] = {
    c: None for c in list(range(0x00, 0x20)) + list(range(0x7F, 0xA0)) if c not in (0x09, 0x0A)
}
_HAZARD_KEYS = ("replacement_chars", "format_chars", "private_use", "unassigned")


def _local_sanitize(text: str) -> tuple[str, dict[str, int]]:
    """The `app.sandbox.textclean` rules, for installs without the sandbox:
    drop controls (keep \\n and \\t), count and drop U+FFFD, Cf, Co, Cn, Cs."""
    counts = {k: 0 for k in _HAZARD_KEYS}
    text = text.translate(_CONTROL_TABLE)
    if text.isascii():
        return text, counts
    out: list[str] = []
    for ch in text:
        if ch == "�":
            counts["replacement_chars"] += 1
            continue
        cat = unicodedata.category(ch)
        if cat == "Cf":
            counts["format_chars"] += 1
        elif cat == "Co":
            counts["private_use"] += 1
        elif cat in ("Cn", "Cs"):
            counts["unassigned"] += 1
        elif cat == "Cc":
            continue
        else:
            out.append(ch)
    return "".join(out), counts


def sanitize(text: str) -> tuple[str, dict[str, int]]:
    if _sanitize_impl is not None:
        clean, counts = _sanitize_impl(text)
        return clean, dict(counts)
    return _local_sanitize(text)


def empty_hazards() -> dict[str, int]:
    return {k: 0 for k in _HAZARD_KEYS}


def _add_hazards(total: dict[str, int], counts: dict[str, int]) -> None:
    for k, v in counts.items():
        total[k] = total.get(k, 0) + int(v)


# PDFium access -----------------------------------------------------------------


@contextmanager
def open_document(pdf_bytes: bytes) -> Iterator[Any]:
    """A PDFium document opened under the lock. The caller re-takes the lock
    for every page touch (`with pdf_split._PDFIUM_LOCK:`); the document is
    closed under the lock on exit."""
    import pypdfium2 as pdfium

    with pdf_split._PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(pdf_bytes)
        except Exception as exc:  # noqa: BLE001 - PDFium raises PdfiumError variants
            raise ValueError(f"The PDF could not be opened for text extraction ({exc}).") from exc
    try:
        yield doc
    finally:
        with pdf_split._PDFIUM_LOCK:
            doc.close()


def page_frame(page: Any) -> Frame:
    """The 5.1 frame of an open PDFium page: /Rotate, CropBox and the
    displayed size in inches (`get_size()` is already rotated, matching
    `plans_reader_images.page_sizes_in`)."""
    rot = int(page.get_rotation()) % 360
    left, bottom, right, top = page.get_cropbox()
    w_pt, h_pt = page.get_size()
    return Frame(
        rotation=rot, left=left, bottom=bottom, right=right, top=top, w_in=w_pt / 72, h_in=h_pt / 72
    )


@dataclass(slots=True)
class Stream:
    """The raw char stream of a page with lazy boxes: `text[i]` is char i,
    `boxes[i]` its user-space (l, b, r, t) or None for generated and
    zero-area chars."""

    text: str
    boxes: list[tuple[float, float, float, float] | None]
    frame: Frame

    def box_pt(self, i0: int, i1: int) -> tuple[float, float, float, float] | None:
        sel = [
            b
            for i, b in enumerate(self.boxes[i0:i1], start=i0)
            if b is not None and not self.text[i].isspace()
        ]
        if not sel:
            return None
        return (
            min(b[0] for b in sel),
            min(b[1] for b in sel),
            max(b[2] for b in sel),
            max(b[3] for b in sel),
        )

    def bbox(self, i0: int, i1: int) -> Bbox | None:
        box = self.box_pt(i0, i1)
        if box is None:
            return None
        return self.frame.box_to_frac(*box)

    def height_pt(self, i0: int, i1: int) -> float:
        hs = [
            b[3] - b[1]
            for i, b in enumerate(self.boxes[i0:i1], start=i0)
            if b is not None and not self.text[i].isspace()
        ]
        return max(hs) if hs else 0.0

    def baseline_pt(self, i0: int, i1: int) -> float | None:
        bs = [
            b[1]
            for i, b in enumerate(self.boxes[i0:i1], start=i0)
            if b is not None and not self.text[i].isspace()
        ]
        return min(bs) if bs else None


@dataclass(slots=True)
class _Char:
    ch: str
    box: tuple[float, float, float, float] | None
    rot: int
    generated: bool
    invisible: bool
    bold: bool


def _char_rotation(angle: float) -> int:
    """pdfium's char angle (radians, clockwise from +x in user space) to the
    counter-clockwise reading direction 0, 90 (upward), 180, 270 (downward)
    or -1 when it is more than 0.3 rad off every axis."""
    ccw = (-angle) % (2 * math.pi)
    for deg, ref in (
        (0, 0.0),
        (90, math.pi / 2),
        (180, math.pi),
        (270, 3 * math.pi / 2),
        (0, 2 * math.pi),
    ):
        if abs(ccw - ref) < 0.3:
            return deg
    return -1


def _is_bold(raw: Any, index: int) -> bool:
    """Font weight >= 600, or a font name carrying "Bold" (the standard 14
    fonts report weight 0)."""
    import pypdfium2.raw as c

    if c.FPDFText_GetFontWeight(raw, index) >= BOLD_WEIGHT:
        return True
    buf = ctypes.create_string_buffer(128)
    flags = ctypes.c_int()
    if c.FPDFText_GetFontInfo(raw, index, buf, 128, flags) > 0:
        return b"bold" in buf.value.lower()
    return False


def _char_table(tp: Any, count: int) -> tuple[str, list[_Char]]:
    import pypdfium2.raw as c

    text = tp.get_text_range(0, count) if count > 0 else ""
    if len(text) != count:  # pdfium can return fewer chars than count_chars()
        count = min(count, len(text))
    chars: list[_Char] = []
    ld, rd, bd, td = ctypes.c_double(), ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
    raw = tp.raw
    mode_cache: dict[int, tuple[bool, bool]] = {}
    for i in range(count):
        ch = text[i]
        if c.FPDFText_IsGenerated(raw, i) == 1:
            chars.append(_Char(ch, None, 0, True, False, False))
            continue
        box: tuple[float, float, float, float] | None = None
        if c.FPDFText_GetCharBox(raw, i, ld, rd, bd, td):
            box = (ld.value, bd.value, rd.value, td.value)
            if box[2] - box[0] <= 0 and box[3] - box[1] <= 0:
                box = None
        obj = c.FPDFText_GetTextObject(raw, i)
        key = ctypes.cast(obj, ctypes.c_void_p).value or 0
        flags = mode_cache.get(key)
        if flags is None:
            invisible = bool(key) and c.FPDFTextObj_GetTextRenderMode(obj) == INVISIBLE_RENDER_MODE
            flags = (invisible, _is_bold(raw, i))
            mode_cache[key] = flags
        rot = _char_rotation(c.FPDFText_GetCharAngle(raw, i)) if box is not None else 0
        chars.append(_Char(ch, box, rot, False, flags[0], flags[1]))
    return text, chars


@dataclass(slots=True)
class _WordAcc:
    text: str
    left: float
    bottom: float
    right: float
    top: float
    h: float
    rot: int
    flags: int


def _same_word(cur: _WordAcc, box: tuple[float, float, float, float], rot: int, h: float) -> bool:
    """The 5.1 join test: same rotation, vertical band overlap (or center
    inside the band) and a horizontal gap within [-0.3 H, 0.45 H]; the axes
    transpose for rotated text."""
    if cur.rot != rot:
        return False
    left, bottom, right, top = box
    hh = max(h, cur.h, 0.5)
    if rot == 0 or rot == 180:
        overlap = min(top, cur.top) - max(bottom, cur.bottom)
        center = (bottom + top) / 2
        same_band = overlap > -0.05 * hh or (cur.bottom - 0.1 * hh <= center <= cur.top + 0.1 * hh)
        gap = (left - cur.right) if rot == 0 else (cur.left - right)
    elif rot == 90 or rot == 270:
        overlap = min(right, cur.right) - max(left, cur.left)
        center = (left + right) / 2
        same_band = overlap > -0.05 * hh or (cur.left - 0.1 * hh <= center <= cur.right + 0.1 * hh)
        gap = (bottom - cur.top) if rot == 90 else (cur.bottom - top)
    else:
        cx, cy = (left + right) / 2, (bottom + top) / 2
        ccx, ccy = (cur.left + cur.right) / 2, (cur.bottom + cur.top) / 2
        return math.hypot(cx - ccx, cy - ccy) < 1.5 * hh
    return same_band and -0.3 * hh <= gap <= 0.45 * hh


def _build_words(
    chars: list[_Char], frame: Frame, *, include_invisible: bool, census: Census
) -> list[Word]:
    accs: list[_WordAcc] = []
    cur: _WordAcc | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None and cur.text:
            accs.append(cur)
        cur = None

    for chr_ in chars:
        ch = chr_.ch
        if chr_.generated:
            census.generated_chars += 1
            flush()
            continue
        if ch == SOFT_HYPHEN_MARK:
            if cur is not None:
                cur.text += "-"
                cur.flags |= F_HYPHEN
            continue
        if ch.isspace():
            flush()
            continue
        if chr_.box is None:
            flush()
            continue
        if chr_.invisible:
            census.invisible_chars += 1
            if not include_invisible:
                flush()
                continue
        left, bottom, right, top = chr_.box
        ux, uy = frame.to_frac((left + right) / 2, (bottom + top) / 2)
        if not (-0.001 <= ux <= 1.001 and -0.001 <= uy <= 1.001):
            census.off_crop_chars += 1
            flush()
            continue
        if not chr_.invisible:
            census.visible_chars += 1
        rot = chr_.rot
        h = (top - bottom) if rot in (0, 180, -1) else (right - left)
        if cur is not None and _same_word(cur, chr_.box, rot, h):
            cur.text += ch
            cur.left = min(cur.left, left)
            cur.right = max(cur.right, right)
            cur.bottom = min(cur.bottom, bottom)
            cur.top = max(cur.top, top)
            cur.h = max(cur.h, h)
            continue
        flush()
        flags = (F_BOLD if chr_.bold else 0) | (F_OCR if chr_.invisible else 0)
        cur = _WordAcc(ch, left, bottom, right, top, h, rot, flags)
    flush()

    words: list[Word] = []
    for acc in accs:
        clean, counts = sanitize(acc.text)
        _add_hazards(census.hazards, counts)
        clean = clean.strip()
        if not clean:
            continue
        # The displayed page is the user page turned clockwise by /Rotate,
        # so a counter-clockwise reading direction loses that much.
        r = (acc.rot - frame.rotation) % 360 if acc.rot >= 0 else -1
        words.append(
            Word(
                t=clean,
                b=frame.box_to_frac(acc.left, acc.bottom, acc.right, acc.top),
                h=round(acc.h, 2),
                r=r,
                f=acc.flags,
            )
        )
    return words


def _annot_string(annot: Any, key: bytes) -> str:
    import pypdfium2.raw as c

    n = int(c.FPDFAnnot_GetStringValue(annot, key, None, 0))
    if n <= 2:
        return ""
    buf = (ctypes.c_ushort * (n // 2 + 1))()
    c.FPDFAnnot_GetStringValue(annot, key, buf, n)
    raw = bytes(buf)[: n - 2]
    return raw.decode("utf-16-le", errors="ignore")


def _shx_words(page: Any, frame: Frame, census: Census) -> list[Word]:
    """Words harvested from "AutoCAD SHX Text" comment annotations: AutoCAD
    exports SHX fonts as vector paths and, when asked, mirrors their text in
    annotations. Each annotation's words share its rect, split by character
    share. Flag 1."""
    import pypdfium2.raw as c

    out: list[Word] = []
    try:
        n = int(c.FPDFPage_GetAnnotCount(page.raw))
    except Exception:  # noqa: BLE001 - annotation access is best effort
        return out
    for i in range(min(n, SHX_ANNOT_CAP)):
        annot = c.FPDFPage_GetAnnot(page.raw, i)
        if not annot:
            continue
        try:
            title = _annot_string(annot, b"T")
            subj = _annot_string(annot, b"Subj")
            if not (title.startswith(SHX_MARK) or subj.startswith(SHX_MARK)):
                continue
            contents = _annot_string(annot, b"Contents")
            rect = c.FS_RECTF()
            if not c.FPDFAnnot_GetRect(annot, rect):
                continue
            census.shx_annots += 1
            clean, counts = sanitize(contents)
            _add_hazards(census.hazards, counts)
            parts = clean.split()
            if not parts:
                continue
            # SHX comments read left-to-right on the displayed sheet: split the
            # displayed rect by character share.
            dx0, dy0, dx1, dy1 = frame.box_to_frac(
                min(rect.left, rect.right),
                min(rect.bottom, rect.top),
                max(rect.left, rect.right),
                max(rect.bottom, rect.top),
            )
            total = sum(len(p) for p in parts) + (len(parts) - 1)
            width = dx1 - dx0
            h = (dy1 - dy0) * frame.h_in * 72
            pos = 0
            for part in parts:
                x0 = dx0 + width * pos / max(total, 1)
                x1 = dx0 + width * (pos + len(part)) / max(total, 1)
                pos += len(part) + 1
                census.shx_chars += len(part)
                out.append(
                    Word(t=part, b=round_box((x0, dy0, x1, dy1)), h=round(h, 2), r=0, f=F_SHX)
                )
        finally:
            c.FPDFPage_CloseAnnot(annot)
    return out


def _object_census(page: Any, frame: Frame, census: Census) -> None:
    """Path and image counts and the image cover (union-free sum of
    `get_bounds()` areas clipped to the CropBox, capped at 1)."""
    import pypdfium2.raw as c

    page_area = max((frame.right - frame.left) * (frame.top - frame.bottom), 1.0)
    cover = 0.0
    paths = 0
    images = 0
    try:
        for obj in page.get_objects(max_depth=2):
            kind = obj.type
            if kind == c.FPDF_PAGEOBJ_PATH:
                paths += 1
                if paths >= PATH_CAP:
                    break
            elif kind == c.FPDF_PAGEOBJ_IMAGE:
                images += 1
                try:
                    left, bottom, right, top = obj.get_bounds()
                except Exception:  # noqa: BLE001 - a loose object has no bounds
                    continue
                cl = max(left, frame.left)
                cr = min(right, frame.right)
                cb = max(bottom, frame.bottom)
                ct = min(top, frame.top)
                if cr > cl and ct > cb:
                    cover += (cr - cl) * (ct - cb) / page_area
    except Exception:  # noqa: BLE001 - object walking is best effort
        pass
    census.path_count = paths
    census.image_count = images
    census.image_cover = min(1.0, cover)


def text_class_for(census: Census) -> str:
    """The 5.1 table. `visible` counts SHX words as text."""
    visible = census.visible_chars + census.shx_chars
    if census.image_cover >= SCANNED_COVER and census.invisible_chars >= OCR_MIN_INVISIBLE:
        return "scanned_ocr"
    if census.image_cover >= SCANNED_COVER and visible < VISIBLE_SPARSE:
        return "scanned"
    if visible >= VISIBLE_CAD:
        return "cad_text"
    if visible >= VISIBLE_SPARSE:
        return "sparse"
    if census.path_count > VECTOR_PATHS:
        return "vector_text"
    return "empty"


# Lines ----------------------------------------------------------------------------


@dataclass(slots=True)
class _Band:
    cy: float
    n: int
    h: float
    idx: list[int]


def build_lines(words: list[Word], w_in: float, h_in: float) -> tuple[list[Word], list[Line]]:
    """Group upright words (r == 0) into lines: band by baseline (|cy - band|
    < 0.45 x max height), then split a band on gaps wider than 1.1 x the
    taller neighbour. Rotated words form single-word lines. Returns the words
    re-ordered so each line's words are contiguous, and the lines sorted
    top-to-bottom then left-to-right."""
    sx, sy = w_in * 72, h_in * 72
    upright = [i for i, w in enumerate(words) if w.r == 0]
    others = [i for i, w in enumerate(words) if w.r != 0]
    bands: list[_Band] = []
    for i in sorted(upright, key=lambda k: (words[k].cy, words[k].b[0])):
        w = words[i]
        cy = w.cy * sy
        placed = False
        for band in reversed(bands[-12:]):
            if abs(band.cy - cy) < 0.45 * max(w.h, band.h, 0.5):
                band.cy = (band.cy * band.n + cy) / (band.n + 1)
                band.n += 1
                band.h = max(band.h, w.h)
                band.idx.append(i)
                placed = True
                break
        if not placed:
            bands.append(_Band(cy, 1, w.h, [i]))

    groups: list[list[int]] = []
    for band in bands:
        idx = sorted(band.idx, key=lambda k: words[k].b[0])
        cur: list[int] = []
        for k in idx:
            if cur:
                prev = words[cur[-1]]
                gap = (words[k].b[0] - prev.b[2]) * sx
                hh = max(prev.h, words[k].h, 0.5)
                if not (-0.2 * hh <= gap < 1.1 * hh):
                    groups.append(cur)
                    cur = []
            cur.append(k)
        if cur:
            groups.append(cur)
    for k in others:
        groups.append([k])

    def group_key(g: list[int]) -> tuple[float, float]:
        ys = [words[k].cy for k in g]
        return (sum(ys) / len(ys), min(words[k].b[0] for k in g))

    groups.sort(key=group_key)
    ordered: list[Word] = []
    lines: list[Line] = []
    for g in groups:
        i0 = len(ordered)
        ws = [words[k] for k in g]
        ordered.extend(ws)
        box = round_box(
            (
                min(w.b[0] for w in ws),
                min(w.b[1] for w in ws),
                max(w.b[2] for w in ws),
                max(w.b[3] for w in ws),
            )
        )
        lines.append(
            Line(
                t=" ".join(w.t for w in ws),
                b=box,
                w=[i0, i0 + len(ws)],
                h=round(max(w.h for w in ws), 2),
            )
        )
    return ordered, lines


# Entry points ---------------------------------------------------------------------


def extract_page(page: Any, *, page_number: int | None = None, objects: bool = True) -> PageWords:
    """Words, lines, census, stream and text class of one open PDFium page.
    The caller holds `pdf_split._PDFIUM_LOCK`."""
    frame = page_frame(page)
    census = Census(hazards=empty_hazards())
    if objects:
        _object_census(page, frame, census)
    tp = page.get_textpage()
    try:
        total = int(tp.count_chars())
        take = min(total, PAGE_CHAR_CAP)
        census.chars = total
        census.truncated = total > PAGE_CHAR_CAP
        text, chars = _char_table(tp, take)
    finally:
        tp.close()
    # A first pass only counts invisible chars so the OCR decision can be
    # made before words are built.
    invisible = sum(
        1 for ch in chars if ch.invisible and ch.box is not None and not ch.ch.isspace()
    )
    include_invisible = census.image_cover >= SCANNED_COVER and invisible >= OCR_MIN_INVISIBLE
    words = _build_words(chars, frame, include_invisible=include_invisible, census=census)
    shx = _shx_words(page, frame, census)
    words.extend(shx)
    ordered, lines = build_lines(words, frame.w_in, frame.h_in)
    census.words = len(ordered)
    text_class = text_class_for(census)
    stream = Stream(
        text=text, boxes=[ch.box if not ch.generated else None for ch in chars], frame=frame
    )
    return PageWords(
        page_w_in=round(frame.w_in, 4),
        page_h_in=round(frame.h_in, 4),
        rotation=frame.rotation,
        words=ordered,
        lines=lines,
        text_class=text_class,
        census=census,
        stream=stream,
        frame=frame,
        page=page_number,
    )


def positioned_words(pdf_bytes: bytes, page_index0: int, *, objects: bool = True) -> PageWords:
    """`PageWords` for one 0-based page of a PDF (pure: bytes in, facts out;
    for derive, offline eval and scripts). Raises ValueError for a broken PDF
    or a page outside the document."""
    with open_document(pdf_bytes) as doc:
        with pdf_split._PDFIUM_LOCK:
            total = len(doc)
            if not 0 <= page_index0 < total:
                raise ValueError(f"Page {page_index0 + 1} is outside the document (1-{total}).")
            page = doc[page_index0]
            try:
                return extract_page(page, page_number=page_index0 + 1, objects=objects)
            finally:
                page.close()


def page_count(pdf_bytes: bytes) -> int:
    return pdf_split.page_count(pdf_bytes)
