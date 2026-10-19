"""Supervisor end to end against the mock BDR: pools, multi-type slots, lease
loss, cancel, shutdown, crash, timeout, stale boot, approval, drain, config."""

import asyncio
import uuid

import pytest

from bdr_agent.heartbeat import HeartbeatThread
from bdr_agent.protocol import ConfigResponse
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Finish, HandedOff, Requeue
from bdr_agent.spool import Spool, StepLog
from bdr_agent.supervisor import EXIT_REVOKED, Supervisor, compute_pools, round_robin_capacity

from .conftest import hello, make_clients

OUTCOME = {"rfi_from_email": "nothing_to_do", "canary": "self_test_ok"}


class Counter:
    def __init__(self):
        self.running: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.peak_total = 0

    def enter(self, t):
        self.running[t] = self.running.get(t, 0) + 1
        self.peak[t] = max(self.peak.get(t, 0), self.running[t])
        self.peak_total = max(self.peak_total, sum(self.running.values()))

    def leave(self, t):
        self.running[t] -= 1


class FakeRunner:
    needs_browser = False

    def __init__(self, counter: Counter, *, hold: bool = False, crash_first: bool = False,
                 result=None, version: str = "1.0.0"):
        self.version = version
        self.counter = counter
        self.hold = hold
        self.crash_first = crash_first
        self.result = result
        self.started: list[uuid.UUID] = []
        self.cancelled: list[uuid.UUID] = []
        self.contexts = []
        self.gate: asyncio.Event | None = None

    def release(self):
        if self.gate is not None:
            self.gate.set()

    async def run(self, ctx):
        if self.gate is None:
            self.gate = asyncio.Event()
            if not self.hold:
                self.gate.set()
        self.started.append(ctx.job_id)
        self.contexts.append(ctx)
        self.counter.enter(ctx.agent_type)
        try:
            ctx.steps.log("tool.fake", f"working on {ctx.agent_type}", tool_name="fake", ok=True)
            await ctx.save_checkpoint({"phase": "session_ready"})
            if self.crash_first and len(self.started) == 1:
                raise RuntimeError(f"boom {ctx.secrets.scrub_text('x')}")
            await self.gate.wait()
            if self.result is not None:
                return self.result
            return Finish("succeeded", "done", outcome=OUTCOME[ctx.agent_type])
        except asyncio.CancelledError:
            self.cancelled.append(ctx.job_id)
            raise
        finally:
            self.counter.leave(ctx.agent_type)


def fast_heartbeat(*args, **kw):
    kw.update(interval=0.05, probe_interval=0.02, exit_fn=lambda _c: None)
    return HeartbeatThread(*args, **kw)


def make_supervisor(bdr, worker, paths, runners, **kw):
    ac, sc = make_clients(bdr, worker.token)
    secrets = LiveSecrets()
    secrets.add(worker.token)
    opts = dict(claim_interval=0.02, config_poll_seconds=0.05, hello_retry_seconds=0.05, shutdown_grace=2.0,
                slot_tick=0.02, flush_interval=0.05, replay_interval=0.1, heartbeat_factory=fast_heartbeat)
    opts.update(kw)
    return Supervisor(client=ac, hb_client=sc, paths=paths, runners=runners, version="1.0.0",
                      secrets=secrets, api_host="bdr.test", **opts)


async def until(pred, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def stop(sup, task):
    sup.request_shutdown()
    return await asyncio.wait_for(task, 10)


# Pure sizing ----------------------------------------------------------------------------


def _cfg(bdr, worker) -> ConfigResponse:
    return ConfigResponse.model_validate(bdr._config_body(worker))


def test_compute_pools_respects_enabled_allowed_and_runners(bdr, worker):
    assert compute_pools(_cfg(bdr, worker), {"rfi_from_email", "canary"}) == {"rfi_from_email": 2, "canary": 1}
    worker.max_slots = 1
    assert compute_pools(_cfg(bdr, worker), {"rfi_from_email", "canary"}) == {"rfi_from_email": 1, "canary": 1}
    worker.allowed_types = ["canary"]
    assert compute_pools(_cfg(bdr, worker), {"rfi_from_email", "canary"}) == {"canary": 1}
    bdr.types["canary"]["enabled"] = False
    assert compute_pools(_cfg(bdr, worker), {"rfi_from_email", "canary"}) == {}
    bdr.enabled = False
    worker.allowed_types = ["canary", "rfi_from_email"]
    assert compute_pools(_cfg(bdr, worker), {"rfi_from_email"}) == {}
    assert compute_pools(None, {"canary"}) == {}


def test_round_robin_capacity():
    pools = {"a": 2, "b": 1, "c": 3}
    assert round_robin_capacity(pools, {}, 3, 0) == {"a": 1, "b": 1, "c": 1}
    assert round_robin_capacity(pools, {}, 2, 1) == {"b": 1, "c": 1}
    assert round_robin_capacity(pools, {"a": 2, "b": 1}, 5, 0) == {"c": 3}
    assert round_robin_capacity(pools, {}, 0, 0) == {}
    assert sum(round_robin_capacity(pools, {}, 10, 2).values()) == 6


# End to end ---------------------------------------------------------------------------


async def test_multiple_agent_types_run_side_by_side_within_pools(bdr, worker, paths):
    counter = Counter()
    rfi, canary = FakeRunner(counter, hold=True), FakeRunner(counter, hold=True)
    jobs = [bdr.add_job("rfi_from_email") for _ in range(3)] + [bdr.add_job("canary") for _ in range(2)]
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi, "canary": canary})
    task = asyncio.create_task(sup.run())
    await until(lambda: counter.running.get("rfi_from_email") == 2 and counter.running.get("canary") == 1)
    await asyncio.sleep(0.2)   # several claim/heartbeat rounds while full
    assert counter.peak == {"rfi_from_email": 2, "canary": 1} and counter.peak_total == 3
    for body in bdr.claim_requests:
        cap = body["capacity"]
        assert cap.get("rfi_from_email", 0) <= 2 and cap.get("canary", 0) <= 1 and sum(cap.values()) <= 3
    rfi.release()
    canary.release()
    await until(lambda: all(bdr.jobs[j].status == "succeeded" for j in jobs))
    assert await stop(sup, task) == 0
    assert {bdr.jobs[j].finish["outcome"] for j in jobs} == {"nothing_to_do", "self_test_ok"}
    assert all(bdr.jobs[j].checkpoint == {"phase": "session_ready"} for j in jobs)
    # Every step reached BDR and the spool emptied.
    assert all(any(r["kind"] == "tool.fake" for r in bdr.steps[j].values()) for j in jobs)
    assert not list(paths.spool_dir.glob("*.jsonl"))
    assert not any(bdr.late_steps.values())   # steps land before the terminal call, never late
    hb = bdr.calls_to("heartbeat")
    assert hb and any(c.body["jobs"] for c in hb)


async def test_lease_loss_aborts_without_terminal_call(bdr, worker, paths):
    counter = Counter()
    rfi = FakeRunner(counter, hold=True)
    jid = bdr.add_job("rfi_from_email")
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi})
    task = asyncio.create_task(sup.run())
    await until(lambda: rfi.started)
    with bdr.lock:   # the sweeper reclaimed it and another attempt failed it
        job = bdr.jobs[jid]
        job.status, job.lease_owner, job.lease_boot = "failed", None, None
        job.epoch += 1
    await until(lambda: rfi.cancelled)
    await until(lambda: not sup._slots)
    assert not [c for c in bdr.calls_to("finish") + bdr.calls_to("requeue") if str(jid) in c.path]
    assert await stop(sup, task) == 0
    assert bdr.jobs[jid].status == "failed"
    # Steps of the lost epoch still arrived (late or before the loss).
    assert any(r["kind"] == "tool.fake" for r in bdr.steps[jid].values())


async def test_cancel_from_heartbeat_finishes_canceled(bdr, worker, paths):
    counter = Counter()
    canary = FakeRunner(counter, hold=True)
    jid = bdr.add_job("canary")
    sup = make_supervisor(bdr, worker, paths, {"canary": canary})
    task = asyncio.create_task(sup.run())
    await until(lambda: canary.started)
    bdr.jobs[jid].cancel_requested = True
    await until(lambda: bdr.jobs[jid].status == "canceled")
    assert bdr.jobs[jid].finish["status"] == "canceled"
    assert await stop(sup, task) == 0


async def test_shutdown_requeues_running_jobs_uncharged(bdr, worker, paths):
    counter = Counter()
    rfi, canary = FakeRunner(counter, hold=True), FakeRunner(counter, hold=True)
    a, b = bdr.add_job("rfi_from_email"), bdr.add_job("canary")
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi, "canary": canary})
    task = asyncio.create_task(sup.run())
    await until(lambda: rfi.started and canary.started)
    assert await stop(sup, task) == 0
    for jid in (a, b):
        job = bdr.jobs[jid]
        assert job.status == "queued" and job.requeues == [
            {"reason": "shutdown", "retry_at": None, "service_stop": True}]
    assert sup.heartbeat is not None and not sup.heartbeat.is_alive()
    assert not list(paths.spool_dir.glob("*.jsonl"))


async def test_runner_crash_requeues_charged_then_retry_succeeds(bdr, worker, paths):
    counter = Counter()
    rfi = FakeRunner(counter, crash_first=True)
    jid = bdr.add_job("rfi_from_email")
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi})
    task = asyncio.create_task(sup.run())
    await until(lambda: bdr.jobs[jid].status == "succeeded")
    assert await stop(sup, task) == 0
    job = bdr.jobs[jid]
    assert job.requeues[0]["reason"] == "crash" and job.attempts == 2
    crash = [r for r in bdr.steps[jid].values() if r["kind"] == "error.crash"]
    assert crash and crash[0]["result"]["type"] == "RuntimeError"


async def test_active_time_cap_finishes_failed(bdr, worker, paths):
    bdr.types["canary"]["max_active_seconds"] = 1
    counter = Counter()
    canary = FakeRunner(counter, hold=True)
    jid = bdr.add_job("canary")
    sup = make_supervisor(bdr, worker, paths, {"canary": canary})
    task = asyncio.create_task(sup.run())
    await until(lambda: bdr.jobs[jid].status == "failed", timeout=5)
    assert bdr.jobs[jid].finish["error_kind"] == "cannot_complete"
    assert await stop(sup, task) == 0


async def test_handed_off_and_bad_input_and_unknown_type(bdr, worker, paths):
    counter = Counter()
    rfi = FakeRunner(counter, result=HandedOff("capture submitted"))
    handed = bdr.add_job("rfi_from_email")
    bad = bdr.add_job("rfi_from_email", {"platform": "procore"})
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi})
    task = asyncio.create_task(sup.run())
    await until(lambda: bdr.jobs[bad].status == "failed" and not sup._slots and bdr.jobs[handed].epoch >= 1)
    await until(lambda: any(o.terminal == "handed_off" for o in sup.outcomes))
    assert await stop(sup, task) == 0
    assert bdr.jobs[bad].finish["error_kind"] == "unsupported"
    assert bdr.jobs[handed].finish is None and not bdr.jobs[handed].requeues


async def test_stale_boot_exits_with_code_3_without_terminal_calls(bdr, worker, paths):
    counter = Counter()
    rfi = FakeRunner(counter, hold=True)
    jid = bdr.add_job("rfi_from_email")
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi})
    task = asyncio.create_task(sup.run())
    await until(lambda: rfi.started)
    worker.boot_id = uuid.uuid4()   # a second process said hello
    code = await asyncio.wait_for(task, 10)
    assert code == 3
    assert not bdr.calls_to("finish") and not bdr.calls_to("requeue")
    assert rfi.cancelled == [jid]


async def test_revoked_worker_exits_4(bdr, worker, paths):
    worker.status = "revoked"
    sup = make_supervisor(bdr, worker, paths, {})
    assert await asyncio.wait_for(sup.run(), 5) == EXIT_REVOKED


async def test_pending_worker_waits_for_approval_then_claims(bdr, worker, paths):
    worker.status = "pending"
    counter = Counter()
    canary = FakeRunner(counter)
    jid = bdr.add_job("canary")
    sup = make_supervisor(bdr, worker, paths, {"canary": canary})
    task = asyncio.create_task(sup.run())
    await until(lambda: len(bdr.calls_to("config")) >= 3)
    assert not bdr.calls_to("claim")
    worker.status = "active"
    await until(lambda: bdr.jobs[jid].status == "succeeded")
    assert await stop(sup, task) == 0


async def test_new_boot_requeues_old_leases_and_replays_old_spool(bdr, worker, paths):
    # A previous process claimed the job, logged a step it never posted, then lost power.
    ac, _ = make_clients(bdr, worker.token)
    from bdr_agent.protocol import ClaimRequest
    boot = await hello(ac, types={"canary": "1.0.0"})
    jid = bdr.add_job("canary")
    old = (await ac.claim(ClaimRequest(boot_id=boot, capacity={"canary": 1}))).jobs[0]
    StepLog(Spool(paths.spool_dir).for_job(old.job_id, old.epoch), LiveSecrets()).log(
        "note", "written just before the power cut")

    counter = Counter()
    canary = FakeRunner(counter)
    sup = make_supervisor(bdr, worker, paths, {"canary": canary})
    task = asyncio.create_task(sup.run())
    await until(lambda: bdr.jobs[jid].status == "succeeded")
    await until(lambda: any(r["summary"] == "written just before the power cut" for r in bdr.steps.get(jid, {}).values()))
    assert await stop(sup, task) == 0
    assert bdr.jobs[jid].interrupted_count == 1 and bdr.jobs[jid].epoch == old.epoch + 2
    assert bdr.jobs[jid].attempts == 1   # interrupted is uncharged
    assert canary.contexts[0].job.last_resume_reason == "interrupted"
    replayed = [r for r in bdr.steps[jid].values() if r["summary"] == "written just before the power cut"]
    assert replayed[0]["_late"] is True and bdr.late_steps[jid] == 1
    assert not list(paths.spool_dir.glob("*.jsonl"))


async def test_config_change_is_picked_up_from_heartbeat(bdr, worker, paths):
    worker.allowed_types = ["canary"]
    counter = Counter()
    rfi = FakeRunner(counter)
    jid = bdr.add_job("rfi_from_email")
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi})
    task = asyncio.create_task(sup.run())
    await until(lambda: len(bdr.calls_to("heartbeat")) >= 2)
    assert bdr.jobs[jid].status == "queued" and sup.pools() == {}
    worker.allowed_types = ["canary", "rfi_from_email"]
    bdr.bump_config()
    await until(lambda: bdr.jobs[jid].status == "succeeded")
    assert sup.config.config_version == bdr.config_version
    assert await stop(sup, task) == 0


async def test_drain_stops_claims_and_requeues_after_the_drain_window(bdr, worker, paths):
    counter = Counter()
    canary = FakeRunner(counter, hold=True)
    a = bdr.add_job("canary")
    sup = make_supervisor(bdr, worker, paths, {"canary": canary}, drain_max_seconds=0.3)
    task = asyncio.create_task(sup.run())
    await until(lambda: canary.started)
    worker.draining = True
    b = bdr.add_job("canary")
    await until(lambda: bdr.jobs[a].requeues, timeout=5)
    assert bdr.jobs[a].requeues[0]["reason"] == "drain"
    claims_before = len(bdr.claim_requests)
    await asyncio.sleep(0.2)
    assert len(bdr.claim_requests) == claims_before and bdr.jobs[b].status == "queued"
    assert await stop(sup, task) == 0


async def test_help_updates_reach_the_running_job(bdr, worker, paths):
    counter = Counter()
    rfi = FakeRunner(counter, hold=True)
    jid = bdr.add_job("rfi_from_email")
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi})
    task = asyncio.create_task(sup.run())
    await until(lambda: rfi.contexts)
    help_id = uuid.uuid4()
    bdr.hb_help_updates = [{"job_id": str(jid), "help_id": str(help_id), "latest_message_id": 3,
                            "status": "answered"}]
    upd = await asyncio.wait_for(rfi.contexts[0].help_updates.get(), 3)
    assert upd.help_id == help_id and upd.status == "answered"
    assert await stop(sup, task) == 0


@pytest.mark.parametrize("result", [Requeue("login_spacing")])
async def test_runner_requested_requeue(bdr, worker, paths, result):
    counter = Counter()
    canary = FakeRunner(counter, result=result)
    jid = bdr.add_job("canary")
    sup = make_supervisor(bdr, worker, paths, {"canary": canary}, claim_interval=0.5)
    task = asyncio.create_task(sup.run())
    await until(lambda: bdr.jobs[jid].requeues)
    assert await stop(sup, task) == 0
    assert bdr.jobs[jid].requeues[0]["reason"] == "login_spacing"


class FakeBrowser:
    def __init__(self):
        self.open: dict[int, uuid.UUID] = {}
        self.created = 0
        self.closed: list[uuid.UUID] = []
        self.peak_open = 0

    async def new_context(self, job):
        self.created += 1
        ctx_id = self.created
        self.open[ctx_id] = job.job_id
        self.peak_open = max(self.peak_open, len(self.open))
        return ctx_id

    async def close_context(self, ctx_id):
        self.closed.append(self.open.pop(ctx_id))


async def test_each_job_gets_its_own_browser_context_closed_before_finish(bdr, worker, paths):
    counter = Counter()
    rfi, canary = FakeRunner(counter, hold=True), FakeRunner(counter, hold=True)
    rfi.needs_browser = canary.needs_browser = True
    browser = FakeBrowser()
    a, b = bdr.add_job("rfi_from_email"), bdr.add_job("canary")
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi, "canary": canary}, browser=browser)
    task = asyncio.create_task(sup.run())
    await until(lambda: rfi.started and canary.started)
    assert browser.peak_open == 2
    assert {c.browser_context for c in rfi.contexts + canary.contexts} == {1, 2}
    rfi.release()
    canary.release()
    await until(lambda: bdr.jobs[a].status == "succeeded" and bdr.jobs[b].status == "succeeded")
    assert await stop(sup, task) == 0
    assert sorted(map(str, browser.closed)) == sorted(map(str, (a, b))) and not browser.open


async def test_browser_types_are_not_advertised_without_a_browser(bdr, worker, paths):
    counter = Counter()
    rfi = FakeRunner(counter)
    rfi.needs_browser = True
    canary = FakeRunner(counter)
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi, "canary": canary})
    assert set(sup.runners) == {"canary"}
    task = asyncio.create_task(sup.run())
    await until(lambda: worker.capabilities is not None)
    assert set(worker.capabilities["types"]) == {"canary"}
    assert await stop(sup, task) == 0


def test_boot_hygiene_leaves_the_live_chromium_temp_dir(bdr, worker, paths):
    """Chromium starts before the supervisor; hygiene must not delete its
    running profile or the TMP dir the process (and the updater) inherit."""
    paths.ensure()
    live = paths.chromium_tmp_dir
    live.mkdir(parents=True, exist_ok=True)
    (live / "profile.lock").write_text("in use")
    (paths.tmp_dir / "update").mkdir()
    (paths.tmp_dir / "update" / "old.zip").write_text("x")
    (paths.tmp_dir / "stray.txt").write_text("x")
    sup = make_supervisor(bdr, worker, paths, {})
    sup._boot_hygiene()
    assert (live / "profile.lock").exists()
    assert not (paths.tmp_dir / "update").exists() and not (paths.tmp_dir / "stray.txt").exists()
