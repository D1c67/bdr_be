"""Run the acceptance suite E1 to E25 and GC1 to GC4 on a bundle or on a
replay (docs/AI_ESTIMATING_BUILD_SPEC.md 3.5; app/services/plans_reader_acceptance.py).

Usage (from bdr_be):

    uv run python scripts/eval_acceptance.py --bundle EVAL_ROOT/bundles/7154/ff8fad25 [--case 7154] \\
        [--only E2,E12] [--json out.json] [--no-docs] [--two-line-demo]
    uv run python scripts/eval_acceptance.py --self-test [--bundle <weekend bundle>] [--json out.json]

Without --self-test the bundle is replayed through the registered stages
(legacy entries carry stored outputs forward) and every check runs over the
replay artifacts; the table prints status, parts and evidence per row and
the 7154 smoke verdict. --self-test compares every row with the Baseline
column of 3.5 (BASELINE_7154) and exits 1 when a row disagrees. Measured
values print counts and ratios; dollar-bearing output goes under EVAL_ROOT.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import eval_bundle, eval_manifest  # noqa: E402
from app.services import eval_reference as R  # noqa: E402
from app.services import plans_reader_acceptance as A  # noqa: E402
from app.services import plans_reader_replay as replay_mod  # noqa: E402

WEEKEND = "bundles/7154/ff8fad25"



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
    p = argparse.ArgumentParser(description="AI Estimating acceptance suite.")
    p.add_argument("--bundle", type=Path, default=None)
    p.add_argument("--case", default=None)
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--backup", type=Path, default=eval_bundle.DEFAULT_BACKUP_DIR)
    p.add_argument("--only", default=None, help="comma list of check ids")
    p.add_argument("--json", type=Path, default=None)
    p.add_argument("--no-docs", action="store_true", help="skip the text-layer checks (no PDFs)")
    p.add_argument("--two-line-demo", action="store_true", help="plans_reader_demo_two_line_style is on")
    p.add_argument("--self-test", action="store_true", help="compare with the 3.5 Baseline column")
    return p


def print_table(rows: list[dict]) -> None:
    print("| Check | Status | Gates 7154 | Parts | Evidence |")
    print("|---|---|---|---|---|")
    for r in rows:
        parts = ", ".join(f"{k}={v}" for k, v in (r.get("parts") or {}).items())
        print(f"| {r['id']} | {r['status']} | {'yes' if r.get('gates_7154_smoke') else 'no'} | {parts} | {'; '.join(r.get('evidence') or [])[:160]} |")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    bundle_dir = args.bundle or (root / WEEKEND)
    if args.self_test:
        rep = A.self_test(bundle_dir, eval_root=root, backup_dir=args.backup, with_docs=not args.no_docs)
        print("| Check | Expected | Got | Baseline matches | Baseline | Evidence |")
        print("|---|---|---|---|---|---|")
        for r in rep.rows:
            print(f"| {r.id} | {r.expected_status} | {r.status} | {r.baseline_matches} | {r.baseline_note} | {'; '.join(r.evidence)[:140]} |")
        if args.json:
            args.json.write_text(json.dumps(rep.to_dict(), indent=1, default=str))
        print("self-test", "OK" if rep.ok else f"FAILED on {[r.id for r in rep.failures()]}")
        return 0 if rep.ok else 1
    bundle = eval_bundle.load_bundle(bundle_dir)
    case_id = args.case or bundle.case_id
    reference = R.load_reference(root, case_id) if case_id else None
    expected_path = root / "references" / str(case_id) / "expected.json"
    expected = json.loads(expected_path.read_text()) if expected_path.is_file() else None
    _register_real_stages()
    result = replay_mod.replay(bundle, reference=reference, expected=expected, case_id=case_id, backup_dir=args.backup,
                               out_dir=root / "replays" / str(case_id) / f"{bundle.run_id8}_acceptance")
    docs = None if args.no_docs else replay_mod.LocalDocBytes(bundle, backup_dir=args.backup)
    proposal = A.load_sent_proposal_text(args.backup, str(bundle.run.get("project_id") or "")) if reference else None
    inp = A.inputs_from_replay(result, reference=reference, expected=expected, case_id=case_id, docs=docs,
                               options={**A.SELF_TEST_OPTIONS, "demo_two_line_style": args.two_line_demo},
                               sent_proposal_text=proposal)
    ids = [c.strip() for c in args.only.split(",")] if args.only else None
    results = A.run_checks(inp, ids)
    rows = A.results_table(results)
    print_table(rows)
    print("7154 smoke gate:", json.dumps(A.smoke_gate(results)))
    if args.json:
        args.json.write_text(json.dumps({"schema": A.ACCEPTANCE_SCHEMA, "bundle": str(bundle.root), "case_id": case_id,
                                         "rows": rows, "smoke_gate": A.smoke_gate(results)}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
