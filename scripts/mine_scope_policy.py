"""Mine the production backup for G3 scope policy evidence (WS5, $0, offline).

Reads the backup on disk only (never a database, never a model) and reports:

- sent proposals: the latest sent proposal docx per project
  (`proposal_sends.status = 'sent'` joined to `project_files` by `file_id`),
  the share of projects carrying each template sentence and each seed
  proposal line, the wage and shift lines, and the custom scope lines;
- estimate workbooks: LS rows and their quantities, standard allowance rows,
  projects carrying thermostat, control cable and circuit breaker rows;
- estimator BOQs: how many estimator rows each seed rule would fire on (hard
  rules on estimator rows are the false-drop proxy: G3 carried those rows);
- projects: owner_type by wage_type.

Holdout projects are never mined: the excluded set comes from
`eval_manifest.excluded_projects("scope_policy")`.

Writes `EVAL_ROOT/reports/scope_policy_mining.md` and `.json` (no dollar
figures; project numbers only where a rule fires on an estimator row).

Usage:
  uv run python scripts/mine_scope_policy.py [--backup DIR] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BE_ROOT = Path(__file__).resolve().parents[1]
if str(_BE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BE_ROOT))

from app.services import scope_policy  # noqa: E402
from app.services.plans_reader_schemas import ESTIMATE_ALLOWANCES  # noqa: E402
from app.services.scope_policy_seed import SEED_POLICY, TEMPLATE_LINES  # noqa: E402

DEFAULT_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
DEFAULT_EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
PACIFIC = ZoneInfo("America/Los_Angeles")

# Spec 8.1 holdout, used only when app.services.eval_manifest (WS0) cannot be
# imported. Keep in sync with the spec; eval_manifest is the source of truth.
HOLDOUT_PROJECT_IDS: frozenset[str] = frozenset(
    {
        "078c470d-efa1-479c-b579-1690f5da47a0",  # 7188
        "ba9f0bc2-ef71-4bfe-8b4f-70f92cf0c4b9",  # 7173
        "220937f8-c69d-43b2-b5f8-b2e50f350310",  # 7178
        "346a995e-3758-40cc-994c-008e00f299c6",  # 7148
    }
)


def excluded_projects(purpose: str = "scope_policy") -> tuple[frozenset[str], str]:
    """(project ids, source). Lazy import so WS0 can land in parallel."""
    try:
        from app.services import eval_manifest  # type: ignore[attr-defined]

        return frozenset(eval_manifest.excluded_projects(purpose)), "eval_manifest"
    except Exception:  # noqa: BLE001 - any import or lookup failure uses the spec list
        return HOLDOUT_PROJECT_IDS, "spec 8.1 fallback list"


def _load_table(backup: Path, name: str) -> list[dict[str, Any]]:
    return json.loads((backup / "data" / f"{name}.json").read_text(encoding="utf-8"))


def _storage_path(backup: Path, file_row: dict[str, Any]) -> Path:
    return backup / "storage" / "project-files" / file_row["storage_path"]


def docx_paragraphs(path: Path) -> list[str]:
    """Paragraph texts of a docx (stdlib only), whitespace-collapsed."""
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8", "replace")
    out: list[str] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para))
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if text:
            out.append(text)
    return out


def _norm(text: str) -> str:
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


# Sent proposals -----------------------------------------------------------------------

_CUSTOM_RE = re.compile(
    r"\b(?:exclud\w*|by\s+others|not\s+includ\w*|provided\s+by|owner|allowance|temporary|"
    r"permit\w*|patch\w*|paint\w*|core|bond|union|davis|night|overtime|phas\w*|"
    r"includes?\b.{0,60}\bonly)\b",
    re.I,
)


def mine_proposals(backup: Path, excluded: frozenset[str]) -> dict[str, Any]:
    sends = _load_table(backup, "proposal_sends")
    files = {f["id"]: f for f in _load_table(backup, "project_files")}
    latest: dict[str, dict[str, Any]] = {}
    for send in sends:
        if send.get("status") != "sent" or send.get("file_id") not in files:
            continue
        pid = send["project_id"]
        if pid in excluded:
            continue
        if pid not in latest or (send.get("sent_at") or "") > (latest[pid].get("sent_at") or ""):
            latest[pid] = send
    per_project: dict[str, set[str]] = {}
    for pid, send in latest.items():
        try:
            paras = docx_paragraphs(_storage_path(backup, files[send["file_id"]]))
        except (OSError, KeyError, zipfile.BadZipFile):
            continue
        per_project[pid] = {_norm(p) for p in paras}
    n = len(per_project)
    template = {line: sum(_norm(line) in s for s in per_project.values()) for line in TEMPLATE_LINES}
    seed_lines: dict[str, dict[str, Any]] = {}
    for rule in SEED_POLICY["rules"]:
        line = rule.get("proposal_line")
        if line:
            seed_lines[rule["rule_id"]] = {
                "line": line,
                "projects": sum(_norm(line) in s for s in per_project.values()),
            }
    wage = Counter()
    shift = Counter()
    line_counts: Counter[str] = Counter()
    for paras in per_project.values():
        for p in paras:
            if p.startswith("all labor is based"):
                wage["non_prevailing" if re.search(r"\bnon[\s-]*prevailing", p) else "prevailing"] += 1
            if p.startswith("all work will be performed"):
                if "night" in p and "day" in p:
                    shift["mixed"] += 1
                elif "night" in p:
                    shift["night"] += 1
                else:
                    shift["day"] += 1
        line_counts.update(paras)
    template_norm = {_norm(t) for t in TEMPLATE_LINES}
    custom = sorted(
        (
            (count, line)
            for line, count in line_counts.items()
            if count <= 3 and line not in template_norm and _CUSTOM_RE.search(line)
            and len(line) <= 200
        ),
        key=lambda x: (-x[0], x[1]),
    )
    return {
        "projects": n,
        "template_lines": template,
        "seed_proposal_lines": seed_lines,
        "wage_lines": dict(wage),
        "shift_lines": dict(shift),
        "custom_lines": [{"projects": c, "line": line} for c, line in custom[:60]],
    }


# Estimate workbooks -------------------------------------------------------------------

_THERMO_RE = re.compile(r"\b(?:thermostats?|t-?stats?)\b", re.I)
_CONTROL_CABLE_RE = re.compile(r"#\s?18\s?/\s?4\s?C?\b|\bcontrol\s+cable\b", re.I)
_BREAKER_RE = re.compile(r"\bcircuit\s+breakers?\b|\bbreakers?\b", re.I)
_FA_RE = re.compile(r"\bfire\s+alarm\b|\bsmoke\s+detector\b|\bpull\s+station\b", re.I)


def _estimate_rows(path: Path) -> list[tuple[Any, ...]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows: list[tuple[Any, ...]] = []
    try:
        for ws in wb.worksheets:
            cols: dict[str, int] | None = None
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip().upper() if c is not None else "" for c in row]
                if cols is None:
                    if "UNIT LABOR HOURS" in cells and "UNIT" in cells and "DESCRIPTION" in cells:
                        cols = {
                            "desc": cells.index("DESCRIPTION"),
                            "unit": cells.index("UNIT"),
                            "qty": cells.index("QUANTITY") if "QUANTITY" in cells else -1,
                        }
                    continue
                desc = row[cols["desc"]] if cols["desc"] < len(row) else None
                unit = row[cols["unit"]] if cols["unit"] < len(row) else None
                qty = row[cols["qty"]] if 0 <= cols["qty"] < len(row) else None
                if desc is None or unit is None:
                    continue
                rows.append((ws.title, str(desc).strip(), str(unit).strip().upper(), qty))
    finally:
        wb.close()
    return rows


def mine_estimates(backup: Path, excluded: frozenset[str]) -> dict[str, Any]:
    files = [
        f
        for f in _load_table(backup, "project_files")
        if f.get("category") == "estimate"
        and str(f.get("filename", "")).lower().endswith(".xlsx")
        and f["project_id"] not in excluded
    ]
    std_names = {n.upper() for n in ESTIMATE_ALLOWANCES}
    ls_rows = ls_zero = ls_std = ls_std_zero = 0
    projects: set[str] = set()
    with_lines: set[str] = set()
    thermo: set[str] = set()
    cable: set[str] = set()
    breakers: set[str] = set()
    fire_alarm: set[str] = set()
    failures = 0
    for f in files:
        try:
            rows = _estimate_rows(_storage_path(backup, f))
        except Exception:  # noqa: BLE001 - a broken workbook is counted, not fatal
            failures += 1
            continue
        pid = f["project_id"]
        projects.add(pid)
        if rows:
            with_lines.add(pid)
        for _sheet, desc, unit, qty in rows:
            if unit == "LS":
                ls_rows += 1
                zero = qty in (None, "", 0, 0.0)
                ls_zero += zero
                if desc.upper() in std_names:
                    ls_std += 1
                    ls_std_zero += zero
            if _THERMO_RE.search(desc):
                thermo.add(pid)
            if _CONTROL_CABLE_RE.search(desc):
                cable.add(pid)
            if _BREAKER_RE.search(desc):
                breakers.add(pid)
            if _FA_RE.search(desc):
                fire_alarm.add(pid)
    return {
        "files": len(files),
        "failures": failures,
        "projects": len(projects),
        "projects_with_lines": len(with_lines),
        "ls_rows": ls_rows,
        "ls_rows_zero_or_blank": ls_zero,
        "ls_rows_standard_allowance": ls_std,
        "ls_rows_standard_allowance_zero": ls_std_zero,
        "projects_with_thermostat_rows": len(thermo),
        "projects_with_control_cable_rows": len(cable),
        "projects_with_breaker_rows": len(breakers),
        "projects_with_fire_alarm_rows": len(fire_alarm),
    }


# Estimator BOQs: rule fire counts -------------------------------------------------------


def _golden_module():
    name = "plans_reader_golden"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _BE_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def mine_boqs(backup: Path, excluded: frozenset[str]) -> dict[str, Any]:
    golden = _golden_module()
    files = [
        f
        for f in _load_table(backup, "project_files")
        if f.get("category") == "boq"
        and str(f.get("filename", "")).lower().endswith(".xlsx")
        and f["project_id"] not in excluded
    ]
    latest: dict[str, dict[str, Any]] = {}
    for f in files:
        pid = f["project_id"]
        if pid not in latest or (f.get("created_at") or "") > (latest[pid].get("created_at") or ""):
            latest[pid] = f
    numbers = {p["id"]: p.get("number") for p in _load_table(backup, "projects")}
    total_rows = 0
    fires: dict[str, Counter[str]] = defaultdict(Counter)
    samples: dict[str, list[str]] = defaultdict(list)
    projects_per_rule: dict[str, set[str]] = defaultdict(set)
    hard_rows = 0
    failures = 0
    for pid, f in latest.items():
        try:
            rows = golden.parse_estimator_boq(_storage_path(backup, f).read_bytes())
        except Exception:  # noqa: BLE001
            failures += 1
            continue
        for row in rows:
            if row.is_accessory:
                continue
            total_rows += 1
            fired_hard = False
            for rule in SEED_POLICY["rules"]:
                if rule["decision"] == "include":
                    continue
                hit = scope_policy.match_rule(
                    rule, row.description, subgroup=row.subgroup, group=row.group
                )
                if hit is None:
                    continue
                fires[rule["rule_id"]]["rows"] += 1
                projects_per_rule[rule["rule_id"]].add(pid)
                if len(samples[rule["rule_id"]]) < 6:
                    samples[rule["rule_id"]].append(
                        f"{numbers.get(pid)}: {row.subgroup or row.group}: {row.description[:90]}"
                    )
                if rule["kind"] == "hard":
                    fired_hard = True
            hard_rows += fired_hard
    return {
        "projects": len(latest),
        "failures": failures,
        "rows": total_rows,
        "rows_hit_by_hard_rules": hard_rows,
        "hard_rule_row_share": (hard_rows / total_rows) if total_rows else 0.0,
        "per_rule": {
            rid: {
                "rows": counts["rows"],
                "projects": len(projects_per_rule[rid]),
                "samples": samples[rid],
            }
            for rid, counts in sorted(fires.items())
        },
    }


# Projects -------------------------------------------------------------------------------


def mine_projects(backup: Path, excluded: frozenset[str]) -> dict[str, Any]:
    table: Counter[tuple[str, str]] = Counter()
    labor_time: Counter[str] = Counter()
    for p in _load_table(backup, "projects"):
        if p["id"] in excluded:
            continue
        table[(str(p.get("owner_type")), str(p.get("wage_type")))] += 1
        labor_time[str(p.get("labor_time"))] += 1
    return {
        "owner_by_wage": [
            {"owner_type": o, "wage_type": w, "projects": n} for (o, w), n in sorted(table.items())
        ],
        "labor_time": dict(labor_time),
    }


# Report ---------------------------------------------------------------------------------


def render_markdown(result: dict[str, Any]) -> str:
    prop = result["proposals"]
    est = result["estimates"]
    boq = result["boqs"]
    n = prop["projects"]
    out = [
        "# Scope policy mining (WS5)",
        "",
        f"Generated {result['generated_at']} (Pacific). Backup `{result['backup']}`.",
        f"Excluded projects ({result['excluded_source']}): {result['excluded_count']}.",
        f"Seed policy sha256 `{result['seed_sha']}`.",
        "",
        "## Sent proposals (latest send per project)",
        "",
        f"Projects with a sent proposal docx: {n}.",
        "",
        "| Template line | Projects | Share |",
        "|---|---|---|",
    ]
    for line, count in prop["template_lines"].items():
        share = f"{count / n:.1%}" if n else "n/a"
        out.append(f"| {line} | {count} of {n} | {share} |")
    out += ["", "| Seed rule | Proposal line | Projects |", "|---|---|---|"]
    for rid, info in prop["seed_proposal_lines"].items():
        out.append(f"| {rid} | {info['line']} | {info['projects']} of {n} |")
    out += [
        "",
        f"Wage lines: {prop['wage_lines']}. Shift lines: {prop['shift_lines']}.",
        "",
        "Custom scope lines (in at most 3 projects):",
        "",
    ]
    out += [f"- ({c['projects']}) {c['line']}" for c in prop["custom_lines"]]
    out += [
        "",
        "## Estimate workbooks",
        "",
        f"Files {est['files']} over {est['projects']} projects ({est['failures']} unreadable); "
        f"projects with lines {est['projects_with_lines']}.",
        f"LS rows {est['ls_rows']}; quantity 0 or blank {est['ls_rows_zero_or_blank']}; "
        f"standard allowance rows {est['ls_rows_standard_allowance']} "
        f"({est['ls_rows_standard_allowance_zero']} at 0).",
        f"Projects with thermostat rows {est['projects_with_thermostat_rows']}, control cable "
        f"{est['projects_with_control_cable_rows']}, breakers {est['projects_with_breaker_rows']}, "
        f"fire alarm {est['projects_with_fire_alarm_rows']}.",
        "",
        "## Estimator BOQs: seed rule fire counts (false-drop proxy)",
        "",
        f"Projects {boq['projects']} ({boq['failures']} unreadable), item rows {boq['rows']}. "
        f"Rows a HARD rule fires on: {boq['rows_hit_by_hard_rules']} "
        f"({boq['hard_rule_row_share']:.2%}; target below 1%).",
        "",
        "| Rule | Rows | Projects | Samples |",
        "|---|---|---|---|",
    ]
    for rid, info in boq["per_rule"].items():
        samples = "<br>".join(s.replace("|", "/") for s in info["samples"])
        out.append(f"| {rid} | {info['rows']} | {info['projects']} | {samples} |")
    out += ["", "## Projects: owner_type by wage_type", "", "| owner_type | wage_type | n |"]
    out.append("|---|---|---|")
    for row in result["projects"]["owner_by_wage"]:
        out.append(f"| {row['owner_type']} | {row['wage_type']} | {row['projects']} |")
    out += ["", f"labor_time: {result['projects']['labor_time']}", ""]
    return "\n".join(out)


def mine(backup: Path) -> dict[str, Any]:
    excluded, source = excluded_projects("scope_policy")
    return {
        "generated_at": datetime.now(PACIFIC).strftime("%Y-%m-%d %H:%M %Z"),
        "backup": str(backup),
        "excluded_source": source,
        "excluded_count": len(excluded),
        "seed_sha": scope_policy.policy_sha(SEED_POLICY),
        "proposals": mine_proposals(backup, excluded),
        "estimates": mine_estimates(backup, excluded),
        "boqs": mine_boqs(backup, excluded),
        "projects": mine_projects(backup, excluded),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_EVAL_ROOT / "reports")
    args = parser.parse_args(argv)
    if not (args.backup / "data" / "projects.json").exists():
        print(f"backup not found at {args.backup}", file=sys.stderr)
        return 2
    result = mine(args.backup)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.out_dir / "scope_policy_mining.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    (args.out_dir / "scope_policy_mining.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    prop = result["proposals"]
    print(f"proposals: {prop['projects']} projects; template lines {prop['template_lines']}")
    print(f"hard-rule share on estimator BOQ rows: {result['boqs']['hard_rule_row_share']:.2%}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
