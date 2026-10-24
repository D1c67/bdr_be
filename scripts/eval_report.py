"""Markdown reports for the AI Estimating eval harness
(docs/AI_ESTIMATING_BUILD_SPEC.md WS0 step 14; docs/AI_ESTIMATING_EVAL.md).

Usage (from bdr_be):

    uv run python scripts/eval_report.py baseline [--bundle <weekend bundle>] [--out EVAL_ROOT/reports/baseline_7154.md]
    uv run python scripts/eval_report.py sweep --sweep-dir EVAL_ROOT/sweeps/<id> [--out <path>] [--dollars]
    uv run python scripts/eval_report.py rescore --bundles EVAL_ROOT/bundles [--out <path>]

`baseline` publishes the $0 baseline table of step 14: every 3.5 row with
its status on the stored weekend bundle, the measured values and the
Baseline column, written to EVAL_ROOT/reports/baseline_7154.md (outside git;
it may hold dollar-derived ratios but prints no absolute dollars unless
--dollars). `sweep` renders a sweep's results.json (per case: status,
acceptance table, score ratios, ledger spend). `rescore` re-scores every
bundle under EVAL_ROOT/bundles with the current MATCHER_VERSION and writes
a diff against each bundle's stored score when one exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import eval_bundle, eval_manifest  # noqa: E402
from app.services import eval_reference as R  # noqa: E402
from app.services import plans_reader_acceptance as A  # noqa: E402
from app.services import plans_reader_replay as replay_mod  # noqa: E402
from app.services import plans_reader_score as S  # noqa: E402
from app.services.estimating_contracts import CHECK_SPECS, FIXED_CHECK_IDS  # noqa: E402

WEEKEND = "bundles/7154/ff8fad25"
# The Baseline column of 3.5, verbatim, for the report.
BASELINE_COLUMN: dict[str, str] = {cid: A.BASELINE_7154[cid]["note"] for cid in FIXED_CHECK_IDS}


def _cell(text: Any, limit: int = 110) -> str:
    s = str(text).replace("|", "/").replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _measured_summary(row: dict[str, Any]) -> str:
    bits = []
    for name, part in (row.get("measured") or {}).items():
        if not isinstance(part, dict):
            continue
        vals = {k: v for k, v in part.items() if k != "status" and not isinstance(v, (list, dict))}
        if vals:
            bits.append(f"{name}: " + ", ".join(f"{k}={v}" for k, v in list(vals.items())[:5]))
    return "; ".join(bits)


def baseline_markdown(report: A.SelfTestReport, replay_result: Any, *, bundle: eval_bundle.RunBundle) -> str:
    lines = [
        "# AI Estimating $0 baseline: the stored 7154 weekend bundle",
        "",
        f"Bundle `{bundle.root}` (run {bundle.run_id}, source {bundle.source}). Generated {eval_bundle.now_pacific_iso()} (Pacific) by "
        "`scripts/eval_report.py baseline` (spec WS0 step 14). Every row of spec 3.5 measured by its detector on the stored "
        f"weekend outputs replayed through the legacy stages (MATCHER_VERSION {S.MATCHER_VERSION}). This is the 'before' table every "
        "workstream reports against. Statuses: pass, fail, skip, safe_refusal, error. Measured values are counts and ratios.",
        "",
        "| Check | Baseline (3.5) | Status | Matches baseline | Gates 7154 | Replay | Measured | Evidence |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in report.rows:
        spec = CHECK_SPECS[r.id]
        lines.append(
            f"| {r.id} | {_cell(BASELINE_COLUMN.get(r.id, ''))} | {r.status} | {r.baseline_matches} | "
            f"{'yes' if spec.gates_7154_smoke else 'no'} | {spec.replay} | "
            f"{_cell(_measured_summary({'measured': r.measured}), 220)} | {_cell('; '.join(r.evidence), 160)} |"
        )
    lines += ["", "## Replay stage table", "", "| Stage | Mode | Version | Owner | Written | Seconds |", "|---|---|---|---|---|---|"]
    for s in replay_result.stages:
        lines.append(f"| {s.name} | {s.mode} | {s.version} | {s.owner} | {', '.join(s.written)} | {s.seconds} |")
    if replay_result.notes:
        lines += ["", "Replay notes:", ""] + [f"- {n}" for n in replay_result.notes]
    skipped = report.skipped()
    lines += ["", "## Rows that skipped", ""]
    if skipped:
        lines += [f"- {cid}: {'; '.join(ev)}" for cid, ev in skipped.items()]
    else:
        lines.append("- none")
    raw = (getattr(replay_result, "artifacts", None) or {}).get("score_raw") or {}
    lines += ["", "## Notes", "",
              f"- Dollar coverage on the weekend bundle (raw truth): 3.6 in band {raw.get('dollar_coverage', 'n/a')}, "
              f"partial credit {raw.get('dollar_coverage_partial', 'n/a')}. E25 (0.319 +/- 0.005) reads the partial-credit figure, "
              "the 1.3 smoke gate (>= 0.85) reads the in-band figure.",
              "- E25 is the one row whose baseline FAIL is under the v1 scorer; scorer v2 (the fix) passes on the same bundle. "
              "tests/test_eval_golden_7154.py::test_v1_scorer_baseline_is_the_e25_failure pins the v1 failure.",
              "- E17 reports two distances per mark: to the nearest anchor of the mark's tag (the 3.5 criterion) and to the nearest "
              "device anchor of any family (the weekend method whose numbers the Baseline column quotes).",
              "- Checks that read artifacts no stage produces yet (planned requests, census, gate report, priced estimate) skip those "
              "parts with a reason; the parts that could be measured decide the row.",
              ""]
    return "\n".join(lines)


def sweep_markdown(results: list[dict[str, Any]], *, dollars: bool = False) -> str:
    lines = ["# Eval sweep results", "", "| Case | Run | Status | Count lines exact | Lengths produced | Dollar coverage (3.6 in band) | Dollar coverage (partial) | Bid vs sent | Checks failing |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        score = r.get("score") or {}
        bid = (score.get("bid") or {}).get("vs_sent") or {}
        failing = [a["id"] for a in (r.get("acceptance") or []) if a.get("status") in ("fail", "error")]
        lines.append(
            f"| {r.get('case_id')} | {str(r.get('run_id') or '')[:8]} | {r.get('status')} | "
            f"{score.get('count_lines_exact', 'n/a')} of {score.get('count_lines_total', 'n/a')} | "
            f"{score.get('length_lines_produced', 'n/a')} of {score.get('length_lines_total', 'n/a')} | "
            f"{score.get('dollar_coverage', 'n/a')} | {score.get('dollar_coverage_partial', 'n/a')} | {bid.get('rel', 'n/a')} ({bid.get('band', 'n/a')}) | {', '.join(failing) or 'none'} |"
        )
    for r in results:
        if r.get("acceptance"):
            lines += ["", f"## {r.get('case_id')} acceptance", "", "| Check | Status | Parts | Evidence |", "|---|---|---|---|"]
            for a in r["acceptance"]:
                parts = ", ".join(f"{k}={v}" for k, v in (a.get("parts") or {}).items())
                lines.append(f"| {a['id']} | {a['status']} | {_cell(parts)} | {_cell('; '.join(a.get('evidence') or []))} |")
    if not dollars:
        lines += ["", "Absolute dollar figures are omitted (pass --dollars locally)."]
    return "\n".join(lines) + "\n"


def rescore_markdown(bundles_root: Path, eval_root: Path, backup: Path) -> str:
    lines = [f"# Re-score with MATCHER_VERSION {S.MATCHER_VERSION}", "", "| Bundle | Case | Exact | Total | Coverage (3.6 in band) | Coverage (partial) | Stored exact | Stored coverage | Delta |",
             "|---|---|---|---|---|---|---|---|---|"]
    for case_dir in sorted(p for p in bundles_root.iterdir() if p.is_dir()):
        for run_dir in sorted(p for p in case_dir.iterdir() if p.is_dir()):
            if not (run_dir / "run.json").is_file():
                continue
            bundle = eval_bundle.load_bundle(run_dir)
            case_id = bundle.case_id or case_dir.name
            try:
                reference = R.load_reference(eval_root, case_id)
            except FileNotFoundError:
                lines.append(f"| {run_dir.name} | {case_id} | n/a | n/a | n/a | n/a | | | no reference |")
                continue
            result = replay_mod.replay(bundle, reference=reference, case_id=case_id, from_stage="score", out_dir=run_dir / "replays" / "rescore", backup_dir=backup)
            score = result.artifacts.get("score") or {}
            stored_path = run_dir / "artifacts" / "score.json"
            stored = json.loads(stored_path.read_text()) if stored_path.is_file() else {}
            delta = ""
            if stored:
                delta = f"exact {score.get('count_lines_exact', 0) - stored.get('count_lines_exact', 0):+d}"
            lines.append(
                f"| {case_dir.name}/{run_dir.name} | {case_id} | {score.get('count_lines_exact', 'n/a')} | {score.get('count_lines_total', 'n/a')} | "
                f"{score.get('dollar_coverage', 'n/a')} | {score.get('dollar_coverage_partial', 'n/a')} | {stored.get('count_lines_exact', '')} | {stored.get('dollar_coverage', '')} | {delta} |"
            )
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AI Estimating eval reports (markdown).")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("baseline", help="the $0 baseline table on the weekend bundle (spec WS0 step 14)")
    b.add_argument("--bundle", type=Path, default=None)
    b.add_argument("--eval-root", type=Path, default=None)
    b.add_argument("--backup", type=Path, default=eval_bundle.DEFAULT_BACKUP_DIR)
    b.add_argument("--out", type=Path, default=None)
    b.add_argument("--json", type=Path, default=None)
    s = sub.add_parser("sweep", help="render a sweep's results.json")
    s.add_argument("--sweep-dir", type=Path, required=True)
    s.add_argument("--out", type=Path, default=None)
    s.add_argument("--dollars", action="store_true")
    r = sub.add_parser("rescore", help="re-score every bundle with the current matcher")
    r.add_argument("--bundles", type=Path, default=None)
    r.add_argument("--eval-root", type=Path, default=None)
    r.add_argument("--backup", type=Path, default=eval_bundle.DEFAULT_BACKUP_DIR)
    r.add_argument("--out", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "baseline":
        root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
        bundle_dir = args.bundle or root / WEEKEND
        out = args.out or root / "reports" / "baseline_7154.md"
        report = A.self_test(bundle_dir, eval_root=root, backup_dir=args.backup, out_dir=root / "replays" / "7154" / "baseline")
        bundle = eval_bundle.load_bundle(bundle_dir)
        reference = R.load_reference(root, bundle.case_id or "7154")
        expected_path = root / "references" / (bundle.case_id or "7154") / "expected.json"
        expected = json.loads(expected_path.read_text()) if expected_path.is_file() else None
        replay_result = replay_mod.replay(bundle, reference=reference, expected=expected, case_id=bundle.case_id or "7154",
                                          out_dir=root / "replays" / "7154" / "baseline", backup_dir=args.backup)
        text = baseline_markdown(report, replay_result, bundle=bundle)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        if args.json:
            args.json.write_text(json.dumps(report.to_dict(), indent=1, default=str))
        print(text)
        print(f"written {out}")
        return 0 if report.ok else 1
    if args.command == "sweep":
        results = json.loads((args.sweep_dir / "results.json").read_text())
        text = sweep_markdown(results, dollars=args.dollars)
        out = args.out or args.sweep_dir / "report.md"
        out.write_text(text)
        print(text)
        return 0
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    bundles = args.bundles or root / "bundles"
    text = rescore_markdown(bundles, root, args.backup)
    out = args.out or root / "reports" / f"rescore_v{S.MATCHER_VERSION}.md"
    out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
