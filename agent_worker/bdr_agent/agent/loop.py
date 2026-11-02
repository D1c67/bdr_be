"""The model-driven job loop (O1, spec 1, 4, 4.5).

    screenshot -> POST /turns -> execute the ONE returned tool -> repeat
    until finish | stop | lease_lost | cancel

BDR owns the transcript, the provider call and the caps; the worker only
runs tools. The loop:

  - numbers turns the way BDR does: `prev_turn_no` starts at the claim's
    `turn_count` (0 on a fresh job) and then follows the server's turn_no;
    a 202 is retried with the same body and Idempotency-Key after
    `retry_after`, so a redeploy or a lost response never double-runs a turn;
  - sends each tool result (<= 32 KB, scrubbed) with the screenshot taken
    after the action, the page URL (query stripped) and title;
  - reports helper messages it has shown the model (`helper_message_ids_seen`)
    and consumes them once BDR has the turn; on resume that includes the
    claim's `unconsumed_help`;
  - PUTs {last_url} into the checkpoint every 5 steps (spec 4.5);
  - maps stop replies: cap_reached / daily_cap_reached -> cap_reached help
    then park; provider_config -> provider_config help then park;
    provider_outage -> /requeue provider_outage (uncharged) with retry_at;
    disabled / canceled -> finish canceled; secret_leak_blocked -> finish
    failed (BDR already failed it; a lease_lost there is expected).

LeaseLost, StaleBoot and revocation propagate to the slot unchanged.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..client import BdrRejected
from ..protocol import (
    TurnActionResponse,
    TurnPendingResponse,
    TurnRequest,
    TurnStopResponse,
    ToolResult,
)
from ..redact import strip_query
from ..slot import Finish, HandedOff, JobResult, Requeue
from .help import HelpDesk

log = logging.getLogger("bdr_agent.agent.loop")

CHECKPOINT_EVERY_STEPS = 5
MAX_PENDING_WAITS = 60          # 202s in a row before the worker gives up the turn (about 5 min)


class AgentLoop:
    def __init__(self, ctx: Any, executor: Any, desk: HelpDesk, *,
                 checkpoint: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 max_turns: int | None = None):
        self.ctx = ctx
        self.ex = executor
        self.desk = desk
        self._checkpoint = checkpoint
        self._sleep = sleep
        # BDR enforces the step cap; this is only a backstop against a
        # misbehaving server handing out actions forever.
        self.max_turns = max_turns or (ctx.type_config.max_steps * 2 + 10)
        self.turns_run = 0

    async def run(self) -> JobResult:
        ctx = self.ctx
        prev = int(ctx.job.turn_count or 0)
        seen = self.desk.absorb_claim(ctx.job.unconsumed_help)
        tool_results: list[ToolResult] = []
        shot_id = None
        since_checkpoint = 0
        if prev:
            ctx.steps.log("note", f"Resuming the model loop after turn {prev}"
                                  f"{' with helper input' if seen else ''}")
        while True:
            page = self.ex.jb.page
            url = None if page is None or page.is_closed() else page.url
            title = None
            if url and url != "about:blank":
                try:
                    title = (await page.title())[:300] or None
                except Exception:
                    title = None
            from ..protocol import UserTurnRequest
            request_model = UserTurnRequest if ctx.job.principal_kind == "user" else TurnRequest
            req = request_model(prev_turn_no=prev, tool_results=tool_results, screenshot_artifact_id=shot_id,
                              url=strip_query(url) if url else None, title=title,
                              helper_message_ids_seen=seen)
            resp = await self._turn(req)
            if isinstance(resp, TurnStopResponse):
                return await self._on_stop(resp)
            assert isinstance(resp, TurnActionResponse)
            if seen:
                await self.desk.consume_seen(seen)
            prev = resp.turn_no
            self.turns_run += 1
            if self.turns_run > self.max_turns:
                ctx.steps.log("error.turns", "Turn backstop reached", ok=False)
                return Finish("failed", "Too many model turns", error_kind="cannot_complete",
                              error="worker turn backstop reached")
            outcome = await self.ex.run_tool(resp.action, turn_no=resp.turn_no)
            if outcome.terminal is not None:
                return outcome.terminal
            tool_results = [ToolResult(call_id=resp.action.call_id, ok=outcome.ok, content=outcome.content)]
            shot_id = outcome.screenshot_artifact_id
            seen = list(outcome.helper_message_ids)
            since_checkpoint += 1
            if since_checkpoint >= CHECKPOINT_EVERY_STEPS and self._checkpoint is not None:
                since_checkpoint = 0
                page = self.ex.jb.page
                if page is not None and not page.is_closed() and page.url != "about:blank":
                    await self._checkpoint({"last_url": strip_query(page.url)})

    async def _turn(self, req: TurnRequest) -> TurnActionResponse | TurnStopResponse:
        waits = 0
        while True:
            resp = await self.ctx.client.turn(self.ctx.lease, req)
            if isinstance(resp, TurnPendingResponse):
                waits += 1
                if waits > MAX_PENDING_WAITS:
                    # Another BDR process holds the turn for too long: give the
                    # job back uncharged and let the next attempt pick it up.
                    raise _PendingTooLong()
                await self._sleep(max(1, int(resp.retry_after)))
                continue
            return resp

    async def _on_stop(self, stop: TurnStopResponse) -> JobResult:
        ctx = self.ctx
        reason = stop.reason
        ctx.steps.log("note", f"BDR stopped the model loop: {reason}")
        if reason == "provider_outage":
            return Requeue("provider_outage", retry_at=stop.retry_at)
        if reason in ("disabled", "canceled"):
            return Finish("canceled", f"Stopped by BDR ({reason})")
        if reason == "secret_leak_blocked":
            return Finish("failed", "BDR blocked a secret in the observation", error_kind="secret_leak_blocked",
                          error="secret_leak_blocked")
        help_reason = "provider_config" if reason == "provider_config" else "cap_reached"
        message = {"cap_reached": "The job reached its step, token, cost or time cap.",
                   "daily_cap_reached": "The global daily spend cap is reached.",
                   "provider_config": "The model provider rejected the configuration."}.get(reason, reason)
        try:
            shot = await self.ex.screenshot(label="stop")
            _resp, thread = await self.desk.open(help_reason, message, shot)
        except BdrRejected as exc:
            return Finish("failed", message, error_kind="cannot_complete", error=f"help refused: {exc.code}")
        if thread is None:
            return Finish("failed", message, error_kind="cannot_complete", error=reason)
        # Nothing to do with the page while capped: free the slot now; a
        # helper Retry (or IT Admin) requeues the job.
        await ctx.client.park(ctx.lease)
        return HandedOff(f"Parked after {reason}")


class _PendingTooLong(Exception):
    pass


async def run_loop(loop: AgentLoop) -> JobResult:
    """Run the loop and turn a turn that never left `thinking` into an
    uncharged requeue."""
    try:
        return await loop.run()
    except _PendingTooLong:
        loop.ctx.steps.log("error.turn_pending", "BDR kept the turn pending too long", ok=False)
        return Requeue("interrupted")
