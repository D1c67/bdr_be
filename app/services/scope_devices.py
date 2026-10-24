"""Device rules (docs/AI_ESTIMATING_BUILD_SPEC.md WS5; fixes E21 and feeds E13).

- Unpriced modifiers (EMERGENCY, ESSENTIAL, NORMAL, LIFE SAFETY, DEDICATED and
  device colors, with their residue nouns such as "EMERGENCY BRANCH") never
  split a receptacle row, and neither do location descriptors ("COUNTER AND
  GENERAL LOCATIONS"), the default 125V or the plural "RECEPTACLES": rows that
  differ only by them merge, in the receptacle family only, conserving the
  total. Priced modifiers (GFCI, TR, WP, USB, QUAD, OCCUPANCY CONTROLLED, ...)
  stay.
- Tamper-resistant duplex quantity comes from the legend symbol count
  (`count_legend_symbol`: the REC-TAMP symbol of the WS1 legend, learned from
  its legend cell and matched on the new-work plan pages). The TR line takes
  its quantity out of the plain duplex line (the total is conserved) when the
  TR symbols were counted on a sheet that line cites, that is when they are
  part of its count; otherwise the TR line is added beside it. A room rule
  ("tamper resistant in offices and corridors") only writes a clarification.
- Hospital grade and 20A specification grade notes go to `material_note` on
  the receptacle rows and to SpecFacts for the pricing engine (key
  `device_grade`, value `hospital`, with the citation).

`count_legend_symbol` is the only function here that touches PDFium (under
pdf_split._PDFIUM_LOCK); everything else is pure. It is a narrow symbol
census for chevron-marked receptacle symbols until WS1 ships a general vector
symbol matcher (pagefacts census kind `symbol`), at which point the engine
should read that census instead.
"""

from __future__ import annotations

import copy
import ctypes
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

UNPRICED_MODIFIERS: tuple[str, ...] = ("EMERGENCY", "ESSENTIAL", "NORMAL", "LIFE SAFETY", "DEDICATED")
COLORS: tuple[str, ...] = ("RED", "BLUE", "GRAY", "GREY", "IVORY", "WHITE", "BLACK", "BROWN", "ORANGE", "GREEN")
# A modifier may carry its noun ("EMERGENCY BRANCH", "NORMAL POWER", "DEDICATED
# CIRCUIT"): the noun is residue of the unpriced modifier and goes with it.
_MOD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in UNPRICED_MODIFIERS + COLORS) + r")\b(?!\s+DOT)"
    r"(?:\s+(?:BRANCH|CIRCUITS?|POWER|SYSTEM))?",
    re.I,
)
# Location descriptors ("COUNTER AND GENERAL LOCATIONS", "TV LOCATION", "AT
# NURSE STATION AREA") never price a receptacle: a description part that ends
# in LOCATION(S) or AREA(S) is dropped from the canonical form.
_LOCATION_PART_RE = re.compile(r"^(?:AT|IN|FOR)?\s*[A-Z0-9/&+'\-\s]*?\b(?:LOCATIONS?|AREAS?)$", re.I)
# A part left over from a stripped modifier on an output enforced by an
# earlier engine version ("DUPLEX RECEPTACLES, 125V, BRANCH").
_RESIDUE_PART_RE = re.compile(r"^(?:BRANCH|CIRCUITS?|POWER|SYSTEM)$", re.I)
# The default receptacle voltage (125V or 120V) distinguishes nothing; other
# voltages (208V, 250V) stay in the description.
_DEFAULT_VOLTAGE_RE = re.compile(r"^1(?:20|25)\s?V(?:AC)?$", re.I)
_BRANCH_AMPS_RE = re.compile(r"^(?:15|20)\s?A(?:MP)?$", re.I)
RECEPTACLE_RE = re.compile(r"\bRECEPT(?:ACLE)?S?\b|\bRECEPTS\b|\bDUPLEX\b|\bQUADPLEX\b|\bSIMPLEX\b", re.I)
NOT_RECEPTACLE_RE = re.compile(r"\bREMOV|\bDEMO|\bRELOCAT|\bDATA\b|\bBREAKER\b|\bCOVER\b(?!\s+PLATE)", re.I)
TR_RE = re.compile(r"\bTR\b|\bTAMPER[\s-]+RESISTANT\b|\bTAMPER\b", re.I)
GFCI_RE = re.compile(r"\bGFCI\b|\bGFI\b|\bGROUND\s+FAULT\b", re.I)
PLAIN_DUPLEX_RE = re.compile(r"^\s*DUPLEX\s+RECEPTACLE(?:\s+OUTLET)?\s*$", re.I)
TR_DESCRIPTION = "DUPLEX RECEPTACLE, TR"
HOSPITAL_GRADE_RE = re.compile(r"\bhospital[\s-]+grade\b", re.I)
SPEC_GRADE_RE = re.compile(r"\b(?:20\s*A(?:MP)?\s+)?spec(?:ification)?[\s-]+grade\b", re.I)
TAMPER_ROOM_RE = re.compile(r"\btamper[\s-]+resistant\b", re.I)


def is_receptacle_row(description: str) -> bool:
    """A receptacle item: the item name (before the first comma, at most 60
    characters) names a receptacle; assemblies that list receptacles among
    their parts are not receptacle rows."""
    d = str(description or "")
    head = d.split(",")[0]
    if len(head) > 60 or re.search(r"\bSYSTEM\b|\bPANEL\b|\bASSEMBL", head, re.I):
        return False
    return bool(RECEPTACLE_RE.search(head)) and not NOT_RECEPTACLE_RE.search(d)


def canonical_receptacle(description: str) -> str:
    """The description without unpriced modifiers ('DUPLEX RECEPTACLE,
    EMERGENCY, DEDICATED' -> 'DUPLEX RECEPTACLE'). Modifier residue
    ("EMERGENCY BRANCH"), location descriptors ("COUNTER AND GENERAL
    LOCATIONS"), the default 125V and the plural "RECEPTACLES" go too, so
    rows the reader split by circuit or room meet on one canonical line."""
    parts = []
    for part in str(description or "").split(","):
        cleaned = re.sub(r"\s+", " ", _MOD_RE.sub(" ", part)).strip(" -/")
        if not cleaned or _DEFAULT_VOLTAGE_RE.match(cleaned):
            continue
        if parts and (_LOCATION_PART_RE.match(cleaned) or _RESIDUE_PART_RE.match(cleaned)):
            continue  # a location tail or modifier residue (the item name is never one)
        cleaned = re.sub(r"\bRECEPTACLES\b", "RECEPTACLE", cleaned, flags=re.I)
        parts.append(cleaned)
    return ", ".join(parts).upper() if parts else str(description or "").strip().upper()


def has_unpriced_modifier(description: str) -> bool:
    return bool(_MOD_RE.search(str(description or "")))


def is_plain_duplex(description: str) -> bool:
    """A plain duplex receptacle line: nothing priced beyond the device
    (no GFCI, TR, USB, WP, quad, controlled, isolated ground ...). The
    branch amperage (15A, 20A) is not a priced split here."""
    canon = canonical_receptacle(description)
    parts = [p.strip() for p in canon.split(",") if p.strip() and not _BRANCH_AMPS_RE.match(p.strip())]
    return bool(parts) and PLAIN_DUPLEX_RE.match(parts[0]) is not None and len(parts) == 1


# BOQ walking ---------------------------------------------------------------------------------


def _subgroups(spec: Mapping[str, Any]) -> Iterable[tuple[str, str, str, list[dict[str, Any]]]]:
    """(section, group, subgroup, items list) for every subgroup (mutable lists)."""
    for area in spec.get("areas") or []:
        for sec in area.get("sections") or []:
            heading = str(sec.get("heading") or "")
            for grp in sec.get("groups") or []:
                for sub in grp.get("subgroups") or []:
                    items = sub.get("items")
                    if isinstance(items, list):
                        yield heading, str(grp.get("name") or ""), str(sub.get("name") or ""), items


def is_exempt(item: Mapping[str, Any]) -> bool:
    """Rows added through chat or modify are exempt from automatic rules."""
    if item.get("combine") == "manual" or item.get("manual") is True:
        return True
    return str(item.get("origin") or item.get("source") or "").lower() in ("chat", "modify", "human", "manual")


# An equipment connection the reader wrote as a RECONNECTION ("FIRE/SMOKE
# DAMPER ELECTRICAL RECONNECTION", 7154 run b87f0ffd C40 and C41) is the
# same ELECTRICAL CONNECTION line; the policy also_pattern \bconnect...\b
# never matched it (no word boundary inside RECONNECTION).
RECONNECT_RE = re.compile(r"\bRE[\s-]?(?=CONNECT)", re.I)


def connection_text(text: Any) -> str:
    """`text` with a RECONNECT prefix read as CONNECT, so the
    IN-EQUIP-CONNECTION also_pattern matches a reconnection row."""
    return RECONNECT_RE.sub("", str(text or ""))


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _qty_out(v: float) -> int | float:
    return int(v) if float(v).is_integer() else round(v, 4)


def _join_notes(notes: Iterable[Any], limit: int = 400) -> str | None:
    seen: list[str] = []
    for n in notes:
        s = str(n or "").strip()
        if s and s not in seen:
            seen.append(s)
    text = " ".join(seen)
    return text[:limit] if text else None


MergeSources = Callable[[dict[str, Any], Sequence[Mapping[str, Any]]], None]


def merge_receptacles(spec: dict[str, Any], *, merge_sources: MergeSources | None = None) -> list[dict[str, Any]]:
    """Merge receptacle rows that differ only by unpriced modifiers, in place.
    Returns the merge log. Idempotent: a second call finds nothing.

    `merge_sources(head, members)` carries the v2 provenance of every merged
    member into the surviving line (the engine passes
    scope_engine._merge_line_sources: the union of `source_refs` with their
    multipliers and of `sheet_refs`, `combine` sum, `needs_recount`, basis).
    Without it the merge kept only the head's refs, so the merged quantity
    no longer reproduced from its sources (gate arithmetic: run ad69e5a4
    B35 "stored 35 EA, computed 26 EA" after B36 folded in) and the other
    members' rows turned into orphans (gate orphan_rows: C33). Each log
    entry's `merged` members record their own `source_refs` when they have
    any, so the decision shows which rows the merged line consumed."""
    log: list[dict[str, Any]] = []
    for heading, group, subgroup, items in _subgroups(spec):
        if heading.upper().startswith("DEMO"):
            continue
        buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
        for i, it in enumerate(items):
            if not isinstance(it, Mapping) or is_exempt(it) or it.get("is_accessory"):
                continue
            desc = str(it.get("description") or "")
            if not is_receptacle_row(desc):
                continue
            buckets[(canonical_receptacle(desc), str(it.get("unit") or "EA").upper())].append(i)
        drop: set[int] = set()
        for (canon, _unit), idxs in buckets.items():
            changed = any(str(items[i].get("description") or "").upper() != canon for i in idxs)
            if len(idxs) == 1 and not changed:
                continue
            head = items[idxs[0]]
            before = []
            for i in idxs:
                entry = {"line_id": items[i].get("line_id"), "description": items[i].get("description"),
                         "quantity": items[i].get("quantity")}
                if items[i].get("source_refs"):
                    entry["source_refs"] = copy.deepcopy(items[i]["source_refs"])
                before.append(entry)
            total = sum(_num(items[i].get("quantity")) for i in idxs)
            if len(idxs) > 1 and merge_sources is not None:
                merge_sources(head, [items[i] for i in idxs])
            head["quantity"] = _qty_out(total)
            head["description"] = canon
            if len(idxs) > 1:
                head["material_note"] = _join_notes(items[i].get("material_note") for i in idxs)
                caveats = [items[i].get("caveat") for i in idxs if items[i].get("caveat")]
                head["caveat"] = caveats[0] if caveats else None
                sheets = []
                for i in idxs:
                    for s in items[i].get("sheet_refs") or []:
                        if s not in sheets:
                            sheets.append(s)
                if sheets:
                    head["sheet_refs"] = sheets
            drop.update(idxs[1:])
            log.append({
                "into": head.get("line_id"),
                "description": canon,
                "quantity": head["quantity"],
                "merged": before,
                "group": group,
                "subgroup": subgroup,
            })
        if drop:
            items[:] = [it for i, it in enumerate(items) if i not in drop]
    return log


def receptacle_total(spec: Mapping[str, Any]) -> float:
    total = 0.0
    for heading, _g, _s, items in _subgroups(spec):
        if heading.upper().startswith("DEMO"):
            continue
        for it in items:
            if isinstance(it, Mapping) and not it.get("is_accessory") and is_receptacle_row(str(it.get("description") or "")):
                total += _num(it.get("quantity"))
    return total


def _sheets_of(item: Mapping[str, Any]) -> set[str]:
    return {str(s).strip().upper() for s in item.get("sheet_refs") or [] if str(s).strip()}


SPLIT_COMBINE = "split"


def mark_split(parent: dict[str, Any], child: dict[str, Any], moved: float, *,
               new_line_id: Callable[[], str] | None = None) -> dict[str, Any] | None:
    """Record that `moved` EA of `parent` now sit on `child` (the TR split),
    so the WS4 provenance gates read the pair as one consumer of the parent's
    rows and the arithmetic recheck reproduces both quantities:

    - child (the line the split created, `scope_rule` DEV-TR, or a line that
      already cites exactly the parent's rows): `source_refs` = a copy of the
      parent's refs, `combine` "split", `split_of` = the parent line id (a
      string), `split_quantity` = the quantity moved so far; its stored
      quantity is what the parent's rows count on it.
    - parent: `split_to` = `[{line_id, quantity}]`, one entry per split line
      with that line's stored quantity, so `plans_reader_lines.recheck`
      expects the parent at the sum of its sources minus its split_to
      quantities, the split line at its own stored quantity, and the
      provenance gate (`split_pairs`, linked from both sides) counts the pair
      as one consumer of the rows.

    A legacy parent (no `source_refs`), a child citing other rows (not a
    split of this parent) and a child without a line id when `new_line_id`
    is None carry no markers. Returns `{parent, child, quantity}` or None."""
    if moved <= 0 or not parent.get("source_refs") or not parent.get("line_id"):
        return None
    parent_refs = {str(r.get("ref")) for r in parent["source_refs"] if isinstance(r, Mapping) and r.get("ref")}
    child_refs = {str(r.get("ref")) for r in child.get("source_refs") or [] if isinstance(r, Mapping) and r.get("ref")}
    if child_refs and child_refs != parent_refs:
        return None
    child_id = child.get("line_id") or (new_line_id() if new_line_id is not None else None)
    if not child_id:
        return None
    child["line_id"] = child_id
    parent_id = str(parent["line_id"])
    if not child_refs:
        child["source_refs"] = copy.deepcopy(parent["source_refs"])
        child["basis"] = parent.get("basis") or "counted"
    child["combine"] = SPLIT_COMBINE
    child["split_of"] = parent_id
    child["split_quantity"] = _qty_out(_num(child.get("split_quantity")) + float(moved))
    entries: list[dict[str, Any]] = []
    for e in parent.get("split_to") or []:
        lid = str(e.get("line_id") if isinstance(e, Mapping) else e or "").strip()
        if lid and lid != str(child_id):
            entries.append({"line_id": lid, "quantity": _qty_out(_num(e.get("quantity")) if isinstance(e, Mapping) else 0.0)})
    entries.append({"line_id": str(child_id), "quantity": _qty_out(_num(child.get("quantity")))})
    parent["split_to"] = sorted(entries, key=lambda e: e["line_id"])
    return {"parent": parent_id, "child": str(child_id), "quantity": _qty_out(float(moved))}


def apply_tr(spec: dict[str, Any], tr_target: int | None, *, cite: str | None = None,
             tr_pages: Mapping[str, Any] | None = None,
             new_line_id: Callable[[], str] | None = None) -> dict[str, Any] | None:
    """Set the TR duplex line to `tr_target`, taking the quantity out of the
    plain duplex line (total conserved). The plain line is the largest plain
    duplex row (`is_plain_duplex`, so "DUPLEX RECEPTACLES, 125V, BRANCH"
    qualifies once canonicalized), preferring one that cites a sheet the TR
    symbols were counted on. The plain line is reduced only when the TR
    symbols are part of its count: when `tr_pages` (sheet -> symbol count)
    and the row's `sheet_refs` are both known and share no sheet, the TR
    line is added without reducing it (`reduced: false`). When quantity
    moves out of a v2 plain line (one with `source_refs`), the pair carries
    the split provenance of `mark_split` (`new_line_id` mints the TR line id
    when the caller assigns ids). Idempotent. None when there is no target or
    nothing to do."""
    if tr_target is None or tr_target < 0:
        return None
    tr_row = None
    tr_sub = None
    plain: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    tr_sheets = {str(s).strip().upper() for s in (tr_pages or {}) if str(s).strip()}
    for heading, _g, _sub, items in _subgroups(spec):
        if heading.upper().startswith("DEMO"):
            continue
        for it in items:
            if not isinstance(it, Mapping) or is_exempt(it):
                continue
            desc = str(it.get("description") or "")
            if not is_receptacle_row(desc) or GFCI_RE.search(desc) or re.search(r"\bQUAD", desc, re.I):
                continue
            if TR_RE.search(desc) and tr_row is None:
                tr_row, tr_sub = it, items
            elif is_plain_duplex(desc) and not TR_RE.search(desc):
                plain.append((it, items))
    current = _num(tr_row.get("quantity")) if tr_row is not None else 0.0
    delta = float(tr_target) - current
    if delta == 0:
        return None
    if not plain:
        return {"applied": False, "reason": "no_plain_duplex_line", "target": tr_target, "current": current}
    plain.sort(key=lambda ps: (bool(_sheets_of(ps[0]) & tr_sheets), _num(ps[0].get("quantity"))), reverse=True)
    plain_row, plain_sub = plain[0]
    row_sheets = _sheets_of(plain_row)
    reduced = not (tr_sheets and row_sheets and not (tr_sheets & row_sheets))
    available = _num(plain_row.get("quantity"))
    if reduced:
        take = min(delta, available) if delta > 0 else delta
        plain_row["quantity"] = _qty_out(available - take)
    else:
        take = delta  # the TR symbols were counted on other sheets: nothing to move
    created = tr_row is None
    if tr_row is None:
        tr_row = {
            "description": TR_DESCRIPTION,
            "quantity": 0,
            "unit": plain_row.get("unit") or "EA",
            "caveat": None,
            "sheet_refs": list(plain_row.get("sheet_refs") or []),
            "material_note": f"Tamper resistant per the legend symbol count{f' ({cite})' if cite else ''}.",
            "wastage_applies": False,
            "is_accessory": False,
            "material_category": plain_row.get("material_category"),
            "scope_rule": "DEV-TR",
        }
        assert plain_sub is not None
        plain_sub.insert(plain_sub.index(plain_row) + 1, tr_row)
    tr_row["quantity"] = _qty_out(current + take)
    split = mark_split(plain_row, tr_row, float(take), new_line_id=new_line_id) if reduced else None
    out = {
        "applied": True,
        "target": tr_target,
        "tr_quantity": tr_row["quantity"],
        "from_line": plain_row.get("line_id"),
        "plain_description": plain_row.get("description"),
        "plain_quantity": plain_row["quantity"],
        "line_id": tr_row.get("line_id"),
        "reduced": reduced,
        "created": created,
        "moved": _qty_out(float(take)) if reduced else 0,
        "split": split,
    }
    if not reduced:
        out["reason"] = "tr_symbols_counted_on_other_sheets"
        out["tr_sheets"] = sorted(tr_sheets)
        out["plain_sheets"] = sorted(row_sheets)
    if take != delta:
        out["warning"] = "plain duplex line smaller than the TR count; TR capped"
    if plain_row["quantity"] == 0 and plain_sub is not None and tr_sub is not None:
        pass  # a zero plain duplex line stays visible (conservation is by totals)
    return out


def apply_grade_notes(spec: dict[str, Any], notes: Sequence[str]) -> int:
    """Add the grade notes to every receptacle row's material_note (once)."""
    if not notes:
        return 0
    n = 0
    for heading, _g, _s, items in _subgroups(spec):
        if heading.upper().startswith("DEMO"):
            continue
        for it in items:
            if not isinstance(it, Mapping) or is_exempt(it) or not is_receptacle_row(str(it.get("description") or "")):
                continue
            current = str(it.get("material_note") or "")
            add = [x for x in notes if x not in current]
            if add:
                it["material_note"] = (current + " " + " ".join(add)).strip()[:600]
                n += 1
    return n


# Device facts from document statements --------------------------------------------------------


@dataclass
class DeviceFacts:
    tr_count: int | None = None
    tr_method: str = "unavailable"  # legend_symbol | unavailable
    tr_pages: dict[str, int] = field(default_factory=dict)
    tr_cite: str | None = None
    tr_room_rule: str | None = None
    grade_notes: list[str] = field(default_factory=list)
    spec_facts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tr_count": self.tr_count,
            "tr_method": self.tr_method,
            "tr_pages": dict(self.tr_pages),
            "tr_cite": self.tr_cite,
            "tr_room_rule": self.tr_room_rule,
            "grade_notes": list(self.grade_notes),
            "spec_facts": list(self.spec_facts),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> DeviceFacts:
        d = dict(data or {})
        return cls(
            tr_count=d.get("tr_count"),
            tr_method=str(d.get("tr_method") or "unavailable"),
            tr_pages=dict(d.get("tr_pages") or {}),
            tr_cite=d.get("tr_cite"),
            tr_room_rule=d.get("tr_room_rule"),
            grade_notes=list(d.get("grade_notes") or []),
            spec_facts=list(d.get("spec_facts") or []),
            warnings=list(d.get("warnings") or []),
        )


def grade_facts(statements: Iterable[Any]) -> tuple[list[str], list[dict[str, Any]], str | None]:
    """(material notes, SpecFacts, TR room rule) from note statements."""
    notes: list[str] = []
    facts: list[dict[str, Any]] = []
    room_rule = None
    for st in statements:
        text = str(getattr(st, "text", None) or (st.get("text") if isinstance(st, Mapping) else "") or "")
        cite_fn = getattr(st, "cite", None)
        cite = cite_fn() if callable(cite_fn) else (st.get("cite") if isinstance(st, Mapping) else None)
        if not text or not re.search(r"\breceptacles?\b|\bdevices?\b", text, re.I):
            continue
        if HOSPITAL_GRADE_RE.search(text):
            note = f"Hospital grade devices in patient care areas per {cite}." if cite else "Hospital grade devices in patient care areas."
            if note not in notes:
                notes.append(note)
            facts.append({"key": "device_grade", "value": "hospital", "source_ref": cite, "quote": text[:240]})
        if SPEC_GRADE_RE.search(text):
            note = f"Specification grade devices per {cite}." if cite else "Specification grade devices."
            if note not in notes:
                notes.append(note)
            facts.append({"key": "device_spec_grade", "value": "specification", "source_ref": cite, "quote": text[:240]})
        if TAMPER_ROOM_RE.search(text) and room_rule is None:
            room_rule = f"Tamper resistant receptacles where the documents require them ({cite})." if cite else \
                "Tamper resistant receptacles where the documents require them."
    return notes, facts, room_rule


# Legend symbol census (chevron-marked receptacle symbols) ----------------------------------------


@dataclass(frozen=True)
class SymbolTemplate:
    arm_ratio: float  # circle diameter / chevron arm length
    offset_ratio: float  # vertex-to-circle-center distance / circle diameter
    angle_deg: float  # angle between the chevron arms

    def to_dict(self) -> dict[str, Any]:
        return {"arm_ratio": round(self.arm_ratio, 4), "offset_ratio": round(self.offset_ratio, 4),
                "angle_deg": round(self.angle_deg, 2)}


def _path_points(obj: Any, raw: Any) -> list[tuple[float, float]]:
    count = raw.FPDFPath_CountSegments(obj.raw)
    pts: list[tuple[float, float]] = []
    x = ctypes.c_float()
    y = ctypes.c_float()
    for i in range(count):
        seg = raw.FPDFPath_GetPathSegment(obj.raw, i)
        raw.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
        pts.append((x.value, y.value))
    a, b, c, d, e, f = obj.get_matrix().get()
    return [(a * px + c * py + e, b * px + d * py + f) for px, py in pts]


def _page_paths(page: Any) -> list[list[tuple[float, float]]]:
    import pypdfium2.raw as raw

    out = []
    for obj in page.get_objects(max_depth=4):
        if obj.type != raw.FPDF_PAGEOBJ_PATH:
            continue
        try:
            pts = _path_points(obj, raw)
        except Exception:  # noqa: BLE001 - a malformed path is skipped
            continue
        if pts:
            out.append(pts)
    return out


def _chevrons(paths: Sequence[Sequence[tuple[float, float]]], *, min_angle: float, max_angle: float,
              max_arm: float = 40.0) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float], float, float]]:
    """(vertex, arm end a, arm end b, arm length, angle) of every pair of
    equal straight segments that share an endpoint at an angle in range."""
    segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for pts in paths:
        if len(pts) > 3:
            continue  # curves and polygons (a round body) never form the chevron
        for p0, p1 in zip(pts, pts[1:], strict=False):
            if p0 != p1:
                segs.append((p0, p1))
    by_end: dict[tuple[float, float], list[tuple[int, int]]] = defaultdict(list)
    for i, (p0, p1) in enumerate(segs):
        by_end[(round(p0[0], 1), round(p0[1], 1))].append((i, 0))
        by_end[(round(p1[0], 1), round(p1[1], 1))].append((i, 1))
    found = []
    for refs in by_end.values():
        if len(refs) != 2:
            continue
        (i, ki), (j, kj) = refs
        if i == j:
            continue
        v = segs[i][ki]
        a = segs[i][1 - ki]
        b = segs[j][1 - kj]
        la, lb = math.dist(v, a), math.dist(v, b)
        if la < 0.5 or la > max_arm or abs(la - lb) > 0.12 * max(la, lb):
            continue
        cos = ((a[0] - v[0]) * (b[0] - v[0]) + (a[1] - v[1]) * (b[1] - v[1])) / (la * lb)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
        if min_angle <= ang <= max_angle:
            found.append((v, a, b, (la + lb) / 2, ang))
    return found


def learn_template(page: Any, frame: Any, symbol_bbox: Sequence[float]) -> SymbolTemplate | None:
    """Measure the chevron receptacle symbol inside a legend cell (page
    fractions). None when the cell holds no chevron next to a round body."""
    x0, y0, x1, y1 = (float(v) for v in symbol_bbox)
    pad_x, pad_y = (x1 - x0) * 0.1, (y1 - y0) * 0.6
    box = (x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y)

    def inside(pt: tuple[float, float]) -> bool:
        u, w = frame.to_frac(pt[0], pt[1])
        return box[0] <= u <= box[2] and box[1] <= w <= box[3]

    paths = [p for p in _page_paths(page) if all(inside(q) for q in p)]
    chevs = _chevrons(paths, min_angle=95.0, max_angle=145.0)
    rounds = [p for p in paths if len(p) >= 8]
    if not chevs or not rounds:
        return None
    best = None
    for v, a, b, arm, ang in chevs:
        bx = (a[0] - v[0]) + (b[0] - v[0])
        by = (a[1] - v[1]) + (b[1] - v[1])
        if math.hypot(bx, by) == 0:
            continue
        for p in rounds:
            xs = [q[0] for q in p]
            ys = [q[1] for q in p]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if w <= 0 or abs(w - h) > 0.2 * w:
                continue
            center = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
            dist = math.dist(center, v)
            if dist > 2.5 * w or dist == 0:
                continue
            cos = ((center[0] - v[0]) * bx + (center[1] - v[1]) * by) / (dist * math.hypot(bx, by))
            if cos < math.cos(math.radians(25)):
                continue  # the arms must open toward the round body
            cand = SymbolTemplate(arm_ratio=w / arm, offset_ratio=dist / w, angle_deg=ang)
            if best is None or dist < best[0]:
                best = (dist, cand)
    return best[1] if best else None


def _match_page(page: Any, frame: Any, tpl: SymbolTemplate, *, exclude: Sequence[Sequence[float]] = ()) -> list[tuple[float, float]]:
    paths = _page_paths(page)
    pts_grid: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    cell = 5.0
    for p in paths:
        for q in p:
            pts_grid[(int(q[0] // cell), int(q[1] // cell))].append(q)

    def near(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for gx in range(int((cx - r) // cell) - 1, int((cx + r) // cell) + 2):
            for gy in range(int((cy - r) // cell) - 1, int((cy + r) // cell) + 2):
                out.extend(pts_grid.get((gx, gy), ()))
        return out

    hits: list[tuple[float, float]] = []
    for v, a, b, arm, _ang in _chevrons(paths, min_angle=tpl.angle_deg - 12, max_angle=tpl.angle_deg + 12):
        bx = (a[0] - v[0]) + (b[0] - v[0])
        by = (a[1] - v[1]) + (b[1] - v[1])
        bn = math.hypot(bx, by)
        if bn == 0:
            continue
        diam = tpl.arm_ratio * arm
        r = diam / 2
        cx = v[0] + bx / bn * tpl.offset_ratio * diam
        cy = v[1] + by / bn * tpl.offset_ratio * diam
        ring = [q for q in near(cx, cy, r * 1.3) if abs(math.dist(q, (cx, cy)) - r) < 0.15 * r]
        sectors = {int((math.degrees(math.atan2(q[1] - cy, q[0] - cx)) + 360) // 30) for q in ring}
        if len(sectors) < 8:
            continue
        u, w = frame.to_frac(v[0], v[1])
        if any(e[0] <= u <= e[2] and e[1] <= w <= e[3] for e in exclude):
            continue
        if all(math.dist((u, w), h) > 0.002 for h in hits):
            hits.append((round(u, 4), round(w, 4)))
    return hits


def count_legend_symbol(
    pdf_bytes: bytes,
    *,
    legend_page: int,
    symbol_bbox: Sequence[float],
    plan_pages: Sequence[int],
    exclude_regions: Mapping[int, Sequence[Sequence[float]]] | None = None,
) -> dict[str, Any] | None:
    """Count a chevron-marked legend symbol on the plan pages (1-based
    pages). Returns {template, pages: {page: count}, total, positions} or
    None when the legend cell holds no measurable chevron symbol."""
    from app.services.pagefacts import textlayer
    from app.services.pdf_split import _PDFIUM_LOCK

    excl = dict(exclude_regions or {})
    with textlayer.open_document(pdf_bytes) as doc:
        with _PDFIUM_LOCK:
            lp = doc[legend_page - 1]
            tpl = learn_template(lp, textlayer.page_frame(lp), symbol_bbox)
        if tpl is None:
            return None
        pages: dict[str, int] = {}
        positions: dict[str, list[list[float]]] = {}
        for pno in plan_pages:
            with _PDFIUM_LOCK:
                pg = doc[pno - 1]
                hits = _match_page(pg, textlayer.page_frame(pg), tpl, exclude=excl.get(pno, ()))
            pages[str(pno)] = len(hits)
            positions[str(pno)] = [list(h) for h in hits]
    return {"template": tpl.to_dict(), "pages": pages, "total": sum(pages.values()), "positions": positions}


def tr_legend_entry(legend: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The plain tamper-resistant duplex entry of a WS1 legend (REC-TAMP)."""
    entries = [e for e in (legend or {}).get("entries") or [] if isinstance(e, Mapping)]
    for e in entries:
        tag = str(e.get("tag") or "").upper()
        desc = str(e.get("description") or "")
        if tag == "REC-TAMP" or (TAMPER_ROOM_RE.search(desc) and "DUPLEX" in desc.upper() and not GFCI_RE.search(desc)):
            if e.get("symbol_bbox") and e.get("page"):
                return dict(e)
    return None


def copy_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(spec))


SymbolCounter = Callable[..., "dict[str, Any] | None"]

__all__ = [
    "UNPRICED_MODIFIERS", "COLORS", "TR_DESCRIPTION", "is_receptacle_row", "canonical_receptacle",
    "has_unpriced_modifier", "is_plain_duplex", "is_exempt", "merge_receptacles", "receptacle_total", "apply_tr", "apply_grade_notes",
    "DeviceFacts", "grade_facts", "SymbolTemplate", "learn_template", "count_legend_symbol", "tr_legend_entry",
    "RECONNECT_RE", "connection_text",
]
