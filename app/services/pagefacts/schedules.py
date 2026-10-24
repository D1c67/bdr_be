"""Schedules (WS1 step 5): the table finder plus the panel and luminaire
parsers. Equipment and feeder parsers are deferred to v1.1 (their tables are
not detected; nothing is guessed).

Tables are built from words, never lines: a CAD export joins a row's AMPS and
LOAD DESCRIPTION cells into one line when the gap is narrow, so every word is
placed by its x-center into a column band whose edges sit midway between the
neighbouring header cells' edges (left-aligned text under a centered header
still lands in its own column). Stacked header words (K over E over Y, WIRE
over SIZE over H N G, OCPD over AMPS) merge by x-center. Two-sided panel
schedules carry two CKT columns; the shared phase (A B C) columns split by
the header center into the left and right circuit of each row.

Circuit status is POSITIVE evidence only (spec 6 WS1):
- `spare` / `space` from the description;
- `new` from a keynote linked to the row whose text matches NEW or FURNISH
  AND INSTALL ... BREAKER (status_basis = the keynote text) or from NEW in
  the description. A panel title without an (E)/EXISTING marker is NOT
  evidence that its loads are new breakers (firms that do not stamp (E)
  would turn every row into a new breaker, the E12 failure mode);
- `relocated` from RELOCAT in the description;
- `existing` when the row says EXISTING LOAD or carries an (E) marker with no
  other load name; `existing_reused` when the (E) or EXISTING marker sits on
  a named load, or the load name matches a demolition-plan load;
- otherwise `unknown` with the signals that were seen: `bold`, `keynote`,
  `n_tag`, `wire_filled`, `vd_filled`, `panel_not_marked_existing` and
  `revision_delta` (a word equal to the sheet's revision number sitting on
  the row's band just outside the table, the text-layer trace of a revision
  delta; revision clouds are vector-only and are never detected).

Never guess a value: a cell that does not parse stays null and the row is
listed in `unparsed_rows`; `parse_confidence = rows_valid / rows_total`.

Pure functions over `PageWords`; no model, no I/O.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from app.services.pagefacts.contracts import F_BOLD, Bbox, Line, PageWords, Word, round_box
from app.services.pagefacts.notes import plain_text, union_box
from app.services.pagefacts.regions import median_line_height

SCHEDULE_KINDS: tuple[str, ...] = ("panel", "luminaire", "equipment", "feeder", "generic")
CIRCUIT_STATUSES: tuple[str, ...] = (
    "new",
    "existing",
    "existing_reused",
    "spare",
    "space",
    "relocated",
    "unknown",
)

PANEL_TITLE_RE = re.compile(
    r"^(?P<pre>\((?:E|N|R)\)\s*|EXISTING\s+|NEW\s+)?(?:(?:BRANCH|DISTRIBUTION|LIGHTING|POWER)\s+)?(?:PANEL(?:BOARD)?|PNL)\s*:?\s*['\"]?"
    r"(?P<name>[A-Z0-9][A-Z0-9\-]{0,7})['\"]?\s*(?:\((?P<post>E|EXISTING|NEW|N)\))?\s*$",
    re.I,
)
# Words a title regex match can never name a panel with ("PANEL AND" in a spec sentence, "Panel Totals").
PANEL_TITLE_STOP_NAMES: frozenset[str] = frozenset({"SCHEDULE", "SCHEDULES", "BOARD", "AND", "OR", "THE", "TO", "FOR", "INTO", "STUB", "WITH", "PER", "TOTALS", "TOTAL", "S", "IS", "AS", "AT", "ON", "IN", "OF", "BY"})
END_RE = re.compile(
    r"LOAD\s+CLASSIFICATION|^TOTALS?\b|CONNECTED\s+LOAD|^NOTES?\s*:|CIRCUIT\s+KEY\s+NOTES|ESTIMATED\s+DEMAND",
    re.I,
)
PLACEHOLDERS: frozenset[str] = frozenset({"-", "--", "- -", "---", "\u2013", "\u2014"})

PANEL_HEADER_ROLES: dict[str, str] = {
    "CKT": "ckt",
    "CKT NO.": "ckt",
    "CKT NO": "ckt",
    "CKT #": "ckt",
    "CIRCUIT": "ckt",
    "CIRCUIT NO.": "ckt",
    "CIRCUIT NO": "ckt",
    "CIRCUIT #": "ckt",
    "NO.": "ckt",
    "NO": "ckt",
    "#": "ckt",
    "KEY": "key",
    "K": "key",
    "E": "key",
    "Y": "key",
    "NOTE": "key",
    "NOTES": "key",
    "REMARKS": "key",
    "LOAD DESCRIPTION": "desc",
    "DESCRIPTION": "desc",
    "LOAD": "desc",
    "LOAD NAME": "desc",
    "CIRCUIT DESCRIPTION": "desc",
    "OCPD AMPS": "trip",
    "OCPD": "trip",
    "AMPS": "trip",
    "AMP": "trip",
    "TRIP": "trip",
    "BKR": "trip",
    "BREAKER": "trip",
    "BKR AMPS": "trip",
    "P": "poles",
    "POLE": "poles",
    "POLES": "poles",
    "WIRE": "wire",
    "WIRE SIZE": "wire",
    "WIRE SIZE N": "wire_n",
    "H": "wire_h",
    "N": "wire_n",
    "G": "wire_g",
    "VD": "vd",
    "VD %": "vd",
    "%": "vd",
    "A": "load_a",
    "B": "load_b",
    "C": "load_c",
    "VA": "va",
    "KVA": "va",
    "LOAD VA": "va",
    "LOAD (VA)": "va",
    "PHASE": "phase",
}
PANEL_ANCHOR_ROLES: frozenset[str] = frozenset({"ckt", "desc", "trip", "poles"})

LUMINAIRE_HEADER_ROLES: dict[str, str] = {
    "TAG": "tag",
    "TYPE": "tag",
    "MARK": "tag",
    "FIXTURE TYPE": "tag",
    "FIXTURE": "tag",
    "SYMBOL": "symbol",
    "DESCRIPTION": "description",
    "MTG": "mounting",
    "MOUNTING": "mounting",
    "MOUNT": "mounting",
    "MTG.": "mounting",
    "LAMP TYPE": "lamp",
    "LAMP": "lamp",
    "LAMPS": "lamp",
    "SOURCE": "lamp",
    "LIGHT SOURCE": "lamp",
    "LUMENS": "lumens",
    "DELIVERED LUMENS": "lumens",
    "WATTS": "watts",
    "WATTAGE": "watts",
    "INPUT WATTS": "watts",
    "VOLTAGE": "volts",
    "VOLTS": "volts",
    "VOLT": "volts",
    "CONTROL": "control",
    "CONTROL METHOD": "control",
    "CONTROLS": "control",
    "DIMMING": "control",
    "MANUFACTURER": "manufacturer",
    "MANUFACTURER - SERIES": "manufacturer",
    "MANUFACTURER SERIES": "manufacturer",
    "CATALOG": "manufacturer",
    "CATALOG NUMBER": "manufacturer",
    "MODEL": "manufacturer",
    "SERIES": "manufacturer",
    "NOTES": "notes",
    "REMARKS": "notes",
}
LUMINAIRE_TAG_RE = re.compile(r"^[A-Z]{1,3}\d{0,3}[A-Z]?(?:-EM)?$")
LINEAR_HINT_RE = re.compile(
    r"UNDER\s*CABINET|\bLINEAR\b|\bSTRIP\b|\bTAPE\b|\bCOVE\b|\bSLOT\b|LIGHT\s*BAR|\bTRACK\b|CONTINUOUS|\bLF\b",
    re.I,
)
EMERGENCY_HINT_RE = re.compile(r"\bEM\b|BATTERY|EMERGENCY", re.I)
WATTS_PER_FT_RE = re.compile(r"/\s*(?:FT|LF|LIN)", re.I)

TRIP_VALUES: frozenset[int] = frozenset(
    {
        15,
        20,
        25,
        30,
        35,
        40,
        45,
        50,
        60,
        70,
        80,
        90,
        100,
        110,
        125,
        150,
        175,
        200,
        225,
        250,
        300,
        350,
        400,
        450,
        500,
        600,
        700,
        800,
        1000,
        1200,
    }
)
WIRE_SIZES: frozenset[str] = frozenset(
    {
        "14",
        "12",
        "10",
        "8",
        "6",
        "4",
        "3",
        "2",
        "1",
        "1/0",
        "2/0",
        "3/0",
        "4/0",
        "250",
        "300",
        "350",
        "400",
        "500",
        "600",
        "750",
    }
)
NEW_BREAKER_NOTE_RE = re.compile(r"\b(?:NEW|FURNISH\s+AND\s+INSTALL)\b.*\bBREAKER\b", re.I)
REVISION_DELTA_REACH_IN = 0.75  # a delta sits this close outside the table, on the row band
REVISION_DELTA_RE = re.compile(r"^(?:Δ|REV\.?\s*)?(?P<n>\d{1,2}|[A-Z])$")
GFCI_RE = re.compile(r"\bGF(?:C)?I\b|GROUND\s+FAULT|\bGF\b", re.I)
EXISTING_MARK_RE = re.compile(r"\(E\)|\bEXISTING\b|\bEXIST\.?\b", re.I)
EXISTING_LOAD_ONLY_RE = re.compile(
    r"^\(?E?\)?\s*(?:EXISTING\s+LOAD|EXISTING|EXIST\.?)\s*(?:\(E\))?$", re.I
)
N_TAG_RE = re.compile(r"\(N\)")

HEADER_STACK_X = 0.006
HEADER_BAND_LINES = 4.0
HEADER_BAND_IN = 0.42  # a stacked header spans at most this many sheet inches
ROW_TOL = 0.4
ROW_END_GAP = 2.5
TITLE_HEIGHT_RATIO = 1.3
TITLE_REACH_Y = 0.25


# Shared table machinery ---------------------------------------------------------------


@dataclass(slots=True)
class HeaderCell:
    label: str
    role: str
    x0: float
    x1: float
    y0: float
    y1: float
    words: list[Word] = field(default_factory=list)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass(slots=True)
class Table:
    kind: str
    page: int
    cells: list[HeaderCell]
    x0: float
    x1: float
    header_y0: float
    header_y1: float
    edges: list[float]
    rows: list[list[Word]] = field(default_factory=list)
    title: str | None = None
    title_line: Line | None = None
    end_y: float = 1.0

    def column_of(self, x: float) -> int:
        """Index into `cells` for an x-center (bands between edge midpoints)."""
        return min(max(bisect_right(self.edges, x) - 1, 0), len(self.cells) - 1)

    def cell(self, x: float) -> HeaderCell:
        return self.cells[self.column_of(x)]

    def bbox(self) -> Bbox:
        y1 = max((w.b[3] for r in self.rows for w in r), default=self.header_y1)
        return round_box((self.x0, self.header_y0, self.x1, y1))


def _h_frac(h_pt: float, pw: PageWords) -> float:
    return h_pt / ((pw.page_h_in or 30.0) * 72.0)


def _is_placeholder(text: str) -> bool:
    return text.strip() in PLACEHOLDERS


def _merge_horizontal(words: list[Word], pw: PageWords, roles: dict[str, str]) -> list[Word]:
    """Join side-by-side header words that form one known label (LAMP TYPE,
    LOAD DESCRIPTION, CONTROL METHOD) into a single word."""
    ordered = sorted(words, key=lambda w: (w.cy, w.b[0]))
    out: list[Word] = []
    i = 0
    while i < len(ordered):
        w = ordered[i]
        merged = w
        j = i + 1
        while j < len(ordered):
            nxt = ordered[j]
            h = _h_frac(max(merged.h, nxt.h), pw)
            same_row = abs(nxt.cy - merged.cy) <= 0.6 * h
            gap = (nxt.b[0] - merged.b[2]) * (pw.page_h_in or 30.0) / (pw.page_w_in or 42.0)
            combined = f"{merged.t} {nxt.t}".upper().rstrip(":")
            if same_row and -0.2 * h <= gap <= 1.5 * h and combined in roles:
                merged = Word(
                    t=f"{merged.t} {nxt.t}",
                    b=round_box(
                        (
                            merged.b[0],
                            min(merged.b[1], nxt.b[1]),
                            nxt.b[2],
                            max(merged.b[3], nxt.b[3]),
                        )
                    ),
                    h=max(merged.h, nxt.h),
                    r=merged.r,
                    f=merged.f | nxt.f,
                )
                ordered.pop(j)
                continue
            if not same_row:
                break
            j += 1
        out.append(merged)
        i += 1
    return out


def _stack_cells(
    words: list[Word], pw: PageWords, roles: dict[str, str] | None = None
) -> list[HeaderCell]:
    """Merge header words stacked vertically (same x-center within 0.006,
    vertically adjacent) into one cell; labels join top to bottom, single
    letters without spaces (K E Y -> KEY). Side-by-side words forming one
    known label merge first; adjacent cells of one role merge last."""
    if roles:
        words = _merge_horizontal(words, pw, roles)
    ordered = sorted(words, key=lambda w: (w.cy, w.cx))
    groups: list[list[Word]] = []
    for w in ordered:
        placed = False
        for g in groups:
            if abs(w.cx - sum(x.cx for x in g) / len(g)) > HEADER_STACK_X:
                continue
            h = _h_frac(max(max(x.h for x in g), w.h), pw)
            if any(abs(w.cy - x.cy) <= 0.6 * h for x in g):
                continue  # same baseline: a neighbouring column, not a stack
            top = min(x.b[1] for x in g)
            bottom = max(x.b[3] for x in g)
            if w.b[1] <= bottom + 1.3 * h and w.b[3] >= top - 1.3 * h:
                g.append(w)
                placed = True
                break
        if not placed:
            groups.append([w])
    cells: list[HeaderCell] = []
    for g in groups:
        g.sort(key=lambda w: w.cy)
        parts = [w.t for w in g]
        joiner = "" if all(len(p) == 1 for p in parts) and len(parts) > 1 else " "
        label = plain_text(joiner.join(parts)).upper()
        cells.append(
            HeaderCell(
                label=label,
                role="",
                x0=min(w.b[0] for w in g),
                x1=max(w.b[2] for w in g),
                y0=min(w.b[1] for w in g),
                y1=max(w.b[3] for w in g),
                words=g,
            )
        )
    cells.sort(key=lambda c: c.cx)
    if roles:
        merged: list[HeaderCell] = []
        for c in cells:
            role = roles.get(c.label, "")
            prev = merged[-1] if merged else None
            if (
                prev is not None
                and role
                and roles.get(prev.label, "") == role
                and (c.x0 - prev.x1) * (pw.page_h_in or 30.0) / (pw.page_w_in or 42.0)
                <= 3.0 * _h_frac(max(c.y1 - c.y0, 0.001) * (pw.page_h_in or 30.0) * 72.0, pw)
                and role
                not in ("load_a", "load_b", "load_c", "ckt", "key", "wire_h", "wire_n", "wire_g")
            ):
                prev.label = f"{prev.label} {c.label}" if prev.label != c.label else prev.label
                prev.x1 = max(prev.x1, c.x1)
                prev.y0 = min(prev.y0, c.y0)
                prev.y1 = max(prev.y1, c.y1)
                prev.words.extend(c.words)
                continue
            merged.append(c)
        cells = merged
    return cells


EDGE_MIN_GAP = 0.004  # facing header edges closer than this fall back to the centers


def _edges(cells: list[HeaderCell], x0: float, x1: float) -> list[float]:
    """Band edges: midway between adjacent header cells' facing edges (so
    left-aligned text under a centered header stays in its column); when
    two headers touch or overlap, midway between their centers."""
    edges = [x0]
    for a, b in zip(cells, cells[1:], strict=False):
        if b.x0 - a.x1 >= EDGE_MIN_GAP:
            edges.append((a.x1 + b.x0) / 2)
        else:
            edges.append((a.cx + b.cx) / 2)
    edges.append(x1)
    return edges


TEXT_ROLES: frozenset[str] = frozenset({"desc", "description", "manufacturer", "notes"})


def _place_words(row: list[Word], table: Table, pw: PageWords) -> list[tuple[Word, HeaderCell]]:
    """(word, cell) for one row: by x-center band, except that a word one
    word-space after a text cell's word stays in that text cell (long
    descriptions overflow their band)."""
    out: list[tuple[Word, HeaderCell]] = []
    width_unit = (pw.page_h_in or 30.0) / (pw.page_w_in or 42.0)
    for w in sorted(row, key=lambda x: x.b[0]):
        cell = table.cell(w.cx)
        if out:
            prev_w, prev_cell = out[-1]
            gap = (w.b[0] - prev_w.b[2]) * width_unit
            h = _h_frac(max(prev_w.h, w.h), pw)
            if (
                prev_cell.role in TEXT_ROLES
                and cell is not prev_cell
                and -0.2 * h <= gap <= 0.8 * h
                and w.b[0] < cell.x0
            ):
                cell = prev_cell
        out.append((w, cell))
    return out


def _row_clusters(words: list[Word], pw: PageWords) -> list[list[Word]]:
    if not words:
        return []
    hs = [w.h for w in words if w.h > 1.5]
    tol = ROW_TOL * _h_frac(float(median(hs)) if hs else 7.0, pw)
    ordered = sorted(words, key=lambda w: (w.cy, w.b[0]))
    rows: list[list[Word]] = []
    centers: list[float] = []
    for w in ordered:
        if rows and abs(w.cy - centers[-1]) <= tol:
            rows[-1].append(w)
            centers[-1] = (centers[-1] * (len(rows[-1]) - 1) + w.cy) / len(rows[-1])
        else:
            rows.append([w])
            centers.append(w.cy)
    for r in rows:
        r.sort(key=lambda w: w.b[0])
    return rows


def _body_rows(
    pw: PageWords, table: Table, *, end_lines: list[Line], hard_end_y: float
) -> list[list[Word]]:
    """Rows under the header inside the table's x-span until an end line, a
    hard y limit (next table title) or a gap of 2.5 row pitches."""
    end_y = hard_end_y
    for ln in end_lines:
        if ln.cy > table.header_y1 and table.x0 - 0.01 <= ln.cx <= table.x1 + 0.01:
            end_y = min(end_y, ln.b[1] - 0.001)
    body = [
        w
        for w in pw.words
        if table.header_y1 < w.cy < end_y
        and table.x0 <= w.cx <= table.x1
        and w.r == 0
        and not _is_placeholder(w.t)
    ]
    rows = _row_clusters(body, pw)
    if len(rows) < 2:
        table.end_y = end_y
        return rows
    pitches = [
        sum(w.cy for w in b) / len(b) - sum(w.cy for w in a) / len(a)
        for a, b in zip(rows, rows[1:], strict=False)
    ]
    pitch = float(median(pitches)) if pitches else 0.01
    kept: list[list[Word]] = [rows[0]]
    for prev, row in zip(rows, rows[1:], strict=False):
        gap = sum(w.cy for w in row) / len(row) - sum(w.cy for w in prev) / len(prev)
        if pitch > 0 and gap > ROW_END_GAP * pitch:
            break
        kept.append(row)
    table.end_y = max(w.b[3] for r in kept for w in r) if kept else end_y
    return kept


def _header_bands(pw: PageWords, roles: dict[str, str], *, limit_x: float) -> list[list[Word]]:
    """Candidate header words grouped into horizontal bands (a band spans
    up to four line heights so stacked headers stay together)."""
    line_of: dict[int, Line] = {}
    for ln in pw.lines:
        for wi in range(ln.w[0], ln.w[1]):
            line_of[wi] = ln

    def is_cand(i: int, w: Word) -> bool:
        t = w.t.strip().upper()
        if t.endswith(":") or t not in roles or w.r != 0 or w.cx >= limit_x:
            return False
        # Every word of the line must be header vocabulary ("EXISTING LOAD"
        # is a cell, "LOAD DESCRIPTION" a header).
        ln = line_of.get(i)
        if ln is None:
            return True
        parts = [pw.words[k].t.strip().upper() for k in range(ln.w[0], ln.w[1])]
        if all(part in roles or not any(ch.isalnum() for ch in part) for part in parts):
            return True
        label = re.sub(r"\s*[-/]\s*", " ", plain_text(ln.t).upper().rstrip(":"))
        return label in roles

    cands = [w for i, w in enumerate(pw.words) if is_cand(i, w)]
    cands.sort(key=lambda w: w.cy)
    window = HEADER_BAND_IN / (pw.page_h_in or 30.0)
    bands: list[list[Word]] = []
    for w in cands:
        if bands:
            last = bands[-1]
            top = min(x.b[1] for x in last)
            h = _h_frac(max(x.h for x in last), pw)
            if w.b[1] - top <= max(HEADER_BAND_LINES * h, window):
                last.append(w)
                continue
        bands.append([w])
    return bands


# Panel schedules ---------------------------------------------------------------------


def _panel_titles(pw: PageWords, *, limit_x: float) -> list[tuple[Line, str, bool]]:
    med = median_line_height(pw.lines)
    out: list[tuple[Line, str, bool]] = []
    for ln in pw.lines:
        if ln.cx >= limit_x:
            continue
        m = PANEL_TITLE_RE.match(plain_text(ln.t))
        if not m:
            continue
        name = m.group("name").upper()
        if name in PANEL_TITLE_STOP_NAMES:
            continue
        tall = med > 0 and ln.h >= TITLE_HEIGHT_RATIO * med
        if not tall:
            # A small title still counts when panel header fields follow it.
            below = [
                x for x in pw.lines if 0 < x.cy - ln.cy < 0.08 and abs(x.b[0] - ln.b[0]) < 0.12
            ]
            if not any(re.search(r"VOLTS?|MAIN|MOUNTING|BUS|FED\s+FROM", x.t, re.I) for x in below):
                continue
        pre = (m.group("pre") or "").upper()
        post = (m.group("post") or "").upper()
        existing = "(E)" in pre or "EXISTING" in pre or post in ("E", "EXISTING")
        out.append((ln, name, existing))
    return out


def _assign_roles(cells: list[HeaderCell], roles: dict[str, str]) -> None:
    for c in cells:
        c.role = roles.get(c.label, "") or roles.get(c.label.split(" ")[0], "")
        if c.label.startswith("WIRE") and c.label[-1] in "HNG" and len(c.label) > 4:
            c.role = {"H": "wire_h", "N": "wire_n", "G": "wire_g"}[c.label[-1]]
        if not c.role and c.label.startswith("VD"):
            c.role = "vd"
        if not c.role and "DESCRIPTION" in c.label:
            c.role = "desc"
        if not c.role and c.label.startswith("OCPD"):
            c.role = "trip"
    # A bare WIRE (SIZE) label over H N G letter columns is a group header,
    # not a column of its own.
    letter_cells = [c for c in cells if c.role in ("wire_h", "wire_n", "wire_g")]
    for c in cells:
        if c.role == "wire" and any(abs(c.cx - x.cx) < 0.01 for x in letter_cells):
            c.role = ""


def _split_title_span(
    titles: list[tuple[Line, str, bool]], idx: int, band_x0: float, band_x1: float
) -> tuple[float, float]:
    """A panel's x-span when several titles share one header band: midway
    between neighbouring title boxes."""
    centers = sorted((t[0].cx, i) for i, t in enumerate(titles))
    pos = next(k for k, (_c, i) in enumerate(centers) if i == idx)
    me = titles[idx][0]
    x0 = band_x0
    x1 = band_x1
    if pos > 0:
        left = titles[centers[pos - 1][1]][0]
        x0 = max(x0, (left.b[2] + me.b[0]) / 2)
    if pos + 1 < len(centers):
        right = titles[centers[pos + 1][1]][0]
        x1 = min(x1, (me.b[2] + right.b[0]) / 2)
    return x0, x1


def find_panel_tables(pw: PageWords, page: int, *, limit_x: float = 1.0) -> list[Table]:
    """Panel tables: a title, then the first header band below it with CKT
    and description or trip columns, split per title on a shared band."""
    titles = _panel_titles(pw, limit_x=limit_x)
    if not titles:
        return []
    bands = _header_bands(pw, PANEL_HEADER_ROLES, limit_x=limit_x)
    tables: list[Table] = []
    end_lines = [ln for ln in pw.lines if END_RE.search(ln.t.strip())]
    for idx, (title, name, existing) in enumerate(titles):
        chosen: tuple[list[HeaderCell], float, float] | None = None
        for band in bands:
            if not (0 < min(w.cy for w in band) - title.cy < TITLE_REACH_Y):
                continue
            if not any(abs(w.cx - title.cx) < 0.3 for w in band):
                continue
            band_x0 = min(w.b[0] for w in band) - 0.004
            band_x1 = max(w.b[2] for w in band) + 0.004
            same_band = [
                (t, i)
                for i, t in enumerate(titles)
                if abs(t[0].cy - title.cy) < 0.03 and band_x0 <= t[0].cx <= band_x1
            ]
            if len(same_band) > 1:
                sub = [t for t, _i in same_band]
                local = next(k for k, (_t, i) in enumerate(same_band) if i == idx)
                x0, x1 = _split_title_span(sub, local, band_x0, band_x1)
            else:
                x0, x1 = band_x0, band_x1
            mine = [w for w in band if x0 <= w.cx <= x1]
            cells = _stack_cells(mine, pw, PANEL_HEADER_ROLES)
            _assign_roles(cells, PANEL_HEADER_ROLES)
            cells = [c for c in cells if c.role]
            roles = {c.role for c in cells}
            if "ckt" in roles and roles & {"desc", "trip"}:
                chosen = (cells, x0, x1)
                break
        if chosen is None:
            continue
        cells, x0, x1 = chosen
        x0 = min(x0, min(c.x0 for c in cells) - 0.004)
        x1 = max(x1, max(c.x1 for c in cells) + 0.004)
        table = Table(
            kind="panel",
            page=page,
            cells=cells,
            x0=x0,
            x1=x1,
            header_y0=min(c.y0 for c in cells),
            header_y1=max(c.y1 for c in cells),
            edges=_edges(cells, x0, x1),
            title=plain_text(title.t),
            title_line=title,
        )
        next_title_y = min(
            (t[0].cy for t in titles if t[0].cy > table.header_y1 and x0 <= t[0].cx <= x1),
            default=1.0,
        )
        table.rows = _body_rows(pw, table, end_lines=end_lines, hard_end_y=next_title_y - 0.005)
        tables.append(table)
    return tables


_INT_RE = re.compile(r"^\d{1,3}$")
_TRIP_RE = re.compile(r"^(?P<amps>\d{2,4})\s*A?(?:\s*/\s*(?P<poles>[123])\s*P?)?$", re.I)
_TRIP_POLES_FIRST_RE = re.compile(r"^(?P<poles>[123])\s*P\s*[-/ ]?\s*(?P<amps>\d{2,4})\s*A?$", re.I)
_FLOAT_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_WIRE_RE = re.compile(r"^#?(\d{1,3}|\d/0)(?:\s*(?:AWG|KCMIL|MCM))?$", re.I)


def _side_cells(table: Table) -> tuple[set[int], set[int], dict[int, float]]:
    """(left cell indices, right cell indices, shared cell index -> center)
    for a two-sided panel; a one-sided table is all left."""
    ckts = [i for i, c in enumerate(table.cells) if c.role == "ckt"]
    if len(ckts) < 2:
        return set(range(len(table.cells))), set(), {}
    loads = [i for i, c in enumerate(table.cells) if c.role in ("load_a", "load_b", "load_c", "va")]
    if loads and min(loads) > ckts[0] and max(loads) < ckts[-1]:
        shared = {i: table.cells[i].cx for i in loads}
        left = {i for i in range(len(table.cells)) if i < min(loads)}
        right = {i for i in range(len(table.cells)) if i > max(loads)}
        return left, right, shared
    mid = (table.cells[ckts[0]].cx + table.cells[ckts[-1]].cx) / 2
    left = {i for i, c in enumerate(table.cells) if c.cx <= mid}
    right = {i for i, c in enumerate(table.cells) if c.cx > mid}
    return left, right, {}


def _parse_trip(tokens: list[str]) -> tuple[int | None, int | None, bool]:
    """(trip_a, poles, gf) from the trip cell tokens."""
    text = plain_text(" ".join(tokens)).upper()
    gf = bool(re.search(r"\bGF(?:C)?I?\b", text))
    text = re.sub(r"\bGF(?:C)?I?\b", "", text).strip()
    if not text:
        return None, None, gf
    m = _TRIP_RE.match(text) or _TRIP_POLES_FIRST_RE.match(text)
    if not m:
        return None, None, gf
    amps = int(m.group("amps"))
    poles = int(m.group("poles")) if m.groupdict().get("poles") else None
    return amps, poles, gf


def _parse_wire(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    text = plain_text(" ".join(tokens)).upper()
    m = _WIRE_RE.match(text)
    return m.group(1) if m else text


def _parse_load(tokens: list[str]) -> float | None:
    for t in tokens:
        if _FLOAT_RE.match(t):
            return float(t)
    return None


def _ckt_and_keys(tokens: list[tuple[Word, HeaderCell]]) -> tuple[int | None, list[str]]:
    """The circuit number is the digit token nearest its header center;
    other 1 to 2 digit tokens in the CKT or KEY band are keynotes."""
    ckt: int | None = None
    keys: list[str] = []
    ckt_tokens = [(w, c) for w, c in tokens if c.role == "ckt" and _INT_RE.match(w.t)]
    if ckt_tokens:
        best = min(ckt_tokens, key=lambda wc: abs(wc[0].cx - wc[1].cx))
        ckt = int(best[0].t)
        for w, _c in ckt_tokens:
            if w is not best[0] and re.fullmatch(r"\d{1,2}", w.t):
                keys.append(w.t)
    for w, c in tokens:
        if c.role == "key" and re.fullmatch(r"\d{1,2}", w.t):
            keys.append(w.t)
    return ckt, keys


def _status_for(
    desc: str,
    keynote_text: list[str],
    *,
    demo_loads: set[str] | None,
) -> tuple[str, str | None]:
    """(status, status_basis) by positive evidence on the row itself: the
    description, a linked keynote or a demolition-plan load match. Nothing
    about the panel decides a status."""
    d = desc.strip()
    du = d.upper()
    if re.search(r"\bSPARE\b", du):
        return "spare", "description SPARE"
    if re.search(r"\bSPACE\b|\bPROVISION", du):
        return "space", "description SPACE"
    for text in keynote_text:
        if NEW_BREAKER_NOTE_RE.search(text):
            return "new", f"keynote: {text}"
    if re.search(r"\bNEW\b", du):
        return "new", "description NEW"
    if re.search(r"RELOCAT", du):
        return "relocated", "description RELOCATED"
    if EXISTING_MARK_RE.search(du):
        if EXISTING_LOAD_ONLY_RE.match(du):
            return "existing", "row marked EXISTING LOAD"
        return "existing_reused", "(E) or EXISTING marker on a named load"
    if demo_loads and d:
        norm = re.sub(r"[^A-Z0-9]+", " ", du).strip()
        if norm in demo_loads:
            return "existing_reused", "description matches a demolition-plan load"
    # No marker either way: unknown. The panel's own (E) state is a signal
    # (`panel_not_marked_existing`), never a status.
    return "unknown", None


def _bold(words: list[Word]) -> bool:
    return any(w.f & F_BOLD for w in words)


def _revision_delta_words(
    table: Table, pw: PageWords, revision: str | None
) -> list[tuple[float, float]]:
    """(cx, cy) of every word equal to the sheet's revision number within
    `REVISION_DELTA_REACH_IN` outside the table's horizontal extent (inside
    the table a lone digit is a keynote key or a circuit number). Computed
    once per table; rows then test their band against this short list."""
    if not revision:
        return []
    rev = revision.strip().upper()
    reach = REVISION_DELTA_REACH_IN / (pw.page_w_in or 42.0)
    out: list[tuple[float, float]] = []
    for w in pw.words:
        if table.x0 <= w.cx <= table.x1:
            continue
        if not (table.x0 - reach <= w.cx <= table.x1 + reach):
            continue
        if w.cy < table.header_y1 or w.cy > table.end_y + 0.01:
            continue
        m = REVISION_DELTA_RE.match(w.t.strip().upper())
        if m and m.group("n") == rev:
            out.append((w.cx, w.cy))
    return out


def _revision_delta_near(
    deltas: list[tuple[float, float]],
    table: Table,
    row_bbox: Bbox,
    *,
    side: str | None = None,
) -> bool:
    """A revision-number word (`_revision_delta_words`) on the row's band.
    On a two-sided schedule `side` is "L" or "R": a delta left of the table
    belongs to the left circuit of the band, one on the right to the right
    circuit; None (one-sided table) accepts either side."""
    if not deltas:
        return False
    y0, y1 = row_bbox[1], row_bbox[3]
    pad = 0.5 * max(y1 - y0, 0.002)
    for cx, cy in deltas:
        if side == "L" and cx > table.x1:
            continue
        if side == "R" and cx < table.x0:
            continue
        if y0 - pad <= cy <= y1 + pad:
            return True
    return False


def parse_panel(
    table: Table,
    pw: PageWords,
    *,
    keynotes: dict[str, str] | None = None,
    demo_loads: set[str] | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """`Schedule` dict (kind panel) with the `PanelSchedule` payload.
    `revision` is the sheet's latest revision number (title block), used
    only for the `revision_delta` signal."""
    keynotes = keynotes or {}
    assert table.title_line is not None
    m = PANEL_TITLE_RE.match(plain_text(table.title_line.t))
    name = m.group("name").upper() if m else (table.title or "?")
    pre = (m.group("pre") or "").upper() if m else ""
    post = (m.group("post") or "").upper() if m else ""
    existing = "(E)" in pre or "EXISTING" in pre or post in ("E", "EXISTING")
    header = _panel_header_fields(pw, table)
    if header.pop("_existing_title", False):
        existing = True
    left, right, shared = _side_cells(table)
    deltas = _revision_delta_words(table, pw, revision)
    circuits: list[dict[str, Any]] = []
    unparsed: list[dict[str, Any]] = []
    rows_total = 0
    rows_valid = 0
    last_breaker: dict[str, dict[str, Any]] = {}

    for row in _merge_split_rows(table, pw, shared, right):
        placed: dict[str, list[tuple[Word, HeaderCell]]] = {"L": [], "R": []}
        for w, cell in _place_words(row, table, pw):
            idx = table.cells.index(cell)
            if idx in shared:
                side = "L" if w.cx <= shared[idx] else "R"
            elif idx in right:
                side = "R"
            else:
                side = "L"
            placed[side].append((w, cell))
        for side in ("L", "R"):
            toks = placed[side]
            if not toks:
                continue
            ckt, keys = _ckt_and_keys(toks)
            by_role: dict[str, list[str]] = {}
            words_by_role: dict[str, list[Word]] = {}
            for w, c in toks:
                by_role.setdefault(c.role, []).append(w.t)
                words_by_role.setdefault(c.role, []).append(w)
            desc = plain_text(" ".join(by_role.get("desc", [])))
            raw_cells = [plain_text(" ".join(by_role.get(c.role, []))) for c in table.cells]
            if ckt is None:
                if desc or by_role.get("trip"):
                    unparsed.append(
                        {
                            "y": round(sum(w.cy for w, _c in toks) / len(toks), 4),
                            "raw": raw_cells,
                            "reason": "no circuit number",
                        }
                    )
                continue
            rows_total += 1
            trip, poles_from_trip, gf = _parse_trip(by_role.get("trip", []))
            poles_tokens = by_role.get("poles", [])
            poles = (
                int(poles_tokens[0])
                if poles_tokens and _INT_RE.match(poles_tokens[0])
                else poles_from_trip
            )
            wire = {
                "hot": _parse_wire(by_role.get("wire_h", [])),
                "neutral": _parse_wire(by_role.get("wire_n", [])),
                "ground": _parse_wire(by_role.get("wire_g", [])),
            }
            if not any(wire.values()) and by_role.get("wire"):
                wire["hot"] = _parse_wire(by_role["wire"])
            vd_tokens = [t for t in by_role.get("vd", []) if _FLOAT_RE.match(t)]
            vd = float(vd_tokens[0]) if vd_tokens else None
            load_va = {
                "A": _parse_load(by_role.get("load_a", [])),
                "B": _parse_load(by_role.get("load_b", [])),
                "C": _parse_load(by_role.get("load_c", [])),
            }
            row_words = [w for w, _c in toks]
            bbox = union_box([w.b for w in row_words])
            # Multi-pole continuation: no trip, no poles, no description.
            prev = last_breaker.get(side)
            if trip is None and not poles_tokens and not desc and prev is not None:
                if (prev.get("poles") or 1) > len(prev["ckts"]):
                    prev["ckts"].append(ckt)
                    prev["bbox"] = union_box([prev["bbox"], bbox])
                    rows_valid += 1
                    continue
            problems: list[str] = []
            if trip is not None and trip not in TRIP_VALUES:
                problems.append(f"trip {trip} A not a standard value")
            if poles is not None and poles not in (1, 2, 3):
                problems.append(f"poles {poles}")
            for k, v in wire.items():
                if v is not None and v not in WIRE_SIZES:
                    problems.append(f"wire {k} {v}")
            keynote_text = [keynotes[k] for k in keys if k in keynotes]
            signals = {
                "bold": _bold(row_words),
                "keynote": bool(keys),
                "n_tag": bool(N_TAG_RE.search(desc)),
                "wire_filled": any(wire.values()),
                "vd_filled": vd is not None,
                "panel_not_marked_existing": not existing,
                "revision_delta": _revision_delta_near(
                    deltas, table, bbox, side=side if right else None
                ),
            }
            status, basis = _status_for(desc, keynote_text, demo_loads=demo_loads)
            gfci = gf or bool(GFCI_RE.search(desc)) or any(GFCI_RE.search(t) for t in keynote_text)
            circuit = {
                "ckt": ckt,
                "ckts": [ckt],
                "side": side,
                "poles": poles,
                "trip_a": trip,
                "desc": desc,
                "status": status,
                "status_basis": basis,
                "signals": {k: v for k, v in signals.items() if v} if status == "unknown" else {},
                "keynotes": keys,
                "load_va": load_va,
                "wire": wire,
                "conduit": None,
                "vd_pct": vd,
                "gfci": gfci,
                "bbox": bbox,
                "raw": raw_cells,
                "problems": problems,
            }
            if problems:
                unparsed.append(
                    {"y": round(bbox[1], 4), "raw": raw_cells, "reason": "; ".join(problems)}
                )
            else:
                rows_valid += 1
            circuits.append(circuit)
            if status not in ("space",):
                last_breaker[side] = circuit
    circuits.sort(key=lambda c: c["ckt"])
    validation = _validate(circuits, rows_total, rows_valid)
    panel = {
        "name": name,
        "existing": existing,
        "status_basis": "(E) prefix"
        if "(E)" in pre or post == "E"
        else ("EXISTING in title" if existing else None),
        **header,
        "circuits": circuits,
        "validation": validation,
    }
    confidence = rows_valid / rows_total if rows_total else 0.0
    return {
        "kind": "panel",
        "page": table.page,
        "bbox": table.bbox(),
        "title": table.title,
        "parse_confidence": round(confidence, 4),
        "columns": [c.label for c in table.cells],
        "unparsed_rows": unparsed,
        "panel": panel,
        "luminaires": None,
        "equipment": None,
        "feeders": None,
        "rows_raw": [[plain_text(w.t) for w in r] for r in table.rows],
        "source": "text_layer",
    }


def _merge_split_rows(
    table: Table, pw: PageWords, shared: dict[int, float], right: set[int]
) -> list[list[Word]]:
    """A multi-pole breaker prints its description and trip on a baseline
    between its circuit numbers; such a content-only row merges into the
    circuit-number row just above it (per side)."""
    if len(table.rows) < 2:
        return list(table.rows)

    def side_of(w: Word) -> str:
        idx = table.column_of(w.cx)
        if idx in shared:
            return "L" if w.cx <= shared[idx] else "R"
        return "R" if idx in right else "L"

    def has_ckt(words: list[Word]) -> bool:
        return any(table.cell(w.cx).role == "ckt" and _INT_RE.match(w.t) for w in words)

    def has_content(words: list[Word]) -> bool:
        return any(table.cell(w.cx).role in ("desc", "trip", "poles") for w in words)

    out: list[list[Word]] = [list(r) for r in table.rows]
    for i in range(1, len(out)):
        for side in ("L", "R"):
            mine = [w for w in out[i] if side_of(w) == side]
            if not mine or has_ckt(mine) or not has_content(mine):
                continue
            above = [w for w in out[i - 1] if side_of(w) == side]
            if above and has_ckt(above) and not has_content(above):
                out[i - 1].extend(mine)
                out[i] = [w for w in out[i] if w not in mine]
    return [r for r in out if r]


def _validate(circuits: list[dict[str, Any]], rows_total: int, rows_valid: int) -> dict[str, Any]:
    odd_even_ok = (
        all((c["ckt"] % 2 == 1) == (c["side"] == "L") for c in circuits if c["side"] in ("L", "R"))
        if any(c["side"] == "R" for c in circuits)
        else True
    )
    seen = sorted({k for c in circuits for k in c["ckts"]})
    continuity_ok = bool(seen) and seen == list(range(1, len(seen) + 1))
    poles_ok = all((c.get("poles") or 1) >= len(c["ckts"]) for c in circuits)
    trip_ok = all(c["trip_a"] is None or c["trip_a"] in TRIP_VALUES for c in circuits)
    return {
        "odd_even_ok": odd_even_ok,
        "continuity_ok": continuity_ok,
        "poles_ok": poles_ok,
        "trip_values_ok": trip_ok,
        "rows_total": rows_total,
        "rows_valid": rows_valid,
    }


_HEADER_FIELD_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("volts", re.compile(r"VOLTS?\s*:?\s*([\d/]+)", re.I)),
    ("phase", re.compile(r"PHASE\s*:?\s*(\d)", re.I)),
    ("wires", re.compile(r"WIRES?\s*:?\s*(\d)\b", re.I)),
    (
        "main",
        re.compile(
            r"MAIN\s*:?\s*((?:\d+\s*A?\s*)?(?:MLO|MCB|M\.?L\.?O\.?|M\.?C\.?B\.?)|\d+\s*A)", re.I
        ),
    ),
    ("bus_amps", re.compile(r"BUS\s*(?:AMPS|RATING)?\s*:?\s*(\d+)", re.I)),
    ("aic_ka", re.compile(r"AIC\s*:?\s*(\d+(?:\.\d+)?)\s*k?A", re.I)),
    ("sccr_ka", re.compile(r"SCCR\s*:?\s*(\d+(?:\.\d+)?)\s*k?A", re.I)),
    ("mounting", re.compile(r"MOUNTING\s*:?\s*([A-Z]+)", re.I)),
    ("fed_from", re.compile(r"FED\s+FROM\s*:?\s*(.+?)(?=\s*\||\s+[A-Z]+\s*:|$)", re.I)),
    ("location", re.compile(r"LOCATION\s*:?\s*(.+?)(?=\s*\||\s+[A-Z]+\s*:|$)", re.I)),
    ("enclosure", re.compile(r"ENCLOSURE\s*:?\s*(.+?)(?=\s*\||\s+[A-Z]+\s*:|$)", re.I)),
)


def _panel_header_fields(pw: PageWords, table: Table) -> dict[str, Any]:
    """KEY: VALUE fields between the title and the column header."""
    assert table.title_line is not None
    out: dict[str, Any] = {k: None for k, _rx in _HEADER_FIELD_RES}
    zone = sorted(
        (
            ln
            for ln in pw.lines
            if table.title_line.cy < ln.cy < table.header_y0
            and table.x0 - 0.01 <= ln.cx <= table.x1 + 0.01
        ),
        key=lambda ln: (ln.cy, ln.b[0]),
    )
    # Lines on one baseline join (MAIN: and 400 A MLO are separate lines).
    rows_text: list[str] = []
    cur: list[Line] = []
    for ln in zone:
        if cur and abs(ln.cy - cur[-1].cy) <= 0.6 * _h_frac(max(ln.h, cur[-1].h), pw):
            cur.append(ln)
        else:
            if cur:
                rows_text.append(
                    plain_text(" | ".join(x.t for x in sorted(cur, key=lambda x: x.b[0])))
                )
            cur = [ln]
    if cur:
        rows_text.append(plain_text(" | ".join(x.t for x in sorted(cur, key=lambda x: x.b[0]))))
    for joined in rows_text:
        flat = joined.replace(" | ", " ")
        for key, rx in _HEADER_FIELD_RES:
            text = joined if key in ("fed_from", "location", "enclosure") else flat
            m = rx.search(text)
            if m and out.get(key) is None:
                val: Any = m.group(1).strip()
                if key in ("phase", "wires", "bus_amps"):
                    val = int(val)
                elif key in ("aic_ka", "sccr_ka"):
                    val = float(val)
                elif key == "main":
                    mm = re.match(r"(\d+)?\s*A?\s*(MLO|MCB)?", val, re.I)
                    val = {
                        "type": (mm.group(2) or "").upper().replace(".", "") or None
                        if mm
                        else None,
                        "amps": int(mm.group(1)) if mm and mm.group(1) else None,
                    }
                if isinstance(val, str):
                    val = val.strip(" |")
                out[key] = val if val not in ("", {}) else None
    out.setdefault("_existing_title", False)
    return out


# Luminaire schedules --------------------------------------------------------------


def find_luminaire_tables(pw: PageWords, page: int, *, limit_x: float = 1.0) -> list[Table]:
    bands = _header_bands(pw, LUMINAIRE_HEADER_ROLES, limit_x=limit_x)
    med = median_line_height(pw.lines)
    headings = [ln for ln in pw.lines if med > 0 and ln.h >= 1.5 * med]
    tables: list[Table] = []
    for band in bands:
        cells = _stack_cells(band, pw, LUMINAIRE_HEADER_ROLES)
        for c in cells:
            c.role = LUMINAIRE_HEADER_ROLES.get(c.label, "")
            if not c.role and "MANUFACTURER" in c.label:
                c.role = "manufacturer"
            if not c.role and "LUMENS" in c.label:
                c.role = "lumens"
            if not c.role and "CONTROL" in c.label:
                c.role = "control"
        cells = [c for c in cells if c.role]
        roles = {c.role for c in cells}
        if not ({"tag", "description"} <= roles) or len(roles) < 3:
            continue
        if not (
            roles & {"manufacturer", "watts", "volts", "lumens", "mounting", "control", "lamp"}
        ):
            continue
        x0 = min(c.x0 for c in cells) - 0.004
        x1 = max(c.x1 for c in cells) + 0.004
        table = Table(
            kind="luminaire",
            page=page,
            cells=cells,
            x0=x0,
            x1=x1,
            header_y0=min(c.y0 for c in cells),
            header_y1=max(c.y1 for c in cells),
            edges=_edges(cells, x0, x1),
        )
        title = max(
            (
                ln
                for ln in headings
                if ln.cy < table.header_y0
                and table.header_y0 - ln.cy < 0.06
                and x0 - 0.05 <= ln.cx <= x1 + 0.05
            ),
            key=lambda ln: ln.cy,
            default=None,
        )
        table.title = plain_text(title.t) if title else None
        end_y = min(
            (
                ln.b[1] - 0.001
                for ln in headings
                if ln.cy > table.header_y1 and x0 - 0.05 <= ln.cx <= x1 + 0.05
            ),
            default=1.0,
        )
        table.rows = _body_rows(pw, table, end_lines=[], hard_end_y=end_y)
        if table.rows:
            tables.append(table)
    return tables


def _cell_text(tokens: list[str]) -> str:
    toks = [t for t in tokens if t.strip().upper() != "N/A"] or tokens
    return plain_text(" ".join(toks))


def _lumens(text: str) -> int | None:
    m = re.search(r"(\d[\d,]*)\s*(?:lm|LM|LUMENS)?", text)
    if not m or not text:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_luminaires(table: Table, pw: PageWords) -> dict[str, Any]:
    """`Schedule` dict (kind luminaire) with one `LuminaireType` per tag row;
    following rows without a tag merge cell-wise into the row above."""
    rows: list[dict[str, list[str]]] = []
    row_boxes: list[Bbox] = []
    pitches: list[float] = []
    prev_cy: float | None = None
    for row in table.rows:
        cy = sum(w.cy for w in row) / len(row)
        if prev_cy is not None:
            pitches.append(cy - prev_cy)
        prev_cy = cy
    pitch = float(median(pitches)) if pitches else 0.01
    last_bottom: float | None = None
    for row in table.rows:
        by_role: dict[str, list[str]] = {}
        for w, cell in _place_words(row, table, pw):
            by_role.setdefault(cell.role, []).append(w.t)
        tag_tokens = by_role.get("tag", [])
        if len(tag_tokens) > 1:
            # Left-aligned description text can start under the TAG header's
            # right edge: only the first token is the tag.
            by_role["description"] = tag_tokens[1:] + by_role.get("description", [])
            by_role["tag"] = tag_tokens[:1]
            tag_tokens = tag_tokens[:1]
        tag = plain_text(" ".join(tag_tokens)).upper()
        top = min(w.b[1] for w in row)
        starts = bool(tag) and bool(LUMINAIRE_TAG_RE.match(tag))
        if starts or not rows or last_bottom is None or top - last_bottom > 1.6 * pitch:
            if not starts:
                last_bottom = max(w.b[3] for w in row)
                continue  # a non-tag row with no row above to merge into
            rows.append(by_role)
            row_boxes.append(union_box([w.b for w in row]))
        else:
            for role, toks in by_role.items():
                rows[-1].setdefault(role, []).extend(toks)
            row_boxes[-1] = union_box([row_boxes[-1], union_box([w.b for w in row])])
        last_bottom = max(w.b[3] for w in row)
    luminaires: list[dict[str, Any]] = []
    for by_role, box in zip(rows, row_boxes, strict=True):
        tag = plain_text(" ".join(by_role.get("tag", []))).upper()
        description = _cell_text(by_role.get("description", []))
        watts_text = _cell_text(by_role.get("watts", []))
        watts_m = re.search(r"(\d+(?:\.\d+)?)", watts_text)
        control = _cell_text(by_role.get("control", []))
        mounting = _cell_text(by_role.get("mounting", []))
        manufacturer_text = _cell_text(by_role.get("manufacturer", []))
        manufacturer, catalog = _split_manufacturer(manufacturer_text)
        emergency = bool(tag.endswith("-EM")) or bool(
            EMERGENCY_HINT_RE.search(f"{description} {control}")
        )
        watts_per = "ft" if WATTS_PER_FT_RE.search(watts_text) else "fixture"
        linear = (
            bool(LINEAR_HINT_RE.search(description))
            or watts_per == "ft"
            or mounting.upper() == "UC"
        )
        luminaires.append(
            {
                "tag": tag,
                "description": description,
                "mounting": mounting or None,
                "lamp": _cell_text(by_role.get("lamp", [])) or None,
                "lumens": _lumens(_cell_text(by_role.get("lumens", []))),
                "watts": float(watts_m.group(1)) if watts_m else None,
                "watts_per": watts_per,
                "volts": _cell_text(by_role.get("volts", [])) or None,
                "control": control or None,
                "manufacturer": manufacturer,
                "catalog": catalog,
                "linear_hint": linear,
                "emergency_hint": emergency,
                "notes": _cell_text(by_role.get("notes", [])) or None,
                "bbox": box,
            }
        )
    confidence = (
        sum(1 for lum in luminaires if lum["description"]) / len(luminaires) if luminaires else 0.0
    )
    return {
        "kind": "luminaire",
        "page": table.page,
        "bbox": table.bbox(),
        "title": table.title,
        "parse_confidence": round(confidence, 4),
        "columns": [c.label for c in table.cells],
        "unparsed_rows": [],
        "panel": None,
        "luminaires": luminaires,
        "equipment": None,
        "feeders": None,
        "rows_raw": [[plain_text(w.t) for w in r] for r in table.rows],
        "source": "text_layer",
    }


def _split_manufacturer(text: str) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    if " - " in text:
        maker, cat = text.split(" - ", 1)
        return plain_text(maker) or None, plain_text(cat) or None
    m = re.match(r"^(.*?)\s*(#.*)$", text)
    if m and m.group(1):
        return plain_text(m.group(1)), plain_text(m.group(2))
    return text, None


# Page entry point ---------------------------------------------------------------------


def parse_page(
    pw: PageWords,
    page: int,
    *,
    keynotes: dict[str, str] | None = None,
    limit_x: float = 1.0,
    demo_loads: set[str] | None = None,
    revision: str | None = None,
) -> list[dict[str, Any]]:
    """Every panel and luminaire schedule on one page (no ids yet; the
    document assigns `schedule_id` in page order). `revision` is the sheet's
    revision number for the `revision_delta` signal."""
    out: list[dict[str, Any]] = []
    for table in find_panel_tables(pw, page, limit_x=limit_x):
        out.append(
            parse_panel(table, pw, keynotes=keynotes, demo_loads=demo_loads, revision=revision)
        )
    for table in find_luminaire_tables(pw, page, limit_x=limit_x):
        out.append(parse_luminaires(table, pw))
    out.sort(key=lambda s: (s["bbox"][1], s["bbox"][0]))
    return out


def assign_ids(schedules: list[dict[str, Any]], start: int = 1) -> int:
    n = start
    for s in schedules:
        s["schedule_id"] = f"s{n}"
        n += 1
    return n


def panels_summary(schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`DocFacts.panels`: one row per parsed panel schedule."""
    out: list[dict[str, Any]] = []
    for s in schedules:
        if s.get("kind") != "panel" or not s.get("panel"):
            continue
        p = s["panel"]
        circuits = p.get("circuits") or []
        out.append(
            {
                "name": p["name"],
                "schedule_id": s.get("schedule_id"),
                "existing": p.get("existing"),
                "page": s["page"],
                "volts": p.get("volts"),
                "phase": p.get("phase"),
                "main": p.get("main"),
                "circuits": len(circuits),
                "new_circuits": [c["ckt"] for c in circuits if c["status"] == "new"],
                "unknown_circuits": sum(1 for c in circuits if c["status"] == "unknown"),
                "parse_confidence": s.get("parse_confidence"),
            }
        )
    return out


def fixture_types_summary(schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`DocFacts.fixture_types`: one row per luminaire tag."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in schedules:
        if s.get("kind") != "luminaire":
            continue
        for lum in s.get("luminaires") or []:
            if lum["tag"] in seen:
                continue
            seen.add(lum["tag"])
            out.append(
                {
                    "tag": lum["tag"],
                    "schedule_id": s.get("schedule_id"),
                    "page": s["page"],
                    "description": lum.get("description"),
                    "control": lum.get("control"),
                    "volts": lum.get("volts"),
                    "mounting": lum.get("mounting"),
                    "linear_hint": lum.get("linear_hint", False),
                    "emergency_hint": lum.get("emergency_hint", False),
                }
            )
    return out


def panel_names(schedules: list[dict[str, Any]]) -> set[str]:
    return {s["panel"]["name"] for s in schedules if s.get("kind") == "panel" and s.get("panel")}


def luminaire_tags(schedules: list[dict[str, Any]]) -> set[str]:
    return {
        lum["tag"]
        for s in schedules
        if s.get("kind") == "luminaire"
        for lum in (s.get("luminaires") or [])
    }


def confident_count(schedules: list[dict[str, Any]], threshold: float = 0.8) -> int:
    return sum(1 for s in schedules if (s.get("parse_confidence") or 0) >= threshold)


def lookup(
    schedules: list[dict[str, Any]],
    *,
    kind: str | None = None,
    name: str | None = None,
    tag: str | None = None,
    circuit: int | None = None,
) -> list[dict[str, Any]]:
    """`schedule_lookup` semantics over a document's schedules."""
    out: list[dict[str, Any]] = []
    for s in schedules:
        if kind and kind != "any" and s.get("kind") != kind:
            continue
        if name and (not s.get("panel") or s["panel"]["name"].upper() != name.upper()):
            continue
        if tag and not any(
            lum["tag"].upper() == tag.upper() for lum in (s.get("luminaires") or [])
        ):
            continue
        if circuit is not None and (
            not s.get("panel") or not any(circuit in c["ckts"] for c in s["panel"]["circuits"])
        ):
            continue
        out.append(s)
    return out
