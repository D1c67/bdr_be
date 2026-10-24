"""The independent review chain of a priced AI estimate (docs/
AI_ESTIMATING_BUILD_SPEC.md 4.1 post-run chain, 4.4, 5.4 ReviewResult, 5.6
MF, WS7).

Chain on one run, after the ai_pricing job stored pricing version v:

    begin(sb, run, pricing_version=v)       called at the end of price_run
      rules lane ($0): WS4 gate findings, WS5 decisions, WS7 rules,
        history bands  -> findings RF1..RFn on the review row
      review group (kind review): fresh context, read-only tools, max 10
        tool calls, REVIEW_SCHEMA, code-verified evidence (plans_reader_reviewer)
    advance(sb, run)                        called by runs.advance after a
      review or revise group settles        (hook) and by begin for a recheck
      revise group (kind revise): one bounded turn, REVISE_SCHEMA dispositions
      apply (mode apply): plans_reader_ops.apply_ops with the evidence rules
        -> BOQ deliverable v+1 through the modify path (render_deliverable
        with the instruction REVISED_INSTRUCTION, the run's change slot
        claimed and released) -> older pricing versions stale -> an
        ai_pricing job whose reason is REVIEW_REVISION_MARKER
      recheck ($0): the pricing job prices BOQ v+1 and calls begin again;
        begin sees the marker, reruns the rules lane on the revised version
        (findings post_revision) and closes the review. The marker never
        starts another review: one review per base pricing version
        (unique (run_id, base_pricing_version)).

Modes (`plans_reader_review_mode`): off = nothing; shadow = the chain runs
and the ops are computed with apply_ops but nothing is written to the
estimate (no BOQ v+1, no re-price); apply = the ops are written.

Verdicts (5.4): clear (no open finding above info), advisory (open
warnings only), needs_human (an open critical finding: never applied,
declined by the builder, or with an op the rules refused), incomplete (a model stage failed or was
cut by a cap or budget), skipped (no priced lines), stale (a human edited
the BOQ after the review, or a newer pricing version replaced its base).
After a revision the verdict pool is the post-revision findings (the rules
lane re-run on the revised version: origins RERUN_ORIGINS) plus every
pre-revision finding of an origin the recheck does not re-run (the
reviewer) that is still open, contested, declined or rejected_by_rule; a
declined critical reviewer finding is still needs_human.

Severity calibration applies to reviewer findings only (plans_reader_reviewer
.severity_for); rule and band severities are fixed by their modules.

Every database read and write is a sync Supabase call (never inside
`async def`); the tests drive the chain on a fake client with injected
loaders, an injected enqueue and an injected modify path.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from app.services import plans_reader_bands as bands_mod
from app.services import plans_reader_ops as ops_mod
from app.services import plans_reader_review_rules as rules_mod
from app.services import plans_reader_reviewer as reviewer_mod
from app.services.plans_reader_review_rules import ReviewFinding, ReviewInputs

logger = logging.getLogger(__name__)

CHAIN_VERSION = "ws7-review-1"
RESULT_SCHEMA = "bdr.review_result/1"
REVIEWS_TABLE = "plans_reader_reviews"
FINDINGS_TABLE = "plans_reader_review_findings"
RECOUNTS_TABLE = "plans_reader_recounts"
GROUPS_TABLE = "plans_reader_groups"
REVIEW_REVISION_MARKER = "review_revision"
REVISED_INSTRUCTION = "Revised after independent review"
MODES: tuple[str, ...] = ("off", "shadow", "apply")
STAGES: tuple[str, ...] = ("rules", "review", "revise", "recheck", "done", "failed", "partial")
TERMINAL_STAGES: frozenset[str] = frozenset({"done", "failed", "partial"})
VERDICTS: tuple[str, ...] = ("clear", "advisory", "needs_human", "incomplete", "skipped", "stale")
STEP_STATUSES: tuple[str, ...] = ("pending", "queued", "done", "failed", "skipped")
# A finding still needing a human: never applied, or declined by the builder
# (a model's decline never silences a code finding), or refused by the rules.
OPEN_DISPOSITIONS: frozenset[str] = frozenset({"open", "contested", "rejected_by_rule", "declined"})
# Origins the recheck re-runs on the revised version (plans_reader_review_rules
# .run_rules: gates, scope decisions, WS7 rules, bands); their pre-revision
# copies are superseded by the post_revision findings. Every other origin
# (the reviewer) is carried into the revised verdict while still open.
RERUN_ORIGINS: frozenset[str] = frozenset({"rule", "gate", "scope", "band"})
REVIEW_PASS = 4
REVISE_PASS = 5
DEFAULT_BUDGET_USD = 4.00
REVIEW_EST_INPUT_TOKENS = 30_000
REVISE_EST_INPUT_TOKENS = 20_000
PACIFIC = ZoneInfo("America/Los_Angeles")

Enqueue = Callable[[Any, list[dict[str, Any]], Mapping[str, Any]], None]
ModifyPath = Callable[..., dict[str, Any]]


class ReviewRefused(ValueError):
    """The chain cannot run on this input (not an error of the run)."""


def _now_iso() -> str:
    return dt.datetime.now(PACIFIC).isoformat()


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _run_id(run: Any) -> str:
    return str(run.get("id") if isinstance(run, Mapping) else run)


def mode_of(settings: Any = None) -> str:
    if settings is None:
        try:
            from app.core.config import get_settings

            settings = get_settings()
        except Exception:  # noqa: BLE001 - no settings reads as off
            return "off"
    mode = str(getattr(settings, "plans_reader_review_mode", "off") or "off").strip().lower()
    return mode if mode in MODES else "off"


def _budget_usd(settings: Any) -> float:
    cap = _f(getattr(settings, "plans_reader_review_budget_usd", None)) if settings is not None else None
    return min(cap, 6.0) if cap and cap > 0 else DEFAULT_BUDGET_USD


# Loaded inputs -----------------------------------------------------------------------------------


@dataclass
class LoadedInputs:
    """What the chain needs from the run, loaded by `load_inputs` (every
    piece best effort: a missing module or table leaves its field empty)."""

    inputs: ReviewInputs
    boq_version: int | None = None
    boq_spec: Mapping[str, Any] | None = None
    artifact: Mapping[str, Any] | None = None
    documents: list[dict[str, Any]] = field(default_factory=list)
    recounts: list[dict[str, Any]] = field(default_factory=list)
    project_id: str | None = None
    project_type: str | None = None
    project_name: str | None = None
    wage_type: str | None = None
    scope_digest: str | None = None
    line_values: dict[str, float] = field(default_factory=dict)
    total_usd: float | None = None
    allowance_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def line_values_from_artifact(artifact: Mapping[str, Any] | None) -> dict[str, float]:
    """line_id -> extended USD (hours x rate + material) of each priced line
    of a `bdr.priced_estimate/1` artifact."""
    if not isinstance(artifact, Mapping):
        return {}
    rate = _f(artifact.get("labor_rate_usd")) or 0.0
    out: dict[str, float] = {}
    for ln in artifact.get("lines") or []:
        if not isinstance(ln, Mapping) or not ln.get("line_id"):
            continue
        qty = _f(ln.get("qty"))
        if qty is None:
            continue
        hours = _f(ln.get("unit_hours")) or 0.0
        mat = _f(ln.get("unit_material_usd")) or 0.0
        out[str(ln["line_id"])] = round(qty * (hours * rate + mat), 2)
    return out


def _artifact_from_version(sb: Any, run: Mapping[str, Any], pricing_row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    from app.services import pricing_engine as pe
    from app.services import pricing_run
    from app.services.pricing_types import PricedEstimate

    lines = [r.get("line") for r in pricing_run.version_lines(sb, str(pricing_row["id"])) if isinstance(r.get("line"), Mapping)]
    data = {
        "schema_version": 1,
        "status": pricing_row.get("status") or "provisional",
        "lines": lines,
        "categories": pricing_row.get("categories") or {},
        "verify": pricing_row.get("verify_draft"),
        "workbook_basis": pricing_row.get("workbook_basis"),
        "range_low": pricing_row.get("range_low"),
        "range_high": pricing_row.get("range_high"),
        "quality_summary": pricing_row.get("quality_summary") or {},
        "unpriced": pricing_row.get("unpriced") or {},
        "blockers": pricing_row.get("blockers") or [],
        "warnings": pricing_row.get("warnings") or [],
        "policy_version": pricing_row.get("policy_version"),
        "library_fingerprint": pricing_row.get("library_fingerprint"),
        "excluded_project_ids_sha256": pricing_row.get("excluded_project_ids_sha256"),
    }
    priced = PricedEstimate.from_dict(data)
    ctx = pricing_row.get("context") if isinstance(pricing_row.get("context"), Mapping) else {}
    return pe.to_priced_estimate_v1(priced, run_id=_run_id(run), wage_type=ctx.get("wage_type"))


def load_inputs(sb: Any, run: Mapping[str, Any], pricing_row: Mapping[str, Any], *, settings: Any = None) -> LoadedInputs:
    """ReviewInputs and the chain's context from the stored run (module
    docstring). Typed inputs that cannot be loaded stay None, which the
    rules lane records in its notes."""
    from app.services import pricing_run

    run_id = _run_id(run)
    notes: list[str] = []
    boq_version = int(pricing_row.get("boq_version") or 0) or None
    boq = None
    try:
        boq = pricing_run.latest_boq(sb, run_id, version=boq_version)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"boq unavailable: {str(exc)[:120]}")
    boq_spec = boq.get("spec_json") if isinstance(boq, Mapping) and isinstance(boq.get("spec_json"), Mapping) else None
    if boq is not None and boq_version is None:
        boq_version = int(boq.get("version") or 0) or None
    artifact = None
    try:
        artifact = _artifact_from_version(sb, run, pricing_row)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"priced artifact unavailable: {str(exc)[:160]}")
    gate_findings = None
    try:
        from app.services import plans_reader_gates as gates

        gate_findings = gates.findings_for_run(sb, run_id, stage=None)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"gate findings unavailable: {str(exc)[:120]}")
    main = None
    try:
        main = pricing_run.main_output(sb, run_id)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"main output unavailable: {str(exc)[:120]}")
    scope_report = main.get("scope_policy") if isinstance(main, Mapping) and isinstance(main.get("scope_policy"), Mapping) else None
    bid = None
    try:
        from app.services import bid_conditions as bc

        bid = bc.for_run(sb, run_id)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"bid conditions unavailable: {str(exc)[:120]}")
    facts: list[Any] = []
    digest = None
    try:
        from app.services import scope_engine

        facts = list(scope_engine.facts_for_run(sb, run_id))
        ctx = scope_engine.context_for_run(sb, run_id)
        digest = str(ctx.get("digest")) if isinstance(ctx, Mapping) and ctx.get("digest") else None
    except Exception as exc:  # noqa: BLE001
        notes.append(f"scope context unavailable: {str(exc)[:120]}")
    categories: list[Mapping[str, Any]] = []
    try:
        rows = sb.table("plans_reader_outputs").select("*").eq("run_id", run_id).eq("scope", "category").execute().data or []
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            cat = str(r.get("category") or "")
            if cat not in latest or int(r.get("version") or 0) > int(latest[cat].get("version") or 0):
                latest[cat] = r
        categories = [r.get("output") for r in latest.values() if isinstance(r.get("output"), Mapping)]
    except Exception as exc:  # noqa: BLE001
        notes.append(f"category outputs unavailable: {str(exc)[:120]}")
    documents: list[dict[str, Any]] = []
    try:
        rows = sb.table("plans_reader_documents").select("*").eq("run_id", run_id).execute().data or []
        for d in rows:
            documents.append({"id": str(d.get("id")), "name": d.get("name"), "category": d.get("category"),
                              "pages": d.get("page_count") or d.get("pages")})
    except Exception as exc:  # noqa: BLE001
        notes.append(f"documents unavailable: {str(exc)[:120]}")
    recounts: list[dict[str, Any]] = []
    try:
        recounts = [dict(r) for r in sb.table(RECOUNTS_TABLE).select("*").eq("run_id", run_id).execute().data or []]
    except Exception as exc:  # noqa: BLE001
        notes.append(f"recounts unavailable: {str(exc)[:120]}")
    project_id = str(run.get("project_id")) if run.get("project_id") else None
    project_type = None
    if project_id:
        try:
            prow = sb.table("projects").select("project_type").eq("id", project_id).limit(1).execute().data or []
            project_type = (prow[0].get("project_type") if prow else None) or None
        except Exception as exc:  # noqa: BLE001
            notes.append(f"project type unavailable: {str(exc)[:120]}")
    rate_table = None
    try:
        policy, _meta = pricing_run.load_policy(sb)
        rate_table = {str(k): float(v) for k, v in policy.labor_rates.items()}
    except Exception as exc:  # noqa: BLE001
        notes.append(f"pricing policy unavailable: {str(exc)[:120]}")
    price = measure = None
    try:
        from app.services.plans_reader_tools import Unavailable, services

        svc = services()
        price = None if isinstance(svc.price, Unavailable) else svc.price
        measure = None if isinstance(svc.measure, Unavailable) else svc.measure
    except Exception:  # noqa: BLE001
        pass
    ctx_doc = pricing_row.get("context") if isinstance(pricing_row.get("context"), Mapping) else {}
    wage = ctx_doc.get("wage_type") or (artifact or {}).get("wage_type") if isinstance(artifact, Mapping) else ctx_doc.get("wage_type")
    totals = (artifact or {}).get("totals") if isinstance(artifact, Mapping) else None
    total = _f((totals or {}).get("bid_total_usd")) if isinstance(totals, Mapping) else None
    allowances = [a for a in ((artifact or {}).get("allowances") or []) if isinstance(a, Mapping)] if isinstance(artifact, Mapping) else []
    project_name = (((run.get("ledger") or {}).get("project") or {}).get("name") if isinstance(run.get("ledger"), Mapping) else None) or run.get("name")
    inputs = ReviewInputs(
        run_id=run_id, boq=boq_spec, priced=artifact, gate_findings=gate_findings, scope_report=scope_report,
        bid_conditions=bid, facts=facts, category_outputs=categories, measure_service=measure, price=price,
        allowances=allowances, rate_table=rate_table, total_usd=total,
        existing_tie_in_ft=_f(getattr(settings, "plans_reader_existing_tie_in_ft", None)) if settings is not None else None,
        local_rules_fallback=bool(getattr(settings, "plans_reader_review_local_rules_fallback", False)) if settings is not None else None,
    )
    return LoadedInputs(
        inputs=inputs, boq_version=boq_version, boq_spec=boq_spec, artifact=artifact, documents=documents,
        recounts=recounts, project_id=project_id, project_type=project_type, project_name=project_name,
        wage_type=wage, scope_digest=digest, line_values=line_values_from_artifact(artifact), total_usd=total,
        allowance_names=[str(a.get("name")) for a in allowances if a.get("name")], notes=notes,
    )


def band_checks_for(sb: Any, loaded: LoadedInputs, *, bands: Any = None) -> list[bands_mod.BandCheck]:
    """The history band checks of the priced artifact (LOPO: the run's own
    project left out); empty without active bands or an artifact."""
    if not isinstance(loaded.artifact, Mapping):
        return []
    hb = bands
    if hb is None and sb is not None:
        try:
            hb = bands_mod.load_active_bands(sb)
        except Exception as exc:  # noqa: BLE001
            loaded.notes.append(f"bands unavailable: {str(exc)[:120]}")
            hb = None
    if hb is None:
        return []
    summary = bands_mod.summary_from_priced(loaded.artifact, project_id=loaded.project_id, project_type=loaded.project_type)
    return bands_mod.check_bands(summary, hb, exclude_project_ids=[loaded.project_id] if loaded.project_id else ())


# Packs -------------------------------------------------------------------------------------------


def pack_lines(boq_spec: Mapping[str, Any] | None, line_values: Mapping[str, float]) -> list[dict[str, Any]]:
    from app.services import plans_reader_rows as rows_mod

    out: list[dict[str, Any]] = []
    if not isinstance(boq_spec, Mapping):
        return out
    for row in rows_mod.iter_boq_lines(boq_spec):
        lid = str(row["line_id"])
        out.append({
            "line_id": lid, "area": row.get("area"), "section": row.get("section"), "group": row.get("group"),
            "subgroup": row.get("subgroup"), "description": row.get("description"), "quantity": row.get("quantity"),
            "unit": row.get("unit"), "basis": row.get("basis"), "sheet_refs": list(row.get("sheet_refs") or []),
            "flags": list(row.get("flags") or []), "ext_usd": line_values.get(lid),
        })
    return out


def build_review_pack(review_id: str, run: Mapping[str, Any], loaded: LoadedInputs, rules: rules_mod.RulesResult,
                      band_checks: Sequence[bands_mod.BandCheck], *, base_pricing_version: int) -> dict[str, Any]:
    """The review group's pack (stored on the group's loop_state; the
    reviewer builds its first user turn from it, never from the builder's
    transcript)."""
    systems: list[str] = []
    for out in loaded.inputs.category_outputs or ():
        brief = out.get("brief") if isinstance(out, Mapping) else None
        for s in (brief or {}).get("systems") or [] if isinstance(brief, Mapping) else []:
            name = str((s or {}).get("system") or "") if isinstance(s, Mapping) else ""
            if name and name not in systems:
                systems.append(name)
    return {
        "review_id": review_id,
        "run_id": _run_id(run),
        "base_pricing_version": int(base_pricing_version),
        "boq_version": loaded.boq_version,
        "project_name": loaded.project_name,
        "project_type": loaded.project_type,
        "wage_type": loaded.wage_type,
        "documents": list(loaded.documents),
        "lines": pack_lines(loaded.boq_spec, loaded.line_values),
        "rule_findings": [f.to_dict() for f in rules.findings],
        "bands": [b.to_dict() for b in band_checks],
        "recounts": [{k: r.get(k) for k in ("target_id", "row_id", "outcome", "builder_qty", "recount_qty")} for r in loaded.recounts],
        "systems": systems,
        "scope_digest": loaded.scope_digest,
        "total_usd": loaded.total_usd,
        "allowance_names": list(loaded.allowance_names),
    }


# Reviews and findings ----------------------------------------------------------------------------


def _review_rows(sb: Any, run_id: str) -> list[dict[str, Any]]:
    rows = sb.table(REVIEWS_TABLE).select("*").eq("run_id", str(run_id)).execute().data or []
    return sorted(rows, key=lambda r: int(r.get("base_pricing_version") or 0), reverse=True)


def review_for_version(sb: Any, run_id: str, pricing_version: int) -> dict[str, Any] | None:
    for r in _review_rows(sb, run_id):
        if int(r.get("base_pricing_version") or 0) == int(pricing_version):
            return r
    return None


def latest_review(sb: Any, run_id: str) -> dict[str, Any] | None:
    rows = _review_rows(sb, run_id)
    return rows[0] if rows else None


def _update_review(sb: Any, review_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    patch = {**patch, "updated_at": _now_iso()}
    rows = sb.table(REVIEWS_TABLE).update(dict(patch)).eq("id", str(review_id)).execute().data or []
    return rows[0] if rows else {"id": review_id, **patch}


def stored_findings(sb: Any, review_id: str) -> list[ReviewFinding]:
    rows = sb.table(FINDINGS_TABLE).select("*").eq("review_id", str(review_id)).execute().data or []
    out = []
    for r in rows:
        try:
            out.append(ReviewFinding.from_dict(r))
        except Exception:  # noqa: BLE001 - a malformed row is skipped
            continue
    return sorted(out, key=lambda f: rules_mod._line_sort_key(f.finding_id) if f.finding_id else (10**9, ""))


def write_findings(sb: Any, review_id: str, run_id: str, findings: Iterable[ReviewFinding]) -> int:
    rows = [f.to_row(review_id=str(review_id), run_id=str(run_id)) for f in findings if f.finding_id]
    if not rows:
        return 0
    sb.table(FINDINGS_TABLE).upsert(rows, on_conflict="review_id,finding_id").execute()
    return len(rows)


def _next_finding_start(findings: Iterable[ReviewFinding]) -> int:
    best = 0
    for f in findings:
        try:
            best = max(best, int(str(f.finding_id)[len(rules_mod.FINDING_ID_PREFIX):]))
        except ValueError:
            continue
    return best + 1


def _result_of(review: Mapping[str, Any]) -> dict[str, Any]:
    result = review.get("result") if isinstance(review.get("result"), Mapping) else {}
    return dict(result)


def _set_step(result: dict[str, Any], step: str, status: str, **extra: Any) -> None:
    stages = result.setdefault("stages", {})
    stages[step] = {"status": status, **extra}


def _step_status(result: Mapping[str, Any], step: str) -> str:
    stages = result.get("stages") if isinstance(result.get("stages"), Mapping) else {}
    st = stages.get(step) if isinstance(stages, Mapping) else None
    return str(st.get("status") if isinstance(st, Mapping) else st or "pending")


def verdict_for(findings: Sequence[ReviewFinding], *, stage: str, revised: bool = False) -> tuple[str, list[str]]:
    """The verdict and its reasons from the findings' severities and
    dispositions (module docstring). When `revised` the pool is the
    post-revision findings (the re-run rules lane) plus the pre-revision
    findings whose origin is not in RERUN_ORIGINS and whose disposition is
    still open (OPEN_DISPOSITIONS): a declined or rule-refused critical
    reviewer finding never vanishes from the recheck's verdict."""
    if stage in ("failed", "partial"):
        return "incomplete", [f"review {stage}"]
    if revised:
        pool = [f for f in findings if f.post_revision]
        pool += [f for f in findings if not f.post_revision and f.origin not in RERUN_ORIGINS
                 and f.disposition in OPEN_DISPOSITIONS]
    else:
        pool = [f for f in findings if not f.post_revision]
    open_crit = [f for f in pool if f.severity == "critical" and f.disposition in OPEN_DISPOSITIONS]
    open_warn = [f for f in pool if f.severity == "warning" and f.disposition in OPEN_DISPOSITIONS]
    if open_crit:
        return "needs_human", [f"{f.finding_id}: {f.title}" for f in open_crit[:8]]
    if open_warn:
        return "advisory", [f"{f.finding_id}: {f.title}" for f in open_warn[:8]]
    return "clear", []


def _result_doc(review: Mapping[str, Any], findings: Sequence[ReviewFinding], *, verdict: str | None, reasons: Sequence[str],
                bands: Sequence[Mapping[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    prior = _result_of(review)
    out = {
        **prior,
        "schema": RESULT_SCHEMA,
        "version": CHAIN_VERSION,
        "review_id": str(review.get("id")),
        "run_id": str(review.get("run_id")),
        "mode": review.get("mode"),
        "base_pricing_version": review.get("base_pricing_version"),
        "revised_pricing_version": review.get("revised_pricing_version"),
        "verdict": verdict,
        "reasons": list(reasons),
        "findings": [f.to_dict() for f in findings],
        "bands": list(bands) if bands is not None else list(prior.get("bands") or []),
        "cost_usd": _f(review.get("cost_usd")) or 0.0,
        "models": dict(review.get("models") or {}),
        "stages": dict(prior.get("stages") or {}),
    }
    out.update(extra)
    return out


# begin -------------------------------------------------------------------------------------------


def _snapshot_route(run: Mapping[str, Any], step: str, settings: Any) -> dict[str, Any]:
    from app.services import plans_reader_model_routes as model_routes

    snapshot = (run.get("settings_snapshot") or {}).get("routes") if isinstance(run.get("settings_snapshot"), Mapping) else None
    try:
        return model_routes.resolve_step(step, settings, snapshot=snapshot if isinstance(snapshot, Mapping) else None).as_dict()
    except Exception:  # noqa: BLE001 - a run without routes still reviews on the defaults
        return {"step": step, "provider": "openai", "model": "gpt-6-astra", "effort": "medium",
                "budget_usd": None, "tools": True, "explicit": False}


def _default_enqueue(sb: Any, groups: list[dict[str, Any]], run: Mapping[str, Any]) -> None:
    from app.services import plans_reader_runs

    plans_reader_runs._enqueue_groups(sb, groups, dict(run))


def _insert_group(sb: Any, run: Mapping[str, Any], *, kind: str, pack: Mapping[str, Any], route: Mapping[str, Any],
                  est_tokens: int, pass_no: int, enqueue: Enqueue | None) -> dict[str, Any]:
    row = {
        "run_id": _run_id(run), "kind": kind, "category": None, "group_index": 0, "pass": pass_no, "pages": [],
        "status": "queued", "est_input_tokens": est_tokens, "mode": "review" if kind == "review" else "write",
        "route": dict(route), "loop_state": {kind: dict(pack)},
    }
    inserted = sb.table(GROUPS_TABLE).insert(row).execute().data or []
    group = inserted[0] if inserted else row
    (enqueue or _default_enqueue)(sb, [group], run)
    return group


def _pricing_version(sb: Any, run_id: str, version: int) -> dict[str, Any] | None:
    from app.services import pricing_run

    for row in pricing_run.versions(sb, run_id):
        if int(row.get("version") or 0) == int(version):
            return row
    return None


def mark_stale(sb: Any, run_id: str, *, reason: str, except_review_id: str | None = None) -> int:
    """Flag every non-stale review of the run `stale` (a human edit after
    the review, or a newer pricing version). Returns the count."""
    n = 0
    for r in _review_rows(sb, run_id):
        if except_review_id and str(r.get("id")) == str(except_review_id):
            continue
        if r.get("verdict") == "stale":
            continue
        result = _result_of(r)
        result["verdict"] = "stale"
        result["reasons"] = [*(result.get("reasons") or []), reason]
        result["stale_at"] = _now_iso()
        _update_review(sb, str(r["id"]), {"verdict": "stale", "reasons": result["reasons"], "result": result})
        n += 1
    return n


def on_boq_changed(sb: Any, run_id: str, *, instruction: str | None = None, user: Any = None) -> int:
    """Hook for `plans_reader_runs.after_deliverable_change` (kind boq): a
    human edit after the review marks its verdict stale. The chain's own
    revision (instruction REVISED_INSTRUCTION) does not."""
    if str(instruction or "") == REVISED_INSTRUCTION:
        return 0
    return mark_stale(sb, str(run_id), reason="human_edit")


def begin(
    sb: Any,
    run: Mapping[str, Any],
    *,
    pricing_version: int,
    settings: Any = None,
    enqueue: Enqueue | None = None,
    loaded: LoadedInputs | None = None,
    bands: Any = None,
) -> dict[str, Any] | None:
    """Start the review of pricing version `pricing_version` (module
    docstring), or run the recheck when that version is the chain's own
    revision. Returns the review row, None when the mode is off. Never
    starts a second review for the same base version."""
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    mode = mode_of(settings)
    if mode == "off":
        return None
    run_id = _run_id(run)
    pricing_row = _pricing_version(sb, run_id, int(pricing_version)) if sb is not None else None
    if pricing_row is None:
        raise ReviewRefused(f"pricing version {pricing_version} of run {run_id} does not exist")
    if str(pricing_row.get("instruction") or "") == REVIEW_REVISION_MARKER:
        pending = next((r for r in _review_rows(sb, run_id) if r.get("stage") == "recheck"), None)
        if pending is not None:
            return recheck(sb, run, pending, pricing_row, settings=settings, loaded=loaded, bands=bands)
        logger.info("review: revision pricing v%s of run %s has no review waiting for a recheck", pricing_version, run_id)
        return None
    existing = review_for_version(sb, run_id, int(pricing_version))
    if existing is not None:
        return existing
    mark_stale(sb, run_id, reason=f"superseded by pricing v{pricing_version}")
    inserted = sb.table(REVIEWS_TABLE).insert({
        "run_id": run_id, "base_pricing_version": int(pricing_version), "mode": mode, "stage": "rules", "verdict": None,
        "budget_usd": _budget_usd(settings), "models": {}, "reasons": [], "result": {"stages": {}}, "cost_usd": 0,
    }).execute().data or []
    review = inserted[0] if inserted else None
    if review is None:
        raise ReviewRefused("the review row could not be inserted")
    review_id = str(review["id"])
    data = loaded if loaded is not None else load_inputs(sb, run, pricing_row, settings=settings)
    checks = band_checks_for(sb, data, bands=bands)
    data.inputs.band_checks = checks
    rules = rules_mod.run_rules(data.inputs)
    write_findings(sb, review_id, run_id, rules.findings)
    result = _result_of(review)
    result.update({"schema": RESULT_SCHEMA, "version": CHAIN_VERSION, "rules": {"version": rules.version, "notes": rules.notes,
                   "stats": rules.stats, "loader_notes": data.notes}, "bands": [c.to_dict() for c in checks],
                   "band_version": checks[0].band_version if checks else None})
    _set_step(result, "rules", "done", findings=len(rules.findings))
    if not data.inputs.lines():
        _set_step(result, "review", "skipped", reason="no BOQ lines")
        doc = _result_doc({**review, "result": result}, rules.findings, verdict="skipped", reasons=["no BOQ lines to review"],
                          bands=result["bands"])
        return _update_review(sb, review_id, {"stage": "done", "verdict": "skipped", "reasons": doc["reasons"], "result": doc,
                                              "band_version": result.get("band_version"), "finished_at": _now_iso()})
    pack = build_review_pack(review_id, run, data, rules, checks, base_pricing_version=int(pricing_version))
    route = _snapshot_route(run, reviewer_mod.REVIEW_STEP, settings)
    group = _insert_group(sb, run, kind="review", pack=pack, route=route, est_tokens=REVIEW_EST_INPUT_TOKENS,
                          pass_no=REVIEW_PASS, enqueue=enqueue)
    _set_step(result, "review", "queued", group_id=str(group.get("id")))
    doc = _result_doc({**review, "result": result}, rules.findings, verdict=None, reasons=["review in progress"],
                      bands=result["bands"])
    return _update_review(sb, review_id, {"stage": "review", "result": doc, "band_version": result.get("band_version"),
                                          "models": {"review": route.get("model")}})


# Recording group outputs ---------------------------------------------------------------------


def record_reviewer_output(sb: Any, run: Mapping[str, Any], group: Mapping[str, Any], output: Mapping[str, Any]) -> dict[str, Any] | None:
    """Persist a finished review group: the reviewer findings (numbered
    after the rule findings), the cost and model, step review done."""
    review_id = str(output.get("review_id") or "")
    rows = sb.table(REVIEWS_TABLE).select("*").eq("id", review_id).execute().data or []
    if not rows:
        return None
    review = rows[0]
    run_id = _run_id(run)
    existing = stored_findings(sb, review_id)
    new = [ReviewFinding.from_dict(f) for f in output.get("findings") or [] if isinstance(f, Mapping)]
    numbered = rules_mod.number_findings(new, start=_next_finding_start(existing))
    write_findings(sb, review_id, run_id, numbered)
    result = _result_of(review)
    _set_step(result, "review", "failed" if output.get("incomplete") else "done", group_id=str(group.get("id")),
              findings=len(numbered), tool_calls=output.get("tool_calls"), stop_reason=output.get("stop_reason"),
              stats=dict(output.get("stats") or {}))
    result["summary"] = output.get("summary")
    result["systems_checked"] = list(output.get("systems_checked") or [])
    cost = (_f(review.get("cost_usd")) or 0.0) + (_f(output.get("cost_usd")) or 0.0)
    models = {**(review.get("models") or {}), "review": output.get("model") or (review.get("models") or {}).get("review")}
    all_findings = [*existing, *numbered]
    doc = _result_doc({**review, "result": result, "cost_usd": cost, "models": models}, all_findings,
                      verdict=None, reasons=["review group done; chain advancing"])
    return _update_review(sb, review_id, {"result": doc, "cost_usd": round(cost, 6), "models": models})


def record_revise_output(sb: Any, run: Mapping[str, Any], group: Mapping[str, Any], output: Mapping[str, Any]) -> dict[str, Any] | None:
    review_id = str(output.get("review_id") or "")
    rows = sb.table(REVIEWS_TABLE).select("*").eq("id", review_id).execute().data or []
    if not rows:
        return None
    review = rows[0]
    result = _result_of(review)
    _set_step(result, "revise", "failed" if output.get("incomplete") else "done", group_id=str(group.get("id")),
              dispositions=len(output.get("dispositions") or []), stats=dict(output.get("stats") or {}))
    result["dispositions"] = [dict(d) for d in output.get("dispositions") or []]
    cost = (_f(review.get("cost_usd")) or 0.0) + (_f(output.get("cost_usd")) or 0.0)
    models = {**(review.get("models") or {}), "revise": output.get("model")}
    findings = stored_findings(sb, review_id)
    doc = _result_doc({**review, "result": result, "cost_usd": cost, "models": models}, findings,
                      verdict=None, reasons=["revise group done; chain advancing"])
    return _update_review(sb, review_id, {"result": doc, "cost_usd": round(cost, 6), "models": models})


def finish_partial(sb: Any, run: Mapping[str, Any], *, group: Mapping[str, Any], reason: str) -> dict[str, Any] | None:
    """The spine's seam: a review or revise group stopped by a cap or
    budget ends the review `partial` (spec 4.2), verdict incomplete."""
    state = group.get("loop_state") if isinstance(group.get("loop_state"), Mapping) else {}
    kind = str(group.get("kind") or "review")
    pack = state.get(kind) if isinstance(state, Mapping) else None
    review_id = str((pack or {}).get("review_id") or "") if isinstance(pack, Mapping) else ""
    review = None
    if review_id:
        rows = sb.table(REVIEWS_TABLE).select("*").eq("id", review_id).execute().data or []
        review = rows[0] if rows else None
    if review is None:
        review = next((r for r in _review_rows(sb, _run_id(run)) if r.get("stage") not in TERMINAL_STAGES), None)
    if review is None:
        return None
    return _finish(sb, review, stage="partial", error=str(reason)[:500], step=kind)


def _finish(sb: Any, review: Mapping[str, Any], *, stage: str, error: str | None = None, step: str | None = None,
            revised: bool = False) -> dict[str, Any]:
    review_id = str(review["id"])
    findings = stored_findings(sb, review_id)
    result = _result_of(review)
    if step:
        _set_step(result, step, "failed" if stage != "done" else "done", error=error)
    verdict, reasons = verdict_for(findings, stage=stage, revised=revised)
    if error:
        reasons = [*reasons, error[:200]]
    doc = _result_doc({**review, "result": result}, findings, verdict=verdict, reasons=reasons)
    patch: dict[str, Any] = {"stage": stage, "verdict": verdict, "reasons": reasons, "result": doc, "finished_at": _now_iso()}
    if error:
        patch["error"] = error[:500]
    return _update_review(sb, review_id, patch)


# advance -----------------------------------------------------------------------------------------


def _group(sb: Any, group_id: str | None) -> dict[str, Any] | None:
    if not group_id:
        return None
    rows = sb.table(GROUPS_TABLE).select("*").eq("id", str(group_id)).execute().data or []
    return rows[0] if rows else None


def _group_of_step(sb: Any, review: Mapping[str, Any], step: str) -> dict[str, Any] | None:
    stages = _result_of(review).get("stages") or {}
    st = stages.get(step) if isinstance(stages, Mapping) else None
    gid = st.get("group_id") if isinstance(st, Mapping) else None
    return _group(sb, str(gid)) if gid else None


def _candidate_findings(findings: Sequence[ReviewFinding]) -> list[ReviewFinding]:
    """Findings the revise turn gets: every open finding above info with
    verified evidence or a fixed (rule, band, gate, scope) origin."""
    out = []
    for f in findings:
        if f.post_revision or f.severity == "info" or f.disposition not in ("open", "contested"):
            continue
        if f.origin == "reviewer" and not f.evidence_verified:
            continue
        out.append(f)
    return out


def _revise_pack(review: Mapping[str, Any], run: Mapping[str, Any], findings: Sequence[ReviewFinding],
                 loaded: LoadedInputs) -> dict[str, Any]:
    return {
        "review_id": str(review["id"]),
        "run_id": _run_id(run),
        "boq_version": loaded.boq_version,
        "documents": list(loaded.documents),
        "lines": pack_lines(loaded.boq_spec, loaded.line_values),
        "findings": [f.to_dict() for f in findings],
        "scope_digest": loaded.scope_digest,
        "total_usd": loaded.total_usd,
    }


def _evidence_for(findings: Sequence[ReviewFinding], dispositions: Sequence[Mapping[str, Any]]) -> dict[str, ops_mod.FindingEvidence]:
    """FindingEvidence per finding from the finding's verified evidence plus
    the verified page quotes the revise turn added. Rule, band, gate and
    scope findings count their own origin as a verified signal."""
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for f in findings:
        entries = [dict(e) for e in f.evidence]
        if f.origin in ("rule", "gate"):
            entries.append({"type": "rule", "verified": True, "policy": False, "ref_id": f.finding_id})
        elif f.origin == "scope":
            entries.append({"type": "rule", "verified": True, "policy": True, "ref_id": f.finding_id})
        elif f.origin == "band":
            entries.append({"type": "band", "verified": True, "ref_id": f.rule_id})
        elif f.origin == "recount":
            entries.append({"type": "recount", "verified": True, "outcome": "raise" if "raise" in f.title.lower() else "confirmed"})
        by_id[f.finding_id] = entries
    for d in dispositions:
        fid = str(d.get("finding_id") or "")
        if fid in by_id:
            by_id[fid].extend(e for e in d.get("evidence") or [] if isinstance(e, Mapping) and e.get("verified"))
    return {fid: reviewer_mod.evidence_signals(entries) for fid, entries in by_id.items()}


def _default_modify_path(sb: Any, run: Mapping[str, Any], spec: Mapping[str, Any], *, version: int) -> dict[str, Any]:
    """The modify path: the run's change slot, render_deliverable for the
    BOQ as version v+1 with REVISED_INSTRUCTION, the slot released."""
    from app.services import plans_reader_runs as runs

    group = runs.claim_change_slot(sb, _run_id(run), output={"kind": "boq", "review_revision": True}, status="running")
    try:
        row = runs.render_deliverable(sb, dict(run), "boq", dict(spec), version=version, instruction=REVISED_INSTRUCTION)
    except Exception as exc:
        sb.table(GROUPS_TABLE).update({"status": "failed", "error": str(exc)[:500], "finished_at": _now_iso()}).eq(
            "id", group["id"]).execute()
        raise
    sb.table(GROUPS_TABLE).update({"status": "done", "finished_at": _now_iso(),
                                   "output": {**(group.get("output") or {}), "version": row.get("version")}}).eq("id", group["id"]).execute()
    return dict(row)


def apply_revision(
    sb: Any,
    run: Mapping[str, Any],
    review: Mapping[str, Any],
    *,
    settings: Any = None,
    loaded: LoadedInputs | None = None,
    modify_path: ModifyPath | None = None,
    enqueue_pricing: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Apply the revise dispositions' ops with the evidence rules (shadow:
    computed, not written; apply: BOQ v+1 through the modify path, older
    pricing stale, the ai_pricing job queued with the review marker)."""
    from app.services import pricing_run

    review_id = str(review["id"])
    run_id = _run_id(run)
    mode = str(review.get("mode") or "shadow")
    result = _result_of(review)
    findings = stored_findings(sb, review_id)
    by_id = {f.finding_id: f for f in findings}
    dispositions = [d for d in result.get("dispositions") or [] if isinstance(d, Mapping)]
    if loaded is None:
        base_row = _pricing_version(sb, run_id, int(review.get("base_pricing_version") or 0))
        if base_row is None:
            return _finish(sb, review, stage="failed", error="the base pricing version is gone", step="apply")
        loaded = load_inputs(sb, run, base_row, settings=settings)
    ops: list[dict[str, Any]] = []
    for d in dispositions:
        fid = str(d.get("finding_id") or "")
        if fid not in by_id or d.get("decision") not in ("apply", "modify"):
            continue
        ops.extend({**dict(op), "finding_id": fid} for op in d.get("ops") or [])
    evidence = _evidence_for(findings, dispositions)
    conditions = {"wage_type": loaded.wage_type}
    pages = {str(d.get("id")): int(d.get("pages") or 0) for d in loaded.documents if d.get("id") and d.get("pages")}
    spec, applied, rejected = ops_mod.apply_ops(
        loaded.boq_spec or {"areas": []}, ops, evidence=evidence, allowance_names=loaded.allowance_names,
        pages_of=pages or None, conditions=conditions, line_values=loaded.line_values, total_usd=loaded.total_usd,
    )
    applied_ids = {str(a.get("finding_id")) for a in applied if a.get("finding_id")}
    rejected_ids = {str(r.get("finding_id")) for r in rejected if r.get("finding_id")}
    for d in dispositions:
        fid = str(d.get("finding_id") or "")
        f = by_id.get(fid)
        if f is None:
            continue
        decision = str(d.get("decision") or "")
        if decision == "decline":
            f.disposition, f.disposition_reason, f.disposition_by = "declined", str(d.get("reason") or "")[:300], "revise"
        elif fid in rejected_ids:
            f.disposition, f.disposition_by = "rejected_by_rule", "code"
            f.disposition_reason = "; ".join(f"{r.get('code')}: {r.get('message')}" for r in rejected if str(r.get("finding_id")) == fid)[:300]
        elif fid in applied_ids:
            f.disposition, f.disposition_by = ("applied" if mode == "apply" else "open"), ("revise" if mode == "apply" else None)
            f.disposition_reason = (str(d.get("reason") or "")[:300] if mode == "apply" else "shadow mode: ops computed, not written")
        else:
            f.disposition, f.disposition_reason, f.disposition_by = "contested", "no applicable op", "code"
    write_findings(sb, review_id, run_id, findings)
    revision = {
        "mode": mode, "ops": len(ops), "applied": applied, "rejected": rejected,
        "summary": dict(spec.get("review_revision") or {}),
    }
    result["revision"] = revision
    if mode != "apply" or not applied:
        _set_step(result, "apply", "skipped", reason="shadow mode" if mode != "apply" else "no op applied")
        doc = _result_doc({**review, "result": result}, findings, verdict="incomplete", reasons=[])
        verdict, reasons = verdict_for(findings, stage="done")
        doc.update({"verdict": verdict, "reasons": reasons})
        return _update_review(sb, review_id, {"stage": "done", "verdict": verdict, "reasons": reasons, "result": doc,
                                              "finished_at": _now_iso()})
    version = int(loaded.boq_version or 0) + 1
    path = modify_path or _default_modify_path
    try:
        row = path(sb, run, spec, version=version)
    except Exception as exc:  # noqa: BLE001 - the revision could not be written; the review ends needs_human
        logger.exception("review: writing BOQ v%s of run %s failed", version, run_id)
        _set_step(result, "apply", "failed", error=str(exc)[:300])
        return _finish(sb, {**review, "result": result}, stage="failed", error=f"revision not written: {str(exc)[:200]}")
    new_version = int(row.get("version") or version)
    _set_step(result, "apply", "done", boq_version=new_version, applied=len(applied), rejected=len(rejected))
    try:
        pricing_run.mark_stale(sb, run_id, except_boq_version=new_version)
    except Exception:  # noqa: BLE001
        logger.exception("review: flagging stale pricing of run %s failed", run_id)
    queued = None
    try:
        queue = enqueue_pricing or pricing_run.enqueue_pricing
        queued = queue(sb, dict(run), settings=settings, reason=REVIEW_REVISION_MARKER)
    except Exception as exc:  # noqa: BLE001 - without a re-price the review ends on the revised BOQ
        logger.exception("review: queueing the re-price of run %s failed", run_id)
        _set_step(result, "recheck", "failed", error=str(exc)[:300])
        return _finish(sb, {**review, "result": result}, stage="partial", error=f"re-price not queued: {str(exc)[:200]}")
    _set_step(result, "recheck", "queued", job_id=(queued or {}).get("id") if isinstance(queued, Mapping) else None,
              boq_version=new_version)
    doc = _result_doc({**review, "result": result}, findings, verdict=None, reasons=["re-pricing the revised BOQ"])
    return _update_review(sb, review_id, {"stage": "recheck", "result": doc, "verdict": None, "reasons": doc["reasons"]})


def recheck(sb: Any, run: Mapping[str, Any], review: Mapping[str, Any], pricing_row: Mapping[str, Any], *,
            settings: Any = None, loaded: LoadedInputs | None = None, bands: Any = None) -> dict[str, Any]:
    """The $0 recheck on the revised pricing version: the rules lane and
    the bands again, findings post_revision, the review closed with the
    verdict of the recheck."""
    review_id = str(review["id"])
    run_id = _run_id(run)
    data = loaded if loaded is not None else load_inputs(sb, run, pricing_row, settings=settings)
    checks = band_checks_for(sb, data, bands=bands)
    data.inputs.band_checks = checks
    rules = rules_mod.run_rules(data.inputs)
    existing = stored_findings(sb, review_id)
    start = _next_finding_start(existing)
    post = []
    for f in rules.findings:
        f.post_revision = True
        post.append(f)
    post = rules_mod.number_findings(post, start=start)
    write_findings(sb, review_id, run_id, post)
    result = _result_of(review)
    _set_step(result, "recheck", "done", pricing_version=int(pricing_row.get("version") or 0), findings=len(post),
              notes=rules.notes)
    result["bands_after"] = [c.to_dict() for c in checks]
    all_findings = [*existing, *post]
    verdict, reasons = verdict_for(all_findings, stage="done", revised=True)
    doc = _result_doc({**review, "result": result, "revised_pricing_version": int(pricing_row.get("version") or 0)},
                      all_findings, verdict=verdict, reasons=reasons)
    return _update_review(sb, review_id, {"stage": "done", "verdict": verdict, "reasons": reasons, "result": doc,
                                          "revised_pricing_version": int(pricing_row.get("version") or 0),
                                          "finished_at": _now_iso()})


def advance(
    sb: Any,
    run: Mapping[str, Any],
    *,
    settings: Any = None,
    enqueue: Enqueue | None = None,
    loaded: LoadedInputs | None = None,
    modify_path: ModifyPath | None = None,
    enqueue_pricing: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """Move the run's open review one step (module docstring). Idempotent:
    a step whose group is still queued or running returns without writing.
    Called by `plans_reader_runs.advance` on every done run (hook) after
    any group settles, so it is cheap when nothing is pending: nothing at
    all when the mode is off, else one read of the reviews table and no
    write when no review is open; it never raises on a run without
    reviews (a failed read is logged and reads as none)."""
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    if mode_of(settings) == "off":
        return None
    run_id = _run_id(run)
    try:
        review = next((r for r in _review_rows(sb, run_id) if r.get("stage") not in TERMINAL_STAGES), None)
    except Exception as exc:  # noqa: BLE001 - a run without a reviews table has no review to advance
        logger.warning("review: reading the reviews of run %s failed: %s", run_id, str(exc)[:200])
        return None
    if review is None:
        return None
    stage = str(review.get("stage") or "")
    result = _result_of(review)
    if stage == "review":
        status = _step_status(result, "review")
        group = _group_of_step(sb, review, "review")
        if status in ("queued", "pending"):
            if group is not None and group.get("status") == "done" and isinstance(group.get("output"), Mapping):
                review = record_reviewer_output(sb, run, group, group["output"]) or review
                result = _result_of(review)
                status = _step_status(result, "review")
            elif group is not None and group.get("status") in ("failed", "canceled"):
                return _finish(sb, review, stage="partial", error=str(group.get("error") or "review group failed"), step="review")
            else:
                return review
        if status == "failed":
            return _finish(sb, review, stage="partial", error="the reviewer did not answer", step="review")
        findings = stored_findings(sb, str(review["id"]))
        candidates = _candidate_findings(findings)
        if not candidates:
            _set_step(result, "revise", "skipped", reason="no finding to revise")
            return _finish(sb, {**review, "result": result}, stage="done")
        if loaded is None:
            base_row = _pricing_version(sb, run_id, int(review.get("base_pricing_version") or 0))
            if base_row is None:
                return _finish(sb, review, stage="failed", error="the base pricing version is gone")
            loaded = load_inputs(sb, run, base_row, settings=settings)
        pack = _revise_pack(review, run, candidates, loaded)
        route = _snapshot_route(run, reviewer_mod.REVISE_STEP, settings)
        group = _insert_group(sb, run, kind="revise", pack=pack, route=route, est_tokens=REVISE_EST_INPUT_TOKENS,
                              pass_no=REVISE_PASS, enqueue=enqueue)
        _set_step(result, "revise", "queued", group_id=str(group.get("id")), findings=len(candidates))
        doc = _result_doc({**review, "result": result}, findings, verdict=None, reasons=["revise in progress"])
        return _update_review(sb, str(review["id"]), {"stage": "revise", "result": doc,
                                                       "models": {**(review.get("models") or {}), "revise": route.get("model")}})
    if stage == "revise":
        status = _step_status(result, "revise")
        group = _group_of_step(sb, review, "revise")
        if status in ("queued", "pending"):
            if group is not None and group.get("status") == "done" and isinstance(group.get("output"), Mapping):
                review = record_revise_output(sb, run, group, group["output"]) or review
                status = _step_status(_result_of(review), "revise")
            elif group is not None and group.get("status") in ("failed", "canceled"):
                return _finish(sb, review, stage="partial", error=str(group.get("error") or "revise group failed"), step="revise")
            else:
                return review
        if status == "failed":
            return _finish(sb, review, stage="partial", error="the builder did not answer", step="revise")
        return apply_revision(sb, run, review, settings=settings, loaded=loaded, modify_path=modify_path,
                              enqueue_pricing=enqueue_pricing)
    if stage == "recheck":
        from app.services import pricing_run

        for row in pricing_run.versions(sb, run_id):
            if str(row.get("instruction") or "") == REVIEW_REVISION_MARKER and not row.get("stale"):
                return recheck(sb, run, review, row, settings=settings, loaded=loaded)
        return review
    if stage == "rules":
        return review
    return review


# Results -----------------------------------------------------------------------------------------


def result_for(sb: Any, run_id: str) -> dict[str, Any] | None:
    """The latest review of the run as a ReviewResult dict (5.4) with its
    findings and bands; None before any review."""
    review = latest_review(sb, str(run_id))
    if review is None:
        return None
    findings = stored_findings(sb, str(review["id"]))
    result = _result_of(review)
    out = {
        **result,
        "schema": RESULT_SCHEMA,
        "review_id": str(review.get("id")),
        "run_id": str(review.get("run_id")),
        "mode": review.get("mode"),
        "stage": review.get("stage"),
        "base_pricing_version": review.get("base_pricing_version"),
        "revised_pricing_version": review.get("revised_pricing_version"),
        "verdict": review.get("verdict") or result.get("verdict") or "incomplete",
        "reasons": list(review.get("reasons") or result.get("reasons") or []),
        "findings": [f.to_dict() for f in findings],
        "bands": list(result.get("bands") or []),
        "cost_usd": _f(review.get("cost_usd")) or 0.0,
        "models": dict(review.get("models") or {}),
        "stages": dict(result.get("stages") or {}),
        "error": review.get("error"),
        "created_at": review.get("created_at"),
        "finished_at": review.get("finished_at"),
    }
    return out


BAND_MONEY_KEYS: tuple[str, ...] = ("value", "p10", "p25", "p50", "p75", "p90")
FINDING_MONEY_KEYS: tuple[str, ...] = ("impact_usd",)


def _redact_node(node: Any) -> Any:
    """A deep copy of `node` with the money keys of every band check of a
    money metric nulled, wherever the band sits (result["bands"],
    result["bands_after"], a nested copy under stages or rules), every
    finding's impact dollars nulled and every `move_share` dropped."""
    if isinstance(node, Mapping):
        out: dict[str, Any] = {}
        is_money_band = node.get("metric") in bands_mod.MONEY_METRICS
        is_finding = "finding_id" in node and "severity" in node
        for key, value in node.items():
            k = str(key)
            if k == "move_share":
                continue
            if is_money_band and k in BAND_MONEY_KEYS:
                out[k] = None
            elif is_finding and k in FINDING_MONEY_KEYS:
                out[k] = None
            else:
                out[k] = _redact_node(value)
        return out
    if isinstance(node, (list, tuple)):
        return [_redact_node(v) for v in node]
    return node


def redact(result: Mapping[str, Any]) -> dict[str, Any]:
    """The result for a non-pricing audience (spec 5.5): no impact dollars,
    no money band values anywhere (`bands`, `bands_after`, any nested band
    and the band facts inside a band finding's evidence), no R12 unit
    dollars (facts keyed as money are stripped and the detail loses its
    numbers), no cost, no revision dollar move. Each finding is rebuilt
    through `ReviewFinding.to_dict(include_money=False)` and then passed
    through `_redact_node` again, so a money band nested in its evidence is
    nulled the same way as a top-level band."""
    out = _redact_node(result)
    out["findings"] = [_redact_node(ReviewFinding.from_dict(f).to_dict(include_money=False))
                       for f in result.get("findings") or [] if isinstance(f, Mapping)]
    out["cost_usd"] = None
    out.pop("total_usd", None)
    return out


def review_brief_block(result: Mapping[str, Any] | None, include_money: bool) -> str:
    """A short text block for the brief and the chat: verdict, counts by
    severity, the open findings, the bands outside their range."""
    if not result:
        return "Independent review: not run."
    verdict = str(result.get("verdict") or "incomplete")
    findings = [f for f in result.get("findings") or [] if isinstance(f, Mapping)]
    counts = {s: sum(1 for f in findings if f.get("severity") == s) for s in ("critical", "warning", "info")}
    lines = [
        f"Independent review: {verdict} (pricing v{result.get('base_pricing_version')}"
        + (f", revised v{result.get('revised_pricing_version')}" if result.get("revised_pricing_version") else "")
        + f"); {counts['critical']} critical, {counts['warning']} warning, {counts['info']} info.",
    ]
    for f in findings:
        if f.get("severity") == "info":
            continue
        lid = ",".join(f.get("line_ids") or []) or "-"
        imp = f.get("impact_usd") if include_money else None
        lines.append(f"- {f.get('finding_id')} [{f.get('severity')}, {f.get('disposition')}] {f.get('title')} (lines {lid})"
                     + (f" impact ${imp:,.0f}" if isinstance(imp, (int, float)) else ""))
    out_of_band = [b for b in result.get("bands") or [] if isinstance(b, Mapping) and b.get("severity") in ("warning", "critical")]
    for b in out_of_band:
        if b.get("metric") in bands_mod.MONEY_METRICS and not include_money:
            lines.append(f"- band {b.get('metric')}: {b.get('position')}")
        else:
            lines.append(f"- band {b.get('metric')}: {b.get('value')} vs p10 {b.get('p10')} to p90 {b.get('p90')} ({b.get('position')})")
    if result.get("reasons"):
        lines.append("Reasons: " + "; ".join(str(r) for r in result.get("reasons") or [])[:400])
    return "\n".join(lines)


__all__ = [
    "CHAIN_VERSION", "RESULT_SCHEMA", "REVIEW_REVISION_MARKER", "REVISED_INSTRUCTION", "MODES", "STAGES", "VERDICTS",
    "OPEN_DISPOSITIONS", "RERUN_ORIGINS", "ReviewRefused", "LoadedInputs", "load_inputs", "line_values_from_artifact", "band_checks_for", "pack_lines",
    "build_review_pack", "begin", "advance", "apply_revision", "recheck", "result_for", "redact",
    "review_brief_block", "record_reviewer_output", "record_revise_output", "finish_partial", "mark_stale",
    "on_boq_changed", "verdict_for", "stored_findings", "write_findings", "latest_review", "review_for_version",
    "mode_of",
]
