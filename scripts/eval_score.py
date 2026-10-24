"""Score a run bundle or a deliverable BOQ against a case reference (WS0,
scorer v2, MATCHER_VERSION 2).

Usage (from bdr_be):
  uv run python scripts/eval_score.py --case 7154 --bundle EVAL_ROOT/bundles/7154/ff8fad25
  uv run python scripts/eval_score.py --case 7154 --boq path/to/boq_spec.json
  uv run python scripts/eval_score.py --case 7154 --boq "path/to/BOQ - COUNTS.xlsx"
  options: --raw (raw estimator truth instead of adjudicated), --priced priced_estimate.json,
           --json out.json, --md out.md, --dollars (print absolute dollar figures; local only)

A bundle is scored on its highest BOQ deliverable version
(deliverables/boq_v<n>.json) and, when present, artifacts/priced_estimate.json.
Reports print ratios; absolute dollars only with --dollars and never into
the repo.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_BE_ROOT = Path(__file__).resolve().parents[1]
if str(_BE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BE_ROOT))

from app.services import eval_manifest as M  # noqa: E402
from app.services import eval_reference as R  # noqa: E402
from app.services import plans_reader_score as S  # noqa: E402

_BOQ_VERSION_RE = re.compile(r"^boq_v(\d+)\.json$")


def bundle_boq_spec(bundle: Path) -> tuple[Path, dict]:
    """The highest boq_v<n>.json under <bundle>/deliverables."""
    deliverables = bundle / "deliverables"
    best: tuple[int, Path] | None = None
    for p in deliverables.glob("boq_v*.json"):
        m = _BOQ_VERSION_RE.match(p.name)
        if m and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), p)
    if best is None:
        raise FileNotFoundError(f"no deliverables/boq_v<n>.json under {bundle}")
    return best[1], json.loads(best[1].read_text())


def rows_from_path(path: Path) -> list[S.BoqRow]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return S.parse_estimator_boq(path.read_bytes())
    return S.rows_from_spec(json.loads(path.read_text()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a bundle or a BOQ against a case reference.")
    ap.add_argument("--case", required=True, help="case id (7154) or a project id prefix")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bundle", type=Path, help="run bundle directory")
    src.add_argument("--boq", type=Path, help="BOQ spec JSON or a rendered BOQ xlsx")
    ap.add_argument("--priced", type=Path, help="a bdr.priced_estimate/1 JSON for the bid metrics")
    ap.add_argument("--reference", type=Path, help="reference.json (defaults to EVAL_ROOT/references/<case>)")
    ap.add_argument("--eval-root", type=Path, default=None)
    ap.add_argument("--raw", action="store_true", help="score against the raw estimator truth")
    ap.add_argument("--json", type=Path, help="write the score JSON here")
    ap.add_argument("--md", type=Path, help="write the markdown report here")
    ap.add_argument("--dollars", action="store_true", help="include absolute dollar figures")
    args = ap.parse_args(argv)

    root = Path(args.eval_root) if args.eval_root else M.eval_root()
    case_id = args.case
    if case_id not in M.CASE_PREFIX:
        try:
            case_id = M.PREFIX_CASE[next(p for p in M.PREFIX_CASE if p.startswith(case_id[:8]))]
        except StopIteration:
            pass
    if args.reference:
        ref = json.loads(args.reference.read_text())
    else:
        ref = R.load_reference(root, case_id)

    priced = None
    if args.bundle:
        boq_path, spec = bundle_boq_spec(args.bundle)
        rows = S.rows_from_spec(spec)
        priced_path = args.bundle / "artifacts" / "priced_estimate.json"
        if priced_path.exists():
            priced = json.loads(priced_path.read_text())
        source = str(boq_path)
    else:
        rows = rows_from_path(args.boq)
        source = str(args.boq)
    if args.priced:
        priced = json.loads(args.priced.read_text())

    score = S.score_case(rows, ref, adjudicated=not args.raw, priced=priced, case_id=case_id)
    md = S.score_markdown(score, include_dollars=args.dollars)
    print(f"source: {source}")
    print(md)
    if args.json:
        args.json.write_text(json.dumps(S.score_json(score, include_dollars=args.dollars), indent=2) + "\n")
    if args.md:
        args.md.write_text(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
