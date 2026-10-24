"""Measurement stage (WS3 steps B5 to B10; docs/AI_ESTIMATING_BUILD_SPEC.md
4.1 derive group, 5.2 measure tools, 5.3 MeasureService, 5.4 MeasureOutput
v1, 5.8 measure write tools, 6 WS3).

`build_measure(inputs, conv)` is the derive-stage orchestration over WS1
page facts and the WS3 geometry, circuits and routes modules. It is pure
over its inputs (facts, optional word, segment and heavy-segment loaders,
category outputs, scope inclusions) and makes no model call:

1. scales per plan view (`plans_reader_geometry.scales_for_page`), one
   `SheetFrame` per plan sheet, NTS sheets measure nothing;
2. registrations between the device sheets and the panel sheets
   (shared-token median solve, identity frames, single anchor, chains);
3. panels from the WS1 schedules and plan labels, with human or model
   `set_panel_location` overrides (a human beats a model, never the
   reverse);
4. circuits from `plans_reader_circuits.wiring_plans` plus the devices of
   each circuit (tag per device; stacked labels and arrows snapped to the
   nearest device);
5. routes (`plans_reader_routes.synthesize_routes`), then the lower tiers:
   equipment connections not covered by a tagged circuit get a home-run
   allowance, device positions without tags get synthetic circuits, and
   with no positions at all the density tier (devices x band median) with
   the `branch_density_only` warning;
6. linear items: vector rectangle candidates nearest each linear fixture
   tag (L5), demolition track anchored on the WS1 keynote markers of a
   linear demolition row (the keynote the row cites, a keynote whose text
   names the track or the lighting removal, or a marker within reach of
   the row's positions; heavy collinear dashes merged); model row
   positions are never anchors themselves, and without a keynote anchor
   (no vector legend symbol matcher exists yet) the row gets a
   `linear_in_ea` block finding routed to a human;
7. measured runs (RUN_ITEM `route` polylines) and the control cable
   allowance (25 ft per device when WS5 includes the thermostats);
8. quantities as `M<n>` rows in the renderer's `_CONDUIT_RE` form, the
   band checks, findings, judgments, overrides and stats.

`apply_measure_to_boq` (legacy main mode) replaces only the model's branch
rows with the M rows and emits a finding per deleted row;
`branch_coverage_findings` maps the measure findings onto the gate ids
(`branch_ft_empty` -> `new_devices_no_branch`). `MeasureService` implements
the plans_reader_tools Protocol and backs the six registered tools; the
versioned writer keeps every override.

Sheet identity: a sheet key is the sheet number (else `D<doc>P<page>`).
When a later document re-issues a sheet number (an addendum E2.01), the
later document wins and the earlier page is marked `superseded_by` in
`scales[]` and skipped everywhere; the two are never merged.

Ids (spec 5.1, 5.2): `M<n>` rows and `r<n>` routes are stable across
measure versions of a run. `stabilize_ids(new, prior)` keeps the prior id
of every row keyed by (group, subgroup, description, kind, m: ref) and of
every route keyed by (tier, circuit_id, occurrence), issues new ids above
the run's high-water marks `row_seq` and `route_seq`, and never reissues a
retired id (`retired_ids`). Registrations whose callout check failed
(`callout_mismatch`) are dropped from routing with a
`registration_callout_mismatch` finding. `measure_polyline` ids are
`pl<12 hex>` of sha256(run_id, document_id, page, points, vertical_ft) and
are stored in the output's `polylines[]` (resolvable after a resume or on
another worker through `MeasureService.resolve_measurement`).

Derivation terms (`quantities[].derivation.terms`; the stable shape WS8
evidence and WS7 review read, keep it unchanged):

- wire, conduit and cable rows (`plans_reader_routes.branch_quantities`):
  one term per route segment and conductor size,
  `{"circuit_id": str, "route_id": "r<n>", "segment": "collector" |
  "homerun" | "tie_in" | "drop" | "allowance" | "switch_leg",
  "length_ft": float (segment length, 2 dp), "count": int (conductors of
  that size; 1 for conduit and cable), "basis": measure basis}`; the row's
  `refs` hold `r:<route_id>`;
- linear rows (linear fixtures and demolition track): one term per
  measured instance, `{"instance": int (1-based into the linear item's
  instances[]), "length_ft": float, "method": "vector_rect" | "heavy_run" |
  "<by>_endpoints", "measure_id": str, "linear_item_id": str (same as
  measure_id), "basis": measure basis}`, the derivation also carries
  `"tag"` and `"measure_id"`, and the row's `refs` hold `m:<measure_id>`
  (the key of the item in `linear_items[]`, field `measure_id`);
- control cable: one term `{"devices": int, "ft_per_device": float}` with
  `"inclusion": "thermostats"` on the derivation.

No database at import time; the DB store is a small sync client wrapper
used from worker threads only. No prices anywhere.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import plans_reader_circuits as circuits_mod
from app.services import plans_reader_geometry as geometry
from app.services import plans_reader_measure_bands as measure_bands
from app.services import plans_reader_routes as routes_mod
from app.services.electrical_items import LINEAR_LUMINAIRE_RE, is_lighting_track, is_linear, parse_item
from app.services.estimating_contracts import ASSUMED_CAVEAT, GATE_ALIASES, line_basis_for_measure, make_row_id
from app.services.pagefacts.contracts import PLAN_ROLES, DocFacts, PageFacts, PageWords
from app.services.plans_reader_render import parse_conduit
from app.services.plans_reader_review_schemas import MEASURE_WRITE_TOOL_ARGS
from app.services.plans_reader_schemas import _arr, _int, _nullable, _num, _obj, _str
from app.services.plans_reader_tools import (
    ToolContext,
    ToolResult,
    ToolSpec,
    kinds_from_codes,
    register,
    register_services,
)

logger = logging.getLogger(__name__)

COMPANY_TZ = ZoneInfo("America/Los_Angeles")
MEASURE_SCHEMA_VERSION = 1
STAGE_VERSION = "ws3-measure-1"
MAX_SUMMARY_CHARS = 6000

# Linear items ---------------------------------------------------------------------------------
RECT_MIN_LONG_IN = 0.2
RECT_GAP_IN = (0.05, 0.30)
RECT_LEN_TOL = 0.03
RECT_OVERLAP_MIN = 0.95
RECT_CAP_TOL = 0.30
RECT_SEARCH_IN = 0.6
RECT_BOX_IN = (6.0, 3.0)
RECT_AXIS_TOL_IN = 0.02
LINEAR_MIN_FT = 1.0
LINEAR_MAX_FT = 40.0
TRACK_MIN_STROKE_PT = 1.5
TRACK_SEARCH_IN = 1.0
TRACK_GAP_IN = 0.3
TRACK_MIN_FT = 1.0
ANCHOR_REACH_IN = 2.0
TRACK_SIBLING_LEN_TOL = 0.03
TRACK_SIBLING_OVERLAP = 0.9
TRACK_SIBLING_GAP_IN = (0.25, 4.0)
LENGTHS_AS_REQUIRED_RE = re.compile(r"LENGTHS?\s+AS\s+(?:REQUIRED|SHOWN|INDICATED)|PER\s+FOOT|/\s*FT\b|\bLF\b", re.I)
KEYNOTE_CITE_RE = re.compile(r"KEYNOTE\s*#?\s*(\d{1,3})", re.I)
POINT_RE = re.compile(r"\(\s*(\d*\.\d+|\d+)\s*,\s*(\d*\.\d+|\d+)\s*\)")
CONNECTION_RE = re.compile(r"\b(?:ELECTRICAL|EQUIPMENT|MOTOR|POWER)\s+CONN?ECTIONS?\b|\bECONN\b", re.I)
PANEL_ROW_RE = re.compile(r"^\s*(?:\((?:E|N)\)\s*|NEW\s+|EXISTING\s+)?PANEL(?:BOARD)?\s*:?\s*['\"]?(?P<name>[A-Z0-9][A-Z0-9\-]{0,7})['\"]?\b", re.I)
PANEL_LOCATION_CONFIDENCE = 0.5
LIGHTING_DEVICE_CLASSES: frozenset[str] = frozenset({"luminaire", "luminaire_linear", "switch", "control"})
CONNECTION_CLASSES: frozenset[str] = frozenset({"equipment_connection", "motor_connection"})
BRANCH_CONDUCTOR_RE = re.compile(r"^\s*#\s*(?:14|12|10)\b", re.I)
BRANCH_CABLE_RE = re.compile(r"^\s*#\s*(?:14|12|10)\s*/\s*\dC\s+(?:MC|NM)\b", re.I)
BRANCH_CONDUIT_SIZES: frozenset[str] = frozenset({"1/2", "3/4"})
CONTROL_CABLE_DESC = "#18/4C CONTROL CABLE"
THERMOSTAT_FAMILIES: frozenset[str] = frozenset({"thermostat", "thermostats", "control_cable", "control cable"})
NEW_DEVICE_GROUPS: tuple[str, ...] = ("OTHER ITEMS", "LIGHTING FIXTURES", "LIGHTING CONTROLS")
EQUIPMENT_COVER_FT = 6.0
BASIS_ORDER = routes_mod.BASIS_RANK


def now_pacific_iso() -> str:
    return datetime.now(COMPANY_TZ).isoformat(timespec="seconds")


# Inputs -----------------------------------------------------------------------------------------


@dataclass
class MeasureDocument:
    """One drawing document of the run with its WS1 facts."""

    document_id: str
    doc_index: int
    facts: DocFacts
    label: str | None = None
    name: str | None = None

    @classmethod
    def from_facts(cls, document_id: str, doc_index: int, facts: DocFacts | Mapping[str, Any], *, label: str | None = None) -> MeasureDocument:
        f = facts if isinstance(facts, DocFacts) else DocFacts.from_dict(dict(facts))
        return cls(document_id=document_id, doc_index=doc_index, facts=f, label=label or f.label, name=f.name)


@dataclass
class ScopeInputs:
    """What the measure stage reads from WS5 (inclusions and the wiring
    method). Tests fake it; the derive wiring passes the ScopeContext
    inclusions."""

    wiring_method: str = "emt"
    thermostats_included: bool = False
    control_cable_devices: int = 0
    inclusions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_scope_context(cls, ctx: Mapping[str, Any] | None) -> ScopeInputs:
        """From a WS5 ScopeContext dict: `standards.wiring_method` (or
        `raceway`), and the thermostat inclusions. Inclusions are deduped by
        trigger family (one control-cable length per thermostat set): the
        device count is the largest EA count among the family's device
        inclusions; companions (`companion_of` set, e.g. IN-CONTROL-CABLE)
        and FT records (a cable length, never a device count) are skipped."""
        if not ctx:
            return cls()
        standards = ctx.get("standards") or {}
        method = str(standards.get("wiring_method") or "").lower()
        raceway = str(standards.get("raceway") or standards.get("branch_conduit") or "").upper()
        if method not in routes_mod.WIRING_METHODS:
            method = "mc" if " MC" in f" {raceway}" else "nm" if " NM" in f" {raceway}" else "emt"
        inclusions = [dict(i) for i in (ctx.get("inclusions") or []) if isinstance(i, Mapping)]
        by_family: dict[str, int] = {}
        for inc in inclusions:
            fam = str(inc.get("trigger_family") or inc.get("family") or "").lower()
            if fam not in THERMOSTAT_FAMILIES:
                continue
            if inc.get("companion_of") or str(inc.get("unit") or "EA").upper() != "EA":
                continue
            try:
                n = int(round(float(inc.get("count") or inc.get("quantity") or 0)))
            except (TypeError, ValueError):
                n = 0
            # Every thermostat family name is one set: thermostat and control cable.
            by_family["thermostat"] = max(by_family.get("thermostat", 0), n)
        devices = by_family.get("thermostat", 0)
        return cls(wiring_method=method, thermostats_included=devices > 0, control_cable_devices=devices, inclusions=inclusions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wiring_method": self.wiring_method,
            "thermostats_included": self.thermostats_included,
            "control_cable_devices": self.control_cable_devices,
        }


WordsLoader = Callable[[str, int], PageWords | None]
SegmentsLoader = Callable[[str, int], geometry.SegmentIndex | None]
HeavyLoader = Callable[[str, int], "list[WeightedSegment] | None"]


@dataclass
class MeasureInputs:
    run_id: str | None
    documents: list[MeasureDocument]
    category_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    scope: ScopeInputs = field(default_factory=ScopeInputs)
    project_type: str | None = None
    words: WordsLoader | None = None
    segments: SegmentsLoader | None = None
    heavy: HeavyLoader | None = None
    registrations: list[geometry.Registration] | None = None
    overrides: list[dict[str, Any]] = field(default_factory=list)
    heartbeat: Callable[[], None] = lambda: None
    new_device_count: int | None = None
    max_registration_pairs: int = 60
    # The run's eval case id (spec 5.7, 8.7): the bands leave its project out (LOPO).
    eval_case_id: str | None = None
    # The previous measure output of the run (ids persist from it, new ones are appended).
    prior: Mapping[str, Any] | None = None

    @classmethod
    def from_pdfs(
        cls,
        run_id: str | None,
        documents: Sequence[MeasureDocument],
        pdf_bytes: Mapping[str, bytes],
        **kwargs: Any,
    ) -> MeasureInputs:
        """Loaders over PDF bytes keyed by document id: WS1 words
        (`pagefacts.extract.extract_words`), the geometry segment index and
        the stroke-weighted segments, each cached per (document, page)."""
        from app.services.pagefacts import extract as pf_extract

        words_cache: dict[tuple[str, int], PageWords | None] = {}
        seg_cache = geometry.SegmentCache()
        heavy_cache: dict[tuple[str, int], list[WeightedSegment] | None] = {}

        def _words(doc_id: str, page: int) -> PageWords | None:
            key = (doc_id, int(page))
            if key not in words_cache:
                pdf = pdf_bytes.get(doc_id)
                if pdf is None:
                    words_cache[key] = None
                else:
                    try:
                        words_cache[key] = pf_extract.extract_words(pdf, pages=[int(page)]).get(int(page))
                    except Exception:  # noqa: BLE001 - a broken page measures nothing
                        words_cache[key] = None
            return words_cache[key]

        def _segments(doc_id: str, page: int) -> geometry.SegmentIndex | None:
            pdf = pdf_bytes.get(doc_id)
            if pdf is None:
                return None
            return seg_cache.get(doc_id, int(page), lambda p: geometry.index_segments(pdf, p - 1))

        def _heavy(doc_id: str, page: int) -> list[WeightedSegment] | None:
            key = (doc_id, int(page))
            if key not in heavy_cache:
                pdf = pdf_bytes.get(doc_id)
                try:
                    heavy_cache[key] = heavy_segments(pdf, int(page) - 1) if pdf is not None else None
                except Exception:  # noqa: BLE001
                    heavy_cache[key] = None
            return heavy_cache[key]

        return cls(run_id=run_id, documents=list(documents), words=_words, segments=_segments, heavy=_heavy, **kwargs)


# Stroke-weighted segments (for the track) ---------------------------------------------------


@dataclass(slots=True)
class WeightedSegment:
    """A straight path segment with its stroke width in points (page
    fractions, like `plans_reader_geometry.Segment`)."""

    x0: float
    y0: float
    x1: float
    y1: float
    stroke_pt: float

    def length_in(self, w_in: float, h_in: float) -> float:
        return math.hypot((self.x1 - self.x0) * w_in, (self.y1 - self.y0) * h_in)


def heavy_segments(pdf_bytes: bytes, page_index0: int, *, min_stroke_pt: float = TRACK_MIN_STROKE_PT, max_segments: int = 200_000) -> list[WeightedSegment]:
    """Straight stroked segments whose stroke width is at least
    `min_stroke_pt`, in page fractions (the 5.1 frame), read once under the
    PDFium lock. Heavy dashes are how CAD prints a lighting track or a
    demolition run; the thin ceiling grid under it is left out."""
    import ctypes

    import pypdfium2.raw as c

    from app.services import pdf_split
    from app.services.pagefacts import textlayer

    out: list[WeightedSegment] = []
    with textlayer.open_document(pdf_bytes) as doc:
        with pdf_split._PDFIUM_LOCK:
            total = len(doc)
            if not 0 <= page_index0 < total:
                raise ValueError(f"Page {page_index0 + 1} is outside the document (1-{total}).")
            page = doc[page_index0]
            try:
                frame = textlayer.page_frame(page)
                x = ctypes.c_float()
                y = ctypes.c_float()
                sw = ctypes.c_float()
                for obj in page.get_objects(max_depth=2):
                    if obj.type != c.FPDF_PAGEOBJ_PATH:
                        continue
                    if not c.FPDFPageObj_GetStrokeWidth(obj.raw, ctypes.byref(sw)):
                        continue
                    if float(sw.value) < min_stroke_pt:
                        continue
                    matrices = []
                    node = obj
                    while node is not None:
                        try:
                            matrices.append(node.get_matrix())
                        except Exception:  # noqa: BLE001
                            break
                        node = getattr(node, "container", None) if getattr(node, "level", 0) else None
                    n = int(c.FPDFPath_CountSegments(obj.raw))
                    prev: tuple[float, float] | None = None
                    for i in range(n):
                        seg = c.FPDFPath_GetPathSegment(obj.raw, i)
                        if not c.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y)):
                            continue
                        kind = int(c.FPDFPathSegment_GetType(seg))
                        px, py = float(x.value), float(y.value)
                        for m in matrices:
                            px, py = m.on_point(px, py)
                        fx, fy = frame.to_frac(px, py)
                        if kind == geometry.SEGMENT_MOVETO:
                            prev = (fx, fy)
                            continue
                        if prev is not None and kind == geometry.SEGMENT_LINETO:
                            out.append(WeightedSegment(prev[0], prev[1], fx, fy, float(sw.value)))
                        prev = (fx, fy)
                        if len(out) >= max_segments:
                            return out
            finally:
                page.close()
    return out


def merge_runs(segs: Sequence[WeightedSegment], *, w_in: float, h_in: float, gap_in: float = TRACK_GAP_IN, tol_in: float = RECT_AXIS_TOL_IN) -> list[tuple[geometry.Segment, float]]:
    """Collinear axis-aligned heavy segments merged across gaps up to
    `gap_in` (dash-dot patterns become one run), per stroke weight class
    (half-point steps) so a thin ceiling grid never joins a heavy track.
    Returns (run, stroke_pt) pairs."""
    tx, ty = tol_in / (w_in or 1.0), tol_in / (h_in or 1.0)
    by_weight: dict[float, list[WeightedSegment]] = {}
    for s in segs:
        by_weight.setdefault(round(s.stroke_pt * 2) / 2, []).append(s)
    out: list[tuple[geometry.Segment, float]] = []
    for weight, group in sorted(by_weight.items()):
        hs = [geometry.Segment(s.x0, s.y0, s.x1, s.y1) for s in group if abs(s.y1 - s.y0) <= ty and abs(s.x1 - s.x0) > tx]
        vs = [geometry.Segment(s.x0, s.y0, s.x1, s.y1) for s in group if abs(s.x1 - s.x0) <= tx and abs(s.y1 - s.y0) > ty]
        merged = geometry._merge_collinear(hs, axis="h", tol=ty, gap=gap_in / (w_in or 1.0))
        merged += geometry._merge_collinear(vs, axis="v", tol=tx, gap=gap_in / (h_in or 1.0))
        out.extend((m, weight) for m in merged)
    return out


# Rectangle candidates (linear fixtures) -----------------------------------------------------


@dataclass
class RectCandidate:
    cand_id: str
    box: list[float]  # page fractions
    long_in: float
    short_in: float
    orientation: str  # h | v

    def length_ft(self, ft_per_in: float) -> float:
        return round(self.long_in * ft_per_in, 2)

    def edge_distance_in(self, x: float, y: float, w_in: float, h_in: float) -> float:
        bx0, by0, bx1, by1 = self.box[0] * w_in, self.box[1] * h_in, self.box[2] * w_in, self.box[3] * h_in
        px, py = x * w_in, y * h_in
        dx = max(bx0 - px, 0.0, px - bx1)
        dy = max(by0 - py, 0.0, py - by1)
        return math.hypot(dx, dy)

    def contains(self, x: float, y: float) -> bool:
        return self.box[0] <= x <= self.box[2] and self.box[1] <= y <= self.box[3]

    def to_dict(self, ft_per_in: float | None = None) -> dict[str, Any]:
        return {
            "id": self.cand_id,
            "bbox": [round(v, 4) for v in self.box],
            "long_in": round(self.long_in, 3),
            "short_in": round(self.short_in, 3),
            "orientation": self.orientation,
            "length_ft": self.length_ft(ft_per_in) if ft_per_in else None,
        }


def _segments_in_box(segments: Iterable[geometry.Segment], box: Sequence[float]) -> list[geometry.Segment]:
    x0, y0, x1, y1 = box
    out = []
    for s in segments:
        if s.curve:
            continue
        if min(s.x0, s.x1) >= x0 and max(s.x0, s.x1) <= x1 and min(s.y0, s.y1) >= y0 and max(s.y0, s.y1) <= y1:
            out.append(s)
    return out


def rect_candidates(
    segments: Iterable[geometry.Segment],
    *,
    w_in: float,
    h_in: float,
    box: Sequence[float] | None = None,
    min_long_in: float = RECT_MIN_LONG_IN,
    gap_in: tuple[float, float] = RECT_GAP_IN,
    len_tol: float = RECT_LEN_TOL,
    overlap_min: float = RECT_OVERLAP_MIN,
    cap_tol: float = RECT_CAP_TOL,
) -> list[RectCandidate]:
    """Pairs of parallel segments of equal length (within `len_tol`), offset
    by `gap_in`, overlapping at least `overlap_min`, both ends closed by a
    perpendicular segment spanning the offset within `cap_tol`: the outline
    of a linear fixture."""
    segs = _segments_in_box(segments, box) if box is not None else [s for s in segments if not s.curve]
    tol_y = RECT_AXIS_TOL_IN / (h_in or 1.0)
    tol_x = RECT_AXIS_TOL_IN / (w_in or 1.0)
    # (a0, a1, line) in inches along the axis, plus the perpendicular position.
    hl = [(min(s.x0, s.x1) * w_in, max(s.x0, s.x1) * w_in, s.y0 * h_in) for s in segs if abs(s.y1 - s.y0) <= tol_y]
    vl = [(min(s.y0, s.y1) * h_in, max(s.y0, s.y1) * h_in, s.x0 * w_in) for s in segs if abs(s.x1 - s.x0) <= tol_x]

    def pairs(lines: list[tuple[float, float, float]]) -> list[tuple[float, float, float, float, float]]:
        lines = sorted((ln for ln in lines if ln[1] - ln[0] >= min_long_in), key=lambda a: a[2])
        res = []
        for i, a in enumerate(lines):
            la = a[1] - a[0]
            for b in lines[i + 1 :]:
                d = b[2] - a[2]
                if d > gap_in[1]:
                    break
                if d < gap_in[0]:
                    continue
                lb = b[1] - b[0]
                if abs(la - lb) > len_tol * max(la, lb):
                    continue
                ov = min(a[1], b[1]) - max(a[0], b[0])
                if ov < overlap_min * max(la, lb):
                    continue
                res.append((min(a[0], b[0]), a[2], max(a[1], b[1]), b[2], max(la, lb)))
        return res

    def by_position(caps: list[tuple[float, float, float]]) -> tuple[list[float], list[tuple[float, float, float]]]:
        ordered = sorted(caps, key=lambda c: c[2])
        return [c[2] for c in ordered], ordered

    def capped(r: tuple[float, float, float, float, float], caps: tuple[list[float], list[tuple[float, float, float]]]) -> bool:
        lo_a, lo_p, hi_a, hi_p, _ = r
        d = hi_p - lo_p
        keys, ordered = caps
        ok = 0
        for end in (lo_a, hi_a):
            for i in range(bisect.bisect_left(keys, end - 0.02), bisect.bisect_right(keys, end + 0.02)):
                cap = ordered[i]
                if abs(cap[2] - end) < 0.02 and abs((cap[1] - cap[0]) - d) < cap_tol * d and abs(cap[0] - lo_p) < cap_tol * d:
                    ok += 1
                    break
        return ok == 2

    v_caps, h_caps = by_position(vl), by_position(hl)
    out: list[RectCandidate] = []
    seen: set[tuple[float, float, float, float]] = set()
    for r in pairs(hl):
        if not capped(r, v_caps):
            continue
        box_f = [r[0] / w_in, r[1] / h_in, r[2] / w_in, r[3] / h_in]
        key = tuple(round(v, 4) for v in box_f)
        if key in seen:
            continue
        seen.add(key)  # type: ignore[arg-type]
        out.append(RectCandidate(f"c{len(out) + 1}", box_f, r[4], r[3] - r[1], "h"))
    for r in pairs(vl):
        if not capped(r, h_caps):
            continue
        box_f = [r[1] / w_in, r[0] / h_in, r[3] / w_in, r[2] / h_in]
        key = tuple(round(v, 4) for v in box_f)
        if key in seen:
            continue
        seen.add(key)  # type: ignore[arg-type]
        out.append(RectCandidate(f"c{len(out) + 1}", box_f, r[4], r[3] - r[1], "v"))
    return out


class _SegmentGrid:
    """Straight segments bucketed by the cell of their first endpoint (cells
    of RECT_BOX_IN sheet inches), so a box query reads a few cells instead of
    the whole page (a dense sheet holds hundreds of thousands of segments)."""

    def __init__(self, segments: Sequence[geometry.Segment], *, w_in: float, h_in: float) -> None:
        self.cw = RECT_BOX_IN[0] / (w_in or 1.0)
        self.ch = RECT_BOX_IN[1] / (h_in or 1.0)
        self.cells: dict[tuple[int, int], list[geometry.Segment]] = {}
        for s in segments:
            self.cells.setdefault((int(s.x0 // self.cw), int(s.y0 // self.ch)), []).append(s)

    def in_box(self, box: Sequence[float]) -> list[geometry.Segment]:
        """Segments whose first endpoint lies in a cell touching `box` (a
        superset of the segments fully inside it)."""
        x0, y0, x1, y1 = box
        out: list[geometry.Segment] = []
        for cx in range(int(x0 // self.cw), int(x1 // self.cw) + 1):
            for cy in range(int(y0 // self.ch), int(y1 // self.ch) + 1):
                out.extend(self.cells.get((cx, cy), ()))
        return out


def measure_linear_tag(
    tag_points: Sequence[Sequence[float]],
    segments: Iterable[geometry.Segment],
    *,
    w_in: float,
    h_in: float,
    ft_per_in: float,
    search_in: float = RECT_SEARCH_IN,
) -> dict[str, Any]:
    """For every occurrence of a linear fixture tag, the rectangle candidate
    nearest the tag (edge distance within `search_in`, never a rectangle
    that contains the tag text itself: that is the tag's own frame). The
    same candidate claimed by two tags counts once."""
    grid = _SegmentGrid([s for s in segments if not s.curve], w_in=w_in, h_in=h_in)
    instances: list[dict[str, Any]] = []
    unmatched: list[list[float]] = []
    claimed: set[tuple[float, ...]] = set()
    all_candidates: list[RectCandidate] = []
    for pt in tag_points:
        x, y = float(pt[0]), float(pt[1])
        box = (x - RECT_BOX_IN[0] / 2 / w_in, y - RECT_BOX_IN[1] / 2 / h_in, x + RECT_BOX_IN[0] / 2 / w_in, y + RECT_BOX_IN[1] / 2 / h_in)
        cands = rect_candidates(grid.in_box(box), w_in=w_in, h_in=h_in, box=box)
        all_candidates.extend(cands)
        scored = []
        for cnd in cands:
            if cnd.contains(x, y):
                continue
            d = cnd.edge_distance_in(x, y, w_in, h_in)
            if d <= search_in:
                scored.append((d, -cnd.long_in, cnd))
        scored.sort(key=lambda t: (t[0], t[1]))
        if not scored:
            unmatched.append([round(x, 4), round(y, 4)])
            continue
        best = scored[0][2]
        key = tuple(round(v, 4) for v in best.box)
        if key in claimed:
            continue
        claimed.add(key)
        instances.append(
            {
                "tag_point": [round(x, 4), round(y, 4)],
                "candidate_id": best.cand_id,
                "bbox": [round(v, 4) for v in best.box],
                "length_ft": best.length_ft(ft_per_in),
                "distance_in": round(scored[0][0], 3),
                "method": "vector_rect",
            }
        )
    total = round(sum(i["length_ft"] for i in instances), 2)
    flags = []
    if unmatched:
        flags.append(f"unmatched_tags:{len(unmatched)}")
    for inst in instances:
        if inst["length_ft"] > LINEAR_MAX_FT or inst["length_ft"] < LINEAR_MIN_FT:
            flags.append("implausible_instance")
            break
    return {"instances": instances, "total_ft": total, "unmatched": unmatched, "method": "vector_rect", "flags": flags, "candidates": len(all_candidates)}


def _track_run_for(
    x: float,
    y: float,
    runs: Sequence[tuple[geometry.Segment, float]],
    *,
    w_in: float,
    h_in: float,
    ft_per_in: float,
    search_in: float = TRACK_SEARCH_IN,
) -> tuple[geometry.Segment, float, float] | None:
    """The merged heavy run one anchor (page fractions) marks: within
    `search_in` of the run body, at least TRACK_MIN_FT long. A keynote
    leader points at the END of the item it marks, so among the candidates
    a plausible linear length (at most LINEAR_MAX_FT) wins, then the
    heaviest stroke, then the nearest endpoint. Returns (run, distance_in,
    stroke_pt) or None."""
    best: tuple[tuple[int, float, float], float, float, geometry.Segment] | None = None
    ax, ay = x * w_in, y * h_in
    for run, weight in runs:
        length_ft = run.length_in(w_in, h_in) * ft_per_in
        if length_ft < TRACK_MIN_FT:
            continue
        d = _point_run_distance_in(x, y, run, w_in, h_in)
        if d > search_in:
            continue
        x0, y0, x1, y1 = run.x0 * w_in, run.y0 * h_in, run.x1 * w_in, run.y1 * h_in
        d_end = min(math.hypot(ax - x0, ay - y0), math.hypot(ax - x1, ay - y1))
        score = (1 if length_ft > LINEAR_MAX_FT else 0, -weight, d_end)
        if best is None or score < best[0]:
            best = (score, d, weight, run)
    if best is None:
        return None
    return best[3], best[1], best[2]


def _point_run_distance_in(x: float, y: float, run: geometry.Segment, w_in: float, h_in: float) -> float:
    """Inches from a page-fraction point to the body of a run."""
    ax, ay = x * w_in, y * h_in
    x0, y0, x1, y1 = run.x0 * w_in, run.y0 * h_in, run.x1 * w_in, run.y1 * h_in
    dx, dy = x1 - x0, y1 - y0
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((ax - x0) * dx + (ay - y0) * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax - (x0 + t * dx), ay - (y0 + t * dy))


def measure_track(
    anchors: Sequence[Sequence[float]],
    heavy: Sequence[WeightedSegment],
    *,
    w_in: float,
    h_in: float,
    ft_per_in: float,
    search_in: float = TRACK_SEARCH_IN,
) -> dict[str, Any]:
    """Track rows: the merged heavy run nearest each anchor (a keynote marker
    or a legend-matched symbol point) within `search_in`, at least
    TRACK_MIN_FT long (`_track_run_for`). One run claimed by two anchors
    counts once."""
    runs = merge_runs(heavy, w_in=w_in, h_in=h_in)
    instances: list[dict[str, Any]] = []
    unmatched: list[list[float]] = []
    claimed: set[tuple[float, ...]] = set()
    for pt in anchors:
        x, y = float(pt[0]), float(pt[1])
        hit = _track_run_for(x, y, runs, w_in=w_in, h_in=h_in, ft_per_in=ft_per_in, search_in=search_in)
        if hit is None:
            unmatched.append([round(x, 4), round(y, 4)])
            continue
        run, dist, weight = hit
        key = tuple(run.to_list())
        if key in claimed:
            continue
        claimed.add(key)
        instances.append(
            {
                "anchor": [round(x, 4), round(y, 4)],
                "polyline": [[round(run.x0, 4), round(run.y0, 4)], [round(run.x1, 4), round(run.y1, 4)]],
                "length_ft": round(run.length_in(w_in, h_in) * ft_per_in, 2),
                "distance_in": round(dist, 3),
                "stroke_pt": weight,
                "method": "heavy_run",
            }
        )
    total = round(sum(i["length_ft"] for i in instances), 2)
    flags = [f"unmatched_anchors:{len(unmatched)}"] if unmatched else []
    return {"instances": instances, "total_ft": total, "unmatched": unmatched, "method": "heavy_run", "flags": flags, "runs": len(runs)}


# Frames, panels, circuits from facts --------------------------------------------------------


def sheet_key_of(doc: MeasureDocument, page: PageFacts) -> str:
    return str(page.sheet_number or f"D{doc.doc_index}P{page.page}")


def superseded_pages(documents: Sequence[MeasureDocument]) -> dict[tuple[str, int], dict[str, Any]]:
    """Pages whose sheet number a LATER document re-issues (an addendum
    E2.01): (document_id, page) -> the winner {document_id, doc_index,
    page}. The later document (higher doc_index) wins; the earlier sheet is
    superseded and never merged with it. Repeats inside one document are
    left alone."""
    winners: dict[str, tuple[int, str, int]] = {}
    pages_by_number: dict[str, list[tuple[int, str, int]]] = {}
    for doc in sorted(documents, key=lambda d: d.doc_index):
        if doc.facts.kind != "drawing":
            continue
        for page in doc.facts.pages:
            if not page.sheet_number:
                continue
            num = str(page.sheet_number)
            entry = (doc.doc_index, doc.document_id, page.page)
            pages_by_number.setdefault(num, []).append(entry)
            if num not in winners or doc.doc_index > winners[num][0]:
                winners[num] = entry
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for num, entries in pages_by_number.items():
        win = winners[num]
        for idx, doc_id, page in entries:
            if doc_id != win[1] and idx < win[0]:
                out[(doc_id, page)] = {"document_id": win[1], "doc_index": win[0], "page": win[2], "sheet_number": num}
    return out


def _is_plan(page: PageFacts) -> bool:
    return bool(set(page.roles) & PLAN_ROLES) or page.page_role in PLAN_ROLES


LIGHT_CATEGORIES: frozenset[str] = frozenset(
    {"mechanical_drawings", "plumbing_drawings", "fire_protection_drawings", "structural_drawings", "architectural_drawings", "low_voltage_drawings"}
)


def needs_vectors(page: PageFacts, panel_pages: Iterable[int] = ()) -> bool:
    """Whether a plan page gets the full scale pass (vector frames for the
    viewports, words for registration): electrical, low voltage, civil,
    general or unknown sheets, and any sheet with circuit tags or a panel
    label. Other plans (mechanical, plumbing, fire protection, structural,
    architectural, low voltage) keep the WS1 scale only; they carry no
    branch power routing, and indexing their vectors is most of the derive
    time on combined sets."""
    if any(t.get("circuits") for t in (page.tags or {}).get("circuit") or []):
        return True
    if page.page in set(panel_pages):
        return True
    return str(page.discipline_category or "") not in LIGHT_CATEGORIES


def build_frames(inputs: MeasureInputs) -> tuple[dict[str, routes_mod.SheetFrame], list[dict[str, Any]], dict[str, geometry.PageScales]]:
    """SheetFrames for every plan page plus the `scales[]` output block
    (`vectors` false on the other trades' sheets, see `needs_vectors`)."""
    frames: dict[str, routes_mod.SheetFrame] = {}
    scales_out: list[dict[str, Any]] = []
    page_scales: dict[str, geometry.PageScales] = {}
    superseded = superseded_pages(inputs.documents)
    for doc in inputs.documents:
        if doc.facts.kind != "drawing":
            continue
        panel_pages = {int(loc.get("page") or 0) for loc in doc.facts.panel_locations or []}
        for page in doc.facts.pages:
            if not _is_plan(page):
                continue
            inputs.heartbeat()
            win = superseded.get((doc.document_id, page.page))
            if win is not None:
                scales_out.append(
                    {
                        "sheet_key": f"D{doc.doc_index}P{page.page}",
                        "document_id": doc.document_id,
                        "doc_index": doc.doc_index,
                        "page": page.page,
                        "sheet_number": page.sheet_number,
                        "title": page.sheet_title,
                        "status": "superseded",
                        "superseded_by": win,
                        "ft_per_in": None,
                        "new_work": False,
                        "vectors": False,
                        "viewports": [],
                        "notes": [f"re-issued by document {win['doc_index']} page {win['page']}; the later document wins"],
                    }
                )
                continue
            full = needs_vectors(page, panel_pages)
            segs = inputs.segments(doc.document_id, page.page) if (inputs.segments and full) else None
            words = inputs.words(doc.document_id, page.page) if (inputs.words and full) else None
            try:
                ps = geometry.scales_for_page(page, segs, words=words)
            except Exception:  # noqa: BLE001 - keep the page with the WS1 primary scale
                ps = geometry.PageScales(page=page.page, sheet_number=page.sheet_number, primary_ft_per_in=page.primary_ft_per_in, nts=False, conflict=page.scale_conflict, viewports=[], reduced_print={"suspected": False, "reasons": []})
            key = sheet_key_of(doc, page)
            ft = None if ps.nts or ps.conflict else ps.primary_ft_per_in
            frame = routes_mod.SheetFrame(
                sheet_key=key,
                document_id=doc.document_id,
                page=page.page,
                w_in=float(page.w_in or 42.0),
                h_in=float(page.h_in or 30.0),
                ft_per_in=ft,
                title=page.sheet_title or (page.views[0].title if page.views else None),
                scale_at=(lambda x, y, _ps=ps: _ps.scale_at(x, y)[0]),
                new_work=circuits_mod.is_new_work_sheet(page),
            )
            frames[key] = frame
            page_scales[key] = ps
            scales_out.append(
                {
                    "sheet_key": key,
                    "document_id": doc.document_id,
                    "doc_index": doc.doc_index,
                    "page": page.page,
                    "sheet_number": page.sheet_number,
                    "title": frame.title,
                    "w_in": frame.w_in,
                    "h_in": frame.h_in,
                    "ft_per_in": ft,
                    "nts": ps.nts,
                    "conflict": ps.conflict,
                    "new_work": frame.new_work,
                    "vectors": full,
                    "status": "nts" if ps.nts else "conflict" if ps.conflict else "selected" if ft else "missing",
                    "reduced_print": ps.reduced_print.get("suspected", False),
                    "viewports": [vp.to_dict() for vp in ps.viewports],
                    "notes": list(ps.notes),
                }
            )
    return frames, scales_out, page_scales


def frames_from_output(measure: Mapping[str, Any]) -> dict[str, routes_mod.SheetFrame]:
    """SheetFrames rebuilt from a stored measure output's `scales[]` (no
    facts needed; the viewport regions give `scale_at`)."""
    frames: dict[str, routes_mod.SheetFrame] = {}
    for s in measure.get("scales") or []:
        if s.get("status") == "superseded":
            continue
        vps = [vp for vp in (s.get("viewports") or []) if vp.get("region") and vp.get("ft_per_in") is not None and not vp.get("skipped")]

        def _scale_at(x: float, y: float, _vps: list[dict[str, Any]] = vps, _ft: float | None = s.get("ft_per_in")) -> float | None:
            hits = [vp for vp in _vps if vp["region"][0] <= x <= vp["region"][2] and vp["region"][1] <= y <= vp["region"][3]]
            if hits:
                best = min(hits, key=lambda vp: (vp["region"][2] - vp["region"][0]) * (vp["region"][3] - vp["region"][1]))
                return float(best["ft_per_in"])
            return _ft

        frames[str(s["sheet_key"])] = routes_mod.SheetFrame(
            sheet_key=str(s["sheet_key"]),
            document_id=str(s.get("document_id")),
            page=int(s.get("page") or 0),
            w_in=float(s.get("w_in") or 42.0),
            h_in=float(s.get("h_in") or 30.0),
            ft_per_in=s.get("ft_per_in"),
            title=s.get("title"),
            scale_at=_scale_at,
            new_work=bool(s.get("new_work", True)),
        )
    return frames


def panel_schedules(inputs: MeasureInputs) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The panel schedules a run routes from (`plans_reader_circuits.
    merge_panel_schedules`): the usable WS1 panel schedules of pages that
    are not superseded, plus the reader's panel schedule rows (category
    output `schedules[]`, schedule_type panel) converted to the same shape
    when the text-layer parser missed or mangled the panel. Returns
    (schedules, info)."""
    dropped = superseded_pages(inputs.documents)
    ws1: list[Mapping[str, Any]] = []
    for doc in inputs.documents:
        ws1.extend(sch for sch in doc.facts.schedules or [] if (doc.document_id, int(sch.get("page") or 0)) not in dropped)
    model: list[Mapping[str, Any]] = []
    for out in inputs.category_outputs.values():
        if not isinstance(out, Mapping):
            continue
        for sch in out.get("schedules") or []:
            if not isinstance(sch, Mapping) or str(sch.get("schedule_type") or "").lower() != "panel":
                continue
            sheet = sch.get("sheet") if isinstance(sch.get("sheet"), Mapping) else {}
            doc_id = sch.get("document_id") or sheet.get("document_id")
            page = sch.get("page") or sheet.get("page")
            if doc_id and page and (str(doc_id), int(page)) in dropped:
                continue
            model.append(sch)
    return circuits_mod.merge_panel_schedules(ws1, model)


def build_panels(inputs: MeasureInputs, frames: Mapping[str, routes_mod.SheetFrame], schedules: Sequence[Mapping[str, Any]] | None = None) -> dict[str, routes_mod.PanelInfo]:
    """Panels from the panel schedules (name, volts, phase; `panel_schedules`
    when `schedules` is None) and plan labels (position on the sheet with
    the smallest ft_per_in that registers, in practice the overall plan),
    then a reader count row that places the panel ("PANEL A LOCATION" with
    one position on a new-work plan, confidence 0.5), then the
    `set_panel_location` overrides."""
    if schedules is None:
        schedules, _info = panel_schedules(inputs)
    panels: dict[str, routes_mod.PanelInfo] = {}
    for sch in schedules:
        p = sch.get("panel") or {}
        name = str(p.get("name") or "").upper()
        if not name:
            continue
        panels.setdefault(name, routes_mod.PanelInfo(name=name, volts=p.get("volts"), phase=p.get("phase"), existing=p.get("existing"), schedule_id=sch.get("schedule_id")))
    page_keys: dict[tuple[str, int], str] = {(f.document_id, f.page): k for k, f in frames.items()}
    for doc in inputs.documents:
        for loc in doc.facts.panel_locations or []:
            name = str(loc.get("panel") or "").upper()
            if not name or loc.get("x") is None:
                continue
            key = page_keys.get((doc.document_id, int(loc.get("page") or 0)))
            if key is None:
                continue
            info = panels.get(name)
            if info is None:
                info = routes_mod.PanelInfo(name=name, existing=loc.get("existing"))
                panels[name] = info
            if info.located and info.confidence >= 0.7:
                continue
            frame = frames[key]
            info.sheet_key = key
            info.document_id = doc.document_id
            info.page = frame.page
            info.x = float(loc["x"])
            info.y = float(loc["y"])
            info.method = str(loc.get("source") or "plan_label")
            info.confidence = 0.7
    for row in _count_rows(inputs):
        m = PANEL_ROW_RE.match(str(row.get("description") or ""))
        if not m:
            continue
        name = m.group("name").upper()
        info = panels.get(name)
        if info is None or info.located:
            continue
        positions = _row_positions(row)
        key = _row_sheet_key(row, frames)
        if len(positions) != 1 or key is None or not frames[key].new_work:
            continue
        frame = frames[key]
        info.sheet_key = key
        info.document_id = frame.document_id
        info.page = frame.page
        info.x, info.y = float(positions[0][0]), float(positions[0][1])
        info.method = "count_row"
        info.confidence = PANEL_LOCATION_CONFIDENCE
    apply_panel_overrides(panels, inputs.overrides, frames)
    return panels


def apply_panel_overrides(panels: dict[str, routes_mod.PanelInfo], overrides: Iterable[Mapping[str, Any]], frames: Mapping[str, routes_mod.SheetFrame]) -> None:
    """`set_panel_location` overrides, human beats model (a model override
    never replaces a human one; the latest of the same kind wins)."""
    page_keys: dict[tuple[str, int], str] = {(f.document_id, f.page): k for k, f in frames.items()}
    applied: dict[str, str] = {}
    for ov in overrides:
        if ov.get("tool") != "set_panel_location":
            continue
        args = ov.get("args") or {}
        name = str(args.get("panel") or "").upper()
        by = str(ov.get("by") or "model")
        if not name:
            continue
        if applied.get(name) == "human" and by != "human":
            continue
        key = page_keys.get((str(args.get("document_id")), int(args.get("page") or 0)))
        if key is None:
            continue
        pt = args.get("point") or [None, None]
        info = panels.get(name) or routes_mod.PanelInfo(name=name)
        panels[name] = info
        info.sheet_key = key
        info.document_id = str(args.get("document_id"))
        info.page = int(args.get("page") or 0)
        info.x, info.y = float(pt[0]), float(pt[1])
        info.method = by
        info.confidence = 1.0 if by == "human" else 0.8
        applied[name] = by


def _snap_devices(page: PageFacts, tags: list[dict[str, Any]], segments: geometry.SegmentIndex | None) -> list[dict[str, Any]]:
    """Stacked labels and home-run arrows snap to the nearest device symbol
    or census point; tag-per-device sheets use the tag positions."""
    conv = circuits_mod.classify_tag_convention(page)
    if conv.convention == "tag_per_device" or not tags:
        return [dict(t) for t in tags]
    devices = circuits_mod.device_points(page)
    if not devices:
        return [dict(t) for t in tags]
    snapped = circuits_mod.snap_tags(tags, devices, w_in=page.w_in, h_in=page.h_in, segments=segments)
    out = []
    for s in snapped:
        t = dict(s.tag)
        t["x"], t["y"] = s.x, s.y
        t["snapped_to"] = s.snapped_to
        out.append(t)
    return out


def build_circuits(inputs: MeasureInputs, frames: Mapping[str, routes_mod.SheetFrame], schedules: Sequence[Mapping[str, Any]] | None = None) -> tuple[list[routes_mod.CircuitSpec], dict[str, Any]]:
    """CircuitSpecs from the WS3 wiring plans with their devices (one per
    tag per circuit on new-work plan sheets). `schedules` are the merged
    panel schedules (`panel_schedules` when None). Returns (circuits, stats)."""
    pages: list[PageFacts] = []
    dropped = superseded_pages(inputs.documents)
    if schedules is None:
        schedules, _info = panel_schedules(inputs)
    for doc in inputs.documents:
        pages.extend(p for p in doc.facts.pages if (doc.document_id, p.page) not in dropped)
    plans = circuits_mod.wiring_plans(schedules, pages)
    devices_by_circuit: dict[str, list[routes_mod.Device]] = {}
    tags_total = 0
    tagged_sheets: list[str] = []
    superseded = superseded_pages(inputs.documents)
    for doc in inputs.documents:
        for page in doc.facts.pages:
            if not _is_plan(page) or not circuits_mod.is_new_work_sheet(page) or (doc.document_id, page.page) in superseded:
                continue
            key = sheet_key_of(doc, page)
            tags = [t for t in (page.tags or {}).get("circuit") or [] if t.get("circuits")]
            if not tags:
                continue
            tagged_sheets.append(key)
            segs = inputs.segments(doc.document_id, page.page) if inputs.segments else None
            snapped = _snap_devices(page, tags, segs)
            seen: set[tuple[str, float, float]] = set()
            for t in snapped:
                x, y = float(t.get("x", 0.0)), float(t.get("y", 0.0))
                for ckt in t.get("circuits") or []:
                    ck = circuits_mod.circuit_key(t.get("panel", "?"), ckt)
                    pos_key = (ck, round(x * page.w_in / circuits_mod.SAME_POSITION_IN), round(y * page.h_in / circuits_mod.SAME_POSITION_IN))
                    if pos_key in seen:
                        continue
                    seen.add(pos_key)
                    tags_total += 1
                    devices_by_circuit.setdefault(ck, []).append(
                        routes_mod.Device(document_id=doc.document_id, page=page.page, sheet_key=key, x=x, y=y, tag=str(t.get("raw") or ck), source="text_tag", fixture_type=t.get("fixture_type"))
                    )
    specs: list[routes_mod.CircuitSpec] = []
    seen_keys: set[str] = set()
    for plan in plans:
        devs = devices_by_circuit.get(plan.circuit, [])
        seen_keys.add(plan.circuit)
        specs.append(routes_mod.CircuitSpec.from_wiring_plan(plan, devs))
    stats = {"tags": tags_total, "circuits": len(specs), "tagged_sheets": sorted(set(tagged_sheets)), "with_devices": sum(1 for s in specs if s.devices)}
    return specs, stats


def pages_by_key(inputs: MeasureInputs) -> dict[str, PageFacts]:
    """PageFacts by sheet key, superseded re-issues left out."""
    dropped = superseded_pages(inputs.documents)
    out: dict[str, PageFacts] = {}
    for doc in inputs.documents:
        for page in doc.facts.pages:
            if (doc.document_id, page.page) not in dropped:
                out[sheet_key_of(doc, page)] = page
    return out


def _tokens_for(inputs: MeasureInputs, frame: routes_mod.SheetFrame, page_by_key: Mapping[str, PageFacts]) -> geometry.SheetTokens | None:
    if inputs.words is None:
        return None
    words = inputs.words(frame.document_id, frame.page)
    page = page_by_key.get(frame.sheet_key)
    if words is None or page is None:
        return None
    return geometry.sheet_tokens(page, words, sheet_key=frame.sheet_key, ft_per_in=frame.ft_per_in)


def build_registrations(
    inputs: MeasureInputs,
    frames: Mapping[str, routes_mod.SheetFrame],
    device_sheets: Iterable[str],
    panel_sheets: Iterable[str],
    *,
    light: Iterable[str] = (),
    findings: list[dict[str, Any]] | None = None,
) -> routes_mod.RegistrationGraph:
    """Registrations between every device sheet and every panel sheet,
    directly and through identity frames with the other plan sheets of the
    same scale (7154: E2.01 -> ED2.01 identity, ED2.01 -> E1.00 single
    anchor). Precomputed registrations (tests, stored outputs) are used as
    given. A registration whose callout check failed (`callout_mismatch`)
    never drives routes: it is dropped with a
    `registration_callout_mismatch` finding (the routes of that sheet fall
    back to the allowance basis)."""
    sink = findings if findings is not None else []

    def usable(reg: geometry.Registration) -> bool:
        if "callout_mismatch" not in (reg.flags or []):
            return True
        sink.append(
            {
                "code": "registration_callout_mismatch",
                "severity": "warn",
                "message": f"{reg.src} -> {reg.dst}: {reg.method} registration dropped; the mapped content does not fall inside a callout rectangle of {reg.dst}",
                "refs": [reg.src, reg.dst],
            }
        )
        return False

    graph = routes_mod.RegistrationGraph([r for r in inputs.registrations or [] if usable(r)])
    if inputs.registrations is not None or inputs.words is None:
        return graph
    page_by_key = pages_by_key(inputs)
    tokens: dict[str, geometry.SheetTokens | None] = {}

    def tok(key: str) -> geometry.SheetTokens | None:
        if key not in tokens:
            fr = frames.get(key)
            tokens[key] = _tokens_for(inputs, fr, page_by_key) if fr is not None else None
            inputs.heartbeat()
        return tokens[key]

    rect_cache: dict[str, list[list[float]] | None] = {}

    def rects(key: str) -> list[list[float]] | None:
        if key not in rect_cache:
            fr = frames.get(key)
            segs = inputs.segments(fr.document_id, fr.page) if (fr is not None and inputs.segments) else None
            rect_cache[key] = segs.rectangles(min_w_in=3.0, min_h_in=3.0) if segs is not None else None
        return rect_cache[key]

    dev = [k for k in dict.fromkeys(device_sheets) if k in frames]
    pan = [k for k in dict.fromkeys(panel_sheets) if k in frames]
    pairs = 0
    done: set[tuple[str, str]] = set()
    # Same-scale identity frames among the plan sheets (cheap, enable chains).
    skip = set(light)
    plan_keys = sorted(k for k in frames if k not in skip)
    for i, a in enumerate(plan_keys):
        for b in plan_keys[i + 1 :]:
            fa, fb = frames[a], frames[b]
            if fa.document_id != fb.document_id or fa.ft_per_in is None or fa.ft_per_in != fb.ft_per_in:
                continue
            if pairs >= inputs.max_registration_pairs:
                break
            ta, tb = tok(a), tok(b)
            if ta is None or tb is None:
                continue
            pairs += 1
            done.add((a, b))
            reg = geometry.register_sheets(ta, tb, allow_single_anchor=False)
            if reg.ok and reg.method == "identity_frame":
                graph.add(reg)
    for d in dev:
        for p in pan:
            if d == p or (d, p) in done or (p, d) in done:
                continue
            if graph.between(d, p) is not None:
                continue
            if pairs >= inputs.max_registration_pairs:
                break
            td, tp = tok(d), tok(p)
            if td is None or tp is None:
                continue
            pairs += 1
            done.add((d, p))
            reg = geometry.register_sheets(td, tp, dst_rectangles=rects(p))
            if reg.ok and usable(reg):
                graph.add(reg)
                continue
            # Through an identity sibling of the device sheet.
            for sib in plan_keys:
                if sib in (d, p) or graph.between(d, sib) is None:
                    continue
                ts = tok(sib)
                if ts is None or pairs >= inputs.max_registration_pairs:
                    continue
                pairs += 1
                reg2 = geometry.register_sheets(ts, tp, dst_rectangles=rects(p))
                if reg2.ok and usable(reg2):
                    graph.add(reg2)
                    break
    return graph


def registrations_from_output(measure: Mapping[str, Any]) -> list[geometry.Registration]:
    out = []
    for r in measure.get("registrations") or []:
        t = r.get("transform")
        if not t:
            continue
        out.append(
            geometry.Registration(
                src=str(r.get("src")),
                dst=str(r.get("dst")),
                transform=geometry.Transform(k=float(t.get("k", 1.0)), rot=int(t.get("rot", 0)), mirror=bool(t.get("mirror")), tx=float(t.get("tx_in", 0.0)), ty=float(t.get("ty_in", 0.0))),
                method=str(r.get("method") or "stored"),
                confidence=float(r.get("confidence") or 0.0),
                flags=list(r.get("flags") or []),
                chain=list(r.get("chain") or []),
            )
        )
    return out


# Count rows (lower tiers, linear items) ----------------------------------------------------------


def _row_positions(row: Mapping[str, Any]) -> list[list[float]]:
    pts = row.get("positions") or row.get("points")
    out: list[list[float]] = []
    if isinstance(pts, list):
        for p in pts:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                out.append([float(p[0]), float(p[1])])
    if not out:
        for m in POINT_RE.finditer(str(row.get("location") or "")):
            out.append([float(m.group(1)), float(m.group(2))])
    return out


def _count_rows(inputs: MeasureInputs) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cat, out in inputs.category_outputs.items():
        if not isinstance(out, Mapping):
            continue
        for c in out.get("counts") or []:
            if isinstance(c, Mapping) and str(c.get("status") or "active") == "active":
                r = dict(c)
                r.setdefault("category", cat)
                rows.append(r)
    return rows


def _row_sheet_key(row: Mapping[str, Any], frames: Mapping[str, routes_mod.SheetFrame]) -> str | None:
    sheet = row.get("sheet") if isinstance(row.get("sheet"), Mapping) else None
    doc_id = row.get("document_id") or (sheet or {}).get("document_id")
    for key in (str((sheet or {}).get("sheet_key") or ""), str((sheet or {}).get("sheet_number") or row.get("sheet_number") or "")):
        if key and key in frames:
            if doc_id and str(doc_id) != frames[key].document_id:
                return None  # a superseded re-issue of the sheet: the later document wins
            return key
    doc_id, page = row.get("document_id"), row.get("page")
    for key, fr in frames.items():
        if fr.document_id == doc_id and fr.page == page:
            return key
    return None


def _is_demolition_row(row: Mapping[str, Any]) -> bool:
    desc = str(row.get("description") or "").upper()
    return bool(re.search(r"REMOV|DEMO|EXISTING\b.*\bTO BE REMOVED|\(X\)", desc)) or str(row.get("material_category_hint") or "").lower() == "demolition"


def _is_existing_row(row: Mapping[str, Any]) -> bool:
    """Existing equipment noted on a plan (to remain, (E)): never a new device."""
    desc = str(row.get("description") or "").upper()
    return bool(re.search(r"\bEXISTING\b|\(E\)|TO REMAIN", desc)) and not re.search(r"\bNEW\b", desc)


NEW_DEVICE_CLASSES: frozenset[str] = frozenset({"receptacle", "luminaire", "luminaire_linear", "switch", "control", "equipment_connection", "motor_connection", "device"})


def new_device_count(inputs: MeasureInputs, frames: Mapping[str, routes_mod.SheetFrame], *, connections: bool = True) -> int:
    """New devices from the count rows: EA rows on new-work sheets that are
    not demolition (receptacles, luminaires, controls, connections).
    `connections=False` leaves out the equipment connection rows (class or
    CONNECTION_RE wording, every trade): the equipment tier routes those,
    so the per-device band never takes them a second time."""
    if inputs.new_device_count is not None:
        return int(inputs.new_device_count)
    n = 0
    for row in _count_rows(inputs):
        if str(row.get("unit") or "EA").upper() != "EA" or _is_demolition_row(row) or _is_existing_row(row):
            continue
        key = _row_sheet_key(row, frames)
        if key is None or not frames[key].new_work:
            continue
        identity = parse_item(str(row.get("description") or ""), "EA")
        if not connections and (identity.item_class in CONNECTION_CLASSES or CONNECTION_RE.search(str(row.get("description") or ""))):
            continue
        if identity.item_class in NEW_DEVICE_CLASSES:
            try:
                n += int(round(float(row.get("quantity") or 0)))
            except (TypeError, ValueError):
                pass
    return n


def _device_kind(item_class: str) -> str:
    """lighting | equipment | power for a count-row item class."""
    if item_class in LIGHTING_DEVICE_CLASSES:
        return "lighting"
    if item_class in CONNECTION_CLASSES:
        return "equipment"
    return "power"


def untagged_devices(
    inputs: MeasureInputs,
    frames: Mapping[str, routes_mod.SheetFrame],
    tagged_sheets: Iterable[str],
    count_rows: Sequence[Mapping[str, Any]],
    page_by_key: Mapping[str, PageFacts],
) -> dict[str, list[routes_mod.Device]]:
    """Positioned devices on new-work plan sheets without circuit tags, by
    load kind: `lighting` (WS1 type marks; luminaire, switch and control
    count rows) and `power` (WS1 N and R status tags; receptacle and other
    device count rows). A sheet uses its count-row positions only when WS1
    found no census point on it. Equipment connections are not here (the
    equipment tier takes them)."""
    tagged = set(tagged_sheets)
    out: dict[str, list[routes_mod.Device]] = {"lighting": [], "power": []}
    for key, frame in frames.items():
        if not frame.new_work or key in tagged:
            continue
        page = page_by_key.get(key)
        if page is None:
            continue
        tags = page.tags or {}
        found = False
        for fam, pts in (tags.get("type_marks") or {}).items():
            for p in pts:
                out["lighting"].append(routes_mod.Device(frame.document_id, frame.page, key, float(p[0]), float(p[1]), tag=str(fam), source="census", fixture_type=str(fam)))
                found = True
        for status, pts in (tags.get("status") or {}).items():
            if str(status).upper() in ("N", "NEW", "R"):
                for p in pts:
                    out["power"].append(routes_mod.Device(frame.document_id, frame.page, key, float(p[0]), float(p[1]), tag=str(status), source="census"))
                    found = True
        if found:
            continue
        for row in count_rows:
            if _row_sheet_key(row, frames) != key or _is_demolition_row(row) or _is_existing_row(row):
                continue
            desc = str(row.get("description") or "")
            if CONNECTION_RE.search(desc):
                continue
            item_class = parse_item(desc, "EA").item_class
            if item_class not in NEW_DEVICE_CLASSES or item_class in CONNECTION_CLASSES:
                continue
            kind = _device_kind(item_class)
            for x, y in _row_positions(row):
                out[kind].append(routes_mod.Device(frame.document_id, frame.page, key, x, y, tag=str(row.get("row_id") or ""), source="count_row", fixture_type=None))
    return out


def equipment_connections(
    count_rows: Sequence[Mapping[str, Any]],
    frames: Mapping[str, routes_mod.SheetFrame],
    tagged_sheets: Iterable[str],
    findings: list[dict[str, Any]],
) -> tuple[list[routes_mod.Device], list[str]]:
    """Equipment connections (count rows matching `CONNECTION_RE`, EA, not
    demolition) on new-work sheets without circuit tags: one device per
    position, or per unit at the sheet centre when the row has no
    positions. When an electrical category carries connection rows the
    other trades' connection rows are skipped (the mechanical plan repeats
    the same units; the dedupe across trades is WS4's)."""
    tagged = set(tagged_sheets)
    rows = [r for r in count_rows if CONNECTION_RE.search(str(r.get("description") or "")) and not _is_demolition_row(r) and str(r.get("unit") or "EA").upper() == "EA"]
    electrical = [r for r in rows if "electrical" in str(r.get("category") or "").lower()]
    skipped = [r for r in rows if r not in electrical]
    if electrical and skipped:
        rows = electrical
        units = 0
        for r in skipped:
            try:
                units += int(round(float(r.get("quantity") or 0)))
            except (TypeError, ValueError):
                pass
        findings.append({"code": "equipment_rows_other_trades_skipped", "severity": "info", "message": f"{len(skipped)} connection row(s) ({units} connections) from other trades' drawings skipped; the electrical drawings carry the connections", "refs": [str(r.get("row_id") or "") for r in skipped]})
    devices: list[routes_mod.Device] = []
    row_ids: list[str] = []
    for row in rows:
        key = _row_sheet_key(row, frames)
        if key is None or not frames[key].new_work or key in tagged:
            continue
        frame = frames[key]
        desc = str(row.get("description") or "")
        positions = _row_positions(row)
        if positions:
            for x, y in positions:
                devices.append(routes_mod.Device(frame.document_id, frame.page, key, x, y, tag=desc[:40], source="equipment"))
                row_ids.append(str(row.get("row_id") or ""))
            continue
        try:
            qty = int(round(float(row.get("quantity") or 0)))
        except (TypeError, ValueError):
            qty = 0
        for _ in range(qty):
            devices.append(routes_mod.Device(frame.document_id, frame.page, key, 0.5, 0.5, tag=desc[:40], source="equipment"))
            row_ids.append(str(row.get("row_id") or ""))
    return devices, row_ids


# Build -------------------------------------------------------------------------------------------


def _ft(x: float) -> float:
    return round(float(x), 2)


def build_measure(inputs: MeasureInputs, conv: routes_mod.Conventions | None = None) -> dict[str, Any]:
    """The MeasureOutput v1 of spec 5.4 for one run. Pure over `inputs` (and
    the active bands file, read once per build: LOPO on
    `inputs.eval_case_id`); `inputs.heartbeat()` is called per sheet and per
    linear item. Ids persist from `inputs.prior` (stabilize_ids)."""
    bandset = measure_bands.load_branch_bands(case_id=inputs.eval_case_id)
    with measure_bands.use(bandset):
        out = _build_measure(inputs, conv, bandset)
    return stabilize_ids(out, inputs.prior)


def _build_measure(inputs: MeasureInputs, conv: routes_mod.Conventions | None, bandset: measure_bands.BranchBandSet) -> dict[str, Any]:
    started = time.monotonic()
    conv = (conv or routes_mod.Conventions.from_settings()).replace(wiring_method=inputs.scope.wiring_method)
    findings: list[dict[str, Any]] = []
    judgments: list[dict[str, Any]] = []
    frames, scales_out, page_scales = build_frames(inputs)
    inputs.heartbeat()
    schedules, schedule_info = panel_schedules(inputs)
    if schedule_info.get("dropped_ws1"):
        findings.append({"code": "panel_schedule_unusable", "severity": "info", "message": f"WS1 panel schedule(s) {', '.join(schedule_info['dropped_ws1'])} dropped: no distinct circuits with a load, trip or pole count (a title matched in running text)", "refs": list(schedule_info["dropped_ws1"])})
    if schedule_info.get("from_model_rows"):
        findings.append({"code": "panel_schedule_from_model_rows", "severity": "info", "message": f"panel(s) {', '.join(schedule_info['from_model_rows'])}: circuits taken from the reader's schedule rows; the text-layer parser found no usable schedule for them", "refs": [str(sch.get("schedule_id") or "") for sch in schedules if sch.get("source") == "model_rows"]})
    panels = build_panels(inputs, frames, schedules)
    circuits, cstats = build_circuits(inputs, frames, schedules)
    inputs.heartbeat()
    for name, p in sorted(panels.items()):
        if p.located and p.method == "count_row":
            findings.append({"code": "panel_location_from_count_row", "severity": "info", "message": f"panel {name} placed from the reader's location row on {p.sheet_key} (confidence {p.confidence:g}); set_panel_location overrides it", "refs": [name]})
    # Feeders (a transformer or panel feed, a breaker of 70 A or more) are
    # never branch routes: the measure writes branch wire and conduit only.
    feeders = [c for c in circuits if c.wiring == "new" and not c.devices and c.load_kind == "feeder"]
    for c in feeders:
        c.wiring = "none"
        c.reasons.append("feeder breaker; not a branch circuit")
    if feeders:
        findings.append({"code": "feeder_circuits_excluded", "severity": "info", "message": f"{len(feeders)} schedule circuit(s) feed a transformer or panel; no branch route written for them", "refs": [c.circuit_id for c in feeders]})
    # Devices without circuit tags, collected before the routes: on an
    # untagged project (no circuit tag on any plan) they are distributed over
    # the schedule circuits by load kind (WS3: untagged projects never empty).
    count_rows = _count_rows(inputs)
    page_by_key = pages_by_key(inputs)
    untagged_project = cstats["tags"] == 0
    untagged = untagged_devices(inputs, frames, cstats["tagged_sheets"], count_rows, page_by_key)
    eq_devices, eq_rows = equipment_connections(count_rows, frames, cstats["tagged_sheets"], findings)
    distributed: dict[str, list[str]] = {}
    if untagged_project and circuits:
        for kind, key in (("lighting", "lighting"), ("receptacle", "power")):
            untagged[key], ids = routes_mod.distribute_devices(circuits, untagged[key], frames, kind=kind)
            if ids:
                distributed[kind] = ids
        eq_devices, ids = routes_mod.distribute_devices(circuits, eq_devices, frames, kind="equipment")
        if ids:
            distributed["equipment"] = ids
    device_sheets = sorted({d.sheet_key for c in circuits for d in c.devices})
    panel_sheets = sorted({p.sheet_key for p in panels.values() if p.located and p.sheet_key})
    graph = build_registrations(inputs, frames, device_sheets, panel_sheets, light={s["sheet_key"] for s in scales_out if not s.get("vectors", True)}, findings=findings)
    inputs.heartbeat()
    for name, p in sorted(panels.items()):
        if not p.located:
            judgments.append({"id": f"J{len(judgments) + 1}", "kind": "panel_location", "refs": {"panel": name}, "default_applied": "home-run allowance", "resolved_by": None})
    # Tier 1 and 2: tagged circuits (and the distributed schedule circuits).
    routes = routes_mod.synthesize_routes(circuits, panels, frames, graph, conv)
    routes_mod.mark_distributed_routes(routes, circuits)
    inputs.heartbeat()
    seq = len(routes)
    if distributed:
        parts = [f"{sum(len(c.devices) for c in circuits if c.circuit_id in ids)} {kind} devices over {len(ids)} circuits" for kind, ids in distributed.items()]
        findings.append({"code": "untagged_schedule_distribution", "severity": "info", "message": "no circuit tags on the plans; devices distributed over the panel schedule circuits by load kind: " + "; ".join(parts), "refs": [cid for ids in distributed.values() for cid in ids]})
    routed_devices: dict[str, list[tuple[float, float]]] = {}
    for r in routes:
        for d in r.devices:
            routed_devices.setdefault(d.sheet_key, []).append((d.x, d.y))
    # Equipment-connection tier. On a tagged sheet the connections are fed
    # from the tagged circuits (7154: dampers and VAVs on E2.01 sit inside
    # the tagged circuits); an uncovered position is noted, never given a
    # second home run. On untagged sheets every connection the schedule
    # circuits did not absorb gets its own allowance.
    for row in count_rows:
        desc = str(row.get("description") or "")
        if not CONNECTION_RE.search(desc) or _is_demolition_row(row) or str(row.get("unit") or "EA").upper() != "EA":
            continue
        key = _row_sheet_key(row, frames)
        if key is None or not frames[key].new_work or key not in cstats["tagged_sheets"]:
            continue
        frame = frames[key]
        positions = _row_positions(row)
        far = [p for p in positions if not any(math.hypot((rx - p[0]) * frame.w_in, (ry - p[1]) * frame.h_in) * (frame.scale_for(p[0], p[1]) or frame.ft_per_in or 4.0) <= EQUIPMENT_COVER_FT for rx, ry in routed_devices.get(key, []))]
        if far:
            findings.append({"code": "equipment_in_tagged_circuits", "severity": "info", "message": f"{desc[:60]}: {len(far)} of {len(positions)} connections are more than {EQUIPMENT_COVER_FT:g} ft from a tagged device on {key}; fed from the sheet's tagged circuits", "refs": [str(row.get("row_id") or "")]})
    if eq_devices:
        eq = routes_mod.equipment_routes(eq_devices, panels, frames, conv, route_seq=seq)
        routes.extend(eq)
        seq += len(eq)
    # Untagged tier: devices no schedule circuit took, as synthetic circuits.
    if untagged["power"]:
        ur = routes_mod.untagged_routes(untagged["power"], panels, frames, graph, conv, route_seq=seq)
        routes.extend(ur)
        seq += len(ur)
    if untagged["lighting"]:
        ul = routes_mod.untagged_routes(untagged["lighting"], panels, frames, graph, conv, route_seq=seq, lighting=True)
        routes.extend(ul)
        seq += len(ul)
    open_circuits = [r for r in routes if r.tier == "tagged" and "no_devices" in r.flags]
    if (untagged["power"] or untagged["lighting"]) and not distributed:
        # The positioned devices stand for the schedule's new loads: a
        # schedule circuit without any tag keeps no second allowance.
        routes = [r for r in routes if r not in open_circuits]
        if open_circuits:
            findings.append({"code": "schedule_circuits_without_tags", "severity": "info", "message": f"{len(open_circuits)} schedule circuits with new loads and no tag were covered by the positioned devices", "refs": [r.circuit_id for r in open_circuits]})
    elif distributed and open_circuits:
        # Schedule circuits no positioned device reached stand for the loads
        # the reader counted without a position: their allowances stay.
        findings.append({"code": "schedule_circuits_open", "severity": "info", "message": f"{len(open_circuits)} schedule circuits with new loads received no positioned device; each keeps its home-run allowance", "refs": [r.circuit_id for r in open_circuits]})
    # Density tier: nothing positioned at all.
    devices_new = new_device_count(inputs, frames)
    # Devices the routes cover, in the units of `devices_new` (equipment
    # connections are their own tier, not per-device band devices).
    routed_count = sum(1 for r in routes for d in r.devices if d.source != "equipment")
    if not routes and devices_new > 0:
        dr = routes_mod.density_routes(devices_new, conv, project_type=inputs.project_type, route_seq=seq)
        routes.extend(dr)
        seq += len(dr)
        findings.append({"code": "branch_density_only", "severity": "warn", "message": f"no device positions or circuit tags; branch quantities are {devices_new} devices x the history band median", "refs": [r.route_id for r in dr]})
    elif untagged_project and not circuits and (band_devices := new_device_count(inputs, frames, connections=False)) > routed_count:
        # Untagged, no panel schedule: the devices no route covers (counted
        # without a position) take the band medians per device. Equipment
        # connections are the equipment tier's (and other trades' repeats of
        # them are skipped there), so they never enter the band count.
        uncovered = band_devices - routed_count
        dr = routes_mod.density_routes(uncovered, conv, project_type=inputs.project_type, route_seq=seq, circuit_id="BAND", flag="untagged_device_band")
        routes.extend(dr)
        seq += len(dr)
        findings.append({"code": "untagged_device_band", "severity": "warn", "message": f"no circuit tags and no panel schedule; {uncovered} of {band_devices} new devices outside the equipment tier have no drawn position, their branch wire and conduit are the history band medians per device ({dr[0].sizing['band']['band_version'] if dr and dr[0].sizing else 'constants'})", "refs": [r.route_id for r in dr]})
    for r in routes:
        if "homerun_allowance" in r.flags:
            findings.append({"code": "homerun_allowance", "severity": "warn", "message": f"{r.circuit_id}: home run is the {conv.homerun_default_ft:g} ft allowance ({', '.join(f for f in r.flags if f != 'homerun_allowance') or 'panel not located'})", "refs": [r.route_id]})
        if "circuit_split_unregistered" in r.flags:
            findings.append({"code": "circuit_split_unregistered", "severity": "warn", "message": f"{r.circuit_id}: devices on sheets that do not register; one home run per sheet", "refs": [r.route_id]})
    quantities = routes_mod.branch_quantities(routes, conv)
    mseq = len(quantities)
    # Linear items (L5 and friends) and the demolition track.
    linear_items: list[dict[str, Any]] = []
    linear_items.extend(_linear_fixtures(inputs, frames, page_by_key, judgments))
    linear_items.extend(_linear_demolition(inputs, frames, count_rows, findings, judgments))
    apply_linear_overrides(linear_items, inputs.overrides, frames)
    for item in linear_items:
        if item.get("total_ft") and item["total_ft"] > 0:
            mseq += 1
            quantities.append(
                {
                    "row_id": make_row_id("M", mseq),
                    "group": item["group"],
                    "subgroup": item["subgroup"],
                    "description": item["description"],
                    "quantity": round(float(item["total_ft"]), 2),
                    "unit": "FT",
                    "wastage_applies": False,
                    "basis": item["basis"],
                    "caveat": None if item["basis"] == "measured_route" else ASSUMED_CAVEAT,
                    "sheet_refs": [item["sheet_key"]],
                    "refs": [f"m:{item['measure_id']}"],
                    "derivation": linear_derivation(item),
                    "line_hint": {"area": item["area"], "group": item["group"], "subgroup": item["subgroup"], "tag": item.get("tag"), "replaces_unit": "EA", "kind": item["kind"]},
                    "material_category": item.get("material_category"),
                }
            )
            item["row_id"] = quantities[-1]["row_id"]
    # Measured runs (RUN_ITEM route polylines).
    measured_runs: list[dict[str, Any]] = []
    for cat, out in inputs.category_outputs.items():
        if not isinstance(out, Mapping):
            continue
        for i, run in enumerate(out.get("runs") or []):
            pts = run.get("route") if isinstance(run, Mapping) else None
            if not pts or not isinstance(pts, list) or len(pts) < 2:
                continue
            key = _row_sheet_key(run, frames)
            if key is None:
                continue
            res = polyline_length(frames[key], pts, None)
            model_len = run.get("length_ft")
            entry = {"run_ref": f"{cat}:runs:{i}", "row_id": run.get("row_id"), "sheet_key": key, "length_ft": res["length_ft"], "model_length_ft": model_len, "flags": res["flags"], "measure_id": f"run{i + 1}"}
            if res["length_ft"] is not None and isinstance(model_len, (int, float)) and model_len:
                entry["delta_pct"] = round((res["length_ft"] - float(model_len)) / float(model_len) * 100.0, 1)
                if abs(entry["delta_pct"]) > 25:
                    findings.append({"code": "measured_run_delta", "severity": "warn", "message": f"run {entry['run_ref']}: measured {res['length_ft']} ft vs model {model_len} ft", "refs": [entry["measure_id"]]})
            measured_runs.append(entry)
    # Control cable (E14 length part; WS5 owns the inclusion).
    control_devices = int(inputs.scope.control_cable_devices or 0)
    if inputs.scope.thermostats_included and control_devices > 0:
        mseq += 1
        quantities.append(
            {
                "row_id": make_row_id("M", mseq),
                "group": "OTHER ITEMS",
                "subgroup": "MISCELLANEOUS",
                "description": CONTROL_CABLE_DESC,
                "quantity": round(control_devices * conv.control_cable_ft_per_device, 2),
                "unit": "FT",
                "wastage_applies": True,
                "basis": "assumed_allowance",
                "caveat": ASSUMED_CAVEAT,
                "sheet_refs": [],
                "refs": [],
                "derivation": {"terms": [{"devices": control_devices, "ft_per_device": conv.control_cable_ft_per_device}], "inclusion": "thermostats"},
                "line_hint": {"area": "NEW WORK", "group": "OTHER ITEMS", "subgroup": "MISCELLANEOUS", "kind": "control_cable"},
                "material_category": "Low Voltage",
            }
        )
    totals = routes_mod.route_totals(routes)
    bands = _band_checks(totals, devices_new, inputs.project_type, conv, findings, bandset)
    # Scale findings.
    for s in scales_out:
        if s["status"] in ("conflict",) and s["new_work"] and s["sheet_key"] in device_sheets:
            findings.append({"code": "scale_unresolved_on_measured_sheet", "severity": "warn", "message": f"{s['sheet_key']}: scale conflict; routes on it fell back to the allowance basis", "refs": [s["sheet_key"]]})
        if s.get("reduced_print"):
            findings.append({"code": "reduced_print_suspected", "severity": "warn", "message": f"{s['sheet_key']}: reduced print suspected; the printed scale was used unchanged", "refs": [s["sheet_key"]]})
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "version": MEASURE_SCHEMA_VERSION,
        "stage_version": STAGE_VERSION,
        "built_at": now_pacific_iso(),
        "run_id": inputs.run_id,
        "conventions": conv.to_dict(),
        "wiring_method": conv.wiring_method,
        "scope": inputs.scope.to_dict(),
        "scales": scales_out,
        "registrations": [r.to_dict() for r in graph.registrations],
        "panels": [p.to_dict() for _n, p in sorted(panels.items())],
        "circuits": [c.to_dict() for c in circuits],
        "routes": [r.to_dict() for r in routes],
        "linear_items": linear_items,
        "measured_runs": measured_runs,
        "quantities": quantities,
        "totals": totals,
        "bands": bands,
        "findings": findings,
        "judgments": judgments,
        "overrides": [dict(o) for o in inputs.overrides],
        "stats": {
            "tags": cstats["tags"],
            "circuits": cstats["circuits"],
            "circuits_with_devices": cstats["with_devices"],
            "routes": len(routes),
            "sheets": len(frames),
            "device_sheets": device_sheets,
            "panel_sheets": panel_sheets,
            "registrations": len(graph.registrations),
            "new_devices": devices_new,
            "routed_devices": routed_count,
            "untagged_project": untagged_project,
            "distributed_circuits": sum(len(v) for v in distributed.values()),
            "panel_schedules": sorted(str((sch.get("panel") or {}).get("name") or "") for sch in schedules),
            "schedule_sources": schedule_info,
            "linear_items": len(linear_items),
            "quantities": len(quantities),
            "elapsed_ms": elapsed_ms,
        },
    }


def build_measure_safe(inputs: MeasureInputs, conv: routes_mod.Conventions | None = None) -> dict[str, Any]:
    """`build_measure`, and when it raises, the density-only output (spec 6
    WS3: a failed derive group never blocks; main runs and the density tier
    applies with the `branch_density_only` warning). The error is kept in a
    `derive_failed` warn finding; nothing else is invented."""
    try:
        return build_measure(inputs, conv)
    except Exception as exc:  # noqa: BLE001 - the derive stage must never block main
        logger.exception("plans_reader_measure: build_measure failed; density tier only")
        return density_only_measure(inputs, conv, error=f"{type(exc).__name__}: {str(exc)[:200]}")


def density_only_measure(inputs: MeasureInputs, conv: routes_mod.Conventions | None = None, *, error: str | None = None) -> dict[str, Any]:
    """A MeasureOutput v1 with only the density tier: new devices from the
    count rows (EA, not demolition or existing, a device class) x the band
    median, basis `assumed_density`."""
    bandset = measure_bands.load_branch_bands(case_id=inputs.eval_case_id)
    with measure_bands.use(bandset):
        out = _density_only_measure(inputs, conv, bandset, error=error)
    return stabilize_ids(out, inputs.prior)


def _density_only_measure(inputs: MeasureInputs, conv: routes_mod.Conventions | None, bandset: measure_bands.BranchBandSet, *, error: str | None = None) -> dict[str, Any]:
    conv = (conv or routes_mod.Conventions.from_settings()).replace(wiring_method=inputs.scope.wiring_method)
    devices = 0
    if inputs.new_device_count is not None:
        devices = int(inputs.new_device_count)
    else:
        for row in _count_rows(inputs):
            if str(row.get("unit") or "EA").upper() != "EA" or _is_demolition_row(row) or _is_existing_row(row):
                continue
            if parse_item(str(row.get("description") or ""), "EA").item_class in NEW_DEVICE_CLASSES:
                try:
                    devices += int(round(float(row.get("quantity") or 0)))
                except (TypeError, ValueError):
                    pass
    routes = routes_mod.density_routes(devices, conv, project_type=inputs.project_type)
    findings: list[dict[str, Any]] = []
    if error:
        findings.append({"code": "derive_failed", "severity": "warn", "message": f"the measure stage failed ({error}); branch quantities fell back to the density tier", "refs": []})
    if routes:
        findings.append({"code": "branch_density_only", "severity": "warn", "message": f"no device positions or circuit tags; branch quantities are {devices} devices x the history band median", "refs": [r.route_id for r in routes]})
    totals = routes_mod.route_totals(routes)
    quantities = routes_mod.branch_quantities(routes, conv)
    return {
        "version": MEASURE_SCHEMA_VERSION,
        "stage_version": STAGE_VERSION,
        "built_at": now_pacific_iso(),
        "run_id": inputs.run_id,
        "conventions": conv.to_dict(),
        "wiring_method": conv.wiring_method,
        "scope": inputs.scope.to_dict(),
        "scales": [],
        "registrations": [],
        "panels": [],
        "circuits": [],
        "routes": [r.to_dict() for r in routes],
        "linear_items": [],
        "measured_runs": [],
        "quantities": quantities,
        "totals": totals,
        "bands": {**bandset.to_dict(), "stratum": measure_bands.stratum_for(inputs.project_type, bandset), "status": "not_applicable"},
        "findings": findings,
        "judgments": [],
        "overrides": [dict(o) for o in inputs.overrides],
        "stats": {"tags": 0, "circuits": 0, "circuits_with_devices": 0, "routes": len(routes), "sheets": 0, "device_sheets": [], "panel_sheets": [], "registrations": 0, "new_devices": devices, "linear_items": 0, "quantities": len(quantities), "elapsed_ms": 0, "failed": bool(error)},
    }


def _band_checks(totals: Mapping[str, Any], devices_new: int, project_type: str | None, conv: routes_mod.Conventions, findings: list[dict[str, Any]], bandset: measure_bands.BranchBandSet | None = None) -> dict[str, Any]:
    bandset = bandset or measure_bands.current()
    conductors = totals.get("conductors") or {}
    raceway = totals.get("raceway") or {}
    wire = sum(v for k, v in conductors.items() if k in ("#12", "#10", "#14"))
    conduit = sum(v for k, v in raceway.items() if parse_conduit(k) and parse_conduit(k)[0] in BRANCH_CONDUIT_SIZES)  # type: ignore[index]
    ratio = round(wire / conduit, 3) if conduit > 0 and wire > 0 else None
    stratum = measure_bands.stratum_for(project_type, bandset)
    per_device_emt = round(conduit / devices_new, 2) if devices_new > 0 and conduit > 0 else None
    per_device_wire = round(wire / devices_new, 2) if devices_new > 0 and wire > 0 else None
    out = {
        **bandset.to_dict(),
        "stratum": stratum,
        "wire_to_conduit": ratio,
        "wire_to_conduit_band": list(measure_bands.WIRE_TO_CONDUIT_BAND),
        "emt_ft_per_device": measure_bands.band_position("emt_ft_per_device", per_device_emt, stratum, bandset),
        "branch_wire_ft_per_device": measure_bands.band_position("branch_wire_ft_per_device", per_device_wire, stratum, bandset),
    }
    lo, hi = measure_bands.WIRE_TO_CONDUIT_BAND
    if ratio is not None and conv.wiring_method == "emt" and not (lo <= ratio <= hi):
        findings.append({"code": "wire_conduit_ratio_out_of_band", "severity": "warn", "message": f"branch wire to conduit ratio {ratio} outside {lo} to {hi}", "refs": []})
        out["status"] = "ratio_out_of_band"
    else:
        out["status"] = "in_band" if ratio is not None else "not_applicable"
    if out["emt_ft_per_device"]["position"] in ("above", "below"):
        findings.append({"code": "conduit_per_device_out_of_band", "severity": "warn", "message": f"EMT {per_device_emt} ft per device is {out['emt_ft_per_device']['position']} the {stratum} band", "refs": []})
    return out


# Linear items from facts and count rows -----------------------------------------------------------


def _linear_fixture_tags(inputs: MeasureInputs) -> dict[str, dict[str, Any]]:
    """Fixture tags that are linear: WS1 `linear_hint`, a linear word in
    the description, or a per-foot cue."""
    out: dict[str, dict[str, Any]] = {}
    for doc in inputs.documents:
        for ft in doc.facts.fixture_types or []:
            tag = str(ft.get("tag") or "").upper()
            desc = str(ft.get("description") or "")
            if not tag:
                continue
            if ft.get("linear_hint") or LINEAR_LUMINAIRE_RE.search(desc.upper()) or LENGTHS_AS_REQUIRED_RE.search(desc):
                out[tag] = {"description": desc, "schedule_id": ft.get("schedule_id"), "page": ft.get("page")}
    return out


def _linear_fixtures(inputs: MeasureInputs, frames: Mapping[str, routes_mod.SheetFrame], page_by_key: Mapping[str, PageFacts], judgments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    linear_tags = _linear_fixture_tags(inputs)
    if not linear_tags:
        return out
    n = 0
    for key, frame in frames.items():
        page = page_by_key.get(key)
        if page is None or not frame.new_work:
            continue
        marks = (page.tags or {}).get("type_marks") or {}
        for tag, meta in linear_tags.items():
            pts = marks.get(tag) or []
            if not pts:
                continue
            inputs.heartbeat()
            n += 1
            measure_id = f"lin{n}"
            scale = frame.scale_for(float(pts[0][0]), float(pts[0][1]))
            base = {"measure_id": measure_id, "ref": f"m:{measure_id}", "kind": "linear_fixture", "tag": tag, "description": f"{tag} {meta['description']}".strip(), "document_id": frame.document_id, "page": frame.page, "sheet_key": key, "tag_points": [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in pts], "area": "NEW WORK", "group": "LIGHTING FIXTURES", "subgroup": "FIXTURES", "material_category": "Lighting", "count": len(pts), "flags": [], "instances": [], "total_ft": None, "basis": "measured_route", "overridden_by": None}
            segs = inputs.segments(frame.document_id, frame.page) if inputs.segments else None
            if scale is None:
                base["flags"].append("scale_unknown")
                base["basis"] = "assumed_allowance"
            elif segs is None:
                base["flags"].append("no_vectors")
            else:
                res = measure_linear_tag(pts, segs.segments, w_in=frame.w_in, h_in=frame.h_in, ft_per_in=scale)
                base["instances"] = res["instances"]
                base["total_ft"] = res["total_ft"] if res["instances"] else None
                base["flags"].extend(res["flags"])
                base["unmatched"] = res["unmatched"]
                if res["unmatched"] or "implausible_instance" in res["flags"]:
                    judgments.append({"id": f"J{len(judgments) + 1}", "kind": "linear_candidates", "refs": {"tag": tag, "document_id": frame.document_id, "page": frame.page, "measure_id": measure_id}, "default_applied": "vector_rect nearest", "resolved_by": None})
            out.append(base)
    return out


def _linear_demolition(inputs: MeasureInputs, frames: Mapping[str, routes_mod.SheetFrame], count_rows: Sequence[Mapping[str, Any]], findings: list[dict[str, Any]], judgments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Demolition rows of a linear class (REMOVE LIGHTING TRACK ...) counted
    in EA: measure the heavy runs anchored on the keynote markers the row
    cites (or near its positions), else a `linear_in_ea` block finding
    routed to a human."""
    out: list[dict[str, Any]] = []
    n = 0
    for row in count_rows:
        desc = str(row.get("description") or "")
        unit = str(row.get("unit") or "EA").upper()
        identity = parse_item(desc, unit)
        if not (_is_demolition_row(row) and _is_track_identity(identity, desc)):
            continue
        if unit == "FT":
            continue
        inputs.heartbeat()
        key = _row_sheet_key(row, frames)
        n += 1
        measure_id = f"trk{n}"
        base = {"measure_id": measure_id, "ref": f"m:{measure_id}", "kind": "linear_demolition", "tag": str(row.get("symbol_or_tag") or ""), "description": desc, "row_id": row.get("row_id"), "document_id": row.get("document_id"), "page": row.get("page"), "sheet_key": key, "area": "DEMOLITION", "group": "DEMOLITION", "subgroup": "", "material_category": "Demolition", "count": row.get("quantity"), "flags": [], "instances": [], "total_ft": None, "basis": "measured_route", "overridden_by": None, "anchors": [], "anchor_method": None}
        frame = frames.get(key) if key else None
        if frame is None:
            base["flags"].append("sheet_unknown")
            out.append(base)
            _block_linear(findings, row, measure_id, "sheet not found")
            continue
        page = next((p for d in inputs.documents for p in d.facts.pages if d.document_id == frame.document_id and p.page == frame.page), None)
        heavy = inputs.heavy(frame.document_id, frame.page) if inputs.heavy else None
        sheet_runs = merge_runs(heavy, w_in=frame.w_in, h_in=frame.h_in) if heavy else None
        anchors, rule, siblings = track_anchors(row, page, frame, sheet_runs)
        base["anchor_method"] = "keynote_marker" if anchors else None
        base["anchor_rule"] = rule
        base["anchor_siblings"] = siblings
        base["anchors"] = [[round(a[0], 4), round(a[1], 4)] for a in anchors]
        scale = frame.scale_for(anchors[0][0], anchors[0][1]) if anchors else frame.ft_per_in
        if not anchors:
            base["flags"].append("no_anchor")
            _block_linear(findings, row, measure_id, "no keynote marker anchors the row and no vector legend symbol match exists")
        elif scale is None:
            base["flags"].append("scale_unknown")
            _block_linear(findings, row, measure_id, "the sheet has no usable scale")
        elif heavy is None:
            base["flags"].append("no_vectors")
            _block_linear(findings, row, measure_id, "no vector geometry available for the sheet")
        else:
            res = measure_track(anchors, heavy, w_in=frame.w_in, h_in=frame.h_in, ft_per_in=scale)
            base["instances"] = res["instances"]
            base["total_ft"] = res["total_ft"] if res["instances"] else None
            base["flags"].extend(res["flags"])
            if not res["instances"]:
                _block_linear(findings, row, measure_id, "no heavy run near the anchors")
            elif res["unmatched"]:
                judgments.append({"id": f"J{len(judgments) + 1}", "kind": "track_candidates", "refs": {"measure_id": measure_id, "document_id": frame.document_id, "page": frame.page}, "default_applied": "heavy run nearest", "resolved_by": None})
        out.append(base)
    return out


TRACK_KEYNOTE_RE = re.compile(r"\bTRACK\b|\b(?:REMOVE|DEMO\w*)\b.*\bLIGHT|\bLIGHT\w*\b.*\bREMOV", re.I)


def _keynote_texts(page: PageFacts) -> dict[str, str]:
    """Keynote number -> text from the page's WS1 note blocks."""
    out: dict[str, str] = {}
    for block in page.notes or []:
        if not isinstance(block, Mapping) or str(block.get("kind") or "") != "keynotes":
            continue
        for item in block.get("items") or []:
            if isinstance(item, Mapping) and item.get("number") is not None:
                out[str(item.get("number"))] = str(item.get("text") or "")
    return out


def _row_quantity(row: Mapping[str, Any]) -> int | None:
    try:
        q = int(round(float(row.get("quantity"))))
    except (TypeError, ValueError):
        return None
    return q if q > 0 else None


def _congruent_runs(a: geometry.Segment, b: geometry.Segment, w_in: float, h_in: float) -> float | None:
    """The perpendicular gap (inches) between two parallel runs of the same
    length (within TRACK_SIBLING_LEN_TOL) whose spans along their axis
    overlap by at least TRACK_SIBLING_OVERLAP of the shorter: the repeated
    pattern of a set of identical tracks. None when not congruent."""
    la, lb = a.length_in(w_in, h_in), b.length_in(w_in, h_in)
    if la <= 0 or lb <= 0 or abs(la - lb) / max(la, lb) > TRACK_SIBLING_LEN_TOL:
        return None
    tol = RECT_AXIS_TOL_IN
    if a.vertical(tol / w_in) and b.vertical(tol / w_in):
        lo, hi = max(min(a.y0, a.y1), min(b.y0, b.y1)), min(max(a.y0, a.y1), max(b.y0, b.y1))
        overlap_in, gap_in = max(0.0, hi - lo) * h_in, abs(a.x0 - b.x0) * w_in
    elif a.horizontal(tol / h_in) and b.horizontal(tol / h_in):
        lo, hi = max(min(a.x0, a.x1), min(b.x0, b.x1)), min(max(a.x0, a.x1), max(b.x0, b.x1))
        overlap_in, gap_in = max(0.0, hi - lo) * w_in, abs(a.y0 - b.y0) * h_in
    else:
        return None
    if overlap_in < TRACK_SIBLING_OVERLAP * min(la, lb):
        return None
    return gap_in


def track_anchors(
    row: Mapping[str, Any],
    page: PageFacts | None,
    frame: routes_mod.SheetFrame,
    runs: Sequence[tuple[geometry.Segment, float]] | None = None,
) -> tuple[list[list[float]], str | None, int]:
    """Anchors of a linear demolition row (spec 6 WS3): WS1 keynote markers
    only, never the model's row positions themselves. A marker anchors when
    the row cites its keynote number, else when the row's positions put it
    within ANCHOR_REACH_IN, else when its keynote text names the track or
    the lighting removal. A legend symbol match needs a vector symbol
    matcher, which does not exist yet, so without a keynote anchor the
    caller blocks with `linear_in_ea` (human route).

    The row positions are the model's rough coordinates and move between
    runs on the same sheet (7154 ED3.01: y 0.47 on one run, 0.57 on the
    next, which left the middle of three track markers 2.13 in away). With
    the sheet's merged heavy `runs` (at the frame's scale for each marker),
    when no marker is within reach of a position, the run a marker marks
    (`_track_run_for`) may be: a position on the track body reaches the
    keynote at its end. Then the sheet geometry completes the set: while
    fewer anchors than the row's EA quantity are chosen, a marker of the
    same keynote number whose run is congruent with a chosen run (parallel,
    same length, same span, TRACK_SIBLING_GAP_IN apart) joins it. The
    track length then follows the keynote markers and the drawn runs, not
    the precision of the row positions. Returns (anchors, rule, siblings):
    rule keynote_cited, keynote_near_row or keynote_text (None without
    anchors); siblings is how many congruent markers joined."""
    if page is None:
        return [], None, 0
    markers = [m for m in page.keynote_markers or [] if isinstance(m, Mapping)]
    if not markers:
        return [], None, 0
    w_in, h_in = frame.w_in, frame.h_in
    run_cache: dict[int, tuple[geometry.Segment, float] | None] = {}

    def hit_of(i: int) -> tuple[geometry.Segment, float] | None:
        if runs is None:
            return None
        if i not in run_cache:
            mx, my = float(markers[i].get("x", 0.0)), float(markers[i].get("y", 0.0))
            scale = frame.scale_for(mx, my)
            hit = _track_run_for(mx, my, runs, w_in=w_in, h_in=h_in, ft_per_in=float(scale)) if scale else None
            run_cache[i] = (hit[0], hit[2]) if hit is not None else None
        return run_cache[i]

    def run_of(i: int) -> geometry.Segment | None:
        hit = hit_of(i)
        return hit[0] if hit is not None else None

    def marker_reaches(i: int, pts: Sequence[Sequence[float]]) -> bool:
        mx, my = float(markers[i].get("x", 0.0)), float(markers[i].get("y", 0.0))
        return any(math.hypot((mx - x) * w_in, (my - y) * h_in) <= ANCHOR_REACH_IN for x, y in pts)

    def body_reaches(i: int, pts: Sequence[Sequence[float]]) -> bool:
        run = run_of(i)
        return run is not None and any(_point_run_distance_in(x, y, run, w_in, h_in) <= ANCHOR_REACH_IN for x, y in pts)

    def near(cands: Sequence[int], pts: Sequence[Sequence[float]]) -> list[int]:
        """Markers within reach of a position; when none is, markers whose
        run body is within reach, keeping the heaviest stroke class (a
        ceiling grid line under a switch keynote never stands in for a
        track)."""
        out = [i for i in cands if marker_reaches(i, pts)]
        if out:
            return out
        body = [i for i in cands if body_reaches(i, pts)]
        if not body:
            return []
        top = max((hit_of(i) or (None, 0.0))[1] for i in body)
        return [i for i in body if (hit_of(i) or (None, 0.0))[1] >= top]

    cited = {m.group(1) for m in KEYNOTE_CITE_RE.finditer(f"{row.get('symbol_or_tag') or ''} {row.get('notes') or ''} {row.get('description') or ''}")}
    positions = _row_positions(row)
    idx = list(range(len(markers)))
    if cited:
        chosen = [i for i in idx if str(markers[i].get("number")) in cited]
        if positions:
            chosen = near(chosen, positions) or chosen
        method = "keynote_cited"
    elif positions:
        chosen = near(idx, positions)
        method = "keynote_near_row"
    else:
        texts = _keynote_texts(page)
        chosen = [i for i in idx if TRACK_KEYNOTE_RE.search(texts.get(str(markers[i].get("number")), ""))]
        method = "keynote_text"
    siblings = 0
    want = _row_quantity(row)
    if chosen and want is not None and len(chosen) < want and runs is not None:
        numbers = {str(markers[i].get("number")) for i in chosen}
        while len(chosen) < want:
            held = [r for r in (run_of(i) for i in chosen) if r is not None]
            best: tuple[float, int] | None = None
            for i in idx:
                if i in chosen or str(markers[i].get("number")) not in numbers:
                    continue
                run = run_of(i)
                if run is None:
                    continue
                gaps = [_congruent_runs(run, h, w_in, h_in) for h in held]
                if not gaps or any(g is not None and g < TRACK_SIBLING_GAP_IN[0] for g in gaps):
                    continue  # the same track (a doubled stroke) is never a sibling
                near_gaps = [g for g in gaps if g is not None and g <= TRACK_SIBLING_GAP_IN[1]]
                if near_gaps and (best is None or min(near_gaps) < best[0]):
                    best = (min(near_gaps), i)
            if best is None:
                break
            chosen.append(best[1])
            siblings += 1
    anchors = [[float(markers[i].get("x", 0.0)), float(markers[i].get("y", 0.0))] for i in chosen]
    return anchors, (method if anchors else None), siblings


def linear_derivation(item: Mapping[str, Any]) -> dict[str, Any]:
    """The derivation of a linear M row (shape in the module docstring)."""
    mid = str(item.get("measure_id") or "")
    return {
        "terms": [
            {"instance": i, "length_ft": inst["length_ft"], "method": inst.get("method"), "measure_id": mid, "linear_item_id": mid, "basis": item.get("basis")}
            for i, inst in enumerate(item.get("instances") or [], start=1)
        ],
        "tag": item.get("tag"),
        "measure_id": mid,
    }


def _is_track_identity(identity: Any, desc: str) -> bool:
    """A lighting track (never a head) or the demolition of one, decided by
    the item class (electrical_items.is_lighting_track: a lighting or
    demolition row whose primary noun is the track, the HEAD guard on that
    noun only), never by the reader's symbol_or_tag wording. Run b87f0ffd
    wrote the 7154 track row as "Remove existing track-lighting assemblies
    including heads": the old guard on HEAD anywhere in the text and the
    luminaire subject parse_item gives it dropped the track M row. `desc`
    is kept for the callers; the identity carries the normalized text."""
    return is_lighting_track(identity)


def _block_linear(findings: list[dict[str, Any]], row: Mapping[str, Any], measure_id: str, why: str) -> None:
    findings.append({"code": "linear_in_ea", "severity": "block", "route": "human", "message": f"{row.get('description')}: a linear item counted in EA could not be measured ({why})", "refs": [measure_id], "row_ids": [row.get("row_id")] if row.get("row_id") else []})


def apply_linear_overrides(linear_items: list[dict[str, Any]], overrides: Iterable[Mapping[str, Any]], frames: Mapping[str, routes_mod.SheetFrame]) -> None:
    """`set_linear_item` overrides: endpoints (page fractions) measured at
    the sheet scale, else the named candidates kept; human beats model."""
    applied: dict[str, str] = {}
    for ov in overrides:
        if ov.get("tool") != "set_linear_item":
            continue
        args = ov.get("args") or {}
        by = str(ov.get("by") or "model")
        tag = str(args.get("tag") or "").upper()
        doc_id, page = str(args.get("document_id")), int(args.get("page") or 0)
        k = f"{doc_id}:{page}:{tag}"
        if applied.get(k) == "human" and by != "human":
            continue
        frame = next((f for f in frames.values() if f.document_id == doc_id and f.page == page), None)
        if frame is None:
            continue
        item = next((it for it in linear_items if str(it.get("tag") or "").upper() == tag and it.get("document_id") == doc_id and it.get("page") == page), None)
        if item is None:
            item = {"measure_id": f"ov{len(linear_items) + 1}", "ref": f"m:ov{len(linear_items) + 1}", "kind": "linear_fixture", "tag": tag, "description": tag, "document_id": doc_id, "page": page, "sheet_key": frame.sheet_key, "area": "NEW WORK", "group": "LIGHTING FIXTURES", "subgroup": "FIXTURES", "material_category": "Lighting", "flags": [], "instances": [], "total_ft": None, "basis": "measured_route", "overridden_by": None}
            linear_items.append(item)
        endpoints = args.get("endpoints")
        if endpoints:
            instances = []
            for seg in endpoints:
                (x0, y0), (x1, y1) = seg[0], seg[1]
                scale = frame.scale_for(float(x0), float(y0))
                if scale is None:
                    continue
                length = math.hypot((float(x1) - float(x0)) * frame.w_in, (float(y1) - float(y0)) * frame.h_in) * scale
                instances.append({"bbox": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], "length_ft": round(length, 2), "method": f"{by}_endpoints"})
            item["instances"] = instances
        else:
            wanted = set(args.get("candidate_ids") or [])
            if wanted:
                item["instances"] = [i for i in item.get("instances") or [] if i.get("candidate_id") in wanted]
        item["total_ft"] = round(sum(i["length_ft"] for i in item["instances"]), 2) if item["instances"] else None
        item["overridden_by"] = by
        item["basis"] = "measured_route"
        applied[k] = by


# Polylines ---------------------------------------------------------------------------------------


def polyline_length(frame: routes_mod.SheetFrame, points: Sequence[Sequence[float]], vertical_ft: float | None) -> dict[str, Any]:
    """Length of a polyline in page fractions at the frame's scale (the
    viewport of the first point), plus the vertical term. Refuses (length
    None) on an NTS or conflicting scale, when the points cross viewports
    or when the length is implausible for the sheet."""
    flags: list[str] = []
    if len(points) < 2:
        return {"length_ft": None, "horizontal_ft": None, "vertical_ft": vertical_ft, "ft_per_in": None, "flags": ["too_few_points"]}
    scale = frame.scale_for(float(points[0][0]), float(points[0][1]))
    if scale is None:
        return {"length_ft": None, "horizontal_ft": None, "vertical_ft": vertical_ft, "ft_per_in": None, "flags": ["scale_unknown"]}
    inches = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        s1 = frame.scale_for(float(x1), float(y1))
        if s1 is not None and abs(s1 - scale) > 1e-9:
            flags.append("crosses_viewports")
            return {"length_ft": None, "horizontal_ft": None, "vertical_ft": vertical_ft, "ft_per_in": scale, "flags": flags}
        inches += math.hypot((float(x1) - float(x0)) * frame.w_in, (float(y1) - float(y0)) * frame.h_in)
    horizontal = inches * scale
    diag = math.hypot(frame.w_in, frame.h_in) * scale
    total = horizontal + float(vertical_ft or 0.0)
    if horizontal > 1.5 * diag:
        flags.append("implausible_length")
        return {"length_ft": None, "horizontal_ft": round(horizontal, 2), "vertical_ft": vertical_ft, "ft_per_in": scale, "flags": flags}
    return {"length_ft": round(total, 2), "horizontal_ft": round(horizontal, 2), "vertical_ft": vertical_ft, "ft_per_in": scale, "flags": flags}


# Summary ------------------------------------------------------------------------------------------


def measure_summary_text(measure: Mapping[str, Any], *, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """The compact block the main prompt receives: scales, quantity rows
    with basis, route counts, linear items, findings. No prices."""
    lines: list[str] = ["MEASURE OUTPUT (app-computed; branch wire and conduit rows come from these M rows, never write them yourself):"]
    conv = measure.get("conventions") or {}
    lines.append(f"conventions {conv.get('version')}: {conv.get('branch_conduit')} per circuit, H+N+G, min {conv.get('min_branch_awg')}, route factor {conv.get('route_factor')}")
    scales = measure.get("scales") or []
    if scales:
        parts = []
        for s in scales:
            st = s.get("status")
            parts.append(f"{s.get('sheet_key')} {s.get('ft_per_in')} ft/in" if st == "selected" else f"{s.get('sheet_key')} {st}")
        lines.append("scales: " + "; ".join(parts))
    stats = measure.get("stats") or {}
    lines.append(f"routes: {stats.get('routes', 0)} circuits from {stats.get('tags', 0)} tags on {len(stats.get('device_sheets') or [])} sheets; registrations {stats.get('registrations', 0)}")
    for q in measure.get("quantities") or []:
        lines.append(f"{q.get('row_id')}: {q.get('description')} {q.get('quantity')} {q.get('unit')} [{q.get('basis')}{' ' + q['caveat'] if q.get('caveat') else ''}] sheets {', '.join(q.get('sheet_refs') or [])}")
    for item in measure.get("linear_items") or []:
        total = item.get("total_ft")
        lines.append(f"linear {item.get('tag') or item.get('description')}: {total if total is not None else 'not measured'} FT from {len(item.get('instances') or [])} instances on {item.get('sheet_key')}" + (f" flags {', '.join(item['flags'])}" if item.get("flags") else ""))
    for f in measure.get("findings") or []:
        lines.append(f"finding {f.get('severity')} {f.get('code')}: {f.get('message')}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 40] + "\n[measure summary truncated]"
    return text


# Legacy BOQ application --------------------------------------------------------------------------


def _is_model_branch_row(item: Mapping[str, Any]) -> str | None:
    """The reason a model-written row is a branch row code owns: conductor
    (#14 to #10), branch conduit (1/2" or 3/4" EMT) or MC/NM 14 to 10 cable.
    Feeders (#8 and larger, conduit 1" and larger) stay."""
    desc = str(item.get("description") or "")
    unit = str(item.get("unit") or "").upper()
    if item.get("is_accessory"):
        return None
    if unit not in ("FT", ""):
        return None
    if BRANCH_CABLE_RE.search(desc):
        return "branch_cable"
    parsed = parse_conduit(desc)
    if parsed is not None:
        size, kind = parsed
        return "branch_conduit" if size.replace(" ", "") in BRANCH_CONDUIT_SIZES and kind == "EMT" else None
    if BRANCH_CONDUCTOR_RE.search(desc) and "CONTROL" not in desc.upper():
        return "branch_conductor"
    return None


def _walk_items(spec: Mapping[str, Any]):
    for a_i, area in enumerate(spec.get("areas") or []):
        for s_i, section in enumerate(area.get("sections") or []):
            for g_i, group in enumerate(section.get("groups") or []):
                for sg_i, sg in enumerate(group.get("subgroups") or []):
                    yield area, section, group, sg, (a_i, s_i, g_i, sg_i)


def _target_area(spec: dict[str, Any], sheet_refs: Sequence[str], want_demo: bool = False) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(area, section) that holds the NEW WORK (or DEMOLITION) groups whose
    sheet_refs cover the given sheets, else the first such area."""
    best: tuple[dict[str, Any], dict[str, Any]] | None = None
    for area in spec.get("areas") or []:
        for section in area.get("sections") or []:
            heading = str(section.get("heading") or section.get("name") or "").upper()
            groups = [str(g.get("name") or "").upper() for g in section.get("groups") or []]
            is_demo = "DEMOLITION" in heading or (groups and all(g == "DEMOLITION" for g in groups))
            if want_demo != is_demo:
                continue
            refs = {str(r) for g in section.get("groups") or [] for sg in g.get("subgroups") or [] for it in sg.get("items") or [] for r in (it.get("sheet_refs") or [])}
            if sheet_refs and set(sheet_refs) & refs:
                return area, section
            if best is None:
                best = (area, section)
    return best


def _ensure_subgroup(section: dict[str, Any], group_name: str, subgroup_name: str) -> dict[str, Any]:
    groups = section.setdefault("groups", [])
    group = next((g for g in groups if str(g.get("name") or "").upper() == group_name.upper()), None)
    if group is None:
        group = {"name": group_name, "subgroups": []}
        groups.append(group)
    subs = group.setdefault("subgroups", [])
    sg = next((s for s in subs if str(s.get("name") or "").upper().startswith(subgroup_name.upper().split("(")[0].strip())), None)
    if sg is None:
        sg = {"name": subgroup_name, "items": []}
        subs.append(sg)
    sg.setdefault("items", [])
    return sg


def _m_row_item(q: Mapping[str, Any]) -> dict[str, Any]:
    basis, caveat = line_basis_for_measure(str(q.get("basis") or "assumed_route"))
    return {
        "description": q["description"],
        "quantity": float(q["quantity"]),
        "unit": str(q.get("unit") or "FT"),
        "wastage_applies": bool(q.get("wastage_applies")),
        "sheet_refs": list(q.get("sheet_refs") or []),
        "caveat": caveat,
        "material_note": None,
        "material_category": q.get("material_category") or "General Material",
        "is_accessory": False,
        "measure_ref": q.get("row_id"),
        "basis": basis,
        "source_refs": [{"ref": q.get("row_id"), "multiplier": 1}],
        "combine": "single",
        "derivation": q.get("derivation"),
    }


def apply_measure_to_boq(boq_spec: Mapping[str, Any], measure: Mapping[str, Any], *, owner: str = "code") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Legacy main mode: replace the model's branch rows with the measure
    output's M rows, convert linear EA rows the measure output covers to FT,
    add the control cable row, and record one finding per change. Rows the
    measure output does not cover are never touched. Returns (new spec,
    findings)."""
    spec = json.loads(json.dumps(dict(boq_spec)))
    findings: list[dict[str, Any]] = []
    quantities = [q for q in measure.get("quantities") or [] if isinstance(q, Mapping)]
    branch_rows = [q for q in quantities if str((q.get("line_hint") or {}).get("kind") or "branch") == "branch" and str(q.get("group") or "").upper() == "WIRING"]
    linear_rows = [q for q in quantities if (q.get("line_hint") or {}).get("replaces_unit") == "EA"]
    control_rows = [q for q in quantities if (q.get("line_hint") or {}).get("kind") == "control_cable"]
    # 1. Remove the model's branch rows (only when the measure output has branch rows).
    deleted: list[dict[str, Any]] = []
    if branch_rows and owner == "code":
        for _area, _section, group, sg, _pos in _walk_items(spec):
            if str(group.get("name") or "").upper() != "WIRING":
                continue
            kept = []
            for item in sg.get("items") or []:
                why = _is_model_branch_row(item)
                if why is None:
                    kept.append(item)
                    continue
                deleted.append(item)
                findings.append({"code": "model_branch_row_replaced", "severity": "info", "message": f"removed model row {item.get('description')} {item.get('quantity')} {item.get('unit')} ({why}); the measure output owns branch rows", "before": {"description": item.get("description"), "quantity": item.get("quantity"), "unit": item.get("unit")}, "after": None})
            sg["items"] = kept
    # 2. Insert the M rows.
    if branch_rows:
        sheet_refs = sorted({r for q in branch_rows for r in (q.get("sheet_refs") or [])})
        target = _target_area(spec, sheet_refs)
        if target is None:
            spec.setdefault("areas", []).append({"area": "NEW WORK", "notes": [], "sections": [{"heading": "NEW WORK", "note": None, "groups": []}]})
            target = (spec["areas"][-1], spec["areas"][-1]["sections"][0])
        _area, section = target
        for q in branch_rows:
            sg = _ensure_subgroup(section, "WIRING", str(q.get("subgroup") or "CONDUCTORS (AWG)"))
            sg["items"].append(_m_row_item(q))
            findings.append({"code": "measure_row_inserted", "severity": "info", "message": f"{q.get('row_id')} {q.get('description')} {q.get('quantity')} FT [{q.get('basis')}]", "before": None, "after": {"description": q.get("description"), "quantity": q.get("quantity"), "unit": "FT"}})
    # 3. Linear items: EA rows the measure output covers become FT.
    for q in linear_rows:
        tag = str((q.get("line_hint") or {}).get("tag") or "").upper()
        kind = (q.get("line_hint") or {}).get("kind")
        converted = False
        for _area, _section, group, sg, _pos in _walk_items(spec):
            for item in sg.get("items") or []:
                if str(item.get("unit") or "").upper() != "EA":
                    continue
                desc = str(item.get("description") or "")
                identity = parse_item(desc, "EA")
                if kind == "linear_fixture":
                    hit = identity.attrs.get("tag", "").upper() == tag or re.match(rf"^\s*{re.escape(tag)}\b", desc.upper()) is not None
                    hit = hit and str(group.get("name") or "").upper() == "LIGHTING FIXTURES"
                else:
                    hit = _is_demolition_row(item) and _is_track_identity(identity, desc)
                if not hit:
                    continue
                before = {"description": desc, "quantity": item.get("quantity"), "unit": "EA"}
                item["unit"] = "FT"
                item["quantity"] = float(q["quantity"])
                item["wastage_applies"] = False
                item["caveat"] = None if q.get("basis") == "measured_route" else ASSUMED_CAVEAT
                item["measure_ref"] = q.get("row_id")
                item["basis"] = line_basis_for_measure(str(q.get("basis") or "measured_route"))[0]
                item["source_refs"] = [{"ref": q.get("row_id"), "multiplier": 1}]
                findings.append({"code": "linear_unit_enforced", "severity": "info", "message": f"{desc}: {before['quantity']} EA -> {q['quantity']} FT ({q.get('row_id')})", "before": before, "after": {"description": desc, "quantity": q["quantity"], "unit": "FT"}})
                converted = True
                break
            if converted:
                break
    # 4. Control cable.
    for q in control_rows:
        exists = any(CONTROL_CABLE_DESC.split()[0] in str(it.get("description") or "").upper() and "CONTROL CABLE" in str(it.get("description") or "").upper() for *_r, sg, _p in _walk_items(spec) for it in sg.get("items") or [])
        if exists:
            continue
        target = _target_area(spec, [])
        if target is None:
            continue
        sg = _ensure_subgroup(target[1], "OTHER ITEMS", "MISCELLANEOUS")
        sg["items"].append({**_m_row_item(q), "material_category": "Low Voltage"})
        findings.append({"code": "measure_row_inserted", "severity": "info", "message": f"{q.get('row_id')} {q.get('description')} {q.get('quantity')} FT [{q.get('basis')}]", "before": None, "after": {"description": q.get("description"), "quantity": q.get("quantity"), "unit": "FT"}})
    return spec, findings


def branch_coverage_findings(boq_spec: Mapping[str, Any], measure: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Gate-shaped findings (spec 5.4 gate ids) from the BOQ and the measure
    output: `new_devices_no_branch` (block) when new devices exist and the
    branch conductor FT (#14 to #10) or the branch conduit FT (1/2" and 3/4"
    EMT) is zero (spec 3.5 E2, 3.6 GC3; under the MC or NM wiring method a
    branch cable row satisfies both), `linear_in_ea` (block) for linear EA
    rows, `branch_ratio` (warn), the density warning. Each carries
    `gate_id`, `severity`, `route`, `target`, `message`."""
    out: list[dict[str, Any]] = []
    new_devices = 0
    branch: dict[str, float] = {"branch_conduit": 0.0, "branch_conductor": 0.0, "branch_cable": 0.0}
    linear_ea: list[dict[str, Any]] = []
    for _area, _section, group, sg, _pos in _walk_items(boq_spec):
        gname = str(group.get("name") or "").upper()
        for item in sg.get("items") or []:
            if item.get("is_accessory"):
                continue
            unit = str(item.get("unit") or "").upper()
            desc = str(item.get("description") or "")
            try:
                qty = float(item.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if gname in NEW_DEVICE_GROUPS and unit == "EA" and not _is_demolition_row(item):
                new_devices += int(round(qty))
            if gname == "WIRING" and unit == "FT":
                why = _is_model_branch_row(item)
                if why in branch:
                    branch[why] += qty
            identity = parse_item(desc, unit)
            if unit == "EA" and (_is_track_identity(identity, desc) or identity.item_class in ("trench", "sawcut") or (is_linear(identity) and (BRANCH_CONDUCTOR_RE.search(desc) or parse_conduit(desc) is not None))):
                linear_ea.append({"description": desc, "line_id": item.get("line_id"), "row_id": item.get("measure_ref")})
    method = str(measure.get("wiring_method") or (measure.get("conventions") or {}).get("wiring_method") or "emt").lower()
    if method in ("mc", "nm") and branch["branch_cable"] > 0:
        missing: list[str] = []
    else:
        missing = [label for key, label in (("branch_conductor", "branch conductor (#14 to #10)"), ("branch_conduit", 'branch conduit (1/2" or 3/4" EMT)')) if branch[key] <= 0]
    if new_devices > 0 and missing:
        out.append(
            {
                "gate_id": GATE_ALIASES["branch_ft_empty"],
                "code": "branch_ft_empty",
                "severity": "block",
                "route": "measure",
                "target": {"field": "branch_ft", "missing": [m.split(" (")[0].replace(" ", "_") for m in missing]},
                "message": f"{new_devices} new devices and no {' or '.join(missing)} FT (wiring method {method})",
                "branch_ft": {k: round(v, 1) for k, v in branch.items()},
            }
        )
    for row in linear_ea:
        out.append({"gate_id": "linear_in_ea", "code": "linear_in_ea", "severity": "block", "route": "human", "target": {k: v for k, v in (("line_id", row.get("line_id")), ("description", row.get("description"))) if v}, "message": f"{row['description']} is a linear class counted in EA"})
    for f in measure.get("findings") or []:
        code = str(f.get("code") or "")
        if code == "branch_density_only":
            out.append({"gate_id": "branch_ratio", "code": code, "severity": "warn", "route": "none", "target": {"field": "branch_ft"}, "message": f.get("message")})
        elif code == "wire_conduit_ratio_out_of_band":
            out.append({"gate_id": "branch_ratio", "code": code, "severity": "warn", "route": "none", "target": {"field": "wire_to_conduit"}, "message": f.get("message")})
        elif code == "linear_in_ea" and not linear_ea:
            out.append({"gate_id": "linear_in_ea", "code": code, "severity": "block", "route": "human", "target": {"row_ids": list(f.get("row_ids") or [])}, "message": f.get("message")})
    return out


# Stable ids --------------------------------------------------------------------------------------

_ROUTE_ID_RE = re.compile(r"^r(\d+)$")
_ROW_ID_RE = re.compile(r"^M(\d+)$")


def _max_seq(ids: Iterable[Any], pattern: re.Pattern[str]) -> int:
    best = 0
    for i in ids:
        m = pattern.match(str(i or ""))
        if m:
            best = max(best, int(m.group(1)))
    return best


def _route_keys(routes: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str, int]]:
    seen: dict[tuple[str, str, str], int] = {}
    keys = []
    for r in routes:
        base = (str(r.get("tier") or ""), str(r.get("circuit_id") or ""), str(r.get("panel") or ""))
        seen[base] = seen.get(base, 0) + 1
        keys.append((*base, seen[base]))
    return keys


def _row_key(q: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    hint = q.get("line_hint") or {}
    mref = next((str(x) for x in q.get("refs") or [] if str(x).startswith("m:")), "")
    return (str(q.get("group") or ""), str(q.get("subgroup") or ""), str(q.get("description") or ""), str(hint.get("kind") or "branch"), mref)


def stabilize_ids(measure: dict[str, Any], prior: Mapping[str, Any] | None) -> dict[str, Any]:
    """Make the `M<n>` row ids and `r<n>` route ids of `measure` stable
    against the run's previous version `prior` (spec 5.1: run-unique, never
    reused; 5.2: refs stable within a run). A row keeps the prior id of the
    row with the same (group, subgroup, description, kind, m: ref), a route
    the prior id of the route with the same (tier, circuit_id, panel,
    occurrence); everything else gets a new id above the high-water marks
    `row_seq` and `route_seq`, so a retired id (kept in `retired_ids`) is
    never issued again. Every reference to a renamed id (route refs and
    derivation terms of the rows, finding refs, linear item row ids) is
    rewritten. Measured polylines (`polylines[]`) carry forward. Mutates and
    returns `measure`."""
    routes = [r for r in measure.get("routes") or [] if isinstance(r, dict)]
    quantities = [q for q in measure.get("quantities") or [] if isinstance(q, dict)]
    prior = prior if isinstance(prior, Mapping) else None
    retired: set[str] = {str(x) for x in (prior or {}).get("retired_ids") or []}
    route_hw = max(int((prior or {}).get("route_seq") or 0), _max_seq((r.get("route_id") for r in (prior or {}).get("routes") or []), _ROUTE_ID_RE), _max_seq(retired, _ROUTE_ID_RE))
    row_hw = max(int((prior or {}).get("row_seq") or 0), _max_seq((q.get("row_id") for q in (prior or {}).get("quantities") or []), _ROW_ID_RE), _max_seq(retired, _ROW_ID_RE))
    rmap: dict[str, str] = {}
    qmap: dict[str, str] = {}
    if prior is None:
        route_hw = max(route_hw, _max_seq((r.get("route_id") for r in routes), _ROUTE_ID_RE))
        row_hw = max(row_hw, _max_seq((q.get("row_id") for q in quantities), _ROW_ID_RE))
    else:
        prior_routes = [r for r in prior.get("routes") or [] if isinstance(r, Mapping)]
        by_key = {k: str(r.get("route_id")) for k, r in zip(_route_keys(prior_routes), prior_routes, strict=True) if r.get("route_id")}
        used: set[str] = set()
        for key, r in zip(_route_keys(routes), routes, strict=True):
            pid = by_key.get(key)
            if pid and pid not in used:
                new = pid
            else:
                route_hw += 1
                new = f"r{route_hw}"
            used.add(new)
            if r.get("route_id"):
                rmap[str(r["route_id"])] = new
            r["route_id"] = new
            r["ref"] = f"r:{new}"
        prior_rows = [q for q in prior.get("quantities") or [] if isinstance(q, Mapping)]
        rows_by_key = {_row_key(q): str(q.get("row_id")) for q in prior_rows if q.get("row_id")}
        used_rows: set[str] = set()
        for q in quantities:
            pid = rows_by_key.get(_row_key(q))
            if pid and pid not in used_rows:
                new = pid
            else:
                row_hw += 1
                new = make_row_id("M", row_hw)
            used_rows.add(new)
            if q.get("row_id"):
                qmap[str(q["row_id"])] = new
            q["row_id"] = new
        retired |= {str(r.get("route_id")) for r in prior_routes if r.get("route_id")} - used
        retired |= {str(q.get("row_id")) for q in prior_rows if q.get("row_id")} - used_rows

        def _ref(x: Any) -> Any:
            t = str(x)
            if t.startswith("r:") and t[2:] in rmap:
                return f"r:{rmap[t[2:]]}"
            return rmap.get(t, x) if isinstance(x, str) else x

        for q in quantities:
            q["refs"] = [_ref(x) for x in q.get("refs") or []]
            for t in (q.get("derivation") or {}).get("terms") or []:
                if isinstance(t, dict) and t.get("route_id") in rmap:
                    t["route_id"] = rmap[t["route_id"]]
        for f in measure.get("findings") or []:
            if isinstance(f, dict) and f.get("refs"):
                f["refs"] = [_ref(x) for x in f["refs"]]
        for item in measure.get("linear_items") or []:
            if isinstance(item, dict) and item.get("row_id") in qmap:
                item["row_id"] = qmap[item["row_id"]]
        polylines = {str(pl.get("measure_id")): dict(pl) for pl in prior.get("polylines") or [] if isinstance(pl, Mapping)}
        for pl in measure.get("polylines") or []:
            if isinstance(pl, Mapping):
                polylines[str(pl.get("measure_id"))] = dict(pl)
        measure["polylines"] = list(polylines.values())
    retired -= {str(r.get("route_id")) for r in routes} | {str(q.get("row_id")) for q in quantities}
    measure["route_seq"] = max(route_hw, _max_seq((r.get("route_id") for r in routes), _ROUTE_ID_RE))
    measure["row_seq"] = max(row_hw, _max_seq((q.get("row_id") for q in quantities), _ROW_ID_RE))
    measure["retired_ids"] = sorted(retired, key=lambda x: (x[:1], int(x[1:]) if x[1:].isdigit() else 0))
    measure.setdefault("polylines", [])
    return measure


# Versioned store and service ----------------------------------------------------------------------


class MemoryMeasureStore:
    """Versions per run in memory (tests, replay)."""

    def __init__(self) -> None:
        self._versions: dict[str, list[dict[str, Any]]] = {}

    def latest(self, run_id: str) -> tuple[int, dict[str, Any]] | None:
        vs = self._versions.get(run_id)
        return (len(vs), vs[-1]) if vs else None

    def write(self, run_id: str, measure: dict[str, Any], *, source_group_ids: Sequence[str] = ()) -> int:
        vs = self._versions.setdefault(run_id, [])
        vs.append(dict(measure))
        return len(vs)

    def versions(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._versions.get(run_id, []))

    def append_polyline(self, run_id: str, entry: Mapping[str, Any]) -> bool:
        """Store a measured polyline in the latest version (no new version:
        a polyline is a read, not an override)."""
        vs = self._versions.get(run_id)
        if not vs:
            return False
        latest = dict(vs[-1])
        pls = [dict(pl) for pl in latest.get("polylines") or []]
        if not any(pl.get("measure_id") == entry.get("measure_id") for pl in pls):
            pls.append(dict(entry))
        latest["polylines"] = pls
        vs[-1] = latest
        return True


class DbMeasureStore:
    """`plans_reader_outputs` rows with scope `measure` (sync Supabase
    client; worker threads and plain `def` routes only)."""

    TABLE = "plans_reader_outputs"

    def __init__(self, sb: Any) -> None:
        """`sb` is a client, or a zero-argument getter resolved per call
        (startup registration before a client exists)."""
        self._sb = sb

    @property
    def sb(self) -> Any:
        return self._sb if hasattr(self._sb, "table") or not callable(self._sb) else self._sb()

    def latest(self, run_id: str) -> tuple[int, dict[str, Any]] | None:
        rows = (
            self.sb.table(self.TABLE)
            .select("version, output")
            .eq("run_id", run_id)
            .eq("scope", "measure")
            .order("version", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return None
        return int(rows[0].get("version") or 1), dict(rows[0].get("output") or {})

    def write(self, run_id: str, measure: dict[str, Any], *, source_group_ids: Sequence[str] = ()) -> int:
        """Insert the next version; a concurrent writer that took the same
        version number (unique run, scope, category, version) makes this
        retry with the next one, at most three times."""
        for attempt in range(3):
            latest = self.latest(run_id)
            version = (latest[0] + 1) if latest else 1
            row = {"run_id": run_id, "scope": "measure", "category": None, "version": version, "output": measure, "source_group_ids": list(source_group_ids)}
            try:
                self.sb.table(self.TABLE).insert(row).execute()
                return version
            except Exception as exc:  # noqa: BLE001 - only a unique violation is retried
                text = str(exc)
                if attempt == 2 or not ("23505" in text or "duplicate key" in text.lower()):
                    raise
        raise RuntimeError("unreachable")

    def append_polyline(self, run_id: str, entry: Mapping[str, Any]) -> bool:
        """Store a measured polyline in the latest version's `polylines[]`
        (read, merge, update, then re-read to confirm; a concurrent writer
        that replaced the row makes this retry, at most three times)."""
        mid = entry.get("measure_id")
        for _attempt in range(3):
            latest = self.latest(run_id)
            if latest is None:
                return False
            version, output = latest
            pls = [dict(pl) for pl in output.get("polylines") or [] if isinstance(pl, Mapping)]
            if any(pl.get("measure_id") == mid for pl in pls):
                return True
            output = {**output, "polylines": pls + [dict(entry)]}
            self.sb.table(self.TABLE).update({"output": output}).eq("run_id", run_id).eq("scope", "measure").eq("version", version).execute()
            check = self.latest(run_id)
            if check is not None and any(isinstance(pl, Mapping) and pl.get("measure_id") == mid for pl in check[1].get("polylines") or []):
                return True
        return False


def polyline_measure_id(run_id: str | None, document_id: str, page: int, points: Sequence[Sequence[float]], vertical_ft: float | None) -> str:
    """`pl` + 12 hex of sha256(run, document, page, points at 4 decimals,
    vertical): the same polyline gets the same id on any worker and after a
    resume, and two different polylines never share one."""
    import hashlib

    key = json.dumps([run_id, document_id, int(page), [[round(float(x), 4), round(float(y), 4)] for x, y in points], vertical_ft], separators=(",", ":"))
    return "pl" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def write_measure_version(store: Any, run_id: str, measure: dict[str, Any], *, override: Mapping[str, Any] | None = None, source_group_ids: Sequence[str] = ()) -> int:
    """Write a new version; with `override` the entry is appended to the
    output's `overrides[]` first ({tool, args, by, user_id, at})."""
    out = dict(measure)
    if override is not None:
        ov = {"tool": override.get("tool"), "args": dict(override.get("args") or {}), "by": override.get("by") or "model", "user_id": override.get("user_id"), "at": override.get("at") or now_pacific_iso()}
        out["overrides"] = [dict(o) for o in out.get("overrides") or []] + [ov]
    return store.write(run_id, out, source_group_ids=source_group_ids)


def _override_allowed(existing: Iterable[Mapping[str, Any]], new: Mapping[str, Any]) -> bool:
    """A model override never replaces a human one for the same target."""
    if new.get("by") == "human":
        return True
    tool = new.get("tool")
    args = new.get("args") or {}
    for ov in existing:
        if ov.get("tool") != tool or ov.get("by") != "human":
            continue
        a = ov.get("args") or {}
        if tool == "set_panel_location" and str(a.get("panel", "")).upper() == str(args.get("panel", "")).upper():
            return False
        if tool == "set_linear_item" and (a.get("document_id"), a.get("page"), str(a.get("tag", "")).upper()) == (args.get("document_id"), args.get("page"), str(args.get("tag", "")).upper()):
            return False
    return True


def recompute_with_overrides(measure: Mapping[str, Any], conv: routes_mod.Conventions, override: Mapping[str, Any]) -> dict[str, Any]:
    """A new measure output from a stored one plus one override, at $0:
    frames, registrations, panels and circuits come from the stored output,
    routes and quantities are recomputed."""
    frames = frames_from_output(measure)
    graph = routes_mod.RegistrationGraph(registrations_from_output(measure))
    overrides = [dict(o) for o in measure.get("overrides") or []] + [dict(override)]
    panels: dict[str, routes_mod.PanelInfo] = {}
    for p in measure.get("panels") or []:
        loc = p.get("location") or {}
        panels[str(p["name"]).upper()] = routes_mod.PanelInfo(name=str(p["name"]).upper(), volts=p.get("volts"), phase=p.get("phase"), existing=p.get("existing"), sheet_key=loc.get("sheet_key"), document_id=loc.get("document_id"), page=loc.get("page"), x=loc.get("x"), y=loc.get("y"), method=loc.get("method"), confidence=float(loc.get("confidence") or 0.0), schedule_id=p.get("schedule_id"))
    apply_panel_overrides(panels, overrides, frames)
    circuits: list[routes_mod.CircuitSpec] = []
    for c in measure.get("circuits") or []:
        devs = [routes_mod.Device(d["document_id"], int(d["page"]), str(d["sheet_key"]), float(d["x"]), float(d["y"]), d.get("tag"), str(d.get("source") or "text_tag"), d.get("fixture_type")) for d in c.get("devices") or []]
        circuits.append(routes_mod.CircuitSpec(circuit_id=str(c["id"]), panel=str(c["panel"]), ckts=[int(n) for n in c.get("numbers") or []], devices=devs, wiring=str(c.get("wiring") or "new"), breaker_status=str(c.get("status") or "unknown"), desc=str(c.get("description") or ""), poles=c.get("poles"), trip_a=c.get("amps"), schedule_awg=c.get("schedule_awg"), load_va=c.get("load_va"), gfci=bool(c.get("gfci")), lighting=bool(c.get("lighting")), keynotes=list(c.get("keynotes") or []), distributed=bool(c.get("distributed"))))
    tagged = routes_mod.synthesize_routes(circuits, panels, frames, graph, conv)
    routes_mod.mark_distributed_routes(tagged, circuits)
    recomputed = {c.circuit_id for c in circuits}
    # The lower tiers (equipment, synthetic untagged, density) keep their
    # stored segments; they are renumbered after the recomputed routes and
    # still count in the quantities and totals. Distributed schedule
    # circuits were just recomputed, so their stored routes are not kept.
    lower: list[routes_mod.CircuitRoute] = []
    seq = len(tagged)
    for r in measure.get("routes") or []:
        if r.get("tier") in ("tagged", "tie_in") or str(r.get("circuit_id") or "") in recomputed:
            continue
        seq += 1
        route = routes_mod.CircuitRoute.from_dict(r)
        route.route_id = f"r{seq}"
        lower.append(route)
    routes = tagged + lower
    quantities = routes_mod.branch_quantities(routes, conv)
    linear_items = json.loads(json.dumps(measure.get("linear_items") or []))
    apply_linear_overrides(linear_items, overrides, frames)
    mseq = len(quantities)
    for item in linear_items:
        if item.get("total_ft"):
            mseq += 1
            quantities.append({"row_id": make_row_id("M", mseq), "group": item["group"], "subgroup": item["subgroup"], "description": item["description"], "quantity": round(float(item["total_ft"]), 2), "unit": "FT", "wastage_applies": False, "basis": item["basis"], "caveat": None if item["basis"] == "measured_route" else ASSUMED_CAVEAT, "sheet_refs": [item["sheet_key"]], "refs": [f"m:{item['measure_id']}"], "derivation": linear_derivation(item), "line_hint": {"area": item["area"], "group": item["group"], "subgroup": item["subgroup"], "tag": item.get("tag"), "replaces_unit": "EA", "kind": item["kind"]}, "material_category": item.get("material_category")})
            item["row_id"] = quantities[-1]["row_id"]
    for q in measure.get("quantities") or []:
        if (q.get("line_hint") or {}).get("kind") == "control_cable":
            mseq += 1
            quantities.append({**dict(q), "row_id": make_row_id("M", mseq)})
    out = dict(measure)
    out["routes"] = [r.to_dict() for r in routes]
    out["panels"] = [p.to_dict() for _n, p in sorted(panels.items())]
    out["linear_items"] = linear_items
    out["quantities"] = quantities
    out["totals"] = routes_mod.route_totals(routes)
    out["overrides"] = overrides
    out["stats"] = {**dict(measure.get("stats") or {}), "routes": len(routes), "quantities": len(quantities)}
    out["built_at"] = now_pacific_iso()
    return stabilize_ids(out, measure)


class MeasureService:
    """The WS3 MeasureService of plans_reader_tools (5.3): reads the stored
    measure output of a run (memory or DB store) and answers the six tools.
    `register_run` seeds a run in memory (tests, replay, the derive job
    right after it wrote the output)."""

    def __init__(self, store: Any | None = None, *, conv: routes_mod.Conventions | None = None) -> None:
        self.store = store if store is not None else MemoryMeasureStore()
        self._conv = conv
        self._by_document: dict[str, str] = {}

    @property
    def conv(self) -> routes_mod.Conventions:
        if self._conv is None:
            self._conv = routes_mod.Conventions.from_settings()
        return self._conv

    def register_run(self, run_id: str, measure: dict[str, Any], *, source_group_ids: Sequence[str] = ()) -> int:
        """Write a new version of the run's measure output; its M and r ids
        persist from the stored latest version (stabilize_ids)."""
        prior = self.store.latest(run_id)
        measure = stabilize_ids(dict(measure), prior[1] if prior else None)
        version = self.store.write(run_id, measure, source_group_ids=source_group_ids)
        for s in measure.get("scales") or []:
            self._by_document[str(s.get("document_id"))] = run_id
        return version

    def run_for_document(self, document_id: str | None) -> str | None:
        return self._by_document.get(str(document_id)) if document_id else None

    def latest(self, run_id: str) -> tuple[int, dict[str, Any]] | None:
        got = self.store.latest(run_id)
        if got is not None:
            for s in got[1].get("scales") or []:
                self._by_document.setdefault(str(s.get("document_id")), run_id)
        return got

    def _frame(self, measure: Mapping[str, Any], document_id: str, page: int) -> routes_mod.SheetFrame | None:
        for fr in frames_from_output(measure).values():
            if fr.document_id == document_id and fr.page == int(page):
                return fr
        return None

    # Protocol -----------------------------------------------------------------------------

    def measure_polyline(self, document_id: str, page: int, points: list[list[float]], vertical_ft: float | None, *, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or self.run_for_document(document_id)
        latest = self.latest(run_id) if run_id else None
        if latest is None:
            return {"error": "unavailable", "service": "measure", "message": "no measure output for this run yet"}
        frame = self._frame(latest[1], document_id, page)
        if frame is None:
            return {"error": "unknown_page", "document_id": document_id, "page": page, "message": "not a measured plan sheet"}
        res = polyline_length(frame, points, vertical_ft)
        measure_id = polyline_measure_id(run_id, document_id, page, points, vertical_ft)
        out = {"measure_id": measure_id, "ref": f"m:{measure_id}", "length_ft": res["length_ft"], "horizontal_ft": res["horizontal_ft"], "vertical_ft": vertical_ft, "scale": {"text": None, "ft_per_in": res["ft_per_in"], "view_id": None, "source": "measure_output"}, "sheet_key": frame.sheet_key, "flags": res["flags"]}
        if res["length_ft"] is not None:
            entry = {"measure_id": measure_id, "document_id": document_id, "page": int(page), "points": [[round(float(x), 4), round(float(y), 4)] for x, y in points], "vertical_ft": vertical_ft, "length_ft": res["length_ft"], "horizontal_ft": res["horizontal_ft"], "ft_per_in": res["ft_per_in"], "sheet_key": frame.sheet_key, "flags": list(res["flags"]), "at": now_pacific_iso()}
            appender = getattr(self.store, "append_polyline", None)
            try:
                stored = bool(appender(run_id, entry)) if callable(appender) else False
            except Exception:  # noqa: BLE001 - the length is still returned; the ref is reissued identically next time
                logger.exception("plans_reader_measure: storing polyline %s failed", measure_id)
                stored = False
            out["stored"] = stored
        return out

    def resolve_measurement(self, run_id: str, measure_id: str) -> dict[str, Any] | None:
        """Resolve an `m:<measure_id>` ref of the run: a stored polyline, a
        linear item or a measured run of the latest measure output (the
        entry plus `source`: polylines, linear_items or measured_runs)."""
        latest = self.latest(run_id)
        if latest is None:
            return None
        mid = measure_id[2:] if measure_id.startswith("m:") else measure_id
        for key in ("polylines", "linear_items", "measured_runs"):
            for it in latest[1].get(key) or []:
                if isinstance(it, Mapping) and str(it.get("measure_id")) == mid:
                    return {**dict(it), "source": key}
        return None

    def synthesize_routes(self, document_id: str | None, page: int | None, circuits: list[str] | None, *, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or self.run_for_document(document_id)
        latest = self.latest(run_id) if run_id else None
        if latest is None:
            return {"error": "unavailable", "service": "measure", "message": "no measure output for this run yet"}
        measure = latest[1]
        wanted = {c.upper() for c in circuits or []}
        routes = []
        for r in measure.get("routes") or []:
            if wanted and str(r.get("circuit_id") or "").upper() not in wanted:
                continue
            if document_id and not any(s.get("document_id") == document_id for s in r.get("segments") or []):
                continue
            if page and not any(s.get("page") == page for s in r.get("segments") or []):
                continue
            routes.append(
                {
                    "route_id": r.get("route_id"),
                    "ref": r.get("ref"),
                    "circuit": r.get("circuit_id"),
                    "panel": r.get("panel"),
                    "devices": r.get("devices"),
                    "length_ft": r.get("total_ft"),
                    "collector_ft": r.get("collector_ft"),
                    "home_run_ft": r.get("home_run_ft"),
                    "basis": r.get("basis"),
                    "caveat": r.get("caveat"),
                    "conductors": sorted({s.get("conductors") for s in r.get("segments") or [] if s.get("conductors")}),
                    "flags": r.get("flags"),
                    "line_ids": r.get("line_ids") or [],
                }
            )
        return {"version": latest[0], "routes": routes, "totals": measure.get("totals") or {}, "quantities": [{"row_id": q.get("row_id"), "description": q.get("description"), "quantity": q.get("quantity"), "unit": q.get("unit"), "basis": q.get("basis")} for q in measure.get("quantities") or []], "caveats": [f.get("message") for f in measure.get("findings") or [] if f.get("severity") in ("warn", "block")]}

    # Extra service methods ------------------------------------------------------------------

    def explain_quantity(self, run_id: str, row_id: str) -> dict[str, Any]:
        latest = self.latest(run_id)
        if latest is None:
            return {"error": "unavailable", "service": "measure", "message": "no measure output for this run yet"}
        measure = latest[1]
        row = next((q for q in measure.get("quantities") or [] if q.get("row_id") == row_id), None)
        if row is None:
            return {"error": "unknown_row", "row_id": row_id}
        route_ids = {t.get("route_id") for t in (row.get("derivation") or {}).get("terms") or [] if t.get("route_id")}
        routes = [r for r in measure.get("routes") or [] if r.get("route_id") in route_ids]
        sheets = set(row.get("sheet_refs") or [])
        scales = [s for s in measure.get("scales") or [] if s.get("sheet_key") in sheets]
        regs = [r for r in measure.get("registrations") or [] if r.get("src") in sheets or r.get("dst") in sheets]
        return {"row": {k: row.get(k) for k in ("row_id", "description", "quantity", "unit", "basis", "caveat", "sheet_refs", "refs")}, "derivation": row.get("derivation"), "routes": [{"route_id": r.get("route_id"), "circuit": r.get("circuit_id"), "segments": [{"kind": s.get("kind"), "length_ft": s.get("length_ft"), "conductors": s.get("conductors"), "raceway": s.get("raceway"), "sheet_key": s.get("sheet_key"), "basis": s.get("basis")} for s in r.get("segments") or []], "flags": r.get("flags")} for r in routes], "scales": [{"sheet_key": s.get("sheet_key"), "ft_per_in": s.get("ft_per_in"), "status": s.get("status")} for s in scales], "registrations": [{"src": r.get("src"), "dst": r.get("dst"), "method": r.get("method"), "confidence": r.get("confidence"), "transform": r.get("transform")} for r in regs], "conventions": measure.get("conventions")}

    def find_linear_items(self, run_id: str, document_id: str, page: int, tag: str, *, segments: geometry.SegmentIndex | None = None) -> dict[str, Any]:
        latest = self.latest(run_id)
        if latest is None:
            return {"error": "unavailable", "service": "measure", "message": "no measure output for this run yet"}
        measure = latest[1]
        frame = self._frame(measure, document_id, page)
        if frame is None:
            return {"error": "unknown_page", "document_id": document_id, "page": page}
        item = next((it for it in measure.get("linear_items") or [] if it.get("document_id") == document_id and it.get("page") == int(page) and str(it.get("tag") or "").upper() == tag.upper()), None)
        candidates: list[dict[str, Any]] = []
        if segments is not None:
            pts = item.get("tag_points") if item else []
            scale = frame.scale_for(float(pts[0][0]), float(pts[0][1])) if pts else frame.ft_per_in
            boxes = []
            for p in pts or [[0.5, 0.5]]:
                x, y = float(p[0]), float(p[1])
                boxes.append((x - RECT_BOX_IN[0] / 2 / frame.w_in, y - RECT_BOX_IN[1] / 2 / frame.h_in, x + RECT_BOX_IN[0] / 2 / frame.w_in, y + RECT_BOX_IN[1] / 2 / frame.h_in))
            seen: set[tuple[float, ...]] = set()
            for box in boxes:
                for cnd in rect_candidates(segments.segments, w_in=frame.w_in, h_in=frame.h_in, box=box):
                    key = tuple(round(v, 4) for v in cnd.box)
                    if key in seen:
                        continue
                    seen.add(key)
                    cnd.cand_id = f"c{len(candidates) + 1}"
                    candidates.append(cnd.to_dict(scale))
        elif item is not None:
            candidates = [{"id": i.get("candidate_id") or f"c{k + 1}", "bbox": i.get("bbox"), "length_ft": i.get("length_ft")} for k, i in enumerate(item.get("instances") or [])]
        return {"tag": tag.upper(), "sheet_key": frame.sheet_key, "candidates": candidates, "chosen": [i.get("candidate_id") for i in (item or {}).get("instances") or []], "total_ft": (item or {}).get("total_ft"), "unmatched": (item or {}).get("unmatched") or []}

    def set_linear_item(self, run_id: str, args: Mapping[str, Any], *, by: str = "model", user_id: str | None = None) -> dict[str, Any]:
        latest = self.latest(run_id)
        if latest is None:
            return {"refused": True, "code": "unavailable", "message": "no measure output for this run yet"}
        measure = latest[1]
        frame = self._frame(measure, str(args.get("document_id")), int(args.get("page") or 0))
        if frame is None:
            return {"refused": True, "code": "unknown_page", "message": "not a measured plan sheet", "detail": {"document_id": args.get("document_id"), "page": args.get("page")}}
        override = {"tool": "set_linear_item", "args": dict(args), "by": by, "user_id": user_id, "at": now_pacific_iso()}
        if not _override_allowed(measure.get("overrides") or [], override):
            return {"refused": True, "code": "human_override_exists", "message": "a human already set this item; a model override never replaces it"}
        wanted = set(args.get("candidate_ids") or [])
        item = next((it for it in measure.get("linear_items") or [] if it.get("document_id") == args.get("document_id") and it.get("page") == int(args.get("page") or 0) and str(it.get("tag") or "").upper() == str(args.get("tag") or "").upper()), None)
        known = {i.get("candidate_id") for i in (item or {}).get("instances") or []}
        if wanted and not args.get("endpoints") and not (wanted & known):
            return {"refused": True, "code": "unknown_candidate", "message": "none of the candidate ids is known for this tag", "detail": {"known": sorted(k for k in known if k)}}
        new = recompute_with_overrides(measure, self.conv, override)
        version = self.store.write(run_id, new)
        item = next((it for it in new.get("linear_items") or [] if it.get("document_id") == args.get("document_id") and it.get("page") == int(args.get("page") or 0) and str(it.get("tag") or "").upper() == str(args.get("tag") or "").upper()), None)
        return {"version": version, "total_ft": (item or {}).get("total_ft"), "instances": [{"id": i.get("candidate_id") or f"e{k + 1}", "length_ft": i.get("length_ft")} for k, i in enumerate((item or {}).get("instances") or [])], "row_id": (item or {}).get("row_id")}

    def set_panel_location(self, run_id: str, args: Mapping[str, Any], *, by: str = "model", user_id: str | None = None) -> dict[str, Any]:
        latest = self.latest(run_id)
        if latest is None:
            return {"refused": True, "code": "unavailable", "message": "no measure output for this run yet"}
        measure = latest[1]
        frame = self._frame(measure, str(args.get("document_id")), int(args.get("page") or 0))
        if frame is None:
            return {"refused": True, "code": "unknown_page", "message": "not a measured plan sheet", "detail": {"document_id": args.get("document_id"), "page": args.get("page")}}
        name = str(args.get("panel") or "").upper()
        if not any(str(p.get("name") or "").upper() == name for p in measure.get("panels") or []) and not any(str(c.get("panel") or "").upper() == name for c in measure.get("circuits") or []):
            return {"refused": True, "code": "unknown_panel", "message": f"panel {name} is not in the schedules or the circuit tags"}
        override = {"tool": "set_panel_location", "args": dict(args), "by": by, "user_id": user_id, "at": now_pacific_iso()}
        if not _override_allowed(measure.get("overrides") or [], override):
            return {"refused": True, "code": "human_override_exists", "message": "a human already placed this panel; a model override never replaces it"}
        before = {r.get("circuit_id"): r.get("total_ft") for r in measure.get("routes") or [] if str(r.get("panel") or "").upper() == name}
        new = recompute_with_overrides(measure, self.conv, override)
        version = self.store.write(run_id, new)
        after = {r.get("circuit_id"): r.get("total_ft") for r in new.get("routes") or [] if str(r.get("panel") or "").upper() == name}
        changed = [c for c in after if abs(float(after[c] or 0) - float(before.get(c) or 0)) > 0.05]
        delta = round(sum(float(after[c] or 0) for c in after) - sum(float(before.get(c) or 0) for c in before), 1)
        return {"version": version, "routes_changed": len(changed), "delta_ft": delta, "panel": name}


_SERVICE: MeasureService | None = None


def service() -> MeasureService:
    """The process-wide MeasureService (memory store until the integrator
    binds the DB store in the derive wiring)."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = MeasureService()
    return _SERVICE


def install_service(svc: MeasureService) -> MeasureService:
    """Replace the process-wide service and register it with the tool
    registry (`register_services(measure=...)`)."""
    global _SERVICE
    _SERVICE = svc
    register_services(measure=svc)
    return svc


# Tools ----------------------------------------------------------------------------------------------


def _svc_for(ctx: ToolContext) -> MeasureService:
    svc = ctx.svc().measure
    return svc if isinstance(svc, MeasureService) else service()


_POINTS_ARG = _arr(_arr(_num()))

MEASURE_POLYLINE_ARGS = _obj({"document_id": _str(), "page": _int(), "points": _POINTS_ARG, "vertical_ft": _num(True)})
SYNTHESIZE_ROUTES_ARGS = _obj({"document_id": _str(True), "page": _int(True), "circuits": _nullable(_arr(_str()))})
FIND_LINEAR_ITEMS_ARGS = _obj({"document_id": _str(), "page": _int(), "tag": _str()})
EXPLAIN_QUANTITY_ARGS = _obj({"row_id": _str()})


def _tool_measure_polyline(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = _svc_for(ctx)
    res = svc.measure_polyline(args["document_id"], int(args["page"]), args["points"], args.get("vertical_ft"), run_id=ctx.run_id)
    if res.get("error"):
        return ToolResult.refusal(str(res["error"]), str(res.get("message") or res["error"]), res)
    return ToolResult.ok(res, citations=[res["ref"]] if res.get("length_ft") is not None else [])


def _tool_synthesize_routes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = _svc_for(ctx)
    res = svc.synthesize_routes(args.get("document_id"), args.get("page"), args.get("circuits"), run_id=ctx.run_id)
    if res.get("error"):
        return ToolResult.refusal(str(res["error"]), str(res.get("message") or res["error"]), res)
    cites = [r["ref"] for r in res.get("routes") or [] if r.get("ref")] + [q["row_id"] for q in res.get("quantities") or [] if q.get("row_id")]
    return ToolResult.ok(res, citations=cites)


def _overlay_png(ctx: ToolContext, document_id: str, page: int, frame: routes_mod.SheetFrame, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A numbered candidate overlay (Set-of-Marks) over a 150 ppi crop around
    the candidates; None when the PDF or the renderer is unavailable."""
    try:
        from io import BytesIO

        from PIL import Image, ImageDraw

        from app.services import plans_reader_images as images

        pdf = ctx.svc().documents.pdf_bytes(document_id)
        if not isinstance(pdf, (bytes, bytearray)) or not candidates:
            return None
        xs = [b for c in candidates for b in (c["bbox"][0], c["bbox"][2])]
        ys = [b for c in candidates for b in (c["bbox"][1], c["bbox"][3])]
        pad_x, pad_y = 1.0 / frame.w_in, 1.0 / frame.h_in
        region = (max(0.0, min(xs) - pad_x), max(0.0, min(ys) - pad_y), min(1.0, max(xs) + pad_x), min(1.0, max(ys) + pad_y))
        ppi = 150
        w_px, h_px = round(frame.w_in * ppi), round(frame.h_in * ppi)
        box = images.box_from_fractions(region, w_px, h_px)
        png, cw, ch = images.render_page_png(bytes(pdf), int(page) - 1, ppi, box)
        img = Image.open(BytesIO(png)).convert("RGB")
        draw = ImageDraw.Draw(img)
        for i, c in enumerate(candidates, start=1):
            bx0 = (c["bbox"][0] - region[0]) / (region[2] - region[0]) * cw
            by0 = (c["bbox"][1] - region[1]) / (region[3] - region[1]) * ch
            bx1 = (c["bbox"][2] - region[0]) / (region[2] - region[0]) * cw
            by1 = (c["bbox"][3] - region[1]) / (region[3] - region[1]) * ch
            draw.rectangle([bx0 - 2, by0 - 2, bx1 + 2, by1 + 2], outline=(220, 30, 30), width=3)
            draw.text((bx0, max(0, by0 - 14)), str(i), fill=(220, 30, 30))
        out = BytesIO()
        img.save(out, format="PNG", optimize=True)
        return {"view_id": f"lin:{document_id[:8]}:{page}", "bbox": [round(v, 4) for v in region], "ppi": ppi, "image": out.getvalue(), "mime": "image/png"}
    except Exception:  # noqa: BLE001 - the overlay is best effort
        logger.exception("plans_reader_measure: overlay failed")
        return None


def _tool_find_linear_items(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = _svc_for(ctx)
    latest = svc.latest(ctx.run_id)
    segments = None
    if latest is not None:
        pdf = ctx.svc().documents.pdf_bytes(args["document_id"])
        if isinstance(pdf, (bytes, bytearray)):
            try:
                segments = geometry.index_segments(bytes(pdf), int(args["page"]) - 1)
            except Exception:  # noqa: BLE001
                segments = None
    res = svc.find_linear_items(ctx.run_id, args["document_id"], int(args["page"]), args["tag"], segments=segments)
    if res.get("error"):
        return ToolResult.refusal(str(res["error"]), str(res.get("message") or res["error"]), res)
    views = []
    if latest is not None:
        frame = svc._frame(latest[1], args["document_id"], int(args["page"]))
        if frame is not None:
            overlay = _overlay_png(ctx, args["document_id"], int(args["page"]), frame, [c for c in res.get("candidates") or [] if c.get("bbox")])
            if overlay is not None:
                views.append(overlay)
                res["overlay_view_id"] = overlay["view_id"]
    return ToolResult.ok(res, views=views)


def _tool_explain_quantity(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = _svc_for(ctx)
    res = svc.explain_quantity(ctx.run_id, args["row_id"])
    if res.get("error"):
        return ToolResult.refusal(str(res["error"]), str(res.get("message") or res["error"]), res)
    return ToolResult.ok(res)


def _writer_ok(ctx: ToolContext) -> ToolResult | None:
    """Chat writes need a writer role AND change words in the user's own
    message, and a human override carries the user id (spec 5.2, 5.8).
    The chat adapter sets `user_id` (the message author), `message_text`
    (the user's own question) and `change_words` on the `ToolContext`; a
    chat write is refused unless both are set; derive-group writes are the
    app's own."""
    if ctx.kind != "chat":
        return None
    try:
        from app.core.roles import WRITER_ROLES

        allowed = {str(getattr(r, "value", r)) for r in WRITER_ROLES}
    except Exception:  # noqa: BLE001
        allowed = set()
    if not ctx.role or str(ctx.role).lower() not in allowed:
        return ToolResult.refusal("writer_role_required", "this change needs a writer role")
    user_id = getattr(ctx, "user_id", None)
    text = getattr(ctx, "message_text", None)
    if not user_id or not isinstance(text, str):
        return ToolResult.refusal("chat_write_unavailable", "measure changes from chat need the user's id and message, which this chat does not pass yet; ask a writer to make the change in the app")
    from app.services.plans_reader_chat import asks_for_change

    if not asks_for_change(text):
        return ToolResult.refusal("change_words_required", "the user's message does not ask for a change; confirm the change first")
    return None


def _tool_set_linear_item(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    refusal = _writer_ok(ctx)
    if refusal is not None:
        return refusal
    svc = _svc_for(ctx)
    by = "human" if ctx.kind == "chat" else "model"
    res = svc.set_linear_item(ctx.run_id, args, by=by, user_id=getattr(ctx, "user_id", None))
    if res.get("refused"):
        return ToolResult.refusal(str(res["code"]), str(res["message"]), res.get("detail"))
    return ToolResult.ok(res)


def _tool_set_panel_location(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    refusal = _writer_ok(ctx)
    if refusal is not None:
        return refusal
    svc = _svc_for(ctx)
    by = "human" if ctx.kind == "chat" else "model"
    res = svc.set_panel_location(ctx.run_id, args, by=by, user_id=getattr(ctx, "user_id", None))
    if res.get("refused"):
        return ToolResult.refusal(str(res["code"]), str(res["message"]), res.get("detail"))
    return ToolResult.ok(res)


MEASURE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="measure_polyline",
        description="Length in feet of a polyline drawn in page fractions on a plan sheet, at that sheet's printed scale (plus an optional vertical term). Refuses on NTS sheets, scale conflicts and polylines that cross viewports.",
        args_schema=MEASURE_POLYLINE_ARGS,
        result_doc={"measure_id": "str", "ref": "m:<id>", "length_ft": "number or null", "scale": {"ft_per_in": "number", "source": "measure_output"}, "flags": ["str"]},
        mode="read",
        kinds=kinds_from_codes("RC, M, RV, RS, D, C"),
        handler=_tool_measure_polyline,
        max_calls_per_group=40,
    ),
    ToolSpec(
        name="synthesize_routes",
        description="The app's synthesized branch routes (collector plus home run per circuit) from the stored measure output: per-route lengths, basis, conductors, totals by size and raceway, and the M quantity rows.",
        args_schema=SYNTHESIZE_ROUTES_ARGS,
        result_doc={"routes": [{"route_id": "str", "ref": "r:<id>", "circuit": "str", "length_ft": "number", "basis": "str"}], "totals": "dict", "quantities": "[M rows]", "caveats": ["str"]},
        mode="read",
        kinds=kinds_from_codes("M, RV, RS, C"),
        handler=_tool_synthesize_routes,
        max_calls_per_group=10,
    ),
    ToolSpec(
        name="find_linear_items",
        description="Vector rectangle candidates for a linear fixture tag (undercabinet, strip, track) on a sheet, numbered on an overlay image, with the candidates the app chose.",
        args_schema=FIND_LINEAR_ITEMS_ARGS,
        result_doc={"candidates": [{"id": "str", "bbox": "[x0,y0,x1,y1]", "length_ft": "number"}], "chosen": ["candidate ids"], "total_ft": "number or null"},
        mode="read",
        kinds=kinds_from_codes("D, RV, C"),
        handler=_tool_find_linear_items,
        cost_class="image",
        needs_pro_max=True,
        max_calls_per_group=8,
        timeout_s=60.0,
    ),
    ToolSpec(
        name="explain_quantity",
        description="How an M quantity row was derived: the terms per circuit and segment, the scales and registrations used, the conventions.",
        args_schema=EXPLAIN_QUANTITY_ARGS,
        result_doc={"row": "the M row", "derivation": {"terms": "[...]"}, "routes": "[...]", "scales": "[...]", "registrations": "[...]"},
        mode="read",
        kinds=kinds_from_codes("M, RV, RS, C"),
        handler=_tool_explain_quantity,
        max_calls_per_group=40,
    ),
    ToolSpec(
        name="set_linear_item",
        description="Set which candidates (or explicit endpoints) measure a linear fixture tag on a sheet; writes a new measure output version with an override. A human choice is never replaced by a model.",
        args_schema=MEASURE_WRITE_TOOL_ARGS["set_linear_item"],
        result_doc={"version": "int", "total_ft": "number", "instances": [{"id": "str", "length_ft": "number"}]},
        mode="write",
        kinds=kinds_from_codes("D, C"),
        handler=_tool_set_linear_item,
        max_calls_per_group=8,
    ),
    ToolSpec(
        name="set_panel_location",
        description="Place a panel at a point on a sheet; recomputes every route of that panel and writes a new measure output version with an override. A human placement is never replaced by a model.",
        args_schema=MEASURE_WRITE_TOOL_ARGS["set_panel_location"],
        result_doc={"version": "int", "routes_changed": "int", "delta_ft": "number"},
        mode="write",
        kinds=kinds_from_codes("D, C"),
        handler=_tool_set_panel_location,
        max_calls_per_group=8,
    ),
)


def register_tools() -> None:
    """Put the WS3 tools in REGISTRY (idempotent)."""
    for spec in MEASURE_TOOLS:
        register(spec, replace=True)


register_tools()


# Replay stage ------------------------------------------------------------------------------------------


def _replay_derive(ctx: Any, art: dict[str, Any]) -> dict[str, Any]:
    """The measure part of the `derive` stage for the $0 replay: WS1 facts
    extracted from the bundle's drawing PDFs, category outputs from the
    stored artifacts, scope inclusions from a stored policy output when
    present. Returns {"measure": output} and keeps a `policy` artifact."""
    from app.services.pagefacts import extract as pf_extract

    documents: list[MeasureDocument] = []
    pdfs: dict[str, bytes] = {}
    for i, d in enumerate(art.get("documents") or [], start=1):
        if str(d.get("category") or "") not in ("electrical_drawings", "drawings", "drawing"):
            continue
        doc_id = str(d.get("id"))
        try:
            pdf = ctx.docs.get(doc_id)
        except Exception:  # noqa: BLE001 - a missing file measures nothing
            ctx.note(f"derive: document {doc_id[:8]} has no local bytes")
            continue
        facts = pf_extract.extract_document(pdf, label="electrical_drawings", name=d.get("name"), page_budget_s=4.0, doc_budget_s=240.0)
        documents.append(MeasureDocument(document_id=doc_id, doc_index=i, facts=facts, label="electrical_drawings", name=d.get("name")))
        pdfs[doc_id] = pdf
    category_outputs = {k.split(":", 1)[1]: v for k, v in art.items() if k.startswith("category:") and isinstance(v, Mapping)}
    # WS5's chained derive hands its fresh ScopeContext as `scope_context`; a stored run has `policy`.
    ctx_art = art.get("scope_context") if isinstance(art.get("scope_context"), Mapping) else art.get("policy")
    scope = ScopeInputs.from_scope_context(ctx_art if isinstance(ctx_art, Mapping) else None)
    conv = routes_mod.Conventions.from_settings(**{k.replace("plans_reader_", ""): v for k, v in (ctx.settings_overrides or {}).items() if k.startswith("plans_reader_") and k.replace("plans_reader_", "") in routes_mod.Conventions.__dataclass_fields__})
    inputs = MeasureInputs.from_pdfs(art.get("run_id") or (ctx.bundle.run_id if getattr(ctx, "bundle", None) else None), documents, pdfs, category_outputs=category_outputs, scope=scope, eval_case_id=getattr(ctx, "case_id", None), prior=art.get("measure") if isinstance(art.get("measure"), Mapping) else None)
    measure = build_measure(inputs, conv)
    out: dict[str, Any] = {"measure": measure}
    if "policy" in art:
        out["policy"] = art["policy"]
    return out


def register_replay_stage() -> None:
    try:
        from app.services import plans_reader_replay as replay

        replay.register_stage("derive", _replay_derive, version=STAGE_VERSION, reads=("documents", "category:*", "policy", "scope_context", "measure"), writes=("measure",), owner="WS3+WS5")
    except Exception:  # noqa: BLE001 - replay is optional at import
        logger.exception("plans_reader_measure: replay stage registration failed")


register_replay_stage()


__all__ = [
    "MeasureDocument",
    "ScopeInputs",
    "MeasureInputs",
    "WeightedSegment",
    "heavy_segments",
    "merge_runs",
    "RectCandidate",
    "rect_candidates",
    "measure_linear_tag",
    "measure_track",
    "build_frames",
    "build_panels",
    "build_circuits",
    "build_registrations",
    "build_measure",
    "build_measure_safe",
    "density_only_measure",
    "needs_vectors",
    "polyline_length",
    "measure_summary_text",
    "apply_measure_to_boq",
    "branch_coverage_findings",
    "MemoryMeasureStore",
    "DbMeasureStore",
    "write_measure_version",
    "recompute_with_overrides",
    "MeasureService",
    "service",
    "install_service",
    "MEASURE_TOOLS",
    "register_tools",
    "register_replay_stage",
]
