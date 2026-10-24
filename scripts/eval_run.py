"""Run golden eval cases against a dev sweep backend (docs/AI_ESTIMATING_BUILD_SPEC.md
WS0 step 13; 0.4 ledger rules; 8.5 sweep backend; docs/AI_ESTIMATING_EVAL.md).

This EXTENDS scripts/plans_reader_live_run.py: it imports that script's
`_Auth` (password-grant session for the dev E2E account), `_project` (backup
file rows), label mapping, upload and poll code, and adds the eval harness
around them. The old script keeps working unchanged.

Usage (from bdr_be; the dev database only; the sweep backend of spec 8.5 on
--base-url):

    uv run python scripts/eval_run.py --case 7154 --dry-run --base-url http://127.0.0.1:5061
    uv run python scripts/eval_run.py --tier smoke --workstream WS0 --purpose smoke --budget 45
    uv run python scripts/eval_run.py --cases 7154,7177 --sweep <open sweep id> [--repeat 2]
    uv run python scripts/eval_run.py --tier holdout --holdout-final --workstream WS0 --purpose holdout --budget 73

Flags: --tier smoke|core|holdout, --case / --cases (comma list), --sweep (an
open sweep id; default: open one from --workstream --purpose --budget),
--dry-run (create, upload, estimate, then stop: $0), --no-start (create and
upload only), --repeat N (the noise repeat), --no-cassette (record the sweep
config without a cassette), --holdout-final (required for the holdout tier;
refuses on a dirty git tree), --base-url, --out, --eval-root, --render-mode,
--poll-seconds, --max-minutes, --skip-specs.

Flow per case: blind guard over the uploads (eval_manifest.blind_guard, the
quantity stage for the drawings and specs, the pricing stage for the quotes)
-> create the run named "EVAL <case>" -> record eval_case_id and
eval_sweep_id on the run row (the create route does not take them, so the
runner writes them through the dev service client and refuses the case when
they do not read back; without them the pricing library keeps the case's own
project, GC4) -> upload the case's quantity inputs from the backup with their
labels -> upload the case's vendor quote PDFs through the pricing quote route
(POST /ai-estimating/pricing/runs/{id}/quotes, the material category the
reference records for each quote; the ai_pricing job prices with them, E9)
-> GET the estimate (and skip the case when its maximum exceeds the
sweep's remaining budget minus the reserve) -> start -> poll with token
refresh -> export the bundle (eval_bundle.export_run) -> replay the score
stage and the acceptance suite -> results.json under the sweep directory.
The sweep is closed at the end (eval_ledger.close_sweep).

In the wave this script was written, NO live call is made: the integrator
runs --dry-run first (spec 0.4 rule 4). Everything that talks to a backend
goes through the `Api` protocol so tests exercise the flow with a fake.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.plans_reader_live_run import (  # noqa: E402
    BACKUP,
    INPUT_CATEGORIES,
    LABELS,
    _Auth,
    _load_env,
    _project,
    _scramble,
)

from app.services import eval_bundle, eval_ledger, eval_manifest  # noqa: E402
from app.services import pricing_run  # noqa: E402

TIERS: tuple[str, ...] = ("smoke", "core", "holdout")
RUN_STATUSES_FINAL: frozenset[str] = frozenset({"done", "done_with_errors", "failed", "canceled", "over_cap"})
BUDGET_RESERVE_USD = Decimal("6")
__all__ = [
    "TIERS", "BUDGET_RESERVE_USD", "build_parser", "resolve_cases", "tree_is_dirty", "run_name", "create_run_body",
    "case_inputs", "case_quotes", "sha256_file", "Api", "HttpApi", "CaseResult", "run_case", "sweep_remaining", "main", "BACKUP", "INPUT_CATEGORIES", "LABELS",
    "_Auth", "_project", "_load_env", "_scramble",
]


def _usd(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a dollar amount") from exc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run golden eval cases on the dev sweep backend (AI Estimating WS0).")
    sel = ap.add_argument_group("cases")
    sel.add_argument("--tier", choices=TIERS, help="run every case of a tier (manifest order)")
    sel.add_argument("--case", help="one case id (7154) or project id prefix")
    sel.add_argument("--cases", help="comma-separated case ids")
    sweep = ap.add_argument_group("sweep (spec 0.4)")
    sweep.add_argument("--sweep", help="an open sweep id to attribute the runs to (default: open one)")
    sweep.add_argument("--workstream", help="workstream of the sweep to open (WS0 ...)")
    sweep.add_argument("--purpose", choices=eval_ledger.PURPOSES, help="purpose of the sweep to open")
    sweep.add_argument("--budget", type=_usd, help="budget in USD of the sweep to open")
    sweep.add_argument("--label", default=None, help="label of the sweep to open")
    mode = ap.add_argument_group("mode")
    mode.add_argument("--dry-run", action="store_true", help="create, upload and estimate only ($0)")
    mode.add_argument("--no-start", action="store_true", help="create and upload only (S2 seeding)")
    mode.add_argument("--repeat", type=int, default=1, help="run each case N times (noise repeat)")
    mode.add_argument("--no-cassette", action="store_true", help="record in the sweep config that no cassette is used")
    mode.add_argument("--holdout-final", action="store_true", help="allow the holdout tier (refuses on a dirty tree)")
    mode.add_argument("--allow-dirty-paths", default=None,
                      help="file of path prefixes another build may leave uncommitted during the holdout (recorded in the sweep config)")
    mode.add_argument("--run-id", default=None, help="attach to an existing run: poll, export and score only")
    io_ = ap.add_argument_group("backend and output")
    io_.add_argument("--base-url", default="http://127.0.0.1:5061")
    io_.add_argument("--eval-root", type=Path, default=None, help="defaults to the eval_root setting")
    io_.add_argument("--out", type=Path, default=None, help="sweep output directory (default EVAL_ROOT/sweeps/<sweep>)")
    io_.add_argument("--backup", type=Path, default=BACKUP)
    io_.add_argument("--render-mode", default="adaptive", choices=("adaptive", "tiles_260"))
    io_.add_argument("--poll-seconds", type=float, default=10)
    io_.add_argument("--max-minutes", type=float, default=120)
    io_.add_argument("--skip-specs", action="store_true")
    return ap


# Cases ---------------------------------------------------------------------------------------


def dirty_paths(root: Path = ROOT) -> list[str] | None:
    """The tree's uncommitted paths (git status --porcelain
    --untracked-files=all, so every untracked file is named and an allow
    list can be matched file by file; caches stay out through .gitignore),
    or None when git is absent."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def tree_is_dirty(root: Path = ROOT, *, allow: Iterable[str] = ()) -> bool:
    """True when the backend tree has uncommitted changes outside `allow`,
    so a holdout never runs on code the recorded HEAD sha does not
    contain. `allow` lists path prefixes that may stay dirty (another
    build's uncommitted files in the shared tree, recorded in the sweep
    config); the AI Estimating files are never on it. True when git is
    absent."""
    paths = dirty_paths(root)
    if paths is None:
        return True
    return bool(unallowed_dirty_paths(paths, allow))


def unallowed_dirty_paths(paths: Iterable[str], allow: Iterable[str]) -> list[str]:
    """The dirty paths no allow prefix covers (a directory prefix ends with
    a slash or equals the path)."""
    prefixes = [a.strip() for a in allow if a and a.strip() and not a.strip().startswith("#")]
    out = []
    for p in paths:
        if any(p == a or p.startswith(a.rstrip("/") + "/") or (a.endswith("/") and p.startswith(a)) for a in prefixes):
            continue
        out.append(p)
    return out


def load_allow_dirty(path: str | None) -> list[str]:
    """Path prefixes allowed to stay dirty for a holdout, one per line
    (blank lines and # comments skipped), from --allow-dirty-paths."""
    if not path:
        return []
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip() and not line.strip().startswith("#")]


def _case_id(text: str) -> str:
    text = text.strip()
    if text in eval_manifest.CASE_PREFIX:
        return text
    for prefix, case in eval_manifest.PREFIX_CASE.items():
        if text.startswith(prefix) or prefix.startswith(text[:8]):
            return case
    raise SystemExit(f"unknown case {text!r}; cases are {sorted(eval_manifest.CASE_PREFIX)}")


def resolve_cases(args: argparse.Namespace, *, dirty: bool | None = None) -> list[str]:
    """The case ids to run, in manifest order. The holdout tier needs
    --holdout-final and a clean tree (spec 8.1: it runs once, frozen)."""
    cases: list[str] = []
    if args.tier:
        cases.extend(c for c, _p, _n in eval_manifest.TIER_CASES[args.tier])
    if args.case:
        cases.append(_case_id(args.case))
    if args.cases:
        cases.extend(_case_id(c) for c in args.cases.split(",") if c.strip())
    if not cases:
        raise SystemExit("choose cases with --tier, --case or --cases")
    seen: list[str] = []
    for c in cases:
        if c not in seen:
            seen.append(c)
    holdout = [c for c in seen if eval_manifest.CASE_TIER.get(c) == "holdout"]
    if holdout:
        if not args.holdout_final:
            raise SystemExit(f"holdout cases {holdout} run once at go/no-go: pass --holdout-final")
        allow = load_allow_dirty(getattr(args, "allow_dirty_paths", None))
        is_dirty = dirty if dirty is not None else tree_is_dirty(allow=allow)
        if is_dirty:
            left = unallowed_dirty_paths(dirty_paths() or [], allow) if dirty is None else []
            raise SystemExit("the holdout sweep needs a clean, committed tree (frozen code version)"
                             + (f"; dirty outside the allow list: {left[:12]}" if left else ""))
    return seen


def run_name(case_id: str) -> str:
    """Stable run names: EVAL <case> (the sweep id lives in eval_sweep_id)."""
    return f"EVAL {case_id}"


def create_run_body(case_id: str, *, sweep_id: str | None, render_mode: str = "adaptive") -> dict[str, Any]:
    body: dict[str, Any] = {"name": run_name(case_id), "source": "upload", "render_mode": render_mode, "eval_case_id": case_id}
    if sweep_id:
        body["eval_sweep_id"] = sweep_id
    return body


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def case_inputs(case_id: str, *, backup: Path = BACKUP, skip_specs: bool = False,
                manifest: eval_manifest.Manifest | None = None) -> list[dict[str, Any]]:
    """The quantity-stage uploads of a case: the manifest's quantity inputs
    when a manifest is given, else the backup's PDF rows of the input
    categories (the live-run script's `_project`), labeled by LABELS."""
    out: list[dict[str, Any]] = []
    if manifest is not None:
        case = manifest.case(case_id)
        for inp in case.quantity_inputs:
            if skip_specs and inp.category == "specification":
                continue
            out.append({"filename": inp.filename, "category": inp.category, "sha256": inp.sha256,
                        "label": inp.label, "path": str(backup / "storage" / "project-files" / inp.backup_path)
                        if not Path(inp.backup_path).is_absolute() else inp.backup_path})
        return out
    prefix = eval_manifest.CASE_PREFIX[case_id]
    _proj, rows = _project(prefix) if backup == BACKUP else _project_rows(prefix, backup)
    for f in rows:
        if skip_specs and f["category"] == "specification":
            continue
        path = backup / "storage" / "project-files" / f["storage_path"]
        out.append({"filename": f["filename"], "category": f["category"], "sha256": sha256_file(path),
                    "label": LABELS.get(f["category"]), "path": str(path)})
    return out


def _project_rows(prefix: str, backup: Path) -> tuple[dict, list[dict]]:
    projects = json.loads((backup / "data/projects.json").read_text())
    files = json.loads((backup / "data/project_files.json").read_text())
    proj = next(p for p in projects if p["id"].startswith(prefix))
    rows = [
        f for f in files
        if f["project_id"] == proj["id"] and f["category"] in INPUT_CATEGORIES
        and f["filename"].lower().endswith(".pdf")
        and (backup / "storage/project-files" / f["storage_path"]).exists()
    ]
    return proj, rows


def case_quotes(case_id: str, *, backup: Path = BACKUP, reference: Mapping[str, Any] | None = None,
                manifest: eval_manifest.Manifest | None = None) -> list[dict[str, Any]]:
    """The pricing-stage uploads of a case: its vendor quote PDFs in the
    backup, the same files the replay price stage reconciles
    (`pricing_run.backup_quote_files`: every quote the WS0 reference lists
    under g3_materials, selected and alternatives, with that material
    category; else the project's `quote` rows with the category read from
    the file name). Each entry carries `category` "quote" (what the blind
    guard checks), `material_category` (the form field of the quote route),
    `in_manifest` (the sha256 is one of the manifest's pricing inputs) and
    no dollar value. Files absent from the backup or not PDF are skipped."""
    case = None
    if manifest is not None:
        try:
            case = manifest.case(case_id)
        except KeyError:
            case = None
    project_id = case.project_id if case is not None else eval_manifest.resolve_case_project(case_id)
    pricing_sha = {i.sha256 for i in case.pricing_inputs} if case is not None else set()
    out: list[dict[str, Any]] = []
    for f in pricing_run.backup_quote_files(reference, backup, project_id):
        path = backup / "storage" / "project-files" / f["storage_path"]
        name = str(f.get("filename") or path.name)
        if not name.lower().endswith(".pdf") or not path.is_file():
            continue
        sha = str(f.get("sha256") or "") or sha256_file(path)
        out.append({"filename": name, "category": "quote", "material_category": f["category"], "sha256": sha,
                    "quote_id": f.get("quote_id"), "path": str(path), "in_manifest": sha in pricing_sha})
    return out


# Backend API -----------------------------------------------------------------------------------


class Api(Protocol):
    def status(self) -> dict[str, Any]: ...
    def create_run(self, body: Mapping[str, Any]) -> str: ...
    def stamp_eval_ids(self, run_id: str, *, case_id: str, sweep_id: str | None) -> dict[str, Any]: ...
    def upload(self, run_id: str, path: Path, filename: str, label: str | None) -> int: ...
    def upload_quote(self, run_id: str, path: Path, filename: str, category: str) -> int: ...
    def estimate(self, run_id: str) -> dict[str, Any]: ...
    def start(self, run_id: str) -> int: ...
    def get_run(self, run_id: str) -> dict[str, Any]: ...


class HttpApi:
    """The real backend through the live-run script's auth and upload code.
    `service` is the dev service-role client (eval_ledger.check_dev_url ran
    on its URL) that records the eval ids on the run row."""

    def __init__(self, auth: _Auth, base_url: str, *, timeout: float = 600, service: Any = None) -> None:
        import httpx

        self.auth = auth
        self.base = base_url.rstrip("/") + "/ai-estimating"
        self.client = httpx.Client(timeout=timeout)
        self.service = service

    def status(self) -> dict[str, Any]:
        return self.auth.retry_401(self.client, "GET", f"{self.base}/status").json()

    def create_run(self, body: Mapping[str, Any]) -> str:
        r = self.auth.retry_401(self.client, "POST", f"{self.base}/runs", json=dict(body))
        r.raise_for_status()
        data = r.json()
        return data["id"] if "id" in data else data["run"]["id"]

    def stamp_eval_ids(self, run_id: str, *, case_id: str, sweep_id: str | None) -> dict[str, Any]:
        """Write eval_case_id (and eval_sweep_id) on a run still in status
        created and read them back. POST /runs takes neither field (its
        AiEstimatingRunIn has no eval keys), so the runner writes them on the
        dev database; returns the row's eval ids as read back."""
        if self.service is None:
            return {}
        patch: dict[str, Any] = {"eval_case_id": case_id}
        if sweep_id:
            patch["eval_sweep_id"] = sweep_id
        table = self.service.table("plans_reader_runs")
        table.update(patch).eq("id", run_id).eq("status", "created").execute()
        rows = self.service.table("plans_reader_runs").select("id,eval_case_id,eval_sweep_id").eq("id", run_id).execute().data or []
        return {k: rows[0].get(k) for k in ("eval_case_id", "eval_sweep_id")} if rows else {}

    def upload_quote(self, run_id: str, path: Path, filename: str, category: str) -> int:
        """POST /ai-estimating/pricing/runs/{id}/quotes (multipart file plus
        the material category); 201 stored, 409 already on the run."""
        url = f"{self.base}/pricing/runs/{run_id}/quotes"
        for attempt in range(2):
            with Path(path).open("rb") as fh:
                r = self.client.post(url, headers=self.auth.headers(), data={"category": category},
                                     files={"file": (filename, fh, "application/pdf")})
            if r.status_code == 401 and attempt == 0:
                self.auth._grant("refresh_token", {"refresh_token": self.auth.refresh})
                continue
            if r.status_code == 429 and attempt == 0:
                time.sleep(float(r.headers.get("Retry-After", "10")))
                continue
            break
        return r.status_code

    def upload(self, run_id: str, path: Path, filename: str, label: str | None) -> int:
        data = {"label": label} if label else {}
        with Path(path).open("rb") as fh:
            r = self.client.post(f"{self.base}/runs/{run_id}/files", headers=self.auth.headers(), data=data,
                                 files={"file": (filename, fh, "application/pdf")})
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "10")))
            with Path(path).open("rb") as fh:
                r = self.client.post(f"{self.base}/runs/{run_id}/files", headers=self.auth.headers(), data=data,
                                     files={"file": (filename, fh, "application/pdf")})
        return r.status_code

    def estimate(self, run_id: str) -> dict[str, Any]:
        r = self.auth.retry_401(self.client, "GET", f"{self.base}/runs/{run_id}/estimate")
        return r.json() if r.status_code < 300 else {"error": r.text[:600], "status_code": r.status_code}

    def start(self, run_id: str) -> int:
        return self.auth.retry_401(self.client, "POST", f"{self.base}/runs/{run_id}/start").status_code

    def get_run(self, run_id: str) -> dict[str, Any]:
        r = self.auth.retry_401(self.client, "GET", f"{self.base}/runs/{run_id}")
        return r.json() if r.status_code < 300 else {"error": r.text[:200]}


# One case ---------------------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    run_id: str | None = None
    status: str = "pending"  # pending | uploaded | estimated | skipped_budget | started | done | failed | budget_aborted
    uploads: list[dict[str, Any]] = field(default_factory=list)
    quotes: list[dict[str, Any]] = field(default_factory=list)
    eval_ids: dict[str, Any] = field(default_factory=dict)
    estimate: dict[str, Any] | None = None
    run: dict[str, Any] | None = None
    bundle: str | None = None
    score: dict[str, Any] | None = None
    acceptance: list[dict[str, Any]] | None = None
    error: str | None = None
    timeline: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("case_id", "run_id", "status", "eval_ids", "uploads", "quotes", "estimate",
                                                "run", "bundle", "score", "acceptance", "error", "timeline")}


def _estimate_max(est: Mapping[str, Any]) -> Decimal | None:
    for key in ("cost_usd_max", "estimated_cost_usd_max", "max_cost_usd", "cost_usd", "estimated_cost_usd"):
        if est.get(key) is not None:
            try:
                return Decimal(str(est[key]))
            except InvalidOperation:
                continue
    return None


def run_case(
    api: Api,
    case_id: str,
    *,
    inputs: Sequence[Mapping[str, Any]],
    sweep_id: str | None,
    quotes: Sequence[Mapping[str, Any]] = (),
    case: eval_manifest.Case | None = None,
    answer_hashes: Mapping[str, str] | None = None,
    dry_run: bool = False,
    no_start: bool = False,
    remaining_usd: Decimal | None = None,
    render_mode: str = "adaptive",
    poll_seconds: float = 10,
    max_minutes: float = 120,
    clock: Any = time.time,
    sleep: Any = time.sleep,
    log: Any = print,
) -> CaseResult:
    """Create, record the eval ids, upload the quantity inputs and the
    vendor quotes, estimate and (unless dry) start and poll one case. The
    blind guards (quantity stage for `inputs`, pricing stage for `quotes`)
    run before anything is created; a run whose eval_case_id does not read
    back is `failed` before any upload (its pricing would keep the case's
    own project in the library); a case whose estimate exceeds the
    remaining budget minus the reserve is `skipped_budget`."""
    result = CaseResult(case_id=case_id)
    t0 = clock()
    eval_manifest.blind_guard(case, list(inputs), stage="quantity", answer_hashes=answer_hashes)
    eval_manifest.blind_guard(case, list(quotes), stage="pricing", answer_hashes=answer_hashes)
    run_id = api.create_run(create_run_body(case_id, sweep_id=sweep_id, render_mode=render_mode))
    result.run_id = run_id
    log(f"run {run_id} {run_name(case_id)}")
    result.eval_ids = dict(api.stamp_eval_ids(run_id, case_id=case_id, sweep_id=sweep_id) or {})
    if result.eval_ids.get("eval_case_id") != case_id:
        result.status = "failed"
        result.error = f"eval_case_id did not read back on run {run_id} (got {result.eval_ids.get('eval_case_id')!r})"
        log(result.error)
        return result
    log(f"  eval ids {json.dumps(result.eval_ids)}")
    for inp in inputs:
        code = api.upload(run_id, Path(inp["path"]), str(inp["filename"]), inp.get("label"))
        result.uploads.append({"filename": inp["filename"], "category": inp["category"], "label": inp.get("label"), "status": code})
        log(f"  upload {inp['category']:20s} {str(inp['filename'])[:60]:60s} {code}")
    for q in quotes:
        code = api.upload_quote(run_id, Path(q["path"]), str(q["filename"]), str(q["material_category"]))
        result.quotes.append({"filename": q["filename"], "material_category": q["material_category"],
                              "in_manifest": bool(q.get("in_manifest")), "status": code})
        log(f"  quote  {str(q['material_category']):20s} {str(q['filename'])[:60]:60s} {code}")
    result.timeline["uploaded_s"] = round(clock() - t0)
    result.status = "uploaded"
    if no_start:
        return result
    est = api.estimate(run_id)
    result.estimate = est
    result.status = "estimated"
    log(f"estimate {json.dumps(est)[:400]}")
    if dry_run:
        return result
    maximum = _estimate_max(est)
    if remaining_usd is not None and maximum is not None and maximum > remaining_usd - BUDGET_RESERVE_USD:
        result.status = "skipped_budget"
        result.error = f"estimate max {maximum} exceeds remaining {remaining_usd} minus the {BUDGET_RESERVE_USD} reserve"
        log(result.error)
        return result
    code = api.start(run_id)
    if code >= 300:
        result.status = "failed"
        result.error = f"start returned {code}"
        return result
    result.status = "started"
    result.timeline["started_s"] = round(clock() - t0)
    last = None
    while clock() - t0 < max_minutes * 60:
        sleep(poll_seconds)
        payload = api.get_run(run_id)
        run_row = payload.get("run", payload)
        groups = payload.get("groups", [])
        line = (f"{str(run_row.get('status')):16s} groups {sum(1 for g in groups if g.get('status') == 'done')}/{len(groups)} "
                f"tokens in {run_row.get('input_tokens')} out {run_row.get('output_tokens')} cost {run_row.get('cost_usd')}")
        if line != last:
            log(f"[{round(clock() - t0):5d}s] {line}")
            last = line
        if run_row.get("status") in RUN_STATUSES_FINAL:
            result.run = {k: run_row.get(k) for k in ("status", "input_tokens", "cached_tokens", "output_tokens",
                                                       "reasoning_tokens", "cost_usd", "estimated_cost_usd", "error",
                                                       "documents_total", "groups_total", "groups_done", "groups_failed")}
            error = str(run_row.get("error") or "")
            if "budget" in error.lower() or "sweep" in error.lower():
                result.status = "budget_aborted"
            else:
                result.status = "done" if run_row.get("status") in ("done", "done_with_errors") else "failed"
            break
    else:
        result.status = "failed"
        result.error = "poll timed out"
    result.timeline["finished_s"] = round(clock() - t0)
    if result.status == "done":
        # The post-run chain (pricing, review, revise, re-price; spec 4.1)
        # runs after the run reads done and its calls need the sweep that
        # this runner closes on return, so wait for it to settle first.
        result.timeline["chain_s"] = _wait_post_run_chain(api, run_id, clock=clock, sleep=sleep, poll_seconds=poll_seconds, log=log)
    return result


POST_RUN_KINDS = ("review", "revise", "modify")
POST_RUN_MAX_MINUTES = 30.0
POST_RUN_GRACE_S = 90.0


def _wait_post_run_chain(api, run_id: str, *, clock=time.monotonic, sleep=time.sleep, poll_seconds: float = 20.0,
                         log=print, max_minutes: float = POST_RUN_MAX_MINUTES) -> int:
    """Poll until no review, revise or modify group of the run is queued or
    running. A chain that has not started yet gets POST_RUN_GRACE_S to
    appear (the pricing job is queued right after the run closes). Returns
    the seconds waited; never raises."""
    t0 = clock()
    seen_active = False
    last = ""
    while clock() - t0 < max_minutes * 60:
        try:
            payload = api.get_run(run_id)
        except Exception as exc:  # noqa: BLE001 - a poll failure never ends the wait early
            log(f"post-run chain poll failed: {str(exc)[:120]}")
            sleep(poll_seconds)
            continue
        groups = payload.get("groups", [])
        active = [g for g in groups if g.get("kind") in POST_RUN_KINDS and g.get("status") in ("queued", "running")]
        line = "post-run chain " + (", ".join(f"{g.get('kind')}:{g.get('status')}" for g in active) or "idle")
        if line != last:
            log(f"[{round(clock() - t0):5d}s] {line}")
            last = line
        if active:
            seen_active = True
        elif seen_active or clock() - t0 >= POST_RUN_GRACE_S:
            break
        sleep(poll_seconds)
    return round(clock() - t0)


# Export, score, acceptance -----------------------------------------------------------------------


def export_and_score(sb: Any, result: CaseResult, *, eval_root: Path, backup: Path, settings: Any = None) -> CaseResult:
    """Export the finished run's bundle, replay the score stage and run the
    acceptance suite; dollar figures stay under EVAL_ROOT. Safe to repeat on
    the same run: a re-export replaces the bundle's members in the existing
    directory (eval_bundle.export_run) and the post_run replay is rewritten
    from scratch, so no artifact of an earlier export (say one taken before
    the pricing and review chain landed) survives into the new score."""
    import shutil

    from app.services import eval_reference as R
    from app.services import plans_reader_acceptance as acceptance
    from app.services import plans_reader_replay as replay_mod

    if result.run_id is None or result.status != "done":
        return result
    out = eval_root / "bundles" / result.case_id / result.run_id[:8]
    eval_bundle.export_run(sb, result.run_id, out, case_id=result.case_id, settings=settings)
    post_run = out / "replays" / "post_run"
    if post_run.is_dir():
        shutil.rmtree(post_run)
    result.bundle = str(out)
    bundle = eval_bundle.load_bundle(out)
    try:
        reference = R.load_reference(eval_root, result.case_id)
    except FileNotFoundError:
        reference = None
    expected_path = eval_root / "references" / result.case_id / "expected.json"
    expected = json.loads(expected_path.read_text()) if expected_path.is_file() else None
    rep = replay_mod.replay(bundle, reference=reference, expected=expected, case_id=result.case_id,
                            out_dir=out / "replays" / "post_run", backup_dir=backup)
    rep.write()
    result.score = rep.artifacts.get("score")
    inp = acceptance.inputs_from_replay(rep, reference=reference, expected=expected, case_id=result.case_id,
                                        docs=replay_mod.LocalDocBytes(bundle, backup_dir=backup))
    result.acceptance = acceptance.results_table(acceptance.run_checks(inp))
    return result


# Main --------------------------------------------------------------------------------------------


def sweep_remaining(report: Mapping[str, Any], sweep_id: str | None) -> Decimal | None:
    """The open sweep's budget minus its spend, from eval_ledger.report;
    the global remaining when the sweep is not listed."""
    for s in report.get("sweeps") or []:
        if str(s.get("id")) == str(sweep_id):
            return Decimal(str(s.get("budget_usd") or 0)) - Decimal(str(s.get("spent_usd") or 0))
    value = report.get("remaining_usd")
    return None if value is None else Decimal(str(value))


def _sweep_config(args: argparse.Namespace, cases: Iterable[str]) -> dict[str, Any]:
    return {
        "cases": list(cases),
        "repeat": args.repeat,
        "cassette": not args.no_cassette,
        "render_mode": args.render_mode,
        "holdout_final": bool(args.holdout_final),
        "allow_dirty_paths": load_allow_dirty(getattr(args, "allow_dirty_paths", None)),
        "dirty_paths_allowed": [p for p in (dirty_paths() or []) if not unallowed_dirty_paths([p], load_allow_dirty(getattr(args, "allow_dirty_paths", None)))] if getattr(args, "allow_dirty_paths", None) else [],
        "base_url": args.base_url,
    }


def _reference(eval_root: Path, case_id: str) -> dict[str, Any] | None:
    """The case's WS0 reference (its g3_materials list the quote files), or
    None when it was never built (quotes then come from the backup rows)."""
    from app.services import eval_reference as R

    try:
        return R.load_reference(eval_root, case_id)
    except (FileNotFoundError, ValueError):
        return None


def attach_run(sb: Any, args: Any, cases: list[str], eval_root: Path) -> int:
    """`--run-id`: export and score an existing dev run ($0: no sweep, no
    backend, no auth). The case comes from --case, else the run's
    eval_case_id. A run still in flight is reported as such and not scored.
    Results go to EVAL_ROOT/sweeps/attach_<run8>_<time>/results.json."""
    run_id = str(args.run_id)
    rows = sb.table("plans_reader_runs").select("id, status, name, eval_case_id, error, cost_usd").eq("id", run_id).execute().data or []
    if not rows:
        raise SystemExit(f"run {run_id} not found on the dev database")
    row = rows[0]
    case_id = (args.case or row.get("eval_case_id") or (cases[0] if cases else None))
    if not case_id:
        raise SystemExit("--run-id needs --case (the run carries no eval_case_id)")
    out_dir = Path(args.out) if args.out else eval_root / "sweeps" / f"attach_{run_id[:8]}_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    res = CaseResult(case_id=str(case_id), run_id=run_id, eval_ids={"eval_case_id": row.get("eval_case_id")})
    res.run = {k: row.get(k) for k in ("status", "error", "cost_usd")}
    if row.get("status") in ("done", "done_with_errors"):
        res.status = "done"
        res = export_and_score(sb, res, eval_root=eval_root, backup=args.backup)
    else:
        res.status = "failed"
        res.error = f"run status {row.get('status')}: not final, nothing scored"
    (out_dir / "results.json").write_text(json.dumps([res.to_dict()], indent=1, default=str))
    sc = res.score or {}
    print(f"run {run_id} {res.status}: count lines exact {sc.get('count_lines_exact')} of {sc.get('count_lines_total')}, "
          f"dollar coverage {sc.get('dollar_coverage')}; acceptance "
          + ", ".join(f"{a.get('id')} {a.get('status')}" for a in (res.acceptance or []) if a.get("status") == "fail")
          + f"; results at {out_dir / 'results.json'}")
    return 0 if res.status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    global print
    print = functools.partial(print, flush=True)  # noqa: A001
    cases = resolve_cases(args)
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    live = not (args.dry_run or args.no_start or args.run_id)
    if live and not args.sweep and not (args.workstream and args.purpose and args.budget):
        raise SystemExit("a live run needs --sweep <open sweep id> or --workstream, --purpose and --budget to open one")
    env = _load_env()
    eval_ledger.check_dev_url(env.get("SUPABASE_URL"))
    eval_root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    manifest = None
    try:
        manifest = eval_manifest.load_manifest(eval_root)
    except (FileNotFoundError, KeyError, ValueError):
        print("manifest unavailable; uploads resolved from the backup rows")
    from supabase import create_client

    if args.run_id:
        return attach_run(create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"]), args, cases, eval_root)

    # The dev service client (URL checked above) records the eval ids on
    # every run, dry and no-start included; the ledger and the export use it
    # on live runs only.
    service = create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    sb = service if live else None
    sweep_id = args.sweep
    sweep_dir = None
    if live:
        if sweep_id is None:
            row = eval_ledger.open_sweep(
                sb, label=args.label or f"eval_run {args.tier or ','.join(cases)}", workstream=args.workstream,
                purpose=args.purpose, budget_usd=args.budget, cases=cases, config=_sweep_config(args, cases),
            )
            sweep_id = str(row["id"])
            print("opened sweep", sweep_id)
        sweep_dir = Path(args.out) if args.out else eval_root / "sweeps" / sweep_id
    else:
        sweep_dir = Path(args.out) if args.out else eval_root / "sweeps" / f"dry_{int(time.time())}"
    sweep_dir.mkdir(parents=True, exist_ok=True)
    auth = _Auth(env)
    api = HttpApi(auth, args.base_url, service=service)
    results: list[CaseResult] = []
    try:
        status = api.status()
        print("status", json.dumps(status)[:400])
        if live and not (status.get("eval_ledger") or {}).get("enforced"):
            raise SystemExit("the backend does not enforce the eval ledger (AI_EVAL_LEDGER_ENFORCE=true on the sweep backend)")
        for case_id in cases:
            case = manifest.case(case_id) if manifest else None
            inputs = case_inputs(case_id, backup=args.backup, skip_specs=args.skip_specs, manifest=manifest)
            quotes = case_quotes(case_id, backup=args.backup, reference=_reference(eval_root, case_id), manifest=manifest)
            print(f"{case_id}: {len(inputs)} quantity inputs, {len(quotes)} vendor quotes")
            for repeat in range(args.repeat):
                print(f"== {case_id} (run {repeat + 1} of {args.repeat})")
                remaining = None
                if live and sb is not None:
                    try:
                        remaining = sweep_remaining(eval_ledger.report(sb), sweep_id)
                    except Exception as exc:  # noqa: BLE001 - the guard still applies on the backend
                        print("ledger report unavailable:", exc)
                try:
                    res = run_case(api, case_id, inputs=inputs, sweep_id=sweep_id, quotes=quotes, case=case,
                                   answer_hashes=manifest.answer_sha256 if manifest else None, dry_run=args.dry_run,
                                   no_start=args.no_start, remaining_usd=remaining, render_mode=args.render_mode,
                                   poll_seconds=args.poll_seconds, max_minutes=args.max_minutes)
                except eval_manifest.BlindViolation as exc:
                    res = CaseResult(case_id=case_id, status="failed", error=f"blind guard: {exc}")
                if live and sb is not None:
                    res = export_and_score(sb, res, eval_root=eval_root, backup=args.backup)
                results.append(res)
                (sweep_dir / "results.json").write_text(json.dumps([r.to_dict() for r in results], indent=1, default=str))
        return 0
    finally:
        (sweep_dir / "results.json").write_text(json.dumps([r.to_dict() for r in results], indent=1, default=str))
        if live and sb is not None and sweep_id and not args.sweep:
            try:
                eval_ledger.close_sweep(sb, sweep_id, result_path=str(sweep_dir / "results.json"))
                print("closed sweep", sweep_id)
            except Exception as exc:  # noqa: BLE001 - reported, the ledger CLI can close it
                print("close_sweep failed:", exc)
        _scramble(env)
        print("E2E password scrambled; results at", sweep_dir / "results.json")


if __name__ == "__main__":
    raise SystemExit(main())
