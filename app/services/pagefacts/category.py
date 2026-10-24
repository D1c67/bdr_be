"""Discipline per sheet and the document category check (WS1 5.4, fixes
E24: the 7154 MECH set was labeled electrical_drawings and read with the
electrical prompt).

`discipline_of` maps the alphabetic prefix of a sheet number to a drawing
category through a longest-prefix-first table; an unknown prefix maps to
`unknown` and never counts toward a share. `category_check` proposes
`relabel` when at least two numbered sheets exist and at least 80% of them
belong to one category that is not the label (7154 MECH: 8/8 M, share 1.0),
`warn_mixed` for a 50 to 80% non-label majority, else `none`. The check is a
proposal: applying it is the facts job's decision behind
`plans_reader_facts_relabel`.
"""

from __future__ import annotations

import re
from collections import Counter

from app.services.pagefacts.contracts import CategoryCheck

ELECTRICAL = "electrical_drawings"
LOW_VOLTAGE = "low_voltage_drawings"
MECHANICAL = "mechanical_drawings"
PLUMBING = "plumbing_drawings"
FIRE_PROTECTION = "fire_protection_drawings"
ARCHITECTURAL = "architectural_drawings"
STRUCTURAL = "structural_drawings"
CIVIL = "civil_drawings"
GENERAL = "general_drawings"
UNKNOWN = "unknown"

CATEGORY_NAMES: dict[str, str] = {
    ELECTRICAL: "Electrical Drawings",
    LOW_VOLTAGE: "Low Voltage Drawings",
    MECHANICAL: "Mechanical Drawings",
    PLUMBING: "Plumbing Drawings",
    FIRE_PROTECTION: "Fire Protection Drawings",
    ARCHITECTURAL: "Architectural Drawings",
    STRUCTURAL: "Structural Drawings",
    CIVIL: "Civil Drawings",
    GENERAL: "General Drawings",
}

# Longest prefix first (checked in this order).
PREFIX_MAP: tuple[tuple[str, str], ...] = (
    ("HVAC", MECHANICAL),
    ("ITS", ELECTRICAL),
    ("AVC", LOW_VOLTAGE),
    ("AVS", LOW_VOLTAGE),
    ("LVD", LOW_VOLTAGE),
    ("TEL", LOW_VOLTAGE),
    ("ED", ELECTRICAL),
    ("EP", ELECTRICAL),
    ("EL", ELECTRICAL),
    ("ES", ELECTRICAL),
    ("EX", ELECTRICAL),
    ("EE", ELECTRICAL),
    ("EA", ELECTRICAL),
    ("LD", ELECTRICAL),
    ("SL", ELECTRICAL),
    ("TS", ELECTRICAL),
    ("FA", LOW_VOLTAGE),
    ("TY", LOW_VOLTAGE),
    ("TD", LOW_VOLTAGE),
    ("TN", LOW_VOLTAGE),
    ("AV", LOW_VOLTAGE),
    ("LV", LOW_VOLTAGE),
    ("SY", LOW_VOLTAGE),
    ("SS", LOW_VOLTAGE),
    ("SC", LOW_VOLTAGE),
    ("CT", LOW_VOLTAGE),
    ("MD", MECHANICAL),
    ("MH", MECHANICAL),
    ("MP", MECHANICAL),
    ("ME", MECHANICAL),
    ("PD", PLUMBING),
    ("PL", PLUMBING),
    ("FP", FIRE_PROTECTION),
    ("FS", FIRE_PROTECTION),
    ("AD", ARCHITECTURAL),
    ("AS", ARCHITECTURAL),
    ("ID", ARCHITECTURAL),
    ("IN", ARCHITECTURAL),
    ("SD", STRUCTURAL),
    ("CD", CIVIL),
    ("CS", CIVIL),
    ("ST", CIVIL),
    ("SG", CIVIL),
    ("GR", CIVIL),
    ("UD", CIVIL),
    ("GN", GENERAL),
    ("GI", GENERAL),
    ("TI", GENERAL),
    ("CV", GENERAL),
    ("E", ELECTRICAL),
    ("T", LOW_VOLTAGE),
    ("M", MECHANICAL),
    ("H", MECHANICAL),
    ("P", PLUMBING),
    ("F", FIRE_PROTECTION),
    ("A", ARCHITECTURAL),
    ("I", ARCHITECTURAL),
    ("Q", ARCHITECTURAL),
    ("K", ARCHITECTURAL),
    ("S", STRUCTURAL),
    ("C", CIVIL),
    ("R", CIVIL),
    ("U", CIVIL),
    ("L", CIVIL),
    ("G", GENERAL),
)
_KNOWN_PREFIXES: frozenset[str] = frozenset(p for p, _ in PREFIX_MAP)

RELABEL_SHARE = 0.8
MIXED_SHARE = 0.5
MIN_NUMBERED_PAGES = 2

_PREFIX_RE = re.compile(r"^([A-Z]+)")


def prefix_of(sheet_number: str | None) -> str | None:
    if not sheet_number:
        return None
    m = _PREFIX_RE.match(sheet_number.strip().upper())
    return m.group(1) if m else None


def _category_for_prefix(
    prefix: str, *, doc_prefixes: Counter[str] | None, has_luminaires: bool
) -> str:
    """The table lookup plus the ambiguous-prefix rules: `L` is landscape
    (civil) unless the document carries E sheets or luminaire evidence,
    `AS`/`SD`/`CS` follow their parent discipline when the set has it,
    `F` alone needs no civil sheets."""
    docp = doc_prefixes or Counter()
    if prefix == "L" and (has_luminaires or any(p.startswith("E") for p in docp)):
        return ELECTRICAL
    if prefix == "CS" and not any(p.startswith("C") and p != "CS" for p in docp):
        return GENERAL
    if prefix == "AS" and not any(p == "A" for p in docp):
        return CIVIL if any(p.startswith("C") for p in docp) else ARCHITECTURAL
    if prefix == "F" and any(p in ("C", "R", "ST") for p in docp):
        return CIVIL
    if prefix == "MI" and any(p.startswith("C") for p in docp):
        return CIVIL
    for p, cat in PREFIX_MAP:
        if prefix == p:
            return cat
    return UNKNOWN


def discipline_of(
    sheet_number: str | None,
    *,
    doc_prefixes: Counter[str] | None = None,
    has_luminaires: bool = False,
) -> tuple[str | None, str]:
    """(discipline prefix, category). A leading building or area letter is
    stripped when the remainder is a known discipline and the rest of the
    document shares that leading letter (WPSD: `BE0-00` is area B + E)."""
    prefix = prefix_of(sheet_number)
    if not prefix:
        return None, UNKNOWN
    cat = _category_for_prefix(prefix, doc_prefixes=doc_prefixes, has_luminaires=has_luminaires)
    if cat != UNKNOWN:
        return prefix, cat
    if len(prefix) >= 2 and prefix[1:] in _KNOWN_PREFIXES and doc_prefixes:
        same_lead = sum(
            n for p, n in doc_prefixes.items() if p.startswith(prefix[0]) and len(p) >= 2
        )
        total = sum(doc_prefixes.values())
        if total and same_lead / total >= 0.5:
            inner = prefix[1:]
            return inner, _category_for_prefix(
                inner, doc_prefixes=doc_prefixes, has_luminaires=has_luminaires
            )
    return prefix, UNKNOWN


def category_check(
    label: str | None, pages: list[tuple[str | None, str]], *, name: str | None = None
) -> CategoryCheck:
    """`pages` = [(discipline prefix, category)] per page; pages without a
    sheet number or with an unknown category do not count."""
    counts: Counter[str] = Counter()
    by_prefix: Counter[str] = Counter()
    for prefix, cat in pages:
        if cat == UNKNOWN or not prefix:
            continue
        counts[cat] += 1
        by_prefix[prefix] += 1
    numbered = sum(counts.values())
    if numbered == 0:
        return CategoryCheck(
            label=label, detected=None, share=0.0, numbered_pages=0, by_discipline={}, action="none"
        )
    detected, top = counts.most_common(1)[0]
    share = top / numbered
    action = "none"
    message: str | None = None
    if detected != label and numbered >= MIN_NUMBERED_PAGES and share >= RELABEL_SHARE:
        action = "relabel"
        doc = f"Document {name}" if name else "The document"
        prefixes = ", ".join(f"{p} ({n})" for p, n in by_prefix.most_common())
        message = (
            f"{doc} was labeled {CATEGORY_NAMES.get(label or '', label or 'unlabeled')} but "
            f"{top} of {numbered} numbered sheets are {prefixes}; it was read as {CATEGORY_NAMES.get(detected, detected)}."
        )
    elif detected != label and MIXED_SHARE <= share < RELABEL_SHARE:
        action = "warn_mixed"
        message = (
            f"{numbered} numbered sheets: {', '.join(f'{CATEGORY_NAMES.get(c, c)} {n}' for c, n in counts.most_common())};"
            f" the label {CATEGORY_NAMES.get(label or '', label or 'unlabeled')} is not the majority."
        )
    return CategoryCheck(
        label=label,
        detected=detected,
        share=share,
        numbered_pages=numbered,
        by_discipline=dict(by_prefix),
        action=action,
        message=message,
    )
