"""Text-layer anchors for the E17 marked-plans check
(docs/AI_ESTIMATING_BUILD_SPEC.md 3.5 E17; WS0 step 9).

E17 scores the marks of a marked-plans spec by their distance to the nearest
text-layer anchor of the mark's tag: a fixture tag (L1, X1), a circuit tag
(1EELC-1, LA-3), an equipment tag (FSD-1, VV-2), a switch letter (S, D, OC)
or a demolition "(X)" run, read from the sheet's positioned words through
WS1's `pagefacts` package (words plus the page facts for the plan area and
the view scale). Distances are in feet at the sheet's primary scale.

The module is pure over PDF bytes and never calls a model. When pagefacts
cannot be imported the check skips (`available()` is False). A page without a
usable text layer answers `method: "unavailable"` so the check skips that
sheet instead of measuring against nothing.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

try:  # WS1 ships pagefacts; the check skips when it is absent
    from app.services.pagefacts import extract as _extract
    from app.services.pagefacts.contracts import text_layer_quality_for as _quality_for
except Exception:  # noqa: BLE001 - optional dependency at import time
    _extract = None
    _quality_for = None

METHOD_TEXT_LAYER = "text_layer"
METHOD_UNAVAILABLE = "unavailable"
NON_PLAN_REGION_KINDS: frozenset[str] = frozenset(
    {"title_block", "legend", "abbreviations", "schedule", "general_notes", "sheet_notes", "keynotes",
     "key_plan", "revision_block"}
)
FAMILIES: tuple[str, ...] = ("fixture", "circuit", "equipment", "switch", "demo_x")
STACK_WITHIN_FT = 0.25
# Mark kinds that are run geometry, not count marks: they are not measured
# against anchors and do not stack.
POLYLINE_KINDS: frozenset[str] = frozenset({"polyline", "line", "run", "path"})

_DEMO_X_RE = re.compile(r"^(?:\(X\))+$|^X\)$")
_FIXTURE_RE = re.compile(r"^(?P<t>[A-Z]{1,2}\d{1,2})(?P=t)?(?:-?EM|/NL|E)?$")
_CIRCUIT_RE = re.compile(r"^(?P<p>\d[A-Z]{1,5}|[A-Z]{1,5})-\d{1,3}(?:[,/]\d{1,3})*$|^(?P<q>\d[A-Z]{2,5})$")
_EQUIPMENT_RE = re.compile(r"^(?P<e>FSD|SFD|VV|VAV|AHU|RTU|EF|FCU|HP|EWH|WH|DF|GFI|GFCI|TS|T)-?\d{0,3}[A-Z]?(?:-[A-Z]{1,2})?$")
_SWITCH_RE = re.compile(r"^(?P<s>S|D|OC|OS|OCC|VS|VSD|K)[0-9A-Z]{0,2}$")
_LEGEND_FIXTURE_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2})(?:-?EM|/NL|E)?\b")
_LEGEND_DEMO_RE = re.compile(r"\bREMOV|\bDEMO")
_LEGEND_CIRCUIT_RE = re.compile(
    r"RECEPT|DUPLEX|QUAD|GFCI|GFI|OUTLET|CONNECTION|DAMPER|\bVAV\b|\bSFD\b|\bFSD\b|SENSOR|RELOCAT|EQUIPMENT|PANEL|TRANSFORMER"
)
_LEGEND_SWITCH_RE = re.compile(r"DIMMER|SWITCH|OCCUPANCY|VACANCY")


def available() -> bool:
    return _extract is not None


@dataclass(frozen=True)
class TextAnchor:
    family: str
    tag: str
    text: str
    x: float
    y: float


@dataclass
class SheetAnchors:
    document_id: str
    page: int
    w_in: float
    h_in: float
    ft_per_in: float | None
    sheet_number: str | None
    method: str
    text_layer_quality: str
    anchors: list[TextAnchor] = field(default_factory=list)
    note: str | None = None

    @property
    def available(self) -> bool:
        return self.method == METHOD_TEXT_LAYER and bool(self.anchors)

    def by_family(self, families: Iterable[str] | None, tags: Iterable[str] | None = None) -> list[TextAnchor]:
        fams = set(families) if families is not None else None
        wanted_tags = set(tags) if tags else None
        out = []
        for a in self.anchors:
            if fams is not None and a.family not in fams:
                continue
            if wanted_tags is not None and a.family == "fixture" and a.tag not in wanted_tags:
                continue
            out.append(a)
        return out


def _classify_piece(t: str) -> tuple[str, str] | None:
    if _DEMO_X_RE.match(t):
        return "demo_x", "(X)"
    m = _FIXTURE_RE.match(t)
    if m:
        return "fixture", m.group("t")
    m = _EQUIPMENT_RE.match(t)
    if m:
        return "equipment", m.group("e")
    m = _CIRCUIT_RE.match(t)
    if m:
        return "circuit", m.group("p") or m.group("q")
    m = _SWITCH_RE.match(t)
    if m:
        return "switch", m.group("s")
    return None


_FAMILY_RANK = {f: i for i, f in enumerate(FAMILIES)}


def classify_token(text: str) -> tuple[str, str] | None:
    """(family, tag) of one positioned word, or None when it is not a
    device-like tag. A slash-joined word ("G/A/1LI-4", "L5/1LH": modifier
    letters, a fixture tag and a circuit) is classified by its pieces, the
    highest-ranked family of FAMILIES winning."""
    t = re.sub(r"[\r\n]", "", str(text or "")).strip().upper()
    if not t:
        return None
    whole = _classify_piece(t)
    if whole is not None:
        return whole
    if "/" not in t:
        return None
    found = [tok for tok in (_classify_piece(piece) for piece in t.split("/") if piece) if tok is not None]
    if not found:
        return None
    return min(found, key=lambda tok: _FAMILY_RANK[tok[0]])


_REFERENCE_LEGEND_RE = re.compile(r"reference only|not additive|\bremains?\b.*\breference|not g3|no purchase", re.I)


def is_reference_legend(entry: Mapping[str, Any] | None) -> bool:
    """A legend entry whose marks evidence no BOQ line (existing equipment
    "remain; reference only", duplicates of another sheet "not additive")."""
    if not isinstance(entry, Mapping):
        return False
    return _REFERENCE_LEGEND_RE.search(str(entry.get("description") or "")) is not None


def wanted_for_legend(legend_entry: Mapping[str, Any] | None) -> tuple[set[str] | None, set[str] | None]:
    """(families, fixture tags) a legend entry's marks should sit on, or
    (None, None) when any anchor family will do."""
    if not isinstance(legend_entry, Mapping):
        return None, None
    text = f"{legend_entry.get('prefix') or ''} {legend_entry.get('description') or ''}".upper()
    tags = {m.group(1) for m in _LEGEND_FIXTURE_RE.finditer(text)}
    if tags:
        return {"fixture"}, tags
    if _LEGEND_DEMO_RE.search(text):
        return {"demo_x"}, None
    if _LEGEND_SWITCH_RE.search(text) and not _LEGEND_CIRCUIT_RE.search(text):
        return {"switch"}, None
    if _LEGEND_CIRCUIT_RE.search(text):
        return {"circuit", "equipment"}, None
    return None, None


def _inside(box: Sequence[float], x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AnchorExtractor:
    """Anchors per (document, page) over PDF bytes, with the page facts and
    words cached per document so a marked set costs one extraction."""

    def __init__(self, *, label: str = "electrical_drawings") -> None:
        self.label = label
        self._facts: dict[str, Any] = {}
        self._words: dict[tuple[str, int], Any] = {}

    def _doc(self, pdf: bytes) -> Any:
        key = _sha(pdf)
        if key not in self._facts:
            self._facts[key] = _extract.extract_document(pdf, label=self.label, page_budget_s=8.0, doc_budget_s=600.0)
        return self._facts[key]

    def _page_words(self, pdf: bytes, page: int) -> Any:
        key = (_sha(pdf), page)
        if key not in self._words:
            self._words[key] = _extract.extract_words(pdf, pages=[page])[page]
        return self._words[key]

    def sheet(self, document_id: str, pdf: bytes, page: int, *, ft_per_in_fallback: float | None = None) -> SheetAnchors:
        if _extract is None:
            return SheetAnchors(document_id, page, 0.0, 0.0, None, None, METHOD_UNAVAILABLE, "none",
                                note="pagefacts is not available")
        facts = self._doc(pdf)
        pf = next((p for p in facts.pages if int(p.page) == int(page)), None)
        if pf is None:
            return SheetAnchors(document_id, page, 0.0, 0.0, None, None, METHOD_UNAVAILABLE, "none",
                                note=f"page {page} is outside the document")
        quality = _quality_for(pf.text_class) if _quality_for else "full"
        ft_per_in = pf.primary_ft_per_in
        if ft_per_in is None:
            for v in pf.views:
                if getattr(v, "ft_per_in", None) and not getattr(v, "example", False):
                    ft_per_in = v.ft_per_in
                    break
        if ft_per_in is None:
            ft_per_in = ft_per_in_fallback
        out = SheetAnchors(document_id, page, float(pf.w_in), float(pf.h_in), ft_per_in, pf.sheet_number,
                           METHOD_TEXT_LAYER, quality)
        if quality != "full":
            out.method = METHOD_UNAVAILABLE
            out.note = f"no usable text layer ({pf.text_class})"
            return out
        words = self._page_words(pdf, page)
        plan_boxes = [r.bbox for r in pf.regions if r.kind == "plan_area"]
        non_plan = [r.bbox for r in pf.regions if r.kind in NON_PLAN_REGION_KINDS]
        for w in words.words:
            tok = classify_token(w.t)
            if tok is None:
                continue
            x, y = (w.b[0] + w.b[2]) / 2, (w.b[1] + w.b[3]) / 2
            if plan_boxes and not any(_inside(b, x, y) for b in plan_boxes):
                continue
            if any(_inside(b, x, y) for b in non_plan):
                continue
            out.anchors.append(TextAnchor(tok[0], tok[1], w.t, round(x, 4), round(y, 4)))
        if ft_per_in is None:
            out.note = "no scale on the sheet (distances unavailable)"
        return out


def distance_ft(a: tuple[float, float], b: tuple[float, float], w_in: float, h_in: float, ft_per_in: float) -> float:
    return math.hypot((a[0] - b[0]) * w_in, (a[1] - b[1]) * h_in) * ft_per_in


def mark_distances(
    sheet: SheetAnchors,
    marks: Sequence[Mapping[str, Any]],
    legend: Sequence[Mapping[str, Any]],
    *,
    skip_reference: bool = True,
) -> list[dict[str, Any]]:
    """One row per mark point: `distance_ft` to the nearest anchor of the
    mark's tag (its wanted families; fallback to any anchor when the page
    has none of them, flagged) and `distance_any_ft` to the nearest device
    anchor of any family (the method of the weekend baseline). Marks of a
    reference-only legend entry are skipped unless `skip_reference` is
    False. Empty when the sheet has no scale or no anchors."""
    rows: list[dict[str, Any]] = []
    if sheet.ft_per_in is None or not sheet.available:
        return rows
    for m in marks:
        if not isinstance(m, Mapping):
            continue
        li = m.get("legend_index")
        entry = legend[int(li)] if isinstance(li, int) and 0 <= li < len(legend) else None
        if skip_reference and is_reference_legend(entry):
            continue
        if str(m.get("kind") or "dot") in POLYLINE_KINDS:
            continue
        fams, tags = wanted_for_legend(entry)
        pool = sheet.by_family(fams, tags)
        fallback = False
        if not pool:
            pool = sheet.anchors
            fallback = True
        kind = str(m.get("kind") or "dot")
        for p in m.get("points") or []:
            if not isinstance(p, Sequence) or len(p) < 2:
                continue
            pt = (float(p[0]), float(p[1]))
            best = min(pool, key=lambda a: distance_ft(pt, (a.x, a.y), sheet.w_in, sheet.h_in, sheet.ft_per_in))
            any_best = min(sheet.anchors, key=lambda a: distance_ft(pt, (a.x, a.y), sheet.w_in, sheet.h_in, sheet.ft_per_in))
            rows.append({
                "kind": kind,
                "legend_index": li,
                "point": [pt[0], pt[1]],
                "distance_ft": round(distance_ft(pt, (best.x, best.y), sheet.w_in, sheet.h_in, sheet.ft_per_in), 3),
                "distance_any_ft": round(distance_ft(pt, (any_best.x, any_best.y), sheet.w_in, sheet.h_in, sheet.ft_per_in), 3),
                "anchor": best.text,
                "family": best.family,
                "fallback": fallback,
            })
    return rows


def stacked_duplicates(points: Sequence[Sequence[float]], w_in: float, h_in: float, ft_per_in: float | None,
                       *, within_ft: float = STACK_WITHIN_FT) -> int:
    """Marks that duplicate another mark within `within_ft` on the same
    sheet: the sum of (cluster size - 1) over clusters joined by that
    distance. Without a scale, identical coordinates count."""
    pts = [(float(p[0]), float(p[1])) for p in points if isinstance(p, Sequence) and len(p) >= 2]
    n = len(pts)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if ft_per_in is None:
                close = pts[i] == pts[j]
            else:
                close = distance_ft(pts[i], pts[j], w_in, h_in, ft_per_in) <= within_ft
            if close:
                parent[find(i)] = find(j)
    sizes: dict[int, int] = {}
    for i in range(n):
        r = find(i)
        sizes[r] = sizes.get(r, 0) + 1
    return sum(s - 1 for s in sizes.values() if s > 1)


__all__ = [
    "METHOD_TEXT_LAYER", "METHOD_UNAVAILABLE", "FAMILIES", "STACK_WITHIN_FT", "POLYLINE_KINDS", "available",
    "TextAnchor", "SheetAnchors", "classify_token", "wanted_for_legend", "is_reference_legend", "AnchorExtractor",
    "distance_ft", "mark_distances", "stacked_duplicates",
]
