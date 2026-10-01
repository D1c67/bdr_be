"""RFP Ingestion: the Bid File Splitter step (docs/RFP_SPLIT.md sections 3
and 4).

Between the harvest and project creation, a `split` pipeline status stages
every verified harvested document into ONE Bid File Splitter job
(`bid_split_jobs.source = 'rfp'`) so the project is born with its documents
already categorized: drawing sets cut into trade sets, specifications / RFP
/ addenda identified and left intact, non-PDF files identified from the
sandbox's converted PDF (triage only, never cut) or from their name (one
text call). The promotion job then files each splitter segment as its own
`project_files` row with the mapped category, keeps the untouched drawing
set as `other` with `is_source_set = true`, and promotes intact files
exactly as before with the mapped category.

This module owns:

- `advance(sb, harvest, ...)`: the state machine both ingest modules call
  from their `_step_split` (start, wait, complete, failed, skipped), fenced
  on `rfp_harvests.split_status` so two workers holding two rows for the
  same harvest never stage two jobs. It never touches the source row: the
  caller CASes `split -> create` on the outcome.
- `SEGMENT_TO_FILE_CATEGORY`, `parse_addendum_number`, the per-kind maps
  for non-PDF verdicts, `validate_name_category` for the name classifier.
- `promote_split_file(...)`: the idempotent reconciliation of one harvest
  entry's splitter outcome into `project_files` (segments copied
  server-side from `bid-splits/`, the source set uploaded once, stale rows
  from an earlier cut removed), shared by the promotion job and by
  `resync_project_files`, which the splitter's correction routes call
  while the project's package has not sent.

The staging decision per entry is `rfp_create_files.promotion_for` plus the
byte check of its fetch (`rfp_create_files.fetch_entry`): a document the
sandbox would not let into the project is never handed to the splitter
either, so the promotion job and this step always agree.

Event-loop rule: every function here is sync (the Supabase SDK is sync);
callers run in the sweep thread, the queue worker or a threadpool.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.config import Settings, get_settings
from app.core.redact import redact_text
from app.sandbox import protocol
from app.services import files_needed, llm, llm_errors, llm_health, llm_queue, pdf_split, storage
from app.services import rfp_test
from app.services.notifications import audit

logger = logging.getLogger(__name__)

JOBS_TABLE = "bid_split_jobs"
FILES_TABLE = "bid_split_files"
SEGMENTS_TABLE = "bid_split_segments"
HARVESTS_TABLE = "rfp_harvests"
RUNS_TABLE = "rfp_ingest_runs"
PROJECT_FILES_TABLE = "project_files"
CREATED_TABLE = "rfp_created_projects"

SOURCE_RFP = "rfp"
SOURCE_MANUAL = "manual"

# rfp_harvests.split_status
SPLIT_NONE = "none"
SPLIT_PENDING = "pending"
SPLIT_RUNNING = "running"
SPLIT_COMPLETE = "complete"
SPLIT_FAILED = "failed"
SPLIT_SKIPPED = "skipped"
SPLIT_TERMINAL = (SPLIT_COMPLETE, SPLIT_FAILED, SPLIT_SKIPPED)

# split.skipped reasons (section 7). `queue_off` is this build's addition:
# the splitter runs only through the llm_jobs queue, so a deployment with
# the queue off skips the step like one with the flags off.
SKIP_FLAGS_OFF = "flags_off"
SKIP_NO_FILES = "no_files"
SKIP_LINKED = "linked"
SKIP_QUEUE_OFF = "queue_off"

# bid_split_files.classified_from
CLASSIFIED_PAGES = "pages"
CLASSIFIED_CONVERTED = "converted_pdf"
CLASSIFIED_NAME = "name"

# bid_split_files.source_format beyond the sandbox's own formats
FORMAT_IMAGE = "image"
FORMAT_OTHER = "other"
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".webp", ".heic")
_OFFICE_FORMATS = (
    protocol.SOURCE_FORMAT_DOCX, protocol.SOURCE_FORMAT_XLSX,
    protocol.SOURCE_FORMAT_DOC, protocol.SOURCE_FORMAT_XLS,
)

# Splitter segment category -> project_files.category (section 3.1).
SEGMENT_TO_FILE_CATEGORY = {
    "general_drawings": "drawing",
    "civil_drawings": "civil_drawing",
    "structural_drawings": "structural_drawing",
    "architectural_drawings": "architectural_drawing",
    "mechanical_drawings": "mechanical_drawing",
    "plumbing_drawings": "plumbing_drawing",
    "electrical_drawings": "electrical_drawing",
    "fire_protection_drawings": "fire_protection_drawing",
    "low_voltage_drawings": "low_voltage_drawing",
    "specifications": "specification",
    "addenda": "addendum",
    "rfp": "rfp",
    "other": "other",
}
# Triage verdict (bid_split.FILE_KINDS) -> the segment category a non-PDF
# file is recorded under when it is identified from its converted PDF.
KIND_TO_SEGMENT_CATEGORY = {
    "drawing_set": "general_drawings",
    "specifications": "specifications",
    "rfp": "rfp",
    "addendum": "addenda",
    "mixed": "other",
    "other": "other",
}
DRAWING_FILE_CATEGORIES = frozenset(
    v for k, v in SEGMENT_TO_FILE_CATEGORY.items() if k.endswith("_drawings")
)
CATEGORY_ADDENDUM = "addendum"
CATEGORY_OTHER = "other"
SOURCE_SET_NOTE = "Source set: split into {n} documents"

MSG_PACKAGE_SENT = "This project's package has already been sent; correct the files on the project."
MSG_JOB_HAS_PROJECT = (
    "This split was staged from an RFP invitation and its project still exists; "
    "discard the project first."
)
MSG_NOT_A_PDF = "This file is not a PDF; it was identified, not split, and cannot be re-cut."
_MSG_JOB_MISSING = "The split job is missing."
_MSG_ALL_FAILED = "Every document failed to split."
_MSG_STAGING_FAILED = "The documents could not be staged for splitting."

_ERROR_MAX_CHARS = 500
_NOTE_MAX_CHARS = 2000
_ADDENDUM_MAX_CHARS = 40
_IN_CHUNK = 200
# A `pending` claim whose stamp (`split_started_at`) is older than
# RFP_SPLIT_STAGING_STALE_SECONDS (default 5 minutes) belongs to a worker
# that died while staging (a deploy mid-run): the claim goes back to `none`
# so the next pass can start again, instead of every row behind it polling
# forever. A live staging re-stamps the claim every
# RFP_SPLIT_STAGING_HEARTBEAT_SECONDS (`_StagingClaim.beat`) however long it
# runs, so only a dead one ever ages past the window (docs/RFP_SPLIT.md 10.5).
_MSG_CLAIM_EXPIRED = "The staging claim expired; the split is being started again."
_MSG_CLAIM_LOST = "Another worker took over the staging claim; this staging was discarded."
_MSG_JOB_INTERRUPTED = "The split was interrupted (the server restarted). Run it again."
# The model being away is never a split failure: the step waits (no job
# staged, no attempt spent) while the splitter's model is not serving, and a
# run whose files died of the outage is queued again once it is back. A file
# is queued this many times in all before its failure counts as real, so a
# storage fault the queue also grades `unreachable` cannot loop forever.
_OUTAGE_KINDS = frozenset({
    llm_errors.KIND_UNREACHABLE, llm_errors.KIND_TIMEOUT, llm_errors.KIND_NOT_CONFIGURED,
    llm_errors.KIND_OUT_OF_TOKENS, llm_errors.KIND_UNAUTHORIZED,
})
_OUTAGE_RUNS_MAX = 3
_MSG_MODEL_AWAY = "The bid file splitting model is not available."
_MSG_SANDBOX_BUSY = "The sandbox is still checking the harvested documents."
_MSG_NONE_STAGED = "No harvested document passed the sandbox checks; nothing was staged."
_FILE_SELECT = (
    "id, job_id, filename, storage_path, size_bytes, page_count, status, error, file_kind, "
    "file_kind_label, rfp_sandbox_file_id, source_format, classified_from, created_at, updated_at"
)
_SEGMENT_SELECT = (
    "id, file_id, sort_order, category, other_type, name, description, page_start, page_end, "
    "storage_path, filename, size_bytes, is_original"
)
_PROJECT_ROW_SELECT = (
    "id, project_id, category, storage_path, filename, note, rfp_harvest_id, rfp_sandbox_file_id, "
    "bid_split_segment_id, bid_split_file_id, is_source_set, preview_path"
)

# "Addendum 3", "Addendum No. 3", "Add. #3", "ADD-03", "Addenda 2A": the
# number (leading zeros dropped, an optional letter suffix kept).
_ADDENDUM_RE = re.compile(
    r"\b(?:addendum|addenda|add)\b\.?\s*(?:no\.?|number|num\.?|#)?\s*[-:#.]?\s*0*(\d{1,3}[a-z]?)\b",
    re.IGNORECASE,
)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def enabled(settings: Settings | None = None) -> bool:
    """Both switches: the splitter itself and the pipeline step."""
    s = settings or get_settings()
    return bool(s.bid_file_splitter_enabled and s.rfp_split_enabled)


def parse_addendum_number(name: Any) -> str | None:
    """The addendum number a segment name carries (`Addendum 3`, `Add. #3`,
    `ADD-03`, case-insensitive), or None when nothing parses. Zero-padded
    numbers lose their padding; a trailing letter (3A) is kept, uppercased."""
    if not isinstance(name, str) or not name.strip():
        return None
    match = _ADDENDUM_RE.search(name)
    if not match:
        return None
    number = match.group(1).upper()
    return number[:_ADDENDUM_MAX_CHARS] or None


def file_category_for_segment(segment: dict) -> str:
    return SEGMENT_TO_FILE_CATEGORY.get(str(segment.get("category") or ""), CATEGORY_OTHER)


def segment_category_for_kind(kind: Any) -> str:
    """The segment category a non-PDF triage verdict is recorded under."""
    return KIND_TO_SEGMENT_CATEGORY.get(str(kind or ""), "other")


def validate_name_category(answer: Any) -> str:
    """The name classifier's answer, validated against the segment
    vocabulary (the map's keys); anything else is `other`."""
    text = str(answer or "").strip().lower()
    return text if text in SEGMENT_TO_FILE_CATEGORY else "other"


def source_format_for(file_row: dict | None, name: str) -> str:
    """pdf | docx | xlsx | doc | xls from the sandbox row, else image / other
    from the filename."""
    fmt = str((file_row or {}).get("source_format") or "").strip().lower()
    if fmt in protocol.SOURCE_FORMATS:
        return fmt
    if name.lower().endswith(_IMAGE_EXTS):
        return FORMAT_IMAGE
    return FORMAT_OTHER


def category_fields(category: str, segment: dict | None, filename: str | None = None) -> dict:
    """The project_files columns a category decides: the label and, for an
    addendum, the number parsed from the segment name, else from `filename`
    (the intact file's own name: a kind correction collapses the segment to
    the fallback name "Addenda", which carries no number, while
    "...Addendum No. 1....pdf" still does). Null issue date; the Estimating
    Admin fills both in later."""
    number = None
    if category == CATEGORY_ADDENDUM:
        number = parse_addendum_number((segment or {}).get("name")) or parse_addendum_number(filename)
    return {
        "category": category,
        "doc_type": None,
        "addendum_number": number,
        "addendum_issued_on": None,
    }


def is_split(segments: list[dict]) -> bool:
    """Non-original segments exist: the source was cut."""
    return bool(segments) and any(not s.get("is_original") for s in segments)


def context_for_email(row: dict, harvest: dict | None = None) -> dict:
    """What the name classifier gets to see beside the filename: the
    invitation subject, the project name the pipeline extracted or the
    platform reported, and the method."""
    data = (harvest or {}).get("data") if isinstance((harvest or {}).get("data"), dict) else {}
    return {
        "subject": (row.get("subject") or "")[:200] or None,
        "project_name": (
            (data.get("project_name") if isinstance(data, dict) else None)
            or row.get("extracted_project_name") or None
        ),
        "invitation_method": row.get("invitation_method"),
    }


def context_for_portal(inv: dict, harvest: dict | None = None) -> dict:
    return {
        "subject": (inv.get("title") or "")[:200] or None,
        "project_name": inv.get("title") or None,
        "invitation_method": inv.get("portal"),
    }


# ── Outcome ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    """What `advance` decided: `waiting` (poll again after
    rfp_split_poll_seconds, or after the pipeline's model-wait interval when
    `model_away`: the splitter's model is not serving and `reason` says so),
    or the terminal split status the harvest now carries (complete / failed /
    skipped): the caller moves the row on."""

    state: str
    reason: str | None = None
    job_id: str | None = None
    model_away: bool = False

    @property
    def waiting(self) -> bool:
        return self.state == "waiting"


def _entries(harvest: dict | None) -> list[dict]:
    from app.services import rfp_create_files as rcf

    # One staged row per sandbox file: a portal that lists the same bytes
    # twice (PipelineSuite shows a file at the root and again under its
    # addendum folder) harvests two entries sharing one sandbox id, and the
    # splitter must not cut the same set twice. The first entry wins.
    out = []
    seen: set[str] = set()
    for entry in (harvest or {}).get("files") or []:
        if isinstance(entry, dict) and entry.get("sandbox_file_id") and entry.get("status") in rcf.ENTRY_WITH_FILE:
            sid = str(entry["sandbox_file_id"])
            if sid in seen:
                continue
            seen.add(sid)
            out.append(entry)
    return out


def _cas_split(sb, harvest_id: str, expected: str, fields: dict) -> bool:
    rows = (
        sb.table(HARVESTS_TABLE)
        .update(fields)
        .eq("id", harvest_id)
        .eq("split_status", expected)
        .execute()
    ).data or []
    return bool(rows)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def stale_seconds(settings: Settings | None = None) -> int:
    """RFP_SPLIT_STAGING_STALE_SECONDS: how long a staging claim (or a
    processing job with nothing queued) may go untouched before it is read
    as dead."""
    s = settings or get_settings()
    return int(getattr(s, "rfp_split_staging_stale_seconds", 300) or 300)


def pending_is_stale(harvest: dict, now: datetime | None = None, settings: Settings | None = None) -> bool:
    """A `pending` claim whose stamp (`split_started_at`, written at the
    claim and re-written by the staging loop's heartbeat) is older than
    RFP_SPLIT_STAGING_STALE_SECONDS, or missing: the worker that took it is
    gone. A live staging, however slow, keeps its stamp fresh."""
    if harvest.get("split_status") != SPLIT_PENDING:
        return False
    at = _parse_ts(harvest.get("split_started_at"))
    if at is None:
        return True
    return at < (now or _now()) - timedelta(seconds=stale_seconds(settings))


def _cas_claim(sb, harvest_id: str, expected: str, stamp: str | None, fields: dict) -> bool:
    """`_cas_split` fenced on the claim's stamp too: a writer that read one
    claim never overwrites a newer one (a heartbeat that just landed, or a
    claim another worker took after this one went stale)."""
    query = sb.table(HARVESTS_TABLE).update(fields).eq("id", harvest_id).eq("split_status", expected)
    query = query.eq("split_started_at", stamp) if stamp else query.is_("split_started_at", "null")
    return bool(query.execute().data or [])


class StagingClaimLost(RuntimeError):
    """The staging claim this worker held was taken over (it went stale and
    another worker reclaimed it, or a second manual run re-claimed it): the
    staging stops, its job is discarded."""


class _StagingClaim:
    """The `pending` claim a staging holds on its harvest, fenced on its own
    `split_started_at` stamp (docs/RFP_SPLIT.md 10.5).

    `beat()` re-stamps the claim at most every
    RFP_SPLIT_STAGING_HEARTBEAT_SECONDS (`force` writes now) and then calls
    `renew` (the ingest sweep's lease renewal, so a long staging never loses
    the sweep lease; the manual run passes none). It returns False once the
    claim is gone: the CAS on (pending, our last stamp) matched nothing.
    A transport error on the write is NOT a lost claim (the window still has
    most of its time; the next beat retries). A False from `renew` is only
    logged: the harvest claim, not the sweep lease, fences the staging, and
    the sweep itself stands down at its next renewal."""

    def __init__(self, sb, harvest_id: str, stamp: str | None, settings: Settings,
                 renew: Callable[[], bool] | None = None) -> None:
        self.sb = sb
        self.harvest_id = harvest_id
        self.stamp = stamp
        self.renew = renew
        self.interval = float(getattr(settings, "rfp_split_staging_heartbeat_seconds", 30) or 30)
        self.lost = False
        self._last = time.monotonic()
        # A beat whose write may have landed although its answer never came
        # back (a dropped connection after the commit): the database may now
        # hold THIS stamp, so the next CAS tries it too before calling the
        # claim lost (review 2026-09-30).
        self._maybe: str | None = None

    def cas(self, fields: dict) -> bool:
        """`_cas_claim` on (pending, our stamp), falling back to the stamp of
        a beat whose answer was lost. Keeps `stamp` in step when the write
        re-stamps the claim. Transport errors propagate."""
        stamps = [self.stamp] + ([self._maybe] if self._maybe and self._maybe != self.stamp else [])
        for stamp in stamps:
            if _cas_claim(self.sb, self.harvest_id, SPLIT_PENDING, stamp, fields):
                self._maybe = None
                self.stamp = fields["split_started_at"] if "split_started_at" in fields else stamp
                return True
        return False

    def beat(self, *, force: bool = False) -> bool:
        if self.lost:
            return False
        if not force and time.monotonic() - self._last < self.interval:
            return True
        stamp = _iso(_now())
        try:
            held = self.cas({"split_started_at": stamp})
        except Exception:  # noqa: BLE001 - a hiccup is not a lost claim; the next beat retries
            logger.exception("rfp split: staging heartbeat failed for harvest %s", self.harvest_id)
            self._maybe = stamp
            return True
        self._last = time.monotonic()
        if not held:
            self.lost = True
            logger.warning("rfp split: staging claim on harvest %s was taken over", self.harvest_id)
            return False
        if self.renew is not None:
            try:
                if not self.renew():
                    logger.warning("rfp split: sweep lease lost during staging of harvest %s", self.harvest_id)
            except Exception:  # noqa: BLE001 - the claim fences the staging, not the lease
                logger.exception("rfp split: lease renewal failed during staging of %s", self.harvest_id)
        return True


def _record(sb, session_id: str | None, kind: str, title: str, *, level: str = rfp_test.LEVEL_INFO,
            detail: dict | None = None, harvest_id: str | None = None, rfp_email_id: str | None = None) -> None:
    rfp_test.record(
        sb, session_id=session_id, source=rfp_test.SOURCE_SPLIT, kind=kind, title=title, level=level,
        detail=detail, harvest_id=harvest_id, rfp_email_id=rfp_email_id,
    )


def _skip(sb, harvest: dict | None, reason: str, session_id: str | None, rfp_email_id: str | None) -> Outcome:
    if harvest is not None:
        _cas_split(sb, harvest["id"], SPLIT_NONE, {
            "split_status": SPLIT_SKIPPED, "split_error": reason, "split_finished_at": _iso(_now()),
        })
    _record(
        sb, session_id, "skipped", f"Split skipped: {reason}",
        detail={"harvest_id": (harvest or {}).get("id"), "reason": reason},
        harvest_id=(harvest or {}).get("id"), rfp_email_id=rfp_email_id,
    )
    return Outcome(SPLIT_SKIPPED, reason=reason)


def model_away(settings: Settings | None = None) -> tuple[str, str] | None:
    """Why the splitter's model cannot run right now, as `(state, detail)`,
    or None when it is serving: nothing configured for `bid_split`, or the
    cached LLM health snapshot grades the feature provider_down /
    model_missing / unconfigured (the same read the three LLM intake steps
    make for their features). A broken probe never blocks the step."""
    from app.services.rfp_email_ingest import model_unavailable  # late: that module imports this one

    s = settings or get_settings()
    if not llm.is_configured("bid_split", s):
        return "unconfigured", "No model is configured for bid file splitting."
    try:
        snapshot = llm_health.cached(s)
    except Exception:  # noqa: BLE001 - a broken probe must not stop the pipeline
        logger.exception("rfp split: LLM health snapshot failed; attempting the split")
        return None
    return model_unavailable(snapshot, "bid_split")


def _wait_for_model(
    sb, harvest: dict, away: tuple[str, str], session_id: str | None, rfp_email_id: str | None,
    *, job_id: str | None = None, requeued: int = 0,
) -> Outcome:
    """The row waits without spending an attempt; the bench sees why."""
    state, detail = away
    _record(
        sb, session_id, "waiting", f"Split waiting: {detail[:120]}", level=rfp_test.LEVEL_WARN,
        detail={"harvest_id": harvest["id"], "job_id": job_id, "state": state, "why": detail,
                "requeued": requeued},
        harvest_id=harvest["id"], rfp_email_id=rfp_email_id,
    )
    return Outcome("waiting", reason=detail[:_ERROR_MAX_CHARS], job_id=job_id, model_away=True)


def sandbox_busy(sb, harvest: dict, entries: list[dict]) -> tuple[int, int] | None:
    """`(still_checking, total)` while the sandbox has not yet answered for
    every document the split would stage, else None. The harvest goes
    `complete` the moment its downloads land (rfp_harvest._complete_harvest);
    the sandbox verdicts (rfp_ingest_files.status) arrive later, and a split
    that looks before they do sees every file as unverified and stages
    nothing. A file is pending while its row is not terminal AND its run is
    still active (or unknown): once the run itself is terminal every file
    has its answer, and a row still non-terminal then is the promotion
    check's to refuse, not ours to wait on."""
    from app.services import rfp_create_files as rcf

    ids = [str(e["sandbox_file_id"]) for e in entries]
    file_rows = rcf._file_rows(sb, ids)
    pending = [
        i for i in ids
        if (file_rows.get(i) or {}).get("status") not in protocol.FILE_TERMINAL_STATUSES
    ]
    if not pending:
        return None
    run_id = harvest.get("sandbox_run_id")
    if run_id:
        rows = sb.table(RUNS_TABLE).select("id, status").eq("id", run_id).limit(1).execute().data or []
        if rows and rows[0].get("status") in protocol.RUN_TERMINAL_STATUSES:
            return None
    return len(pending), len(ids)


def _wait_for_sandbox(
    sb, harvest: dict, pending: tuple[int, int], session_id: str | None, rfp_email_id: str | None,
) -> Outcome:
    """The row waits on the sandbox without spending an attempt or taking
    the claim; the bench sees how many documents are still being checked."""
    still, total = pending
    _record(
        sb, session_id, "waiting",
        f"Split waiting: the sandbox is still checking {still} of {total} document{'s' if total != 1 else ''}",
        detail={"harvest_id": harvest["id"], "job_id": None, "state": "sandbox_busy",
                "why": _MSG_SANDBOX_BUSY, "still_checking": still, "total": total,
                "sandbox_run_id": harvest.get("sandbox_run_id")},
        harvest_id=harvest["id"], rfp_email_id=rfp_email_id,
    )
    return Outcome("waiting", reason=_MSG_SANDBOX_BUSY)


def advance(
    sb, harvest: dict | None, *, settings: Settings | None = None, context: dict | None = None,
    test_session_id: str | None = None, rfp_email_id: str | None = None,
    renew: Callable[[], bool] | None = None,
) -> Outcome:
    """The split step's state machine (section 3.2), fenced on
    `rfp_harvests.split_status`:

    - flags off, queue off, no harvested entries, or a harvest that already
      has a project -> `skipped` (status written once, from `none`);
    - `none` with the sandbox still checking any entry (`sandbox_busy`) ->
      wait, no claim taken, no attempt spent: the harvest completes when its
      downloads land, the verdicts come later, and staging before them
      would refuse every file as unverified;
    - `none` with the splitter's model away (`model_away`) -> wait, no job
      staged, no attempt spent (`Outcome.model_away`);
    - `none` where no entry passes the promotion check -> `skipped`
      (`no_files`) with the job KEPT (status `failed`, zero files) and
      linked on `split_job_id`, and every refusal reason in the event;
    - `none` -> claim (`none -> pending`), stage the job, `running`; a lost
      claim means another worker is on it: wait;
    - `pending` (another worker staging) or `running` with the job still
      processing -> wait; a `pending` whose heartbeat stamp is older than
      RFP_SPLIT_STAGING_STALE_SECONDS (the worker died mid-staging) is put
      back to `none`, the half-staged job it left is discarded
      (`_discard_orphans`), and the split is started again;
    - `running` with a job that is `processing` but dead (nothing queued,
      nothing running, untouched for the stale window: a restart between a
      file's `pending` mark and its enqueue) -> its stranded files are
      marked failed "interrupted" and the job settles (`_reap_dead_job`);
    - job terminal with files that died of a model outage (their latest
      queue run failed with an outage kind) -> wait while the model is still
      away, then queue those files again, up to `_OUTAGE_RUNS_MAX` runs each;
    - job terminal -> `complete` (done / done_with_errors) or `failed`.

    Raises on staging trouble after putting the claim back to `none`, so the
    caller's retry ladder applies and a later attempt can start again.

    `renew` is the ingest sweep's lease renewal: the staging heartbeat calls
    it (docs/RFP_SPLIT.md 10.5) so a long staging keeps the sweep lease."""
    s = settings or get_settings()
    session_id = test_session_id or (harvest or {}).get("test_session_id")
    if harvest is None:
        return _skip(sb, None, SKIP_NO_FILES, session_id, rfp_email_id)
    status = str(harvest.get("split_status") or SPLIT_NONE)
    if status in SPLIT_TERMINAL:
        return Outcome(status, reason=harvest.get("split_error"), job_id=harvest.get("split_job_id"))
    if status == SPLIT_NONE:
        if not enabled(s):
            return _skip(sb, harvest, SKIP_FLAGS_OFF, session_id, rfp_email_id)
        if not s.llm_queue_enabled:
            return _skip(sb, harvest, SKIP_QUEUE_OFF, session_id, rfp_email_id)
        if harvest.get("project_id"):
            return _skip(sb, harvest, SKIP_LINKED, session_id, rfp_email_id)
        entries = _entries(harvest)
        if not entries:
            return _skip(sb, harvest, SKIP_NO_FILES, session_id, rfp_email_id)
        pending = sandbox_busy(sb, harvest, entries)
        if pending:
            return _wait_for_sandbox(sb, harvest, pending, session_id, rfp_email_id)
        away = model_away(s)
        if away:
            return _wait_for_model(sb, harvest, away, session_id, rfp_email_id)
        stamp = _iso(_now())
        # Fenced on the harvest still having no project: the row's read may
        # predate a creation that just linked one (the late-link race,
        # review 2026-09-30), and a split staged now would never be linked.
        claimed = bool((
            sb.table(HARVESTS_TABLE)
            .update({"split_status": SPLIT_PENDING, "split_error": None, "split_started_at": stamp})
            .eq("id", harvest["id"]).eq("split_status", SPLIT_NONE).is_("project_id", "null")
            .execute()
        ).data or [])
        if not claimed:
            fresh = _harvest_full(sb, harvest["id"]) or {}
            if fresh.get("project_id") and str(fresh.get("split_status") or SPLIT_NONE) == SPLIT_NONE:
                return _skip(sb, fresh, SKIP_LINKED, session_id, rfp_email_id)
            return Outcome("waiting")
        claim = _StagingClaim(sb, harvest["id"], stamp, s, renew)
        try:
            job_id, staged = _start(sb, harvest, entries, s, context or {}, session_id, rfp_email_id, claim=claim)
        except Exception as exc:
            # Fenced on our last stamp: a claim another worker took over
            # (ours went stale) is theirs, never put back.
            try:
                claim.cas({"split_status": SPLIT_NONE, "split_error": redact_text(exc)[:_ERROR_MAX_CHARS]})
            except Exception:  # noqa: BLE001 - the stale-claim rule frees it; the staging error is the one to raise
                logger.exception("rfp split: could not put the claim on harvest %s back", harvest["id"])
            raise
        if staged == 0:
            # Nothing passed the promotion check. The job row stays (failed,
            # zero files) so the refusal is visible and nothing that
            # references it disappears; `_start` recorded the reasons.
            claim.cas({
                "split_status": SPLIT_SKIPPED, "split_error": SKIP_NO_FILES, "split_job_id": job_id,
                "split_finished_at": _iso(_now()),
            })
            return Outcome(SPLIT_SKIPPED, reason=SKIP_NO_FILES, job_id=job_id)
        return Outcome("waiting", job_id=job_id)
    if status == SPLIT_PENDING:
        if harvest.get("project_id") and pending_is_stale(harvest, settings=s):
            # A stale claim on a harvest that already has its project (an
            # interrupted "Run the splitter", section 10.3). A mate row
            # reaching the split step must not reclaim it: back at `none`
            # the linked-harvest skip would overwrite the failed run with
            # `skipped` and the project's flag would vanish (the flag reads
            # a stale claim as failed; the person runs it again). The row
            # just moves on and links; nothing is written.
            return Outcome(SPLIT_SKIPPED, reason=SKIP_LINKED, job_id=harvest.get("split_job_id"))
        if pending_is_stale(harvest, settings=s) and _cas_claim(
            sb, harvest["id"], SPLIT_PENDING, harvest.get("split_started_at"), {
                "split_status": SPLIT_NONE, "split_error": _MSG_CLAIM_EXPIRED, "split_started_at": None,
            },
        ):
            logger.warning("rfp split: stale staging claim on harvest %s reclaimed", harvest["id"])
            # The dead staging's half-staged job (never linked to the
            # harvest, nothing queued) would sit at `processing` forever.
            _discard_orphans(sb, harvest, s)
            return advance(
                sb, {**harvest, "split_status": SPLIT_NONE, "split_started_at": None},
                settings=s, context=context, test_session_id=test_session_id, rfp_email_id=rfp_email_id,
                renew=renew,
            )
        return Outcome("waiting")
    return _check(sb, harvest, session_id, rfp_email_id, settings=s)


# ── Creation waits for a split in flight (docs/RFP_SPLIT.md 10.5) ───────────

MSG_CREATE_WAITS = (
    "The documents for this invitation are still being split. "
    "The project will be created when the split finishes."
)
_SOURCE_TABLES = ("rfp_emails", "rfp_portal_invitations")
STATUS_SPLIT = "split"


def _row_at_split(
    sb, harvest_id: str, exclude: tuple[str, str] | None, settings: Settings | None = None,
) -> bool:
    """Some email or portal invitation sharing the harvest sits at the
    `split` step (other than `exclude`, `(table, id)`: the row creating) AND
    a sweep is still visiting it: its next visit is scheduled in the future
    (every wait the split step writes), or it was touched within the stale
    window plus the longest wait interval. A row nothing visits any more (an
    ended test session's frozen rows, a sweep switched off) would otherwise
    hold creation forever (review 2026-09-30); creation then proceeds and a
    split that starts later links itself to the project
    (`_link_late_project`). Missing stamps count as visited."""
    s = settings or get_settings()
    now = _now()
    window = stale_seconds(s) + max(
        int(getattr(s, "rfp_split_poll_seconds", 30) or 30),
        int(getattr(s, "rfp_email_ingestion_classify_retry_seconds", 300) or 300),
    )
    cutoff = now - timedelta(seconds=window)
    for table in _SOURCE_TABLES:
        query = (
            sb.table(table).select("id, next_attempt_at, updated_at")
            .eq("harvest_id", harvest_id).eq("status", STATUS_SPLIT)
        )
        if exclude and exclude[0] == table:
            query = query.neq("id", exclude[1])
        for row in query.limit(50).execute().data or []:
            due = _parse_ts(row.get("next_attempt_at"))
            touched = _parse_ts(row.get("updated_at"))
            if (due is not None and due > now) or touched is None or touched >= cutoff:
                return True
    return False


def creation_must_wait(
    sb, harvest: dict | None, *, settings: Settings | None = None,
    exclude: tuple[str, str] | None = None,
) -> bool:
    """Whether creating a project from this harvest now would pre-empt a
    split that is going to file its documents. Several rows can share one
    harvest (a reminder, a second copy); pressing Create on a `done` copy
    while the leader is still at `split` built the project from whole,
    unsplit files, and the split then never re-filed them.

    The rule (never a deadlock: every wait ends when something that is
    certainly moving finishes):

    - never with no harvest, a harvest that already has a project (the row
      links), the split step off (either switch) or the queue off;
    - never once the harvest's split is terminal (complete / failed /
      skipped): the leader that just finished its own split creates in the
      same pass;
    - wait while a staging claim is live (`pending` with a fresh heartbeat:
      a worker is staging right now);
    - otherwise (`none`, `running`, a stale `pending`) wait only while a row
      sharing the harvest actually sits at the `split` step, since that row
      will move the split along (start it, restart it, grade it, or give up
      and move on). No row at `split` means nothing will ever move a `none`
      harvest (the flags came on later, a harvest older than the step) or
      finish grading a `running` one (the ladder gave up on its check and
      the leader moved on): creation proceeds, links a running job and lets
      its files re-file the project (section 10.2). A `none` harvest with
      no harvested entries never waits (the step would skip it at once).

    Best effort: a read failure never blocks creation (False)."""
    if not harvest or harvest.get("project_id"):
        return False
    s = settings or get_settings()
    if not (getattr(s, "bid_file_splitter_enabled", False) and getattr(s, "rfp_split_enabled", False)):
        return False
    if not getattr(s, "llm_queue_enabled", False):
        return False
    status = str(harvest.get("split_status") or SPLIT_NONE)
    if status in SPLIT_TERMINAL:
        return False
    if status == SPLIT_PENDING and not pending_is_stale(harvest, settings=s):
        return True
    if status == SPLIT_NONE and not _entries(harvest):
        return False
    try:
        return _row_at_split(sb, str(harvest["id"]), exclude, s)
    except Exception:  # noqa: BLE001 - a read failure never blocks creation
        logger.exception("rfp split: split-in-flight check failed for harvest %s", harvest.get("id"))
        return False


# ── Staging (start) ──────────────────────────────────────────────────────────


def _scratch_dir(settings: Settings) -> Path:
    base = settings.rfp_ingest_scratch_dir or tempfile.gettempdir()
    root = Path(base) / "rfp-split"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="rfp-split-", dir=root))


def _clean_scratch(scratch: Path | None) -> None:
    if scratch is None:
        return
    try:
        for leftover in scratch.iterdir():
            try:
                os.unlink(leftover)
            except OSError:
                pass
        scratch.rmdir()
    except OSError:
        pass


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _model_label(settings: Settings) -> str | None:
    try:
        return llm.active_model("bid_split", settings)
    except Exception:  # noqa: BLE001 - a label for history, never a gate
        return None


def _stage_row(
    sb, job_id: str, entry: dict, file_row: dict, decision, data: bytes, name: str,
    context: dict, settings: Settings, dest: Path,
) -> dict:
    """One bid_split_files row for a verified entry: a PDF goes the normal
    way (page count, `pages`); an office file is staged as the sandbox's
    converted PDF (`converted_pdf`, triage only); anything without a PDF to
    sample is staged as its own bytes and identified from its name
    (`name`). Returns the inserted row."""
    from app.services import rfp_create_files as rcf

    fmt = source_format_for(file_row, name)
    page_count: int | None = None
    error: str | None = None
    classified = CLASSIFIED_NAME
    upload_bytes = data
    content_type = decision.content_type
    key_name = name
    if fmt == protocol.SOURCE_FORMAT_PDF and content_type == "application/pdf":
        classified = CLASSIFIED_PAGES
        try:
            page_count = pdf_split.page_count(data)
            if page_count > settings.bid_split_max_pages_per_file:
                error = (
                    f"The PDF has {page_count} pages; the splitter limit is "
                    f"{settings.bid_split_max_pages_per_file} pages per file."
                )
        except ValueError as exc:
            error = str(exc)[:_ERROR_MAX_CHARS]
    elif fmt in _OFFICE_FORMATS:
        pdf_bytes: bytes | None = data if content_type == "application/pdf" else None
        if pdf_bytes is None:
            converted = rcf.converted_promotion(file_row, name, fmt)
            if isinstance(converted, rcf.Promote):
                fetched = rcf.fetch_verified(converted, dest, settings.upload_max_bytes)
                if not isinstance(fetched, rcf.Skip):
                    pdf_bytes = fetched
        if pdf_bytes is not None:
            classified = CLASSIFIED_CONVERTED
            upload_bytes = pdf_bytes
            content_type = "application/pdf"
            key_name = f"{_stem(name)}.pdf"
    path = storage.build_bid_split_source_path(job_id, key_name)
    storage.upload_file(path, upload_bytes, content_type)
    row = {
        "job_id": job_id,
        "filename": name,
        "storage_path": path,
        "size_bytes": len(data),
        "page_count": page_count,
        "status": "failed" if error else "pending",
        "error": error,
        "rfp_sandbox_file_id": file_row["id"],
        "source_format": fmt,
        "classified_from": classified,
    }
    if classified == CLASSIFIED_NAME:
        row["input_snapshot"] = {
            "name_context": {
                "filename": name,
                "path": entry.get("file_path") or entry.get("file_name"),
                "origin": entry.get("origin"),
                "provider": entry.get("provider"),
                "kind": entry.get("kind"),
                "discipline": entry.get("discipline"),
                **{k: v for k, v in (context or {}).items() if v},
            }
        }
    try:
        return sb.table(FILES_TABLE).insert(row).execute().data[0]
    except Exception:
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001
            logger.exception("rfp split: orphaned staged object %s", path)
        raise


def _discard_job(sb, job_id: str) -> None:
    """Best effort: the storage prefix, then the job row (files cascade). A
    queue run already enqueued for one of its rows ends as "This file no
    longer exists" and is marked failed by the queue; nothing waits on it."""
    try:
        storage.delete_bid_split_prefix(job_id)
    except Exception:  # noqa: BLE001
        logger.exception("rfp split: could not sweep the objects of discarded job %s", job_id)
    try:
        sb.table(JOBS_TABLE).delete().eq("id", job_id).execute()
    except Exception:  # noqa: BLE001
        logger.exception("rfp split: could not delete discarded job %s", job_id)


def _link_late_project(sb, harvest_id: str, job_id: str) -> None:
    """The late-link race (review 2026-09-30): creation found no row at
    `split` and made the project while a mate row was staging this job, so
    creation's own link (`rfp_create._attach_harvest`) may have read the
    harvest before it pointed here. Re-read the harvest AFTER pointing it at
    the job (creation writes its project_id before it re-reads the job, so
    one side always sees the other) and, when a project exists, link the
    job to it: each file that finishes then re-files the project
    (`resync_after_run`) and the settle writes the outcome back. Files that
    already finished before the link are re-filed now; a job that already
    settled is settled onto the project. Best effort."""
    try:
        rows = sb.table(HARVESTS_TABLE).select("id, project_id").eq("id", harvest_id).limit(1).execute().data or []
        project_id = (rows[0] if rows else {}).get("project_id")
        if not project_id:
            return
        linked = (
            sb.table(JOBS_TABLE).update({"project_id": project_id}).eq("id", job_id)
            .is_("project_id", "null").execute()
        ).data or []
        if not linked:
            return
        logger.warning("rfp split: job %s staged while project %s was created; linked late", job_id, project_id)
        sync_record(sb, str(project_id), SPLIT_RUNNING, job_id)
        for f in job_files(sb, job_id):
            if f.get("status") == "done":
                resync_after_run(str(f["id"]))
        job = _job(sb, job_id)
        if job is not None and job.get("status") != "processing":
            settle_linked_job(sb, job)
    except Exception:  # noqa: BLE001 - creation's own link and the settle cover most of it
        logger.exception("rfp split: late project link failed for job %s", job_id)


def _start(
    sb, harvest: dict, entries: list[dict], settings: Settings, context: dict,
    session_id: str | None, rfp_email_id: str | None, *,
    project_id: str | None = None, created_by: str | None = None,
    claim: _StagingClaim | None = None,
) -> tuple[str, int]:
    """Create the job, stage every entry the promotion decision admits (up to
    the per-job cap), then enqueue one bid_split run per pending row, and
    move the harvest to `running`. Returns (job_id, staged).

    Staging runs RFP_SPLIT_STAGE_CONCURRENCY entries at once (default 4;
    docs/RFP_SPLIT.md 10.5): each worker fetches one verified document,
    checks it, uploads it into the job and inserts its bid_split_files row
    (`_stage_one`). The email and portal sweeps stage inline, so 77 to 105
    documents one at a time held the whole intake for minutes. Kept exactly
    as the serial loop had it:

    - all or nothing: the first exception stops new work (entries not yet
      started are cancelled, the ones in flight finish), then the job is
      discarded with every staged object and the exception re-raised, so
      the caller's ladder applies;
    - the per-job cap, and one entry per sandbox file (`_entries`, first
      entry wins);
    - `staged_names` in entry order, whatever order the workers finish in;
    - nothing is queued until the WHOLE staging has finished: a staging
      exception on entry N discards the job, and runs already queued for
      entries 1..N-1 would each have ended "This file no longer exists"
      (some after spending model time; 114 such llm_jobs in one hour on
      dev, 2026-09-30). A row whose enqueue fails is marked failed with the
      staging sentence, as before;
    - the scratch directory is removed whatever happens.

    The coordinating thread (the sweep's, or the manual run's background
    task) waits on the workers in slices of the heartbeat interval and
    re-stamps the harvest's `pending` claim each time (`claim.beat`), so a
    live staging is never read as dead however long one 450 MB file takes,
    and a dead one (the server restarted) ages out within
    RFP_SPLIT_STAGING_STALE_SECONDS. A claim that was taken over
    (`StagingClaimLost`) stops the staging like any other exception.

    Memory: each worker holds one document's bytes (and, for an office
    file, its converted PDF) only while it is being checked and uploaded;
    nothing is kept once its row is inserted. The peak is about
    concurrency x the largest documents in flight (4 x 450 MB worst case at
    the default settings; typical drawing sets are 20 to 100 MB).

    `page_count` (in `_stage_row`) reads the PDF with pypdf, one reader per
    call, and never touches PDFium; the renderer's `_PDFIUM_LOCK` stays the
    only PDFium entry point, so the workers need no extra lock.

    `project_id` / `created_by` are the manual run's (docs/RFP_SPLIT.md 10):
    the job is born linked to the project, so every file that finishes
    re-files the project's documents through `resync_after_run`, and the
    person who pressed "Run the splitter" gets the splitter's finished bell.
    The pipeline passes neither (the project does not exist yet).

    `claim` is the caller's `_StagingClaim`; without one (a direct call)
    the claim is the harvest's current stamp."""
    from app.services import bid_split
    from app.services import rfp_create_files as rcf

    claim = claim or _StagingClaim(sb, harvest["id"], harvest.get("split_started_at"), settings)
    job = (
        sb.table(JOBS_TABLE)
        .insert({
            "status": "processing",
            "file_count": 0,
            "model": _model_label(settings),
            "created_by": created_by,
            "source": SOURCE_RFP,
            "rfp_harvest_id": harvest["id"],
            **({"project_id": project_id} if project_id else {}),
        })
        .execute()
    ).data[0]
    job_id = str(job["id"])
    cap = max(1, int(settings.bid_split_max_files_per_job))
    file_rows = rcf._file_rows(sb, [str(e["sandbox_file_id"]) for e in entries])
    capped = entries[:cap]
    # One slot per entry, filled in entry order however the workers finish:
    # (its staged_names entry, the inserted row or None).
    slots: list[tuple[dict, dict | None] | None] = [None] * len(capped)
    scratch: Path | None = None
    try:
        scratch = _scratch_dir(settings)
        work: list[tuple[int, dict, dict | None, str, Any]] = []
        for i, entry in enumerate(capped):
            file_row = file_rows.get(str(entry.get("sandbox_file_id") or ""))
            name = rcf.entry_name(entry, file_row)
            decision = rcf.promotion_for(entry, file_row)
            if isinstance(decision, rcf.Skip):
                slots[i] = ({"filename": name, "staged": False, "reason": decision.reason}, None)
                continue
            work.append((i, entry, file_row, name, decision))
        _stage_all(sb, job_id, work, slots, scratch, context, settings, claim)
        # The last word before anything is queued: a claim taken over while
        # the last files landed must not leave runs queued for a job the
        # harvest will never point at.
        if not claim.beat(force=True):
            raise StagingClaimLost(_MSG_CLAIM_LOST)
    except Exception:
        # Staging died part way (storage, the sandbox bucket, a lost
        # claim): the job row would otherwise sit at `processing` forever
        # with the staged objects (the app shell's in-flight marker polls
        # for processing jobs). The claim goes back to `none` in `advance`,
        # so the next pass stages a fresh job; this one is discarded now.
        # Every worker has finished by here (`_stage_all` joins them), so
        # no row lands after the discard.
        _discard_job(sb, job_id)
        raise
    finally:
        _clean_scratch(scratch)
    staged_names: list[dict] = []
    to_queue: list[tuple[dict, dict]] = []   # (row, its staged_names entry), queued after the loop
    staged = 0
    for slot in slots:
        if slot is None:
            continue
        noted, row = slot
        staged_names.append(noted)
        if row is None:
            continue
        staged += 1
        if row.get("status") == "pending":
            to_queue.append((row, noted))
    for row, noted in to_queue:
        if not claim.beat():
            # Taken over mid-enqueue: the harvest will never point at this
            # job, so its runs would spend model time on a split nobody
            # files. Discard it (a run already queued ends "This file no
            # longer exists"); the new owner stages its own.
            _discard_job(sb, job_id)
            raise StagingClaimLost(_MSG_CLAIM_LOST)
        try:
            llm_queue.enqueue(
                llm_queue.JOB_BID_SPLIT, target_id=str(row["id"]),
                payload={"file_id": str(row["id"])}, created_by=created_by, settings=settings,
            )
        except Exception as exc:  # noqa: BLE001 - the row fails; promotion falls back
            logger.exception("rfp split: enqueue failed for staged file %s", row.get("id"))
            sb.table(FILES_TABLE).update({
                "status": "failed", "error": _MSG_STAGING_FAILED, "finished_at": _iso(_now()),
            }).eq("id", row["id"]).execute()
            noted["error"] = redact_text(exc)[:200]
    if staged == 0:
        # Keep the job: terminal, zero files. Deleting it hid WHY nothing
        # was staged; the bench event below carries every refusal.
        sb.table(JOBS_TABLE).update({
            "status": "failed", "file_count": 0, "completed_at": _iso(_now()),
        }).eq("id", job_id).execute()
        refused = [n for n in staged_names if not n.get("staged")]
        reasons = sorted({str(n.get("reason") or "unknown") for n in refused})
        _record(
            sb, session_id, "skipped",
            f"Split skipped: no document passed the sandbox checks ({', '.join(reasons)[:160]})",
            level=rfp_test.LEVEL_WARN,
            detail={"harvest_id": harvest["id"], "job_id": job_id, "reason": SKIP_NO_FILES,
                    "why": _MSG_NONE_STAGED, "files": staged_names[:250],
                    "over_cap": max(0, len(entries) - cap)},
            harvest_id=harvest["id"], rfp_email_id=rfp_email_id,
        )
        return job_id, 0
    sb.table(JOBS_TABLE).update({"file_count": staged}).eq("id", job_id).execute()
    # A row that failed at staging (unreadable PDF, over the page cap) never
    # runs; derive the aggregate once so the job cannot sit at processing
    # when nothing is queued.
    try:
        bid_split.refresh_job(job_id)
    except Exception:  # noqa: BLE001 - the queue's marks refresh it again
        logger.exception("rfp split: refresh_job failed for %s", job_id)
    if not claim.cas({
        "split_status": SPLIT_RUNNING, "split_job_id": job_id, "split_started_at": _iso(_now()),
        "split_error": None,
    }):
        logger.warning(
            "rfp split: harvest %s moved during the enqueue of job %s; the harvest was not pointed at it",
            harvest["id"], job_id,
        )
        _discard_job(sb, job_id)
        raise StagingClaimLost(_MSG_CLAIM_LOST)
    if not project_id:
        _link_late_project(sb, harvest["id"], job_id)
    _record(
        sb, session_id, "started", f"Split started: {staged} document{'s' if staged != 1 else ''} staged",
        detail={"harvest_id": harvest["id"], "job_id": job_id, "files": staged_names[:250],
                "over_cap": max(0, len(entries) - cap)},
        harvest_id=harvest["id"], rfp_email_id=rfp_email_id,
    )
    return job_id, staged


def _stage_one(
    sb, job_id: str, i: int, entry: dict, file_row: dict | None, name: str, decision,
    scratch: Path, context: dict, settings: Settings, abort: threading.Event,
) -> tuple[dict, dict | None] | None:
    """One worker's entry: fetch the verified bytes, check them, upload them
    into the job and insert the row. `(staged_names entry, row or None)`,
    or None when the staging was aborted before this entry started. The
    bytes are dropped when this returns."""
    from app.services import rfp_create_files as rcf

    if abort.is_set():
        return None
    dest = scratch / f"{i}-{uuid.uuid4().hex}"
    fetched = rcf.fetch_entry(decision, file_row, name, dest, settings.upload_max_bytes)
    if isinstance(fetched, rcf.Skip):
        return {"filename": name, "staged": False, "reason": fetched.reason}, None
    decision, data = fetched
    row = _stage_row(sb, job_id, entry, file_row, decision, data, name, context, settings, dest)
    del data
    return {
        "filename": name, "staged": True, "file_id": row["id"],
        "classified_from": row.get("classified_from"), "source_format": row.get("source_format"),
        "error": row.get("error"),
    }, row


def _stage_all(
    sb, job_id: str, work: list[tuple], slots: list, scratch: Path, context: dict,
    settings: Settings, claim: _StagingClaim,
) -> None:
    """Run `_stage_one` over `work` with bounded concurrency, filling
    `slots[i]`. Waits in slices of the heartbeat interval and beats the
    claim after each slice (a file finishing, or the interval passing), so
    the stamp moves while the loop is alive. On the first failure (a
    worker's exception, or the claim taken over) no further entry starts;
    the ones in flight are joined before the failure is raised."""
    if not work:
        return
    workers = max(1, min(int(getattr(settings, "rfp_split_stage_concurrency", 4) or 1), len(work)))
    abort = threading.Event()
    failure: BaseException | None = None
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rfp-split-stage") as pool:
        futures = {
            pool.submit(_stage_one, sb, job_id, i, entry, file_row, name, decision, scratch, context,
                        settings, abort): i
            for i, entry, file_row, name, decision in work
        }
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=claim.interval, return_when=FIRST_COMPLETED)
            for future in done:
                if future.cancelled():
                    continue
                exc = future.exception()
                if exc is not None:
                    failure = failure or exc
                    continue
                result = future.result()
                if result is not None:
                    slots[futures[future]] = result
            # Beat while draining too: the workers still in flight after a
            # failure can take minutes (a 450 MB upload and its retries), and
            # a claim left to age out meanwhile would be reclaimed and the
            # job discarded under them (review 2026-09-30).
            if not claim.beat() and failure is None:
                failure = StagingClaimLost(_MSG_CLAIM_LOST)
            if failure is not None and not abort.is_set():
                abort.set()
                for future in pending:
                    future.cancel()
    # The `with` block has joined every worker.
    if failure is not None:
        raise failure


# ── Polling (check) ──────────────────────────────────────────────────────────


def _job(sb, job_id: str | None) -> dict | None:
    if not job_id:
        return None
    rows = sb.table(JOBS_TABLE).select("*").eq("id", job_id).limit(1).execute().data or []
    return rows[0] if rows else None


def job_files(sb, job_id: str) -> list[dict]:
    return (
        sb.table(FILES_TABLE).select(_FILE_SELECT).eq("job_id", job_id).order("created_at").execute()
    ).data or []


def segments_for(sb, file_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    clean = [str(i) for i in file_ids if i]
    for i in range(0, len(clean), _IN_CHUNK):
        rows = (
            sb.table(SEGMENTS_TABLE).select(_SEGMENT_SELECT).in_("file_id", clean[i:i + _IN_CHUNK])
            .order("sort_order").execute()
        ).data or []
        for row in rows:
            out.setdefault(str(row["file_id"]), []).append(row)
    return out


def _check(
    sb, harvest: dict, session_id: str | None, rfp_email_id: str | None, *, settings: Settings | None = None,
) -> Outcome:
    job = _job(sb, harvest.get("split_job_id"))
    now = _iso(_now())
    if job is None:
        _cas_split(sb, harvest["id"], SPLIT_RUNNING, {
            "split_status": SPLIT_FAILED, "split_error": _MSG_JOB_MISSING, "split_finished_at": now,
        })
        _record(sb, session_id, "finished", "Split failed: the job is missing", level=rfp_test.LEVEL_ERROR,
                detail={"harvest_id": harvest["id"], "job_id": harvest.get("split_job_id"), "status": "failed"},
                harvest_id=harvest["id"], rfp_email_id=rfp_email_id)
        return Outcome(SPLIT_FAILED, reason=_MSG_JOB_MISSING, job_id=harvest.get("split_job_id"))
    if job.get("status") == "processing":
        # A job nothing will ever finish (docs/RFP_SPLIT.md 10.5): its
        # stranded files are marked failed "interrupted" and the job
        # settles, so the row moves on and the project's flag offers "Run
        # the splitter". Checked only once the job itself has sat untouched
        # for the stale window (every file transition touches it).
        if not _untouched(job, settings) or not _reap_if_dead(sb, harvest, job, settings):
            return Outcome("waiting", job_id=str(job["id"]))
        job = _job(sb, str(job["id"])) or job
        if job.get("status") == "processing":
            return Outcome("waiting", job_id=str(job["id"]))
    files = job_files(sb, str(job["id"]))
    failed = [f for f in files if f.get("status") == "failed"]
    outage = _outage_failures(sb, failed)
    if outage:
        away = model_away()
        if away:
            return _wait_for_model(sb, harvest, away, session_id, rfp_email_id, job_id=str(job["id"]))
        return _requeue_after_outage(sb, harvest, str(job["id"]), outage, session_id, rfp_email_id)
    segs = segments_for(sb, [str(f["id"]) for f in files])
    done = [f for f in files if f.get("status") == "done"]
    total_segments = sum(len(v) for v in segs.values())
    if session_id:
        for f in files:
            level = rfp_test.LEVEL_WARN if f.get("status") == "failed" else rfp_test.LEVEL_INFO
            _record(
                sb, session_id, "file",
                f"Split {f.get('status')}: {f.get('filename')}" + (f" ({f.get('error')})" if f.get("error") else ""),
                level=level,
                detail={"job_id": str(job["id"]), "file_id": f["id"], "filename": f.get("filename"),
                        "status": f.get("status"), "kind": f.get("file_kind"),
                        "segments": len(segs.get(str(f["id"]), [])),
                        "classified_from": f.get("classified_from"), "source_format": f.get("source_format"),
                        "error": f.get("error")},
                harvest_id=harvest["id"], rfp_email_id=rfp_email_id,
            )
    if job.get("status") in ("done", "done_with_errors"):
        _cas_split(sb, harvest["id"], SPLIT_RUNNING, {
            "split_status": SPLIT_COMPLETE, "split_error": None, "split_finished_at": now,
        })
        outcome = Outcome(SPLIT_COMPLETE, job_id=str(job["id"]))
    else:
        reason = failure_reason(files)
        _cas_split(sb, harvest["id"], SPLIT_RUNNING, {
            "split_status": SPLIT_FAILED, "split_error": reason, "split_finished_at": now,
        })
        outcome = Outcome(SPLIT_FAILED, reason=reason, job_id=str(job["id"]))
    _record(
        sb, session_id, "finished",
        f"Split {outcome.state}: {len(done)} of {len(files)} files, {total_segments} segments",
        level=rfp_test.LEVEL_ERROR if outcome.state == SPLIT_FAILED else (
            rfp_test.LEVEL_WARN if failed else rfp_test.LEVEL_INFO),
        detail={"harvest_id": harvest["id"], "job_id": str(job["id"]), "status": outcome.state,
                "files_done": len(done), "files_failed": len(failed), "segments": total_segments},
        harvest_id=harvest["id"], rfp_email_id=rfp_email_id,
    )
    return outcome


def _outage_failures(sb, failed: list[dict]) -> list[dict]:
    """The failed files whose LATEST queue run died of the model being away
    (an outage kind on the llm_jobs row), or that a server restart stranded
    (`_reap_dead_job`'s interrupted sentence: the pipeline re-queues them on
    its own instead of waiting for a person, review 2026-09-30), and that
    have run fewer than `_OUTAGE_RUNS_MAX` times. Anything else failed for
    real; past the cap an interrupted file stays failed and the project
    carries the flag ("Run the splitter")."""
    if not failed:
        return []
    ids = [str(f["id"]) for f in failed]
    runs: dict[str, list[dict]] = {}
    for i in range(0, len(ids), _IN_CHUNK):
        rows = (
            sb.table("llm_jobs").select("target_id, status, error_kind, created_at")
            .eq("job_type", llm_queue.JOB_BID_SPLIT).in_("target_id", ids[i:i + _IN_CHUNK])
            .order("created_at", desc=True).execute()
        ).data or []
        for r in rows:
            runs.setdefault(str(r["target_id"]), []).append(r)
    out = []
    for f in failed:
        history = runs.get(str(f["id"])) or []
        latest = history[0] if history else None
        if f.get("error") == _MSG_JOB_INTERRUPTED:
            if len(history) < _OUTAGE_RUNS_MAX:
                out.append(f)
            continue
        if latest is None or latest.get("status") != "failed":
            continue
        if latest.get("error_kind") in _OUTAGE_KINDS and len(history) < _OUTAGE_RUNS_MAX:
            out.append(f)
    return out


# ── Dead jobs and orphans (docs/RFP_SPLIT.md 10.5) ───────────────────────────
#
# A server restart can strand a split job at `processing` with nothing that
# will ever move it: a staging that died before it queued anything (the job
# is not yet the harvest's; an "orphan"), or a restart between a file's
# `pending` mark and its enqueue (the outage requeue, a manual run's
# requeue). A job is DEAD when it is `processing`, it has sat untouched for
# RFP_SPLIT_STAGING_STALE_SECONDS (every file transition touches it through
# bid_split.refresh_job), every non-terminal file has sat untouched as long,
# no llm_jobs run for any of them is queued or running, and no live staging
# can be feeding it (its harvest is not at a fresh `pending`). A live but
# slow job never matches: a file that is running has an active queue run.
# Missing timestamps read as fresh (never dead on a guess).

_NON_TERMINAL_FILE = ("pending", "running")


def _untouched(row: dict | None, settings: Settings | None, now: datetime | None = None) -> bool:
    """The row's `updated_at` (else `created_at`) is older than the stale
    window. A row without either stamp is NOT untouched (never dead on a
    guess)."""
    at = _parse_ts((row or {}).get("updated_at")) or _parse_ts((row or {}).get("created_at"))
    if at is None:
        return False
    return at < (now or _now()) - timedelta(seconds=stale_seconds(settings))


def _active_run_targets(sb, file_ids: list[str]) -> set[str]:
    """The file ids with a bid_split queue run still queued or running."""
    out: set[str] = set()
    clean = [str(i) for i in file_ids if i]
    for i in range(0, len(clean), _IN_CHUNK):
        rows = (
            sb.table("llm_jobs").select("target_id, status")
            .eq("job_type", llm_queue.JOB_BID_SPLIT).in_("target_id", clean[i:i + _IN_CHUNK])
            .in_("status", ["queued", "running"]).execute()
        ).data or []
        out.update(str(r["target_id"]) for r in rows)
    return out


def dead_job_ids(
    sb, jobs: list[dict], files_by_job: dict[str, list[dict]], harvests_by_job: dict[str, dict | None],
    *, settings: Settings | None = None, now: datetime | None = None,
) -> set[str]:
    """Which of `jobs` are dead (the rule above). One llm_jobs query per
    chunk of candidate files, and only for jobs that already look stranded
    (processing, untouched, their open files untouched)."""
    candidates: dict[str, list[str]] = {}
    for job in jobs:
        jid = str(job.get("id") or "")
        if not jid or job.get("status") != "processing" or not _untouched(job, settings, now):
            continue
        harvest = harvests_by_job.get(jid)
        if harvest and harvest.get("split_status") == SPLIT_PENDING and not pending_is_stale(harvest, now, settings):
            continue   # a live staging may be feeding it
        open_files = [f for f in files_by_job.get(jid, []) if f.get("status") in _NON_TERMINAL_FILE]
        if any(not _untouched(f, settings, now) for f in open_files):
            continue
        candidates[jid] = [str(f["id"]) for f in open_files]
    if not candidates:
        return set()
    active = _active_run_targets(sb, [fid for ids in candidates.values() for fid in ids])
    return {jid for jid, ids in candidates.items() if not any(fid in active for fid in ids)}


def _reap_dead_job(sb, job_id: str) -> int:
    """Mark a dead job's stranded files failed with the interrupted sentence
    (fenced on their open status) and let the job settle through the normal
    aggregate: `refresh_job` grades it, and a job serving a project writes
    its harvest back (`settle_linked_job`). A job with no files at all is
    failed directly. Returns how many files were marked."""
    from app.services import bid_split

    now = _iso(_now())
    marked = (
        sb.table(FILES_TABLE).update({"status": "failed", "error": _MSG_JOB_INTERRUPTED, "finished_at": now})
        .eq("job_id", job_id).in_("status", list(_NON_TERMINAL_FILE)).execute()
    ).data or []
    if not job_files(sb, job_id):
        sb.table(JOBS_TABLE).update({"status": "failed", "completed_at": now}).eq("id", job_id).eq(
            "status", "processing").execute()
        return 0
    try:
        bid_split.refresh_job(job_id)
    except Exception:  # noqa: BLE001 - the next poll grades it again
        logger.exception("rfp split: refresh_job failed while reaping %s", job_id)
    logger.warning("rfp split: job %s was dead (nothing queued or running); %s file(s) marked interrupted",
                   job_id, len(marked))
    return len(marked)


def _reap_if_dead(sb, harvest: dict | None, job: dict, settings: Settings | None) -> bool:
    """`_reap_dead_job` when `dead_job_ids` says so; True when it reaped."""
    jid = str(job["id"])
    if jid not in dead_job_ids(sb, [job], {jid: job_files(sb, jid)}, {jid: harvest}, settings=settings):
        return False
    _reap_dead_job(sb, jid)
    return True


def _discard_orphans(sb, harvest: dict, settings: Settings | None) -> int:
    """Discard the `processing` rfp jobs of this harvest that it does not
    point at and that nothing is running for: what a staging killed before
    it queued anything leaves behind. Called only by the worker that just
    took (or re-took) the harvest's staging claim, so no live staging can be
    feeding one of them. A job with any queue run still active is left
    alone (its runs settle it). Best effort. Returns how many went."""
    try:
        rows = (
            sb.table(JOBS_TABLE).select("id, status").eq("rfp_harvest_id", harvest["id"])
            .eq("status", "processing").execute()
        ).data or []
        orphans = [r for r in rows if str(r["id"]) != str(harvest.get("split_job_id") or "")]
        gone = 0
        for job in orphans:
            ids = [str(f["id"]) for f in job_files(sb, str(job["id"]))]
            if ids and _active_run_targets(sb, ids):
                continue
            _discard_job(sb, str(job["id"]))
            gone += 1
        if gone:
            logger.warning("rfp split: discarded %s orphaned staging job(s) of harvest %s", gone, harvest["id"])
        return gone
    except Exception:  # noqa: BLE001 - clutter, never a gate
        logger.exception("rfp split: orphan sweep failed for harvest %s", harvest.get("id"))
        return 0


def _requeue_after_outage(
    sb, harvest: dict, job_id: str, files: list[dict], session_id: str | None, rfp_email_id: str | None,
) -> Outcome:
    """The model is back: the files that died of the outage go pending and
    are queued again; the job returns to processing through the mark, so the
    next poll sees it running. A file whose enqueue fails stays failed (its
    real failure is graded on the next poll)."""
    from app.services import bid_split

    settings = get_settings()
    requeued = 0
    for f in files:
        try:
            bid_split._mark(str(f["id"]), status="pending", error=None)
            llm_queue.enqueue(
                llm_queue.JOB_BID_SPLIT, target_id=str(f["id"]), payload={"file_id": str(f["id"])},
                created_by=None, settings=settings,
            )
            requeued += 1
        except Exception:  # noqa: BLE001 - the file keeps its failure; the next poll grades it
            logger.exception("rfp split: requeue after outage failed for file %s", f.get("id"))
            try:
                bid_split._mark(str(f["id"]), status="failed", error=f.get("error"))
            except Exception:  # noqa: BLE001
                logger.exception("rfp split: could not restore the failed mark on %s", f.get("id"))
    _record(
        sb, session_id, "requeued",
        f"Split resumed: the model is back, {requeued} of {len(files)} document{'s' if len(files) != 1 else ''} queued again",
        level=rfp_test.LEVEL_INFO if requeued else rfp_test.LEVEL_WARN,
        detail={"harvest_id": harvest["id"], "job_id": job_id, "requeued": requeued,
                "files": [{"file_id": str(f["id"]), "filename": f.get("filename")} for f in files][:250]},
        harvest_id=harvest["id"], rfp_email_id=rfp_email_id,
    )
    return Outcome("waiting", job_id=job_id)


# ── Promotion (section 4) ────────────────────────────────────────────────────


@dataclass
class PromoteResult:
    used_split: bool = False       # False: the caller promotes through the pre-split mapping
    documents: int = 0             # rows present for the entry after the call (segments, or the intact row)
    drawings: int = 0              # of those, drawings inserted by THIS call
    inserted: int = 0
    replaced: int = 0              # stale rows removed
    skipped: list[dict] = field(default_factory=list)


def split_rows_for_job(sb, job_id: str | None) -> dict[str, tuple[dict, list[dict]]]:
    """`{rfp_sandbox_file_id: (bid_split_files row, its segments)}` for the
    job, or an empty map without a job."""
    if not job_id:
        return {}
    files = job_files(sb, str(job_id))
    segs = segments_for(sb, [str(f["id"]) for f in files])
    return {
        str(f["rfp_sandbox_file_id"]): (f, segs.get(str(f["id"]), []))
        for f in files if f.get("rfp_sandbox_file_id")
    }


def project_rows(sb, project_id: str) -> list[dict]:
    """Every project_files row promotion or a resync may own: promoted from
    the sandbox or from the splitter."""
    seen: dict[str, dict] = {}
    for column in ("rfp_sandbox_file_id", "bid_split_file_id"):
        rows = (
            sb.table(PROJECT_FILES_TABLE).select(_PROJECT_ROW_SELECT).eq("project_id", project_id)
            .not_.is_(column, "null").execute()
        ).data or []
        for row in rows:
            seen.setdefault(str(row["id"]), row)
    return list(seen.values())


def rows_for_file(rows: list[dict], split_file: dict) -> list[dict]:
    fid = str(split_file.get("id"))
    sid = str(split_file.get("rfp_sandbox_file_id") or "")
    return [
        r for r in rows
        if str(r.get("bid_split_file_id") or "") == fid or (sid and str(r.get("rfp_sandbox_file_id") or "") == sid)
    ]


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return "23505" in text or "duplicate key" in text


def _delete_row(sb, row: dict) -> None:
    """A project_files row this file no longer stands behind: the object,
    the preview derivative (best effort) and the row."""
    from app.services import office_preview

    for path in {row.get("storage_path"), row.get("preview_path"),
                 office_preview.preview_object_path(str(row["project_id"]), str(row["id"]))}:
        if not path:
            continue
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001 - an orphan must never block the re-file
            logger.warning("rfp split: could not delete %s", path)
    sb.table(PROJECT_FILES_TABLE).delete().eq("id", row["id"]).execute()


def _insert_row(sb, row: dict, key: str) -> dict | None:
    """Insert, reading the unique race as "already there" (the fresh object
    is deleted). Storage trouble propagates. A new drawing or specification
    row clears the project's files flag (docs/RFP_BUILDINGCONNECTED.md 3.8),
    best effort inside that service."""
    try:
        inserted = sb.table(PROJECT_FILES_TABLE).insert(row).execute().data[0]
    except Exception as exc:  # noqa: BLE001
        try:
            storage.delete_file(key)
        except Exception:  # noqa: BLE001
            logger.exception("rfp split: orphaned object %s", key)
        if _is_unique_violation(exc):
            return None
        raise
    files_needed.clear_if_satisfied(sb, row.get("project_id"), row.get("category"), None)
    return inserted


def _base_row(project_id: str, harvest_id: str | None, split_file: dict, key: str, filename: str,
              content_type: str, size: int, note: str | None) -> dict:
    return {
        "project_id": project_id,
        "storage_path": key,
        "filename": filename,
        "material_category_id": None,
        "uploaded_by": None,
        "mime_type": content_type,
        "size_bytes": size,
        "preview_status": "none",
        "note": (note or None) and note[:_NOTE_MAX_CHARS],
        "estimator_deliverable": False,
        "rfp_harvest_id": harvest_id,
        "rfp_sandbox_file_id": None,
        "bid_split_segment_id": None,
        "bid_split_file_id": split_file["id"],
        "is_source_set": False,
    }


def _promote_segment(sb, project_id: str, harvest_id: str | None, split_file: dict, seg: dict) -> dict | None:
    """One cut segment into the project: a server-side copy of the split
    PDF under the mapped category, filename = segment name + .pdf."""
    category = file_category_for_segment(seg)
    filename = f"{(seg.get('name') or 'Document').strip()[:180]}.pdf"
    key = storage.build_object_path(project_id, category, filename)
    storage.copy_file(seg["storage_path"], key)
    row = _base_row(project_id, harvest_id, split_file, key, filename, "application/pdf",
                    int(seg.get("size_bytes") or 0), seg.get("description"))
    row.update(category_fields(category, seg))
    row["bid_split_segment_id"] = seg["id"]
    inserted = _insert_row(sb, row, key)
    if inserted is not None:
        audit(None, "file.upload", "project_file", inserted.get("id"),
              {"category": category, "source": "rfp", "split": True})
    return inserted


def _upload_original(
    sb, project_id: str, harvest_id: str | None, split_file: dict, decision, data: bytes, *,
    category: str, seg: dict | None, note: str | None, is_source_set: bool,
) -> dict | None:
    """The verified original bytes (the intact file, or the kept source
    set) as one row carrying the sandbox id."""
    from app.services import office_preview

    key = storage.build_object_path(project_id, category, decision.filename)
    storage.upload_file(key, data, decision.content_type)
    convertible = office_preview.is_convertible(decision.filename, category)
    row = _base_row(project_id, harvest_id, split_file, key, decision.filename, decision.content_type,
                    len(data), note)
    row.update(category_fields(category, seg, decision.filename))
    row["preview_status"] = "pending" if convertible else "none"
    row["rfp_sandbox_file_id"] = split_file.get("rfp_sandbox_file_id")
    row["bid_split_segment_id"] = seg["id"] if seg else None
    row["is_source_set"] = bool(is_source_set)
    inserted = _insert_row(sb, row, key)
    if inserted is None:
        return None
    audit(None, "file.upload", "project_file", inserted.get("id"),
          {"category": category, "source": "rfp", "source_set": bool(is_source_set)})
    if convertible:
        try:
            office_preview.generate_preview(inserted["id"])
        except Exception:  # noqa: BLE001 - a preview is a convenience
            logger.exception("rfp split: preview failed for %s", inserted.get("id"))
    return inserted


def promote_split_file(
    sb, *, project_id: str, harvest_id: str | None, split_file: dict, segments: list[dict],
    existing: list[dict], fetch_original: Callable[[], Any], name: str,
) -> PromoteResult:
    """Reconcile one harvest entry's splitter outcome into project_files
    (section 4), idempotently:

    - split (non-original segments): every current segment has a row
      (copied server-side; a row already there keeps its object and takes
      the category if it changed), rows of segments that no longer exist
      go, and the source PDF is kept once as `other` / `is_source_set`
      (a row that carried the sandbox id from an earlier intact promotion
      becomes the source set in place);
    - intact (one `is_original` segment): the sandbox id's row takes the
      mapped category and the segment id, or is uploaded from the verified
      bytes; any segment rows from an earlier cut go.

    `fetch_original()` yields `(Promote, bytes)` or a `Skip` for the
    verified original, called only when a row must be uploaded. A file that
    is not `done` or has no segments returns `used_split = False`: the
    caller promotes through the pre-split mapping."""
    result = PromoteResult()
    segs = sorted([s for s in segments if isinstance(s, dict)], key=lambda s: int(s.get("sort_order") or 0))
    if split_file.get("status") != "done" or not segs:
        return result
    result.used_split = True
    sid = str(split_file.get("rfp_sandbox_file_id") or "")
    by_seg = {str(r.get("bid_split_segment_id")): r for r in existing if r.get("bid_split_segment_id")}
    current_ids = {str(s["id"]) for s in segs}
    sandbox_rows = [r for r in existing if sid and str(r.get("rfp_sandbox_file_id") or "") == sid]

    if is_split(segs):
        for seg in segs:
            category = file_category_for_segment(seg)
            row = by_seg.get(str(seg["id"]))
            if row is not None:
                if row.get("category") != category:
                    sb.table(PROJECT_FILES_TABLE).update(category_fields(category, seg)).eq("id", row["id"]).execute()
                result.documents += 1
                continue
            inserted = _promote_segment(sb, project_id, harvest_id, split_file, seg)
            result.documents += 1
            if inserted is not None:
                result.inserted += 1
                if category in DRAWING_FILE_CATEGORIES:
                    result.drawings += 1
        for row in existing:
            if row in sandbox_rows:
                continue
            if str(row.get("bid_split_segment_id") or "") not in current_ids:
                _delete_row(sb, row)
                result.replaced += 1
        note = SOURCE_SET_NOTE.format(n=len(segs))
        if sandbox_rows:
            src = sandbox_rows[0]
            if not src.get("is_source_set") or src.get("note") != note or src.get("category") != CATEGORY_OTHER:
                sb.table(PROJECT_FILES_TABLE).update({
                    **category_fields(CATEGORY_OTHER, None), "is_source_set": True, "note": note,
                    "bid_split_segment_id": None, "bid_split_file_id": split_file["id"],
                }).eq("id", src["id"]).execute()
                if not src.get("is_source_set"):
                    result.replaced += 1
        else:
            fetched = fetch_original()
            if isinstance(fetched, tuple):
                decision, data = fetched
                inserted = _upload_original(sb, project_id, harvest_id, split_file, decision, data,
                                            category=CATEGORY_OTHER, seg=None, note=note, is_source_set=True)
                if inserted is None:
                    # The unique index on the sandbox id: a whole-file row
                    # landed after `existing` was read (the promotion job
                    # running beside this re-file). It becomes the source set
                    # in place, never a whole file left in its trade
                    # category beside the new segments.
                    _adopt_sandbox_row(sb, project_id, sid, {
                        **category_fields(CATEGORY_OTHER, None), "is_source_set": True, "note": note,
                        "bid_split_segment_id": None, "bid_split_file_id": split_file["id"],
                    })
            else:
                result.skipped.append({"file_path": name, "reason": f"source_set:{getattr(fetched, 'reason', 'unknown')}"})
        return result

    seg = segs[0]
    category = file_category_for_segment(seg)
    intact = sandbox_rows[0] if sandbox_rows else None
    for row in existing:
        if intact is not None and row["id"] == intact["id"]:
            continue
        _delete_row(sb, row)
        result.replaced += 1
    if intact is not None:
        fields = {
            **category_fields(category, seg, intact.get("filename")), "is_source_set": False,
            "bid_split_segment_id": seg["id"], "bid_split_file_id": split_file["id"],
        }
        if intact.get("is_source_set"):
            fields["note"] = None
        changed = any(intact.get(k) != v for k, v in fields.items())
        if changed:
            sb.table(PROJECT_FILES_TABLE).update(fields).eq("id", intact["id"]).execute()
            if intact.get("category") != category and category in DRAWING_FILE_CATEGORIES:
                result.drawings += 1
        result.documents += 1
        return result
    fetched = fetch_original()
    if not isinstance(fetched, tuple):
        result.skipped.append({"file_path": name, "reason": getattr(fetched, "reason", "unknown")})
        return result
    decision, data = fetched
    inserted = _upload_original(sb, project_id, harvest_id, split_file, decision, data,
                                category=category, seg=seg, note=decision.note, is_source_set=False)
    result.documents += 1
    if inserted is None:
        # Same race as the source set above: the promotion's whole row won
        # the unique index; it takes the splitter's category in place.
        _adopt_sandbox_row(sb, project_id, sid, {
            **category_fields(category, seg, decision.filename), "is_source_set": False,
            "bid_split_segment_id": seg["id"], "bid_split_file_id": split_file["id"],
        })
    if inserted is not None:
        result.inserted += 1
        if category in DRAWING_FILE_CATEGORIES:
            result.drawings += 1
    return result


def _adopt_sandbox_row(sb, project_id: str, sandbox_file_id: str, fields: dict) -> None:
    """The project's row for this sandbox file takes `fields` (the re-file
    lost the unique race on (project_id, rfp_sandbox_file_id) to a row the
    promotion job inserted meanwhile). No-op without a sandbox id."""
    if not sandbox_file_id:
        return
    sb.table(PROJECT_FILES_TABLE).update(fields).eq("project_id", project_id).eq(
        "rfp_sandbox_file_id", sandbox_file_id).execute()


def count_documents(sb, project_id: str) -> int:
    """`files_promoted` after a promotion or a resync: every promoted row
    that is a document (segments and intact files; the source set is not
    one)."""
    return sum(1 for r in project_rows(sb, project_id) if not r.get("is_source_set"))


# ── Corrections after creation (section 3.3 / 6) ─────────────────────────────


def job_for_file(sb, file_id: str) -> tuple[dict | None, dict | None]:
    rows = sb.table(FILES_TABLE).select(_FILE_SELECT).eq("id", file_id).limit(1).execute().data or []
    if not rows:
        return None, None
    return rows[0], _job(sb, rows[0].get("job_id"))


def resync_project_files(sb, file_id: str, *, actor_id: str | None = None) -> PromoteResult | None:
    """After a correction in the splitter (or a re-run) on a file whose job
    has a project: re-file that project's rows for the file through
    `promote_split_file`. None when the job has no project, the record is
    gone, or the file is not `done`. Audits `rfp_split.resync` on the
    project and refreshes the created record's `files_promoted`."""
    from app.services import rfp_create_files as rcf

    split_file, job = job_for_file(sb, file_id)
    if split_file is None or job is None or not job.get("project_id"):
        return None
    project_id = str(job["project_id"])
    records = sb.table(CREATED_TABLE).select("project_id, harvest_id").eq("project_id", project_id).limit(1).execute().data or []
    record = records[0] if records else {}
    harvest_id = record.get("harvest_id") or job.get("rfp_harvest_id")
    harvest_rows = sb.table(HARVESTS_TABLE).select("id, files").eq("id", harvest_id).limit(1).execute().data if harvest_id else []
    harvest = harvest_rows[0] if harvest_rows else {}
    sid = str(split_file.get("rfp_sandbox_file_id") or "")
    entry = next(
        (e for e in (harvest.get("files") or []) if isinstance(e, dict) and str(e.get("sandbox_file_id") or "") == sid),
        {"sandbox_file_id": sid, "status": "accepted", "file_path": split_file.get("filename")},
    )
    file_rows = rcf._file_rows(sb, [sid]) if sid else {}
    file_row = file_rows.get(sid)
    name = rcf.entry_name(entry, file_row)
    segments = segments_for(sb, [file_id]).get(str(file_id), [])
    existing = rows_for_file(project_rows(sb, project_id), split_file)
    settings = get_settings()
    scratch = _scratch_dir(settings)

    def fetch_original():
        decision = rcf.promotion_for(entry, file_row)
        if isinstance(decision, rcf.Skip):
            return decision
        return rcf.fetch_entry(decision, file_row, name, scratch / uuid.uuid4().hex, settings.upload_max_bytes)

    try:
        result = promote_split_file(
            sb, project_id=project_id, harvest_id=harvest_id, split_file=split_file, segments=segments,
            existing=existing, fetch_original=fetch_original, name=name,
        )
    finally:
        _clean_scratch(scratch)
    if not result.used_split:
        return result
    try:
        sb.table(CREATED_TABLE).update({"files_promoted": count_documents(sb, project_id)}).eq(
            "project_id", project_id).execute()
    except Exception:  # noqa: BLE001 - a count, never a gate
        logger.exception("rfp split: could not refresh files_promoted on %s", project_id)
    audit(actor_id, "rfp_split.resync", "project", project_id, {
        "bid_split_file_id": file_id, "documents": result.documents, "inserted": result.inserted,
        "replaced": result.replaced, "skipped": result.skipped[:20],
    })
    return result


def resync_after_run(file_id: str) -> None:
    """Called by the splitter worker when a run on an rfp job's file ends
    `done`: re-file the project's rows when the job has a project and its
    package has not sent. Best effort, never raises."""
    from app.core.supabase_client import get_supabase
    from app.routers.files import handoff_locked

    try:
        sb = get_supabase()
        split_file, job = job_for_file(sb, file_id)
        if split_file is None or job is None or not job.get("project_id"):
            return
        if handoff_locked(str(job["project_id"])):
            logger.info("rfp split: package sent for %s; run on %s not re-filed", job["project_id"], file_id)
            return
        resync_project_files(sb, file_id, actor_id=None)
    except Exception:  # noqa: BLE001
        logger.exception("rfp split: resync after run failed for %s", file_id)


# ── Split failure fallback, the project flag and the manual run (section 10) ─
#
# A split that cannot finish no longer holds the project back. The pipeline
# keeps its retry ladder (4 attempts, 1 / 5 / 15 min; the model away and the
# sandbox still checking spend none); when the ladder is spent at the split
# step the harvest is marked split `failed` with the reason and the row moves
# on to `create` like any terminal split outcome, so the project is born with
# every verified document promoted WHOLE through the pre-split mapping. The
# harvest's split_status / split_error stay the source of truth for the
# failure; the project page and the Created from RFP Ingestion card read the
# flag from them live (`issues_for_records`). A person then either runs the
# splitter from the project (`start_manual_run`) or marks it split outside the
# app (rfp_created_projects.split_resolution, migration 0143).

ISSUE_FAILED = "failed"
ISSUE_PARTIAL = "partial"
ISSUE_RUNNING = "running"
RESOLUTION_OUTSIDE = "outside"

MSG_SPLIT_RUNNING = "The Bid File Splitter is already running on this project."
MSG_SPLIT_OFF = "The Bid File Splitter is turned off on this server."
MSG_QUEUE_OFF = "The AI job queue is off on this server, so the splitter cannot run."
MSG_MARKED_OUTSIDE = (
    "This project is marked as split outside the app. Undo that first to run the splitter here."
)
MSG_NOTHING_TO_RUN = "The Bid File Splitter has nothing to re-run on this project."
MSG_NO_HARVEST = "This project has no harvested documents to split."
MSG_QUEUE_FAILED = "The splitter runs could not be queued. Try again in a minute."
MSG_RETRY_WHILE_SPLITTING = (
    "The Bid File Splitter is still running on this project. Retry documents once it finishes."
)
MSG_FILES_PROMOTING = (
    "The documents are still being added to this project. Run the splitter once that finishes."
)
_MSG_OVER_PAGE_CAP = (
    "The files that failed are over the splitter's page limit ({cap} pages per file); "
    "split them outside the app."
)
_MSG_RUN_INTERRUPTED = "The splitter run was interrupted before its documents were staged."
_ISSUE_FILES_MAX = 100
_ISSUE_FILE_SELECT = "id, job_id, filename, status, error, page_count, created_at, updated_at"
_HARVEST_ISSUE_SELECT = (
    "id, split_status, split_error, split_job_id, split_started_at, split_finished_at"
)


def failure_reason(files: list[dict]) -> str:
    """`split_error` for a job whose every file failed: the per-file reasons,
    so the flag says WHY (one shared reason is named once)."""
    failed = [f for f in files if f.get("status") == "failed"]
    if not failed:
        return _MSG_ALL_FAILED
    head = _MSG_ALL_FAILED.rstrip(".")
    errors = {str(f.get("error") or "").strip().rstrip(".") for f in failed}
    if len(errors) == 1 and next(iter(errors)):
        text = f"{head} ({next(iter(errors))})."
    else:
        parts = [
            f"{f.get('filename') or 'file'}: {str(f.get('error') or '').strip().rstrip('.') or 'no reason recorded'}"
            for f in failed
        ]
        text = f"{head}: " + "; ".join(parts) + "."
    return text[:_ERROR_MAX_CHARS]


def give_up(
    sb, harvest_id: str | None, error: str, *, attempts: int, session_id: str | None = None,
    rfp_email_id: str | None = None,
) -> str:
    """The retry ladder is spent at the split step: mark the harvest split
    `failed` with a plain reason carrying the error text (CAS from `none`,
    where `advance` puts the claim back on a staging exception, or from a
    `pending` claim the put-back missed) and return that reason for the
    source row. The caller then moves the row `split -> create`.

    A harvest at `running` is left alone: its job exists and keeps going
    (only the check failed); creation links the job to the project, each file
    that finishes re-files the project (`resync_after_run`) and the job's
    settle writes the outcome back (`settle_linked_job`). A write that fails
    (the database is what is down) is logged; creation reads the harvest on
    its own ladder. Never raises."""
    n = max(1, int(attempts or 1))
    tries = f"{n} attempt{'s' if n != 1 else ''}"
    error = redact_text(error)
    reason = f"The documents could not be staged for splitting after {tries}: {error}"[:_ERROR_MAX_CHARS]
    marked = False
    if harvest_id:
        fields = {"split_status": SPLIT_FAILED, "split_error": reason, "split_finished_at": _iso(_now())}
        try:
            marked = _cas_split(sb, harvest_id, SPLIT_NONE, fields) or _cas_split(
                sb, harvest_id, SPLIT_PENDING, fields)
        except Exception:  # noqa: BLE001 - the row still moves on; creation retries on its own ladder
            logger.exception("rfp split: could not mark harvest %s split failed", harvest_id)
    if harvest_id and not marked:
        reason = (
            f"The split step gave up after {tries} ({error}); the project is created now and "
            "any split job still running re-files its documents when it finishes."
        )[:_ERROR_MAX_CHARS]
    _record(
        sb, session_id, "gave_up",
        f"Split gave up after {tries}; the project is created with every document whole",
        level=rfp_test.LEVEL_ERROR,
        detail={"harvest_id": harvest_id, "attempts": n, "error": str(error)[:_ERROR_MAX_CHARS],
                "reason": reason, "marked_failed": marked, "next": "create"},
        harvest_id=harvest_id, rfp_email_id=rfp_email_id,
    )
    return reason


def sync_record(sb, project_id: str, split_status: str | None, job_id: str | None) -> None:
    """Refresh the rfp_created_projects copy of the split status (written at
    creation for the page; the flag itself reads the harvest live). Best
    effort."""
    try:
        sb.table(CREATED_TABLE).update({"split_status": split_status, "split_job_id": job_id}).eq(
            "project_id", project_id).execute()
    except Exception:  # noqa: BLE001 - a denormalized copy, never a gate
        logger.exception("rfp split: could not refresh the created record of %s", project_id)


def settle_linked_job(sb, job: dict) -> None:
    """A job that serves an existing project reached a terminal status
    (bid_split.refresh_job calls this once per settle): write the outcome to
    the harvest that points at it, exactly as `_check` grades it (done /
    done_with_errors -> complete, failed -> failed with the per-file
    reasons), and refresh the created record's copy. The pipeline's own
    `_check` handles jobs whose project does not exist yet."""
    job_id = str(job.get("id") or "")
    status = job.get("status")
    project_id = job.get("project_id")
    if not job_id or not project_id or status not in ("done", "done_with_errors", "failed"):
        return
    files = job_files(sb, job_id)
    if status == "failed":
        fields = {"split_status": SPLIT_FAILED, "split_error": failure_reason(files)}
    else:
        fields = {"split_status": SPLIT_COMPLETE, "split_error": None}
    fields["split_finished_at"] = _iso(_now())
    rows = (
        sb.table(HARVESTS_TABLE).update(fields).eq("split_job_id", job_id)
        .in_("split_status", [SPLIT_RUNNING, SPLIT_COMPLETE, SPLIT_FAILED]).execute()
    ).data or []
    if not rows:
        return
    sync_record(sb, str(project_id), fields["split_status"], job_id)
    audit(None, "rfp_split.settled", "project", str(project_id), {
        "job_id": job_id, "status": fields["split_status"],
        "files_failed": sum(1 for f in files if f.get("status") == "failed"), "files": len(files),
    })


def split_issue(
    harvest: dict | None, job: dict | None, files: list[dict], *, record: dict | None = None,
    names: dict[str, str | None] | None = None, now: datetime | None = None,
    dead: bool = False, settings: Settings | None = None,
) -> dict | None:
    """The project's split flag, or None when there is nothing to say:

    - `running`: a run started after creation is staging (`pending` with a
      fresh heartbeat) or its job is processing;
    - `failed`: the harvest's split failed (staging gave up, every file
      failed, the job went missing), a staging claim went stale (no
      heartbeat for RFP_SPLIT_STAGING_STALE_SECONDS: the server restarted
      mid-staging), or the job is `dead` (`dead_job_ids`: processing with
      nothing queued or running); `reason` is the harvest's `split_error`
      or the interrupted sentence;
    - `partial`: the split completed but some files failed (their reasons in
      `files_failed`); those files were promoted whole.

    `skipped` (flags off, queue off, no_files, linked) is never a splitter
    failure: the documents-skipped / missing-files flags cover those."""
    if not harvest:
        return None
    hs = str(harvest.get("split_status") or SPLIT_NONE)
    job_status = (job or {}).get("status")
    failed = [f for f in files if f.get("status") == "failed"]
    state: str | None = None
    reason: str | None = None
    if hs == SPLIT_PENDING:
        if pending_is_stale(harvest, now, settings):
            state, reason = ISSUE_FAILED, harvest.get("split_error") or _MSG_RUN_INTERRUPTED
        else:
            state = ISSUE_RUNNING
    elif job_status == "processing" and hs in (SPLIT_RUNNING, SPLIT_COMPLETE, SPLIT_FAILED):
        if dead:
            state, reason = ISSUE_FAILED, _MSG_JOB_INTERRUPTED
        else:
            state = ISSUE_RUNNING
    elif hs == SPLIT_RUNNING:
        # Settled but not yet written back: grade the job as `_check` would.
        if job is None:
            state, reason = ISSUE_FAILED, _MSG_JOB_MISSING
        elif job_status == "failed":
            state, reason = ISSUE_FAILED, failure_reason(files)
        elif failed:
            state = ISSUE_PARTIAL
    elif hs == SPLIT_FAILED:
        state, reason = ISSUE_FAILED, harvest.get("split_error") or _MSG_ALL_FAILED
    elif hs == SPLIT_COMPLETE and failed:
        state = ISSUE_PARTIAL
    if state is None:
        return None
    rec = record or {}
    resolution = None
    if rec.get("split_resolution") == RESOLUTION_OUTSIDE and state != ISSUE_RUNNING:
        by = rec.get("split_resolved_by")
        resolution = {
            "kind": RESOLUTION_OUTSIDE,
            "by": {"id": str(by), "full_name": (names or {}).get(str(by))} if by else None,
            "at": rec.get("split_resolved_at"),
        }
    return {
        "state": state,
        "reason": reason,
        "job_id": (str(job["id"]) if job else None) or harvest.get("split_job_id"),
        "files_total": len(files),
        "files_done": sum(1 for f in files if f.get("status") == "done"),
        "files_failed_count": len(failed),
        "files_failed": [
            {"file_id": str(f.get("id")), "filename": f.get("filename"), "error": f.get("error")}
            for f in failed[:_ISSUE_FILES_MAX]
        ],
        "started_at": harvest.get("split_started_at"),
        "finished_at": harvest.get("split_finished_at"),
        "resolution": resolution,
    }


def issues_for_records(
    sb, records: list[dict], names: dict[str, str | None] | None = None,
) -> dict[str, dict | None]:
    """`{project_id: split_issue}` for rfp_created_projects rows: one query
    per table (harvests, jobs, job files), never a query per record, plus
    one llm_jobs read for jobs that already look dead (`dead_job_ids`)."""
    out: dict[str, dict | None] = {}
    harvest_ids = sorted({str(r["harvest_id"]) for r in records if r.get("harvest_id")})
    harvests: dict[str, dict] = {}
    for i in range(0, len(harvest_ids), _IN_CHUNK):
        for row in (
            sb.table(HARVESTS_TABLE).select(_HARVEST_ISSUE_SELECT)
            .in_("id", harvest_ids[i:i + _IN_CHUNK]).execute()
        ).data or []:
            harvests[str(row["id"])] = row
    job_ids = sorted({
        str(h.get("split_job_id")) for h in harvests.values()
        if h.get("split_job_id") and h.get("split_status") not in (SPLIT_NONE, SPLIT_SKIPPED)
    })
    jobs: dict[str, dict] = {}
    files: dict[str, list[dict]] = {}
    for i in range(0, len(job_ids), _IN_CHUNK):
        chunk = job_ids[i:i + _IN_CHUNK]
        for row in (
            sb.table(JOBS_TABLE).select("id, status, project_id, created_at, updated_at").in_("id", chunk).execute()
        ).data or []:
            jobs[str(row["id"])] = row
        for row in (
            sb.table(FILES_TABLE).select(_ISSUE_FILE_SELECT).in_("job_id", chunk).order("created_at").execute()
        ).data or []:
            files.setdefault(str(row.get("job_id")), []).append(row)
    settings = get_settings()
    by_job = {str(h["split_job_id"]): h for h in harvests.values() if h.get("split_job_id")}
    try:
        dead = dead_job_ids(sb, list(jobs.values()), files, by_job, settings=settings)
    except Exception:  # noqa: BLE001 - a flag read; the job reads as running instead
        logger.exception("rfp split: dead-job check failed")
        dead = set()
    for rec in records:
        pid = str(rec.get("project_id") or "")
        harvest = harvests.get(str(rec.get("harvest_id") or ""))
        jid = str((harvest or {}).get("split_job_id") or "")
        out[pid] = split_issue(harvest, jobs.get(jid), files.get(jid, []), record=rec, names=names,
                               dead=jid in dead, settings=settings)
    return out


def split_running(sb, record: dict) -> bool:
    """The project's split is live right now: a staging claim with a fresh
    heartbeat, or a job still processing that is not dead. "Retry
    documents" waits for it (docs/RFP_SPLIT.md 10.5): the promotion and the
    splitter's re-file reconcile the same sandbox ids."""
    if not record.get("harvest_id"):
        return False
    issue = issues_for_records(sb, [record]).get(str(record.get("project_id") or ""))
    return bool(issue and issue.get("state") == ISSUE_RUNNING)


def creation_note(sb, harvest: dict | None) -> str | None:
    """One sentence for the "Project created" bells when the split did not
    fully succeed, else None. Best effort (None on any trouble)."""
    try:
        if not harvest:
            return None
        hs = harvest.get("split_status")
        if hs == SPLIT_FAILED:
            reason = str(harvest.get("split_error") or _MSG_ALL_FAILED).rstrip(".")
            return f"The Bid File Splitter failed on it ({reason}); every document was added whole."
        if hs == SPLIT_COMPLETE and harvest.get("split_job_id"):
            files = job_files(sb, str(harvest["split_job_id"]))
            failed = sum(1 for f in files if f.get("status") == "failed")
            if failed:
                return (
                    f"The Bid File Splitter could not split {failed} of {len(files)} files; "
                    "those files were added whole."
                )
    except Exception:  # noqa: BLE001 - a courtesy line on a bell
        logger.exception("rfp split: creation note failed")
    return None


def _harvest_full(sb, harvest_id: str | None) -> dict | None:
    if not harvest_id:
        return None
    rows = sb.table(HARVESTS_TABLE).select("*").eq("id", harvest_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _claim_manual(sb, harvest: dict, expected: str, fields: dict) -> bool:
    """The manual run's claim: a CAS on the status just read AND on its
    `split_started_at` stamp. The status alone does not fence a `running`
    harvest whose job settled without the write-back (running -> running
    would let two presses both pass); every claim writes a fresh stamp, so
    the second press no longer matches."""
    query = sb.table(HARVESTS_TABLE).update(fields).eq("id", harvest["id"]).eq("split_status", expected)
    started = harvest.get("split_started_at")
    query = query.eq("split_started_at", started) if started else query.is_("split_started_at", "null")
    return bool(query.execute().data or [])


class RunRefused(Exception):
    """The manual run cannot start; `str(exc)` is the sentence for the
    person, `status_code` the HTTP answer (409 unless said otherwise)."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def start_manual_run(sb, record: dict, *, actor_id: str | None, settings: Settings | None = None) -> dict:
    """"Run the splitter" on an RFP-created project whose split failed or
    partly failed (section 10.3). Refuses (`RunRefused`) while the splitter
    or the queue is off, the project is marked split outside the app, its
    hand-off package has sent (MSG_PACKAGE_SENT, the corrections rule), a
    run is already going, there is nothing to re-run, or the model is away
    (its sentence).

    Two modes, both claimed on the harvest's split_status first (a CAS from
    the status just read, so two presses start one run):

    - `requeue`: the harvest has a split job with failed files. Those files
      (except the ones over the page cap, which would fail the same way)
      go `pending` and are queued again on that job, now linked to the
      project; harvest `running`.
    - `stage`: no job (staging gave up, or the job is gone): harvest
      `pending`; the CALLER schedules `stage_for_project` in the background
      (staging downloads and uploads every document; minutes, never on the
      request thread).

    Either way each file that finishes re-files the project's documents
    through `resync_after_run` (a whole-file row carrying the sandbox id
    becomes the source set in place, or takes its category), and the job's
    settle writes the outcome back (`settle_linked_job`). Returns
    `{mode, job_id, files}`."""
    from app.routers.files import handoff_locked
    from app.services import bid_split

    s = settings or get_settings()
    project_id = str(record["project_id"])
    if not enabled(s):
        raise RunRefused(MSG_SPLIT_OFF)
    if not s.llm_queue_enabled:
        raise RunRefused(MSG_QUEUE_OFF)
    if record.get("split_resolution") == RESOLUTION_OUTSIDE:
        raise RunRefused(MSG_MARKED_OUTSIDE)
    if handoff_locked(project_id):
        raise RunRefused(MSG_PACKAGE_SENT)
    # The document promotion (rfp_create_files) and a run's re-file both
    # reconcile the same sandbox ids: run together, the promotion's whole
    # row can win the unique index over the re-file's source set and stay
    # in its trade category beside the new segments. Wait for it.
    from app.services import rfp_create_files as rcf

    if record.get("files_status") in (rcf.STATUS_PENDING, rcf.STATUS_RUNNING) and not rcf.claim_is_stale(record, s):
        raise RunRefused(MSG_FILES_PROMOTING)
    harvest = _harvest_full(sb, record.get("harvest_id"))
    if harvest is None:
        raise RunRefused(MSG_NO_HARVEST)
    issue = issues_for_records(sb, [record]).get(project_id)
    if issue and issue["state"] == ISSUE_RUNNING:
        raise RunRefused(MSG_SPLIT_RUNNING)
    if not issue:
        raise RunRefused(MSG_NOTHING_TO_RUN)
    away = model_away(s)
    if away:
        raise RunRefused(away[1])
    job = _job(sb, harvest.get("split_job_id"))
    if job is not None and job.get("status") == "processing":
        # The flag is not `running`, so this job should be dead (a restart
        # between a file's pending mark and its enqueue): its stranded files
        # go failed "interrupted", the job settles and writes the harvest
        # back, and the run below queues them again (docs/RFP_SPLIT.md
        # 10.5). Re-checked here: a job with a queue run still active is
        # never reaped.
        if not _reap_if_dead(sb, harvest, job, s):
            raise RunRefused(MSG_SPLIT_RUNNING)
        harvest = _harvest_full(sb, record.get("harvest_id")) or harvest
        job = _job(sb, harvest.get("split_job_id"))
    hs = str(harvest.get("split_status") or SPLIT_NONE)
    files = job_files(sb, str(job["id"])) if job else []

    if job is not None and files:
        cap = int(s.bid_split_max_pages_per_file)
        failed = [f for f in files if f.get("status") == "failed"]
        runnable = [f for f in failed if not (f.get("page_count") and int(f["page_count"]) > cap)]
        if not failed:
            raise RunRefused(MSG_NOTHING_TO_RUN)
        if not runnable:
            raise RunRefused(_MSG_OVER_PAGE_CAP.format(cap=cap))
        job_id = str(job["id"])
        if not _claim_manual(sb, harvest, hs, {
            "split_status": SPLIT_RUNNING, "split_error": None, "split_finished_at": None,
            "split_started_at": _iso(_now()),
        }):
            raise RunRefused(MSG_SPLIT_RUNNING)
        if hs == SPLIT_PENDING:
            _discard_orphans(sb, {**harvest, "split_job_id": job_id}, s)   # an interrupted stage run's leftovers
        sb.table(JOBS_TABLE).update({"project_id": project_id}).eq("id", job_id).is_("project_id", "null").execute()
        sync_record(sb, project_id, SPLIT_RUNNING, job_id)
        queued = 0
        for f in runnable:
            fid = str(f["id"])
            try:
                bid_split._mark(fid, status="pending", error=None)
                llm_queue.enqueue(
                    llm_queue.JOB_BID_SPLIT, target_id=fid, payload={"file_id": fid},
                    created_by=actor_id, settings=s,
                )
                queued += 1
            except llm_queue.JobAlreadyActive:
                queued += 1   # a run is already in flight for it
            except Exception:  # noqa: BLE001 - the file keeps its failure; the settle grades it
                logger.exception("rfp split: manual requeue failed for file %s", fid)
                try:
                    bid_split._mark(fid, status="failed", error=f.get("error"))
                except Exception:  # noqa: BLE001
                    logger.exception("rfp split: could not restore the failed mark on %s", fid)
        if queued == 0:
            raise RunRefused(MSG_QUEUE_FAILED, 503)
        return {"mode": "requeue", "job_id": job_id, "files": queued}

    if not _entries(harvest):
        raise RunRefused(MSG_NO_HARVEST)
    if not _claim_manual(sb, harvest, hs, {
        "split_status": SPLIT_PENDING, "split_error": None, "split_finished_at": None,
        "split_started_at": _iso(_now()),
    }):
        raise RunRefused(MSG_SPLIT_RUNNING)
    sync_record(sb, project_id, SPLIT_PENDING, harvest.get("split_job_id"))
    return {"mode": "stage", "job_id": None, "files": len(_entries(harvest))}


def _manual_context(sb, record: dict, harvest: dict) -> dict:
    """The name classifier's context for a manual run, from the source row
    (best effort: an empty context only makes the name guess plainer)."""
    try:
        if record.get("portal_invitation_id"):
            rows = (
                sb.table("rfp_portal_invitations").select("id, title, portal")
                .eq("id", record["portal_invitation_id"]).limit(1).execute()
            ).data or []
            return context_for_portal(rows[0], harvest) if rows else {}
        if record.get("rfp_email_id"):
            rows = (
                sb.table("rfp_emails").select("id, subject, extracted_project_name, invitation_method")
                .eq("id", record["rfp_email_id"]).limit(1).execute()
            ).data or []
            return context_for_email(rows[0], harvest) if rows else {}
    except Exception:  # noqa: BLE001
        logger.exception("rfp split: manual run context failed for %s", record.get("project_id"))
    return {}


def stage_for_project(project_id: str, actor_id: str | None) -> None:
    """The background half of a `stage` manual run: stage a fresh job from
    the project's harvest with the pipeline's own `_start` (the upload now
    retries a dropped connection), born linked to the project. A staging
    exception marks the harvest failed with the reason (the job is already
    discarded by `_start`); nothing staged marks it failed too (every
    document refused). A job whose every row failed at staging settles
    inside `_start`, before the harvest points at it, so it is settled here.

    The claim `start_manual_run` wrote is taken over first with a forced
    heartbeat (a second task that read the same claim then finds it moved
    and stands down), orphaned jobs of an earlier interrupted staging are
    discarded, and the staging loop keeps the claim's stamp fresh, so a
    restart mid-staging reads as interrupted within
    RFP_SPLIT_STAGING_STALE_SECONDS, not an hour (docs/RFP_SPLIT.md 10.5).
    Never raises (a background task)."""
    from app.core.supabase_client import get_supabase

    sb = get_supabase()
    settings = get_settings()
    harvest_id = None
    claim: _StagingClaim | None = None
    try:
        records = sb.table(CREATED_TABLE).select("*").eq("project_id", project_id).limit(1).execute().data or []
        record = records[0] if records else {}
        harvest = _harvest_full(sb, record.get("harvest_id"))
        if harvest is None or harvest.get("split_status") != SPLIT_PENDING:
            return
        claim = _StagingClaim(sb, harvest["id"], harvest.get("split_started_at"), settings)
        if not claim.beat(force=True):
            logger.warning("rfp split: manual staging of %s stood down; its claim moved", project_id)
            return
        harvest_id = harvest["id"]
        _discard_orphans(sb, harvest, settings)
        job_id, staged = _start(
            sb, harvest, _entries(harvest), settings, _manual_context(sb, record, harvest), None, None,
            project_id=project_id, created_by=actor_id, claim=claim,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rfp split: manual staging failed for project %s", project_id)
        if isinstance(exc, StagingClaimLost):
            return   # a newer run owns the harvest now; its outcome is the one to show
        reason = f"The documents could not be staged for splitting: {redact_text(exc)}"[:_ERROR_MAX_CHARS]
        if harvest_id:
            try:
                claim.cas({
                    "split_status": SPLIT_FAILED, "split_error": reason, "split_finished_at": _iso(_now()),
                })
            except Exception:  # noqa: BLE001 - the stale-claim rule frees it within minutes
                logger.exception("rfp split: could not mark harvest %s failed", harvest_id)
            sync_record(sb, project_id, SPLIT_FAILED, None)
        audit(actor_id, "rfp_split.manual_run_failed", "project", project_id, {"error": reason})
        return
    if staged == 0:
        claim.cas({
            "split_status": SPLIT_FAILED, "split_error": _MSG_NONE_STAGED, "split_job_id": job_id,
            "split_finished_at": _iso(_now()),
        })
        sync_record(sb, project_id, SPLIT_FAILED, job_id)
        return
    sync_record(sb, project_id, SPLIT_RUNNING, job_id)
    job = _job(sb, job_id)
    if job is not None and job.get("status") != "processing":
        settle_linked_job(sb, job)
