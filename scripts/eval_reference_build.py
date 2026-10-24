"""Build reference.json for golden cases (WS0 step 3) from the backup.

Usage (from bdr_be):
  uv run python scripts/eval_reference_build.py --case 7154 [--case 7177 ...]
  uv run python scripts/eval_reference_build.py --all            (the 12 cases of spec 8.1)
  uv run python scripts/eval_reference_build.py --project <id or prefix> --case-id <label>
  uv run python scripts/eval_reference_build.py --pool-checks    (counts over the 66 golden ids)

Each case's adjudications are read from EVAL_ROOT/references/<case>/adjudications.json
when present and applied with their reviewed flag (nothing here marks one
reviewed). The data-quality checks of 8.6 print as pass/fail/skip; dollar
values stay in the written file under EVAL_ROOT.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BE_ROOT = Path(__file__).resolve().parents[1]
if str(_BE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BE_ROOT))

from app.services import eval_manifest as M  # noqa: E402
from app.services import eval_reference as R  # noqa: E402


def _build_one(case_id: str, project: str, data: R.BackupData, root: Path, *, quiet: bool) -> dict:
    adj_path = R.reference_dir(root, case_id) / "adjudications.json"
    adjudications = R.load_adjudications(adj_path)
    ref = R.build_reference(case_id, project, data=data, adjudications=adjudications)
    path = R.write_reference(ref, root)
    if not quiet:
        lines = ref["quantity_lines"]
        counts = {c["check"]: c["status"] for c in ref["data_quality"]}
        print(
            f"{case_id}: wrote {path}; {len(lines)} lines ({len(ref['accessory_lines'])} accessories), "
            f"adjudications {len(ref['adjudications_applied'])} applied / {len(ref['adjudications_ignored'])} ignored "
            f"(reviewed={ref['adjudications_reviewed']}); checks {counts}; warnings {len(ref['warnings'])}"
        )
        for w in ref["warnings"]:
            print(f"  warning: {w}")
    return ref


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build EVAL_ROOT/references/<case>/reference.json.")
    ap.add_argument("--case", action="append", default=[], help="case id of spec 8.1 (repeatable)")
    ap.add_argument("--all", action="store_true", help="every case of the 12 tiers")
    ap.add_argument("--project", help="a project id or prefix outside the tiers (with --case-id)")
    ap.add_argument("--case-id", help="label for --project")
    ap.add_argument("--pool-checks", action="store_true", help="Verify and materials reconcile counts over the golden ids")
    ap.add_argument("--backup-dir", type=Path, default=R.DEFAULT_BACKUP_DIR)
    ap.add_argument("--eval-root", type=Path, default=None, help="defaults to settings.eval_root")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.eval_root) if args.eval_root else M.eval_root()
    data = R.BackupData(args.backup_dir)

    if args.pool_checks:
        counts = R.pool_data_quality(M.GOLDEN_IDS, args.backup_dir)
        non_holdout = R.pool_data_quality([g for g in M.GOLDEN_IDS if g not in M.HOLDOUT_IDS], args.backup_dir)
        print(f"golden sent+verified: {counts['cases_sent_and_verified']}; verify sum == sent: {counts['verify_sum_matches_sent']}; materials == taxed quotes: {counts['materials_match_quotes']}")
        print(f"non-holdout sent+verified: {non_holdout['cases_sent_and_verified']}; reconciled (LOPO n): {non_holdout['verify_sum_matches_sent']}; failing: {non_holdout['failing_verify']}")
        print(f"labor notes parsed: {counts['labor_notes_parsed']} of {counts['labor_notes_rows']}")

    targets: list[tuple[str, str]] = []
    if args.all:
        targets = [(c, p) for rows in M.TIER_CASES.values() for c, p, _n in rows]
    for c in args.case:
        if c not in M.CASE_PREFIX:
            print(f"unknown case {c}; known: {', '.join(M.CASE_PREFIX)}", file=sys.stderr)
            return 2
        targets.append((c, M.CASE_PREFIX[c]))
    if args.project:
        targets.append((args.case_id or args.project[:8], args.project))
    for case_id, project in targets:
        _build_one(case_id, project, data, root, quiet=args.quiet)
    if not targets and not args.pool_checks:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
