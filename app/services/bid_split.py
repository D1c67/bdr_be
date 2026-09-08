"""Bid File Splitter pipeline - triage a bid-set PDF, then split it if it
is a drawing set.

One `execute(file_id)` run owns one uploaded source PDF (bid_split_files row):

  1. TRIAGE: render a small sample of pages (BID_SPLIT_TRIAGE_PAGES spread
     through the file) and ask the vision model what the FILE is: a drawing
     set, a specifications book, an RFP, an addendum, a mixed package, or
     something else. Specifications, RFP and addendum files STOP HERE: they
     are labeled and left intact (one segment row pointing at the untouched
     source object; the file is never re-written). Figures printed inside a
     spec book reference the drawings; they must never be carved out of it.
  2. Drawing sets and mixed packages (and any low-confidence verdict) go to
     the per-page pass: render every page in batches of
     BID_SPLIT_PAGES_PER_CALL and classify each (trade or document category +
     title block text + confidence) via llm.complete_images_json - per-page
     classification rather than model-proposed ranges, so batch boundaries can
     never split or hide a document boundary.
  3. ISLAND REPAIR: a short run of pages (<= BID_SPLIT_ISLAND_MAX_PAGES)
     sandwiched between two runs of one same category is assumed
     misclassified and absorbed - that is how a figure inside a spec section
     stays in the spec section, and a schedule sheet inside an electrical run
     stays electrical.
  4. Merge contiguous same-category runs into segments in code (the page
     ranges are therefore guaranteed to cover the document exactly once).
  5. One cheap text call names and describes each segment (falls back to
     generated names - a naming hiccup must not fail a finished split).
  6. Cut the source along the ranges (pdf_split.extract_range), upload each
     output PDF, and insert bid_split_segments rows. A segment spanning the
     whole file is never cut or copied: its row points at the source object
     with is_original=true. When the file holds General / Cover Sheets pages,
     those pages are ALSO prepended to every trade segment cut from the same
     file (pdf_split.extract_pages): the cover sheet, drawing index, symbols
     and general notes are context a reader needs open next to any trade's
     sheets. The general run still comes out as its own segment.

Dispatch mirrors the other AI jobs: the llm_queue runs `execute` (raises on
failure; the queue classifies, retries transients, and marks the domain row),
and `run_file` is the BackgroundTasks fallback that does its own marking.
Interrupted attempts are safe to re-run: execute wipes any segments a previous
attempt left behind before producing its own.

Every run also freezes its exact model input references (input_snapshot:
prompts, sampled/batched page numbers, render settings) and the pristine model
output (model_output: validated triage verdict + per-page classifications
BEFORE island repair) onto the file row - the raw material the training
capture (services/bid_split_training.py) pairs with user corrections.
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import datetime, timezone

import httpx

from app.core.config import Settings, get_settings
from app.core.supabase_client import get_supabase
from app.services import llm, llm_errors, notifications, pdf_split, storage
from app.services.llm import LlmBadOutput

logger = logging.getLogger(__name__)

_ERROR_MAX_CHARS = 500

# Trade buckets a drawing set splits into. 'other' (with a free-text
# other_type label) is the open end: an unusual discipline (marine/underwater
# work, landscape, ...) still comes out as its own named segment.
TRADE_CATEGORIES = (
    "general_drawings",
    "civil_drawings",
    "structural_drawings",
    "architectural_drawings",
    "mechanical_drawings",
    "plumbing_drawings",
    "electrical_drawings",
    "fire_protection_drawings",
    "low_voltage_drawings",
)

# Document-level categories: these are identified, never split internally.
DOC_CATEGORIES = ("specifications", "addenda", "rfp")

CATEGORIES = TRADE_CATEGORIES + DOC_CATEGORIES + ("other",)

CATEGORY_LABELS = {
    "general_drawings": "General / Cover Sheets",
    "civil_drawings": "Civil Drawings",
    "structural_drawings": "Structural Drawings",
    "architectural_drawings": "Architectural Drawings",
    "mechanical_drawings": "Mechanical Drawings",
    "plumbing_drawings": "Plumbing Drawings",
    "electrical_drawings": "Electrical Drawings",
    "fire_protection_drawings": "Fire Protection Drawings",
    "low_voltage_drawings": "Low Voltage Drawings",
    "specifications": "Specifications",
    "addenda": "Addenda",
    "rfp": "RFP",
    "other": "Other",
}

# What the triage verdict can say a FILE is (bid_split_files.file_kind).
FILE_KINDS = ("drawing_set", "specifications", "rfp", "addendum", "mixed", "other")

# File kinds that stop the pipeline at triage: identified, labeled, left
# intact. Everything else ('drawing_set', 'mixed') earns the per-page pass.
INTACT_KINDS = ("specifications", "rfp", "addendum", "other")

# File kinds whose segments come from the per-page split pass. A user
# re-typing a file to one of these can force a reprocess: execute() then
# skips triage and pins their verdict (the model must split, not re-triage).
SPLIT_KINDS = ("drawing_set", "mixed")

# The segment category an intact whole-file verdict is recorded under.
_KIND_TO_CATEGORY = {
    "specifications": "specifications",
    "rfp": "rfp",
    "addendum": "addenda",
    "other": "other",
}

_TRIAGE_SYSTEM = """You identify construction bid package PDFs for an electrical subcontractor. You see a small SAMPLE of pages from one PDF (each image preceded by its page label); most of the file is not shown. Decide what the FILE AS A WHOLE is:

- drawing_set: a set of drawing sheets (plans, elevations, details, schedules in title-blocked sheets), possibly covering several trades.
- specifications: a project manual / spec book in CSI format (section numbers like 26 05 19). Figures, diagrams or reduced drawings printed inside spec sections do NOT make it a drawing set - they reference the drawings.
- rfp: procurement documents: request for proposal, invitation or advertisement to bid, instructions to bidders, bid forms, agreement and contract terms, general and supplementary conditions, insurance and bonding requirements.
- addendum: an addendum package - a narrative of changes, possibly with reissued sheets or spec pages attached.
- mixed: more than one of the above bound into one PDF (e.g. RFP then specifications then drawings). Use mixed whenever the sampled pages belong to clearly different document types, or a table of contents lists multiple document types.
- other: none of the above (geotechnical report, permit set, schedule, ...). Set kind_label to a short label such as "Geotechnical Report".

kind_label must be null for every kind except other.

Also give:
- title: a short document title (e.g. "Project Manual Vol. 1", "Addendum No. 2"), from a cover page when one is shown; null if none is evident.
- description: one or two sentences saying what the file contains, specific enough that an estimator knows whether to open it.
- confidence: your confidence in the kind, 0 to 1. Remember the sample is sparse: if the sampled pages are consistent but the file could plausibly hold other document types between them, lower your confidence or answer mixed.
- confidence_reason: one or two sentences, written for an estimator, saying WHY the confidence is that high or that low: what in the sampled pages settled the kind (a cover page, CSI section numbers, title blocks) and what left it open (pages you could not read, a table of contents listing document types the sample never showed). Name the evidence; do not restate the number and do not say "I am confident". Keep it under 50 words."""

_TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind", "kind_label", "title", "description", "confidence",
        "confidence_reason",
    ],
    "properties": {
        "kind": {"type": "string", "enum": list(FILE_KINDS)},
        "kind_label": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "confidence_reason": {"type": ["string", "null"]},
    },
}

_CLASSIFY_SYSTEM = """You classify pages of construction bid packages for an electrical subcontractor. Each request contains rendered page images from ONE PDF, each image preceded by its page label.

Assign every page exactly one category:
- general_drawings: the drawing set's cover sheet, drawing index, and general G-series sheets (symbols, abbreviations, code/life-safety general notes for the overall set).
- civil_drawings: civil/site sheets (C- series): grading, drainage, site utilities, paving, erosion control.
- structural_drawings: structural sheets (S- series): foundations, framing plans, structural details and schedules.
- architectural_drawings: architectural sheets (A- series): floor plans, elevations, sections, reflected ceiling plans, finish schedules and details.
- mechanical_drawings: mechanical and HVAC sheets (M- or H- series): ductwork, piping, equipment schedules, controls.
- plumbing_drawings: plumbing sheets (P- series): domestic water, sanitary, storm, gas, fixture schedules.
- electrical_drawings: electrical sheets (E-, EL-, EP- series): power and lighting plans, one-line and riser diagrams, panel schedules, electrical site plans and details.
- fire_protection_drawings: fire protection sheets: sprinkler and suppression (FP-, F- series) AND fire alarm (FA- series).
- low_voltage_drawings: technology and low-voltage sheets (T-, TC-, LV- series): telecom and data, structured cabling, security, audio-visual, nurse call.
- specifications: project manual and spec book pages in CSI format (section numbers like 26 05 19), including the table of contents. A figure, diagram or reduced drawing printed INSIDE a spec section is still specifications - it references the drawings and must stay with them.
- addenda: addendum narratives and cover pages listing changes. A drawing sheet reissued under an addendum still belongs to its discipline category, not here.
- rfp: procurement documents: request for proposal, invitation or advertisement to bid, instructions to bidders, bid forms, agreement and contract terms, general and supplementary conditions, insurance and bonding requirements.
- other: everything else (landscape drawings, geotechnical reports, permits, schedules, and any trade not listed above - e.g. marine/underwater work). Set other_type to a short label such as "Landscape Drawings" or "Geotechnical Report", and keep the spelling identical across consecutive pages of the same document so they merge into one segment.

other_type must be null for every category except other.

For every page also give:
- title: the sheet number and title from the title block (e.g. "E2.1 - Level 2 Lighting Plan"), or a short description when there is no title block; null if unreadable.
- confidence: your confidence in the category assignment, 0 to 1.
- confidence_reason: ONE short clause (at most 15 words) naming what drove that page's confidence, e.g. "E-series sheet number in a legible title block", "no title block; read from the panel schedule", "duct plan with lighting shown for reference, could be either trade". Say what you saw, not how sure you feel.

Pages arrive in document order. Return one entry per page, in the same order, using the exact page numbers from the labels."""

_PAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pages"],
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "page", "category", "other_type", "title", "confidence",
                    "confidence_reason",
                ],
                "properties": {
                    "page": {"type": "integer"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "other_type": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "confidence_reason": {"type": ["string", "null"]},
                },
            },
        }
    },
}

_NAME_SYSTEM = """You name the documents found inside a construction bid package PDF that has been split into segments by category. For each segment produce:
- name: a short document title (e.g. "Electrical Drawings", "Addendum No. 2", "Division 26 Specifications"). No page numbers.
- description: one or two sentences saying what the segment contains, specific enough that an estimator knows whether to open it.
Return one entry per segment, in the given order."""

_NAME_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["segments"],
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        }
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Domain row marks (queue adapter) ─────────────────────────────────────


def _mark(file_id: str, **fields) -> None:
    """Patch the bid_split_files row (the llm_queue's domain mark), keeping the
    timing stamps and the parent job's aggregate status in step."""
    status = fields.get("status")
    if status == "running":
        fields.setdefault("started_at", _now_iso())
        fields.setdefault("finished_at", None)
    elif status in ("done", "failed"):
        fields.setdefault("finished_at", _now_iso())
    elif status == "pending":
        fields.setdefault("finished_at", None)
    sb = get_supabase()
    rows = (
        sb.table("bid_split_files").update(fields).eq("id", file_id).execute()
    ).data or []
    if rows:
        refresh_job(rows[0]["job_id"])


def refresh_job(job_id: str, _recheck: bool = True) -> None:
    """Derive the job's aggregate status from its files. Called after every
    file transition; last writer wins and all inputs are re-read, so
    concurrent per-file workers converge on the right answer.

    The write that takes a job OUT of processing is conditional on the row
    still reading processing, so the completion notification below fires
    exactly once per run even when two workers finish their files together.
    A processing write re-reads the files afterwards: if every file has
    already settled (the other worker's terminal write raced ahead of this
    stale one), one more pass re-derives and lands the terminal status, so
    the job cannot stick at processing with nothing left to do."""
    sb = get_supabase()
    files = (
        sb.table("bid_split_files").select("status").eq("job_id", job_id).execute()
    ).data or []
    if not files:
        return
    statuses = {f["status"] for f in files}
    if statuses & {"pending", "running"}:
        sb.table("bid_split_jobs").update(
            {"status": "processing", "completed_at": None}
        ).eq("id", job_id).execute()
        if _recheck:
            again = (
                sb.table("bid_split_files").select("status").eq("job_id", job_id).execute()
            ).data or []
            if again and not ({f["status"] for f in again} & {"pending", "running"}):
                refresh_job(job_id, _recheck=False)
        return
    if statuses == {"done"}:
        status = "done"
    elif statuses == {"failed"}:
        status = "failed"
    else:
        status = "done_with_errors"
    rows = (
        sb.table("bid_split_jobs")
        .update({"status": status, "completed_at": _now_iso()})
        .eq("id", job_id)
        .eq("status", "processing")
        .execute()
    ).data or []
    if rows:
        _notify_job_finished(rows[0], status, files)


# In-app notification type for a finished run. The bell deep-links it to
# /bid-splitter?job=<id> (metadata carries the job id; there is no project).
JOB_FINISHED_NOTIFICATION = "bid_split.finished"


def _notify_job_finished(job: dict, status: str, files: list[dict]) -> None:
    """Tell whoever started the job that it has settled. The run continues on
    the server after they leave the page, so the bell is how they learn it
    finished; no email mirror (a split is a workbench action, not a task
    hand-off). Best-effort: a notification failure never fails the mark."""
    user_id = job.get("created_by")
    if not user_id:
        return
    total = len(files)
    done = sum(1 for f in files if f["status"] == "done")
    failed = total - done
    if status == "done":
        message = f"Bid splitter finished: all {total} file{'s' if total != 1 else ''} are ready."
    elif status == "failed":
        message = f"Bid splitter finished: all {total} file{'s' if total != 1 else ''} failed."
    else:
        message = (
            f"Bid splitter finished: {done} of {total} files are ready, "
            f"{failed} failed."
        )
    try:
        notifications.notify_user(
            user_id,
            None,
            JOB_FINISHED_NOTIFICATION,
            message,
            mirror_email=False,
            metadata={"job_id": job["id"], "status": status},
        )
    except Exception:  # noqa: BLE001 — the status write stands; the bell is best-effort
        logger.exception("bid_split: completion notification failed for job %s", job["id"])


def _delete_segments(file_id: str, keep_paths: set[str] | frozenset[str] = frozenset()) -> None:
    """Drop any segments (rows + objects) a previous interrupted attempt left,
    so a queue re-run replaces cleanly instead of duplicating. is_original
    rows point at the UNTOUCHED SOURCE object - the row goes, the object
    must stay. `keep_paths` names objects the replacement rows still point
    at (a segment edit reuses untouched cuts) - those rows go, objects stay."""
    sb = get_supabase()
    rows = (
        sb.table("bid_split_segments")
        .select("id, storage_path, is_original")
        .eq("file_id", file_id)
        .execute()
    ).data or []
    for row in rows:
        if row.get("is_original") or row["storage_path"] in keep_paths:
            continue
        try:
            storage.delete_file(row["storage_path"])
        except Exception:  # noqa: BLE001 — a missing object must not block the re-run
            logger.warning("bid_split: could not delete stale object %s", row["storage_path"])
    if rows:
        sb.table("bid_split_segments").delete().eq("file_id", file_id).execute()


# ── Training capture: model input references + pristine output ───────────


def _persist_training_io(sb, file_id: str, snapshot: dict, output: dict) -> None:
    """Write the model-input references and pristine model output onto the
    file row (the raw material a correction's training example is built
    from, 0113). A direct update on purpose, NOT _mark: these land mid-run,
    several times, and must not churn refresh_job. Supabase update replaces
    the whole jsonb column, so callers always pass the full accumulated
    dicts."""
    sb.table("bid_split_files").update(
        {"input_snapshot": snapshot, "model_output": output or None}
    ).eq("id", file_id).execute()


def _snapshot_pages(output: dict, pages_meta: list[dict]) -> dict:
    """Merge the pristine per-page classifications into the accumulated model
    output BEFORE repair_islands mutates them in place - deep-copied so the
    stored output stays what the model actually said."""
    output["pages"] = copy.deepcopy(pages_meta)
    return output


# ── Triage (stage 1: what IS this file?) ─────────────────────────────────


def _sample_pages(total: int, k: int) -> list[int]:
    """Pages (1-based) for the triage sample: the first three and the last
    page anchor the ends (RFPs and spec-book covers front-load), the rest
    spread evenly through the interior. Rounding collisions on tiny files may
    return fewer than k pages; that is fine."""
    if total <= k:
        return list(range(1, total + 1))
    picks = {1, 2, 3, total}
    fill = k - len(picks)
    for i in range(1, fill + 1):
        picks.add(3 + round((total - 3) * i / (fill + 1)))
    return sorted(picks)


# Reasoning is model free text riding on a display-only path (a hover panel);
# bound it so an unhinged reply cannot bloat every row or the analytics
# payload. Truncation is silent - a clipped reason still reads.
_TRIAGE_REASON_MAX_CHARS = 500
_PAGE_REASON_MAX_CHARS = 240
_SEGMENT_REASON_MAX_CHARS = 800


def _truncate_reason(text: str, limit: int) -> str:
    """Cap a reason without cutting a word in half. A model that overruns the
    cap mid-sentence would otherwise leave the hover panel ending on "it must"
    (seen live), which reads as a bug: fall back to the last complete sentence
    inside the limit, else the last whole word plus an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    stop = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if stop >= limit // 2:
        return head[: stop + 1]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip(",;: ") + "..."


def _clean_reason(value, limit: int) -> str | None:
    """One line of model reasoning: trimmed, newlines collapsed (the FE shows
    it in a hover panel), length-capped on a word boundary. Missing or blank
    means the model did not say - never a fabricated explanation."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return _truncate_reason(text, limit) if text else None


def _validate_triage(result: dict) -> dict:
    """A usable triage verdict: known kind, clamped confidence, label only on
    'other'. Anything else is unusable output (regeneration usually fixes)."""
    if not isinstance(result, dict) or result.get("kind") not in FILE_KINDS:
        raise LlmBadOutput("The model reply did not identify the file. Retry the run.")
    try:
        confidence = min(1.0, max(0.0, float(result.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0
    kind_label = result.get("kind_label")
    kind_label = str(kind_label).strip() if kind_label else None
    title = result.get("title")
    title = str(title).strip() if title else None
    description = result.get("description")
    description = str(description).strip() if description else None
    return {
        "kind": result["kind"],
        "kind_label": kind_label if result["kind"] == "other" else None,
        "title": title,
        "description": description,
        "confidence": confidence,
        "confidence_reason": _clean_reason(
            result.get("confidence_reason"), _TRIAGE_REASON_MAX_CHARS
        ),
    }


def _triage_prompt(sample: list[int], total_pages: int, filename: str) -> str:
    """The exact triage user prompt - built once, both sent to the model and
    snapshotted onto the file row (0113)."""
    return (
        f"The pages above are a sample ({len(sample)} of {total_pages} pages, "
        f"at the labeled positions) from the file \"{filename}\". "
        "Identify what the file as a whole is."
    )


def _triage(
    pdf: bytes, filename: str, total_pages: int, s: Settings,
    sample: list[int], prompt: str,
) -> tuple[dict, int, int]:
    """One vision call over a page sample decides what the file is. The caller
    computes the sample and prompt (execute snapshots them onto the file row
    before this call runs). Returns (verdict, llm_calls, llm_ms). One in-run
    retry on unusable output."""
    images = pdf_split.render_pages(
        pdf,
        [p - 1 for p in sample],
        long_side=s.bid_split_render_long_side,
        jpeg_quality=s.bid_split_render_jpeg_quality,
    )
    labeled = [(f"Page {p} of {total_pages}", img) for p, img in zip(sample, images)]
    llm_calls = 0
    llm_ms = 0
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            llm_calls += 1
            result = llm.complete_images_json(
                "bid_split",
                system=_TRIAGE_SYSTEM,
                prompt=prompt,
                images=labeled,
                schema=_TRIAGE_SCHEMA,
                schema_name="file_triage",
                max_tokens=s.bid_split_max_tokens,
                settings=s,
            )
            return _validate_triage(result), llm_calls, llm_ms
        except LlmBadOutput:
            if attempt == 2:
                raise
            logger.warning("bid_split: unusable triage reply for %s, retrying once", filename)
        finally:
            llm_ms += int((time.monotonic() - started) * 1000)
    raise AssertionError("unreachable")  # pragma: no cover


# ── Classification (stage 2: per-page pass) ──────────────────────────────


def _batches(pages: list[int], size: int) -> list[list[int]]:
    return [pages[i : i + size] for i in range(0, len(pages), size)]


def _validate_batch(result: dict, expected_pages: list[int]) -> list[dict]:
    """One entry per requested page, categories from the vocabulary,
    confidence clamped to 0..1. Anything else is unusable output (transient:
    regeneration usually fixes it)."""
    entries = result.get("pages") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        raise LlmBadOutput("The model reply was missing the pages list. Retry the run.")
    by_page: dict[int, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        page = entry.get("page")
        if isinstance(page, int):
            by_page.setdefault(page, entry)
    out: list[dict] = []
    for page in expected_pages:
        entry = by_page.get(page)
        if entry is None:
            raise LlmBadOutput(
                f"The model reply skipped page {page}. Retry the run."
            )
        category = entry.get("category")
        if category not in CATEGORIES:
            raise LlmBadOutput(
                f"The model returned an unknown category for page {page}. Retry the run."
            )
        try:
            confidence = min(1.0, max(0.0, float(entry.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.0
        other_type = entry.get("other_type")
        other_type = str(other_type).strip() if other_type else None
        title = entry.get("title")
        title = str(title).strip() if title else None
        out.append(
            {
                "page": page,
                "category": category,
                "other_type": other_type if category == "other" else None,
                "title": title,
                "confidence": confidence,
                "confidence_reason": _clean_reason(
                    entry.get("confidence_reason"), _PAGE_REASON_MAX_CHARS
                ),
            }
        )
    return out


def _classify_context(verdict: dict, forced: bool = False) -> str:
    """One sentence of triage context for the per-page prompt, so the model
    knows what kind of file it is looking at. `forced` = the kind is a user
    verdict (a forced reprocess), not a triage scan."""
    kind = verdict["kind"]
    lead = "The user identified" if forced else "A quick scan identified"
    if kind == "drawing_set":
        return (
            f"{lead} this file as a drawing set: expect nearly "
            "every page to be a title-blocked drawing sheet, classified by trade."
        )
    if kind == "mixed":
        return (
            f"{lead} this file as a mixed package holding more "
            "than one document type: watch for the boundaries between documents."
        )
    label = verdict["kind_label"] or kind
    return (
        f"A quick scan tentatively identified this file as \"{label}\" but with "
        "low confidence; verify page by page."
    )


def _classify_prompt(
    context: str, batch: list[int], total_pages: int, filename: str
) -> str:
    """The exact per-batch user prompt - built the same way for the model call
    and for the input snapshot (0113), so the snapshot never drifts from what
    was actually sent."""
    return (
        f"{context} Classify each of the {len(batch)} pages above (pages "
        f"{batch[0]}-{batch[-1]} of the {total_pages}-page file "
        f"\"{filename}\"). Return one entry per page."
    )


def _classify_pages(
    pdf: bytes, filename: str, total_pages: int, s: Settings, verdict: dict,
    forced: bool = False,
) -> tuple[list[dict], int, int]:
    """Classify every page. Returns (page entries, llm_calls, llm_ms). One
    in-run retry per batch on unusable output before the whole file fails."""
    context = _classify_context(verdict, forced)
    pages_meta: list[dict] = []
    llm_calls = 0
    llm_ms = 0
    for batch in _batches(list(range(1, total_pages + 1)), s.bid_split_pages_per_call):
        images = pdf_split.render_pages(
            pdf,
            [p - 1 for p in batch],
            long_side=s.bid_split_render_long_side,
            jpeg_quality=s.bid_split_render_jpeg_quality,
        )
        labeled = [
            (f"Page {p} of {total_pages}", img) for p, img in zip(batch, images)
        ]
        prompt = _classify_prompt(context, batch, total_pages, filename)
        for attempt in (1, 2):
            started = time.monotonic()
            try:
                llm_calls += 1
                result = llm.complete_images_json(
                    "bid_split",
                    system=_CLASSIFY_SYSTEM,
                    prompt=prompt,
                    images=labeled,
                    schema=_PAGE_SCHEMA,
                    schema_name="page_classification",
                    max_tokens=s.bid_split_max_tokens,
                    settings=s,
                )
                pages_meta.extend(_validate_batch(result, batch))
                break
            except LlmBadOutput:
                if attempt == 2:
                    raise
                logger.warning(
                    "bid_split: unusable batch reply (pages %s-%s), retrying once",
                    batch[0],
                    batch[-1],
                )
            finally:
                llm_ms += int((time.monotonic() - started) * 1000)
    return pages_meta, llm_calls, llm_ms


# ── Segments ─────────────────────────────────────────────────────────────


def _run_key(entry: dict) -> tuple[str, str]:
    """Identity a page merges under: the category, plus the (case-folded)
    other_type for 'other' so unrelated misc documents never collapse."""
    if entry["category"] == "other":
        return ("other", (entry["other_type"] or "").lower())
    return (entry["category"], "")


def _page_label(entry: dict) -> str:
    """How a page's category reads to a user: the category label, or the
    model's free-text label for 'other'."""
    if entry["category"] == "other" and entry.get("other_type"):
        return entry["other_type"]
    return CATEGORY_LABELS.get(entry["category"], entry["category"])


def repair_islands(pages_meta: list[dict], max_island: int) -> list[dict]:
    """Absorb short misclassified runs ("islands") into their surroundings.

    The per-page classifier reads each page on its own, so a figure printed
    inside a spec section looks like a drawing and a schedule sheet inside an
    electrical run looks like specifications. Document flow says otherwise:
    a run of <= max_island pages whose two neighbors share one category (and
    are together at least as long as the island) is reassigned to that
    category. At the file's edges the same absorption applies only INTO a
    document category (specifications/addenda/rfp) - a spec book's cover page
    reading as a drawing sheet - never out of one: a small genuine drawing
    section at the head of a mixed package must survive.

    Runs strictly decrease on every absorption, so iterating to a fixpoint
    terminates. Reassigned pages keep their original confidence: the segment
    mean stays honest about the disagreement.
    """
    while True:
        runs: list[list] = []  # [key, start_idx, end_idx]
        for i, entry in enumerate(pages_meta):
            key = _run_key(entry)
            if runs and runs[-1][0] == key:
                runs[-1][2] = i
            else:
                runs.append([key, i, i])
        absorbed = False
        for idx, (key, start, end) in enumerate(runs):
            length = end - start + 1
            if length > max_island:
                continue
            prev_run = runs[idx - 1] if idx > 0 else None
            next_run = runs[idx + 1] if idx + 1 < len(runs) else None
            donor = None
            if (
                prev_run is not None
                and next_run is not None
                and prev_run[0] == next_run[0]
                and prev_run[0] != key
            ):
                neighbors = (prev_run[2] - prev_run[1] + 1) + (
                    next_run[2] - next_run[1] + 1
                )
                if neighbors >= length:
                    donor = prev_run
            elif (prev_run is None) != (next_run is None):
                edge = prev_run if prev_run is not None else next_run
                if (
                    edge[0] != key
                    and edge[0][0] in DOC_CATEGORIES
                    and key[0] not in DOC_CATEGORIES
                    and (edge[2] - edge[1] + 1) >= length
                ):
                    donor = edge
            if donor is not None:
                source = pages_meta[donor[1]]
                for i in range(start, end + 1):
                    # What the page read as ON ITS OWN, kept for the segment's
                    # confidence reasoning. setdefault, not assignment: a page
                    # absorbed twice (inner island, then outer) must still
                    # report the category the model actually gave it.
                    pages_meta[i].setdefault(
                        "reassigned_from", _page_label(pages_meta[i])
                    )
                    pages_meta[i]["category"] = source["category"]
                    pages_meta[i]["other_type"] = source["other_type"]
                absorbed = True
                break  # run indices are stale now; rebuild and rescan
        if not absorbed:
            return pages_meta


def merge_pages(pages_meta: list[dict]) -> list[dict]:
    """Fold consecutive same-category pages into segments. 'other' pages only
    merge while their other_type matches (case-insensitively), so two unrelated
    misc documents never collapse into one output PDF."""
    segments: list[dict] = []
    for entry in pages_meta:
        current = segments[-1] if segments else None
        if current is not None and _run_key(entry) == current["_key"]:
            current["page_end"] = entry["page"]
            current["confidences"].append(entry["confidence"])
            current["pages"].append(entry)
            if entry["title"]:
                current["titles"].append(entry["title"])
        else:
            segments.append(
                {
                    "_key": _run_key(entry),
                    "category": entry["category"],
                    "other_type": entry["other_type"],
                    "page_start": entry["page"],
                    "page_end": entry["page"],
                    "confidences": [entry["confidence"]],
                    # The page entries themselves, for the confidence
                    # reasoning (per-page reasons, and which pages island
                    # repair folded in).
                    "pages": [entry],
                    "titles": [entry["title"]] if entry["title"] else [],
                }
            )
    for seg in segments:
        del seg["_key"]
    return segments


# ── Confidence reasoning ─────────────────────────────────────────────────
#
# The score alone tells an estimator nothing actionable: 63% could mean an
# unreadable title block, a sheet that genuinely belongs to two trades, or a
# segment whose pages disagreed with each other. Every stage therefore asks
# the model WHY, and these composers turn those per-page answers into the one
# sentence-or-three the FE shows on hover over the score. Everything here is
# derived from what the model actually returned: when it said nothing, the
# reason says less rather than inventing a justification.


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _ends_sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


def triage_confidence_reason(
    verdict: dict, sample_size: int, total_pages: int
) -> str | None:
    """Why the FILE-level triage score is what it is. The sample size leads:
    triage reads a handful of pages out of the whole file, and that alone
    caps how sure it can be."""
    parts = []
    if total_pages and sample_size < total_pages:
        parts.append(
            f"Judged from a {sample_size}-page sample of the "
            f"{total_pages}-page file."
        )
    elif total_pages:
        parts.append(f"Judged from all {total_pages} pages.")
    reason = verdict.get("confidence_reason")
    if reason:
        parts.append(_ends_sentence(reason))
    return _truncate_reason(" ".join(parts), _SEGMENT_REASON_MAX_CHARS) or None


def build_segment_reason(seg: dict) -> str | None:
    """Why a cut segment's score is what it is.

    A segment's score is the mean of its pages, so the explanation has to
    account for the whole run: how wide the spread is, the model's own words
    on the page it was least sure of (the one worth opening), the same for
    its most confident page when the two disagree, and a note when island
    repair folded in pages that read as something else on their own.
    """
    pages = seg.get("pages") or []
    if not pages:
        return None
    confs = [p["confidence"] for p in pages]
    lowest = min(pages, key=lambda p: p["confidence"])
    highest = max(pages, key=lambda p: p["confidence"])
    parts: list[str] = []

    if len(pages) == 1:
        parts.append(f"Page {pages[0]['page']} scored {_pct(confs[0])}.")
        reason = pages[0].get("confidence_reason")
        if reason:
            parts.append(_ends_sentence(reason))
    else:
        mean = sum(confs) / len(confs)
        if min(confs) == max(confs):
            parts.append(
                f"Every one of the {len(pages)} pages scored {_pct(mean)}."
            )
        else:
            parts.append(
                f"Average of {len(pages)} pages, {_pct(min(confs))} to "
                f"{_pct(max(confs))}."
            )
        low_reason = lowest.get("confidence_reason")
        if low_reason and min(confs) == max(confs):
            # Nothing is "least sure" on a flat run: quote the first page as
            # what the whole run looked like.
            parts.append(
                f"Page {pages[0]['page']}: "
                f"{_ends_sentence(pages[0].get('confidence_reason') or low_reason)}"
            )
        elif low_reason:
            parts.append(
                f"Least sure on page {lowest['page']} at "
                f"{_pct(lowest['confidence'])}: {_ends_sentence(low_reason)}"
            )
        high_reason = highest.get("confidence_reason")
        if (
            high_reason
            and highest["page"] != lowest["page"]
            and highest["confidence"] - lowest["confidence"] >= 0.1
            and high_reason != low_reason
        ):
            parts.append(
                f"Most sure on page {highest['page']} at "
                f"{_pct(highest['confidence'])}: {_ends_sentence(high_reason)}"
            )

    # Island repair moved these pages here on document flow, against what the
    # model said about them page by page - the single most useful thing to
    # know when deciding whether to check a section by hand.
    moved = [
        p for p in pages
        if p.get("reassigned_from") and p["reassigned_from"] != _page_label(p)
    ]
    if moved:
        froms = list(dict.fromkeys(p["reassigned_from"] for p in moved))
        parts.append(
            f"{'Page' if len(moved) == 1 else 'Pages'} "
            f"{_pages_label([p['page'] for p in moved])} read as "
            f"{' / '.join(froms)} alone and "
            f"{'was' if len(moved) == 1 else 'were'} folded in from the "
            f"pages around {'it' if len(moved) == 1 else 'them'}."
        )
    return _truncate_reason(" ".join(parts), _SEGMENT_REASON_MAX_CHARS) or None


def pages_confidence_reason(pages_meta: list[dict]) -> str | None:
    """Why the FILE-level score is what it is once the per-page pass has
    overwritten the triage verdict: the same shape as a segment's reason, over
    every page of the file."""
    return build_segment_reason({"pages": pages_meta})


# Trade segments that get the file's General / Cover Sheets pages prepended
# to their output PDF. general_drawings itself keeps its own segment; document
# categories (specifications/addenda/rfp) and 'other' are separate documents,
# not plan sheets read alongside the covers.
COVER_PREFIX_CATEGORIES = frozenset(TRADE_CATEGORIES) - {"general_drawings"}


def general_cover_pages(segments: list[dict]) -> list[int]:
    """All pages classified General / Cover Sheets in this file, in document
    order. These carry the drawing index, symbols, abbreviations and general
    notes the whole set is read against, so they are prepended to every trade
    segment cut from the same source file."""
    pages: list[int] = []
    for seg in segments:
        if seg["category"] == "general_drawings":
            pages.extend(range(seg["page_start"], seg["page_end"] + 1))
    return pages


def _pages_label(pages: list[int]) -> str:
    """Compact label for an ascending page list: [1, 2, 3, 17] -> "1-3, 17"."""
    parts: list[str] = []
    start = prev = pages[0]
    for p in pages[1:] + [None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        parts.append(f"{start}-{prev}" if prev > start else str(start))
        if p is not None:
            start = prev = p
    return ", ".join(parts)


def _derive_kind(segments: list[dict]) -> tuple[str, str | None]:
    """What the per-page pass says the FILE is, to overwrite the triage
    verdict with ground truth: a lone whole-file segment maps to its
    category's kind; several segments are a drawing set unless a document
    category (specs/addenda/rfp) is among them, which makes it mixed."""
    if len(segments) == 1:
        seg = segments[0]
        cat = seg["category"]
        if cat == "specifications":
            return "specifications", None
        if cat == "rfp":
            return "rfp", None
        if cat == "addenda":
            return "addendum", None
        if cat == "other":
            return "other", seg["other_type"]
        return "drawing_set", None
    if {s["category"] for s in segments} & set(DOC_CATEGORIES):
        return "mixed", None
    return "drawing_set", None


def _fallback_name(segment: dict) -> str:
    if segment["category"] == "other" and segment["other_type"]:
        return segment["other_type"]
    return CATEGORY_LABELS[segment["category"]]


def _name_segments(
    segments: list[dict], filename: str, s: Settings
) -> tuple[int, int]:
    """Fill name/description on each segment via one cheap text call.
    Best-effort: any failure falls back to generated names, because a naming
    hiccup must not fail an otherwise finished split. Returns (calls, ms)."""
    for seg in segments:
        seg["name"] = _fallback_name(seg)
        titles = [t for t in seg["titles"][:3] if t]
        seg["description"] = (
            f"Pages {seg['page_start']}-{seg['page_end']} of {filename}."
            + (f" Starts with: {'; '.join(titles)}." if titles else "")
        )
    listing = "\n".join(
        (
            f"{i + 1}. category: {CATEGORY_LABELS[seg['category']]}"
            + (f" ({seg['other_type']})" if seg["other_type"] else "")
            + f", pages {seg['page_start']}-{seg['page_end']}"
            + (
                f", sheet titles: {'; '.join(seg['titles'][:12])}"
                if seg["titles"]
                else ""
            )
        )
        for i, seg in enumerate(segments)
    )
    prompt = (
        f"The bid package \"{filename}\" was split into {len(segments)} segments:\n"
        f"{listing}\n\nName and describe each segment."
    )
    started = time.monotonic()
    try:
        result = llm.complete_json(
            "bid_split",
            system=_NAME_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            schema=_NAME_SCHEMA,
            schema_name="segment_names",
            max_tokens=s.bid_split_max_tokens,
            settings=s,
        )
        entries = result.get("segments") if isinstance(result, dict) else None
        if isinstance(entries, list) and len(entries) == len(segments):
            for seg, entry in zip(segments, entries):
                name = str(entry.get("name") or "").strip()
                description = str(entry.get("description") or "").strip()
                if name:
                    seg["name"] = name
                if description:
                    seg["description"] = description
    except Exception:  # noqa: BLE001 — fallback names already stand
        logger.exception("bid_split: segment naming failed; using fallback names")
    return 1, int((time.monotonic() - started) * 1000)


# ── Execution ────────────────────────────────────────────────────────────


def cut_segment(
    pdf: bytes, frow: dict, seg: dict, general_pages: list[int], total_pages: int
) -> tuple[str, str, int, bool]:
    """Cut one segment's output PDF out of its source and upload it. Returns
    (storage_path, filename, size_bytes, is_original). Shared by execute()
    and the user's segment-edit endpoint so the semantics stay in one place:

    - A segment spanning the whole file is never cut or copied: it points at
      the untouched source object with is_original=True. (If cover pages
      exist they form their own segment, so no trade segment can span the
      whole file.)
    - Trade segments get the file's General / Cover Sheets pages prepended
      ("+ cover sheets" in the output filename); the stored page range stays
      the segment's own range in the source.
    """
    if seg["page_start"] == 1 and seg["page_end"] == total_pages:
        return frow["storage_path"], frow["filename"], frow["size_bytes"], True
    prefix = general_pages if seg["category"] in COVER_PREFIX_CATEGORIES else []
    if prefix:
        part = pdf_split.extract_pages(
            pdf,
            prefix + list(range(seg["page_start"], seg["page_end"] + 1)),
        )
        out_name = (
            f"{seg['name']} (pages {seg['page_start']}-{seg['page_end']}"
            " + cover sheets).pdf"
        )
    else:
        part = pdf_split.extract_range(pdf, seg["page_start"], seg["page_end"])
        out_name = (
            f"{seg['name']} (pages {seg['page_start']}-{seg['page_end']}).pdf"
        )
    # Big outputs (drawing cuts run to hundreds of MB) occasionally die at the
    # TLS layer mid-upload (httpx.TransportError; seen as ReadError
    # "SSLV3_ALERT_BAD_RECORD_MAC"). Retry on a fresh connection under a fresh
    # uuid path: if a failed attempt actually landed server-side, the orphan
    # object is swept with the job prefix on delete, never referenced.
    attempts = 3
    for attempt in range(1, attempts + 1):
        path = storage.build_bid_split_output_path(frow["job_id"], out_name)
        try:
            storage.upload_file(path, part, "application/pdf")
            break
        except httpx.TransportError:
            if attempt == attempts:
                raise
            logger.warning(
                "bid_split: upload of %s (%d bytes) dropped (attempt %d/%d); retrying",
                out_name,
                len(part),
                attempt,
                attempts,
            )
            time.sleep(2.0 * attempt)
    return path, out_name, len(part), False


def _set_kind(
    sb,
    file_id: str,
    kind: str,
    label: str | None,
    confidence: float | None,
    reason: str | None = None,
) -> None:
    """Score and reasoning are written together, always: a stale reason under
    a fresh number would be worse than no reason at all."""
    sb.table("bid_split_files").update(
        {
            "file_kind": kind,
            "file_kind_label": label,
            "file_kind_confidence": round(confidence, 3) if confidence is not None else None,
            "file_kind_confidence_reason": reason if confidence is not None else None,
        }
    ).eq("id", file_id).execute()


def execute(file_id: str, forced_kind: str | None = None) -> None:
    """Run one source file to completion. Raises on failure (the llm_queue
    classifies and retries); marks running/done itself, per the _JobSpec
    contract.

    `forced_kind` (a SPLIT_KINDS member) is a user verdict from a reprocess:
    triage is skipped, the per-page split pass always runs, and the file's
    kind stays exactly what the user set. A forced run must never let the
    model overturn the correction it exists to honor."""
    if forced_kind is not None and forced_kind not in SPLIT_KINDS:
        raise ValueError("Only a drawing set or mixed package can force a split.")
    s = get_settings()
    sb = get_supabase()
    rows = (
        sb.table("bid_split_files").select("*").eq("id", file_id).limit(1).execute()
    ).data or []
    if not rows:
        raise ValueError("This file no longer exists.")
    frow = rows[0]
    _mark(file_id, status="running", error=None)
    llm_calls = 0
    llm_ms = 0
    done = False
    try:
        pdf = storage.download_file(frow["storage_path"])
        total_pages = pdf_split.page_count(pdf)
        if total_pages > s.bid_split_max_pages_per_file:
            raise ValueError(
                f"The PDF has {total_pages} pages; the splitter limit is "
                f"{s.bid_split_max_pages_per_file} pages per file."
            )
        sb.table("bid_split_files").update({"page_count": total_pages}).eq(
            "id", file_id
        ).execute()
        _delete_segments(file_id)

        render = {
            "long_side": s.bid_split_render_long_side,
            "jpeg_quality": s.bid_split_render_jpeg_quality,
        }
        if forced_kind is None:
            # Snapshot the exact model input BEFORE the call - the dev Training
            # page pairs it with corrections, and even a failed run keeps what it
            # was asked (boq_extraction precedent). Page images are not inlined:
            # the recorded sample/batch page lists + render settings re-render
            # them from the stored source PDF.
            sample = _sample_pages(total_pages, s.bid_split_triage_pages)
            triage_prompt = _triage_prompt(sample, total_pages, frow["filename"])
            snapshot = {
                "model": llm.active_model("bid_split", s),
                "provider": llm.resolve("bid_split", s).provider,
                "render": render,
                "triage": {
                    "system": _TRIAGE_SYSTEM,
                    "prompt": triage_prompt,
                    "sample_pages": sample,
                },
            }
            raw_output: dict = {}
            _persist_training_io(sb, file_id, snapshot, raw_output)

            verdict, llm_calls, llm_ms = _triage(
                pdf, frow["filename"], total_pages, s, sample, triage_prompt
            )
            # The pristine verdict, persisted before anything derives from it.
            raw_output["triage"] = verdict
            _persist_training_io(sb, file_id, snapshot, raw_output)
            triage_reason = triage_confidence_reason(verdict, len(sample), total_pages)
            _set_kind(
                sb,
                file_id,
                verdict["kind"],
                verdict["kind_label"],
                verdict["confidence"],
                triage_reason,
            )

            if (
                verdict["kind"] in INTACT_KINDS
                and verdict["confidence"] >= s.bid_split_triage_min_confidence
            ):
                # Identified, labeled, left intact: one segment row pointing at
                # the untouched source object. No page pass, no copy.
                category = _KIND_TO_CATEGORY[verdict["kind"]]
                name = verdict["title"] or verdict["kind_label"] or CATEGORY_LABELS[category]
                sb.table("bid_split_segments").insert(
                    {
                        "file_id": file_id,
                        "sort_order": 0,
                        "category": category,
                        "other_type": verdict["kind_label"] if category == "other" else None,
                        "name": name,
                        "description": verdict["description"]
                        or f"Identified as {CATEGORY_LABELS[category]}; left intact.",
                        "confidence": round(verdict["confidence"], 3),
                        "confidence_reason": triage_reason,
                        "page_start": 1,
                        "page_end": total_pages,
                        "storage_path": frow["storage_path"],
                        "filename": frow["filename"],
                        "size_bytes": frow["size_bytes"],
                        "is_original": True,
                    }
                ).execute()
                _mark(file_id, status="done", error=None, llm_calls=llm_calls, llm_ms=llm_ms)
                done = True
                return
        else:
            # Forced reprocess: no triage call. The snapshot records the
            # pinned user verdict where the triage section would sit, so the
            # Training page shows how this run was steered.
            snapshot = {
                "model": llm.active_model("bid_split", s),
                "provider": llm.resolve("bid_split", s).provider,
                "render": render,
                "forced_kind": forced_kind,
            }
            raw_output = {}
            _persist_training_io(sb, file_id, snapshot, raw_output)
            verdict = {
                "kind": forced_kind,
                "kind_label": None,
                "title": None,
                "description": None,
                "confidence": None,
                "confidence_reason": None,
            }

        # The per-page batches are known upfront, so the exact per-batch
        # prompts join the snapshot before the calls start.
        context = _classify_context(verdict, forced=forced_kind is not None)
        snapshot["classify"] = {
            "system": _CLASSIFY_SYSTEM,
            "context": context,
            "batches": [
                {
                    "pages": batch,
                    "prompt": _classify_prompt(
                        context, batch, total_pages, frow["filename"]
                    ),
                }
                for batch in _batches(
                    list(range(1, total_pages + 1)), s.bid_split_pages_per_call
                )
            ],
        }
        _persist_training_io(sb, file_id, snapshot, raw_output)

        pages_meta, c_calls, c_ms = _classify_pages(
            pdf, frow["filename"], total_pages, s, verdict,
            forced=forced_kind is not None,
        )
        llm_calls += c_calls
        llm_ms += c_ms
        # Pristine per-page classifications, captured BEFORE island repair
        # mutates them in place - the repaired pages, derived kind and
        # segment rows are downstream computations.
        _snapshot_pages(raw_output, pages_meta)
        _persist_training_io(sb, file_id, snapshot, raw_output)
        pages_meta = repair_islands(pages_meta, s.bid_split_island_max_pages)
        segments = merge_pages(pages_meta)
        name_calls, name_ms = _name_segments(segments, frow["filename"], s)
        llm_calls += name_calls
        llm_ms += name_ms

        if forced_kind is None:
            # The page pass saw every page; its verdict on what the file is
            # outranks the sampled triage. Confidence = mean page confidence.
            # On a forced run the user's verdict stays untouched instead (no
            # score, Corrected badge kept): only the segments are the model's.
            kind, kind_label = _derive_kind(segments)
            page_conf = [p["confidence"] for p in pages_meta]
            _set_kind(
                sb,
                file_id,
                kind,
                kind_label,
                sum(page_conf) / len(page_conf) if page_conf else None,
                pages_confidence_reason(pages_meta),
            )

        # General / Cover Sheets pages from THIS file ride at the front of
        # every trade segment cut from it - the index, symbols and general
        # notes are context for reading any trade's sheets. The general run
        # still comes out as its own segment. page_start/page_end stay the
        # segment's own range in the source; the description says what was
        # added.
        general_pages = general_cover_pages(segments)
        if general_pages:
            note = (
                "Includes the General / Cover Sheets (pages "
                f"{_pages_label(general_pages)}) at the front."
            )
            for seg in segments:
                if seg["category"] in COVER_PREFIX_CATEGORIES:
                    seg["description"] = (
                        f"{seg['description']} {note}" if seg["description"] else note
                    )

        inserts = []
        for order, seg in enumerate(segments):
            path, out_name, size, whole_file = cut_segment(
                pdf, frow, seg, general_pages, total_pages
            )
            confidences = seg["confidences"]
            inserts.append(
                {
                    "file_id": file_id,
                    "sort_order": order,
                    "category": seg["category"],
                    "other_type": seg["other_type"],
                    "name": seg["name"],
                    "description": seg["description"],
                    "confidence": round(sum(confidences) / len(confidences), 3),
                    "confidence_reason": build_segment_reason(seg),
                    "page_start": seg["page_start"],
                    "page_end": seg["page_end"],
                    "storage_path": path,
                    "filename": out_name,
                    "size_bytes": size,
                    "is_original": whole_file,
                }
            )
        if inserts:
            sb.table("bid_split_segments").insert(inserts).execute()
        _mark(file_id, status="done", error=None, llm_calls=llm_calls, llm_ms=llm_ms)
        done = True
    finally:
        if not done:
            # Persist the spend before the queue writes its failure mark, so
            # the analytics still count the calls a failed run burned.
            try:
                sb.table("bid_split_files").update(
                    {"llm_calls": llm_calls, "llm_ms": llm_ms}
                ).eq("id", file_id).execute()
            except Exception:  # noqa: BLE001 — stats must not mask the real error
                logger.exception("bid_split: could not persist llm stats for %s", file_id)


def run_file(file_id: str, forced_kind: str | None = None) -> None:
    """BackgroundTasks fallback when the llm_queue is off or the enqueue
    failed: same run, but owns its own failure marking (no queue to do it)."""
    try:
        execute(file_id, forced_kind)
    except Exception as exc:  # noqa: BLE001 — terminal mark, mirrors queue behavior
        message = llm_errors.user_message(exc, llm.active_model("bid_split"))
        _mark(file_id, status="failed", error=message[:_ERROR_MAX_CHARS])
        logger.exception("bid_split: file %s failed (inline dispatch)", file_id)
