"""One running job (spec 3.6, 4).

A Slot owns a claimed job from start to its terminal call:
  - a write-ahead StepLog on the job's spool, flushed in the background;
  - a fresh browser context per job from the BrowserProvider (when the type
    needs one), closed in `finally` whatever happens;
  - the type's JobRunner as a child task, supervised once a second for the
    active-time cap, BDR silence (15 min) and stop requests;
  - exactly one terminal call: /finish, /requeue, or none (the runner handed
    the job off through /rfi-capture or /park, or the lease is gone).

Stop reasons and what reaches BDR:
  lost         lease_lost / job_not_running / heartbeat `lost`: nothing
  cancel       heartbeat `cancel` (IT Admin stop): /finish canceled
  shutdown     service stop: /requeue shutdown (service_stop=true, uncharged)
  drain        update drain timeout: /requeue drain (service_stop=true, uncharged)
  timeout      max_active_seconds: /finish failed cannot_complete
  unreachable  15 min without BDR: nothing (local abort; spool uploads later)
An unexpected exception in the runner is /requeue crash (charged).
StaleBoot propagates to the supervisor, which exits the process with code 3.
Power loss at any line is safe: BDR requeues the lease at the next /hello and
the spool replays the steps as late rows.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError

from .client import (
    BdrApiError,
    BdrClient,
    BdrUnreachable,
    Lease,
    LeaseLost,
    RetryPolicy,
    StaleBoot,
    WorkerRevoked,
    WorkerUnauthorized,
)
from .heartbeat import JobTracker
from .protocol import (
    BDR_UNREACHABLE_ABORT_SECONDS,
    ERROR_TEXT_MAX,
    FINISH_SUMMARY_MAX,
    ClaimedJob,
    FinishRequest,
    HelpUpdate,
    RequeueRequest,
    TypeConfig,
    parse_input,
)
from .redact import LiveSecrets
from .spool import Spool, StepLog

log = logging.getLogger("bdr_agent.slot")

StopReason = Literal["lost", "cancel", "shutdown", "drain", "timeout", "unreachable"]
_STOP_PRIORITY: dict[str, int] = {"lost": 3, "shutdown": 2, "drain": 2, "cancel": 1, "timeout": 1,
                                  "unreachable": 1}

TERMINAL_RETRY = RetryPolicy(max_attempts=None, deadline_s=float(BDR_UNREACHABLE_ABORT_SECONDS), cap_s=60.0)
SHUTDOWN_RETRY = RetryPolicy(max_attempts=3, deadline_s=10.0, cap_s=3.0)


# What a runner returns -------------------------------------------------------------


@dataclass(frozen=True)
class Finish:
    status: Literal["succeeded", "failed", "canceled"]
    summary: str
    outcome: Literal["nothing_to_do", "self_test_ok"] | None = None
    error_kind: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Requeue:
    reason: Literal["crash", "login_spacing", "provider_outage", "interrupted"]
    retry_at: datetime | None = None


@dataclass(frozen=True)
class HandedOff:
    """The runner already made the job's terminal call (/rfi-capture moved it
    to applying, or /park released it while waiting for help)."""
    summary: str


JobResult = Finish | Requeue | HandedOff


@dataclass(frozen=True)
class _ServiceRequeue:
    """Uncharged requeue for a service stop or an update drain (slot-internal)."""
    reason: Literal["shutdown", "drain"]
    retry_at: datetime | None = None


class JobRunner(Protocol):
    """One per agent type (W3/W4 register them in bdr_agent.agents.REGISTRY)."""
    version: str            # advertised in /hello capabilities.types
    needs_browser: bool

    async def run(self, ctx: "JobContext") -> JobResult: ...


class BrowserProvider(Protocol):
    """One Chromium per worker, one new context per job (W2)."""

    async def new_context(self, job: ClaimedJob) -> Any: ...

    async def close_context(self, context: Any) -> None: ...


# The context a runner gets --------------------------------------------------------


class JobContext:
    def __init__(self, *, job: ClaimedJob, lease: Lease, input: BaseModel, type_config: TypeConfig,
                 client: BdrClient, steps: StepLog, secrets: LiveSecrets, tracker: JobTracker,
                 browser_context: Any, api_host: str):
        self.job = job
        self.lease = lease
        self.agent_type = job.agent_type
        self.input = input
        self.type_config = type_config
        self.client = client
        self.steps = steps
        self.secrets = secrets
        self.browser_context = browser_context
        self.api_host = api_host
        self.checkpoint: dict[str, Any] = dict(job.checkpoint or {})
        self.checkpoint_seq = job.checkpoint_seq
        self.help_updates: asyncio.Queue[HelpUpdate] = asyncio.Queue()
        self._tracker = tracker
        self.waiting_help = False

    @property
    def job_id(self) -> UUID:
        return self.job.job_id

    def set_phase(self, phase: str | None) -> None:
        self._tracker.set_phase(self.job_id, phase)

    def set_waiting_help(self, waiting: bool) -> None:
        """While waiting, active time does not count toward max_active_seconds."""
        self.waiting_help = waiting
        self._tracker.set_active(self.job_id, not waiting)

    async def save_checkpoint(self, data: dict[str, Any]) -> None:
        """Monotonic PUT; BDR ignores a seq it already has (safe on retry)."""
        seq = self.checkpoint_seq + 1
        await self.client.put_checkpoint(self.lease, seq, data)
        self.checkpoint_seq = seq
        self.checkpoint = dict(data)
        phase = data.get("phase") if isinstance(data, dict) else None
        if isinstance(phase, str):
            self.set_phase(phase)


@dataclass
class SlotDeps:
    client: BdrClient
    tracker: JobTracker
    spool: Spool
    secrets: LiveSecrets
    runners: dict[str, JobRunner]
    browser: BrowserProvider | None = None
    api_host: str = ""
    contact_age: Callable[[], float] | None = None   # seconds since BDR last answered
    flush_interval: float = 2.0
    tick: float = 1.0
    unreachable_abort_seconds: float = float(BDR_UNREACHABLE_ABORT_SECONDS)
    terminal_retry: RetryPolicy = TERMINAL_RETRY
    shutdown_retry: RetryPolicy = SHUTDOWN_RETRY


@dataclass
class SlotOutcome:
    job_id: UUID
    agent_type: str
    terminal: str           # finish:<status> | requeue:<reason> | handed_off | none
    stop_reason: str | None = None
    detail: str | None = None


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Slot:
    def __init__(self, agent_type: str, index: int, deps: SlotDeps):
        self.agent_type = agent_type
        self.index = index
        self.deps = deps
        self.job: ClaimedJob | None = None
        self.ctx: JobContext | None = None
        self.stop_reason: StopReason | None = None
        self._task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self.job is not None

    def stop(self, reason: StopReason) -> None:
        """Ask the running job to stop. Safe to call more than once; a
        higher-priority reason (lost > shutdown/drain > others) wins."""
        if self.job is None:
            return
        if self.stop_reason is None or _STOP_PRIORITY[reason] > _STOP_PRIORITY[self.stop_reason]:
            self.stop_reason = reason
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def deliver_help_update(self, update: HelpUpdate) -> None:
        if self.ctx is not None:
            self.ctx.help_updates.put_nowait(update)

    # Run ------------------------------------------------------------------------------

    async def run(self, job: ClaimedJob, type_config: TypeConfig) -> SlotOutcome:
        d = self.deps
        self.job = job
        self.stop_reason = None
        lease = Lease(job.job_id, job.epoch)
        if not d.tracker.holds(job.job_id, job.epoch):
            d.tracker.register(job.job_id, job.epoch, job.agent_type)
        job_spool = d.spool.for_job(job.job_id, job.epoch)
        steps = StepLog(job_spool, d.secrets, on_logged=lambda n: d.tracker.set_steps(job.job_id, n))
        d.tracker.attach_steps(job.job_id, steps)
        flusher = asyncio.create_task(self._flush_loop(steps), name=f"flush-{job.job_id}")
        outcome = SlotOutcome(job.job_id, job.agent_type, "none")
        try:
            steps.log("note", f"Started on slot {job.agent_type}#{self.index} (attempt {job.attempts}, "
                              f"epoch {job.epoch}, resume {job.last_resume_reason or 'none'})")
            browser_holder: list[Any] = []
            try:
                result, detail = await self._execute(job, type_config, lease, steps, browser_holder)
            finally:
                # Close the job's browser before BDR can hand the job to anyone else.
                if browser_holder:
                    await self._close_context(browser_holder[0])
            outcome.stop_reason = self.stop_reason
            outcome.detail = detail
            outcome.terminal = await self._terminal(lease, steps, result)
        finally:
            flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await flusher
            try:
                await steps.flush(d.client)
            except Exception as exc:
                log.warning("final step flush for %s failed: %s", job.job_id, exc)
            job_spool.mark_done()
            if job_spool.remove_if_settled():
                d.spool.forget(job.job_id, job.epoch)
            d.tracker.unregister(job.job_id)
            # Job-scoped secrets (released password, MFA code, cookie values)
            # are kept for scrubbing only while the job lives.
            d.secrets.discard_owner(str(job.job_id))
            self.job = None
            self.ctx = None
            self._task = None
        return outcome

    async def _close_context(self, browser_ctx: Any) -> None:
        if self.deps.browser is None:
            return
        try:
            await asyncio.wait_for(self.deps.browser.close_context(browser_ctx), timeout=15)
        except Exception as exc:   # closing must never mask the job outcome
            log.warning("closing browser context failed: %s", exc)

    async def _execute(self, job: ClaimedJob, type_config: TypeConfig, lease: Lease, steps: StepLog,
                       browser_holder: list) -> tuple[JobResult | None, str | None]:
        d = self.deps
        runner = d.runners.get(job.agent_type)
        if runner is None:
            steps.log("error.unsupported", f"No runner for agent type {job.agent_type}", ok=False)
            return Finish("failed", "This worker cannot run this agent type", error_kind="unsupported",
                          error=f"no runner for {job.agent_type}"), "no_runner"
        try:
            parsed = parse_input(job.agent_type, job.input)
        except (ValidationError, KeyError) as exc:
            steps.log("error.input", "Job input failed validation", ok=False,
                      result={"errors": str(exc)[:1000]})
            return Finish("failed", "Invalid job input", error_kind="unsupported",
                          error="job input failed validation"), "bad_input"

        try:
            if getattr(runner, "needs_browser", False):
                if d.browser is None:
                    raise RuntimeError("this agent type needs a browser and none is configured")
                browser_holder.append(await d.browser.new_context(job))
            ctx = JobContext(job=job, lease=lease, input=parsed, type_config=type_config, client=d.client,
                             steps=steps, secrets=d.secrets, tracker=d.tracker,
                             browser_context=browser_holder[0] if browser_holder else None,
                             api_host=d.api_host)
            self.ctx = ctx
            if self.stop_reason is not None:
                return self._result_for_stop(steps), self.stop_reason
            self._task = asyncio.create_task(runner.run(ctx), name=f"job-{job.job_id}")
            await self._supervise(ctx, type_config)
            return self._collect(steps)
        except (StaleBoot, WorkerRevoked, WorkerUnauthorized):
            self.stop_reason = "lost"
            raise
        except LeaseLost:
            self.stop_reason = "lost"
            return None, "lease_lost"
        except Exception as exc:   # context creation failed
            steps.log("error.crash", f"Job setup failed: {type(exc).__name__}", ok=False,
                      result={"error": _clip(d.secrets.scrub_text(str(exc)), 1000)})
            return Requeue("crash"), "setup_failed"

    async def _supervise(self, ctx: JobContext, type_config: TypeConfig) -> None:
        d = self.deps
        assert self._task is not None
        active = 0.0
        last = time.monotonic()
        limit = float(type_config.max_active_seconds)
        while True:
            done, _ = await asyncio.wait({self._task}, timeout=d.tick)
            if done:
                return
            now = time.monotonic()
            if not ctx.waiting_help:
                active += now - last
            last = now
            if active > limit and self.stop_reason is None:
                self.stop("timeout")
            if (d.contact_age is not None and self.stop_reason is None
                    and d.contact_age() > d.unreachable_abort_seconds):
                self.stop("unreachable")

    def _collect(self, steps: StepLog) -> tuple[JobResult | None, str | None]:
        task = self._task
        assert task is not None and task.done()
        try:
            result = task.result()
        except asyncio.CancelledError:
            return self._result_for_stop(steps), self.stop_reason
        except LeaseLost:
            self.stop_reason = "lost"
            steps.log("error.lease_lost", "Lease lost; job aborted", ok=False)
            return None, "lease_lost"
        except (StaleBoot, WorkerRevoked, WorkerUnauthorized):
            self.stop_reason = "lost"
            raise
        except BdrUnreachable as exc:
            steps.log("error.bdr_unreachable", f"BDR unreachable: {exc.endpoint}", ok=False)
            if self.deps.contact_age is not None and self.deps.contact_age() > self.deps.unreachable_abort_seconds:
                self.stop_reason = "unreachable"
                return None, "unreachable"
            # A BDR outage is not the job's fault (SPEC 3.3): uncharged requeue.
            return Requeue("interrupted"), "unreachable_call"
        except Exception as exc:
            name = type(exc).__name__
            steps.log("error.crash", f"Runner crashed: {name}", ok=False,
                      result={"error": _clip(self.deps.secrets.scrub_text(str(exc)), 1000), "type": name})
            return Requeue("crash"), "crash"
        if self.stop_reason == "lost":
            return None, "lost"
        if not isinstance(result, (Finish, Requeue, HandedOff)):
            steps.log("error.crash", f"Runner returned {type(result).__name__}", ok=False)
            return Requeue("crash"), "bad_result"
        return result, None

    def _result_for_stop(self, steps: StepLog) -> JobResult | _ServiceRequeue | None:
        reason = self.stop_reason
        if reason == "cancel":
            steps.log("note", "Stopped by IT Admin")
            return Finish("canceled", "Stopped by IT Admin")
        if reason == "timeout":
            steps.log("error.timeout", "Active time cap reached", ok=False)
            return Finish("failed", "Active time cap reached", error_kind="cannot_complete",
                          error="max_active_seconds exceeded")
        if reason == "unreachable":
            steps.log("error.bdr_unreachable", "BDR unreachable for 15 minutes; aborted locally", ok=False)
            return None
        if reason in ("shutdown", "drain"):
            steps.log("note", f"Stopping for {reason}")
            return _ServiceRequeue(reason)
        return None   # lost

    async def _terminal(self, lease: Lease, steps: StepLog,
                        result: JobResult | _ServiceRequeue | None) -> str:
        d = self.deps
        if result is None or self.stop_reason == "lost":
            return "none"
        if isinstance(result, HandedOff):
            steps.log("note", _clip(f"Handed off: {result.summary}", 500) or "Handed off")
            await self._flush_before_terminal(steps)
            return "handed_off"
        service = isinstance(result, _ServiceRequeue)
        retry = d.shutdown_retry if service else d.terminal_retry
        try:
            if isinstance(result, Finish):
                req = FinishRequest(status=result.status, outcome=result.outcome,
                                    error_kind=result.error_kind,  # type: ignore[arg-type]
                                    error=_clip(d.secrets.scrub_text(result.error) if result.error else None,
                                                ERROR_TEXT_MAX),
                                    summary=_clip(d.secrets.scrub_text(result.summary), FINISH_SUMMARY_MAX) or "")
                steps.log("note", _clip(f"Finishing: {result.status}", 500) or "Finishing")
                await self._flush_before_terminal(steps)
                await d.client.finish(lease, req, retry=retry)
                return f"finish:{result.status}"
            if service:
                req_q = RequeueRequest(reason=result.reason, service_stop=True)
            else:
                req_q = RequeueRequest(reason=result.reason, retry_at=result.retry_at)
            steps.log("note", f"Requeueing: {result.reason}")
            await self._flush_before_terminal(steps, quick=service)
            await d.client.requeue(lease, req_q, retry=retry)
            return f"requeue:{result.reason}"
        except ValidationError as exc:
            # The runner produced an invalid terminal shape: charge it as a crash.
            steps.log("error.crash", "Runner returned an invalid finish", ok=False,
                      result={"error": str(exc)[:1000]})
            try:
                await d.client.requeue(lease, RequeueRequest(reason="crash"), retry=retry)
                return "requeue:crash"
            except (BdrApiError, BdrUnreachable):
                return "none"
        except (StaleBoot, WorkerRevoked, WorkerUnauthorized):
            raise
        except LeaseLost:
            steps.log("error.lease_lost", "Lease lost before the terminal call", ok=False)
            return "none"
        except (BdrApiError, BdrUnreachable) as exc:
            # BDR will reclaim the lease (sweeper) or requeue it at the next /hello.
            log.warning("terminal call for %s failed: %s", lease.job_id, exc)
            return "none"

    async def _flush_before_terminal(self, steps: StepLog, *, quick: bool = False) -> None:
        """Post the job's steps while the lease is still live, so they land as
        regular rows rather than late ones. Best effort: the spool keeps any
        row that does not make it."""
        try:
            await asyncio.wait_for(steps.flush(self.deps.client), timeout=5 if quick else 30)
        except (Exception, TimeoutError) as exc:
            log.debug("pre-terminal flush failed: %s", exc)

    async def _flush_loop(self, steps: StepLog) -> None:
        while True:
            await asyncio.sleep(self.deps.flush_interval)
            if steps.pending():
                try:
                    await steps.flush(self.deps.client)
                except Exception as exc:   # flushing is best effort; the spool keeps the rows
                    log.debug("step flush failed: %s", exc)
