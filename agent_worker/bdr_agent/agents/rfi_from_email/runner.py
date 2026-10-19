"""JobRunner for `rfi_from_email` (O1: the model drives every job).

Setup per attempt: guard steps and Settings host narrowing on the job's
browser, the checkpoint (spec 4.5), the help desk, the executor with the
Procore site tools, the help tools and finish. Then:

  submitted                     -> nothing to do (BDR already has the capture)
  detail_captured, < 30 min old -> submit /rfi-capture straight away
  anything else                 -> the model loop (AgentLoop)
"""

from typing import Any

from ...agent.help import HelpDesk, HelpTools
from ...agent.loop import AgentLoop, run_loop
from ...browser.executor import Executor
from ...slot import HandedOff, JobResult
from ..pm_apps.procore.identity import ProcoreIdentity
from .tools import RfiState, RfiTools

VERSION = "1.0.0"


class RfiFromEmailRunner:
    version = VERSION
    needs_browser = True

    def __init__(self, *, executor_factory: Any = Executor, help_poll_seconds: float | None = None):
        self._executor_factory = executor_factory
        self._help_poll_seconds = help_poll_seconds

    async def run(self, ctx: Any) -> JobResult:
        jb = ctx.browser_context
        jb.attach_steps(ctx.steps.log)
        jb.apply_narrowing(ctx.type_config.host_narrowing)
        state = RfiState.load(ctx)
        if state.ckpt.phase == "submitted":
            ctx.steps.log("note", "The capture was already submitted; nothing to do")
            return HandedOff("already submitted")

        desk_kw: dict[str, Any] = {"contributions_sent": list(state.ckpt.contributions_sent),
                                   "on_contribution": state.add_contribution}
        if self._help_poll_seconds is not None:
            desk_kw["poll_seconds"] = self._help_poll_seconds
        desk = HelpDesk(ctx, **desk_kw)
        executor = self._executor_factory(ctx, jb)

        async def session_ready() -> None:
            await state.save(phase="session_ready")

        identity = ProcoreIdentity(ctx, executor, desk, host=state.seed.host, on_session_ready=session_ready)
        tools = RfiTools(ctx, executor, state, identity)
        executor.extra_tools.update(tools.handlers())
        executor.extra_tools.update(HelpTools(desk, executor, rfi_id=state.rfi_id).handlers())

        if state.fresh_capture():
            ctx.steps.log("note", "Resuming with a fresh capture; submitting it")
            return await tools.submit_capture()
        return await run_loop(AgentLoop(ctx, executor, desk, checkpoint=state.save_last_url))
