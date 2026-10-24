"""The `plans_reader_facts` queue job (WS1 step 9; spec 4.1): one job per
run document, non-LLM, claimed in the interactive tier of the AI Estimating
pass, cached by (sha256, kind, extractor_version).

What one job does (`execute(payload)`, `run_document` is the testable core):

1. marks the document `running` and renews the queue lease;
2. downloads the stored PDF and takes its sha256;
3. cache lookup in `plans_reader_doc_facts`: a hit loads the cached facts,
   a miss runs `extract.extract_document` (every PDFium touch under
   `pdf_split._PDFIUM_LOCK` per page), writes one word blob per page to
   storage, the doc row and the page rows (renewing the lease every
   `gate.LEASE_RENEW_EVERY_PAGES` pages);
4. recomputes the category check against THIS document's label (the cache
   is shared by documents with different labels) and applies the relabel
   when `plans_reader_facts_relabel` is on: `category` becomes the detected
   drawing category, `category_label` keeps the label the user gave, and
   `facts_doc.reduced_reading` tells the planner to read a non-electrical
   set only on its schedule, legend and cross-trade pages (E24);
5. writes `sha256`, `facts_status = done`, `facts_doc` on the document;
6. ENDS by calling `plans_reader_runs.advance(sb, run_id)` for every run in
   `splitting` that holds the document (try/except, logged), so the run
   leaves the gate the moment its last document settles.

A failure marks the document `failed` with a short sentence and still
advances the waiting runs: facts are an enhancement and a run never waits
on a failed document. A lost lease (`renew_lease` False) aborts without
writing, since another worker owns the job. With the master flag off the
job marks the document `skipped`.

Only `llm_queue`'s job adapter calls `execute`, `mark_from_queue` and
`current_status`; nothing here calls a model.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from app.services.pagefacts import api
from app.services.pagefacts import category as category_mod
from app.services.pagefacts import extract as extract_mod
from app.services.pagefacts import gate
from app.services.pagefacts import store as store_mod
from app.services.pagefacts.contracts import EXTRACTOR_VERSION, CategoryCheck, DocFacts, PageWords
from app.services.plans_reader_schemas import CATEGORIES

logger = logging.getLogger(__name__)

JOB_TYPE = gate.JOB_PLANS_FACTS
MODEL_LABEL = "pagefacts"
FEATURE = "plans_facts"
BLOB_CONTENT_TYPE = "application/gzip"
ERROR_MAX_CHARS = 500
PROBLEMS_MAX = 20
SPLITTER_PAGES_MAX = 200
DRAWING_SUFFIX = "_drawings"
ELECTRICAL = "electrical_drawings"
MSG_FAILED = "The page facts of this document could not be extracted; the run reads it without them."
MSG_SKIPPED = "Page facts are switched off on this server."


class FactsLeaseLost(RuntimeError):
    """The queue lease was lost mid-job: another worker owns it now."""

    llm_queue_lease_lost = True


def error_message(exc: Exception, _label: str) -> str:
    """The sentence the queue stores for a failure (never provider text)."""
    if isinstance(exc, FactsLeaseLost):
        return "The page facts job lost its lease and was handed to another worker."
    return MSG_FAILED


# Queue adapter -----------------------------------------------------------------------


def execute(payload: dict[str, Any]) -> None:
    """The queue runner: one document per job (payload from
    `gate.facts_job_payload`)."""
    from app.core.supabase_client import get_supabase

    document_id = str(payload.get("document_id") or "")
    if not document_id:
        raise ValueError("The page facts job needs a document_id.")
    run_document(get_supabase(), document_id)


def mark_from_queue(target_id: str, fields: dict[str, Any]) -> None:
    """The queue's domain marks: `pending` (a retry is scheduled) and
    `failed` (terminal, which also releases every waiting run)."""
    from app.core.supabase_client import get_supabase

    sb = get_supabase()
    status = fields.get("status")
    if status == "pending":
        _patch(sb, target_id, {"facts_status": "pending"})
        return
    if status == "failed":
        _patch(sb, target_id, gate.status_after_job(False, error=str(fields.get("error") or MSG_FAILED)))
        try:
            doc = api.document_row(sb, target_id)
        except Exception:  # noqa: BLE001 - the document may be gone with its run
            return
        advance_waiting_runs(sb, doc)


def current_status(target_id: str) -> str | None:
    """The AI monitor's retry guard: every settled state reads as done."""
    from app.core.supabase_client import get_supabase

    rows = (
        get_supabase().table(api.DOCUMENTS_TABLE).select("*").eq("id", str(target_id)).limit(1).execute().data
        or []
    )
    if not rows:
        return None
    status = rows[0].get("facts_status")
    if status in gate.SETTLED_STATUSES:
        return "done"
    return status or "pending"


# The core ----------------------------------------------------------------------------------


def _patch(sb: Any, document_id: str, patch: dict[str, Any]) -> None:
    sb.table(api.DOCUMENTS_TABLE).update(patch).eq("id", str(document_id)).execute()


def _renew(renew: Callable[[], bool]) -> None:
    if not renew():
        raise FactsLeaseLost("lease lost")


def _splitter_titles(doc: dict[str, Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    meta = doc.get("page_meta") if isinstance(doc.get("page_meta"), list) else []
    for i, entry in enumerate(meta, start=1):
        if not isinstance(entry, dict) or entry.get("prepended") or entry.get("derived"):
            continue
        title = entry.get("title")
        if title:
            try:
                out[int(entry.get("pdf_page") or i)] = str(title)
            except (TypeError, ValueError):
                continue
    return out


def _capturing_hooks(words: dict[int, PageWords]) -> extract_mod.ExtractHooks:
    """The production hooks with the facts-block hook wrapped to keep each
    page's words, so the blobs are written from the same extraction."""
    hooks = extract_mod.part_b_hooks()
    inner = hooks.facts_block_tokens

    def capture(ctx: extract_mod.PageContext, doc: extract_mod.DocContext) -> int:
        words[ctx.page] = ctx.words
        return int(inner(ctx, doc)) if inner is not None else 0

    hooks.facts_block_tokens = capture
    return hooks


def _cache_lookup(sb: Any, key: store_mod.CacheKey) -> DocFacts | None:
    try:
        return api.cached_doc_facts(sb, key)
    except Exception:  # noqa: BLE001 - FactsUnavailable or a corrupt row: treat as a miss
        return None


def _write_cache(sb: Any, facts: DocFacts, words: dict[int, PageWords], *, renew: Callable[[], bool], upload: Callable[[str, bytes], None]) -> None:
    doc_row, page_rows = store_mod.facts_to_rows(facts)
    for n, (page, pw) in enumerate(sorted(words.items()), start=1):
        upload(store_mod.blob_path(facts.sha256, page, facts.extractor_version), store_mod.encode_blob(pw))
        if n % gate.LEASE_RENEW_EVERY_PAGES == 0:
            _renew(renew)
    key = {"sha256": facts.sha256, "kind": facts.kind, "extractor_version": facts.extractor_version}
    existing = (
        sb.table(api.DOC_TABLE)
        .select("sha256")
        .eq("sha256", key["sha256"])
        .eq("kind", key["kind"])
        .eq("extractor_version", key["extractor_version"])
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        sb.table(api.DOC_TABLE).update({k: v for k, v in doc_row.items() if k not in key}).eq("sha256", key["sha256"]).eq("kind", key["kind"]).eq("extractor_version", key["extractor_version"]).execute()
        sb.table(api.PAGE_TABLE).delete().eq("sha256", key["sha256"]).eq("kind", key["kind"]).eq("extractor_version", key["extractor_version"]).execute()
    else:
        sb.table(api.DOC_TABLE).insert(doc_row).execute()
    if page_rows:
        sb.table(api.PAGE_TABLE).insert(page_rows).execute()


def category_check_for(facts: DocFacts, label: str | None, name: str | None) -> CategoryCheck | None:
    """The category check against a given label (a cache hit was computed
    against the first run's label)."""
    if facts.kind != "drawing":
        return None
    pairs: list[tuple[str | None, str]] = []
    for p in facts.pages:
        if p.sheet_number:
            pairs.append((p.discipline, p.discipline_category or category_mod.UNKNOWN))
    return category_mod.category_check(label, pairs, name=name)


def relabel_decision(doc: dict[str, Any], check: CategoryCheck | None, *, enabled: bool) -> dict[str, Any]:
    """{applied, from, to, share, reduced_reading, reason}: E24's relabel
    of a drawing set whose numbered sheets belong to another discipline."""
    current = str(doc.get("category") or "")
    out: dict[str, Any] = {"applied": False, "from": current, "to": None, "share": None, "reduced_reading": False, "reason": None}
    if check is None:
        out["reason"] = "not a drawing document"
        return out
    out["share"] = check.share
    if check.action != "relabel":
        out["reason"] = f"category check action {check.action}"
        return out
    detected = check.detected
    if detected not in CATEGORIES or not str(detected).endswith(DRAWING_SUFFIX):
        out["reason"] = f"detected category {detected!r} is not a drawing category"
        return out
    if detected == current:
        out["reason"] = "already that category"
        return out
    out["to"] = detected
    if not enabled:
        out["reason"] = "plans_reader_facts_relabel is off (proposal recorded, not applied)"
        return out
    out["applied"] = True
    out["reduced_reading"] = detected != ELECTRICAL
    out["reason"] = check.message
    return out


def _facts_doc(facts: DocFacts, *, cache: str, check: CategoryCheck | None, relabel: dict[str, Any]) -> dict[str, Any]:
    spec = facts.spec if isinstance(facts.spec, dict) else None
    sel = (spec or {}).get("selection") or {}
    # Splitter-title fallbacks (spec 5.6 MB): pages whose title came from
    # the splitter, not the text layer. The facts are cached per sha256, so
    # on a cache hit these are the titles of the run that extracted them.
    splitter_pages = [p.page for p in facts.pages if p.title_source == "splitter"]
    out: dict[str, Any] = {
        "extractor_version": facts.extractor_version,
        "sha256": facts.sha256,
        "kind": facts.kind,
        "cache": cache,
        "pages": facts.page_count,
        "pages_with_sheet_number": sum(1 for p in facts.pages if p.sheet_number),
        "splitter_title_count": len(splitter_pages),
        "splitter_title_pages": splitter_pages[:SPLITTER_PAGES_MAX],
        "text_classes": dict((facts.stats or {}).get("text_classes") or {}),
        "category_check": check.to_dict() if check is not None else None,
        "relabel": relabel,
        "reduced_reading": bool(relabel.get("reduced_reading")),
        "spec": (
            {
                "pages": list(sel.get("pages") or []),
                "pages_share": sel.get("pages_share"),
                "est_tokens_before": sel.get("est_tokens_before"),
                "est_tokens_after": sel.get("est_tokens_after"),
                "snippets": len(sel.get("tier_b_snippets") or []),
                "flags": list(sel.get("flags") or []),
                "policy": sel.get("policy"),
                "has_div": dict(spec.get("has_div") or {}),
                "mapped_page_share": spec.get("mapped_page_share"),
            }
            if spec
            else None
        ),
        "cross_trade_pages": sorted({int(m["page"]) for m in facts.cross_trade if m.get("page") is not None}),
        "legend_entries": len((facts.legend or {}).get("entries") or []),
        "panels": [p.get("name") for p in facts.panels],
        "fixture_types": [f.get("tag") for f in facts.fixture_types],
        "stats": dict(facts.stats or {}),
        "problems": list(facts.problems or [])[:PROBLEMS_MAX],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return out


def run_document(
    sb: Any,
    document_id: str,
    *,
    settings: Any = None,
    pdf_bytes: bytes | None = None,
    renew: Callable[[], bool] | None = None,
    extract: Callable[..., DocFacts] | None = None,
    upload: Callable[[str, bytes], None] | None = None,
    advance: Callable[[Any, str], None] | None = None,
) -> dict[str, Any]:
    """Extract (or load from the cache), relabel, write the document state
    and advance the waiting runs. Returns a small summary
    {status, cache, relabel, advanced}. Every collaborator is injectable
    for tests; the defaults are the real queue, storage and extractor."""
    from app.core.config import get_settings
    from app.services import llm_queue, storage

    s = settings if settings is not None else get_settings()
    renew = renew or llm_queue.renew_lease
    extract = extract or extract_mod.extract_document
    upload = upload or (lambda path, data: storage.upload_file(path, data, BLOB_CONTENT_TYPE, upsert=True))
    doc = api.document_row(sb, document_id)
    summary: dict[str, Any] = {"status": None, "cache": None, "relabel": None, "advanced": []}
    if not getattr(s, "plans_reader_page_facts_enabled", False):
        _patch(sb, document_id, {"facts_status": "skipped", "facts_error": MSG_SKIPPED})
        summary["status"] = "skipped"
        summary["advanced"] = advance_waiting_runs(sb, doc, advance=advance)
        return summary
    _renew(renew)
    _patch(sb, document_id, {"facts_status": "running"})
    sha: str | None = None
    try:
        data = pdf_bytes if pdf_bytes is not None else storage.download_file(str(doc.get("storage_path") or ""))
        sha = extract_mod.sha256_of(data)
        label = doc.get("category_label") or doc.get("category")
        kind = extract_mod.kind_for_label(label)
        key = store_mod.cache_key(sha, kind, EXTRACTOR_VERSION)
        facts = _cache_lookup(sb, key)
        cache = "hit"
        if facts is None:
            cache = "miss"
            words: dict[int, PageWords] = {}
            facts = extract(data, label=label, name=doc.get("name"), kind=kind, splitter_titles=_splitter_titles(doc), hooks=_capturing_hooks(words))
            _renew(renew)
            _write_cache(sb, facts, words, renew=renew, upload=upload)
            api.clear_caches()
        _renew(renew)
        check = category_check_for(facts, str(doc.get("category") or ""), doc.get("name"))
        relabel = relabel_decision(doc, check, enabled=bool(getattr(s, "plans_reader_facts_relabel", False)))
        patch: dict[str, Any] = {
            "sha256": sha,
            "facts_status": "done",
            "facts_error": None,
            "facts_doc": _facts_doc(facts, cache=cache, check=check, relabel=relabel),
        }
        if not doc.get("category_label"):
            patch["category_label"] = doc.get("category")
        if relabel["applied"]:
            patch["category"] = relabel["to"]
        _patch(sb, document_id, patch)
        summary.update({"status": "done", "cache": cache, "relabel": relabel})
    except FactsLeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed document never blocks its run
        logger.exception("pagefacts: facts job for document %s failed", document_id)
        message = f"{type(exc).__name__}: {exc}"[:ERROR_MAX_CHARS]
        patch = gate.status_after_job(False, error=message)
        if sha:
            patch["sha256"] = sha
        _patch(sb, document_id, patch)
        summary["status"] = "failed"
        summary["error"] = message
    summary["advanced"] = advance_waiting_runs(sb, {**doc, "sha256": sha or doc.get("sha256")}, advance=advance)
    return summary


def waiting_run_ids(sb: Any, doc: dict[str, Any]) -> list[str]:
    """Runs in `splitting` that hold this document (its own run and the
    runs of the other documents with the same sha256)."""
    run_ids: set[str] = set()
    if doc.get("run_id"):
        run_ids.add(str(doc["run_id"]))
    sha = doc.get("sha256")
    if sha:
        try:
            rows = sb.table(api.DOCUMENTS_TABLE).select("run_id").eq("sha256", str(sha)).execute().data or []
            run_ids.update(str(r.get("run_id")) for r in rows if r.get("run_id"))
        except Exception:  # noqa: BLE001 - the own run is still advanced
            logger.exception("pagefacts: sibling document lookup for %s failed", doc.get("id"))
    if not run_ids:
        return []
    runs = sb.table("plans_reader_runs").select("id, status").in_("id", sorted(run_ids)).execute().data or []
    return [str(r["id"]) for r in runs if r.get("status") == "splitting"]


def advance_waiting_runs(sb: Any, doc: dict[str, Any], *, advance: Callable[[Any, str], None] | None = None) -> list[str]:
    """Call `plans_reader_runs.advance` for every waiting run; each call is
    guarded so one failure never stops the others or the job."""
    if advance is None:
        from app.services import plans_reader_runs

        advance = plans_reader_runs.advance
    advanced: list[str] = []
    try:
        run_ids = waiting_run_ids(sb, doc)
    except Exception:  # noqa: BLE001
        logger.exception("pagefacts: waiting run lookup for document %s failed", doc.get("id"))
        return advanced
    for run_id in run_ids:
        try:
            advance(sb, run_id)
            advanced.append(run_id)
        except Exception:  # noqa: BLE001 - advance is idempotent and re-runs on the next poll
            logger.exception("pagefacts: advancing run %s after the facts job of %s failed", run_id, doc.get("id"))
    return advanced


__all__ = [
    "JOB_TYPE",
    "MODEL_LABEL",
    "FEATURE",
    "FactsLeaseLost",
    "error_message",
    "execute",
    "mark_from_queue",
    "current_status",
    "category_check_for",
    "relabel_decision",
    "run_document",
    "waiting_run_ids",
    "advance_waiting_runs",
]
