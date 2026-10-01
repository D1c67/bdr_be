"""Run orchestration for the RFP Ingestion Sandbox: runs, files, the queue
job, the CAS marks and bell, retention prune, and the boot self-test.

This is the layer between the dev-only router (app/routers/rfp_ingest.py) and
the sandbox machinery (app/services/rfp_sandbox_runner.py). It owns the three
tables from migration 0119 and every transition between their statuses; the
design record is docs/RFP_INGESTION_SANDBOX.md (sections 2, 3.5 to 3.8 and 4)
and the verdict vocabulary is app/sandbox/protocol.py.

Shape of a run:

- An UPLOAD run is created `staging`, collects files one POST at a time
  (`add_upload_file`: sniff + sha256 in the request thread, the row inserted
  BEFORE the object upload so the partial unique index on (run_id, sha256) is
  the duplicate check), then `start_run` CASes it to `pending` and the router
  dispatches. An EMAIL run is created `pending` with one file row per
  `ingested_email_attachments` row and is dispatched at once; its bytes are
  materialized by the runner (stored copy in project-files, else a streamed
  Graph `$value` download under the byte cap).
- ONE llm_jobs row per run (`job_type='rfp_ingest'`, priority 200). The queue
  calls `execute(run_id)`; `mark_from_queue` receives only the queue's own
  marks (pending on a scheduled retry, failed on a terminal failure) and
  `current_status` reports every terminal run as "done" so the AI monitor's
  generic retry refuses (the /retry route is the only retry path).
- `execute` claims each pending file with a fresh `claim_token` and fences
  EVERY later write on it, materializes the bytes into a scratch directory,
  sniffs and hashes them, converts an office file to a PDF through the
  Gotenberg service (`rfp_office_convert`, spec section 2.1: the raw bytes
  are never opened here; the derived PDF is sniffed like an upload, kept in
  the derived bucket as `converted.pdf` so a re-run reuses it, and is what
  the child gets), runs the sandbox (`rfp_sandbox_runner.run_sandbox`,
  a synchronous blocking call made from the queue's worker thread), then
  writes the tail (text.json, manifest.json, the images-PDF parts), replaces
  the pages rows and CASes the file terminal. The page images never
  accumulate in this process: `_PageSink` is handed to the runner, which
  delivers each validated page's two JPEGs as it re-reads them, and the sink
  appends the thumb to the streaming images PDF and uploads in batches
  bounded by both `_UPLOAD_BATCH_PAGES` pages and `_UPLOAD_BATCH_BYTES`
  resident image bytes, refreshing the file's progress, renewing the
  queue lease and re-checking the abort flag at every batch. Per-file
  problems NEVER escape the loop: they become `failed/<code>` with an
  app-authored sentence from `protocol.VERDICT_MESSAGES`; only a lost queue
  lease (`LeaseLost`), the shutdown event and a failure to load or CAS the
  run row itself get out.
- `_mark` is the single writer of `rfp_ingest_runs.status` and is a
  compare-and-swap, not an update: pending/running/failed/done apply only
  from pending|running, canceled is sticky (from staging|pending|running),
  expired only from a terminal status, and staging -> pending happens only in
  `start_run`. The terminal transition that WINS the CAS sends the
  `rfp_ingest.finished` bell exactly once; a failed or canceled mark also
  hands that run's `running` files back to `pending` (reject_code
  `interrupted`, cleared on the next claim).

Cooperative stops while a child runs come through `should_abort()`: the
shutdown event (`SHUTTING_DOWN`, set by the lifespan teardown before the
worker tasks are cancelled; the runner kills its child, leaves the file
`running`, requeues the job through `llm_queue.requeue_self` and returns),
a canceled run (the in-flight file is CAS-reset to pending and its derived
objects removed), and a lost file claim. A lost queue lease is reported by
`llm_queue.renew_lease` returning False and surfaces as `LeaseLost`, which
`execute` catches at the top and logs: nothing is written for that attempt.

`prune_expired` (the queue's hourly slot) carries the retention work: it
sweeps orphaned scratch trees left by a worker that died without running its
`finally`, reclaims staging runs nobody ever started, and marks terminal runs
past `rfp_ingest_retention_days` expired BEFORE deleting their objects, so a
`/retry` landing in that window can never resurrect a run whose bytes are
already gone. `delete_run` is the mirror image: it refuses while the queue
still holds an active job for the run (that job row is the runner's
acknowledgement), and removes the objects before the rows, since the rows are
what the prune walks to find the objects.

Every string that lands in an `error` column is app-authored. Filenames,
metadata and child detail strings pass through `rfp_sanitize.sanitize_display`
before they reach a log line or a jsonb column, and the child's stderr tail
goes into the manifest (sanitized, 4 KB), never into `run.error`.

Every function here is synchronous and uses the sync Supabase SDK, so the
router runs them under `run_in_threadpool` and the queue calls `execute`
from `asyncio.to_thread`; nothing here may be called inside `async def`.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.supabase_client import get_supabase
from app.sandbox import protocol
from app.services import graph_inbox, llm_queue, notifications, rfp_sanitize, storage
from app.services import rfp_ingest_storage as rs
from app.services import rfp_office_convert as office
from app.services import rfp_sandbox_runner as runner
from app.services.rfp_image_pdf import ImagesPdfWriter
from app.services.rfp_sandbox_runner import SandboxLimits, SandboxResult

logger = logging.getLogger(__name__)

RUNS_TABLE = "rfp_ingest_runs"
FILES_TABLE = "rfp_ingest_files"
PAGES_TABLE = "rfp_ingest_pages"

# In-app notification type for a settled run; the bell deep-links it to
# /ingestion-sandbox?run=<id> (metadata carries the run id; there is no project).
RUN_FINISHED_NOTIFICATION = "rfp_ingest.finished"

# Set by the lifespan teardown before the queue tasks are cancelled: a running
# sandbox kills its child, leaves the file running, requeues its job and
# returns instead of waiting for a CancelledError it cannot see from a thread.
SHUTTING_DOWN = threading.Event()

_ERROR_MAX_CHARS = 500
_FILENAME_MAX_CHARS = 200
_MIME_MAX_CHARS = 100
_PAGES_UPSERT_BATCH = 500
_UPLOAD_BATCH_PAGES = 32          # page objects handed to upload_many at a time (2 per page)
# Second bound on the same batch, in resident bytes. The page count alone does
# not bound memory: the protocol accepts up to MAX_THUMB_BYTES + MAX_FULL_BYTES
# per page, so a child that pads every artifact to its cap could park 32 x 64
# MiB in this worker before the first flush. 96 MiB is several batches of real
# drawing pages (a 4000 px full is 1 to 3 MB), so honest files still flush on
# the page trigger and pay no extra round trips.
_UPLOAD_BATCH_BYTES = 96 * 1024 * 1024
_PROGRESS_WRITE_SECONDS = 10.0    # spec 3.2: the file row's live fields refresh every 10 s
_CANCEL_POLL_SECONDS = 5.0        # how often should_abort() re-reads the run row mid-child
_SELF_TEST_TIMEOUT_SECONDS = 120
_SELF_TEST_OPEN_TIMEOUT_SECONDS = 60
_SELF_TEST_SLOT_WAIT_SECONDS = 30.0
_SELF_TEST_SLOT_POLL_SECONDS = 1.0
# How long a FAILED self-test verdict gates runs before it is re-run: a worker
# that boots into a bad minute must not refuse every run it ever claims.
_SELF_TEST_VERDICT_TTL_SECONDS = 900.0
_FILE_TIMEOUT_FLOOR_SECONDS = 600
_PRUNE_PAGE = 200
# File ids per `in.()` filter when the expiry pass clears the page path
# columns: the filter rides in the URL, so this is far smaller than the
# insert batch above.
_PATH_CLEAR_BATCH = 50
_LIST_MAX = 500
_IMAGES_DIR = "images"
_SCRATCH_PREFIX = "rfp-ingest-"
_SELFTEST_PREFIX = "rfp-selftest-"
_SANDBOX_PREFIX = "rfp-sandbox-"        # the runner's own per-file work dir
_SWEEP_PREFIXES = (_SCRATCH_PREFIX, _SANDBOX_PREFIX, _SELFTEST_PREFIX)
_SCRATCH_SUBDIR = "rfp-ingest"
# A staging run nobody started: its quarantine objects are reclaimed after
# this long (measured on updated_at, so a paused upload session is safe).
_STAGING_ABANDON_HOURS = 48
# Orphan scratch sweep: a directory is a candidate only when nothing in its
# tree has been touched for the longest file any worker could still be
# processing, times this margin (the workers cannot see each other's trees).
_SCRATCH_SWEEP_MARGIN = 2.0
# Bounds on the orphan sweep's walk, mirroring the runner's out-dir accounting
# (_MAX_OUT_DEPTH / _OUT_ENTRY_SLACK): an orphaned tree is by definition one
# whose quota mirror died with its parent, so nothing else bounds its entry
# count and the walk must bound itself.
_MAX_TREE_DEPTH = 8
_SWEEP_ENTRY_SLACK = 65536

_RUN_ACTIVE_FROM = [protocol.RUN_PENDING, protocol.RUN_RUNNING]
_RUN_CANCELABLE_FROM = [protocol.RUN_STAGING, protocol.RUN_PENDING, protocol.RUN_RUNNING]
_RUN_EXPIRABLE_FROM = sorted(protocol.RUN_TERMINAL_STATUSES - {protocol.RUN_EXPIRED})
_RUN_RETRYABLE = frozenset(
    {protocol.RUN_FAILED, protocol.RUN_DONE_WITH_ERRORS, protocol.RUN_CANCELED}
)
_RUN_DELETABLE = frozenset(
    {
        protocol.RUN_STAGING,
        protocol.RUN_DONE,
        protocol.RUN_DONE_WITH_ERRORS,
        protocol.RUN_FAILED,
        protocol.RUN_CANCELED,
        protocol.RUN_EXPIRED,
    }
)
_BELL_STATUSES = frozenset(
    {
        protocol.RUN_DONE,
        protocol.RUN_DONE_WITH_ERRORS,
        protocol.RUN_FAILED,
        protocol.RUN_CANCELED,
    }
)
_FILE_RETRY_RESET = [protocol.STATUS_FAILED, protocol.STATUS_PENDING, protocol.STATUS_RUNNING]
_FILE_OK_STATUSES = (protocol.STATUS_VERIFIED, protocol.STATUS_VERIFIED_WITH_GAPS)

# Columns of a file row served in lists (the manifest jsonb is fetched only by
# get_file; it can be hundreds of KB for a long document).
_FILE_COLUMNS = (
    "id, run_id, source_attachment_id, filename, declared_mime, size_bytes, sha256, "
    "quarantine_path, source_format, converted_path, status, reject_code, error, phase, "
    "pages_done, "
    "last_progress_at, child_pid, page_count, pages_ok, pages_failed, hazards, "
    "derived_prefix, images_pdf_paths, text_path, restarts, elapsed_ms, sandbox_version, "
    "protocol_version, limits_hash, started_at, finished_at, created_at, updated_at, "
    # Only the source pointer of the stored manifest (the harvest records the
    # platform and the file's path there at upload time); never the whole
    # manifest, which can be large.
    "manifest->identity->source"
)
_PAGE_COLUMNS = (
    "id, file_id, page_index, status, code, detail, width_pt, height_pt, rotation, tier, "
    "thumb_path, full_path, thumb_w, thumb_h, full_w, full_h, text_chars, text_truncated, "
    "text_hazards, render_ms, created_at"
)
_EMAIL_COLUMNS = (
    "id, mailbox, subject, from_name, from_address, message_at, status, has_attachments, "
    "project_id, created_at"
)

# App-authored sentences that are not verdict codes.
_MSG_RUN_ALL_FAILED = "Every file in the run failed; retry the run."
_MSG_RUN_SPAWN = protocol.VERDICT_MESSAGES[protocol.FAIL_SPAWN]
_MSG_RUN_CANCELED = "The run was canceled."
_MSG_SELF_TEST_OK = "The sandbox self-test rendered the embedded page and its text."
_MSG_SELF_TEST_SLOT_BUSY = "The sandbox self-test uid slot was busy; try again shortly."
_MSG_SELF_TEST_ERROR = "The sandbox self-test could not be started; check the deployment."
_MSG_SELF_TEST_NOT_RUN = "The sandbox self-test has not run on this worker yet."
_MSG_CAPACITY = "The sandbox is at capacity on this worker; the run will be retried."
_MSG_INLINE_TRANSIENT = (
    "The run could not be processed on this worker; retry it from the run page."
)
_MSG_INLINE_STOPPED = "The run stopped when the worker shut down; retry it from the run page."
_MSG_STAGING_ABANDONED = (
    "The run was never started and its uploaded files have been removed."
)
_MSG_RECORD_FAILED = "A file result could not be recorded; the run will be retried."
_MSG_RUN_ROW_FAILED = "The run row could not be read; the run will be retried."

# The last self-test result (dict with ok/detail/at ...), read by execute():
# a failed self-test refuses to start any run until GET /status re-runs it.
_last_self_test: dict | None = None

# What status_report() reports for the self-test before one has run on this
# worker: the payload keeps the same shape either way, and `ok` None is this
# module's "unknown", not a failure.
_SELF_TEST_NOT_RUN: dict = {
    "ok": None,
    "detail": _MSG_SELF_TEST_NOT_RUN,
    "at": None,
    "elapsed_ms": None,
}

# Belt-and-braces per-process concurrency assertion (spec 3.6). The queue's
# second claim pass is the real limiter; this only refuses a run the queue
# should never have handed us, as a retryable infrastructure fault.
_capacity_lock = threading.Lock()
_active_runs = 0


# ── Exceptions ───────────────────────────────────────────────────────────


class RfpIngestTransient(RuntimeError):
    """A retryable infrastructure fault (storage, disk, spawn plumbing). The
    queue classifies it as `infrastructure` and retries on its ladder; the
    message is app-authored and stored verbatim (capped)."""

    llm_error_kind = "infrastructure"


class RfpIngestPermanent(ValueError):
    """A permanent, app-authored refusal. `http_status` lets the router map
    it (409 for a state conflict, 413 over a cap, 404 when a row is gone)."""

    llm_error_kind = "bad_input"

    def __init__(self, message: str, *, http_status: int = 409) -> None:
        super().__init__(message)
        self.http_status = http_status


class RfpIngestDuplicate(RfpIngestPermanent):
    """The uploaded bytes already exist in this run (`existing_file_id` is the
    row that won the partial unique index)."""

    def __init__(self, existing_file_id: str | None) -> None:
        super().__init__(protocol.VERDICT_MESSAGES[protocol.REJECT_DUPLICATE], http_status=409)
        self.existing_file_id = existing_file_id


class _FileVerdict(Exception):
    """Internal: stop processing this file with `status`/`code` (and an
    optional manifest fragment). Raised by the per-file steps and turned
    into the row's terminal CAS by `_process_file`."""

    def __init__(self, status: str, code: str, *, extra: dict | None = None) -> None:
        super().__init__(f"{status}/{code}")
        self.status = status
        self.code = code
        self.extra = extra or {}


# ── Small helpers ────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _display(value: object, max_chars: int = _FILENAME_MAX_CHARS) -> str:
    return rfp_sanitize.sanitize_display(value, max_chars=max_chars)


def _disk_free(path: Path) -> int:
    """Free bytes on the filesystem holding `path` (tests replace this)."""
    return shutil.disk_usage(path).free


def _scratch_root(s: Settings) -> Path:
    """The scratch root: `rfp_ingest_scratch_dir` or a dedicated directory
    under the system temp dir, created 0o711 so a sandbox child can traverse
    into its own work directory but never create entries in the root (a slot
    lock name planted by a child would read as a busy slot)."""
    base = Path(s.rfp_ingest_scratch_dir) if s.rfp_ingest_scratch_dir else (
        Path(tempfile.gettempdir()) / _SCRATCH_SUBDIR
    )
    try:
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o711)
    except OSError as exc:
        raise RfpIngestTransient("The sandbox scratch root could not be prepared.") from exc
    return base


def _sweep_entry_budget(s: Settings) -> int:
    """How many entries the sweep will look at in one tree before it gives up
    on it, sized off the same quantity the runner's quota mirror uses (three
    artifacts per page) plus slack for a free-form `out/tmp`. No honest tree
    comes near it; a file-count bomb does. Tests replace this."""
    return 3 * int(s.rfp_ingest_max_pages_per_file) + _SWEEP_ENTRY_SLACK


def _tree_stats(path: Path, budget: int) -> tuple[float, int, bool]:
    """(newest mtime, bytes, overflowed) anywhere under `path`, including
    `path` itself. A work directory's own mtime goes stale the moment the
    child writes only inside `out/`, so the newest entry in the tree is what
    says whether anybody is still using it. Unreadable entries are skipped.

    The tree was written by an untrusted uid and nothing bounded its entry
    count (the quota mirror the runner keeps died with the parent that owned
    it), so the walk carries the same two bounds `_dir_usage` does: at most
    `budget` entries and at most `_MAX_TREE_DEPTH` levels, and it returns
    `overflowed=True` the moment the budget runs out. Entries are streamed
    rather than listed, so peak memory is the budget and not one directory.
    """
    newest = 0.0
    total = 0
    overflowed = False
    stack = [(path, 0)]
    while stack and not overflowed:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    budget -= 1
                    if budget < 0:
                        overflowed = True
                        break
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, stat.st_mtime)
                    total += int(stat.st_size)
                    if is_dir and depth + 1 < _MAX_TREE_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
        except OSError:
            continue
    try:
        newest = max(newest, path.stat().st_mtime)
    except OSError:
        pass
    return newest, total, overflowed


def _sweep_scratch(s: Settings) -> int:
    """Remove orphaned scratch trees under the scratch root and return how
    many went. Returns 0 and never raises when the root cannot be read.

    Both scratch trees are `tempfile.mkdtemp` directories removed only by an
    in-process `finally`, so any death that skips finalizers (an OOM kill, a
    SIGKILL, a segfault) leaves up to the whole sandbox disk quota behind,
    and uvicorn respawns the worker inside the SAME container: without this
    sweep one hard death wedged `_check_disk` for the life of the container.

    The rules are deliberately conservative, since the two workers share this
    root and neither can see the other's live directories: only directories
    (never the `rfp-ingest-slot-N.lock` files, whose inode IS the uid lock),
    never symlinks, and only when nothing anywhere in the tree has been
    touched for longer than the longest file any worker could still be
    working on. A tree whose entry count exceeds `_sweep_entry_budget` is
    logged and left alone rather than walked or deleted.
    """
    try:
        root = _scratch_root(s)
    except RfpIngestTransient:
        logger.warning("rfp ingest: the scratch root could not be prepared for the sweep")
        return 0
    horizon = time.time() - (
        float(s.rfp_ingest_file_timeout_max_seconds) * _SCRATCH_SWEEP_MARGIN
    )
    budget = _sweep_entry_budget(s)
    try:
        entries = list(os.scandir(root))
    except OSError:
        logger.exception("rfp ingest: the scratch root could not be listed")
        return 0
    swept = 0
    freed = 0
    for entry in entries:
        if not entry.name.startswith(_SWEEP_PREFIXES):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        path = Path(entry.path)
        newest, size, overflowed = _tree_stats(path, budget)
        if overflowed:
            # Not merely conservative: shutil.rmtree lists each directory in
            # full, so deleting a tree with millions of entries would hit the
            # very memory peak this walk just refused. An operator removes it.
            logger.error(
                "rfp ingest: the scratch tree %s holds more entries than the sweep will walk "
                "and was left in place for an operator to remove",
                entry.name,
            )
            continue
        if newest > horizon:
            continue
        runner.cleanup_scratch(path)
        swept += 1
        freed += size
    if swept:
        logger.warning(
            "rfp ingest: swept %d orphaned scratch directories (%d bytes reclaimed)",
            swept, freed,
        )
    return swept


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) == "23505" or "23505" in text or "duplicate key" in text


def _file_timeout_seconds(s: Settings) -> int:
    """clamp(base + per_page_ms x pages, 600, max) with the per-file page cap
    as the page count: the real count is unknown until the child's document
    event, and the runner enforces the child's own deadline per spawn."""
    estimate = s.rfp_ingest_file_timeout_base_seconds + (
        s.rfp_ingest_file_timeout_per_page_ms * s.rfp_ingest_max_pages_per_file
    ) / 1000.0
    return int(
        max(_FILE_TIMEOUT_FLOOR_SECONDS, min(estimate, s.rfp_ingest_file_timeout_max_seconds))
    )


def _load_run(sb: Any, run_id: str) -> dict | None:
    rows = (sb.table(RUNS_TABLE).select("*").eq("id", run_id).limit(1).execute()).data or []
    return rows[0] if rows else None


def _run_or_404(sb: Any, run_id: str) -> dict:
    run = _load_run(sb, run_id)
    if run is None:
        raise RfpIngestPermanent("Run not found.", http_status=404)
    return run


def _run_files(sb: Any, run_id: str, columns: str = _FILE_COLUMNS) -> list[dict]:
    return (
        sb.table(FILES_TABLE)
        .select(columns)
        .eq("run_id", run_id)
        .order("created_at")
        .execute()
    ).data or []


def _counters(files: list[dict]) -> dict:
    verified = sum(1 for f in files if f.get("status") == protocol.STATUS_VERIFIED)
    gapped = sum(1 for f in files if f.get("status") == protocol.STATUS_VERIFIED_WITH_GAPS)
    rejected = sum(1 for f in files if f.get("status") == protocol.STATUS_REJECTED)
    failed = sum(1 for f in files if f.get("status") == protocol.STATUS_FAILED)
    pages = sum(
        int(f.get("page_count") or 0) for f in files if f.get("status") in _FILE_OK_STATUSES
    )
    return {
        "file_count": len(files),
        "files_verified": verified,
        "files_gapped": gapped,
        "files_rejected": rejected,
        "files_failed": failed,
        "pages_total": pages,
    }


def _delete_file_outputs(run_id: str, file_id: str) -> None:
    """Drop a file's pages rows and its derived prefix (a re-run replaces
    them), sparing `converted.pdf` so an office file is not converted a
    second time (the re-run checks its digest against the manifest before
    trusting it; delete_run and the retention prune remove the whole run
    prefix without this exception). Best-effort on the storage side: a
    listing hiccup is logged, the re-run upserts over the same deterministic
    keys anyway."""
    get_supabase().table(PAGES_TABLE).delete().eq("file_id", file_id).execute()
    try:
        rs.delete_prefix(
            rs.DERIVED_BUCKET, rs.derived_prefix(run_id, file_id), keep=(rs.CONVERTED_OBJECT,)
        )
    except Exception:  # noqa: BLE001 - orphans are swept again by delete/prune
        logger.exception("rfp ingest: derived prefix cleanup failed for file %s", file_id)


# ── Run marks (CAS) and the bell ─────────────────────────────────────────


def _mark(run_id: str, *, status: str, from_statuses: list[str] | None = None,
          **fields: Any) -> dict | None:
    """Compare-and-swap the run's status per spec 3.6 and return the updated
    row, or None when the CAS lost (the caller must then write nothing that
    assumes the transition happened).

    Fences: pending / running / failed / done / done_with_errors apply only
    from pending|running; canceled is sticky (applies from
    staging|pending|running and nothing ever moves a run out of it);
    expired applies only from a terminal status. staging -> pending is
    start_run's own CAS, never this function's.

    A terminal transition that wins the CAS sends the `rfp_ingest.finished`
    bell once (this write is the only one that can win), and failed or
    canceled also hands the run's `running` files back to pending with
    reject_code `interrupted` so a retry revisits them.

    `from_statuses` narrows (never widens) the fence to a subset of the
    transition's own allowed set: the retention sweep cancels an abandoned
    `staging` run that way, so a run a user started in the same second is
    left alone instead of being canceled out from under them.
    """
    if status == protocol.RUN_CANCELED:
        allowed = _RUN_CANCELABLE_FROM
    elif status == protocol.RUN_EXPIRED:
        allowed = _RUN_EXPIRABLE_FROM
    elif status in (
        protocol.RUN_PENDING,
        protocol.RUN_RUNNING,
        protocol.RUN_FAILED,
        protocol.RUN_DONE,
        protocol.RUN_DONE_WITH_ERRORS,
    ):
        allowed = _RUN_ACTIVE_FROM
    else:
        raise ValueError(f"_mark cannot move a run to {status!r}")
    if from_statuses is not None:
        allowed = [value for value in allowed if value in from_statuses]
        if not allowed:
            raise ValueError(f"_mark cannot move a run to {status!r} from {from_statuses!r}")
    payload = {"status": status, **fields}
    if status == protocol.RUN_RUNNING:
        payload.setdefault("started_at", _now_iso())
        payload.setdefault("completed_at", None)
    elif status == protocol.RUN_PENDING:
        payload.setdefault("completed_at", None)
    elif status in protocol.RUN_TERMINAL_STATUSES and status != protocol.RUN_EXPIRED:
        payload.setdefault("completed_at", _now_iso())
    sb = get_supabase()
    rows = (
        sb.table(RUNS_TABLE).update(payload).eq("id", run_id).in_("status", allowed).execute()
    ).data or []
    if not rows:
        return None
    row = rows[0]
    if status in (protocol.RUN_FAILED, protocol.RUN_CANCELED):
        _release_running_files(sb, run_id)
    if status in _BELL_STATUSES:
        _notify_run_finished(row, status)
    return row


def _release_running_files(sb: Any, run_id: str) -> None:
    """A failed or canceled run has no runner any more (or is about to lose
    it): its running files go back to pending as `interrupted`, so a retry
    processes them and the UI stops showing a live phase."""
    sb.table(FILES_TABLE).update(
        {
            "status": protocol.STATUS_PENDING,
            "reject_code": protocol.FAIL_INTERRUPTED,
            "error": protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED],
            "claim_token": None,
            "phase": None,
            "child_pid": None,
            "pages_done": 0,
        }
    ).eq("run_id", run_id).eq("status", protocol.STATUS_RUNNING).execute()


def _finished_message(row: dict, status: str) -> str:
    if status == protocol.RUN_FAILED:
        return f"RFP ingestion run failed: {row.get('error') or _MSG_RUN_ALL_FAILED}"
    if status == protocol.RUN_CANCELED:
        return f"RFP ingestion run canceled: {row.get('error') or _MSG_RUN_CANCELED}"
    return (
        "RFP ingestion run finished: "
        f"{int(row.get('files_verified') or 0)} verified, "
        f"{int(row.get('files_gapped') or 0)} with gaps, "
        f"{int(row.get('files_rejected') or 0)} rejected, "
        f"{int(row.get('files_failed') or 0)} failed"
    )


def _notify_run_finished(row: dict, status: str) -> None:
    """The once-only bell to whoever started the run. Best-effort: a
    notification failure never fails the status write."""
    user_id = row.get("created_by")
    if not user_id:
        return
    try:
        notifications.notify_user(
            user_id,
            None,
            RUN_FINISHED_NOTIFICATION,
            _finished_message(row, status),
            mirror_email=False,
            metadata={
                "run_id": row["id"],
                "status": status,
                "files_verified": int(row.get("files_verified") or 0),
                "files_gapped": int(row.get("files_gapped") or 0),
                "files_rejected": int(row.get("files_rejected") or 0),
                "files_failed": int(row.get("files_failed") or 0),
            },
        )
    except Exception:  # noqa: BLE001 - the status write stands; the bell is best-effort
        logger.exception("rfp ingest: completion notification failed for run %s", row["id"])


# ── Queue adapter ────────────────────────────────────────────────────────


def mark_from_queue(run_id: str, status: str, error: str | None) -> None:
    """The queue's domain marks: "pending" when a retry is scheduled and
    "failed" when the job ends terminally (its message is app-authored by
    llm_errors). Both go through `_mark`, so a canceled or already finished
    run ignores them."""
    if status == protocol.RUN_PENDING:
        _mark(run_id, status=protocol.RUN_PENDING, error=None)
    elif status == protocol.RUN_FAILED:
        _mark(
            run_id,
            status=protocol.RUN_FAILED,
            error=(error or protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED])[
                :_ERROR_MAX_CHARS
            ],
        )
    else:
        logger.warning("rfp ingest: ignoring queue mark %r for run %s", status, run_id)


def current_status(run_id: str) -> str | None:
    """The run's status for the queue: every terminal status reads as "done"
    (so `requeue_terminal` refuses), staging/pending/running pass through,
    None when the row is gone."""
    run = _load_run(get_supabase(), run_id)
    if run is None:
        return None
    status = run.get("status")
    if status in protocol.RUN_TERMINAL_STATUSES:
        return "done"
    return status


def _acquire_capacity(s: Settings) -> bool:
    global _active_runs
    with _capacity_lock:
        if _active_runs >= max(1, int(s.rfp_ingest_sandbox_concurrency)):
            return False
        _active_runs += 1
        return True


def _release_capacity() -> None:
    global _active_runs
    with _capacity_lock:
        _active_runs = max(0, _active_runs - 1)


def active_run_count() -> int:
    with _capacity_lock:
        return _active_runs


# ── Run creation, uploads, start, dispatch ───────────────────────────────


def create_upload_run(*, created_by: str | None) -> dict:
    """A `staging` run that collects uploaded files until `start_run`."""
    row = {
        "source_kind": "upload",
        "status": protocol.RUN_STAGING,
        "created_by": created_by,
        "file_count": 0,
    }
    return get_supabase().table(RUNS_TABLE).insert(row).execute().data[0]


def create_email_run(*, email_id: str, created_by: str | None) -> tuple[dict, list[dict]]:
    """A `pending` run with one file row per `ingested_email_attachments` row
    of the email (metadata only; the runner materializes the bytes). Raises
    RfpIngestPermanent 404 when the email is unknown, 409 when it has no
    attachment rows, 413 when it has more than the per-run cap. The ROUTER
    dispatches the returned run."""
    sb = get_supabase()
    s = get_settings()
    emails = (
        sb.table("ingested_emails").select(_EMAIL_COLUMNS).eq("id", email_id).limit(1).execute()
    ).data or []
    if not emails:
        raise RfpIngestPermanent("Email not found.", http_status=404)
    attachments = (
        sb.table("ingested_email_attachments")
        .select("id, graph_attachment_id, filename, mime_type, size_bytes, storage_path, "
                "skipped_reason")
        .eq("email_id", email_id)
        .order("created_at")
        .execute()
    ).data or []
    if not attachments:
        raise RfpIngestPermanent("The email has no attachments to ingest.", http_status=409)
    if len(attachments) > s.rfp_ingest_max_files_per_run:
        raise RfpIngestPermanent(
            "The email has more attachments than the per-run file limit.", http_status=413
        )
    run = (
        sb.table(RUNS_TABLE)
        .insert(
            {
                "source_kind": "email",
                "email_id": email_id,
                "status": protocol.RUN_PENDING,
                "created_by": created_by,
                "file_count": len(attachments),
            }
        )
        .execute()
    ).data[0]
    rows = [
        {
            "run_id": run["id"],
            "source_attachment_id": att["id"],
            "filename": _display(att.get("filename")) or "attachment",
            "declared_mime": _display(att.get("mime_type"), _MIME_MAX_CHARS) or None,
            "status": protocol.STATUS_PENDING,
            "quarantine_path": None,
            "size_bytes": None,
        }
        for att in attachments
    ]
    files = sb.table(FILES_TABLE).insert(rows).execute().data or []
    return run, files


def create_harvest_run(
    *, rfp_email_id: str, harvest_id: str, test_session_id: str | None = None
) -> dict:
    """A `staging` run for the RFP harvest (docs/RFP_HARVEST.md): the harvest
    job adds the platform's documents through `add_upload_file` and starts
    it. `created_by` is null (the system), the run carries the email and the
    harvest it belongs to, and the test bench's tag when the harvest is a
    test session's (docs/RFP_TESTING.md 4.4; its cleanup deletes the run)."""
    row = {
        "source_kind": "rfp_email",
        "rfp_email_id": rfp_email_id,
        "harvest_id": harvest_id,
        "status": protocol.RUN_STAGING,
        "created_by": None,
        "file_count": 0,
    }
    if test_session_id:
        row["test_session_id"] = test_session_id
    return get_supabase().table(RUNS_TABLE).insert(row).execute().data[0]


def create_portal_run(*, portal_invitation_id: str, harvest_id: str) -> dict:
    """A `staging` run for a portal harvest (docs/RFP_NGEM_PORTAL.md 2.4):
    the harvest job adds the bid's attachments through `add_upload_file`
    (source `{kind: "ngem", file_name, harvest_id}`) and starts it. The run
    carries the invitation and the harvest it belongs to; `created_by` is
    null (the system)."""
    row = {
        "source_kind": "rfp_portal",
        "portal_invitation_id": portal_invitation_id,
        "harvest_id": harvest_id,
        "status": protocol.RUN_STAGING,
        "created_by": None,
        "file_count": 0,
    }
    return get_supabase().table(RUNS_TABLE).insert(row).execute().data[0]


def _identity(filename: str, declared_mime: str | None, source: dict) -> dict:
    return {
        "filename": filename,
        "declared_mime": declared_mime,
        "source": source,
    }


def _sniff_fragment(res: rfp_sanitize.SniffResult) -> dict:
    return {
        **{key: res.flags.get(key) for key in rfp_sanitize.SNIFF_FLAG_KEYS},
        "byte_markers": dict(res.byte_markers),
        "verdict": res.verdict,
        "source_format": res.source_format,
    }


def _sniff_name(s: Settings, filename: str | None) -> str | None:
    """The filename the sniff may use for the office-format branch: the
    display-sanitized name while office files are enabled, None (PDF-only
    rules, the pre-2.1 behaviour) while they are off."""
    return filename if s.rfp_ingest_office_files_enabled and filename else None


def add_upload_file(
    run_id: str,
    *,
    filename: str,
    declared_mime: str | None,
    data: bytes,
    source: dict | None = None,
) -> dict:
    """Record one uploaded file (a PDF, or with office files enabled a Word
    or Excel document recognised by magic and extension) on a staging run:
    sniff and sha256 in the caller's thread, the row inserted BEFORE the
    object upload (the partial unique index on (run_id, sha256) is the
    duplicate check: a 23505 raises RfpIngestDuplicate with the winning file
    id), rejected-at-sniff files recorded as `rejected` with no object
    (quarantine_path null), and an upload failure recorded as
    `failed/storage` so no object is ever orphaned. The row's
    `source_format` and the quarantine object's extension and content type
    come from the sniff, never from the declared name. Raises
    RfpIngestPermanent 404/409/413 (http_status) when the run is missing,
    not staging, or already holds the per-run maximum. `source` replaces the
    manifest's `{kind: "upload"}` pointer (the harvest records the platform
    and the file's path there)."""
    sb = get_supabase()
    s = get_settings()
    run = _run_or_404(sb, run_id)
    if run.get("status") != protocol.RUN_STAGING:
        raise RfpIngestPermanent(
            "Files can only be added while the run is staging.", http_status=409
        )
    existing = (
        sb.table(FILES_TABLE).select("id", count="exact").eq("run_id", run_id).execute()
    )
    count = existing.count if existing.count is not None else len(existing.data or [])
    if count >= s.rfp_ingest_max_files_per_run:
        raise RfpIngestPermanent(
            f"The run already holds {s.rfp_ingest_max_files_per_run} files, the per-run limit.",
            http_status=413,
        )
    name = _display(filename) or "upload.pdf"
    mime = _display(declared_mime, _MIME_MAX_CHARS) or None
    size = len(data)
    sha = rfp_sanitize.sha256_bytes(data)
    manifest: dict = {"identity": {**_identity(name, mime, source or {"kind": "upload"})}}
    manifest["identity"]["sha256"] = sha
    manifest["identity"]["size_bytes"] = size
    verdict: str | None = None
    source_format = protocol.SOURCE_FORMAT_PDF
    if size > s.rfp_ingest_max_file_bytes:
        verdict = protocol.REJECT_TOO_LARGE
    else:
        res = rfp_sanitize.sniff_bytes(data, filename=_sniff_name(s, name))
        manifest["sniff"] = _sniff_fragment(res)
        verdict = res.verdict
        source_format = res.source_format or source_format
    manifest["identity"]["source_format"] = source_format
    row: dict = {
        "run_id": run_id,
        "filename": name,
        "declared_mime": mime,
        "size_bytes": size,
        "sha256": sha,
        "quarantine_path": None,
        "source_format": source_format,
        "status": protocol.STATUS_PENDING,
        "manifest": manifest,
    }
    if verdict:
        manifest["verdict"] = {"status": protocol.STATUS_REJECTED, "code": verdict}
        row.update(
            status=protocol.STATUS_REJECTED,
            reject_code=verdict,
            error=protocol.VERDICT_MESSAGES[verdict],
            finished_at=_now_iso(),
        )
    try:
        frow = sb.table(FILES_TABLE).insert(row).execute().data[0]
    except Exception as exc:  # noqa: BLE001 - only the duplicate race is expected
        if not _is_unique_violation(exc):
            raise
        winners = (
            sb.table(FILES_TABLE)
            .select("id")
            .eq("run_id", run_id)
            .eq("sha256", sha)
            .limit(1)
            .execute()
        ).data or []
        raise RfpIngestDuplicate(winners[0]["id"] if winners else None) from exc
    sb.table(RUNS_TABLE).update({"file_count": count + 1}).eq("id", run_id).execute()
    if verdict:
        return frow
    path = rs.quarantine_path(run_id, frow["id"], source_format)
    try:
        rs.upload_bytes(rs.QUARANTINE_BUCKET, path, data, rs.source_content_type(source_format))
    except Exception:  # noqa: BLE001 - recorded as failed/storage, never an orphan object
        logger.exception("rfp ingest: quarantine upload failed for file %s", frow["id"])
        rows = (
            sb.table(FILES_TABLE)
            .update(
                {
                    "status": protocol.STATUS_FAILED,
                    "reject_code": protocol.FAIL_STORAGE,
                    "error": protocol.VERDICT_MESSAGES[protocol.FAIL_STORAGE],
                    "finished_at": _now_iso(),
                }
            )
            .eq("id", frow["id"])
            .execute()
        ).data or []
        return rows[0] if rows else frow
    rows = (
        sb.table(FILES_TABLE).update({"quarantine_path": path}).eq("id", frow["id"]).execute()
    ).data or []
    return rows[0] if rows else frow


def start_run(run_id: str) -> dict:
    """CAS staging -> pending. Raises RfpIngestPermanent 409 (http_status)
    when the run is not staging, has no files, or every file was rejected at
    upload. The ROUTER dispatches the returned run."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") != protocol.RUN_STAGING:
        raise RfpIngestPermanent("The run has already been started.", http_status=409)
    files = _run_files(sb, run_id, "id, status")
    if not files:
        raise RfpIngestPermanent("The run has no files.", http_status=409)
    if all(f.get("status") == protocol.STATUS_REJECTED for f in files):
        raise RfpIngestPermanent(
            "Every file in the run was rejected at upload.", http_status=409
        )
    rows = (
        sb.table(RUNS_TABLE)
        .update({"status": protocol.RUN_PENDING, "file_count": len(files), "error": None})
        .eq("id", run_id)
        .eq("status", protocol.RUN_STAGING)
        .execute()
    ).data or []
    if not rows:
        raise RfpIngestPermanent("The run has already been started.", http_status=409)
    return rows[0]


def dispatch(
    run_id: str,
    *,
    created_by: str | None,
    background: Any | None,
    raise_on_active: bool = False,
) -> dict | None:
    """Queue the run (one llm_jobs row per run, priority
    rfp_ingest_queue_priority), degrading to FastAPI BackgroundTasks when the
    queue is off or the enqueue fails, exactly like the splitter's _dispatch.
    Returns the job row, or None when the BackgroundTasks fallback took it.
    With `raise_on_active`, llm_queue.JobAlreadyActive propagates (the retry
    route maps it to 409) instead of falling back."""
    s = get_settings()
    if s.llm_queue_enabled:
        try:
            return llm_queue.enqueue(
                llm_queue.JOB_RFP_INGEST,
                target_id=run_id,
                payload={"run_id": run_id},
                created_by=created_by,
                priority=s.rfp_ingest_queue_priority,
                raise_on_active=raise_on_active,
            )
        except llm_queue.JobAlreadyActive:
            raise
        except Exception:  # noqa: BLE001 - queue outage degrades to inline dispatch
            logger.exception("rfp ingest: enqueue failed; falling back to BackgroundTasks")
    if background is None:
        logger.error("rfp ingest: no dispatcher available for run %s", run_id)
        return None
    background.add_task(run_in_background, run_id)
    return None


def run_in_background(run_id: str) -> None:
    """BackgroundTasks fallback (queue off or enqueue failed): same run, but
    it owns every terminal mark, since no queue will write one.

    Nothing retries this path, so neither of the two outcomes the queue would
    have handled may be left dangling: a retryable fault
    (`RfpIngestTransient`, e.g. the per-worker capacity assertion) is recorded
    as failed with a sentence that asks for a retry instead of promising one,
    and a run left active because the worker is shutting down (there is no
    llm_jobs row to requeue and no sweep to find it) is failed the same way,
    so `/retry` can pick it up rather than the run hanging in `running`."""
    try:
        execute(run_id)
    except Exception as exc:  # noqa: BLE001 - terminal mark mirrors the queue's
        logger.exception("rfp ingest: run %s failed (inline dispatch)", run_id)
        if isinstance(exc, RfpIngestTransient):
            message = _MSG_INLINE_TRANSIENT
        elif isinstance(exc, RfpIngestPermanent):
            message = str(exc)
        else:
            message = protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED]
        try:
            _mark(run_id, status=protocol.RUN_FAILED, error=message[:_ERROR_MAX_CHARS])
        except Exception:  # noqa: BLE001 - nothing left to try
            logger.exception("rfp ingest: terminal mark failed for run %s", run_id)
        return
    try:
        if _run_status(get_supabase(), run_id) in _RUN_ACTIVE_FROM:
            logger.warning("rfp ingest: run %s was left active by the inline dispatch", run_id)
            _mark(run_id, status=protocol.RUN_FAILED, error=_MSG_INLINE_STOPPED)
    except Exception:  # noqa: BLE001 - nothing left to try
        logger.exception("rfp ingest: terminal mark failed for run %s", run_id)


# ── execute: per-run context and per-file state ──────────────────────────


class _ClaimLost(Exception):
    """Internal: a write fenced on the file's claim matched nothing (the file
    was reset by a cancel or reclaimed by a retry); abandon it silently."""


class _SinkAborted(Exception):
    """Internal: `should_abort()` said stop between two upload batches of the
    runner's page delivery. `_FileState.abort` says why, exactly as it does
    for an abort the runner itself noticed."""


@dataclass
class _RunContext:
    s: Settings
    sb: Any
    run: dict
    limits: SandboxLimits
    scratch_root: Path
    consumed: int = 0
    email: dict | None = None
    email_loaded: bool = False
    state: _FileState | None = None
    manifest: dict | None = None

    @property
    def run_id(self) -> str:
        return self.run["id"]


@dataclass
class _FileState:
    file_id: str
    token: str
    started_at: str
    claim_lost: bool = False
    abort: str | None = None          # shutdown | claim | cancel | stop
    last_progress_write: float = 0.0
    last_cancel_check: float = 0.0
    bounds: list[str] = field(default_factory=list)


def _renew() -> bool:
    return llm_queue.renew_lease()


def _run_status(sb: Any, run_id: str) -> str | None:
    rows = (sb.table(RUNS_TABLE).select("status").eq("id", run_id).limit(1).execute()).data or []
    return rows[0].get("status") if rows else None


def _update_claimed(ctx: _RunContext, state: _FileState, fields: dict) -> bool:
    """A write fenced on (id, status running, claim_token). False = lost."""
    rows = (
        ctx.sb.table(FILES_TABLE)
        .update(fields)
        .eq("id", state.file_id)
        .eq("status", protocol.STATUS_RUNNING)
        .eq("claim_token", state.token)
        .execute()
    ).data or []
    return bool(rows)


def _set_phase(ctx: _RunContext, state: _FileState, phase: str) -> None:
    try:
        if not _update_claimed(ctx, state, {"phase": phase, "last_progress_at": _now_iso()}):
            state.claim_lost = True
    except Exception:  # noqa: BLE001 - a progress write must never fail the file
        logger.exception("rfp ingest: phase update failed for file %s", state.file_id)


def _make_callbacks(ctx: _RunContext, state: _FileState):
    """`should_abort` and `on_progress` for one file's sandbox run. Both are
    called from the runner's monitor loop: should_abort every tick (the run
    row is re-read at most every _CANCEL_POLL_SECONDS), on_progress at most
    every 2 s (written through at most every _PROGRESS_WRITE_SECONDS). Neither
    lets a transient database error escape, since the runner would kill the
    child and propagate it."""

    def should_abort() -> bool:
        if SHUTTING_DOWN.is_set():
            state.abort = "shutdown"
            return True
        if state.claim_lost:
            state.abort = "claim"
            return True
        now = time.monotonic()
        if now - state.last_cancel_check >= _CANCEL_POLL_SECONDS:
            state.last_cancel_check = now
            try:
                status = _run_status(ctx.sb, ctx.run_id)
            except Exception:  # noqa: BLE001 - a read hiccup is not a cancel
                logger.warning("rfp ingest: cancel poll failed for run %s", ctx.run_id)
                return False
            if status != protocol.RUN_RUNNING:
                state.abort = "cancel" if status == protocol.RUN_CANCELED else "stop"
                return True
        return False

    def on_progress(phase: str, pages_done: int, child_pid: int | None) -> None:
        now = time.monotonic()
        if state.last_progress_write and now - state.last_progress_write < _PROGRESS_WRITE_SECONDS:
            return
        state.last_progress_write = now
        try:
            if not _update_claimed(
                ctx,
                state,
                {
                    "phase": phase,
                    "pages_done": int(pages_done),
                    "last_progress_at": _now_iso(),
                    "child_pid": child_pid,
                },
            ):
                state.claim_lost = True
        except Exception:  # noqa: BLE001 - never kill the child over a progress write
            logger.exception("rfp ingest: progress update failed for file %s", state.file_id)

    return should_abort, on_progress


# ── execute: per-file steps ──────────────────────────────────────────────


def _claim_file(ctx: _RunContext, frow: dict) -> str | None:
    """CAS pending -> running with a fresh claim token; None when lost."""
    token = uuid.uuid4().hex
    now = _now_iso()
    rows = (
        ctx.sb.table(FILES_TABLE)
        .update(
            {
                "status": protocol.STATUS_RUNNING,
                "claim_token": token,
                "reject_code": None,
                "error": None,
                "phase": protocol.PHASE_MATERIALIZE,
                "pages_done": 0,
                "last_progress_at": now,
                "child_pid": None,
                "started_at": now,
                "finished_at": None,
            }
        )
        .eq("id", frow["id"])
        .eq("status", protocol.STATUS_PENDING)
        .execute()
    ).data or []
    return token if rows else None


def _check_disk(ctx: _RunContext, frow: dict) -> None:
    mb = 1024 * 1024
    estimate = int(frow.get("size_bytes") or ctx.s.rfp_ingest_max_file_bytes) + (
        ctx.s.rfp_ingest_sandbox_disk_mb * mb
    )
    reserve = ctx.s.rfp_ingest_scratch_reserve_mb * mb
    try:
        free = int(_disk_free(ctx.scratch_root))
    except OSError as exc:
        raise _FileVerdict(protocol.STATUS_FAILED, protocol.FAIL_STORAGE) from exc
    if free - estimate < reserve:
        logger.warning(
            "rfp ingest: %d bytes free under the %d reserve for file %s; skipping",
            free, reserve, frow["id"],
        )
        raise _FileVerdict(
            protocol.STATUS_FAILED,
            protocol.FAIL_STORAGE,
            extra={"disk": {"free_bytes": free, "estimate_bytes": estimate,
                            "reserve_bytes": reserve}},
        )


def _email_row(ctx: _RunContext) -> dict | None:
    if not ctx.email_loaded:
        ctx.email_loaded = True
        email_id = ctx.run.get("email_id")
        if email_id:
            rows = (
                ctx.sb.table("ingested_emails")
                .select("id, mailbox, graph_message_id")
                .eq("id", email_id)
                .limit(1)
                .execute()
            ).data or []
            ctx.email = rows[0] if rows else None
    return ctx.email


def _write_excl(dest: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dest, flags, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _materialize(ctx: _RunContext, frow: dict, dest: Path) -> dict:
    """Bring the file's bytes into `dest` (created O_EXCL by the helper that
    writes it). Returns the manifest's source pointer. Raises _FileVerdict:
    rejected/not_stored (nothing to fetch any more), rejected/too_large,
    rejected/item_attachment, or failed/storage (retryable)."""
    cap = ctx.s.rfp_ingest_max_file_bytes
    if ctx.run.get("source_kind") != "email":
        path = frow.get("quarantine_path")
        if not path:
            raise _FileVerdict(protocol.STATUS_FAILED, protocol.FAIL_STORAGE)
        try:
            rs.download_to_file(rs.QUARANTINE_BUCKET, path, dest, max_bytes=cap)
        except rs.RfpStorageNotFound as exc:
            raise _FileVerdict(protocol.STATUS_REJECTED, protocol.REJECT_NOT_STORED) from exc
        except rs.RfpStorageTooLarge as exc:
            raise _FileVerdict(protocol.STATUS_REJECTED, protocol.REJECT_TOO_LARGE) from exc
        except (rs.RfpStorageError, httpx.HTTPError, OSError) as exc:
            raise _FileVerdict(protocol.STATUS_FAILED, protocol.FAIL_STORAGE) from exc
        # A file a harvest uploaded keeps its platform provenance (Procore:
        # kind, file_path, harvest_id; NGEM: kind "ngem", file_name,
        # harvest_id); a page upload is recorded as such.
        prior = frow.get("source")
        if isinstance(prior, dict) and prior.get("kind") not in (None, "upload", "email"):
            return {**prior, "quarantine_path": path}
        return {"kind": "upload", "quarantine_path": path}

    att_id = frow.get("source_attachment_id")
    if not att_id:
        raise _FileVerdict(protocol.STATUS_REJECTED, protocol.REJECT_NOT_STORED)
    rows = (
        ctx.sb.table("ingested_email_attachments")
        .select("id, email_id, graph_attachment_id, filename, mime_type, size_bytes, "
                "storage_path, skipped_reason")
        .eq("id", att_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise _FileVerdict(protocol.STATUS_REJECTED, protocol.REJECT_NOT_STORED)
    att = rows[0]
    email = _email_row(ctx)
    source = {
        "kind": "email",
        "email_id": ctx.run.get("email_id"),
        "attachment_id": att_id,
        "graph_attachment_id": att.get("graph_attachment_id"),
        "mailbox": (email or {}).get("mailbox"),
        "stored_copy": bool(att.get("storage_path")),
    }
    if att.get("skipped_reason") == "item_attachment":
        raise _FileVerdict(
            protocol.STATUS_REJECTED, protocol.REJECT_ITEM_ATTACHMENT, extra={"source": source}
        )
    if att.get("storage_path"):
        try:
            data = storage.download_file(att["storage_path"])
        except Exception as exc:  # noqa: BLE001 - any SDK/transport failure is retryable
            raise _FileVerdict(
                protocol.STATUS_FAILED, protocol.FAIL_STORAGE, extra={"source": source}
            ) from exc
        if len(data) > cap:
            raise _FileVerdict(
                protocol.STATUS_REJECTED, protocol.REJECT_TOO_LARGE, extra={"source": source}
            )
        _write_excl(dest, data)
        return source
    graph_id = att.get("graph_attachment_id")
    if not graph_id or not email or not email.get("mailbox") or not email.get("graph_message_id"):
        raise _FileVerdict(
            protocol.STATUS_REJECTED, protocol.REJECT_NOT_STORED, extra={"source": source}
        )
    try:
        graph_inbox.download_attachment_to_file(
            email["graph_message_id"],
            graph_id,
            mailbox=email["mailbox"],
            max_bytes=cap,
            dest=dest,
        )
    except graph_inbox.AttachmentNotStored as exc:
        raise _FileVerdict(
            protocol.STATUS_REJECTED, protocol.REJECT_NOT_STORED, extra={"source": source}
        ) from exc
    except graph_inbox.AttachmentTooLarge as exc:
        raise _FileVerdict(
            protocol.STATUS_REJECTED, protocol.REJECT_TOO_LARGE, extra={"source": source}
        ) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise _FileVerdict(
            protocol.STATUS_FAILED, protocol.FAIL_STORAGE, extra={"source": source}
        ) from exc
    return source


def _record_identity(
    ctx: _RunContext, state: _FileState, sha: str, size: int, source_format: str
) -> None:
    """CAS {sha256, size_bytes, source_format} fenced on the claim; a 23505
    from the partial unique index means the same bytes are already in the
    run."""
    try:
        won = _update_claimed(
            ctx, state, {"sha256": sha, "size_bytes": size, "source_format": source_format}
        )
    except Exception as exc:  # noqa: BLE001 - only the duplicate index is expected
        if not _is_unique_violation(exc):
            raise
        winners = (
            ctx.sb.table(FILES_TABLE)
            .select("id")
            .eq("run_id", ctx.run_id)
            .eq("sha256", sha)
            .neq("id", state.file_id)
            .limit(1)
            .execute()
        ).data or []
        raise _FileVerdict(
            protocol.STATUS_REJECTED,
            protocol.REJECT_DUPLICATE,
            extra={"identity": {"duplicate_of": winners[0]["id"] if winners else None}},
        ) from exc
    if not won:
        raise _ClaimLost()


def _prior_conversion(ctx: _RunContext, state: _FileState, frow: dict) -> dict | None:
    """The `conversion` block an earlier attempt recorded for this file, when
    it left a `converted.pdf` behind (the manifest column is read on its own
    here: it is not in _FILE_COLUMNS and only an office file on a re-run
    needs it)."""
    if not frow.get("converted_path"):
        return None
    rows = (
        ctx.sb.table(FILES_TABLE).select("manifest").eq("id", state.file_id).limit(1).execute()
    ).data or []
    manifest = rows[0].get("manifest") if rows else None
    prior = manifest.get("conversion") if isinstance(manifest, dict) else None
    if isinstance(prior, dict) and isinstance(prior.get("pdf_sha256"), str):
        return prior
    return None


_CONVERSION_KEYS = (
    "engine", "route", "source_format", "duration_ms", "http_status", "pdf_sha256", "pdf_bytes",
)


def _reuse_converted(ctx: _RunContext, cpath: str, dest: Path, prior: dict) -> dict | None:
    """Bring an earlier attempt's `converted.pdf` back into `dest` and accept
    it only when its digest is the one the manifest recorded. Anything else
    (gone, unreadable, a different digest) means a fresh conversion."""
    try:
        rs.download_to_file(rs.DERIVED_BUCKET, cpath, dest, max_bytes=ctx.s.rfp_ingest_max_file_bytes)
    except rs.RfpStorageNotFound:
        return None
    except (rs.RfpStorageError, httpx.HTTPError, OSError):
        logger.warning("rfp ingest: converted.pdf could not be reused for file %s", cpath)
        _unlink_quietly(dest)
        return None
    sha, size = rfp_sanitize.sha256_file(dest)
    if sha != prior.get("pdf_sha256") or size != prior.get("pdf_bytes"):
        logger.warning("rfp ingest: converted.pdf digest changed for %s; reconverting", cpath)
        _unlink_quietly(dest)
        return None
    return {**{key: prior.get(key) for key in _CONVERSION_KEYS}, "reused": True}


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _convert_office(
    ctx: _RunContext, state: _FileState, frow: dict, raw: Path, dest: Path, source_format: str
) -> dict:
    """Spec section 2.1: turn the office file at `raw` into the PDF at
    `dest` the child will be run over, and return the manifest's
    `conversion` block. A re-run reuses the `converted.pdf` an earlier
    attempt uploaded when its digest matches that attempt's manifest;
    otherwise the raw bytes go to Gotenberg (never opened here) under the
    configured timeout and the per-file byte cap. The returned PDF gets the
    parent's own byte sniff before anything else touches it, and is uploaded
    to the derived bucket so the next attempt can skip the converter.
    Raises _FileVerdict: failed/conversion_unavailable (retryable),
    rejected/conversion_rejected, rejected/too_large."""
    s = ctx.s
    _set_phase(ctx, state, protocol.PHASE_CONVERT)
    cpath = rs.converted_path(ctx.run_id, state.file_id)
    info: dict | None = None
    prior = _prior_conversion(ctx, state, frow)
    if prior is not None and frow.get("converted_path") == cpath:
        info = _reuse_converted(ctx, cpath, dest, prior)
    if info is None:
        try:
            info = office.convert_to_pdf(
                raw,
                dest,
                source_format=source_format,
                max_bytes=s.rfp_ingest_max_file_bytes,
                timeout_seconds=s.rfp_ingest_office_convert_timeout_seconds,
                base_url=s.gotenberg_base_url,
            )
        except office.ConversionUnavailable as exc:
            logger.warning("rfp ingest: converter unavailable for file %s: %s", state.file_id, exc)
            raise _FileVerdict(
                protocol.STATUS_FAILED, protocol.FAIL_CONVERSION_UNAVAILABLE,
                extra={"conversion": exc.info},
            ) from exc
        except office.ConversionRejected as exc:
            raise _FileVerdict(
                protocol.STATUS_REJECTED, protocol.REJECT_CONVERSION_REJECTED,
                extra={"conversion": exc.info},
            ) from exc
        except office.ConversionTooLarge as exc:
            raise _FileVerdict(
                protocol.STATUS_REJECTED, protocol.REJECT_TOO_LARGE,
                extra={"conversion": exc.info},
            ) from exc
        info["reused"] = False
    # The converter's output is attacker-influenced bytes like any upload:
    # the same parent sniff (PDF rules, no filename) runs before the child
    # ever sees it, and a non-PDF answer is the converter's refusal.
    pdf_sniff = rfp_sanitize.sniff_file(dest)
    info["pdf_sniff"] = _sniff_fragment(pdf_sniff)
    if pdf_sniff.verdict:
        _unlink_quietly(dest)
        raise _FileVerdict(
            protocol.STATUS_REJECTED, protocol.REJECT_CONVERSION_REJECTED,
            extra={"conversion": info},
        )
    if not llm_queue.renew_lease():
        raise runner.LeaseLost("queue lease lost during the office conversion")
    if not info["reused"]:
        rs.upload_file(rs.DERIVED_BUCKET, cpath, dest, "application/pdf")
        if not _update_claimed(ctx, state, {"converted_path": cpath}):
            raise _ClaimLost()
    info["converted_path"] = cpath
    return info


def _run_file_sandbox(
    ctx: _RunContext, state: _FileState, dest: Path, should_abort, on_progress, page_sink
) -> SandboxResult:
    """Claim a uid slot (None locally), run the sandbox, release the slot.
    `page_sink` receives every validated page and its two images as the
    runner delivers them, so the result that comes back carries no image
    bytes at all (spec section 2)."""
    s = ctx.s
    _set_phase(ctx, state, protocol.PHASE_SANDBOX)
    slot = runner.acquire_uid_slot(
        ctx.scratch_root,
        sandbox_uid=s.rfp_ingest_sandbox_uid,
        pool_base=s.rfp_ingest_sandbox_uid_pool_base,
        pool_size=s.rfp_ingest_sandbox_uid_pool_size,
        renew=_renew,
        should_abort=should_abort,
    )
    try:
        return runner.run_sandbox(
            dest,
            limits=ctx.limits,
            scratch_root=ctx.scratch_root,
            open_timeout_seconds=s.rfp_ingest_open_timeout_seconds,
            page_stall_seconds=s.rfp_ingest_page_stall_seconds,
            file_timeout_seconds=_file_timeout_seconds(s),
            max_restarts=s.rfp_ingest_max_child_restarts,
            max_pages_remaining=max(0, s.rfp_ingest_max_pages_per_run - ctx.consumed),
            uid_slot=slot,
            should_abort=should_abort,
            on_progress=on_progress,
            renew=_renew,
            restart_ratio=s.rfp_ingest_max_failed_page_ratio,
            min_failed_pages_allowed=s.rfp_ingest_min_failed_pages_allowed,
            failed_page_ratio=s.rfp_ingest_max_failed_page_ratio,
            max_text_bytes_per_file=s.rfp_ingest_max_text_bytes_per_file,
            page_sink=page_sink,
        )
    finally:
        if slot is not None:
            slot.release()


def _child_protocol_version(value: object) -> int:
    """The child's claimed protocol version, or the parent's constant. Both
    provenance versions land in typed columns (int4 / text), so a child that
    reports something else must not be able to poison the terminal CAS with a
    numeric-overflow or invalid-syntax error the file could never recover
    from."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
        return value
    return protocol.PROTOCOL_VERSION


def _child_sandbox_version(value: object) -> str:
    if isinstance(value, str) and value:
        return _display(value, 64)
    return protocol.SANDBOX_VERSION


def _sandbox_fragments(result: SandboxResult, limits: SandboxLimits, state: _FileState) -> dict:
    """The manifest sections that come from a SandboxResult (structure,
    hazards, resource behavior, verify summary, provenance)."""
    start = result.start or {}
    ready = result.ready or {}
    doc = result.document or {}
    if doc.get("event") == protocol.EVENT_REJECT:
        structure: dict = {
            "reject": {"code": doc.get("code"), "detail": _display(doc.get("detail"), 1000)}
        }
    else:
        structure = {
            key: doc.get(key)
            for key in (
                "page_count",
                "pdf_version",
                "owner_restricted",
                "security_handler_revision",
                "form_type",
                "metadata",
            )
        }
    failed_pages = [
        {"index": p.index, "code": p.code}
        for p in result.pages
        if p.status == protocol.PAGE_FAILED
    ]
    return {
        "structure": structure,
        "hazards": dict(result.hazards),
        "resources": {
            # The runner and the parent can both hit the same bound on one file
            # (a file whose pages outrun the per-file text budget trips
            # BOUND_TEXT_BYTES in _validate_pages AND again in _text_json), so
            # the manifest lists each bound once, in the order first seen.
            "bounds_hit": list(dict.fromkeys(list(result.bounds_hit) + list(state.bounds))),
            "restarts": result.restarts,
            "spawns": result.spawns,
            "elapsed_ms": result.elapsed_ms,
            "peak_rss_kb": result.peak_rss_kb,
            "limits_applied": start.get("limits_applied"),
            "end": result.end,
        },
        "verify": {
            "pages_ok": sum(1 for p in result.pages if p.status == protocol.PAGE_OK),
            "pages_failed": len(failed_pages),
            "pages_failed_verify": sum(
                1 for p in failed_pages if p["code"] == protocol.PAGE_FAIL_VERIFY
            ),
            "failed_pages": failed_pages[:_LIST_MAX],
        },
        "provenance": {
            "sandbox_version": _child_sandbox_version(start.get("sandbox_version")),
            "protocol_version": _child_protocol_version(start.get("protocol_version")),
            "versions": ready.get("versions"),
            "pdfium_flags": ready.get("pdfium_flags"),
            "platform": ready.get("platform"),
            "limits": limits.to_dict(),
            "limits_hash": limits.limits_hash(),
            "uid": result.uid,
            "gid": result.gid,
            "slot": result.slot,
            "uid_switch_applied": result.uid is not None,
            "started_at": state.started_at,
            "elapsed_ms": result.elapsed_ms,
            "restarts": result.restarts,
            "peak_rss_kb": result.peak_rss_kb,
            "stderr_tail": result.stderr_tail,
        },
    }


class _PageSink:
    """The `page_sink` `run_sandbox` delivers its validated pages through.

    The runner re-opens and re-hashes each ok page after the `--verify` spawn
    and hands it here, in index order, exactly once, keeping no reference to
    the bytes afterwards; the parent therefore never holds more than one
    upload batch of images at a time (a 3000 page drawing set is ~500 MB of
    JPEG, which is why nothing accumulates).

    A batch is bounded by BOTH `_UPLOAD_BATCH_PAGES` and `_UPLOAD_BATCH_BYTES`,
    so a child that pads every artifact to the protocol's per-artifact caps
    cannot park more than one byte-bounded batch in this process: the page
    count is what an honest file trips, the byte budget is what a hostile one
    trips. A single artifact larger than the whole budget still ships on its
    own (one page resident is already the runner's own invariant).

    Each page is appended to the thumb-tier images PDF and queued for upload.
    Every `_UPLOAD_BATCH_PAGES` pages (or sooner, on the byte budget) the
    queue is flushed to the derived bucket, and each flush first refreshes
    the file's live progress fields
    (which also detects a lost claim), consults `should_abort()` and renews
    the queue lease: the upload phase of a large file is minutes long, so
    without that a cancel would keep uploading and the lease could expire
    under a second worker. An images-PDF builder failure is a bound, never a
    verdict; an upload failure propagates and becomes failed/storage.
    """

    def __init__(self, ctx: _RunContext, state: _FileState, scratch: Path, should_abort) -> None:
        self._ctx = ctx
        self._state = state
        self._should_abort = should_abort
        self._writer = ImagesPdfWriter(
            scratch / _IMAGES_DIR, part_bytes=ctx.s.rfp_ingest_images_pdf_part_bytes
        )
        self._items: list[tuple[str, bytes, str]] = []
        self._queued = 0
        self._queued_bytes = 0
        self._closed = False
        self.images_ok = True
        self.pages_delivered = 0
        self.objects_uploaded = 0

    def __call__(self, page, thumb: bytes, full: bytes) -> None:
        run_id, file_id = self._ctx.run_id, self._state.file_id
        if self.pages_delivered == 0:
            _set_phase(self._ctx, self._state, protocol.PHASE_UPLOAD)
        if self.images_ok:
            try:
                self._writer.add_page(
                    thumb,
                    width_px=int(page.thumb_meta["w"]),
                    height_px=int(page.thumb_meta["h"]),
                    width_pt=float(page.width_pt),
                    height_pt=float(page.height_pt),
                )
            except Exception:  # noqa: BLE001 - recorded as a bound, never a verdict
                logger.exception("rfp ingest: images PDF build failed")
                self.images_ok = False
        self._items.append((rs.thumb_path(run_id, file_id, page.index), thumb, "image/jpeg"))
        self._items.append((rs.full_path(run_id, file_id, page.index), full, "image/jpeg"))
        self.pages_delivered += 1
        self._queued += 1
        self._queued_bytes += len(thumb) + len(full)
        # Either bound triggers, and both are checked AFTER the append so a
        # page bigger than the whole budget is uploaded on its own instead of
        # being stranded in the queue.
        if self._queued >= _UPLOAD_BATCH_PAGES or self._queued_bytes >= _UPLOAD_BATCH_BYTES:
            self.flush()

    def flush(self) -> None:
        """Upload the queued batch after re-checking the claim, the abort flag
        and the lease. Raises `_SinkAborted` (a cancel, a shutdown or a lost
        claim) or `LeaseLost`, both before anything is written."""
        if not self._items:
            return
        _set_phase(self._ctx, self._state, protocol.PHASE_UPLOAD)
        if self._should_abort():
            raise _SinkAborted()
        if not llm_queue.renew_lease():
            raise runner.LeaseLost("queue lease lost while uploading page images")
        items, self._items, self._queued, self._queued_bytes = self._items, [], 0, 0
        rs.upload_many(rs.DERIVED_BUCKET, items)
        self.objects_uploaded += len(items)

    def close(self) -> tuple[list[Path], int, bool]:
        """Flush the tail and finalize the images PDF: (parts, bytes, built).
        Idempotent, so the caller can close it again in its finally."""
        if self._closed:
            return (list(self._writer.parts), int(self._writer.bytes_written), self.images_ok)
        self.flush()
        self._closed = True
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001 - recorded as a bound, never a verdict
            logger.exception("rfp ingest: images PDF could not be finalized")
            self.images_ok = False
        if not self.images_ok:
            return [], 0, False
        return list(self._writer.parts), int(self._writer.bytes_written), True

    def discard(self) -> None:
        """Drop the queued batch and close the writer without uploading
        anything (the file is being abandoned); never raises."""
        self._items = []
        self._queued = 0
        self._queued_bytes = 0
        self._closed = True
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001 - the whole scratch tree is about to go
            logger.debug("rfp ingest: images writer close failed on an abandoned file")


def _text_json(s: Settings, pages: list) -> tuple[bytes, bool, int]:
    """text.json `{pages: [{index, chars, truncated, hazards, text}]}` under
    rfp_ingest_max_text_bytes_per_file: the page that would cross the cap
    keeps as much text as fits and every later page carries none; both are
    flagged truncated. Returns (bytes, file_truncated, page_count_with_text)."""
    cap = int(s.rfp_ingest_max_text_bytes_per_file)
    head = b'{"pages":['
    tail = b"]}"
    budget = cap - len(head) - len(tail)
    chunks: list[bytes] = []
    used = 0
    file_truncated = False
    with_text = 0
    for page in sorted(pages, key=lambda p: p.index):
        ok = page.status == protocol.PAGE_OK
        entry = {
            "index": page.index,
            "chars": int(page.text_chars) if ok else 0,
            "truncated": bool(page.text_truncated) if ok else False,
            "hazards": dict(page.text_hazards or {}) if ok else {},
            "text": (page.text or "") if ok else None,
            "status": page.status,
        }
        sep = 1 if chunks else 0
        encoded = json.dumps(entry, ensure_ascii=False).encode("utf-8")
        if used + sep + len(encoded) > budget:
            file_truncated = True
            entry["truncated"] = True
            text = entry["text"] or ""
            entry["text"] = "" if ok else None
            bare = json.dumps(entry, ensure_ascii=False).encode("utf-8")
            room = budget - used - sep - len(bare)
            if ok and room > 0 and text:
                cut = text.encode("utf-8")[:room].decode("utf-8", errors="ignore")
                entry["text"] = cut
                encoded = json.dumps(entry, ensure_ascii=False).encode("utf-8")
                if used + sep + len(encoded) > budget:
                    entry["text"] = ""
                    encoded = bare
            else:
                encoded = bare
            if used + sep + len(encoded) > budget:
                break
        if ok and entry["text"]:
            with_text += 1
        chunks.append(encoded)
        used += sep + len(encoded)
    return head + b",".join(chunks) + tail, file_truncated, with_text


def _page_row(run_id: str, file_id: str, page) -> dict:
    row: dict = {
        "file_id": file_id,
        "page_index": page.index,
        "status": page.status,
        "code": page.code,
        "detail": _display(page.detail, protocol.DETAIL_MAX_CHARS) or None,
        "width_pt": page.width_pt,
        "height_pt": page.height_pt,
        "rotation": page.rotation,
        "tier": page.tier,
        "thumb_path": None,
        "full_path": None,
        "thumb_w": None,
        "thumb_h": None,
        "full_w": None,
        "full_h": None,
        "text_chars": int(page.text_chars or 0),
        "text_truncated": bool(page.text_truncated),
        "text_hazards": dict(page.text_hazards or {}),
        "render_ms": page.render_ms,
    }
    if page.status == protocol.PAGE_OK:
        row.update(
            thumb_path=rs.thumb_path(run_id, file_id, page.index),
            full_path=rs.full_path(run_id, file_id, page.index),
            thumb_w=int(page.thumb_meta["w"]),
            thumb_h=int(page.thumb_meta["h"]),
            full_w=int(page.full_meta["w"]),
            full_h=int(page.full_meta["h"]),
        )
    return row


def _replace_pages(ctx: _RunContext, state: _FileState, pages: list) -> None:
    """Replace the file's `rfp_ingest_pages` rows with this attempt's, fenced
    on the claim we are about to CAS the file row with.

    The rows and the file row's page_count / pages_ok / pages_failed have to
    come from the SAME attempt: an upsert that ignored duplicates let a stale
    attempt's rows outlive the winner's (the winner's own rows were silently
    dropped), and an upsert that did not would let a stale attempt clobber the
    winner instead. Deleting first and inserting under a fresh claim check
    leaves only the milliseconds between that check and the terminal CAS, and
    a dispossessed attempt stops uploading at its next batch anyway. Closing
    that last gap needs one statement (a claim-checked SECURITY DEFINER
    function) that PostgREST cannot express as two calls.
    """
    if not _claim_intact(ctx, state):
        raise _ClaimLost()
    run_id, file_id = ctx.run_id, state.file_id
    rows = [_page_row(run_id, file_id, p) for p in sorted(pages, key=lambda p: p.index)]
    ctx.sb.table(PAGES_TABLE).delete().eq("file_id", file_id).execute()
    for start in range(0, len(rows), _PAGES_UPSERT_BATCH):
        ctx.sb.table(PAGES_TABLE).insert(rows[start : start + _PAGES_UPSERT_BATCH]).execute()


def _claim_intact(ctx: _RunContext, state: _FileState) -> bool:
    rows = (
        ctx.sb.table(FILES_TABLE)
        .select("id, status, claim_token")
        .eq("id", state.file_id)
        .limit(1)
        .execute()
    ).data or []
    return bool(rows) and rows[0].get("status") == protocol.STATUS_RUNNING and (
        rows[0].get("claim_token") == state.token
    )


def _complete_file(
    ctx: _RunContext, state: _FileState, result: SandboxResult, sink: _PageSink, manifest: dict
) -> None:
    """A complete sandbox result whose pages the sink has already uploaded:
    finalize the images PDF, re-check the claim, upload the tail (text.json,
    manifest.json, the PDF parts), replace the pages rows and CAS the file
    terminal (verified / verified_with_gaps). Upload failures propagate to
    the guard as failed/storage."""
    s = ctx.s
    run_id, file_id = ctx.run_id, state.file_id
    ok_pages = sorted(
        (p for p in result.pages if p.status == protocol.PAGE_OK), key=lambda p: p.index
    )
    parts, images_bytes, images_ok = sink.close()
    if not images_ok:
        state.bounds.append(protocol.BOUND_IMAGES_PDF)
    if sink.pages_delivered != len(ok_pages):
        # The runner delivers every ok page of a complete result; a short
        # delivery would leave page rows pointing at objects that are not
        # there, so the file gets no verdict from this attempt.
        logger.error(
            "rfp ingest: file %s delivered %d of %d pages",
            file_id, sink.pages_delivered, len(ok_pages),
        )
        _discard_derived(ctx, state, sink)
        raise _FileVerdict(protocol.STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT)
    if not _claim_intact(ctx, state):
        raise _ClaimLost()
    if not llm_queue.renew_lease():
        raise runner.LeaseLost("queue lease lost before the derived tail was written")
    _set_phase(ctx, state, protocol.PHASE_UPLOAD)
    text_bytes, text_truncated, pages_with_text = _text_json(s, result.pages)
    if text_truncated:
        state.bounds.append(protocol.BOUND_TEXT_BYTES)
    text_key = rs.text_path(run_id, file_id)
    rs.upload_bytes(rs.DERIVED_BUCKET, text_key, text_bytes, "application/json")

    pages_ok = len(ok_pages)
    pages_failed = len(result.pages) - pages_ok
    page_count = len(result.pages)
    manifest.update(_sandbox_fragments(result, ctx.limits, state))
    part_keys = [rs.images_pdf_path(run_id, file_id, i) for i in range(1, len(parts) + 1)]
    manifest["output"] = {
        "page_count": page_count,
        "pages_ok": pages_ok,
        "pages_failed": pages_failed,
        "text_bytes": len(text_bytes),
        "text_truncated": text_truncated,
        "pages_with_text": pages_with_text,
        "images_pdf": {"parts": len(parts), "bytes": images_bytes, "built": images_ok,
                       "paths": part_keys},
        "derived_prefix": rs.derived_prefix(run_id, file_id),
        "text_path": text_key,
    }
    status = (
        protocol.STATUS_VERIFIED if pages_failed == 0 else protocol.STATUS_VERIFIED_WITH_GAPS
    )
    finished = _now_iso()
    manifest["verdict"] = {"status": status, "code": None, "detail": None}
    manifest["provenance"]["finished_at"] = finished
    rs.upload_bytes(
        rs.DERIVED_BUCKET,
        rs.manifest_path(run_id, file_id),
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        "application/json",
    )
    for key, part in zip(part_keys, parts, strict=True):
        rs.upload_file(rs.DERIVED_BUCKET, key, part, "application/pdf")
    _replace_pages(ctx, state, result.pages)
    fields = {
        "status": status,
        "reject_code": None,
        "error": None,
        "page_count": page_count,
        "pages_ok": pages_ok,
        "pages_failed": pages_failed,
        "pages_done": page_count,
        "hazards": dict(result.hazards),
        "manifest": manifest,
        "derived_prefix": rs.derived_prefix(run_id, file_id),
        "images_pdf_paths": part_keys,
        "text_path": text_key,
        "restarts": result.restarts,
        "elapsed_ms": result.elapsed_ms,
        "sandbox_version": manifest["provenance"]["sandbox_version"],
        "protocol_version": manifest["provenance"]["protocol_version"],
        "limits_hash": ctx.limits.limits_hash(),
        "phase": None,
        "child_pid": None,
        "finished_at": finished,
    }
    if not _update_claimed(ctx, state, fields):
        raise _ClaimLost()
    ctx.consumed += page_count


def _finish_file(
    ctx: _RunContext, state: _FileState, status: str, code: str, manifest: dict
) -> bool:
    """CAS the file to a rejected/failed verdict, fenced on the claim."""
    detail = protocol.VERDICT_MESSAGES.get(code) or protocol.VERDICT_MESSAGES[
        protocol.FAIL_INTERRUPTED
    ]
    finished = _now_iso()
    manifest["verdict"] = {"status": status, "code": code, "detail": detail}
    manifest.setdefault("provenance", {})["finished_at"] = finished
    resources = manifest.get("resources") or {}
    fields = {
        "status": status,
        "reject_code": code,
        "error": detail,
        "manifest": manifest,
        "hazards": manifest.get("hazards"),
        "restarts": int(resources.get("restarts") or 0),
        "elapsed_ms": resources.get("elapsed_ms"),
        "page_count": (manifest.get("structure") or {}).get("page_count"),
        "sandbox_version": (manifest.get("provenance") or {}).get("sandbox_version"),
        "protocol_version": (manifest.get("provenance") or {}).get("protocol_version"),
        "limits_hash": ctx.limits.limits_hash(),
        "phase": None,
        "child_pid": None,
        "finished_at": finished,
    }
    won = _update_claimed(ctx, state, fields)
    if not won:
        logger.warning("rfp ingest: verdict %s/%s for file %s lost its claim",
                       status, code, state.file_id)
    return won


def _release_in_flight(ctx: _RunContext, state: _FileState) -> None:
    """Cancel while the child ran: the file goes back to pending (fenced on
    the claim) and whatever it produced is removed."""
    won = _update_claimed(
        ctx,
        state,
        {
            "status": protocol.STATUS_PENDING,
            "claim_token": None,
            "reject_code": protocol.FAIL_INTERRUPTED,
            "error": protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED],
            "phase": None,
            "child_pid": None,
            "pages_done": 0,
            "finished_at": None,
        },
    )
    if not won:
        # _mark(canceled) usually beat us to the reset (it hands every running
        # file back); the outputs are still ours to remove as long as nobody
        # has re-claimed the file since.
        rows = (
            ctx.sb.table(FILES_TABLE)
            .select("id, status, claim_token")
            .eq("id", state.file_id)
            .limit(1)
            .execute()
        ).data or []
        won = bool(rows) and rows[0].get("status") == protocol.STATUS_PENDING and (
            rows[0].get("claim_token") is None
        )
    if won:
        _delete_file_outputs(ctx.run_id, state.file_id)


def _merge_extra(manifest: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(manifest.get(key), dict):
            manifest[key].update(value)
        else:
            manifest[key] = value


def _discard_derived(ctx: _RunContext, state: _FileState, sink: _PageSink | None) -> None:
    """Remove the objects a page delivery already uploaded for a file that is
    not going to be complete. Best-effort: the same deterministic keys are
    rewritten by the next attempt, and the prune sweeps the rest."""
    if sink is None or not sink.objects_uploaded:
        return
    try:
        rs.delete_prefix(
            rs.DERIVED_BUCKET, rs.derived_prefix(ctx.run_id, state.file_id),
            keep=(rs.CONVERTED_OBJECT,),
        )
    except Exception:  # noqa: BLE001 - a listing hiccup is not a file failure
        logger.exception("rfp ingest: partial derived cleanup failed for file %s", state.file_id)


def _process_file(ctx: _RunContext, frow: dict) -> str | None:
    """One file, start to finish. Returns None (next file), "lost" (the
    claim went away; re-check the run), "shutdown", "cancel", "stop" (the
    run is no longer running) or "spawn" (abort the run)."""
    token = _claim_file(ctx, frow)
    if token is None:
        return "lost"
    state = _FileState(file_id=frow["id"], token=token, started_at=_now_iso())
    ctx.state = state
    manifest: dict = {
        "identity": _identity(
            _display(frow.get("filename")), frow.get("declared_mime"),
            {"kind": ctx.run.get("source_kind")},
        )
    }
    ctx.manifest = manifest
    should_abort, on_progress = _make_callbacks(ctx, state)
    scratch = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=ctx.scratch_root))
    sink: _PageSink | None = None
    try:
        try:
            _check_disk(ctx, frow)
            dest = scratch / "source.pdf"
            manifest["identity"]["source"] = _materialize(ctx, frow, dest)
            sniff = rfp_sanitize.sniff_file(
                dest, filename=_sniff_name(ctx.s, _display(frow.get("filename")))
            )
            manifest["sniff"] = _sniff_fragment(sniff)
            if sniff.verdict:
                raise _FileVerdict(protocol.STATUS_REJECTED, sniff.verdict)
            source_format = sniff.source_format or protocol.SOURCE_FORMAT_PDF
            manifest["identity"]["source_format"] = source_format
            # An office file keeps its raw bytes under their own name; the
            # child's `source.pdf` is what the converter writes (2.1).
            raw = dest
            if source_format != protocol.SOURCE_FORMAT_PDF:
                raw = scratch / f"source.{source_format}"
                os.rename(dest, raw)
            sha, size = rfp_sanitize.sha256_file(raw)
            manifest["identity"]["sha256"] = sha
            manifest["identity"]["size_bytes"] = size
            _record_identity(ctx, state, sha, size, source_format)
            if ctx.run.get("source_kind") == "email":
                qpath = rs.quarantine_path(ctx.run_id, state.file_id, source_format)
                rs.upload_file(
                    rs.QUARANTINE_BUCKET, qpath, raw, rs.source_content_type(source_format)
                )
                manifest["identity"]["quarantine_path"] = qpath
                if not _update_claimed(ctx, state, {"quarantine_path": qpath}):
                    raise _ClaimLost()
            if ctx.s.rfp_ingest_max_pages_per_run - ctx.consumed <= 0:
                state.bounds.append(protocol.BOUND_RUN_PAGE_BUDGET)
                raise _FileVerdict(
                    protocol.STATUS_REJECTED,
                    protocol.REJECT_RUN_PAGE_BUDGET,
                    extra={"resources": {"bounds_hit": list(state.bounds)}},
                )
            if source_format != protocol.SOURCE_FORMAT_PDF:
                manifest["conversion"] = _convert_office(
                    ctx, state, frow, raw, dest, source_format
                )
            sink = _PageSink(ctx, state, scratch, should_abort)
            result = _run_file_sandbox(ctx, state, dest, should_abort, on_progress, sink)
            if (
                result.status == runner.STATUS_FAILED
                and result.code == protocol.FAIL_INTERRUPTED
                and state.abort
            ):
                if state.abort == "shutdown" or SHUTTING_DOWN.is_set():
                    return "shutdown"
                if state.abort == "cancel":
                    _release_in_flight(ctx, state)
                    return "cancel"
                return "lost"
            manifest.update(_sandbox_fragments(result, ctx.limits, state))
            if result.status != runner.STATUS_COMPLETE:
                # The runner can still refuse a file after it has delivered
                # pages (a re-read whose sha256 no longer matches), so any
                # object this attempt uploaded goes before the verdict.
                _discard_derived(ctx, state, sink)
                raise _FileVerdict(result.status, result.code or protocol.FAIL_INVALID_OUTPUT)
            _complete_file(ctx, state, result, sink, manifest)
            return None
        except _FileVerdict as verdict:
            _merge_extra(manifest, verdict.extra)
            _finish_file(ctx, state, verdict.status, verdict.code, manifest)
            return None
        except _ClaimLost:
            logger.warning("rfp ingest: file %s lost its claim; abandoning", state.file_id)
            return "lost"
        except _SinkAborted:
            # A cancel, a shutdown or a lost claim between two upload batches.
            if state.abort == "shutdown" or SHUTTING_DOWN.is_set():
                return "shutdown"
            if state.abort == "cancel":
                _release_in_flight(ctx, state)
                return "cancel"
            return "lost"
        except runner.SandboxSpawnError as exc:
            if state.abort:
                if state.abort == "shutdown":
                    return "shutdown"
                if state.abort == "cancel":
                    _release_in_flight(ctx, state)
                    return "cancel"
                return "lost"
            logger.error("rfp ingest: sandbox spawn failed for file %s: %s", state.file_id, exc)
            _finish_file(ctx, state, protocol.STATUS_FAILED, protocol.FAIL_SPAWN, manifest)
            return "spawn"
    finally:
        if sink is not None:
            sink.discard()
        runner.cleanup_scratch(scratch)


def _guarded_process_file(ctx: _RunContext, frow: dict) -> str | None:
    """`_process_file` with the spec's guarantee: no per-file exception
    escapes. Anything unexpected becomes failed/<code> (storage for transfer
    and disk errors, interrupted otherwise) with the full exception in the
    log only. LeaseLost passes through; a failure to record the verdict
    itself is a transient fault for the queue. A uid slot that never frees
    is capacity, not a bad file, so the queue requeues the whole run rather
    than burning this file on a condition the next attempt may not hit."""
    ctx.state = None
    ctx.manifest = None
    try:
        return _process_file(ctx, frow)
    except runner.LeaseLost:
        raise
    except runner.SlotWaitTimeout as exc:
        logger.warning("rfp ingest: file %s gave up waiting for a uid slot", frow["id"])
        raise RfpIngestTransient(_MSG_CAPACITY) from exc
    except Exception as exc:  # noqa: BLE001 - mapped to failed/<code>
        state, manifest = ctx.state, ctx.manifest or {}
        logger.exception("rfp ingest: file %s failed unexpectedly", frow["id"])
        if state is None:
            raise RfpIngestTransient(_MSG_RECORD_FAILED) from exc
        transfer = (rs.RfpStorageError, httpx.HTTPError, OSError)
        code = protocol.FAIL_STORAGE if isinstance(exc, transfer) else protocol.FAIL_INTERRUPTED
        try:
            _finish_file(ctx, state, protocol.STATUS_FAILED, code, manifest)
        except Exception as exc2:  # noqa: BLE001 - the queue retries the run
            raise RfpIngestTransient(_MSG_RECORD_FAILED) from exc2
        return None
    finally:
        ctx.state = None
        ctx.manifest = None


# ── execute: the run ─────────────────────────────────────────────────────


def _reset_stale_running_files(ctx: _RunContext) -> None:
    """Files left `running` by an earlier attempt (a crashed worker, a
    shutdown requeue) go back to pending first, fenced on their old claim,
    and only then lose their pages rows and derived prefix."""
    for frow in _run_files(ctx.sb, ctx.run_id, "id, status, claim_token"):
        if frow.get("status") != protocol.STATUS_RUNNING:
            continue
        query = (
            ctx.sb.table(FILES_TABLE)
            .update(
                {
                    "status": protocol.STATUS_PENDING,
                    "claim_token": None,
                    "reject_code": None,
                    "error": None,
                    "phase": None,
                    "child_pid": None,
                    "pages_done": 0,
                }
            )
            .eq("id", frow["id"])
            .eq("status", protocol.STATUS_RUNNING)
        )
        token = frow.get("claim_token")
        query = query.eq("claim_token", token) if token else query.is_("claim_token", "null")
        if (query.execute()).data:
            _delete_file_outputs(ctx.run_id, frow["id"])


def _next_pending(ctx: _RunContext) -> dict | None:
    rows = (
        ctx.sb.table(FILES_TABLE)
        .select(_FILE_COLUMNS)
        .eq("run_id", ctx.run_id)
        .eq("status", protocol.STATUS_PENDING)
        .order("created_at")
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _file_loop(ctx: _RunContext) -> str | None:
    """Process pending files in created_at order until none remain or a stop
    is called for. Returns None (all processed), "shutdown", "stop" (the run
    is no longer running: canceled, or failed by the queue) or "spawn"."""
    seen: set[str] = set()
    while True:
        if SHUTTING_DOWN.is_set():
            return "shutdown"
        if not llm_queue.renew_lease():
            raise runner.LeaseLost("queue lease lost between files")
        if _run_status(ctx.sb, ctx.run_id) != protocol.RUN_RUNNING:
            return "stop"
        frow = _next_pending(ctx)
        if frow is None:
            return None
        if frow["id"] in seen:
            # A file that came back pending after we finished with it belongs
            # to whoever reset it; never spin on it.
            logger.warning("rfp ingest: file %s is pending again; leaving it", frow["id"])
            return None
        seen.add(frow["id"])
        outcome = _guarded_process_file(ctx, frow)
        if outcome in ("shutdown", "stop", "cancel", "spawn"):
            return "stop" if outcome == "cancel" else outcome


def _orphan_leftovers(ctx: _RunContext) -> None:
    """Spec 3.6 step 3: pending files that survived the loop with no other
    active job for the run are failed/orphaned (a requeue race hands them to
    the other job instead)."""
    leftovers = [
        f for f in _run_files(ctx.sb, ctx.run_id, "id, status")
        if f.get("status") == protocol.STATUS_PENDING
    ]
    if not leftovers:
        return
    job = llm_queue.current_job.get()
    active = llm_queue.active_job(llm_queue.JOB_RFP_INGEST, ctx.run_id)
    ours = active is None or (job is not None and active.get("id") == job.get("id"))
    if not ours:
        logger.warning(
            "rfp ingest: run %s has %d pending files owned by another job", ctx.run_id,
            len(leftovers),
        )
        return
    for frow in leftovers:
        ctx.sb.table(FILES_TABLE).update(
            {
                "status": protocol.STATUS_FAILED,
                "reject_code": protocol.FAIL_ORPHANED,
                "error": protocol.VERDICT_MESSAGES[protocol.FAIL_ORPHANED],
                "finished_at": _now_iso(),
            }
        ).eq("id", frow["id"]).eq("status", protocol.STATUS_PENDING).execute()


def _derive_run_status(ctx: _RunContext) -> None:
    """done when every file is terminal and none failed (all-rejected is
    still done); failed when every file failed; otherwise done_with_errors.
    Counters are written with the same CAS. Files still pending or running
    belong to another job: the run is left running for it."""
    files = _run_files(ctx.sb, ctx.run_id, "id, status, page_count")
    if any(f.get("status") not in protocol.FILE_TERMINAL_STATUSES for f in files):
        logger.warning("rfp ingest: run %s still has active files; not deriving", ctx.run_id)
        return
    counters = _counters(files)
    if files and counters["files_failed"] == len(files):
        status, error = protocol.RUN_FAILED, _MSG_RUN_ALL_FAILED
    elif counters["files_failed"] == 0:
        status, error = protocol.RUN_DONE, None
    else:
        status, error = protocol.RUN_DONE_WITH_ERRORS, None
    _mark(ctx.run_id, status=status, error=error, **counters)


def _execute(run_id: str) -> None:
    s = get_settings()
    sb = get_supabase()
    if not _acquire_capacity(s):
        raise RfpIngestTransient(_MSG_CAPACITY)
    try:
        verdict = _self_test_gate()
        if verdict is not None and not verdict.get("ok"):
            detail = str(verdict.get("detail") or _MSG_SELF_TEST_ERROR)[:_ERROR_MAX_CHARS]
            if verdict.get("ok") is None:
                # Contention for the self-test uid slot, not a broken sandbox:
                # the queue retries the run on the next tick or worker.
                logger.warning("rfp ingest: self-test slot busy; retrying run %s", run_id)
                raise RfpIngestTransient(detail)
            logger.error("rfp ingest: refusing run %s, last self-test failed", run_id)
            _mark(run_id, status=protocol.RUN_FAILED, error=detail)
            return
        limits = SandboxLimits.from_settings(s)
        try:
            scratch_root = _scratch_root(s)
        except RfpIngestTransient:
            raise
        run = _mark(
            run_id,
            status=protocol.RUN_RUNNING,
            error=None,
            limits=limits.to_dict(),
            limits_hash=limits.limits_hash(),
            sandbox_version=protocol.SANDBOX_VERSION,
            protocol_version=protocol.PROTOCOL_VERSION,
            started_at=_now_iso(),
        )
        if run is None:
            logger.info("rfp ingest: run %s is not pending/running; nothing to do", run_id)
            return
        ctx = _RunContext(s=s, sb=sb, run=run, limits=limits, scratch_root=scratch_root)
        _reset_stale_running_files(ctx)
        ctx.consumed = _counters(_run_files(sb, run_id, "id, status, page_count"))[
            "pages_total"
        ]
        stop = _file_loop(ctx)
        if stop == "shutdown":
            logger.warning("rfp ingest: run %s interrupted by shutdown; requeuing", run_id)
            try:
                llm_queue.requeue_self()
            except Exception:  # noqa: BLE001 - the sweep requeues it when the lease expires
                logger.exception("rfp ingest: requeue_self failed for run %s", run_id)
            return
        if stop == "stop":
            logger.info("rfp ingest: run %s is no longer running; stopping", run_id)
            return
        if stop == "spawn":
            counters = _counters(_run_files(sb, run_id, "id, status, page_count"))
            _mark(run_id, status=protocol.RUN_FAILED, error=_MSG_RUN_SPAWN, **counters)
            return
        _orphan_leftovers(ctx)
        _derive_run_status(ctx)
    finally:
        _release_capacity()


def execute(run_id: str) -> None:
    """The queue's run (spec 3.6). Never raises for a per-file problem;
    raises RfpIngestTransient for an infrastructure fault so the queue
    retries. A lost queue lease is caught here: the child is already dead
    and nothing may be written for this attempt."""
    try:
        _execute(run_id)
    except runner.LeaseLost:
        logger.warning("rfp ingest: run %s lost its queue lease; no writes", run_id)


# ── Cancel / retry / delete ──────────────────────────────────────────────


def cancel_run(run_id: str) -> dict:
    """Spec 3.6 cancel. The run is CASed to `canceled` (sticky) FIRST, then
    the queued job is canceled: llm_queue.cancel marks the domain row failed,
    and canceled must win that race. A running runner sees should_abort(),
    kills its child, hands the in-flight file back and returns normally.
    Raises RfpIngestPermanent 409 (http_status) when the run is not
    cancelable, 404 when it is gone."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") not in _RUN_CANCELABLE_FROM:
        raise RfpIngestPermanent("The run can no longer be canceled.", http_status=409)
    row = _mark(run_id, status=protocol.RUN_CANCELED, error=_MSG_RUN_CANCELED)
    if row is None:
        raise RfpIngestPermanent("The run can no longer be canceled.", http_status=409)
    try:
        job = llm_queue.active_job(llm_queue.JOB_RFP_INGEST, run_id)
        if job is not None:
            llm_queue.cancel(job["id"])
    except Exception:  # noqa: BLE001 - the running job sees the canceled row itself
        logger.exception("rfp ingest: queue cancel failed for run %s", run_id)
    return row


def retry_run(run_id: str, *, created_by: str | None, background: Any | None) -> dict:
    """Spec 3.6 retry: allowed on failed | done_with_errors | canceled with
    no active job. The run is CASed to pending FIRST (409 when it moved
    meanwhile); only then do its files in failed/pending/running go back to
    pending (claim, verdict and error cleared, pages rows and derived prefix
    removed; rejected files are never revisited), and the job is enqueued
    with raise_on_active (409 when one appeared meanwhile, the run put back
    with its previous status, completed_at and error). Raises
    RfpIngestPermanent 409 (http_status) on a wrong status or an active
    job."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    previous = run.get("status")
    if previous not in _RUN_RETRYABLE:
        raise RfpIngestPermanent(
            "Only failed, canceled or partially failed runs can be retried.", http_status=409
        )
    active = llm_queue.active_job(llm_queue.JOB_RFP_INGEST, run_id)
    if active is not None:
        raise RfpIngestPermanent(
            f"A job is already {active.get('status') or 'active'} for this run.",
            http_status=409,
        )
    rows = (
        sb.table(RUNS_TABLE)
        .update({"status": protocol.RUN_PENDING, "error": None, "completed_at": None})
        .eq("id", run_id)
        .eq("status", previous)
        .execute()
    ).data or []
    if not rows:
        raise RfpIngestPermanent("The run changed while retrying; reload it.", http_status=409)
    # Only now, with the run ours, are the files reset: a losing CAS (a cancel
    # that landed first) must not clear a claim or delete a derived prefix a
    # live runner still owns.
    for frow in _run_files(sb, run_id, "id, status"):
        if frow.get("status") not in _FILE_RETRY_RESET:
            continue
        reset = (
            sb.table(FILES_TABLE)
            .update(
                {
                    "status": protocol.STATUS_PENDING,
                    "claim_token": None,
                    "reject_code": None,
                    "error": None,
                    "phase": None,
                    "pages_done": 0,
                    "child_pid": None,
                    "page_count": None,
                    "pages_ok": None,
                    "pages_failed": None,
                    "derived_prefix": None,
                    "images_pdf_paths": None,
                    "text_path": None,
                    "finished_at": None,
                }
            )
            .eq("id", frow["id"])
            .in_("status", _FILE_RETRY_RESET)
            .execute()
        ).data or []
        if reset:
            _delete_file_outputs(run_id, frow["id"])
    try:
        dispatch(run_id, created_by=created_by, background=background, raise_on_active=True)
    except llm_queue.JobAlreadyActive as exc:
        # Put the whole terminal shape back, not just the status: a run left
        # pending with completed_at NULL is terminal to every reader but can
        # never be reclaimed by the retention prune.
        sb.table(RUNS_TABLE).update(
            {
                "status": previous,
                "completed_at": run.get("completed_at"),
                "error": run.get("error"),
            }
        ).eq("id", run_id).eq("status", protocol.RUN_PENDING).execute()
        raise RfpIngestPermanent(
            "A job is already queued or running for this run.", http_status=409
        ) from exc
    return rows[0]


def delete_run(run_id: str) -> None:
    """Spec 3.6 delete: only staging | done | done_with_errors | failed |
    canceled | expired, and only once the queue has no active job for the run
    (409 otherwise). That job row IS the runner's acknowledgement the spec
    asks for: `llm_queue.cancel` refuses to cancel a running job with a live
    lease, so the row stays active for as long as a runner is alive and a
    dead worker's lease expires into the sweep on its own. Without the guard
    a DELETE issued a millisecond after a cancel raced the runner's upload
    phase and every object written afterwards was orphaned with no row and no
    prefix left to sweep.

    Both storage prefixes go BEFORE the rows: the rows are what the retention
    prune walks to learn which prefixes to delete, so losing them first turns
    any delete failure into a permanent orphan. A failure here leaves the run
    deletable and is reported as retryable."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") not in _RUN_DELETABLE:
        raise RfpIngestPermanent(
            "Cancel the run and wait for it to stop before deleting it.", http_status=409
        )
    active = llm_queue.active_job(llm_queue.JOB_RFP_INGEST, run_id)
    if active is not None:
        raise RfpIngestPermanent(
            "Cancel the run and wait for it to stop before deleting it.", http_status=409
        )
    for bucket in (rs.DERIVED_BUCKET, rs.QUARANTINE_BUCKET):
        try:
            rs.delete_prefix(bucket, run_id)
        except Exception as exc:  # noqa: BLE001 - the rows stay, so a retry can finish it
            logger.exception("rfp ingest: prefix delete failed for run %s in %s", run_id, bucket)
            raise RfpIngestTransient(
                "The run's stored files could not be removed; try the delete again."
            ) from exc
    rows = (
        sb.table(RUNS_TABLE)
        .delete()
        .eq("id", run_id)
        .in_("status", sorted(_RUN_DELETABLE))
        .execute()
    ).data
    if rows is not None and not rows:
        raise RfpIngestPermanent(
            "Cancel the run and wait for it to stop before deleting it.", http_status=409
        )


# ── Reads for the router ─────────────────────────────────────────────────


def get_run(run_id: str) -> dict | None:
    """The run, its files (manifest excluded) and the queue's poll info."""
    sb = get_supabase()
    run = _load_run(sb, run_id)
    if run is None:
        return None
    run["files"] = _run_files(sb, run_id)
    try:
        run["queue"] = llm_queue.poll_info(llm_queue.JOB_RFP_INGEST, run_id)
    except Exception:  # noqa: BLE001 - detail is optional, polling must not break
        logger.exception("rfp ingest: queue poll_info failed for run %s", run_id)
        run["queue"] = None
    return run


def list_runs(*, status: str | None, limit: int, offset: int) -> dict:
    """`{rows, total, offset, limit}`, newest first. An unknown status is a
    400 (http_status) rather than an empty page."""
    if status is not None and status not in protocol.RUN_STATUSES:
        raise RfpIngestPermanent("Unknown run status.", http_status=400)
    limit = max(1, min(int(limit), _LIST_MAX))
    offset = max(0, int(offset))
    query = get_supabase().table(RUNS_TABLE).select("*", count="exact")
    if status is not None:
        query = query.eq("status", status)
    resp = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    rows = resp.data or []
    total = resp.count if resp.count is not None else len(rows)
    return {"rows": rows, "total": total, "offset": offset, "limit": limit}


def get_file(file_id: str) -> dict | None:
    """The file row with its manifest and hazards. `claim_token` is the
    internal write fence every `_update_claimed` write is compared against and
    never leaves the process (it is not in `_FILE_COLUMNS` either)."""
    rows = (
        get_supabase().table(FILES_TABLE).select("*").eq("id", file_id).limit(1).execute()
    ).data or []
    if not rows:
        return None
    row = dict(rows[0])
    row.pop("claim_token", None)
    return row


def _signed(path: str | None, *, download: str | None = None) -> str | None:
    if not path:
        return None
    try:
        return rs.signed_url(rs.DERIVED_BUCKET, path, download=download)
    except Exception:  # noqa: BLE001 - a URL mint failure must not break the listing
        logger.exception("rfp ingest: signed URL failed for %s", path)
        return None


def list_pages(file_id: str, *, offset: int, limit: int) -> dict:
    """`{rows, total, offset, limit}` in page order; each ok row carries a
    server-minted `thumb_url` (the reading tier is fetched per page through
    page_urls)."""
    limit = max(1, min(int(limit), _LIST_MAX))
    offset = max(0, int(offset))
    resp = (
        get_supabase()
        .table(PAGES_TABLE)
        .select(_PAGE_COLUMNS, count="exact")
        .eq("file_id", file_id)
        .order("page_index")
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = resp.data or []
    for row in rows:
        row["thumb_url"] = _signed(row.get("thumb_path"))
    total = resp.count if resp.count is not None else len(rows)
    return {"rows": rows, "total": total, "offset": offset, "limit": limit}


def page_urls(page_id: str) -> dict | None:
    """`{full}`: the reading-tier signed URL (None for a failed page)."""
    rows = (
        get_supabase()
        .table(PAGES_TABLE)
        .select("id, status, full_path")
        .eq("id", page_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return None
    return {"full": _signed(rows[0].get("full_path"))}


def file_urls(file_id: str) -> dict | None:
    """`{images_pdf: [...], text, manifest}` as download URLs. `images_pdf`
    carries one entry per `images_pdf_paths` entry, in the same order, and
    `null` where the URL could not be minted, so the caller can keep its
    part numbering; dropping the failures would renumber every later part."""
    rows = (
        get_supabase()
        .table(FILES_TABLE)
        .select("id, filename, images_pdf_paths, text_path, derived_prefix")
        .eq("id", file_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return None
    frow = rows[0]
    name = str(frow.get("filename") or "file")
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    stem = (re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file")[:80]
    parts = frow.get("images_pdf_paths") or []
    images = [
        _signed(path, download=f"{stem}-images-{index:03d}.pdf")
        for index, path in enumerate(parts, start=1)
    ]
    manifest_path = (
        rs.manifest_path(*frow["derived_prefix"].split("/", 1))
        if frow.get("derived_prefix") and "/" in frow["derived_prefix"]
        else None
    )
    return {
        "images_pdf": images,
        "text": _signed(frow.get("text_path"), download=f"{stem}-text.json"),
        "manifest": _signed(manifest_path, download=f"{stem}-manifest.json"),
    }


# ── Status, self-test, retention, email picker ───────────────────────────


def _dist_version(name: str) -> str | None:
    """Installed distribution version without importing the package (the
    parent never loads pypdfium2 or Pillow)."""
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _uid_switch_expected(s: Settings) -> bool:
    return runner._geteuid() == 0 and int(s.rfp_ingest_sandbox_uid) != 0


def _self_test_slot(s: Settings, scratch_root: Path) -> tuple[Any, str | None]:
    """The dedicated self-test uid slot: index pool_size (uid pool_base +
    pool_size), reserved so a self-test never steals a run's slot. Returns
    (UidSlot or None, None) or (None, "busy") when another worker's
    self-test held it for longer than the wait."""
    if not _uid_switch_expected(s):
        return None, None
    slot_index = int(s.rfp_ingest_sandbox_uid_pool_size)
    uid = int(s.rfp_ingest_sandbox_uid_pool_base) + slot_index
    path = runner._slot_lock_path(scratch_root, slot_index)
    deadline = time.monotonic() + _SELF_TEST_SLOT_WAIT_SECONDS
    while True:
        fd = runner._try_lock(path, create=True)
        if fd is not None:
            return runner.UidSlot(uid=uid, gid=uid, slot=slot_index, _fd=fd), None
        if time.monotonic() >= deadline:
            return None, "busy"
        time.sleep(_SELF_TEST_SLOT_POLL_SECONDS)


def _self_test_gate() -> dict | None:
    """The verdict `execute()` gates on: None before the first self-test, the
    cached one while it stands, or a fresh run when a cached FAILURE has aged
    past `_SELF_TEST_VERDICT_TTL_SECONDS`.

    A cached failure used to pin a worker for its whole life (the only other
    callers are the lifespan boot and GET /status, which the load balancer may
    never route to that worker), so every run it claimed was refused
    terminally. Re-running a stale failed verdict here is what un-poisons it.
    An `ok` of None is "unknown", not "failed": see `self_test`.
    """
    last = _last_self_test
    if last is None or last.get("ok"):
        return last
    try:
        age = (_now() - datetime.fromisoformat(str(last.get("at")))).total_seconds()
    except (TypeError, ValueError):
        age = _SELF_TEST_VERDICT_TTL_SECONDS + 1.0
    if age <= _SELF_TEST_VERDICT_TTL_SECONDS:
        return last
    logger.warning("rfp ingest: the cached self-test failure is stale; re-running it")
    return self_test()


def self_test() -> dict:
    """Spawn the child on the embedded one-page PDF under the configured uid
    switch (a dedicated slot) and report `{ok, detail, at, elapsed_ms,
    uid_switch_applied, versions}`. `ok` is True (the sandbox works), False
    (it does not) or None (unknown: the dedicated self-test slot was busy, so
    nothing was proven either way). True and False are cached in a module
    global that `execute()` gates on through `_self_test_gate`; an unknown
    verdict is never cached, so contention between the workers' boots cannot
    make one of them refuse every run it claims. Called from the lifespan
    when the flag is on and by `status_report(self_test_fresh=True)`, which
    is only ever GET /status?self_test=1: a plain read serves the cached
    verdict instead."""
    global _last_self_test
    s = get_settings()
    started = time.monotonic()
    ok = False
    detail = _MSG_SELF_TEST_ERROR
    info: dict = {}
    work: Path | None = None
    slot = None
    try:
        scratch_root = _scratch_root(s)
        work = Path(tempfile.mkdtemp(prefix=_SELFTEST_PREFIX, dir=scratch_root))
        pdf = work / "selftest.pdf"
        _write_excl(pdf, runner.embedded_selftest_pdf())
        slot, why = _self_test_slot(s, scratch_root)
        if why == "busy":
            # Another worker's self-test held the dedicated slot. That says
            # nothing about this sandbox, so it is reported as unknown
            # (`ok` None) and NOT cached: caching it as a failure refused
            # every run this worker later claimed.
            logger.warning("rfp ingest: %s", _MSG_SELF_TEST_SLOT_BUSY)
            return {
                "ok": None,
                "detail": _MSG_SELF_TEST_SLOT_BUSY,
                "at": _now_iso(),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        else:
            limits = SandboxLimits.from_settings(s)
            result = runner.run_sandbox(
                pdf,
                limits=limits,
                scratch_root=scratch_root,
                open_timeout_seconds=min(
                    _SELF_TEST_OPEN_TIMEOUT_SECONDS, s.rfp_ingest_open_timeout_seconds
                ),
                page_stall_seconds=min(
                    _SELF_TEST_OPEN_TIMEOUT_SECONDS, s.rfp_ingest_page_stall_seconds
                ),
                file_timeout_seconds=_SELF_TEST_TIMEOUT_SECONDS,
                max_restarts=1,
                max_pages_remaining=1,
                uid_slot=slot,
                should_abort=SHUTTING_DOWN.is_set,
                on_progress=lambda phase, pages_done, pid: None,
                renew=lambda: True,
            )
            page = result.pages[0] if result.pages else None
            rendered = (
                result.status == runner.STATUS_COMPLETE
                and page is not None
                and page.status == protocol.PAGE_OK
                and "self-test" in (page.text or "")
            )
            info = {
                "status": result.status,
                "code": result.code,
                "uid_switch_applied": result.uid is not None,
                "uid": result.uid,
                "versions": (result.ready or {}).get("versions"),
                "pdfium_flags": (result.ready or {}).get("pdfium_flags"),
                "limits_applied": (result.start or {}).get("limits_applied"),
                "bounds_hit": list(result.bounds_hit),
                "stderr_tail": result.stderr_tail,
            }
            if rendered:
                ok, detail = True, _MSG_SELF_TEST_OK
            elif result.status == runner.STATUS_COMPLETE:
                detail = "The sandbox self-test rendered the page but its text was not found."
            else:
                detail = (
                    f"The sandbox self-test ended {result.status}/{result.code}: "
                    f"{result.detail or protocol.VERDICT_MESSAGES[protocol.FAIL_SPAWN]}"
                )
    except Exception:  # noqa: BLE001 - reported, never raised: status must always answer
        logger.exception("rfp ingest: self-test could not run")
        detail = _MSG_SELF_TEST_ERROR
    finally:
        if slot is not None:
            slot.release()
        if work is not None:
            runner.cleanup_scratch(work)
    if not ok:
        logger.error("rfp ingest: SELF-TEST FAILED: %s", detail)
    _last_self_test = {
        "ok": ok,
        "detail": detail,
        "at": _now_iso(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        **info,
    }
    return dict(_last_self_test)


def last_self_test() -> dict | None:
    """The cached last self-test result (None before the first run)."""
    return dict(_last_self_test) if _last_self_test is not None else None


def status_report(self_test_fresh: bool = True) -> dict:
    """GET /status: limits, versions, platform notes, free scratch disk, the
    self-test, slot occupancy and the per-process load.

    `self_test_fresh` decides what the `self_test` key costs. True spawns a
    REAL child on the embedded page (seconds of wall clock, a uid slot); False
    serves this worker's last verdict, or `_SELF_TEST_NOT_RUN` when none has
    run yet, so the whole report is one `disk_usage` call plus a slot scan.
    The route passes False for a plain read and True only for `?self_test=1`
    (the sandbox must never be spawned by a poll). `self_test_fresh` is
    echoed in the payload so a reader can tell the two apart.

    `_self_test_gate` is deliberately NOT used for the cheap path: it re-runs
    a stale FAILED verdict, which would put a child spawn back on a plain
    read. Un-poisoning a failed verdict stays the job of `execute()` and of
    an explicit `?self_test=1`."""
    s = get_settings()
    limits = SandboxLimits.from_settings(s)
    euid = runner._geteuid()
    switch = _uid_switch_expected(s)
    notes: list[str] = []
    if euid != 0:
        notes.append("The parent is not root: no uid switch happens and this is not a "
                     "security boundary (local development).")
    elif int(s.rfp_ingest_sandbox_uid) == 0:
        notes.append("RFP_INGEST_SANDBOX_UID=0: the uid switch is disabled even as root.")
    if platform.system() == "Darwin":
        notes.append("macOS: RLIMIT_AS is not applied; CPU, FSIZE, NOFILE, NPROC and CORE are.")
    if not s.rfp_ingest_scratch_dir:
        notes.append("RFP_INGEST_SCRATCH_DIR is empty: scratch lives under the system temp dir.")
    scratch: dict = {"root": None, "free_bytes": None, "total_bytes": None,
                     "reserve_bytes": s.rfp_ingest_scratch_reserve_mb * 1024 * 1024}
    slots: list[dict] = []
    self_test_slot: dict | None = None
    try:
        root = _scratch_root(s)
        usage = shutil.disk_usage(root)
        scratch.update(root=str(root), free_bytes=usage.free, total_bytes=usage.total)
        # One index past the pool is the dedicated self-test slot: both
        # workers contend for it at boot, and a busy one is the whole reason
        # a self-test can come back unknown, so the report has to show it.
        pool_size = int(s.rfp_ingest_sandbox_uid_pool_size)
        occupancy = runner.slot_occupancy(
            root,
            pool_base=s.rfp_ingest_sandbox_uid_pool_base,
            pool_size=pool_size + 1,
        )
        slots = occupancy[:pool_size]
        self_test_slot = occupancy[pool_size] if len(occupancy) > pool_size else None
    except Exception:  # noqa: BLE001 - the report must still answer
        logger.exception("rfp ingest: scratch inspection failed")
        notes.append("The scratch root could not be inspected.")
    return {
        "enabled": bool(s.rfp_ingest_enabled),
        "limits": limits.to_dict(),
        "limits_hash": limits.limits_hash(),
        "versions": {
            "sandbox_version": protocol.SANDBOX_VERSION,
            "protocol_version": protocol.PROTOCOL_VERSION,
            "python": platform.python_version(),
            "pypdfium2": _dist_version("pypdfium2"),
            "pillow": _dist_version("pillow"),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "euid": euid,
            "uid_switch": switch,
            "sandbox_uid": s.rfp_ingest_sandbox_uid,
            "uid_pool_base": s.rfp_ingest_sandbox_uid_pool_base,
            "uid_pool_size": s.rfp_ingest_sandbox_uid_pool_size,
            "self_test_uid": (
                s.rfp_ingest_sandbox_uid_pool_base + s.rfp_ingest_sandbox_uid_pool_size
            ),
            "self_test_slot": self_test_slot,
            "pid": os.getpid(),          # which worker answered (they cache separately)
            "notes": notes,
        },
        "scratch": scratch,
        "slots": slots,
        "concurrency": {
            "sandbox_concurrency": s.rfp_ingest_sandbox_concurrency,
            "active_runs": active_run_count(),
        },
        "office_files": {
            "enabled": bool(s.rfp_ingest_office_files_enabled),
            "engine": office.ENGINE,
            "formats": sorted(protocol.OFFICE_FORMATS),
            "convert_timeout_seconds": s.rfp_ingest_office_convert_timeout_seconds,
        },
        "caps": {
            "max_file_bytes": s.rfp_ingest_max_file_bytes,
            "max_files_per_run": s.rfp_ingest_max_files_per_run,
            "max_pages_per_file": s.rfp_ingest_max_pages_per_file,
            "max_pages_per_run": s.rfp_ingest_max_pages_per_run,
            "max_text_bytes_per_file": s.rfp_ingest_max_text_bytes_per_file,
            "images_pdf_part_bytes": s.rfp_ingest_images_pdf_part_bytes,
            "file_timeout_seconds": _file_timeout_seconds(s),
            "retention_days": s.rfp_ingest_retention_days,
        },
        "self_test": (
            self_test() if self_test_fresh else (last_self_test() or dict(_SELF_TEST_NOT_RUN))
        ),
        "self_test_fresh": self_test_fresh,
    }


def _clear_expired_paths(sb: Any, run_id: str) -> None:
    """An expired run keeps its rows and manifests but its objects are gone,
    so the columns the readers mint signed URLs from are cleared: otherwise
    the page would keep offering downloads and thumbnails that every viewer
    gets a 400 from."""
    files = _run_files(sb, run_id, "id")
    if not files:
        return
    sb.table(FILES_TABLE).update(
        {"derived_prefix": None, "images_pdf_paths": None, "text_path": None,
         "converted_path": None}
    ).eq("run_id", run_id).execute()
    ids = [f["id"] for f in files]
    # One statement per chunk, not one per file: a run holds up to
    # rfp_ingest_max_files_per_run rows and the prune walks a whole page of
    # runs on the queue's worker thread, where every round trip blocks the
    # claim tick behind it.
    for start in range(0, len(ids), _PATH_CLEAR_BATCH):
        sb.table(PAGES_TABLE).update({"thumb_path": None, "full_path": None}).in_(
            "file_id", ids[start : start + _PATH_CLEAR_BATCH]
        ).execute()


def _prune_abandoned_staging(sb: Any) -> int:
    """Staging runs nobody started: `add_upload_file` stores every accepted
    PDF in the quarantine bucket as it arrives, but a staging run has no
    completed_at and never becomes terminal, so the expiry pass below could
    never see it and a closed tab leaked its uploads forever. After
    `_STAGING_ABANDON_HOURS` of no writes the run is canceled (fenced on
    `staging`, so a run started in the same second is left alone), moved
    straight on to `expired`, and only then do its objects go.

    Both marks, in that order, before the deletes: `canceled` is the write
    that rings the bell once and carries `_MSG_STAGING_ABANDONED`, but
    `canceled` is also a status /retry accepts, and retrying a run whose
    source bytes have just been deleted rejects every one of its files as
    `not_stored` with no path back. `expired` is the terminal status /retry
    refuses, and it overwrites neither `error` nor `completed_at`. The file
    rows lose their quarantine_path with the objects, the way the expiry pass
    clears the columns it invalidates. A crash between the CAS and the deletes
    leaves an expired run holding its objects for the next `delete_run`, which
    is the tradeoff `prune_expired` already documents."""
    cutoff = (_now() - timedelta(hours=_STAGING_ABANDON_HOURS)).isoformat()
    rows = (
        sb.table(RUNS_TABLE)
        .select("id, status, updated_at")
        .eq("status", protocol.RUN_STAGING)
        .lt("updated_at", cutoff)
        .order("updated_at")
        .limit(_PRUNE_PAGE)
        .execute()
    ).data or []
    swept = 0
    for run in rows:
        if _mark(
            run["id"],
            status=protocol.RUN_CANCELED,
            from_statuses=[protocol.RUN_STAGING],
            error=_MSG_STAGING_ABANDONED,
        ) is None:
            continue
        swept += 1
        _mark(run["id"], status=protocol.RUN_EXPIRED)
        sb.table(FILES_TABLE).update({"quarantine_path": None}).eq(
            "run_id", run["id"]
        ).execute()
        try:
            rs.delete_prefix(rs.QUARANTINE_BUCKET, run["id"])
            rs.delete_prefix(rs.DERIVED_BUCKET, run["id"])
        except Exception:  # noqa: BLE001 - the expiry pass deletes them again later
            logger.exception(
                "rfp ingest: prune could not delete abandoned staging run %s", run["id"]
            )
    return swept


def prune_expired() -> int:
    """Hourly (from the queue's prune slot): orphaned scratch directories are
    swept, abandoned staging runs are canceled, expired and lose their
    quarantine objects, and terminal runs whose completed_at is older than
    rfp_ingest_retention_days are marked expired (only from a terminal
    status) and then lose both storage prefixes; rows and manifests are kept,
    the columns pointing at the deleted objects are cleared. Returns how many
    runs expired.

    The expired CAS comes FIRST and the deletes follow: the statuses this
    pass reads are exactly the ones /retry accepts, so deleting first left a
    window in which a retry resurrected a run whose source bytes had just
    been removed, and every one of its files became rejected/not_stored with
    no path back. A delete that fails after the CAS leaves the run expired
    with its objects for the next `delete_run`."""
    s = get_settings()
    sb = get_supabase()
    _sweep_scratch(s)
    _prune_abandoned_staging(sb)
    cutoff = (_now() - timedelta(days=int(s.rfp_ingest_retention_days))).isoformat()
    expired = 0
    while True:
        rows = (
            sb.table(RUNS_TABLE)
            .select("id, status, completed_at")
            .in_("status", _RUN_EXPIRABLE_FROM)
            .lt("completed_at", cutoff)
            .order("completed_at")
            .limit(_PRUNE_PAGE)
            .execute()
        ).data or []
        progressed = 0
        for run in rows:
            if _mark(run["id"], status=protocol.RUN_EXPIRED) is None:
                continue                      # a retry took it back; its bytes stay
            expired += 1
            progressed += 1
            _clear_expired_paths(sb, run["id"])
            try:
                rs.delete_prefix(rs.DERIVED_BUCKET, run["id"])
                rs.delete_prefix(rs.QUARANTINE_BUCKET, run["id"])
            except Exception:  # noqa: BLE001 - the row is expired; objects wait for a delete
                logger.exception("rfp ingest: prune could not delete run %s", run["id"])
        if len(rows) < _PRUNE_PAGE or progressed == 0:
            return expired


def _sanitize_query(q: str) -> str:
    """The emails router's sanitizer when it is importable, else the same
    rule: strip PostgREST filter syntax and ilike wildcards, cap at 200."""
    try:
        from app.routers.emails import _sanitize_query as shared
    except Exception:  # noqa: BLE001 - the router is optional to this module
        return re.sub(r"[,()%_*\\]", " ", q[:200]).strip()
    return shared(q)


def recent_emails(*, q: str | None, limit: int) -> list[dict]:
    """Recent inbound ingested_emails for the picker, newest first, with
    attachment counts (`attachment_count`, `stored_count`)."""
    sb = get_supabase()
    limit = max(1, min(int(limit), _LIST_MAX))
    query = sb.table("ingested_emails").select(_EMAIL_COLUMNS).eq("direction", "inbound")
    if q:
        term = _sanitize_query(q)
        if term:
            query = query.or_(
                f"subject.ilike.%{term}%,from_address.ilike.%{term}%,from_name.ilike.%{term}%"
            )
    emails = (
        query.order("message_at", desc=True).order("created_at", desc=True).limit(limit).execute()
    ).data or []
    if not emails:
        return []
    counts: dict[str, list[int]] = {e["id"]: [0, 0] for e in emails}
    attachments = (
        sb.table("ingested_email_attachments")
        .select("email_id, storage_path")
        .in_("email_id", list(counts))
        .execute()
    ).data or []
    for att in attachments:
        entry = counts.get(att.get("email_id"))
        if entry is None:
            continue
        entry[0] += 1
        if att.get("storage_path"):
            entry[1] += 1
    for email in emails:
        total, stored = counts[email["id"]]
        email["attachment_count"] = total
        email["stored_count"] = stored
        email["subject"] = _display(email.get("subject"), 300)
        email["from_name"] = _display(email.get("from_name"), 200)
    return emails
