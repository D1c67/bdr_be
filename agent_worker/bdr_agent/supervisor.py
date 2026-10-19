"""Worker process supervisor (spec 3.3, 3.5, 3.6).

Boot:
  1. hygiene: create dirs, wipe tmp/, delete stray atomic-write temps, let the
     browser provider kill leftover Chromium (W2 hook);
  2. new boot_id, then /hello (retried until BDR answers). /hello makes BDR
     requeue every lease an older boot held, so the worker never resumes from
     local state except to upload spooled late steps;
  3. /config until the worker is approved (pending answers {pending:true});
  4. start the heartbeat thread and the spool replay task.

Run: per-type slot pools sized from BDR config
  pool[type] = min(worker.max_slots, type.max_concurrency) for every type that
  is enabled, in worker.allowed_types and has a local runner; total running is
  capped at worker.max_slots. Claim capacity is spread round-robin across types
  (the start rotates each claim) so several agent types run side by side.
  A config change (heartbeat config_version) refetches /config within ~15 s;
  running jobs are never preempted by it.

Drain: no claims while the kill switch is off, the worker is draining (config
or heartbeat), or a shutdown is underway. A drain that lasts `drain_max_seconds`
(10 min) stops the running jobs with /requeue {drain}.

Shutdown (signal or service stop): stop claiming, stop every slot with
/requeue {shutdown, service_stop:true}, wait `shutdown_grace`, stop the
heartbeat, try one last spool upload. Any line can be the last one (power
loss): BDR requeues at the next /hello and the spool replays.

Self-update (spec 8, updater.py): when /config offers a signed release other
than this version, the worker drains while it downloads and stages it through
the launcher, then exits with EXIT_UPDATE (5) once no job is running.

Exit codes: 0 clean stop, 3 stale_boot (launcher restarts), 4 revoked or
unauthorized (re-enroll needed), 5 update staged (launcher starts the new
version), 2 local config problems (see __main__).
"""

import asyncio
import contextlib
import logging
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

from .atomicfile import remove_stray_temps
from .client import (
    BdrApiError,
    BdrClient,
    BdrUnreachable,
    PendingApproval,
    RetryPolicy,
    StaleBoot,
    SyncBdrClient,
    WorkerRevoked,
    WorkerUnauthorized,
)
from .heartbeat import HeartbeatThread, JobTracker
from .local_config import CHROMIUM_TMP_NAME, Paths
from .protocol import (
    STALE_BOOT_EXIT_CODE,
    ClaimedJob,
    ClaimRequest,
    ConfigResponse,
    HelloRequest,
    HelpUpdate,
    PendingResponse,
    TypeConfig,
    WorkerCapabilities,
)
from .redact import LiveSecrets
from .slot import BrowserProvider, JobRunner, Slot, SlotDeps
from .spool import Spool, replay_spool
from .updater import EXIT_UPDATE, Updater

log = logging.getLogger("bdr_agent.supervisor")

EXIT_OK = 0
EXIT_REVOKED = 4
MAX_LOCAL_SLOTS = 8
HELLO_RETRY = RetryPolicy(max_attempts=4, cap_s=10.0)
CONFIG_RETRY = RetryPolicy(max_attempts=3, cap_s=5.0)


def compute_pools(cfg: ConfigResponse | None, runner_types: set[str]) -> dict[str, int]:
    """Per-type pool sizes from BDR config. Empty when nothing may run."""
    if cfg is None or not cfg.enabled or cfg.worker.status != "active":
        return {}
    total = max(0, int(cfg.worker.max_slots))
    allowed = set(cfg.worker.allowed_types)
    pools: dict[str, int] = {}
    for key in sorted(runner_types):
        t = cfg.types.get(key)
        if t is None or not t.enabled or key not in allowed:
            continue
        size = min(total, max(0, int(t.max_concurrency)), MAX_LOCAL_SLOTS)
        if size > 0:
            pools[key] = size
    return pools


def round_robin_capacity(pools: dict[str, int], running: dict[str, int], total_free: int,
                         start: int = 0) -> dict[str, int]:
    """Spread `total_free` slots across types with room, one at a time,
    starting at index `start` of the sorted type list."""
    keys = sorted(pools)
    if not keys or total_free <= 0:
        return {}
    room = {k: max(0, pools[k] - running.get(k, 0)) for k in keys}
    out: dict[str, int] = {}
    left = total_free
    i = start % len(keys)
    idle_rounds = 0
    while left > 0 and idle_rounds < len(keys):
        k = keys[i]
        if room[k] > 0:
            room[k] -= 1
            out[k] = out.get(k, 0) + 1
            left -= 1
            idle_rounds = 0
        else:
            idle_rounds += 1
        i = (i + 1) % len(keys)
    return out


class Supervisor:
    def __init__(self, *, client: BdrClient, hb_client: SyncBdrClient, paths: Paths,
                 runners: dict[str, JobRunner], version: str, secrets: LiveSecrets,
                 browser: BrowserProvider | None = None, api_host: str = "",
                 chromium_version: str | None = None, bad_versions: list[str] | None = None,
                 claim_interval: float = 5.0, config_poll_seconds: float = 30.0,
                 hello_retry_seconds: float = 30.0, shutdown_grace: float = 20.0,
                 drain_max_seconds: float = 600.0, replay_interval: float = 60.0,
                 heartbeat_factory: Callable[..., HeartbeatThread] | None = None,
                 slot_tick: float = 1.0, flush_interval: float = 2.0, updater: Updater | None = None):
        self.client = client
        self.hb_client = hb_client
        self.paths = paths
        # A type that needs a browser is not advertised without one, so BDR
        # never hands this worker a job it would only crash on.
        self.runners = {k: r for k, r in runners.items() if browser is not None or not getattr(r, "needs_browser", False)}
        for k in sorted(set(runners) - set(self.runners)):
            log.warning("agent type %s needs a browser and none is configured; not advertised", k)
        self.version = version
        self.secrets = secrets
        self.browser = browser
        self.api_host = api_host
        self.chromium_version = chromium_version
        self.bad_versions = list(bad_versions or [])
        self.claim_interval = claim_interval
        self.config_poll_seconds = config_poll_seconds
        self.hello_retry_seconds = hello_retry_seconds
        self.shutdown_grace = shutdown_grace
        self.drain_max_seconds = drain_max_seconds
        self.replay_interval = replay_interval
        self._heartbeat_factory = heartbeat_factory or HeartbeatThread
        self.updater = updater
        self._update_task: asyncio.Task | None = None
        self._update_ready: str | None = None

        self.boot_id: UUID = uuid.uuid4()
        self.tracker = JobTracker()
        self.spool = Spool(paths.spool_dir)
        self.config: ConfigResponse | None = None
        self.heartbeat: HeartbeatThread | None = None
        self.exit_code: int | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._shutdown_requested = threading.Event()
        self._config_dirty = False
        self._hb_drain = False
        self._drain_since: float | None = None
        self._rr = 0
        self._slots: dict[UUID, Slot] = {}
        self._slot_tasks: dict[UUID, asyncio.Task] = {}
        self._slot_index: dict[str, int] = {}
        self._replay_task: asyncio.Task | None = None
        self.outcomes: list[Any] = []
        self._deps = SlotDeps(
            client=client, tracker=self.tracker, spool=self.spool, secrets=secrets, runners=self.runners,
            browser=browser, api_host=api_host, contact_age=self.contact_age, tick=slot_tick,
            flush_interval=flush_interval)

    # Thread-safe controls ------------------------------------------------------------

    def contact_age(self) -> float:
        return min(self.client.seconds_since_contact(), self.hb_client.seconds_since_contact())

    def request_shutdown(self) -> None:
        """Safe from a signal handler or any thread."""
        self._shutdown_requested.set()
        self._call_soon(lambda: None)

    def _call_soon(self, fn: Callable[[], None]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _run() -> None:
            fn()
            if self._wake is not None:
                self._wake.set()

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_run)

    # HeartbeatSink (called from the heartbeat thread) --------------------------------

    def on_lost(self, job_id: UUID) -> None:
        self._call_soon(lambda: self._stop_job(job_id, "lost"))

    def on_cancel(self, job_id: UUID) -> None:
        self._call_soon(lambda: self._stop_job(job_id, "cancel"))

    def on_help_update(self, update: HelpUpdate) -> None:
        def _deliver() -> None:
            slot = self._slots.get(update.job_id)
            if slot is not None:
                slot.deliver_help_update(update)
        self._call_soon(_deliver)

    def on_config_version(self, version: int) -> None:
        def _mark() -> None:
            if self.config is None or version != self.config.config_version:
                self._config_dirty = True
        self._call_soon(_mark)

    def on_drain(self, draining: bool) -> None:
        def _set() -> None:
            self._hb_drain = draining
        self._call_soon(_set)

    def on_fatal(self, exc: BdrApiError) -> None:
        code = STALE_BOOT_EXIT_CODE if isinstance(exc, StaleBoot) else EXIT_REVOKED
        self._call_soon(lambda: self._fatal(code))

    # Internals ------------------------------------------------------------------------

    def _fatal(self, code: int) -> None:
        if self.exit_code is None:
            self.exit_code = code
        # We no longer own these leases (stale boot) or may not talk to BDR
        # (revoked): stop without terminal calls.
        for slot in self._slots.values():
            slot.stop("lost")
        self._shutdown_requested.set()

    def _stop_job(self, job_id: UUID, reason: str) -> None:
        slot = self._slots.get(job_id)
        if slot is not None:
            slot.stop(reason)  # type: ignore[arg-type]

    def _draining(self) -> bool:
        cfg = self.config
        if self._update_task is not None or self._update_ready is not None:
            return True
        return bool(self._hb_drain or (cfg is not None and (cfg.worker.draining or not cfg.enabled)))

    def pools(self) -> dict[str, int]:
        return compute_pools(self.config, set(self.runners))

    def running_by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for slot in self._slots.values():
            out[slot.agent_type] = out.get(slot.agent_type, 0) + 1
        return out

    def slots_free(self) -> dict[str, int]:
        """For the heartbeat: free slots per pool (thread-safe enough: reads
        immutable snapshots of small dicts)."""
        pools = self.pools()
        running = self.tracker.count_by_type()
        cfg = self.config
        total_free = max(0, (cfg.worker.max_slots if cfg else 0) - sum(running.values()))
        return {k: max(0, min(total_free, v - running.get(k, 0))) for k, v in pools.items()}

    def capacity(self) -> dict[str, int]:
        if self._shutdown_requested.is_set() or self._draining() or self.config is None:
            return {}
        pools = self.pools()
        running = self.running_by_type()
        total_free = max(0, int(self.config.worker.max_slots) - len(self._slots))
        cap = round_robin_capacity(pools, running, total_free, self._rr)
        if pools:
            self._rr = (self._rr + 1) % len(pools)
        return cap

    async def _sleep_or_wake(self, seconds: float) -> None:
        assert self._wake is not None
        if self._shutdown_requested.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        self._wake.clear()

    def _boot_hygiene(self) -> None:
        self.paths.ensure()
        for entry in list(self.paths.tmp_dir.iterdir()):
            if entry.name == CHROMIUM_TMP_NAME:
                # Chromium is already running from here (the provider starts
                # before the supervisor and wipes it itself before launch).
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except OSError as exc:
                log.warning("could not wipe %s: %s", entry.name, exc)
        remove_stray_temps(self.paths.home)

    async def _hello(self) -> bool:
        """True once BDR answered /hello; False on shutdown or a fatal refusal."""
        caps = WorkerCapabilities(types={k: r.version for k, r in sorted(self.runners.items())},
                                  slots=MAX_LOCAL_SLOTS)
        req = HelloRequest(boot_id=self.boot_id, version=self.version, chromium_version=self.chromium_version,
                           bad_versions=self.bad_versions, capabilities=caps)
        while not self._shutdown_requested.is_set():
            try:
                resp = await self.client.hello(req, retry=HELLO_RETRY)
            except (WorkerRevoked, WorkerUnauthorized) as exc:
                log.error("BDR refused this worker (%s); re-enroll with the installer", exc.code)
                self.exit_code = EXIT_REVOKED
                return False
            except PendingApproval:
                log.info("worker is waiting for IT Admin approval")
            except (BdrUnreachable, BdrApiError) as exc:
                log.warning("hello failed: %s", exc)
            else:
                log.info("hello ok: status=%s config_version=%s requeued=%s", resp.status,
                         resp.config_version, resp.requeued)
                return True
            await self._sleep_or_wake(self.hello_retry_seconds)
        return False

    async def refresh_config(self, *, force: bool = False) -> None:
        etag = None if force or self.config is None else self.config.config_version
        try:
            resp = await self.client.get_config(etag, retry=CONFIG_RETRY)
        except StaleBoot:
            self._fatal(STALE_BOOT_EXIT_CODE)
            return
        except (WorkerRevoked, WorkerUnauthorized):
            self._fatal(EXIT_REVOKED)
            return
        except (BdrUnreachable, BdrApiError) as exc:
            log.warning("config refresh failed: %s", exc)
            return
        self._config_dirty = False
        if resp is None:
            return
        if isinstance(resp, PendingResponse):
            self.config = None
            return
        if self.config is None or resp.config_version != self.config.config_version:
            log.info("config v%s: enabled=%s max_slots=%s draining=%s pools=%s", resp.config_version,
                     resp.enabled, resp.worker.max_slots, resp.worker.draining,
                     compute_pools(resp, set(self.runners)))
        self.config = resp
        if resp.worker.status == "revoked":
            self._fatal(EXIT_REVOKED)

    async def _await_approval(self) -> bool:
        while not self._shutdown_requested.is_set():
            await self.refresh_config(force=True)
            if self.exit_code is not None:
                return False
            if self.config is not None:
                return True
            await self._sleep_or_wake(self.config_poll_seconds)
        return False

    def _start_heartbeat(self) -> None:
        self.hb_client.boot_id = self.boot_id
        self.heartbeat = self._heartbeat_factory(
            self.hb_client, self.tracker, self, boot_id=self.boot_id,
            config_version=lambda: self.config.config_version if self.config else 0,
            slots_free=self.slots_free, loop=self._loop)
        self.heartbeat.start()

    async def _replay_loop(self) -> None:
        while True:
            try:
                res = await replay_spool(self.spool, self.client,
                                         exclude=lambda j, e: self.tracker.holds(j, e))
                if res.posted or res.dropped:
                    log.info("spool replay: %s posted late, %s dropped, %s pending", res.posted,
                             res.dropped, res.pending)
            except Exception:
                log.exception("spool replay failed")
            await asyncio.sleep(self.replay_interval)

    def _type_config(self, agent_type: str) -> TypeConfig | None:
        return self.config.types.get(agent_type) if self.config else None

    def _start_job(self, job: ClaimedJob) -> None:
        tc = self._type_config(job.agent_type)
        # Register before anything awaits, so the next heartbeat lists it and
        # never mistakes it for an orphan.
        self.tracker.register(job.job_id, job.epoch, job.agent_type)
        idx = self._slot_index.get(job.agent_type, 0) + 1
        self._slot_index[job.agent_type] = idx
        slot = Slot(job.agent_type, idx, self._deps)
        self._slots[job.job_id] = slot
        if tc is None:
            # BDR handed us a type this config does not describe: use the
            # protocol defaults rather than drop a claimed job.
            tc = TypeConfig(enabled=True, max_concurrency=1, max_steps=40, max_active_seconds=1200)
        task = asyncio.create_task(slot.run(job, tc), name=f"slot-{job.agent_type}-{job.job_id}")
        self._slot_tasks[job.job_id] = task
        task.add_done_callback(lambda t, jid=job.job_id: self._slot_done(jid, t))

    def _slot_done(self, job_id: UUID, task: asyncio.Task) -> None:
        self._slots.pop(job_id, None)
        self._slot_tasks.pop(job_id, None)
        self.tracker.unregister(job_id)
        exc = None if task.cancelled() else task.exception()
        if task.cancelled():
            log.warning("slot for %s was cancelled", job_id)
        elif exc is not None:
            if isinstance(exc, StaleBoot):
                self._fatal(STALE_BOOT_EXIT_CODE)
            elif isinstance(exc, (WorkerRevoked, WorkerUnauthorized)):
                self._fatal(EXIT_REVOKED)
            else:
                log.error("slot for %s failed: %r", job_id, exc)
        else:
            self.outcomes.append(task.result())
        if self._wake is not None:
            self._wake.set()

    async def _claim_once(self) -> int:
        cap = self.capacity()
        if not cap:
            return 0
        self.tracker.claim_started()
        try:
            resp = await self.client.claim(ClaimRequest(boot_id=self.boot_id, capacity=cap))
            started = 0
            for job in resp.jobs:
                if job.job_id in self._slots:
                    continue
                self._start_job(job)
                started += 1
            return started
        except StaleBoot:
            self._fatal(STALE_BOOT_EXIT_CODE)
        except (WorkerRevoked, WorkerUnauthorized):
            self._fatal(EXIT_REVOKED)
        except PendingApproval:
            self.config = None
        except (BdrUnreachable, BdrApiError) as exc:
            log.warning("claim failed: %s", exc)
        finally:
            self.tracker.claim_finished()
        return 0

    def _check_drain_timeout(self) -> None:
        if self._draining() and self._slots:
            now = time.monotonic()
            if self._drain_since is None:
                self._drain_since = now
            elif now - self._drain_since >= self.drain_max_seconds:
                log.info("drain timeout: requeueing %d running job(s)", len(self._slots))
                for slot in self._slots.values():
                    slot.stop("drain")
        else:
            self._drain_since = None

    # Self-update ---------------------------------------------------------------------

    def _maybe_start_update(self) -> None:
        if self.updater is None or self.config is None or self._update_task is not None \
                or self._update_ready is not None:
            return
        offer = self.config.release
        decision = self.updater.check(offer)
        if decision.action != "stage" or offer is None:
            return
        log.info("release %s offered (running %s): draining to update", offer.version, self.version)
        self._update_task = asyncio.create_task(self.updater.prepare(self.client, offer),
                                                name=f"update-{offer.version}")
        self._update_task.add_done_callback(lambda t, v=offer.version: self._update_done(v, t))

    def _update_done(self, version: str, task: asyncio.Task) -> None:
        self._update_task = None
        exc = None if task.cancelled() else task.exception()
        if task.cancelled() or exc is not None:
            if exc is not None:
                log.error("update to %s failed: %s; retrying later", version, exc)
            if self.updater is not None:
                self.updater.mark_failed(version)
        else:
            self._update_ready = task.result()
        if self._wake is not None:
            self._wake.set()

    def _update_exit_due(self) -> bool:
        if self._update_ready is None or self._slots:
            return False
        if self.exit_code is None:
            self.exit_code = EXIT_UPDATE
        self._shutdown_requested.set()
        return True

    async def _shutdown(self) -> None:
        reason = "lost" if self.exit_code not in (None, EXIT_OK, EXIT_UPDATE) else "shutdown"
        for slot in list(self._slots.values()):
            slot.stop(reason)  # type: ignore[arg-type]
        tasks = list(self._slot_tasks.values())
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self.shutdown_grace)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5)
        if self._update_task is not None:
            self._update_task.cancel()
            with contextlib.suppress(BaseException):
                await self._update_task
        if self._replay_task is not None:
            self._replay_task.cancel()
            with contextlib.suppress(BaseException):
                await self._replay_task
        if self.heartbeat is not None:
            await asyncio.to_thread(self.heartbeat.stop, 5.0)
        if reason == "shutdown":
            with contextlib.suppress(Exception):
                await replay_spool(self.spool, self.client)

    # Main ------------------------------------------------------------------------------

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self.client.boot_id = self.boot_id
        self.hb_client.boot_id = self.boot_id
        self._boot_hygiene()
        try:
            if not await self._hello():
                return self.exit_code if self.exit_code is not None else EXIT_OK
            if not await self._await_approval():
                return self.exit_code if self.exit_code is not None else EXIT_OK
            self._start_heartbeat()
            self._replay_task = asyncio.create_task(self._replay_loop(), name="spool-replay")
            while not self._shutdown_requested.is_set():
                if self._config_dirty:
                    await self.refresh_config()
                    if self._shutdown_requested.is_set():
                        break
                if self.config is None:
                    # Became pending again (unlikely): wait for approval.
                    if not await self._await_approval():
                        break
                self._maybe_start_update()
                if self._update_exit_due():
                    break
                self._check_drain_timeout()
                started = await self._claim_once()
                if started:
                    continue   # there may be more queued work for the remaining slots
                await self._sleep_or_wake(self.claim_interval)
        finally:
            await self._shutdown()
        return self.exit_code if self.exit_code is not None else EXIT_OK
