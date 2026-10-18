"""Golden-set scorer for AI Estimating BOQs (docs/PLANS_READER.md section 4).

Compares an AI-produced BOQ spec (the BOQ_SPEC JSON from
app/services/plans_reader_schemas.py) with the estimator's real
`BOQ - COUNTS.xlsx` for the same project. Quantities only; no pricing.

Estimator workbook layout (confirmed on the 2026-10-01 production backup):
a blank 'Sheet1' (sometimes 'Chart1'), then 'SHEET' or one sheet per area.
Row 4 holds the headers A 'SR. NO.', B 'DESCRIPTION', C 'QUANTITY', D 'UNIT'.
Rows with B filled and C empty are headings: level 0 DEMOLITION / NEW WORK,
level 1 groups (WIRING, SWITCHGEAR, OTHER ITEMS, ...; centered and bold), then
subheads (CONDUCTORS (AWG), CONDUITS, ...; left aligned and bold). A heading
may carry a note on its second line. Item rows have a numeric C and a unit in
D. Unnumbered right-aligned rows are generated accessories (connectors,
couplings, straps, anchors, screws, adapters, locknuts, elbows).

Usage:
  uv run python scripts/plans_reader_golden.py --spec spec.json --estimator boq.xlsx [--json out.json]
  uv run python scripts/plans_reader_golden.py --spec spec.json --from-backup 026b2dc5
  uv run python scripts/plans_reader_golden.py --from-backup 026b2dc5   (structure only)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

_BE_ROOT = Path(__file__).resolve().parents[1]
if str(_BE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BE_ROOT))

import openpyxl  # noqa: E402

from app.services.plans_reader_schemas import (  # noqa: E402
    BOQ_GROUPS,
    BOQ_LEVEL0,
    BOQ_SUBGROUPS,
    UNITS,
)

MATCH_THRESHOLD = 0.72
EXACT_TOLERANCE = 0.02
WASTAGE_FACTOR = 1.1
DEFAULT_BACKUP_DIR = _BE_ROOT.parent / "backups" / "BDR_Prod_2026-10-01"

_BLANK_SHEETS = {"SHEET1", "CHART1"}
_ACCESSORY_SUBGROUP = "OTHER CONDUIT ACCESSORIES"
# Subheads seen in the sent BOQs that are not in the schema vocabulary.
_EXTRA_SUBGROUPS = ("TRENCH AND UNDERGROUND CONDUITS", "FIXTURES")
_KNOWN_SUBGROUPS = {s.split("(")[0].strip() for subs in BOQ_SUBGROUPS.values() for s in subs}
_KNOWN_SUBGROUPS.update(_EXTRA_SUBGROUPS)
# Subhead prefix -> its group, for item rows that appear before any group
# heading (seen on single-page site sheets that open with the trench subhead).
_SUBGROUP_PARENT: tuple[tuple[str, str], ...] = tuple(
    (prefix, g)
    for g, subs in BOQ_SUBGROUPS.items()
    for prefix in sorted({s.split("(")[0].strip().rstrip("S") for s in subs})
) + (("TRENCH", "WIRING"),)
_ACCESSORY_WORDS = re.compile(
    r"\b(CONN|CONNECTOR|COUPLING|STRAP|ANCHOR|SCREW|ADAPTER|LOCKNUT|ELBOW|BUSHING|BLANK COVER)\b"
)
_CAVEAT_WORDS = re.compile(
    r"ASSUMED|CONFIRM|NOT GIVEN|BY OTHERS|VERIFY|\bTBD\b|NOT SHOWN|NOT PROVIDED"
)
_UNIT_ALIASES = {
    "EA": "EA", "EACH": "EA", "PCS": "EA", "PC": "EA", "NO": "EA", "NOS": "EA",
    "FT": "FT", "LF": "FT", "FEET": "FT", "FOOT": "FT", "LFT": "FT",
    "CU FT": "CU FT", "CUFT": "CU FT", "CF": "CU FT", "CFT": "CU FT",
    "SQ FT": "SQ FT", "SQFT": "SQ FT", "SF": "SQ FT", "SFT": "SQ FT",
    "LS": "LS", "LOT": "LS", "LUMP SUM": "LS",
    "CY": "CY", "CU YD": "CY", "CUYD": "CY",
}
assert set(_UNIT_ALIASES.values()) <= set(UNITS)


# Data shapes ------------------------------------------------------------------

@dataclass
class BoqRow:
    area: str
    section: str
    group: str
    subgroup: str
    description: str
    quantity: float
    unit: str
    is_accessory: bool = False
    ref: str = ""  # 'SHEET!B14' for the estimator, 'areas[0]...' for the spec

    @property
    def group_key(self) -> str:
        return f"{self.section} / {self.group or '-'}"


@dataclass
class MatchedPair:
    ai: BoqRow
    est: BoqRow
    score: float
    abs_error: float
    pct_error: float | None
    unit_mismatch: bool
    group_mismatch: bool
    exact: bool


@dataclass
class MatchReport:
    pairs: list[MatchedPair]
    unmatched_ai: list[BoqRow]
    unmatched_est: list[BoqRow]
    ai_total: int
    est_total: int
    precision: float
    recall: float
    mape: float | None
    mape_rows: int
    exact_matches: int
    unit_mismatches: int
    group_mismatches: int
    per_group: dict[str, dict[str, Any]] = field(default_factory=dict)
    ignored_accessories: dict[str, int] = field(default_factory=dict)
    threshold: float = MATCH_THRESHOLD


# Normalization ----------------------------------------------------------------

_FRACTIONS = {"\u00bd": "1/2", "\u00bc": "1/4", "\u00be": "3/4", "\u215b": "1/8", "\u2153": "1/3"}
_QUOTES = {
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2018": "'", "\u2019": "'", "\u2032": "'", "`": "'",
}
_DASHES = {"\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2010": "-", "\u2011": "-"}


def _clean_text(s: Any) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\u00a0", " ")).strip()


def normalize_unit(u: Any) -> str:
    key = _clean_text(u).upper().rstrip(".")
    key = re.sub(r"[.]", "", key)
    return _UNIT_ALIASES.get(key, key)


def _strip_caveats(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = re.sub(
            r"\(([^()]*)\)",
            lambda m: " " if _CAVEAT_WORDS.search(m.group(1)) else m.group(0),
            s,
        )
    return s


def normalize_description(s: Any) -> str:
    """Uppercase, unify quotes / fractions / dashes, drop caveat parentheticals,
    collapse spaces and strip trailing punctuation. A rendered AI row carries
    its material note on a second line in parentheses ("#3/0 THHN/THWN Cu
    (ASSUMED)\n(Feeder 1: 4 x 300 FT ...)"); only the first line names the
    item, so the note is dropped before matching."""
    text = "" if s is None else str(s)
    # Only a note that starts on its own line and is wrapped in parentheses
    # is dropped; a line break inside the item text itself is kept.
    text = re.sub(r"\n\s*\(.*\)\s*$", "", text, flags=re.S)
    return _normalize(text)


@lru_cache(maxsize=65536)
def _normalize(t: str) -> str:
    for src, dst in {**_QUOTES, **_DASHES}.items():
        t = t.replace(src, dst)
    for src, dst in _FRACTIONS.items():
        # '1\u00bd' -> '1-1/2', '\u00bd' -> '1/2'
        t = re.sub(rf"(\d)\s*{src}", rf"\1-{dst}", t)
        t = t.replace(src, dst)
    t = t.upper().replace("''", '"')
    t = _strip_caveats(t)
    t = re.sub(r"\s+", " ", t)
    # Mixed numbers: '1 1/4"' -> '1-1/4"'
    t = re.sub(r"\b(\d+)\s+(\d+/\d+)", r"\1-\2", t)
    # Inch words after a size: '3/4 IN' / '3/4 INCH' -> '3/4"'
    t = re.sub(r"(\d)\s*(?:INCHES|INCH|IN\.?)(?=\s|$|[,;)])", r'\1"', t)
    # Size and conduit marker: '3/4" C EMT' -> '3/4"C EMT'
    t = re.sub(r'(\d)\s*"\s*C\b', r'\1"C', t)
    # '3/4"EMT' -> '3/4"C EMT' style consistency is not forced; spaces around
    # commas and slashes are tidied.
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"\s*/\s*", "/", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[\s.,;:\-]+$", "", t)
    return t.strip()


def _heading_name(text: Any) -> tuple[str, str]:
    """('WIRING', '(10% wastage ...)') from a two-line heading cell."""
    raw = "" if text is None else str(text)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return "", ""
    name = _clean_text(lines[0]).upper()
    note = " ".join(lines[1:])
    return name, note


def _canonical(name: str) -> str:
    """Heading without trailing notes or parentheticals, for vocabulary checks."""
    return re.sub(r"\s+", " ", re.sub(r"\(.*", "", name)).strip().upper()


# Key tokens -------------------------------------------------------------------

_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("wire", re.compile(r"#\s?(\d+/0|\d+)(?!\s*X)\b")),
    ("wire", re.compile(r"(?<![#/\d])\b(\d+/0|\d{1,2})\s?AWG\b")),
    ("kcmil", re.compile(r"\b(\d{3,4})\s?(?:KCMIL|MCM)\b")),
    ("size", re.compile(r'(?<![#\w/-])(\d+(?:-\d+/\d+)?|\d+/\d+)"')),
    ("amps", re.compile(r"\b(\d{1,4})\s?A(?:MP|MPS)?\b")),
    ("poles", re.compile(r"\b(\d{1,2})[\s-]?(?:P|POLES?)\b")),
    ("fuse", re.compile(r"\b(\d{1,4})\s?AF\b")),
    ("main", re.compile(r"\b(MLO|MCB)\b")),
    ("panel", re.compile(r"\b(?:PANEL|SWITCHBOARD|PANELBOARD)\s+'?(?!TYPE\b)(?!\d+A\b)([A-Z0-9-]{1,6})'?(?=[,\s]|$)")),
    ("kva", re.compile(r"\b(\d+(?:\.\d+)?)\s?KVA\b")),
    ("nema", re.compile(r"\bNEMA[\s-]?(\d+[A-Z]?)\b")),
    ("conduit", re.compile(r"\b(EMT|PVC|RGS|RMC|IMC|FMC|LFMC|MC|ENT)\b")),
    ("strand", re.compile(r"\b(\d+)[\s-]?STRAN?D\b")),
    ("cable", re.compile(r"\b(CAT\s?\d+E?|RG\d+)\b")),
    ("cores", re.compile(r"/(\d+)C\b")),  # '#22/3C CONTROL CABLE'
    ("config", re.compile(r"\b(L?\d{1,2}-\d{2}[RP])\b")),  # 'NEMA L6-50R'
)
# A fixture or device tag leading the description: 'E3, ...', 'K6 - ...',
# 'TYPE A: ...'. Letters only up to 3 characters, or up to 6 with a digit.
_TAG_RE = re.compile(
    r"^(?:TYPE\s+)?([A-Z]{1,3}|(?=[A-Z0-9.-]*\d)[A-Z][A-Z0-9.-]{0,5})\s*(?:,|:|\s-\s)"
)
_POLE_WORDS = {"SINGLE": "1", "TWO": "2", "THREE": "3"}
# Device modifiers that make a different line item when only one side has them.
_MODIFIERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GFCI", re.compile(r"\bGFC?I\b")),
    ("WP", re.compile(r"\b(?:WP|WEATHER ?PROOF|WEATHER RESISTANT|WR)\b")),
    ("DUPLEX", re.compile(r"\bDUPLEX\b")),
    ("QUADPLEX", re.compile(r"\bQUAD(?:RU)?PLEX\b|\bFOURPLEX\b")),
    ("SWITCHED", re.compile(r"\bSWITCHED\b")),
    ("FLOOR", re.compile(r"\bFLOOR\b")),
    ("USB", re.compile(r"\bUSB\b")),
    ("ISOLATED", re.compile(r"\bISOLATED GROUND\b|\bIG\b")),
    ("PULL STRING", re.compile(r"\bPULL ?(?:STRING|ROPE|TAPE)\b|\bMUL[EL]? ?TAPE\b")),
    ("EMERGENCY", re.compile(r"\bEMERGENCY\b|\bBATTERY\b")),
)
_CONFLICT_PENALTY = {
    "tag": 0.3, "wire": 0.15, "kcmil": 0.15, "size": 0.15, "amps": 0.15, "poles": 0.15,
    "kva": 0.15, "conduit": 0.15, "strand": 0.15, "cores": 0.15, "config": 0.15,
    "fuse": 0.15, "main": 0.15, "panel": 0.3, "volts": 0.15, "nema": 0.1,
}


_VOLTAGE_RE = re.compile(r"\b(\d{3})/(\d{3})V?\b")


@lru_cache(maxsize=65536)
def key_tokens(norm: str) -> frozenset[tuple[str, str]]:
    """Identity tokens of a normalized description: wire and conduit sizes,
    amperage, poles, kVA, NEMA rating, conduit type, fixture tag and device
    modifiers (GFCI, WP, duplex / quadplex, ...)."""
    toks: set[tuple[str, str]] = set()
    for kind, pat in _KEY_PATTERNS:
        for m in pat.finditer(norm):
            toks.add((kind, m.group(1).replace(" ", "")))
    for m in _VOLTAGE_RE.finditer(norm):
        toks.add(("volts", "/".join(sorted((m.group(1), m.group(2))))))
    for word, n in _POLE_WORDS.items():
        if re.search(rf"\b{word}[\s-]POLE\b", norm):
            toks.add(("poles", n))
    for name, pat in _MODIFIERS:
        if pat.search(norm):
            toks.add(("mod", name))
    m = _TAG_RE.match(norm)
    if m:
        toks.add(("tag", m.group(1)))
    return frozenset(toks)


_SYNONYMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bCOPPER\b"), "CU"),
    (re.compile(r"\bALUMINUM\b"), "AL"),
    (re.compile(r"\bGFI\b"), "GFCI"),
    (re.compile(r"\bQUAD(?:RU)?PLEX\b"), "QUADPLEX"),
    (re.compile(r"\bWEATHER ?PROOF\b"), "WP"),
    (re.compile(r"\bW/"), "WITH "),
    (re.compile(r"\bCAT (\d)"), r"CAT\1"),
    (re.compile(r"\b(\d+/0|\d{1,2}) ?AWG\b"), r"#\1"),
    (re.compile(r"##"), "#"),
)


def _ratio_text(norm: str) -> str:
    for pat, rep in _SYNONYMS:
        norm = pat.sub(rep, norm)
    return re.sub(r"\s+", " ", norm).strip()


def similarity(a: BoqRow, b: BoqRow) -> float:
    """Score two rows in 0..1.

    Base: difflib ratio of the normalized descriptions (with a few synonyms
    folded: COPPER / CU, GFI / GFCI, 12 AWG / #12, ...). When either side has
    key tokens the score is 0.6 x ratio + 0.4 x key-token Jaccard (the key
    credit fades out below a ratio of 0.6 so a shared size alone cannot carry an
    unrelated row), then 0.15 off per identity kind that disagrees (0.3 for a
    fixture tag; wire size, conduit size, amps, poles, conduit type, ...) and per device modifier
    present on one side only. A same-unit bonus of 0.05 applies, and one side
    being under half the length of the other costs 0.1. Equal fixture tags in
    the same section and group score at least 0.75."""
    na, nb = normalize_description(a.description), normalize_description(b.description)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, _ratio_text(na), _ratio_text(nb)).ratio()
    ka, kb = key_tokens(na), key_tokens(nb)
    unit_bonus = 0.05 if normalize_unit(a.unit) == normalize_unit(b.unit) else 0.0
    length_penalty = 0.1 if min(len(na), len(nb)) < 0.5 * max(len(na), len(nb)) else 0.0
    if not ka and not kb:
        return max(0.0, min(1.0, ratio + unit_bonus - length_penalty))
    jacc = len(ka & kb) / len(ka | kb)
    score = 0.6 * ratio + 0.4 * jacc * min(1.0, ratio / 0.6) + unit_bonus - length_penalty
    kinds_a: dict[str, set[str]] = defaultdict(set)
    kinds_b: dict[str, set[str]] = defaultdict(set)
    for k, v in ka:
        kinds_a[k].add(v)
    for k, v in kb:
        kinds_b[k].add(v)
    for kind, penalty in _CONFLICT_PENALTY.items():
        if kinds_a.get(kind) and kinds_b.get(kind) and not (kinds_a[kind] & kinds_b[kind]):
            score -= penalty
    score -= 0.15 * len(kinds_a.get("mod", set()) ^ kinds_b.get("mod", set()))
    tags_a, tags_b = kinds_a.get("tag"), kinds_b.get("tag")
    if tags_a and tags_a == tags_b and (a.section, a.group) == (b.section, b.group):
        score = max(score, 0.75 + 0.25 * ratio)
    return max(0.0, min(1.0, score))


# Estimator workbook -----------------------------------------------------------

def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return float(s)
        if re.fullmatch(r"=[\d.+\-*/() ]+", s):  # cached value missing, simple math
            try:
                return float(eval(s[1:], {"__builtins__": {}}, {}))  # noqa: S307
            except Exception:  # noqa: BLE001
                return None
    return None


def _find_header_row(ws: Any) -> int | None:
    for r in range(1, min(ws.max_row, 30) + 1):
        if _clean_text(ws.cell(r, 2).value).upper() == "DESCRIPTION":
            return r
    return None


def parse_estimator_boq(xlsx_bytes: bytes) -> list[BoqRow]:
    """Walk every area sheet of an estimator BOQ and return its item rows with
    the heading hierarchy (section, group, subgroup) resolved."""
    raw_wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    val_wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    level0 = set(BOQ_LEVEL0)
    groups = set(BOQ_GROUPS)
    rows: list[BoqRow] = []
    for ws in raw_wb.worksheets:
        title = _clean_text(ws.title)
        header = _find_header_row(ws)
        if header is None or (title.upper() in _BLANK_SHEETS and ws.max_row <= 1):
            continue  # the blank 'Sheet1' / 'Chart1' and any non-BOQ sheet
        vws = val_wb[ws.title]
        section, group, subgroup = "NEW WORK", "", ""
        for r in range(header + 1, ws.max_row + 1):
            b_cell = ws.cell(r, 2)
            desc_raw = b_cell.value
            desc = _clean_text(desc_raw)
            if not desc:
                continue
            qty = _num(vws.cell(r, 3).value)
            if qty is None:
                qty = _num(ws.cell(r, 3).value)
            unit = normalize_unit(vws.cell(r, 4).value)
            name, _note = _heading_name(desc_raw)
            canon = _canonical(name)
            bold = bool(b_cell.font and b_cell.font.b)
            align = (b_cell.alignment.horizontal or "") if b_cell.alignment else ""
            in_vocab = canon in level0 or canon in groups or canon in _KNOWN_SUBGROUPS
            is_heading = (qty is None and (bold or in_vocab or (desc.isupper() and len(desc) <= 80)))
            # A vocabulary heading with a stray number in C (seen on
            # OTHER CONDUIT ACCESSORIES) is still a heading when it has no unit.
            if not is_heading and in_vocab and bold and not unit:
                is_heading = True
            if is_heading:
                if canon in level0:
                    section, group, subgroup = canon, "", ""
                elif canon in groups:
                    group, subgroup = canon, ""
                elif canon in _KNOWN_SUBGROUPS:
                    subgroup = name
                elif align == "center":
                    group, subgroup = name, ""
                elif align == "left" or group:
                    subgroup = name
                else:
                    group, subgroup = name, ""
                continue
            if qty is None:
                continue  # a note row
            a_raw = ws.cell(r, 1).value
            accessory = (
                align == "right"
                or _canonical(subgroup) == _ACCESSORY_SUBGROUP
                or (a_raw in (None, "") and bool(_ACCESSORY_WORDS.search(desc.upper())))
            )
            if not group and subgroup:
                csub = _canonical(subgroup)
                group = next((g for pre, g in _SUBGROUP_PARENT if csub.startswith(pre)), "")
            rows.append(
                BoqRow(
                    area=title,
                    section=section,
                    group=group or (section if section == "DEMOLITION" else ""),
                    subgroup=subgroup,
                    description=desc,
                    quantity=qty,
                    unit=unit,
                    is_accessory=accessory,
                    ref=f"{title}!B{r}",
                )
            )
    return rows


# AI spec ----------------------------------------------------------------------

def _boq_from(spec: dict[str, Any]) -> dict[str, Any]:
    if "areas" not in spec and isinstance(spec.get("boq"), dict):
        return spec["boq"]
    return spec


def rows_from_spec(boq_spec: dict[str, Any]) -> list[BoqRow]:
    """Flatten a BOQ_SPEC into rows, applying the renderer's wastage rule
    (quantity x 1.1 on FT rows with wastage_applies). Accepts the full main
    agent output too (uses its 'boq')."""
    spec = _boq_from(boq_spec)
    rows: list[BoqRow] = []
    for ai, area in enumerate(spec.get("areas") or []):
        area_name = _clean_text(area.get("area")) or "SHEET"
        for si, sec in enumerate(area.get("sections") or []):
            section = _canonical(_clean_text(sec.get("heading"))) or "NEW WORK"
            for gi, grp in enumerate(sec.get("groups") or []):
                gname = _canonical(_heading_name(grp.get("name"))[0])
                if section == "DEMOLITION" and gname not in BOQ_GROUPS:
                    gname = "DEMOLITION"  # the estimators list demolition rows ungrouped
                for ui, sub in enumerate(grp.get("subgroups") or []):
                    sname = _heading_name(sub.get("name"))[0]
                    for ii, item in enumerate(sub.get("items") or []):
                        qty = _num(item.get("quantity")) or 0.0
                        unit = normalize_unit(item.get("unit"))
                        if unit == "FT" and item.get("wastage_applies"):
                            qty = qty * WASTAGE_FACTOR
                        desc = _clean_text(item.get("description"))
                        caveat = _clean_text(item.get("caveat"))
                        if caveat and caveat.upper() not in desc.upper():
                            desc = f"{desc} {caveat}"
                        rows.append(
                            BoqRow(
                                area=area_name,
                                section=section,
                                group=gname,
                                subgroup=sname,
                                description=desc,
                                quantity=qty,
                                unit=unit,
                                is_accessory=bool(item.get("is_accessory")),
                                ref=f"areas[{ai}].sections[{si}].groups[{gi}]"
                                f".subgroups[{ui}].items[{ii}]",
                            )
                        )
    return rows


# Matching ---------------------------------------------------------------------

def _area_map(ai_rows: list[BoqRow], est_rows: list[BoqRow]) -> dict[str, str]:
    """AI area name -> estimator area name, or '' when areas are not comparable
    (either side has a single area, or no estimator area is close enough)."""
    ai_areas = sorted({r.area for r in ai_rows})
    est_areas = sorted({r.area for r in est_rows})
    if len(ai_areas) <= 1 or len(est_areas) <= 1:
        return {a: "" for a in ai_areas}
    out: dict[str, str] = {}
    for a in ai_areas:
        na = normalize_description(a)
        best, best_s = "", 0.0
        for e in est_areas:
            ne = normalize_description(e)
            s = SequenceMatcher(None, na, ne).ratio()
            if ne and (ne in na or na in ne):
                s = max(s, 0.9)
            if s > best_s:
                best, best_s = e, s
        out[a] = best if best_s >= 0.5 else ""
    return out


def _pct(ai_q: float, est_q: float) -> float | None:
    if est_q == 0:
        return None
    return abs(ai_q - est_q) / abs(est_q) * 100.0


def match_rows(
    ai_rows: list[BoqRow],
    est_rows: list[BoqRow],
    *,
    ignore_accessories: bool = True,
    threshold: float = MATCH_THRESHOLD,
) -> MatchReport:
    """Greedy one-to-one matching: first within the same area, section and
    group, then across groups within the same area. Each pass takes the best
    remaining pair at or above `threshold`."""
    ignored = {"ai": 0, "est": 0}
    if ignore_accessories:
        ignored = {
            "ai": sum(r.is_accessory for r in ai_rows),
            "est": sum(r.is_accessory for r in est_rows),
        }
        ai_rows = [r for r in ai_rows if not r.is_accessory]
        est_rows = [r for r in est_rows if not r.is_accessory]
    amap = _area_map(ai_rows, est_rows)
    est_multi = len({r.area for r in est_rows}) > 1 and any(amap.values())

    def ai_area(r: BoqRow) -> str:
        return amap.get(r.area, "")

    def est_area(r: BoqRow) -> str:
        return r.area if est_multi else ""

    scores: dict[tuple[int, int], float] = {}
    for i, a in enumerate(ai_rows):
        for j, e in enumerate(est_rows):
            s = similarity(a, e)
            if s >= threshold:
                scores[(i, j)] = s

    # Pass 1: same area, section and group. Pass 2: same area (rows of an AI
    # area that maps to no estimator area may match anywhere). Areas are only
    # compared when both sides have several and the names map.
    passes = (
        lambda a, e: ai_area(a) == est_area(e) and a.section == e.section and a.group == e.group,
        lambda a, e: ai_area(a) == est_area(e) or not ai_area(a),
    )
    used_ai: set[int] = set()
    used_est: set[int] = set()
    chosen: list[tuple[int, int, float]] = []
    for ok in passes:
        cands = sorted(
            ((s, i, j) for (i, j), s in scores.items()
             if i not in used_ai and j not in used_est and ok(ai_rows[i], est_rows[j])),
            key=lambda t: (-t[0], t[1], t[2]),
        )
        for s, i, j in cands:
            if i in used_ai or j in used_est:
                continue
            used_ai.add(i)
            used_est.add(j)
            chosen.append((i, j, s))

    pairs: list[MatchedPair] = []
    for i, j, s in sorted(chosen, key=lambda t: t[1]):
        a, e = ai_rows[i], est_rows[j]
        unit_mismatch = normalize_unit(a.unit) != normalize_unit(e.unit)
        pct = None if unit_mismatch else _pct(a.quantity, e.quantity)
        exact = (not unit_mismatch) and (
            abs(a.quantity - e.quantity) <= EXACT_TOLERANCE * abs(e.quantity)
            if e.quantity else a.quantity == 0
        )
        pairs.append(
            MatchedPair(
                ai=a,
                est=e,
                score=round(s, 4),
                abs_error=round(a.quantity - e.quantity, 4),
                pct_error=None if pct is None else round(pct, 2),
                unit_mismatch=unit_mismatch,
                group_mismatch=(a.section, a.group) != (e.section, e.group),
                exact=exact,
            )
        )

    mape_vals = [p.pct_error for p in pairs if p.pct_error is not None and p.est.quantity > 0]
    per_group: dict[str, dict[str, Any]] = {}
    est_by_group = Counter(r.group_key for r in est_rows)
    ai_by_group = Counter(r.group_key for r in ai_rows)
    est_matched = Counter(p.est.group_key for p in pairs)
    ai_matched = Counter(p.ai.group_key for p in pairs)
    for g in sorted(set(est_by_group) | set(ai_by_group)):
        per_group[g] = {
            "est_rows": est_by_group[g],
            "est_matched": est_matched[g],
            "recall": round(est_matched[g] / est_by_group[g], 4) if est_by_group[g] else None,
            "ai_rows": ai_by_group[g],
            "ai_matched": ai_matched[g],
            "precision": round(ai_matched[g] / ai_by_group[g], 4) if ai_by_group[g] else None,
        }

    return MatchReport(
        pairs=pairs,
        unmatched_ai=[r for i, r in enumerate(ai_rows) if i not in used_ai],
        unmatched_est=[r for j, r in enumerate(est_rows) if j not in used_est],
        ai_total=len(ai_rows),
        est_total=len(est_rows),
        precision=round(len(pairs) / len(ai_rows), 4) if ai_rows else 0.0,
        recall=round(len(pairs) / len(est_rows), 4) if est_rows else 0.0,
        mape=round(sum(mape_vals) / len(mape_vals), 2) if mape_vals else None,
        mape_rows=len(mape_vals),
        exact_matches=sum(p.exact for p in pairs),
        unit_mismatches=sum(p.unit_mismatch for p in pairs),
        group_mismatches=sum(p.group_mismatch for p in pairs),
        per_group=per_group,
        ignored_accessories=ignored,
        threshold=threshold,
    )


# Reports ----------------------------------------------------------------------

def report_json(report: MatchReport) -> dict[str, Any]:
    return {
        "threshold": report.threshold,
        "totals": {
            "ai_rows": report.ai_total,
            "est_rows": report.est_total,
            "matched": len(report.pairs),
            "precision": report.precision,
            "recall": report.recall,
            "mape": report.mape,
            "mape_rows": report.mape_rows,
            "exact_matches": report.exact_matches,
            "unit_mismatches": report.unit_mismatches,
            "group_mismatches": report.group_mismatches,
            "ignored_accessories": report.ignored_accessories,
        },
        "per_group": report.per_group,
        "pairs": [
            {
                "ai": asdict(p.ai),
                "est": asdict(p.est),
                "score": p.score,
                "abs_error": p.abs_error,
                "pct_error": p.pct_error,
                "unit_mismatch": p.unit_mismatch,
                "group_mismatch": p.group_mismatch,
                "exact": p.exact,
            }
            for p in report.pairs
        ],
        "unmatched_ai": [asdict(r) for r in report.unmatched_ai],
        "unmatched_est": [asdict(r) for r in report.unmatched_est],
    }


def _fmt_q(q: float) -> str:
    return f"{q:,.2f}".rstrip("0").rstrip(".")


def _pct_s(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _md_cell(s: str, limit: int = 70) -> str:
    s = s.replace("|", "/")
    return s if len(s) <= limit else s[: limit - 3] + "..."


def report_markdown(report: MatchReport) -> str:
    out: list[str] = ["# Plans Reader golden comparison", ""]
    out += [
        "| Metric | Value |",
        "|---|---|",
        f"| AI rows | {report.ai_total} |",
        f"| Estimator rows | {report.est_total} |",
        f"| Matched (score >= {report.threshold}) | {len(report.pairs)} |",
        f"| Precision | {_pct_s(report.precision)} |",
        f"| Recall | {_pct_s(report.recall)} |",
        "| MAPE (matched, same unit, est qty > 0) | "
        + ("n/a" if report.mape is None else f"{report.mape:.1f}% over {report.mape_rows} rows")
        + " |",
        f"| Exact quantity (within 2%) | {report.exact_matches} |",
        f"| Unit mismatches | {report.unit_mismatches} |",
        f"| Matched across groups | {report.group_mismatches} |",
        f"| Accessory rows ignored (AI / est) | {report.ignored_accessories.get('ai', 0)}"
        f" / {report.ignored_accessories.get('est', 0)} |",
        "",
        "## Per group",
        "",
        "| Group | Est rows | Recall | AI rows | Precision |",
        "|---|---|---|---|---|",
    ]
    for g, v in report.per_group.items():
        out.append(
            f"| {g} | {v['est_rows']} | {_pct_s(v['recall'])} | {v['ai_rows']} |"
            f" {_pct_s(v['precision'])} |"
        )
    out += ["", "## Matched rows", "", "| Estimator | AI | Est qty | AI qty | Err % | Flags |",
            "|---|---|---|---|---|---|"]
    for p in report.pairs:
        flags = []
        if p.unit_mismatch:
            flags.append(f"UNIT {p.est.unit} vs {p.ai.unit}")
        if p.group_mismatch:
            flags.append(f"GROUP {p.ai.group_key}")
        if p.exact:
            flags.append("exact")
        err = "n/a" if p.pct_error is None else f"{p.pct_error:.1f}"
        out.append(
            f"| {_md_cell(p.est.description)} | {_md_cell(p.ai.description)} |"
            f" {_fmt_q(p.est.quantity)} {p.est.unit} | {_fmt_q(p.ai.quantity)} {p.ai.unit} |"
            f" {err} | {', '.join(flags)} |"
        )
    out += ["", f"## Missed scope (unmatched estimator rows: {len(report.unmatched_est)})", ""]
    out += [
        f"- [{r.area} / {r.group_key}] {r.description} = {_fmt_q(r.quantity)} {r.unit}"
        for r in report.unmatched_est
    ] or ["- none"]
    out += ["", f"## Extra AI rows (hallucinated or over-split: {len(report.unmatched_ai)})", ""]
    out += [
        f"- [{r.area} / {r.group_key}] {r.description} = {_fmt_q(r.quantity)} {r.unit}"
        for r in report.unmatched_ai
    ] or ["- none"]
    return "\n".join(out) + "\n"


def structure_markdown(rows: list[BoqRow]) -> str:
    """Item counts per area and group of a parsed estimator BOQ."""
    out = ["| Area | Group | Items | Accessories |", "|---|---|---|---|"]
    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        counts[(r.area, r.group_key)][1 if r.is_accessory else 0] += 1
    for (area, g), (n, acc) in counts.items():
        out.append(f"| {area} | {g} | {n} | {acc} |")
    return "\n".join(out) + "\n"


# Backup lookup ----------------------------------------------------------------

def find_backup_boq(prefix: str, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path:
    """The estimator `BOQ - COUNTS.xlsx` of the project whose id starts with
    `prefix` in the production backup (latest round, latest upload)."""
    rows = json.loads((backup_dir / "data" / "project_files.json").read_text())
    cands = [
        r for r in rows
        if r.get("category") == "boq"
        and str(r.get("project_id", "")).startswith(prefix)
        and str(r.get("filename", "")).lower().endswith((".xlsx", ".xlsm"))
    ]
    if not cands:
        raise FileNotFoundError(f"no BOQ xlsx for project prefix {prefix!r} in {backup_dir}")
    if len({r["project_id"] for r in cands}) > 1:
        raise ValueError(f"project prefix {prefix!r} is ambiguous")
    cands.sort(
        key=lambda r: (
            "COUNTS" in str(r.get("filename", "")).upper(),
            r.get("submission_round") or 0,
            r.get("created_at") or "",
        )
    )
    path = backup_dir / "storage" / "project-files" / cands[-1]["storage_path"]
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path


# CLI --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score an AI BOQ spec against an estimator BOQ.")
    ap.add_argument("--spec", type=Path, help="BOQ_SPEC JSON (or a main agent output with 'boq')")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--estimator", type=Path, help="the estimator's BOQ - COUNTS.xlsx")
    src.add_argument("--from-backup", metavar="PROJECT_ID_PREFIX",
                     help="locate the estimator BOQ in the production backup")
    ap.add_argument("--backup-dir", type=Path,
                    default=Path(os.environ.get("BDR_BACKUP_DIR", DEFAULT_BACKUP_DIR)))
    ap.add_argument("--json", type=Path, help="also write the JSON report here")
    ap.add_argument("--include-accessories", action="store_true",
                    help="score generated accessory rows too")
    ap.add_argument("--threshold", type=float, default=MATCH_THRESHOLD)
    args = ap.parse_args(argv)

    est_path = args.estimator or find_backup_boq(args.from_backup, args.backup_dir)
    est_rows = parse_estimator_boq(est_path.read_bytes())
    if args.spec is None:
        print(f"# Estimator BOQ structure: {est_path}\n")
        print(structure_markdown(est_rows))
        return 0
    spec = json.loads(args.spec.read_text())
    report = match_rows(
        rows_from_spec(spec), est_rows,
        ignore_accessories=not args.include_accessories, threshold=args.threshold,
    )
    print(report_markdown(report))
    if args.json:
        args.json.write_text(json.dumps(report_json(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
