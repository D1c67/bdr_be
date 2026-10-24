"""Scales and views (WS1 5.5): architectural and engineering scale strings
found on the raw char stream (they span words: `1/4" = 1'-0"`), the
view-number bubble rule, the legend EXAMPLE rule, view association and the
per-page `primary_ft_per_in` with `scale_conflict`.

Rules that cost time before (prototype gotchas 4 and 5):
- The view-number bubble precedes the scale in the char stream: "1 1/4" =
  1'-0"" is 1/4" (4 ft per inch) unless the "1" has the same glyph height,
  baseline and a small gap, in which case it is the whole-inch part.
- A scale inside a legend, abbreviations or general-notes region, or within
  1 in of the text "VIEW NAME", is a legend example and never the page scale
  (7154 E0.00 carries `VIEW NAME 1/8" = 1'-0"`).
- `views[].region` is never invented: frame rectangles are not detected in
  this cut, so the single plan view gets the plan area as its region only
  when every other scaled view on the sheet shares its scale; otherwise the
  regions stay None and `scale_conflict` tells consumers not to measure
  across views.
"""

from __future__ import annotations

import math
import re

from app.services.pagefacts.contracts import Bbox, Line, PageWords, Region, View
from app.services.pagefacts.regions import median_line_height

STREAM_SCALE = re.compile(
    r"(?:(?P<whole>\d{1,2})[ \-])?(?P<num>\d{1,2})/(?P<den>\d{1,3})\"\s*=\s*1'\s*-?\s*0\"?"
    r"|(?P<inch>\d{1,2})\"\s*=\s*1'\s*-?\s*0\"?"
    r"|1\"\s*=\s*(?P<eng>\d{1,4})\s*(?:'|FT\b|FEET\b)"
    r"|(?P<nts>\bN\.?T\.?S\b\.?|NO\s+SCALE|NOT\s+TO\s+SCALE|AS\s+NOTED|AS\s+SHOWN)",
    re.I,
)
VIEW_TITLE_RE = re.compile(r"PLAN|DETAIL|SECTION|ELEVATION|DIAGRAM|ENLARGED|RISER|SCHEDULE", re.I)
EXAMPLE_TEXT_RE = re.compile(r"VIEW\s+NAME", re.I)
EXAMPLE_REGION_KINDS: frozenset[str] = frozenset({"legend", "abbreviations", "general_notes"})
VIEW_REACH_IN = 1.0
EXAMPLE_REACH_IN = 1.0
# A whole-inch digit joins the fraction when its glyph height is within 15%,
# its baseline within 0.2 H and the gap under 1 H (tight charboxes make a
# single space about 0.8 H wide); a view bubble is taller and further away.
BUBBLE_GAP_H = 1.0
HEADING_RATIO = 1.15


def view_kind(title: str | None) -> str:
    if not title:
        return "other"
    t = title.upper()
    if "ENLARGED" in t and "PLAN" in t:
        return "enlarged_plan"
    if "PLAN" in t:
        return "plan"
    if "DETAIL" in t:
        return "detail"
    if "SECTION" in t:
        return "section"
    if "ELEVATION" in t:
        return "elevation"
    if re.search(r"DIAGRAM|RISER|ONE[\s-]?LINE|SINGLE[\s-]?LINE", t):
        return "diagram"
    if "SCHEDULE" in t:
        return "schedule"
    return "other"


def parse_scale_text(text: str) -> tuple[float | None, bool]:
    """(ft_per_in, nts) for a scale string on its own (no bubble context):
    `1/4" = 1'-0"` -> 4.0, `1 1/2" = 1'-0"` -> 0.6667, `1" = 30'` -> 30.0,
    `N.T.S.` -> (None, True)."""
    m = STREAM_SCALE.search(text)
    if not m:
        return None, False
    if m.group("nts"):
        return None, True
    if m.group("eng"):
        return float(m.group("eng")), False
    if m.group("inch"):
        return round(1 / float(m.group("inch")), 4), False
    frac = int(m.group("num")) / int(m.group("den"))
    if m.group("whole"):
        frac += int(m.group("whole"))
    return round(1 / frac, 4), False


def _center_in(box: Bbox, pw: PageWords) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2 * pw.page_w_in, (box[1] + box[3]) / 2 * pw.page_h_in)


def _distance_in(a: Bbox, b: Bbox, pw: PageWords) -> float:
    """Gap between two boxes in sheet inches (0 when they overlap)."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2])) * pw.page_w_in
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3])) * pw.page_h_in
    return math.hypot(dx, dy)


def _is_example(pw: PageWords, box: Bbox, regions: list[Region]) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for r in regions:
        if r.kind in EXAMPLE_REGION_KINDS and r.contains(cx, cy):
            return True
    for ln in pw.lines:
        if EXAMPLE_TEXT_RE.search(ln.t) and _distance_in(ln.b, box, pw) <= EXAMPLE_REACH_IN:
            return True
    return False


def _view_title_near(pw: PageWords, box: Bbox, med: float) -> Line | None:
    """The nearest tall view-title line within 1 in above the scale or on its
    baseline (either side)."""
    best: tuple[float, Line] | None = None
    scale_cy = (box[1] + box[3]) / 2
    for ln in pw.lines:
        if med > 0 and ln.h < HEADING_RATIO * med:
            continue
        if not VIEW_TITLE_RE.search(ln.t):
            continue
        if ln.cy > box[3] + 0.5 * (box[3] - box[1]) and abs(ln.cy - scale_cy) > (box[3] - box[1]):
            continue  # clearly below the scale
        d = _distance_in(ln.b, box, pw)
        if d <= VIEW_REACH_IN and (best is None or d < best[0]):
            best = (d, ln)
    return best[1] if best else None


def _bubble_number(pw: PageWords, box: Bbox, title: Line | None) -> str | None:
    """A 1 to 2 digit token of view-title height just left of the scale or
    the title: the view number."""
    anchors = [box] + ([title.b] if title else [])
    best: tuple[float, str] | None = None
    for ln in pw.lines:
        t = ln.t.strip()
        if not re.fullmatch(r"\d{1,2}|[A-Z]\d?", t):
            continue
        if title is not None and ln.h < 0.6 * title.h:
            continue
        for a in anchors:
            if ln.b[2] > a[0] + 0.002:
                continue  # not left of the anchor
            if ln.b[3] < a[1] - 0.01 or ln.b[1] > a[3] + 0.01:
                continue  # not on the anchor's band
            d = _distance_in(ln.b, a, pw)
            if d <= 0.6 and (best is None or d < best[0]):
                best = (d, t)
    return best[1] if best else None


def find_views(
    pw: PageWords, regions: list[Region]
) -> tuple[list[View], float | None, bool, list[str]]:
    """Every scale on the page as a View (titled when a view title sits next
    to it), plus (primary_ft_per_in, scale_conflict, notes). Needs
    `pw.stream` (runtime extraction); returns nothing for a blob-only page."""
    stream = pw.stream
    views: list[View] = []
    notes: list[str] = []
    if stream is None or not stream.text:
        return views, None, False, notes
    med = median_line_height(pw.lines)
    seen: set[tuple[float, float]] = set()
    for m in STREAM_SCALE.finditer(stream.text):
        box = stream.bbox(m.start(), m.end())
        if box is None:
            continue
        nts = False
        ft: float | None = None
        text = re.sub(r"\s+", " ", m.group(0)).strip()
        if m.group("nts"):
            nts = True
        elif m.group("eng"):
            ft = float(m.group("eng"))
        elif m.group("inch"):
            ft = round(1 / float(m.group("inch")), 4)
        else:
            frac = int(m.group("num")) / int(m.group("den"))
            whole = m.group("whole")
            if whole:
                wb = stream.box_pt(m.start("whole"), m.end("whole"))
                fb = stream.box_pt(m.start("num"), m.end("num"))  # digits only: "/" has a descender
                same = False
                if wb and fb:
                    fh = fb[3] - fb[1]
                    wh = wb[3] - wb[1]
                    same = (
                        abs(wh - fh) <= 0.15 * max(fh, 0.1)
                        and abs(wb[1] - fb[1]) <= 0.2 * max(fh, 0.1)
                        and (fb[0] - wb[2]) < BUBBLE_GAP_H * max(fh, 0.1)
                    )
                if same:
                    frac += int(whole)
                else:
                    text = re.sub(r"\s+", " ", m.group(0)[m.start("num") - m.start() :]).strip()
                    box = stream.bbox(m.start("num"), m.end()) or box
            ft = round(1 / frac, 4) if frac > 0 else None
        key = (round(box[0], 3), round(box[1], 3))
        if key in seen:
            continue
        seen.add(key)
        example = _is_example(pw, box, regions)
        title_line = _view_title_near(pw, box, med)
        title = title_line.t.strip() if title_line else None
        number = _bubble_number(pw, box, title_line) if not example else None
        views.append(
            View(
                view_id=f"v{len(views) + 1}",
                title=title,
                number=number,
                scale_text=text,
                ft_per_in=ft,
                kind=view_kind(title),
                anchor=box,
                region=None,
                example=example,
                nts=nts,
            )
        )
        if example:
            notes.append(f"{text} near {title or 'VIEW NAME'} is a legend example and was ignored")

    usable = [v for v in views if not v.example and v.ft_per_in is not None]
    plan_views = [v for v in usable if v.kind in ("plan", "enlarged_plan")]
    pool = plan_views or [v for v in usable if v.kind == "other"]
    primary: float | None = None
    conflict = False
    if pool:
        distinct = sorted({v.ft_per_in for v in pool if v.ft_per_in is not None})
        if len(distinct) == 1:
            primary = distinct[0]
        else:
            conflict = True

            def title_h(v: View) -> float:
                if not v.title:
                    return 0.0
                for ln in pw.lines:
                    if ln.t.strip() == v.title:
                        return ln.h
                return 0.0

            top = max(pool, key=title_h)
            primary = top.ft_per_in
            notes.append(
                "scale_conflict: "
                + ", ".join(f"{v.scale_text} ({v.title or 'untitled'})" for v in pool)
            )
    # View regions: no frame-rectangle detection exists in this cut, so the
    # plan view owns the whole plan area only when nothing on the page is
    # drawn at another scale. A plan view beside details, sections or
    # elevations at a different scale leaves every region None and reports
    # scale_conflict: consumers must not measure across views (spec 6 WS1).
    if len(plan_views) == 1 and not conflict:
        plan = plan_views[0]
        other_scales = sorted(
            {v.ft_per_in for v in usable if v is not plan and v.ft_per_in != plan.ft_per_in}
        )
        if not other_scales:
            for r in regions:
                if r.kind == "plan_area":
                    plan.region = list(r.bbox)
        else:
            conflict = True
            others = ", ".join(
                f"{v.scale_text} ({v.title or v.kind})"
                for v in usable
                if v is not plan and v.ft_per_in != plan.ft_per_in
            )
            notes.append(
                f"scale_conflict: {plan.scale_text} ({plan.title or 'plan'}) shares the sheet "
                f"with views at other scales: {others}; view regions are unknown, "
                "do not measure across views"
            )
    return views, primary, conflict, notes
