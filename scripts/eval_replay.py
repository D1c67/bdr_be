"""Replay a stored run bundle through the pipeline stages at $0
(docs/AI_ESTIMATING_BUILD_SPEC.md WS0 step 8; app/services/plans_reader_replay.py).

Usage (from bdr_be):

    uv run python scripts/eval_replay.py --bundle EVAL_ROOT/bundles/7154/ff8fad25 [--case 7154] \\
        [--from-stage plan] [--to-stage score] [--stage consolidate=stored] [--out <dir>] [--render-marked] [--dollars]

Stages run in STAGE_ORDER (plan, merge, recount, derive, consolidate, enforce,
gate, evidence, render, price, review, score). `--stage name=mode` pins one
stage to `rerun`, `stored` or `skip`. The replay refuses every provider call
(a $0 replay can never spend) and writes replay.json plus artifacts/ under
--out (default EVAL_ROOT/replays/<case>/<run id8>_<timestamp>/). Reports
print ratios; absolute dollars only with --dollars and never into the repo.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import eval_bundle, eval_manifest  # noqa: E402
from app.services import eval_reference as R  # noqa: E402
from app.services import plans_reader_replay as replay_mod  # noqa: E402
from app.services import plans_reader_score as S  # noqa: E402



def _register_real_stages(args=None) -> None:
    """Every implemented replay stage (spec 4.1 spine): the import-time ones
    through plans_reader_replay.register_real_stages, plus the explicit ones
    (page facts plan, WS5 scope derive and enforce, WS8 evidence)."""
    replay_mod.register_real_stages()
    for mod, fn in (
        ("app.services.pagefacts.replay", "register"),
        ("app.services.scope_engine", "register_replay_stages"),
        ("app.services.plans_reader_evidence", "register_replay_stage"),
    ):
        try:
            import importlib

            getattr(importlib.import_module(mod), fn)()
        except Exception as exc:  # noqa: BLE001
            print(f"replay stage from {mod} not registered: {exc}")

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Replay a run bundle through the code stages ($0).")
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--case", default=None, help="case id (defaults to the bundle's)")
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--backup", type=Path, default=eval_bundle.DEFAULT_BACKUP_DIR)
    p.add_argument("--from-stage", default="plan", choices=replay_mod.STAGE_ORDER)
    p.add_argument("--to-stage", default="score", choices=replay_mod.STAGE_ORDER)
    p.add_argument("--stage", action="append", default=[], help="name=mode (rerun|stored|skip)")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--render-marked", action="store_true", help="also render the marked plans PDF (needs the PDFs)")
    p.add_argument("--dollars", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    bundle = eval_bundle.load_bundle(args.bundle)
    case_id = args.case or bundle.case_id
    reference = expected = None
    if case_id:
        try:
            reference = R.load_reference(root, case_id)
        except FileNotFoundError:
            print(f"no reference for case {case_id}; the score stage is skipped")
        expected_path = root / "references" / case_id / "expected.json"
        if expected_path.is_file():
            expected = json.loads(expected_path.read_text())
    stages = {}
    for item in args.stage:
        name, _, mode = item.partition("=")
        stages[name.strip()] = mode.strip() or "rerun"
    out = args.out or root / "replays" / (case_id or "unknown") / f"{bundle.run_id8}_{int(time.time())}"
    _register_real_stages(args)
    result = replay_mod.replay(bundle, reference=reference, expected=expected, case_id=case_id, from_stage=args.from_stage,
                               to_stage=args.to_stage, stages=stages, out_dir=out, backup_dir=args.backup,
                               render_marked=args.render_marked)
    result.write()
    print(f"replay of {bundle.run_id8} ({case_id}) -> {out}")
    print("| stage | mode | version | owner | written | seconds | error |")
    print("|---|---|---|---|---|---|---|")
    for s in result.stages:
        print(f"| {s.name} | {s.mode} | {s.version} | {s.owner} | {', '.join(s.written)} | {s.seconds} | {s.error or ''} |")
    for note in result.notes:
        print("note:", note)
    if result.score is not None:
        print(S.score_markdown(result.score, include_dollars=args.dollars))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
