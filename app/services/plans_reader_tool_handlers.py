"""WS2-owned tools of the AI Estimating tool loop: `zoom_region`, the
`DocumentAccess` service (page sizes, PDF bytes and renders through
plans_reader_images under the PDFium lock) and the queue `heartbeat`
(docs/AI_ESTIMATING_BUILD_SPEC.md 5.2 tool table, 5.3, section 6 WS2
step 9).

zoom_region (the design paragraph of section 6 WS2):

- Targets come from `anchor` (a `t:` ref or a text with an occurrence),
  `expect_text` (every occurrence) or `census_ref` (the occurrences of a
  census the run already has). Text targets are matched on the page's
  text runs (WS1 TextIndex); for purpose `read_text` a target widens to
  its whole text line (the schedule row, E19).
- Containment check: when the model gave a `bbox`, the targets must sit
  inside it. When they do not, the view is retargeted: to the union of
  the bbox and the targets when that union is at most 3x the bbox area,
  else to the targets themselves, clustered when they are far apart (at
  most 4 regions), padded by purpose. Coverage (targets, inside, complete)
  is stamped on the result so the merge can never take an incomplete view
  for a sheet total (E11).
- ppi by purpose (read_text 300, count 200, verify_symbol 300, measure
  150; an explicit `ppi` wins), stepping down the ladder until the views
  fit in at most 4 tiles under the route's ProviderImageSpec
  (`ToolContext.image_spec`: OpenAI 30,000 patches per tile, Anthropic a
  long edge of at most 2,576 px and at most 3.75 MP per tile; the OpenAI
  spec when none is set); when even the lowest rung does not fit, a $0
  error asks for a narrower region. Nothing is rendered before the plan
  fits, and every delivered tile passes the spec's `fits`.
- `text_lines`: the text runs inside the delivered views, grouped into
  lines with their refs, so a read_text zoom cites what it read.
- On a page with a usable text layer, `read_text` and `count` without a
  target return candidates (the text runs the model may anchor on) at $0
  and no image (spec 4.3 rule 2).

Coordinates are page fractions of the displayed page (spec 5.1), the frame
of `plans_reader_images.render_page_png`, so a rotated sheet needs no
transform here. No model, no prices, no database in the handler itself
(DocumentAccess reads storage through the service seam).
"""

from __future__ import annotations

import io
import logging
import math
import re
import threading
from dataclasses import dataclass
from typing import Any

from app.services import llm, llm_parts
from app.services import plans_reader_images as images
from app.services.pagefacts import tags as tags_mod
from app.services.pagefacts.contracts import (
    TEXT_LAYER_QUALITY_FULL,
    FactsUnavailable,
    Word,
    text_layer_quality_for,
)
from app.services.pagefacts.textlayer import build_lines
from app.services.plans_reader_tools import (
    ToolContext,
    ToolResult,
    ToolServices,
    ToolSpec,
    doc8,
    is_unavailable,
    kinds_from_codes,
    parse_ref,
    register,
    services,
    unavailable_result,
)

logger = logging.getLogger(__name__)

PURPOSES: tuple[str, ...] = ("read_text", "count", "verify_symbol", "measure")
PAD_IN: dict[str, float] = {"read_text": 0.75, "count": 1.5, "verify_symbol": 0.5, "measure": 2.0}
PPI_BY_PURPOSE: dict[str, int] = {"read_text": 300, "count": 200, "verify_symbol": 300, "measure": 150}
CLUSTER_EPS_IN: dict[str, float] = {"read_text": 2.0, "count": 6.0, "verify_symbol": 3.0, "measure": 8.0}
PPI_LADDER: tuple[int, ...] = (400, 300, 260, 220, 200, 180, 150, 120, 100, 80, 60)
PPI_MIN, PPI_MAX = 40, 400
MAX_TILES = 4
MAX_REGIONS = 4
UNION_AREA_FACTOR = 3.0
MAX_CANDIDATES = 40
MAX_TEXT_LINES = 80
MAX_TARGETS_LISTED = 60
ROW_GAP_IN = 3.0  # read_text widens a target to its table row: cells closer than this chain
TARGET_REQUIRED_PURPOSES: frozenset[str] = frozenset({"read_text", "count"})
_TAG_LIKE_RE = re.compile(r"^[A-Z0-9(][A-Z0-9()/\-.]{0,9}$")

Bbox = list[float]


ZOOM_ARGS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document_id", "page", "bbox", "anchor", "expect_text", "census_ref", "purpose", "ppi"],
    "properties": {
        "document_id": {"type": "string", "description": "The document uuid from the sheet index."},
        "page": {"type": "integer", "description": "1-based page of the document."},
        "bbox": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
            "description": "[x0, y0, x1, y1] page fractions, origin top-left; null to let the targets decide.",
        },
        "anchor": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["ref", "text", "occurrence"],
            "properties": {
                "ref": {"type": ["string", "null"], "description": "A t: text ref from text_search."},
                "text": {"type": ["string", "null"], "description": "Exact text on the page (a tag, a row key)."},
                "occurrence": {"type": ["integer", "null"], "description": "1-based occurrence of the text; null for all."},
            },
            "description": "One text the view must contain.",
        },
        "expect_text": {"type": ["string", "null"], "description": "Text every occurrence of which the view must contain."},
        "census_ref": {"type": ["string", "null"], "description": "A c: census ref (or c:id:k for one occurrence) the view must cover."},
        "purpose": {"type": "string", "enum": list(PURPOSES)},
        "ppi": {"type": ["integer", "null"], "description": "Render resolution; null picks it by purpose."},
    },
}

ZOOM_DESCRIPTION = (
    "Render one region of a drawing page at high resolution and return the crop(s) with the text "
    "lines inside them. Give a target (anchor text or ref, expect_text, or a census ref): the region "
    "is checked to contain it and widened or moved when it does not, and coverage says how many "
    "targets the views hold. read_text and count need a target on a page with a text layer; without "
    "one you get candidate texts and no image."
)

ZOOM_RESULT_DOC: dict[str, Any] = {
    "views": "[{view_id, ref v:, bbox, w_px, h_px, tile_of}] delivered crops (images attached)",
    "bbox_used": "[[x0,y0,x1,y1], ...] the regions rendered",
    "ppi": "render resolution used",
    "retargeted": "bool",
    "retarget_reason": "union | targets | none",
    "coverage": "{targets, inside, complete}",
    "text_lines": "[{text, bbox, refs}] text runs inside the views",
    "candidates": "[{ref, text, bbox}] when a target was required and missing",
    "warnings": "[str]",
    "text_layer_quality": "full | sparse | none | vector_text | unknown",
}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _clamp_box(box: Any) -> Bbox:
    x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in box)
    return [round(min(x0, x1), 4), round(min(y0, y1), 4), round(max(x0, x1), 4), round(max(y0, y1), 4)]


def _area(box: Bbox) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _center(box: Bbox) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _contains(box: Bbox, x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _union(boxes: list[Bbox]) -> Bbox:
    return _clamp_box(
        [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    )


def _pad(box: Bbox, pad_in: float, w_in: float, h_in: float) -> Bbox:
    px, py = pad_in / (w_in or 42.0), pad_in / (h_in or 30.0)
    return _clamp_box([box[0] - px, box[1] - py, box[2] + px, box[3] + py])


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().upper()


def _fold(text: str) -> str:
    """CAD doubled text folds (L1L1 -> L1, L5/1LH -> L5) for tag matching."""
    t = _norm(text).replace("\r", "").replace("\n", "")
    m = re.match(r"^(?P<t>[A-Z]{1,3}\d{0,2}(?:-[A-Z]{1,3})?)(?P=t)?(?:/[A-Z0-9-]+)?$", t)
    return m.group("t") if m else t


def text_matches(run_text: Any, query: str) -> bool:
    """A page text run matches a query when it equals it, folds to it, or
    (for queries of three or more characters) contains it."""
    q = _norm(query)
    if not q:
        return False
    r = _norm(run_text)
    if r == q or _fold(r) == _fold(q):
        return True
    return len(q) >= 3 and q in r


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass
class Target:
    bbox: Bbox
    ref: str | None = None
    text: str | None = None
    source: str = "anchor"  # anchor | expect_text | census


def _run_bbox(run: dict[str, Any]) -> Bbox | None:
    box = run.get("bbox") or run.get("b")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        return _clamp_box(box)
    except (TypeError, ValueError):
        return None


def _runs_to_words(runs: list[dict[str, Any]], w_in: float, h_in: float) -> list[Word]:
    out: list[Word] = []
    for run in runs:
        box = _run_bbox(run)
        if box is None:
            out.append(Word(t="", b=[0.0, 0.0, 0.0, 0.0], h=0.0))
            continue
        h = run.get("h")
        if not isinstance(h, (int, float)) or h <= 0:
            h = max(0.5, (box[3] - box[1]) * (h_in or 30.0) * 72)
        out.append(Word(t=str(run.get("text") or run.get("t") or ""), b=list(box), h=float(h), r=int(run.get("r") or 0)))
    return out


class _Lines:
    """Text lines of a page's runs, built once per call (the line a run
    belongs to is the schedule row a read_text target widens to)."""

    def __init__(self, runs: list[dict[str, Any]], w_in: float, h_in: float) -> None:
        self.runs = runs
        self.words = _runs_to_words(runs, w_in, h_in)
        self.line_of: dict[int, int] = {}
        self.lines: list[tuple[Bbox, list[int]]] = []
        if not self.words:
            return
        ordered, lines = build_lines(list(self.words), w_in or 42.0, h_in or 30.0)
        # build_lines re-orders the words; map each line's words back to run indexes.
        index_of = {id(w): i for i, w in enumerate(self.words)}
        for li, ln in enumerate(lines):
            members = [index_of[id(w)] for w in ordered[ln.w[0] : ln.w[1]] if id(w) in index_of]
            if not members:
                continue
            boxes = [self.words[i].b for i in members if self.words[i].t]
            if not boxes:
                continue
            self.lines.append((_union([list(b) for b in boxes]), members))
            for i in members:
                self.line_of[i] = len(self.lines) - 1

    def line_box(self, run_index: int) -> Bbox | None:
        li = self.line_of.get(run_index)
        return self.lines[li][0] if li is not None else None

    def row_box(self, run_index: int, *, w_in: float, h_in: float, max_gap_in: float = ROW_GAP_IN) -> Bbox | None:
        """The table row a run sits in: the runs on its baseline band,
        chained left and right through gaps of at most `max_gap_in` (a
        schedule row is several text lines because its cells are far
        apart; a plan tag chains with nothing)."""
        if not 0 <= run_index < len(self.words) or not self.words[run_index].t:
            return None
        base = self.words[run_index]
        tol = 0.6 * max(base.h, 0.5) / ((h_in or 30.0) * 72)
        band = sorted(
            (i for i, w in enumerate(self.words) if w.t and w.r == base.r and abs(w.cy - base.cy) <= tol),
            key=lambda i: self.words[i].b[0],
        )
        gap = max_gap_in / (w_in or 42.0)
        pos = band.index(run_index)
        lo = pos
        while lo > 0 and self.words[band[lo]].b[0] - self.words[band[lo - 1]].b[2] <= gap:
            lo -= 1
        hi = pos
        while hi < len(band) - 1 and self.words[band[hi + 1]].b[0] - self.words[band[hi]].b[2] <= gap:
            hi += 1
        return _union([list(self.words[i].b) for i in band[lo : hi + 1]])

    def inside(self, boxes: list[Bbox], limit: int = MAX_TEXT_LINES) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for box, members in self.lines:
            cx, cy = _center(box)
            if not any(_contains(b, cx, cy) for b in boxes):
                continue
            words = [self.words[i] for i in members if self.words[i].t]
            refs = [str(self.runs[i].get("ref")) for i in members if self.runs[i].get("ref")]
            out.append({"text": " ".join(w.t for w in words), "bbox": box, "refs": refs})
            if len(out) >= limit:
                break
        return out


def _ref_index(ref: str, document_id: str, page: int) -> int | None:
    try:
        kind, parts = parse_ref(ref)
    except ValueError:
        return None
    if kind != "t" or parts[0] != doc8(document_id) or str(parts[1]) != str(page):
        return None
    return int(parts[2]) if parts[2].isdigit() else None


def _targets_from_text(
    runs: list[dict[str, Any]], query: str, occurrence: int | None, *, source: str
) -> list[Target]:
    hits = [(i, run) for i, run in enumerate(runs) if text_matches(run.get("text") or run.get("t"), query)]
    if occurrence is not None and occurrence >= 1:
        hits = hits[occurrence - 1 : occurrence]
    out: list[Target] = []
    for i, run in hits:
        box = _run_bbox(run)
        if box is None:
            continue
        out.append(Target(bbox=box, ref=run.get("ref") or f"t:{{doc8}}:{{page}}:{i}", text=str(run.get("text") or run.get("t") or ""), source=source))
    return out


def _resolve_census(ctx: ToolContext, ref: str) -> dict[str, Any] | None:
    """The census result a c: ref names: the census service's `lookup`
    when it has one (WS1), else the row store's resolver."""
    svc = ctx.svc()
    census_id = None
    try:
        kind, parts = parse_ref(ref)
        if kind == "c":
            census_id = parts[0]
    except ValueError:
        return None
    if census_id is None:
        return None
    lookup = getattr(svc.census, "lookup", None)
    if callable(lookup):
        try:
            meta = lookup(census_id)
        except Exception:  # noqa: BLE001 - fall through to the row store
            meta = None
        if isinstance(meta, dict) and not is_unavailable(meta):
            return meta
    resolver = getattr(svc.rows, "resolve", None)
    if callable(resolver):
        try:
            meta = resolver(ctx.run_id, f"c:{census_id}")
        except Exception:  # noqa: BLE001
            meta = None
        if isinstance(meta, dict) and not is_unavailable(meta):
            return meta
    return None


def _targets_from_census(ctx: ToolContext, ref: str, warnings: list[str]) -> list[Target]:
    meta = _resolve_census(ctx, ref)
    if meta is None:
        warnings.append(f"census ref {ref} is not resolvable in this run; no targets taken from it")
        return []
    try:
        _kind, parts = parse_ref(ref)
    except ValueError:
        return []
    want = parts[1] if len(parts) > 1 else None
    out: list[Target] = []
    for k, occ in enumerate(meta.get("occurrences") or []):
        if not isinstance(occ, dict):
            continue
        box = _run_bbox(occ)
        if box is None:
            continue
        occ_ref = str(occ.get("ref") or f"c:{parts[0]}:{k}")
        if want is not None and not occ_ref.endswith(f":{want}"):
            continue
        out.append(Target(bbox=box, ref=occ_ref, text=str(occ.get("text") or ""), source="census"))
    if not out:
        warnings.append(f"census {ref} carries no positioned occurrences")
    return out


# ---------------------------------------------------------------------------
# Planning the views
# ---------------------------------------------------------------------------


def _cluster_boxes(targets: list[Target], purpose: str, w_in: float, h_in: float) -> list[Bbox]:
    """Single-link clusters of the targets (WS1 `clusters`, largest first,
    at most MAX_REGIONS), each the union of its members' boxes padded by
    purpose, so a widened row keeps its whole width."""
    points = [list(_center(t.bbox)) for t in targets]
    out: list[Bbox] = []
    for group in tags_mod.clusters(points, w_in, h_in, eps_in=CLUSTER_EPS_IN[purpose])[:MAX_REGIONS]:
        out.append(_pad(_union([targets[i].bbox for i in group]), PAD_IN[purpose], w_in, h_in))
    return out


def plan_views(
    bbox: Bbox | None, targets: list[Target], purpose: str, w_in: float, h_in: float
) -> tuple[list[Bbox], bool, str, dict[str, Any]]:
    """(boxes to render, retargeted, reason, coverage). Pure."""
    coverage = {"targets": len(targets), "inside": 0, "complete": False}
    if not targets:
        boxes = [bbox] if bbox is not None else []
        return boxes, False, "none", coverage
    tboxes = [t.bbox for t in targets]
    if bbox is not None:
        inside = [t for t in targets if _contains(bbox, *_center(t.bbox))]
        if len(inside) == len(targets):
            used, retargeted, reason = [bbox], False, "none"
        else:
            union = _pad(_union([bbox, *tboxes]), PAD_IN[purpose] / 2, w_in, h_in)
            if _area(union) <= UNION_AREA_FACTOR * max(_area(bbox), 1e-9):
                used, retargeted, reason = [union], True, "union"
            else:
                used, retargeted, reason = _cluster_boxes(targets, purpose, w_in, h_in), True, "targets"
    else:
        used, retargeted, reason = _cluster_boxes(targets, purpose, w_in, h_in), False, "targets"
    covered = sum(1 for t in targets if any(_contains(b, *_center(t.bbox)) for b in used))
    coverage.update({"inside": covered, "complete": covered == len(targets)})
    return used, retargeted, reason, coverage


def _spec(image_spec: Any) -> llm_parts.ProviderImageSpec:
    """The provider image spec to tile under (OpenAI when none is set)."""
    return image_spec if isinstance(image_spec, llm_parts.ProviderImageSpec) else llm_parts.OPENAI_IMAGES


def _max_extent(total: int, parts: int, overlap: float) -> int:
    """The widest tile of a `parts` split of `total` px (an interior tile
    carries half the overlap on both sides), never more than `total`."""
    cell = math.ceil(total / parts)
    pad = math.ceil(overlap * (total / parts) / 2) if parts > 1 else 0
    return min(total, cell + 2 * pad)


def plan_tiles_for(
    w_px: int, h_px: int, image_spec: Any = None, *, overlap: float = images.DEFAULT_TILE_OVERLAP
) -> list[tuple[int, int, int, int]]:
    """Pixel boxes covering a w x h image with the fewest grid tiles whose
    every tile fits `image_spec` as is (no provider downscale, no
    rejection). The OpenAI spec keeps `plans_reader_images.plan_tiles`
    (30,000 patches); a spec with a long-edge or pixel cap (Anthropic)
    searches the grids under those caps. Ties go to the grid with fewer
    total pixels. ValueError when no grid fits."""
    spec = _spec(image_spec)
    if spec.max_long_edge_px is None and spec.max_pixels is None:
        return images.plan_tiles(w_px, h_px, max_patches=spec.max_patches or images.MAX_PATCHES_ORIGINAL, overlap=overlap)
    if w_px <= 0 or h_px <= 0:
        raise ValueError("The image has no size and cannot be tiled.")
    best: tuple[int, int, list[tuple[int, int, int, int]]] | None = None
    max_cols = math.ceil(w_px / images.PATCH_PX)
    max_rows = math.ceil(h_px / images.PATCH_PX)
    for cols in range(1, max_cols + 1):
        if best is not None and cols > best[0]:
            break
        tw = _max_extent(w_px, cols, overlap)
        if spec.max_long_edge_px is not None and tw > spec.max_long_edge_px:
            continue
        for rows in range(1, max_rows + 1):
            if best is not None and cols * rows > best[0]:
                break
            if not spec.fits(tw, _max_extent(h_px, rows, overlap)):
                continue
            boxes = images._grid_boxes(w_px, h_px, cols, rows, overlap)
            if not all(spec.fits(b[2] - b[0], b[3] - b[1]) for b in boxes):
                continue
            key = (cols * rows, sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes))
            if best is None or key < (best[0], best[1]):
                best = (key[0], key[1], boxes)
            break  # more rows for this column count only adds tiles
    if best is None:
        raise ValueError("The image cannot be tiled under the provider's image limits.")
    return best[2]


def _tiles_at(boxes: list[Bbox], ppi: int, w_in: float, h_in: float, image_spec: Any = None) -> int:
    total = 0
    for box in boxes:
        w_px = images.px_for_inches((box[2] - box[0]) * w_in, ppi)
        h_px = images.px_for_inches((box[3] - box[1]) * h_in, ppi)
        if w_px <= 0 or h_px <= 0:
            continue
        total += len(plan_tiles_for(w_px, h_px, image_spec))
    return total


def choose_ppi(
    boxes: list[Bbox], purpose: str, requested: int | None, w_in: float, h_in: float, image_spec: Any = None
) -> int | None:
    """The ppi to render at: the requested one (clamped) or the purpose's,
    stepped down the ladder until the boxes fit in MAX_TILES tiles under
    the provider's image spec; None when even the lowest rung does not
    fit."""
    start = int(requested) if requested else PPI_BY_PURPOSE[purpose]
    start = max(PPI_MIN, min(PPI_MAX, start))
    ladder = [start] + [p for p in PPI_LADDER if p < start]
    for ppi in ladder:
        if _tiles_at(boxes, ppi, w_in, h_in, image_spec) <= MAX_TILES:
            return ppi
    return None


def fit_png(png: bytes, w_px: int, h_px: int, image_spec: Any = None) -> tuple[bytes, int, int]:
    """`png` unchanged when it fits the provider's image spec, else
    downscaled (same aspect) to the largest size that fits. ValueError
    when an oversized image cannot be decoded."""
    spec = _spec(image_spec)
    if w_px <= 0 or h_px <= 0 or spec.fits(w_px, h_px):
        return png, w_px, h_px
    from PIL import Image

    nw, nh = spec.scale_to_fit(w_px, h_px)
    try:
        image = Image.open(io.BytesIO(png))
        image.load()
    except Exception as exc:  # noqa: BLE001 - PIL raises several error types
        raise ValueError("an oversized view could not be scaled to the provider's image limits") from exc
    image = image.convert("RGB").resize((nw, nh), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, "PNG", optimize=True)
    return buf.getvalue(), nw, nh


def _view_prefix(ctx: ToolContext, document_id: str, page: int) -> str:
    call = re.sub(r"[^A-Za-z0-9]", "", str(ctx.call_id or "x"))[-10:] or "x"
    return f"{doc8(document_id)}-p{page}-{call}"


def _render_views(
    ctx: ToolContext,
    document_id: str,
    page: int,
    pdf_page: int,
    boxes: list[Bbox],
    ppi: int,
    w_in: float,
    h_in: float,
    image_spec: Any = None,
) -> list[dict[str, Any]]:
    spec = _spec(image_spec)
    svc = ctx.svc()
    pdf = svc.documents.pdf_bytes(document_id)
    if not isinstance(pdf, (bytes, bytearray)):
        raise ValueError("the document's PDF bytes are not available on this server")
    full_w = images.px_for_points(w_in * 72, ppi)
    full_h = images.px_for_points(h_in * 72, ppi)
    prefix = _view_prefix(ctx, document_id, page)
    views: list[dict[str, Any]] = []
    for bi, box in enumerate(boxes):
        bx0, by0, bx1, by1 = images.box_from_fractions(tuple(box), full_w, full_h)  # type: ignore[arg-type]
        tiles = [
            (bx0 + a, by0 + b, bx0 + c, by0 + d) for (a, b, c, d) in plan_tiles_for(bx1 - bx0, by1 - by0, spec)
        ]
        for ti, tile in enumerate(tiles):
            ctx.heartbeat()
            png, w_px, h_px = images.render_page_png(
                bytes(pdf), pdf_page - 1, ppi, box_px=tile, max_patches=spec.max_patches
            )
            png, w_px, h_px = fit_png(png, w_px, h_px, spec)
            view_id = f"{prefix}-{bi + 1}{ti + 1}"
            views.append(
                {
                    "view_id": view_id,
                    "ref": f"v:{view_id}",
                    "document_id": document_id,
                    "page": page,
                    "bbox": images.box_to_fractions(tile, full_w, full_h),
                    "tile_of": bi,
                    "ppi": ppi,
                    "w_px": w_px,
                    "h_px": h_px,
                    "image": png,
                }
            )
    return views


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


def _text_quality(svc: ToolServices, document_id: str, page: int, runs: Any) -> str:
    probe = getattr(svc.text, "text_layer_quality", None)
    if callable(probe):
        try:
            q = probe(document_id, page)
            if isinstance(q, str) and q:
                return q
        except Exception:  # noqa: BLE001 - fall back to the runs
            pass
    if isinstance(runs, list):
        return TEXT_LAYER_QUALITY_FULL if runs else "none"
    return "unknown"


def _candidates(runs: list[dict[str, Any]], bbox: Bbox | None) -> list[dict[str, Any]]:
    """Texts the model may anchor on: one per distinct text (first
    occurrence, reading order), tag-like tokens first, inside `bbox` when
    one was given, at most MAX_CANDIDATES."""
    seen: set[str] = set()
    tagged: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    ordered = sorted(
        (run for run in runs if _run_bbox(run) is not None),
        key=lambda r: (round(_run_bbox(r)[1], 2), _run_bbox(r)[0]),  # type: ignore[index]
    )
    for run in ordered:
        box = _run_bbox(run)
        text = _norm(run.get("text") or run.get("t"))
        if box is None or not text or text in seen:
            continue
        if bbox is not None and not _contains(bbox, *_center(box)):
            continue
        seen.add(text)
        item = {"ref": run.get("ref"), "text": text, "bbox": box}
        (tagged if _TAG_LIKE_RE.match(text) else other).append(item)
    if bbox is None:
        other = []
    return (tagged + other)[:MAX_CANDIDATES]


def zoom_region(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = ctx.svc()
    document_id = str(args.get("document_id") or "")
    page = int(args.get("page") or 0)
    purpose = str(args.get("purpose") or "")
    if purpose not in PURPOSES:
        return ToolResult.refusal("invalid_args", f"purpose must be one of {', '.join(PURPOSES)}")
    if page < 1:
        return ToolResult.refusal("invalid_args", "page is 1-based")
    size = svc.documents.page_size(document_id, page)
    if not isinstance(size, dict) or "error" in size:
        return ToolResult.refusal(
            "unknown_document",
            f"document {document_id[:8]} page {page} is not in this run",
            size if isinstance(size, dict) else None,
        )
    w_in, h_in = float(size.get("w_in") or 0), float(size.get("h_in") or 0)
    if w_in <= 0 or h_in <= 0:
        return ToolResult.refusal("unknown_document", "the page has no size")
    bbox = _clamp_box(args["bbox"]) if args.get("bbox") else None
    if bbox is not None and _area(bbox) <= 0:
        return ToolResult.refusal("invalid_args", "bbox is empty")
    warnings: list[str] = []
    runs_raw = svc.text.runs(document_id, page)
    runs: list[dict[str, Any]] = runs_raw if isinstance(runs_raw, list) else []
    quality = _text_quality(svc, document_id, page, runs_raw)
    has_text = quality == TEXT_LAYER_QUALITY_FULL or (quality == "sparse" and bool(runs))
    lines = _Lines(runs, w_in, h_in)

    targets: list[Target] = []
    anchor = args.get("anchor")
    if isinstance(anchor, dict):
        ref = anchor.get("ref")
        if ref:
            idx = _ref_index(str(ref), document_id, page)
            if idx is not None and 0 <= idx < len(runs):
                box = _run_bbox(runs[idx])
                if box is not None:
                    targets.append(Target(bbox=box, ref=str(ref), text=str(runs[idx].get("text") or ""), source="anchor"))
            else:
                warnings.append(f"anchor ref {ref} is not a text run of this page")
        if anchor.get("text"):
            found = _targets_from_text(runs, str(anchor["text"]), anchor.get("occurrence"), source="anchor")
            if not found:
                warnings.append(f"anchor text {_norm(anchor['text'])[:40]!r} was not found on this page")
            targets.extend(found)
    if args.get("expect_text"):
        found = _targets_from_text(runs, str(args["expect_text"]), None, source="expect_text")
        if not found:
            warnings.append(f"expect_text {_norm(args['expect_text'])[:40]!r} was not found on this page")
        targets.extend(found)
    if args.get("census_ref"):
        targets.extend(_targets_from_census(ctx, str(args["census_ref"]), warnings))
    for t in targets:
        if t.ref and "{doc8}" in t.ref:
            t.ref = t.ref.replace("{doc8}", doc8(document_id)).replace("{page}", str(page))
    if purpose == "read_text":
        for t in targets:
            if t.source == "census":
                continue
            idx = _ref_index(t.ref or "", document_id, page)
            if idx is not None:
                row = lines.row_box(idx, w_in=w_in, h_in=h_in)
                if row is not None:
                    t.bbox = _union([t.bbox, row])

    if not targets and purpose in TARGET_REQUIRED_PURPOSES and has_text:
        data = {
            "document_id": document_id,
            "page": page,
            "purpose": purpose,
            "views": [],
            "bbox_used": [],
            "retargeted": False,
            "retarget_reason": "none",
            "coverage": {"targets": 0, "inside": 0, "complete": False},
            "text_lines": [],
            "candidates": _candidates(runs, bbox),
            "warnings": warnings
            + [
                f"{purpose} on a page with a text layer needs a target: give anchor (text or t: ref), "
                "expect_text or census_ref, for example one of the candidates. No image was rendered."
            ],
            "text_layer_quality": quality,
        }
        return ToolResult.ok(data)
    if not targets and bbox is None:
        return ToolResult.refusal(
            "need_target",
            "zoom_region needs a bbox or a target (anchor, expect_text or census_ref)",
            {"text_layer_quality": quality, "warnings": warnings},
        )

    boxes, retargeted, reason, coverage = plan_views(bbox, targets, purpose, w_in, h_in)
    boxes = [b for b in boxes if _area(b) > 0]
    if not boxes:
        return ToolResult.refusal("invalid_args", "the region to render is empty")
    image_spec = _spec(ctx.image_spec)
    ppi = choose_ppi(boxes, purpose, args.get("ppi"), w_in, h_in, image_spec)
    if ppi is None:
        return ToolResult.refusal(
            "region_too_large",
            f"the region(s) would need more than {MAX_TILES} tiles even at {PPI_LADDER[-1]} ppi; "
            "narrow the bbox or zoom one cluster at a time",
            {"bbox_used": boxes, "coverage": coverage, "retarget_reason": reason},
        )
    pdf_page_of = getattr(svc.documents, "pdf_page", None)
    pdf_page = page
    if callable(pdf_page_of):
        try:
            pdf_page = int(pdf_page_of(document_id, page) or page)
        except Exception:  # noqa: BLE001 - the page is its own pdf page
            pdf_page = page
    try:
        views = _render_views(ctx, document_id, page, pdf_page, boxes, ppi, w_in, h_in, image_spec)
    except ValueError as exc:
        return ToolResult.refusal("render_failed", f"the page could not be rendered: {exc}")
    if not has_text and purpose in TARGET_REQUIRED_PURPOSES:
        warnings.append(
            f"this page has no usable text layer ({quality}); counts from the image are not floors "
            "and no text census can confirm them"
        )
    text_lines = lines.inside(boxes)
    data = {
        "document_id": document_id,
        "page": page,
        "purpose": purpose,
        "ppi": ppi,
        "image_provider": image_spec.provider,
        "views": [{k: v for k, v in view.items() if k != "image"} for view in views],
        "bbox_used": boxes,
        "retargeted": retargeted,
        "retarget_reason": reason,
        "coverage": coverage,
        "targets": [
            {"ref": t.ref, "text": t.text, "bbox": t.bbox, "source": t.source} for t in targets[:MAX_TARGETS_LISTED]
        ],
        "text_lines": text_lines,
        "candidates": [],
        "warnings": warnings,
        "text_layer_quality": quality,
    }
    citations = [view["ref"] for view in views] + [t.ref for t in targets if t.ref and t.source != "census"]
    return ToolResult.ok(data, citations=citations, views=views)


ZOOM_SPEC = ToolSpec(
    name="zoom_region",
    description=ZOOM_DESCRIPTION,
    args_schema=ZOOM_ARGS,
    result_doc=ZOOM_RESULT_DOC,
    mode="read",
    kinds=kinds_from_codes("RC, RV, C"),
    handler=zoom_region,
    cost_class="image",
    needs_pro_max=True,
    max_calls_per_group=8,
    timeout_s=90.0,
)


def register_tools() -> None:
    """Put the WS2 tools in REGISTRY (idempotent)."""
    register(ZOOM_SPEC, replace=True)


register_tools()


# ---------------------------------------------------------------------------
# DocumentAccess and the heartbeat
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class RunDocuments:
    """DocumentAccess over a run's plans_reader_documents rows: page sizes
    from the splitter's page_meta (or the document's sheet size), PDF bytes
    downloaded once per instance through services.storage, the pdf page of
    a document page (prepended cover pages skipped). `facts` is an optional
    EvidenceInputs provider that knows the rotation and text class."""

    def __init__(self, sb: Any, run_id: str, *, facts: Any = None, rows: list[dict[str, Any]] | None = None) -> None:
        self.sb = sb
        self.run_id = run_id
        self.facts = facts
        self._rows: dict[str, dict[str, Any]] | None = (
            {str(r.get("id")): dict(r) for r in rows} if rows is not None else None
        )
        self._bytes: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._rows is None:
            rows = (
                self.sb.table("plans_reader_documents").select("*").eq("run_id", self.run_id).execute().data or []
            )
            self._rows = {str(r.get("id")): dict(r) for r in rows}
        return self._rows

    def documents(self, run_id: str) -> list[dict[str, Any]]:
        out = []
        for doc_id, row in self._load().items():
            out.append(
                {
                    "id": doc_id,
                    "name": row.get("name"),
                    "category": row.get("category"),
                    "page_count": row.get("page_count"),
                    "w_in": row.get("sheet_width_in"),
                    "h_in": row.get("sheet_height_in"),
                }
            )
        return out

    def _meta(self, document_id: str, page: int) -> dict[str, Any] | None:
        row = self._load().get(document_id)
        if row is None:
            return None
        meta = row.get("page_meta")
        own = [m for m in meta if isinstance(m, dict) and not m.get("prepended")] if isinstance(meta, list) else []
        if 1 <= page <= len(own):
            return own[page - 1]
        return {}

    def pdf_page(self, document_id: str, page: int) -> int:
        meta = self._meta(document_id, page) or {}
        return _as_int(meta.get("pdf_page")) or page

    def page_size(self, document_id: str, page: int) -> dict[str, Any]:
        row = self._load().get(document_id)
        if row is None:
            return {"error": "unknown_document", "document_id": document_id}
        meta = self._meta(document_id, page) or {}
        w = meta.get("w_in") or row.get("sheet_width_in")
        h = meta.get("h_in") or row.get("sheet_height_in")
        rotation = 0
        text_class = None
        if self.facts is not None:
            try:
                sheet = self.facts.sheet(document_id, page)
                w, h, rotation, text_class = sheet.w_in, sheet.h_in, sheet.rotation, sheet.text_class
            except FactsUnavailable:
                pass
        try:
            w_in, h_in = float(w or 0), float(h or 0)
        except (TypeError, ValueError):
            w_in = h_in = 0.0
        if w_in <= 0 or h_in <= 0:
            try:
                sizes = images.page_sizes_in(self.pdf_bytes(document_id))  # type: ignore[arg-type]
                pdf_page = self.pdf_page(document_id, page)
                if 1 <= pdf_page <= len(sizes):
                    w_in, h_in = sizes[pdf_page - 1]
            except Exception:  # noqa: BLE001 - reported as unknown below
                pass
        if w_in <= 0 or h_in <= 0:
            return {"error": "unknown_page", "document_id": document_id, "page": page}
        out: dict[str, Any] = {"w_in": w_in, "h_in": h_in, "rotation": rotation}
        if text_class:
            out["text_layer_quality"] = text_layer_quality_for(text_class)
        return out

    def pdf_bytes(self, document_id: str) -> bytes | dict[str, Any]:
        row = self._load().get(document_id)
        if row is None or not row.get("storage_path"):
            return {"error": "unknown_document", "document_id": document_id}
        with self._lock:
            if document_id not in self._bytes:
                from app.services import storage

                self._bytes[document_id] = storage.download_file(str(row["storage_path"]))
            return self._bytes[document_id]


class LocalDocuments:
    """DocumentAccess over PDF bytes in memory (tests, replay, offline eval).
    Page sizes come from PDFium (displayed frame)."""

    def __init__(self, pdfs: dict[str, bytes], *, names: dict[str, str] | None = None) -> None:
        self._pdfs = dict(pdfs)
        self._names = dict(names or {})
        self._sizes: dict[str, list[tuple[float, float]]] = {}
        self.bytes_reads = 0

    def documents(self, run_id: str) -> list[dict[str, Any]]:
        return [{"id": d, "name": self._names.get(d, d), "category": None} for d in self._pdfs]

    def _page_sizes(self, document_id: str) -> list[tuple[float, float]]:
        if document_id not in self._sizes:
            self._sizes[document_id] = images.page_sizes_in(self._pdfs[document_id])
        return self._sizes[document_id]

    def page_size(self, document_id: str, page: int) -> dict[str, Any]:
        if document_id not in self._pdfs:
            return {"error": "unknown_document", "document_id": document_id}
        try:
            sizes = self._page_sizes(document_id)
        except ValueError as exc:
            return {"error": "unreadable_document", "document_id": document_id, "detail": str(exc)}
        if not 1 <= page <= len(sizes):
            return {"error": "unknown_page", "document_id": document_id, "page": page}
        w, h = sizes[page - 1]
        return {"w_in": w, "h_in": h, "rotation": 0}

    def pdf_page(self, document_id: str, page: int) -> int:
        return page

    def pdf_bytes(self, document_id: str) -> bytes | dict[str, Any]:
        if document_id not in self._pdfs:
            return {"error": "unknown_document", "document_id": document_id}
        self.bytes_reads += 1
        return self._pdfs[document_id]


class WordsTextIndex:
    """A TextIndex over an EvidenceInputs provider (WS1 words through
    plans_reader_evidence_inputs): offline tests, replay and golden checks
    until WS1 registers its database-backed index. Refs are
    `t:{doc8}:{page}:{word index}`.

    `search` loads every page it is asked about (spec 5.3: it searches the
    given documents and pages), enumerating documents and pages through
    `documents` (a DocumentAccess) when the scope leaves them open. A page
    that cannot be loaded, or a scope that cannot be enumerated, makes the
    result partial (`complete: false` plus the pages not searched), and a
    search that could load nothing at all is the typed unavailable result:
    "none found" must never read as a confirmed absence."""

    MAX_PAGES_PER_DOCUMENT = 500

    def __init__(self, inputs: Any, *, documents: Any = None) -> None:
        self.inputs = inputs
        self.documents = documents
        self._cache: dict[tuple[str, int], Any] = {}

    def _sheet(self, document_id: str, page: int) -> Any:
        key = (document_id, page)
        if key not in self._cache:
            self._cache[key] = self.inputs.sheet(document_id, page)
        return self._cache[key]

    def text_layer_quality(self, document_id: str, page: int) -> str:
        try:
            return str(self._sheet(document_id, page).text_layer_quality)
        except FactsUnavailable:
            return "unknown"

    def runs(self, document_id: str, page: int) -> list[dict[str, Any]] | dict[str, Any]:
        try:
            sheet = self._sheet(document_id, page)
        except FactsUnavailable as exc:
            return unavailable_result("text", str(exc))
        d8 = doc8(document_id)
        return [
            {"ref": f"t:{d8}:{page}:{i}", "document_id": document_id, "page": page, "text": w.t, "bbox": list(w.b), "h": w.h, "r": w.r}
            for i, w in enumerate(sheet.words)
        ]

    def _document_ids(self) -> list[str] | None:
        """Every document of the run through DocumentAccess, or None when
        there is no way to list them."""
        if self.documents is None:
            return None
        try:
            docs = self.documents.documents("")
        except Exception:  # noqa: BLE001 - an unlistable scope is reported partial
            return None
        if not isinstance(docs, list):
            return None
        return [str(d.get("id")) for d in docs if isinstance(d, dict) and d.get("id")]

    def _page_numbers(self, document_id: str) -> list[int] | None:
        """1-based pages of a document: the listed page_count, else probing
        page_size until it reports an unknown page; None when unknown."""
        if self.documents is None:
            return None
        try:
            docs = self.documents.documents("")
        except Exception:  # noqa: BLE001
            docs = None
        if isinstance(docs, list):
            for d in docs:
                if isinstance(d, dict) and str(d.get("id")) == document_id and d.get("page_count"):
                    try:
                        return list(range(1, int(d["page_count"]) + 1))
                    except (TypeError, ValueError):
                        break
        out: list[int] = []
        for page in range(1, self.MAX_PAGES_PER_DOCUMENT + 1):
            try:
                size = self.documents.page_size(document_id, page)
            except Exception:  # noqa: BLE001
                return out or None
            if not isinstance(size, dict) or size.get("error"):
                return out or None
            out.append(page)
        return out

    def _scope(
        self, document_ids: list[str] | None, pages: list[int] | None
    ) -> tuple[list[tuple[str, int]], list[str]]:
        """(doc, page) pairs to search and what of the scope could not be
        enumerated."""
        unknown: list[str] = []
        docs = list(document_ids) if document_ids else self._document_ids()
        if docs is None:
            docs = sorted({d for d, _ in self._cache})
            unknown.append("the run's document list")
        pairs: list[tuple[str, int]] = []
        for doc_id in docs:
            if pages:
                pairs.extend((doc_id, int(p)) for p in pages)
                continue
            numbers = self._page_numbers(doc_id)
            if numbers is None:
                numbers = sorted(p for d, p in self._cache if d == doc_id)
                unknown.append(f"the pages of document {doc8(doc_id)}")
            pairs.extend((doc_id, p) for p in numbers)
        return sorted(set(pairs)), unknown

    def search(
        self,
        query: str,
        *,
        document_ids: list[str] | None = None,
        pages: list[int] | None = None,
        mode: str = "token",
        limit: int = 40,
        offset: int = 0,
    ) -> dict[str, Any]:
        pairs, unknown = self._scope(document_ids, pages)
        loaded: list[tuple[str, int, Any]] = []
        not_searched: list[dict[str, Any]] = []
        for doc_id, page in pairs:
            try:
                loaded.append((doc_id, page, self._sheet(doc_id, page)))
            except FactsUnavailable as exc:
                not_searched.append({"document_id": doc_id, "page": page, "reason": str(exc)[:120]})
        if not loaded:
            return unavailable_result(
                "text",
                "no page of the requested scope could be loaded, so nothing was searched"
                + (f" ({'; '.join(unknown)} unknown)" if unknown else ""),
            )
        hits: list[dict[str, Any]] = []
        for doc_id, page, sheet in loaded:
            for i, w in enumerate(sheet.words):
                ok = _norm(w.t) == _norm(query) if mode == "exact" else text_matches(w.t, query)
                if ok:
                    hits.append({"ref": f"t:{doc8(doc_id)}:{page}:{i}", "document_id": doc_id, "page": page, "text": w.t, "bbox": list(w.b)})
        total = len(hits)
        page_hits = hits[offset : offset + limit]
        omitted = max(0, total - offset - len(page_hits))
        out: dict[str, Any] = {
            "hits": page_hits,
            "total": total,
            "omitted": omitted,
            "next_offset": (offset + len(page_hits)) if omitted else None,
            "pages_searched": len(loaded),
            "complete": not not_searched and not unknown,
        }
        if not_searched or unknown:
            out["pages_not_searched"] = not_searched[:MAX_TARGETS_LISTED]
            out["warning"] = (
                "partial search: "
                + "; ".join(
                    ([f"{len(not_searched)} page(s) could not be loaded"] if not_searched else [])
                    + [f"{u} is not known here" for u in unknown]
                )
                + ". A total of 0 here means not found in the pages searched, never absent."
            )
        return out


def heartbeat() -> None:
    """ToolContext.heartbeat for queue jobs: renew the lease or raise
    LlmLeaseLost (a no-op outside a queue job)."""
    llm.renew_lease_or_raise()


def services_for_run(
    sb: Any,
    run_id: str,
    *,
    documents: Any = None,
    pdfs: dict[str, bytes] | None = None,
    inputs: Any = None,
    document_rows: list[dict[str, Any]] | None = None,
) -> ToolServices:
    """A ToolServices for one loop: every slot of the process registry, with
    `documents` set to the run's DocumentAccess (RunDocuments over the
    client, or LocalDocuments over `pdfs`) and, when the registry has no
    text index yet and an EvidenceInputs provider is given, a
    WordsTextIndex over it."""
    base = services()
    out = ToolServices(**{name: getattr(base, name) for name in base.__dataclass_fields__})
    if documents is not None:
        out.documents = documents
    elif pdfs:
        out.documents = LocalDocuments(pdfs)
    elif sb is not None:
        out.documents = RunDocuments(sb, run_id, facts=inputs, rows=document_rows)
    if inputs is not None and type(out.text).__name__ == "Unavailable":
        out.text = WordsTextIndex(inputs, documents=out.documents)
    return out


__all__ = [
    "CLUSTER_EPS_IN",
    "LocalDocuments",
    "MAX_TILES",
    "PAD_IN",
    "PPI_BY_PURPOSE",
    "PPI_LADDER",
    "PURPOSES",
    "RunDocuments",
    "Target",
    "WordsTextIndex",
    "ZOOM_ARGS",
    "ZOOM_SPEC",
    "choose_ppi",
    "fit_png",
    "heartbeat",
    "plan_tiles_for",
    "plan_views",
    "register_tools",
    "services_for_run",
    "text_matches",
    "zoom_region",
]
