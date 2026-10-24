"""Electrical item identity: the ONE module for BOQ item normalization,
key tokens, row similarity, item classes and units of measure
(docs/AI_ESTIMATING_BUILD_SPEC.md section 5.1).

The first half (BoqRow, normalize_*, key_tokens, similarity and their
constants) moved here verbatim from scripts/plans_reader_golden.py, which
re-exports them so its golden tests are unchanged. The second half adds the
identity layer the pricing engine, the consolidator and the scorer share:
parse_item -> ItemIdentity, hard_conflict, canon_uom, convert_unit_value,
count_key, item_class and LINEAR_CLASSES.

This module imports nothing from scripts/ and never touches the database.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any

from app.services.plans_reader_schemas import (
    BOQ_GROUPS,
    BOQ_LEVEL0,
    BOQ_SUBGROUPS,
    UNITS,
)

MATCH_THRESHOLD = 0.72
EXACT_TOLERANCE = 0.02
WASTAGE_FACTOR = 1.1

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
    ("GFCI", re.compile(r"(?<!\bNON-)(?<!\bNON )(?<!\bNON)\bGFC?I\b", re.I)),
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




# Item identity ----------------------------------------------------------------
#
# Everything below is deterministic text classification over the normalized
# description (normalize_description) plus the unit of measure. No model.

ITEM_CLASSES: tuple[str, ...] = (
    "conductor", "conduit", "cable", "raceway", "trench", "sawcut", "lighting_track",
    "luminaire", "luminaire_linear", "receptacle", "switch", "control", "breaker",
    "panel", "equipment_connection", "demolition", "allowance", "other",
)
# Classes that are always measured in feet: an EA row of one of these blocks
# (gate linear_in_ea). This is the ONE definition (spec 5.1); the contracts
# module (estimating_contracts) and plans_reader_tools import it from here.
LINEAR_CLASSES: frozenset[str] = frozenset(
    {"conductor", "conduit", "cable", "raceway", "trench", "sawcut", "lighting_track"}
)
# Linear luminaires are FT only on evidence (fixture schedule per foot, a
# vendor quote priced per foot, or a measured run); otherwise EA with a warning.
# The words are the 5.1 vocabulary; LINEAR_LUMINAIRE_RE is their matcher over a
# normalized (uppercase) description and accepts UNDER CABINET and UNDER-CABINET.
LINEAR_LUMINAIRE_WORDS: tuple[str, ...] = ("undercabinet", "strip", "tape", "linear", "cove")
LINEAR_LUMINAIRE_RE = re.compile(
    r"\bUNDER[\s-]?CABINET\b|\bSTRIP\b|\bTAPE\b|\bLINEAR\b|\bCOVE\b"
)
assert all(LINEAR_LUMINAIRE_RE.search(w.upper()) for w in LINEAR_LUMINAIRE_WORDS)

# Canonical units. normalize_unit folds the estimator spellings into UNITS;
# the identity layer adds C (hundred) and M (thousand) for priced feet.
CANON_UOMS: tuple[str, ...] = ("EA", "FT", "C", "M", "CU FT", "CY", "SQ FT", "LS")
_CANON_ALIASES: dict[str, str] = {
    **_UNIT_ALIASES,
    "LIN FT": "FT", "LINFT": "FT", "LIN": "FT", "L F": "FT", "LNFT": "FT",
    "C": "C", "CLF": "C", "CFT": "C", "HUNDRED": "C", "PER C": "C",
    "M": "M", "MFT": "M", "MLF": "M", "THOUSAND": "M", "PER M": "M",
    "EACH": "EA", "UNIT": "EA", "UNITS": "EA",
    "CUFT": "CU FT", "CU FT": "CU FT",
    "SQ FT": "SQ FT", "SQFT": "SQ FT",
    "CY": "CY", "CU YD": "CY", "CUYD": "CY", "CUBIC YARD": "CY", "CUBIC YARDS": "CY",
}
# _UNIT_ALIASES maps CFT to CU FT (an estimator spelling of cubic feet); the
# identity layer keeps that so a workbook CFT stays volume, never hundred feet.
_CANON_ALIASES["CFT"] = "CU FT"
assert set(_CANON_ALIASES.values()) <= set(CANON_UOMS)
# Feet per unit for the length family; EA and the rest never convert.
_FEET_PER: dict[str, float] = {"FT": 1.0, "C": 100.0, "M": 1000.0}
_UOM_DIMENSION: dict[str, str] = {
    "EA": "count", "FT": "length", "C": "length", "M": "length",
    "CU FT": "volume", "CY": "volume", "SQ FT": "area", "LS": "lump",
}


def canon_uom(unit: Any) -> str:
    """Canonical unit of measure: one of CANON_UOMS, or '' for a blank unit.

    LF, LFT, LIN FT and FEET canonicalize to FT; C is hundred and M thousand
    (feet); CU FT / CY are volume; SQ FT area; LS lump sum. An unknown unit
    raises ValueError so a caller never prices a row on a guessed basis."""
    key = normalize_unit(unit)
    if not key:
        return ""
    canon = _CANON_ALIASES.get(key)
    if canon is None:
        raise ValueError(f"unknown unit of measure: {unit!r}")
    return canon


def uom_dimension(uom: str) -> str:
    """'count' | 'length' | 'volume' | 'area' | 'lump' | '' for a canonical uom."""
    return _UOM_DIMENSION.get(canon_uom(uom), "")


def convert_unit_value(value: float, from_uom: Any, to_uom: Any) -> float:
    """Convert a quantity between compatible units (FT, LF, C, M). Same unit
    returns the value unchanged; anything else (EA to FT, CY to FT, ...)
    raises ValueError."""
    src, dst = canon_uom(from_uom), canon_uom(to_uom)
    if not src or not dst:
        raise ValueError(f"cannot convert with a blank unit: {from_uom!r} -> {to_uom!r}")
    if src == dst:
        return value
    if src in _FEET_PER and dst in _FEET_PER:
        return value * _FEET_PER[src] / _FEET_PER[dst]
    raise ValueError(f"incompatible units: {src} -> {dst}")


@dataclass
class ItemIdentity:
    """What a BOQ line IS, independent of its wording and quantity.

    item_key groups equal items (count lines, cost library rows); family is
    the class plus its subtype ('conductor:thhn', 'conduit:emt',
    'receptacle:duplex', 'luminaire:L5'); attrs hold the identity attributes
    that were found (conductor_size, conduit_size, conduit_type, material,
    insulation, grade, amps, poles, volts, tag, ...); modifiers are the device
    modifiers of key_tokens (GFCI, WP, DUPLEX, ...). uom is the canonical unit
    or '' when the unit was blank or unknown."""

    item_key: str
    family: str
    item_class: str
    uom: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    modifiers: tuple[str, ...] = ()
    norm: str = ""


_DEMO_RE = re.compile(r"\b(?:REMOVAL|REMOVE|DEMO|DEMOLITION|DEMOLISH)\b")
_DEMO_STRIP_RE = re.compile(
    r"^(?:REMOVAL|REMOVE|DEMO|DEMOLITION|DEMOLISH)\b(?:\s+OF)?(?:\s+(?:THE|ALL))?"
    r"(?:\s+EXISTING|\s+\(E\))?\s*"
)
# A demolition description names what is removed first; a clause after W/,
# WITH, &, AND, INCLUDING, PLUS or a comma says what comes out with it
# ("REMOVAL OF POWER DEVICES W/ BACKBOX & ASSOCIATE WIRE, CONDUIT BACK TO
# SOURCE"). The estimator prices such a row by the each of the devices
# removed (remove-to-source), so the primary noun decides the subject and
# a trailing wire or conduit clause never makes it linear. Conductor
# demolition is linear only when the primary noun itself is wire, cable,
# conductor, conduit, raceway or track.
_DEMO_TRAILING_RE = re.compile(r"\s*(?:\bW/|\bWITH\b|&|\bAND\b|\bINCLUDING\b|\bINCL\b|\bPLUS\b|[,;:])")
_DEMO_DEVICE_NOUN_RE = re.compile(r"\bDEVICES?\b|\bEQUIPMENT\b|\bASSEMBL(?:Y|IES)\b|\bUNITS?\b|\bGEAR\b")
_DEMO_DEVICE_CLASSES: frozenset[str] = frozenset(
    {"receptacle", "switch", "control", "luminaire", "luminaire_linear", "breaker", "panel", "equipment_connection"}
)
_ALLOWANCE_RE = re.compile(r"\bALLOWANCE\b|\bLUMP SUM\b")
_ACCESSORY_RE = re.compile(
    r"\b(?:BOX|BOXES|J-?BOX|FITTING|FITTINGS|HANGER|SUPPORT|CLAMP|SLEEVE|NIPPLE|LB|BODY|"
    r"CONDULET|JUNCTION|COVER|PLATE|RING|BUSHING|REDUCER|UNISTRUT|STRUT|ROD)\b"
)
_TRENCH_RE = re.compile(
    r"\bTRENCH(?:ING|ES)?\b|\bEXCAVAT(?:E|ION|ING)\b|\bBACKFILL\b|\bBOR(?:E|ING)\b|"
    r"\bDIRECTIONAL\b|\bDUCT ?BANK\b"
)
_SAWCUT_RE = re.compile(r"\bSAW[\s-]?CUT(?:TING)?\b")
_TRACK_RE = re.compile(r"\bTRACK\b")
_TRACK_HEAD_RE = re.compile(r"\bHEADS?\b")
_RACEWAY_RE = re.compile(
    r"\bWIREMOLD\b|\bRACEWAY\b|\bCABLE TRAY\b|\bTRAY\b|\bWIREWAY\b|\bGUTTER\b|"
    r"\bINNERDUCT\b|\bLADDER RACK\b"
)
_CABLE_RE = re.compile(
    r"\bCABLE\b|\bROMEX\b|\bNM-?B?\b|\bCORD\b|\bFIBER\b|\bUF-?B?\b|\bTC\b|"
    r"\bMC\b|\bAC\b\s*\d+/\d"
)
_CONDUCTOR_WORDS_RE = re.compile(
    r"\bTHHN\b|\bTHWN\b|\bTHW\b|\bXHHW\b|\bUSE-?2?\b|\bRHH\b|\bRHW\b|\bBARE\b|"
    r"\bWIRE\b|\bCONDUCTOR\b|\bGEC\b"
)
_CONDUIT_RE = re.compile(
    r'\d"C\b|\bCONDUIT\b|\bFLEX\b|\bSEAL-?TIGHT\b|\bSEALTITE\b|\bLIQUID-?TIGHT\b|\bSCH\s?\d+\b'
)
# LIGHTING CONTROL is read in the singular only, hyphenated or not: the
# readers count "lighting-control symbols" (7172 E2.00), while the plural
# "LIGHTING CONTROLS" in the sent BOQs names a system inside demolition and
# disconnect lines ("REMOVAL OF LIGHT FIXTURE & LIGHTING CONTROLS",
# "DISCONNECT EXISTING LIGHTING CONTROLS FOR LIGHT FIXTURE") whose other
# noun is the item the estimator priced. The cost library and the LOPO
# golden band classify those lines through parse_item, so the plural keeps
# the class it had before the reader vocabulary widened (the device naming
# path, _DEV_LIGHTING_CONTROL_RE, still accepts the plural).
_CONTROL_RE = re.compile(
    r"\bSENSOR\b|\bOCCUPANCY\b|\bVACANCY\b|\bPHOTOCELL\b|\bPHOTO CELL\b|\bTIME ?CLOCK\b|"
    r"\bCONTACTOR\b|\bRELAY\b|\bLIGHTING[\s-]CONTROL\b|\bCONTROL PANEL\b|\bTHERMOSTAT\b|"
    r"\bDIMMING\b|\bDALI\b|\b0-10V\b|\bKEYPAD\b|\bDAYLIGHT\b|\bCONTROLLER\b|\bPOWER PACK\b"
)
_BREAKER_RE = re.compile(r"\bBREAKER\b|\bBKR\b|\bCB\b|\bFUSES?\b")
_PANEL_RE = re.compile(
    r"\bPANEL(?:BOARD)?\b|\bSWITCHBOARD\b|\bSWITCHGEAR\b|\bMCC\b|\bLOAD CENTER\b|"
    r"\bTRANSFORMER\b|\bXFMR\b|\bDISCONNECT\b|\bSAFETY SWITCH\b|\bTRANSFER SWITCH\b|"
    r"\bATS\b|\bMETER\b|\bGENERATOR\b|\bCT CABINET\b|\bDISTRIBUTION\b"
)
_PANEL_FAMILY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("transformer", re.compile(r"\bTRANSFORMER\b|\bXFMR\b")),
    ("disconnect", re.compile(r"\bDISCONNECT\b|\bSAFETY SWITCH\b")),
    ("ats", re.compile(r"\bTRANSFER SWITCH\b|\bATS\b")),
    ("generator", re.compile(r"\bGENERATOR\b")),
    ("meter", re.compile(r"\bMETER\b|\bCT CABINET\b")),
    ("switchboard", re.compile(r"\bSWITCHBOARD\b|\bSWITCHGEAR\b|\bDISTRIBUTION\b")),
    ("mcc", re.compile(r"\bMCC\b")),
)
_EQUIPMENT_RE = re.compile(
    r"\bCONNECTIONS?\b|\bCONNECT\b|\bHOOK-?UPS?\b|\bWHIPS?\b|\bPOWER TO\b|\bFEED TO\b"
)
# A disconnect and reconnect of equipment that is already wired ("ELECTRICAL
# CONNECTIONS WOULD BE DISCONNECTED AND RECONNECTED AFTER UNITS ARE SET") is
# not a new equipment connection: the line keeps its own wording (other), as
# it did before the plural CONNECTIONS joined _EQUIPMENT_RE, so the cost
# library never pools its hours with ELECTRICAL CONNECTION.
_RECONNECT_RE = re.compile(r"\bRE-?CONNECT\w*\b")
_LOW_VOLTAGE_OUTLET_RE = re.compile(r"\b(?:DATA|TELEPHONE|TEL|PHONE|TV|CATV|AV|COMM|VOICE|LOW[\s-]?VOLTAGE)\b")
# A conductor count in a gear description ("3-PHASE, 4-WIRE", "3W") names the
# system, not a conductor item: it is blanked before the conductor words are
# read when the text names gear (7172: "New panel A, 200A, 277/480V, 3-phase,
# 4-wire, 42 circuits" classed as a conductor and was refused in EA).
_WIRE_COUNT_RE = re.compile(r"\b\d[\s-]?WIRES?\b")
_RECEPTACLE_RE = re.compile(
    r"\bRECEPTACLES?\b|\bRECEPT\b|\bOUTLETS?\b|\bDUPLEX\b|\bQUAD(?:RU)?PLEX\b|\bGFC?I\b|\bUSB\b"
)
_SWITCH_RE = re.compile(r"\bSWITCH(?:ES)?\b|\bDIMMERS?\b|\b[34]-WAY\b|\bTOGGLE\b")
_SWITCH_FAMILY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dimmer", re.compile(r"\bDIMMERS?\b")),
    ("3-way", re.compile(r"\b3-WAY\b")),
    ("4-way", re.compile(r"\b4-WAY\b")),
)
_LUMINAIRE_RE = re.compile(
    r"\bFIXTURES?\b|\bLUMINAIRES?\b|\bLIGHTS?\b|\bLIGHTING\b|\bLED\b|\bLAMPS?\b|"
    r"\bDOWNLIGHT\b|\bHIGH ?BAY\b|\bTROFFER\b|\bSCONCE\b|\bPENDANT\b|\bEXIT\b|"
    r"\bWALL ?PACK\b|\bBOLLARD\b|\bFLOOD\b|\bCANOPY\b|\bBATTERY PACK\b|\bEM PACK\b|"
    r"\bTRACK HEADS?\b"
)
_LUMINAIRE_TAG_RE = re.compile(r"^([A-Z]{1,2}\d{1,2}[A-Z]?)\b(?![\"'/#-])")
_MATERIAL_RE = re.compile(r"\b(CU|COPPER|AL|ALUMINUM|ALUMINIUM)\b")
_INSULATION_RE = re.compile(r"\b(THHN|THWN|THW|XHHW|USE-?2?|RHH|RHW|BARE)\b")
_GRADE_RE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hospital", re.compile(r"\bHOSPITAL GRADE\b|\bHG\b")),
    ("spec", re.compile(r"\bSPEC(?:IFICATION)? GRADE\b|\bSG\b")),
    ("commercial", re.compile(r"\bCOMMERCIAL GRADE\b")),
    ("industrial", re.compile(r"\bINDUSTRIAL GRADE\b")),
    ("residential", re.compile(r"\bRESIDENTIAL GRADE\b")),
    ("heavy_duty", re.compile(r"\bHEAVY[\s-]?DUTY\b|\bHD\b")),
)
_FORM_RE = re.compile(r"\b(\d)\s?X\s?(\d)\b")
_LENGTH_FT_RE = re.compile(r"\b(\d+)'(?![\w\"])")
_SLUG_RE = re.compile(r"[^A-Z0-9]+")


def _slug(norm: str, limit: int = 48) -> str:
    return _SLUG_RE.sub("_", norm).strip("_")[:limit].rstrip("_").lower()


def _linear_luminaire_kind(norm: str) -> str:
    m = LINEAR_LUMINAIRE_RE.search(norm)
    if not m:
        return ""
    word = m.group(0).replace(" ", "").replace("-", "")
    return word.lower()


def _classify(norm: str, toks: dict[str, set[str]], *, allow_demolition: bool) -> tuple[str, str]:
    """(item_class, family) of a normalized description. Order matters: the
    first rule that fires wins, so accessories (EA) are taken before the
    linear classes their words could otherwise land in."""
    if _ALLOWANCE_RE.search(norm):
        return "allowance", "allowance"
    if allow_demolition and _DEMO_RE.search(norm):
        rest = _DEMO_STRIP_RE.sub("", norm, count=1) if _DEMO_STRIP_RE.match(norm) else norm
        subject, _fam = _classify(rest, key_kinds(rest), allow_demolition=False)
        if subject in LINEAR_CLASSES:
            subject = _demo_primary_subject(rest, subject)
        return "demolition", f"demolition:{subject}"
    if _ACCESSORY_WORDS.search(norm) or _ACCESSORY_RE.search(norm):
        return "other", "other:accessory"
    if _TRENCH_RE.search(norm) and not _CONDUIT_RE.search(norm):
        return "trench", "trench"
    if _SAWCUT_RE.search(norm):
        return "sawcut", "sawcut"
    if _TRACK_RE.search(norm) and not _TRACK_HEAD_RE.search(norm) and "TRAY" not in norm:
        return "lighting_track", "lighting_track"
    if _RACEWAY_RE.search(norm):
        return "raceway", "raceway"
    if toks.get("cable") or toks.get("cores") or _CABLE_RE.search(norm):
        sub = next(iter(sorted(toks.get("cable", ()))), "")
        if not sub:
            m = re.search(r"\bMC\b|\bROMEX\b|\bNM-?B?\b|\bCONTROL\b|\bFIBER\b|\bCORD\b", norm)
            sub = m.group(0).replace("-", "") if m else "cable"
        return "cable", f"cable:{sub.lower()}"
    conductor_text = _WIRE_COUNT_RE.sub(" ", norm) if _PANEL_RE.search(norm) else norm
    if toks.get("wire") or toks.get("kcmil") or _CONDUCTOR_WORDS_RE.search(conductor_text):
        ins = _INSULATION_RE.search(norm)
        return "conductor", f"conductor:{ins.group(1).lower() if ins else 'wire'}"
    if toks.get("conduit") or _CONDUIT_RE.search(norm):
        ctype = next(iter(sorted(toks.get("conduit", ()))), "")
        return "conduit", f"conduit:{ctype.lower() or 'conduit'}"
    if _CONTROL_RE.search(norm):
        return "control", "control"
    if _BREAKER_RE.search(norm):
        return "breaker", "breaker"
    if _PANEL_RE.search(norm):
        fam = next((name for name, pat in _PANEL_FAMILY if pat.search(norm)), "panel")
        return "panel", f"panel:{fam}"
    if _EQUIPMENT_RE.search(norm) and not _RECONNECT_RE.search(norm):
        return "equipment_connection", "equipment_connection"
    if _RECEPTACLE_RE.search(norm):
        if re.search(r"\bOUTLETS?\b", norm) and _LOW_VOLTAGE_OUTLET_RE.search(norm):
            return "other", "other:low_voltage_outlet"
        if "QUADPLEX" in toks.get("mod", set()):
            return "receptacle", "receptacle:quadplex"
        if "DUPLEX" in toks.get("mod", set()):
            return "receptacle", "receptacle:duplex"
        return "receptacle", "receptacle"
    if _SWITCH_RE.search(norm):
        fam = next((name for name, pat in _SWITCH_FAMILY if pat.search(norm)), "switch")
        return "switch", f"switch:{fam}" if fam != "switch" else "switch"
    if _LUMINAIRE_RE.search(norm) or _TAG_RE.match(norm):
        kind = _linear_luminaire_kind(norm)
        if kind:
            return "luminaire_linear", f"luminaire_linear:{kind}"
        return "luminaire", "luminaire"
    return "other", f"other:{_slug(norm) or 'blank'}"


def _demo_primary_subject(rest: str, subject: str) -> str:
    """The subject of a demolition row whose whole text classes as linear:
    the class of the primary noun (the text before the first trailing
    clause) when that noun is a device, fixture, piece of gear or a plain
    DEVICES / EQUIPMENT / ASSEMBLY word; else `subject` unchanged."""
    head = _DEMO_TRAILING_RE.split(rest, maxsplit=1)[0].strip()
    if not head or head == rest.strip():
        return subject
    head_cls, _fam = _classify(head, key_kinds(head), allow_demolition=False)
    if head_cls in _DEMO_DEVICE_CLASSES:
        return head_cls
    if head_cls == "other" and _DEMO_DEVICE_NOUN_RE.search(head):
        return "other"
    return subject


def key_kinds(norm: str) -> dict[str, set[str]]:
    """key_tokens grouped by kind: {'wire': {'12'}, 'mod': {'GFCI', 'WP'}}."""
    kinds: dict[str, set[str]] = defaultdict(set)
    for kind, value in key_tokens(norm):
        kinds[kind].add(value)
    return kinds


def parse_item(description: Any, unit: Any = "") -> ItemIdentity:
    """Deterministic identity of a BOQ line from its description and unit.

    The description is normalized the way the scorer normalizes it, the key
    tokens are read off it, the class is decided by word rules (see
    _classify) and the attributes are filled from the tokens. The same text
    always gives the same ItemIdentity."""
    norm = normalize_description(description)
    toks = key_kinds(norm)
    try:
        uom = canon_uom(unit)
    except ValueError:
        uom = ""
    cls, family = _classify(norm, toks, allow_demolition=True)
    attrs: dict[str, str] = {}

    def first(kind: str) -> str:
        values = toks.get(kind)
        return sorted(values)[0] if values else ""

    if toks.get("kcmil"):
        attrs["conductor_size"] = f"{first('kcmil')}KCMIL"
    elif toks.get("wire"):
        attrs["conductor_size"] = first("wire")
    size = first("size")
    if size:
        attrs["conduit_size" if cls == "conduit" else "size"] = size
    if toks.get("conduit"):
        attrs["conduit_type"] = first("conduit")
    for kind in ("amps", "poles", "kva", "nema", "fuse", "main", "panel", "strand",
                 "cable", "cores", "config", "volts", "tag"):
        value = first(kind)
        if value:
            attrs[kind] = value
    m = _MATERIAL_RE.search(norm)
    if m:
        attrs["material"] = {"COPPER": "CU", "ALUMINUM": "AL", "ALUMINIUM": "AL"}.get(
            m.group(1), m.group(1)
        )
    insulations = sorted({i.group(1) for i in _INSULATION_RE.finditer(norm)})
    if insulations:
        attrs["insulation"] = "/".join(insulations)
    for grade, pat in _GRADE_RE:
        if pat.search(norm):
            attrs["grade"] = grade
            break
    fm = _FORM_RE.search(norm)
    if fm:
        attrs["form"] = f"{fm.group(1)}X{fm.group(2)}"
    lm = _LENGTH_FT_RE.search(norm)
    if lm and cls in ("luminaire", "luminaire_linear", "lighting_track"):
        attrs["length_ft"] = lm.group(1)
    if cls in ("luminaire", "luminaire_linear") and "tag" not in attrs:
        tm = _LUMINAIRE_TAG_RE.match(norm)
        if tm:
            attrs["tag"] = tm.group(1)
    if cls == "demolition":
        subject = family.split(":", 1)[1]
        attrs["subject"] = subject
        attrs["linear"] = "1" if subject in LINEAR_CLASSES else "0"
    if cls in ("other", "luminaire", "control", "equipment_connection", "panel") and (
        "tag" not in attrs
    ):
        attrs["desc"] = _slug(norm)
    modifiers = tuple(sorted(toks.get("mod", ())))
    parts = [family]
    parts.extend(f"{k}={v}" for k, v in sorted(attrs.items()))
    parts.extend(f"mod={m}" for m in modifiers)
    return ItemIdentity(
        item_key="|".join(parts),
        family=family,
        item_class=cls,
        uom=uom,
        attrs=attrs,
        modifiers=modifiers,
        norm=norm,
    )


def item_class(identity: ItemIdentity) -> str:
    """The coarse class of an identity, one of ITEM_CLASSES."""
    return identity.item_class


def is_linear(identity: ItemIdentity) -> bool:
    """True when the item is always measured in feet: a LINEAR_CLASSES item or
    the demolition of one (attrs linear = '1')."""
    if identity.item_class in LINEAR_CLASSES:
        return True
    return identity.item_class == "demolition" and identity.attrs.get("linear") == "1"


def linear_evidence_required(identity: ItemIdentity) -> bool:
    """True for linear luminaires (undercabinet, strip, tape, linear, cove):
    FT only when the fixture schedule, a vendor quote or a measurement says
    so; EA is allowed with a warning otherwise."""
    return identity.item_class == "luminaire_linear"


def count_key(identity: ItemIdentity) -> str:
    """The key count lines group on. Tagged fixtures group by class and tag
    (the wording of the schedule row does not split a count); everything
    else groups on its item_key."""
    tag = identity.attrs.get("tag")
    if tag and identity.item_class in ("luminaire", "luminaire_linear", "lighting_track"):
        parts = [f"{identity.item_class}|tag={tag}"]
        parts.extend(f"mod={m}" for m in identity.modifiers)
        return "|".join(parts)
    return identity.item_key


LUMINAIRE_CLASSES: frozenset[str] = frozenset({"luminaire", "luminaire_linear", "lighting_track"})
# Words after a fixture tag that mark the life-safety (EM) or night-light
# (NL) variant of a schedule tag: the scorer's GLOBAL_ALIASES plus SHADED
# (the common legend convention "shaded luminaire = life safety branch",
# 7154 E0.00). "UNSHADED" never matches (word boundary).
_EM_WORDS_RE = re.compile(r"\b(?:EM|EMERGENCY|LIFE[\s-]?SAFETY|BATTERY|EGRESS|SHADED)\b")
_NL_WORDS_RE = re.compile(r"\b(?:NL|NIGHT[\s-]?LIGHT)\b")
_TAG_SUFFIX_RE = re.compile(r"-(EM|NL)$")


def luminaire_variant(text: Any) -> str | None:
    """EM or NL when the text carries a variant word, else None."""
    norm = normalize_description(text)
    if _EM_WORDS_RE.search(norm):
        return "EM"
    if _NL_WORDS_RE.search(norm):
        return "NL"
    return None


def luminaire_tag_parts(tag: Any) -> tuple[str, str | None]:
    """("L1", "EM") for L1-EM, ("L1", None) for L1."""
    t = str(tag or "").strip().upper()
    m = _TAG_SUFFIX_RE.search(t)
    if m:
        return t[: m.start()], m.group(1)
    return t, None


def identity_matches(a: ItemIdentity, b: ItemIdentity) -> bool:
    """True when two identities name the same item for a line that cites an
    app-computed row: the same class, and the attribute that identifies the
    class (demolition subject, luminaire tag, conductor size, conduit size
    and type, cable size and cores) when both sides carry it."""
    if a.item_class != b.item_class:
        return False
    if a.item_class == "demolition":
        return a.attrs.get("subject") == b.attrs.get("subject")
    keys: tuple[str, ...]
    if a.item_class in LUMINAIRE_CLASSES:
        keys = ("tag",)
    elif a.item_class == "conductor":
        keys = ("conductor_size",)
    elif a.item_class == "conduit":
        keys = ("conduit_size", "conduit_type")
    elif a.item_class == "cable":
        keys = ("cable", "cores", "conductor_size")
    else:
        return True
    for key in keys:
        va, vb = a.attrs.get(key), b.attrs.get(key)
        if (va or vb) and va != vb:
            return False
    return True


# Estimator device vocabulary (spec 3.4) ------------------------------------------
#
# The estimators name standard devices with a fixed vocabulary (DIMMER SWITCH,
# DIMMER & OCCUPANCY SENSOR SWITCH, DUPLEX RECEPTACLE, TR, ...). The reader's
# wording scores as a different item: on 7154 run 2 "WALL DIMMER, INCLUDING
# OCCUPANCY-SENSOR OVERRIDE DIMMERS" 11 EA and "WALL OCCUPANCY-SENSING DIMMER"
# 2 EA both read as dimmer and sensor switches (13 against a truth of 2, and
# 0 against DIMMER SWITCH 10). parse_device decides the device family and
# its priced modifiers from text alone, deterministically, and
# device_description composes the estimator's name. Text that is not one of
# the standard devices (special receptacles, dimming panels, a cover on its
# own, demolition, thermostats, plain switches) gives None, so the caller
# keeps the model's wording. parse_item is untouched: it classes an
# occupancy-controlled receptacle as a control and a lockable cover as an
# accessory, which is right for identity matching but not for naming.

DEVICE_VOCABULARY: tuple[str, ...] = (
    "DIMMER SWITCH",
    "DIMMER & OCCUPANCY SENSOR SWITCH",
    "DIMMER SWITCH W/ LOCKABLE COVER",
    "OCCUPANCY SENSOR SWITCH",
    "OCCUPANCY SENSOR",
    "DUPLEX RECEPTACLE",
    "DUPLEX RECEPTACLE, GFCI",
    "DUPLEX RECEPTACLE, TR",
    "DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY",
    "QUADPLEX RECEPTACLE",
    "QUADPLEX RECEPTACLE CONTROLLED BY OCCUPANCY",
    "ELECTRICAL CONNECTION",
    "FLOOR BOX RECEPTACLE",
    "TELE/DATA OUTLET",
    "TV OUTLET",
)
DEVICE_FAMILIES: tuple[str, ...] = (
    "dimmer", "occupancy_sensor", "receptacle:duplex", "receptacle:quadplex", "receptacle:floor", "connection",
    "lv_outlet:data", "lv_outlet:tv",
)
# Priced modifiers a device name carries (the scorer's receptacle and switch
# attrs): OCC and LOCK on dimmers, WALL on sensors, GFCI, TR and CTRL on
# receptacles. Unpriced modifiers (EMERGENCY, NORMAL, colors, 125V, 20A,
# hospital grade, locations) never reach the name.
DEVICE_MODIFIERS: tuple[str, ...] = ("OCC", "LOCK", "WALL", "GFCI", "TR", "CTRL")

# An exclusion clause names what the row does NOT count ("Wall dimmer,
# excluding occupancy-integral and lockable-cover dimmers"): everything
# after it is dropped before the modifiers are read.
_DEV_EXCLUDE_CLAUSE_RE = re.compile(
    r"\b(?:EXCLUDING|EXCLUDES?|EXCEPT(?:ING)?|WITHOUT|W/O|NOT|NO|OTHER THAN)\b.*$"
)
_DEV_DEMO_RE = re.compile(
    r"\bREMOV\w*\b|\bDEMO\w*\b|\bRELOCAT\w*\b|\bEXISTING\b|\bTO REMAIN\b|\bABANDON\w*\b|\bSALVAGE\w*\b"
)
_DEV_NOT_DEVICE_RE = re.compile(
    r"\bRACKS?\b|\bPANEL(?:BOARD)?S?\b|\bMODULES?\b|\bCABINETS?\b|\bDRIVERS?\b|\bBALLASTS?\b|\bSYSTEMS?\b|"
    r"\bCONTROLLERS?\b|\bRELAYS?\b|\bPOWER PACKS?\b|\bGATEWAYS?\b|\bBRIDGES?\b|\bKEYPADS?\b|\bSTATIONS?\b|"
    r"\bTIME ?CLOCKS?\b|\bPHOTO ?CELLS?\b|\bDAYLIGHT\w*\b|\bPLATES?\b|\bBOX(?:ES)?\b|\bTHERMOSTATS?\b|"
    r"\bT-?STATS?\b|\bTEMPERATURE\b|\bCO2\b|\bHUMIDITY\b|\bSMOKE\b|\bHEAT\b|\bDUCT\b|\bLEAK\b|\bWATER\b|"
    r"\bGAS\b|\bDOOR\b|\bWINDOW\b|\bALLOWANCE\b"
)
_DEV_RECEPT_RE = re.compile(r"\bRECEPT\w*\b|\bRECEP\b|\bDUPLEX\b|\bQUAD(?:RU)?PLEX\b|\bQUAD\b|\bFOURPLEX\b")
_DEV_QUAD_RE = re.compile(r"\bQUAD(?:RU)?PLEX\b|\bQUAD\b|\bFOURPLEX\b|\bDOUBLE DUPLEX\b")
# Receptacles outside the vocabulary (the scorer prices them apart): a
# special configuration, a floor or weatherproof device, USB, isolated
# ground, switched, a non-default voltage, or a combined low-voltage outlet.
_DEV_RECEPT_SPECIAL_RE = re.compile(
    r"\bSIMPLEX\b|\bSINGLE\b|\bTRIPLEX\b|\bL?\d{1,2}-\d{2}[RP]\b|\b(?:30|50|60|100)\s?A(?:MPS?)?\b|"
    r"\bPIN AND SLEEVE\b|\bTWIST[\s-]?LOCK\b|\bSPECIAL PURPOSE\b|\bFLOOR\b|\bPOKE[\s-]?THRU\b|\bUSB\b|"
    r"\bWP\b|\bWEATHER\w*\b|\bWR\b|\bIN[\s-]?USE\b|\bISOLATED\b|\bIG\b|\bSWITCHED\b|\bSPLIT[\s-]?WIRED\b|"
    r"\bCEILING\b|\bPEDESTAL\b|\bSURGE\b|\b(?:208|240|250|277|480)\s?V\b|\bDATA\b|\bTELE(?:PHONE|COM)?\b|\bPHONE\b|"
    r"\bCOMM\b|\bVOICE\b|\bRANGE\b|\bDRYER\b|\bWELD\w*\b"
)
_DEV_GFCI_RE = re.compile(r"(?<!\bNON-)(?<!\bNON )(?<!\bNON)\bGFC?I\b|(?<!\bNON-)(?<!\bNON )\bGROUND[\s-]FAULT\b", re.I)
_DEV_TR_RE = re.compile(r"\bTR\b|\bTAMPER\b")
_DEV_CTRL_RE = re.compile(r"\bCONTROLLED\b|\bOCCUPANCY\b|\bVACANCY\b|\bPLUG[\s-]?LOAD\b|\bAUTOMATIC\w*\b")
_DEV_DIMMER_RE = re.compile(r"\bDIMMERS?\b|\bDIMMING SWITCH(?:ES)?\b|\bWALL SWITCH DIMMING\b")
_DEV_OCC_WORDS_RE = re.compile(r"\bOCCUPANCY\b|\bVACANCY\b|\bOCC\b|\bMOTION\b|\bSENSORS?\b|\bSENSING\b")
_DEV_OCC_RE = re.compile(r"\bOCCUPANCY\b|\bVACANCY\b|\bMOTION\b")
_DEV_OVERRIDE_RE = re.compile(r"\bOVERRID\w*\b")
_DEV_LOCK_RE = re.compile(r"\bLOCK(?:ABLE|ING|ED)?\b|\bKEYED\b")
_DEV_COVER_RE = re.compile(r"\bCOVERS?\b")
# A cover named as part of the device ("DIMMER SWITCH W/ LOCKABLE COVER",
# "WALL DIMMER WITH LOCKABLE COVER"); a bare "LOCKABLE DIMMER COVER" is an
# accessory row, not a dimmer.
_DEV_COVER_CLAUSE_RE = re.compile(
    r"(?:\bW/\s*|\bWITH\b\s*|\bAND\b\s*|&\s*|\bINCLUDING\b\s*|\bINCL\.?\s*|,\s*)(?:A\s+)?(?:LOCK\w*\s+)?COVERS?\b"
)
_DEV_SENSOR_NOUN_RE = re.compile(r"\bSENSORS?\b|\bSWITCH(?:ES)?\b|\bDETECTORS?\b")
_DEV_WALL_RE = re.compile(r"\bWALL\b|\bSWITCH(?:ES)?\b")
_DEV_CEILING_RE = re.compile(r"\bCEILING\b|\bCORNER\b|\bHIGH[\s-]?BAY\b")
_DEV_CONNECTION_RE = re.compile(r"\bCONNECTIONS?\b|\bHOOK-?UPS?\b|\bWHIPS?\b|\bCONNECT\b|\bFINAL CONNECT\w*\b")
_DEV_NOT_CONNECTION_RE = re.compile(
    r"\bSERVICE\b|\bUTILITY\b|\bMETER\w*\b|\bGROUND\w*\b|\bBOND\w*\b|\bDATA\b|\bTELE(?:PHONE|COM)?\b|\bFEEDERS?\b|"
    r"\bCONNECTORS?\b|\bPANEL(?:BOARD)?S?\b|\bGENERATOR\b|\bTRANSFORMER\b"
)
_DEV_DEFAULT_VOLTAGE_PART_RE = re.compile(r",\s*1(?:20|25)\s?V(?:AC)?\b")
# Low-voltage outlets (7172 E2.00: "green triangular low-voltage outlet
# symbols" 77, "TV outlet locations" 4; the estimator's TELE/DATA OUTLET 78
# and TV OUTLET 4). A TV word wins over a data word; a receptacle word or a
# cable word makes the text something else.
_DEV_LV_OUTLET_NOUN_RE = re.compile(r"\bOUTLETS?\b|\bJACKS?\b|\bDROPS?\b|\bLOCATIONS?\b")
_DEV_LV_TV_RE = re.compile(r"\bTV\b|\bCATV\b|\bTELEVISION\b")
_DEV_LV_DATA_RE = re.compile(r"\bDATA\b|\bTELE(?:PHONE|COM)?\b|\bPHONE\b|\bVOICE\b|\bCOMM\b|\bLOW[\s-]?VOLTAGE\b")
_DEV_LV_NOT_RE = re.compile(r"\bCABLE\b|\bCAT\s?\d\b|\bRACK\b|\bPANEL\b|\bBACKBOARD\b|\bCAMERAS?\b|\bSPEAKERS?\b|\bCONDUIT\b")
# A floor box receptacle ("conference-table floor-box receptacle symbols").
_DEV_FLOOR_RE = re.compile(r"\bFLOOR[\s-]?BOX(?:ES)?\b|\bFLOOR\b.{0,20}\bRECEPT|\bPOKE[\s-]?THRU\b")
# Legend marks of lighting-control symbols the readers count by their letter
# ("wall-mounted lighting-control symbols marked S/OS" 28, "lighting-control
# symbols marked M" 10 on 7172 E2.00): S/OS, OS/S, SO and OSW are a wall
# switch sensor (OCCUPANCY SENSOR SWITCH), OS alone is a sensor placed by the
# text, M and MS are a motion sensor (ceiling unless the text says wall).
_DEV_LIGHTING_CONTROL_RE = re.compile(r"\bLIGHTING[\s-]CONTROLS?\b|\bSENSORS?\b|\bSWITCH(?:ES)?\b")
_DEV_MARK_RE = re.compile(r"\b(?:MARKED|LABELED|LABELLED|TAGGED|SYMBOLS?)\s+(?:AS\s+)?['\"]?(S/OS|OS/S|OSW|OS|SO|MS|M)['\"]?(?=$|[\s,;.)])")
_DEV_MARK_WALL: frozenset[str] = frozenset({"S/OS", "OS/S", "OSW", "SO"})


@dataclass(frozen=True)
class DeviceName:
    """A standard device: its family (DEVICE_FAMILIES) and the priced
    modifiers found (DEVICE_MODIFIERS); `description` is the estimator's
    name for it."""

    family: str
    modifiers: frozenset[str] = frozenset()

    @property
    def description(self) -> str:
        return device_description(self.family, self.modifiers)


def device_description(family: str, modifiers: Any = ()) -> str:
    """The estimator vocabulary name of a device family with its priced
    modifiers. Unknown modifiers are ignored; an unknown family raises."""
    mods = {str(m).upper() for m in (modifiers or ())}
    if family == "dimmer":
        if "OCC" in mods:
            return "DIMMER & OCCUPANCY SENSOR SWITCH"
        if "LOCK" in mods:
            return "DIMMER SWITCH W/ LOCKABLE COVER"
        return "DIMMER SWITCH"
    if family == "occupancy_sensor":
        return "OCCUPANCY SENSOR SWITCH" if "WALL" in mods else "OCCUPANCY SENSOR"
    if family in ("receptacle:duplex", "receptacle:quadplex"):
        name = "QUADPLEX RECEPTACLE" if family == "receptacle:quadplex" else "DUPLEX RECEPTACLE"
        if "CTRL" in mods:
            name += " CONTROLLED BY OCCUPANCY"
        if "GFCI" in mods:
            name += ", GFCI"
        if "TR" in mods:
            name += ", TR"
        return name
    if family == "receptacle:floor":
        name = "FLOOR BOX RECEPTACLE"
        if "GFCI" in mods:
            name += ", GFCI"
        if "TR" in mods:
            name += ", TR"
        return name
    if family == "connection":
        return "ELECTRICAL CONNECTION"
    if family == "lv_outlet:data":
        return "TELE/DATA OUTLET"
    if family == "lv_outlet:tv":
        return "TV OUTLET"
    raise ValueError(f"unknown device family: {family!r}")


def parse_device(text: Any) -> DeviceName | None:
    """The standard device a description names, or None when the text is
    not one (demolition, a special receptacle, a dimming panel, a cover on
    its own, a thermostat, a plain switch, anything else). Deterministic
    over the normalized description; an exclusion clause ("excluding ...")
    is dropped first; "occupancy-sensor OVERRIDE dimmers" are plain dimmers
    (the sensor is a separate device)."""
    norm = normalize_description(text)
    if not norm:
        return None
    body = _DEV_EXCLUDE_CLAUSE_RE.sub("", norm).strip(" ,;:-")
    if not body or _DEV_DEMO_RE.search(body):
        return None
    if _DEV_FLOOR_RE.search(body) and _DEV_RECEPT_RE.search(body) and not _DEV_NOT_DEVICE_RE.search(_DEV_FLOOR_RE.sub(" ", body)):
        mods = set()
        if _DEV_GFCI_RE.search(body):
            mods.add("GFCI")
        if _DEV_TR_RE.search(body):
            mods.add("TR")
        return DeviceName("receptacle:floor", frozenset(mods))
    if _DEV_RECEPT_RE.search(body):
        if _DEV_RECEPT_SPECIAL_RE.search(body) or _DEV_NOT_DEVICE_RE.search(body):
            return None
        mods: set[str] = set()
        if _DEV_GFCI_RE.search(body):
            mods.add("GFCI")
        if _DEV_TR_RE.search(body):
            mods.add("TR")
        if _DEV_CTRL_RE.search(body):
            mods.add("CTRL")
        family = "receptacle:quadplex" if _DEV_QUAD_RE.search(body) else "receptacle:duplex"
        return DeviceName(family, frozenset(mods))
    if _DEV_DIMMER_RE.search(body):
        if _DEV_NOT_DEVICE_RE.search(body):
            return None
        if _DEV_COVER_RE.search(body) and not _DEV_COVER_CLAUSE_RE.search(body):
            return None  # a cover, not a dimmer
        mods = set()
        if _DEV_OCC_WORDS_RE.search(body) and not _DEV_OVERRIDE_RE.search(body):
            mods.add("OCC")
        elif _DEV_LOCK_RE.search(body):
            mods.add("LOCK")
        return DeviceName("dimmer", frozenset(mods))
    if _DEV_OCC_RE.search(body) and _DEV_SENSOR_NOUN_RE.search(body):
        if _DEV_NOT_DEVICE_RE.search(body):
            return None
        wall = bool(_DEV_WALL_RE.search(body)) and not _DEV_CEILING_RE.search(body)
        return DeviceName("occupancy_sensor", frozenset({"WALL"} if wall else ()))
    mark = _DEV_MARK_RE.search(body)
    if mark and _DEV_LIGHTING_CONTROL_RE.search(body) and not _DEV_NOT_DEVICE_RE.search(body) and not _DEV_RECEPT_RE.search(body):
        letters = mark.group(1)
        if letters in _DEV_MARK_WALL:
            wall = not _DEV_CEILING_RE.search(body)
        else:
            wall = bool(re.search(r"\bWALL\b", body)) and not _DEV_CEILING_RE.search(body)
        return DeviceName("occupancy_sensor", frozenset({"WALL"} if wall else ()))
    if _DEV_LV_OUTLET_NOUN_RE.search(body) and not _DEV_LV_NOT_RE.search(body) and not _DEV_RECEPT_RE.search(body):
        if _DEV_LV_TV_RE.search(body):
            return DeviceName("lv_outlet:tv")
        if _DEV_LV_DATA_RE.search(body):
            return DeviceName("lv_outlet:data")
    if _DEV_CONNECTION_RE.search(body):
        if _DEV_NOT_CONNECTION_RE.search(body):
            return None
        return DeviceName("connection")
    return None


def canonical_device_description(text: Any) -> str | None:
    """The estimator vocabulary name of the device `text` describes, or
    None when it is not a standard device."""
    device = parse_device(text)
    return device.description if device is not None else None


def same_device_wording(description: Any, canonical: str) -> bool:
    """True when `description` already is the canonical name (case, spacing
    and a default ", 125V" part aside), so a conforming line is never
    renamed or warned about."""
    left = _DEV_DEFAULT_VOLTAGE_PART_RE.sub("", normalize_description(description))
    return re.sub(r"\s+", " ", left).strip() == normalize_description(canonical)


# Estimator gear vocabulary (spec 3.4; the sent BOQs of the 8.1 pool) -------------
#
# Breakers, disconnects and transformers are named by their ratings alone:
# "20A/1P CIRCUIT BREAKER", "30A/3P DISCONNECT SWITCH, NEMA 3R", "75KVA
# TRANSFORMER". The readers name them by panel and look ("Panel A branch
# breaker, 20A 1-pole", "Rooftop-unit disconnect symbols; ratings not
# shown", "New 75 kVA dry-type transformer T-B, 480V to 120/208V") and the
# scorer keys gear on the rating tokens, so 7172 matched none of its 14 gear
# truth lines. The composers take the ratings; gear_description reads them
# off the text when the text carries them and gives None otherwise (the
# consolidator then looks for them in a linked panel schedule). Panels are
# composed by scope_breakers.panel_description from the schedule header.

_GEAR_POLE_WORDS_RE = re.compile(r"\b(SINGLE|TWO|THREE)[\s-]POLE\b")
_GEAR_FUSED_RE = re.compile(r"\bFUSED\b|\bFUSIBLE\b")
_GEAR_NON_FUSED_RE = re.compile(r"\bNON[\s-]?FUSED\b|\bUNFUSED\b|\bNF\b")
_GEAR_AFCI_RE = re.compile(r"\bAFCI\b|\bARC[\s-]FAULT\b")
_GEAR_GFCI_RE = re.compile(r"(?<!\bNON-)(?<!\bNON )\bGFC?I\b|\bGROUND[\s-]FAULT\b|\bGFEP\b|\bGFP\b")
_GEAR_OUTDOOR_RE = re.compile(r"\bROOF(?:TOP)?\b|\bOUTDOOR\b|\bEXTERIOR\b|\bOUTSIDE\b|\bWP\b|\bWEATHER\w*\b|\bRAIN\w*\b")


def breaker_description(amps: Any, poles: Any, *, gfci: bool = False, afci: bool = False) -> str:
    """'20A/1P CIRCUIT BREAKER', '20A/1P CIRCUIT BREAKER, GFCI'."""
    head = f"{_rating_text(amps)}A/{_rating_text(poles)}P CIRCUIT BREAKER" if amps and poles else "CIRCUIT BREAKER"
    mods = (["GFCI"] if gfci else []) + (["AFCI"] if afci else [])
    return head + (", " + ", ".join(mods) if mods else "")


def disconnect_description(amps: Any, poles: Any, *, nema: Any = None, fused: bool = False) -> str:
    """'30A/3P DISCONNECT SWITCH, NEMA 3R'; the NEMA part only when known."""
    rating = f"{_rating_text(amps)}A/{_rating_text(poles)}P " if amps and poles else (f"{_rating_text(amps)}A " if amps else "")
    head = f"{rating}{'FUSED ' if fused else ''}DISCONNECT SWITCH"
    return f"{head}, NEMA {str(nema).upper()}" if nema else head


def transformer_description(kva: Any) -> str:
    """'75KVA TRANSFORMER'."""
    return f"{_rating_text(kva)}KVA TRANSFORMER" if kva else "TRANSFORMER"


def _rating_text(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value or "").strip()
    return str(int(f)) if f.is_integer() else f"{f:g}"


def gear_ratings(text: Any) -> dict[str, Any]:
    """The ratings a gear description carries: amps, poles, kva, nema, fused,
    gfci, afci, outdoor (a roof, outdoor or weatherproof word). Missing
    ratings are absent, never guessed."""
    norm = normalize_description(text)
    toks = key_kinds(norm)
    out: dict[str, Any] = {}
    if toks.get("amps"):
        out["amps"] = int(sorted(toks["amps"], key=lambda v: int(v))[0])
    poles = toks.get("poles")
    if poles:
        out["poles"] = int(sorted(poles)[0])
    else:
        m = _GEAR_POLE_WORDS_RE.search(norm)
        if m:
            out["poles"] = int(_POLE_WORDS[m.group(1)])
    if toks.get("kva"):
        out["kva"] = float(sorted(toks["kva"])[0])
    if toks.get("nema"):
        out["nema"] = sorted(toks["nema"])[0]
    if _GEAR_FUSED_RE.search(norm) and not _GEAR_NON_FUSED_RE.search(norm):
        out["fused"] = True
    if _GEAR_GFCI_RE.search(norm):
        out["gfci"] = True
    if _GEAR_AFCI_RE.search(norm):
        out["afci"] = True
    if _GEAR_OUTDOOR_RE.search(norm):
        out["outdoor"] = True
    return out


def gear_description(text: Any, unit: Any = "EA") -> str | None:
    """The estimator vocabulary name of a breaker, disconnect or transformer
    the text describes with its ratings, else None (a text without the
    ratings, a panel, anything else keeps its wording). Demolition text
    gives None too."""
    identity = parse_item(text, unit)
    if identity.item_class == "demolition":
        return None
    r = gear_ratings(text)
    if identity.item_class == "breaker":
        if not (r.get("amps") and r.get("poles")):
            return None
        return breaker_description(r["amps"], r["poles"], gfci=bool(r.get("gfci")), afci=bool(r.get("afci")))
    if identity.item_class != "panel":
        return None
    family = identity.family.split(":", 1)[1] if ":" in identity.family else "panel"
    if family == "transformer":
        return transformer_description(r["kva"]) if r.get("kva") else None
    if family == "disconnect":
        if not (r.get("amps") and r.get("poles")):
            return None
        return disconnect_description(r["amps"], r["poles"], nema=r.get("nema"), fused=bool(r.get("fused")))
    return None


# Conduit accessories (spec 3.4, E23). The renderer derives the fittings of
# every conduit row from its wastage-inclusive BOQ length; these are the
# ratios it uses (plans_reader_render.conduit_fittings, measured on the sent
# BOQs) and the vocabulary that names them, so the acceptance check and the
# pricing engine read the same rule. 7154: 6,337.71 FT EMT x 1.10 = 6,971.48
# FT -> 140 connectors, 698 couplings, 698 straps, 349 anchors, 349 screws,
# 70 boxes.
CONDUIT_ACCESSORY_RATIOS: dict[str, float] = {
    "connector_ft": 50.0,    # one compression connector per 50 FT (ceil)
    "coupling_ft": 10.0,     # one coupling per 10 FT stick (ceil)
    "straps_per_coupling": 1.0,
    "straps_per_anchor": 2.0,  # anchors = ceil(straps / 2); screws = anchors
    "anchors_per_box": 5.0,    # SQ boxes = ceil(anchors / 5)
}
_ACCESSORY_ROLE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("connector", re.compile(r"\bCONN(?:ECTOR|ECTORS|S)?\b")),
    ("coupling", re.compile(r"\bCOUPLINGS?\b")),
    ("strap", re.compile(r"\bSTRAPS?\b|\b1-H\b")),
    ("anchor", re.compile(r"\bANCHORS?\b")),
    ("screw", re.compile(r"\bSCREWS?\b")),
    ("box", re.compile(r"\bSQ\.? ?BOX(?:ES)?\b|\bBOX(?:ES)? COMB\b")),
    ("elbow", re.compile(r"\bELBOWS?\b|\bELL\b")),
    ("locknut", re.compile(r"\bLOCKNUTS?\b")),
    ("adapter", re.compile(r"\bADAPTERS?\b")),
    ("bushing", re.compile(r"\bBUSHINGS?\b")),
)


def _ceil_int(value: float) -> int:
    return int(math.ceil(round(value, 9)))


def conduit_accessory_counts(length_ft: float) -> dict[str, int]:
    """Accessory counts for one EMT or RGS conduit run of `length_ft` (the
    wastage-inclusive BOQ length): connectors, couplings, straps, anchors,
    screws, boxes. Zero everywhere when the length is not positive."""
    try:
        length = float(length_ft)
    except (TypeError, ValueError):
        length = 0.0
    if length <= 0:
        return {k: 0 for k in ("connectors", "couplings", "straps", "anchors", "screws", "boxes")}
    r = CONDUIT_ACCESSORY_RATIOS
    connectors = _ceil_int(length / r["connector_ft"])
    couplings = _ceil_int(length / r["coupling_ft"])
    straps = _ceil_int(couplings * r["straps_per_coupling"])
    anchors = _ceil_int(straps / r["straps_per_anchor"])
    boxes = _ceil_int(anchors / r["anchors_per_box"])
    return {"connectors": connectors, "couplings": couplings, "straps": straps, "anchors": anchors, "screws": anchors, "boxes": boxes}


def accessory_role(description: Any) -> str | None:
    """The accessory an is_accessory row names (connector, coupling, strap,
    anchor, screw, box, elbow, locknut, adapter, bushing), reading the
    estimator abbreviations ("3/4\"C EMT Compression Conn Stl" is a
    connector), else None."""
    norm = normalize_description(description)
    if not norm:
        return None
    for role, pat in _ACCESSORY_ROLE_RES:
        if pat.search(norm):
            return role
    return None


def hard_conflict(a: ItemIdentity, b: ItemIdentity) -> bool:
    """A conflict that forbids matching a to b: a different conductor size,
    a different conduit size or type, a different voltage, one side linear
    (always FT) and the other a count class, or a length-vs-count unit on a
    LINEAR_CLASSES item. Linear luminaires in EA and FT are not a hard
    conflict (the evidence rule of linear_evidence_required decides)."""
    for key in ("conductor_size", "conduit_size", "conduit_type", "volts"):
        va, vb = a.attrs.get(key), b.attrs.get(key)
        if va and vb and va != vb:
            return True
    if is_linear(a) != is_linear(b):
        return True
    if a.item_class in LINEAR_CLASSES or b.item_class in LINEAR_CLASSES:
        da, db = _UOM_DIMENSION.get(a.uom, ""), _UOM_DIMENSION.get(b.uom, "")
        if da and db and da != db:
            return True
    return False


# Lighting track rows (spec 3.5 E15) -------------------------------------------------------
# The reader words a track assembly row many ways ("REMOVE EXISTING TRACK-
# LIGHTING ASSEMBLIES INCLUDING HEADS", "REMOVE LIGHTING TRACK ROW") and the
# heads on it as their own row ("VISIBLE EXISTING TRACK-LIGHTING HEADS TO
# REMOVE"). parse_item classes a description with HEADS anywhere in it as a
# luminaire, so a track assembly row that names its heads in a trailing
# clause lost its track class. The primary noun decides here: the text
# before the first W/, WITH, AND, INCLUDING or comma clause, with the
# demolition verb stripped.
_TRACK_LIGHTING_HINT_RE = re.compile(r"\bLIGHT|\bLUMINAIRE|\bFIXTURE|\bHEADS?\b")


def track_subject(description: Any) -> str | None:
    """'track' when the primary noun of a description names a lighting track
    (not its heads), 'head' when it names the track heads, None when the
    text names no track (a cable TRAY is never a track)."""
    norm = normalize_description(description)
    if not _TRACK_RE.search(norm) or "TRAY" in norm:
        return None
    rest = _DEMO_STRIP_RE.sub("", norm, count=1) if _DEMO_STRIP_RE.match(norm) else norm
    head = _DEMO_TRAILING_RE.split(rest, maxsplit=1)[0].strip() or rest
    if _TRACK_HEAD_RE.search(head):
        return "head"
    if _TRACK_RE.search(head):
        return "track"
    return None


def is_lighting_track(identity: ItemIdentity) -> bool:
    """A lighting row or the demolition of one whose subject is the track
    itself, never its heads: the identity's class is a lighting class
    (LUMINAIRE_CLASSES) or a demolition whose subject is one (or a bare
    track subject on a lighting text), and track_subject of its text is
    'track'."""
    if track_subject(identity.norm) != "track":
        return False
    if identity.item_class in LUMINAIRE_CLASSES:
        return True
    if identity.item_class != "demolition":
        return False
    subject = identity.attrs.get("subject", "")
    return subject in LUMINAIRE_CLASSES or (subject == "other" and bool(_TRACK_LIGHTING_HINT_RE.search(identity.norm)))


__all__ = [
    "BOQ_GROUPS", "BOQ_LEVEL0", "BOQ_SUBGROUPS", "UNITS",
    "MATCH_THRESHOLD", "EXACT_TOLERANCE", "WASTAGE_FACTOR",
    "BoqRow", "normalize_unit", "normalize_description", "key_tokens", "similarity",
    "ITEM_CLASSES", "LINEAR_CLASSES", "LINEAR_LUMINAIRE_WORDS", "LINEAR_LUMINAIRE_RE",
    "CANON_UOMS",
    "ItemIdentity", "parse_item", "item_class", "is_linear", "linear_evidence_required",
    "count_key", "hard_conflict", "canon_uom", "uom_dimension", "convert_unit_value",
    "key_kinds",
    "LUMINAIRE_CLASSES", "luminaire_variant", "luminaire_tag_parts", "identity_matches",
    "DEVICE_VOCABULARY", "DEVICE_FAMILIES", "DEVICE_MODIFIERS", "DeviceName", "device_description",
    "parse_device", "canonical_device_description", "same_device_wording",
    "CONDUIT_ACCESSORY_RATIOS", "conduit_accessory_counts", "accessory_role",
    "track_subject", "is_lighting_track",
]
