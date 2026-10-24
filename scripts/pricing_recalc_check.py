"""Recalc check of the AI priced workbook in LibreOffice (AI_ESTIMATING_BUILD_SPEC
WS6 acceptance: "priced xlsx recalc BASE BID equals bid_total").

The priced workbook (app/services/pricing_render.py) writes the recap value
rows, formulas over them (TOTALS, the BID SUMMARY chain) and BASE BID PRICE
as the Verify draft's bid total. openpyxl writes formulas without cached
values, so only a spreadsheet engine can prove the formulas close on the
Verify number. This script opens the workbook with LibreOffice inside the
running `bdr-gotenberg` container (the host has no soffice; recipe in the
memory note gotenberg-libreoffice-recalc), reads the recalculated values back
with openpyxl and asserts, to the cent:

- recap TOTAL COST WITH OVERHEADS + PROFIT + BID SECURITY == BASE BID PRICE;
- BASE BID PRICE == the Verify bid total (the expected value);
- the Checks sheet's recalc formula (recap_base_bid_equals_verify) == 0.00;
- no cell of any sheet holds a spreadsheet error (#REF!, #VALUE!, ...).

Usage (from bdr_be; $0, no model, no database, no backend):

    uv run python scripts/pricing_recalc_check.py                 # the 7154 case
    uv run python scripts/pricing_recalc_check.py --case 7154 --keep
    uv run python scripts/pricing_recalc_check.py --xlsx priced.xlsx --bid-total 1234.56

Default mode builds the 7154 priced workbook at $0 the way the replay price
stage prices the stored weekend bundle: the stored BOQ v1, a cost library
from the local backup with eval_manifest.excluded_projects("pricing_library",
"7154") (holdout and the case's own project left out), the code policy with
the expected.json seed amounts, the reference wage type, the cited material
note facts, and both backup vendor quotes parsed by code and reconciled (the
stored L5 EA line blocks with uom_conflict_quote; the workbook still renders,
labeled BLOCKED in its basis column). `--xlsx` checks any priced workbook
(for example one downloaded from GET /ai-estimating/pricing/versions/{id}/xlsx)
against `--bid-total` (its verify_draft.bid_total).

When docker is missing or the container is not running the script prints
`SKIP` and exits 0 (a skip is not a failure; the integrator starts the
container and runs it). Exit 1 on a failed check, 2 on a usage or build
error. The JSON report and, with --keep, the recalculated workbook go under
EVAL_ROOT/reports/pricing_recalc/ (they carry real project dollars; stdout
prints only differences in cents and pass or fail).
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CONTAINER = "bdr-gotenberg"
WORK_DIR = "/tmp/bdr_pricing_recalc"
DEFAULT_EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
DEFAULT_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
BUNDLES = {"7154": "bundles/7154/ff8fad25"}
TIMEOUT_S = 240
CENT = Decimal("0.01")
ERROR_VALUES = ("#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#N/A", "#NUM!", "#NULL!", "Err:")
LABEL_TOTAL = "TOTAL COST WITH OVERHEADS + PROFIT"
LABEL_SECURITY = "BID SECURITY"
LABEL_BASE = "BASE BID PRICE"
CHECK_NAME = "recap_base_bid_equals_verify"
EXIT_OK, EXIT_FAIL, EXIT_ERROR = 0, 1, 2


class Skip(RuntimeError):
    """The recalc path is unavailable (no docker, container not running)."""


# The LibreOffice round trip ------------------------------------------------------------------------


def _run(cmd: Sequence[str], *, timeout: int = TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, check=False)


def container_running(container: str = CONTAINER) -> bool:
    """True when docker answers and the container is running."""
    if shutil.which("docker") is None:
        return False
    try:
        res = _run(["docker", "inspect", "-f", "{{.State.Running}}", container], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and res.stdout.strip() == "true"


def recalc_in_container(xlsx: bytes, *, container: str = CONTAINER) -> bytes:
    """Round-trip the workbook through soffice in the container (formulas get
    computed and cached) and return the converted bytes. Raises Skip when the
    container is not available and RuntimeError when soffice fails."""
    if not container_running(container):
        raise Skip(f"docker container {container} is not running")
    job = f"{WORK_DIR}/{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "priced.xlsx"
        src.write_bytes(xlsx)
        steps = [
            ["docker", "exec", container, "sh", "-c", f"rm -rf {job} && mkdir -p {job}/out"],
            ["docker", "cp", str(src), f"{container}:{job}/priced.xlsx"],
            ["docker", "exec", container, "sh", "-c",
             f'cd {job} && HOME={job} soffice --headless --norestore --calc '
             f'--convert-to xlsx:"Calc MS Excel 2007 XML" --outdir {job}/out {job}/priced.xlsx'],
            ["docker", "cp", f"{container}:{job}/out/priced.xlsx", str(Path(tmp) / "recalc.xlsx")],
        ]
        try:
            for cmd in steps:
                res = _run(cmd)
                if res.returncode != 0:
                    raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(res.stderr or res.stdout).strip()[:300]}")
            out = Path(tmp) / "recalc.xlsx"
            if not out.is_file():
                raise RuntimeError("soffice produced no output workbook")
            return out.read_bytes()
        finally:
            _run(["docker", "exec", container, "sh", "-c", f"rm -rf {job}"], timeout=30)


# The checks ------------------------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - text in a number cell is reported, not raised
        return None


def _label_rows(ws: Any, *, max_col: int = 3) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in ws.iter_rows(min_col=1, max_col=max_col):
        for c in row:
            if isinstance(c.value, str) and c.value.strip():
                out.setdefault(c.value.strip(), c.row)
    return out


def _cents(a: Decimal | None, b: Decimal | None) -> int | None:
    if a is None or b is None:
        return None
    return int(((a - b) / CENT).quantize(Decimal(1)))


def check_recalculated(xlsx: bytes, bid_total: Decimal | float | str) -> dict[str, Any]:
    """The four checks over a RECALCULATED workbook (cached values present).
    Returns {ok, checks[{name, ok, diff_cents|detail}], values} where values
    carry the dollar figures (for the EVAL_ROOT report only)."""
    from openpyxl import load_workbook

    from app.services import pricing_render as pr
    from app.services.plans_reader_render import RECAP_SHEET

    want = Decimal(str(bid_total)).quantize(CENT)
    wb = load_workbook(io.BytesIO(xlsx), data_only=True)
    checks: list[dict[str, Any]] = []
    values: dict[str, Any] = {"bid_total": str(want)}
    if RECAP_SHEET not in wb.sheetnames:
        return {"ok": False, "checks": [{"name": "recap_sheet", "ok": False, "detail": f"no sheet {RECAP_SHEET!r}"}],
                "values": values}
    recap = wb[RECAP_SHEET]
    labels = _label_rows(recap)
    missing = [lb for lb in (LABEL_TOTAL, LABEL_SECURITY, LABEL_BASE) if lb not in labels]
    if missing:
        return {"ok": False, "checks": [{"name": "recap_labels", "ok": False, "detail": f"missing {missing}"}],
                "values": values}
    total = _dec(recap.cell(row=labels[LABEL_TOTAL], column=5).value)
    security = _dec(recap.cell(row=labels[LABEL_SECURITY], column=5).value) or Decimal(0)
    base = _dec(recap.cell(row=labels[LABEL_BASE], column=5).value)
    values.update({"total_with_ohp": None if total is None else str(total), "bid_security": str(security),
                   "base_bid": None if base is None else str(base)})
    summary = None if total is None else (total + security).quantize(CENT)
    checks.append({"name": "summary_equals_base_bid", "ok": summary is not None and base is not None
                   and summary == base.quantize(CENT), "diff_cents": _cents(summary, base)})
    checks.append({"name": "base_bid_equals_bid_total", "ok": base is not None and base.quantize(CENT) == want,
                   "diff_cents": _cents(base, want)})
    check_value = None
    if pr.CHECKS_SHEET in wb.sheetnames:
        ws = wb[pr.CHECKS_SHEET]
        for row in ws.iter_rows(min_col=1, max_col=2):
            if row[0].value == CHECK_NAME:
                check_value = _dec(row[1].value)
    values["checks_cell"] = None if check_value is None else str(check_value)
    checks.append({"name": "checks_sheet_formula_zero", "ok": check_value is not None and check_value.quantize(CENT) == 0,
                   "diff_cents": _cents(check_value, Decimal(0))})
    errors: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith(ERROR_VALUES):
                    errors.append(f"{ws.title}!{c.coordinate}={c.value}")
    checks.append({"name": "no_formula_errors", "ok": not errors, "detail": errors[:20]})
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "values": values}


# Building the 7154 workbook at $0 ------------------------------------------------------------------


def build_case_workbook(case_id: str, *, eval_root: Path, backup: Path) -> tuple[bytes, Decimal, dict[str, Any]]:
    """The case's priced workbook from its stored bundle (module docstring).
    Returns (xlsx bytes, the Verify bid total, a summary without dollars)."""
    from app.services import eval_bundle, eval_manifest
    from app.services import eval_reference as R
    from app.services import pricing_engine as pe
    from app.services import pricing_render as pr
    from app.services import pricing_run as prun
    from app.services import plans_reader_replay
    from app.services.cost_library import CostLibrary
    from app.services.estimating_contracts import WAGE_TYPES
    from app.services.pricing_policy import PricingPolicy
    from app.services.pricing_types import PricingContext

    if case_id not in BUNDLES:
        raise ValueError(f"no bundle is registered for case {case_id!r} (known: {sorted(BUNDLES)})")
    reference = R.load_reference(eval_root, case_id)
    expected = json.loads((eval_root / "references" / case_id / "expected.json").read_text())
    bundle = eval_bundle.load_bundle(eval_root / BUNDLES[case_id])
    art = plans_reader_replay.stored_artifacts(bundle)
    boq_spec = art.get("boq_spec")
    if not isinstance(boq_spec, dict):
        raise ValueError("the bundle holds no BOQ deliverable")
    rows = prun.boq_rows(boq_spec)
    project_id = str(bundle.run.get("project_id") or reference.get("project_id") or "") or None
    excluded = set(eval_manifest.excluded_projects(prun.LIBRARY_PURPOSE, case_id)) | ({project_id} if project_id else set())
    library = CostLibrary.from_backup(backup, exclude_projects=excluded)
    policy = prun._seeded_from_expected(PricingPolicy(), expected)
    wage = (reference.get("conditions") or {}).get("wage_type")
    wage = wage if wage in WAGE_TYPES else None
    main = art.get("main") if isinstance(art.get("main"), dict) else {}
    facts = prun.spec_facts_from_material_notes(main)
    extras = ((main or {}).get("estimate") or {}).get("extra_allowances") or ()
    ctx = PricingContext(
        wage_type=wage, labor_rate_usd=policy.labor_rates.get(wage or policy.default_wage_type, Decimal(0)),
        labor_rate_source="policy", rate_overrides=policy.rate_overrides, spec_facts=tuple(facts),
        allowances=prun.standard_allowances(extra=extras, document_amounts=()), policy_version=str(policy.version),
        exclude_project_ids=frozenset(library.excluded_projects), eval_case_id=case_id,
        tax_rate_pct=policy.tax_rate_pct, labor_markup_pct=policy.labor_markup_pct,
        hour_roundup_steps=policy.hour_roundup_steps,
    )
    notes: list[str] = []
    qctx = SimpleNamespace(backup_dir=backup, reference=reference, note=notes.append)
    quote_rows = prun.backup_quote_rows(qctx, project_id)
    quotes = prun.quotes_for_pricing(quote_rows, rows, policy=policy, run_id=str(bundle.run.get("id") or ""))
    priced = pe.compute_estimate(
        rows, library, policy, quotes["refs"], ctx, exclude_project=project_id, pricing_allowed=(True, []),
        selected_quote_ids=quotes["human_selected"], quote_reconciliations=quotes["reconciliations"],
    )
    prun.attach_quote_details(priced, quotes)
    if quotes["blocked_line_ids"]:
        prun.apply_quote_blocks(priced, quotes["blocked_line_ids"], "the vendor quote prices these lines per foot")
    if priced.verify is None:
        raise ValueError("the engine returned no Verify draft")
    estimate = {"project_name": f"Case {case_id}", **dict((main or {}).get("estimate") or {})}
    xlsx, layout = pr.render_priced_estimate_xlsx(
        boq_spec, estimate, priced, date=dt.date.today(), policy=policy, version=1, quote_labels=quotes["labels"],
    )
    summary = {
        "case_id": case_id, "status": priced.status, "lines": len(priced.lines), "quotes": len(quotes["refs"]),
        "blockers": [b.code for b in priced.blockers], "recap_labels": len(layout.get("labels") or ()),
        "library_projects": library.n_projects, "notes": notes,
    }
    return xlsx, priced.verify.bid_total, summary


# Main ------------------------------------------------------------------------------------------------


def _write_report(eval_root: Path, name: str, report: dict[str, Any], recalc: bytes | None) -> Path:
    out_dir = eval_root / "reports" / "pricing_recalc"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{name}_{stamp}.json"
    path.write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + "\n")
    if recalc is not None:
        (out_dir / f"{name}_{stamp}_recalc.xlsx").write_bytes(recalc)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", default="7154", help="case whose stored bundle is priced (default 7154)")
    ap.add_argument("--xlsx", type=Path, help="check this priced workbook instead of building one")
    ap.add_argument("--bid-total", help="the Verify bid total the --xlsx workbook must close on")
    ap.add_argument("--eval-root", type=Path, default=DEFAULT_EVAL_ROOT)
    ap.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--keep", action="store_true", help="also keep the recalculated workbook under EVAL_ROOT")
    args = ap.parse_args(argv)

    if not container_running(args.container):
        print(f"SKIP: docker container {args.container} is not running (start it, then run this check again)")
        return EXIT_OK
    try:
        if args.xlsx is not None:
            if args.bid_total is None:
                print("ERROR: --xlsx needs --bid-total (the version's verify_draft.bid_total)")
                return EXIT_ERROR
            xlsx = args.xlsx.read_bytes()
            bid_total = Decimal(str(args.bid_total))
            summary: dict[str, Any] = {"xlsx": args.xlsx.name}
            name = args.xlsx.stem
        else:
            xlsx, bid_total, summary = build_case_workbook(args.case, eval_root=args.eval_root, backup=args.backup)
            name = f"case_{args.case}"
        recalc = recalc_in_container(xlsx, container=args.container)
    except Skip as exc:
        print(f"SKIP: {exc}")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - a build or soffice failure is an error, not a check failure
        print(f"ERROR: {type(exc).__name__}: {str(exc)[:300]}")
        return EXIT_ERROR
    result = check_recalculated(recalc, bid_total)
    report = {"summary": summary, **result, "container": args.container,
              "checked_at": dt.datetime.now().astimezone().isoformat()}
    path = _write_report(args.eval_root, name, report, recalc if args.keep else None)
    for c in result["checks"]:
        extra = f" diff_cents={c['diff_cents']}" if "diff_cents" in c else (f" detail={c['detail']}" if c.get("detail") else "")
        print(f"{'PASS' if c['ok'] else 'FAIL'} {c['name']}{extra}")
    print(f"{'PASS' if result['ok'] else 'FAIL'}: {sum(c['ok'] for c in result['checks'])} of {len(result['checks'])} "
          f"checks; status {summary.get('status', 'n/a')}; report {path}")
    return EXIT_OK if result["ok"] else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
