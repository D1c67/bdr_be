"""WS3 measurement evaluation over the production backup ($0, no model).

Usage (from bdr_be):

    uv run python scripts/plans_reader_measure_eval.py --survey \\
        [--eval-root EVAL_ROOT] [--backup BACKUP_DIR] [--projects 7dfc64e6,2bf3b21e] \\
        [--limit N] [--max-pages 120] [--no-cache] [--json out.json]

--survey (spec 6 WS3 step B2) classifies every golden pool project (not core,
not holdout; 7154 included; `eval_manifest.population("tagged_training")`
minus `eval_manifest.excluded_projects("calibration")`) by its drawing
sets: `tagged`, `untagged_text`, `vector_text`, `scanned` (or `no_plans`),
and records the tag convention per plan sheet (`tag_per_device`,
`stacked_label`, `home_run_arrow`, `untagged`). It writes
`EVAL_ROOT/reports/measure_survey.md` and the tagged training split of 8.7
to `EVAL_ROOT/manifest/ws3_split.json`. Page facts come from WS1
`pagefacts.extract.extract_document` on the backup PDFs and are cached as
JSON under `EVAL_ROOT/cache/pagefacts_survey/<sha256>.json`.

    uv run python scripts/plans_reader_measure_eval.py --calibrate \
        [--split EVAL_ROOT/manifest/ws3_split.json] [--projects ...] [--no-cache]

--calibrate (spec 6 WS3 step B9, Q17) runs `plans_reader_measure.build_measure`
once per project of the tagged training split (never the holdout; the
split file was written before any calibration) with the code-default
conventions, then evaluates the reduced grid in closed form (route_factor
0.90 to 1.30 in steps of 0.05, drop_ft_per_device 0, 4 or 8, panel_rise_ft
fixed 0: measured terms scale with the factor, allowances do not, drops
add per device) against the estimator's branch conduit (1/2" plus 3/4"
EMT, pre-wastage) and branch wire (#12 plus #10) from the estimate
workbook (one-book rule). It reports the grid, the leave-one-project-out
error, the median home-run length per circuit (the candidate
`HOMERUN_MEDIAN_FT`), and writes `EVAL_ROOT/reports/measure_calibration.md`
plus JSON. It never changes a code default: the owner signs off. The
golden shares are labeled provisional only while n < 15 (spec 6 WS3); at
n >= 15 a miss is reported as FAILED (owner question per spec 0.5).
Collector and home-run FT are reported separately per project; the
estimate workbooks carry one branch conduit total, so a separate collector
or home-run ERROR is not measurable on this corpus and the report says so.

    uv run python scripts/plans_reader_measure_eval.py --never-empty \
        [--split EVAL_ROOT/manifest/ws3_split.json] [--projects ...]

--never-empty (spec 6 WS3 golden "untagged projects never empty") runs
`build_measure` with WS1 facts only (no model category outputs, zero model
calls) on every `untagged_text` and `vector_text` project of the survey
split and checks that each yields non-empty branch rows (a WIRING M row
with FT > 0). Facts alone carry no new-device count (the reader's count
rows feed the density tier in a real run), so each project is built a
second time with the estimator's device count from the active bands file
standing in for the reader's count rows (counts only). It writes
`EVAL_ROOT/reports/measure_never_empty.md` plus JSON.

Counts and ratios only; no dollar figures anywhere in the outputs.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import estimate_parse  # noqa: E402
from app.services import eval_manifest  # noqa: E402
from app.services import plans_reader_circuits as circuits  # noqa: E402
from app.services import plans_reader_measure as measure  # noqa: E402
from app.services import plans_reader_routes as routes_mod  # noqa: E402
from app.services.eval_reference import DEFAULT_BACKUP_DIR, BackupData, now_pacific_iso  # noqa: E402
from app.services.pagefacts import extract  # noqa: E402
from app.services.pagefacts.contracts import DocFacts  # noqa: E402
from app.services.plans_reader_render import parse_conduit  # noqa: E402

SPLIT_SCHEMA = "bdr.ws3_split/1"
EXCLUSION_PURPOSE = "calibration"
DRAWING_CATEGORIES: tuple[str, ...] = ("drawing", "electrical_drawing", "addendum")
LABEL_BY_CATEGORY: dict[str, str | None] = {
    "electrical_drawing": "electrical_drawings",
    "drawing": None,
    "addendum": None,
}
BUDGETS = {"page_budget_s": 4.0, "doc_budget_s": 240.0}
GOLDEN_MIN_N = 15
NEVER_EMPTY_CLASSES: tuple[str, ...] = ("untagged_text", "vector_text")


ROUTE_FACTORS: tuple[float, ...] = tuple(round(0.90 + 0.05 * i, 2) for i in range(9))
DROPS: tuple[float, ...] = (0.0, 4.0, 8.0)
PANEL_RISE = 0.0
BRANCH_CONDUIT_SIZES = ("1/2", "3/4")
BRANCH_WIRE_RE = re.compile(r"^\s*#\s*(12|10)\b")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WS3 measurement evaluation (survey, calibrate).")
    p.add_argument("--survey", action="store_true", help="B2 survey over the golden pool")
    p.add_argument("--calibrate", action="store_true", help="B9 grid on the tagged training split")
    p.add_argument("--never-empty", action="store_true", help="never-empty check over the untagged and vector_text projects")
    p.add_argument("--split", type=Path, default=None, help="ws3_split.json (default EVAL_ROOT/manifest)")
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--backup", type=Path, default=DEFAULT_BACKUP_DIR)
    p.add_argument("--projects", default=None, help="comma list of project ids or prefixes")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-pages", type=int, default=120, help="skip PDFs with more pages")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", type=Path, default=None)
    return p


# Facts per document --------------------------------------------------------------------


def facts_for_file(
    data: BackupData, row: dict[str, Any], *, cache_dir: Path | None, max_pages: int
) -> tuple[DocFacts | None, str]:
    """(facts, note) for one backup project_files row; facts None when the
    file is skipped (missing, not a PDF, too many pages, unreadable)."""
    path = data.file_path(row)
    if not path.exists() or path.suffix.lower() != ".pdf":
        return None, "missing or not a PDF"
    pdf = path.read_bytes()
    sha = extract.sha256_of(pdf)
    cached = cache_dir / f"{sha}.json" if cache_dir else None
    if cached is not None and cached.exists():
        try:
            return DocFacts.from_dict(json.loads(cached.read_text())), "cache"
        except Exception:  # noqa: BLE001 - a stale cache entry is rebuilt
            pass
    try:
        pages = extract.textlayer.page_count(pdf)
    except Exception as exc:  # noqa: BLE001
        return None, f"unreadable ({type(exc).__name__})"
    if pages > max_pages:
        return None, f"skipped: {pages} pages > {max_pages}"
    label = LABEL_BY_CATEGORY.get(str(row.get("category")), None)
    try:
        facts = extract.extract_document(pdf, label=label, name=row.get("filename"), **BUDGETS)
    except ValueError as exc:
        return None, f"unreadable ({exc})"
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(facts.to_dict(), separators=(",", ":")))
    return facts, "extracted"


# Survey rows ----------------------------------------------------------------------------------


def survey_project(project_id: str, name: str, docs: list[DocFacts], notes: list[str]) -> dict[str, Any]:
    """One survey row: class, plan pages, tags, conventions per sheet."""
    cls = circuits.survey_class(docs)
    sheets: list[dict[str, Any]] = []
    for doc in docs:
        for page in doc.pages:
            is_plan = bool(set(page.roles) & {"plan", "site"}) or page.page_role in ("plan", "site")
            if cls.roles_fallback:
                is_plan = page.page_role in (None, "other") and not page.roles
            if not is_plan:
                continue
            census = circuits.tag_census(page)
            conv = circuits.classify_tag_convention(page)
            sheets.append(
                {
                    "doc": doc.name,
                    "page": page.page,
                    "sheet_number": page.sheet_number,
                    "text_class": page.text_class,
                    "tags": census.tags,
                    "circuits": census.circuits,
                    "convention": conv.convention,
                    "stacked_share": round(conv.stacked_share, 3),
                    "multi_circuit_share": round(conv.multi_circuit_share, 3),
                }
            )
    return {
        "project_id": project_id,
        "prefix": project_id[:8],
        "name": name,
        "class": cls.cls,
        "plan_pages": cls.plan_pages,
        "tagged_pages": cls.tagged_pages,
        "tags": cls.tags,
        "text_classes": cls.text_classes,
        "roles_fallback": cls.roles_fallback,
        "documents": len(docs),
        "notes": list(notes),
        "sheets": sheets,
    }


def build_split(rows: list[dict[str, Any]], excluded: set[str]) -> dict[str, Any]:
    tagged = sorted(r["project_id"] for r in rows if r["class"] == "tagged")
    return {
        "schema": SPLIT_SCHEMA,
        "built_at": now_pacific_iso(),
        "rule": (
            "golden pool projects (not core, not holdout; 7154 included) whose WS3 B2 survey "
            "class is tagged; written before any calibration (spec 8.7)"
        ),
        "exclusion_purpose": EXCLUSION_PURPOSE,
        "excluded": sorted(excluded),
        "candidates": len(rows),
        "classes": {r["project_id"]: r["class"] for r in rows},
        "n_tagged": len(tagged),
        "tagged_training": tagged,
    }


def render_report(rows: list[dict[str, Any]], split: dict[str, Any]) -> str:
    classes = Counter(r["class"] for r in rows)
    conv = Counter(s["convention"] for r in rows for s in r["sheets"])
    text_classes: Counter[str] = Counter()
    for r in rows:
        text_classes.update(r["text_classes"])
    out: list[str] = []
    out.append("# WS3 measurement survey (B2)")
    out.append("")
    out.append(f"Built {split['built_at']} (America/Los_Angeles). Candidates: {len(rows)} golden pool projects ")
    out.append("(not core, not holdout; 7154 included), holdout excluded through ")
    out.append(f"`eval_manifest.excluded_projects(\"{EXCLUSION_PURPOSE}\")` ({len(split['excluded'])} ids). ")
    out.append("Counts and ratios only; no model calls.")
    out.append("")
    out.append("## Classes")
    out.append("")
    out.append("| Class | Projects |")
    out.append("|---|---|")
    for cls in circuits.SURVEY_CLASSES:
        out.append(f"| {cls} | {classes.get(cls, 0)} |")
    out.append("")
    out.append(f"Tagged training split: {split['n_tagged']} projects (`manifest/ws3_split.json`).")
    out.append("")
    out.append("## Tag convention per plan sheet")
    out.append("")
    out.append("| Convention | Sheets |")
    out.append("|---|---|")
    for c in circuits.TAG_CONVENTIONS:
        out.append(f"| {c} | {conv.get(c, 0)} |")
    out.append("")
    out.append("## Text classes of plan pages")
    out.append("")
    out.append("| text_class | Pages |")
    out.append("|---|---|")
    for k, v in sorted(text_classes.items()):
        out.append(f"| {k} | {v} |")
    out.append("")
    out.append("## Projects")
    out.append("")
    out.append("| Project | Name | Class | Plan pages | Tagged pages | Tags | Docs | Conventions | Notes |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (r["class"], r["prefix"])):
        convs = Counter(s["convention"] for s in r["sheets"] if s["convention"] != "untagged")
        conv_text = ", ".join(f"{k} {v}" for k, v in sorted(convs.items())) or "none"
        notes = "; ".join((["plan roles from unroled pages"] if r.get("roles_fallback") else []) + r["notes"])[:120]
        out.append(
            f"| {r['prefix']} | {str(r['name'])[:40]} | {r['class']} | {r['plan_pages']} | "
            f"{r['tagged_pages']} | {r['tags']} | {r['documents']} | {conv_text} | {notes} |"
        )
    out.append("")
    out.append("## Tagged sheets")
    out.append("")
    out.append("| Project | Sheet | Tags | Circuits | Convention | Stacked share | Multi-circuit share |")
    out.append("|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: r["prefix"]):
        for s in r["sheets"]:
            if s["tags"] <= 0:
                continue
            out.append(
                f"| {r['prefix']} | {s['sheet_number'] or ('p' + str(s['page']))} | {s['tags']} | "
                f"{s['circuits']} | {s['convention']} | {s['stacked_share']} | {s['multi_circuit_share']} |"
            )
    out.append("")
    return "\n".join(out)


def run_survey(args: argparse.Namespace) -> int:
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    data = BackupData(args.backup)
    pop = eval_manifest.population("tagged_training", data, backup_dir=args.backup)
    excluded = set(eval_manifest.excluded_projects(EXCLUSION_PURPOSE))
    ids = [pid for pid in pop["ids"] if pid not in excluded]
    if args.projects:
        wanted = [p.strip() for p in args.projects.split(",") if p.strip()]
        ids = [pid for pid in ids if any(pid.startswith(w) for w in wanted)]
    if args.limit:
        ids = ids[: args.limit]
    cache_dir = None if args.no_cache else root / "cache" / "pagefacts_survey"
    projects = data.by_id("projects")
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for i, pid in enumerate(ids, start=1):
        name = str((projects.get(pid) or {}).get("name") or "")
        docs: list[DocFacts] = []
        notes: list[str] = []
        files = [r for r in data.files(pid) if r.get("category") in DRAWING_CATEGORIES]
        for row in files:
            facts, note = facts_for_file(data, row, cache_dir=cache_dir, max_pages=args.max_pages)
            if facts is None:
                notes.append(f"{row.get('filename')}: {note}")
                continue
            docs.append(facts)
        row_out = survey_project(pid, name, docs, notes)
        rows.append(row_out)
        print(
            f"[{i}/{len(ids)}] {pid[:8]} {row_out['class']:14s} plan pages {row_out['plan_pages']:3d} "
            f"tags {row_out['tags']:4d} docs {row_out['documents']} ({time.monotonic() - started:.0f}s)",
            flush=True,
        )
    split = build_split(rows, excluded)
    report = render_report(rows, split)
    reports_dir = root / "reports"
    manifest_dir = root / "manifest"
    reports_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "measure_survey.md").write_text(report)
    (manifest_dir / "ws3_split.json").write_text(json.dumps(split, indent=2))
    if args.json:
        args.json.write_text(json.dumps({"rows": rows, "split": split}, indent=2))
    classes = Counter(r["class"] for r in rows)
    conv = Counter(s["convention"] for r in rows for s in r["sheets"])
    print(f"classes: {dict(sorted(classes.items()))}")
    print(f"conventions per sheet: {dict(sorted(conv.items()))}")
    print(f"tagged training split: {split['n_tagged']} of {len(rows)} -> {manifest_dir / 'ws3_split.json'}")
    print(f"report: {reports_dir / 'measure_survey.md'}")
    return 0


# Calibration (B9) ---------------------------------------------------------------------------


def truth_branch(data: BackupData, project_id: str) -> dict[str, Any] | None:
    """Estimator branch truth from the estimate workbook (one-book rule):
    pre-wastage FT of branch conduit (1/2" and 3/4" EMT), #12 and #10."""
    books = []
    for row in data.files(project_id, "estimate"):
        if not str(row.get("filename", "")).lower().endswith((".xlsx", ".xlsm")):
            continue
        try:
            books.append((row, estimate_parse.parse_estimate(data.file_path(row).read_bytes())))
        except Exception:  # noqa: BLE001 - an unparsable book is skipped
            continue
    idx = estimate_parse.choose_book(books)
    if idx is None:
        return None
    meta, parsed = books[idx]
    conduit = wire12 = wire10 = 0.0
    for ln in parsed.item_lines:
        if ln.unit != "FT" or ln.qty_base is None:
            continue
        d = ln.description.upper()
        c = parse_conduit(d)
        if c is not None and c[1] == "EMT" and c[0].replace(" ", "") in BRANCH_CONDUIT_SIZES:
            conduit += float(ln.qty_base)
            continue
        m = BRANCH_WIRE_RE.match(d)
        if m and "G" not in d.split("THHN")[0].replace("#", "").replace(m.group(1), "", 1).strip()[:2]:
            if m.group(1) == "12":
                wire12 += float(ln.qty_base)
            else:
                wire10 += float(ln.qty_base)
    return {"book": meta.get("filename"), "conduit_ft": round(conduit, 1), "wire12_ft": round(wire12, 1), "wire10_ft": round(wire10, 1), "wire_ft": round(wire12 + wire10, 1)}


def measure_components(data: BackupData, project_id: str, *, cache_dir: Path | None, max_pages: int) -> dict[str, Any]:
    """One build_measure at the code defaults, reduced to the terms the grid
    needs: measured route FT (scales with route_factor), allowance FT (does
    not), devices (drops), the home-run lengths per circuit, the wire split."""
    docs: list[measure.MeasureDocument] = []
    pdfs: dict[str, bytes] = {}
    notes: list[str] = []
    files = [r for r in data.files(project_id) if r.get("category") in DRAWING_CATEGORIES]
    for i, row in enumerate(files, start=1):
        facts, note = facts_for_file(data, row, cache_dir=cache_dir, max_pages=max_pages)
        if facts is None:
            notes.append(f"{row.get('filename')}: {note}")
            continue
        doc_id = str(row.get("id") or f"doc{i}")
        docs.append(measure.MeasureDocument(document_id=doc_id, doc_index=i, facts=facts, label=facts.label, name=facts.name))
        pdfs[doc_id] = data.file_path(row).read_bytes()
    pages = sum(len(d.facts.pages) for d in docs)
    started = time.monotonic()
    conv = routes_mod.Conventions()
    inputs = measure.MeasureInputs.from_pdfs(None, docs, pdfs, project_type=(data.project(project_id) or {}).get("project_type"), eval_case_id=project_id)
    out = measure.build_measure(inputs, conv)
    elapsed = time.monotonic() - started
    measured = allowance = collector = homerun_total = 0.0
    devices = 0
    homeruns: list[float] = []
    wire12 = wire10 = 0.0
    tiers: Counter[str] = Counter()
    for r in out.get("routes") or []:
        tiers[str(r.get("tier"))] += 1
        if r.get("tier") in ("tagged", "tie_in", "untagged"):
            devices += int(r.get("devices") or 0)
        hr = 0.0
        for s in r.get("segments") or []:
            ft = float(s.get("length_ft") or 0)
            if s.get("kind") in ("collector", "homerun", "drop"):
                measured += ft
            else:
                allowance += ft
            if s.get("kind") in ("collector", "drop"):
                collector += ft
            if s.get("kind") == "homerun":
                hr += ft
                homerun_total += ft
            for awg, n in (s.get("conductors_parsed") or {}).items():
                if awg == "#12":
                    wire12 += ft * n
                elif awg == "#10":
                    wire10 += ft * n
        # Home-run length per circuit: drawn-panel home runs only (never the allowance).
        if hr > 0 and r.get("tier") == "tagged":
            homeruns.append(hr)
    return {
        "code": code_fingerprint(),
        "project_id": project_id,
        "pages": pages,
        "routes": len(out.get("routes") or []),
        "tagged_routes": sum(1 for r in out.get("routes") or [] if r.get("tier") in ("tagged", "tie_in")),
        "tiers": dict(sorted(tiers.items())),
        "measured_ft": round(measured, 1),
        "allowance_ft": round(allowance, 1),
        "collector_ft": round(collector, 1),
        "homerun_ft": round(homerun_total, 1),
        "bands_source": (out.get("bands") or {}).get("bands_source"),
        "devices": devices,
        "wire12_ft": round(wire12, 1),
        "wire10_ft": round(wire10, 1),
        "homeruns": [round(h, 1) for h in homeruns],
        "registrations": len(out.get("registrations") or []),
        "tags": (out.get("stats") or {}).get("tags"),
        "elapsed_s": round(elapsed, 1),
        "s_per_25_sheets": round(elapsed * 25.0 / pages, 1) if pages else None,
        "findings": sorted({f.get("code") for f in out.get("findings") or []}),
        "notes": notes,
    }


def code_fingerprint() -> str:
    """sha256 (12 hex) of the WS3 modules a calibration row depends on, so a
    cached row is reused only when the code that produced it is unchanged."""
    import hashlib

    h = hashlib.sha256()
    for mod in (measure, routes_mod, circuits, measure.geometry):
        h.update(Path(mod.__file__).read_bytes())
    return h.hexdigest()[:12]


def conduit_at(comp: dict[str, Any], route_factor: float, drop: float) -> float:
    return comp["measured_ft"] * route_factor + comp["allowance_ft"] + drop * comp["devices"]


def grid_errors(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Median absolute relative error of branch conduit per grid point."""
    out: dict[str, dict[str, Any]] = {}
    for rf in ROUTE_FACTORS:
        for drop in DROPS:
            errs = [abs(conduit_at(r["comp"], rf, drop) - r["truth"]["conduit_ft"]) / r["truth"]["conduit_ft"] for r in rows if r["truth"]["conduit_ft"] > 0]
            if not errs:
                continue
            out[f"{rf:.2f}/{drop:g}"] = {"route_factor": rf, "drop_ft_per_device": drop, "panel_rise_ft": PANEL_RISE, "n": len(errs), "median_abs_err": round(statistics.median(errs), 4), "within_15": round(sum(e <= 0.15 for e in errs) / len(errs), 3), "within_30": round(sum(e <= 0.30 for e in errs) / len(errs), 3)}
    return out


def best_point(grid: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not grid:
        return None
    return min(grid.values(), key=lambda g: (g["median_abs_err"], -g["within_15"], abs(g["route_factor"] - 1.0), g["drop_ft_per_device"]))


def lopo(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errs = []
    picks = []
    for i, held in enumerate(rows):
        if held["truth"]["conduit_ft"] <= 0:
            continue
        others = [r for j, r in enumerate(rows) if j != i]
        best = best_point(grid_errors(others))
        if best is None:
            continue
        e = abs(conduit_at(held["comp"], best["route_factor"], best["drop_ft_per_device"]) - held["truth"]["conduit_ft"]) / held["truth"]["conduit_ft"]
        errs.append(e)
        picks.append((held["project_id"][:8], best["route_factor"], best["drop_ft_per_device"], round(e, 3)))
    return {"n": len(errs), "median_abs_err": round(statistics.median(errs), 4) if errs else None, "within_15": round(sum(e <= 0.15 for e in errs) / len(errs), 3) if errs else None, "within_30": round(sum(e <= 0.30 for e in errs) / len(errs), 3) if errs else None, "picks": picks}


def render_calibration(rows: list[dict[str, Any]], grid: dict[str, dict[str, Any]], best: dict[str, Any] | None, lopo_result: dict[str, Any], homerun_median: float | None, split: dict[str, Any], skipped: list[tuple[str, str]]) -> str:
    out: list[str] = []
    out.append("# WS3 measure calibration (B9)")
    out.append("")
    out.append(f"Built {now_pacific_iso()} (America/Los_Angeles). Tagged training split of {split.get('n_tagged')} projects from `manifest/ws3_split.json` (holdout excluded, never tuned on). Code defaults: route_factor 1.00, drop 0, panel_rise 0 (`g3-branch-2026-10-a`). This report changes no default; the owner signs off the `-b` conventions.")
    out.append("")
    out.append("Objective: median absolute relative error of branch conduit FT (1/2\" plus 3/4\" EMT, pre-wastage) against the estimate workbook (one-book rule). Grid: route_factor 0.90 to 1.30 step 0.05, drop_ft_per_device 0 / 4 / 8, panel_rise fixed 0.")
    out.append("")
    out.append("## Per project (code defaults)")
    out.append("")
    out.append("| Project | Name | Tags | Routes | Measured FT | Allowance FT | Devices | Conduit FT (model) | Conduit FT (estimator) | Error | Wire FT (model) | Wire FT (estimator) | Wire error | Findings | Pages | s | s per 25 sheets |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        c, t = r["comp"], r["truth"]
        model = conduit_at(c, 1.0, 0.0)
        err = (model - t["conduit_ft"]) / t["conduit_ft"] if t["conduit_ft"] > 0 else None
        wire_model = c["wire12_ft"] + c["wire10_ft"]
        werr = (wire_model - t["wire_ft"]) / t["wire_ft"] if t["wire_ft"] > 0 else None
        out.append(f"| {r['project_id'][:8]} | {r['name'][:28]} | {c['tags']} | {c['tagged_routes']}/{c['routes']} | {c['measured_ft']:.0f} | {c['allowance_ft']:.0f} | {c['devices']} | {model:.0f} | {t['conduit_ft']:.0f} | {'n/a' if err is None else f'{err:+.0%}'} | {wire_model:.0f} | {t['wire_ft']:.0f} | {'n/a' if werr is None else f'{werr:+.0%}'} | {', '.join(c['findings'])[:60]} | {c.get('pages')} | {c['elapsed_s']} | {c.get('s_per_25_sheets')} |")
    if skipped:
        out.append("")
        out.append("Skipped: " + "; ".join(f"{p[:8]} ({why})" for p, why in skipped))
    out.append("")
    out.append("## Grid (median absolute error of branch conduit)")
    out.append("")
    out.append("| route_factor | drop 0 | drop 4 | drop 8 |")
    out.append("|---|---|---|---|")
    for rf in ROUTE_FACTORS:
        cells = []
        for drop in DROPS:
            g = grid.get(f"{rf:.2f}/{drop:g}")
            cells.append("n/a" if g is None else f"{g['median_abs_err']:.1%} ({g['within_15']:.0%} / {g['within_30']:.0%})")
        out.append(f"| {rf:.2f} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("Cells: median abs error (share within 15% / within 30%).")
    out.append("")
    if best:
        out.append(f"Best grid point (in-sample): route_factor {best['route_factor']:.2f}, drop {best['drop_ft_per_device']:g}, panel_rise {PANEL_RISE:g}: median abs error {best['median_abs_err']:.1%}, within 15% on {best['within_15']:.0%}, within 30% on {best['within_30']:.0%} (n {best['n']}).")
    out.append("")
    out.append(f"Leave-one-project-out: n {lopo_result['n']}, median abs error {lopo_result['median_abs_err'] if lopo_result['median_abs_err'] is None else f'{lopo_result['median_abs_err']:.1%}'}, within 15% on {lopo_result['within_15']}, within 30% on {lopo_result['within_30']}. Picks per held-out project (project, route_factor, drop, error): {lopo_result['picks']}")
    out.append("")
    out.append(f"Golden median home-run length per circuit (homerun segments of tagged routes to a located panel, allowances excluded): {'n/a' if homerun_median is None else f'{homerun_median:.1f} FT'} (candidate `HOMERUN_MEDIAN_FT` in plans_reader_measure_bands; code default stays 150 until sign-off).")
    out.append("")
    acc = golden_acceptance(rows)
    label = f"provisional: n {acc['n_conduit']} < {GOLDEN_MIN_N}" if acc["provisional"] else f"n {acc['n_conduit']} >= {GOLDEN_MIN_N}, binding"
    out.append(f"## Spec 6 WS3 golden acceptance (code defaults; {label})")
    out.append("")
    out.append(f"- branch conduit within 15% on {acc['conduit_within_15']:.0%} (target >= 60%) and within 30% on {acc['conduit_within_30']:.0%} (target >= 85%), n {acc['n_conduit']}: {acc['conduit_status']}")
    out.append(f"- branch wire within 20% on {acc['wire_within_20']:.0%} (target >= 60%), n {acc['n_wire']}: {acc['wire_status']}")
    out.append(f"- derive time per 25-sheet set (build_measure on cached WS1 facts, seconds x 25 / drawing pages): p50 {acc['s25_p50']} s, p90 {acc['s25_p90']} s (target p90 < 60 s), n {acc['n_time']}: {'MET' if acc['time_met'] else 'NOT MET'}")
    if acc["failed"]:
        out.append(f"- WS3 golden acceptance FAILED at n {acc['n_conduit']} (owner question per spec 0.5): the misses above are binding, not provisional.")
    out.append("")
    out.append("## Collector and home-run FT (reported separately)")
    out.append("")
    out.append("The estimate workbooks carry one branch conduit total per project, so a separate collector or home-run error is not measurable on this corpus; the model's split is reported for the owner.")
    out.append("")
    out.append("| Project | Collector FT | Home-run FT | Allowance FT | Home-run share of measured |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        c = r["comp"]
        col, hr = c.get("collector_ft"), c.get("homerun_ft")
        share = f"{hr / (col + hr):.0%}" if col is not None and hr is not None and (col + hr) > 0 else "n/a"
        out.append(f"| {r['project_id'][:8]} | {'n/a' if col is None else f'{col:.0f}'} | {'n/a' if hr is None else f'{hr:.0f}'} | {c['allowance_ft']:.0f} | {share} |")
    out.append("")
    return "\n".join(out)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (pos - lo), 1)


def golden_acceptance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The spec 6 WS3 golden shares at the code defaults, plus the derive
    time per 25-sheet set. `provisional` is true only while n < 15; at
    n >= 15 a miss sets `failed` (owner question per spec 0.5)."""
    cerr = [abs(conduit_at(r["comp"], 1.0, 0.0) - r["truth"]["conduit_ft"]) / r["truth"]["conduit_ft"] for r in rows if r["truth"]["conduit_ft"] > 0]
    werr = [abs(r["comp"]["wire12_ft"] + r["comp"]["wire10_ft"] - r["truth"]["wire_ft"]) / r["truth"]["wire_ft"] for r in rows if r["truth"]["wire_ft"] > 0]
    s25 = [float(r["comp"]["s_per_25_sheets"]) for r in rows if r["comp"].get("s_per_25_sheets") is not None]
    c15 = sum(e <= 0.15 for e in cerr) / len(cerr) if cerr else 0.0
    c30 = sum(e <= 0.30 for e in cerr) / len(cerr) if cerr else 0.0
    w20 = sum(e <= 0.20 for e in werr) / len(werr) if werr else 0.0
    p90 = _quantile(s25, 0.9)
    conduit_met = c15 >= 0.60 and c30 >= 0.85
    wire_met = w20 >= 0.60
    provisional = len(cerr) < GOLDEN_MIN_N

    def _status(met: bool) -> str:
        if met:
            return "MET"
        return "NOT MET (provisional)" if provisional else "FAILED"

    return {
        "n_conduit": len(cerr),
        "provisional": provisional,
        "failed": not provisional and not (conduit_met and wire_met),
        "conduit_within_15": round(c15, 3),
        "conduit_within_30": round(c30, 3),
        "conduit_met": conduit_met,
        "conduit_status": _status(conduit_met),
        "n_wire": len(werr),
        "wire_within_20": round(w20, 3),
        "wire_met": wire_met,
        "wire_status": _status(wire_met),
        "n_time": len(s25),
        "s25_p50": _quantile(s25, 0.5),
        "s25_p90": p90,
        "time_met": p90 is not None and p90 < 60.0,
    }


def run_calibrate(args: argparse.Namespace) -> int:
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    data = BackupData(args.backup)
    split_path = Path(args.split) if args.split else root / "manifest" / "ws3_split.json"
    split = json.loads(split_path.read_text())
    excluded = set(eval_manifest.excluded_projects(EXCLUSION_PURPOSE))
    ids = [pid for pid in split.get("tagged_training") or [] if pid not in excluded]
    if args.projects:
        wanted = [p.strip() for p in args.projects.split(",") if p.strip()]
        ids = [pid for pid in ids if any(pid.startswith(w) for w in wanted)]
    if args.limit:
        ids = ids[: args.limit]
    cache_dir = None if args.no_cache else root / "cache" / "pagefacts_survey"
    comp_cache = root / "cache" / "measure_calibrate"
    comp_cache.mkdir(parents=True, exist_ok=True)
    projects = data.by_id("projects")
    rows: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    started = time.monotonic()
    for i, pid in enumerate(ids, start=1):
        name = str((projects.get(pid) or {}).get("name") or "")
        truth = truth_branch(data, pid)
        if truth is None or truth["conduit_ft"] <= 0:
            skipped.append((pid, "no branch conduit in the estimate book"))
            print(f"[{i}/{len(ids)}] {pid[:8]} skipped: no branch conduit truth", flush=True)
            continue
        cpath = comp_cache / f"{pid}.json"
        cached = json.loads(cpath.read_text()) if cpath.exists() and not args.no_cache else None
        if cached is not None and cached.get("code") == code_fingerprint():
            comp = cached
        else:
            comp = measure_components(data, pid, cache_dir=cache_dir, max_pages=args.max_pages)
            cpath.write_text(json.dumps(comp, indent=1))
        rows.append({"project_id": pid, "name": name, "comp": comp, "truth": truth})
        model = conduit_at(comp, 1.0, 0.0)
        print(f"[{i}/{len(ids)}] {pid[:8]} {name[:30]:30s} tags {comp['tags']} routes {comp['tagged_routes']} conduit {model:.0f} vs {truth['conduit_ft']:.0f} ({(model - truth['conduit_ft']) / truth['conduit_ft']:+.0%}) in {comp['elapsed_s']} s ({time.monotonic() - started:.0f}s)", flush=True)
    grid = grid_errors(rows)
    best = best_point(grid)
    lopo_result = lopo(rows)
    homeruns = [h for r in rows for h in r["comp"]["homeruns"]]
    homerun_median = round(statistics.median(homeruns), 1) if homeruns else None
    report = render_calibration(rows, grid, best, lopo_result, homerun_median, split, skipped)
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "measure_calibration.md").write_text(report)
    payload = {"built_at": now_pacific_iso(), "split": str(split_path), "n": len(rows), "rows": rows, "grid": grid, "best": best, "lopo": lopo_result, "homerun_median_ft": homerun_median, "acceptance": golden_acceptance(rows), "skipped": skipped}
    (reports_dir / "measure_calibration.json").write_text(json.dumps(payload, indent=1))
    if args.json:
        args.json.write_text(json.dumps(payload, indent=1))
    print(f"best: {best}")
    print(f"lopo: {lopo_result}")
    print(f"homerun median: {homerun_median}")
    print(f"acceptance: {golden_acceptance(rows)}")
    print(f"report: {reports_dir / 'measure_calibration.md'}")
    return 0


# Never empty (untagged and vector_text pool projects) ------------------------------------------


def branch_rows_ft(out: dict[str, Any]) -> float:
    """Total FT of the WIRING M rows (branch wire, conduit or cable)."""
    return round(sum(float(q.get("quantity") or 0) for q in out.get("quantities") or [] if str(q.get("group") or "").upper() == "WIRING" and str(q.get("unit") or "").upper() == "FT"), 1)


def bands_device_counts() -> dict[str, float]:
    """Estimator device counts per project from the active bands file
    (counts only; empty when no bands file exists)."""
    from app.services import plans_reader_bands

    path = plans_reader_bands.latest_bands_path()
    if path is None:
        return {}
    bands = plans_reader_bands.load_bands(path)
    return {str(p.get("project_id")): float(p.get("devices") or 0) for p in bands.projects if p.get("devices")}


def never_empty_row(data: BackupData, project_id: str, *, cache_dir: Path | None, max_pages: int, devices: float | None = None) -> dict[str, Any]:
    """build_measure on WS1 facts only (no model category outputs) for one
    project, reduced to the never-empty check; with `devices` a second build
    gives the reader's device count (the density tier input) as well."""
    docs: list[measure.MeasureDocument] = []
    pdfs: dict[str, bytes] = {}
    notes: list[str] = []
    files = [r for r in data.files(project_id) if r.get("category") in DRAWING_CATEGORIES]
    for i, row in enumerate(files, start=1):
        facts, note = facts_for_file(data, row, cache_dir=cache_dir, max_pages=max_pages)
        if facts is None:
            notes.append(f"{row.get('filename')}: {note}")
            continue
        doc_id = str(row.get("id") or f"doc{i}")
        docs.append(measure.MeasureDocument(document_id=doc_id, doc_index=i, facts=facts, label=facts.label, name=facts.name))
        pdfs[doc_id] = data.file_path(row).read_bytes()
    started = time.monotonic()
    inputs = measure.MeasureInputs.from_pdfs(None, docs, pdfs, project_type=(data.project(project_id) or {}).get("project_type"), eval_case_id=project_id)
    out = measure.build_measure_safe(inputs, routes_mod.Conventions())
    tiers = Counter(str(r.get("tier")) for r in out.get("routes") or [])
    with_devices: float | None = None
    if devices:
        inputs2 = measure.MeasureInputs.from_pdfs(None, docs, pdfs, project_type=inputs.project_type, eval_case_id=project_id, new_device_count=int(round(devices)))
        with_devices = branch_rows_ft(measure.build_measure_safe(inputs2, routes_mod.Conventions()))
    return {
        "devices_count": devices,
        "branch_ft_with_devices": with_devices,
        "non_empty_with_devices": None if with_devices is None else with_devices > 0,
        "code": code_fingerprint(),
        "project_id": project_id,
        "documents": len(docs),
        "pages": sum(len(d.facts.pages) for d in docs),
        "branch_ft": branch_rows_ft(out),
        "non_empty": branch_rows_ft(out) > 0,
        "routes": len(out.get("routes") or []),
        "tiers": dict(sorted(tiers.items())),
        "new_devices": (out.get("stats") or {}).get("new_devices"),
        "findings": sorted({str(f.get("code")) for f in out.get("findings") or []}),
        "elapsed_s": round(time.monotonic() - started, 1),
        "notes": notes,
    }


def render_never_empty(rows: list[dict[str, Any]], classes: dict[str, str]) -> str:
    n = len(rows)
    ok = sum(1 for r in rows if r["non_empty"])
    out = ["# WS3 never-empty check (untagged and vector_text projects)", ""]
    out.append(f"Built {now_pacific_iso()} (America/Los_Angeles). build_measure with WS1 facts only (no model category outputs, zero model calls) on the {', '.join(NEVER_EMPTY_CLASSES)} projects of `manifest/ws3_split.json` (holdout excluded). Non-empty = a WIRING M row with FT > 0. Counts and ratios only.")
    out.append("")
    out.append(f"- WS1 facts only (no count rows): non-empty on {ok} of {n} projects ({ok / n:.0%})" if n else "- no projects")
    for cls in NEVER_EMPTY_CLASSES:
        sub = [r for r in rows if classes.get(r["project_id"]) == cls]
        if sub:
            out.append(f"  - {cls}: {sum(1 for r in sub if r['non_empty'])} of {len(sub)}")
    dev = [r for r in rows if r.get("non_empty_with_devices") is not None]
    if dev:
        out.append(f"- with the reader's device count (estimator device count from the bands file standing in for the count rows): non-empty on {sum(1 for r in dev if r['non_empty_with_devices'])} of {len(dev)} projects that have a count; {n - len(dev)} projects have no device count in the bands file")
    out.append("")
    out.append("| Project | Class | Docs | Pages | Branch FT (facts only) | Routes | Tiers | Device count | Branch FT (with count) | Findings | s |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (classes.get(r["project_id"], ""), r["project_id"])):
        tiers = ", ".join(f"{k} {v}" for k, v in r["tiers"].items()) or "none"
        wd = r.get("branch_ft_with_devices")
        out.append(f"| {r['project_id'][:8]} | {classes.get(r['project_id'])} | {r['documents']} | {r['pages']} | {r['branch_ft']:.0f} | {r['routes']} | {tiers} | {'n/a' if not r.get('devices_count') else f'{r['devices_count']:.0f}'} | {'n/a' if wd is None else f'{wd:.0f}'} | {', '.join(r['findings'])[:60]} | {r['elapsed_s']} |")
    out.append("")
    return "\n".join(out)


def run_never_empty(args: argparse.Namespace) -> int:
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    data = BackupData(args.backup)
    split_path = Path(args.split) if args.split else root / "manifest" / "ws3_split.json"
    split = json.loads(split_path.read_text())
    excluded = set(eval_manifest.excluded_projects(EXCLUSION_PURPOSE))
    classes = {pid: cls for pid, cls in (split.get("classes") or {}).items() if cls in NEVER_EMPTY_CLASSES and pid not in excluded}
    ids = sorted(classes)
    if args.projects:
        wanted = [p.strip() for p in args.projects.split(",") if p.strip()]
        ids = [pid for pid in ids if any(pid.startswith(w) for w in wanted)]
    if args.limit:
        ids = ids[: args.limit]
    cache_dir = None if args.no_cache else root / "cache" / "pagefacts_survey"
    rows: list[dict[str, Any]] = []
    device_counts = bands_device_counts()
    for i, pid in enumerate(ids, start=1):
        row = never_empty_row(data, pid, cache_dir=cache_dir, max_pages=args.max_pages, devices=device_counts.get(pid))
        row["class"] = classes[pid]
        rows.append(row)
        print(f"[{i}/{len(ids)}] {pid[:8]} {classes[pid]:14s} branch FT {row['branch_ft']:8.0f} routes {row['routes']} tiers {row['tiers']} in {row['elapsed_s']} s", flush=True)
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "measure_never_empty.md").write_text(render_never_empty(rows, classes))
    dev = [r for r in rows if r.get("non_empty_with_devices") is not None]
    payload = {"built_at": now_pacific_iso(), "split": str(split_path), "n": len(rows), "non_empty": sum(1 for r in rows if r["non_empty"]), "n_with_devices": len(dev), "non_empty_with_devices": sum(1 for r in dev if r["non_empty_with_devices"]), "rows": rows}
    (reports_dir / "measure_never_empty.json").write_text(json.dumps(payload, indent=1))
    if args.json:
        args.json.write_text(json.dumps(payload, indent=1))
    print(f"non-empty (facts only): {payload['non_empty']} of {payload['n']}; with the device count: {payload['non_empty_with_devices']} of {payload['n_with_devices']}")
    print(f"report: {reports_dir / 'measure_never_empty.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.survey:
        return run_survey(args)
    if args.calibrate:
        return run_calibrate(args)
    if args.never_empty:
        return run_never_empty(args)
    _parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
