"""Heartbeat thread: renew by epoch, lost/cancel/orphans/help/config/drain, stall watchdog."""

import asyncio
import threading
import time
import uuid

from bdr_agent.client import StaleBoot
from bdr_agent.heartbeat import HeartbeatThread, JobTracker
from bdr_agent.redact import LiveSecrets
from bdr_agent.spool import Spool, StepLog

from .conftest import hello_and_claim, make_clients


class Sink:
    def __init__(self):
        self.lost, self.cancel, self.help, self.config, self.drain, self.fatal = [], [], [], [], [], []

    def on_lost(self, job_id):
        self.lost.append(job_id)

    def on_cancel(self, job_id):
        self.cancel.append(job_id)

    def on_help_update(self, update):
        self.help.append(update)

    def on_config_version(self, version):
        self.config.append(version)

    def on_drain(self, draining):
        self.drain.append(draining)

    def on_fatal(self, exc):
        self.fatal.append(exc)


def _setup(bdr, worker, agent_type="rfi_from_email"):
    ac, sc = make_clients(bdr, worker.token)
    job = asyncio.run(hello_and_claim(bdr, ac, agent_type))
    sc.boot_id = ac.boot_id
    return ac, sc, job


def _thread(sc, tracker, sink, **kw):
    return HeartbeatThread(sc, tracker, sink, boot_id=sc.boot_id, config_version=lambda: 1,
                           slots_free=lambda: {"rfi_from_email": 1}, **kw)


def test_beat_renews_and_routes_signals(bdr, worker):
    _ac, sc, job = _setup(bdr, worker)
    tracker, sink = JobTracker(), Sink()
    tracker.register(job.job_id, job.epoch, job.agent_type)
    tracker.set_phase(job.job_id, "session_ready")
    other = uuid.uuid4()
    bdr.hb_cancel = [job.job_id]
    bdr.hb_lost = [other]
    bdr.hb_help_updates = [{"job_id": str(job.job_id), "help_id": str(uuid.uuid4()),
                            "latest_message_id": 9, "status": "answered"}]
    worker.draining = True
    resp = _thread(sc, tracker, sink).beat_once()
    assert resp is not None and resp.renewed[0].job_id == job.job_id
    assert tracker.get(job.job_id).lease_expires_at is not None
    assert sink.cancel == [job.job_id] and sink.lost == [other]
    assert sink.help[0].latest_message_id == 9
    assert sink.config == [bdr.config_version] and sink.drain == [True]
    sent = bdr.calls_to("heartbeat")[-1].body
    assert sent["jobs"][0]["epoch"] == job.epoch and sent["jobs"][0]["phase"] == "session_ready"
    assert sent["slots_free"] == {"rfi_from_email": 1}


def test_wrong_epoch_is_reported_lost(bdr, worker):
    _ac, sc, job = _setup(bdr, worker)
    tracker, sink = JobTracker(), Sink()
    tracker.register(job.job_id, job.epoch + 5, job.agent_type)
    _thread(sc, tracker, sink).beat_once()
    assert sink.lost == [job.job_id]


def test_orphans_released_only_on_second_sighting_and_not_during_claims(bdr, worker):
    _ac, sc, job = _setup(bdr, worker)
    tracker, sink = JobTracker(), Sink()   # the process does not run the job
    hb = _thread(sc, tracker, sink)
    hb.beat_once()
    assert bdr.jobs[job.job_id].status == "running" and not bdr.calls_to("requeue")
    tracker.claim_started()
    hb.beat_once()
    assert not bdr.calls_to("requeue")
    tracker.claim_finished()
    hb.beat_once()
    hb.beat_once()
    req = bdr.calls_to("requeue")
    assert req and req[-1].body["reason"] == "interrupted"
    assert bdr.jobs[job.job_id].status == "queued"


def test_stale_boot_is_fatal(bdr, worker):
    _ac, sc, job = _setup(bdr, worker)
    tracker, sink = JobTracker(), Sink()
    worker.boot_id = uuid.uuid4()   # another process said hello
    assert _thread(sc, tracker, sink).beat_once() is None
    assert isinstance(sink.fatal[0], StaleBoot)


def test_active_seconds_pause_while_waiting_and_clamp(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("bdr_agent.heartbeat.time.monotonic", lambda: clock[0])
    t = JobTracker()
    jid = uuid.uuid4()
    t.register(jid, 1, "canary")
    clock[0] += 10
    assert t.beat_snapshot()[0].active_seconds_delta == 10
    t.set_active(jid, False)
    clock[0] += 100
    assert t.beat_snapshot()[0].active_seconds_delta == 0
    t.set_active(jid, True)
    clock[0] += 45
    assert t.beat_snapshot()[0].active_seconds_delta == 30    # clamped, rest carried
    assert t.beat_snapshot()[0].active_seconds_delta == 15


def test_heartbeat_survives_a_blocked_loop_and_watchdog_logs_and_exits(bdr, worker, paths):
    _ac, sc, job = _setup(bdr, worker)
    tracker, sink = JobTracker(), Sink()
    tracker.register(job.job_id, job.epoch, job.agent_type)
    steps = StepLog(Spool(paths.spool_dir).for_job(job.job_id, job.epoch), LiveSecrets())
    tracker.attach_steps(job.job_id, steps)

    loop = asyncio.new_event_loop()
    lt = threading.Thread(target=loop.run_forever, daemon=True)
    lt.start()
    exits = []
    hb = _thread(sc, tracker, sink, loop=loop, interval=0.05, probe_interval=0.01,
                 stall_seconds=0.15, stall_exit_seconds=0.6, exit_fn=exits.append)
    try:
        hb.start()
        time.sleep(0.15)
        beats_before = hb.beats_ok
        loop.call_soon_threadsafe(time.sleep, 0.9)    # block the loop hard
        time.sleep(0.8)
        assert hb.beats_ok >= beats_before + 5        # renewals kept going
        kinds = [r["kind"] for r in bdr.steps.get(job.job_id, {}).values()]
        assert "browser.loop_stall" in kinds
        assert exits and exits[0] == 1
        time.sleep(0.3)
        lag = [c.body["loop_lag_ms"] for c in bdr.calls_to("heartbeat")]
        assert max(lag) >= 150
    finally:
        hb.stop()
        loop.call_soon_threadsafe(loop.stop)
        lt.join(2)
        loop.close()
    assert not hb.is_alive()
