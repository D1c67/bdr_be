"""client.py against the mock BDR: headers, retries, idempotency, error mapping."""

import uuid

import httpx
import pytest

from bdr_agent.client import (
    BdrRejected,
    BdrUnreachable,
    Lease,
    LeaseLost,
    PendingApproval,
    StaleBoot,
    WorkerRevoked,
    WorkerUnauthorized,
    steps_idempotency_key,
)
from bdr_agent.protocol import (
    ClaimRequest,
    ConfigResponse,
    FinishRequest,
    PendingResponse,
    RequeueRequest,
    TurnActionResponse,
    TurnPendingResponse,
    TurnRequest,
)

from .conftest import hello, hello_and_claim, make_clients


async def test_hello_then_claim_sends_auth_boot_and_idempotency(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    boot = await hello(ac)
    assert worker.boot_id == boot
    await ac.claim(ClaimRequest(boot_id=boot, capacity={"canary": 1}))
    call = bdr.calls_to("claim")[-1]
    assert call.headers["authorization"] == f"Bearer {worker.token}"
    assert call.headers["x-agent-boot"] == str(boot)
    assert call.headers["idempotency-key"]
    assert "x-agent-lease" not in call.headers


async def test_job_call_carries_lease_and_reuses_idempotency_key_across_retries(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac)
    bdr.inject("finish", 503, httpx.ConnectError("boom"))
    lease = Lease(job.job_id, job.epoch)
    await ac.finish(lease, FinishRequest(status="succeeded", outcome="nothing_to_do", summary="done"))
    calls = bdr.calls_to("finish")
    assert [c.status for c in calls] == [503, 204]   # the ConnectError never reached the record
    keys = {c.headers["idempotency-key"] for c in calls}
    assert len(keys) == 1
    assert calls[-1].headers["x-agent-lease"] == f"{job.job_id}:{job.epoch}"
    assert bdr.jobs[job.job_id].status == "succeeded"


async def test_wrong_boot_is_stale_boot(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    await hello(ac)
    ac.boot_id = uuid.uuid4()
    with pytest.raises(StaleBoot):
        await ac.claim(ClaimRequest(boot_id=ac.boot_id, capacity={"canary": 1}))


async def test_wrong_epoch_is_lease_lost_and_job_not_running_maps_to_lease_lost(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac)
    with pytest.raises(LeaseLost) as ei:
        await ac.requeue(Lease(job.job_id, job.epoch + 1), RequeueRequest(reason="crash"))
    assert ei.value.code == "lease_lost"
    bdr.inject("park", "job_not_running")
    with pytest.raises(LeaseLost) as ei2:
        await ac.park(Lease(job.job_id, job.epoch))
    assert ei2.value.code == "job_not_running"


async def test_auth_failures_are_typed(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    await hello(ac)
    worker.status = "revoked"
    with pytest.raises(WorkerRevoked):
        await ac.get_config()
    bad, _ = make_clients(bdr, "bdrw_" + "B" * 43)
    with pytest.raises(WorkerUnauthorized):
        await bad.get_config()
    pending = bdr.add_worker(status="pending")
    pc, _ = make_clients(bdr, pending.token)
    assert isinstance(await pc.get_config(), PendingResponse)
    with pytest.raises(PendingApproval):
        await pc.claim(ClaimRequest(boot_id=uuid.uuid4(), capacity={}))


async def test_config_etag_returns_none_when_unchanged(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    await hello(ac)
    cfg = await ac.get_config()
    assert isinstance(cfg, ConfigResponse) and cfg.config_version == bdr.config_version
    assert await ac.get_config(cfg.config_version) is None
    bdr.bump_config()
    newer = await ac.get_config(cfg.config_version)
    assert newer is not None and newer.config_version == cfg.config_version + 1


async def test_retries_run_out_as_unreachable(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    await hello(ac)
    bdr.inject("config", 502, 503, 504)
    with pytest.raises(BdrUnreachable):
        await ac.get_config()
    assert len(bdr.calls_to("config")) == 3


async def test_rate_limited_is_retried_and_other_4xx_is_not(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    await hello(ac)
    bdr.inject("config", httpx.Response(429, headers={"Retry-After": "0", "X-Error-Code": "rate_limited"}))
    assert await ac.get_config() is not None
    bdr.inject("config", "idor")
    before = len(bdr.calls_to("config"))
    with pytest.raises(BdrRejected) as ei:
        await ac.get_config()
    assert ei.value.code == "idor" and ei.value.status == 403
    assert len(bdr.calls_to("config")) == before + 1


async def test_artifact_upload_is_multipart_with_verified_sha(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac)
    lease = Lease(job.job_id, job.epoch)
    body = b'{"ok": true}'
    resp = await ac.upload_artifact(lease, "rfi_detail_json", "rfi.json", body, "application/json")
    call = bdr.calls_to("artifacts")[-1]
    assert call.body["kind"] == "rfi_detail_json" and call.body["file"] == body
    assert call.headers["idempotency-key"].startswith("artifact:rfi_detail_json:")
    assert resp.existed is False
    with pytest.raises(ValueError):
        await ac.upload_artifact(lease, "screenshot", "a.gif", b"GIF89a", "image/gif")


async def test_turn_responses_parse_by_status(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac)
    lease = Lease(job.job_id, job.epoch)
    r = await ac.turn(lease, TurnRequest(prev_turn_no=0))
    assert isinstance(r, TurnActionResponse) and r.action.tool == "navigate"
    bdr.inject("turns", httpx.Response(202, json={"retry_after": 5}))
    assert isinstance(await ac.turn(lease, TurnRequest(prev_turn_no=0)), TurnPendingResponse)


async def test_canary_page_and_session_fixture(bdr, worker):
    ac, _ = make_clients(bdr, worker.token)
    job = await hello_and_claim(bdr, ac, "canary")
    assert "BDR canary ok" in await ac.canary_page()
    from bdr_agent.protocol import SessionRequest
    sess = await ac.identity_session(Lease(job.job_id, job.epoch), "procore", SessionRequest(host="app.procore.com"))
    assert sess.status == "ok" and sess.cookies
    assert sess.cookies[0].value not in repr(sess.cookies[0])


def test_steps_idempotency_key_is_deterministic():
    a = [{"idem_key": "x:1"}, {"idem_key": "x:2"}]
    assert steps_idempotency_key(a) == steps_idempotency_key(list(a))
    assert steps_idempotency_key(a) != steps_idempotency_key(a[:1])


def test_sync_client_heartbeat(bdr, worker):
    import asyncio

    ac, sc = make_clients(bdr, worker.token)
    boot = asyncio.run(hello(ac))
    sc.boot_id = boot
    from bdr_agent.protocol import HeartbeatRequest
    resp = sc.heartbeat(HeartbeatRequest(boot_id=boot, config_version=1))
    assert resp.config_version == bdr.config_version and resp.controls == []


async def test_in_job_calls_outlast_a_bdr_deploy(bdr, worker):
    """SPEC 3.3: in-job calls keep retrying until the 15-minute abort, so a
    30-90 s deploy (more failures than the default 5-try budget) is survived."""
    from bdr_agent.client import BDR_UNREACHABLE_ABORT_SECONDS, IN_JOB_RETRY, RetryPolicy

    assert IN_JOB_RETRY.max_attempts is None
    assert IN_JOB_RETRY.deadline_s == float(BDR_UNREACHABLE_ABORT_SECONDS)
    ac, _ = make_clients(bdr, worker.token,
                         in_job_retry=RetryPolicy(max_attempts=None, deadline_s=30.0, base_s=0.001, cap_s=0.002))
    job = await hello_and_claim(bdr, ac)
    lease = Lease(job.job_id, job.epoch)
    bdr.inject("checkpoint", *([502] * 8), httpx.ConnectError("deploy"))
    await ac.put_checkpoint(lease, 1, {"phase": "seeded"})
    assert len(bdr.calls_to("checkpoint")) >= 9
    bdr.inject("turns", *([503] * 8))
    resp = await ac.turn(lease, TurnRequest(prev_turn_no=0))
    assert isinstance(resp, (TurnActionResponse, TurnPendingResponse))
    # Calls outside a job keep the short default budget.
    bdr.inject("config", 502, 503, 504)
    with pytest.raises(BdrUnreachable):
        await ac.get_config()
