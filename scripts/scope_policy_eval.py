"""Bid conditions corpus eval (WS5, $0, offline; spec 8.7 wage population).

Runs `bid_conditions.classify` over every project of the wage population
(`projects.json` rows with `wage_type` set, minus names starting with ZZ or
containing TEST) with the backup's document text, in three variants:

- record: the full project row (the project record wins; expected 100%);
- wage_hidden (the acceptance metric): `wage_type` removed, so documents,
  owner type, lexicon and the default decide;
- owner_hidden: `wage_type` and `owner_type` removed (documents, lexicon over
  the project name and first pages, default).

Truth is `projects.wage_type`. A free-text value (the NRS 338 threshold text)
is reported on its own row, not scored. Holdout projects (spec 8.1) are
reported in their own column and never tuned on. Cue precision is the share of
projects whose wage was decided by document cues (wage_hidden) that agree with
the record; it must be 100%.

Document text: `project_files` rows mapped by category (never by folder):
specification, addendum and other PDFs up to 400 pages, drawing and
electrical_drawing PDFs first 4 pages; the text layer is read with pypdfium2
under the PDFium lock and cached under `EVAL_ROOT/cache/ws5_pdf_text/`.

Writes `EVAL_ROOT/reports/bid_conditions_eval.md` and `.json`.

Usage:
  uv run python scripts/scope_policy_eval.py [--bid-conditions] [--backup DIR]
      [--eval-root DIR] [--workers N] [--min-agreement 0.93]
Exit status 1 when wage_hidden agreement (non-holdout) is below the minimum or
cue precision is below 100%.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BE_ROOT = Path(__file__).resolve().parents[1]
if str(_BE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BE_ROOT))

from app.services import bid_conditions  # noqa: E402
from app.services.estimating_contracts import WAGE_TYPES  # noqa: E402

DEFAULT_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
DEFAULT_EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
PACIFIC = ZoneInfo("America/Los_Angeles")
MIN_AGREEMENT = 0.93

FULL_TEXT_CATEGORIES: frozenset[str] = frozenset({"specification", "addendum", "other"})
DRAWING_CATEGORIES: frozenset[str] = frozenset({"drawing", "electrical_drawing"})
FULL_TEXT_MAX_PAGES = 400
DRAWING_MAX_PAGES = 4
VARIANTS: tuple[str, ...] = ("record", "wage_hidden", "owner_hidden")

# Spec 8.1 holdout, used only when app.services.eval_manifest (WS0) cannot be
# imported. Keep in sync with the spec; eval_manifest is the source of truth.
HOLDOUT_PROJECT_IDS: frozenset[str] = frozenset(
    {
        "078c470d-efa1-479c-b579-1690f5da47a0",  # 7188
        "ba9f0bc2-ef71-4bfe-8b4f-70f92cf0c4b9",  # 7173
        "220937f8-c69d-43b2-b5f8-b2e50f350310",  # 7178
        "346a995e-3758-40cc-994c-008e00f299c6",  # 7148
    }
)


def holdout_projects() -> tuple[frozenset[str], str]:
    """(holdout ids, source). The eval reports them in their own column."""
    try:
        from app.services import eval_manifest  # type: ignore[attr-defined]

        return frozenset(eval_manifest.excluded_projects("scope_policy")), "eval_manifest"
    except Exception:  # noqa: BLE001 - any import or lookup failure uses the spec list
        return HOLDOUT_PROJECT_IDS, "spec 8.1 fallback list"


# PDF text layer ---------------------------------------------------------------------------


def pdf_page_texts(path: Path, max_pages: int) -> list[str]:
    """Text layer of the first `max_pages` pages (pypdfium2 under the PDFium
    lock). Unreadable files or pages give empty strings, never an exception."""
    import pypdfium2 as pdfium

    from app.services.pdf_split import _PDFIUM_LOCK

    texts: list[str] = []
    try:
        data = path.read_bytes()
    except OSError:
        return texts
    with _PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(data)
        except Exception:  # noqa: BLE001 - broken or encrypted PDF
            return texts
        try:
            for i in range(min(len(doc), max_pages)):
                try:
                    page = doc[i]
                    textpage = page.get_textpage()
                    texts.append(textpage.get_text_range() or "")
                    textpage.close()
                    page.close()
                except Exception:  # noqa: BLE001
                    texts.append("")
        finally:
            doc.close()
    return texts


def _cached_texts(job: tuple[str, str, int, str]) -> tuple[str, list[str]]:
    file_id, path, max_pages, cache_dir = job
    cache = Path(cache_dir) / f"{file_id}_{max_pages}.json"
    if cache.exists():
        try:
            return file_id, json.loads(cache.read_text(encoding="utf-8"))["pages"]
        except (OSError, ValueError, KeyError):
            pass
    pages = pdf_page_texts(Path(path), max_pages)
    try:
        cache.write_text(json.dumps({"file_id": file_id, "pages": pages}), encoding="utf-8")
    except OSError:
        pass
    return file_id, pages


# Population and documents -------------------------------------------------------------------


def _load_table(backup: Path, name: str) -> list[dict[str, Any]]:
    return json.loads((backup / "data" / f"{name}.json").read_text(encoding="utf-8"))


def wage_population(projects: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spec 8.7: wage_type set, minus ZZ or TEST names."""
    out = []
    for p in projects:
        name = str(p.get("name") or "").strip().upper()
        if p.get("wage_type") in (None, ""):
            continue
        if name.startswith("ZZ") or "TEST" in name:
            continue
        out.append(p)
    return out


def project_documents(
    backup: Path,
    project_ids: set[str],
    files: list[dict[str, Any]],
    cache_dir: Path,
    workers: int,
) -> dict[str, list[dict[str, Any]]]:
    """project id -> [{document_id, page (1-based), text, role}]."""
    jobs: list[tuple[str, str, int, str]] = []
    meta: dict[str, dict[str, Any]] = {}
    for f in files:
        if f["project_id"] not in project_ids:
            continue
        category = f.get("category")
        if not str(f.get("filename", "")).lower().endswith(".pdf"):
            continue
        if category in FULL_TEXT_CATEGORIES:
            max_pages = FULL_TEXT_MAX_PAGES
        elif category in DRAWING_CATEGORIES:
            max_pages = DRAWING_MAX_PAGES
        else:
            continue
        path = backup / "storage" / "project-files" / f["storage_path"]
        if not path.exists():
            continue
        meta[f["id"]] = f
        jobs.append((f["id"], str(path), max_pages, str(cache_dir)))
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[str]] = {}
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for file_id, pages in pool.map(_cached_texts, jobs, chunksize=2):
                results[file_id] = pages
    else:
        for job in jobs:
            file_id, pages = _cached_texts(job)
            results[file_id] = pages
    docs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for file_id, pages in results.items():
        f = meta[file_id]
        for i, text in enumerate(pages):
            if text and text.strip():
                docs[f["project_id"]].append(
                    {"document_id": file_id, "page": i + 1, "text": text, "role": f["category"]}
                )
    return docs


# Scoring ---------------------------------------------------------------------------------


def _variant_row(project: dict[str, Any], variant: str) -> dict[str, Any]:
    row = dict(project)
    if variant in ("wage_hidden", "owner_hidden"):
        row["wage_type"] = None
    if variant == "owner_hidden":
        row["owner_type"] = None
    return row


def _agreement(rows: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    scored = [r for r in rows if r["truth"] in WAGE_TYPES]
    correct = sum(r[variant]["wage_type"] == r["truth"] for r in scored)
    n = len(scored)
    return {"correct": correct, "n": n, "share": (correct / n) if n else None}


def _cue_precision(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [
        r for r in rows if r["truth"] in WAGE_TYPES and r["wage_hidden"]["wage_basis"] == "document_cue"
    ]
    correct = sum(r["wage_hidden"]["wage_type"] == r["truth"] for r in decided)
    pw_fired = [r for r in rows if r["truth"] in WAGE_TYPES and r["pw_score"] >= 2]
    return {
        "decided": len(decided),
        "correct": correct,
        "precision": (correct / len(decided)) if decided else None,
        "pw_verdicts": len(pw_fired),
        "pw_verdicts_on_non_pw": sum(r["truth"] != "prevailing_wage" for r in pw_fired),
    }


def _per_cue(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name in bid_conditions.CUES:
        hit = [r for r in rows if r["truth"] in WAGE_TYPES and name in r["cues"]]
        out[name] = {
            "projects": len(hit),
            "prevailing": sum(r["truth"] == "prevailing_wage" for r in hit),
            "non_prevailing": sum(r["truth"] == "non_prevailing_wage" for r in hit),
        }
    return out


def _confusion(rows: list[dict[str, Any]], variant: str) -> dict[str, int]:
    c: Counter[str] = Counter()
    for r in rows:
        if r["truth"] in WAGE_TYPES:
            c[f"{r['truth']} -> {r[variant]['wage_type']}"] += 1
    return dict(sorted(c.items()))


def run_eval(
    backup: Path = DEFAULT_BACKUP,
    eval_root: Path = DEFAULT_EVAL_ROOT,
    *,
    workers: int = 6,
) -> dict[str, Any]:
    projects = _load_table(backup, "projects")
    files = _load_table(backup, "project_files")
    population = wage_population(projects)
    holdout, holdout_source = holdout_projects()
    docs = project_documents(
        backup,
        {p["id"] for p in population},
        files,
        eval_root / "cache" / "ws5_pdf_text",
        workers,
    )
    rows: list[dict[str, Any]] = []
    for p in population:
        texts = docs.get(p["id"], [])
        row: dict[str, Any] = {
            "project_id": p["id"],
            "number": p.get("number"),
            "owner_type": p.get("owner_type"),
            "truth": p.get("wage_type") if p.get("wage_type") in WAGE_TYPES else None,
            "raw_wage_type": p.get("wage_type"),
            "holdout": p["id"] in holdout,
            "pages": len(texts),
        }
        for variant in VARIANTS:
            bc = bid_conditions.classify(_variant_row(p, variant), texts)
            row[variant] = {
                "wage_type": bc.wage_type,
                "wage_basis": bc.wage_basis,
                "needs_review": list(bc.needs_review),
                "labor_time": bc.labor_time,
            }
            if variant == "wage_hidden":
                row["pw_score"] = bc.pw_score
                row["cues"] = [c.cue for c in bc.cues]
        rows.append(row)
    columns = {
        "main": [r for r in rows if not r["holdout"]],
        "holdout": [r for r in rows if r["holdout"]],
        "all": rows,
    }
    metrics = {
        col: {
            "agreement": {v: _agreement(sub, v) for v in VARIANTS},
            "cue_precision": _cue_precision(sub),
            "confusion_wage_hidden": _confusion(sub, "wage_hidden"),
            "confusion_owner_hidden": _confusion(sub, "owner_hidden"),
        }
        for col, sub in columns.items()
    }
    return {
        "generated_at": datetime.now(PACIFIC).strftime("%Y-%m-%d %H:%M %Z"),
        "backup": str(backup),
        "holdout_source": holdout_source,
        "population": len(population),
        "unscored": [
            {"number": r["number"], "raw_wage_type": r["raw_wage_type"], "wage_hidden": r["wage_hidden"]}
            for r in rows
            if r["truth"] is None
        ],
        "metrics": metrics,
        "per_cue_main": _per_cue(columns["main"]),
        "misses_wage_hidden": [
            {
                "number": r["number"],
                "owner_type": r["owner_type"],
                "truth": r["truth"],
                "predicted": r["wage_hidden"]["wage_type"],
                "basis": r["wage_hidden"]["wage_basis"],
                "pw_score": r["pw_score"],
                "cues": r["cues"],
                "holdout": r["holdout"],
            }
            for r in rows
            if r["truth"] in WAGE_TYPES and r["wage_hidden"]["wage_type"] != r["truth"]
        ],
        "rows": rows,
    }


def _pct(stat: dict[str, Any]) -> str:
    share = stat.get("share")
    return f"{stat['correct']} of {stat['n']} ({share:.1%})" if share is not None else "n/a"


def render_markdown(result: dict[str, Any]) -> str:
    m = result["metrics"]
    out = [
        "# Bid conditions eval (WS5)",
        "",
        f"Generated {result['generated_at']} (Pacific). Backup `{result['backup']}`.",
        f"Wage population (spec 8.7): {result['population']} projects. Holdout source: "
        f"{result['holdout_source']}.",
        "",
        "## Wage agreement with projects.wage_type",
        "",
        "| Variant | Main (non-holdout) | Holdout | All |",
        "|---|---|---|---|",
    ]
    for v in VARIANTS:
        out.append(
            f"| {v} | {_pct(m['main']['agreement'][v])} | {_pct(m['holdout']['agreement'][v])} "
            f"| {_pct(m['all']['agreement'][v])} |"
        )
    out += ["", "## Cue precision (wage_hidden, wage decided by document cues)", ""]
    out += ["| Column | Decided by cues | Correct | Precision | PW verdicts on non-PW |"]
    out += ["|---|---|---|---|---|"]
    for col in ("main", "holdout", "all"):
        cp = m[col]["cue_precision"]
        prec = f"{cp['precision']:.1%}" if cp["precision"] is not None else "n/a"
        out.append(
            f"| {col} | {cp['decided']} | {cp['correct']} | {prec} | {cp['pw_verdicts_on_non_pw']} |"
        )
    out += ["", "## Per cue (main column)", "", "| Cue | Weight | Projects | PW | non-PW |"]
    out += ["|---|---|---|---|---|"]
    for name, stat in result["per_cue_main"].items():
        weight = bid_conditions.CUES[name][0]
        out.append(
            f"| {name} | {weight} | {stat['projects']} | {stat['prevailing']} | "
            f"{stat['non_prevailing']} |"
        )
    out += ["", "## Confusion (main column)", ""]
    out.append(f"- wage_hidden: {m['main']['confusion_wage_hidden']}")
    out.append(f"- owner_hidden: {m['main']['confusion_owner_hidden']}")
    out += ["", "## Misses (wage_hidden)", "", "| Project | Owner type | Truth | Predicted | Basis |"]
    out.append("|---|---|---|---|---|")
    for miss in result["misses_wage_hidden"]:
        tag = " (holdout)" if miss["holdout"] else ""
        out.append(
            f"| {miss['number']}{tag} | {miss['owner_type']} | {miss['truth']} | "
            f"{miss['predicted']} | {miss['basis']} (score {miss['pw_score']}) |"
        )
    out += ["", "## Not scored (free-text wage_type)", ""]
    for row in result["unscored"]:
        wh = row["wage_hidden"]
        out.append(
            f"- {row['number']}: '{row['raw_wage_type']}' -> {wh['wage_type']} "
            f"({wh['wage_basis']}; needs_review {wh['needs_review']})"
        )
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bid-conditions", action="store_true", help="run the wage eval (default)")
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument("--eval-root", type=Path, default=DEFAULT_EVAL_ROOT)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--min-agreement", type=float, default=MIN_AGREEMENT)
    args = parser.parse_args(argv)
    if not (args.backup / "data" / "projects.json").exists():
        print(f"backup not found at {args.backup}", file=sys.stderr)
        return 2
    result = run_eval(args.backup, args.eval_root, workers=args.workers)
    reports = args.eval_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    md_path = reports / "bid_conditions_eval.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    (reports / "bid_conditions_eval.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    main_metrics = result["metrics"]["main"]
    agreement = main_metrics["agreement"]["wage_hidden"]
    precision = main_metrics["cue_precision"]["precision"]
    print(f"wage_hidden agreement (main): {_pct(agreement)}")
    print(f"owner_hidden agreement (main): {_pct(main_metrics['agreement']['owner_hidden'])}")
    print(f"cue precision (main): {precision}")
    print(f"wrote {md_path}")
    ok = (agreement["share"] or 0) >= args.min_agreement and precision in (None, 1.0)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
