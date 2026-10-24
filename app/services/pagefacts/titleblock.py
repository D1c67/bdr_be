"""Title block: sheet number, title and revision (WS1 5.3).

Sheet numbers come from document-level SLOT LEARNING: every page's
sheet-number-like tokens are clustered by position across the document; the
position whose values cover most pages and are mostly distinct is the slot
(a stamp that repeats on every page, a firm number or a project code fails
the distinctness rule). Per-page fallbacks are a label anchor ("SHEET NO."),
SHX annotation words and the splitter's page title. A page with none of
those gets `sheet_number: None, source: "none"`: a sheet number is never
invented.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass

from app.services.pagefacts.contracts import (
    F_SHX,
    Line,
    PageWords,
    Region,
    Revision,
    TitleBlockSlot,
)

SHEET_RE = re.compile(r"^[A-Z]{1,4}[-.]?\d{1,3}(?:[.\-]\d{1,3}){0,2}[A-Z]?$")
STOP_TOKENS: frozenset[str] = frozenset({"NO", "CKT", "SIM", "TYP", "UON"})
LABEL_RE = re.compile(
    r"SHEET\s*(?:NO\b|NUMBER\b|#)|DRAWING\s*(?:NO\b|NUMBER\b)|DWG\.?\s*NO\b", re.I
)
BOILERPLATE_RE = re.compile(
    r"SHEET\s*(?:NO|TITLE)|KEY\s*PLAN|REVISION|ISSUE|\bDATE\b|PROJECT|DRAWN|CHECKED|COPYRIGHT|PROPERTY|SEAL"
    r"|CONTACT|SUITE|\d{3}[.\-]\d{3}[.\-]\d{4}|www\.|\bPERMIT\b|\bSCALE\b",
    re.I,
)
TITLE_PREFIX_RE = re.compile(r"^\s*SHEET\s+TITLE\s*:?\s*", re.I)
REVISION_ROW_RE = re.compile(r"^(\d{1,2}|[A-Z])\s+(.+?)\s+(\d{1,2}/\d{1,2}/\d{2,4})$")
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")

SLOT_DX = 0.025
SLOT_DY = 0.02
SLOT_MIN_SCORE = 0.5
SLOT_MIN_MEMBERS = 2
EDGE_X = 0.75  # single-page fallback: the tallest candidate in the bottom-right corner
EDGE_Y = 0.80
LABEL_REACH_IN = 2.5
TITLE_ABOVE = 0.12
TITLE_BELOW = 0.02
TITLE_HEIGHT_RATIO = 0.7
TITLE_GAP_LINES = 1.5


@dataclass(slots=True)
class Candidate:
    text: str
    cx: float
    cy: float
    h: float
    index: int
    shx: bool = False


def clean_token(text: str) -> str:
    return text.strip().strip(":,;")


def is_sheet_token(text: str) -> bool:
    t = clean_token(text)
    return bool(SHEET_RE.match(t)) and any(ch.isdigit() for ch in t) and t not in STOP_TOKENS


def candidates(pw: PageWords) -> list[Candidate]:
    out: list[Candidate] = []
    for i, w in enumerate(pw.words):
        if is_sheet_token(w.t):
            out.append(Candidate(clean_token(w.t), w.cx, w.cy, w.h, i, bool(w.f & F_SHX)))
    return out


def _cell(c: Candidate) -> tuple[int, int]:
    return (int(c.cx // SLOT_DX), int(c.cy // SLOT_DY))


def _learn(page_cands: dict[int, list[Candidate]], pages: list[int]) -> TitleBlockSlot | None:
    n_pages = len(pages)
    if n_pages == 0:
        return None
    grid: dict[tuple[int, int], list[tuple[int, Candidate]]] = defaultdict(list)
    all_cands: list[tuple[int, Candidate]] = []
    for p in pages:
        for c in page_cands.get(p, []):
            grid[_cell(c)].append((p, c))
            all_cands.append((p, c))
    best: TitleBlockSlot | None = None
    seen: set[tuple[int, int]] = set()
    for _p, c in all_cands:
        key = (round(c.cx, 3), round(c.cy, 3))
        if key in seen:
            continue
        seen.add(key)
        gx, gy = _cell(c)
        per_page: dict[int, Candidate] = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for p, o in grid.get((gx + dx, gy + dy), ()):
                    if abs(o.cx - c.cx) < SLOT_DX and abs(o.cy - c.cy) < SLOT_DY:
                        if p not in per_page or o.h > per_page[p].h:
                            per_page[p] = o
        members = len(per_page)
        if members < SLOT_MIN_MEMBERS:
            continue  # one page cannot establish a position
        cover = members / n_pages
        values = [m.text for m in per_page.values()]
        distinct = len(set(values)) / members
        if members >= 3 and distinct < 0.5:
            continue
        edge = 1.0 if (c.cx > 0.75 or c.cy > 0.80) else 0.5
        score = cover * distinct * distinct * edge
        if best is None or score > best.score:
            best = TitleBlockSlot(
                x=c.cx, y=c.cy, cover=cover, distinct=distinct, score=score, pages=sorted(per_page)
            )
    return best


def learn_slots(page_words: dict[int, PageWords]) -> list[TitleBlockSlot]:
    """Up to two slots: slot 1 over every page (score >= 0.5), slot 2 over
    the pages slot 1 did not cover (two sheet sizes in one PDF; needs >= 2
    pages and score >= 0.5)."""
    page_cands = {p: candidates(pw) for p, pw in page_words.items()}
    pages = sorted(page_words)
    slots: list[TitleBlockSlot] = []
    first = _learn(page_cands, pages)
    if first is not None and first.score >= SLOT_MIN_SCORE:
        slots.append(first)
        rest = [p for p in pages if p not in set(first.pages)]
        if len(rest) >= 2:
            second = _learn(page_cands, rest)
            if second is not None and second.score >= SLOT_MIN_SCORE and len(second.pages) >= 2:
                slots.append(second)
    return slots


def slot_value(slot: TitleBlockSlot, cands: list[Candidate]) -> Candidate | None:
    near = [c for c in cands if abs(c.cx - slot.x) < SLOT_DX and abs(c.cy - slot.y) < SLOT_DY]
    if not near:
        return None
    return max(near, key=lambda c: c.h)


def label_anchor(pw: PageWords, cands: list[Candidate]) -> Candidate | None:
    """The tallest candidate within 2.5 in of a "SHEET NO" style label and to
    its right or below it."""
    labels = [ln for ln in pw.lines if LABEL_RE.search(ln.t)]
    if not labels:
        return None
    w_in, h_in = pw.page_w_in or 1.0, pw.page_h_in or 1.0
    best: Candidate | None = None
    for ln in labels:
        for c in cands:
            if c.cx < ln.b[0] - 0.005 and c.cy < ln.b[3] - 0.002:
                continue  # left of and above the label
            d = math.hypot((c.cx - ln.cx) * w_in, (c.cy - ln.cy) * h_in)
            if d <= LABEL_REACH_IN and (best is None or c.h > best.h):
                best = c
    return best


def splitter_sheet_number(title: str | None) -> str | None:
    """The leading sheet token of a splitter page title ("E2.01 POWER PLAN"
    -> "E2.01"); None when the title does not start with one."""
    if not title:
        return None
    first = title.strip().split()
    if not first:
        return None
    tok = clean_token(first[0]).upper()
    return tok if is_sheet_token(tok) else None


def sheet_number_for_page(
    pw: PageWords,
    slots: list[TitleBlockSlot],
    *,
    splitter_title: str | None = None,
    single_page: bool = False,
) -> tuple[str | None, str, Candidate | None]:
    """(sheet_number, source, candidate). Sources in order: learned slot,
    label anchor, SHX words, the bottom-right corner (single-page documents
    only: a heuristic guess reported as `corner`, or `shx` when the word is
    an SHX annotation, never as a learned slot), splitter title, none."""
    cands = candidates(pw)
    plain = [c for c in cands if not c.shx]
    shx = [c for c in cands if c.shx]
    for slot in slots:
        c = slot_value(slot, plain)
        if c is not None:
            return c.text, "slot", c
    c = label_anchor(pw, plain)
    if c is not None:
        return c.text, "label", c
    if shx:
        for slot in slots:
            c = slot_value(slot, shx)
            if c is not None:
                return c.text, "shx", c
        c = label_anchor(pw, shx)
        if c is not None:
            return c.text, "shx", c
    if not slots and single_page:
        corner = [c for c in cands if c.cx > EDGE_X and c.cy > EDGE_Y]
        if corner:
            c = max(corner, key=lambda c: c.h)
            return c.text, "shx" if c.shx else "corner", c
    number = splitter_sheet_number(splitter_title)
    if number:
        return number, "splitter", None
    return None, "none", None


def _frac_h(pw: PageWords, line: Line) -> float:
    return line.h / (pw.page_h_in * 72) if pw.page_h_in else 0.01


def sheet_title(
    pw: PageWords,
    slot_xy: tuple[float, float] | None,
    title_block: Region | None,
    *,
    sheet_number: str | None = None,
) -> str | None:
    """Title lines inside the title block within 0.12 above and 0.02 below
    the slot, boilerplate excluded, at least 0.7 x the tallest such line,
    contiguous, joined in reading order."""
    if slot_xy is None or title_block is None:
        return None
    _sx, sy = slot_xy
    cands: list[Line] = []
    for ln in pw.lines:
        if not title_block.contains(ln.cx, ln.cy):
            continue
        if not (sy - TITLE_ABOVE <= ln.cy <= sy + TITLE_BELOW):
            continue
        if BOILERPLATE_RE.search(ln.t):
            continue
        if not re.search(r"[A-Z]{3,}", ln.t):
            continue
        if sheet_number and clean_token(ln.t).upper() == sheet_number.upper():
            continue
        cands.append(ln)
    if not cands:
        return None
    tallest = max(ln.h for ln in cands)
    keep = sorted(
        (ln for ln in cands if ln.h >= TITLE_HEIGHT_RATIO * tallest),
        key=lambda ln: (ln.cy, ln.b[0]),
    )
    groups: list[list[Line]] = []
    for ln in keep:
        if groups:
            prev = groups[-1][-1]
            if ln.b[1] - prev.b[3] < TITLE_GAP_LINES * max(_frac_h(pw, prev), _frac_h(pw, ln)):
                groups[-1].append(ln)
                continue
        groups.append([ln])
    best = max(groups, key=lambda g: (max(ln.h for ln in g), len(g)))
    text = " ".join(ln.t.strip() for ln in best)
    text = TITLE_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def revision(pw: PageWords, regions: list[Region]) -> Revision | None:
    """The latest revision row (number, description, date) in the revision
    block; rows are matched as one line or as aligned words on one band."""
    found = [r for r in regions if r.kind == "revision_block"]
    if not found:
        return None
    # "REVISION" and "ISSUE DATE" seed two blocks over one table: parse their union.
    union = Region(
        kind="revision_block",
        bbox=[
            min(r.bbox[0] for r in found),
            min(r.bbox[1] for r in found),
            max(r.bbox[2] for r in found),
            max(r.bbox[3] for r in found),
        ],
    )
    blocks = [union]
    rows: list[Revision] = []
    for block in blocks:
        lines = [ln for ln in pw.lines if block.contains(ln.cx, ln.cy)]
        for ln in lines:
            m = REVISION_ROW_RE.match(ln.t.strip())
            if m:
                rows.append(
                    Revision(number=m.group(1), description=m.group(2).strip(), date=m.group(3))
                )
        if rows:
            continue
        dates = [ln for ln in lines if DATE_RE.match(ln.t.strip())]
        for d in dates:
            tol = 0.6 * _frac_h(pw, d) + 0.004
            band = [ln for ln in lines if abs(ln.cy - d.cy) < tol and ln is not d]
            row_h = max(_frac_h(pw, d), 0.002)
            band.sort(key=lambda ln: (round(ln.cy / (0.5 * row_h)), ln.b[0]))
            number = None
            desc: list[str] = []
            for ln in band:
                t = ln.t.strip()
                if number is None and re.fullmatch(r"\d{1,2}|[A-Z]", t):
                    number = t
                elif re.search(r"[A-Za-z]", t):
                    desc.append(t)
            rows.append(
                Revision(number=number, description=" ".join(desc) or None, date=d.t.strip())
            )
    if not rows:
        return None

    def key(r: Revision) -> tuple[int, int, int]:
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", r.date or "")
        if not m:
            return (0, 0, 0)
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return (y, int(m.group(1)), int(m.group(2)))

    return max(rows, key=key)
