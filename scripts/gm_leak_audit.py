"""General Material leak audit over a production backup on disk ($0, no
database; AI_ESTIMATING_BUILD_SPEC 3.3 "Verify leak", WS6 GC1).

    uv run python scripts/gm_leak_audit.py [--backup DIR] [--eval-root DIR]

For every `general_material_estimates` row whose project has a parseable
estimate workbook, the stored General Material figure is compared with the
recap's WIRING material and with WIRING + OTHER ITEMS material (what Verify
should carry). Each project is classed:

- `combined`: the figure equals WIRING + OTHER ITEMS (no leak);
- `wiring_only`: the figure equals WIRING alone (OTHER ITEMS never reached
  Verify: the 7154 leak);
- `other`: neither (a manual figure, a different workbook, or a rounding
  the two rules do not cover).

The report goes to EVAL_ROOT/reports/gm_leak_audit.md (outside git, it
carries real project dollars). Nothing is written to a database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services import cost_library as cl  # noqa: E402
from app.services import estimate_parse as ep  # noqa: E402

DEFAULT_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
DEFAULT_EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
COMPANY_TZ = ZoneInfo("America/Los_Angeles")
TOLERANCE_ABS = Decimal("2.00")
TOLERANCE_REL = Decimal("0.002")


@dataclass
class AuditRow:
    project_id: str
    stored: Decimal
    wiring: Decimal
    other_items: Decimal
    verdict: str
    source: str | None
    has_other_items_key: bool

    @property
    def leaked(self) -> Decimal:
        return self.other_items if self.verdict == "wiring_only" else Decimal(0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project_id[:8],
            "stored": float(self.stored),
            "wiring": float(self.wiring),
            "other_items": float(self.other_items),
            "verdict": self.verdict,
            "source": self.source,
            "has_other_items_key": self.has_other_items_key,
        }


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(TOLERANCE_ABS, abs(b) * TOLERANCE_REL)


def classify(stored: Decimal, wiring: Decimal, other_items: Decimal) -> str:
    """Pure: the leak verdict for one project."""
    if _close(stored, wiring + other_items):
        return "combined"
    if _close(stored, wiring):
        return "wiring_only"
    return "other"


def recap_material(book: ep.ParsedEstimate, label: str) -> Decimal:
    """Material of one recap label; when the recap is absent, the detail
    lines of that group (rounded to cents like a recap row)."""
    row = book.recap_sections.get(label)
    if row is not None:
        return row.material.quantize(Decimal("0.01"))
    total = Decimal(0)
    for ln in book.item_lines:
        q = ln.quantity_eff
        if q and ln.unit_material_cost and ln.group == label:
            total += ln.unit_material_cost * q
    return total.quantize(Decimal("0.01"))


def audit(backup: Path, chosen: Mapping[str, ep.ParsedEstimate] | None = None) -> list[AuditRow]:
    if chosen is None:
        _report, chosen = cl.backup_books(backup)
    with open(backup / "data" / "general_material_estimates.json", encoding="utf-8") as fh:
        rows = json.load(fh)
    out: list[AuditRow] = []
    for g in rows:
        pid = g.get("project_id")
        book = chosen.get(pid) if pid else None
        if book is None or g.get("amount") is None:
            continue
        stored = Decimal(str(g["amount"]))
        wiring = recap_material(book, "WIRING")
        other = recap_material(book, "OTHER ITEMS")
        raw = g.get("raw_extraction")
        has_key = "other_items_material_cost" in json.dumps(raw) if raw is not None else False
        out.append(AuditRow(
            project_id=pid, stored=stored, wiring=wiring, other_items=other,
            verdict=classify(stored, wiring, other), source=g.get("source"),
            has_other_items_key=has_key,
        ))
    out.sort(key=lambda r: (r.verdict, r.project_id))
    return out


def markdown(rows: Sequence[AuditRow]) -> str:
    counts = {k: sum(1 for r in rows if r.verdict == k) for k in ("combined", "wiring_only", "other")}
    leaks = [r for r in rows if r.verdict == "wiring_only" and r.other_items > 0]
    lines = [
        "# General Material leak audit",
        "",
        f"Generated {dt.datetime.now(COMPANY_TZ).strftime('%Y-%m-%d %H:%M %Z')}.",
        "",
        f"Projects audited: {len(rows)}; combined {counts['combined']}; wiring only {counts['wiring_only']}; "
        f"other {counts['other']}. Projects where OTHER ITEMS material never reached Verify: {len(leaks)}.",
        "",
        "| Project | Stored GM | Recap WIRING | Recap OTHER ITEMS | Verdict | Source | other_items key |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.project_id[:8]} | {r.stored} | {r.wiring} | {r.other_items} | {r.verdict} | "
            f"{r.source or ''} | {'yes' if r.has_other_items_key else 'no'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", default=str(DEFAULT_BACKUP))
    ap.add_argument("--eval-root", default=str(DEFAULT_EVAL_ROOT))
    args = ap.parse_args(argv)
    rows = audit(Path(args.backup))
    reports = Path(args.eval_root) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "gm_leak_audit.md").write_text(markdown(rows))
    counts = {k: sum(1 for r in rows if r.verdict == k) for k in ("combined", "wiring_only", "other")}
    print(f"audited {len(rows)} projects: {counts} -> {reports / 'gm_leak_audit.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
