"""The child <-> parent contract for the RFP Ingestion Sandbox.

Pure constants and tiny helpers, importable from BOTH sides. The parent
(app/services/rfp_sandbox_runner.py) builds the limits document, launches the
child, and parses progress logs against these names; the child
(app/sandbox/__main__.py) reads the limits document and writes progress events
with them. Nothing here may import anything outside the standard library.

Versioning: bump SANDBOX_VERSION for any change in what the child produces
(render settings semantics, text extraction, hazard counting). Bump
PROTOCOL_VERSION only when the shape of the progress log or the limits document
changes incompatibly. Both are recorded on every file row so outputs can be
reused or invalidated later.

Launch (parent side):

    [sys.executable, "-I", "-X", "utf8", "-c", BOOTSTRAP, <repo_root>,
     "--spawn", <n>, "--input", <pdf>, "--out", <dir>, "--limits", <json>,
     ("--skip-file", <path>)]
    [sys.executable, "-I", "-X", "utf8", "-c", BOOTSTRAP, <repo_root>,
     "--verify", "--out", <dir>, "--limits", <json>, "--list-file", <path>]

`--spawn <n>` is the 0-based spawn index for this file: the child writes
`progress_file(n)` (O_CREAT | O_EXCL | O_APPEND; an existing file is a parent
bug and exits EXIT_BAD_ARGS) and the parent pre-creates `stderr_file(n)`.
Verify mode writes VERIFY_FILE the same way (O_EXCL).

`-I` is full isolation: no PYTHON* env vars, no user site, and neither the
script directory nor the cwd is added to sys.path, so the child imports only
what BOOTSTRAP puts on the path (the repo root, for `app.sandbox`). The cwd is
the out dir, never the repo (locally the repo holds .env). `-X utf8` fixes the
text encoding regardless of the (scrubbed) environment.

Crash attribution (parent side). This table is the authority; section 3.2 of
docs/RFP_INGESTION_SANDBOX.md summarizes it and must not disagree with it.

A. The parent killed the child. Its own reason decides, ahead of anything in
   the log, with one exception: `linger` (the child wrote its terminal event
   and then would not exit) is dropped and the verdict is whatever it wrote.

     abort (the run was cancelled) ............... failed/interrupted
     invalid output (a log, an artifact or an .... failed/invalid_output
       out-dir entry the parent refuses, incl.
       a parent-derived name the child planted
       inside <out>, which it can write to)
     the out dir passed the disk quota ........... failed/resource_limit
     whole-file timeout .......................... failed/resource_limit
     open timeout (no `document` in time) ........ failed/resource_limit
     ready timeout (no `ready` in time) .......... failed/spawn
     the run's page budget was exhausted ......... rejected/run_page_budget
     a respawn reported a different page count ... rejected/unreadable
     page stall (silence past the stall bound) ... blame, per C below, with
                                                   page-fail code `stall`
     the queue lease was lost .................... the run raises LeaseLost

B. The child exited on its own.

     exit EXIT_BAD_ARGS, or no `start`, or no `ready` ..... failed/spawn
       (a parent or image problem, never the file's fault, never respawned)
     a `reject` event ..................................... rejected/<its code>
       on spawn 0; on a later spawn the code is not trusted (the file already
       parsed once) and the verdict is rejected/unreadable
     no `document` and no `reject` ........................ rejected/unreadable
     `end` with `aborted` set ............................. failed/resource_limit
       (pages with no event of their own are recorded failed/aborted for the
       audit trail)
     `end` but some index still has no event .............. failed/invalid_output
     `end` with every index covered ....................... the spawn is complete
     died after `document` with no `end` .................. blame, per C below

C. Blame and respawn (a page stall, or a death after `document`).

     * a dangling `page_start` (no matching `page`) blames THAT index. The
       page-fail code is `stall` after a stall kill; after a death it is
       `memory` when the limits document's memory rlimit actually applied AND
       the child died by SIGKILL/SIGABRT or a non-zero exit code, else `crash`;
     * otherwise the first index not in (skip list + indices with a `page`
       event) is blamed, with `stall` after a stall kill and `crash` after a
       death (never `memory`, since no page was open);
     * a blamed index is added to the skip list, its partial artifacts are
       unlinked, and the child is respawned;
     * nothing left to blame (every index already covered), or one restart
       more than max(max_restarts, ceil(page_count * restart_ratio)):
       rejected/crash_loop.

D. Verify mode (--verify). The verify spawn is monitored against a budget of
   its OWN, max(what is left of the file budget, 2 * the page stall), because
   the file budget is sized off the configured page cap rather than the real
   page count and a long render can leave verify nothing. Abort, invalid
   output and the disk quota keep their whole-file verdicts, and EXIT_BAD_ARGS
   is failed/spawn; a page stall or a wall-clock kill records only its bound
   (file_timeout, page_stall) and does NOT fail the file, since throwing away
   a fully rendered set because the second pass ran long is the wrong trade.
   Then every page that rendered ok but whose thumb and full were not BOTH
   reported ok=true is turned into failed/verify with its artifacts dropped,
   and the gap rule decides the file. A verify-spawn-level verdict returns
   only the failed placeholders, never ok pages with their bytes nulled.

The child MUST emit `page_start` before any PDFium call for that page, MUST
write its terminal event (`end`, or a `reject`) before any further PDFium work
such as closing the document, and MUST write every event as ONE os.write of the
whole line (O_APPEND) so a kill can tear at most the final line, which the
parent discards when the child did not exit cleanly.
"""

from __future__ import annotations

import hashlib
import json

SANDBOX_VERSION = "1.0.0"
PROTOCOL_VERSION = 1

# ── Limits document (parent -> child, JSON file) ─────────────────────────────
# Every key is required; the child refuses to start on a missing or mistyped
# key (exit code EXIT_BAD_ARGS) so a parent bug cannot silently run unbounded.
# The child applies every rlimit as (soft, hard) = (v, v) so it cannot raise
# them back, and records which ones actually applied in the `start` event.
LIMIT_KEYS: dict[str, type] = {
    "memory_bytes": int,            # RLIMIT_AS
    "cpu_seconds": int,             # RLIMIT_CPU
    "max_output_file_bytes": int,   # RLIMIT_FSIZE (largest single output file)
    "max_output_total_bytes": int,  # child-tracked disk quota for <out> (parent mirrors it)
    "max_open_files": int,          # RLIMIT_NOFILE
    "max_processes": int,           # RLIMIT_NPROC (1 = no forks; per uid, ignored for root)
    "max_pages": int,               # over -> reject too_many_pages
    "max_page_side_pt": int,        # a page with a longer side fails (page_size)
    "thumb_long_side": int,         # px
    "thumb_jpeg_quality": int,
    "full_long_side": int,          # px, sheets longer than full_small_threshold_pt
    "full_small_long_side": int,    # px, pages up to full_small_threshold_pt (letter/tabloid)
    "full_small_threshold_pt": int, # long side in points at or below which the small tier applies
    "full_jpeg_quality": int,
    "max_text_chars_per_page": int,
    "deadline_seconds": int,        # child self-deadline (wall clock) for THIS spawn
    "heartbeat_seconds": int,       # max silence during open/inventory before a heartbeat
}
# Keys the parent may vary per spawn without changing the limits hash.
PER_SPAWN_KEYS = frozenset({"deadline_seconds"})

# ── CLI ──────────────────────────────────────────────────────────────────────
MODULE = "app.sandbox"
INTERPRETER_FLAGS = ("-I", "-X", "utf8")
BOOTSTRAP = (
    "import sys; root = sys.argv.pop(1); sys.path.insert(0, root); "
    "import runpy; runpy.run_module('app.sandbox', run_name='__main__', alter_sys=True)"
)
MODE_PROCESS = "process"   # default: render + text + hazards
MODE_VERIFY = "verify"     # --verify: re-decode listed JPEGs in a fresh sandbox

EXIT_OK = 0            # an `end` or `reject` event was written
EXIT_BAD_ARGS = 2      # arguments or limits document unusable (parent bug)
# Any other exit code, or EXIT_OK without an end/reject event, is a crash.

# ── Output layout (relative to <out>) ────────────────────────────────────────
PROGRESS_FILE_TEMPLATE = "progress.{spawn:02d}.jsonl"   # one per spawn, append-only
STDERR_FILE_TEMPLATE = "stderr.{spawn:02d}.log"         # child's stderr, never a pipe
VERIFY_FILE = "verify.jsonl"
THUMB_DIR = "thumb"
FULL_DIR = "full"
TEXT_DIR = "text"
TMP_DIR = "tmp"
PAGE_NAME_WIDTH = 4
# Every page JPEG is RGB (3 components); the parent's marker walk refuses any
# other component count and the images PDF writer relies on /DeviceRGB.
JPEG_COMPONENTS = 3
# Parent-side per-artifact byte caps (mirrors of RLIMIT_FSIZE, applied BEFORE reading).
MAX_THUMB_BYTES = 16 * 1024 * 1024
MAX_FULL_BYTES = 48 * 1024 * 1024
MAX_PROGRESS_LINE_BYTES = 64 * 1024
MAX_STDERR_TAIL_BYTES = 4 * 1024
# Extra lines allowed beyond 2 * pages (start, ready, document, end, heartbeats).
MAX_PROGRESS_EXTRA_LINES = 4096


def page_file(kind_dir: str, index: int, ext: str) -> str:
    """Relative output path for a page artifact, e.g. thumb/0007.jpg. The
    parent derives names with this function and NEVER opens a path taken from
    the child's log."""
    return f"{kind_dir}/{index:0{PAGE_NAME_WIDTH}d}.{ext}"


def progress_file(spawn: int) -> str:
    return PROGRESS_FILE_TEMPLATE.format(spawn=spawn)


def stderr_file(spawn: int) -> str:
    return STDERR_FILE_TEMPLATE.format(spawn=spawn)


# ── Progress events (child -> parent, one JSON object per line) ─────────────
EVENT_START = "start"          # limits applied; BEFORE importing pypdfium2/Pillow
EVENT_READY = "ready"          # pypdfium2 + Pillow imported, versions known
EVENT_HEARTBEAT = "heartbeat"  # {phase, elapsed_ms} during open/inventory
EVENT_DOCUMENT = "document"
EVENT_REJECT = "reject"
EVENT_PAGE_START = "page_start"
EVENT_PAGE = "page"
EVENT_END = "end"
EVENTS = frozenset(
    {
        EVENT_START,
        EVENT_READY,
        EVENT_HEARTBEAT,
        EVENT_DOCUMENT,
        EVENT_REJECT,
        EVENT_PAGE_START,
        EVENT_PAGE,
        EVENT_END,
    }
)
# Verify mode writes {"event": "verified", "file": ..., "w": ..., "h": ..., "ok": bool}
EVENT_VERIFIED = "verified"

PHASE_OPEN = "open"
PHASE_INVENTORY = "inventory"
PHASE_RENDER = "render"

PAGE_OK = "ok"
PAGE_FAILED = "failed"

# Document-level reject codes the CHILD can emit.
REJECT_ENCRYPTED = "encrypted"
REJECT_UNREADABLE = "unreadable"
REJECT_NO_PAGES = "no_pages"
REJECT_TOO_MANY_PAGES = "too_many_pages"
CHILD_REJECT_CODES = frozenset(
    {REJECT_ENCRYPTED, REJECT_UNREADABLE, REJECT_NO_PAGES, REJECT_TOO_MANY_PAGES}
)

# Page-level failure codes the CHILD can emit.
PAGE_FAIL_SIZE = "page_size"
PAGE_FAIL_RENDER = "render_error"
PAGE_FAIL_MEMORY = "memory"
CHILD_PAGE_FAIL_CODES = frozenset({PAGE_FAIL_SIZE, PAGE_FAIL_RENDER, PAGE_FAIL_MEMORY})

# Page-level failure codes the PARENT attributes (the child never writes these).
PAGE_FAIL_CRASH = "crash"
PAGE_FAIL_STALL = "stall"
PAGE_FAIL_ABORTED = "aborted"
PAGE_FAIL_VERIFY = "verify"

# `end.aborted` values.
ABORT_DISK_QUOTA = "disk_quota"
ABORT_DEADLINE = "deadline"

# Form types reported in the document event.
FORM_NONE = "none"
FORM_ACROFORM = "acroform"
FORM_XFA_FULL = "xfa_full"
FORM_XFA_FOREGROUND = "xfa_foreground"

# Hazard keys. Document-level ones come from the document event; page-level
# ones are summed by the parent across page events. Byte markers are scanned
# by the PARENT (pure bytes, no parser) in rfp_sanitize.
DOC_HAZARD_KEYS = ("javascript_actions", "attachments", "xfa_packets")
PAGE_HAZARD_KEYS = (
    "uri_links",
    "launch_actions",
    "remote_goto",
    "embedded_goto",
    "page_actions",
    "file_attachments",
)
BYTE_MARKERS = (
    b"/OpenAction",
    b"/AA",
    b"/JavaScript",
    b"/JS",
    b"/Launch",
    b"/EmbeddedFile",
    b"/XFA",
    b"/URI",
    b"/RichMedia",
    b"/GoToR",
)
# Per-page text hazard counters the child records (and the parent re-counts).
TEXT_HAZARD_KEYS = ("format_chars", "private_use", "unassigned", "replacement_chars")
METADATA_KEYS = (
    "title",
    "author",
    "subject",
    "keywords",
    "creator",
    "producer",
    "creation_date",
    "mod_date",
)
METADATA_MAX_CHARS = 500
DETAIL_MAX_CHARS = 1000

# ── Parent-side file verdicts (rfp_ingest_files.status / reject_code) ───────
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_VERIFIED = "verified"
STATUS_VERIFIED_WITH_GAPS = "verified_with_gaps"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
FILE_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_VERIFIED,
        STATUS_VERIFIED_WITH_GAPS,
        STATUS_REJECTED,
        STATUS_FAILED,
    }
)
FILE_TERMINAL_STATUSES = frozenset(
    {STATUS_VERIFIED, STATUS_VERIFIED_WITH_GAPS, STATUS_REJECTED, STATUS_FAILED}
)

# Run statuses (rfp_ingest_runs.status).
RUN_STAGING = "staging"
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_DONE_WITH_ERRORS = "done_with_errors"
RUN_FAILED = "failed"
RUN_CANCELED = "canceled"
RUN_EXPIRED = "expired"
RUN_STATUSES = frozenset(
    {
        RUN_STAGING,
        RUN_PENDING,
        RUN_RUNNING,
        RUN_DONE,
        RUN_DONE_WITH_ERRORS,
        RUN_FAILED,
        RUN_CANCELED,
        RUN_EXPIRED,
    }
)
RUN_ACTIVE_STATUSES = frozenset({RUN_STAGING, RUN_PENDING, RUN_RUNNING})
RUN_TERMINAL_STATUSES = frozenset(
    {RUN_DONE, RUN_DONE_WITH_ERRORS, RUN_FAILED, RUN_CANCELED, RUN_EXPIRED}
)

# Source formats the PARENT records on every file row (rfp_ingest_files.
# source_format). The child only ever sees a PDF: an office file is converted
# to one by the parent's Gotenberg step (docs/RFP_INGESTION_SANDBOX.md,
# section 2.1) and the derived PDF is what the child opens. The sniff that
# assigns these lives in app/services/rfp_sanitize.py.
SOURCE_FORMAT_PDF = "pdf"
SOURCE_FORMAT_DOCX = "docx"
SOURCE_FORMAT_XLSX = "xlsx"
SOURCE_FORMAT_DOC = "doc"
SOURCE_FORMAT_XLS = "xls"
OFFICE_FORMATS = frozenset(
    {SOURCE_FORMAT_DOCX, SOURCE_FORMAT_XLSX, SOURCE_FORMAT_DOC, SOURCE_FORMAT_XLS}
)
SOURCE_FORMATS = (
    SOURCE_FORMAT_PDF,
    SOURCE_FORMAT_DOCX,
    SOURCE_FORMAT_XLSX,
    SOURCE_FORMAT_DOC,
    SOURCE_FORMAT_XLS,
)

# Reject codes the PARENT can assign (a superset of the child's). Permanent:
# retry never revisits a rejected file. No DB CHECK constraint: this tuple is
# the authority.
REJECT_NOT_PDF = "not_pdf"
REJECT_POLYGLOT = "polyglot"
REJECT_TOO_LARGE = "too_large"
REJECT_EMPTY = "empty"
REJECT_DUPLICATE = "duplicate"
REJECT_ITEM_ATTACHMENT = "item_attachment"
REJECT_NOT_STORED = "not_stored"
REJECT_RUN_PAGE_BUDGET = "run_page_budget"
REJECT_CRASH_LOOP = "crash_loop"
REJECT_TOO_MANY_FAILED_PAGES = "too_many_failed_pages"
# The converter answered 4xx (a corrupt or unsupported office document), or
# what it returned was not a PDF by the parent's own byte sniff.
REJECT_CONVERSION_REJECTED = "conversion_rejected"
REJECT_CODES = CHILD_REJECT_CODES | frozenset(
    {
        REJECT_NOT_PDF,
        REJECT_POLYGLOT,
        REJECT_TOO_LARGE,
        REJECT_EMPTY,
        REJECT_DUPLICATE,
        REJECT_ITEM_ATTACHMENT,
        REJECT_NOT_STORED,
        REJECT_RUN_PAGE_BUDGET,
        REJECT_CRASH_LOOP,
        REJECT_TOO_MANY_FAILED_PAGES,
        REJECT_CONVERSION_REJECTED,
    }
)

# `failed` (retryable) error codes, stored in rfp_ingest_files.reject_code too
# (the column is the verdict code for both statuses).
FAIL_INVALID_OUTPUT = "invalid_output"
FAIL_STORAGE = "storage"
FAIL_SPAWN = "spawn"
FAIL_RESOURCE_LIMIT = "resource_limit"
FAIL_INTERRUPTED = "interrupted"
FAIL_ORPHANED = "orphaned"
# The office-to-PDF converter could not be reached, timed out or answered
# 5xx: retryable, like a storage failure.
FAIL_CONVERSION_UNAVAILABLE = "conversion_unavailable"
FAIL_CODES = frozenset(
    {
        FAIL_INVALID_OUTPUT,
        FAIL_STORAGE,
        FAIL_SPAWN,
        FAIL_RESOURCE_LIMIT,
        FAIL_INTERRUPTED,
        FAIL_ORPHANED,
        FAIL_CONVERSION_UNAVAILABLE,
    }
)

# User-facing sentences for every verdict code. App-authored; nothing from an
# exception, a filename or the child ever lands in an error column verbatim.
VERDICT_MESSAGES: dict[str, str] = {
    REJECT_ENCRYPTED: "The PDF is password-protected and cannot be processed.",
    REJECT_UNREADABLE: "The PDF could not be opened by the sandbox.",
    REJECT_NO_PAGES: "The PDF has no pages.",
    REJECT_TOO_MANY_PAGES: "The PDF has more pages than the per-file limit.",
    REJECT_NOT_PDF: "The file is not a PDF, Word or Excel file.",
    REJECT_POLYGLOT: "The file starts as another format and only contains a PDF inside it.",
    REJECT_TOO_LARGE: "The file is larger than the per-file limit.",
    REJECT_EMPTY: "The file is empty.",
    REJECT_DUPLICATE: "A file with identical content is already in this run.",
    REJECT_ITEM_ATTACHMENT: "The attachment is an embedded item, not a file.",
    REJECT_NOT_STORED: "The attachment content is no longer available.",
    REJECT_RUN_PAGE_BUDGET: "The file would exceed the run's page budget.",
    REJECT_CRASH_LOOP: "The sandbox kept crashing on this file.",
    REJECT_TOO_MANY_FAILED_PAGES: "Too many pages failed to render.",
    REJECT_CONVERSION_REJECTED: "The file could not be converted to PDF.",
    FAIL_INVALID_OUTPUT: "The sandbox output failed validation.",
    FAIL_STORAGE: "A storage operation failed; retry the run.",
    FAIL_SPAWN: "The sandbox could not be started; check the deployment.",
    FAIL_RESOURCE_LIMIT: "Processing hit a resource limit; retry or raise the limit.",
    FAIL_INTERRUPTED: "Processing was interrupted; retry the run.",
    FAIL_ORPHANED: "The file was never processed; retry the run.",
    FAIL_CONVERSION_UNAVAILABLE: "The PDF converter was unavailable; retry the run.",
}

# Bounds the parent records in `bounds_hit` when they fire.
BOUND_PAGE_STALL = "page_stall"
BOUND_OPEN_TIMEOUT = "open_timeout"
BOUND_FILE_TIMEOUT = "file_timeout"
BOUND_RESTARTS = "child_restarts"
BOUND_DISK_QUOTA = "disk_quota"
BOUND_DEADLINE = "child_deadline"
BOUND_TEXT_BYTES = "text_bytes"
BOUND_FAILED_PAGE_RATIO = "failed_page_ratio"
BOUND_IMAGES_PDF = "images_pdf"
BOUND_RUN_PAGE_BUDGET = "run_page_budget"

# File processing phases (rfp_ingest_files.phase), updated every few seconds.
PHASE_MATERIALIZE = "materialize"
PHASE_CONVERT = "convert"          # office file -> PDF through the converter
PHASE_SANDBOX = "sandbox"
PHASE_VERIFY = "verify"
PHASE_UPLOAD = "upload"


def canonical_json(value: object) -> str:
    """Deterministic JSON (sorted keys, no whitespace) for hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def limits_hash(limits: dict) -> str:
    """sha256 of the canonical limits document with the per-spawn keys removed,
    stored on every file row so a later slice can tell whether existing outputs
    were made under the same bounds."""
    stable = {k: v for k, v in limits.items() if k not in PER_SPAWN_KEYS}
    return hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()


def validate_limits(limits: object) -> dict:
    """Return the limits dict if every key is present with the right type and a
    positive value; raise ValueError otherwise. Used by both sides."""
    if not isinstance(limits, dict):
        raise ValueError("limits must be a JSON object")
    out: dict = {}
    for key, typ in LIMIT_KEYS.items():
        if key not in limits:
            raise ValueError(f"limits missing {key}")
        value = limits[key]
        # bool is an int subclass; refuse it explicitly.
        if isinstance(value, bool) or not isinstance(value, typ):
            raise ValueError(f"limits.{key} must be {typ.__name__}")
        if value <= 0:
            raise ValueError(f"limits.{key} must be positive")
        out[key] = value
    extra = set(limits) - set(LIMIT_KEYS)
    if extra:
        raise ValueError(f"limits has unknown keys: {sorted(extra)}")
    if out["thumb_long_side"] > out["full_small_long_side"]:
        raise ValueError("limits.thumb_long_side must not exceed full_small_long_side")
    if out["full_small_long_side"] > out["full_long_side"]:
        raise ValueError("limits.full_small_long_side must not exceed full_long_side")
    for key in ("thumb_jpeg_quality", "full_jpeg_quality"):
        if out[key] > 100:
            raise ValueError(f"limits.{key} must be <= 100")
    if out["max_output_file_bytes"] > out["max_output_total_bytes"]:
        raise ValueError("limits.max_output_file_bytes must not exceed the total quota")
    return out
