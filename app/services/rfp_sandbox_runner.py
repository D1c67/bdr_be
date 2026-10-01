"""The parent side of the RFP Ingestion Sandbox: spawn, monitor, kill, respawn,
and re-validate an untrusted child that renders one PDF.

This module is the trust boundary described in docs/RFP_INGESTION_SANDBOX.md
(sections 1, 2, 3.1, 3.2 and 3.4) and app/sandbox/protocol.py (whose module
docstring is the authority on the launch command and on crash attribution).
The API process calls `run_sandbox()` once per file; everything the child
produces is treated as attacker-controlled until this module has checked it
byte by byte:

- Names are DERIVED (`protocol.page_file`), never read from the child's log.
  An event's `file` field must equal the derived name, and every artifact is
  opened relative to a directory fd (`O_NOFOLLOW`, one level at a time) that
  the parent opened BEFORE the child was spawned.
- Each artifact must be a regular file with `st_nlink == 1`, owned by the
  sandbox uid when a uid switch is in force, under the tier's byte cap BEFORE
  it is read, read once into a bounded buffer, and its sha256 and size must
  match the log. JPEGs are checked with the hand-written marker walk in
  `rfp_sanitize.jpeg_dimensions` (never decoded here); the decode happens in a
  second, fresh `--verify` sandbox invocation. Text is re-sanitized here and
  held to the per-page character cap.
- Page text is NOT accumulated without a bound: validation spends a running
  per-file budget (`max_text_bytes_per_file`, counted in UTF-8 bytes, which is
  never smaller than CPython's per-character width) and every page past it
  carries an empty string, so one file's resident text cannot exceed that
  budget plus a single page.
- Image bytes are NOT accumulated: with a `page_sink`, validation drops each
  buffer as soon as it has been hashed and walked, and a complete file's ok
  pages are re-opened one at a time after the verify spawn under the same
  discipline, re-hashed against the digest recorded at validation, and handed
  to the sink (one page resident at a time). Without a sink (the smoke script,
  the fake-child tests) the validated bytes ride out on the PageOutputs.
- The progress log is tailed incrementally with a 64 KB line cap and a total
  line cap, parsed with `parse_constant` refusing NaN/Infinity and
  `RecursionError` caught, every number `isfinite` and range-checked, and
  `page_count` validated before any per-page structure exists (pages live in a
  dict keyed by index, never a list sized by the child's number).
- The out directory is walked with `lstat`; anything unnamed, any symlink,
  FIFO, socket or device, or any nested surprise is `failed/invalid_output`,
  and then nothing is returned as bytes.

Kill discipline is the same on every exit path: `os.killpg(SIGKILL)` on the
child's own session while the Popen object is still unreaped (swallowing
`ProcessLookupError`), then `wait()`, then the log is re-read to EOF, then
blame is attributed, then validation runs. `communicate()` is never used: the
child's stderr goes to a file (an undrained pipe reads as a stall).

What this module deliberately does NOT do: touch Supabase, the network, or
any Settings object (limits arrive as a `SandboxLimits`, callbacks carry the
lease/cancel/progress plumbing), and it never parses a PDF. `run_sandbox`
never raises for anything the child did; it raises `SandboxSpawnError` for
parent-side faults (scratch, spawn) and `LeaseLost` when `renew()` reports
the queue lease gone (after killing the child). On macOS (local development)
the parent is not root, so no uid switch happens and this is not a security
boundary; see the design doc's honest-limitations list.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.sandbox import protocol

logger = logging.getLogger(__name__)

# ── Cadences and constants ───────────────────────────────────────────────────
# Module-level so tests can shorten them; production never changes them.
TICK_SECONDS = 0.5              # monitor loop period (spec 3.2)
PROGRESS_INTERVAL_SECONDS = 2.0  # on_progress() is called at most this often
RENEW_INTERVAL_SECONDS = 30.0    # renew() cadence, while running and while waiting for a slot
SLOT_POLL_SECONDS = 1.0          # uid-slot flock retry period
SLOT_WAIT_SECONDS = 300.0        # a wedged or leaked uid slot must not park a worker forever
HEARTBEAT_SECONDS = 10           # limits.heartbeat_seconds (child heartbeat cadence)
MAX_OPEN_FILES = 64              # limits.max_open_files (RLIMIT_NOFILE), a constant per 3.1
MAX_PROCESSES = 1                # limits.max_processes (RLIMIT_NPROC), a constant per 3.1
MIN_SPAWN_DEADLINE_SECONDS = 5   # a respawn's child deadline never drops below this
SLOT_LOCK_TEMPLATE = "rfp-ingest-slot-{slot}.lock"
# Fallback per-file text budget for a caller that does not pass one (the
# settings default, repeated here because this module never reads Settings).
DEFAULT_MAX_TEXT_BYTES_PER_FILE = 64 * 1024 * 1024

STATUS_COMPLETE = "complete"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

_SCRATCH_PREFIX = "rfp-sandbox-"
_SOURCE_NAME = "source.pdf"
_OUT_NAME = "out"
_LIMITS_TEMPLATE = "limits.{spawn:02d}.json"
_SKIP_TEMPLATE = "skip.{spawn:02d}.txt"
_VERIFY_LIST_NAME = "verify-list.txt"
_READ_CHUNK = 1 << 16
_MAX_TAIL_BYTES_PER_TICK = 4 << 20   # a log that grows faster than this waits a tick
_MAX_OUT_DEPTH = 8                    # the disk-usage walk never descends deeper (tmp/ is free-form)
_OUT_ENTRY_SLACK = 8192               # entries allowed beyond 3 artifacts per page
_SHA256_HEX_LEN = 64
_PR_SET_CHILD_SUBREAPER = 36
_JPEG_MAX_SIDE = 65535
_CLEAN_MAX_CHARS = 500
_CLEAN_MAX_ITEMS = 64
_CLEAN_MAX_DEPTH = 4
_TIERS = ("full", "full_small")
_ROTATIONS = (0, 90, 180, 270)
_ARTIFACT_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CHILD_LANG = "C.UTF-8"

# Parent-side kill reasons (internal; never stored verbatim).
_KILL_READY_TIMEOUT = "ready_timeout"
_KILL_OPEN_TIMEOUT = "open_timeout"
_KILL_PAGE_STALL = "page_stall"
_KILL_FILE_TIMEOUT = "file_timeout"
_KILL_DISK_QUOTA = "disk_quota"
_KILL_ABORT = "abort"
_KILL_LEASE = "lease"
_KILL_PAGE_BUDGET = "page_budget"
_KILL_UNREADABLE = "unreadable"
_KILL_INVALID = "invalid"
_KILL_LINGER = "linger"   # the child finished its protocol but would not exit

# App-authored per-page detail for every failure code: the codes the PARENT
# attributes, plus fallbacks for the child's codes when its own `detail` is
# empty (a child detail passes through sanitize_display and a length cap).
PAGE_DETAIL_MESSAGES: dict[str, str] = {
    protocol.PAGE_FAIL_CRASH: "The sandbox crashed while rendering this page.",
    protocol.PAGE_FAIL_STALL: "Rendering this page stalled and the sandbox was stopped.",
    protocol.PAGE_FAIL_MEMORY: "Rendering this page exceeded the sandbox memory limit.",
    protocol.PAGE_FAIL_ABORTED: "The sandbox stopped before it reached this page.",
    protocol.PAGE_FAIL_VERIFY: "The rendered image failed decode verification.",
    protocol.PAGE_FAIL_SIZE: "The page is larger than the per-page size limit.",
    protocol.PAGE_FAIL_RENDER: "The page could not be rendered.",
}
INTERRUPTED_DETAIL = protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED]

_SUBREAPER_DONE = False

# Indirections so tests can rehearse the root-only paths (uid switch, chown)
# without being root. Production never touches these names.
_popen = subprocess.Popen
_chown = os.chown
_geteuid = os.geteuid


# ── Public types ─────────────────────────────────────────────────────────────


class LeaseLost(Exception):
    """`renew()` returned False: the queue lease is gone. The child has already
    been killed; the caller must not write anything for this job."""


class SandboxSpawnError(Exception):
    """A parent-side fault: the scratch layout or the spawn itself failed. Never
    raised for anything the child did."""


class SlotWaitTimeout(Exception):
    """No sandbox uid slot came free within the wait bound. Nothing was spawned
    and nothing was written, so the caller may retry the file or the run; the
    bound exists so an exhausted or leaked pool cannot park a worker forever
    with the queue lease renewed."""


class _Invalid(Exception):
    """Internal: the child's output violated the protocol (failed/invalid_output)."""


class _Stop(Exception):
    """Internal: the monitor must kill the child for `reason` right now."""

    def __init__(self, reason: str, note: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.note = note


@dataclass(frozen=True)
class SandboxLimits:
    """The limits document (one field per `protocol.LIMIT_KEYS` key). Built from
    Settings once per run; `with_deadline()` derives the per-spawn variant, and
    `limits_hash()` ignores the per-spawn keys so every spawn of a run shares
    one hash. Construction validates the document with
    `protocol.validate_limits` (a parent bug fails here, never in the child)."""

    memory_bytes: int
    cpu_seconds: int
    max_output_file_bytes: int
    max_output_total_bytes: int
    max_open_files: int
    max_processes: int
    max_pages: int
    max_page_side_pt: int
    thumb_long_side: int
    thumb_jpeg_quality: int
    full_long_side: int
    full_small_long_side: int
    full_small_threshold_pt: int
    full_jpeg_quality: int
    max_text_chars_per_page: int
    deadline_seconds: int
    heartbeat_seconds: int

    def __post_init__(self) -> None:
        protocol.validate_limits(self.to_dict())

    @classmethod
    def from_settings(cls, settings: object) -> SandboxLimits:
        """Map the `rfp_ingest_*` settings onto the limits document. The
        deadline starts at the file-timeout ceiling; `run_sandbox` replaces it
        per spawn with the remaining file budget."""
        mb = 1024 * 1024
        return cls(
            memory_bytes=int(settings.rfp_ingest_sandbox_memory_mb) * mb,
            cpu_seconds=int(settings.rfp_ingest_sandbox_cpu_seconds),
            max_output_file_bytes=int(settings.rfp_ingest_sandbox_output_file_mb) * mb,
            max_output_total_bytes=int(settings.rfp_ingest_sandbox_disk_mb) * mb,
            max_open_files=MAX_OPEN_FILES,
            max_processes=MAX_PROCESSES,
            max_pages=int(settings.rfp_ingest_max_pages_per_file),
            max_page_side_pt=int(settings.rfp_ingest_max_page_side_pt),
            thumb_long_side=int(settings.rfp_ingest_thumb_long_side),
            thumb_jpeg_quality=int(settings.rfp_ingest_thumb_jpeg_quality),
            full_long_side=int(settings.rfp_ingest_full_long_side),
            full_small_long_side=int(settings.rfp_ingest_full_small_long_side),
            full_small_threshold_pt=int(settings.rfp_ingest_full_small_threshold_pt),
            full_jpeg_quality=int(settings.rfp_ingest_full_jpeg_quality),
            max_text_chars_per_page=int(settings.rfp_ingest_max_text_chars_per_page),
            deadline_seconds=int(settings.rfp_ingest_file_timeout_max_seconds),
            heartbeat_seconds=HEARTBEAT_SECONDS,
        )

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in protocol.LIMIT_KEYS}

    def to_json(self) -> str:
        return protocol.canonical_json(self.to_dict())

    def with_deadline(self, seconds: int) -> SandboxLimits:
        return replace(self, deadline_seconds=max(MIN_SPAWN_DEADLINE_SECONDS, int(seconds)))

    def limits_hash(self) -> str:
        return protocol.limits_hash(self.to_dict())


@dataclass
class PageOutput:
    """One page of the result. For `status == "ok"` the bytes are the validated
    artifacts (read once) and `text` is the parent-re-sanitized string; for a
    failed page the bytes are None and `code` says why (a child code from
    `protocol.CHILD_PAGE_FAIL_CODES` or a parent code: crash, stall, memory,
    aborted, verify). A verify failure keeps the metadata and text but drops
    the image bytes. With a `page_sink` the bytes are None on every page,
    including the ok ones: the sink received them page by page and
    `thumb_meta`/`full_meta` still carry the size and digest that were
    checked. `text` is the page's text bounded by `max_text_chars_per_page`,
    or the empty string when the file's running text budget
    (`max_text_bytes_per_file`) was already spent by earlier pages;
    `text_chars` is always the true character count either way, and a spent
    budget is reported as `text_bytes` in `SandboxResult.bounds_hit`."""

    index: int
    status: str
    code: str | None
    detail: str | None
    width_pt: float | None
    height_pt: float | None
    rotation: int | None
    tier: str | None
    thumb: bytes | None
    full: bytes | None
    thumb_meta: dict | None
    full_meta: dict | None
    text: str | None
    text_chars: int
    text_truncated: bool
    text_hazards: dict
    hazards: dict
    render_ms: int | None


@dataclass
class SandboxResult:
    """Everything the caller needs to write the file row and upload outputs.
    `status` is complete | rejected | failed; `code` is a `protocol` reject code
    or FAIL_* code (None when complete) and `detail` is always app-authored
    (`protocol.VERDICT_MESSAGES`). When complete, `pages` holds every index
    0..page_count-1 exactly once, sorted; failed/invalid_output returns no
    pages at all, and every other verdict returns only the failed placeholders
    known so far (never an ok page, whose bytes the caller cannot have). `document` carries the cleaned
    `document` event, or the cleaned `reject` event (with its `event` key) when
    the verdict came from the child's reject."""

    status: str
    code: str | None
    detail: str | None
    start: dict | None
    ready: dict | None
    document: dict | None
    end: dict | None
    pages: list[PageOutput]
    hazards: dict
    bounds_hit: list[str]
    restarts: int
    elapsed_ms: int
    uid: int | None
    gid: int | None
    slot: int | None
    stderr_tail: str
    spawns: int
    peak_rss_kb: int | None


@dataclass
class UidSlot:
    """A claimed per-slot sandbox uid, held by an flock on the slot's lock file
    until `release()`. `uid == gid == pool_base + slot`."""

    uid: int
    gid: int
    slot: int
    _fd: int | None = field(default=None, repr=False)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# ── uid slots ────────────────────────────────────────────────────────────────


def _slot_lock_path(scratch_root: Path, slot: int) -> Path:
    return Path(scratch_root) / SLOT_LOCK_TEMPLATE.format(slot=slot)


def _try_lock(path: Path, *, create: bool) -> int | None:
    """Return an fd holding an exclusive flock on `path`, or None when busy.
    `O_NOFOLLOW`, and a file not owned by us is treated as busy: the scratch
    root may be a shared temp directory where another uid (a sandbox child
    included) could plant a name first."""
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SandboxSpawnError("The sandbox uid-slot lock file could not be opened.") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            logger.warning("rfp sandbox: slot lock %s is not ours; treating as busy", path)
            os.close(fd)
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(fd)
                return None
            raise
    except BaseException:
        os.close(fd)
        raise
    return fd


def acquire_uid_slot(
    scratch_root: Path,
    *,
    sandbox_uid: int,
    pool_base: int,
    pool_size: int,
    renew: Callable[[], bool],
    should_abort: Callable[[], bool],
    wait_timeout_seconds: float = SLOT_WAIT_SECONDS,
) -> UidSlot | None:
    """Claim a per-slot sandbox uid, or return None when no switch applies
    (parent not root, or `sandbox_uid == 0`, the explicit opt-out). Blocks
    until a slot is free, polling every second, calling `renew()` every 30 s
    (False raises LeaseLost) and raising SandboxSpawnError when
    `should_abort()` turns true while waiting. The wait itself is bounded by
    `wait_timeout_seconds`: on expiry it logs the pool occupancy and raises
    SlotWaitTimeout rather than waiting on a wedged slot forever."""
    if _geteuid() != 0 or int(sandbox_uid) == 0:
        return None
    if pool_size < 1:
        raise SandboxSpawnError("The sandbox uid pool is empty.")
    root = Path(scratch_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxSpawnError("The sandbox scratch root could not be created.") from exc
    last_renew = time.monotonic()
    deadline = last_renew + float(wait_timeout_seconds)
    while True:
        for slot in range(int(pool_size)):
            fd = _try_lock(_slot_lock_path(root, slot), create=True)
            if fd is not None:
                uid = int(pool_base) + slot
                return UidSlot(uid=uid, gid=uid, slot=slot, _fd=fd)
        if should_abort():
            raise SandboxSpawnError("Processing was aborted while waiting for a sandbox slot.")
        if time.monotonic() >= deadline:
            logger.error(
                "rfp sandbox: no uid slot free after %.0fs; occupancy %s",
                float(wait_timeout_seconds),
                slot_occupancy(root, pool_base=pool_base, pool_size=pool_size),
            )
            raise SlotWaitTimeout("No sandbox uid slot became free within the wait bound.")
        now = time.monotonic()
        if now - last_renew >= RENEW_INTERVAL_SECONDS:
            last_renew = now
            if not renew():
                raise LeaseLost("lease lost while waiting for a sandbox uid slot")
        time.sleep(SLOT_POLL_SECONDS)


def slot_occupancy(scratch_root: Path, *, pool_base: int, pool_size: int) -> list[dict]:
    """`[{slot, uid, busy}]` for GET /status. Never blocks and never creates
    lock files (a slot whose lock file does not exist yet is free)."""
    out: list[dict] = []
    for slot in range(int(pool_size)):
        path = _slot_lock_path(Path(scratch_root), slot)
        if not os.path.lexists(path):
            busy = False
        else:
            try:
                fd = _try_lock(path, create=False)
            except SandboxSpawnError:
                fd = None
            busy = fd is None
            if fd is not None:
                os.close(fd)
        out.append({"slot": slot, "uid": int(pool_base) + slot, "busy": busy})
    return out


# ── Small helpers ────────────────────────────────────────────────────────────


def _san():
    from app.services import rfp_sanitize

    return rfp_sanitize


def default_repo_root() -> Path:
    """The directory containing the `app` package (what BOOTSTRAP puts on the
    child's sys.path)."""
    return Path(protocol.__file__).resolve().parents[2]


def _ensure_subreaper() -> None:
    """Linux only, once per process, best effort: descendants that escape the
    child's session re-parent to us instead of to PID 1 (`sh`, no reaper)."""
    global _SUBREAPER_DONE
    if _SUBREAPER_DONE or sys.platform != "linux":
        return
    _SUBREAPER_DONE = True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except (OSError, AttributeError) as exc:  # pragma: no cover - platform dependent
        logger.warning("rfp sandbox: PR_SET_CHILD_SUBREAPER not applied (%s)", type(exc).__name__)


def _refuse_constant(name: str) -> None:
    raise ValueError(f"refusing JSON constant {name}")


def _parse_line(line: bytes) -> dict:
    """Strict JSON object per line: no NaN/Infinity, bounded nesting, a dict
    with a string `event`."""
    try:
        obj = json.loads(line.decode("utf-8"), parse_constant=_refuse_constant)
    except (ValueError, RecursionError, TypeError, UnicodeDecodeError) as exc:
        raise _Invalid(f"unparseable progress line ({type(exc).__name__})") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("event"), str):
        raise _Invalid("progress line is not an event object")
    return obj


def _clean_value(value: object, depth: int = 0) -> object:
    """A bounded, sanitized copy of a JSON value for provenance fields."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2**63 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _san().sanitize_display(value, max_chars=_CLEAN_MAX_CHARS)
    if depth >= _CLEAN_MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [_clean_value(v, depth + 1) for v in value[:_CLEAN_MAX_ITEMS]]
    if isinstance(value, dict):
        out: dict = {}
        for key, val in list(value.items())[:_CLEAN_MAX_ITEMS]:
            out[_san().sanitize_display(key, max_chars=64)] = _clean_value(val, depth + 1)
        return out
    return None


def _int(obj: dict, key: str, lo: int, hi: int, *, optional: bool = False) -> int | None:
    value = obj.get(key)
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Invalid(f"{key} is not an integer")
    if value < lo or value > hi:
        raise _Invalid(f"{key} out of range")
    return value


def _num(obj: dict, key: str, lo: float, hi: float, *, exclusive_lo: bool = False) -> float:
    """A finite number in [lo, hi]. `exclusive_lo` makes the lower bound
    exclusive, which is what a page side needs: the child's own contract fails
    a non-positive page size, and the images-PDF writer refuses one, so a zero
    must be a protocol violation here rather than a PDF that silently loses
    every part later."""
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Invalid(f"{key} is not a number")
    value = float(value)
    too_low = value <= lo if exclusive_lo else value < lo
    if not math.isfinite(value) or too_low or value > hi:
        raise _Invalid(f"{key} out of range")
    return value


def _bool(obj: dict, key: str, *, optional: bool = False) -> bool | None:
    value = obj.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, bool):
        raise _Invalid(f"{key} is not a boolean")
    return value


def _str(obj: dict, key: str, max_chars: int, *, optional: bool = False) -> str | None:
    value = obj.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _Invalid(f"{key} is not a string")
    return _san().sanitize_display(value, max_chars=max_chars)


def _sha(obj: dict, key: str) -> str:
    value = obj.get(key)
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_HEX_LEN
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise _Invalid(f"{key} is not a sha256 hex digest")
    return value


def _hazards(obj: object, keys: tuple[str, ...], what: str) -> dict[str, int]:
    """A hazard dict whose keys are within `keys` with non-negative ints;
    missing keys read as 0. Anything else is a protocol violation."""
    if obj is None:
        obj = {}
    if not isinstance(obj, dict):
        raise _Invalid(f"{what} hazards is not an object")
    out = dict.fromkeys(keys, 0)
    for key, value in obj.items():
        if key not in keys:
            raise _Invalid(f"{what} hazards has an unknown key")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**31:
            raise _Invalid(f"{what} hazards value is not a count")
        out[key] = value
    return out


def _exited(proc: subprocess.Popen) -> bool:
    """True once the child has terminated. On Linux this uses waitid(WNOWAIT)
    so the pid stays unreaped until `_kill_and_reap` has signalled the group;
    macOS has no waitid, so `poll()` reaps (the group signal still reaches any
    other member, and a pid recycled within one tick is not a realistic race)."""
    if proc.returncode is not None:
        return True
    waitid = getattr(os, "waitid", None)
    if waitid is not None and sys.platform == "linux":
        try:
            return waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return proc.poll() is not None


def _kill_and_reap(proc: subprocess.Popen) -> int:
    """SIGKILL the child's whole session (it was started with
    start_new_session, so pgid == pid), then wait. Swallows the lookups that
    mean "already gone". Returns the exit status (negative = signal)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:  # pragma: no cover - a foreign process in our group id
        logger.warning("rfp sandbox: killpg refused for pid %s", proc.pid)
    rc = proc.wait()
    # Reap any stragglers left in the group (a subreaper parent collects them).
    while True:
        try:
            pid, _ = os.waitpid(-proc.pid, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
    return rc


def _sleep_tick(proc: subprocess.Popen, seconds: float) -> None:
    """Sleep one tick, waking early when the child exits."""
    deadline = time.monotonic() + seconds
    while True:
        if _exited(proc):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


# ── Incremental log tail ─────────────────────────────────────────────────────


class _Tail:
    """Reads complete lines from a child-written log, opened once (relative to
    the out-dir fd, O_NOFOLLOW, must be a regular file) as soon as it exists.
    A line without a newline stays in `carry` for the next tick. Enforces the
    per-line byte cap and the total line cap. `capped` says the last `read()`
    stopped on the per-tick byte window rather than at end of file, which is
    what the post-exit drain loops on (a window can end mid-line and yield no
    lines at all, so "it returned nothing" is not end of file)."""

    def __init__(self, dir_fd: int, name: str, max_lines: int,
                 expect_uid: int | None = None) -> None:
        self.dir_fd = dir_fd
        self.name = name
        self.max_lines = max_lines
        self.expect_uid = expect_uid
        self.fd: int | None = None
        self.carry = b""
        self.lines = 0
        self.capped = False

    def _open(self) -> bool:
        try:
            fd = os.open(self.name, _ARTIFACT_OPEN_FLAGS, dir_fd=self.dir_fd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _Invalid("progress log could not be opened safely") from exc
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(fd)
            raise _Invalid("progress log is not a regular single-link file")
        if self.expect_uid is not None and st.st_uid != self.expect_uid:
            os.close(fd)
            raise _Invalid("progress log is not owned by the sandbox uid")
        self.fd = fd
        return True

    def read(self) -> list[bytes]:
        self.capped = False
        if self.fd is None and not self._open():
            return []
        assert self.fd is not None
        out: list[bytes] = []
        read_bytes = 0
        while True:
            if read_bytes >= _MAX_TAIL_BYTES_PER_TICK:
                self.capped = True
                break
            try:
                chunk = os.read(self.fd, _READ_CHUNK)
            except BlockingIOError:
                break
            if not chunk:
                break
            read_bytes += len(chunk)
            self.carry += chunk
            while True:
                cut = self.carry.find(b"\n")
                if cut < 0:
                    break
                line, self.carry = self.carry[:cut], self.carry[cut + 1 :]
                if len(line) > protocol.MAX_PROGRESS_LINE_BYTES:
                    raise _Invalid("progress line exceeds the line cap")
                self.lines += 1
                if self.lines > self.max_lines:
                    raise _Invalid("progress log exceeds the line cap")
                out.append(line)
            if len(self.carry) > protocol.MAX_PROGRESS_LINE_BYTES:
                raise _Invalid("progress line exceeds the line cap")
        return out

    @property
    def torn(self) -> bool:
        return bool(self.carry)

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)


# ── Out-dir accounting and validation ────────────────────────────────────────


def _dir_usage(dir_fd: int, depth: int, budget: list[int]) -> int:
    """lstat-sum of everything under `dir_fd` (symlinks count their own size,
    nothing is followed). `budget[0]` is the remaining entry allowance; it goes
    negative on overflow."""
    total = 0
    try:
        entries = list(os.scandir(dir_fd))
    except OSError:
        return 0
    for entry in entries:
        budget[0] -= 1
        if budget[0] < 0:
            return total
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        total += st.st_size
        if stat.S_ISDIR(st.st_mode) and depth < _MAX_OUT_DEPTH:
            try:
                sub = os.open(entry.name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
            except OSError:
                continue
            try:
                total += _dir_usage(sub, depth + 1, budget)
            finally:
                os.close(sub)
    return total


def _out_usage(out_fd: int, max_pages: int) -> tuple[int, bool]:
    """(bytes, overflow) for the disk-quota mirror; overflow means the entry cap
    was exceeded, which counts as a quota hit (an output-volume abuse)."""
    budget = [3 * max_pages + _OUT_ENTRY_SLACK]
    total = _dir_usage(out_fd, 0, budget)
    return total, budget[0] < 0


def _split_name(rel: str) -> tuple[str, str]:
    subdir, base = rel.split("/", 1)
    return subdir, base


def _read_artifact(sub_fd: int, base: str, cap: int, expect_uid: int | None) -> bytes:
    """Open one artifact one level below its (already opened) subdir fd, check
    it BEFORE reading, then read it exactly once into a bounded buffer."""
    try:
        fd = os.open(base, _ARTIFACT_OPEN_FLAGS, dir_fd=sub_fd)
    except OSError as exc:
        raise _Invalid("artifact could not be opened safely") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _Invalid("artifact is not a regular file")
        if st.st_nlink != 1:
            raise _Invalid("artifact has extra links")
        if expect_uid is not None and st.st_uid != expect_uid:
            raise _Invalid("artifact is not owned by the sandbox uid")
        if st.st_size > cap:
            raise _Invalid("artifact exceeds the tier byte cap")
        size = st.st_size
        buf = bytearray()
        while len(buf) < size:
            chunk = os.read(fd, min(_READ_CHUNK, size - len(buf)))
            if not chunk:
                raise _Invalid("artifact shrank while being read")
            buf += chunk
        if os.read(fd, 1):
            raise _Invalid("artifact grew while being read")
        return bytes(buf)
    finally:
        os.close(fd)


def _open_subdir(out_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIR_OPEN_FLAGS, dir_fd=out_fd)
    except OSError as exc:
        raise _Invalid(f"output subdirectory {name} is missing or not a directory") from exc


def _unlink_partials(out_fd: int, index: int) -> None:
    """Remove a blamed page's partial artifacts (derived names only) before a
    respawn. Anything odd (a directory where a file should be) is left for
    validation to refuse."""
    for kind_dir, ext in ((protocol.THUMB_DIR, "jpg"), (protocol.FULL_DIR, "jpg"),
                          (protocol.TEXT_DIR, "txt")):
        subdir, base = _split_name(protocol.page_file(kind_dir, index, ext))
        try:
            sub_fd = os.open(subdir, _DIR_OPEN_FLAGS, dir_fd=out_fd)
        except OSError:
            continue
        try:
            os.unlink(base, dir_fd=sub_fd)
        except OSError:
            pass
        finally:
            os.close(sub_fd)


def _read_stderr_tail(out_fd: int, spawn: int) -> str:
    """The last MAX_STDERR_TAIL_BYTES of a spawn's stderr file, sanitized.
    Bounded seek-from-the-end read; never raises.

    The file lives in <out>, which is the child's own 0700 territory, so it
    gets the same gate as every other parent read of a child-reachable path: a
    regular file, exactly one link (the child may not hard-link some other file
    of ours into the name) and still owned by the parent that created it (the
    parent opens this name itself and never chowns it; the child only ever
    writes through the inherited fd 2). Anything else reads as no tail."""
    try:
        fd = os.open(protocol.stderr_file(spawn), _ARTIFACT_OPEN_FLAGS, dir_fd=out_fd)
    except OSError:
        return ""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            return ""
        start = max(0, st.st_size - protocol.MAX_STDERR_TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, protocol.MAX_STDERR_TAIL_BYTES)
    except OSError:
        return ""
    finally:
        os.close(fd)
    try:
        return _san().sanitize_stderr_tail(data)
    except Exception:  # noqa: BLE001 - a diagnostic must never fail the run
        return ""


# ── Run state ────────────────────────────────────────────────────────────────


@dataclass
class _Run:
    """Everything one `run_sandbox` call accumulates across spawns."""

    work: Path
    out: Path
    out_fd: int
    source: Path
    limits: SandboxLimits
    repo_root: Path
    python: str
    uid_slot: UidSlot | None
    should_abort: Callable[[], bool]
    on_progress: Callable[[str, int, int | None], None]
    renew: Callable[[], bool]
    open_timeout: float
    page_stall: float
    started: float
    file_deadline: float
    max_pages_remaining: int
    page_count: int | None = None
    # The verify spawn is monitored against its own deadline (a long render
    # must not leave the decode pass zero wall clock and throw the file away).
    spawn_deadline: float | None = None
    verify_spawn: int | None = None
    pages: dict[int, dict] = field(default_factory=dict)      # index -> cleaned page event
    blamed: dict[int, str] = field(default_factory=dict)      # index -> parent failure code
    start: dict | None = None
    ready: dict | None = None
    document: dict | None = None
    end: dict | None = None
    bounds: list[str] = field(default_factory=list)
    peak_rss_kb: int | None = None
    spawns: int = 0
    restarts: int = 0
    last_progress: float = float("-inf")
    last_renew: float = 0.0
    phase: str = protocol.PHASE_SANDBOX

    def bound(self, name: str) -> None:
        if name not in self.bounds:
            self.bounds.append(name)

    @property
    def pages_done(self) -> int:
        return len(self.pages) + len(self.blamed)

    def uncovered(self, skip: set[int]) -> list[int]:
        assert self.page_count is not None
        return [
            i for i in range(self.page_count)
            if i not in self.pages and i not in self.blamed and i not in skip
        ]

    def remaining_budget(self) -> int:
        return int(self.file_deadline - time.monotonic())

    @property
    def expect_uid(self) -> int | None:
        """The uid every child-written path must carry, or None when no uid
        switch is in force (local development)."""
        return self.uid_slot.uid if self.uid_slot is not None else None

    def deadline(self) -> float:
        return self.spawn_deadline if self.spawn_deadline is not None else self.file_deadline


# ── Event sinks ──────────────────────────────────────────────────────────────


class _ProcessSink:
    """Strict state machine over one process-mode spawn's progress events."""

    phase = protocol.PHASE_SANDBOX

    def __init__(self, run: _Run, spawn: int, skip: set[int], spawn_at: float) -> None:
        self.run = run
        self.spawn = spawn
        self.skip = skip
        self.spawn_at = spawn_at
        self.start: dict | None = None
        self.ready: dict | None = None
        self.document: dict | None = None
        self.reject: dict | None = None
        self.end: dict | None = None
        self.dangling: int | None = None
        self.closed = False
        self.ready_at: float | None = None
        self.first_page_start_at: float | None = None
        self.last_complete_at: float | None = None
        self.limits_memory_applied = False

    @property
    def pages_done(self) -> int:
        return self.run.pages_done

    def on_event(self, obj: dict, now: float) -> None:
        ev = obj["event"]
        if ev not in protocol.EVENTS:
            raise _Invalid("unknown progress event")
        if self.closed:
            raise _Invalid("event after the terminal event")
        if ev == protocol.EVENT_START:
            self._on_start(obj)
        elif ev == protocol.EVENT_READY:
            self._on_ready(obj, now)
        elif ev == protocol.EVENT_HEARTBEAT:
            if self.start is None:
                raise _Invalid("heartbeat before start")
        elif ev == protocol.EVENT_DOCUMENT:
            self._on_document(obj)
        elif ev == protocol.EVENT_REJECT:
            self._on_reject(obj, now)
        elif ev == protocol.EVENT_PAGE_START:
            self._on_page_start(obj, now)
        elif ev == protocol.EVENT_PAGE:
            self._on_page(obj, now)
        elif ev == protocol.EVENT_END:
            self._on_end(obj, now)

    def _on_start(self, obj: dict) -> None:
        if self.start is not None:
            raise _Invalid("duplicate start")
        cleaned = _clean_value(obj)
        assert isinstance(cleaned, dict)
        applied = obj.get("limits_applied")
        self.limits_memory_applied = isinstance(applied, dict) and applied.get("memory") is True
        self.start = cleaned
        self.run.start = cleaned

    def _on_ready(self, obj: dict, now: float) -> None:
        if self.start is None or self.ready is not None:
            raise _Invalid("ready out of order")
        cleaned = _clean_value(obj)
        assert isinstance(cleaned, dict)
        self.ready = cleaned
        self.run.ready = cleaned
        self.ready_at = now

    def _on_document(self, obj: dict) -> None:
        if self.ready is None or self.document is not None or self.reject is not None:
            raise _Invalid("document out of order")
        limits = self.run.limits
        # page_count is validated BEFORE any per-page structure exists.
        page_count = _int(obj, "page_count", 1, limits.max_pages)
        assert page_count is not None
        if self.run.page_count is not None and page_count != self.run.page_count:
            raise _Stop(_KILL_UNREADABLE, "respawn reported a different page count")
        metadata_in = obj.get("metadata")
        if metadata_in is not None and not isinstance(metadata_in, dict):
            raise _Invalid("document metadata is not an object")
        metadata: dict = {}
        for key in protocol.METADATA_KEYS:
            value = (metadata_in or {}).get(key)
            if value is None:
                metadata[key] = None
            elif isinstance(value, str):
                metadata[key] = _san().sanitize_display(
                    value, max_chars=protocol.METADATA_MAX_CHARS
                )
            else:
                raise _Invalid("document metadata value is not a string")
        document = {
            "page_count": page_count,
            "pdf_version": _str(obj, "pdf_version", 32, optional=True),
            "owner_restricted": _bool(obj, "owner_restricted", optional=True),
            "security_handler_revision": _int(
                obj, "security_handler_revision", 0, 2**16, optional=True
            ),
            "form_type": _str(obj, "form_type", 32, optional=True),
            "metadata": metadata,
            "hazards": _hazards(obj.get("hazards"), protocol.DOC_HAZARD_KEYS, "document"),
        }
        self.document = document
        self.run.document = document
        self.run.page_count = page_count
        if page_count > self.run.max_pages_remaining:
            # Recorded first (the caller stores page_count), then killed.
            raise _Stop(_KILL_PAGE_BUDGET)

    def _on_reject(self, obj: dict, now: float) -> None:
        if self.ready is None or self.document is not None:
            raise _Invalid("reject out of order")
        code = obj.get("code")
        if not isinstance(code, str) or code not in protocol.CHILD_REJECT_CODES:
            raise _Invalid("reject code is not a child reject code")
        self.reject = {
            "event": protocol.EVENT_REJECT,
            "code": code,
            "detail": _str(obj, "detail", protocol.DETAIL_MAX_CHARS, optional=True),
        }
        self.closed = True
        self.last_complete_at = now

    def _index(self, obj: dict) -> int:
        assert self.run.page_count is not None
        index = _int(obj, "index", 0, self.run.page_count - 1)
        assert index is not None
        return index

    def _on_page_start(self, obj: dict, now: float) -> None:
        if self.document is None:
            raise _Invalid("page_start before document")
        if self.dangling is not None:
            raise _Invalid("page_start while a page is open")
        index = self._index(obj)
        if index in self.skip or index in self.run.pages or index in self.run.blamed:
            raise _Invalid("page_start for a skipped or finished index")
        self.dangling = index
        if self.first_page_start_at is None:
            self.first_page_start_at = now
        self.last_complete_at = now

    def _on_page(self, obj: dict, now: float) -> None:
        if self.document is None:
            raise _Invalid("page before document")
        index = self._index(obj)
        if self.dangling != index:
            raise _Invalid("page without a matching page_start")
        status = obj.get("status")
        if status == protocol.PAGE_OK:
            cleaned = self._clean_ok_page(obj, index)
        elif status == protocol.PAGE_FAILED:
            code = obj.get("code")
            if not isinstance(code, str) or code not in protocol.CHILD_PAGE_FAIL_CODES:
                raise _Invalid("page failure code is not a child code")
            cleaned = {
                "index": index,
                "status": protocol.PAGE_FAILED,
                "code": code,
                "detail": _str(obj, "detail", protocol.DETAIL_MAX_CHARS, optional=True),
            }
        else:
            raise _Invalid("page status is neither ok nor failed")
        self.run.pages[index] = cleaned
        self.dangling = None
        self.last_complete_at = now

    def _clean_ok_page(self, obj: dict, index: int) -> dict:
        limits = self.run.limits
        tier = obj.get("tier")
        if tier not in _TIERS:
            raise _Invalid("page tier is unknown")
        rotation = _int(obj, "rotation", 0, 359)
        if rotation not in _ROTATIONS:
            raise _Invalid("page rotation is not a multiple of 90")
        full_side = limits.full_long_side if tier == "full" else limits.full_small_long_side
        thumb = self._clean_image(
            obj.get("thumb"), protocol.page_file(protocol.THUMB_DIR, index, "jpg"),
            limits.thumb_long_side, protocol.MAX_THUMB_BYTES, "thumb",
        )
        full = self._clean_image(
            obj.get("full"), protocol.page_file(protocol.FULL_DIR, index, "jpg"),
            full_side, protocol.MAX_FULL_BYTES, "full",
        )
        text_in = obj.get("text")
        if not isinstance(text_in, dict):
            raise _Invalid("page text is not an object")
        if text_in.get("file") != protocol.page_file(protocol.TEXT_DIR, index, "txt"):
            raise _Invalid("page text file name is not the derived name")
        text = {
            "file": text_in["file"],
            "chars": _int(text_in, "chars", 0, limits.max_text_chars_per_page),
            "truncated": _bool(text_in, "truncated"),
            "sha256": _sha(text_in, "sha256"),
            "hazards": _hazards(text_in.get("hazards"), protocol.TEXT_HAZARD_KEYS, "text"),
        }
        return {
            "index": index,
            "status": protocol.PAGE_OK,
            "width_pt": _num(obj, "width_pt", 0.0, float(limits.max_page_side_pt),
                             exclusive_lo=True),
            "height_pt": _num(obj, "height_pt", 0.0, float(limits.max_page_side_pt),
                              exclusive_lo=True),
            "rotation": rotation,
            "tier": tier,
            "thumb": thumb,
            "full": full,
            "text": text,
            "hazards": _hazards(obj.get("hazards"), protocol.PAGE_HAZARD_KEYS, "page"),
            "render_ms": _int(obj, "render_ms", 0, 2**40),
        }

    @staticmethod
    def _clean_image(obj: object, expected: str, long_side: int, cap: int, what: str) -> dict:
        if not isinstance(obj, dict):
            raise _Invalid(f"page {what} is not an object")
        if obj.get("file") != expected:
            raise _Invalid(f"page {what} file name is not the derived name")
        w = _int(obj, "w", 1, _JPEG_MAX_SIDE)
        h = _int(obj, "h", 1, _JPEG_MAX_SIDE)
        assert w is not None and h is not None
        if max(w, h) > long_side:
            raise _Invalid(f"page {what} exceeds the tier long side")
        return {
            "file": expected,
            "w": w,
            "h": h,
            "bytes": _int(obj, "bytes", 1, cap),
            "sha256": _sha(obj, "sha256"),
        }

    def _on_end(self, obj: dict, now: float) -> None:
        if self.document is None:
            raise _Invalid("end before document")
        if self.dangling is not None:
            raise _Invalid("end while a page is open")
        aborted = obj.get("aborted")
        if aborted is not None and aborted not in (
            protocol.ABORT_DISK_QUOTA, protocol.ABORT_DEADLINE
        ):
            raise _Invalid("end.aborted is not a known value")
        end = {
            "pages_ok": _int(obj, "pages_ok", 0, self.run.limits.max_pages),
            "pages_failed": _int(obj, "pages_failed", 0, self.run.limits.max_pages),
            "elapsed_ms": _int(obj, "elapsed_ms", 0, 2**40),
            "peak_rss_kb": _int(obj, "peak_rss_kb", 0, 2**40, optional=True),
            "output_bytes": _int(obj, "output_bytes", 0, 2**50, optional=True),
            "aborted": aborted,
        }
        self.end = end
        self.run.end = end
        peak = end["peak_rss_kb"]
        if peak is not None:
            self.run.peak_rss_kb = max(self.run.peak_rss_kb or 0, peak)
        self.closed = True
        self.last_complete_at = now

    def check_timeouts(self, now: float) -> str | None:
        run = self.run
        if self.ready is None:
            if now - self.spawn_at > run.open_timeout:
                return _KILL_READY_TIMEOUT
            return None
        if self.closed:
            # end/reject written but the process will not exit: its verdict is
            # already on disk, so this kill changes nothing but frees the slot.
            assert self.last_complete_at is not None
            if now - self.last_complete_at > run.page_stall:
                return _KILL_LINGER
            return None
        if self.document is None:
            assert self.ready_at is not None
            if now - self.ready_at > run.open_timeout:
                return _KILL_OPEN_TIMEOUT
            return None
        if self.first_page_start_at is not None:
            assert self.last_complete_at is not None
            if now - self.last_complete_at > run.page_stall:
                return _KILL_PAGE_STALL
        return None

    def finish(self, rc: int, torn: bool) -> None:
        """Post-exit strictness: a clean exit must leave no torn line and must
        end on a terminal event when it wrote one."""
        if rc == protocol.EXIT_OK and torn:
            raise _Invalid("torn trailing line after a clean exit")


class _VerifySink:
    """Events of the `--verify` spawn: one `verified` line per listed file."""

    phase = protocol.PHASE_VERIFY

    def __init__(self, run: _Run, expected: dict[str, tuple[int, int]], spawn_at: float) -> None:
        self.run = run
        self.expected = expected
        self.results: dict[str, bool] = {}
        self.last_event_at = spawn_at

    @property
    def pages_done(self) -> int:
        return self.run.pages_done

    def on_event(self, obj: dict, now: float) -> None:
        if obj["event"] != protocol.EVENT_VERIFIED:
            raise _Invalid("unknown verify event")
        name = obj.get("file")
        if not isinstance(name, str) or name not in self.expected:
            raise _Invalid("verify line names a file that was not listed")
        if name in self.results:
            raise _Invalid("duplicate verify line")
        ok = _bool(obj, "ok")
        w = _int(obj, "w", 0, _JPEG_MAX_SIDE, optional=True)
        h = _int(obj, "h", 0, _JPEG_MAX_SIDE, optional=True)
        self.results[name] = bool(ok) and (w, h) == self.expected[name]
        self.last_event_at = now

    def check_timeouts(self, now: float) -> str | None:
        if now - self.last_event_at > self.run.page_stall:
            return _KILL_LINGER if len(self.results) == len(self.expected) else _KILL_PAGE_STALL
        return None

    def finish(self, rc: int, torn: bool) -> None:
        if rc == protocol.EXIT_OK and torn:
            raise _Invalid("torn trailing verify line after a clean exit")


# ── Spawning and monitoring ──────────────────────────────────────────────────


def _write_parent_file(path: Path, data: bytes) -> None:
    """A parent-authored input for the child (limits, skip list, verify list):
    created O_EXCL, world-readable so a switched uid can read it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    except OSError as exc:
        raise SandboxSpawnError("A sandbox input file could not be created.") from exc
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _launch(run: _Run, spawn: int, args: list[str]) -> subprocess.Popen:
    """Spawn exactly the protocol's command line for spawn `spawn`."""
    _ensure_subreaper()
    argv = [
        run.python, *protocol.INTERPRETER_FLAGS, "-c", protocol.BOOTSTRAP, str(run.repo_root),
        *args,
    ]
    tmp = run.out / protocol.TMP_DIR
    env = {"LANG": _CHILD_LANG, "TMPDIR": str(tmp), "HOME": str(tmp)}
    try:
        err_fd = os.open(
            protocol.stderr_file(spawn),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o644,
            dir_fd=run.out_fd,
        )
    except OSError as exc:
        # <out> belongs to the child (0700, sandbox uid), so a name already
        # sitting where the parent derives one is the FILE's fault, not the
        # deployment's: it becomes failed/invalid_output for this file instead
        # of a SandboxSpawnError that would abandon every remaining file.
        raise _Invalid("the spawn stderr file could not be created in the output directory") from exc
    kwargs: dict = {
        "cwd": str(run.out),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": err_fd,
        "close_fds": True,
        "start_new_session": True,
    }
    if run.uid_slot is not None:
        kwargs.update(user=run.uid_slot.uid, group=run.uid_slot.gid, extra_groups=[])
    try:
        return _popen(argv, **kwargs)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.error("rfp sandbox: spawn failed: %s", type(exc).__name__)
        raise SandboxSpawnError("The sandbox process could not be started.") from exc
    finally:
        os.close(err_fd)


def _monitor(run: _Run, proc: subprocess.Popen, tail: _Tail, sink) -> tuple[str | None, int]:
    """The monitor loop for one spawn. Returns (kill_reason, exit_status);
    kill_reason is None when the child exited on its own. The child is always
    dead and reaped on return, whatever happened (including exceptions from
    the callbacks, which propagate after the kill)."""
    kill_reason: str | None = None
    note = ""
    try:
        while True:
            exited = _exited(proc)
            now = time.monotonic()
            try:
                for line in tail.read():
                    sink.on_event(_parse_line(line), now)
            except _Invalid as exc:
                kill_reason, note = _KILL_INVALID, str(exc)
                break
            except _Stop as exc:
                kill_reason, note = exc.reason, exc.note
                break
            usage, overflow = _out_usage(run.out_fd, run.limits.max_pages)
            if overflow or usage > run.limits.max_output_total_bytes:
                kill_reason = _KILL_DISK_QUOTA
                break
            timed_out = sink.check_timeouts(now)
            if timed_out is not None:
                kill_reason = timed_out
                break
            if now > run.deadline():
                kill_reason = _KILL_FILE_TIMEOUT
                break
            if run.should_abort():
                kill_reason = _KILL_ABORT
                break
            if now - run.last_progress >= PROGRESS_INTERVAL_SECONDS:
                run.last_progress = now
                run.on_progress(sink.phase, sink.pages_done, proc.pid)
            if now - run.last_renew >= RENEW_INTERVAL_SECONDS:
                run.last_renew = now
                if not run.renew():
                    kill_reason = _KILL_LEASE
                    break
            if exited:
                break
            _sleep_tick(proc, TICK_SECONDS)
    finally:
        rc = _kill_and_reap(proc)
    if kill_reason == _KILL_LEASE:
        raise LeaseLost("lease lost while the sandbox child was running")
    if kill_reason != _KILL_INVALID:
        # Re-read the log to EOF now that the child is dead. One read() stops
        # at the per-tick byte window, so this loops until the log is drained;
        # otherwise a padded tail would push the terminal event out of reach
        # and an innocent page would be blamed. Lines that arrive here are
        # still held to the same strictness (the line caps bound the work).
        try:
            now = time.monotonic()
            while True:
                for line in tail.read():
                    sink.on_event(_parse_line(line), now)
                if not tail.capped:
                    break
            if kill_reason is None:
                sink.finish(rc, tail.torn)
        except _Invalid as exc:
            kill_reason, note = _KILL_INVALID, str(exc)
        except _Stop as exc:
            kill_reason, note = exc.reason, exc.note
    if kill_reason is not None:
        logger.info(
            "rfp sandbox: spawn %s stopped by the parent (%s%s)",
            run.spawns - 1, kill_reason, f": {note}" if note else "",
        )
    return kill_reason, rc


@dataclass
class _Verdict:
    status: str
    code: str | None


def _process_spawn(run: _Run, spawn: int, skip: set[int]) -> tuple[_ProcessSink, str | None, int]:
    """Write this spawn's inputs, launch, monitor to the end. Returns the sink
    plus the monitor's (kill_reason, rc)."""
    limits = run.limits.with_deadline(run.remaining_budget())
    limits_path = run.work / _LIMITS_TEMPLATE.format(spawn=spawn)
    _write_parent_file(limits_path, limits.to_json().encode("utf-8"))
    args = [
        "--spawn", str(spawn), "--input", str(run.source), "--out", str(run.out),
        "--limits", str(limits_path),
    ]
    if skip:
        skip_path = run.work / _SKIP_TEMPLATE.format(spawn=spawn)
        _write_parent_file(skip_path, "".join(f"{i}\n" for i in sorted(skip)).encode("ascii"))
        args += ["--skip-file", str(skip_path)]
    max_lines = 2 * limits.max_pages + protocol.MAX_PROGRESS_EXTRA_LINES
    tail = _Tail(run.out_fd, protocol.progress_file(spawn), max_lines, run.expect_uid)
    try:
        proc = _launch(run, spawn, args)
        run.spawns += 1
        sink = _ProcessSink(run, spawn, skip, time.monotonic())
        kill_reason, rc = _monitor(run, proc, tail, sink)
    finally:
        tail.close()
    return sink, kill_reason, rc


def _blame(run: _Run, sink: _ProcessSink, skip: set[int], code_dangling: str,
           code_other: str) -> int | None:
    """Pick the index to blame for a crash or stall per the protocol docstring:
    the dangling page_start, else the first index not covered by anything."""
    if sink.dangling is not None:
        run.blamed[sink.dangling] = code_dangling
        return sink.dangling
    uncovered = run.uncovered(skip)
    if not uncovered:
        return None
    run.blamed[uncovered[0]] = code_other
    return uncovered[0]


def _memory_or_crash(sink: _ProcessSink, rc: int) -> str:
    """Memory attribution: the parent did not kill, limits_applied.memory was
    true, and the child died by SIGKILL/SIGABRT or a non-zero exit code."""
    by_signal = rc < 0 and -rc in (signal.SIGKILL, signal.SIGABRT)
    if sink.limits_memory_applied and (by_signal or rc > 0):
        return protocol.PAGE_FAIL_MEMORY
    return protocol.PAGE_FAIL_CRASH


def _attribute(run: _Run, sink: _ProcessSink, kill_reason: str | None, rc: int,
               spawn: int, skip: set[int], *, max_restarts: int,
               restart_ratio: float) -> tuple[str, _Verdict | None]:
    """Decide what one finished spawn means: ("final", verdict), ("respawn",
    None) with the skip list already grown, or ("complete", None)."""
    if kill_reason == _KILL_LINGER:
        kill_reason = None   # the verdict is whatever the child wrote before idling
    if kill_reason == _KILL_ABORT:
        return "final", _Verdict(STATUS_FAILED, protocol.FAIL_INTERRUPTED)
    if kill_reason == _KILL_INVALID:
        return "final", _Verdict(STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT)
    if kill_reason == _KILL_DISK_QUOTA:
        run.bound(protocol.BOUND_DISK_QUOTA)
        return "final", _Verdict(STATUS_FAILED, protocol.FAIL_RESOURCE_LIMIT)
    if kill_reason == _KILL_FILE_TIMEOUT:
        run.bound(protocol.BOUND_FILE_TIMEOUT)
        return "final", _Verdict(STATUS_FAILED, protocol.FAIL_RESOURCE_LIMIT)
    if kill_reason == _KILL_OPEN_TIMEOUT:
        run.bound(protocol.BOUND_OPEN_TIMEOUT)
        return "final", _Verdict(STATUS_FAILED, protocol.FAIL_RESOURCE_LIMIT)
    if kill_reason == _KILL_READY_TIMEOUT:
        run.bound(protocol.BOUND_OPEN_TIMEOUT)
        return "final", _Verdict(STATUS_FAILED, protocol.FAIL_SPAWN)
    if kill_reason == _KILL_PAGE_BUDGET:
        run.bound(protocol.BOUND_RUN_PAGE_BUDGET)
        return "final", _Verdict(STATUS_REJECTED, protocol.REJECT_RUN_PAGE_BUDGET)
    if kill_reason == _KILL_UNREADABLE:
        return "final", _Verdict(STATUS_REJECTED, protocol.REJECT_UNREADABLE)
    if kill_reason == _KILL_PAGE_STALL:
        run.bound(protocol.BOUND_PAGE_STALL)
        index = _blame(run, sink, skip, protocol.PAGE_FAIL_STALL, protocol.PAGE_FAIL_STALL)
        return _respawn_or_loop(run, index, max_restarts, restart_ratio)
    # The child exited on its own.
    if rc == protocol.EXIT_BAD_ARGS or sink.start is None or sink.ready is None:
        return "final", _Verdict(STATUS_FAILED, protocol.FAIL_SPAWN)
    if sink.reject is not None:
        run.document = sink.reject
        code = sink.reject["code"] if spawn == 0 else protocol.REJECT_UNREADABLE
        return "final", _Verdict(STATUS_REJECTED, code)
    if sink.document is None:
        return "final", _Verdict(STATUS_REJECTED, protocol.REJECT_UNREADABLE)
    if sink.end is not None:
        aborted = sink.end.get("aborted")
        if aborted is not None:
            run.bound(
                protocol.BOUND_DISK_QUOTA
                if aborted == protocol.ABORT_DISK_QUOTA
                else protocol.BOUND_DEADLINE
            )
            for index in run.uncovered(skip):
                run.blamed[index] = protocol.PAGE_FAIL_ABORTED
            return "final", _Verdict(STATUS_FAILED, protocol.FAIL_RESOURCE_LIMIT)
        if run.uncovered(skip):
            return "final", _Verdict(STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT)
        return "complete", None
    # Died after document without an end.
    index = _blame(run, sink, skip, _memory_or_crash(sink, rc), protocol.PAGE_FAIL_CRASH)
    return _respawn_or_loop(run, index, max_restarts, restart_ratio)


def _respawn_or_loop(run: _Run, index: int | None, max_restarts: int,
                     restart_ratio: float) -> tuple[str, _Verdict | None]:
    if index is None:
        # Nothing new to skip: a respawn would only repeat the crash.
        return "final", _Verdict(STATUS_REJECTED, protocol.REJECT_CRASH_LOOP)
    assert run.page_count is not None
    cap = max(int(max_restarts), math.ceil(run.page_count * float(restart_ratio)))
    if run.restarts + 1 > cap:
        run.bound(protocol.BOUND_RESTARTS)
        return "final", _Verdict(STATUS_REJECTED, protocol.REJECT_CRASH_LOOP)
    _unlink_partials(run.out_fd, index)
    run.restarts += 1
    return "respawn", None


# ── Validation ───────────────────────────────────────────────────────────────


def _walk_out(run: _Run, ok_indices: set[int]) -> dict[str, int]:
    """Refuse anything in <out> that the protocol does not name, and open the
    artifact subdirectories. Returns {subdir: fd} for thumb/full/text (the
    caller closes them)."""
    assert run.page_count is not None
    known_files = set()
    for spawn in range(run.spawns):
        known_files.add(protocol.progress_file(spawn))
        known_files.add(protocol.stderr_file(spawn))
    known_dirs = {protocol.THUMB_DIR, protocol.FULL_DIR, protocol.TEXT_DIR, protocol.TMP_DIR}
    try:
        entries = list(os.scandir(run.out_fd))
    except OSError as exc:
        raise _Invalid("output directory could not be listed") from exc
    for entry in entries:
        st = entry.stat(follow_symlinks=False)
        if entry.name in known_files:
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise _Invalid("a log entry is not a regular single-link file")
        elif entry.name in known_dirs:
            if not stat.S_ISDIR(st.st_mode):
                raise _Invalid("an output subdirectory is not a directory")
        else:
            raise _Invalid("unexpected entry in the output directory")
    derived: dict[str, set[str]] = {
        protocol.THUMB_DIR: set(), protocol.FULL_DIR: set(), protocol.TEXT_DIR: set(),
    }
    for index in range(run.page_count):
        for kind_dir, ext in ((protocol.THUMB_DIR, "jpg"), (protocol.FULL_DIR, "jpg"),
                              (protocol.TEXT_DIR, "txt")):
            derived[kind_dir].add(_split_name(protocol.page_file(kind_dir, index, ext))[1])
    fds: dict[str, int] = {}
    try:
        for subdir in derived:
            try:
                fd = os.open(subdir, _DIR_OPEN_FLAGS, dir_fd=run.out_fd)
            except FileNotFoundError:
                if ok_indices:
                    raise _Invalid(f"output subdirectory {subdir} is missing") from None
                continue
            except OSError as exc:
                raise _Invalid(f"output subdirectory {subdir} could not be opened") from exc
            fds[subdir] = fd
            for entry in os.scandir(fd):
                st = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    raise _Invalid("an artifact entry is not a regular file")
                if entry.name not in derived[subdir]:
                    raise _Invalid("an artifact name is not a derived page name")
    except BaseException:
        for fd in fds.values():
            os.close(fd)
        raise
    return fds


def _validate_pages(run: _Run, *, keep_bytes: bool,
                    max_text_bytes: int) -> dict[int, PageOutput]:
    """Section 2 "Output integrity": read every ok page's artifacts once, check
    them against the log, and build PageOutputs for every index. Raises
    _Invalid on any mismatch.

    `keep_bytes` False drops each image buffer as soon as it has been hashed
    and marker-walked (the sha256 recorded in `thumb_meta`/`full_meta` is what
    `_deliver_pages` re-checks when it hands the page to the caller's sink),
    so the parent never holds more than one artifact at a time. True keeps the
    validated bytes on the PageOutput, which is what a caller without a sink
    gets.

    `max_text_bytes` is the per-file text budget, spent in index order in
    UTF-8 bytes: the page that crosses it is the last one to carry text, every
    later page gets the empty string, and `BOUND_TEXT_BYTES` is recorded. The
    per-page character cap alone would let 3000 pages x 200,000 characters sit
    in the parent at once, and a UTF-8 byte is never narrower than CPython's
    per-character width, so this bounds residency as well as the text.json the
    caller will write (which applies the same budget in the same order, so no
    page it could have used is dropped here)."""
    assert run.page_count is not None
    san = _san()
    limits = run.limits
    expect_uid = run.expect_uid
    ok_indices = {i for i, p in run.pages.items() if p["status"] == protocol.PAGE_OK}
    for index in range(run.page_count):
        if (index in run.pages) == (index in run.blamed):
            raise _Invalid("a page index is missing or covered twice")
    if any(i < 0 or i >= run.page_count for i in list(run.pages) + list(run.blamed)):
        raise _Invalid("a page index is out of range")
    fds = _walk_out(run, ok_indices)
    text_cap = 4 * limits.max_text_chars_per_page + 16
    text_budget = max(0, int(max_text_bytes))
    text_spent = 0
    outputs: dict[int, PageOutput] = {}
    try:
        for index in sorted(ok_indices):
            page = run.pages[index]
            images: dict[str, bytes] = {}
            for kind in ("thumb", "full"):
                meta = page[kind]
                subdir, base = _split_name(meta["file"])
                cap = protocol.MAX_THUMB_BYTES if kind == "thumb" else protocol.MAX_FULL_BYTES
                buf = _read_artifact(fds[subdir], base, cap, expect_uid)
                if len(buf) != meta["bytes"] or hashlib.sha256(buf).hexdigest() != meta["sha256"]:
                    raise _Invalid(f"page {kind} size or digest mismatch")
                try:
                    w, h = san.jpeg_dimensions(buf)
                except ValueError as exc:
                    raise _Invalid(f"page {kind} is not a well-formed JPEG") from exc
                if (w, h) != (meta["w"], meta["h"]):
                    raise _Invalid(f"page {kind} dimensions differ from the log")
                if keep_bytes:
                    images[kind] = buf
                buf = b""
            text_meta = page["text"]
            subdir, base = _split_name(text_meta["file"])
            raw = _read_artifact(fds[subdir], base, text_cap, expect_uid)
            if hashlib.sha256(raw).hexdigest() != text_meta["sha256"]:
                raise _Invalid("page text digest mismatch")
            clean, hazards = san.sanitize_text(raw.decode("utf-8", errors="replace"))
            # The byte cap above allows 4 bytes per code point, so the
            # character cap is enforced here or a page could carry four times
            # the configured maximum. The log's own `chars` is range-checked,
            # so what the parent read may not exceed it either.
            if (len(clean) > limits.max_text_chars_per_page
                    or len(clean) > int(text_meta["chars"])):
                raise _Invalid("page text is longer than the per-page cap or the log")
            merged = {
                key: max(int(hazards.get(key, 0)), int(text_meta["hazards"].get(key, 0)))
                for key in protocol.TEXT_HAZARD_KEYS
            }
            # The per-file text budget, spent in index order. `text_chars`
            # stays the true count and `text_truncated` stays the child's own
            # per-page flag; a page past the budget is simply "characters
            # recorded, no body", which is what text.json would have written.
            text_chars = len(clean)
            if text_spent >= text_budget:
                clean = ""
                run.bound(protocol.BOUND_TEXT_BYTES)
            else:
                text_spent += len(clean.encode("utf-8"))
            outputs[index] = PageOutput(
                index=index, status=protocol.PAGE_OK, code=None, detail=None,
                width_pt=page["width_pt"], height_pt=page["height_pt"],
                rotation=page["rotation"], tier=page["tier"],
                thumb=images.get("thumb"), full=images.get("full"),
                thumb_meta={k: page["thumb"][k] for k in ("w", "h", "bytes", "sha256")},
                full_meta={k: page["full"][k] for k in ("w", "h", "bytes", "sha256")},
                text=clean, text_chars=text_chars, text_truncated=bool(text_meta["truncated"]),
                text_hazards=merged, hazards=dict(page["hazards"]),
                render_ms=page["render_ms"],
            )
    finally:
        for fd in fds.values():
            os.close(fd)
    for index, page in run.pages.items():
        if page["status"] == protocol.PAGE_FAILED:
            outputs[index] = _failed_page(index, page["code"], page.get("detail"))
    for index, code in run.blamed.items():
        outputs[index] = _failed_page(index, code, None)
    return outputs


def _failed_page(index: int, code: str, detail: str | None) -> PageOutput:
    return PageOutput(
        index=index, status=protocol.PAGE_FAILED, code=code,
        detail=detail if detail else PAGE_DETAIL_MESSAGES.get(code, ""),
        width_pt=None, height_pt=None, rotation=None, tier=None,
        thumb=None, full=None, thumb_meta=None, full_meta=None,
        text=None, text_chars=0, text_truncated=False,
        text_hazards=dict.fromkeys(protocol.TEXT_HAZARD_KEYS, 0),
        hazards=dict.fromkeys(protocol.PAGE_HAZARD_KEYS, 0), render_ms=None,
    )


def _reread_artifact(run: _Run, fds: dict[str, int], kind_dir: str, index: int,
                     meta: dict, cap: int) -> bytes:
    """Re-open one already-validated artifact under the validation discipline
    (its subdir fd, O_NOFOLLOW, fstat before the read, one bounded read) and
    check it against the size and digest recorded at validation."""
    subdir, base = _split_name(protocol.page_file(kind_dir, index, "jpg"))
    if subdir not in fds:
        fds[subdir] = _open_subdir(run.out_fd, subdir)
    buf = _read_artifact(fds[subdir], base, cap, run.expect_uid)
    if len(buf) != meta["bytes"] or hashlib.sha256(buf).hexdigest() != meta["sha256"]:
        raise _Invalid(f"page {kind_dir} changed between validation and delivery")
    return buf


def _deliver_pages(run: _Run, pages: list[PageOutput],
                   page_sink: Callable[[PageOutput, bytes, bytes], None]) -> None:
    """Hand every ok page's images to `page_sink` in index order, one page
    resident at a time: each artifact is re-opened and re-hashed against the
    digest recorded at validation, so the window between the two reads is
    closed even though the child is already dead. A mismatch anywhere raises
    _Invalid (the whole file becomes failed/invalid_output) before that page
    reaches the sink and no further page is delivered. Anything the sink
    itself raises propagates unchanged; a sink must not raise this module's
    private _Invalid."""
    fds: dict[str, int] = {}
    try:
        for page in pages:
            if page.status != protocol.PAGE_OK:
                continue
            assert page.thumb_meta is not None and page.full_meta is not None
            thumb_bytes = _reread_artifact(
                run, fds, protocol.THUMB_DIR, page.index, page.thumb_meta,
                protocol.MAX_THUMB_BYTES,
            )
            full_bytes = _reread_artifact(
                run, fds, protocol.FULL_DIR, page.index, page.full_meta,
                protocol.MAX_FULL_BYTES,
            )
            page_sink(page, thumb_bytes, full_bytes)
            thumb_bytes = full_bytes = b""   # the runner keeps no reference
    finally:
        for fd in fds.values():
            os.close(fd)


def _verify_spawn(run: _Run, outputs: dict[int, PageOutput]) -> _Verdict | None:
    """The --verify pass over every ok page's images. Pages it does not
    confirm become failed/verify (image bytes dropped). Returns a verdict only
    for a spawn-level problem (EXIT_BAD_ARGS, invalid verify log, cancel, the
    disk quota).

    The pass gets its own wall clock: `max(remaining file budget, 2 x the page
    stall)`, monitored instead of the file deadline. A long render must not
    leave the decode pass no time at all, and running out of THAT budget is
    not a whole-file verdict either: the pages verify did not reach fall
    through to failed/verify and the gap rule decides, exactly as a verify
    stall does. The page-stall check still bounds a hung verify child."""
    ok_indices = sorted(i for i, p in outputs.items() if p.status == protocol.PAGE_OK)
    if not ok_indices:
        return None
    expected: dict[str, tuple[int, int]] = {}
    for index in ok_indices:
        page = outputs[index]
        assert page.thumb_meta is not None and page.full_meta is not None
        expected[protocol.page_file(protocol.THUMB_DIR, index, "jpg")] = (
            page.thumb_meta["w"], page.thumb_meta["h"],
        )
        expected[protocol.page_file(protocol.FULL_DIR, index, "jpg")] = (
            page.full_meta["w"], page.full_meta["h"],
        )
    spawn = run.spawns
    budget = max(run.remaining_budget(), int(2 * run.page_stall))
    limits = run.limits.with_deadline(budget)
    limits_path = run.work / _LIMITS_TEMPLATE.format(spawn=spawn)
    _write_parent_file(limits_path, limits.to_json().encode("utf-8"))
    list_path = run.work / _VERIFY_LIST_NAME
    _write_parent_file(list_path, "".join(f"{name}\n" for name in expected).encode("ascii"))
    args = ["--verify", "--out", str(run.out), "--limits", str(limits_path),
            "--list-file", str(list_path)]
    run.phase = protocol.PHASE_VERIFY
    tail = _Tail(run.out_fd, protocol.VERIFY_FILE,
                 len(expected) + protocol.MAX_PROGRESS_EXTRA_LINES, run.expect_uid)
    try:
        run.spawn_deadline = time.monotonic() + float(budget)
        proc = _launch(run, spawn, args)
        run.spawns += 1
        run.verify_spawn = spawn
        sink = _VerifySink(run, expected, time.monotonic())
        kill_reason, rc = _monitor(run, proc, tail, sink)
    finally:
        run.spawn_deadline = None
        tail.close()
    if kill_reason == _KILL_ABORT:
        return _Verdict(STATUS_FAILED, protocol.FAIL_INTERRUPTED)
    if kill_reason == _KILL_INVALID:
        return _Verdict(STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT)
    if kill_reason == _KILL_DISK_QUOTA:
        run.bound(protocol.BOUND_DISK_QUOTA)
        return _Verdict(STATUS_FAILED, protocol.FAIL_RESOURCE_LIMIT)
    if kill_reason == _KILL_FILE_TIMEOUT:
        # Verify ran out of its own budget: per page, not per file.
        run.bound(protocol.BOUND_FILE_TIMEOUT)
    if kill_reason == _KILL_PAGE_STALL:
        run.bound(protocol.BOUND_PAGE_STALL)
    if kill_reason is None and rc == protocol.EXIT_BAD_ARGS:
        return _Verdict(STATUS_FAILED, protocol.FAIL_SPAWN)
    if kill_reason == _KILL_LEASE:  # pragma: no cover - _monitor raises before returning it
        raise LeaseLost("lease lost during verification")
    for index in ok_indices:
        page = outputs[index]
        names = (
            protocol.page_file(protocol.THUMB_DIR, index, "jpg"),
            protocol.page_file(protocol.FULL_DIR, index, "jpg"),
        )
        if all(sink.results.get(name) is True for name in names):
            continue
        page.status = protocol.PAGE_FAILED
        page.code = protocol.PAGE_FAIL_VERIFY
        page.detail = PAGE_DETAIL_MESSAGES[protocol.PAGE_FAIL_VERIFY]
        page.thumb = None
        page.full = None
    return None


def _stderr_tail(run: _Run) -> str:
    """The manifest's stderr tail: the last PROCESS-mode spawn that wrote
    anything AND the verify spawn's own tail, labeled and joined under the
    protocol's byte bound.

    Both are kept because the verify child writes a preamble line on every
    run: taking only the highest-numbered spawn that wrote anything would make
    that preamble the tail of every complete file and hide the traceback of a
    process spawn that crashed, which is the one diagnostic an operator needs
    when `restarts > 0`."""
    parts: list[str] = []
    for spawn_no in range(run.spawns - 1, -1, -1):
        if spawn_no == run.verify_spawn:
            continue
        tail = _read_stderr_tail(run.out_fd, spawn_no)
        if tail:
            parts.append(f"spawn {spawn_no}:\n{tail}")
            break
    if run.verify_spawn is not None:
        tail = _read_stderr_tail(run.out_fd, run.verify_spawn)
        if tail:
            parts.append(f"verify:\n{tail}")
    joined = "\n".join(parts)
    if len(joined) > protocol.MAX_STDERR_TAIL_BYTES:
        joined = joined[-protocol.MAX_STDERR_TAIL_BYTES:]
    return joined


# ── Scratch layout ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Scratch:
    work: Path
    source: Path
    out: Path
    out_fd: int


def _prepare_scratch(pdf_path: Path, scratch_root: Path, uid_slot: UidSlot | None) -> _Scratch:
    """Section 3.1: the per-file work directory (0o711), the source copy
    (0o644, a hard link when the filesystem allows), the out dir and out/tmp
    (0o700, owned by the sandbox uid when switching), and the out-dir fd that
    every later access is relative to."""
    root = Path(scratch_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=root))
    except OSError as exc:
        raise SandboxSpawnError("The sandbox scratch directory could not be created.") from exc
    try:
        os.chmod(work, 0o711)
        source = work / _SOURCE_NAME
        try:
            os.link(pdf_path, source)
        except OSError:
            shutil.copyfile(pdf_path, source)
        os.chmod(source, 0o644)
        out = work / _OUT_NAME
        tmp = out / protocol.TMP_DIR
        os.mkdir(out, 0o700)
        os.mkdir(tmp, 0o700)
        if uid_slot is not None:
            _chown(out, uid_slot.uid, uid_slot.gid)
            _chown(tmp, uid_slot.uid, uid_slot.gid)
        os.chmod(out, 0o700)
        os.chmod(tmp, 0o700)
        out_fd = os.open(out, _DIR_OPEN_FLAGS)
    except OSError as exc:
        cleanup_scratch(work)
        raise SandboxSpawnError("The sandbox scratch layout could not be prepared.") from exc
    return _Scratch(work=work, source=source, out=out, out_fd=out_fd)


def cleanup_scratch(path: Path | str) -> None:
    """Remove a per-file scratch tree as root-safely as possible: every
    permission error is answered with a chmod of the parent and the entry and
    one retry. Never raises."""
    target = Path(path)
    if not os.path.lexists(target):
        return
    if os.path.islink(target):
        try:
            os.unlink(target)
        except OSError:
            pass
        return

    def _swallow(_func, _target, _exc):
        return None

    def _retry(func, failed, _exc):
        try:
            os.chmod(os.path.dirname(failed), 0o700)
            if os.path.isdir(failed) and not os.path.islink(failed):
                os.chmod(failed, 0o700)
                shutil.rmtree(failed, onexc=_swallow)
            else:
                os.unlink(failed)
        except OSError:
            pass

    try:
        shutil.rmtree(target, onexc=_retry)
    except OSError:
        pass


# ── Entry point ──────────────────────────────────────────────────────────────


def run_sandbox(
    pdf_path: Path,
    *,
    limits: SandboxLimits,
    scratch_root: Path,
    open_timeout_seconds: int,
    page_stall_seconds: int,
    file_timeout_seconds: int,
    max_restarts: int,
    max_pages_remaining: int,
    uid_slot: UidSlot | None,
    should_abort: Callable[[], bool],
    on_progress: Callable[[str, int, int | None], None],
    renew: Callable[[], bool],
    repo_root: Path | None = None,
    python_executable: str | None = None,
    restart_ratio: float = 0.05,
    min_failed_pages_allowed: int = 2,
    failed_page_ratio: float = 0.05,
    max_text_bytes_per_file: int = DEFAULT_MAX_TEXT_BYTES_PER_FILE,
    page_sink: Callable[[PageOutput, bytes, bytes], None] | None = None,
) -> SandboxResult:
    """Process one PDF in the sandbox and return a fully validated result.

    Creates its own work directory under `scratch_root` (removed before
    returning), links or copies `pdf_path` into it, spawns the child under
    `limits` (deadline = the remaining file budget per spawn), monitors it per
    spec 3.2, respawns per the protocol docstring, validates every output per
    spec section 2, runs the `--verify` spawn, and applies the gap rule.
    `on_progress(phase, pages_done, child_pid)` is called at most every 2 s;
    `renew()` every 30 s (False kills the child and raises LeaseLost);
    `should_abort()` every tick (True kills the child; result
    failed/interrupted). Raises SandboxSpawnError for parent-side faults only.

    `page_sink` is how a caller takes the image bytes without the parent ever
    holding more than one page of them. When it is given, validation drops
    each buffer after hashing it and `PageOutput.thumb`/`.full` stay None; then
    ONLY if the file is going to be complete, every ok page (post-verify, in
    index order) is re-opened, re-hashed against the digest recorded at
    validation and passed to `page_sink(page, thumb_bytes, full_bytes)` while
    the out directory still exists. A hash mismatch makes the whole file
    failed/invalid_output with no further sink calls; an exception from the
    sink propagates out of this function unchanged (the work directory is
    still removed, and the child is long dead by then). Without a sink the
    validated bytes ride out on the PageOutputs as before.

    `max_text_bytes_per_file` is the same budget the caller applies when it
    writes text.json (`rfp_ingest_max_text_bytes_per_file`), enforced here in
    index order so the parent never holds more than that much page text plus
    one page; pages past it come back with `text == ""` and the result records
    `text_bytes` in `bounds_hit`.
    """
    started = time.monotonic()
    scratch = _prepare_scratch(Path(pdf_path), Path(scratch_root), uid_slot)
    run = _Run(
        work=scratch.work, out=scratch.out, out_fd=scratch.out_fd, source=scratch.source,
        limits=limits,
        repo_root=Path(repo_root) if repo_root is not None else default_repo_root(),
        python=python_executable or sys.executable,
        uid_slot=uid_slot, should_abort=should_abort, on_progress=on_progress, renew=renew,
        open_timeout=float(open_timeout_seconds), page_stall=float(page_stall_seconds),
        started=started, file_deadline=started + float(file_timeout_seconds),
        max_pages_remaining=int(max_pages_remaining), last_renew=started,
    )
    try:
        verdict = _drive(run, max_restarts=max_restarts, restart_ratio=restart_ratio,
                         min_failed_pages_allowed=min_failed_pages_allowed,
                         failed_page_ratio=failed_page_ratio,
                         max_text_bytes=int(max_text_bytes_per_file),
                         page_sink=page_sink)
        return verdict
    finally:
        try:
            os.close(run.out_fd)
        except OSError:
            pass
        cleanup_scratch(run.work)


def _drive(run: _Run, *, max_restarts: int, restart_ratio: float,
           min_failed_pages_allowed: int, failed_page_ratio: float,
           max_text_bytes: int,
           page_sink: Callable[[PageOutput, bytes, bytes], None] | None) -> SandboxResult:
    """Spawn, attribute, validate, verify and settle one file.

    Two rules settle a validated file. The gap rule allows
    max(min_failed_pages_allowed, ceil(ratio x page_count)) failed pages, and
    a file with NO ok page is rejected regardless of that allowance: the
    absolute floor is there for a stray bad page in a long document, not to
    let a one- or two-page file with nothing rendered settle as usable. Page
    text is bounded across the file by `max_text_bytes` (see
    `_validate_pages`)."""
    skip: set[int] = set()
    spawn = 0
    pages: list[PageOutput] = []
    verdict: _Verdict | None = None
    while True:
        try:
            sink, kill_reason, rc = _process_spawn(run, spawn, skip)
        except _Invalid as exc:
            # Only one thing raises out here: a parent-derived name inside
            # <out> that the child planted first. That is this file's fault.
            logger.warning("rfp sandbox: spawn %s could not be prepared: %s", spawn, exc)
            verdict = _Verdict(STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT)
            break
        outcome, verdict = _attribute(
            run, sink, kill_reason, rc, spawn, skip,
            max_restarts=max_restarts, restart_ratio=restart_ratio,
        )
        if outcome == "respawn":
            skip = set(run.blamed) | set(run.pages)
            spawn += 1
            continue
        break
    if verdict is None:
        try:
            outputs = _validate_pages(run, keep_bytes=page_sink is None,
                                      max_text_bytes=max_text_bytes)
        except _Invalid as exc:
            logger.warning("rfp sandbox: output validation failed: %s", exc)
            verdict = _Verdict(STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT)
            outputs = {}
        if verdict is None:
            try:
                verdict = _verify_spawn(run, outputs)
            except _Invalid as exc:
                logger.warning("rfp sandbox: the verify spawn could not be prepared: %s", exc)
                verdict = _Verdict(STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT)
        if verdict is None:
            assert run.page_count is not None
            pages = [outputs[i] for i in sorted(outputs)]
            failed = sum(1 for p in pages if p.status == protocol.PAGE_FAILED)
            pages_ok = sum(1 for p in pages if p.status == protocol.PAGE_OK)
            allowed = max(
                int(min_failed_pages_allowed),
                math.ceil(float(failed_page_ratio) * run.page_count),
            )
            # `pages_ok` is counted over the validated pages, not page_count,
            # so a skipped index cannot stand in for a rendered one. A file
            # with nothing rendered is rejected even when the absolute floor
            # would have covered every one of its failures.
            if failed > allowed or pages_ok == 0:
                run.bound(protocol.BOUND_FAILED_PAGE_RATIO)
                verdict = _Verdict(STATUS_REJECTED, protocol.REJECT_TOO_MANY_FAILED_PAGES)
                for page in pages:
                    page.thumb = None
                    page.full = None
            elif page_sink is not None:
                # The file is complete: hand the bytes over while <out> still
                # exists. Anything the sink raises is the caller's to handle.
                try:
                    _deliver_pages(run, pages, page_sink)
                except _Invalid as exc:
                    logger.warning("rfp sandbox: page delivery failed validation: %s", exc)
                    verdict = _Verdict(STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT)
                    pages = []
        elif verdict.code == protocol.FAIL_INVALID_OUTPUT:
            pages = []
        else:
            # A verify-spawn-level verdict (cancel, bad args, the disk quota):
            # like any pre-validation verdict, only the failed placeholders are
            # returned, never an ok page the caller cannot have the bytes for.
            pages = [outputs[i] for i in sorted(outputs)
                     if outputs[i].status == protocol.PAGE_FAILED]
            for page in pages:
                page.thumb = None
                page.full = None
    elif verdict.code != protocol.FAIL_INVALID_OUTPUT:
        # A verdict reached before validation: the failed placeholders known so
        # far are returned for the audit trail, never any ok page or bytes.
        failed = {i: (p["code"], p.get("detail")) for i, p in run.pages.items()
                  if p["status"] == protocol.PAGE_FAILED}
        failed.update({i: (code, None) for i, code in run.blamed.items()})
        pages = [_failed_page(i, *failed[i]) for i in sorted(failed)]
    hazards = dict.fromkeys(protocol.DOC_HAZARD_KEYS, 0)
    if run.document is not None and "hazards" in run.document:
        hazards.update(run.document["hazards"])
    page_hazards = dict.fromkeys(protocol.PAGE_HAZARD_KEYS, 0)
    for page in run.pages.values():
        for key, value in (page.get("hazards") or {}).items():
            page_hazards[key] = page_hazards.get(key, 0) + value
    hazards.update(page_hazards)
    stderr_tail = _stderr_tail(run)
    status = verdict.status if verdict is not None else STATUS_COMPLETE
    code = verdict.code if verdict is not None else None
    if status != STATUS_COMPLETE:
        logger.info("rfp sandbox: file finished %s/%s after %s spawn(s)", status, code, run.spawns)
    return SandboxResult(
        status=status,
        code=code,
        detail=protocol.VERDICT_MESSAGES.get(code) if code else None,
        start=run.start,
        ready=run.ready,
        document=run.document,
        end=run.end,
        pages=pages,
        hazards=hazards,
        bounds_hit=list(run.bounds),
        restarts=run.restarts,
        elapsed_ms=int((time.monotonic() - run.started) * 1000),
        uid=run.uid_slot.uid if run.uid_slot else None,
        gid=run.uid_slot.gid if run.uid_slot else None,
        slot=run.uid_slot.slot if run.uid_slot else None,
        stderr_tail=stderr_tail,
        spawns=run.spawns,
        peak_rss_kb=run.peak_rss_kb,
    )


# ── Self-test fixture ────────────────────────────────────────────────────────


def embedded_selftest_pdf() -> bytes:
    """A hand-written, valid one-page letter-size PDF with one Helvetica text
    line, for `rfp_ingest.self_test()`. Offsets in the xref are computed here
    so the file is byte-exact valid (no pypdf at runtime, no fixtures dir in
    the image)."""
    content = b"BT /F1 24 Tf 72 700 Td (RFP ingestion sandbox self-test) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref_at,
    )
    return bytes(out)
