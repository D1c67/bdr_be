"""Bundle CLI for the AI Estimating eval harness (docs/AI_ESTIMATING_BUILD_SPEC.md
WS0 step 7; app/services/eval_bundle.py).

Usage (from bdr_be):

    uv run python scripts/eval_bundle.py import-weekend \\
        --src /Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/ff8fad25 [--out <dir>] [--case 7154]
    uv run python scripts/eval_bundle.py export --run-id <dev run uuid> --case 7172 [--out <dir>]
    uv run python scripts/eval_bundle.py show --bundle <dir>
    uv run python scripts/eval_bundle.py verify --bundle <dir>

`import-weekend` writes bundle.json next to the weekend export (in place when
--out is omitted). `export` reads ONE dev run through the service-role client
(SUPABASE_URL must point at the dev project; BDR_Prod and BDR_Staging are
refused) into EVAL_ROOT/bundles/<case>/<run id8>/; exporting a run again into
the same directory replaces the members (a re-export after the pricing and
review chain finished picks them up). The Five Star (7172) and
YMCA (7177) dev-run exports are wave 2 work: find their run ids on dev
(spec WS0 step 7) and pass them here. `show` prints the bundle summary and
`verify` checks every member against bundle.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import eval_bundle, eval_manifest  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AI Estimating eval bundles.")
    sub = p.add_subparsers(dest="command", required=True)
    w = sub.add_parser("import-weekend", help="make the weekend export a bundle (adds bundle.json)")
    w.add_argument("--src", type=Path, required=True)
    w.add_argument("--out", type=Path, default=None)
    w.add_argument("--case", default="7154")
    e = sub.add_parser("export", help="export a DEV run to a bundle")
    e.add_argument("--run-id", required=True)
    e.add_argument("--case", default=None, help="case id (defaults to the run's eval_case_id)")
    e.add_argument("--out", type=Path, default=None)
    e.add_argument("--eval-root", type=Path, default=None)
    s = sub.add_parser("show", help="summarize a bundle")
    s.add_argument("--bundle", type=Path, required=True)
    v = sub.add_parser("verify", help="check members against bundle.json")
    v.add_argument("--bundle", type=Path, required=True)
    return p


def _show(bundle: eval_bundle.RunBundle) -> dict:
    return {
        "root": str(bundle.root),
        "run_id": bundle.run_id,
        "case_id": bundle.case_id,
        "source": bundle.source,
        "status": bundle.run.get("status"),
        "files": len(bundle.files),
        "documents": [(d.get("name"), d.get("category"), d.get("page_count")) for d in bundle.documents],
        "groups": [(g.get("kind"), g.get("category"), g.get("group_index"), g.get("pass"), str(g.get("id")) in bundle.group_outputs) for g in bundle.groups],
        "outputs": sorted(bundle.outputs),
        "deliverables": sorted(f"{k}_v{v}" for k, v in bundle.deliverables),
        "artifacts": sorted(bundle.artifacts),
        "calls": len(bundle.calls),
        "chat_probe": bool(bundle.chat_probe()),
        "run_columns": sorted(bundle.run_columns),
        "autonomy_state": bundle.run.get("autonomy_state"),
        "pricing_versions": [(r.get("version"), r.get("status"), bool(r.get("stale")), r.get("boq_version"),
                              len(bundle.pricing_version_lines(int(r.get("version") or 0)))) for r in bundle.pricing_rows],
        "quotes": len(bundle.quote_rows),
        "reviews": [(r.get("base_pricing_version"), r.get("revised_pricing_version"), r.get("stage"), r.get("verdict"))
                    for r in bundle.review_rows],
        "review_findings": len(bundle.review_finding_rows),
        "gate_findings": [(r.get("stage"), r.get("gate_id"), r.get("severity"), r.get("status")) for r in bundle.gate_finding_rows],
        "recounts": [(r.get("row_id"), r.get("outcome")) for r in bundle.recount_rows],
        "signoffs": len(bundle.signoff_rows),
        "unavailable_tables": bundle.meta.get("unavailable_tables"),
        "notes": bundle.notes,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "import-weekend":
        out = eval_bundle.import_weekend_export(args.src, args.out, case_id=args.case)
        print(json.dumps(_show(eval_bundle.load_bundle(out)), indent=1, default=str))
        print(out)
        return 0
    if args.command == "export":
        settings = get_settings()
        eval_bundle.check_not_production(settings)
        from supabase import create_client

        sb = create_client(settings.supabase_url, settings.supabase_service_role_key)
        root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
        rows = sb.table("plans_reader_runs").select("id,eval_case_id,name").eq("id", args.run_id).execute().data or []
        if not rows:
            raise SystemExit(f"run {args.run_id} not found on dev")
        case = args.case or rows[0].get("eval_case_id")
        if not case:
            raise SystemExit("pass --case: the run has no eval_case_id")
        out = Path(args.out) if args.out else root / "bundles" / str(case) / str(args.run_id)[:8]
        eval_bundle.export_run(sb, args.run_id, out, case_id=str(case), settings=settings)
        print(json.dumps(_show(eval_bundle.load_bundle(out)), indent=1, default=str))
        print(out)
        return 0
    if args.command == "show":
        print(json.dumps(_show(eval_bundle.load_bundle(args.bundle)), indent=1, default=str))
        return 0
    bad = eval_bundle.verify_members(args.bundle)
    if bad:
        print("members changed since bundle.json:", *bad, sep="\n  ")
        return 1
    print("bundle intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
