"""Build the WS7 history bands file from the production backup on disk
($0, no model, no database; AI_ESTIMATING_BUILD_SPEC WS7, 8.7 bands
population).

    uv run python scripts/build_history_bands.py
    uv run python scripts/build_history_bands.py --case 7154

Reads every estimate workbook of the backup through WS6 `estimate_parse`
(one book per project), drops the projects of
`eval_manifest.excluded_projects("bands", case)` (the holdout always, plus
the case's own project with --case), applies the documented book filter
(budget, ROM and labor-only books) and writes:

- EVAL_ROOT/bands/<band_version>.json (per-project values, quantiles per
  metric and stratum, the LOPO baseline per metric);
- EVAL_ROOT/reports/bands_<band_version>.md (n per metric and the LOPO
  baseline per metric).

Both live under EVAL_ROOT, outside both git repos, because the bid per
device carries real project dollars. The `ai_history_bands` table load is
wave 2 (after migration MF).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services import plans_reader_bands as pb  # noqa: E402

DEFAULT_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
DEFAULT_EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
# Spec 8.1 holdout (7188, 7173, 7178, 7148): the fallback when eval_manifest
# is not importable.
SPEC_HOLDOUT_IDS: tuple[str, ...] = (
    "078c470d-efa1-479c-b579-1690f5da47a0",
    "ba9f0bc2-ef71-4bfe-8b4f-70f92cf0c4b9",
    "220937f8-c69d-43b2-b5f8-b2e50f350310",
    "346a995e-3758-40cc-994c-008e00f299c6",
)


def excluded_ids(case: str | None = None) -> frozenset[str]:
    """`eval_manifest.excluded_projects("bands", case)`, imported lazily;
    without the module, the spec 8.1 holdout plus `case` when it is a full
    project id."""
    try:
        from app.services import eval_manifest
    except ImportError:
        out = set(SPEC_HOLDOUT_IDS)
        if case and len(case) == 36:
            out.add(case)
        return frozenset(out)
    return frozenset(eval_manifest.excluded_projects("bands", case))


def build(
    backup: Path | str = DEFAULT_BACKUP,
    eval_root: Path | str = DEFAULT_EVAL_ROOT,
    *,
    case: str | None = None,
    write: bool = True,
) -> tuple[pb.HistoryBands, Path | None, Path | None]:
    bands = pb.build_from_backup(backup, excluded_project_ids=excluded_ids(case))
    if not write:
        return bands, None, None
    json_path = pb.save_bands(bands, eval_root)
    reports = Path(eval_root) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    md_path = reports / f"bands_{bands.band_version}.md"
    md_path.write_text(pb.report_markdown(bands), encoding="utf-8")
    return bands, json_path, md_path


def summary_lines(bands: pb.HistoryBands, metrics: Iterable[str] = pb.METRIC_IDS) -> list[str]:
    """n and LOPO baseline per metric on the building stratum (no dollars)."""
    out = [f"band_version {bands.band_version}"]
    for metric in metrics:
        b = bands.band(metric, "building")
        if b is None:
            out.append(f"{metric}: n 0")
            continue
        lo = b.lopo
        out.append(
            f"{metric}: n {b.n}, zeros {len(b.zeros)}, LOPO extreme rate {lo.extreme_rate:.3f}, "
            f"critical {'supported' if lo.critical_supported else 'not supported'}"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backup", default=str(DEFAULT_BACKUP))
    ap.add_argument("--eval-root", default=str(DEFAULT_EVAL_ROOT))
    ap.add_argument("--case", default=None, help="case id, project id or prefix to leave out (LOPO)")
    ap.add_argument("--dry-run", action="store_true", help="build and print, write nothing")
    ap.add_argument("--persist", action="store_true", help="also upsert the bands into ai_history_bands on the dev database (after 0168)")
    args = ap.parse_args(argv)
    bands, json_path, md_path = build(args.backup, args.eval_root, case=args.case, write=not args.dry_run)
    for line in summary_lines(bands):
        print(line)
    if json_path:
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")
    if args.persist and not args.dry_run:
        from app.core.supabase_client import get_supabase

        written = pb.persist_bands(get_supabase(), bands)
        print(f"persisted {written} ai_history_bands rows ({bands.band_version} active)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
