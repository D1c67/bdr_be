"""AgentLoop against the fake BDR with a fake executor (no browser): turn
numbering, 202 retries, stop replies, checkpoints, the backstop."""

from dataclasses import dataclass, field

import pytest

from bdr_agent import protocol as P
from bdr_agent.agent import loop as loop_mod
from bdr_agent.agent.help import HelpDesk
from bdr_agent.agent.loop import AgentLoop, run_loop
from bdr_agent.browser.executor import ToolOutcome
from bdr_agent.heartbeat import JobTracker
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Finish, HandedOff, JobContext, Requeue
from bdr_agent.spool import Spool, StepLog

from .agent_bdr import AgentBdr
from .agent_harness import claim


@dataclass
class FakePage:
    url: str = "about:blank"

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Fake"


@dataclass
class FakeJb:
    page: FakePage = field(default_factory=FakePage)


class FakeExecutor:
    def __init__(self, results=None):
        self.jb = FakeJb()
        self.calls: list[tuple[str, int]] = []
        self.results = results or {}

    async def screenshot(self, page=None, *, label="shot"):
        return None

    async def run_tool(self, call, *, turn_no=None, actor="model"):
        self.calls.append((call.tool, turn_no))
        if call.tool == "navigate":
            self.jb.page.url = call.args["url"]
        terminal = self.results.get(call.tool)
        return ToolOutcome(call_id=call.call_id, tool=call.tool, ok=True, content='{"ok":true}', terminal=terminal)


async def _ctx(bdr, paths, *, turn_count=0):
    ac, sc, job = await claim(bdr, "rfi_from_email", turn_count=turn_count)
    tracker = JobTracker()
    tracker.register(job.job_id, job.epoch, job.agent_type)
    steps = StepLog(Spool(paths.spool_dir).for_job(job.job_id, job.epoch), LiveSecrets())
    from bdr_agent.client import Lease

    ctx = JobContext(job=job, lease=Lease(job.job_id, job.epoch), input=P.parse_input(job.agent_type, job.input),
                     type_config=P.TypeConfig.model_validate(bdr.types["rfi_from_email"]), client=ac, steps=steps,
                     secrets=LiveSecrets(), tracker=tracker, browser_context=None, api_host="bdr.test")
    return ctx, ac, sc


async def _no_sleep(_s):
    return None


async def test_numbering_follows_turn_count_and_server(paths):
    bdr = AgentBdr()
    bdr.script = [("read_page", {}), ("scroll", {"direction": "down", "amount": 1}),
                  ("finish", {"outcome": "nothing_to_do", "summary": "x"})]
    ctx, ac, sc = await _ctx(bdr, paths, turn_count=7)
    ex = FakeExecutor({"finish": Finish("succeeded", "x", outcome="nothing_to_do")})
    result = await AgentLoop(ctx, ex, HelpDesk(ctx), sleep=_no_sleep).run()
    assert isinstance(result, Finish) and result.outcome == "nothing_to_do"
    assert [r["prev_turn_no"] for r in bdr.turn_requests] == [7, 8, 9]
    assert ex.calls == [("read_page", 8), ("scroll", 9), ("finish", 10)]
    assert bdr.turn_requests[1]["tool_results"][0]["call_id"] == "call_8"
    await ac.aclose()
    sc.close()


async def test_checkpoint_last_url_every_five(paths):
    bdr = AgentBdr()
    bdr.script = [("navigate", {"url": f"https://app.procore.com/1/project/rfi/show/{i}"}) for i in range(1, 12)]
    ctx, ac, sc = await _ctx(bdr, paths)
    saved = []

    async def checkpoint(data):
        saved.append(data)

    result = await AgentLoop(ctx, FakeExecutor(), HelpDesk(ctx), checkpoint=checkpoint, sleep=_no_sleep).run()
    # Script exhausted -> cap_reached stop -> cap help, then park.
    assert isinstance(result, HandedOff)
    assert saved == [{"last_url": "https://app.procore.com/1/project/rfi/show/5"},
                     {"last_url": "https://app.procore.com/1/project/rfi/show/10"}]
    [help_req] = bdr.helps.values()
    assert help_req["reason"] == "cap_reached"
    await ac.aclose()
    sc.close()


async def test_backstop_ends_a_runaway_server(paths):
    bdr = AgentBdr()
    bdr.script = [("read_page", {}) for _ in range(20)]
    ctx, ac, sc = await _ctx(bdr, paths)
    result = await AgentLoop(ctx, FakeExecutor(), HelpDesk(ctx), sleep=_no_sleep, max_turns=5).run()
    assert isinstance(result, Finish) and result.error_kind == "cannot_complete"
    await ac.aclose()
    sc.close()


async def test_pending_forever_requeues_uncharged(paths, monkeypatch):
    monkeypatch.setattr(loop_mod, "MAX_PENDING_WAITS", 3)
    bdr = AgentBdr()
    bdr.script = ["pending"] * 10
    ctx, ac, sc = await _ctx(bdr, paths)
    result = await run_loop(AgentLoop(ctx, FakeExecutor(), HelpDesk(ctx), sleep=_no_sleep))
    assert result == Requeue("interrupted")
    keys = {c.headers["idempotency-key"] for c in bdr.calls_to("turns")}
    assert len(keys) == 1
    await ac.aclose()
    sc.close()


@pytest.mark.parametrize("stop,expected", [
    ({"stop": "secret_leak_blocked"}, Finish("failed", "BDR blocked a secret in the observation",
                                             error_kind="secret_leak_blocked", error="secret_leak_blocked")),
    ({"stop": "canceled"}, Finish("canceled", "Stopped by BDR (canceled)")),
])
async def test_terminal_stops(paths, stop, expected):
    bdr = AgentBdr()
    bdr.script = [stop]
    ctx, ac, sc = await _ctx(bdr, paths)
    assert await AgentLoop(ctx, FakeExecutor(), HelpDesk(ctx), sleep=_no_sleep).run() == expected
    await ac.aclose()
    sc.close()


async def test_daily_cap_opens_cap_help_and_parks(paths):
    bdr = AgentBdr()
    bdr.script = [{"stop": "daily_cap_reached"}]
    ctx, ac, sc = await _ctx(bdr, paths)
    result = await AgentLoop(ctx, FakeExecutor(), HelpDesk(ctx), sleep=_no_sleep).run()
    assert isinstance(result, HandedOff)
    [help_req] = bdr.helps.values()
    assert help_req["reason"] == "cap_reached" and "daily" in help_req["opened_message"]
    assert bdr.parks == [ctx.job_id]
    await ac.aclose()
    sc.close()
