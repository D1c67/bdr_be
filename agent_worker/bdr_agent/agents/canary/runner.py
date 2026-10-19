"""JobRunner for `canary` (spec 12.4 M1): the model opens BDR's static
canary page, reads it and finishes. The only reachable page is
GET <api host>/agent-worker/v1/canary/page (protocol.CEILINGS["canary"]).

finish(nothing_to_do) becomes succeeded/self_test_ok only when the worker
itself sees the expected text on the current page; otherwise the self-test
failed (cannot_complete).
"""

from typing import Any

from ...agent.help import HelpDesk
from ...agent.loop import AgentLoop, run_loop
from ...browser.executor import Executor, ToolReturn
from ...browser.guard import Ceiling
from ...protocol import finish_to_job
from ...slot import Finish

VERSION = "1.0.0"


def ceiling(agent_type: str = "canary", api_host: str = "") -> Ceiling:
    return Ceiling.from_protocol(agent_type, api_host=api_host)


class CanaryRunner:
    version = VERSION
    needs_browser = True

    def __init__(self, *, executor_factory: Any = Executor):
        self._executor_factory = executor_factory

    async def run(self, ctx: Any) -> Any:
        jb = ctx.browser_context
        jb.attach_steps(ctx.steps.log)
        jb.apply_narrowing(ctx.type_config.host_narrowing)
        executor = self._executor_factory(ctx, jb)
        expect = ctx.input.expect_text

        async def finish(args: Any) -> ToolReturn:
            action = finish_to_job("canary", args)
            if action["status"] == "failed":
                return ToolReturn({"finished": True}, screenshot=False, terminal=Finish(
                    "failed", args.summary or "Self-test failed", error_kind=action["error_kind"],
                    error=args.summary or None))
            page = jb.page
            text = ""
            if page is not None and not page.is_closed() and page.url != "about:blank":
                try:
                    text = await page.inner_text("body", timeout=5_000)
                except Exception:
                    text = ""
            if expect not in text:
                ctx.steps.log("error.canary", "The canary text is not on the current page", ok=False)
                return ToolReturn({"finished": True, "found": False}, ok=False, screenshot=False, terminal=Finish(
                    "failed", "The canary text was not on the page", error_kind="cannot_complete",
                    error="expected text not found"))
            return ToolReturn({"finished": True, "found": True}, screenshot=False,
                              terminal=Finish("succeeded", args.summary or "Self-test ok", outcome="self_test_ok"))

        executor.extra_tools["finish"] = finish
        return await run_loop(AgentLoop(ctx, executor, HelpDesk(ctx)))
