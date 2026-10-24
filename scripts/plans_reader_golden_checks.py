"""WS4 offline golden checks over the production backup ($0, no model).

Three questions the gates need answered from the estimators' own work
(docs/AI_ESTIMATING_BUILD_SPEC.md WS4 steps and 5.7):

1. Floors vs estimator totals. For every non-holdout golden project with a
   text-layer electrical drawing set, the census floors of
   `plans_reader_floors` (summed over the plan sheets) against the
   estimator BOQ totals of the same family. A family is trustworthy when
   `estimator_total >= floor - tolerance` in >= 95% of its (project, family)
   pairs; otherwise it is warn-only (`plans_reader_gates.BLOCKING_FAMILIES`
   and `WARN_FAMILIES`). A pair where the estimator carries no line of the
   family counts as estimator 0, so it fails whenever floor - tolerance > 0
   (the spec's rule over every pair; the share without those pairs is
   reported beside it, never used for the verdict).
2. The demolition two-line style share: of the golden estimator BOQs with
   a demolition section, how many carry exactly one lighting removal row
   and one power device removal row. >= 60% sets the dev value of
   `plans_reader_demo_two_line_style` to true.
3. Linear luminaire FT vs EA share per class (undercabinet, strip, tape,
   linear, cove): the gate `linear_in_ea` blocks an EA line of a class only
   when estimators price it in FT >= 80% of the time
   (`plans_reader_gates.LINEAR_LUMINAIRE_FT_SHARE`).

Holdout projects are excluded through `eval_manifest.excluded_projects`.
Page facts are cached under EVAL_ROOT/cache/pagefacts/<sha256>.json so a
re-run costs seconds. The report (counts and ratios only, never dollars)
is written to EVAL_ROOT/reports/ws4_golden_checks.md and .json.

Usage: uv run python scripts/plans_reader_golden_checks.py [--limit N] [--no-floors]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import eval_manifest as M  # noqa: E402
from app.services import eval_reference as R  # noqa: E402
from app.services import plans_reader_floors as F  # noqa: E402
from app.services import plans_reader_score as S  # noqa: E402
from app.services.electrical_items import parse_item  # noqa: E402
from app.services.pagefacts import extract  # noqa: E402
from app.services.pagefacts.contracts import DocFacts  # noqa: E402

COMPANY_TZ = ZoneInfo("America/Los_Angeles")
PASS_SHARE = 0.95
TWO_LINE_SHARE = 0.60
LINEAR_BLOCK_SHARE = 0.80
# Build choice (not in the spec): a family is ADMITTED as blocking only
# with at least this many (project, family) pairs; fewer passing pairs read
# `unmeasured` (warn-only). A family under 95% is warn_only at any count.
# A linear class needs MIN_LINEAR_LINES priced lines before its FT share
# can block.
MIN_JUDGED_PAIRS = 10
MIN_LINEAR_LINES = 5
DRAWING_CATEGORIES = ("electrical_drawing",)
NEW_DEVICE_KINDS = frozenset(
    {"fixture", "pole_light", "receptacle", "switch", "dimmer", "occ_sensor", "lighting_control_other",
     "equipment_connection", "thermostat", "ev_charger"}
)
DEMO_EA_KINDS = frozenset({"demo_lighting", "demo_power", "demo_other"})
_RELOCATE_RE = re.compile(r"(?i)\brelocat\w*\b|\bre-locat\w*\b")
_MOD_RES = {
    "G": re.compile(r"(?i)\bGFCI\b|\bGFI\b|\bGROUND\s+FAULT\b"),
    "WP": re.compile(r"(?i)\bWP\b|\bWEATHER\s*PROOF\b|\bWEATHERPROOF\b"),
    "IG": re.compile(r"(?i)\bIG\b|\bISOLATED\s+GROUND\b"),
    "TR": re.compile(r"(?i)\bTR\b|\bTAMPER\s*RESISTANT\b"),
    "USB": re.compile(r"(?i)\bUSB\b"),
    "EM": re.compile(r"(?i)\bEM\b|\bEMERGENCY\b|\bLIFE\s+SAFETY\b"),
}


def now_pacific() -> str:
    return datetime.now(COMPANY_TZ).strftime("%Y-%m-%d %H:%M %Z")


# Page facts (cached) ----------------------------------------------------------------


def cache_dir(root: Path) -> Path:
    d = root / "cache" / "pagefacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def doc_facts(pdf_path: Path, root: Path, *, quiet: bool) -> DocFacts | None:
    data = pdf_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    cached = cache_dir(root) / f"{sha}.json"
    if cached.exists():
        try:
            return DocFacts.from_dict(json.loads(cached.read_text()))
        except Exception:  # noqa: BLE001 - a stale cache entry is rebuilt
            pass
    t0 = time.monotonic()
    try:
        facts = extract.extract_document(data, label="electrical_drawings", name=pdf_path.name)
    except Exception as exc:  # noqa: BLE001 - a broken PDF is reported, not fatal
        if not quiet:
            print(f"  facts failed for {pdf_path.name}: {exc}")
        return None
    cached.write_text(json.dumps(facts.to_dict()))
    if not quiet:
        print(f"  facts {pdf_path.name[:60]}: {facts.page_count} pages in {time.monotonic() - t0:.1f}s")
    return facts


# Estimator totals ----------------------------------------------------------------------


def estimator_rows(data: R.BackupData, project_id: str) -> list[Any] | None:
    boq_file = R.pick_boq_file(data, project_id)
    if boq_file is None:
        return None
    try:
        return S.parse_estimator_boq(data.file_path(boq_file).read_bytes())
    except Exception:  # noqa: BLE001 - an unreadable workbook is reported as no_boq
        return None


def classify(rows: list[Any]) -> list[tuple[Any, Any]]:
    out = []
    for r in rows:
        if r.is_accessory:
            continue
        key = S.classify_line(r.description, r.section, r.group, r.unit, subgroup=r.subgroup)
        out.append((r, key))
    return out


def family_totals(rows: list[Any]) -> dict[str, float]:
    """{family key: EA total} the estimator carries (None when no line of
    the family exists is expressed by absence)."""
    totals: dict[str, float] = defaultdict(float)
    present: set[str] = set()
    for r, key in classify(rows):
        unit = str(r.unit or "").upper()
        qty = float(r.quantity or 0.0)
        if key.kind in DEMO_EA_KINDS and unit == "EA":
            totals["demo_x"] += qty
            present.add("demo_x")
        if _RELOCATE_RE.search(r.description) and unit == "EA":
            totals["relocate_r"] += qty
            present.add("relocate_r")
        if key.kind in NEW_DEVICE_KINDS and unit == "EA" and str(r.section).upper() != "DEMOLITION":
            totals["circuit"] += qty
            present.add("circuit")
            identity = parse_item(r.description, unit)
            tag = identity.attrs.get("tag")
            if tag and identity.item_class in ("luminaire", "luminaire_linear", "lighting_track"):
                totals[f"fixture_mark:{str(tag).upper()}"] += qty
                present.add(f"fixture_mark:{str(tag).upper()}")
            for mod, pat in _MOD_RES.items():
                if pat.search(r.description):
                    totals[f"device_mod:{mod}"] += qty
                    present.add(f"device_mod:{mod}")
    return {k: totals[k] for k in present}


def demolition_style(rows: list[Any]) -> dict[str, Any] | None:
    kinds = Counter()
    for r, key in classify(rows):
        if key.kind in S.DEMO_KINDS or str(r.section).upper() == "DEMOLITION":
            kinds[key.kind] += 1
    if not kinds:
        return None
    return {
        "demo_lines": sum(kinds.values()),
        "lighting_rows": kinds.get("demo_lighting", 0),
        "power_rows": kinds.get("demo_power", 0),
        "two_line_style": kinds.get("demo_lighting", 0) == 1 and kinds.get("demo_power", 0) == 1,
        "kinds": dict(kinds),
    }


def linear_luminaire_units(rows: list[Any]) -> list[tuple[str, str]]:
    out = []
    for r in rows:
        if r.is_accessory or str(r.section).upper() == "DEMOLITION":
            continue
        identity = parse_item(r.description, r.unit)
        if identity.item_class == "luminaire_linear":
            kind = identity.family.split(":", 1)[1] if ":" in identity.family else identity.family
            out.append((kind, str(r.unit or "").upper()))
    return out


# Floors per project ----------------------------------------------------------------------


def project_floors(data: R.BackupData, project_id: str, root: Path, *, quiet: bool) -> dict[str, Any]:
    """{family key: {floor, sheets, unknown_sheets}} summed over the plan
    sheets of the project's electrical drawing PDFs."""
    files = [f for f in data.by_project("project_files", project_id) if f.get("category") in DRAWING_CATEGORIES and str(f.get("filename", "")).lower().endswith(".pdf")]
    sums: dict[str, dict[str, Any]] = defaultdict(lambda: {"floor": 0, "sheets": 0, "positions": 0})
    plan_pages = 0
    text_pages = 0
    docs = 0
    seen_sha: set[str] = set()
    for f in files:
        path = data.file_path(f)
        if not path.exists():
            continue
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if sha in seen_sha:
            continue  # the same set uploaded twice counts once
        seen_sha.add(sha)
        facts = doc_facts(path, root, quiet=quiet)
        if facts is None:
            continue
        docs += 1
        pages = {(str(f.get("id")), p.page): p.to_dict() for p in facts.pages}
        for p in facts.pages:
            roles = set(p.roles) | {p.page_role}
            if roles & {"plan", "site"}:
                plan_pages += 1
                if F.text_usable(p.to_dict()):
                    text_pages += 1
        for fl in F.compute_floors(pages, doc_numbers={str(f.get("id")): 1}):
            if not fl.known:
                continue
            entry = sums[fl.key]
            entry["floor"] += int(fl.floor or 0)
            entry["sheets"] += 1
    return {"docs": docs, "plan_pages": plan_pages, "text_plan_pages": text_pages, "floors": dict(sums)}


def tolerance_for(key: str, floor: int) -> int:
    family = key.split(":", 1)[0]
    rel = F.DEFAULT_TOL_STRONG if family in F.STRONG_FAMILIES else F.DEFAULT_TOL_WEAK
    return max(1, math.ceil(rel * floor - 1e-9))


def family_verdict(family: str, fam_pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts, both shares and the verdict of one family. Every pair is
    judged (a no-line pair is estimator 0). `pass_share` (the verdict's
    input) is over all pairs; `pass_share_with_line` leaves the no-line
    pairs out (reported only). Verdict: under PASS_SHARE -> warn_only at any
    count; at or over it with fewer than MIN_JUDGED_PAIRS pairs ->
    unmeasured (warn-only); else blocking for a strong family, warn for a
    weak one."""
    n = len(fam_pairs)
    passed = sum(1 for p in fam_pairs if p["status"] == "pass")
    with_line = [p for p in fam_pairs if not p.get("no_estimator_line")]
    passed_with_line = sum(1 for p in with_line if p["status"] == "pass")
    share = round(passed / n, 4) if n else None
    share_with_line = round(passed_with_line / len(with_line), 4) if with_line else None
    if share is None:
        verdict = "unmeasured"
    elif share < PASS_SHARE:
        verdict = "warn_only"
    elif n < MIN_JUDGED_PAIRS:
        verdict = "unmeasured"
    else:
        verdict = "blocking" if family in F.STRONG_FAMILIES else "warn"
    return {
        "pairs": n,
        "judged": n,
        "no_estimator_line": n - len(with_line),
        "no_estimator_line_failed": sum(1 for p in fam_pairs if p.get("no_estimator_line") and p["status"] == "fail"),
        "passed": passed,
        "pass_share": share,
        "pairs_with_line": len(with_line),
        "passed_with_line": passed_with_line,
        "pass_share_with_line": share_with_line,
        "verdict": verdict,
    }


# Main ------------------------------------------------------------------------------------


def run(*, root: Path, backup: Path, limit: int | None, floors: bool, quiet: bool) -> dict[str, Any]:
    data = R.BackupData(backup)
    excluded = M.excluded_projects("thresholds")
    projects = [p for p in M.GOLDEN_IDS if p not in excluded]
    if limit:
        projects = projects[:limit]
    per_project: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    styles: list[dict[str, Any]] = []
    linear: list[tuple[str, str]] = []
    for i, pid in enumerate(projects, start=1):
        rows = estimator_rows(data, pid)
        entry: dict[str, Any] = {"project": pid[:8], "tier": M.tier_of(pid), "boq": rows is not None}
        if rows is None:
            per_project.append(entry)
            continue
        totals = family_totals(rows)
        style = demolition_style(rows)
        if style is not None:
            styles.append({"project": pid[:8], **style})
        linear.extend(linear_luminaire_units(rows))
        entry["estimator_families"] = {k: v for k, v in totals.items()}
        entry["demolition_style"] = style
        if floors:
            if not quiet:
                print(f"[{i}/{len(projects)}] {pid[:8]} floors")
            pf = project_floors(data, pid, root, quiet=quiet)
            entry.update({k: v for k, v in pf.items() if k != "floors"})
            entry["floors"] = pf["floors"]
            for key, fl in pf["floors"].items():
                if fl["floor"] <= 0:
                    continue
                est = totals.get(key)
                tol = tolerance_for(key, fl["floor"])
                no_line = est is None
                value = 0.0 if no_line else float(est)
                status = "pass" if value >= fl["floor"] - tol else "fail"
                pairs.append({"project": pid[:8], "family": key.split(":", 1)[0], "key": key, "floor": fl["floor"], "sheets": fl["sheets"], "estimator": value, "no_estimator_line": no_line, "tolerance": tol, "status": status})
        per_project.append(entry)

    # 1. floors vs estimator totals, per family
    family_report: dict[str, Any] = {}
    for fam in F.FAMILIES:
        fam_pairs = [p for p in pairs if p["family"] == fam]
        verdict_info = family_verdict(fam, fam_pairs)
        family_report[fam] = {
            **verdict_info,
            "failures": [p for p in fam_pairs if p["status"] == "fail"][:12],
        }
    # 2. demolition two-line style
    with_demo = len(styles)
    two_line = sum(1 for s in styles if s["two_line_style"])
    style_share = round(two_line / with_demo, 4) if with_demo else None
    # 3. linear luminaires
    linear_report: dict[str, Any] = {}
    by_kind: dict[str, Counter] = defaultdict(Counter)
    for kind, unit in linear:
        by_kind[kind][unit] += 1
    for kind, units in sorted(by_kind.items()):
        ft = units.get("FT", 0)
        ea = units.get("EA", 0)
        share = round(ft / (ft + ea), 4) if (ft + ea) else None
        linear_report[kind] = {
            "ft": ft, "ea": ea, "other": sum(v for u, v in units.items() if u not in ("FT", "EA")), "n": ft + ea,
            "ft_share": share, "blocks_in_ea": bool(share is not None and (ft + ea) >= MIN_LINEAR_LINES and share >= LINEAR_BLOCK_SHARE),
        }
    return {
        "generated_at": now_pacific(),
        "backup_dir": str(backup),
        "projects": len(projects),
        "projects_with_boq": sum(1 for e in per_project if e.get("boq")),
        "holdout_excluded": sorted(p[:8] for p in M.HOLDOUT_IDS),
        "floors": {"pairs": len(pairs), "families": family_report, "pair_rows": pairs},
        "demolition_two_line_style": {"boqs_with_demolition": with_demo, "two_line": two_line, "share": style_share, "dev_value": bool(style_share is not None and style_share >= TWO_LINE_SHARE), "threshold": TWO_LINE_SHARE, "cases": styles},
        "linear_luminaires": {"block_share": LINEAR_BLOCK_SHARE, "classes": linear_report},
        "per_project": per_project,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# WS4 offline golden checks",
        "",
        f"Generated {report['generated_at']} over {report['projects']} non-holdout golden projects ({report['projects_with_boq']} with a BOQ workbook); holdout excluded: {', '.join(report['holdout_excluded'])}. Counts and ratios only.",
        "",
        "## 1. Census floors vs estimator totals",
        "",
        "A family blocks when estimator_total >= floor - tolerance in >= 95% of its (project, family) pairs; strong families use the 5% tolerance, weak ones 10% (minimum 1). A pair where the estimator carries no line of the family counts as estimator 0 (it fails when floor - tolerance > 0). The share without those pairs is shown beside it and is not used for the verdict.",
        "",
        f"A family under 95% is warn-only at any count; a passing family needs at least {MIN_JUDGED_PAIRS} pairs to block (else unmeasured, warn-only; a build choice, not in the spec). Floors are summed over the plan sheets of the electrical drawing PDFs with a usable text layer (quality full; vector_text, sparse and scanned pages have no floor; duplicate uploads counted once; enlarged views on a sheet deduped against its overall view).",
        "",
        "| family | pairs | no estimator line (failed) | passed | pass share (all pairs) | pass share (pairs with a line) | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for fam, r in report["floors"]["families"].items():
        share = "n/a" if r["pass_share"] is None else f"{r['passed']} of {r['pairs']} = {r['pass_share']:.1%}"
        share_wl = "n/a" if r["pass_share_with_line"] is None else f"{r['passed_with_line']} of {r['pairs_with_line']} = {r['pass_share_with_line']:.1%}"
        lines.append(f"| {fam} | {r['pairs']} | {r['no_estimator_line']} ({r['no_estimator_line_failed']}) | {r['passed']} | {share} | {share_wl} | {r['verdict']} |")
    lines += ["", "Failures (first 12 per family):", ""]
    for fam, r in report["floors"]["families"].items():
        for p in r["failures"]:
            no_line = " (no estimator line)" if p.get("no_estimator_line") else ""
            lines.append(f"- {fam} {p['project']} {p['key']}: floor {p['floor']} over {p['sheets']} sheet(s), estimator {p['estimator']:g}{no_line}, tolerance {p['tolerance']}")
    d = report["demolition_two_line_style"]
    share = "n/a" if d["share"] is None else f"{d['share']:.1%}"
    lines += [
        "",
        "## 2. Demolition two-line style",
        "",
        f"BOQs with demolition: {d['boqs_with_demolition']}; exactly one lighting removal row and one power device removal row: {d['two_line']}; share {share}; threshold {d['threshold']:.0%}.",
        "",
        f"Dev value of PLANS_READER_DEMO_TWO_LINE_STYLE: **{'true' if d['dev_value'] else 'false'}**.",
        "",
        "## 3. Linear luminaires: FT vs EA per class",
        "",
        "| class | FT lines | EA lines | other | n | FT share | blocks in EA |",
        "|---|---|---|---|---|---|---|",
    ]
    for kind, r in report["linear_luminaires"]["classes"].items():
        share = "n/a" if r["ft_share"] is None else f"{r['ft_share']:.1%}"
        lines.append(f"| {kind} | {r['ft']} | {r['ea']} | {r['other']} | {r['n']} | {share} | {'yes' if r['blocks_in_ea'] else 'no'} |")
    lines += ["", f"A class blocks an EA line only at >= {report['linear_luminaires']['block_share']:.0%} FT over at least {MIN_LINEAR_LINES} lines (spec 5.1); the others warn. plans_reader_gates.LINEAR_LUMINAIRE_FT_SHARE carries these shares.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--eval-root", default=None)
    ap.add_argument("--backup-dir", default=str(R.DEFAULT_BACKUP_DIR))
    ap.add_argument("--limit", type=int, default=None, help="first N projects only (a dry run)")
    ap.add_argument("--no-floors", action="store_true", help="skip the page facts extraction (checks 2 and 3 only)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.eval_root) if args.eval_root else M.eval_root()
    report = run(root=root, backup=Path(args.backup_dir), limit=args.limit, floors=not args.no_floors, quiet=args.quiet)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "ws4_golden_checks.json").write_text(json.dumps(report, indent=1, default=str))
    md = markdown(report)
    (reports / "ws4_golden_checks.md").write_text(md)
    print(md)
    print(f"written: {reports / 'ws4_golden_checks.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
