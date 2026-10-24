"""Integrity gates, findings persistence and the pricing gate
(docs/AI_ESTIMATING_BUILD_SPEC.md 4.4, 5.4 gate ids, 5.6 plans_reader_gate_findings, WS4).

Gates are code. They read the BOQ, the main output prose, the category
output rows, the census floors, the verified scope facts and the measure
output, and they return findings that never lower a number: a blocking
finding routes the problem (`model_fix` inside the consolidator loop,
`human` at the final gate) and the quantity stays what the evidence says.

Two evaluation modes:

- `tools`: the BOQ lines are v2 (line_id, source_refs, combine, basis), so
  provenance gates run (unsourced_line, unverifiable_citation, orphan_rows,
  double_consumption, arithmetic, lost_sheets, brief_numbers);
- `legacy`: the stored v1 BOQ (today's main call); those gates return
  `not_applicable`, census accounting sums EA lines by sheet_refs, and
  partial_basis reads the material notes and the merged rows' coverage
  matched by description. The legacy mode is what the $0 replay of the
  stored 7154 run exercises (test_gates_legacy_7154).

Gate ids are the 5.4 list and nothing else (`estimating_contracts.GATE_IDS`;
`uom_conflict_quote` and `wage_type_unknown` are raised by WS6 at stage
pricing and recorded here). `new_devices_no_branch` counts cable (MC, AC,
NM) as branch and is satisfied by a verified existing-circuit fact or an
`existing_circuit` disposition. `excluded_system` moves an allowance of a
system a verified fact excludes (the Honeywell fire alarm and access
control allowance on 7154) and blocks a line of it; a fact about dampers is
not a fact about fire alarm (`systems_of`), and a power connection or a
rough-in is EC work that no system fact excludes. Raceway and rough-in
lines of an excluded system (conduit, EMT, boxes, circuits, connections:
`is_raceway_text`) stay too, unless the fact itself puts that raceway on
others (`fact_excludes_raceway`): "FA devices by others; EC provides
conduit and boxes" keeps the FA conduit (spec WS4, the devices_by_others
split of WS5 scope_attribution).

Persistence: `record_findings(sb, run, findings, stage)` upserts on
(run_id, output_version, stage, fingerprint), keeps a human status
(resolved, overridden) and supersedes open findings the new pass no longer
raises. `pricing_allowed(run)` reads the stage `final` findings (the run's
`gate_summary`, or the table when a client is given); a run on which no
final gate pass ever ran refuses (`gates_not_run`), never allows by absence. The report shape is
`bdr.gate_report/1`. `plans_reader_gates_blocking` (5.7) says whether the
findings are enforced; the findings keep their severity either way so the
Checks tab shows the verdict.

The findings table ships with migration 0165 (applied after this wave):
every table read is through the client passed in, nothing is needed at
import, and inserts omit null values (the MC rule).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import plans_reader_floors as floors_mod
from app.services import plans_reader_lines as lines_mod
from app.services import plans_reader_rows as rows_mod
from app.services.electrical_items import (
    BoqRow,
    is_linear,
    linear_evidence_required,
    normalize_description,
    parse_item,
    similarity,
)
from app.services.estimating_contracts import (
    ACTIVE_FINDING_STATUSES,
    BOQ_LINE_PREFIX,
    GATE_IDS,
    GATE_REPORT_SCHEMA,
    PRICING_GATES_UNAVAILABLE,
    PRICING_STAGE_GATES,
    ContractError,
    ExcludedLine,
    Fact,
    GateFinding,
    _stamp,
    gate_report_from_findings,
    is_row_id,
    validate_finding_stage,
)
from app.services.plans_reader_regions import for_recount
from app.services.scope_attribution import ROUGH_IN_RE as RACEWAY_TEXT_RE

GATE_VERSION = "ws4-gates-1"
# pricing_allowed reason when no final gate pass ever ran on the run (no
# gate_summary and no stage `final` finding in the table or the argument);
# the same code plans_reader_autonomy.check_gates uses.
GATES_NOT_RUN = "gates_not_run"
COMPANY_TZ = ZoneInfo("America/Los_Angeles")
FINDINGS_TABLE = "plans_reader_gate_findings"
MODES: tuple[str, ...] = ("tools", "legacy")
ROUTES_FINAL: tuple[str, ...] = ("model_fix", "human")

# Gates that need v2 provenance (source_refs, consumption, row ids).
PROVENANCE_GATES: tuple[str, ...] = (
    "unverifiable_citation", "unsourced_line", "orphan_rows", "arithmetic",
    "double_consumption", "lost_sheets", "brief_numbers",
)
# Gates this module evaluates at stage final (the rest are WS6's).
FINAL_GATES: tuple[str, ...] = tuple(g for g in GATE_IDS if g not in PRICING_STAGE_GATES)
assert set(PROVENANCE_GATES) <= set(FINAL_GATES)

# The E3 sentences (estimating_contracts CHECK_SPECS E3 carries the same list).
CAPABILITY_DENIAL_PATTERNS: tuple[str, ...] = (
    r"physical page calibration",
    r"do not use per-device averages",
    r"(no|without) .*invented assumed routes",
    r"printed scale alone .* insufficient",
    r"cannot be defensibly measured",
)
_DENIAL_RES = tuple(re.compile(p, re.I) for p in CAPABILITY_DENIAL_PATTERNS)

# Branch wiring classes; cable (MC, AC, NM) counts as both conductor and raceway.
BRANCH_CONDUCTOR_CLASSES: frozenset[str] = frozenset({"conductor", "cable"})
BRANCH_RACEWAY_CLASSES: frozenset[str] = frozenset({"conduit", "raceway", "cable"})
# Classes whose EA lines are the "new devices" branch wiring must reach.
DEVICE_CLASSES: frozenset[str] = frozenset(
    {"receptacle", "switch", "control", "luminaire", "luminaire_linear", "equipment_connection", "other"}
)
DEVICE_GROUPS: frozenset[str] = frozenset({"OTHER ITEMS", "LIGHTING FIXTURES", "LIGHTING CONTROLS"})
COUNT_CLASSES: frozenset[str] = frozenset(
    {"receptacle", "switch", "control", "luminaire", "breaker", "panel", "equipment_connection"}
)
# Linear luminaire kinds and how the estimators price them, (FT lines, EA
# lines) over the 62 non-holdout golden BOQs (scripts/plans_reader_golden_checks.py,
# run 2026-10-22; EVAL_ROOT/reports/ws4_golden_checks.md). A kind blocks an
# EA line only when >= LINEAR_BLOCK_SHARE of at least LINEAR_MIN_LINES lines
# are FT (spec 5.1). None does: linear 7 of 48, strip 1 of 24, undercabinet
# 1 of 4; cove and tape have one line each. An EA line of a linear luminaire
# therefore warns unless the run carries per-foot evidence (schedule per
# foot, a per-foot vendor quote, a WS3 measurement), which blocks.
LINEAR_BLOCK_SHARE = 0.80
LINEAR_MIN_LINES = 5
LINEAR_LUMINAIRE_FT_COUNTS: dict[str, tuple[int, int]] = {
    "cove": (1, 0), "linear": (7, 41), "strip": (1, 23), "tape": (1, 0), "undercabinet": (1, 3),
}


def _effective_ft_share(counts: Mapping[str, tuple[int, int]]) -> dict[str, float]:
    """FT share per kind; a kind under LINEAR_MIN_LINES lines reads as 0."""
    out: dict[str, float] = {}
    for kind, (ft, ea) in counts.items():
        n = ft + ea
        out[kind] = round(ft / n, 4) if n >= LINEAR_MIN_LINES else 0.0
    return out


LINEAR_LUMINAIRE_FT_SHARE: dict[str, float] = _effective_ft_share(LINEAR_LUMINAIRE_FT_COUNTS)
# Census families that block when short: the strong families whose golden
# check passed (scripts/plans_reader_golden_checks.py: estimator total >=
# floor - tolerance in >= 95% of ALL (project, family) pairs, a pair with no
# estimator line of the family counting as estimator 0; at least 10 pairs).
# Re-measured 2026-10-22 over 62 non-holdout golden projects:
#   demo_x       16 of 17 = 94.1% (16 of 16 = 100% leaving out the one
#                project with 4 printed (X) tags and no demolition line)
#   relocate_r    2 of 5  = 40.0% (1 of 3 with a line): warn-only; the
#                (R) floors run above what estimators carry (e70b9bdc 22
#                vs 15, f9182cfc 58 vs 26), so a short relocate count is a
#                prompt to look, not a refusal
#   circuit      18 of 21 = 85.7% (17 of 19 with a line)
#   fixture_mark 23 of 102 = 22.6% (19 of 30 with a line): type marks
#                exceed fixtures on sheets that print the type at every label
#   device_mod    1 of 1 (unmeasured)
# No family passes, so none blocks: every family is warn-only and still
# warns (spec WS4: a family that fails the floor check is warn-only).
# test_gates_legacy_7154 (spec: census_floor ED3.01 among the blocks) now
# sees a warn; the owner decides between this rule and an exclusion of
# no-line pairs (see the build log).
BLOCKING_FAMILIES: frozenset[str] = frozenset()
WARN_FAMILIES: frozenset[str] = frozenset(floors_mod.FAMILIES) - BLOCKING_FAMILIES
# Families whose short floors become recount TARGETS in pre_consolidation:
# a separate choice from the finding severity. demo_x (strong, the E11
# case: 34 read vs 39 printed tags) and circuit (the best measured weak
# family, one token per tagged device group). relocate_r, fixture_mark and
# device_mod floors run above estimator totals on the golden set, so a
# recount against them would spend the recount budget confirming counts.
RECOUNT_FAMILIES: frozenset[str] = frozenset({"demo_x", "circuit"})
PARTIAL_MATCH_THRESHOLD = rows_mod.PATCH_MATCH_THRESHOLD
# What a legacy BOQ note says when a count came from a tile (spec WS4 legacy
# mode: partial_basis by SUBTOTAL_RE on material_note and caveat). One
# definition: plans_reader_rows.SUBTOTAL_RE, extended only with the
# enlarged-tile phrase the escalation notes use ("in enlarged tile only").
# Bare "not complete" and "clipped" are left out: "panel schedule not
# complete" or "clipped at match line" is not a tile subtotal.
PARTIAL_TILE_ONLY = r"\b(?:in|within|inside)\s+(?:the\s+)?(?:one\s+)?enlarged\s+tiles?\s+only\b"
PARTIAL_NOTE_RE = re.compile(rows_mod.SUBTOTAL_RE.pattern + "|" + PARTIAL_TILE_ONLY)
assert all(PARTIAL_NOTE_RE.search(t) for t in ("New tile subtotal only.", "inside the tiles only", "in enlarged tile only"))
assert not any(PARTIAL_NOTE_RE.search(t) for t in ("panel schedule not complete", "clipped at match line"))
_ALLOWANCE_FIELDS = ("extra_allowances",)
_FIRE_ALARM_RE = re.compile(
    r"\bFIRE\s*ALARM\b|\bFA\b|\bFIRE\s+DETECTION\b|\bSMOKE\s+DETECT\w*\b|\bHEAT\s+DETECT\w*\b|\bPULL\s+STATION\b"
    r"|\bHORN\b|\bSTROBE\b|\bNOTIFICATION\s+APPLIANCE\b|\bFACP\b|\bANNUNCIATOR\b|\bDUCT\s+DETECT\w*\b"
)
_SYSTEM_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("access_control", re.compile(r"\bACCESS\s+CONTROL\w*\b|\bCARD\s+READER\b|\bDOOR\s+CONTROL\b|\bMAG(?:NETIC)?\s*LOCK\b|\bELECTRIC\s+STRIKE\b|\bREQUEST\s+TO\s+EXIT\b|\bREX\b")),
    ("damper", re.compile(r"\bDAMPERS?\b")),
    ("building_automation", re.compile(r"\bTHERMOSTAT\w*\b|\bATC\b|\bBMS\b|\bBAS\b|\bDDC\b|\bCONTROLS\s+CONTRACTOR\b|\bTEMPERATURE\s+CONTROL\b")),
    ("security", re.compile(r"\bCCTV\b|\bCAMERAS?\b|\bSECURITY\b|\bINTRUSION\b")),
    ("low_voltage", re.compile(r"\bDATA\b|\bTELECOM\w*\b|\bCOMMUNICATIONS?\b|\bVOICE\b|\bCAT\s?[56]A?\b|\bTELEPHONE\b|\bWAP\b|\bWIRELESS\s+ACCESS\b|\bTELEMETRY\b|\bCENTRAK\b|\bNURSE\s+CALL\b|\bPAGING\b|\bPA\s+SYSTEM\b|\bINTERCOM\b|\bAUDIO\s*/?\s*VISUAL\b|\bA/V\b")),
    ("temporary_power", re.compile(r"\bTEMP(?:ORARY)?\s+(?:POWER|LIGHT\w*|DISTRIBUTION)\b")),
    ("furniture", re.compile(r"\bFURNITURE\b")),
    ("door_hardware", re.compile(r"\bDOOR\s+HARDWARE\b|\bDOOR\s+OPERATORS?\b")),
    ("painting", re.compile(r"\bPAINT\w*\b")),
    ("solar", re.compile(r"\bSOLAR\b|\bPHOTOVOLTAIC\b|\bPV\b")),
    ("ev_charging", re.compile(r"\bEV\s+CHARG\w*\b|\bEVSE\b|\bCHARGING\s+STATION\b")),
    ("generator", re.compile(r"\bGENERATORS?\b")),
    ("lightning_protection", re.compile(r"\bLIGHTNING\b")),
    ("mechanical_equipment", re.compile(r"\bHVAC\b|\bMECHANICAL\s+EQUIPMENT\b|\bAIR\s+HANDL\w*\b|\bVAV\b|\bRTU\b|\bEXHAUST\s+FAN\b")),
)
_CONNECTION_RE = re.compile(r"\bPOWER\s+CONNECTION\b|\bELECTRICAL\s+CONNECTION\b|\bCONNECTION\s+TO\b|\bCONNECT\s+TO\b|\bFINAL\s+CONNECTION\b|\bMAKE\s+SAFE\b")
_ROUGH_IN_RE = re.compile(r"\bROUGH[\s-]?INS?\b|\bBACK\s*BOX\w*\b|\bCONDUIT\s+AND\s+BOX\w*\b|\bBOXES?\s+AND\s+CONDUIT\b|\bJ-?BOX\w*\b|\bRACEWAYS?\s+ONLY\b")
_EXISTING_CIRCUIT_RE = re.compile(r"(?i)\bexisting\b.*\b(circuit|wiring|branch|conductor|raceway|conduit)\w*\b|\b(circuit|wiring|branch)\w*\b.*\bexisting\b|\bextend\w*\b.*\bexisting\b")
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_BRIEF_FIELDS = ("project_summary", "electrical_scope_summary", "systems", "other_trades_affecting_electrical", "sheet_summaries", "spec_highlights", "missing_documents")
_TAG_CLAIM_RE = re.compile(r"(?i)\b(\d{1,4}|" + "|".join(_NUMBER_WORDS) + r")\s+(?:x\s+)?([A-Z]{1,2}\d{1,2})\b(?![\w./-])")
_RECEPT_CLAIM_RE = re.compile(r"(?i)\b(\d{1,4})\s+(?:new\s+)?(?:receptacle|receptacles|receptacle assemblies)\b")
_WS_RE = re.compile(r"\s+")


def now_pacific_iso() -> str:
    return datetime.now(COMPANY_TZ).isoformat(timespec="seconds")


# Settings -------------------------------------------------------------------------


@dataclass(frozen=True)
class GateSettings:
    """The 5.7 WS4 settings the gates read."""

    tol_strong: float = floors_mod.DEFAULT_TOL_STRONG
    tol_weak: float = floors_mod.DEFAULT_TOL_WEAK
    blocking: bool = False
    demo_two_line_style: bool = False
    ft_share: Mapping[str, float] = field(default_factory=lambda: dict(LINEAR_LUMINAIRE_FT_SHARE))
    blocking_families: frozenset[str] = BLOCKING_FAMILIES
    warn_families: frozenset[str] = WARN_FAMILIES
    recount_families: frozenset[str] = RECOUNT_FAMILIES

    @classmethod
    def load(cls, settings: Any = None, **overrides: Any) -> GateSettings:
        if isinstance(settings, GateSettings):
            changes = {k: v for k, v in overrides.items() if v is not None}
            return replace(settings, **changes) if changes else settings
        src = settings
        if src is None:
            try:
                from app.core.config import get_settings

                src = get_settings()
            except Exception:  # noqa: BLE001 - settings unavailable reads as the code defaults
                src = None

        def get(name: str, default: Any) -> Any:
            if src is None:
                return default
            if isinstance(src, Mapping):
                return src.get(name, default)
            return getattr(src, name, default)

        values = {
            "tol_strong": float(get("plans_reader_census_tol_strong", cls.tol_strong)),
            "tol_weak": float(get("plans_reader_census_tol_weak", cls.tol_weak)),
            "blocking": bool(get("plans_reader_gates_blocking", cls.blocking)),
            "demo_two_line_style": bool(get("plans_reader_demo_two_line_style", cls.demo_two_line_style)),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def as_floor_settings(self) -> dict[str, float]:
        return {"plans_reader_census_tol_strong": self.tol_strong, "plans_reader_census_tol_weak": self.tol_weak}


# Systems (facts vs lines) -----------------------------------------------------------


def systems_of(text: Any) -> frozenset[str]:
    """The named systems a description or fact statement is about. A power
    or electrical connection and a rough-in are EC work (`equipment_connection`,
    `rough_in`), never the system they serve; a damper is `damper`, not fire
    alarm, even when the text says "fire/smoke damper"."""
    norm = normalize_description(text or "")
    if not norm:
        return frozenset()
    if _CONNECTION_RE.search(norm):
        return frozenset({"equipment_connection"})
    if _ROUGH_IN_RE.search(norm):
        return frozenset({"rough_in"})
    found: set[str] = set()
    damper = bool(re.search(r"\bDAMPERS?\b", norm))
    if _FIRE_ALARM_RE.search(norm) and not damper:
        found.add("fire_alarm")
    for name, pat in _SYSTEM_RULES:
        if pat.search(norm):
            found.add(name)
    return frozenset(found)


def fact_systems(fact: Any) -> frozenset[str]:
    """Systems a Fact (or its dict) excludes: from its `system` field, else
    its statement and quote."""
    if isinstance(fact, Mapping):
        system, statement, quote = fact.get("system"), fact.get("statement"), (fact.get("cite") or {}).get("quote")
    else:
        system, statement = getattr(fact, "system", None), getattr(fact, "statement", None)
        cite = getattr(fact, "cite", None)
        quote = getattr(cite, "quote", None) if cite is not None else None
    out = systems_of(system)
    if not out:
        out = systems_of(f"{statement or ''} {quote or ''}")
    return out


def fact_excludes(fact: Any) -> bool:
    if isinstance(fact, Fact):
        return fact.excludes
    if isinstance(fact, Mapping):
        kind = fact.get("kind")
        return bool(fact.get("verified")) and (kind is None or kind in ("provided_by_others", "not_used", "nic", "by_gc"))
    return bool(getattr(fact, "excludes", False))


# Item classes that are raceway (spec 5.1 LINEAR_CLASSES minus wiring): a line
# of these classes is rough-in work the EC keeps for a system by others.
RACEWAY_CLASSES: frozenset[str] = frozenset({"conduit", "raceway"})
# "EC provides conduit and boxes": the fact keeps the raceway with the EC.
_EC_PROVIDES_RE = re.compile(
    r"(?i)\b(?:EC|E\.C\.|electrical\s+contractor|division\s*26|div\.?\s*26)\b[^.;]{0,40}?"
    r"\b(?:provides?|providing|furnish\w*|install\w*|responsible\s+for|shall\s+provide)\b"
)


def is_raceway_text(description: Any, unit: Any = "EA") -> bool:
    """A raceway or rough-in line: conduit, EMT, raceway, boxes, rough-in,
    stub-ups, sleeves, pull strings, circuits or connections (the WS5
    scope_attribution ROUGH_IN_RE, one definition), or an item of a
    RACEWAY_CLASSES class."""
    text = _text(description)
    if not text:
        return False
    if RACEWAY_TEXT_RE.search(text):
        return True
    return parse_item(text, str(unit or "EA")).item_class in RACEWAY_CLASSES


def _fact_texts(fact: Any) -> tuple[str, str | None]:
    if isinstance(fact, Mapping):
        statement = f"{fact.get('system') or ''} {fact.get('statement') or ''} {(fact.get('cite') or {}).get('quote') or ''}"
        return statement, fact.get("split")
    cite = getattr(fact, "cite", None)
    statement = f"{getattr(fact, 'system', '') or ''} {getattr(fact, 'statement', '') or ''} {getattr(cite, 'quote', '') or ''}"
    return statement, getattr(fact, "split", None)


def fact_excludes_raceway(fact: Any) -> bool:
    """True only when the fact itself puts the raceway on others: it names
    conduit, raceway, boxes or rough-in and does not say the EC provides
    them. A devices_by_others or raceway_by_ec split (WS5), a fact that
    names no raceway ("FA devices by others") and "EC provides conduit and
    boxes" all keep the raceway and rough-in lines with the EC."""
    statement, split = _fact_texts(fact)
    if split in ("devices_by_others", "raceway_by_ec"):
        return False
    if split == "raceway_by_others":
        return True
    if not RACEWAY_TEXT_RE.search(statement):
        return False
    return not _EC_PROVIDES_RE.search(statement)


def fact_is_existing_circuit(fact: Any) -> bool:
    """A verified fact that the new devices reuse existing circuits."""
    if isinstance(fact, Mapping):
        verified, kind = bool(fact.get("verified")), fact.get("kind")
        text = f"{fact.get('system') or ''} {fact.get('statement') or ''}"
    else:
        verified, kind = bool(getattr(fact, "verified", False)), getattr(fact, "kind", None)
        text = f"{getattr(fact, 'system', '') or ''} {getattr(fact, 'statement', '') or ''}"
    return verified and kind == "existing_to_remain" and bool(_EXISTING_CIRCUIT_RE.search(text))


def _fact_id(fact: Any) -> str | None:
    return str(fact.get("id")) if isinstance(fact, Mapping) else getattr(fact, "id", None)


def _fact_cite(fact: Any) -> dict[str, Any]:
    if isinstance(fact, Mapping):
        cite = dict(fact.get("cite") or {})
        return {"document_id": cite.get("document_id"), "page": cite.get("page"), "quote": cite.get("quote")}
    cite = getattr(fact, "cite", None)
    return {
        "document_id": getattr(cite, "document_id", None),
        "page": getattr(cite, "page", None),
        "quote": getattr(cite, "quote", None),
    }


def _fact_party(fact: Any) -> str | None:
    return fact.get("party") if isinstance(fact, Mapping) else getattr(fact, "party", None)


# Inputs -------------------------------------------------------------------------------


def _text(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _walk_strings(obj: Any, out: list[tuple[str, str]], path: str = "") -> None:
    if isinstance(obj, str):
        out.append((path, obj))
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            _walk_strings(v, out, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk_strings(v, out, f"{path}[{i}]")


def boq_lines_of(boq: Any) -> list[dict[str, Any]]:
    """BOQ lines (B rows) from a BOQ spec, a main output, or a list of
    lines; transient ids in BOQ order for a legacy spec."""
    if isinstance(boq, (list, tuple)):
        rows = [dict(r) for r in boq if isinstance(r, Mapping)]
        if not rows:
            return []
        if all(is_row_id(r.get("line_id"), BOQ_LINE_PREFIX) for r in rows):
            return rows
        spec = {"areas": [{"area": "", "sections": [{"heading": "NEW WORK", "groups": [{"name": "", "subgroups": [{"name": "", "items": rows}]}]}]}]}
        return list(rows_mod.iter_boq_lines(spec))
    return list(rows_mod.iter_rows(boq, kinds="B"))


def detect_mode(lines: Iterable[Mapping[str, Any]]) -> str:
    """`tools` when every non-accessory line carries v2 provenance."""
    seen = False
    for ln in lines:
        if ln.get("is_accessory"):
            continue
        seen = True
        if not isinstance(ln.get("source_refs"), list) or not ln.get("combine"):
            return "legacy"
    return "tools" if seen else "legacy"


@dataclass
class GateContext:
    """Everything one final-gate evaluation reads (all optional but the BOQ)."""

    run_id: str | None
    output_version: int
    mode: str
    route: str
    settings: GateSettings
    lines: list[dict[str, Any]]
    boq_spec: Mapping[str, Any] | None = None
    main: Mapping[str, Any] | None = None
    category_outputs: list[Mapping[str, Any]] = field(default_factory=list)
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    floors: list[floors_mod.Floor] = field(default_factory=list)
    facts: list[Any] = field(default_factory=list)
    dispositions: list[Mapping[str, Any]] = field(default_factory=list)
    measure: Mapping[str, Any] | None = None
    linear_evidence: frozenset[str] = frozenset()
    excluded_lines: list[Any] = field(default_factory=list)
    bands: Mapping[str, Any] | None = None
    # line -> the G3 policy inclusion or include rule id that carries it, else
    # None (policy_include_matcher); an inclusion beats an excluding fact.
    policy_includes: Any = None

    def included_by_policy(self, line: Mapping[str, Any]) -> str | None:
        if self.policy_includes is None:
            return str(line.get("scope_rule")) if line.get("scope_rule") else None
        try:
            rule = self.policy_includes(line)
        except Exception:  # noqa: BLE001 - a matcher failure never hides a finding
            return None
        return str(rule) if rule else None

    def finding(self, gate_id: str, severity: str, target: Mapping[str, Any], message: str, evidence: Mapping[str, Any] | None = None, *, route: str | None = None) -> GateFinding:
        return GateFinding(
            run_id=self.run_id, output_version=self.output_version, stage="final", gate_id=gate_id,
            severity=severity, route=route or (self.route if severity == "block" else "none"),
            target=dict(target), message=message, evidence=dict(evidence or {}),
        )

    def prose(self) -> list[tuple[str, str]]:
        texts: list[tuple[str, str]] = []
        if self.main:
            for key in ("brief", "estimate", "confidence_notes", "material_notes"):
                if key in self.main:
                    _walk_strings(self.main[key], texts, key)
            for ln in self.lines:
                for key in ("caveat", "material_note"):
                    if ln.get(key):
                        texts.append((f"boq.{ln.get('line_id')}.{key}", str(ln[key])))
        return texts

    def allowances(self) -> list[str]:
        est = (self.main or {}).get("estimate") if isinstance(self.main, Mapping) else None
        out: list[str] = []
        if isinstance(est, Mapping):
            for key in _ALLOWANCE_FIELDS:
                out.extend(str(a) for a in est.get(key) or [] if a)
        return out


@dataclass
class GateReport:
    """The result of `run_final`: findings, the gates that could not run in
    this mode, what was moved or excluded, and the `bdr.gate_report/1`."""

    findings: list[GateFinding]
    evaluated: list[str]
    not_applicable: list[str]
    mode: str
    enforced: bool
    moved_allowances: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[ExcludedLine] = field(default_factory=list)
    manual_lines_unapproved: int = 0
    output_version: int = 1
    run_id: str | None = None

    @property
    def blocking(self) -> bool:
        return any(f.blocking for f in self.findings)

    def by_gate(self) -> dict[str, list[GateFinding]]:
        out: dict[str, list[GateFinding]] = {}
        for f in self.findings:
            out.setdefault(f.gate_id, []).append(f)
        return out

    def report(self) -> dict[str, Any]:
        return gate_report_from_findings(self.findings)

    def summary(self) -> dict[str, Any]:
        """The `plans_reader_runs.gate_summary` value."""
        active = [f for f in self.findings if f.status in ACTIVE_FINDING_STATUSES]
        return {
            "schema": GATE_REPORT_SCHEMA,
            "stage": "final",
            "gates_version": GATE_VERSION,
            "output_version": self.output_version,
            "mode": self.mode,
            "enforced": self.enforced,
            "blocking": self.blocking,
            "codes": sorted({f.gate_id for f in active if f.severity == "block"}),
            "warnings": sorted({f.gate_id for f in active if f.severity == "warn"}),
            "counts": {
                "block": sum(1 for f in active if f.severity == "block"),
                "warn": sum(1 for f in active if f.severity == "warn"),
            },
            "evaluated": list(self.evaluated),
            "not_applicable": list(self.not_applicable),
            "manual_lines_unapproved": self.manual_lines_unapproved,
            "moved_allowances": [dict(m) for m in self.moved_allowances],
            "evaluated_at": now_pacific_iso(),
        }


# Line helpers -------------------------------------------------------------------------


def _identity(line: Mapping[str, Any]) -> Any:
    return parse_item(_text(line.get("description")), str(line.get("unit") or "EA"))


def _line_sheets(line: Mapping[str, Any]) -> set[str]:
    return {rows_mod.normalize_sheet_number(s) for s in line.get("sheet_refs") or [] if s}


def _section(line: Mapping[str, Any]) -> str:
    return str(line.get("section") or "").upper()


def _group(line: Mapping[str, Any]) -> str:
    return str(line.get("group") or "").upper()


def _is_new_work(line: Mapping[str, Any]) -> bool:
    return _section(line) != "DEMOLITION"


def new_device_count(lines: Iterable[Mapping[str, Any]]) -> tuple[int, list[str]]:
    """EA device lines of NEW WORK in the device groups (receptacles,
    fixtures, controls, connections): the devices branch wiring must reach."""
    total = 0.0
    ids: list[str] = []
    for ln in lines:
        if ln.get("is_accessory") or not _is_new_work(ln) or str(ln.get("unit") or "").upper() != "EA":
            continue
        if _group(ln) not in DEVICE_GROUPS:
            continue
        if _identity(ln).item_class not in DEVICE_CLASSES:
            continue
        q = _num(ln.get("quantity"))
        if q:
            total += q
            ids.append(str(ln.get("line_id")))
    return int(round(total)), ids


# Power cable that carries branch circuits (NEC chapter 3 wiring methods):
# metal-clad, armored and non-metallic sheathed cable. Control and low-voltage
# cable (#18/4C control cable, CAT6, RG6, fiber, speaker wire) is a cable
# class item but never branch wiring, so it never satisfies
# new_devices_no_branch.
_POWER_CABLE_RE = re.compile(r"\bMC\b|\bAC\b|\bNM-?B?\b|\bROMEX\b")
_POWER_CABLE_SUBS: frozenset[str] = frozenset({"mc", "ac", "nm", "nmb", "romex"})


def is_power_cable(description: Any, identity: Any = None) -> bool:
    """True for MC, AC and NM (Romex) cable: the cable kinds that count as
    branch conductor and raceway at once. Control, low-voltage, data and
    coax cable are never branch wiring."""
    text = _text(description)
    identity = identity if identity is not None else parse_item(text, "FT")
    if identity.item_class != "cable":
        return False
    sub = identity.family.split(":", 1)[1] if ":" in identity.family else ""
    if sub in _POWER_CABLE_SUBS:
        return True
    return bool(_POWER_CABLE_RE.search(text.upper()))


def branch_feet(lines: Iterable[Mapping[str, Any]], measure: Mapping[str, Any] | None = None) -> dict[str, float]:
    """FT of branch conductor, raceway and POWER cable (MC, AC, NM) in NEW
    WORK lines plus the measure output's quantity rows. Control and
    low-voltage cable (#18/4C, CAT6, any cable that is not MC, AC or NM)
    is reported under `other_cable_ft` and never counts as branch wiring."""
    out = {"conductor_ft": 0.0, "raceway_ft": 0.0, "cable_ft": 0.0}
    other = 0.0
    items: list[Mapping[str, Any]] = [ln for ln in lines if _is_new_work(ln) and not ln.get("is_accessory")]
    if isinstance(measure, Mapping):
        items.extend(q for q in measure.get("quantities") or [] if isinstance(q, Mapping))
    for ln in items:
        if str(ln.get("unit") or "").upper() != "FT":
            continue
        q = _num(ln.get("quantity")) or 0.0
        identity = _identity(ln)
        cls = identity.item_class
        if cls == "cable":
            if is_power_cable(ln.get("description"), identity):
                out["cable_ft"] += q
            else:
                other += q
        elif cls in BRANCH_CONDUCTOR_CLASSES:
            out["conductor_ft"] += q
        elif cls in BRANCH_RACEWAY_CLASSES:
            out["raceway_ft"] += q
    if other:
        out["other_cable_ft"] = other
    return out


# Gates ----------------------------------------------------------------------------------


def gate_new_devices_no_branch(ctx: GateContext) -> list[GateFinding]:
    devices, ids = new_device_count(ctx.lines)
    if devices <= 0:
        return []
    feet = branch_feet(ctx.lines, ctx.measure)
    conductor = feet["conductor_ft"] + feet["cable_ft"]
    raceway = feet["raceway_ft"] + feet["cable_ft"]
    if conductor > 0 and raceway > 0:
        return []
    satisfied_by = None
    if any(str(d.get("disposition")) == "existing_circuit" for d in ctx.dispositions if isinstance(d, Mapping)):
        satisfied_by = "disposition existing_circuit"
    else:
        fact = next((f for f in ctx.facts if fact_is_existing_circuit(f)), None)
        if fact is not None:
            satisfied_by = f"fact {_fact_id(fact)}"
    evidence = {"devices": devices, "device_line_ids": ids, **{k: round(v, 2) for k, v in feet.items()}, "satisfied_by": satisfied_by}
    if satisfied_by:
        return [ctx.finding("new_devices_no_branch", "warn", {"family": "branch"}, f"{devices} new devices on existing circuits ({satisfied_by}); no branch wiring carried", evidence)]
    return [
        ctx.finding(
            "new_devices_no_branch", "block", {"family": "branch", "row_ids": ids},
            f"{devices} EA of new devices and {conductor:g} FT of branch conductor, {raceway:g} FT of raceway: new devices need branch wiring (MC, AC or NM power cable counts as both; control cable never)",
            evidence,
        )
    ]


def _linear_kind(identity: Any) -> str:
    fam = identity.family
    return fam.split(":", 1)[1] if ":" in fam else fam


def gate_linear_in_ea(ctx: GateContext) -> list[GateFinding]:
    out: list[GateFinding] = []
    for ln in ctx.lines:
        if ln.get("is_accessory"):
            continue
        unit = str(ln.get("unit") or "").upper()
        if unit == "FT":
            continue
        identity = _identity(ln)
        lid = str(ln.get("line_id"))
        if is_linear(identity):
            label = identity.item_class if identity.item_class != "demolition" else f"demolition of {identity.attrs.get('subject')}"
            out.append(
                ctx.finding(
                    "linear_in_ea", "block", {"line_id": lid, "item_class": label},
                    f"{lid} {_text(ln.get('description'))[:80]}: {label} is a linear class and must be FT, not {unit}",
                    {"unit": unit, "quantity": ln.get("quantity"), "item_class": label},
                )
            )
            continue
        if linear_evidence_required(identity) and _is_new_work(ln):
            # The demolition of a linear luminaire stays EA (estimators remove
            # fixtures by the each); only new work needs the per-foot evidence.
            kind = _linear_kind(identity)
            tag = str(identity.attrs.get("tag") or "").upper()
            evidence_ft = tag in ctx.linear_evidence or kind in ctx.linear_evidence or lid in ctx.linear_evidence
            share = float(ctx.settings.ft_share.get(kind, 0.0))
            blocks = evidence_ft or share >= LINEAR_BLOCK_SHARE
            why = "the run carries per-foot evidence for it" if evidence_ft else (
                f"estimators price this kind in FT {share:.0%} of the time" if blocks else f"estimators price this kind in FT only {share:.0%} of the time; EA allowed with a warning"
            )
            out.append(
                ctx.finding(
                    "linear_in_ea", "block" if blocks else "warn", {"line_id": lid, "item_class": f"luminaire_linear:{kind}"},
                    f"{lid} {_text(ln.get('description'))[:80]}: a linear luminaire in {unit}; {why}",
                    {"unit": unit, "quantity": ln.get("quantity"), "kind": kind, "tag": tag or None, "ft_share": share, "evidence": evidence_ft},
                )
            )
    return out


def gate_unit_class(ctx: GateContext) -> list[GateFinding]:
    out: list[GateFinding] = []
    for ln in ctx.lines:
        if ln.get("is_accessory"):
            continue
        unit = str(ln.get("unit") or "").upper()
        identity = _identity(ln)
        lid = str(ln.get("line_id"))
        if unit == "FT" and identity.item_class in COUNT_CLASSES:
            out.append(ctx.finding("unit_class", "block", {"line_id": lid}, f"{lid} {_text(ln.get('description'))[:80]}: a {identity.item_class} is counted in EA, not FT", {"unit": unit, "item_class": identity.item_class}))
        elif unit in ("CU FT", "CY", "SQ FT") and identity.item_class not in ("trench", "sawcut", "other", "allowance"):
            out.append(ctx.finding("unit_class", "warn", {"line_id": lid}, f"{lid} {_text(ln.get('description'))[:80]}: {unit} on a {identity.item_class} line", {"unit": unit, "item_class": identity.item_class}))
    return out


def census_checks(ctx: GateContext) -> list[dict[str, Any]]:
    """Every known floor against the BOQ lines (legacy: EA by sheet_refs;
    tools: instances), with its status."""
    out: list[dict[str, Any]] = []
    mode = "legacy" if ctx.mode == "legacy" else "v2"
    for fl in ctx.floors:
        if not fl.known:
            continue
        acc = floors_mod.accounted(fl, ctx.lines, mode=mode)
        status = floors_mod.census_status(fl, acc, settings=ctx.settings.as_floor_settings())
        out.append({"floor_obj": fl, "accounted_obj": acc, **status})
    return out


def gate_census_floor(ctx: GateContext) -> list[GateFinding]:
    out: list[GateFinding] = []
    for check in census_checks(ctx):
        if check["status"] != "short":
            continue
        fl: floors_mod.Floor = check["floor_obj"]
        acc: floors_mod.Accounted = check["accounted_obj"]
        if fl.family in ctx.settings.blocking_families:
            severity = "block"
        elif fl.family in ctx.settings.warn_families:
            severity = "warn"
        else:
            continue
        out.append(
            ctx.finding(
                "census_floor", severity, {"sheet_key": fl.sheet_key, "family": fl.key, "row_ids": acc.ids},
                f"{fl.sheet_number or fl.sheet_key} {fl.key}: {acc.count} accounted against a printed floor of {fl.floor} (tolerance {check['tolerance']}); the sheet is missing devices",
                {"floor": fl.floor, "accounted": acc.count, "tolerance": check["tolerance"], "strength": fl.strength, "sheet_number": fl.sheet_number, "items": acc.items},
            )
        )
    return out


def _partial_rows_by_sheet(ctx: GateContext) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows_mod.iter_rows(ctx.category_outputs, kinds="C"):
        if row.get("status") != "active" or row.get("coverage") not in rows_mod.INCOMPLETE_COVERAGES:
            continue
        number = rows_mod.normalize_sheet_number((row.get("sheet") or {}).get("sheet_number"))
        if number:
            out.setdefault(number, []).append(row)
    return out


def gate_partial_basis(ctx: GateContext) -> list[GateFinding]:
    """Lines whose count rests on a tile or partial read. Tools mode: the
    line's own `needs_recount` or a `partial` basis. Legacy mode: a note that
    says so, or a merged row with incomplete coverage on the same sheet with
    a matching description. One finding per sheet (one recount target)."""
    hits: dict[str, list[dict[str, Any]]] = {}
    partial_rows = _partial_rows_by_sheet(ctx) if ctx.mode == "legacy" else {}
    for ln in ctx.lines:
        if ln.get("is_accessory"):
            continue
        lid = str(ln.get("line_id"))
        reasons: list[str] = []
        if ctx.mode == "tools":
            if ln.get("needs_recount") or str(ln.get("basis") or "") == "partial":
                reasons.append(f"basis {ln.get('basis')}, needs_recount")
        else:
            note = f"{ln.get('material_note') or ''} {ln.get('caveat') or ''}"
            m = PARTIAL_NOTE_RE.search(note)
            if m:
                reasons.append(f"note says {m.group(0)!r}")
            unit = str(ln.get("unit") or "EA").upper()
            for number in _line_sheets(ln):
                for row in partial_rows.get(number, []):
                    if str(row.get("unit") or "EA").upper() != unit:
                        continue
                    a = BoqRow("", "", "", "", _text(ln.get("description")), 0.0, unit)
                    b = BoqRow("", "", "", "", _text(row.get("description")), 0.0, unit)
                    if normalize_description(a.description) == normalize_description(b.description) or similarity(a, b) >= PARTIAL_MATCH_THRESHOLD:
                        reasons.append(f"row {row.get('row_id')} coverage {row.get('coverage')}")
        if not reasons:
            continue
        sheets = sorted(_line_sheets(ln)) or ["?"]
        hits.setdefault(sheets[0], []).append({"line_id": lid, "description": _text(ln.get("description"))[:80], "quantity": ln.get("quantity"), "reasons": reasons})
    out: list[GateFinding] = []
    for number, items in hits.items():
        sheet_key = None
        for row in rows_mod.iter_rows(ctx.category_outputs, kinds="C"):
            if rows_mod.normalize_sheet_number((row.get("sheet") or {}).get("sheet_number")) == number:
                sheet_key = (row.get("sheet") or {}).get("sheet_key")
                break
        ids = [i["line_id"] for i in items]
        out.append(
            ctx.finding(
                "partial_basis", "block", {"sheet_key": sheet_key, "sheet_number": number, "row_ids": ids},
                f"{number}: {', '.join(ids)} rest on a tile or partial read ({items[0]['description']}); a tile subtotal is a floor, not a sheet total",
                {"lines": items},
            )
        )
    return out


def capability_denial_hits(texts: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for path, text in texts:
        for rx in _DENIAL_RES:
            if rx.search(text):
                hits.append({"field": path, "pattern": rx.pattern, "text": text[:160]})
    return hits


def gate_capability_denial(ctx: GateContext) -> list[GateFinding]:
    hits = capability_denial_hits(ctx.prose())
    if not hits:
        return []
    fields = sorted({h["field"].split("[")[0] for h in hits})
    return [
        ctx.finding(
            "capability_denial", "block", {"field": "prose"},
            f"{len(hits)} sentence(s) deny a capability the pipeline has (measurement): " + "; ".join(sorted({h['pattern'] for h in hits})),
            {"hits": hits, "fields": fields},
        )
    ]


_CONTROL_CABLE_TEXT_RE = re.compile(r"#\s?18\s*/\s*4\s*C|\bcontrol\s+cable\b|\bthermostat\s+cable\b", re.I)


def policy_include_matcher(scope_context: Mapping[str, Any] | None, policy: Mapping[str, Any] | None = None) -> Any:
    """A `line -> rule id or None` callable: the G3 policy inclusion of the
    run's scope context (scope_engine.compute_inclusions: pattern,
    also_pattern, line template) or the include rule of the scope policy
    (decision include) that carries the line. The same precedence as
    scope_engine.fact_exclusions ("an include rule beats a fact", owner
    decision Q14: thermostat relocations and control cable are carried
    even when a verified fact assigns ATC wiring to the controls trade):
    the consolidator does not refuse such a line with excluded_by_fact and
    gate excluded_system records a fact_conflict warning for a human
    instead of a block. A line that carries `scope_rule` (an engine insert
    or canonicalization) names its rule itself. `policy` defaults to the
    active policy (the code seed when none is pinned or loaded)."""
    from app.services import scope_policy

    incs: list[tuple[str, Any, Any, str, str, bool]] = []
    for inc in (scope_context or {}).get("inclusions") or []:
        if not isinstance(inc, Mapping) or not inc.get("rule_id"):
            continue
        try:
            pat = scope_policy.compile_pattern(str(inc.get("pattern") or r"\bx\b"))
            also = scope_policy.compile_pattern(str(inc["also_pattern"])) if inc.get("also_pattern") else None
        except Exception:  # noqa: BLE001 - a bad stored pattern never matches
            continue
        canon = re.sub(r"\s+", " ", _text(inc.get("description")).upper())
        incs.append((str(inc["rule_id"]), pat, also, canon, str(inc.get("unit") or "EA").upper(),
                     bool(inc.get("companion_of")) or str(inc.get("trigger_family") or "") == "thermostat"))
    doc: Mapping[str, Any] = policy if policy is not None else {}
    if policy is None:
        try:
            doc = scope_policy.active_policy().doc
        except Exception:  # noqa: BLE001 - no policy reachable: inclusions of the context only
            doc = {}
    rules = [r for r in doc.get("rules") or [] if isinstance(r, Mapping) and r.get("decision") == "include"
             and scope_policy.rule_enabled(r)]

    def match(line: Mapping[str, Any]) -> str | None:
        if line.get("scope_rule"):
            return str(line["scope_rule"])
        desc = _text(line.get("description"))
        if not desc:
            return None
        unit = str(line.get("unit") or "EA").upper()
        norm = re.sub(r"\s+", " ", desc.upper())
        for rid, pat, also, canon, inc_unit, any_unit in incs:
            if canon and norm == canon:
                return rid
            if not any_unit and unit != inc_unit:
                continue
            if pat.search(desc) and (also is None or also.search(desc)):
                return rid
            if _CONTROL_CABLE_TEXT_RE.search(desc) and canon and _CONTROL_CABLE_TEXT_RE.search(canon):
                return rid
        for r in rules:
            if scope_policy.match_rule(r, desc, subgroup=_text(line.get("subgroup")) or None, group=_text(line.get("group")) or None):
                return str(r.get("rule_id"))
        return None

    return match


def gate_excluded_system(ctx: GateContext) -> tuple[list[GateFinding], list[dict[str, Any]], list[ExcludedLine]]:
    out: list[GateFinding] = []
    moved: list[dict[str, Any]] = []
    excluded: list[ExcludedLine] = []
    facts = [f for f in ctx.facts if fact_excludes(f)]
    if not facts:
        return out, moved, excluded
    for ln in ctx.lines:
        if ln.get("is_accessory"):
            continue
        systems = systems_of(ln.get("description"))
        if not systems or systems & {"equipment_connection", "rough_in"}:
            continue
        raceway = is_raceway_text(ln.get("description"), ln.get("unit"))
        for fact in facts:
            hit = systems & fact_systems(fact)
            if not hit:
                continue
            if raceway and not fact_excludes_raceway(fact):
                continue  # conduit, boxes, circuits and connections stay EC work
            lid = str(ln.get("line_id"))
            fid = _fact_id(fact)
            cite = _fact_cite(fact)
            rule = ctx.included_by_policy(ln)
            if rule:
                # A G3 policy inclusion beats the fact (Q14); the conflict is
                # recorded for a human, the line stays.
                out.append(
                    ctx.finding(
                        "fact_conflict", "warn", {"line_id": lid, "fact_id": fid, "system": sorted(hit)[0], "rule_id": rule},
                        f"{lid} {_text(ln.get('description'))[:80]}: carried by G3 policy inclusion {rule} while verified fact {fid} "
                        f"excludes {sorted(hit)[0]} ({_fact_party(fact) or 'others'}); a human confirms the inclusion",
                        {"systems": sorted(hit), "fact": fid, "cite": cite, "rule_id": rule},
                    )
                )
                break
            out.append(
                ctx.finding(
                    "excluded_system", "block", {"line_id": lid, "fact_id": fid, "system": sorted(hit)[0]},
                    f"{lid} {_text(ln.get('description'))[:80]}: {sorted(hit)[0]} is excluded by verified fact {fid} ({_fact_party(fact) or 'others'})",
                    {"systems": sorted(hit), "fact": fid, "cite": cite},
                )
            )
            q = _num(ln.get("quantity")) or 0.0
            try:
                excluded.append(
                    ExcludedLine(
                        line_id=lid, description=_text(ln.get("description")), quantity=q,
                        unit=str(ln.get("unit") or "EA"), system=sorted(hit)[0],
                        reason=f"excluded by verified fact {fid}", source="fact", party=_fact_party(fact), fact_id=fid,
                    )
                )
            except ContractError:
                pass
            break
    for name in ctx.allowances():
        systems = systems_of(name)
        if not systems or systems & {"equipment_connection", "rough_in"}:
            continue
        raceway = is_raceway_text(name)
        for fact in facts:
            hit = systems & fact_systems(fact)
            if not hit:
                continue
            if raceway and not fact_excludes_raceway(fact):
                continue
            fid = _fact_id(fact)
            cite = _fact_cite(fact)
            moved.append({"allowance": name, "systems": sorted(hit), "fact_id": fid, "party": _fact_party(fact), "cite": cite})
            out.append(
                ctx.finding(
                    "excluded_system", "warn", {"field": "estimate.extra_allowances", "allowance": name, "fact_id": fid},
                    f"allowance {name!r} moved to the exclusions: {', '.join(sorted(hit))} by {_fact_party(fact) or 'others'} per verified fact {fid}",
                    {"moved": True, "systems": sorted(hit), "fact": fid, "cite": cite},
                )
            )
            break
    return out, moved, excluded


def gate_fact_conflict(ctx: GateContext) -> list[GateFinding]:
    """The brief says a system is in scope that a verified fact excludes."""
    out: list[GateFinding] = []
    facts = [f for f in ctx.facts if fact_excludes(f)]
    brief = (ctx.main or {}).get("brief") if isinstance(ctx.main, Mapping) else None
    if not facts or not isinstance(brief, Mapping):
        return out
    for i, entry in enumerate(brief.get("systems") or []):
        if not isinstance(entry, Mapping) or entry.get("in_scope") != "yes":
            continue
        systems = systems_of(entry.get("system"))
        for fact in facts:
            hit = systems & fact_systems(fact)
            if hit:
                fid = _fact_id(fact)
                out.append(
                    ctx.finding(
                        "fact_conflict", "warn", {"field": f"brief.systems[{i}]", "fact_id": fid, "system": sorted(hit)[0]},
                        f"brief marks {entry.get('system')!r} in scope while verified fact {fid} excludes {sorted(hit)[0]}",
                        {"systems": sorted(hit), "fact": fid, "cite": _fact_cite(fact)},
                    )
                )
                break
    return out


def _refs_of(line: Mapping[str, Any]) -> list[str]:
    out = []
    for item in line.get("source_refs") or []:
        if isinstance(item, Mapping) and item.get("ref"):
            out.append(str(item["ref"]).split("#", 1)[0])
        elif isinstance(item, str):
            out.append(item.split("#", 1)[0])
    return out


def _measure_row_order(rid: str) -> tuple[int, str]:
    tail = rid[1:]
    return (int(tail) if tail.isdigit() else 0, rid)


def unconsumed_measure_rows(rows: Mapping[str, Mapping[str, Any]], *, consumed: Iterable[str] = (), disposed: Iterable[str] = ()) -> list[str]:
    """Active M rows with a positive quantity that no line cites (`consumed`:
    refs with any `#` suffix stripped) and no disposition or exclusion record
    covers (`disposed`), in M order."""
    used = set(consumed) | set(disposed)
    out: list[str] = []
    for rid, row in rows.items():
        if not rid or rid[0] != "M" or not isinstance(row, Mapping):
            continue
        if str(row.get("status") or "active") != "active":
            continue
        if (_num(row.get("quantity")) or 0.0) <= 0:
            continue
        if rid in used:
            continue
        out.append(rid)
    return sorted(out, key=_measure_row_order)


def gate_provenance(ctx: GateContext) -> list[GateFinding]:
    """The gates that need v2 lines and rows (tools mode only)."""
    out: list[GateFinding] = []
    consumed: dict[str, list[str]] = {}
    # A split line (enforce moved part of a parent's quantity to it, DEV-TR)
    # cites its parent's rows: the pair linked from both sides consumes
    # them once. The quantities must still add up (recheck below).
    pairs = lines_mod.split_pairs(ctx.lines)
    for ln in ctx.lines:
        if ln.get("is_accessory"):
            continue
        lid = str(ln.get("line_id"))
        combine = str(ln.get("combine") or "")
        refs = _refs_of(ln)
        if combine != "manual" and not refs:
            out.append(ctx.finding("unsourced_line", "block", {"line_id": lid}, f"{lid} {_text(ln.get('description'))[:80]} cites no source row", {}))
            continue
        consumer = pairs.get(lid, lid)
        for ref in refs:
            consumed.setdefault(ref, []).append(consumer)
            if not is_row_id(ref):
                continue
            row = ctx.rows.get(ref)
            if row is None:
                out.append(ctx.finding("unverifiable_citation", "block", {"line_id": lid, "ref": ref}, f"{lid} cites {ref}, which is not a row of this run", {}))
            elif row.get("status") in ("unverifiable", "superseded", "alternate"):
                out.append(ctx.finding("unverifiable_citation", "block", {"line_id": lid, "ref": ref}, f"{lid} cites {ref}, a row with status {row.get('status')}", {"status": row.get("status")}))
    for ref, holders in consumed.items():
        if len(set(holders)) > 1 and is_row_id(ref) and ref[0] in ("C", "M", "S"):
            out.append(ctx.finding("double_consumption", "block", {"ref": ref, "row_ids": sorted(set(holders))}, f"{ref} is consumed by {', '.join(sorted(set(holders)))}", {}))
    disposed = {str(r) for d in ctx.dispositions if isinstance(d, Mapping) for r in d.get("row_ids") or []}
    # A line that enforce moved to excluded_lines (policy, fact or human) still
    # accounts for the rows it consumed: the exclusion record carries the
    # original item (`item`) or its refs.
    for ex in ctx.excluded_lines:
        if not isinstance(ex, Mapping):
            ex = getattr(ex, "to_dict", lambda: {})() if hasattr(ex, "to_dict") else {}
        item = ex.get("item") if isinstance(ex.get("item"), Mapping) else ex
        disposed.update(_refs_of(item))
    orphans: list[str] = []
    sheets_with_rows: dict[str, list[str]] = {}
    sheets_used: set[str] = set()
    for rid, row in ctx.rows.items():
        if rid[0] != "C" or row.get("status") != "active":
            continue
        key = str((row.get("sheet") or {}).get("sheet_key") or "")
        sheets_with_rows.setdefault(key, []).append(rid)
        if rid in consumed or rid in disposed:
            sheets_used.add(key)
        elif row.get("coverage") == "sheet_total":
            orphans.append(rid)
    if orphans:
        out.append(ctx.finding("orphan_rows", "block", {"row_ids": sorted(orphans)}, f"{len(orphans)} active count rows neither consumed by a line nor accounted for: {', '.join(sorted(orphans)[:12])}", {"row_ids": sorted(orphans)}))
    # Measure quantity rows (M: the app's own lengths, counts and policy
    # rows) are consumed or accounted like count rows: an M row no line
    # cites and no disposition covers is a quantity the BOQ silently lost
    # (7154 run 51ec302b: M5, the measured track demolition, must reach a
    # line). Same gate id, its own target, so the finish report lists the
    # rows with the call that resolves each.
    measure_orphans = unconsumed_measure_rows(ctx.rows, consumed=consumed, disposed=disposed)
    if measure_orphans:
        out.append(ctx.finding(
            "orphan_rows", "block", {"row_ids": measure_orphans, "kind": "measure"},
            f"{len(measure_orphans)} measure quantity rows (M) neither consumed by a line nor accounted for: {', '.join(measure_orphans[:12])}",
            {"row_ids": measure_orphans, "kind": "measure"},
        ))
    lost = sorted(k for k, ids in sheets_with_rows.items() if k and k not in sheets_used)
    if lost:
        out.append(ctx.finding("lost_sheets", "block", {"sheet_keys": lost}, f"sheets with rows that no line or disposition uses: {', '.join(lost)}", {"sheets": {k: sheets_with_rows[k] for k in lost}}))
    for problem in lines_mod.recheck(ctx.lines, ctx.rows):
        lid = str(problem.get("line_id"))
        if is_row_id(lid, BOQ_LINE_PREFIX):
            out.append(ctx.finding("arithmetic", "block", {"line_id": lid}, f"{lid}: {problem.get('message')}", {k: v for k, v in problem.items() if k != "line_id"}))
    out.extend(gate_brief_numbers(ctx))
    return out


def tag_totals(lines: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """EA totals per luminaire tag (L1, X1) over NEW WORK fixture lines."""
    out: dict[str, float] = {}
    for ln in lines:
        if not _is_new_work(ln) or str(ln.get("unit") or "").upper() != "EA":
            continue
        identity = _identity(ln)
        tag = identity.attrs.get("tag")
        if tag and identity.item_class in ("luminaire", "luminaire_linear", "lighting_track"):
            out[str(tag).upper()] = out.get(str(tag).upper(), 0.0) + (_num(ln.get("quantity")) or 0.0)
    return out


def receptacle_total(lines: Iterable[Mapping[str, Any]]) -> float:
    return sum(
        _num(ln.get("quantity")) or 0.0
        for ln in lines
        if _is_new_work(ln) and str(ln.get("unit") or "").upper() == "EA"
        and (_identity(ln).item_class == "receptacle" or re.search(r"(?i)\brecept", _text(ln.get("description"))))
    )


def brief_number_claims(main: Mapping[str, Any] | None, lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Numeric claims in the brief that disagree with the BOQ totals: "N <tag>"
    luminaire claims and "N receptacles"."""
    brief = (main or {}).get("brief") if isinstance(main, Mapping) else None
    if not isinstance(brief, Mapping):
        return []
    texts: list[tuple[str, str]] = []
    for field_name in _BRIEF_FIELDS:
        if field_name in brief:
            _walk_strings(brief[field_name], texts, f"brief.{field_name}")
    tags = tag_totals(lines)
    recept = receptacle_total(lines)
    problems: list[dict[str, Any]] = []
    for path, text in texts:
        for m in _TAG_CLAIM_RE.finditer(text):
            n = _NUMBER_WORDS.get(m.group(1).lower()) or (int(m.group(1)) if m.group(1).isdigit() else None)
            tag = m.group(2).upper()
            if n is None or tag not in tags:
                continue
            if abs(tags[tag] - n) > 1e-6:
                problems.append({"field": path, "claim": m.group(0), "tag": tag, "claimed": n, "total": tags[tag]})
        for m in _RECEPT_CLAIM_RE.finditer(text):
            n = int(m.group(1))
            if recept and abs(recept - n) > 1e-6:
                problems.append({"field": path, "claim": m.group(0), "tag": "receptacles", "claimed": n, "total": recept})
    return problems


def gate_brief_numbers(ctx: GateContext) -> list[GateFinding]:
    problems = brief_number_claims(ctx.main, ctx.lines)
    if not problems:
        return []
    return [
        ctx.finding(
            "brief_numbers", "block", {"field": "brief"},
            "brief numbers disagree with the BOQ totals: " + "; ".join(f"{p['claim']} vs {p['total']:g}" for p in problems[:6]),
            {"claims": problems},
        )
    ]


def gate_branch_ratio(ctx: GateContext) -> list[GateFinding]:
    """Warn when branch FT per device sits outside the history band."""
    if not isinstance(ctx.bands, Mapping):
        return []
    devices, _ids = new_device_count(ctx.lines)
    if devices <= 0:
        return []
    feet = branch_feet(ctx.lines, ctx.measure)
    out: list[GateFinding] = []
    for metric, value in (("branch_wire_ft_per_device", feet["conductor_ft"] + feet["cable_ft"]), ("emt_ft_per_device", feet["raceway_ft"] + feet["cable_ft"])):
        band = ctx.bands.get(metric)
        if not isinstance(band, Mapping) or value <= 0:
            continue
        ratio = value / devices
        lo, hi = _num(band.get("p10")), _num(band.get("p90"))
        if (lo is not None and ratio < lo) or (hi is not None and ratio > hi):
            out.append(ctx.finding("branch_ratio", "warn", {"family": metric}, f"{metric} {ratio:.1f} outside the history band p10 {lo} to p90 {hi}", {"ratio": round(ratio, 2), "p10": lo, "p90": hi, "devices": devices}))
    return out


# run_final ------------------------------------------------------------------------------


def _coerce_floors(floors: Any, category_outputs: Iterable[Mapping[str, Any]]) -> list[floors_mod.Floor]:
    out: list[floors_mod.Floor] = []
    if floors:
        for f in floors:
            out.append(f if isinstance(f, floors_mod.Floor) else floors_mod.Floor.from_dict(f))
        return out
    for cat in category_outputs:
        if isinstance(cat, Mapping):
            for f in cat.get("floors") or []:
                if isinstance(f, Mapping):
                    out.append(floors_mod.Floor.from_dict(f))
    return out


def _index_rows_with_aliases(cats: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The row index the consolidator session used: rows_mod.index_rows
    (first row of an id wins) plus the rows a later category lost to an id
    collision, re-numbered on the run sequence (plans_reader_consolidator
    .index_sources), so a line citing a session id verifies. Falls back to
    the plain index when the consolidator is unavailable."""
    try:
        from app.services import plans_reader_consolidator as consolidator

        rows, _aliases = consolidator.index_sources(list(cats))
        return dict(rows)
    except Exception:  # noqa: BLE001 - the plain index is the legacy behaviour
        return rows_mod.index_rows(cats)


def run_final(
    *,
    boq: Any,
    run: Any = None,
    main: Mapping[str, Any] | None = None,
    category_outputs: Iterable[Mapping[str, Any]] = (),
    floors: Any = None,
    facts: Iterable[Any] = (),
    dispositions: Iterable[Mapping[str, Any]] = (),
    measure: Mapping[str, Any] | None = None,
    linear_evidence: Iterable[str] = (),
    excluded_lines: Iterable[Any] = (),
    mode: str | None = None,
    route: str = "human",
    settings: Any = None,
    blocking: bool | None = None,
    output_version: int = 1,
    bands: Mapping[str, Any] | None = None,
    policy_includes: Any = None,
) -> GateReport:
    """Evaluate the final gates (5.4 ids) over one run's outputs.

    `boq` is the BOQ spec, the main output, or a list of lines; `run` the
    run row or id; `category_outputs` the v2 (or v1) category outputs;
    `floors` the census floors (else read from the outputs); `facts` the
    WS5 facts (F rows); `dispositions` the account_rows records; `measure`
    the WS3 output; `linear_evidence` tags or kinds with per-foot evidence;
    `mode` tools or legacy (detected from the lines when None); `route`
    model_fix (inside the consolidator loop) or human; `blocking`
    overrides the plans_reader_gates_blocking setting; `policy_includes`
    the `policy_include_matcher` of the run (a policy inclusion beats an
    excluding fact: fact_conflict warn instead of excluded_system block).
    """
    if route not in ROUTES_FINAL:
        raise ContractError(f"route must be one of {ROUTES_FINAL}")
    gs = GateSettings.load(settings, blocking=blocking)
    lines = boq_lines_of(boq)
    if main is None and isinstance(boq, Mapping) and "brief" in boq:
        main = boq
    cats = [c for c in category_outputs if isinstance(c, Mapping)]
    chosen_mode = mode or detect_mode(lines)
    if chosen_mode not in MODES:
        raise ContractError(f"mode must be one of {MODES}")
    run_id = run if isinstance(run, str) else (str(run.get("id")) if isinstance(run, Mapping) and run.get("id") else getattr(run, "id", None))
    rows_index = _index_rows_with_aliases(cats) if cats else {}
    if isinstance(measure, Mapping):
        for m in rows_mod.iter_measure_rows(measure):
            rows_index.setdefault(m["row_id"], m)
    # Tools mode: the consolidator's provenance block carries the WS5 policy
    # rows (breaker plan as M rows with basis policy) and the account_rows
    # dispositions; both are read from the main output when the caller
    # passes none, so the render-time gates see what finish() saw.
    provenance = main.get("consolidator") if isinstance(main, Mapping) and isinstance(main.get("consolidator"), Mapping) else {}
    for r in provenance.get("policy_rows") or []:
        if isinstance(r, Mapping) and is_row_id(r.get("row_id")):
            rows_index.setdefault(str(r["row_id"]), dict(r))
    dispositions_list = [d for d in dispositions if isinstance(d, Mapping)]
    if not dispositions_list and isinstance(main, Mapping):
        dispositions_list = [d for d in main.get("dispositions") or [] if isinstance(d, Mapping)]
    ctx = GateContext(
        run_id=run_id, output_version=output_version, mode=chosen_mode, route=route, settings=gs, lines=lines,
        boq_spec=rows_mod._boq_spec(boq) if isinstance(boq, Mapping) else None, main=main, category_outputs=cats,
        rows=rows_index, floors=_coerce_floors(floors, cats), facts=list(facts), dispositions=dispositions_list,
        measure=measure, linear_evidence=frozenset(str(x).upper() if len(str(x)) <= 3 else str(x) for x in linear_evidence),
        excluded_lines=list(excluded_lines), bands=bands, policy_includes=policy_includes,
    )
    findings: list[GateFinding] = []
    evaluated: list[str] = []
    not_applicable: list[str] = []
    steps: list[tuple[str, Any]] = [
        ("new_devices_no_branch", gate_new_devices_no_branch),
        ("linear_in_ea", gate_linear_in_ea),
        ("unit_class", gate_unit_class),
        ("census_floor", gate_census_floor if ctx.floors else None),
        ("partial_basis", gate_partial_basis),
        ("capability_denial", gate_capability_denial),
    ]
    for gate_id, fn in steps:
        if fn is None:
            not_applicable.append(gate_id)
            continue
        findings += fn(ctx)
        evaluated.append(gate_id)
    excluded_findings, moved, excluded = gate_excluded_system(ctx)
    findings += excluded_findings
    evaluated.append("excluded_system")
    findings += gate_fact_conflict(ctx)
    evaluated.append("fact_conflict")
    if chosen_mode == "tools":
        findings += gate_provenance(ctx)
        evaluated.extend(PROVENANCE_GATES)
    else:
        not_applicable.extend(PROVENANCE_GATES)
    if bands is not None:
        findings += gate_branch_ratio(ctx)
        evaluated.append("branch_ratio")
    else:
        not_applicable.append("branch_ratio")
    manual = sum(1 for ln in lines if str(ln.get("combine") or "") == "manual" and not ln.get("approved_by"))
    # One finding per (gate, target) fingerprint.
    unique: dict[str, GateFinding] = {}
    for f in findings:
        unique.setdefault(f.fingerprint, f)
    return GateReport(
        findings=list(unique.values()), evaluated=evaluated, not_applicable=not_applicable, mode=chosen_mode,
        enforced=gs.blocking, moved_allowances=moved, excluded=excluded, manual_lines_unapproved=manual,
        output_version=output_version, run_id=run_id,
    )


# pre_consolidation -------------------------------------------------------------------------


def pre_consolidation(
    category_outputs: Iterable[Mapping[str, Any]],
    floors: Any = None,
    *,
    settings: Any = None,
    page_facts: Mapping[Any, Any] | None = None,
    max_targets: int | None = None,
) -> list[dict[str, Any]]:
    """The pre-gates of the reading stage: census_floor and partial_basis
    over the merged v2 rows, returning recount TARGETS only (nothing is
    persisted and no number changes). Each target: target_id, gate_id,
    sheet_key, document_id, page, sheet_number, family, floor, accounted,
    row_ids, regions (code regions for the recount), priority. Census
    targets come only from `recount_families` (RECOUNT_FAMILIES), a choice
    separate from the finding severities."""
    gs = GateSettings.load(settings)
    cats = [c for c in category_outputs if isinstance(c, Mapping)]
    fl_list = _coerce_floors(floors, cats)
    rows = [r for r in rows_mod.iter_rows(cats, kinds="C")]
    facts_index: dict[tuple[str, int], Any] = {}
    for key, value in (page_facts or {}).items():
        if isinstance(key, tuple) and len(key) == 2:
            facts_index[(str(key[0]), int(key[1]))] = value
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str]] = set()
    admitted = set(gs.recount_families)
    for fl in fl_list:
        if not fl.known or fl.family not in admitted:
            continue
        acc = floors_mod.accounted(fl, rows, mode="v2")
        status = floors_mod.census_status(fl, acc, settings=gs.as_floor_settings())
        if status["status"] != "short":
            continue
        page = facts_index.get((str(fl.document_id), int(fl.page or 0)))
        regions = for_recount(fl.sheet_key or "", fl.key, [fl], page=page, settings=settings)
        targets.append(
            {
                "target_id": f"T{len(targets) + 1}", "gate_id": "census_floor", "sheet_key": fl.sheet_key,
                "document_id": fl.document_id, "page": fl.page, "sheet_number": fl.sheet_number, "family": fl.key,
                "floor": fl.floor, "accounted": acc.count, "tolerance": status["tolerance"], "strength": fl.strength,
                "row_ids": acc.ids, "regions": [r.to_dict() for r in regions],
                "priority": (0 if fl.strength == "strong" else 1, -((fl.floor or 0) - (acc.count or 0))),
            }
        )
        seen.add((fl.sheet_key, fl.key))
    by_sheet: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("status") != "active" or row.get("coverage") not in rows_mod.INCOMPLETE_COVERAGES:
            continue
        sheet = row.get("sheet") or {}
        by_sheet.setdefault((str(sheet.get("sheet_key") or ""), sheet.get("sheet_number")), []).append(row)
    for (sheet_key, number), group in by_sheet.items():
        fams = {t for r in group for t in r.get("family_tags") or []}
        if any((sheet_key, f.split("@", 1)[0]) in seen for f in fams):
            continue
        first = group[0]
        family = sorted(f.split("@", 1)[0] for f in fams)[0] if fams else None
        page = facts_index.get((str(first.get("document_id")), int(first.get("page") or 0)))
        regions = for_recount(sheet_key, family, fl_list, page=page, settings=settings) if family and sheet_key else []
        floor_obj = next((f for f in fl_list if f.sheet_key == sheet_key and f.key == family and f.known), None)
        targets.append(
            {
                "target_id": f"T{len(targets) + 1}", "gate_id": "partial_basis", "sheet_key": sheet_key or None,
                "document_id": first.get("document_id"), "page": first.get("page"), "sheet_number": number,
                "family": family, "floor": floor_obj.floor if floor_obj else None,
                "accounted": None, "tolerance": None, "strength": floor_obj.strength if floor_obj else None,
                "row_ids": [r.get("row_id") for r in group], "regions": [r.to_dict() for r in regions],
                "priority": (2, -len(group)),
            }
        )
    targets.sort(key=lambda t: t["priority"])
    for i, t in enumerate(targets, start=1):
        t["target_id"] = f"T{i}"
        t["priority"] = i
    if max_targets is not None:
        targets = targets[:max_targets]
    return targets


# Persistence ----------------------------------------------------------------------------------


def _row_for_insert(finding: GateFinding) -> dict[str, Any]:
    """The table row without null values (MC rule: new columns are omitted
    when the value is null)."""
    return {k: v for k, v in finding.to_row().items() if v is not None}


def record_findings(sb: Any, run: Any, findings: Sequence[GateFinding], stage: str) -> list[GateFinding]:
    """Upsert findings on (run_id, output_version, stage, fingerprint).

    An existing row keeps its human status (resolved, overridden) and its
    resolution fields; message, evidence, severity, route and target are
    refreshed. Open or routed rows of the same run, version and stage that
    the new pass no longer raises become `superseded`. Returns the findings
    as stored. `sb` is the Supabase client (sync; callers are plain def).
    """
    validate_finding_stage(stage)
    stamped = _stamp(run, findings, stage)
    run_id = run if isinstance(run, str) else ((run or {}).get("id") if isinstance(run, Mapping) else getattr(run, "id", None))
    if run_id is None and stamped:
        run_id = stamped[0].run_id
    if run_id is None:
        raise ContractError("record_findings needs a run id")
    foreign = sorted({f.run_id for f in stamped if f.run_id is not None and str(f.run_id) != str(run_id)})
    if foreign:
        raise ContractError(f"findings of run(s) {', '.join(str(x)[:8] for x in foreign)} cannot be recorded on run {str(run_id)[:8]}")
    versions = sorted({f.output_version for f in stamped if f.output_version is not None}) or [None]
    table = sb.table(FINDINGS_TABLE)
    stored: list[GateFinding] = []
    for version in versions:
        batch = [f for f in stamped if f.output_version == version]
        query = table.select("*").eq("run_id", str(run_id)).eq("stage", stage)
        if version is not None:
            query = query.eq("output_version", version)
        existing = {r["fingerprint"]: r for r in (query.execute().data or []) if r.get("fingerprint")}
        new_fps = {f.fingerprint for f in batch}
        for f in batch:
            prior = existing.get(f.fingerprint)
            if prior is None:
                res = table.insert(_row_for_insert(f)).execute()
                row = (res.data or [None])[0] if getattr(res, "data", None) else None
                stored.append(GateFinding.from_row(row) if isinstance(row, Mapping) else f)
                continue
            patch = {
                "severity": f.severity, "target": dict(f.target), "message": f.message,
                "evidence": dict(f.evidence), "updated_at": now_pacific_iso(),
            }
            if prior.get("status") in ACTIVE_FINDING_STATUSES:
                patch["route"] = f.route
            if prior.get("status") == "superseded":
                patch["status"] = "open"
                patch["route"] = f.route
            table.update(patch).eq("id", prior["id"]).execute()
            merged = dict(prior)
            merged.update(patch)
            stored.append(GateFinding.from_row(merged))
        for fp, prior in existing.items():
            if fp in new_fps or prior.get("status") not in ACTIVE_FINDING_STATUSES:
                continue
            table.update({"status": "superseded", "updated_at": now_pacific_iso()}).eq("id", prior["id"]).execute()
    return stored


def findings_for_run(sb: Any, run_id: str, *, stage: str | None = "final", output_version: int | None = None) -> list[GateFinding]:
    query = sb.table(FINDINGS_TABLE).select("*").eq("run_id", str(run_id))
    if stage is not None:
        query = query.eq("stage", stage)
    if output_version is not None:
        query = query.eq("output_version", output_version)
    rows = query.execute().data or []
    return [GateFinding.from_row(r) for r in rows if isinstance(r, Mapping)]


def gate_summary_from_findings(findings: Iterable[GateFinding], *, enforced: bool, output_version: int | None = None, mode: str | None = None, not_applicable: Iterable[str] = (), manual_lines_unapproved: int = 0) -> dict[str, Any]:
    report = GateReport(
        findings=list(findings), evaluated=[], not_applicable=list(not_applicable), mode=mode or "legacy",
        enforced=enforced, manual_lines_unapproved=manual_lines_unapproved, output_version=output_version or 1,
    )
    return report.summary()


def pricing_allowed(run: Any, *, sb: Any = None, findings: Iterable[GateFinding] | None = None) -> tuple[bool, list[str]]:
    """(allowed, reasons) from the stage `final` findings: the run's
    `gate_summary` (written by run_final), the table when a client is given,
    or `findings`. Never allowed by absence: with no source to read (no
    summary, no client, no findings) it refuses with
    `pricing_gates_unavailable`; with a source that shows no final gate pass
    ever ran (no gate_summary and no stage `final` finding of any status) it
    refuses with `gates_not_run`. The gate_summary is the marker that the
    gates ran. Unapproved manual lines refuse. When the gates are not
    enforced (plans_reader_gates_blocking off) pricing proceeds and the
    reasons name the codes as `not_enforced:<code>`."""
    summary: Mapping[str, Any] | None = None
    if isinstance(run, Mapping) and isinstance(run.get("gate_summary"), Mapping):
        summary = run["gate_summary"]
    run_id = run if isinstance(run, str) else (str(run.get("id")) if isinstance(run, Mapping) and run.get("id") else getattr(run, "id", None))
    final: list[GateFinding] | None = None
    if findings is not None:
        final = [f for f in findings if f.stage == "final"]
    elif sb is not None and run_id:
        final = list(findings_for_run(sb, run_id, stage="final"))
    if final is None and summary is None:
        return False, [PRICING_GATES_UNAVAILABLE]
    if summary is None and not final:
        return False, [GATES_NOT_RUN]
    # An empty table read never overrides a gate_summary: the summary's codes decide then.
    active = [f for f in final if f.status in ACTIVE_FINDING_STATUSES] if final else None
    enforced = bool(summary.get("enforced", True)) if summary is not None else True
    manual = int(summary.get("manual_lines_unapproved") or 0) if summary is not None else 0
    if active is not None:
        codes = sorted({f.gate_id for f in active if f.severity == "block"})
    else:
        codes = [str(c) for c in summary.get("codes") or []] if summary is not None else []
    reasons: list[str] = []
    if manual > 0:
        reasons.append("manual_lines_unapproved")
    if codes:
        if enforced:
            reasons.extend(codes)
        else:
            reasons.extend(f"not_enforced:{c}" for c in codes)
    allowed = manual == 0 and (not codes or not enforced)
    return allowed, reasons


# Replay stage -----------------------------------------------------------------------------------


def replay_gate(ctx: Any, art: dict[str, Any]) -> dict[str, Any]:
    """The `gate` stage of plans_reader_replay: the final gates over the
    stored main output (legacy mode) or the assembled main (tools mode),
    the v2 category outputs of the merge stage and the facts the derive
    stage stored. Writes gate_report (bdr.gate_report/1), gate_findings and
    gate_summary."""
    main = art.get("main")
    boq = art.get("boq_spec") or (main or {}).get("boq")
    if not boq:
        ctx.note("gate: no BOQ to gate")
        return {}
    cats = [v for k, v in art.items() if k.startswith("category:") and isinstance(v, Mapping)]
    facts_raw = art.get("facts") or (art.get("scope_context") or {}).get("facts") or []
    facts: list[Any] = []
    for f in facts_raw:
        try:
            facts.append(Fact.from_dict(f) if isinstance(f, Mapping) else f)
        except Exception:  # noqa: BLE001 - a malformed stored fact is skipped, not fabricated
            continue
    overrides = dict(getattr(ctx, "settings_overrides", {}) or {})
    report = run_final(
        boq=boq, run=ctx.bundle.run if getattr(ctx, "bundle", None) else None, main=main, category_outputs=cats,
        facts=facts, measure=art.get("measure"), excluded_lines=art.get("excluded_lines") or (),
        linear_evidence=art.get("linear_evidence") or (), settings=overrides or None,
        blocking=overrides.get("plans_reader_gates_blocking"),
        output_version=int((art.get("deliverable_versions") or {}).get("boq") or 1),
    )
    ctx.note(f"gate: mode {report.mode}, {len(report.findings)} findings, blocking {report.blocking}, not applicable {', '.join(report.not_applicable) or 'none'}")
    return {
        "gate_report": report.report(),
        "gate_findings": [f.to_row() for f in report.findings],
        "gate_summary": report.summary(),
        "excluded_lines": [e.to_dict() for e in report.excluded],
        "moved_allowances": list(report.moved_allowances),
    }


def register_replay_stage() -> None:
    from app.services.plans_reader_replay import register_stage

    register_stage(
        "gate", replay_gate, version=GATE_VERSION, reads=("main", "boq_spec", "category:*", "facts", "measure"),
        writes=("gate_report", "gate_findings", "gate_summary", "excluded_lines"), owner="WS4",
    )


try:  # the replay registry is optional at import (scripts without WS0)
    register_replay_stage()
except Exception:  # noqa: BLE001 - never block the product import on the eval registry
    pass


__all__ = [
    "GATE_VERSION", "GATES_NOT_RUN", "RACEWAY_CLASSES", "FINDINGS_TABLE", "MODES", "ROUTES_FINAL", "PROVENANCE_GATES", "FINAL_GATES",
    "CAPABILITY_DENIAL_PATTERNS", "BRANCH_CONDUCTOR_CLASSES", "BRANCH_RACEWAY_CLASSES", "DEVICE_CLASSES", "is_power_cable",
    "DEVICE_GROUPS", "COUNT_CLASSES", "LINEAR_BLOCK_SHARE", "LINEAR_MIN_LINES", "LINEAR_LUMINAIRE_FT_COUNTS",
    "LINEAR_LUMINAIRE_FT_SHARE", "BLOCKING_FAMILIES", "WARN_FAMILIES", "RECOUNT_FAMILIES",
    "PARTIAL_NOTE_RE", "GateSettings", "GateContext", "GateReport", "systems_of", "fact_systems", "fact_excludes",
    "is_raceway_text", "fact_excludes_raceway",
    "fact_is_existing_circuit", "boq_lines_of", "detect_mode", "new_device_count", "branch_feet",
    "capability_denial_hits", "brief_number_claims", "tag_totals", "receptacle_total", "census_checks",
    "run_final", "pre_consolidation", "record_findings", "findings_for_run", "gate_summary_from_findings", "unconsumed_measure_rows",
    "pricing_allowed", "replay_gate", "register_replay_stage", "now_pacific_iso",
]
