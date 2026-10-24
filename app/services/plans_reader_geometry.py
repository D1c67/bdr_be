"""Measurement geometry (WS3 steps B1 and B4; docs/AI_ESTIMATING_BUILD_SPEC.md
5.1 coordinates, 6 WS3).

Four pure parts over WS1 page facts and PDFium page objects:

1. Scale parsing. `parse_scale_text` reads a scale string on its own:
   architectural (`1/4" = 1'-0"` -> 4.0 ft per in, `1/16"` -> 16, `3/64"`
   -> 21.3333, `1-1/2"` -> 0.6667, `3" = 1'-0"` -> 0.3333), engineering
   (`1" = 20'` -> 20, `1" = 100 FT` -> 100), ratio (`SCALE 1:48` -> 4.0,
   `SCALE 1:100` -> 8.3333) only when a scale word is present, timestamps
   (`1:36:22 PM`, `10:30`) rejected, and the NTS variants (`N.T.S.`, `NTS`,
   `NOT TO SCALE`, `NO SCALE`, `SCALE: NONE`, `AS NOTED`, `AS SHOWN`) which
   give `ft_per_in` None with `nts` True. Unknown text gives None.

2. Viewports. `detect_viewports` joins the WS1 views of a page with frame
   rectangles found in the vector segments: a view owns the smallest frame
   that contains its scale anchor or sits right above it (titles print under
   the frame). Views in legend, abbreviation, schedule and notes blocks and
   WS1 `example` views are skipped. Without a frame the single plan view
   owns the plan area (WS1's rule); otherwise regions stay None. Scales are
   never guessed: `reduced_print_check` reports a suspected half-size print
   (page size at half a standard sheet, or a reduced-print cue in the text)
   as a flag and never changes `ft_per_in`.

3. Vector segments. `index_segments` reads the page's path objects once
   (pypdfium2 page objects under `pdf_split._PDFIUM_LOCK`), applies each
   object's matrix and the 5.1 frame, and keeps straight segments as page
   fractions. `SegmentCache` indexes a page once per run; `rectangles`
   assembles axis-aligned frames from long horizontal and vertical segments
   (dashed lines are merged first).

4. Registration. `register_sheets` maps one sheet's inches onto another's:
   `identity_frame` (at least 3 identical tokens and at least 50% of the
   common tokens at identical positions -> the identity transform at
   confidence 1.0); otherwise a shared-token median solve over the four
   right-angle rotations plus a mirror check, keeping the lowest residual,
   with k fixed from the scale ratio when both scales are known; otherwise a
   single-anchor registration (k from the scale ratio, rotation 0,
   translation from one unique multi-word label) at confidence 0.5,
   cross-checked against the callout-box rectangles of the destination
   sheet when its segments are indexed (a pass keeps 0.5 with
   `callout_checked`; a failure drops to 0.3 with `callout_mismatch`, and
   the measure stage never routes through it). `chain_registrations` composes
   registrations through identity frames (7154: E2.01 -> ED2.01 identity,
   ED2.01 -> E1.00 single anchor, so E2.01 -> E1.00 by chain).

Tokens for registration are plan-area words only: words inside non-plan
regions, in the view title or scale text, within the page margin (plot
stamps), bare numbers, dates and times are dropped. Positions are sheet
inches (fraction x `w_in` or `h_in`); transforms act in inches.

No model, no database, no network; PDFium only inside `index_segments`.
Nothing here carries prices.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from app.services.pagefacts.contracts import (
    NON_PLAN_REGION_KINDS,
    Bbox,
    PageFacts,
    PageWords,
    Region,
    View,
    Word,
    round_box,
)
from app.services.pagefacts.regions import in_plan_area

__all__ = [
    "ScaleParse",
    "parse_scale_text",
    "ft_per_in",
    "Viewport",
    "detect_viewports",
    "PageScales",
    "scales_for_page",
    "reduced_print_check",
    "Segment",
    "SegmentIndex",
    "index_segments",
    "index_page_segments",
    "SegmentCache",
    "Transform",
    "Token",
    "SheetTokens",
    "sheet_tokens",
    "Registration",
    "identity_frame",
    "register_sheets",
    "chain_registrations",
]

# Scale parsing ----------------------------------------------------------------------

SCALE_WORD_RE = re.compile(r"\bSCALE\b", re.I)
ARCH_FRACTION_RE = re.compile(
    r"(?:(?P<whole>\d{1,2})\s*[-\s]\s*)?(?P<num>\d{1,2})\s*/\s*(?P<den>\d{1,3})\s*"
    r"(?:\"|''|”|IN(?:CH(?:ES)?)?\.?)\s*=\s*1\s*(?:'|’|FT\b|FOOT\b)"
    r"(?:\s*-?\s*0\s*(?:\"|''|”)?)?",
    re.I,
)
ARCH_INCH_RE = re.compile(
    r"(?<![\d/])(?P<inch>\d{1,2})\s*(?:\"|''|”|IN(?:CH(?:ES)?)?\.?)\s*=\s*1\s*"
    r"(?:'|’|FT\b|FOOT\b)(?:\s*-?\s*0\s*(?:\"|''|”)?)?",
    re.I,
)
ENGINEERING_RE = re.compile(
    r"\b1\s*(?:\"|''|”|IN(?:CH)?\.?)\s*=\s*(?P<ft>\d{1,4}(?:\.\d+)?)\s*"
    r"(?:'|’|FT\b|FEET\b|FOOT\b)(?:\s*-?\s*0\s*(?:\"|''|”)?)?",
    re.I,
)
RATIO_RE = re.compile(r"(?<![\d:.])1\s*:\s*(?P<r>\d{1,5})(?![\d:])")
# A ratio reads as a time only with a seconds part or an AM/PM suffix
# (`1:36:22`, `1:30 PM`); `SCALE 1:48` is a ratio.
TIME_RE = re.compile(r"\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b", re.I)
AMPM_RE = re.compile(r"^\s*[AP]\.?M\b", re.I)
NTS_RE = re.compile(
    r"\bN\.?\s?T\.?\s?S\b\.?|NOT\s+TO\s+SCALE|NO\s+SCALE|SCALE\s*[:=]?\s*NONE\b"
    r"|\bNONE\b|AS\s+NOTED|AS\s+SHOWN|\bNTS\b",
    re.I,
)
NTS_NEEDS_SCALE_WORD: frozenset[str] = frozenset({"NONE", "AS NOTED", "AS SHOWN"})
RATIO_MIN = 2
RATIO_MAX = 20000


@dataclass(slots=True)
class ScaleParse:
    """`ft_per_in` is None for NTS (`nts` True). `kind` is architectural,
    engineering, ratio or nts."""

    ft_per_in: float | None
    nts: bool
    kind: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"ft_per_in": self.ft_per_in, "nts": self.nts, "kind": self.kind, "text": self.text}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _ratio_is_time(text: str, m: re.Match[str]) -> bool:
    for t in TIME_RE.finditer(text):
        if t.start() <= m.start() < t.end():
            return True
    tail = text[m.end() :]
    return bool(AMPM_RE.match(tail))


def parse_scale_text(text: str | None) -> ScaleParse | None:
    """A scale string on its own, without bubble context (the WS1 stream
    parser handles the view-number bubble). None when the text holds no
    scale; `ft_per_in` None with `nts` True for the NTS variants."""
    if not text:
        return None
    raw = _squash(str(text))
    m = ARCH_FRACTION_RE.search(raw)
    if m:
        num, den = int(m.group("num")), int(m.group("den"))
        if den <= 0 or num <= 0:
            return None
        frac = num / den
        if m.group("whole"):
            frac += int(m.group("whole"))
        return ScaleParse(round(1 / frac, 4), False, "architectural", _squash(m.group(0)))
    m = ARCH_INCH_RE.search(raw)
    if m:
        inch = int(m.group("inch"))
        if inch <= 0:
            return None
        return ScaleParse(round(1 / inch, 4), False, "architectural", _squash(m.group(0)))
    m = ENGINEERING_RE.search(raw)
    if m:
        ft = float(m.group("ft"))
        if ft <= 0:
            return None
        return ScaleParse(round(ft, 4), False, "engineering", _squash(m.group(0)))
    has_scale_word = bool(SCALE_WORD_RE.search(raw))
    m = RATIO_RE.search(raw)
    if m and has_scale_word and not _ratio_is_time(raw, m):
        r = int(m.group("r"))
        if RATIO_MIN <= r <= RATIO_MAX:
            return ScaleParse(round(r / 12, 4), False, "ratio", _squash(m.group(0)))
    m = NTS_RE.search(raw)
    if m:
        phrase = _squash(m.group(0)).upper()
        if phrase in NTS_NEEDS_SCALE_WORD and not has_scale_word:
            return None
        return ScaleParse(None, True, "nts", _squash(m.group(0)))
    return None


def ft_per_in(text: str | None) -> float | None:
    parsed = parse_scale_text(text)
    return parsed.ft_per_in if parsed else None


# Vector segments --------------------------------------------------------------------

MAX_SEGMENTS = 400_000
SEGMENT_LINETO = 0
SEGMENT_BEZIERTO = 1
SEGMENT_MOVETO = 2
GRID_CELLS = 64


@dataclass(slots=True)
class Segment:
    """One straight segment in page fractions (`curve` marks a bezier chord)."""

    x0: float
    y0: float
    x1: float
    y1: float
    curve: bool = False

    def length_in(self, w_in: float, h_in: float) -> float:
        return math.hypot((self.x1 - self.x0) * w_in, (self.y1 - self.y0) * h_in)

    def horizontal(self, tol: float) -> bool:
        return abs(self.y1 - self.y0) <= tol

    def vertical(self, tol: float) -> bool:
        return abs(self.x1 - self.x0) <= tol

    def to_list(self) -> list[float]:
        return [round(self.x0, 4), round(self.y0, 4), round(self.x1, 4), round(self.y1, 4)]


@dataclass(slots=True)
class SegmentIndex:
    """Every straight vector segment of one page, with a coarse grid for
    neighbourhood queries. Built once per page per run (`SegmentCache`)."""

    page: int
    w_in: float
    h_in: float
    segments: list[Segment] = field(default_factory=list)
    paths: int = 0
    truncated: bool = False
    _grid: dict[tuple[int, int], list[int]] | None = field(default=None, repr=False)

    def __len__(self) -> int:
        return len(self.segments)

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return (
            min(GRID_CELLS - 1, max(0, int(x * GRID_CELLS))),
            min(GRID_CELLS - 1, max(0, int(y * GRID_CELLS))),
        )

    def _ensure_grid(self) -> dict[tuple[int, int], list[int]]:
        if self._grid is None:
            grid: dict[tuple[int, int], list[int]] = defaultdict(list)
            for i, s in enumerate(self.segments):
                cx0, cy0 = self._cell(min(s.x0, s.x1), min(s.y0, s.y1))
                cx1, cy1 = self._cell(max(s.x0, s.x1), max(s.y0, s.y1))
                for cx in range(cx0, cx1 + 1):
                    for cy in range(cy0, cy1 + 1):
                        grid[(cx, cy)].append(i)
            self._grid = dict(grid)
        return self._grid

    def near(self, x: float, y: float, radius_in: float) -> list[Segment]:
        """Segments with an endpoint within `radius_in` sheet inches of (x, y)."""
        grid = self._ensure_grid()
        rx, ry = radius_in / (self.w_in or 1.0), radius_in / (self.h_in or 1.0)
        cx0, cy0 = self._cell(x - rx, y - ry)
        cx1, cy1 = self._cell(x + rx, y + ry)
        seen: set[int] = set()
        out: list[Segment] = []
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                for i in grid.get((cx, cy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    s = self.segments[i]
                    for ex, ey in ((s.x0, s.y0), (s.x1, s.y1)):
                        if math.hypot((ex - x) * self.w_in, (ey - y) * self.h_in) <= radius_in:
                            out.append(s)
                            break
        return out

    def axis_aligned(
        self, min_len_in: float, *, tol_in: float = 0.02
    ) -> tuple[list[Segment], list[Segment]]:
        """Long horizontal and vertical segments (dashes merged along a line)."""
        tx, ty = tol_in / (self.w_in or 1.0), tol_in / (self.h_in or 1.0)
        hs = [s for s in self.segments if not s.curve and s.horizontal(ty)]
        vs = [s for s in self.segments if not s.curve and s.vertical(tx)]
        hs = _merge_collinear(hs, axis="h", tol=ty, gap=0.12 / (self.w_in or 1.0))
        vs = _merge_collinear(vs, axis="v", tol=tx, gap=0.12 / (self.h_in or 1.0))
        hs = [s for s in hs if s.length_in(self.w_in, self.h_in) >= min_len_in]
        vs = [s for s in vs if s.length_in(self.w_in, self.h_in) >= min_len_in]
        return hs, vs

    def rectangles(
        self,
        *,
        min_w_in: float = 1.0,
        min_h_in: float = 1.0,
        tol_in: float = 0.1,
        max_cover: float = 0.95,
    ) -> list[Bbox]:
        """Axis-aligned frames: two horizontals sharing an x-span and two
        verticals sharing a y-span whose ends meet within `tol_in`. The page
        border (cover above `max_cover`) is dropped. Sorted by area."""
        hs, vs = self.axis_aligned(min(min_w_in, min_h_in))
        tx, ty = tol_in / (self.w_in or 1.0), tol_in / (self.h_in or 1.0)
        out: list[Bbox] = []
        seen: set[tuple[float, float, float, float]] = set()
        hs_sorted = sorted(hs, key=lambda s: min(s.y0, s.y1))
        for i, top in enumerate(hs_sorted):
            tx0, tx1 = min(top.x0, top.x1), max(top.x0, top.x1)
            ty0 = top.y0
            for bottom in hs_sorted[i + 1 :]:
                by0 = bottom.y0
                if by0 - ty0 < min_h_in / (self.h_in or 1.0):
                    continue
                bx0, bx1 = min(bottom.x0, bottom.x1), max(bottom.x0, bottom.x1)
                if abs(bx0 - tx0) > tx or abs(bx1 - tx1) > tx:
                    continue
                left = _vertical_at(vs, tx0, ty0, by0, tx, ty)
                right = _vertical_at(vs, tx1, ty0, by0, tx, ty)
                if left is None or right is None:
                    continue
                box = round_box((tx0, ty0, tx1, by0))
                if (box[2] - box[0]) * self.w_in < min_w_in:
                    continue
                cover = (box[2] - box[0]) * (box[3] - box[1])
                if cover > max_cover:
                    continue
                key = tuple(box)
                if key in seen:
                    continue
                seen.add(key)  # type: ignore[arg-type]
                out.append(box)
        out.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "w_in": self.w_in,
            "h_in": self.h_in,
            "paths": self.paths,
            "segments": len(self.segments),
            "truncated": self.truncated,
        }


def _vertical_at(
    vs: list[Segment], x: float, y0: float, y1: float, tx: float, ty: float
) -> Segment | None:
    for s in vs:
        if abs(s.x0 - x) > tx:
            continue
        s0, s1 = min(s.y0, s.y1), max(s.y0, s.y1)
        if s0 <= y0 + ty and s1 >= y1 - ty:
            return s
    return None


def _merge_collinear(segs: list[Segment], *, axis: str, tol: float, gap: float) -> list[Segment]:
    """Join dashes that share a line (within `tol`) and leave gaps under `gap`."""
    if not segs:
        return []
    if axis == "h":
        keyed = sorted(segs, key=lambda s: (round(s.y0 / tol) if tol else s.y0, min(s.x0, s.x1)))
    else:
        keyed = sorted(segs, key=lambda s: (round(s.x0 / tol) if tol else s.x0, min(s.y0, s.y1)))
    out: list[Segment] = []
    cur: Segment | None = None
    for s in keyed:
        if axis == "h":
            a0, a1, line = min(s.x0, s.x1), max(s.x0, s.x1), s.y0
        else:
            a0, a1, line = min(s.y0, s.y1), max(s.y0, s.y1), s.x0
        if cur is not None:
            if axis == "h":
                c0, c1, cline = min(cur.x0, cur.x1), max(cur.x0, cur.x1), cur.y0
            else:
                c0, c1, cline = min(cur.y0, cur.y1), max(cur.y0, cur.y1), cur.x0
            if abs(line - cline) <= tol and a0 <= c1 + gap:
                n0, n1 = min(c0, a0), max(c1, a1)
                cur = (
                    Segment(n0, cline, n1, cline) if axis == "h" else Segment(cline, n0, cline, n1)
                )
                continue
            out.append(cur)
        cur = Segment(a0, line, a1, line) if axis == "h" else Segment(line, a0, line, a1)
    if cur is not None:
        out.append(cur)
    return out


def index_page_segments(page: Any, frame: Any, *, max_segments: int = MAX_SEGMENTS) -> SegmentIndex:
    """Segments of one open PDFium page. The caller holds
    `pdf_split._PDFIUM_LOCK`. `frame` is the WS1 `Frame` of the page."""
    import ctypes

    import pypdfium2.raw as c

    out = SegmentIndex(
        page=0, w_in=round(frame.w_in, 4), h_in=round(frame.h_in, 4), segments=[], paths=0
    )
    segs = out.segments
    x = ctypes.c_float()
    y = ctypes.c_float()
    try:
        for obj in page.get_objects(max_depth=2):
            if obj.type != c.FPDF_PAGEOBJ_PATH:
                continue
            out.paths += 1
            matrices = []
            node = obj
            while node is not None:
                try:
                    matrices.append(node.get_matrix())
                except Exception:  # noqa: BLE001 - a loose object has no matrix
                    break
                node = getattr(node, "container", None) if getattr(node, "level", 0) else None
            n = int(c.FPDFPath_CountSegments(obj.raw))
            prev: tuple[float, float] | None = None
            start: tuple[float, float] | None = None
            for i in range(n):
                seg = c.FPDFPath_GetPathSegment(obj.raw, i)
                if not c.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y)):
                    continue
                kind = int(c.FPDFPathSegment_GetType(seg))
                px, py = float(x.value), float(y.value)
                for m in matrices:
                    px, py = m.on_point(px, py)
                fx, fy = frame.to_frac(px, py)
                if kind == SEGMENT_MOVETO:
                    prev = (fx, fy)
                    start = prev
                    continue
                if prev is not None:
                    segs.append(Segment(prev[0], prev[1], fx, fy, curve=kind == SEGMENT_BEZIERTO))
                prev = (fx, fy)
                if c.FPDFPathSegment_GetClose(seg) and start is not None and prev != start:
                    segs.append(Segment(prev[0], prev[1], start[0], start[1]))
                    prev = start
                if len(segs) >= max_segments:
                    out.truncated = True
                    return out
    except Exception:  # noqa: BLE001 - object walking is best effort; partial index stands
        out.truncated = True
    return out


def index_segments(pdf_bytes: bytes, page_index0: int, *, max_segments: int = MAX_SEGMENTS) -> SegmentIndex:
    """`SegmentIndex` for one 0-based page of a PDF, under the PDFium lock."""
    from app.services import pdf_split
    from app.services.pagefacts import textlayer

    with textlayer.open_document(pdf_bytes) as doc:
        with pdf_split._PDFIUM_LOCK:
            total = len(doc)
            if not 0 <= page_index0 < total:
                raise ValueError(f"Page {page_index0 + 1} is outside the document (1-{total}).")
            page = doc[page_index0]
            try:
                frame = textlayer.page_frame(page)
                idx = index_page_segments(page, frame, max_segments=max_segments)
                idx.page = page_index0 + 1
                return idx
            finally:
                page.close()


class SegmentCache:
    """Index each (document key, page) once per run. `loader(page)` returns the
    `SegmentIndex`; a failed load is cached as None so a page is never
    re-read in the same run."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, int], SegmentIndex | None] = {}

    def get(
        self, doc_key: str, page: int, loader: Callable[[int], SegmentIndex]
    ) -> SegmentIndex | None:
        key = (doc_key, int(page))
        if key in self._items:
            return self._items[key]
        try:
            idx = loader(int(page))
        except Exception:  # noqa: BLE001 - a broken page leaves measurement without vectors
            idx = None
        self._items[key] = idx
        return idx

    def __len__(self) -> int:
        return len(self._items)

    def stats(self) -> dict[str, Any]:
        return {
            "pages": len(self._items),
            "segments": sum(len(v) for v in self._items.values() if v is not None),
            "failed": sum(1 for v in self._items.values() if v is None),
        }


# Viewports -----------------------------------------------------------------------------

SKIP_VIEW_REGION_KINDS: frozenset[str] = frozenset(
    {"legend", "abbreviations", "schedule", "general_notes", "sheet_notes", "keynotes", "key_plan"}
)
FRAME_MIN_IN = 3.0
SHEET_BORDER_COVER = 0.75  # a frame covering this much of the page is the border
TITLE_BELOW_REACH_IN = 1.5
STANDARD_SHEETS_IN: tuple[tuple[str, float, float], ...] = (
    ("ARCH E", 36.0, 48.0),
    ("ARCH E1", 30.0, 42.0),
    ("ARCH D", 24.0, 36.0),
    ("ANSI E", 34.0, 44.0),
    ("ANSI D", 22.0, 34.0),
)
REDUCED_CUE_RE = re.compile(
    r"HALF\s*SIZE|REDUCED|NOT\s+TO\s+SCALE\s+(?:WHEN|IF)\s+PRINTED|PLOT(?:TED)?\s+AT|11\s*X\s*17"
    r"|IF\s+(?:THIS\s+)?BAR\s+(?:DOES\s+NOT|IS\s+NOT)|VERIFY\s+SCALE|SCALE\s+BAR|DRAWING\s+MAY\s+BE\s+REDUCED",
    re.I,
)
HALF_SIZE_TOL = 0.04
SMALL_SHEET_IN: tuple[float, float] = (24.0, 18.0)


@dataclass(slots=True)
class Viewport:
    view_id: str
    title: str | None
    kind: str
    scale_text: str | None
    ft_per_in: float | None
    nts: bool
    region: Bbox | None
    source: str  # frame | plan_area | none
    anchor: Bbox | None = None
    example: bool = False
    skipped: str | None = None

    def contains(self, x: float, y: float) -> bool:
        return bool(self.region) and (
            self.region[0] <= x <= self.region[2] and self.region[1] <= y <= self.region[3]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "title": self.title,
            "kind": self.kind,
            "scale_text": self.scale_text,
            "ft_per_in": self.ft_per_in,
            "nts": self.nts,
            "region": list(self.region) if self.region else None,
            "source": self.source,
            "anchor": list(self.anchor) if self.anchor else None,
            "example": self.example,
            "skipped": self.skipped,
        }


def _region_kind_at(regions: Iterable[Region], x: float, y: float) -> str | None:
    for r in regions:
        if r.kind in NON_PLAN_REGION_KINDS and r.contains(x, y):
            return r.kind
    return None


def _plan_area(regions: Iterable[Region]) -> Bbox:
    for r in regions:
        if r.kind == "plan_area":
            return list(r.bbox)
    return [0.0, 0.0, 1.0, 1.0]


def _frame_for(anchor: Bbox, frames: list[Bbox], w_in: float, h_in: float) -> Bbox | None:
    """The smallest frame containing the anchor, else the smallest whose
    bottom edge is within `TITLE_BELOW_REACH_IN` above the anchor and whose
    x-span covers it."""
    cx, cy = (anchor[0] + anchor[2]) / 2, (anchor[1] + anchor[3]) / 2
    inside = [f for f in frames if f[0] <= cx <= f[2] and f[1] <= cy <= f[3]]
    if inside:
        return min(inside, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))
    reach = TITLE_BELOW_REACH_IN / (h_in or 1.0)
    below = [f for f in frames if f[0] <= cx <= f[2] and 0 <= anchor[1] - f[3] <= reach]
    if below:
        return min(below, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))
    return None


def detect_viewports(
    facts: PageFacts,
    segments: SegmentIndex | None = None,
    *,
    frames: list[Bbox] | None = None,
) -> list[Viewport]:
    """Viewports of one page from WS1 views plus frame rectangles (from
    `segments`, or `frames` given directly). Legend and symbol block views
    and WS1 examples are kept in the list but marked `skipped` with no
    region, so callers can log why a scale was ignored."""
    regions = list(facts.regions)
    w_in, h_in = facts.w_in or 42.0, facts.h_in or 30.0
    if frames is None and segments is not None:
        frames = segments.rectangles(min_w_in=FRAME_MIN_IN, min_h_in=FRAME_MIN_IN)
    frames = list(frames or [])
    plan = _plan_area(regions)
    # Frames that are mostly inside a non-plan region are not viewports.
    kept_frames: list[Bbox] = []
    for f in frames:
        cx, cy = (f[0] + f[2]) / 2, (f[1] + f[3]) / 2
        if _region_kind_at(regions, cx, cy) is not None:
            continue
        if (f[2] - f[0]) * (f[3] - f[1]) >= SHEET_BORDER_COVER:
            continue  # the sheet border, not a viewport
        kept_frames.append(f)
    out: list[Viewport] = []
    for v in facts.views:
        vp = Viewport(
            view_id=v.view_id,
            title=v.title,
            kind=v.kind,
            scale_text=v.scale_text,
            ft_per_in=v.ft_per_in,
            nts=bool(v.nts),
            region=None,
            source="none",
            anchor=list(v.anchor) if v.anchor else None,
            example=bool(v.example),
        )
        if v.example:
            vp.skipped = "legend_example"
        elif v.anchor is not None:
            kind = _region_kind_at(regions, (v.anchor[0] + v.anchor[2]) / 2, (v.anchor[1] + v.anchor[3]) / 2)
            if kind in SKIP_VIEW_REGION_KINDS:
                vp.skipped = f"in_{kind}"
        if vp.skipped is None and v.anchor is not None and kept_frames:
            frame = _frame_for(list(v.anchor), kept_frames, w_in, h_in)
            if frame is not None:
                vp.region = list(frame)
                vp.source = "frame"
        out.append(vp)
    usable = [vp for vp in out if vp.skipped is None and (vp.ft_per_in is not None or vp.nts)]
    unframed = [vp for vp in usable if vp.region is None]
    plan_views = [vp for vp in usable if vp.kind in ("plan", "enlarged_plan")]
    if len(unframed) == 1 and unframed[0].ft_per_in is not None:
        only = unframed[0]
        framed_scales = {vp.ft_per_in for vp in usable if vp.region is not None}
        if only in plan_views or not framed_scales or framed_scales == {only.ft_per_in}:
            only.region = list(plan)
            only.source = "plan_area"
    elif not kept_frames and len(plan_views) == 1:
        scales = {vp.ft_per_in for vp in usable if vp.ft_per_in is not None}
        if len(scales) == 1:
            plan_views[0].region = list(plan)
            plan_views[0].source = "plan_area"
    return out


@dataclass(slots=True)
class PageScales:
    page: int
    sheet_number: str | None
    primary_ft_per_in: float | None
    nts: bool
    conflict: bool
    viewports: list[Viewport]
    reduced_print: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def scale_at(self, x: float, y: float) -> tuple[float | None, Viewport | None]:
        """(ft_per_in, viewport) at a page-fraction point: the smallest
        framed viewport containing it, else the primary scale."""
        hits = [vp for vp in self.viewports if vp.skipped is None and vp.region and vp.contains(x, y)]
        if hits:
            best = min(hits, key=lambda vp: (vp.region[2] - vp.region[0]) * (vp.region[3] - vp.region[1]))  # type: ignore[index]
            return best.ft_per_in, best
        return (None if self.conflict else self.primary_ft_per_in), None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "sheet_number": self.sheet_number,
            "primary_ft_per_in": self.primary_ft_per_in,
            "nts": self.nts,
            "conflict": self.conflict,
            "viewports": [vp.to_dict() for vp in self.viewports],
            "reduced_print": dict(self.reduced_print),
            "notes": list(self.notes),
        }


def reduced_print_check(facts: PageFacts, words: PageWords | None = None) -> dict[str, Any]:
    """Suspect a reduced print when the page is a half-size standard sheet
    or carries a reduced-print cue. Reports only; the scale is never
    changed (consumers warn or ask)."""
    w, h = facts.w_in, facts.h_in
    lo, hi = sorted((w, h))
    reasons: list[str] = []
    nearest: str | None = None
    for name, sw, sh in STANDARD_SHEETS_IN:
        if abs(lo - sw / 2) <= HALF_SIZE_TOL * sw and abs(hi - sh / 2) <= HALF_SIZE_TOL * sh:
            nearest = name
            reasons.append(f"page {w:g} x {h:g} in is half of {name} ({sw:g} x {sh:g} in)")
            break
    if words is not None:
        for ln in words.lines:
            if REDUCED_CUE_RE.search(ln.t):
                reasons.append(f"text cue: {_squash(ln.t)[:80]}")
                break
    small = lo < SMALL_SHEET_IN[1] and hi < SMALL_SHEET_IN[0]
    if small and facts.primary_ft_per_in is not None and not reasons:
        reasons.append(f"small sheet {w:g} x {h:g} in with an architectural scale")
    return {
        "suspected": bool(reasons),
        "reasons": reasons,
        "page_size_in": [round(w, 2), round(h, 2)],
        "half_of": nearest,
        "auto_scaled": False,
    }


def scales_for_page(
    facts: PageFacts,
    segments: SegmentIndex | None = None,
    *,
    words: PageWords | None = None,
    frames: list[Bbox] | None = None,
) -> PageScales:
    """Per-view scales of one page: viewports, the primary scale, a conflict
    flag when usable views disagree and no frames separate them, NTS pages
    (`nts` True, no lengths) and the reduced-print check."""
    viewports = detect_viewports(facts, segments, frames=frames)
    usable = [vp for vp in viewports if vp.skipped is None]
    scaled = [vp for vp in usable if vp.ft_per_in is not None]
    nts_only = bool(usable) and not scaled and any(vp.nts for vp in usable)
    notes: list[str] = []
    distinct = sorted({vp.ft_per_in for vp in scaled if vp.ft_per_in is not None})
    conflict = False
    primary: float | None = None
    if len(distinct) == 1:
        primary = distinct[0]
    elif len(distinct) > 1:
        framed = [vp for vp in scaled if vp.region is not None and vp.source == "frame"]
        unframed = [vp for vp in scaled if vp.region is None]
        plan_views = [vp for vp in scaled if vp.kind in ("plan", "enlarged_plan")]
        conflict = bool(unframed) or not framed
        if plan_views and len({vp.ft_per_in for vp in plan_views}) == 1:
            primary = plan_views[0].ft_per_in
        elif not conflict:
            pool = [vp for vp in framed if vp.kind in ("plan", "enlarged_plan")] or framed
            largest = max(pool, key=lambda vp: (vp.region[2] - vp.region[0]) * (vp.region[3] - vp.region[1]))  # type: ignore[index]
            primary = largest.ft_per_in
        else:
            primary = facts.primary_ft_per_in
        if conflict:
            notes.append(
                "scale_conflict: "
                + ", ".join(f"{vp.scale_text} ({vp.title or vp.kind})" for vp in scaled)
                + "; measure only inside a framed viewport"
            )
    if primary is None and facts.primary_ft_per_in is not None and not nts_only:
        primary = facts.primary_ft_per_in
    for vp in viewports:
        if vp.skipped:
            notes.append(f"{vp.scale_text} skipped ({vp.skipped})")
    return PageScales(
        page=facts.page,
        sheet_number=facts.sheet_number,
        primary_ft_per_in=primary,
        nts=nts_only or (primary is None and any(vp.nts for vp in usable)),
        conflict=conflict,
        viewports=viewports,
        reduced_print=reduced_print_check(facts, words),
        notes=notes,
    )


# Registration ---------------------------------------------------------------------------

MARGIN_IN = 0.75
IDENTICAL_IN = 0.05
IDENTITY_MIN_TOKENS = 3
IDENTITY_MIN_SHARE = 0.5
MEDIAN_MIN_PAIRS = 3
MEDIAN_GOOD_RESIDUAL_IN = 0.1
MEDIAN_OK_RESIDUAL_IN = 0.25
MEDIAN_MAX_RESIDUAL_IN = 1.0
MEDIAN_MAX_PAIRS = 200
LABEL_GAP_LINES = 2.2
LABEL_MIN_REACH_IN = 0.35
LABEL_STACK_DX_FACTOR = 8.0
LABEL_INLINE_DX_IN = 3.0
SINGLE_ANCHOR_CONFIDENCE = 0.5
# Spec 6 WS3: single anchor is allowed at 0.5; a passing callout check confirms
# it without raising it, a failed one (0.3, callout_mismatch) never drives routes.
CALLOUT_CONFIDENCE = SINGLE_ANCHOR_CONFIDENCE
CALLOUT_MISMATCH_CONFIDENCE = 0.3
CALLOUT_MIN_OVERLAP = 0.5
MIN_TOKEN_CHARS = 2
NUMBER_TOKEN_RE = re.compile(r"^[\d.,#\-/'\"()]+$")
DATE_TOKEN_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{4}-\d{2}-\d{2}$")
TIME_TOKEN_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$|^[AP]\.?M\.?$", re.I)
ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)


@dataclass(slots=True)
class Transform:
    """dst_in = k * M(rot, mirror) * src_in + (tx, ty). `rot` turns the
    source clockwise in the displayed frame (y down); `mirror` flips x first."""

    k: float = 1.0
    rot: int = 0
    mirror: bool = False
    tx: float = 0.0
    ty: float = 0.0

    def _m(self, x: float, y: float) -> tuple[float, float]:
        if self.mirror:
            x = -x
        if self.rot == 90:
            return -y, x
        if self.rot == 180:
            return -x, -y
        if self.rot == 270:
            return y, -x
        return x, y

    def apply(self, x_in: float, y_in: float) -> tuple[float, float]:
        mx, my = self._m(x_in, y_in)
        return self.k * mx + self.tx, self.k * my + self.ty

    def apply_box(self, box: Sequence[float]) -> list[float]:
        pts = [self.apply(box[0], box[1]), self.apply(box[2], box[1]), self.apply(box[2], box[3]), self.apply(box[0], box[3])]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return [min(xs), min(ys), max(xs), max(ys)]

    def is_identity(self, tol: float = 1e-6) -> bool:
        return (
            abs(self.k - 1.0) <= tol
            and self.rot == 0
            and not self.mirror
            and abs(self.tx) <= tol
            and abs(self.ty) <= tol
        )

    def compose(self, first: Transform) -> Transform:
        """The transform that applies `first` and then `self`
        (src -> mid -> dst). Rotations and mirrors are combined in the
        displayed frame; the result is exact for right-angle transforms."""
        rot = (self.rot + (-first.rot if self.mirror else first.rot)) % 360
        mirror = self.mirror != first.mirror
        k = self.k * first.k
        # Translation: self(first(0)) = k_self * M_self(first.t) + self.t
        mx, my = self._m(first.tx, first.ty)
        return Transform(k=k, rot=rot, mirror=mirror, tx=self.k * mx + self.tx, ty=self.k * my + self.ty)

    def to_dict(self) -> dict[str, Any]:
        return {
            "k": round(self.k, 6),
            "rot": self.rot,
            "mirror": self.mirror,
            "tx_in": round(self.tx, 4),
            "ty_in": round(self.ty, 4),
        }


@dataclass(slots=True)
class Token:
    text: str
    x_in: float
    y_in: float
    h_pt: float = 0.0


@dataclass(slots=True)
class SheetTokens:
    """Registration tokens of one sheet, in sheet inches."""

    sheet_key: str
    w_in: float
    h_in: float
    ft_per_in: float | None
    tokens: list[Token] = field(default_factory=list)
    content_bbox_in: list[float] | None = None

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for t in self.tokens:
            c[t.text] += 1
        return dict(c)

    def unique(self) -> dict[str, Token]:
        c = self.counts()
        return {t.text: t for t in self.tokens if c[t.text] == 1}


def page_scale(facts: PageFacts) -> float | None:
    """The page's one scale: `primary_ft_per_in`, else the scale every
    usable (non-example) scaled view agrees on; None on disagreement."""
    if facts.primary_ft_per_in is not None:
        return facts.primary_ft_per_in
    scales = {v.ft_per_in for v in facts.views if not v.example and v.ft_per_in is not None}
    return scales.pop() if len(scales) == 1 else None


def _norm_token(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().upper())


def sheet_tokens(
    facts: PageFacts,
    words: PageWords | Sequence[Word],
    *,
    sheet_key: str | None = None,
    ft_per_in: float | None = None,
    margin_in: float = MARGIN_IN,
) -> SheetTokens:
    """Plan-area words as registration tokens. Dropped: words in non-plan
    regions, words on the view-title or scale lines, words inside the page
    margin (plot stamps), rotated words, bare numbers, dates and times."""
    wlist: list[Word] = list(words.words) if isinstance(words, PageWords) else list(words)
    regions = list(facts.regions)
    w_in, h_in = facts.w_in or 42.0, facts.h_in or 30.0
    skip_text: set[str] = set()
    for v in facts.views:
        if v.title:
            skip_text.update(_norm_token(p) for p in v.title.split())
        if v.scale_text:
            skip_text.update(_norm_token(p) for p in v.scale_text.split())
    out: list[Token] = []
    xs: list[float] = []
    ys: list[float] = []
    for w in wlist:
        if not in_plan_area(regions, w.cx, w.cy):
            continue
        x_in, y_in = w.cx * w_in, w.cy * h_in
        xs.append(x_in)
        ys.append(y_in)
        if w.r not in (0,):
            continue
        if x_in < margin_in or y_in < margin_in or x_in > w_in - margin_in or y_in > h_in - margin_in:
            continue
        t = _norm_token(w.t)
        if len(t) < MIN_TOKEN_CHARS or t in skip_text:
            continue
        if NUMBER_TOKEN_RE.match(t) or DATE_TOKEN_RE.match(t) or TIME_TOKEN_RE.match(t):
            continue
        out.append(Token(text=t, x_in=round(x_in, 4), y_in=round(y_in, 4), h_pt=float(w.h)))
    bbox = [min(xs), min(ys), max(xs), max(ys)] if xs else None
    return SheetTokens(
        sheet_key=sheet_key or f"p{facts.page}",
        w_in=w_in,
        h_in=h_in,
        ft_per_in=ft_per_in if ft_per_in is not None else page_scale(facts),
        tokens=out,
        content_bbox_in=bbox,
    )


@dataclass(slots=True)
class Registration:
    src: str
    dst: str
    transform: Transform | None
    method: str  # identity_frame | median | single_anchor | chain | none
    confidence: float
    residual_in: float | None = None
    n_tokens: int = 0
    n_identical: int = 0
    n_common: int = 0
    anchor: str | None = None
    callout: dict[str, Any] | None = None
    flags: list[str] = field(default_factory=list)
    chain: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.transform is not None and self.method != "none"

    def apply(self, x_in: float, y_in: float) -> tuple[float, float] | None:
        if self.transform is None:
            return None
        return self.transform.apply(x_in, y_in)

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "transform": self.transform.to_dict() if self.transform else None,
            "method": self.method,
            "confidence": round(self.confidence, 3),
            "residual_in": None if self.residual_in is None else round(self.residual_in, 4),
            "n_tokens": self.n_tokens,
            "n_identical": self.n_identical,
            "n_common": self.n_common,
            "anchor": self.anchor,
            "callout": self.callout,
            "flags": list(self.flags),
            "chain": list(self.chain),
        }


def identity_frame(src: SheetTokens, dst: SheetTokens) -> tuple[bool, int, int]:
    """(is_identity, identical, common): tokens of the smaller sheet whose
    text occurs on the other sheet (common) and, of those, the ones that
    also sit at the same inch position within `IDENTICAL_IN` (identical)."""
    small, large = (src, dst) if len(src.tokens) <= len(dst.tokens) else (dst, src)
    by_text: dict[str, list[Token]] = defaultdict(list)
    for t in large.tokens:
        by_text[t.text].append(t)
    common = 0
    identical = 0
    for t in small.tokens:
        cands = by_text.get(t.text)
        if not cands:
            continue
        common += 1
        if any(math.hypot(c.x_in - t.x_in, c.y_in - t.y_in) <= IDENTICAL_IN for c in cands):
            identical += 1
    ok = identical >= IDENTITY_MIN_TOKENS and identical >= IDENTITY_MIN_SHARE * common
    return ok, identical, common


def _shared_unique(src: SheetTokens, dst: SheetTokens) -> list[tuple[Token, Token]]:
    us, ud = src.unique(), dst.unique()
    return [(us[t], ud[t]) for t in sorted(set(us) & set(ud))]


def _median_candidate(
    pairs: list[tuple[Token, Token]], *, k_fixed: float | None, rot: int, mirror: bool
) -> tuple[Transform, float] | None:
    probe = Transform(k=1.0, rot=rot, mirror=mirror)
    src_pts = [probe._m(a.x_in, a.y_in) for a, _b in pairs]
    dst_pts = [(b.x_in, b.y_in) for _a, b in pairs]
    if k_fixed is not None:
        k = k_fixed
    else:
        ratios: list[float] = []
        n = len(pairs)
        for i in range(n):
            for j in range(i + 1, n):
                ds = math.dist(src_pts[i], src_pts[j])
                dd = math.dist(dst_pts[i], dst_pts[j])
                if ds > 0.5 and dd > 0.05:
                    ratios.append(dd / ds)
                if len(ratios) >= MEDIAN_MAX_PAIRS:
                    break
            if len(ratios) >= MEDIAN_MAX_PAIRS:
                break
        if not ratios:
            return None
        k = float(median(ratios))
    tx = float(median(d[0] - k * s[0] for s, d in zip(src_pts, dst_pts, strict=True)))
    ty = float(median(d[1] - k * s[1] for s, d in zip(src_pts, dst_pts, strict=True)))
    res = float(
        median(
            math.hypot(d[0] - (k * s[0] + tx), d[1] - (k * s[1] + ty))
            for s, d in zip(src_pts, dst_pts, strict=True)
        )
    )
    return Transform(k=k, rot=rot, mirror=mirror, tx=tx, ty=ty), res


def _labels(tokens: SheetTokens) -> dict[str, tuple[float, float]]:
    """Unique multi-word labels: pairs of unique alphabetic tokens that sit
    stacked (within `LABEL_GAP_LINES` glyph heights or `LABEL_MIN_REACH_IN`
    vertically, left edges within `LABEL_STACK_DX_FACTOR` heights) or in
    line (same baseline, within `LABEL_INLINE_DX_IN`). Key = the two texts
    in reading order; value = the label center in inches."""
    uniq = [t for t in tokens.unique().values() if re.search(r"[A-Z]{2,}", t.text)]
    uniq.sort(key=lambda t: (t.y_in, t.x_in))
    out: dict[str, tuple[float, float]] = {}
    for i, a in enumerate(uniq):
        ha = max(a.h_pt, 4.0) / 72.0
        for b in uniq[i + 1 :]:
            hb = max(b.h_pt, 4.0) / 72.0
            h = max(ha, hb)
            reach_y = max(LABEL_GAP_LINES * h, LABEL_MIN_REACH_IN)
            if b.y_in - a.y_in > reach_y:
                break
            dx, dy = abs(b.x_in - a.x_in), abs(b.y_in - a.y_in)
            stacked = dy <= reach_y and dx <= LABEL_STACK_DX_FACTOR * h
            inline = dy <= 0.5 * h and dx <= LABEL_INLINE_DX_IN
            if stacked or inline:
                out[f"{a.text} {b.text}"] = ((a.x_in + b.x_in) / 2, (a.y_in + b.y_in) / 2)
    return out


def _callout_check(
    src: SheetTokens, transform: Transform, rectangles: list[Bbox] | None, dst: SheetTokens
) -> tuple[dict[str, Any] | None, float, list[str]]:
    """Map the source content bbox into the destination and compare with
    its callout rectangles (page fractions)."""
    if rectangles is None:
        return None, SINGLE_ANCHOR_CONFIDENCE, ["callout_unchecked"]
    if src.content_bbox_in is None:
        return None, SINGLE_ANCHOR_CONFIDENCE, ["callout_unchecked"]
    mapped = transform.apply_box(src.content_bbox_in)
    best: dict[str, Any] | None = None
    for r in rectangles:
        rb = [r[0] * dst.w_in, r[1] * dst.h_in, r[2] * dst.w_in, r[3] * dst.h_in]
        ix0, iy0 = max(mapped[0], rb[0]), max(mapped[1], rb[1])
        ix1, iy1 = min(mapped[2], rb[2]), min(mapped[3], rb[3])
        if ix1 <= ix0 or iy1 <= iy0:
            continue
        inter = (ix1 - ix0) * (iy1 - iy0)
        area = max((mapped[2] - mapped[0]) * (mapped[3] - mapped[1]), 1e-6)
        overlap = inter / area
        cx, cy = (mapped[0] + mapped[2]) / 2, (mapped[1] + mapped[3]) / 2
        center_inside = rb[0] <= cx <= rb[2] and rb[1] <= cy <= rb[3]
        cand = {
            "rect": [round(v, 4) for v in r],
            "rect_in": [round(v, 2) for v in rb],
            "mapped_in": [round(v, 2) for v in mapped],
            "overlap": round(overlap, 3),
            "center_inside": center_inside,
        }
        if best is None or overlap > best["overlap"]:
            best = cand
    if best is not None and best["overlap"] >= CALLOUT_MIN_OVERLAP and best["center_inside"]:
        return best, CALLOUT_CONFIDENCE, ["callout_checked"]
    return best, CALLOUT_MISMATCH_CONFIDENCE, ["callout_mismatch"]


def register_sheets(
    src: SheetTokens,
    dst: SheetTokens,
    *,
    dst_rectangles: list[Bbox] | None = None,
    allow_single_anchor: bool = True,
) -> Registration:
    """Map `src` inches onto `dst` inches: identity frame, else the median
    solve over 4 rotations and a mirror check, else a single-anchor
    registration from one unique multi-word label."""
    k_fixed: float | None = None
    if src.ft_per_in and dst.ft_per_in:
        k_fixed = src.ft_per_in / dst.ft_per_in
    same_scale = k_fixed is None or abs(k_fixed - 1.0) < 1e-6
    if same_scale:
        ok, identical, common = identity_frame(src, dst)
        if ok:
            return Registration(
                src=src.sheet_key,
                dst=dst.sheet_key,
                transform=Transform(),
                method="identity_frame",
                confidence=1.0,
                residual_in=0.0,
                n_tokens=identical,
                n_identical=identical,
                n_common=common,
            )
    else:
        identical, common = 0, 0
    pairs = _shared_unique(src, dst)
    if len(pairs) >= MEDIAN_MIN_PAIRS:
        best: tuple[Transform, float] | None = None
        for mirror in (False, True):
            for rot in ROTATIONS:
                cand = _median_candidate(pairs, k_fixed=k_fixed, rot=rot, mirror=mirror)
                if cand is None:
                    continue
                if best is None or cand[1] < best[1]:
                    best = cand
        if best is not None and best[1] <= MEDIAN_MAX_RESIDUAL_IN:
            transform, res = best
            flags: list[str] = []
            if res <= MEDIAN_GOOD_RESIDUAL_IN and len(pairs) >= 5:
                conf = 0.9
            elif res <= MEDIAN_OK_RESIDUAL_IN:
                conf = 0.75
            else:
                conf = 0.5
                flags.append("high_residual")
            if transform.mirror:
                flags.append("mirrored")
            if k_fixed is None:
                flags.append("k_estimated")
            return Registration(
                src=src.sheet_key,
                dst=dst.sheet_key,
                transform=transform,
                method="median",
                confidence=conf,
                residual_in=res,
                n_tokens=len(pairs),
                n_identical=identical,
                n_common=common,
                flags=flags,
            )
    if allow_single_anchor and k_fixed is not None:
        ls, ld = _labels(src), _labels(dst)
        shared = sorted(set(ls) & set(ld))
        if shared:
            txs: list[float] = []
            tys: list[float] = []
            for key in shared:
                sx, sy = ls[key]
                dx, dy = ld[key]
                txs.append(dx - k_fixed * sx)
                tys.append(dy - k_fixed * sy)
            transform = Transform(k=k_fixed, rot=0, mirror=False, tx=float(median(txs)), ty=float(median(tys)))
            spread = max(math.hypot(a - transform.tx, b - transform.ty) for a, b in zip(txs, tys, strict=True))
            callout, conf, flags = _callout_check(src, transform, dst_rectangles, dst)
            if len(shared) > 1:
                flags.append(f"anchors:{len(shared)}")
            if spread > MEDIAN_OK_RESIDUAL_IN:
                flags.append("anchors_disagree")
                conf = min(conf, CALLOUT_MISMATCH_CONFIDENCE)
            return Registration(
                src=src.sheet_key,
                dst=dst.sheet_key,
                transform=transform,
                method="single_anchor",
                confidence=conf,
                residual_in=spread if len(shared) > 1 else None,
                n_tokens=len(shared),
                n_identical=identical,
                n_common=common,
                anchor=shared[0],
                callout=callout,
                flags=flags,
            )
    flags = []
    if k_fixed is None:
        flags.append("scale_unknown")
    if len(pairs) < MEDIAN_MIN_PAIRS:
        flags.append(f"shared_tokens:{len(pairs)}")
    return Registration(
        src=src.sheet_key,
        dst=dst.sheet_key,
        transform=None,
        method="none",
        confidence=0.0,
        n_tokens=len(pairs),
        n_identical=identical,
        n_common=common,
        flags=flags,
    )


def _inverse(t: Transform) -> Transform:
    """The inverse of a right-angle similarity (exact)."""
    k = 1.0 / t.k
    if t.mirror:
        rot = t.rot  # mirror then rotate: the inverse rotates back then mirrors; same angle
    else:
        rot = (-t.rot) % 360
    inv = Transform(k=k, rot=rot, mirror=t.mirror)
    # Solve inv(t.t) = 0: tx', ty' = -k * M_inv(t.tx, t.ty)
    mx, my = inv._m(t.tx, t.ty)
    inv.tx, inv.ty = -k * mx, -k * my
    return inv


def chain_registrations(
    registrations: Iterable[Registration],
    src: str,
    dst: str,
    *,
    min_confidence: float = 0.0,
) -> Registration | None:
    """Compose registrations along the shortest path from `src` to `dst`
    (registrations are usable in both directions). Confidence is the
    minimum along the path; the method is `chain` unless the path is a
    single registration. Returns None without a path."""
    edges: dict[str, list[tuple[str, Registration, bool]]] = defaultdict(list)
    for r in registrations:
        if not r.ok or r.confidence < min_confidence:
            continue
        edges[r.src].append((r.dst, r, False))
        edges[r.dst].append((r.src, r, True))
    if src == dst:
        return Registration(src=src, dst=dst, transform=Transform(), method="identity_frame", confidence=1.0)
    prev: dict[str, tuple[str, Registration, bool]] = {}
    queue: deque[str] = deque([src])
    seen = {src}
    while queue:
        node = queue.popleft()
        if node == dst:
            break
        for nxt, reg, reverse in edges.get(node, ()):
            if nxt in seen:
                continue
            seen.add(nxt)
            prev[nxt] = (node, reg, reverse)
            queue.append(nxt)
    if dst not in prev:
        return None
    path: list[tuple[Registration, bool]] = []
    node = dst
    while node != src:
        p, reg, reverse = prev[node]
        path.append((reg, reverse))
        node = p
    path.reverse()
    transform = Transform()
    conf = 1.0
    names: list[str] = [src]
    flags: list[str] = []
    for reg, reverse in path:
        assert reg.transform is not None
        step = _inverse(reg.transform) if reverse else reg.transform
        transform = step.compose(transform)
        conf = min(conf, reg.confidence)
        names.append(reg.src if reverse else reg.dst)
        flags.extend(f for f in reg.flags if f not in flags)
    if len(path) == 1:
        only = path[0][0]
        return Registration(
            src=src,
            dst=dst,
            transform=transform,
            method=only.method,
            confidence=only.confidence,
            residual_in=only.residual_in,
            n_tokens=only.n_tokens,
            n_identical=only.n_identical,
            n_common=only.n_common,
            anchor=only.anchor,
            callout=only.callout,
            flags=list(only.flags),
            chain=names,
        )
    return Registration(
        src=src,
        dst=dst,
        transform=transform,
        method="chain",
        confidence=conf,
        n_tokens=min(r.n_tokens for r, _rev in path),
        flags=flags,
        chain=names,
    )


def view_scale_for_point(
    facts: PageFacts, x: float, y: float, *, views: Sequence[View] | None = None
) -> float | None:
    """The WS1 view scale at a page-fraction point (framed views first),
    else the page primary scale; None on a conflict."""
    vs = list(views) if views is not None else list(facts.views)
    hits = [
        v
        for v in vs
        if not v.example
        and v.ft_per_in is not None
        and v.region
        and v.region[0] <= x <= v.region[2]
        and v.region[1] <= y <= v.region[3]
    ]
    if hits:
        best = min(hits, key=lambda v: (v.region[2] - v.region[0]) * (v.region[3] - v.region[1]))  # type: ignore[index]
        return best.ft_per_in
    if facts.scale_conflict:
        return None
    return facts.primary_ft_per_in
