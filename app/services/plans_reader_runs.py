"""AI Estimating run lifecycle (docs/PLANS_READER.md sections 3 and 7).

Why this module exists: one AI Estimating run turns a set of uploaded files
(or a read-only copy of an existing project's files) into a brief and the
deliverable files, through many independent steps that finish in any order
on different workers:

    created -> splitting -> reading -> consolidating -> rendering -> done
                                                               (done_with_errors)
    any active state -> over_cap (spend cap) -> resume -> back where it was
    any non-terminal state -> canceled; failed when nothing usable is left

- Files are labeled by the user with a splitter category, or go through the
  Bid File Splitter (a `bid_split_jobs` row with source='plans_reader' and
  no project, so none of the project re-filing side effects fire). Each
  splitter segment, and each labeled file, becomes a plans_reader_documents
  row.
- `advance` is the state machine. It is called after every group finishes
  (plans_reader_agents.run_group), after a terminal group failure (the
  queue's mark), after every split file settles (bid_split's hook) and by
  resume. It is idempotent and safe under concurrency: every stage change
  is a compare-and-set on the run's status (`update ... where status =
  <expected>`, like llm_queue._cas_job), so exactly one caller wins each
  transition and the main group is enqueued exactly once; group rows a
  stage adds (roll-ups, escalations, the main group) are deduplicated after
  insert (a started row, else the earliest, wins per identity and the
  others are deleted before anything is enqueued), so two workers
  finishing at once never queue a call twice. The heavy splitting ->
  reading planning step is claimed first (_claim_planning), and a split
  file is claimed before its documents are written (FILE_SETTLING).
- Lost work is repaired, never duplicated: a stage whose winner died (no
  groups, no main group, a render cut short) is repaired only after
  RECOVERY_AFTER_SECONDS without a change to the run row; long steps bump
  updated_at as a heartbeat. A poll (get_run_payload) re-enqueues queued
  groups that have no live job and kicks a stalled run on one background
  thread per run and process. A start that fails part way is undone (the
  run goes back to created), and a consolidation that failed for good is
  retried once at a reasoning effort one step lower before the run fails.
- Group jobs are paced: before a stage enqueues its groups, their start
  times are spread over 60 s windows so the estimated input of the jobs
  scheduled in any window stays under PLANS_READER_TOKENS_PER_MINUTE (the
  OpenAI account's per-minute token limit is shared by every run).
- Deliverables are rendered from the main agent's JSON specs by
  plans_reader_render into `plans-reader/{run_id}/deliverables/v{n}/` and
  recorded as plans_reader_deliverables versions; a renderer failure is
  recorded on the run and the other deliverables still render. A later
  re-render runs on the queue (a plans_reader_modify job on a `modify`
  group, one change per run at a time), never inside the request.

Every function is synchronous (Supabase SDK, storage and PDF work) and safe
to call from the queue worker thread; routers run them in the threadpool.
User-facing refusals raise RunRefused, a ValueError carrying an HTTP status.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import ai_spend, bid_split, llm, llm_pricing, llm_queue, storage
from app.services import plans_reader_agents as agents
from app.services import plans_reader_images as images
from app.services import plans_reader_pack as pack
from app.services import plans_reader_render as render
from app.services.plans_reader_schemas import CATEGORIES, DELIVERABLE_KINDS

logger = logging.getLogger(__name__)

COMPANY_TZ = ZoneInfo("America/Los_Angeles")

RUN_STATUSES = (
    "created", "uploading", "splitting", "reading", "consolidating",
    "rendering", "done", "done_with_errors", "failed", "canceled", "over_cap",
)
TERMINAL_STATUSES = frozenset({"done", "done_with_errors", "failed", "canceled"})
# States with work in flight: delete refuses them (cancel first).
BUSY_STATUSES = frozenset({"uploading", "splitting", "reading", "consolidating", "rendering"})
# resume() accepts these; the router currently offers it for over_cap only
# (polls repair the other stalls on their own, see _self_heal).
RESUMABLE_STATUSES = frozenset({"over_cap", "reading", "consolidating", "rendering"})
RENDER_MODES = ("adaptive", "tiles_260")
SOURCES = ("upload", "project")

GROUP_ACTIVE = ("queued", "running")
GROUP_TERMINAL = frozenset({"done", "failed", "canceled"})
PIPELINE_KINDS = ("sub", "rollup", "escalation", "main")
# The main group's pass: 1 is the consolidation, 2 the one retry at a
# reasoning effort one step lower after pass 1 failed for good.
MAIN_PASS = 1
MAIN_RETRY_PASS = 2
# A split file whose documents are being written: the settler claimed it
# (splitting -> this) so only one caller writes them (migration 0153 adds the
# value). A database without 0153 refuses it with a CHECK violation; the
# claim then falls back to the legacy 'converted', which AI Estimating used
# for this state before 0153 and never uses otherwise.
FILE_SETTLING = "settling"
FILE_SETTLING_LEGACY = "converted"
FILE_SETTLING_STATES = frozenset({FILE_SETTLING, FILE_SETTLING_LEGACY})
FILE_UNSETTLED = frozenset({"pending", "splitting"}) | FILE_SETTLING_STATES
# delete() claims a run out of one of these (never a busy one).
DELETABLE_STATUSES = frozenset(RUN_STATUSES) - BUSY_STATUSES

JOB_AGENT = "plans_reader_agent"
JOB_MAIN = "plans_reader_main"
JOB_MODIFY = "plans_reader_modify"
# Pacing against the account's tokens-per-minute limit (see _paced_starts):
# the window length and how many scheduled jobs are read to measure the
# load other runs already put on the coming windows.
PACING_WINDOW_SECONDS = 60
PACING_LOOKBACK_SECONDS = 60
PACING_SCAN_MAX_JOBS = 500

# project_files.category -> the run's file label (docs/PLANS_READER.md 7).
# None = unlabeled: goes through the splitter.
PROJECT_CATEGORY_LABELS: dict[str, str | None] = {
    "drawing": None,
    "electrical_drawing": "electrical_drawings",
    "civil_drawing": "civil_drawings",
    "structural_drawing": "structural_drawings",
    "architectural_drawing": "architectural_drawings",
    "mechanical_drawing": "mechanical_drawings",
    "plumbing_drawing": "plumbing_drawings",
    "fire_protection_drawing": "fire_protection_drawings",
    "low_voltage_drawing": "low_voltage_drawings",
    "specification": "specifications",
    "addendum": "addenda",
    "rfp": "rfp",
    "other": "other",
}
# Our own outputs and the bid's commercial documents are never re-read.
REFUSED_PROJECT_CATEGORIES = frozenset(
    {"marked_plans", "boq", "estimate", "markup", "quote", "proposal", "rfq_split", "revision"}
)

PDF_MIME = "application/pdf"
CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": PDF_MIME,
}
NAME_MAX_CHARS = 200
FILENAME_MAX_CHARS = 200
PROJECT_FILES_MAX = 200
LIST_MAX = 200
MARKED_PLANS_PPI = 100
DELIVERABLE_VERSION_ATTEMPTS = 3
QUEUE_POLL_MAX = 10
# A stage winner inserts its groups right after its status change; a run
# found in that in-between state for this long lost its winner (a crash or
# deploy) and is repaired. Anything younger is a winner still writing.
RECOVERY_AFTER_SECONDS = 600
# A background advance per run and process at most this often (polls from
# several tabs every few seconds would otherwise each start one).
KICK_MIN_INTERVAL_SECONDS = 15
# The ledger key of the splitting -> reading planning claim (see
# _claim_planning); a claim older than RECOVERY_AFTER_SECONDS is dead.
PLANNING_CLAIM_KEY = "planning_claim"
# A queued group with no live queue job is re-enqueued by a poll at most
# this often per run and process, and only when it is at least this old.
REQUEUE_CHECK_SECONDS = 60
REQUEUE_MIN_AGE_SECONDS = 120
_ERROR_MAX_CHARS = 2_000
_PAGE = 1_000

MSG_QUEUE_OFF = (
    "AI Estimating needs the AI job queue, which is turned off on this server. "
    "Contact your IT Director."
)
MSG_NOT_CONFIGURED = "AI Estimating is not configured on this server (no OpenAI key or model)."
MSG_SPLITTER_NOT_CONFIGURED = (
    "The Bid File Splitter model is not configured, so unlabeled files cannot be split. "
    "Label every file or ask your IT Director to configure the splitter."
)
MSG_CONVERTER_OFF = (
    "Word and Excel files need the PDF converter, which is not set up on this server. "
    "Save the file as a PDF and upload that instead."
)
MSG_NO_DOCUMENTS = "No readable documents came out of the files in this run."
MSG_ALL_GROUPS_FAILED = "Every reading step of this run failed, so there is nothing to consolidate."
MSG_STARTED_DURING_UPLOAD = "The run started while this file was being added, so it was not added."
MSG_ADDED_DURING_START = (
    "This file was added while the run was starting, so it was not read. "
    "Start a new run to include it."
)
MSG_START_FAILED = (
    "The run could not be started because of a server error, so the start was undone. "
    "Try starting it again in a minute."
)
MSG_START_ROLLED_BACK = "The last start attempt failed with a server error and was undone. Start the run again."
MSG_DELETE_RACED = "This run changed while it was being deleted (it may have started). Refresh and try again."
MSG_DELETING = "Being deleted."
MSG_CHANGE_ACTIVE = (
    "A change to this run's files is already in progress. Wait for it to finish, then try again."
)
MSG_RERENDER_WHILE_RENDERING = "This run is still saving its files. Try again when it has finished."
MSG_INLINE_CHANGE_LOST = "The chat answer making this change stopped before it finished. Ask for the change again."
MSG_DELETE_BUSY = (
    "A step of this run is still talking to the AI model. Wait for it to finish, "
    "then delete the run."
)


class RunRefused(ValueError):
    """A refusal the person can act on. `str(exc)` is app-authored and ASCII;
    `status_code` is the HTTP answer the router should give."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_pacific() -> date:
    """The company day (estimate dates are shown in Pacific time)."""
    return datetime.now(COMPANY_TZ).date()


def _uid(user: Any) -> str | None:
    return agents._uid(user)


def _role(user: Any) -> str | None:
    role = user.get("role") if isinstance(user, dict) else getattr(user, "role", None)
    return str(role.value if hasattr(role, "value") else role) if role else None


def _refuse_estimator(user: Any) -> None:
    """The external estimator role never reaches AI Estimating (the router
    gates it too; this keeps the service honest on its own)."""
    if _role(user) == Role.ESTIMATOR.value:
        raise RunRefused("AI Estimating is not available to external estimators.", 403)


def _one(sb, table: str, row_id: str) -> dict | None:
    rows = sb.table(table).select("*").eq("id", row_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _load_run(sb, run_or_id: dict | str) -> dict:
    run_id = run_or_id.get("id") if isinstance(run_or_id, dict) else run_or_id
    row = _one(sb, "plans_reader_runs", str(run_id))
    if row is None:
        raise RunRefused("This AI Estimating run no longer exists.", 404)
    return row


def _files(sb, run_id: str) -> list[dict]:
    return (
        sb.table("plans_reader_files").select("*").eq("run_id", run_id).order("created_at").execute().data
        or []
    )


def _documents(sb, run_id: str) -> list[dict]:
    return (
        sb.table("plans_reader_documents").select("*").eq("run_id", run_id).order("created_at").execute().data
        or []
    )


def _groups(sb, run_id: str) -> list[dict]:
    return (
        sb.table("plans_reader_groups").select("*").eq("run_id", run_id).order("created_at").execute().data
        or []
    )


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) == "23505" or "23505" in text or "duplicate key" in text


def _is_check_violation(exc: Exception) -> bool:
    """A CHECK constraint refused the write (Postgres 23514), for example a
    status value a database without the latest migration does not know."""
    text = str(exc)
    return getattr(exc, "code", None) == "23514" or "23514" in text or "check constraint" in text.lower()


def _cas_status(sb, run_id: str, expected: str | tuple[str, ...], new: str, fields: dict | None = None) -> dict | None:
    """Move the run to `new` only if it is still in `expected` (one status
    or several); the winner gets the updated row, everyone else None."""
    patch = dict(fields or {})
    patch["status"] = new
    query = sb.table("plans_reader_runs").update(patch).eq("id", run_id)
    if isinstance(expected, tuple):
        query = query.in_("status", list(expected))
    else:
        query = query.eq("status", expected)
    rows = query.execute().data or []
    return rows[0] if rows else None


def _age_seconds(raw: Any) -> float | None:
    """Seconds since an ISO timestamp, or None when it is unreadable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _stale(run: dict, seconds: int = RECOVERY_AFTER_SECONDS) -> bool:
    """True when the run row has not changed for `seconds` (its updated_at,
    else created_at). An unreadable stamp is never stale: repairing a run a
    live worker is still writing would duplicate its work."""
    age = _age_seconds(run.get("updated_at") or run.get("created_at"))
    return age is not None and age > seconds


def _touch(sb, run_id: str, status: str) -> None:
    """Bump the run's updated_at while it is still in `status` (a heartbeat
    for long steps, so the stale-run repairs leave a live worker alone).
    Best effort."""
    try:
        sb.table("plans_reader_runs").update({"updated_at": _now_iso()}).eq("id", run_id).eq(
            "status", status
        ).execute()
    except Exception:  # noqa: BLE001 - a heartbeat, not a requirement
        logger.exception("plans_reader: heartbeat for run %s failed", run_id)


def _join_errors(errors: list[str]) -> str | None:
    text = " | ".join(e for e in errors if e)
    return text[:_ERROR_MAX_CHARS] or None


def _clean_filename(filename: str | None) -> str:
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = agents._clean_text(name, FILENAME_MAX_CHARS)
    return name or "upload.pdf"


def _ext(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def _pdf_name(filename: str) -> str:
    """The name a PDF copy of `filename` goes by (the splitter requires a
    .pdf extension)."""
    if _ext(filename) == ".pdf":
        return filename
    stem = filename[: filename.rfind(".")] if "." in filename else filename
    return f"{stem or 'document'}.pdf"


def _needs_split(file_row: dict) -> bool:
    label = file_row.get("label")
    return not label or label == "general_drawings"


# Runs this process is advancing on a background thread, and when each
# was last kicked: a poll every few seconds from several tabs must not
# start several threads that each download and plan the whole set.
_ADVANCING: set[str] = set()
_LAST_KICK: dict[str, float] = {}
_KICK_LOCK = threading.Lock()


def _kick_advance(run_id: str, *, force: bool = False) -> bool:
    """Run advance(run_id) on a daemon thread: used where the caller is a
    request (start, a poll) and the next stage downloads and plans every
    document. At most one such thread per run and process, and (unless
    `force`) at most one kick per KICK_MIN_INTERVAL_SECONDS; advance is
    idempotent and its planning step is claimed across processes, so a
    kick another process also made is harmless. Returns True when a thread
    was started."""
    key = str(run_id)
    now = time.monotonic()
    with _KICK_LOCK:
        if key in _ADVANCING:
            return False
        last = _LAST_KICK.get(key)
        if not force and last is not None and now - last < KICK_MIN_INTERVAL_SECONDS:
            return False
        _ADVANCING.add(key)
        _LAST_KICK[key] = now
        if len(_LAST_KICK) > 1_000:
            for stale_key in [k for k, t in _LAST_KICK.items() if now - t > 3_600]:
                _LAST_KICK.pop(stale_key, None)

    def _go() -> None:
        try:
            advance(get_supabase(), key)
        except Exception:  # noqa: BLE001 - the next event or poll retries
            logger.exception("plans_reader: background advance of run %s failed", key)
        finally:
            with _KICK_LOCK:
                _ADVANCING.discard(key)

    try:
        threading.Thread(target=_go, name=f"plans-reader-advance-{key}", daemon=True).start()
    except Exception:
        with _KICK_LOCK:
            _ADVANCING.discard(key)
        raise
    return True


# Page sizes per stored object (immutable: every path carries a uuid), so
# the cost preview and start do not download a 400 MB set twice.
_INFO_CACHE: OrderedDict[str, dict] = OrderedDict()
_INFO_CACHE_MAX = 256
_INFO_LOCK = threading.Lock()


def _remember_info(path: str, info: dict) -> None:
    with _INFO_LOCK:
        _INFO_CACHE[path] = info
        _INFO_CACHE.move_to_end(path)
        while len(_INFO_CACHE) > _INFO_CACHE_MAX:
            _INFO_CACHE.popitem(last=False)


def _pdf_info(pdf: bytes) -> dict:
    sizes = images.page_sizes_in(pdf)
    return {"page_count": len(sizes), "page_sizes_in": sizes, "size_bytes": len(pdf)}


def _file_info(sb, file_row: dict) -> dict:
    """{page_count, page_sizes_in, size_bytes} of a run file, downloaded and
    measured once per stored object and process; also fills the row's
    page_count when it was unknown (project copies)."""
    path = file_row["storage_path"]
    with _INFO_LOCK:
        cached = _INFO_CACHE.get(path)
    if cached is not None:
        return cached
    info = _pdf_info(storage.download_file(path))
    _remember_info(path, info)
    if file_row.get("page_count") != info["page_count"]:
        try:
            sb.table("plans_reader_files").update({"page_count": info["page_count"]}).eq(
                "id", file_row["id"]
            ).execute()
        except Exception:  # noqa: BLE001 - a cache fill, not a requirement
            logger.exception("plans_reader: page count update for file %s failed", file_row.get("id"))
    return info


def _settings_snapshot(s) -> dict:
    return {
        "openai_plans_model": s.openai_plans_model,
        "plans_reader_reasoning_effort": s.plans_reader_reasoning_effort,
        "plans_reader_group_input_tokens": s.plans_reader_group_input_tokens,
        "plans_reader_ledger_max_tokens": s.plans_reader_ledger_max_tokens,
        "plans_reader_max_input_tokens_per_run": s.plans_reader_max_input_tokens_per_run,
        "plans_reader_promax_max_pages_per_message": s.plans_reader_promax_max_pages_per_message,
        "plans_reader_escalation_max_fraction": s.plans_reader_escalation_max_fraction,
        "plans_reader_tile_ppi": s.plans_reader_tile_ppi,
        "plans_reader_native_max_patches": s.plans_reader_native_max_patches,
        "plans_reader_file_max_bytes": s.plans_reader_file_max_bytes,
        "plans_reader_call_timeout_seconds": s.plans_reader_call_timeout_seconds,
    }


# ---------------------------------------------------------------------------
# Create, upload, copy from a project
# ---------------------------------------------------------------------------


def create_run(
    sb,
    *,
    name: str,
    source: str = "upload",
    project_id: str | None = None,
    render_mode: str | None = "adaptive",
    user: Any,
) -> dict:
    """Insert a plans_reader_runs row (status created) with the model, the
    reasoning effort and a snapshot of the PLANS_READER_* settings, so a
    run's numbers can be explained after the settings change."""
    _refuse_estimator(user)
    s = get_settings()
    clean = agents._clean_text(name, NAME_MAX_CHARS)
    if not clean:
        raise RunRefused("Give the run a name.")
    if source not in SOURCES:
        raise RunRefused("A run starts from uploaded files or from a project's files.")
    mode = render_mode or "adaptive"
    if mode not in RENDER_MODES:
        raise RunRefused("The render mode must be adaptive or tiles_260.")
    if source == "project" and not project_id:
        raise RunRefused("Choose the project whose files this run reads.")
    if project_id:
        if _one(sb, "projects", str(project_id)) is None:
            raise RunRefused("That project no longer exists.", 404)
    row = {
        "name": clean,
        "status": "created",
        "source": source,
        "project_id": str(project_id) if project_id else None,
        "render_mode": mode,
        "reasoning_effort": s.plans_reader_reasoning_effort,
        "model": s.openai_plans_model,
        "settings_snapshot": _settings_snapshot(s),
        "created_by": _uid(user),
    }
    return sb.table("plans_reader_runs").insert(row).execute().data[0]


def _require_created(run: dict) -> None:
    if run.get("status") != "created":
        raise RunRefused("Files can only be added before the run starts.", 409)


def _validate_label(label: str | None) -> str | None:
    if label in (None, ""):
        return None
    if label not in CATEGORIES:
        raise RunRefused("Unknown file label. Pick one of the splitter categories or leave it blank.")
    return label


def _convert_office(content: bytes, ext: str, s) -> bytes:
    """A .docx or .xlsx as PDF through the Gotenberg route the RFP sandbox
    uses (rfp_office_convert scans the container for external content
    before anything is sent). Refusals become RunRefused."""
    from app.services import rfp_office_convert as office

    if s.preview_engine != "gotenberg" or not s.gotenberg_url:
        raise RunRefused(MSG_CONVERTER_OFF, 415)
    fmt = ext.lstrip(".")
    with tempfile.TemporaryDirectory(prefix="plans-reader-") as tmp:
        src = Path(tmp) / f"source.{fmt}"
        dest = Path(tmp) / "converted.pdf"
        src.write_bytes(content)
        try:
            office.convert_to_pdf(
                src,
                dest,
                source_format=fmt,
                max_bytes=s.upload_max_bytes,
                timeout_seconds=float(s.rfp_ingest_office_convert_timeout_seconds),
                base_url=s.gotenberg_url,
            )
        except office.ConversionTooLarge as exc:
            raise RunRefused(str(exc), 413) from exc
        except office.ConversionRejected as exc:
            raise RunRefused(str(exc), 415) from exc
        except office.ConversionUnavailable as exc:
            raise RunRefused(f"{exc} Try again in a few minutes.", 503) from exc
        return dest.read_bytes()


def _sniff(filename: str, content: bytes) -> str:
    """'.pdf', '.docx' or '.xlsx' by extension AND magic bytes."""
    ext = _ext(filename)
    if ext == ".pdf" and content[:5] == b"%PDF-":
        return ext
    if ext in (".docx", ".xlsx") and content[:4] == b"PK\x03\x04":
        return ext
    raise RunRefused(
        f"{filename}: AI Estimating reads PDF, Word (.docx) and Excel (.xlsx) files only.", 415
    )


def _check_split_pages(filename: str, pages: int, label: str | None, s) -> None:
    if (not label or label == "general_drawings") and pages > s.bid_split_max_pages_per_file:
        raise RunRefused(
            f"{filename}: {pages} pages is over the splitter's limit of "
            f"{s.bid_split_max_pages_per_file} pages per file. Label the file (for example "
            "Electrical Drawings or Specifications) so it is read without splitting.",
            413,
        )


def _store_source(sb, run: dict, *, filename: str, pdf: bytes, label: str | None, extra: dict | None = None) -> dict:
    path = storage.build_plans_reader_path(run["id"], "source", _pdf_name(filename))
    storage.upload_file(path, pdf, PDF_MIME)
    row = {
        "run_id": run["id"],
        "filename": filename,
        "storage_path": path,
        "size_bytes": len(pdf),
        "mime_type": PDF_MIME,
        "label": label,
        "status": "pending",
        **(extra or {}),
    }
    try:
        inserted = sb.table("plans_reader_files").insert(row).execute().data[0]
    except Exception:
        storage.delete_file(path)  # no orphan object for a failed insert
        raise
    _ensure_still_created(sb, run["id"], inserted)
    return inserted


def _ensure_still_created(sb, run_id: str, file_row: dict) -> None:
    """After a file row is inserted: if the run left `created` meanwhile
    (start ran while this file was converted or copied), take the row and
    its object back out and refuse, so no file sits `pending` in a started
    run (it would hold the run in splitting for good). start fails any such
    row it sees itself, so the file is never half-read either way."""
    current = _one(sb, "plans_reader_runs", str(run_id))
    if current is not None and current.get("status") == "created":
        return
    try:
        sb.table("plans_reader_files").delete().eq("id", file_row["id"]).execute()
    finally:
        try:
            storage.delete_file(file_row["storage_path"])
        except Exception:  # noqa: BLE001 - an orphan object is swept with the run
            logger.exception("plans_reader: could not remove %s", file_row.get("storage_path"))
    raise RunRefused(f"{file_row.get('filename') or 'File'}: {MSG_STARTED_DURING_UPLOAD}", 409)


def add_upload(sb, run: dict, *, filename: str, content: bytes, label: str | None, user: Any) -> dict:
    """Validate one uploaded file (PDF, .docx or .xlsx by extension and
    magic; at most UPLOAD_MAX_BYTES), convert an office file to PDF,
    store it under `plans-reader/{run_id}/source/` and insert the
    plans_reader_files row (status pending, the label as given)."""
    _refuse_estimator(user)
    s = get_settings()
    run = _load_run(sb, run)
    _require_created(run)
    name = _clean_filename(filename)
    label = _validate_label(label)
    if not content:
        raise RunRefused(f"{name}: the file is empty.")
    if len(content) > s.upload_max_bytes:
        mb = s.upload_max_bytes // (1024 * 1024)
        raise RunRefused(f"{name}: the file is over the {mb} MB upload limit.", 413)
    ext = _sniff(name, content)
    pdf = content if ext == ".pdf" else _convert_office(content, ext, s)
    try:
        info = _pdf_info(pdf)
    except ValueError as exc:
        raise RunRefused(f"{name}: {exc}", 415) from exc
    if info["page_count"] < 1:
        raise RunRefused(f"{name}: the PDF has no pages.", 415)
    _check_split_pages(name, info["page_count"], label, s)
    row = _store_source(sb, run, filename=name, pdf=pdf, label=label, extra={"page_count": info["page_count"]})
    _remember_info(row["storage_path"], info)
    return row


def add_project_files(sb, run: dict, *, project_file_ids: list[str], user: Any) -> list[dict]:
    """Copy chosen project files into the run (server-side storage copy;
    the project is never modified) with a label derived from the project
    file category; office files are converted to PDF on the way. Files
    already added are skipped, so a double click adds nothing twice."""
    _refuse_estimator(user)
    s = get_settings()
    run = _load_run(sb, run)
    _require_created(run)
    ids = list(dict.fromkeys(str(i) for i in (project_file_ids or []) if i))
    if not ids:
        raise RunRefused("Choose at least one project file.")
    if len(ids) > PROJECT_FILES_MAX:
        raise RunRefused(f"Choose at most {PROJECT_FILES_MAX} files at a time.")
    rows = sb.table("project_files").select("*").in_("id", ids).execute().data or []
    by_id = {str(r["id"]): r for r in rows}
    if len(by_id) != len(ids):
        raise RunRefused("One of the chosen project files no longer exists.", 404)
    project_ids = {str(r.get("project_id")) for r in rows}
    if run.get("project_id") and project_ids != {str(run["project_id"])}:
        raise RunRefused("Every file must come from the run's project.")
    projects = sb.table("projects").select("id").in_("id", sorted(project_ids)).execute().data or []
    if len(projects) != len(project_ids):
        raise RunRefused("One of the chosen files belongs to a project that no longer exists.", 404)
    plan: list[tuple[dict, str | None]] = []
    for pid in ids:
        pf = by_id[pid]
        category = str(pf.get("category") or "")
        name = _clean_filename(pf.get("filename"))
        if category in REFUSED_PROJECT_CATEGORIES or category not in PROJECT_CATEGORY_LABELS:
            raise RunRefused(f"{name}: files of this kind ({category or 'unknown'}) are not read by AI Estimating.")
        if _ext(name) not in (".pdf", ".docx", ".xlsx"):
            raise RunRefused(f"{name}: AI Estimating reads PDF, Word (.docx) and Excel (.xlsx) files only.", 415)
        plan.append((pf, PROJECT_CATEGORY_LABELS[category]))
    existing = {str(f.get("source_project_file_id")) for f in _files(sb, run["id"]) if f.get("source_project_file_id")}
    out: list[dict] = []
    for pf, label in plan:
        if str(pf["id"]) in existing:
            continue
        name = _clean_filename(pf.get("filename"))
        extra = {"source_project_file_id": str(pf["id"])}
        if _ext(name) == ".pdf":
            path = storage.build_plans_reader_path(run["id"], "source", name)
            storage.copy_file(pf["storage_path"], path)
            row = {
                "run_id": run["id"],
                "filename": name,
                "storage_path": path,
                "size_bytes": int(pf.get("size_bytes") or 0),
                "mime_type": PDF_MIME,
                "label": label,
                "status": "pending",
                **extra,
            }
            try:
                inserted = sb.table("plans_reader_files").insert(row).execute().data[0]
            except Exception:
                storage.delete_file(path)
                raise
            _ensure_still_created(sb, run["id"], inserted)
            out.append(inserted)
            continue
        content = storage.download_file(pf["storage_path"])
        ext = _sniff(name, content)
        pdf = _convert_office(content, ext, s)
        info = _pdf_info(pdf)
        _check_split_pages(name, info["page_count"], label, s)
        extra["page_count"] = info["page_count"]
        row = _store_source(sb, run, filename=name, pdf=pdf, label=label, extra=extra)
        _remember_info(row["storage_path"], info)
        out.append(row)
    if not run.get("project_id") and len(project_ids) == 1:
        sb.table("plans_reader_runs").update({"project_id": next(iter(project_ids))}).eq("id", run["id"]).execute()
    return out


# ---------------------------------------------------------------------------
# Cost preview and start
# ---------------------------------------------------------------------------


def _prices(model: str | None) -> tuple[float, float, int, bool]:
    price = llm_pricing.price_for(model)
    if price is None:
        return 0.0, 0.0, pack.LONG_CONTEXT_THRESHOLD, False
    return (
        float(price.input),
        float(price.output),
        int(price.long_context_threshold or pack.LONG_CONTEXT_THRESHOLD),
        True,
    )


def estimate(sb, run: dict) -> dict:
    """The cost preview (docs/PLANS_READER.md 3.1 and 7): every file planned
    with plans_reader_pack, files not split yet estimated as drawings, the
    ledger at its worst case (PLANS_READER_LEDGER_MAX_TOKENS in every
    group). Returns {documents, groups, input_tokens, output_tokens,
    cost_usd, cost_usd_max, escalation_max_input_tokens, cap_remaining_usd,
    over_cap, over_run_limit, max_input_tokens, per_category, problems,
    priced} and stores the estimate on the run. After start the stored
    estimate is returned with the live cap."""
    s = get_settings()
    run = _load_run(sb, run)
    model = run.get("model") or s.openai_plans_model
    in_m, out_m, threshold, priced = _prices(model)
    problems: list[str] = []
    if run.get("status") != "created":
        groups = [g for g in _groups(sb, run["id"]) if g.get("kind") in PIPELINE_KINDS]
        result: dict[str, Any] = {
            "documents": len(_documents(sb, run["id"])),
            "groups": len(groups),
            "input_tokens": int(run.get("estimated_input_tokens") or 0),
            "output_tokens": sum(agents.expected_output_tokens(g) for g in groups),
            "cost_usd": float(run.get("estimated_cost_usd") or 0),
            "cost_usd_max": float(run.get("estimated_cost_usd") or 0),
            "escalation_max_input_tokens": 0,
            "per_category": {},
        }
    else:
        ledger_tokens = s.plans_reader_ledger_max_tokens
        prompt = agents.prompt_tokens("sub")
        max_pages = min(
            images.FILE_MAX_PAGES,
            pack.max_file_pages_for_budget(s.plans_reader_group_input_tokens, ledger_tokens, prompt_tokens=prompt),
        )
        docs: list[pack.PackDoc] = []
        files = [f for f in _files(sb, run["id"]) if f.get("status") != "failed"]
        for f in files:
            try:
                info = _file_info(sb, f)
            except ValueError as exc:
                problems.append(f"{_clean_filename(f.get('filename'))}: {exc}")
                continue
            category = "general_drawings" if _needs_split(f) else f["label"]
            if category in agents.DRAWING_CATEGORIES:
                pages = [
                    pack.plan_page(
                        i, w, h,
                        render_mode=run.get("render_mode") or "adaptive",
                        tile_ppi=s.plans_reader_tile_ppi,
                        max_patches=s.plans_reader_native_max_patches,
                    )
                    for i, (w, h) in enumerate(info["page_sizes_in"], start=1)
                ]
                docs.append(pack.PackDoc(str(f["id"]), category, "drawing", pages=pages, page_count=len(pages)))
            else:
                docs.append(
                    pack.plan_file_document(
                        str(f["id"]), category, info["page_count"], info["size_bytes"],
                        max_bytes=s.plans_reader_file_max_bytes, max_pages=max_pages,
                    )
                )
        if docs:
            est = pack.estimate_run(
                docs,
                budget_tokens=s.plans_reader_group_input_tokens,
                ledger_tokens=ledger_tokens,
                input_per_m=in_m,
                output_per_m=out_m,
                prompt_tokens=prompt,
                file_max_bytes=s.plans_reader_file_max_bytes,
                ledger_images=agents.LEDGER_MAX_IMAGES,
                escalation_max_fraction=s.plans_reader_escalation_max_fraction,
                long_context_threshold=threshold,
            )
        else:
            est = {
                "groups": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                "cost_usd_max": 0.0, "escalation_max_input_tokens": 0, "per_category": {},
            }
        result = {
            "documents": len(docs),
            "groups": est["groups"],
            "input_tokens": est["input_tokens"],
            "output_tokens": est["output_tokens"],
            "cost_usd": est["cost_usd"],
            "cost_usd_max": est["cost_usd_max"],
            "escalation_max_input_tokens": est["escalation_max_input_tokens"],
            "per_category": est["per_category"],
        }
        sb.table("plans_reader_runs").update(
            {"estimated_input_tokens": int(est["input_tokens"]), "estimated_cost_usd": float(est["cost_usd"])}
        ).eq("id", run["id"]).execute()
    try:
        cap = ai_spend.status(sb)
        remaining = float(cap.get("remaining_usd") or 0)
        cap_hit = bool(cap.get("cap_hit"))
    except Exception:  # noqa: BLE001 - the preview still shows the run's own numbers
        logger.exception("plans_reader: spend status for the estimate failed")
        remaining, cap_hit = 0.0, True
    limit = s.plans_reader_max_input_tokens_per_run
    # over_cap means THIS run cannot start today (the cap is reached, or its
    # worst case does not fit in what is left). status()["blocked"] also
    # covers "a step was refused earlier this window", which is a Settings
    # banner, not a verdict on a smaller run that still fits.
    result.update(
        {
            "cap_remaining_usd": remaining,
            "over_cap": cap_hit or float(result["cost_usd_max"]) > remaining,
            "max_input_tokens": limit,
            "over_run_limit": int(result["input_tokens"]) + int(result["escalation_max_input_tokens"]) > limit,
            "problems": problems,
            "priced": priced,
        }
    )
    return result


def _labeled_page_meta(label: str, info: dict) -> list[dict] | None:
    """page_meta for a labeled file read whole: the triage-style per-page
    list for text kinds (spec_text), and for a drawing set a derived list
    (page numbers and sizes, no titles or roles: no classify pass runs on a
    file the user labeled) so its sheets still reach the ledger's sheet
    index and are numbered exactly as the sub agent's `D<n> P<p>` labels."""
    if label in agents.FILE_CATEGORIES:
        return bid_split.intact_page_meta(label, info["page_count"], 1.0)
    if label in agents.DRAWING_CATEGORIES:
        return agents.derived_page_meta(info["page_sizes_in"]) or None
    return None


def _document_from_file(sb, run: dict, file_row: dict) -> dict:
    """A labeled file read whole as one document."""
    info = _file_info(sb, file_row)
    label = file_row["label"]
    n = info["page_count"]
    first = info["page_sizes_in"][0] if info["page_sizes_in"] else (None, None)
    row = {
        "run_id": run["id"],
        "file_id": file_row["id"],
        "segment_id": None,
        "category": label,
        "name": _clean_filename(file_row.get("filename")),
        "storage_path": file_row["storage_path"],
        "page_start": 1,
        "page_end": n,
        "page_count": n,
        "page_meta": _labeled_page_meta(label, info),
        "sheet_width_in": round(first[0], 2) if first[0] else None,
        "sheet_height_in": round(first[1], 2) if first[1] else None,
        "status": "pending",
    }
    return sb.table("plans_reader_documents").insert(row).execute().data[0]


def _mark_file(sb, file_id: str, status: str, error: str | None = None, extra: dict | None = None) -> None:
    patch = {"status": status, "error": (error or None) and str(error)[:500], **(extra or {})}
    sb.table("plans_reader_files").update(patch).eq("id", file_id).execute()


def start(sb, run: dict, *, user: Any) -> dict:
    """Start a created run (docs/PLANS_READER.md 7). Refuses (RunRefused)
    without files, with the AI queue off or over the per-run token limit;
    raises llm.LlmNotConfigured while AI Estimating cannot run (self-hosted
    mode, no OpenAI key) and ai_spend.SpendCapReached when the daily cap
    refuses the estimate, so the router answers with their own codes. Labeled files become documents at once;
    unlabeled ones (and General Drawings) are staged into one splitter job
    (source plans_reader, no project) and dispatched after all are staged.
    The run moves to splitting; with nothing to split the next stage is
    kicked off right away. An unexpected error part way is undone
    (_undo_start: the run goes back to created with a note) and refused
    with 503, so a crash never strands a run in splitting. Returns the run
    row."""
    _refuse_estimator(user)
    s = get_settings()
    run = _load_run(sb, run)
    if run.get("status") != "created":
        raise RunRefused("This run has already started.", 409)
    files = [f for f in _files(sb, run["id"]) if f.get("status") != "failed"]
    if not files:
        raise RunRefused("Add at least one file before starting the run.")
    if not s.llm_queue_enabled:
        raise RunRefused(MSG_QUEUE_OFF, 409)
    reason = llm.unavailable_reason("plans_sub", s)
    if reason:
        raise llm.LlmNotConfigured(reason)
    if not llm.is_configured("plans_sub", s):
        raise llm.LlmNotConfigured(MSG_NOT_CONFIGURED)
    to_split = [f for f in files if _needs_split(f)]
    if to_split and not llm.is_configured("bid_split", s):
        raise RunRefused(MSG_SPLITTER_NOT_CONFIGURED, 409)
    if len(to_split) > s.bid_split_max_files_per_job:
        raise RunRefused(
            f"At most {s.bid_split_max_files_per_job} unlabeled files can be split in one run; "
            "label the others or start a second run."
        )
    est = estimate(sb, run)
    worst = int(est["input_tokens"]) + int(est["escalation_max_input_tokens"])
    if worst > s.plans_reader_max_input_tokens_per_run:
        raise RunRefused(
            f"This run would read about {worst:,} input tokens, over the limit of "
            f"{s.plans_reader_max_input_tokens_per_run:,} per run. Remove or label files to make it smaller.",
            413,
        )
    ai_spend.check_cap(sb, estimated_usd=Decimal(str(est["cost_usd"] or 0)))  # SpendCapReached propagates

    claimed = _cas_status(sb, run["id"], "created", "splitting", {"started_at": _now_iso(), "error": None})
    if claimed is None:
        raise RunRefused("This run has already started.", 409)
    uid = _uid(user)
    known = {str(f["id"]) for f in files}
    progress: dict[str, Any] = {"split_job_id": None, "staged": []}
    try:
        staged = _stage_start(sb, run, files, to_split, known, uid, s, progress)
    except Exception as exc:
        # Anything unexpected part way (a database or storage error outside
        # the per-file guards) must not strand the run in splitting with
        # files that nothing will ever settle: undo what this start did and
        # hand the run back to created so the user can simply start again.
        logger.exception("plans_reader: start of run %s failed part way; rolling back", run["id"])
        _undo_start(sb, run["id"], known, progress)
        raise RunRefused(MSG_START_FAILED, 503) from exc
    if not staged:
        _kick_advance(run["id"], force=True)
    return _load_run(sb, run["id"])


def _stage_start(
    sb,
    run: dict,
    files: list[dict],
    to_split: list[dict],
    known: set[str],
    uid: str | None,
    s,
    progress: dict[str, Any],
) -> list[str]:
    """The body of start() after the run moved to splitting: labeled files
    become documents, unlabeled ones are staged into one splitter job and
    dispatched. Records what it did in `progress` (the split job id and the
    staged split file ids) so _undo_start can take it back. Returns the
    staged split file ids."""
    errors: list[str] = []
    # A file inserted after the snapshot above (an upload that was still
    # converting) was neither estimated nor planned: fail it visibly rather
    # than leave it pending, which would hold the run in splitting for good.
    for f in _files(sb, run["id"]):
        if str(f["id"]) not in known and f.get("status") == "pending":
            progress.setdefault("late", []).append(str(f["id"]))
            _mark_file(sb, f["id"], "failed", MSG_ADDED_DURING_START)
            errors.append(f"{_clean_filename(f.get('filename'))}: {MSG_ADDED_DURING_START}")
    for f in files:
        if _needs_split(f):
            continue
        try:
            _document_from_file(sb, run, f)
            _mark_file(sb, f["id"], "labeled")
        except Exception as exc:  # noqa: BLE001 - one bad file must not strand the run
            logger.exception("plans_reader: labeled file %s could not become a document", f["id"])
            message = str(exc) if isinstance(exc, ValueError) else "The file could not be read."
            _mark_file(sb, f["id"], "failed", message)
            errors.append(f"{_clean_filename(f.get('filename'))}: {message}")

    staged: list[str] = []
    if to_split:
        try:
            model = llm.active_model("bid_split", s)
        except Exception:  # noqa: BLE001 - a label for history only
            model = None
        job = (
            sb.table("bid_split_jobs")
            .insert(
                {
                    "status": "processing",
                    "file_count": len(to_split),
                    "model": model,
                    "created_by": uid,
                    "source": bid_split.SOURCE_PLANS_READER,
                    "project_id": None,
                }
            )
            .execute()
            .data[0]
        )
        progress["split_job_id"] = job["id"]
        sb.table("plans_reader_runs").update({"split_job_id": job["id"]}).eq("id", run["id"]).execute()
        for f in to_split:
            name = _pdf_name(_clean_filename(f.get("filename")))
            try:
                content = storage.download_file(f["storage_path"])
                pages = bid_split.check_upload(name, content, s)
                frow = bid_split.stage_upload(sb, job, name, content, pages, uid)
            except bid_split.UploadRefused as exc:
                _mark_file(sb, f["id"], "failed", str(exc))
                errors.append(str(exc))
                continue
            except Exception:  # noqa: BLE001 - one bad file must not strand the run
                logger.exception("plans_reader: staging file %s for the splitter failed", f["id"])
                _mark_file(sb, f["id"], "failed", "The file could not be staged for splitting.")
                errors.append(f"{name}: the file could not be staged for splitting.")
                continue
            progress["staged"].append(frow["id"])
            _mark_file(sb, f["id"], "splitting", extra={"split_file_id": frow["id"]})
            staged.append(frow["id"])
        for split_file_id in staged:
            bid_split.dispatch(None, split_file_id, uid)
    if errors:
        sb.table("plans_reader_runs").update({"error": _join_errors(errors)}).eq("id", run["id"]).execute()
    return staged


def _undo_start(sb, run_id: str, known: set[str], progress: dict[str, Any]) -> None:
    """Take back a start that failed part way, best effort per step: cancel
    the splitter jobs already queued, drop the split job (its rows and
    objects), remove the documents written, put every file the start saw
    back to pending, then move the run back to created (only from
    splitting, so nothing that moved on is overwritten) with a note.

    A split a worker already picked up cannot be stopped (its model call is
    in flight) and would write its segments under the split job's prefix.
    The split job's row and objects are then kept, not deleted, so what it
    writes still belongs to a job row (source plans_reader, no run points
    at it any more) instead of landing under a prefix nothing owns; the
    job and file ids are logged for cleanup. Its result is ignored: the run
    file no longer carries its split_file_id."""
    ids = sorted(set(known) | set(progress.get("late") or []))
    running: list[str] = []
    for split_file_id in progress.get("staged") or []:
        try:
            job = llm_queue.active_job(llm_queue.JOB_BID_SPLIT, str(split_file_id))
            if job and job.get("status") == "queued" and llm_queue.cancel(str(job["id"])) is None:
                job = llm_queue.active_job(llm_queue.JOB_BID_SPLIT, str(split_file_id))  # claimed meanwhile
            if job and job.get("status") == "running":
                running.append(str(split_file_id))
        except Exception:  # noqa: BLE001 - a split of a rolled-back file finds no run file
            logger.exception("plans_reader: canceling the split of %s failed", split_file_id)
    steps: list[tuple[str, Any]] = [
        ("documents", lambda: sb.table("plans_reader_documents").delete().eq("run_id", run_id).execute()),
        (
            "files",
            lambda: sb.table("plans_reader_files")
            .update({"status": "pending", "error": None, "split_file_id": None})
            .eq("run_id", run_id)
            .in_("id", ids)
            .execute()
            if ids
            else None,
        ),
    ]
    job_id = progress.get("split_job_id")
    if job_id and running:
        logger.warning(
            "plans_reader: undoing the start of run %s kept split job %s (files %s still being split)",
            run_id, job_id, ", ".join(running),
        )
    elif job_id:
        steps.append(("split objects", lambda: storage.delete_bid_split_prefix(str(job_id))))
        steps.append(("split job", lambda: sb.table("bid_split_jobs").delete().eq("id", job_id).execute()))
    for label, step in steps:
        try:
            step()
        except Exception:  # noqa: BLE001 - every other step still runs
            logger.exception("plans_reader: undoing the start of run %s (%s) failed", run_id, label)
    try:
        _cas_status(
            sb, run_id, "splitting", "created",
            {"started_at": None, "split_job_id": None, "error": MSG_START_ROLLED_BACK},
        )
    except Exception:  # noqa: BLE001 - logged; the refusal still reaches the user
        logger.exception("plans_reader: could not return run %s to created", run_id)


# ---------------------------------------------------------------------------
# Split results
# ---------------------------------------------------------------------------


def _document_from_segment(run_id: str, file_row: dict, seg: dict) -> dict:
    meta = seg.get("page_meta") if isinstance(seg.get("page_meta"), list) else None
    page_start = int(seg.get("page_start") or 1)
    page_end = int(seg.get("page_end") or page_start)
    return {
        "run_id": run_id,
        "file_id": file_row["id"],
        "segment_id": seg["id"],
        "category": seg.get("category") if seg.get("category") in CATEGORIES else "other",
        "other_type": seg.get("other_type"),
        "name": agents._clean_text(seg.get("name") or seg.get("filename") or file_row.get("filename"), NAME_MAX_CHARS)
        or "Document",
        "storage_path": seg["storage_path"],
        "page_start": page_start,
        "page_end": page_end,
        "page_count": len(meta) if meta else page_end - page_start + 1,
        "page_meta": meta,
        "status": "pending",
    }


def _dedupe_documents(sb, file_id: str) -> None:
    """Keep one document per segment of a file (the earliest row): two
    settlers racing on the same file may both have inserted."""
    rows = sb.table("plans_reader_documents").select("id, segment_id, created_at").eq("file_id", file_id).execute().data or []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or ""))):
        key = str(row.get("segment_id"))
        if row.get("segment_id") is None:
            continue
        if key in seen:
            sb.table("plans_reader_documents").delete().eq("id", row["id"]).execute()
        else:
            seen.add(key)


def _settle_split_file(sb, file_row: dict) -> bool:
    """Turn one settled split file into documents (done) or a failed run
    file (failed). The caller first claims the file (splitting ->
    FILE_SETTLING, a compare-and-set), so when the hook and a poll settle
    the same file at once only one of them writes its documents; the file
    becomes `split` only after they are written, and FILE_SETTLING counts
    as unsettled, so the run never starts reading while a file's documents
    are still being written. Returns True when this call settled the
    file."""
    if file_row.get("status") != "splitting" or not file_row.get("split_file_id"):
        return False

    def _fail(message: str, expected: str = "splitting") -> bool:
        rows = (
            sb.table("plans_reader_files")
            .update({"status": "failed", "error": message[:500]})
            .eq("id", file_row["id"]).eq("status", expected).execute().data
            or []
        )
        return bool(rows)

    split = _one(sb, "bid_split_files", str(file_row["split_file_id"]))
    if split is None:
        return _fail("The split result for this file is gone.")
    if split.get("status") == "failed":
        return _fail(split.get("error") or "The splitter could not read this file.")
    if split.get("status") != "done":
        return False
    segments = (
        sb.table("bid_split_segments").select("*").eq("file_id", split["id"]).order("sort_order").execute().data
        or []
    )
    if not segments:
        return _fail("The splitter found no documents in this file.")
    state = _claim_settling(sb, str(file_row["id"]))
    if state is None:
        return False  # another caller is settling (or settled) this file
    # The stale-run repair in _self_heal judges a stuck FILE_SETTLING file
    # by the run's age: mark the run as active now.
    _touch(sb, str(file_row["run_id"]), "splitting")
    have = {
        str(d.get("segment_id"))
        for d in sb.table("plans_reader_documents").select("segment_id").eq("file_id", file_row["id"]).execute().data
        or []
    }
    rows = [_document_from_segment(file_row["run_id"], file_row, seg) for seg in segments if str(seg["id"]) not in have]
    try:
        if rows:
            sb.table("plans_reader_documents").insert(rows).execute()
            _dedupe_documents(sb, file_row["id"])
    except Exception:  # noqa: BLE001 - the file fails, the run goes on
        logger.exception("plans_reader: documents for split file %s could not be written", split["id"])
        return _fail("The split documents could not be recorded.", state)
    claimed = (
        sb.table("plans_reader_files").update({"status": "split", "error": None})
        .eq("id", file_row["id"]).eq("status", state).execute().data
        or []
    )
    return bool(claimed)


def _claim_settling(sb, file_id: str) -> str | None:
    """Claim a split file for settling (splitting -> FILE_SETTLING, a
    compare-and-set) and return the state it now holds, or None when
    another caller claimed it first. A database without migration 0153
    refuses 'settling' with a CHECK violation; the claim then uses the
    legacy 'converted' so the deploy order of code and migration does not
    matter."""
    for state in (FILE_SETTLING, FILE_SETTLING_LEGACY):
        try:
            rows = (
                sb.table("plans_reader_files").update({"status": state})
                .eq("id", file_id).eq("status", "splitting").execute().data
                or []
            )
        except Exception as exc:
            if state == FILE_SETTLING and _is_check_violation(exc):
                logger.warning(
                    "plans_reader: 'settling' refused (migration 0153 not applied); using '%s'",
                    FILE_SETTLING_LEGACY,
                )
                continue
            raise
        return state if rows else None
    return None


def _reclaim_stuck_settling(sb, run: dict, files: list[dict]) -> list[dict]:
    """Files a settler claimed and then died on (FILE_SETTLING in a run
    that has not changed for RECOVERY_AFTER_SECONDS) go back to splitting
    so they can be settled again (already written documents are kept and
    not written twice). Returns the files that were handed back."""
    if not _stale(run):
        return []
    back = []
    for f in files:
        if f.get("status") not in FILE_SETTLING_STATES:
            continue
        rows = (
            sb.table("plans_reader_files").update({"status": "splitting"})
            .eq("id", f["id"]).eq("status", f["status"]).execute().data
            or []
        )
        back.extend(rows)
    return back


def _maybe_finish_splitting(sb, run_id: str) -> None:
    run = _one(sb, "plans_reader_runs", run_id)
    if run is None or run.get("status") != "splitting":
        return
    if any(f.get("status") in FILE_UNSETTLED for f in _files(sb, run_id)):
        return
    advance(sb, run_id)


def on_split_file_done(sb_or_file_id: Any, file_id: str | None = None) -> None:
    """bid_split's hook (docs/PLANS_READER.md 7): one split file of a
    plans_reader job settled (done or failed). Called as
    `on_split_file_done(file_id)` by bid_split._notify_plans_reader, or as
    `on_split_file_done(sb, file_id)`. Builds that file's documents, marks
    the run file, and advances the run once every file has settled.
    Idempotent: a second call for the same file finds it settled."""
    if file_id is None:
        sb, split_file_id = get_supabase(), str(sb_or_file_id)
    else:
        sb, split_file_id = sb_or_file_id, str(file_id)
    rows = sb.table("plans_reader_files").select("*").eq("split_file_id", split_file_id).execute().data or []
    for row in rows:
        _settle_split_file(sb, row)
    for run_id in {str(r["run_id"]) for r in rows}:
        _maybe_finish_splitting(sb, run_id)


def on_split_done(sb, split_job_id: str) -> None:
    """Settle every file of one plans_reader split job (the job-level form
    of the hook; also how a poll catches up when a hook was missed)."""
    split_files = sb.table("bid_split_files").select("id").eq("job_id", split_job_id).execute().data or []
    for sf in split_files:
        on_split_file_done(sb, str(sf["id"]))


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def _readable_pages(document: dict, total: int) -> list[int]:
    """The pdf pages of a document to read: its own pages from page_meta
    (prepended cover pages are read in the General document), else all."""
    own = agents._own_pages(document)
    if isinstance(document.get("page_meta"), list):
        return [e["pdf_page"] for e in own if 1 <= e["pdf_page"] <= total]
    return list(range(1, total + 1))


def _plan_reading(sb, run: dict, documents: list[dict], s) -> tuple[dict, list[dict], list[str]]:
    """Download and measure every document, build and plan the ledger, and
    pack the pass-1 groups. Returns (ledger, group rows, problems)."""
    problems: list[str] = []
    mode = run.get("render_mode") or "adaptive"
    pack_docs: dict[str, pack.PackDoc] = {}
    plans_by_key: dict[tuple[str, int], pack.PagePlan] = {}
    usable: list[dict] = []
    for doc in documents:
        if doc.get("category") not in agents.DRAWING_CATEGORIES:
            continue
        doc_id = str(doc["id"])
        try:
            sizes = images.page_sizes_in(storage.download_file(doc["storage_path"]))
        except ValueError as exc:
            problems.append(f"{doc.get('name')}: {exc}")
            sb.table("plans_reader_documents").update({"status": "failed"}).eq("id", doc_id).execute()
            continue
        patch: dict[str, Any] = {}
        if not isinstance(doc.get("page_meta"), list):
            # A drawing set with no splitter page detail (a labeled file from
            # before derived page_meta, or a segment cut before 0152): derive
            # the page list so its sheets reach the ledger's sheet index under
            # the same page numbers the sub agent's labels use.
            derived = agents.derived_page_meta(sizes)
            if derived:
                doc["page_meta"] = derived
                patch["page_meta"] = derived
        pages = _readable_pages(doc, len(sizes))
        doc["_page_sizes_in"] = sizes
        first = sizes[pages[0] - 1] if pages else sizes[0]
        patch.update(
            {
                "page_count": len(sizes),
                "sheet_width_in": round(first[0], 2),
                "sheet_height_in": round(first[1], 2),
                "status": "reading" if pages else "done",
            }
        )
        sb.table("plans_reader_documents").update(patch).eq("id", doc_id).execute()
        doc["page_count"] = len(sizes)
        usable.append(doc)
        if not pages:
            continue
        plans = [
            pack.plan_page(
                p, sizes[p - 1][0], sizes[p - 1][1],
                render_mode=mode,
                tile_ppi=s.plans_reader_tile_ppi,
                max_patches=s.plans_reader_native_max_patches,
            )
            for p in pages
        ]
        pack_docs[doc_id] = pack.PackDoc(doc_id, doc["category"], "drawing", pages=plans, page_count=len(plans))
        for pl in plans:
            plans_by_key[(doc_id, pl.page)] = pl

    files = [d for d in documents if d.get("category") not in agents.DRAWING_CATEGORIES]
    ledger = agents.plan_ledger(
        agents.build_ledger(sb, run, usable + files),
        budget_tokens=s.plans_reader_ledger_max_tokens,
        max_patches=s.plans_reader_native_max_patches,
    )
    ledger_tokens, ledger_images = agents.ledger_cost(ledger)
    prompt = agents.prompt_tokens("sub")
    max_pages = min(
        images.FILE_MAX_PAGES,
        pack.max_file_pages_for_budget(s.plans_reader_group_input_tokens, ledger_tokens, prompt_tokens=prompt),
    )
    for doc in files:
        doc_id = str(doc["id"])
        try:
            pdf = storage.download_file(doc["storage_path"])
            n = images.page_count(pdf)
            chunks = images.pdf_chunks(pdf, max_bytes=s.plans_reader_file_max_bytes, max_pages=max_pages)
        except ValueError as exc:
            problems.append(f"{doc.get('name')}: {exc}")
            sb.table("plans_reader_documents").update({"status": "failed"}).eq("id", doc_id).execute()
            continue
        pack_docs[doc_id] = pack.plan_file_document(
            doc_id, doc["category"], n, len(pdf),
            chunks=[(a, b, len(data)) for a, b, data in chunks],
            max_bytes=s.plans_reader_file_max_bytes, max_pages=max_pages,
        )
        del pdf, chunks
        sb.table("plans_reader_documents").update({"page_count": n, "status": "reading"}).eq("id", doc_id).execute()

    ordered = [pack_docs[str(d["id"])] for d in agents.run_doc_order(documents) if str(d["id"]) in pack_docs]
    groups = pack.pack_groups(
        ordered,
        budget_tokens=s.plans_reader_group_input_tokens,
        ledger_tokens=ledger_tokens,
        prompt_tokens=prompt,
        file_max_bytes=s.plans_reader_file_max_bytes,
        ledger_images=ledger_images,
    )
    rows: list[dict] = []
    for g in groups:
        items = []
        for item in g.items:
            item = dict(item)
            pl = plans_by_key.get((str(item["document_id"]), int(item["page"])))
            if pl is not None:
                item["w_in"] = round(pl.w_in, 3)
                item["h_in"] = round(pl.h_in, 3)
            items.append(item)
        rows.append(
            {
                "run_id": run["id"],
                "kind": "sub",
                "category": g.category,
                "group_index": g.group_index,
                "pass": 1,
                "pages": items,
                "est_input_tokens": int(g.est_input_tokens),
                "status": "queued",
            }
        )
    return ledger, rows, problems


def _job_type_for(group: dict) -> str:
    """The llm_jobs type a group runs under (and is polled by)."""
    kind = group.get("kind")
    if kind == "main":
        return JOB_MAIN
    if kind == "modify":
        return JOB_MODIFY
    return JOB_AGENT


def _enqueue_group(sb, group: dict, run: dict, *, next_attempt_at: str | None = None) -> None:
    """One llm_jobs job per group (target = group id, no project); the job
    id is kept on the group. The payload carries the group's input estimate
    so the pacing of later batches can see the load this job puts on its
    minute; `next_attempt_at` (ISO, None = now) is the paced start. A failed
    enqueue leaves the group queued for resume and is logged."""
    try:
        job = llm_queue.enqueue(
            _job_type_for(group),
            target_id=str(group["id"]),
            project_id=None,
            payload={
                "group_id": str(group["id"]),
                "run_id": str(run["id"]),
                "est_input_tokens": int(group.get("est_input_tokens") or 0),
            },
            created_by=run.get("created_by"),
            next_attempt_at=next_attempt_at,
        )
    except Exception:  # noqa: BLE001 - resume re-enqueues queued groups
        logger.exception("plans_reader: enqueue of group %s failed", group.get("id"))
        return
    if job and job.get("id"):
        sb.table("plans_reader_groups").update({"llm_job_id": job["id"]}).eq("id", group["id"]).execute()


def _scheduled_load(sb, now: datetime) -> dict[int, int]:
    """Estimated input tokens per pacing window (0 = the window starting
    now) of the AI Estimating batch jobs already queued or running across
    every run: a job counts in the window its next_attempt_at falls in, and
    one due within the last PACING_LOOKBACK_SECONDS counts in window 0 (its
    request goes out about now). Jobs without an estimate in their payload
    (enqueued before pacing) count as nothing. Best effort: an unreadable
    queue is treated as empty."""
    cutoff = (now - timedelta(seconds=PACING_LOOKBACK_SECONDS)).isoformat()
    try:
        rows = (
            sb.table("llm_jobs")
            .select("id, status, next_attempt_at, payload")
            .in_("job_type", [JOB_AGENT, JOB_MAIN])
            .in_("status", list(GROUP_ACTIVE))
            .gte("next_attempt_at", cutoff)
            .limit(PACING_SCAN_MAX_JOBS)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001 - pacing is a courtesy; the queue retries a 429
        logger.exception("plans_reader: reading the queue for pacing failed")
        return {}
    load: dict[int, int] = {}
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        tokens = agents._as_int(payload.get("est_input_tokens")) or 0
        if tokens <= 0:
            continue
        age = _age_seconds(row.get("next_attempt_at"))
        ahead = -age if age is not None else 0.0
        window = max(0, int(ahead // PACING_WINDOW_SECONDS))
        load[window] = load.get(window, 0) + tokens
    return load


def _paced_starts(sb, groups: list[dict], s) -> list[str | None]:
    """When each of `groups` (in order) may start, so the estimated input
    tokens of the AI Estimating jobs scheduled in any PACING_WINDOW_SECONDS
    window stay under PLANS_READER_TOKENS_PER_MINUTE (the account's
    tokens-per-minute limit is shared by every run; the load already
    scheduled is read from the queue). Windows are filled in order, a group
    never starts before an earlier one, and a group larger than the whole
    budget gets a window to itself. Returns an ISO next_attempt_at per group,
    None for the window starting now (the job is due at once). With pacing
    off (0), or a batch that fits the current window, every entry is None."""
    budget = int(getattr(s, "plans_reader_tokens_per_minute", 0) or 0)
    if budget <= 0 or not groups:
        return [None] * len(groups)
    now = datetime.now(timezone.utc)
    load = _scheduled_load(sb, now)
    out: list[str | None] = []
    window = 0
    for g in groups:
        tokens = max(0, int(g.get("est_input_tokens") or 0))
        while load.get(window, 0) and load[window] + tokens > budget:
            window += 1
        load[window] = load.get(window, 0) + tokens
        out.append(None if window == 0 else (now + timedelta(seconds=window * PACING_WINDOW_SECONDS)).isoformat())
    return out


def _enqueue_groups(sb, groups: list[dict], run: dict) -> None:
    """Enqueue a stage's groups with paced start times (_paced_starts), so a
    600-page run does not send a burst of 240k-token requests into the
    account's per-minute token limit at once."""
    if not groups:
        return
    starts = _paced_starts(sb, groups, get_settings())
    for g, when in zip(groups, starts, strict=True):
        _enqueue_group(sb, g, run, next_attempt_at=when)


def _group_key(group: dict) -> tuple:
    """The identity a stage's added group is deduplicated on."""
    kind = group.get("kind")
    if kind == "rollup":
        return ("rollup", group.get("category"))
    if kind == "main":
        # The consolidation and its one lower-effort retry are separate.
        return ("main", int(group.get("pass") or MAIN_PASS))
    if kind == "escalation":
        items = group.get("pages") or [{}]
        first = items[0] if items else {}
        return ("escalation", group.get("category"), str(first.get("document_id")), int(first.get("page") or 0))
    return ("sub", group.get("category"), group.get("group_index"), group.get("pass"))


def _order_key(group: dict) -> tuple:
    return (str(group.get("created_at") or ""), str(group.get("id") or ""))


def _dedupe_rank(group: dict) -> tuple:
    """Which row of one identity survives: a row already past `queued`
    (its job is running, or it is done) first, then the earliest. Ranking
    a started row first keeps a commit-order inversion (a later-committed
    row with an earlier created_at) from queuing a second call next to one
    already running."""
    return (group.get("status") == "queued",) + _order_key(group)


def _delete_if_queued(sb, group_id: str) -> bool:
    rows = sb.table("plans_reader_groups").delete().eq("id", group_id).eq("status", "queued").execute().data or []
    return bool(rows)


def _insert_deduped(sb, run: dict, rows: list[dict]) -> list[dict]:
    """Insert stage-added groups, then keep one row per identity (another
    worker may have inserted the same group a moment earlier): a started
    row first, else the earliest (_dedupe_rank). Every queued loser is
    deleted before anything is enqueued, not only this caller's: a racer
    that saw its own row alone may have enqueued it, and that job then
    finds no group and ends without a model call. A loser that started
    running between the read and its delete outranks a queued winner,
    which is deleted instead. Returns this caller's winning rows that are
    still queued (to enqueue)."""
    if not rows:
        return []
    inserted = sb.table("plans_reader_groups").insert(rows).execute().data or []
    kinds = sorted({r.get("kind") for r in inserted})
    current = (
        sb.table("plans_reader_groups").select("*").eq("run_id", run["id"]).in_("kind", kinds).execute().data or []
    )
    keys = {_group_key(g) for g in inserted}
    by_key: dict[tuple, list[dict]] = {}
    for g in current:
        key = _group_key(g)
        if key in keys:
            by_key.setdefault(key, []).append(g)
    mine_ids = {g["id"] for g in inserted}
    out: list[dict] = []
    for group_rows in by_key.values():
        ranked = sorted(group_rows, key=_dedupe_rank)
        winner = ranked[0]
        for loser in ranked[1:]:
            if loser.get("status") != "queued":
                continue  # two started rows: nothing left to prevent
            if _delete_if_queued(sb, loser["id"]):
                continue
            # The loser started meanwhile: it is the one to keep.
            if winner.get("status") == "queued" and _delete_if_queued(sb, winner["id"]):
                winner = {**loser, "status": "running"}
        if winner["id"] in mine_ids and winner.get("status") == "queued":
            out.append(winner)
    return out


def _flags_by_sheet(group: dict) -> dict[tuple[str, int], list[dict]]:
    """Resolution flags of a done sub group, per (document_id, page) of the
    group's own image pages."""
    output = group.get("output") if isinstance(group.get("output"), dict) else {}
    own = {
        (str(i.get("document_id")), int(i.get("page") or 0))
        for i in group.get("pages") or []
        if isinstance(i, dict) and i.get("kind") != "file"
    }
    out: dict[tuple[str, int], list[dict]] = {}
    for page in output.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for flag in page.get("resolution_flags") or []:
            if not isinstance(flag, dict):
                continue
            doc = flag.get("document_id") or page.get("document_id")
            pg = agents._as_int(flag.get("page")) or agents._as_int(page.get("page")) or 0
            key = (str(doc), pg)
            if key in own:
                out.setdefault(key, []).append(flag)
    return out


def _escalation_tiles(w_in: float, h_in: float, regions: list[str], ppi: int, max_patches: int) -> tuple[list[list[float]], int]:
    """Page-fraction tiles at `ppi` covering the flagged regions (the whole
    sheet when a region says so or more than four were flagged), each under
    the patch cap, and their image tokens."""
    w_px, h_px = images.px_for_inches(w_in, ppi), images.px_for_inches(h_in, ppi)
    boxes = []
    for region in regions:
        try:
            boxes.append(images.region_to_box(region, w_px, h_px))
        except ValueError:
            continue
    whole = (0, 0, w_px, h_px)
    if not boxes or len(boxes) > 4 or whole in boxes:
        boxes = [whole]
    fractions: list[list[float]] = []
    tokens = 0
    for box in boxes:
        for tile in images.plan_region_tiles(w_px, h_px, box, max_patches=max_patches):
            frac = images.box_to_fractions(tile, w_px, h_px)
            if frac in fractions:
                continue
            fractions.append(frac)
            tokens += images.image_tokens(tile[2] - tile[0], tile[3] - tile[1])
    return fractions, tokens


def _escalation_rows(run: dict, groups: list[dict], category: str, s, budget_left: int) -> tuple[list[dict], int]:
    """Escalation groups (pass 2) for the flagged sheets of one category
    whose pass-1 groups are all settled, within the run's escalation
    budget. Only sheets read below the tile ppi qualify (a tiles_260 run
    is already at full resolution)."""
    if s.plans_reader_escalation_max_fraction <= 0 or budget_left <= 0:
        return [], budget_left
    tile_ppi = s.plans_reader_tile_ppi
    existing = {
        _group_key(g)[2:] for g in groups if g.get("kind") == "escalation" and g.get("category") == category
    }
    seq = sum(1 for g in groups if g.get("kind") == "escalation" and g.get("category") == category)
    ledger_text_tokens = int((run.get("ledger") or {}).get("text_tokens") or 0)
    overhead = ledger_text_tokens + agents.prompt_tokens("escalation") + 2_000
    rows: list[dict] = []
    subs = sorted(
        (g for g in groups if g.get("kind") == "sub" and g.get("category") == category and g.get("status") == "done"),
        key=lambda g: int(g.get("group_index") or 0),
    )
    for g in subs:
        items = {
            (str(i.get("document_id")), int(i.get("page") or 0)): i
            for i in g.get("pages") or []
            if isinstance(i, dict) and i.get("kind") != "file"
        }
        for key, flags in _flags_by_sheet(g).items():
            if key in existing:
                continue
            item = items.get(key) or {}
            if int(item.get("ppi") or 0) >= tile_ppi or not item.get("w_in") or not item.get("h_in"):
                continue
            regions = [agents._clean_text(f.get("region"), 60) for f in flags]
            reasons = [agents._clean_text(f.get("reason"), 300) for f in flags]
            try:
                tiles, tokens = _escalation_tiles(
                    float(item["w_in"]), float(item["h_in"]), regions, tile_ppi, s.plans_reader_native_max_patches
                )
            except ValueError:
                continue
            est = tokens + overhead
            if not tiles or est > budget_left:
                continue
            budget_left -= est
            existing.add(key)
            rows.append(
                {
                    "run_id": run["id"],
                    "kind": "escalation",
                    "category": category,
                    "group_index": seq,
                    "pass": 2,
                    "pages": [
                        {
                            "document_id": key[0],
                            "page": key[1],
                            "ppi": tile_ppi,
                            "detail": "original",
                            "tiles": tiles,
                            "kind": "image",
                            "w_in": item.get("w_in"),
                            "h_in": item.get("h_in"),
                            "regions": regions,
                            "reasons": reasons,
                            "source_group_id": str(g["id"]),
                        }
                    ],
                    "est_input_tokens": est,
                    "status": "queued",
                }
            )
            seq += 1
    return rows, budget_left


def _merge_outputs(outputs: list[dict], category: str) -> dict:
    """Concatenate several group outputs of one category (when there is no
    roll-up output to use): arrays joined in group order, summaries joined."""
    merged: dict[str, Any] = {
        "category": category, "summary": "", "pages": [], "counts": [], "runs": [],
        "scope": [], "specs": [], "schedules": [], "details": [], "questions": [],
    }
    summaries = []
    for out in outputs:
        for key in ("pages", "counts", "runs", "scope", "specs", "schedules", "details", "questions"):
            merged[key].extend(x for x in out.get(key) or [] if isinstance(x, dict))
        if out.get("summary"):
            summaries.append(str(out["summary"]))
    merged["summary"] = " ".join(summaries)
    return merged


def _category_output(category: str, groups: list[dict]) -> tuple[dict | None, list[str]]:
    """The stored category output: the roll-up's answer, else the one
    group's, else the groups merged; with the escalation patches and the
    flags nobody re-read. Returns (output or None, source group ids)."""
    subs = sorted(
        (g for g in groups if g.get("kind") == "sub" and g.get("category") == category),
        key=lambda g: int(g.get("group_index") or 0),
    )
    done = [g for g in subs if g.get("status") == "done" and isinstance(g.get("output"), dict)]
    if not done:
        return None, []
    rollup = next(
        (
            g for g in sorted(groups, key=_order_key)
            if g.get("kind") == "rollup" and g.get("category") == category
            and g.get("status") == "done" and isinstance(g.get("output"), dict)
        ),
        None,
    )
    if rollup is not None:
        base = dict(rollup["output"])
        sources = [str(rollup["id"])] + [str(g["id"]) for g in done]
    elif len(done) == 1:
        base = dict(done[0]["output"])
        sources = [str(done[0]["id"])]
    else:
        base = _merge_outputs([g["output"] for g in done], category)
        sources = [str(g["id"]) for g in done]
    escalations = [
        g for g in sorted(groups, key=_order_key)
        if g.get("kind") == "escalation" and g.get("category") == category
    ]
    patched = set()
    patches = []
    for g in escalations:
        if g.get("status") == "done" and isinstance(g.get("output"), dict):
            patches.append(g["output"])
            sources.append(str(g["id"]))
            for item in g.get("pages") or []:
                patched.add((str(item.get("document_id")), int(item.get("page") or 0)))
    unresolved = []
    for g in done:
        for key, flags in _flags_by_sheet(g).items():
            if key in patched:
                continue
            for flag in flags:
                unresolved.append(
                    {
                        "document_id": key[0],
                        "doc_index": flag.get("doc_index"),
                        "page": key[1],
                        "region": flag.get("region"),
                        "reason": flag.get("reason"),
                    }
                )
    base["escalation_patches"] = patches
    base["unresolved_flags"] = unresolved
    base["source"] = {
        "groups": len(subs),
        "failed_groups": sum(1 for g in subs if g.get("status") != "done"),
        "rolled_up": rollup is not None,
    }
    return base, sources


def _write_output(sb, run_id: str, scope: str, category: str | None, output: dict, sources: list[str]) -> None:
    try:
        sb.table("plans_reader_outputs").insert(
            {
                "run_id": run_id,
                "scope": scope,
                "category": category,
                "version": 1,
                "output": output,
                "source_group_ids": sources,
            }
        ).execute()
    except Exception as exc:
        if not _is_unique_violation(exc):
            raise


def _update_counters(sb, run_id: str, groups: list[dict]) -> None:
    pipeline = [g for g in groups if g.get("kind") in PIPELINE_KINDS]
    try:
        sb.table("plans_reader_runs").update(
            {
                "groups_total": len(pipeline),
                "groups_done": sum(1 for g in pipeline if g.get("status") == "done"),
                "groups_failed": sum(1 for g in pipeline if g.get("status") == "failed"),
            }
        ).eq("id", run_id).execute()
    except Exception:  # noqa: BLE001 - display counters only
        logger.exception("plans_reader: counters for run %s failed", run_id)


def _planning_claim_live(run: dict) -> bool:
    """True while another caller holds the planning claim (and it is not
    older than RECOVERY_AFTER_SECONDS, which means its holder died)."""
    ledger = run.get("ledger")
    claim = ledger.get(PLANNING_CLAIM_KEY) if isinstance(ledger, dict) else None
    if not isinstance(claim, dict):
        return False
    age = _age_seconds(claim.get("at"))
    return age is not None and age <= RECOVERY_AFTER_SECONDS


def _claim_planning(sb, run: dict) -> dict | None:
    """Take the splitting -> reading planning step for this caller before
    the heavy work (every document downloaded and measured): a
    compare-and-set on the row's updated_at (any write bumps it, here and
    in the database's trigger) that leaves a claim in the ledger column, so
    a caller that read the run before the claim loses the CAS and one that
    reads it after sees the live claim. Planning on several workers at
    once would download a whole set per worker. A CAS lost to an unrelated
    write (no claim seen on the re-read) is tried again, so that write
    cannot leave the run waiting for the next poll. Returns the claimed row
    or None."""
    for _attempt in range(3):
        if run.get("status") != "splitting" or _planning_claim_live(run):
            return None
        stamp = run.get("updated_at")
        if not isinstance(stamp, str) or not stamp:
            return None
        now = _now_iso()
        rows = (
            sb.table("plans_reader_runs")
            .update({"ledger": {PLANNING_CLAIM_KEY: {"at": now}}, "updated_at": now})
            .eq("id", run["id"])
            .eq("status", "splitting")
            .eq("updated_at", stamp)
            .execute()
            .data
            or []
        )
        if rows:
            return rows[0]
        fresh = _one(sb, "plans_reader_runs", str(run["id"]))
        if fresh is None:
            return None
        run = fresh
    return None


def _release_planning(sb, run_id: str) -> None:
    """Drop this caller's planning claim after an unexpected error, so the
    next event plans again at once instead of after the claim expires."""
    try:
        sb.table("plans_reader_runs").update({"ledger": None}).eq("id", run_id).eq("status", "splitting").execute()
    except Exception:  # noqa: BLE001 - the claim expires on its own
        logger.exception("plans_reader: releasing the planning claim of run %s failed", run_id)


def _advance_splitting(sb, run: dict, s) -> None:
    if any(f.get("status") in FILE_UNSETTLED for f in _files(sb, run["id"])):
        return
    documents = [d for d in _documents(sb, run["id"]) if d.get("status") != "failed"]
    if not documents:
        _cas_status(sb, run["id"], "splitting", "failed", {"error": MSG_NO_DOCUMENTS, "finished_at": _now_iso()})
        return
    claimed = _claim_planning(sb, run)
    if claimed is None:
        return  # another caller is planning this run
    run = claimed
    try:
        ledger, rows, problems = _plan_reading(sb, run, documents, s)
    except ValueError as exc:
        _cas_status(sb, run["id"], "splitting", "failed", {"error": str(exc)[:500], "finished_at": _now_iso()})
        return
    except BaseException:
        _release_planning(sb, run["id"])
        raise
    if not rows:
        _cas_status(
            sb, run["id"], "splitting", "failed",
            {"error": _join_errors([MSG_NO_DOCUMENTS] + problems), "finished_at": _now_iso()},
        )
        return
    prior = [run.get("error")] if run.get("error") else []
    won = _cas_status(
        sb,
        run["id"],
        "splitting",
        "reading",
        {
            "ledger": ledger,
            "documents_total": len(documents),
            "groups_total": len(rows),
            "error": _join_errors(prior + problems),
        },
    )
    if won is None:
        return
    inserted = sb.table("plans_reader_groups").insert(rows).execute().data or []
    _enqueue_groups(sb, inserted, won)


def _advance_reading(sb, run: dict, s) -> None:
    groups = [g for g in _groups(sb, run["id"]) if g.get("kind") in PIPELINE_KINDS]
    subs = [g for g in groups if g.get("kind") == "sub"]
    if not subs:
        # Either the planning winner is inserting its groups this very moment
        # (leave it alone), or it died between its status change and its
        # insert long ago: hand the run back to splitting (one winner) and
        # plan again.
        if _stale(run) and _cas_status(sb, run["id"], "reading", "splitting") is not None:
            advance(sb, run["id"])
        return
    _update_counters(sb, run["id"], groups)
    categories = list(dict.fromkeys(g.get("category") for g in subs))
    total_pass1 = sum(int(g.get("est_input_tokens") or 0) for g in subs)
    budget = int(total_pass1 * float(s.plans_reader_escalation_max_fraction))
    budget -= sum(int(g.get("est_input_tokens") or 0) for g in groups if g.get("kind") == "escalation")
    new_rows: list[dict] = []
    for category in categories:
        cat_subs = [g for g in subs if g.get("category") == category]
        if any(g.get("status") in GROUP_ACTIVE for g in cat_subs):
            continue
        done = [g for g in cat_subs if g.get("status") == "done"]
        has_rollup = any(g.get("kind") == "rollup" and g.get("category") == category for g in groups)
        if len(done) > 1 and not has_rollup:
            est = sum(len(agents._json(g.get("output") or {})) for g in done) // agents.CHARS_PER_TOKEN
            est += int((run.get("ledger") or {}).get("text_tokens") or 0) + agents.prompt_tokens("rollup")
            new_rows.append(
                {
                    "run_id": run["id"],
                    "kind": "rollup",
                    "category": category,
                    "group_index": 0,
                    "pass": 1,
                    "pages": [],
                    "est_input_tokens": int(est),
                    "status": "queued",
                }
            )
        esc, budget = _escalation_rows(run, groups, category, s, budget)
        new_rows.extend(esc)
    if new_rows:
        _enqueue_groups(sb, _insert_deduped(sb, run, new_rows), run)
        return
    if any(g.get("status") in GROUP_ACTIVE for g in groups):
        return
    won = _cas_status(sb, run["id"], "reading", "consolidating")
    if won is None:
        return
    _write_outputs_and_queue_main(sb, won, groups)


def _write_outputs_and_queue_main(sb, run: dict, groups: list[dict]) -> None:
    """Write every category output (idempotent: a version already written
    is kept), then insert and enqueue the main group with its input
    estimate; the run fails when no category produced an output. Run by
    the reading -> consolidating winner, and again by the stale repair when
    that winner died part way (some outputs missing, no main group)."""
    pipeline = [g for g in groups if g.get("kind") in PIPELINE_KINDS]
    categories = list(dict.fromkeys(g.get("category") for g in pipeline if g.get("kind") == "sub"))
    written = 0
    errors = [run.get("error")] if run.get("error") else []
    for category in categories:
        output, sources = _category_output(category, pipeline)
        if output is None:
            note = f"{agents.category_label(category)}: every reading step failed."
            if note not in (run.get("error") or ""):
                errors.append(note)
            continue
        _write_output(sb, run["id"], "category", category, output, sources)
        written += 1
    if not written:
        _cas_status(
            sb, run["id"], "consolidating", "failed",
            {"error": _join_errors([MSG_ALL_GROUPS_FAILED] + errors), "finished_at": _now_iso()},
        )
        return
    est = sum(
        len(agents._json(o.get("output") or {}))
        for o in sb.table("plans_reader_outputs").select("output").eq("run_id", run["id"]).eq("scope", "category").execute().data
        or []
    ) // agents.CHARS_PER_TOKEN
    est += int((run.get("ledger") or {}).get("text_tokens") or 0) + agents.prompt_tokens("main")
    if _join_errors(errors) != (run.get("error") or None):
        sb.table("plans_reader_runs").update({"error": _join_errors(errors)}).eq("id", run["id"]).execute()
    main_row = {
        "run_id": run["id"],
        "kind": "main",
        "category": None,
        "group_index": 0,
        "pass": 1,
        "pages": [],
        "est_input_tokens": int(est),
        "status": "queued",
    }
    _enqueue_groups(sb, _insert_deduped(sb, run, [main_row]), run)


def _advance_consolidating(sb, run: dict, s) -> None:
    groups = _groups(sb, run["id"])
    mains = sorted((g for g in groups if g.get("kind") == "main"), key=_order_key)
    if not mains:
        # The consolidating winner is still writing the category outputs
        # (leave it alone), or it died before inserting the main group:
        # write whatever outputs it did not get to, then queue the main.
        if not _stale(run):
            return
        _write_outputs_and_queue_main(sb, run, groups)
        return
    first = next((g for g in mains if int(g.get("pass") or MAIN_PASS) < MAIN_RETRY_PASS), mains[0])
    retry = next((g for g in mains if int(g.get("pass") or MAIN_PASS) >= MAIN_RETRY_PASS), None)
    if first.get("status") in GROUP_ACTIVE:
        return
    chosen = first if _main_answered(first) else None
    if chosen is None:
        # The consolidation failed for good (the queue's own retries are
        # spent): retry it once at a reasoning effort one step lower (a
        # cut-off or timed-out answer usually fits then) before the run
        # fails. Both attempts keep their plans_reader_calls rows.
        if retry is None:
            if _queue_main_retry(sb, run, first):
                return
            error = f"The consolidation step failed: {first.get('error') or 'no answer'}"
        elif retry.get("status") in GROUP_ACTIVE:
            return
        elif _main_answered(retry):
            chosen = retry
        else:
            effort = agents.group_reasoning_effort(run, retry)
            error = (
                f"The consolidation step failed: {first.get('error') or 'no answer'}. "
                f"The retry at {effort} reasoning effort failed too: {retry.get('error') or 'no answer'}"
            )
        if chosen is None:
            prior = [run.get("error")] if run.get("error") else []
            _cas_status(
                sb, run["id"], "consolidating", "failed",
                {"error": _join_errors(prior + [error]), "finished_at": _now_iso()},
            )
            return
    won = _cas_status(sb, run["id"], "consolidating", "rendering")
    if won is None:
        return
    _finish_with_main(sb, won, chosen, groups)


def _main_answered(group: dict) -> bool:
    return group.get("status") == "done" and isinstance(group.get("output"), dict)


def _queue_main_retry(sb, run: dict, failed_main: dict) -> bool:
    """Insert and enqueue the one retry of a failed main group (pass 2, run
    by plans_reader_agents at the run's reasoning effort one step lower) and
    note it on the run. False when there is no lower effort to try (the run
    then fails). Deduplicated like every stage-added group, so two workers
    seeing the failure queue one retry."""
    base = agents.group_reasoning_effort(run, failed_main)
    lower = agents.lower_reasoning_effort(base)
    if lower is None:
        return False
    note = (
        f"The consolidation step failed at {base} reasoning effort "
        f"({agents._clean_text(failed_main.get('error') or 'no answer', 200)}); retrying once at {lower}."
    )
    if note not in (run.get("error") or ""):
        prior = [run.get("error")] if run.get("error") else []
        try:
            sb.table("plans_reader_runs").update({"error": _join_errors(prior + [note])}).eq("id", run["id"]).eq(
                "status", "consolidating"
            ).execute()
        except Exception:  # noqa: BLE001 - the note is informational; the retry still runs
            logger.exception("plans_reader: noting the consolidation retry of run %s failed", run["id"])
    row = {
        "run_id": run["id"],
        "kind": "main",
        "category": None,
        "group_index": 0,
        "pass": MAIN_RETRY_PASS,
        "pages": [],
        "est_input_tokens": int(failed_main.get("est_input_tokens") or 0),
        "status": "queued",
    }
    _enqueue_groups(sb, _insert_deduped(sb, run, [row]), run)
    return True


_RETRY_NOTE_HEAD = "The consolidation step failed at "
_RETRY_NOTE_MARK = "; retrying once at "
_RETRY_SUCCESS_MARK = "; the retry at "


def _settle_retry_note(run: dict, main: dict, groups: list[dict]) -> str | None:
    """The run's error with the consolidation-retry note (_queue_main_retry)
    settled once the retry's answer is used: "retrying once at X" becomes a
    sentence saying the retry at X succeeded. Unchanged when the first
    consolidation answered."""
    error = run.get("error") or None
    if not error or int(main.get("pass") or MAIN_PASS) < MAIN_RETRY_PASS:
        return error
    first = next(
        (g for g in groups if g.get("kind") == "main" and int(g.get("pass") or MAIN_PASS) < MAIN_RETRY_PASS),
        None,
    )
    base = agents.group_reasoning_effort(run, first) if first else None
    lower = agents.group_reasoning_effort(run, main)
    parts = []
    for part in error.split(" | "):
        if part.startswith(_RETRY_NOTE_HEAD) and _RETRY_NOTE_MARK in part:
            at = f" at {base} reasoning effort" if base else ""
            part = f"The consolidation step failed{at}{_RETRY_SUCCESS_MARK}{lower} succeeded."
        parts.append(part)
    return _join_errors(parts)


def _strip_retry_success(error: str | None) -> str | None:
    """A run that ends `done` shows no error: the settled retry note (see
    _settle_retry_note) is dropped, other notes stay."""
    if not error:
        return None
    parts = [
        p for p in error.split(" | ")
        if not (p.startswith("The consolidation step failed") and _RETRY_SUCCESS_MARK in p and p.endswith(" succeeded."))
    ]
    return _join_errors(parts)


def _finish_with_main(sb, run: dict, main: dict, groups: list[dict]) -> None:
    """Store the main output, render every deliverable (each failure
    recorded, the rest still rendered) and close the run. After a stale
    rendering repair the deliverables the dead worker already saved are
    kept, not rendered a second time. The run's updated_at is bumped after
    each deliverable so the repair leaves a slow but live render alone."""
    output = main["output"]
    _write_output(sb, run["id"], "main", None, output, [str(main["id"])])
    done_kinds = {
        str(r.get("kind"))
        for r in sb.table("plans_reader_deliverables").select("kind").eq("run_id", run["id"]).execute().data or []
    }
    _rows, render_errors = render_all_deliverables(
        sb, run, output, skip_kinds=done_kinds, heartbeat=lambda: _touch(sb, run["id"], "rendering")
    )
    # Only the main group whose answer was used counts: a failed first
    # consolidation that its lower-effort retry replaced is neither a group
    # of the result nor a missing reading step, so groups_total stays equal
    # to groups_done + groups_failed.
    pipeline = [
        g for g in groups
        if g.get("kind") in PIPELINE_KINDS and (g.get("kind") != "main" or str(g.get("id")) == str(main.get("id")))
    ]
    failed = [g for g in pipeline if g.get("status") == "failed"]
    prior = _settle_retry_note(run, main, groups)
    errors = [prior] if prior else []
    if failed:
        errors.append(f"{len(failed)} reading step(s) failed; their pages are missing from the outputs.")
    errors.extend(render_errors)
    final = "done_with_errors" if (failed or render_errors) else "done"
    _cas_status(
        sb,
        run["id"],
        "rendering",
        final,
        {
            "error": _join_errors(errors) if final != "done" else (_strip_retry_success(prior) or None),
            "finished_at": _now_iso(),
            "groups_total": len(pipeline),
            "groups_done": sum(1 for g in pipeline if g.get("status") == "done"),
            "groups_failed": len(failed),
        },
    )
    try:
        sb.table("plans_reader_documents").update({"status": "done"}).eq("run_id", run["id"]).eq(
            "status", "reading"
        ).execute()
        recompute_totals(sb, run["id"])
    except Exception:  # noqa: BLE001 - the run is closed either way
        logger.exception("plans_reader: closing bookkeeping for run %s failed", run["id"])


def advance(sb, run_id: str) -> None:
    """The run's state machine (docs/PLANS_READER.md 7), idempotent and safe
    to call from several workers at once:

    - splitting -> reading once every file has settled: documents measured,
      the ledger built and planned, pass-1 groups packed, inserted and
      enqueued (one plans_reader_agent job each);
    - reading: when every pass-1 group of a category has settled, a roll-up
      group (more than one done group) and escalation groups (flagged
      sheets, within the escalation budget) are added; when every group has
      settled, the category outputs are written and the main group is
      enqueued (reading -> consolidating);
    - consolidating -> rendering when the main group is done: the main
      output is stored and every deliverable rendered, then done or
      done_with_errors; a main group that failed for good is retried once
      at a reasoning effort one step lower (pass 2), and the run fails only
      when that retry fails too (or no lower effort exists);
    - repairs, each only after RECOVERY_AFTER_SECONDS without a change to
      the run row: reading with no groups goes back to splitting,
      consolidating with no main group writes the missing outputs and
      queues it, rendering goes back to consolidating and finishes again.

    Every transition is a compare-and-set on the run status; the loser of a
    race returns without writing."""
    s = get_settings()
    run = _one(sb, "plans_reader_runs", str(run_id))
    if run is None:
        return
    status = run.get("status")
    if status == "splitting":
        _advance_splitting(sb, run, s)
    elif status == "reading":
        _advance_reading(sb, run, s)
    elif status == "consolidating":
        _advance_consolidating(sb, run, s)
    elif status == "rendering":
        _advance_rendering(sb, run, s)


def _advance_rendering(sb, run: dict, s) -> None:
    """A run left in rendering by a worker that died (a deploy, a crash, an
    error outside the per-deliverable guard): rendering happens inline in
    one worker and bumps updated_at after every deliverable, so a run that
    has not changed for RECOVERY_AFTER_SECONDS lost it. One caller hands it
    back to consolidating and finishes it again (the deliverables already
    saved are kept). A younger run is left to its worker."""
    if not _stale(run):
        return
    if _cas_status(sb, run["id"], "rendering", "consolidating") is None:
        return
    fresh = _one(sb, "plans_reader_runs", str(run["id"]))
    if fresh is not None:
        _advance_consolidating(sb, fresh, s)


def resume(sb, run: dict, *, user: Any = None) -> dict:
    """Continue a run the spend cap paused (over_cap), or nudge one that
    stalled in reading, consolidating or rendering: the run goes back to
    the stage it was in, every queued group is enqueued again (an active
    job is reused), then advance runs. The cap must allow it, except for a
    rendering run (rendering makes no model call). A rendering run is
    resumed only once it has not changed for RECOVERY_AFTER_SECONDS: a
    younger one still has a worker rendering, and taking it over would
    render every deliverable twice."""
    run = _load_run(sb, run)
    status = run.get("status")
    if status not in RESUMABLE_STATUSES:
        raise RunRefused("Only a paused or stalled run can be resumed.", 409)
    if status == "rendering":
        if not _stale(run):
            raise RunRefused("This run is still saving its files. Give it a few minutes.", 409)
        if _cas_status(sb, run["id"], "rendering", "consolidating") is None:
            raise RunRefused("This run changed while resuming; refresh and try again.", 409)
        advance(sb, run["id"])
        return _load_run(sb, run["id"])
    ai_spend.check_cap(sb)  # SpendCapReached propagates (the router answers 409 with its code)
    groups = _groups(sb, run["id"])
    if status == "over_cap":
        target = "consolidating" if any(g.get("kind") == "main" for g in groups) else "reading"
        if _cas_status(sb, run["id"], "over_cap", target, {"error": None}) is None:
            raise RunRefused("This run changed while resuming; refresh and try again.", 409)
    fresh = _load_run(sb, run["id"])
    _enqueue_groups(
        sb, [g for g in groups if g.get("status") == "queued" and g.get("kind") in PIPELINE_KINDS], fresh
    )
    advance(sb, run["id"])
    return _load_run(sb, run["id"])


def _cancel_split_jobs(sb, run: dict) -> None:
    """Cancel the queued splitter jobs of a canceled run's files, so their
    model calls (which the AI Estimating cap does not count) are not made.
    A file already being split finishes; its result is discarded because
    the run is canceled. Best effort."""
    rows = [f for f in _files(sb, run["id"]) if f.get("status") == "splitting" and f.get("split_file_id")]
    for f in rows:
        try:
            job = llm_queue.active_job(llm_queue.JOB_BID_SPLIT, str(f["split_file_id"]))
            if job and job.get("status") == "queued":
                llm_queue.cancel(str(job["id"]))
        except Exception:  # noqa: BLE001 - a splitter job finishing for a canceled run is harmless
            logger.exception("plans_reader: canceling the split of file %s failed", f.get("id"))


def cancel(sb, run: dict, *, user: Any) -> dict:
    """Cancel a run that has not finished: the run and its queued or
    running groups become canceled, their queued jobs are canceled, and so
    are the queued splitter jobs of its files. A model call already in
    flight finishes and is billed, but its answer is dropped (a group only
    stores an answer while it is active) and the run does not move on."""
    _refuse_estimator(user)
    run = _load_run(sb, run)
    if run.get("status") in TERMINAL_STATUSES:
        raise RunRefused("This run has already finished.", 409)
    active = [st for st in RUN_STATUSES if st not in TERMINAL_STATUSES]
    rows = (
        sb.table("plans_reader_runs")
        .update({"status": "canceled", "finished_at": _now_iso(), "error": "Canceled."})
        .eq("id", run["id"])
        .in_("status", active)
        .execute()
        .data
        or []
    )
    if not rows:
        raise RunRefused("This run has already finished.", 409)
    groups = [g for g in _groups(sb, run["id"]) if g.get("status") in GROUP_ACTIVE]
    if groups:
        sb.table("plans_reader_groups").update({"status": "canceled", "finished_at": _now_iso()}).eq(
            "run_id", run["id"]
        ).in_("status", list(GROUP_ACTIVE)).execute()
    for g in groups:
        if g.get("llm_job_id"):
            try:
                llm_queue.cancel(str(g["llm_job_id"]))
            except Exception:  # noqa: BLE001 - a queued job finding a canceled group is a no-op anyway
                logger.exception("plans_reader: canceling job %s failed", g["llm_job_id"])
    if run.get("split_job_id"):
        _cancel_split_jobs(sb, run)
    return rows[0]


def _split_still_running(sb, split_job_id: str) -> bool:
    """True while a file of the run's splitter job is still being split (a
    worker would keep writing under the prefix delete sweeps). Files whose
    worker died are cleared by bid_split's stale sweep first."""
    try:
        bid_split.sweep_stale_files(split_job_id, sb=sb)
    except Exception:  # noqa: BLE001 - the check below still runs
        logger.exception("plans_reader: stale sweep of split job %s failed", split_job_id)
    rows = sb.table("bid_split_files").select("id, status").eq("job_id", split_job_id).execute().data or []
    return any(r.get("status") == "running" for r in rows)


def _busy_after_claim(sb, run_id: str) -> bool:
    """True when a group of the run is still running or one of its chat
    answers is pending or running (delete's check after its claim)."""
    running = (
        sb.table("plans_reader_groups").select("id").eq("run_id", run_id).eq("status", "running").limit(1).execute()
    ).data or []
    if running:
        return True
    threads = sb.table("plans_reader_chat_threads").select("id").eq("run_id", run_id).execute().data or []
    thread_ids = [str(t["id"]) for t in threads if t.get("id")]
    if not thread_ids:
        return False
    answering = (
        sb.table("plans_reader_chat_messages")
        .select("id")
        .in_("thread_id", thread_ids)
        .eq("role", "assistant")
        .in_("status", ["pending", "running"])
        .limit(1)
        .execute()
    ).data or []
    return bool(answering)


def _undo_delete_claim(sb, run: dict, status: str, waiting: list[str]) -> None:
    """Take back a delete that did not go through: the run returns from
    canceled to `status` with its old error, then the pipeline groups that
    were waiting before the claim and were failed (their job canceled) or
    canceled (a worker found the run claimed) meanwhile are queued again
    with no job, for resume to enqueue. Best effort: a run left canceled is
    finished by deleting it again."""
    try:
        restored = _cas_status(sb, run["id"], "canceled", status, {"error": run.get("error")})
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("plans_reader: restoring the status of run %s failed", run["id"])
        return
    if restored is None or not waiting:
        return
    try:
        (
            sb.table("plans_reader_groups")
            .update({"status": "queued", "error": None, "finished_at": None, "llm_job_id": None})
            .eq("run_id", run["id"])
            .in_("id", waiting)
            .in_("status", ["failed", "canceled"])
            .execute()
        )
    except Exception:  # noqa: BLE001 - logged; those pages are then missing like any failed group
        logger.exception("plans_reader: queuing the groups of run %s again failed", run["id"])


def delete(sb, run: dict, *, user: Any) -> dict:
    """Delete a run that is not busy (cancel first): every object under its
    storage prefix, its splitter job's objects and row (the splitter page
    refuses to delete plans_reader jobs, so this is the only sweep), then
    the run row (documents, groups, calls, outputs, deliverables and chats
    cascade).

    Race-safe against start and resume: before anything is swept the run is
    claimed with a compare-and-set out of the (non-busy) status read here to
    canceled, so a start (created only) or resume (over_cap only) that won
    meanwhile makes the delete refuse with 409 instead of sweeping the files
    of a run that is now reading them, and once claimed both refuse this
    run. The final row delete is fenced on that claim too.

    After the claim the run is checked again for a group still running or
    a chat answer still pending or running (the router's own check runs
    before the claim, so a job picked up in between is caught here): such a
    job would write rows and objects after the sweep, so the claim is undone
    and the delete answers 409. Once claimed, a queued job finds the run
    canceled and stops (plans_reader_agents.run_group, the modify and
    re-render jobs).

    A delete that does not go through (that 409, or a failed sweep, which
    answers 503) puts the original status back and queues again the
    pipeline groups that were waiting (an over_cap run's): canceling their
    jobs failed them, and resume only re-enqueues queued groups."""
    _refuse_estimator(user)
    run = _load_run(sb, run)
    status = str(run.get("status") or "")
    if status not in DELETABLE_STATUSES:
        raise RunRefused("Cancel the run before deleting it.", 409)
    if run.get("split_job_id") and _split_still_running(sb, str(run["split_job_id"])):
        raise RunRefused(
            "The splitter is still finishing a file of this run. Try deleting it again in a few minutes.", 409
        )
    waiting = [
        str(g["id"]) for g in _groups(sb, run["id"])
        if g.get("kind") in PIPELINE_KINDS and g.get("status") in GROUP_ACTIVE
    ]
    if _cas_status(sb, run["id"], status, "canceled", {"error": MSG_DELETING}) is None:
        raise RunRefused(MSG_DELETE_RACED, 409)
    if _busy_after_claim(sb, str(run["id"])):
        _undo_delete_claim(sb, run, status, waiting)
        raise RunRefused(MSG_DELETE_BUSY, 409)
    for g in _groups(sb, run["id"]):
        if g.get("status") in GROUP_ACTIVE and g.get("llm_job_id"):
            try:
                llm_queue.cancel(str(g["llm_job_id"]))
            except Exception:  # noqa: BLE001
                logger.exception("plans_reader: canceling job %s failed", g["llm_job_id"])
    try:
        storage.delete_plans_reader_prefix(str(run["id"]))
        if run.get("split_job_id"):
            storage.delete_bid_split_prefix(str(run["split_job_id"]))
    except Exception as exc:
        logger.exception("plans_reader: storage sweep for run %s failed", run["id"])
        _undo_delete_claim(sb, run, status, waiting)
        raise RunRefused("The run's files could not be removed right now. Try again in a minute.", 503) from exc
    if run.get("split_job_id"):
        sb.table("bid_split_jobs").delete().eq("id", run["split_job_id"]).execute()
    sb.table("plans_reader_runs").delete().eq("id", run["id"]).eq("status", "canceled").execute()
    return {"id": run["id"], "deleted": True}


# ---------------------------------------------------------------------------
# Deliverables
# ---------------------------------------------------------------------------


def main_output(sb, run_id: str) -> dict | None:
    """The run's latest main output JSON, or None before consolidation."""
    rows = (
        sb.table("plans_reader_outputs").select("*").eq("run_id", run_id).eq("scope", "main").execute().data or []
    )
    if not rows:
        return None
    row = max(rows, key=lambda r: int(r.get("version") or 0))
    return row.get("output") if isinstance(row.get("output"), dict) else None


def latest_deliverable(sb, run_id: str, kind: str) -> dict | None:
    rows = (
        sb.table("plans_reader_deliverables").select("*").eq("run_id", run_id).eq("kind", kind).execute().data or []
    )
    return max(rows, key=lambda r: int(r.get("version") or 0)) if rows else None


def deliverable_spec(kind: str, main: dict | None) -> dict | None:
    """The part of the main output a deliverable is rendered from
    (DELIVERABLE_SPEC_SCHEMAS shapes), or None when the run has nothing of
    that kind (no trench sheets, no marked sheets)."""
    if not isinstance(main, dict):
        return None
    if kind == "boq":
        return main.get("boq") if isinstance(main.get("boq"), dict) else None
    if kind == "estimate":
        if isinstance(main.get("boq"), dict) and isinstance(main.get("estimate"), dict):
            return {"boq": main["boq"], "estimate": main["estimate"]}
        return None
    if kind in ("trench_markup", "sawcut_markup"):
        want = "trench" if kind == "trench_markup" else "sawcut"
        sheets = [
            sh for sh in ((main.get("markups") or {}).get("sheets") or [])
            if isinstance(sh, dict) and sh.get("kind") == want and sh.get("lines")
        ]
        return {"sheets": sheets} if sheets else None
    if kind == "marked_plans":
        sheets = [sh for sh in ((main.get("marked_plans") or {}).get("sheets") or []) if isinstance(sh, dict)]
        return {"sheets": sheets} if sheets else None
    if kind == "brief":
        return main.get("brief") if isinstance(main.get("brief"), dict) else None
    return None


# Deliverable kinds stored as office files; each gets a PDF copy beside it
# for the in-app preview (the other kinds are PDFs already).
PDF_COPY_KINDS = ("boq", "estimate", "brief")
_OFFICE_SUFFIXES = (".docx", ".xlsx")


def pdf_copy_path(path: str) -> str:
    """Where an office deliverable's PDF copy lives: beside the docx or xlsx,
    same key with a .pdf suffix (the deliverables table has one row per kind
    and version; the copy has none of its own)."""
    lower = path.lower()
    for suffix in _OFFICE_SUFFIXES:
        if lower.endswith(suffix):
            return path[: -len(suffix)] + ".pdf"
    return path + ".pdf"


# The brief was the first kind with a PDF copy; the old name stays in use.
brief_pdf_path = pdf_copy_path


def pdf_copy_filename(kind: str, project_name: str, version: int) -> str:
    """The download name of a deliverable's PDF copy: the sent-file name with
    .pdf ('Deep Brief.pdf', 'BOQ - COUNTS.pdf', 'Rev 2 - <NAME> - ELECTRICAL
    ESTIMATE.pdf')."""
    if kind == "brief":
        return render.deliverable_filename("brief_pdf", project_name, version)
    name = render.deliverable_filename(kind, project_name, version)
    lower = name.lower()
    for suffix in _OFFICE_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)] + ".pdf"
    return name + ".pdf"


# PDF copies this process is building on a background thread (preview
# requests for a version rendered before copies existed, or whose render-time
# conversion failed), and the copies whose build failed recently: a poll
# every few seconds must not start a converter call each time, and a
# converter outage is retried only after PDF_COPY_RETRY_SECONDS.
PDF_COPY_RETRY_SECONDS = 300
_PDF_COPY_BUILDING: set[str] = set()
_PDF_COPY_FAILED: dict[str, float] = {}
_PDF_COPY_LOCK = threading.Lock()


def _build_pdf_copy(path: str, filename: str) -> None:
    """Convert the office deliverable at `path` and store the PDF beside it.
    Blocking (converter I/O): runs on the thread ensure_pdf_copy starts."""
    try:
        pdf = render.office_pdf_bytes(storage.download_file(path), filename)
        if pdf:
            storage.upload_file(pdf_copy_path(path), pdf, PDF_MIME, upsert=True)
    except Exception:  # noqa: BLE001 - recorded as a failed build below
        logger.exception("plans_reader: PDF copy of %s failed", path)
        pdf = None
    with _PDF_COPY_LOCK:
        _PDF_COPY_BUILDING.discard(path)
        if pdf:
            _PDF_COPY_FAILED.pop(path, None)
        else:
            _PDF_COPY_FAILED[path] = time.monotonic()


def pdf_copy_status(run: dict, row: dict) -> dict:
    """What the preview can show for one deliverable version, in the shape
    the file preview strategies poll (`preview_status`: ready, pending,
    failed or unavailable; `url` and `filename` when ready). PDF kinds are
    ready as they are. An office kind is ready when its PDF copy exists;
    otherwise, when a converter is configured, the copy is built on a daemon
    thread (one per object and process) and the answer is pending until a
    later poll finds it, or failed for PDF_COPY_RETRY_SECONDS after a build
    that did not produce a PDF. Without a converter it is unavailable."""
    from app.services import office_preview

    kind = str(row.get("kind") or "")
    path = str(row.get("storage_path") or "")
    version = int(row.get("version") or 1)
    if kind not in PDF_COPY_KINDS:
        name = _clean_filename(row.get("filename")) or f"{kind}-v{version}.pdf"
        return {"preview_status": "ready", "url": storage.signed_url(path, download=name), "filename": name}
    name = pdf_copy_filename(kind, _project_name(run), version)
    copy = pdf_copy_path(path)
    if storage.object_exists(copy):
        with _PDF_COPY_LOCK:
            _PDF_COPY_FAILED.pop(path, None)
        return {"preview_status": "ready", "url": storage.signed_url(copy, download=name), "filename": name}
    try:
        converter = office_preview.get_converter()
    except Exception:  # noqa: BLE001 - settings trouble reads as no converter
        converter = None
    if converter is None:
        return {"preview_status": "unavailable", "filename": name}
    now = time.monotonic()
    with _PDF_COPY_LOCK:
        if path in _PDF_COPY_BUILDING:
            return {"preview_status": "pending", "filename": name}
        failed_at = _PDF_COPY_FAILED.get(path)
        if failed_at is not None and now - failed_at < PDF_COPY_RETRY_SECONDS:
            return {"preview_status": "failed", "filename": name}
        _PDF_COPY_FAILED.pop(path, None)
        _PDF_COPY_BUILDING.add(path)
    try:
        threading.Thread(
            target=_build_pdf_copy,
            args=(path, _clean_filename(row.get("filename")) or name),
            name=f"plans-reader-pdf-copy-{version}",
            daemon=True,
        ).start()
    except Exception:
        with _PDF_COPY_LOCK:
            _PDF_COPY_BUILDING.discard(path)
        raise
    return {"preview_status": "pending", "filename": name}


def _project_name(run: dict) -> str:
    project = (run.get("ledger") or {}).get("project") or {}
    return agents._clean_text(project.get("name") or run.get("name"), NAME_MAX_CHARS) or "Project"


class _DocBytes:
    """Document PDFs downloaded once per render (evict() drops one when its
    last sheet is done, so a long marked set does not hold every PDF)."""

    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = documents
        self._cache: dict[str, bytes] = {}

    def get(self, doc_id: str) -> bytes:
        if doc_id not in self.documents:
            raise ValueError("A sheet references a document that is not in this run.")
        if doc_id not in self._cache:
            self._cache[doc_id] = storage.download_file(self.documents[doc_id]["storage_path"])
        return self._cache[doc_id]

    def evict(self, doc_id: str) -> None:
        self._cache.pop(doc_id, None)


class _MarkedPages:
    """The marked plans pages for render_marked_plans_pdf, rendered lazily:
    each sheet's PNG is made when the renderer reaches it and dropped after
    (the renderer encodes one sheet at a time), and a document's PDF is
    evicted after its last sheet. Sheets whose page cannot be rendered are
    skipped and counted."""

    def __init__(self, sheets: list[tuple[str, int, dict]], docs: _DocBytes) -> None:
        self.sheets = sheets
        self.docs = docs
        self.rendered = 0
        self.skipped = 0
        self._last = {doc_id: i for i, (doc_id, _page, _sheet) in enumerate(sheets)}

    def __bool__(self) -> bool:
        return bool(self.sheets)

    def __iter__(self):
        for i, (doc_id, page, sheet) in enumerate(self.sheets):
            try:
                png, _w, _h = images.render_page_png(self.docs.get(doc_id), page - 1, MARKED_PLANS_PPI)
            except ValueError:
                self.skipped += 1
                png = None
            finally:
                if self._last.get(doc_id) == i:
                    self.docs.evict(doc_id)
            if png is None:
                continue
            self.rendered += 1
            yield {
                "png_bytes": png,
                "legend": sheet.get("legend") or [],
                "marks": sheet.get("marks") or [],
                "area": sheet.get("area"),
                "sheet_number": sheet.get("sheet_number"),
            }


def _render_bytes(kind: str, spec: dict, run: dict, docs: _DocBytes) -> tuple[bytes, str, bytes | None]:
    """(file bytes, content type, PDF copy or None) for one deliverable. The
    office kinds (PDF_COPY_KINDS) carry a PDF copy for the in-app preview
    when the converter answers; the rest are PDFs already."""
    if kind == "boq":
        xlsx = render.render_boq_xlsx(spec)
        return xlsx, CONTENT_TYPES[".xlsx"], render.office_pdf_bytes(xlsx, render.deliverable_filename(kind, "", 1))
    if kind == "estimate":
        xlsx = render.render_estimate_xlsx(spec.get("boq") or {}, spec.get("estimate") or {}, date=today_pacific())
        return (
            xlsx,
            CONTENT_TYPES[".xlsx"],
            render.office_pdf_bytes(xlsx, render.deliverable_filename(kind, _project_name(run), 1)),
        )
    if kind in ("trench_markup", "sawcut_markup"):
        # A sheet whose document is not in this run (an unmapped doc_index)
        # is left out rather than failing the whole markup, like marked
        # plans; every remaining sheet carries its run document_id.
        want = "trench" if kind == "trench_markup" else "sawcut"
        sheets = []
        skipped = 0
        for sheet in spec.get("sheets") or []:
            if not isinstance(sheet, dict) or sheet.get("kind") != want:
                continue
            doc_id = str(sheet.get("document_id") or "")
            if doc_id not in docs.documents:
                skipped += 1
                continue
            sheets.append({**sheet, "document_id": doc_id})
        if not sheets:
            raise ValueError("None of the markup sheets could be found in this run's documents.")
        if skipped:
            logger.warning("plans_reader: %s %s sheet(s) skipped for run %s", skipped, want, run.get("id"))
        sources: dict[Any, bytes] = {}
        for key in render.markup_documents(sheets, kind=kind):
            sources[str(key)] = docs.get(str(key))
        return render.render_markup_pdf(sources, sheets, kind=kind), PDF_MIME, None
    if kind == "marked_plans":
        chosen: list[tuple[str, int, dict]] = []
        missing = 0
        for sheet in spec.get("sheets") or []:
            if not isinstance(sheet, dict):
                continue
            doc_id = str(sheet.get("document_id") or "")
            page = agents._as_int(sheet.get("page")) or 0
            if doc_id not in docs.documents or page < 1:
                missing += 1
                continue
            chosen.append((doc_id, page, sheet))
        if not chosen:
            raise ValueError("None of the marked sheets could be found in this run's documents.")
        pages = _MarkedPages(chosen, docs)
        data = render.render_marked_plans_pdf(pages)  # type: ignore[arg-type]  # a lazy iterable
        if not pages.rendered:
            raise ValueError("None of the marked sheets could be found in this run's documents.")
        if missing or pages.skipped:
            logger.warning(
                "plans_reader: %s marked sheet(s) skipped for run %s", missing + pages.skipped, run.get("id")
            )
        return data, PDF_MIME, None
    if kind == "brief":
        docx = render.render_brief_docx(spec, project_name=_project_name(run))
        return docx, CONTENT_TYPES[".docx"], render.brief_pdf_bytes(docx)
    raise ValueError(f"Unknown deliverable kind '{agents._clean_text(kind, 40)}'.")


def render_deliverable(
    sb,
    run: dict,
    kind: str,
    spec: dict,
    *,
    version: int,
    instruction: str | None = None,
    user: Any = None,
    documents: dict[str, dict] | None = None,
) -> dict:
    """Render one deliverable from its spec, store it under
    `plans-reader/{run_id}/deliverables/v{n}/` (an office kind's PDF copy
    beside it) and insert the plans_reader_deliverables row. A version taken
    by a concurrent render moves to the next number. Raises ValueError when
    the renderer refuses the spec."""
    if kind not in DELIVERABLE_KINDS:
        raise ValueError(f"Unknown deliverable kind '{agents._clean_text(kind, 40)}'.")
    if not isinstance(spec, dict):
        raise ValueError("The deliverable spec must be an object.")
    docs = documents if documents is not None else {str(d["id"]): d for d in _documents(sb, run["id"])}
    data, content_type, pdf_copy = _render_bytes(kind, spec, run, _DocBytes(docs))
    v = max(1, int(version))
    for _attempt in range(DELIVERABLE_VERSION_ATTEMPTS):
        filename = render.deliverable_filename(kind, _project_name(run), v)
        path = storage.build_plans_reader_path(run["id"], f"deliverables/v{v}", filename)
        storage.upload_file(path, data, content_type)
        if pdf_copy:
            storage.upload_file(pdf_copy_path(path), pdf_copy, PDF_MIME)
        row = {
            "run_id": run["id"],
            "kind": kind,
            "version": v,
            "filename": filename,
            "storage_path": path,
            "size_bytes": len(data),
            "spec_json": spec,
            "instruction": instruction,
            "created_by": _uid(user),
        }
        try:
            inserted = sb.table("plans_reader_deliverables").insert(row).execute().data[0]
        except Exception as exc:
            for p in [path] + ([pdf_copy_path(path)] if pdf_copy else []):
                try:
                    storage.delete_file(p)
                except Exception:  # noqa: BLE001 - an orphan is swept with the run
                    logger.exception("plans_reader: could not remove %s", p)
            if not _is_unique_violation(exc):
                raise
            v += 1
            continue
        out = dict(inserted)
        if kind in PDF_COPY_KINDS:
            out["pdf_available"] = bool(pdf_copy)
            out["pdf_filename"] = pdf_copy_filename(kind, _project_name(run), v)
        return out
    raise RunRefused("Another version of this deliverable was being saved; try again.", 409)


def render_all_deliverables(
    sb,
    run: dict,
    main: dict,
    *,
    version: int = 1,
    user: Any = None,
    skip_kinds: set[str] | frozenset[str] = frozenset(),
    heartbeat: Any = None,
) -> tuple[list[dict], list[str]]:
    """Render every deliverable the main output has a spec for, except
    `skip_kinds` (already saved). Each renderer failure is caught and named
    in the returned errors; the others still render. `heartbeat()` is
    called after each deliverable."""
    docs = {str(d["id"]): d for d in _documents(sb, run["id"])}
    rows: list[dict] = []
    errors: list[str] = []
    labels = {
        "boq": "BOQ", "estimate": "Estimate", "trench_markup": "Trench markup",
        "sawcut_markup": "Saw cut markup", "marked_plans": "Marked plans", "brief": "Deep brief",
    }
    for kind in DELIVERABLE_KINDS:
        if kind in skip_kinds:
            continue
        spec = deliverable_spec(kind, main)
        if spec is None:
            if kind in ("boq", "estimate", "brief"):
                errors.append(f"{labels[kind]}: the consolidated output has no {labels[kind].lower()}.")
            continue
        try:
            rows.append(render_deliverable(sb, run, kind, spec, version=version, user=user, documents=docs))
        except Exception as exc:  # noqa: BLE001 - one renderer must not stop the others
            logger.exception("plans_reader: rendering %s for run %s failed", kind, run.get("id"))
            message = str(exc) if isinstance(exc, ValueError) else "the file could not be rendered."
            errors.append(f"{labels[kind]}: {message}"[:400])
        if heartbeat is not None:
            heartbeat()
    return rows, errors


def _rerender_source(sb, run: dict, kind: str) -> tuple[dict, int, int]:
    """(spec, next version, latest version or 0) for a re-render: the latest
    version's spec, or the main output's part when the deliverable was never
    rendered (a renderer that failed the first time). RunRefused 404 when
    the run has nothing of that kind to render."""
    if kind not in DELIVERABLE_KINDS:
        raise RunRefused("Unknown deliverable kind.")
    latest = latest_deliverable(sb, run["id"], kind)
    spec = latest.get("spec_json") if latest else deliverable_spec(kind, main_output(sb, run["id"]))
    if not isinstance(spec, dict):
        raise RunRefused("This run has nothing to render for that deliverable.", 404)
    current = int(latest.get("version") or 0) if latest else 0
    return spec, current + 1, current


def release_orphan_changes(sb, run_id: str) -> int:
    """Fail the inline chat changes whose answer is over. A change made from
    chat holds a `modify` group with no queue job (output `inline` and
    `message_id`, see plans_reader_chat._tool_modify_deliverable) and
    closes it when the call ends; a worker that died mid-change leaves it
    active, and nothing else would ever close it, so it would block every
    later change on the run. Once that answer is no longer pending or
    running (chat's stale sweep or the queue failed it) the group is failed
    here. Returns how many groups were failed."""
    held: dict[str, dict] = {}
    for g in _groups(sb, run_id):
        out = g.get("output") if isinstance(g.get("output"), dict) else {}
        if (
            g.get("kind") == "modify"
            and g.get("status") in GROUP_ACTIVE
            and not g.get("llm_job_id")
            and out.get("inline")
            and out.get("message_id")
        ):
            held[str(out["message_id"])] = g
    if not held:
        return 0
    rows = (
        sb.table("plans_reader_chat_messages").select("id, status").in_("id", list(held)).execute()
    ).data or []
    live = {str(r.get("id")) for r in rows if r.get("status") in ("pending", "running")}
    released = 0
    for message_id, g in held.items():
        if message_id in live:
            continue
        done = (
            sb.table("plans_reader_groups")
            .update({"status": "failed", "error": MSG_INLINE_CHANGE_LOST, "finished_at": _now_iso()})
            .eq("id", g["id"])
            .in_("status", list(GROUP_ACTIVE))
            .execute()
        ).data or []
        released += len(done)
    return released


def claim_change_slot(
    sb, run_id: str, *, output: dict | None = None, status: str = "queued"
) -> dict:
    """Insert a `modify` group (the run's one deliverable change at a time:
    a model modification, a re-render or a change made from chat) and
    return it. `status` is `queued` for a queue job, `running` for a change
    made inline (chat), which closes the group itself. Inline changes whose
    answer died are released first (release_orphan_changes). Migration
    0153's partial unique index allows one queued or running modify group
    per run; its 23505 becomes RunRefused 409. On a database without the
    index the run's active modify groups are read back and every claim but
    the oldest (created_at, then id) withdraws its own row, the same rule
    the Modify route applies."""
    try:
        release_orphan_changes(sb, run_id)
    except Exception:  # noqa: BLE001 - a stuck slot then refuses this claim like any active change
        logger.exception("plans_reader: releasing orphan changes of run %s failed", run_id)
    row: dict[str, Any] = {"run_id": run_id, "kind": "modify", "pages": [], "status": status, "output": output}
    if status == "running":
        row.update({"started_at": _now_iso(), "attempts": 1})
    try:
        group = sb.table("plans_reader_groups").insert(row).execute().data[0]
    except Exception as exc:
        if _is_unique_violation(exc):
            raise RunRefused(MSG_CHANGE_ACTIVE, 409) from exc
        raise
    active = [
        g for g in _groups(sb, run_id)
        if g.get("kind") == "modify" and g.get("status") in GROUP_ACTIVE
    ]
    oldest = min(active, key=_order_key) if active else group
    if str(oldest.get("id")) != str(group["id"]):
        sb.table("plans_reader_groups").delete().eq("id", group["id"]).execute()
        raise RunRefused(MSG_CHANGE_ACTIVE, 409)
    return group


def rerender_deliverable(sb, run: dict, kind: str, *, user: Any = None) -> dict:
    """Queue a re-render of one deliverable as a new version (no model call:
    after a renderer fix, or to get a file a renderer failed on the first
    time). Rendering marked plans draws every page, so it runs on the queue
    worker, never in the request: a `modify` group (claim_change_slot, one
    change per run at a time) and a plans_reader_modify job with
    `instruction` null and `rerender` true, which llm_queue runs through
    rerender_now. Returns {"group", "queue", "kind", "version"} (version is
    the one the job will write). RunRefused: 404 nothing to render, 409
    another change active or the run still rendering, 409 queue off."""
    run = _load_run(sb, run)
    if run.get("status") == "rendering":
        raise RunRefused(MSG_RERENDER_WHILE_RENDERING, 409)
    _spec, version, current = _rerender_source(sb, run, kind)
    if not get_settings().llm_queue_enabled:
        raise RunRefused(MSG_QUEUE_OFF, 409)
    group = claim_change_slot(sb, str(run["id"]), output={"kind": kind, "rerender": True})
    uid = _uid(user)
    try:
        job = llm_queue.enqueue(
            JOB_MODIFY,
            target_id=str(group["id"]),
            project_id=None,
            payload={
                "group_id": str(group["id"]),
                "run_id": str(run["id"]),
                "kind": kind,
                "instruction": None,
                "rerender": True,
                "user_id": uid,
                "base_version": current,
            },
            created_by=uid,
        )
    except Exception:
        # No job means nothing would ever run the group: drop it so the run
        # does not show a change that is not coming.
        sb.table("plans_reader_groups").delete().eq("id", group["id"]).execute()
        raise
    if job and job.get("id"):
        sb.table("plans_reader_groups").update({"llm_job_id": job["id"]}).eq("id", group["id"]).execute()
        group["llm_job_id"] = job["id"]
    try:
        queue = llm_queue.poll_info(JOB_MODIFY, str(group["id"]))
    except Exception:  # noqa: BLE001 - queue detail is optional
        logger.exception("plans_reader: poll info for re-render group %s failed", group.get("id"))
        queue = None
    return {"group": group, "queue": queue, "kind": kind, "version": version}


def rerender_now(sb, run: dict, kind: str, *, user: Any = None) -> dict:
    """Render a deliverable again as a new version, inline (the
    plans_reader_modify job of a re-render runs this): from its latest spec,
    or from the main output when it was never rendered. Returns the new
    plans_reader_deliverables row; RunRefused when there is nothing to
    render or the renderer refuses the spec."""
    run = _load_run(sb, run)
    spec, version, _current = _rerender_source(sb, run, kind)
    try:
        return render_deliverable(sb, run, kind, spec, version=version, user=user)
    except RunRefused:
        raise
    except ValueError as exc:
        raise RunRefused(str(exc)) from exc


# ---------------------------------------------------------------------------
# Totals, lists and the poll payload
# ---------------------------------------------------------------------------

_CALL_COLUMNS = (
    "id, run_id, group_id, thread_id, kind, category, model, reasoning_effort, response_id, "
    "input_tokens, cached_tokens, output_tokens, reasoning_tokens, cost_usd, duration_ms, ok, "
    "error_kind, error, created_at"
)
# The run payload lists at most this many calls, newest first
# (`totals.calls` counts them all).
CALLS_MAX = 2000


def _calls(sb, run_id: str) -> list[dict]:
    out: list[dict] = []
    start_at = 0
    while True:
        page = (
            sb.table("plans_reader_calls")
            .select(_CALL_COLUMNS)
            .eq("run_id", run_id)
            .order("created_at")
            .order("id")
            .range(start_at, start_at + _PAGE - 1)
            .execute()
            .data
            or []
        )
        out.extend(page)
        if len(page) < _PAGE:
            return out
        start_at += _PAGE


def _sum_calls(calls: list[dict]) -> dict:
    totals = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    cost = Decimal("0")
    for c in calls:
        for k in totals:
            totals[k] += int(c.get(k) or 0)
        cost += Decimal(str(c.get("cost_usd") or 0))
    return {**totals, "cost_usd": float(cost.quantize(Decimal("0.000001"))), "calls": len(calls)}


def recompute_totals(sb, run_id: str) -> dict:
    """Sum the run's plans_reader_calls into its token and cost columns
    (recomputed, never incremented, so concurrent workers cannot drift)."""
    totals = _sum_calls(_calls(sb, run_id))
    sb.table("plans_reader_runs").update(
        {k: totals[k] for k in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "cost_usd")}
    ).eq("id", run_id).execute()
    return totals


_LIST_COLUMNS = (
    "id, name, status, source, project_id, render_mode, model, documents_total, groups_total, "
    "groups_done, groups_failed, estimated_input_tokens, estimated_cost_usd, input_tokens, "
    "output_tokens, cost_usd, error, created_by, started_at, finished_at, created_at, updated_at"
)


def _like_pattern(q: str) -> str:
    text = q[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{text}%"


def list_runs(sb, *, status: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """Runs newest first, optionally by status and a name search."""
    if status and status not in RUN_STATUSES:
        raise RunRefused("Unknown run status.")
    limit = max(1, min(int(limit or 50), LIST_MAX))
    offset = max(0, int(offset or 0))
    query = sb.table("plans_reader_runs").select(_LIST_COLUMNS)
    if status:
        query = query.eq("status", status)
    text = agents._clean_text(q, 100) if q else ""
    if text:
        query = query.ilike("name", _like_pattern(text))
    return query.order("created_at", desc=True).range(offset, offset + limit - 1).execute().data or []


_REQUEUE_CHECKED: dict[str, float] = {}


def _requeue_orphans(sb, run: dict, groups: list[dict]) -> None:
    """Enqueue again the queued groups that have no live queue job: an
    enqueue that failed (a transient queue error leaves the group queued
    with no job) or a job that ended without settling its group. At most
    once per REQUEUE_CHECK_SECONDS per run and process, and only for groups
    older than REQUEUE_MIN_AGE_SECONDS (a group just inserted is being
    enqueued by its stage winner); llm_queue.enqueue reuses an active job,
    so a race with that winner still queues one job."""
    key = str(run["id"])
    now = time.monotonic()
    with _KICK_LOCK:
        last = _REQUEUE_CHECKED.get(key)
        if last is not None and now - last < REQUEUE_CHECK_SECONDS:
            return
        _REQUEUE_CHECKED[key] = now
        if len(_REQUEUE_CHECKED) > 1_000:
            for old in [k for k, t in _REQUEUE_CHECKED.items() if now - t > 3_600]:
                _REQUEUE_CHECKED.pop(old, None)
    queued = [
        g for g in groups
        if g.get("kind") in PIPELINE_KINDS and g.get("status") == "queued"
        and (_age_seconds(g.get("created_at")) or 0) > REQUEUE_MIN_AGE_SECONDS
    ]
    if not queued:
        return
    job_ids = [str(g["llm_job_id"]) for g in queued if g.get("llm_job_id")]
    live: set[str] = set()
    if job_ids:
        rows = sb.table("llm_jobs").select("id, status").in_("id", job_ids).execute().data or []
        live = {str(r["id"]) for r in rows if r.get("status") in GROUP_ACTIVE}
    orphans = [g for g in queued if not (g.get("llm_job_id") and str(g["llm_job_id"]) in live)]
    for g in orphans:
        logger.warning("plans_reader: group %s of run %s had no live job; enqueuing it again", g["id"], key)
    _enqueue_groups(sb, orphans, run)


def _self_heal(sb, run: dict, files: list[dict], groups: list[dict]) -> None:
    """A poll's safety net for a missed event (a hook that failed, a worker
    that died between a mark and its advance, a lost enqueue, a render cut
    short). Never raises; the heavy next step runs on a background thread
    (at most one per run and process, see _kick_advance)."""
    try:
        status = run.get("status")
        if status == "splitting":
            if run.get("split_job_id"):
                bid_split.sweep_stale_files(str(run["split_job_id"]), sb=sb)
            candidates = [f for f in files if f.get("status") == "splitting"]
            candidates += _reclaim_stuck_settling(sb, run, files)
            settled_any = False
            for f in candidates:
                settled_any = _settle_split_file(sb, f) or settled_any
            fresh = _files(sb, run["id"]) if (settled_any or candidates) else files
            if not any(f.get("status") in FILE_UNSETTLED for f in fresh) and not _planning_claim_live(run):
                _kick_advance(run["id"])
        elif status == "reading":
            pipeline = [g for g in groups if g.get("kind") in PIPELINE_KINDS]
            if not any(g.get("status") in GROUP_ACTIVE for g in pipeline):
                _kick_advance(run["id"])
            else:
                _requeue_orphans(sb, run, groups)
        elif status == "consolidating":
            mains = [g for g in groups if g.get("kind") == "main"]
            if not mains or all(g.get("status") in GROUP_TERMINAL for g in mains):
                _kick_advance(run["id"])
            else:
                _requeue_orphans(sb, run, groups)
        elif status == "rendering":
            if _stale(run):
                _kick_advance(run["id"])
        if any(
            g.get("kind") == "modify" and g.get("status") in GROUP_ACTIVE and not g.get("llm_job_id")
            for g in groups
        ):
            release_orphan_changes(sb, str(run["id"]))
    except Exception:  # noqa: BLE001 - the payload is still returned
        logger.exception("plans_reader: self-heal for run %s failed", run.get("id"))


_GROUP_PUBLIC = (
    "id", "kind", "category", "group_index", "pass", "status", "est_input_tokens",
    "error", "attempts", "llm_job_id", "started_at", "finished_at", "created_at",
)


def _change_labels(sb, run_id: str) -> dict[str, dict]:
    """What each `modify` group of a run is, for the run page: `change`
    ("rerender" for a re-render, "chat" for a change made from chat, else
    "modify"), `deliverable_kind` and, once done, `deliverable_version`.
    Read from the small outputs of the modify groups only (the pipeline
    groups' outputs are large and stay out of the poll). Best effort."""
    try:
        rows = (
            sb.table("plans_reader_groups").select("id, output").eq("run_id", run_id).eq("kind", "modify").execute()
        ).data or []
    except Exception:  # noqa: BLE001 - labels are optional detail
        logger.exception("plans_reader: reading the change labels of run %s failed", run_id)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        o = r.get("output") if isinstance(r.get("output"), dict) else {}
        change = "rerender" if o.get("rerender") else "chat" if o.get("inline") else "modify"
        version = o.get("version")
        out[str(r.get("id"))] = {
            "change": change,
            "deliverable_kind": o.get("kind") if o.get("kind") in DELIVERABLE_KINDS else None,
            "deliverable_version": int(version) if isinstance(version, int) else None,
        }
    return out


def get_run_payload(sb, run_id: str) -> dict | None:
    """Everything the run page polls: {run, files, documents,
    documents_by_category, groups (status, estimated and actual tokens,
    cost, document_pages), outputs [{id, scope, category, version}],
    deliverables, totals, queue (poll info for up to 10 queued groups),
    calls (newest CALLS_MAX, newest first)}. None for an unknown run.
    page_meta and group outputs stay out (large); the outputs route and the
    chat tools serve them."""
    run = _one(sb, "plans_reader_runs", str(run_id))
    if run is None:
        return None
    files = _files(sb, run["id"])
    # Group outputs are large and polled every few seconds: left out here.
    groups = (
        sb.table("plans_reader_groups")
        .select(", ".join(_GROUP_PUBLIC + ("run_id", "pages")))
        .eq("run_id", run["id"])
        .order("created_at")
        .execute()
        .data
        or []
    )
    _self_heal(sb, run, files, groups)
    documents = []
    by_category: dict[str, list[str]] = {}
    for d in agents.run_doc_order(_documents(sb, run["id"])):
        roles: dict[str, int] = {}
        for entry in agents._own_pages(d):
            role = entry.get("page_role") or "other"
            roles[role] = roles.get(role, 0) + 1
        doc = {k: v for k, v in d.items() if k != "page_meta"}
        doc["page_roles"] = roles
        documents.append(doc)
        by_category.setdefault(d.get("category") or "other", []).append(str(d["id"]))
    calls = _calls(sb, run["id"])
    per_group: dict[str, list[dict]] = {}
    for c in calls:
        if c.get("group_id"):
            per_group.setdefault(str(c["group_id"]), []).append(c)
    changes = _change_labels(sb, run["id"]) if any(g.get("kind") == "modify" for g in groups) else {}
    group_rows = []
    for g in groups:
        row = {k: g.get(k) for k in _GROUP_PUBLIC}
        row["pages"] = len(g.get("pages") or [])
        row["document_pages"] = _document_pages(g.get("pages") or [])
        row["has_output"] = g.get("status") == "done"
        if g.get("kind") == "modify":
            row.update(changes.get(str(g["id"])) or {"change": "modify", "deliverable_kind": None,
                                                     "deliverable_version": None})
        row.update(_sum_calls(per_group.get(str(g["id"]), [])))
        group_rows.append(row)
    outputs = [
        {k: o.get(k) for k in ("id", "scope", "category", "version", "created_at")}
        for o in sb.table("plans_reader_outputs").select("id, scope, category, version, created_at").eq(
            "run_id", run["id"]
        ).execute().data
        or []
    ]
    deliverables = []
    raw_deliverables = sorted(
        sb.table("plans_reader_deliverables").select(
            "id, kind, version, filename, storage_path, size_bytes, instruction, created_by, created_at"
        ).eq("run_id", run["id"]).execute().data
        or [],
        key=lambda r: (str(r.get("kind")), int(r.get("version") or 0)),
    )
    for d in raw_deliverables:
        row = {k: v for k, v in d.items() if k != "storage_path"}
        if d.get("kind") in PDF_COPY_KINDS:
            row["pdf_filename"] = pdf_copy_filename(str(d["kind"]), _project_name(run), int(d.get("version") or 1))
            row["pdf_available"] = None  # checked for the latest version of each kind below
        deliverables.append(row)
    for kind in PDF_COPY_KINDS:
        of_kind = [r for r in deliverables if r.get("kind") == kind]
        if not of_kind:
            continue
        latest = of_kind[-1]
        path = next(
            (str(d.get("storage_path") or "") for d in raw_deliverables if d.get("id") == latest.get("id")),
            "",
        )
        try:
            latest["pdf_available"] = bool(path) and storage.object_exists(pdf_copy_path(path))
        except Exception:  # noqa: BLE001 - optional detail
            latest["pdf_available"] = None
    queue: dict[str, Any] = {}
    for g in [g for g in groups if g.get("status") == "queued"][:QUEUE_POLL_MAX]:
        try:
            # Each group is polled under the job type it was enqueued as
            # (modify groups under plans_reader_modify, or their detail is null).
            queue[str(g["id"])] = llm_queue.poll_info(_job_type_for(g), str(g["id"]))
        except Exception:  # noqa: BLE001 - queue detail is optional
            logger.exception("plans_reader: poll info for group %s failed", g.get("id"))
    totals = _sum_calls(calls)
    totals.update(
        {
            "estimated_input_tokens": run.get("estimated_input_tokens"),
            "estimated_cost_usd": float(run["estimated_cost_usd"]) if run.get("estimated_cost_usd") is not None else None,
        }
    )
    return {
        "run": {k: v for k, v in run.items() if k != "ledger"},
        "files": files,
        "documents": documents,
        "documents_by_category": by_category,
        "groups": group_rows,
        "outputs": outputs,
        "deliverables": deliverables,
        "totals": totals,
        "queue": queue,
        # Newest first for the Calls tab; the request snapshot stays out.
        "calls": list(reversed(calls))[:CALLS_MAX],
    }


def _document_pages(pages: list) -> dict[str, int]:
    """{document_id: page count} for one group's page list. The payload
    carries only counts (page entries are large); the document tree splits
    a group's tokens over its documents by this share."""
    counts: dict[str, int] = {}
    for page in pages:
        if isinstance(page, dict) and page.get("document_id"):
            doc_id = str(page["document_id"])
            counts[doc_id] = counts.get(doc_id, 0) + 1
    return counts

