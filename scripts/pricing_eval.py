"""WS6 pricing evaluation over the production backup on disk ($0, no model,
no database; AI_ESTIMATING_BUILD_SPEC WS6, 1.3 item 4, 8.6, 8.7).

    uv run python scripts/pricing_eval.py --mode parse-dryrun
    uv run python scripts/pricing_eval.py --mode verify-replay
    uv run python scripts/pricing_eval.py --mode calibrate
    uv run python scripts/pricing_eval.py --mode lopo
    uv run python scripts/pricing_eval.py --mode counterfactual

Modes:

- parse-dryrun: parse every estimate workbook of the backup (the estimate
  population of 8.7) and explain each failure -> reports/estimate_parse_dryrun.md
- verify-replay: rebuild every committed Verify snapshot from the recorded
  labor figure, the selected quotes, the General Material figure and the
  markup percentages through `pricing_engine.verify_totals` (the formula the
  router runs) -> reports/pricing_verify_replay.md
- calibrate: the hour uplift (G3 reviewer hours over ENGINE basis hours on
  the estimator's quantities with the project left out of the library) on
  the training pool only, clean band 0.95 to 1.6, pooled and per coverage
  regime (`PricingPolicy.coverage_class` of the pair's own LOPO estimate)
  -> reports/pricing_calibrate.md
- lopo: leave-one-project-out backtest on the LOPO population (golden ids
  with a committed Verify and a sent proposal, minus the holdout, minus the
  reconcile filter): coverage, workbook-basis hours, false-confident rate,
  hard-reject audit, the golden Verify band and the 1.3 item 4 oracle
  backtest -> reports/pricing_lopo.md and .json
- counterfactual: the 7154 replays A1 (recorded inputs), A2 (estimator
  quantities, LOPO units, selected quote, recorded hours and rate) and A3
  (fully engine) -> reports/pricing_counterfactual.md

Every report goes under EVAL_ROOT/reports (outside both git repos) because
it carries real project dollars. Nothing here writes to a database. Expected
7154 values are read from EVAL_ROOT/references/7154/expected.json (WS0, the
one reference file), never typed into code; `expected_values` maps its nested
keys onto the flat names used here and derives the estimator-units General
Material figure (WIRING + OTHER ITEMS material).

Holdout rule (spec 8.1, WS6): every cost library built here, the uplift
calibration and every LOPO, band or counterfactual run uses
`eval_manifest.excluded_projects("pricing_library", case_id)`, which is the
four holdout projects plus the case's own project, so the holdout is never
inside a library, a calibration pair or a threshold.

The functions are importable (tests/test_pricing_golden.py loads this file
with importlib) so the golden tests measure exactly what the reports say.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services import cost_library as cl  # noqa: E402
from app.services import estimate_parse as ep  # noqa: E402
from app.services import eval_manifest as em  # noqa: E402
from app.services import plans_reader_bands as pb  # noqa: E402
from app.services import pricing_engine as pe  # noqa: E402
from app.services import pricing_policy as pp  # noqa: E402
from app.services.electrical_items import uom_dimension  # noqa: E402
from app.services.pricing_formula import VERIFY_NUMBERS, verify_sum  # noqa: E402
from app.services.pricing_types import PricingContext, QuoteRef, SpecFact  # noqa: E402

DEFAULT_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
DEFAULT_EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
COMPANY_TZ = ZoneInfo("America/Los_Angeles")

# Golden tiers of spec 8.1 (project id prefixes). The holdout is excluded from
# every population and the core from the training pool of the calibration.
SMOKE_PREFIXES = ("7dfc64e6", "ba167b17", "dc0cb2f0")
CORE_PREFIXES = ("2bf3b21e", "061335de", "2b3586cd", "c45e3f91", "b9bd3846")
HOLDOUT_PREFIXES = ("078c470d", "ba9f0bc2", "220937f8", "346a995e")
assert frozenset(HOLDOUT_PREFIXES) == em.HOLDOUT_PREFIXES
CASE_7154_PREFIX = "7dfc64e6"
RECONCILE_TOLERANCE_USD = Decimal("1.00")
LIBRARY_PURPOSE = "pricing_library"
# Q8: PG-A2 is the gate, within 3% of the ADJ4 corrected total (a spec
# tolerance, not project data).
PG_A2_TOLERANCE = 0.03
# Q5 per-GC overrides by GC name (resolved to general_contractors.id from the
# backup at run time; the rate is a policy number).
DEFAULT_GC_OVERRIDES: dict[str, Decimal] = {"Monument": Decimal("120"), "Sletten": Decimal("120")}
_HOURS_RE = re.compile(r"Labor\s+Hours\s*[=:]\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_RATE_RE = re.compile(r"Labor\s+Rate\s*[=:]\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
FALSE_CONFIDENT_TOLERANCE = 0.35
_SECTION_OF_MARKUP = {
    "materials": "materials_markup_pct",
    "gear": "gear_markup_pct",
    "underground": "underground_markup_pct",
    "low_voltage": "low_voltage_markup_pct",
}


def now_pacific() -> str:
    return dt.datetime.now(COMPANY_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


# Backup --------------------------------------------------------------------------------


@dataclass
class Backup:
    """The backup tables this evaluation reads (spec 8.6)."""

    root: Path
    projects: dict[str, dict[str, Any]]
    project_gcs: dict[str, list[str]]
    gc_names: dict[str, str]
    sends: dict[str, list[dict[str, Any]]]
    verifications: dict[str, dict[str, Any]]
    labor_reviews: dict[str, dict[str, Any]]
    markups: dict[str, dict[str, Any]]
    gm_estimates: dict[str, dict[str, Any]]
    rfqs: dict[str, dict[str, Any]]
    categories: dict[str, dict[str, Any]]
    quotes: list[dict[str, Any]]
    golden_ids: list[str]

    @classmethod
    def load(cls, root: Path, eval_root: Path) -> Backup:
        def table(name: str) -> list[dict[str, Any]]:
            with open(root / "data" / f"{name}.json", encoding="utf-8") as fh:
                return json.load(fh)

        projects = {p["id"]: p for p in table("projects")}
        gcs: dict[str, list[str]] = {}
        for row in table("project_gcs"):
            gcs.setdefault(row["project_id"], []).append(row["gc_id"])
        sends: dict[str, list[dict[str, Any]]] = {}
        for row in table("proposal_sends"):
            if row.get("status") == "sent":
                sends.setdefault(row["project_id"], []).append(row)
        gold_path = eval_root / "proto" / "spec_ws" / "scripts" / "ws0_gold_ids.json"
        golden = json.loads(gold_path.read_text()) if gold_path.is_file() else []
        return cls(
            root=root,
            projects=projects,
            project_gcs=gcs,
            gc_names={g["id"]: g.get("name") or "" for g in table("general_contractors")},
            sends=sends,
            verifications={v["project_id"]: v for v in table("verifications")},
            labor_reviews={r["project_id"]: r for r in table("labor_reviews")},
            markups={m["project_id"]: m for m in table("markups")},
            gm_estimates={g["project_id"]: g for g in table("general_material_estimates")},
            rfqs={r["id"]: r for r in table("rfqs")},
            categories={c["id"]: c for c in table("material_categories")},
            quotes=table("quotes"),
            golden_ids=list(golden),
        )

    # Truth values ----------------------------------------------------------------

    def sent_total(self, project_id: str) -> tuple[Decimal | None, dict[str, Any]]:
        """The latest sent proposal's total (five amounts, nulls as 0) and the
        spread across the project's sends."""
        rows = self.sends.get(project_id) or []
        if not rows:
            return None, {}
        latest = max(rows, key=lambda r: r.get("sent_at") or "")
        totals = []
        for r in rows:
            totals.append(sum(
                (_dec(r.get(k)) or Decimal(0) for k in (
                    "labor_amount", "material_amount", "gear_amount", "underground_amount",
                    "low_voltage_amount")),
                Decimal(0),
            ))
        total = sum(
            (_dec(latest.get(k)) or Decimal(0) for k in (
                "labor_amount", "material_amount", "gear_amount", "underground_amount",
                "low_voltage_amount")),
            Decimal(0),
        )
        return total, {"sends": len(rows), "min": min(totals), "max": max(totals)}

    def committed_verify(self, project_id: str) -> dict[str, Decimal | None] | None:
        row = self.verifications.get(project_id)
        if not row or not row.get("committed_at"):
            return None
        return {k: _dec(row.get(k)) for k in VERIFY_NUMBERS}

    def g3_hours_rate(self, project_id: str) -> tuple[Decimal | None, Decimal | None]:
        row = self.labor_reviews.get(project_id)
        if not row:
            return None, None
        notes = row.get("labor_notes") or ""
        h = _HOURS_RE.search(notes)
        r = _RATE_RE.search(notes)
        hours = Decimal(h.group(1).replace(",", "")) if h else None
        rate = Decimal(r.group(1).replace(",", "")) if r else None
        return hours, rate

    def recorded_markups(self, project_id: str) -> dict[str, Decimal]:
        row = self.markups.get(project_id) or {}
        out: dict[str, Decimal] = {}
        for section, key in _SECTION_OF_MARKUP.items():
            out[section] = _dec(row.get(key)) or Decimal(0)
        return out

    def recorded_labor_markup_pct(self, project_id: str) -> Decimal:
        row = self.markups.get(project_id) or {}
        return _dec(row.get("labor_markup_pct")) or Decimal(0)

    def selected_quotes(self, project_id: str) -> list[dict[str, Any]]:
        """Every selected quote of the project (see project_quotes)."""
        return self.project_quotes(project_id, selected_only=True)

    def recorded_markup_amounts(self, project_id: str) -> dict[str, Decimal | None]:
        row = self.markups.get(project_id) or {}
        return {
            section: _dec(row.get(key.replace("_pct", "_amount")))
            for section, key in _SECTION_OF_MARKUP.items()
        }

    def project_quotes(self, project_id: str, *, selected_only: bool = False) -> list[dict[str, Any]]:
        """The project's quotes (approved ones, or every selected one) with
        their category name, section and `is_general` flag."""
        out: list[dict[str, Any]] = []
        for q in self.quotes:
            if selected_only and not q.get("is_selected"):
                continue
            if not selected_only and not (q.get("is_selected") or q.get("is_approved")):
                continue
            rfq = self.rfqs.get(q.get("rfq_id"))
            if not rfq or rfq.get("project_id") != project_id:
                continue
            cat = self.categories.get(rfq.get("material_category_id")) or {}
            out.append({
                "quote_id": q["id"],
                "category": cat.get("name") or "Other",
                "section": cat.get("pricing_section") or "materials",
                "is_general": bool(cat.get("is_general")),
                "amount": _dec(q.get("amount")) or Decimal(0),
                "tax_included": q.get("tax_included"),
                "tax_rate": _dec(q.get("tax_rate")),
                "vendor_id": q.get("vendor_id") or "",
                "origin": q.get("origin"),
                "is_selected": bool(q.get("is_selected")),
            })
        return out

    def quote_refs(self, project_id: str) -> tuple[list[QuoteRef], set[str]]:
        """Engine quotes: every approved or selected non-General quote of the
        project (the engine prices General Material itself) and the ids the
        humans selected."""
        refs: list[QuoteRef] = []
        selected: set[str] = set()
        for q in self.project_quotes(project_id):
            if q["is_general"]:
                continue
            refs.append(QuoteRef(
                quote_id=q["quote_id"], category=q["category"], vendor=q["vendor_id"],
                amount_usd=q["amount"], tax_included=bool(q["tax_included"]),
            ))
            if q["is_selected"]:
                selected.add(q["quote_id"])
        return refs, selected

    def gc_override_ids(self, overrides_by_name: Mapping[str, Decimal]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for gc_id, name in self.gc_names.items():
            for needle, rate in overrides_by_name.items():
                if needle.lower() in (name or "").lower():
                    out[gc_id] = rate
        return out

    def tier(self, project_id: str) -> str:
        p = project_id[:8]
        if p in HOLDOUT_PREFIXES:
            return "holdout"
        if p in CORE_PREFIXES:
            return "core"
        if p in SMOKE_PREFIXES:
            return "smoke"
        return "pool"


def library_exclusions(bk: Backup, case_id: str | None) -> frozenset[str]:
    """The project ids a library, calibration or band run must drop for
    `case_id`: `eval_manifest.excluded_projects("pricing_library", case_id)`
    (the four holdout projects plus the case's own project). The fallback
    (manifest module without the API) is the holdout prefixes of 8.1 plus
    the case, resolved against the backup."""
    try:
        return frozenset(em.excluded_projects(LIBRARY_PURPOSE, case_id))
    except AttributeError:  # pragma: no cover - older manifest module
        out = {p for p in bk.projects if p[:8] in HOLDOUT_PREFIXES}
        if case_id:
            out.add(case_id)
        return frozenset(out)


def expected_values(eval_root: Path) -> dict[str, Any]:
    """The 7154 reference figures from WS0's expected.json (the one reference
    file), mapped onto the flat names this evaluation uses. The estimator-
    units General Material figure is derived as WIRING + OTHER ITEMS material
    (general_material.pretax_usd is G3's manual GM figure, a different
    number). Seeds come through as `seeds` (duplex and GFCI hospital grade).
    Missing keys stay absent so a caller can skip."""
    path = eval_root / "references" / "7154" / "expected.json"
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text())

    def get(*keys: str) -> Any:
        node: Any = raw
        for k in keys:
            if not isinstance(node, Mapping) or k not in node:
                return None
            node = node[k]
        return node

    mapping = {
        "sent_total_usd": ("totals", "g3_sent_usd"),
        "corrected_total_usd": ("totals", "g3_corrected_usd"),
        "estimator_total_usd": ("totals", "estimator_bid_usd"),
        "g3_general_material_quote_usd": ("quotes", "general_material_manual_usd"),
        "labor_hours_recorded": ("totals", "g3_hours"),
        "labor_rate_recorded_usd": ("totals", "g3_rate_usd"),
        "estimator_labor_rate_usd": ("totals", "estimator_rate_usd"),
        "estimator_hours": ("totals", "estimator_hours"),
        "lighting_quote_codale_usd": ("quotes", "codale_pretax_usd"),
        "lighting_quote_graybar_usd": ("quotes", "graybar_pretax_usd"),
        "lighting_quote_selected_vendor": ("quotes", "selected_vendor"),
        "tax_rate_pct": ("quotes", "tax_rate_pct"),
        "general_material_wiring_usd": ("general_material", "wiring_material_usd"),
        "general_material_other_items_usd": ("general_material", "other_items_material_usd"),
        "general_material_manual_usd": ("general_material", "pretax_usd"),
    }
    out: dict[str, Any] = {"case_id": raw.get("case_id"), "project_id": raw.get("project_id")}
    for name, keys in mapping.items():
        value = get(*keys)
        if value is not None:
            out[name] = value
    wiring, other = out.get("general_material_wiring_usd"), out.get("general_material_other_items_usd")
    if wiring is not None and other is not None:
        out["general_material_pretax_usd"] = float(
            (Decimal(str(wiring)) + Decimal(str(other))).quantize(Decimal("0.01"))
        )
    for section in ("e9", "e13", "seeds"):
        node = raw.get(section)
        if isinstance(node, Mapping):
            out[section] = dict(node)
    out["pg_a2_tolerance"] = PG_A2_TOLERANCE
    return out


def seeded_policy(policy: pp.PricingPolicy, exp: Mapping[str, Any]) -> pp.PricingPolicy:
    """The policy with the Q11 grade seeds given their amounts from
    expected.json seeds.* (duplex_hospital_grade_usd, gfci_hospital_grade_usd).
    The code default carries no amount, so without the reference file the
    seeds stay inert."""
    seeds = exp.get("seeds") or {}
    out: list[pp.GradeSeed] = []
    for seed in policy.grade_seeds:
        key = "gfci_hospital_grade_usd" if any("GFCI" in k for k in seed.item_keys) else "duplex_hospital_grade_usd"
        amount = seeds.get(key)
        out.append(seed.with_amount(amount) if amount is not None else seed)
    return policy.replace(grade_seeds=tuple(out))


# Verify replay ----------------------------------------------------------------------------


@dataclass
class ReplayResult:
    project_id: str
    reproduced: Decimal
    committed: Decimal
    diff: Decimal
    within_one: bool
    numbers: dict[str, Decimal | None]
    explanation: str


def verify_rows_from_backup(bk: Backup, project_id: str) -> list[pe.CategoryRow]:
    """The Verify category rows exactly as routers/pricing._materials_rows
    builds them: every selected quote (General included when it has an RFQ),
    else the General Material estimate figure."""
    rows: list[pe.CategoryRow] = []
    saw_general = False
    for q in bk.selected_quotes(project_id):
        saw_general = saw_general or q["is_general"]
        rows.append(pe.CategoryRow(
            category=q["category"], amount_pretax=q["amount"], tax_included=q["tax_included"],
            section=q["section"], source="quote", quote_id=q["quote_id"],
        ))
    gm = bk.gm_estimates.get(project_id)
    if not saw_general and gm and gm.get("amount") is not None:
        rows.append(pe.CategoryRow(
            category="General Material", amount_pretax=_dec(gm["amount"]) or Decimal(0),
            tax_included=gm.get("tax_included"), section="materials", source="estimate",
        ))
    return rows


def replay_verification(bk: Backup, project_id: str) -> ReplayResult | None:
    committed = bk.committed_verify(project_id)
    if committed is None:
        return None
    labor = bk.labor_reviews.get(project_id) or {}
    labor_amount = _dec(labor.get("labor_amount")) or Decimal(0)
    rows = verify_rows_from_backup(bk, project_id)
    present = {k for k in ("gear", "underground", "low_voltage") if committed.get(f"{k}_amount") is not None}
    tax_rate = next((r for r in (_dec(q["tax_rate"]) for q in bk.selected_quotes(project_id)) if r), Decimal("8.375"))
    markup_row = bk.markups.get(project_id) or {}
    breakdown = pe.verify_totals(
        labor_hours=Decimal(1), labor_rate=labor_amount, category_rows=rows,
        tax_rate_pct=tax_rate, material_markup_pct=bk.recorded_markups(project_id),
        labor_markup_pct=bk.recorded_labor_markup_pct(project_id), present_sections=present,
        markup_amounts=bk.recorded_markup_amounts(project_id),
        labor_markup_amount=_dec(markup_row.get("labor_markup_amount")),
    )
    committed_sum = verify_sum(committed)
    diff = breakdown.bid_total - committed_sum
    explanation = ""
    if abs(diff) > RECONCILE_TOLERANCE_USD:
        parts = []
        for key in VERIFY_NUMBERS:
            a, b = breakdown.numbers.get(key), committed.get(key)
            if (a or Decimal(0)) != (b or Decimal(0)):
                parts.append(f"{key}: replay {a} vs committed {b}")
        if any(k.startswith("labor_amount") for k in parts):
            explanation = "labor figure overridden at Verify; " + "; ".join(parts)
        elif not rows:
            explanation = "no selected quotes or GM figure in the backup; " + "; ".join(parts)
        elif all("_amount: " in k and "markup" not in k for k in parts):
            explanation = "section amount overridden at Verify or quote changed after commit; " + "; ".join(parts)
        else:
            explanation = "; ".join(parts)
    return ReplayResult(
        project_id=project_id, reproduced=breakdown.bid_total, committed=committed_sum,
        diff=diff, within_one=abs(diff) <= RECONCILE_TOLERANCE_USD, numbers=breakdown.numbers,
        explanation=explanation,
    )


def run_verify_replay(bk: Backup) -> list[ReplayResult]:
    out = []
    for pid in sorted(bk.verifications):
        res = replay_verification(bk, pid)
        if res is not None:
            out.append(res)
    return out


def verify_replay_markdown(results: Sequence[ReplayResult], case_7154: ReplayResult | None,
                           expected_sent: Any) -> str:
    within = sum(1 for r in results if r.within_one)
    lines = [
        "# Verify replay (committed verifications rebuilt through verify_totals)",
        "",
        f"Generated {now_pacific()}.",
        "",
        f"Committed verifications: {len(results)}; within $1: {within} "
        f"({within / len(results):.1%} of them)." if results else "No committed verifications.",
        "",
    ]
    if case_7154 is not None:
        exact = expected_sent is not None and case_7154.reproduced == Decimal(str(expected_sent))
        lines.append(
            f"7154: replay {case_7154.reproduced} vs expected sent {expected_sent}: "
            f"{'EXACT' if exact else 'DIFFERENT'}."
        )
        lines.append("")
    lines.append("| Project | Replay | Committed | Diff | Within $1 | Explanation |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        lines.append(
            f"| {r.project_id[:8]} | {r.reproduced} | {r.committed} | {r.diff} | "
            f"{'yes' if r.within_one else 'no'} | {r.explanation} |"
        )
    return "\n".join(lines) + "\n"


# LOPO -----------------------------------------------------------------------------------------


@dataclass
class CaseRun:
    """One LOPO case priced three ways."""

    project_id: str
    tier: str
    sent: Decimal | None
    verify_committed: Decimal | None
    reconciled: bool
    g3_hours: Decimal | None
    g3_rate: Decimal | None
    wage_type: str | None
    estimator_hours: Decimal | None
    basis_hours: Decimal
    uplift: Decimal | None = None
    coverage: str = ""
    oracle_bid: Decimal | None = None
    oracle_status: str = ""
    band_bid: Decimal | None = None
    clean: bool | None = None
    strong_good_share: float = 0.0
    hours_error: float | None = None
    false_confident: tuple[int, int] = (0, 0)
    hard_reject_violations: int = 0
    lines: int = 0
    methods: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    library_excluded: tuple[str, ...] = ()  # the ids the case's library was built without

    @property
    def oracle_error(self) -> float | None:
        if self.oracle_bid is None or not self.sent:
            return None
        return float((self.oracle_bid - self.sent) / self.sent)

    @property
    def band_error(self) -> float | None:
        if self.band_bid is None or not self.sent:
            return None
        return float((self.band_bid - self.sent) / self.sent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project_id[:8], "tier": self.tier, "sent": _f(self.sent),
            "verify_committed": _f(self.verify_committed), "reconciled": self.reconciled,
            "g3_hours": _f(self.g3_hours), "g3_rate": _f(self.g3_rate),
            "wage_type": self.wage_type, "estimator_hours": _f(self.estimator_hours),
            "basis_hours": _f(self.basis_hours), "uplift": _f(self.uplift), "coverage": self.coverage,
            "oracle_bid": _f(self.oracle_bid), "oracle_error": self.oracle_error,
            "oracle_status": self.oracle_status, "band_bid": _f(self.band_bid),
            "band_error": self.band_error, "clean": self.clean,
            "strong_good_share": self.strong_good_share, "hours_error": self.hours_error,
            "false_confident": list(self.false_confident),
            "hard_reject_violations": self.hard_reject_violations, "lines": self.lines,
            "methods": self.methods, "warnings": self.warnings, "notes": self.notes,
            "library_excluded": [p[:8] for p in sorted(self.library_excluded)],
        }


def lopo_population(bk: Backup, chosen: Mapping[str, ep.ParsedEstimate]) -> tuple[list[str], dict[str, str]]:
    """The 8.7 LOPO population and the reason each dropped candidate left."""
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for pid in bk.golden_ids:
        if bk.tier(pid) == "holdout":
            dropped[pid] = "holdout"
            continue
        if pid not in chosen:
            dropped[pid] = "no parsed estimate workbook"
            continue
        committed = bk.committed_verify(pid)
        sent, _spread = bk.sent_total(pid)
        if committed is None:
            dropped[pid] = "no committed Verify"
            continue
        if sent is None:
            dropped[pid] = "no sent proposal"
            continue
        if abs(verify_sum(committed) - sent) > RECONCILE_TOLERANCE_USD:
            dropped[pid] = "reconcile filter (Verify sum != sent total)"
            continue
        kept.append(pid)
    return kept, dropped


def _context(bk: Backup, pid: str, *, overrides: Mapping[str, Decimal], recorded_rate: Decimal | None,
             book: ep.ParsedEstimate, excluded: Iterable[str] = ()) -> PricingContext:
    project = bk.projects.get(pid) or {}
    wage = project.get("wage_type")
    if wage not in ("prevailing_wage", "non_prevailing_wage"):
        wage = pp.DEFAULT_WAGE_TYPE  # Q13 default; the engine marks the rate source default
        source = "default"
    else:
        source = "policy"
    if recorded_rate is not None:
        source = "recorded"
    return PricingContext(
        wage_type=wage,
        labor_rate_usd=recorded_rate if recorded_rate is not None else Decimal(0),
        labor_rate_source=source,
        rate_overrides=dict(overrides),
        project_gc_ids=tuple(bk.project_gcs.get(pid) or ()),
        allowances=ep.allowances(book),
        exclude_project_ids=frozenset(excluded) | {pid},
        eval_case_id=pid[:8],
    )


def _line_audit(bk_book: ep.ParsedEstimate, est: Any) -> tuple[tuple[int, int], int, float]:
    """(false-confident (hits, strong_good_lines), hard-reject violations,
    strong-or-good share of basis hours) for one priced estimate against the
    estimator's own unit values."""
    est_lines = {ln.line_id: ln for ln in est.lines}
    rows = ep.price_rows(bk_book)
    by_id = {}
    n = 0
    for ln in bk_book.item_lines:
        q = ln.quantity_eff
        if q is None or q <= 0:
            continue
        n += 1
        by_id[f"B{n}"] = ln
    hits = confident = violations = 0
    sg_hours = Decimal(0)
    total_hours = Decimal(0)
    for row in rows:
        pl = est_lines.get(row.line_id)
        src = by_id.get(row.line_id)
        if pl is None or src is None or pl.labor_hours is None:
            continue
        total_hours += pl.labor_hours
        line_quality = pl.labor.quality
        if pl.material.quality in ("weak", "missing") and line_quality in ("strong", "good") and pl.material_source not in ("seed",) and "quote_covered" not in pl.flags:
            line_quality = "weak"
        if line_quality in ("strong", "good"):
            sg_hours += pl.labor_hours
            confident += 1
            est_h = float(src.unit_labor_hours or 0)
            got_h = float(pl.labor.value or 0)
            off = est_h > 0 and abs(got_h - est_h) / est_h > FALSE_CONFIDENT_TOLERANCE
            est_m = float(src.unit_material_cost or 0)
            if (not off and est_m > 0 and "quote_covered" not in pl.flags and pl.material.value is not None):
                off = abs(float(pl.material.value) - est_m) / est_m > FALSE_CONFIDENT_TOLERANCE
            if off:
                hits += 1
        if pl.labor.method in ("exact", "compatible") and pl.item_key:
            matched = next((c for c in pl.candidates if c.get("matched")), None)
            if matched is None:
                continue
            try:
                same_dim = uom_dimension(matched.get("uom") or "") == uom_dimension(row.unit)
            except ValueError:
                same_dim = False
            q_attrs, q_mods = cl.key_attrs(pl.item_key)
            m_attrs, m_mods = cl.key_attrs(str(matched["item_key"]))
            shared_differ = any(q_attrs[k] != m_attrs[k] for k in q_attrs if k in m_attrs)
            if not same_dim or shared_differ or q_mods != m_mods:
                violations += 1
    share = float(sg_hours / total_hours) if total_hours else 0.0
    return (hits, confident), violations, share


def run_lopo(bk: Backup, *, overrides_by_name: Mapping[str, Decimal] = DEFAULT_GC_OVERRIDES,
             policy: pp.PricingPolicy = pp.DEFAULT_POLICY, population: Sequence[str] | None = None,
             include_training: bool = True) -> dict[str, Any]:
    """The LOPO backtest. Returns the per-case runs, the calibration and the
    aggregate metrics (shares and percentages; dollars only per case)."""
    report, chosen = cl.backup_books(bk.root)
    holdout = library_exclusions(bk, None)
    # The holdout never enters any library (8.1): drop its observations once.
    observations = [o for o in cl.backup_observations(bk.root) if o.project_id not in holdout]
    overrides = bk.gc_override_ids(overrides_by_name)
    cases, dropped = lopo_population(bk, chosen)
    if population is not None:
        cases = [c for c in population if c in chosen and c not in holdout]
    # Pass 1: engine basis hours and coverage regime per case (LOPO library)
    # for every project with a workbook and G3 hours: the calibration triples.
    basis: dict[str, Decimal] = {}
    coverage_by_pid: dict[str, str] = {}
    pairs_by_pid: dict[str, tuple[float, float, str]] = {}
    training = [pid for pid in chosen if bk.tier(pid) in ("pool", "smoke") and pid not in holdout]
    excluded_by_pid: dict[str, frozenset[str]] = {}
    for pid in sorted(set(cases) | set(training)):
        book = chosen[pid]
        excl = library_exclusions(bk, pid)
        excluded_by_pid[pid] = excl
        lib = cl.CostLibrary.from_observations(observations, exclude_projects=excl, thresholds=policy.quality)
        ctx = _context(bk, pid, overrides=overrides, recorded_rate=None, book=book, excluded=excl)
        est = pe.compute_estimate(ep.price_rows(book), lib, policy, [], ctx, exclude_project=pid)
        basis[pid] = est.verify.labor_hours_basis if est.verify else Decimal(0)
        coverage_by_pid[pid] = str(est.quality_summary.get("coverage_class") or pp.COVERAGE_COVERED)
        hours, _rate = bk.g3_hours_rate(pid)
        if hours and basis[pid] > 0 and pid in training:
            pairs_by_pid[pid] = (float(basis[pid]), float(hours), coverage_by_pid[pid])
    calibration = pp.calibrate_uplift_by_coverage(
        list(pairs_by_pid.values()), band=policy.clean_band, steps=policy.hour_roundup_steps,
        fallback_covered=policy.hour_uplift, fallback_under_covered=policy.hour_uplift_under_covered,
    )
    runs: list[CaseRun] = []
    for pid in cases:
        book = chosen[pid]
        loo_pairs = [v for k, v in pairs_by_pid.items() if k != pid]
        cal = pp.calibrate_uplift_by_coverage(
            loo_pairs, band=policy.clean_band, steps=policy.hour_roundup_steps,
            fallback_covered=policy.hour_uplift, fallback_under_covered=policy.hour_uplift_under_covered,
        )
        uplift = cal.uplift_for(coverage_by_pid[pid])
        excl = excluded_by_pid[pid]
        lib = cl.CostLibrary.from_observations(observations, exclude_projects=excl, thresholds=policy.quality)
        quotes, selected = bk.quote_refs(pid)
        sent, _spread = bk.sent_total(pid)
        committed = bk.committed_verify(pid)
        g3_hours, g3_rate = bk.g3_hours_rate(pid)
        rows = ep.price_rows(book)
        case_policy = policy.replace(hour_uplift=cal.chosen_covered, hour_uplift_under_covered=cal.chosen_under_covered)
        ctx = _context(bk, pid, overrides=overrides, recorded_rate=None, book=book, excluded=excl)
        oracle = pe.compute_estimate(rows, lib, case_policy, quotes, ctx, exclude_project=pid,
                                     selected_quote_ids=selected)
        run = CaseRun(
            project_id=pid, tier=bk.tier(pid), sent=sent,
            verify_committed=verify_sum(committed) if committed else None,
            reconciled=pid in cases, g3_hours=g3_hours, g3_rate=g3_rate,
            wage_type=(bk.projects.get(pid) or {}).get("wage_type"),
            estimator_hours=book.total_labor_hours, basis_hours=basis[pid], uplift=uplift,
            coverage=str(oracle.quality_summary.get("coverage_class") or coverage_by_pid[pid]),
            oracle_bid=oracle.verify.bid_total if oracle.verify else None,
            oracle_status=oracle.status, lines=len(rows),
            methods=dict(oracle.quality_summary.get("methods") or {}),
            warnings=[w.code for w in oracle.warnings],
            library_excluded=tuple(sorted(lib.excluded_projects)),
        )
        fc, violations, share = _line_audit(book, oracle)
        run.false_confident, run.hard_reject_violations, run.strong_good_share = fc, violations, share
        if book.total_labor_hours:
            run.hours_error = float(abs(basis[pid] - book.total_labor_hours) / book.total_labor_hours)
        # Golden Verify band: recorded rate and markups, selected quotes, engine GM.
        if g3_hours and g3_rate and basis[pid] > 0:
            ratio = g3_hours / basis[pid]
            run.clean = policy.clean_band[0] <= ratio <= policy.clean_band[1]
            band_policy = case_policy.replace(
                material_markup_pct=bk.recorded_markups(pid),
                labor_markup_pct=bk.recorded_labor_markup_pct(pid),
            )
            band_ctx = _context(bk, pid, overrides=overrides, recorded_rate=g3_rate, book=book, excluded=excl)
            band = pe.compute_estimate(rows, lib, band_policy, quotes, band_ctx, exclude_project=pid,
                                       selected_quote_ids=selected)
            run.band_bid = band.verify.bid_total if band.verify else None
        else:
            run.notes.append("no parseable G3 hours and rate: band skipped")
        runs.append(run)
    return {
        "generated_at": now_pacific(),
        "population": cases,
        "dropped": dropped,
        "holdout_excluded": sorted(holdout),
        "library_observations": len(observations),
        "training_pairs": len(pairs_by_pid),
        "training_pairs_by_coverage": {
            cov: sum(1 for _b, _g, c in pairs_by_pid.values() if c == cov) for cov in pp.COVERAGE_CLASSES
        },
        "calibration": calibration.pooled.to_dict(),
        "calibration_by_coverage": calibration.to_dict(),
        "runs": runs,
        "metrics": lopo_metrics(runs),
        "parse": {"workbooks": len(report.entries), "parsed": report.parsed, "projects": report.projects},
    }


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def lopo_metrics(runs: Sequence[CaseRun]) -> dict[str, Any]:
    oracle = [abs(r.oracle_error) for r in runs if r.oracle_error is not None]
    refused = sum(1 for r in runs if r.oracle_status == "blocked")
    # refused cases count as misses (1.3 item 4 scoring)
    oracle_scored = oracle + [1.0] * refused
    band = [abs(r.band_error) for r in runs if r.band_error is not None and r.clean]
    band_all = [abs(r.band_error) for r in runs if r.band_error is not None]
    hours = [r.hours_error for r in runs if r.hours_error is not None]
    fc_hits = sum(r.false_confident[0] for r in runs)
    fc_n = sum(r.false_confident[1] for r in runs)
    shares = [r.strong_good_share for r in runs]
    return {
        "n_cases": len(runs),
        "n_refused": refused,
        "oracle_median_abs_error": _median(oracle_scored),
        "oracle_within_10": (sum(1 for e in oracle_scored if e <= 0.10) / len(oracle_scored)) if oracle_scored else None,
        "oracle_within_25": (sum(1 for e in oracle_scored if e <= 0.25) / len(oracle_scored)) if oracle_scored else None,
        "band_clean_n": len(band),
        "band_median_abs_error": _median(band),
        "band_within_10": (sum(1 for e in band if e <= 0.10) / len(band)) if band else None,
        "band_all_n": len(band_all),
        "band_all_median_abs_error": _median(band_all),
        "band_all_within_10": (sum(1 for e in band_all if e <= 0.10) / len(band_all)) if band_all else None,
        "strong_good_share_median": _median(shares),
        "hours_median_abs_error": _median(hours),
        "hours_within_10": (sum(1 for e in hours if e <= 0.10) / len(hours)) if hours else None,
        "false_confident_rate": (fc_hits / fc_n) if fc_n else 0.0,
        "false_confident_lines": fc_n,
        "hard_reject_violations": sum(r.hard_reject_violations for r in runs),
    }


def _calibration_lines(result: Mapping[str, Any], pct) -> list[str]:
    """The pooled calibration line plus one per coverage regime."""
    cal = result["calibration"]
    lines = [
        f"pooled: pairs {cal['n_pairs']}, clean {cal['n_clean']}, median {cal['median']}, p25 {cal['p25']}, p75 {cal['p75']}; "
        f"LOO median abs error with u = median {pct(cal['loo_median_abs_error_median_u'])} (within 10%: {pct(cal['loo_within_10_median_u'])}), "
        f"with u = p25 {pct(cal['loo_median_abs_error_p25_u'])} (within 10%: {pct(cal['loo_within_10_p25_u'])}); chosen {cal['chosen']} = {cal['chosen_uplift']}.",
    ]
    by_cov = result.get("calibration_by_coverage") or {}
    for cov in pp.COVERAGE_CLASSES:
        c = by_cov.get(cov)
        if not c:
            continue
        lines.append(
            f"{cov}: pairs {c['n_pairs']}, clean {c['n_clean']}, median {c['median']}, p25 {c['p25']}, p75 {c['p75']}; "
            f"LOO median abs error with u = median {pct(c['loo_median_abs_error_median_u'])} (within 10%: {pct(c['loo_within_10_median_u'])}); "
            f"applied {by_cov.get(f'chosen_{cov}')} (source {by_cov.get(f'{cov}_source')})."
        )
    return lines


def lopo_markdown(result: Mapping[str, Any]) -> str:
    m = result["metrics"]

    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.1%}"

    lines = [
        "# WS6 LOPO pricing backtest",
        "",
        f"Generated {result['generated_at']}. Population {m['n_cases']} cases "
        f"(dropped: {len(result['dropped'])}); training pairs for the uplift {result['training_pairs']}.",
        "",
        f"Every library is built with eval_manifest.excluded_projects(\"pricing_library\", case_id): the "
        f"{len(result.get('holdout_excluded') or [])} holdout projects "
        f"({', '.join(p[:8] for p in result.get('holdout_excluded') or [])}) plus the case's own project; "
        f"{result.get('library_observations', 'n/a')} observations remain before the per-case exclusion.",
        "",
        "## Gates (spec WS6 acceptance and 1.3 item 4)",
        "",
        "| Metric | Measured | Target |",
        "|---|---|---|",
        f"| strong or good share of basis hours (median per project) | {pct(m['strong_good_share_median'])} | >= 70% |",
        f"| workbook-basis hours within 10% | {pct(m['hours_within_10'])} | >= 65% |",
        f"| workbook-basis hours median abs error | {pct(m['hours_median_abs_error'])} | <= 8% |",
        f"| false-confident rate (strong/good lines off by > 35%) | {pct(m['false_confident_rate'])} (of {m['false_confident_lines']} lines) | <= 5% |",
        f"| hard-reject violations | {m['hard_reject_violations']} | 0 |",
        f"| golden Verify band (clean set, n={m['band_clean_n']}) median abs error | {pct(m['band_median_abs_error'])} | <= 8% |",
        f"| golden Verify band within 10% | {pct(m['band_within_10'])} | >= 60% |",
        f"| golden Verify band (all cases, n={m['band_all_n']}) median / within 10% | {pct(m['band_all_median_abs_error'])} / {pct(m['band_all_within_10'])} | report |",
        f"| 1.3 item 4 oracle: median abs error vs G3 sent | {pct(m['oracle_median_abs_error'])} | <= 7% |",
        f"| 1.3 item 4 oracle: within 10% | {pct(m['oracle_within_10'])} | >= 70% |",
        f"| oracle refusals (counted as misses) | {m['n_refused']} | report |",
        "",
        "## Uplift calibration (training pool, clean band, pooled and per coverage regime)",
        "",
        *_calibration_lines(result, pct),
        "",
        "## Per case",
        "",
        "| Project | Tier | Wage | Lines | Basis h | Est h | G3 h | Coverage | Uplift | Oracle err | Band err | Clean | S/G share | FC | Status | Warnings |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in result["runs"]:
        lines.append(
            f"| {r.project_id[:8]} | {r.tier} | {r.wage_type or '-'} | {r.lines} | {r.basis_hours:.1f} | "
            f"{(r.estimator_hours or 0):.1f} | {r.g3_hours or '-'} | {r.coverage or '-'} | {r.uplift} | {pct(r.oracle_error)} | "
            f"{pct(r.band_error)} | {r.clean} | {pct(r.strong_good_share)} | "
            f"{r.false_confident[0]}/{r.false_confident[1]} | {r.oracle_status} | {', '.join(r.warnings)} |"
        )
    lines.append("")
    lines.append("## Dropped candidates")
    lines.append("")
    for pid, why in sorted(result["dropped"].items()):
        lines.append(f"- {pid[:8]}: {why}")
    return "\n".join(lines) + "\n"


def calibrate_markdown(result: Mapping[str, Any]) -> str:
    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.1%}"

    lines = [
        "# Hour uplift calibration (engine basis, training pool, clean band)",
        "",
        f"Generated {result['generated_at']}. Training pairs by coverage regime: "
        f"{json.dumps(result.get('training_pairs_by_coverage') or {})}.",
        "",
        *_calibration_lines(result, pct),
        "",
        "```",
        json.dumps(result.get("calibration_by_coverage") or result["calibration"], indent=2),
        "```",
        "",
        "Pairs are (engine basis hours with the project left out of the library, G3 reviewer hours "
        "from labor_notes, the coverage regime of that LOPO estimate) over the training pool (golden "
        "pool and smoke, never core or holdout). Every library excludes the four holdout projects and "
        "the pair's own project (eval_manifest.excluded_projects(\"pricing_library\", case_id)). The "
        "regime is PricingPolicy.coverage_class: covered when the fallback share of the basis hours and "
        "the share of lines matched to nothing both stay within the policy limits, else under_covered.",
    ]
    return "\n".join(lines) + "\n"


# Counterfactual (7154) ---------------------------------------------------------------------


def run_counterfactual(bk: Backup, eval_root: Path, *, policy: pp.PricingPolicy = pp.DEFAULT_POLICY,
                       uplift: Decimal | None = None) -> dict[str, Any]:
    exp = expected_values(eval_root)
    pid = next((p for p in bk.projects if p.startswith(CASE_7154_PREFIX)), None)
    if pid is None:
        raise SystemExit("7154 is not in the backup")
    _report, chosen = cl.backup_books(bk.root)
    book = chosen[pid]
    policy = seeded_policy(policy, exp)
    excl = library_exclusions(bk, pid)
    observations = cl.backup_observations(bk.root)
    lib = cl.CostLibrary.from_observations(observations, exclude_projects=excl, thresholds=policy.quality)
    overrides = bk.gc_override_ids(DEFAULT_GC_OVERRIDES)
    rows = ep.price_rows(book)
    quotes, selected = bk.quote_refs(pid)
    g3_hours, g3_rate = bk.g3_hours_rate(pid)
    out: dict[str, Any] = {
        "generated_at": now_pacific(), "project": pid[:8],
        "library_excluded": [p[:8] for p in sorted(lib.excluded_projects)],
        "seeds_active": sum(1 for sd in policy.grade_seeds if sd.active),
    }
    # A1: recorded inputs through the formula.
    replay = replay_verification(bk, pid)
    out["a1_replay"] = _f(replay.reproduced) if replay else None
    out["a1_expected_sent"] = exp.get("sent_total_usd")
    out["a1_exact"] = replay is not None and exp.get("sent_total_usd") is not None and replay.reproduced == Decimal(str(exp["sent_total_usd"]))
    # General Material pre-tax with the estimator's own units (the leak check).
    out["general_material_pretax_estimator_units"] = _f(general_material_pretax(book))
    out["general_material_expected"] = exp.get("general_material_pretax_usd")
    # A2: estimator quantities, LOPO units, selected quote, recorded hours and rate.
    corrected = _dec(exp.get("corrected_total_usd"))
    sent = _dec(exp.get("sent_total_usd"))
    ctx_rec = PricingContext(
        wage_type="prevailing_wage", labor_rate_usd=g3_rate or Decimal(0), labor_rate_source="recorded",
        project_gc_ids=tuple(bk.project_gcs.get(pid) or ()), allowances=ep.allowances(book),
        exclude_project_ids=excl, eval_case_id="7154",
    )
    a2 = pe.compute_estimate(rows, lib, policy, quotes, ctx_rec, exclude_project=pid,
                             selected_quote_ids=selected, reviewer_hours=g3_hours)
    out["a2_bid"] = _f(a2.verify.bid_total)
    out["a2_vs_corrected"] = _f((a2.verify.bid_total - corrected) / corrected) if corrected else None
    out["a2_vs_sent"] = _f((a2.verify.bid_total - sent) / sent) if sent else None
    out["a2_within_tolerance"] = (
        out["a2_vs_corrected"] is not None and abs(out["a2_vs_corrected"]) <= float(exp.get("pg_a2_tolerance", PG_A2_TOLERANCE))
    )
    a2_low = pe.compute_estimate(rows, lib, policy, quotes, ctx_rec, exclude_project=pid, reviewer_hours=g3_hours)
    out["a2_lowest_quote_bid"] = _f(a2_low.verify.bid_total)
    # A3: fully engine, default policy, with and without the hospital-grade fact.
    # An explicit --uplift pins both coverage regimes.
    case_policy = policy if uplift is None else policy.replace(hour_uplift=uplift, hour_uplift_under_covered=uplift)
    ctx = PricingContext(
        wage_type="prevailing_wage", labor_rate_usd=Decimal(0), labor_rate_source="policy",
        rate_overrides=overrides, project_gc_ids=tuple(bk.project_gcs.get(pid) or ()),
        allowances=ep.allowances(book), exclude_project_ids=excl, eval_case_id="7154",
    )
    a3 = pe.compute_estimate(rows, lib, case_policy, quotes, ctx, exclude_project=pid, selected_quote_ids=selected)
    out["a3_bid"] = _f(a3.verify.bid_total)
    out["a3_hours_basis"] = _f(a3.verify.labor_hours_basis)
    out["a3_hours_reviewer"] = _f(a3.verify.labor_hours_reviewer)
    out["a3_coverage"] = a3.quality_summary.get("coverage_class")
    out["a3_uplift"] = _f(Decimal(str(a3.quality_summary.get("hour_uplift_applied") or case_policy.hour_uplift)))
    out["a3_vs_corrected"] = _f((a3.verify.bid_total - corrected) / corrected) if corrected else None
    out["a3_vs_sent"] = _f((a3.verify.bid_total - sent) / sent) if sent else None
    out["a3_status"] = a3.status
    out["a3_warnings"] = [w.code for w in a3.warnings]
    fact = SpecFact(key="device_grade", value="hospital", source_ref="E2.01 sheet note 10")
    ctx_hg = PricingContext(
        wage_type="prevailing_wage", labor_rate_usd=Decimal(0), labor_rate_source="policy",
        rate_overrides=overrides, project_gc_ids=tuple(bk.project_gcs.get(pid) or ()),
        allowances=ep.allowances(book), spec_facts=(fact,), exclude_project_ids=excl,
        eval_case_id="7154",
    )
    a3_hg = pe.compute_estimate(rows, lib, case_policy, quotes, ctx_hg, exclude_project=pid, selected_quote_ids=selected)
    out["a3_with_grade_fact_bid"] = _f(a3_hg.verify.bid_total)
    out["a3_with_grade_fact_seed_lines"] = sum(1 for ln in a3_hg.lines if "grade_seed" in ln.flags)
    out["a3_counterfactual_hours_637"] = _f(a3.verify.labor_hours_basis)
    # PG-A3 restated against the latest history bands file (WS7, file-based in
    # wave 1): the A3 artifact summarised on the estimator basis and placed
    # against the LOPO bands of its stratum, the 7154 project left out.
    out["a3_band_checks"] = a3_band_checks(a3, eval_root, pid, (bk.projects.get(pid) or {}).get("project_type"))
    return out


def a3_band_checks(estimate: Any, eval_root: Path, pid: str, project_type: str | None) -> dict[str, Any]:
    """`plans_reader_bands.check_bands` over the PG-A3 artifact against the
    latest `EVAL_ROOT/bands/hb*.json`; empty with a reason when no bands file
    exists. Money metrics keep their values (the report lives under EVAL_ROOT)."""
    path = pb.latest_bands_path(eval_root)
    if path is None:
        return {"band_version": None, "reason": "no bands file under EVAL_ROOT/bands", "checks": []}
    bands = pb.load_bands(path)
    doc = pe.to_priced_estimate_v1(estimate)
    summary = pb.summary_from_priced(doc, project_id=pid, project_type=project_type)
    checks = pb.check_bands(summary, bands)
    return {
        "band_version": bands.band_version,
        "project_type": project_type,
        "stratum": checks[0].stratum if checks else None,
        "devices": summary.devices,
        "checks": [c.to_dict() for c in checks],
    }


def general_material_pretax(book: ep.ParsedEstimate) -> Decimal:
    """General Material pre-tax at the estimator's own units: the WIRING and
    OTHER ITEMS material, each group rounded to cents (a recap row), summed."""
    by_group: dict[str, Decimal] = {}
    for ln in book.item_lines:
        q = ln.quantity_eff
        if q and ln.unit_material_cost and ln.group in ("WIRING", "OTHER ITEMS"):
            by_group[ln.group] = by_group.get(ln.group, Decimal(0)) + ln.unit_material_cost * q
    return sum((v.quantize(Decimal("0.01")) for v in by_group.values()), Decimal(0))


def counterfactual_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# 7154 counterfactuals", "", f"Generated {result['generated_at']}.", "",
        "Library built with eval_manifest.excluded_projects(\"pricing_library\", \"7154\"): "
        f"excluded {', '.join(result.get('library_excluded') or [])} (holdout plus 7154).",
        "",
    ]
    bands = result.get("a3_band_checks") or {}
    checks = bands.get("checks") or []
    lines += ["## PG-A3 against history bands", ""]
    if not checks:
        lines.append(f"No band checks: {bands.get('reason', 'no checks')}.")
    else:
        lines += [
            f"Bands {bands.get('band_version')}, stratum {bands.get('stratum')}, project_type {bands.get('project_type')}.",
            "",
            "| Metric | Value | n | p10 | p50 | p90 | Position | Percentile | Severity | Level | Reason |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for c in checks:
            lines.append(
                f"| {c['metric']} | {c['value']} | {c['n']} | {c['p10']} | {c['p50']} | {c['p90']} | "
                f"{c['position']} | {c['percentile']} | {c['severity']} | {c['level']} | {c['reason']} |"
            )
    lines += ["", "## Full result", "", "```", json.dumps(result, indent=2, default=str), "```", ""]
    return "\n".join(lines)


# CLI -----------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", required=True,
                    choices=("parse-dryrun", "verify-replay", "calibrate", "lopo", "counterfactual"))
    ap.add_argument("--backup", default=str(DEFAULT_BACKUP))
    ap.add_argument("--eval-root", default=str(DEFAULT_EVAL_ROOT))
    ap.add_argument("--uplift", default=None,
                    help="hour uplift for the counterfactual A3, pinned for both coverage regimes (default: policy)")
    args = ap.parse_args(argv)
    backup = Path(args.backup)
    eval_root = Path(args.eval_root)
    reports = eval_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if args.mode == "parse-dryrun":
        report, _chosen = cl.backup_books(backup)
        text = ep.dry_run_markdown(report, generated_at=now_pacific())
        (reports / "estimate_parse_dryrun.md").write_text(text)
        print(f"parsed {report.parsed} of {len(report.entries)} workbooks over {report.projects} projects; "
              f"failures {len(report.failed)} -> {reports / 'estimate_parse_dryrun.md'}")
        return 0
    bk = Backup.load(backup, eval_root)
    if args.mode == "verify-replay":
        results = run_verify_replay(bk)
        case = next((r for r in results if r.project_id.startswith(CASE_7154_PREFIX)), None)
        exp = expected_values(eval_root)
        text = verify_replay_markdown(results, case, exp.get("sent_total_usd"))
        (reports / "pricing_verify_replay.md").write_text(text)
        within = sum(1 for r in results if r.within_one)
        print(f"{within} of {len(results)} committed verifications within $1 -> {reports / 'pricing_verify_replay.md'}")
        return 0
    if args.mode in ("calibrate", "lopo"):
        result = run_lopo(bk)
        if args.mode == "calibrate":
            (reports / "pricing_calibrate.md").write_text(calibrate_markdown(result))
            print(json.dumps(result["calibration"], indent=2))
            return 0
        (reports / "pricing_lopo.md").write_text(lopo_markdown(result))
        payload = {k: v for k, v in result.items() if k != "runs"}
        payload["runs"] = [r.to_dict() for r in result["runs"]]
        (reports / "pricing_lopo.json").write_text(json.dumps(payload, indent=2, default=str))
        print(json.dumps(result["metrics"], indent=2))
        return 0
    result = run_counterfactual(bk, eval_root, uplift=Decimal(args.uplift) if args.uplift else None)
    (reports / "pricing_counterfactual.md").write_text(counterfactual_markdown(result))
    print(json.dumps({k: v for k, v in result.items() if k != "generated_at"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
