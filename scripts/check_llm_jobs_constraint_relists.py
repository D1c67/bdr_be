#!/usr/bin/env python3
"""Fail when a migration re-lists llm_jobs_job_type_check with a full list.

A migration that drops llm_jobs_job_type_check and re-creates it with its own
literal list of job types silently removes every type a sibling migration
added, whenever the two are applied out of order. Prod skipped 0154 to 0158
(it has 0001-0153 and 0159-0162), and 0152 and 0154 both re-list the
constraint (0152:32-33, 0154:70-71), so that is the known baseline. 0165
(AI Estimating autonomy core) re-lists it too; it is accepted as baseline
on the integration decision of 2026-10-22 (docs/ASSISTANT_BUILD_LOG.md),
with the release rule that 0165 runs BEFORE 0170 in every environment: 0170
widens whatever list it finds, so that order keeps every type; the reverse
order would drop the assistant types. New migrations widen the constraint
by reading pg_get_constraintdef and appending their types (see
0170_assistant_core.sql, section 9a).

Classification of every literal re-list (an "add constraint
llm_jobs_job_type_check check (job_type in (...))" whose list carries the
original 'boq_extraction' type):

  historical   numbered below 0152: applied in order to every environment
               before prod and dev diverged, so they cannot clash.
  baseline     0152, 0154 and 0165: the known set, reported and allowed.
  violation    anything else: the script exits 1 and names the file.

Run from anywhere: python scripts/check_llm_jobs_constraint_relists.py
(optionally pass a migrations directory as the first argument).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"

# 0165 accepted 2026-10-22: it belongs to the AI Estimating build, and the
# release note orders it before 0170 everywhere (module docstring).
KNOWN_BASELINE: frozenset[int] = frozenset({152, 154, 165})
HISTORICAL_BELOW = 152
BASE_TYPE = "boq_extraction"  # the first type 0094 created; every full list carries it

_ADD_RE = re.compile(
    r"add\s+constraint\s+llm_jobs_job_type_check\s+check\s*\(\s*job_type\s+in\s*\((?P<list>[^)]*)\)",
    re.IGNORECASE,
)
_LITERAL_RE = re.compile(r"'((?:[^']|'')*)'")
_NUMBER_RE = re.compile(r"^(\d{4})_.*\.sql$")


@dataclass
class Report:
    historical: list[str] = field(default_factory=list)
    baseline: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _strip_comments(sql: str) -> str:
    """Drop -- line comments so prose that quotes the statement never counts."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def relists_full_list(sql: str) -> bool:
    """True when the SQL re-creates the constraint from a literal full list."""
    for m in _ADD_RE.finditer(_strip_comments(sql)):
        types = {t.replace("''", "'") for t in _LITERAL_RE.findall(m.group("list"))}
        if BASE_TYPE in types:
            return True
    return False


def scan(migrations_dir: Path) -> Report:
    report = Report()
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _NUMBER_RE.match(path.name)
        if not m:
            continue
        if not relists_full_list(path.read_text(encoding="utf-8")):
            continue
        number = int(m.group(1))
        if number < HISTORICAL_BELOW:
            report.historical.append(path.name)
        elif number in KNOWN_BASELINE:
            report.baseline.append(path.name)
        else:
            report.violations.append(path.name)
    return report


def main(argv: list[str]) -> int:
    migrations_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_DIR
    report = scan(migrations_dir)
    print(f"historical re-lists (below {HISTORICAL_BELOW:04d}, applied in order everywhere): "
          f"{', '.join(report.historical) or 'none'}")
    print(f"known baseline (0152, 0154 and 0165): {', '.join(report.baseline) or 'none'}")
    if report.ok:
        print("OK: no other migration re-lists llm_jobs_job_type_check with a full list.")
        return 0
    print("ERROR: these migrations also re-list llm_jobs_job_type_check with a full list:", file=sys.stderr)
    for name in report.violations:
        print(f"  {name}", file=sys.stderr)
    print("Widen it from pg_get_constraintdef instead (see 0170_assistant_core.sql, section 9a), "
          "so an out-of-order apply cannot drop another migration's job types.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
