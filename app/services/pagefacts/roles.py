"""Multi-label page roles (WS1 5.6) in the existing `PAGE_ROLES` vocabulary
of `plans_reader_schemas`, so the ledger and splitter stay compatible.

A page can be a legend AND a notes sheet (7154 E0.00), which is exactly what
the single-role ledger lost (E16). Scores: title keywords (3), region and
parsed-content evidence (2 to 4), plan evidence (a scaled plan view 3, most
words in the plan area 1), body headings (1 each, at most 2 per role).
`roles` = every role at or above 3; `page_role` = the highest score with the
tie-break order of `ROLE_PRIORITY`; no role gives `other`.

Part A runs on region proxies (legend region line count, numbered-note
lines, numeric table rows); part B passes parsed evidence counts through
`evidence` and those take precedence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.pagefacts.contracts import (
    ROLE_PRIORITY,
    ROLE_THRESHOLD,
    ROLES,
    PageWords,
    Region,
    View,
)
from app.services.pagefacts.regions import is_heading, lines_in, median_line_height

TITLE_KEYWORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "cover_index",
        re.compile(r"COVER|COVERSHEET|TITLE\s+SHEET|SHEET\s+INDEX|DRAWING\s+INDEX", re.I),
    ),
    ("legend_symbols", re.compile(r"LEGEND|SYMBOL|ABBREVIATION", re.I)),
    (
        "general_notes",
        re.compile(r"^(?!\s*(?:SHEET|PLAN|KEY)\b).*(?:\bNOTES?\b|SPECIFICATION)", re.I),
    ),
    ("schedule", re.compile(r"SCHEDULE", re.I)),
    ("diagram", re.compile(r"ONE[\s-]?LINE|SINGLE[\s-]?LINE|RISER|DIAGRAM", re.I)),
    ("detail", re.compile(r"DETAIL|SECTIONS|ELEVATIONS", re.I)),
    ("site", re.compile(r"\bSITE\b|\bCIVIL\b|ROADWAY|UTILITY", re.I)),
    ("plan", re.compile(r"\bPLAN\b", re.I)),
)
TITLE_WEIGHT = 3
HEADING_WEIGHT = 1
HEADING_CAP = 2
NUMBERED_ITEM_RE = re.compile(r"^\s*(?:\d{1,2}|[A-Z])[.)]?\s+\S")
GUTTER_NUMBER_RE = re.compile(r"^\s*\d{1,2}[.)]?\s*$")  # the item number alone, left of its text
NUMERIC_TOKEN_RE = re.compile(r"^[\d.,/#-]+$")
NUMERIC_CELL_RE = re.compile(r"^[\d.,/#-]*\d[\d.,/#-]*$")

LEGEND_MIN_ENTRIES = 8
LEGEND_MIN_MODIFIERS = 3
NOTES_MIN_ITEMS = 5
NOTES_SHARE = 0.4
TABLE_MIN_ROWS = 20
PLAN_WORD_SHARE = 0.6


@dataclass(slots=True)
class RoleEvidence:
    """Parsed-content evidence from part B (counts); None means "use the
    region proxy"."""

    legend_entries: int | None = None
    legend_modifiers: int | None = None
    confident_schedules: int | None = None
    notes_items: int | None = None


def _numbered_lines(lines: list, *, gutter: bool = False) -> int:
    """Lines that start a numbered item; inside a notes block the bare
    number in the gutter counts too (it is its own line when the text sits
    more than a word gap to its right)."""
    n = 0
    for ln in lines:
        if NUMBERED_ITEM_RE.match(ln.t) or (gutter and GUTTER_NUMBER_RE.match(ln.t)):
            n += 1
    return n


def _numeric_rows(pw: PageWords) -> int:
    """Table evidence without a parser: lines with 3 or more numeric tokens
    (a row that stayed on one line) and numeric cells on their own (a cell
    per line when column gaps split the row), counted together."""
    rows = 0
    cells = 0
    for ln in pw.lines:
        toks = ln.t.split()
        if (
            len(toks) >= 3
            and sum(1 for t in toks if NUMERIC_TOKEN_RE.match(t) and any(c.isdigit() for c in t))
            >= 3
        ):
            rows += 1
        elif len(toks) == 1 and NUMERIC_CELL_RE.match(toks[0]):
            cells += 1
    return rows + cells // 4


def score_roles(
    title: str | None,
    pw: PageWords,
    regions: list[Region],
    views: list[View],
    *,
    evidence: RoleEvidence | None = None,
) -> tuple[list[str], str, dict[str, int]]:
    """(roles, page_role, role_scores)."""
    ev = evidence or RoleEvidence()
    scores: dict[str, int] = {r: 0 for r in ROLES}

    if title:
        for role, rx in TITLE_KEYWORDS:
            if rx.search(title):
                scores[role] += TITLE_WEIGHT

    legend_regions = [r for r in regions if r.kind == "legend"]
    abbreviation_regions = [r for r in regions if r.kind == "abbreviations"]
    notes_regions = [r for r in regions if r.kind in ("general_notes", "sheet_notes")]
    schedule_regions = [r for r in regions if r.kind == "schedule"]
    title_block = next((r for r in regions if r.kind == "title_block"), None)
    plan_area = next((r for r in regions if r.kind == "plan_area"), None)

    legend_entries = ev.legend_entries
    if legend_entries is None:
        legend_entries = sum(
            len(lines_in(pw, r)) - 1 for r in legend_regions + abbreviation_regions
        )
    if legend_entries >= LEGEND_MIN_ENTRIES:
        scores["legend_symbols"] += 4
    if (ev.legend_modifiers or 0) >= LEGEND_MIN_MODIFIERS:
        scores["legend_symbols"] += 2

    confident = ev.confident_schedules
    if confident is None:
        confident = (
            1
            if (schedule_regions or title and "SCHEDULE" in title.upper())
            and _numeric_rows(pw) >= TABLE_MIN_ROWS
            else 0
        )
    if confident >= 1:
        scores["schedule"] += 4

    notes_items = ev.notes_items
    if notes_items is None:
        notes_items = sum(_numbered_lines(lines_in(pw, r), gutter=True) for r in notes_regions)
    if notes_items >= NOTES_MIN_ITEMS:
        # Sheet notes and keynotes belong to their plan sheet: they count
        # half, so a plan page never becomes a general-notes page on its own.
        scores["general_notes"] += 2 if any(r.kind == "general_notes" for r in notes_regions) else 1
    body_lines = [
        ln for ln in pw.lines if title_block is None or not title_block.contains(ln.cx, ln.cy)
    ]
    if body_lines and _numbered_lines(body_lines) / len(body_lines) >= NOTES_SHARE:
        scores["general_notes"] += 3

    if any(
        v.kind in ("plan", "enlarged_plan") and not v.example and v.ft_per_in is not None
        for v in views
    ):
        scores["plan"] += 3
    if plan_area is not None:
        body_words = [
            w for w in pw.words if title_block is None or not title_block.contains(w.cx, w.cy)
        ]
        if body_words:
            inside = sum(1 for w in body_words if plan_area.contains(w.cx, w.cy))
            excluded = [r for r in regions if r.kind not in ("plan_area", "title_block")]
            inside -= sum(1 for w in body_words if any(r.contains(w.cx, w.cy) for r in excluded))
            if inside / len(body_words) >= PLAN_WORD_SHARE:
                scores["plan"] += 1

    med = median_line_height(pw.lines)
    heading_hits: dict[str, int] = {r: 0 for r in ROLES}
    for ln in body_lines:
        if not is_heading(ln, med):
            continue
        for role, rx in TITLE_KEYWORDS:
            if rx.search(ln.t) and heading_hits[role] < HEADING_CAP:
                heading_hits[role] += HEADING_WEIGHT
    for role, n in heading_hits.items():
        scores[role] += n

    roles = [r for r in ROLE_PRIORITY if scores.get(r, 0) >= ROLE_THRESHOLD]
    if roles:
        page_role = max(roles, key=lambda r: (scores[r], -ROLE_PRIORITY.index(r)))
    else:
        page_role = "other"
    nonzero = {r: s for r, s in scores.items() if s > 0}
    return roles, page_role, nonzero
