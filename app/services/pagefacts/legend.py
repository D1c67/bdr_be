"""Legend dictionary (WS1 step 4): table legends (SYMBOL, TAG, SPEC SECTION,
DESCRIPTION columns), abbreviation tables (ABBR, DESCRIPTION), key-value
legends ("1 = CIRCUIT NUMBER", '"NL" INDICATES ...', "(X) TAG or 'TAG'-X" with
its text column) and the modifier effects they declare.

Table rows are built from words, never lines: a legend row's TAG and
DESCRIPTION cells are separate lines in most CAD exports (the column gap is
wider than a word gap), and the two symbol lists of 7154 E0.00 sit side by
side, so each table owns an x-span bounded by the next header cluster on its
baseline and takes only left-aligned cell text (a cell's first word starts
at its column header). Entries carry `used_on_pages` (tag tokens found in the
plan areas of the document's plan pages) and a `device_type` from a keyword
table; `rank_entries` orders them by modifier relevance and device-type
relevance first, then `used_on_pages`, so the receptacle, junction-box and
connection entries survive a small ledger cap.

Pure functions over `PageWords`; no model, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from app.services.pagefacts.contracts import Bbox, Line, PageWords, Region, Word, round_box
from app.services.pagefacts.notes import (
    column_items,
    line_height_frac,
    plain_text,
    union_box,
)
from app.services.pagefacts.regions import in_plan_area, is_heading, median_line_height, seed_kind

LEGEND_REGION_KINDS: tuple[str, ...] = ("legend", "abbreviations")

HEADER_ROLES: dict[str, str] = {
    "SYMBOL": "symbol",
    "SYM": "symbol",
    "TAG": "tag",
    "MARK": "tag",
    "SPEC": "spec",
    "SECTION": "spec",
    "DESCRIPTION": "description",
    "DESC": "description",
    "MEANING": "description",
    "ABBR": "abbr",
    "ABBREV": "abbr",
    "ABBREVIATION": "abbr",
}
TAG_RE = re.compile(r"^[A-Z0-9#'][A-Z0-9#'\-/ ]{0,24}$")
TAG_CONTINUATION_RE = re.compile(r"^/[A-Z0-9#'\-/]{1,24}$")
ABBR_RE = re.compile(r"^[A-Z0-9#/&.\-]{1,10}$")
CELL_REACH = 0.02  # a cell's first word starts within this (page fraction) of its header x0
STACK_TOL = 0.006  # header words stacked vertically share an x-center within this
HEADER_BAND = 1.4  # header lines cluster within this many line heights
ROW_TOL = 0.5  # of the word height: words on one row
CONTINUATION_GAP = 1.5  # line heights
WORD_GAP = 3.0  # word heights (as a page-width fraction): a wider gap ends a cell
TABLE_END_GAP_LINES = 6.0
TABLE_END_HEADING = 1.5  # of the median line height: a heading this tall ends a table
NEIGHBOUR_DY = 0.12  # a header cluster this close in y and to the right bounds the x-span

KEY_LINE_RE = re.compile(
    r"""^(?=.{1,24}$)"?[A-Za-z0-9()#'/\-]{1,12}"?(?:\s+(?:TAG|or|OR|and|AND|'?[A-Z0-9'()\-]{1,8}'?))*$"""
)
KEY_INLINE_RE = re.compile(
    r"""^(?P<num>"?[A-Za-z0-9()#'/\-]{1,12}"?(?:\s+[A-Z]{2,12})?)\s*(?:=|INDICATES)\s*(?P<text>\S.*)$"""
)

EFFECTS: tuple[str, ...] = (
    "emergency_circuit",
    "life_safety_branch",
    "normal_branch",
    "critical_branch",
    "night_light",
    "switched_emergency",
    "existing_remain",
    "remove",
    "relocate",
    "new",
    "in_scope_underline",
    "tamper_resistant",
    "gfci",
    "weatherproof",
    "isolated_ground",
    "usb",
    "occupancy_controlled",
    "surface_mounted",
    "mounting_height",
    "circuit_number",
    "switch_designation",
    "zone_designation",
    "fixture_tag",
    "not_in_contract",
    "other",
)

# (effect, regex on "KEY TEXT"); first match wins, except the status tags
# which are checked in order so "(X) ... REMOVED" never reads as "new".
MODIFIER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("emergency_circuit", re.compile(r"SHADED\s+(?:DEVICE|SYMBOL).*EMERGENCY", re.I)),
    ("life_safety_branch", re.compile(r"LIFE\s+SAFETY\s+BRANCH", re.I)),
    ("critical_branch", re.compile(r"CRITICAL\s+BRANCH", re.I)),
    ("normal_branch", re.compile(r"NORMAL\s+BRANCH", re.I)),
    ("night_light", re.compile(r"(?:^|\s)\"?NL\"?\b.*NIGHT\s*LIGHT", re.I)),
    ("switched_emergency", re.compile(r"(?:^|\s)\"?SE\"?\b.*(?:SWITCHED|EMERGENCY)", re.I)),
    ("existing_remain", re.compile(r"(?:\(E\)|'TAG'-E\b|-E\b).*(?:EXISTING|REMAIN)", re.I)),
    ("remove", re.compile(r"(?:\(X\)|'TAG'-X\b|-X\b|\(D\)).*(?:REMOV|DEMOLI)", re.I)),
    ("relocate", re.compile(r"(?:\(R\)|'TAG'-R\b|-R\b).*RELOCAT", re.I)),
    ("new", re.compile(r"\(N\).*NEW", re.I)),
    ("in_scope_underline", re.compile(r"UNDERLINED\s+TAG.*IN-?SCOPE", re.I)),
    ("not_in_contract", re.compile(r"\bNIC\b|NOT\s+IN\s+CONTRACT", re.I)),
    ("circuit_number", re.compile(r"^\S{1,4}\s*=?\s*CIRCUIT\s+NUMBER", re.I)),
    ("switch_designation", re.compile(r"^\S{1,4}\s*=?\s*SWITCH\s+DESIGNATION", re.I)),
    ("zone_designation", re.compile(r"^\S{1,4}\s*=?\s*ZONE\s+DESIGNATION", re.I)),
    ("fixture_tag", re.compile(r"^\S{1,4}\s*=?\s*FIXTURE\s+TAG", re.I)),
    ("tamper_resistant", re.compile(r"TAMPER", re.I)),
    ("gfci", re.compile(r"\bGF(?:C)?I\b|GROUND\s+FAULT", re.I)),
    ("weatherproof", re.compile(r"WEATHERPROOF|\bWP\b|WHILE-IN-USE", re.I)),
    ("isolated_ground", re.compile(r"ISOLATED\s+GROUND", re.I)),
    ("usb", re.compile(r"\bUSB\b", re.I)),
    ("occupancy_controlled", re.compile(r"OCCUPANCY", re.I)),
    ("surface_mounted", re.compile(r"SURFACE\s+MOUNT", re.I)),
    (
        "mounting_height",
        re.compile(r"MOUNT(?:ED|ING)?\s+(?:AT|IN|ORIENTED|HEIGHT)|\bAFF\b|CENTERLINE", re.I),
    ),
)

DEVICE_TYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gfci_receptacle", re.compile(r"\bGF(?:C)?I\b|GROUND\s+FAULT", re.I)),
    ("quad_receptacle", re.compile(r"\bQUAD", re.I)),
    ("duplex_receptacle", re.compile(r"DUPLEX|RECEPTACLE", re.I)),
    ("floor_box", re.compile(r"FLOOR\s+BOX|POKE\s*THROUGH", re.I)),
    ("junction_box", re.compile(r"JUNCTION\s+BOX|\bJ-?BOX\b", re.I)),
    ("disconnect", re.compile(r"DISCONNECT", re.I)),
    ("panelboard", re.compile(r"PANELBOARD|\bPANEL\b", re.I)),
    ("transformer", re.compile(r"TRANSFORMER", re.I)),
    ("exit_sign", re.compile(r"EXIT\s+SIGN", re.I)),
    ("emergency_unit", re.compile(r"EMERGENCY\s+(?:UNIT|LIGHT|BATTERY)", re.I)),
    ("occupancy_sensor", re.compile(r"OCCUPANCY|VACANCY\s+SENSOR", re.I)),
    ("dimmer", re.compile(r"DIMMER", re.I)),
    ("switch_3way", re.compile(r"3-?WAY|THREE\s+WAY", re.I)),
    ("switch_1p", re.compile(r"\bSWITCH\b", re.I)),
    ("fa_smoke", re.compile(r"SMOKE", re.I)),
    ("fa_horn_strobe", re.compile(r"HORN|STROBE", re.I)),
    ("fa_pull_station", re.compile(r"PULL\s+STATION", re.I)),
    ("data_outlet", re.compile(r"\bDATA\b|\bVOICE\b|TECHNOLOGY\s+OUTLET|RI-?TECH", re.I)),
    ("card_reader", re.compile(r"CARD\s+READER", re.I)),
    ("camera", re.compile(r"CAMERA", re.I)),
    ("motor_connection", re.compile(r"ELECTRICAL\s+CONNECTION|\bECONN\b|MOTOR\s+CONNECTION", re.I)),
    (
        "fixture_type_ref",
        re.compile(r"LUMINAIRE|TROFFER|DOWNLIGHT|SCONCE|\bLINEAR\b|LIGHT\s+FIXTURE", re.I),
    ),
)

# Ledger relevance: device classes that drive counts first.
DEVICE_PRIORITY: tuple[str, ...] = (
    "gfci_receptacle",
    "duplex_receptacle",
    "quad_receptacle",
    "junction_box",
    "motor_connection",
    "disconnect",
    "floor_box",
    "data_outlet",
    "switch_1p",
    "switch_3way",
    "dimmer",
    "occupancy_sensor",
    "exit_sign",
    "emergency_unit",
    "fixture_type_ref",
    "panelboard",
    "transformer",
    "fa_smoke",
    "fa_horn_strobe",
    "fa_pull_station",
    "card_reader",
    "camera",
    "other",
)
RELEVANT_EFFECTS: frozenset[str] = frozenset(
    {"gfci", "tamper_resistant", "weatherproof", "usb", "isolated_ground", "occupancy_controlled"}
)


def device_type_of(description: str) -> str:
    for name, rx in DEVICE_TYPES:
        if rx.search(description):
            return name
    return "other"


def effect_of(text: str) -> str | None:
    for effect, rx in MODIFIER_RULES:
        if rx.search(text):
            return effect
    return None


# Table legends ---------------------------------------------------------------------


@dataclass(slots=True)
class HeaderCluster:
    cy: float
    y1: float
    columns: list[tuple[str, float, float, float]]  # (role, x0, x1, center) sorted by center
    x_end: float = 1.0
    y_end: float = 1.0

    @property
    def x0(self) -> float:
        return min(c[1] for c in self.columns)

    @property
    def x1(self) -> float:
        return max(c[2] for c in self.columns)

    def roles(self) -> set[str]:
        return {c[0] for c in self.columns}

    def column(self, role: str) -> tuple[str, float, float, float] | None:
        for c in self.columns:
            if c[0] == role:
                return c
        return None

    def band(self, role: str) -> tuple[float, float] | None:
        """[x0, x1) of a column: midpoints to the neighbouring headers."""
        idx = next((i for i, c in enumerate(self.columns) if c[0] == role), None)
        if idx is None:
            return None
        center = self.columns[idx][3]
        left = (self.columns[idx - 1][3] + center) / 2 if idx > 0 else self.x0 - 0.01
        right = (
            (center + self.columns[idx + 1][3]) / 2 if idx + 1 < len(self.columns) else self.x_end
        )
        return left, right


def _header_role(text: str) -> str | None:
    t = text.strip().rstrip(":").strip().upper()
    return HEADER_ROLES.get(t)


def header_clusters(pw: PageWords, *, limit_x: float = 1.0) -> list[HeaderCluster]:
    """Legend and abbreviation table headers: header-vocabulary lines on one
    baseline (stacked words such as SPEC over SECTION merge by x-center)."""
    cands = [
        (ln, role)
        for ln in pw.lines
        if ln.cx < limit_x and (role := _header_role(ln.t)) is not None
    ]
    cands.sort(key=lambda c: (c[0].cy, c[0].b[0]))
    used = [False] * len(cands)
    out: list[HeaderCluster] = []
    for i, (ln, role) in enumerate(cands):
        if used[i]:
            continue
        h = line_height_frac(ln, pw)
        members = [(ln, role)]
        used[i] = True
        for j in range(i + 1, len(cands)):
            if used[j]:
                continue
            ln2, role2 = cands[j]
            if ln2.cy - ln.cy > HEADER_BAND * 2 * h:
                break
            same_row = abs(ln2.cy - ln.cy) <= HEADER_BAND * h
            stacked = any(
                abs(ln2.cx - m.cx) <= STACK_TOL and 0 <= ln2.b[1] - m.b[3] <= 1.2 * h
                for m, _r in members
            )
            if not (same_row or stacked):
                continue
            # Only merge members that are part of one table: a header word
            # further than a wide gap from the previous one starts another.
            members.append((ln2, role2))
            used[j] = True
        # Split by wide x gaps into separate tables on the same baseline.
        members.sort(key=lambda m: m[0].b[0])
        groups: list[list[tuple[Line, str]]] = []
        for m in members:
            if not groups:
                groups.append([m])
                continue
            roles_so_far = {r for _l, r in groups[-1]}
            wide_gap = m[0].b[0] - max(x[0].b[2] for x in groups[-1]) > 0.08
            # A second table on the baseline starts at its own key column
            # (SYMBOL, TAG or ABBR) once the current one already has its text column.
            new_table = m[1] in ("symbol", "tag", "abbr") and (
                "description" in roles_so_far or m[1] in roles_so_far
            )
            if wide_gap or new_table:
                groups.append([m])
            else:
                groups[-1].append(m)
        for grp in groups:
            cols: dict[str, list[Line]] = {}
            for m, r in grp:
                cols.setdefault(r, []).append(m)
            if "description" not in cols or not ({"tag", "abbr"} & set(cols)):
                continue
            columns = []
            for r, lines in cols.items():
                x0 = min(x.b[0] for x in lines)
                x1 = max(x.b[2] for x in lines)
                columns.append((r, x0, x1, (x0 + x1) / 2))
            columns.sort(key=lambda c: c[3])
            out.append(
                HeaderCluster(
                    cy=sum(m.cy for m, _r in grp) / len(grp),
                    y1=max(m.b[3] for m, _r in grp),
                    columns=columns,
                )
            )
    out.sort(key=lambda c: (c.cy, c.x0))
    # Extents: the next cluster to the right on the same baseline bounds the
    # x-span; the next cluster or heading below (x-overlapping) bounds y.
    med = median_line_height(pw.lines)
    headings = [
        ln
        for ln in pw.lines
        if ln.h >= TABLE_END_HEADING * med and is_heading(ln, med) and _header_role(ln.t) is None
    ]
    for c in out:
        right = [
            o.x0
            for o in out
            if o is not c and abs(o.cy - c.cy) < NEIGHBOUR_DY and o.x0 > c.x1 - 0.005
        ]
        c.x_end = min(right) - 0.003 if right else limit_x
        below = [
            o.cy - 0.012
            for o in out
            if o is not c and o.cy > c.cy + 0.01 and o.x0 < c.x_end and o.x1 > c.x0
        ]
        below += [
            ln.cy - 0.006 for ln in headings if ln.cy > c.y1 and c.x0 - 0.02 <= ln.b[0] <= c.x_end
        ]
        c.y_end = min(below) if below else 1.0
    return out


def _rows_of(words: list[Word], pw: PageWords) -> list[list[Word]]:
    """Cluster words into rows by center y (0.5 x the median word height)."""
    if not words:
        return []
    hs = [w.h for w in words if w.h > 0]
    tol = ROW_TOL * (float(median(hs)) if hs else 7.0) / ((pw.page_h_in or 30.0) * 72.0)
    ordered = sorted(words, key=lambda w: (w.cy, w.b[0]))
    rows: list[list[Word]] = []
    for w in ordered:
        if rows and abs(w.cy - sum(x.cy for x in rows[-1]) / len(rows[-1])) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    for r in rows:
        r.sort(key=lambda w: w.b[0])
    return rows


def _cell(
    words: list[Word], band: tuple[float, float], header_x0: float, pw: PageWords
) -> list[Word]:
    """Left-aligned cell text in a column band: the first word starts near
    the header's x0, following words continue without a wide gap."""
    inside = [w for w in words if band[0] <= w.cx < band[1] and w.r == 0]
    if not inside:
        return []
    first = next((w for w in inside if abs(w.b[0] - header_x0) <= CELL_REACH), None)
    if first is None:
        return []
    out = [first]
    width_unit = 1.0 / ((pw.page_w_in or 42.0) * 72.0)
    for w in inside:
        if w.b[0] <= first.b[0]:
            continue
        prev = out[-1]
        if w.b[0] - prev.b[2] > WORD_GAP * max(prev.h, w.h, 1.0) * width_unit:
            break
        out.append(w)
    return out


def parse_tables(
    pw: PageWords, page: int, *, limit_x: float = 1.0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Bbox]]:
    """(entries, abbreviations, table areas) from every header cluster;
    `limit_x` is the title block's left edge (tables never cross it)."""
    clusters = header_clusters(pw, limit_x=limit_x)
    entries: list[dict[str, Any]] = []
    abbreviations: list[dict[str, Any]] = []
    areas: list[Bbox] = []
    for c in clusters:
        key_role = "tag" if c.column("tag") else "abbr"
        key_col = c.column(key_role)
        desc_col = c.column("description")
        if key_col is None or desc_col is None:
            continue
        key_band = c.band(key_role) or (0.0, 0.0)
        desc_band = c.band("description") or (0.0, 0.0)
        spec_band = c.band("spec")
        symbol_band = c.band("symbol")
        spec_col = c.column("spec")
        body = [w for w in pw.words if c.x0 - 0.015 <= w.cx < c.x_end and c.y1 < w.cy < c.y_end]
        rows = _rows_of(body, pw)
        current: dict[str, Any] | None = None
        last_bottom = c.y1
        group_label = _group_label(pw, c)
        for row in rows:
            row_h = max(w.h for w in row) / ((pw.page_h_in or 30.0) * 72.0)
            key_words = _cell(row, key_band, key_col[1], pw)
            key_text = plain_text(" ".join(w.t for w in key_words)) if key_words else ""
            desc_words = _cell(row, desc_band, desc_col[1], pw)
            desc_text = plain_text(" ".join(w.t for w in desc_words)) if desc_words else ""
            gap = min(w.b[1] for w in row) - last_bottom
            starts = bool(key_text) and bool(
                (TAG_RE.match(key_text) if key_role == "tag" else ABBR_RE.match(key_text))
            )
            wrapped_tag = (
                current is not None
                and key_role == "tag"
                and str(current.get("tag", "")).endswith(("-", "/"))
                and gap <= CONTINUATION_GAP * max(row_h, 0.002)
            )
            if wrapped_tag and key_text:
                starts = False
            if starts:
                row_box = union_box([w.b for w in row])
                item: dict[str, Any] = {
                    "tag" if key_role == "tag" else "abbr": key_text,
                    "description" if key_role == "tag" else "meaning": desc_text,
                    "page": page,
                    "bbox": row_box,
                }
                if key_role == "tag":
                    spec_text = ""
                    if spec_band and spec_col:
                        spec_words = _cell(row, spec_band, spec_col[1], pw)
                        spec_text = plain_text(" ".join(w.t for w in spec_words))
                    item["spec_section"] = (
                        spec_text if spec_text and spec_text.upper() != "N/A" else None
                    )
                    item["group"] = group_label
                    item["symbol_bbox"] = (
                        round_box((symbol_band[0], row_box[1], symbol_band[1], row_box[3]))
                        if symbol_band
                        else None
                    )
                    entries.append(item)
                else:
                    abbreviations.append(item)
                current = item
                last_bottom = max(w.b[3] for w in row)
                continue
            if current is None or gap > CONTINUATION_GAP * max(row_h, 0.002):
                continue
            if (
                key_text
                and key_role == "tag"
                and (TAG_CONTINUATION_RE.match(key_text) or wrapped_tag)
            ):
                current["tag"] = current["tag"] + key_text
            if desc_text:
                field_name = "description" if key_role == "tag" else "meaning"
                current[field_name] = plain_text(current[field_name] + " " + desc_text)
            current["bbox"] = union_box([current["bbox"], union_box([w.b for w in row])])
            last_bottom = max(w.b[3] for w in row)
        if rows:
            areas.append(
                round_box(
                    (
                        c.x0 - 0.015,
                        c.cy - 0.01,
                        c.x_end,
                        max(w.b[3] for r in rows for w in r),
                    )
                )
            )
    return entries, abbreviations, areas


def _group_label(pw: PageWords, c: HeaderCluster) -> str | None:
    """The heading line just above a header cluster (e.g. ELECTRICAL SYMBOL LIST)."""
    med = median_line_height(pw.lines)
    best: Line | None = None
    for ln in pw.lines:
        if ln.cy >= c.cy or c.cy - ln.cy > 0.05:
            continue
        if ln.b[0] > c.x_end or ln.b[2] < c.x0:
            continue
        if not is_heading(ln, med) or _header_role(ln.t) is not None:
            continue
        if best is None or ln.cy > best.cy:
            best = ln
    return plain_text(best.t) if best else None


# Key-value legends ---------------------------------------------------------------


def _inside_any(box: Bbox, areas: list[Bbox]) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(a[0] <= cx <= a[2] and a[1] <= cy <= a[3] for a in areas)


def _column_lines_below(pw: PageWords, heading: Line, areas: list[Bbox]) -> list[Line]:
    """Lines under a legend heading in its own column (x0 within -0.012 and
    +0.12 of the heading's x0) until a peer heading (as tall as this one)
    in that column or a gap of six line heights; table areas are excluded."""
    out: list[Line] = []
    last_bottom = heading.b[3]
    last_h = line_height_frac(heading, pw)
    for ln in sorted(pw.lines, key=lambda x: (x.cy, x.b[0])):
        if ln.cy <= heading.cy or ln is heading:
            continue
        if not (heading.b[0] - 0.012 <= ln.b[0] <= heading.b[0] + 0.12):
            continue
        if _inside_any(ln.b, areas):
            continue
        if ln.b[1] - last_bottom > TABLE_END_GAP_LINES * max(last_h, 0.002):
            break
        peer = seed_kind(ln.t) is not None and ln.h >= 0.9 * heading.h
        if peer or ln.h >= 1.5 * heading.h:
            break
        out.append(ln)
        last_bottom = max(last_bottom, ln.b[3])
        last_h = line_height_frac(ln, pw)
    return out


def key_value_items(
    pw: PageWords, regions: list[Region], areas: list[Bbox], page: int
) -> list[dict[str, Any]]:
    """Modifier and subscript declarations from the key-value legends."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for region in regions:
        if region.kind not in LEGEND_REGION_KINDS or not region.label:
            continue
        heading = next((ln for ln in pw.lines if ln.t.strip() == region.label.strip()), None)
        if heading is None:
            continue
        lines = _column_lines_below(pw, heading, areas)
        items, leftover = column_items(
            pw, lines, key_re=KEY_LINE_RE, inline_re=KEY_INLINE_RE, max_key_chars=24
        )
        for it in items:
            key_text, text = it.key, it.text
            inline = KEY_INLINE_RE.match(text)
            if inline is not None:
                # A symbol label next to an inline "k = v" line: the line is the item.
                key_text, text = plain_text(inline.group("num")), plain_text(inline.group("text"))
            if len(text) < 3:
                continue
            full = f"{key_text} {text}".strip()
            effect = effect_of(full)
            key = (key_text, text)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "key": key_text,
                    "text": text,
                    "effect": effect or "other",
                    "group": plain_text(region.label),
                    "page": page,
                    "bbox": it.bbox,
                }
            )
        for ln in leftover:
            effect = effect_of(ln.t)
            if effect is None or effect in ("mounting_height", "surface_mounted"):
                continue
            key = ("", plain_text(ln.t))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "key": "",
                    "text": plain_text(ln.t),
                    "effect": effect,
                    "group": plain_text(region.label),
                    "page": page,
                    "bbox": list(ln.b),
                }
            )
    return out


def _applies_to(mod: dict[str, Any]) -> str:
    group = (mod.get("group") or "").upper()
    key = mod.get("key") or ""
    if "LUMINAIRE" in group or mod["effect"] in (
        "life_safety_branch",
        "normal_branch",
        "critical_branch",
        "night_light",
        "switched_emergency",
        "fixture_tag",
    ):
        return "luminaire"
    if "SUBSCRIPT" in group or (len(key) <= 2 and key.isalpha()):
        return "subscript"
    if mod["effect"] in ("existing_remain", "remove", "relocate", "new", "in_scope_underline"):
        return "tag"
    return "device"


# Document level ---------------------------------------------------------------------


@dataclass(slots=True)
class PageLegend:
    entries: list[dict[str, Any]] = field(default_factory=list)
    abbreviations: list[dict[str, Any]] = field(default_factory=list)
    modifiers: list[dict[str, Any]] = field(default_factory=list)
    areas: list[Bbox] = field(default_factory=list)


def parse_page(pw: PageWords, regions: list[Region], page: int) -> PageLegend:
    """Every legend fact on one page (tables first, then key-value blocks)."""
    if not any(r.kind in LEGEND_REGION_KINDS for r in regions):
        return PageLegend()
    tb = next((r for r in regions if r.kind == "title_block"), None)
    limit_x = tb.bbox[0] if tb is not None and tb.bbox[0] > 0.5 else 1.0
    entries, abbreviations, areas = parse_tables(pw, page, limit_x=limit_x)
    modifiers = key_value_items(pw, regions, areas, page)
    return PageLegend(entries, abbreviations, modifiers, areas)


def _tag_tokens(tag: str) -> set[str]:
    return {t for t in re.split(r"[\s/]+", tag) if t and "#" not in t and len(t) >= 2}


def used_on_pages(
    entries: list[dict[str, Any]], plan_pages: dict[int, tuple[PageWords, list[Region]]]
) -> None:
    """Fill `used_on_pages`: the entry's tag (or a component) found as a word
    in the plan area of a plan page."""
    index: dict[int, set[str]] = {}
    for page, (pw, regions) in plan_pages.items():
        toks: set[str] = set()
        for w in pw.words:
            if not in_plan_area(regions, w.cx, w.cy):
                continue
            toks.add(w.t)
            toks.update(p for p in w.t.split("/") if p)
        index[page] = toks
    for e in entries:
        tag = e.get("tag", "")
        wanted = {tag} | ({tag.split("/")[0]} if "/" in tag else set())
        wanted = {t for t in wanted if t and "#" not in t}
        e["used_on_pages"] = sorted(p for p, toks in index.items() if wanted & toks)


def build_document_legend(
    page_legends: dict[int, PageLegend],
    plan_pages: dict[int, tuple[PageWords, list[Region]]],
) -> dict[str, Any]:
    """`DocFacts.legend`: entries with ids, device types and usage; modifiers
    with ids and `applies_to`; abbreviations. Duplicate tags keep the first
    (document order) and list the others as `alternates`."""
    entries: list[dict[str, Any]] = []
    by_tag: dict[str, dict[str, Any]] = {}
    modifiers: list[dict[str, Any]] = []
    abbreviations: list[dict[str, Any]] = []
    for page in sorted(page_legends):
        pl = page_legends[page]
        for e in pl.entries:
            tag = e["tag"]
            if tag in by_tag:
                by_tag[tag].setdefault("alternates", []).append(
                    {"page": e["page"], "description": e["description"]}
                )
                continue
            item = dict(e)
            item["entry_id"] = f"lg{len(entries) + 1}"
            item["device_type"] = device_type_of(e["description"])
            item.setdefault("alternates", [])
            by_tag[tag] = item
            entries.append(item)
        for m in pl.modifiers:
            twin = next((x for x in modifiers if x["text"] == m["text"]), None)
            if twin is not None:
                if not twin.get("key") and m.get("key"):
                    twin["key"], twin["group"], twin["bbox"] = m["key"], m["group"], m["bbox"]
                    twin["applies_to"] = _applies_to(twin)
                continue
            item = dict(m)
            item["modifier_id"] = f"md{len(modifiers) + 1}"
            item["applies_to"] = _applies_to(item)
            modifiers.append(item)
        for a in pl.abbreviations:
            abbreviations.append(dict(a))
    used_on_pages(entries, plan_pages)
    return {"entries": entries, "modifiers": modifiers, "abbreviations": abbreviations}


def subscript_map(legend: dict[str, Any]) -> dict[str, str]:
    """Subscript letter -> effect (G gfci, O occupancy_controlled, ...)."""
    out: dict[str, str] = {}
    for m in legend.get("modifiers", []):
        key = (m.get("key") or "").strip("\"' ")
        if m.get("applies_to") == "subscript" and 1 <= len(key) <= 2 and key.isalnum():
            out.setdefault(key.upper(), m["effect"])
    return out


def declared_status_forms(legend: dict[str, Any]) -> set[str]:
    """Status letters the legend declares as a '-X' tag suffix form."""
    out: set[str] = set()
    for m in legend.get("modifiers", []):
        text = f"{m.get('key', '')} {m.get('text', '')}"
        for letter in ("X", "E", "R", "N"):
            if re.search(rf"'TAG'-{letter}\b|DASH\s+'{letter}'", text):
                out.add(letter)
    return out


# Ranking and rendering -----------------------------------------------------------


def _device_prio(e: dict[str, Any]) -> int:
    dt = e.get("device_type", "other")
    return DEVICE_PRIORITY.index(dt) if dt in DEVICE_PRIORITY else len(DEVICE_PRIORITY)


def _has_relevant_effect(e: dict[str, Any]) -> bool:
    text = f"{e.get('tag', '')} {e.get('description', '')}"
    return any(rx.search(text) for eff, rx in MODIFIER_RULES if eff in RELEVANT_EFFECTS)


def rank_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ledger order: entries carrying a declared modifier (GFCI, tamper,
    weatherproof, USB, isolated ground, occupancy) first by device
    priority, then the rest round-robin over device types in priority
    order (one of each type per round), each type's list sorted by usage on
    plan pages then document order. A small cap therefore keeps one entry
    of every device class before a second receptacle variant."""
    indexed = list(enumerate(entries))
    tier0 = [(i, e) for i, e in indexed if _has_relevant_effect(e)]
    tier0.sort(key=lambda ie: (_device_prio(ie[1]), -len(ie[1].get("used_on_pages") or []), ie[0]))
    rest = [(i, e) for i, e in indexed if not _has_relevant_effect(e)]
    by_type: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for i, e in rest:
        by_type.setdefault(_device_prio(e), []).append((i, e))
    for lst in by_type.values():
        lst.sort(key=lambda ie: (-len(ie[1].get("used_on_pages") or []), ie[0]))
    ordered: list[dict[str, Any]] = [e for _i, e in tier0]
    while any(by_type.values()):
        for prio in sorted(by_type):
            if by_type[prio]:
                ordered.append(by_type[prio].pop(0)[1])
    return ordered


def _tokens(text: str) -> int:
    from app.services.pagefacts.block import estimate_tokens

    return estimate_tokens(text)


def render_legend_text(
    legend: dict[str, Any], *, max_tokens: int, sheet_label: str | None = None
) -> str:
    """The ledger legend text: modifiers in full, then ranked entries until
    the cap (the entries dropped are counted on the last line)."""
    head = "LEGEND (exact text from the PDF text layer"
    head += f" of {sheet_label}" if sheet_label else ""
    head += "; symbols themselves are drawings):"
    lines: list[str] = []
    mods = legend.get("modifiers") or []
    if mods:
        lines.append("MODIFIERS:")
        for m in mods:
            key = m.get("key") or ""
            effect = f" [{m['effect']}]" if m.get("effect") not in (None, "other") else ""
            lines.append(f"- {key + ' = ' if key else ''}{m['text']}{effect}")
    lines.append(head)
    budget = max_tokens - _tokens("\n".join(lines)) - 20
    shown = 0
    entries = rank_entries(legend.get("entries") or [])
    for e in entries:
        text = f"- {e['tag']}: {e['description']}"
        cost = _tokens(text)
        if cost > budget:
            break
        lines.append(text)
        budget -= cost
        shown += 1
    if shown < len(entries):
        lines.append(f"- ({len(entries) - shown} more legend entries not shown)")
    return "\n".join(lines)


def lookup(
    legend: dict[str, Any], query: str | None = None, *, effect: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """`legend_lookup` semantics: entries whose tag or description contains
    the query (case-insensitive), modifiers filtered by effect."""
    q = (query or "").strip().upper()
    entries = [
        e
        for e in legend.get("entries") or []
        if not q or q in e["tag"].upper() or q in e["description"].upper()
    ]
    mods = [
        m
        for m in legend.get("modifiers") or []
        if (not effect or m["effect"] == effect)
        and (not q or q in (m.get("key") or "").upper() or q in m["text"].upper())
    ]
    return {"entries": entries[:limit], "modifiers": mods[:limit]}
