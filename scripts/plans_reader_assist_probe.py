"""WS3 measure assist probe (docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS3
"Live $6 (assist picks on 6 golden pool projects)", step B11; 0.4 ledger
rules).

For six tagged golden pool projects the probe builds the WS3 measure output
at $0 from the production backup PDFs (WS1 facts, `build_measure_safe`,
code-default conventions), seeds an in-memory MeasureService under a
synthetic run id, and runs the derive-stage assist
(`plans_reader_measure_assist.run_assist`, feature plans_derive, step
derive, per-run cap `plans_reader_measure_assist_max_usd`) over the linear
fixtures the code is unsure of. The assist serves the WS1
`find_linear_items` tool in-process (registry dispatch over the run's
MeasureService and the backup PDF bytes) and every model call goes through
`llm.complete_turn`, which books the eval ledger itself; the probe's
`record_call` only collects usage.

Project pick (deterministic): the tagged training split of the WS3 survey
(`EVAL_ROOT/manifest/ws3_split.json`), golden pool only (never core or
smoke), holdout excluded and 7154 excluded
(`eval_manifest.excluded_projects("calibration", "7154")`), ordered by
sha256 of a fixed salt plus the project id; the first six projects that
have at least one uncertain linear item a model would be asked about are
taken (projects without one are listed as skipped with the reason).
`--projects` names the projects instead (the same exclusions refuse).

Usage (from bdr_be):

    # $0: build the measure outputs, list the uncertain items per project
    # and the estimate; no sweep needed, no model call.
    uv run python scripts/plans_reader_assist_probe.py --dry-run [--n 6] [--out DIR]
    # Live (wave 4 only): inside an open sweep of workstream WS3, purpose dev_probe.
    uv run python scripts/plans_reader_assist_probe.py --sweep open

A live run refuses without --sweep (an id, or `open`), a sweep that is not
open, not of purpose dev_probe or not of workstream WS3, a derive route
model without a verified llm_pricing row, a database other than dev, and a
plan whose worst case (per project the smaller of the per-run cap and the
summed call reserves) exceeds what the sweep has left. It arms the process
(EVAL_SWEEP_ID, AI_EVAL_LEDGER_ENFORCE=true, LLM_CASSETTE_MODE=
record_missing for dev_probe, rule 6) before its first call and writes
results.json and table.md under
EVAL_ROOT/sweeps/<sweep>/assist_probe_<time>/.

Recorded per project: uncertain linear items, items asked, picks, none_fit,
rejected picks, cost, and per item the length change against the code
value; per tag the code and assisted FT next to the estimator's quantity
for that tag (reference.json quantity lines when the project has one,
else the estimator BOQ parsed by the golden scorer). Quantities and API
costs only; never a project price.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLAN_SCHEMA = "bdr.eval.assist_probe_plan/1"
RESULTS_SCHEMA = "bdr.eval.assist_probe/1"
DEV_REF = "bpidntbyvoooqvaispup"
SWEEP_PURPOSE = "dev_probe"
WORKSTREAM = "WS3"
CASSETTE_MODE = "record_missing"
DEFAULT_N = 6
SELECTION_SALT = "ws3-assist-probe-1"
EXCLUSION_PURPOSE = "calibration"
EXCLUDED_CASE = "7154"
RUN_PREFIX = "assist-probe-"
CACHE_DIR = "assist_probe"
# Planning figure for one assist answer (a few ids, a short note and the
# reasoning tokens of the derive effort); the reserve stays the request max.
EXPECTED_OUTPUT_TOKENS = 800
DEFAULT_MAX_PAGES = 120


class ProbeError(RuntimeError):
    """The probe cannot go ahead as asked (usage, preflight, ledger)."""


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def order_key(project_id: str) -> str:
    return hashlib.sha256((SELECTION_SALT + project_id).encode()).hexdigest()


def excluded_ids() -> frozenset[str]:
    """Holdout plus 7154 (the WS3 acceptance fixture is never a probe case)."""
    from app.services import eval_manifest

    return eval_manifest.excluded_projects(EXCLUSION_PURPOSE, EXCLUDED_CASE)


def load_split(root: Path, path: Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else root / "manifest" / "ws3_split.json"
    if not p.is_file():
        raise ProbeError(f"{p} not found; run scripts/plans_reader_measure_eval.py --survey first")
    doc = json.loads(p.read_text())
    if doc.get("schema") != "bdr.ws3_split/1":
        raise ProbeError(f"{p} is not a bdr.ws3_split/1 file")
    return doc


def candidate_ids(split: Mapping[str, Any], *, excluded: Iterable[str], pool: Iterable[str]) -> list[str]:
    """The tagged training split, golden pool only, minus the exclusions,
    in the probe's fixed order."""
    drop, keep = set(excluded), set(pool)
    ids = {str(p) for p in split.get("tagged_training") or []}
    return sorted((p for p in ids if p in keep and p not in drop), key=order_key)


def resolve_requested(wanted: Sequence[str], candidates: Sequence[str], *, excluded: Iterable[str]) -> list[str]:
    """Project ids for `--projects` prefixes: each must name exactly one
    candidate; an excluded project (holdout, 7154) refuses."""
    drop = set(excluded)
    out: list[str] = []
    for w in wanted:
        if any(x.startswith(w) for x in drop):
            raise ProbeError(f"project {w} is excluded (holdout or 7154)")
        hits = [c for c in candidates if c.startswith(w)]
        if len(hits) != 1:
            raise ProbeError(f"project {w} does not name exactly one tagged golden pool project of the split")
        if hits[0] not in out:
            out.append(hits[0])
    return out


# ---------------------------------------------------------------------------
# Measure output at $0
# ---------------------------------------------------------------------------


@dataclass
class Prepared:
    project_id: str
    name: str
    run_id: str
    measure: dict[str, Any] = field(default_factory=dict)
    svc: Any = None
    pdfs: dict[str, bytes] = field(default_factory=dict)
    items: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source: str = ""
    elapsed_s: float = 0.0
    error: str | None = None

    @property
    def linear_fixtures(self) -> int:
        return sum(1 for it in self.measure.get("linear_items") or [] if it.get("kind") == "linear_fixture")


def _settings_for_assist(settings: Any) -> Any:
    """The probe's in-process settings: the assist flag on (this script is
    the dev probe of the flag), everything else as configured."""
    return settings.model_copy(update={"plans_reader_measure_assist_enabled": True})


def build_measure_for(data: Any, project_id: str, *, root: Path, max_pages: int, use_cache: bool) -> tuple[dict[str, Any], dict[str, bytes], list[str], str]:
    """(measure output, pdf bytes by document id, notes, source) for one
    backup project; the output is cached under EVAL_ROOT/cache/assist_probe
    keyed by the WS3 code fingerprint."""
    from app.services import plans_reader_measure as M
    from app.services import plans_reader_routes as routes_mod
    from scripts import plans_reader_measure_eval as measure_eval

    facts_cache = None if not use_cache else root / "cache" / "pagefacts_survey"
    docs: list[Any] = []
    pdfs: dict[str, bytes] = {}
    notes: list[str] = []
    files = [r for r in data.files(project_id) if r.get("category") in measure_eval.DRAWING_CATEGORIES]
    for i, row in enumerate(files, start=1):
        facts, note = measure_eval.facts_for_file(data, row, cache_dir=facts_cache, max_pages=max_pages)
        if facts is None:
            notes.append(f"{row.get('filename')}: {note}")
            continue
        doc_id = str(row.get("id") or f"doc{i}")
        docs.append(M.MeasureDocument(document_id=doc_id, doc_index=i, facts=facts, label=facts.label, name=facts.name))
        pdfs[doc_id] = data.file_path(row).read_bytes()
    fingerprint = measure_eval.code_fingerprint()
    cpath = root / "cache" / CACHE_DIR / f"{project_id}.json"
    if use_cache and cpath.exists():
        try:
            cached = json.loads(cpath.read_text())
        except ValueError:
            cached = None
        if isinstance(cached, Mapping) and cached.get("code") == fingerprint and isinstance(cached.get("measure"), Mapping):
            return dict(cached["measure"]), pdfs, notes, "cache"
    project_type = (data.project(project_id) or {}).get("project_type")
    inputs = M.MeasureInputs.from_pdfs(None, docs, pdfs, project_type=project_type, eval_case_id=project_id)
    measure = M.build_measure_safe(inputs, routes_mod.Conventions())
    if use_cache:
        try:
            text = json.dumps({"code": fingerprint, "project_id": project_id, "measure": measure}, separators=(",", ":"))
        except (TypeError, ValueError):
            text = None
        if text is not None:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(text)
    return measure, pdfs, notes, "built"


def prepare_project(data: Any, project_id: str, *, settings: Any, root: Path, max_pages: int = DEFAULT_MAX_PAGES, use_cache: bool = True) -> Prepared:
    """Measure output at $0, an in-memory MeasureService under a synthetic
    run id, and the items the assist would select."""
    from app.services import plans_reader_measure as M
    from app.services import plans_reader_measure_assist as A
    from app.services import plans_reader_routes as routes_mod

    name = str((data.project(project_id) or {}).get("name") or "")
    prep = Prepared(project_id=project_id, name=name, run_id=f"{RUN_PREFIX}{project_id[:8]}")
    started = time.monotonic()
    try:
        measure, pdfs, notes, source = build_measure_for(data, project_id, root=root, max_pages=max_pages, use_cache=use_cache)
    except Exception as exc:  # noqa: BLE001 - one broken project is skipped, never fatal
        prep.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        prep.elapsed_s = round(time.monotonic() - started, 1)
        return prep
    svc = M.MeasureService(M.MemoryMeasureStore(), conv=routes_mod.Conventions())
    svc.register_run(prep.run_id, measure)
    latest = svc.latest(prep.run_id)
    prep.measure = dict(latest[1]) if latest else dict(measure)
    prep.svc = svc
    prep.notes, prep.source = notes, source
    max_items = int(getattr(settings, "plans_reader_measure_assist_max_items", 8) or 0)
    prep.items = A.select_items(prep.measure, max_items=max_items)
    wanted = {it.document_id for it in prep.items}
    prep.pdfs = {k: v for k, v in pdfs.items() if k in wanted}
    prep.elapsed_s = round(time.monotonic() - started, 1)
    return prep


# ---------------------------------------------------------------------------
# Preview and estimate ($0)
# ---------------------------------------------------------------------------


def derive_route(settings: Any, route_snapshot: Mapping[str, Any] | None) -> Any:
    from app.services import plans_reader_model_routes as model_routes

    snapshot = {"derive": dict(route_snapshot)} if isinstance(route_snapshot, Mapping) else None
    return model_routes.resolve_step("derive", settings, snapshot=snapshot)


def _input_tokens(parts: Sequence[Any], provider: str) -> int:
    """The token count `estimate_call_usd` prices (system and schema text
    plus the parts)."""
    from app.services import llm_parts
    from app.services import plans_reader_measure_assist as A

    text_tokens = math.ceil((len(A.ASSIST_SYSTEM) + len(str(A.ASSIST_SCHEMA))) / 3)
    return text_tokens + llm_parts.estimate_part_tokens(parts, provider)


def preview_project(prep: Prepared, *, settings: Any, route: Any) -> dict[str, Any]:
    """What the assist would do on this project, without a call: the same
    tool dispatch, candidate filter, overlay and request parts as
    run_assist, the call reserve (request maximum) and the expected cost
    (EXPECTED_OUTPUT_TOKENS), under the per-run cap with the expected
    spend. Returns the items, the count it would ask and the dollars."""
    from app.services import llm_parts, llm_pricing
    from app.services import plans_reader_measure_assist as A
    from app.services import plans_reader_tools as tools_mod

    cap = Decimal(str(getattr(settings, "plans_reader_measure_assist_max_usd", 0) or 0))
    out: dict[str, Any] = {"items": [], "would_ask": 0, "reserve_usd": Decimal("0"), "expected_usd": Decimal("0"), "worst_usd": Decimal("0"), "cap_usd": cap, "priced": llm_pricing.price_for(route.model) is not None}
    if not prep.items:
        return out
    ctx = tools_mod.ToolContext(
        run_id=prep.run_id,
        kind="derive",
        services=tools_mod.ToolServices(measure=prep.svc, documents=A._PdfDocuments(prep.pdfs)),
        image_spec=llm_parts.image_spec_for(route.provider),
    )
    find_spec = tools_mod.get_spec("find_linear_items")
    if find_spec is None:
        raise ProbeError("the WS3 find_linear_items tool is not registered")
    spent = Decimal("0")
    reserves = Decimal("0")
    stopped = False
    for item in prep.items:
        row: dict[str, Any] = {"key": item.key, "measure_id": item.measure_id, "tag": item.tag, "sheet_key": item.sheet_key, "reason": item.reason, "tag_count": item.tag_count, "candidates": 0, "outcome": None, "input_tokens": None, "reserve_usd": None, "expected_usd": None}
        if stopped:
            row["outcome"] = "budget"
            out["items"].append(row)
            continue
        found = tools_mod.dispatch(find_spec, ctx, {"document_id": item.document_id, "page": item.page, "tag": item.tag})
        if found.refused:
            row["outcome"] = f"tool_refused:{found.code}"
            out["items"].append(row)
            if found.code == "max_calls":
                stopped = True
            continue
        candidates = [c for c in found.data.get("candidates") or [] if c.get("bbox") and c.get("id")]
        row["candidates"] = len(candidates)
        view = next((v for v in found.views if isinstance(v.get("image"), (bytes, bytearray))), None)
        if not candidates:
            row["outcome"] = "no_candidates"
        elif len(candidates) > A.MAX_CANDIDATES:
            row["outcome"] = "too_many_candidates"
        elif view is None:
            row["outcome"] = "no_overlay"
        if row["outcome"]:
            out["items"].append(row)
            continue
        png, w_px, h_px = A.fit_image(bytes(view["image"]), route.provider)
        item_row = next((it for it in prep.measure.get("linear_items") or [] if str(it.get("measure_id")) == item.measure_id), None)
        prompt = A.build_prompt(item, candidates, A._app_choice(item_row, candidates))
        parts = (llm_parts.TextPart(prompt), llm_parts.ImagePart(png=png, w_px=w_px, h_px=h_px, view_id=view.get("view_id")))
        est = A.estimate_call_usd(route.model, route.provider, parts)
        tokens = _input_tokens(parts, route.provider)
        row["input_tokens"] = tokens
        if est is None:
            row["outcome"] = "unpriced_model"
            out["items"].append(row)
            stopped = True
            continue
        expected = llm_pricing.estimate_cost(route.model, input_tokens=tokens, output_tokens=EXPECTED_OUTPUT_TOKENS) or est
        expected = min(expected, est)
        row["reserve_usd"], row["expected_usd"] = est, expected
        if spent + est > cap:
            row["outcome"] = "budget"
            stopped = True
            out["items"].append(row)
            continue
        row["outcome"] = "ask"
        spent += expected
        reserves += est
        out["would_ask"] += 1
        out["items"].append(row)
    out["reserve_usd"] = reserves
    out["expected_usd"] = spent
    # Live, each call is admitted only while spent plus its reserve fits the
    # cap, so a project never spends more than the smaller of the two.
    out["worst_usd"] = min(cap, reserves) if out["would_ask"] else Decimal("0")
    return out


def pick_projects(
    ids: Sequence[str],
    *,
    n: int,
    prepare: Callable[[str], Prepared],
    preview: Callable[[Prepared], dict[str, Any]],
    log: Callable[[str], None] = print,
    require_ask: bool = True,
) -> tuple[list[tuple[Prepared, dict[str, Any]]], list[dict[str, Any]]]:
    """Walk `ids` in order and take the first `n` projects with at least one
    item the assist would ask about (every project when `require_ask` is
    off). Returns (picked [(prepared, preview)], skipped rows)."""
    picked: list[tuple[Prepared, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    for pid in ids:
        if len(picked) >= n:
            break
        prep = prepare(pid)
        if prep.error:
            skipped.append({"project_id": pid, "name": prep.name, "why": f"measure failed: {prep.error}"})
            log(f"{pid[:8]} skipped: measure failed ({prep.error})")
            continue
        pv = preview(prep)
        if require_ask and not pv["would_ask"]:
            why = "no uncertain linear item" if not prep.items else "no item reaches a model (" + ", ".join(f"{k} {v}" for k, v in sorted(Counter(i["outcome"] for i in pv["items"]).items())) + ")"
            skipped.append({"project_id": pid, "name": prep.name, "why": why, "linear_fixtures": prep.linear_fixtures, "uncertain": len(prep.items)})
            log(f"{pid[:8]} skipped: {why} ({prep.linear_fixtures} linear fixtures, {prep.source} in {prep.elapsed_s} s)")
            continue
        picked.append((prep, pv))
        log(f"{pid[:8]} picked: {len(prep.items)} uncertain, {pv['would_ask']} to ask ({prep.source} in {prep.elapsed_s} s)")
    return picked, skipped


def build_plan(picked: Sequence[tuple[Prepared, dict[str, Any]]], skipped: Sequence[Mapping[str, Any]], *, route: Any, n: int, candidates: int) -> dict[str, Any]:
    rows = []
    for prep, pv in picked:
        rows.append({
            "project_id": prep.project_id,
            "name": prep.name,
            "run_id": prep.run_id,
            "linear_fixtures": prep.linear_fixtures,
            "uncertain": len(prep.items),
            "reasons": dict(sorted(Counter(i.reason for i in prep.items).items())),
            "would_ask": pv["would_ask"],
            "outcomes": dict(sorted(Counter(i["outcome"] for i in pv["items"]).items())),
            "items": pv["items"],
            "expected_usd": pv["expected_usd"],
            "worst_usd": pv["worst_usd"],
            "cap_usd": pv["cap_usd"],
            "source": prep.source,
            "notes": prep.notes,
        })
    expected = sum((r["expected_usd"] for r in rows), Decimal("0"))
    worst = sum((r["worst_usd"] for r in rows), Decimal("0"))
    return {
        "schema": PLAN_SCHEMA,
        "route": {"provider": route.provider, "model": route.model, "effort": route.effort, "explicit": route.explicit},
        "priced": _priced(route.model),
        "n_wanted": n,
        "candidates": candidates,
        "projects": rows,
        "skipped": list(skipped),
        "expected_usd": expected.quantize(Decimal("0.01")),
        "worst_usd": worst.quantize(Decimal("0.01")),
        "model_calls_made": 0,
    }


def _priced(model: str) -> bool:
    from app.services import llm_pricing

    return llm_pricing.price_for(model) is not None


def _money(v: Any) -> str:
    return "n/a" if v is None else f"${Decimal(str(v)):.2f}"


def format_plan(plan: Mapping[str, Any]) -> list[str]:
    r = plan["route"]
    lines = [
        f"Assist probe plan: {len(plan['projects'])} of {plan['n_wanted']} projects picked from {plan['candidates']} candidates "
        f"(tagged golden pool, holdout and 7154 excluded)",
        f"derive route: {r['provider']} {r['model']} {r['effort']}" + ("" if plan["priced"] else "  (NOT PRICED: a live run refuses it)"),
        "project | name | linear fixtures | uncertain | reasons | to ask | outcomes | expected | worst case",
    ]
    for p in plan["projects"]:
        reasons = ", ".join(f"{k} {v}" for k, v in p["reasons"].items())
        outcomes = ", ".join(f"{k} {v}" for k, v in p["outcomes"].items())
        lines.append(
            f"{p['project_id'][:8]} | {p['name'][:28]} | {p['linear_fixtures']} | {p['uncertain']} | {reasons} | "
            f"{p['would_ask']} | {outcomes} | {_money(p['expected_usd'])} | {_money(p['worst_usd'])}"
        )
        for it in p["items"]:
            lines.append(
                f"    {it['tag']} on {it['sheet_key'] or '?'}: {it['reason']}, {it['candidates']} candidates, {it['outcome']}"
                + (f", reserve {_money(it['reserve_usd'])}" if it.get("reserve_usd") is not None else "")
            )
    for s in plan["skipped"]:
        lines.append(f"skipped {s['project_id'][:8]} {str(s.get('name') or '')[:28]}: {s['why']}")
    lines.append(
        f"expected {_money(plan['expected_usd'])}, worst case (per project the per-run cap or the summed reserves) "
        f"{_money(plan['worst_usd'])}"
    )
    lines.append("model calls made: 0")
    return lines


# ---------------------------------------------------------------------------
# Estimator reference (quantities only)
# ---------------------------------------------------------------------------


def _reference_lines(root: Path, project_id: str) -> list[dict[str, Any]] | None:
    """Quantity lines of an EVAL_ROOT reference.json for the project
    (adjudicated first), reduced to section, description, unit and qty."""
    refs = root / "references"
    if not refs.is_dir():
        return None
    for path in sorted(refs.glob("*/reference.json")):
        try:
            doc = json.loads(path.read_text())
        except ValueError:
            continue
        if str(doc.get("project_id")) != project_id:
            continue
        lines = doc.get("quantity_lines_adjudicated") or doc.get("quantity_lines") or []
        return [{"section": ln.get("section"), "description": ln.get("description"), "unit": ln.get("unit"), "qty": ln.get("qty")} for ln in lines if isinstance(ln, Mapping)]
    return None


def _boq_lines(data: Any, project_id: str) -> list[dict[str, Any]] | None:
    from app.services import eval_reference
    from app.services import plans_reader_score as score

    row = eval_reference.pick_boq_file(data, project_id)
    if row is None:
        return None
    try:
        rows = score.parse_estimator_boq(data.file_path(row).read_bytes())
    except Exception:  # noqa: BLE001 - an unreadable workbook reads as no reference
        return None
    return [{"section": r.section, "description": r.description, "unit": r.unit, "qty": r.quantity} for r in rows if not r.is_accessory]


def estimator_linear(data: Any, root: Path, project_id: str, tags: Iterable[str]) -> dict[str, Any]:
    """{"source", "tags": {tag: {"ft", "ea", "rows"}}} for the given tags:
    non-demolition estimator lines whose parsed identity carries the tag."""
    from app.services.electrical_items import parse_item

    wanted = {str(t).upper() for t in tags if t}
    lines = _reference_lines(root, project_id)
    source = "reference.json"
    if lines is None:
        lines = _boq_lines(data, project_id) if data is not None else None
        source = "estimator BOQ"
    if lines is None:
        return {"source": None, "tags": {}}
    out: dict[str, dict[str, Any]] = {}
    for ln in lines:
        if str(ln.get("section") or "").upper() == "DEMOLITION":
            continue
        unit = str(ln.get("unit") or "").upper()
        try:
            qty = float(ln.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        tag = str(parse_item(str(ln.get("description") or ""), unit).attrs.get("tag") or "").upper()
        if tag not in wanted:
            continue
        rec = out.setdefault(tag, {"ft": 0.0, "ea": 0.0, "rows": 0})
        rec["rows"] += 1
        if unit == "FT":
            rec["ft"] = round(rec["ft"] + qty, 2)
        elif unit == "EA":
            rec["ea"] = round(rec["ea"] + qty, 2)
    return {"source": source, "tags": out}


# ---------------------------------------------------------------------------
# The live run
# ---------------------------------------------------------------------------


def arm_sweep(sweep: str, *, eval_root: Path, settings: Any = None) -> dict[str, Any]:
    """Check the sweep (dev database, exists, open, purpose dev_probe,
    workstream WS3) and arm this process for it (spec 0.4 rules 2 and 6).
    Returns the sweep row from eval_ledger.report with `remaining_usd`."""
    from app.core.config import get_settings
    from app.services import eval_ledger

    s = settings or get_settings()
    url = str(getattr(s, "supabase_url", "") or "")
    if DEV_REF not in url:
        raise ProbeError(f"SUPABASE_URL must point at the dev project {DEV_REF}; refusing")
    rep = eval_ledger.report()
    wanted = rep.get("open_sweep_id") if sweep == "open" else sweep
    if not wanted:
        raise ProbeError("no sweep is open; open one with scripts/eval_ledger.py open --workstream WS3 --purpose dev_probe")
    try:
        wanted = str(uuid.UUID(str(wanted)))
    except ValueError as exc:
        raise ProbeError(f"--sweep must be a sweep id or 'open', got {sweep!r}") from exc
    row = next((r for r in rep.get("sweeps") or [] if str(r.get("id")) == wanted), None)
    if row is None:
        raise ProbeError(f"sweep {wanted} not found")
    if row.get("status") != "open":
        raise ProbeError(f"sweep {wanted} is {row.get('status')}, not open")
    if row.get("purpose") != SWEEP_PURPOSE:
        raise ProbeError(f"sweep {wanted} has purpose {row.get('purpose')}; the assist probe needs a sweep of purpose {SWEEP_PURPOSE}")
    if row.get("workstream") != WORKSTREAM:
        raise ProbeError(f"sweep {wanted} belongs to {row.get('workstream')}; the assist probe spends {WORKSTREAM}'s allocation")
    remaining = Decimal(str(row.get("budget_usd") or 0)) - Decimal(str(row.get("spent_usd") or 0))
    os.environ["EVAL_SWEEP_ID"] = wanted
    os.environ["AI_EVAL_LEDGER_ENFORCE"] = "true"
    os.environ["LLM_CASSETTE_MODE"] = CASSETTE_MODE
    os.environ["LLM_CASSETTE_DIR"] = str(eval_root / "cassettes")
    get_settings.cache_clear()
    return {**row, "remaining_usd": remaining}


def make_collector(sink: list[dict[str, Any]]) -> Callable[..., None]:
    """A `record_call` that only collects usage (complete_turn books the
    ledger and llm_call_log itself)."""
    from app.services import llm

    def _record(*, item: Any, turn: Any, error: Exception | None, est_usd: Decimal, cost_usd: Decimal, duration_ms: int, model: str) -> None:
        usage = getattr(turn, "usage", None) if turn is not None else llm.last_usage()
        try:
            replayed = bool(llm.last_call_replayed())
        except Exception:  # noqa: BLE001 - informational only
            replayed = False
        sink.append({
            "key": item.key,
            "tag": item.tag,
            "model": getattr(turn, "model", None) or model,
            "response_id": getattr(turn, "response_id", None),
            "input_tokens": getattr(usage, "input_tokens", None),
            "cached_tokens": getattr(usage, "cached_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "reasoning_tokens": getattr(usage, "reasoning_tokens", None),
            "est_usd": float(est_usd),
            "cost_usd": float(cost_usd),
            "duration_ms": duration_ms,
            "replayed": replayed,
            "error": f"{type(error).__name__}: {str(error)[:300]}" if error is not None else None,
        })

    return _record


def _ft(v: Any) -> float | None:
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _tag_totals(measure: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for it in measure.get("linear_items") or []:
        if it.get("kind") != "linear_fixture":
            continue
        tag = str(it.get("tag") or "").upper()
        rec = out.setdefault(tag, {"ft": 0.0, "unmeasured": 0, "items": 0})
        rec["items"] += 1
        ft = _ft(it.get("total_ft"))
        if ft is None:
            rec["unmeasured"] += 1
        else:
            rec["ft"] = round(rec["ft"] + ft, 2)
    return out


def run_project(prep: Prepared, *, settings: Any, route_snapshot: Mapping[str, Any] | None, data: Any, root: Path, assist: Callable[..., Any] | None = None) -> dict[str, Any]:
    """run_assist on one prepared project and the per-item and per-tag
    comparison against the code values and the estimator."""
    from app.services import plans_reader_measure_assist as A

    fn = assist or A.run_assist
    before = {str(it.get("measure_id")): it for it in prep.measure.get("linear_items") or []}
    tags_before = _tag_totals(prep.measure)
    calls: list[dict[str, Any]] = []
    started = time.monotonic()
    res = fn(prep.run_id, svc=prep.svc, pdfs=prep.pdfs, settings=settings, route_snapshot=route_snapshot, record_call=make_collector(calls))
    latest = prep.svc.latest(prep.run_id)
    after_measure = latest[1] if latest else prep.measure
    after = {str(it.get("measure_id")): it for it in after_measure.get("linear_items") or []}
    items = []
    for o in res.items:
        mid = str(o.get("measure_id"))
        code_ft = _ft((before.get(mid) or {}).get("total_ft"))
        new_ft = _ft((after.get(mid) or {}).get("total_ft")) if o.get("outcome") == "picked" else code_ft
        delta = None if code_ft is None or new_ft is None else round(new_ft - code_ft, 2)
        items.append({
            "key": o.get("key"), "tag": o.get("tag"), "sheet_key": o.get("sheet_key"), "reason": o.get("reason"),
            "candidates": o.get("candidates"), "outcome": o.get("outcome"), "picks": list(o.get("picks") or []),
            "rejected": list(o.get("rejected") or []), "note": o.get("note"), "cost_usd": o.get("cost_usd"),
            "code_ft": code_ft, "assist_ft": new_ft, "delta_ft": delta,
            "change": "measured" if code_ft is None and new_ft is not None else ("none" if delta in (None, 0) else "changed"),
        })
    tags_after = _tag_totals(after_measure)
    est = estimator_linear(data, root, prep.project_id, tags_after)
    tags = []
    for tag in sorted(tags_after):
        e = est["tags"].get(tag) or {}
        est_ft = e.get("ft") or None
        code_ft, new_ft = tags_before.get(tag, {}).get("ft"), tags_after[tag]["ft"]
        tags.append({
            "tag": tag, "items": tags_after[tag]["items"], "code_ft": code_ft, "assist_ft": new_ft,
            "unmeasured_code": tags_before.get(tag, {}).get("unmeasured"), "unmeasured_assist": tags_after[tag]["unmeasured"],
            "estimator_ft": est_ft, "estimator_ea": e.get("ea") or None,
            "code_err": round((code_ft - est_ft) / est_ft, 4) if est_ft and code_ft is not None else None,
            "assist_err": round((new_ft - est_ft) / est_ft, 4) if est_ft else None,
        })
    outcomes = Counter(i["outcome"] for i in items)
    return {
        "project_id": prep.project_id,
        "name": prep.name,
        "run_id": prep.run_id,
        "status": res.status,
        "message": res.message,
        "uncertain": len(prep.items),
        "asked": res.calls,
        "picked_items": outcomes.get("picked", 0),
        "picks": sum(len(i["picks"]) for i in items),
        "none_fit": outcomes.get("none_fit", 0),
        "rejected_picks": sum(len(i["rejected"]) for i in items),
        "overrides": res.overrides,
        "cost_usd": round(float(res.spent_usd or 0), 6),
        "outcomes": dict(sorted(outcomes.items())),
        "items": items,
        "tags": tags,
        "estimator_source": est["source"],
        "calls": calls,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def run_live(picked: Sequence[tuple[Prepared, dict[str, Any]]], *, settings: Any, route_snapshot: Mapping[str, Any] | None, remaining_usd: Decimal, data: Any, root: Path, assist: Callable[..., Any] | None = None, log: Callable[[str], None] = print) -> tuple[list[dict[str, Any]], list[str]]:
    """Projects in pick order; a project starts only while the sweep has
    its worst case left. Returns (results, skipped project ids)."""
    left = Decimal(str(remaining_usd))
    results: list[dict[str, Any]] = []
    skipped: list[str] = []
    for prep, pv in picked:
        need = Decimal(str(pv["worst_usd"]))
        if need > left:
            skipped.append(prep.project_id)
            log(f"{prep.project_id[:8]} skipped: worst case {_money(need)} > {_money(left)} left in the sweep")
            continue
        try:
            row = run_project(prep, settings=settings, route_snapshot=route_snapshot, data=data, root=root, assist=assist)
        except Exception as exc:  # noqa: BLE001 - one failed project never stops the probe
            from app.services import llm_errors

            if isinstance(exc, llm_errors.EvalBudgetRefused):
                raise
            row = {"project_id": prep.project_id, "name": prep.name, "status": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}", "cost_usd": 0.0, "items": [], "tags": [], "calls": []}
        results.append(row)
        left -= Decimal(str(row.get("cost_usd") or 0))
        log(f"{prep.project_id[:8]} {row['status']}: asked {row.get('asked', 0)}, picked {row.get('picked_items', 0)}, "
            f"none_fit {row.get('none_fit', 0)}, rejected {row.get('rejected_picks', 0)}, cost {row['cost_usd']:.4f}")
    return results, skipped


def _cell(v: Any, fmt: str = "{:.1f}") -> str:
    return "-" if v is None else fmt.format(v)


def format_table(results: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| project | name | status | uncertain | asked | picked items | picks | none_fit | rejected picks | cost $ |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['project_id'][:8]} | {str(r.get('name') or '')[:28]} | {r.get('status')} | {r.get('uncertain', 0)} | "
            f"{r.get('asked', 0)} | {r.get('picked_items', 0)} | {r.get('picks', 0)} | {r.get('none_fit', 0)} | "
            f"{r.get('rejected_picks', 0)} | {float(r.get('cost_usd') or 0):.4f} |"
        )
    total = sum(float(r.get("cost_usd") or 0) for r in results)
    lines.append(f"| total | | | {sum(r.get('uncertain', 0) for r in results)} | {sum(r.get('asked', 0) for r in results)} | "
                 f"{sum(r.get('picked_items', 0) for r in results)} | {sum(r.get('picks', 0) for r in results)} | "
                 f"{sum(r.get('none_fit', 0) for r in results)} | {sum(r.get('rejected_picks', 0) for r in results)} | {total:.4f} |")
    lines += ["", "Per item (length change vs the code value):", "",
              "| project | tag | sheet | reason | candidates | outcome | picks | rejected | code FT | assist FT | change FT |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        for i in r.get("items") or []:
            rejected = ", ".join(f"{x.get('id')} ({x.get('why')})" for x in i.get("rejected") or [])
            lines.append(
                f"| {r['project_id'][:8]} | {i.get('tag')} | {i.get('sheet_key') or ''} | {i.get('reason')} | {i.get('candidates')} | "
                f"{i.get('outcome')} | {', '.join(i.get('picks') or []) or '-'} | {rejected or '-'} | {_cell(i.get('code_ft'))} | "
                f"{_cell(i.get('assist_ft'))} | {_cell(i.get('delta_ft'), '{:+.1f}')} |"
            )
    lines += ["", "Per tag against the estimator (quantities only):", "",
              "| project | tag | items | code FT | assist FT | estimator FT | estimator EA | code error | assist error | source |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        for t in r.get("tags") or []:
            lines.append(
                f"| {r['project_id'][:8]} | {t['tag']} | {t['items']} | {_cell(t.get('code_ft'))} | {_cell(t.get('assist_ft'))} | "
                f"{_cell(t.get('estimator_ft'))} | {_cell(t.get('estimator_ea'), '{:.0f}')} | {_cell(t.get('code_err'), '{:+.0%}')} | "
                f"{_cell(t.get('assist_err'), '{:+.0%}')} | {r.get('estimator_source') or '-'} |"
            )
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_route(text: str | None) -> dict[str, Any] | None:
    """`provider:model[:effort]` as a derive route snapshot, or None."""
    raw = (text or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) not in (2, 3) or not parts[1]:
        raise ProbeError("--route must be provider:model[:effort]")
    provider = parts[0].strip().lower()
    if provider not in ("openai", "anthropic"):
        raise ProbeError("--route provider must be openai or anthropic")
    out: dict[str, Any] = {"provider": provider, "model": parts[1].strip(), "tools": False, "explicit": True}
    if len(parts) == 3 and parts[2]:
        out["effort"] = parts[2].strip().lower()
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WS3 measure assist probe on tagged golden pool projects (AI Estimating B11).")
    p.add_argument("--dry-run", action="store_true", help="build the measure outputs, print the plan and the estimate; no call ($0)")
    p.add_argument("--sweep", default=None, help="open sweep id of workstream WS3 and purpose dev_probe, or 'open' (required live)")
    p.add_argument("--n", type=int, default=DEFAULT_N, help="projects to pick (default 6)")
    p.add_argument("--projects", default=None, help="comma list of project ids or prefixes instead of the pick")
    p.add_argument("--route", default=None, help="derive route snapshot provider:model[:effort] (default: the configured derive route)")
    p.add_argument("--split", type=Path, default=None, help="ws3_split.json (default EVAL_ROOT/manifest)")
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--backup", type=Path, default=None, help="production backup dir (default eval_reference.DEFAULT_BACKUP_DIR)")
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="skip drawing PDFs with more pages")
    p.add_argument("--no-cache", action="store_true", help="rebuild facts and measure outputs")
    p.add_argument("--out", type=Path, default=None, help="output directory (default EVAL_ROOT/sweeps/<sweep>/assist_probe_<time>)")
    return p


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes>"
    return str(obj)


def main(
    argv: list[str] | None = None,
    *,
    settings: Any = None,
    data: Any = None,
    prepare: Callable[[str], Prepared] | None = None,
    assist: Callable[..., Any] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    from app.core.config import get_settings
    from app.services import eval_manifest

    s = settings or get_settings()
    root = Path(args.eval_root or getattr(s, "eval_root", "") or "/Users/thomasmoore/Desktop/BDR/backups/eval")
    try:
        if args.n < 1:
            raise ProbeError("--n must be at least 1")
        route_snapshot = parse_route(args.route)
        s_assist = _settings_for_assist(s)
        route = derive_route(s_assist, route_snapshot)
        excluded = excluded_ids()
        split = load_split(root, args.split)
        ids = candidate_ids(split, excluded=excluded, pool=eval_manifest.TIER_IDS["pool"])
        require_ask = True
        if args.projects:
            wanted = [w.strip() for w in args.projects.split(",") if w.strip()]
            ids = resolve_requested(wanted, ids, excluded=excluded)
            require_ask = False
        if data is None and prepare is None:
            from app.services.eval_reference import DEFAULT_BACKUP_DIR, BackupData

            data = BackupData(args.backup or DEFAULT_BACKUP_DIR)
        prep_fn = prepare or (lambda pid: prepare_project(data, pid, settings=s_assist, root=root, max_pages=args.max_pages, use_cache=not args.no_cache))
        picked, skipped = pick_projects(
            ids, n=args.n, prepare=prep_fn, preview=lambda p: preview_project(p, settings=s_assist, route=route), require_ask=require_ask,
        )
        plan = build_plan(picked, skipped, route=route, n=args.n, candidates=len(ids))
        for line in format_plan(plan):
            print(line)
        if args.dry_run:
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / "plan.json").write_text(json.dumps(plan, indent=1, default=_json_default))
                print(f"plan: {args.out / 'plan.json'}")
            return 0
        if not args.sweep:
            raise ProbeError(
                "a live probe needs --sweep <open sweep id of workstream WS3, purpose dev_probe> or --sweep open "
                "(spec 0.4 rule 2: every live call runs inside an open sweep); use --dry-run for the plan"
            )
        if not plan["priced"]:
            raise ProbeError(f"no verified llm_pricing row for {route.model} (spec 0.4 rule 3)")
        if not picked:
            raise ProbeError("no project has an item the assist would ask about")
        sweep = arm_sweep(args.sweep, eval_root=root, settings=s)
        s = get_settings() if settings is None else s
        s_assist = _settings_for_assist(s)
        remaining = Decimal(str(sweep["remaining_usd"]))
        if plan["worst_usd"] > remaining:
            raise ProbeError(
                f"the plan's worst case ({_money(plan['worst_usd'])}) exceeds what sweep {sweep['id']} has left "
                f"({_money(remaining)}); narrow it with --n or --projects or open a larger sweep"
            )
        started = time.time()
        results, skipped_live = run_live(picked, settings=s_assist, route_snapshot=route_snapshot, remaining_usd=remaining, data=data, root=root, assist=assist)
        out_dir = args.out or root / "sweeps" / str(sweep["id"]) / f"assist_probe_{time.strftime('%Y%m%d_%H%M%S')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": RESULTS_SCHEMA,
            "sweep_id": sweep["id"],
            "route": plan["route"],
            "plan": plan,
            "results": results,
            "skipped_for_budget": skipped_live,
            "elapsed_s": round(time.time() - started, 1),
            "spent_usd": round(sum(float(r.get("cost_usd") or 0) for r in results), 6),
        }
        (out_dir / "results.json").write_text(json.dumps(payload, indent=1, default=_json_default))
        table = format_table(results)
        (out_dir / "table.md").write_text("\n".join(table) + "\n")
        for line in table:
            print(line)
        print(f"results: {out_dir / 'results.json'}")
        return 0
    except ProbeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "Prepared", "ProbeError", "arm_sweep", "build_plan", "candidate_ids", "estimator_linear", "format_plan",
    "format_table", "load_split", "main", "make_collector", "order_key", "parse_route", "pick_projects",
    "prepare_project", "preview_project", "resolve_requested", "run_live", "run_project",
]


if __name__ == "__main__":
    raise SystemExit(main())
