"""Durable step spool and the per-job step log (spec 3.3, 10.2).

Write-ahead: every step row is appended (and fsynced) to
`spool/<job_id>_<epoch>.jsonl` BEFORE it is posted, and an ack line is
appended once BDR accepted it. A hard power loss at any line therefore loses
nothing: after restart the unacked rows are replayed in order as late steps,
idempotently by `idem_key` (BDR skips duplicates and accepts an older epoch the
worker really held).

Line formats (one JSON object per line):
    {"s": <StepIn dict>}          a step row
    {"a": [<idem_key>, ...]}      rows BDR accepted (or refused for good)
    {"d": 1}                      the job ended in this process
A torn last line (power loss mid-write) is ignored on read and a newline is
written before the next append. The spool never contains secrets: rows are
scrubbed with LiveSecrets before they are written.
"""

import asyncio
import itertools
import json
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from .atomicfile import fsync_dir
from .client import (
    BdrApiError,
    BdrClient,
    BdrUnreachable,
    Lease,
    PendingApproval,
    RetryPolicy,
    StaleBoot,
    SyncBdrClient,
    WorkerRevoked,
    WorkerUnauthorized,
)
from .protocol import (
    MAX_STEPS_PER_POST,
    STEP_PAYLOAD_MAX_BYTES,
    STEP_SUMMARY_MAX,
    StepIn,
    worker_step_kind_allowed,
)
from .redact import LiveSecrets, strip_query

log = logging.getLogger("bdr_agent.spool")

_NAME_RE = re.compile(r"^([0-9a-f-]{36})_(\d{1,12})\.jsonl$")
FLUSH_RETRY = RetryPolicy(max_attempts=2)


class JobSpool:
    """One job epoch's spool file. Thread-safe (the heartbeat thread writes
    loop-stall rows into it)."""

    def __init__(self, path: Path, job_id: UUID, epoch: int):
        self.path = Path(path)
        self.job_id = job_id
        self.epoch = epoch
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._acked: set[str] = set()
        self._done = False
        self._loaded = False

    @property
    def lease(self) -> Lease:
        return Lease(self.job_id, self.epoch)

    # Disk ----------------------------------------------------------------------

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue   # torn write
            if not isinstance(rec, dict):
                continue
            if isinstance(rec.get("s"), dict) and rec["s"].get("idem_key"):
                self._rows.setdefault(str(rec["s"]["idem_key"]), rec["s"])
            elif isinstance(rec.get("a"), list):
                self._acked.update(str(k) for k in rec["a"])
            elif rec.get("d"):
                self._done = True

    def _write_locked(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        payload = b"".join(
            json.dumps(r, separators=(",", ":"), default=str).encode("utf-8") + b"\n" for r in records)
        created = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab+") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    payload = b"\n" + payload   # close a torn line first
                fh.seek(0, os.SEEK_END)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if created:
            fsync_dir(self.path.parent)

    # API ---------------------------------------------------------------------------

    def append(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._load_locked()
            fresh = [r for r in rows if r["idem_key"] not in self._rows]
            self._write_locked([{"s": r} for r in fresh])
            for r in fresh:
                self._rows[r["idem_key"]] = r

    def ack(self, idem_keys: list[str]) -> None:
        with self._lock:
            self._load_locked()
            keys = [k for k in idem_keys if k not in self._acked]
            if keys:
                self._write_locked([{"a": keys}])
                self._acked.update(keys)

    def mark_done(self) -> None:
        with self._lock:
            self._load_locked()
            if not self._done:
                self._write_locked([{"d": 1}])
                self._done = True

    def unacked(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load_locked()
            return [r for k, r in self._rows.items() if k not in self._acked]

    def counts(self) -> tuple[int, int]:
        with self._lock:
            self._load_locked()
            return len(self._rows), len(self._acked & set(self._rows))

    @property
    def done(self) -> bool:
        with self._lock:
            self._load_locked()
            return self._done

    def remove_if_settled(self) -> bool:
        """Delete the file once the job ended and every row was acked."""
        with self._lock:
            self._load_locked()
            if not self._done or any(k not in self._acked for k in self._rows):
                return False
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            fsync_dir(self.path.parent)
            return True


class Spool:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._open: dict[tuple[UUID, int], JobSpool] = {}

    def for_job(self, job_id: UUID, epoch: int) -> JobSpool:
        key = (UUID(str(job_id)), int(epoch))
        with self._lock:
            sp = self._open.get(key)
            if sp is None:
                sp = JobSpool(self.directory / f"{key[0]}_{key[1]}.jsonl", key[0], key[1])
                self._open[key] = sp
            return sp

    def forget(self, job_id: UUID, epoch: int) -> None:
        with self._lock:
            self._open.pop((UUID(str(job_id)), int(epoch)), None)

    def on_disk(self) -> list[JobSpool]:
        """Every spool file present, oldest first (by name order within mtime)."""
        found: list[tuple[float, JobSpool]] = []
        for entry in self.directory.iterdir():
            m = _NAME_RE.match(entry.name)
            if not m:
                continue
            try:
                mtime = entry.stat().st_mtime
            except FileNotFoundError:
                continue
            found.append((mtime, self.for_job(UUID(m.group(1)), int(m.group(2)))))
        found.sort(key=lambda t: (t[0], t[1].path.name))
        return [sp for _, sp in found]


@dataclass
class FlushResult:
    posted: int = 0
    dropped: int = 0
    pending: int = 0
    unreachable: bool = False
    fatal: BdrApiError | None = None   # process-level refusal; rows kept


# Refusals about this process or worker, not about the rows: keep the rows.
_PROCESS_ERRORS = (StaleBoot, WorkerRevoked, WorkerUnauthorized, PendingApproval)


def _batches(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [rows[i:i + MAX_STEPS_PER_POST] for i in range(0, len(rows), MAX_STEPS_PER_POST)]


def _settle(sp: JobSpool, batch: list[dict[str, Any]], exc: BdrApiError | None, result: FlushResult) -> None:
    keys = [r["idem_key"] for r in batch]
    sp.ack(keys)
    if exc is None:
        result.posted += len(batch)
    else:
        # A definitive refusal (no claim step for that epoch, revoked worker,
        # invalid row) will never succeed: drop the rows so replay terminates.
        result.dropped += len(batch)
        log.warning("spool rows dropped for job %s epoch %s: %s", sp.job_id, sp.epoch, exc.code or exc.status)


async def flush_spool(sp: JobSpool, client: BdrClient, *, retry: RetryPolicy = FLUSH_RETRY) -> FlushResult:
    result = FlushResult()
    for batch in _batches(sp.unacked()):
        try:
            await client.append_steps(sp.lease, batch, retry=retry)
        except BdrUnreachable:
            result.unreachable = True
            break
        except _PROCESS_ERRORS as exc:
            result.fatal = exc
            break
        except BdrApiError as exc:
            _settle(sp, batch, exc, result)
            continue
        _settle(sp, batch, None, result)
    result.pending = len(sp.unacked())
    return result


def flush_spool_sync(sp: JobSpool, client: SyncBdrClient, *, retry: RetryPolicy = FLUSH_RETRY) -> FlushResult:
    result = FlushResult()
    for batch in _batches(sp.unacked()):
        try:
            client.append_steps(sp.lease, batch, retry=retry)
        except BdrUnreachable:
            result.unreachable = True
            break
        except _PROCESS_ERRORS as exc:
            result.fatal = exc
            break
        except BdrApiError as exc:
            _settle(sp, batch, exc, result)
            continue
        _settle(sp, batch, None, result)
    result.pending = len(sp.unacked())
    return result


async def replay_spool(spool: Spool, client: BdrClient, *,
                       exclude: Callable[[UUID, int], bool] | None = None) -> FlushResult:
    """Upload every leftover spool file from earlier processes as late steps.
    Files of jobs this process is running (`exclude`) are skipped. A fully
    settled file is deleted; an unreachable BDR stops the pass (try later)."""
    total = FlushResult()
    for sp in spool.on_disk():
        if exclude is not None and exclude(sp.job_id, sp.epoch):
            continue
        res = await flush_spool(sp, client)
        total.posted += res.posted
        total.dropped += res.dropped
        total.pending += res.pending
        if res.unreachable or res.fatal is not None:
            total.unreachable = res.unreachable
            total.fatal = res.fatal
            break
        # A leftover file belongs to a dead process: its job ended there.
        sp.mark_done()
        if sp.remove_if_settled():
            spool.forget(sp.job_id, sp.epoch)
    return total


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _payload_bytes(args: Any, result: Any) -> int:
    return len(json.dumps([args, result], separators=(",", ":"), sort_keys=True, default=str).encode("utf-8"))


class StepLog:
    """Builds, scrubs, validates and spools step rows for one job epoch.
    `log()` is synchronous and thread-safe; posting happens in `flush()`."""

    def __init__(self, spool: JobSpool, secrets: LiveSecrets, *, prefix: str = "w",
                 on_logged: Callable[[int], None] | None = None):
        self.spool = spool
        self.secrets = secrets
        self.prefix = prefix
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._count = 0
        self._on_logged = on_logged
        self._flush_lock = asyncio.Lock()

    @property
    def lease(self) -> Lease:
        return self.spool.lease

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def next_key(self, prefix: str | None = None) -> str:
        with self._lock:
            n = next(self._counter)
        return f"{self.spool.job_id}:{self.spool.epoch}:{prefix or self.prefix}{n}"

    def log(self, kind: str, summary: str, *, actor: str = "worker", tool_name: str | None = None,
            args: dict[str, Any] | None = None, result: dict[str, Any] | None = None,
            url_before: str | None = None, url_after: str | None = None,
            screenshot_artifact_id: UUID | None = None, turn_no: int | None = None,
            ok: bool | None = None, duration_ms: int | None = None, prefix: str | None = None) -> str:
        if not worker_step_kind_allowed(kind):
            raise ValueError(f"step kind {kind!r} is not a worker kind")
        args = self.secrets.scrub(args) if args is not None else None
        result = self.secrets.scrub(result) if result is not None else None
        if _payload_bytes(args, result) > STEP_PAYLOAD_MAX_BYTES:
            size = _payload_bytes(args, result)
            args = {"truncated": True} if args is not None else None
            result = {"truncated": True, "bytes": size}
        row = {
            "idem_key": self.next_key(prefix),
            "kind": kind,
            "actor": actor,
            "summary": self.secrets.scrub_text(summary)[:STEP_SUMMARY_MAX],
            "tool_name": tool_name,
            "args": args,
            "result": result,
            "url_before": strip_query(self.secrets.scrub_text(url_before)) if url_before else None,
            "url_after": strip_query(self.secrets.scrub_text(url_after)) if url_after else None,
            "screenshot_artifact_id": str(screenshot_artifact_id) if screenshot_artifact_id else None,
            "turn_no": turn_no,
            "ok": ok,
            "duration_ms": duration_ms,
            "at": _now_iso(),
        }
        row = StepIn.model_validate(row).model_dump(mode="json")
        self.spool.append([row])
        with self._lock:
            self._count += 1
            count = self._count
        if self._on_logged is not None:
            self._on_logged(count)
        return row["idem_key"]

    def pending(self) -> int:
        return len(self.spool.unacked())

    async def flush(self, client: BdrClient, *, retry: RetryPolicy = FLUSH_RETRY) -> FlushResult:
        async with self._flush_lock:
            return await flush_spool(self.spool, client, retry=retry)

    def flush_sync(self, client: SyncBdrClient, *, retry: RetryPolicy = FLUSH_RETRY) -> FlushResult:
        return flush_spool_sync(self.spool, client, retry=retry)
