"""Shared plumbing for the browser and agent tests: the fixture site, a real
headless Chromium pointed at it, and a helper that runs one claimed job
through the W1 Slot exactly as the supervisor would."""

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from bdr_agent import protocol as P
from bdr_agent.heartbeat import JobTracker
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Slot, SlotDeps, SlotOutcome
from bdr_agent.spool import Spool

from .agent_bdr import AgentBdr
from .conftest import make_clients

SEED = {
    "platform": "procore", "host": "app.procore.com", "external_project_id": "3167322",
    "rfi_id": "27914290", "company_id": "8089", "rfi_number_hint": "Const-198", "event_hint": "closed",
    "email_message_at": "2026-10-19T14:58:00Z", "source_email_ids": [],
}


def browser_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


needs_browser = pytest.mark.skipif(not browser_available(), reason="playwright is not installed in this venv")


async def make_provider(site: Any) -> Any:
    from bdr_agent.agents import ceiling_for
    from bdr_agent.browser.browser import ChromiumProvider

    provider = ChromiumProvider(ceilings=ceiling_for, api_host="bdr.test", proxy={"server": site.proxy_url},
                                ignore_https_errors=True)
    await provider.start()
    return provider


@dataclass
class JobRun:
    outcome: SlotOutcome
    job: Any
    secrets: LiveSecrets

    def steps(self, bdr: AgentBdr) -> list[dict[str, Any]]:
        return list(bdr.steps.get(self.job.job_id, {}).values())


async def claim(bdr: AgentBdr, agent_type: str, *, input: dict[str, Any] | None = None,
                checkpoint: dict[str, Any] | None = None, checkpoint_seq: int = 0, turn_count: int = 0,
                unconsumed: list[dict[str, Any]] | None = None, worker: Any = None):
    from .conftest import hello

    worker = worker or bdr.add_worker()
    ac, sc = make_clients(bdr, worker.token)
    await hello(ac)
    job_id = bdr.add_job(agent_type, input)
    job = bdr.jobs[job_id]
    if checkpoint is not None:
        job.checkpoint, job.checkpoint_seq = checkpoint, checkpoint_seq
    resp = await ac.claim(P.ClaimRequest(boot_id=ac.boot_id, capacity={agent_type: 1}))
    claimed = resp.jobs[0]
    if turn_count or unconsumed:
        claimed = claimed.model_copy(update={
            "turn_count": turn_count,
            "unconsumed_help": [P.UnconsumedHelp.model_validate(u) for u in (unconsumed or [])]})
    return ac, sc, claimed


async def run_job(bdr: AgentBdr, provider: Any, paths: Any, agent_type: str, *, runners: dict[str, Any] | None = None,
                  input: dict[str, Any] | None = None, checkpoint: dict[str, Any] | None = None,
                  checkpoint_seq: int = 0, turn_count: int = 0, unconsumed: list[dict[str, Any]] | None = None,
                  secrets: LiveSecrets | None = None) -> JobRun:
    from bdr_agent.agents import REGISTRY

    ac, sc, claimed = await claim(bdr, agent_type, input=input, checkpoint=checkpoint,
                                  checkpoint_seq=checkpoint_seq, turn_count=turn_count, unconsumed=unconsumed)
    secrets = secrets or LiveSecrets()
    deps = SlotDeps(client=ac, tracker=JobTracker(), spool=Spool(paths.spool_dir), secrets=secrets,
                    runners=runners or REGISTRY, browser=provider, api_host="bdr.test", flush_interval=0.2,
                    tick=0.1)
    slot = Slot(agent_type, 1, deps)
    tc = P.TypeConfig.model_validate(bdr.types[agent_type])
    outcome = await slot.run(claimed, tc)
    await ac.aclose()
    sc.close()
    return JobRun(outcome, bdr.jobs[claimed.job_id], secrets)


def new_uuid() -> str:
    return str(uuid.uuid4())
