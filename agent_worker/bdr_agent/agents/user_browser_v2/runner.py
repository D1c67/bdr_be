"""The user_browser v2 runner (plan 15; WI-10). Signed worker code.

One claimed `user_browser` job, start to terminal:
  1. The slot asks `user_slot_parts(base_client)` for the personal transport
     (client.UserV2Client: closed endpoint map, memory-only spool).
  2. The job policy (UserJobPolicy) is fetched over the lease-fenced policy
     route; a policy for another job ends the task.
  3. UserGuardV2 (WI-9) is built from the policy, its holds bound to this
     job's hold routes, and installed on the job's own Chromium (WI-8:
     every request is aborted until then). A site marked `needs_headed` gets
     its browser relaunched headed first.
  4. Downloads (downloads.py) and saved sessions (identity.py) are wired.
  5. The model loop runs (agent/loop.py) with V2Executor and the new tools.
     Each model action's one-line text becomes the description on hold cards.
  6. On the way out an unreported sign-in attempt is reported, downloads in
     progress get a short drain, and the login exemption is closed. The slot
     closes the browser (process, proxy, job and quarantine folders).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from ...agent.loop import AgentLoop, run_loop
from ...client import (
    BdrClient,
    BdrRejected,
    LeaseLost,
    StaleBoot,
    WorkerRevoked,
    WorkerUnauthorized,
)
from ...protocol import TurnActionResponse, worker_step_kind_allowed
from ...slot import Finish, JobResult, UserSlotParts
from .client import MemorySpool, UserV2Client, UserV2StepLog
from .downloads import DefenderScanner, DownloadManager
from .guard import build_guard
from .identity import UserIdentityV2
from .tools import V2Executor, V2HelpDesk, V2Tools

log = logging.getLogger("bdr_agent.user_browser_v2.runner")

END_DRAIN_SECONDS = 30.0
_FATAL = (LeaseLost, StaleBoot, WorkerRevoked, WorkerUnauthorized)


class V2AgentLoop(AgentLoop):
    """The shared loop; the model's own words for each action go to the
    guard, which shows them on hold cards as the assistant's description."""

    async def _turn(self, req: Any) -> Any:
        resp = await super()._turn(req)
        if isinstance(resp, TurnActionResponse):
            self.ex.v2.set_action_description(resp.assistant_text)
        return resp


def guard_step_logger(log_step: Any) -> Any:
    """The guard logs hold steps as `hold.<x>`, which is not a worker step
    kind (protocol.WORKER_STEP_KIND_PREFIXES); they are recorded as
    `browser.hold_<x>` so the hold trail reaches the job's step ledger."""

    def logged(kind: str, summary: str, **fields: Any) -> Any:
        if not worker_step_kind_allowed(kind):
            kind = "browser." + kind.replace(".", "_")
        return log_step(kind, summary, **fields)

    return logged


def _selftest_downloads(jb: Any) -> bool:
    provider = getattr(jb, "provider", None)
    pool = getattr(provider, "user_pool", None)
    selftest = getattr(pool, "selftest", None)
    return bool(getattr(selftest, "downloads_allowed", False))


class UserBrowserV2Runner:
    version = "1.0.0"         # the type implementation version (capabilities.types), not the release
    needs_browser = True

    def __init__(self, *, verifier: Any = None, scanner: Any = None, downloads_allowed: Any = None,
                 sleep: Any = None):
        self._verifier = verifier
        self._scanner = scanner
        self._downloads_allowed = downloads_allowed
        self._sleep = sleep

    def user_slot_parts(self, base_client: BdrClient) -> UserSlotParts:
        return UserSlotParts(client=UserV2Client(base_client), spool=MemorySpool(), step_log_class=UserV2StepLog)

    async def run(self, ctx: Any) -> JobResult:
        job = ctx.job
        if job.principal_kind != "user" or not job.principal_user_id:
            return Finish("failed", "Personal principal required", error_kind="unsupported")
        client = ctx.client
        jb = ctx.browser_context
        if not isinstance(client, UserV2Client):
            return Finish("failed", "Personal transport required", error_kind="unsupported")
        if jb is None or not hasattr(jb, "install_guard"):
            return Finish("failed", "Personal browser required", error_kind="unsupported")
        lease = ctx.lease
        policy = await client.get_policy(lease)
        holder: dict[str, Any] = {}

        async def hold_screenshot(page: Any) -> Any:
            ex = holder.get("ex")
            return await ex.screenshot(page, label="hold") if ex is not None else None

        async def refresh() -> Any:
            return await client.get_policy(lease)

        guard = build_guard(policy, send=client.sender(lease), lease_epoch=lambda: ctx.lease.epoch,
                            verifier=self._verifier, redact=ctx.secrets.scrub_text,
                            log_step=guard_step_logger(ctx.steps.log),
                            screenshot=hold_screenshot, refresh_policy=refresh)

        async def on_fence() -> None:
            guard.set_policy(await client.get_policy(lease))

        client.on_scope_fence = on_fence
        await jb.install_guard(guard)
        start = ctx.input
        site = policy.site(start.start_site_key) if start.start_site_key else None
        if site is None and start.start_url:
            site = guard.site_for(start.start_url)
        if site is not None and site.needs_headed and getattr(jb, "headless", False):
            ctx.steps.log("browser.headed", f"Opening {site.label} in a visible window", ok=True)
            await jb.relaunch(headless=False)

        executor = V2Executor(ctx, jb, guard, start_url=start.start_url,
                              **({"sleep": self._sleep} if self._sleep is not None else {}))
        holder["ex"] = executor
        desk = V2HelpDesk(ctx)
        identity = UserIdentityV2(ctx, executor, desk, guard)
        allowed = self._downloads_allowed or (lambda: _selftest_downloads(jb))
        downloads = DownloadManager(quarantine_dir=jb.downloads_dir,
                                    upload=lambda meta, path: client.upload_download(lease, meta, path),
                                    caps=lambda: guard.policy.caps, allowed=allowed,
                                    scanner=self._scanner or DefenderScanner(), log_step=ctx.steps.log,
                                    redact=ctx.secrets.scrub_text)
        downloads.attach(jb.context)
        executor.identity = identity
        executor.downloads = downloads
        executor.extra_tools.update(V2Tools(executor, desk, identity, downloads).handlers())
        try:
            with contextlib.suppress(BdrRejected):
                await identity.load_saved_sessions()
            await jb.model_page()
            return await run_loop(V2AgentLoop(ctx, executor, desk, checkpoint=ctx.save_checkpoint))
        finally:
            await self._wind_down(identity, downloads, guard)

    @staticmethod
    async def _wind_down(identity: UserIdentityV2, downloads: DownloadManager, guard: Any) -> None:
        try:
            await identity.abandon()
        except _FATAL:
            pass
        try:
            await downloads.drain(END_DRAIN_SECONDS)
        except _FATAL:
            pass
        except Exception as exc:
            log.debug("download drain failed: %s", type(exc).__name__)
        with contextlib.suppress(Exception):
            guard.close_login_attempt("job_end")


__all__ = ["UserBrowserV2Runner", "V2AgentLoop"]
