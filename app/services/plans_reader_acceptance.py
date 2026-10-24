"""The acceptance suite E1 to E25 and GC1 to GC4
(docs/AI_ESTIMATING_BUILD_SPEC.md 3.5 is the ONLY definition of each pass
criterion; 3.6 holds the metrics; WS0 step 9).

Every check is a detector over the stage artifacts a replay (or a live run's
bundle) produced, named by the "Reads" column of 3.5, plus the case reference
(`eval_reference`), the scorer v2 result (`plans_reader_score`) and the
7154 `expected.json` for the thresholds that carry real dollars. A detector
returns `pass | fail | skip | safe_refusal | error` with the measured values:

- `fail` when a measurable part misses its threshold;
- `skip` only when nothing could be measured because an artifact the row
  reads is absent (the reason is recorded), except where 3.5 says the absent
  artifact IS the failure (no priced estimate on E1, E4, E9, E13, GC1, GC4);
- `safe_refusal` when the run refused with a blocking gate that maps to a
  real defect (E15 on cases other than 7154, GC3);
- `error` when the detector itself crashed (`Check.run` catches it).

The checks register through `estimating_contracts.register_check` with the
`gates_7154_smoke` and `replayable` flags of the 3.5 table; the thresholds are
the table's, as data, resolved against expected.json at run time (never a
7154 dollar literal here). `run_checks` runs them over an `AcceptanceInputs`;
`self_test` runs every check on the weekend bundle and compares with the
Baseline column (`BASELINE_7154`), which the golden test asserts.

On a live run's bundle the priced estimate (E1, E4, E9, E13, GC1, GC4), the
gate report, the review and the run's bid conditions come from the bundle's
pricing, findings and run members through `plans_reader_replay.stored_artifacts`;
E4 falls back to the bid conditions (run column, then the stored policy output)
for the wage type when no priced estimate carries one.

Pure over its inputs: no database, no network, no model.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services import plans_reader_score as score_mod
from app.services.electrical_items import accessory_role
from app.services.estimating_contracts import (
    CHECK_SPECS,
    CHECKS,
    FIXED_CHECK_IDS,
    CheckResult,
    bid_total_matches_verify,
    excluded_project_ids_sha256,
    register_check,
    resolve_reference_thresholds,
)

logger = logging.getLogger(__name__)

OWNER = "WS0"
ACCEPTANCE_SCHEMA = "bdr.eval.acceptance/1"
STATUS_ORDER: tuple[str, ...] = ("fail", "safe_refusal", "pass", "skip", "error")

# Metric constants of 3.6 (the thresholds themselves live in CHECK_SPECS).
GOOD_BAND = score_mod.GOOD_BAND
ACCEPTABLE_BAND = score_mod.ACCEPTABLE_BAND
GC2_TOPICS: dict[str, str] = {
    "temporary_power": r"\btemp(?:orary)?\s+power\b",
    "temporary_lighting": r"\btemp(?:orary)?\s+lighting\b",
    "hepa": r"\bhepa\b",
    "coordination_study": r"\bcoordination\s+stud(?:y|ies)\b",
    "commissioning": r"\bcommissioning\b",
    "cutting_and_patching": r"\b(?:cutting\s+and\s+patching|patch(?:ing)?)\b",
    "painting": r"\bpaint(?:ing)?\b",
    "firestopping": r"\bfire\s*stop(?:ping)?\b",
    "bim": r"\bbim\b",
    "training": r"\btraining\b",
    "phasing": r"\bphasing\b",
    "bond": r"\bbond\b",
    "fire_alarm_by_others": r"\bfire\s+alarm\b",
    "low_voltage_by_others": r"\blow\s+voltage\b",
    "permits_and_fees": r"\bpermits?\b|\bfees\b",
    "after_hours_work": r"\bafter[\s-]hours\b|\bovertime\b|\bnight\s+work\b",
}
GC3_GATE_MAP: dict[str, str] = {
    "census_floor": "truth_count_above_ai",
    "partial_basis": "truth_count_above_ai",
    "new_devices_no_branch": "truth_branch_ft_positive_ai_zero",
    "linear_in_ea": "truth_line_is_ft",
    "excluded_system": "truth_omits_system",
    "fact_conflict": "truth_omits_system",
    "uom_conflict_quote": "truth_unit_equals_quote_unit",
    "wage_type_unknown": "project_wage_type_null",
}
GC3_GENERIC_GATES: frozenset[str] = frozenset(
    {"unsourced_line", "orphan_rows", "arithmetic", "brief_numbers", "capability_denial", "lost_sheets",
     "double_consumption", "unverifiable_citation", "unit_class"}
)

_CITE_RE = re.compile(
    r"exhibit\s+[a-z]\b|\bp(?:age|g)?\.?\s*\d+\b|\b[A-Z]{1,2}D?\d\.\d{2}\b|\bsheet\s+[A-Z]*\d|\bsection\s+\d{2}\s?\d{2}|\bdiv(?:ision)?\s+\d{2}",
    re.I,
)
_NEED_CONFIRM_RE = re.compile(r"NEED TO CONFIRM", re.I)
_FIRE_ALARM_RE = re.compile(r"\bFIRE\s*ALARM\b|\bFA\b|\bFIRE DETECTION\b|\bSMOKE DETECT", re.I)
_ACCESS_RE = re.compile(r"\bACCESS\s+(?:CONTROL|INTERFACE)\b|\bCARD\s+READER\b|\bDOOR\s+ACCESS\b", re.I)
_HONEYWELL_RE = re.compile(r"honeywell", re.I)
_L4_CONTROL_RE = re.compile(r"\bL4\b.{0,60}\bcontrol|\bcontrol.{0,60}\bL4\b", re.I | re.S)
_UNREADABLE_RE = re.compile(r"unreadable", re.I)
_E20_RE = re.compile(
    r"maintain(?:ed)?\s+and\s+cap|\bcapp?(?:ed)?\b.{0,80}\bremov|\bremov\w*\b.{0,80}\bto\s+(?:the\s+)?source\b|retained/capped",
    re.I | re.S,
)
_RACEWAY_RFI_RE = re.compile(r"\braceway\b|\bEMT\b|\brigid\b|\bconduit\s+(?:type|method)\b|\bMC\s+cable\b|\bMC\b", re.I)
_EMT_RE = re.compile(r"\bEMT\b", re.I)
_MAKE_SAFE_DAMPER_RE = re.compile(r"MAKE\s*SAFE.*DAMPER|DAMPER.*MAKE\s*SAFE", re.I)
_TROFFER_RE = re.compile(r"TROFFER|RECTANGULAR|2\s?X\s?4|2X4", re.I)
_SPARE_EXISTING_RE = re.compile(r"\bSPARE\b|\bEXISTING\b|PROVISIONAL|NEED TO CONFIRM", re.I)
_COLOR_RE = re.compile(r"\b(RED|BLUE|GRAY|GREY|IVORY|WHITE|BLACK|BROWN|ORANGE|GREEN)\b", re.I)
_M_PREFIX_RE = re.compile(r"^M[A-Z]?\d", re.I)
_SHEET_NUMBER_RE = re.compile(r"^[A-Z]{1,3}\d")
_NUMBER_RE = re.compile(r"(?<![\w.])(\d{2,5}(?:,\d{3})?(?:\.\d+)?)\s*(?:h\b|hrs?\b|hours?\b|labor[\s-]hours?\b|man[\s-]hours?\b)", re.I)
_DRAWING_CATEGORIES: frozenset[str] = frozenset({"electrical_drawings", "drawings", "addenda"})
_DEVICE_KINDS: frozenset[str] = frozenset(
    {"receptacle", "fixture", "switch", "dimmer", "occ_sensor", "lighting_control_other", "equipment_connection",
     "thermostat", "box", "low_voltage", "fire_alarm"}
)


# Inputs ----------------------------------------------------------------------------------


@dataclass
class Part:
    name: str
    status: str
    measured: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


@dataclass
class AcceptanceInputs:
    """Everything a detector may read. `artifacts` are the replay artifacts
    (see plans_reader_replay); `docs` gives source PDF bytes (`get(doc_id)`)
    for the text-layer checks and may be None; `options` carries
    `demo_two_line_style` (the 5.7 setting) and `plan_scale_ft_per_in` (a
    fallback scale when the text layer has none)."""

    artifacts: dict[str, Any]
    case_id: str | None = None
    reference: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    score: Any = None  # plans_reader_score.CaseScore (adjudicated)
    score_raw: Any = None
    docs: Any = None
    options: dict[str, Any] = field(default_factory=dict)
    sent_proposal_text: str | None = None
    _rows: list[Any] | None = field(default=None, repr=False)
    _items: list[dict[str, Any]] | None = field(default=None, repr=False)

    # artifacts

    def art(self, name: str) -> Any:
        return self.artifacts.get(name)

    @property
    def priced(self) -> dict[str, Any] | None:
        p = self.art("priced_estimate")
        return p if isinstance(p, Mapping) else None

    @property
    def gate_report(self) -> dict[str, Any] | None:
        g = self.art("gate_report")
        return g if isinstance(g, Mapping) else None

    @property
    def main(self) -> dict[str, Any] | None:
        m = self.art("main")
        return m if isinstance(m, Mapping) else None

    @property
    def brief(self) -> dict[str, Any]:
        b = self.art("brief_spec")
        if isinstance(b, Mapping):
            return dict(b)
        m = self.main or {}
        return dict(m.get("brief") or {}) if isinstance(m.get("brief"), Mapping) else {}

    @property
    def estimate_spec(self) -> dict[str, Any]:
        e = self.art("estimate_spec")
        if isinstance(e, Mapping):
            inner = e.get("estimate")
            return dict(inner) if isinstance(inner, Mapping) else dict(e)
        m = self.main or {}
        return dict(m.get("estimate") or {}) if isinstance(m.get("estimate"), Mapping) else {}

    @property
    def boq_spec(self) -> dict[str, Any] | None:
        b = self.art("boq_spec")
        if isinstance(b, Mapping):
            return dict(b)
        m = self.main or {}
        return dict(m["boq"]) if isinstance(m.get("boq"), Mapping) else None

    @property
    def scope_report(self) -> dict[str, Any] | None:
        s = self.art("scope_report")
        return s if isinstance(s, Mapping) else None

    @property
    def bid_conditions(self) -> tuple[dict[str, Any] | None, str | None]:
        """(bid conditions, source): the run's `bid_conditions` column when
        the bundle carries it, else the stored policy output's (WS5 scope
        context), else the replay's scope context; (None, None) otherwise."""
        for name in ("bid_conditions",):
            value = self.art(name)
            if isinstance(value, Mapping) and value:
                return dict(value), "run.bid_conditions"
        for name in ("policy", "scope_context"):
            ctx = self.art(name)
            value = ctx.get("bid_conditions") if isinstance(ctx, Mapping) else None
            if isinstance(value, Mapping) and value:
                return dict(value), f"{name}.bid_conditions"
        return None, None

    @property
    def latest_boq_version(self) -> int | None:
        versions = self.art("deliverable_versions")
        value = versions.get("boq") if isinstance(versions, Mapping) else None
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def documents(self) -> list[dict[str, Any]]:
        return [d for d in (self.art("documents") or []) if isinstance(d, Mapping)]

    @property
    def groups(self) -> list[dict[str, Any]]:
        return [g for g in (self.art("groups") or []) if isinstance(g, Mapping)]

    @property
    def ledger(self) -> dict[str, Any]:
        led = self.art("ledger")
        return dict(led) if isinstance(led, Mapping) else {}

    @property
    def category_outputs(self) -> dict[str, dict[str, Any]]:
        return {k.split(":", 1)[1]: v for k, v in self.artifacts.items() if k.startswith("category:") and isinstance(v, Mapping)}

    @property
    def rendered_rows(self) -> list[dict[str, Any]] | None:
        r = self.art("rendered")
        if isinstance(r, Mapping) and isinstance(r.get("boq_rows"), list):
            return list(r["boq_rows"])
        return None

    @property
    def chat_probe(self) -> dict[str, Any] | None:
        p = self.art("chat_probe")
        return dict(p) if isinstance(p, Mapping) else None

    # derived

    def boq_rows(self) -> list[Any]:
        if self._rows is None:
            spec = self.boq_spec
            self._rows = score_mod.rows_from_spec(spec) if spec else []
        return self._rows

    def boq_items(self) -> list[dict[str, Any]]:
        """Flat BOQ items with their section, group and subgroup and every
        raw field (basis, caveat, sheet_refs, coverage ...)."""
        if self._items is None:
            items: list[dict[str, Any]] = []
            spec = self.boq_spec or {}
            for area in spec.get("areas") or []:
                for sec in area.get("sections") or []:
                    section = str(sec.get("heading") or "NEW WORK").upper()
                    for grp in sec.get("groups") or []:
                        gname = str(grp.get("name") or "")
                        for sub in grp.get("subgroups") or []:
                            for it in sub.get("items") or []:
                                if not isinstance(it, Mapping):
                                    continue
                                items.append({
                                    **it,
                                    "_area": area.get("area"),
                                    "_section": "DEMOLITION" if section.startswith("DEMO") else "NEW WORK",
                                    "_group": gname,
                                    "_subgroup": str(sub.get("name") or ""),
                                })
            self._items = items
        return self._items

    def classified_items(self) -> list[tuple[dict[str, Any], Any]]:
        aliases = score_mod.merge_aliases((self.reference or {}).get("aliases") or {})
        out = []
        for it in self.boq_items():
            if it.get("is_accessory"):
                continue
            desc = str(it.get("description") or "")
            caveat = str(it.get("caveat") or "")
            text = f"{desc} {caveat}" if caveat and caveat.upper() not in desc.upper() else desc
            key = score_mod.classify_line(text, it["_section"], it["_group"], str(it.get("unit") or ""),
                                          subgroup=it["_subgroup"], aliases=aliases)
            out.append((it, key))
        return out

    def score_line(self, kind: str, *, attrs: Iterable[str] = (), desc_re: str | None = None,
                   section: str | None = None) -> Any:
        """The scorer LineResult of the truth line of `kind` whose attrs
        contain `attrs` (and whose description matches `desc_re`)."""
        if self.score is None:
            return None
        want = set(attrs)
        for ln in self.score.lines:
            if ln.kind != kind or not want.issubset(set(ln.attrs)):
                continue
            if section is not None and ln.section != section:
                continue
            if desc_re and not re.search(desc_re, ln.description, re.I):
                continue
            return ln
        return None

    def questions(self) -> list[str]:
        """Every RFI and question text of the run (brief RFIs, category
        questions, scope report questions, priced estimate RFIs)."""
        out: list[str] = []
        for r in self.brief.get("rfis") or []:
            out.append(str(r.get("question") if isinstance(r, Mapping) else r))
        report = self.scope_report or {}
        rows = [r for r in report.get("questions_resolved") or [] if isinstance(r, Mapping)]
        resolved = {(str(r.get("category")), str(r.get("row_id"))) for r in rows}
        resolved_text = {re.sub(r"\s+", " ", str(r.get("question") or "")).strip().upper() for r in rows if r.get("question")}
        # A question the engine rewrote (a decided clause removed, e.g. the L4
        # control clause after the luminaire schedule) counts as its rewrite:
        # category rows are immutable, so the report carries the new text.
        rewritten_rows = [r for r in report.get("questions_rewritten") or [] if isinstance(r, Mapping) and r.get("after")]
        rewritten = {(str(r.get("category")), str(r.get("row_id"))): str(r["after"]) for r in rewritten_rows}
        rewritten_text = {re.sub(r"\s+", " ", str(r.get("question") or "")).strip().upper(): str(r["after"])
                          for r in rewritten_rows if r.get("question")}
        for cat_name, cat in self.category_outputs.items():
            for q in cat.get("questions") or []:
                text = str(q.get("question") if isinstance(q, Mapping) else q)
                if isinstance(q, Mapping) and (cat_name, str(q.get("row_id"))) in resolved:
                    continue
                norm = re.sub(r"\s+", " ", text).strip().upper()
                if norm in resolved_text:
                    continue  # a rebuilt category output (legacy merge replay) carries no row ids
                if isinstance(q, Mapping) and (cat_name, str(q.get("row_id"))) in rewritten:
                    out.append(rewritten[(cat_name, str(q.get("row_id")))])
                    continue
                if norm in rewritten_text:
                    out.append(rewritten_text[norm])
                    continue
                out.append(text)
        for src in (self.scope_report or {}, self.priced or {}):
            for q in src.get("rfis") or src.get("questions") or []:
                out.append(str(q.get("question") if isinstance(q, Mapping) else q))
        return [q for q in out if q]

    def exclusions_and_clarifications(self) -> list[str]:
        out: list[str] = []
        for src in (self.priced or {}, self.scope_report or {}):
            for key in ("exclusions", "clarifications", "proposal_lines"):
                for e in src.get(key) or []:
                    if isinstance(e, Mapping):
                        text = str(e.get("text") or e.get("clarification") or e.get("description") or "")
                        cite = e.get("cite") or e.get("citation") or e.get("source")
                        out.append(f"{text} [{cite}]" if cite else text)
                    else:
                        out.append(str(e))
        for e in self.brief.get("exclusions") or []:
            out.append(str(e))
        return [t for t in out if t]

    def prose(self) -> list[str]:
        """Every string of the main output and the brief (E3, E19)."""
        texts: list[str] = []
        _walk_strings(self.main or {}, texts)
        if not self.main:
            _walk_strings(self.brief, texts)
            _walk_strings(self.estimate_spec, texts)
            _walk_strings(self.boq_spec or {}, texts)
        return texts

    def threshold(self, cid: str) -> dict[str, Any]:
        spec = CHECK_SPECS[cid]
        if self.expected is not None:
            try:
                return resolve_reference_thresholds(spec, self.expected)
            except Exception:  # noqa: BLE001 - a missing key leaves the refs unresolved
                pass
        return json.loads(json.dumps(spec.threshold))

    def truth_qty(self, kind: str, *, attrs: Iterable[str] = (), desc_re: str | None = None) -> float | None:
        ln = self.score_line(kind, attrs=attrs, desc_re=desc_re)
        return None if ln is None else float(ln.truth_qty)


def _walk_strings(obj: Any, out: list[str]) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, Mapping):
        for v in obj.values():
            _walk_strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_strings(v, out)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _band(ai: float | None, truth: float | None) -> tuple[str, float | None]:
    if truth is None or truth <= 0 or ai is None or ai <= 0:
        return "none", None
    rel = abs(ai - truth) / truth
    if rel <= GOOD_BAND:
        return "good", rel
    if rel <= ACCEPTABLE_BAND:
        return "acceptable", rel
    return "miss", rel


def _combine(cid: str, parts: Sequence[Part], threshold: Mapping[str, Any]) -> CheckResult:
    statuses = [p.status for p in parts]
    if "fail" in statuses:
        status = "fail"
    elif "safe_refusal" in statuses:
        status = "safe_refusal"
    elif "pass" in statuses:
        status = "pass"
    else:
        status = "skip"
    measured: dict[str, Any] = {}
    evidence: list[str] = []
    for p in parts:
        measured[p.name] = {"status": p.status, **p.measured}
        if p.note:
            evidence.append(f"{p.name}: {p.note}")
    measured["parts"] = {p.name: p.status for p in parts}
    return CheckResult(id=cid, status=status, measured=measured, threshold=dict(threshold), evidence=evidence, owner=OWNER)


def _skip(name: str, reason: str) -> Part:
    return Part(name, "skip", {}, reason)


def _verdict(name: str, ok: bool, measured: Mapping[str, Any], note: str | None = None) -> Part:
    return Part(name, "pass" if ok else "fail", dict(measured), note)


# Priced estimate helpers --------------------------------------------------------------------


def _priced_lines(priced: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [ln for ln in priced.get("lines") or [] if isinstance(ln, Mapping)]


def _line_amount(ln: Mapping[str, Any]) -> float:
    qty = _num(ln.get("qty")) or 0.0
    mat = _num(ln.get("unit_material_usd")) or 0.0
    hrs = _num(ln.get("unit_hours")) or 0.0
    return qty * (mat + hrs)


def _allowances(priced: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [a for a in priced.get("allowances") or [] if isinstance(a, Mapping)]


def _allowance_amount(a: Mapping[str, Any]) -> float:
    return _num(a.get("amount_usd")) or 0.0


def _priced_hits(priced: Mapping[str, Any], pattern: re.Pattern[str]) -> list[str]:
    hits = []
    for ln in _priced_lines(priced):
        if ln.get("excluded") or not ln.get("priced", True):
            continue
        desc = str(ln.get("description") or "")
        if pattern.search(desc) and _line_amount(ln) > 0:
            hits.append(desc)
    for a in _allowances(priced):
        name = str(a.get("name") or "")
        if pattern.search(name) and _allowance_amount(a) > 0:
            hits.append(name)
    return hits


def _item_cited(texts: Sequence[str], pattern: re.Pattern[str]) -> tuple[bool, bool]:
    """(mentioned, mentioned with a citation) in exclusions or clarifications."""
    mentioned = cited = False
    for t in texts:
        if pattern.search(t):
            mentioned = True
            if _CITE_RE.search(t):
                cited = True
    return mentioned, cited


# The checks ---------------------------------------------------------------------------------


def check_e1(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E1")
    parts: list[Part] = []
    priced = inp.priced
    if priced is None:
        parts.append(Part("priced_estimate", "fail", {"present": False}, "no priced estimate artifact"))
    else:
        lines = [ln for ln in _priced_lines(priced) if not ln.get("excluded")]
        priced_n = sum(1 for ln in lines if ln.get("priced") and ln.get("unit_hours") is not None and ln.get("unit_material_usd") is not None)
        share = priced_n / len(lines) if lines else 0.0
        totals = priced.get("totals") if isinstance(priced.get("totals"), Mapping) else {}
        verify = totals.get("verify") if isinstance(totals.get("verify"), Mapping) else {}
        to_cent = bid_total_matches_verify(totals.get("bid_total_usd"), verify) if verify else False
        # 3.5: the artifact must price the run's latest BOQ version (checked
        # when both versions are known: a live bundle records them).
        meta = priced.get("metadata") if isinstance(priced.get("metadata"), Mapping) else {}
        priced_boq = _num(meta.get("boq_version"))
        latest_boq = inp.latest_boq_version
        boq_ok = priced_boq is None or latest_boq is None or int(priced_boq) == latest_boq
        ok = (priced.get("schema") == th["artifact"] and priced.get("refused") is th["refused"]
              and share >= th["priced_share_min"] and to_cent and boq_ok)
        parts.append(_verdict("priced_estimate", ok, {
            "present": True, "schema": priced.get("schema"), "refused": priced.get("refused"),
            "pricing_status": priced.get("status"), "pricing_version": meta.get("pricing_version"),
            "priced_share": round(share, 4), "lines": len(lines), "bid_total_equals_verify_sum": to_cent,
            "boq_version": None if priced_boq is None else int(priced_boq), "latest_boq_version": latest_boq,
        }, None if boq_ok else "the priced version is not the latest BOQ version"))
    probe = inp.chat_probe
    if probe is None:
        parts.append(_skip("chat_probe", "no chat probe ran"))
    else:
        answer = str(probe.get("answer") or "")
        m = _NUMBER_RE.search(answer)
        hours = float(m.group(1).replace(",", "")) if m else None
        total_hours = _num((priced or {}).get("totals", {}).get("hours")) if priced else None
        if hours is None:
            parts.append(Part("chat_probe", "fail", {"hours_answer": None, "declined": True}, "chat declined to give hours"))
        elif total_hours is None:
            parts.append(Part("chat_probe", "fail", {"hours_answer": hours, "totals_hours": None}, "no priced hours to compare"))
        else:
            rel = abs(hours - total_hours) / total_hours if total_hours else None
            parts.append(_verdict("chat_probe", rel is not None and rel <= th["chat_hours_rel_tol"],
                                  {"hours_answer": hours, "totals_hours": total_hours, "rel": rel}))
    return _combine("E1", parts, th)


def _ft_total(inp: AcceptanceInputs, kind: str, *, attrs: Iterable[str] = (), section: str = "NEW WORK") -> float:
    want = set(attrs)
    total = 0.0
    aliases = score_mod.merge_aliases((inp.reference or {}).get("aliases") or {})
    for r in inp.boq_rows():
        if r.is_accessory or r.unit != "FT" or r.section != section:
            continue
        key = score_mod.classify_line(r.description, r.section, r.group, r.unit, subgroup=r.subgroup, aliases=aliases)
        if key.kind == kind and want.issubset(set(key.attrs)):
            total += float(r.quantity)
    return total


def _new_devices(inp: AcceptanceInputs) -> float:
    total = 0.0
    for _it, key in inp.classified_items():
        if _it["_section"] == "NEW WORK" and key.kind in _DEVICE_KINDS and str(_it.get("unit") or "").upper() == "EA":
            total += _num(_it.get("quantity")) or 0.0
    return total


def check_e2(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E2")
    parts: list[Part] = []
    if inp.boq_spec is None:
        return _combine("E2", [_skip("boq", "no BOQ spec")], th)
    devices = _new_devices(inp)
    conductor_ft = _ft_total(inp, "conductor")
    conduit_ft = _ft_total(inp, "conduit")
    generic_ok = devices <= 0 or (conductor_ft > 0 and conduit_ft > 0)
    parts.append(_verdict("new_devices_need_branch", generic_ok,
                          {"new_devices_ea": devices, "branch_conductor_ft": round(conductor_ft, 2), "branch_conduit_ft": round(conduit_ft, 2)}))
    lines = {
        "#12": inp.score_line("conductor", attrs=("#12",)),
        "#10": inp.score_line("conductor", attrs=("#10",)),
        '3/4" EMT': inp.score_line("conduit", attrs=("EMT",), desc_re=r"3/4"),
    }
    if all(v is None for v in lines.values()):
        parts.append(_skip("truth_bands", "the reference has no #12, #10 or 3/4 EMT truth line"))
    else:
        measured: dict[str, Any] = {}
        ok = True
        for name, ln in lines.items():
            if ln is None:
                measured[name] = {"status": "no_truth_line"}
                continue
            band, rel = _band(ln.ai_qty, ln.truth_qty)
            need = th["emt_band"] if "EMT" in name else th["band"]
            good = band == "good" or (need == "acceptable" and band == "acceptable")
            ok = ok and good
            measured[name] = {"ai_ft": round(ln.ai_qty, 2), "truth_ft": round(ln.truth_qty, 2), "band": band,
                              "rel": None if rel is None else round(rel, 4), "need": need}
        parts.append(_verdict("truth_bands", ok, measured))
    return _combine("E2", parts, th)


def check_e3(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E3")
    texts = inp.prose()
    if not texts:
        return _combine("E3", [_skip("prose", "no main output or brief")], th)
    hits: list[dict[str, Any]] = []
    for pat in th["denial_patterns"]:
        rx = re.compile(pat, re.I)
        for t in texts:
            if rx.search(t):
                hits.append({"pattern": pat, "text": t[:120]})
    parts = [_verdict("denial_phrases", len(hits) <= th["max_hits"], {"hits": len(hits), "examples": hits[:6]})]
    ft_items = [it for it in inp.boq_items() if str(it.get("unit") or "").upper() == "FT" and not it.get("is_accessory")]
    if not ft_items:
        parts.append(Part("ft_lines_basis", "skip" if hits else "pass", {"ft_lines": 0}, "no FT lines"))
    else:
        missing = [str(it.get("description")) for it in ft_items if not it.get("basis")]
        parts.append(_verdict("ft_lines_basis", not missing, {"ft_lines": len(ft_items), "without_basis": missing[:10]}))
    return _combine("E3", parts, th)


def check_e4(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E4")
    parts: list[Part] = []
    priced = inp.priced
    wage, source = (priced.get("wage_type"), "priced_estimate") if priced is not None and priced.get("wage_type") else (None, None)
    if wage is None:
        bid, bid_source = inp.bid_conditions
        if bid is not None and bid.get("wage_type"):
            wage, source = bid.get("wage_type"), bid_source
    if priced is None and wage is None:
        parts.append(Part("wage_decision", "fail", {"present": False}, "no priced estimate, so no wage decision"))
    else:
        rate = _num(priced.get("labor_rate_usd")) if priced is not None else None
        ok = wage == th["wage_type"] and rate is not None and abs(rate - float(th["labor_rate_usd"])) < 0.005
        parts.append(_verdict("wage_decision", ok, {
            "present": True, "wage_type": wage, "wage_source": source, "labor_rate_usd": rate,
            "priced": priced is not None,
        }, None if priced is not None else "wage decided by the bid conditions but no priced estimate carries the rate"))
    rx = re.compile(th["forbidden_rfi_pattern"], re.I)
    rfis = [q for q in inp.questions() if rx.search(q)]
    parts.append(_verdict("no_wage_rfi", not rfis, {"wage_rfis": len(rfis), "examples": [q[:120] for q in rfis[:3]]}))
    return _combine("E4", parts, th)


def check_e5(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E5")
    rx = re.compile(th["house_exclusion_pattern"], re.I)
    parts: list[Part] = []
    priced = inp.priced
    extra = [str(a) for a in (inp.estimate_spec.get("extra_allowances") or [])]
    house = [a for a in extra if rx.search(a)]
    if priced is not None:
        hits = _priced_hits(priced, rx)
        parts.append(_verdict("priced_house_exclusions", len(hits) <= th["max_priced_hits"], {"hits": hits}))
        texts = inp.exclusions_and_clarifications()
        uncited = [h for h in set(hits) | set(house) if not any(re.search(re.escape(h[:24]), t, re.I) for t in texts)]
        parts.append(_verdict("detected_in_exclusions_or_clarifications", not uncited, {"missing": uncited}))
        priced_allow = [a for a in _allowances(priced) if _allowance_amount(a) > 0 and str(a.get("name") or "").upper()
                        not in {s.upper() for s in _standard_allowances()}]
        parts.append(_verdict("extra_allowances", len(priced_allow) <= th["max_extra_allowances"],
                              {"extra_allowances": [str(a.get("name")) for a in priced_allow]}))
    elif inp.estimate_spec:
        parts.append(Part("extra_allowances", "fail" if (house or len(extra) > th["max_extra_allowances"]) else "pass",
                          {"extra_allowances": len(extra), "house_excluded": len(house), "examples": house},
                          "estimate spec extra allowances (no priced estimate yet)"))
    else:
        parts.append(_skip("estimate_spec", "no priced estimate and no estimate spec"))
    return _combine("E5", parts, th)


def _standard_allowances() -> tuple[str, ...]:
    from app.services.plans_reader_schemas import ESTIMATE_ALLOWANCES

    return ESTIMATE_ALLOWANCES


def check_e6(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E6")
    parts: list[Part] = []
    if inp.boq_spec is None:
        return _combine("E6", [_skip("boq", "no BOQ spec")], th)
    emt_ft = _ft_total(inp, "conduit", attrs=("EMT",))
    forbidden = {t: _ft_total(inp, "conduit", attrs=(t,)) for t in th["forbidden_branch_types"]}
    truth = inp.truth_qty("conduit", attrs=("EMT",))
    measured = {"emt_ft": round(emt_ft, 2), "forbidden_ft": forbidden, "truth_emt_ft": truth}
    ok = emt_ft > 0 and all(v == 0 for v in forbidden.values())
    if truth:
        ok = ok and emt_ft >= th["emt_ft_min_ratio"] * truth
        measured["ratio"] = round(emt_ft / truth, 4) if truth else None
    parts.append(_verdict("branch_conduit", ok, measured, None if emt_ft > 0 else "no conduit line"))
    texts = inp.exclusions_and_clarifications()
    names = any(_EMT_RE.search(t) for t in texts)
    if inp.priced is None and inp.scope_report is None:
        parts.append(_skip("clarification_names_emt", "no priced estimate or scope report"))
    else:
        parts.append(_verdict("clarification_names_emt", names, {"clarifications": len(texts)}))
    rfis = [q for q in inp.questions() if _RACEWAY_RFI_RE.search(q)]
    parts.append(_verdict("no_raceway_rfi", len(rfis) <= th["max_raceway_rfis"], {"raceway_rfis": len(rfis), "examples": [q[:120] for q in rfis[:3]]}))
    return _combine("E6", parts, th)


def check_e7(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E7")
    parts: list[Part] = []
    priced = inp.priced
    rx = re.compile(f"{_FIRE_ALARM_RE.pattern}|{_ACCESS_RE.pattern}", re.I)
    if priced is not None:
        hits = _priced_hits(priced, rx)
        parts.append(_verdict("no_priced_fa_or_access", not hits, {"hits": hits}))
        texts = inp.exclusions_and_clarifications()
        fa_m, fa_c = _item_cited(texts, _FIRE_ALARM_RE)
        ac_m, ac_c = _item_cited(texts, _ACCESS_RE)
        party = any(_HONEYWELL_RE.search(t) for t in texts)
        exhibit = any(re.search(th["exclusion_cite"], t, re.I) for t in texts if _FIRE_ALARM_RE.search(t) or _ACCESS_RE.search(t))
        parts.append(_verdict("exclusion_names_party_with_cite", fa_m and ac_m and party and exhibit and (fa_c or ac_c),
                              {"fire_alarm": fa_m, "access_control": ac_m, "party_named": party, "exhibit_cited": exhibit}))
    else:
        extra = [str(a) for a in (inp.estimate_spec.get("extra_allowances") or [])]
        carried = [a for a in extra if rx.search(a)]
        if not inp.estimate_spec:
            parts.append(_skip("estimate_spec", "no priced estimate and no estimate spec"))
        else:
            parts.append(_verdict("no_fa_or_access_allowance", not carried, {"carried": carried},
                                  "estimate spec extra allowances (no priced estimate yet)"))
    return _combine("E7", parts, th)


def check_e8(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E8")
    rx = re.compile(th["forbidden_pattern"], re.I)
    parts: list[Part] = []
    priced = inp.priced
    if priced is not None:
        hits = _priced_hits(priced, rx)
        parts.append(_verdict("no_priced_rows", not hits, {"hits": hits}))
        texts = inp.exclusions_and_clarifications()
        missing = []
        for topic in th["must_be_excluded_or_clarified"]:
            m, c = _item_cited(texts, re.compile(re.escape(topic), re.I))
            if not m or (th["citation_required"] and not c):
                missing.append(topic)
        parts.append(_verdict("topics_excluded_with_citation", not missing, {"missing": missing}))
    else:
        extra = [str(a) for a in (inp.estimate_spec.get("extra_allowances") or [])]
        carried = [a for a in extra if rx.search(a)]
        rows = [str(it.get("description")) for it in inp.boq_items() if rx.search(str(it.get("description") or ""))]
        if not inp.estimate_spec and inp.boq_spec is None:
            parts.append(_skip("estimate_spec", "no priced estimate, estimate spec or BOQ"))
        else:
            parts.append(_verdict("no_rows_or_allowances", not carried and not rows, {"allowances": carried, "rows": rows},
                                  "estimate spec and BOQ (no priced estimate yet)"))
    return _combine("E8", parts, th)


def check_e9(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E9")
    parts: list[Part] = []
    priced = inp.priced
    if priced is None:
        return _combine("E9", [Part("vendor_quote", "fail", {"present": False}, "no priced estimate, so no quotes")], th)
    sections = priced.get("sections") if isinstance(priced.get("sections"), Mapping) else {}
    sec = sections.get(th["section"]) if isinstance(sections.get(th["section"]), Mapping) else {}
    quotes = [q for q in priced.get("quotes") or [] if isinstance(q, Mapping)]
    lighting = [q for q in quotes if th["section"] in (q.get("replaces_sections") or []) or str(q.get("category") or "").lower() == "lighting"]
    if not lighting:
        parts.append(Part("vendor_quote", "fail", {"quotes": len(quotes)}, "no lighting quote reconciled"))
        return _combine("E9", parts, th)
    source_ok = sec.get("material_source") == th["material_source"]
    amount = _num(lighting[0].get("amount_usd"))
    targets = th["pretax_targets_usd"] if isinstance(th.get("pretax_targets_usd"), Mapping) else {}
    lowest = _num(targets.get("lowest_eligible"))
    alternate = _num(targets.get("alternate_selection"))
    reason = str(lighting[0].get("reason") or lighting[0].get("selection_reason") or "")
    within = None
    if amount is not None and lowest:
        if abs(amount - lowest) / lowest <= th["rel_tol"]:
            within = "lowest_eligible"
        elif alternate and abs(amount - alternate) / alternate <= th["rel_tol"] and (reason or not th["reason_required"]):
            within = "alternate_selection"
    elif lowest is None:
        parts.append(_skip("pretax_within_tolerance", "expected.json quote targets are unavailable"))
    if lowest is not None:
        parts.append(_verdict("pretax_within_tolerance", within is not None and source_ok,
                              {"material_source": sec.get("material_source"), "matched": within, "reason_recorded": bool(reason)}))
    rec = lighting[0].get("reconciled") if isinstance(lighting[0].get("reconciled"), Mapping) else None
    if rec is None:
        parts.append(_skip("quote_reconciles", "quote carries no reconciled line counts"))
    else:
        want = th["reconcile"]
        got = {"L3_once": _num(rec.get("L3")), "L5_ft": _num(rec.get("L5_ft")), "L1": _num(rec.get("L1")), "L3": _num(rec.get("L3"))}
        ok = all(got.get(k) is not None and abs(got[k] - float(v)) < 0.5 for k, v in want.items())
        parts.append(_verdict("quote_reconciles", ok, {"got": got, "collapse_warning": bool(rec.get("collapse_warning"))}))
    return _combine("E9", parts, th)


def check_e10(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E10")
    ln = inp.score_line("fixture", attrs=(th["item"],))
    parts: list[Part] = []
    if ln is None:
        if inp.boq_spec is None:
            return _combine("E10", [_skip("l5", "no BOQ spec")], th)
        parts.append(_skip("l5", f"the reference has no {th['item']} line"))
    else:
        rows = [it for it, key in inp.classified_items() if key.kind == "fixture" and th["item"] in key.attrs]
        units = sorted({str(it.get("unit") or "").upper() for it in rows})
        band, rel = _band(ln.ai_qty, ln.truth_qty)
        ok = units == [th["unit"]] and band in ("good", "acceptable")
        parts.append(_verdict("l5_unit_and_band", ok, {"units": units, "ai_qty": ln.ai_qty, "truth_qty": ln.truth_qty,
                                                        "band": band, "rel": None if rel is None else round(rel, 4),
                                                        "unit_mismatch_rows": ln.unit_mismatch_rows}))
        if units and units != [th["unit"]]:
            gate = inp.gate_report
            if gate is None:
                parts.append(_skip("uom_conflict_quote_raised", "no gate report (pricing findings) to read"))
            else:
                codes = [str(c.get("code")) for c in gate.get("checks") or [] if isinstance(c, Mapping)]
                parts.append(_verdict("uom_conflict_quote_raised", th["stored_v1_gate"] in codes, {"codes": codes}))
    return _combine("E10", parts, th)


def check_e11(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E11")
    parts: list[Part] = []
    if inp.boq_spec is None:
        return _combine("E11", [_skip("boq", "no BOQ spec")], th)
    ln = inp.score_line("demo_lighting")
    if ln is None:
        parts.append(_skip("demo_lighting", "the reference has no demolition lighting line"))
    else:
        rel = abs(ln.ai_qty - ln.truth_qty) / ln.truth_qty if ln.truth_qty else None
        parts.append(_verdict("demo_lighting", rel is not None and rel <= th["demo_lighting_rel_tol"],
                              {"ai": ln.ai_qty, "truth": ln.truth_qty, "rel": None if rel is None else round(rel, 4)}))
    troffers = 0.0
    seen = False
    for it, key in inp.classified_items():
        if key.kind == "demo_lighting" and _TROFFER_RE.search(str(it.get("description") or "")):
            refs = " ".join(str(s) for s in (it.get("sheet_refs") or []))
            if "ED3.01" in refs or not refs:
                seen = True
                troffers += _num(it.get("quantity")) or 0.0
    if inp.case_id == "7154" or seen:
        parts.append(_verdict("ed3_01_troffers", abs(troffers - th["ed3_01_troffers"]) < 0.5, {"troffers": troffers}))
    tile_rows = [str(it.get("description")) for it in inp.boq_items()
                 if str(it.get("coverage") or it.get("basis") or "").lower() in ("tile_subtotal", "region_subtotal")]
    parts.append(_verdict("no_tile_subtotal_rows", len(tile_rows) <= th["tile_subtotal_rows_max"], {"rows": tile_rows}))
    gate = inp.gate_report
    census = inp.art("census") if isinstance(inp.art("census"), Mapping) else None
    if gate is None and census is None:
        parts.append(_skip("census_vs_boq", "no gate report or census artifact"))
    else:
        codes = [str(c.get("code")) for c in (gate or {}).get("checks") or [] if isinstance(c, Mapping)]
        blocking = bool((gate or {}).get("blocking"))
        mismatch = _num((census or {}).get("max_mismatch"))
        # Met when a census artifact is inside the tolerance, when the final
        # gates blocked on a census shortfall (a safe refusal), or when the
        # final gates evaluated the census floors and raised no census_floor
        # finding at all (every printed floor met by the BOQ).
        evaluated = [str(c) for c in (gate or {}).get("evaluated") or []]
        census_met = gate is not None and "census_floor" not in codes and ("census_floor" in evaluated or not evaluated)
        ok = (mismatch is not None and mismatch <= th["census_mismatch_max"]) or (blocking and "census_floor" in codes) or census_met
        parts.append(_verdict("census_vs_boq", ok, {"max_mismatch": mismatch, "blocking": blocking, "codes": codes, "census_met": census_met}))
    return _combine("E11", parts, th)


def check_e12(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E12")
    if inp.boq_spec is None:
        return _combine("E12", [_skip("boq", "no BOQ spec")], th)
    breakers = [(it, key) for it, key in inp.classified_items() if key.kind == "breaker" and it["_section"] == "NEW WORK"]
    gfci = [it for it, key in breakers if "GFCI" in key.attrs]
    others = [it for it, key in breakers if "GFCI" not in key.attrs]
    total = sum(_num(it.get("quantity")) or 0.0 for it, _k in breakers)
    provisional = sum(_num(it.get("quantity")) or 0.0 for it in others
                      if _SPARE_EXISTING_RE.search(f"{it.get('description')} {it.get('caveat') or ''}"))
    gfci_qty = sum(_num(it.get("quantity")) or 0.0 for it in gfci)
    ok = (len(breakers) == th["breaker_lines"] and len(gfci) == 1 and abs(gfci_qty - th["breaker_qty"]) < 0.5
          and provisional <= th["provisional_on_spare_or_existing"])
    return _combine("E12", [_verdict("breaker_lines", ok, {
        "breaker_rows": len(breakers), "breaker_total_ea": total, "gfci_rows": len(gfci), "gfci_ea": gfci_qty,
        "provisional_ea": provisional, "descriptions": [str(it.get("description")) for it, _k in breakers][:8],
    })], th)


def check_e13(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E13")
    priced = inp.priced
    if priced is None:
        return _combine("E13", [Part("device_pricing", "fail", {"present": False}, "no pricing")], th)
    duplex_min = _num(th.get("duplex_unit_material_min_usd"))
    gfci_min = _num(th.get("gfci_unit_material_min_usd"))
    if duplex_min is None or gfci_min is None:
        return _combine("E13", [_skip("device_pricing", "expected.json thresholds are unavailable")], th)
    aliases = score_mod.merge_aliases((inp.reference or {}).get("aliases") or {})
    duplex: list[Mapping[str, Any]] = []
    gfci: list[Mapping[str, Any]] = []
    for ln in _priced_lines(priced):
        key = score_mod.classify_line(str(ln.get("description") or ""), str(ln.get("section") or ""), str(ln.get("group") or ""),
                                      str(ln.get("unit") or ""), aliases=aliases)
        if key.kind != "receptacle" or "DUPLEX" not in key.attrs or str(ln.get("section") or "").upper().startswith("DEMO"):
            continue
        (gfci if "GFCI" in key.attrs else duplex).append(ln)

    def ok_lines(lines: list[Mapping[str, Any]], floor: float) -> tuple[bool, list[dict[str, Any]]]:
        rows = []
        good = bool(lines)
        for ln in lines:
            mat = _num(ln.get("unit_material_usd"))
            fact = bool(ln.get("spec_fact_ref") or ln.get("spec_facts") or ln.get("fact_refs") or ln.get("price_basis_cite"))
            rows.append({"description": str(ln.get("description"))[:60], "unit_material_usd": mat, "spec_fact": fact})
            good = good and mat is not None and mat >= floor and (fact or not th["spec_fact_required"])
        return good, rows

    d_ok, d_rows = ok_lines(duplex, duplex_min)
    g_ok, g_rows = ok_lines(gfci, gfci_min)
    return _combine("E13", [
        _verdict("duplex", d_ok, {"lines": d_rows}),
        _verdict("gfci", g_ok, {"lines": g_rows}),
    ], th)


def check_e14(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E14")
    if inp.boq_spec is None:
        return _combine("E14", [_skip("boq", "no BOQ spec")], th)
    parts: list[Part] = []
    t = inp.score_line("thermostat")
    if t is None:
        parts.append(_skip("thermostat", "the reference has no thermostat line"))
    else:
        parts.append(_verdict("thermostat", t.status == "exact", {"ai": t.ai_qty, "truth": t.truth_qty, "status": t.status}))
    c = inp.score_line("control_cable")
    if c is None:
        parts.append(_skip("control_cable", "the reference has no control cable line"))
    else:
        band, rel = _band(c.ai_qty, c.truth_qty)
        parts.append(_verdict("control_cable", band in ("good", "acceptable"),
                              {"ai_ft": c.ai_qty, "truth_ft": c.truth_qty, "band": band, "rel": None if rel is None else round(rel, 4)}))
    return _combine("E14", parts, th)


def check_e15(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E15")
    if inp.boq_spec is None:
        return _combine("E15", [_skip("boq", "no BOQ spec")], th)
    ln = inp.score_line("demo_track")
    rows = [it for it, key in inp.classified_items() if key.kind == "demo_track"]
    units = sorted({str(it.get("unit") or "").upper() for it in rows})
    bases = sorted({str(it.get("basis") or "") for it in rows})
    gate = inp.gate_report or {}
    human_block = any(
        isinstance(c, Mapping) and c.get("code") == "linear_in_ea" and c.get("severity") == "block"
        for c in gate.get("checks") or []
    )
    if ln is None:
        if not rows:
            return _combine("E15", [_skip("demo_track", "the reference has no demolition track line")], th)
        measured = {"units": units, "bases": bases}
        if human_block and inp.case_id != "7154":
            return _combine("E15", [Part("demo_track", "safe_refusal", measured, "linear_in_ea routed to a human")], th)
        return _combine("E15", [_verdict("demo_track", units == [th["unit"]] and th["basis"] in bases, measured)], th)
    band, rel = _band(ln.ai_qty, ln.truth_qty)
    measured = {"units": units, "bases": bases, "ai": ln.ai_qty, "truth_ft": ln.truth_qty, "band": band,
                "rel": None if rel is None else round(rel, 4), "unit_mismatch_rows": ln.unit_mismatch_rows,
                "linear_in_ea_block": human_block}
    ok = units == [th["unit"]] and th["basis"] in bases and band in ("good", "acceptable")
    if not ok and human_block and inp.case_id != "7154":
        return _combine("E15", [Part("demo_track", "safe_refusal", measured, "linear_in_ea routed to a human")], th)
    return _combine("E15", [_verdict("demo_track", ok, measured)], th)


def _legend_pages(ledger: Mapping[str, Any]) -> list[str]:
    out = []
    for p in ledger.get("legend_pages") or []:
        if isinstance(p, Mapping):
            out.append(str(p.get("sheet_number") or p.get("title") or f"{p.get('document_id', '')[:8]}:{p.get('page')}"))
        else:
            out.append(str(p))
    return out


def check_e16(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E16")
    parts: list[Part] = []
    ledger = inp.ledger
    if not ledger:
        parts.append(_skip("ledger", "no ledger"))
    else:
        pages = _legend_pages(ledger)
        legend = ledger.get("legend")
        entries = len(legend) if isinstance(legend, (list, Mapping)) else 0
        roles = [str(s.get("page_role")) for s in ledger.get("sheet_index") or [] if isinstance(s, Mapping)]
        ok = any(th["legend_page"] in p for p in pages) and entries >= th["legend_entries_min"]
        parts.append(_verdict("ledger_legend", ok, {"legend_pages": pages, "legend_entries": entries,
                                                    "sheet_roles": sorted(set(roles))}))
    planned = inp.art("planned_requests")
    if not isinstance(planned, list):
        parts.append(_skip("legend_in_drawing_requests", "no planned requests artifact (plan stage not rerun)"))
    else:
        drawing = [r for r in planned if isinstance(r, Mapping) and r.get("kind") == "sub"
                   and str(r.get("category")) in _DRAWING_CATEGORIES]
        missing = [str(r.get("group_id") or r.get("group_index")) for r in drawing if not r.get("has_legend_text")]
        parts.append(_verdict("legend_in_drawing_requests", not missing and bool(drawing), {"drawing_requests": len(drawing), "missing": missing}))
    if inp.boq_spec is None:
        parts.append(_skip("fixture_caveats", "no BOQ spec"))
    else:
        caveats = [str(it.get("description")) for it, key in inp.classified_items()
                   if key.kind == "fixture" and _NEED_CONFIRM_RE.search(f"{it.get('caveat') or ''} {it.get('description') or ''}")]
        parts.append(_verdict("fixture_caveats", len(caveats) <= th["need_to_confirm_caveat_rows"], {"rows": len(caveats), "examples": caveats[:6]}))
    return _combine("E16", parts, th)


def check_e17(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E17")
    spec = inp.art("marked_plans_spec")
    sheets = [s for s in ((spec or {}).get("sheets") or []) if isinstance(s, Mapping)] if isinstance(spec, Mapping) else []
    evidence = inp.art("evidence")
    if isinstance(evidence, Mapping) and isinstance(evidence.get("sheets"), list):
        sheets = [s for s in evidence["sheets"] if isinstance(s, Mapping)] or sheets
    if not sheets:
        return _combine("E17", [_skip("marks", "no marked plans spec or evidence artifact")], th)
    from app.services import eval_text_anchors as anchors

    if inp.docs is None or not anchors.available():
        return _combine("E17", [_skip("marks", "no source PDFs or pagefacts available for text-layer anchors")], th)
    extractor = anchors.AnchorExtractor()
    fallback = _num(inp.options.get("plan_scale_ft_per_in"))
    distances: list[float] = []
    distances_any: list[float] = []
    per_sheet: list[dict[str, Any]] = []
    stacked = 0
    polylines = 0
    skipped: list[str] = []
    for s in sheets:
        doc_id = str(s.get("document_id") or "")
        page = int(_num(s.get("page")) or 0)
        marks = [m for m in s.get("marks") or [] if isinstance(m, Mapping)]
        polylines += sum(1 for m in marks if str(m.get("kind") or "") in ("polyline", "line", "run"))
        if not doc_id or page < 1 or not marks:
            continue
        try:
            pdf = inp.docs.get(doc_id)
        except Exception as exc:  # noqa: BLE001 - a missing PDF skips the sheet
            skipped.append(f"{s.get('sheet_number') or page}: {exc}")
            continue
        sa = extractor.sheet(doc_id, pdf, page, ft_per_in_fallback=fallback)
        if not sa.available or sa.ft_per_in is None:
            skipped.append(f"{s.get('sheet_number') or page}: {sa.note or 'no anchors'}")
            continue
        number = str(sa.sheet_number or s.get("sheet_number") or "")
        if _M_PREFIX_RE.match(number):
            skipped.append(f"{number}: mechanical sheet (E24), marks are no electrical evidence")
            continue
        legend = [e for e in s.get("legend") or [] if isinstance(e, Mapping)]
        rows = anchors.mark_distances(sa, marks, legend)
        if not rows:
            skipped.append(f"{number or page}: only reference-only marks")
            continue
        ds = [r["distance_ft"] for r in rows]
        ds_any = [r["distance_any_ft"] for r in rows]
        counted = [m for m in marks if str(m.get("kind") or "dot") not in anchors.POLYLINE_KINDS and not anchors.is_reference_legend(
            legend[int(m["legend_index"])] if isinstance(m.get("legend_index"), int) and 0 <= m["legend_index"] < len(legend) else None)]
        pts = [p for m in counted for p in (m.get("points") or [])]
        st = anchors.stacked_duplicates(pts, sa.w_in, sa.h_in, sa.ft_per_in, within_ft=th["stacked_within_ft"])
        stacked += st
        distances.extend(ds)
        distances_any.extend(ds_any)
        per_sheet.append({
            "sheet": number or page, "page": page, "marks": len(ds), "anchors": len(sa.anchors),
            "median_ft": round(statistics.median(ds), 2),
            "median_any_ft": round(statistics.median(ds_any), 2),
            "over_5ft": sum(1 for d in ds if d > 5), "over_5ft_any": sum(1 for d in ds_any if d > 5),
            "stacked": st, "ft_per_in": sa.ft_per_in,
            "fallback_anchors": sum(1 for r in rows if r.get("fallback")),
        })
    if not distances:
        return _combine("E17", [_skip("marks", "no sheet could be measured: " + "; ".join(skipped[:4]))], th)
    median = statistics.median(distances)
    over = sum(1 for d in distances if d > 5)
    share = over / len(distances)
    parts = [
        _verdict("median_anchor_distance", median <= th["median_anchor_ft_max"],
                 {"median_ft": round(median, 2), "median_any_ft": round(statistics.median(distances_any), 2),
                  "marks": len(distances), "sheets": per_sheet, "skipped": skipped}),
        _verdict("over_5ft_share", share <= th["over_5ft_share_max"],
                 {"over_5ft": over, "over_5ft_any": sum(1 for d in distances_any if d > 5), "marks": len(distances),
                  "share": round(share, 4)}),
        _verdict("stacked_duplicates", stacked <= th["stacked_max"], {"stacked": stacked}),
    ]
    ft_with_geometry = [it for it in inp.boq_items() if str(it.get("unit") or "").upper() == "FT"
                        and not it.get("is_accessory") and str(it.get("basis") or "") == "measured"]
    if not ft_with_geometry:
        parts.append(Part("ft_lines_have_polylines", "skip", {"polylines": polylines, "measured_ft_lines": 0},
                          "no FT line with geometry (no polylines expected)"))
    else:
        parts.append(_verdict("ft_lines_have_polylines", polylines > 0, {"polylines": polylines, "measured_ft_lines": len(ft_with_geometry)}))
    return _combine("E17", parts, th)


def check_e18(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E18")
    parts: list[Part] = []
    docs = inp.documents
    if not docs:
        return _combine("E18", [_skip("documents", "no documents")], th)
    pages = 0
    labeled = 0
    for d in docs:
        if str(d.get("category")) not in _DRAWING_CATEGORIES:
            continue
        for m in d.get("page_meta") or []:
            if not isinstance(m, Mapping):
                continue
            pages += 1
            if m.get("sheet_number") and m.get("title") and str(m.get("page_role") or "other") != "other":
                labeled += 1
    share = labeled / pages if pages else 0.0
    parts.append(_verdict("drawing_pages_labeled", pages > 0 and share >= th["drawing_pages_labeled_share_min"],
                          {"drawing_pages": pages, "labeled": labeled, "share": round(share, 4)}))
    statuses = {str(d.get("id"))[:8]: d.get("facts_status") for d in docs}
    parts.append(_verdict("facts_status_done", all(v == th["facts_status"] for v in statuses.values()), {"facts_status": statuses}))
    planned = inp.art("planned_requests")
    if not isinstance(planned, list):
        parts.append(_skip("facts_block_every_page", "no planned requests artifact (plan stage not rerun)"))
    else:
        subs = [r for r in planned if isinstance(r, Mapping) and r.get("kind") == "sub"]
        missing = [str(r.get("group_id") or r.get("group_index")) for r in subs
                   if int(_num(r.get("pages_without_facts_block")) or 0) > 0]
        parts.append(_verdict("facts_block_every_page", not missing and bool(subs), {"sub_requests": len(subs), "missing": missing}))
        tokens = sum(int(_num(r.get("est_input_tokens")) or 0) for r in planned if isinstance(r, Mapping))
        if inp.case_id == "7154":
            parts.append(_verdict("planned_input_tokens", tokens <= th["planned_input_tokens_max_7154"], {"est_input_tokens": tokens}))
    return _combine("E18", parts, th)


def check_e19(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E19")
    texts: list[str] = []
    for cat in inp.category_outputs.values():
        _walk_strings(cat, texts)
    texts.extend(inp.prose())
    if not texts:
        return _combine("E19", [_skip("outputs", "no category or main outputs")], th)
    unreadable = [t[:120] for t in texts if _UNREADABLE_RE.search(t)]
    parts = [_verdict("unreadable_hits", len(unreadable) <= th["unreadable_hits_for_text_layer_values"],
                      {"hits": len(unreadable), "examples": unreadable[:4]})]
    l4_control: str | None = None
    for cat in inp.category_outputs.values():
        for sch in cat.get("schedules") or []:
            if not isinstance(sch, Mapping):
                continue
            for row in sch.get("rows") or []:
                cells = [str(c) for c in row] if isinstance(row, (list, tuple)) else [str(v) for v in (row.values() if isinstance(row, Mapping) else [])]
                if any(c.strip() == "L4" for c in cells):
                    hit = next((c for c in cells if re.search(r"\bLOCAL\b|0-10V|DIMM|SWITCH|UNREADABLE", c, re.I)), None)
                    l4_control = hit or l4_control
    if inp.case_id == "7154" or l4_control is not None:
        parts.append(_verdict("l4_control", l4_control is not None and re.search(th["l4_control"], l4_control, re.I) is not None,
                              {"l4_control": l4_control}))
    qs = [q[:120] for q in inp.questions() if _L4_CONTROL_RE.search(q)]
    if inp.case_id == "7154" or qs:
        parts.append(_verdict("l4_control_questions", len(qs) <= th["l4_control_questions"], {"questions": len(qs), "examples": qs[:3]}))
    return _combine("E19", parts, th)


def check_e20(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E20")
    qs = inp.questions()
    if not qs and inp.boq_spec is None:
        return _combine("E20", [_skip("outputs", "no questions or BOQ")], th)
    hits = [q[:120] for q in qs if _E20_RE.search(q)]
    parts = [_verdict("maintain_vs_remove_rfis", len(hits) <= th["maintain_vs_remove_rfis"], {"rfis": len(hits), "examples": hits[:3]})]
    demo = [it for it in inp.boq_items() if it["_section"] == "DEMOLITION" and not it.get("is_accessory")]
    if not demo:
        parts.append(_skip("demo_disposition", "no demolition lines"))
    else:
        decided = [it for it in demo if it.get("demo_disposition")]
        cited = [it for it in decided if isinstance(it.get("demo_disposition"), Mapping)
                 and (it["demo_disposition"].get("cite") or it["demo_disposition"].get("citation"))]
        report = inp.scope_report or {}
        report_decision = any(isinstance(d, Mapping) and str(d.get("key") or "").startswith("demo_disposition")
                              and d.get("evidence") for d in report.get("decisions") or [])
        ok = report_decision or (len(decided) == len(demo) and len(cited) == len(demo))
        parts.append(_verdict("demo_disposition", ok, {"demo_lines": len(demo), "with_decision": len(decided),
                                                        "with_citation": len(cited), "scope_report_decision": report_decision}))
    return _combine("E20", parts, th)


def check_e21(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E21")
    if inp.boq_spec is None:
        return _combine("E21", [_skip("boq", "no BOQ spec")], th)
    parts: list[Part] = []
    tr = inp.score_line("receptacle", attrs=("TR",))
    if tr is None:
        parts.append(_skip("tr_duplex", "the reference has no TR duplex line"))
    else:
        parts.append(_verdict("tr_duplex", tr.status == "exact", {"ai": tr.ai_qty, "truth": tr.truth_qty, "status": tr.status}))
    mods = [m for m in th["unpriced_modifiers"] if m != "colors"]
    rx = re.compile("|".join(rf"\b{re.escape(m)}\b" for m in mods), re.I)
    split_rows = []
    total = 0.0
    for it, key in inp.classified_items():
        if key.kind != "receptacle" or it["_section"] != "NEW WORK":
            continue
        total += _num(it.get("quantity")) or 0.0
        desc = str(it.get("description") or "")
        if rx.search(desc) or _COLOR_RE.search(desc):
            split_rows.append(desc)
    parts.append(_verdict("unpriced_modifier_rows", len(split_rows) <= th["unpriced_modifier_split_rows"],
                          {"rows": len(split_rows), "examples": split_rows[:6]}))
    truth_total = th["receptacle_total"] if inp.case_id == "7154" else None
    if truth_total is None and inp.score is not None:
        truth_total = sum(ln.truth_qty for ln in inp.score.lines if ln.kind == "receptacle" and ln.section == "NEW WORK") or None
    if truth_total:
        parts.append(_verdict("receptacle_total", abs(total - float(truth_total)) < 0.5, {"total": total, "truth": truth_total}))
    return _combine("E21", parts, th)


def check_e22(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E22")
    if inp.boq_spec is None:
        return _combine("E22", [_skip("boq", "no BOQ spec")], th)
    rows = [it for it, key in inp.classified_items() if key.kind == "demo_power"]
    total = sum(_num(it.get("quantity")) or 0.0 for it in rows)
    two_line = bool(inp.options.get("demo_two_line_style", False))
    max_rows = th["max_rows_two_line_style"] if two_line else th["max_rows_default"]
    truth = inp.truth_qty("demo_power")
    if truth is None and inp.case_id == "7154":
        truth = float(th["demo_power_truth"])
    damper = [str(it.get("description")) for it in inp.boq_items() if _MAKE_SAFE_DAMPER_RE.search(str(it.get("description") or ""))]
    parts = [
        _verdict("demo_power_rows", len(rows) <= max_rows, {"rows": len(rows), "max_rows": max_rows, "two_line_style": two_line}),
        _verdict("no_make_safe_damper_row", len(damper) <= th["make_safe_damper_rows"], {"rows": damper}),
    ]
    if truth is None:
        parts.append(_skip("sum_within_1", "the reference has no demolition power line"))
    else:
        parts.append(_verdict("sum_within_1", abs(total - truth) <= th["sum_tol"], {"sum": total, "truth": truth}))
    return _combine("E22", parts, th)


def check_e23(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E23")
    rows = inp.rendered_rows
    if rows is None:
        return _combine("E23", [_skip("rendered", "no rendered BOQ (render stage not run)")], th)
    emt = [r for r in rows if r.get("unit") == "FT" and _EMT_RE.search(str(r.get("description") or "")) and not r.get("is_accessory")]
    emt_ft = sum(_num(r.get("quantity")) or 0.0 for r in emt)
    acc = {name: sum(_num(r.get("quantity")) or 0.0 for r in rows
                     if accessory_role(r.get("description")) == name[:-1] and _EMT_RE.search(str(r.get("description") or "")))
           for name in th["emt_accessories"]}
    if emt_ft <= 0:
        return _combine("E23", [Part("emt_accessories", "fail", {"emt_ft": 0.0, "accessories": acc}, "no EMT line, so no accessories")], th)
    couplings_truth = None
    for ln in (inp.reference or {}).get("accessory_lines") or []:
        if isinstance(ln, Mapping) and re.search(r"COUPLING", str(ln.get("description") or ""), re.I):
            couplings_truth = _num(ln.get("qty"))
    if couplings_truth is None and inp.case_id == "7154":
        couplings_truth = float(th["couplings_truth"])
    ok = all(v > 0 for v in acc.values())
    measured: dict[str, Any] = {"emt_ft": round(emt_ft, 2), "accessories": acc}
    if couplings_truth:
        rel = abs(acc.get("couplings", 0.0) - couplings_truth) / couplings_truth
        measured["couplings_rel"] = round(rel, 4)
        ok = ok and rel <= th["couplings_rel_tol"]
    return _combine("E23", [_verdict("emt_accessories", ok, measured)], th)


def _m_prefix_share(inp: AcceptanceInputs, d: Mapping[str, Any]) -> tuple[float | None, int, str]:
    numbers = [str(m.get("sheet_number")) for m in d.get("page_meta") or [] if isinstance(m, Mapping) and m.get("sheet_number")]
    source = "page_meta"
    if not numbers and inp.docs is not None:
        try:
            from app.services.pagefacts import extract as pf_extract

            pdf = inp.docs.get(str(d.get("id")))
            facts = pf_extract.extract_document(pdf, label=str(d.get("category") or ""), page_budget_s=8.0, doc_budget_s=600.0)
            numbers = [str(p.sheet_number) for p in facts.pages if p.sheet_number]
            source = "text_layer"
        except Exception as exc:  # noqa: BLE001 - falls back to the name heuristic
            logger.info("acceptance E24: text-layer sheet numbers unavailable for %s: %s", d.get("name"), exc)
    if not numbers:
        name = str(d.get("name") or "")
        if re.search(r"\bMECH\b|\bMECHANICAL\b|\bM-?\d", name, re.I):
            return 1.0, 0, "name_heuristic"
        return None, 0, "none"
    m = sum(1 for n in numbers if _M_PREFIX_RE.match(n))
    return m / len(numbers), len(numbers), source


def check_e24(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E24")
    docs = inp.documents
    if not docs:
        return _combine("E24", [_skip("documents", "no documents")], th)
    parts: list[Part] = []
    offenders = []
    details = []
    for d in docs:
        if str(d.get("category")) != "electrical_drawings":
            continue
        share, n, source = _m_prefix_share(inp, d)
        details.append({"name": str(d.get("name"))[:60], "m_share": share, "numbered": n, "source": source})
        if share is not None and share >= th["m_prefix_share_min"]:
            offenders.append(str(d.get("name"))[:60])
    parts.append(_verdict("no_mechanical_set_read_as_electrical", not offenders, {"offenders": offenders, "documents": details}))
    spec_docs = [d for d in docs if str(d.get("category")) == "specifications"]
    spec_pages = sum(int(_num(d.get("page_count")) or 0) for d in spec_docs)
    if not spec_pages:
        parts.append(_skip("spec_pages_sent", "no specification documents"))
    else:
        sent = 0
        for g in inp.groups:
            if g.get("category") == "specifications" and g.get("kind") in ("sub", "escalation"):
                sent += len([p for p in g.get("pages") or [] if isinstance(p, Mapping)])
        limit = th["spec_pages_sent_max_7154"] if inp.case_id == "7154" else int(th["spec_pages_sent_share_max"] * spec_pages)
        parts.append(_verdict("spec_pages_sent", sent <= limit, {"sent": sent, "spec_pages": spec_pages, "limit": limit}))
    return _combine("E24", parts, th)


def _r4(v: float | None) -> float | None:
    return None if v is None else round(v, 4)


def check_e25(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("E25")
    if inp.score is None:
        return _combine("E25", [_skip("score", "no score (score stage not run)")], th)
    parts: list[Part] = []
    sc = inp.score
    parts.append(_verdict("matcher", sc.matcher_version == th["matcher_version"] and sc.collisions.unresolved <= th["unresolved_truth_key_collisions"],
                          {"matcher_version": sc.matcher_version, "unresolved_collisions": sc.collisions.unresolved}))
    raw = inp.score_raw if inp.score_raw is not None else sc
    is_weekend = inp.case_id == "7154" and str(inp.options.get("bundle") or "").startswith("ff8fad25")
    if is_weekend:
        l1 = next((ln for ln in raw.lines if ln.kind == "fixture" and set(ln.attrs) == {"L1"}), None)
        l1em = next((ln for ln in raw.lines if ln.kind == "fixture" and set(ln.attrs) == {"L1-EM"}), None)
        tr = next((ln for ln in raw.lines if ln.kind == "receptacle" and "TR" in ln.attrs), None)
        pairing = (l1 is not None and l1.status == "exact" and all("LIFE SAFETY" not in d for d in l1.ai_descriptions)
                   and l1em is not None and l1em.ai_qty == 5 and tr is not None and not tr.ai_refs)
        # The E25 0.319 baseline was measured with the prototype formula, so it
        # reads the partial-credit field (the threshold key keeps its 3.5 name).
        cov = raw.dollar_coverage_partial
        ok = (raw.count_lines_exact == th["count_lines_exact"] and raw.count_lines_total == th["count_lines_total"]
              and cov is not None and abs(cov - th["dollar_coverage"]) <= th["dollar_coverage_tol"] and pairing)
        parts.append(_verdict("weekend_bundle_numbers", ok, {
            "count_lines_exact": raw.count_lines_exact, "count_lines_total": raw.count_lines_total,
            "dollar_coverage_partial": _r4(cov), "dollar_coverage": _r4(raw.dollar_coverage), "l1_pairing": pairing,
        }))
    else:
        parts.append(Part("scored", "pass", {"count_lines_exact": sc.count_lines_exact, "count_lines_total": sc.count_lines_total,
                                             "dollar_coverage": _r4(sc.dollar_coverage),
                                             "dollar_coverage_partial": _r4(sc.dollar_coverage_partial)}))
    return _combine("E25", parts, th)


def check_gc1(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("GC1")
    priced = inp.priced
    if priced is None:
        return _combine("GC1", [Part("non_quoted_material", "fail", {"present": False}, "no pricing")], th)
    recap = ((inp.reference or {}).get("estimator_recap") or {}).get("sections") or {}
    sections = priced.get("sections") if isinstance(priced.get("sections"), Mapping) else {}
    if not recap:
        return _combine("GC1", [_skip("non_quoted_material", "the reference has no estimator recap")], th)
    missing = []
    checked = []
    for name, row in recap.items():
        truth_mat = _num((row or {}).get("material")) or 0.0
        if truth_mat <= 0:
            continue
        sec = sections.get(name) if isinstance(sections.get(name), Mapping) else {}
        if sec.get("material_source") == "vendor_quote":
            continue
        checked.append(name)
        if (_num(sec.get("material_usd")) or 0.0) <= 0:
            missing.append(name)
    return _combine("GC1", [_verdict("non_quoted_material", not missing and bool(checked), {"checked": checked, "missing": missing})], th)


def gc2_topics(text: str) -> set[str]:
    return {k for k, rx in GC2_TOPICS.items() if re.search(rx, text or "", re.I)}


def check_gc2(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("GC2")
    ai_texts = inp.exclusions_and_clarifications() if (inp.priced is not None or inp.scope_report is not None) else []
    if not ai_texts:
        return _combine("GC2", [_skip("scope_report", "no scope report")], th)
    if not inp.sent_proposal_text:
        return _combine("GC2", [_skip("sent_proposal", "no sent proposal text for this case")], th)
    s_topics = gc2_topics(inp.sent_proposal_text)
    if not s_topics:
        return _combine("GC2", [_skip("sent_proposal", "the sent proposal names no lexicon topic")], th)
    a_topics = set()
    for t in ai_texts:
        a_topics |= gc2_topics(t)
    recall = len(a_topics & s_topics) / len(s_topics)
    violations = []
    for ln in _priced_lines(inp.priced or {}):
        if _line_amount(ln) > 0 and gc2_topics(str(ln.get("description") or "")) & s_topics:
            violations.append(str(ln.get("description")))
    ok = recall >= th["topic_recall_min"] and len(violations) <= th["priced_lines_on_excluded_topics"]
    return _combine("GC2", [_verdict("generic_scope", ok, {"recall": round(recall, 4), "sent_topics": sorted(s_topics),
                                                            "ai_topics": sorted(a_topics), "violations": violations})], th)


def _gc3_confirms(code: str, check: Mapping[str, Any], inp: AcceptanceInputs) -> bool | None:
    """Whether the truth confirms the refusal (3.6 mapping); None when the
    reference cannot answer."""
    sc = inp.score
    ref = inp.reference or {}
    rows = [str(r) for r in check.get("rows") or []]
    if code in ("census_floor", "partial_basis"):
        if sc is None:
            return None
        return any(ln.truth_qty > ln.ai_qty for ln in sc.lines if not ln.is_length)
    if code == "new_devices_no_branch":
        if sc is None:
            return None
        truth_ft = sum(ln.truth_qty for ln in sc.lines if ln.kind == "conductor")
        ai_ft = sum(ln.ai_qty for ln in sc.lines if ln.kind == "conductor")
        return truth_ft > 0 and ai_ft == 0
    if code == "linear_in_ea":
        if sc is None:
            return None
        return any(ln.is_length and ln.unit_mismatch_rows > 0 for ln in sc.lines)
    if code in ("excluded_system", "fact_conflict"):
        detail = str(check.get("detail") or "")
        if sc is None:
            return None
        return not any(re.search(re.escape(detail[:20]), ln.description, re.I) for ln in sc.lines if detail)
    if code == "uom_conflict_quote":
        if sc is None:
            return None
        return any(ln.is_length and ln.unit_mismatch_rows > 0 for ln in sc.lines)
    if code == "wage_type_unknown":
        return ((ref.get("conditions") or {}).get("wage_type")) is None
    if sc is None:
        return None
    cited = [ln for ln in sc.lines if any(r in (ln.ai_refs or []) for r in rows)] if rows else []
    if cited:
        return any(ln.rel_error is None or ln.rel_error > 0.10 for ln in cited)
    return any((ln.rel_error is not None and ln.rel_error > 0.10) or ln.status in ("miss", "none") for ln in sc.lines)


def check_gc3(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("GC3")
    priced = inp.priced
    gate = inp.gate_report
    refused = bool(priced and priced.get("refused")) or bool(gate and gate.get("blocking"))
    if priced is None and gate is None:
        return _combine("GC3", [_skip("refusals", "no gate report and no priced estimate (nothing refused)")], th)
    if not refused:
        return _combine("GC3", [Part("refusals", "pass", {"refused": False})], th)
    if inp.case_id == "7154":
        return _combine("GC3", [Part("refusals", "fail", {"refused": True}, "7154 must not refuse (1.3)")], th)
    false_refusals = []
    confirmed = []
    unknown = []
    checks = [c for c in (gate or {}).get("checks") or [] if isinstance(c, Mapping) and c.get("severity") == "block"]
    for c in checks:
        code = str(c.get("code") or "")
        verdict = _gc3_confirms(code, c, inp)
        if verdict is None:
            unknown.append(code)
        elif verdict:
            confirmed.append(code)
        else:
            false_refusals.append(code)
    if not checks:
        false_refusals.append("refused_without_blocking_gate")
    measured = {"confirmed": confirmed, "false_refusals": false_refusals, "unverifiable": unknown}
    if false_refusals:
        return _combine("GC3", [Part("refusals", "fail", measured, "false_refusal")], th)
    if confirmed:
        return _combine("GC3", [Part("refusals", "safe_refusal", measured)], th)
    return _combine("GC3", [_skip("refusals", "the reference cannot confirm the refusal")], th)


def check_gc4(inp: AcceptanceInputs) -> CheckResult:
    th = inp.threshold("GC4")
    priced = inp.priced
    if priced is None:
        return _combine("GC4", [Part("excluded_projects", "fail", {"present": False}, "no priced estimate (field absent)")], th)
    meta = priced.get("metadata") if isinstance(priced.get("metadata"), Mapping) else {}
    digest = meta.get("excluded_project_ids_sha256")
    if not digest:
        return _combine("GC4", [Part("excluded_projects", "fail", {"present": False}, "field absent")], th)
    ids = meta.get("excluded_project_ids")
    own = str((inp.reference or {}).get("project_id") or "")
    if isinstance(ids, list):
        expected_digest = excluded_project_ids_sha256(str(i) for i in ids)
        includes = bool(own) and own in {str(i) for i in ids}
    else:
        try:
            from app.services.eval_manifest import excluded_projects

            computed = excluded_projects("pricing_library", inp.case_id)
        except Exception:  # noqa: BLE001 - manifest unavailable: compare only the digest shape
            computed = frozenset()
        expected_digest = excluded_project_ids_sha256(sorted(computed)) if computed else None
        includes = bool(own) and own in computed
    ok = (expected_digest is None or digest == expected_digest) and includes
    return _combine("GC4", [_verdict("excluded_projects", ok, {"sha256_matches": expected_digest is None or digest == expected_digest,
                                                               "includes_own_project": includes})], th)


DETECTORS: dict[str, Callable[[AcceptanceInputs], CheckResult]] = {
    "E1": check_e1, "E2": check_e2, "E3": check_e3, "E4": check_e4, "E5": check_e5, "E6": check_e6, "E7": check_e7,
    "E8": check_e8, "E9": check_e9, "E10": check_e10, "E11": check_e11, "E12": check_e12, "E13": check_e13,
    "E14": check_e14, "E15": check_e15, "E16": check_e16, "E17": check_e17, "E18": check_e18, "E19": check_e19,
    "E20": check_e20, "E21": check_e21, "E22": check_e22, "E23": check_e23, "E24": check_e24, "E25": check_e25,
    "GC1": check_gc1, "GC2": check_gc2, "GC3": check_gc3, "GC4": check_gc4,
}
TITLES: dict[str, str] = {
    "E1": "Priced estimate exists and reconciles", "E2": "Branch wire and conduit produced", "E3": "No capability denial prose",
    "E4": "Prevailing wage decided", "E5": "House exclusions not priced", "E6": "Branch raceway EMT", "E7": "Fire alarm and access by others",
    "E8": "Owner obligations not priced", "E9": "Lighting quote reconciled", "E10": "L5 in feet", "E11": "Demolition lighting complete",
    "E12": "Breakers from keynotes only", "E13": "Spec-grade device pricing", "E14": "Thermostats and control cable carried",
    "E15": "Demolition track measured", "E16": "Legend reaches the readers", "E17": "Marks at text anchors",
    "E18": "Page facts from the text layer", "E19": "No unreadable text-layer values", "E20": "Demolition disposition decided",
    "E21": "TR receptacles, no style splits", "E22": "Demolition power rows", "E23": "EMT accessories rendered",
    "E24": "Mechanical set relabeled, specs selected", "E25": "Scorer v2", "GC1": "Non-quoted material carried",
    "GC2": "Generic scope score", "GC3": "Refusals map to defects", "GC4": "Leave-one-project-out recorded",
}


def register_all() -> None:
    for cid, fn in DETECTORS.items():
        spec = CHECK_SPECS[cid]
        register_check(cid, fn, gates_7154_smoke=spec.gates_7154_smoke, replayable=spec.replayable,
                       title=TITLES.get(cid, cid), owner=OWNER)


register_all()


def run_checks(inp: AcceptanceInputs, ids: Iterable[str] | None = None) -> list[CheckResult]:
    """Run the registered checks (all of 3.5 by default) over the inputs, in
    table order. A crashing detector yields `error`, never a pass."""
    wanted = list(ids) if ids is not None else list(FIXED_CHECK_IDS)
    out = []
    for cid in wanted:
        check = CHECKS.get(cid)
        if check is None:
            out.append(CheckResult(id=cid, status="error", measured={"exception": "no detector registered"}, owner=OWNER))
            continue
        out.append(check.run(inp))
    return out


def results_table(results: Sequence[CheckResult]) -> list[dict[str, Any]]:
    rows = []
    for r in results:
        spec = CHECK_SPECS.get(r.id)
        rows.append({
            "id": r.id,
            "title": TITLES.get(r.id, ""),
            "status": r.status,
            "gates_7154_smoke": spec.gates_7154_smoke if spec else None,
            "replay": spec.replay if spec else None,
            "parts": r.measured.get("parts", {}),
            "evidence": list(r.evidence),
            "measured": {k: v for k, v in r.measured.items() if k != "parts"},
        })
    return rows


# The skip allowances of the 3.5 rows marked "gates 7154 smoke" (1.3 item 1:
# rows marked `skip allowed` may skip only for the reason stated there). The
# value is the set of parts that may skip ("*" = the whole row) and whether
# the allowance holds only on a replay. A row not listed may not skip at all
# (E15 says "no skip" explicitly).
SMOKE_SKIP_ALLOWANCES: dict[str, tuple[frozenset[str], bool]] = {
    "E1": (frozenset({"chat_probe"}), False),  # chat part only, when no chat probe ran
    "E11": (frozenset({"demo_lighting", "ed3_01_troffers"}), True),  # the count needs the live recount
    "E16": (frozenset({"fixture_caveats"}), True),  # caveat part may skip on replay
    "E19": (frozenset({"*"}), True),  # sub reading is live
}
SMOKE_DOLLAR_COVERAGE_MIN = 0.85  # 1.3 item 1, reads CaseScore.dollar_coverage (the 3.6 in-band definition)


def _skip_reason(r: CheckResult, live: bool) -> str | None:
    """None when every skip on this gated row is allowed by its 3.5 skip
    rule, else the reason the skip fails the smoke gate."""
    parts = r.measured.get("parts") if isinstance(r.measured.get("parts"), Mapping) else {}
    skipped = sorted(name for name, st in parts.items() if st == "skip")
    if r.status != "skip" and not skipped:
        return None
    allowed, replay_only = SMOKE_SKIP_ALLOWANCES.get(r.id, (frozenset(), False))
    if not allowed:
        return f"skip not allowed on a gated row ({', '.join(skipped) or 'whole row'})"
    if replay_only and live:
        return f"skip allowed only on a replay ({', '.join(skipped) or 'whole row'})"
    if "*" in allowed:
        return None
    if r.status == "skip" and not skipped:
        return "whole row skipped; only the parts " + ", ".join(sorted(allowed)) + " may skip"
    bad = [name for name in skipped if name not in allowed]
    if bad:
        return "skipped parts not allowed: " + ", ".join(bad)
    if r.status == "skip":
        return "every part skipped; only the parts " + ", ".join(sorted(allowed)) + " may skip"
    return None


def smoke_gate(results: Sequence[CheckResult], *, score: Any = None, live: bool = False) -> dict[str, Any]:
    """The 7154 smoke verdict of 1.3 item 1 over the rows marked gates: a
    safe_refusal counts as a fail, and a skip (of the row or of one of its
    parts) fails unless that row's 3.5 skip rule allows it
    (SMOKE_SKIP_ALLOWANCES; replay-only allowances fail when `live`). With a
    scorer v2 `score` the 1.3 dollar coverage floor is added, read from
    `score.dollar_coverage` (3.6 in band), never from the partial-credit
    figure. `reasons` maps each failing id to why."""
    failing: list[str] = []
    reasons: dict[str, str] = {}
    skipped: list[str] = []
    for r in results:
        spec = CHECK_SPECS.get(r.id)
        if not spec or not spec.gates_7154_smoke:
            continue
        if r.status in ("fail", "safe_refusal", "error"):
            failing.append(r.id)
            reasons[r.id] = r.status
            continue
        parts = r.measured.get("parts") if isinstance(r.measured.get("parts"), Mapping) else {}
        if r.status == "skip" or any(st == "skip" for st in parts.values()):
            skipped.append(r.id)
        why = _skip_reason(r, live)
        if why is not None:
            failing.append(r.id)
            reasons[r.id] = why
    out: dict[str, Any] = {"failing": failing, "reasons": reasons, "skipped": skipped}
    if score is not None:
        cov = getattr(score, "dollar_coverage", None)
        ok = cov is not None and cov >= SMOKE_DOLLAR_COVERAGE_MIN
        out["dollar_coverage"] = {"value": cov, "min": SMOKE_DOLLAR_COVERAGE_MIN, "pass": ok,
                                  "partial_credit": getattr(score, "dollar_coverage_partial", None)}
        if not ok:
            failing.append("dollar_coverage")
            reasons["dollar_coverage"] = f"dollar coverage {cov} below {SMOKE_DOLLAR_COVERAGE_MIN}"
    out["pass"] = not failing
    return out


# Inputs from a replay ---------------------------------------------------------------------------


def inputs_from_replay(result: Any, *, reference: Mapping[str, Any] | None, expected: Mapping[str, Any] | None,
                       case_id: str | None = None, docs: Any = None, options: Mapping[str, Any] | None = None,
                       sent_proposal_text: str | None = None) -> AcceptanceInputs:
    """AcceptanceInputs over a plans_reader_replay.ReplayResult."""
    opts = dict(options or {})
    opts.setdefault("bundle", Path(result.bundle_root).name)
    score_raw = None
    score = result.score
    if score is not None and reference:
        try:
            spec = result.artifacts.get("boq_spec")
            if isinstance(spec, Mapping):
                rows = score_mod.rows_from_spec(dict(spec))
                priced = result.artifacts.get("priced_estimate") if isinstance(result.artifacts.get("priced_estimate"), Mapping) else None
                score_raw = score_mod.score_case(rows, dict(reference), adjudicated=False, priced=priced, case_id=case_id or result.case_id)
        except Exception as exc:  # noqa: BLE001 - the raw score is informational
            logger.info("acceptance: raw score unavailable: %s", exc)
    return AcceptanceInputs(
        artifacts=dict(result.artifacts),
        case_id=case_id or result.case_id,
        reference=dict(reference) if reference else None,
        expected=dict(expected) if expected else None,
        score=score,
        score_raw=score_raw,
        docs=docs,
        options=opts,
        sent_proposal_text=sent_proposal_text,
    )


def load_sent_proposal_text(backup_dir: Path | str, project_id: str) -> str | None:
    """Paragraph and table text of the latest sent proposal docx of a
    project in the backup (GC2's S set), or None."""
    backup_dir = Path(backup_dir)
    sends_path = backup_dir / "data" / "proposal_sends.json"
    if not sends_path.is_file():
        return None
    sends = [s for s in json.loads(sends_path.read_text()) if s.get("project_id") == project_id and s.get("status") == "sent" and s.get("file_id")]
    if not sends:
        return None
    latest = max(sends, key=lambda s: str(s.get("sent_at") or ""))
    from app.services.eval_bundle import backup_project_file

    row = backup_project_file(backup_dir, str(latest["file_id"]))
    if row is None:
        return None
    path = backup_dir / "storage" / "project-files" / str(row.get("storage_path") or "")
    if not path.is_file() or not path.suffix.lower() == ".docx":
        return None
    try:
        import docx  # python-docx
    except ImportError:
        return None
    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row_ in table.rows:
            parts.extend(cell.text for cell in row_.cells)
    return "\n".join(t for t in parts if t)


# Self test on the weekend bundle ----------------------------------------------------------------

# The Baseline column of 3.5 as predicates over the measured values of each
# detector on the stored weekend bundle (run ff8fad25). Measured values are
# counts and ratios, never dollars. E25 is the exception the table itself
# states: its baseline FAIL is under the v1 scorer, so the v2 detector passes
# on the weekend bundle (tests/test_eval_golden_7154.py pins the v1 failure).
BASELINE_7154: dict[str, dict[str, Any]] = {
    "E1": {"status": "fail", "note": "no priced estimate; chat declined hours",
           "check": lambda m: m["priced_estimate"]["present"] is False and m["chat_probe"].get("declined") is True},
    "E2": {"status": "fail", "note": "0 / 0 / 0 FT",
           "check": lambda m: all(m["truth_bands"][k]["ai_ft"] == 0 for k in ("#12", "#10", '3/4" EMT'))},
    "E3": {"status": "fail", "note": "4 hits", "check": lambda m: m["denial_phrases"]["hits"] == 4},
    "E4": {"status": "fail", "note": "no wage decision", "check": lambda m: m["wage_decision"]["present"] is False},
    "E5": {"status": "fail", "note": "8 house-excluded of 10 extra allowances",
           "check": lambda m: m["extra_allowances"]["house_excluded"] == 8 and m["extra_allowances"]["extra_allowances"] == 10},
    "E6": {"status": "fail", "note": "no conduit", "check": lambda m: m["branch_conduit"]["emt_ft"] == 0},
    "E7": {"status": "fail", "note": "Honeywell FA and access allowance carried",
           "check": lambda m: any("HONEYWELL" in c.upper() for c in m["no_fa_or_access_allowance"]["carried"])},
    "E8": {"status": "fail", "note": "TELEMETRY / CENTRAK / WAP SURVEY AND REINSTALLATION",
           "check": lambda m: any("TELEMETRY" in c.upper() for c in m["no_rows_or_allowances"]["allowances"])},
    "E9": {"status": "fail", "note": "no quotes", "check": lambda m: m["vendor_quote"]["present"] is False},
    "E10": {"status": "fail", "note": "7 EA", "check": lambda m: m["l5_unit_and_band"]["units"] == ["EA"] and m["l5_unit_and_band"]["unit_mismatch_rows"] == 1},
    "E11": {"status": "fail", "note": "34 (37 with track EA rows)", "check": lambda m: m["demo_lighting"]["ai"] == 34},
    "E12": {"status": "fail", "note": "26 EA in 5 rows, 22 provisional",
            "check": lambda m: m["breaker_lines"]["breaker_total_ea"] == 26 and m["breaker_lines"]["breaker_rows"] == 5 and m["breaker_lines"]["provisional_ea"] == 22},
    "E13": {"status": "fail", "note": "no pricing", "check": lambda m: m["device_pricing"]["present"] is False},
    "E14": {"status": "fail", "note": "both absent", "check": lambda m: m["thermostat"]["ai"] == 0 and m["control_cable"]["ai_ft"] == 0},
    "E15": {"status": "fail", "note": "3 EA", "check": lambda m: m["demo_track"]["units"] == ["EA"] and m["demo_track"]["ai"] == 0},
    "E16": {"status": "fail", "note": "legend_pages: [], page_role other everywhere",
            "check": lambda m: m["ledger_legend"]["legend_pages"] == [] and m["ledger_legend"]["sheet_roles"] == ["other"]},
    "E17": {"status": "fail", "note": "median 3.8 to 6.6 ft, 116 of 208 over 5 ft, 23 stacked, no polylines",
            "check": lambda m: (all(3.5 <= s["median_any_ft"] <= 7.0 for s in m["median_anchor_distance"]["sheets"])
                                and 205 <= m["over_5ft_share"]["marks"] <= 208 and 100 <= m["over_5ft_share"]["over_5ft_any"] <= 130
                                and m["stacked_duplicates"]["stacked"] >= 20 and m["ft_lines_have_polylines"]["polylines"] == 0),
            "golden_only": True},
    "E18": {"status": "fail", "note": "titles None, role other on all drawing pages, no facts block",
            "check": lambda m: m["drawing_pages_labeled"]["labeled"] == 0 and m["drawing_pages_labeled"]["drawing_pages"] == 18},
    "E19": {"status": "fail", "note": "L4 control remains unreadable, 1 false RFI",
            "check": lambda m: m["unreadable_hits"]["hits"] >= 1 and m["l4_control_questions"]["questions"] >= 1},
    "E20": {"status": "fail", "note": "left as RFIs", "check": lambda m: m["maintain_vs_remove_rfis"]["rfis"] >= 1},
    "E21": {"status": "fail", "note": "TR 0; 4 style-split rows",
            "check": lambda m: m["tr_duplex"]["ai"] == 0 and m["unpriced_modifier_rows"]["rows"] == 4 and m["receptacle_total"]["total"] == 74},
    "E22": {"status": "fail", "note": "4 rows summing to 34 plus the damper row",
            "check": lambda m: m["demo_power_rows"]["rows"] == 4 and m["sum_within_1"]["sum"] == 34 and len(m["no_make_safe_damper_row"]["rows"]) == 1},
    "E23": {"status": "fail", "note": "no EMT, no accessories", "check": lambda m: m["emt_accessories"]["emt_ft"] == 0},
    "E24": {"status": "fail", "note": "MECH read as electrical; 170 spec pages sent",
            "check": lambda m: len(m["no_mechanical_set_read_as_electrical"]["offenders"]) == 1 and m["spec_pages_sent"]["sent"] == 170},
    "E25": {"status": "pass", "note": "FAIL under the v1 scorer; scorer v2 (the fix) passes: 17 of 22, partial-credit coverage 0.319",
            "check": lambda m: m["weekend_bundle_numbers"]["count_lines_exact"] == 17 and m["weekend_bundle_numbers"]["count_lines_total"] == 22},
    "GC1": {"status": "fail", "note": "no pricing", "check": lambda m: m["non_quoted_material"]["present"] is False},
    "GC2": {"status": "skip", "note": "not run (no scope report)", "check": lambda m: True},
    "GC3": {"status": "skip", "note": "not applicable", "check": lambda m: True},
    "GC4": {"status": "fail", "note": "field absent", "check": lambda m: m["excluded_projects"]["present"] is False},
}
# A 7154 plan sheet is drawn at 1/4" = 1'-0" (spec 3.3 E3); the text layer
# carries that scale, this is only the fallback when it does not.
SELF_TEST_OPTIONS: dict[str, Any] = {"plan_scale_ft_per_in": 4.0, "demo_two_line_style": False}


@dataclass
class SelfTestRow:
    id: str
    expected_status: str
    status: str
    baseline_matches: bool | None
    baseline_note: str
    measured: dict[str, Any]
    evidence: list[str]

    @property
    def ok(self) -> bool:
        return self.status == self.expected_status and self.baseline_matches is not False


@dataclass
class SelfTestReport:
    bundle_root: str
    rows: list[SelfTestRow]
    replay_notes: list[str]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.rows)

    def failures(self) -> list[SelfTestRow]:
        return [r for r in self.rows if not r.ok]

    def skipped(self) -> dict[str, list[str]]:
        return {r.id: r.evidence for r in self.rows if r.status == "skip"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ACCEPTANCE_SCHEMA,
            "bundle_root": self.bundle_root,
            "ok": self.ok,
            "rows": [
                {"id": r.id, "expected_status": r.expected_status, "status": r.status, "baseline_matches": r.baseline_matches,
                 "baseline_note": r.baseline_note, "evidence": r.evidence, "measured": r.measured}
                for r in self.rows
            ],
            "replay_notes": list(self.replay_notes),
        }


def self_test(
    bundle_root: Path | str,
    *,
    eval_root: Path | str,
    backup_dir: Path | str | None = None,
    out_dir: Path | str | None = None,
    with_docs: bool = True,
) -> SelfTestReport:
    """Replay the weekend bundle through the legacy stages, run every check
    and compare each with BASELINE_7154. `with_docs` False leaves the
    text-layer checks (E17, the E24 fallback) without PDFs."""
    from app.services import eval_bundle
    from app.services import eval_reference as R
    from app.services import plans_reader_replay as replay_mod

    eval_root = Path(eval_root)
    backup = Path(backup_dir) if backup_dir else eval_bundle.DEFAULT_BACKUP_DIR
    bundle = eval_bundle.load_bundle(bundle_root)
    case_id = bundle.case_id or "7154"
    reference = R.load_reference(eval_root, case_id)
    expected_path = eval_root / "references" / case_id / "expected.json"
    expected = json.loads(expected_path.read_text()) if expected_path.is_file() else None
    result = replay_mod.replay(bundle, reference=reference, expected=expected, case_id=case_id, out_dir=out_dir, backup_dir=backup,
                               legacy_only=True)
    docs = result and replay_mod.LocalDocBytes(bundle, backup_dir=backup) if with_docs else None
    inp = inputs_from_replay(result, reference=reference, expected=expected, case_id=case_id, docs=docs, options=SELF_TEST_OPTIONS)
    results = run_checks(inp)
    rows = []
    for r in results:
        base = BASELINE_7154.get(r.id, {"status": "skip", "note": "", "check": lambda m: True})
        matches: bool | None
        try:
            matches = bool(base["check"](r.measured)) if r.status == base["status"] else False
        except (KeyError, TypeError) as exc:
            matches = False
            r = CheckResult(id=r.id, status=r.status, measured={**r.measured, "baseline_check_error": repr(exc)},
                            threshold=r.threshold, evidence=r.evidence, owner=r.owner)
        rows.append(SelfTestRow(r.id, base["status"], r.status, matches, base["note"], dict(r.measured), list(r.evidence)))
    return SelfTestReport(str(bundle.root), rows, list(result.notes))


__all__ = [
    "OWNER", "ACCEPTANCE_SCHEMA", "GC2_TOPICS", "GC3_GATE_MAP", "GC3_GENERIC_GATES", "DETECTORS", "TITLES",
    "BASELINE_7154", "SELF_TEST_OPTIONS", "Part", "AcceptanceInputs", "register_all", "run_checks", "results_table",
    "smoke_gate", "SMOKE_SKIP_ALLOWANCES", "SMOKE_DOLLAR_COVERAGE_MIN", "inputs_from_replay", "load_sent_proposal_text", "gc2_topics", "SelfTestRow", "SelfTestReport", "self_test",
]
