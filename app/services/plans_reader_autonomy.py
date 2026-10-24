"""Autonomy status of an AI Estimating run (docs/AI_ESTIMATING_BUILD_SPEC.md
section 5.4 `AutonomyStatus`, section 1.4, WS8; persisted by migration 0165
as `plans_reader_runs.autonomy_state`).

`status(sb, run, *, include_pricing)` combines eight deterministic checks,
each read through an `AutonomySources` provider (Supabase or a fake):

| key | pass | warn | fail |
|---|---|---|---|
| reading_complete | no failed reading group | failed groups only outside drawings | a failed drawing group, a failed run, or an open lost_sheets finding |
| integrity_gates | gates ran, no active finding | only warn findings, or block findings all overridden | an active block finding (final or pricing stage) |
| lengths_produced | measure output present, every FT line drawn and in length | assumed routes over half of the drawn branch feet, FT lines without polylines, no measure output | an open new_devices_no_branch or linear_in_ea finding, a linear item in EA, or new devices without both branch conductor FT and branch conduit (or power cable) FT |
| scope_conditions | wage type decided from a cited basis | wage from the default, or open needs_review | wage unknown, or an open fact_conflict / excluded_system finding |
| pricing_coverage | priced version for the current BOQ, >= 80% of the extended value by quote or strong/good library | 60% to 80% | < 60%, a unit mismatch, a blocked or stale priced version |
| independent_review | WS7 verdict clear | advisory or stale | needs_human or incomplete |
| history_bands | no band outside p10 to p90 | an advisory band | a critical (total-level) band |
| evidence | provenance and mark density >= 0.90 and every FT line drawn | 0.75 to 0.90 | < 0.75 |

A check whose provider has nothing is `pending` while the run (or its
post-run chain) is active, else `skipped` with reason `provider_unavailable`.
`na` marks a check that does not apply (pricing with AI pricing off).

Overall: `not_evaluated` for legacy runs (gates never ran and no evidence);
`in_progress` while any check is pending; `blocked` when any fail is not
overridden; `needs_review` when a warn, a skipped check or an overridden fail
remains; `ready_for_verification` otherwise.

`include_pricing=False` (a caller outside the pricing roles) keeps every
state but drops the pricing check's params and the sign-off note: no money
and no price provenance leave this module for that audience.

`recompute(sb, run_id)` computes the status and persists the overall state;
`validate_signoff` and `signoff_row` are the pure rules of the sign-off
record (migration 0169, the route is wave 3).

Pure apart from the Supabase provider; no model, no network of its own.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.services.electrical_items import is_linear, parse_item
from app.services.estimating_contracts import ACTIVE_FINDING_STATUSES, WAGE_TYPES, GateFinding
from app.services.plans_reader_rows import iter_boq_lines

logger = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")

CHECK_KEYS: tuple[str, ...] = (
    "reading_complete",
    "integrity_gates",
    "lengths_produced",
    "scope_conditions",
    "pricing_coverage",
    "independent_review",
    "history_bands",
    "evidence",
)
CHECK_STATES: tuple[str, ...] = ("pass", "warn", "fail", "pending", "skipped", "na")
OVERALL_STATES: tuple[str, ...] = (
    "ready_for_verification",
    "needs_review",
    "blocked",
    "in_progress",
    "not_evaluated",
)
CHECK_TABS: dict[str, str] = {
    "reading_complete": "outputs",
    "integrity_gates": "checks",
    "lengths_produced": "evidence",
    "scope_conditions": "checks",
    "pricing_coverage": "pricing",
    "independent_review": "checks",
    "history_bands": "checks",
    "evidence": "evidence",
}
PROVIDER_UNAVAILABLE = "provider_unavailable"

TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"done", "done_with_errors", "failed", "cancelled"})
READING_GROUP_KINDS: frozenset[str] = frozenset({"sub", "rollup", "escalation", "recount", "derive", "main"})
DRAWING_CATEGORY_SUFFIX = "drawings"
LENGTH_GATES: frozenset[str] = frozenset({"new_devices_no_branch", "linear_in_ea"})
SCOPE_GATES: frozenset[str] = frozenset({"fact_conflict", "excluded_system"})
DEVICE_CLASSES: frozenset[str] = frozenset(
    {"receptacle", "switch", "control", "luminaire", "luminaire_linear", "equipment_connection"}
)
BRANCH_CLASSES: frozenset[str] = frozenset({"conductor", "conduit", "cable", "raceway"})
BRANCH_RACEWAY_CLASSES: frozenset[str] = frozenset({"conduit", "raceway"})
MIN_BRANCH_AWG = 14  # a cable with conductors of #14 or larger is power wiring

PRICING_PASS_SHARE = 0.80
PRICING_WARN_SHARE = 0.60
EVIDENCE_PASS = 0.90
EVIDENCE_WARN = 0.75
ASSUMED_SHARE_WARN = 0.50
DEFAULT_RATE_FOR_WEIGHTS = 100.0  # only weights the coverage share; never shown
SIGNOFF_DECISIONS: tuple[str, ...] = ("verified", "rejected")
SIGNOFF_NOTE_MIN = 20
REJECT_NOTE_MIN = 10


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(value: Any) -> int | None:
    f = _num(value)
    return int(f) if f is not None else None


def _run_id(run: Any) -> str:
    if isinstance(run, str):
        return run
    if isinstance(run, Mapping):
        return str(run.get("id") or "")
    return str(getattr(run, "id", "") or "")


def now_pacific_iso() -> str:
    return datetime.now(PACIFIC).isoformat(timespec="seconds")


def pacific(value: Any) -> str | None:
    """An ISO timestamp shown in America/Los_Angeles (None stays None)."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if ts.tzinfo is None:
        return ts.isoformat(timespec="seconds")
    return ts.astimezone(PACIFIC).isoformat(timespec="seconds")


# Status shapes ----------------------------------------------------------------------


@dataclass(frozen=True)
class AutonomyCheck:
    key: str
    state: str
    params: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    overridden: bool = False

    @property
    def summary_key(self) -> str:
        return f"aiEstimating.autonomy.check.{self.key}.{self.state}"

    @property
    def tab(self) -> str:
        return CHECK_TABS.get(self.key, "checks")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state,
            "summary_key": self.summary_key,
            "params": dict(self.params),
            "reasons": list(self.reasons),
            "tab": self.tab,
            "overridden": self.overridden,
        }


@dataclass
class AutonomyStatus:
    run_id: str | None
    overall: str
    checks: list[AutonomyCheck]
    boq_version: int | None = None
    pricing_version: int | None = None
    signoff: dict[str, Any] | None = None
    computed_at: str = ""
    include_pricing: bool = False

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.state == "pass")

    @property
    def total(self) -> int:
        return sum(1 for c in self.checks if c.state != "na")

    def check(self, key: str) -> AutonomyCheck:
        for c in self.checks:
            if c.key == key:
                return c
        raise KeyError(key)

    def failing(self) -> list[str]:
        """Checks a verifier must accept explicitly to sign `verified`."""
        return [c.key for c in self.checks if c.state in ("fail", "warn", "skipped", "pending")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "overall": self.overall,
            "passed": self.passed,
            "total": self.total,
            "boq_version": self.boq_version,
            "pricing_version": self.pricing_version if self.include_pricing else None,
            "checks": [c.to_dict() for c in self.checks],
            "signoff": dict(self.signoff) if self.signoff else None,
            "computed_at": self.computed_at,
        }


# Sources --------------------------------------------------------------------------


class AutonomySources(Protocol):
    """Everything the checks read. Each method returns None when its provider
    has nothing (table absent, nothing stored, module not landed)."""

    def run(self, run_id: str) -> dict[str, Any] | None: ...

    def groups(self, run_id: str) -> list[dict[str, Any]] | None: ...

    def gate_findings(self, run_id: str) -> list[GateFinding] | None: ...

    def boq(self, run_id: str) -> tuple[int | None, dict[str, Any]] | None: ...

    def measure(self, run_id: str) -> dict[str, Any] | None: ...

    def scope(self, run_id: str, run: Mapping[str, Any] | None) -> dict[str, Any] | None: ...

    def pricing(self, run_id: str) -> dict[str, Any] | None: ...

    def review(self, run_id: str) -> dict[str, Any] | None: ...

    def evidence(self, run_id: str) -> dict[str, Any] | None: ...

    def signoff(self, run_id: str) -> dict[str, Any] | None: ...


class FakeAutonomySources:
    """Canned data for tests (`None` = provider has nothing)."""

    def __init__(
        self,
        *,
        run: Mapping[str, Any] | None = None,
        groups: Sequence[Mapping[str, Any]] | None = None,
        gate_findings: Sequence[GateFinding] | None = None,
        boq: tuple[int | None, Mapping[str, Any]] | None = None,
        measure: Mapping[str, Any] | None = None,
        scope: Mapping[str, Any] | None = None,
        pricing: Mapping[str, Any] | None = None,
        review: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
        signoff: Mapping[str, Any] | None = None,
    ) -> None:
        self._run = dict(run) if run is not None else None
        self._groups = [dict(g) for g in groups] if groups is not None else None
        self._findings = list(gate_findings) if gate_findings is not None else None
        self._boq = (boq[0], dict(boq[1])) if boq is not None else None
        self._measure = dict(measure) if measure is not None else None
        self._scope = dict(scope) if scope is not None else None
        self._pricing = dict(pricing) if pricing is not None else None
        self._review = dict(review) if review is not None else None
        self._evidence = dict(evidence) if evidence is not None else None
        self._signoff = dict(signoff) if signoff is not None else None
        self.updates: list[tuple[str, str]] = []

    def run(self, run_id: str) -> dict[str, Any] | None:
        return dict(self._run) if self._run is not None else None

    def groups(self, run_id: str) -> list[dict[str, Any]] | None:
        return [dict(g) for g in self._groups] if self._groups is not None else None

    def gate_findings(self, run_id: str) -> list[GateFinding] | None:
        return list(self._findings) if self._findings is not None else None

    def boq(self, run_id: str) -> tuple[int | None, dict[str, Any]] | None:
        return self._boq

    def measure(self, run_id: str) -> dict[str, Any] | None:
        return self._measure

    def scope(self, run_id: str, run: Mapping[str, Any] | None) -> dict[str, Any] | None:
        return self._scope

    def pricing(self, run_id: str) -> dict[str, Any] | None:
        return self._pricing

    def review(self, run_id: str) -> dict[str, Any] | None:
        return self._review

    def evidence(self, run_id: str) -> dict[str, Any] | None:
        return self._evidence

    def signoff(self, run_id: str) -> dict[str, Any] | None:
        return self._signoff


class SupabaseAutonomySources:
    """The tables of migrations 0152 and 0164 to 0169 (sync client; call from
    plain def or a worker thread). A missing table or column answers None
    (logged), never an exception, so the status works before every
    migration is applied."""

    def __init__(self, sb: Any) -> None:
        self._sb = sb

    def _select(self, table: str, columns: str, **eq: Any) -> list[dict[str, Any]] | None:
        try:
            q = self._sb.table(table).select(columns)
            for k, v in eq.items():
                q = q.eq(k, v)
            return list(q.execute().data or [])
        except Exception as exc:  # noqa: BLE001 - provider unavailable, never a failure
            logger.info("autonomy: %s unavailable: %s", table, exc)
            return None

    @staticmethod
    def _latest(rows: list[dict[str, Any]] | None, key: str = "version") -> dict[str, Any] | None:
        if not rows:
            return None
        return max(rows, key=lambda r: (_int(r.get(key)) or 0, str(r.get("created_at") or "")))

    def run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._select("plans_reader_runs", "*", id=run_id)
        return rows[0] if rows else None

    def groups(self, run_id: str) -> list[dict[str, Any]] | None:
        return self._select("plans_reader_groups", "id, kind, status, category", run_id=run_id)

    def gate_findings(self, run_id: str) -> list[GateFinding] | None:
        rows = self._select("plans_reader_gate_findings", "*", run_id=run_id)
        if rows is None:
            return None
        out = []
        for r in rows:
            try:
                out.append(GateFinding.from_row(r))
            except Exception as exc:  # noqa: BLE001 - a malformed row is skipped, logged
                logger.warning("autonomy: gate finding row skipped: %s", exc)
        return out

    def boq(self, run_id: str) -> tuple[int | None, dict[str, Any]] | None:
        row = self._latest(self._select("plans_reader_deliverables", "version, spec_json", run_id=run_id, kind="boq"))
        if row is None or not isinstance(row.get("spec_json"), dict):
            return None
        return _int(row.get("version")), row["spec_json"]

    def _output(self, run_id: str, scope: str) -> dict[str, Any] | None:
        row = self._latest(self._select("plans_reader_outputs", "version, output", run_id=run_id, scope=scope))
        o = row.get("output") if row else None
        return o if isinstance(o, dict) else None

    def measure(self, run_id: str) -> dict[str, Any] | None:
        return self._output(run_id, "measure")

    def scope(self, run_id: str, run: Mapping[str, Any] | None) -> dict[str, Any] | None:
        bid = (run or {}).get("bid_conditions") if isinstance(run, Mapping) else None
        main = self._output(run_id, "main") or {}
        report = main.get("scope_policy") if isinstance(main.get("scope_policy"), Mapping) else None
        if not isinstance(bid, Mapping) and report is None:
            return None
        return {"bid_conditions": dict(bid) if isinstance(bid, Mapping) else None, "scope_report": report}

    def pricing(self, run_id: str) -> dict[str, Any] | None:
        row = self._latest(self._select("plans_reader_pricing", "*", run_id=run_id))
        if row is None:
            return None
        lines = self._select("plans_reader_priced_lines", "line_id, line", pricing_id=row.get("id")) or []
        out = dict(row)
        out["lines"] = [dict(r.get("line") or {}, line_id=r.get("line_id")) for r in lines]
        return out

    def review(self, run_id: str) -> dict[str, Any] | None:
        row = self._latest(self._select("plans_reader_reviews", "*", run_id=run_id), key="base_pricing_version")
        if row is None:
            return None
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        return {**result, "verdict": row.get("verdict") or result.get("verdict"),
                "base_pricing_version": row.get("base_pricing_version")}

    def evidence(self, run_id: str) -> dict[str, Any] | None:
        return self._output(run_id, "evidence")

    def signoff(self, run_id: str) -> dict[str, Any] | None:
        rows = self._select("plans_reader_price_signoffs", "*", run_id=run_id)
        if not rows:
            return None
        return max(rows, key=lambda r: str(r.get("created_at") or ""))


# Checks ---------------------------------------------------------------------------


def _missing(key: str, active: bool, reason: str = PROVIDER_UNAVAILABLE) -> AutonomyCheck:
    return AutonomyCheck(key, "pending" if active else "skipped", {}, (reason,))


def _active_findings(findings: Iterable[GateFinding]) -> list[GateFinding]:
    return [f for f in findings if f.status in ACTIVE_FINDING_STATUSES and f.stage in ("final", "pricing")]


def _reason(f: GateFinding) -> str:
    return f"gate:{f.gate_id}:{f.fingerprint[:8]}"


def check_reading(run: Mapping[str, Any], groups: list[dict[str, Any]] | None,
                  findings: list[GateFinding] | None, active: bool) -> AutonomyCheck:
    key = "reading_complete"
    if str(run.get("status") or "") == "failed":
        return AutonomyCheck(key, "fail", {}, ("run:failed",))
    lost = [f for f in findings or [] if f.gate_id == "lost_sheets" and f.status in ACTIVE_FINDING_STATUSES]
    if lost:
        return AutonomyCheck(key, "fail", {"lost": len(lost)}, tuple(_reason(f) for f in lost))
    if groups is None:
        return _missing(key, active)
    reading = [g for g in groups if str(g.get("kind") or "") in READING_GROUP_KINDS]
    failed = [g for g in reading if str(g.get("status") or "") == "failed"]
    drawing = [g for g in failed if str(g.get("category") or "").endswith(DRAWING_CATEGORY_SUFFIX)
               or str(g.get("kind") or "") == "main"]
    params = {"groups": len(reading), "failed": len(failed)}
    if drawing:
        return AutonomyCheck(key, "fail", params, tuple(f"group:failed:{g.get('id')}" for g in drawing))
    if failed:
        return AutonomyCheck(key, "warn", params, tuple(f"group:failed:{g.get('id')}" for g in failed))
    if active and str(run.get("status") or "") not in TERMINAL_RUN_STATUSES:
        return AutonomyCheck(key, "pending", params, ("run:active",))
    return AutonomyCheck(key, "pass", params)


def gates_evaluated(run: Mapping[str, Any], findings: list[GateFinding] | None) -> bool:
    if findings is None:
        return False
    return bool(findings) or run.get("gate_summary") is not None


def check_gates(run: Mapping[str, Any], findings: list[GateFinding] | None, active: bool) -> AutonomyCheck:
    key = "integrity_gates"
    if not gates_evaluated(run, findings):
        return _missing(key, active, PROVIDER_UNAVAILABLE if findings is None else "gates_not_run")
    assert findings is not None
    act = _active_findings(findings)
    blocks = [f for f in act if f.severity == "block"]
    warns = [f for f in act if f.severity == "warn"]
    overridden = [f for f in findings if f.status == "overridden" and f.severity == "block"]
    params = {"blocking": len(blocks), "warnings": len(warns), "overridden": len(overridden)}
    if blocks:
        return AutonomyCheck(key, "fail", params, tuple(_reason(f) for f in blocks))
    if overridden:
        return AutonomyCheck(key, "warn", params, tuple(_reason(f) for f in overridden), overridden=True)
    if warns:
        return AutonomyCheck(key, "warn", params, tuple(_reason(f) for f in warns))
    return AutonomyCheck(key, "pass", params)


def is_power_cable(ident: Any) -> bool:
    """A branch power cable (MC, AC, NM with #14 or larger conductors): it
    carries both the conductors and their sheath. Control and low-voltage
    cable (#18/4C control cable, CAT6, #16 shielded) is not branch wiring."""
    if ident.item_class != "cable":
        return False
    fam = str(ident.family or "")
    if fam.endswith((":control", ":cat5", ":cat5e", ":cat6", ":cat6a", ":fiber", ":coax")):
        return False
    size = str((ident.attrs or {}).get("conductor_size") or "").strip()
    if not size:
        return False
    if "/0" in size or size.upper().endswith("KCMIL") or size.upper().endswith("MCM"):
        return True
    try:
        return int(size) <= MIN_BRANCH_AWG
    except ValueError:
        return False


def boq_length_problems(boq: Mapping[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
    """Code checks on the BOQ itself (independent of the gates): linear items
    in EA, and new devices without branch lengths. Spec E2: new devices > 0
    needs branch conductor FT > 0 AND branch conduit (raceway or power
    cable) FT > 0; a power cable counts as both, control and low-voltage
    cable counts as neither. Returns (reasons, params)."""
    reasons: list[str] = []
    devices = 0
    conductor_ft = 0.0
    raceway_ft = 0.0
    branch_ft = 0.0
    linear_ea: list[str] = []
    for ln in iter_boq_lines(boq or {}):
        if ln.get("is_accessory"):
            continue
        unit = str(ln.get("unit") or "").upper()
        ident = parse_item(str(ln.get("description") or ""), unit)
        klass = ident.item_class
        if unit == "EA" and is_linear(ident):
            linear_ea.append(str(ln["line_id"]))
        if unit == "EA" and klass in DEVICE_CLASSES:
            devices += int(round(_num(ln.get("quantity")) or 0))
        if unit != "FT":
            continue
        q = _num(ln.get("quantity")) or 0.0
        if klass == "conductor":
            conductor_ft += q
        elif klass in BRANCH_RACEWAY_CLASSES:
            raceway_ft += q
        elif is_power_cable(ident):
            conductor_ft += q
            raceway_ft += q
        else:
            continue
        branch_ft += q
    reasons.extend(f"boq:linear_in_ea:{lid}" for lid in linear_ea)
    if devices > 0 and (conductor_ft <= 0 or raceway_ft <= 0):
        reasons.append("boq:no_branch_lengths")
    return reasons, {
        "devices": devices,
        "branch_ft_lines": round(branch_ft, 2),
        "branch_conductor_ft": round(conductor_ft, 2),
        "branch_raceway_ft": round(raceway_ft, 2),
        "linear_in_ea": len(linear_ea),
    }


def check_lengths(findings: list[GateFinding] | None, boq: tuple[int | None, Mapping[str, Any]] | None,
                  measure: Mapping[str, Any] | None, evidence: Mapping[str, Any] | None,
                  active: bool) -> AutonomyCheck:
    key = "lengths_produced"
    gate_hits = [f for f in _active_findings(findings or []) if f.gate_id in LENGTH_GATES and f.severity == "block"]
    if boq is None and findings is None:
        return _missing(key, active)
    boq_reasons, params = boq_length_problems(boq[1] if boq else None)
    params = dict(params)
    params["measure_available"] = measure is not None
    reasons = [_reason(f) for f in gate_hits] + boq_reasons
    if reasons:
        return AutonomyCheck(key, "fail", params, tuple(reasons))
    warn: list[str] = []
    if measure is None:
        warn.append("measure:missing")
    else:
        assumed = _assumed_share(measure)
        params["assumed_share"] = assumed
        if assumed is not None and assumed > ASSUMED_SHARE_WARN:
            warn.append("measure:assumed_routes_over_half")
    stats = (evidence or {}).get("stats") if isinstance(evidence, Mapping) else None
    if isinstance(stats, Mapping):
        ft_lines = _int(stats.get("ft_lines")) or 0
        with_poly = _int(stats.get("ft_lines_with_polyline")) or 0
        params["ft_lines"], params["ft_lines_with_polyline"] = ft_lines, with_poly
        if with_poly < ft_lines:
            warn.append("evidence:ft_lines_not_drawn")
        if (_int(stats.get("length_mismatch")) or 0) > 0:
            warn.append("evidence:length_mismatch")
    if warn:
        return AutonomyCheck(key, "warn", params, tuple(warn))
    return AutonomyCheck(key, "pass", params)


def _assumed_share(measure: Mapping[str, Any]) -> float | None:
    total = 0.0
    assumed = 0.0
    for r in measure.get("routes") or []:
        if not isinstance(r, Mapping):
            continue
        ft = _num(r.get("total_ft")) or sum(_num(s.get("length_ft")) or 0.0 for s in r.get("segments") or [] if isinstance(s, Mapping))
        total += ft
        if str(r.get("basis") or "").startswith("assumed"):
            assumed += ft
    return round(assumed / total, 4) if total > 0 else None


def check_scope(scope: Mapping[str, Any] | None, findings: list[GateFinding] | None, active: bool) -> AutonomyCheck:
    key = "scope_conditions"
    conflicts = [f for f in _active_findings(findings or []) if f.gate_id in SCOPE_GATES and f.severity == "block"]
    if scope is None and not conflicts:
        return _missing(key, active)
    bid = (scope or {}).get("bid_conditions") if isinstance((scope or {}).get("bid_conditions"), Mapping) else {}
    report = (scope or {}).get("scope_report") if isinstance((scope or {}).get("scope_report"), Mapping) else {}
    wage = bid.get("wage_type") if bid else None
    basis = bid.get("wage_basis") if bid else None
    params = {"wage_type": wage, "wage_basis": basis}
    reasons = [_reason(f) for f in conflicts]
    if conflicts:
        return AutonomyCheck(key, "fail", params, tuple(reasons))
    if wage not in WAGE_TYPES:
        return AutonomyCheck(key, "fail", params, ("wage_unknown",))
    needs = [str(n) for n in (bid.get("needs_review") or [])] + [str(n) for n in (report.get("needs_review") or [])]
    if basis in (None, "default") or needs:
        return AutonomyCheck(key, "warn", {**params, "needs_review": len(needs)},
                             tuple(["wage_basis:default"] if basis in (None, "default") else []) + tuple(f"needs_review:{n}" for n in needs[:8]))
    return AutonomyCheck(key, "pass", params)


def _price_chip_kind(line: Mapping[str, Any]) -> str:
    # Local import: the evidence module imports page facts; status() stays light.
    from app.services.plans_reader_evidence import price_chip

    return price_chip(line).kind if line else "missing"


def check_pricing(pricing: Mapping[str, Any] | None, boq_version: int | None, active: bool,
                  *, include_pricing: bool, pricing_enabled: bool) -> AutonomyCheck:
    key = "pricing_coverage"
    if not pricing_enabled:
        return AutonomyCheck(key, "na", {}, ("ai_pricing_disabled",))
    if pricing is None:
        return _missing(key, active)
    reasons: list[str] = []
    status = str(pricing.get("status") or "")
    p_boq = _int(pricing.get("boq_version"))
    if pricing.get("stale") or (boq_version is not None and p_boq is not None and p_boq != boq_version):
        reasons.append("pricing:stale")
    if status == "blocked":
        reasons.append("pricing:blocked")
    verify = pricing.get("verify_draft") if isinstance(pricing.get("verify_draft"), Mapping) else {}
    rate = _num(verify.get("labor_rate")) or DEFAULT_RATE_FOR_WEIGHTS
    total = 0.0
    strong = 0.0
    mismatch = 0
    lines = [ln for ln in pricing.get("lines") or [] if isinstance(ln, Mapping)]
    for ln in lines:
        w = (_num(ln.get("labor_hours")) or 0.0) * rate + (_num(ln.get("material_cost")) or 0.0)
        kind = _price_chip_kind(ln)
        if kind == "uom_mismatch":
            mismatch += 1
        total += w
        if kind in ("quote", "library"):  # library = strong or good quality
            strong += w
    share = round(strong / total, 4) if total > 0 else None
    if mismatch:
        reasons.append("pricing:uom_mismatch")
    params = {"share": share, "lines": len(lines), "status": status, "uom_mismatch": mismatch} if include_pricing else {}
    if reasons:
        return AutonomyCheck(key, "fail", params, tuple(reasons))
    if share is None:
        return AutonomyCheck(key, "fail", params, ("pricing:no_priced_value",))
    if share >= PRICING_PASS_SHARE and status == "priced":
        return AutonomyCheck(key, "pass", params)
    if share >= PRICING_WARN_SHARE:
        why = ("pricing:provisional",) if status != "priced" else ("pricing:coverage_below_pass",)
        return AutonomyCheck(key, "warn", params, why)
    return AutonomyCheck(key, "fail", params, ("pricing:coverage_low",))


def check_review(review: Mapping[str, Any] | None, active: bool) -> AutonomyCheck:
    key = "independent_review"
    if review is None:
        return _missing(key, active)
    verdict = str(review.get("verdict") or "")
    params = {"verdict": verdict, "findings": len(review.get("findings") or [])}
    if verdict == "clear":
        return AutonomyCheck(key, "pass", params)
    if verdict == "advisory":
        return AutonomyCheck(key, "warn", params, ("review:advisory",))
    if verdict in ("needs_human", "incomplete"):
        return AutonomyCheck(key, "fail", params, (f"review:{verdict}",))
    if verdict == "stale":
        return AutonomyCheck(key, "pending" if active else "warn", params, ("review:stale",))
    if verdict == "skipped":
        return AutonomyCheck(key, "skipped", params, ("review:skipped",))
    return _missing(key, active, "review:unknown_verdict")


def check_bands(review: Mapping[str, Any] | None, active: bool) -> AutonomyCheck:
    key = "history_bands"
    bands = (review or {}).get("bands") if isinstance(review, Mapping) else None
    if not isinstance(bands, list):
        return _missing(key, active)
    crit = [b for b in bands if isinstance(b, Mapping) and str(b.get("severity")) == "critical"]
    warn = [b for b in bands if isinstance(b, Mapping) and str(b.get("severity")) == "warning"]
    params = {"bands": len(bands), "critical": len(crit), "warning": len(warn)}
    if crit:
        return AutonomyCheck(key, "fail", params, tuple(f"band:{b.get('metric')}" for b in crit))
    if warn:
        return AutonomyCheck(key, "warn", params, tuple(f"band:{b.get('metric')}" for b in warn))
    return AutonomyCheck(key, "pass", params)


def check_evidence(evidence: Mapping[str, Any] | None, active: bool) -> AutonomyCheck:
    key = "evidence"
    stats = (evidence or {}).get("stats") if isinstance(evidence, Mapping) else None
    if not isinstance(stats, Mapping):
        return _missing(key, active)
    pc = _num(stats.get("provenance_completeness"))
    density = _num(stats.get("density"))
    ft_lines = _int(stats.get("ft_lines")) or 0
    with_poly = _int(stats.get("ft_lines_with_polyline")) or 0
    params = {
        "provenance_completeness": pc,
        "density": density,
        "anchor_share": _num(stats.get("anchor_share")),
        "ft_lines": ft_lines,
        "ft_lines_with_polyline": with_poly,
    }
    values = [v for v in (pc, density) if v is not None]
    if not values:
        return AutonomyCheck(key, "warn", params, ("evidence:no_lines",))
    worst = min(values)
    reasons = []
    if pc is not None and pc < EVIDENCE_PASS:
        reasons.append("evidence:provenance")
    if density is not None and density < EVIDENCE_PASS:
        reasons.append("evidence:density")
    if with_poly < ft_lines:
        reasons.append("evidence:ft_lines_not_drawn")
    if worst < EVIDENCE_WARN:
        return AutonomyCheck(key, "fail", params, tuple(reasons))
    if reasons:
        return AutonomyCheck(key, "warn", params, tuple(reasons))
    return AutonomyCheck(key, "pass", params)


def overall_state(checks: Sequence[AutonomyCheck], *, legacy: bool) -> str:
    if legacy:
        return "not_evaluated"
    states = [c.state for c in checks]
    if "pending" in states:
        return "in_progress"
    if any(c.state == "fail" and not c.overridden for c in checks):
        return "blocked"
    if any(c.state in ("warn", "skipped") or (c.state == "fail" and c.overridden) for c in checks):
        return "needs_review"
    return "ready_for_verification"


def _signoff_view(row: Mapping[str, Any] | None, latest_pricing: int | None, include_pricing: bool) -> dict[str, Any] | None:
    if not row:
        return None
    version = _int(row.get("pricing_version"))
    out = {
        "decision": row.get("decision"),
        "created_by": row.get("created_by"),
        "created_at": pacific(row.get("created_at")),
        "superseded": bool(latest_pricing is not None and version is not None and version < latest_pricing),
    }
    if include_pricing:
        out["pricing_version"] = version
        out["note"] = row.get("note")
    return out


def _settings_pricing_enabled() -> bool:
    try:
        from app.core.config import get_settings

        return bool(getattr(get_settings(), "ai_pricing_enabled", False))
    except Exception:  # noqa: BLE001 - settings unavailable means pricing off
        return False


def status(
    sb: Any,
    run: Any,
    *,
    include_pricing: bool,
    sources: AutonomySources | None = None,
    chain_active: bool = False,
    pricing_enabled: bool | None = None,
) -> AutonomyStatus:
    """The AutonomyStatus of a run (module docstring). `run` is the run row
    or its id. `chain_active` is True while the post-run chain (pricing,
    review) is running, so missing checks read `pending`."""
    src: AutonomySources = sources if sources is not None else SupabaseAutonomySources(sb)
    run_id = _run_id(run)
    if isinstance(run, Mapping):
        row = dict(run)
        if not row.get("status"):
            row = {**(src.run(run_id) or {}), **row}
    else:
        row = src.run(run_id) or {"id": run_id}
    run_status = str(row.get("status") or "")
    active = chain_active or (bool(run_status) and run_status not in TERMINAL_RUN_STATUSES)
    enabled = _settings_pricing_enabled() if pricing_enabled is None else pricing_enabled

    groups = src.groups(run_id)
    findings = src.gate_findings(run_id)
    boq = src.boq(run_id)
    measure = src.measure(run_id)
    scope = src.scope(run_id, row)
    pricing = src.pricing(run_id) if enabled else None
    review = src.review(run_id)
    evidence = src.evidence(run_id)
    signoff = src.signoff(run_id)
    boq_version = boq[0] if boq else None

    checks = [
        check_reading(row, groups, findings, active),
        check_gates(row, findings, active),
        check_lengths(findings, boq, measure, evidence, active),
        check_scope(scope, findings, active),
        check_pricing(pricing, boq_version, active, include_pricing=include_pricing, pricing_enabled=enabled),
        check_review(review, active),
        check_bands(review, active),
        check_evidence(evidence, active),
    ]
    legacy = not gates_evaluated(row, findings) and evidence is None
    pricing_version = _int((pricing or {}).get("version"))
    return AutonomyStatus(
        run_id=run_id or None,
        overall=overall_state(checks, legacy=legacy),
        checks=checks,
        boq_version=boq_version,
        pricing_version=pricing_version,
        signoff=_signoff_view(signoff, pricing_version, include_pricing),
        computed_at=now_pacific_iso(),
        include_pricing=include_pricing,
    )


def recompute(
    sb: Any,
    run_id: str,
    *,
    sources: AutonomySources | None = None,
    chain_active: bool = False,
    pricing_enabled: bool | None = None,
) -> str:
    """Compute the status (pricing audience, so the pricing check counts) and
    persist `plans_reader_runs.autonomy_state` (migration 0165). A failed
    write is logged and the computed state is still returned."""
    st = status(sb, run_id, include_pricing=True, sources=sources, chain_active=chain_active,
                pricing_enabled=pricing_enabled)
    if sb is not None:
        try:
            sb.table("plans_reader_runs").update({"autonomy_state": st.overall}).eq("id", run_id).execute()
        except Exception as exc:  # noqa: BLE001 - before 0165 the column is absent
            logger.warning("autonomy: autonomy_state write for run %s failed: %s", run_id, exc)
    return st.overall


# Sign-off rules (migration 0169; the route is wave 3) -------------------------------


def validate_signoff(
    st: AutonomyStatus,
    decision: str,
    note: str | None,
    *,
    accepted: Mapping[str, str] | None = None,
    pricing_version: int | None = None,
) -> list[str]:
    """Errors for a sign-off request (empty when valid). `verified` needs
    `ready_for_verification`, or an explicit note of >= 20 characters for
    every check that is not passing (`accepted` {check key: note}).
    `rejected` needs a note of >= 10 characters. The version must be the
    latest priced version."""
    errors: list[str] = []
    if decision not in SIGNOFF_DECISIONS:
        return [f"decision must be one of {', '.join(SIGNOFF_DECISIONS)}"]
    if pricing_version is None or pricing_version < 1:
        errors.append("a priced version is required")
    elif st.pricing_version is not None and pricing_version != st.pricing_version:
        errors.append("only the latest priced version can be signed")
    if decision == "rejected":
        if len((note or "").strip()) < REJECT_NOTE_MIN:
            errors.append(f"a rejection needs a note of at least {REJECT_NOTE_MIN} characters")
        return errors
    if st.overall == "ready_for_verification":
        return errors
    acc = accepted or {}
    for key in st.failing():
        if key == "pricing_coverage" and st.check(key).state == "na":
            continue
        if len((acc.get(key) or "").strip()) < SIGNOFF_NOTE_MIN:
            errors.append(f"check {key} is not passing: accept it with a note of at least {SIGNOFF_NOTE_MIN} characters")
    return errors


def signoff_row(
    run_id: str,
    pricing_version: int,
    decision: str,
    note: str | None,
    st: AutonomyStatus,
    totals: Mapping[str, Any],
    *,
    created_by: str | None,
    accepted: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The plans_reader_price_signoffs insert (nullable keys omitted when
    None, so the insert works with the column defaults)."""
    snap = st.to_dict()
    if accepted:
        snap["accepted"] = dict(accepted)
    row: dict[str, Any] = {
        "run_id": run_id,
        "pricing_version": int(pricing_version),
        "decision": decision,
        "autonomy_snapshot": snap,
        "totals_snapshot": dict(totals),
    }
    if note:
        row["note"] = note
    if created_by:
        row["created_by"] = created_by
    return row


__all__ = [
    "AutonomyCheck",
    "AutonomySources",
    "AutonomyStatus",
    "CHECK_KEYS",
    "FakeAutonomySources",
    "OVERALL_STATES",
    "SupabaseAutonomySources",
    "boq_length_problems",
    "overall_state",
    "recompute",
    "signoff_row",
    "status",
    "validate_signoff",
]
