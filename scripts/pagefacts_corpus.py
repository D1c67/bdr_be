"""Page facts corpus and blind fixture measurements (WS1 step 12, $0).

Two measurements over the backup PDFs, no model, no database:

- corpus (default): the extractor over the first 3 drawing PDFs of each of
  the 66 golden projects (electrical drawings first, then other drawings,
  by file name), at most 40 pages each, with the production time budgets.
  Writes `EVAL_ROOT/reports/pagefacts_corpus.md` (and `.json`): per-page
  timing p50 and p90, crashes (an exception out of the extractor) and
  unreadable pages, the share of drawing pages with a sheet number, the
  text classes and the share of no-text pages per project.
- `--blind`: the extractor over the pages of the blind fixture
  `tests/fixtures/pagefacts/blind_sheet_numbers.json` (labels written by
  eye from page renders BEFORE the extractor ran on those pages; the label
  file hash is checked first) and the precision and recall of the sheet
  numbers and the title match share against the labels. Writes
  `EVAL_ROOT/reports/pagefacts_blind.md` (and `.json`).

    cd bdr_be && uv run python scripts/pagefacts_corpus.py [--blind] [--limit-projects N]

Targets (spec 6 WS1 acceptance): corpus p90 <= 0.5 s per page and zero
crashes; on electrical text pages of the blind fixture a sheet number on
>= 80% with precision >= 98%, and the title exact or contained >= 90%.
The reports hold no dollar figures.
"""

from __future__ import annotations

import argparse
import hashlib
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

from app.core.config import get_settings  # noqa: E402
from app.services import pdf_split  # noqa: E402
from app.services.eval_reference import DEFAULT_BACKUP_DIR, BackupData  # noqa: E402
from app.services.pagefacts import extract as extract_mod  # noqa: E402

EVAL_ROOT = Path(get_settings().eval_root)
FIXTURE = ROOT / "tests" / "fixtures" / "pagefacts" / "blind_sheet_numbers.json"
MAX_DOCS_PER_PROJECT = 3
MAX_PAGES = 40
DRAWING_CATEGORIES = ("electrical_drawing", "drawing")
LABEL_FOR_CATEGORY = {"electrical_drawing": "electrical_drawings", "drawing": "other_drawings"}
TARGET_P90_S = 0.5
TARGET_SHEET_COVERAGE = 0.80
TARGET_SHEET_PRECISION = 0.98
TARGET_TITLE_SHARE = 0.90


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _pdf_bytes(path: Path, max_pages: int) -> tuple[bytes, int]:
    data = path.read_bytes()
    total = pdf_split.page_count(data)
    if total > max_pages:
        data = pdf_split.extract_range(data, 1, max_pages)
    return data, total


def corpus_documents(data: BackupData, project_id: str) -> list[dict[str, Any]]:
    """The first 3 drawing PDFs of a project: electrical drawings first,
    then other drawings, each by file name."""
    rank = {c: i for i, c in enumerate(DRAWING_CATEGORIES)}
    rows = [
        r for r in data.files(project_id)
        if r.get("category") in rank and str(r.get("filename") or "").lower().endswith(".pdf")
    ]
    rows.sort(key=lambda r: (rank[r["category"]], str(r.get("filename") or "").lower(), str(r.get("id"))))
    return rows[:MAX_DOCS_PER_PROJECT]


def run_corpus(*, limit_projects: int | None = None) -> dict[str, Any]:
    manifest = json.loads((EVAL_ROOT / "manifest" / "manifest.json").read_text())
    data = BackupData(DEFAULT_BACKUP_DIR)
    projects = list(manifest["golden_ids"])[: limit_projects or None]
    page_times: list[float] = []
    per_project: list[dict[str, Any]] = []
    crashes: list[dict[str, Any]] = []
    totals = Counter()
    started = time.monotonic()
    for pid in projects:
        proj = data.project(pid) or {}
        row_out = {"project8": pid[:8], "number": proj.get("number"), "documents": 0, "pages": 0, "no_text_pages": 0,
                   "drawing_text_pages": 0, "with_sheet_number": 0, "text_classes": Counter(), "missing_files": 0}
        for f in corpus_documents(data, pid):
            path = data.file_path(f)
            if not path.exists():
                row_out["missing_files"] += 1
                continue
            try:
                pdf, total = _pdf_bytes(path, MAX_PAGES)
                facts = extract_mod.extract_document(
                    pdf, label=LABEL_FOR_CATEGORY[f["category"]], name=f.get("filename"),
                    page_budget_s=4.0, doc_budget_s=240.0,
                )
            except Exception as exc:  # noqa: BLE001 - a crash is the measured value
                crashes.append({"project8": pid[:8], "file8": str(f.get("id"))[:8], "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                continue
            row_out["documents"] += 1
            for pg in facts.pages:
                row_out["pages"] += 1
                row_out["text_classes"][pg.text_class] += 1
                if pg.text_layer_quality == "none":
                    row_out["no_text_pages"] += 1
                if pg.text_layer_quality == "full":
                    row_out["drawing_text_pages"] += 1
                    if pg.sheet_number:
                        row_out["with_sheet_number"] += 1
                if pg.text_class not in ("skipped_budget",) and pg.timing_ms:
                    page_times.append(pg.timing_ms / 1000.0)
            totals["unreadable_pages"] += sum(1 for p in facts.problems if "text layer unreadable" in p)
            totals["budget_hit_docs"] += int(bool((facts.stats or {}).get("budget_hit")))
        row_out["text_classes"] = dict(row_out["text_classes"])
        row_out["no_text_share"] = round(row_out["no_text_pages"] / row_out["pages"], 4) if row_out["pages"] else None
        row_out["sheet_number_share"] = (
            round(row_out["with_sheet_number"] / row_out["drawing_text_pages"], 4) if row_out["drawing_text_pages"] else None
        )
        per_project.append(row_out)
    pages = sum(r["pages"] for r in per_project)
    text_pages = sum(r["drawing_text_pages"] for r in per_project)
    with_number = sum(r["with_sheet_number"] for r in per_project)
    return {
        "measured_at": time.strftime("%Y-%m-%d %H:%M"),
        "extractor_version": extract_mod.EXTRACTOR_VERSION,
        "projects": len(projects),
        "projects_with_pages": sum(1 for r in per_project if r["pages"]),
        "documents": sum(r["documents"] for r in per_project),
        "pages": pages,
        "page_s_p50": round(statistics.median(page_times), 4) if page_times else None,
        "page_s_p90": round(_p(page_times, 0.9), 4),
        "page_s_max": round(max(page_times), 4) if page_times else None,
        "crashes": crashes,
        "unreadable_pages": totals["unreadable_pages"],
        "budget_hit_documents": totals["budget_hit_docs"],
        "text_pages": text_pages,
        "sheet_number_share_text_pages": round(with_number / text_pages, 4) if text_pages else None,
        "no_text_share_all": round(sum(r["no_text_pages"] for r in per_project) / pages, 4) if pages else None,
        "wall_s": round(time.monotonic() - started, 1),
        "per_project": per_project,
    }


# Blind fixture ---------------------------------------------------------------------


def norm_sheet(text: Any) -> str:
    """Case-insensitive, whitespace-free sheet number (punctuation kept:
    E-0.1 and E0.1 are different sheet numbers)."""
    return re.sub(r"\s+", "", str(text or "")).upper()


def norm_title(text: Any) -> str:
    return " ".join(re.sub(r"[^A-Z0-9&]+", " ", str(text or "").upper()).split())


def title_match(label: str, got: str | None) -> bool:
    """Exact or contained after normalization (either way round), the
    shorter side at least 3 characters."""
    a, b = norm_title(label), norm_title(got)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and short in long_


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    fx = json.loads(path.read_text())
    pages_sha = hashlib.sha256(json.dumps(fx["pages"], sort_keys=True).encode()).hexdigest()
    if pages_sha != fx["pages_sha256"]:
        raise SystemExit("blind fixture pages do not match their recorded sha256")
    label_file = Path(fx["label_file"])
    if label_file.exists() and hashlib.sha256(label_file.read_bytes()).hexdigest() != fx["label_sha256"]:
        raise SystemExit("blind label file does not match the recorded label_sha256")
    return fx


def run_blind(path: Path = FIXTURE, backup_dir: Path | str = DEFAULT_BACKUP_DIR) -> dict[str, Any]:
    fx = load_fixture(path)
    files_root = Path(backup_dir) / "storage" / "project-files"
    labels = {(p["doc"], int(p["page"])): p for p in fx["pages"]}
    rows: list[dict[str, Any]] = []
    for d in fx["documents"]:
        src = files_root / d["backup_path"]
        data = src.read_bytes()
        if hashlib.sha256(data).hexdigest() != d["sha256"]:
            raise SystemExit(f"{d['doc']}: backup file sha256 changed")
        want = sorted(p for (doc, p) in labels if doc == d["doc"])
        pdf = pdf_split.extract_range(data, 1, max(want)) if pdf_split.page_count(data) > max(want) else data
        facts = extract_mod.extract_document(pdf, label="electrical_drawings", name=d["filename"], page_budget_s=4.0, doc_budget_s=240.0)
        for p in want:
            pf = facts.page(p)
            lab = labels[(d["doc"], p)]
            got_num = pf.sheet_number if pf else None
            rows.append({
                "doc": d["doc"], "project": d["project_number"], "page": p,
                "text_layer_quality": pf.text_layer_quality if pf else "none",
                "label_sheet": lab["sheet_number"], "got_sheet": got_num,
                "sheet_ok": bool(got_num) and norm_sheet(got_num) == norm_sheet(lab["sheet_number"]),
                "label_title": lab["title"], "got_title": pf.sheet_title if pf else None,
                "title_source": pf.title_source if pf else None,
                "title_ok": title_match(lab["title"], pf.sheet_title if pf else None),
            })
    text = [r for r in rows if r["text_layer_quality"] == "full"]
    answered = [r for r in text if r["got_sheet"]]
    correct = [r for r in answered if r["sheet_ok"]]
    out = {
        "measured_at": time.strftime("%Y-%m-%d %H:%M"),
        "extractor_version": extract_mod.EXTRACTOR_VERSION,
        "label_sha256": fx["label_sha256"],
        "pages": len(rows),
        "documents": len(fx["documents"]),
        "projects": len({d["project_number"] for d in fx["documents"]}),
        "text_pages": len(text),
        "non_text_pages": len(rows) - len(text),
        "sheet_answered": len(answered),
        "sheet_correct": len(correct),
        "sheet_coverage": round(len(answered) / len(text), 4) if text else None,
        "sheet_precision": round(len(correct) / len(answered), 4) if answered else None,
        "sheet_recall": round(len(correct) / len(text), 4) if text else None,
        "title_ok": sum(1 for r in text if r["title_ok"]),
        "title_share": round(sum(1 for r in text if r["title_ok"]) / len(text), 4) if text else None,
        "per_document": {},
        "misses": [r for r in text if not r["sheet_ok"] or not r["title_ok"]],
    }
    for d in fx["documents"]:
        mine = [r for r in text if r["doc"] == d["doc"]]
        out["per_document"][d["doc"]] = {
            "project": d["project_number"], "text_pages": len(mine),
            "sheet_correct": sum(1 for r in mine if r["sheet_ok"]),
            "sheet_answered": sum(1 for r in mine if r["got_sheet"]),
            "title_ok": sum(1 for r in mine if r["title_ok"]),
        }
    out["targets"] = {
        "sheet_coverage": [TARGET_SHEET_COVERAGE, out["sheet_coverage"] is not None and out["sheet_coverage"] >= TARGET_SHEET_COVERAGE],
        "sheet_precision": [TARGET_SHEET_PRECISION, out["sheet_precision"] is not None and out["sheet_precision"] >= TARGET_SHEET_PRECISION],
        "title_share": [TARGET_TITLE_SHARE, out["title_share"] is not None and out["title_share"] >= TARGET_TITLE_SHARE],
    }
    return out


# Reports ----------------------------------------------------------------------------


def corpus_markdown(res: dict[str, Any]) -> str:
    lines = [
        "# Page facts corpus (WS1 step 12)",
        "",
        f"Measured {res['measured_at']} (extractor {res['extractor_version']}). First {MAX_DOCS_PER_PROJECT} drawing PDFs"
        f" of each golden project (electrical drawings first), at most {MAX_PAGES} pages each, production time budgets.",
        "",
        f"- projects {res['projects']} ({res['projects_with_pages']} with drawing pages), documents {res['documents']}, pages {res['pages']}",
        f"- page time p50 {res['page_s_p50']} s, p90 {res['page_s_p90']} s (target <= {TARGET_P90_S}), max {res['page_s_max']} s",
        f"- crashes {len(res['crashes'])} (target 0), unreadable pages {res['unreadable_pages']}, documents over the time budget {res['budget_hit_documents']}",
        f"- sheet number on {res['sheet_number_share_text_pages']} of {res['text_pages']} text pages; no-text share of all pages {res['no_text_share_all']}",
        f"- wall time {res['wall_s']} s",
        "",
        "| project | number | docs | pages | text pages | with sheet number | sheet share | no-text share | text classes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in res["per_project"]:
        classes = ", ".join(f"{k} {v}" for k, v in sorted(r["text_classes"].items()))
        lines.append(
            f"| {r['project8']} | {r['number'] or ''} | {r['documents']} | {r['pages']} | {r['drawing_text_pages']} | {r['with_sheet_number']} |"
            f" {r['sheet_number_share'] if r['sheet_number_share'] is not None else ''} | {r['no_text_share'] if r['no_text_share'] is not None else ''} | {classes} |"
        )
    if res["crashes"]:
        lines += ["", "## Crashes", ""] + [f"- {c['project8']} {c['file8']}: {c['error']}" for c in res["crashes"]]
    return "\n".join(lines) + "\n"


def blind_markdown(res: dict[str, Any]) -> str:
    t = res["targets"]
    lines = [
        "# Page facts blind fixture (WS1 step 12)",
        "",
        f"Measured {res['measured_at']} (extractor {res['extractor_version']}) against the labels of"
        f" tests/fixtures/pagefacts/blind_sheet_numbers.json (label sha256 {res['label_sha256'][:16]}),"
        f" {res['pages']} pages from {res['documents']} documents across {res['projects']} projects.",
        "",
        f"- electrical text pages {res['text_pages']} (others {res['non_text_pages']})",
        f"- sheet number answered on {res['sheet_answered']} ({res['sheet_coverage']}; target >= {t['sheet_coverage'][0]}: {'met' if t['sheet_coverage'][1] else 'MISSED'})",
        f"- sheet number precision {res['sheet_correct']} of {res['sheet_answered']} = {res['sheet_precision']} (target >= {t['sheet_precision'][0]}: {'met' if t['sheet_precision'][1] else 'MISSED'}); recall {res['sheet_recall']}",
        f"- title exact or contained {res['title_ok']} of {res['text_pages']} = {res['title_share']} (target >= {t['title_share'][0]}: {'met' if t['title_share'][1] else 'MISSED'})",
        "",
        "| doc | project | text pages | sheet answered | sheet correct | title ok |",
        "|---|---|---|---|---|---|",
    ]
    for doc, r in res["per_document"].items():
        lines.append(f"| {doc} | {r['project']} | {r['text_pages']} | {r['sheet_answered']} | {r['sheet_correct']} | {r['title_ok']} |")
    if res["misses"]:
        lines += ["", "## Misses (label vs extractor)", "", "| doc | page | label sheet | got sheet | label title | got title (source) |", "|---|---|---|---|---|---|"]
        for m in res["misses"]:
            lines.append(
                f"| {m['doc']} | {m['page']} | {m['label_sheet']} | {m['got_sheet'] or ''} | {m['label_title']} | {(m['got_title'] or '').replace('|', '/')} ({m['title_source']}) |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--blind", action="store_true", help="measure the blind fixture instead of the corpus")
    ap.add_argument("--limit-projects", type=int, default=None)
    args = ap.parse_args(argv)
    reports = EVAL_ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if args.blind:
        res = run_blind()
        (reports / "pagefacts_blind.json").write_text(json.dumps(res, indent=1, default=str))
        (reports / "pagefacts_blind.md").write_text(blind_markdown(res))
        print(json.dumps({k: res[k] for k in ("pages", "text_pages", "sheet_coverage", "sheet_precision", "sheet_recall", "title_share")}))
        return 0
    res = run_corpus(limit_projects=args.limit_projects)
    (reports / "pagefacts_corpus.json").write_text(json.dumps(res, indent=1, default=str))
    (reports / "pagefacts_corpus.md").write_text(corpus_markdown(res))
    print(json.dumps({k: res[k] for k in ("projects", "documents", "pages", "page_s_p50", "page_s_p90", "unreadable_pages", "sheet_number_share_text_pages", "no_text_share_all", "wall_s")} | {"crashes": len(res["crashes"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
