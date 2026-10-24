"""Code-computed escalation and recount regions (docs/AI_ESTIMATING_BUILD_SPEC.md
WS4; fixes E19 and the E11 containment half).

The weekend run let the model pick its own escalation tiles: on ED3.01 the
two tiles it chose held 22 of the 39 printed "(X)" tags and the tile's
subtotal became the sheet total (E11); on E0.04 the tile covered the L1 row
while the question was about the L4 control cell, which the text layer
prints as LOCAL (E19). This module computes regions from the WS1 facts
instead:

- a flag about a schedule row (a luminaire tag, a panel name, a circuit) is
  anchored to that row's text: the row band across the whole schedule, so
  the crop holds every cell of the row (`text_anchor`);
- a flag about counting a family of tagged devices becomes the padded
  bounding boxes of the family's tag clusters (`tag_cluster`), which by
  construction contain every printed tag of the family (39 of 39);
- anything else keeps the model's region (`model`), so no flag is lost.

Overall and enlarged views are deduped the way the floors module does it
(an enlarged view repeats the overall plan's devices), and clusters are
formed in sheet inches so a 42 x 30 sheet and a 24 x 18 sheet behave alike.
Region strings are the RESOLUTION_FLAG form "x0,y0,x1,y1" that
`plans_reader_images.region_to_box` reads, so `_escalation_tiles` consumes
them unchanged. `plans_reader_region_pad` and `plans_reader_cluster_gap`
(5.7, page fractions of the page width) set the padding and the single-link
gap; both are children of `plans_reader_code_regions`.

Pure: facts in, regions out. The optional WS1 `PageWords` lets the text
anchor search see words the facts summary does not keep.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.plans_reader_floors import (
    FAMILIES,
    Floor,
    dedupe_views,
    family_points,
    split_family_key,
)

DEFAULT_PAD = 0.03  # page fraction of the width (plans_reader_region_pad)
DEFAULT_CLUSTER_GAP = 0.06  # page fraction of the width (plans_reader_cluster_gap)
MAX_REGIONS = 4
REGION_KINDS: tuple[str, ...] = ("text_anchor", "tag_cluster", "model", "view")
# Smallest region side (page fraction) so a one-tag cluster still renders a
# readable crop.
MIN_SIDE = 0.04

_TAG_TOKEN_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?)\b")
_PANEL_TOKEN_RE = re.compile(r"\b(\d?[A-Z]{1,4}\d{0,2}[A-Z]?)\s*-\s*(\d{1,3})\b")
_FAMILY_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("demo_x", re.compile(r"(?i)\(\s*X\s*\)|\bdemolition\b|\bdemo\b|\bremov\w*\b|\bexisting to be removed\b")),
    ("relocate_r", re.compile(r"(?i)\(\s*(?:R|RL|ER)\s*\)|\brelocat\w*\b")),
    ("circuit", re.compile(r"(?i)\bcircuit\s+tags?\b|\bhome\s*runs?\b|\bcircuit numbers?\b")),
)
_COUNT_WORDS = re.compile(r"(?i)\bcount\w*\b|\bsymbols?\b|\bgraphics?\b|\bfixtures?\b|\bdevices?\b|\bheads?\b|\btrack\b")
_SCHEDULE_WORDS = re.compile(r"(?i)\bschedule\b|\bcell\b|\bcolumn\b|\brow\b|\bcontrol method\b|\bcatalog\b")
_WS_RE = re.compile(r"\s+")


@dataclass
class CodeRegion:
    """One computed region: `box` [x0, y0, x1, y1] page fractions, `kind`
    (REGION_KINDS), the anchors that produced it, the tag positions it
    contains, the view it sits in, and the flag it answers."""

    box: list[float]
    kind: str
    anchors: list[str] = field(default_factory=list)
    family: str | None = None
    contains: int = 0
    targets: int = 0
    view_id: str | None = None
    reason: str | None = None
    note: str | None = None

    @property
    def region(self) -> str:
        """The RESOLUTION_FLAG region string."""
        return ",".join(f"{v:.4f}" for v in self.box)

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "box": [round(v, 4) for v in self.box],
            "kind": self.kind,
            "anchors": list(self.anchors),
            "family": self.family,
            "contains": self.contains,
            "targets": self.targets,
            "view_id": self.view_id,
            "reason": self.reason,
            "note": self.note,
        }


# Settings -------------------------------------------------------------------------


def region_settings(settings: Any = None) -> tuple[float, float]:
    """(pad, cluster_gap) page fractions from `settings` (object or mapping;
    None reads the app settings when they load, else the code defaults)."""
    pad, gap = DEFAULT_PAD, DEFAULT_CLUSTER_GAP
    src = settings
    if src is None:
        try:
            from app.core.config import get_settings

            src = get_settings()
        except Exception:  # noqa: BLE001 - settings unavailable reads as the defaults
            src = None
    if isinstance(src, Mapping):
        pad = float(src.get("plans_reader_region_pad", pad))
        gap = float(src.get("plans_reader_cluster_gap", gap))
    elif src is not None:
        pad = float(getattr(src, "plans_reader_region_pad", pad))
        gap = float(getattr(src, "plans_reader_cluster_gap", gap))
    return pad, gap


# Geometry ---------------------------------------------------------------------------


def _page_dict(page: Any) -> dict[str, Any]:
    if page is None:
        return {}
    if isinstance(page, Mapping):
        return dict(page)
    to_dict = getattr(page, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    raise TypeError("page must be a PageFacts or its dict form")


def _size(page: Mapping[str, Any], w_in: float | None, h_in: float | None) -> tuple[float, float]:
    w = float(w_in or page.get("w_in") or 42.0)
    h = float(h_in or page.get("h_in") or 30.0)
    return (w if w > 0 else 42.0), (h if h > 0 else 30.0)


def clamp_box(box: Iterable[float]) -> list[float]:
    x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in box)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def pad_box(box: Iterable[float], pad: float, w_in: float, h_in: float, *, min_side: float = MIN_SIDE, pad_y: float | None = None) -> list[float]:
    """Grow a box by `pad` (a fraction of the page WIDTH, applied as the same
    sheet distance vertically unless `pad_y` is given) and to at least
    `min_side` on each side."""
    x0, y0, x1, y1 = (float(v) for v in box)
    py = pad * w_in / h_in if pad_y is None else pad_y
    x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - py, y1 + py
    if x1 - x0 < min_side:
        c = (x0 + x1) / 2
        x0, x1 = c - min_side / 2, c + min_side / 2
    if y1 - y0 < min_side:
        c = (y0 + y1) / 2
        y0, y1 = c - min_side / 2, c + min_side / 2
    return clamp_box((x0, y0, x1, y1))


def point_in(p: Iterable[float], box: Iterable[float]) -> bool:
    x, y = (float(v) for v in list(p)[:2])
    x0, y0, x1, y1 = (float(v) for v in box)
    return x0 <= x <= x1 and y0 <= y <= y1


def points_inside(points: Iterable[Iterable[float]], boxes: Iterable[Iterable[float]]) -> int:
    bs = [list(b) for b in boxes]
    return sum(1 for p in points if any(point_in(p, b) for b in bs))


def cluster_points(points: list[list[float]], w_in: float, h_in: float, *, gap: float) -> list[list[int]]:
    """Single-link clusters of point indices; `gap` is a fraction of the
    page width, compared in sheet inches (largest cluster first)."""
    n = len(points)
    parent = list(range(n))
    eps = gap * w_in

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        xi, yi = points[i][0] * w_in, points[i][1] * h_in
        for j in range(i + 1, n):
            xj, yj = points[j][0] * w_in, points[j][1] * h_in
            if ((xi - xj) ** 2 + (yi - yj) ** 2) ** 0.5 <= eps:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0]))


def cluster_boxes(
    points: list[list[float]],
    w_in: float,
    h_in: float,
    *,
    pad: float = DEFAULT_PAD,
    gap: float = DEFAULT_CLUSTER_GAP,
    max_regions: int = MAX_REGIONS,
) -> list[list[float]]:
    """Padded bounding boxes of the point clusters, largest first. When the
    clusters exceed `max_regions`, the smallest ones are merged into one box
    so every point stays inside some region (containment is the invariant;
    a wider crop costs tokens, a missed tag costs devices)."""
    if not points:
        return []
    groups = cluster_points(points, w_in, h_in, gap=gap)
    if len(groups) > max_regions:
        head = groups[: max_regions - 1]
        tail = [i for g in groups[max_regions - 1 :] for i in g]
        groups = head + [tail]
    out = []
    for g in groups:
        xs = [points[i][0] for i in g]
        ys = [points[i][1] for i in g]
        out.append(pad_box((min(xs), min(ys), max(xs), max(ys)), pad, w_in, h_in))
    return out


def merge_overlapping(boxes: list[list[float]]) -> list[list[float]]:
    """Union of boxes that overlap (keeps the regions distinct otherwise)."""
    out: list[list[float]] = []
    for b in boxes:
        box = list(b)
        merged = True
        while merged:
            merged = False
            for i, o in enumerate(out):
                if box[0] <= o[2] and o[0] <= box[2] and box[1] <= o[3] and o[1] <= box[3]:
                    box = [min(box[0], o[0]), min(box[1], o[1]), max(box[2], o[2]), max(box[3], o[3])]
                    out.pop(i)
                    merged = True
                    break
        out.append(clamp_box(box))
    return out


def view_of(page: Mapping[str, Any], box: Iterable[float]) -> str | None:
    """The view whose region holds the box center."""
    x0, y0, x1, y1 = (float(v) for v in box)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    best: tuple[float, str] | None = None
    for v in page.get("views") or []:
        if not isinstance(v, Mapping) or not v.get("region"):
            continue
        r = [float(x) for x in v["region"]]
        if point_in((cx, cy), r):
            area = (r[2] - r[0]) * (r[3] - r[1])
            if best is None or area < best[0]:
                best = (area, str(v.get("view_id")))
    return best[1] if best else None


# Text anchors -----------------------------------------------------------------------


def _schedule_regions(page: Mapping[str, Any]) -> list[list[float]]:
    out = []
    for r in page.get("regions") or []:
        if isinstance(r, Mapping) and r.get("kind") in ("schedule", "legend", "keynotes", "sheet_notes", "general_notes") and r.get("bbox"):
            out.append([float(v) for v in r["bbox"]])
    return out


def _enclosing(box: list[float], containers: list[list[float]]) -> list[float] | None:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    hits = [c for c in containers if point_in((cx, cy), c)]
    if not hits:
        return None
    return min(hits, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]))


def _facts_row_boxes(page: Mapping[str, Any], schedules: Iterable[Mapping[str, Any]], token: str) -> list[tuple[list[float], str]]:
    """Row bboxes of schedule rows keyed by `token` on this page from the
    WS1 document schedules (luminaire tag, panel circuit, equipment tag)."""
    page_no = page.get("page")
    out: list[tuple[list[float], str]] = []
    tok = token.upper()
    for s in schedules:
        if not isinstance(s, Mapping) or (page_no is not None and s.get("page") not in (None, page_no)):
            continue
        for row in s.get("luminaires") or []:
            if isinstance(row, Mapping) and str(row.get("tag") or "").upper() == tok and row.get("bbox"):
                out.append(([float(v) for v in row["bbox"]], f"luminaire {tok}"))
        for row in s.get("equipment") or []:
            if isinstance(row, Mapping) and str(row.get("tag") or "").upper() == tok and row.get("bbox"):
                out.append(([float(v) for v in row["bbox"]], f"equipment {tok}"))
        panel = s.get("panel") if isinstance(s.get("panel"), Mapping) else None
        if panel and str(panel.get("name") or "").upper() == tok and s.get("bbox"):
            out.append(([float(v) for v in s["bbox"]], f"panel {tok}"))
    return out


def _word_row_boxes(words: Any, token: str, containers: list[list[float]]) -> list[tuple[list[float], str]]:
    """Rows of the WS1 words whose text holds the token, widened to the
    enclosing schedule (or notes) region so every cell is inside."""
    if words is None:
        return []
    lines = getattr(words, "lines", None)
    wlist = getattr(words, "words", None)
    if lines is None and isinstance(words, Mapping):
        lines, wlist = words.get("lines"), words.get("words")
    out: list[tuple[list[float], str]] = []
    tok = token.upper()
    if lines:
        for ln in lines:
            text = str(getattr(ln, "t", None) if not isinstance(ln, Mapping) else ln.get("t") or "")
            if not re.search(rf"(?<![A-Z0-9]){re.escape(tok)}(?![A-Z0-9])", text.upper()):
                continue
            b = getattr(ln, "b", None) if not isinstance(ln, Mapping) else ln.get("b")
            if not b:
                continue
            box = _row_span([float(v) for v in b], containers)
            out.append((box, f"text {tok}"))
    elif wlist:
        for w in wlist:
            text = str(getattr(w, "t", None) if not isinstance(w, Mapping) else w.get("t") or "")
            if text.upper() != tok:
                continue
            b = getattr(w, "b", None) if not isinstance(w, Mapping) else w.get("b")
            if not b:
                continue
            box = _row_span([float(v) for v in b], containers)
            out.append((box, f"text {tok}"))
    return out


def _row_span(box: list[float], containers: list[list[float]]) -> list[float]:
    """The row band of `box` widened to every schedule region it touches, so
    a crop of the row holds every cell (the tag at the left edge included)."""
    enc = _enclosing(box, containers)
    if enc is None:
        return box
    x0, x1 = min(enc[0], box[0]), max(enc[2], box[2])
    for c in containers:
        if c[0] <= x1 and x0 <= c[2] and c[1] <= box[3] and box[1] <= c[3]:
            x0, x1 = min(x0, c[0]), max(x1, c[2])
    return [x0, box[1], x1, box[3]]


def anchor_tokens(reason: str, page: Mapping[str, Any], schedules: Iterable[Mapping[str, Any]] = ()) -> list[str]:
    """Row keys named in a flag reason that exist in the page's schedules or
    tags: luminaire tags (L4), panel names, circuit tags (1LH-4)."""
    text = str(reason or "").upper()
    known: set[str] = set()
    for s in schedules:
        if not isinstance(s, Mapping):
            continue
        for row in (s.get("luminaires") or []) + (s.get("equipment") or []):
            if isinstance(row, Mapping) and row.get("tag"):
                known.add(str(row["tag"]).upper())
        panel = s.get("panel") if isinstance(s.get("panel"), Mapping) else None
        if panel and panel.get("name"):
            known.add(str(panel["name"]).upper())
    tags = page.get("tags") or {}
    for t in tags.get("type_marks") or {}:
        known.add(str(t).upper())
    for lab in tags.get("panel_labels") or []:
        if isinstance(lab, Mapping) and lab.get("panel"):
            known.add(str(lab["panel"]).upper())
    found: list[str] = []
    for tok in sorted(known, key=lambda t: (text.find(t) if t in text else 10**6, t)):
        if re.search(rf"(?<![A-Z0-9]){re.escape(tok)}(?![A-Z0-9])", text) and tok not in found:
            found.append(tok)
    for m in _PANEL_TOKEN_RE.finditer(text):
        tok = f"{m.group(1)}-{m.group(2)}"
        if tok not in found and (m.group(1) in known or not known):
            found.append(tok)
    return found


def family_for_reason(reason: str) -> str | None:
    """The census family a counting flag is about, or None."""
    text = str(reason or "")
    for fam, pat in _FAMILY_WORDS:
        if pat.search(text):
            return fam
    return None


# Public API -----------------------------------------------------------------------------


def _parse_model_region(region: Any) -> list[float] | None:
    text = str(region or "").strip().lower().replace(" ", "")
    if text in ("whole", "all", "page"):
        return [0.0, 0.0, 1.0, 1.0]
    m = re.match(r"^r([1-3])c([1-3])$", text)
    if m:
        r, c = int(m.group(1)) - 1, int(m.group(2)) - 1
        return clamp_box((c / 3, r / 3, (c + 1) / 3, (r + 1) / 3))
    parts = text.strip("[]()").split(",")
    if len(parts) == 4:
        try:
            return clamp_box(float(p) for p in parts)
        except ValueError:
            return None
    return None


def text_anchor_regions(
    reason: str,
    *,
    page: Any,
    words: Any = None,
    schedules: Iterable[Mapping[str, Any]] = (),
    pad: float = DEFAULT_PAD,
    w_in: float | None = None,
    h_in: float | None = None,
) -> list[CodeRegion]:
    """Regions anchored to schedule rows named in `reason` (E19)."""
    pg = _page_dict(page)
    w, h = _size(pg, w_in, h_in)
    containers = _schedule_regions(pg)
    out: list[CodeRegion] = []
    for tok in anchor_tokens(reason, pg, schedules):
        rows = _facts_row_boxes(pg, schedules, tok) or _word_row_boxes(words, tok, containers)
        for box, label in rows:
            full = _row_span(box, containers)
            # Two row heights above and below: the neighbors give the crop
            # context, the anchored row stays the center.
            row_h = max(box[3] - box[1], 0.002)
            out.append(
                CodeRegion(
                    box=pad_box(full, pad, w, h, min_side=0.0, pad_y=2 * row_h + 0.002), kind="text_anchor",
                    anchors=[label], view_id=view_of(pg, full), reason=reason,
                )
            )
    return out


def tag_cluster_regions(
    family: str,
    *,
    page: Any,
    tag: str | None = None,
    pad: float = DEFAULT_PAD,
    gap: float = DEFAULT_CLUSTER_GAP,
    max_regions: int = MAX_REGIONS,
    w_in: float | None = None,
    h_in: float | None = None,
    positions: list[list[float]] | None = None,
) -> list[CodeRegion]:
    """Regions around the family's tag clusters; together they contain every
    occurrence (`contains == targets`)."""
    pg = _page_dict(page)
    w, h = _size(pg, w_in, h_in)
    pts = list(positions) if positions is not None else family_points(pg, family, tag)
    if positions is not None:
        pts = dedupe_views(pg, pts)
    boxes = merge_overlapping(cluster_boxes(pts, w, h, pad=pad, gap=gap, max_regions=max_regions))
    out = []
    for box in boxes:
        inside = points_inside(pts, [box])
        out.append(
            CodeRegion(
                box=box, kind="tag_cluster", anchors=[f"{family}{':' + tag if tag else ''}"],
                family=family, contains=inside, targets=len(pts), view_id=view_of(pg, box),
            )
        )
    return out


def regions_for_flags(
    flags: Iterable[Mapping[str, Any]],
    *,
    page: Any,
    words: Any = None,
    schedules: Iterable[Mapping[str, Any]] = (),
    w_in: float | None = None,
    h_in: float | None = None,
    settings: Any = None,
    max_regions: int = MAX_REGIONS,
) -> list[CodeRegion]:
    """Code regions for a page's resolution flags (`{region, reason}` as
    the sub schema writes them). Text-anchored for schedule-row questions,
    tag clusters for counting questions, the model's region otherwise. The
    result's `.region` strings feed `_escalation_tiles` unchanged."""
    pg = _page_dict(page)
    w, h = _size(pg, w_in, h_in)
    pad, gap = region_settings(settings)
    out: list[CodeRegion] = []
    for flag in flags:
        if not isinstance(flag, Mapping):
            continue
        reason = str(flag.get("reason") or "")
        regions: list[CodeRegion] = []
        if _SCHEDULE_WORDS.search(reason) or anchor_tokens(reason, pg, schedules):
            regions = text_anchor_regions(reason, page=pg, words=words, schedules=schedules, pad=pad, w_in=w, h_in=h)
        fam = family_for_reason(reason) if not regions else None
        if fam and (_COUNT_WORDS.search(reason) or fam in ("demo_x", "relocate_r")):
            regions = tag_cluster_regions(fam, page=pg, pad=pad, gap=gap, max_regions=max_regions, w_in=w, h_in=h)
            for r in regions:
                r.reason = reason
        if not regions:
            box = _parse_model_region(flag.get("region"))
            if box is None:
                continue
            model_pts = family_points(pg, fam) if fam else []
            regions = [
                CodeRegion(
                    box=box, kind="model", anchors=[], family=fam,
                    contains=points_inside(model_pts, [box]) if model_pts else 0,
                    targets=len(model_pts), view_id=view_of(pg, box), reason=reason,
                    note="no text anchor or tag cluster found; the model's region kept",
                )
            ]
        out.extend(regions)
    # The same box answering two flags is one region.
    seen: set[tuple[float, ...]] = set()
    unique: list[CodeRegion] = []
    for r in out:
        key = tuple(r.box)
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def for_recount(
    sheet_key: str,
    family: str,
    floors: Iterable[Floor | Mapping[str, Any]],
    words: Any = None,
    *,
    page: Any = None,
    settings: Any = None,
    max_regions: int = MAX_REGIONS,
    w_in: float | None = None,
    h_in: float | None = None,
) -> list[CodeRegion]:
    """Recount regions for one (sheet, family): the tag clusters of the
    floor's positions (every printed tag inside), as the WS7 recount module
    takes them. `family` may be a family key (`fixture_mark:L5`). Without a
    known floor the model gets no region (an empty list), never a guess."""
    fam, tag = split_family_key(family) if ":" in family or family in FAMILIES else (family, None)
    pg = _page_dict(page)
    pad, gap = region_settings(settings)
    chosen: Floor | None = None
    for f in floors:
        fl = f if isinstance(f, Floor) else Floor.from_dict(f)
        if fl.sheet_key == sheet_key and fl.family == fam and (fl.tag or None) == tag and fl.known:
            chosen = fl
            break
    if chosen is None:
        return []
    w, h = _size(pg, w_in, h_in)
    positions = chosen.positions or family_points(pg, fam, tag)
    if not positions and words is not None:
        positions = _positions_from_words(words, fam)
    if not positions:
        return []
    regions = tag_cluster_regions(
        fam, page=pg, tag=tag, pad=pad, gap=gap, max_regions=max_regions, w_in=w, h_in=h, positions=positions,
    )
    for r in regions:
        r.reason = f"recount {chosen.key} on {chosen.sheet_number or sheet_key}: floor {chosen.floor}"
    return regions


def _positions_from_words(words: Any, family: str) -> list[list[float]]:
    """Family tag positions straight from WS1 words (status tags only need
    no circuit grammar), for callers that have words but no facts tags."""
    from app.services.pagefacts.tags import STATUS_TAG_RE

    wanted = {"demo_x": {"X", "D"}, "relocate_r": {"R", "RL", "ER"}}.get(family)
    if not wanted:
        return []
    out: list[list[float]] = []
    wlist = getattr(words, "words", None)
    if wlist is None and isinstance(words, Mapping):
        wlist = words.get("words")
    for w in wlist or []:
        text = str(getattr(w, "t", None) if not isinstance(w, Mapping) else w.get("t") or "")
        m = STATUS_TAG_RE.match(text)
        if not m or m.group(1) not in wanted:
            continue
        b = getattr(w, "b", None) if not isinstance(w, Mapping) else w.get("b")
        if b:
            out.append([round((float(b[0]) + float(b[2])) / 2, 4), round((float(b[1]) + float(b[3])) / 2, 4)])
    return out


def containment(page: Any, family: str, tiles: Iterable[Iterable[float]], *, tag: str | None = None) -> dict[str, Any]:
    """`{targets, inside, complete, family}` of a family's printed tags
    against escalation tiles (the plans_reader_rows containment shape)."""
    pg = _page_dict(page)
    pts = family_points(pg, family, tag)
    boxes = [list(t) for t in tiles or []]
    inside = points_inside(pts, boxes) if boxes else 0
    return {"family": family, "targets": len(pts), "inside": inside, "complete": bool(pts) and inside >= len(pts)}


def region_strings(regions: Iterable[CodeRegion]) -> list[str]:
    return [r.region for r in regions]


__all__ = [
    "DEFAULT_PAD", "DEFAULT_CLUSTER_GAP", "MAX_REGIONS", "REGION_KINDS", "MIN_SIDE", "CodeRegion",
    "region_settings", "clamp_box", "pad_box", "point_in", "points_inside", "cluster_points", "cluster_boxes",
    "merge_overlapping", "view_of", "anchor_tokens", "family_for_reason", "text_anchor_regions",
    "tag_cluster_regions", "regions_for_flags", "for_recount", "containment", "region_strings",
]
