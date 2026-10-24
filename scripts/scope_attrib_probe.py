"""WS5 live probe: the capped scope attribution fallback on 8 golden
projects (docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS5 "attribution
fallback on 8 projects $3"; 0.4 ledger rules 2, 3 and 6).

For each picked project the ScopeContext is built from the backup's text
documents (specification, addendum and other PDFs, the run labels
specifications, addenda and other) exactly as `scope_engine.prepare` builds
it at $0 (`prepare_context` over a ScopeInputs whose PDFs come from the
backup), then the attribution fallback runs ONE capped call per project as
prepare runs it with allow_llm=True: `scope_attribution.llm_fallback` over
the uncertain code obligations (lowest confidence first, capped at
AI_SCOPE_ATTRIB_MAX_STATEMENTS), through `scope_attribution.default_llm_caller`
(feature plans_scope_attrib, step scope_attrib, `llm.complete_turn`; the
route follows the main route's model at effort low, spec 8.3). A project
without an uncertain statement makes no call (prepare would not either).

Recorded per project: uncertain statements, statements sent
(min(cap, uncertain)), accepted attributions (quote verified against the
statement and the page), rejected ones by reason, the call's cost, and the
change in excluded obligations against the code-only attribution (counts and
the obligation ids that moved).

Selection (deterministic, holdout never inside): the golden tiers smoke and
core in spec 8.1 order, then the pool in id order, minus
`eval_manifest.excluded_projects("scope_policy")`; a project qualifies when
it has at least one specification PDF in the backup; first --n (8).
Misfiled inputs (eval_manifest.CASE_NOTES) and files whose names mark an
answer (eval_manifest.ANSWER_NAME_RE) are left out.

Usage (from bdr_be):

    # $0: the plan (projects, documents, statements, uncertain, sent, the
    # estimate); no sweep needed, no call made.
    uv run python scripts/scope_attrib_probe.py --dry-run
    # Live (wave 4 only): inside an open sweep of purpose dev_probe on WS5.
    PLANS_READER_ENABLED=true uv run python scripts/scope_attrib_probe.py --sweep <id|open>

A live run refuses without --sweep, with a sweep that is not open, not of
purpose dev_probe or not on WS5, with an unpriced model (rule 3), with
PLANS_READER_ENABLED off, against a database other than dev, and when the
plan's expected dollars exceed what the sweep has left (capped by
--max-usd, default $3). It arms the process (EVAL_SWEEP_ID,
AI_EVAL_LEDGER_ENFORCE=true, LLM_CASSETTE_MODE=record_missing for
dev_probe, rule 6) before its first call. Each call's ledger reserve is the
request's input plus --reserve-output-tokens of output (the production
request itself is unchanged, max_tokens included); a project starts only
while the dollars left cover that reserve, and a ledger refusal stops the
run. Results: EVAL_ROOT/sweeps/<sweep>/scope_attrib_probe_<time>/results.json
and table.md (counts, ids and the build's own API cost; no project prices).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for _p in (ROOT, SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import eval_probe_common as common  # noqa: E402
from eval_probe_common import ProbeError  # noqa: E402

NAME = "scope_attrib_probe"
RESULTS_SCHEMA = "bdr.eval.scope_attrib_probe/1"
WORKSTREAM = "WS5"
BUDGET_USD = Decimal("3")
DEFAULT_N = 8
EXCLUSION_PURPOSE = "scope_policy"
TIERS: tuple[str, ...] = common.PROBE_TIERS
# Backup category -> the run's document category (scope_engine.TEXT_ROLE_BY_CATEGORY keys).
TEXT_LABELS: dict[str, str] = {"specification": "specifications", "addendum": "addenda", "other": "other"}
# --with-drawings adds the drawing sets (scope_engine.DRAWING_CATEGORIES; WS1
# facts extracted on the fly from the PDF, as prepare does without a facts job).
DRAWING_LABELS: dict[str, str] = {"drawing": "drawings", "electrical_drawing": "electrical_drawings"}
REQUIRED_CATEGORY = "specification"
# Planning figures for the expected dollars: one strict-JSON item per sent
# statement (index, party, carry, systems, a short quote) and the reasoning
# of a low-effort turn. The reserve uses --reserve-output-tokens instead.
EXPECTED_TOKENS_PER_ITEM = 60
EXPECTED_REASONING_TOKENS = 1500
DEFAULT_RESERVE_OUTPUT_TOKENS = 8000


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@dataclass
class Pick:
    case: str
    project_id: str
    tier: str
    documents: list[dict[str, Any]] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)


def text_documents(data: Any, case: str, project_id: str, *,
                   with_drawings: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """The project's text PDFs (and with `with_drawings` its drawing PDFs)
    as ScopeInputs document rows (plus the backup path) and the names left
    out with the reason."""
    from app.services import eval_manifest as em

    labels = {**TEXT_LABELS, **(DRAWING_LABELS if with_drawings else {})}
    misfiled = set((em.CASE_NOTES.get(case) or {}).get("misfiled_inputs") or {})
    docs: list[dict[str, Any]] = []
    left_out: list[str] = []
    for row in data.files(project_id):
        category = str(row.get("category") or "")
        if category not in labels:
            continue
        name = str(row.get("filename") or "")
        if not name.lower().endswith(".pdf") or not row.get("storage_path"):
            continue
        if name in misfiled:
            left_out.append(f"{name}: misfiled")
            continue
        if em.ANSWER_NAME_RE.search(name):
            left_out.append(f"{name}: names an answer")
            continue
        path = data.file_path(row)
        if not path.is_file():
            left_out.append(f"{name}: not in the backup")
            continue
        docs.append({"id": str(row["id"]), "name": name, "category": labels[category],
                     "backup_category": category, "path": str(path)})
    return docs, left_out


def pick_projects(data: Any, *, n: int = DEFAULT_N, tiers: Sequence[str] = TIERS,
                  only: Sequence[str] | None = None,
                  with_drawings: bool = False) -> tuple[list[Pick], list[dict[str, Any]]]:
    """The first `n` qualifying projects in selection order, and the ones
    passed over with the reason. `only` (case ids or id prefixes) narrows
    the candidates; holdout and excluded projects are refused either way."""
    from app.services import eval_manifest as em

    excluded = em.excluded_projects(EXCLUSION_PURPOSE)
    picks: list[Pick] = []
    passed: list[dict[str, Any]] = []
    for case, pid, tier, why in common.ordered_candidates(data, tiers):
        if len(picks) >= n:
            break
        if not common.matches_only(case, pid, only):
            continue
        if pid is None:
            passed.append({"case": case, "tier": tier, "why": why})
            continue
        if pid in excluded or pid in em.HOLDOUT_IDS:
            passed.append({"case": case, "tier": tier, "why": "excluded (holdout)"})
            continue
        docs, left_out = text_documents(data, case, pid, with_drawings=with_drawings)
        if not any(d["backup_category"] == REQUIRED_CATEGORY for d in docs):
            passed.append({"case": case, "tier": tier, "why": "no specification PDF in the backup"})
            continue
        picks.append(Pick(case=case, project_id=pid, tier=tier, documents=docs, left_out=left_out))
    return picks, passed


def inputs_for(pick: Pick, data: Any) -> Any:
    """The ScopeInputs of one project: its text documents with the backup's
    PDF bytes, the project row, no category outputs (spec documents only)."""
    from app.services import scope_engine

    paths = {d["id"]: Path(d["path"]) for d in pick.documents}
    rows = [{k: d[k] for k in ("id", "name", "category")} for d in pick.documents]

    def _pdf(doc_id: str) -> bytes | None:
        p = paths.get(str(doc_id))
        return p.read_bytes() if p is not None and p.is_file() else None

    return scope_engine.ScopeInputs(rows, project=data.project(pick.project_id), pdf_bytes=_pdf)


# ---------------------------------------------------------------------------
# Plan ($0) and the excluded-obligation comparison
# ---------------------------------------------------------------------------


def probe_settings(settings: Any) -> Any:
    """The settings with the attribution fallback switched on for this
    process only (AI_SCOPE_ATTRIB_LLM, the WS5 flag prepare checks)."""
    return settings.model_copy(update={"ai_scope_attrib_llm": True})


def _uncertain(obligations: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for o in obligations if o.get("uncertain") and o.get("source") == "code")


def excluded_ids(obligations: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(o.get("obligation_id")) for o in obligations if o.get("carry") == "exclude"}


def excluded_change(code_ctx: Mapping[str, Any], llm_ctx: Mapping[str, Any]) -> dict[str, Any]:
    code_ex = excluded_ids(code_ctx.get("obligations") or [])
    llm_ex = excluded_ids(llm_ctx.get("obligations") or [])
    return {
        "excluded_code": len(code_ex),
        "excluded_llm": len(llm_ex),
        "delta": len(llm_ex) - len(code_ex),
        "newly_excluded": sorted(llm_ex - code_ex),
        "no_longer_excluded": sorted(code_ex - llm_ex),
        "verified_facts_code": (code_ctx.get("stats") or {}).get("verified_facts"),
        "verified_facts_llm": (llm_ctx.get("stats") or {}).get("verified_facts"),
    }


CallerFactory = Callable[[Any], Any]


def _default_caller_factory(settings: Any) -> Any:
    from app.services import scope_attribution as attr

    return attr.default_llm_caller(settings)


def plan_project(pick: Pick, inputs: Any, *, settings: Any, route: Any, reserve_output_tokens: int,
                 caller_factory: CallerFactory = _default_caller_factory) -> tuple[dict[str, Any], dict[str, Any]]:
    """(plan row, code-only ScopeContext). prepare_context runs with the
    fallback allowed and the production caller, but `llm.complete_turn` is
    captured: the exact request is recorded, nothing is sent, the fallback
    keeps the code answers (the context is the code-only one)."""
    from app.services import scope_engine

    ps = probe_settings(settings)
    started = time.monotonic()
    with common.captured_requests() as seen:
        ctx = scope_engine.prepare_context(inputs, settings=ps, allow_llm=True, llm_caller=caller_factory(ps))
    fb = ctx.get("fallback") or {}
    obligations = ctx.get("obligations") or []
    stats = ctx.get("stats") or {}
    sent = int(fb.get("sent") or 0)
    req = seen[0] if seen else None
    in_tokens = common.request_input_tokens(req, route.provider) if req is not None else 0
    expected = reserve = None
    if sent:
        expected = common.estimate_usd(route.model, in_tokens, sent * EXPECTED_TOKENS_PER_ITEM + EXPECTED_REASONING_TOKENS)
        reserve = common.estimate_usd(route.model, in_tokens, reserve_output_tokens)
    row = {
        "case": pick.case, "project_id": pick.project_id, "tier": pick.tier,
        "documents": len(pick.documents),
        "document_categories": dict(Counter(d["backup_category"] for d in pick.documents)),
        "left_out": pick.left_out,
        "statements": stats.get("statements"), "obligations": stats.get("obligations"),
        "uncertain": _uncertain(obligations), "sent": sent,
        "cap": int(getattr(ps, "ai_scope_attrib_max_statements", 40)),
        "excluded_code": stats.get("excluded_obligations"),
        "input_tokens": in_tokens if sent else 0,
        "max_tokens": getattr(req, "max_tokens", None) if req is not None else None,
        "expected_usd": Decimal("0") if not sent else expected,
        "reserve_usd": Decimal("0") if not sent else reserve,
        "prepare_seconds": round(time.monotonic() - started, 1),
        "notes": list(ctx.get("notes") or [])[:5],
    }
    return row, ctx


def build_plan(picks: Sequence[Pick], passed: Sequence[Mapping[str, Any]], *, data: Any, settings: Any,
               reserve_output_tokens: int = DEFAULT_RESERVE_OUTPUT_TOKENS,
               caller_factory: CallerFactory = _default_caller_factory,
               keep: int | None = None, require_uncertain: bool = False,
               log: Callable[[str], None] = lambda _m: None) -> tuple[dict[str, Any], dict[str, tuple[Any, Any]]]:
    """The plan and, per case, (inputs, code-only context) for the live run.
    `keep` stops after that many planned projects; with `require_uncertain`
    a project without an uncertain statement is passed over (prepare would
    make no call on it) and the next candidate is planned instead."""
    from app.services import llm_pricing
    from app.services import plans_reader_model_routes as model_routes
    from app.services import scope_attribution as attr

    route = model_routes.resolve_step(attr.FALLBACK_STEP, settings)
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    state: dict[str, tuple[Any, Any]] = {}
    passed = list(passed)
    for pick in picks:
        if keep is not None and len(rows) >= keep:
            break
        log(f"preparing {pick.case} ({len(pick.documents)} text documents)")
        try:
            inputs = inputs_for(pick, data)
            row, ctx = plan_project(pick, inputs, settings=settings, route=route,
                                    reserve_output_tokens=reserve_output_tokens, caller_factory=caller_factory)
        except Exception as exc:  # noqa: BLE001 - reported in the plan, refused live
            problems.append(f"{pick.case}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        if require_uncertain and not row["sent"]:
            passed.append({"case": pick.case, "tier": pick.tier,
                           "why": f"no uncertain statement ({row['obligations']} obligations)"})
            continue
        rows.append(row)
        state[pick.case] = (inputs, ctx)
    expected = [r["expected_usd"] for r in rows]
    reserves = [r["reserve_usd"] for r in rows]
    plan = {
        "name": NAME,
        "route": {"step": attr.FALLBACK_STEP, "feature": attr.FALLBACK_FEATURE, "provider": route.provider,
                  "model": route.model, "effort": route.effort,
                  "priced": llm_pricing.price_for(route.model) is not None},
        "projects": rows,
        "passed_over": passed,
        "problems": problems,
        "calls": sum(1 for r in rows if r["sent"]),
        "expected_usd": None if any(v is None for v in expected) else sum(expected, Decimal("0")),
        "reserve_usd": None if any(v is None for v in reserves) else sum(reserves, Decimal("0")),
        "budget_usd": BUDGET_USD,
        "reserve_output_tokens": reserve_output_tokens,
        "model_calls_made": 0,
    }
    return plan, state


def format_plan(plan: Mapping[str, Any]) -> list[str]:
    r = plan["route"]
    lines = [
        f"scope attribution probe plan: {len(plan['projects'])} projects, {plan['calls']} calls "
        f"(feature {r['feature']}, step {r['step']}: {r['provider']} {r['model']} effort {r['effort']}"
        + ("" if r["priced"] else ", NOT PRICED: a live run refuses it") + ")",
        "case | tier | docs | statements | obligations | uncertain | sent (cap) | excluded (code) | input tok | est | reserve",
    ]
    for p in plan["projects"]:
        lines.append(" | ".join([
            p["case"], p["tier"], str(p["documents"]), str(p["statements"]), str(p["obligations"]),
            str(p["uncertain"]), f"{p['sent']} ({p['cap']})", str(p["excluded_code"]), str(p["input_tokens"]),
            common.money(p["expected_usd"]), common.money(p["reserve_usd"]),
        ]))
    for p in plan["passed_over"]:
        lines.append(f"passed over: {p['case']} ({p['tier']}): {p['why']}")
    for prob in plan["problems"]:
        lines.append(f"problem: {prob}")
    lines.append(
        f"expected {common.money(plan['expected_usd'])} of the {common.money(plan['budget_usd'])} budget; "
        f"summed reserves (input + {plan['reserve_output_tokens']} output tokens each) "
        f"{common.money(plan['reserve_usd'])}"
    )
    lines.append("model calls made: 0")
    return lines


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


def run_project(row: Mapping[str, Any], inputs: Any, code_ctx: Mapping[str, Any], *, settings: Any,
                caller_factory: CallerFactory = _default_caller_factory) -> dict[str, Any]:
    """The live fallback on one project, as prepare runs it (one capped
    call), compared against the code-only context."""
    from app.services import scope_engine

    ps = probe_settings(settings)
    meter = common.CallMeter(row.get("reserve_usd"))
    ctx = scope_engine.prepare_context(inputs, settings=ps, allow_llm=True, llm_caller=meter.wrap(caller_factory(ps)))
    fb = ctx.get("fallback") or {}
    rec = meter.record
    out = {
        "case": row["case"], "project_id": row["project_id"], "tier": row["tier"],
        "uncertain": row["uncertain"], "sent": int(fb.get("sent") or 0),
        "called": bool(fb.get("called")), "accepted": len(fb.get("applied") or []),
        "rejected": len(fb.get("rejected") or []),
        "rejected_why": dict(Counter(str(r.get("why")) for r in fb.get("rejected") or [])),
        "accepted_ids": list(fb.get("applied") or []),
        "fallback_error": fb.get("error"),
        "cost_usd": rec.cost_usd, "replayed": rec.replayed, "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens, "cached_tokens": rec.cached_tokens, "seconds": rec.seconds,
        "reserve_usd": rec.reserve_usd, "ledger_refused": rec.ledger_refused,
        **excluded_change(code_ctx, ctx),
    }
    return out


def format_table(results: Sequence[Mapping[str, Any]], skipped: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["| case | tier | uncertain | sent | accepted | rejected | excluded code | excluded llm | delta | cost | note |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        note = []
        if r.get("replayed"):
            note.append("cassette")
        if r.get("fallback_error"):
            note.append(str(r["fallback_error"])[:80])
        if r.get("rejected_why"):
            note.append(", ".join(f"{k} {v}" for k, v in r["rejected_why"].items()))
        lines.append("| " + " | ".join([
            r["case"], r["tier"], str(r["uncertain"]), str(r["sent"]), str(r["accepted"]), str(r["rejected"]),
            str(r["excluded_code"]), str(r["excluded_llm"]), f"{r['delta']:+d}", f"{r['cost_usd']:.4f}",
            "; ".join(note),
        ]) + " |")
    for s in skipped:
        lines.append(f"| {s['case']} | {s.get('tier', '')} | | | | | | | | | skipped: {s['why']} |")
    spent = sum(float(r.get("cost_usd") or 0) for r in results)
    lines += ["", f"calls {sum(1 for r in results if r.get('called'))}, accepted "
                  f"{sum(r['accepted'] for r in results)} of {sum(r['sent'] for r in results)} sent, "
                  f"excluded obligations {sum(r['excluded_code'] for r in results)} -> "
                  f"{sum(r['excluded_llm'] for r in results)}, spent ${spent:.4f}"]
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WS5 probe: the capped scope attribution fallback on 8 golden projects.")
    p.add_argument("--dry-run", action="store_true", help="print the plan and the estimate; no call ($0)")
    p.add_argument("--sweep", default=None, help="open sweep id of purpose dev_probe on WS5, or 'open' (live only)")
    p.add_argument("--n", type=int, default=DEFAULT_N, help="projects to probe (default 8)")
    p.add_argument("--only", default=None, help="comma list of case ids or project id prefixes")
    p.add_argument("--tiers", default=",".join(TIERS), help="comma list of smoke, core, pool (holdout never)")
    p.add_argument("--with-drawings", action="store_true",
                   help="also read the drawing sets (keynotes, sheet and general notes; slower, still $0)")
    p.add_argument("--require-uncertain", action="store_true",
                   help="pass over projects without an uncertain statement and plan the next candidate")
    p.add_argument("--max-usd", type=float, default=float(BUDGET_USD), help="cap on this run's spend (default 3)")
    p.add_argument("--reserve-output-tokens", type=int, default=DEFAULT_RESERVE_OUTPUT_TOKENS,
                   help="output tokens in each call's ledger reserve (the request itself is unchanged)")
    p.add_argument("--no-cassette", action="store_true", help="do not use cassettes (default record_missing, rule 6)")
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--backup", type=Path, default=None, help="production backup dir (default eval_reference.DEFAULT_BACKUP_DIR)")
    p.add_argument("--out", type=Path, default=None, help="output dir (default EVAL_ROOT/sweeps/<sweep>/scope_attrib_probe_<time>)")
    return p


def main(argv: list[str] | None = None, *, settings: Any = None, data: Any = None,
         caller_factory: CallerFactory | None = None) -> int:
    args = build_parser().parse_args(argv)
    from app.core.config import get_settings
    from app.services.eval_reference import DEFAULT_BACKUP_DIR, BackupData

    s = settings or get_settings()
    eval_root = common.eval_root_for(args.eval_root, s)
    factory = caller_factory or _default_caller_factory
    try:
        if args.n < 1:
            raise ProbeError("--n must be at least 1")
        if args.max_usd <= 0:
            raise ProbeError("--max-usd must be positive")
        if args.reserve_output_tokens < 1:
            raise ProbeError("--reserve-output-tokens must be positive")
        tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
        if "holdout" in tiers:
            raise ProbeError("the holdout is never probed (spec 8.1)")
        data = data or BackupData(args.backup or DEFAULT_BACKUP_DIR)
        only = args.only.split(",") if args.only else None
        # With --require-uncertain every qualifying candidate is a reserve
        # (planned in order until --n of them have an uncertain statement).
        picks, passed = pick_projects(data, n=10**6 if args.require_uncertain else args.n, tiers=tiers, only=only,
                                      with_drawings=args.with_drawings)
        if not picks:
            raise ProbeError("no project qualifies")
        plan, state = build_plan(picks, passed, data=data, settings=s, reserve_output_tokens=args.reserve_output_tokens,
                                 caller_factory=factory, keep=args.n, require_uncertain=args.require_uncertain,
                                 log=lambda m: print(m, file=sys.stderr))
        if not plan["projects"]:
            for line in format_plan(plan):
                print(line)
            raise ProbeError("no planned project (every candidate was passed over)")
        for line in format_plan(plan):
            print(line)
        if args.dry_run:
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / "plan.json").write_text(json.dumps(plan, indent=1, default=common.json_default))
                print(f"plan: {args.out / 'plan.json'}")
            return 0
        if not args.sweep:
            raise ProbeError(
                "a live probe needs --sweep <open sweep id of purpose dev_probe on WS5> or --sweep open "
                "(spec 0.4 rule 2); use --dry-run for the plan"
            )
        from app.services import plans_reader_model_routes as model_routes
        from app.services import scope_attribution as attr

        common.check_dev(s)
        common.check_route(model_routes.resolve_step(attr.FALLBACK_STEP, s), s)
        if plan["problems"]:
            raise ProbeError("fix the plan problems first: " + "; ".join(plan["problems"]))
        if plan["expected_usd"] is None or plan["reserve_usd"] is None:
            raise ProbeError("the plan has no price for the route's model (spec 0.4 rule 3)")
        sweep = common.arm_sweep(args.sweep, workstream=WORKSTREAM, eval_root=eval_root, settings=s,
                                 cassette=not args.no_cassette)
        s = get_settings() if settings is None else s
        left = min(Decimal(str(sweep["remaining_usd"])), Decimal(str(args.max_usd)))
        if plan["expected_usd"] > left:
            raise ProbeError(
                f"the plan's expected dollars ({common.money(plan['expected_usd'])}) exceed what this run may spend "
                f"({common.money(left)}: sweep {sweep['id']} remaining, capped by --max-usd); narrow it with --only or --n"
            )
        started = time.time()
        results: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        stop = None
        for row in plan["projects"]:
            if not row["sent"]:
                results.append({**{k: row[k] for k in ("case", "project_id", "tier", "uncertain", "sent")},
                                "called": False, "accepted": 0, "rejected": 0, "rejected_why": {}, "accepted_ids": [],
                                "fallback_error": None, "cost_usd": 0.0, "replayed": False,
                                "excluded_code": row["excluded_code"], "excluded_llm": row["excluded_code"], "delta": 0,
                                "newly_excluded": [], "no_longer_excluded": []})
                continue
            if stop:
                skipped.append({"case": row["case"], "tier": row["tier"], "why": stop})
                continue
            if Decimal(str(row["reserve_usd"])) > left:
                skipped.append({"case": row["case"], "tier": row["tier"],
                                "why": f"budget (reserve {common.money(row['reserve_usd'])}, left {common.money(left)})"})
                continue
            inputs, code_ctx = state[row["case"]]
            res = run_project(row, inputs, code_ctx, settings=s, caller_factory=factory)
            results.append(res)
            left -= Decimal(str(res["cost_usd"] or 0))
            print(f"{row['case']}: sent {res['sent']} accepted {res['accepted']} rejected {res['rejected']} "
                  f"excluded {res['excluded_code']} -> {res['excluded_llm']} cost {res['cost_usd']:.4f}"
                  + (f" ERROR {res['fallback_error']}" if res["fallback_error"] else ""))
            if res["ledger_refused"]:
                stop = "the ledger refused the previous call"
        out_dir = common.out_dir_for(eval_root, str(sweep["id"]), NAME, args.out)
        table = format_table(results, skipped)
        payload = {
            "schema": RESULTS_SCHEMA, "sweep_id": sweep["id"], "workstream": WORKSTREAM, "plan": plan,
            "results": results, "skipped": skipped, "elapsed_s": round(time.time() - started, 1),
            "spent_usd": round(sum(float(r.get("cost_usd") or 0) for r in results), 6),
        }
        path = common.write_results(out_dir, payload, table)
        for line in table:
            print(line)
        print(f"results: {path}")
        return 0
    except ProbeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "Pick", "build_plan", "excluded_change", "format_plan", "format_table", "inputs_for", "main", "pick_projects",
    "plan_project", "probe_settings", "run_project", "text_documents",
]


if __name__ == "__main__":
    raise SystemExit(main())
