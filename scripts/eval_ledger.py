"""The eval ledger CLI (docs/AI_ESTIMATING_BUILD_SPEC.md 0.4, WS0 step 10).

Usage (from bdr_be; the dev database only):

    grep '^SUPABASE_URL' .env    # must show the dev ref bpidntbyvoooqvaispup
    uv run python scripts/eval_ledger.py status
    uv run python scripts/eval_ledger.py open --workstream WS3 --purpose dev_probe \\
        --budget 4 --label "route synth probe 7154 E2.01" [--cases 7154] [--config '{"k": 1}']
    uv run python scripts/eval_ledger.py close [--sweep <id>] [--result-path <path>]
    uv run python scripts/eval_ledger.py abort [--sweep <id>]
    uv run python scripts/eval_ledger.py allocate --workstream WS4 --usd 4 \\
        --from contingency --note "owner approved: extra consolidator iteration"
    uv run python scripts/eval_ledger.py report [--json]

One sweep is open at a time and every live call of the build runs inside
it (spec 0.4 rule 2). `open` records the code version and the prompt hash
on the sweep row and prints the sweep id on the last line; pass it to live
scripts as --sweep or EVAL_SWEEP_ID. `allocate` without --from sets an
allocation; with --from it moves dollars (how contingency is released);
every change needs a note. All times are Pacific.

Refuses unless SUPABASE_URL points at the dev project, and never touches
BDR_Prod or BDR_Staging. Writes only the ai_eval_* tables (migration 0163).
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import eval_ledger  # noqa: E402

DEV_REF = eval_ledger.DEV_REF


def _usd(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a dollar amount") from exc
    return value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AI Estimating eval ledger (dev only).")
    sub = p.add_subparsers(dest="command", required=True)
    o = sub.add_parser("open", help="open a sweep")
    o.add_argument("--workstream", required=True)
    o.add_argument("--purpose", required=True, choices=eval_ledger.PURPOSES)
    o.add_argument("--budget", required=True, type=_usd)
    o.add_argument("--label", required=True)
    o.add_argument("--cases", default="", help="comma list of case ids")
    o.add_argument("--config", default="", help="JSON object recorded on the sweep")
    for name in ("close", "abort"):
        c = sub.add_parser(name, help=f"{name} the open sweep (or --sweep)")
        c.add_argument("--sweep", default=None)
        c.add_argument("--result-path", default=None)
    a = sub.add_parser("allocate", help="set or move an allocation")
    a.add_argument("--workstream", required=True)
    a.add_argument("--usd", required=True, type=_usd)
    a.add_argument("--note", required=True)
    a.add_argument("--from", dest="from_workstream", default=None)
    r = sub.add_parser("report", help="spend by workstream and sweep")
    r.add_argument("--json", action="store_true")
    sub.add_parser("status", help="ceiling, spent, remaining and the open sweep")
    return p


def check_target(settings: Any) -> None:
    """Refuse anything but the dev BDR project."""
    if getattr(settings, "is_production", False):
        raise eval_ledger.EvalLedgerError("The eval ledger never runs in production.")
    eval_ledger.check_dev_url(getattr(settings, "supabase_url", ""))


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def run(argv: list[str], *, store: Any = None, settings: Any = None, out=None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    s = settings or get_settings()
    try:
        check_target(s)
        if store is None:
            from app.core.supabase_client import get_supabase

            store = eval_ledger.SupabaseLedgerStore(get_supabase(), supabase_url=s.supabase_url)
        if args.command == "open":
            from app.services import plans_reader_prompt_hash as ph

            config = json.loads(args.config) if args.config else {}
            if not isinstance(config, dict):
                raise eval_ledger.EvalLedgerError("--config must be a JSON object.")
            row = eval_ledger.open_sweep(
                store,
                label=args.label,
                workstream=args.workstream,
                purpose=args.purpose,
                budget_usd=args.budget,
                cases=[c.strip() for c in args.cases.split(",") if c.strip()],
                config=config,
                code_version=ph.code_version(),
                prompt_hash=ph.prompt_hash(),
            )
            print(
                f"Opened sweep {row.get('label')} ({row.get('workstream')}, {row.get('purpose')}, "
                f"budget ${Decimal(str(row.get('budget_usd'))):.2f}) at {eval_ledger.pacific(row.get('opened_at'))}.",
                file=out,
            )
            print(row["id"], file=out)
        elif args.command in ("close", "abort"):
            fn = eval_ledger.close_sweep if args.command == "close" else eval_ledger.abort_sweep
            row = fn(store, args.sweep, result_path=args.result_path)
            print(
                f"Sweep {row.get('label')} is {row.get('status')} at {eval_ledger.pacific(row.get('closed_at'))}; "
                f"spent ${Decimal(str(row.get('spent_usd') or 0)):.2f} of "
                f"${Decimal(str(row.get('budget_usd'))):.2f}.",
                file=out,
            )
        elif args.command == "allocate":
            allocations = eval_ledger.allocate(
                store,
                workstream=args.workstream,
                usd=args.usd,
                note=args.note,
                from_workstream=args.from_workstream,
            )
            for ws in sorted(allocations):
                print(f"{ws}: ${Decimal(str(allocations[ws]['allocated_usd'])):.2f}", file=out)
        elif args.command == "report":
            rep = eval_ledger.report(store)
            if args.json:
                print(json.dumps(rep, default=_json_default, indent=2), file=out)
            else:
                print(eval_ledger.report_markdown(rep), file=out)
        else:
            rep = eval_ledger.report(store)
            print(
                f"Ceiling ${rep['ceiling_usd']:.2f}, spent ${rep['spent_usd']:.2f}, "
                f"remaining ${rep['remaining_usd']:.2f}.",
                file=out,
            )
            open_rows = [sw for sw in rep["sweeps"] if sw["status"] == "open"]
            if open_rows:
                sw = open_rows[0]
                print(
                    f"Open sweep {sw['label']} ({sw['id']}): {sw['workstream']} {sw['purpose']}, "
                    f"spent ${sw['spent_usd']:.2f} of ${sw['budget_usd']:.2f}, opened {sw['opened_at']}.",
                    file=out,
                )
            else:
                print("No sweep is open.", file=out)
            if rep["final_zone"]:
                print("Global spend is past $360: only smoke or holdout sweeps may open.", file=out)
    except (eval_ledger.EvalLedgerError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
