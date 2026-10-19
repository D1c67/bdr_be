"""Heartbeat thread and loop-stall watchdog (spec 3.2 heartbeat, 3.3).

A dedicated thread with its own SyncBdrClient beats every 15 s, so a blocked
or busy asyncio loop never stops lease renewal. Each beat:
  - lists every job this process holds with its epoch; BDR renews by epoch
    even when the lease already expired, as long as nobody reclaimed it;
  - reports `lost` and `cancel` job ids to the supervisor;
  - releases orphans (leases BDR thinks this boot holds but the process does
    not run) with /requeue {interrupted}, only after two consecutive beats so
    a job claimed between snapshot and response is never requeued;
  - passes help_updates, config_version changes and drain to the supervisor;
  - turns stale_boot / revoked / unauthorized into a fatal signal.

The watchdog posts a probe callback into the asyncio loop every second. A
probe not run within 5 s writes one `browser.loop_stall` step per running job
(spooled and posted from this thread). A loop stalled past
`stall_exit_seconds` exits the process (code 1) so the service restarts and
/hello requeues the leases; otherwise the thread would renew them forever.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from .client import (
    BdrApiError,
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
    HEARTBEAT_ACTIVE_SECONDS_MAX,
    HEARTBEAT_SECONDS,
    HeartbeatJob,
    HeartbeatRequest,
    HeartbeatResponse,
    HelpUpdate,
    RequeueRequest,
)

log = logging.getLogger("bdr_agent.heartbeat")

STALL_SECONDS = 5.0
STALL_EXIT_SECONDS = 300.0
BEAT_RETRY = RetryPolicy(max_attempts=2, base_s=1.0, cap_s=3.0)
ORPHAN_RETRY = RetryPolicy(max_attempts=2, base_s=1.0, cap_s=3.0)
EXIT_LOOP_STALLED = 1


# Shared job state ----------------------------------------------------------------


@dataclass
class TrackedJob:
    job_id: UUID
    epoch: int
    agent_type: str
    phase: str | None = None
    step_count: int = 0
    active: bool = True                 # False while waiting for help
    lease_expires_at: datetime | None = None
    steps: Any = None                   # spool.StepLog, for loop-stall rows
    _accounted: float = 0.0             # monotonic time active seconds are counted up to
    _banked: float = 0.0                # active seconds not yet reported


class JobTracker:
    """Thread-safe registry of the jobs this process holds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[UUID, TrackedJob] = {}
        self._claims_in_flight = 0

    def register(self, job_id: UUID, epoch: int, agent_type: str) -> TrackedJob:
        with self._lock:
            tj = TrackedJob(job_id=job_id, epoch=epoch, agent_type=agent_type, _accounted=time.monotonic())
            self._jobs[job_id] = tj
            return tj

    def unregister(self, job_id: UUID) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def attach_steps(self, job_id: UUID, steps: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].steps = steps

    def set_phase(self, job_id: UUID, phase: str | None) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].phase = (phase or "")[:40] or None

    def set_steps(self, job_id: UUID, count: int) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].step_count = count

    def set_active(self, job_id: UUID, active: bool) -> None:
        with self._lock:
            tj = self._jobs.get(job_id)
            if tj is not None and tj.active != active:
                now = time.monotonic()
                if tj.active:
                    tj._banked += now - tj._accounted   # bank active time before pausing
                tj.active = active
                tj._accounted = now

    def renewed(self, job_id: UUID, lease_expires_at: datetime) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].lease_expires_at = lease_expires_at

    def holds(self, job_id: UUID, epoch: int | None = None) -> bool:
        with self._lock:
            tj = self._jobs.get(job_id)
            return tj is not None and (epoch is None or tj.epoch == epoch)

    def get(self, job_id: UUID) -> TrackedJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[TrackedJob]:
        with self._lock:
            return list(self._jobs.values())

    def count_by_type(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for tj in self._jobs.values():
                out[tj.agent_type] = out.get(tj.agent_type, 0) + 1
            return out

    def claim_started(self) -> None:
        with self._lock:
            self._claims_in_flight += 1

    def claim_finished(self) -> None:
        with self._lock:
            self._claims_in_flight = max(0, self._claims_in_flight - 1)

    def claims_in_flight(self) -> bool:
        with self._lock:
            return self._claims_in_flight > 0

    def beat_snapshot(self) -> list[HeartbeatJob]:
        now = time.monotonic()
        out: list[HeartbeatJob] = []
        with self._lock:
            for tj in self._jobs.values():
                if tj.active:
                    tj._banked += max(0.0, now - tj._accounted)
                    tj._accounted = now
                # BDR clamps a beat at 30 s; carry the rest to the next beat.
                delta = int(min(HEARTBEAT_ACTIVE_SECONDS_MAX, tj._banked))
                tj._banked -= delta
                out.append(HeartbeatJob(job_id=tj.job_id, epoch=tj.epoch, phase=tj.phase,
                                        step_count=tj.step_count, active_seconds_delta=delta))
        return out


class HeartbeatSink(Protocol):
    """Implemented by the supervisor. Called from the heartbeat thread; every
    method must be thread-safe (the supervisor marshals into its loop)."""

    def on_lost(self, job_id: UUID) -> None: ...

    def on_cancel(self, job_id: UUID) -> None: ...

    def on_help_update(self, update: HelpUpdate) -> None: ...

    def on_config_version(self, version: int) -> None: ...

    def on_drain(self, draining: bool) -> None: ...

    def on_fatal(self, exc: BdrApiError) -> None: ...


# The thread -----------------------------------------------------------------------


class HeartbeatThread(threading.Thread):
    def __init__(self, client: SyncBdrClient, tracker: JobTracker, sink: HeartbeatSink, *,
                 boot_id: UUID, config_version: Callable[[], int],
                 slots_free: Callable[[], dict[str, int]], loop: Any = None,
                 interval: float = HEARTBEAT_SECONDS, probe_interval: float = 1.0,
                 stall_seconds: float = STALL_SECONDS, stall_exit_seconds: float = STALL_EXIT_SECONDS,
                 exit_fn: Callable[[int], None] = os._exit):
        super().__init__(name="bdr-heartbeat", daemon=True)
        self.client = client
        self.tracker = tracker
        self.sink = sink
        self.boot_id = boot_id
        self._config_version = config_version
        self._slots_free = slots_free
        self.loop = loop
        self.interval = interval
        self.probe_interval = probe_interval
        self.stall_seconds = stall_seconds
        self.stall_exit_seconds = stall_exit_seconds
        self._exit_fn = exit_fn
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._orphan_seen: set[tuple[UUID, int]] = set()
        self._probe_lock = threading.Lock()
        self._probe_sent_at: float | None = None
        self._probe_reported = False
        self._max_lag = 0.0
        self._exiting = False
        self.beats_ok = 0
        self.beats_failed = 0
        self.last_response: HeartbeatResponse | None = None

    # Control ---------------------------------------------------------------------

    def stop(self, timeout: float | None = 5.0) -> None:
        self._halt.set()
        self._wake.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    def beat_now(self) -> None:
        """Ask for an immediate beat (e.g. right after a claim)."""
        self._wake.set()

    def run(self) -> None:
        next_beat = time.monotonic()
        while not self._halt.is_set():
            now = time.monotonic()
            if now >= next_beat or self._wake.is_set():
                self._wake.clear()
                try:
                    self.beat_once()
                except Exception:   # never let the thread die
                    log.exception("heartbeat beat failed")
                next_beat = time.monotonic() + self.interval
            try:
                self.check_loop()
            except Exception:
                log.exception("loop watchdog failed")
            wait = max(0.0, min(self.probe_interval, next_beat - time.monotonic()))
            self._wake.wait(wait)

    # One beat -----------------------------------------------------------------------

    def _take_lag_ms(self) -> int:
        with self._probe_lock:
            lag = self._max_lag
            if self._probe_sent_at is not None:
                lag = max(lag, time.monotonic() - self._probe_sent_at)
            self._max_lag = 0.0
        return int(lag * 1000)

    def beat_once(self) -> HeartbeatResponse | None:
        req = HeartbeatRequest(
            boot_id=self.boot_id,
            config_version=int(self._config_version()),
            slots_free={k: max(0, min(8, int(v))) for k, v in self._slots_free().items()},
            loop_lag_ms=self._take_lag_ms(),
            jobs=self.tracker.beat_snapshot(),
        )
        try:
            resp: HeartbeatResponse = self.client.heartbeat(req, retry=BEAT_RETRY)
        except (StaleBoot, WorkerRevoked, WorkerUnauthorized) as exc:
            self.beats_failed += 1
            log.error("heartbeat refused: %s", exc.code)
            self.sink.on_fatal(exc)
            self._halt.set()
            return None
        except PendingApproval:
            self.beats_failed += 1
            return None
        except (BdrUnreachable, BdrApiError) as exc:
            self.beats_failed += 1
            log.warning("heartbeat failed: %s", exc)
            return None
        self.beats_ok += 1
        self.last_response = resp
        self._apply(req, resp)
        return resp

    def _apply(self, req: HeartbeatRequest, resp: HeartbeatResponse) -> None:
        for r in resp.renewed:
            self.tracker.renewed(r.job_id, r.lease_expires_at)
        for job_id in resp.lost:
            self.sink.on_lost(job_id)
        for job_id in resp.cancel:
            self.sink.on_cancel(job_id)
        for upd in resp.help_updates:
            self.sink.on_help_update(upd)
        self._release_orphans(resp)
        if resp.config_version != req.config_version:
            self.sink.on_config_version(resp.config_version)
        self.sink.on_drain(bool(resp.drain))

    def _release_orphans(self, resp: HeartbeatResponse) -> None:
        seen_now: set[tuple[UUID, int]] = set()
        claiming = self.tracker.claims_in_flight()
        for o in resp.orphans:
            key = (o.job_id, o.epoch)
            if self.tracker.holds(o.job_id):
                continue
            seen_now.add(key)
            if claiming or key not in self._orphan_seen:
                continue   # second consecutive sighting required
            try:
                self.client.requeue(Lease(o.job_id, o.epoch), RequeueRequest(reason="interrupted"),
                                    retry=ORPHAN_RETRY)
                log.info("released orphan lease %s epoch %s", o.job_id, o.epoch)
                seen_now.discard(key)
            except (BdrApiError, BdrUnreachable) as exc:
                log.warning("orphan release failed for %s: %s", o.job_id, exc)
        self._orphan_seen = seen_now

    # Loop watchdog ---------------------------------------------------------------------

    def _probe_ran(self, sent_at: float) -> None:
        with self._probe_lock:
            lag = time.monotonic() - sent_at
            self._max_lag = max(self._max_lag, lag)
            if self._probe_sent_at == sent_at:
                self._probe_sent_at = None
                self._probe_reported = False

    def check_loop(self) -> None:
        if self.loop is None:
            return
        now = time.monotonic()
        with self._probe_lock:
            sent = self._probe_sent_at
            reported = self._probe_reported
        if sent is None:
            with self._probe_lock:
                self._probe_sent_at = now
            try:
                self.loop.call_soon_threadsafe(self._probe_ran, now)
            except RuntimeError:   # loop closed: the process is shutting down
                with self._probe_lock:
                    self._probe_sent_at = None
            return
        stalled = now - sent
        if stalled >= self.stall_seconds and not reported:
            with self._probe_lock:
                self._probe_reported = True
            self._report_stall(stalled)
        if stalled >= self.stall_exit_seconds and not self._exiting:
            self._exiting = True
            log.critical("asyncio loop stalled %.0f s; exiting so the service restarts", stalled)
            self._report_stall(stalled, final=True)
            self._exit_fn(EXIT_LOOP_STALLED)

    def _report_stall(self, stalled: float, *, final: bool = False) -> None:
        summary = (f"Worker loop stalled {stalled:.0f} s; restarting the process" if final
                   else f"Worker loop stalled more than {self.stall_seconds:.0f} s")
        for tj in self.tracker.all():
            steps = tj.steps
            if steps is None:
                continue
            try:
                steps.log("browser.loop_stall", summary, actor="worker", ok=False,
                          result={"stalled_ms": int(stalled * 1000), "final": final}, prefix="hb")
                steps.flush_sync(self.client)
            except Exception:
                log.exception("could not record loop stall for %s", tj.job_id)
