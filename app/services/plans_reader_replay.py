"""Replay a stored run bundle through the pipeline stages at $0
(docs/AI_ESTIMATING_BUILD_SPEC.md WS0 step 8; section 4.1 is the spine).

STAGE_ORDER mirrors section 4.1 exactly:

    plan, merge, recount, derive, consolidate, enforce, gate, evidence, render, price, review, score

Owners: plan WS1; merge, consolidate and gate WS4; recount and review WS7
(stored outputs or a cassette, never live); derive WS3 and WS5; enforce WS5;
evidence WS8; price WS6; render and score WS0. Each stage is a registry entry
(`register_stage`) that the owning workstream fills in; until then the legacy
entry registered here carries the stored outputs forward (the weekend bundle
replays end to end through the legacy entries). A stage function takes
`(ctx, artifacts)` and returns the artifacts it writes or replaces; it must be
pure over `ctx` (`ctx.sb is None`, documents through `ctx.docs`), deterministic,
and must not call a model unless registered with `llm=True` (such a stage may
only answer from a cassette: `replay()` refuses every provider call while it
runs, so a $0 replay can never spend).

Artifacts (JSON-able dicts keyed by name): `documents`, `files`, `ledger`,
`groups`, `calls`, `chat`, `page_meta`, `category:<category>`, `main`,
`boq_spec`, `estimate_spec`, `brief_spec`, `marked_plans_spec`, `markups_spec`,
`scope_report`, `measurements`, `gate_report`, `evidence`, `rendered`,
`priced_estimate`, `chat_probe`, `review`, `score`, plus whatever a bundle's
`artifacts/` directory holds. A live run's bundle also loads (`stored_artifacts`):
`policy`, `measure` and `evidence` (the stored outputs of those scopes), the run
columns `bid_conditions`, `gate_summary` and `autonomy_state`, `gate_findings`
(rows) with the `gate_report` built from the active ones, `pricing_versions`
(rows) with the `priced_estimate` rebuilt from the newest non-stale version and
its lines, `quote_extractions` (rows) and `quotes` (QuoteRef dicts of the parsed
ones), `review` (the newest review row with its findings), `recounts` and
`signoffs`. A bundle without those members (the weekend bundle) yields none of
them. The render stage calls
`plans_reader_render.render_boq_xlsx` and `render_estimate_xlsx` directly with
`office_pdf_bytes` patched out and writes files only under the replay's
out_dir, never to storage. The score stage runs scorer v2 (`plans_reader_score`).

Nothing here touches a database or the network.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

from app.services import eval_bundle
from app.services.eval_bundle import DEFAULT_BACKUP_DIR, RunBundle

logger = logging.getLogger(__name__)

STAGE_ORDER: tuple[str, ...] = (
    "plan",
    "merge",
    "recount",
    "derive",
    "consolidate",
    "enforce",
    "gate",
    "evidence",
    "render",
    "price",
    "review",
    "score",
)
STAGE_OWNERS: dict[str, str] = {
    "plan": "WS1",
    "merge": "WS4",
    "recount": "WS7",
    "derive": "WS3+WS5",
    "consolidate": "WS4",
    "enforce": "WS5",
    "gate": "WS4",
    "evidence": "WS8",
    "render": "WS0",
    "price": "WS6",
    "review": "WS7",
    "score": "WS0",
}
LEGACY_VERSION = "legacy-1"
MODES: tuple[str, ...] = ("rerun", "stored", "skip")
REPLAY_SCHEMA = "bdr.eval.replay/1"

StageFn = Callable[["ReplayContext", dict[str, Any]], dict[str, Any]]


class ReplayError(RuntimeError):
    """A stage, a mode or a bundle cannot be replayed as asked."""


class ReplayLiveCallRefused(RuntimeError):
    """A provider call was attempted during a $0 replay."""


@dataclass(frozen=True)
class StageSpec:
    name: str
    fn: StageFn
    version: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    llm: bool
    owner: str
    legacy: bool = False


_STAGES: dict[str, StageSpec] = {}
_LEGACY_STAGES: dict[str, StageSpec] = {}


def register_stage(
    name: str,
    fn: StageFn,
    *,
    version: str,
    reads: Iterable[str] = (),
    writes: Iterable[str] = (),
    llm: bool = False,
    owner: str | None = None,
    legacy: bool = False,
) -> StageSpec:
    """Register (or replace) the implementation of one stage of STAGE_ORDER.
    A workstream's real implementation replaces the legacy entry; a legacy
    entry never replaces a real one."""
    if name not in STAGE_ORDER:
        raise ReplayError(f"unknown stage {name!r}; stages are {STAGE_ORDER}")
    if not callable(fn):
        raise ReplayError(f"stage {name}: fn must be callable")
    if not version:
        raise ReplayError(f"stage {name}: a version is required")
    existing = _STAGES.get(name)
    if legacy:
        _LEGACY_STAGES[name] = StageSpec(name=name, fn=fn, version=str(version), reads=tuple(reads),
                                         writes=tuple(writes), llm=bool(llm),
                                         owner=owner or STAGE_OWNERS[name], legacy=True)
    if existing is not None and legacy and not existing.legacy:
        return existing
    spec = StageSpec(
        name=name,
        fn=fn,
        version=str(version),
        reads=tuple(reads),
        writes=tuple(writes),
        llm=bool(llm),
        owner=owner or STAGE_OWNERS[name],
        legacy=legacy,
    )
    _STAGES[name] = spec
    return spec


def stage(name: str) -> StageSpec:
    try:
        return _STAGES[name]
    except KeyError:
        raise ReplayError(f"no stage registered as {name!r}") from None


def registered_stages() -> dict[str, StageSpec]:
    return {name: _STAGES[name] for name in STAGE_ORDER if name in _STAGES}


def legacy_stages() -> list[str]:
    return [name for name in STAGE_ORDER if name in _STAGES and _STAGES[name].legacy]


# Context ----------------------------------------------------------------------------------


class LocalDocBytes:
    """The `_DocBytes` interface of plans_reader_runs over a bundle: PDFs come
    from the backup or the bundle's inputs, cached until evicted."""

    def __init__(self, bundle: RunBundle, *, backup_dir: Path | str = DEFAULT_BACKUP_DIR) -> None:
        self.bundle = bundle
        self.backup_dir = Path(backup_dir)
        self.documents: dict[str, dict] = {str(d.get("id")): d for d in bundle.documents}
        self._cache: dict[str, bytes] = {}
        self.reads = 0

    def get(self, doc_id: str) -> bytes:
        if doc_id not in self.documents:
            raise ValueError("A sheet references a document that is not in this run.")
        if doc_id not in self._cache:
            self._cache[doc_id] = self.bundle.doc_bytes(doc_id, backup_dir=self.backup_dir)
            self.reads += 1
        return self._cache[doc_id]

    def evict(self, doc_id: str) -> None:
        self._cache.pop(doc_id, None)

    def available(self, doc_id: str) -> bool:
        try:
            self.get(doc_id)
        except (eval_bundle.BundleError, OSError, ValueError):
            return False
        return True


@dataclass
class ReplayContext:
    bundle: RunBundle
    docs: LocalDocBytes
    out_dir: Path
    case_id: str | None = None
    reference: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    backup_dir: Path = DEFAULT_BACKUP_DIR
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    sb: Any = None  # always None: stages read documents through ctx.docs
    allow_live: bool = False
    sweep_id: str | None = None
    render_marked: bool = False
    notes: list[str] = field(default_factory=list)
    objects: dict[str, Any] = field(default_factory=dict)  # non-JSON side products (the CaseScore)

    def note(self, text: str) -> None:
        self.notes.append(text)


# Artifacts ----------------------------------------------------------------------------------


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def stored_artifacts(bundle: RunBundle) -> dict[str, Any]:
    """The artifacts a bundle holds before any stage reruns: run rows, the
    category and main outputs, the latest deliverable specs, and the
    bundle's artifacts/ directory."""
    run = bundle.run
    art: dict[str, Any] = {
        "documents": [dict(d) for d in bundle.documents],
        "files": [dict(f) for f in bundle.files],
        "ledger": dict(run.get("ledger") or {}),
        "settings_snapshot": dict(run.get("settings_snapshot") or {}),
        "groups": [dict(g) for g in bundle.groups],
        "calls": list(bundle.calls),
        "chat": list(bundle.chat),
    }
    for cat in bundle.categories:
        art[f"category:{cat}"] = bundle.category_output(cat)
    if bundle.main_output is not None:
        art["main"] = bundle.main_output
    for kind, key in (("boq", "boq_spec"), ("estimate", "estimate_spec"), ("brief", "brief_spec"),
                      ("marked_plans", "marked_plans_spec")):
        latest = bundle.latest_deliverable(kind)
        if latest is not None:
            art[key] = latest[1]
            art.setdefault("deliverable_versions", {})[kind] = latest[0]
    probe = bundle.chat_probe()
    if probe is not None:
        art["chat_probe"] = probe
    art.update(live_run_artifacts(bundle))
    for name, value in bundle.artifacts.items():
        art[name] = value
    return art


def live_run_artifacts(bundle: RunBundle) -> dict[str, Any]:
    """The artifacts of a live run's bundle members (module docstring);
    empty for a bundle that has none of them. Every piece is best effort: a
    member that cannot be turned into its artifact is left out and noted in
    `bundle.notes`, never raised."""
    art: dict[str, Any] = {}
    for scope in ("policy", "measure", "evidence"):
        out = bundle.scope_output(scope)
        if out is not None:
            art[scope] = out
            art.setdefault("output_versions", {})[scope] = bundle.output_versions.get(scope)
    for col, value in bundle.run_columns.items():
        if col in ("bid_conditions", "gate_summary", "autonomy_state"):
            art[col] = value
    if bundle.has_member("gate_findings.json"):
        art["gate_findings"] = [dict(r) for r in bundle.gate_finding_rows]
        try:
            from app.services.estimating_contracts import GateFinding, gate_report_from_findings

            art["gate_report"] = gate_report_from_findings(GateFinding.from_row(r) for r in bundle.gate_finding_rows)
        except Exception as exc:  # noqa: BLE001 - a malformed finding row leaves the report out
            bundle.notes.append(f"gate_report not built from gate_findings.json: {type(exc).__name__}: {str(exc)[:120]}")
    if bundle.has_member("quotes.json"):
        art["quote_extractions"] = [dict(r) for r in bundle.quote_rows]
    if bundle.has_member("pricing.json"):
        art["pricing_versions"] = [dict(r) for r in bundle.pricing_rows]
        latest = bundle.latest_pricing()
        if latest is not None:
            try:
                priced, quote_refs = priced_estimate_from_rows(
                    bundle.run, latest, bundle.pricing_version_lines(int(latest.get("version") or 0)), bundle.quote_rows,
                )
                art["priced_estimate"] = priced
                art["pricing_version"] = int(latest.get("version") or 0)
                if quote_refs:
                    art["quotes"] = quote_refs
            except Exception as exc:  # noqa: BLE001 - the rows stay; only the artifact is absent
                bundle.notes.append(f"priced_estimate not rebuilt from pricing v{latest.get('version')}: "
                                    f"{type(exc).__name__}: {str(exc)[:160]}")
    review = bundle.latest_review()
    if review is not None:
        art["review"] = review
    if bundle.has_member("recounts.json"):
        art["recounts"] = [dict(r) for r in bundle.recount_rows]
    if bundle.has_member("signoffs.json"):
        art["signoffs"] = [dict(r) for r in bundle.signoff_rows]
    return art


def priced_estimate_from_rows(
    run: Mapping[str, Any],
    pricing_row: Mapping[str, Any],
    line_rows: Iterable[Mapping[str, Any]],
    quote_rows: Iterable[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The `bdr.priced_estimate/1` artifact of one stored pricing version,
    built the way the review chain rebuilds it from the database
    (plans_reader_review._artifact_from_version: PricedEstimate.from_dict
    over the row and its lines, then pricing_engine.to_priced_estimate_v1),
    with the version's context allowances, the parsed quotes of the run
    (QuoteRef plus the selection reason and reconciled counts the version's
    categories recorded) and, under `metadata`, the version, its BOQ
    version and the excluded project ids of its context. Returns the
    artifact and the QuoteRef dicts. Pure: no database."""
    from app.services import pricing_engine as pe
    from app.services.pricing_types import Allowance, PricedEstimate

    lines = [r.get("line") for r in line_rows if isinstance(r.get("line"), Mapping)]
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
    allowances = []
    for a in ctx.get("allowances") or ():
        if isinstance(a, Mapping) and a.get("name"):
            try:
                allowances.append(Allowance.from_dict(a))
            except Exception:  # noqa: BLE001 - a malformed allowance is skipped, not fabricated
                continue
    refs, details = _quote_refs(quote_rows, priced.categories)
    artifact = pe.to_priced_estimate_v1(
        priced, run_id=str(run.get("id") or "") or None, wage_type=ctx.get("wage_type"),
        quotes=refs, allowances=allowances, quote_details=details,
    )
    meta = dict(artifact.get("metadata") or {})
    meta["pricing_version"] = pricing_row.get("version")
    meta["boq_version"] = pricing_row.get("boq_version")
    meta["stale"] = bool(pricing_row.get("stale"))
    if isinstance(ctx.get("exclude_project_ids"), list):
        meta["excluded_project_ids"] = [str(i) for i in ctx["exclude_project_ids"]]
    if ctx.get("eval_case_id") is not None:
        meta["eval_case_id"] = ctx.get("eval_case_id")
    artifact["metadata"] = meta
    return artifact, [r.to_dict() for r in refs]


def _quote_refs(quote_rows: Iterable[Mapping[str, Any]], categories: Mapping[str, Any]) -> tuple[list[Any], dict[str, dict[str, Any]]]:
    """QuoteRefs of the parsed quote rows and, per quote id, what the
    version's categories recorded about it (chosen, selection reason,
    reconciled counts)."""
    rows = [r for r in quote_rows if isinstance(r, Mapping) and r.get("status") == "parsed" and r.get("id")]
    if not rows:
        return [], {}
    from app.services import vendor_quotes as vq

    refs = []
    for row in rows:
        try:
            parsed = vq.ParsedQuote.from_row(row)
            refs.append(vq.quote_ref(str(row["id"]), parsed, category=str(row.get("category") or "Lighting"),
                                     file_sha256=row.get("file_sha256")))
        except Exception:  # noqa: BLE001 - an unreadable quote row is left out
            continue
    details: dict[str, dict[str, Any]] = {}
    for entry in (categories or {}).values():
        if not isinstance(entry, Mapping) or not entry.get("quote_id"):
            continue
        qid = str(entry["quote_id"])
        details[qid] = {"chosen": True, "selection_reason": entry.get("quote_selection_reason"),
                        "reconciled": entry.get("quote_reconciled")}
    return refs, details


def _category_names(artifacts: Mapping[str, Any]) -> list[str]:
    return sorted(k.split(":", 1)[1] for k in artifacts if k.startswith("category:"))


# Legacy stage implementations ----------------------------------------------------------------


def _legacy_plan(ctx: ReplayContext, art: dict[str, Any]) -> dict[str, Any]:
    """Carries the stored page_meta and ledger forward (WS1 replaces this
    with page facts, relabel, spec selection and planned requests)."""
    page_meta = []
    for d in art.get("documents") or []:
        for m in d.get("page_meta") or []:
            if isinstance(m, Mapping):
                page_meta.append({"document_id": d.get("id"), "category": d.get("category"), **m})
    return {"page_meta": page_meta, "ledger": dict(art.get("ledger") or {})}


_MERGE_BOOKKEEPING = ("source", "escalation_patches", "unresolved_flags")


def _content(output: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in output.items() if k not in _MERGE_BOOKKEEPING}


def _legacy_merge(ctx: ReplayContext, art: dict[str, Any]) -> dict[str, Any]:
    """Rebuilds each category output from the stored group outputs with the
    code path of today (`plans_reader_runs._category_output`, pure over
    group dicts) and notes where it differs from the stored output."""
    from app.services.plans_reader_runs import _category_output

    out: dict[str, Any] = {}
    groups = list(art.get("groups") or [])
    for cat in _category_names(art):
        rebuilt, _sources = _category_output(cat, groups)
        if rebuilt is None:
            ctx.note(f"merge: category {cat} has no done sub group output; stored output kept")
            continue
        stored = art.get(f"category:{cat}")
        if isinstance(stored, dict) and stored.get("rows_version"):
            # A live run's v2 row set (WS4 merge) carries row ids the later
            # stages and the acceptance readers cite; the v1 rebuild would
            # drop them, so the stored output is kept as is.
            ctx.note(f"merge: stored v2 {cat} output kept (legacy rebuild would drop its row ids)")
            out[f"category:{cat}"] = stored
            continue
        if stored is not None and _sha(_content(rebuilt)) != _sha(_content(stored)):
            ctx.note(f"merge: rebuilt {cat} output differs from the stored one")
        out[f"category:{cat}"] = rebuilt
    return out


def _legacy_passthrough(name: str, note: str) -> StageFn:
    def fn(ctx: ReplayContext, art: dict[str, Any]) -> dict[str, Any]:
        ctx.note(f"{name}: {note}")
        return {}

    fn.__name__ = f"legacy_{name}"
    return fn


def _legacy_consolidate(ctx: ReplayContext, art: dict[str, Any]) -> dict[str, Any]:
    """The stored main output (today's single-call consolidation) cut into
    the deliverable specs with `plans_reader_runs.deliverable_spec`."""
    from app.services.plans_reader_runs import deliverable_spec

    main = art.get("main")
    if not isinstance(main, Mapping):
        ctx.note("consolidate: no stored main output; nothing to carry forward")
        return {}
    out: dict[str, Any] = {"main": main}
    for kind, key in (("boq", "boq_spec"), ("estimate", "estimate_spec"), ("brief", "brief_spec"),
                      ("marked_plans", "marked_plans_spec")):
        spec = deliverable_spec(kind, dict(main))
        if spec is not None:
            out[key] = spec
    markups = deliverable_spec("trench_markup", dict(main))
    if markups is not None:
        out["markups_spec"] = markups
    if isinstance(main.get("scope_policy"), Mapping):
        out["scope_report"] = main["scope_policy"]
    return out


def _legacy_enforce(ctx: ReplayContext, art: dict[str, Any]) -> dict[str, Any]:
    main = art.get("main")
    if isinstance(main, Mapping) and isinstance(main.get("scope_policy"), Mapping):
        return {"scope_report": main["scope_policy"]}
    ctx.note("enforce: legacy pass-through (no scope policy engine yet)")
    return {}


def _rows_as_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append({
            "area": r.area,
            "section": r.section,
            "group": r.group,
            "subgroup": r.subgroup,
            "description": r.description,
            "quantity": r.quantity,
            "unit": r.unit,
            "is_accessory": bool(r.is_accessory),
        })
    return out


def _legacy_render(ctx: ReplayContext, art: dict[str, Any]) -> dict[str, Any]:
    """BOQ and estimate workbooks (and the brief docx) rendered from the
    specs with the office converter patched out; files only under out_dir."""
    from app.services import plans_reader_render as render
    from app.services import plans_reader_score as score
    from app.services.plans_reader_runs import today_pacific

    boq_spec = art.get("boq_spec")
    if not isinstance(boq_spec, Mapping):
        ctx.note("render: no BOQ spec to render")
        return {}
    render_dir = ctx.out_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    rendered: dict[str, Any] = {"files": files, "office_pdf": "patched_out"}
    with mock.patch.object(render, "office_pdf_bytes", lambda *_a, **_k: None):
        boq_xlsx = render.render_boq_xlsx(dict(boq_spec))
        path = render_dir / "BOQ - COUNTS.xlsx"
        path.write_bytes(boq_xlsx)
        files["boq"] = str(path)
        rendered["boq_rows"] = _rows_as_dicts(score.parse_estimator_boq(boq_xlsx))
        estimate_spec = art.get("estimate_spec")
        if isinstance(estimate_spec, Mapping):
            est_boq = estimate_spec.get("boq") if isinstance(estimate_spec.get("boq"), Mapping) else boq_spec
            est = estimate_spec.get("estimate") if isinstance(estimate_spec.get("estimate"), Mapping) else estimate_spec
            xlsx = render.render_estimate_xlsx(dict(est_boq), dict(est), date=today_pacific())
            path = render_dir / "ELECTRICAL ESTIMATE.xlsx"
            path.write_bytes(xlsx)
            files["estimate"] = str(path)
        brief_spec = art.get("brief_spec")
        if isinstance(brief_spec, Mapping):
            project = (art.get("ledger") or {}).get("project") or {}
            name = str(project.get("name") or ctx.bundle.run.get("name") or "Project")
            try:
                docx = render.render_brief_docx(dict(brief_spec), project_name=name)
            except ValueError as exc:
                ctx.note(f"render: brief not rendered ({exc})")
            else:
                path = render_dir / "Deep Brief.docx"
                path.write_bytes(docx)
                files["brief"] = str(path)
        marked = art.get("marked_plans_spec")
        if ctx.render_marked and isinstance(marked, Mapping):
            from app.services.plans_reader_runs import _render_bytes

            try:
                data, _ct, _pdf = _render_bytes("marked_plans", dict(marked), dict(ctx.bundle.run), ctx.docs)
            except (ValueError, eval_bundle.BundleError, OSError) as exc:
                ctx.note(f"render: marked plans not rendered ({exc})")
            else:
                path = render_dir / "Marked Plans.pdf"
                path.write_bytes(data)
                files["marked_plans"] = str(path)
        elif isinstance(marked, Mapping):
            rendered["marked_plans"] = "skipped (render_marked is off)"
    return {"rendered": rendered}


def _legacy_score(ctx: ReplayContext, art: dict[str, Any]) -> dict[str, Any]:
    """Scorer v2 on the BOQ spec against the case reference."""
    from app.services import plans_reader_score as score

    boq_spec = art.get("boq_spec")
    if not isinstance(boq_spec, Mapping):
        ctx.note("score: no BOQ spec to score")
        return {}
    if not ctx.reference:
        ctx.note("score: no reference for this case; skipped")
        return {}
    rows = score.rows_from_spec(dict(boq_spec))
    priced = art.get("priced_estimate") if isinstance(art.get("priced_estimate"), Mapping) else None
    result = score.score_case(rows, ctx.reference, adjudicated=True, priced=priced, case_id=ctx.case_id)
    raw = score.score_case(rows, ctx.reference, adjudicated=False, priced=priced, case_id=ctx.case_id)
    ctx.objects["score"] = result
    ctx.objects["score_raw"] = raw
    return {
        "score": score.score_json(result),
        "score_raw": score.score_json(raw),
    }


def _register_legacy() -> None:
    register_stage("plan", _legacy_plan, version=LEGACY_VERSION, reads=("documents", "ledger"),
                   writes=("page_meta", "ledger"), legacy=True)
    register_stage("merge", _legacy_merge, version=LEGACY_VERSION, reads=("groups",),
                   writes=("category:*",), legacy=True)
    register_stage("recount", _legacy_passthrough("recount", "legacy pass-through (no recount groups in a v1 bundle)"),
                   version=LEGACY_VERSION, reads=("category:*",), writes=(), legacy=True)
    register_stage("derive", _legacy_passthrough("derive", "legacy pass-through (no measure or scope engine yet)"),
                   version=LEGACY_VERSION, reads=("category:*", "ledger"), writes=(), legacy=True)
    register_stage("consolidate", _legacy_consolidate, version=LEGACY_VERSION, reads=("main",),
                   writes=("main", "boq_spec", "estimate_spec", "brief_spec", "marked_plans_spec"), llm=False, legacy=True)
    register_stage("enforce", _legacy_enforce, version=LEGACY_VERSION, reads=("main",), writes=("scope_report",), legacy=True)
    register_stage("gate", _legacy_passthrough("gate", "legacy pass-through (gate_report carried from the bundle when stored)"),
                   version=LEGACY_VERSION, reads=("boq_spec",), writes=(), legacy=True)
    register_stage("evidence", _legacy_passthrough("evidence", "legacy pass-through (model-placed marks stay as stored)"),
                   version=LEGACY_VERSION, reads=("marked_plans_spec",), writes=(), legacy=True)
    register_stage("render", _legacy_render, version=LEGACY_VERSION,
                   reads=("boq_spec", "estimate_spec", "brief_spec", "marked_plans_spec"), writes=("rendered",), legacy=True)
    register_stage("price", _legacy_passthrough("price", "legacy pass-through (priced_estimate carried from the bundle when stored)"),
                   version=LEGACY_VERSION, reads=("boq_spec",), writes=(), legacy=True)
    register_stage("review", _legacy_passthrough("review", "legacy pass-through (no review stage stored)"),
                   version=LEGACY_VERSION, reads=("priced_estimate",), writes=(), legacy=True)
    register_stage("score", _legacy_score, version=LEGACY_VERSION, reads=("boq_spec", "priced_estimate"),
                   writes=("score", "score_raw"), legacy=True)


def reset_stages(*, real: bool = False) -> None:
    """Drop every registration and restore the legacy entries (tests), then
    re-register the real stages of every module already imported (they
    register at import and would otherwise vanish for the rest of the
    process); `real=False` keeps only the legacy entries."""
    _STAGES.clear()
    _register_legacy()
    if real:
        register_real_stages()


def register_real_stages() -> None:
    """Register every implemented stage whose module is importable: a real
    entry replaces the legacy one, never another real one. Modules that
    register at import are imported here on purpose."""
    import importlib

    for mod, fn in (
        # WS3 derive (measure) first, so a WS5 derive registered later chains before it.
        ("app.services.plans_reader_measure", "register_replay_stage"),
        ("app.services.plans_reader_merge", "register_replay_stage"),
        ("app.services.plans_reader_consolidator", "register_replay_stage"),
        ("app.services.plans_reader_gates", "register_replay_stage"),
        ("app.services.plans_reader_evidence", "register_replay_stage"),
        ("app.services.pricing_run", "register_replay_stage"),
        ("app.services.plans_reader_recount", "register_replay_stage"),
        ("app.services.plans_reader_review_rules", "register_replay_stage"),
    ):
        try:
            m = importlib.import_module(mod)
            getattr(m, fn)()
        except Exception:  # noqa: BLE001 - a missing sibling never breaks a replay
            continue


_register_legacy()


# The replay --------------------------------------------------------------------------------


@contextlib.contextmanager
def no_provider_calls() -> Iterator[None]:
    """Refuse every provider call for the duration (the replay guard): the
    llm module's guarded transport and client factories raise."""
    from app.services import llm

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise ReplayLiveCallRefused("a $0 replay never calls a model provider")

    with mock.patch.object(llm, "_guarded", refuse), mock.patch.object(llm, "_build_client", refuse), \
            mock.patch.object(llm, "_client_for", refuse):
        yield


@dataclass
class StageRun:
    name: str
    mode: str
    version: str
    owner: str
    legacy: bool
    reads: list[str]
    writes: list[str]
    written: list[str]
    inputs_sha256: str
    outputs_sha256: str
    seconds: float
    error: str | None = None


@dataclass
class ReplayResult:
    schema: str
    case_id: str | None
    run_id: str
    bundle_root: str
    from_stage: str
    to_stage: str
    stages: list[StageRun]
    artifacts: dict[str, Any]
    notes: list[str]
    code_version: str | None
    matcher_version: int
    out_dir: str
    score: Any = None  # the CaseScore object (not serialized)

    def stage(self, name: str) -> StageRun:
        for s in self.stages:
            if s.name == name:
                return s
        raise ReplayError(f"stage {name} was not part of this replay")

    @property
    def ok(self) -> bool:
        return not any(s.error for s in self.stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "bundle_root": self.bundle_root,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "code_version": self.code_version,
            "matcher_version": self.matcher_version,
            "out_dir": self.out_dir,
            "stages": [asdict(s) for s in self.stages],
            "artifacts": sorted(self.artifacts),
            "notes": list(self.notes),
            "replayed_at": eval_bundle.now_pacific_iso(),
        }

    def write(self, out_dir: Path | str | None = None, *, include_artifacts: bool = True) -> Path:
        """replay.json (the stage table) plus artifacts/<name>.json under
        the out dir. Artifacts are JSON-able by contract."""
        out = Path(out_dir) if out_dir is not None else Path(self.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "replay.json").write_text(json.dumps(self.to_dict(), indent=1, sort_keys=True, default=str) + "\n")
        if include_artifacts:
            art_dir = out / "artifacts"
            art_dir.mkdir(exist_ok=True)
            for name, value in self.artifacts.items():
                safe = name.replace(":", "_").replace("/", "_")
                (art_dir / f"{safe}.json").write_text(json.dumps(value, indent=1, sort_keys=True, default=str) + "\n")
        return out


def _code_version() -> str | None:
    try:
        from app.services.plans_reader_prompt_hash import code_version

        return code_version()
    except Exception:  # noqa: BLE001 - best effort, never fails a replay
        return None


def stage_window(from_stage: str, to_stage: str) -> list[str]:
    if from_stage not in STAGE_ORDER or to_stage not in STAGE_ORDER:
        raise ReplayError(f"stages must be in {STAGE_ORDER}; got {from_stage!r} and {to_stage!r}")
    a, b = STAGE_ORDER.index(from_stage), STAGE_ORDER.index(to_stage)
    if a > b:
        raise ReplayError(f"from_stage {from_stage} comes after to_stage {to_stage}")
    return list(STAGE_ORDER[a:b + 1])


def replay(
    bundle: RunBundle,
    *,
    reference: Mapping[str, Any] | None = None,
    expected: Mapping[str, Any] | None = None,
    case_id: str | None = None,
    from_stage: str = "plan",
    to_stage: str = "score",
    stages: Mapping[str, str] | None = None,
    out_dir: Path | str | None = None,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    allow_live: bool = False,
    render_marked: bool = False,
    settings_overrides: Mapping[str, Any] | None = None,
    legacy_only: bool = False,
) -> ReplayResult:
    """Run the stages `from_stage`..`to_stage` over the bundle's stored
    artifacts. `stages` pins a mode per stage: `rerun` (the registry entry,
    default), `stored` (carry the bundle's artifacts, run nothing) or
    `skip`. Stages before `from_stage` are `stored`. With `allow_live`
    False (always, in this build) every provider call raises."""
    if allow_live:
        raise ReplayError("live replay is not available in this build (spec 0.4: no live calls outside a sweep backend)")
    modes = {k: str(v) for k, v in (stages or {}).items()}
    for name, mode in modes.items():
        if name not in STAGE_ORDER:
            raise ReplayError(f"unknown stage {name!r} in stages")
        if mode not in MODES:
            raise ReplayError(f"stage {name}: mode must be one of {MODES}; got {mode!r}")
    window = stage_window(from_stage, to_stage)
    case = case_id or bundle.case_id
    out = Path(out_dir) if out_dir is not None else bundle.root / "replays" / f"replay_{int(time.time())}"
    out.mkdir(parents=True, exist_ok=True)
    docs = LocalDocBytes(bundle, backup_dir=backup_dir)
    ctx = ReplayContext(
        bundle=bundle,
        docs=docs,
        out_dir=out,
        case_id=case,
        reference=dict(reference) if reference else None,
        expected=dict(expected) if expected else None,
        backup_dir=Path(backup_dir),
        settings_overrides=dict(settings_overrides or {}),
        render_marked=render_marked,
    )
    artifacts = stored_artifacts(bundle)
    runs: list[StageRun] = []
    with no_provider_calls():
        for name in STAGE_ORDER:
            # legacy_only: the stored-bundle baseline (acceptance self-test)
            # must never run a real stage another module registered at import.
            spec = _LEGACY_STAGES.get(name) if legacy_only else _STAGES.get(name)
            mode = modes.get(name, "rerun" if name in window else "stored")
            if spec is None:
                runs.append(StageRun(name, "skip", "", STAGE_OWNERS[name], False, [], [], [], "", "", 0.0,
                                     error=f"no stage registered as {name}"))
                continue
            if mode != "rerun":
                runs.append(StageRun(name, mode, spec.version, spec.owner, spec.legacy, list(spec.reads), list(spec.writes),
                                     [], _sha({k: artifacts.get(k) for k in spec.reads}), "", 0.0))
                continue
            inputs_sha = _sha({k: artifacts.get(k) for k in spec.reads if not k.endswith("*")})
            started = time.monotonic()
            error = None
            written: list[str] = []
            try:
                produced = spec.fn(ctx, artifacts) or {}
                if not isinstance(produced, Mapping):
                    raise ReplayError(f"stage {name} returned {type(produced).__name__}, not a mapping")
                for k, v in produced.items():
                    artifacts[k] = v
                    written.append(k)
            except ReplayLiveCallRefused:
                raise
            except Exception as exc:  # noqa: BLE001 - recorded in the stage table
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("replay: stage %s failed: %s", name, error)
            runs.append(StageRun(
                name=name,
                mode="legacy" if spec.legacy else "rerun",
                version=spec.version,
                owner=spec.owner,
                legacy=spec.legacy,
                reads=list(spec.reads),
                writes=list(spec.writes),
                written=sorted(written),
                inputs_sha256=inputs_sha,
                outputs_sha256=_sha({k: artifacts.get(k) for k in written}),
                seconds=round(time.monotonic() - started, 3),
                error=error,
            ))
    from app.services.plans_reader_score import MATCHER_VERSION

    return ReplayResult(
        schema=REPLAY_SCHEMA,
        case_id=case,
        run_id=bundle.run_id,
        bundle_root=str(bundle.root),
        from_stage=from_stage,
        to_stage=to_stage,
        stages=runs,
        artifacts=artifacts,
        notes=list(ctx.notes),
        code_version=_code_version(),
        matcher_version=MATCHER_VERSION,
        out_dir=str(out),
        score=ctx.objects.get("score"),
    )


__all__ = [
    "STAGE_ORDER", "STAGE_OWNERS", "LEGACY_VERSION", "MODES", "REPLAY_SCHEMA",
    "ReplayError", "ReplayLiveCallRefused", "StageSpec", "register_stage", "stage", "registered_stages",
    "legacy_stages", "reset_stages", "LocalDocBytes", "ReplayContext", "stored_artifacts", "no_provider_calls",
    "live_run_artifacts", "priced_estimate_from_rows",
    "StageRun", "ReplayResult", "stage_window", "replay",
]
