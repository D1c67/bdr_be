"""Note blocks (WS1 step 4): keynotes, sheet notes and general notes as
numbered items with exact text, keynote markers on plan pages and the
cross-trade mentions that feed the scope workstream.

Items are joined by column: the item number sits in the block's left gutter
(its own line, or the head of an inline "1. TEXT" line) and the text lives in
the column to its right; a continuation line belongs to the item above when
it overlaps that text column in x and follows with consistent leading (a
vertical gap under 1.5 line heights). Title-block lines that interleave in y
never enter a block because the block's x-span excludes them.

The same column join builds key-value legends (`legend.py`) through
`column_items`, so one routine owns the exact-string behaviour the tests
pin ("FURNISH AND INSTALL NEW GFCI TYPE CIRCUIT BREAKER WITH ALL REQUIRED
MOUNTING HARDWARE. BREAKER MANUFACTURER AND AIC RATING TO MATCH EXISTING.").

Pure functions over `PageWords`; no model, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from app.services.pagefacts.contracts import Bbox, Line, PageWords, Region, Word, round_box
from app.services.pagefacts.regions import in_plan_area

NOTE_REGION_KINDS: tuple[str, ...] = ("keynotes", "sheet_notes", "general_notes")

# Dashes the document may carry (U+2013, U+2014) become a plain hyphen in
# facts text so no fact ever prints one; the words blob keeps the original.
_DASHES = {0x2013: "-", 0x2014: "-", 0x2012: "-", 0x2015: "-", 0x2212: "-"}
_SPACES_RE = re.compile(r"\s+")

INLINE_ITEM_RE = re.compile(r"^\s*(?P<num>\d{1,2}|[A-Z])[.)]\s+(?P<text>\S.*)$")
GUTTER_NUMBER_RE = re.compile(r"^(?P<num>\d{1,2})[.)]?$|^(?P<alpha>[A-Z])[.)]$")
HASH_ONLY_RE = re.compile(r"^#+$")
CONTINUATION_GAP_LINES = 1.5
GUTTER_REACH = 0.012  # page fraction: a gutter number sits this close to the block's left edge
SAME_BASELINE = 0.6  # of the line height
MARKER_HEIGHT_TOL = 0.15
MARKER_CONFIDENCE = 0.6

CROSS_TRADE_RE = re.compile(
    r"ELECTRICAL\s+CONTRACTOR|DIVISION\s*26|DIV\.?\s*26|\bBY\s+E\.?C\.?\b|BY\s+ELECTRICAL"
    r"|POWER\s+(?:WIRING|CONNECTION)S?\s+BY|\bE\.C\.\s+(?:TO|SHALL)",
    re.I,
)
CROSS_TRADE_MAX_CHARS = 300


def plain_text(text: str) -> str:
    """Facts text: dashes normalized to "-", whitespace collapsed."""
    return _SPACES_RE.sub(" ", str(text).translate(_DASHES)).strip()


def line_height_frac(line: Line, pw: PageWords) -> float:
    """A line's glyph height as a page-height fraction."""
    if pw.page_h_in and pw.page_h_in > 0:
        return line.h / (pw.page_h_in * 72.0)
    return 0.01


def lines_in_box(pw: PageWords, bbox: Bbox) -> list[Line]:
    x0, y0, x1, y1 = bbox
    return [ln for ln in pw.lines if x0 <= ln.cx <= x1 and y0 <= ln.cy <= y1]


def words_in_box(pw: PageWords, bbox: Bbox) -> list[Word]:
    x0, y0, x1, y1 = bbox
    return [w for w in pw.words if x0 <= w.cx <= x1 and y0 <= w.cy <= y1]


def baseline_rows(pw: PageWords, *, max_gap_in: float = 0.6) -> list[list[Line]]:
    """Lines grouped by baseline (center y within 0.6 line heights) and then
    joined left to right while the gap to the previous line is under
    `max_gap_in` sheet inches (a label such as "PANEL 1LI" or a TOC row with
    dot leaders splits into several lines at the column gaps)."""
    bands: list[list[Line]] = []
    for ln in sorted(pw.lines, key=lambda x: (x.cy, x.b[0])):
        if bands:
            band = bands[-1]
            ref = band[0]
            h = max(line_height_frac(ref, pw), line_height_frac(ln, pw), 0.002)
            if abs(ln.cy - ref.cy) <= SAME_BASELINE * h:
                band.append(ln)
                continue
        bands.append([ln])
    rows: list[list[Line]] = []
    w_in = pw.page_w_in or 42.0
    for band in bands:
        band.sort(key=lambda x: x.b[0])
        cur: list[Line] = []
        for ln in band:
            if cur:
                gap_in = (ln.b[0] - cur[-1].b[2]) * w_in
                if not (-0.05 <= gap_in <= max_gap_in):
                    rows.append(cur)
                    cur = []
            cur.append(ln)
        if cur:
            rows.append(cur)
    rows.sort(key=lambda r: (r[0].cy, r[0].b[0]))
    return rows


def row_text(row: list[Line]) -> str:
    return plain_text(" ".join(ln.t for ln in row))


def union_box(boxes: list[Bbox]) -> Bbox:
    return round_box(
        (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
    )


@dataclass(slots=True)
class ColumnItem:
    """One gutter key with its joined text column."""

    key: str
    text: str
    bbox: Bbox
    key_bbox: Bbox | None
    lines: list[Line] = field(default_factory=list)


def _x_overlap(a: Bbox, b: Bbox) -> float:
    return min(a[2], b[2]) - max(a[0], b[0])


def column_items(
    pw: PageWords,
    lines: list[Line],
    *,
    key_re: re.Pattern[str] = GUTTER_NUMBER_RE,
    inline_re: re.Pattern[str] | None = INLINE_ITEM_RE,
    gutter_reach: float = GUTTER_REACH,
    max_key_chars: int = 3,
) -> tuple[list[ColumnItem], list[Line]]:
    """Join `lines` (one block, already sorted by y) into keyed items.

    A key line is a short line at the block's left gutter whose text matches
    `key_re`; its text is every line on the same baseline to its right plus
    the continuation lines below that overlap that text column in x and
    follow within 1.5 line heights. `inline_re` catches "1. TEXT" lines that
    carry the number in the text itself. Returns (items, leftover lines).
    """
    if not lines:
        return [], []
    ordered = sorted(lines, key=lambda ln: (ln.cy, ln.b[0]))
    gutter_x = min(ln.b[0] for ln in ordered)
    used: set[int] = set()
    items: list[ColumnItem] = []

    def is_key_line(ln: Line) -> bool:
        t = ln.t.strip()
        if len(t) > max_key_chars + 1 or ln.b[0] > gutter_x + gutter_reach:
            return False
        return bool(key_re.match(t))

    heads: list[tuple[int, str, Bbox | None, Line | None, str]] = []
    for i, ln in enumerate(ordered):
        t = ln.t.strip()
        if HASH_ONLY_RE.match(t):
            used.add(i)
            continue
        if is_key_line(ln):
            m = key_re.match(t)
            key = next((g for g in (m.groups() if m else ()) if g), t.rstrip(".)"))
            heads.append((i, key, list(ln.b), ln, ""))
            used.add(i)
            continue
        if inline_re is not None and ln.b[0] <= gutter_x + gutter_reach:
            m = inline_re.match(t)
            if m:
                heads.append((i, m.group("num"), None, ln, m.group("text")))
                used.add(i)
    if not heads:
        return [], list(ordered)

    for h_idx, (i, key, key_box, key_line, inline_text) in enumerate(heads):
        assert key_line is not None
        if h_idx + 1 < len(heads):
            nxt = ordered[heads[h_idx + 1][0]]
            # Lines on the next key's baseline belong to the next item.
            next_head_y = nxt.cy - SAME_BASELINE * line_height_frac(nxt, pw)
        else:
            next_head_y = 2.0
        h = line_height_frac(key_line, pw)
        texts: list[str] = [inline_text] if inline_text else []
        boxes: list[Bbox] = [list(key_line.b)]
        taken: list[Line] = [key_line]
        column: Bbox | None = list(key_line.b) if inline_text else None
        last_bottom = key_line.b[3]
        last_h = h
        # Same-baseline text to the right of a gutter key.
        if key_box is not None:
            for j, ln in enumerate(ordered):
                if j in used or ln.b[0] <= key_line.b[2] - 0.001:
                    continue
                if abs(ln.cy - key_line.cy) <= SAME_BASELINE * max(h, line_height_frac(ln, pw)):
                    texts.append(ln.t.strip())
                    boxes.append(list(ln.b))
                    taken.append(ln)
                    used.add(j)
                    column = union_box([column, ln.b]) if column else list(ln.b)
                    last_bottom = max(last_bottom, ln.b[3])
                    last_h = line_height_frac(ln, pw)
        # Continuation lines: below, overlapping the text column, consistent leading.
        for j, ln in enumerate(ordered):
            if j in used or ln.cy <= key_line.cy or ln.cy >= next_head_y - 1e-9:
                continue
            if column is None:
                continue
            if _x_overlap(ln.b, column) <= 0:
                continue
            if key_box is not None and ln.b[0] < key_line.b[2] - 0.001:
                continue  # a gutter-keyed item never swallows a gutter line
            gap = ln.b[1] - last_bottom
            if gap > CONTINUATION_GAP_LINES * max(last_h, line_height_frac(ln, pw)):
                continue
            texts.append(ln.t.strip())
            boxes.append(list(ln.b))
            taken.append(ln)
            used.add(j)
            column = union_box([column, ln.b])
            last_bottom = max(last_bottom, ln.b[3])
            last_h = line_height_frac(ln, pw)
        items.append(
            ColumnItem(
                key=plain_text(key),
                text=plain_text(" ".join(t for t in texts if t)),
                bbox=union_box(boxes),
                key_bbox=key_box,
                lines=taken,
            )
        )
    leftover = [ln for j, ln in enumerate(ordered) if j not in used]
    return items, leftover


def _heading_of(region: Region, lines: list[Line]) -> Line | None:
    if region.label is None:
        return None
    for ln in lines:
        if ln.t.strip() == region.label.strip():
            return ln
    return None


def note_blocks(pw: PageWords, regions: list[Region]) -> list[dict[str, Any]]:
    """`PageFacts.notes`: one block per keynotes, sheet-notes or general-notes
    region with its numbered items (exact text) and the unnumbered lines."""
    out: list[dict[str, Any]] = []
    for idx, region in enumerate(regions):
        if region.kind not in NOTE_REGION_KINDS:
            continue
        lines = lines_in_box(pw, region.bbox)
        heading = _heading_of(region, lines)
        body = [ln for ln in lines if ln is not heading]
        if heading is not None:
            # The heading's own baseline may carry a "#" column marker.
            body = [
                ln
                for ln in body
                if not (
                    abs(ln.cy - heading.cy) < SAME_BASELINE * line_height_frac(heading, pw)
                    and HASH_ONLY_RE.match(ln.t.strip())
                )
            ]
        items, leftover = column_items(pw, body)
        if not items and not leftover:
            continue
        out.append(
            {
                "kind": region.kind,
                "region": idx,
                "label": plain_text(region.label or ""),
                "bbox": list(region.bbox),
                "items": [
                    {"number": it.key, "text": it.text, "bbox": it.bbox} for it in items if it.text
                ],
                "unnumbered": [plain_text(ln.t) for ln in leftover if ln.t.strip()],
            }
        )
    return out


def keynote_texts(notes: list[dict[str, Any]]) -> dict[str, str]:
    """number -> text of the page's keynotes (first block wins)."""
    out: dict[str, str] = {}
    for block in notes:
        if block.get("kind") != "keynotes":
            continue
        for item in block.get("items", []):
            out.setdefault(str(item["number"]), item["text"])
    return out


def notes_item_count(notes: list[dict[str, Any]]) -> int:
    return sum(len(b.get("items", [])) for b in notes)


def _keynote_number_heights(pw: PageWords, regions: list[Region]) -> list[float]:
    hs: list[float] = []
    for region in regions:
        if region.kind != "keynotes":
            continue
        for ln in lines_in_box(pw, region.bbox):
            if GUTTER_NUMBER_RE.match(ln.t.strip()) or INLINE_ITEM_RE.match(ln.t.strip()):
                hs.append(ln.h)
    return hs


def keynote_markers(
    pw: PageWords,
    regions: list[Region],
    notes: list[dict[str, Any]],
    *,
    consumed: set[int] | None = None,
) -> list[dict[str, Any]]:
    """1 to 2 digit words in the plan area whose height is within 15% of the
    median height of this sheet's keynote numbers and that are not attached
    to a circuit tag (`consumed` word indices). Confidence 0.6: text alone
    cannot see the hexagon; measurement and census treat them as hints."""
    numbers = keynote_texts(notes)
    if not numbers:
        return []
    heights = _keynote_number_heights(pw, regions)
    if not heights:
        return []
    ref_h = float(median(heights))
    consumed = consumed or set()
    out: list[dict[str, Any]] = []
    for i, w in enumerate(pw.words):
        if i in consumed or w.r != 0 or not re.fullmatch(r"\d{1,2}", w.t):
            continue
        if w.t not in numbers:
            continue
        if abs(w.h - ref_h) > MARKER_HEIGHT_TOL * ref_h:
            continue
        if not in_plan_area(regions, w.cx, w.cy):
            continue
        out.append(
            {
                "number": w.t,
                "x": round(w.cx, 4),
                "y": round(w.cy, 4),
                "confidence": MARKER_CONFIDENCE,
            }
        )
    return out


def cross_trade_mentions(
    pages: dict[int, PageWords],
    *,
    notes_by_page: dict[int, list[dict[str, Any]]] | None = None,
    whole_document: bool,
) -> list[dict[str, Any]]:
    """Lines that hand work to the electrical contractor: every line of a
    non-electrical document (`whole_document`), else only the note items of
    an electrical one (its own notes mention the EC constantly, so a line
    scan would drown the signal)."""
    out: list[dict[str, Any]] = []
    for page in sorted(pages):
        pw = pages[page]
        if whole_document:
            for ln in pw.lines:
                if CROSS_TRADE_RE.search(ln.t):
                    out.append(
                        {
                            "page": page,
                            "bbox": list(ln.b),
                            "text": plain_text(ln.t)[:CROSS_TRADE_MAX_CHARS],
                        }
                    )
        else:
            for block in (notes_by_page or {}).get(page, []):
                for item in block.get("items", []):
                    if CROSS_TRADE_RE.search(item["text"]):
                        out.append(
                            {
                                "page": page,
                                "bbox": list(item["bbox"]),
                                "text": item["text"][:CROSS_TRADE_MAX_CHARS],
                            }
                        )
    return out
