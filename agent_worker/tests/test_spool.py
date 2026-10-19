"""Write-ahead step spool: crash at any line, replay idempotently as late steps."""

import base64
import uuid

import pytest

from bdr_agent.client import Lease
from bdr_agent.redact import LiveSecrets, strip_query
from bdr_agent.spool import JobSpool, Spool, StepLog, replay_spool

from .conftest import hello, hello_and_claim, make_clients


def _secrets(*values):
    s = LiveSecrets()
    for v in values:
        s.add(v)
    return s


async def test_spool_replays_unposted_steps_in_order_after_crash(bdr, worker, paths):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac)
    spool = Spool(paths.spool_dir)
    steps = StepLog(spool.for_job(job.job_id, job.epoch), _secrets(worker.token))
    k1 = steps.log("tool.ensure_login", "Login ok", tool_name="ensure_login",
                   args={"host": "app.procore.com"}, result={"leak": f"Bearer {worker.token}"}, ok=True)
    assert (await steps.flush(ac)).posted == 1

    bdr.down = True    # BDR goes away; the worker keeps logging
    k2 = steps.log("nav", "Opened project home",
                   url_after="https://app.procore.com/1/project/home?token=abc#frag")
    k3 = steps.log("note", "Third step")
    res = await steps.flush(ac)
    assert res.unreachable and res.pending == 2

    path = paths.spool_dir / f"{job.job_id}_{job.epoch}.jsonl"
    raw = path.read_bytes()
    assert worker.token.encode() not in raw and b"[redacted]" in raw
    assert b"token=abc" not in raw
    with open(path, "ab") as fh:     # power loss in the middle of a line
        fh.write(b'{"s": {"idem_key": "torn')

    # New process: new boot, /hello requeues the old lease (epoch bump).
    bdr.down = False
    ac2, _ = make_clients(bdr, worker.token)
    await hello(ac2)
    assert bdr.jobs[job.job_id].status == "queued"
    assert bdr.jobs[job.job_id].epoch == job.epoch + 1

    result = await replay_spool(Spool(paths.spool_dir), ac2)
    assert result.posted == 2 and result.dropped == 0 and result.pending == 0
    assert list(bdr.steps[job.job_id]) == [k1, k2, k3]
    assert bdr.late_steps[job.job_id] == 2
    assert bdr.steps[job.job_id][k2]["url_after"] == "https://app.procore.com/1/project/home"
    assert not path.exists()


async def test_replay_is_idempotent_when_ack_was_lost(bdr, worker, paths):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac)
    sp = Spool(paths.spool_dir).for_job(job.job_id, job.epoch)
    steps = StepLog(sp, _secrets())
    steps.log("note", "posted, but the ack line never hit the disk")
    # BDR accepted the batch, then power was lost before the ack was written.
    await ac.append_steps(Lease(job.job_id, job.epoch), sp.unacked())
    assert len(bdr.steps[job.job_id]) == 1

    ac2, _ = make_clients(bdr, worker.token)
    await hello(ac2)
    result = await replay_spool(Spool(paths.spool_dir), ac2)
    assert result.posted == 1
    assert len(bdr.steps[job.job_id]) == 1
    assert bdr.duplicate_steps[job.job_id] == 1


async def test_replay_drops_rows_bdr_refuses_for_good(bdr, worker, paths):
    ac, _ = make_clients(bdr, worker.token)
    await hello(ac)
    stranger = Spool(paths.spool_dir).for_job(uuid.uuid4(), 7)   # an epoch this worker never held
    StepLog(stranger, _secrets()).log("note", "orphan row")
    result = await replay_spool(Spool(paths.spool_dir), ac)
    assert result.dropped == 1 and result.posted == 0
    assert not stranger.path.exists()


async def test_replay_keeps_rows_while_unreachable_and_skips_live_jobs(bdr, worker, paths):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac)
    spool = Spool(paths.spool_dir)
    StepLog(spool.for_job(job.job_id, job.epoch), _secrets()).log("note", "live job row")
    res = await replay_spool(spool, ac, exclude=lambda j, e: j == job.job_id)
    assert res.posted == 0 and spool.for_job(job.job_id, job.epoch).path.exists()
    bdr.down = True
    res = await replay_spool(Spool(paths.spool_dir), ac)
    assert res.unreachable and spool.for_job(job.job_id, job.epoch).path.exists()


def test_job_spool_survives_torn_line_and_appends_cleanly(tmp_path):
    jid = uuid.uuid4()
    path = tmp_path / f"{jid}_1.jsonl"
    sp = JobSpool(path, jid, 1)
    sp.append([{"idem_key": "a"}])
    with open(path, "ab") as fh:
        fh.write(b'{"s": {"idem_k')
    again = JobSpool(path, jid, 1)
    again.append([{"idem_key": "b"}])
    again.ack(["a"])
    fresh = JobSpool(path, jid, 1)
    assert [r["idem_key"] for r in fresh.unacked()] == ["b"]
    assert fresh.counts() == (2, 1)
    fresh.ack(["b"])
    assert not fresh.remove_if_settled()     # job not marked done yet
    fresh.mark_done()
    assert fresh.remove_if_settled() and not path.exists()


def test_steplog_validates_kind_and_truncates(tmp_path):
    jid = uuid.uuid4()
    steps = StepLog(JobSpool(tmp_path / f"{jid}_1.jsonl", jid, 1), _secrets())
    with pytest.raises(ValueError):
        steps.log("claim", "BDR-only kind")
    steps.log("tool.big", "x" * 900, result={"blob": "y" * 70_000})
    row = steps.spool.unacked()[0]
    assert len(row["summary"]) == 500
    assert row["result"]["truncated"] is True
    assert row["idem_key"] == f"{jid}:1:w1"


def test_live_secrets_scrub_variants():
    secret = "hunter2-very-secret"
    s = _secrets(secret)
    b64 = base64.b64encode(secret.encode()).decode()
    text = f"a {secret} b hunter2-very-secret%21 c {b64} d"
    out = s.scrub_text(text.replace("%21", ""))
    assert secret not in out and b64 not in out
    assert s.scrub({"k": [secret, 1]}) == {"k": ["[redacted]", 1]}
    s.add("short")   # too short to scrub safely
    assert s.scrub_text("short") == "short"
    assert strip_query("https://u:p@x.com/a?b=1#c") == "https://x.com/a"
