"""Plan-area tags (WS1 step 6): circuit tags, status tags, luminaire type
marks, panel labels and tag clusters.

Circuit grammar (one word after sanitization), built per document from the
panel name set P: `^(?:(?P<mods>(?:[A-Z0-9]{1,4}/)+))?(?P<panel>P1|P2|...)
(?:[-:](?P<ckts>\\d{1,3}(?:[,&]\\d{1,3})*))?(?P<tail>/[A-Z]{1,3})?$`. The
circuit number is optional: IMEG splits "1EELC - 1" into three words, so a
tag without a number attaches the nearest unused number word within 0.35
sheet inches (same rotation, below or right preferred) and records
`attached_number: true`. One-letter panel names enter P only when a parsed
schedule declares them (a bare "A" would otherwise match every subscript).
A sheet note such as "ALL CIRCUITS TO PANEL LA UON" sets the page's
`default_panel`, which fills tags that carry mods and a number but no panel.

Only words in the plan area count (`regions.in_plan_area`), and a panel
name right after PANEL or PNL on its line is a panel label, not a tag. A
label name that carries a digit (1EELC, H1, EM2) is taken as written; a
letter-only name (LA, ILL) counts only when it is quoted, prefixed by
(E)/(N)/(R) or ends its row, and never when it is a stop word (AND, LOAD,
TYPE, ...), so prose such as "MARKED WITH PANEL AND CIRCUIT NUMBERS" or
"PANELBOARD LOAD SUMMARIES" adds nothing to the document grammar.
Counts are floors: engineers tag one device per group with TYP, so "none
found" means unknown, never 0.

Pure functions over `PageWords`; no model, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.services.pagefacts.contracts import Bbox, PageWords, Region, Word, round_box
from app.services.pagefacts.notes import baseline_rows, plain_text, row_text
from app.services.pagefacts.regions import in_plan_area

CIRCUIT_NUMBER_RE = re.compile(r"^-?(?P<n>\d{1,3}(?:[,&]\d{1,3})*)$")
STATUS_TAG_RE = re.compile(r"^\((X|E|R|N|ER|RL|D)\)$")
STATUS_SUFFIX_RE = re.compile(r"-(X|E|R|N)$")
PANEL_LABEL_RE = re.compile(
    r"(?:\((?P<pre>E|N|R)\)\s*)?(?:PANEL(?:BOARD)?|PNL)\s*:?\s*(?P<q1>['\"]?)(?P<name>[A-Z0-9][A-Z0-9\-]{1,7})(?P<q2>['\"]?)(?![A-Z0-9\-])",
)
# A label name is panel-like: it carries a digit or is short (LA, ILL, H1, EM2).
NAME_OK_RE = re.compile(r"^(?=.*\d)[A-Z0-9\-]{2,8}$|^[A-Z]{2,4}$")
LETTER_NAME_RE = re.compile(r"^[A-Z]+$")
# Prose words that follow PANEL without naming one.
LABEL_STOP_WORDS: frozenset[str] = frozenset(
    {
        "AND",
        "OR",
        "LOAD",
        "LOADS",
        "TYPE",
        "THE",
        "FOR",
        "WITH",
        "TO",
        "IS",
        "ARE",
        "PER",
        "ALL",
        "NEW",
        "NO",
        "SHALL",
        "SCHEDULE",
        "SCHEDULES",
        "BOARD",
        "BOARDS",
    }
)
ROW_END_PUNCT = " .:;,)"
PANEL_WORDS: frozenset[str] = frozenset({"PANEL", "PANELBOARD", "PNL"})
DEFAULT_PANEL_RE = re.compile(
    r"ALL\s+(?:\w+\s+){0,2}CIRCUITS?\s+(?:ARE\s+|SHALL\s+BE\s+|TO\s+BE\s+)?(?:TO|FROM|FED\s+FROM|IN|ON|AT)\s+"
    r"(?:\(?E\)?\s*)?(?:PANEL(?:BOARD)?|PNL)\s*['\"]?(?P<name>[A-Z0-9][A-Z0-9\-]{0,7})['\"]?",
    re.I,
)
ATTACH_RADIUS_IN = 0.35
CLUSTER_EPS_IN = 6.0
DEFAULT_PAD_IN = 1.0
MAX_REGIONS = 4
MIN_LABEL_NAME = 2


def circuit_grammar(panel_names: Iterable[str]) -> re.Pattern[str] | None:
    names = sorted({n.upper() for n in panel_names if n}, key=lambda n: (-len(n), n))
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(
        rf"^(?:(?P<mods>(?:[A-Z0-9]{{1,4}}/)+))?(?P<panel>{alt})"
        r"(?:\s*[-:]\s*(?P<ckts>\d{1,3}(?:[,&]\d{1,3})*))?(?P<tail>/[A-Z]{1,3})?$"
    )


NO_PANEL_RE = re.compile(
    r"^(?P<mods>(?:[A-Z0-9]{1,4}/)+)(?P<ckts>\d{1,3}(?:[,&]\d{1,3})*)$|^(?:CKT|CIRCUIT|C)[-#]?(?P<ckts2>\d{1,3})$"
)


def _circuits(text: str | None) -> list[int]:
    if not text:
        return []
    return [int(t) for t in re.split(r"[,&]", text) if t.isdigit()]


def parse_tag_word(
    text: str, grammar: re.Pattern[str] | None, *, default_panel: str | None = None
) -> dict[str, Any] | None:
    """{panel, circuits, mods, tail} for one word or None."""
    t = plain_text(text).upper()
    if grammar is not None:
        m = grammar.match(t)
        if m:
            mods = [x for x in (m.group("mods") or "").split("/") if x]
            return {
                "panel": m.group("panel"),
                "circuits": _circuits(m.group("ckts")),
                "mods": mods,
                "tail": (m.group("tail") or "").lstrip("/") or None,
            }
    if default_panel:
        m = NO_PANEL_RE.match(t)
        if m:
            mods = [x for x in (m.group("mods") or "").split("/") if x]
            return {
                "panel": default_panel,
                "circuits": _circuits(m.group("ckts") or m.group("ckts2")),
                "mods": mods,
                "tail": None,
                "default_panel": True,
            }
    return None


def default_panel_from_notes(notes: list[dict[str, Any]]) -> str | None:
    for block in notes:
        for item in block.get("items", []):
            m = DEFAULT_PANEL_RE.search(item.get("text", ""))
            if m:
                return m.group("name").upper()
        for text in block.get("unnumbered", []):
            m = DEFAULT_PANEL_RE.search(text)
            if m:
                return m.group("name").upper()
    return None


def label_name_admitted(m: re.Match[str], text: str) -> bool:
    """A letter-only name after PANEL is a label when quoted, prefixed by a
    status marker, or the last word of its row (trailing punctuation aside)."""
    if m.group("q1") or m.group("q2") or m.group("pre"):
        return True
    return text[m.end() :].strip(ROW_END_PUNCT) == ""


def panel_labels_on_page(
    pw: PageWords, regions: list[Region] | None = None, *, plan_area_only: bool = False
) -> list[dict[str, Any]]:
    """PANEL 'name' labels (names of 2 to 8 characters); the largest glyph
    per panel on the page wins. A letter-only name is admitted only when it
    is quoted, carries an (E)/(N)/(R) prefix or ends its row, and never when
    it is a stop word; digit-bearing names are taken as written."""
    best: dict[str, dict[str, Any]] = {}
    for row in baseline_rows(pw):
        text = row_text(row).upper()
        ln = row[0]
        for m in PANEL_LABEL_RE.finditer(text):
            name = m.group("name")
            if len(name) < MIN_LABEL_NAME or name in LABEL_STOP_WORDS:
                continue
            if not NAME_OK_RE.match(name):
                continue
            if LETTER_NAME_RE.match(name) and not label_name_admitted(m, text):
                continue
            if plan_area_only and regions is not None and not in_plan_area(regions, ln.cx, ln.cy):
                continue
            x0 = min(x.b[0] for x in row)
            x1 = max(x.b[2] for x in row)
            h = max(x.h for x in row)
            item = {
                "panel": name,
                "x": round((x0 + x1) / 2, 4),
                "y": round(ln.cy, 4),
                "raw": row_text(row),
                "existing": (m.group("pre") or "").upper() == "E",
                "h": h,
                "source": "plan_label",
            }
            cur = best.get(name)
            if cur is None or h > cur["h"]:
                best[name] = item
    return sorted(best.values(), key=lambda d: (d["y"], d["x"]))


def _word_lines(pw: PageWords) -> dict[int, int]:
    out: dict[int, int] = {}
    for li, ln in enumerate(pw.lines):
        for wi in range(ln.w[0], ln.w[1]):
            out[wi] = li
    return out


def _box_distance_in(a: Bbox, b: Bbox, w_in: float, h_in: float) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0) * w_in
    dy = max(a[1] - b[3], b[1] - a[3], 0.0) * h_in
    return (dx * dx + dy * dy) ** 0.5


def page_tags(
    pw: PageWords,
    regions: list[Region],
    *,
    panel_names: Iterable[str],
    luminaire_tags: Iterable[str] = (),
    subscripts: dict[str, str] | None = None,
    status_forms: Iterable[str] = (),
    notes: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], set[int]]:
    """`PageFacts.tags` for one page plus the indices of words consumed by
    circuit tags (tag words and attached numbers), so keynote markers never
    reuse them."""
    names = {n.upper() for n in panel_names if n}
    lum = {t.upper() for t in luminaire_tags if t}
    subs = subscripts or {}
    forms = {f.upper() for f in status_forms}
    grammar = circuit_grammar(names)
    default_panel = default_panel_from_notes(notes or [])
    w_in, h_in = pw.page_w_in or 42.0, pw.page_h_in or 30.0
    line_of = _word_lines(pw)
    consumed: set[int] = set()
    circuit: list[dict[str, Any]] = []
    status: dict[str, list[list[float]]] = {}
    type_marks: dict[str, list[list[float]]] = {}

    def is_panel_label_word(i: int, w: Word) -> bool:
        """The nearest word to the left on the same baseline is PANEL."""
        li = line_of.get(i)
        if li is not None and i > pw.lines[li].w[0]:
            prev = pw.words[i - 1].t.upper().rstrip(":")
            if prev in PANEL_WORDS or prev.rstrip("'\"") in PANEL_WORDS:
                return True
        h = max(w.h, 1.0) / (h_in * 72.0)
        best: tuple[float, Word] | None = None
        for other in pw.words:
            if other is w or other.b[2] > w.b[0] + 1e-6 or abs(other.cy - w.cy) > 0.6 * h:
                continue
            gap = (w.b[0] - other.b[2]) * w_in
            if gap <= 0.6 and (best is None or gap < best[0]):
                best = (gap, other)
        return best is not None and best[1].t.upper().rstrip(":'\"") in PANEL_WORDS

    plan_idx = [i for i, w in enumerate(pw.words) if in_plan_area(regions, w.cx, w.cy)]
    number_words = [
        i for i in plan_idx if pw.words[i].t != "-" and CIRCUIT_NUMBER_RE.match(pw.words[i].t)
    ]
    for i in plan_idx:
        w = pw.words[i]
        parsed = parse_tag_word(w.t, grammar, default_panel=default_panel)
        if parsed is not None and not is_panel_label_word(i, w):
            item = {
                "panel": parsed["panel"],
                "circuits": parsed["circuits"],
                "mods": parsed["mods"],
                "fixture_type": next((m for m in parsed["mods"] if m in lum), None),
                "mod_effects": [subs[m] for m in parsed["mods"] if m in subs],
                "raw": w.t,
                "x": round(w.cx, 4),
                "y": round(w.cy, 4),
                "rotation": w.r,
                "attached_number": False,
            }
            if parsed.get("default_panel"):
                item["default_panel"] = True
            consumed.add(i)
            if not item["circuits"]:
                best: tuple[float, int] | None = None
                for j in number_words:
                    if j in consumed or pw.words[j].r != w.r:
                        continue
                    nw = pw.words[j]
                    dist = _box_distance_in(w.b, nw.b, w_in, h_in)
                    if dist > ATTACH_RADIUS_IN:
                        continue
                    below_or_right = nw.cy > w.cy - 1e-6 or nw.cx > w.cx
                    score = dist + (0.0 if below_or_right else 0.1)
                    if best is None or score < best[0]:
                        best = (score, j)
                if best is not None:
                    j = best[1]
                    m = CIRCUIT_NUMBER_RE.match(pw.words[j].t)
                    item["circuits"] = _circuits(m.group("n") if m else None)
                    item["attached_number"] = True
                    consumed.add(j)
            circuit.append(item)
            if lum and item["fixture_type"]:
                type_marks.setdefault(item["fixture_type"], []).append([item["x"], item["y"]])
            continue
        m = STATUS_TAG_RE.match(w.t)
        if m:
            status.setdefault(m.group(1), []).append([round(w.cx, 4), round(w.cy, 4)])
        elif forms:
            sm = STATUS_SUFFIX_RE.search(w.t.upper())
            if sm and sm.group(1) in forms and len(w.t) > 2:
                status.setdefault(sm.group(1), []).append([round(w.cx, 4), round(w.cy, 4)])
        if lum:
            parts = [p for p in w.t.upper().split("/") if p]
            hit = next((p for p in parts if p in lum), None)
            if hit:
                type_marks.setdefault(hit, []).append([round(w.cx, 4), round(w.cy, 4)])
    labels = panel_labels_on_page(pw, regions, plan_area_only=True)
    for lab in labels:
        lab.pop("h", None)
    tags = {
        "circuit": circuit,
        "status": {k: v for k, v in sorted(status.items())},
        "type_marks": {k: v for k, v in sorted(type_marks.items())},
        "panel_labels": labels,
    }
    if default_panel:
        tags["default_panel"] = (
            default_panel  # only when a note declares one (4-key shape otherwise)
        )
    return tags, consumed


def occurrences_by_circuit(tags: dict[str, Any]) -> dict[tuple[str, int], list[list[float]]]:
    """(panel, circuit) -> positions, for route synthesis."""
    out: dict[tuple[str, int], list[list[float]]] = {}
    for t in tags.get("circuit") or []:
        for c in t.get("circuits") or []:
            out.setdefault((t["panel"], int(c)), []).append([t["x"], t["y"]])
    return out


def clusters(
    points: list[list[float]], w_in: float, h_in: float, *, eps_in: float = CLUSTER_EPS_IN
) -> list[list[int]]:
    """Single-link clusters of point indices in sheet inches."""
    n = len(points)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        xi, yi = points[i][0] * w_in, points[i][1] * h_in
        for j in range(i + 1, n):
            xj, yj = points[j][0] * w_in, points[j][1] * h_in
            if ((xi - xj) ** 2 + (yi - yj) ** 2) ** 0.5 <= eps_in:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0]))


def tag_regions(
    points: list[list[float]],
    w_in: float,
    h_in: float,
    *,
    pad_in: float = DEFAULT_PAD_IN,
    max_regions: int = MAX_REGIONS,
    eps_in: float = CLUSTER_EPS_IN,
) -> list[Bbox]:
    """Padded cluster bboxes (largest clusters first), at most `max_regions`,
    for escalation and zoom targets."""
    out: list[Bbox] = []
    px, py = pad_in / (w_in or 42.0), pad_in / (h_in or 30.0)
    for group in clusters(points, w_in, h_in, eps_in=eps_in)[:max_regions]:
        xs = [points[i][0] for i in group]
        ys = [points[i][1] for i in group]
        out.append(
            round_box(
                (
                    max(0.0, min(xs) - px),
                    max(0.0, min(ys) - py),
                    min(1.0, max(xs) + px),
                    min(1.0, max(ys) + py),
                )
            )
        )
    return out
