"""Code-built evidence for an AI Estimating run: marks, polylines and chips
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS8, section 5.4 outputs scope
`evidence`; fixes E17).

Evidence answers "where on the plans does each BOQ number come from" with no
model call. Every input is read through an adapter: page facts (words,
regions, views, scales) through `plans_reader_evidence_inputs`, the run's own
data (BOQ, main output, category rows, MeasureOutput, documents, legacy model
marks) through a `RunInputs` provider defined here (Supabase or a fake), so a
sibling's shape change touches one function.

Placement per EA line (`place_sheet`, one sheet at a time):

1. Pools. A line draws from the text-layer anchors of its own tag (the
   base tag, e.g. L1 for "L1, 2X4 TROFFER" and "L1, ..., LIFE SAFETY") or of
   its family (`demo_x`: demolition lines and the "(X)" tags).
2. Registration. Model points (category row positions, or the legacy main
   output's marked plans) are fitted per sheet to same-pool anchors with an
   axis-aligned trimmed least-squares ICP (tag-aware pairs only; never "any
   nearest anchor"). Accepted only with enough pairs, both scales in range
   and a small median residual in feet. A sheet with no tag-aware pairs of
   its own (untagged receptacles and controls) borrows the accepted
   registration of another sheet of the same document and page size
   (`document_prior`, the donor's sheet key in `prior_from`): the model saw
   every page of a document through the same frame.
3. Allocation inside a pool: greedy by distance between the line's
   (registered) model points and the anchors (Hungarian deferred), then the
   remaining anchors in reading order. Method `anchor` only when the anchor
   text folds to the line's own tag and that tag is not shared with another
   line (or the line is the shared tag's primary, the one without variant
   words such as EM or LIFE SAFETY); everything else allocated from a pool is
   `assigned` (family or reading-order allocation; drawn in its own tone and
   excluded from the 90% anchor metric).
4. Remaining quantity uses the line's leftover model points: `snapped` to an
   unclaimed hint anchor (a device label such as D, OC, GFI) within the snap
   radius in feet at the view scale; a point within 2 ft of an anchor the
   same line already holds reads that device twice and stacks on it (count,
   the extra not located); else `registered` through the sheet
   registration; else `approx` (the raw model point).
5. Stacked marks: only model-point marks (registered, approx) of one line
   closer than the stack threshold (feet at the view scale) merge into one
   mark with a count; the merged ones are not located.
6. Unclaimed pool anchors become "tag not counted" marks (no line).

Polylines per FT line come from the WS3 MeasureOutput through the line's M
rows and their derivation terms (the term shape is documented on
`MeasureView`): each route term names one segment by route id and segment
kind; segments with geometry become polylines (dashed for assumed routes);
drops, allowances, tie-ins and other vertical or undrawn terms are listed as
not drawn; wire lines compare drawn length times the term's conductor count
against the line quantity; linear-item instances (rectangles or heavy runs)
are converted to centerlines. The replay stage reads the derive stage's
`measure` artifact.

Chips: every BOQ line gets a quantity chip (how the number was made). Price
chips (quote, library median n=, policy, missing) are computed at read time
by `attach_price_chips` for pricing roles and are never stored: the stored
evidence carries no money and no price provenance.

Storage: one `plans_reader_outputs` row with scope `evidence` per
(main_version, boq_deliverable_version), upserted through an injected
`EvidenceWriter`; a build whose `inputs_hash` equals the stored row's is
skipped and the stored evidence returned. A heartbeat callable runs between
sheets; each sheet's serialized evidence is capped in bytes.

`evidence_to_marked_spec(evidence)` turns evidence into the renderer-only
marked-plans spec (never validated against or sent to a model).
`register_replay_stage()` registers this module as the replay stage
"evidence" (plans_reader_replay.register_stage).

Pure apart from the adapters; no model, no prices, no network of its own.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.services import plans_reader_anchors as anchors_mod
from app.services import plans_reader_evidence_inputs as inputs_mod
from app.services.electrical_items import BoqRow, is_linear, linear_evidence_required, parse_item, similarity
from app.services.plans_reader_evidence_inputs import EvidenceInputs, FactsUnavailable, SheetInputs
from app.services.plans_reader_lines import line_conductor
from app.services.plans_reader_rows import (
    doc_order,
    iter_boq_lines,
    iter_measure_rows,
    normalize_sheet_number,
    sheet_key as make_sheet_key,
)
from app.services.plans_reader_schemas import CATEGORIES
from app.services.plans_reader_tools import doc8

logger = logging.getLogger(__name__)

EVIDENCE_SCHEMA = "bdr.evidence/1"
EVIDENCE_CODE_VERSION = "ws8-evidence-2026-10-22-1"
OUTPUT_SCOPE = "evidence"
PACIFIC = ZoneInfo("America/Los_Angeles")

METHOD_ANCHOR = "anchor"
METHOD_ASSIGNED = "assigned"
METHOD_SNAPPED = "snapped"
METHOD_REGISTERED = "registered"
METHOD_APPROX = "approx"
METHOD_UNCLAIMED = "unclaimed"
PLACEMENT_METHODS: tuple[str, ...] = (
    METHOD_ANCHOR,
    METHOD_ASSIGNED,
    METHOD_SNAPPED,
    METHOD_REGISTERED,
    METHOD_APPROX,
)
# Marks placed from model points: the only ones the stacked-mark merge touches.
STACKABLE_METHODS: frozenset[str] = frozenset({METHOD_REGISTERED, METHOD_APPROX})
# Excluded from the 90% anchor metric (allocation, not identification).
METRIC_EXCLUDED_METHODS: frozenset[str] = frozenset({METHOD_ASSIGNED})
MARK_STYLES: dict[str, str] = {
    METHOD_ANCHOR: "solid",
    METHOD_SNAPPED: "solid",
    METHOD_REGISTERED: "solid",
    METHOD_ASSIGNED: "assigned",
    METHOD_APPROX: "ring",
    METHOD_UNCLAIMED: "unclaimed",
}

FAMILY_DEMO_X = anchors_mod.FAMILY_DEMO_X
PLAN_ROLES: frozenset[str] = frozenset({"plan", "site"})

# Distances. Feet thresholds convert to paper inches with the view scale at
# the point; the inch fallbacks apply on a sheet without a known scale.
SNAP_RADIUS_FT = 3.0
SNAP_RADIUS_IN = 0.75
STACK_MERGE_FT = 1.0
STACK_MERGE_IN = 0.25
# A leftover model point of a line this close to a text anchor the same line
# already holds (anchor or snapped) reads the same device twice: it stacks on
# that mark instead of becoming a second, offset mark.
DUPLICATE_SNAP_FT = 2.0
DUPLICATE_SNAP_IN = 0.5
# Registration (spec WS8 design 6.4).
REG_MIN_PAIRS = 6
# A sheet with no tag-aware pairs of its own borrows the accepted
# registration of another sheet of the same document and page size (the
# model saw every page of a document through the same frame).
PRIOR_REASONS: frozenset[str] = frozenset({"no_pairs", "too_few_pairs"})
REASON_DOCUMENT_PRIOR = "document_prior"
REG_SCALE_RANGE: tuple[float, float] = (0.75, 1.33)
REG_MAX_RESIDUAL_FT = 2.0
REG_MAX_RESIDUAL_IN = 0.5
REG_TRIM = 0.8
REG_ITERS = 20
# Size guards: per sheet serialized bytes, per run marks and polylines.
MAX_SHEET_BYTES = 200_000
MAX_MARKS = 5_000
MAX_POLYLINES = 2_000
# Polyline length check: within max(1 ft, 1%) of the line quantity.
LENGTH_TOL_FT = 1.0
LENGTH_TOL_REL = 0.01

# Legend prefix codes (the estimator's marked-plans grammar: "LF, L1").
GROUP_CODES: dict[str, str] = {
    "LIGHTING FIXTURES": "LF",
    "LIGHTING CONTROLS": "LC",
    "DEMOLITION": "DEMO",
    "WIRING": "PW",
    "SWITCHGEAR": "SG",
    "OTHER ITEMS": "MISC",
    "LOW VOLTAGE": "LV",
    "UNDERGROUND": "UG",
}

# Variant words: a line carrying one shares its tag as a variant (L1 vs L1-EM).
_VARIANT_RE = re.compile(
    r"(?:-EM\b|\bEM\b|\bEMERGENCY\b|\bLIFE[\s-]SAFETY\b|\bSHADED\b|\bNL\b|\bNIGHT[\s-]?LIGHT\b|\bBATTERY\b)"
)
_LEADING_TAG_RE = re.compile(r"^\s*(?:TYPE\s+)?([A-Z]{1,3}\d{1,2}(?:-[A-Z]{1,3})?)(?=[\s,:;(]|$)")
_DEMO_LINE_RE = re.compile(r"\b(?:REMOVE|REMOVAL|DEMO|DEMOLISH|DEMOLITION)\b|\(\s*X\s*\)")
_ASSUMED_RE = re.compile(r"\(ASSUMED\)", re.IGNORECASE)
# Device label hints: text-layer labels a device line may snap to.
HINT_TAGS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\bDIMMER\b"), ("D", "DM", "DO")),
    (re.compile(r"\bOCCUPANCY\b.*\bSENSOR\b(?!.*\b(?:SWITCH|DIMMER)\b)"), ("OC", "OS", "OCC")),
    (re.compile(r"\bOCCUPANCY\b.*\bSWITCH\b"), ("OS", "SO")),
    (re.compile(r"\bVACANCY\b"), ("VS", "VC")),
    (re.compile(r"\bGFCI\b|\bGFI\b"), ("GFI", "GFCI", "GF")),
    (re.compile(r"\bWEATHER\s*PROOF\b|\bWP\b"), ("WP",)),
    (re.compile(r"\bTHERMOSTAT\b"), ("T", "TS")),
    (re.compile(r"\bEXIT\b"), ("EX",)),
)
# Measure derivation term kinds that are never drawn on a plan.
NOT_DRAWN_TERM_KINDS: frozenset[str] = frozenset(
    {
        "vertical",
        "drop",
        "rise",
        "panel_rise",
        "floor_to_floor",
        "allowance",
        "homerun_default",
        "home_run_default",
        "tie_in",
        "tie_in_allowance",
        "density",
        "factor",
    }
)
MEASURED_BASES: frozenset[str] = frozenset({"measured", "measured_route", "drawn", "stated"})

# Chip vocabulary (spec 1.4 and WS8 design 6.7). The FE renders
# t("aiEstimating.chips.<kind>", params); xlsx and PDF use CHIP_TEXT_EN.
CHIP_TONES: tuple[str, ...] = ("ok", "warn", "danger", "neutral")
TONE_RANK: dict[str, int] = {"danger": 0, "warn": 1, "neutral": 2, "ok": 3}
CHIP_TEXT_EN: dict[str, str] = {
    "unit_error": "Linear item in EA",
    "linear_luminaire_ea": "Linear fixture in EA, {q} EA",
    "unsourced": "No source, {q} {unit}",
    "partial": "Partial count, recount needed",
    "census_floor": "Census floor {q}",
    "counted": "Counted {q}, marked {m}",
    "counted_not_located": "Counted {q}, marked {m}, {k} not located",
    "measured": "Measured {ft} FT",
    "assumed_route": "Assumed route {ft} FT, {c} circuits",
    "schedule": "Schedule {sheet}",
    "stated": "Stated on {sheet}",
    "policy": "Policy: {rule}",
    "manual": "Manual entry",
    "mixed": "{first} + {second}",
    "quote": "Quote {quote}",
    "library": "Library median n={n}",
    "library_weak": "Library weak n={n}",
    "price_policy": "Policy rate",
    "missing": "No price",
    "uom_mismatch": "Unit mismatch",
}
QTY_CHIP_KINDS: tuple[str, ...] = (
    "unit_error",
    "linear_luminaire_ea",
    "unsourced",
    "partial",
    "census_floor",
    "counted",
    "measured",
    "assumed_route",
    "schedule",
    "stated",
    "policy",
    "manual",
    "mixed",
)
PRICE_CHIP_KINDS: tuple[str, ...] = (
    "quote",
    "library",
    "library_weak",
    "price_policy",
    "missing",
    "uom_mismatch",
)

# Keys that must never appear in stored evidence (money or price provenance).
MONEY_KEY_RE = re.compile(r"(?i)price|cost|usd|dollar|amount|bid|unit_hours|unit_material|labor|material_source")


# Small helpers ----------------------------------------------------------------------


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(value: Any) -> int | None:
    f = _num(value)
    return int(f) if f is not None else None


def _r4(v: float) -> float:
    return round(float(v), 4)


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def now_pacific_iso() -> str:
    return datetime.now(PACIFIC).isoformat(timespec="seconds")


def _run_id(run: Any) -> str | None:
    if run is None:
        return None
    if isinstance(run, str):
        return run
    if isinstance(run, Mapping):
        v = run.get("id")
        return str(v) if v is not None else None
    v = getattr(run, "id", None)
    return str(v) if v is not None else None


def _point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        x, y = _num(value[0]), _num(value[1])
        if x is not None and y is not None and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            return (x, y)
    return None


def _points(value: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in value or [] if isinstance(value, (list, tuple)) else []:
        q = _point(p)
        if q is not None:
            out.append(q)
    return out


def dist_in(a: tuple[float, float], b: tuple[float, float], w_in: float, h_in: float) -> float:
    """Paper inches between two displayed-frame points."""
    return math.hypot((a[0] - b[0]) * w_in, (a[1] - b[1]) * h_in)


# Chips ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Chip:
    """`{axis qty|price, kind, tone ok|warn|danger|neutral, params}`; `text`
    is the English template filled with params (xlsx and PDF)."""

    axis: str
    kind: str
    tone: str
    params: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        key = self.kind
        if key == "counted" and (self.params.get("k") or 0) > 0:
            key = "counted_not_located"
        template = CHIP_TEXT_EN.get(key, key)
        try:
            return template.format(**{k: _fmt(v) for k, v in self.params.items()})
        except (KeyError, IndexError, ValueError):
            return template

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "kind": self.kind,
            "tone": self.tone,
            "params": dict(self.params),
            "text": self.text(),
        }


def _fmt(v: Any) -> Any:
    if isinstance(v, float):
        if abs(v - round(v)) < 1e-9:
            return f"{int(round(v)):,}"
        return f"{v:,.1f}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return v if v is not None else ""


def _min_tone(*tones: str) -> str:
    return min(tones, key=lambda t: TONE_RANK.get(t, 2))


# Run inputs (the run's own data) ----------------------------------------------------


class RunInputs(Protocol):
    """The run data evidence reads (besides page facts). Every method returns
    None or an empty list when the data is missing; none raises for a missing
    table or column."""

    def documents(self) -> list[dict[str, Any]]: ...

    def main_output(self, version: int | None) -> tuple[int | None, dict[str, Any] | None]: ...

    def boq_spec(
        self, version: int | None, main: Mapping[str, Any] | None
    ) -> tuple[int | None, dict[str, Any] | None]: ...

    def category_outputs(self) -> list[dict[str, Any]]: ...

    def measure_output(self) -> dict[str, Any] | None: ...

    def model_marks(self, main: Mapping[str, Any] | None) -> dict[str, Any] | None: ...


class FakeRunInputs:
    """Canned run data for tests and replay."""

    def __init__(
        self,
        *,
        documents: Sequence[Mapping[str, Any]] = (),
        main: Mapping[str, Any] | None = None,
        main_version: int | None = 1,
        boq: Mapping[str, Any] | None = None,
        boq_version: int | None = 1,
        categories: Sequence[Mapping[str, Any]] = (),
        measure: Mapping[str, Any] | None = None,
        marked: Mapping[str, Any] | None = None,
    ) -> None:
        self._documents = [dict(d) for d in documents]
        self._main = dict(main) if main is not None else None
        self._main_version = main_version
        self._boq = dict(boq) if boq is not None else None
        self._boq_version = boq_version
        self._categories = [dict(c) for c in categories]
        self._measure = dict(measure) if measure is not None else None
        self._marked = dict(marked) if marked is not None else None

    def documents(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._documents]

    def main_output(self, version: int | None) -> tuple[int | None, dict[str, Any] | None]:
        return (version if version is not None else self._main_version), copy.deepcopy(self._main)

    def boq_spec(
        self, version: int | None, main: Mapping[str, Any] | None
    ) -> tuple[int | None, dict[str, Any] | None]:
        if self._boq is not None:
            return (version if version is not None else self._boq_version), copy.deepcopy(self._boq)
        boq = (main or {}).get("boq") if isinstance(main, Mapping) else None
        return (version if version is not None else self._boq_version), copy.deepcopy(boq) if boq else None

    def category_outputs(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._categories)

    def measure_output(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._measure)

    def model_marks(self, main: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if self._marked is not None:
            return copy.deepcopy(self._marked)
        mp = (main or {}).get("marked_plans") if isinstance(main, Mapping) else None
        return copy.deepcopy(mp) if isinstance(mp, Mapping) else None


class SupabaseRunInputs:
    """The run data from the database (sync client; call from plain def or a
    worker thread, never inside async def). A missing table, column or row
    gives None (logged), never an exception."""

    def __init__(self, sb: Any, run_id: str) -> None:
        self._sb = sb
        self._run_id = run_id

    def _rows(self, table: str, columns: str, **eq: Any) -> list[dict[str, Any]]:
        try:
            q = self._sb.table(table).select(columns).eq("run_id", self._run_id)
            for k, v in eq.items():
                q = q.eq(k, v)
            return list(q.execute().data or [])
        except Exception as exc:  # noqa: BLE001 - a missing table degrades, never fails the build
            logger.warning("evidence: read %s for run %s failed: %s", table, self._run_id, exc)
            return []

    def documents(self) -> list[dict[str, Any]]:
        return self._rows(
            "plans_reader_documents",
            "id, category, page_meta, page_count, sheet_width_in, sheet_height_in, created_at",
        )

    def _latest(self, rows: list[dict[str, Any]], version: int | None) -> dict[str, Any] | None:
        if version is not None:
            rows = [r for r in rows if _int(r.get("version")) == int(version)]
        if not rows:
            return None
        return max(rows, key=lambda r: _int(r.get("version")) or 0)

    def main_output(self, version: int | None) -> tuple[int | None, dict[str, Any] | None]:
        row = self._latest(self._rows("plans_reader_outputs", "version, output", scope="main"), version)
        if row is None:
            return version, None
        return _int(row.get("version")), row.get("output") if isinstance(row.get("output"), dict) else None

    def boq_spec(
        self, version: int | None, main: Mapping[str, Any] | None
    ) -> tuple[int | None, dict[str, Any] | None]:
        row = self._latest(self._rows("plans_reader_deliverables", "version, spec_json", kind="boq"), version)
        if row is not None and isinstance(row.get("spec_json"), dict):
            return _int(row.get("version")), row["spec_json"]
        boq = (main or {}).get("boq") if isinstance(main, Mapping) else None
        return version, boq if isinstance(boq, dict) else None

    def category_outputs(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for r in self._rows("plans_reader_outputs", "category, version, output", scope="category"):
            cat = str(r.get("category") or "")
            if cat not in latest or (_int(r.get("version")) or 0) > (_int(latest[cat].get("version")) or 0):
                latest[cat] = r
        out = []
        for cat in sorted(latest):
            o = latest[cat].get("output")
            if isinstance(o, dict):
                item = dict(o)
                item.setdefault("category", cat)
                out.append(item)
        return out

    def measure_output(self) -> dict[str, Any] | None:
        row = self._latest(self._rows("plans_reader_outputs", "version, output", scope="measure"), None)
        o = row.get("output") if row else None
        return o if isinstance(o, dict) else None

    def model_marks(self, main: Mapping[str, Any] | None) -> dict[str, Any] | None:
        mp = (main or {}).get("marked_plans") if isinstance(main, Mapping) else None
        if isinstance(mp, dict):
            return mp
        row = self._latest(self._rows("plans_reader_deliverables", "version, spec_json", kind="marked_plans"), None)
        spec = row.get("spec_json") if row else None
        return spec if isinstance(spec, dict) else None


# Evidence writer (outputs scope 'evidence') ------------------------------------------


class EvidenceWriter(Protocol):
    """Upserts the evidence output keyed by (main_version, boq_version)."""

    def find(self, sb: Any, run_id: str, main_version: int | None, boq_version: int | None) -> dict[str, Any] | None: ...

    def write(self, sb: Any, run_id: str, output: dict[str, Any]) -> dict[str, Any]: ...


def _same_key(output: Any, main_version: int | None, boq_version: int | None) -> bool:
    return (
        isinstance(output, Mapping)
        and _int(output.get("main_version")) == main_version
        and _int(output.get("boq_version")) == boq_version
    )


class MemoryEvidenceWriter:
    """In-memory writer (tests and replay). `rows` mirrors the table."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.writes = 0

    def find(self, sb: Any, run_id: str, main_version: int | None, boq_version: int | None) -> dict[str, Any] | None:
        for r in self.rows:
            if r["run_id"] == run_id and _same_key(r["output"], main_version, boq_version):
                return copy.deepcopy(r)
        return None

    def write(self, sb: Any, run_id: str, output: dict[str, Any]) -> dict[str, Any]:
        self.writes += 1
        main_v, boq_v = _int(output.get("main_version")), _int(output.get("boq_version"))
        for r in self.rows:
            if r["run_id"] == run_id and _same_key(r["output"], main_v, boq_v):
                r["output"] = copy.deepcopy(output)
                return copy.deepcopy(r)
        version = 1 + max((r["version"] for r in self.rows if r["run_id"] == run_id), default=0)
        row = {"run_id": run_id, "scope": OUTPUT_SCOPE, "version": version, "output": copy.deepcopy(output)}
        self.rows.append(row)
        return copy.deepcopy(row)


class SupabaseEvidenceWriter:
    """`plans_reader_outputs` rows with scope 'evidence' (the scope CHECK is
    widened by migration 0165). One row per (main_version, boq_version):
    updated in place when it exists, else inserted as the next version.
    `category` and `source_group_ids` are omitted (column defaults)."""

    def _existing(self, sb: Any, run_id: str) -> list[dict[str, Any]]:
        res = (
            sb.table("plans_reader_outputs")
            .select("id, version, output")
            .eq("run_id", run_id)
            .eq("scope", OUTPUT_SCOPE)
            .execute()
        )
        return list(res.data or [])

    def find(self, sb: Any, run_id: str, main_version: int | None, boq_version: int | None) -> dict[str, Any] | None:
        try:
            rows = self._existing(sb, run_id)
        except Exception as exc:  # noqa: BLE001 - before migration 0165 nothing is stored
            logger.warning("evidence: read of stored evidence for run %s failed: %s", run_id, exc)
            return None
        for r in rows:
            if _same_key(r.get("output"), main_version, boq_version):
                return r
        return None

    def write(self, sb: Any, run_id: str, output: dict[str, Any]) -> dict[str, Any]:
        rows = self._existing(sb, run_id)
        main_v, boq_v = _int(output.get("main_version")), _int(output.get("boq_version"))
        for r in rows:
            if _same_key(r.get("output"), main_v, boq_v):
                data = (
                    sb.table("plans_reader_outputs").update({"output": output}).eq("id", r["id"]).execute().data
                    or []
                )
                return data[0] if data else {**r, "output": output}
        version = 1 + max((_int(r.get("version")) or 0 for r in rows), default=0)
        row = {"run_id": run_id, "scope": OUTPUT_SCOPE, "version": version, "output": output}
        data = sb.table("plans_reader_outputs").insert(row).execute().data or []
        return data[0] if data else row


# Sheets ------------------------------------------------------------------------------


@dataclass
class SheetRef:
    document_id: str
    page: int
    sheet_number: str | None
    sheet_key: str
    w_in: float
    h_in: float
    doc_index: int | None


class SheetIndex:
    """Sheet numbers to (document, page), sheet keys "D<n>P<page>" (doc8 and
    page when the document is not in the run's list) and page sizes, from the
    documents, the category outputs' page lists and the legacy marked spec."""

    def __init__(
        self,
        documents: Sequence[Mapping[str, Any]],
        categories: Sequence[Mapping[str, Any]] = (),
        marked: Mapping[str, Any] | None = None,
    ) -> None:
        self.doc_numbers = doc_order(documents)
        self.sizes: dict[str, tuple[float, float]] = {}
        self.page_sizes: dict[tuple[str, int], tuple[float, float]] = {}
        self.numbers: dict[tuple[str, int], str] = {}
        self.by_number: dict[str, tuple[str, int]] = {}
        self.categories: dict[str, str] = {}
        for d in documents:
            did = str(d.get("id") or "")
            if not did:
                continue
            self.categories[did] = str(d.get("category") or "")
            w, h = _num(d.get("sheet_width_in")), _num(d.get("sheet_height_in"))
            if w and h:
                self.sizes[did] = (w, h)
            for m in d.get("page_meta") or []:
                if not isinstance(m, Mapping):
                    continue
                page = _int(m.get("page"))
                mw, mh = _num(m.get("w_in")), _num(m.get("h_in"))
                if page and mw and mh:
                    self.page_sizes[(did, page)] = (mw, mh)
                if page and m.get("sheet_number"):
                    self.learn(did, page, str(m["sheet_number"]))
        for cat in categories:
            for p in cat.get("pages") or []:
                if isinstance(p, Mapping) and p.get("document_id") and p.get("sheet_number"):
                    page = _int(p.get("page"))
                    if page:
                        self.learn(str(p["document_id"]), page, str(p["sheet_number"]))
        for s in (marked or {}).get("sheets") or []:
            if isinstance(s, Mapping) and s.get("document_id") and s.get("sheet_number"):
                page = _int(s.get("page"))
                if page:
                    self.learn(str(s["document_id"]), page, str(s["sheet_number"]))

    def learn(self, document_id: str, page: int, sheet_number: str | None) -> None:
        if not sheet_number:
            return
        self.numbers.setdefault((document_id, page), sheet_number)
        self.by_number.setdefault(normalize_sheet_number(sheet_number), (document_id, page))

    def resolve(self, sheet_number: Any) -> tuple[str, int] | None:
        return self.by_number.get(normalize_sheet_number(sheet_number))

    def key(self, document_id: str, page: int) -> str:
        return make_sheet_key(self.doc_numbers.get(document_id), page) or f"{doc8(document_id)}:{page}"

    def size(self, document_id: str, page: int) -> tuple[float, float]:
        return self.page_sizes.get((document_id, page)) or self.sizes.get(document_id) or (42.0, 30.0)

    def ref(self, document_id: str, page: int, sheet: SheetInputs | None = None) -> SheetRef:
        if sheet is not None and sheet.sheet_number:
            self.learn(document_id, page, sheet.sheet_number)
        w, h = (sheet.w_in, sheet.h_in) if sheet is not None and sheet.w_in and sheet.h_in else self.size(document_id, page)
        return SheetRef(
            document_id=document_id,
            page=page,
            sheet_number=self.numbers.get((document_id, page)),
            sheet_key=self.key(document_id, page),
            w_in=float(w),
            h_in=float(h),
            doc_index=self.doc_numbers.get(document_id),
        )


class _SheetCache:
    """SheetInputs per (document, page), read once; None when unavailable."""

    def __init__(self, inputs: EvidenceInputs) -> None:
        self.inputs = inputs
        self._cache: dict[tuple[str, int], SheetInputs | None] = {}
        self.errors: dict[tuple[str, int], str] = {}

    def get(self, document_id: str, page: int) -> SheetInputs | None:
        key = (document_id, page)
        if key not in self._cache:
            try:
                self._cache[key] = self.inputs.sheet(document_id, page)
            except FactsUnavailable as exc:
                self._cache[key] = None
                self.errors[key] = str(exc)
            except Exception as exc:  # noqa: BLE001 - a broken page degrades to model points
                logger.warning("evidence: page facts for %s page %s failed: %s", document_id, page, exc)
                self._cache[key] = None
                self.errors[key] = f"{type(exc).__name__}: {exc}"
        return self._cache[key]


# Lines ---------------------------------------------------------------------------------


def line_tag(line: Mapping[str, Any]) -> str | None:
    """The tag token a BOQ line counts ("L1", "L1-EM"), or None: an explicit
    `tag`, else the identity parser's tag, else a leading TYPE/tag token."""
    for key in ("tag", "symbol_or_tag"):
        v = line.get(key)
        if isinstance(v, str) and v.strip() and "," not in v:
            return re.sub(r"\s+", "", v).upper()
    desc = str(line.get("description") or "").upper()
    m = _LEADING_TAG_RE.match(desc)
    if m:
        return m.group(1)
    ident = parse_item(desc, line.get("unit") or "")
    tag = ident.attrs.get("tag")
    return str(tag).upper() if tag else None


def is_variant(line: Mapping[str, Any], own_tag: str | None) -> bool:
    desc = str(line.get("description") or "").upper()
    if own_tag and anchors_mod.normalize_tag(own_tag) != own_tag:
        return True
    return bool(_VARIANT_RE.search(desc))


def line_family(line: Mapping[str, Any], rows: Sequence[Mapping[str, Any]] = ()) -> str | None:
    """`demo_x` for demolition count lines (group DEMOLITION, removal words,
    an "(X)" or a WS4 family tag on a source row), else None."""
    for r in rows:
        fams = [str(f).upper() for f in r.get("family_tags") or []]
        if "DEMO_X" in fams or "(X)" in fams:
            return FAMILY_DEMO_X
    group = str(line.get("group") or "").upper()
    desc = str(line.get("description") or "").upper()
    if group == "DEMOLITION" or _DEMO_LINE_RE.search(desc):
        return FAMILY_DEMO_X
    return None


def hint_tags(line: Mapping[str, Any]) -> tuple[str, ...]:
    desc = str(line.get("description") or "").upper()
    out: list[str] = []
    for pattern, tags in HINT_TAGS:
        if pattern.search(desc):
            out.extend(t for t in tags if t not in out)
    return tuple(out)


def legend_prefix(line: Mapping[str, Any], tag: str | None) -> str:
    group = str(line.get("group") or "").upper()
    code = GROUP_CODES.get(group) or (re.sub(r"[^A-Z]", "", group)[:4] or "LN")
    label = tag or str(line.get("description") or "")[:40]
    return f"{code}, {label}"


def _source_refs(line: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for sr in line.get("source_refs") or []:
        if isinstance(sr, Mapping) and sr.get("ref"):
            out.append({"ref": str(sr["ref"]), "multiplier": _num(sr.get("multiplier")) or 1.0})
        elif isinstance(sr, str) and sr:
            out.append({"ref": sr, "multiplier": 1.0})
    return out


def _ref_row_id(ref: str) -> str:
    """Row ids from refs: "C12", "M3" stay; "m:M3"/"r:R2" lose the prefix."""
    if ":" in ref and ref.split(":", 1)[0].lower() in ("m", "r", "c"):
        return ref.split(":", 1)[1].split(":")[0]
    return ref


@dataclass
class _Demand:
    """One EA line's need on one sheet."""

    line_id: str
    document_id: str
    page: int
    need: int
    points: list[tuple[float, float]]
    point_rows: list[str | None]
    own_tag: str | None
    base_tag: str | None
    family: str | None
    hints: tuple[str, ...]
    variant: bool
    source: str  # rows | model_marks | sheet_refs

    @property
    def pool(self) -> tuple[str, str] | None:
        if self.base_tag:
            return ("tag", self.base_tag)
        if self.family:
            return ("family", self.family)
        return None


def _boq_row(description: str, unit: str, quantity: float = 0.0) -> BoqRow:
    return BoqRow("", "", "", "", description, quantity, unit)


def _match_model_legend(
    lines: Sequence[Mapping[str, Any]],
    marked: Mapping[str, Any] | None,
    index: SheetIndex,
) -> dict[str, list[dict[str, Any]]]:
    """Legacy runs: each EA line's rows in the main output's marked plans
    (one legend row per sheet at most), one-to-one per sheet, greedy by score
    = description similarity + 0.3 for an equal quantity + 0.2 for an equal
    tag; accepted from 0.45. Returns {line_id: [{document_id, page, need,
    points}]}."""
    out: dict[str, list[dict[str, Any]]] = {}
    for s in (marked or {}).get("sheets") or []:
        if not isinstance(s, Mapping) or not s.get("document_id"):
            continue
        did, page = str(s["document_id"]), _int(s.get("page")) or 0
        number = normalize_sheet_number(s.get("sheet_number"))
        legend = [r for r in s.get("legend") or [] if isinstance(r, Mapping)]
        marks = [m for m in s.get("marks") or [] if isinstance(m, Mapping)]
        candidates = []
        for li, line in enumerate(lines):
            refs = {normalize_sheet_number(r) for r in line.get("sheet_refs") or []}
            if refs and number and number not in refs:
                continue
            ltag = line_tag(line)
            for ri, row in enumerate(legend):
                if str(row.get("unit") or "EA").upper() != "EA":
                    continue
                text = f"{row.get('prefix') or ''} {row.get('description') or ''}".strip()
                score = similarity(
                    _boq_row(str(line.get("description") or ""), "EA"), _boq_row(text, "EA")
                )
                if _num(row.get("quantity")) == _num(line.get("quantity")):
                    score += 0.3
                rtag = anchors_mod.fold_token(str(row.get("prefix") or ""))
                if ltag and rtag and anchors_mod.normalize_tag(rtag) == anchors_mod.normalize_tag(ltag):
                    score += 0.2
                candidates.append((score, li, ri))
        candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
        used_l: set[int] = set()
        used_r: set[int] = set()
        for score, li, ri in candidates:
            if score < 0.45 or li in used_l or ri in used_r:
                continue
            used_l.add(li)
            used_r.add(ri)
            pts = [p for m in marks if m.get("legend_index") == ri for p in _points(m.get("points"))]
            need = _int(legend[ri].get("quantity")) or len(pts)
            out.setdefault(str(lines[li]["line_id"]), []).append(
                {"document_id": did, "page": page, "need": need, "points": pts}
            )
    return out


def _demands_for_line(
    line: Mapping[str, Any],
    rows_by_id: Mapping[str, Mapping[str, Any]],
    model_legend: Mapping[str, list[dict[str, Any]]],
    index: SheetIndex,
    sheets: _SheetCache,
) -> list[_Demand]:
    """Per-sheet needs of one EA line: from its source rows (v2), else from
    the legacy model marks, else its whole quantity on its first plan sheet."""
    lid = str(line["line_id"])
    qty = _num(line.get("quantity")) or 0.0
    need_total = int(round(qty))
    own = line_tag(line)
    base = anchors_mod.normalize_tag(own) if own else None
    src_rows = [rows_by_id[_ref_row_id(sr["ref"])] for sr in _source_refs(line) if _ref_row_id(sr["ref"]) in rows_by_id]
    fam = None if base else line_family(line, src_rows)
    hints = hint_tags(line)
    variant = is_variant(line, own)

    def demand(did: str, page: int, need: int, pts: list[tuple[float, float]], prow: list[str | None], source: str) -> _Demand:
        return _Demand(lid, did, page, max(0, need), pts, prow, own, base, fam, hints, variant, source)

    out: list[_Demand] = []
    refs = _source_refs(line)
    if refs:
        by_sheet: dict[tuple[str, int], dict[str, Any]] = {}
        for sr in refs:
            row = rows_by_id.get(_ref_row_id(sr["ref"]))
            if row is None or str(row.get("kind") or "") != "C":
                continue
            sheet = row.get("sheet") if isinstance(row.get("sheet"), Mapping) else {}
            did = str(sheet.get("document_id") or row.get("document_id") or "")
            page = _int(sheet.get("page") or row.get("page")) or 0
            if not did or page < 1:
                continue
            slot = by_sheet.setdefault((did, page), {"need": 0.0, "points": [], "rows": []})
            slot["need"] += (_num(row.get("quantity")) or 0.0) * sr["multiplier"]
            for p in _points(row.get("positions") or row.get("points")):
                slot["points"].append(p)
                slot["rows"].append(str(row.get("row_id") or "") or None)
        for (did, page), slot in sorted(by_sheet.items(), key=lambda kv: (index.key(*kv[0]), kv[0])):
            out.append(demand(did, page, int(round(slot["need"])), slot["points"], slot["rows"], "rows"))
        if out:
            return out
    legend_hits = model_legend.get(lid) or []
    if legend_hits:
        remaining = need_total
        for hit in legend_hits:
            n = min(int(hit["need"]), remaining) if remaining > 0 else 0
            remaining -= n
            out.append(demand(hit["document_id"], int(hit["page"]), n, list(hit["points"]), [None] * len(hit["points"]), "model_marks"))
        return [d for d in out if d.need > 0]
    resolved = [index.resolve(r) for r in line.get("sheet_refs") or []]
    resolved = [r for r in resolved if r is not None]
    chosen = None
    for did, page in resolved:
        sh = sheets.get(did, page)
        if sh is None or not sh.roles or set(sh.roles) & PLAN_ROLES:
            chosen = (did, page)
            break
    if chosen is None and resolved:
        chosen = resolved[0]
    if chosen is None or need_total <= 0:
        return []
    return [demand(chosen[0], chosen[1], need_total, [], [], "sheet_refs")]


# Registration ------------------------------------------------------------------------


@dataclass
class Registration:
    ok: bool
    sx: float = 1.0
    sy: float = 1.0
    dx: float = 0.0
    dy: float = 0.0
    pairs: int = 0
    median_residual_ft: float | None = None
    median_residual_in: float | None = None
    reason: str | None = None
    prior_from: str | None = None

    def apply(self, p: tuple[float, float]) -> tuple[float, float]:
        if not self.ok:
            return p
        return (self.sx * p[0] + self.dx, self.sy * p[1] + self.dy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sx": round(self.sx, 4),
            "sy": round(self.sy, 4),
            "dx": round(self.dx, 4),
            "dy": round(self.dy, 4),
            "pairs": self.pairs,
            "median_residual_ft": None if self.median_residual_ft is None else round(self.median_residual_ft, 2),
            "median_residual_in": None if self.median_residual_in is None else round(self.median_residual_in, 3),
            "reason": self.reason,
            "prior_from": self.prior_from,
        }

    def borrowed(self, sheet_key: str) -> Registration:
        """This accepted registration as another sheet's document prior."""
        return Registration(True, self.sx, self.sy, self.dx, self.dy, 0, self.median_residual_ft,
                            self.median_residual_in, REASON_DOCUMENT_PRIOR, sheet_key)


def _ls(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return 1.0, my - mx
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return k, my - k * mx


def _pairs(
    sets: Sequence[tuple[Sequence[tuple[float, float]], Sequence[tuple[float, float]]]],
    reg: Registration,
    w_in: float,
    h_in: float,
) -> list[tuple[tuple[float, float], tuple[float, float], float]]:
    out = []
    for pts, ancs in sets:
        if not ancs:
            continue
        for p in pts:
            q = reg.apply(p)
            a = min(ancs, key=lambda c: (dist_in(q, c, w_in, h_in), c))
            out.append((p, a, dist_in(q, a, w_in, h_in)))
    return out


def _refit(pairs: Sequence[tuple[tuple[float, float], tuple[float, float], float]]) -> Registration:
    sx, dx = _ls([p[0] for p, _a, _d in pairs], [a[0] for _p, a, _d in pairs])
    sy, dy = _ls([p[1] for p, _a, _d in pairs], [a[1] for _p, a, _d in pairs])
    return Registration(True, sx, sy, dx, dy)


def _median_residual(
    sets: Sequence[tuple[Sequence[tuple[float, float]], Sequence[tuple[float, float]]]],
    reg: Registration,
    w_in: float,
    h_in: float,
) -> tuple[float | None, int]:
    res = sorted(d for _p, _a, d in _pairs(sets, reg, w_in, h_in))
    return (res[len(res) // 2] if res else None), len(res)


def fit_registration(
    sets: Sequence[tuple[Sequence[tuple[float, float]], Sequence[tuple[float, float]]]],
    w_in: float,
    h_in: float,
    ft_per_in: float | None,
    *,
    min_pairs: int = REG_MIN_PAIRS,
    scale_range: tuple[float, float] = REG_SCALE_RANGE,
    max_residual_ft: float = REG_MAX_RESIDUAL_FT,
    max_residual_in: float = REG_MAX_RESIDUAL_IN,
) -> Registration:
    """Axis-aligned `x' = sx x + dx, y' = sy y + dy` fitted by ICP on
    tag-aware sets (model points, same-pool anchors): each iteration pairs
    every point with its nearest same-set anchor through the current
    transform and refits per axis by least squares. A final trimmed refit
    (the best REG_TRIM share of pairs) is kept only when it lowers the median
    residual: trimming every iteration shrinks the scales toward the densest
    cluster (measured on 7154 E3.01: sy 0.51 trimmed against 1.22 untrimmed).
    Accepted with enough pairs, both scales in range and a median residual
    under the limit in feet (inches without a scale)."""
    n_points = sum(len(p) for p, a in sets if a)
    if n_points < min_pairs:
        return Registration(False, pairs=n_points, reason="too_few_pairs")
    reg = Registration(True)
    for _ in range(REG_ITERS):
        nxt = _refit(_pairs(sets, reg, w_in, h_in))
        moved = abs(nxt.sx - reg.sx) + abs(nxt.sy - reg.sy) + abs(nxt.dx - reg.dx) + abs(nxt.dy - reg.dy)
        reg = nxt
        if moved < 1e-7:
            break
    med_in, n_pairs = _median_residual(sets, reg, w_in, h_in)
    pairs = sorted(_pairs(sets, reg, w_in, h_in), key=lambda t: t[2])
    keep = pairs[: max(min_pairs, int(math.ceil(REG_TRIM * len(pairs))))]
    trimmed = _refit(keep)
    t_med, _n = _median_residual(sets, trimmed, w_in, h_in)
    if t_med is not None and med_in is not None and t_med < med_in:
        reg, med_in = trimmed, t_med
    reg.pairs = n_pairs
    reg.median_residual_in = med_in
    reg.median_residual_ft = med_in * ft_per_in if med_in is not None and ft_per_in else None
    lo, hi = scale_range
    if not (lo <= reg.sx <= hi and lo <= reg.sy <= hi):
        reg.ok = False
        reg.reason = "scale_out_of_range"
    elif reg.median_residual_ft is not None and reg.median_residual_ft > max_residual_ft:
        reg.ok = False
        reg.reason = "residual_over_limit"
    elif reg.median_residual_ft is None and (med_in is None or med_in > max_residual_in):
        reg.ok = False
        reg.reason = "residual_over_limit"
    return reg


# Placement -----------------------------------------------------------------------------


@dataclass
class _Mark:
    line_id: str | None
    x: float
    y: float
    method: str
    ref: str | None = None
    row_id: str | None = None
    tag: str | None = None
    count: int = 1
    shared: bool = False

    def to_dict(self, mark_id: str, legend_index: int | None) -> dict[str, Any]:
        return {
            "mark_id": mark_id,
            "kind": "dot",
            "style": MARK_STYLES.get(self.method, "solid"),
            "points": [[_r4(self.x), _r4(self.y)]],
            "legend_index": legend_index,
            "line_id": self.line_id,
            "method": self.method,
            "ref": self.ref,
            "row_id": self.row_id,
            "tag": self.tag,
            "count": self.count,
            "shared_tag": self.shared,
        }


@dataclass
class SheetPlacement:
    """What `place_sheet` decided for one sheet."""

    ref: SheetRef
    method: str
    text_layer_quality: str
    ft_per_in: float | None
    registration: Registration
    marks: list[_Mark] = field(default_factory=list)
    not_located: dict[str, int] = field(default_factory=dict)
    need: dict[str, int] = field(default_factory=dict)
    stacked_merged: int = 0
    anchor_counts: dict[str, int | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class _Scale:
    """Feet-to-paper-inch conversion at a point (the anchor module's view
    assignment, so a two-scale sheet converts per view)."""

    def __init__(self, sheet: SheetInputs | None) -> None:
        self.sheet = sheet

    def ft_per_in(self, p: tuple[float, float]) -> float | None:
        if self.sheet is None:
            return None
        view = anchors_mod.view_at(p[0], p[1], self.sheet.views, self.sheet.w_in, self.sheet.h_in)
        return anchors_mod.scale_for(view, self.sheet)

    def inches(self, p: tuple[float, float], feet: float, fallback_in: float) -> float:
        f = self.ft_per_in(p)
        return feet / f if f and f > 0 else fallback_in

    def primary(self) -> float | None:
        if self.sheet is None:
            return None
        views = anchors_mod.usable_views(self.sheet.views)
        scales = [v.ft_per_in for v in views if v.ft_per_in and v.kind in anchors_mod.PLAN_VIEW_KINDS]
        if scales:
            return scales[0]
        return self.sheet.primary_ft_per_in


@dataclass
class _PoolAlloc:
    """Claims pool anchors for demands; decides anchor vs assigned."""

    key: tuple[str, str]
    pool: list[Any]
    free: list[int]
    token_counts: dict[str, int]
    primary: int | None
    shared: bool
    out: SheetPlacement
    claimed: set[str]
    remaining: dict[int, int]

    def take(self, d: _Demand, i: int, ai: int, row_id: str | None) -> None:
        a = self.pool[ai]
        token = anchors_mod.fold_token(a.text)
        own = d.own_tag is not None and token == d.own_tag
        unique = own and (self.token_counts.get(d.own_tag or "", 0) == 1 or i == self.primary)
        method = METHOD_ANCHOR if (self.key[0] == "tag" and unique) else METHOD_ASSIGNED
        self.out.marks.append(_Mark(d.line_id, a.x, a.y, method, a.ref, row_id, a.tag, 1, self.shared))
        self.claimed.add(a.ref)
        self.free.remove(ai)
        self.remaining[i] -= 1


def place_sheet(
    ref: SheetRef,
    sheet: SheetInputs | None,
    demands: Sequence[_Demand],
    *,
    unavailable_note: str | None = None,
    prior: Registration | None = None,
) -> SheetPlacement:
    """Marks for every EA demand on one sheet (module docstring, steps 1 to 6).
    Deterministic: demands in (line_id) order, anchors in reading order.
    `prior` (a same-document registration, `Registration.borrowed`) is used
    only when the sheet has no tag-aware pairs of its own."""
    w, h = ref.w_in, ref.h_in
    scale = _Scale(sheet)
    tags = sorted({d.base_tag for d in demands if d.base_tag} | {t for d in demands for t in d.hints})
    fams = sorted({d.family for d in demands if d.family and not d.base_tag})
    if sheet is not None:
        aset = anchors_mod.extract_anchors(sheet, tags, families=fams)
    else:
        aset = anchors_mod.unavailable_set(ref.document_id, ref.page, tags, families=fams, note=unavailable_note)
    out = SheetPlacement(
        ref=ref,
        method=aset.method,
        text_layer_quality=aset.text_layer_quality,
        ft_per_in=scale.primary(),
        registration=Registration(False, reason="no_pairs"),
        anchor_counts=aset.counts(),
        notes=list(aset.notes),
    )
    pools: dict[tuple[str, str], list[anchors_mod.Anchor]] = {}
    for d in demands:
        key = d.pool
        if key is None or key in pools:
            continue
        pools[key] = aset.by_tag(anchors_mod.DEMO_X_TAG if key[0] == "family" else key[1]) if aset.available else []
    pool_tags = {k[1] for k in pools if k[0] == "tag"}
    hint_anchors = [a for a in aset.anchors if a.tag not in pool_tags and a.family == anchors_mod.FAMILY_TAG]

    # Registration on tag-aware (and family-aware) sets.
    sets = []
    for d in demands:
        pool = pools.get(d.pool) if d.pool else None
        if pool and d.points:
            sets.append((d.points, [(a.x, a.y) for a in pool]))
    if sets:
        out.registration = fit_registration(sets, w, h, out.ft_per_in)
    if prior is not None and prior.ok and not out.registration.ok and out.registration.reason in PRIOR_REASONS:
        out.registration = prior
    reg = out.registration

    used_points: dict[int, set[int]] = {i: set() for i in range(len(demands))}
    remaining = {i: d.need for i, d in enumerate(demands)}
    claimed: set[str] = set()
    for i, d in enumerate(demands):
        out.need[d.line_id] = out.need.get(d.line_id, 0) + d.need

    for key, pool in pools.items():
        members = [i for i, d in enumerate(demands) if d.pool == key]
        token_counts: dict[str, int] = {}
        for i in members:
            tok = demands[i].own_tag
            if tok:
                token_counts[tok] = token_counts.get(tok, 0) + 1
        primaries = [i for i in members if demands[i].own_tag and not demands[i].variant]
        primary = primaries[0] if len(primaries) == 1 else None
        shared = len(members) > 1
        order = sorted(members, key=lambda i: (0 if i == primary else 1, -demands[i].need, demands[i].line_id))
        free = list(range(len(pool)))
        take = _PoolAlloc(key, pool, free, token_counts, primary, shared, out, claimed, remaining).take

        pairs = []
        for i in members:
            for pi, p in enumerate(demands[i].points):
                q = reg.apply(p)
                for ai, a in enumerate(pool):
                    pairs.append((dist_in(q, (a.x, a.y), w, h), demands[i].line_id, pi, ai, i))
        pairs.sort()
        for _dd, _lid, pi, ai, i in pairs:
            if remaining[i] <= 0 or ai not in free or pi in used_points[i]:
                continue
            used_points[i].add(pi)
            take(demands[i], i, ai, demands[i].point_rows[pi] if pi < len(demands[i].point_rows) else None)
        for i in order:
            while remaining[i] > 0 and free:
                take(demands[i], i, free[0], None)
        for ai in free:
            a = pool[ai]
            out.marks.append(_Mark(None, a.x, a.y, METHOD_UNCLAIMED, a.ref, None, a.tag))

    # Remaining quantity from leftover model points.
    for i, d in enumerate(demands):
        if remaining[i] <= 0:
            continue
        model_marks: list[_Mark] = []
        for pi, p in enumerate(d.points):
            if remaining[i] <= 0:
                break
            if pi in used_points[i]:
                continue
            used_points[i].add(pi)
            row_id = d.point_rows[pi] if pi < len(d.point_rows) else None
            q = reg.apply(p)
            radius = scale.inches(q, SNAP_RADIUS_FT, SNAP_RADIUS_IN)
            best = None
            if d.hints:
                cands = [a for a in hint_anchors if a.tag in d.hints and a.ref not in claimed]
                if cands:
                    best = min(cands, key=lambda a: (dist_in(q, (a.x, a.y), w, h), a.ref))
                    if dist_in(q, (best.x, best.y), w, h) > radius:
                        best = None
            if best is not None:
                claimed.add(best.ref)
                out.marks.append(_Mark(d.line_id, best.x, best.y, METHOD_SNAPPED, best.ref, row_id, best.tag))
                remaining[i] -= 1
                continue
            dup_radius = scale.inches(q, DUPLICATE_SNAP_FT, DUPLICATE_SNAP_IN)
            held = [m for m in out.marks if m.line_id == d.line_id and m.method in (METHOD_ANCHOR, METHOD_SNAPPED)]
            twin = min(held, key=lambda m: (dist_in(q, (m.x, m.y), w, h), m.ref or "")) if held else None
            if twin is not None and dist_in(q, (twin.x, twin.y), w, h) <= dup_radius:
                twin.count += 1  # the same device read twice: stacked, the extra is not located
                remaining[i] -= 1
                continue
            method = METHOD_REGISTERED if reg.ok else METHOD_APPROX
            x = min(1.0, max(0.0, q[0]))
            y = min(1.0, max(0.0, q[1]))
            model_marks.append(_Mark(d.line_id, x, y, method, None, row_id, d.base_tag))
            remaining[i] -= 1
        merged = _merge_stacked(model_marks, scale, w, h)
        out.stacked_merged += len(model_marks) - len(merged)
        out.marks.extend(merged)

    placed: dict[str, int] = {}
    for m in out.marks:
        if m.line_id is not None:
            placed[m.line_id] = placed.get(m.line_id, 0) + 1
    for lid, need in out.need.items():
        out.not_located[lid] = max(0, need - placed.get(lid, 0))
    return out


def _merge_stacked(marks: list[_Mark], scale: _Scale, w: float, h: float) -> list[_Mark]:
    """Model-point marks of one line closer than STACK_MERGE_FT (paper inches
    at the view scale) merge into the first, which carries the count."""
    kept: list[_Mark] = []
    for m in marks:
        if m.method not in STACKABLE_METHODS:
            kept.append(m)
            continue
        limit = scale.inches((m.x, m.y), STACK_MERGE_FT, STACK_MERGE_IN)
        target = None
        for k in kept:
            if k.method in STACKABLE_METHODS and dist_in((k.x, k.y), (m.x, m.y), w, h) < limit:
                target = k
                break
        if target is None:
            kept.append(m)
        else:
            target.count += m.count
    return kept


# Measure (WS3) input adapter --------------------------------------------------------


@dataclass
class _Term:
    kind: str
    route: str | None
    segments: tuple[int, ...] | None
    length_ft: float | None
    conductors: Any
    drawn: bool | None
    linear_item: str | None
    segment_kind: str | None = None
    count: int | None = None
    instance: int | None = None
    basis: str | None = None


class MeasureView:
    """A tolerant reader over a MeasureOutput v1 (spec 5.4): routes by route
    id and circuit id, quantity rows by M id, linear items by measure id.
    Unknown or missing keys read as absent; nothing raises.

    Derivation terms are read in the shape WS3 writes
    (`plans_reader_routes.branch_quantities` and `plans_reader_measure`):

    - route terms `{circuit_id, route_id, segment: <segment kind>,
      length_ft, count, basis}`, one per route segment (per conductor size
      for wire rows); `count` is the conductor count of that size;
    - linear-item terms `{instance, length_ft, method, measure_id,
      linear_item_id, basis}` under a derivation that carries `tag` and
      `measure_id`; the item is `linear_items[]` by `measure_id` (from the
      term, else the M row's `refs` `m:<measure_id>`, else the derivation
      tag); `instance` is 1-based into the item's `instances[]`;
    - allowance terms `{devices, ft_per_device}` (control cable).

    An older shape (`kind`, `segments` index lists, `linear_item_id`) is
    still read so stored outputs from before WS3 keep their polylines."""

    def __init__(self, measure: Mapping[str, Any] | None) -> None:
        self.measure = measure if isinstance(measure, Mapping) else None
        self.routes: dict[str, dict[str, Any]] = {}
        self.quantities: dict[str, dict[str, Any]] = {}
        self.linear_items: dict[str, dict[str, Any]] = {}
        if self.measure is None:
            return
        for i, r in enumerate(self.measure.get("routes") or []):
            if not isinstance(r, Mapping):
                continue
            route = dict(r)
            rid = str(route.get("route_id") or route.get("id") or f"route{i + 1}")
            route.setdefault("route_id", rid)
            self.routes[rid] = route
            cid = route.get("circuit_id") or route.get("circuit")
            if cid is not None:
                self.routes.setdefault(str(cid), route)
        for q in iter_measure_rows(self.measure):
            self.quantities[str(q["row_id"])] = q
        for i, it in enumerate(self.measure.get("linear_items") or []):
            if isinstance(it, Mapping):
                iid = str(it.get("measure_id") or it.get("id") or it.get("item_id") or f"li{i + 1}")
                self.linear_items[iid] = dict(it)
                for alias in (it.get("id"), it.get("item_id")):
                    if alias and str(alias) not in self.linear_items:
                        self.linear_items[str(alias)] = self.linear_items[iid]

    @property
    def available(self) -> bool:
        return self.measure is not None

    def linear_item_for(self, qrow: Mapping[str, Any]) -> str | None:
        """The linear item behind an M row: its `m:<measure_id>` ref, else
        the derivation tag on the same sheet keys (first match)."""
        for ref in qrow.get("refs") or []:
            ref = str(ref)
            if ref.lower().startswith("m:") and ref[2:] in self.linear_items:
                return ref[2:]
        deriv = qrow.get("derivation") if isinstance(qrow.get("derivation"), Mapping) else {}
        tag = str(deriv.get("tag") or "").upper()
        if not tag:
            return None
        sheets = {str(s) for s in qrow.get("sheet_refs") or []}
        for iid, it in self.linear_items.items():
            if str(it.get("tag") or "").upper() == tag and (not sheets or str(it.get("sheet_key") or "") in sheets):
                return iid
        return None

    def terms(self, qrow: Mapping[str, Any]) -> list[_Term]:
        deriv = qrow.get("derivation") if isinstance(qrow.get("derivation"), Mapping) else {}
        raw = deriv.get("terms") if isinstance(deriv.get("terms"), list) else []
        row_item = None
        out = []
        for t in raw:
            if not isinstance(t, Mapping):
                continue
            seg_kind: str | None = None
            segs = t.get("segments")
            seg = t.get("segment")
            if segs is None and seg is not None:
                if not isinstance(seg, bool) and _int(seg) is not None:
                    segs = [seg]
                else:
                    seg_kind = str(seg).lower()
            if segs is None and t.get("segment_index") is not None:
                segs = [t.get("segment_index")]
            seg_t = tuple(int(s) for s in segs if _int(s) is not None) if isinstance(segs, list) else None
            route = t.get("route_id") or t.get("route") or t.get("circuit_id") or t.get("circuit")
            length = _num(t.get("length_ft", t.get("ft", t.get("feet"))))
            kind = str(t.get("kind") or t.get("type") or seg_kind or "").lower()
            linear = t.get("linear_item_id") or t.get("measure_id") or t.get("linear_item") or t.get("item_id")
            instance = _int(t.get("instance")) if t.get("instance") is not None else None
            if instance is not None and not linear:
                if row_item is None:
                    row_item = self.linear_item_for(qrow) or ""
                linear = row_item or None
            if instance is not None or linear:
                kind = kind or "linear_item"
            if route is None and not linear and t.get("devices") is not None and t.get("ft_per_device") is not None:
                devices = _num(t.get("devices")) or 0.0
                per = _num(t.get("ft_per_device")) or 0.0
                length = round(devices * per, 2)
                kind = kind or "allowance"
            count = _int(t.get("count")) if not isinstance(t.get("count"), bool) else None
            out.append(
                _Term(
                    kind=kind,
                    route=str(route) if route is not None else None,
                    segments=seg_t,
                    length_ft=length,
                    conductors=t.get("conductors"),
                    drawn=t.get("drawn") if isinstance(t.get("drawn"), bool) else None,
                    linear_item=str(linear) if linear else None,
                    segment_kind=seg_kind,
                    count=count if count and count > 0 else None,
                    instance=instance,
                    basis=str(t.get("basis")) if t.get("basis") else None,
                )
            )
        if not out:
            for rid in qrow.get("route_ids") or qrow.get("routes") or []:
                out.append(_Term("route", str(rid), None, None, None, None, None))
        return out


def _conductor_count(value: Any, size: str | None) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    if isinstance(value, Mapping):
        if size and size in value:
            return _int(value[size])
        total = sum(_int(v) or 0 for v in value.values())
        return total or None
    return None


def centerline(item: Mapping[str, Any], w_in: float, h_in: float) -> list[tuple[float, float]]:
    """A linear item's centerline in page fractions (4 decimals): from
    `polyline`, `endpoints`, a 4-point `polygon` (midpoints of the two short
    edges) or an axis-aligned `rect`/`bbox` [x0, y0, x1, y1] (midline along
    the long side in paper inches)."""
    return [(_r4(x), _r4(y)) for x, y in _centerline(item, w_in, h_in)]


def _centerline(item: Mapping[str, Any], w_in: float, h_in: float) -> list[tuple[float, float]]:
    for key in ("polyline", "centerline"):
        pts = _points(item.get(key))
        if len(pts) >= 2:
            return pts
    ends = item.get("endpoints")
    if isinstance(ends, list) and len(ends) == 2:
        pts = _points(ends)
        if len(pts) == 2:
            return pts
    poly = _points(item.get("polygon"))
    if len(poly) == 4:
        edges = [(poly[i], poly[(i + 1) % 4]) for i in range(4)]
        lens = [dist_in(a, b, w_in, h_in) for a, b in edges]
        short = sorted(range(4), key=lambda i: lens[i])[:2]
        mids = [((edges[i][0][0] + edges[i][1][0]) / 2, (edges[i][0][1] + edges[i][1][1]) / 2) for i in sorted(short)]
        return mids
    box = item.get("rect") or item.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4 and all(_num(v) is not None for v in box):
        x0, y0, x1, y1 = (float(v) for v in box)
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if (x1 - x0) * w_in >= (y1 - y0) * h_in:
            ym = (y0 + y1) / 2
            return [(x0, ym), (x1, ym)]
        xm = (x0 + x1) / 2
        return [(xm, y0), (xm, y1)]
    return []


@dataclass
class _Poly:
    line_id: str
    document_id: str
    page: int
    points: list[tuple[float, float]]
    length_ft: float | None
    basis: str
    segment_kind: str
    ref: str | None
    conductors: int
    label: str

    @property
    def dashed(self) -> bool:
        return self.basis not in MEASURED_BASES

    def to_dict(self, mark_id: str, legend_index: int | None) -> dict[str, Any]:
        return {
            "mark_id": mark_id,
            "kind": "polyline",
            "style": "dashed" if self.dashed else "solid",
            "points": [[_r4(x), _r4(y)] for x, y in self.points],
            "legend_index": legend_index,
            "line_id": self.line_id,
            "length_ft": None if self.length_ft is None else round(self.length_ft, 2),
            "basis": self.basis,
            "segment_kind": self.segment_kind,
            "ref": self.ref,
            "conductors": self.conductors,
            "label": self.label,
        }


def line_polylines(
    line: Mapping[str, Any],
    measure: MeasureView,
    rows_by_id: Mapping[str, Mapping[str, Any]],
    index: SheetIndex,
) -> tuple[list[_Poly], dict[str, Any]]:
    """Polylines of one FT line and its length check (module docstring).
    Returns (polylines, summary {drawn_ft, not_drawn [{kind, ft}],
    expected_ft, line_ft, ok, circuits, assumed_share, sources})."""
    lid = str(line["line_id"])
    desc = str(line.get("description") or "")
    ident = parse_item(desc, line.get("unit") or "FT")
    klass = ident.item_class
    size = line_conductor(desc) if klass in ("conductor", "cable") else None
    qty = _num(line.get("quantity"))
    polys: list[_Poly] = []
    not_drawn: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    circuits: set[str] = set()
    weighted_drawn = 0.0
    weighted_assumed = 0.0
    sources: list[str] = []

    def conductors_of(t: _Term | None, seg: Mapping[str, Any] | None, route: Mapping[str, Any] | None) -> int:
        if not size:
            return 1
        if t is not None and t.count:
            return t.count
        for src in (
            (seg or {}).get("conductors_parsed"),
            (seg or {}).get("conductors"),
            t.conductors if t is not None else None,
            (route or {}).get("conductors"),
        ):
            n = _conductor_count(src, size)
            if n:
                return n
        return 1

    def add_segment(route: Mapping[str, Any], si: int, seg: Mapping[str, Any], t: _Term) -> None:
        nonlocal weighted_drawn, weighted_assumed
        rid = str(route.get("route_id") or "")
        if (rid, si) in seen:
            return
        seen.add((rid, si))
        basis_raw = str(seg.get("basis") or t.basis or route.get("basis") or "").lower()
        basis = "measured" if basis_raw in ("measured", "measured_route") else "assumed_route"
        length = _num(seg.get("length_ft"))
        if length is None:
            length = t.length_ft
        n = conductors_of(t, seg, route)
        did = str(seg.get("document_id") or route.get("document_id") or "")
        page = _int(seg.get("page") or route.get("page")) or 0
        pieces = [pl for pl in (_points(x) for x in seg.get("polylines") or []) if len(pl) >= 2]
        if not pieces:
            pts = _points(seg.get("polyline"))
            pieces = [pts] if len(pts) >= 2 else []
        kind = str(seg.get("kind") or t.segment_kind or "route")
        # WS3 writes drawn=False on every synthesized segment (routes are not
        # drawn by the designer); geometry is what makes a segment drawable.
        drawable = bool(pieces) and bool(did) and page > 0 and kind not in NOT_DRAWN_TERM_KINDS
        if not drawable:
            not_drawn.append({"kind": kind, "ft": round((length or 0.0) * n, 2), "route": rid})
            return
        circuits.add(str(route.get("circuit_id") or rid))
        weighted_drawn += (length or 0.0) * n
        if basis != "measured":
            weighted_assumed += (length or 0.0) * n
        w, h = index.size(did, page)
        paper = [sum(dist_in(a, b, w, h) for a, b in zip(pl, pl[1:])) for pl in pieces]
        total_in = sum(paper)
        cid = route.get("circuit_id") or rid
        for k, pl in enumerate(pieces):
            if length is None:
                piece_ft = None
            elif len(pieces) == 1 or total_in <= 0:
                piece_ft = length if k == 0 else 0.0
            else:
                piece_ft = length * paper[k] / total_in
            label = f"{cid} {length:.1f} FT" if length is not None and k == 0 else str(cid)
            polys.append(_Poly(lid, did, page, pl, piece_ft, basis, kind, f"r:{rid}", n, label))

    def pick_segment(segs: list[Mapping[str, Any]], rid: str, t: _Term) -> int | None:
        """The one route segment a WS3 term names: its kind, the first not
        yet used with the same length (0.01 ft), else the first unused of
        the kind."""
        cands = [i for i, sg in enumerate(segs) if str(sg.get("kind") or "").lower() == t.segment_kind and (rid, i) not in seen]
        if t.length_ft is not None:
            for i in cands:
                if abs((_num(segs[i].get("length_ft")) or 0.0) - t.length_ft) <= 0.011:
                    return i
        return cands[0] if cands else None

    m_ids = [_ref_row_id(sr["ref"]) for sr in _source_refs(line)]
    for mid in m_ids:
        q = measure.quantities.get(mid)
        if q is None:
            continue
        sources.append(mid)
        for t in measure.terms(q):
            if t.linear_item or t.kind == "linear_item":
                item = measure.linear_items.get(t.linear_item or "")
                inst: Mapping[str, Any] | None = None
                if item is not None and t.instance is not None:
                    insts = [x for x in item.get("instances") or [] if isinstance(x, Mapping)]
                    inst = insts[t.instance - 1] if 1 <= t.instance <= len(insts) else None
                geo = inst if inst is not None else item
                if geo is None:
                    not_drawn.append({"kind": "linear_item", "ft": round(t.length_ft or 0.0, 2), "route": None})
                    continue
                did = str((item or {}).get("document_id") or "")
                page = _int((item or {}).get("page")) or 0
                w, h = index.size(did, page)
                pts = centerline(geo, w, h)
                length = _num(geo.get("length_ft")) or t.length_ft
                if len(pts) >= 2 and did and page:
                    weighted_drawn += length or 0.0
                    mid_ref = str((item or {}).get("measure_id") or t.linear_item)
                    item_basis = str((item or {}).get("basis") or t.basis or "measured").lower()
                    lbasis = "measured" if item_basis in ("measured", "measured_route") else "assumed_route"
                    if lbasis != "measured":
                        weighted_assumed += length or 0.0
                    polys.append(
                        _Poly(lid, did, page, pts, length, lbasis, "linear_item", f"m:{mid_ref}", 1,
                              f"{(item or {}).get('tag') or ''} {length or 0:.1f} FT".strip())
                    )
                else:
                    not_drawn.append({"kind": "linear_item", "ft": round(length or 0.0, 2), "route": None})
                continue
            if t.kind in NOT_DRAWN_TERM_KINDS or t.drawn is False:
                n = conductors_of(t, None, None)
                not_drawn.append({"kind": t.kind or "term", "ft": round((t.length_ft or 0.0) * n, 2), "route": t.route})
                continue
            route = measure.routes.get(t.route or "") if t.route else None
            if route is None:
                if t.length_ft:
                    n = conductors_of(t, None, None)
                    not_drawn.append({"kind": t.kind or "term", "ft": round(t.length_ft * n, 2), "route": t.route})
                continue
            rid = str(route.get("route_id") or "")
            segs = [sg for sg in route.get("segments") or [] if isinstance(sg, Mapping)]
            if t.segment_kind is not None:
                si = pick_segment(segs, rid, t)
                if si is None:
                    if t.length_ft:
                        n = conductors_of(t, None, route)
                        not_drawn.append({"kind": t.segment_kind, "ft": round(t.length_ft * n, 2), "route": rid})
                    continue
                add_segment(route, si, segs[si], t)
                continue
            idxs = t.segments if t.segments else tuple(range(len(segs)))
            for si in idxs:
                if 0 <= si < len(segs):
                    add_segment(route, si, segs[si], t)
    for sr in _source_refs(line):
        row = rows_by_id.get(_ref_row_id(sr["ref"]))
        if row is None or str(row.get("kind") or "") != "R":
            continue
        pts = _points(row.get("route"))
        sheet = row.get("sheet") if isinstance(row.get("sheet"), Mapping) else {}
        did = str(sheet.get("document_id") or row.get("document_id") or "")
        page = _int(sheet.get("page") or row.get("page")) or 0
        if len(pts) >= 2 and did and page:
            length = _num(row.get("length_ft"))
            basis = "measured" if str(row.get("basis") or "").startswith("measured") else "assumed_route"
            weighted_drawn += length or 0.0
            sources.append(str(row.get("row_id")))
            polys.append(_Poly(lid, did, page, pts, length, basis, "run", str(row.get("row_id")), 1, f"{length or 0:.1f} FT"))

    nd_total = sum(float(n["ft"]) for n in not_drawn)
    expected = weighted_drawn + nd_total
    ok = None
    if qty is not None and (polys or not_drawn):
        ok = abs(expected - qty) <= max(LENGTH_TOL_FT, LENGTH_TOL_REL * qty)
    summary = {
        "drawn_ft": round(weighted_drawn, 2),
        "not_drawn": not_drawn,
        "expected_ft": round(expected, 2),
        "line_ft": qty,
        "ok": ok,
        "circuits": len(circuits),
        "assumed_share": round(weighted_assumed / weighted_drawn, 4) if weighted_drawn > 0 else None,
        "sources": sources,
    }
    return polys, summary


# Quantity chips ---------------------------------------------------------------------


def _line_basis(line: Mapping[str, Any], rows_by_id: Mapping[str, Mapping[str, Any]], measure: MeasureView) -> str | None:
    basis = line.get("basis")
    if isinstance(basis, str) and basis:
        return basis
    kinds = set()
    for sr in _source_refs(line):
        rid = _ref_row_id(sr["ref"])
        row = rows_by_id.get(rid) or measure.quantities.get(rid)
        if row is None:
            continue
        k = str(row.get("kind") or rid[:1])
        if k == "C":
            kinds.add("counted")
        elif k == "S":
            kinds.add("schedule")
        elif k == "M":
            mb = str(row.get("basis") or "")
            kinds.add("measured" if mb in ("measured_route", "measured") else "assumed_route")
        elif k in ("F", "O", "P"):
            kinds.add("policy")
        elif k == "R":
            kinds.add("measured" if str(row.get("basis") or "").startswith("measured") else "assumed_route")
    if len(kinds) == 1:
        return kinds.pop()
    if len(kinds) > 1:
        return "mixed"
    return None


def quantity_chip(
    line: Mapping[str, Any],
    *,
    marks: int = 0,
    not_located: int = 0,
    poly: Mapping[str, Any] | None = None,
    rows_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    measure: MeasureView | None = None,
) -> Chip:
    """The quantity chip by the precedence of WS8 design 6.7: unit_error,
    manual, policy, unsourced, partial, linear_luminaire_ea, census_floor,
    then the basis (counted, measured, assumed_route, schedule, stated,
    mixed)."""
    rows_by_id = rows_by_id or {}
    measure = measure or MeasureView(None)
    unit = str(line.get("unit") or "").upper()
    qty = _num(line.get("quantity")) or 0.0
    ident = parse_item(str(line.get("description") or ""), unit)
    q = int(round(qty)) if unit == "EA" else round(qty, 2)
    sheets = [str(s) for s in line.get("sheet_refs") or []]
    sheet = sheets[0] if sheets else ""
    if unit == "EA" and is_linear(ident):
        return Chip("qty", "unit_error", "danger", {"q": q, "class": ident.item_class})
    combine = str(line.get("combine") or "")
    basis = _line_basis(line, rows_by_id, measure)
    if combine == "manual" or basis == "manual":
        return Chip("qty", "manual", "neutral", {"q": q})
    refs = _source_refs(line)
    if basis == "policy" or str(line.get("group") or "").upper() == "ALLOWANCES" or unit == "LS":
        rule = next((r["ref"] for r in refs if r["ref"].startswith("p:")), None) or line.get("policy_rule_id") or ""
        return Chip("qty", "policy", "neutral", {"rule": rule, "q": q})
    if not refs:
        caveat = f"{line.get('caveat') or ''} {line.get('description') or ''}"
        if unit == "FT" and _ASSUMED_RE.search(caveat):
            return Chip("qty", "assumed_route", "warn", {"ft": q, "c": (poly or {}).get("circuits", 0)})
        return Chip("qty", "unsourced", "danger", {"q": q, "unit": unit, "m": marks, "k": not_located})
    rows = [rows_by_id.get(_ref_row_id(r["ref"])) for r in refs]
    rows = [r for r in rows if r is not None]
    if line.get("needs_recount") or any(str(r.get("coverage") or "") in ("tile_subtotal", "partial") for r in rows):
        return Chip("qty", "partial", "danger", {"q": q})
    if unit == "EA" and linear_evidence_required(ident):
        return Chip("qty", "linear_luminaire_ea", "warn", {"q": q, "m": marks})
    if combine == "floor" or basis == "census_floor":
        return Chip("qty", "census_floor", "warn", {"q": q})
    poly = poly or {}
    if basis == "counted" or (basis is None and unit == "EA"):
        tone = "ok" if marks >= q and not_located == 0 else "warn"
        return Chip("qty", "counted", tone, {"q": q, "m": marks, "k": not_located})
    if basis == "measured":
        ok = poly.get("ok")
        return Chip("qty", "measured", "ok" if ok else "warn",
                    {"ft": q, "drawn_ft": poly.get("drawn_ft"), "length_ok": ok})
    if basis == "assumed_route":
        return Chip("qty", "assumed_route", "warn", {"ft": q, "c": poly.get("circuits", 0), "length_ok": poly.get("ok")})
    if basis == "schedule":
        return Chip("qty", "schedule", "ok", {"sheet": sheet, "q": q})
    if basis == "stated":
        return Chip("qty", "stated", "ok", {"sheet": sheet, "q": q})
    if basis == "mixed":
        parts = []
        for r in rows:
            k = str(r.get("kind") or "")
            label = {"C": "counted", "S": "schedule", "M": "measured", "R": "run"}.get(k, "source")
            if label not in parts:
                parts.append(label)
        first = parts[0] if parts else "source"
        second = parts[1] if len(parts) > 1 else "source"
        return Chip("qty", "mixed", "warn", {"first": first, "second": second, "q": q})
    return Chip("qty", "counted" if unit == "EA" else "measured", "warn", {"q": q, "m": marks, "k": not_located, "ft": q})


# Price chips (read time, pricing roles only; never stored) --------------------------


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _as_line_dict(priced: Any) -> dict[str, Any]:
    if isinstance(priced, Mapping):
        return dict(priced)
    to_dict = getattr(priced, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {}


def price_chip(priced: Any | None) -> Chip:
    """The price chip of one priced line (a WS6 PricedLine or its dict):
    uom_mismatch, quote, price_policy (seed), library (strong or good,
    n = observations), library_weak, missing. Never carries an amount."""
    if priced is None:
        return Chip("price", "missing", "danger", {})
    p = _as_line_dict(priced)
    flags = {str(f) for f in p.get("flags") or ()}
    if flags & {"uom_conflict", "uom_conflict_quote", "uom_mismatch"}:
        return Chip("price", "uom_mismatch", "danger", {})
    source = str(p.get("material_source") or "")
    if source.startswith("quote:"):
        return Chip("price", "quote", "ok", {"quote": source.split(":", 1)[1]})
    labor = p.get("labor") if isinstance(p.get("labor"), Mapping) else {}
    material = p.get("material") if isinstance(p.get("material"), Mapping) else {}
    if labor.get("value") is None and material.get("value") is None:
        return Chip("price", "missing", "danger", {})
    methods = {str(labor.get("method") or ""), str(material.get("method") or "")}
    if "seed" in methods or source == "seed":
        return Chip("price", "price_policy", "neutral", {})
    quality = str(labor.get("quality") or material.get("quality") or "missing")
    n = _int(labor.get("n_obs")) or _int(material.get("n_obs")) or 0
    projects = _int(labor.get("n_projects")) or _int(material.get("n_projects")) or 0
    if quality == "strong":
        return Chip("price", "library", "ok", {"n": n, "projects": projects, "quality": quality})
    if quality == "good":
        return Chip("price", "library", "neutral", {"n": n, "projects": projects, "quality": quality})
    if quality == "weak":
        return Chip("price", "library_weak", "warn", {"n": n, "projects": projects})
    if quality == "policy":
        return Chip("price", "price_policy", "neutral", {})
    if quality == "quote":
        return Chip("price", "quote", "ok", {"quote": ""})
    return Chip("price", "missing", "danger", {})


def attach_price_chips(evidence: Mapping[str, Any], priced_lines: Iterable[Any]) -> dict[str, Any]:
    """A copy of the evidence dict with `price_chip` on every line (pricing
    roles only; the route checks the role). Lines without a priced line get
    `missing`."""
    by_id: dict[str, Any] = {}
    for pl in priced_lines or ():
        lid = _get(pl, "line_id")
        if lid:
            by_id[str(lid)] = pl
    out = copy.deepcopy(dict(evidence))
    for line in out.get("lines") or []:
        line["price_chip"] = price_chip(by_id.get(str(line.get("line_id")))).to_dict()
    return out


# Evidence output --------------------------------------------------------------------


@dataclass
class EvidenceOutput:
    run_id: str | None
    main_version: int | None
    boq_version: int | None
    inputs_hash: str
    built_at: str
    sheets: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    code_version: str = EVIDENCE_CODE_VERSION
    reused: bool = False  # True when an identical stored evidence was returned

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EVIDENCE_SCHEMA,
            "code_version": self.code_version,
            "run_id": self.run_id,
            "main_version": self.main_version,
            "boq_version": self.boq_version,
            "inputs_hash": self.inputs_hash,
            "built_at": self.built_at,
            "sheets": copy.deepcopy(self.sheets),
            "lines": copy.deepcopy(self.lines),
            "stats": copy.deepcopy(self.stats),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, reused: bool = False) -> EvidenceOutput:
        return cls(
            run_id=data.get("run_id"),
            main_version=_int(data.get("main_version")),
            boq_version=_int(data.get("boq_version")),
            inputs_hash=str(data.get("inputs_hash") or ""),
            built_at=str(data.get("built_at") or ""),
            sheets=copy.deepcopy(list(data.get("sheets") or [])),
            lines=copy.deepcopy(list(data.get("lines") or [])),
            stats=copy.deepcopy(dict(data.get("stats") or {})),
            notes=list(data.get("notes") or []),
            code_version=str(data.get("code_version") or EVIDENCE_CODE_VERSION),
            reused=reused,
        )

    def line(self, line_id: str) -> dict[str, Any] | None:
        for ln in self.lines:
            if ln.get("line_id") == line_id:
                return ln
        return None

    def marks(self, *, sheet_key: str | None = None, kind: str = "dot") -> list[dict[str, Any]]:
        return [
            m
            for s in self.sheets
            if sheet_key is None or s.get("sheet_key") == sheet_key
            for m in s.get("marks") or []
            if m.get("kind") == kind
        ]


def money_keys(obj: Any, path: str = "") -> list[str]:
    """Paths of keys that look like money or price provenance (the stored
    evidence must have none)."""
    out: list[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if MONEY_KEY_RE.search(str(k)):
                out.append(p)
            out.extend(money_keys(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(money_keys(v, f"{path}[{i}]"))
    return out


def inputs_hash(
    *,
    lines: Sequence[Mapping[str, Any]],
    categories: Sequence[Mapping[str, Any]],
    measure: Mapping[str, Any] | None,
    marked: Mapping[str, Any] | None,
    documents: Sequence[Mapping[str, Any]],
    source: str,
) -> str:
    """What evidence depends on: the BOQ lines, the category rows, the
    MeasureOutput, the legacy model marks, the documents, the page facts
    source and this module's code version and thresholds."""
    keep = ("line_id", "description", "quantity", "unit", "sheet_refs", "source_refs", "basis", "combine",
            "caveat", "needs_recount", "group", "section")
    return _sha(
        {
            "code": EVIDENCE_CODE_VERSION,
            "thresholds": [SNAP_RADIUS_FT, STACK_MERGE_FT, REG_MIN_PAIRS, REG_SCALE_RANGE, REG_MAX_RESIDUAL_FT,
                           REG_TRIM, MAX_SHEET_BYTES],
            "lines": [{k: ln.get(k) for k in keep} for ln in lines],
            "categories": _sha(list(categories)),
            "measure": _sha(measure) if measure else None,
            "marked": _sha(marked) if marked else None,
            "documents": sorted(
                (str(d.get("id")), d.get("sheet_width_in"), d.get("sheet_height_in"), d.get("page_count"))
                for d in documents
            ),
            "source": source,
        }
    )


def _cap_sheet(sheet: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Keep a sheet's serialized evidence under max_bytes: drop unclaimed
    marks first, then trailing marks (line order), flagging `truncated`."""
    if len(_canon(sheet)) <= max_bytes:
        return sheet
    sheet["truncated"] = True
    marks = sheet.get("marks") or []
    sheet["marks"] = [m for m in marks if m.get("method") != METHOD_UNCLAIMED]
    while sheet["marks"] and len(_canon(sheet)) > max_bytes:
        cut = max(1, len(sheet["marks"]) // 10)
        sheet["marks"] = sheet["marks"][:-cut]
    return sheet


def _first(values: Iterable[Any]) -> Any:
    for v in values:
        return v
    return None


def _rows_index(categories: Sequence[Mapping[str, Any]], run_id: str, notes: list[str]) -> dict[str, dict[str, Any]]:
    """{row id: row} of the stored category outputs, with the ids the main
    session used. A BOQ line cites the session's ids: when two stored
    outputs were merged from 1 (every category of the 7172 run), the
    session kept the first row of an id and re-numbered the other under a
    new id (plans_reader_consolidator.index_sources, `original_row_id`), so
    a line may cite C63 for a row stored as C1. The same index, built from
    the outputs in the order the live loader hands them to the session
    (category vocabulary rank), resolves those citations here; without a
    collision it is exactly the plain index. Malformed rows degrade to the
    legacy placement, never fail the build."""
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    ordered = sorted(
        (c for c in categories if isinstance(c, Mapping)),
        key=lambda c: (rank.get(str(c.get("category") or ""), len(rank)), str(c.get("category") or "")),
    )
    try:
        from app.services.plans_reader_consolidator import index_sources

        rows, aliases = index_sources(list(ordered), None)
        if aliases:
            notes.append(f"{len(aliases)} category rows re-numbered after row id collisions (session ids kept)")
        return rows
    except Exception as exc:  # noqa: BLE001 - malformed rows degrade to legacy placement
        notes.append(f"category rows unreadable: {type(exc).__name__}")
        logger.warning("evidence: rows for run %s unreadable: %s", run_id, exc)
        return {}


def build(
    sb: Any,
    run: Any,
    *,
    main_version: int | None,
    boq_version: int | None,
    inputs: RunInputs | None = None,
    sheets: EvidenceInputs | None = None,
    writer: EvidenceWriter | None = None,
    heartbeat: Callable[[], None] | None = None,
    force: bool = False,
    max_sheet_bytes: int = MAX_SHEET_BYTES,
) -> EvidenceOutput:
    """Evidence for one (main_version, boq_version) of a run (module
    docstring). Missing inputs degrade methods and chips; the build never
    raises for them. With a client (or a writer) the result is upserted as
    the outputs row of scope 'evidence'; an identical stored build (same
    inputs_hash) is returned as is unless `force`."""
    run_id = _run_id(run) or ""
    beat = heartbeat or (lambda: None)
    run_inputs: RunInputs = inputs if inputs is not None else SupabaseRunInputs(sb, run_id)
    sheet_inputs: EvidenceInputs = sheets if sheets is not None else inputs_mod.inputs_for(sb)
    if writer is None and sb is not None:
        writer = SupabaseEvidenceWriter()

    main_v, main = run_inputs.main_output(main_version)
    boq_v, boq = run_inputs.boq_spec(boq_version, main)
    documents = run_inputs.documents()
    categories = run_inputs.category_outputs()
    measure_raw = run_inputs.measure_output()
    marked = run_inputs.model_marks(main)
    lines = list(iter_boq_lines(boq or {}))
    notes: list[str] = []
    if boq is None:
        notes.append("no BOQ spec for this version")
    h = inputs_hash(lines=lines, categories=categories, measure=measure_raw, marked=marked, documents=documents,
                    source=getattr(sheet_inputs, "source", "unknown"))
    if writer is not None and not force:
        stored = writer.find(sb, run_id, main_v, boq_v)
        if stored is not None and isinstance(stored.get("output"), Mapping) and stored["output"].get("inputs_hash") == h:
            return EvidenceOutput.from_dict(stored["output"], reused=True)

    rows_by_id = _rows_index(categories, run_id, notes)
    measure = MeasureView(measure_raw)
    index = SheetIndex(documents, categories, marked)
    cache = _SheetCache(sheet_inputs)

    ea_lines = [ln for ln in lines if str(ln.get("unit") or "").upper() == "EA" and not ln.get("is_accessory")]
    legacy_lines = [ln for ln in ea_lines if not _source_refs(ln)]
    model_legend = _match_model_legend(legacy_lines, marked, index) if legacy_lines else {}

    demands: list[_Demand] = []
    for ln in ea_lines:
        demands.extend(_demands_for_line(ln, rows_by_id, model_legend, index, cache))
    by_sheet: dict[tuple[str, int], list[_Demand]] = {}
    for d in demands:
        by_sheet.setdefault((d.document_id, d.page), []).append(d)

    placements: list[SheetPlacement] = []
    sheet_order = sorted(by_sheet, key=lambda k: (index.doc_numbers.get(k[0], 10**6), k[0], k[1]))
    for (did, page) in sheet_order:
        beat()
        sheet = cache.get(did, page)
        ref = index.ref(did, page, sheet)
        ds = sorted(by_sheet[(did, page)], key=lambda d: d.line_id)
        placements.append(place_sheet(ref, sheet, ds, unavailable_note=cache.errors.get((did, page))))
    # Document priors: a sheet without tag-aware pairs of its own is placed
    # again through the best accepted registration of another sheet of the
    # same document and page size (most pairs, then lowest residual).
    for k, pl in enumerate(placements):
        if pl.registration.ok or pl.registration.reason not in PRIOR_REASONS:
            continue
        did, page = sheet_order[k]
        ds = sorted(by_sheet[(did, page)], key=lambda d: d.line_id)
        if not any(d.points for d in ds):
            continue
        donors = [
            o for o in placements
            if o is not pl and o.ref.document_id == did and o.registration.ok and not o.registration.prior_from
            and o.registration.pairs >= REG_MIN_PAIRS
            and abs(o.ref.w_in - pl.ref.w_in) < 0.05 and abs(o.ref.h_in - pl.ref.h_in) < 0.05
        ]
        if not donors:
            continue
        donor = min(donors, key=lambda o: (-o.registration.pairs, o.registration.median_residual_in or 0.0, o.ref.page))
        beat()
        placements[k] = place_sheet(pl.ref, cache.get(did, page), ds, unavailable_note=cache.errors.get((did, page)),
                                    prior=donor.registration.borrowed(donor.ref.sheet_key))

    ft_lines = [ln for ln in lines if str(ln.get("unit") or "").upper() == "FT" and not ln.get("is_accessory")]
    polys_by_line: dict[str, list[_Poly]] = {}
    poly_summary: dict[str, dict[str, Any]] = {}
    for ln in ft_lines:
        beat()
        polys, summary = line_polylines(ln, measure, rows_by_id, index)
        polys_by_line[str(ln["line_id"])] = polys
        poly_summary[str(ln["line_id"])] = summary

    # Sheets: marks and polylines with one legend per sheet.
    line_by_id = {str(ln["line_id"]): ln for ln in lines}
    sheet_dicts: dict[tuple[str, int], dict[str, Any]] = {}
    legend_index: dict[tuple[str, int], dict[str, int]] = {}
    marks_per_line: dict[str, dict[str, int]] = {}
    sheets_per_line: dict[str, list[str]] = {}
    mark_seq = 0
    poly_seq = 0
    total_marks = 0
    total_polys = 0
    truncated = False

    def sheet_dict(did: str, page: int, placement: SheetPlacement | None) -> dict[str, Any]:
        key = (did, page)
        if key not in sheet_dicts:
            ref = placement.ref if placement else index.ref(did, page, cache.get(did, page) if cache.inputs else None)
            sheet_dicts[key] = {
                "sheet_key": ref.sheet_key,
                "document_id": did,
                "doc_index": ref.doc_index,
                "page": page,
                "sheet_number": ref.sheet_number,
                "w_in": ref.w_in,
                "h_in": ref.h_in,
                "scale": {"ft_per_in": placement.ft_per_in if placement else None},
                "anchor_method": placement.method if placement else None,
                "text_layer_quality": placement.text_layer_quality if placement else None,
                "anchor_source": getattr(sheet_inputs, "source", None),
                "anchor_counts": dict(placement.anchor_counts) if placement else {},
                "registration": placement.registration.to_dict() if placement else None,
                "stacked_merged": placement.stacked_merged if placement else 0,
                "legend": [],
                "marks": [],
                "notes": list(placement.notes) if placement else [],
            }
            legend_index[key] = {}
        return sheet_dicts[key]

    def legend_row(key: tuple[str, int], lid: str | None, tag: str | None, sheet: dict[str, Any], **row: Any) -> int:
        idx_key = lid if lid is not None else f"unclaimed:{tag}"
        found = legend_index[key].get(idx_key)
        if found is not None:
            return found
        sheet["legend"].append(row)
        legend_index[key][idx_key] = len(sheet["legend"]) - 1
        return legend_index[key][idx_key]

    for pl in placements:
        key = (pl.ref.document_id, pl.ref.page)
        s = sheet_dict(pl.ref.document_id, pl.ref.page, pl)
        for m in pl.marks:
            if total_marks >= MAX_MARKS:
                truncated = True
                break
            if m.line_id is not None:
                ln = line_by_id.get(m.line_id, {})
                li = legend_row(
                    key, m.line_id, None, s,
                    prefix=legend_prefix(ln, line_tag(ln)),
                    description=str(ln.get("description") or ""),
                    quantity=pl.need.get(m.line_id, 0),
                    unit="EA",
                    line_id=m.line_id,
                    marked=0,
                    not_located=pl.not_located.get(m.line_id, 0),
                )
                s["legend"][li]["marked"] += 1
                per = marks_per_line.setdefault(m.line_id, {})
                per[m.method] = per.get(m.method, 0) + 1
            else:
                li = legend_row(
                    key, None, m.tag, s,
                    prefix=f"NC, {m.tag}",
                    description=f"Tag {m.tag} not counted",
                    quantity=0,
                    unit="EA",
                    line_id=None,
                    marked=0,
                    not_located=0,
                )
                s["legend"][li]["quantity"] += 1
                s["legend"][li]["marked"] += 1
            mark_seq += 1
            total_marks += 1
            s["marks"].append(m.to_dict(f"K{mark_seq}", li))
        for lid, need in pl.need.items():
            sheets_per_line.setdefault(lid, [])
            if pl.ref.sheet_key not in sheets_per_line[lid]:
                sheets_per_line[lid].append(pl.ref.sheet_key)
            if lid not in legend_index[key] and need > 0:
                ln = line_by_id.get(lid, {})
                legend_row(
                    key, lid, None, s,
                    prefix=legend_prefix(ln, line_tag(ln)),
                    description=str(ln.get("description") or ""),
                    quantity=need,
                    unit="EA",
                    line_id=lid,
                    marked=0,
                    not_located=pl.not_located.get(lid, need),
                )

    for lid, polys in polys_by_line.items():
        ln = line_by_id.get(lid, {})
        for p in polys:
            if total_polys >= MAX_POLYLINES:
                truncated = True
                break
            key = (p.document_id, p.page)
            s = sheet_dict(p.document_id, p.page, None)
            li = legend_row(
                key, lid, None, s,
                prefix=legend_prefix(ln, None),
                description=str(ln.get("description") or ""),
                quantity=0.0,
                unit="FT",
                line_id=lid,
                marked=0,
                not_located=0,
            )
            s["legend"][li]["quantity"] = round(float(s["legend"][li]["quantity"]) + (p.length_ft or 0.0), 2)
            s["legend"][li]["marked"] += 1
            poly_seq += 1
            total_polys += 1
            s["marks"].append(p.to_dict(f"P{poly_seq}", li))
            sheets_per_line.setdefault(lid, [])
            if s["sheet_key"] not in sheets_per_line[lid]:
                sheets_per_line[lid].append(s["sheet_key"])

    # Lines with chips.
    not_located_by_line: dict[str, int] = {}
    need_by_line: dict[str, int] = {}
    for pl in placements:
        for lid, k in pl.not_located.items():
            not_located_by_line[lid] = not_located_by_line.get(lid, 0) + k
        for lid, n in pl.need.items():
            need_by_line[lid] = need_by_line.get(lid, 0) + n
    line_dicts = []
    for ln in lines:
        lid = str(ln["line_id"])
        unit = str(ln.get("unit") or "").upper()
        per = marks_per_line.get(lid, {})
        marked = sum(per.values())
        qty = _num(ln.get("quantity")) or 0.0
        nl = not_located_by_line.get(lid)
        if unit == "EA" and nl is None and not ln.get("is_accessory"):
            nl = int(round(qty))  # no sheet resolved: nothing located
        chip = quantity_chip(
            ln,
            marks=marked,
            not_located=nl or 0,
            poly=poly_summary.get(lid),
            rows_by_id=rows_by_id,
            measure=measure,
        )
        line_dicts.append(
            {
                "line_id": lid,
                "description": str(ln.get("description") or ""),
                "unit": unit,
                "quantity": ln.get("quantity"),
                "group": ln.get("group"),
                "section": ln.get("section"),
                "area": ln.get("area"),
                "is_accessory": bool(ln.get("is_accessory")),
                "tag": line_tag(ln) if unit == "EA" else None,
                "sheets": sheets_per_line.get(lid, []),
                "sheet_refs": list(ln.get("sheet_refs") or []),
                "source_refs": _source_refs(ln),
                "marks": marked,
                "marks_by_method": dict(sorted(per.items())),
                "not_located": nl if unit == "EA" else None,
                "need_on_sheets": need_by_line.get(lid) if unit == "EA" else None,
                "polylines": len(polys_by_line.get(lid, [])) if unit == "FT" else None,
                "polyline_ft": poly_summary.get(lid, {}).get("drawn_ft") if unit == "FT" else None,
                "length_check": poly_summary.get(lid) if unit == "FT" else None,
                "transient_id": bool(ln.get("transient_id")),
                "qty_chip": chip.to_dict(),
            }
        )

    sheet_list = []
    for key in sorted(sheet_dicts, key=lambda k: (index.doc_numbers.get(k[0], 10**6), k[0], k[1])):
        s = _cap_sheet(sheet_dicts[key], max_sheet_bytes)
        truncated = truncated or bool(s.get("truncated"))
        sheet_list.append(s)

    by_method: dict[str, int] = {}
    unclaimed = 0
    for s in sheet_list:
        for m in s["marks"]:
            if m.get("kind") != "dot":
                continue
            if m.get("method") == METHOD_UNCLAIMED:
                unclaimed += 1
            else:
                by_method[m["method"]] = by_method.get(m["method"], 0) + 1
    placed = sum(by_method.values())
    metric_base = placed - sum(by_method.get(k, 0) for k in METRIC_EXCLUDED_METHODS)
    ea_qty = sum(int(round(_num(ln.get("quantity")) or 0)) for ln in ea_lines)
    sourced = [d for d in line_dicts if d["qty_chip"]["kind"] not in ("unsourced", "unit_error")]
    ft_with_poly = [d for d in line_dicts if d["unit"] == "FT" and not d["is_accessory"] and (d["polylines"] or 0) > 0]
    stats = {
        "lines": len(line_dicts),
        "lines_with_qty_chip": sum(1 for d in line_dicts if d.get("qty_chip")),
        "ea_lines": len(ea_lines),
        "ea_quantity": ea_qty,
        "ea_marked": placed,
        "density": round(placed / ea_qty, 4) if ea_qty else None,
        "marks_by_method": dict(sorted(by_method.items())),
        "anchor_share": round(by_method.get(METHOD_ANCHOR, 0) / metric_base, 4) if metric_base > 0 else None,
        "assigned_marks": by_method.get(METHOD_ASSIGNED, 0),
        "approx_marks": by_method.get(METHOD_APPROX, 0),
        "unclaimed_anchors": unclaimed,
        "stacked_merged": sum(pl.stacked_merged for pl in placements),
        "not_located": sum(not_located_by_line.values()),
        "ft_lines": len(ft_lines),
        "ft_lines_with_polyline": len(ft_with_poly),
        "length_mismatch": sum(1 for s_ in poly_summary.values() if s_.get("ok") is False),
        "polylines": total_polys,
        "provenance_completeness": round(len(sourced) / len(line_dicts), 4) if line_dicts else None,
        "measure_available": measure.available,
        "sheets": len(sheet_list),
        "truncated": truncated,
    }
    if not measure.available and ft_lines:
        notes.append("no measure output: FT lines have no polylines")
    out = EvidenceOutput(
        run_id=run_id or None,
        main_version=main_v,
        boq_version=boq_v,
        inputs_hash=h,
        built_at=now_pacific_iso(),
        sheets=sheet_list,
        lines=line_dicts,
        stats=stats,
        notes=notes,
    )
    if writer is not None and run_id:
        try:
            writer.write(sb, run_id, out.to_dict())
        except Exception as exc:  # noqa: BLE001 - the evidence still returns; storage is retried next build
            logger.warning("evidence: write for run %s failed: %s", run_id, exc)
            out.notes.append(f"not stored: {type(exc).__name__}")
    return out


# Renderer-only marked plans spec ----------------------------------------------------


def evidence_to_marked_spec(evidence: Mapping[str, Any] | EvidenceOutput) -> dict[str, Any]:
    """The marked-plans spec the renderer draws from evidence (renderer only:
    never validated against MARKED_PLANS_SPEC or sent to a model). Sheets
    with at least one mark or polyline, in document order. Legend rows carry
    the sheet quantity, marked and not-located counts, the chip text and the
    line id; marks carry kind dot or polyline, a style (solid, assigned,
    ring, unclaimed, dashed) and an optional label."""
    data = evidence.to_dict() if isinstance(evidence, EvidenceOutput) else dict(evidence)
    chips = {str(ln.get("line_id")): (ln.get("qty_chip") or {}).get("text") for ln in data.get("lines") or []}
    areas = {str(ln.get("line_id")): ln.get("area") for ln in data.get("lines") or []}
    sheets = []
    for s in data.get("sheets") or []:
        marks = [m for m in s.get("marks") or [] if m.get("points")]
        if not marks:
            continue
        legend = []
        for row in s.get("legend") or []:
            lid = row.get("line_id")
            legend.append(
                {
                    "prefix": row.get("prefix") or "",
                    "description": row.get("description") or "",
                    "quantity": row.get("quantity") or 0,
                    "unit": row.get("unit") or "EA",
                    "marked": row.get("marked") or 0,
                    "not_located": row.get("not_located") or 0,
                    "chip_text": chips.get(str(lid)) if lid else "Tag on the plan with no BOQ line",
                    "line_id": lid,
                }
            )
        area = _first(a for a in (areas.get(str(r.get("line_id"))) for r in s.get("legend") or []) if a) or ""
        sheets.append(
            {
                "area": str(area),
                "doc_index": s.get("doc_index"),
                "document_id": s.get("document_id"),
                "page": s.get("page"),
                "sheet_number": s.get("sheet_number"),
                "legend": legend,
                "marks": [
                    {
                        "legend_index": m.get("legend_index"),
                        "kind": "polyline" if m.get("kind") == "polyline" else "dot",
                        "style": m.get("style") or "solid",
                        "points": [list(p) for p in m.get("points") or []],
                        "label": m.get("label") if m.get("kind") == "polyline" else (
                            f"x{m['count']}" if (m.get("count") or 1) > 1 else None
                        ),
                    }
                    for m in marks
                ],
            }
        )
    return {"sheets": sheets}


# Replay stage ------------------------------------------------------------------------

REPLAY_STAGE_VERSION = EVIDENCE_CODE_VERSION
_DRAWING_CATEGORIES = ("electrical_drawings", "other_drawings", "drawings", "mechanical_drawings")


def _replay_measure(art: Mapping[str, Any]) -> dict[str, Any] | None:
    """The WS3 MeasureOutput of a replay: the derive stage's `measure`
    artifact (a dict, or an object with `to_dict()`), else the older
    `measurements` key."""
    for key in ("measure", "measurements"):
        value = art.get(key)
        if value is not None and not isinstance(value, Mapping) and callable(getattr(value, "to_dict", None)):
            value = value.to_dict()
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _replay_evidence(ctx: Any, art: dict[str, Any]) -> dict[str, Any]:
    """The replay stage "evidence": builds evidence from the bundle's
    artifacts at $0 with page facts extracted from the bundle's PDFs
    (`ctx.docs`; `ctx.sb is None`)."""
    documents = [d for d in art.get("documents") or [] if isinstance(d, Mapping)]
    categories = [v for k, v in art.items() if k.startswith("category:") and isinstance(v, Mapping)]
    pdfs: dict[str, bytes] = {}
    labels: dict[str, str] = {}
    for d in documents:
        did = str(d.get("id") or "")
        cat = str(d.get("category") or "")
        if did and (cat in _DRAWING_CATEGORIES or cat.endswith("drawings")) and ctx.docs.available(did):
            pdfs[did] = ctx.docs.get(did)
            labels[did] = cat
    sheet_inputs: EvidenceInputs = (
        inputs_mod.LocalPdfInputs(pdfs, labels=labels) if pdfs else inputs_mod.UnavailableEvidenceInputs()
    )
    main = art.get("main") if isinstance(art.get("main"), Mapping) else None
    versions = art.get("deliverable_versions") or {}
    fake = FakeRunInputs(
        documents=documents,
        main=main,
        main_version=1,
        boq=art.get("boq_spec") if isinstance(art.get("boq_spec"), Mapping) else None,
        boq_version=_int(versions.get("boq")) or 1,
        categories=categories,
        measure=_replay_measure(art),
        marked=art.get("marked_plans_spec") if isinstance(art.get("marked_plans_spec"), Mapping) else None,
    )
    out = build(None, ctx.bundle.run_id, main_version=1, boq_version=_int(versions.get("boq")) or 1,
                inputs=fake, sheets=sheet_inputs)
    return {"evidence": out.to_dict()}


def register_replay_stage() -> Any:
    """Register this module as the replay stage "evidence" (replaces the
    legacy pass-through). Called by the replay and eval entry points, not at
    import, so importing evidence never changes a replay that did not ask for
    it."""
    from app.services import plans_reader_replay

    return plans_reader_replay.register_stage(
        "evidence",
        _replay_evidence,
        version=REPLAY_STAGE_VERSION,
        reads=("documents", "main", "boq_spec", "category:*", "measure", "measurements", "marked_plans_spec"),
        writes=("evidence",),
        owner="WS8",
    )


__all__ = [
    "Chip",
    "EVIDENCE_SCHEMA",
    "EvidenceOutput",
    "EvidenceWriter",
    "FakeRunInputs",
    "MeasureView",
    "MemoryEvidenceWriter",
    "Registration",
    "RunInputs",
    "SheetIndex",
    "SupabaseEvidenceWriter",
    "SupabaseRunInputs",
    "attach_price_chips",
    "build",
    "centerline",
    "evidence_to_marked_spec",
    "fit_registration",
    "line_polylines",
    "money_keys",
    "place_sheet",
    "price_chip",
    "quantity_chip",
    "register_replay_stage",
]
