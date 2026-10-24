"""G3 cost library: unit labor hours and unit material cost per item, built
from the estimator workbooks (AI_ESTIMATING_BUILD_SPEC WS6, `cost_library`).

Wave 1 builds the library in memory from parsed workbooks (`from_backup`,
`from_observations`); `load(sb, ...)` pages the same observations out of the
ME `cost_observations` table once the migration exists. Everything below the
constructor is pure: no I/O, no database, no model.

Statistics per item key and canonical unit of measure:

1. each project contributes ONE value per item (the median of its rows);
2. labor unit = median across projects, p25 and p75 across projects, no
   recency decay;
3. material unit = recency-weighted median across projects, weight
   `0.5 ** (age_days / 180)` from the workbook date relative to the newest
   observation in the library; p25 and p75 unweighted;
4. outliers: with at least four projects, a project value outside
   [p25 - 3 x IQR, p75 + 3 x IQR] of the others is excluded;
5. demolition items carry material 0 by policy (the estimators price labor
   only on removals).

Matching ladder (labor and material from the same rung): `exact` (same
library key and unit; FT, C and M convert within the length family) ->
`compatible` (same family, no hard conflict, same modifiers, every attribute
present on both sides equal) -> `context` (same family only; quality capped
at weak) -> `fallback` (class-level median; quality weak, the engine flags
`fallback_price`) -> `none`. Seeds are the policy's and the engine applies
them only with a cited spec fact. Strict unit equality at every rung: never
EA against FT, never LS, never a count against a length or an area.

Leave-one-project-out: `exclude_projects` drops those projects' observations
before any statistic is computed, so an eval case never sees itself; the
`fingerprint` covers exactly the observations used. Tool results built from
this module carry aggregates only (medians, quartiles, n, month), never a
project id, project name or GC name.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.services import estimate_parse
from app.services.electrical_items import (
    LINEAR_LUMINAIRE_RE,
    BoqRow,
    ItemIdentity,
    canon_uom,
    convert_unit_value,
    hard_conflict,
    parse_item,
    similarity,
)
from app.services.pricing_types import ValueStat

LIBRARY_VERSION = 1
MATERIAL_HALF_LIFE_DAYS = 180
OUTLIER_IQR_FACTOR = 3.0
OUTLIER_MIN_PROJECTS = 4
COMPATIBLE_POOL_TOLERANCE = 0.25
# A compatible, context or fallback value needs at least this many projects;
# with fewer the ladder moves on (a single project's number is a coin flip).
MIN_PROJECTS_BELOW_EXACT = 2
# Attributes that decide a class: a candidate lacking one the query has (or
# the reverse) is never compatible (a bare 'conductor:wire' key must not
# price 500 kcmil).
CRITICAL_ATTRS: dict[str, tuple[str, ...]] = {
    "conductor": ("conductor_size", "material"),
    "conduit": ("conduit_size", "conduit_type"),
    "cable": ("cable", "cores", "conductor_size"),
    "raceway": ("size",),
    "breaker": ("amps", "poles"),
    "panel": ("amps", "kva"),
}
# Classes whose class-level median is an acceptable last resort (a device
# is a device). Sizes dominate the rest (a 250 kcmil at the #12 rate is not
# a price), so those fall to `none` and the engine reports them unpriced.
FALLBACK_CLASSES: frozenset[str] = frozenset({
    "receptacle", "switch", "control", "luminaire", "luminaire_linear",
    "equipment_connection", "demolition", "other",
})
FALLBACK_UOMS: frozenset[str] = frozenset({"EA", "FT", "C", "M"})
_EM_TAG_RE = re.compile(r"(?:-?EM|-E)$")
DB_PAGE_SIZE = 1000
_LENGTH_UOMS = ("FT", "C", "M")
# Attributes that never decide identity across projects.
_NON_IDENTITY_ATTRS = ("tag", "desc")
_SIX = Decimal("0.000001")


class LibraryUnavailable(RuntimeError):
    """The database path cannot serve observations (table missing or no client)."""


@dataclass(frozen=True)
class QualityThresholds:
    """When a looked-up value is strong, good or weak (policy data; the LOPO
    eval tunes them)."""

    # Calibrated on the 2026-10-21 LOPO sweep (scripts/pricing_eval.py, 33
    # cases, every library built with eval_manifest.excluded_projects so the
    # holdout and the case's own project are out; 1,161 exact-match lines):
    # the share of exact matches off by more than 35% from the project's own
    # estimator value is 15% at n >= 10 projects and IQR/median <= 0.25, 19%
    # at n >= 10 and <= 0.5, and 39% to 56% in every thinner or wider bucket.
    strong_min_projects: int = 10
    strong_max_iqr_ratio: float = 0.25
    good_min_projects: int = 5
    good_max_iqr_ratio: float = 0.5
    compatible_good_min_projects: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "strong_min_projects": self.strong_min_projects,
            "strong_max_iqr_ratio": self.strong_max_iqr_ratio,
            "good_min_projects": self.good_min_projects,
            "good_max_iqr_ratio": self.good_max_iqr_ratio,
            "compatible_good_min_projects": self.compatible_good_min_projects,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> QualityThresholds:
        data = data or {}
        return cls(
            strong_min_projects=int(data.get("strong_min_projects", 10)),
            strong_max_iqr_ratio=float(data.get("strong_max_iqr_ratio", 0.25)),
            good_min_projects=int(data.get("good_min_projects", 5)),
            good_max_iqr_ratio=float(data.get("good_max_iqr_ratio", 0.5)),
            compatible_good_min_projects=int(data.get("compatible_good_min_projects", 10)),
        )


DEFAULT_THRESHOLDS = QualityThresholds()


@dataclass(frozen=True)
class Observation:
    """One estimator line as the library sees it."""

    project_id: str
    item_key: str
    family: str
    item_class: str
    uom: str
    unit_labor_hours: float | None
    unit_material_cost: float | None
    observed_on: dt.date | None
    description: str = ""
    group: str = ""
    source_ref: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "item_key": self.item_key,
            "family": self.family,
            "item_class": self.item_class,
            "uom": self.uom,
            "unit_labor_hours": self.unit_labor_hours,
            "unit_material_cost": self.unit_material_cost,
            "observed_on": self.observed_on.isoformat() if self.observed_on else None,
            "raw_description": self.description,
            "group_name": self.group,
            "source_ref": self.source_ref,
        }


_TAG_LEAD_RE = re.compile(r"^(?:TYPE\s+)?[A-Z][A-Z0-9.-]{0,5}\s*(?:,|:|\s-\s)\s*")
_FORM_FT_RE = re.compile(r"\b(\d)\s*'?\s*X\s*(\d)\s*'?(?![\d\"])")
_SLUG_RE = re.compile(r"[^A-Z0-9]+")
_DEMO_SUBJECT_SPLIT_RE = re.compile(r"\s+(?:W/|WITH|&|AND|,|INCLUDING|BACK TO|TO POINT)\b")
_DEMO_LEAD_RE = re.compile(
    r"^(?:REMOVAL|REMOVE|DEMO|DEMOLITION|DEMOLISH)\b(?:\s+OF)?(?:\s+(?:THE|ALL))?"
    r"(?:\s+EXISTING|\s+\(E\))?\s*"
)
_FIXTURE_CLASSES = ("luminaire", "luminaire_linear", "control", "other", "switch")


def _slug(text: str, limit: int = 40) -> str:
    return _SLUG_RE.sub("_", text).strip("_")[:limit].rstrip("_").lower()


def _fixture_signature(identity: ItemIdentity) -> dict[str, str] | None:
    """For a tagged fixture-schedule line ('L1 - 2' X 4' RECESSED TROFFER -
    RE - LED - ...'), the cross-project attributes: the first descriptive
    segment as `kind`, the form (2X4) and the schedule size. The fixture tag
    itself is project-local and never part of the key."""
    norm = identity.norm
    if "tag" not in identity.attrs or not _TAG_LEAD_RE.match(norm):
        return None
    rest = _TAG_LEAD_RE.sub("", norm, count=1)
    segment = re.split(r"\s-\s|,", rest, maxsplit=1)[0].strip()
    if not segment:
        return None
    attrs = {"kind": _slug(segment)}
    form = _FORM_FT_RE.search(segment)
    if form:
        attrs["form"] = f"{form.group(1)}X{form.group(2)}"
    size = identity.attrs.get("size")
    if size:
        attrs["size"] = size
    length = identity.attrs.get("length_ft")
    if length and not form:
        attrs["length_ft"] = length
    return attrs


def library_key(identity: ItemIdentity) -> str:
    """The cross-project identity of an item: the item_key without the
    project-local attributes (fixture tag, description slug of tagged
    fixtures). Tagged fixture lines key on their schedule signature (kind,
    form, size) under the luminaire family; demolition rows key on the
    removed subject's leading words so 'REMOVAL OF LIGHTING FIXTURE ...' and
    'REMOVAL OF POWER DEVICES ...' never share a value."""
    family = identity.family
    attrs = {k: v for k, v in identity.attrs.items() if k not in _NON_IDENTITY_ATTRS}
    if identity.item_class in ("other", "control", "equipment_connection", "panel") and (
        "desc" in identity.attrs and "tag" not in identity.attrs
    ):
        attrs["desc"] = identity.attrs["desc"]
    modifiers = list(identity.modifiers)
    if identity.item_class in _FIXTURE_CLASSES:
        sig = _fixture_signature(identity)
        if sig is not None:
            linear = bool(LINEAR_LUMINAIRE_RE.search(identity.norm))
            family = "luminaire_linear" if linear else "luminaire"
            attrs = sig
            # An EM fixture tag (L1-EM, L1E) is the battery variant: a modifier.
            if _EM_TAG_RE.search(identity.attrs.get("tag", "")) and "EM" not in modifiers:
                modifiers.append("EM")
    if identity.item_class == "demolition":
        rest = _DEMO_LEAD_RE.sub("", identity.norm, count=1)
        subject = _DEMO_SUBJECT_SPLIT_RE.split(rest, maxsplit=1)[0].strip()
        if subject:
            attrs["subject_words"] = _slug(subject, 32)
    parts = [family]
    parts.extend(f"{k}={v}" for k, v in sorted(attrs.items()))
    parts.extend(f"mod={m}" for m in sorted(modifiers))
    return "|".join(parts)


def key_attrs(key: str) -> tuple[dict[str, str], frozenset[str]]:
    """(attributes, modifiers) encoded in a library key."""
    attrs: dict[str, str] = {}
    mods: set[str] = set()
    for part in key.split("|")[1:]:
        name, _sep, value = part.partition("=")
        if name == "mod":
            mods.add(value)
        else:
            attrs[name] = value
    return attrs, frozenset(mods)


def library_family(identity: ItemIdentity) -> str:
    """The family a library key was filed under (see library_key)."""
    return library_key(identity).split("|", 1)[0]


def identity_of(description: Any, unit: Any) -> ItemIdentity:
    return parse_item(description, unit)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def observations_from_book(
    project_id: str,
    parsed: estimate_parse.ParsedEstimate,
    *,
    fallback_date: dt.date | None = None,
) -> tuple[list[Observation], list[dict[str, Any]]]:
    """Observations of one parsed workbook plus the rows left out with a
    reason (allowance, zero quantity, unit unknown, no labor value)."""
    out: list[Observation] = []
    skipped: list[dict[str, Any]] = []
    when = parsed.workbook_date or fallback_date
    for ln in parsed.lines:
        ref = f"{ln.sheet}!{ln.row}"
        if ln.is_allowance:
            skipped.append({"ref": ref, "reason": "allowance"})
            continue
        qty = ln.quantity_eff
        if qty is None or qty <= 0:
            skipped.append({"ref": ref, "reason": "zero_qty"})
            continue
        if ln.unit_labor_hours is None:
            skipped.append({"ref": ref, "reason": "no_labor_value"})
            continue
        identity = parse_item(ln.description, ln.unit)
        if not identity.uom or identity.uom == "LS":
            skipped.append({"ref": ref, "reason": "unparsed_unit"})
            continue
        material = _to_float(ln.unit_material_cost)
        if material is None:
            material = 0.0  # a blank unit material cell prices the row at $0 material
        if identity.item_class == "demolition":
            material = 0.0
        out.append(
            Observation(
                project_id=project_id,
                item_key=library_key(identity),
                family=library_family(identity),
                item_class=identity.item_class,
                uom=identity.uom,
                unit_labor_hours=_to_float(ln.unit_labor_hours),
                unit_material_cost=material,
                observed_on=when,
                description=ln.description,
                group=ln.group,
                source_ref=ref,
            )
        )
    return out, skipped


# Statistics -------------------------------------------------------------------------


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values))


def _quartiles(values: Sequence[float]) -> tuple[float, float]:
    if len(values) == 1:
        return values[0], values[0]
    srt = sorted(values)
    if len(srt) < 4:
        return srt[0], srt[-1]
    q = statistics.quantiles(srt, n=4, method="inclusive")
    return float(q[0]), float(q[2])


def _weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    pairs = sorted(zip(values, weights, strict=True))
    total = sum(w for _v, w in pairs)
    if total <= 0:
        return _median(values)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def _drop_outliers(per_project: dict[str, float]) -> tuple[dict[str, float], list[str]]:
    """Exclude project values outside [p25 - k IQR, p75 + k IQR] of the OTHER
    projects, when at least OUTLIER_MIN_PROJECTS projects contribute."""
    if len(per_project) < OUTLIER_MIN_PROJECTS:
        return dict(per_project), []
    excluded: list[str] = []
    for pid, value in per_project.items():
        others = [v for p, v in per_project.items() if p != pid]
        p25, p75 = _quartiles(others)
        iqr = p75 - p25
        low, high = p25 - OUTLIER_IQR_FACTOR * iqr, p75 + OUTLIER_IQR_FACTOR * iqr
        if value < low or value > high:
            excluded.append(pid)
    kept = {p: v for p, v in per_project.items() if p not in excluded}
    return (kept, excluded) if kept else (dict(per_project), [])


@dataclass
class ItemStats:
    """The library's numbers for one (library key, uom)."""

    item_key: str
    family: str
    item_class: str
    uom: str
    n_obs: int
    n_projects: int
    latest_seen: dt.date | None
    labor_median: float | None
    labor_p25: float | None
    labor_p75: float | None
    labor_projects: int
    material_median: float | None
    material_p25: float | None
    material_p75: float | None
    material_projects: int
    descriptions: tuple[str, ...] = ()
    outliers_labor: tuple[str, ...] = ()
    outliers_material: tuple[str, ...] = ()

    @property
    def latest_month(self) -> str | None:
        return self.latest_seen.strftime("%Y-%m") if self.latest_seen else None


def _per_project(obs: Sequence[Observation], attr: str) -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for o in obs:
        v = getattr(o, attr)
        if v is None:
            continue
        groups.setdefault(o.project_id, []).append(v)
    return {p: _median(vs) for p, vs in groups.items()}


def compute_stats(obs: Sequence[Observation], *, as_of: dt.date | None) -> ItemStats:
    first = obs[0]
    labor_pp, labor_out = _drop_outliers(_per_project(obs, "unit_labor_hours"))
    mat_pp, mat_out = _drop_outliers(_per_project(obs, "unit_material_cost"))
    dates = [o.observed_on for o in obs if o.observed_on]
    latest = max(dates) if dates else None
    labor_vals = list(labor_pp.values())
    mat_vals = list(mat_pp.values())
    labor_med = _median(labor_vals) if labor_vals else None
    labor_q = _quartiles(labor_vals) if labor_vals else (None, None)
    if first.item_class == "demolition":
        mat_med, mat_q = 0.0, (0.0, 0.0)
        mat_pp = {p: 0.0 for p in labor_pp} or mat_pp
    elif mat_vals:
        weights = []
        for pid in mat_pp:
            when = max((o.observed_on for o in obs if o.project_id == pid and o.observed_on),
                       default=None)
            if when is None or as_of is None:
                weights.append(1.0)
            else:
                age = max(0, (as_of - when).days)
                weights.append(0.5 ** (age / MATERIAL_HALF_LIFE_DAYS))
        mat_med = _weighted_median(mat_vals, weights)
        mat_q = _quartiles(mat_vals)
    else:
        mat_med, mat_q = None, (None, None)
    descs = tuple(sorted({o.description for o in obs if o.description})[:5])
    return ItemStats(
        item_key=first.item_key,
        family=first.family,
        item_class=first.item_class,
        uom=first.uom,
        n_obs=len(obs),
        n_projects=len({o.project_id for o in obs}),
        latest_seen=latest,
        labor_median=labor_med,
        labor_p25=labor_q[0],
        labor_p75=labor_q[1],
        labor_projects=len(labor_pp),
        material_median=mat_med,
        material_p25=mat_q[0],
        material_p75=mat_q[1],
        material_projects=len(mat_pp),
        descriptions=descs,
        outliers_labor=tuple(sorted(labor_out)),
        outliers_material=tuple(sorted(mat_out)),
    )


# Lookup result ------------------------------------------------------------------------


@dataclass
class Lookup:
    """What the library found for one item: labor and material ValueStats
    (unit values in the REQUESTED unit), the rung, the matched key and up to
    three candidates (aggregates only)."""

    method: str
    labor: ValueStat
    material: ValueStat
    matched_key: str | None = None
    matched_uom: str | None = None
    conversion: str | None = None
    flags: tuple[str, ...] = ()
    candidates: tuple[dict[str, Any], ...] = ()

    @property
    def found(self) -> bool:
        return self.method != "none"


def _dec(value: float | None, places: Decimal = _SIX) -> Decimal | None:
    if value is None:
        return None
    return Decimal(repr(value)).quantize(places)


def _quality(
    method: str, n_projects: int, median: float | None, p25: float | None, p75: float | None,
    thresholds: QualityThresholds,
) -> str:
    if median is None:
        return "missing"
    ratio = (p75 - p25) / median if (median and p25 is not None and p75 is not None) else 0.0
    if method == "exact":
        if n_projects >= thresholds.strong_min_projects and ratio <= thresholds.strong_max_iqr_ratio:
            return "strong"
        if n_projects >= thresholds.good_min_projects and ratio <= thresholds.good_max_iqr_ratio:
            return "good"
        return "weak"
    if method == "compatible":
        if (
            n_projects >= thresholds.compatible_good_min_projects
            and ratio <= thresholds.good_max_iqr_ratio
        ):
            return "good"
        return "weak"
    return "weak"


def _stat(
    method: str, median: float | None, p25: float | None, p75: float | None, n_obs: int,
    n_projects: int, latest: str | None, factor: float, thresholds: QualityThresholds,
) -> ValueStat:
    if median is None:
        return ValueStat.missing()
    return ValueStat(
        value=_dec(median * factor),
        p25=_dec(p25 * factor) if p25 is not None else None,
        p75=_dec(p75 * factor) if p75 is not None else None,
        n_obs=n_obs,
        n_projects=n_projects,
        latest_seen=latest,
        quality=_quality(method, n_projects, median, p25, p75, thresholds),
        method=method,
    )


def _pool(stats: Sequence[ItemStats], obs_by_key: Mapping[str, Sequence[Observation]],
          as_of: dt.date | None) -> ItemStats:
    """Recompute one ItemStats over the union of several keys' observations."""
    pooled: list[Observation] = []
    for s in stats:
        pooled.extend(obs_by_key[(s.item_key, s.uom)])
    merged = compute_stats(pooled, as_of=as_of)
    merged.item_key = stats[0].item_key
    return merged


class CostLibrary:
    """The in-memory library. Build with `from_observations`, `from_backup`
    or `load`; query with `lookup` and `fallback`."""

    def __init__(
        self,
        observations: Iterable[Observation],
        *,
        exclude_projects: Iterable[str] = (),
        thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
        as_of: dt.date | None = None,
    ) -> None:
        excluded = frozenset(str(p) for p in exclude_projects)
        self.excluded_projects = excluded
        self.thresholds = thresholds
        used = [o for o in observations if o.project_id not in excluded]
        self.observations: tuple[Observation, ...] = tuple(used)
        dates = [o.observed_on for o in used if o.observed_on]
        self.as_of = as_of or (max(dates) if dates else None)
        self._by_key: dict[tuple[str, str], list[Observation]] = {}
        for o in used:
            self._by_key.setdefault((o.item_key, o.uom), []).append(o)
        self._stats: dict[tuple[str, str], ItemStats] = {
            key: compute_stats(obs, as_of=self.as_of) for key, obs in self._by_key.items()
        }
        self._identity: dict[tuple[str, str], ItemIdentity] = {}
        self._by_family: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._by_class: dict[tuple[str, str], list[Observation]] = {}
        for (key, uom), obs in self._by_key.items():
            ident = parse_item(obs[0].description, uom)
            self._identity[(key, uom)] = ident
            self._by_family.setdefault((key.split("|", 1)[0], uom), []).append((key, uom))
            self._by_class.setdefault((obs[0].item_class, uom), []).extend(obs)
        self._fingerprint: str | None = None

    # Construction ---------------------------------------------------------------

    @classmethod
    def from_observations(
        cls, observations: Iterable[Observation], *, exclude_projects: Iterable[str] = (),
        thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
    ) -> CostLibrary:
        return cls(observations, exclude_projects=exclude_projects, thresholds=thresholds)

    @classmethod
    def from_rows(
        cls, rows: Iterable[Mapping[str, Any]], *, exclude_projects: Iterable[str] = (),
        thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
    ) -> CostLibrary:
        """From `cost_observations`-shaped rows (project_id, item_key, family,
        item_class, uom, unit_labor_hours, unit_material_cost, observed_on,
        raw_description, group_name)."""
        return cls(
            (observation_from_row(r) for r in rows),
            exclude_projects=exclude_projects,
            thresholds=thresholds,
        )

    @classmethod
    def from_backup(
        cls, backup_root: str | Path, *, exclude_projects: Iterable[str] = (),
        thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
    ) -> CostLibrary:
        """Wave 1: every chosen estimate workbook of the backup (one book per
        project), minus `exclude_projects`."""
        return cls(
            backup_observations(backup_root),
            exclude_projects=exclude_projects,
            thresholds=thresholds,
        )

    @classmethod
    def load(
        cls, sb: Any, *, exclude_projects: Iterable[str] = frozenset(),
        thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
    ) -> CostLibrary:
        """The 5.3 signature: observations paged out of `cost_observations`
        through PostgREST in DB_PAGE_SIZE rows, ordered by the primary key
        `id` before paging (PostgREST ranges without an ORDER BY are not
        stable between pages, which would repeat or skip rows and drift the
        fingerprint). Until migration ME exists the table is absent and this
        raises LibraryUnavailable; callers fall back to an empty library and
        report it. Sync: call from a worker thread, never inside `async def`."""
        if sb is None:
            raise LibraryUnavailable("no database client")
        columns = (
            "project_id, item_key, family, item_class, uom, unit_labor_hours, "
            "unit_material_cost, observed_on, raw_description, group_name, source_ref, excluded"
        )
        rows: list[Mapping[str, Any]] = []
        start = 0
        try:
            while True:
                page = (
                    sb.table("cost_observations")
                    .select(columns)
                    .order("id")
                    .range(start, start + DB_PAGE_SIZE - 1)
                    .execute()
                ).data or []
                rows.extend(r for r in page if not r.get("excluded"))
                if len(page) < DB_PAGE_SIZE:
                    break
                start += DB_PAGE_SIZE
        except Exception as exc:  # table missing, network, auth: all unavailable
            raise LibraryUnavailable(f"cost_observations unavailable: {exc}") from exc
        return cls.from_rows(rows, exclude_projects=exclude_projects, thresholds=thresholds)

    # Introspection --------------------------------------------------------------

    @property
    def n_observations(self) -> int:
        return len(self.observations)

    @property
    def n_projects(self) -> int:
        return len({o.project_id for o in self.observations})

    @property
    def n_items(self) -> int:
        return len(self._stats)

    def stats(self, item_key: str, uom: str) -> ItemStats | None:
        return self._stats.get((item_key, canon_uom(uom)))

    def keys(self) -> list[tuple[str, str]]:
        return sorted(self._stats)

    @property
    def fingerprint(self) -> str:
        """sha256 over the observations used (sorted), so two libraries built
        from the same data with the same exclusions share a fingerprint."""
        if self._fingerprint is None:
            h = hashlib.sha256()
            h.update(f"v{LIBRARY_VERSION}\n".encode())
            for o in sorted(
                self.observations,
                key=lambda o: (o.project_id, o.item_key, o.uom, o.source_ref, o.description),
            ):
                h.update(
                    f"{o.project_id}|{o.item_key}|{o.uom}|{o.unit_labor_hours}|"
                    f"{o.unit_material_cost}|{o.observed_on}\n".encode()
                )
            self._fingerprint = h.hexdigest()
        return self._fingerprint

    # Lookup ------------------------------------------------------------------------

    def _unit_match(self, want: str, have: str) -> tuple[float, str | None] | None:
        """(factor to apply to a per-`have` value to get a per-`want` value,
        conversion note) when the units are equal or both lengths; else None."""
        if want == have:
            return 1.0, None
        if want in _LENGTH_UOMS and have in _LENGTH_UOMS:
            # A value per `have` unit becomes per `want` unit: value x (want feet / have feet).
            factor = convert_unit_value(1.0, want, have)
            return factor, f"per {have} converted to per {want}"
        return None

    def _candidates_same_family(
        self, identity: ItemIdentity, uom: str
    ) -> list[tuple[ItemStats, float, str | None]]:
        """Every key of the item's family whose unit equals (or converts to)
        the requested one, with the per-unit factor and conversion note."""
        units = [uom]
        if uom in _LENGTH_UOMS:
            units.extend(u for u in _LENGTH_UOMS if u != uom)
        out: list[tuple[ItemStats, float, str | None]] = []
        family = library_family(identity)
        for have in units:
            match = self._unit_match(uom, have)
            if match is None:
                continue
            for key_uom in self._by_family.get((family, have), []):
                out.append((self._stats[key_uom], match[0], match[1]))
        return out

    def lookup(
        self, identity: ItemIdentity | str, uom: str | None = None, *,
        thresholds: QualityThresholds | None = None,
    ) -> Lookup:
        """Labor and material for an item in the requested unit, by the ladder.
        `identity` may be a description (then `uom` is its unit). A blank or
        LS unit, or a count against a length, returns `none`."""
        thr = thresholds or self.thresholds
        if isinstance(identity, str):
            identity = parse_item(identity, uom or "")
        try:
            want = canon_uom(uom) if uom is not None else identity.uom
        except ValueError:
            want = ""
        if not want or want == "LS":
            return Lookup(method="none", labor=ValueStat.missing(), material=ValueStat.missing(),
                          flags=("uom_unpriceable",))
        key = library_key(identity)
        # 1. exact (same key; FT, C and M convert).
        for have in (want, *[u for u in _LENGTH_UOMS if u != want and want in _LENGTH_UOMS]):
            st = self._stats.get((key, have))
            if st is None:
                continue
            match = self._unit_match(want, have)
            if match is None:
                continue
            return self._result("exact", st, match[0], match[1], thr, identity, want)
        # 2. compatible (same family, modifiers equal, every attribute present on
        # both sides equal, no hard conflict): one side lacks attributes the
        # other has, for example 'DUPLEX RECEPTACLE' against '... 20A'.
        q_attrs, q_mods = key_attrs(key)
        critical = CRITICAL_ATTRS.get(identity.item_class, ())
        compatible: list[tuple[ItemStats, float, str | None]] = []
        for st, factor, note in self._candidates_same_family(identity, want):
            other = self._identity[(st.item_key, st.uom)]
            o_attrs, o_mods = key_attrs(st.item_key)
            if o_mods != q_mods or hard_conflict(identity, other):
                continue
            if any(q_attrs[k] != o_attrs[k] for k in q_attrs if k in o_attrs):
                continue
            if any((k in q_attrs) != (k in o_attrs) for k in critical):
                continue
            compatible.append((st, factor, note))
        if compatible:
            out = self._pooled("compatible", compatible, thr, identity, want)
            if out.labor.n_projects >= MIN_PROJECTS_BELOW_EXACT:
                return out
        # 3. context (same family only, no hard conflict, critical attrs present
        # on both sides and equal).
        context = []
        for st, factor, note in self._candidates_same_family(identity, want):
            other = self._identity[(st.item_key, st.uom)]
            if hard_conflict(identity, other):
                continue
            o_attrs, _o_mods = key_attrs(st.item_key)
            if any((k in q_attrs) != (k in o_attrs) for k in critical):
                continue
            if any(k in q_attrs and k in o_attrs and q_attrs[k] != o_attrs[k] for k in critical):
                continue
            context.append((st, factor, note))
        if context:
            out = self._pooled("context", context, thr, identity, want)
            if out.labor.n_projects >= MIN_PROJECTS_BELOW_EXACT:
                out.flags = tuple(sorted(set(out.flags) | {"context_match"}))
                return out
        return Lookup(method="none", labor=ValueStat.missing(), material=ValueStat.missing())

    def fallback(self, identity: ItemIdentity, uom: str) -> Lookup:
        """Class-level median for an item nothing matched: quality weak."""
        try:
            want = canon_uom(uom)
        except ValueError:
            want = ""
        if not want or want not in FALLBACK_UOMS or identity.item_class not in FALLBACK_CLASSES:
            return Lookup(method="none", labor=ValueStat.missing(), material=ValueStat.missing())
        obs = list(self._by_class.get((identity.item_class, want), []))
        factor = 1.0
        note = None
        if not obs and want in _LENGTH_UOMS:
            for have in _LENGTH_UOMS:
                if have == want:
                    continue
                obs = list(self._by_class.get((identity.item_class, have), []))
                if obs:
                    factor, note = self._unit_match(want, have) or (1.0, None)
                    break
        if not obs or len({o.project_id for o in obs}) < MIN_PROJECTS_BELOW_EXACT:
            return Lookup(method="none", labor=ValueStat.missing(), material=ValueStat.missing())
        st = compute_stats(obs, as_of=self.as_of)
        if identity.item_class == "demolition":
            st.material_median, st.material_p25, st.material_p75 = 0.0, 0.0, 0.0
        labor = _stat("fallback", st.labor_median, st.labor_p25, st.labor_p75, st.n_obs,
                      st.labor_projects, st.latest_month, factor, self.thresholds)
        material = _stat("fallback", st.material_median, st.material_p25, st.material_p75,
                         st.n_obs, st.material_projects, st.latest_month, factor, self.thresholds)
        return Lookup(method="fallback", labor=labor, material=material,
                      matched_key=f"class:{identity.item_class}", conversion=note,
                      flags=("fallback_price",))

    def _result(
        self, method: str, st: ItemStats, factor: float, note: str | None,
        thr: QualityThresholds, identity: ItemIdentity, want: str,
        extra_flags: tuple[str, ...] = (),
    ) -> Lookup:
        labor = _stat(method, st.labor_median, st.labor_p25, st.labor_p75, st.n_obs,
                      st.labor_projects, st.latest_month, factor, thr)
        if st.item_class == "demolition" or identity.item_class == "demolition":
            material = ValueStat(value=Decimal(0), p25=Decimal(0), p75=Decimal(0),
                                 n_obs=st.n_obs, n_projects=st.labor_projects,
                                 latest_seen=st.latest_month, quality=labor.quality,
                                 method=method)
        else:
            material = _stat(method, st.material_median, st.material_p25, st.material_p75,
                             st.n_obs, st.material_projects, st.latest_month, factor, thr)
        flags = list(extra_flags)
        if note:
            flags.append("unit_converted")
        return Lookup(
            method=method, labor=labor, material=material, matched_key=st.item_key,
            matched_uom=st.uom, conversion=note, flags=tuple(flags),
            candidates=self._top_candidates(identity, want, exclude=st.item_key),
        )

    def _pooled(
        self, method: str, found: list[tuple[ItemStats, float, str | None]],
        thr: QualityThresholds, identity: ItemIdentity, want: str,
    ) -> Lookup:
        """Pool compatible keys whose labor medians agree within
        COMPATIBLE_POOL_TOLERANCE; else the key with the most projects, flagged."""
        found.sort(key=lambda t: (-t[0].n_projects, t[0].item_key))
        meds = [s.labor_median * f for s, f, _n in found if s.labor_median is not None]
        flags: tuple[str, ...] = ()
        if len(found) > 1 and meds and max(meds) > 0 and (max(meds) - min(meds)) / max(meds) > COMPATIBLE_POOL_TOLERANCE:
            chosen = [found[0]]
            flags = ("ambiguous_attrs",)
        else:
            chosen = found
        factors = {f for _s, f, _n in chosen}
        if len(factors) == 1:
            factor, note = chosen[0][1], chosen[0][2]
            st = _pool([s for s, _f, _n in chosen], self._by_key, self.as_of) if len(chosen) > 1 else chosen[0][0]
        else:
            # Mixed units inside the pool: keep the unit of the best key only.
            best = chosen[0]
            factor, note, st = best[1], best[2], best[0]
        return self._result(method, st, factor, note, thr, identity, want, extra_flags=flags)

    def _top_candidates(self, identity: ItemIdentity, want: str, *, exclude: str | None,
                        limit: int = 3) -> tuple[dict[str, Any], ...]:
        probe = BoqRow(area="", section="", group="", subgroup="", description=identity.norm,
                       quantity=1.0, unit=want)
        scored: list[tuple[float, ItemStats]] = []
        for st, _factor, _note in self._candidates_same_family(identity, want):
            if st.item_key == exclude:
                continue
            desc = st.descriptions[0] if st.descriptions else st.item_key
            row = BoqRow(area="", section="", group="", subgroup="", description=desc,
                         quantity=1.0, unit=st.uom)
            scored.append((similarity(probe, row), st))
        scored.sort(key=lambda t: (-t[0], -t[1].n_projects, t[1].item_key))
        return tuple(
            {"item_key": st.item_key, "family": st.family, "uom": st.uom,
             "n_projects": st.n_projects, "score": round(score, 3)}
            for score, st in scored[:limit]
        )


def observation_from_row(row: Mapping[str, Any]) -> Observation:
    when = row.get("observed_on")
    if isinstance(when, str):
        when = dt.date.fromisoformat(when[:10]) if when else None
    elif isinstance(when, dt.datetime):
        when = when.date()
    return Observation(
        project_id=str(row.get("project_id") or ""),
        item_key=str(row["item_key"]),
        family=str(row.get("family") or row["item_key"].split("|", 1)[0]),
        item_class=str(row.get("item_class") or row["item_key"].split(":", 1)[0].split("|", 1)[0]),
        uom=canon_uom(row.get("uom") or "EA"),
        unit_labor_hours=_to_float(row.get("unit_labor_hours")),
        unit_material_cost=_to_float(row.get("unit_material_cost")),
        observed_on=when,
        description=str(row.get("raw_description") or ""),
        group=str(row.get("group_name") or ""),
        source_ref=str(row.get("source_ref") or ""),
    )


# Backup cache ------------------------------------------------------------------------

_BACKUP_CACHE: dict[str, tuple[estimate_parse.DryRunReport, dict[str, estimate_parse.ParsedEstimate]]] = {}


def backup_books(backup_root: str | Path) -> tuple[estimate_parse.DryRunReport, dict[str, estimate_parse.ParsedEstimate]]:
    """Parse the backup's estimate workbooks once per process (one book per
    project by the one-book rule)."""
    key = str(Path(backup_root).resolve())
    if key not in _BACKUP_CACHE:
        _BACKUP_CACHE[key] = estimate_parse.parse_backup(backup_root)
    return _BACKUP_CACHE[key]


def backup_observations(backup_root: str | Path) -> list[Observation]:
    report, chosen = backup_books(backup_root)
    created = {e.file.project_id: e.file.created_at for e in report.entries if e.chosen}
    out: list[Observation] = []
    for pid, parsed in chosen.items():
        fallback = None
        raw = created.get(pid)
        if raw:
            try:
                fallback = dt.date.fromisoformat(raw[:10])
            except ValueError:
                fallback = None
        obs, _skipped = observations_from_book(pid, parsed, fallback_date=fallback)
        out.extend(obs)
    return out


# Backfill plan (migration ME tables) ---------------------------------------------------------


@dataclass
class BackfillPlan:
    """Rows for the three ME tables built from the backup workbooks: one
    `cost_sources` row per chosen workbook (keyed by project and file id),
    one `cost_items` row per (library key, uom) with its trigram search
    text, and one `cost_observations` row per workbook line. Pure data:
    the script writes it, this module never touches the database."""

    sources: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    excluded_projects: tuple[str, ...] = ()
    skipped_projects: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "sources": len(self.sources),
            "items": len(self.items),
            "observations": len(self.observations),
            "projects": len({o["project_id"] for o in self.observations}),
            "excluded_projects": len(self.excluded_projects),
            "skipped_projects": len(self.skipped_projects),
        }


def search_text_for(descriptions: Iterable[str]) -> str:
    """The pg_trgm search text of an item: its distinct normalized
    descriptions joined by ' | ' (upper case, single spaces)."""
    seen: list[str] = []
    for d in descriptions:
        norm = re.sub(r"\s+", " ", str(d or "")).strip().upper()
        if norm and norm not in seen:
            seen.append(norm)
    return " | ".join(seen)[:2000]


def backfill_plan(
    backup_root: str | Path, *, exclude_projects: Iterable[str] = (),
) -> BackfillPlan:
    """The ME rows for every chosen estimate workbook of the backup minus
    `exclude_projects` (the holdout through
    eval_manifest.excluded_projects("pricing_library")). Source and
    observation rows carry client-side ids so the observations can
    reference their source without a round trip."""
    import uuid

    excluded = frozenset(str(p) for p in exclude_projects)
    report, chosen = backup_books(backup_root)
    entries = {e.file.project_id: e for e in report.entries if e.chosen}
    plan = BackfillPlan(excluded_projects=tuple(sorted(excluded)))
    skipped: list[str] = []
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for pid in sorted(chosen):
        if pid in excluded:
            skipped.append(pid)
            continue
        parsed = chosen[pid]
        entry = entries.get(pid)
        fallback = None
        if entry is not None and entry.file.created_at:
            try:
                fallback = dt.date.fromisoformat(entry.file.created_at[:10])
            except ValueError:
                fallback = None
        obs, skipped_rows = observations_from_book(pid, parsed, fallback_date=fallback)
        source_id = str(uuid.uuid4())
        plan.sources.append({
            "id": source_id,
            "project_id": pid,
            "file_id": entry.file.file_id if entry is not None else None,
            "filename": entry.file.filename if entry is not None else None,
            "workbook_date": (parsed.workbook_date or fallback).isoformat() if (parsed.workbook_date or fallback) else None,
            "labor_rate": float(parsed.labor_rate) if parsed.labor_rate is not None else None,
            "parser_version": parsed.parser_version,
            "sha256": hashlib.sha256(entry.file.path.read_bytes()).hexdigest() if entry is not None and entry.file.path.is_file() else None,
            "line_count": len(obs),
            "skipped": {"rows": len(skipped_rows), "reasons": _count_reasons(skipped_rows)},
        })
        for o in obs:
            row = o.to_row()
            row["source_id"] = source_id
            plan.observations.append(row)
            item = items.setdefault((o.item_key, o.uom), {
                "item_key": o.item_key, "uom": o.uom, "family": o.family,
                "item_class": o.item_class, "descriptions": [],
            })
            if o.description and o.description not in item["descriptions"] and len(item["descriptions"]) < 8:
                item["descriptions"].append(o.description)
    for key in sorted(items):
        item = items[key]
        item["search_text"] = search_text_for(item["descriptions"])
        plan.items.append(item)
    plan.skipped_projects = tuple(skipped)
    return plan


def _count_reasons(skipped_rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in skipped_rows:
        reason = str(r.get("reason") or "other")
        out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items()))


__all__ = [
    "CostLibrary", "Observation", "ItemStats", "Lookup", "QualityThresholds",
    "DEFAULT_THRESHOLDS", "LibraryUnavailable", "library_key", "library_family", "key_attrs", "identity_of",
    "observations_from_book", "observation_from_row", "compute_stats",
    "backup_books", "backup_observations", "MATERIAL_HALF_LIFE_DAYS",
    "BackfillPlan", "backfill_plan", "search_text_for",
]
