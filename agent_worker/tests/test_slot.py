"""Slot edge cases: BDR silence for 15 min, bad runner results, run command guards."""

import asyncio

from bdr_agent.__main__ import build_parser, cmd_run, load_runners
from bdr_agent.heartbeat import JobTracker
from bdr_agent.protocol import TypeConfig
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Slot, SlotDeps
from bdr_agent.spool import Spool

from .conftest import hello_and_claim, make_clients

TC = TypeConfig(enabled=True, max_concurrency=1, max_steps=10, max_active_seconds=300)


class Runner:
    needs_browser = False
    version = "1.0.0"

    def __init__(self, result=None, wait=False):
        self.result = result
        self.wait = wait

    async def run(self, ctx):
        if self.wait:
            await asyncio.Event().wait()
        return self.result


def _deps(bdr, worker, ac, paths, runner, **kw):
    return SlotDeps(client=ac, tracker=JobTracker(), spool=Spool(paths.spool_dir), secrets=LiveSecrets(),
                    runners={"canary": runner}, tick=0.01, flush_interval=0.05, **kw)


async def test_bdr_silence_aborts_locally_and_keeps_steps_spooled(bdr, worker, paths):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac, "canary")
    age = [0.0]
    deps = _deps(bdr, worker, ac, paths, Runner(wait=True), contact_age=lambda: age[0],
                 unreachable_abort_seconds=900)
    slot = Slot("canary", 1, deps)
    task = asyncio.create_task(slot.run(job, TC))
    await asyncio.sleep(0.1)
    bdr.down = True
    age[0] = 901.0
    outcome = await asyncio.wait_for(task, 5)
    assert outcome.terminal == "none" and outcome.stop_reason == "unreachable"
    assert not bdr.calls_to("finish") and not bdr.calls_to("requeue")
    left = Spool(paths.spool_dir).for_job(job.job_id, job.epoch).unacked()
    assert any(r["kind"] == "error.bdr_unreachable" for r in left)


async def test_runner_returning_garbage_is_a_charged_crash(bdr, worker, paths):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac, "canary")
    outcome = await Slot("canary", 1, _deps(bdr, worker, ac, paths, Runner(result="ok"))).run(job, TC)
    assert outcome.terminal == "requeue:crash"
    assert bdr.jobs[job.job_id].requeues[0]["reason"] == "crash"


def test_run_command_refuses_without_enrollment(paths, capsys):
    args = build_parser().parse_args(["--home", str(paths.home), "run"])
    assert cmd_run(args) == 2


def test_load_runners_finds_the_agent_types():
    runners, provider = load_runners()
    assert set(runners) == {"rfi_from_email", "canary", "user_browser"}
    assert all(r.needs_browser and r.version == "1.0.0" for r in runners.values())
    assert callable(provider)


class _RaisingRunner(Runner):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    async def run(self, ctx):
        raise self.exc


async def test_bdr_outage_inside_a_job_is_an_uncharged_interruption(bdr, worker, paths):
    """A short BDR outage (deploy) that outlasts one call is 'interrupted', not a charged crash."""
    from bdr_agent.client import BdrUnreachable

    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac, "canary")
    deps = _deps(bdr, worker, ac, paths, _RaisingRunner(BdrUnreachable("turns", "HTTP 502")),
                 contact_age=lambda: 60.0, unreachable_abort_seconds=900)
    outcome = await Slot("canary", 1, deps).run(job, TC)
    assert outcome.terminal == "requeue:interrupted"
    rec = bdr.jobs[job.job_id]
    assert rec.requeues[0]["reason"] == "interrupted"
    assert rec.resume_reason == "interrupted"


async def test_personal_claim_refused_before_browser_runner_or_plaintext_spool(bdr, worker, paths):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac, "canary")
    job = job.model_copy(update={"principal_kind": "user"})
    deps = _deps(bdr, worker, ac, paths, _RaisingRunner(AssertionError("must not run")))
    outcome = await Slot("canary", 1, deps).run(job, TC)
    assert outcome.detail == "principal_mismatch"
    assert outcome.terminal == "none"
    assert not deps.tracker.holds(job.job_id, job.epoch)
    assert not list(paths.spool_dir.glob("**/*.jsonl"))
    assert not bdr.calls_to("turns") and not bdr.calls_to("finish")
