"""The deterministic review lane of the AI Estimating independent review
(docs/AI_ESTIMATING_BUILD_SPEC.md WS7 design, 4.4, 5.4 ReviewResult).

The lane runs at $0 on a priced estimate before any reviewer model call and
again after a revision (the recheck). It never changes a number: it turns
typed inputs into `ReviewFinding` rows that direct the reviewer, the revise
turn and the human verifier.

Inputs are typed, never re-derived here when the owning module exists:

- WS4 gate findings (`estimating_contracts.GateFinding`, stage pre, final or
  pricing) become findings of origin `gate`;
- WS5 scope decisions (the `ScopeReport` of `scope_engine.enforce`: an
  excluded or by-others decision whose line is still carried) become
  findings of origin `scope`; WS5 bid conditions feed R7;
- WS6 pricing: the `bdr.priced_estimate/1` artifact (plus the
  `PricedEstimate.categories` rows when given) for R8, R12 and the
  Verify-basis invariants; `price_lookup` through the PriceService Protocol;
- WS1 page facts (fixture types, panels with new circuits, sheet roles) and
  WS3 MeasureService (`synthesize_routes`) for the floor and coverage rules;
- WS7 history bands (`plans_reader_bands.BandCheck`) become origin `band`.

When `plans_reader_review_local_rules_fallback` is true AND a typed input is
absent (None, the module has not landed), the lane runs its own small copies
(`linear_in_ea` and `partial_basis` for WS4; the house-excluded topics and
by-others facts for WS5; a statute text search for R7). The flag is false in
code and dev.

WS7-unique rules (fixed severities, never calibrated):

- R1  new devices >= 20 and branch wire FT == 0: critical missing_scope.
- R3  linear item in EA, only for RUN, ROW, TAPE, CONTINUOUS (lighting
      context), a per-foot schedule or quote, or a WS4 linear-class row;
      heads, end caps and fixed lengths excluded; warning unless WS4
      `linear_in_ea` also fired on the line (then critical).
- R7  wage via WS5 bid conditions: priced wage type differs from the bid
      conditions, or a statute cue (NRS 338, Davis-Bacon, wage determination)
      with a non-prevailing wage: critical bid_condition; wage unknown:
      warning.
- R8  only allowances with an amount > 0 or a non-template name: warning
      when the name is a house-excluded topic or a WS5 exclusion, else info.
- R12 unit price outlier only on quote or manual priced lines: outside
      [p25 / 1.5, p75 x 1.5] of the library; warning with n >= 5 projects,
      info below.
- R13 duplicates: same normalized description and unit, overlapping sheet
      refs, different area or group: warning double_count.
- R14 false RFI: a question about a tag attribute that the text layer (WS1
      fixture types, or the page text) answers: info false_rfi.
- MQ1 measured-quantity floor: branch wire FT vs new circuits x a
      per-circuit floor (conductors x the existing tie-in length), and vs an
      independent code re-run of `synthesize_routes` totals; warning when the
      BOQ is more than 25% below.
- V1 to V5 Verify-basis invariants on the sent number: rate matches the wage
      type; the hour uplift is applied; tax on non-quoted material; every
      non-quoted material section carried; quote coverage when quotes exist.
- CV1 coverage: fixture types, panels with new work or equipment marks with
      no BOQ line; CV2 electrical plan sheets with zero yield.

Pure: no database, no network, no model. Pricing data stays inside the
returned findings (impact_usd), which only pricing roles may read
(`ReviewFinding.to_dict(include_money=False)` strips it).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services import plans_reader_rows as rows_mod
from app.services.electrical_items import (
    LINEAR_CLASSES,
    is_linear,
    normalize_description,
    parse_item,
)
from app.services.estimating_contracts import (
    ACTIVE_FINDING_STATUSES,
    BOQ_LINE_PREFIX,
    GateFinding,
    is_row_id,
)
from app.services.plans_reader_bands import MONEY_METRICS, BandCheck, classify_row
from app.services.plans_reader_review_schemas import FINDING_KINDS, FINDING_SEVERITIES
from app.services.plans_reader_schemas import ESTIMATE_ALLOWANCES

RULES_VERSION = "ws7-rules-1"
REVIEW_RULES_SCHEMA = "bdr.review_rules/1"

FINDING_ORIGINS: tuple[str, ...] = ("rule", "band", "gate", "scope", "recount", "reviewer")
SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}
FINDING_ID_PREFIX = "RF"

# Impact threshold of the WS7 design: max($1,000, 1% of the total).
IMPACT_FLOOR_USD = 1000.0
IMPACT_TOTAL_SHARE = 0.01

R1_MIN_DEVICES = 20
# Measured-quantity floor: conductors per new branch circuit (hot, neutral,
# ground) times the existing tie-in length setting; warn below 75%.
FLOOR_CONDUCTORS_PER_CIRCUIT = 3
FLOOR_SHORTFALL = 0.25
R12_MIN_PROJECTS = 5
R12_SPREAD = 1.5
MONEY_TOLERANCE_USD = 1.0

# Gate ids -> finding kinds (WS4 GATE_IDS).
GATE_KIND: dict[str, str] = {
    "new_devices_no_branch": "missing_scope",
    "linear_in_ea": "unit_wrong",
    "unit_class": "unit_wrong",
    "uom_conflict_quote": "unit_wrong",
    "census_floor": "quantity_suspect",
    "partial_basis": "quantity_suspect",
    "arithmetic": "quantity_suspect",
    "branch_ratio": "quantity_suspect",
    "excluded_system": "excluded_scope_carried",
    "fact_conflict": "excluded_scope_carried",
    "double_consumption": "double_count",
    "wage_type_unknown": "bid_condition",
    "lost_sheets": "missing_scope",
    "orphan_rows": "missing_scope",
}
# A WS7 rule that subsumes a gate on the same lines (the gate becomes evidence).
GATE_SUBSUMED_BY: dict[str, str] = {"new_devices_no_branch": "R1", "linear_in_ea": "R3"}  # documentation

# WS5 decision actions that take a line out of G3 scope.
EXCLUDING_ACTIONS: frozenset[str] = frozenset(
    {"exclude", "excluded", "by_others", "remove", "removed", "remove_allowance", "nic", "not_used"}
)

# R3 vocabulary.
R3_KEYWORD_RE = re.compile(r"\b(RUNS?|ROWS?|TAPE|CONTINUOUS)\b")
R3_LIGHTING_RE = re.compile(
    r"\bLIGHT|\bLUMINAIRE|\bFIXTURE|\bLED\b|\bLAMP|\bTROFFER|\bDOWNLIGHT|UNDER[\s-]?CABINET|\bCOVE\b"
    r"|\bSTRIP\b|\bTRACK\b"
)
R3_EXCLUDE_RE = re.compile(
    r"\bHEADS?\b|\bEND\s*CAPS?\b|\b\d+(?:\.\d+)?\s*(?:FT|LF|FEET|FOOT)\b|\d+\s*'(?:\s*-?\s*\d+\s*\")?"
)
_LUMINAIRE_CLASSES = frozenset({"luminaire", "luminaire_linear", "lighting_track"})

# House-excluded topics: the spec 3.5 row E5 topic list (TEMP(ORARY) POWER,
# DISTRIBUTION or LIGHTING, HEPA, COORDINATION STUDY, COMMISSIONING, PATCH,
# FIRESTOP, BIM, TRAINING, PHASING, ANCILLARY) plus painting from the Q12
# cut, patch and paint rule. Bare words, so every WS5 seed exclusion topic
# (scope_policy_seed EX-TEMP-POWER, EX-CUT-PATCH-PAINT, EX-FIRESTOP-LABELS,
# EX-FIRESTOPPING, EX-BIM, EX-TRAINING, EX-COMMISSIONING, EX-PHASING-PREMIUM,
# EX-ANCILLARY) is covered; a test keeps the two in sync. Local WS5 copy, used
# only behind the fallback flag, and by R8 to grade a carried allowance.
HOUSE_EXCLUDED_RE = re.compile(
    r"\bTEMP(?:ORARY)?\.?\s+(?:POWER|DISTRIBUTION|LIGHTING|LIGHTS|HEPA)\b|\bHEPA\b|\bCOORDINATION\s+STUD(?:Y|IES)\b"
    r"|\bCOMMISSIONING\b|\bPATCH\w*|\bPAINT(?:ING)?\b|\bFIRESTOP\w*|\bFIRE[\s-]STOPPING\b"
    r"|\bBIM\b|\bTRAINING\b|\bPHASING\b|\bANCILLARY\b",
    re.IGNORECASE,
)
STATUTE_RE = re.compile(
    r"\bNRS\s*338\b|\bprevailing\s+wages?\b|\bDavis[\s-]Bacon\b|\bpublic\s+works?\b", re.IGNORECASE
)
STATUTE_CUES: frozenset[str] = frozenset({"nrs338", "davis_bacon", "wage_determination"})

# R14: question words -> WS1 fixture type attributes.
R14_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "control": ("control",),
    "voltage": ("volts",),
    "volts": ("volts",),
    "mounting": ("mounting",),
    "mount": ("mounting",),
    "description": ("description",),
    "type": ("description",),
}
_TAG_IN_TEXT_RE = re.compile(r"\b([A-Z]{1,3}\d{1,3}[A-Z]?)\b")

# Electrical plan sheet numbers (CV2): E, ED, EL, EP, ES prefixes.
_ELEC_SHEET_RE = re.compile(r"^E[DLPS]?\d")
_PANEL_NAME_RE_TMPL = r"(?<![A-Z0-9]){name}(?![A-Z0-9])"


# Findings ----------------------------------------------------------------------------


@dataclass
class ReviewFinding:
    """One finding of the review (rule, band, gate, scope, recount or
    reviewer origin). Severity is final: rule and band severities are fixed
    by code; reviewer severities are recomputed by code (WS7 reviewer)."""

    origin: str
    rule_id: str
    kind: str
    severity: str
    title: str
    detail: str = ""
    line_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    proposed_ops: list[dict[str, Any]] = field(default_factory=list)
    impact_usd: float | None = None
    impact_hours: float | None = None
    evidence_verified: bool = True
    disposition: str = "open"
    disposition_reason: str | None = None
    disposition_by: str | None = None
    post_revision: bool = False
    finding_id: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.origin not in FINDING_ORIGINS:
            raise ValueError(f"unknown finding origin {self.origin!r}")
        if self.kind not in FINDING_KINDS:
            raise ValueError(f"unknown finding kind {self.kind!r}")
        if self.severity not in FINDING_SEVERITIES:
            raise ValueError(f"unknown finding severity {self.severity!r}")
        self.title = _ascii(self.title)[:120]
        self.detail = _ascii(self.detail)[:600]
        self.line_ids = sorted({str(i) for i in self.line_ids if is_row_id(i, BOQ_LINE_PREFIX)},
                               key=_line_sort_key)
        if not self.fingerprint:
            self.fingerprint = finding_fingerprint(self.origin, self.rule_id, self.kind, self.line_ids,
                                                   self.title)
        if self.severity == "info" and self.disposition == "open":
            self.disposition = "info_only"

    @property
    def ref(self) -> str:
        return f"f:{self.finding_id}" if self.finding_id else ""

    def to_dict(self, *, include_money: bool = True) -> dict[str, Any]:
        out = {
            "finding_id": self.finding_id,
            "origin": self.origin,
            "rule_id": self.rule_id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "line_ids": list(self.line_ids),
            "evidence": [dict(e) for e in self.evidence],
            "evidence_verified": self.evidence_verified,
            "proposed_ops": [dict(o) for o in self.proposed_ops],
            "impact_usd": self.impact_usd,
            "impact_hours": self.impact_hours,
            "disposition": self.disposition,
            "disposition_reason": self.disposition_reason,
            "disposition_by": self.disposition_by,
            "post_revision": self.post_revision,
            "fingerprint": self.fingerprint,
        }
        if not include_money:
            out["impact_usd"] = None
            out["evidence"] = [_strip_money(e) for e in out["evidence"]]
            if self.kind == "price_outlier" and self.impact_usd is not None:
                # R12 unit material: the detail quotes the unit dollars and the
                # library band; the non-pricing audience gets the sentence
                # without the numbers (spec 5.5).
                out["detail"] = money_free_price_outlier_detail(self)
        return out

    def to_row(self, *, review_id: str, run_id: str) -> dict[str, Any]:
        """The `plans_reader_review_findings` row (null optional columns are
        omitted, spec 5.6 MC rule)."""
        row: dict[str, Any] = {
            "review_id": review_id,
            "run_id": run_id,
            "finding_id": self.finding_id,
            "origin": self.origin,
            "rule_id": self.rule_id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "line_ids": list(self.line_ids),
            "evidence": [dict(e) for e in self.evidence],
            "evidence_verified": self.evidence_verified,
            "proposed_ops": [dict(o) for o in self.proposed_ops],
            "disposition": self.disposition,
            "post_revision": self.post_revision,
            "fingerprint": self.fingerprint,
        }
        for key in ("impact_usd", "impact_hours", "disposition_reason", "disposition_by"):
            value = getattr(self, key)
            if value is not None:
                row[key] = value
        return row

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewFinding:
        return cls(
            origin=str(data["origin"]),
            rule_id=str(data.get("rule_id") or ""),
            kind=str(data["kind"]),
            severity=str(data["severity"]),
            title=str(data.get("title") or ""),
            detail=str(data.get("detail") or ""),
            line_ids=list(data.get("line_ids") or []),
            evidence=[dict(e) for e in data.get("evidence") or [] if isinstance(e, Mapping)],
            proposed_ops=[dict(o) for o in data.get("proposed_ops") or [] if isinstance(o, Mapping)],
            impact_usd=_f(data.get("impact_usd")),
            impact_hours=_f(data.get("impact_hours")),
            evidence_verified=bool(data.get("evidence_verified", True)),
            disposition=str(data.get("disposition") or "open"),
            disposition_reason=data.get("disposition_reason"),
            disposition_by=data.get("disposition_by"),
            post_revision=bool(data.get("post_revision")),
            finding_id=str(data.get("finding_id") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
        )


def finding_fingerprint(origin: str, rule_id: str, kind: str, line_ids: Iterable[str], title: str) -> str:
    payload = json.dumps(
        {"o": origin, "r": rule_id, "k": kind, "l": sorted(line_ids), "t": title},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _strip_money(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """The evidence item without money: money-looking keys dropped (top
    level and facts) and, when the facts are a band check of a money metric
    (`metric` in plans_reader_bands.MONEY_METRICS), its value and
    percentiles nulled (the shape stays; the dollars go)."""
    out = dict(evidence)
    facts = out.get("facts")
    if isinstance(facts, Mapping):
        kept = {k: v for k, v in facts.items() if not _MONEY_KEY_RE.search(str(k))}
        if kept.get("metric") in MONEY_METRICS:
            for key in BAND_MONEY_FACT_KEYS:
                if key in kept:
                    kept[key] = None
        out["facts"] = kept
    for key in list(out):
        if _MONEY_KEY_RE.search(str(key)):
            out.pop(key)
    return out


def money_free_price_outlier_detail(finding: ReviewFinding) -> str:
    """The R12 unit material sentence for a reader outside the pricing
    roles: which band it left and over how many projects, no dollars."""
    n = None
    for e in finding.evidence:
        facts = e.get("facts") if isinstance(e, Mapping) else None
        if isinstance(facts, Mapping) and facts.get("n_projects") is not None:
            n = facts.get("n_projects")
            break
    over = f" over {int(n)} projects" if isinstance(n, (int, float)) and not isinstance(n, bool) else ""
    return f"unit material outside the library band{over}; the dollar values are visible to pricing roles only"


_MONEY_KEY_RE = re.compile(r"(?i)(usd|price|cost|amount|dollar|rate)")
# The band check keys that carry the metric's value and percentiles (dollars
# when the metric is a money metric).
BAND_MONEY_FACT_KEYS: tuple[str, ...] = ("value", "p10", "p25", "p50", "p75", "p90")


def _ascii(text: Any) -> str:
    s = str(text or "")
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    return s.encode("ascii", "replace").decode("ascii")


def _line_sort_key(line_id: str) -> tuple[int, str]:
    try:
        return (int(str(line_id)[1:]), str(line_id))
    except ValueError:
        return (10**9, str(line_id))


def number_findings(findings: Sequence[ReviewFinding], *, start: int = 1) -> list[ReviewFinding]:
    """Sort by severity (critical first), origin, rule and lines, and assign
    `RF<n>` ids in that order. Deterministic for the same inputs."""
    origin_rank = {o: i for i, o in enumerate(FINDING_ORIGINS)}
    ordered = sorted(
        findings,
        key=lambda f: (-SEVERITY_RANK[f.severity], origin_rank.get(f.origin, 99), f.rule_id,
                       [_line_sort_key(i) for i in f.line_ids], f.title),
    )
    for i, f in enumerate(ordered, start=start):
        f.finding_id = f"{FINDING_ID_PREFIX}{i}"
    return ordered


def impact_threshold(total_usd: float | None) -> float:
    """max($1,000, 1% of the total) (WS7 design)."""
    total = _f(total_usd) or 0.0
    return max(IMPACT_FLOOR_USD, IMPACT_TOTAL_SHARE * total)


# Inputs ------------------------------------------------------------------------------


PageText = Callable[[str, int], "str | None"]


@dataclass
class ReviewInputs:
    """Everything the rule lane reads. Every field is optional; a None typed
    input means its module has not landed (the local fallback may run)."""

    run_id: str | None = None
    boq: Mapping[str, Any] | None = None
    priced: Mapping[str, Any] | None = None  # bdr.priced_estimate/1
    pricing_categories: Mapping[str, Any] | None = None  # PricedEstimate.categories
    gate_findings: Sequence[GateFinding] | None = None
    scope_report: Mapping[str, Any] | None = None
    bid_conditions: Any = None  # bid_conditions.BidConditions or its dict
    facts: Sequence[Any] = ()
    category_outputs: Sequence[Mapping[str, Any]] = ()
    questions: Sequence[Mapping[str, Any]] | None = None
    page_text: PageText | None = None
    page_facts: Sequence[Mapping[str, Any]] = ()  # WS1 doc facts
    circuits: int | None = None  # new branch circuits (else from page facts panels)
    equipment_marks: Sequence[str] = ()
    measure_service: Any = None  # plans_reader_tools.MeasureService
    price: Any = None  # plans_reader_tools.PriceService
    allowances: Sequence[Mapping[str, Any]] = ()
    per_foot_quote_line_ids: Sequence[str] = ()
    rate_table: Mapping[str, float] | None = None  # wage_type -> labor rate (policy row)
    band_checks: Sequence[BandCheck] = ()
    total_usd: float | None = None
    local_rules_fallback: bool | None = None
    existing_tie_in_ft: float | None = None

    def lines(self) -> list[dict[str, Any]]:
        if not isinstance(self.boq, Mapping):
            return []
        return list(rows_mod.iter_boq_lines(self.boq))

    def fallback_enabled(self) -> bool:
        if self.local_rules_fallback is not None:
            return bool(self.local_rules_fallback)
        try:
            from app.core.config import get_settings

            return bool(get_settings().plans_reader_review_local_rules_fallback)
        except Exception:  # noqa: BLE001 - settings unavailable reads as the code default
            return False

    def tie_in_ft(self) -> float:
        if self.existing_tie_in_ft is not None:
            return float(self.existing_tie_in_ft)
        try:
            from app.core.config import get_settings

            return float(get_settings().plans_reader_existing_tie_in_ft)
        except Exception:  # noqa: BLE001
            return 25.0


@dataclass
class RulesResult:
    findings: list[ReviewFinding]
    notes: list[str]
    stats: dict[str, Any]
    version: str = RULES_VERSION

    def to_dict(self, *, include_money: bool = True) -> dict[str, Any]:
        return {
            "schema": REVIEW_RULES_SCHEMA,
            "version": self.version,
            "findings": [f.to_dict(include_money=include_money) for f in self.findings],
            "notes": list(self.notes),
            "stats": dict(self.stats),
        }

    def by_rule(self, rule_id: str) -> list[ReviewFinding]:
        return [f for f in self.findings if f.rule_id == rule_id]


# Small helpers -------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _qty(line: Mapping[str, Any]) -> float:
    for key in ("qty_base", "quantity", "qty"):
        v = _f(line.get(key))
        if v is not None:
            return v
    return 0.0


def _up(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().upper()


def _line_text(line: Mapping[str, Any]) -> str:
    return _up(line.get("description"))


def _is_excluded_line(line: Mapping[str, Any]) -> bool:
    flags = {str(f).lower() for f in line.get("flags") or []}
    return bool(line.get("excluded")) or bool(flags & {"excluded", "by_others", "alternate"})


def _active_lines(inputs: ReviewInputs) -> list[dict[str, Any]]:
    return [ln for ln in inputs.lines() if not _is_excluded_line(ln)]


def is_new_device(line: Mapping[str, Any]) -> bool:
    """A new-work EA device (the bands classifier, plus fixtures under a
    LIGHTING FIXTURES group whose subgroup is FIXTURES)."""
    if _up(line.get("unit")) != "EA" or _qty(line) <= 0:
        return False
    if _up(line.get("section")) == "DEMOLITION":
        return False
    cls = classify_row(line.get("section"), line.get("group"), line.get("subgroup"),
                       line.get("description"), line.get("unit"))
    if cls == "device":
        return True
    return _up(line.get("group")).startswith("LIGHTING FIXTURES")


def branch_wire_ft(lines: Iterable[Mapping[str, Any]]) -> tuple[float, list[str]]:
    total, ids = 0.0, []
    for ln in lines:
        cls = classify_row(ln.get("section"), ln.get("group"), ln.get("subgroup"),
                           ln.get("description"), ln.get("unit"))
        if cls == "branch_wire":
            total += _qty(ln)
            ids.append(str(ln["line_id"]))
    return total, ids


def _gate_findings_active(inputs: ReviewInputs) -> list[GateFinding]:
    return [g for g in inputs.gate_findings or [] if g.status in ACTIVE_FINDING_STATUSES]


def _gate_lines(gates: Iterable[GateFinding], gate_id: str) -> set[str]:
    out: set[str] = set()
    for g in gates:
        if g.gate_id == gate_id:
            out.update(g.line_ids())
    return out


def _gate_evidence(g: GateFinding) -> dict[str, Any]:
    return {"type": "rule", "rule_id": f"gate:{g.gate_id}", "facts": {"stage": g.stage,
            "severity": g.severity, "message": _ascii(g.message)[:240], "fingerprint": g.fingerprint}}


def _rule_evidence(rule_id: str, **facts: Any) -> dict[str, Any]:
    return {"type": "rule", "rule_id": rule_id, "facts": facts}


# Local copies of WS4 and WS5 rules (fallback flag only) ---------------------------------


def local_gate_findings(lines: Sequence[Mapping[str, Any]], run_id: str | None = None) -> list[GateFinding]:
    """WS7 copies of two WS4 gates, used only behind the fallback flag while
    plans_reader_gates is absent: `linear_in_ea` (a LINEAR_CLASSES item, or
    the demolition of one, not in FT) and `partial_basis` (a caveat or
    material note that says the count is a tile or partial subtotal)."""
    from app.services.plans_reader_recount import PARTIAL_RE  # local: avoids an import cycle

    out: list[GateFinding] = []
    for ln in lines:
        unit = _up(ln.get("unit"))
        identity = parse_item(ln.get("description"), unit)
        lid = str(ln["line_id"])
        if is_linear(identity) and unit and unit != "FT":
            out.append(GateFinding(run_id=run_id, output_version=None, stage="final",
                                   gate_id="linear_in_ea", severity="block", route="human",
                                   target={"line_id": lid},
                                   message=f"{identity.item_class} in {unit} (local copy)"))
        text = f"{ln.get('caveat') or ''} {ln.get('material_note') or ''}"
        if PARTIAL_RE.search(text):
            out.append(GateFinding(run_id=run_id, output_version=None, stage="final",
                                   gate_id="partial_basis", severity="block", route="recount",
                                   target={"line_id": lid},
                                   message="partial evidence carried as a total (local copy)"))
    return out


def local_scope_findings(inputs: ReviewInputs, lines: Sequence[Mapping[str, Any]]) -> list[ReviewFinding]:
    """WS7 copies of the WS5 decisions (fallback flag only): house-excluded
    topics carried as lines, and lines whose system a verified by-others
    fact excludes."""
    out: list[ReviewFinding] = []
    for ln in lines:
        m = HOUSE_EXCLUDED_RE.search(_line_text(ln))
        if m:
            out.append(ReviewFinding(
                origin="scope", rule_id="L-HOUSE", kind="excluded_scope_carried", severity="warning",
                title=f"House-excluded topic carried: {m.group(0).upper()}",
                detail="G3 excludes this topic by default (local rule copy; WS5 policy absent).",
                line_ids=[ln["line_id"]], evidence=[_rule_evidence("L-HOUSE", match=m.group(0))],
            ))
    for fact in inputs.facts or ():
        excludes = getattr(fact, "excludes", False)
        system = _up(getattr(fact, "system", ""))
        if not excludes or len(system) < 3:
            continue
        hit = [ln["line_id"] for ln in lines if system in _line_text(ln)]
        if hit:
            out.append(ReviewFinding(
                origin="scope", rule_id="L-BYOTHERS", kind="excluded_scope_carried", severity="warning",
                title=f"{system.title()} carried although a verified fact puts it by others",
                detail=_ascii(getattr(fact, "statement", ""))[:300], line_ids=hit,
                evidence=[{"type": "rule", "rule_id": "L-BYOTHERS",
                           "facts": {"fact_id": getattr(fact, "id", None)}}],
            ))
    return out


# Gate and scope consumption ------------------------------------------------------------


def gate_review_findings(gates: Iterable[GateFinding], skip: set[tuple[str, str]]) -> list[ReviewFinding]:
    """Active WS4 findings as review findings (block -> critical, warn ->
    warning). `skip` holds (gate_id, line_id) pairs a WS7 rule already
    carries as evidence."""
    out: list[ReviewFinding] = []
    for g in gates:
        lines = g.line_ids()
        if lines and all((g.gate_id, lid) in skip for lid in lines):
            continue
        out.append(ReviewFinding(
            origin="gate", rule_id=g.gate_id, kind=GATE_KIND.get(g.gate_id, "other"),
            severity="critical" if g.severity == "block" else "warning",
            title=f"Integrity gate {g.gate_id}", detail=_ascii(g.message),
            line_ids=lines, evidence=[_gate_evidence(g)],
        ))
    return out


def decision_line_ids(d: Mapping[str, Any]) -> list[str]:
    """The BOQ line ids a WS5 decision names. Real WS5 decisions
    (`scope_engine._decide`) carry the line id only as the last segment of
    `key` (`rule:<rule_id>:<line_id>`, `fact:<fact_id>:<line_id>`) and put
    the description text in `subject`; `subject` and `line_ids` stay as
    fallbacks for hand-built reports."""
    ids: list[str] = []
    key = str(d.get("key") or "")
    if ":" in key:
        tail = key.rsplit(":", 1)[1]
        if is_row_id(tail, BOQ_LINE_PREFIX):
            ids.append(tail)
    subject = d.get("subject")
    for s in subject if isinstance(subject, list) else [subject]:
        if isinstance(s, str) and is_row_id(s, BOQ_LINE_PREFIX) and s not in ids:
            ids.append(s)
    for s in d.get("line_ids") or []:
        if isinstance(s, str) and is_row_id(s, BOQ_LINE_PREFIX) and s not in ids:
            ids.append(s)
    return ids


def scope_review_findings(report: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> list[ReviewFinding]:
    """WS5 ScopeReport decisions whose excluded line is still carried
    (critical excluded_scope_carried), and needs_review entries (info)."""
    active = {str(ln["line_id"]) for ln in lines}
    out: list[ReviewFinding] = []
    for d in report.get("decisions") or []:
        if not isinstance(d, Mapping):
            continue
        action = str(d.get("action") or "").lower()
        if action not in EXCLUDING_ACTIONS:
            continue
        carried = sorted({s for s in decision_line_ids(d) if s in active})
        if not carried:
            continue
        out.append(ReviewFinding(
            origin="scope", rule_id=str(d.get("rule_id") or d.get("key") or "scope"),
            kind="excluded_scope_carried", severity="critical",
            title=f"Scope decision {action} not applied", detail=_ascii(d.get("clarification") or ""),
            line_ids=carried, evidence=[_rule_evidence(f"p:{d.get('rule_id')}", key=d.get("key"),
                                                       action=action)],
        ))
    for item in report.get("needs_review") or []:
        out.append(ReviewFinding(
            origin="scope", rule_id="needs_review", kind="bid_condition" if "wage" in str(item) else "other",
            severity="info", title=f"Scope needs review: {_ascii(item)[:80]}",
            evidence=[_rule_evidence("needs_review", item=str(item))],
        ))
    return out


# WS7 rules -----------------------------------------------------------------------------


def rule_r1(lines: Sequence[Mapping[str, Any]], gates: Sequence[GateFinding]) -> list[ReviewFinding]:
    devices = [ln for ln in lines if is_new_device(ln)]
    n_dev = sum(_qty(ln) for ln in devices)
    wire_ft, _ids = branch_wire_ft(lines)
    if n_dev < R1_MIN_DEVICES or wire_ft > 0:
        return []
    evidence = [_rule_evidence("R1", new_devices=n_dev, branch_wire_ft=wire_ft)]
    evidence += [_gate_evidence(g) for g in gates if g.gate_id == "new_devices_no_branch"]
    return [ReviewFinding(
        origin="rule", rule_id="R1", kind="missing_scope", severity="critical",
        title=f"{n_dev:g} new devices and no branch wire",
        detail="New devices need branch circuits; the BOQ carries no #12 or #10 branch conductor FT.",
        evidence=evidence,
    )]


def _per_foot_lines(lines: Sequence[Mapping[str, Any]], inputs: ReviewInputs) -> dict[str, str]:
    """Line id -> reason for lines a schedule or quote prices per foot: a WS1
    fixture type with linear_hint whose tag starts the description, or a line
    id the WS6 quote reconciliation lists as priced per foot."""
    out = {str(i): "quote priced per foot" for i in inputs.per_foot_quote_line_ids or ()}
    linear_tags = set()
    for doc in inputs.page_facts or ():
        for ft in (doc.get("fixture_types") or []) if isinstance(doc, Mapping) else []:
            if isinstance(ft, Mapping) and ft.get("linear_hint") and ft.get("tag"):
                linear_tags.add(_up(ft["tag"]))
    for ln in lines:
        m = re.match(r"^(?:TYPE\s+)?([A-Z]{1,3}\d{1,3}[A-Z]?)\b", _line_text(ln))
        if m and m.group(1) in linear_tags:
            out.setdefault(str(ln["line_id"]), f"fixture schedule {m.group(1)} per foot")
    return out


def r3_trigger(line: Mapping[str, Any], per_foot: Mapping[str, str]) -> str | None:
    """Why an EA line is a linear-in-EA candidate (R3), None when it is not.
    Heads, end caps and fixed lengths never are."""
    if _up(line.get("unit")) != "EA":
        return None
    text = _line_text(line)
    if R3_EXCLUDE_RE.search(text):
        return None
    lid = str(line.get("line_id"))
    if lid in per_foot:
        return per_foot[lid]
    identity = parse_item(line.get("description"), "EA")
    m = R3_KEYWORD_RE.search(text)
    lighting = identity.item_class in _LUMINAIRE_CLASSES or bool(R3_LIGHTING_RE.search(text)) or (
        identity.item_class == "demolition" and identity.attrs.get("subject") in _LUMINAIRE_CLASSES
    )
    if m and lighting:
        return f"{m.group(1)} in EA"
    if identity.item_class in LINEAR_CLASSES or is_linear(identity):
        return f"linear class {identity.attrs.get('subject') or identity.item_class}"
    return None


def rule_r3(lines: Sequence[Mapping[str, Any]], inputs: ReviewInputs,
            gates: Sequence[GateFinding]) -> list[ReviewFinding]:
    per_foot = _per_foot_lines(lines, inputs)
    # "Warning unless WS4 linear_in_ea also fired": only a blocking WS4
    # finding escalates. A warn-severity linear_in_ea is WS4 allowing EA for a
    # linear luminaire kind estimators rarely price in FT; it rides along as
    # evidence and the R3 finding stays a warning.
    gated = _gate_lines([g for g in gates if g.severity == "block"], "linear_in_ea")
    out: list[ReviewFinding] = []
    for ln in lines:
        why = r3_trigger(ln, per_foot)
        if why is None:
            continue
        lid = str(ln["line_id"])
        critical = lid in gated
        evidence = [_rule_evidence("R3", trigger=why, unit="EA", quantity=_qty(ln))]
        evidence += [_gate_evidence(g) for g in gates if g.gate_id == "linear_in_ea" and lid in g.line_ids()]
        out.append(ReviewFinding(
            origin="rule", rule_id="R3", kind="unit_wrong", severity="critical" if critical else "warning",
            title=f"Linear item in EA: {_ascii(ln.get('description'))[:70]}",
            detail=f"{why}; a linear item is carried in FT on evidence"
                   + ("; WS4 linear_in_ea also fired" if critical else ""),
            line_ids=[lid], evidence=evidence,
        ))
    return out


def _bc_get(bc: Any, key: str, default: Any = None) -> Any:
    if bc is None:
        return default
    if isinstance(bc, Mapping):
        return bc.get(key, default)
    return getattr(bc, key, default)


def rule_r7(inputs: ReviewInputs) -> list[ReviewFinding]:
    bc = inputs.bid_conditions
    priced_wage = (inputs.priced or {}).get("wage_type") if isinstance(inputs.priced, Mapping) else None
    out: list[ReviewFinding] = []
    if bc is None:
        if not (inputs.fallback_enabled() and inputs.page_text is not None):
            return []
        hits = _statute_hits(inputs)
        if hits and priced_wage != "prevailing_wage":
            doc, page, quote = hits[0]
            out.append(ReviewFinding(
                origin="rule", rule_id="R7", kind="bid_condition", severity="critical",
                title="Prevailing wage language with a non-prevailing price",
                detail="Statute text found in the documents (local rule copy; WS5 bid conditions absent).",
                evidence=[{"type": "page_quote", "document_id": doc, "page": page, "quote": quote}],
            ))
        return out
    wage = _bc_get(bc, "wage_type")
    cues = list(_bc_get(bc, "cues", ()) or ())
    statute = [c for c in cues if _bc_get(c, "cue") in STATUTE_CUES]
    evidence = [{"type": "page_quote", "document_id": _bc_get(c, "document_id"), "page": _bc_get(c, "page"),
                 "quote": _ascii(_bc_get(c, "quote", ""))[:200]} for c in statute[:3]]
    if priced_wage and wage and priced_wage != wage:
        out.append(ReviewFinding(
            origin="rule", rule_id="R7", kind="bid_condition", severity="critical",
            title=f"Priced as {priced_wage}; bid conditions say {wage}",
            detail=f"Wage basis {_bc_get(bc, 'wage_basis')}; the labor rate must follow the bid conditions.",
            evidence=evidence or [_rule_evidence("R7", priced=priced_wage, conditions=wage)],
        ))
    elif statute and (priced_wage or wage) != "prevailing_wage":
        out.append(ReviewFinding(
            origin="rule", rule_id="R7", kind="bid_condition", severity="critical",
            title="Statute cue with a non-prevailing wage",
            detail="NRS 338, Davis-Bacon or a wage determination is cited in the documents.",
            evidence=evidence,
        ))
    if "wage_unknown" in set(_bc_get(bc, "needs_review", ()) or ()):
        out.append(ReviewFinding(
            origin="rule", rule_id="R7", kind="bid_condition", severity="warning",
            title="Wage type unknown; priced on the policy default",
            evidence=[_rule_evidence("R7", needs_review="wage_unknown")],
        ))
    return out


def _statute_hits(inputs: ReviewInputs) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for doc in inputs.page_facts or ():
        if not isinstance(doc, Mapping):
            continue
        doc_id = str(doc.get("document_id") or "")
        for page in doc.get("pages") or []:
            pno = int(page.get("page") or 0) if isinstance(page, Mapping) else 0
            text = inputs.page_text(doc_id, pno) if inputs.page_text and doc_id and pno else None
            m = STATUTE_RE.search(text or "")
            if m:
                out.append((doc_id, pno, text[max(0, m.start() - 60): m.end() + 60]))
    return out


_TEMPLATE_ALLOWANCES = frozenset(_up(a) for a in ESTIMATE_ALLOWANCES)


def rule_r8(inputs: ReviewInputs) -> list[ReviewFinding]:
    allowances = list(inputs.allowances or ())
    if isinstance(inputs.priced, Mapping):
        allowances += [a for a in inputs.priced.get("allowances") or [] if isinstance(a, Mapping)]
    excluded_names = set()
    decisions = inputs.scope_report.get("decisions") if isinstance(inputs.scope_report, Mapping) else None
    for d in decisions or []:
        if isinstance(d, Mapping) and str(d.get("action") or "").lower() in EXCLUDING_ACTIONS:
            excluded_names.add(_up(d.get("subject") if isinstance(d.get("subject"), str) else ""))
    out: list[ReviewFinding] = []
    seen: set[str] = set()
    for a in allowances:
        name = _up(a.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        amount = _f(a.get("amount_usd", a.get("amount"))) or 0.0
        template = name in _TEMPLATE_ALLOWANCES
        if amount <= 0 and template:
            continue
        house = HOUSE_EXCLUDED_RE.search(name)
        excluded = house is not None or name in excluded_names
        out.append(ReviewFinding(
            origin="rule", rule_id="R8", kind="excluded_scope_carried" if excluded else "other",
            severity="warning" if excluded else "info",
            title=f"Allowance carried: {_ascii(name)[:80]}",
            detail=("house-excluded topic" if house else "excluded by the scope policy" if excluded
                    else "non-template name or a positive amount; confirm the document amount"),
            evidence=[_rule_evidence("R8", name=name, template=template, amount_usd=amount)],
            impact_usd=amount if amount > 0 else None,
        ))
    return out


def _lookup_val(stat: Any, *keys: str) -> float | None:
    if isinstance(stat, Mapping):
        for k in keys:
            v = _f(stat.get(k))
            if v is not None:
                return v
    return _f(stat)


def rule_r12(inputs: ReviewInputs) -> list[ReviewFinding]:
    priced = inputs.priced if isinstance(inputs.priced, Mapping) else None
    if priced is None or inputs.price is None:
        return []
    out: list[ReviewFinding] = []
    for ln in priced.get("lines") or []:
        if not isinstance(ln, Mapping) or not ln.get("priced"):
            continue
        manual = ln.get("hours_source") == "manual" or ln.get("material_source") == "manual"
        if not (manual or ln.get("material_source") == "vendor_quote"):
            continue
        try:
            res = inputs.price.lookup({"description": ln.get("description") or "", "uom": ln.get("unit") or "EA",
                                       "quantity": _f(ln.get("qty")), "family_hint": None, "spec_facts": []})
        except Exception:  # noqa: BLE001 - a lookup failure skips the line
            continue
        if not isinstance(res, Mapping) or res.get("status") != "ok":
            continue
        for label, unit_value, stat_key in (("unit hours", ln.get("unit_hours"), "labor_hours"),
                                            ("unit material", ln.get("unit_material_usd"), "material")):
            value = _f(unit_value)
            stat = res.get(stat_key)
            p25, p75 = _lookup_val(stat, "p25"), _lookup_val(stat, "p75")
            n = int(_lookup_val(stat, "n_projects", "n") or 0) if isinstance(stat, Mapping) else 0
            if value is None or p25 is None or p75 is None:
                continue
            low, high = p25 / R12_SPREAD, p75 * R12_SPREAD
            if low <= value <= high:
                continue
            p50 = _lookup_val(stat, "median", "p50", "value")
            qty = _f(ln.get("qty")) or 0.0
            impact = abs(value - p50) * qty if p50 is not None else None
            money = label == "unit material"
            # Material facts are keyed as money so `_strip_money` drops them
            # for a non-pricing reader; hours facts keep their plain keys.
            facts = ({"unit_material_usd": value, "p25_usd": p25, "p75_usd": p75, "n_projects": n} if money
                     else {"value": value, "p25": p25, "p75": p75, "n_projects": n})
            out.append(ReviewFinding(
                origin="rule", rule_id="R12", kind="price_outlier",
                severity="warning" if n >= R12_MIN_PROJECTS else "info",
                title=f"{label} outside the library band: {_ascii(ln.get('description'))[:60]}",
                detail=f"{label} {value:g} vs library [{low:g}, {high:g}] over {n} projects",
                line_ids=[ln.get("line_id")], impact_usd=impact if money else None,
                impact_hours=impact if label == "unit hours" else None,
                evidence=[_rule_evidence("R12", **facts)],
            ))
    return out


def _sheet_set(line: Mapping[str, Any]) -> set[str]:
    return {rows_mod.normalize_sheet_number(s) for s in line.get("sheet_refs") or [] if s}


def rule_r13(lines: Sequence[Mapping[str, Any]]) -> list[ReviewFinding]:
    out: list[ReviewFinding] = []
    by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for ln in lines:
        key = (normalize_description(ln.get("description")), _up(ln.get("unit")))
        if key[0]:
            by_key.setdefault(key, []).append(ln)
    for (desc, unit), group in by_key.items():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if (_up(a.get("area")), _up(a.get("group"))) == (_up(b.get("area")), _up(b.get("group"))):
                    continue
                overlap = _sheet_set(a) & _sheet_set(b)
                if not overlap:
                    continue
                out.append(ReviewFinding(
                    origin="rule", rule_id="R13", kind="double_count", severity="warning",
                    title=f"Possible duplicate: {_ascii(desc)[:70]} ({unit})",
                    detail=f"Same item on {', '.join(sorted(overlap))} in two areas or groups",
                    line_ids=[a["line_id"], b["line_id"]],
                    evidence=[_rule_evidence("R13", sheets=sorted(overlap))],
                ))
    return out


def _questions(inputs: ReviewInputs) -> list[Mapping[str, Any]]:
    if inputs.questions is not None:
        return [q for q in inputs.questions if isinstance(q, Mapping)]
    out: list[Mapping[str, Any]] = []
    for output in inputs.category_outputs or ():
        out += [q for q in output.get("questions") or [] if isinstance(q, Mapping)]
    return out


def rule_r14(inputs: ReviewInputs) -> list[ReviewFinding]:
    fixture_types: dict[str, Mapping[str, Any]] = {}
    for doc in inputs.page_facts or ():
        for ft in (doc.get("fixture_types") or []) if isinstance(doc, Mapping) else []:
            if isinstance(ft, Mapping) and ft.get("tag"):
                fixture_types.setdefault(_up(ft["tag"]), ft)
    out: list[ReviewFinding] = []
    for q in _questions(inputs):
        text = str(q.get("question") or "")
        words = {w.lower() for w in re.findall(r"[A-Za-z]+", text)}
        attrs = [a for w, keys in R14_ATTRIBUTES.items() if w in words for a in keys]
        if not attrs:
            continue
        for tag in dict.fromkeys(_TAG_IN_TEXT_RE.findall(text.upper())):
            answer = None
            ft = fixture_types.get(tag)
            if ft is not None:
                for a in attrs:
                    if ft.get(a) not in (None, ""):
                        answer = (a, str(ft.get(a)), ft.get("page"))
                        break
            if answer is None and inputs.page_text is not None and q.get("document_id") and q.get("page"):
                page_text = inputs.page_text(str(q["document_id"]), int(q["page"])) or ""
                for line in page_text.splitlines():
                    toks = line.upper().split()
                    if tag in toks and len(toks) >= 3:
                        answer = (attrs[0], " ".join(toks)[:120], q.get("page"))
                        break
            if answer is None:
                continue
            attr, value, page = answer
            out.append(ReviewFinding(
                origin="rule", rule_id="R14", kind="false_rfi", severity="info",
                title=f"RFI answered by the drawings: {tag} {attr} = {_ascii(value)[:40]}",
                detail=_ascii(text)[:300],
                evidence=[{"type": "rule", "rule_id": "R14",
                           "facts": {"tag": tag, "attribute": attr, "value": _ascii(value)[:120],
                                     "page": page, "sheet": q.get("sheet_number")}}],
            ))
            break
    return out


def new_circuits_from_facts(page_facts: Iterable[Mapping[str, Any]]) -> int | None:
    """New branch circuits from WS1 panel facts (`panels[].new_circuits`);
    None when no panel was parsed."""
    seen = False
    total = 0
    for doc in page_facts or ():
        for p in (doc.get("panels") or []) if isinstance(doc, Mapping) else []:
            if not isinstance(p, Mapping):
                continue
            seen = True
            total += len(p.get("new_circuits") or [])
    return total if seen else None


def _routes_total_ft(result: Any) -> float | None:
    if not isinstance(result, Mapping) or result.get("error"):
        return None
    totals = result.get("totals")
    if isinstance(totals, Mapping) and totals:
        s = 0.0
        found = False
        for v in totals.values():
            val = _lookup_val(v, "wire_ft", "length_ft", "ft") if isinstance(v, Mapping) else _f(v)
            if val is not None:
                s += val
                found = True
        if found:
            return s
    routes = result.get("routes")
    if isinstance(routes, list) and routes:
        vals = [_f(r.get("length_ft")) for r in routes if isinstance(r, Mapping)]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None
    return None


def rule_measured_floor(lines: Sequence[Mapping[str, Any]], inputs: ReviewInputs,
                        notes: list[str]) -> list[ReviewFinding]:
    wire_ft, ids = branch_wire_ft(lines)
    out: list[ReviewFinding] = []
    circuits = inputs.circuits if inputs.circuits is not None else new_circuits_from_facts(inputs.page_facts)
    if circuits:
        per_circuit = FLOOR_CONDUCTORS_PER_CIRCUIT * inputs.tie_in_ft()
        floor = circuits * per_circuit
        if wire_ft < (1.0 - FLOOR_SHORTFALL) * floor:
            out.append(ReviewFinding(
                origin="rule", rule_id="MQ1", kind="quantity_suspect", severity="warning",
                title=f"Branch wire {wire_ft:g} FT below the {circuits}-circuit floor",
                detail=f"{circuits} new circuits x {per_circuit:g} FT per circuit = {floor:g} FT; "
                       f"the BOQ is more than {int(FLOOR_SHORTFALL * 100)}% below",
                line_ids=ids,
                evidence=[_rule_evidence("MQ1", circuits=circuits, per_circuit_ft=per_circuit,
                                         floor_ft=floor, boq_ft=wire_ft)],
            ))
    else:
        notes.append("MQ1: no panel circuits parsed; circuit floor skipped")
    svc = inputs.measure_service
    if svc is not None:
        try:
            res = svc.synthesize_routes(document_id=None, page=None, circuits=None)
        except Exception:  # noqa: BLE001 - an unavailable service skips the check
            res = None
        total = _routes_total_ft(res)
        if total and wire_ft < (1.0 - FLOOR_SHORTFALL) * total:
            out.append(ReviewFinding(
                origin="rule", rule_id="MQ2", kind="quantity_suspect", severity="warning",
                title=f"Branch wire {wire_ft:g} FT below the route synthesis {total:g} FT",
                detail="An independent code re-run of synthesize_routes measures more than the BOQ carries",
                line_ids=ids, evidence=[_rule_evidence("MQ2", routes_ft=total, boq_ft=wire_ft)],
            ))
        elif total is None:
            notes.append("MQ2: synthesize_routes unavailable; route floor skipped")
    else:
        notes.append("MQ2: no MeasureService; route floor skipped")
    return out


# Verify-basis invariants -----------------------------------------------------------------


_SECTION_KEYS: tuple[str, ...] = ("materials", "gear", "underground", "low_voltage")


def rule_verify(inputs: ReviewInputs, notes: list[str]) -> list[ReviewFinding]:
    priced = inputs.priced if isinstance(inputs.priced, Mapping) else None
    if priced is None:
        notes.append("V: no priced estimate; Verify invariants skipped")
        return []
    totals = priced.get("totals") if isinstance(priced.get("totals"), Mapping) else {}
    verify = totals.get("verify") if isinstance(totals.get("verify"), Mapping) else None
    if verify is None:
        notes.append("V: priced estimate has no Verify draft")
        return []
    out: list[ReviewFinding] = []

    def add(rule_id: str, title: str, detail: str, kind: str = "other", **facts: Any) -> None:
        out.append(ReviewFinding(origin="rule", rule_id=rule_id, kind=kind, severity="critical",
                                 title=title, detail=detail, evidence=[_rule_evidence(rule_id, **facts)]))

    # V1 rate matches the wage type.
    wage = priced.get("wage_type")
    bc_wage = _bc_get(inputs.bid_conditions, "wage_type")
    rate = _f(priced.get("labor_rate_usd"))
    if bc_wage and wage and bc_wage != wage:
        add("V1", "Labor rate basis differs from the bid conditions",
            f"priced wage {wage}, bid conditions {bc_wage}", kind="bid_condition",
            priced_wage=wage, conditions_wage=bc_wage)
    elif inputs.rate_table and wage in inputs.rate_table and rate is not None:
        want = _f(inputs.rate_table[wage])
        if want is not None and abs(rate - want) > 0.005:
            add("V1", f"Labor rate does not match the {wage} rate table", "rate differs from the policy table",
                kind="bid_condition", labor_rate_usd=rate, expected_rate_usd=want)
    # V2 hour uplift applied and labor = hours x rate.
    hours_raw, hours = _f(totals.get("hours_raw")), _f(totals.get("hours"))
    if hours_raw and hours is not None and hours < hours_raw:
        add("V2", "Reviewer hours below the basis hours", "the hour uplift and round-up were not applied",
            hours_raw=hours_raw, hours=hours)
    labor = _f(verify.get("labor_amount"))
    if hours is not None and rate is not None and labor is not None:
        if abs(labor - round(hours * rate, 2)) > MONEY_TOLERANCE_USD:
            add("V2", "Labor amount is not hours x rate", "Verify labor differs from reviewer hours x rate",
                hours=hours, labor_rate_usd=rate, labor_usd=labor)
    # V3 and V4: tax on non-quoted material, every non-quoted section carried.
    cats = inputs.pricing_categories if isinstance(inputs.pricing_categories, Mapping) else None
    tax = (_f(totals.get("tax_rate_pct")) or 0.0) / 100.0
    if cats:
        by_section: dict[str, float] = {}
        for cat, row in cats.items():
            if not isinstance(row, Mapping):
                continue
            section = str(row.get("section") or "materials")
            pre = _f(row.get("pre_tax")) or 0.0
            total = _f(row.get("total"))
            quoted = str(row.get("source") or "").startswith("quote") or bool(row.get("quote_id"))
            if total is None:
                total = pre
            if not quoted and row.get("taxable", True) and not row.get("tax_included") and pre > 0 and tax > 0:
                if total + MONEY_TOLERANCE_USD < round(pre * (1 + tax), 2):
                    add("V3", f"Tax not applied on non-quoted material {cat}",
                        "non-quoted material must carry sales tax", category=str(cat), pre_tax_usd=pre,
                        total_usd=total, tax_rate_pct=tax * 100)
            by_section[section] = by_section.get(section, 0.0) + total
        for section, amount in by_section.items():
            carried = _f(verify.get(f"{section}_amount")) or 0.0
            if amount > MONEY_TOLERANCE_USD and carried + MONEY_TOLERANCE_USD < amount:
                add("V4", f"Material section {section} not fully carried to Verify",
                    "a priced material section must reach its Verify number", section=section,
                    section_usd=round(amount, 2), verify_usd=carried)
    else:
        lines = [ln for ln in priced.get("lines") or [] if isinstance(ln, Mapping) and ln.get("priced")]
        non_quoted = sum((_f(ln.get("qty")) or 0) * (_f(ln.get("unit_material_usd")) or 0)
                         for ln in lines if ln.get("material_source") in ("library", "policy", "manual"))
        material = _f(totals.get("material_usd")) or 0.0
        if non_quoted > 0 and material + MONEY_TOLERANCE_USD < round(non_quoted * (1 + tax), 2):
            add("V3", "Material below non-quoted material plus tax",
                "non-quoted material must carry tax and reach Verify", non_quoted_pretax_usd=round(non_quoted, 2),
                material_usd=material, tax_rate_pct=tax * 100)
    # V5 quote coverage when quotes exist.
    for q in priced.get("quotes") or []:
        if not isinstance(q, Mapping):
            continue
        amount = _f(q.get("amount_usd")) or 0.0
        if amount <= 0:
            continue
        taxed = amount if q.get("tax_included") else round(amount * (1 + tax), 2)
        sections = [s for s in q.get("replaces_sections") or [] if s in _SECTION_KEYS] or ["materials"]
        carried = sum(_f(verify.get(f"{s}_amount")) or 0.0 for s in sections)
        if carried + MONEY_TOLERANCE_USD < taxed:
            add("V5", f"Quote {q.get('category')} not covered by Verify",
                "the selected quote must be carried in its section", category=str(q.get("category")),
                quote_usd=taxed, verify_usd=carried)
    return out


# Coverage --------------------------------------------------------------------------------


def _mentions(lines: Sequence[Mapping[str, Any]], token: str, *, notes_too: bool = False) -> bool:
    pat = re.compile(_PANEL_NAME_RE_TMPL.format(name=re.escape(token.upper())))
    for ln in lines:
        text = _line_text(ln)
        if notes_too:
            text += " " + _up(ln.get("material_note")) + " " + _up(ln.get("caveat"))
        if pat.search(text):
            return True
    return False


def rule_coverage(lines: Sequence[Mapping[str, Any]], inputs: ReviewInputs, notes: list[str]) -> list[ReviewFinding]:
    out: list[ReviewFinding] = []
    all_lines = inputs.lines()
    for doc in inputs.page_facts or ():
        if not isinstance(doc, Mapping):
            continue
        for ft in doc.get("fixture_types") or []:
            tag = _up(ft.get("tag")) if isinstance(ft, Mapping) else ""
            if tag and not _mentions(all_lines, tag):
                out.append(ReviewFinding(
                    origin="rule", rule_id="CV1", kind="missing_scope", severity="warning",
                    title=f"Fixture type {tag} has no BOQ line",
                    detail=_ascii(ft.get("description"))[:200],
                    evidence=[_rule_evidence("CV1", tag=tag, schedule_page=ft.get("page"))],
                ))
        for p in doc.get("panels") or []:
            if not isinstance(p, Mapping) or not p.get("name"):
                continue
            new_work = bool(p.get("new_circuits")) or p.get("existing") is False
            if new_work and not _mentions(all_lines, str(p["name"]), notes_too=True):
                out.append(ReviewFinding(
                    origin="rule", rule_id="CV1", kind="missing_scope", severity="warning",
                    title=f"Panel {p['name']} has new work and no BOQ line",
                    evidence=[_rule_evidence("CV1", panel=p["name"], new_circuits=len(p.get("new_circuits") or []))],
                ))
    for mark in inputs.equipment_marks or ():
        if mark and not _mentions(all_lines, str(mark), notes_too=True):
            out.append(ReviewFinding(
                origin="rule", rule_id="CV1", kind="missing_scope", severity="warning",
                title=f"Equipment {mark} has no BOQ line", evidence=[_rule_evidence("CV1", equipment=mark)],
            ))
    # CV2 electrical plan sheets with zero yield.
    yielded: set[str] = set()
    for output in inputs.category_outputs or ():
        for key in ("counts", "runs"):
            for r in output.get(key) or []:
                if isinstance(r, Mapping) and r.get("sheet_number"):
                    yielded.add(rows_mod.normalize_sheet_number(r["sheet_number"]))
    for ln in all_lines:
        yielded |= _sheet_set(ln)
    plan_sheets = []
    for doc in inputs.page_facts or ():
        for page in (doc.get("pages") or []) if isinstance(doc, Mapping) else []:
            if not isinstance(page, Mapping):
                continue
            number = rows_mod.normalize_sheet_number(page.get("sheet_number") or "")
            if number and _ELEC_SHEET_RE.match(number) and page.get("page_role") == "plan":
                plan_sheets.append(number)
    if not plan_sheets:
        notes.append("CV2: no electrical plan sheets in page facts; zero-yield check skipped")
    for number in dict.fromkeys(plan_sheets):
        if number not in yielded:
            out.append(ReviewFinding(
                origin="rule", rule_id="CV2", kind="missing_scope", severity="warning",
                title=f"Electrical plan sheet {number} yielded nothing",
                evidence=[_rule_evidence("CV2", sheet=number)],
            ))
    return out


def band_review_findings(checks: Iterable[BandCheck]) -> list[ReviewFinding]:
    out: list[ReviewFinding] = []
    for c in checks:
        if c.severity not in ("warning", "critical"):
            continue
        out.append(ReviewFinding(
            origin="band", rule_id=f"band:{c.metric}", kind="band_outlier", severity=c.severity,
            title=f"{c.metric} {c.position} ({c.stratum}, n {c.n})", detail=c.reason,
            line_ids=list(c.line_ids),
            evidence=[{"type": "band", "rule_id": f"band:{c.metric}", "facts": c.to_dict()}],
        ))
    return out


# The lane --------------------------------------------------------------------------------


def run_rules(inputs: ReviewInputs) -> RulesResult:
    """The deterministic lane: consume WS4 gates and WS5 decisions, run the
    WS7 rules, number the findings. $0, pure over `inputs`."""
    notes: list[str] = []
    lines = _active_lines(inputs)
    fallback = inputs.fallback_enabled()
    if inputs.gate_findings is None and fallback:
        gates = local_gate_findings(lines, inputs.run_id)
        notes.append(f"WS4 gates absent: local copies produced {len(gates)} findings")
    else:
        gates = _gate_findings_active(inputs)
        if inputs.gate_findings is None:
            notes.append("WS4 gates absent and the local fallback is off")
    findings: list[ReviewFinding] = []
    r1 = rule_r1(lines, gates)
    r3 = rule_r3(lines, inputs, gates)
    findings += r1 + r3
    # A gate a WS7 rule already carries as evidence is not repeated.
    skip = {("linear_in_ea", lid) for f in r3 for lid in f.line_ids}
    consumed = [g for g in gates if not (r1 and g.gate_id == "new_devices_no_branch")]
    findings += gate_review_findings(consumed, skip)
    if isinstance(inputs.scope_report, Mapping):
        findings += scope_review_findings(inputs.scope_report, lines)
    elif fallback:
        findings += local_scope_findings(inputs, lines)
        notes.append("WS5 scope report absent: local rule copies ran")
    findings += rule_r7(inputs)
    findings += rule_r8(inputs)
    findings += rule_r12(inputs)
    findings += rule_r13(lines)
    findings += rule_r14(inputs)
    findings += rule_measured_floor(lines, inputs, notes)
    findings += rule_verify(inputs, notes)
    findings += rule_coverage(lines, inputs, notes)
    findings += band_review_findings(inputs.band_checks or ())
    unique: dict[str, ReviewFinding] = {}
    for f in findings:
        unique.setdefault(f.fingerprint, f)
    ordered = number_findings(list(unique.values()))
    stats = {
        "lines": len(lines),
        "findings": len(ordered),
        "by_severity": {s: sum(1 for f in ordered if f.severity == s) for s in FINDING_SEVERITIES},
        "by_rule": _count_by(ordered, lambda f: f.rule_id),
        "gates_consumed": len(gates),
        "local_fallback": bool(fallback),
    }
    return RulesResult(findings=ordered, notes=notes, stats=stats)


def _count_by(items: Iterable[ReviewFinding], key: Callable[[ReviewFinding], str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = key(it)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def gate_findings_from_report(report: Mapping[str, Any] | None, run_id: str | None = None) -> list[GateFinding] | None:
    """GateFindings from a `bdr.gate_report/1` artifact (replay), None when
    there is no report."""
    if not isinstance(report, Mapping):
        return None
    out: list[GateFinding] = []
    for check in report.get("checks") or []:
        if not isinstance(check, Mapping):
            continue
        rows = [r for r in check.get("rows") or [] if is_row_id(r, BOQ_LINE_PREFIX)]
        try:
            out.append(GateFinding(run_id=run_id, output_version=None, stage="final",
                                   gate_id=str(check.get("code")), severity=str(check.get("severity")),
                                   target={"row_ids": rows} if rows else {},
                                   message=str(check.get("detail") or "")))
        except ValueError:
            continue
    return out


# Replay stage "review" (rules lane rerun; model stages stored) -------------------------------


REPLAY_STAGE_VERSION = "ws7-review-rules-1"


def _replay_review(ctx: Any, art: dict[str, Any]) -> dict[str, Any]:
    boq = art.get("boq_spec") if isinstance(art.get("boq_spec"), Mapping) else None
    if boq is None:
        ctx.note("review: no BOQ spec; rules lane skipped")
        return {}
    cats = [art[k] for k in sorted(art) if k.startswith("category:") and isinstance(art[k], Mapping)]
    priced = art.get("priced_estimate") if isinstance(art.get("priced_estimate"), Mapping) else None
    scope = art.get("scope_report") if isinstance(art.get("scope_report"), Mapping) else None
    page_facts = art.get("page_facts") if isinstance(art.get("page_facts"), list) else []
    inputs = ReviewInputs(
        run_id=None, boq=boq, priced=priced,
        gate_findings=gate_findings_from_report(art.get("gate_report")),
        scope_report=scope, category_outputs=cats, page_facts=page_facts,
        bid_conditions=art.get("bid_conditions") if isinstance(art.get("bid_conditions"), Mapping) else None,
    )
    result = run_rules(inputs)
    out = result.to_dict()
    stored = art.get("review") if isinstance(art.get("review"), Mapping) else None
    if stored:
        model = [f for f in stored.get("findings") or [] if isinstance(f, Mapping) and f.get("origin") == "reviewer"]
        out["reviewer_findings_stored"] = model
        out["stored_verdict"] = stored.get("verdict")
    for n in result.notes:
        ctx.note(f"review: {n}")
    return {"review_rules": out}


def register_replay_stage() -> None:
    """Register the WS7 `review` replay stage (rules lane rerun at $0; the
    reviewer and revise model stages are carried from the bundle)."""
    from app.services.plans_reader_replay import register_stage

    register_stage("review", _replay_review, version=REPLAY_STAGE_VERSION,
                   reads=("boq_spec", "priced_estimate", "gate_report", "scope_report", "category:*", "review"),
                   writes=("review_rules",), llm=False, owner="WS7")


register_replay_stage()


__all__ = [
    "RULES_VERSION", "REVIEW_RULES_SCHEMA", "FINDING_ORIGINS", "ReviewFinding", "ReviewInputs", "RulesResult",
    "run_rules", "number_findings", "finding_fingerprint", "impact_threshold", "local_gate_findings",
    "local_scope_findings", "gate_review_findings", "scope_review_findings", "rule_r1", "rule_r3", "rule_r7",
    "rule_r8", "rule_r12", "rule_r13", "rule_r14", "rule_measured_floor", "rule_verify", "rule_coverage",
    "band_review_findings", "r3_trigger", "is_new_device", "branch_wire_ft", "new_circuits_from_facts",
    "gate_findings_from_report", "register_replay_stage", "R3_EXCLUDE_RE", "R3_KEYWORD_RE",
]
