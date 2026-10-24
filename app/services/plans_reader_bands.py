"""History bands on the estimator basis (AI_ESTIMATING_BUILD_SPEC WS7, 8.7
bands population, 5.3 `check_bands`).

A band is the p10 / p25 / p50 / p75 / p90 of one metric over the estimator
workbooks of one stratum of past projects. The run's own estimate is
measured the same way and placed against the band. Bands never change a
quantity: they produce `BandCheck` rows that direct the reviewer and the
human.

Metrics (all on the estimator basis, from WS6 `estimate_parse` with the
one-book rule of 8.8; per-device metrics need at least MIN_DEVICES devices):

- `hours_per_device`: workbook TOTAL LABOR HOURS / devices (total level);
- `branch_wire_ft_per_device`: #12 and #10 conductor FT, pre-waste
  (column D) / devices (component; a zero is a structural zero);
- `emt_ft_per_device`: EMT FT of every size, pre-waste / devices
  (component; a zero is a structural zero);
- `wire_per_emt34`: branch wire FT / 3/4" EMT FT, when both are > 0
  (component);
- `bid_per_device`: workbook BASE BID PRICE / devices (total level).

Device = a NEW WORK row in EA with a positive quantity whose innermost
heading (subgroup, else group) starts with LIGHTING FIXTURES, LIGHTING
CONTROLS, RECEPTACLES, LOW VOLTAGE WORK, MISCELLANEOUS, CONTROLS, OTHER
ITEMS or FIRE ALARM ROUGH, never under SWITCHGEAR (`classify_row`). The
same classifier measures the history and the AI estimate.

Strata: `building` = project_type in ti, other, new_construction,
multi_family or any casino_* type (the bands population of 8.7); `ti`,
`roadway` and `all` are stored for the report. A run uses `building` when
its type is a building type, else `roadway` when it is one and that band
has at least MIN_STRATUM_N values, else `all`.

Book filter (documented, `book_exclusion`): a chosen book is dropped from
every band when it is
- `budget_rom`: its file or sheet names say BUDGET, BUDGETARY, ROM, ROUGH
  ORDER or CONCEPTUAL; or it carries fewer than ROM_MIN_PRICED_LINES item
  lines with both a quantity and unit labor hours; or fewer than
  ROM_MIN_HOURS_SHARE of its item lines carry unit labor hours (a lump
  priced book);
- `labor_only`: material is below LABOR_ONLY_MAX_MATERIAL_SHARE of material
  plus labor cost (material by others).

Severity (fixed, never calibrated): inside [p10, p90] none; outside it
`warning` (advisory); outside the extreme band [p10 / 2, 2 x p90] a
`critical` only for (a) a structural zero of branch wire or EMT per device
and (b) the total-level metrics, and in both cases only when the metric's
LOPO baseline supports it; every other extreme stays `warning`.

LOPO baseline: for every project of a stratum, its own value is placed
against the band built from the other projects. The share of projects that
land outside the extreme band (or at a structural zero) is the false
critical rate a clean estimator book would see. A metric supports
`critical` when its band has at least CRITICAL_MIN_N values and that rate
is at most LOPO_CRITICAL_MAX_RATE. `check_bands` always leaves the run's
own project out of the band (leave-one-project-out in eval and live
sweeps).

Persistence: files under `EVAL_ROOT/bands/<band_version>.json` (`save_bands`,
`load_bands`) and the `ai_history_bands` table of migration MF
(`persist_bands`, `load_active_bands`; at most one active band_version).

Pure apart from the file helpers, `summaries_from_backup` (reads the backup
on disk) and the two table helpers (sync Supabase client, worker threads
only). No model.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.services import estimate_parse
from app.services.estimating_contracts import excluded_project_ids_sha256

logger = logging.getLogger(__name__)

COMPANY_TZ = ZoneInfo("America/Los_Angeles")

BANDS_SCHEMA = "bdr.history_bands/1"
METRICS_VERSION = 1
BASIS = "estimator_workbook"

MIN_DEVICES = 20
MIN_STRATUM_N = 8
CRITICAL_MIN_N = 20
LOPO_CRITICAL_MAX_RATE = 0.05
EXTREME_LOW_FACTOR = 0.5
EXTREME_HIGH_FACTOR = 2.0
QUANTILES: tuple[float, ...] = (0.10, 0.25, 0.50, 0.75, 0.90)

# Book filter (documented in the module docstring).
BUDGET_NAME_RE = re.compile(r"\bBUDGET(?:ARY)?\b|\bROM\b|ROUGH\s+ORDER|\bCONCEPTUAL\b", re.IGNORECASE)
ROM_MIN_PRICED_LINES = 3
ROM_MIN_HOURS_SHARE = 0.5
LABOR_ONLY_MAX_MATERIAL_SHARE = 0.05

BUILDING_TYPES: frozenset[str] = frozenset({"ti", "other", "new_construction", "multi_family"})
BUILDING_TYPE_PREFIXES: tuple[str, ...] = ("casino_",)
STRATA: tuple[str, ...] = ("building", "ti", "roadway", "all")

SEVERITIES: tuple[str, ...] = ("none", "info", "warning", "critical")
POSITIONS: tuple[str, ...] = (
    "below_extreme",
    "below",
    "low",
    "in_band",
    "high",
    "above",
    "above_extreme",
    "not_applicable",
)

# Row classes.
ROW_CLASSES: tuple[str, ...] = ("device", "branch_wire", "emt34", "emt_other", "other")
_DEVICE_HEADING_RE = re.compile(
    r"^(LIGHTING FIXTURES|LIGHTING CONTROLS|RECEPTACLES|LOW VOLTAGE WORK|MISCELLANEOUS|CONTROLS"
    r"|OTHER ITEMS|FIRE ALARM ROUGH)"
)
_BRANCH_WIRE_RE = re.compile(r"#\s*(12|10)\b")
_EMT34_RE = re.compile(r"3/4\s*\"?\s*(C\s*)?EMT", re.IGNORECASE)


@dataclass(frozen=True)
class MetricSpec:
    metric: str
    level: str  # total | component
    per_device: bool
    structural_zero: bool = False
    money: bool = False
    label: str = ""


METRICS: dict[str, MetricSpec] = {
    "hours_per_device": MetricSpec(
        "hours_per_device", "total", True, label="estimator hours per device"
    ),
    "branch_wire_ft_per_device": MetricSpec(
        "branch_wire_ft_per_device", "component", True, structural_zero=True,
        label="#12 and #10 branch wire FT (pre-waste) per device",
    ),
    "emt_ft_per_device": MetricSpec(
        "emt_ft_per_device", "component", True, structural_zero=True,
        label="EMT FT (all sizes, pre-waste) per device",
    ),
    "wire_per_emt34": MetricSpec(
        "wire_per_emt34", "component", False, label="branch wire FT per 3/4 in EMT FT"
    ),
    "bid_per_device": MetricSpec(
        "bid_per_device", "total", True, money=True, label="estimator base bid per device"
    ),
}
METRIC_IDS: tuple[str, ...] = tuple(METRICS)
MONEY_METRICS: frozenset[str] = frozenset(m for m, s in METRICS.items() if s.money)


# Row classifier ----------------------------------------------------------------------


def _up(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def classify_row(section: Any, group: Any, subgroup: Any, description: Any, unit: Any) -> str:
    """The band class of one estimate or BOQ row: device, branch_wire, emt34,
    emt_other or other. Shared by the history builder and the run side."""
    sec, grp, sub, desc, uom = _up(section), _up(group), _up(subgroup), _up(description), _up(unit)
    heading = sub or grp
    if uom == "EA":
        if sec != "DEMOLITION" and grp != "SWITCHGEAR" and _DEVICE_HEADING_RE.match(heading):
            return "device"
        return "other"
    if uom != "FT":
        return "other"
    if heading.startswith("CONDUCTORS") and _BRANCH_WIRE_RE.search(desc):
        return "branch_wire"
    if heading.startswith("CONDUITS") and "EMT" in desc:
        return "emt34" if _EMT34_RE.search(desc) else "emt_other"
    return "other"


def is_building_type(project_type: Any) -> bool:
    t = str(project_type or "").strip().lower()
    return t in BUILDING_TYPES or any(t.startswith(p) for p in BUILDING_TYPE_PREFIXES)


def strata_for_type(project_type: Any) -> tuple[str, ...]:
    """Every stratum a project of this type contributes to."""
    t = str(project_type or "").strip().lower()
    out = ["all"]
    if is_building_type(t):
        out.append("building")
    if t == "ti":
        out.append("ti")
    if t == "roadway":
        out.append("roadway")
    return tuple(s for s in STRATA if s in out)


# Estimate summary ------------------------------------------------------------------------


@dataclass(frozen=True)
class EstimateSummary:
    """The band inputs of one estimate on the estimator basis."""

    project_id: str | None
    project_type: str | None
    devices: float
    hours: float | None
    branch_wire_ft: float
    emt_ft: float
    emt34_ft: float
    bid_usd: float | None = None
    line_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def metric_values(self) -> dict[str, float | None]:
        """Value per metric; None where the metric does not apply."""
        out: dict[str, float | None] = {m: None for m in METRIC_IDS}
        dev = self.devices
        if dev >= MIN_DEVICES:
            if self.hours is not None:
                out["hours_per_device"] = self.hours / dev
            out["branch_wire_ft_per_device"] = self.branch_wire_ft / dev
            out["emt_ft_per_device"] = self.emt_ft / dev
            if self.bid_usd is not None and self.bid_usd > 0:
                out["bid_per_device"] = self.bid_usd / dev
        if self.branch_wire_ft > 0 and self.emt34_ft > 0:
            out["wire_per_emt34"] = self.branch_wire_ft / self.emt34_ft
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_type": self.project_type,
            "devices": _r(self.devices),
            "hours": _r(self.hours),
            "branch_wire_ft": _r(self.branch_wire_ft),
            "emt_ft": _r(self.emt_ft),
            "emt34_ft": _r(self.emt34_ft),
            "bid_usd": _r(self.bid_usd),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EstimateSummary:
        return cls(
            project_id=data.get("project_id"),
            project_type=data.get("project_type"),
            devices=float(data.get("devices") or 0),
            hours=_f(data.get("hours")),
            branch_wire_ft=float(data.get("branch_wire_ft") or 0),
            emt_ft=float(data.get("emt_ft") or 0),
            emt34_ft=float(data.get("emt34_ft") or 0),
            bid_usd=_f(data.get("bid_usd")),
        )


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _r(value: float | None, places: int = 6) -> float | None:
    return None if value is None else round(float(value), places)


def _get(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def _pre_waste_qty(row: Any) -> float | None:
    base = _f(_get(row, "qty_base"))
    if base is not None:
        return base
    qty = _f(_get(row, "qty"))
    if qty is None:
        qty = _f(_get(row, "quantity"))
    if qty is None:
        return None
    wastage = _f(_get(row, "wastage")) or 0.0
    if wastage > 1:
        wastage /= 100.0
    return qty / (1.0 + wastage) if wastage > 0 else qty


def summarize_rows(
    rows: Iterable[Any],
    *,
    hours: float | None,
    bid_usd: float | None = None,
    project_id: str | None = None,
    project_type: str | None = None,
    id_key: str = "line_id",
) -> EstimateSummary:
    """Summary from rows carrying section, group, subgroup, description, unit
    and a pre-waste quantity (`qty_base`, else qty / (1 + wastage))."""
    totals = {"device": 0.0, "branch_wire": 0.0, "emt34": 0.0, "emt_other": 0.0}
    ids: dict[str, list[str]] = {k: [] for k in totals}
    for row in rows:
        cls = classify_row(
            _get(row, "section"), _get(row, "group"), _get(row, "subgroup"),
            _get(row, "description"), _get(row, "unit"),
        )
        if cls == "other":
            continue
        q = _pre_waste_qty(row)
        if q is None or q <= 0:
            continue
        totals[cls] += q
        rid = _get(row, id_key)
        if rid:
            ids[cls].append(str(rid))
    return EstimateSummary(
        project_id=project_id,
        project_type=project_type,
        devices=totals["device"],
        hours=hours,
        branch_wire_ft=totals["branch_wire"],
        emt_ft=totals["emt34"] + totals["emt_other"],
        emt34_ft=totals["emt34"],
        bid_usd=bid_usd,
        line_ids={k: tuple(v) for k, v in ids.items() if v},
    )


def book_bid(parsed: estimate_parse.ParsedEstimate) -> float | None:
    """The workbook's estimator-basis bid: BASE BID PRICE, else the recap
    TOTALS price."""
    if parsed.base_bid is not None:
        return float(parsed.base_bid)
    if parsed.recap_totals is not None and parsed.recap_totals.price:
        return float(parsed.recap_totals.price)
    return None


def summary_from_parsed(
    parsed: estimate_parse.ParsedEstimate,
    *,
    project_id: str | None = None,
    project_type: str | None = None,
) -> EstimateSummary:
    """Summary of a parsed estimator workbook (history side)."""
    hours = parsed.total_labor_hours
    rows = [
        {
            "line_id": f"{ln.sheet}!{ln.row}",
            "section": ln.section,
            "group": ln.group,
            "subgroup": ln.subgroup,
            "description": ln.description,
            "unit": ln.unit,
            "qty_base": ln.qty_base,
            "quantity": ln.quantity,
            "wastage": ln.wastage,
        }
        for ln in parsed.item_lines
    ]
    return summarize_rows(
        rows,
        hours=float(hours) if hours is not None else None,
        bid_usd=book_bid(parsed),
        project_id=project_id,
        project_type=project_type,
    )


def summary_from_priced(
    doc: Mapping[str, Any],
    *,
    project_id: str | None = None,
    project_type: str | None = None,
    bid_usd: float | None = None,
) -> EstimateSummary:
    """Summary of a `bdr.priced_estimate/1` artifact (run side). Hours are
    the estimator-basis hours (unit hours x wastage-inclusive quantity over
    lines that are not excluded), the measure the workbook TOTAL LABOR HOURS
    uses. The Verify bid is not on the estimator basis, so `bid_per_device`
    applies only when the caller passes an estimator-basis `bid_usd`."""
    lines = [ln for ln in (doc.get("lines") or []) if isinstance(ln, Mapping) and not ln.get("excluded")]
    hours = 0.0
    any_hours = False
    for ln in lines:
        uh, qty = _f(ln.get("unit_hours")), _f(ln.get("qty"))
        if uh is None or qty is None:
            continue
        hours += uh * qty
        any_hours = True
    return summarize_rows(
        lines,
        hours=hours if any_hours else None,
        bid_usd=bid_usd,
        project_id=project_id,
        project_type=project_type,
    )


def book_exclusion(
    parsed: estimate_parse.ParsedEstimate,
    *,
    filename: str = "",
) -> str | None:
    """`budget_rom`, `labor_only` or None (the documented book filter)."""
    names = " ".join([filename or "", *parsed.sheets])
    if BUDGET_NAME_RE.search(names):
        return "budget_rom"
    items = parsed.item_lines
    with_hours = [ln for ln in items if ln.unit_labor_hours is not None]
    priced = [ln for ln in with_hours if (ln.quantity_eff or Decimal(0)) > 0]
    if len(priced) < ROM_MIN_PRICED_LINES:
        return "budget_rom"
    if items and len(with_hours) / len(items) < ROM_MIN_HOURS_SHARE:
        return "budget_rom"
    material = float(parsed.material_total())
    labor = _f(parsed.summary.get("total_labor_cost"))
    if labor is None:
        rate = float(parsed.labor_rate) if parsed.labor_rate is not None else None
        labor = float(parsed.basis_hours()) * rate if rate is not None else None
    if labor is not None and material + labor > 0:
        if material / (material + labor) < LABOR_ONLY_MAX_MATERIAL_SHARE:
            return "labor_only"
    elif material <= 0:
        return "labor_only"
    return None


# Percentiles and bands -------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        raise ValueError("percentile of no values")
    vals = sorted(values)
    k = (len(vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def empirical_percentile(values: Sequence[float], value: float) -> float | None:
    """Share of values at or below `value`, 0 to 100."""
    if not values:
        return None
    return round(100.0 * sum(1 for v in values if v <= value) / len(values), 1)


@dataclass(frozen=True)
class LopoBaseline:
    """Leave-one-project-out placement of the stratum's own books."""

    n_evaluated: int = 0
    n_advisory: int = 0
    n_extreme: int = 0
    n_zero: int = 0
    advisory_rate: float = 0.0
    extreme_rate: float = 0.0
    zero_rate: float = 0.0
    critical_scope: str = "none"  # none | extreme | structural_zero
    critical_supported: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_evaluated": self.n_evaluated,
            "n_advisory": self.n_advisory,
            "n_extreme": self.n_extreme,
            "n_zero": self.n_zero,
            "advisory_rate": round(self.advisory_rate, 4),
            "extreme_rate": round(self.extreme_rate, 4),
            "zero_rate": round(self.zero_rate, 4),
            "critical_scope": self.critical_scope,
            "critical_supported": self.critical_supported,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> LopoBaseline:
        data = data or {}
        return cls(
            n_evaluated=int(data.get("n_evaluated") or 0),
            n_advisory=int(data.get("n_advisory") or 0),
            n_extreme=int(data.get("n_extreme") or 0),
            n_zero=int(data.get("n_zero") or 0),
            advisory_rate=float(data.get("advisory_rate") or 0),
            extreme_rate=float(data.get("extreme_rate") or 0),
            zero_rate=float(data.get("zero_rate") or 0),
            critical_scope=str(data.get("critical_scope") or "none"),
            critical_supported=bool(data.get("critical_supported")),
            reason=str(data.get("reason") or ""),
        )


@dataclass(frozen=True)
class MetricBand:
    """One metric over one stratum. `values` hold the non-zero per-project
    values the quantiles use; `zeros` the projects with a structural zero
    (kept out of the quantiles, counted by the LOPO baseline)."""

    metric: str
    stratum: str
    values: tuple[tuple[str, float], ...]
    zeros: tuple[str, ...] = ()
    lopo: LopoBaseline = field(default_factory=LopoBaseline)

    @property
    def n(self) -> int:
        return len(self.values)

    def quantiles(self) -> dict[str, float] | None:
        vals = [v for _p, v in self.values]
        if not vals:
            return None
        return {f"p{int(q * 100)}": percentile(vals, q) for q in QUANTILES}

    def without(self, project_ids: Iterable[str]) -> MetricBand:
        """The band with these projects left out (LOPO)."""
        drop = {str(p) for p in project_ids if p}
        if not drop:
            return self
        return replace(
            self,
            values=tuple((p, v) for p, v in self.values if p not in drop),
            zeros=tuple(p for p in self.zeros if p not in drop),
        )

    def to_dict(self) -> dict[str, Any]:
        q = self.quantiles() or {}
        return {
            "metric": self.metric,
            "stratum": self.stratum,
            "n": self.n,
            **{k: _r(v) for k, v in q.items()},
            "values": [{"project_id": p, "value": _r(v)} for p, v in self.values],
            "zeros": list(self.zeros),
            "lopo": self.lopo.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MetricBand:
        return cls(
            metric=str(data["metric"]),
            stratum=str(data["stratum"]),
            values=tuple((str(v["project_id"]), float(v["value"])) for v in data.get("values") or []),
            zeros=tuple(str(p) for p in data.get("zeros") or []),
            lopo=LopoBaseline.from_dict(data.get("lopo")),
        )


@dataclass(frozen=True)
class BandCheck:
    """One metric of one estimate placed against its (LOPO) band."""

    metric: str
    value: float | None
    stratum: str
    n: int
    p10: float | None
    p25: float | None
    p50: float | None
    p75: float | None
    p90: float | None
    position: str
    percentile: float | None
    severity: str
    level: str
    reason: str = ""
    band_version: str = ""
    line_ids: tuple[str, ...] = ()

    @property
    def advisory(self) -> bool:
        return self.severity == "warning"

    def to_dict(self, *, include_money: bool = True) -> dict[str, Any]:
        out = {
            "metric": self.metric,
            "value": _r(self.value, 4),
            "stratum": self.stratum,
            "n": self.n,
            "p10": _r(self.p10, 4),
            "p25": _r(self.p25, 4),
            "p50": _r(self.p50, 4),
            "p75": _r(self.p75, 4),
            "p90": _r(self.p90, 4),
            "position": self.position,
            "percentile": self.percentile,
            "severity": self.severity,
            "level": self.level,
            "reason": self.reason,
            "band_version": self.band_version,
            "line_ids": list(self.line_ids),
        }
        if not include_money and self.metric in MONEY_METRICS:
            for key in ("value", "p10", "p25", "p50", "p75", "p90"):
                out[key] = None
        return out


@dataclass(frozen=True)
class HistoryBands:
    """A versioned set of bands (one file under EVAL_ROOT/bands)."""

    band_version: str
    built_at: str
    bands: Mapping[tuple[str, str], MetricBand]
    projects: tuple[Mapping[str, Any], ...] = ()
    excluded_project_ids: tuple[str, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)
    schema: str = BANDS_SCHEMA
    basis: str = BASIS

    def band(self, metric: str, stratum: str) -> MetricBand | None:
        return self.bands.get((metric, stratum))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "band_version": self.band_version,
            "built_at": self.built_at,
            "basis": self.basis,
            "metrics_version": METRICS_VERSION,
            "settings": dict(self.settings),
            "excluded_project_ids": list(self.excluded_project_ids),
            "excluded_project_ids_sha256": excluded_project_ids_sha256(self.excluded_project_ids),
            "projects": [dict(p) for p in self.projects],
            "bands": [self.bands[k].to_dict() for k in sorted(self.bands)],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HistoryBands:
        if data.get("schema") != BANDS_SCHEMA:
            raise ValueError(f"not a history bands file: schema {data.get('schema')!r}")
        bands = {}
        for raw in data.get("bands") or []:
            b = MetricBand.from_dict(raw)
            bands[(b.metric, b.stratum)] = b
        return cls(
            band_version=str(data["band_version"]),
            built_at=str(data.get("built_at") or ""),
            bands=bands,
            projects=tuple(data.get("projects") or ()),
            excluded_project_ids=tuple(data.get("excluded_project_ids") or ()),
            settings=dict(data.get("settings") or {}),
            basis=str(data.get("basis") or BASIS),
        )


def current_settings(max_rate: float = LOPO_CRITICAL_MAX_RATE) -> dict[str, Any]:
    return {
        "min_devices": MIN_DEVICES,
        "min_stratum_n": MIN_STRATUM_N,
        "critical_min_n": CRITICAL_MIN_N,
        "lopo_critical_max_rate": max_rate,
        "extreme_low_factor": EXTREME_LOW_FACTOR,
        "extreme_high_factor": EXTREME_HIGH_FACTOR,
        "rom_min_priced_lines": ROM_MIN_PRICED_LINES,
        "rom_min_hours_share": ROM_MIN_HOURS_SHARE,
        "labor_only_max_material_share": LABOR_ONLY_MAX_MATERIAL_SHARE,
        "building_types": sorted(BUILDING_TYPES) + [p + "*" for p in BUILDING_TYPE_PREFIXES],
    }


# Placement ----------------------------------------------------------------------------------


def _place(value: float, q: Mapping[str, float]) -> str:
    p10, p25, p75, p90 = q["p10"], q["p25"], q["p75"], q["p90"]
    if value < p10 * EXTREME_LOW_FACTOR:
        return "below_extreme"
    if value > p90 * EXTREME_HIGH_FACTOR:
        return "above_extreme"
    if value < p10:
        return "below"
    if value > p90:
        return "above"
    if value < p25:
        return "low"
    if value > p75:
        return "high"
    return "in_band"


def _is_structural_zero(spec: MetricSpec, value: float | None) -> bool:
    return spec.structural_zero and value is not None and value == 0


def lopo_baseline(band: MetricBand, *, max_rate: float = LOPO_CRITICAL_MAX_RATE) -> LopoBaseline:
    """Place every project of the band against the band without it."""
    spec = METRICS[band.metric]
    n_eval = n_adv = n_ext = 0
    for pid, value in band.values:
        q = band.without([pid]).quantiles()
        if q is None or band.n - 1 < MIN_STRATUM_N:
            continue
        n_eval += 1
        pos = _place(value, q)
        if pos in ("below", "above"):
            n_adv += 1
        elif pos in ("below_extreme", "above_extreme"):
            n_ext += 1
    n_zero = len(band.zeros)
    population = band.n + n_zero
    zero_rate = n_zero / population if population else 0.0
    if n_zero and band.n >= MIN_STRATUM_N:
        n_eval += n_zero  # a structural zero is always an extreme placement
        n_ext += n_zero
    adv_rate = n_adv / n_eval if n_eval else 0.0
    ext_rate = n_ext / n_eval if n_eval else 0.0
    if spec.structural_zero:
        scope = "structural_zero"
        ok = band.n >= CRITICAL_MIN_N and zero_rate <= max_rate
        reason = (
            f"structural zero rate {zero_rate:.3f} over {population} books"
            f" {'<=' if zero_rate <= max_rate else '>'} {max_rate}"
        )
    elif spec.level == "total":
        scope = "extreme"
        ok = band.n >= CRITICAL_MIN_N and n_eval > 0 and ext_rate <= max_rate
        reason = (
            f"LOPO extreme rate {ext_rate:.3f} over {n_eval} books"
            f" {'<=' if ext_rate <= max_rate else '>'} {max_rate}"
        )
    else:
        scope, ok, reason = "none", False, "component metric: advisory only"
    if scope != "none" and band.n < CRITICAL_MIN_N:
        reason += f"; n {band.n} < {CRITICAL_MIN_N}"
    return LopoBaseline(
        n_evaluated=n_eval,
        n_advisory=n_adv,
        n_extreme=n_ext,
        n_zero=n_zero,
        advisory_rate=adv_rate,
        extreme_rate=ext_rate,
        zero_rate=zero_rate,
        critical_scope=scope,
        critical_supported=ok,
        reason=reason,
    )


# Build ----------------------------------------------------------------------------------------


def _now_pacific() -> dt.datetime:
    return dt.datetime.now(COMPANY_TZ)


def band_version_for(payload: Mapping[str, Any], built_at: dt.datetime) -> str:
    """`hb<metrics version>-<YYYYMMDD Pacific>-<8 hex of the inputs>`."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    day = built_at.astimezone(COMPANY_TZ).strftime("%Y%m%d")
    return f"hb{METRICS_VERSION}-{day}-{digest[:8]}"


def build_bands(
    summaries: Iterable[EstimateSummary],
    *,
    excluded_project_ids: Iterable[str] = (),
    book_exclusions: Mapping[str, str] | None = None,
    built_at: dt.datetime | None = None,
    max_rate: float = LOPO_CRITICAL_MAX_RATE,
    band_version: str | None = None,
) -> HistoryBands:
    """Bands per metric and stratum from per-project summaries. Projects in
    `excluded_project_ids` (the holdout, plus the case under LOPO) are dropped
    entirely; projects in `book_exclusions` are listed with the reason and
    contribute no value."""
    excluded = tuple(sorted({str(p) for p in excluded_project_ids if p}))
    drop = set(excluded)
    book_exclusions = dict(book_exclusions or {})
    built = built_at or _now_pacific()
    per_band: dict[tuple[str, str], list[tuple[str, float]]] = {}
    zeros: dict[tuple[str, str], list[str]] = {}
    projects: list[dict[str, Any]] = []
    for s in sorted(summaries, key=lambda x: str(x.project_id)):
        pid = str(s.project_id or "")
        if not pid or pid in drop:
            continue
        reason = book_exclusions.get(pid)
        values = s.metric_values()
        projects.append(
            {
                **s.to_dict(),
                "strata": list(strata_for_type(s.project_type)),
                "excluded_reason": reason,
                "metrics": {m: _r(v) for m, v in values.items()},
            }
        )
        if reason:
            continue
        for stratum in strata_for_type(s.project_type):
            for metric, value in values.items():
                if value is None:
                    continue
                key = (metric, stratum)
                if _is_structural_zero(METRICS[metric], value):
                    zeros.setdefault(key, []).append(pid)
                    continue
                if value <= 0:
                    continue
                per_band.setdefault(key, []).append((pid, float(value)))
    bands: dict[tuple[str, str], MetricBand] = {}
    for key in sorted(set(per_band) | set(zeros)):
        raw = MetricBand(
            metric=key[0], stratum=key[1],
            values=tuple(per_band.get(key, ())), zeros=tuple(zeros.get(key, ())),
        )
        bands[key] = replace(raw, lopo=lopo_baseline(raw, max_rate=max_rate))
    settings = current_settings(max_rate)
    version = band_version or band_version_for(
        {
            "metrics_version": METRICS_VERSION,
            "settings": settings,
            "excluded": excluded,
            "projects": [(p["project_id"], p["excluded_reason"], p["metrics"]) for p in projects],
        },
        built,
    )
    return HistoryBands(
        band_version=version,
        built_at=built.astimezone(COMPANY_TZ).isoformat(timespec="seconds"),
        bands=bands,
        projects=tuple(projects),
        excluded_project_ids=excluded,
        settings=settings,
    )


# Check ----------------------------------------------------------------------------------------


def choose_stratum(bands: HistoryBands, metric: str, project_type: Any, *, exclude: Iterable[str] = ()) -> str:
    """`building` for building types; `roadway` for roadway when its band
    holds MIN_STRATUM_N values; else `all`."""
    if is_building_type(project_type):
        return "building"
    if str(project_type or "").strip().lower() == "roadway":
        band = bands.band(metric, "roadway")
        if band is not None and band.without(exclude).n >= MIN_STRATUM_N:
            return "roadway"
    return "all"


def check_bands(
    estimate_summary: EstimateSummary,
    bands: HistoryBands,
    *,
    exclude_project_ids: Iterable[str] = (),
) -> list[BandCheck]:
    """Place every metric of the estimate against its band, leaving the
    estimate's own project (and any extra ids) out of the band."""
    exclude = {str(p) for p in exclude_project_ids if p}
    if estimate_summary.project_id:
        exclude.add(str(estimate_summary.project_id))
    values = estimate_summary.metric_values()
    numerator_ids = {
        "hours_per_device": (),
        "branch_wire_ft_per_device": ("branch_wire",),
        "emt_ft_per_device": ("emt34", "emt_other"),
        "wire_per_emt34": ("branch_wire", "emt34"),
        "bid_per_device": (),
    }
    out: list[BandCheck] = []
    for metric in METRIC_IDS:
        spec = METRICS[metric]
        value = values.get(metric)
        stratum = choose_stratum(bands, metric, estimate_summary.project_type, exclude=exclude)
        stored = bands.band(metric, stratum)
        band = stored.without(exclude) if stored is not None else None
        q = band.quantiles() if band is not None else None
        ids = tuple(
            i for cls in numerator_ids[metric] for i in estimate_summary.line_ids.get(cls, ())
        )
        base = {
            "metric": metric,
            "value": value,
            "stratum": stratum,
            "n": band.n if band is not None else 0,
            "level": spec.level,
            "band_version": bands.band_version,
            "line_ids": ids,
        }
        if value is None:
            reason = (
                f"devices {estimate_summary.devices:g} < {MIN_DEVICES}" if spec.per_device
                and estimate_summary.devices < MIN_DEVICES else "inputs missing"
            )
            out.append(BandCheck(**base, **_nq(q), position="not_applicable", percentile=None,
                                 severity="none", reason=reason))
            continue
        if band is None or q is None or band.n < MIN_STRATUM_N:
            out.append(BandCheck(**base, **_nq(q), position="not_applicable", percentile=None,
                                 severity="none", reason=f"band n {base['n']} < {MIN_STRATUM_N}"))
            continue
        vals = [v for _p, v in band.values]
        lopo = stored.lopo if stored is not None else LopoBaseline()
        if _is_structural_zero(spec, value):
            pos = "below_extreme"
            if lopo.critical_supported:
                sev, reason = "critical", f"structural zero; {lopo.reason}"
            else:
                sev, reason = "warning", f"structural zero; critical not supported ({lopo.reason})"
        else:
            pos = _place(value, q)
            if pos in ("in_band", "low", "high"):
                sev, reason = "none", "inside p10 to p90"
            elif pos in ("below", "above"):
                sev, reason = "warning", "outside p10 to p90 (advisory)"
            elif spec.level == "total" and lopo.critical_supported:
                sev, reason = "critical", f"outside [p10/2, 2 x p90]; {lopo.reason}"
            else:
                why = lopo.reason if spec.level == "total" else "component metric"
                sev, reason = "warning", f"outside [p10/2, 2 x p90]; advisory ({why})"
        out.append(BandCheck(**base, **_nq(q), position=pos,
                             percentile=empirical_percentile(vals, value), severity=sev, reason=reason))
    return out


def _nq(q: Mapping[str, float] | None) -> dict[str, float | None]:
    keys = ("p10", "p25", "p50", "p75", "p90")
    if not q:
        return {k: None for k in keys}
    return {k: q[k] for k in keys}


def worst_severity(checks: Iterable[BandCheck]) -> str:
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    return max((c.severity for c in checks), key=lambda s: rank.get(s, 0), default="none")


# Files ------------------------------------------------------------------------------------------


def bands_dir(root: Path | str | None = None) -> Path:
    if root is None:
        from app.core.config import get_settings

        root = get_settings().eval_root
    return Path(root) / "bands"


def save_bands(bands: HistoryBands, root: Path | str | None = None) -> Path:
    """Write `<root>/bands/<band_version>.json` (root defaults to EVAL_ROOT)."""
    folder = bands_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{bands.band_version}.json"
    path.write_text(json.dumps(bands.to_dict(), indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return path


def load_bands(version_or_path: str | Path, root: Path | str | None = None) -> HistoryBands:
    """Read a bands file by path or by band_version under `<root>/bands`."""
    path = Path(version_or_path)
    if not path.suffix:
        path = bands_dir(root) / f"{version_or_path}.json"
    return HistoryBands.from_dict(json.loads(path.read_text(encoding="utf-8")))


def latest_bands_path(root: Path | str | None = None) -> Path | None:
    folder = bands_dir(root)
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("hb*.json"), key=lambda p: (p.stat().st_mtime, p.name))
    return files[-1] if files else None


BANDS_TABLE = "ai_history_bands"
_PERSIST_CHUNK = 200


def manifest_sha256(bands: HistoryBands) -> str:
    """sha256 of the canonical bands document (the file `save_bands` writes,
    minus formatting): the identity of the inputs a persisted band row came
    from."""
    payload = json.dumps(bands.to_dict(), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def band_rows(bands: HistoryBands, *, active: bool | None = False) -> list[dict[str, Any]]:
    """One `ai_history_bands` row per (band_version, metric, stratum).
    `values` carries {project_id, value} per project, `zeros` the projects
    with a structural zero, `lopo` the baseline that decides critical.
    `active=None` leaves the `active` column out of the row, so an upsert
    keeps an existing row's flag and a new row takes the column default
    (false, migration MF)."""
    digest = manifest_sha256(bands)
    out: list[dict[str, Any]] = []
    for key in sorted(bands.bands):
        b = bands.bands[key]
        q = b.quantiles() or {}
        row: dict[str, Any] = {
            "band_version": bands.band_version,
            "metric": b.metric,
            "stratum": b.stratum,
            "n": b.n,
            "values": [{"project_id": p, "value": _r(v)} for p, v in b.values],
            "zeros": list(b.zeros),
            "lopo": b.lopo.to_dict(),
            "basis": bands.basis,
            "settings": dict(bands.settings),
            "excluded_project_ids": list(bands.excluded_project_ids),
            "manifest_sha256": digest,
        }
        if active is not None:
            row["active"] = bool(active)
        if bands.built_at:
            row["built_at"] = bands.built_at
        for name in ("p10", "p25", "p50", "p75", "p90"):
            if q.get(name) is not None:
                row[name] = _r(q[name])
        out.append(row)
    return out


def persist_bands(sb: Any, bands: HistoryBands, *, activate: bool = True) -> int:
    """Upsert one `ai_history_bands` row per (band_version, metric, stratum)
    (migration MF) and, with `activate`, make this band_version the only
    active one. Returns the number of rows written. Sync: call from a worker
    thread or a plain `def` route, never inside `async def`.

    The upserted rows never carry `active`: a new version's rows start
    inactive (the column default) and a version already stored keeps its
    flag, so re-persisting the active version with `activate=False` (a
    backfill or a `--persist` rerun) is a no-op on the flag and never leaves
    zero active bands. With `activate`, every other version is switched off,
    then this version is switched on (at most one active version visible)."""
    rows = band_rows(bands, active=None)
    if not rows:
        return 0
    for i in range(0, len(rows), _PERSIST_CHUNK):
        sb.table(BANDS_TABLE).upsert(
            rows[i:i + _PERSIST_CHUNK], on_conflict="band_version,metric,stratum"
        ).execute()
    if activate:
        sb.table(BANDS_TABLE).update({"active": False}).neq(
            "band_version", bands.band_version
        ).eq("active", True).execute()
        sb.table(BANDS_TABLE).update({"active": True}).eq(
            "band_version", bands.band_version
        ).execute()
    return len(rows)


def bands_from_rows(rows: Iterable[Mapping[str, Any]]) -> HistoryBands | None:
    """Rebuild a HistoryBands from `ai_history_bands` rows of ONE
    band_version (the latest built_at wins when several are given)."""
    rows = [r for r in rows if isinstance(r, Mapping) and r.get("band_version")]
    if not rows:
        return None
    latest = max(rows, key=lambda r: (str(r.get("built_at") or ""), str(r["band_version"])))
    version = str(latest["band_version"])
    mine = [r for r in rows if str(r["band_version"]) == version]
    bands: dict[tuple[str, str], MetricBand] = {}
    for r in mine:
        metric, stratum = str(r["metric"]), str(r["stratum"])
        if metric not in METRICS:
            continue
        values = tuple(
            (str(v["project_id"]), float(v["value"]))
            for v in (r.get("values") or [])
            if isinstance(v, Mapping) and v.get("value") is not None
        )
        bands[(metric, stratum)] = MetricBand(
            metric=metric,
            stratum=stratum,
            values=values,
            zeros=tuple(str(p) for p in r.get("zeros") or []),
            lopo=LopoBaseline.from_dict(r.get("lopo")),
        )
    return HistoryBands(
        band_version=version,
        built_at=str(latest.get("built_at") or ""),
        bands=bands,
        excluded_project_ids=tuple(str(p) for p in latest.get("excluded_project_ids") or []),
        settings=dict(latest.get("settings") or {}),
        basis=str(latest.get("basis") or BASIS),
    )


def load_active_bands(sb: Any, *, band_version: str | None = None) -> HistoryBands | None:
    """The active band set from `ai_history_bands` (or `band_version` when
    given), None when no row exists or the table is not there yet (MF not
    applied). Sync: call from a worker thread or a plain `def` route."""
    try:
        query = sb.table(BANDS_TABLE).select("*")
        query = query.eq("band_version", band_version) if band_version else query.eq("active", True)
        rows = query.execute().data or []
    except Exception:  # noqa: BLE001 - a missing table reads as no bands
        logger.warning("plans_reader_bands: ai_history_bands unavailable", exc_info=True)
        return None
    return bands_from_rows(rows)


# Backup ------------------------------------------------------------------------------------------


def summaries_from_backup(
    backup_root: str | Path,
) -> tuple[list[EstimateSummary], dict[str, str], dict[str, str]]:
    """Per-project summaries of the chosen book (one-book rule) of every
    estimate workbook in the backup, the book-filter exclusions, and the
    chosen file name per project. Reads the backup on disk only."""
    from app.services.cost_library import backup_books

    root = Path(backup_root)
    report, chosen = backup_books(root)
    with open(root / "data" / "projects.json", encoding="utf-8") as fh:
        rows = json.load(fh)
    if isinstance(rows, dict):
        rows = rows.get("rows") or []
    types = {str(p.get("id")): p.get("project_type") for p in rows}
    filenames = {e.file.project_id: e.file.filename for e in report.entries if e.chosen}
    summaries: list[EstimateSummary] = []
    exclusions: dict[str, str] = {}
    for pid in sorted(chosen):
        parsed = chosen[pid]
        summaries.append(summary_from_parsed(parsed, project_id=pid, project_type=types.get(pid)))
        reason = book_exclusion(parsed, filename=filenames.get(pid, ""))
        if reason:
            exclusions[pid] = reason
    return summaries, exclusions, filenames


def build_from_backup(
    backup_root: str | Path,
    *,
    excluded_project_ids: Iterable[str],
    built_at: dt.datetime | None = None,
    max_rate: float = LOPO_CRITICAL_MAX_RATE,
) -> HistoryBands:
    summaries, exclusions, _names = summaries_from_backup(backup_root)
    return build_bands(
        summaries,
        excluded_project_ids=excluded_project_ids,
        book_exclusions=exclusions,
        built_at=built_at,
        max_rate=max_rate,
    )


# Report ------------------------------------------------------------------------------------------


def report_markdown(bands: HistoryBands) -> str:
    """The build report: n and quantiles per metric and stratum, and the
    LOPO baseline per metric. Lives under EVAL_ROOT/reports (it carries the
    bid-per-device dollars)."""
    excluded_books = [p for p in bands.projects if p.get("excluded_reason")]
    lines = [
        f"# History bands {bands.band_version}",
        "",
        f"Built {bands.built_at} (America/Los_Angeles). Basis: {bands.basis}. "
        f"Metrics version {METRICS_VERSION}.",
        "",
        f"Projects with a chosen book: {len(bands.projects)}; dropped by the book filter: "
        f"{len(excluded_books)}; excluded ids (holdout and LOPO): {len(bands.excluded_project_ids)} "
        f"(sha256 {excluded_project_ids_sha256(bands.excluded_project_ids)[:12]}).",
        "",
        "Settings: " + ", ".join(f"{k} {v}" for k, v in bands.settings.items()),
        "",
        "## Bands",
        "",
        "| Metric | Stratum | n | zeros | p10 | p25 | p50 | p75 | p90 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for (metric, stratum) in sorted(bands.bands, key=lambda k: (METRIC_IDS.index(k[0]), STRATA.index(k[1]))):
        b = bands.bands[(metric, stratum)]
        q = b.quantiles() or {}
        cells = " | ".join(f"{q[k]:.3f}" if k in q else "" for k in ("p10", "p25", "p50", "p75", "p90"))
        lines.append(f"| {metric} | {stratum} | {b.n} | {len(b.zeros)} | {cells} |")
    lines += [
        "",
        "## LOPO baseline (building stratum)",
        "",
        "Each building book placed against the band without it. Critical is allowed only "
        "where `critical_supported` is yes.",
        "",
        "| Metric | n | evaluated | advisory | extreme | zeros | extreme rate | scope | critical supported | reason |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for metric in METRIC_IDS:
        b = bands.band(metric, "building")
        if b is None:
            lines.append(f"| {metric} | 0 | | | | | | | no | no values |")
            continue
        lo = b.lopo
        lines.append(
            f"| {metric} | {b.n} | {lo.n_evaluated} | {lo.n_advisory} | {lo.n_extreme} | {lo.n_zero} | "
            f"{lo.extreme_rate:.3f} | {lo.critical_scope} | {'yes' if lo.critical_supported else 'no'} | "
            f"{lo.reason} |"
        )
    lines += ["", "## Books dropped by the filter", ""]
    if not excluded_books:
        lines.append("None.")
    for p in excluded_books:
        lines.append(f"- {str(p['project_id'])[:8]} ({p.get('project_type')}): {p['excluded_reason']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "BANDS_SCHEMA", "METRICS", "METRIC_IDS", "MetricSpec", "EstimateSummary", "MetricBand",
    "LopoBaseline", "BandCheck", "HistoryBands", "classify_row", "is_building_type",
    "strata_for_type", "summarize_rows", "summary_from_parsed", "summary_from_priced",
    "book_exclusion", "book_bid", "percentile", "empirical_percentile", "lopo_baseline",
    "build_bands", "band_version_for", "choose_stratum", "check_bands", "worst_severity",
    "bands_dir", "save_bands", "load_bands", "latest_bands_path", "persist_bands",
    "load_active_bands", "bands_from_rows", "band_rows", "manifest_sha256", "BANDS_TABLE",
    "summaries_from_backup", "build_from_backup", "report_markdown",
]
