"""Route synthesis (WS3 step B5; docs/AI_ESTIMATING_BUILD_SPEC.md 5.1 measure
bases, 5.4 MeasureOutput routes, 6 WS3).

Branch routes are not drawn on TI plans (7154 E2.01 keynote 1: "extend
circuiting from junction box to outlets with same circuit numbers"), so
code synthesizes them from what IS on the sheets: circuit tags grouped per
circuit (WS3 circuits), the panel positions on the overall plan (WS1 panel
locations), the scale of each sheet (WS3 geometry) and the registration of
the enlarged plan onto the overall plan (WS3 geometry). Per circuit:

1. Device set: tags of the circuit on new-work plans, deduplicated across
   registered sheets (the same circuit within 1 ft after registration
   counts once; the larger-scale sheet keeps the device). Devices on two
   sheets that do not register form two sub-trees, each with its own home
   run (flag `circuit_split_unregistered`).
2. Collector (inside the work area): the rectilinear minimum spanning tree
   of the device points (`rect_mst`, Manhattan edges) plus the Manhattan leg
   from the device nearest the exit point E to E, at the device sheet's
   scale, times `route_factor`. E is the panel position mapped into the
   device sheet's frame and clamped into the work-area box (the bounding
   box of the sheet's circuit tags expanded by 0.25 in). The clamp keeps
   the total equal to the direct Manhattan distance; it only decides which
   part is collector (#12) and which is home run.
3. Home run: Manhattan from E (mapped onto the panel sheet) to the panel,
   at the panel sheet's scale, times `route_factor`, plus `panel_rise_ft`
   and `floor_to_floor_ft` per floor when the sheets name different levels.
4. EXISTING LOAD circuits with an intercept or extend keynote (WS3 circuits
   `wiring == "tie_in"`): no home run, one `tie_in` segment of
   `existing_tie_in_ft`. Breaker status never changes the wiring.
5. Drops: `drop_ft_per_device` per device (default 0, calibrated in B9).
6. Unknown panel location, no registration or no scale: the home run is the
   `homerun_default_ft` allowance (basis `assumed_allowance`, flag
   `homerun_allowance`), never nothing.
7. Conductors (Q15, `plans_reader_circuits.size_conductor`): lighting
   circuits #12 unless voltage drop exceeds the limit; power home runs from
   the schedule wire column, else voltage drop, else a 20 A default capped
   at #10; collectors #12 when the breaker is 20 A or less. H + N + G in one
   raceway per circuit (`branch_conduit`, the renderer's `_CONDUIT_RE`
   form) for the EMT wiring method; MC or NM cable rows otherwise.

Tiers below the tagged one (never an empty branch row when new devices
exist): `equipment_routes` (a home-run allowance per equipment connection),
`untagged_routes` (device positions without circuit tags, grouped into
synthetic circuits of at most `receptacles_per_circuit`, basis
`assumed_route_untagged`) and `density_routes` (device counts x the WS7 band
median, basis `assumed_density`, finding `branch_density_only`).

`branch_quantities` sums the segments into the M quantity rows of 5.4 by
conductor size and raceway. Everything here is pure: no model, no I/O, no
prices. Positions in results are page fractions (5.1); sheet inches stay
internal.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.services import plans_reader_circuits as circuits_mod
from app.services import plans_reader_geometry as geometry
from app.services import plans_reader_measure_bands as measure_bands
from app.services.estimating_contracts import ASSUMED_CAVEAT, MEASURE_BASES, make_row_id

__all__ = [
    "Conventions",
    "SheetFrame",
    "Device",
    "CircuitSpec",
    "PanelInfo",
    "RouteSegment",
    "CircuitRoute",
    "RegistrationGraph",
    "rect_mst",
    "clamp_to_box",
    "work_box",
    "synthesize_routes",
    "equipment_routes",
    "untagged_routes",
    "distribute_devices",
    "mark_distributed_routes",
    "density_routes",
    "BAND_BASIS",
    "branch_quantities",
    "route_totals",
    "weakest_basis",
    "level_of",
]

BASIS_RANK: dict[str, int] = {b: i for i, b in enumerate(MEASURE_BASES)}
# The per-device band tier's basis: `assumed_band` once the contracts enum
# carries it (spec 5.1 names `assumed_density` today), else `assumed_density`.
BAND_BASIS: str = "assumed_band" if "assumed_band" in MEASURE_BASES else "assumed_density"
UNTAGGED_DISTRIBUTION_FLAG = "schedule_distribution"
DEDUPE_FT = 1.0
WORK_BOX_PAD_IN = 0.25
MAX_DEVICES_PER_CIRCUIT = 400
SEGMENT_KINDS: tuple[str, ...] = ("collector", "homerun", "tie_in", "drop", "allowance", "switch_leg")
WIRING_METHODS: tuple[str, ...] = ("emt", "mc", "nm")
LEVEL_RE = re.compile(r"\b(?:LEVEL|FLOOR|LVL)\s*(\d{1,2})\b|\b(\d{1,2})(?:ST|ND|RD|TH)\s+FLOOR\b", re.I)
VOLTS_RE = re.compile(r"\d{2,3}")


def weakest_basis(bases: Iterable[str]) -> str:
    """The weakest measure basis among `bases` (5.1 order); `assumed_route`
    when empty."""
    worst = -1
    out = "assumed_route"
    for b in bases:
        r = BASIS_RANK.get(b, -1)
        if r > worst:
            worst, out = r, b
    return out


def level_of(title: str | None) -> int | None:
    m = LEVEL_RE.search(str(title or ""))
    if not m:
        return None
    return int(m.group(1) or m.group(2))


# Conventions ------------------------------------------------------------------------


@dataclass(frozen=True)
class Conventions:
    """The versioned G3 branch conventions (spec 5.7 settings, 6 WS3 5.2).
    `min_branch_awg` is the canonical `#12` key."""

    version: str = "g3-branch-2026-10-a"
    wiring_method: str = "emt"
    branch_conduit: str = '3/4"C EMT'
    min_branch_awg: str = "#12"
    vd_max_pct: float = 3.0
    route_factor: float = 1.0
    drop_ft_per_device: float = 0.0
    panel_rise_ft: float = 0.0
    existing_tie_in_ft: float = 25.0
    homerun_default_ft: float = 150.0
    floor_to_floor_ft: float = 15.0
    receptacles_per_circuit: int = 6
    control_cable_ft_per_device: float = 25.0

    @classmethod
    def from_settings(cls, settings: Any = None, **overrides: Any) -> Conventions:
        """Read the `plans_reader_*` settings (app.core.config); `overrides`
        win (calibration grids, tests)."""
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        awg = circuits_mod.parse_awg(getattr(settings, "plans_reader_min_branch_awg", 12)) or "#12"
        values = {
            "version": str(getattr(settings, "plans_reader_measure_conventions_version", cls.version)),
            "branch_conduit": str(getattr(settings, "plans_reader_branch_conduit", cls.branch_conduit)),
            "min_branch_awg": awg,
            "vd_max_pct": float(getattr(settings, "plans_reader_vd_max_pct", cls.vd_max_pct)),
            "route_factor": float(getattr(settings, "plans_reader_route_factor", cls.route_factor)),
            "drop_ft_per_device": float(getattr(settings, "plans_reader_drop_ft_per_device", 0.0)),
            "panel_rise_ft": float(getattr(settings, "plans_reader_panel_rise_ft", 0.0)),
            "existing_tie_in_ft": float(getattr(settings, "plans_reader_existing_tie_in_ft", 25.0)),
            "homerun_default_ft": float(getattr(settings, "plans_reader_homerun_default_ft", 150.0)),
            "floor_to_floor_ft": float(getattr(settings, "plans_reader_floor_to_floor_ft", 15.0)),
            "receptacles_per_circuit": int(getattr(settings, "plans_reader_receptacles_per_circuit", 6)),
            "control_cable_ft_per_device": float(
                getattr(settings, "plans_reader_control_cable_ft_per_device", 25.0)
            ),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def replace(self, **changes: Any) -> Conventions:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "wiring_method": self.wiring_method,
            "branch_conduit": self.branch_conduit,
            "min_branch_awg": self.min_branch_awg,
            "vd_max_pct": self.vd_max_pct,
            "route_factor": self.route_factor,
            "drop_ft_per_device": self.drop_ft_per_device,
            "panel_rise_ft": self.panel_rise_ft,
            "existing_tie_in_ft": self.existing_tie_in_ft,
            "homerun_default_ft": self.homerun_default_ft,
            "floor_to_floor_ft": self.floor_to_floor_ft,
            "receptacles_per_circuit": self.receptacles_per_circuit,
            "control_cable_ft_per_device": self.control_cable_ft_per_device,
        }


# Inputs -------------------------------------------------------------------------------


@dataclass
class SheetFrame:
    """One plan sheet's frame: size, scale (page level, or `scale_at` per
    viewport from `plans_reader_geometry.PageScales`), title and the ids a
    route segment cites."""

    sheet_key: str
    document_id: str
    page: int
    w_in: float
    h_in: float
    ft_per_in: float | None
    title: str | None = None
    scale_at: Callable[[float, float], float | None] | None = None
    new_work: bool = True

    def to_in(self, x: float, y: float) -> tuple[float, float]:
        return x * self.w_in, y * self.h_in

    def to_frac(self, x_in: float, y_in: float) -> list[float]:
        return [round(x_in / (self.w_in or 1.0), 4), round(y_in / (self.h_in or 1.0), 4)]

    def scale_for(self, x: float, y: float) -> float | None:
        """ft per inch at a page-fraction point (viewport first, else the
        page scale; None on NTS or a conflict)."""
        if self.scale_at is not None:
            try:
                s = self.scale_at(x, y)
            except Exception:  # noqa: BLE001 - a broken callback reads as unknown
                s = None
            if s is not None:
                return float(s)
        return self.ft_per_in

    @property
    def level(self) -> int | None:
        return level_of(self.title)


@dataclass
class Device:
    document_id: str
    page: int
    sheet_key: str
    x: float
    y: float
    tag: str | None = None
    source: str = "text_tag"  # text_tag | count_row | census | equipment
    fixture_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "page": self.page,
            "sheet_key": self.sheet_key,
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "tag": self.tag,
            "source": self.source,
            "fixture_type": self.fixture_type,
        }


@dataclass
class CircuitSpec:
    """One circuit to route: the WS3 wiring decision plus its devices."""

    circuit_id: str
    panel: str
    ckts: list[int]
    devices: list[Device] = field(default_factory=list)
    wiring: str = "new"  # new | tie_in | none
    breaker_status: str = "unknown"
    desc: str = ""
    poles: int | None = 1
    trip_a: int | None = None
    schedule_awg: str | None = None
    load_va: float | None = None
    gfci: bool = False
    lighting: bool = False
    tie_in_ft: float = 0.0
    keynotes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    schedule_id: str | None = None
    row_ids: list[str] = field(default_factory=list)
    # Untagged devices distributed onto this schedule circuit by load kind (WS3 untagged tier).
    distributed: bool = False

    @property
    def load_kind(self) -> str:
        return circuits_mod.load_kind(self.desc, poles=self.poles, trip_a=self.trip_a, lighting=self.lighting)

    @classmethod
    def from_wiring_plan(cls, plan: circuits_mod.WiringPlan, devices: Sequence[Device]) -> CircuitSpec:
        fixture_tagged = any(d.fixture_type for d in devices)
        return cls(
            circuit_id=plan.circuit,
            panel=plan.panel,
            ckts=[plan.ckt],
            devices=list(devices),
            wiring=plan.wiring,
            breaker_status=plan.breaker_status,
            desc=plan.desc,
            poles=plan.poles,
            trip_a=plan.trip_a,
            schedule_awg=plan.schedule_awg,
            load_va=plan.load_va,
            gfci=plan.gfci,
            lighting=circuits_mod.is_lighting_circuit(plan.desc, fixture_tagged=fixture_tagged),
            tie_in_ft=plan.tie_in_ft,
            keynotes=list(plan.keynotes),
            reasons=list(plan.reasons),
            schedule_id=plan.schedule_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.circuit_id,
            "panel": self.panel,
            "numbers": list(self.ckts),
            "poles": self.poles,
            "amps": self.trip_a,
            "status": self.breaker_status,
            "wiring": self.wiring,
            "description": self.desc,
            "lighting": self.lighting,
            "schedule_awg": self.schedule_awg,
            "load_va": self.load_va,
            "gfci": self.gfci,
            "devices": [d.to_dict() for d in self.devices],
            "keynotes": list(self.keynotes),
            "reasons": list(self.reasons),
            "distributed": self.distributed,
            "load_kind": self.load_kind,
        }


@dataclass
class PanelInfo:
    """A panel and, when known, where it is drawn (page fractions)."""

    name: str
    volts: str | None = None
    phase: int | None = None
    existing: bool | None = None
    sheet_key: str | None = None
    document_id: str | None = None
    page: int | None = None
    x: float | None = None
    y: float | None = None
    method: str | None = None  # plan_label | schedule_location | model | human
    confidence: float = 0.0
    schedule_id: str | None = None

    @property
    def located(self) -> bool:
        return self.sheet_key is not None and self.x is not None and self.y is not None

    def to_dict(self) -> dict[str, Any]:
        loc = None
        if self.located:
            loc = {
                "sheet_key": self.sheet_key,
                "document_id": self.document_id,
                "page": self.page,
                "x": round(float(self.x), 4),  # type: ignore[arg-type]
                "y": round(float(self.y), 4),  # type: ignore[arg-type]
                "method": self.method,
                "confidence": round(self.confidence, 3),
            }
        return {
            "name": self.name,
            "volts": self.volts,
            "phase": self.phase,
            "existing": self.existing,
            "schedule_id": self.schedule_id,
            "location": loc,
        }


class RegistrationGraph:
    """Registrations usable in both directions, composed through chains
    (`plans_reader_geometry.chain_registrations`), cached per pair."""

    def __init__(self, registrations: Iterable[geometry.Registration] = ()) -> None:
        self._regs = [r for r in registrations if r.ok]
        self._cache: dict[tuple[str, str], geometry.Registration | None] = {}

    def add(self, reg: geometry.Registration) -> None:
        if reg.ok:
            self._regs.append(reg)
            self._cache.clear()

    @property
    def registrations(self) -> list[geometry.Registration]:
        return list(self._regs)

    def between(self, src: str, dst: str) -> geometry.Registration | None:
        """The transform mapping `src` sheet inches onto `dst` sheet inches."""
        key = (src, dst)
        if key not in self._cache:
            self._cache[key] = geometry.chain_registrations(self._regs, src, dst)
        return self._cache[key]


# Geometry helpers -----------------------------------------------------------------------


def rect_mst(points: Sequence[tuple[float, float]]) -> tuple[float, list[tuple[int, int]]]:
    """Prim's minimum spanning tree under the Manhattan metric: (total
    length, edges as index pairs). O(n^2); n is capped by the caller."""
    n = len(points)
    if n <= 1:
        return 0.0, []
    in_tree = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0.0
    total = 0.0
    edges: list[tuple[int, int]] = []
    for _ in range(n):
        u = -1
        for i in range(n):
            if not in_tree[i] and (u < 0 or best[i] < best[u]):
                u = i
        in_tree[u] = True
        total += best[u] if best[u] < math.inf else 0.0
        if parent[u] >= 0:
            edges.append((parent[u], u))
        ux, uy = points[u]
        for v in range(n):
            if in_tree[v]:
                continue
            d = abs(points[v][0] - ux) + abs(points[v][1] - uy)
            if d < best[v]:
                best[v] = d
                parent[v] = u
    return total, edges


def clamp_to_box(point: tuple[float, float], box: Sequence[float]) -> tuple[float, float]:
    x = min(max(point[0], box[0]), box[2])
    y = min(max(point[1], box[1]), box[3])
    return x, y


def work_box(points_in: Sequence[tuple[float, float]], *, pad_in: float = WORK_BOX_PAD_IN) -> list[float]:
    """The work-area box of a sheet in inches: the bounding box of the given
    points (every circuit tag on the sheet) expanded by `pad_in`."""
    xs = [p[0] for p in points_in]
    ys = [p[1] for p in points_in]
    return [min(xs) - pad_in, min(ys) - pad_in, max(xs) + pad_in, max(ys) + pad_in]


def _manhattan(a: tuple[float, float], b: tuple[float, float]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _l_polyline(frame: SheetFrame, a: tuple[float, float], b: tuple[float, float]) -> list[list[float]]:
    """An L-shaped polyline (horizontal first) in page fractions."""
    if abs(a[0] - b[0]) < 1e-9 or abs(a[1] - b[1]) < 1e-9:
        return [frame.to_frac(*a), frame.to_frac(*b)]
    return [frame.to_frac(*a), frame.to_frac(b[0], a[1]), frame.to_frac(*b)]


# Segments and routes ----------------------------------------------------------------------


@dataclass
class RouteSegment:
    kind: str
    length_ft: float
    conductors: dict[str, int] = field(default_factory=dict)
    conductors_text: str = ""
    raceway: str | None = None
    cable: str | None = None
    document_id: str | None = None
    page: int | None = None
    sheet_key: str | None = None
    polyline: list[list[float]] = field(default_factory=list)
    polylines: list[list[list[float]]] = field(default_factory=list)
    ft_per_in: float | None = None
    drawn: bool = False
    basis: str = "assumed_route"

    def to_dict(self) -> dict[str, Any]:
        polylines = [[list(p) for p in pl] for pl in self.polylines] or ([[list(p) for p in self.polyline]] if self.polyline else [])
        return {
            "kind": self.kind,
            "length_ft": round(self.length_ft, 2),
            "conductors": self.conductors_text,
            "conductors_parsed": dict(self.conductors),
            "raceway": self.raceway,
            "cable": self.cable,
            "document_id": self.document_id,
            "page": self.page,
            "sheet_key": self.sheet_key,
            "polyline": [list(p) for p in self.polyline] or ([list(p) for p in polylines[0]] if polylines else []),
            "polylines": polylines,
            "ft_per_in": self.ft_per_in,
            "drawn": self.drawn,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RouteSegment:
        """Inverse of `to_dict` (stored measure outputs, override recompute)."""
        polylines = [[list(p) for p in pl] for pl in d.get("polylines") or []]
        return cls(
            kind=str(d.get("kind") or "allowance"),
            length_ft=float(d.get("length_ft") or 0.0),
            conductors={str(k): int(v) for k, v in (d.get("conductors_parsed") or {}).items()},
            conductors_text=str(d.get("conductors") or ""),
            raceway=d.get("raceway"),
            cable=d.get("cable"),
            document_id=d.get("document_id"),
            page=d.get("page"),
            sheet_key=d.get("sheet_key"),
            polyline=[] if len(polylines) > 1 else [list(p) for p in d.get("polyline") or []],
            polylines=polylines if len(polylines) > 1 else [],
            ft_per_in=d.get("ft_per_in"),
            drawn=bool(d.get("drawn")),
            basis=str(d.get("basis") or "assumed_route"),
        )


@dataclass
class CircuitRoute:
    circuit_id: str
    panel: str
    ckts: list[int]
    devices: list[Device]
    segments: list[RouteSegment]
    basis: str
    caveat: str | None
    flags: list[str] = field(default_factory=list)
    route_id: str = ""
    sizing: dict[str, Any] | None = None
    tier: str = "tagged"  # tagged | equipment | untagged | density | tie_in
    line_ids: list[str] = field(default_factory=list)

    @property
    def total_ft(self) -> float:
        return round(sum(s.length_ft for s in self.segments), 2)

    @property
    def collector_ft(self) -> float:
        return round(sum(s.length_ft for s in self.segments if s.kind in ("collector", "drop", "switch_leg")), 2)

    @property
    def home_run_ft(self) -> float:
        return round(sum(s.length_ft for s in self.segments if s.kind in ("homerun", "tie_in", "allowance")), 2)

    @property
    def sheet_keys(self) -> list[str]:
        return sorted({s.sheet_key for s in self.segments if s.sheet_key} | {d.sheet_key for d in self.devices})

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "ref": f"r:{self.route_id}" if self.route_id else None,
            "circuit_id": self.circuit_id,
            "circuit": self.circuit_id,
            "panel": self.panel,
            "numbers": list(self.ckts),
            "devices": len(self.devices),
            "total_ft": self.total_ft,
            "collector_ft": self.collector_ft,
            "home_run_ft": self.home_run_ft,
            "length_ft": self.total_ft,
            "basis": self.basis,
            "caveat": self.caveat,
            "tier": self.tier,
            "sheet_keys": self.sheet_keys,
            "segments": [s.to_dict() for s in self.segments],
            "sizing": dict(self.sizing) if self.sizing else None,
            "flags": list(self.flags),
            "line_ids": list(self.line_ids),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], devices: Sequence[Device] = ()) -> CircuitRoute:
        """Inverse of `to_dict`; the stored output keeps a device count only,
        so the caller passes the devices when it has them."""
        return cls(
            circuit_id=str(d.get("circuit_id") or d.get("circuit") or ""),
            panel=str(d.get("panel") or "?"),
            ckts=[int(n) for n in d.get("numbers") or []],
            devices=list(devices),
            segments=[RouteSegment.from_dict(s) for s in d.get("segments") or []],
            basis=str(d.get("basis") or "assumed_route"),
            caveat=d.get("caveat"),
            flags=list(d.get("flags") or []),
            route_id=str(d.get("route_id") or ""),
            sizing=dict(d["sizing"]) if d.get("sizing") else None,
            tier=str(d.get("tier") or "tagged"),
            line_ids=list(d.get("line_ids") or []),
        )


def _ground_awg(hot: str, trip_a: int | None) -> str:
    """Ground = hot size up to #10 (the schedule's G column on 7154), above
    that NEC 250.122 by breaker."""
    if circuits_mod.awg_index(hot) <= circuits_mod.awg_index("#10"):
        return hot
    amps = trip_a or 20
    if amps <= 20:
        return "#12"
    if amps <= 60:
        return "#10"
    if amps <= 100:
        return "#8"
    if amps <= 200:
        return "#6"
    return "#4"


def _conductor_set(hot: str, poles: int | None, trip_a: int | None) -> tuple[dict[str, int], str]:
    """{size: count} and the callout text for H + N + G of one circuit."""
    hots = max(1, int(poles or 1))
    ground = _ground_awg(hot, trip_a)
    out: dict[str, int] = {hot: hots + 1}
    out[ground] = out.get(ground, 0) + 1
    if ground == hot:
        text = f"{hots + 2}#{hot.lstrip('#')}"
    else:
        text = f"{hots + 1}#{hot.lstrip('#')}, 1#{ground.lstrip('#')}G"
    return out, text


def _cable_text(hot: str, poles: int | None, method: str) -> str:
    conductors = max(1, int(poles or 1)) + 1
    return f"#{hot.lstrip('#')}/{conductors}C {method.upper()} CABLE"


def _size(circuit: CircuitSpec, panel: PanelInfo | None, one_way_ft: float, conv: Conventions) -> tuple[str, str, dict[str, Any]]:
    """(home run awg, collector awg, sizing dict) for a circuit."""
    volts = circuits_mod.circuit_volts(panel.volts if panel else None, circuit.poles)
    kind = "lighting" if circuit.lighting else "power"
    sizing = circuits_mod.size_conductor(
        kind=kind,
        length_ft=max(one_way_ft, 1.0),
        volts=volts,
        load_va=circuit.load_va,
        trip_a=circuit.trip_a,
        schedule_awg=circuit.schedule_awg,
        poles=circuit.poles,
        vd_max_pct=conv.vd_max_pct,
        min_awg=conv.min_branch_awg,
    )
    hr = sizing.awg
    if kind == "lighting":
        collector = hr
    else:
        collector = conv.min_branch_awg if (circuit.trip_a or 20) <= 20 else hr
    if circuits_mod.awg_index(collector) > circuits_mod.awg_index(hr):
        collector = hr
    return hr, collector, sizing.to_dict()


def _dress(seg: RouteSegment, awg: str, circuit: CircuitSpec, conv: Conventions) -> RouteSegment:
    """Put the conductors and raceway (or cable) on a segment."""
    if conv.wiring_method in ("mc", "nm"):
        seg.conductors, seg.conductors_text = _conductor_set(awg, circuit.poles, circuit.trip_a)
        seg.cable = _cable_text(awg, circuit.poles, conv.wiring_method)
        seg.raceway = None
    else:
        seg.conductors, seg.conductors_text = _conductor_set(awg, circuit.poles, circuit.trip_a)
        seg.raceway = conv.branch_conduit
        seg.cable = None
    return seg


def _dedupe_devices(
    devices: Sequence[Device], frames: Mapping[str, SheetFrame], regs: RegistrationGraph | None
) -> tuple[dict[str, list[Device]], list[str]]:
    """Devices per sheet after cross-sheet dedupe: a device that maps within
    DEDUPE_FT of a device on a larger-scale registered sheet is dropped."""
    by_sheet: dict[str, list[Device]] = {}
    for d in devices:
        by_sheet.setdefault(d.sheet_key, []).append(d)
    flags: list[str] = []
    if len(by_sheet) <= 1 or regs is None:
        return by_sheet, flags
    # Larger scale first (smaller ft_per_in keeps its devices).
    order = sorted(by_sheet, key=lambda k: (frames[k].ft_per_in if k in frames and frames[k].ft_per_in else math.inf, k))
    kept: dict[str, list[Device]] = {order[0]: list(by_sheet[order[0]])}
    for key in order[1:]:
        remaining: list[Device] = []
        for d in by_sheet[key]:
            dup = False
            fr = frames.get(key)
            if fr is None:
                remaining.append(d)
                continue
            dx, dy = fr.to_in(d.x, d.y)
            for other_key, others in kept.items():
                reg = regs.between(key, other_key)
                of = frames.get(other_key)
                if reg is None or reg.transform is None or of is None or of.ft_per_in is None:
                    continue
                mx, my = reg.transform.apply(dx, dy)
                for o in others:
                    ox, oy = of.to_in(o.x, o.y)
                    if math.hypot(mx - ox, my - oy) * of.ft_per_in <= DEDUPE_FT:
                        dup = True
                        break
                if dup:
                    break
            if not dup:
                remaining.append(d)
        if remaining:
            kept[key] = remaining
        if len(remaining) < len(by_sheet[key]):
            flags.append(f"dedupe:{key}:{len(by_sheet[key]) - len(remaining)}")
    return kept, flags


def _allowance_home_run(circuit: CircuitSpec, conv: Conventions, awg: str) -> RouteSegment:
    seg = RouteSegment(kind="allowance", length_ft=conv.homerun_default_ft, basis="assumed_allowance")
    seg.drawn = False
    _dress(seg, awg, circuit, conv)
    seg.polyline = []
    seg.ft_per_in = None
    seg.document_id = None
    return seg


def _route_one_subtree(
    circuit: CircuitSpec,
    sheet_key: str,
    devices: list[Device],
    frames: Mapping[str, SheetFrame],
    panel: PanelInfo | None,
    regs: RegistrationGraph | None,
    boxes: Mapping[str, Sequence[float]],
    conv: Conventions,
) -> tuple[list[RouteSegment], list[str], float, float]:
    """Segments of one sub-tree (one sheet): collector, leg, home run or
    allowance, drops. Returns (segments, flags, collector_ft, home_run_ft)
    before conductors are assigned (sizes need the one-way length)."""
    flags: list[str] = []
    frame = frames.get(sheet_key)
    segs: list[RouteSegment] = []
    if frame is None:
        flags.extend([f"sheet_unknown:{sheet_key}", "homerun_allowance"])
        return [RouteSegment(kind="allowance", length_ft=conv.homerun_default_ft, basis="assumed_allowance")], flags, 0.0, conv.homerun_default_ft
    pts_in = [frame.to_in(d.x, d.y) for d in devices]
    cx = sum(p[0] for p in pts_in) / len(pts_in)
    cy = sum(p[1] for p in pts_in) / len(pts_in)
    scale = frame.scale_for(cx / frame.w_in, cy / frame.h_in)
    if scale is None:
        flags.extend([f"scale_unknown:{sheet_key}", "homerun_allowance"])
        return [RouteSegment(kind="allowance", length_ft=conv.homerun_default_ft, basis="assumed_allowance", sheet_key=sheet_key, document_id=frame.document_id, page=frame.page)], flags, 0.0, conv.homerun_default_ft
    if len(pts_in) > MAX_DEVICES_PER_CIRCUIT:
        flags.append("circuit_too_large")
        pts_in = pts_in[:MAX_DEVICES_PER_CIRCUIT]
    mst_in, edges = rect_mst(pts_in)
    polylines: list[list[list[float]]] = [_l_polyline(frame, pts_in[a], pts_in[b]) for a, b in edges]
    collector_in = mst_in
    home_run_ft = 0.0
    home_run_seg: RouteSegment | None = None
    # Exit point from the panel location.
    exit_in: tuple[float, float] | None = None
    if panel is not None and panel.located and panel.sheet_key is not None:
        pframe = frames.get(panel.sheet_key)
        if pframe is not None:
            p_in = pframe.to_in(float(panel.x), float(panel.y))  # type: ignore[arg-type]
            if panel.sheet_key == sheet_key:
                mapped = p_in
                reg_ok = True
            else:
                reg = regs.between(panel.sheet_key, sheet_key) if regs is not None else None
                if reg is not None and reg.transform is not None:
                    mapped = reg.transform.apply(*p_in)
                    reg_ok = True
                else:
                    mapped = None
                    reg_ok = False
            if reg_ok and mapped is not None:
                box = boxes.get(sheet_key) or work_box(pts_in)
                exit_in = clamp_to_box(mapped, box)
                # Leg from the nearest device to E.
                nearest = min(pts_in, key=lambda p: _manhattan(p, exit_in))  # type: ignore[arg-type]
                leg_in = _manhattan(nearest, exit_in)
                collector_in += leg_in
                if leg_in > 1e-6:
                    polylines.append(_l_polyline(frame, nearest, exit_in))
                # Home run on the panel sheet.
                pscale = pframe.scale_for(float(panel.x), float(panel.y))  # type: ignore[arg-type]
                if panel.sheet_key == sheet_key:
                    e_on_panel = exit_in
                else:
                    back = regs.between(sheet_key, panel.sheet_key) if regs is not None else None
                    e_on_panel = back.transform.apply(*exit_in) if back is not None and back.transform is not None else None
                if pscale is not None and e_on_panel is not None:
                    hr_in = _manhattan(e_on_panel, p_in)
                    floors = 0
                    if frame.level is not None and pframe.level is not None:
                        floors = abs(frame.level - pframe.level)
                    home_run_ft = hr_in * pscale * conv.route_factor + conv.panel_rise_ft + conv.floor_to_floor_ft * floors
                    home_run_seg = RouteSegment(
                        kind="homerun",
                        length_ft=home_run_ft,
                        document_id=pframe.document_id,
                        page=pframe.page,
                        sheet_key=pframe.sheet_key,
                        polyline=_l_polyline(pframe, e_on_panel, p_in),
                        ft_per_in=pscale,
                        drawn=False,
                        basis="assumed_route",
                    )
                    if floors:
                        flags.append(f"floors:{floors}")
                else:
                    flags.append(f"scale_unknown:{panel.sheet_key}")
            else:
                flags.append(f"registration_missing:{sheet_key}->{panel.sheet_key}")
        else:
            flags.append(f"sheet_unknown:{panel.sheet_key}")
    elif panel is None:
        flags.append("panel_unknown")
    else:
        flags.append("panel_unlocated")
    collector_ft = collector_in * scale * conv.route_factor
    segs.append(
        RouteSegment(
            kind="collector",
            length_ft=collector_ft,
            document_id=frame.document_id,
            page=frame.page,
            sheet_key=sheet_key,
            polyline=[pt for pl in polylines for pt in pl] if len(polylines) == 1 else [],
            polylines=polylines,
            ft_per_in=scale,
            drawn=False,
            basis="assumed_route",
        )
    )
    if home_run_seg is not None:
        segs.append(home_run_seg)
    else:
        allowance = RouteSegment(kind="allowance", length_ft=conv.homerun_default_ft, basis="assumed_allowance", sheet_key=sheet_key, document_id=frame.document_id, page=frame.page)
        segs.append(allowance)
        home_run_ft = conv.homerun_default_ft
        flags.append("homerun_allowance")
    if conv.drop_ft_per_device > 0 and devices:
        segs.append(
            RouteSegment(
                kind="drop",
                length_ft=conv.drop_ft_per_device * len(devices),
                document_id=frame.document_id,
                page=frame.page,
                sheet_key=sheet_key,
                ft_per_in=scale,
                basis="assumed_route",
            )
        )
    return segs, flags, collector_ft, home_run_ft


def _merge_registered(
    by_sheet: dict[str, list[Device]], frames: Mapping[str, SheetFrame], regs: RegistrationGraph | None
) -> tuple[dict[str, list[Device]], list[str]]:
    """Sub-trees on sheets that register onto one anchor sheet become a
    single tree on the anchor (the sheet with the most devices, then the
    larger scale): device points are mapped through the registration into
    the anchor's frame, so one collector and one home run serve them. Only
    sheets that do not register stay separate (`circuit_split_unregistered`)."""
    if len(by_sheet) <= 1 or regs is None:
        return by_sheet, []
    order = sorted(by_sheet, key=lambda k: (-len(by_sheet[k]), frames[k].ft_per_in if k in frames and frames[k].ft_per_in else math.inf, k))
    anchor = order[0]
    af = frames.get(anchor)
    if af is None:
        return by_sheet, []
    merged: dict[str, list[Device]] = {anchor: list(by_sheet[anchor])}
    flags: list[str] = []
    for key in order[1:]:
        fr = frames.get(key)
        reg = regs.between(key, anchor) if fr is not None else None
        if fr is None or reg is None or reg.transform is None:
            merged[key] = list(by_sheet[key])
            continue
        for d in by_sheet[key]:
            mx, my = reg.transform.apply(*fr.to_in(d.x, d.y))
            merged[anchor].append(
                Device(af.document_id, af.page, anchor, mx / (af.w_in or 1.0), my / (af.h_in or 1.0), tag=d.tag, source=d.source, fixture_type=d.fixture_type)
            )
        flags.append(f"merged:{key}->{anchor}")
    return merged, flags


def synthesize_routes(
    circuits: Sequence[CircuitSpec],
    panels: Mapping[str, PanelInfo],
    frames: Mapping[str, SheetFrame],
    regs: RegistrationGraph | None,
    conv: Conventions,
    *,
    work_boxes: Mapping[str, Sequence[float]] | None = None,
    route_seq: int = 0,
) -> list[CircuitRoute]:
    """One `CircuitRoute` per circuit with new wiring or a tie-in (circuits
    with `wiring == "none"` are skipped). Route ids continue from
    `route_seq` (`r1`, `r2`, ...). Devices of one circuit on several
    sheets are deduplicated and merged onto one registered sheet first;
    the work-area boxes are taken over the merged device points."""
    ordered = sorted(circuits, key=lambda c: (c.panel, c.ckts[0] if c.ckts else 0, c.circuit_id))
    trees: dict[int, tuple[dict[str, list[Device]], list[str]]] = {}
    for i, circuit in enumerate(ordered):
        if circuit.wiring in ("none", "tie_in") or not circuit.devices:
            continue
        by_sheet, dflags = _dedupe_devices(circuit.devices, frames, regs)
        by_sheet, mflags = _merge_registered(by_sheet, frames, regs)
        trees[i] = (by_sheet, dflags + mflags)
    boxes: dict[str, Sequence[float]] = dict(work_boxes or {})
    if not boxes:
        pts_by_sheet: dict[str, list[tuple[float, float]]] = {}
        for by_sheet, _f in trees.values():
            for skey, devs in by_sheet.items():
                fr = frames.get(skey)
                if fr is not None:
                    pts_by_sheet.setdefault(skey, []).extend(fr.to_in(d.x, d.y) for d in devs)
        boxes = {k: work_box(v) for k, v in pts_by_sheet.items() if v}
    out: list[CircuitRoute] = []
    seq = route_seq
    for i, circuit in enumerate(ordered):
        if circuit.wiring == "none":
            continue
        panel = panels.get(circuit.panel.upper()) or panels.get(circuit.panel)
        flags: list[str] = []
        segments: list[RouteSegment] = []
        awg_hr = conv.min_branch_awg
        awg_col = conv.min_branch_awg
        sizing: dict[str, Any] | None = None
        if circuit.wiring == "tie_in":
            tie = RouteSegment(kind="tie_in", length_ft=circuit.tie_in_ft or conv.existing_tie_in_ft, basis="assumed_allowance")
            awg_hr, awg_col, sizing = _size(circuit, panel, tie.length_ft, conv)
            _dress(tie, awg_col, circuit, conv)
            segments.append(tie)
            if circuit.devices:
                tie.sheet_key = circuit.devices[0].sheet_key
                tie.document_id = circuit.devices[0].document_id
                tie.page = circuit.devices[0].page
            tier = "tie_in"
        elif not circuit.devices:
            # New wiring named by the schedule but no tag on a plan: a home-run allowance.
            awg_hr, awg_col, sizing = _size(circuit, panel, conv.homerun_default_ft, conv)
            segments.append(_allowance_home_run(circuit, conv, awg_hr))
            flags.extend(["homerun_allowance", "no_devices"])
            tier = "tagged"
        else:
            by_sheet, tflags = trees[i]
            flags.extend(tflags)
            if len(by_sheet) > 1:
                flags.append("circuit_split_unregistered")
            raw_segments: list[RouteSegment] = []
            collector_total = 0.0
            hr_total = 0.0
            for skey, devs in sorted(by_sheet.items()):
                segs, sflags, col_ft, hr_ft = _route_one_subtree(circuit, skey, devs, frames, panel, regs, boxes, conv)
                raw_segments.extend(segs)
                flags.extend([f for f in sflags if f not in flags])
                collector_total += col_ft
                hr_total += hr_ft
            one_way = collector_total + hr_total
            awg_hr, awg_col, sizing = _size(circuit, panel, one_way, conv)
            for seg in raw_segments:
                _dress(seg, awg_hr if seg.kind in ("homerun", "allowance") else awg_col, circuit, conv)
            segments = raw_segments
            tier = "tagged"
        basis = weakest_basis(s.basis for s in segments)
        caveat = None if basis == "measured_route" else ASSUMED_CAVEAT
        seq += 1
        out.append(
            CircuitRoute(
                circuit_id=circuit.circuit_id,
                panel=circuit.panel,
                ckts=list(circuit.ckts),
                devices=list(circuit.devices),
                segments=segments,
                basis=basis,
                caveat=caveat,
                flags=flags,
                route_id=f"r{seq}",
                sizing={"home_run_awg": awg_hr, "collector_awg": awg_col, **(sizing or {})},
                tier=tier,
            )
        )
    return out


# Lower tiers ------------------------------------------------------------------------------


def equipment_routes(
    connections: Sequence[Device],
    panels: Mapping[str, PanelInfo],
    frames: Mapping[str, SheetFrame],
    conv: Conventions,
    *,
    route_seq: int = 0,
    descriptions: Mapping[str, str] | None = None,
) -> list[CircuitRoute]:
    """The equipment-connection tier: every untagged equipment connection
    (schedule or count row) gets its own home-run allowance so a dropped
    model row is always replaced. Basis `assumed_allowance`."""
    out: list[CircuitRoute] = []
    seq = route_seq
    for i, dev in enumerate(connections, start=1):
        circuit = CircuitSpec(circuit_id=f"EQ-{i}", panel="?", ckts=[], devices=[dev], desc=(descriptions or {}).get(dev.tag or "", "EQUIPMENT CONNECTION"))
        awg_hr, _col, sizing = _size(circuit, None, conv.homerun_default_ft, conv)
        seg = _allowance_home_run(circuit, conv, awg_hr)
        seg.sheet_key = dev.sheet_key
        seg.document_id = dev.document_id
        seg.page = dev.page
        seq += 1
        out.append(
            CircuitRoute(
                circuit_id=circuit.circuit_id,
                panel="?",
                ckts=[],
                devices=[dev],
                segments=[seg],
                basis="assumed_allowance",
                caveat=ASSUMED_CAVEAT,
                flags=["equipment_allowance"],
                route_id=f"r{seq}",
                sizing={"home_run_awg": awg_hr, "collector_awg": awg_hr, **sizing},
                tier="equipment",
            )
        )
    return out


def _chains(points: Sequence[tuple[float, float]], size: int) -> list[list[int]]:
    """Nearest-neighbour chains of at most `size` points."""
    n = len(points)
    unused = set(range(n))
    groups: list[list[int]] = []
    while unused:
        start = min(unused, key=lambda i: (points[i][1], points[i][0]))
        chain = [start]
        unused.discard(start)
        while len(chain) < size and unused:
            last = points[chain[-1]]
            nxt = min(unused, key=lambda i: _manhattan(points[i], last))
            chain.append(nxt)
            unused.discard(nxt)
        groups.append(chain)
    return groups


def untagged_routes(
    devices: Sequence[Device],
    panels: Mapping[str, PanelInfo],
    frames: Mapping[str, SheetFrame],
    regs: RegistrationGraph | None,
    conv: Conventions,
    *,
    route_seq: int = 0,
    lighting: bool = False,
    fixtures_per_circuit: int = 12,
) -> list[CircuitRoute]:
    """Device positions without circuit tags: synthetic circuits of at most
    `receptacles_per_circuit` receptacles (or `fixtures_per_circuit`
    luminaires) by nearest-neighbour chains per sheet, routed to the first
    located panel (any voltage when none matches). Basis
    `assumed_route_untagged`, caveat (ASSUMED)."""
    out: list[CircuitRoute] = []
    seq = route_seq
    by_sheet: dict[str, list[Device]] = {}
    for d in devices:
        by_sheet.setdefault(d.sheet_key, []).append(d)
    located = [p for p in panels.values() if p.located]
    want_volts = "277" if lighting else "120"
    panel = next((p for p in located if want_volts in str(p.volts or "")), located[0] if located else None)
    size = fixtures_per_circuit if lighting else conv.receptacles_per_circuit
    n = 0
    for skey, devs in sorted(by_sheet.items()):
        fr = frames.get(skey)
        if fr is None:
            continue
        pts = [fr.to_in(d.x, d.y) for d in devs]
        for chain in _chains(pts, max(1, size)):
            n += 1
            circuit = CircuitSpec(
                circuit_id=f"U-{n}",
                panel=panel.name if panel else "?",
                ckts=[],
                devices=[devs[i] for i in chain],
                lighting=lighting,
                trip_a=20,
            )
            routes = synthesize_routes([circuit], {panel.name: panel} if panel else {}, frames, regs, conv, route_seq=seq)
            for r in routes:
                seq += 1
                for s in r.segments:
                    if s.basis == "assumed_route":
                        s.basis = "assumed_route_untagged"
                r.basis = weakest_basis(s.basis for s in r.segments)
                r.caveat = ASSUMED_CAVEAT
                r.tier = "untagged"
                r.flags.append("untagged")
                out.append(r)
    return out


def distribute_devices(
    circuits: Sequence[CircuitSpec],
    devices: Sequence[Device],
    frames: Mapping[str, SheetFrame],
    *,
    kind: str,
) -> tuple[list[Device], list[str]]:
    """The WS3 untagged tier on a project with panel schedules: devices
    without circuit tags are spread over the schedule's open circuits of
    the same load kind (lighting on lighting circuits, receptacles on
    receptacle circuits, connections on equipment circuits). Open means
    new wiring, no tagged device, not yet distributed. Per sheet the
    devices form nearest-neighbour chains of ceil(devices / circuits)
    points; chains are dealt to the circuits in panel and circuit order
    (the circuit gets the chain as its devices and `distributed` True).
    Returns (devices left over because no open circuit of the kind
    remained, the circuit ids that received devices). With no open circuit
    of the kind every device is left over (the caller falls back to the
    synthetic `untagged_routes`)."""
    if not devices:
        return [], []
    open_circuits = sorted(
        (c for c in circuits if c.wiring == "new" and not c.devices and not c.distributed and c.load_kind == kind),
        key=lambda c: (c.panel, c.ckts[0] if c.ckts else 0, c.circuit_id),
    )
    if not open_circuits:
        return list(devices), []
    size = max(1, math.ceil(len(devices) / len(open_circuits)))
    by_sheet: dict[str, list[Device]] = {}
    for d in devices:
        by_sheet.setdefault(d.sheet_key, []).append(d)
    chains: list[list[Device]] = []
    leftover: list[Device] = []
    for skey, devs in sorted(by_sheet.items()):
        fr = frames.get(skey)
        if fr is None:
            leftover.extend(devs)
            continue
        pts = [fr.to_in(d.x, d.y) for d in devs]
        for chain in _chains(pts, size):
            chains.append([devs[i] for i in chain])
    assigned: list[str] = []
    for i, chain in enumerate(chains):
        if i >= len(open_circuits):
            leftover.extend(chain)
            continue
        circuit = open_circuits[i]
        circuit.devices = list(chain)
        circuit.distributed = True
        circuit.reasons.append(f"{len(chain)} untagged {kind} device(s) on {chain[0].sheet_key} distributed onto this circuit (no circuit tags on the plans)")
        assigned.append(circuit.circuit_id)
    return leftover, assigned


def mark_distributed_routes(routes: Sequence[CircuitRoute], circuits: Sequence[CircuitSpec]) -> list[str]:
    """Routes of distributed circuits read as the untagged tier: basis
    `assumed_route_untagged`, caveat (ASSUMED), flags `untagged` and
    `schedule_distribution`. Returns the route ids changed."""
    distributed = {c.circuit_id for c in circuits if c.distributed}
    changed: list[str] = []
    for r in routes:
        if r.circuit_id not in distributed or r.tier in ("tie_in", "equipment", "density"):
            continue
        for seg in r.segments:
            if seg.basis == "assumed_route":
                seg.basis = "assumed_route_untagged"
        r.basis = weakest_basis(seg.basis for seg in r.segments)
        r.caveat = None if r.basis == "measured_route" else ASSUMED_CAVEAT
        r.tier = "untagged"
        for flag in ("untagged", UNTAGGED_DISTRIBUTION_FLAG):
            if flag not in r.flags:
                r.flags.append(flag)
        changed.append(r.route_id)
    return changed


def density_routes(
    new_devices: int,
    conv: Conventions,
    *,
    project_type: str | None = None,
    route_seq: int = 0,
    circuit_id: str = "DENSITY",
    flag: str = "branch_density_only",
) -> list[CircuitRoute]:
    """Branch quantities per device from the active history bands (WS3,
    untagged projects never empty): conduit FT = devices x the band median
    EMT FT per device, wire FT = devices x the band median branch wire FT
    per device, split into #12 and #10 by the #10 share. The whole device
    count goes through here when nothing is positioned (`flag`
    `branch_density_only`); the devices no route covers go through it as
    `untagged_device_band`. Basis `BAND_BASIS`; the band source and version
    are recorded in `sizing.band`."""
    if new_devices <= 0:
        return []
    bandset = measure_bands.current()
    stratum = measure_bands.stratum_for(project_type, bandset)
    dens = measure_bands.density_per_device(stratum, bandset)
    conduit_ft = new_devices * dens["emt_ft_per_device"]
    wire_ft = new_devices * dens["wire_ft_per_device"]
    share10 = dens["awg10_share"]
    segs: list[RouteSegment] = []
    if conv.wiring_method in ("mc", "nm"):
        # Cable carries its own conductors: the band's wire footage per 2-conductor cable.
        for awg, share in (("#12", 1.0 - share10), ("#10", share10)):
            if share <= 0:
                continue
            seg = RouteSegment(kind="allowance", length_ft=round(wire_ft * share / 2.0, 2), basis=BAND_BASIS)
            seg.conductors = {awg: 2}
            seg.conductors_text = f"2#{awg.lstrip('#')}"
            seg.cable = _cable_text(awg, 1, conv.wiring_method)
            segs.append(seg)
    else:
        raceway = RouteSegment(kind="allowance", length_ft=round(conduit_ft, 2), basis=BAND_BASIS)
        raceway.raceway = conv.branch_conduit
        raceway.conductors = {}
        raceway.conductors_text = ""
        segs.append(raceway)
        for awg, share in (("#12", 1.0 - share10), ("#10", share10)):
            if share <= 0:
                continue
            seg = RouteSegment(kind="allowance", length_ft=round(wire_ft * share, 2), basis=BAND_BASIS)
            seg.conductors = {awg: 1}
            seg.conductors_text = f"1#{awg.lstrip('#')}"
            segs.append(seg)
    band = {
        "bands_source": bandset.source,
        "band_version": bandset.source,
        "from_file": bandset.from_file,
        "stratum": stratum,
        "emt_ft_per_device_p50": dens["emt_ft_per_device"],
        "branch_wire_ft_per_device_p50": dens["wire_ft_per_device"],
        "awg10_share_p50": share10,
        "basis": "assumed_band",
    }
    route = CircuitRoute(
        circuit_id=circuit_id,
        panel="?",
        ckts=[],
        devices=[],
        segments=segs,
        basis=BAND_BASIS,
        caveat=ASSUMED_CAVEAT,
        flags=[flag, f"stratum:{stratum}", f"devices:{new_devices}", f"wire_ft:{round(wire_ft, 1)}", f"band:{bandset.source}"],
        route_id=f"r{route_seq + 1}",
        tier="density",
        sizing={"emt_ft_per_device": dens["emt_ft_per_device"], "wire_ft_per_device": dens["wire_ft_per_device"], "wire_to_conduit": dens["wire_to_conduit"], "awg10_share": share10, "band": band},
    )
    return [route]


# Quantities -----------------------------------------------------------------------------


def _awg_sort(key: str) -> int:
    try:
        return circuits_mod.awg_index(key)
    except ValueError:
        return 999


def branch_quantities(
    routes: Sequence[CircuitRoute],
    conv: Conventions,
    *,
    row_seq: int = 0,
    area: str = "NEW WORK",
) -> list[dict[str, Any]]:
    """The M rows (spec 5.4 `quantities[]`) summed from every route segment:
    one conductor row per size (`#12 THHN/THWN Cu`, WIRING / CONDUCTORS
    (AWG)), one conduit row per raceway (`3/4"C EMT`, WIRING / CONDUITS, the
    `_CONDUIT_RE` form so the renderer derives the accessories, E23) or one
    cable row per cable. Quantities are pre-wastage, rounded to 0.1 FT; the
    renderer adds the 10%. Row ids continue from `row_seq`."""
    conductors: dict[str, float] = {}
    raceways: dict[str, float] = {}
    cables: dict[str, float] = {}
    terms: dict[str, list[dict[str, Any]]] = {}
    bases: dict[str, list[str]] = {}
    sheets: dict[str, set[str]] = {}
    refs: dict[str, list[str]] = {}

    def _add(bucket: dict[str, float], key: str, ft: float, route: CircuitRoute, seg: RouteSegment, count: int) -> None:
        bucket[key] = bucket.get(key, 0.0) + ft
        terms.setdefault(key, []).append(
            {"circuit_id": route.circuit_id, "route_id": route.route_id, "segment": seg.kind, "length_ft": round(seg.length_ft, 2), "count": count, "basis": seg.basis}
        )
        bases.setdefault(key, []).append(seg.basis)
        if seg.sheet_key:
            sheets.setdefault(key, set()).add(seg.sheet_key)
        for d in route.devices:
            sheets.setdefault(key, set()).add(d.sheet_key)
        ref = f"r:{route.route_id}"
        if route.route_id and ref not in refs.setdefault(key, []):
            refs[key].append(ref)

    for route in routes:
        for seg in route.segments:
            for awg, count in seg.conductors.items():
                if seg.cable:
                    continue
                _add(conductors, awg, seg.length_ft * count, route, seg, count)
            if seg.raceway:
                _add(raceways, seg.raceway, seg.length_ft, route, seg, 1)
            if seg.cable:
                _add(cables, seg.cable, seg.length_ft, route, seg, 1)

    rows: list[dict[str, Any]] = []
    seq = row_seq

    def _row(description: str, subgroup: str, key: str, ft: float) -> dict[str, Any]:
        nonlocal seq
        seq += 1
        basis = weakest_basis(bases.get(key, []))
        return {
            "row_id": make_row_id("M", seq),
            "group": "WIRING",
            "subgroup": subgroup,
            "description": description,
            "quantity": round(ft, 1),
            "unit": "FT",
            "wastage_applies": True,
            "basis": basis,
            "caveat": None if basis == "measured_route" else ASSUMED_CAVEAT,
            "sheet_refs": sorted(sheets.get(key, set())),
            "refs": list(refs.get(key, [])),
            "derivation": {"terms": terms.get(key, []), "route_factor": conv.route_factor, "conventions": conv.version},
            "line_hint": {"area": area, "group": "WIRING", "subgroup": subgroup},
            "material_category": "General Material",
        }

    for awg in sorted(conductors, key=_awg_sort):
        if conductors[awg] <= 0:
            continue
        rows.append(_row(circuits_mod.conductor_description(awg), "CONDUCTORS (AWG)", awg, conductors[awg]))
    for cable in sorted(cables):
        if cables[cable] > 0:
            rows.append(_row(cable, "CONDUCTORS (AWG)", cable, cables[cable]))
    for raceway in sorted(raceways):
        if raceways[raceway] > 0:
            rows.append(_row(raceway, "CONDUITS", raceway, raceways[raceway]))
    return rows


def route_totals(routes: Sequence[CircuitRoute]) -> dict[str, Any]:
    """Totals by conductor size and raceway (FT, pre-wastage), collector and
    home-run totals, counts per basis and tier."""
    conductors: dict[str, float] = {}
    raceways: dict[str, float] = {}
    cables: dict[str, float] = {}
    by_basis: dict[str, float] = {}
    by_tier: dict[str, int] = {}
    collector = 0.0
    home_run = 0.0
    for r in routes:
        by_tier[r.tier] = by_tier.get(r.tier, 0) + 1
        collector += r.collector_ft
        home_run += r.home_run_ft
        for s in r.segments:
            by_basis[s.basis] = by_basis.get(s.basis, 0.0) + s.length_ft
            if s.raceway:
                raceways[s.raceway] = raceways.get(s.raceway, 0.0) + s.length_ft
            if s.cable:
                cables[s.cable] = cables.get(s.cable, 0.0) + s.length_ft
            else:
                for awg, n in s.conductors.items():
                    conductors[awg] = conductors.get(awg, 0.0) + s.length_ft * n
    return {
        "circuits": len(routes),
        "conductors": {k: round(v, 1) for k, v in sorted(conductors.items(), key=lambda kv: _awg_sort(kv[0]))},
        "raceway": {k: round(v, 1) for k, v in sorted(raceways.items())},
        "cable": {k: round(v, 1) for k, v in sorted(cables.items())},
        "collector_ft": round(collector, 1),
        "home_run_ft": round(home_run, 1),
        "by_basis": {k: round(v, 1) for k, v in sorted(by_basis.items())},
        "by_tier": dict(sorted(by_tier.items())),
    }
