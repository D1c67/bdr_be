"""Text-layer anchors for code-built marks (docs/AI_ESTIMATING_BUILD_SPEC.md
section 6 WS8, step "anchors"; section 5.1 coordinates).

An anchor is one drawn occurrence of a device or fixture tag on a plan view:
the place a mark goes when a BOQ line counts that tag (placement is WS8 wave
2). Anchors come from WS1 positioned words read through
`plans_reader_evidence_inputs`, never from model coordinates.

Rules (spec WS8 design 6.2, adapted to WS1 words):

1. Coordinates are WS1's displayed frame (after /Rotate, inside the CropBox,
   origin top-left, 4 decimals), so rotated sheets need no extra transform.
2. SHX words (flag 1, harvested from "AutoCAD SHX Text" comments) are words
   like any other; an anchor records `shx` when its words came from SHX.
3. Non-plan areas are excluded: a word whose center is outside WS1's plan
   area or inside a non-plan region (legend, schedule, notes, keynotes, title
   block, key plan, revision block) is never an anchor, so the schedule copy
   of "L1" cannot become a mark. Words in a non-plan view (detail, section,
   elevation, diagram, schedule views from WS1 `views`) are excluded too,
   and so is every word of a sheet whose WS1 roles hold no plan role (a
   schedule or legend sheet: WS1 schedule regions can be narrower than the
   table, as on 7154 E0.04).
4. Token fold: CR and LF removed, uppercased, then
   `^(?P<t>TAG)(?P=t)?(?:/SUFFIX)?$`: doubled text "L1L1" folds to L1, a
   circuit suffix "L5/1LH" folds to L5. The folded token is reduced to its
   base tag (`-EM`, `/NL`, `EM` suffixes dropped) and kept only when the base
   is in the requested tag set (fixture schedule marks and BOQ tags of the
   sheet).
5. Dedupe: the same base tag within 0.15 in on paper is one anchor (CAD
   exports draw tags twice; WS1 keeps both words).
6. Per-view scale: each anchor is assigned to a WS1 view (legend examples
   skipped) and carries that view's `ft_per_in`, so distances in page
   fractions convert to feet with the right scale on a two-scale sheet.
7. A page without a usable text layer (`text_layer_quality` none or
   vector_text, or no words) answers `method: "unavailable"` with counts of
   None, never 0 (text counts are floors; spec 5.1). A sparse page answers
   anchors with `complete: False`.

Optional family `demo_x`: pure "(X)" runs (and a split "X)") become anchors
with tag "(X)", k occurrences in one word spread evenly along its width.

Pure functions; no database, no network, no model, no prices.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.services.pagefacts import regions as regions_mod
from app.services.pagefacts.contracts import (
    F_SHX,
    NON_PLAN_REGION_KINDS,
    PLAN_ROLES,
    Region,
    View,
    Word,
)
from app.services.plans_reader_evidence_inputs import (
    EvidenceInputs,
    FactsUnavailable,
    SheetInputs,
)
from app.services.plans_reader_tools import doc8

DEDUPE_IN = 0.15
# View assignment: a view title and scale sit below their drawing; a title
# counts as "below" a point within this slack, and titles whose left edges are
# within COLUMN_IN of each other share a column.
VIEW_BELOW_SLACK_IN = 0.5
VIEW_LEFT_SLACK_IN = 1.0
VIEW_COLUMN_IN = 2.0

METHOD_TEXT_LAYER = "text_layer"
METHOD_UNAVAILABLE = "unavailable"
UNUSABLE_QUALITIES: frozenset[str] = frozenset({"none", "vector_text"})

PLAN_VIEW_KINDS: frozenset[str] = frozenset({"plan", "enlarged_plan"})
NON_PLAN_VIEW_KINDS: frozenset[str] = frozenset(
    {"detail", "section", "elevation", "diagram", "schedule"}
)

FAMILY_TAG = "tag"
FAMILY_DEMO_X = "demo_x"
DEMO_X_TAG = "(X)"
KNOWN_FAMILIES: frozenset[str] = frozenset({FAMILY_DEMO_X})

EXCLUDED_SHEET = "non_plan_sheet"
EXCLUDED_REGION = "non_plan_region"
EXCLUDED_VIEW = "non_plan_view"

_FOLD_RE = re.compile(r"^(?P<t>[A-Z]{1,3}\d{0,2}(?:-[A-Z]{1,3})?)(?P=t)?(?:/[A-Z0-9-]+)?$")
_BASE_SUFFIX_RE = re.compile(r"(?:-EM|/NL|EM)$")
_DEMO_X_RE = re.compile(r"^(?:\(X\))+$|^X\)$")


# Tags -----------------------------------------------------------------------------


def normalize_tag(tag: str) -> str:
    """The base tag a BOQ line or a schedule mark counts: uppercased, spaces
    removed, `-EM`, `/NL` or `EM` dropped when something is left ("L1-EM" ->
    "L1", "L3EM" -> "L3", "EM" stays "EM")."""
    t = re.sub(r"\s+", "", str(tag or "")).upper()
    base = _BASE_SUFFIX_RE.sub("", t)
    return base if base else t


def fold_token(text: str) -> str | None:
    """The tag token a text-layer word draws, or None: "L1L1" -> "L1",
    "L5/1LH" -> "L5", "L1-EM" -> "L1-EM", "A/1LI-2" -> "A"."""
    t = str(text or "").replace("\r", "").replace("\n", "").strip().upper()
    m = _FOLD_RE.match(t)
    return m.group("t") if m else None


def word_tag(text: str, tags: frozenset[str]) -> str | None:
    """The requested base tag a word draws, or None."""
    token = fold_token(text)
    if token is None:
        return None
    base = normalize_tag(token)
    if base in tags:
        return base
    if token in tags:
        return token
    return None


# Views and scales -----------------------------------------------------------------


def usable_views(views: Iterable[View]) -> list[View]:
    """WS1 views minus legend examples (a `VIEW NAME 1/8" = 1'-0"` sample is
    never a view)."""
    return [v for v in views if not v.example]


def view_at(x: float, y: float, views: list[View], w_in: float, h_in: float) -> View | None:
    """The view a point belongs to. One usable view: that view. Several: the
    views whose scale label sits at or below the point (titles go under their
    drawing), then the column whose title starts at or left of the point (the
    right-most such column), then the nearest title below inside that column.
    A view with only a `region` is chosen when the region holds the point."""
    vs = usable_views(views)
    if not vs:
        return None
    if len(vs) == 1:
        return vs[0]
    with_anchor = [v for v in vs if v.anchor]
    if not with_anchor:
        for v in vs:
            if v.region and v.region[0] <= x <= v.region[2] and v.region[1] <= y <= v.region[3]:
                return v
        return None
    w = w_in or 1.0
    h = h_in or 1.0
    below = [v for v in with_anchor if v.anchor[1] >= y - VIEW_BELOW_SLACK_IN / h]
    pool = below or with_anchor
    left = [v for v in pool if v.anchor[0] <= x + VIEW_LEFT_SLACK_IN / w]
    if left:
        col_x = max(v.anchor[0] for v in left)
        column = [v for v in left if v.anchor[0] >= col_x - VIEW_COLUMN_IN / w]
    else:
        col_x = min(v.anchor[0] for v in pool)
        column = [v for v in pool if v.anchor[0] <= col_x + VIEW_COLUMN_IN / w]

    def gap(v: View) -> tuple[float, str]:
        return (abs(v.anchor[1] - y) * h, v.view_id)

    return min(column, key=gap)


def scale_for(view: View | None, sheet: SheetInputs) -> float | None:
    """Feet per sheet inch at a point: its view's scale, or the page's
    primary scale when WS1 found no views at all. A view without a scale
    (NTS, AS SHOWN) gives None."""
    if view is not None:
        return view.ft_per_in
    if not usable_views(sheet.views):
        return sheet.primary_ft_per_in
    return None


def ft_per_frac(w_in: float, h_in: float, ft_per_in: float | None) -> tuple[float, float] | None:
    """Feet per page fraction along x and y at a scale, or None without one."""
    if not ft_per_in or ft_per_in <= 0:
        return None
    return (w_in * ft_per_in, h_in * ft_per_in)


def distance_in(a: tuple[float, float], b: tuple[float, float], w_in: float, h_in: float) -> float:
    """Paper distance in sheet inches between two displayed-frame points."""
    return math.hypot((a[0] - b[0]) * w_in, (a[1] - b[1]) * h_in)


def distance_ft(
    a: tuple[float, float],
    b: tuple[float, float],
    w_in: float,
    h_in: float,
    ft_per_in: float | None,
) -> float | None:
    """Real-world distance in feet between two displayed-frame points at a
    view scale, or None without a scale."""
    if not ft_per_in or ft_per_in <= 0:
        return None
    return distance_in(a, b, w_in, h_in) * ft_per_in


# Anchors ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Anchor:
    """One text-layer tag occurrence on a plan view. `ref` is the WS2 text
    ref `t:{doc8}:{page}:{word_index}` of the first word folded into it."""

    ref: str
    tag: str
    text: str
    family: str
    x: float
    y: float
    bbox: tuple[float, float, float, float]
    word_index: int
    view_id: str | None
    ft_per_in: float | None
    shx: bool = False
    folded: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "tag": self.tag,
            "text": self.text,
            "family": self.family,
            "x": self.x,
            "y": self.y,
            "bbox": list(self.bbox),
            "word_index": self.word_index,
            "view_id": self.view_id,
            "ft_per_in": self.ft_per_in,
            "shx": self.shx,
            "folded": self.folded,
        }


@dataclass(frozen=True, slots=True)
class ExcludedWord:
    """A word that drew a requested tag but sits outside the plan: the reason
    (`non_plan_region` or `non_plan_view`) and where (region kind or view
    id and kind)."""

    tag: str
    text: str
    word_index: int
    reason: str
    where: str | None
    bbox: tuple[float, float, float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "text": self.text,
            "word_index": self.word_index,
            "reason": self.reason,
            "where": self.where,
            "bbox": list(self.bbox),
        }


@dataclass(slots=True)
class AnchorSet:
    """Anchors of one sheet for a requested tag set. `counts()` answers None
    per tag when the text layer is unusable (unknown, never 0)."""

    document_id: str
    page: int
    w_in: float
    h_in: float
    tags: tuple[str, ...]
    method: str
    text_layer_quality: str
    complete: bool
    anchors: list[Anchor] = field(default_factory=list)
    excluded: list[ExcludedWord] = field(default_factory=list)
    families: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.method != METHOD_UNAVAILABLE

    def by_tag(self, tag: str) -> list[Anchor]:
        key = normalize_tag(tag) if tag != DEMO_X_TAG else tag
        return [a for a in self.anchors if a.tag == key]

    def counts(self) -> dict[str, int | None]:
        keys = list(self.tags) + ([DEMO_X_TAG] if FAMILY_DEMO_X in self.families else [])
        if not self.available:
            return {k: None for k in keys}
        out: dict[str, int | None] = {k: 0 for k in keys}
        for a in self.anchors:
            out[a.tag] = (out.get(a.tag) or 0) + 1
        return out

    def excluded_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.excluded:
            out[e.reason] = out.get(e.reason, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "page": self.page,
            "w_in": self.w_in,
            "h_in": self.h_in,
            "tags": list(self.tags),
            "families": list(self.families),
            "method": self.method,
            "text_layer_quality": self.text_layer_quality,
            "complete": self.complete,
            "counts": self.counts(),
            "excluded_counts": self.excluded_counts(),
            "anchors": [a.to_dict() for a in self.anchors],
            "excluded": [e.to_dict() for e in self.excluded],
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class _Candidate:
    tag: str
    family: str
    text: str
    x: float
    y: float
    bbox: tuple[float, float, float, float]
    word_index: int
    shx: bool


def _box(word: Word) -> tuple[float, float, float, float]:
    b = word.b
    return (round(b[0], 4), round(b[1], 4), round(b[2], 4), round(b[3], 4))


def _word_candidates(
    index: int, word: Word, tags: frozenset[str], demo_x: bool
) -> list[_Candidate]:
    text = word.t
    shx = bool(word.f & F_SHX)
    box = _box(word)
    tag = word_tag(text, tags) if tags else None
    if tag is not None:
        return [
            _Candidate(tag, FAMILY_TAG, text, round(word.cx, 4), round(word.cy, 4), box, index, shx)
        ]
    if demo_x:
        clean = re.sub(r"\s+", "", text).upper()
        if _DEMO_X_RE.match(clean):
            k = max(1, clean.count(DEMO_X_TAG))
            width = box[2] - box[0]
            cy = round(word.cy, 4)
            return [
                _Candidate(
                    DEMO_X_TAG,
                    FAMILY_DEMO_X,
                    text,
                    round(box[0] + width * (i + 0.5) / k, 4),
                    cy,
                    box,
                    index,
                    shx,
                )
                for i in range(k)
            ]
    return []


def _non_plan_region(regions: list[Region], x: float, y: float) -> str | None:
    """The non-plan region kind holding a point, "outside_plan_area" when the
    point is off WS1's plan-area rectangle, or None for plan words. No
    regions (no facts): every point is plan."""
    if not regions:
        return None
    for r in regions:
        if r.kind in NON_PLAN_REGION_KINDS and r.contains(x, y):
            return r.kind
    if not regions_mod.in_plan_area(regions, x, y):
        if not any(r.kind == "plan_area" for r in regions):
            return None
        return "outside_plan_area"
    return None


def unavailable_set(
    document_id: str,
    page: int,
    tags: Iterable[str],
    *,
    families: Iterable[str] = (),
    quality: str = "none",
    note: str | None = None,
    w_in: float = 0.0,
    h_in: float = 0.0,
) -> AnchorSet:
    return AnchorSet(
        document_id=document_id,
        page=page,
        w_in=w_in,
        h_in=h_in,
        tags=tuple(sorted({normalize_tag(t) for t in tags if str(t).strip()})),
        method=METHOD_UNAVAILABLE,
        text_layer_quality=quality,
        complete=False,
        families=tuple(sorted(set(families) & KNOWN_FAMILIES)),
        notes=[note] if note else [],
    )


def extract_anchors(
    sheet: SheetInputs,
    tags: Iterable[str],
    *,
    families: Iterable[str] = (),
    dedupe_in: float = DEDUPE_IN,
) -> AnchorSet:
    """Text-layer anchors of the requested tags on one sheet (rules in the
    module docstring). Anchors are in reading order (top to bottom, then left
    to right); excluded words are reported, not dropped silently."""
    wanted = frozenset(normalize_tag(t) for t in tags if str(t).strip())
    fams = tuple(sorted(set(families)))
    known = tuple(f for f in fams if f in KNOWN_FAMILIES)
    quality = sheet.text_layer_quality
    if quality in UNUSABLE_QUALITIES or not sheet.words:
        return unavailable_set(
            sheet.document_id,
            sheet.page,
            wanted,
            families=known,
            quality=quality,
            note=f"no usable text layer ({sheet.text_class})",
            w_in=sheet.w_in,
            h_in=sheet.h_in,
        )
    out = AnchorSet(
        document_id=sheet.document_id,
        page=sheet.page,
        w_in=sheet.w_in,
        h_in=sheet.h_in,
        tags=tuple(sorted(wanted)),
        method=METHOD_TEXT_LAYER,
        text_layer_quality=quality,
        complete=sheet.text_layer_usable,
        families=known,
    )
    unknown = [f for f in fams if f not in KNOWN_FAMILIES]
    if unknown:
        out.notes.append(f"families not supported yet: {', '.join(unknown)}")
    if not sheet.regions:
        out.notes.append("no page regions: every word treated as plan")

    views = usable_views(sheet.views)
    kept: list[tuple[_Candidate, View | None]] = []
    demo_x = FAMILY_DEMO_X in known
    plan_sheet = not sheet.roles or bool(PLAN_ROLES & set(sheet.roles))
    if not plan_sheet:
        out.notes.append(f"not a plan sheet ({sheet.page_role or ', '.join(sheet.roles)})")
    for i, word in enumerate(sheet.words):
        for cand in _word_candidates(i, word, wanted, demo_x):
            if not plan_sheet:
                out.excluded.append(
                    ExcludedWord(cand.tag, cand.text, i, EXCLUDED_SHEET, sheet.page_role, cand.bbox)
                )
                continue
            region_kind = _non_plan_region(sheet.regions, cand.x, cand.y)
            if region_kind is not None:
                out.excluded.append(
                    ExcludedWord(cand.tag, cand.text, i, EXCLUDED_REGION, region_kind, cand.bbox)
                )
                continue
            view = view_at(cand.x, cand.y, views, sheet.w_in, sheet.h_in)
            if view is not None and view.kind in NON_PLAN_VIEW_KINDS:
                out.excluded.append(
                    ExcludedWord(
                        cand.tag,
                        cand.text,
                        i,
                        EXCLUDED_VIEW,
                        f"{view.view_id} {view.kind}",
                        cand.bbox,
                    )
                )
                continue
            kept.append((cand, view))

    kept.sort(key=lambda cv: (cv[0].y, cv[0].x, cv[0].word_index))
    clusters: list[list[tuple[_Candidate, View | None]]] = []
    by_tag: dict[str, list[int]] = {}
    for cand, view in kept:
        target = None
        for ci in by_tag.get(cand.tag, []):
            head = clusters[ci][0][0]
            if distance_in((cand.x, cand.y), (head.x, head.y), sheet.w_in, sheet.h_in) <= dedupe_in:
                target = ci
                break
        if target is None:
            by_tag.setdefault(cand.tag, []).append(len(clusters))
            clusters.append([(cand, view)])
        else:
            clusters[target].append((cand, view))

    d8 = doc8(sheet.document_id)
    for members in clusters:
        head, view = members[0]
        first = min(c.word_index for c, _ in members)
        out.anchors.append(
            Anchor(
                ref=f"t:{d8}:{sheet.page}:{first}",
                tag=head.tag,
                text=head.text,
                family=head.family,
                x=head.x,
                y=head.y,
                bbox=head.bbox,
                word_index=first,
                view_id=view.view_id if view is not None else None,
                ft_per_in=scale_for(view, sheet),
                shx=any(c.shx for c, _ in members),
                folded=len(members),
            )
        )
    return out


def anchors_for_sheet(
    inputs: EvidenceInputs,
    document_id: str,
    page: int,
    tags: Iterable[str],
    *,
    families: Iterable[str] = (),
) -> AnchorSet:
    """`extract_anchors` over the adapter's sheet; missing page facts give an
    unavailable set (counts None), never an empty count."""
    tag_list = list(tags)
    try:
        sheet = inputs.sheet(document_id, page)
    except FactsUnavailable as exc:
        return unavailable_set(document_id, page, tag_list, families=families, note=str(exc))
    return extract_anchors(sheet, tag_list, families=families)


def anchor_distance_ft(a: Anchor, b: Anchor, w_in: float, h_in: float) -> float | None:
    """Feet between two anchors of one sheet, at their shared view scale
    (None when they sit in different views or the view has no scale)."""
    if a.view_id != b.view_id or a.ft_per_in != b.ft_per_in:
        return None
    return distance_ft((a.x, a.y), (b.x, b.y), w_in, h_in, a.ft_per_in)
