"""Circuits for measurement (WS3 steps B2 and B3; docs/AI_ESTIMATING_BUILD_SPEC.md
6 WS3, Q15). Consumes WS1 page facts (circuit tags, keynote markers, panel
schedules, panel locations); no model, no I/O, no prices.

Parts:

1. Conductor grammar. `parse_awg` reads a wire size token (`12`, `#12`,
   `12 AWG`, `1/0`, `250 KCMIL`) into the canonical `#12` / `#1/0` /
   `#250 KCMIL` keys used by WS4 `plans_reader_lines.parse_conductors`
   (which this module reuses for counted callouts); `parse_callout` adds the
   raceway (`3/4"C EMT`, the `_CONDUIT_RE` form the renderer derives
   accessories from); `conductor_description` renders the BOQ text.

2. Tag census and conventions. `tag_census` counts a sheet's circuit tags
   (tags at the same position collapse) per `panel-ckt` circuit key and per
   tag family (circuit plus fixture prefix, `L5/1LH-11`).
   `classify_tag_convention` labels a sheet `tag_per_device` (one tag next
   to each device), `stacked_label` (tags stacked in columns away from the
   devices), `home_run_arrow` (few tags, many circuits per tag, one tag per
   circuit) or `untagged`. `snap_tags` moves stacked labels and arrow tags
   onto the nearest device symbol or census point, following a leader
   segment when a vector index is given.

3. Wiring status, separate from breaker status. `wiring_plans` gives every
   schedule circuit (and every tagged circuit without a schedule row) a
   wiring decision: a circuit whose description names a load, or that is
   tagged on a new-work plan, gets new wiring and a home run whatever the
   breaker status (new, existing, unknown); only an EXISTING LOAD circuit
   with an intercept or extend keynote (a keynote marker near one of its
   tags, or on its schedule row) gets the tie-in allowance instead; an
   EXISTING LOAD circuit with neither gets nothing.

4. Conductor sizing (Q15). `size_conductor`: lighting circuits are #12
   unless voltage drop on the load current exceeds `vd_max_pct`; power home
   runs take the schedule wire column, else voltage drop with
   I = load_va / volts when the schedule has a load, else a 20 A branch
   circuit sized by voltage drop and capped at #10. `voltage_drop_pct` is
   the formula the sizing records its inputs for:
   VD% = 100 x factor x K x I x L / (CM x V), K = 12.9 ohm-cmil/ft (copper,
   75 C), factor 2 for a two-wire circuit and sqrt(3) for three-phase.

5. Survey. `survey_class` labels a drawing set `tagged`, `untagged_text`,
   `vector_text` or `scanned` from its plan pages (B2), for the training
   split of 8.7.

6. Panel schedule sources. `usable_panel_schedule` rejects a WS1 panel
   schedule that carries no circuits worth routing (a spec sentence such
   as "PANEL AND" matched as a title, every row the same number with no
   description, trip or pole count). `panel_schedule_from_rows` turns a
   reader schedule (category output `schedules[]`, schedule_type panel,
   columns plus rows) into the WS1 PanelSchedule shape so the measure has
   the circuits and loads of a panel the text-layer parser missed (Revit
   "Branch Panel: A" tables). `merge_panel_schedules` keeps, per panel
   name, the more complete of the two sources. `load_kind` classifies a
   circuit's load (lighting, receptacle, equipment, feeder, spare) for the
   untagged distribution of devices over schedule circuits (WS3, "untagged
   projects never empty").
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.pagefacts.contracts import PLAN_ROLES, DocFacts, PageFacts
from app.services.plans_reader_lines import parse_conductors

__all__ = [
    "AWG_ORDER",
    "AWG_CIRCULAR_MILS",
    "parse_awg",
    "awg_index",
    "Callout",
    "parse_callout",
    "conductor_description",
    "raceway_description",
    "circuit_key",
    "TagCensus",
    "tag_census",
    "TagConvention",
    "classify_tag_convention",
    "SnappedTag",
    "snap_tags",
    "WiringPlan",
    "wiring_plans",
    "is_new_work_sheet",
    "ConductorSizing",
    "voltage_drop_pct",
    "size_conductor",
    "circuit_volts",
    "load_amps",
    "SurveyClass",
    "survey_class",
    "usable_panel_schedule",
    "panel_schedule_from_rows",
    "merge_panel_schedules",
    "schedule_completeness",
    "load_kind",
    "LOAD_KINDS",
]

# Conductor grammar -------------------------------------------------------------------

AWG_ORDER: tuple[str, ...] = (
    "#14",
    "#12",
    "#10",
    "#8",
    "#6",
    "#4",
    "#3",
    "#2",
    "#1",
    "#1/0",
    "#2/0",
    "#3/0",
    "#4/0",
    "#250 KCMIL",
    "#300 KCMIL",
    "#350 KCMIL",
    "#400 KCMIL",
    "#500 KCMIL",
    "#600 KCMIL",
    "#750 KCMIL",
)
AWG_CIRCULAR_MILS: dict[str, float] = {
    "#14": 4107,
    "#12": 6530,
    "#10": 10380,
    "#8": 16510,
    "#6": 26240,
    "#4": 41740,
    "#3": 52620,
    "#2": 66360,
    "#1": 83690,
    "#1/0": 105600,
    "#2/0": 133100,
    "#3/0": 167800,
    "#4/0": 211600,
    "#250 KCMIL": 250000,
    "#300 KCMIL": 300000,
    "#350 KCMIL": 350000,
    "#400 KCMIL": 400000,
    "#500 KCMIL": 500000,
    "#600 KCMIL": 600000,
    "#750 KCMIL": 750000,
}
# NEC 310.16, 75 C copper (the breaker sizing check only).
AMPACITY_75C: dict[str, int] = {
    "#14": 20,
    "#12": 25,
    "#10": 35,
    "#8": 50,
    "#6": 65,
    "#4": 85,
    "#3": 100,
    "#2": 115,
    "#1": 130,
    "#1/0": 150,
    "#2/0": 175,
    "#3/0": 200,
    "#4/0": 230,
    "#250 KCMIL": 255,
    "#300 KCMIL": 285,
    "#350 KCMIL": 310,
    "#400 KCMIL": 335,
    "#500 KCMIL": 380,
    "#600 KCMIL": 420,
    "#750 KCMIL": 475,
}
K_COPPER_OHM_CMIL_FT = 12.9
AWG_TOKEN_RE = re.compile(
    r"^\s*#?\s*(?P<size>\d{1,3}(?:/0)?)\s*(?P<k>KCMIL|MCM)?\s*(?:AWG|CU|COPPER)?\s*$", re.I
)
RACEWAY_RE = re.compile(
    r"(?P<size>\d(?:\s*[-\s]\s*\d/\d)?|\d/\d)\s*(?:\"|''|”|IN\.?)\s*"
    r"(?P<type>C\b\.?|CONDUIT|EMT|RGS|RMC|IMC|PVC|FMC|LFMC|MC)\b\.?"
    r"(?:\s*(?P<type2>EMT|RGS|RMC|IMC|PVC|FMC|LFMC))?",
    re.I,
)
DEFAULT_RACEWAY_TYPE = "EMT"
THHN_TEXT = "THHN/THWN Cu"


def parse_awg(token: Any) -> str | None:
    """Canonical `#12`, `#1/0`, `#250 KCMIL` or None."""
    if token is None:
        return None
    m = AWG_TOKEN_RE.match(str(token))
    if not m:
        return None
    size = m.group("size")
    if m.group("k") or (size.isdigit() and int(size) >= 250):
        key = f"#{int(size)} KCMIL"
    else:
        key = f"#{size}"
    return key if key in AWG_CIRCULAR_MILS else None


def awg_index(awg: str) -> int:
    """Position in `AWG_ORDER` (larger index = larger conductor)."""
    return AWG_ORDER.index(awg)


def conductor_description(awg: str) -> str:
    return f"{awg} {THHN_TEXT}"


def raceway_description(size: str, kind: str = DEFAULT_RACEWAY_TYPE) -> str:
    """The `_CONDUIT_RE` form: `3/4"C EMT`."""
    return f'{size}"C {kind.upper()}'


@dataclass(slots=True)
class Callout:
    conductors: dict[str, int]
    raceway: str | None
    raw: str

    def to_dict(self) -> dict[str, Any]:
        return {"conductors": dict(self.conductors), "raceway": self.raceway, "raw": self.raw}


def parse_callout(text: Any) -> Callout:
    """Conductors (via WS4 `parse_conductors`) plus the raceway of a wiring
    callout: `3#12, 1#12G, 3/4"C` -> {"#12": 3, "#12 G": 1}, `3/4"C EMT`."""
    raw = str(text or "")
    conductors = parse_conductors(raw)
    raceway: str | None = None
    m = RACEWAY_RE.search(raw)
    if m:
        size = re.sub(r"\s+", "", m.group("size")).replace("-", "-")
        kind = (m.group("type2") or m.group("type") or "").rstrip(".").upper()
        if kind in ("C", "CONDUIT", ""):
            kind = DEFAULT_RACEWAY_TYPE
        raceway = raceway_description(size, kind)
    return Callout(conductors=conductors, raceway=raceway, raw=raw)


# Tag census and conventions ----------------------------------------------------------------

TAG_CONVENTIONS: tuple[str, ...] = ("tag_per_device", "stacked_label", "home_run_arrow", "untagged")
SAME_POSITION_IN = 0.03
STACK_DX_IN = 0.12
STACK_DY_IN = 0.3
STACKED_SHARE = 0.5
MULTI_CIRCUIT_SHARE = 0.3
ARROW_DEVICE_RATIO = 0.35
LEADER_REACH_IN = 0.2
SNAP_REACH_IN = 0.6
LEADER_MAX_IN = 8.0


def circuit_key(panel: str, ckt: int | str) -> str:
    return f"{str(panel).upper()}-{ckt}"


def _facts_dict(page: PageFacts | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(page, PageFacts):
        return page.to_dict()
    return dict(page)


@dataclass(slots=True)
class TagCensus:
    sheet_key: str
    tags: int
    circuits: int
    by_circuit: dict[str, int]
    families: dict[str, int]
    untagged_numbers: int
    dropped_duplicates: int
    positions: dict[str, list[list[float]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_key": self.sheet_key,
            "tags": self.tags,
            "circuits": self.circuits,
            "by_circuit": dict(self.by_circuit),
            "families": dict(self.families),
            "untagged_numbers": self.untagged_numbers,
            "dropped_duplicates": self.dropped_duplicates,
        }


def tag_census(page: PageFacts | Mapping[str, Any], *, sheet_key: str | None = None) -> TagCensus:
    """Circuit tags of one sheet: tags carrying a circuit number, collapsed
    when two tags of the same circuit sit at the same position (a CAD
    double print), keyed `PANEL-CKT`; `families` adds the fixture prefix
    (`L5/1LH-11`). Tags without a number are counted as `untagged_numbers`.
    Counts are floors (spec 5.1)."""
    d = _facts_dict(page)
    w_in, h_in = float(d.get("w_in") or 42.0), float(d.get("h_in") or 30.0)
    key = sheet_key or str(d.get("sheet_number") or f"p{d.get('page')}")
    by_circuit: Counter[str] = Counter()
    families: Counter[str] = Counter()
    positions: dict[str, list[list[float]]] = defaultdict(list)
    seen: list[tuple[str, float, float]] = []
    dropped = 0
    untagged = 0
    total = 0
    for t in (d.get("tags") or {}).get("circuit") or []:
        ckts = t.get("circuits") or []
        if not ckts:
            untagged += 1
            continue
        x, y = float(t.get("x", 0.0)), float(t.get("y", 0.0))
        for c in ckts:
            ck = circuit_key(t.get("panel", "?"), c)
            dup = any(
                sk == ck and math.hypot((sx - x) * w_in, (sy - y) * h_in) <= SAME_POSITION_IN
                for sk, sx, sy in seen
            )
            if dup:
                dropped += 1
                continue
            seen.append((ck, x, y))
            total += 1
            by_circuit[ck] += 1
            fam = t.get("fixture_type")
            families[f"{fam}/{ck}" if fam else ck] += 1
            positions[ck].append([round(x, 4), round(y, 4)])
    return TagCensus(
        sheet_key=key,
        tags=total,
        circuits=len(by_circuit),
        by_circuit=dict(sorted(by_circuit.items())),
        families=dict(sorted(families.items())),
        untagged_numbers=untagged,
        dropped_duplicates=dropped,
        positions=dict(positions),
    )


@dataclass(slots=True)
class TagConvention:
    sheet_key: str
    convention: str
    tags: int
    stacked_share: float
    multi_circuit_share: float
    device_ratio: float | None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_key": self.sheet_key,
            "convention": self.convention,
            "tags": self.tags,
            "stacked_share": round(self.stacked_share, 3),
            "multi_circuit_share": round(self.multi_circuit_share, 3),
            "device_ratio": None if self.device_ratio is None else round(self.device_ratio, 3),
            "reasons": list(self.reasons),
        }


def _stacks(points: list[tuple[float, float]], w_in: float, h_in: float) -> list[list[int]]:
    """Vertical stacks: tags within `STACK_DX_IN` in x and `STACK_DY_IN`
    between consecutive rows."""
    order = sorted(range(len(points)), key=lambda i: (points[i][0], points[i][1]))
    stacks: list[list[int]] = []
    used: set[int] = set()
    for i in order:
        if i in used:
            continue
        stack = [i]
        used.add(i)
        changed = True
        while changed:
            changed = False
            for j in order:
                if j in used:
                    continue
                if any(
                    abs(points[j][0] - points[k][0]) * w_in <= STACK_DX_IN
                    and abs(points[j][1] - points[k][1]) * h_in <= STACK_DY_IN
                    for k in stack
                ):
                    stack.append(j)
                    used.add(j)
                    changed = True
        if len(stack) >= 2:
            stacks.append(stack)
    return stacks


def device_points(page: PageFacts | Mapping[str, Any]) -> list[list[float]]:
    """Census points a tag can snap to: luminaire type marks and status
    tags from the WS1 tags block (keynote markers are hints, not devices)."""
    d = _facts_dict(page)
    tags = d.get("tags") or {}
    out: list[list[float]] = []
    for pts in (tags.get("type_marks") or {}).values():
        out.extend([float(p[0]), float(p[1])] for p in pts)
    for pts in (tags.get("status") or {}).values():
        out.extend([float(p[0]), float(p[1])] for p in pts)
    return out


def classify_tag_convention(
    page: PageFacts | Mapping[str, Any],
    *,
    devices: Sequence[Sequence[float]] | None = None,
    sheet_key: str | None = None,
) -> TagConvention:
    """Per-sheet convention from the tag layout. `devices` are census
    points (page fractions); when None the WS1 type marks and status tags
    of the page are used, and the ratio is None when there are none."""
    d = _facts_dict(page)
    w_in, h_in = float(d.get("w_in") or 42.0), float(d.get("h_in") or 30.0)
    key = sheet_key or str(d.get("sheet_number") or f"p{d.get('page')}")
    tags = [t for t in (d.get("tags") or {}).get("circuit") or [] if t.get("circuits")]
    n = len(tags)
    if n == 0:
        return TagConvention(key, "untagged", 0, 0.0, 0.0, None, ["no circuit tags"])
    pts = [(float(t["x"]), float(t["y"])) for t in tags]
    stacks = _stacks(pts, w_in, h_in)
    stacked = sum(len(s) for s in stacks)
    stacked_share = stacked / n
    multi = sum(1 for t in tags if len(t.get("circuits") or []) >= 2) / n
    dev = list(devices) if devices is not None else device_points(d)
    ratio: float | None = (n / len(dev)) if dev else None
    reasons: list[str] = []
    if multi >= MULTI_CIRCUIT_SHARE:
        reasons.append(f"{multi:.0%} of tags list several circuits")
        conv = "home_run_arrow"
    elif ratio is not None and ratio <= ARROW_DEVICE_RATIO and stacked_share < STACKED_SHARE:
        reasons.append(f"{n} tags for {len(dev)} devices (ratio {ratio:.2f})")
        conv = "home_run_arrow"
    elif stacked_share >= STACKED_SHARE:
        reasons.append(f"{stacked_share:.0%} of tags sit in vertical stacks ({len(stacks)} stacks)")
        conv = "stacked_label"
    else:
        reasons.append(f"{n} tags spread over the plan")
        conv = "tag_per_device"
    return TagConvention(key, conv, n, stacked_share, multi, ratio, reasons)


@dataclass(slots=True)
class SnappedTag:
    tag: dict[str, Any]
    x: float
    y: float
    snapped_to: str | None  # leader | device | None
    distance_in: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "panel": self.tag.get("panel"),
            "circuits": list(self.tag.get("circuits") or []),
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "snapped_to": self.snapped_to,
            "distance_in": round(self.distance_in, 3),
        }


def _nearest(
    x: float, y: float, points: Sequence[Sequence[float]], w_in: float, h_in: float
) -> tuple[int | None, float]:
    best: tuple[int | None, float] = (None, math.inf)
    for i, p in enumerate(points):
        dd = math.hypot((p[0] - x) * w_in, (p[1] - y) * h_in)
        if dd < best[1]:
            best = (i, dd)
    return best


def snap_tags(
    tags: Iterable[Mapping[str, Any]],
    devices: Sequence[Sequence[float]],
    *,
    w_in: float,
    h_in: float,
    segments: Any = None,
    leader_reach_in: float = LEADER_REACH_IN,
    snap_in: float = SNAP_REACH_IN,
) -> list[SnappedTag]:
    """Snap each tag to a device point: follow a leader segment that starts
    within `leader_reach_in` of the tag to its far end and take the nearest
    device within `snap_in` there; else the nearest device within `snap_in`
    of the tag itself; else the tag stays where it is (`snapped_to` None).
    `segments` is a `plans_reader_geometry.SegmentIndex` or None."""
    out: list[SnappedTag] = []
    for t in tags:
        x, y = float(t.get("x", 0.0)), float(t.get("y", 0.0))
        done = False
        if segments is not None and devices:
            for s in segments.near(x, y, leader_reach_in):
                if s.length_in(w_in, h_in) > LEADER_MAX_IN:
                    continue
                d0 = math.hypot((s.x0 - x) * w_in, (s.y0 - y) * h_in)
                d1 = math.hypot((s.x1 - x) * w_in, (s.y1 - y) * h_in)
                fx, fy = (s.x1, s.y1) if d0 <= d1 else (s.x0, s.y0)
                i, dist = _nearest(fx, fy, devices, w_in, h_in)
                if i is not None and dist <= snap_in:
                    out.append(SnappedTag(dict(t), float(devices[i][0]), float(devices[i][1]), "leader", dist))
                    done = True
                    break
        if done:
            continue
        i, dist = _nearest(x, y, devices, w_in, h_in) if devices else (None, math.inf)
        if i is not None and dist <= snap_in:
            out.append(SnappedTag(dict(t), float(devices[i][0]), float(devices[i][1]), "device", dist))
        else:
            out.append(SnappedTag(dict(t), x, y, None, 0.0))
    return out


# Wiring status --------------------------------------------------------------------------

EXISTING_LOAD_RE = re.compile(r"^\s*\(?\s*E\s*\)?\s*EXISTING\s+LOAD\s*$|^\s*EXISTING\s+LOAD\b", re.I)
SPARE_RE = re.compile(r"^\s*(?:SPARE|SPACE|UNUSABLE\s+SPACE|BLANK)\b", re.I)
EXISTING_MARK_RE = re.compile(r"\(E\)|\bEXISTING\b|\bEXIST\.?\b", re.I)
INTERCEPT_RE = re.compile(r"\bINTERCEPT|\bEXTEND(?:ED)?\b|\bRE-?CONNECT\b.*\bEXISTING\b", re.I)
DEMOLITION_RE = re.compile(r"DEMOLITION|\bDEMO\b|REMOVAL", re.I)
DEMO_SHEET_RE = re.compile(r"^[A-Z]{1,2}D\d", re.I)
KEYNOTE_LINK_IN = 1.0
WIRING_STATUSES: tuple[str, ...] = ("new", "tie_in", "none")


def is_new_work_sheet(page: PageFacts | Mapping[str, Any]) -> bool:
    """A plan sheet that is not a demolition sheet (number `ED2.01`, title
    with DEMOLITION or REMOVAL)."""
    d = _facts_dict(page)
    roles = set(d.get("roles") or [])
    if not (roles & PLAN_ROLES) and d.get("page_role") not in PLAN_ROLES:
        return False
    number = str(d.get("sheet_number") or "")
    if DEMO_SHEET_RE.match(number):
        return False
    title = str(d.get("sheet_title") or "")
    titles = [title] + [str(v.get("title") or "") for v in d.get("views") or []]
    return not any(DEMOLITION_RE.search(t) for t in titles if t)


def is_demolition_sheet(page: PageFacts | Mapping[str, Any]) -> bool:
    d = _facts_dict(page)
    roles = set(d.get("roles") or [])
    if not (roles & PLAN_ROLES) and d.get("page_role") not in PLAN_ROLES:
        return False
    return not is_new_work_sheet(d)


@dataclass(slots=True)
class WiringPlan:
    circuit: str
    panel: str
    ckt: int
    breaker_status: str
    desc: str
    wiring: str  # new | tie_in | none
    home_run: bool
    tie_in_ft: float
    poles: int | None = None
    trip_a: int | None = None
    gfci: bool = False
    schedule_awg: str | None = None
    load_va: float | None = None
    tagged_sheets: list[str] = field(default_factory=list)
    n_tags: int = 0
    keynotes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    schedule_id: str | None = None
    no_schedule_row: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "circuit": self.circuit,
            "panel": self.panel,
            "ckt": self.ckt,
            "breaker_status": self.breaker_status,
            "desc": self.desc,
            "wiring": self.wiring,
            "home_run": self.home_run,
            "tie_in_ft": self.tie_in_ft,
            "poles": self.poles,
            "trip_a": self.trip_a,
            "gfci": self.gfci,
            "schedule_awg": self.schedule_awg,
            "load_va": self.load_va,
            "tagged_sheets": list(self.tagged_sheets),
            "n_tags": self.n_tags,
            "keynotes": list(self.keynotes),
            "reasons": list(self.reasons),
            "schedule_id": self.schedule_id,
            "no_schedule_row": self.no_schedule_row,
        }


def _keynote_texts(page: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for block in page.get("notes") or []:
        if block.get("kind") != "keynotes":
            continue
        for item in block.get("items") or []:
            out.setdefault(str(item.get("number")), str(item.get("text") or ""))
    return out


def _linked_intercept_keynotes(
    page: Mapping[str, Any], tag_points: list[tuple[float, float]]
) -> list[str]:
    """Keynote numbers whose text says INTERCEPT or EXTEND and whose marker
    sits within `KEYNOTE_LINK_IN` of one of the circuit's tags."""
    texts = _keynote_texts(page)
    w_in, h_in = float(page.get("w_in") or 42.0), float(page.get("h_in") or 30.0)
    hits: list[str] = []
    for m in page.get("keynote_markers") or []:
        num = str(m.get("number"))
        text = texts.get(num, "")
        if not INTERCEPT_RE.search(text):
            continue
        mx, my = float(m.get("x", 0.0)), float(m.get("y", 0.0))
        if any(math.hypot((mx - x) * w_in, (my - y) * h_in) <= KEYNOTE_LINK_IN for x, y in tag_points):
            if num not in hits:
                hits.append(num)
    return hits


def _schedule_awg(circuit: Mapping[str, Any]) -> str | None:
    wire = circuit.get("wire") or {}
    return parse_awg(wire.get("hot")) if isinstance(wire, Mapping) else None


def _load_total(circuit: Mapping[str, Any]) -> float | None:
    load = circuit.get("load_va")
    if isinstance(load, Mapping):
        vals = [float(v) for v in load.values() if v is not None]
        return sum(vals) if vals else None
    if load is None:
        return None
    return float(load)


def wiring_plans(
    schedules: Iterable[Mapping[str, Any]],
    pages: Iterable[PageFacts | Mapping[str, Any]],
    *,
    tie_in_ft: float = 25.0,
) -> list[WiringPlan]:
    """Wiring decisions per circuit. `schedules` are WS1 schedule dicts
    (kind panel); `pages` the WS1 page facts (plan sheets are used)."""
    page_dicts = [_facts_dict(p) for p in pages]
    plan_pages = [p for p in page_dicts if (set(p.get("roles") or []) & PLAN_ROLES) or p.get("page_role") in PLAN_ROLES]
    new_work = {str(p.get("sheet_number") or f"p{p.get('page')}"): is_new_work_sheet(p) for p in plan_pages}
    # Tags per circuit per sheet (positions, page fractions).
    tags_by_circuit: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
    for p in plan_pages:
        skey = str(p.get("sheet_number") or f"p{p.get('page')}")
        for t in (p.get("tags") or {}).get("circuit") or []:
            for c in t.get("circuits") or []:
                tags_by_circuit[circuit_key(t.get("panel", "?"), c)][skey].append(
                    (float(t.get("x", 0.0)), float(t.get("y", 0.0)))
                )
    page_by_key = {str(p.get("sheet_number") or f"p{p.get('page')}"): p for p in plan_pages}
    out: list[WiringPlan] = []
    seen: set[str] = set()
    for s in schedules:
        if s.get("kind") != "panel" or not s.get("panel"):
            continue
        panel = s["panel"]
        pname = str(panel.get("name") or "?").upper()
        for c in panel.get("circuits") or []:
            status = str(c.get("status") or "unknown")
            desc = str(c.get("desc") or "").strip()
            ckts = [int(x) for x in (c.get("ckts") or [c.get("ckt")]) if x is not None]
            if not ckts:
                continue
            key = circuit_key(pname, ckts[0])
            seen.update(circuit_key(pname, k) for k in ckts)
            if status in ("spare", "space") or SPARE_RE.match(desc):
                continue
            by_sheet: dict[str, list[tuple[float, float]]] = {}
            for k in ckts:
                for skey, pts in tags_by_circuit.get(circuit_key(pname, k), {}).items():
                    by_sheet.setdefault(skey, []).extend(pts)
            tagged_new = [skey for skey in by_sheet if new_work.get(skey)]
            n_tags = sum(len(v) for v in by_sheet.values())
            existing_load = (
                bool(EXISTING_LOAD_RE.search(desc))
                or status in ("existing", "existing_reused")
                or bool(EXISTING_MARK_RE.search(desc))
            )
            named_load = bool(desc) and not existing_load
            linked: list[str] = []
            for skey, pts in by_sheet.items():
                for num in _linked_intercept_keynotes(page_by_key[skey], pts):
                    linked.append(f"{skey}:{num}")
            row_keynotes = [str(k) for k in (c.get("keynotes") or [])]
            basis = str(c.get("status_basis") or "")
            if row_keynotes and INTERCEPT_RE.search(basis):
                linked.extend(f"schedule:{k}" for k in row_keynotes)
            reasons: list[str] = []
            if existing_load and linked:
                wiring, home_run = "tie_in", False
                reasons.append(f"EXISTING LOAD with intercept/extend keynote ({', '.join(linked)}): {tie_in_ft:g} ft tie-in")
            elif named_load or tagged_new:
                wiring, home_run = "new", True
                if named_load:
                    reasons.append(f"schedule names a load ({desc[:40]})")
                if tagged_new:
                    reasons.append(f"tagged on new-work sheet(s) {', '.join(sorted(tagged_new))}")
                reasons.append(f"breaker status {status} does not change the wiring")
            else:
                wiring, home_run = "none", False
                reasons.append(
                    "existing circuit without new-work tags or an intercept keynote"
                    if existing_load
                    else "no load named, no tags"
                )
            out.append(
                WiringPlan(
                    circuit=key,
                    panel=pname,
                    ckt=ckts[0],
                    breaker_status=status,
                    desc=desc,
                    wiring=wiring,
                    home_run=home_run,
                    tie_in_ft=tie_in_ft if wiring == "tie_in" else 0.0,
                    poles=c.get("poles"),
                    trip_a=c.get("trip_a"),
                    gfci=bool(c.get("gfci")),
                    schedule_awg=_schedule_awg(c),
                    load_va=_load_total(c),
                    tagged_sheets=sorted(by_sheet),
                    n_tags=n_tags,
                    keynotes=linked,
                    reasons=reasons,
                    schedule_id=s.get("schedule_id"),
                )
            )
    # Tagged circuits without a schedule row (panel not parsed).
    for key in sorted(tags_by_circuit):
        if key in seen:
            continue
        by_sheet = tags_by_circuit[key]
        tagged_new = [skey for skey in by_sheet if new_work.get(skey)]
        if not tagged_new:
            continue
        panel, _, ckt = key.rpartition("-")
        out.append(
            WiringPlan(
                circuit=key,
                panel=panel,
                ckt=int(ckt) if ckt.isdigit() else 0,
                breaker_status="unknown",
                desc="",
                wiring="new",
                home_run=True,
                tie_in_ft=0.0,
                tagged_sheets=sorted(by_sheet),
                n_tags=sum(len(v) for v in by_sheet.values()),
                reasons=[f"tagged on new-work sheet(s) {', '.join(sorted(tagged_new))}; no schedule row"],
                no_schedule_row=True,
            )
        )
    return out


# Conductor sizing (Q15) -------------------------------------------------------------------

LIGHTING_RE = re.compile(r"LIGHT|LTG|LIGHTING|LUMIN|EXIT\b|EM\s+LIGHT", re.I)
DEFAULT_BRANCH_AMPS = 20.0
DEFAULT_CAP_AWG = "#10"
KVA_THRESHOLD = 50.0


@dataclass(slots=True)
class ConductorSizing:
    awg: str
    basis: str  # schedule | vd | default | lighting_default
    vd_pct: float | None
    vd_upsize: bool
    inputs: dict[str, Any]
    cap_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "awg": self.awg,
            "basis": self.basis,
            "vd_pct": None if self.vd_pct is None else round(self.vd_pct, 3),
            "vd_upsize": self.vd_upsize,
            "inputs": dict(self.inputs),
            "cap_applied": self.cap_applied,
        }


def voltage_drop_pct(amps: float, length_ft: float, awg: str, volts: float, *, phase: int = 1) -> float:
    """VD% = 100 x factor x K x I x L / (CM x V); factor 2 single-phase,
    sqrt(3) three-phase; K 12.9 ohm-cmil/ft copper."""
    cm = AWG_CIRCULAR_MILS[awg]
    factor = math.sqrt(3) if phase == 3 else 2.0
    if volts <= 0:
        return math.inf
    return 100.0 * factor * K_COPPER_OHM_CMIL_FT * amps * length_ft / (cm * volts)


def circuit_volts(panel_volts: str | None, poles: int | None) -> float:
    """Line-to-neutral volts for one pole, line-to-line otherwise, from a
    panel voltage text (`120/208`, `277/480`, `480/277`); 120 V default."""
    nums = [float(n) for n in re.findall(r"\d{2,3}", str(panel_volts or ""))]
    if not nums:
        return 120.0 if (poles or 1) == 1 else 208.0
    ln, ll = min(nums), max(nums)
    return ln if (poles or 1) == 1 else ll


def load_amps(load_va: float | None, volts: float, *, poles: int | None = None) -> float | None:
    """I = load_va / volts. A schedule load under `KVA_THRESHOLD` is read as
    kVA (the usual column unit) and converted. Three-pole loads divide by
    sqrt(3) x line-to-line volts."""
    if load_va is None or load_va <= 0 or volts <= 0:
        return None
    va = load_va * 1000.0 if load_va < KVA_THRESHOLD else load_va
    if (poles or 1) == 3:
        return va / (math.sqrt(3) * volts)
    return va / volts


def _upsize(start: str, amps: float, length_ft: float, volts: float, vd_max: float, *, phase: int, cap: str | None) -> tuple[str, float, bool, bool]:
    idx = awg_index(start)
    cap_idx = awg_index(cap) if cap else len(AWG_ORDER) - 1
    awg = AWG_ORDER[idx]
    vd = voltage_drop_pct(amps, length_ft, awg, volts, phase=phase)
    upsized = False
    capped = False
    while vd > vd_max and idx < len(AWG_ORDER) - 1:
        if idx >= cap_idx:
            capped = True
            break
        idx += 1
        awg = AWG_ORDER[idx]
        vd = voltage_drop_pct(amps, length_ft, awg, volts, phase=phase)
        upsized = True
    return awg, vd, upsized, capped


def size_conductor(
    *,
    kind: str,
    length_ft: float,
    volts: float,
    load_va: float | None = None,
    trip_a: int | None = None,
    schedule_awg: str | None = None,
    poles: int | None = 1,
    vd_max_pct: float = 3.0,
    min_awg: str = "#12",
) -> ConductorSizing:
    """Q15 sizing. `kind` is lighting or power; `length_ft` the one-way
    route length (collector plus home run)."""
    phase = 3 if (poles or 1) == 3 else 1
    floor = min_awg if min_awg in AWG_CIRCULAR_MILS else "#12"
    amps = load_amps(load_va, volts, poles=poles)
    base_inputs = {
        "kind": kind,
        "length_ft": round(float(length_ft), 2),
        "volts": volts,
        "k_ohm_cmil_ft": K_COPPER_OHM_CMIL_FT,
        "phase": phase,
        "factor": math.sqrt(3) if phase == 3 else 2.0,
        "min_awg": floor,
    }
    if kind == "lighting":
        if amps is None:
            inputs = {**base_inputs, "amps": None}
            return ConductorSizing(floor, "lighting_default", None, False, inputs)
        awg, vd, upsized, _capped = _upsize(floor, amps, length_ft, volts, vd_max_pct, phase=phase, cap=None)
        inputs = {**base_inputs, "amps": amps, "cm": AWG_CIRCULAR_MILS[awg], "vd_max_pct": vd_max_pct}
        return ConductorSizing(awg, "vd" if upsized else "lighting_default", vd, upsized, inputs)
    # power
    sched = parse_awg(schedule_awg) if schedule_awg else None
    if sched is not None:
        awg = sched if awg_index(sched) >= awg_index(floor) else floor
        vd = voltage_drop_pct(amps, length_ft, awg, volts, phase=phase) if amps is not None else None
        inputs = {**base_inputs, "amps": amps, "schedule_awg": sched, "cm": AWG_CIRCULAR_MILS[awg]}
        return ConductorSizing(awg, "schedule", vd, False, inputs)
    if amps is not None:
        awg, vd, upsized, _capped = _upsize(floor, amps, length_ft, volts, vd_max_pct, phase=phase, cap=None)
        inputs = {**base_inputs, "amps": amps, "cm": AWG_CIRCULAR_MILS[awg], "vd_max_pct": vd_max_pct}
        return ConductorSizing(awg, "vd", vd, upsized, inputs)
    amps = float(trip_a) if trip_a else DEFAULT_BRANCH_AMPS
    amps = min(amps, DEFAULT_BRANCH_AMPS)
    awg, vd, upsized, capped = _upsize(floor, amps, length_ft, volts, vd_max_pct, phase=phase, cap=DEFAULT_CAP_AWG)
    inputs = {**base_inputs, "amps": amps, "cm": AWG_CIRCULAR_MILS[awg], "vd_max_pct": vd_max_pct, "cap_awg": DEFAULT_CAP_AWG}
    return ConductorSizing(awg, "default", vd, upsized, inputs, cap_applied=capped)


def is_lighting_circuit(desc: str | None, *, fixture_tagged: bool = False) -> bool:
    return fixture_tagged or bool(LIGHTING_RE.search(str(desc or "")))


# Load kinds and panel schedule sources ------------------------------------------------------

LOAD_KINDS: tuple[str, ...] = ("lighting", "receptacle", "equipment", "feeder", "spare", "unknown")
RECEPTACLE_RE = re.compile(
    r"\bRECEPT(?:ACLES?|S)?\b|\bRECPTS?\b|\bRCPTS?\b|\bRECEPS?\b|\bRECS?\b|\bOUTLETS?\b|\bPOWER\b|\bGFC?I\b|\bCONVENIENCE\b|\bGEN(?:ERAL)?\.?\s+PURPOSE\b|\bDUPLEX\b|\bQUAD(?:PLEX)?\b",
    re.I,
)
FEEDER_RE = re.compile(r"TRANSFORMER|\bXFMR\b|\bFEEDER\b|\bSUB-?PANEL\b|\bPANEL\b|\bPNL\b|\bMCC\b|\bDISTRIBUTION\b", re.I)
FEEDER_MIN_TRIP_A = 70
MODEL_PANEL_NAME_RE = re.compile(r"(?:PANEL(?:BOARD)?|PNL)\s*:?\s*['\"]?(?P<name>[A-Z0-9][A-Z0-9\-]{0,7})['\"]?", re.I)
PANEL_STOP_NAMES: frozenset[str] = frozenset({"AND", "OR", "THE", "TO", "FOR", "INTO", "STUB", "WITH", "PER", "SCHEDULE", "SCHEDULES", "BOARD", "TOTALS", "TOTAL", "S", "IS", "AS", "AT", "ON", "IN", "OF", "BY"})
MODEL_HEADER_ROLES: dict[str, str] = {
    "CKT": "ckt",
    "CKT NO": "ckt",
    "CKT #": "ckt",
    "CIRCUIT": "ckt",
    "CIRCUIT NO": "ckt",
    "CIRCUIT #": "ckt",
    "NO": "ckt",
    "#": "ckt",
    "CIRCUIT DESCRIPTION": "desc",
    "LOAD DESCRIPTION": "desc",
    "DESCRIPTION": "desc",
    "LOAD NAME": "desc",
    "TRIP": "trip",
    "AMPS": "trip",
    "AMP": "trip",
    "BKR": "trip",
    "BREAKER": "trip",
    "OCPD": "trip",
    "OCPD AMPS": "trip",
    "POLES": "poles",
    "POLE": "poles",
    "P": "poles",
    "WIRE": "wire",
    "WIRE SIZE": "wire",
    "KEY": "key",
    "NOTE": "key",
    "NOTES": "key",
    "REMARKS": "key",
}
LOAD_HEADER_RE = re.compile(r"^(?:PHASE\s*)?[ABC](?:\s*\(?K?VA\)?)?$|^(?:LOAD\s*)?\(?K?VA\)?$|^LOAD$", re.I)
VOLTS_NOTE_RE = re.compile(r"VOLT(?:S|AGE)?\s*[:=]?\s*(\d{3}\s*[/Y]\s*\d{3}|\d{3})", re.I)
PHASE_NOTE_RE = re.compile(r"PHASES?\s*[:=]?\s*([13])\b", re.I)
TRUNCATED_RE = re.compile(r"(?:\.\.\.|\u2026)\s*$")
VA_CELL_RE = re.compile(r"^\s*(?P<n>\d+(?:[.,]\d+)?)\s*(?P<k>K)?\s*VA?\s*$", re.I)
TRIP_CELL_RE = re.compile(r"^\s*(?P<amps>\d{2,4})\s*A?(?:\s*/\s*(?P<poles>[123])\s*P?)?\s*$", re.I)
SPACE_CELL_RE = re.compile(r"^\s*(?:SPACE|BLANK)\s*$", re.I)
SPARE_CELL_RE = re.compile(r"^\s*SPARE\s*$", re.I)
PLACEHOLDER_CELL_RE = re.compile(r"^\s*(?:-+|\u2013|\u2014|n/?a)?\s*$", re.I)
GFCI_RE = re.compile(r"\bGF(?:C)?I\b|GROUND\s+FAULT", re.I)


def load_kind(desc: str | None, *, poles: int | None = None, trip_a: int | None = None, lighting: bool = False) -> str:
    """The load family of one schedule circuit: `lighting` (the lighting
    regex or a fixture-tagged circuit), `receptacle` (receptacle, outlet or
    general power wording), `feeder` (a transformer or panel feed, or a
    breaker of `FEEDER_MIN_TRIP_A` or more), `spare` (spare or space),
    `equipment` (any other named load) or `unknown` (nothing named)."""
    text = str(desc or "").strip()
    if lighting or (text and LIGHTING_RE.search(text) and not FEEDER_RE.search(text)):
        return "lighting"
    if not text:
        return "unknown"
    if SPARE_RE.match(text):
        return "spare"
    if FEEDER_RE.search(text) or (trip_a is not None and trip_a >= FEEDER_MIN_TRIP_A):
        return "feeder"
    if RECEPTACLE_RE.search(text):
        return "receptacle"
    return "equipment"


def usable_panel_schedule(schedule: Mapping[str, Any]) -> bool:
    """A panel schedule the measure can route from: kind panel whose rows
    carry distinct circuit numbers (a one-row schedule passes) and one row
    naming a load, a trip or a pole count. A title regex false positive
    ("PANEL AND" in a spec paragraph: every row circuit 1, nothing else)
    fails this."""
    if schedule.get("kind") != "panel" or not schedule.get("panel"):
        return False
    circuits = schedule["panel"].get("circuits") or []
    if not circuits:
        return False
    numbers = {int(c.get("ckt")) for c in circuits if c.get("ckt") is not None}
    if len(circuits) > 1 and len(numbers) < 2:
        return False
    return any(str(c.get("desc") or "").strip() or c.get("trip_a") or c.get("poles") for c in circuits)


def schedule_completeness(schedule: Mapping[str, Any]) -> float:
    """How much a panel schedule says about its circuits: one point per
    circuit with a description plus a trip or pole count, half a point per
    circuit with a load value. The merge keeps the more complete source."""
    score = 0.0
    for c in (schedule.get("panel") or {}).get("circuits") or []:
        desc = str(c.get("desc") or "").strip()
        if desc and (c.get("trip_a") or c.get("poles")):
            score += 1.0
        if _load_total(c):
            score += 0.5
    return score


def _header_role(label: Any) -> str:
    text = re.sub(r"[.\s]+", " ", str(label or "")).strip().upper()
    if not text:
        return ""
    role = MODEL_HEADER_ROLES.get(text)
    if role:
        return role
    if LOAD_HEADER_RE.match(text):
        return "load"
    if "DESCRIPTION" in text or text.startswith("LOAD NAME"):
        return "desc"
    if text.startswith(("OCPD", "BKR", "BREAKER", "TRIP")):
        return "trip"
    if text.startswith("POLE"):
        return "poles"
    if text.startswith(("CKT", "CIRCUIT")):
        return "ckt"
    if text.startswith("WIRE"):
        return "wire"
    if text.startswith(("KEY", "NOTE", "REMARK")):
        return "key"
    return ""


def _parse_va_cell(text: Any) -> float | None:
    raw = str(text or "").strip()
    if not raw or TRUNCATED_RE.search(raw) or PLACEHOLDER_CELL_RE.match(raw):
        return None
    m = VA_CELL_RE.match(raw)
    if not m:
        return None
    value = float(m.group("n").replace(",", "."))
    return value * 1000.0 if m.group("k") else value


def _parse_trip_cell(text: Any) -> tuple[int | None, int | None]:
    raw = str(text or "").strip()
    m = TRIP_CELL_RE.match(raw)
    if not m:
        return None, None
    return int(m.group("amps")), int(m.group("poles")) if m.group("poles") else None


def _side_columns(columns: Sequence[str], roles: Sequence[str]) -> tuple[dict[str, dict[str, list[int]]], list[tuple[int, int | None, str]], bool]:
    """Columns per side (L, R) by role, the load column pairs (left index,
    right index or None when shared, phase letter) and whether the table
    is two-sided (odd circuits left, even circuits right)."""
    ckt_cols = [i for i, r in enumerate(roles) if r == "ckt"]
    two_sided = len(ckt_cols) >= 2
    sides: dict[str, dict[str, list[int]]] = {"L": {}, "R": {}}
    loads: list[tuple[int, int | None, str]] = []
    side = "L"
    seen: set[str] = set()
    i = 0
    while i < len(columns):
        role = roles[i]
        if role == "load":
            label = str(columns[i]).strip().upper()
            j = i
            while j + 1 < len(columns) and roles[j + 1] == "load" and str(columns[j + 1]).strip().upper() == label:
                j += 1
            phase = label[0] if label[:1] in ("A", "B", "C") else "A"
            if two_sided and j > i:
                loads.append((i, j, phase))
            else:
                loads.append((i, None, phase))
            i = j + 1
            continue
        if role and two_sided and side == "L" and role in seen and len(seen) >= 2:
            side = "R"
            seen = set()
        if role:
            sides[side].setdefault(role, []).append(i)
            seen.add(role)
        i += 1
    return sides, loads, two_sided


def panel_schedule_from_rows(schedule: Mapping[str, Any]) -> dict[str, Any] | None:
    """A WS1-shaped panel schedule (kind panel, `panel.circuits` with ckt,
    ckts, poles, trip_a, desc, status, load_va, wire, keynotes, gfci) from
    a reader schedule row: `columns` plus `rows` (lists in column order or
    dicts keyed by column), `name` or `schedule_id` for the panel name,
    `notes` for the voltage and phases. Two-sided tables (odd circuits on
    the left, even on the right, each phase column doubled per side) are
    read per side; multi-pole continuation rows ("--" under a 2P or 3P
    breaker) extend the breaker's circuit numbers; truncated load cells
    ("1535...") read as unknown. None when the row has no circuit column
    or no rows; `source` is `model_rows`."""
    columns = [str(c) for c in (schedule.get("columns") or [])]
    rows = schedule.get("rows") or []
    if not columns or not rows:
        return None
    roles = [_header_role(c) for c in columns]
    if "ckt" not in roles:
        return None
    sides, loads, two_sided = _side_columns(columns, roles)
    name = ""
    for cand in (schedule.get("name"), schedule.get("title"), schedule.get("schedule_id")):
        text = str(cand or "").strip()
        if not text:
            continue
        m = MODEL_PANEL_NAME_RE.search(text)
        token = (m.group("name") if m else text).upper()
        if m is None and not re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{0,7}", token):
            continue
        if token in PANEL_STOP_NAMES:
            continue
        name = token
        break
    if not name:
        return None
    notes = str(schedule.get("notes") or "")
    volts = schedule.get("volts")
    if not volts:
        m = VOLTS_NOTE_RE.search(notes)
        volts = re.sub(r"\s+", "", m.group(1)) if m else None
    phase = schedule.get("phase")
    if phase is None:
        m = PHASE_NOTE_RE.search(notes)
        phase = int(m.group(1)) if m else None
    existing = bool(schedule.get("existing")) or bool(re.search(r"\(E\)|\bEXISTING\b", str(schedule.get("name") or ""), re.I))
    circuits: list[dict[str, Any]] = []
    rows_total = 0
    rows_valid = 0
    last_breaker: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping):
            cells = [str(row.get(c, "") if row.get(c) is not None else "") for c in columns]
        elif isinstance(row, (list, tuple)):
            cells = [str(v) if v is not None else "" for v in row]
        else:
            continue
        cells += [""] * (len(columns) - len(cells))
        for side in ("L", "R") if two_sided else ("L",):
            cols = sides.get(side) or {}
            ckt_cols = cols.get("ckt") or []
            if not ckt_cols:
                continue
            ckt_text = cells[ckt_cols[0]].strip()
            if not re.fullmatch(r"\d{1,3}", ckt_text):
                continue
            ckt = int(ckt_text)
            rows_total += 1
            desc = " ".join(cells[i].strip() for i in cols.get("desc", []) if cells[i].strip())
            desc_blank = not desc or PLACEHOLDER_CELL_RE.match(desc)
            trip: int | None = None
            poles_from_trip: int | None = None
            for i in cols.get("trip", []):
                trip, poles_from_trip = _parse_trip_cell(cells[i])
                if trip is not None:
                    break
            poles: int | None = None
            for i in cols.get("poles", []):
                if re.fullmatch(r"[123]", cells[i].strip()):
                    poles = int(cells[i].strip())
                    break
            if poles is None:
                poles = poles_from_trip
            load_va: dict[str, float | None] = {"A": None, "B": None, "C": None}
            for left, right, ph in loads:
                if right is None and two_sided and side == "R":
                    # A phase column printed once per row is read on the left side only.
                    continue
                value = _parse_va_cell(cells[left if side == "L" or right is None else right])
                if value is not None:
                    load_va[ph] = (load_va.get(ph) or 0.0) + value
            wire_text = next((cells[i].strip() for i in cols.get("wire", []) if cells[i].strip()), "")
            keys = [cells[i].strip() for i in cols.get("key", []) if cells[i].strip() and not PLACEHOLDER_CELL_RE.match(cells[i])]
            prev = last_breaker.get(side)
            if desc_blank and trip is None and poles is None and prev is not None and (prev.get("poles") or 1) > len(prev["ckts"]):
                prev["ckts"].append(ckt)
                for ph, v in load_va.items():
                    if v is not None:
                        prev["load_va"][ph] = (prev["load_va"].get(ph) or 0.0) + v
                rows_valid += 1
                continue
            if desc_blank:
                desc = ""
            if SPACE_CELL_RE.match(desc):
                status = "space"
            elif SPARE_CELL_RE.match(desc):
                status = "spare"
            else:
                status = "unknown"
            circuit = {
                "ckt": ckt,
                "ckts": [ckt],
                "side": side,
                "poles": poles,
                "trip_a": trip,
                "desc": desc,
                "status": status,
                "status_basis": None,
                "signals": {},
                "keynotes": keys,
                "load_va": load_va,
                "wire": {"hot": parse_awg(wire_text).lstrip("#") if parse_awg(wire_text) else None, "neutral": None, "ground": None},
                "conduit": None,
                "vd_pct": None,
                "gfci": bool(GFCI_RE.search(desc)),
                "bbox": None,
                "raw": cells,
                "problems": [],
            }
            circuits.append(circuit)
            rows_valid += 1
            if status != "space":
                last_breaker[side] = circuit
    if not circuits:
        return None
    circuits.sort(key=lambda c: c["ckt"])
    sheet = schedule.get("sheet") if isinstance(schedule.get("sheet"), Mapping) else {}
    page = schedule.get("page")
    if page is None:
        page = sheet.get("page")
    return {
        "kind": "panel",
        "page": int(page or 0),
        "bbox": None,
        "title": str(schedule.get("name") or f"PANEL {name}"),
        "parse_confidence": round(rows_valid / rows_total, 4) if rows_total else 0.0,
        "columns": columns,
        "unparsed_rows": [],
        "panel": {
            "name": name,
            "existing": existing,
            "status_basis": None,
            "volts": volts,
            "phase": phase,
            "circuits": circuits,
            "validation": {"rows_total": rows_total, "rows_valid": rows_valid},
        },
        "luminaires": None,
        "equipment": None,
        "feeders": None,
        "rows_raw": [],
        "source": "model_rows",
        "schedule_id": str(schedule.get("row_id") or schedule.get("schedule_id") or name),
        "document_id": schedule.get("document_id") or sheet.get("document_id"),
        "sheet_number": schedule.get("sheet_number") or sheet.get("sheet_number"),
    }


def merge_panel_schedules(
    ws1: Iterable[Mapping[str, Any]], model: Iterable[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The panel schedules a measure routes from: the usable WS1 panel
    schedules, then the reader's panel schedule rows converted with
    `panel_schedule_from_rows`; per panel name the more complete source
    wins (ties keep WS1). Returns (schedules, info) with info listing the
    WS1 schedules dropped as unusable and the panels taken from model rows."""
    by_name: dict[str, dict[str, Any]] = {}
    info: dict[str, Any] = {"dropped_ws1": [], "from_model_rows": [], "ws1": [], "replaced_ws1": []}
    for s in ws1:
        if s.get("kind") != "panel" or not s.get("panel"):
            continue
        name = str(s["panel"].get("name") or "").upper()
        if not name:
            continue
        if not usable_panel_schedule(s):
            info["dropped_ws1"].append(name)
            continue
        if name not in by_name:
            by_name[name] = dict(s)
            info["ws1"].append(name)
    for raw in model:
        if str(raw.get("schedule_type") or raw.get("kind") or "").lower() != "panel":
            continue
        if str(raw.get("status") or "active") != "active":
            continue
        conv = panel_schedule_from_rows(raw)
        if conv is None or not usable_panel_schedule(conv):
            continue
        name = conv["panel"]["name"]
        current = by_name.get(name)
        if current is not None and schedule_completeness(current) >= schedule_completeness(conv):
            continue
        if current is not None:
            info["replaced_ws1"].append(name)
            info["ws1"].remove(name)
        by_name[name] = conv
        info["from_model_rows"].append(name)
    return list(by_name.values()), info


# Survey (B2) ----------------------------------------------------------------------------------

SURVEY_CLASSES: tuple[str, ...] = ("tagged", "untagged_text", "vector_text", "scanned", "no_plans")
TAGGED_MIN_TAGS = 8
TAGGED_MIN_PAGE_TAGS = 5
TEXT_CLASSES_WITH_TEXT: frozenset[str] = frozenset({"cad_text", "scanned_ocr"})


@dataclass(slots=True)
class SurveyClass:
    cls: str
    plan_pages: int
    tagged_pages: int
    tags: int
    text_classes: dict[str, int]
    conventions: dict[str, str]
    roles_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.cls,
            "plan_pages": self.plan_pages,
            "tagged_pages": self.tagged_pages,
            "tags": self.tags,
            "text_classes": dict(self.text_classes),
            "conventions": dict(self.conventions),
            "roles_fallback": self.roles_fallback,
        }


def survey_class(docs: Iterable[DocFacts | Mapping[str, Any]]) -> SurveyClass:
    """Class of a drawing set from its plan pages: `tagged` when the circuit
    tags reach `TAGGED_MIN_TAGS` overall with one page at
    `TAGGED_MIN_PAGE_TAGS`; else `untagged_text` when most plan pages carry a
    text layer; else `vector_text`; else `scanned`; `no_plans` without a
    plan page. Sets with no plan-role page at all (one sheet per file, civil
    sets) fall back to their unroled pages with `roles_fallback` True. Also
    the tag convention per tagged sheet."""
    plan_pages: list[dict[str, Any]] = []
    unroled: list[dict[str, Any]] = []
    for doc in docs:
        d = doc.to_dict() if isinstance(doc, DocFacts) else dict(doc)
        for p in d.get("pages") or []:
            pd = _facts_dict(p)
            if (set(pd.get("roles") or []) & PLAN_ROLES) or pd.get("page_role") in PLAN_ROLES:
                plan_pages.append(pd)
            elif pd.get("page_role") in (None, "other") and not pd.get("roles"):
                unroled.append(pd)
    roles_fallback = False
    if not plan_pages and unroled:
        # WS1 finds no plan role on one-sheet-per-file sets and civil sets
        # (no title block slot to learn from); their unroled pages are the
        # plan candidates for the survey, flagged as a fallback.
        plan_pages = unroled
        roles_fallback = True
    if not plan_pages:
        return SurveyClass("no_plans", 0, 0, 0, {}, {})
    classes = Counter(str(p.get("text_class") or "empty") for p in plan_pages)
    censuses = [tag_census(p) for p in plan_pages]
    total = sum(c.tags for c in censuses)
    tagged_pages = sum(1 for c in censuses if c.tags >= TAGGED_MIN_PAGE_TAGS)
    conventions = {
        c.sheet_key: classify_tag_convention(p).convention
        for p, c in zip(plan_pages, censuses, strict=True)
        if c.tags > 0
    }
    if total >= TAGGED_MIN_TAGS and tagged_pages >= 1:
        cls = "tagged"
    else:
        n = len(plan_pages)
        with_text = sum(v for k, v in classes.items() if k in TEXT_CLASSES_WITH_TEXT)
        vector = classes.get("vector_text", 0)
        if with_text * 2 >= n:
            cls = "untagged_text"
        elif vector > 0 and (vector * 2 >= n or vector >= with_text):
            cls = "vector_text"
        else:
            cls = "scanned"
    return SurveyClass(
        cls, len(plan_pages), tagged_pages, total, dict(classes), conventions, roles_fallback
    )
