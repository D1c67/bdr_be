"""Backfill plans_reader_notes from AI Estimating outputs
(docs/ASSISTANT_PHASE4_PLAN.md 3.3).

    uv run python scripts/backfill_plans_reader_notes.py --dry-run|--apply [--run <uuid>] [--limit N]

Every run with a category or main output (any status: partial runs hold
category outputs) is synced through `plans_reader_notes.sync_run`, which is
idempotent, so the script is safe to run twice. `--dry-run` prints what would
be written per run and writes nothing. Refuses the production and staging
projects: the notes are filled on dev here, and by the maintenance sweep and
the lazy repair in the routes everywhere else.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD_REF = "zxqkqotlcgbalwoqeeca"
STAGING_REF = "qpfuugcwsdfpxvdgwvwx"
REFUSED_REFS = (PROD_REF, STAGING_REF)
PAGE = 1000
MAX_PAGES = 200


class RefusedError(RuntimeError):
    """The script will not run against this database."""


def check_target(url: str | None) -> None:
    """Raise RefusedError for a missing URL or the production or staging
    project."""
    if not url:
        raise RefusedError("SUPABASE_URL is not set; nothing to backfill.")
    low = url.lower()
    for ref in REFUSED_REFS:
        if ref in low:
            raise RefusedError(
                "SUPABASE_URL points at the production or staging project; this backfill runs on dev only."
            )


def _url() -> str | None:
    url = os.environ.get("SUPABASE_URL")
    if url:
        return url
    try:
        from app.core.config import get_settings

        return get_settings().supabase_url
    except Exception:  # noqa: BLE001 - no settings means no database
        return None


def run_ids_with_outputs(sb: Any, limit: int | None = None) -> list[str]:
    """Run ids holding a category or main output, oldest output first."""
    seen: dict[str, None] = {}
    for page in range(MAX_PAGES):
        start = page * PAGE
        rows = (
            sb.table("plans_reader_outputs")
            .select("id, run_id, scope")
            .in_("scope", ["category", "main"])
            .order("created_at")
            .order("id")
            .range(start, start + PAGE - 1)
            .execute()
            .data
            or []
        )
        for r in rows:
            if r.get("run_id"):
                seen.setdefault(str(r["run_id"]), None)
                if limit is not None and len(seen) >= limit:
                    return list(seen)
        if len(rows) < PAGE:
            break
    return list(seen)


def main(argv: list[str] | None = None, *, sb: Any = None, url: str | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    mode.add_argument("--apply", action="store_true", help="write the notes")
    parser.add_argument("--run", help="only this run id")
    parser.add_argument("--limit", type=int, default=None, help="at most N runs")
    args = parser.parse_args(argv)

    check_target(url if url is not None else _url())
    if args.run:
        try:
            uuid.UUID(args.run)
        except ValueError:
            raise RefusedError("--run must be a run uuid.") from None
    if args.limit is not None and args.limit < 1:
        raise RefusedError("--limit must be at least 1.")
    if sb is None:
        from app.core.supabase_client import get_supabase

        sb = get_supabase()
    from app.services import plans_reader_notes as notes

    ids = [args.run] if args.run else run_ids_with_outputs(sb, args.limit)
    totals = {"runs": 0, "rows": 0, "written": 0, "removed": 0, "failed": 0}
    for run_id in ids:
        totals["runs"] += 1
        try:
            if args.dry_run:
                plan = notes.plan_run(sb, run_id)
                totals["rows"] += plan["rows"]
                detail = ", ".join(f"{s['label']} v{s['version']}: {s['rows']}" for s in plan["sources"])
                print(f"{run_id}: {plan['rows']} rows ({detail or 'no sources'})")
            else:
                result = notes.sync_run(sb, run_id, max_rows=None)
                totals["written"] += result["written"]
                totals["removed"] += result["removed"]
                print(f"{run_id}: written {result['written']}, removed {result['removed']}")
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the backfill
            totals["failed"] += 1
            print(f"{run_id}: failed ({type(exc).__name__}: {str(exc)[:200]})", file=sys.stderr)
    mode_name = "dry run" if args.dry_run else "applied"
    print(f"{mode_name}: " + ", ".join(f"{k} {v}" for k, v in totals.items()))
    return totals


if __name__ == "__main__":
    try:
        main()
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(2)
