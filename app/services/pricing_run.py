"""Price one AI Estimating run: the I/O wrapper around the pure engine
(AI_ESTIMATING_BUILD_SPEC WS6 `price_run`, 4.1 post-run chain, the
`ai_pricing` llm_jobs job).

`price_run(sb, run_id, *, boq_version=None, user=None)`:

1. loads the run (done or done_with_errors; anything else is refused) and
   its latest BOQ deliverable (or `boq_version`), and turns the BOQ lines
   into PriceRows through `plans_reader_rows.iter_boq_lines` (stable B ids;
   accessory fitting rows stay BOQ-only, as in the estimate workbook);
2. builds the PricingContext with `pricing_context_from_scope`: wage type
   from the run's bid conditions (WS5, `bid_conditions.for_run`), else the
   project record; SpecFacts from the material notes' grade modifiers, WS5
   facts (`facts_for_run`) and the scope context's cited device facts
   (`scope_context_for_run`; both injected, `execute_job` resolves
   scope_engine's lazily); the policy's standard allowances at $0 unless a
   document names an amount (the scope context's `document_amounts`); the
   project's GC ids;
3. loads the policy row (`ai_pricing_settings`, else the code policy); on
   an eval run (`run.eval_case_id` set) a grade seed without an amount takes
   it from the case's EVAL_ROOT/references/<case>/expected.json `seeds.*`,
   the same stand-in for the settings row the replay price stage uses
   (warning `eval_seed_amounts`; a seed still applies only with a cited
   fact), and a line that a cited device-grade fact names while its seed
   still has no amount is reported (warning `grade_seed_inert`); then
   the cost library (`CostLibrary.load`, excluding
   `eval_manifest.excluded_projects("pricing_library", run.eval_case_id)`
   and the run's own project; an absent table gives an empty library and a
   warning);
4. reads the run's vendor quotes (`vendor_quote_extractions`), parses the
   pending ones (the LLM fallback only through the injected caller and
   only when quote_lines_llm_fallback_enabled) and reconciles each against
   the BOQ rows (`vendor_quotes.reconcile`);
5. asks WS4 `gates.pricing_allowed(run)` through the contract (injected;
   the real module when it exists, else the typed "unavailable" refusal);
6. computes the estimate with the chosen quotes' reconciliations (a line
   the quote does not carry keeps library material, flag `not_in_quote`),
   applies the quote blocks (a line in a `uom_conflict_quote` finding is
   flagged `blocked` and the version's status becomes `blocked`) and
   records ONE batch per pricing pass through the injected WS4
   `record_findings(..., stage="pricing")`: the quote findings plus one
   GateFinding per engine blocker that is a gate id (linear_in_ea,
   wage_type_unknown). The call is made even with an empty batch, so a
   finding the pass no longer raises is superseded;
7. stores a version (`store_version`: idempotent on the inputs hash,
   unique (run_id, version), older versions flagged stale), renders the
   priced workbook (`pricing_render`) to run-private storage and calls the
   injected review begin hook (WS7, wired by the integrator).

The ai_pricing job (`execute_job`, `mark_from_queue`, `current_status`,
`error_message`) is described for llm_queue's `_JobSpec`: one job per run
(target_id = run id), claimed in the plans pass, lease renewed between
steps, gated by plans_reader_enabled and ai_pricing_enabled (never an RFP
flag). `autotrigger` enqueues it at step 6 of `_finish_with_main` and on
every new BOQ deliverable version when ai_pricing_autorun is on;
`on_new_boq_version` does nothing unless the run is finished (status
done or done_with_errors) and pricing is enabled, so the first render
(status rendering) never queues a job that would be refused.

Sync only (the queue runs it on a worker thread; the router calls it from
plain `def` routes). Never constructs a provider client.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services import bid_conditions as bc
from app.services import estimating_contracts as ec
from app.services import eval_manifest
from app.services import pricing_engine as pe
from app.services import pricing_render
from app.services import scope_policy
from app.services import vendor_quotes as vq
from app.services.cost_library import CostLibrary, LibraryUnavailable
from app.services.plans_reader_render import estimate_wastage
from app.services.plans_reader_rows import iter_boq_lines
from app.services.pricing_policy import DEFAULT_POLICY, PricingPolicy, inert_grade_seed_for
from app.services.pricing_types import (
    Allowance,
    PricedEstimate,
    PriceRow,
    PricingContext,
    PricingIssue,
    QuoteRef,
    SpecFact,
)

logger = logging.getLogger(__name__)

MODEL_LABEL = "ai_pricing"
PRICING_FEATURE = "pricing_quote"
LIBRARY_PURPOSE = "pricing_library"
PRICEABLE_RUN_STATUSES: tuple[str, ...] = ("done", "done_with_errors")
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("queued", "running")
JOB_TYPE = "ai_pricing"
VERSION_ATTEMPTS = 3
LINES_PAGE = 200
WARN_LIBRARY_UNAVAILABLE = "library_unavailable"
WARN_SETTINGS_DEFAULT = "policy_default"
WARN_EVAL_SEEDS = "eval_seed_amounts"
WARN_SEED_INERT = "grade_seed_inert"
_GRADE_WORDS = {
    "HOSPITAL GRADE": ("device_grade", "hospital"),
    "HOSPITAL-GRADE": ("device_grade", "hospital"),
    "SPEC GRADE": ("device_grade", "spec"),
    "SPEC-GRADE": ("device_grade", "spec"),
    "COMMERCIAL GRADE": ("device_grade", "commercial"),
    "INDUSTRIAL GRADE": ("device_grade", "industrial"),
}
_SIX = Decimal("0.000001")
_ZERO = Decimal(0)
_DOLLARS_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_NORM_RE = re.compile(r"[^A-Z0-9]+")
SELECTION_HUMAN = "human_selected"
SELECTION_LOWEST = "lowest_eligible"
SELECTION_ALTERNATIVE = "alternative"
ScopeContextForRun = Callable[[Any, str], Mapping[str, Any] | None]


class PricingRefused(ValueError):
    """A pricing request the service refuses (the router maps `status_code`)."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


# Helpers -------------------------------------------------------------------------------------


def _uid(user: Any) -> str | None:
    if user is None:
        return None
    if isinstance(user, Mapping):
        value = user.get("id")
    else:
        value = getattr(user, "id", None)
    return str(value) if value else None


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _table_missing(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    text = str(exc)
    return code in ("PGRST205", "42P01", "42703") or any(k in text for k in ("PGRST205", "42P01", "42703"))


def _one(sb: Any, table: str, **filters: Any) -> dict | None:
    q = sb.table(table).select("*")
    for col, val in filters.items():
        q = q.eq(col, val)
    rows = q.limit(1).execute().data or []
    return rows[0] if rows else None


def load_run(sb: Any, run_id: str) -> dict:
    run = _one(sb, "plans_reader_runs", id=str(run_id))
    if run is None:
        raise PricingRefused("This AI Estimating run no longer exists.", 404)
    return run


def _drop_none(row: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """The 5.6 MC rule: nullable new columns are omitted from an insert when
    the value is None, so the insert works before and after the column."""
    out = dict(row)
    for key in keys:
        if out.get(key) is None:
            out.pop(key, None)
    return out


# Policy -----------------------------------------------------------------------------------------


def load_policy(sb: Any) -> tuple[PricingPolicy, dict[str, Any]]:
    """The policy of `ai_pricing_settings` (row 1), else the code policy.
    Returns (policy, {source, version})."""
    if sb is None:
        return DEFAULT_POLICY, {"source": "default", "version": None}
    try:
        row = _one(sb, "ai_pricing_settings", id=1)
    except Exception as exc:  # noqa: BLE001 - before ME the table is absent
        if not _table_missing(exc):
            logger.exception("ai_pricing_settings read failed")
        return DEFAULT_POLICY, {"source": "default", "version": None}
    if not row or not row.get("policy"):
        return DEFAULT_POLICY, {"source": "default", "version": (row or {}).get("version")}
    try:
        policy = PricingPolicy.from_dict(row["policy"])
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("ai_pricing_settings.policy invalid (%s); using the code policy", exc)
        return DEFAULT_POLICY, {"source": "default", "version": row.get("version"), "error": str(exc)}
    return policy, {"source": "table", "version": row.get("version")}


def eval_expected(case_id: str | None, *, eval_root: Path | str | None = None) -> dict[str, Any] | None:
    """The eval case's expected.json under EVAL_ROOT (references/<case>/),
    or None (no case, no file, unreadable). Read only for grade seed
    amounts; nothing from it is written into a run's rows besides the
    priced lines it seeds."""
    if not case_id or "/" in str(case_id) or ".." in str(case_id):
        return None
    try:
        root = Path(eval_root) if eval_root is not None else eval_manifest.eval_root()
        path = root / "references" / str(case_id) / "expected.json"
        data = json.loads(path.read_text()) if path.is_file() else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def eval_seeded_policy(
    policy: PricingPolicy, run: Mapping[str, Any], *, eval_root: Path | str | None = None,
) -> tuple[PricingPolicy, bool]:
    """(policy, seeded): on an eval run whose policy has grade seeds without
    an amount (no ai_pricing_settings amounts on the dev database), those
    seeds take the case's expected.json amounts, as `replay_price` does.
    A production run (no eval_case_id) is never touched."""
    case_id = run.get("eval_case_id")
    if not case_id or all(seed.active for seed in policy.grade_seeds):
        return policy, False
    seeded = _seeded_from_expected(policy, eval_expected(str(case_id), eval_root=eval_root))
    return seeded, seeded.grade_seeds != policy.grade_seeds


def inert_seed_warning(priced: PricedEstimate, policy: PricingPolicy, ctx: PricingContext) -> PricingIssue | None:
    """A warning naming the lines a cited device-grade fact covers while
    their policy seed has no amount (priced from the library, so the grade
    premium is missing until ai_pricing_settings carries the amount)."""
    ids: list[str] = []
    refs: list[str] = []
    for ln in priced.lines:
        if pe.FLAG_SEED in ln.flags:
            continue
        hit = inert_grade_seed_for(policy, (str(ln.item_key or ""),), ctx.spec_facts)
        if hit is not None:
            ids.append(ln.line_id)
            if hit[1].source_ref not in refs:
                refs.append(hit[1].source_ref)
    if not ids:
        return None
    return PricingIssue(
        code=WARN_SEED_INERT,
        detail=f"{len(ids)} lines are cited as {'; '.join(refs)[:160]} but the grade seed has no amount; priced from the library",
        line_ids=tuple(ids),
    )


def save_policy(sb: Any, doc: Mapping[str, Any], *, user: Any) -> dict[str, Any]:
    """Validate and store a policy document; bumps the version. Raises
    ValueError on an invalid document (nothing written)."""
    policy = PricingPolicy.from_dict(doc)
    current = _one(sb, "ai_pricing_settings", id=1) or {"version": 0}
    patch = {
        "policy": policy.to_dict(),
        "version": int(current.get("version") or 0) + 1,
        "updated_by": _uid(user),
        "updated_at": _now_iso(),
    }
    rows = sb.table("ai_pricing_settings").update(patch).eq("id", 1).execute().data or []
    if not rows:
        rows = sb.table("ai_pricing_settings").insert({"id": 1, **patch}).execute().data or [patch]
    return rows[0]


# Rows ----------------------------------------------------------------------------------------------


def boq_rows(boq_spec: Mapping[str, Any]) -> list[PriceRow]:
    """PriceRows of a BOQ spec: one per non-accessory item, keyed on the
    spec's line ids (transient ids in BOQ order for legacy specs)."""
    rows: list[PriceRow] = []
    for item in iter_boq_lines(boq_spec):
        if item.get("is_accessory"):
            continue
        try:
            base = Decimal(str(item.get("quantity") or 0))
        except (ValueError, ArithmeticError):
            continue
        if base <= 0:
            continue
        wastage = Decimal(str(estimate_wastage(item)))
        quantity = (base * (1 + wastage)).quantize(_SIX)
        basis = item.get("basis") if item.get("basis") in ec.QUANTITY_BASES else None
        rows.append(PriceRow(
            line_id=str(item["line_id"]),
            description=str(item.get("description") or ""),
            group=str(item.get("group") or ""),
            subgroup=item.get("subgroup") or None,
            section=item.get("section") or None,
            unit=str(item.get("unit") or "EA"),
            quantity=quantity,
            qty_base=base,
            wastage_applies=wastage > 0,
            material_note=item.get("material_note"),
            caveat=item.get("caveat"),
            sheet_refs=tuple(str(r) for r in item.get("sheet_refs") or ()),
            quantity_basis=basis,
            item_key=item.get("item_key"),
            material_category=item.get("material_category"),
            area=item.get("area"),
        ))
    return rows


def latest_boq(sb: Any, run_id: str, *, version: int | None = None) -> dict | None:
    q = sb.table("plans_reader_deliverables").select("*").eq("run_id", run_id).eq("kind", "boq")
    if version is not None:
        q = q.eq("version", int(version))
    rows = q.execute().data or []
    return max(rows, key=lambda r: int(r.get("version") or 0)) if rows else None


def main_output(sb: Any, run_id: str) -> dict | None:
    rows = sb.table("plans_reader_outputs").select("*").eq("run_id", run_id).eq("scope", "main").execute().data or []
    if not rows:
        return None
    row = max(rows, key=lambda r: int(r.get("version") or 0))
    return row.get("output") if isinstance(row.get("output"), dict) else None


# Context ------------------------------------------------------------------------------------------


def spec_facts_from_material_notes(main: Mapping[str, Any] | None) -> list[SpecFact]:
    """Grade modifiers named in the main output's material notes, cited as
    material_note:<category> (the engine's seeds apply only with a cited
    fact)."""
    out: list[SpecFact] = []
    seen: set[tuple[str, str, str]] = set()
    for note in (main or {}).get("material_notes") or ():
        if not isinstance(note, Mapping):
            continue
        category = str(note.get("material_category") or "")
        texts = [str(t) for t in (note.get("notes") or ()) if t]
        texts.extend(str(t) for t in (note.get("specified_manufacturers") or ()) if t)
        for text in texts:
            up = text.upper()
            for word, (key, value) in _GRADE_WORDS.items():
                if word in up and (key, value, category) not in seen:
                    seen.add((key, value, category))
                    out.append(SpecFact(key=key, value=value, source_ref=f"material_note:{category or 'general'}"))
    return out


def spec_facts_from_facts(facts: Iterable[ec.Fact]) -> list[SpecFact]:
    """WS5 facts whose statement names a device grade, cited by fact id."""
    out: list[SpecFact] = []
    for fact in facts:
        up = (fact.statement or "").upper()
        for word, (key, value) in _GRADE_WORDS.items():
            if word in up:
                out.append(SpecFact(key=key, value=value, source_ref=fact.id))
                break
    return out


def spec_facts_from_scope_context(scope_ctx: Mapping[str, Any] | None) -> list[SpecFact]:
    """The scope context's cited device facts (WS5 `device_facts.spec_facts`,
    e.g. hospital-grade devices with the drawing note as source_ref). A
    fact without a citation is dropped (a seed needs a cited fact)."""
    out: list[SpecFact] = []
    device = scope_ctx.get("device_facts") if isinstance(scope_ctx, Mapping) else None
    raws = device.get("spec_facts") if isinstance(device, Mapping) else None
    for raw in raws or ():
        if not isinstance(raw, Mapping) or not raw.get("key") or not raw.get("value") or not raw.get("source_ref"):
            continue
        fact = SpecFact(key=str(raw["key"]), value=str(raw["value"]), source_ref=str(raw["source_ref"]))
        if fact not in out:
            out.append(fact)
    return out


def _norm_words(text: Any) -> str:
    return " ".join(_NORM_RE.sub(" ", str(text or "").upper()).split())


def document_allowance_amount(name: str, document_amounts: Iterable[Mapping[str, Any]]) -> Decimal | None:
    """The dollar figure a document names for allowance `name` (the scope
    context's `document_amounts`: text plus matched phrase), matched when at
    least half of the name's longer words appear in the text; else None."""
    words = [w for w in _norm_words(name).split() if len(w) > 3]
    if not words:
        return None
    for entry in document_amounts or ():
        if not isinstance(entry, Mapping):
            continue
        text = _norm_words(entry.get("text"))
        if sum(1 for w in words if w in text) < max(1, len(words) // 2):
            continue
        m = _DOLLARS_RE.search(str(entry.get("match") or "")) or _DOLLARS_RE.search(str(entry.get("text") or ""))
        if not m:
            continue
        try:
            amount = Decimal(m.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        if amount > 0:
            return amount
    return None


def standard_allowances(
    policy_doc: Mapping[str, Any] | None = None,
    extra: Iterable[Any] = (),
    document_amounts: Iterable[Mapping[str, Any]] = (),
) -> tuple[Allowance, ...]:
    """The scope policy's standard allowance rows plus the extra names, each
    at $0 unless a document names an amount for it (WS6: "nine standard
    rows at $0 unless a document names an amount"). An extra entry that is
    an object with a name and an amount keeps its amount."""
    doc = policy_doc if policy_doc is not None else scope_policy.active_policy().doc
    amounts = [a for a in document_amounts or () if isinstance(a, Mapping)]
    names: list[str] = [str(n) for n in ((doc or {}).get("allowances") or {}).get("standard") or ()]
    given: dict[str, Decimal] = {}
    for entry in extra:
        if isinstance(entry, Mapping):
            clean = str(entry.get("name") or "").strip()
            raw = entry.get("amount_usd", entry.get("amount"))
            try:
                if clean and raw is not None and Decimal(str(raw)) > 0:
                    given[clean] = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                pass
        else:
            clean = str(entry).strip()
        if clean and clean not in names:
            names.append(clean)
    out: list[Allowance] = []
    for name in names:
        amount = given.get(name)
        if amount is None:
            amount = document_allowance_amount(name, amounts)
        out.append(Allowance(name=name, amount_usd=amount if amount is not None else 0))
    return tuple(out)


def _project_wage_type(sb: Any, project_id: str | None) -> str | None:
    if sb is None or not project_id:
        return None
    try:
        rows = sb.table("projects").select("wage_type").eq("id", str(project_id)).limit(1).execute().data or []
    except Exception:  # noqa: BLE001 - a missing project is "unknown wage", never a crash
        return None
    value = rows[0].get("wage_type") if rows else None
    return value if value in ec.WAGE_TYPES else None


def _project_gc_ids(sb: Any, project_id: str | None) -> tuple[str, ...]:
    if sb is None or not project_id:
        return ()
    try:
        rows = sb.table("project_gcs").select("gc_id").eq("project_id", str(project_id)).execute().data or []
    except Exception:  # noqa: BLE001
        return ()
    return tuple(str(r["gc_id"]) for r in rows if r.get("gc_id"))


def pricing_context_from_scope(
    sb: Any,
    run: Mapping[str, Any],
    *,
    policy: PricingPolicy = DEFAULT_POLICY,
    main: Mapping[str, Any] | None = None,
    facts_for_run: ec.FactsForRun = ec.no_facts,
    scope_context_for_run: ScopeContextForRun | None = None,
    exclude_project_ids: Iterable[str] = (),
) -> PricingContext:
    """The 5.3 adapter from WS5 output to the engine's context (module
    docstring, step 2). Wage unknown stays None (the engine blocks with
    wage_type_unknown)."""
    run_id = str(run.get("id") or "")
    scope_ctx: Mapping[str, Any] | None = None
    if scope_context_for_run is not None:
        try:
            got = scope_context_for_run(sb, run_id)
            scope_ctx = got if isinstance(got, Mapping) else None
        except Exception:  # noqa: BLE001 - the scope context is optional context
            logger.exception("pricing_run: scope context failed for %s", run_id)
    conditions = bc.for_run(sb, run_id) if sb is not None else None
    wage: str | None = conditions.wage_type if conditions is not None else None
    if wage is None and scope_ctx is not None:
        raw_bid = scope_ctx.get("bid_conditions")
        if isinstance(raw_bid, Mapping) and raw_bid.get("wage_type") in ec.WAGE_TYPES:
            wage = str(raw_bid["wage_type"])
    if wage is None:
        raw = run.get("bid_conditions")
        if isinstance(raw, Mapping) and raw.get("wage_type") in ec.WAGE_TYPES:
            wage = str(raw["wage_type"])
    if wage is None:
        wage = _project_wage_type(sb, run.get("project_id"))
    rate = policy.labor_rates.get(wage or policy.default_wage_type, policy.labor_rates[policy.default_wage_type])
    main_doc = main if main is not None else (main_output(sb, run_id) if sb is not None else None)
    # Scope decisions with their own citations first, then WS5 facts, then the
    # material notes (a seeded line cites the first matching fact).
    facts = spec_facts_from_scope_context(scope_ctx)
    try:
        facts.extend(spec_facts_from_facts(facts_for_run(sb, run_id)))
    except Exception:  # noqa: BLE001 - facts are optional context
        logger.exception("pricing_run: facts_for_run failed for %s", run_id)
    facts.extend(spec_facts_from_material_notes(main_doc))
    extras = ((main_doc or {}).get("estimate") or {}).get("extra_allowances") or ()
    excluded = set(str(p) for p in exclude_project_ids)
    if run.get("project_id"):
        excluded.add(str(run["project_id"]))
    return PricingContext(
        wage_type=wage,
        labor_rate_usd=rate,
        labor_rate_source="policy",
        rate_overrides=policy.rate_overrides,
        project_gc_ids=_project_gc_ids(sb, run.get("project_id")),
        spec_facts=tuple(facts),
        allowances=standard_allowances(extra=extras, document_amounts=(scope_ctx or {}).get("document_amounts") or ()),
        policy_version=str(policy.version),
        exclude_project_ids=frozenset(excluded),
        eval_case_id=run.get("eval_case_id"),
        tax_rate_pct=policy.tax_rate_pct,
        material_markup_pct=policy.material_markup_pct.get("materials", Decimal("10")),
        labor_markup_pct=policy.labor_markup_pct,
        hour_roundup_steps=policy.hour_roundup_steps,
    )


def excluded_projects_for(run: Mapping[str, Any]) -> frozenset[str]:
    """The library exclusions of a run: the holdout always, the eval case's
    project (leave-one-project-out) and the run's own project."""
    out = set(eval_manifest.excluded_projects(LIBRARY_PURPOSE, run.get("eval_case_id")))
    if run.get("project_id"):
        out.add(str(run["project_id"]))
    return frozenset(out)


def load_library(sb: Any, run: Mapping[str, Any], *, warnings: list[PricingIssue]) -> CostLibrary:
    excluded = excluded_projects_for(run)
    try:
        return CostLibrary.load(sb, exclude_projects=excluded)
    except LibraryUnavailable as exc:
        warnings.append(PricingIssue(code=WARN_LIBRARY_UNAVAILABLE, detail=str(exc)[:200]))
        return CostLibrary((), exclude_projects=excluded)


# Quotes --------------------------------------------------------------------------------------------


def run_quotes(sb: Any, run_id: str) -> list[dict]:
    try:
        return sb.table("vendor_quote_extractions").select("*").eq("run_id", run_id).execute().data or []
    except Exception as exc:  # noqa: BLE001 - before ME
        if not _table_missing(exc):
            logger.exception("vendor_quote_extractions read failed for %s", run_id)
        return []


def parse_pending_quotes(
    sb: Any,
    rows: Sequence[dict],
    *,
    settings: Any = None,
    download: Callable[[str], bytes] | None = None,
    llm_fallback: vq.FallbackCaller | None = None,
    heartbeat: Callable[[], None] = lambda: None,
) -> list[dict]:
    """Parse the quotes that still need it (pending, or needs_review when
    the LLM fallback is on and the text parse never had it). The fallback
    goes only through the injected caller."""
    s = settings or get_settings()
    fallback_on = bool(getattr(s, "quote_lines_llm_fallback_enabled", False))
    out: list[dict] = []
    for row in rows:
        status = row.get("status")
        retry = status == "needs_review" and fallback_on and llm_fallback is not None and row.get("parser") == "text"
        if status not in ("pending",) and not retry:
            out.append(row)
            continue
        if download is None or not row.get("storage_path"):
            out.append(row)
            continue
        heartbeat()
        try:
            data = download(str(row["storage_path"]))
            parsed = vq.parse_quote_pdf(
                data, filename=row.get("filename"), fallback_enabled=fallback_on, llm_fallback=llm_fallback,
            )
            patch = parsed.to_row()
        except Exception as exc:  # noqa: BLE001 - one bad quote never stops the pricing
            logger.exception("vendor quote %s failed to parse", row.get("id"))
            patch = {"status": "failed", "error": str(exc)[:300]}
        patch["updated_at"] = _now_iso()
        try:
            sb.table("vendor_quote_extractions").update(patch).eq("id", row["id"]).execute()
        except Exception:  # noqa: BLE001
            logger.exception("vendor quote %s could not be updated", row.get("id"))
        out.append({**row, **patch})
    return out


def quotes_for_pricing(
    rows: Sequence[Mapping[str, Any]],
    price_rows: Sequence[PriceRow],
    *,
    policy: PricingPolicy,
    run_id: str | None = None,
    output_version: int | None = None,
) -> dict[str, Any]:
    """Reconcile every parsed quote against the BOQ rows of its category
    and pick per category (human selection first, else lowest
    tax-inclusive). Returns refs (chosen quote first in each category),
    chosen, reconciliations (the engine reads the chosen quote's), findings
    and blocked line ids of the chosen quotes, the vendor labels and
    `details` per quote id (reconciled counts, selection reason, chosen)."""
    candidates: list[tuple[QuoteRef, vq.ParsedQuote]] = []
    parsed_by_id: dict[str, vq.ParsedQuote] = {}
    reconciliations: dict[str, vq.Reconciliation] = {}
    labels: dict[str, str] = {}
    human: list[str] = []
    for row in rows:
        if row.get("status") not in ("parsed",):
            continue
        parsed = vq.ParsedQuote.from_row(row)
        category = str(row.get("category") or "Lighting")
        ref = vq.quote_ref(str(row["id"]), parsed, category=category, file_sha256=row.get("file_sha256"))
        candidates.append((ref, parsed))
        parsed_by_id[ref.quote_id] = parsed
        labels[ref.quote_id] = parsed.vendor or str(row.get("filename") or "")
        if row.get("selected"):
            human.append(ref.quote_id)
        reconciliations[ref.quote_id] = vq.reconcile(
            parsed, price_rows, category=category, policy=policy, run_id=run_id, output_version=output_version,
        )
    refs, chosen = vq.select_quotes(candidates, human_selected=human, tax_rate_pct=policy.tax_rate_pct)
    chosen_ids = {q.quote_id for q, _alts in chosen.values()}
    findings: list[ec.GateFinding] = []
    blocked: list[str] = []
    for quote_id, rec in reconciliations.items():
        if quote_id not in chosen_ids:
            continue
        findings.extend(rec.findings)
        blocked.extend(rec.blocked_line_ids)
    details: dict[str, dict[str, Any]] = {}
    for ref in refs:
        is_chosen = ref.quote_id in chosen_ids
        details[ref.quote_id] = {
            "quote_id": ref.quote_id,
            "chosen": is_chosen,
            "selection_reason": selection_reason(ref.quote_id, chosen_ids, human),
            "reconciled": reconciled_summary(reconciliations[ref.quote_id], parsed_by_id[ref.quote_id]),
        }
    refs = sorted(refs, key=lambda r: (r.category, r.quote_id not in chosen_ids))
    return {
        "refs": refs,
        "chosen": chosen,
        "reconciliations": reconciliations,
        "findings": findings,
        "blocked_line_ids": tuple(dict.fromkeys(blocked)),
        "human_selected": human,
        "labels": labels,
        "details": details,
    }


def selection_reason(quote_id: str, chosen_ids: Iterable[str], human: Iterable[str]) -> str:
    """Why a quote prices its category (Q9): the human-selected quote when
    eligible, else the lowest eligible; any other quote is an alternative."""
    if quote_id not in set(chosen_ids):
        return SELECTION_ALTERNATIVE
    return SELECTION_HUMAN if quote_id in set(human) else SELECTION_LOWEST


def reconciled_summary(rec: vq.Reconciliation, parsed: vq.ParsedQuote) -> dict[str, Any]:
    """The reconciled counts of one quote (E9 reads them): per mark the
    quote quantity after EM folding (`<mark>_ft` when the quote prices per
    foot); a mark whose BOQ count disagrees carries the BOQ's folded count
    so the mismatch shows; `collapse_warning` when the parser collapsed
    exact duplicate lines; statuses and the not-in-quote line count."""
    out: dict[str, Any] = {}
    statuses: dict[str, str] = {}
    for m in rec.matches:
        statuses[m.mark] = m.status
        if m.quote_qty is None:
            continue
        if m.quote_uom == "FT":
            out[f"{m.mark}_ft"] = float(m.quote_qty)
        elif m.status == "count_mismatch":
            out[m.mark] = float(m.boq_qty)
        else:
            out[m.mark] = float(m.quote_qty)
    out["collapse_warning"] = any(str(w).startswith("duplicate_lines_collapsed:") for w in parsed.warnings)
    out["em_folded"] = any(m.em_line_ids for m in rec.matches)
    out["statuses"] = statuses
    out["not_in_quote_lines"] = len(rec.not_in_quote_line_ids)
    out["blocked_lines"] = len(rec.blocked_line_ids)
    out["flags"] = list(rec.flags)
    return out


def attach_quote_details(priced: PricedEstimate, quotes: Mapping[str, Any]) -> PricedEstimate:
    """The chosen quote's selection reason and reconciled counts on its
    category entry (stored with the version's `categories`)."""
    details = quotes.get("details") or {}
    for cat, entry in priced.categories.items():
        quote_id = entry.get("quote_id") if isinstance(entry, dict) else None
        if quote_id and quote_id in details:
            entry["quote_selection_reason"] = details[quote_id]["selection_reason"]
            entry["quote_reconciled"] = details[quote_id]["reconciled"]
    return priced


# Gates ------------------------------------------------------------------------------------------------


def resolve_pricing_allowed() -> ec.PricingAllowed:
    """WS4's `plans_reader_gates.pricing_allowed` when the module exists,
    else the contract's typed refusal (never a silent allow)."""
    try:
        from app.services import plans_reader_gates  # type: ignore[attr-defined]
    except ImportError:
        return ec.pricing_allowed_unavailable
    fn = getattr(plans_reader_gates, "pricing_allowed", None)
    return fn if callable(fn) else ec.pricing_allowed_unavailable


def resolve_record_findings() -> ec.RecordFindings:
    try:
        from app.services import plans_reader_gates  # type: ignore[attr-defined]
    except ImportError:
        return ec.null_record_findings
    fn = getattr(plans_reader_gates, "record_findings", None)
    return fn if callable(fn) else ec.null_record_findings


def resolve_review_begin() -> Callable[..., Any] | None:
    try:
        from app.services import plans_reader_review  # type: ignore[attr-defined]
    except ImportError:
        return None
    fn = getattr(plans_reader_review, "begin", None)
    return fn if callable(fn) else None


def engine_blocker_findings(priced: PricedEstimate, *, run_id: str, output_version: int | None) -> list[ec.GateFinding]:
    """One GateFinding per engine blocker whose code is a gate id (the
    integrator's wave 2 wiring note): linear_in_ea per line,
    wage_type_unknown once. pricing_not_allowed is WS4's own finding."""
    out: list[ec.GateFinding] = []
    for blocker in priced.blockers:
        if blocker.code not in ec.GATE_IDS or blocker.code == vq.GATE_UOM_CONFLICT:
            continue  # uom_conflict_quote is recorded by the reconciliation with its own target
        if blocker.line_ids:
            for line_id in blocker.line_ids:
                out.append(ec.GateFinding(
                    run_id=run_id, output_version=output_version, stage="pricing", gate_id=blocker.code,
                    severity="block", route="measure" if blocker.code == "linear_in_ea" else "human",
                    target={"line_id": line_id}, message=blocker.detail,
                ))
        else:
            out.append(ec.GateFinding(
                run_id=run_id, output_version=output_version, stage="pricing", gate_id=blocker.code,
                severity="block", route="human", target={"field": blocker.code}, message=blocker.detail,
            ))
    return out


def apply_quote_blocks(priced: PricedEstimate, blocked_line_ids: Sequence[str], detail: str) -> PricedEstimate:
    """A pricing-stage block sets the line's flag and the version's status
    to blocked; the numbers stay so the verifier sees them."""
    ids = [lid for lid in blocked_line_ids if priced.line(lid) is not None]
    if not ids:
        return priced
    for lid in ids:
        line = priced.line(lid)
        assert line is not None
        flags = list(line.flags)
        for flag in (pe.FLAG_BLOCKED, vq.GATE_UOM_CONFLICT):
            if flag not in flags:
                flags.append(flag)
        line.flags = tuple(flags)
    priced.blockers.append(PricingIssue(code=vq.GATE_UOM_CONFLICT, detail=detail, line_ids=tuple(ids)))
    priced.status = "blocked"
    return priced


# Versions ----------------------------------------------------------------------------------------


def inputs_sha256(
    *, boq_version: int | None, library_fingerprint: str | None, policy_doc: Mapping[str, Any],
    context: Mapping[str, Any], quote_ids: Iterable[str], gate_verdict: Any,
) -> str:
    payload = json.dumps(
        {
            "boq_version": boq_version, "library": library_fingerprint, "policy": policy_doc,
            "context": context, "quotes": sorted(str(q) for q in quote_ids), "gates": gate_verdict,
            "engine": pe.ENGINE_VERSION, "render": pricing_render.RENDER_VERSION,
        },
        sort_keys=True, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def versions(sb: Any, run_id: str) -> list[dict]:
    try:
        rows = sb.table("plans_reader_pricing").select("*").eq("run_id", run_id).execute().data or []
    except Exception as exc:  # noqa: BLE001 - before ME
        if not _table_missing(exc):
            raise
        return []
    return sorted(rows, key=lambda r: int(r.get("version") or 0), reverse=True)


def latest_version(sb: Any, run_id: str) -> dict | None:
    rows = versions(sb, run_id)
    return rows[0] if rows else None


def version_row(sb: Any, pricing_id: str) -> dict | None:
    return _one(sb, "plans_reader_pricing", id=str(pricing_id))


def version_lines(sb: Any, pricing_id: str) -> list[dict]:
    rows = sb.table("plans_reader_priced_lines").select("*").eq("pricing_id", str(pricing_id)).execute().data or []
    return sorted(rows, key=lambda r: ec.parse_row_id(r["line_id"])[1] if ec.is_row_id(r.get("line_id")) else 0)


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) == "23505" or "23505" in text or "duplicate key" in text


def store_version(
    sb: Any,
    run_id: str,
    priced: PricedEstimate,
    instruction: str | None,
    *,
    boq_version: int | None = None,
    context: Mapping[str, Any] | None = None,
    policy_doc: Mapping[str, Any] | None = None,
    inputs: str | None = None,
    user: Any = None,
) -> tuple[dict, bool]:
    """Insert the next version of the run (unique (run_id, version); a
    concurrent insert moves to the next number) with its lines, and flag
    the older versions stale. Idempotent: when the newest non-stale
    version was computed from the same `inputs` hash it is returned
    unchanged and nothing is written. Returns (row, inserted)."""
    existing = versions(sb, run_id)
    if inputs and existing and not existing[0].get("stale") and existing[0].get("inputs_sha256") == inputs:
        return existing[0], False
    next_version = (int(existing[0]["version"]) if existing else 0) + 1
    doc = priced.to_dict()
    base_row = {
        "run_id": run_id,
        "boq_version": boq_version,
        "status": priced.status,
        "policy": dict(policy_doc) if policy_doc is not None else None,
        "policy_version": priced.policy_version,
        "library_fingerprint": priced.library_fingerprint,
        "excluded_project_ids_sha256": priced.excluded_project_ids_sha256,
        "inputs_sha256": inputs,
        "context": dict(context) if context is not None else None,
        "categories": doc["categories"],
        "verify_draft": doc["verify"],
        "workbook_basis": doc["workbook_basis"],
        "range_low": doc["range_low"],
        "range_high": doc["range_high"],
        "quality_summary": doc["quality_summary"],
        "unpriced": doc["unpriced"],
        "blockers": doc["blockers"],
        "warnings": doc["warnings"],
        "stale": False,
        "instruction": instruction,
        "created_by": _uid(user),
    }
    inserted: dict | None = None
    for _attempt in range(VERSION_ATTEMPTS):
        row = _drop_none({**base_row, "version": next_version}, ("boq_version", "created_by", "instruction", "context", "policy", "inputs_sha256"))
        try:
            inserted = sb.table("plans_reader_pricing").insert(row).execute().data[0]
            break
        except Exception as exc:  # noqa: BLE001 - only the version race is expected
            if not _is_unique_violation(exc):
                raise
            next_version += 1
    if inserted is None:
        raise PricingRefused("Another pricing version was being saved; try again.", 409)
    lines = [{"pricing_id": inserted["id"], "line_id": ln.line_id, "line": ln.to_dict()} for ln in priced.lines]
    for i in range(0, len(lines), LINES_PAGE):
        sb.table("plans_reader_priced_lines").insert(lines[i : i + LINES_PAGE]).execute()
    # Re-read: a version that landed concurrently (the race above) goes stale too.
    for old in versions(sb, run_id):
        if str(old["id"]) != str(inserted["id"]) and not old.get("stale"):
            sb.table("plans_reader_pricing").update({"stale": True}).eq("id", old["id"]).execute()
    return inserted, True


def mark_stale(sb: Any, run_id: str, *, except_boq_version: int | None = None) -> int:
    """Flag the run's pricing versions stale (a new BOQ version landed)."""
    n = 0
    for row in versions(sb, run_id):
        if row.get("stale"):
            continue
        if except_boq_version is not None and row.get("boq_version") == except_boq_version:
            continue
        sb.table("plans_reader_pricing").update({"stale": True}).eq("id", row["id"]).execute()
        n += 1
    return n


# The run -------------------------------------------------------------------------------------------


def _workbook_rows(sb: Any, run_id: str, priced: Any) -> tuple[list[Any], list[Any]]:
    """The Basis and Checks sheet rows of the priced workbook
    (plans_reader_basis_rows.rows_for_run over the in-memory estimate) when
    the caller injected none. Tolerant: a failure logs and leaves both
    empty, so the workbook renders without the two sheets."""
    try:
        from app.services import plans_reader_basis_rows

        basis, checks = plans_reader_basis_rows.rows_for_run(sb, run_id, priced=priced)
        return list(basis or []), list(checks or [])
    except Exception:  # noqa: BLE001 - the workbook stands without the Basis and Checks sheets
        logger.exception("pricing: basis and checks rows for run %s failed", run_id)
        return [], []


def price_run(
    sb: Any,
    run_id: str,
    *,
    boq_version: int | None = None,
    user: Any = None,
    instruction: str | None = None,
    settings: Any = None,
    policy: PricingPolicy | None = None,
    library: CostLibrary | None = None,
    pricing_allowed: ec.PricingAllowed | None = None,
    record_findings: ec.RecordFindings | None = None,
    review_begin: Callable[..., Any] | None = None,
    facts_for_run: ec.FactsForRun = ec.no_facts,
    scope_context_for_run: ScopeContextForRun | None = None,
    upload: Callable[[str, bytes, str], None] | None = None,
    download: Callable[[str], bytes] | None = None,
    llm_fallback: vq.FallbackCaller | None = None,
    basis_rows: Sequence[Any] = (),
    checks_rows: Sequence[Any] = (),
    heartbeat: Callable[[], None] = lambda: None,
    now: dt.date | None = None,
) -> dict[str, Any]:
    """Price the run's current BOQ and store a version (module docstring).
    Every I/O beyond the Supabase client is injected (storage upload and
    download, the quote fallback caller, the WS4 and WS7 contracts), so the
    tests run it end to end on a fake client with no network."""
    s = settings or get_settings()
    run = load_run(sb, run_id)
    if run.get("status") not in PRICEABLE_RUN_STATUSES:
        raise PricingRefused("Pricing needs a finished run (quantities complete).", 409)
    boq = latest_boq(sb, run_id, version=boq_version)
    if boq is None or not isinstance(boq.get("spec_json"), Mapping):
        raise PricingRefused("This run has no BOQ deliverable to price.", 409)
    boq_v = int(boq.get("version") or 0) or None
    rows = boq_rows(boq["spec_json"])
    if not rows:
        raise PricingRefused("The BOQ has no priceable lines.", 409)
    heartbeat()
    warnings: list[PricingIssue] = []
    pol, pol_meta = (policy, {"source": "injected", "version": None}) if policy is not None else load_policy(sb)
    if pol_meta.get("source") == "default":
        warnings.append(PricingIssue(code=WARN_SETTINGS_DEFAULT, detail="no ai_pricing_settings row; the code policy priced this version"))
    pol, eval_seeded = eval_seeded_policy(pol, run, eval_root=getattr(s, "eval_root", None) or None)
    if eval_seeded:
        warnings.append(PricingIssue(
            code=WARN_EVAL_SEEDS,
            detail=f"eval case {run.get('eval_case_id')}: grade seed amounts from the case's expected.json (stand-in for the settings row)",
        ))
    main = main_output(sb, run_id)
    ctx = pricing_context_from_scope(
        sb, run, policy=pol, main=main, facts_for_run=facts_for_run, scope_context_for_run=scope_context_for_run,
        exclude_project_ids=eval_manifest.excluded_projects(LIBRARY_PURPOSE, run.get("eval_case_id")),
    )
    lib = library if library is not None else load_library(sb, run, warnings=warnings)
    heartbeat()

    # Quotes: parse and reconcile (the findings are recorded with the engine's below).
    quote_rows = parse_pending_quotes(
        sb, run_quotes(sb, run_id), settings=s, download=download, llm_fallback=llm_fallback, heartbeat=heartbeat,
    )
    main_version = None
    quotes = quotes_for_pricing(quote_rows, rows, policy=pol, run_id=run_id, output_version=main_version)

    # Gates (WS4 verdict through the contract) and the estimate.
    gate = pricing_allowed if pricing_allowed is not None else resolve_pricing_allowed()
    verdict = gate(run)
    allowed = (bool(verdict[0]), [str(r) for r in verdict[1]])
    priced = pe.compute_estimate(
        rows, lib, pol, quotes["refs"], ctx,
        exclude_project=str(run["project_id"]) if run.get("project_id") else None,
        pricing_allowed=allowed, selected_quote_ids=quotes["human_selected"],
        quote_reconciliations=quotes["reconciliations"],
    )
    attach_quote_details(priced, quotes)
    inert = inert_seed_warning(priced, pol, ctx)
    if inert is not None:
        warnings.append(inert)
    if not basis_rows and not checks_rows:
        basis_rows, checks_rows = _workbook_rows(sb, run_id, priced)
    priced.warnings.extend(warnings)
    for quote_id, rec in quotes["reconciliations"].items():
        for w in rec.warnings:
            priced.warnings.append(PricingIssue(code=f"quote:{rec.category}", detail=w, line_ids=()))
        for flag in rec.flags:
            if flag == vq.FLAG_EM_PREMIUM:
                priced.warnings.append(PricingIssue(code=vq.FLAG_EM_PREMIUM, detail=f"quote {quote_id}: EM batteries excluded; EM fixtures folded into base marks", line_ids=()))
    if quotes["blocked_line_ids"]:
        apply_quote_blocks(
            priced, quotes["blocked_line_ids"],
            "the vendor quote prices these lines per foot where the BOQ counts each; measure the run length",
        )
    # One recorder call per pricing pass, always (an empty batch supersedes
    # the findings this pass no longer raises): the quote findings and the
    # engine blockers together, so neither supersedes the other.
    batch = [*quotes["findings"], *engine_blocker_findings(priced, run_id=run_id, output_version=main_version)]
    recorder = record_findings if record_findings is not None else resolve_record_findings()
    recorded = list(recorder(sb, run, batch, "pricing"))
    heartbeat()

    # Store the version (idempotent on the inputs), render, hand to review.
    policy_doc = pol.to_dict()
    ctx_doc = ctx.to_dict()
    inputs = inputs_sha256(
        boq_version=boq_v, library_fingerprint=lib.fingerprint, policy_doc=policy_doc, context=ctx_doc,
        quote_ids=[q.quote_id for q in quotes["refs"]], gate_verdict=allowed,
    )
    row, inserted = store_version(
        sb, run_id, priced, instruction, boq_version=boq_v, context=ctx_doc, policy_doc=policy_doc,
        inputs=inputs, user=user,
    )
    if not inserted:
        return {**row, "inserted": False, "findings": [f.to_row() for f in recorded]}
    version_no = int(row["version"])
    estimate_spec = (main or {}).get("estimate") if isinstance((main or {}).get("estimate"), Mapping) else {}
    project_name = ((run.get("ledger") or {}).get("project") or {}).get("name") or run.get("name") or "Project"
    est = {"project_name": project_name, **dict(estimate_spec or {})}
    storage_path = None
    filename = pricing_render.priced_estimate_filename(str(project_name), version_no)
    try:
        xlsx, _layout = pricing_render.render_priced_estimate_xlsx(
            boq["spec_json"], est, priced, date=now or dt.datetime.now(dt.UTC).date(), policy=pol,
            version=version_no, basis_rows=basis_rows, checks_rows=checks_rows, quote_labels=quotes["labels"],
        )
        if upload is None:
            from app.services import storage

            def upload(path: str, data: bytes, content_type: str) -> None:  # noqa: E306
                storage.upload_file(path, data, content_type)

            storage_path = storage.build_plans_reader_path(run_id, f"pricing/v{version_no}", filename)
        else:
            storage_path = f"plans-reader/{run_id}/pricing/v{version_no}/{filename}"
        upload(storage_path, xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        sb.table("plans_reader_pricing").update({"storage_path": storage_path, "filename": filename}).eq("id", row["id"]).execute()
        row = {**row, "storage_path": storage_path, "filename": filename}
    except Exception:  # noqa: BLE001 - the version stands without its workbook; the verifier can re-render
        logger.exception("pricing_run: workbook render or upload failed for run %s v%s", run_id, version_no)
    heartbeat()
    begin = review_begin if review_begin is not None else resolve_review_begin()
    if begin is not None:
        try:
            begin(sb, run, pricing_version=version_no)
        except Exception:  # noqa: BLE001 - review is downstream; the priced version stands
            logger.exception("pricing_run: review begin failed for run %s v%s", run_id, version_no)
    return {**row, "inserted": True, "findings": [f.to_row() for f in recorded]}


# Status ----------------------------------------------------------------------------------------------

_HEAVY = ("categories", "quality_summary", "unpriced", "context", "policy")


def active_job(sb: Any, run_id: str) -> dict | None:
    try:
        rows = (
            sb.table("llm_jobs").select("id, status, created_at, attempts, last_error")
            .eq("job_type", JOB_TYPE).eq("target_id", str(run_id)).in_("status", list(ACTIVE_JOB_STATUSES))
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return None
    return rows[0] if rows else None


def summary_row(row: Mapping[str, Any] | None, *, include_money: bool = True) -> dict | None:
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k not in _HEAVY}
    if not include_money:
        for key in ("verify_draft", "workbook_basis", "range_low", "range_high", "blockers", "warnings"):
            out.pop(key, None)
    return out


def pricing_status(sb: Any, run_id: str) -> dict[str, Any]:
    latest = latest_version(sb, run_id)
    quotes = run_quotes(sb, run_id)
    return {
        "run_id": str(run_id),
        "latest": summary_row(latest),
        "versions": [summary_row(r) for r in versions(sb, run_id)],
        "job": active_job(sb, run_id),
        "quotes": len(quotes),
    }


# Queue -----------------------------------------------------------------------------------------------


def pricing_enabled(settings: Any = None) -> bool:
    s = settings or get_settings()
    return bool(getattr(s, "plans_reader_enabled", False)) and bool(getattr(s, "ai_pricing_enabled", False))


def enqueue_pricing(sb: Any, run: Mapping[str, Any], *, user: Any = None, settings: Any = None, reason: str = "manual") -> dict:
    """Queue one ai_pricing job for the run (target_id = run id; a second
    request while one is active collapses onto it). 503 when the queue
    does not know the job type yet (the llm_queue hook of wave 2)."""
    if not pricing_enabled(settings):
        raise PricingRefused("AI pricing is switched off.", 404)
    from app.services import llm_queue

    try:
        return llm_queue.enqueue(
            JOB_TYPE, target_id=str(run["id"]), project_id=run.get("project_id"),
            payload={"run_id": str(run["id"]), "reason": reason}, created_by=_uid(user), settings=settings,
        )
    except KeyError as exc:
        raise PricingRefused("The pricing job is not wired into the queue on this server.", 503) from exc


def autotrigger(sb: Any, run_id: str, *, settings: Any = None, reason: str = "auto") -> dict | None:
    """Step 6 of _finish_with_main and the new-BOQ hook: queue the pricing
    job when AI_PRICING_ENABLED and AI_PRICING_AUTORUN. Never raises."""
    s = settings or get_settings()
    if not pricing_enabled(s) or not getattr(s, "ai_pricing_autorun", False):
        return None
    try:
        run = load_run(sb, run_id)
        return enqueue_pricing(sb, run, settings=s, reason=reason)
    except Exception:  # noqa: BLE001 - never block the run's own finish
        logger.exception("pricing_run: autotrigger failed for run %s", run_id)
        return None


def on_new_boq_version(sb: Any, run_id: str, boq_version: int, *, settings: Any = None) -> dict | None:
    """A new BOQ deliverable version of a FINISHED run: older pricing goes
    stale, autorun prices it again. Returns at once (nothing written, no
    job) unless pricing is enabled and the run's status is done or
    done_with_errors: the first render happens while the run is still
    `rendering`, and step 6 of _finish_with_main prices that BOQ."""
    s = settings or get_settings()
    if not pricing_enabled(s):
        return None
    try:
        status = load_run(sb, run_id).get("status")
    except Exception:  # noqa: BLE001 - a missing run is nothing to price
        logger.exception("pricing_run: new BOQ hook could not load run %s", run_id)
        return None
    if status not in PRICEABLE_RUN_STATUSES:
        return None
    try:
        mark_stale(sb, run_id, except_boq_version=int(boq_version))
    except Exception:  # noqa: BLE001
        logger.exception("pricing_run: could not flag stale pricing for run %s", run_id)
    return autotrigger(sb, run_id, settings=s, reason="boq_version")


def resolve_scope_inputs() -> tuple[ec.FactsForRun, ScopeContextForRun | None]:
    """WS5's `scope_engine.facts_for_run` and `context_for_run` when the
    module exists (resolved at call time, like `resolve_pricing_allowed`),
    else no facts and no scope context."""
    try:
        from app.services import scope_engine  # type: ignore[attr-defined]
    except ImportError:
        return ec.no_facts, None
    facts = getattr(scope_engine, "facts_for_run", None)
    context = getattr(scope_engine, "context_for_run", None)
    return (facts if callable(facts) else ec.no_facts), (context if callable(context) else None)


def execute_job(payload: Mapping[str, Any]) -> None:
    """The llm_queue runner: one job per run, lease renewed between steps."""
    from app.core.supabase_client import get_supabase
    from app.services import llm_queue, storage

    s = get_settings()
    if not pricing_enabled(s):
        raise PricingRefused("AI pricing is switched off.", 404)
    run_id = str(payload["run_id"])

    def _heartbeat() -> None:
        if not llm_queue.renew_lease():
            raise RuntimeError("lease lost: another worker owns this pricing job")

    fallback = vq.llm_quote_fallback if getattr(s, "quote_lines_llm_fallback_enabled", False) else None
    facts_for_run, scope_context_for_run = resolve_scope_inputs()
    price_run(
        get_supabase(), run_id, settings=s, download=storage.download_file,
        facts_for_run=facts_for_run, scope_context_for_run=scope_context_for_run,
        llm_fallback=(lambda pdf, pages: fallback(pdf, pages, settings=s)) if fallback else None,
        heartbeat=_heartbeat, instruction=str(payload.get("reason") or "queue"),
    )


def mark_from_queue(target: str, status: str | None, error: str | None) -> None:
    """The queue's marks: a pricing run has no pending domain row (a
    version appears only on success), so a failure is logged and kept on
    the job row's last_error."""
    if status == "failed":
        logger.warning("ai_pricing job for run %s failed: %s", target, error)


def current_status(target: str) -> str | None:
    """`done` once the run has a non-stale version, else `queued` (the AI
    monitor's retry guard reads it)."""
    from app.core.supabase_client import get_supabase

    try:
        latest = latest_version(get_supabase(), str(target))
    except Exception:  # noqa: BLE001
        return None
    if latest is None:
        return "queued"
    return "done" if not latest.get("stale") else "queued"


def error_message(exc: Exception, model_label: str) -> str:
    if isinstance(exc, PricingRefused):
        return str(exc)
    return f"AI pricing failed: {str(exc)[:200]}"


# Replay stage -----------------------------------------------------------------------------------------


def _quote_category_from_name(filename: str) -> str | None:
    up = str(filename or "").upper()
    if "LIGHT" in up:
        return "Lighting"
    if any(w in up for w in ("SWITCHGEAR", "GEAR", "SWBD", "PANEL")):
        return "Switchgear"
    if "TRENCH" in up:
        return "Trenching"
    if "LOW VOLTAGE" in up or "LOW-VOLTAGE" in up:
        return "Low Voltage"
    return None


def backup_quote_files(reference: Mapping[str, Any] | None, backup_dir: Path | str,
                       project_id: str | None) -> list[dict[str, Any]]:
    """The case's vendor quote PDFs in the backup: every file the WS0
    reference lists under `g3_materials` (selected and alternatives, with
    their category), else the project's data/project_files.json rows of
    category `quote` (category from the file name). Each entry: quote_id,
    category, filename, storage_path, sha256."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mat in (reference or {}).get("g3_materials") or ():
        if not isinstance(mat, Mapping):
            continue
        for quote in [mat.get("selected"), *(mat.get("alternatives") or ())]:
            f = (quote or {}).get("file") if isinstance(quote, Mapping) else None
            if not isinstance(f, Mapping) or not f.get("storage_path") or str(f["storage_path"]) in seen:
                continue
            seen.add(str(f["storage_path"]))
            out.append({"quote_id": str(quote.get("quote_id") or f.get("sha256") or f["storage_path"]),
                        "category": str(mat.get("category") or "Lighting"), "filename": f.get("filename"),
                        "storage_path": str(f["storage_path"]), "sha256": f.get("sha256")})
    if out or not project_id:
        return out
    path = Path(backup_dir) / "data" / "project_files.json"
    if not path.is_file():
        return out
    rows = json.loads(path.read_text())
    rows = rows.get("rows", []) if isinstance(rows, Mapping) else rows
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("project_id")) != str(project_id) or row.get("category") != "quote":
            continue
        name = str(row.get("filename") or "")
        category = _quote_category_from_name(name)
        if not name.lower().endswith(".pdf") or category is None or not row.get("storage_path"):
            continue
        out.append({"quote_id": str(row.get("id")), "category": category, "filename": name,
                    "storage_path": str(row["storage_path"]), "sha256": row.get("sha256")})
    return out


def backup_quote_rows(ctx: Any, project_id: str | None, *, selected: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The case's quote PDFs parsed by code (`vendor_quotes.parse_quote_pdf`,
    no LLM fallback at $0) as vendor_quote_extractions-shaped rows, ready for
    `quotes_for_pricing`. `selected` names quote ids or file names a human
    selected (none by default: the replay prices the lowest eligible, Q9)."""
    picked = {str(s) for s in selected}
    backup_dir = Path(getattr(ctx, "backup_dir", None) or ".")
    rows: list[dict[str, Any]] = []
    for f in backup_quote_files(getattr(ctx, "reference", None), backup_dir, project_id):
        path = backup_dir / "storage" / "project-files" / f["storage_path"]
        if not path.is_file():
            ctx.note(f"price: quote file {f.get('filename')} not in the backup")
            continue
        data = path.read_bytes()
        parsed = vq.parse_quote_pdf(data, filename=f.get("filename"))
        rows.append({
            "id": f["quote_id"], "category": f["category"], "filename": f.get("filename"),
            "file_sha256": f.get("sha256") or vq.sha256_bytes(data),
            "selected": f["quote_id"] in picked or str(f.get("filename")) in picked,
            **parsed.to_row(),
        })
    return rows


def _seeded_from_expected(policy: PricingPolicy, expected: Mapping[str, Any] | None) -> PricingPolicy:
    """Inert grade seeds take their amounts from the case's expected.json
    `seeds.*` (EVAL_ROOT; the stand-in for the settings row at $0, as in
    scripts/pricing_eval.py). A seed still applies only with a cited fact."""
    seeds = (expected or {}).get("seeds") if isinstance(expected, Mapping) else None
    if not isinstance(seeds, Mapping):
        return policy
    out = []
    for seed in policy.grade_seeds:
        if seed.active:
            out.append(seed)
            continue
        key = "gfci_hospital_grade_usd" if any("GFCI" in k for k in seed.item_keys) else "duplex_hospital_grade_usd"
        amount = seeds.get(key)
        out.append(seed.with_amount(amount) if amount is not None else seed)
    return policy.replace(grade_seeds=tuple(out))


def _merge_pricing_findings(gate: Any, findings: Sequence[ec.GateFinding]) -> dict[str, Any] | None:
    """The gate report with the pricing-stage findings appended (the gate
    report carries them, spec 4.1); a check already present (same code and
    rows) is not repeated. None when there is nothing to add."""
    if not findings:
        return None
    base = dict(gate) if isinstance(gate, Mapping) else {"schema": ec.GATE_REPORT_SCHEMA, "blocking": False, "checks": []}
    checks = [c for c in base.get("checks") or [] if isinstance(c, Mapping)]
    have = {(str(c.get("code")), tuple(c.get("rows") or ())) for c in checks}
    extra = ec.gate_report_from_findings(findings)
    for check in extra["checks"]:
        key = (str(check.get("code")), tuple(check.get("rows") or ()))
        if key not in have:
            have.add(key)
            checks.append(check)
    base["checks"] = checks
    base["blocking"] = bool(base.get("blocking")) or bool(extra["blocking"])
    return base


def replay_price(ctx: Any, art: Mapping[str, Any]) -> dict[str, Any]:
    """The $0 `price` stage of plans_reader_replay: price the bundle's BOQ
    spec with a library built from the backup files (leave-one-project-out
    through eval_manifest), the code policy (inert grade seeds take the
    case's expected.json amounts) and the WS5 scope context when the derive
    stage stored one (`scope_context`: wage type from its bid conditions,
    cited device facts, facts and document amounts; else the reference's
    wage). Quotes: `art["quotes"]` (QuoteRef dicts) when stored, else the
    case's backup quote PDFs parsed by code and reconciled against the BOQ;
    the reconciliation drives the engine, its `uom_conflict_quote` findings
    block the lines (pricing findings, merged into the gate report) and each
    artifact quote carries `reconciled` and `selection_reason`. Writes
    `priced_estimate` (bdr.priced_estimate/1), `pricing_summary`,
    `pricing_findings` and, with findings, `gate_report`."""
    boq_spec = art.get("boq_spec")
    if not isinstance(boq_spec, Mapping):
        ctx.note("price: no BOQ spec to price")
        return {}
    rows = boq_rows(boq_spec)
    if not rows:
        ctx.note("price: the BOQ has no priceable lines")
        return {}
    excluded = eval_manifest.excluded_projects(LIBRARY_PURPOSE, getattr(ctx, "case_id", None))
    run = getattr(ctx.bundle, "run", None) or {}
    reference = getattr(ctx, "reference", None) or {}
    project_id = str(run.get("project_id") or reference.get("project_id") or "") or None
    run_id = str(run.get("id") or "") or None
    try:
        library = CostLibrary.from_backup(ctx.backup_dir, exclude_projects=set(excluded) | ({project_id} if project_id else set()))
    except Exception as exc:  # noqa: BLE001 - no backup: an empty library, reported
        ctx.note(f"price: library unavailable ({str(exc)[:80]}); priced with an empty library")
        library = CostLibrary((), exclude_projects=excluded)
    overrides = getattr(ctx, "settings_overrides", None) or {}
    policy = _seeded_from_expected(PricingPolicy.from_dict(overrides.get("pricing_policy")), getattr(ctx, "expected", None))
    scope_ctx = art.get("scope_context") if isinstance(art.get("scope_context"), Mapping) else None
    bid = (scope_ctx or {}).get("bid_conditions") if isinstance((scope_ctx or {}).get("bid_conditions"), Mapping) else {}
    wage = bid.get("wage_type") if bid.get("wage_type") in ec.WAGE_TYPES else None
    wage_source = "scope_context" if wage else None
    if wage is None:
        wage = (reference.get("conditions") or {}).get("wage_type")
        wage = wage if wage in ec.WAGE_TYPES else None
        wage_source = "reference" if wage else None
    main = art.get("main") if isinstance(art.get("main"), Mapping) else None
    facts = spec_facts_from_scope_context(scope_ctx)
    scope_facts: list[ec.Fact] = []
    for raw in (scope_ctx or {}).get("facts") or ():
        try:
            scope_facts.append(ec.Fact.from_dict(raw) if isinstance(raw, Mapping) else raw)
        except Exception:  # noqa: BLE001 - a malformed stored fact is skipped, not fabricated
            continue
    facts.extend(spec_facts_from_facts(scope_facts))
    facts.extend(spec_facts_from_material_notes(main))
    extras = ((main or {}).get("estimate") or {}).get("extra_allowances") or ()
    context = PricingContext(
        wage_type=wage, labor_rate_usd=policy.labor_rates.get(wage or policy.default_wage_type, Decimal(0)),
        labor_rate_source="policy", rate_overrides=policy.rate_overrides, spec_facts=tuple(facts),
        allowances=standard_allowances(extra=extras, document_amounts=(scope_ctx or {}).get("document_amounts") or ()),
        policy_version=str(policy.version),
        exclude_project_ids=frozenset(library.excluded_projects), eval_case_id=getattr(ctx, "case_id", None),
        tax_rate_pct=policy.tax_rate_pct, labor_markup_pct=policy.labor_markup_pct,
        hour_roundup_steps=policy.hour_roundup_steps,
    )
    stored_quotes = [QuoteRef.from_dict(q) for q in (art.get("quotes") or ()) if isinstance(q, Mapping)]
    if stored_quotes:
        quotes: dict[str, Any] = {"refs": stored_quotes, "reconciliations": {}, "findings": [], "blocked_line_ids": (),
                                  "human_selected": [], "details": {}, "labels": {}}
    else:
        quote_rows = backup_quote_rows(ctx, project_id, selected=overrides.get("pricing_selected_quotes") or ())
        quotes = quotes_for_pricing(quote_rows, rows, policy=policy, run_id=run_id)
    gate = art.get("gate_report")
    allowed = (not bool(gate.get("blocking")), [c["code"] for c in gate.get("checks", ()) if c.get("severity") == "block"]) if isinstance(gate, Mapping) else None
    priced = pe.compute_estimate(
        rows, library, policy, quotes["refs"], context, exclude_project=project_id, pricing_allowed=allowed,
        selected_quote_ids=quotes["human_selected"], quote_reconciliations=quotes["reconciliations"],
    )
    attach_quote_details(priced, quotes)
    for quote_id, rec in quotes["reconciliations"].items():
        if quotes["details"].get(quote_id, {}).get("chosen"):
            for w in rec.warnings:
                priced.warnings.append(PricingIssue(code=f"quote:{rec.category}", detail=w, line_ids=()))
    if quotes["blocked_line_ids"]:
        apply_quote_blocks(
            priced, quotes["blocked_line_ids"],
            "the vendor quote prices these lines per foot where the BOQ counts each; measure the run length",
        )
    findings = [*quotes["findings"], *engine_blocker_findings(priced, run_id=run_id, output_version=None)]
    artifact = pe.to_priced_estimate_v1(
        priced, run_id=run_id, wage_type=wage, quotes=quotes["refs"], allowances=context.allowances,
        quote_details=quotes["details"],
    )
    ctx.note(
        f"price: {len(priced.lines)} lines, status {priced.status}, library {library.n_projects} projects, "
        f"{len(quotes['refs'])} quotes, {len(findings)} pricing findings, wage from {wage_source or 'nowhere'}, "
        f"{len(facts)} spec facts"
    )
    out: dict[str, Any] = {
        "priced_estimate": artifact,
        "pricing_summary": {
            "status": priced.status, "lines": len(priced.lines), "library_projects": library.n_projects,
            "library_fingerprint": library.fingerprint, "unpriced": list(priced.unpriced.lines),
            "blockers": [b.code for b in priced.blockers], "methods": (priced.quality_summary or {}).get("methods"),
            "quotes": [{"quote_id": q.quote_id, "category": q.category, **{k: v for k, v in quotes["details"].get(q.quote_id, {}).items() if k != "quote_id"}}
                       for q in quotes["refs"]],
            "wage_source": wage_source,
            "spec_facts": [f.to_dict() for f in facts],
            "seed_lines": sum(1 for ln in priced.lines if pe.FLAG_SEED in ln.flags),
            "not_in_quote_lines": sum(1 for ln in priced.lines if pe.FLAG_NOT_IN_QUOTE in ln.flags),
        },
        "pricing_findings": [f.to_row() for f in findings],
    }
    merged = _merge_pricing_findings(gate, findings)
    if merged is not None:
        out["gate_report"] = merged
    return out


def register_replay_stage() -> None:
    from app.services import plans_reader_replay

    plans_reader_replay.register_stage(
        "price", replay_price, version=f"ws6-engine-{pe.ENGINE_VERSION}",
        reads=("boq_spec", "main", "quotes", "gate_report", "scope_context"),
        writes=("priced_estimate", "pricing_summary", "pricing_findings", "gate_report"), owner="WS6",
    )


register_replay_stage()


__all__ = [
    "MODEL_LABEL", "PRICING_FEATURE", "JOB_TYPE", "PricingRefused", "load_policy", "save_policy", "boq_rows",
    "latest_boq", "pricing_context_from_scope", "excluded_projects_for", "load_library", "run_quotes",
    "parse_pending_quotes", "quotes_for_pricing", "resolve_pricing_allowed", "engine_blocker_findings",
    "apply_quote_blocks", "inputs_sha256", "versions", "latest_version", "version_row", "version_lines",
    "store_version", "mark_stale", "price_run", "pricing_status", "active_job", "summary_row",
    "pricing_enabled", "enqueue_pricing", "autotrigger", "on_new_boq_version", "execute_job",
    "mark_from_queue", "current_status", "error_message", "replay_price", "register_replay_stage",
    "spec_facts_from_scope_context", "document_allowance_amount", "standard_allowances", "selection_reason",
    "reconciled_summary", "attach_quote_details", "resolve_scope_inputs", "backup_quote_files", "backup_quote_rows",
]
