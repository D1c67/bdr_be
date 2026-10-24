"""Page regions from headings (WS1 5.2): legend, abbreviations, notes,
keynotes, schedules, key plan, revision block, the title block (from the
document-level slot) and the plan area (the page minus all of those).

Pure functions over `PageWords`. Regions are page fractions in the displayed
frame. The plan area is one rectangle (left of a right-strip title block, or
above a bottom-strip one) and `in_plan_area` subtracts the other regions, so
a tag inside a legend table never counts as a plan symbol.
"""

from __future__ import annotations

import re
from statistics import median

from app.services.pagefacts.contracts import (
    NON_PLAN_REGION_KINDS,
    Bbox,
    Line,
    PageWords,
    Region,
    round_box,
)

HEADING_RATIO = 1.15
BLOCK_GAP_LINES = 3.0  # a vertical gap of more than this many line heights ends a block
BLOCK_GAP_MIN_IN = 0.5  # but never less than half an inch (title-block tables space their rows)
MAX_BLOCK_LINES = 400

# (kind, regex on the heading text; case-insensitive)
SEEDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "legend",
        re.compile(
            r"(?:ELECTRICAL\s+)?(?:SYMBOL|DEVICE)S?\s+(?:LIST|LEGEND|SCHEDULE|KEY)\b|^LEGEND\b|SHADING\s+KEY"
            r"|CIRCUIT\s+AND\s+CONTROL\s+KEY|SUBSCRIPT\s+KEY|DEVICE\s+KEY|LINE\s*TYPE\s+AND\s+TAG\s+KEY|LUMINAIRE\s+SHADING",
            re.I,
        ),
    ),
    ("abbreviations", re.compile(r"ABBREVIATIONS?(?:\s+KEY)?\b", re.I)),
    ("sheet_notes", re.compile(r"^(?:SHEET|PLAN)\s+NOTES?\b", re.I)),
    ("keynotes", re.compile(r"^KEY(?:ED)?\s*NOTES?\b", re.I)),
    (
        "general_notes",
        re.compile(
            r"^(?:GENERAL|ELECTRICAL|DEMOLITION|POWER|LIGHTING|FIRE\s+ALARM)(?:\s+[A-Z/&-]+){0,2}\s+NOTES?\b",
            re.I,
        ),
    ),
    ("schedule", re.compile(r"\bSCHEDULES?\b(?!\s*\d)", re.I)),
    ("key_plan", re.compile(r"^KEY\s+PLAN\s*:?$", re.I)),
    ("revision_block", re.compile(r"^REVISIONS?\b|ISSUE\s+DATE", re.I)),
)

TITLE_BLOCK_BOILERPLATE = re.compile(
    r"SHEET\s*(?:NO\b|NUMBER\b|TITLE\b)|KEY\s+PLAN|DRAWN\s+BY|CHECKED\s+BY|COPYRIGHT|PROPERTY\s+OF",
    re.I,
)
BOILERPLATE_REACH = 0.10  # boilerplate lines count only this close (page fraction) to the slot
# Mentions that are not a schedule heading ("REFER TO SCHEDULE", "SCHEDULE 40").
NOT_SCHEDULE_HEADING = re.compile(
    r"(?:REFER|SEE|PER|ACCORDING)\s+(?:TO\s+)?(?:THE\s+)?SCHEDULE", re.I
)
# Seeds that live inside the title block by nature.
TITLE_BLOCK_SEEDS: frozenset[str] = frozenset({"revision_block", "key_plan"})
BLOCK_LEFT_REACH_IN = 1.25  # numbered items hang their numbers left of the heading


def median_line_height(lines: list[Line]) -> float:
    hs = [ln.h for ln in lines if ln.h > 0]
    return float(median(hs)) if hs else 0.0


def is_heading(line: Line, median_h: float) -> bool:
    """A heading is taller than 1.15 x the page median line or ends with a
    colon (and holds letters)."""
    if not re.search(r"[A-Z]{2,}", line.t):
        return False
    if median_h > 0 and line.h >= HEADING_RATIO * median_h:
        return True
    return line.t.rstrip().endswith(":")


def seed_kind(text: str) -> str | None:
    t = text.strip()
    for kind, rx in SEEDS:
        if rx.search(t):
            if kind == "schedule" and NOT_SCHEDULE_HEADING.search(t):
                return None
            return kind
    return None


def title_block_region(pw: PageWords, slot_xy: tuple[float, float] | None) -> Region | None:
    """The title block strip from the sheet-number slot (5.2): a right strip
    when the slot sits at x > 0.85 (clipped left to the column of
    title-block boilerplate lines), a bottom strip when it sits at y > 0.9
    further left. None when there is no slot."""
    if slot_xy is None:
        return None
    sx, sy = slot_xy
    if sx > 0.85:
        left = sx - 0.06
        boiler = [
            ln.b[0]
            for ln in pw.lines
            if ln.b[0] >= sx - BOILERPLATE_REACH and TITLE_BLOCK_BOILERPLATE.search(ln.t)
        ]
        if boiler:
            left = min(left, min(boiler) - 0.005)
        return Region(kind="title_block", bbox=round_box((max(0.0, left), 0.0, 1.0, 1.0)))
    if sy > 0.9:
        return Region(kind="title_block", bbox=round_box((0.0, max(0.0, sy - 0.08), 1.0, 1.0)))
    return None


def _inside(box: Bbox, region: Region | None) -> bool:
    if region is None:
        return False
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return region.contains(cx, cy)


def _grow_block(
    pw: PageWords, heading: Line, heading_set: set[int], title_block: Region | None
) -> Bbox:
    """Lines below the heading whose x0 sits within the heading's column,
    until a vertical gap of 3 line heights, another heading or the title
    block."""
    h_frac = heading.h / (pw.page_h_in * 72) if pw.page_h_in else 0.01
    x_lo = heading.b[0] - (BLOCK_LEFT_REACH_IN / pw.page_w_in if pw.page_w_in else 0.03)
    x_hi = heading.b[0] + max(0.25, (heading.b[2] - heading.b[0]) * 3)
    heading_in_tb = _inside(heading.b, title_block)
    boxes = [heading.b]
    last_y = heading.b[3]
    last_h = h_frac
    taken = 0
    for i, ln in enumerate(pw.lines):
        if ln.cy <= heading.cy or ln is heading:
            continue
        if not (x_lo <= ln.b[0] <= x_hi):
            continue
        if _inside(ln.b, title_block) != heading_in_tb:
            continue  # a body block never swallows title-block lines, and the reverse
        gap = ln.b[1] - last_y
        if gap > max(
            BLOCK_GAP_LINES * last_h, BLOCK_GAP_MIN_IN / pw.page_h_in if pw.page_h_in else 0.02
        ):
            break
        if i in heading_set:
            break
        boxes.append(ln.b)
        last_y = max(last_y, ln.b[3])
        last_h = ln.h / (pw.page_h_in * 72) if pw.page_h_in else last_h
        taken += 1
        if taken >= MAX_BLOCK_LINES:
            break
    return round_box(
        (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
    )


def find_regions(pw: PageWords, *, slot_xy: tuple[float, float] | None = None) -> list[Region]:
    """Every region on the page: title block first (when a slot is known),
    then one region per seeded heading, then the plan area last."""
    out: list[Region] = []
    tb = title_block_region(pw, slot_xy)
    if tb is not None:
        out.append(tb)
    med = median_line_height(pw.lines)
    heading_idx: dict[int, str] = {}
    for i, ln in enumerate(pw.lines):
        if not is_heading(ln, med):
            continue
        kind = seed_kind(ln.t)
        if kind is None:
            continue
        if _inside(ln.b, tb) and kind not in TITLE_BLOCK_SEEDS:
            continue
        heading_idx[i] = kind
    heading_set = set(heading_idx)
    for i, kind in heading_idx.items():
        ln = pw.lines[i]
        if kind == "key_plan":
            w_in, h_in = pw.page_w_in or 1.0, pw.page_h_in or 1.0
            box = round_box(
                (ln.b[0], ln.b[1], min(1.0, ln.b[0] + 3 / w_in), min(1.0, ln.b[3] + 3 / h_in))
            )
        else:
            box = _grow_block(pw, ln, heading_set, tb)
        out.append(Region(kind=kind, bbox=box, label=ln.t.strip()))
    out.append(plan_area(out))
    return out


def plan_area(regions: list[Region]) -> Region:
    """The largest rectangle away from the title block: left of a right
    strip, above a bottom strip, the whole page otherwise."""
    box: Bbox = [0.0, 0.0, 1.0, 1.0]
    for r in regions:
        if r.kind != "title_block":
            continue
        if r.bbox[0] > 0.5 and r.bbox[1] <= 0.0 and r.bbox[3] >= 1.0:
            box = [0.0, 0.0, r.bbox[0], 1.0]
        elif r.bbox[1] > 0.5:
            box = [0.0, 0.0, 1.0, r.bbox[1]]
    return Region(kind="plan_area", bbox=round_box(box))


def in_plan_area(regions: list[Region], x: float, y: float) -> bool:
    """Inside the plan-area rectangle and outside every non-plan region."""
    inside = False
    for r in regions:
        if r.kind == "plan_area":
            inside = r.contains(x, y)
            break
    if not inside:
        return False
    for r in regions:
        if r.kind in NON_PLAN_REGION_KINDS and r.contains(x, y):
            return False
    return True


def region_at(regions: list[Region], x: float, y: float) -> str | None:
    """The most specific region kind at a point (non-plan regions first)."""
    for r in regions:
        if r.kind in NON_PLAN_REGION_KINDS and r.contains(x, y):
            return r.kind
    return "plan_area" if in_plan_area(regions, x, y) else None


def lines_in(pw: PageWords, region: Region) -> list[Line]:
    return [ln for ln in pw.lines if region.contains(ln.cx, ln.cy)]
