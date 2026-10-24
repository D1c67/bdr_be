"""Load the backup estimate workbooks into the ME cost library tables (dev only).

    uv run python scripts/cost_library_backfill.py --dry-run
    uv run python scripts/cost_library_backfill.py --backup /path/to/BDR_Prod_2026-10-01

What it does (docs/AI_ESTIMATING_BUILD_SPEC.md WS6 wave 2):

1. refuses unless SUPABASE_URL points at the dev project
   (`bpidntbyvoooqvaispup`; spec 0.2: the cost library never lands in
   staging or prod from this script);
2. builds `cost_library.backfill_plan` from the chosen workbook of every
   backup project, with the holdout projects left out through
   `eval_manifest.excluded_projects("pricing_library")` so a library built
   from the tables can never see the holdout;
3. with --dry-run prints the counts and stops; otherwise upserts the
   sources and items and inserts the observations in pages of 500 through
   the Supabase client, idempotently (a re-run replaces a source's
   observations instead of duplicating them).

No dollar figure is printed: counts and ratios only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import cost_library as cl  # noqa: E402
from app.services import eval_manifest  # noqa: E402
from app.services.eval_bundle import DEFAULT_BACKUP_DIR  # noqa: E402

DEV_REF = "bpidntbyvoooqvaispup"
LIBRARY_PURPOSE = "pricing_library"
PAGE = 500


class RefusedError(RuntimeError):
    """The script will not run against this database."""


def check_dev_url(url: str | None) -> str:
    """The dev project ref or RefusedError (any other ref, or none)."""
    if not url or DEV_REF not in url:
        raise RefusedError(
            "SUPABASE_URL does not point at the dev project; the cost library backfill runs on dev only."
        )
    return DEV_REF


def _chunks(rows: list[dict[str, Any]], size: int = PAGE):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def apply_plan(sb: Any, plan: cl.BackfillPlan) -> dict[str, int]:
    """Write the plan: sources upserted on (project_id, file_id), their
    prior observations deleted, items upserted on (item_key, uom), then the
    observations inserted in pages with item ids resolved. Returns counts."""
    written = {"sources": 0, "items": 0, "observations": 0, "replaced_sources": 0}
    source_ids: dict[str, str] = {}
    for src in plan.sources:
        existing = (
            sb.table("cost_sources").select("id").eq("project_id", src["project_id"])
            .eq("file_id", src["file_id"]).limit(1).execute()
        ).data or []
        if existing:
            sid = str(existing[0]["id"])
            sb.table("cost_sources").update({k: v for k, v in src.items() if k != "id"}).eq("id", sid).execute()
            sb.table("cost_observations").delete().eq("source_id", sid).execute()
            written["replaced_sources"] += 1
        else:
            inserted = sb.table("cost_sources").insert(src).execute().data or [src]
            sid = str(inserted[0].get("id") or src["id"])
        source_ids[src["id"]] = sid
        written["sources"] += 1
    item_ids: dict[tuple[str, str], str] = {}
    for page in _chunks(plan.items):
        rows = sb.table("cost_items").upsert(page, on_conflict="item_key,uom").execute().data or []
        for r in rows:
            if r.get("id"):
                item_ids[(str(r["item_key"]), str(r["uom"]))] = str(r["id"])
        written["items"] += len(page)
    obs_rows: list[dict[str, Any]] = []
    for o in plan.observations:
        row = dict(o)
        row["source_id"] = source_ids.get(row["source_id"], row["source_id"])
        item_id = item_ids.get((row["item_key"], row["uom"]))
        if item_id is not None:
            row["item_id"] = item_id
        obs_rows.append(row)
    for page in _chunks(obs_rows):
        sb.table("cost_observations").insert(page).execute()
        written["observations"] += len(page)
    return written


def build_plan(backup: str | Path, *, purpose: str = LIBRARY_PURPOSE) -> cl.BackfillPlan:
    excluded = eval_manifest.excluded_projects(purpose)
    return cl.backfill_plan(backup, exclude_projects=excluded)


def run(argv: list[str] | None = None, *, sb: Any = None, supabase_url: str | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--backup", default=str(DEFAULT_BACKUP_DIR), help="production backup directory")
    parser.add_argument("--dry-run", action="store_true", help="print the counts, write nothing")
    parser.add_argument("--json", action="store_true", help="print the counts as JSON")
    args = parser.parse_args(argv)
    url = supabase_url if supabase_url is not None else os.environ.get("SUPABASE_URL")
    if url is None and supabase_url is None:
        try:
            from app.core.config import get_settings

            url = get_settings().supabase_url
        except Exception:  # noqa: BLE001 - no settings means no database
            url = None
    check_dev_url(url)
    plan = build_plan(args.backup)
    out: dict[str, Any] = {"plan": plan.counts, "dry_run": bool(args.dry_run)}
    if not args.dry_run:
        if sb is None:
            from app.core.supabase_client import get_supabase

            sb = get_supabase()
        out["written"] = apply_plan(sb, plan)
    text = json.dumps(out, indent=2) if args.json else "\n".join(
        f"{k}: {v}" for section in ("plan", "written") if section in out for k, v in out[section].items()
    )
    print(text)
    return out


if __name__ == "__main__":
    try:
        run()
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(2)
