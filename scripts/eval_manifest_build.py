"""Build the golden manifest of the AI Estimating eval (WS0 step 4).

Reads the production backup on disk (read-only), hashes every input and
answer file of the 12 cases of spec 8.1, counts big sheets and spec pages,
computes the populations of 8.7 and writes EVAL_ROOT/manifest/manifest.json.

Usage (from bdr_be):
  uv run python scripts/eval_manifest_build.py [--backup-dir DIR] [--eval-root DIR]
      [--no-pages] [--populations golden,lopo,...] [--print]

Nothing here touches a database or a model. Dollar figures stay under
EVAL_ROOT; the printout shows counts only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BE_ROOT = Path(__file__).resolve().parents[1]
if str(_BE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BE_ROOT))

from app.services import eval_manifest as M  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build EVAL_ROOT/manifest/manifest.json from the backup.")
    ap.add_argument("--backup-dir", type=Path, default=M.DEFAULT_BACKUP_DIR)
    ap.add_argument("--eval-root", type=Path, default=None, help="defaults to settings.eval_root")
    ap.add_argument("--no-pages", action="store_true", help="skip PDF page counting (faster)")
    ap.add_argument(
        "--populations",
        default="golden,lopo,estimate,wage,markup,old_pipeline_bundles",
        help="comma list of 8.7 populations to record (r3 and bands parse every BOQ, slower)",
    )
    ap.add_argument("--print", action="store_true", help="print the case table after writing")
    args = ap.parse_args(argv)

    pops = [p.strip() for p in args.populations.split(",") if p.strip()]
    manifest = M.build_manifest(
        args.backup_dir,
        eval_root_dir=args.eval_root,
        count_pages=not args.no_pages,
        populations=pops,
    )
    path = M.write_manifest(manifest, args.eval_root)
    print(f"wrote {path} ({len(manifest.cases)} cases, {len(manifest.answer_sha256)} answer hashes)")
    if args.print:
        print("| case | tier | number | type | wage | big sheets | spec pages | inputs | quotes | sends | verify |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for c in manifest.cases:
            print(
                f"| {c.case_id} | {c.tier} | {c.number} | {c.project_type} | {c.wage_type} | {c.big_sheets} |"
                f" {c.spec_pages} | {len(c.quantity_inputs)} | {len(c.pricing_inputs)} | {c.sends} |"
                f" {'yes' if c.committed_verify else 'no'} |"
            )
        for name, pop in manifest.populations.items():
            n = pop.get("n") if pop.get("n") is not None else len(pop.get("ids") or [])
            print(f"population {name}: n={n}")
        for note in manifest.notes:
            print(f"note: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
