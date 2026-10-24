"""WS3 markup truth (step B7; docs/AI_ESTIMATING_BUILD_SPEC.md 6 WS3, 8.7
markup population). $0, no model, read-only over the production backup.

Usage (from bdr_be):

    uv run python scripts/plans_reader_markup_truth.py \\
        [--eval-root EVAL_ROOT] [--backup BACKUP_DIR] [--projects 0d03bc95,...] [--json out.json]

The estimators' trench and saw-cut markups (`project_files` category
`markup`, 33 files over 30 projects on 2026-10-21, holdout excluded through
`eval_manifest.excluded_projects("calibration")`) hold `/Line` and
`/PolyLine` annotations drawn over the plan, and none carries a `/Measure`
dictionary, so the page's own printed scale is the only scale. Per page this
script:

1. sums the annotation lengths in paper inches (PDF user units / 72; a
   page `/Rotate` leaves lengths unchanged);
2. reads the printed scale from the page's text layer through WS1
   `positioned_words` and `plans_reader_geometry.parse_scale_text` (one
   distinct scale = unambiguous; none or several = ambiguous, reported but
   not scored);
3. converts to feet and compares with the estimator BOQ (category `boq`,
   latest, `parse_estimator_boq`): saw-cut rows in FT directly, trench rows
   in CU FT divided by the depth x width the description states
   (`2' DEEP X 1' WIDE`), both summed per project.

It validates the measurement code itself (spec target: within 5% on at
least 80% of the pages with an unambiguous scale) and writes
`EVAL_ROOT/reports/markup_truth.md` plus JSON. The BOQ truth is per
project, so two shares are reported and labeled: the PAGE share (the spec
metric) over the pages that carry their own truth, i.e. the only annotated
page of a scored project, and the PER-PROJECT share (all annotated pages
of a project summed against its BOQ total). The target is judged on the
page share. Counts and ratios only; no
dollar figures anywhere in the outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pypdf import PdfReader  # noqa: E402

from app.services import eval_manifest  # noqa: E402
from app.services import plans_reader_geometry as geometry  # noqa: E402
from app.services.eval_reference import DEFAULT_BACKUP_DIR, BackupData, now_pacific_iso  # noqa: E402
from app.services.pagefacts import textlayer  # noqa: E402
from app.services.plans_reader_score import parse_estimator_boq  # noqa: E402

EXCLUSION_PURPOSE = "calibration"
TARGET_TOL = 0.05
TARGET_SHARE = 0.80
TRENCH_RE = re.compile(r"TRENCH|EXCAVAT", re.I)
SAWCUT_RE = re.compile(r"SAW\s*CUT", re.I)
DIMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*'?\s*DEEP\s*X\s*(\d+(?:\.\d+)?)\s*'?\s*WIDE", re.I)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WS3 markup truth (B7).")
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--backup", type=Path, default=DEFAULT_BACKUP_DIR)
    p.add_argument("--projects", default=None, help="comma list of project ids or prefixes")
    p.add_argument("--json", type=Path, default=None)
    return p


# Annotations ----------------------------------------------------------------------------------


def annotation_lengths_in(page: Any) -> tuple[float, int, dict[str, int]]:
    """(total inches, line count, counts by subtype) of the /Line and
    /PolyLine annotations of a pypdf page."""
    total = 0.0
    n = 0
    kinds: dict[str, int] = {}
    for ref in page.get("/Annots") or []:
        try:
            obj = ref.get_object()
        except Exception:  # noqa: BLE001 - a broken annotation is skipped
            continue
        sub = str(obj.get("/Subtype") or "")
        kinds[sub] = kinds.get(sub, 0) + 1
        pts: list[tuple[float, float]] = []
        if sub == "/Line":
            raw = obj.get("/L")
            if raw and len(raw) >= 4:
                pts = [(float(raw[0]), float(raw[1])), (float(raw[2]), float(raw[3]))]
        elif sub == "/PolyLine":
            raw = obj.get("/Vertices")
            if raw:
                vals = [float(v) for v in raw]
                pts = list(zip(vals[0::2], vals[1::2], strict=False))
        if len(pts) < 2:
            continue
        n += 1
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            total += math.hypot(x1 - x0, y1 - y0) / 72.0
    return total, n, kinds


def page_scale(pdf: bytes, page_index0: int) -> tuple[float | None, list[str]]:
    """(ft_per_in, scale texts) from the page's text layer: one distinct
    scaled value is unambiguous; none or several give None."""
    try:
        words = textlayer.positioned_words(pdf, page_index0, objects=False)
    except Exception:  # noqa: BLE001 - no text layer
        return None, []
    found: dict[float, str] = {}
    texts: list[str] = []
    for ln in words.lines:
        parsed = geometry.parse_scale_text(ln.t)
        if parsed is None or parsed.ft_per_in is None:
            continue
        found.setdefault(parsed.ft_per_in, parsed.text)
        texts.append(parsed.text)
    if len(found) == 1:
        return next(iter(found)), texts
    return None, texts


# Truth --------------------------------------------------------------------------------------------


def truth_rows(data: BackupData, project_id: str) -> dict[str, Any]:
    """Trench and saw-cut FT from the estimator BOQ (latest `boq` xlsx):
    saw cut in FT, trench CU FT / (depth x width) when the description
    states the dimensions."""
    boq = [r for r in data.files(project_id, "boq") if str(r.get("filename", "")).lower().endswith((".xlsx", ".xlsm"))]
    out: dict[str, Any] = {"trench_ft": 0.0, "sawcut_ft": 0.0, "rows": [], "file": None}
    if not boq:
        return out
    out["file"] = boq[-1].get("filename")
    try:
        rows = parse_estimator_boq(data.file_path(boq[-1]).read_bytes())
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}"
        return out
    for r in rows:
        d = r.description.upper()
        if SAWCUT_RE.search(d) and r.unit == "FT":
            out["sawcut_ft"] += float(r.quantity)
            out["rows"].append({"kind": "sawcut", "description": r.description[:80], "quantity": r.quantity, "unit": r.unit, "ft": round(float(r.quantity), 1)})
        elif TRENCH_RE.search(d):
            ft: float | None = None
            if r.unit == "FT":
                ft = float(r.quantity)
            elif r.unit == "CU FT":
                m = DIMS_RE.search(d)
                if m:
                    area = float(m.group(1)) * float(m.group(2))
                    ft = float(r.quantity) / area if area > 0 else None
            if ft is not None:
                out["trench_ft"] += ft
            out["rows"].append({"kind": "trench", "description": r.description[:80], "quantity": r.quantity, "unit": r.unit, "ft": None if ft is None else round(ft, 1)})
    out["trench_ft"] = round(out["trench_ft"], 1)
    out["sawcut_ft"] = round(out["sawcut_ft"], 1)
    return out


# Run ----------------------------------------------------------------------------------------------


def measure_file(data: BackupData, row: dict[str, Any]) -> list[dict[str, Any]]:
    path = data.file_path(row)
    pdf = path.read_bytes()
    reader = PdfReader(str(path))
    pages: list[dict[str, Any]] = []
    for i, page in enumerate(reader.pages):
        inches, n, kinds = annotation_lengths_in(page)
        ft_per_in, texts = page_scale(pdf, i)
        mb = page.mediabox
        pages.append(
            {
                "page": i + 1,
                "size_in": [round(float(mb.width) / 72, 1), round(float(mb.height) / 72, 1)],
                "rotate": int(page.get("/Rotate") or 0),
                "lines": n,
                "annotations": kinds,
                "inches": round(inches, 3),
                "ft_per_in": ft_per_in,
                "scale_texts": texts[:4],
                "ft": None if ft_per_in is None else round(inches * ft_per_in, 1),
                "ambiguous": ft_per_in is None,
            }
        )
    return pages


def score_project(pages: list[dict[str, Any]], truth: dict[str, Any]) -> dict[str, Any]:
    """Per project: the summed measured feet of unambiguous pages against
    the trench plus saw-cut truth (one markup file usually carries both)."""
    measured = sum(p["ft"] for p in pages if p["ft"] is not None)
    unambiguous = [p for p in pages if not p["ambiguous"] and p["lines"] > 0]
    truth_ft = float(truth.get("trench_ft") or 0) + float(truth.get("sawcut_ft") or 0)
    err = None
    if truth_ft > 0 and unambiguous and not any(p["ambiguous"] and p["lines"] > 0 for p in pages):
        err = (measured - truth_ft) / truth_ft
    return {"measured_ft": round(measured, 1), "truth_ft": round(truth_ft, 1), "unambiguous_pages": len(unambiguous), "ambiguous_pages": sum(1 for p in pages if p["ambiguous"] and p["lines"] > 0), "rel_err": None if err is None else round(err, 4), "within_5": None if err is None else abs(err) <= TARGET_TOL}


def page_scores(project_scores: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Page-level scores where per-page truth exists: a scored project whose
    files hold exactly one annotated page (that page carries the whole BOQ
    truth). Projects with several annotated pages have no per-page truth."""
    out: list[dict[str, Any]] = []
    for ps in project_scores:
        if ps["score"]["rel_err"] is None:
            continue
        pages = [(r["file"], p) for r in rows if r["project_id"] == ps["project_id"] for p in r["pages"] if p["lines"] > 0]
        if len(pages) != 1:
            continue
        file, page = pages[0]
        truth = ps["score"]["truth_ft"]
        err = (page["ft"] - truth) / truth if page["ft"] is not None and truth > 0 else None
        if err is None:
            continue
        out.append({"project_id": ps["project_id"], "file": file, "page": page["page"], "ft": page["ft"], "truth_ft": truth, "rel_err": round(err, 4), "within_5": abs(err) <= TARGET_TOL})
    return out


def render(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    out: list[str] = []
    out.append("# WS3 markup truth (B7)")
    out.append("")
    out.append(f"Built {summary['built_at']} (America/Los_Angeles). Markup population of spec 8.7: {summary['files']} files over {summary['projects']} projects, holdout excluded through `eval_manifest.excluded_projects(\"{EXCLUSION_PURPOSE}\")`. Lengths from /Line and /PolyLine annotations at the page's printed scale (text layer); no /Measure dictionaries exist in this population. Truth: estimator BOQ saw-cut FT and trench CU FT / (depth x width). Counts and ratios only.")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(f"- pages with annotations: {summary['pages_with_lines']}; with an unambiguous printed scale: {summary['pages_unambiguous']}; ambiguous (no or several scales): {summary['pages_ambiguous']}")
    out.append(f"- projects scored (every annotated page unambiguous and a trench or saw-cut truth row): {summary['projects_scored']}")
    out.append(f"- PER-PROJECT share (not the spec metric; all annotated pages of a project summed against its BOQ total): within 5% on {summary['within_5']} of {summary['projects_scored']} scored projects ({summary['within_5_share']}); median abs error {summary['median_abs_err']}")
    out.append(f"- PAGE share (the spec metric, over the {summary['pages_scored']} pages that carry their own truth: the only annotated page of a scored project): within 5% on {summary['page_within_5']} of {summary['pages_scored']} ({summary['page_within_5_share']}); the other {summary['pages_unambiguous'] - summary['pages_scored']} unambiguous pages share one BOQ total with sibling pages and have no per-page truth")
    out.append(f"- spec target: within 5% on at least {TARGET_SHARE:.0%} of unambiguous pages, judged on the page share: {'MET' if summary['target_met'] else 'NOT MET'} (n {summary['pages_scored']} pages; provisional, the per-page truth population is small)")
    out.append("")
    out.append("## Projects")
    out.append("")
    out.append("| Project | Name | File | Pages | Lines | Unambiguous | Ambiguous | Measured FT | Truth FT (trench + saw cut) | Error | Truth rows |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        s = r["score"]
        ps = r.get("project_score") or s
        err = "n/a" if ps["rel_err"] is None else f"{ps['rel_err']:+.1%}"
        truth_desc = "; ".join(f"{t['kind']} {t['quantity']:g} {t['unit']}" for t in r["truth"]["rows"])[:80]
        out.append(f"| {r['project_id'][:8]} | {r['name'][:26]} | {r['file'][:28]} | {len(r['pages'])} | {sum(p['lines'] for p in r['pages'])} | {s['unambiguous_pages']} | {s['ambiguous_pages']} | {s['measured_ft']} | {s['truth_ft']} | {err} (project) | {truth_desc} |")
    out.append("")
    out.append("## Pages")
    out.append("")
    out.append("| Project | File | Page | Size in | Lines | Inches | Scale | FT |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        for p in r["pages"]:
            if p["lines"] == 0:
                continue
            out.append(f"| {r['project_id'][:8]} | {r['file'][:24]} | {p['page']} | {p['size_in'][0]} x {p['size_in'][1]} | {p['lines']} | {p['inches']} | {p['scale_texts'][0] if p['scale_texts'] else 'none'}{' (ambiguous)' if p['ambiguous'] else ''} | {p['ft'] if p['ft'] is not None else 'n/a'} |")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    data = BackupData(args.backup)
    excluded = set(eval_manifest.excluded_projects(EXCLUSION_PURPOSE))
    files = [r for r in data.table("project_files") if r.get("category") == "markup" and str(r.get("project_id")) not in excluded]
    if args.projects:
        wanted = [p.strip() for p in args.projects.split(",") if p.strip()]
        files = [r for r in files if any(str(r.get("project_id")).startswith(w) for w in wanted)]
    projects = data.by_id("projects")
    rows: list[dict[str, Any]] = []
    truths: dict[str, dict[str, Any]] = {}
    seen_bytes: set[tuple[str, str]] = set()
    for row in sorted(files, key=lambda r: (str(r.get("project_id")), str(r.get("created_at") or ""))):
        pid = str(row.get("project_id"))
        path = data.file_path(row)
        if not path.exists() or path.suffix.lower() != ".pdf":
            print(f"{pid[:8]} {row.get('filename')}: missing or not a PDF")
            continue
        # The same markup uploaded twice (identical bytes) counts once.
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if (pid, digest) in seen_bytes:
            print(f"{pid[:8]} {row.get('filename')}: duplicate upload (same bytes), skipped")
            continue
        seen_bytes.add((pid, digest))
        try:
            pages = measure_file(data, row)
        except Exception as exc:  # noqa: BLE001
            print(f"{pid[:8]} {row.get('filename')}: unreadable ({type(exc).__name__})")
            continue
        truth = truths.get(pid) or truth_rows(data, pid)
        truths[pid] = truth
        score = score_project(pages, truth)
        rows.append({"project_id": pid, "name": str((projects.get(pid) or {}).get("name") or ""), "file": str(row.get("filename")), "pages": pages, "truth": truth, "score": score})
        print(f"{pid[:8]} {str(row.get('filename'))[:32]:32s} pages {len(pages)} lines {sum(p['lines'] for p in pages):3d} measured {score['measured_ft']:8.1f} truth {score['truth_ft']:8.1f} err {'n/a' if score['rel_err'] is None else f'{score['rel_err']:+.1%}'}", flush=True)
    # A project with several markup files (one per funding source or phase)
    # is scored on the sum of its files against its one BOQ truth.
    by_project: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_project.setdefault(r["project_id"], []).append(r)
    project_scores: list[dict[str, Any]] = []
    for pid, group in by_project.items():
        pages = [p for r in group for p in r["pages"]]
        score = score_project(pages, truths[pid])
        project_scores.append({"project_id": pid, "name": group[0]["name"], "files": [r["file"] for r in group], "score": score})
        for r in group:
            r["project_score"] = score
    scored = [r for r in project_scores if r["score"]["rel_err"] is not None]
    within = sum(1 for r in scored if r["score"]["within_5"])
    errs = sorted(abs(r["score"]["rel_err"]) for r in scored)
    pscores = page_scores(project_scores, rows)
    page_within = sum(1 for p in pscores if p["within_5"])
    summary = {
        "built_at": now_pacific_iso(),
        "files": len(rows),
        "projects": len(by_project),
        "pages_with_lines": sum(1 for r in rows for p in r["pages"] if p["lines"] > 0),
        "pages_unambiguous": sum(1 for r in rows for p in r["pages"] if p["lines"] > 0 and not p["ambiguous"]),
        "pages_ambiguous": sum(1 for r in rows for p in r["pages"] if p["lines"] > 0 and p["ambiguous"]),
        "projects_scored": len(scored),
        "within_5": within,
        "within_5_share": f"{within / len(scored):.0%}" if scored else "n/a",
        "median_abs_err": f"{errs[len(errs) // 2]:.1%}" if errs else "n/a",
        "share_basis": "page",
        "pages_scored": len(pscores),
        "page_within_5": page_within,
        "page_within_5_share": f"{page_within / len(pscores):.0%}" if pscores else "n/a",
        "target_met": bool(pscores) and page_within / len(pscores) >= TARGET_SHARE,
        "project_within_5_share_label": "per-project (not the spec page metric)",
        "page_scores": pscores,
        "project_scores": project_scores,
    }
    report = render(rows, summary)
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "markup_truth.md").write_text(report)
    payload = {"summary": summary, "rows": rows}
    (reports_dir / "markup_truth.json").write_text(json.dumps(payload, indent=1, default=str))
    if args.json:
        args.json.write_text(json.dumps(payload, indent=1, default=str))
    print(json.dumps(summary, indent=1))
    print(f"report: {reports_dir / 'markup_truth.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
