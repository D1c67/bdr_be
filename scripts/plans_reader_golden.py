"""Golden-set scorer for AI Estimating BOQs (docs/PLANS_READER.md section 4).

Compares an AI-produced BOQ spec (the BOQ_SPEC JSON from
app/services/plans_reader_schemas.py) with the estimator's real
`BOQ - COUNTS.xlsx` for the same project. Quantities only; no pricing.

Estimator workbook layout (confirmed on the 2026-10-01 production backup):
a blank 'Sheet1' (sometimes 'Chart1'), then 'SHEET' or one sheet per area.
Row 4 holds the headers A 'SR. NO.', B 'DESCRIPTION', C 'QUANTITY', D 'UNIT'.
Rows with B filled and C empty are headings: level 0 DEMOLITION / NEW WORK,
level 1 groups (WIRING, SWITCHGEAR, OTHER ITEMS, ...; centered and bold), then
subheads (CONDUCTORS (AWG), CONDUITS, ...; left aligned and bold). A heading
may carry a note on its second line. Item rows have a numeric C and a unit in
D. Unnumbered right-aligned rows are generated accessories (connectors,
couplings, straps, anchors, screws, adapters, locknuts, elbows).

Usage:
  uv run python scripts/plans_reader_golden.py --spec spec.json --estimator boq.xlsx [--json out.json]
  uv run python scripts/plans_reader_golden.py --spec spec.json --from-backup 026b2dc5
  uv run python scripts/plans_reader_golden.py --from-backup 026b2dc5   (structure only)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_BE_ROOT = Path(__file__).resolve().parents[1]
if str(_BE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BE_ROOT))

# Item identity, normalization and row similarity live in
# app/services/electrical_items.py (AI_ESTIMATING_BUILD_SPEC.md 5.1); the
# script re-exports every moved name so callers and tests are unchanged.
from app.services.electrical_items import *  # noqa: E402, F403
from app.services.electrical_items import (  # noqa: E402, F401
    BOQ_GROUPS,
    BOQ_LEVEL0,
    BOQ_SUBGROUPS,
    EXACT_TOLERANCE,
    MATCH_THRESHOLD,
    UNITS,
    WASTAGE_FACTOR,
    BoqRow,
    _ACCESSORY_WORDS,
    _CAVEAT_WORDS,
    _CONFLICT_PENALTY,
    _DASHES,
    _EXTRA_SUBGROUPS,
    _FRACTIONS,
    _KEY_PATTERNS,
    _KNOWN_SUBGROUPS,
    _MODIFIERS,
    _POLE_WORDS,
    _QUOTES,
    _SUBGROUP_PARENT,
    _SYNONYMS,
    _TAG_RE,
    _UNIT_ALIASES,
    _VOLTAGE_RE,
    _canonical,
    _clean_text,
    _heading_name,
    _normalize,
    _ratio_text,
    _strip_caveats,
    key_tokens,
    normalize_description,
    normalize_unit,
    similarity,
)

from app.services.plans_reader_score import (  # noqa: E402, F401
    DEFAULT_BACKUP_DIR,
    _ACCESSORY_SUBGROUP,
    _BLANK_SHEETS,
    _boq_from,
    _find_header_row,
    _num,
    find_backup_boq,
    parse_estimator_boq,
    rows_from_spec,
)

# Data shapes ------------------------------------------------------------------

@dataclass
class MatchedPair:
    ai: BoqRow
    est: BoqRow
    score: float
    abs_error: float
    pct_error: float | None
    unit_mismatch: bool
    group_mismatch: bool
    exact: bool


@dataclass
class MatchReport:
    pairs: list[MatchedPair]
    unmatched_ai: list[BoqRow]
    unmatched_est: list[BoqRow]
    ai_total: int
    est_total: int
    precision: float
    recall: float
    mape: float | None
    mape_rows: int
    exact_matches: int
    unit_mismatches: int
    group_mismatches: int
    per_group: dict[str, dict[str, Any]] = field(default_factory=dict)
    ignored_accessories: dict[str, int] = field(default_factory=dict)
    threshold: float = MATCH_THRESHOLD



# Estimator workbook -----------------------------------------------------------

# AI spec ----------------------------------------------------------------------

# Matching ---------------------------------------------------------------------

def _area_map(ai_rows: list[BoqRow], est_rows: list[BoqRow]) -> dict[str, str]:
    """AI area name -> estimator area name, or '' when areas are not comparable
    (either side has a single area, or no estimator area is close enough)."""
    ai_areas = sorted({r.area for r in ai_rows})
    est_areas = sorted({r.area for r in est_rows})
    if len(ai_areas) <= 1 or len(est_areas) <= 1:
        return {a: "" for a in ai_areas}
    out: dict[str, str] = {}
    for a in ai_areas:
        na = normalize_description(a)
        best, best_s = "", 0.0
        for e in est_areas:
            ne = normalize_description(e)
            s = SequenceMatcher(None, na, ne).ratio()
            if ne and (ne in na or na in ne):
                s = max(s, 0.9)
            if s > best_s:
                best, best_s = e, s
        out[a] = best if best_s >= 0.5 else ""
    return out


def _pct(ai_q: float, est_q: float) -> float | None:
    if est_q == 0:
        return None
    return abs(ai_q - est_q) / abs(est_q) * 100.0


def match_rows(
    ai_rows: list[BoqRow],
    est_rows: list[BoqRow],
    *,
    ignore_accessories: bool = True,
    threshold: float = MATCH_THRESHOLD,
) -> MatchReport:
    """Greedy one-to-one matching: first within the same area, section and
    group, then across groups within the same area. Each pass takes the best
    remaining pair at or above `threshold`."""
    ignored = {"ai": 0, "est": 0}
    if ignore_accessories:
        ignored = {
            "ai": sum(r.is_accessory for r in ai_rows),
            "est": sum(r.is_accessory for r in est_rows),
        }
        ai_rows = [r for r in ai_rows if not r.is_accessory]
        est_rows = [r for r in est_rows if not r.is_accessory]
    amap = _area_map(ai_rows, est_rows)
    est_multi = len({r.area for r in est_rows}) > 1 and any(amap.values())

    def ai_area(r: BoqRow) -> str:
        return amap.get(r.area, "")

    def est_area(r: BoqRow) -> str:
        return r.area if est_multi else ""

    scores: dict[tuple[int, int], float] = {}
    for i, a in enumerate(ai_rows):
        for j, e in enumerate(est_rows):
            s = similarity(a, e)
            if s >= threshold:
                scores[(i, j)] = s

    # Pass 1: same area, section and group. Pass 2: same area (rows of an AI
    # area that maps to no estimator area may match anywhere). Areas are only
    # compared when both sides have several and the names map.
    passes = (
        lambda a, e: ai_area(a) == est_area(e) and a.section == e.section and a.group == e.group,
        lambda a, e: ai_area(a) == est_area(e) or not ai_area(a),
    )
    used_ai: set[int] = set()
    used_est: set[int] = set()
    chosen: list[tuple[int, int, float]] = []
    for ok in passes:
        cands = sorted(
            ((s, i, j) for (i, j), s in scores.items()
             if i not in used_ai and j not in used_est and ok(ai_rows[i], est_rows[j])),
            key=lambda t: (-t[0], t[1], t[2]),
        )
        for s, i, j in cands:
            if i in used_ai or j in used_est:
                continue
            used_ai.add(i)
            used_est.add(j)
            chosen.append((i, j, s))

    pairs: list[MatchedPair] = []
    for i, j, s in sorted(chosen, key=lambda t: t[1]):
        a, e = ai_rows[i], est_rows[j]
        unit_mismatch = normalize_unit(a.unit) != normalize_unit(e.unit)
        pct = None if unit_mismatch else _pct(a.quantity, e.quantity)
        exact = (not unit_mismatch) and (
            abs(a.quantity - e.quantity) <= EXACT_TOLERANCE * abs(e.quantity)
            if e.quantity else a.quantity == 0
        )
        pairs.append(
            MatchedPair(
                ai=a,
                est=e,
                score=round(s, 4),
                abs_error=round(a.quantity - e.quantity, 4),
                pct_error=None if pct is None else round(pct, 2),
                unit_mismatch=unit_mismatch,
                group_mismatch=(a.section, a.group) != (e.section, e.group),
                exact=exact,
            )
        )

    mape_vals = [p.pct_error for p in pairs if p.pct_error is not None and p.est.quantity > 0]
    per_group: dict[str, dict[str, Any]] = {}
    est_by_group = Counter(r.group_key for r in est_rows)
    ai_by_group = Counter(r.group_key for r in ai_rows)
    est_matched = Counter(p.est.group_key for p in pairs)
    ai_matched = Counter(p.ai.group_key for p in pairs)
    for g in sorted(set(est_by_group) | set(ai_by_group)):
        per_group[g] = {
            "est_rows": est_by_group[g],
            "est_matched": est_matched[g],
            "recall": round(est_matched[g] / est_by_group[g], 4) if est_by_group[g] else None,
            "ai_rows": ai_by_group[g],
            "ai_matched": ai_matched[g],
            "precision": round(ai_matched[g] / ai_by_group[g], 4) if ai_by_group[g] else None,
        }

    return MatchReport(
        pairs=pairs,
        unmatched_ai=[r for i, r in enumerate(ai_rows) if i not in used_ai],
        unmatched_est=[r for j, r in enumerate(est_rows) if j not in used_est],
        ai_total=len(ai_rows),
        est_total=len(est_rows),
        precision=round(len(pairs) / len(ai_rows), 4) if ai_rows else 0.0,
        recall=round(len(pairs) / len(est_rows), 4) if est_rows else 0.0,
        mape=round(sum(mape_vals) / len(mape_vals), 2) if mape_vals else None,
        mape_rows=len(mape_vals),
        exact_matches=sum(p.exact for p in pairs),
        unit_mismatches=sum(p.unit_mismatch for p in pairs),
        group_mismatches=sum(p.group_mismatch for p in pairs),
        per_group=per_group,
        ignored_accessories=ignored,
        threshold=threshold,
    )


# Reports ----------------------------------------------------------------------

def report_json(report: MatchReport) -> dict[str, Any]:
    return {
        "threshold": report.threshold,
        "totals": {
            "ai_rows": report.ai_total,
            "est_rows": report.est_total,
            "matched": len(report.pairs),
            "precision": report.precision,
            "recall": report.recall,
            "mape": report.mape,
            "mape_rows": report.mape_rows,
            "exact_matches": report.exact_matches,
            "unit_mismatches": report.unit_mismatches,
            "group_mismatches": report.group_mismatches,
            "ignored_accessories": report.ignored_accessories,
        },
        "per_group": report.per_group,
        "pairs": [
            {
                "ai": asdict(p.ai),
                "est": asdict(p.est),
                "score": p.score,
                "abs_error": p.abs_error,
                "pct_error": p.pct_error,
                "unit_mismatch": p.unit_mismatch,
                "group_mismatch": p.group_mismatch,
                "exact": p.exact,
            }
            for p in report.pairs
        ],
        "unmatched_ai": [asdict(r) for r in report.unmatched_ai],
        "unmatched_est": [asdict(r) for r in report.unmatched_est],
    }


def _fmt_q(q: float) -> str:
    return f"{q:,.2f}".rstrip("0").rstrip(".")


def _pct_s(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _md_cell(s: str, limit: int = 70) -> str:
    s = s.replace("|", "/")
    return s if len(s) <= limit else s[: limit - 3] + "..."


def report_markdown(report: MatchReport) -> str:
    out: list[str] = ["# Plans Reader golden comparison", ""]
    out += [
        "| Metric | Value |",
        "|---|---|",
        f"| AI rows | {report.ai_total} |",
        f"| Estimator rows | {report.est_total} |",
        f"| Matched (score >= {report.threshold}) | {len(report.pairs)} |",
        f"| Precision | {_pct_s(report.precision)} |",
        f"| Recall | {_pct_s(report.recall)} |",
        "| MAPE (matched, same unit, est qty > 0) | "
        + ("n/a" if report.mape is None else f"{report.mape:.1f}% over {report.mape_rows} rows")
        + " |",
        f"| Exact quantity (within 2%) | {report.exact_matches} |",
        f"| Unit mismatches | {report.unit_mismatches} |",
        f"| Matched across groups | {report.group_mismatches} |",
        f"| Accessory rows ignored (AI / est) | {report.ignored_accessories.get('ai', 0)}"
        f" / {report.ignored_accessories.get('est', 0)} |",
        "",
        "## Per group",
        "",
        "| Group | Est rows | Recall | AI rows | Precision |",
        "|---|---|---|---|---|",
    ]
    for g, v in report.per_group.items():
        out.append(
            f"| {g} | {v['est_rows']} | {_pct_s(v['recall'])} | {v['ai_rows']} |"
            f" {_pct_s(v['precision'])} |"
        )
    out += ["", "## Matched rows", "", "| Estimator | AI | Est qty | AI qty | Err % | Flags |",
            "|---|---|---|---|---|---|"]
    for p in report.pairs:
        flags = []
        if p.unit_mismatch:
            flags.append(f"UNIT {p.est.unit} vs {p.ai.unit}")
        if p.group_mismatch:
            flags.append(f"GROUP {p.ai.group_key}")
        if p.exact:
            flags.append("exact")
        err = "n/a" if p.pct_error is None else f"{p.pct_error:.1f}"
        out.append(
            f"| {_md_cell(p.est.description)} | {_md_cell(p.ai.description)} |"
            f" {_fmt_q(p.est.quantity)} {p.est.unit} | {_fmt_q(p.ai.quantity)} {p.ai.unit} |"
            f" {err} | {', '.join(flags)} |"
        )
    out += ["", f"## Missed scope (unmatched estimator rows: {len(report.unmatched_est)})", ""]
    out += [
        f"- [{r.area} / {r.group_key}] {r.description} = {_fmt_q(r.quantity)} {r.unit}"
        for r in report.unmatched_est
    ] or ["- none"]
    out += ["", f"## Extra AI rows (hallucinated or over-split: {len(report.unmatched_ai)})", ""]
    out += [
        f"- [{r.area} / {r.group_key}] {r.description} = {_fmt_q(r.quantity)} {r.unit}"
        for r in report.unmatched_ai
    ] or ["- none"]
    return "\n".join(out) + "\n"


def structure_markdown(rows: list[BoqRow]) -> str:
    """Item counts per area and group of a parsed estimator BOQ."""
    out = ["| Area | Group | Items | Accessories |", "|---|---|---|---|"]
    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        counts[(r.area, r.group_key)][1 if r.is_accessory else 0] += 1
    for (area, g), (n, acc) in counts.items():
        out.append(f"| {area} | {g} | {n} | {acc} |")
    return "\n".join(out) + "\n"


# Backup lookup ----------------------------------------------------------------

# CLI --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score an AI BOQ spec against an estimator BOQ.")
    ap.add_argument("--spec", type=Path, help="BOQ_SPEC JSON (or a main agent output with 'boq')")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--estimator", type=Path, help="the estimator's BOQ - COUNTS.xlsx")
    src.add_argument("--from-backup", metavar="PROJECT_ID_PREFIX",
                     help="locate the estimator BOQ in the production backup")
    ap.add_argument("--backup-dir", type=Path,
                    default=Path(os.environ.get("BDR_BACKUP_DIR", DEFAULT_BACKUP_DIR)))
    ap.add_argument("--json", type=Path, help="also write the JSON report here")
    ap.add_argument("--include-accessories", action="store_true",
                    help="score generated accessory rows too")
    ap.add_argument("--threshold", type=float, default=MATCH_THRESHOLD)
    args = ap.parse_args(argv)

    est_path = args.estimator or find_backup_boq(args.from_backup, args.backup_dir)
    est_rows = parse_estimator_boq(est_path.read_bytes())
    if args.spec is None:
        print(f"# Estimator BOQ structure: {est_path}\n")
        print(structure_markdown(est_rows))
        return 0
    spec = json.loads(args.spec.read_text())
    report = match_rows(
        rows_from_spec(spec), est_rows,
        ignore_accessories=not args.include_accessories, threshold=args.threshold,
    )
    print(report_markdown(report))
    if args.json:
        args.json.write_text(json.dumps(report_json(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
